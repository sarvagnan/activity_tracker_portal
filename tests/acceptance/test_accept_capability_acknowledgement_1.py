"""Acceptance tests for `accept-capability-acknowledgement@1`.

Requirement numbers refer to `packs/accept-capability-acknowledgement-1.spec.md`.
Every database is a real `operational-store@2` database in a temporary
directory, every message is built with `ingest-protocol@1`, and every value is
synthetic. Assertions that something wrote nothing compare the whole database,
and every Delivery Block is checked with its durable record.
"""

from __future__ import annotations

import base64
import collections
import json
import random
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from activity_tracker_portal import ingest_protocol, sender_installation
from activity_tracker_portal.accept_capability_acknowledgement import accept_capability_acknowledgement
from activity_tracker_portal.accept_ingest_batch import DeliveryResponse, accept_ingest_batch
from activity_tracker_portal.ingest_protocol import (
    DELIVERY_BLOCK_FAILURES,
    chain_start,
    encode_message,
    evaluate_delivery,
    message_sha256,
    new_delivery_state,
)
from activity_tracker_portal.operational_store import StoreRetryable, open_store

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/accept-capability-acknowledgement.1.json").read_text(encoding="utf-8"))

UTC = timezone.utc
T0 = datetime(2026, 10, 15, 9, 0, 0, 123456, tzinfo=UTC)
RESERVE = 1_000_000
PLENTY = 10**12
A = "00000000-0000-4000-8000-0000000000a1"
B = "00000000-0000-4000-8000-0000000000b2"
UNKNOWN = "00000000-0000-4000-8000-0000000000c3"


def advertisement(generation: int) -> dict[str, Any]:
    return {
        "wire_version": 1,
        "kind": "capability-advertisement",
        "generation": generation,
        "wire_versions": [1],
        "projection_schemas": [{"record_type": "steps", "versions": [1]}],
        "max_record_count": 4,
        "max_message_bytes": 8192,
    }


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def acknowledgement(sequence: int, generation: int = 1, installation: str = A) -> bytes:
    return encode_message({
        "wire_version": 1, "kind": "capability-acknowledgement", "sender_installation": installation,
        "sequence": sequence, "capability_generation": generation,
    })


def batch(sequence: int, cycle: str, generation: int = 1, installation: str = A) -> bytes:
    return encode_message({
        "wire_version": 1, "kind": "ingest-batch", "sender_installation": installation, "sequence": sequence,
        "capability_generation": generation, "record_type": "steps", "projection_schema_version": 1,
        "cycle_kind": "change-cycle", "cycle_id": cycle, "source_page_index": 1, "fragment_index": 1,
        "previous_chain": chain_start(cycle),
        "items": [{"kind": "projection", "data": base64.b64encode(b"synthetic-%d" % sequence).decode("ascii")}],
    })


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
    return {**snapshot, "tables": {name: rows for name, rows in snapshot["tables"].items() if name != "delivery_blocks"}}


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
        for generation in (1, 2):
            self.store.honor_generation(generation, encode_message(advertisement(generation)), T0)

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
        return accept_capability_acknowledgement(self.store, data, at(minute))

    def assertReceipt(self, response: DeliveryResponse, data: bytes, gap_after: int | None = None) -> None:
        message = ingest_protocol.decode_message(data)
        expected = encode_message({
            "wire_version": 1, "kind": "delivery-receipt", "sender_installation": message["sender_installation"],
            "sequence": message["sequence"], "message_sha256": message_sha256(data),
        })
        self.assertEqual(response, DeliveryResponse("receipt", expected, None, gap_after))

    def assertRetryable(self, response: DeliveryResponse, reason: str, failure: str) -> None:
        body = encode_message({"wire_version": 1, "kind": "retryable-failure", "reason": reason})
        self.assertEqual(response, DeliveryResponse("retryable", body, failure, None))

    def block_index(self) -> dict[tuple[object, ...], Any]:
        with self.store.read() as view:
            return {(r.installation_id, r.sequence, r.message_sha256, r.failure): r for r in view.delivery_blocks()}

    def deliverBlocked(self, data: bytes, failure: str, minute: float = 1) -> None:
        """Deliver an acknowledgement that must be blocked; require the durable record and no other write."""
        message = ingest_protocol.decode_message(data)
        before, records_before = contents(self.path), self.block_index()
        body = encode_message({
            "wire_version": 1, "kind": "delivery-block", "failure": failure, "message_sha256": message_sha256(data),
            "sender_installation": message["sender_installation"], "sequence": message["sequence"],
        })
        self.assertEqual(self.accept(data, minute), DeliveryResponse("block", body, failure, None))
        self.assertEqual(without_blocks(contents(self.path)), without_blocks(before))
        key = (message["sender_installation"], message["sequence"], message_sha256(data), failure)
        records = self.block_index()
        prior = records_before.get(key)
        self.assertEqual(
            (records[key].count, records[key].first_seen_at, records[key].last_seen_at),
            ((prior.count if prior else 0) + 1, prior.first_seen_at if prior else at(minute), at(minute)),
        )
        self.assertEqual({k: v for k, v in records.items() if k != key}, {k: v for k, v in records_before.items() if k != key})


