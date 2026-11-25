"""Acceptance tests for `operational-store@5`.

Requirement numbers refer to `packs/operational-store-5.spec.md`. `@1`-`@4`'s
requirements R1-R40 stay bound to their suites, which the `@5` runner re-runs
through `@4`'s bound runner. Every database lives in a temporary directory and
every value is synthetic. Source Records, selections, jobs, and outcomes are
built through `@3`'s and `@4`'s own operations, with hand-written outcomes.
Zones are real IANA names, but the store never resolves them (Decision 9);
only R49's timeline test hands them to the published `interpret`. Every time
is the caller's: the store never reads a clock.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import tzdata

from activity_tracker_portal import interpret_steps
from activity_tracker_portal import operational_store as store_module
from activity_tracker_portal.interpret_source_projection import (
    CanonicalRevision,
    FallbackTimezonePolicy,
    InterpretFailure,
    ProcessingInput,
    decode_envelope,
    interpret,
)
from activity_tracker_portal.operational_store import (
    BatchItem,
    FallbackTimezonePolicyRecord,
    FallbackTimezoneRecording,
    IngestBatchRecord,
    ItemPosition,
    KnownOrigin,
    MessageHeader,
    OriginKey,
    OutcomeEntry,
    ProcessingAttempt,
    ProcessingJob,
    ProcessingOutcome,
    ProjectionEntry,
    RetractionEntry,
    RevisionEntry,
    SourceKey,
    SourceSelectionPolicyRecord,
    StoreRefusal,
    StoreRetryable,
    open_store,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_4 = json.loads((ROOT / "governance/scopes/operational-store.4.json").read_text(encoding="utf-8"))
CONTRACT_5 = json.loads((ROOT / "governance/scopes/operational-store.5.json").read_text(encoding="utf-8"))
EFFECTS_5 = json.loads((ROOT / "packs/operational-store-5.effects.json").read_text(encoding="utf-8"))
SCOPE_MAP = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
FIXTURE = ROOT / "tests/acceptance/fixtures/operational-store-4-processing.sql"
VECTORS = {vector["name"]: vector for vector in
           json.loads((ROOT / "projection/v1/vectors/positive.json").read_text(encoding="utf-8"))["vectors"]}

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 11, 25, 9, 0, 0, 123456, tzinfo=UTC)
NAIVE = datetime(2026, 11, 25, 9, 0)
RESERVE = 1_000_000
PLENTY = 10**12
INSTALLATION = "0f8fad5b-d9cb-469f-a165-70867728950e"
CYCLE = "e3b0c442-98fc-4c14-9afb-f4c8996fb924"
OTHER_CYCLE = "5d41402a-bc4b-4a76-b971-9d911017c592"
HC = "health-connect"
ORIGIN_A = "org.example.synthetic.source"
ORIGIN_B = "org.example.synthetic.other"
OA = OriginKey(HC, ORIGIN_A)
OB = OriginKey(HC, ORIGIN_B)
PV = "interpret-steps@1"
PV2 = "interpret-steps@2"
WPV = "interpret-weight@1"
MAP = {"steps": PV, "weight": WPV}
NONE = "none"
INFERRED = 'fallback-timezone:[["synthetic-ftz-1"],"2026f"]'
OTHER_INFERRED = 'fallback-timezone:[["synthetic-ftz-2"],"2026f"]'
FTZ_KIND = "fallback-timezone"
OWNER = "synthetic-worker-1"
OTHER_OWNER = "synthetic-worker-2"
LEASE = 60
MEASURE = "daily-steps"
HEALTH_MARKER = b"synthetic-health-marker"
IDENTITY_MARKER = "synthetic-identity-marker"
START_NS = 1_790_000_000_000_000_000
MARCH = datetime(2026, 3, 1, tzinfo=UTC)
JUNE = datetime(2026, 6, 1, tzinfo=UTC)
SEPTEMBER = datetime(2026, 9, 1, tzinfo=UTC)
# Supersession lists in other containers than a tuple (review finding 7).
BAD_CONTAINERS = ([], [1], {1}, set(), 1, None)
NEW_FAILURES = (
    "policy-request-invalid",
    "policy-unknown",
    "policy-supersession-invalid",
    "policy-overlap",
    "policy-origin-unknown",
)
NEW_OUTPUTS = ["policy_record", "policy_reprocessing"]
NEW_TYPES = (OriginKey, FallbackTimezonePolicyRecord, SourceSelectionPolicyRecord, FallbackTimezoneRecording, KnownOrigin)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def header(installation_id: str, sequence: int) -> MessageHeader:
    body = {"synthetic": True, "kind": "ingest-batch", "installation": installation_id, "sequence": sequence}
    data = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return MessageHeader(
        installation_id=installation_id, sequence=sequence, message_sha256=digest(data), staged_generation=1,
        receipt_time=at(sequence), envelope=data, receipt=b'{"synthetic-receipt":%d}' % sequence,
    )


def batch(*items: BatchItem, record_type: str = "steps", change: bool = False, page: int = 1) -> IngestBatchRecord:
    return IngestBatchRecord(
        record_type=record_type, projection_schema_version=1,
        cycle_kind="change-cycle" if change else "initial-snapshot",  # type: ignore[arg-type]
        cycle_id=OTHER_CYCLE if change else CYCLE, source_page_index=page, fragment_index=1,
        previous_chain=digest(b"synthetic-chain-%d" % page), items=items,
    )


def position(sequence: int, item_index: int) -> ItemPosition:
    return ItemPosition(installation_id=INSTALLATION, sequence=sequence, item_index=item_index)


def revision(label: bytes = b"r", *, policy: str = NONE, pv: str = PV, attribution: str = "experienced") -> OutcomeEntry:
    return OutcomeEntry("revision", pv, policy, None, None, None, RevisionEntry(  # type: ignore[arg-type]
        b'{"synthetic-header":"' + label + b'"}', b'{"synthetic-body":"' + label + b'"}', 1,
        START_NS, START_NS + 600_000_000_000, date(2026, 9, 21), date(2026, 9, 21), attribution, attribution))


def pending(policy_kind: str = FTZ_KIND, *, policy: str = NONE, pv: str = PV) -> OutcomeEntry:
    return OutcomeEntry("policy-pending", pv, policy, "fallback-timezone-policy-missing", None, policy_kind, None)


def unmodeled(*, policy: str = NONE, pv: str = PV) -> OutcomeEntry:
    return OutcomeEntry("unmodeled", pv, policy, "synthetic-unmodeled-shape", None, None, None)


def quarantined(*, policy: str = NONE, pv: str = PV) -> OutcomeEntry:
    return OutcomeEntry("quarantine", pv, policy, "instant-out-of-range", "/record/time", None, None)


def without_rowid(connection: sqlite3.Connection) -> set[str]:
    """The main schema's WITHOUT ROWID tables, whatever tables a store version defines."""
    return {name for schema_name, name, _kind, _columns, flag, _strict in connection.execute("PRAGMA table_list")
            if schema_name == "main" and flag}


def table_rows(connection: sqlite3.Connection, table: str, columns: str = "*", rowless: set[str] | None = None):
    """Every row of one table. A rowid table's rows lead with their rowid, in rowid order, because the
    rowid is observable state: archival order follows archived_messages.rowid. A WITHOUT ROWID table
    has no such identity, so its rows are compared as a sorted list."""
    if table in (without_rowid(connection) if rowless is None else rowless):
        rows = connection.execute(f'SELECT {columns} FROM "{table}"').fetchall()
        return sorted(rows, key=lambda row: [(value is None, type(value).__name__, 0 if value is None else value) for value in row])
    return connection.execute(f'SELECT rowid, {columns} FROM "{table}" ORDER BY rowid').fetchall()


def contents(path: Path) -> dict[str, object]:
    """The whole database: schema version, every schema object, and every row of every table, BLOBs and rowids included."""
    connection = sqlite3.connect(path)
    try:
        schema = connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        rowless = without_rowid(connection)
        tables: dict[str, list[tuple[object, ...]]] = {}
        for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
            tables[name] = table_rows(connection, name, rowless=rowless)
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


