"""Acceptance tests for `accept-ingest-batch@1`.

Requirement numbers refer to `packs/accept-ingest-batch-1.spec.md`. Every
database is a real `operational-store@2` database in a temporary directory,
every message is built with `ingest-protocol@1`, and every value is synthetic.
Assertions that something wrote nothing compare the whole database.
"""

from __future__ import annotations

import base64
import json
import random
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from activity_tracker_portal import ingest_protocol
from activity_tracker_portal.accept_ingest_batch import DeliveryResponse, accept_ingest_batch
from activity_tracker_portal.ingest_protocol import (
    DELIVERY_BLOCK_FAILURES,
    chain_next,
    chain_start,
    encode_message,
    evaluate_delivery,
    message_sha256,
    new_delivery_state,
)
from activity_tracker_portal.operational_store import (
    BatchItem,
    CompletionRecord,
    MessageHeader,
    StoreRetryable,
    open_store,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/accept-ingest-batch.1.json").read_text(encoding="utf-8"))

UTC = timezone.utc
T0 = datetime(2026, 10, 15, 9, 0, 0, 123456, tzinfo=UTC)
RESERVE = 1_000_000
PLENTY = 10**12
A = "00000000-0000-4000-8000-0000000000a1"
B = "00000000-0000-4000-8000-0000000000b2"
UNKNOWN = "00000000-0000-4000-8000-0000000000c3"
CYCLE = "00000000-0000-4000-8000-000000000c01"
CYCLE_2 = "00000000-0000-4000-8000-000000000c02"
MAX_RECORDS = 4
MAX_BYTES = 8192
HEALTH_MARKER = b"synthetic-health-marker"
IDENTITY_MARKER = "synthetic-identity-marker"


def advertisement(generation: int) -> dict[str, Any]:
    return {
        "wire_version": 1,
        "kind": "capability-advertisement",
        "generation": generation,
        "wire_versions": [1],
        "projection_schemas": [
            {"record_type": "heart-rate", "versions": [1]},
            {"record_type": "steps", "versions": [1, 2]},
        ],
        "max_record_count": MAX_RECORDS,
        "max_message_bytes": MAX_BYTES,
    }


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def projection(data: bytes) -> dict[str, str]:
    return {"kind": "projection", "data": base64.b64encode(data).decode("ascii")}


def retraction(identity: str) -> dict[str, str]:
    return {"kind": "retraction", "source_identity": identity}


def batch(
    sequence: int,
    *,
    installation: str = A,
    cycle: str = CYCLE,
    page: int = 1,
    fragment: int = 1,
    previous: str | None = None,
    record_type: str = "steps",
    schema: int = 1,
    kind: str = "initial-snapshot",
    generation: int = 1,
    items: list[dict[str, str]] | None = None,
) -> bytes:
    return encode_message({
        "wire_version": 1,
        "kind": "ingest-batch",
        "sender_installation": installation,
        "sequence": sequence,
        "capability_generation": generation,
        "record_type": record_type,
        "projection_schema_version": schema,
        "cycle_kind": kind,
        "cycle_id": cycle,
        "source_page_index": page,
        "fragment_index": fragment,
        "previous_chain": chain_start(cycle) if previous is None else previous,
        "items": [projection(b"synthetic-projection-%d" % sequence)] if items is None else items,
    })


def after(previous: bytes) -> str:
    """The chain value a fragment following `previous` must carry."""
    return chain_next(ingest_protocol.decode_message(previous)["previous_chain"], message_sha256(previous))


def control(kind: str, sequence: int, installation: str = A, generation: int = 1, **fields: Any) -> bytes:
    return encode_message({
        "wire_version": 1, "kind": kind, "sender_installation": installation, "sequence": sequence,
        "capability_generation": generation, **fields,
    })


def completion_message(sequence: int, *, cycle: str, record_type: str, kind: str, page_count: int,
                       fragment_count: int, record_count: int, final_chain: str) -> bytes:
    fields: dict[str, Any] = {
        "record_type": record_type, "cycle_id": cycle, "page_count": page_count, "fragment_count": fragment_count,
        "record_count": record_count, "final_chain": final_chain,
    }
    if kind == "change-cycle":
        return control("change-completion", sequence, **fields)
    return control("snapshot-completion", sequence, snapshot_kind=kind.split("-")[0], **fields)


def archive_completion(store, data: bytes, now: datetime) -> None:
    """Archive a completion as accept-sync-completion would, so cycles can be completed in tests."""
    message = ingest_protocol.decode_message(data)
    sha = message_sha256(data)
    receipt = encode_message({
        "wire_version": 1, "kind": "delivery-receipt", "sender_installation": message["sender_installation"],
        "sequence": message["sequence"], "message_sha256": sha,
    })
    store.archive_completion(
        MessageHeader(message["sender_installation"], message["sequence"], sha, message["capability_generation"], now, data, receipt),
        CompletionRecord(
            "snapshot" if message["kind"] == "snapshot-completion" else "change", message.get("snapshot_kind"),
            message["record_type"], message["cycle_id"], message["page_count"], message["fragment_count"],
            message["record_count"], message["final_chain"],
        ),
    )


def contents(path: Path) -> dict[str, object]:
    """The whole database: schema version, every schema object, and every row of every table, BLOBs included."""
    connection = sqlite3.connect(path)
    try:
        schema = connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        tables: dict[str, list[tuple[object, ...]]] = {}
        for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
            rows = connection.execute(f'SELECT * FROM "{name}"').fetchall()
            tables[name] = sorted(rows, key=lambda row: [(value is None, type(value).__name__, 0 if value is None else value) for value in row])
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()
    return {"user_version": user_version, "schema": schema, "tables": tables}


def without_blocks(snapshot: dict[str, Any]) -> dict[str, Any]:
    tables = {name: rows for name, rows in snapshot["tables"].items() if name != "delivery_blocks"}
    return {**snapshot, "tables": tables}


class FreeSpace:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self, _path: object) -> int:
        return self.value


