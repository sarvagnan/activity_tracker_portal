"""Acceptance tests for `accept-sync-completion@1`.

Requirement numbers refer to `packs/accept-sync-completion-1.spec.md`. Every
database is a real `operational-store@2` database in a temporary directory,
every message is built with `ingest-protocol@1`, every fragment is archived
through `accept-ingest-batch@1`, and every value is synthetic. Assertions that
something wrote nothing compare the whole database, and every Delivery Block
is checked with its durable record.
"""

from __future__ import annotations

import base64
import collections
import dataclasses
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
from activity_tracker_portal.accept_sync_completion import accept_sync_completion
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
    CompletionRecord,
    DeliveryCycle,
    MessageHeader,
    StoreRetryable,
    open_store,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/accept-sync-completion.1.json").read_text(encoding="utf-8"))

UTC = timezone.utc
T0 = datetime(2026, 10, 15, 9, 0, 0, 123456, tzinfo=UTC)
RESERVE = 1_000_000
PLENTY = 10**12
A = "00000000-0000-4000-8000-0000000000a1"
B = "00000000-0000-4000-8000-0000000000b2"
UNKNOWN = "00000000-0000-4000-8000-0000000000c3"
WRONG_CHAIN = "0" * 64
BLOCK_FAILURES = (
    "sequence-content-mismatch", "sequence-out-of-order", "unsupported-wire-protocol",
    "completion-identity-mismatch", "completion-kind-mismatch", "completion-sequence-gap",
    "completion-count-mismatch", "completion-chain-mismatch",
)
# Each completion label: its message kind, its snapshot kind, and the cycle kind it completes.
COMPLETIONS = {
    "initial": ("snapshot-completion", "initial", "initial-snapshot"),
    "reconciliation": ("snapshot-completion", "reconciliation", "reconciliation-snapshot"),
    "change": ("change-completion", None, "change-cycle"),
}
LABEL = {cycle_kind: label for label, (_, _, cycle_kind) in COMPLETIONS.items()}


def cycle_id(number: int) -> str:
    return f"00000000-0000-4000-8000-{0xc000 + number:012x}"


def advertisement(generation: int, wire_versions: list[int] | None = None) -> dict[str, Any]:
    return {
        "wire_version": 1,
        "kind": "capability-advertisement",
        "generation": generation,
        "wire_versions": [1] if wire_versions is None else wire_versions,
        "projection_schemas": [{"record_type": "sleep", "versions": [1]}, {"record_type": "steps", "versions": [1]}],
        "max_record_count": 4,
        "max_message_bytes": 8192,
    }


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def completion(
    sequence: int, cycle: str, label: str = "change", counts: tuple[int, int, int] = (0, 0, 0),
    final_chain: str | None = None, record_type: str = "steps", generation: int = 1, installation: str = A,
) -> bytes:
    kind, snapshot_kind, _ = COMPLETIONS[label]
    value: dict[str, Any] = {
        "wire_version": 1, "kind": kind, "sender_installation": installation, "sequence": sequence,
        "capability_generation": generation, "record_type": record_type, "cycle_id": cycle,
        "page_count": counts[0], "fragment_count": counts[1], "record_count": counts[2],
        "final_chain": chain_start(cycle) if final_chain is None else final_chain,
    }
    if snapshot_kind is not None:
        value["snapshot_kind"] = snapshot_kind
    return encode_message(value)


def acknowledgement(sequence: int, generation: int = 1, installation: str = A) -> bytes:
    return encode_message({
        "wire_version": 1, "kind": "capability-acknowledgement", "sender_installation": installation,
        "sequence": sequence, "capability_generation": generation,
    })


def receipt_for(data: bytes) -> bytes:
    message = ingest_protocol.decode_message(data)
    return encode_message({
        "wire_version": 1, "kind": "delivery-receipt", "sender_installation": message["sender_installation"],
        "sequence": message["sequence"], "message_sha256": message_sha256(data),
    })


class Cycle:
    """The Sender's side of one cycle: its next fragment and its exact completion."""

    def __init__(self, cycle: str, kind: str = "change-cycle", record_type: str = "steps", installation: str = A) -> None:
        self.cycle, self.kind, self.record_type, self.installation = cycle, kind, record_type, installation
        self.page = self.index = self.fragments = self.records = 0
        self.chain = chain_start(cycle)

    def fragment(self, sequence: int, items: int = 1, new_page: bool = False, generation: int = 1) -> bytes:
        page, index = (self.page + 1, 1) if new_page or self.page == 0 else (self.page, self.index + 1)
        return encode_message({
            "wire_version": 1, "kind": "ingest-batch", "sender_installation": self.installation, "sequence": sequence,
            "capability_generation": generation, "record_type": self.record_type, "projection_schema_version": 1,
            "cycle_kind": self.kind, "cycle_id": self.cycle, "source_page_index": page, "fragment_index": index,
            "previous_chain": self.chain,
            "items": [
                {"kind": "retraction", "source_identity": f"synthetic-identity-{sequence}-{n}"} if n % 3 == 2
                else {"kind": "projection", "data": base64.b64encode(b"synthetic-%d-%d" % (sequence, n)).decode("ascii")}
                for n in range(items)
            ],
        })

    def archived(self, data: bytes) -> None:
        """Advance past a fragment the Portal accepted."""
        message = ingest_protocol.decode_message(data)
        self.page, self.index = message["source_page_index"], message["fragment_index"]
        self.chain = chain_next(self.chain, message_sha256(data))
        self.fragments += 1
        self.records += len(message["items"])

    def completion(
        self, sequence: int, label: str | None = None, counts: tuple[int, int, int] | None = None,
        final_chain: str | None = None, record_type: str | None = None, generation: int = 1,
    ) -> bytes:
        return completion(
            sequence, self.cycle, LABEL[self.kind] if label is None else label,
            (self.page, self.fragments, self.records) if counts is None else counts,
            self.chain if final_chain is None else final_chain,
            self.record_type if record_type is None else record_type, generation, self.installation,
        )