# R1 — Governed Scope contract


class R1GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual((CONTRACT["scope"], CONTRACT["version"], CONTRACT["kind"]),
                         ("accept-capability-acknowledgement", "1", "leaf"))
        self.assertTrue(CONTRACT["purpose"])
        self.assertEqual(
            {item["name"]: item["mode"] for item in CONTRACT["inputs"]},
            {"delivered_bytes": "pure", "receipt_time": "pure", "operational_state": "effectful", "free_space": "volatile"},
        )
        self.assertEqual({output["name"] for output in CONTRACT["outputs"]},
                         {"delivery_response", "archived_acknowledgement", "delivery_block_record"})
        by_outcome: dict[str, set[str]] = {}
        for failure in CONTRACT["named_failures"]:
            by_outcome.setdefault(failure["outcome"], set()).add(failure["name"])
        self.assertEqual(len(CONTRACT["named_failures"]), 8)
        self.assertEqual(by_outcome["delivery-block"], {"sequence-content-mismatch", "sequence-out-of-order", "unsupported-wire-protocol"})
        self.assertLessEqual(by_outcome["delivery-block"], DELIVERY_BLOCK_FAILURES)
        self.assertEqual(by_outcome["retryable"], {
            "installation-pending", "installation-ended", "installation-unknown", "storage-reserve-exhausted", "store-busy",
        })
        self.assertEqual([effect["name"] for effect in CONTRACT["observable_effects"]], ["operational-store-write"])
        self.assertEqual(CONTRACT["dependencies"], [
            {"scope": "ingest-protocol", "version": "1"},
            {"scope": "accept-ingest-batch", "version": "1"},
            {"scope": "manage-sender-installation", "version": "1"},
            {"scope": "operational-store", "version": "2"},
        ])
        for owner in ("ingest-protocol", "operational-store", "manage-sender-installation", "accept-ingest-batch", "ingress shell"):
            self.assertTrue(any(owner in entry for entry in CONTRACT["authority"]["does_not_own"]), owner)
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["python"])
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `accept-capability-acknowledgement` |")]
        self.assertEqual(len(rows), 1)
        _, kind, purpose, inputs, outputs, depends_on = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(sorted(depends_on.split(", ")), sorted(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]))
        self.assertIn("capability acknowledgement", purpose)
        self.assertIn("Capability acknowledgement envelope", inputs)
        self.assertIn("latest acknowledged generation", outputs)


# R2 — Caller errors


