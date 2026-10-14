"""Acceptance tests for `manage-sender-installation@1`.

Requirement numbers refer to `packs/manage-sender-installation-1.spec.md`.
Every database is a real `operational-store@1` store in a temporary
directory, every message is built with `ingest-protocol@1`, and every value
is synthetic.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from activity_tracker_portal.ingest_protocol import decode_message, encode_message, installation_fingerprint, message_sha256
from activity_tracker_portal.operational_store import (
    MessageHeader,
    RecordTypeOutcome,
    SyncCheckpointRecord,
    open_store,
)
from activity_tracker_portal.sender_installation import (
    ENROLLMENT_CODE_LIFETIME,
    Capabilities,
    ConfirmationResponse,
    EnrollmentOffer,
    InstallationRefusal,
    InstallationRetryable,
    answer_enrollment_confirmation,
    begin_draining,
    confirm_installation,
    current_advertisement,
    honor_capabilities,
    offer_enrollment,
    retire_destructively,
    retire_drained,
    stop_superseded_generations,
    withdraw_installation,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/manage-sender-installation.1.json").read_text(encoding="utf-8"))
OUTCOMES = {"refusal", "retryable"}
INPUT_MODES = {
    "operator_intent": "pure",
    "enrollment_confirmation": "pure",
    "portal_configuration": "pure",
    "current_time": "pure",
    "enrollment_randomness": "volatile",
    "operational_state": "effectful",
}

UTC = timezone.utc
T0 = datetime(2026, 10, 14, 9, 0, 0, 123456, tzinfo=UTC)
URL = "http://portal.invalid:8443"
CAPABILITIES = Capabilities((1,), (("heart-rate", (1,)), ("steps", (1,))), 500, 1_000_000)
LATER = Capabilities((1,), (("heart-rate", (1,)), ("steps", (1, 2))), 500, 1_000_000)
LATEST = Capabilities((1,), (("heart-rate", (1,)), ("steps", (1, 2))), 800, 1_000_000)
A, B, C, D = (f"00000000-0000-4000-8000-{n:012x}" for n in range(1, 5))
SECRET_CODE = "SYNTHETICCODE234"


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def advertisement(generation: int, capabilities: Capabilities = CAPABILITIES) -> bytes:
    return encode_message(
        {
            "wire_version": 1,
            "kind": "capability-advertisement",
            "generation": generation,
            "wire_versions": list(capabilities.wire_versions),
            "projection_schemas": [
                {"record_type": record_type, "versions": list(versions)} for record_type, versions in capabilities.projection_schemas
            ],
            "max_record_count": capabilities.max_record_count,
            "max_message_bytes": capabilities.max_message_bytes,
        }
    )


def confirmation(code: str, installation_id: str) -> bytes:
    return encode_message(
        {
            "wire_version": 1,
            "kind": "enrollment-confirmation",
            "enrollment_code": code,
            "sender_installation": installation_id,
            "fingerprint": installation_fingerprint(installation_id),
        }
    )


def code_digest(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


class Untouchable:
    """A store stand-in that fails the test on any use, showing a caller error came first."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the store was used ({name}) before the caller error")


UNTOUCHABLE: Any = Untouchable()


class Randomness:
    """A random source that returns the given chunks in order and records each request."""

    def __init__(self, *chunks: bytes) -> None:
        self.chunks = list(chunks)
        self.requests: list[int] = []

    def __call__(self, size: int) -> bytes:
        self.requests.append(size)
        return self.chunks.pop(0)


def checkpoint() -> SyncCheckpointRecord:
    return SyncCheckpointRecord(
        run_sequence=1,
        trigger="background",
        started_at="2026-10-14T08:59:00.000Z",
        ended_at="2026-10-14T08:59:30.000Z",
        elapsed_ms=30000,
        background_scheduling="available",
        record_types=(RecordTypeOutcome(record_type="steps", outcome="no-change", reason=None, source_data_staged=False),),
        outbox_messages_before_checkpoint=0,
        outbox_bytes_before_checkpoint=0,
    )