def contents(path: Path) -> dict[str, Any]:
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

    def send(self, cycle: Cycle, sequence: int, items: int = 1, new_page: bool = False, minute: float = 0.5) -> bytes:
        """Archive the cycle's next fragment through accept-ingest-batch@1."""
        data = cycle.fragment(sequence, items, new_page)
        response = accept_ingest_batch(self.store, data, at(minute))
        self.assertEqual(response.outcome, "receipt", response)
        cycle.archived(data)
        return data

    def acknowledge(self, sequence: int, generation: int, minute: float = 0.5) -> None:
        """Archive a capability acknowledgement through operational-store@2, as its own accept unit would."""
        data = acknowledgement(sequence, generation)
        self.store.archive_capability_acknowledgement(MessageHeader(
            installation_id=A, sequence=sequence, message_sha256=message_sha256(data), staged_generation=generation,
            receipt_time=at(minute), envelope=data, receipt=receipt_for(data),
        ))

    def accept(self, data: bytes, minute: float = 1) -> DeliveryResponse:
        return accept_sync_completion(self.store, data, at(minute))

    def delivery_cycle(self, cycle: str, installation: str = A) -> DeliveryCycle | None:
        with self.store.read() as view:
            return view.delivery_cycle(installation, cycle)

    def assertReceipt(self, response: DeliveryResponse, data: bytes, gap_after: int | None = None) -> None:
        self.assertEqual(response, DeliveryResponse("receipt", receipt_for(data), None, gap_after))

    def assertRetryable(self, response: DeliveryResponse, reason: str, failure: str) -> None:
        body = encode_message({"wire_version": 1, "kind": "retryable-failure", "reason": reason})
        self.assertEqual(response, DeliveryResponse("retryable", body, failure, None))

    def assertFragmentBlocked(self, data: bytes, minute: float) -> None:
        """accept-ingest-batch@1 blocks a fragment of a completed cycle."""
        response = accept_ingest_batch(self.store, data, at(minute))
        self.assertEqual((response.outcome, response.failure), ("block", "fragment-order-mismatch"))

    def block_index(self) -> dict[tuple[object, ...], Any]:
        with self.store.read() as view:
            return {(r.installation_id, r.sequence, r.message_sha256, r.failure): r for r in view.delivery_blocks()}

    def deliverBlocked(self, data: bytes, failure: str, minute: float = 1, store: Any = None) -> None:
        """Deliver a completion that must be blocked; require the durable record and no other write."""
        message = ingest_protocol.decode_message(data)
        before, records_before = contents(self.path), self.block_index()
        body = encode_message({
            "wire_version": 1, "kind": "delivery-block", "failure": failure, "message_sha256": message_sha256(data),
            "sender_installation": message["sender_installation"], "sequence": message["sequence"],
        })
        response = accept_sync_completion(store or self.store, data, at(minute))
        self.assertEqual(response, DeliveryResponse("block", body, failure, None))
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
        self.assertEqual((CONTRACT["scope"], CONTRACT["version"], CONTRACT["kind"]), ("accept-sync-completion", "1", "leaf"))
        self.assertTrue(CONTRACT["purpose"])
        self.assertEqual(
            {item["name"]: item["mode"] for item in CONTRACT["inputs"]},
            {"delivered_bytes": "pure", "receipt_time": "pure", "operational_state": "effectful", "free_space": "volatile"},
        )
        self.assertEqual({output["name"] for output in CONTRACT["outputs"]},
                         {"delivery_response", "archived_completion", "delivery_block_record"})
        by_outcome: dict[str, set[str]] = {}
        for failure in CONTRACT["named_failures"]:
            by_outcome.setdefault(failure["outcome"], set()).add(failure["name"])
        self.assertEqual(len(CONTRACT["named_failures"]), 13)
        self.assertEqual(by_outcome["delivery-block"], set(BLOCK_FAILURES))
        self.assertLessEqual(by_outcome["delivery-block"], DELIVERY_BLOCK_FAILURES)
        self.assertEqual(by_outcome["retryable"], {
            "installation-pending", "installation-ended", "installation-unknown", "storage-reserve-exhausted", "store-busy",
        })
        self.assertEqual([effect["name"] for effect in CONTRACT["observable_effects"]], ["operational-store-write"])
        pairs = [(dependency["scope"], dependency["version"]) for dependency in CONTRACT["dependencies"]]
        self.assertEqual(len(pairs), len(set(pairs)))
        self.assertEqual(set(pairs), {("ingest-protocol", "1"), ("accept-ingest-batch", "1"),
                                      ("manage-sender-installation", "1"), ("operational-store", "2")})
        for owner in ("ingest-protocol", "operational-store", "manage-sender-installation", "accept-ingest-batch",
                      "ingress shell", "complete-change-cycle"):
            self.assertTrue(any(owner in entry for entry in CONTRACT["authority"]["does_not_own"]), owner)
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["python"])
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `accept-sync-completion` |")]
        self.assertEqual(len(rows), 1)
        _, kind, purpose, _inputs, outputs, depends_on = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(sorted(depends_on.split(", ")), sorted(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]))
        self.assertIn("false completeness", purpose)
        self.assertIn("no source cursor or token", outputs)