def job_state(job: ProcessingJob) -> tuple[object, ...]:
    return (job.state, job.selection_id, job.attempts, job.not_before, job.lease_owner, job.lease_expires_at,
            job.failure, job.closed_at)


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "portal.sqlite3"
        self.free = PLENTY
        self.sequence = 0
        self.minute = 100

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

    def tick(self) -> datetime:
        self.minute += 1
        return at(self.minute)

    def archive(self, store, *items: BatchItem, record_type: str = "steps", change: bool = False) -> int:
        self.sequence += 1
        store.archive_ingest_batch(header(INSTALLATION, self.sequence),
                                   batch(*items, record_type=record_type, change=change, page=self.sequence))
        return self.sequence

    def record_id(self, store, identity: str, record_type: str = "steps", origin: str = ORIGIN_A,
                  kind: str = HC) -> int:
        with store.read() as view:
            found = view.source_record_id(SourceKey(kind, origin, record_type, identity))
        self.assertIsNotNone(found, identity)
        assert found is not None
        return found

    def project(self, store, identity: str, record_type: str = "steps", origin: str = ORIGIN_A,
                kind: str = HC) -> tuple[int, int]:
        """Archive, index, and select one projection of identity: (record_id, selection_id)."""
        data = b'{"synthetic-projection":"%s-%d","marker":"%s"}' % (identity.encode(), self.sequence + 1, HEALTH_MARKER)
        sequence = self.archive(store, BatchItem(projection=data), record_type=record_type)
        store.index_items((ProjectionEntry(position(sequence, 0), kind, origin, identity, at(sequence)),), at(sequence))
        record_id = self.record_id(store, identity, record_type, origin, kind)
        with store.read() as view:
            found = view.source_record(record_id)
        assert found is not None
        selection = store.record_selection(record_id, "projection", (found.evidence[-1].evidence_id,),
                                           found.evidence_head, at(sequence))
        return record_id, selection.selection_id

    def retract(self, store, identity: str, record_type: str = "steps", origin: str = ORIGIN_A) -> int:
        sequence = self.archive(store, BatchItem(retracted_identity=identity), record_type=record_type, change=True)
        store.index_items((RetractionEntry(position(sequence, 0)),), at(sequence))
        record_id = self.record_id(store, identity, record_type, origin)
        with store.read() as view:
            found = view.source_record(record_id)
        assert found is not None
        return store.record_selection(record_id, "retraction", (found.evidence[-1].evidence_id,),
                                      found.evidence_head, at(sequence)).selection_id

    def process(self, store, record: tuple[int, int], outcome: OutcomeEntry, pv: str | None = None) -> ProcessingOutcome:
        """Enqueue, claim, and complete one job for record: the recorded outcome."""
        when = self.tick()
        store.enqueue_processing(*record, pv or outcome.processor_version, 3, when)
        lease = store.claim_processing_job(OWNER, LEASE, when)
        assert lease is not None
        self.assertEqual(lease.record_id, record[0])
        return store.complete_processing_job(lease.job_id, lease.fence, outcome, when)

    def lease(self, store, record: tuple[int, int], pv: str = PV, *, seconds: int = 3600, max_attempts: int = 3):
        when = self.tick()
        store.enqueue_processing(*record, pv, max_attempts, when)
        lease = store.claim_processing_job(OWNER, seconds, when)
        assert lease is not None
        self.assertEqual(lease.record_id, record[0])
        return lease

    def seed_origins(self, store) -> None:
        """Source Records from two steps origins and one weight origin."""
        self.project(store, "seed-a")
        self.project(store, "seed-b", origin=ORIGIN_B)
        self.project(store, "seed-w", "weight")

    def ftz(self, store, zone: str = "Asia/Kolkata", start: datetime | None = None, supersedes: tuple[int, ...] = (),
            mapping: dict[str, str] | None = None, max_attempts: int = 3, when: datetime | None = None):
        return store.record_fallback_timezone_policy(zone, start, supersedes, MAP if mapping is None else mapping,
                                                     max_attempts, when or self.tick())

    def ssp(self, store, origins: tuple[OriginKey, ...] = (OA,), first: date | None = None, last: date | None = None,
            supersedes: tuple[int, ...] = (), measure: str = MEASURE, record_type: str = "steps",
            when: datetime | None = None):
        return store.record_source_selection_policy(measure, record_type, origins, first, last, supersedes,
                                                    when or self.tick())

    def drain(self, store) -> None:
        """Claim and complete every eligible job; a job that keeps coming back fails the test."""
        for _ in range(20):
            lease = store.claim_processing_job(OWNER, LEASE, self.tick())
            if lease is None:
                return
            store.complete_processing_job(lease.job_id, lease.fence, revision(policy=INFERRED, pv=lease.processor_version),
                                          self.tick())
        self.fail("completed jobs keep returning to the queue")

    def jobs(self, store, record_id: int) -> tuple[ProcessingJob, ...]:
        with store.read() as view:
            return view.processing_jobs(record_id)

    def job(self, store, job_id: int) -> ProcessingJob:
        with store.read() as view:
            found = view.processing_job(job_id)
        assert found is not None
        return found

    def all_jobs(self, store, record_ids) -> dict[int, tuple[ProcessingJob, ...]]:
        return {record_id: self.jobs(store, record_id) for record_id in record_ids}

    def new_jobs(self, before: dict[int, tuple[ProcessingJob, ...]], after: dict[int, tuple[ProcessingJob, ...]]):
        """The jobs added between two all_jobs snapshots, after checking every earlier job is unchanged."""
        added = []
        for record_id, jobs in after.items():
            self.assertEqual(jobs[: len(before[record_id])], before[record_id], record_id)
            added.extend(jobs[len(before[record_id]):])
        return added

    def view(self, store, name: str, *args):
        with store.read() as view:
            return getattr(view, name)(*args)

    def assertRefused(self, failure: str, operation, *args, **kwargs) -> StoreRefusal:
        before = contents(self.path)
        with self.assertRaises(StoreRefusal) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.failure, failure)
        self.assertEqual(contents(self.path), before, f"{failure} changed the database")
        return caught.exception

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


class R41GovernedScopeContractVersion5(unittest.TestCase):
    def test_contract_5_extends_version_4(self) -> None:
        self.assertEqual(CONTRACT_5["scope"], "operational-store")
        self.assertEqual(CONTRACT_5["version"], "5")
        self.assertEqual(CONTRACT_5["kind"], CONTRACT_4["kind"])
        for name in ("observable_effects", "dependencies"):
            self.assertEqual(CONTRACT_5[name], CONTRACT_4[name], name)
        self.assertEqual([(item["name"], item["mode"]) for item in CONTRACT_5["inputs"]],
                         [(item["name"], item["mode"]) for item in CONTRACT_4["inputs"]])
        prior = CONTRACT_4["named_failures"]
        self.assertEqual(CONTRACT_5["named_failures"][: len(prior)], prior)
        added = CONTRACT_5["named_failures"][len(prior):]
        self.assertEqual(tuple(failure["name"] for failure in added), NEW_FAILURES)
        self.assertEqual({failure["outcome"] for failure in added}, {"invariant-refusal"})
        outputs = [item["name"] for item in CONTRACT_5["outputs"]]
        self.assertEqual(outputs[: len(CONTRACT_4["outputs"])], [item["name"] for item in CONTRACT_4["outputs"]])
        self.assertEqual(outputs[len(CONTRACT_4["outputs"]):], NEW_OUTPUTS)
        owns_4, owns_5 = CONTRACT_4["authority"]["owns"], CONTRACT_5["authority"]["owns"]
        self.assertEqual(owns_5[: len(owns_4)], owns_4)
        appended = " ".join(owns_5[len(owns_4):])
        for phrase in ("Fallback Timezone Policy", "Source Selection", "supersession", "origins known"):
            self.assertIn(phrase, appended)
        does_not_own = " ".join(CONTRACT_5["authority"]["does_not_own"])
        self.assertIn("Summary Refresh Jobs", does_not_own)
        self.assertIn("policy activation", does_not_own)
        oracle = CONTRACT_5["acceptance_oracle"]
        self.assertEqual(oracle["specification"], "packs/operational-store-5.spec.md")
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_operational_store_5.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_operational_store_5.sh")
        self.assertEqual([effect["name"] for effect in EFFECTS_5["effects"]],
                         [effect["name"] for effect in CONTRACT_5["observable_effects"]])

    def test_scope_map_row_does_not_contradict_contract_5(self) -> None:
        row = next(line for line in SCOPE_MAP.splitlines() if line.startswith("| `operational-store` |"))
        self.assertIn("| shared |", row)
        self.assertIn("transactional", row)
        self.assertIn("policy", row)
        self.assertIn("| — |", row)

    def test_failures_match_contract_5(self) -> None:
        partitions = {
            "startup-refusal": set(store_module.STARTUP_FAILURES),
            "retryable": set(store_module.RETRYABLE_FAILURES),
            "invariant-refusal": set(store_module.INVARIANT_FAILURES),
        }
        for failure in CONTRACT_5["named_failures"]:
            self.assertIn(failure["name"], partitions[failure["outcome"]], failure["name"])
        self.assertLessEqual(set(NEW_FAILURES), set(store_module.FAILURES))
        self.assertLessEqual(set(NEW_FAILURES), set(store_module.INVARIANT_FAILURES))