class R2CallerErrors(Case):
    def test_invalid_time_is_a_caller_error(self) -> None:
        for now in (datetime(2026, 10, 15, 9, 0), "2026-10-15T09:00:00Z", None):
            with self.assertRaises(ValueError):
                accept_capability_acknowledgement(Untouchable(), acknowledgement(1), now)  # type: ignore[arg-type]

    def test_other_bytes_are_caller_errors(self) -> None:
        others = [
            b"synthetic bytes that are not json",
            json.dumps(json.loads(acknowledgement(1)), indent=1).encode(),
            batch(1, "00000000-0000-4000-8000-000000000c01"),
            encode_message(advertisement(1)),
        ]
        for data in others:
            with self.assertRaises(ValueError):
                accept_capability_acknowledgement(Untouchable(), data, T0)  # type: ignore[arg-type]


# R3 — Identity and retry


class R3IdentityAndRetry(Case):
    def test_identical_retry_returns_the_archived_receipt(self) -> None:
        self.activate()
        data = acknowledgement(1, generation=2)
        first = self.accept(data, minute=1)
        self.assertReceipt(first, data)
        before = contents(self.path)
        self.assertEqual(self.accept(data, minute=7), first)
        # Still answered after the generation stops, the installation retires, and the reserve runs out.
        self.store.stop_honoring_generation(1, at(8))
        self.store.begin_draining(A, at(9))
        self.store.retire_installation(A, "destructive", at(10))
        self.free.value = RESERVE - 1
        before = contents(self.path)
        self.assertEqual(self.accept(data, minute=11), first)
        self.assertEqual(contents(self.path), before)

    def test_same_sequence_different_bytes_is_blocked(self) -> None:
        self.activate()
        self.accept(acknowledgement(1, generation=1))
        for minute in (2, 5, 9):
            self.deliverBlocked(acknowledgement(1, generation=2), "sequence-content-mismatch", minute=minute)
        with self.store.read() as view:
            self.assertEqual([(r.first_seen_at, r.last_seen_at, r.count) for r in view.delivery_blocks()], [(at(2), at(9), 3)])


# R4 — Installation state


class R4InstallationState(Case):
    def test_non_delivering_installations_are_retryable(self) -> None:
        self.enroll(A)
        before = contents(self.path)
        self.assertRetryable(self.accept(acknowledgement(1)), "temporarily-unavailable", "installation-pending")
        self.assertRetryable(self.accept(acknowledgement(1, installation=UNKNOWN)), "temporarily-unavailable", "installation-unknown")
        self.assertEqual(contents(self.path), before)
        self.store.withdraw_installation(A, at(2))
        self.activate(B, minute=3)
        self.store.begin_draining(B, at(4))
        self.store.retire_installation(B, "drained", at(5))
        before = contents(self.path)
        for installation in (A, B):
            self.assertRetryable(self.accept(acknowledgement(1, installation=installation), minute=6),
                                 "temporarily-unavailable", "installation-ended")
        self.assertEqual(contents(self.path), before)

    def test_draining_installation_still_delivers(self) -> None:
        self.activate()
        self.store.begin_draining(A, at(1))
        data = acknowledgement(1, generation=2)
        self.assertReceipt(self.accept(data, minute=2), data)


# R5 — Sequence order and gaps


class R5SequenceOrderAndGaps(Case):
    def test_older_unseen_sequence_is_blocked(self) -> None:
        self.activate()
        self.accept(acknowledgement(1))
        self.accept(acknowledgement(4, generation=2))
        self.deliverBlocked(acknowledgement(2, generation=2), "sequence-out-of-order", minute=2)

    def test_gap_is_accepted_and_reported(self) -> None:
        self.activate()
        first = acknowledgement(2)
        self.assertReceipt(self.accept(first), first, gap_after=0)
        later = acknowledgement(5, generation=2)
        self.assertReceipt(self.accept(later), later, gap_after=2)
        with self.store.read() as view:
            self.assertEqual([(gap.gap_after, gap.sequence) for gap in view.sequence_gaps(A)], [(0, 2), (2, 5)])