# R2 — Caller errors


class R2CallerErrors(Case):
    def test_invalid_time_is_a_caller_error(self) -> None:
        for now in (datetime(2026, 10, 15, 9, 0), "2026-10-15T09:00:00Z", None):
            with self.assertRaises(ValueError):
                accept_sync_completion(Untouchable(), completion(1, cycle_id(1)), now)  # type: ignore[arg-type]

    def test_other_bytes_are_caller_errors(self) -> None:
        def raw(value: dict[str, Any]) -> bytes:
            return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

        valid = json.loads(completion(1, cycle_id(1), "initial"))
        change = json.loads(completion(1, cycle_id(1), "change"))
        others = [
            b"synthetic bytes that are not json",
            json.dumps(valid, indent=1).encode(),
            raw({**valid, "snapshot_kind": "partial"}),
            raw({**change, "snapshot_kind": "initial"}),
            raw({**valid, "record_count": -1}),
            Cycle(cycle_id(1)).fragment(1),
            acknowledgement(1),
            encode_message({
                "wire_version": 1, "kind": "sync-checkpoint", "sender_installation": A, "sequence": 1,
                "capability_generation": 1, "run_sequence": 1, "trigger": "background",
                "started_at": "2026-10-15T08:59:00.000Z", "ended_at": "2026-10-15T08:59:30.000Z",
                "background_scheduling": "available", "record_types": [],
                "outbox_messages_before_checkpoint": 0, "outbox_bytes_before_checkpoint": 0,
            }),
            encode_message(advertisement(1)),
            receipt_for(completion(1, cycle_id(1))),
        ]
        for data in others:
            with self.assertRaises(ValueError):
                accept_sync_completion(Untouchable(), data, T0)  # type: ignore[arg-type]


# R3 — Identity and retry


class R3IdentityAndRetry(Case):
    def test_identical_retry_returns_the_archived_receipt(self) -> None:
        self.activate()
        cycle = Cycle(cycle_id(1))
        self.send(cycle, 1, items=2)
        self.send(cycle, 2, items=0)
        data = cycle.completion(3, generation=1)
        first = self.accept(data, minute=3)
        self.assertReceipt(first, data)
        before = contents(self.path)
        self.assertEqual(self.accept(data, minute=7), first)
        self.assertEqual(contents(self.path), before)
        # Still answered after its generation stops, the installation retires, and the reserve runs out.
        self.acknowledge(4, generation=2)
        self.store.stop_honoring_generation(1, at(8))
        self.store.begin_draining(A, at(9))
        self.store.retire_installation(A, "destructive", at(10))
        self.free.value = RESERVE - 1
        before = contents(self.path)
        self.assertEqual(self.accept(data, minute=11), first)
        self.assertEqual(contents(self.path), before)

    def test_conflict_after_retirement_is_still_blocked(self) -> None:
        self.activate()
        cycle = Cycle(cycle_id(1))
        self.send(cycle, 1)
        self.accept(cycle.completion(2))
        self.store.begin_draining(A, at(2))
        self.store.retire_installation(A, "drained", at(3))
        for minute in (4, 8):
            self.deliverBlocked(cycle.completion(2, generation=2), "sequence-content-mismatch", minute=minute)
        with self.store.read() as view:
            self.assertEqual([(r.first_seen_at, r.last_seen_at, r.count) for r in view.delivery_blocks()], [(at(4), at(8), 2)])

    def test_same_sequence_different_bytes_is_blocked(self) -> None:
        self.activate()
        cycle = Cycle(cycle_id(1))
        self.send(cycle, 1)
        # At an archived fragment's sequence, and at an archived completion's.
        self.deliverBlocked(cycle.completion(1), "sequence-content-mismatch", minute=2)
        self.assertReceipt(self.accept(cycle.completion(2), minute=3), cycle.completion(2))
        for minute in (4, 5, 9):
            self.deliverBlocked(cycle.completion(2, generation=2), "sequence-content-mismatch", minute=minute)
        with self.store.read() as view:
            self.assertEqual(
                sorted((r.sequence, r.first_seen_at, r.last_seen_at, r.count) for r in view.delivery_blocks()),
                [(1, at(2), at(2), 1), (2, at(4), at(9), 3)],
            )


# R4 — Installation state


class R4InstallationState(Case):
    def test_non_delivering_installations_are_retryable(self) -> None:
        self.enroll(A)
        before = contents(self.path)
        # Installation state is checked before the completion proof.
        for counts in ((0, 0, 0), (9, 9, 9)):
            self.assertRetryable(self.accept(completion(1, cycle_id(1), counts=counts)),
                                 "temporarily-unavailable", "installation-pending")
            self.assertRetryable(self.accept(completion(1, cycle_id(1), counts=counts, installation=UNKNOWN)),
                                 "temporarily-unavailable", "installation-unknown")
        self.assertEqual(contents(self.path), before)
        self.store.withdraw_installation(A, at(2))
        self.activate(B, minute=3)
        cycle = Cycle(cycle_id(1), installation=B)
        self.send(cycle, 1, minute=3)
        self.store.begin_draining(B, at(4))
        self.store.retire_installation(B, "drained", at(5))
        before = contents(self.path)
        for data in (completion(1, cycle_id(1)), cycle.completion(2), cycle.completion(2, counts=(9, 9, 9))):
            self.assertRetryable(self.accept(data, minute=6), "temporarily-unavailable", "installation-ended")
        self.assertEqual(contents(self.path), before)

    def test_draining_installation_still_delivers(self) -> None:
        self.activate()
        cycle = Cycle(cycle_id(1), "reconciliation-snapshot")
        self.send(cycle, 1, items=3)
        self.store.begin_draining(A, at(1))
        data = cycle.completion(2, generation=2)
        self.assertReceipt(self.accept(data, minute=2), data)


