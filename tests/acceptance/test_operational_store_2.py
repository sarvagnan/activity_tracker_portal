"""Acceptance tests for `operational-store@2`.

Requirement numbers refer to `packs/operational-store-2.spec.md`. `@1`'s
requirements R1-R12 stay bound to `test_operational_store_1.py`, which the
`@2` runner re-runs unchanged. Every database lives in a temporary directory,
every value is synthetic, and the store is exercised only through its
Interface plus the on-disk migration record that `@1` R3 makes part of the
contract. Like `@1`'s suite, this one never imports `ingest_protocol`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

from activity_tracker_portal import operational_store as store_module
from activity_tracker_portal.operational_store import (
    BatchItem,
    CompletionRecord,
    DeliveryCycle,
    IngestBatchRecord,
    MessageHeader,
    RecordTypeOutcome,
    SyncCheckpointRecord,
    open_store,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_1 = json.loads((ROOT / "governance/scopes/operational-store.1.json").read_text(encoding="utf-8"))
CONTRACT_2 = json.loads((ROOT / "governance/scopes/operational-store.2.json").read_text(encoding="utf-8"))

UTC = timezone.utc
T0 = datetime(2026, 10, 15, 9, 0, 0, 123456, tzinfo=UTC)
RESERVE = 1_000_000
PLENTY = 10**12
INSTALLATION = "0f8fad5b-d9cb-469f-a165-70867728950e"
OTHER = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
CYCLE = "e3b0c442-98fc-4c14-9afb-f4c8996fb924"
OTHER_CYCLE = "5d41402a-bc4b-4a76-b971-9d911017c592"
THIRD_CYCLE = "16fd2706-8baf-433b-82eb-8c7fada847da"
HEALTH_MARKER = b"synthetic-health-marker"
IDENTITY_MARKER = "synthetic-identity-marker"
# Lowercase SHA-256 of operational-store@1's one baseline migration SQL.
BASELINE_CHECKSUM = "45d360c0b2b535d4c18c5337ca1c123e487a3aed65550a38179cab3080c5f811"


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def chain(label: str) -> str:
    """A synthetic 64-character hex chain value; the store never interprets it."""
    return digest(b"synthetic-chain-" + label.encode())


def envelope(installation_id: str, sequence: int, kind: str) -> bytes:
    body = {"synthetic": True, "kind": kind, "installation": installation_id, "sequence": sequence}
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode()


def header(installation_id: str, sequence: int, kind: str) -> MessageHeader:
    data = envelope(installation_id, sequence, kind)
    return MessageHeader(
        installation_id=installation_id,
        sequence=sequence,
        message_sha256=digest(data),
        staged_generation=1,
        receipt_time=at(sequence),
        envelope=data,
        receipt=b'{"synthetic-receipt":%d}' % sequence,
    )


def projections(count: int, marker: bytes = b"synthetic-projection") -> tuple[BatchItem, ...]:
    return tuple(BatchItem(projection=marker + b"-%d" % index) for index in range(count))


def retractions(count: int, marker: str = "synthetic-identity") -> tuple[BatchItem, ...]:
    return tuple(BatchItem(retracted_identity=f"{marker}-{index}") for index in range(count))


def fragment(
    cycle_id: str,
    page: int,
    index: int,
    previous: str,
    items: tuple[BatchItem, ...],
    *,
    record_type: str = "steps",
    cycle_kind: str = "initial-snapshot",
) -> IngestBatchRecord:
    return IngestBatchRecord(
        record_type=record_type,
        projection_schema_version=1,
        cycle_kind=cycle_kind,  # type: ignore[arg-type]
        cycle_id=cycle_id,
        source_page_index=page,
        fragment_index=index,
        previous_chain=previous,
        items=items,
    )


def completion(cycle_id: str, record_type: str = "steps") -> CompletionRecord:
    return CompletionRecord(
        completion_kind="snapshot",
        snapshot_kind="initial",
        record_type=record_type,
        cycle_id=cycle_id,
        page_count=2,
        fragment_count=3,
        record_count=6,
        final_chain=chain("final-" + cycle_id),
    )


def checkpoint(run_sequence: int) -> SyncCheckpointRecord:
    return SyncCheckpointRecord(
        run_sequence=run_sequence,
        trigger="background",
        started_at="2026-10-15T08:59:00.000Z",
        ended_at="2026-10-15T08:59:30.000Z",
        elapsed_ms=30000,
        background_scheduling="available",
        record_types=(RecordTypeOutcome(record_type="steps", outcome="progressed", reason=None, source_data_staged=True),),
        outbox_messages_before_checkpoint=1,
        outbox_bytes_before_checkpoint=1024,
    )


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "portal.sqlite3"

    def open(self):
        store = open_store(self.path, reserve_bytes=RESERVE, free_bytes=lambda _path: PLENTY)
        self.addCleanup(store.close)
        return store

    def activate(self, store, installation_id: str, minute: float = 0) -> None:
        offer = digest(b"synthetic-enrollment-code-" + installation_id.encode())
        store.record_enrollment_offer(offer, at(minute + 30), at(minute))
        store.enroll_installation(offer, installation_id, digest(b"synthetic-fingerprint-" + installation_id.encode()), at(minute))
        store.activate_installation(installation_id, at(minute))

    def ready(self):
        store = self.open()
        self.activate(store, INSTALLATION)
        store.honor_generation(1, b'{"synthetic-advertisement":1}', T0)
        return store

    def cycle(self, store, installation_id: str, cycle_id: str) -> DeliveryCycle | None:
        with store.read() as view:
            return view.delivery_cycle(installation_id, cycle_id)


class R13GovernedScopeContractVersion2(unittest.TestCase):
    def test_contract_2_keeps_version_1_and_adds_cycle_reads(self) -> None:
        self.assertEqual(CONTRACT_2["scope"], "operational-store")
        self.assertEqual(CONTRACT_2["version"], "2")
        self.assertEqual(CONTRACT_2["kind"], CONTRACT_1["kind"])
        for key in ("inputs", "named_failures", "observable_effects", "dependencies"):
            self.assertEqual(CONTRACT_2[key], CONTRACT_1[key], key)
        self.assertEqual([output["name"] for output in CONTRACT_2["outputs"]], [output["name"] for output in CONTRACT_1["outputs"]])
        by_outcome: dict[str, set[str]] = {}
        for failure in CONTRACT_2["named_failures"]:
            by_outcome.setdefault(failure["outcome"], set()).add(failure["name"])
        self.assertEqual(by_outcome, {
            "startup-refusal": set(store_module.STARTUP_FAILURES),
            "retryable": set(store_module.RETRYABLE_FAILURES),
            "invariant-refusal": set(store_module.INVARIANT_FAILURES),
        })
        owns_1 = CONTRACT_1["authority"]["owns"]
        self.assertEqual(CONTRACT_2["authority"]["owns"][: len(owns_1)], owns_1)
        self.assertTrue(any("delivery cycle" in entry for entry in CONTRACT_2["authority"]["owns"][len(owns_1):]))
        self.assertIn("delivery cycle", CONTRACT_2["purpose"])
        view = next(output for output in CONTRACT_2["outputs"] if output["name"] == "coherent_view")
        self.assertIn("delivery cycle", view["description"])
        oracle = CONTRACT_2["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["python"])
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract_2(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `operational-store` |")]
        self.assertEqual(len(rows), 1)
        cells = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(cells[1], CONTRACT_2["kind"])
        self.assertEqual(cells[-1], "—")
        self.assertEqual(CONTRACT_2["dependencies"], [])


class R14LastFragmentAndCounts(StoreCase):
    def test_cycle_reports_its_last_fragment_and_chain_inputs(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1, "ingest-batch"), fragment(CYCLE, 1, 1, chain("a"), projections(2)))
        store.archive_ingest_batch(header(INSTALLATION, 2, "ingest-batch"), fragment(CYCLE, 1, 2, chain("b"), projections(1)))
        store.archive_ingest_batch(header(INSTALLATION, 3, "ingest-batch"), fragment(CYCLE, 2, 1, chain("c"), projections(1)))
        self.assertEqual(self.cycle(store, INSTALLATION, CYCLE), DeliveryCycle(
            installation_id=INSTALLATION,
            cycle_id=CYCLE,
            record_type="steps",
            cycle_kind="initial-snapshot",
            first_sequence=1,
            last_sequence=3,
            last_source_page_index=2,
            last_fragment_index=1,
            last_previous_chain=chain("c"),
            last_message_sha256=digest(envelope(INSTALLATION, 3, "ingest-batch")),
            fragment_count=3,
            record_count=4,
            completion_sequence=None,
        ))
        # The store reports facts, not verdicts: a later fragment that disagrees is still the last.
        store.archive_ingest_batch(header(INSTALLATION, 4, "ingest-batch"),
                                   fragment(CYCLE, 9, 7, chain("d"), retractions(2), record_type="weight", cycle_kind="change-cycle"))
        cycle = self.cycle(store, INSTALLATION, CYCLE)
        assert cycle is not None
        self.assertEqual(
            (cycle.record_type, cycle.cycle_kind, cycle.first_sequence, cycle.last_sequence, cycle.last_source_page_index,
             cycle.last_fragment_index, cycle.last_previous_chain, cycle.fragment_count, cycle.record_count),
            ("weight", "change-cycle", 1, 4, 9, 7, chain("d"), 4, 6),
        )

    def test_cycle_counts_fragments_and_items(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1, "ingest-batch"),
                                   fragment(CYCLE, 1, 1, chain("a"), projections(2), cycle_kind="change-cycle"))
        store.archive_ingest_batch(header(INSTALLATION, 2, "ingest-batch"),
                                   fragment(CYCLE, 2, 1, chain("b"), projections(1) + retractions(2), cycle_kind="change-cycle"))
        store.archive_ingest_batch(header(INSTALLATION, 3, "ingest-batch"),
                                   fragment(CYCLE, 3, 1, chain("c"), retractions(1), cycle_kind="change-cycle"))
        cycle = self.cycle(store, INSTALLATION, CYCLE)
        assert cycle is not None
        self.assertEqual((cycle.fragment_count, cycle.record_count), (3, 6))
        self.assertEqual((cycle.first_sequence, cycle.last_sequence, cycle.cycle_kind), (1, 3, "change-cycle"))

    def test_interleaved_messages_do_not_change_a_cycle(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1, "ingest-batch"), fragment(CYCLE, 1, 1, chain("a"), projections(2)))
        store.archive_ingest_batch(header(INSTALLATION, 2, "ingest-batch"),
                                   fragment(OTHER_CYCLE, 1, 1, chain("x"), projections(5), record_type="weight", cycle_kind="change-cycle"))
        store.archive_sync_checkpoint(header(INSTALLATION, 3, "sync-checkpoint"), checkpoint(1))
        store.archive_capability_acknowledgement(header(INSTALLATION, 4, "capability-acknowledgement"))
        store.archive_completion(header(INSTALLATION, 5, "snapshot-completion"), completion(OTHER_CYCLE, "weight"))
        store.archive_ingest_batch(header(INSTALLATION, 6, "ingest-batch"), fragment(CYCLE, 1, 2, chain("b"), projections(1)))
        store.archive_ingest_batch(header(INSTALLATION, 7, "ingest-batch"),
                                   fragment(THIRD_CYCLE, 1, 1, chain("y"), projections(3),
                                            record_type="weight", cycle_kind="change-cycle"))
        self.assertEqual(self.cycle(store, INSTALLATION, CYCLE), DeliveryCycle(
            installation_id=INSTALLATION,
            cycle_id=CYCLE,
            record_type="steps",
            cycle_kind="initial-snapshot",
            first_sequence=1,
            last_sequence=6,
            last_source_page_index=1,
            last_fragment_index=2,
            last_previous_chain=chain("b"),
            last_message_sha256=digest(envelope(INSTALLATION, 6, "ingest-batch")),
            fragment_count=2,
            record_count=3,
            completion_sequence=None,
        ))


class R15AbsenceCompletionAndScope(StoreCase):
    def test_unknown_cycle_or_installation_is_none(self) -> None:
        store = self.ready()
        self.assertIsNone(self.cycle(store, INSTALLATION, CYCLE))
        self.assertIsNone(self.cycle(store, OTHER, CYCLE))
        store.archive_ingest_batch(header(INSTALLATION, 1, "ingest-batch"), fragment(OTHER_CYCLE, 1, 1, chain("a"), projections(1)))
        self.assertIsNone(self.cycle(store, INSTALLATION, CYCLE))
        self.assertIsNone(self.cycle(store, OTHER, OTHER_CYCLE))

    def test_completion_without_fragments(self) -> None:
        store = self.ready()
        store.archive_completion(header(INSTALLATION, 1, "snapshot-completion"), completion(CYCLE, "heart-rate"))
        self.assertEqual(self.cycle(store, INSTALLATION, CYCLE), DeliveryCycle(
            installation_id=INSTALLATION,
            cycle_id=CYCLE,
            record_type="heart-rate",
            cycle_kind=None,
            first_sequence=None,
            last_sequence=None,
            last_source_page_index=0,
            last_fragment_index=0,
            last_previous_chain=None,
            last_message_sha256=None,
            fragment_count=0,
            record_count=0,
            completion_sequence=1,
        ))

    def test_completed_cycle_reports_its_completion(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1, "ingest-batch"), fragment(CYCLE, 1, 1, chain("a"), projections(2)))
        store.archive_ingest_batch(header(INSTALLATION, 2, "ingest-batch"), fragment(CYCLE, 1, 2, chain("b"), projections(1)))
        before = self.cycle(store, INSTALLATION, CYCLE)
        assert before is not None
        self.assertIsNone(before.completion_sequence)
        store.archive_completion(header(INSTALLATION, 4, "snapshot-completion"), completion(CYCLE))
        after = self.cycle(store, INSTALLATION, CYCLE)
        assert after is not None
        self.assertEqual(after.completion_sequence, 4)
        self.assertEqual(
            {field.name: getattr(after, field.name) for field in fields(after) if field.name != "completion_sequence"},
            {field.name: getattr(before, field.name) for field in fields(before) if field.name != "completion_sequence"},
        )

    def test_cycles_are_scoped_to_their_installation(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1, "ingest-batch"), fragment(CYCLE, 1, 1, chain("a"), projections(2)))
        store.archive_ingest_batch(header(INSTALLATION, 2, "ingest-batch"), fragment(CYCLE, 1, 2, chain("b"), projections(1)))
        first = self.cycle(store, INSTALLATION, CYCLE)
        store.begin_draining(INSTALLATION, at(10))
        store.retire_installation(INSTALLATION, "drained", at(11))
        self.activate(store, OTHER, minute=12)
        store.archive_ingest_batch(header(OTHER, 1, "ingest-batch"),
                                   fragment(CYCLE, 1, 1, chain("z"), retractions(4), record_type="weight", cycle_kind="change-cycle"))
        store.archive_completion(header(OTHER, 2, "change-completion"),
                                 CompletionRecord("change", None, "weight", CYCLE, 1, 1, 4, chain("final")))
        self.assertEqual(self.cycle(store, INSTALLATION, CYCLE), first)
        other = self.cycle(store, OTHER, CYCLE)
        assert other is not None
        self.assertEqual(
            (other.record_type, other.cycle_kind, other.fragment_count, other.record_count, other.last_previous_chain,
             other.completion_sequence, other.first_sequence),
            ("weight", "change-cycle", 1, 4, chain("z"), 2, 1),
        )


class R16CoherentDurableAndHealthDataFree(StoreCase):
    def test_cycle_read_is_isolated_from_later_commits(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1, "ingest-batch"), fragment(CYCLE, 1, 1, chain("a"), projections(1)))
        with store.read() as view:
            before = view.delivery_cycle(INSTALLATION, CYCLE)
            store.archive_ingest_batch(header(INSTALLATION, 2, "ingest-batch"), fragment(CYCLE, 1, 2, chain("b"), projections(2)))
            store.archive_completion(header(INSTALLATION, 3, "snapshot-completion"), completion(CYCLE))
            self.assertEqual(view.delivery_cycle(INSTALLATION, CYCLE), before)
            self.assertIsNone(view.delivery_cycle(INSTALLATION, OTHER_CYCLE))
        assert before is not None
        self.assertEqual((before.fragment_count, before.record_count, before.completion_sequence), (1, 1, None))
        after = self.cycle(store, INSTALLATION, CYCLE)
        assert after is not None
        self.assertEqual((after.fragment_count, after.record_count, after.last_sequence, after.completion_sequence), (2, 3, 2, 3))

    def test_cycle_read_survives_reopen(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1, "ingest-batch"), fragment(CYCLE, 1, 1, chain("a"), projections(2)))
        store.archive_ingest_batch(header(INSTALLATION, 2, "ingest-batch"), fragment(CYCLE, 2, 1, chain("b"), retractions(1)))
        store.archive_completion(header(INSTALLATION, 3, "snapshot-completion"), completion(CYCLE))
        before = self.cycle(store, INSTALLATION, CYCLE)
        store.close()
        reopened = self.open()
        self.assertEqual(self.cycle(reopened, INSTALLATION, CYCLE), before)

    def test_cycle_read_is_health_data_free(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1, "ingest-batch"),
                                   fragment(CYCLE, 1, 1, chain("a"), projections(2, HEALTH_MARKER) + retractions(2, IDENTITY_MARKER)))
        cycle = self.cycle(store, INSTALLATION, CYCLE)
        assert cycle is not None
        for field in fields(cycle):
            value = getattr(cycle, field.name)
            self.assertIsInstance(value, (str, int, type(None)), field.name)
            self.assertNotIsInstance(value, bool, field.name)
        text = repr(cycle)
        self.assertNotIn(HEALTH_MARKER.decode(), text)
        self.assertNotIn(IDENTITY_MARKER, text)


class R17NoSchemaChange(StoreCase):
    def test_version_2_keeps_the_baseline_schema(self) -> None:
        self.assertEqual([version for version, _sql in store_module.MIGRATIONS], [1])
        self.assertEqual(digest(store_module.MIGRATIONS[0][1].encode("utf-8")), BASELINE_CHECKSUM)

    def test_version_1_database_answers_cycle_reads(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1, "ingest-batch"), fragment(CYCLE, 1, 1, chain("a"), projections(3)))
        store.close()
        connection = sqlite3.connect(self.path)
        try:
            recorded = connection.execute("SELECT version, checksum FROM store_migrations ORDER BY version").fetchall()
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual((recorded, user_version), ([(1, BASELINE_CHECKSUM)], 1))
        reopened = self.open()
        cycle = self.cycle(reopened, INSTALLATION, CYCLE)
        assert cycle is not None
        self.assertEqual((cycle.fragment_count, cycle.record_count, cycle.last_previous_chain), (1, 3, chain("a")))
        self.assertEqual(reopened.settings().schema_migrations, ((1, BASELINE_CHECKSUM),))


if __name__ == "__main__":
    unittest.main()