# R6 — Generation and version


class R6GenerationAndVersion(Case):
    def test_unhonored_generation_is_unsupported(self) -> None:
        self.activate()
        self.deliverBlocked(acknowledgement(1, generation=3), "unsupported-wire-protocol")
        # A generation that stopped being honored is unsupported for a new acknowledgement.
        self.assertReceipt(self.accept(acknowledgement(1, generation=2), minute=2), acknowledgement(1, generation=2))
        self.store.stop_honoring_generation(1, at(3))
        self.deliverBlocked(acknowledgement(2, generation=1), "unsupported-wire-protocol", minute=4)


# R7 — Archival and the acknowledged generation


class R7ArchivalAndGeneration(Case):
    def test_accepted_acknowledgement_is_archived_exactly(self) -> None:
        self.activate()
        data = acknowledgement(1, generation=2)
        response = self.accept(data, minute=7)
        with self.store.read() as view:
            archived = view.archived_message(A, 1)
            self.assertEqual(view.installation(A).last_delivery_at, at(7))
            self.assertEqual(view.latest_acknowledged_generation(), 2)
        assert archived is not None
        self.assertEqual(
            (archived.kind, archived.envelope, archived.receipt, archived.receipt_time, archived.staged_generation,
             archived.message_sha256, archived.batch, archived.completion, archived.checkpoint),
            ("capability-acknowledgement", data, response.body, at(7), 2, message_sha256(data), None, None, None),
        )
        self.assertReceipt(response, data)

    def test_acknowledgement_lets_superseded_generations_stop(self) -> None:
        self.activate()
        self.assertIsNone(sender_installation.stop_superseded_generation(self.store, at(1)))
        self.accept(acknowledgement(1, generation=2), minute=2)
        self.assertEqual(sender_installation.stop_superseded_generation(self.store, at(3)), 1)
        with self.store.read() as view:
            self.assertEqual([item.generation for item in view.honored_generations()], [2])

    def test_acceptance_stops_no_generation(self) -> None:
        self.activate()
        self.accept(acknowledgement(1, generation=2), minute=2)
        with self.store.read() as view:
            self.assertEqual([item.generation for item in view.honored_generations()], [1, 2])


# R8 — Capacity and contention


class R8CapacityAndContention(Case):
    def test_exhausted_reserve_is_retryable_and_blocks_still_win(self) -> None:
        self.activate()
        self.accept(acknowledgement(1))
        self.accept(acknowledgement(3))
        self.free.value = RESERVE - 1
        self.deliverBlocked(acknowledgement(1, generation=2), "sequence-content-mismatch")
        self.deliverBlocked(acknowledgement(2), "sequence-out-of-order")
        self.deliverBlocked(acknowledgement(4, generation=3), "unsupported-wire-protocol")
        before = contents(self.path)
        self.assertRetryable(self.accept(acknowledgement(4)), "storage-reserve-exhausted", "storage-reserve-exhausted")
        self.assertEqual(contents(self.path), before)
        self.free.value = RESERVE
        self.assertReceipt(self.accept(acknowledgement(4)), acknowledgement(4))

    def test_busy_store_is_retryable(self) -> None:
        self.activate()
        before = contents(self.path)
        holder = sqlite3.connect(self.path, isolation_level=None, timeout=0)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        try:
            self.assertRetryable(self.accept(acknowledgement(1)), "temporarily-unavailable", "store-busy")
        finally:
            holder.execute("ROLLBACK")
        for operation, data in (("read", acknowledgement(1)), ("record_delivery_block", acknowledgement(1, generation=3))):
            self.assertRetryable(accept_capability_acknowledgement(BusyAt(self.store, operation), data, at(2)),
                                 "temporarily-unavailable", "store-busy")
        self.assertEqual(contents(self.path), before)
        self.assertReceipt(self.accept(acknowledgement(1)), acknowledgement(1))


# R9 — Conformance with the protocol's evaluation