# R5 — Sequence order and gaps


class R5SequenceOrderAndGaps(Case):
    def test_older_unseen_sequence_is_blocked(self) -> None:
        self.activate()
        cycle = Cycle(cycle_id(1))
        self.send(cycle, 1)
        self.send(Cycle(cycle_id(2)), 4)
        self.deliverBlocked(completion(2, cycle_id(3)), "sequence-out-of-order", minute=2)
        self.deliverBlocked(cycle.completion(3), "sequence-out-of-order", minute=3)

    def test_gap_is_accepted_only_for_an_empty_cycle(self) -> None:
        self.activate()
        first = completion(2, cycle_id(1))
        self.assertReceipt(self.accept(first), first, gap_after=0)
        cycle = Cycle(cycle_id(2))
        self.send(cycle, 3)
        self.send(cycle, 4)
        # A cycle with fragments: the gap its completion would open lies inside the cycle.
        self.deliverBlocked(cycle.completion(6), "completion-sequence-gap", minute=2)
        empty = completion(6, cycle_id(3), "initial")
        self.assertReceipt(self.accept(empty, minute=3), empty, gap_after=4)
        with self.store.read() as view:
            self.assertEqual([(gap.gap_after, gap.sequence, gap.detected_at) for gap in view.sequence_gaps(A)],
                             [(0, 2, at(1)), (4, 6, at(3))])


# R6 — Generation and version


class R6GenerationAndVersion(Case):
    def test_unhonored_generation_is_unsupported(self) -> None:
        self.activate()
        cycle = Cycle(cycle_id(1))
        self.send(cycle, 1)
        self.deliverBlocked(cycle.completion(3, generation=3), "unsupported-wire-protocol")
        # The generation is checked before the completion proof.
        self.deliverBlocked(cycle.completion(3, generation=3, counts=(9, 9, 9)), "unsupported-wire-protocol")
        # A generation that stopped being honored is unsupported for a new completion.
        self.acknowledge(2, generation=2)
        self.store.stop_honoring_generation(1, at(3))
        self.deliverBlocked(cycle.completion(3, generation=1), "unsupported-wire-protocol", minute=4)
        # A valid advertisement may list only a later Wire Protocol Version.
        self.store.honor_generation(3, encode_message(advertisement(3, wire_versions=[2])), at(5))
        self.deliverBlocked(cycle.completion(3, generation=3), "unsupported-wire-protocol", minute=6)
        data = cycle.completion(3, generation=2)
        self.assertReceipt(self.accept(data, minute=7), data)


# R7 — The completion proof


