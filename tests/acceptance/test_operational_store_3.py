"""Acceptance tests for `operational-store@3`.

Requirement numbers refer to `packs/operational-store-3.spec.md`. `@1` and
`@2`'s requirements R1-R17 stay bound to their suites, which the `@3` runner
re-runs through `@2`'s bound runner. Every database lives in a temporary
directory and every value is synthetic. Index entries are hand-written
extractions: the store never checks them against the projection bytes
(Decision 1). Like the earlier suites, this one never imports
`ingest_protocol`.
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
    Evidence,
    IndexCounts,
    IndexResult,
    IngestBatchRecord,
    ItemPosition,
    MessageHeader,
    PendingItem,
    ProjectionEntry,
    RetractionEntry,
    Selection,
    SourceKey,
    SourceRecord,
    StoreRefusal,
    StoreRetryable,
    SyncCheckpointRecord,
    RecordTypeOutcome,
    open_store,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_2 = json.loads((ROOT / "governance/scopes/operational-store.2.json").read_text(encoding="utf-8"))
CONTRACT_3 = json.loads((ROOT / "governance/scopes/operational-store.3.json").read_text(encoding="utf-8"))
SCOPE_MAP = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
FIXTURE = ROOT / "tests/acceptance/fixtures/operational-store-1-archive.sql"

UTC = timezone.utc
T0 = datetime(2026, 11, 24, 9, 0, 0, 123456, tzinfo=UTC)
RESERVE = 1_000_000
PLENTY = 10**12
INSTALLATION = "0f8fad5b-d9cb-469f-a165-70867728950e"
OTHER = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
CYCLE = "e3b0c442-98fc-4c14-9afb-f4c8996fb924"
OTHER_CYCLE = "5d41402a-bc4b-4a76-b971-9d911017c592"
HC = "health-connect"
ORIGIN = "org.example.synthetic.source"
OTHER_ORIGIN = "org.example.synthetic.other"
HEALTH_MARKER = b"synthetic-health-marker"
IDENTITY_MARKER = "synthetic-identity-marker"
NEW_FAILURES = (
    "archived-item-unknown",
    "index-entry-mismatch",
    "index-entry-conflict",
    "source-key-invalid",
    "source-record-unknown",
    "selection-evidence-invalid",
    "selection-stale",
)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def envelope(installation_id: str, sequence: int, kind: str) -> bytes:
    body = {"synthetic": True, "kind": kind, "installation": installation_id, "sequence": sequence}
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode()


def header(installation_id: str, sequence: int, kind: str = "ingest-batch") -> MessageHeader:
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


def projection(label: str) -> BatchItem:
    return BatchItem(projection=b'{"synthetic-projection":"' + label.encode() + b'"}')


def retraction(identity: str) -> BatchItem:
    return BatchItem(retracted_identity=identity)


def batch(*items: BatchItem, record_type: str = "steps", cycle_id: str = CYCLE,
          cycle_kind: str = "initial-snapshot", page: int = 1, index: int = 1) -> IngestBatchRecord:
    return IngestBatchRecord(
        record_type=record_type,
        projection_schema_version=1,
        cycle_kind=cycle_kind,  # type: ignore[arg-type]
        cycle_id=cycle_id,
        source_page_index=page,
        fragment_index=index,
        previous_chain=digest(b"synthetic-chain-%d-%d" % (page, index)),
        items=items,
    )


def position(sequence: int, item_index: int, installation_id: str = INSTALLATION) -> ItemPosition:
    return ItemPosition(installation_id=installation_id, sequence=sequence, item_index=item_index)


def entry(sequence: int, item_index: int, identity: str, *, origin: str = ORIGIN, minute: float = 0,
          installation_id: str = INSTALLATION, kind: str = HC) -> ProjectionEntry:
    return ProjectionEntry(
        position=position(sequence, item_index, installation_id),
        source_system_kind=kind,
        origin=origin,
        source_identity=identity,
        last_modified_time=at(minute),
    )


def gone(sequence: int, item_index: int, installation_id: str = INSTALLATION) -> RetractionEntry:
    return RetractionEntry(position=position(sequence, item_index, installation_id))


def key(identity: str, *, origin: str = ORIGIN, record_type: str = "steps", kind: str = HC) -> SourceKey:
    return SourceKey(source_system_kind=kind, origin=origin, record_type=record_type, source_identity=identity)


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


def baseline_tables() -> set[str]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(store_module.MIGRATIONS[0][1])
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
    finally:
        connection.close()


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "portal.sqlite3"
        self.free = PLENTY

    def open(self):
        store = open_store(self.path, reserve_bytes=RESERVE, free_bytes=lambda _path: self.free)
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

    def handover(self, store) -> None:
        """Drain and retire INSTALLATION, then activate OTHER, so both have observed records."""
        store.begin_draining(INSTALLATION, at(100))
        store.retire_installation(INSTALLATION, "drained", at(101))
        self.activate(store, OTHER, 102)

    def pending(self, store, limit: int = 1000) -> tuple[PendingItem, ...]:
        with store.read() as view:
            return view.pending_items(limit)

    def record(self, store, source_key: SourceKey) -> SourceRecord:
        with store.read() as view:
            record_id = view.source_record_id(source_key)
            self.assertIsNotNone(record_id, source_key)
            found = view.source_record(record_id)
        assert found is not None
        return found

    def assertRefused(self, failure: str, operation, *args, **kwargs) -> None:
        before = contents(self.path)
        with self.assertRaises(StoreRefusal) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.failure, failure)
        self.assertEqual(contents(self.path), before, f"{failure} changed the database")

    def assertRetryable(self, failure: str, operation, *args, **kwargs) -> None:
        before = contents(self.path)
        with self.assertRaises(StoreRetryable) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.failure, failure)
        self.assertEqual(contents(self.path), before, f"{failure} changed the database")

    def two_records(self):
        """One batch: projections of identities a and b, then a second batch retracting a."""
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(projection("a"), projection("b")))
        store.archive_ingest_batch(header(INSTALLATION, 2), batch(retraction("id-a"), cycle_kind="change-cycle", cycle_id=OTHER_CYCLE))
        store.index_items((entry(1, 0, "id-a", minute=1), entry(1, 1, "id-b", minute=2), gone(2, 0)), at(10))
        return store


class R18GovernedScopeContractVersion3(unittest.TestCase):
    def test_contract_3_extends_version_2(self) -> None:
        self.assertEqual(CONTRACT_3["scope"], "operational-store")
        self.assertEqual(CONTRACT_3["version"], "3")
        self.assertEqual(CONTRACT_3["kind"], CONTRACT_2["kind"])
        for name in ("observable_effects", "dependencies"):
            self.assertEqual(CONTRACT_3[name], CONTRACT_2[name], name)
        self.assertEqual([(item["name"], item["mode"]) for item in CONTRACT_3["inputs"]],
                         [(item["name"], item["mode"]) for item in CONTRACT_2["inputs"]])
        two = CONTRACT_2["named_failures"]
        self.assertEqual(CONTRACT_3["named_failures"][: len(two)], two)
        added = CONTRACT_3["named_failures"][len(two):]
        self.assertEqual(tuple(failure["name"] for failure in added), NEW_FAILURES)
        self.assertEqual({failure["outcome"] for failure in added}, {"invariant-refusal"})
        outputs = [item["name"] for item in CONTRACT_3["outputs"]]
        self.assertEqual(outputs, [item["name"] for item in CONTRACT_2["outputs"]] + ["index_result", "selection_record"])
        owns_2, owns_3 = CONTRACT_2["authority"]["owns"], CONTRACT_3["authority"]["owns"]
        self.assertEqual(owns_3[1: len(owns_2)], owns_2[1:])
        self.assertTrue(any("Source Record identity" in entry for entry in owns_3[len(owns_2):]))
        self.assertIn("Source Record", CONTRACT_3["purpose"])
        oracle = CONTRACT_3["acceptance_oracle"]
        self.assertEqual(oracle["specification"], "packs/operational-store-3.spec.md")
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_operational_store_3.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_operational_store_3.sh")

    def test_scope_map_row_does_not_contradict_contract_3(self) -> None:
        row = next(line for line in SCOPE_MAP.splitlines() if line.startswith("| `operational-store` |"))
        self.assertIn("| shared |", row)
        self.assertIn("transactional", row)
        self.assertIn("selector", row)

    def test_failures_match_contract_3(self) -> None:
        partitions = {
            "startup-refusal": set(store_module.STARTUP_FAILURES),
            "retryable": set(store_module.RETRYABLE_FAILURES),
            "invariant-refusal": set(store_module.INVARIANT_FAILURES),
        }
        for failure in CONTRACT_3["named_failures"]:
            self.assertIn(failure["name"], partitions[failure["outcome"]], failure["name"])
        self.assertLessEqual(set(NEW_FAILURES), set(store_module.FAILURES))


class R19MigrationTwoIsAdditive(StoreCase):
    def test_new_database_records_migrations_1_and_2(self) -> None:
        self.open().close()
        known = [(version, digest(sql.encode("utf-8"))) for version, sql in store_module.MIGRATIONS]
        self.assertGreaterEqual(len(known), 2)
        self.assertEqual([version for version, _ in known[:2]], [1, 2])
        state = contents(self.path)
        self.assertEqual([row[:2] for row in sorted(state["tables"]["store_migrations"])], known)
        self.assertEqual(state["user_version"], known[-1][0])

    def load_fixture(self) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.executescript(FIXTURE.read_text(encoding="utf-8"))
        finally:
            connection.close()

    def test_version_2_database_requires_migration_and_is_untouched(self) -> None:
        self.load_fixture()
        before = contents(self.path)
        self.assertEqual(before["user_version"], 1)
        with self.assertRaises(StoreRefusal) as caught:
            open_store(self.path, reserve_bytes=RESERVE, free_bytes=lambda _path: PLENTY)
        self.assertEqual(caught.exception.failure, "store-migration-required")
        self.assertEqual(contents(self.path), before)

    def test_migrated_version_2_archive_is_pending(self) -> None:
        self.load_fixture()
        connection = sqlite3.connect(self.path)
        try:
            archived = [
                (installation_id, sequence, item_index)
                for installation_id, sequence, item_index in connection.execute(
                    "SELECT i.installation_id, i.sequence, i.item_index FROM batch_items i "
                    "JOIN archived_messages m ON m.installation_id = i.installation_id AND m.sequence = i.sequence "
                    "ORDER BY m.rowid, i.item_index"
                )
            ]
            original = {name: connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
                        for name in baseline_tables() if name != "store_migrations"}
            for version, sql in store_module.MIGRATIONS[1:]:
                connection.executescript(sql)
                connection.execute(
                    "INSERT INTO store_migrations (version, checksum, applied_at) VALUES (?, ?, ?)",
                    (version, digest(sql.encode("utf-8")), "2026-11-24T09:00:00.000000Z"),
                )
                connection.execute(f"PRAGMA user_version = {version}")
                connection.commit()
            # Migration 2 only adds tables: every baseline row is unchanged and every added table is empty.
            self.assertEqual({name: connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
                              for name in original}, original)
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall():
                if name not in baseline_tables():
                    self.assertEqual(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0], 0, name)
        finally:
            connection.close()
        self.assertGreater(len(archived), 0)
        store = self.open()
        self.assertEqual([(item.position.installation_id, item.position.sequence, item.position.item_index)
                          for item in self.pending(store)], archived)


class R20PendingItems(StoreCase):
    def test_pending_items_are_every_unindexed_item_in_archival_order(self) -> None:
        # OTHER delivers first, then INSTALLATION. Archival order puts OTHER's item first,
        # although INSTALLATION's identity sorts before OTHER's.
        store = self.open()
        self.activate(store, OTHER)
        store.honor_generation(1, b'{"synthetic-advertisement":1}', T0)
        store.archive_ingest_batch(header(OTHER, 7), batch(projection("late"), record_type="weight", page=2))
        store.begin_draining(OTHER, at(100))
        store.retire_installation(OTHER, "drained", at(101))
        self.activate(store, INSTALLATION, 102)
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(projection("first"), retraction("id-x"), cycle_kind="change-cycle", cycle_id=OTHER_CYCLE))
        pending = self.pending(store)
        self.assertEqual([item.position for item in pending], [position(7, 0, OTHER), position(1, 0), position(1, 1)])
        late, first, retracted = pending
        self.assertEqual((late.record_type, late.projection_schema_version, late.cycle_kind, late.cycle_id),
                         ("weight", 1, "initial-snapshot", CYCLE))
        self.assertEqual(late.projection, b'{"synthetic-projection":"late"}')
        self.assertIsNone(late.retracted_identity)
        self.assertEqual((first.cycle_kind, first.cycle_id), ("change-cycle", OTHER_CYCLE))
        self.assertIsNone(retracted.projection)
        self.assertEqual(retracted.retracted_identity, "id-x")

    def test_pending_items_respects_limit(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(projection("a"), projection("b"), projection("c")))
        self.assertEqual([item.position for item in self.pending(store, 2)], [position(1, 0), position(1, 1)])

    def test_indexed_items_leave_pending(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(projection("a"), projection("b")))
        store.index_items((entry(1, 1, "id-b"),), at(5))
        self.assertEqual([item.position for item in self.pending(store)], [position(1, 0)])

    def test_only_ingest_batch_items_are_pending(self) -> None:
        store = self.ready()
        store.archive_completion(header(INSTALLATION, 1, "snapshot-completion"), CompletionRecord(
            completion_kind="snapshot", snapshot_kind="initial", record_type="steps", cycle_id=CYCLE,
            page_count=1, fragment_count=1, record_count=0, final_chain=digest(b"synthetic-final")))
        store.archive_sync_checkpoint(header(INSTALLATION, 2, "sync-checkpoint"), SyncCheckpointRecord(
            run_sequence=1, trigger="manual", started_at="2026-11-24T08:59:00.000Z", ended_at="2026-11-24T08:59:30.000Z",
            elapsed_ms=30000, background_scheduling="available",
            record_types=(RecordTypeOutcome(record_type="steps", outcome="no-change", reason=None, source_data_staged=False),),
            outbox_messages_before_checkpoint=0, outbox_bytes_before_checkpoint=0))
        store.archive_ingest_batch(header(INSTALLATION, 3), batch())
        self.assertEqual(self.pending(store), ())

    def test_pending_read_writes_nothing(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(projection("a")))
        before = contents(self.path)
        self.pending(store)
        with store.read() as view:
            view.index_counts()
            view.records_awaiting_selection(10)
        self.assertEqual(contents(self.path), before)


class R21IndexingProjections(StoreCase):
    def test_new_key_creates_a_source_record(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(projection("a")))
        result = store.index_items((entry(1, 0, "id-a", minute=3),), at(5))
        self.assertEqual(result, IndexResult(created_records=1, created_evidence=1))
        found = self.record(store, key("id-a"))
        self.assertEqual(found.key, key("id-a"))
        self.assertEqual(found.observing_installations, (INSTALLATION,))
        self.assertEqual(len(found.evidence), 1)
        self.assertEqual(found.evidence_head, found.evidence[0].evidence_id)
        self.assertEqual(found.selections, ())

    def test_another_installation_joins_the_same_source_record(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(projection("a")))
        store.index_items((entry(1, 0, "id-a"),), at(5))
        self.handover(store)
        store.archive_ingest_batch(header(OTHER, 1), batch(projection("a-again"), cycle_id=OTHER_CYCLE))
        result = store.index_items((entry(1, 0, "id-a", installation_id=OTHER, minute=4),), at(110))
        self.assertEqual(result, IndexResult(created_records=0, created_evidence=1))
        found = self.record(store, key("id-a"))
        self.assertEqual(found.observing_installations, tuple(sorted((INSTALLATION, OTHER))))
        self.assertEqual([item.position.installation_id for item in found.evidence], [INSTALLATION, OTHER])
        with store.read() as view:
            self.assertEqual(view.index_counts().source_records, 1)

    def test_each_key_part_separates_source_records(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(*(projection(str(index)) for index in range(4))))
        store.archive_ingest_batch(header(INSTALLATION, 2), batch(projection("w"), record_type="weight", page=2))
        result = store.index_items((
            entry(1, 0, "id-a"),
            entry(1, 1, "id-a", origin=OTHER_ORIGIN),
            entry(1, 2, "id-b"),
            entry(1, 3, "id-a", kind="synthetic-other-system"),
            entry(2, 0, "id-a"),
        ), at(5))
        self.assertEqual(result, IndexResult(created_records=5, created_evidence=5))
        with store.read() as view:
            identities = {view.source_record_id(candidate) for candidate in (
                key("id-a"), key("id-a", origin=OTHER_ORIGIN), key("id-b"),
                key("id-a", kind="synthetic-other-system"), key("id-a", record_type="weight"))}
        self.assertEqual(len(identities), 5)
        self.assertNotIn(None, identities)
        self.assertNotIn("client_record_id", {field.name for field in fields(ProjectionEntry)})
        self.assertNotIn("client_record_id", {field.name for field in fields(SourceKey)})

    def test_evidence_keeps_position_cycle_time_and_digest(self) -> None:
        store = self.ready()
        item = projection("a")
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(item, cycle_kind="change-cycle", cycle_id=OTHER_CYCLE))
        modified = datetime(2026, 11, 20, 7, 30, 15, 250000, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        store.index_items((ProjectionEntry(position(1, 0), HC, ORIGIN, "id-a", modified),), at(5))
        (evidence,) = self.record(store, key("id-a")).evidence
        self.assertEqual(evidence.kind, "projection")
        self.assertEqual(evidence.position, position(1, 0))
        self.assertEqual((evidence.cycle_kind, evidence.cycle_id), ("change-cycle", OTHER_CYCLE))
        self.assertEqual(evidence.last_modified_time, modified)
        self.assertEqual(evidence.last_modified_time.utcoffset(), timedelta(0))
        self.assertEqual(evidence.projection_sha256, digest(item.projection or b""))
        self.assertEqual(evidence.indexed_at, at(5))

    def test_identical_reindex_writes_nothing(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(projection("a"), retraction("id-z")))
        entries = (entry(1, 0, "id-a"), gone(1, 1))
        store.index_items(entries, at(5))
        before = contents(self.path)
        self.assertEqual(store.index_items(entries, at(6)), IndexResult(created_records=0, created_evidence=0))
        self.assertEqual(store.index_items((), at(7)), IndexResult(created_records=0, created_evidence=0))
        self.assertEqual(contents(self.path), before)

    def test_repeated_entry_in_one_call_counts_once(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(projection("a")))
        self.assertEqual(store.index_items((entry(1, 0, "id-a"), entry(1, 0, "id-a")), at(5)),
                         IndexResult(created_records=1, created_evidence=1))
        self.assertEqual(len(self.record(store, key("id-a")).evidence), 1)


class R22IndexingRetractions(StoreCase):
    def test_retraction_belongs_to_every_matching_source_record(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(projection("a"), projection("a2"), projection("w")))
        store.archive_ingest_batch(header(INSTALLATION, 2), batch(retraction("id-a"), cycle_kind="change-cycle", cycle_id=OTHER_CYCLE))
        store.index_items((entry(1, 0, "id-a"), entry(1, 1, "id-a", origin=OTHER_ORIGIN), entry(1, 2, "id-b"), gone(2, 0)), at(5))
        for candidate in (key("id-a"), key("id-a", origin=OTHER_ORIGIN)):
            found = self.record(store, candidate)
            self.assertEqual([item.kind for item in found.evidence], ["projection", "retraction"], candidate)
            retracted = found.evidence[1]
            self.assertEqual(retracted.position, position(2, 0))
            self.assertIsNone(retracted.last_modified_time)
            self.assertIsNone(retracted.projection_sha256)
        self.assertEqual([item.kind for item in self.record(store, key("id-b")).evidence], ["projection"])

    def test_retraction_before_projection_still_belongs(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(retraction("id-a"), cycle_kind="change-cycle", cycle_id=OTHER_CYCLE))
        self.assertEqual(store.index_items((gone(1, 0),), at(5)), IndexResult(created_records=0, created_evidence=1))
        with store.read() as view:
            self.assertIsNone(view.source_record_id(key("id-a")))
            self.assertEqual(view.index_counts(), IndexCounts(unindexed_items=0, source_records=0, evidence_items=1, records_awaiting_selection=0))
        store.archive_ingest_batch(header(INSTALLATION, 2), batch(projection("a"), page=2))
        store.index_items((entry(2, 0, "id-a"),), at(6))
        found = self.record(store, key("id-a"))
        self.assertEqual([(item.kind, item.position) for item in found.evidence],
                         [("retraction", position(1, 0)), ("projection", position(2, 0))])

    def test_retract_then_reproject_keeps_delivery_order(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(projection("a")))
        store.archive_ingest_batch(header(INSTALLATION, 2), batch(retraction("id-a"), projection("a-again"), cycle_kind="change-cycle", cycle_id=OTHER_CYCLE))
        # Indexed in one call, in reverse order: evidence still follows delivery position.
        store.index_items((entry(2, 1, "id-a", minute=9), gone(2, 0), entry(1, 0, "id-a", minute=1)), at(5))
        found = self.record(store, key("id-a"))
        self.assertEqual([(item.kind, item.position) for item in found.evidence],
                         [("projection", position(1, 0)), ("retraction", position(2, 0)), ("projection", position(2, 1))])
        ids = [item.evidence_id for item in found.evidence]
        self.assertEqual(ids, sorted(ids))

    def test_retraction_changes_no_projection_evidence(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(projection("a")))
        store.index_items((entry(1, 0, "id-a", minute=1),), at(5))
        (before,) = self.record(store, key("id-a")).evidence
        store.archive_ingest_batch(header(INSTALLATION, 2), batch(retraction("id-a"), cycle_kind="change-cycle", cycle_id=OTHER_CYCLE))
        store.index_items((gone(2, 0),), at(6))
        found = self.record(store, key("id-a"))
        self.assertEqual(found.evidence[0], before)
        with store.read() as view:
            self.assertEqual(view.archived_items(INSTALLATION, 1), (projection("a"),))


class R23IndexRefusals(StoreCase):
    def archived(self):
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(projection("a"), retraction("id-z")))
        return store

    def test_index_refusal_order(self) -> None:
        store = self.archived()
        naive = ProjectionEntry(position(1, 0), HC, ORIGIN, "id-a", datetime(2026, 11, 24, 9, 0))
        blank = entry(1, 0, "")
        unknown = entry(9, 0, "id-a")
        mismatch = entry(1, 1, "id-a")
        store.index_items((entry(1, 0, "id-a"),), at(5))
        conflict = entry(1, 0, "id-other")
        self.assertRefused("timestamp-invalid", store.index_items, (blank, naive), at(6))
        self.assertRefused("source-key-invalid", store.index_items, (unknown, blank), at(6))
        self.assertRefused("archived-item-unknown", store.index_items, (mismatch, unknown), at(6))
        self.assertRefused("index-entry-mismatch", store.index_items, (conflict, mismatch), at(6))
        self.free = RESERVE - 1
        self.assertRefused("index-entry-conflict", store.index_items, (conflict,), at(6))
        self.assertRetryable("storage-reserve-exhausted", store.index_items, (gone(1, 1),), at(6))

    def test_each_index_refusal_leaves_database_unchanged(self) -> None:
        store = self.archived()
        store.index_items((entry(1, 0, "id-a"),), at(5))
        cases = {
            "timestamp-invalid": ((gone(1, 1),), datetime(2026, 11, 24, 9, 0)),
            "source-key-invalid": ((entry(1, 0, "id-a", origin=""),), at(6)),
            "archived-item-unknown": ((gone(1, 7),), at(6)),
            "index-entry-mismatch": ((RetractionEntry(position(1, 0)),), at(6)),
            "index-entry-conflict": ((entry(1, 0, "id-a", minute=30),), at(6)),
        }
        for failure, (entries, when) in cases.items():
            with self.subTest(failure):
                self.assertRefused(failure, store.index_items, entries, when)
        for bad in (entry(1, 0, "id-a", kind=""), ProjectionEntry(position(1, 0), HC, ORIGIN, 7, at(1))):  # type: ignore[arg-type]
            with self.subTest(bad):
                self.assertRefused("source-key-invalid", store.index_items, (bad,), at(6))
        self.assertRefused("archived-item-unknown", store.index_items, (entry(1, 0, "id-a", installation_id=OTHER),), at(6))

    def test_one_bad_entry_refuses_the_whole_call(self) -> None:
        store = self.archived()
        self.assertRefused("archived-item-unknown", store.index_items, (entry(1, 0, "id-a"), gone(1, 1), gone(3, 0)), at(5))
        self.assertEqual(len(self.pending(store)), 2)
        self.assertRefused("index-entry-conflict", store.index_items, (entry(1, 0, "id-a"), entry(1, 0, "id-b")), at(5))

    def install_fault_triggers(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        connection.execute("CREATE TABLE test_fault (remaining INTEGER NOT NULL)")
        connection.execute("INSERT INTO test_fault VALUES (1000000)")
        for table in tables:
            for event in ("INSERT", "UPDATE", "DELETE"):
                connection.execute(
                    f'CREATE TRIGGER "test_fault_{table}_{event}" AFTER {event} ON "{table}" BEGIN '
                    "UPDATE test_fault SET remaining = remaining - 1; "
                    "SELECT RAISE(ABORT, 'injected fault') WHERE (SELECT remaining FROM test_fault) <= 0; END"
                )
        self.addCleanup(connection.close)
        return connection

    def assert_every_partial_failure_commits_nothing(self, faults: sqlite3.Connection, operation) -> None:
        failures = 0
        for writes_allowed in range(1, 200):
            faults.execute("UPDATE test_fault SET remaining = ?", (writes_allowed,))
            before = contents(self.path)
            try:
                operation()
            except Exception:
                failures += 1
                after = contents(self.path)
                after["tables"].pop("test_fault", None)  # type: ignore[union-attr]
                before["tables"].pop("test_fault", None)  # type: ignore[union-attr]
                self.assertEqual(after, before, f"partial commit after {writes_allowed} writes")
                continue
            break
        else:
            self.fail("operation never completed")
        self.assertGreater(failures, 0)

    def test_index_is_atomic_under_injected_failure(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(projection("a"), projection("b"), retraction("id-a")))
        faults = self.install_fault_triggers()
        self.assert_every_partial_failure_commits_nothing(
            faults, lambda: store.index_items((entry(1, 0, "id-a"), entry(1, 1, "id-b"), gone(1, 2)), at(5)))
        found = self.record(store, key("id-a"))
        self.assertEqual(len(found.evidence), 2)
        self.assertEqual(found.evidence_head, found.evidence[-1].evidence_id)
        self.assert_every_partial_failure_commits_nothing(
            faults, lambda: store.record_selection(found.record_id, "retraction", (found.evidence[1].evidence_id,), found.evidence_head, at(6)))


class R24SourceRecordReads(StoreCase):
    def test_source_record_lists_evidence_and_provenance(self) -> None:
        store = self.two_records()
        with store.read() as view:
            record_id = view.source_record_id(key("id-a"))
            found = view.source_record(record_id)
        assert found is not None
        self.assertEqual(found.record_id, record_id)
        self.assertEqual(found.key, key("id-a"))
        self.assertEqual([item.kind for item in found.evidence], ["projection", "retraction"])
        self.assertEqual(found.evidence_head, max(item.evidence_id for item in found.evidence))
        self.assertEqual(found.observing_installations, (INSTALLATION,))
        for dataclass in (ItemPosition, PendingItem, ProjectionEntry, RetractionEntry, SourceKey, Evidence, Selection,
                          SourceRecord, IndexResult, IndexCounts):
            with self.subTest(dataclass.__name__), self.assertRaises(Exception):
                setattr(dataclass.__new__(dataclass), fields(dataclass)[0].name, None)

    def test_unknown_source_record_reads_none(self) -> None:
        store = self.two_records()
        with store.read() as view:
            self.assertIsNone(view.source_record_id(key("id-missing")))
            self.assertIsNone(view.source_record(999_999))

    def test_source_record_read_is_isolated_from_later_commits(self) -> None:
        store = self.two_records()
        record_id = self.record(store, key("id-a")).record_id
        with store.read() as view:
            first = view.source_record(record_id)
            store.archive_ingest_batch(header(INSTALLATION, 3), batch(projection("a3"), cycle_kind="change-cycle", cycle_id=OTHER_CYCLE, page=2))
            store.index_items((entry(3, 0, "id-a", minute=20),), at(20))
            self.assertEqual(view.source_record(record_id), first)
            self.assertEqual(view.pending_items(10), ())
        self.assertEqual(len(self.record(store, key("id-a")).evidence), 3)

    def test_source_record_reads_write_nothing(self) -> None:
        store = self.two_records()
        before = contents(self.path)
        with store.read() as view:
            record_id = view.source_record_id(key("id-a"))
            view.source_record(record_id)
            view.source_record_id(key("id-missing"))
            view.source_record(999_999)
        self.assertEqual(contents(self.path), before)


class R25ReconciledSelections(StoreCase):
    def test_each_selection_outcome_is_recorded(self) -> None:
        store = self.two_records()
        a = self.record(store, key("id-a"))
        b = self.record(store, key("id-b"))
        projected, retracted = (item.evidence_id for item in a.evidence)
        first = store.record_selection(a.record_id, "projection", (projected,), a.evidence_head, at(11))
        self.assertEqual((first.outcome, first.evidence_ids, first.evidence_head, first.recorded_at),
                         ("projection", (projected,), a.evidence_head, at(11)))
        second = store.record_selection(a.record_id, "retraction", (retracted,), a.evidence_head, at(12))
        self.assertGreater(second.selection_id, first.selection_id)
        store.archive_ingest_batch(header(INSTALLATION, 3), batch(projection("b2"), cycle_kind="change-cycle", cycle_id=OTHER_CYCLE, page=2))
        store.index_items((entry(3, 0, "id-b", minute=2),), at(13))
        b = self.record(store, key("id-b"))
        ids = tuple(item.evidence_id for item in b.evidence)
        conflict = store.record_selection(b.record_id, "conflict", ids, b.evidence_head, at(14))
        self.assertEqual((conflict.outcome, set(conflict.evidence_ids)), ("conflict", set(ids)))
        self.assertEqual(self.record(store, key("id-a")).selections, (first, second))
        self.assertEqual(self.record(store, key("id-b")).selections, (conflict,))

    def test_selection_shapes_are_checked(self) -> None:
        store = self.two_records()
        a = self.record(store, key("id-a"))
        b = self.record(store, key("id-b"))
        projected, retracted = (item.evidence_id for item in a.evidence)
        (b_projected,) = (item.evidence_id for item in b.evidence)
        head = a.evidence_head
        invalid = (
            ("projection", ()),
            ("projection", (projected, projected)),
            ("projection", (retracted,)),
            ("projection", (b_projected,)),
            ("projection", (999_999,)),
            ("retraction", (projected,)),
            ("retraction", ()),
            ("conflict", (projected,)),
            ("conflict", (projected, retracted)),
            ("conflict", (projected, projected)),
            ("conflict", (projected, b_projected)),
            ("synthetic-outcome", (projected,)),
        )
        for outcome, evidence_ids in invalid:
            with self.subTest(outcome=outcome, evidence_ids=evidence_ids):
                self.assertRefused("selection-evidence-invalid", store.record_selection, a.record_id, outcome, evidence_ids, head, at(11))
        self.assertRefused("source-record-unknown", store.record_selection, 999_999, "projection", (projected,), head, at(11))
        self.assertRefused("timestamp-invalid", store.record_selection, a.record_id, "projection", (projected,), head, datetime(2026, 11, 24, 9, 0))
        # A retraction of another identity does not belong to this record.
        store.archive_ingest_batch(header(INSTALLATION, 3), batch(retraction("id-b"), cycle_kind="change-cycle", cycle_id=OTHER_CYCLE, page=2))
        store.index_items((gone(3, 0),), at(12))
        b = self.record(store, key("id-b"))
        self.assertRefused("selection-evidence-invalid", store.record_selection, a.record_id, "retraction",
                           (b.evidence[-1].evidence_id,), head, at(13))
        self.free = RESERVE - 1
        self.assertRetryable("storage-reserve-exhausted", store.record_selection, a.record_id, "projection", (projected,), head, at(14))

    def test_stale_selection_is_refused(self) -> None:
        store = self.two_records()
        a = self.record(store, key("id-a"))
        projected = a.evidence[0].evidence_id
        self.assertRefused("selection-stale", store.record_selection, a.record_id, "projection", (projected,), a.evidence_head - 1, at(11))
        self.assertRefused("selection-stale", store.record_selection, a.record_id, "projection", (projected,), a.evidence_head + 1, at(11))
        store.archive_ingest_batch(header(INSTALLATION, 3), batch(projection("a3"), cycle_kind="change-cycle", cycle_id=OTHER_CYCLE, page=2))
        store.index_items((entry(3, 0, "id-a", minute=20),), at(12))
        self.assertRefused("selection-stale", store.record_selection, a.record_id, "projection", (projected,), a.evidence_head, at(13))

    def test_selection_history_is_append_only(self) -> None:
        store = self.two_records()
        a = self.record(store, key("id-a"))
        projected, retracted = (item.evidence_id for item in a.evidence)
        first = store.record_selection(a.record_id, "projection", (projected,), a.evidence_head, at(11))
        store.record_selection(a.record_id, "retraction", (retracted,), a.evidence_head, at(12))
        store.archive_ingest_batch(header(INSTALLATION, 3), batch(projection("a3"), cycle_kind="change-cycle", cycle_id=OTHER_CYCLE, page=2))
        store.index_items((entry(3, 0, "id-a", minute=20),), at(13))
        latest = self.record(store, key("id-a"))
        third = store.record_selection(a.record_id, "projection", (latest.evidence[-1].evidence_id,), latest.evidence_head, at(14))
        selections = self.record(store, key("id-a")).selections
        self.assertEqual(len(selections), 3)
        self.assertEqual(selections[0], first)
        self.assertEqual(selections[-1], third)

    def test_identical_selection_writes_nothing(self) -> None:
        store = self.two_records()
        a = self.record(store, key("id-a"))
        projected = a.evidence[0].evidence_id
        first = store.record_selection(a.record_id, "projection", (projected,), a.evidence_head, at(11))
        before = contents(self.path)
        self.assertEqual(store.record_selection(a.record_id, "projection", (projected,), a.evidence_head, at(15)), first)
        self.assertEqual(contents(self.path), before)

    def test_records_awaiting_selection(self) -> None:
        store = self.two_records()
        a = self.record(store, key("id-a"))
        b = self.record(store, key("id-b"))
        with store.read() as view:
            self.assertEqual(view.records_awaiting_selection(10), tuple(sorted((a.record_id, b.record_id))))
            self.assertEqual(view.records_awaiting_selection(1), (min(a.record_id, b.record_id),))
        store.record_selection(a.record_id, "retraction", (a.evidence[1].evidence_id,), a.evidence_head, at(11))
        with store.read() as view:
            self.assertEqual(view.records_awaiting_selection(10), (b.record_id,))

    def test_new_evidence_makes_a_selected_record_await_again(self) -> None:
        store = self.two_records()
        b = self.record(store, key("id-b"))
        store.record_selection(b.record_id, "projection", (b.evidence[0].evidence_id,), b.evidence_head, at(11))
        with store.read() as view:
            self.assertNotIn(b.record_id, view.records_awaiting_selection(10))
        store.archive_ingest_batch(header(INSTALLATION, 3), batch(retraction("id-b"), cycle_kind="change-cycle", cycle_id=OTHER_CYCLE, page=2))
        store.index_items((gone(3, 0),), at(12))
        with store.read() as view:
            self.assertIn(b.record_id, view.records_awaiting_selection(10))


class R26HealthDataFreeCountsAndRefusals(StoreCase):
    def test_index_counts(self) -> None:
        store = self.two_records()
        store.archive_ingest_batch(header(INSTALLATION, 3), batch(projection("c"), projection("d"), page=2))
        with store.read() as view:
            self.assertEqual(view.index_counts(), IndexCounts(unindexed_items=2, source_records=2, evidence_items=3, records_awaiting_selection=2))
        for field in fields(IndexCounts):
            self.assertIs(type(getattr(IndexCounts(1, 2, 3, 4), field.name)), int)

    def test_refusals_and_counts_are_health_data_free(self) -> None:
        store = self.ready()
        marked = BatchItem(projection=b'{"' + HEALTH_MARKER + b'":1}')
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(marked, retraction(IDENTITY_MARKER)))
        texts = [repr(store.index_items((entry(1, 0, IDENTITY_MARKER), gone(1, 1)), at(5)))]
        with store.read() as view:
            texts.append(repr(view.index_counts()))
        for operation in (
            lambda: store.index_items((entry(1, 0, IDENTITY_MARKER + "-other"),), at(6)),
            lambda: store.index_items((entry(1, 0, ""),), at(6)),
            lambda: store.record_selection(999_999, "projection", (1,), 1, at(6)),
        ):
            with self.assertRaises(StoreRefusal) as caught:
                operation()
            texts += [str(caught.exception), repr(caught.exception)]
        for text in texts:
            self.assertNotIn(HEALTH_MARKER.decode(), text)
            self.assertNotIn(IDENTITY_MARKER, text)


class R27VersionOneAndTwoUnchanged(StoreCase):
    def test_index_and_selection_change_no_version_1_table(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(projection("a"), projection("b"), retraction("id-a")))
        names = baseline_tables()
        before = {name: rows for name, rows in contents(self.path)["tables"].items() if name in names}  # type: ignore[union-attr]
        store.index_items((entry(1, 0, "id-a"), entry(1, 1, "id-b"), gone(1, 2)), at(5))
        found = self.record(store, key("id-a"))
        store.record_selection(found.record_id, "retraction", (found.evidence[-1].evidence_id,), found.evidence_head, at(6))
        store.index_items((entry(1, 0, "id-a"),), at(7))
        after = {name: rows for name, rows in contents(self.path)["tables"].items() if name in names}  # type: ignore[union-attr]
        self.assertEqual(after, before)
        with store.read() as view:
            self.assertEqual(view.archived_items(INSTALLATION, 1), (projection("a"), projection("b"), retraction("id-a")))
            self.assertIsNotNone(view.delivery_cycle(INSTALLATION, CYCLE))


class R21R22OrderAcrossCalls(StoreCase):
    def test_reverse_archival_indexing_across_calls_keeps_positions_and_advances_head(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(projection("a")))
        store.archive_ingest_batch(header(INSTALLATION, 2), batch(retraction("id-a"), cycle_kind="change-cycle", cycle_id=OTHER_CYCLE))
        store.archive_ingest_batch(header(INSTALLATION, 3), batch(projection("a3"), cycle_kind="change-cycle", cycle_id=OTHER_CYCLE, page=2))
        store.index_items((entry(1, 0, "id-a", minute=1),), at(5))
        heads = [self.record(store, key("id-a")).evidence_head]
        store.index_items((entry(3, 0, "id-a", minute=3),), at(6))
        heads.append(self.record(store, key("id-a")).evidence_head)
        store.index_items((gone(2, 0),), at(7))
        found = self.record(store, key("id-a"))
        heads.append(found.evidence_head)
        # Indexing order across calls; every row keeps its delivery position.
        self.assertEqual([(item.kind, item.position) for item in found.evidence],
                         [("projection", position(1, 0)), ("projection", position(3, 0)), ("retraction", position(2, 0))])
        self.assertEqual(heads, sorted(set(heads)))
        self.assertEqual(found.evidence_head, found.evidence[-1].evidence_id)

    def test_retraction_never_crosses_record_types(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(projection("s"), projection("s2")))
        store.archive_ingest_batch(header(INSTALLATION, 2), batch(projection("w"), record_type="weight", page=2))
        store.index_items((entry(1, 0, "id-a"), entry(1, 1, "id-a", origin=OTHER_ORIGIN), entry(2, 0, "id-a")), at(5))
        weight = self.record(store, key("id-a", record_type="weight"))
        store.record_selection(weight.record_id, "projection", (weight.evidence[0].evidence_id,), weight.evidence_head, at(6))
        steps_before = [self.record(store, candidate) for candidate in (key("id-a"), key("id-a", origin=OTHER_ORIGIN))]
        store.archive_ingest_batch(header(INSTALLATION, 3), batch(retraction("id-a"), cycle_kind="change-cycle", cycle_id=OTHER_CYCLE, page=3))
        store.index_items((gone(3, 0),), at(7))
        retracted_ids = set()
        for before in steps_before:
            after = self.record(store, before.key)
            self.assertEqual(after.evidence[:-1], before.evidence)
            self.assertEqual((after.evidence[-1].kind, after.evidence[-1].position), ("retraction", position(3, 0)))
            self.assertGreater(after.evidence_head, before.evidence_head)
            retracted_ids.add(after.evidence[-1].evidence_id)
        self.assertEqual(len(retracted_ids), 1)
        weight_after = self.record(store, key("id-a", record_type="weight"))
        self.assertEqual((weight_after.evidence, weight_after.evidence_head), (weight.evidence, weight.evidence_head))
        with store.read() as view:
            awaiting = view.records_awaiting_selection(10)
        self.assertNotIn(weight.record_id, awaiting)
        for before in steps_before:
            self.assertIn(before.record_id, awaiting)


class R23ReserveAndNoWriteCalls(StoreCase):
    def test_reserve_allows_no_write_calls_and_equal_free_space(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(INSTALLATION, 1), batch(projection("a"), projection("b"), retraction("id-a")))
        store.index_items((entry(1, 0, "id-a"),), at(5))
        found = self.record(store, key("id-a"))
        selected = store.record_selection(found.record_id, "projection", (found.evidence[0].evidence_id,), found.evidence_head, at(6))
        self.free = RESERVE - 1
        before = contents(self.path)
        self.assertEqual(store.index_items((), at(7)), IndexResult(created_records=0, created_evidence=0))
        self.assertEqual(store.index_items((entry(1, 0, "id-a"),), at(7)), IndexResult(created_records=0, created_evidence=0))
        self.assertEqual(store.record_selection(found.record_id, "projection", (found.evidence[0].evidence_id,), found.evidence_head, at(7)), selected)
        self.assertEqual(contents(self.path), before)
        self.assertRetryable("storage-reserve-exhausted", store.index_items, (entry(1, 0, "id-a"), entry(1, 1, "id-b")), at(7))
        self.free = RESERVE
        self.assertEqual(store.index_items((entry(1, 1, "id-b"), gone(1, 2)), at(8)), IndexResult(created_records=1, created_evidence=2))
        found = self.record(store, key("id-a"))
        self.free = RESERVE - 1
        self.assertRetryable("storage-reserve-exhausted", store.record_selection, found.record_id, "retraction",
                             (found.evidence[-1].evidence_id,), found.evidence_head, at(9))
        self.free = RESERVE
        store.record_selection(found.record_id, "retraction", (found.evidence[-1].evidence_id,), found.evidence_head, at(9))
        self.assertEqual(self.record(store, key("id-a")).selections[-1].outcome, "retraction")


class R25LatestSelectionAndStaleness(StoreCase):
    def test_only_the_latest_selection_is_a_repeat(self) -> None:
        store = self.two_records()
        a = self.record(store, key("id-a"))
        projected, retracted = (item.evidence_id for item in a.evidence)
        first = store.record_selection(a.record_id, "projection", (projected,), a.evidence_head, at(11))
        second = store.record_selection(a.record_id, "retraction", (retracted,), a.evidence_head, at(12))
        third = store.record_selection(a.record_id, "projection", (projected,), a.evidence_head, at(13))
        self.assertNotEqual(third.selection_id, first.selection_id)
        self.assertEqual(self.record(store, key("id-a")).selections, (first, second, third))

    def test_identical_selection_after_new_evidence_is_stale(self) -> None:
        store = self.two_records()
        b = self.record(store, key("id-b"))
        store.record_selection(b.record_id, "projection", (b.evidence[0].evidence_id,), b.evidence_head, at(11))
        store.archive_ingest_batch(header(INSTALLATION, 3), batch(projection("b3"), cycle_kind="change-cycle", cycle_id=OTHER_CYCLE, page=2))
        store.index_items((entry(3, 0, "id-b", minute=30),), at(12))
        self.assertRefused("selection-stale", store.record_selection, b.record_id, "projection",
                           (b.evidence[0].evidence_id,), b.evidence_head, at(13))


class R25DurabilityAcrossReopen(StoreCase):
    def test_index_and_selections_survive_reopen(self) -> None:
        store = self.two_records()
        a = self.record(store, key("id-a"))
        selection_args = (a.record_id, "retraction", (a.evidence[1].evidence_id,), a.evidence_head)
        store.record_selection(*selection_args, at(11))
        store.archive_ingest_batch(header(INSTALLATION, 3), batch(retraction("id-q"), projection("p"), cycle_kind="change-cycle", cycle_id=OTHER_CYCLE, page=2))
        store.index_items((gone(3, 0),), at(12))
        with store.read() as view:
            snapshot = (view.source_record(a.record_id), view.source_record(self.record(store, key("id-b")).record_id),
                        view.index_counts(), view.pending_items(10), view.records_awaiting_selection(10))
        store.close()
        reopened = self.open()
        with reopened.read() as view:
            self.assertEqual((view.source_record(a.record_id), view.source_record(snapshot[1].record_id),
                              view.index_counts(), view.pending_items(10), view.records_awaiting_selection(10)), snapshot)
        before = contents(self.path)
        self.assertEqual(reopened.index_items((entry(1, 0, "id-a", minute=1), gone(3, 0)), at(13)), IndexResult(0, 0))
        self.assertEqual(reopened.record_selection(*selection_args, at(13)), snapshot[0].selections[-1])
        self.assertEqual(contents(self.path), before)
        reopened.index_items((entry(3, 1, "id-q"),), at(14))
        q = self.record(reopened, key("id-q"))
        self.assertEqual([(item.kind, item.position) for item in q.evidence], [("retraction", position(3, 0)), ("projection", position(3, 1))])


if __name__ == "__main__":
    unittest.main()