class R9Conformance(Case):
    def test_answers_match_the_protocol_evaluation(self) -> None:
        self.events: collections.Counter[str] = collections.Counter()
        for seed in range(8):
            with self.subTest(seed=seed):
                self.run_history(seed)
        for event in ("receipt", "gap", "retry", "batch", "repeated-block",
                      "sequence-content-mismatch", "sequence-out-of-order", "unsupported-wire-protocol"):
            self.assertGreater(self.events[event], 0, event)

    def records(self, store) -> dict[tuple[object, ...], Any]:
        with store.read() as view:
            return {(r.installation_id, r.sequence, r.message_sha256, r.failure): r for r in view.delivery_blocks()}

    def run_history(self, seed: int) -> None:
        rng = random.Random(seed)
        directory = self.directory / f"seed-{seed}"
        directory.mkdir()
        store = self.open(directory / "portal.sqlite3")
        for generation in (1, 2, 3):
            store.honor_generation(generation, encode_message(advertisement(generation)), T0)
        self.activate(A, store=store)
        state = new_delivery_state([advertisement(1), advertisement(2), advertisement(3)])
        sequence, sent, accepted, skipped, cycles = 0, [], {}, [], 0
        for step in range(150):
            action = rng.choice(["ack"] * 6 + ["batch", "retry", "conflict", "old", "gap", "unknown-generation"])
            if action == "gap":
                sequence += 1
                skipped.append(sequence)
                continue
            if action == "batch":
                sequence += 1
                cycles += 1
                data = batch(sequence, f"00000000-0000-4000-8000-{cycles:012x}", generation=rng.choice([1, 2, 3]))
                expected = evaluate_delivery(state, data)
                self.assertEqual(accept_ingest_batch(store, data, at(step)).body, expected.response)
                if expected.outcome == "receipt":
                    accepted[sequence] = data
                    state = expected.state
                    self.events["batch"] += 1
                continue
            if action == "retry" and sent:
                data = rng.choice(sent)
            elif action == "conflict" and accepted:
                data = acknowledgement(rng.choice(sorted(accepted)), generation=rng.choice([1, 2, 3]))
            elif action == "old" and skipped:
                data = acknowledgement(rng.choice(skipped), generation=rng.choice([1, 2, 3]))
            elif action == "unknown-generation":
                sequence += 1
                data = acknowledgement(sequence, generation=4)
            else:
                sequence += 1
                data = acknowledgement(sequence, generation=rng.choice([1, 2, 3]))
            message = ingest_protocol.decode_message(data)
            expected = evaluate_delivery(state, data)
            records_before = self.records(store)
            actual = accept_capability_acknowledgement(store, data, at(step))
            self.assertEqual(
                (actual.outcome, actual.body, actual.failure, actual.gap_after),
                (expected.outcome, expected.response, expected.failure, expected.gap_after),
                (seed, step, action),
            )
            records = self.records(store)
            if expected.outcome == "block":
                key = (A, message["sequence"], message_sha256(data), expected.failure)
                prior = records_before.get(key)
                self.assertEqual(
                    (records[key].count, records[key].first_seen_at, records[key].last_seen_at),
                    ((prior.count if prior else 0) + 1, prior.first_seen_at if prior else at(step), at(step)),
                )
                self.events[expected.failure] += 1
                if prior is not None:
                    self.events["repeated-block"] += 1
                records = {k: v for k, v in records.items() if k != key}
                records_before = {k: v for k, v in records_before.items() if k != key}
            self.assertEqual(records, records_before, (seed, step, action))
            if expected.outcome == "receipt":
                self.events["receipt"] += 1
                if expected.gap_after is not None:
                    self.events["gap"] += 1
                if message["sequence"] in accepted:
                    self.events["retry"] += 1
                accepted[message["sequence"]] = data
                state = expected.state
            sent.append(data)


if __name__ == "__main__":
    unittest.main()