class R7CompletionProof(Case):
    def test_exact_cycles_are_accepted(self) -> None:
        # The same cycle identity under an earlier installation is a separate cycle.
        self.activate(B)
        earlier = Cycle(cycle_id(1), "initial-snapshot", installation=B)
        self.send(earlier, 1, items=4)
        self.send(earlier, 2, items=4)
        self.store.begin_draining(B, at(0.6))
        self.store.retire_installation(B, "drained", at(0.7))
        self.activate(A, minute=1)
        initial = Cycle(cycle_id(1), "initial-snapshot")
        reconciliation = Cycle(cycle_id(2), "reconciliation-snapshot", "sleep")
        change = Cycle(cycle_id(3))
        # Concurrent cycles interleave with each other and with other kinds of message.
        self.send(initial, 1, items=2)
        self.send(reconciliation, 2, items=1)
        self.send(initial, 3, items=0)
        self.acknowledge(4, generation=2)
        self.send(change, 5, items=3)
        self.send(initial, 6, items=4, new_page=True)
        self.send(reconciliation, 7, items=4, new_page=True)
        self.send(change, 8, items=0)
        for cycle, sequence, counts in ((initial, 9, (2, 3, 6)), (change, 10, (1, 2, 3))):
            self.assertEqual((cycle.page, cycle.fragments, cycle.records), counts)
            data = cycle.completion(sequence)
            self.assertReceipt(self.accept(data, minute=2), data)
        self.send(reconciliation, 11, items=2)
        self.assertEqual((reconciliation.page, reconciliation.fragments, reconciliation.records), (2, 3, 7))
        data = reconciliation.completion(12)
        self.assertReceipt(self.accept(data, minute=3), data)

    def test_empty_cycle_completion(self) -> None:
        self.activate()
        for sequence, (label, cycle) in enumerate((("initial", cycle_id(1)), ("reconciliation", cycle_id(2)),
                                                   ("change", cycle_id(3))), start=1):
            for counts in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):
                self.deliverBlocked(completion(sequence, cycle, label, counts), "completion-count-mismatch")
            for chain in (chain_start(cycle_id(9)), chain_next(chain_start(cycle), WRONG_CHAIN)):
                self.deliverBlocked(completion(sequence, cycle, label, final_chain=chain), "completion-chain-mismatch")
            data = completion(sequence, cycle, label)
            self.assertReceipt(self.accept(data, minute=2), data)
            self.assertEqual(self.delivery_cycle(cycle), DeliveryCycle(
                installation_id=A, cycle_id=cycle, record_type="steps", cycle_kind=None, first_sequence=None,
                last_sequence=None, last_source_page_index=0, last_fragment_index=0, last_previous_chain=None,
                last_message_sha256=None, fragment_count=0, record_count=0, completion_sequence=sequence,
            ))
        # An empty cycle is closed by its completion like any other.
        self.deliverBlocked(completion(4, cycle_id(1), "initial"), "completion-identity-mismatch", minute=3)
        self.assertFragmentBlocked(Cycle(cycle_id(3)).fragment(4), minute=4)

    def test_completed_cycle_takes_no_second_completion(self) -> None:
        self.activate()
        cycle = Cycle(cycle_id(1))
        self.send(cycle, 1, items=2)
        self.send(cycle, 2, items=1, new_page=True)
        self.assertReceipt(self.accept(cycle.completion(3)), cycle.completion(3))
        # Neither the same proof at a new sequence nor any other completion of the cycle is accepted.
        for data in (cycle.completion(4), cycle.completion(4, generation=2), cycle.completion(4, label="initial"),
                     cycle.completion(4, record_type="sleep"),
                     cycle.completion(4, counts=(0, 0, 0), final_chain=chain_start(cycle.cycle))):
            self.deliverBlocked(data, "completion-identity-mismatch", minute=2)
        self.deliverBlocked(cycle.completion(4), "completion-identity-mismatch", minute=5)
        self.assertEqual(self.delivery_cycle(cycle.cycle).completion_sequence, 3)
        self.assertFragmentBlocked(cycle.fragment(4), minute=6)

    def test_other_record_type_is_an_identity_mismatch(self) -> None:
        self.activate()
        steps, sleep = Cycle(cycle_id(1)), Cycle(cycle_id(2), "initial-snapshot", "sleep")
        self.send(steps, 1)
        self.send(sleep, 2, items=2)
        self.deliverBlocked(steps.completion(3, record_type="sleep"), "completion-identity-mismatch")
        self.deliverBlocked(sleep.completion(3, record_type="steps"), "completion-identity-mismatch")
        # The record type is checked before the kind and the proof.
        self.deliverBlocked(steps.completion(3, label="reconciliation", record_type="sleep", counts=(9, 9, 9)),
                            "completion-identity-mismatch")
        data = steps.completion(3)
        self.assertReceipt(self.accept(data, minute=2), data)

    def test_kind_must_match_the_cycle(self) -> None:
        self.activate()
        cycles = [Cycle(cycle_id(n), kind) for n, kind in
                  enumerate(("initial-snapshot", "reconciliation-snapshot", "change-cycle"), start=1)]
        for sequence, cycle in enumerate(cycles, start=1):
            self.send(cycle, sequence, items=sequence)
        for sequence, cycle in enumerate(cycles, start=4):
            for label in sorted(set(COMPLETIONS) - {LABEL[cycle.kind]}):
                self.deliverBlocked(cycle.completion(sequence, label=label), "completion-kind-mismatch")
            data = cycle.completion(sequence)
            self.assertReceipt(self.accept(data, minute=2), data)

    def test_missing_sequence_in_the_cycle_is_a_gap(self) -> None:
        self.activate()
        first, second, third, fourth = (Cycle(cycle_id(n)) for n in range(1, 5))
        # A sequence skipped inside the cycle; sequences 1 and 4 are never delivered.
        self.send(first, 2)
        self.send(first, 3)
        self.send(first, 5, new_page=True)
        for minute in (1, 4):
            self.deliverBlocked(first.completion(6), "completion-sequence-gap", minute=minute)
        # A sequence left by a blocked message inside the cycle.
        self.send(second, 6)
        self.deliverBlocked(completion(7, cycle_id(9), generation=3), "unsupported-wire-protocol")
        self.send(second, 8)
        self.deliverBlocked(second.completion(9), "completion-sequence-gap")
        # A sequence missing between the cycle's last fragment and its completion.
        self.send(third, 9)
        self.send(fourth, 11)
        self.deliverBlocked(third.completion(12), "completion-sequence-gap")
        # Earlier holes do not block a cycle that started after them.
        data = fourth.completion(12)
        self.assertReceipt(self.accept(data, minute=5), data)
        with self.store.read() as view:
            self.assertEqual([(gap.gap_after, gap.sequence) for gap in view.sequence_gaps(A)],
                             [(0, 2), (3, 5), (6, 8), (9, 11)])
            for cycle in (first, second, third):
                self.assertIsNone(view.delivery_cycle(A, cycle.cycle).completion_sequence)

    def test_counts_must_match_the_archived_fragments(self) -> None:
        self.activate()
        cycle = Cycle(cycle_id(1), "reconciliation-snapshot")
        self.send(cycle, 1, items=2)
        self.send(cycle, 2, items=0)
        self.send(cycle, 3, items=3, new_page=True)
        self.assertEqual((cycle.page, cycle.fragments, cycle.records), (2, 3, 5))
        for counts in ((1, 3, 5), (3, 3, 5), (2, 2, 5), (2, 4, 5), (2, 3, 4), (2, 3, 6), (3, 2, 5), (0, 0, 0)):
            self.deliverBlocked(cycle.completion(4, counts=counts), "completion-count-mismatch")
        data = cycle.completion(4)
        self.assertReceipt(self.accept(data, minute=2), data)

    def test_final_chain_must_match(self) -> None:
        self.activate()
        cycle = Cycle(cycle_id(1), "initial-snapshot")
        first = self.send(cycle, 1, items=2)
        after_first = cycle.chain
        last = self.send(cycle, 2, items=1)
        for chain in (chain_start(cycle.cycle), after_first, message_sha256(last),
                      chain_next(after_first, message_sha256(first)), chain_next(cycle.chain, message_sha256(last)),
                      WRONG_CHAIN):
            self.deliverBlocked(cycle.completion(3, final_chain=chain), "completion-chain-mismatch")
        data = cycle.completion(3)
        self.assertReceipt(self.accept(data, minute=2), data)

    def test_completion_failures_follow_protocol_order(self) -> None:
        self.activate()
        closed, other, holed, plain = (Cycle(cycle_id(n)) for n in range(1, 5))
        self.send(closed, 1)
        self.assertReceipt(self.accept(closed.completion(2)), closed.completion(2))
        self.send(other, 3)
        self.send(holed, 5)
        self.send(holed, 7)
        self.send(plain, 8)
        wrong: dict[str, Any] = {"counts": (9, 9, 9), "final_chain": WRONG_CHAIN}
        # Each check wins over every later one.
        for data, failure in (
            (holed.completion(2, label="initial", record_type="sleep", **wrong), "sequence-content-mismatch"),
            (closed.completion(4, label="initial", record_type="sleep", **wrong), "sequence-out-of-order"),
            (closed.completion(9, label="initial", record_type="sleep", generation=3, **wrong), "unsupported-wire-protocol"),
            (closed.completion(9, label="initial", record_type="sleep", **wrong), "completion-identity-mismatch"),
            (other.completion(9, label="initial", record_type="sleep", **wrong), "completion-identity-mismatch"),
            (holed.completion(9, label="initial", **wrong), "completion-kind-mismatch"),
            (holed.completion(9, **wrong), "completion-sequence-gap"),
            (plain.completion(9, **wrong), "completion-count-mismatch"),
            (plain.completion(9, final_chain=WRONG_CHAIN), "completion-chain-mismatch"),
        ):
            self.deliverBlocked(data, failure)


