"""Acceptance tests for `reconcile-source-record@1`.

Requirement numbers refer to `packs/reconcile-source-record-1.spec.md`. `decide`
tests build `operational-store@3` `SourceRecord` values directly; `reconcile`
tests use real temporary stores built through the store's own operations, with
synthetic projections and hand-written index entries.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from activity_tracker_portal.operational_store import (
    BatchItem,
    Evidence,
    IngestBatchRecord,
    ItemPosition,
    MessageHeader,
    ProjectionEntry,
    RetractionEntry,
    Selection,
    SourceKey,
    SourceRecord,
    open_store,
)
from activity_tracker_portal.reconcile_source_record import (
    Decision,
    ReconcileRefusal,
    ReconcileResult,
    ReconcileRetryable,
    decide,
    reconcile,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/reconcile-source-record.1.json").read_text(encoding="utf-8"))
EFFECTS = json.loads((ROOT / "packs/reconcile-source-record-1.effects.json").read_text(encoding="utf-8"))
SCOPE_MAP = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PST = timezone(timedelta(hours=-8))
T0 = datetime(2026, 11, 24, 9, 0, 0, 123456, tzinfo=UTC)
RESERVE = 1_000_000
PLENTY = 10**12
INSTALLATION = "0f8fad5b-d9cb-469f-a165-70867728950e"
OTHER = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
CYCLE = "e3b0c442-98fc-4c14-9afb-f4c8996fb924"
CHANGES = "5d41402a-bc4b-4a76-b971-9d911017c592"
HC = "health-connect"
ORIGIN = "org.example.synthetic.source"
HEALTH_MARKER = "synthetic-health-marker"
IDENTITY = "synthetic-identity-marker"
KEY = SourceKey(HC, ORIGIN, "steps", IDENTITY)


def at(minutes: float, zone: timezone = UTC) -> datetime:
    return (T0 + timedelta(minutes=minutes)).astimezone(zone)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content(label: str) -> str:
    return digest(b"synthetic-content-" + label.encode())


# ---- SourceRecord values for decide ----

class Builder:
    """Evidence in delivery order; identities are assigned from a supplied sequence so tests can permute them."""

    def __init__(self) -> None:
        self.rows: list[Evidence] = []

    def projection(self, minute: float, label: str, *, installation: str = INSTALLATION, zone: timezone = UTC,
                   cycle: str = CYCLE, sequence: int | None = None) -> "Builder":
        index = len(self.rows)
        self.rows.append(Evidence(
            evidence_id=index + 1, kind="projection", position=ItemPosition(installation, sequence or index + 1, 0),
            cycle_kind="initial-snapshot", cycle_id=cycle, last_modified_time=at(minute, zone),
            projection_sha256=content(label), indexed_at=at(500 + index)))
        return self

    def retraction(self, *, installation: str = INSTALLATION, sequence: int | None = None) -> "Builder":
        index = len(self.rows)
        self.rows.append(Evidence(
            evidence_id=index + 1, kind="retraction", position=ItemPosition(installation, sequence or index + 1, 0),
            cycle_kind="change-cycle", cycle_id=CHANGES, last_modified_time=None, projection_sha256=None,
            indexed_at=at(500 + index)))
        return self

    def record(self, *, ids: list[int] | None = None, selections: tuple[Selection, ...] = ()) -> SourceRecord:
        rows = self.rows if ids is None else [replace(row, evidence_id=ids[index]) for index, row in enumerate(self.rows)]
        return SourceRecord(
            record_id=7, key=KEY, evidence=tuple(rows), evidence_head=max(row.evidence_id for row in rows),
            observing_installations=tuple(sorted({row.position.installation_id for row in rows})), selections=selections)


def ids(record: SourceRecord, *indexes: int) -> tuple[int, ...]:
    return tuple(record.evidence[index].evidence_id for index in indexes)


class R1GovernedScopeContract(unittest.TestCase):
    def test_contract_shape_and_dependency(self) -> None:
        self.assertEqual((CONTRACT["scope"], CONTRACT["version"], CONTRACT["kind"]), ("reconcile-source-record", "1", "leaf"))
        self.assertEqual(CONTRACT["dependencies"], [{"scope": "operational-store", "version": "3"}])
        self.assertEqual({failure["name"]: failure["outcome"] for failure in CONTRACT["named_failures"]}, {
            "source-record-unknown": "invariant-refusal",
            "evidence-moved": "retryable",
            "storage-reserve-exhausted": "retryable",
            "store-busy": "retryable",
        })
        self.assertEqual([effect["name"] for effect in CONTRACT["observable_effects"]], ["source-selection-write"])
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_reconcile_source_record_1.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_reconcile_source_record_1.sh")

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        row = next(line for line in SCOPE_MAP.splitlines() if line.startswith("| `reconcile-source-record` |"))
        self.assertIn("| leaf |", row)
        self.assertIn("arrival order", row)
        self.assertIn("`operational-store`", row)

    def test_effects_declaration_matches_contract(self) -> None:
        self.assertEqual(EFFECTS["effects_format"], 1)
        (effect,) = EFFECTS["effects"]
        self.assertEqual((effect["kind"], effect["name"], effect["outcome"]), ("action", "source-selection-write", "confirmed"))


class R2SourceModificationTimeOrdersProjections(unittest.TestCase):
    def test_newest_projection_wins(self) -> None:
        record = Builder().projection(1, "a").projection(5, "b").projection(3, "c").record()
        self.assertEqual(decide(record), Decision("projection", ids(record, 1)))

    def test_later_delivered_older_projection_does_not_win(self) -> None:
        # A change cycle delivered the newer version first; a late snapshot page delivers the older one.
        record = Builder().projection(9, "new").projection(2, "old").record()
        self.assertEqual(decide(record), Decision("projection", ids(record, 0)))

    def test_installation_does_not_affect_the_choice(self) -> None:
        one = Builder().projection(1, "a").projection(5, "b", installation=OTHER).record()
        two = Builder().projection(1, "a", installation=OTHER).projection(5, "b").record()
        self.assertEqual(decide(one), Decision("projection", ids(one, 1)))
        self.assertEqual(decide(two), Decision("projection", ids(two, 1)))

    def test_times_compare_as_instants_across_zones(self) -> None:
        record = Builder().projection(5, "later", zone=PST).projection(4, "earlier", zone=IST).record()
        self.assertEqual(decide(record), Decision("projection", ids(record, 0)))


class R3SameContentIsOneAssertion(unittest.TestCase):
    def test_identical_reobservation_is_not_a_conflict(self) -> None:
        record = Builder().projection(5, "a").projection(5, "a", installation=OTHER).record()
        self.assertEqual(decide(record).outcome, "projection")

    def test_identical_reobservation_names_the_earliest_delivered(self) -> None:
        record = Builder().projection(2, "old").projection(5, "a").projection(5, "a", zone=IST).projection(5, "a", installation=OTHER).record()
        self.assertEqual(decide(record), Decision("projection", ids(record, 1)))


class R4DifferentContentAtTheSameTimeConflicts(unittest.TestCase):
    def test_different_content_at_the_newest_time_conflicts(self) -> None:
        record = Builder().projection(5, "a").projection(5, "b").record()
        self.assertEqual(decide(record), Decision("conflict", ids(record, 0, 1)))

    def test_conflict_names_one_row_per_distinct_content(self) -> None:
        record = Builder().projection(5, "b").projection(1, "old").projection(5, "a").projection(5, "b").projection(5, "c").projection(5, "a").record()
        self.assertEqual(decide(record), Decision("conflict", ids(record, 0, 2, 4)))

    def test_older_conflicting_content_is_not_a_conflict(self) -> None:
        record = Builder().projection(3, "a").projection(3, "b").projection(4, "c").record()
        self.assertEqual(decide(record), Decision("projection", ids(record, 2)))

    def test_newer_projection_ends_a_conflict(self) -> None:
        record = Builder().projection(5, "a").projection(5, "b").projection(6, "c").record()
        self.assertEqual(decide(record), Decision("projection", ids(record, 2)))


class R5Retractions(unittest.TestCase):
    def test_retraction_after_projection_retracts(self) -> None:
        record = Builder().projection(5, "a").retraction().record()
        self.assertEqual(decide(record), Decision("retraction", ids(record, 1)))

    def test_reprojection_after_retraction_with_newer_time_wins(self) -> None:
        record = Builder().projection(5, "a").retraction().projection(6, "b").record()
        self.assertEqual(decide(record), Decision("projection", ids(record, 2)))

    def test_stale_projection_after_retraction_does_not_revive(self) -> None:
        for minute, label in ((5, "a"), (5, "b"), (4, "a")):
            with self.subTest(minute=minute, label=label):
                record = Builder().projection(5, "a").retraction(installation=OTHER).projection(minute, label, installation=OTHER).record()
                self.assertEqual(decide(record), Decision("retraction", ids(record, 1)))

    def test_projection_after_an_earlier_retraction_wins(self) -> None:
        record = Builder().retraction().projection(1, "a").record()
        self.assertEqual(decide(record), Decision("projection", ids(record, 1)))

    def test_latest_effective_retraction_is_named(self) -> None:
        later = Builder().projection(5, "a").retraction().projection(7, "b").retraction().record()
        self.assertEqual(decide(later), Decision("retraction", ids(later, 3)))
        earliest = Builder().projection(5, "a").retraction().retraction().record()
        self.assertEqual(decide(earliest), Decision("retraction", ids(earliest, 1)))
        superseded = Builder().projection(5, "a").retraction().projection(7, "b").record()
        self.assertEqual(decide(superseded), Decision("projection", ids(superseded, 2)))

    def test_effective_time_is_the_newest_preceding_projection(self) -> None:
        # The newest preceding time, not the last delivered one.
        record = Builder().projection(9, "a").projection(2, "b").retraction().record()
        self.assertEqual(decide(record), Decision("retraction", ids(record, 2)))
        record = Builder().projection(9, "a").projection(2, "b").retraction().projection(5, "c").record()
        self.assertEqual(decide(record), Decision("retraction", ids(record, 2)))

    def test_stale_projection_between_retractions_keeps_the_earliest_named(self) -> None:
        record = Builder().projection(5, "a").retraction().projection(3, "b").retraction().record()
        self.assertEqual(decide(record), Decision("retraction", ids(record, 1)))

    def test_retraction_supersedes_a_conflict_it_follows(self) -> None:
        record = Builder().projection(5, "a").projection(5, "b").retraction().record()
        self.assertEqual(decide(record), Decision("retraction", ids(record, 2)))


class R6DecideIsPureAndDeterministic(unittest.TestCase):
    def test_decide_ignores_installation_cycle_and_selection(self) -> None:
        base = Builder().projection(5, "a").projection(5, "b").retraction().projection(8, "c").record()
        varied = replace(base, observing_installations=(OTHER,), selections=(
            Selection(selection_id=1, outcome="retraction", evidence_ids=ids(base, 2), evidence_head=base.evidence_head, recorded_at=at(1)),),
            evidence=tuple(replace(row, position=ItemPosition(OTHER, row.position.sequence + 100, 3), cycle_id=CHANGES,
                                   indexed_at=at(900)) for row in base.evidence))
        self.assertEqual(decide(varied), decide(base))

    def test_decide_uses_delivery_order_not_identities(self) -> None:
        builder = Builder().projection(5, "a").retraction().projection(5, "a").projection(5, "b")
        record = builder.record(ids=[40, 10, 30, 20])
        # The retraction follows the first projection only; later projections at the same time do not revive.
        self.assertEqual(decide(record), Decision("retraction", (10,)))
        record = Builder().projection(5, "a").projection(5, "b").record(ids=[9, 3])
        self.assertEqual(decide(record), Decision("conflict", (9, 3)))

    def test_decide_ignores_positions_out_of_delivery_order(self) -> None:
        # Installation A delivered at sequence 20, then installation B at sequence 1.
        record = Builder().projection(5, "a", sequence=20).projection(5, "b", installation=OTHER, sequence=1).record()
        self.assertEqual(decide(record), Decision("conflict", ids(record, 0, 1)))
        record = Builder().projection(5, "a", sequence=20).retraction(installation=OTHER, sequence=1).record()
        self.assertEqual(decide(record), Decision("retraction", ids(record, 1)))
        record = Builder().retraction(sequence=30).projection(1, "a", installation=OTHER, sequence=2).record()
        self.assertEqual(decide(record), Decision("projection", ids(record, 1)))

    def test_decide_has_no_store_or_clock_dependency(self) -> None:
        record = Builder().projection(5, "a").retraction().projection(6, "b").record()
        first = decide(record)
        self.assertEqual([decide(record) for _ in range(3)], [first] * 3)
        self.assertEqual(first, Decision("projection", ids(record, 2)))


# ---- reconcile against real stores ----

def envelope(installation_id: str, sequence: int) -> bytes:
    return json.dumps({"synthetic": True, "installation": installation_id, "sequence": sequence}, separators=(",", ":")).encode()


def header(installation_id: str, sequence: int) -> MessageHeader:
    data = envelope(installation_id, sequence)
    return MessageHeader(installation_id=installation_id, sequence=sequence, message_sha256=digest(data), staged_generation=1,
                         receipt_time=at(sequence), envelope=data, receipt=b'{"synthetic-receipt":%d}' % sequence)


def batch(*items: BatchItem, cycle_kind: str = "initial-snapshot", page: int = 1) -> IngestBatchRecord:
    return IngestBatchRecord(record_type="steps", projection_schema_version=1, cycle_kind=cycle_kind,  # type: ignore[arg-type]
                             cycle_id=CYCLE if cycle_kind == "initial-snapshot" else CHANGES, source_page_index=page,
                             fragment_index=1, previous_chain=digest(b"synthetic-chain-%d" % page), items=items)


def projection(label: str) -> BatchItem:
    return BatchItem(projection=('{"%s":"%s"}' % (HEALTH_MARKER, label)).encode())


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


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "portal.sqlite3"
        self.free = PLENTY
        self.store = open_store(self.path, reserve_bytes=RESERVE, free_bytes=lambda _path: self.free, busy_timeout_ms=200)
        self.addCleanup(self.store.close)
        offer = digest(b"synthetic-enrollment-code")
        self.store.record_enrollment_offer(offer, at(30), at(0))
        self.store.enroll_installation(offer, INSTALLATION, digest(b"synthetic-fingerprint"), at(0))
        self.store.activate_installation(INSTALLATION, at(0))
        self.store.honor_generation(1, b'{"synthetic-advertisement":1}', T0)
        self.sequence = 0

    def deliver(self, *items: tuple[str, float, str] | str, cycle_kind: str = "initial-snapshot") -> None:
        """Archive one batch and index it: ("p", minute, label) projects KEY; "r" retracts it."""
        self.sequence += 1
        archived = tuple(projection(item[2]) if item != "r" else BatchItem(retracted_identity=IDENTITY) for item in items)
        self.store.archive_ingest_batch(header(INSTALLATION, self.sequence), batch(*archived, cycle_kind=cycle_kind, page=self.sequence))
        entries = []
        for index, item in enumerate(items):
            position = ItemPosition(INSTALLATION, self.sequence, index)
            if item == "r":
                entries.append(RetractionEntry(position))
            else:
                entries.append(ProjectionEntry(position, HC, ORIGIN, IDENTITY, at(item[1])))
        self.store.index_items(tuple(entries), at(100 + self.sequence))

    def record(self) -> SourceRecord:
        with self.store.read() as view:
            record_id = view.source_record_id(KEY)
            assert record_id is not None
            found = view.source_record(record_id)
        assert found is not None
        return found

    def assertNothingRecorded(self, error: type[Exception], failure: str, operation) -> None:
        before = contents(self.path)
        with self.assertRaises(error) as caught:
            operation()
        self.assertEqual(caught.exception.failure, failure)  # type: ignore[attr-defined]
        self.assertEqual(contents(self.path), before)
        for text in (str(caught.exception), repr(caught.exception)):
            self.assertNotIn(HEALTH_MARKER, text)
            self.assertNotIn(IDENTITY, text)


class R7RecordingAndUnchangedState(StoreCase):
    def test_reconcile_records_the_decision(self) -> None:
        self.deliver(("p", 1, "a"), ("p", 5, "b"))
        record = self.record()
        result = reconcile(self.store, record.record_id, at(200))
        self.assertIsInstance(result, ReconcileResult)
        self.assertEqual(result.record_id, record.record_id)
        self.assertEqual(result.decision, Decision("projection", (record.evidence[1].evidence_id,)))
        self.assertFalse(result.unchanged)
        self.assertEqual((result.selection.outcome, result.selection.evidence_ids, result.selection.evidence_head, result.selection.recorded_at),
                         ("projection", result.decision.evidence_ids, record.evidence_head, at(200)))
        self.assertEqual(self.record().selections, (result.selection,))

    def test_reconcile_reports_unchanged_and_still_advances(self) -> None:
        self.deliver(("p", 5, "a"))
        first = reconcile(self.store, self.record().record_id, at(200))
        self.deliver(("p", 5, "a"), cycle_kind="change-cycle")
        record = self.record()
        with self.store.read() as view:
            self.assertIn(record.record_id, view.records_awaiting_selection(10))
        second = reconcile(self.store, record.record_id, at(201))
        self.assertTrue(second.unchanged)
        self.assertEqual(second.decision, first.decision)
        self.assertEqual(second.selection.evidence_head, record.evidence_head)
        self.assertNotEqual(second.selection.selection_id, first.selection.selection_id)
        with self.store.read() as view:
            self.assertNotIn(record.record_id, view.records_awaiting_selection(10))
        self.deliver("r", cycle_kind="change-cycle")
        third = reconcile(self.store, record.record_id, at(202))
        self.assertFalse(third.unchanged)
        self.assertEqual(third.decision.outcome, "retraction")

    def test_same_outcome_with_different_evidence_is_changed(self) -> None:
        self.deliver(("p", 5, "a"))
        record_id = self.record().record_id
        first = reconcile(self.store, record_id, at(200))
        self.deliver(("p", 6, "b"), cycle_kind="change-cycle")
        second = reconcile(self.store, record_id, at(201))
        self.assertEqual((first.decision.outcome, second.decision.outcome), ("projection", "projection"))
        self.assertNotEqual(second.decision.evidence_ids, first.decision.evidence_ids)
        self.assertFalse(second.unchanged)

    def test_identical_reconcile_writes_nothing(self) -> None:
        self.deliver(("p", 5, "a"), ("p", 5, "b"))
        record = self.record()
        first = reconcile(self.store, record.record_id, at(200))
        before = contents(self.path)
        again = reconcile(self.store, record.record_id, at(300))
        self.assertTrue(again.unchanged)
        self.assertEqual(again.selection, first.selection)
        self.assertEqual(again.decision, first.decision)
        self.assertEqual(contents(self.path), before)

    def test_reconcile_writes_only_one_selection(self) -> None:
        self.deliver(("p", 5, "a"))
        self.deliver("r", cycle_kind="change-cycle")
        record = self.record()
        # A control copy of the database, taken before reconcile.
        control_path = self.path.with_name("control.sqlite3")
        source = sqlite3.connect(self.path)
        target = sqlite3.connect(control_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        result = reconcile(self.store, record.record_id, at(200))
        # Reconcile's whole effect equals the one selection write, made directly on the control copy.
        control = open_store(control_path, reserve_bytes=RESERVE, free_bytes=lambda _path: PLENTY)
        try:
            control.record_selection(record.record_id, result.decision.outcome, result.decision.evidence_ids,
                                     record.evidence_head, at(200))
        finally:
            control.close()
        self.assertEqual(contents(self.path), contents(control_path))
        found = self.record()
        self.assertEqual(found.evidence, record.evidence)
        self.assertEqual(found.selections, (result.selection,))

class MovingStore:
    """Delegates to a real store, but indexes newer evidence just before the selection is written."""

    def __init__(self, store, inject) -> None:
        self._store = store
        self._inject = inject

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    def record_selection(self, *args, **kwargs):
        self._inject()
        return self._store.record_selection(*args, **kwargs)


class R8Failures(StoreCase):
    def test_unknown_record_is_refused(self) -> None:
        self.deliver(("p", 5, "a"))
        self.assertNothingRecorded(ReconcileRefusal, "source-record-unknown", lambda: reconcile(self.store, 999_999, at(200)))

    def test_evidence_moved_is_retryable_and_writes_nothing(self) -> None:
        self.deliver(("p", 5, "a"))
        record = self.record()
        expected: dict[str, object] = {}

        def inject() -> None:
            self.deliver(("p", 9, "b"), cycle_kind="change-cycle")
            expected.update(contents(self.path))

        moving = MovingStore(self.store, inject)
        with self.assertRaises(ReconcileRetryable) as caught:
            reconcile(moving, record.record_id, at(200))  # type: ignore[arg-type]
        self.assertEqual(caught.exception.failure, "evidence-moved")
        # The failed reconcile wrote nothing; only the concurrent indexing is present.
        self.assertEqual(contents(self.path), expected)
        for text in (str(caught.exception), repr(caught.exception)):
            self.assertNotIn(HEALTH_MARKER, text)
            self.assertNotIn(IDENTITY, text)
        found = self.record()
        self.assertEqual(found.selections, ())
        self.assertEqual(len(found.evidence), 2)
        self.assertEqual(reconcile(self.store, record.record_id, at(201)).decision,
                         Decision("projection", (found.evidence[1].evidence_id,)))

    def test_reserve_and_busy_are_retryable(self) -> None:
        self.deliver(("p", 5, "a"))
        record_id = self.record().record_id
        self.free = RESERVE - 1
        self.assertNothingRecorded(ReconcileRetryable, "storage-reserve-exhausted", lambda: reconcile(self.store, record_id, at(200)))
        self.free = PLENTY
        before = contents(self.path)
        blocker = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(ReconcileRetryable) as caught:
                reconcile(self.store, record_id, at(200))
            self.assertEqual(caught.exception.failure, "store-busy")
        finally:
            blocker.execute("ROLLBACK")
        self.assertEqual(contents(self.path), before)
        for text in (str(caught.exception), repr(caught.exception)):
            self.assertNotIn(HEALTH_MARKER, text)
            self.assertNotIn(IDENTITY, text)
        self.assertEqual(self.record().selections, ())

    def test_failures_are_health_data_free(self) -> None:
        self.deliver(("p", 5, "a"))
        record_id = self.record().record_id
        for error, failure, operation in (
            (ReconcileRefusal, "source-record-unknown", lambda: reconcile(self.store, record_id + 1, at(200))),
            (ReconcileRetryable, "storage-reserve-exhausted", lambda: (setattr(self, "free", 1), reconcile(self.store, record_id, at(200)))),
        ):
            with self.subTest(failure):
                self.assertNothingRecorded(error, failure, operation)
        self.free = PLENTY
        result = reconcile(self.store, record_id, at(201))
        for text in (repr(result), repr(result.decision)):
            self.assertNotIn(HEALTH_MARKER, text)
            self.assertNotIn(IDENTITY, text)


if __name__ == "__main__":
    unittest.main()