class R42MigrationFourIsAdditive(StoreCase):
    def load_fixture(self) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.executescript(FIXTURE.read_text(encoding="utf-8"))
        finally:
            connection.close()

    def test_new_database_records_migrations_1_to_4(self) -> None:
        self.open().close()
        known = [(version, digest(sql.encode("utf-8"))) for version, sql in store_module.MIGRATIONS]
        self.assertGreaterEqual(len(known), 4)
        self.assertEqual([version for version, _ in known[:4]], [1, 2, 3, 4])
        # Migrations 1-3 are @4's, exactly as the published @4 module recorded them.
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(FIXTURE.read_text(encoding="utf-8"))
            recorded = connection.execute("SELECT version, checksum FROM store_migrations ORDER BY version").fetchall()
        finally:
            connection.close()
        self.assertEqual(recorded, known[:3])
        state = contents(self.path)
        connection = sqlite3.connect(self.path)
        try:
            recorded = connection.execute("SELECT version, checksum FROM store_migrations ORDER BY version").fetchall()
        finally:
            connection.close()
        self.assertEqual(recorded, known)
        self.assertEqual(state["user_version"], known[-1][0])
        # Migration 4 only creates tables and indexes.
        statements = [statement for statement in store_module.MIGRATIONS[3][1].split(";") if statement.strip()]
        self.assertGreater(len(statements), 0)
        for statement in statements:
            code = "\n".join(line for line in statement.strip().splitlines() if not line.strip().startswith("--")).strip()
            self.assertRegex(code.upper(), r"^CREATE\s+(UNIQUE\s+)?(TABLE|INDEX)\s", code)
        self.assertLess(tables_created_by(3), tables_created_by(4))

    def test_version_4_database_requires_migration_and_is_untouched(self) -> None:
        self.load_fixture()
        before = contents(self.path)
        self.assertEqual(before["user_version"], 3)
        with self.assertRaises(StoreRefusal) as caught:
            open_store(self.path, reserve_bytes=RESERVE, free_bytes=lambda _path: PLENTY)
        self.assertEqual(caught.exception.failure, "store-migration-required")
        self.assertEqual(contents(self.path), before)

    def test_migrated_version_4_database_takes_policies(self) -> None:
        self.load_fixture()
        connection = sqlite3.connect(self.path)
        try:
            earlier = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            columns = {table: [row[1] for row in connection.execute(f'PRAGMA table_xinfo("{table}")')] for table in earlier}

            rowless = without_rowid(connection)

            def rows(table: str) -> list[tuple[object, ...]]:
                # The earlier columns only, with each row's rowid where the table has one.
                names = ", ".join(f'"{column}"' for column in columns[table])
                return table_rows(connection, table, names, rowless)

            original = {table: rows(table) for table in earlier if table != "store_migrations"}
            self.assertGreater(len(original["processing_jobs"]), 0)
            for version, sql in store_module.MIGRATIONS[3:]:
                connection.executescript(sql)
                connection.execute(
                    "INSERT INTO store_migrations (version, checksum, applied_at) VALUES (?, ?, ?)",
                    (version, digest(sql.encode("utf-8")), "2026-11-25T09:00:00.000000Z"),
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
        finally:
            connection.close()
        store = self.open()
        ids = {identity: self.record_id(store, identity, record_type, origin) for identity, record_type, origin in (
            ("id-inferred", "steps", ORIGIN_A), ("id-experienced", "steps", ORIGIN_A), ("id-pending", "steps", ORIGIN_B),
            ("id-weight-inferred", "weight", ORIGIN_A), ("id-weight-quarantine", "weight", ORIGIN_A),
            ("id-retracted", "steps", ORIGIN_A), ("id-failed", "steps", ORIGIN_A), ("id-leased", "steps", ORIGIN_B),
            ("id-queued", "steps", ORIGIN_A))}
        self.assertEqual(self.view(store, "known_origins", None), (
            KnownOrigin(HC, "steps", ORIGIN_B, 2), KnownOrigin(HC, "steps", ORIGIN_A, 5),
            KnownOrigin(HC, "weight", ORIGIN_A, 2)))
        self.assertEqual(self.view(store, "fallback_timezone_timeline"), ())
        before = self.all_jobs(store, ids.values())
        (leased_job,) = [job for job in before[ids["id-leased"]] if job.state == "leased"]
        when = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
        recording = self.ftz(store, when=when)
        self.assertEqual((recording.jobs_enqueued, recording.jobs_rearmed), (3, 1))
        added = self.new_jobs(before, self.all_jobs(store, ids.values()))
        self.assertEqual(sorted((job.record_id, job.processor_version) for job in added), sorted([
            (ids["id-inferred"], PV), (ids["id-pending"], PV), (ids["id-weight-inferred"], WPV)]))
        for job in added:
            self.assertEqual((job.state, job.attempts, job.not_before, job.enqueued_at, job.max_attempts),
                             ("queued", 0, when, when, 3))
        # The fixture's expired but unclaimed lease is still held, and was marked.
        outcome = store.complete_processing_job(leased_job.job_id, leased_job.fence, pending(), when + timedelta(minutes=1))
        self.assertEqual(outcome.job_id, leased_job.job_id)
        self.assertEqual(self.job(store, leased_job.job_id).state, "queued")
        # Source selection works over the migrated origins, and the store stays writable.
        self.assertEqual(self.ssp(store, (OA, OB)).origins, (OB, OA))
        record = (ids["id-failed"], self.view(store, "source_record", ids["id-failed"]).selections[-1].selection_id)
        self.assertEqual(store.enqueue_processing(*record, PV, 3, when).state, "queued")


class R43RecordingAFallbackTimezonePolicy(StoreCase):
    def test_fallback_timezone_policy_is_recorded_pending(self) -> None:
        store = self.ready()
        when = self.tick()
        start = datetime(2026, 3, 1, 5, 30, tzinfo=IST)
        recording = self.ftz(store, "Asia/Kolkata", start, when=when)
        self.assertIsInstance(recording, FallbackTimezoneRecording)
        policy = recording.policy
        self.assertIsInstance(policy, FallbackTimezonePolicyRecord)
        self.assertIsInstance(policy.policy_id, int)
        self.assertGreater(policy.policy_id, 0)
        self.assertEqual((policy.zone, policy.effective_from, policy.supersedes, policy.superseded_by, policy.requested_at),
                         ("Asia/Kolkata", datetime(2026, 3, 1, tzinfo=UTC), (), None, when))
        self.assertEqual(policy.effective_from.utcoffset(), timedelta(0))
        self.assertEqual(policy.requested_at.utcoffset(), timedelta(0))
        self.assertEqual((recording.jobs_enqueued, recording.jobs_rearmed), (0, 0))
        with store.read() as view:
            self.assertEqual(view.policy(policy.policy_id), policy)
            self.assertEqual(view.fallback_timezone_policies(), (policy,))
            self.assertEqual(view.fallback_timezone_timeline(), (policy,))
            self.assertEqual(view.source_selection_policies(), ())

    def test_all_earlier_history_policy(self) -> None:
        store = self.ready()
        later = self.ftz(store, "Europe/London", JUNE).policy
        history = self.ftz(store, "Asia/Kolkata", None).policy
        self.assertIsNone(history.effective_from)
        self.assertEqual(self.view(store, "fallback_timezone_timeline"), (history, later))
        self.assertRefused("policy-overlap", self.ftz, store, "UTC", None)
        replacement = self.ftz(store, "UTC", None, (history.policy_id,)).policy
        self.assertEqual(self.view(store, "fallback_timezone_timeline"), (replacement, later))

    def test_fallback_timezone_refusals(self) -> None:
        store = self.ready()
        existing = self.ftz(store, "Asia/Kolkata", None).policy
        refusals = [
            ("timestamp-invalid", dict(when=NAIVE)),
            ("timestamp-invalid", dict(start=NAIVE)),
            *[("policy-request-invalid", dict(zone=zone, start=MARCH)) for zone in (
                "", "Europe//London", "/Europe/London", "Europe/London/", "../etc", "Europe/../London", "Europe/./London",
                "Europe\\London", " UTC", "UTC ", "Europe/Lon don", "Europe/Lon\x00don", "Europe/Londón", None, 5,
                "UTC\n", "UTC\r\n", "Europe/London\r", "\nUTC", "UTC\t", "Europe/Lon\tdon", "UTC\x7f")],
            *[("policy-request-invalid", dict(supersedes=container, start=MARCH)) for container in (
                *BAD_CONTAINERS, [existing.policy_id])],
            *[("policy-request-invalid", dict(supersedes=supersedes, start=MARCH)) for supersedes in (
                (0,), (-1,), ("1",), (1.0,), (existing.policy_id, existing.policy_id), (None,))],
            ("policy-request-invalid", dict(mapping={"": PV}, start=MARCH)),
            ("policy-request-invalid", dict(mapping={5: PV}, start=MARCH)),
            *[("processor-version-invalid", dict(mapping={"steps": version}, start=MARCH)) for version in (
                "interpret-steps", "Interpret-steps@1", "interpret_steps@1", "interpret-steps@x", "", 5)],
            *[("processing-request-invalid", dict(max_attempts=value, start=MARCH)) for value in (0, 101, 2.5, "3")],
        ]
        for failure, kwargs in refusals:
            with self.subTest(failure=failure, kwargs=kwargs):
                self.assertRefused(failure, self.ftz, store, **kwargs)
        # The store checks the form only, never whether tzdata knows the zone (Decision 9).
        for number, zone in enumerate(("UTC", "Etc/GMT+5", "America/Argentina/Buenos_Aires", "America/Port-au-Prince",
                                       "Synthetic/Unknown_Zone")):
            with self.subTest(zone=zone):
                self.assertEqual(self.ftz(store, zone, MARCH + timedelta(days=number)).policy.zone, zone)
        for number, value in enumerate((1, 100)):
            self.assertEqual(self.ftz(store, "UTC", JUNE + timedelta(days=number), max_attempts=value).policy.zone, "UTC")


class R44RecordingASourceSelectionPolicy(StoreCase):
    def test_source_selection_policy_is_recorded_pending(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        when = self.tick()
        policy = self.ssp(store, (OA, OB), date(2026, 1, 1), date(2026, 1, 31), when=when)
        self.assertIsInstance(policy, SourceSelectionPolicyRecord)
        self.assertIsInstance(policy.policy_id, int)
        self.assertEqual(
            (policy.measure, policy.record_type, policy.origins, policy.first_date, policy.last_date, policy.supersedes,
             policy.superseded_by, policy.requested_at),
            (MEASURE, "steps", (OB, OA), date(2026, 1, 1), date(2026, 1, 31), (), None, when))
        self.assertEqual(policy.requested_at.utcoffset(), timedelta(0))
        with store.read() as view:
            self.assertEqual(view.policy(policy.policy_id), policy)
            self.assertEqual(view.source_selection_policies(), (policy,))
            self.assertEqual(view.source_selection_timeline(MEASURE, "steps"), (policy,))
            self.assertEqual(view.fallback_timezone_policies(), ())
        unbounded = self.ssp(store, (OriginKey(HC, ORIGIN_A),), record_type="weight")
        self.assertEqual((unbounded.first_date, unbounded.last_date, unbounded.origins), (None, None, (OA,)))
        single_day = self.ssp(store, (OA,), date(2026, 2, 1), date(2026, 2, 1))
        self.assertEqual((single_day.first_date, single_day.last_date), (date(2026, 2, 1), date(2026, 2, 1)))

    def test_combination_needs_several_origins(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        one = self.ssp(store, (OB,), date(2026, 1, 1), date(2026, 1, 31))
        both = self.ssp(store, (OB, OA), date(2026, 2, 1), date(2026, 2, 28))
        with store.read() as view:
            self.assertEqual(view.applicable_source_selection(MEASURE, "steps", date(2026, 1, 15)).origins, (OB,))
            self.assertEqual(view.applicable_source_selection(MEASURE, "steps", date(2026, 2, 15)).origins, (OB, OA))
        self.assertEqual((one.origins, both.origins), ((OB,), (OB, OA)))

    def test_source_selection_refusals(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        refusals = [
            ("timestamp-invalid", dict(when=NAIVE)),
            *[("policy-request-invalid", dict(measure=measure)) for measure in (
                "", "Daily-Steps", "daily_steps", "daily steps", "-daily", "daily-", "a" * 65, None, 5)],
            *[("policy-request-invalid", dict(record_type=record_type)) for record_type in ("", None, 5)],
            *[("policy-request-invalid", dict(origins=origins)) for origins in (
                (), (OA, OA), ((HC, ORIGIN_A),), (ORIGIN_A,), (OriginKey(5, ORIGIN_A),),  # type: ignore[arg-type]
                (OriginKey(HC, None),))],  # type: ignore[arg-type]
            ("policy-request-invalid", dict(origins=[OA])),
            *[("policy-request-invalid", dict(supersedes=container)) for container in BAD_CONTAINERS],
            # A datetime is not a local date (Decision 6), aware or naive, in either bound.
            ("policy-request-invalid", dict(first=datetime(2026, 1, 1, tzinfo=UTC))),
            ("policy-request-invalid", dict(first=datetime(2026, 1, 1))),
            ("policy-request-invalid", dict(last=datetime(2026, 1, 31, tzinfo=IST))),
            ("policy-request-invalid", dict(last=datetime(2026, 1, 31))),
            ("policy-request-invalid", dict(first=date(2026, 1, 1), last=datetime(2026, 1, 31, tzinfo=UTC))),
            ("policy-request-invalid", dict(first="2026-01-01")),
            ("policy-request-invalid", dict(last="2026-01-31")),
            ("policy-request-invalid", dict(first=date(2026, 2, 1), last=date(2026, 1, 31))),
            *[("policy-request-invalid", dict(supersedes=supersedes)) for supersedes in ((0,), ("1",), (1, 1))],
        ]
        for failure, kwargs in refusals:
            with self.subTest(failure=failure, kwargs=kwargs):
                self.assertRefused(failure, self.ssp, store, **kwargs)
        self.assertEqual(self.ssp(store, measure="a" * 64).measure, "a" * 64)

    def test_unknown_origin_is_refused(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        self.project(store, "only-weight", "weight", origin="org.example.synthetic.weight-only")
        self.project(store, "other-kind", origin=ORIGIN_A, kind="synthetic-system")
        for origins in (
            (OriginKey(HC, "org.example.synthetic.never-seen"),),
            (OriginKey(HC, "org.example.synthetic.weight-only"),),  # known only for weight
            (OriginKey("synthetic-system", ORIGIN_B),),  # known only under another kind
            (OA, OriginKey(HC, "org.example.synthetic.never-seen")),
        ):
            with self.subTest(origins=origins):
                self.assertRefused("policy-origin-unknown", self.ssp, store, origins)
        self.assertEqual(self.ssp(store, (OriginKey("synthetic-system", ORIGIN_A),)).origins,
                         (OriginKey("synthetic-system", ORIGIN_A),))

    def test_source_selection_queues_no_job(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        inferred = self.project(store, "id-inferred")
        self.process(store, inferred, revision(policy=INFERRED))
        held = self.lease(store, self.project(store, "id-held"))
        names = tables_created_by(3)
        before = {name: rows for name, rows in contents(self.path)["tables"].items() if name in names}  # type: ignore[union-attr]
        self.ssp(store, (OA, OB))
        after = {name: rows for name, rows in contents(self.path)["tables"].items() if name in names}  # type: ignore[union-attr]
        self.assertEqual(after, before)
        store.complete_processing_job(held.job_id, held.fence, revision(), self.tick())
        self.assertEqual(self.job(store, held.job_id).state, "completed")


class R45SupersessionAndOverlap(StoreCase):
    def test_supersession_replaces_named_policies(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        p1 = self.ftz(store, "Asia/Kolkata", None).policy
        p2 = self.ftz(store, "Europe/London", JUNE).policy
        p3 = self.ftz(store, "Europe/Paris", datetime(2026, 6, 1, 2, 0, tzinfo=timezone(timedelta(hours=2))),
                      (p2.policy_id,)).policy
        self.assertEqual(p3.supersedes, (p2.policy_id,))
        with store.read() as view:
            self.assertEqual(view.policy(p2.policy_id).superseded_by, p3.policy_id)
            self.assertEqual(view.policy(p1.policy_id), p1)
            self.assertEqual(view.fallback_timezone_timeline(), (p1, p3))
        s1 = self.ssp(store, (OA,), date(2026, 1, 1), date(2026, 1, 31))
        s2 = self.ssp(store, (OB,), date(2026, 2, 1), date(2026, 2, 28))
        s3 = self.ssp(store, (OA, OB), date(2026, 1, 1), date(2026, 2, 28), (s2.policy_id, s1.policy_id))
        self.assertEqual(s3.supersedes, (s1.policy_id, s2.policy_id))
        with store.read() as view:
            self.assertEqual(view.policy(s1.policy_id).superseded_by, s3.policy_id)
            self.assertEqual(view.policy(s2.policy_id).superseded_by, s3.policy_id)
            self.assertEqual(view.source_selection_timeline(MEASURE, "steps"), (s3,))
        # Superseding a live policy that would not even overlap is allowed.
        p4 = self.ftz(store, "Asia/Tokyo", SEPTEMBER, (p1.policy_id,)).policy
        self.assertEqual(self.view(store, "fallback_timezone_timeline"), (p3, p4))

    def test_supersession_refusals(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        f1 = self.ftz(store, "Asia/Kolkata", None).policy
        f2 = self.ftz(store, "Europe/London", JUNE, (f1.policy_id,)).policy
        s1 = self.ssp(store, (OA,))
        s_weight = self.ssp(store, (OA,), record_type="weight")
        s_other = self.ssp(store, (OA,), measure="steps-total")
        refusals = [
            ("policy-unknown", lambda: self.ftz(store, "UTC", MARCH, (999_999,))),
            ("policy-unknown", lambda: self.ftz(store, "UTC", MARCH, (f2.policy_id, 999_999))),
            ("policy-unknown", lambda: self.ssp(store, (OB,), supersedes=(999_999,))),
            ("policy-supersession-invalid", lambda: self.ftz(store, "UTC", MARCH, (s1.policy_id,))),
            ("policy-supersession-invalid", lambda: self.ssp(store, (OB,), supersedes=(f2.policy_id,))),
            ("policy-supersession-invalid", lambda: self.ftz(store, "UTC", MARCH, (f1.policy_id,))),
            ("policy-supersession-invalid", lambda: self.ftz(store, "UTC", MARCH, (f2.policy_id, f1.policy_id))),
            ("policy-supersession-invalid", lambda: self.ssp(store, (OB,), supersedes=(s1.policy_id, s_weight.policy_id))),
            ("policy-supersession-invalid", lambda: self.ssp(store, (OB,), supersedes=(s1.policy_id, s_other.policy_id))),
            # In a well-formed tuple, policy-unknown wins over policy-supersession-invalid, in either order.
            ("policy-unknown", lambda: self.ftz(store, "UTC", MARCH, (999_999, f1.policy_id))),
            ("policy-unknown", lambda: self.ftz(store, "UTC", MARCH, (f1.policy_id, 999_999))),
            ("policy-unknown", lambda: self.ftz(store, "UTC", MARCH, (999_999, s1.policy_id))),
            ("policy-unknown", lambda: self.ftz(store, "UTC", MARCH, (s1.policy_id, 999_999))),
            ("policy-unknown", lambda: self.ssp(store, (OB,), supersedes=(999_999, f2.policy_id))),
            ("policy-unknown", lambda: self.ssp(store, (OB,), supersedes=(f2.policy_id, 999_999))),
            ("policy-unknown", lambda: self.ssp(store, (OB,), supersedes=(999_999, s_weight.policy_id))),
            ("policy-unknown", lambda: self.ssp(store, (OB,), supersedes=(s_weight.policy_id, 999_999))),
        ]
        for failure, operation in refusals:
            with self.subTest(failure=failure):
                self.assertRefused(failure, operation)

    def test_overlapping_timezone_start_is_refused(self) -> None:
        store = self.ready()
        first = self.ftz(store, "Asia/Kolkata", None).policy
        june = self.ftz(store, "Europe/London", JUNE).policy
        self.assertRefused("policy-overlap", self.ftz, store, "Europe/Paris", JUNE)
        # The same instant written in another offset is the same start.
        self.assertRefused("policy-overlap", self.ftz, store, "Europe/Paris", datetime(2026, 6, 1, 5, 30, tzinfo=IST))
        self.assertRefused("policy-overlap", self.ftz, store, "UTC", None)
        # A microsecond apart is a different start.
        nearby = self.ftz(store, "Europe/Paris", JUNE + timedelta(microseconds=1)).policy
        self.assertEqual(self.view(store, "fallback_timezone_timeline"), (first, june, nearby))
        replaced = self.ftz(store, "Europe/Paris", JUNE, (june.policy_id,)).policy
        self.assertEqual(self.view(store, "fallback_timezone_timeline"), (first, replaced, nearby))

    def test_overlapping_selection_dates_are_refused(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        january = self.ssp(store, (OA,), date(2026, 1, 1), date(2026, 1, 31))
        cases = [
            (date(2026, 1, 31), date(2026, 2, 28)),  # shares the inclusive last date
            (date(2025, 12, 1), date(2026, 1, 1)),  # shares the inclusive first date
            (date(2026, 1, 10), date(2026, 1, 10)),
            (None, None),
            (None, date(2026, 1, 1)),
            (date(2026, 1, 31), None),
            (date(2025, 1, 1), date(2027, 1, 1)),
        ]
        for first, last in cases:
            with self.subTest(first=first, last=last):
                self.assertRefused("policy-overlap", self.ssp, store, (OB,), first, last)
        tail = self.ssp(store, (OB,), date(2026, 3, 1), None)
        self.assertRefused("policy-overlap", self.ssp, store, (OA,), date(2027, 1, 1), date(2027, 1, 1))
        self.assertEqual(self.view(store, "source_selection_timeline", MEASURE, "steps"), (january, tail))

    def test_disjoint_selection_intervals_coexist(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        made = [
            self.ssp(store, (OA,), date(2026, 2, 1), date(2026, 2, 28)),
            self.ssp(store, (OB,), None, date(2026, 1, 31)),
            self.ssp(store, (OA, OB), date(2026, 3, 1), None),
            self.ssp(store, (OA,), measure="steps-total"),
            self.ssp(store, (OA,), record_type="weight"),
        ]
        with store.read() as view:
            self.assertEqual(view.source_selection_timeline(MEASURE, "steps"), (made[1], made[0], made[2]))
            self.assertEqual(view.source_selection_timeline("steps-total", "steps"), (made[3],))
            self.assertEqual(view.source_selection_timeline(MEASURE, "weight"), (made[4],))
            self.assertEqual(view.source_selection_policies(), tuple(made))

    def test_superseded_policy_stays_in_history(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        january = self.ssp(store, (OA,), date(2026, 1, 1), date(2026, 1, 31))
        february = self.ssp(store, (OB,), date(2026, 2, 1), date(2026, 2, 28), (january.policy_id,))
        with store.read() as view:
            kept = view.policy(january.policy_id)
            self.assertEqual(view.source_selection_policies(MEASURE, "steps"), (kept, february))
        self.assertEqual(kept, SourceSelectionPolicyRecord(
            january.policy_id, MEASURE, "steps", (OA,), date(2026, 1, 1), date(2026, 1, 31), (), february.policy_id,
            january.requested_at))
        self.assertRefused("policy-supersession-invalid", self.ssp, store, (OA,), date(2026, 1, 1), date(2026, 1, 31),
                           (january.policy_id,))
        # A request equal to a superseded policy is a new request, not a repeat, and January is free again.
        again = self.ssp(store, (OA,), date(2026, 1, 1), date(2026, 1, 31))
        self.assertGreater(again.policy_id, february.policy_id)
        with store.read() as view:
            self.assertEqual(view.policy(january.policy_id), kept)
            self.assertEqual(view.source_selection_timeline(MEASURE, "steps"), (again, february))
        old = self.ftz(store, "Asia/Kolkata", None).policy
        new = self.ftz(store, "UTC", None, (old.policy_id,)).policy
        self.assertEqual(self.view(store, "fallback_timezone_policies"),
                         (FallbackTimezonePolicyRecord(old.policy_id, "Asia/Kolkata", None, (), new.policy_id,
                                                       old.requested_at), new))
        self.assertRefused("policy-overlap", self.ftz, store, "Asia/Kolkata", None)


class R46IdenticalRepeatsAndTheReserve(StoreCase):
    def test_identical_policy_repeat_writes_nothing(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        inferred = self.project(store, "id-inferred")
        self.process(store, inferred, revision(policy=INFERRED))
        held = self.lease(store, self.project(store, "id-held"))
        first = self.ftz(store, "Asia/Kolkata", JUNE)
        self.assertEqual((first.jobs_enqueued, first.jobs_rearmed), (1, 1))
        # The marked completion re-queues; drain the queue so a new write would queue the record again.
        store.complete_processing_job(held.job_id, held.fence, revision(), self.tick())
        self.drain(store)
        relaxed = self.lease(store, self.project(store, "id-held-2"))
        for zone_start, mapping, attempts in (
            (JUNE, MAP, 3),
            (datetime(2026, 6, 1, 5, 30, tzinfo=IST), MAP, 3),  # the same instant in another offset
            (JUNE, {}, 7),  # the mapping and retry bound are not part of a policy
        ):
            with self.subTest(start=zone_start, mapping=mapping):
                repeat = self.assertWritesNothing(self.ftz, store, "Asia/Kolkata", zone_start, (), mapping, attempts)
                self.assertEqual(repeat, FallbackTimezoneRecording(first.policy, 0, 0))
        # A repeat marks no lease: a job claimed after the original write completes normally.
        store.complete_processing_job(relaxed.job_id, relaxed.fence, revision(), self.tick())
        self.assertEqual(self.job(store, relaxed.job_id).state, "completed")
        self.assertRefused("policy-overlap", self.ftz, store, "Europe/London", JUNE)
        selection = self.ssp(store, (OB, OA), date(2026, 1, 1), None)
        # The same origins in another order are the same policy.
        self.assertEqual(self.assertWritesNothing(self.ssp, store, (OA, OB), date(2026, 1, 1), None), selection)
        replacement = self.ssp(store, (OA,), date(2026, 1, 1), None, (selection.policy_id,))
        # The repeat of a superseding request names policies it has already superseded.
        self.assertEqual(self.assertWritesNothing(self.ssp, store, (OA,), date(2026, 1, 1), None, (selection.policy_id,)),
                         replacement)
        moved = self.ftz(store, "Europe/London", JUNE, (first.policy.policy_id,)).policy
        self.assertEqual(self.assertWritesNothing(self.ftz, store, "Europe/London", JUNE, (first.policy.policy_id,)),
                         FallbackTimezoneRecording(moved, 0, 0))
        # A different supersession set is not a repeat.
        self.assertRefused("policy-overlap", self.ssp, store, (OA,), date(2026, 1, 1), None)
        self.assertRefused("policy-overlap", self.ftz, store, "Europe/London", JUNE)

    def test_policy_writes_are_gated_by_the_reserve(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        inferred = self.project(store, "id-inferred")
        self.process(store, inferred, revision(policy=INFERRED))
        held = self.lease(store, self.project(store, "id-held"))
        ftz = self.ftz(store, "Asia/Kolkata", None, mapping={}).policy
        ssp = self.ssp(store, (OA,))
        self.free = RESERVE - 1
        # Identical repeats write nothing and succeed below the reserve.
        self.assertEqual(self.assertWritesNothing(self.ftz, store, "Asia/Kolkata", None).policy, ftz)
        self.assertEqual(self.assertWritesNothing(self.ssp, store, (OA,)), ssp)
        # Every new policy is a write, even one that queues and marks nothing.
        self.assertRetryable("storage-reserve-exhausted", self.ftz, store, "UTC", JUNE)
        self.assertRetryable("storage-reserve-exhausted", self.ftz, store, "UTC", JUNE, mapping={})
        self.assertRetryable("storage-reserve-exhausted", self.ssp, store, (OB,), measure="steps-total")
        self.assertRetryable("storage-reserve-exhausted", self.ssp, store, (OB,), supersedes=(ssp.policy_id,))
        # Equal free space is enough.
        self.free = RESERVE
        recording = self.ftz(store, "UTC", JUNE)
        self.assertEqual((recording.jobs_enqueued, recording.jobs_rearmed), (1, 1))
        self.assertEqual(self.ssp(store, (OB,), measure="steps-total").measure, "steps-total")


class R47AffectedReprocessing(StoreCase):
    def policy_jobs(self, store, records: dict[str, tuple[int, int]], **kwargs) -> tuple[set[tuple[str, str]], object]:
        ids = {record_id: name for name, (record_id, _) in records.items()}
        before = self.all_jobs(store, ids)
        recording = self.ftz(store, **kwargs)
        added = self.new_jobs(before, self.all_jobs(store, ids))
        self.assertEqual(recording.jobs_enqueued, len(added))
        return {(ids[job.record_id], job.processor_version) for job in added}, recording

    def test_policy_queues_records_that_used_a_fallback(self) -> None:
        store = self.ready()
        made = {name: self.project(store, name) for name in (
            "inferred-revision", "inferred-quarantine", "inferred-unmodeled")}
        made["inferred-weight"] = self.project(store, "inferred-weight", "weight")
        self.process(store, made["inferred-revision"], revision(policy=INFERRED, attribution="inferred"))
        self.process(store, made["inferred-quarantine"], quarantined(policy=OTHER_INFERRED))
        self.process(store, made["inferred-unmodeled"], unmodeled(policy=INFERRED))
        self.process(store, made["inferred-weight"], revision(policy=INFERRED, pv=WPV))
        when = self.tick()
        added, recording = self.policy_jobs(store, made, when=when, max_attempts=5)
        self.assertEqual(added, {("inferred-revision", PV), ("inferred-quarantine", PV), ("inferred-unmodeled", PV),
                                 ("inferred-weight", WPV)})
        self.assertEqual(recording.jobs_rearmed, 0)
        for name, (record_id, selection_id) in made.items():
            (_, job) = self.jobs(store, record_id)
            self.assertEqual(
                (job.state, job.selection_id, job.max_attempts, job.attempts, job.not_before, job.enqueued_at,
                 job.lease_owner, job.lease_expires_at, job.failure, job.closed_at, job.history),
                ("queued", selection_id, 5, 0, when, when, None, None, None, None, ()), name)

    def test_policy_queues_policy_pending_records(self) -> None:
        store = self.ready()
        made = {name: self.project(store, name) for name in ("pending-none", "pending-inferred", "pending-other-kind",
                                                             "pending-other-kind-inferred")}
        self.process(store, made["pending-none"], pending())
        self.process(store, made["pending-inferred"], pending(policy=INFERRED))
        self.process(store, made["pending-other-kind"], pending("synthetic-timezone"))
        self.process(store, made["pending-other-kind-inferred"], pending("synthetic-timezone", policy=INFERRED))
        added, _ = self.policy_jobs(store, made)
        self.assertEqual(added, {("pending-none", PV), ("pending-inferred", PV), ("pending-other-kind-inferred", PV)})

    def test_experienced_records_are_not_queued(self) -> None:
        store = self.ready()
        made = {name: self.project(store, name) for name in ("revision", "unmodeled", "quarantine")}
        made["weight"] = self.project(store, "weight", "weight")
        self.process(store, made["revision"], revision(attribution="experienced"))
        self.process(store, made["unmodeled"], unmodeled())
        self.process(store, made["quarantine"], quarantined())
        self.process(store, made["weight"], revision(pv=WPV))
        added, recording = self.policy_jobs(store, made)
        self.assertEqual((added, recording.jobs_enqueued, recording.jobs_rearmed), (set(), 0, 0))

    def test_open_jobs_are_not_duplicated(self) -> None:
        store = self.ready()
        made = {name: self.project(store, name) for name in ("queued", "leased", "other-version-open")}
        for name in made:
            self.process(store, made[name], revision(policy=INFERRED))
        held = self.lease(store, made["leased"])
        queued = store.enqueue_processing(*made["queued"], PV, 2, self.tick())
        other = store.enqueue_processing(*made["other-version-open"], PV2, 2, self.tick())
        added, recording = self.policy_jobs(store, made)
        # An open job under another processor version does not stand in for the mapped one.
        self.assertEqual(added, {("other-version-open", PV)})
        self.assertEqual(recording.jobs_rearmed, 1)
        self.assertEqual(self.job(store, queued.job_id), queued)
        self.assertEqual(job_state(self.job(store, other.job_id)), job_state(other))
        self.assertEqual(self.job(store, held.job_id).state, "leased")

    def test_failed_and_unprocessed_records_are_not_queued(self) -> None:
        store = self.ready()
        names = ("unprocessed", "failed", "retracted", "newer-selection", "latest-is-none", "latest-is-inferred",
                 "other-version-only", "failed-after-outcome")
        made = {name: self.project(store, name) for name in names}
        # A failed job with no outcome stays failed.
        failed = self.lease(store, made["failed"])
        store.fail_processing_job(failed.job_id, failed.fence, "deterministic", "synthetic-invariant", None, self.tick())
        # A retracted record, and one whose newer projection has no outcome yet, are not affected.
        self.process(store, made["retracted"], revision(policy=INFERRED))
        self.retract(store, "retracted")
        self.process(store, made["newer-selection"], revision(policy=INFERRED))
        self.project(store, "newer-selection")
        # Only the latest outcome for the latest selection counts, as latest_outcome reads it.
        self.process(store, made["latest-is-none"], revision(b"first", policy=INFERRED))
        self.process(store, made["latest-is-none"], revision(b"second", policy=NONE))
        self.process(store, made["latest-is-inferred"], revision(b"first", policy=NONE))
        self.process(store, made["latest-is-inferred"], revision(b"second", policy=INFERRED))
        # An outcome under an unmapped version does not count for the mapped one.
        self.process(store, made["other-version-only"], revision(policy=INFERRED, pv=PV2))
        self.process(store, made["other-version-only"], revision(policy=NONE))
        # A record with an affected outcome and a later failed retry is affected: it has no open job.
        self.process(store, made["failed-after-outcome"], revision(policy=INFERRED))
        retry = self.lease(store, made["failed-after-outcome"])
        store.fail_processing_job(retry.job_id, retry.fence, "deterministic", "synthetic-invariant", None, self.tick())
        with store.read() as view:
            self.assertEqual(view.latest_outcome(made["latest-is-none"][0], PV).policy_identity, NONE)
        added, _ = self.policy_jobs(store, made)
        self.assertEqual(added, {("latest-is-inferred", PV), ("failed-after-outcome", PV)})
        self.assertEqual(self.jobs(store, made["failed"][0])[-1].state, "failed")

    def test_only_mapped_types_are_queued(self) -> None:
        for mapping, expected in (
            ({"steps": PV}, {("steps", PV)}),
            ({"weight": WPV}, {("weight", WPV)}),
            ({"steps": PV2, "weight": PV}, set()),
            ({}, set()),
            ({"heart-rate": "interpret-heart-rate@1", **MAP}, {("steps", PV), ("weight", WPV)}),
        ):
            with self.subTest(mapping=mapping):
                self.setUp()
                store = self.ready()
                made = {"steps": self.project(store, "steps"), "weight": self.project(store, "weight", "weight")}
                self.process(store, made["steps"], revision(policy=INFERRED))
                self.process(store, made["weight"], revision(policy=INFERRED, pv=WPV))
                added, _ = self.policy_jobs(store, made, mapping=mapping)
                self.assertEqual(added, expected)
                store.close()

    def test_queued_policy_job_reprocesses_to_a_new_outcome(self) -> None:
        store = self.ready()
        record = self.project(store, "inferred")
        first = self.process(store, record, revision(b"old", policy=INFERRED, attribution="inferred"))
        recording = self.ftz(store, "Europe/London", JUNE)
        self.assertEqual(recording.jobs_enqueued, 1)
        lease = store.claim_processing_job(OWNER, LEASE, self.tick())
        assert lease is not None
        self.assertEqual((lease.record_id, lease.selection_id, lease.processor_version), (record[0], record[1], PV))
        second = store.complete_processing_job(lease.job_id, lease.fence, revision(b"new", policy=OTHER_INFERRED), self.tick())
        self.assertNotEqual(second.outcome_id, first.outcome_id)
        with store.read() as view:
            self.assertEqual(view.processing_outcomes(record[0]), (first, second))
            self.assertEqual(view.latest_outcome(record[0], PV), second)
        # Claimed after the write, this job was not marked, so it completes.
        self.assertEqual(self.job(store, lease.job_id).state, "completed")
        self.assertEqual(self.ftz(store, "Europe/Paris", SEPTEMBER).jobs_enqueued, 1)


class R48LeasedJobsRunAgain(StoreCase):
    def test_lease_held_at_policy_write_is_requeued_on_completion(self) -> None:
        store = self.ready()
        record = self.project(store, "held")
        lease = self.lease(store, record, max_attempts=2)
        claimed_at = lease.expires_at - timedelta(seconds=3600)
        recording = self.ftz(store)
        self.assertEqual((recording.jobs_enqueued, recording.jobs_rearmed), (0, 1))
        self.assertEqual(self.job(store, lease.job_id).state, "leased")
        # Renewal keeps the fence and the mark.
        renewed = store.renew_processing_lease(lease.job_id, lease.fence, 3600, self.tick())
        self.assertEqual(renewed.fence, lease.fence)
        self.assertGreater(renewed.expires_at, lease.expires_at)
        done = self.tick()
        outcome = store.complete_processing_job(lease.job_id, lease.fence, revision(policy=INFERRED), done)
        self.assertEqual((outcome.job_id, outcome.fence, outcome.selection_id), (lease.job_id, lease.fence, record[1]))
        job = self.job(store, lease.job_id)
        self.assertEqual(
            (job.state, job.selection_id, job.max_attempts, job.attempts, job.not_before, job.fence, job.lease_owner,
             job.lease_expires_at, job.failure, job.closed_at),
            ("queued", record[1], 2, 0, done, lease.fence, None, None, None, None))
        # The attempt keeps the claim's times; renewal moved only the job's expiry (@4 R32).
        first_attempt = ProcessingAttempt(lease.fence, OWNER, claimed_at, lease.expires_at, done, "completed", None)
        self.assertEqual(job.history, (first_attempt,))
        with store.read() as view:
            counts = view.processing_counts(done)
        self.assertEqual((counts.jobs_ready, counts.jobs_leased, counts.jobs_completed), (1, 0, 0))
        # The next claim runs it again with a higher fence; its completion is not marked.
        again = store.claim_processing_job(OTHER_OWNER, LEASE, self.tick())
        assert again is not None
        self.assertEqual((again.job_id, again.selection_id), (lease.job_id, record[1]))
        self.assertGreater(again.fence, lease.fence)
        self.assertEqual(self.job(store, lease.job_id).attempts, 1)
        repeat = store.complete_processing_job(again.job_id, again.fence, revision(policy=INFERRED), self.tick())
        self.assertEqual(repeat, outcome)
        job = self.job(store, lease.job_id)
        self.assertEqual(job.state, "completed")
        self.assertEqual([(item.fence, item.owner, item.result) for item in job.history],
                         [(lease.fence, OWNER, "completed"), (again.fence, OTHER_OWNER, "completed")])
        self.assertRefused("lease-not-held", store.complete_processing_job, lease.job_id, lease.fence, revision(), self.tick())
        store.close()
        reopened = self.open()
        self.assertEqual(self.job(reopened, lease.job_id).history[0], first_attempt)

    def test_marked_completion_reusing_an_outcome_is_requeued(self) -> None:
        store = self.ready()
        record = self.project(store, "reused")
        first = self.process(store, record, revision(policy=INFERRED))
        lease = self.lease(store, record)
        self.assertEqual(self.ftz(store, mapping={}).jobs_rearmed, 1)
        done = self.tick()
        # The identical outcome is reused, but the marked job must still run again.
        self.assertEqual(store.complete_processing_job(lease.job_id, lease.fence, revision(policy=INFERRED), done), first)
        self.assertEqual(self.view(store, "processing_outcomes", record[0]), (first,))
        job = self.job(store, lease.job_id)
        self.assertEqual((job.state, job.selection_id, job.attempts, job.not_before, job.lease_owner, job.closed_at),
                         ("queued", record[1], 0, done, None, None))
        self.assertEqual(job.history, (ProcessingAttempt(lease.fence, OWNER, lease.expires_at - timedelta(seconds=3600),
                                                         lease.expires_at, done, "completed", None),))

    def test_refused_marked_completion_keeps_its_mark(self) -> None:
        store = self.ready()
        record = self.project(store, "refused")
        self.process(store, record, revision(b"kept", policy=INFERRED))
        lease = self.lease(store, record)
        self.ftz(store, mapping={})
        complete = store.complete_processing_job
        self.free = RESERVE - 1
        self.assertRetryable("storage-reserve-exhausted", complete, lease.job_id, lease.fence,
                             revision(b"new", policy=OTHER_INFERRED), self.tick())
        self.free = PLENTY
        self.assertRefused("outcome-invalid", complete, lease.job_id, lease.fence,
                           OutcomeEntry("revision", PV, INFERRED, None, None, None, None), self.tick())
        self.assertRefused("outcome-conflict", complete, lease.job_id, lease.fence,
                           revision(b"different", policy=INFERRED), self.tick())
        # Each refusal wrote nothing, so the mark is intact and a valid completion re-queues.
        done = self.tick()
        complete(lease.job_id, lease.fence, revision(b"new", policy=OTHER_INFERRED), done)
        job = self.job(store, lease.job_id)
        self.assertEqual((job.state, job.attempts, job.not_before), ("queued", 0, done))

    def test_each_policy_write_counts_every_held_lease(self) -> None:
        store = self.ready()
        lease = self.lease(store, self.project(store, "held"))
        self.assertEqual(self.ftz(store, "Asia/Kolkata", None, mapping={}).jobs_rearmed, 1)
        # A distinct policy counts the same fence again, although it is already marked.
        self.assertEqual(self.ftz(store, "Europe/London", JUNE, mapping={}).jobs_rearmed, 1)
        # An identical repeat counts nothing.
        self.assertEqual(self.ftz(store, "Europe/London", JUNE, mapping={}).jobs_rearmed, 0)
        other = self.lease(store, self.project(store, "held-later"))
        self.assertEqual(self.ftz(store, "Europe/Paris", SEPTEMBER, mapping={}).jobs_rearmed, 2)
        # However many times it was counted, the lease runs again exactly once.
        store.complete_processing_job(lease.job_id, lease.fence, revision(), self.tick())
        self.assertEqual(self.job(store, lease.job_id).state, "queued")
        store.complete_processing_job(other.job_id, other.fence, revision(), self.tick())
        self.drain(store)
        self.assertEqual({self.job(store, job_id).state for job_id in (lease.job_id, other.job_id)}, {"completed"})

    def test_every_held_lease_is_marked(self) -> None:
        store = self.ready()
        unmapped = self.lease(store, self.project(store, "unmapped"), PV2)
        weight = self.lease(store, self.project(store, "weight", "weight"), WPV)
        mapped = self.lease(store, self.project(store, "mapped"), seconds=60)
        self.minute += 30  # the first lease has expired, but no claim has taken it
        recording = self.ftz(store, mapping={"steps": PV})
        self.assertEqual(recording.jobs_rearmed, 3)
        for lease in (mapped, unmapped, weight):
            with self.subTest(job=lease.job_id):
                store.complete_processing_job(lease.job_id, lease.fence, revision(pv=lease.processor_version), self.tick())
                self.assertEqual(self.job(store, lease.job_id).state, "queued")

    def test_reclaimed_lease_ignores_the_mark(self) -> None:
        store = self.ready()
        lease = self.lease(store, self.project(store, "expiring"), seconds=60)
        queued = store.enqueue_processing(*self.project(store, "queued"), PV, 3, self.tick())
        self.ftz(store)
        # The marked lease expires and another claim takes it with a new fence.
        self.minute += 10
        again = store.claim_processing_job(OTHER_OWNER, LEASE, self.tick())
        assert again is not None and again.job_id == lease.job_id
        self.assertGreater(again.fence, lease.fence)
        store.complete_processing_job(again.job_id, again.fence, revision(), self.tick())
        job = self.job(store, lease.job_id)
        self.assertEqual(job.state, "completed")
        self.assertEqual([item.result for item in job.history], ["lease-expired", "completed"])
        # A job queued at the write and claimed after it is not marked.
        claimed = store.claim_processing_job(OWNER, LEASE, self.tick())
        assert claimed is not None and claimed.job_id == queued.job_id
        store.complete_processing_job(claimed.job_id, claimed.fence, revision(), self.tick())
        self.assertEqual(self.job(store, queued.job_id).state, "completed")
        # Nor is a lease taken after a policy write, or held across a Source Selection write.
        self.seed_origins(store)
        later = self.lease(store, self.project(store, "later"))
        self.ssp(store, (OA,))
        store.complete_processing_job(later.job_id, later.fence, revision(), self.tick())
        self.assertEqual(self.job(store, later.job_id).state, "completed")

    def test_mark_does_not_change_failure_or_newer_selection(self) -> None:
        store = self.ready()
        transient = self.lease(store, self.project(store, "transient"))
        deterministic = self.lease(store, self.project(store, "deterministic"))
        newer = self.lease(store, self.project(store, "newer"))
        retracted = self.lease(store, self.project(store, "retracted"))
        self.assertEqual(self.ftz(store).jobs_rearmed, 4)
        retry_at = at(self.minute + 50)
        failed = store.fail_processing_job(transient.job_id, transient.fence, "transient", "synthetic-timeout", retry_at, self.tick())
        self.assertEqual((failed.state, failed.attempts, failed.not_before), ("queued", 1, retry_at))
        failed = store.fail_processing_job(deterministic.job_id, deterministic.fence, "deterministic", "synthetic-invariant",
                                           None, self.tick())
        self.assertEqual((failed.state, failed.failure), ("failed", "synthetic-invariant"))
        _, newest = self.project(store, "newer")
        done = self.tick()
        store.complete_processing_job(newer.job_id, newer.fence, revision(), done)
        job = self.job(store, newer.job_id)
        self.assertEqual((job.state, job.selection_id, job.attempts, job.not_before), ("queued", newest, 0, done))
        self.retract(store, "retracted")
        store.complete_processing_job(retracted.job_id, retracted.fence, revision(), self.tick())
        self.assertEqual(self.job(store, retracted.job_id).state, "superseded")
        # After its transient retry the job runs under a new fence and completes.
        for _ in range(3):
            again = store.claim_processing_job(OWNER, LEASE, retry_at)
            if again is None:
                break
            store.complete_processing_job(again.job_id, again.fence, revision(), retry_at)
        self.assertIsNone(store.claim_processing_job(OWNER, LEASE, retry_at), "an unmarked completion was re-queued")
        job = self.job(store, transient.job_id)
        self.assertEqual(job.state, "completed")
        self.assertGreater(job.history[-1].fence, transient.fence)

    def test_completion_without_a_policy_write_is_unchanged(self) -> None:
        store = self.ready()
        plain = self.lease(store, self.project(store, "plain"))
        newer = self.lease(store, self.project(store, "newer"))
        retracted = self.lease(store, self.project(store, "retracted"))
        done = self.tick()
        store.complete_processing_job(plain.job_id, plain.fence, revision(policy=INFERRED), done)
        job = self.job(store, plain.job_id)
        self.assertEqual((job.state, job.closed_at, job.attempts), ("completed", done, 1))
        _, newest = self.project(store, "newer")
        store.complete_processing_job(newer.job_id, newer.fence, revision(), done)
        self.assertEqual(job_state(self.job(store, newer.job_id))[:3], ("queued", newest, 0))
        self.retract(store, "retracted")
        store.complete_processing_job(retracted.job_id, retracted.fence, revision(), done)
        self.assertEqual(self.job(store, retracted.job_id).state, "superseded")


class R49PolicyReads(StoreCase):
    def test_policy_history_in_arrival_order(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        f1 = self.ftz(store, "Asia/Kolkata", None).policy
        s1 = self.ssp(store, (OA,))
        f2 = self.ftz(store, "Europe/London", JUNE, (f1.policy_id,)).policy
        s2 = self.ssp(store, (OA,), record_type="weight")
        s3 = self.ssp(store, (OB,), measure="steps-total")
        ids = [f1.policy_id, s1.policy_id, f2.policy_id, s2.policy_id, s3.policy_id]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), 5)
        with store.read() as view:
            f1 = view.policy(f1.policy_id)
            self.assertEqual(f1.superseded_by, f2.policy_id)
            self.assertEqual(view.fallback_timezone_policies(), (f1, f2))
            self.assertEqual(view.source_selection_policies(), (s1, s2, s3))
            self.assertEqual(view.source_selection_policies(MEASURE), (s1, s2))
            self.assertEqual(view.source_selection_policies(None, "steps"), (s1, s3))
            self.assertEqual(view.source_selection_policies(MEASURE, "weight"), (s2,))
            self.assertEqual(view.source_selection_policies("unknown-measure"), ())
            for policy in (f2, s1, s2, s3):
                self.assertEqual(view.policy(policy.policy_id), policy)
            for missing in (0, -1, max(ids) + 1, 999_999):
                self.assertIsNone(view.policy(missing))

    def test_timeline_matches_the_interpreter_shape(self) -> None:
        store = self.ready()
        london = self.ftz(store, "Europe/London", JUNE).policy
        kolkata = self.ftz(store, "Asia/Kolkata", None).policy
        new_york = self.ftz(store, "America/New_York", datetime(2026, 2, 28, 19, 0, tzinfo=timezone(timedelta(hours=-5)))).policy
        timeline = self.view(store, "fallback_timezone_timeline")
        self.assertEqual(timeline, (kolkata, new_york, london))
        policies = [FallbackTimezonePolicy(str(item.policy_id), item.effective_from, item.zone) for item in timeline]
        vector = json.loads(VECTORS["steps-maximal"]["bytes"])
        for start, expected_policy, local in (
            ("2025-12-31T20:00:00Z", kolkata, date(2026, 1, 1)),
            ("2026-04-15T02:00:00Z", new_york, date(2026, 4, 14)),
            ("2026-07-01T23:30:00Z", london, date(2026, 7, 2)),
        ):
            with self.subTest(start=start):
                value = json.loads(json.dumps(vector))
                for name in ("start_zone_offset", "end_zone_offset"):
                    value["record"].pop(name, None)
                value["record"]["start_time"] = start
                value["record"]["end_time"] = start.replace(":00:00Z", ":20:00Z").replace(":30:00Z", ":50:00Z")
                data = json.dumps(value, separators=(",", ":")).encode()
                envelope = decode_envelope(data, "steps", value["projection_schema_version"])
                processing_input = ProcessingInput(
                    envelope.source_system_kind, envelope.origin, "steps", envelope.source_identity, 1, 1, 1,
                    digest(data), envelope.last_modified_time, value["projection_schema_version"], data)
                result = interpret(processing_input, interpret_steps.PROCESSOR, policies)
                self.assertIsInstance(result, CanonicalRevision)
                endpoint = result.header.time.start
                self.assertEqual((endpoint.policy_id, endpoint.local_date, endpoint.attribution),
                                 (str(expected_policy.policy_id), local, "inferred"))
                self.assertEqual(result.policy_identity, "fallback-timezone:" + json.dumps(
                    [[str(expected_policy.policy_id)], tzdata.IANA_VERSION], separators=(",", ":")))
        # Arrival order is not a valid timeline: the interpreter refuses it.
        with self.assertRaises(InterpretFailure):
            interpret(processing_input, interpret_steps.PROCESSOR,
                      sorted(policies, key=lambda item: int(item.policy_id)))

    def test_timeline_drops_superseded_policies(self) -> None:
        store = self.ready()
        self.assertEqual(self.view(store, "fallback_timezone_timeline"), ())
        a = self.ftz(store, "Asia/Kolkata", None).policy
        b = self.ftz(store, "Europe/London", JUNE).policy
        c = self.ftz(store, "Europe/Paris", MARCH).policy
        d = self.ftz(store, "UTC", None, (a.policy_id, b.policy_id)).policy
        self.assertEqual(self.view(store, "fallback_timezone_timeline"), (d, c))
        e = self.ftz(store, "Asia/Tokyo", SEPTEMBER, (c.policy_id,)).policy
        self.assertEqual(self.view(store, "fallback_timezone_timeline"), (d, e))
        self.assertEqual(len(self.view(store, "fallback_timezone_policies")), 5)

    def test_applicable_source_selection_by_date(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        early = self.ssp(store, (OA,), None, date(2026, 1, 31))
        march = self.ssp(store, (OB,), date(2026, 3, 1), date(2026, 3, 31))
        later = self.ssp(store, (OA, OB), date(2026, 6, 1), None)
        weekly = self.ssp(store, (OB,), measure="weekly-steps")
        cases = {
            date(2000, 1, 1): early, date(2026, 1, 31): early, date(2026, 2, 1): None, date(2026, 2, 28): None,
            date(2026, 3, 1): march, date(2026, 3, 31): march, date(2026, 4, 1): None, date(2026, 6, 1): later,
            date(9999, 12, 31): later,
        }
        with store.read() as view:
            for on, expected in cases.items():
                with self.subTest(on=on):
                    self.assertEqual(view.applicable_source_selection(MEASURE, "steps", on), expected)
            self.assertIsNone(view.applicable_source_selection(MEASURE, "weight", date(2026, 3, 1)))
            self.assertEqual(view.applicable_source_selection("weekly-steps", "steps", date(2026, 2, 1)), weekly)
            self.assertEqual(view.source_selection_timeline(MEASURE, "steps"), (early, march, later))
        replacement = self.ssp(store, (OA,), date(2026, 3, 15), date(2026, 4, 30), (march.policy_id,))
        with store.read() as view:
            self.assertIsNone(view.applicable_source_selection(MEASURE, "steps", date(2026, 3, 1)))
            self.assertEqual(view.applicable_source_selection(MEASURE, "steps", date(2026, 4, 30)), replacement)
            self.assertEqual(view.source_selection_timeline(MEASURE, "steps"), (early, replacement, later))

    def test_policy_reads_write_nothing(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        f = self.ftz(store, "Asia/Kolkata", None).policy
        s = self.ssp(store, (OA,))
        with store.read() as view:
            for name, args in (
                ("policy", (f.policy_id,)), ("policy", (s.policy_id,)), ("policy", (999,)),
                ("fallback_timezone_policies", ()), ("fallback_timezone_timeline", ()),
                ("source_selection_policies", ()), ("source_selection_policies", (MEASURE, "steps")),
                ("source_selection_timeline", (MEASURE, "steps")),
                ("applicable_source_selection", (MEASURE, "steps", date(2026, 1, 1))),
                ("known_origins", ()), ("known_origins", ("steps",)),
            ):
                with self.subTest(name=name, args=args):
                    self.assertWritesNothing(getattr(view, name), *args)
        # A read view is isolated from later commits.
        with store.read() as view:
            before = view.fallback_timezone_timeline()
            self.ftz(store, "Europe/London", JUNE)
            self.assertEqual(view.fallback_timezone_timeline(), before)


class R50KnownOrigins(StoreCase):
    def test_known_origins_by_record_type(self) -> None:
        store = self.ready()
        self.assertEqual(self.view(store, "known_origins", None), ())
        self.project(store, "a", origin=ORIGIN_A)
        self.project(store, "b", origin=ORIGIN_B)
        self.project(store, "z", "heart-rate", origin="org.example.synthetic.zeta")
        self.project(store, "w", "weight", origin=ORIGIN_A)
        self.project(store, "k", origin=ORIGIN_A, kind="a-synthetic-system")
        # A retraction with no Source Record adds no origin.
        sequence = self.archive(store, BatchItem(retracted_identity="never-seen"), change=True)
        store.index_items((RetractionEntry(position(sequence, 0)),), at(sequence))
        everything = (
            KnownOrigin("health-connect", "heart-rate", "org.example.synthetic.zeta", 1),
            KnownOrigin("a-synthetic-system", "steps", ORIGIN_A, 1),
            KnownOrigin(HC, "steps", ORIGIN_B, 1),
            KnownOrigin(HC, "steps", ORIGIN_A, 1),
            KnownOrigin(HC, "weight", ORIGIN_A, 1),
        )
        with store.read() as view:
            self.assertEqual(view.known_origins(), everything)
            self.assertEqual(view.known_origins(None), everything)
            self.assertEqual(view.known_origins("steps"), everything[1:4])
            self.assertEqual(view.known_origins("weight"), everything[4:])
            self.assertEqual(view.known_origins("sleep-session"), ())

    def test_known_origins_count_records(self) -> None:
        store = self.ready()
        for identity in ("a1", "a2", "a3"):
            self.project(store, identity)
        self.project(store, "a1")  # newer evidence for the same Source Record
        self.project(store, "b1", origin=ORIGIN_B)
        self.retract(store, "a2")  # a retracted record is still a Source Record
        # Indexed but never selected, a record still counts.
        sequence = self.archive(store, BatchItem(projection=b'{"synthetic-projection":"unselected"}'))
        store.index_items((ProjectionEntry(position(sequence, 0), HC, ORIGIN_B, "b2", at(sequence)),), at(sequence))
        self.assertEqual(self.view(store, "known_origins", "steps"), (
            KnownOrigin(HC, "steps", ORIGIN_B, 2), KnownOrigin(HC, "steps", ORIGIN_A, 3)))


class R51HealthDataFreeSurfaces(StoreCase):
    def test_policy_surfaces_are_health_data_free(self) -> None:
        store = self.ready()
        marked = f"{IDENTITY_MARKER}-1"
        record = self.project(store, marked)
        self.project(store, f"{IDENTITY_MARKER}-2", origin=ORIGIN_B)
        self.process(store, record, revision(HEALTH_MARKER, policy=INFERRED))
        held = self.lease(store, self.project(store, f"{IDENTITY_MARKER}-3"))
        shown = []
        recording = self.ftz(store, "Asia/Kolkata", None)
        shown += [recording, recording.policy]
        selection = self.ssp(store, (OA, OB))
        shown.append(selection)
        with store.read() as view:
            shown += [view.fallback_timezone_timeline(), view.fallback_timezone_policies(), view.source_selection_policies(),
                      view.known_origins(), view.policy(selection.policy_id),
                      view.applicable_source_selection(MEASURE, "steps", date(2026, 1, 1))]
        refusals = [
            ("policy-request-invalid", lambda: self.ftz(store, f"Europe/{IDENTITY_MARKER} x", MARCH)),
            ("policy-request-invalid", lambda: self.ssp(store, (OA,), measure=f"{IDENTITY_MARKER} measure")),
            ("policy-origin-unknown", lambda: self.ssp(store, (OriginKey(HC, IDENTITY_MARKER),), measure="other")),
            ("policy-unknown", lambda: self.ssp(store, (OA,), measure="other", supersedes=(424242,))),
            ("policy-supersession-invalid", lambda: self.ssp(store, (OA,), measure="other",
                                                             supersedes=(recording.policy.policy_id,))),
            ("policy-overlap", lambda: self.ftz(store, f"Europe/{IDENTITY_MARKER}", None)),
            ("policy-overlap", lambda: self.ssp(store, (OB,))),
        ]
        for failure, operation in refusals:
            with self.subTest(failure=failure):
                caught = self.assertRefused(failure, operation)
                self.assertEqual(caught.args, (failure,))
                self.assertEqual(str(caught), failure)
                shown.append(caught)
        store.complete_processing_job(held.job_id, held.fence, revision(HEALTH_MARKER), self.tick())
        for value in shown:
            text = repr(value) + str(value)
            self.assertNotIn(IDENTITY_MARKER, text)
            self.assertNotIn(HEALTH_MARKER.decode(), text)
        for kind in NEW_TYPES:
            self.assertTrue(getattr(kind, "__dataclass_params__").frozen, kind)


class R52AtomicDurableAndSerialized(StoreCase):
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
        self.fault_tables = set(tables)
        return connection

    def assert_every_partial_failure_commits_nothing(self, faults: sqlite3.Connection, operation):
        failures = 0
        for writes_allowed in range(1, 400):
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
        return result, failures

    def test_policy_writes_are_atomic_under_injected_failure(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        old = self.ftz(store, "Asia/Kolkata", None, mapping={}).policy
        affected = [self.project(store, f"affected-{number}") for number in range(3)]
        for record in affected:
            self.process(store, record, revision(policy=INFERRED))
        held = [self.lease(store, self.project(store, f"held-{number}")) for number in range(2)]
        s1 = self.ssp(store, (OA,), date(2026, 1, 1), date(2026, 1, 31))
        s2 = self.ssp(store, (OB,), date(2026, 2, 1), date(2026, 2, 28))
        faults = self.install_fault_triggers()
        check = self.assert_every_partial_failure_commits_nothing
        # Every partial write rolls back; the number of physical row writes is not prescribed.
        (recording, _) = check(faults, lambda: self.ftz(store, "UTC", None, (old.policy_id,), when=at(500)))
        self.assertEqual((recording.jobs_enqueued, recording.jobs_rearmed), (3, 2))
        (selection, _) = check(faults, lambda: self.ssp(store, (OA, OB), date(2026, 1, 1), date(2026, 2, 28),
                                                        (s1.policy_id, s2.policy_id), when=at(501)))
        with store.read() as view:
            self.assertEqual(view.source_selection_timeline(MEASURE, "steps"), (selection,))
            self.assertEqual(view.fallback_timezone_timeline(), (recording.policy,))
        # Completing a marked lease re-queues its job atomically.
        check(faults, lambda: store.complete_processing_job(held[0].job_id, held[0].fence, revision(), at(502)))
        self.assertEqual(self.job(store, held[0].job_id).state, "queued")
        self.assertLess(tables_created_by(3), self.fault_tables)

    def test_policy_refusals_leave_database_unchanged(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        inferred = self.project(store, "inferred")
        self.process(store, inferred, revision(policy=INFERRED))
        f = self.ftz(store, "Asia/Kolkata", None, mapping={}).policy
        s = self.ssp(store, (OA,))
        held = self.lease(store, self.project(store, "held"))
        refusals = [
            ("timestamp-invalid", lambda: self.ftz(store, "UTC", MARCH, when=NAIVE)),
            ("policy-request-invalid", lambda: self.ftz(store, "Europe//London", MARCH)),
            ("processor-version-invalid", lambda: self.ftz(store, "UTC", MARCH, mapping={"steps": "x"})),
            ("processing-request-invalid", lambda: self.ftz(store, "UTC", MARCH, max_attempts=0)),
            ("policy-unknown", lambda: self.ftz(store, "UTC", MARCH, (999,))),
            ("policy-supersession-invalid", lambda: self.ftz(store, "UTC", MARCH, (s.policy_id,))),
            ("policy-overlap", lambda: self.ftz(store, "UTC", None)),
            ("timestamp-invalid", lambda: self.ssp(store, (OB,), measure="other", when=NAIVE)),
            ("policy-request-invalid", lambda: self.ssp(store, ())),
            ("policy-origin-unknown", lambda: self.ssp(store, (OriginKey(HC, "org.example.unknown"),), measure="other")),
            ("policy-unknown", lambda: self.ssp(store, (OB,), supersedes=(999,))),
            ("policy-supersession-invalid", lambda: self.ssp(store, (OB,), supersedes=(f.policy_id,))),
            ("policy-overlap", lambda: self.ssp(store, (OB,))),
        ]
        for failure, operation in refusals:
            with self.subTest(failure=failure):
                self.assertRefused(failure, operation)
        # None of them queued a job or marked the held lease.
        self.assertEqual(len(self.jobs(store, inferred[0])), 1)
        store.complete_processing_job(held.job_id, held.fence, revision(), self.tick())
        self.assertEqual(self.job(store, held.job_id).state, "completed")

    def test_policy_state_survives_reopen(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        inferred = self.project(store, "inferred")
        self.process(store, inferred, revision(policy=INFERRED))
        held = self.lease(store, self.project(store, "held"), seconds=86_400)
        old = self.ftz(store, "Asia/Kolkata", None).policy
        self.ftz(store, "Europe/London", JUNE, (old.policy_id,))
        s1 = self.ssp(store, (OA,), date(2026, 1, 1), None)
        self.ssp(store, (OA, OB), date(2026, 1, 1), None, (s1.policy_id,))

        def snapshot(handle):
            with handle.read() as view:
                return (view.fallback_timezone_policies(), view.fallback_timezone_timeline(),
                        view.source_selection_policies(), view.source_selection_timeline(MEASURE, "steps"),
                        view.applicable_source_selection(MEASURE, "steps", date(2026, 5, 1)), view.known_origins(),
                        view.processing_jobs(inferred[0]), view.processing_job(held.job_id))

        before = snapshot(store)
        store.close()
        reopened = self.open()
        self.assertEqual(snapshot(reopened), before)
        # The mark survives: the held lease's completion still re-queues its job.
        reopened.complete_processing_job(held.job_id, held.fence, revision(), self.tick())
        self.assertEqual(self.job(reopened, held.job_id).state, "queued")

    def test_racing_overlapping_policies_leave_one_live(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        # Stress, not a forced interleaving: two handles, each opened in its own
        # thread, race to record overlapping policies. Exactly one may win.
        for round_number in range(6):
            barrier = threading.Barrier(2, timeout=30)
            results: list[object] = [None, None]

            def run(index: int) -> None:
                try:
                    handle = open_store(self.path, reserve_bytes=RESERVE, free_bytes=lambda _path: PLENTY)
                    try:
                        barrier.wait()
                        if round_number % 2:
                            results[index] = handle.record_fallback_timezone_policy(
                                ("Asia/Kolkata", "Europe/London")[index], at(round_number), (), {}, 3, at(600))
                        else:
                            results[index] = handle.record_source_selection_policy(
                                f"race-{round_number}", "steps", ((OA,), (OB,))[index], None, None, (), at(600))
                    finally:
                        handle.close()
                except BaseException as error:  # noqa: BLE001 - reported below
                    results[index] = error
                    barrier.abort()

            threads = [threading.Thread(target=run, args=(index,), daemon=True) for index in (0, 1)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
            self.assertFalse(any(thread.is_alive() for thread in threads), "a racing writer did not finish")
            for result in results:
                if isinstance(result, BaseException) and not isinstance(
                        result, (StoreRefusal, StoreRetryable, threading.BrokenBarrierError)):
                    raise result
            failures = [result for result in results if isinstance(result, BaseException)]
            self.assertEqual(len(failures), 1, results)
            (failure,) = failures
            self.assertIsInstance(failure, (StoreRefusal, StoreRetryable), failure)
            self.assertIn(failure.failure, ("policy-overlap", "store-busy"))  # type: ignore[union-attr]
            with store.read() as view:
                if round_number % 2:
                    live = [item for item in view.fallback_timezone_timeline() if item.effective_from == at(round_number)]
                else:
                    live = list(view.source_selection_timeline(f"race-{round_number}", "steps"))
            self.assertEqual(len(live), 1)


class R53EarlierVersionsUnchanged(StoreCase):
    def test_policy_writes_change_no_earlier_table(self) -> None:
        store = self.ready()
        self.seed_origins(store)
        made = [self.project(store, f"inferred-{number}") for number in range(2)]
        for record in made:
            self.process(store, record, revision(policy=INFERRED))
        self.lease(store, self.project(store, "held"))
        earliest = tables_created_by(2)
        processing = tables_created_by(3) - earliest
        self.assertIn("source_selections", earliest)
        self.assertGreater(len(processing), 0)
        before = contents(self.path)["tables"]
        old = self.ftz(store, "Asia/Kolkata", None, mapping={}).policy
        recording = self.ftz(store, "UTC", None, (old.policy_id,))
        self.ftz(store, "UTC", None, (old.policy_id,))
        s1 = self.ssp(store, (OA,))
        self.ssp(store, (OA, OB), None, None, (s1.policy_id,))
        with store.read() as view:
            view.fallback_timezone_timeline()
            view.known_origins()
            view.applicable_source_selection(MEASURE, "steps", date(2026, 1, 1))
        after = contents(self.path)["tables"]
        for name in earliest:
            self.assertEqual(after[name], before[name], name)  # type: ignore[index]
        processing_jobs = None
        for name in processing:
            if after[name] == before[name]:  # type: ignore[index]
                continue
            # Only one @4 table changes, and only by gaining the queued jobs.
            self.assertIsNone(processing_jobs, name)
            processing_jobs = name
            self.assertLessEqual(set(before[name]), set(after[name]), name)  # type: ignore[index]
            self.assertEqual(len(after[name]), len(before[name]) + recording.jobs_enqueued)  # type: ignore[index]
        self.assertEqual(recording.jobs_enqueued, 2)
        self.assertIsNotNone(processing_jobs)


if __name__ == "__main__":
    unittest.main()