# R8 — Archival and the completed cycle


class R8ArchivalAndCompletedCycle(Case):
    def test_accepted_completion_is_archived_exactly(self) -> None:
        self.activate()
        snapshot = Cycle(cycle_id(1), "initial-snapshot")
        self.send(snapshot, 1, items=2)
        self.send(snapshot, 2, items=1, new_page=True)
        cases = (
            (snapshot.completion(3, generation=2), 7, 2, None,
             CompletionRecord("snapshot", "initial", "steps", cycle_id(1), 2, 2, 3, snapshot.chain)),
            (completion(5, cycle_id(2), "change", record_type="sleep"), 9, 1, 3,
             CompletionRecord("change", None, "sleep", cycle_id(2), 0, 0, 0, chain_start(cycle_id(2)))),
        )
        for data, minute, generation, gap_after, record in cases:
            message = ingest_protocol.decode_message(data)
            sequence = message["sequence"]
            before = contents(self.path)
            with self.store.read() as view:
                status_before = view.installation(A)
            response = self.accept(data, minute=minute)
            self.assertReceipt(response, data, gap_after)
            with self.store.read() as view:
                archived = view.archived_message(A, sequence)
                status = view.installation(A)
                gaps = [(gap.gap_after, gap.sequence, gap.detected_at) for gap in view.sequence_gaps(A)]
            assert archived is not None
            self.assertEqual(
                (archived.kind, archived.envelope, archived.receipt, archived.receipt_time, archived.staged_generation,
                 archived.message_sha256, archived.batch, archived.completion, archived.checkpoint),
                (message["kind"], data, response.body, at(minute), generation, message_sha256(data), None, record, None),
            )
            self.assertEqual(status, dataclasses.replace(status_before, highest_sequence=sequence, last_delivery_at=at(minute)))
            self.assertEqual(gaps, [] if gap_after is None else [(gap_after, sequence, at(minute))])
            # Exactly the message, its completion evidence, any gap, and the installation's progress were written.
            after = contents(self.path)
            changed = {"archived_messages", "completions", "sequence_gaps", "installations"}
            self.assertEqual((after["user_version"], after["schema"]), (before["user_version"], before["schema"]))
            self.assertEqual({name: rows for name, rows in after["tables"].items() if name not in changed},
                             {name: rows for name, rows in before["tables"].items() if name not in changed})
            for table, added in (("archived_messages", 1), ("completions", 1), ("installations", 0),
                                 ("sequence_gaps", 0 if gap_after is None else 1)):
                self.assertEqual(len(after["tables"][table]), len(before["tables"][table]) + added, table)
            for table in ("archived_messages", "completions", "sequence_gaps"):
                self.assertLessEqual(set(before["tables"][table]), set(after["tables"][table]), table)
            self.assertEqual(
                set(after["tables"]["completions"]) - set(before["tables"]["completions"]),
                {(A, sequence, record.completion_kind, record.snapshot_kind, record.record_type, record.cycle_id,
                  record.page_count, record.fragment_count, record.record_count, record.final_chain)},
            )

    def test_completion_closes_the_cycle(self) -> None:
        self.activate()
        cycle = Cycle(cycle_id(1))
        self.send(cycle, 1, items=3)
        self.send(cycle, 2, items=1, new_page=True)
        with self.store.read() as view:
            open_cycle = view.delivery_cycle(A, cycle.cycle)
            generations, acknowledged = view.honored_generations(), view.latest_acknowledged_generation()
        assert open_cycle is not None
        self.assertIsNone(open_cycle.completion_sequence)
        data = cycle.completion(3)
        self.assertReceipt(self.accept(data, minute=4), data)
        with self.store.read() as view:
            self.assertEqual(view.delivery_cycle(A, cycle.cycle), dataclasses.replace(open_cycle, completion_sequence=3))
            self.assertEqual((view.honored_generations(), view.latest_acknowledged_generation()), (generations, acknowledged))
        # accept-ingest-batch@1 now blocks the cycle's later fragments.
        self.assertFragmentBlocked(cycle.fragment(4), minute=5)