class Case(unittest.TestCase):
    """A fresh Operational Store per test, with helpers that drive it through this unit."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "portal.db")
        self.store = open_store(self.path, reserve_bytes=0, free_bytes=lambda _: 10**12, busy_timeout_ms=100)
        self.addCleanup(self.store.close)
        self.next_random = 0

    def random(self) -> Randomness:
        self.next_random += 1
        return Randomness(bytes((self.next_random * 16 + index) % 256 for index in range(10)))

    def honor(self, capabilities: Capabilities = CAPABILITIES, minutes: float = 0) -> int:
        return honor_capabilities(self.store, capabilities, at(minutes))

    def offer(self, minutes: float = 0) -> EnrollmentOffer:
        return offer_enrollment(self.store, URL, at(minutes), random_bytes=self.random())

    def enroll(self, installation_id: str, minutes: float = 0) -> ConfirmationResponse:
        offer = self.offer(minutes)
        response = answer_enrollment_confirmation(self.store, confirmation(offer.code, installation_id), at(minutes))
        self.assertEqual("pending", response.outcome, response.failure)
        return response

    def activate(self, installation_id: str, minutes: float = 0) -> None:
        self.enroll(installation_id, minutes)
        confirm_installation(self.store, installation_id, installation_fingerprint(installation_id), at(minutes))

    def deliver(self, installation_id: str, sequence: int, generation: int = 1, minutes: float = 1) -> None:
        """Archive a capability acknowledgement, the simplest Outbox Message, at this sequence."""
        envelope = encode_message(
            {
                "wire_version": 1,
                "kind": "capability-acknowledgement",
                "sender_installation": installation_id,
                "sequence": sequence,
                "capability_generation": generation,
            }
        )
        self.store.archive_capability_acknowledgement(self.header(installation_id, sequence, envelope, generation, minutes))

    def deliver_checkpoint(self, installation_id: str, sequence: int, minutes: float = 1) -> None:
        envelope = json.dumps({"synthetic": "checkpoint", "sequence": sequence}).encode()
        self.store.archive_sync_checkpoint(self.header(installation_id, sequence, envelope, 1, minutes), checkpoint())

    def header(self, installation_id: str, sequence: int, envelope: bytes, generation: int, minutes: float) -> MessageHeader:
        digest = message_sha256(envelope)
        receipt = encode_message(
            {"wire_version": 1, "kind": "delivery-receipt", "sender_installation": installation_id, "sequence": sequence, "message_sha256": digest}
        )
        return MessageHeader(installation_id, sequence, digest, generation, at(minutes), envelope, receipt)

    def status(self, installation_id: str) -> Any:
        with self.store.read() as view:
            return view.installation(installation_id)

    def honored(self) -> list[int]:
        with self.store.read() as view:
            return [generation.generation for generation in view.honored_generations()]

    def dump(self) -> list[str]:
        connection = sqlite3.connect(self.path)
        try:
            return list(connection.iterdump())
        finally:
            connection.close()

    def blocked(self) -> sqlite3.Connection:
        blocker = sqlite3.connect(self.path, isolation_level=None, timeout=0)
        blocker.execute("BEGIN IMMEDIATE")
        self.addCleanup(blocker.close)
        return blocker

    def assertRefused(self, failure: str, call: Callable[[], object]) -> InstallationRefusal:
        before = self.dump()
        with self.assertRaises(InstallationRefusal) as caught:
            call()
        self.assertEqual(failure, caught.exception.failure)
        self.assertEqual(before, self.dump(), f"{failure} changed the store")
        return caught.exception

    def assertAnswer(self, data: bytes, failure: str, minutes: float = 0) -> ConfirmationResponse:
        before = self.dump()
        response = answer_enrollment_confirmation(self.store, data, at(minutes))
        self.assertEqual(ConfirmationResponse("refused", None, failure), response)
        self.assertEqual(before, self.dump(), f"{failure} changed the store")
        return response


class R1GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual(CONTRACT["scope"], "manage-sender-installation")
        self.assertEqual(CONTRACT["version"], "1")
        self.assertEqual(CONTRACT["kind"], "integration")
        self.assertEqual({item["name"]: item["mode"] for item in CONTRACT["inputs"]}, INPUT_MODES)
        names = [failure["name"] for failure in CONTRACT["named_failures"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual({failure["outcome"] for failure in CONTRACT["named_failures"]}, OUTCOMES)
        self.assertEqual([effect["name"] for effect in CONTRACT["observable_effects"]], ["operational-store-write"])
        self.assertEqual(
            CONTRACT["dependencies"],
            [{"scope": "ingest-protocol", "version": "1"}, {"scope": "operational-store", "version": "1"}],
        )
        self.assertTrue(CONTRACT["authority"]["owns"])
        self.assertTrue(CONTRACT["authority"]["does_not_own"])
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["python"])
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_rows_do_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")

        def row(scope: str) -> list[str]:
            rows = [line for line in scope_map.splitlines() if line.startswith(f"| `{scope}` |")]
            self.assertEqual(len(rows), 1, scope)
            return [cell.strip() for cell in rows[0].strip("|").split("|")]

        _, kind, purpose, inputs, outputs, depends_on = row("manage-sender-installation")
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(depends_on, ", ".join(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]))
        self.assertIn("keeping one active installation", purpose)
        for phrase in ("Operator intent", "installation identity and fingerprint", "final sequence", "configured capabilities"):
            self.assertIn(phrase, inputs)
        for phrase in ("Enrollment offer", "confirmation response", "capability generation", "named refusal", "never-reported Polling Freshness"):
            self.assertIn(phrase, outputs)
        _, sender_kind, sender_purpose, _, _, sender_depends_on = row("manage-sender-enrollment")
        self.assertEqual(sender_kind, "integration")
        self.assertIn("drain, unlink, or destructively reset", sender_purpose)
        self.assertEqual(sender_depends_on, "`ingest-protocol`")
        flow = (ROOT / "docs/governance/delivery-flow.md").read_text(encoding="utf-8")
        phase = next(line for line in flow.splitlines() if line.startswith("| 3. Receipt-bound delivery |"))
        self.assertIn("`manage-sender-installation`", phase)
        self.assertIn("`manage-sender-enrollment`", phase)


class R2CallerErrors(Case):
    def test_naive_times_are_caller_errors(self) -> None:
        naive = datetime(2026, 10, 14, 9, 0)
        calls: list[Callable[[], object]] = [
            lambda: offer_enrollment(UNTOUCHABLE, URL, naive, random_bytes=self.random()),
            lambda: answer_enrollment_confirmation(UNTOUCHABLE, confirmation(SECRET_CODE, A), naive),
            lambda: confirm_installation(UNTOUCHABLE, A, installation_fingerprint(A), naive),
            lambda: withdraw_installation(UNTOUCHABLE, A, naive),
            lambda: begin_draining(UNTOUCHABLE, A, naive),
            lambda: retire_drained(UNTOUCHABLE, A, 0, naive),
            lambda: retire_destructively(UNTOUCHABLE, A, naive, acknowledge_possible_loss=True),
            lambda: honor_capabilities(UNTOUCHABLE, CAPABILITIES, naive),
            lambda: stop_superseded_generations(UNTOUCHABLE, naive),
        ]
        for call in calls:
            with self.assertRaises(ValueError):
                call()

    def test_invalid_portal_url_is_a_caller_error(self) -> None:
        for url in ("", "ftp://portal.invalid", "http://Portal.invalid", "http://portal.invalid/", "portal.invalid", "http://portal.invalid:8443/enroll"):
            with self.assertRaises(ValueError, msg=url):
                offer_enrollment(UNTOUCHABLE, url, T0, random_bytes=self.random())
        self.honor()
        offer_enrollment(self.store, "https://portal.invalid", T0, random_bytes=self.random())

    def test_invalid_capabilities_are_caller_errors(self) -> None:
        invalid = [
            Capabilities((), (("steps", (1,)),), 500, 1_000_000),
            Capabilities((2, 1), (("steps", (1,)),), 500, 1_000_000),
            Capabilities((1,), (("steps", (1,)), ("heart-rate", (1,))), 500, 1_000_000),
            Capabilities((1,), (("Steps", (1,)),), 500, 1_000_000),
            Capabilities((1,), (("steps", (2, 1)),), 500, 1_000_000),
            Capabilities((1,), (("steps", (1,)),), 0, 1_000_000),
            Capabilities((1,), (("steps", (1,)),), 500, 4095),
        ]
        for capabilities in invalid:
            with self.assertRaises(ValueError, msg=repr(capabilities)):
                honor_capabilities(UNTOUCHABLE, capabilities, T0)

    def test_negative_final_sequence_is_a_caller_error(self) -> None:
        with self.assertRaises(ValueError):
            retire_drained(UNTOUCHABLE, A, -1, at(2))

    def test_wrong_length_random_source_is_a_caller_error(self) -> None:
        for chunk in (bytes(9), bytes(11), b""):
            with self.assertRaises(ValueError, msg=len(chunk)):
                offer_enrollment(UNTOUCHABLE, URL, T0, random_bytes=Randomness(chunk))


class R3EnrollmentOffers(Case):
    def test_offer_code_is_base32_of_ten_random_bytes(self) -> None:
        self.honor()
        source = Randomness(bytes(range(250, 256)) + bytes(range(4)))
        offer = offer_enrollment(self.store, URL, T0, random_bytes=source)
        self.assertEqual([10], source.requests)
        self.assertEqual(base64.b32encode(bytes(range(250, 256)) + bytes(range(4))).decode("ascii"), offer.code)
        self.assertRegex(offer.code, r"\A[A-Z2-7]{16}\Z")

    def test_offer_records_only_the_code_digest_and_expiry(self) -> None:
        self.honor()
        before = set(self.dump())
        offer = self.offer()
        added = sorted(set(self.dump()) - before)
        self.assertEqual(1, len(added), added)
        self.assertIn('INSERT INTO "enrollment_offers"', added[0])
        self.assertIn(code_digest(offer.code), added[0])
        self.assertNotIn(offer.code, "\n".join(self.dump()))
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        rows = connection.execute("SELECT code_digest, expires_at, consumed_at FROM enrollment_offers").fetchall()
        self.assertEqual([(code_digest(offer.code), "2026-10-14T09:10:00.123000Z", None)], rows)

    def test_offer_message_is_canonical_and_expires_after_ten_minutes(self) -> None:
        self.assertEqual(timedelta(minutes=10), ENROLLMENT_CODE_LIFETIME)
        self.honor()
        offer = self.offer()
        self.assertEqual(datetime(2026, 10, 14, 9, 10, 0, 123000, tzinfo=UTC), offer.expires_at)
        expected = {
            "wire_version": 1,
            "kind": "enrollment-offer",
            "enrollment_code": offer.code,
            "portal_url": URL,
            "expires_at": "2026-10-14T09:10:00.123Z",
        }
        self.assertEqual(expected, decode_message(offer.message))
        self.assertEqual(encode_message(expected), offer.message)

    def test_offer_requires_an_honored_generation(self) -> None:
        self.assertRefused("no-capability-generation", lambda: offer_enrollment(self.store, URL, T0, random_bytes=self.random()))

    def test_repeated_code_is_refused(self) -> None:
        self.honor()
        chunk = bytes(range(10))
        offer_enrollment(self.store, URL, T0, random_bytes=Randomness(chunk))
        self.assertRefused("enrollment-code-exists", lambda: offer_enrollment(self.store, URL, at(1), random_bytes=Randomness(chunk)))

    def test_outstanding_offers_are_independent(self) -> None:
        self.honor()
        first = self.offer(0)
        second = self.offer(5)
        third = self.offer(5)
        self.assertEqual("pending", answer_enrollment_confirmation(self.store, confirmation(second.code, B), at(9)).outcome)
        self.assertEqual("pending", answer_enrollment_confirmation(self.store, confirmation(third.code, C), at(14)).outcome)
        self.assertAnswer(confirmation(first.code, A), "enrollment-code-expired", minutes=12)


class R4AnsweringConfirmations(Case):
    def test_confirmation_returns_the_current_advertisement_bytes(self) -> None:
        self.honor(CAPABILITIES)
        self.honor(LATER, 1)
        offer = self.offer(2)
        response = answer_enrollment_confirmation(self.store, confirmation(offer.code, A), at(3))
        self.assertEqual(ConfirmationResponse("pending", advertisement(2, LATER), None), response)
        self.assertEqual(current_advertisement(self.store), response.body)

    def test_confirmation_creates_a_pending_installation(self) -> None:
        self.honor()
        self.enroll(A, minutes=2)
        status = self.status(A)
        self.assertEqual("pending", status.state)
        self.assertEqual(installation_fingerprint(A), status.fingerprint)
        self.assertEqual(at(2), status.enrolled_at)
        self.assertIsNone(status.activated_at)

    def test_undecodable_confirmations_are_refused_without_a_body(self) -> None:
        self.honor()
        offer = self.offer()
        valid = json.loads(confirmation(offer.code, A))
        spaced = json.dumps(valid, sort_keys=True).encode()
        version_two = json.dumps({**valid, "wire_version": 2}, sort_keys=True, separators=(",", ":")).encode()
        wrong_fingerprint = json.dumps({**valid, "fingerprint": "0" * 64}, sort_keys=True, separators=(",", ":")).encode()
        cases = [
            (b"", "malformed-message"),
            (b"not json", "malformed-message"),
            (spaced, "noncanonical-message"),
            (version_two, "unsupported-wire-protocol"),
            (wrong_fingerprint, "malformed-message"),
        ]
        for data, failure in cases:
            self.assertAnswer(data, failure)
        self.assertEqual("pending", answer_enrollment_confirmation(self.store, confirmation(offer.code, A), T0).outcome)

    def test_other_message_kinds_are_malformed(self) -> None:
        self.honor()
        offer = self.offer()
        acknowledgement = encode_message(
            {"wire_version": 1, "kind": "capability-acknowledgement", "sender_installation": A, "sequence": 1, "capability_generation": 1}
        )
        for data in (offer.message, acknowledgement, advertisement(1)):
            self.assertAnswer(data, "malformed-message")

    def test_unknown_consumed_and_expired_codes_are_refused(self) -> None:
        self.honor()
        self.assertAnswer(confirmation(SECRET_CODE, A), "enrollment-code-unknown")
        used = self.offer()
        answer_enrollment_confirmation(self.store, confirmation(used.code, A), at(1))
        self.assertAnswer(confirmation(used.code, B), "enrollment-code-consumed", minutes=2)
        stale = self.offer(3)
        self.assertAnswer(confirmation(stale.code, C), "enrollment-code-expired", minutes=14)

    def test_code_is_usable_until_its_expiry(self) -> None:
        self.honor()
        early = self.offer()
        late = self.offer()
        just_before = early.expires_at - timedelta(microseconds=1)
        self.assertEqual("pending", answer_enrollment_confirmation(self.store, confirmation(early.code, A), just_before).outcome)
        before = self.dump()
        response = answer_enrollment_confirmation(self.store, confirmation(late.code, B), late.expires_at)
        self.assertEqual(ConfirmationResponse("refused", None, "enrollment-code-expired"), response)
        self.assertEqual(before, self.dump())

    def test_confirmation_without_a_generation_is_refused_before_enrolling(self) -> None:
        self.store.record_enrollment_offer(code_digest(SECRET_CODE), at(10), T0)
        self.assertAnswer(confirmation(SECRET_CODE, A), "no-capability-generation")
        with self.store.read() as view:
            self.assertEqual((), view.installations())

    def test_transient_store_conditions_are_retryable_responses(self) -> None:
        self.honor()
        offer = self.offer()
        blocker = self.blocked()
        response = answer_enrollment_confirmation(self.store, confirmation(offer.code, A), at(1))
        blocker.rollback()
        retryable = encode_message({"wire_version": 1, "kind": "retryable-failure", "reason": "temporarily-unavailable"})
        self.assertEqual(ConfirmationResponse("retryable", retryable, "store-busy"), response)
        with self.store.read() as view:
            self.assertEqual((), view.installations())
        self.assertEqual("pending", answer_enrollment_confirmation(self.store, confirmation(offer.code, A), at(2)).outcome)


class R5RetriedConfirmations(Case):
    def test_retried_confirmation_returns_the_advertisement_without_writing(self) -> None:
        self.honor()
        offer = self.offer()
        data = confirmation(offer.code, A)
        first = answer_enrollment_confirmation(self.store, data, at(1))
        for step in (None, "confirm", "drain"):
            if step == "confirm":
                confirm_installation(self.store, A, installation_fingerprint(A), at(2))
            if step == "drain":
                begin_draining(self.store, A, at(3))
            before = self.dump()
            self.assertEqual(first, answer_enrollment_confirmation(self.store, data, at(4)))
            self.assertEqual(first, answer_enrollment_confirmation(self.store, confirmation(SECRET_CODE, A), at(4)))
            self.assertEqual(before, self.dump(), step)
        self.assertEqual(ConfirmationResponse("pending", advertisement(1), None), first)

    def test_ended_identity_never_enrolls_again(self) -> None:
        self.honor()
        self.enroll(A)
        withdraw_installation(self.store, A, at(1))
        self.activate(B, 2)
        retire_destructively(self.store, B, at(3), acknowledge_possible_loss=True)
        for installation_id in (A, B):
            fresh = self.offer(4)
            self.assertAnswer(confirmation(fresh.code, installation_id), "installation-ended", minutes=5)
            self.assertEqual("pending", answer_enrollment_confirmation(self.store, confirmation(fresh.code, C if installation_id == A else D), at(6)).outcome)

    def test_consumed_code_refuses_a_new_identity(self) -> None:
        self.honor()
        offer = self.offer()
        answer_enrollment_confirmation(self.store, confirmation(offer.code, A), at(1))
        self.assertAnswer(confirmation(offer.code, B), "enrollment-code-consumed", minutes=1)


class R6OperatorConfirmation(Case):
    def test_confirming_requires_the_compared_fingerprint(self) -> None:
        self.honor()
        self.enroll(A)
        self.enroll(B)
        for fingerprint in (installation_fingerprint(B), "0" * 64):
            self.assertRefused("fingerprint-mismatch", lambda: confirm_installation(self.store, A, fingerprint, at(1)))
        status = confirm_installation(self.store, A, installation_fingerprint(A), at(2))
        self.assertEqual(("active", at(2)), (status.state, status.activated_at))
        self.assertEqual(status, self.status(A))

    def test_confirmation_activates_only_without_a_delivering_installation(self) -> None:
        self.honor()
        self.activate(A)
        self.enroll(B, 1)
        self.assertRefused("active-installation-exists", lambda: confirm_installation(self.store, B, installation_fingerprint(B), at(2)))
        begin_draining(self.store, A, at(3))
        self.assertRefused("active-installation-exists", lambda: confirm_installation(self.store, B, installation_fingerprint(B), at(4)))
        retire_drained(self.store, A, 0, at(5))
        self.assertEqual("active", confirm_installation(self.store, B, installation_fingerprint(B), at(6)).state)

    def test_other_pending_installations_stay_pending(self) -> None:
        self.honor()
        self.enroll(B)
        self.enroll(C)
        confirm_installation(self.store, B, installation_fingerprint(B), at(1))
        self.assertEqual("pending", self.status(C).state)

    def test_pending_installation_can_be_withdrawn(self) -> None:
        self.honor()
        self.enroll(A)
        status = withdraw_installation(self.store, A, at(1))
        self.assertEqual(("withdrawn", at(1)), (status.state, status.withdrawn_at))

    def test_only_pending_installations_can_be_confirmed_or_withdrawn(self) -> None:
        self.honor()
        self.activate(A)
        self.enroll(B)
        withdraw_installation(self.store, B, at(1))
        for installation_id in (A, B):
            self.assertRefused(
                "installation-transition-invalid",
                lambda: confirm_installation(self.store, installation_id, installation_fingerprint(installation_id), at(2)),
            )
            self.assertRefused("installation-transition-invalid", lambda: withdraw_installation(self.store, installation_id, at(2)))


class R7DrainingAndRetirement(Case):
    def test_draining_starts_only_from_active(self) -> None:
        self.honor()
        self.enroll(A)
        self.assertRefused("installation-transition-invalid", lambda: begin_draining(self.store, A, at(1)))
        confirm_installation(self.store, A, installation_fingerprint(A), at(1))
        status = begin_draining(self.store, A, at(2))
        self.assertEqual(("draining", at(2)), (status.state, status.draining_at))
        self.assertRefused("installation-transition-invalid", lambda: begin_draining(self.store, A, at(3)))
        retire_drained(self.store, A, 0, at(4))
        self.assertRefused("installation-transition-invalid", lambda: begin_draining(self.store, A, at(5)))

    def test_drained_retirement_requires_the_final_sequence(self) -> None:
        self.honor()
        self.activate(A)
        for sequence in (1, 2, 3):
            self.deliver(A, sequence)
        begin_draining(self.store, A, at(2))
        for final in (0, 2, 4):
            self.assertRefused("drain-sequence-mismatch", lambda: retire_drained(self.store, A, final, at(3)))
        status = retire_drained(self.store, A, 3, at(4))
        self.assertEqual(("retired", "drained", at(4)), (status.state, status.retirement_kind, status.retired_at))

    def test_drained_retirement_refuses_sequence_gaps(self) -> None:
        self.honor()
        self.activate(A)
        self.deliver(A, 1)
        self.deliver(A, 3)
        begin_draining(self.store, A, at(2))
        self.assertRefused("drain-has-gaps", lambda: retire_drained(self.store, A, 3, at(3)))
        self.assertEqual("retired", retire_destructively(self.store, A, at(4), acknowledge_possible_loss=True).state)

    def test_never_delivering_installation_drains_at_sequence_zero(self) -> None:
        self.honor()
        self.activate(A)
        begin_draining(self.store, A, at(1))
        self.assertRefused("drain-sequence-mismatch", lambda: retire_drained(self.store, A, 1, at(2)))
        self.assertEqual("drained", retire_drained(self.store, A, 0, at(3)).retirement_kind)

    def test_destructive_retirement_requires_acknowledged_loss(self) -> None:
        self.honor()
        self.activate(A)
        self.assertRefused(
            "loss-not-acknowledged", lambda: retire_destructively(self.store, A, at(1), acknowledge_possible_loss=False)
        )
        self.assertEqual("active", self.status(A).state)

    def test_destructive_retirement_from_active_or_draining(self) -> None:
        self.honor()
        self.activate(A)
        status = retire_destructively(self.store, A, at(1), acknowledge_possible_loss=True)
        self.assertEqual(("retired", "destructive", at(1)), (status.state, status.retirement_kind, status.retired_at))
        self.activate(B, 2)
        begin_draining(self.store, B, at(3))
        self.assertEqual("destructive", retire_destructively(self.store, B, at(4), acknowledge_possible_loss=True).retirement_kind)
        self.enroll(C, 5)
        for installation_id in (A, C):
            self.assertRefused(
                "installation-transition-invalid",
                lambda: retire_destructively(self.store, installation_id, at(6), acknowledge_possible_loss=True),
            )
        self.activate(D, 7)
        self.assertRefused("installation-transition-invalid", lambda: retire_drained(self.store, D, 0, at(8)))


class R8ReplacementUnlinkingAndLoss(Case):
    def test_replacement_starts_with_no_operational_state(self) -> None:
        self.honor()
        self.activate(A)
        self.deliver(A, 1, minutes=1)
        self.deliver(A, 2, minutes=2)
        self.deliver_checkpoint(A, 3, minutes=3)
        self.enroll(B, 4)
        self.assertRefused("active-installation-exists", lambda: confirm_installation(self.store, B, installation_fingerprint(B), at(5)))
        begin_draining(self.store, A, at(6))
        retire_drained(self.store, A, 3, at(7))
        status = confirm_installation(self.store, B, installation_fingerprint(B), at(8))
        self.assertEqual("active", status.state)
        self.assertIsNone(status.highest_sequence)
        self.assertIsNone(status.last_delivery_at)
        self.assertIsNone(status.latest_checkpoint_receipt_at)
        self.assertIsNone(status.background_scheduling)
        old = self.status(A)
        self.assertEqual((3, at(3), at(3)), (old.highest_sequence, old.last_delivery_at, old.latest_checkpoint_receipt_at))

    def test_unlink_leaves_no_delivering_installation(self) -> None:
        self.honor()
        self.activate(A)
        self.deliver(A, 1)
        begin_draining(self.store, A, at(2))
        retire_drained(self.store, A, 1, at(3))
        with self.store.read() as view:
            self.assertEqual([], [status.installation_id for status in view.installations() if status.state in ("active", "draining")])
        self.activate(B, 4)
        self.assertEqual("active", self.status(B).state)

    def test_lost_phone_is_replaced_after_destructive_retirement(self) -> None:
        self.honor()
        self.activate(A)
        self.deliver(A, 1)
        self.enroll(B, 2)
        retire_destructively(self.store, A, at(3), acknowledge_possible_loss=True)
        self.assertEqual("active", confirm_installation(self.store, B, installation_fingerprint(B), at(4)).state)
        self.assertEqual("destructive", self.status(A).retirement_kind)


class R9CapabilityGenerations(Case):
    def test_configured_capabilities_get_the_next_generation(self) -> None:
        self.assertEqual(1, self.honor(CAPABILITIES))
        self.assertEqual(2, self.honor(LATER, 1))
        self.assertEqual(3, self.honor(CAPABILITIES, 2))
        self.assertEqual([1, 2, 3], self.honored())

    def test_unchanged_capabilities_keep_their_generation(self) -> None:
        self.assertEqual(1, self.honor(CAPABILITIES))
        before = self.dump()
        self.assertEqual(1, self.honor(CAPABILITIES, 1))
        self.assertEqual(before, self.dump())

    def test_advertisement_bytes_are_exact_and_canonical(self) -> None:
        self.honor(CAPABILITIES)
        self.honor(LATER, 1)
        with self.store.read() as view:
            stored = [generation.advertisement for generation in view.honored_generations()]
        self.assertEqual([advertisement(1, CAPABILITIES), advertisement(2, LATER)], stored)
        for data in stored:
            self.assertEqual(data, encode_message(decode_message(data)))

    def test_superseded_generations_stop_only_below_the_latest_acknowledgement(self) -> None:
        self.honor(CAPABILITIES)
        self.honor(LATER, 1)
        self.honor(LATEST, 2)
        self.assertEqual((), stop_superseded_generations(self.store, at(3)))
        self.activate(A, 3)
        self.deliver(A, 1, generation=2, minutes=4)
        self.assertEqual((1,), stop_superseded_generations(self.store, at(5)))
        self.assertEqual([2, 3], self.honored())
        self.assertEqual((), stop_superseded_generations(self.store, at(6)))
        self.deliver(A, 2, generation=3, minutes=7)
        self.assertEqual((2,), stop_superseded_generations(self.store, at(8)))
        self.assertEqual([3], self.honored())

    def test_latest_acknowledgement_is_the_highest_not_the_newest(self) -> None:
        self.honor(CAPABILITIES)
        self.honor(LATER, 1)
        self.honor(LATEST, 2)
        self.activate(A, 3)
        self.deliver(A, 1, generation=3, minutes=4)
        self.deliver(A, 2, generation=2, minutes=5)
        self.assertEqual((1, 2), stop_superseded_generations(self.store, at(6)))
        self.assertEqual([3], self.honored())

    def test_generations_are_kept_while_an_installation_is_pending(self) -> None:
        self.honor(CAPABILITIES)
        self.honor(LATER, 1)
        self.activate(A, 2)
        self.deliver(A, 1, generation=2, minutes=3)
        self.enroll(B, 4)
        self.assertRefused("pending-installation-exists", lambda: stop_superseded_generations(self.store, at(5)))
        withdraw_installation(self.store, B, at(6))
        self.assertEqual((1,), stop_superseded_generations(self.store, at(7)))

    def test_current_advertisement_is_the_highest_generation(self) -> None:
        self.assertRefused("no-capability-generation", lambda: current_advertisement(self.store))
        self.honor(CAPABILITIES)
        self.assertEqual(advertisement(1, CAPABILITIES), current_advertisement(self.store))
        self.honor(LATER, 1)
        self.assertEqual(advertisement(2, LATER), current_advertisement(self.store))
        self.activate(A, 2)
        self.deliver(A, 1, generation=2, minutes=3)
        stop_superseded_generations(self.store, at(4))
        self.assertEqual(advertisement(2, LATER), current_advertisement(self.store))


class R10RefusalsChangeNothing(Case):
    def test_every_refusal_leaves_the_store_unchanged(self) -> None:
        self.honor(CAPABILITIES)
        self.honor(LATER, 1)
        self.activate(A, 2)
        self.deliver(A, 1, generation=2, minutes=3)
        self.deliver(A, 3, generation=2, minutes=4)
        self.enroll(B, 5)
        used = self.offer(6)
        answer_enrollment_confirmation(self.store, confirmation(used.code, C), at(6))
        withdraw_installation(self.store, C, at(7))
        stale = self.offer(7)
        repeated = bytes((self.next_random * 16 + index) % 256 for index in range(10))
        refusals: list[tuple[str, Callable[[], object]]] = [
            ("enrollment-code-exists", lambda: offer_enrollment(self.store, URL, at(8), random_bytes=Randomness(repeated))),
            ("installation-unknown", lambda: confirm_installation(self.store, D, installation_fingerprint(D), at(8))),
            ("fingerprint-mismatch", lambda: confirm_installation(self.store, B, installation_fingerprint(A), at(8))),
            ("installation-transition-invalid", lambda: withdraw_installation(self.store, A, at(8))),
            ("active-installation-exists", lambda: confirm_installation(self.store, B, installation_fingerprint(B), at(8))),
            ("loss-not-acknowledged", lambda: retire_destructively(self.store, A, at(8), acknowledge_possible_loss=False)),
            ("pending-installation-exists", lambda: stop_superseded_generations(self.store, at(8))),
        ]
        for failure, call in refusals:
            self.assertRefused(failure, call)
        for data, failure in (
            (b"{}", "malformed-message"),
            (confirmation(SECRET_CODE, D), "enrollment-code-unknown"),
            (confirmation(used.code, D), "enrollment-code-consumed"),
            (confirmation(stale.code, D), "enrollment-code-expired"),
            (confirmation(stale.code, C), "installation-ended"),
        ):
            self.assertAnswer(data, failure, minutes=20)
        begin_draining(self.store, A, at(21))
        self.assertRefused("drain-sequence-mismatch", lambda: retire_drained(self.store, A, 1, at(22)))
        self.assertRefused("drain-has-gaps", lambda: retire_drained(self.store, A, 3, at(22)))

    def test_retryable_conditions_leave_the_store_unchanged(self) -> None:
        self.honor()
        self.activate(A)
        self.deliver(A, 1, generation=1, minutes=1)
        self.honor(LATER, 2)
        self.deliver(A, 2, generation=2, minutes=3)
        self.enroll(B, 4)
        withdraw_installation(self.store, B, at(4))
        self.enroll(C, 5)
        offer = self.offer(5)
        writes: list[Callable[[], object]] = [
            lambda: offer_enrollment(self.store, URL, at(6), random_bytes=self.random()),
            lambda: withdraw_installation(self.store, C, at(6)),
            lambda: begin_draining(self.store, A, at(6)),
            lambda: retire_destructively(self.store, A, at(6), acknowledge_possible_loss=True),
            lambda: honor_capabilities(self.store, LATEST, at(6)),
        ]
        before = self.dump()
        blocker = self.blocked()
        for call in writes:
            with self.assertRaises(InstallationRetryable) as caught:
                call()
            self.assertEqual("store-busy", caught.exception.failure)
        response = answer_enrollment_confirmation(self.store, confirmation(offer.code, D), at(6))
        self.assertEqual(("retryable", "store-busy"), (response.outcome, response.failure))
        blocker.rollback()
        self.assertEqual(before, self.dump())
        withdraw_installation(self.store, C, at(7))
        begin_draining(self.store, A, at(8))
        before = self.dump()
        blocker = self.blocked()
        for call in (lambda: retire_drained(self.store, A, 2, at(9)), lambda: stop_superseded_generations(self.store, at(9))):
            with self.assertRaises(InstallationRetryable) as busy:
                call()
            self.assertEqual("store-busy", busy.exception.failure)
        # A read-only refusal needs no write, so it is the same under contention.
        with self.assertRaises(InstallationRefusal) as unknown:
            confirm_installation(self.store, D, installation_fingerprint(D), at(9))
        self.assertEqual("installation-unknown", unknown.exception.failure)
        blocker.rollback()
        self.assertEqual(before, self.dump())


    def test_stopping_resumes_after_a_transient_failure(self) -> None:
        self.honor(CAPABILITIES)
        self.honor(LATER, 1)
        self.honor(LATEST, 2)
        self.activate(A, 3)
        self.deliver(A, 1, generation=3, minutes=4)
        case = self

        class BusyOnSecondStop:
            """The real store, made busy just before the second generation is stopped."""

            def __init__(self, store: Any) -> None:
                self.store = store
                self.stops = 0
                self.blocker: sqlite3.Connection | None = None

            def __getattr__(self, name: str) -> Any:
                return getattr(self.store, name)

            def stop_honoring_generation(self, generation: int, at: datetime) -> None:
                self.stops += 1
                if self.stops == 2:
                    self.blocker = case.blocked()
                self.store.stop_honoring_generation(generation, at)

        proxy = BusyOnSecondStop(self.store)
        with self.assertRaises(InstallationRetryable) as caught:
            stop_superseded_generations(proxy, at(5))
        self.assertEqual("store-busy", caught.exception.failure)
        assert proxy.blocker is not None
        proxy.blocker.rollback()
        self.assertEqual([2, 3], self.honored())
        self.assertEqual((2,), stop_superseded_generations(self.store, at(6)))
        self.assertEqual([3], self.honored())


class R11HealthDataFreeOutcomes(Case):
    def test_outcomes_carry_only_named_failures(self) -> None:
        self.honor()
        self.store.record_enrollment_offer(code_digest(SECRET_CODE), at(-1), at(-2))
        response = answer_enrollment_confirmation(self.store, confirmation(SECRET_CODE, A), T0)
        self.assertEqual(ConfirmationResponse("refused", None, "enrollment-code-expired"), response)
        self.assertNotIn(SECRET_CODE, repr(response))
        self.assertNotIn(A, repr(response))
        refusal = self.assertRefused("installation-unknown", lambda: confirm_installation(self.store, A, installation_fingerprint(A), T0))
        self.assertEqual(("installation-unknown",), refusal.args)
        self.assertNotIn(A, str(refusal))
        self.assertIsNone(refusal.__cause__)
        self.assertTrue(refusal.__context__ is None or refusal.__suppress_context__)
        blocker = self.blocked()
        with self.assertRaises(InstallationRetryable) as caught:
            honor_capabilities(self.store, LATER, T0)
        blocker.rollback()
        self.assertEqual(("store-busy",), caught.exception.args)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__context__ is None or caught.exception.__suppress_context__)

    def test_every_named_failure_is_reachable_and_in_the_contract(self) -> None:
        observed: set[tuple[str, str]] = set()

        def refused(call: Callable[[], object]) -> None:
            try:
                call()
            except InstallationRefusal as refusal:
                observed.add(("refusal", refusal.failure))
            except InstallationRetryable as retryable:
                observed.add(("retryable", retryable.failure))
            else:
                self.fail("expected a refusal")

        def answered(data: bytes, minutes: float = 0) -> None:
            response = answer_enrollment_confirmation(self.store, data, at(minutes))
            self.assertNotEqual("pending", response.outcome)
            observed.add(("retryable" if response.outcome == "retryable" else "refusal", str(response.failure)))

        refused(lambda: current_advertisement(self.store))
        self.honor()
        offer = self.offer()
        answered(b"")
        answered(json.dumps(json.loads(confirmation(offer.code, A)), sort_keys=True).encode())
        answered(json.dumps({**json.loads(confirmation(offer.code, A)), "wire_version": 2}, sort_keys=True, separators=(",", ":")).encode())
        answered(confirmation(SECRET_CODE, A))
        answer_enrollment_confirmation(self.store, confirmation(offer.code, A), at(1))
        answered(confirmation(offer.code, B), 1)
        answered(confirmation(self.offer(2).code, B), 13)
        repeated = bytes((self.next_random * 16 + index) % 256 for index in range(10))
        refused(lambda: offer_enrollment(self.store, URL, at(14), random_bytes=Randomness(repeated)))
        withdraw_installation(self.store, A, at(14))
        answered(confirmation(self.offer(15).code, A), 15)
        refused(lambda: confirm_installation(self.store, B, installation_fingerprint(B), at(16)))
        self.enroll(C, 16)
        refused(lambda: confirm_installation(self.store, C, "0" * 64, at(17)))
        refused(lambda: begin_draining(self.store, C, at(17)))
        refused(lambda: stop_superseded_generations(self.store, at(17)))
        refused(lambda: retire_destructively(self.store, C, at(17), acknowledge_possible_loss=False))
        self.activate(D, 18)
        refused(lambda: confirm_installation(self.store, C, installation_fingerprint(C), at(19)))
        self.deliver(D, 2, minutes=19)
        begin_draining(self.store, D, at(20))
        refused(lambda: retire_drained(self.store, D, 1, at(21)))
        refused(lambda: retire_drained(self.store, D, 2, at(21)))
        blocker = self.blocked()
        refused(lambda: withdraw_installation(self.store, C, at(22)))
        blocker.rollback()
        named = {(failure["outcome"], failure["name"]) for failure in CONTRACT["named_failures"]}
        self.assertEqual(named, observed)


class R12OrderOfChecks(Case):
    def test_checks_follow_the_documented_order(self) -> None:
        # offer_enrollment: no-capability-generation before enrollment-code-exists.
        chunk = bytes(range(10))
        self.store.record_enrollment_offer(code_digest(base64.b32encode(chunk).decode("ascii")), at(10), T0)
        self.assertRefused("no-capability-generation", lambda: offer_enrollment(self.store, URL, T0, random_bytes=Randomness(chunk)))
        # answer_enrollment_confirmation: decoding, then kind, then the generation, then the identity, then the code.
        self.store.enroll_installation(code_digest(base64.b32encode(chunk).decode("ascii")), A, installation_fingerprint(A), T0)
        self.assertAnswer(b"", "malformed-message")
        self.assertAnswer(advertisement(1), "malformed-message")
        self.assertAnswer(confirmation(SECRET_CODE, A), "no-capability-generation")
        self.honor()
        self.assertEqual("pending", answer_enrollment_confirmation(self.store, confirmation(SECRET_CODE, A), at(1)).outcome)
        withdraw_installation(self.store, A, at(2))
        self.assertAnswer(confirmation(SECRET_CODE, A), "installation-ended", minutes=3)
        consumed = self.offer(3)
        answer_enrollment_confirmation(self.store, confirmation(consumed.code, B), at(3))
        self.assertAnswer(confirmation(consumed.code, C), "enrollment-code-consumed", minutes=30)
        # confirm_installation: unknown, fingerprint, transition, active installation.
        self.assertRefused("installation-unknown", lambda: confirm_installation(self.store, D, "0" * 64, at(31)))
        confirm_installation(self.store, B, installation_fingerprint(B), at(31))
        self.assertRefused("fingerprint-mismatch", lambda: confirm_installation(self.store, B, "0" * 64, at(32)))
        self.assertRefused("installation-transition-invalid", lambda: confirm_installation(self.store, A, installation_fingerprint(A), at(32)))
        # retire_drained: transition before sequence, sequence before gaps.
        self.deliver(B, 1, minutes=32)
        self.deliver(B, 3, minutes=33)
        self.assertRefused("installation-transition-invalid", lambda: retire_drained(self.store, B, 9, at(34)))
        begin_draining(self.store, B, at(34))
        self.assertRefused("drain-sequence-mismatch", lambda: retire_drained(self.store, B, 2, at(35)))
        # retire_destructively: the acknowledgement first.
        self.assertRefused("loss-not-acknowledged", lambda: retire_destructively(self.store, D, at(35), acknowledge_possible_loss=False))
        self.assertRefused("loss-not-acknowledged", lambda: retire_destructively(self.store, A, at(35), acknowledge_possible_loss=False))
        self.assertRefused("installation-unknown", lambda: retire_destructively(self.store, D, at(35), acknowledge_possible_loss=True))


if __name__ == "__main__":
    unittest.main()
