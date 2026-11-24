"""Acceptance tests for `operational-store@4`.

Requirement numbers refer to `packs/operational-store-4.spec.md`. `@1`, `@2`,
and `@3`'s requirements R1-R27 stay bound to their suites, which the `@4`
runner re-runs through `@3`'s bound runner. Every database lives in a
temporary directory and every value is synthetic. Source Records and
selections are built through `@3`'s own operations; outcomes are hand-written
with marker bytes, and the store never checks them against
`interpret-source-projection@1` (Decision 7). Every time is the caller's: the
store never reads a clock (Decision 15).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from activity_tracker_portal import operational_store as store_module
from activity_tracker_portal.operational_store import (
    AwaitingProcessing,
    BatchItem,
    IngestBatchRecord,
    ItemPosition,
    ItemQuarantine,
    Lease,
    MessageHeader,
    OutcomeEntry,
    PendingItem,
    ProcessingAttempt,
    ProcessingCounts,
    ProcessingJob,
    ProcessingOutcome,
    ProjectionEntry,
    RetractionEntry,
    RevisionEntry,
    SourceKey,
    SourceRecord,
    StoredRevision,
    StoreRefusal,
    StoreRetryable,
    open_store,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_3 = json.loads((ROOT / "governance/scopes/operational-store.3.json").read_text(encoding="utf-8"))
CONTRACT_4 = json.loads((ROOT / "governance/scopes/operational-store.4.json").read_text(encoding="utf-8"))
SCOPE_MAP = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
FIXTURE = ROOT / "tests/acceptance/fixtures/operational-store-3-index.sql"

UTC = timezone.utc
T0 = datetime(2026, 11, 24, 9, 0, 0, 123456, tzinfo=UTC)
NAIVE = datetime(2026, 11, 24, 9, 0)
RESERVE = 1_000_000
PLENTY = 10**12
INSTALLATION = "0f8fad5b-d9cb-469f-a165-70867728950e"
CYCLE = "e3b0c442-98fc-4c14-9afb-f4c8996fb924"
OTHER_CYCLE = "5d41402a-bc4b-4a76-b971-9d911017c592"
HC = "health-connect"
ORIGIN = "org.example.synthetic.source"
PV = "interpret-steps@1"
PV2 = "interpret-steps@2"
WPV = "interpret-weight@1"
POLICY = "synthetic-policy-1"
OTHER_POLICY = "synthetic-policy-2"
OWNER = "synthetic-worker-1"
OTHER_OWNER = "synthetic-worker-2"
LEASE = 60
HEALTH_MARKER = b"synthetic-health-marker"
IDENTITY_MARKER = "synthetic-identity-marker"
HEADER_MARKER = b"synthetic-header-marker"
BODY_MARKER = b"synthetic-body-marker"
START_NS = 1_790_000_000_000_000_000
NEW_FAILURES = (
    "selection-unknown",
    "selection-not-latest",
    "selection-not-projection",
    "processor-version-invalid",
    "processing-request-invalid",
    "processing-job-unknown",
    "lease-not-held",
    "outcome-invalid",
    "outcome-conflict",
    "item-already-indexed",
    "item-quarantine-conflict",
)
NEW_OUTPUTS = ["processing_job", "processing_lease", "processing_outcome", "item_quarantine"]
NEW_TYPES = (RevisionEntry, OutcomeEntry, StoredRevision, ProcessingOutcome, ProcessingAttempt, ProcessingJob, Lease,
             ItemQuarantine, AwaitingProcessing, ProcessingCounts)
COUNT_FIELDS = ("jobs_ready", "jobs_delayed", "jobs_leased", "jobs_lease_expired", "jobs_completed", "jobs_failed",
                "jobs_superseded", "revisions", "unmodeled", "policy_pending", "quarantined", "quarantined_items",
                "items_awaiting_index")


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def after(moment: datetime, seconds: float) -> datetime:
    return moment + timedelta(seconds=seconds)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def header(installation_id: str, sequence: int) -> MessageHeader:
    body = {"synthetic": True, "kind": "ingest-batch", "installation": installation_id, "sequence": sequence}
    data = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return MessageHeader(
        installation_id=installation_id, sequence=sequence, message_sha256=digest(data), staged_generation=1,
        receipt_time=at(sequence), envelope=data, receipt=b'{"synthetic-receipt":%d}' % sequence,
    )


def batch(*items: BatchItem, record_type: str = "steps", cycle_kind: str = "initial-snapshot",
          cycle_id: str = CYCLE, page: int = 1) -> IngestBatchRecord:
    return IngestBatchRecord(
        record_type=record_type, projection_schema_version=1, cycle_kind=cycle_kind,  # type: ignore[arg-type]
        cycle_id=cycle_id, source_page_index=page, fragment_index=1,
        previous_chain=digest(b"synthetic-chain-%d" % page), items=items,
    )


def position(sequence: int, item_index: int) -> ItemPosition:
    return ItemPosition(installation_id=INSTALLATION, sequence=sequence, item_index=item_index)


def key(identity: str, record_type: str = "steps") -> SourceKey:
    return SourceKey(source_system_kind=HC, origin=ORIGIN, record_type=record_type, source_identity=identity)


def revision_entry(label: bytes = b"r", **changes: object) -> RevisionEntry:
    values: dict[str, object] = dict(
        header_bytes=b'{"synthetic-header":"' + label + b'"}', body_bytes=b'{"synthetic-body":"' + label + b'"}',
        body_version=1, start_epoch_ns=START_NS, end_epoch_ns=START_NS + 600_000_000_000,
        start_local_date=date(2026, 9, 21), end_local_date=date(2026, 9, 21),
        start_attribution="experienced", end_attribution="experienced",
    )
    values.update(changes)
    return RevisionEntry(**values)  # type: ignore[arg-type]


def instant_entry(label: bytes = b"i", **changes: object) -> RevisionEntry:
    return revision_entry(label, **{"end_epoch_ns": None, "end_local_date": None, "end_attribution": None, **changes})


def revision(label: bytes = b"r", *, pv: str = PV, policy: object = POLICY, **changes: object) -> OutcomeEntry:
    return OutcomeEntry(kind="revision", processor_version=pv, policy_identity=policy,  # type: ignore[arg-type]
                        reason=None, pointer=None, policy_kind=None, revision=revision_entry(label, **changes))


def unmodeled(reason: object = "synthetic-unmodeled-shape", *, pv: str = PV, policy: str = POLICY,
              policy_kind: str | None = None) -> OutcomeEntry:
    return OutcomeEntry(kind="unmodeled", processor_version=pv, policy_identity=policy, reason=reason,  # type: ignore[arg-type]
                        pointer=None, policy_kind=policy_kind, revision=None)


def policy_pending(reason: object = "synthetic-policy-needed", policy_kind: object = "synthetic-timezone", *,
                   pv: str = PV, policy: str = POLICY) -> OutcomeEntry:
    return OutcomeEntry(kind="policy-pending", processor_version=pv, policy_identity=policy, reason=reason,  # type: ignore[arg-type]
                        pointer=None, policy_kind=policy_kind, revision=None)  # type: ignore[arg-type]


def quarantine(reason: object = "synthetic-value-out-of-range", pointer: object = "/value", *, pv: str = PV,
               policy: str = POLICY, policy_kind: str | None = None) -> OutcomeEntry:
    return OutcomeEntry(kind="quarantine", processor_version=pv, policy_identity=policy, reason=reason,  # type: ignore[arg-type]
                        pointer=pointer, policy_kind=policy_kind, revision=None)  # type: ignore[arg-type]


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


def tables_created_by(migrations: int) -> set[str]:
    """The tables that the first `migrations` known migrations create."""
    connection = sqlite3.connect(":memory:")
    try:
        for _version, sql in store_module.MIGRATIONS[:migrations]:
            connection.executescript(sql)
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
    finally:
        connection.close()


def counts(value: ProcessingCounts) -> dict[str, int]:
    return {name: getattr(value, name) for name in COUNT_FIELDS}


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "portal.sqlite3"
        self.free = PLENTY
        self.sequence = 0

    def open(self):
        store = open_store(self.path, reserve_bytes=RESERVE, free_bytes=lambda _path: self.free)
        self.addCleanup(store.close)
        return store

    def ready(self):
        store = self.open()
        offer = digest(b"synthetic-enrollment-code")
        store.record_enrollment_offer(offer, at(30), at(0))
        store.enroll_installation(offer, INSTALLATION, digest(b"synthetic-fingerprint"), at(0))
        store.activate_installation(INSTALLATION, at(0))
        store.honor_generation(1, b'{"synthetic-advertisement":1}', T0)
        return store

    def archive(self, store, *items: BatchItem, record_type: str = "steps", change: bool = False) -> int:
        self.sequence += 1
        store.archive_ingest_batch(header(INSTALLATION, self.sequence), batch(
            *items, record_type=record_type, page=self.sequence,
            cycle_kind="change-cycle" if change else "initial-snapshot", cycle_id=OTHER_CYCLE if change else CYCLE))
        return self.sequence

    def record(self, store, identity: str, record_type: str = "steps") -> SourceRecord:
        with store.read() as view:
            record_id = view.source_record_id(key(identity, record_type))
            self.assertIsNotNone(record_id, identity)
            found = view.source_record(record_id)
        assert found is not None
        return found

    def project(self, store, identity: str, record_type: str = "steps", payload: bytes | None = None) -> tuple[int, int]:
        """Archive and index one projection of identity and select it as current: (record_id, selection_id)."""
        data = payload if payload is not None else b'{"synthetic-projection":"%s-%d"}' % (identity.encode(), self.sequence + 1)
        sequence = self.archive(store, BatchItem(projection=data), record_type=record_type)
        store.index_items((ProjectionEntry(position(sequence, 0), HC, ORIGIN, identity, at(sequence)),), at(sequence))
        found = self.record(store, identity, record_type)
        selection = store.record_selection(found.record_id, "projection", (found.evidence[-1].evidence_id,),
                                           found.evidence_head, at(sequence))
        return found.record_id, selection.selection_id

    def retract(self, store, identity: str, record_type: str = "steps") -> int:
        """Archive and index a retraction of identity and select it as current: the selection_id."""
        sequence = self.archive(store, BatchItem(retracted_identity=identity), record_type=record_type, change=True)
        store.index_items((RetractionEntry(position(sequence, 0)),), at(sequence))
        found = self.record(store, identity, record_type)
        return store.record_selection(found.record_id, "retraction", (found.evidence[-1].evidence_id,),
                                      found.evidence_head, at(sequence)).selection_id

    def conflict(self, store, identity: str, record_type: str = "steps") -> int:
        """Archive and index another projection of identity and select a Source Conflict: the selection_id."""
        sequence = self.archive(store, BatchItem(projection=b'{"synthetic-conflict":%d}' % self.sequence), record_type=record_type)
        store.index_items((ProjectionEntry(position(sequence, 0), HC, ORIGIN, identity, at(sequence)),), at(sequence))
        found = self.record(store, identity, record_type)
        ids = tuple(item.evidence_id for item in found.evidence if item.kind == "projection")
        return store.record_selection(found.record_id, "conflict", ids, found.evidence_head, at(sequence)).selection_id

    def evidence_of(self, store, record_id: int, selection_id: int) -> int:
        with store.read() as view:
            found = view.source_record(record_id)
        assert found is not None
        (selection,) = [item for item in found.selections if item.selection_id == selection_id]
        (evidence_id,) = selection.evidence_ids
        return evidence_id

    def job(self, store, job_id: int) -> ProcessingJob:
        with store.read() as view:
            found = view.processing_job(job_id)
        assert found is not None
        return found

    def jobs(self, store, record_id: int) -> tuple[ProcessingJob, ...]:
        with store.read() as view:
            return view.processing_jobs(record_id)

    def outcomes(self, store, record_id: int) -> tuple[ProcessingOutcome, ...]:
        with store.read() as view:
            return view.processing_outcomes(record_id)

    def awaiting(self, store, mapping: dict[str, str], limit: int = 100) -> tuple[AwaitingProcessing, ...]:
        with store.read() as view:
            return view.selections_awaiting_processing(mapping, limit)

    def leased(self, store, identity: str = "id-a", *, max_attempts: int = 3, when: datetime | None = None,
               lease_seconds: int = LEASE, pv: str = PV) -> tuple[int, int, Lease]:
        """A projected record with a job that has just been claimed: (record_id, selection_id, lease)."""
        record_id, selection_id = self.project(store, identity)
        when = when or at(10)
        store.enqueue_processing(record_id, selection_id, pv, max_attempts, when)
        lease = store.claim_processing_job(OWNER, lease_seconds, when)
        assert lease is not None
        self.assertEqual(lease.record_id, record_id)
        return record_id, selection_id, lease

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

    def assertWritesNothing(self, operation, *args, **kwargs):
        before = contents(self.path)
        result = operation(*args, **kwargs)
        self.assertEqual(contents(self.path), before, "a call that writes nothing changed the database")
        return result


class R28GovernedScopeContractVersion4(unittest.TestCase):
    def test_contract_4_extends_version_3(self) -> None:
        self.assertEqual(CONTRACT_4["scope"], "operational-store")
        self.assertEqual(CONTRACT_4["version"], "4")
        self.assertEqual(CONTRACT_4["kind"], CONTRACT_3["kind"])
        for name in ("observable_effects", "dependencies"):
            self.assertEqual(CONTRACT_4[name], CONTRACT_3[name], name)
        self.assertEqual([(item["name"], item["mode"]) for item in CONTRACT_4["inputs"]],
                         [(item["name"], item["mode"]) for item in CONTRACT_3["inputs"]])
        three = CONTRACT_3["named_failures"]
        self.assertEqual(CONTRACT_4["named_failures"][: len(three)], three)
        added = CONTRACT_4["named_failures"][len(three):]
        self.assertEqual(tuple(failure["name"] for failure in added), NEW_FAILURES)
        self.assertEqual({failure["outcome"] for failure in added}, {"invariant-refusal"})
        self.assertEqual([item["name"] for item in CONTRACT_4["outputs"]],
                         [item["name"] for item in CONTRACT_3["outputs"]] + NEW_OUTPUTS)
        owns_3, owns_4 = CONTRACT_3["authority"]["owns"], CONTRACT_4["authority"]["owns"]
        self.assertEqual(owns_4[: len(owns_3)], owns_3)
        appended = owns_4[len(owns_3):]
        self.assertTrue(any("Processing Jobs" in entry for entry in appended))
        self.assertTrue(any("Canonical Revisions" in entry for entry in appended))
        self.assertTrue(any("quarantine" in entry for entry in appended))
        self.assertIn("Processing Job", CONTRACT_4["purpose"])
        oracle = CONTRACT_4["acceptance_oracle"]
        self.assertEqual(oracle["specification"], "packs/operational-store-4.spec.md")
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_operational_store_4.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_operational_store_4.sh")

    def test_scope_map_row_does_not_contradict_contract_4(self) -> None:
        row = next(line for line in SCOPE_MAP.splitlines() if line.startswith("| `operational-store` |"))
        self.assertIn("| shared |", row)
        self.assertIn("transactional", row)
        self.assertIn("processing", row)
        self.assertIn("lease", row)

    def test_failures_match_contract_4(self) -> None:
        partitions = {
            "startup-refusal": set(store_module.STARTUP_FAILURES),
            "retryable": set(store_module.RETRYABLE_FAILURES),
            "invariant-refusal": set(store_module.INVARIANT_FAILURES),
        }
        for failure in CONTRACT_4["named_failures"]:
            self.assertIn(failure["name"], partitions[failure["outcome"]], failure["name"])
        self.assertLessEqual(set(NEW_FAILURES), set(store_module.FAILURES))
        self.assertLessEqual(set(NEW_FAILURES), set(store_module.INVARIANT_FAILURES))


class R29MigrationThreeIsAdditive(StoreCase):
    def load_fixture(self) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.executescript(FIXTURE.read_text(encoding="utf-8"))
        finally:
            connection.close()

    def test_new_database_records_migrations_1_to_3(self) -> None:
        self.open().close()
        known = [(version, digest(sql.encode("utf-8"))) for version, sql in store_module.MIGRATIONS]
        self.assertGreaterEqual(len(known), 3)
        self.assertEqual([version for version, _ in known[:3]], [1, 2, 3])
        # Migrations 1 and 2 are @3's, exactly as the published @3 module recorded them.
        self.load_fixture_into_memory_and_compare(known[:2])
        state = contents(self.path)
        self.assertEqual([row[:2] for row in sorted(state["tables"]["store_migrations"])], known)  # type: ignore[index]
        self.assertEqual(state["user_version"], known[-1][0])
        # Migration 3 only creates tables and indexes.
        statements = [statement for statement in store_module.MIGRATIONS[2][1].split(";") if statement.strip()]
        self.assertGreater(len(statements), 0)
        for statement in statements:
            code = "\n".join(line for line in statement.strip().splitlines() if not line.strip().startswith("--")).strip()
            self.assertRegex(code.upper(), r"^CREATE\s+(UNIQUE\s+)?(TABLE|INDEX)\s", code)
        self.assertLess(tables_created_by(2), tables_created_by(3))

    def load_fixture_into_memory_and_compare(self, expected: list[tuple[int, str]]) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(FIXTURE.read_text(encoding="utf-8"))
            recorded = connection.execute("SELECT version, checksum FROM store_migrations ORDER BY version").fetchall()
        finally:
            connection.close()
        self.assertEqual(recorded, expected)

    def test_version_3_database_requires_migration_and_is_untouched(self) -> None:
        self.load_fixture()
        before = contents(self.path)
        self.assertEqual(before["user_version"], 2)
        with self.assertRaises(StoreRefusal) as caught:
            open_store(self.path, reserve_bytes=RESERVE, free_bytes=lambda _path: PLENTY)
        self.assertEqual(caught.exception.failure, "store-migration-required")
        self.assertEqual(contents(self.path), before)

    def test_migrated_version_3_index_awaits_processing(self) -> None:
        self.load_fixture()
        connection = sqlite3.connect(self.path)
        try:
            earlier = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            columns = {table: [row[1] for row in connection.execute(f'PRAGMA table_xinfo("{table}")')] for table in earlier}

            def rows(table: str) -> list[tuple[object, ...]]:
                names = ", ".join(f'"{column}"' for column in columns[table])
                return list(connection.execute(f'SELECT {names} FROM "{table}" ORDER BY rowid'))

            original = {table: rows(table) for table in earlier if table != "store_migrations"}
            self.assertGreater(len(original["source_selections"]), 0)
            for version, sql in store_module.MIGRATIONS[2:]:
                connection.executescript(sql)
                connection.execute(
                    "INSERT INTO store_migrations (version, checksum, applied_at) VALUES (?, ?, ?)",
                    (version, digest(sql.encode("utf-8")), "2026-11-24T09:00:00.000000Z"),
                )
                connection.execute(f"PRAGMA user_version = {version}")
                connection.commit()
            # No backfill: every earlier row is unchanged, columns added to earlier
            # tables stay NULL, and every added table is empty.
            self.assertEqual({table: rows(table) for table in original}, original)
            for table in earlier:
                for _cid, column, *_rest, hidden in connection.execute(f'PRAGMA table_xinfo("{table}")'):
                    if column not in columns[table] and hidden != 2:
                        filled = connection.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" IS NOT NULL').fetchone()[0]
                        self.assertEqual(filled, 0, f"{table}.{column}")
            added = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'") if row[0] not in columns]
            self.assertGreater(len(added), 0)
            for table in added:
                self.assertEqual(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0], 0, table)
            record_ids = [row[0] for row in connection.execute("SELECT record_id FROM source_records ORDER BY record_id")]
        finally:
            connection.close()
        store = self.open()
        mapping = {"steps": PV, "weight": WPV}
        with store.read() as view:
            expected = []
            for record_id in record_ids:
                found = view.source_record(record_id)
                assert found is not None
                if found.selections and found.selections[-1].outcome == "projection":
                    expected.append(AwaitingProcessing(record_id, found.selections[-1].selection_id,
                                                       mapping[found.key.record_type]))
            awaiting = view.selections_awaiting_processing(mapping, 100)
            pending = view.pending_items(100)
            items = view.items_awaiting_index(100)
            processing = view.processing_counts(at(0))
            outcomes = [view.processing_outcomes(record_id) for record_id in record_ids]
            jobs = [view.processing_jobs(record_id) for record_id in record_ids]
            a = view.source_record(view.source_record_id(key("synthetic-id-a")))  # type: ignore[arg-type]
        # The fixture holds projection, retraction, conflict, and unselected records,
        # and a record whose newer projection was selected again.
        self.assertEqual(len(expected), 3)
        self.assertEqual(awaiting, tuple(expected))
        assert a is not None
        self.assertEqual(len(a.selections), 2)
        self.assertIn(AwaitingProcessing(a.record_id, a.selections[-1].selection_id, PV), awaiting)
        self.assertEqual(len(pending), 3)
        self.assertEqual(items, pending)
        self.assertEqual(outcomes, [()] * len(record_ids))
        self.assertEqual(jobs, [()] * len(record_ids))
        self.assertEqual(counts(processing), {name: 3 if name == "items_awaiting_index" else 0 for name in COUNT_FIELDS})
        job = store.enqueue_processing(a.record_id, a.selections[-1].selection_id, PV, 3, at(1))
        self.assertEqual((job.state, job.selection_id), ("queued", a.selections[-1].selection_id))


class R30EnqueueingAProcessingJob(StoreCase):
    def test_enqueue_creates_a_queued_job(self) -> None:
        store = self.ready()
        record_id, selection_id = self.project(store, "id-a")
        job = store.enqueue_processing(record_id, selection_id, PV, 3, at(20))
        self.assertIsInstance(job, ProcessingJob)
        self.assertEqual(
            (job.record_id, job.processor_version, job.selection_id, job.state, job.max_attempts, job.attempts,
             job.not_before, job.lease_owner, job.lease_expires_at, job.failure, job.enqueued_at, job.closed_at,
             job.history),
            (record_id, PV, selection_id, "queued", 3, 0, at(20), None, None, None, at(20), None, ()),
        )
        self.assertEqual(self.job(store, job.job_id), job)
        self.assertEqual(self.jobs(store, record_id), (job,))
        # Another processor version is another job; the bounds 1 and 100 are accepted.
        other = store.enqueue_processing(record_id, selection_id, PV2, 1, at(21))
        self.assertNotEqual(other.job_id, job.job_id)
        self.assertEqual(self.jobs(store, record_id), (job, other))
        b, b_selection = self.project(store, "id-b")
        self.assertEqual(store.enqueue_processing(b, b_selection, PV, 100, at(22)).max_attempts, 100)

    def test_open_job_is_returned_unchanged(self) -> None:
        store = self.ready()
        record_id, first_selection = self.project(store, "id-a")
        job = store.enqueue_processing(record_id, first_selection, PV, 3, at(20))
        record_id, newer_selection = self.project(store, "id-a")
        self.free = RESERVE - 1
        # A queued job is returned as it is, even for a newer selection, another bound, and a later time.
        self.assertEqual(self.assertWritesNothing(store.enqueue_processing, record_id, newer_selection, PV, 5, at(25)), job)
        self.assertEqual(job.selection_id, first_selection)
        self.free = PLENTY
        lease = store.claim_processing_job(OWNER, LEASE, at(30))
        assert lease is not None
        held = self.job(store, job.job_id)
        self.assertEqual(held.state, "leased")
        self.free = RESERVE - 1
        self.assertEqual(self.assertWritesNothing(store.enqueue_processing, record_id, newer_selection, PV, 3, at(31)), held)
        # Even after its lease expired, an unclaimed leased job is still the open job.
        self.assertEqual(self.assertWritesNothing(store.enqueue_processing, record_id, newer_selection, PV, 3, at(90)), held)
        self.assertEqual(len(self.jobs(store, record_id)), 1)

    def test_enqueue_refusals(self) -> None:
        store = self.ready()
        record_id, old_selection = self.project(store, "id-a")
        record_id, selection_id = self.project(store, "id-a")
        other_record, other_selection = self.project(store, "id-b")
        retracted_record, _ = self.project(store, "id-r")
        retraction = self.retract(store, "id-r")
        conflicted_record, _ = self.project(store, "id-c")
        conflict = self.conflict(store, "id-c")
        self.assertRefused("timestamp-invalid", store.enqueue_processing, record_id, selection_id, PV, 3, NAIVE)
        for bad in (0, -1, 101, 2.5, "3", None):
            with self.subTest(max_attempts=bad):
                self.assertRefused("processing-request-invalid", store.enqueue_processing, record_id, selection_id, PV, bad, at(20))
        for bad in ("interpret-steps", "interpret-steps@", "@1", "Interpret-Steps@1", "interpret_steps@1",
                    "interpret steps@1", "interpret-steps@one", "interpret-steps@1@2", "", 7, None):
            with self.subTest(processor_version=bad):
                self.assertRefused("processor-version-invalid", store.enqueue_processing, record_id, selection_id, bad, 3, at(20))
        self.assertRefused("source-record-unknown", store.enqueue_processing, 999_999, selection_id, PV, 3, at(20))
        self.assertRefused("selection-unknown", store.enqueue_processing, record_id, 999_999, PV, 3, at(20))
        self.assertRefused("selection-unknown", store.enqueue_processing, record_id, other_selection, PV, 3, at(20))
        self.assertRefused("selection-not-latest", store.enqueue_processing, record_id, old_selection, PV, 3, at(20))
        self.assertRefused("selection-not-projection", store.enqueue_processing, retracted_record, retraction, PV, 3, at(20))
        self.assertRefused("selection-not-projection", store.enqueue_processing, conflicted_record, conflict, PV, 3, at(20))
        self.free = RESERVE - 1
        self.assertRetryable("storage-reserve-exhausted", store.enqueue_processing, record_id, selection_id, PV, 3, at(20))
        self.free = RESERVE
        self.assertEqual(store.enqueue_processing(record_id, selection_id, PV, 3, at(20)).state, "queued")
        self.assertEqual(self.jobs(store, other_record), ())

    def test_enqueue_after_completion_creates_a_new_job(self) -> None:
        store = self.ready()
        record_id, selection_id, lease = self.leased(store)
        recorded = store.complete_processing_job(lease.job_id, lease.fence, revision(), at(11))
        again = store.enqueue_processing(record_id, selection_id, PV, 2, at(12))
        self.assertGreater(again.job_id, lease.job_id)
        self.assertEqual((again.state, again.selection_id, again.attempts, again.max_attempts, again.not_before),
                         ("queued", selection_id, 0, 2, at(12)))
        self.assertEqual(self.job(store, lease.job_id).state, "completed")
        self.assertEqual(self.outcomes(store, record_id), (recorded,))
        # A failed job does not block a manual retry either (R35).
        second = store.claim_processing_job(OWNER, LEASE, at(13))
        assert second is not None
        store.fail_processing_job(second.job_id, second.fence, "deterministic", "synthetic-defect", None, at(14))
        third = store.enqueue_processing(record_id, selection_id, PV, 3, at(15))
        self.assertGreater(third.job_id, second.job_id)
        self.assertEqual(third.state, "queued")
        self.assertEqual([job.state for job in self.jobs(store, record_id)], ["completed", "failed", "queued"])


class R31ClaimingAndExpiringLeases(StoreCase):
    def test_claim_takes_the_lowest_eligible_job(self) -> None:
        store = self.ready()
        made = []
        for identity in ("id-a", "id-b", "id-c"):
            record_id, selection_id = self.project(store, identity)
            made.append((record_id, selection_id, store.enqueue_processing(record_id, selection_id, PV, 3, at(20)).job_id))
        made.sort(key=lambda item: item[2])
        for index, (record_id, selection_id, job_id) in enumerate(made):
            when = at(21 + index)
            lease = store.claim_processing_job(OWNER, 3600, when)
            assert lease is not None
            self.assertEqual(lease, Lease(job_id=job_id, fence=lease.fence, owner=OWNER, record_id=record_id,
                                          selection_id=selection_id, evidence_id=self.evidence_of(store, record_id, selection_id),
                                          processor_version=PV, expires_at=after(when, 3600)))
            job = self.job(store, job_id)
            self.assertEqual((job.state, job.attempts, job.fence, job.lease_owner, job.lease_expires_at, job.selection_id),
                             ("leased", 1, lease.fence, OWNER, after(when, 3600), selection_id))
            self.assertEqual(job.history, (ProcessingAttempt(lease.fence, OWNER, when, after(when, 3600), None, None, None),))
        self.assertIsNone(self.assertWritesNothing(store.claim_processing_job, OWNER, 3600, at(24)))

    def test_delayed_job_is_not_claimed_early(self) -> None:
        store = self.ready()
        a, _, lease = self.leased(store, "id-a", when=at(10))
        store.fail_processing_job(lease.job_id, lease.fence, "transient", "synthetic-timeout", at(50), at(11))
        b, b_selection = self.project(store, "id-b")
        later = store.enqueue_processing(b, b_selection, PV, 3, at(30))
        # A job enqueued for a later time is not eligible before then.
        self.assertIsNone(self.assertWritesNothing(store.claim_processing_job, OWNER, LEASE, at(29)))
        # The lower job is delayed, so a higher ready job is claimed instead.
        self.assertEqual(store.claim_processing_job(OWNER, 3600, at(30)).job_id, later.job_id)  # type: ignore[union-attr]
        self.assertIsNone(self.assertWritesNothing(store.claim_processing_job, OWNER, LEASE, after(at(50), -0.000001)))
        # not_before is inclusive.
        retried = store.claim_processing_job(OWNER, LEASE, at(50))
        assert retried is not None
        self.assertEqual((retried.job_id, retried.record_id), (lease.job_id, a))
        self.assertGreater(retried.fence, lease.fence)

    def test_expired_lease_is_reclaimed_with_a_higher_fence(self) -> None:
        store = self.ready()
        record_id, selection_id, first = self.leased(store, when=at(10), lease_seconds=LEASE)
        self.assertEqual(first.expires_at, after(at(10), LEASE))
        self.assertIsNone(self.assertWritesNothing(store.claim_processing_job, OTHER_OWNER, LEASE, after(at(10), LEASE - 0.000001)))
        # A lease that expires exactly at the claim time is expired.
        when = after(at(10), LEASE)
        second = store.claim_processing_job(OTHER_OWNER, LEASE, when)
        assert second is not None
        self.assertEqual((second.job_id, second.owner, second.selection_id, second.expires_at),
                         (first.job_id, OTHER_OWNER, selection_id, after(when, LEASE)))
        self.assertGreater(second.fence, first.fence)
        job = self.job(store, first.job_id)
        self.assertEqual((job.state, job.attempts, job.fence, job.lease_owner), ("leased", 2, second.fence, OTHER_OWNER))
        self.assertEqual([(attempt.fence, attempt.owner, attempt.result) for attempt in job.history],
                         [(first.fence, OWNER, "lease-expired"), (second.fence, OTHER_OWNER, None)])
        self.assertIsNotNone(job.history[0].finished_at)
        self.assertIsNone(job.history[1].finished_at)
        self.assertRefused("lease-not-held", store.renew_processing_lease, first.job_id, first.fence, LEASE, when)
        self.assertRefused("lease-not-held", store.complete_processing_job, first.job_id, first.fence, revision(), when)
        self.assertRefused("lease-not-held", store.fail_processing_job, first.job_id, first.fence, "transient",
                           "synthetic-timeout", when, when)

    def test_exhausted_expired_job_fails(self) -> None:
        store = self.ready()
        record_id, _, first = self.leased(store, max_attempts=1, when=at(10))
        late = at(30)
        self.assertIsNone(store.claim_processing_job(OTHER_OWNER, LEASE, late))
        job = self.job(store, first.job_id)
        self.assertEqual((job.state, job.failure, job.attempts, job.closed_at), ("failed", "lease-expired-retries-exhausted", 1, late))
        self.assertEqual([attempt.result for attempt in job.history], ["lease-expired"])
        # The bound counts reclaims of expired leases: two attempts allow one reclaim, and the claim moves on.
        b, _, second = self.leased(store, "id-b", max_attempts=2, when=at(40))
        c, c_selection = self.project(store, "id-c")
        c_job = store.enqueue_processing(c, c_selection, PV, 2, at(40))
        reclaimed = store.claim_processing_job(OTHER_OWNER, 3600, at(50))
        self.assertEqual((reclaimed.job_id, reclaimed.owner), (second.job_id, OTHER_OWNER))  # type: ignore[union-attr]
        self.assertEqual(store.claim_processing_job(OWNER, LEASE, at(50)).job_id, c_job.job_id)  # type: ignore[union-attr]
        moved_on = store.claim_processing_job(OWNER, LEASE, at(200))
        assert moved_on is not None
        self.assertEqual(moved_on.job_id, c_job.job_id)
        exhausted = self.job(store, second.job_id)
        self.assertEqual((exhausted.state, exhausted.failure, exhausted.attempts, exhausted.closed_at),
                         ("failed", "lease-expired-retries-exhausted", 2, at(200)))
        self.assertEqual([attempt.result for attempt in exhausted.history], ["lease-expired", "lease-expired"])
        # A failed job is never claimed again.
        self.assertIsNone(store.claim_processing_job(OWNER, LEASE, at(200.5)))
        self.assertEqual(self.job(store, second.job_id), exhausted)
        self.assertEqual(self.job(store, first.job_id), job)

    def test_claim_retargets_to_the_latest_selection(self) -> None:
        store = self.ready()
        record_id, first_selection = self.project(store, "id-a")
        job = store.enqueue_processing(record_id, first_selection, PV, 3, at(20))
        _, second_selection = self.project(store, "id-a")
        lease = store.claim_processing_job(OWNER, LEASE, at(21))
        assert lease is not None
        self.assertEqual((lease.job_id, lease.selection_id, lease.evidence_id),
                         (job.job_id, second_selection, self.evidence_of(store, record_id, second_selection)))
        self.assertEqual(self.job(store, job.job_id).selection_id, second_selection)
        # A reclaimed expired lease is re-targeted too.
        _, third_selection = self.project(store, "id-a")
        again = store.claim_processing_job(OWNER, LEASE, at(30))
        assert again is not None
        self.assertEqual((again.job_id, again.selection_id, again.evidence_id),
                         (job.job_id, third_selection, self.evidence_of(store, record_id, third_selection)))
        self.assertNotEqual(lease.evidence_id, again.evidence_id)
        self.assertEqual(self.job(store, job.job_id).selection_id, third_selection)
        self.assertEqual(len(self.jobs(store, record_id)), 1)

    def test_retargeting_keeps_the_retry_budget(self) -> None:
        store = self.ready()
        record_id, _, first = self.leased(store, max_attempts=2, when=at(10))
        _, second_selection = self.project(store, "id-a")
        # The reclaim re-targets the job and still uses its second and last attempt;
        # only completion against a newer projection resets attempts (R33).
        second = store.claim_processing_job(OTHER_OWNER, LEASE, at(20))
        assert second is not None
        self.assertEqual((second.job_id, second.selection_id), (first.job_id, second_selection))
        self.assertGreater(second.fence, first.fence)
        job = self.job(store, first.job_id)
        self.assertEqual((job.attempts, job.selection_id), (2, second_selection))
        self.assertEqual([(attempt.fence, attempt.result) for attempt in job.history],
                         [(first.fence, "lease-expired"), (second.fence, None)])
        # Newer evidence does not refresh an exhausted budget. The expired job fails
        # before any re-targeting, so it last targeted the selection it was leased for.
        _, third_selection = self.project(store, "id-a")
        self.assertIsNone(store.claim_processing_job(OWNER, LEASE, at(30)))
        failed = self.job(store, first.job_id)
        self.assertEqual((failed.state, failed.failure, failed.attempts, failed.closed_at, failed.selection_id),
                         ("failed", "lease-expired-retries-exhausted", 2, at(30), second_selection))
        self.assertEqual([attempt.result for attempt in failed.history], ["lease-expired", "lease-expired"])
        self.assertEqual(len(self.jobs(store, record_id)), 1)
        self.assertEqual(self.awaiting(store, {"steps": PV}), (AwaitingProcessing(record_id, third_selection, PV),))
        # A transient retry re-targeted at claim uses the same budget.
        _, _, lease = self.leased(store, "id-b", max_attempts=2, when=at(40))
        store.fail_processing_job(lease.job_id, lease.fence, "transient", "synthetic-timeout", at(41), at(40))
        _, b_newer = self.project(store, "id-b")
        retried = store.claim_processing_job(OWNER, LEASE, at(41))
        assert retried is not None
        self.assertEqual((retried.job_id, retried.selection_id), (lease.job_id, b_newer))
        self.assertEqual(self.job(store, lease.job_id).attempts, 2)
        exhausted = store.fail_processing_job(retried.job_id, retried.fence, "transient", "synthetic-timeout", at(42), at(41))
        self.assertEqual((exhausted.state, exhausted.failure, exhausted.attempts), ("failed", "retries-exhausted", 2))

    def test_retracted_or_conflicted_record_supersedes_the_job(self) -> None:
        store = self.ready()
        made = {}
        for identity in ("id-r", "id-c", "id-ok"):
            record_id, selection_id = self.project(store, identity)
            made[identity] = store.enqueue_processing(record_id, selection_id, PV, 3, at(20))
        self.retract(store, "id-r")
        self.conflict(store, "id-c")
        lease = store.claim_processing_job(OWNER, LEASE, at(21))
        assert lease is not None
        self.assertEqual(lease.job_id, made["id-ok"].job_id)
        for identity in ("id-r", "id-c"):
            job = self.job(store, made[identity].job_id)
            self.assertEqual((job.state, job.closed_at, job.attempts, job.history), ("superseded", at(21), 0, ()), identity)
        # An expired lease whose record was retracted closes its attempt as expired, then is superseded.
        self.retract(store, "id-ok")
        self.assertIsNone(store.claim_processing_job(OWNER, LEASE, at(40)))
        job = self.job(store, lease.job_id)
        self.assertEqual((job.state, job.closed_at), ("superseded", at(40)))
        self.assertEqual([attempt.result for attempt in job.history], ["lease-expired"])
        self.assertRefused("lease-not-held", store.complete_processing_job, lease.job_id, lease.fence, revision(), at(41))

    def test_claim_with_nothing_eligible_returns_none(self) -> None:
        store = self.ready()
        self.assertIsNone(self.assertWritesNothing(store.claim_processing_job, OWNER, LEASE, at(10)))
        record_id, selection_id = self.project(store, "id-a")
        store.enqueue_processing(record_id, selection_id, PV, 3, at(20))
        self.assertIsNone(self.assertWritesNothing(store.claim_processing_job, OWNER, LEASE, at(19)))
        lease = store.claim_processing_job(OWNER, 3600, at(20))
        assert lease is not None
        self.assertIsNone(self.assertWritesNothing(store.claim_processing_job, OWNER, LEASE, at(21)))
        # A claim that only records a supersession commits it and returns None.
        b, b_selection = self.project(store, "id-b")
        b_job = store.enqueue_processing(b, b_selection, PV, 3, at(22))
        self.retract(store, "id-b")
        self.assertIsNone(store.claim_processing_job(OWNER, LEASE, at(23)))
        self.assertEqual(self.job(store, b_job.job_id).state, "superseded")
        self.assertEqual(self.job(store, lease.job_id).state, "leased")

    def test_two_handles_never_hold_one_job(self) -> None:
        store = self.ready()
        other = self.open()
        record_id, selection_id = self.project(store, "id-a")
        job = store.enqueue_processing(record_id, selection_id, PV, 3, at(20))
        first = store.claim_processing_job(OWNER, LEASE, at(21))
        assert first is not None
        self.assertIsNone(other.claim_processing_job(OTHER_OWNER, LEASE, at(21)))
        second = other.claim_processing_job(OTHER_OWNER, LEASE, at(30))
        assert second is not None
        self.assertEqual(second.job_id, job.job_id)
        self.assertGreater(second.fence, first.fence)
        self.assertRefused("lease-not-held", store.complete_processing_job, first.job_id, first.fence, revision(), at(31))
        other.complete_processing_job(second.job_id, second.fence, revision(), at(31))
        # Stress, not a forced interleaving: two workers on separate handles race to
        # claim twelve jobs, and no job is ever claimed twice. Either may claim all.
        jobs = set()
        for index in range(12):
            r, s = self.project(store, f"id-many-{index}")
            jobs.add(store.enqueue_processing(r, s, PV, 3, at(40)).job_id)
        claims: dict[str, list[int]] = {"left": [], "right": []}
        errors: list[BaseException] = []
        start = threading.Barrier(2, timeout=30)

        def work(name: str) -> None:
            try:
                handle = open_store(self.path, reserve_bytes=RESERVE, free_bytes=lambda _path: PLENTY)
                try:
                    start.wait()
                    while (lease := handle.claim_processing_job(name, 3600, at(41))) is not None:
                        claims[name].append(lease.job_id)
                finally:
                    handle.close()
            except BaseException as error:  # pragma: no cover - reported below
                errors.append(error)
                # Release the peer so that neither worker can wait forever.
                start.abort()

        threads = [threading.Thread(target=work, args=(name,), daemon=True) for name in claims]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual([thread.is_alive() for thread in threads], [False, False], "a claim worker did not finish")
        self.assertEqual(errors, [])
        self.assertEqual(set(claims["left"]) & set(claims["right"]), set())
        self.assertEqual(sorted(claims["left"] + claims["right"]), sorted(jobs))
        for job_id in jobs:
            self.assertEqual(len(self.job(store, job_id).history), 1)


class R32RenewingALease(StoreCase):
    def test_renew_extends_a_held_lease(self) -> None:
        store = self.ready()
        record_id, selection_id, lease = self.leased(store, when=at(10), lease_seconds=LEASE)
        renewed = store.renew_processing_lease(lease.job_id, lease.fence, 300, after(at(10), 30))
        self.assertEqual(renewed, Lease(lease.job_id, lease.fence, OWNER, record_id, selection_id, lease.evidence_id, PV,
                                        after(at(10), 330)))
        job = self.job(store, lease.job_id)
        self.assertEqual((job.state, job.fence, job.lease_expires_at, job.attempts), ("leased", lease.fence, after(at(10), 330), 1))
        self.assertEqual(job.history[-1].expires_at, after(at(10), LEASE))
        self.assertIsNone(store.claim_processing_job(OTHER_OWNER, LEASE, after(at(10), 329)))
        self.assertRefused("processing-job-unknown", store.renew_processing_lease, 999_999, lease.fence, LEASE, at(11))
        for fence in (lease.fence - 1, lease.fence + 1):
            self.assertRefused("lease-not-held", store.renew_processing_lease, lease.job_id, fence, LEASE, at(11))
        for bad in (0, 86_401, 2.5, "60"):
            with self.subTest(lease_seconds=bad):
                self.assertRefused("processing-request-invalid", store.renew_processing_lease, lease.job_id, lease.fence, bad, at(11))
        self.assertRefused("timestamp-invalid", store.renew_processing_lease, lease.job_id, lease.fence, LEASE, NAIVE)
        self.assertEqual(store.renew_processing_lease(lease.job_id, lease.fence, 86_400, at(11)).expires_at, after(at(11), 86_400))
        self.assertEqual(store.renew_processing_lease(lease.job_id, lease.fence, 1, at(12)).expires_at, after(at(12), 1))
        # A queued job holds no lease.
        b, b_selection = self.project(store, "id-b")
        queued = store.enqueue_processing(b, b_selection, PV, 3, at(13))
        self.assertRefused("lease-not-held", store.renew_processing_lease, queued.job_id, queued.fence, LEASE, at(13))

    def test_renew_after_reclaim_is_not_held(self) -> None:
        store = self.ready()
        _, _, first = self.leased(store, when=at(10))
        second = store.claim_processing_job(OTHER_OWNER, LEASE, at(20))
        assert second is not None
        self.assertRefused("lease-not-held", store.renew_processing_lease, first.job_id, first.fence, LEASE, at(20))
        self.assertEqual(store.renew_processing_lease(second.job_id, second.fence, LEASE, at(20)).fence, second.fence)

    def test_expired_but_unclaimed_lease_can_renew(self) -> None:
        store = self.ready()
        _, _, lease = self.leased(store, when=at(10))
        renewed = store.renew_processing_lease(lease.job_id, lease.fence, LEASE, at(20))
        self.assertEqual((renewed.fence, renewed.expires_at), (lease.fence, after(at(20), LEASE)))
        self.assertIsNone(store.claim_processing_job(OTHER_OWNER, LEASE, after(at(20), LEASE - 1)))
        self.assertEqual(self.job(store, lease.job_id).history[-1].result, None)


class R32AttemptExpiryIsFixedAtClaim(StoreCase):
    def test_attempt_keeps_its_claim_time_expiry(self) -> None:
        store = self.ready()
        _, _, first = self.leased(store, max_attempts=3, when=at(10))
        renewed = store.renew_processing_lease(first.job_id, first.fence, 300, at(10.5))
        job = self.job(store, first.job_id)
        # The live lease is renewed; the attempt keeps the expiry it was claimed with.
        self.assertEqual((job.lease_expires_at, job.history[-1].expires_at), (renewed.expires_at, after(at(10), LEASE)))
        second = store.claim_processing_job(OTHER_OWNER, LEASE, at(20))
        assert second is not None
        store.renew_processing_lease(second.job_id, second.fence, 600, at(20.5))
        store.fail_processing_job(second.job_id, second.fence, "transient", "synthetic-timeout", at(21), at(21))
        third = store.claim_processing_job(OWNER, LEASE, at(21))
        assert third is not None
        store.renew_processing_lease(third.job_id, third.fence, 900, at(21.5))
        store.complete_processing_job(third.job_id, third.fence, revision(), at(22))
        expected = [after(at(10), LEASE), after(at(20), LEASE), after(at(21), LEASE)]
        self.assertEqual([attempt.expires_at for attempt in self.job(store, first.job_id).history], expected)
        store.close()
        reopened = self.open()
        with reopened.read() as view:
            found = view.processing_job(first.job_id)
        assert found is not None
        self.assertEqual([attempt.expires_at for attempt in found.history], expected)


class R33CompletingAJobWithItsOutcome(StoreCase):
    def assertRecorded(self, recorded: ProcessingOutcome, entry: OutcomeEntry, lease: Lease, when: datetime) -> None:
        self.assertEqual(
            (recorded.record_id, recorded.selection_id, recorded.evidence_id, recorded.processor_version,
             recorded.policy_identity, recorded.kind, recorded.reason, recorded.pointer, recorded.policy_kind,
             recorded.job_id, recorded.fence, recorded.recorded_at),
            (lease.record_id, lease.selection_id, lease.evidence_id, entry.processor_version, entry.policy_identity,
             entry.kind, entry.reason, entry.pointer, entry.policy_kind, lease.job_id, lease.fence, when),
        )
        if entry.revision is None:
            self.assertIsNone(recorded.revision)
        else:
            stored = recorded.revision
            assert stored is not None
            self.assertIsInstance(stored, StoredRevision)
            for field in fields(RevisionEntry):
                self.assertEqual(getattr(stored, field.name), getattr(entry.revision, field.name), field.name)
            self.assertEqual((stored.header_sha256, stored.body_sha256),
                             (digest(entry.revision.header_bytes), digest(entry.revision.body_bytes)))

    def test_completion_records_outcome_and_closes_job(self) -> None:
        store = self.ready()
        record_id, selection_id, lease = self.leased(store, when=at(10))
        entry = revision(b"done")
        # A held lease completes even after it expired, until another claim takes the job.
        when = at(20)
        recorded = store.complete_processing_job(lease.job_id, lease.fence, entry, when)
        self.assertIsInstance(recorded, ProcessingOutcome)
        self.assertRecorded(recorded, entry, lease, when)
        job = self.job(store, lease.job_id)
        self.assertEqual((job.state, job.closed_at, job.selection_id, job.attempts), ("completed", when, selection_id, 1))
        self.assertEqual([(attempt.fence, attempt.result, attempt.finished_at) for attempt in job.history],
                         [(lease.fence, "completed", when)])
        self.assertEqual(self.outcomes(store, record_id), (recorded,))
        with store.read() as view:
            self.assertEqual(view.latest_outcome(record_id, PV), recorded)
        # The lease is no longer held: a repeat completion is refused and changes nothing.
        self.assertRefused("lease-not-held", store.complete_processing_job, lease.job_id, lease.fence, entry, at(21))
        self.assertIsNone(store.claim_processing_job(OWNER, LEASE, at(22)))

    def test_identical_outcome_is_not_duplicated(self) -> None:
        store = self.ready()
        record_id, selection_id, lease = self.leased(store, when=at(10))
        first = store.complete_processing_job(lease.job_id, lease.fence, revision(b"same"), at(11))
        store.enqueue_processing(record_id, selection_id, PV, 3, at(12))
        again = store.claim_processing_job(OTHER_OWNER, LEASE, at(13))
        assert again is not None
        self.assertNotEqual(again.job_id, lease.job_id)
        self.assertEqual(store.complete_processing_job(again.job_id, again.fence, revision(b"same"), at(14)), first)
        self.assertEqual(self.outcomes(store, record_id), (first,))
        job = self.job(store, again.job_id)
        self.assertEqual((job.state, job.closed_at, job.history[-1].result), ("completed", at(14), "completed"))
        # Another policy identity is another key: reprocessing after a policy change appends.
        store.enqueue_processing(record_id, selection_id, PV, 3, at(15))
        third = store.claim_processing_job(OWNER, LEASE, at(16))
        assert third is not None
        reprocessed = store.complete_processing_job(third.job_id, third.fence, revision(b"same", policy=OTHER_POLICY), at(17))
        self.assertGreater(reprocessed.outcome_id, first.outcome_id)
        self.assertEqual(self.outcomes(store, record_id), (first, reprocessed))

    def test_different_outcome_for_the_same_key_conflicts(self) -> None:
        store = self.ready()
        record_id, selection_id, lease = self.leased(store, when=at(10))
        store.complete_processing_job(lease.job_id, lease.fence, revision(b"first"), at(11))
        store.enqueue_processing(record_id, selection_id, PV, 3, at(12))
        again = store.claim_processing_job(OWNER, LEASE, at(13))
        assert again is not None
        for different in (
            revision(b"other"),
            revision(b"first", body_bytes=b'{"synthetic-body":"changed"}'),
            revision(b"first", header_bytes=b'{"synthetic-header":"changed"}'),
            revision(b"first", body_version=2),
            revision(b"first", start_epoch_ns=START_NS + 1),
            revision(b"first", end_epoch_ns=START_NS + 1),
            revision(b"first", start_local_date=date(2026, 9, 20)),
            revision(b"first", end_local_date=date(2026, 9, 22)),
            revision(b"first", start_attribution="inferred"),
            revision(b"first", end_attribution="inferred"),
            OutcomeEntry("revision", PV, POLICY, None, None, None, instant_entry(b"first")),
            unmodeled(),
            policy_pending(),
            quarantine(),
        ):
            with self.subTest(different=different):
                self.assertRefused("outcome-conflict", store.complete_processing_job, again.job_id, again.fence, different, at(14))
        self.assertEqual(self.job(store, again.job_id).state, "leased")
        self.assertEqual(len(self.outcomes(store, record_id)), 1)
        store.complete_processing_job(again.job_id, again.fence, revision(b"first"), at(15))
        # Non-revision outcomes conflict on any field too.
        b, b_selection, b_lease = self.leased(store, "id-b", when=at(20))
        store.complete_processing_job(b_lease.job_id, b_lease.fence, quarantine("synthetic-bad-value", "/value/0"), at(21))
        store.enqueue_processing(b, b_selection, PV, 3, at(22))
        b_again = store.claim_processing_job(OWNER, LEASE, at(23))
        assert b_again is not None
        for different in (quarantine("synthetic-other-value", "/value/0"), quarantine("synthetic-bad-value", "/value/1"),
                          quarantine("synthetic-bad-value", "")):
            with self.subTest(different=different):
                self.assertRefused("outcome-conflict", store.complete_processing_job, b_again.job_id, b_again.fence, different, at(24))
        store.complete_processing_job(b_again.job_id, b_again.fence, quarantine("synthetic-bad-value", "/value/0"), at(25))
        self.assertEqual(len(self.outcomes(store, b)), 1)
        c, c_selection, c_lease = self.leased(store, "id-c", when=at(30))
        store.complete_processing_job(c_lease.job_id, c_lease.fence, policy_pending("synthetic-policy-needed", "synthetic-timezone"), at(31))
        store.enqueue_processing(c, c_selection, PV, 3, at(32))
        c_again = store.claim_processing_job(OWNER, LEASE, at(33))
        assert c_again is not None
        self.assertRefused("outcome-conflict", store.complete_processing_job, c_again.job_id, c_again.fence,
                           policy_pending("synthetic-policy-needed", "synthetic-other-policy"), at(34))

    def test_newer_selection_requeues_the_job(self) -> None:
        store = self.ready()
        record_id, first_selection, lease = self.leased(store, max_attempts=1, when=at(10))
        _, newer_selection = self.project(store, "id-a")
        recorded = store.complete_processing_job(lease.job_id, lease.fence, revision(), at(11))
        # The outcome is recorded against the leased selection and its evidence, not the newer one.
        self.assertEqual((recorded.selection_id, recorded.evidence_id), (first_selection, lease.evidence_id))
        self.assertNotEqual(lease.evidence_id, self.evidence_of(store, record_id, newer_selection))
        job = self.job(store, lease.job_id)
        self.assertEqual(
            (job.state, job.selection_id, job.not_before, job.attempts, job.max_attempts, job.closed_at,
             job.lease_owner, job.lease_expires_at, job.failure),
            ("queued", newer_selection, at(11), 0, 1, None, None, None, None),
        )
        self.assertEqual([attempt.result for attempt in job.history], ["completed"])
        self.assertRefused("lease-not-held", store.complete_processing_job, lease.job_id, lease.fence, revision(), at(11))
        # Attempts were reset, so the bound of one allows the next claim.
        again = store.claim_processing_job(OWNER, LEASE, at(11))
        assert again is not None
        self.assertEqual((again.job_id, again.selection_id), (lease.job_id, newer_selection))
        self.assertGreater(again.fence, lease.fence)
        second = store.complete_processing_job(again.job_id, again.fence, revision(b"newer"), at(12))
        self.assertEqual(self.job(store, lease.job_id).state, "completed")
        self.assertEqual(self.outcomes(store, record_id), (recorded, second))

    def test_retraction_after_lease_supersedes_the_job(self) -> None:
        store = self.ready()
        record_id, selection_id, lease = self.leased(store, when=at(10))
        self.retract(store, "id-a")
        recorded = store.complete_processing_job(lease.job_id, lease.fence, revision(), at(11))
        self.assertEqual((recorded.selection_id, recorded.evidence_id), (selection_id, lease.evidence_id))
        job = self.job(store, lease.job_id)
        self.assertEqual((job.state, job.closed_at, job.selection_id), ("superseded", at(11), selection_id))
        self.assertEqual([attempt.result for attempt in job.history], ["completed"])
        self.assertEqual(self.outcomes(store, record_id), (recorded,))
        b, b_selection, b_lease = self.leased(store, "id-b", when=at(20))
        self.conflict(store, "id-b")
        store.complete_processing_job(b_lease.job_id, b_lease.fence, unmodeled(), at(21))
        self.assertEqual(self.job(store, b_lease.job_id).state, "superseded")
        self.assertIsNone(store.claim_processing_job(OWNER, LEASE, at(22)))

    def test_reclaimed_lease_cannot_complete(self) -> None:
        store = self.ready()
        record_id, _, first = self.leased(store, when=at(10))
        second = store.claim_processing_job(OTHER_OWNER, LEASE, at(20))
        assert second is not None
        self.assertRefused("lease-not-held", store.complete_processing_job, first.job_id, first.fence, revision(), at(20))
        self.assertRefused("processing-job-unknown", store.complete_processing_job, 999_999, first.fence, revision(), at(20))
        recorded = store.complete_processing_job(second.job_id, second.fence, revision(), at(21))
        self.assertEqual((recorded.job_id, recorded.fence), (second.job_id, second.fence))
        self.assertRefused("lease-not-held", store.complete_processing_job, second.job_id, second.fence, revision(), at(22))
        self.assertEqual(self.outcomes(store, record_id), (recorded,))

    def test_each_outcome_kind_is_recorded(self) -> None:
        store = self.ready()
        longest = "r" + "-x" * 31 + "y"
        self.assertEqual(len(longest), 64)
        cases = (
            ("id-rev", revision(b"interval")),
            ("id-instant", OutcomeEntry("revision", PV, POLICY, None, None, None, instant_entry(b"instant"))),
            ("id-zero", revision(b"zero", end_epoch_ns=START_NS, start_attribution="inferred", end_attribution="inferred")),
            ("id-unmodeled", unmodeled(longest)),
            ("id-pending", policy_pending("synthetic-policy-needed", "synthetic-timezone")),
            ("id-quarantine", quarantine("synthetic-value-out-of-range", "/value/0")),
            ("id-root", quarantine("synthetic-not-an-object", "")),
        )
        recorded_kinds = []
        for index, (identity, entry) in enumerate(cases):
            record_id, _, lease = self.leased(store, identity, when=at(10 + index))
            recorded = store.complete_processing_job(lease.job_id, lease.fence, entry, at(10.5 + index))
            self.assertRecorded(recorded, entry, lease, at(10.5 + index))
            self.assertEqual(self.outcomes(store, record_id), (recorded,))
            self.assertEqual(self.job(store, lease.job_id).state, "completed")
            recorded_kinds.append(recorded.kind)
        self.assertEqual(recorded_kinds, ["revision", "revision", "revision", "unmodeled", "policy-pending", "quarantine", "quarantine"])


class R34OutcomeShape(StoreCase):
    def test_outcome_shapes_are_checked(self) -> None:
        store = self.ready()
        record_id, _, lease = self.leased(store, when=at(10))
        too_long = "r" + "-x" * 32
        self.assertEqual(len(too_long), 65)
        invalid = {
            "unknown kind": OutcomeEntry("synthetic-kind", PV, POLICY, "synthetic-reason", None, None, None),  # type: ignore[arg-type]
            "other processor version": revision(pv=PV2),
            "empty policy identity": revision(policy=""),
            "non-string policy identity": revision(policy=7),
            "revision missing": OutcomeEntry("revision", PV, POLICY, None, None, None, None),
            "revision on unmodeled": OutcomeEntry("unmodeled", PV, POLICY, "synthetic-reason", None, None, revision_entry()),
            "revision on policy-pending": OutcomeEntry("policy-pending", PV, POLICY, "synthetic-reason", None,
                                                       "synthetic-timezone", revision_entry()),
            "revision on quarantine": OutcomeEntry("quarantine", PV, POLICY, "synthetic-reason", "/value", None, revision_entry()),
            "empty header": revision(header_bytes=b""),
            "empty body": revision(body_bytes=b""),
            "body version 0": revision(body_version=0),
            "end before start": revision(end_epoch_ns=START_NS - 1),
            "only end instant": revision(end_local_date=None, end_attribution=None),
            "end date missing": revision(end_local_date=None),
            "end attribution missing": revision(end_attribution=None),
            "only end date": OutcomeEntry("revision", PV, POLICY, None, None, None, instant_entry(end_local_date=date(2026, 9, 21))),
            "only end attribution": OutcomeEntry("revision", PV, POLICY, None, None, None, instant_entry(end_attribution="experienced")),
            "unknown start attribution": revision(start_attribution="observed"),
            "unknown end attribution": revision(end_attribution="observed"),
            "start date text": revision(start_local_date="2026-09-21"),
            "end date text": revision(end_local_date="2026-09-21"),
            "reason on revision": OutcomeEntry("revision", PV, POLICY, "synthetic-reason", None, None, revision_entry()),
            "unmodeled without reason": unmodeled(None),
            "uppercase reason": unmodeled("Synthetic-Reason"),
            "underscore reason": unmodeled("synthetic_reason"),
            "spaced reason": unmodeled("synthetic reason"),
            "leading hyphen reason": unmodeled("-synthetic"),
            "trailing hyphen reason": unmodeled("synthetic-"),
            "empty reason": unmodeled(""),
            "long reason": unmodeled(too_long),
            "non-string reason": unmodeled(7),
            "policy-pending without reason": policy_pending(None),
            "quarantine without reason": quarantine(None),
            "quarantine without pointer": quarantine(pointer=None),
            "quarantine relative pointer": quarantine(pointer="value/0"),
            "policy-pending without policy kind": policy_pending(policy_kind=None),
            "policy-pending non-kebab policy kind": policy_pending(policy_kind="Synthetic_Timezone"),
            "unmodeled with policy kind": unmodeled(policy_kind="synthetic-timezone"),
            "quarantine with policy kind": quarantine(policy_kind="synthetic-timezone"),
            "revision with policy kind": OutcomeEntry("revision", PV, POLICY, None, None, "synthetic-timezone", revision_entry()),
        }
        for name, entry in invalid.items():
            with self.subTest(name):
                self.assertRefused("outcome-invalid", store.complete_processing_job, lease.job_id, lease.fence, entry, at(11))
        self.assertEqual(self.job(store, lease.job_id).state, "leased")
        self.assertEqual(self.outcomes(store, record_id), ())
        self.assertEqual(store.complete_processing_job(lease.job_id, lease.fence, revision(), at(12)).kind, "revision")

    def test_revision_digests_are_computed_by_the_store(self) -> None:
        store = self.ready()
        record_id, _, lease = self.leased(store, when=at(10))
        # Opaque bytes that are not JSON: the store never parses them.
        head, body = b"\x00\xffsynthetic header \xfe", b"\x01synthetic body that is not json{"
        recorded = store.complete_processing_job(lease.job_id, lease.fence,
                                                 revision(header_bytes=head, body_bytes=body, body_version=7), at(11))
        stored = recorded.revision
        assert stored is not None
        self.assertEqual((stored.header_bytes, stored.body_bytes, stored.body_version), (head, body, 7))
        self.assertEqual((stored.header_sha256, stored.body_sha256), (digest(head), digest(body)))
        (read,) = self.outcomes(store, record_id)
        self.assertEqual(read.revision, stored)

    def test_outcomes_are_append_only(self) -> None:
        store = self.ready()
        record_id, selection_id, lease = self.leased(store, when=at(10))
        first = store.complete_processing_job(lease.job_id, lease.fence, revision(b"one"), at(11))
        history = [first]
        for step, entry in enumerate((revision(b"two", policy=OTHER_POLICY), unmodeled(policy="synthetic-policy-3"))):
            store.enqueue_processing(record_id, selection_id, PV, 3, at(12 + step))
            again = store.claim_processing_job(OWNER, LEASE, at(12 + step))
            assert again is not None
            self.assertRefused("outcome-conflict", store.complete_processing_job, again.job_id, again.fence, unmodeled(), at(12 + step))
            history.append(store.complete_processing_job(again.job_id, again.fence, entry, at(12.5 + step)))
            self.assertEqual(self.outcomes(store, record_id), tuple(history))
        _, newer = self.project(store, "id-a")
        store.enqueue_processing(record_id, newer, PV, 3, at(20))
        latest = store.claim_processing_job(OWNER, LEASE, at(20))
        assert latest is not None
        history.append(store.complete_processing_job(latest.job_id, latest.fence, revision(b"one"), at(21)))
        self.retract(store, "id-a")
        self.assertEqual(self.outcomes(store, record_id), tuple(history))
        self.assertEqual(len({item.outcome_id for item in history}), len(history))
        self.assertEqual([item.outcome_id for item in history], sorted(item.outcome_id for item in history))


class R35FailureRetryAndBackoff(StoreCase):
    def test_transient_failure_requeues_after_backoff(self) -> None:
        store = self.ready()
        record_id, selection_id, lease = self.leased(store, max_attempts=3, when=at(10))
        job = store.fail_processing_job(lease.job_id, lease.fence, "transient", "synthetic-timeout", at(25), at(11))
        self.assertEqual(job, self.job(store, lease.job_id))
        self.assertEqual(
            (job.state, job.not_before, job.attempts, job.failure, job.closed_at, job.lease_owner, job.lease_expires_at,
             job.selection_id),
            ("queued", at(25), 1, None, None, None, None, selection_id),
        )
        self.assertEqual([(attempt.fence, attempt.result, attempt.reason, attempt.finished_at) for attempt in job.history],
                         [(lease.fence, "transient-failure", "synthetic-timeout", at(11))])
        self.assertIsNone(store.claim_processing_job(OWNER, LEASE, at(24)))
        retried = store.claim_processing_job(OWNER, LEASE, at(25))
        assert retried is not None
        self.assertEqual(retried.job_id, lease.job_id)
        self.assertGreater(retried.fence, lease.fence)
        self.assertEqual(self.job(store, lease.job_id).attempts, 2)
        # retry_at may equal at.
        now = store.fail_processing_job(retried.job_id, retried.fence, "transient", "synthetic-busy", at(26), at(26))
        self.assertEqual((now.state, now.not_before), ("queued", at(26)))

    def test_retries_are_bounded(self) -> None:
        store = self.ready()
        _, _, lease = self.leased(store, max_attempts=2, when=at(10))
        store.fail_processing_job(lease.job_id, lease.fence, "transient", "synthetic-timeout", at(11), at(11))
        second = store.claim_processing_job(OWNER, LEASE, at(11))
        assert second is not None
        failed = store.fail_processing_job(second.job_id, second.fence, "transient", "synthetic-timeout", at(12), at(12))
        self.assertEqual((failed.state, failed.failure, failed.attempts, failed.closed_at), ("failed", "retries-exhausted", 2, at(12)))
        self.assertEqual([attempt.result for attempt in failed.history], ["transient-failure", "transient-failure"])
        self.assertIsNone(store.claim_processing_job(OWNER, LEASE, at(13)))
        # A reclaimed expired lease used an attempt too.
        _, _, first = self.leased(store, "id-b", max_attempts=2, when=at(20))
        reclaimed = store.claim_processing_job(OWNER, LEASE, at(30))
        assert reclaimed is not None
        self.assertEqual(reclaimed.job_id, first.job_id)
        exhausted = store.fail_processing_job(reclaimed.job_id, reclaimed.fence, "transient", "synthetic-timeout", at(31), at(31))
        self.assertEqual((exhausted.state, exhausted.failure), ("failed", "retries-exhausted"))
        self.assertEqual([attempt.result for attempt in exhausted.history], ["lease-expired", "transient-failure"])
        # A failure on an expired lease that no claim took is still held.
        _, _, late = self.leased(store, "id-c", max_attempts=3, when=at(40))
        self.assertEqual(store.fail_processing_job(late.job_id, late.fence, "transient", "synthetic-timeout", at(60), at(50)).state, "queued")

    def test_deterministic_failure_is_terminal(self) -> None:
        store = self.ready()
        _, _, lease = self.leased(store, max_attempts=5, when=at(10))
        failed = store.fail_processing_job(lease.job_id, lease.fence, "deterministic", "synthetic-unsupported-shape", None, at(11))
        self.assertEqual((failed.state, failed.failure, failed.attempts, failed.closed_at), ("failed", "synthetic-unsupported-shape", 1, at(11)))
        self.assertEqual([(attempt.result, attempt.reason, attempt.finished_at) for attempt in failed.history],
                         [("deterministic-failure", "synthetic-unsupported-shape", at(11))])
        self.assertEqual(self.job(store, lease.job_id), failed)
        self.assertIsNone(store.claim_processing_job(OWNER, LEASE, at(12)))
        self.assertRefused("lease-not-held", store.fail_processing_job, lease.job_id, lease.fence, "deterministic",
                           "synthetic-unsupported-shape", None, at(13))

    def test_failure_refusals(self) -> None:
        store = self.ready()
        record_id, selection_id, lease = self.leased(store, when=at(10))
        too_long = "r" + "-x" * 32
        args = (lease.job_id, lease.fence)
        self.assertRefused("processing-job-unknown", store.fail_processing_job, 999_999, lease.fence, "transient",
                           "synthetic-timeout", at(12), at(11))
        self.assertRefused("lease-not-held", store.fail_processing_job, lease.job_id, lease.fence + 1, "transient",
                           "synthetic-timeout", at(12), at(11))
        for kind, reason, retry_at in (
            ("fatal", "synthetic-timeout", at(12)),
            ("transient", "Synthetic-Timeout", at(12)),
            ("transient", "synthetic_timeout", at(12)),
            ("transient", "", at(12)),
            ("transient", too_long, at(12)),
            ("transient", None, at(12)),
            ("transient", "synthetic-timeout", None),
            ("transient", "synthetic-timeout", after(at(11), -0.000001)),
            ("deterministic", "synthetic-defect", at(12)),
            ("deterministic", "synthetic defect", None),
        ):
            with self.subTest(kind=kind, reason=reason, retry_at=retry_at):
                self.assertRefused("processing-request-invalid", store.fail_processing_job, *args, kind, reason, retry_at, at(11))
        self.assertRefused("timestamp-invalid", store.fail_processing_job, *args, "transient", "synthetic-timeout", at(12), NAIVE)
        self.assertRefused("timestamp-invalid", store.fail_processing_job, *args, "transient", "synthetic-timeout", NAIVE, at(11))
        self.free = RESERVE - 1
        self.assertRetryable("storage-reserve-exhausted", store.fail_processing_job, *args, "transient", "synthetic-timeout", at(12), at(11))
        self.free = RESERVE
        self.assertEqual(store.fail_processing_job(*args, "transient", "synthetic-timeout", at(12), at(11)).state, "queued")
        # A queued job holds no lease.
        self.assertRefused("lease-not-held", store.fail_processing_job, *args, "transient", "synthetic-timeout", at(13), at(12))

    def test_failed_job_is_not_retried_automatically(self) -> None:
        store = self.ready()
        record_id, selection_id, lease = self.leased(store, max_attempts=5, when=at(10))
        failed = store.fail_processing_job(lease.job_id, lease.fence, "deterministic", "synthetic-defect", None, at(11))
        for minutes in (12, 600, 60_000):
            self.assertIsNone(store.claim_processing_job(OWNER, LEASE, at(minutes)))
        self.assertEqual(self.job(store, lease.job_id), failed)
        self.assertEqual(self.awaiting(store, {"steps": PV}), ())
        self.assertEqual(self.awaiting(store, {"steps": PV2}), (AwaitingProcessing(record_id, selection_id, PV2),))
        # A manual retry enqueues a new job; the failed one stays failed.
        retry = store.enqueue_processing(record_id, selection_id, PV, 3, at(70_000))
        self.assertNotEqual(retry.job_id, failed.job_id)
        self.assertEqual(self.job(store, failed.job_id), failed)


class R36ItemsThatCannotBeIndexed(StoreCase):
    def archived(self, store):
        """Five items: projections at 0, 1, 2, and 4, and a retraction at 3. Returns the sequence."""
        return self.archive(store, BatchItem(projection=b'{"synthetic":0}'), BatchItem(projection=b"\xffnot-json"),
                            BatchItem(projection=b'{"synthetic":2}'), BatchItem(retracted_identity="id-gone"),
                            BatchItem(projection=b'{"synthetic":4}'))

    def test_quarantined_item_leaves_items_awaiting_index(self) -> None:
        store = self.ready()
        sequence = self.archived(store)
        recorded = store.quarantine_item(position(sequence, 1), "synthetic-not-json", "", at(20))
        self.assertEqual(recorded, ItemQuarantine(position(sequence, 1), "synthetic-not-json", "", at(20)))
        store.quarantine_item(position(sequence, 0), "synthetic-identity-missing", "/metadata/id", at(21))
        with store.read() as view:
            pending = view.pending_items(100)
            awaiting = view.items_awaiting_index(100)
            self.assertEqual(view.item_quarantine(position(sequence, 1)), recorded)
            self.assertIsNone(view.item_quarantine(position(sequence, 2)))
            self.assertIsNone(view.item_quarantine(position(99, 0)))
            # The limit applies after quarantined items are left out.
            self.assertEqual(view.items_awaiting_index(1), awaiting[:1])
            self.assertEqual(view.items_awaiting_index(2), awaiting[:2])
        self.assertEqual([item.position for item in awaiting], [position(sequence, 2), position(sequence, 3), position(sequence, 4)])
        self.assertEqual(awaiting, tuple(item for item in pending if item.position not in (position(sequence, 0), position(sequence, 1))))

    def test_pending_items_still_lists_quarantined_items(self) -> None:
        store = self.ready()
        sequence = self.archived(store)
        with store.read() as view:
            before = (view.pending_items(100), view.index_counts())
        store.quarantine_item(position(sequence, 1), "synthetic-not-json", "", at(20))
        with store.read() as view:
            self.assertEqual((view.pending_items(100), view.index_counts()), before)
        self.assertEqual(len(before[0]), 5)
        self.assertEqual(before[1].unindexed_items, 5)

    def test_item_quarantine_refusals(self) -> None:
        store = self.ready()
        sequence = self.archived(store)
        store.index_items((ProjectionEntry(position(sequence, 2), HC, ORIGIN, "id-indexed", at(1)),), at(19))
        store.quarantine_item(position(sequence, 1), "synthetic-not-json", "", at(20))
        too_long = "r" + "-x" * 32
        self.assertRefused("timestamp-invalid", store.quarantine_item, position(sequence, 0), "synthetic-not-json", "", NAIVE)
        for reason, pointer in (("Synthetic", ""), ("synthetic_reason", ""), ("", ""), (too_long, ""), (None, ""), (7, ""),
                                ("synthetic-not-json", "metadata/id"), ("synthetic-not-json", None), ("synthetic-not-json", 7)):
            with self.subTest(reason=reason, pointer=pointer):
                self.assertRefused("processing-request-invalid", store.quarantine_item, position(sequence, 0), reason, pointer, at(21))
        self.assertRefused("archived-item-unknown", store.quarantine_item, position(sequence, 9), "synthetic-not-json", "", at(21))
        self.assertRefused("archived-item-unknown", store.quarantine_item, position(sequence + 1, 0), "synthetic-not-json", "", at(21))
        self.assertRefused("index-entry-mismatch", store.quarantine_item, position(sequence, 3), "synthetic-not-json", "", at(21))
        self.assertRefused("item-already-indexed", store.quarantine_item, position(sequence, 2), "synthetic-not-json", "", at(21))
        self.assertRefused("item-quarantine-conflict", store.quarantine_item, position(sequence, 1), "synthetic-other", "", at(21))
        self.assertRefused("item-quarantine-conflict", store.quarantine_item, position(sequence, 1), "synthetic-not-json", "/x", at(21))
        self.free = RESERVE - 1
        self.assertRetryable("storage-reserve-exhausted", store.quarantine_item, position(sequence, 0), "synthetic-not-json", "", at(21))
        self.free = RESERVE
        self.assertEqual(store.quarantine_item(position(sequence, 0), "synthetic-not-json", "/0", at(21)).pointer, "/0")

    def test_identical_item_quarantine_writes_nothing(self) -> None:
        store = self.ready()
        sequence = self.archived(store)
        recorded = store.quarantine_item(position(sequence, 1), "synthetic-not-json", "/a~1b", at(20))
        self.free = RESERVE - 1
        self.assertEqual(self.assertWritesNothing(store.quarantine_item, position(sequence, 1), "synthetic-not-json", "/a~1b", at(30)),
                         recorded)

    def test_quarantined_item_can_still_be_indexed(self) -> None:
        store = self.ready()
        sequence = self.archived(store)
        recorded = store.quarantine_item(position(sequence, 1), "synthetic-not-json", "", at(20))
        store.index_items((ProjectionEntry(position(sequence, 1), HC, ORIGIN, "id-corrected", at(1)),), at(21))
        with store.read() as view:
            self.assertNotIn(position(sequence, 1), [item.position for item in view.pending_items(100)])
            self.assertNotIn(position(sequence, 1), [item.position for item in view.items_awaiting_index(100)])
            self.assertEqual(view.item_quarantine(position(sequence, 1)), recorded)
            self.assertEqual(view.index_counts().unindexed_items, 4)
        self.assertEqual(len(self.record(store, "id-corrected").evidence), 1)


class R37ProcessingReads(StoreCase):
    def test_job_read_includes_history(self) -> None:
        store = self.ready()
        record_id, selection_id, first = self.leased(store, max_attempts=3, when=at(0))
        second = store.claim_processing_job(OTHER_OWNER, 120, at(5))
        assert second is not None
        store.fail_processing_job(second.job_id, second.fence, "transient", "synthetic-timeout", at(7), at(6))
        third = store.claim_processing_job(OWNER, 30, at(7))
        assert third is not None
        store.complete_processing_job(third.job_id, third.fence, revision(), at(8))
        job = self.job(store, first.job_id)
        self.assertEqual((job.state, job.attempts, job.fence, job.max_attempts, job.closed_at), ("completed", 3, third.fence, 3, at(8)))
        self.assertLess(first.fence, second.fence)
        self.assertLess(second.fence, third.fence)
        self.assertEqual([(a.fence, a.owner, a.claimed_at, a.expires_at, a.result) for a in job.history], [
            (first.fence, OWNER, at(0), after(at(0), LEASE), "lease-expired"),
            (second.fence, OTHER_OWNER, at(5), after(at(5), 120), "transient-failure"),
            (third.fence, OWNER, at(7), after(at(7), 30), "completed"),
        ])
        self.assertEqual([(a.finished_at, a.reason) for a in job.history[1:]], [(at(6), "synthetic-timeout"), (at(8), None)])
        other = store.enqueue_processing(record_id, selection_id, PV2, 2, at(9))
        with store.read() as view:
            self.assertEqual(view.processing_jobs(record_id), (job, other))
            self.assertIsNone(view.processing_job(999_999))
            self.assertEqual(view.processing_jobs(999_999), ())
        for value in (job, other) + job.history:
            with self.subTest(type(value).__name__), self.assertRaises(Exception):
                setattr(value, fields(value)[0].name, None)

    def test_outcomes_span_processor_versions(self) -> None:
        store = self.ready()
        record_id, selection_id, lease = self.leased(store, when=at(10))
        first = store.complete_processing_job(lease.job_id, lease.fence, revision(b"v1"), at(11))
        store.enqueue_processing(record_id, selection_id, PV2, 3, at(12))
        other = store.claim_processing_job(OWNER, LEASE, at(12))
        assert other is not None
        second = store.complete_processing_job(other.job_id, other.fence, unmodeled(pv=PV2), at(13))
        _, newer = self.project(store, "id-a")
        store.enqueue_processing(record_id, newer, PV, 3, at(14))
        third_lease = store.claim_processing_job(OWNER, LEASE, at(14))
        assert third_lease is not None
        third = store.complete_processing_job(third_lease.job_id, third_lease.fence, revision(b"v1-newer"), at(15))
        b, _, b_lease = self.leased(store, "id-b", when=at(16))
        store.complete_processing_job(b_lease.job_id, b_lease.fence, revision(b"b"), at(17))
        outcomes = self.outcomes(store, record_id)
        self.assertEqual(outcomes, (first, second, third))
        self.assertEqual([(item.processor_version, item.selection_id) for item in outcomes],
                         [(PV, selection_id), (PV2, selection_id), (PV, newer)])
        self.assertEqual(outcomes[0].revision.body_bytes, b'{"synthetic-body":"v1"}')  # type: ignore[union-attr]
        with store.read() as view:
            self.assertEqual(view.processing_outcomes(999_999), ())
            self.assertEqual(len(view.processing_outcomes(b)), 1)

    def test_latest_outcome_follows_the_latest_selection(self) -> None:
        store = self.ready()
        record_id, selection_id, lease = self.leased(store, when=at(10))
        first = store.complete_processing_job(lease.job_id, lease.fence, revision(b"p1"), at(11))
        store.enqueue_processing(record_id, selection_id, PV, 3, at(12))
        again = store.claim_processing_job(OWNER, LEASE, at(12))
        assert again is not None
        second = store.complete_processing_job(again.job_id, again.fence, revision(b"p2", policy=OTHER_POLICY), at(13))
        with store.read() as view:
            self.assertEqual(view.latest_outcome(record_id, PV), second)
            self.assertIsNone(view.latest_outcome(record_id, PV2))
            self.assertIsNone(view.latest_outcome(999_999, PV))
        self.assertNotEqual(first, second)
        _, newer = self.project(store, "id-a")
        with store.read() as view:
            self.assertIsNone(view.latest_outcome(record_id, PV))
        store.enqueue_processing(record_id, newer, PV, 3, at(14))
        third_lease = store.claim_processing_job(OWNER, LEASE, at(14))
        assert third_lease is not None
        third = store.complete_processing_job(third_lease.job_id, third_lease.fence, unmodeled(), at(15))
        with store.read() as view:
            self.assertEqual(view.latest_outcome(record_id, PV), third)
        self.retract(store, "id-a")
        with store.read() as view:
            self.assertIsNone(view.latest_outcome(record_id, PV))

    def complete(self, store, record_id: int, selection_id: int, entry: OutcomeEntry, when: datetime) -> ProcessingOutcome:
        store.enqueue_processing(record_id, selection_id, entry.processor_version, 3, when)
        lease = store.claim_processing_job(OWNER, LEASE, when)
        assert lease is not None and lease.record_id == record_id
        return store.complete_processing_job(lease.job_id, lease.fence, entry, when)

    def test_selections_awaiting_processing(self) -> None:
        store = self.ready()
        mapping = {"steps": PV, "weight": WPV}
        plain, plain_selection = self.project(store, "id-plain")
        retracted, _ = self.project(store, "id-retracted")
        self.retract(store, "id-retracted")
        conflicted, _ = self.project(store, "id-conflicted")
        self.conflict(store, "id-conflicted")
        weight, weight_selection = self.project(store, "id-weight", "weight")
        sleep, _ = self.project(store, "id-sleep", "sleep")
        done, done_selection = self.project(store, "id-done")
        self.complete(store, done, done_selection, unmodeled(policy=OTHER_POLICY), at(10))
        other_version, other_selection = self.project(store, "id-other-version")
        self.complete(store, other_version, other_selection, unmodeled(pv=PV2), at(11))
        older, older_selection = self.project(store, "id-older")
        self.complete(store, older, older_selection, revision(), at(12))
        _, newer_selection = self.project(store, "id-older")
        sequence = self.archive(store, BatchItem(projection=b'{"synthetic":"unselected"}'))
        store.index_items((ProjectionEntry(position(sequence, 0), HC, ORIGIN, "id-unselected", at(1)),), at(13))
        expected = (
            AwaitingProcessing(plain, plain_selection, PV),
            AwaitingProcessing(weight, weight_selection, WPV),
            AwaitingProcessing(other_version, other_selection, PV),
            AwaitingProcessing(older, newer_selection, PV),
        )
        self.assertEqual(self.awaiting(store, mapping), expected)
        self.assertEqual([item.record_id for item in expected], sorted(item.record_id for item in expected))
        self.assertEqual(self.awaiting(store, mapping, 2), expected[:2])
        self.assertEqual(self.awaiting(store, {"steps": PV2}), (
            AwaitingProcessing(plain, plain_selection, PV2),
            AwaitingProcessing(done, done_selection, PV2),
            AwaitingProcessing(older, newer_selection, PV2),
        ))
        self.assertEqual(self.awaiting(store, {"sleep": "interpret-sleep@1"}),
                         (AwaitingProcessing(sleep, self.record(store, "id-sleep", "sleep").selections[-1].selection_id, "interpret-sleep@1"),))
        self.assertEqual(self.awaiting(store, {}), ())
        self.assertNotIn(retracted, [item.record_id for item in self.awaiting(store, mapping)])
        self.assertNotIn(conflicted, [item.record_id for item in self.awaiting(store, mapping)])

    def test_failed_and_open_work_is_not_awaiting(self) -> None:
        store = self.ready()
        mapping = {"steps": PV}
        queued, queued_selection = self.project(store, "id-queued")
        leased, _ = self.project(store, "id-leased")
        failed, failed_selection = self.project(store, "id-failed")
        refailed, _ = self.project(store, "id-refailed")
        superseded, _ = self.project(store, "id-superseded")
        completed, completed_selection = self.project(store, "id-completed")
        requeued, _ = self.project(store, "id-requeued")
        with store.read() as view:
            self.assertEqual(len(view.selections_awaiting_processing(mapping, 100)), 7)

        def claim(record_id: int, when: datetime, lease_seconds: int = LEASE) -> Lease:
            selection_id = self.record_selection(store, record_id)
            store.enqueue_processing(record_id, selection_id, PV, 3, when)
            lease = store.claim_processing_job(OWNER, lease_seconds, when)
            assert lease is not None and lease.record_id == record_id
            return lease

        claim(leased, at(10), 86_400)
        lease = claim(failed, at(11))
        store.fail_processing_job(lease.job_id, lease.fence, "deterministic", "synthetic-defect", None, at(11))
        lease = claim(refailed, at(12))
        store.fail_processing_job(lease.job_id, lease.fence, "deterministic", "synthetic-defect", None, at(12))
        _, refailed_newer = self.project(store, "id-refailed")
        store.enqueue_processing(superseded, self.record_selection(store, superseded), PV, 3, at(13))
        self.retract(store, "id-superseded")
        self.assertIsNone(store.claim_processing_job(OWNER, LEASE, at(13)))
        _, superseded_newer = self.project(store, "id-superseded")
        lease = claim(completed, at(14))
        store.complete_processing_job(lease.job_id, lease.fence, revision(), at(14))
        lease = claim(requeued, at(15))
        self.project(store, "id-requeued")
        store.complete_processing_job(lease.job_id, lease.fence, revision(), at(15))
        self.assertEqual(self.jobs(store, requeued)[0].state, "queued")
        store.enqueue_processing(queued, queued_selection, PV, 3, at(16))
        self.assertEqual(self.awaiting(store, mapping), (
            AwaitingProcessing(refailed, refailed_newer, PV),
            AwaitingProcessing(superseded, superseded_newer, PV),
        ))
        self.assertIn(AwaitingProcessing(queued, queued_selection, PV2), self.awaiting(store, {"steps": PV2}))
        self.assertIn(AwaitingProcessing(failed, failed_selection, PV2), self.awaiting(store, {"steps": PV2}))
        self.assertIn(AwaitingProcessing(completed, completed_selection, PV2), self.awaiting(store, {"steps": PV2}))

    def record_selection(self, store, record_id: int) -> int:
        with store.read() as view:
            found = view.source_record(record_id)
        assert found is not None
        return found.selections[-1].selection_id

    def test_projection_bytes_by_evidence(self) -> None:
        store = self.ready()
        payload = b'{"synthetic-projection":"exact bytes"}'
        record_id, selection_id = self.project(store, "id-a", payload=payload)
        self.retract(store, "id-a")
        found = self.record(store, "id-a")
        projected, retracted = found.evidence
        with store.read() as view:
            self.assertEqual(view.projection_bytes(projected.evidence_id), payload)
            self.assertEqual(view.projection_bytes(self.evidence_of(store, record_id, selection_id)), payload)
            self.assertIsNone(view.projection_bytes(retracted.evidence_id))
            self.assertIsNone(view.projection_bytes(999_999))

    def test_processing_reads_write_nothing(self) -> None:
        store = self.ready()
        record_id, selection_id, lease = self.leased(store, when=at(10))
        store.complete_processing_job(lease.job_id, lease.fence, revision(), at(11))
        b, _, b_lease = self.leased(store, "id-b", when=at(12))
        sequence = self.archive(store, BatchItem(projection=b"\xff"), BatchItem(projection=b"{}"))
        store.quarantine_item(position(sequence, 0), "synthetic-not-json", "", at(13))
        before = contents(self.path)
        with store.read() as view:
            first = (view.processing_job(lease.job_id), view.processing_jobs(record_id), view.processing_outcomes(record_id),
                     view.latest_outcome(record_id, PV), view.selections_awaiting_processing({"steps": PV}, 10),
                     view.items_awaiting_index(10), view.item_quarantine(position(sequence, 0)),
                     view.projection_bytes(b_lease.evidence_id), view.processing_counts(at(14)))
            view.processing_job(999_999)
            view.processing_jobs(999_999)
            view.latest_outcome(999_999, PV)
            view.item_quarantine(position(99, 0))
            self.assertEqual(contents(self.path), before)
            # Each read comes from one read transaction: a later commit is invisible to this view.
            store.complete_processing_job(b_lease.job_id, b_lease.fence, unmodeled(), at(15))
            store.quarantine_item(position(sequence, 1), "synthetic-not-json", "", at(15))
            self.assertEqual(view.processing_job(b_lease.job_id).state, "leased")  # type: ignore[union-attr]
            self.assertEqual(view.processing_outcomes(b), ())
            self.assertEqual(view.items_awaiting_index(10), first[5])
            self.assertEqual(view.processing_counts(at(14)), first[8])
        with store.read() as view:
            self.assertEqual(view.processing_job(b_lease.job_id).state, "completed")  # type: ignore[union-attr]
        self.assertIsNotNone(first[0])


class R38HealthDataFreeCountsAndRefusals(StoreCase):
    def test_processing_counts(self) -> None:
        store = self.ready()
        record = {}
        for identity in ("rev", "unm", "pend", "quar", "failed", "superseded", "delayed", "expired", "boundary", "held",
                         "ready", "ready-boundary"):
            record[identity] = self.project(store, "id-" + identity)

        def claim(identity: str, when: datetime, lease_seconds: int = LEASE) -> Lease:
            store.enqueue_processing(*record[identity], PV, 3, when)
            lease = store.claim_processing_job(OWNER, lease_seconds, when)
            assert lease is not None and lease.record_id == record[identity][0], identity
            return lease

        for identity, entry in (("rev", revision()), ("unm", unmodeled()), ("pend", policy_pending()), ("quar", quarantine())):
            lease = claim(identity, at(10))
            store.complete_processing_job(lease.job_id, lease.fence, entry, at(10))
        lease = claim("failed", at(10))
        store.fail_processing_job(lease.job_id, lease.fence, "deterministic", "synthetic-defect", None, at(10))
        store.enqueue_processing(*record["superseded"], PV, 3, at(10))
        self.retract(store, "id-superseded")
        self.assertIsNone(store.claim_processing_job(OWNER, LEASE, at(10)))
        lease = claim("delayed", at(10))
        store.fail_processing_job(lease.job_id, lease.fence, "transient", "synthetic-timeout", at(200), at(10))
        claim("expired", at(95), 120)          # expires at(97)
        claim("boundary", at(96), 240)         # expires at(100) exactly
        claim("held", at(96.5), 600)           # expires at(106.5)
        store.enqueue_processing(*record["ready"], PV, 3, at(99))
        store.enqueue_processing(*record["ready-boundary"], PV, 3, at(100))
        sequence = self.archive(store, BatchItem(projection=b"\xff"), BatchItem(projection=b"{}"), BatchItem(projection=b"[]"))
        store.quarantine_item(position(sequence, 1), "synthetic-not-json", "", at(10))
        with store.read() as view:
            now = view.processing_counts(at(100))
            early = view.processing_counts(at(0))
            late = view.processing_counts(at(300))
        fixed = {"jobs_completed": 4, "jobs_failed": 1, "jobs_superseded": 1, "revisions": 1, "unmodeled": 1,
                 "policy_pending": 1, "quarantined": 1, "quarantined_items": 1, "items_awaiting_index": 2}
        self.assertEqual(counts(now), {"jobs_ready": 2, "jobs_delayed": 1, "jobs_leased": 1, "jobs_lease_expired": 2, **fixed})
        self.assertEqual(counts(early), {"jobs_ready": 0, "jobs_delayed": 3, "jobs_leased": 3, "jobs_lease_expired": 0, **fixed})
        self.assertEqual(counts(late), {"jobs_ready": 3, "jobs_delayed": 0, "jobs_leased": 0, "jobs_lease_expired": 3, **fixed})
        for value in (now, early, late):
            for field in fields(value):
                self.assertIs(type(getattr(value, field.name)), int, field.name)
        self.assertLessEqual(set(COUNT_FIELDS), {field.name for field in fields(ProcessingCounts)})

    def test_processing_surfaces_are_health_data_free(self) -> None:
        store = self.ready()
        record_id, selection_id = self.project(store, IDENTITY_MARKER, payload=b'{"' + HEALTH_MARKER + b'":1}')
        store.enqueue_processing(record_id, selection_id, PV, 3, at(10))
        lease = store.claim_processing_job(OWNER, LEASE, at(10))
        assert lease is not None
        marked = revision(header_bytes=b'{"h":"' + HEADER_MARKER + b'"}', body_bytes=b'{"b":"' + BODY_MARKER + b'"}')
        texts = [repr(lease), repr(marked), repr(marked.revision)]
        self.assertRefused("outcome-invalid", store.complete_processing_job, lease.job_id, lease.fence,
                           revision(header_bytes=b'{"h":"' + HEADER_MARKER + b'"}', body_bytes=b'{"b":"' + BODY_MARKER + b'"}', end_epoch_ns=0), at(11))
        recorded = store.complete_processing_job(lease.job_id, lease.fence, marked, at(11))
        texts += [repr(recorded), repr(recorded.revision)]
        store.enqueue_processing(record_id, selection_id, PV, 3, at(12))
        again = store.claim_processing_job(OWNER, LEASE, at(12))
        assert again is not None
        refusals = [
            lambda: store.complete_processing_job(again.job_id, again.fence, revision(header_bytes=b"[" + HEADER_MARKER + b"]",
                                                                                      body_bytes=b"[" + BODY_MARKER + b"]"), at(13)),
            lambda: store.complete_processing_job(again.job_id, again.fence + 7, marked, at(13)),
            lambda: store.complete_processing_job(again.job_id, again.fence, revision(header_bytes=HEADER_MARKER, body_bytes=b""), at(13)),
            lambda: store.enqueue_processing(record_id, 999_999, PV2, 3, at(13)),
            lambda: store.renew_processing_lease(999_999, 1, LEASE, at(13)),
            lambda: store.fail_processing_job(again.job_id, again.fence, "transient", IDENTITY_MARKER.upper(), at(14), at(13)),
        ]
        sequence = self.archive(store, BatchItem(projection=b'{"' + HEALTH_MARKER + b'":2}'), BatchItem(retracted_identity=IDENTITY_MARKER))
        quarantined = store.quarantine_item(position(sequence, 0), "synthetic-not-json", "/" + "x", at(13))
        refusals += [
            lambda: store.quarantine_item(position(sequence, 0), "synthetic-other", "", at(14)),
            lambda: store.quarantine_item(position(sequence, 1), "synthetic-not-json", "", at(14)),
        ]
        for refusal in refusals:
            with self.assertRaises(StoreRefusal) as caught:
                refusal()
            texts += [str(caught.exception), repr(caught.exception)]
        with store.read() as view:
            texts += [repr(view.processing_job(lease.job_id)), repr(view.processing_jobs(record_id)),
                      repr(view.processing_outcomes(record_id)), repr(view.latest_outcome(record_id, PV)),
                      repr(view.selections_awaiting_processing({"steps": PV2}, 10)), repr(view.processing_counts(at(14))),
                      repr(view.item_quarantine(position(sequence, 0))), repr(quarantined)]
        self.assertGreater(len(texts), 20)
        for text in texts:
            for marker in (HEALTH_MARKER.decode(), IDENTITY_MARKER, HEADER_MARKER.decode(), BODY_MARKER.decode()):
                self.assertNotIn(marker, text)
        # The bytes are still recorded exactly.
        self.assertEqual(recorded.revision.header_bytes, marked.revision.header_bytes)  # type: ignore[union-attr]


class R38CounterPopulations(StoreCase):
    def test_processing_counts_count_history(self) -> None:
        # Outcome counters count every recorded outcome; job counters count jobs by
        # their current state, with `at` splitting only queued and leased jobs;
        # quarantined_items counts every item quarantine, including items indexed later.
        store = self.ready()
        record_id, selection_id, lease = self.leased(store, when=at(10))
        store.complete_processing_job(lease.job_id, lease.fence, revision(), at(10))
        for entry in (unmodeled(policy=OTHER_POLICY), policy_pending(pv=PV2)):
            store.enqueue_processing(record_id, selection_id, entry.processor_version, 3, at(11))
            again = store.claim_processing_job(OWNER, LEASE, at(11))
            assert again is not None and again.record_id == record_id
            store.complete_processing_job(again.job_id, again.fence, entry, at(11))
        _, newer = self.project(store, "id-a")
        store.enqueue_processing(record_id, newer, PV, 3, at(12))
        latest = store.claim_processing_job(OWNER, LEASE, at(12))
        assert latest is not None
        store.complete_processing_job(latest.job_id, latest.fence, quarantine(), at(12))
        self.retract(store, "id-a")
        b, b_selection, b_lease = self.leased(store, "id-b", when=at(13))
        store.fail_processing_job(b_lease.job_id, b_lease.fence, "deterministic", "synthetic-defect", None, at(13))
        store.enqueue_processing(b, b_selection, PV, 3, at(14))
        retry = store.claim_processing_job(OWNER, LEASE, at(14))
        assert retry is not None
        store.complete_processing_job(retry.job_id, retry.fence, revision(b"b"), at(14))
        sequence = self.archive(store, BatchItem(projection=b"\xff"), BatchItem(projection=b"{}"))
        store.quarantine_item(position(sequence, 0), "synthetic-not-json", "", at(15))
        store.index_items((ProjectionEntry(position(sequence, 0), HC, ORIGIN, "id-corrected", at(1)),), at(16))
        self.assertEqual(len(self.outcomes(store, record_id)), 4)
        expected = {"jobs_ready": 0, "jobs_delayed": 0, "jobs_leased": 0, "jobs_lease_expired": 0, "jobs_completed": 5,
                    "jobs_failed": 1, "jobs_superseded": 0, "revisions": 2, "unmodeled": 1, "policy_pending": 1,
                    "quarantined": 1, "quarantined_items": 1, "items_awaiting_index": 1}
        with store.read() as view:
            for when in (at(0), at(100)):
                self.assertEqual(counts(view.processing_counts(when)), expected, when)


class R39AtomicAndDurable(StoreCase):
    def install_fault_triggers(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        self.assertLess(tables_created_by(2), set(tables))
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

    def assert_every_partial_failure_commits_nothing(self, faults: sqlite3.Connection, operation):
        failures = 0
        for writes_allowed in range(1, 200):
            faults.execute("UPDATE test_fault SET remaining = ?", (writes_allowed,))
            before = contents(self.path)
            try:
                result = operation()
            except (StoreRefusal, StoreRetryable):
                raise
            except Exception:
                failures += 1
                after_fault = contents(self.path)
                after_fault["tables"].pop("test_fault", None)  # type: ignore[union-attr]
                before["tables"].pop("test_fault", None)  # type: ignore[union-attr]
                self.assertEqual(after_fault, before, f"partial commit after {writes_allowed} writes")
                continue
            break
        else:
            self.fail("operation never completed")
        faults.execute("UPDATE test_fault SET remaining = 1000000")
        self.assertGreater(failures, 0)
        return result

    def test_processing_writes_are_atomic_under_injected_failure(self) -> None:
        store = self.ready()
        made = {identity: self.project(store, identity) for identity in ("id-exhausted", "id-retracted", "id-a", "id-d")}
        exhausted = store.enqueue_processing(*made["id-exhausted"], PV, 1, at(10))
        self.assertIsNotNone(store.claim_processing_job(OWNER, LEASE, at(10)))
        superseded = store.enqueue_processing(*made["id-retracted"], PV, 3, at(10))
        self.retract(store, "id-retracted")
        sequence = self.archive(store, BatchItem(projection=b"\xff"))
        faults = self.install_fault_triggers()
        check = self.assert_every_partial_failure_commits_nothing
        a = check(faults, lambda: store.enqueue_processing(*made["id-a"], PV, 3, at(20)))
        # One claim fails the exhausted job, supersedes the retracted one, and leases the third.
        lease = check(faults, lambda: store.claim_processing_job(OWNER, LEASE, at(21)))
        self.assertEqual(lease.job_id, a.job_id)
        self.assertEqual(self.job(store, exhausted.job_id).state, "failed")
        self.assertEqual(self.job(store, superseded.job_id).state, "superseded")
        check(faults, lambda: store.renew_processing_lease(lease.job_id, lease.fence, LEASE, at(22)))
        self.project(store, "id-a")
        first = check(faults, lambda: store.complete_processing_job(lease.job_id, lease.fence, revision(b"one"), at(23)))
        self.assertEqual(self.job(store, a.job_id).state, "queued")
        lease = store.claim_processing_job(OWNER, LEASE, at(24))
        assert lease is not None
        check(faults, lambda: store.fail_processing_job(lease.job_id, lease.fence, "transient", "synthetic-timeout", at(25), at(24)))
        lease = store.claim_processing_job(OWNER, LEASE, at(25))
        assert lease is not None
        store.complete_processing_job(lease.job_id, lease.fence, revision(b"two"), at(25))
        # An identical outcome with a supersession writes no outcome but still closes the job.
        record_id, selection_id = made["id-a"][0], lease.selection_id
        store.enqueue_processing(record_id, selection_id, PV, 3, at(26))
        lease = store.claim_processing_job(OWNER, LEASE, at(26))
        assert lease is not None
        self.retract(store, "id-a")
        check(faults, lambda: store.complete_processing_job(lease.job_id, lease.fence, revision(b"two"), at(27)))
        self.assertEqual(self.job(store, lease.job_id).state, "superseded")
        self.assertEqual(len(self.outcomes(store, record_id)), 2)
        self.assertNotEqual(first.outcome_id, self.outcomes(store, record_id)[1].outcome_id)
        d = store.enqueue_processing(*made["id-d"], PV, 1, at(28))
        lease = store.claim_processing_job(OWNER, LEASE, at(28))
        assert lease is not None and lease.job_id == d.job_id
        check(faults, lambda: store.fail_processing_job(lease.job_id, lease.fence, "transient", "synthetic-timeout", at(29), at(28)))
        self.assertEqual(self.job(store, d.job_id).failure, "retries-exhausted")
        check(faults, lambda: store.quarantine_item(position(sequence, 0), "synthetic-not-json", "", at(30)))
        with store.read() as view:
            self.assertIsNotNone(view.item_quarantine(position(sequence, 0)))

    def test_processing_refusals_leave_database_unchanged(self) -> None:
        store = self.ready()
        record_id, selection_id, lease = self.leased(store, when=at(10))
        b, b_selection = self.project(store, "id-b")
        queued = store.enqueue_processing(b, b_selection, PV, 3, at(10))
        sequence = self.archive(store, BatchItem(projection=b"\xff"), BatchItem(retracted_identity="id-gone"))
        store.quarantine_item(position(sequence, 0), "synthetic-not-json", "", at(10))
        refusals = [
            ("timestamp-invalid", store.claim_processing_job, (OWNER, LEASE, NAIVE)),
            ("processing-request-invalid", store.claim_processing_job, ("", LEASE, at(11))),
            ("processing-request-invalid", store.claim_processing_job, (OWNER, 0, at(11))),
            ("processing-request-invalid", store.claim_processing_job, (OWNER, 86_401, at(11))),
            ("processing-request-invalid", store.claim_processing_job, (OWNER, 1.5, at(11))),
            ("processing-request-invalid", store.enqueue_processing, (b, b_selection, PV2, 0, at(11))),
            ("processor-version-invalid", store.enqueue_processing, (b, b_selection, "interpret-steps", 3, at(11))),
            ("source-record-unknown", store.enqueue_processing, (999_999, b_selection, PV, 3, at(11))),
            ("selection-unknown", store.enqueue_processing, (b, selection_id, PV2, 3, at(11))),
            ("processing-job-unknown", store.renew_processing_lease, (999_999, lease.fence, LEASE, at(11))),
            ("lease-not-held", store.renew_processing_lease, (queued.job_id, queued.fence, LEASE, at(11))),
            ("processing-request-invalid", store.renew_processing_lease, (lease.job_id, lease.fence, 0, at(11))),
            ("timestamp-invalid", store.renew_processing_lease, (lease.job_id, lease.fence, LEASE, NAIVE)),
            ("processing-job-unknown", store.complete_processing_job, (999_999, lease.fence, revision(), at(11))),
            ("lease-not-held", store.complete_processing_job, (queued.job_id, queued.fence, revision(), at(11))),
            ("outcome-invalid", store.complete_processing_job, (lease.job_id, lease.fence, revision(body_version=0), at(11))),
            ("timestamp-invalid", store.complete_processing_job, (lease.job_id, lease.fence, revision(), NAIVE)),
            ("processing-job-unknown", store.fail_processing_job, (999_999, lease.fence, "transient", "synthetic-x", at(12), at(11))),
            ("lease-not-held", store.fail_processing_job, (queued.job_id, queued.fence, "transient", "synthetic-x", at(12), at(11))),
            ("processing-request-invalid", store.fail_processing_job, (lease.job_id, lease.fence, "transient", "synthetic-x", at(10), at(11))),
            ("timestamp-invalid", store.fail_processing_job, (lease.job_id, lease.fence, "deterministic", "synthetic-x", None, NAIVE)),
            ("timestamp-invalid", store.quarantine_item, (position(sequence, 0), "synthetic-not-json", "", NAIVE)),
            ("processing-request-invalid", store.quarantine_item, (position(sequence, 0), "synthetic-not-json", "x", at(11))),
            ("archived-item-unknown", store.quarantine_item, (position(sequence, 5), "synthetic-not-json", "", at(11))),
            ("index-entry-mismatch", store.quarantine_item, (position(sequence, 1), "synthetic-not-json", "", at(11))),
            ("item-quarantine-conflict", store.quarantine_item, (position(sequence, 0), "synthetic-other", "", at(11))),
        ]
        for failure, operation, args in refusals:
            with self.subTest(failure=failure, operation=operation.__name__, args=args):
                self.assertRefused(failure, operation, *args)
        self.free = RESERVE - 1
        for operation, args in (
            (store.claim_processing_job, (OWNER, LEASE, at(11))),
            (store.renew_processing_lease, (lease.job_id, lease.fence, LEASE, at(11))),
            (store.complete_processing_job, (lease.job_id, lease.fence, revision(), at(11))),
            (store.fail_processing_job, (lease.job_id, lease.fence, "deterministic", "synthetic-x", None, at(11))),
            (store.enqueue_processing, (b, b_selection, PV2, 3, at(11))),
        ):
            with self.subTest(operation=operation.__name__):
                self.assertRetryable("storage-reserve-exhausted", operation, *args)
        self.assertEqual(self.job(store, lease.job_id).state, "leased")
        self.assertEqual(self.job(store, queued.job_id).state, "queued")

    def test_processing_state_survives_reopen(self) -> None:
        store = self.ready()
        record_id, selection_id, done = self.leased(store, when=at(10))
        store.complete_processing_job(done.job_id, done.fence, revision(b"kept"), at(11))
        b, b_selection, failed = self.leased(store, "id-b", when=at(12))
        store.fail_processing_job(failed.job_id, failed.fence, "transient", "synthetic-timeout", at(500), at(12))
        c, c_selection, held = self.leased(store, "id-c", when=at(13), lease_seconds=3600)
        sequence = self.archive(store, BatchItem(projection=b"\xff"), BatchItem(projection=b"{}"))
        store.quarantine_item(position(sequence, 0), "synthetic-not-json", "", at(14))
        mapping = {"steps": PV}

        def snapshot(handle):
            with handle.read() as view:
                return (
                    [view.processing_jobs(identity) for identity in (record_id, b, c)],
                    [view.processing_outcomes(identity) for identity in (record_id, b, c)],
                    view.latest_outcome(record_id, PV), view.selections_awaiting_processing(mapping, 10),
                    view.items_awaiting_index(10), view.item_quarantine(position(sequence, 0)),
                    view.processing_counts(at(15)), view.projection_bytes(held.evidence_id),
                )

        before = snapshot(store)
        store.close()
        reopened = self.open()
        self.assertEqual(snapshot(reopened), before)
        # A held lease is still held, and nothing else may claim its job.
        self.assertIsNone(reopened.claim_processing_job(OTHER_OWNER, LEASE, at(16)))
        renewed = reopened.renew_processing_lease(held.job_id, held.fence, 3600, at(16))
        self.assertEqual(renewed.fence, held.fence)
        recorded = reopened.complete_processing_job(held.job_id, held.fence, unmodeled(), at(17))
        self.assertEqual((recorded.job_id, recorded.fence), (held.job_id, held.fence))
        self.assertEqual(self.assertWritesNothing(reopened.quarantine_item, position(sequence, 0), "synthetic-not-json", "", at(18)),
                         before[5])
        retried = reopened.claim_processing_job(OWNER, LEASE, at(500))
        assert retried is not None
        self.assertEqual((retried.job_id, retried.selection_id), (failed.job_id, b_selection))
        self.assertGreater(retried.fence, failed.fence)


class R40EarlierVersionsUnchanged(StoreCase):
    def test_processing_changes_no_earlier_table(self) -> None:
        store = self.ready()
        made = {identity: self.project(store, identity) for identity in ("id-a", "id-b", "id-c")}
        self.retract(store, "id-c")
        _, newer = self.project(store, "id-b")
        sequence = self.archive(store, BatchItem(projection=b"\xff"), BatchItem(projection=b"{}"))
        names = tables_created_by(2)
        self.assertIn("source_selections", names)
        before = {name: rows for name, rows in contents(self.path)["tables"].items() if name in names}  # type: ignore[union-attr]
        self.assertEqual(set(before), names)
        a = store.enqueue_processing(*made["id-a"], PV, 2, at(10))
        b = store.enqueue_processing(made["id-b"][0], newer, PV, 2, at(10))
        lease = store.claim_processing_job(OWNER, LEASE, at(11))
        assert lease is not None and lease.job_id == a.job_id
        store.renew_processing_lease(lease.job_id, lease.fence, LEASE, at(11))
        store.fail_processing_job(lease.job_id, lease.fence, "transient", "synthetic-timeout", at(12), at(11))
        lease = store.claim_processing_job(OWNER, LEASE, at(12))
        assert lease is not None
        store.complete_processing_job(lease.job_id, lease.fence, revision(), at(12))
        lease = store.claim_processing_job(OWNER, LEASE, at(13))
        assert lease is not None and lease.job_id == b.job_id
        again = store.claim_processing_job(OTHER_OWNER, LEASE, at(20))
        assert again is not None and again.job_id == b.job_id
        self.assertIsNone(store.claim_processing_job(OWNER, LEASE, at(30)))
        store.quarantine_item(position(sequence, 0), "synthetic-not-json", "", at(31))
        store.quarantine_item(position(sequence, 0), "synthetic-not-json", "", at(32))
        with store.read() as view:
            view.processing_counts(at(33))
            view.selections_awaiting_processing({"steps": PV}, 10)
            view.items_awaiting_index(10)
        after_calls = {name: rows for name, rows in contents(self.path)["tables"].items() if name in names}  # type: ignore[union-attr]
        self.assertEqual(after_calls, before)
        self.assertEqual(self.job(store, b.job_id).failure, "lease-expired-retries-exhausted")


class R30R39ReserveGating(StoreCase):
    def test_processing_reserve_admits_no_write_calls_and_equal_free_space(self) -> None:
        store = self.ready()
        record_id, selection_id, lease = self.leased(store, when=at(10), lease_seconds=3600)
        b, b_selection = self.project(store, "id-b")
        sequence = self.archive(store, BatchItem(projection=b"\xff"), BatchItem(projection=b"{}"))
        store.quarantine_item(position(sequence, 0), "synthetic-not-json", "", at(10))
        self.free = RESERVE - 1
        # Calls that write nothing succeed below the reserve.
        self.assertWritesNothing(store.enqueue_processing, record_id, selection_id, PV, 3, at(11))
        self.assertIsNone(self.assertWritesNothing(store.claim_processing_job, OWNER, LEASE, at(11)))
        self.assertWritesNothing(store.quarantine_item, position(sequence, 0), "synthetic-not-json", "", at(11))
        # Calls that would write are refused below it.
        self.assertRetryable("storage-reserve-exhausted", store.enqueue_processing, b, b_selection, PV, 3, at(11))
        self.assertRetryable("storage-reserve-exhausted", store.quarantine_item, position(sequence, 1), "synthetic-not-json", "", at(11))
        self.assertRetryable("storage-reserve-exhausted", store.renew_processing_lease, lease.job_id, lease.fence, LEASE, at(11))
        self.assertRetryable("storage-reserve-exhausted", store.complete_processing_job, lease.job_id, lease.fence, revision(), at(11))
        self.assertRetryable("storage-reserve-exhausted", store.fail_processing_job, lease.job_id, lease.fence, "transient",
                             "synthetic-timeout", at(12), at(11))
        # A claim that would reclaim an expired lease is a write too.
        self.assertRetryable("storage-reserve-exhausted", store.claim_processing_job, OWNER, LEASE, at(200))
        # Free space equal to the reserve is enough.
        self.free = RESERVE
        queued = store.enqueue_processing(b, b_selection, PV, 3, at(12))
        self.assertEqual(store.quarantine_item(position(sequence, 1), "synthetic-not-json", "", at(12)).reason, "synthetic-not-json")
        self.assertEqual(store.renew_processing_lease(lease.job_id, lease.fence, LEASE, at(12)).expires_at, after(at(12), LEASE))
        self.assertEqual(store.complete_processing_job(lease.job_id, lease.fence, revision(), at(12)).job_id, lease.job_id)
        self.free = RESERVE - 1
        self.assertRetryable("storage-reserve-exhausted", store.claim_processing_job, OWNER, LEASE, at(13))
        self.free = RESERVE
        claimed = store.claim_processing_job(OWNER, LEASE, at(13))
        assert claimed is not None
        self.assertEqual(claimed.job_id, queued.job_id)
        self.assertEqual(store.fail_processing_job(claimed.job_id, claimed.fence, "transient", "synthetic-timeout", at(14), at(13)).state, "queued")


    def test_bookkeeping_only_claims_are_gated_by_the_reserve(self) -> None:
        store = self.ready()
        _, _, lease = self.leased(store, max_attempts=1, when=at(10))
        # Only an exhausted expired lease is eligible: failing it is a write.
        self.free = RESERVE - 1
        self.assertRetryable("storage-reserve-exhausted", store.claim_processing_job, OWNER, LEASE, at(20))
        self.free = RESERVE
        self.assertIsNone(store.claim_processing_job(OWNER, LEASE, at(20)))
        self.assertEqual((self.job(store, lease.job_id).state, self.job(store, lease.job_id).failure),
                         ("failed", "lease-expired-retries-exhausted"))
        # Only a queued job whose record was retracted or conflicted is eligible: superseding it is a write.
        for identity, change in (("id-r", self.retract), ("id-c", self.conflict)):
            with self.subTest(identity):
                self.free = PLENTY
                record_id, selection_id = self.project(store, identity)
                job = store.enqueue_processing(record_id, selection_id, PV, 3, at(30))
                change(store, identity)
                self.free = RESERVE - 1
                self.assertRetryable("storage-reserve-exhausted", store.claim_processing_job, OWNER, LEASE, at(31))
                self.free = RESERVE
                self.assertIsNone(store.claim_processing_job(OWNER, LEASE, at(31)))
                self.assertEqual(self.job(store, job.job_id).state, "superseded")


if __name__ == "__main__":
    unittest.main()