# R9 — Capacity and contention


class R9CapacityAndContention(Case):
    def test_exhausted_reserve_is_retryable_and_blocks_still_win(self) -> None:
        self.activate()
        holed, closed, plain = (Cycle(cycle_id(n)) for n in range(1, 4))
        self.send(holed, 1)
        self.send(closed, 2)
        self.accept(closed.completion(3))
        self.send(plain, 6)
        self.free.value = RESERVE - 1
        wrong: dict[str, Any] = {"counts": (9, 9, 9), "final_chain": WRONG_CHAIN}
        for data, failure in (
            (holed.completion(1), "sequence-content-mismatch"),
            (plain.completion(4), "sequence-out-of-order"),
            (plain.completion(7, generation=3), "unsupported-wire-protocol"),
            (closed.completion(7), "completion-identity-mismatch"),
            (holed.completion(7, label="initial"), "completion-kind-mismatch"),
            (holed.completion(7), "completion-sequence-gap"),
            (plain.completion(7, counts=wrong["counts"]), "completion-count-mismatch"),
            (plain.completion(7, final_chain=WRONG_CHAIN), "completion-chain-mismatch"),
        ):
            self.deliverBlocked(data, failure)
        before = contents(self.path)
        for data in (plain.completion(7), completion(7, cycle_id(9), "reconciliation")):
            self.assertRetryable(self.accept(data), "storage-reserve-exhausted", "storage-reserve-exhausted")
        self.assertEqual(contents(self.path), before)
        self.free.value = RESERVE
        self.assertReceipt(self.accept(plain.completion(7)), plain.completion(7))

    def test_busy_store_is_retryable(self) -> None:
        self.activate()
        cycle = Cycle(cycle_id(1))
        self.send(cycle, 1, items=2)
        exact, wrong = cycle.completion(2), cycle.completion(2, counts=(1, 1, 1))
        before = contents(self.path)
        holder = sqlite3.connect(self.path, isolation_level=None, timeout=0)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        try:
            for data in (exact, wrong):
                self.assertRetryable(self.accept(data), "temporarily-unavailable", "store-busy")
        finally:
            holder.execute("ROLLBACK")
        for operation, data in (("read", exact), ("read", wrong), ("archive_completion", exact),
                                ("record_delivery_block", wrong)):
            self.assertRetryable(accept_sync_completion(BusyAt(self.store, operation), data, at(2)),
                                 "temporarily-unavailable", "store-busy")
        self.assertEqual(contents(self.path), before)
        # The proof is checked before archival: a busy archive never hides a block.
        busy_archive = BusyAt(self.store, "archive_completion")
        for minute in (3, 6):
            self.deliverBlocked(wrong, "completion-count-mismatch", minute=minute, store=busy_archive)
        self.assertEqual(without_blocks(contents(self.path)), without_blocks(before))
        self.assertReceipt(self.accept(exact), exact)


# R10 — Conformance with the protocol's evaluation