class BusyAt:
    """The real store, except that one operation fails as a busy store would."""

    def __init__(self, store: Any, operation: str) -> None:
        self._store, self._operation = store, operation

    def __getattr__(self, name: str) -> Any:
        if name == self._operation:
            def busy(*_args: Any, **_kwargs: Any) -> Any:
                raise StoreRetryable("store-busy")
            return busy
        return getattr(self._store, name)


class Untouchable:
    """A store that fails any use, for caller errors that must precede every store call."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"store used: {name}")


class Case(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "portal.sqlite3"
        self.free = FreeSpace(PLENTY)
        self.store = self.open(self.path)
        self.store.honor_generation(1, encode_message(advertisement(1)), T0)

    def open(self, path: Path):
        store = open_store(path, reserve_bytes=RESERVE, free_bytes=self.free, busy_timeout_ms=100)
        self.addCleanup(store.close)
        return store

    def enroll(self, installation: str, minute: float = 0, store=None) -> None:
        store = store or self.store
        code = message_sha256(b"synthetic-enrollment-code-" + installation.encode())
        store.record_enrollment_offer(code, at(minute + 30), at(minute))
        store.enroll_installation(code, installation, ingest_protocol.installation_fingerprint(installation), at(minute))

    def activate(self, installation: str = A, minute: float = 0, store=None) -> None:
        store = store or self.store
        self.enroll(installation, minute, store)
        store.activate_installation(installation, at(minute))

    def accept(self, data: bytes, minute: float = 1) -> DeliveryResponse:
        return accept_ingest_batch(self.store, data, at(minute))

    def assertReceipt(self, response: DeliveryResponse, data: bytes, gap_after: int | None = None) -> None:
        message = ingest_protocol.decode_message(data)
        expected = encode_message({
            "wire_version": 1, "kind": "delivery-receipt", "sender_installation": message["sender_installation"],
            "sequence": message["sequence"], "message_sha256": message_sha256(data),
        })
        self.assertEqual(response, DeliveryResponse("receipt", expected, None, gap_after))

    def assertBlocked(self, response: DeliveryResponse, data: bytes, failure: str, identity: bool = True) -> None:
        value: dict[str, Any] = {"wire_version": 1, "kind": "delivery-block", "failure": failure, "message_sha256": message_sha256(data)}
        if identity:
            message = ingest_protocol.decode_message(data)
            value.update(sender_installation=message["sender_installation"], sequence=message["sequence"])
        self.assertEqual(response, DeliveryResponse("block", encode_message(value), failure, None))

    def assertRetryable(self, response: DeliveryResponse, reason: str, failure: str) -> None:
        body = encode_message({"wire_version": 1, "kind": "retryable-failure", "reason": reason})
        self.assertEqual(response, DeliveryResponse("retryable", body, failure, None))

    def blocks(self):
        with self.store.read() as view:
            return view.delivery_blocks()


# R1 — Governed Scope contract


class R1GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual((CONTRACT["scope"], CONTRACT["version"], CONTRACT["kind"]), ("accept-ingest-batch", "1", "leaf"))
        self.assertTrue(CONTRACT["purpose"])
        self.assertEqual(
            {item["name"]: item["mode"] for item in CONTRACT["inputs"]},
            {"delivered_bytes": "pure", "receipt_time": "pure", "operational_state": "effectful", "free_space": "volatile"},
        )
        self.assertEqual({output["name"] for output in CONTRACT["outputs"]},
                         {"delivery_response", "archived_ingest_batch", "delivery_block_record"})
        names = [failure["name"] for failure in CONTRACT["named_failures"]]
        self.assertEqual(len(names), 14)
        self.assertEqual(len(names), len(set(names)))
        by_outcome: dict[str, set[str]] = {}
        for failure in CONTRACT["named_failures"]:
            by_outcome.setdefault(failure["outcome"], set()).add(failure["name"])
        self.assertEqual(set(by_outcome), {"delivery-block", "retryable"})
        self.assertLessEqual(by_outcome["delivery-block"], DELIVERY_BLOCK_FAILURES)
        self.assertEqual(len(by_outcome["delivery-block"]), 9)
        self.assertEqual(by_outcome["retryable"], {
            "installation-pending", "installation-ended", "installation-unknown", "storage-reserve-exhausted", "store-busy",
        })
        self.assertEqual([effect["name"] for effect in CONTRACT["observable_effects"]], ["operational-store-write"])
        self.assertEqual(CONTRACT["dependencies"], [
            {"scope": "ingest-protocol", "version": "1"},
            {"scope": "manage-sender-installation", "version": "1"},
            {"scope": "operational-store", "version": "2"},
        ])
        self.assertTrue(CONTRACT["authority"]["owns"])
        for owner in ("ingest-protocol", "operational-store", "manage-sender-installation", "ingress shell"):
            self.assertTrue(any(owner in entry for entry in CONTRACT["authority"]["does_not_own"]), owner)
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["python"])
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `accept-ingest-batch` |")]
        self.assertEqual(len(rows), 1)
        _, kind, purpose, inputs, outputs, depends_on = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(sorted(depends_on.split(", ")), sorted(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]))
        self.assertIn("idempotently archive, and acknowledge an Ingest Batch", purpose)
        for phrase in ("Envelope", "active Sender Installation", "capacity state", "supported versions"):
            self.assertIn(phrase, inputs)
        for phrase in ("Archived Raw Records plus Delivery Receipt, retryable outcome, or Delivery Block",
                       "accepted sequence gaps become visible anomalies"):
            self.assertIn(phrase, outputs)


# R2 — Caller errors


class R2CallerErrors(Case):
    def test_naive_time_is_a_caller_error(self) -> None:
        for now in (datetime(2026, 10, 15, 9, 0), "2026-10-15T09:00:00Z", None):
            with self.assertRaises(ValueError):
                accept_ingest_batch(Untouchable(), batch(1), now)  # type: ignore[arg-type]

    def test_other_outbox_kinds_are_caller_errors(self) -> None:
        others = [
            control("capability-acknowledgement", 1),
            completion_message(1, cycle=CYCLE, record_type="steps", kind="initial-snapshot", page_count=0,
                               fragment_count=0, record_count=0, final_chain=chain_start(CYCLE)),
            completion_message(1, cycle=CYCLE, record_type="steps", kind="change-cycle", page_count=0,
                               fragment_count=0, record_count=0, final_chain=chain_start(CYCLE)),
            control("sync-checkpoint", 1, run_sequence=1, trigger="background", started_at="2026-10-15T08:59:00.000Z",
                    ended_at="2026-10-15T08:59:30.000Z", background_scheduling="available", record_types=[],
                    outbox_messages_before_checkpoint=0, outbox_bytes_before_checkpoint=0),
        ]
        for data in others:
            with self.assertRaises(ValueError):
                accept_ingest_batch(Untouchable(), data, T0)  # type: ignore[arg-type]


# R3 — Undecodable messages


class R3UndecodableMessages(Case):
    def assertProtocolBlock(self, data: bytes, failure: str) -> None:
        self.activate()
        before = contents(self.path)
        response = self.accept(data)
        reference = evaluate_delivery(new_delivery_state([advertisement(1)]), data)
        self.assertEqual((reference.outcome, reference.failure), ("block", failure))
        self.assertEqual(response, DeliveryResponse("block", reference.response, failure, None))
        block = ingest_protocol.decode_message(reference.response)
        self.assertEqual(
            [(record.installation_id, record.sequence, record.message_sha256, record.failure, record.count) for record in self.blocks()],
            [(block.get("sender_installation"), block.get("sequence"), message_sha256(data), failure, 1)],
        )
        self.assertEqual(without_blocks(contents(self.path)), without_blocks(before))

    def test_malformed_bytes_are_blocked(self) -> None:
        self.assertProtocolBlock(b"synthetic bytes that are not json", "malformed-message")

    def test_noncanonical_batch_is_blocked_with_its_identity(self) -> None:
        canonical = batch(7)
        noncanonical = json.dumps(json.loads(canonical), indent=1).encode()
        self.assertProtocolBlock(noncanonical, "noncanonical-message")
        self.assertEqual((self.blocks()[0].installation_id, self.blocks()[0].sequence), (A, 7))

    def test_non_outbox_message_is_blocked(self) -> None:
        self.assertProtocolBlock(encode_message(advertisement(1)), "malformed-message")


# R4 — Identity and retry


class R4IdentityAndRetry(Case):
    def test_identical_retry_returns_the_archived_receipt(self) -> None:
        self.activate()
        data = batch(1)
        first = self.accept(data, minute=1)
        self.assertReceipt(first, data)
        before = contents(self.path)
        self.assertEqual(self.accept(data, minute=9), first)
        self.assertEqual(contents(self.path), before)

    def test_retry_is_answered_after_retirement_and_generation_stop(self) -> None:
        self.activate()
        data = batch(1)
        first = self.accept(data)
        # Generation 1 stops once generation 2 is acknowledged.
        self.store.honor_generation(2, encode_message(advertisement(2)), at(2))
        acknowledgement = control("capability-acknowledgement", 2, generation=2)
        self.store.archive_capability_acknowledgement(MessageHeader(
            A, 2, message_sha256(acknowledgement), 2, at(2), acknowledgement, b'{"synthetic-receipt":2}'))
        self.store.stop_honoring_generation(1, at(3))
        self.store.begin_draining(A, at(4))
        self.store.retire_installation(A, "destructive", at(5))
        before = contents(self.path)
        self.assertEqual(self.accept(data, minute=6), first)
        self.assertEqual(contents(self.path), before)

    def test_same_sequence_different_bytes_is_blocked(self) -> None:
        self.activate()
        self.assertReceipt(self.accept(batch(1)), batch(1))
        other = batch(1, items=[projection(b"synthetic-other-projection")])
        before = contents(self.path)
        self.assertBlocked(self.accept(other), other, "sequence-content-mismatch")
        self.assertEqual(without_blocks(contents(self.path)), without_blocks(before))
        self.assertEqual([record.failure for record in self.blocks()], ["sequence-content-mismatch"])


# R5 — Installation state


class R5InstallationState(Case):
    def test_pending_installation_is_retryable_until_confirmed(self) -> None:
        self.enroll(A)
        data = batch(1)
        before = contents(self.path)
        self.assertRetryable(self.accept(data), "temporarily-unavailable", "installation-pending")
        self.assertEqual(contents(self.path), before)
        self.store.activate_installation(A, at(2))
        self.assertReceipt(self.accept(data, minute=3), data)

    def test_ended_installations_are_retryable(self) -> None:
        self.enroll(A)
        self.store.withdraw_installation(A, at(1))
        self.activate(B, minute=2)
        self.store.begin_draining(B, at(3))
        self.store.retire_installation(B, "drained", at(4))
        before = contents(self.path)
        for installation in (A, B):
            self.assertRetryable(self.accept(batch(1, installation=installation), minute=5),
                                 "temporarily-unavailable", "installation-ended")
        self.assertEqual(contents(self.path), before)

    def test_unknown_installation_is_retryable(self) -> None:
        self.activate()
        before = contents(self.path)
        self.assertRetryable(self.accept(batch(1, installation=UNKNOWN)), "temporarily-unavailable", "installation-unknown")
        self.assertEqual(contents(self.path), before)

    def test_draining_installation_still_delivers(self) -> None:
        self.activate()
        self.store.begin_draining(A, at(1))
        data = batch(1)
        self.assertReceipt(self.accept(data, minute=2), data)


# R6 — Sequence order and gaps


class R6SequenceOrderAndGaps(Case):
    def test_older_unseen_sequence_is_blocked(self) -> None:
        self.activate()
        first = batch(1)
        self.accept(first)
        later = batch(4, fragment=2, previous=after(first))
        self.accept(later)
        older = batch(2, fragment=2, previous=after(first))
        self.assertBlocked(self.accept(older), older, "sequence-out-of-order")

    def test_gap_is_accepted_and_reported(self) -> None:
        self.activate()
        opening = batch(3)
        self.assertReceipt(self.accept(opening), opening, gap_after=0)
        later = batch(6, fragment=2, previous=after(opening))
        self.assertReceipt(self.accept(later), later, gap_after=3)
        with self.store.read() as view:
            self.assertEqual([(gap.gap_after, gap.sequence) for gap in view.sequence_gaps(A)], [(0, 3), (3, 6)])
        next_one = batch(7, fragment=3, previous=after(later))
        self.assertReceipt(self.accept(next_one), next_one, gap_after=None)


# R7 — Generation, versions, and limits


def sized(sequence: int, total: int, *, previous: str | None = None, fragment: int = 1) -> bytes:
    """A batch of exactly `total` bytes: one projection sized by binary search, then one retraction tuning the last bytes."""
    def build(size: int, length: int) -> bytes:
        return batch(sequence, previous=previous, fragment=fragment,
                     items=[projection(b"s" * size), retraction("r" * length)])
    size, step = 0, 4096
    while step:
        if len(build(size + step, 1)) <= total:
            size += step
        else:
            step //= 2
    for length in range(1, 257):
        data = build(size, length)
        if len(data) == total:
            return data
    raise AssertionError(f"no batch of {total} bytes")


class R7GenerationVersionsAndLimits(Case):
    def test_unhonored_generation_is_unsupported(self) -> None:
        self.activate()
        unknown = batch(1, generation=2)
        self.assertBlocked(self.accept(unknown), unknown, "unsupported-wire-protocol")
        # A generation that stopped being honored is unsupported for new messages.
        self.store.honor_generation(2, encode_message(advertisement(2)), at(2))
        acknowledgement = control("capability-acknowledgement", 1, generation=2)
        self.store.archive_capability_acknowledgement(MessageHeader(
            A, 1, message_sha256(acknowledgement), 2, at(2), acknowledgement, b'{"synthetic-receipt":1}'))
        self.store.stop_honoring_generation(1, at(3))
        stopped = batch(2, generation=1)
        self.assertBlocked(self.accept(stopped, minute=4), stopped, "unsupported-wire-protocol")
        current = batch(3, generation=2)
        self.assertReceipt(self.accept(current, minute=5), current, gap_after=1)

    def test_unsupported_schema_version_is_blocked(self) -> None:
        self.activate()
        for sequence, record_type, schema in ((1, "steps", 3), (2, "heart-rate", 2), (3, "weight", 1)):
            data = batch(sequence, record_type=record_type, schema=schema, cycle=f"00000000-0000-4000-8000-00000000d00{sequence}")
            self.assertBlocked(self.accept(data), data, "unsupported-source-projection-schema")
        accepted = batch(1, schema=2)
        self.assertReceipt(self.accept(accepted), accepted)

    def test_limits_are_inclusive(self) -> None:
        self.activate()
        four = batch(1, items=[projection(b"synthetic-%d" % index) for index in range(MAX_RECORDS)])
        self.assertReceipt(self.accept(four), four)
        exact = sized(2, MAX_BYTES, previous=after(four), fragment=2)
        self.assertEqual(len(exact), MAX_BYTES)
        self.assertReceipt(self.accept(exact), exact)

    def test_over_limit_batches_are_blocked(self) -> None:
        self.activate()
        five = batch(1, items=[projection(b"synthetic-%d" % index) for index in range(MAX_RECORDS + 1)])
        self.assertBlocked(self.accept(five), five, "record-count-limit-exceeded")
        over = sized(1, MAX_BYTES + 1)
        self.assertBlocked(self.accept(over), over, "uncompressed-byte-limit-exceeded")
        # The record count is checked before the size.
        both = batch(1, items=[projection(bytes(3000))] * (MAX_RECORDS + 1))
        self.assertGreater(len(both), MAX_BYTES)
        self.assertBlocked(self.accept(both), both, "record-count-limit-exceeded")


# R8 — Fragment continuity


class R8FragmentContinuity(Case):
    def test_a_cycle_starts_at_its_first_fragment(self) -> None:
        self.activate()
        for data in (batch(1, page=2), batch(1, fragment=2), batch(1, previous=chain_start(CYCLE_2))):
            self.assertBlocked(self.accept(data), data, "fragment-order-mismatch")
        start = batch(1)
        self.assertReceipt(self.accept(start), start)

    def test_fragments_continue_by_page_and_chain(self) -> None:
        self.activate()
        first = batch(1)
        same_page = batch(2, fragment=2, previous=after(first))
        next_page = batch(3, page=2, fragment=1, previous=after(same_page))
        for data in (first, same_page, next_page):
            self.assertReceipt(self.accept(data), data)
        # Another cycle may interleave without affecting this one.
        other = batch(4, cycle=CYCLE_2, record_type="heart-rate", kind="change-cycle")
        self.assertReceipt(self.accept(other), other)
        resumed = batch(5, page=2, fragment=2, previous=after(next_page))
        self.assertReceipt(self.accept(resumed), resumed)

    def test_discontinuous_fragments_are_blocked(self) -> None:
        self.activate()
        first = batch(1)
        self.accept(first)
        chain = after(first)
        cases = [
            batch(2, fragment=3, previous=chain),
            batch(2, page=3, fragment=1, previous=chain),
            batch(2, page=2, fragment=2, previous=chain),
            batch(2, fragment=2, previous=chain_start(CYCLE)),
            batch(2, fragment=2, previous=chain, record_type="heart-rate"),
            batch(2, fragment=2, previous=chain, kind="reconciliation-snapshot"),
        ]
        for data in cases:
            self.assertBlocked(self.accept(data), data, "fragment-order-mismatch")
        valid = batch(2, fragment=2, previous=chain)
        self.assertReceipt(self.accept(valid), valid)

    def test_completed_cycle_accepts_no_fragment(self) -> None:
        self.activate()
        first = batch(1)
        self.accept(first)
        archive_completion(self.store, completion_message(
            2, cycle=CYCLE, record_type="steps", kind="initial-snapshot", page_count=1, fragment_count=1,
            record_count=1, final_chain=after(first)), at(2))
        late = batch(3, fragment=2, previous=after(first))
        self.assertBlocked(self.accept(late, minute=3), late, "fragment-order-mismatch")
        # A cycle completed without fragments accepts none either.
        archive_completion(self.store, completion_message(
            4, cycle=CYCLE_2, record_type="steps", kind="change-cycle", page_count=0, fragment_count=0,
            record_count=0, final_chain=chain_start(CYCLE_2)), at(4))
        empty = batch(5, cycle=CYCLE_2, kind="change-cycle")
        self.assertBlocked(self.accept(empty, minute=5), empty, "fragment-order-mismatch")

    def test_cycles_of_other_installations_never_matter(self) -> None:
        self.activate()
        first = batch(1)
        self.accept(first)
        self.store.begin_draining(A, at(2))
        self.store.retire_installation(A, "drained", at(3))
        self.activate(B, minute=4)
        fresh = batch(1, installation=B)
        self.assertReceipt(self.accept(fresh, minute=5), fresh)
        continuing = batch(2, installation=B, fragment=2, previous=after(first))
        self.assertBlocked(self.accept(continuing, minute=6), continuing, "fragment-order-mismatch")


# R9 — Exact archival before receipt


class R9ExactArchival(Case):
    def test_accepted_batch_is_archived_exactly(self) -> None:
        self.activate()
        data = batch(1, schema=2, kind="change-cycle", items=[
            projection(b"synthetic-projection-a"), retraction("synthetic-identity-b"), projection(b"\x00\xffsynthetic"),
        ])
        response = self.accept(data, minute=7)
        with self.store.read() as view:
            archived = view.archived_message(A, 1)
            items = view.archived_items(A, 1)
        assert archived is not None and archived.batch is not None
        self.assertEqual(
            (archived.kind, archived.envelope, archived.receipt, archived.receipt_time, archived.staged_generation,
             archived.message_sha256),
            ("ingest-batch", data, response.body, at(7), 1, message_sha256(data)),
        )
        self.assertEqual(
            (archived.batch.record_type, archived.batch.projection_schema_version, archived.batch.cycle_kind,
             archived.batch.cycle_id, archived.batch.source_page_index, archived.batch.fragment_index,
             archived.batch.previous_chain),
            ("steps", 2, "change-cycle", CYCLE, 1, 1, chain_start(CYCLE)),
        )
        self.assertEqual(items, (
            BatchItem(projection=b"synthetic-projection-a"),
            BatchItem(retracted_identity="synthetic-identity-b"),
            BatchItem(projection=b"\x00\xffsynthetic"),
        ))

    def test_receipt_echoes_the_message_identity(self) -> None:
        self.activate()
        data = batch(1)
        response = self.accept(data)
        self.assertEqual(ingest_protocol.decode_message(response.body), {
            "wire_version": 1, "kind": "delivery-receipt", "sender_installation": A, "sequence": 1,
            "message_sha256": message_sha256(data),
        })
        self.assertReceipt(response, data)

    def test_last_delivery_is_the_receipt_time(self) -> None:
        self.activate()
        first = batch(1)
        self.accept(first, minute=5)
        self.accept(first, minute=9)
        with self.store.read() as view:
            self.assertEqual(view.installation(A).last_delivery_at, at(5))
        second = batch(2, fragment=2, previous=after(first))
        self.accept(second, minute=11)
        with self.store.read() as view:
            self.assertEqual(view.installation(A).last_delivery_at, at(11))


# R10 — Capacity and contention


class R10CapacityAndContention(Case):
    def test_exhausted_reserve_is_retryable_and_writes_nothing(self) -> None:
        self.activate()
        first = batch(1)
        receipt = self.accept(first)
        self.free.value = RESERVE - 1
        before = contents(self.path)
        second = batch(2, fragment=2, previous=after(first))
        self.assertRetryable(self.accept(second), "storage-reserve-exhausted", "storage-reserve-exhausted")
        self.assertEqual(self.accept(first), receipt)
        self.assertEqual(contents(self.path), before)
        self.free.value = RESERVE
        self.assertReceipt(self.accept(second), second)

    def test_busy_store_is_retryable(self) -> None:
        self.activate()
        holder = sqlite3.connect(self.path, isolation_level=None, timeout=0)
        self.addCleanup(holder.close)
        before = contents(self.path)
        holder.execute("BEGIN IMMEDIATE")
        try:
            self.assertRetryable(self.accept(batch(1)), "temporarily-unavailable", "store-busy")
        finally:
            holder.execute("ROLLBACK")
        self.assertEqual(contents(self.path), before)
        # Busy while reading, or while recording a block: no answer without its effect.
        self.assertRetryable(accept_ingest_batch(BusyAt(self.store, "read"), batch(1), at(2)), "temporarily-unavailable", "store-busy")
        malformed = b"synthetic bytes that are not json"
        self.assertRetryable(accept_ingest_batch(BusyAt(self.store, "record_delivery_block"), malformed, at(2)),
                             "temporarily-unavailable", "store-busy")
        self.assertEqual(contents(self.path), before)
        self.assertReceipt(self.accept(batch(1)), batch(1))


# R11 — Conformance with the protocol's evaluation


class History:
    """Synthetic delivery histories over two record types, with deliberate defects."""

    def __init__(self, seed: int) -> None:
        self.random = random.Random(seed)
        self.sequence = 0
        self.skipped: list[int] = []
        self.sent: list[bytes] = []
        self.accepted: dict[int, bytes] = {}
        self.cycles: dict[str, dict[str, Any]] = {}
        self.completed: list[str] = []
        self.counter = 0

    def cycle_id(self) -> str:
        self.counter += 1
        return f"00000000-0000-4000-8000-{self.counter:012x}"

    def next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence

    def fragment(self, record_type: str, sequence: int, **overrides: Any) -> bytes:
        cycle = self.cycles.get(record_type)
        if cycle is None:
            kind = self.random.choice(["initial-snapshot", "reconciliation-snapshot", "change-cycle"])
            cycle = {"id": self.cycle_id(), "kind": kind, "page": 1, "fragment": 0, "chain": None, "pages": 0, "fragments": 0, "records": 0}
            cycle["chain"] = chain_start(cycle["id"])
            self.cycles[record_type] = cycle
        if cycle["fragment"] == 0 or self.random.random() < 0.6:
            page, fragment = cycle["page"], cycle["fragment"] + 1
        else:
            page, fragment = cycle["page"] + 1, 1
        count = self.random.randint(0, MAX_RECORDS)
        items = [projection(b"synthetic-%d-%d" % (sequence, index)) if self.random.random() < 0.7
                 else retraction(f"synthetic-identity-{sequence}-{index}") for index in range(count)]
        fields = {"cycle": cycle["id"], "page": page, "fragment": fragment, "previous": cycle["chain"],
                  "record_type": record_type, "kind": cycle["kind"], "items": items}
        fields.update(overrides)
        return batch(sequence, **fields)

    def accept_fragment(self, record_type: str, data: bytes) -> None:
        message = ingest_protocol.decode_message(data)
        cycle = self.cycles[record_type]
        cycle.update(page=message["source_page_index"], fragment=message["fragment_index"],
                     chain=chain_next(message["previous_chain"], message_sha256(data)),
                     pages=message["source_page_index"], fragments=cycle["fragments"] + 1,
                     records=cycle["records"] + len(message["items"]))


class R11Conformance(Case):
    def test_answers_match_the_protocol_evaluation(self) -> None:
        for seed in range(8):
            with self.subTest(seed=seed):
                self.run_history(seed)

    def run_history(self, seed: int) -> None:
        directory = self.directory / f"seed-{seed}"
        directory.mkdir()
        store = self.open(directory / "portal.sqlite3")
        store.honor_generation(1, encode_message(advertisement(1)), T0)
        self.activate(A, store=store)
        state = new_delivery_state([advertisement(1)])
        history = History(seed)
        actions = ["next"] * 8 + ["complete", "retry", "gap", "bad-chain", "bad-order", "after-completion",
                                  "bad-schema", "too-many", "too-big", "old", "conflict", "unknown-generation"]
        for step in range(120):
            action = history.random.choice(actions)
            record_type = history.random.choice(["steps", "heart-rate"])
            data: bytes | None = None
            if action == "complete" and record_type in history.cycles and history.cycles[record_type]["fragment"]:
                cycle = history.cycles.pop(record_type)
                data = completion_message(history.next_sequence(), cycle=cycle["id"], record_type=record_type,
                                          kind=cycle["kind"], page_count=cycle["pages"], fragment_count=cycle["fragments"],
                                          record_count=cycle["records"], final_chain=cycle["chain"])
                expected = evaluate_delivery(state, data)
                if expected.outcome != "receipt":
                    # A blocked message left a hole in the cycle's sequences; the cycle stays open.
                    history.cycles[record_type] = cycle
                    continue
                archive_completion(store, data, at(step))
                state = expected.state
                history.completed.append(cycle["id"])
                continue
            if action == "retry" and history.sent:
                data = history.random.choice(history.sent)
            elif action == "gap":
                history.skipped.append(history.next_sequence())
                continue
            elif action == "old" and history.skipped:
                data = history.fragment(record_type, history.random.choice(history.skipped))
            elif action == "conflict" and history.accepted:
                sequence = history.random.choice(sorted(history.accepted))
                data = batch(sequence, items=[projection(b"synthetic-conflict-%d" % step)])
            elif action == "after-completion" and history.completed:
                data = batch(history.next_sequence(), cycle=history.random.choice(history.completed), fragment=2)
            elif action in ("bad-chain", "bad-order", "bad-schema", "too-many", "too-big", "unknown-generation"):
                overrides = {
                    "bad-chain": {"previous": chain_start(CYCLE_2)},
                    "bad-order": {"fragment": 5, "page": 7},
                    "bad-schema": {"schema": 9},
                    "too-many": {"items": [projection(b"synthetic-%d" % index) for index in range(MAX_RECORDS + 1)]},
                    "too-big": {"items": [projection(bytes(MAX_BYTES))]},
                    "unknown-generation": {"generation": 2},
                }[action]
                data = history.fragment(record_type, history.next_sequence(), **overrides)
            if data is None:
                data = history.fragment(record_type, history.next_sequence())
            expected = evaluate_delivery(state, data)
            actual = accept_ingest_batch(store, data, at(step))
            self.assertEqual(
                (actual.outcome, actual.body, actual.failure, actual.gap_after),
                (expected.outcome, expected.response, expected.failure, expected.gap_after),
                (seed, step, action),
            )
            history.sent.append(data)
            if expected.outcome == "receipt":
                message = ingest_protocol.decode_message(data)
                if message["sequence"] not in history.accepted:
                    history.accepted[message["sequence"]] = data
                    if message["kind"] == "ingest-batch" and message["record_type"] in history.cycles \
                            and history.cycles[message["record_type"]]["id"] == message["cycle_id"]:
                        history.accept_fragment(message["record_type"], data)
                state = expected.state


# R12 — Health-data-free evidence


class R12HealthDataFree(Case):
    def test_blocks_and_responses_are_health_data_free(self) -> None:
        self.activate()
        marked = [projection(HEALTH_MARKER), retraction(IDENTITY_MARKER)]
        first = batch(1, items=marked)
        responses = [self.accept(first)]
        for data in (
            batch(1, items=marked + [retraction(IDENTITY_MARKER + "-other")]),
            batch(2, fragment=5, items=marked),
            batch(3, schema=9, items=marked),
            batch(4, generation=7, items=marked),
            batch(5, items=marked * 3),
            json.dumps(json.loads(batch(6, items=marked)), indent=2).encode(),
            batch(7, installation=UNKNOWN, items=marked),
        ):
            responses.append(self.accept(data))
        self.assertEqual([response.outcome for response in responses],
                         ["receipt", "block", "block", "block", "block", "block", "block", "retryable"])
        evidence = [response.body for response in responses] + [repr(record).encode() for record in self.blocks()]
        for value in evidence:
            self.assertNotIn(HEALTH_MARKER, value)
            self.assertNotIn(base64.b64encode(HEALTH_MARKER), value)
            self.assertNotIn(IDENTITY_MARKER.encode(), value)


if __name__ == "__main__":
    unittest.main()