class R10Conformance(Case):
    def test_answers_match_the_protocol_evaluation(self) -> None:
        self.events: collections.Counter[str] = collections.Counter()
        for seed in range(10):
            with self.subTest(seed=seed):
                self.run_history(seed)
        required = ("receipt", "gap", "retry", "repeated-block", "empty", "concurrent", "fragment-after-completion",
                    *(f"accepted-{label}" for label in COMPLETIONS), "accepted-sleep", "accepted-steps", *BLOCK_FAILURES)
        for event in required:
            self.assertGreater(self.events[event], 0, event)

    def records(self, store) -> dict[tuple[object, ...], Any]:
        with store.read() as view:
            return {(r.installation_id, r.sequence, r.message_sha256, r.failure): r for r in view.delivery_blocks()}

    def run_history(self, seed: int) -> None:
        rng = random.Random(seed)
        directory = self.directory / f"seed-{seed}"
        directory.mkdir()
        path = directory / "portal.sqlite3"
        store = self.open(path)
        for generation in (1, 2, 3):
            store.honor_generation(generation, encode_message(advertisement(generation)), T0)
        self.activate(A, store=store)
        state = new_delivery_state([advertisement(1), advertisement(2), advertisement(3)])
        sequence, numbers = 0, 0
        cycles: dict[str, Cycle] = {}
        open_cycles: list[Cycle] = []
        closed: list[Cycle] = []
        sent: list[bytes] = []
        accepted: set[int] = set()
        skipped: list[int] = []

        def new_cycle() -> Cycle:
            nonlocal numbers
            numbers += 1
            cycle = Cycle(cycle_id(numbers), rng.choice(sorted(LABEL)), rng.choice(["sleep", "steps"]))
            cycles[cycle.cycle] = cycle
            return cycle

        def blocked_new_sequence() -> None:
            # A blocked new sequence is either reused by the next message or left as a hole.
            nonlocal sequence
            if rng.random() < 0.75:
                sequence -= 1
            else:
                skipped.append(sequence)

        for step in range(150):
            action = rng.choice(
                ["open"] * 2 + ["fragment"] * 5 + ["complete"] * 4
                + ["late-fragment", "ack", "skip", "blocked-hole", "empty", "wrong-kind", "wrong-record-type",
                   "wrong-counts", "wrong-chain", "second", "retry", "retry", "conflict", "old", "unknown-generation"]
            )
            if action == "skip":
                sequence += 1
                skipped.append(sequence)
                continue
            if action == "ack":
                sequence += 1
                data = acknowledgement(sequence, rng.choice([1, 2, 3]))
                expected = evaluate_delivery(state, data)
                self.assertEqual(expected.outcome, "receipt")
                message = ingest_protocol.decode_message(data)
                store.archive_capability_acknowledgement(MessageHeader(
                    installation_id=A, sequence=sequence, message_sha256=message_sha256(data),
                    staged_generation=message["capability_generation"], receipt_time=at(step), envelope=data,
                    receipt=receipt_for(data),
                ))
                accepted.add(sequence)
                state = expected.state
                continue
            if action in ("open", "fragment", "late-fragment", "blocked-hole"):
                if action == "open" or (action in ("fragment", "blocked-hole") and not open_cycles):
                    cycle = new_cycle()
                elif action == "late-fragment":
                    if not closed:
                        continue
                    cycle = rng.choice(closed)
                else:
                    cycle = rng.choice(open_cycles)
                sequence += 1
                generation = 4 if action == "blocked-hole" else rng.choice([1, 2, 3])
                data = cycle.fragment(sequence, rng.randint(0, 4), rng.random() < 0.3, generation)
                expected = evaluate_delivery(state, data)
                response = accept_ingest_batch(store, data, at(step))
                self.assertEqual((response.outcome, response.body), (expected.outcome, expected.response), (seed, step, action))
                if expected.outcome == "receipt":
                    accepted.add(sequence)
                    state = expected.state
                    cycle.archived(data)
                    if cycle not in open_cycles:
                        open_cycles.append(cycle)
                else:
                    if action == "late-fragment":
                        self.events["fragment-after-completion"] += 1
                    blocked_new_sequence()
                continue

            if action in ("complete", "wrong-kind", "wrong-record-type", "wrong-counts", "wrong-chain", "unknown-generation"):
                if not open_cycles:
                    continue
                cycle = rng.choice(open_cycles)
                sequence += 1
                if action == "wrong-kind":
                    data = cycle.completion(sequence, label=rng.choice(sorted(set(COMPLETIONS) - {LABEL[cycle.kind]})))
                elif action == "wrong-record-type":
                    data = cycle.completion(sequence, record_type="sleep" if cycle.record_type == "steps" else "steps")
                elif action == "wrong-counts":
                    counts = [cycle.page, cycle.fragments, cycle.records]
                    index = rng.randrange(3)
                    counts[index] += 1 if counts[index] == 0 or rng.random() < 0.5 else -1
                    data = cycle.completion(sequence, counts=(counts[0], counts[1], counts[2]))
                elif action == "wrong-chain":
                    data = cycle.completion(sequence, final_chain=rng.choice([chain_start(cycle.cycle), WRONG_CHAIN]))
                else:
                    data = cycle.completion(sequence, generation=4 if action == "unknown-generation" else rng.choice([1, 2, 3]))
            elif action == "empty":
                sequence += 1
                cycle = new_cycle()
                data = cycle.completion(sequence, generation=rng.choice([1, 2, 3]))
            elif action == "second":
                if not closed:
                    continue
                sequence += 1
                cycle = rng.choice(closed)
                data = cycle.completion(sequence, generation=rng.choice([1, 2, 3]))
            elif action == "retry":
                if not sent:
                    continue
                data = rng.choice(sent)
            elif action == "conflict":
                if not accepted:
                    continue
                data = completion(rng.choice(sorted(accepted)), cycle_id(100_000 + step), rng.choice(sorted(COMPLETIONS)))
            else:  # old
                if not skipped:
                    continue
                data = completion(rng.choice(skipped), cycle_id(100_000 + step), rng.choice(sorted(COMPLETIONS)))

            message = ingest_protocol.decode_message(data)
            new = message["sequence"] == sequence and message["sequence"] not in accepted and action not in ("retry", "conflict", "old")
            expected = evaluate_delivery(state, data)
            records_before = self.records(store)
            database_before = contents(path)
            actual = accept_sync_completion(store, data, at(step))
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
                if new:
                    blocked_new_sequence()
                if expected.failure == "completion-sequence-gap" and cycles[message["cycle_id"]] in open_cycles:
                    # The hole is permanent: the Sender abandons the cycle.
                    open_cycles.remove(cycles[message["cycle_id"]])
            self.assertEqual(records, records_before, (seed, step, action))
            database_after = contents(path)
            if expected.outcome == "block":
                self.assertEqual(without_blocks(database_after), without_blocks(database_before), (seed, step, action))
            elif message["sequence"] in accepted:
                self.assertEqual(database_after, database_before, (seed, step, action))
                self.events["retry"] += 1
            if expected.outcome == "receipt":
                self.events["receipt"] += 1
                if expected.gap_after is not None:
                    self.events["gap"] += 1
                if message["sequence"] not in accepted:
                    target = cycles.get(message["cycle_id"])
                    label = message.get("snapshot_kind") or "change"
                    if target is None or target.fragments == 0:
                        self.events["empty"] += 1
                    else:
                        self.events[f"accepted-{label}"] += 1
                        self.events[f"accepted-{message['record_type']}"] += 1
                        if len(open_cycles) > 1:
                            self.events["concurrent"] += 1
                    if target is not None:
                        if target in open_cycles:
                            open_cycles.remove(target)
                        if target not in closed:
                            closed.append(target)
                    if message["sequence"] in skipped:
                        skipped.remove(message["sequence"])
                    accepted.add(message["sequence"])
                    state = expected.state
            sent.append(data)


if __name__ == "__main__":
    unittest.main()
