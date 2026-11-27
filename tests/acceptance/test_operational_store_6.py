"""Acceptance tests for `operational-store@6`.

Requirement numbers refer to `packs/operational-store-6.spec.md`. `@1`-`@5`'s
requirements R1-R53 stay bound to their suites, which the `@6` runner re-runs
through `@5`'s bound runner. Every database lives in a temporary directory and
every value is synthetic. Source Records, selections, jobs, outcomes, and
policies are built through `@3`-`@5`'s own operations, with hand-written
outcomes. Summary entries are hand-written marker bytes; the suite never imports
`summary_contract`, because the store must not depend on it. Every time is the
caller's: the store never reads a clock.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import tempfile
import threading
import types
import unittest
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path

from activity_tracker_portal import operational_store as store_module
from activity_tracker_portal.operational_store import (
    BatchItem,
    CompletionRecord,
    EvidenceAcknowledgement,
    EvidenceBookmark,
    EvidenceChange,
    EvidenceChanges,
    IngestBatchRecord,
    ItemPosition,
    MessageHeader,
    OriginKey,
    OutcomeEntry,
    ProcessedPosition,
    ProcessingOutcome,
    ProcessorGeneration,
    ProjectionEntry,
    RefreshAttempt,
    RefreshCompletion,
    RefreshCounts,
    RefreshLease,
    RefreshRequest,
    RetractionEntry,
    RevisionEntry,
    SourceKey,
    StoredSummary,
    StoreRefusal,
    StoreRetryable,
    SummaryEntry,
    SummaryEvidence,
    SummaryEvidenceItem,
    SummaryKey,
    SummaryRefreshJob,
    SummaryTarget,
    open_store,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_5 = json.loads((ROOT / "governance/scopes/operational-store.5.json").read_text(encoding="utf-8"))
CONTRACT_6 = json.loads((ROOT / "governance/scopes/operational-store.6.json").read_text(encoding="utf-8"))
EFFECTS_6 = json.loads((ROOT / "packs/operational-store-6.effects.json").read_text(encoding="utf-8"))
SCOPE_MAP = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
FIXTURE = ROOT / "tests/acceptance/fixtures/operational-store-6/operational-store-5-policies.sql"

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 11, 27, 9, 0, 0, 123456, tzinfo=UTC)
NAIVE = datetime(2026, 11, 27, 9, 0)
MICRO = timedelta(microseconds=1)
RESERVE = 1_000_000
PLENTY = 10**12
# INSTALLATION_B sorts before INSTALLATION, so position order is not creation order.
INSTALLATION = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
INSTALLATION_B = "16fd2706-8baf-433b-82eb-8c7fada847da"
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
HPV = "interpret-heart-rate@1"
GEN_MAP = {"steps": PV, "weight": WPV}
NONE = "none"
INFERRED = 'fallback-timezone:[["synthetic-ftz-1"],"2026f"]'
OWNER = "synthetic-refresh-worker-1"
OTHER_OWNER = "synthetic-refresh-worker-2"
PROCESSOR = "synthetic-processing-worker"
LEASE = 86_400
MEASURE = "daily-steps"
DAY = date(2026, 9, 21)
CONTRACT = "summary-contract@1"
DERIVATION = "derive-daily-steps@1"
HEALTH_MARKER = b"synthetic-health-marker"
SUMMARY_MARKER = b"synthetic-summary-marker"
IDENTITY_MARKER = "synthetic-identity-marker"
START_NS = 1_790_000_000_000_000_000
DEFAULT = object()
NEW_FAILURES = (
    "processor-generation-invalid",
    "processor-generation-unknown",
    "summary-request-invalid",
    "summary-target-invalid",
    "summary-refresh-job-unknown",
    "summary-entry-invalid",
    "summary-watermark-invalid",
    "summary-contributor-invalid",
)
NEW_OUTPUTS = ["processor_generation", "summary_refresh_request", "summary_refresh_job", "summary_refresh_lease",
               "summary_revision"]
NEW_TYPES = (ProcessorGeneration, SummaryKey, SummaryTarget, ProcessedPosition, SummaryEntry, StoredSummary,
             RefreshAttempt, SummaryRefreshJob, RefreshLease, RefreshRequest, RefreshCompletion, EvidenceChange,
             EvidenceBookmark, EvidenceChanges, EvidenceAcknowledgement, SummaryEvidenceItem, SummaryEvidence,
             RefreshCounts)


class RepeatedHour(tzinfo):
    """A synthetic zone, independent of host tzdata, in which every wall time is repeated: fold 0 is UTC-4, fold 1
    is UTC-5. Python never treats such a time as equal to a time in another zone, so a store must normalize."""

    def utcoffset(self, moment: datetime | None) -> timedelta:
        return timedelta(hours=-5 if moment is not None and moment.fold else -4)

    def dst(self, moment: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, moment: datetime | None) -> str:
        return "SYNTHETIC-REPEATED"


REPEATED_HOUR = RepeatedHour()


def repeated(moment: datetime, fold: int) -> datetime:
    """The wall time in RepeatedHour that denotes moment at fold 0 (fold 1 is an hour later)."""
    return (moment.astimezone(UTC) - timedelta(hours=4)).replace(tzinfo=REPEATED_HOUR, fold=fold)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def header(installation_id: str, sequence: int, when: datetime) -> MessageHeader:
    body = {"synthetic": True, "installation": installation_id, "sequence": sequence}
    data = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return MessageHeader(
        installation_id=installation_id, sequence=sequence, message_sha256=digest(data), staged_generation=1,
        receipt_time=when, envelope=data, receipt=b'{"synthetic-receipt":%d}' % sequence,
    )


def batch(*items: BatchItem, record_type: str = "steps", change: bool = False, page: int = 1) -> IngestBatchRecord:
    return IngestBatchRecord(
        record_type=record_type, projection_schema_version=1,
        cycle_kind="change-cycle" if change else "initial-snapshot",  # type: ignore[arg-type]
        cycle_id=OTHER_CYCLE if change else CYCLE, source_page_index=page, fragment_index=1,
        previous_chain=digest(b"synthetic-chain-%d" % page), items=items,
    )


def revision(label: bytes = b"r", *, policy: str = NONE, pv: str = PV, start: date = DAY, end: date | None = None,
             instant: bool = False) -> OutcomeEntry:
    """A Canonical Revision over local dates start..end (end defaults to start); an instant has no end."""
    last = None if instant else (end or start)
    return OutcomeEntry("revision", pv, policy, None, None, None, RevisionEntry(  # type: ignore[arg-type]
        b'{"synthetic-header":"' + label + b'"}', b'{"synthetic-body":"' + label + b'","marker":"' + HEALTH_MARKER + b'"}',
        1, START_NS, None if instant else START_NS + 600_000_000_000, start, last, "experienced",
        None if instant else "experienced"))


def pending(*, policy: str = NONE, pv: str = PV) -> OutcomeEntry:
    return OutcomeEntry("policy-pending", pv, policy, "fallback-timezone-policy-missing", None, "fallback-timezone", None)


def unmodeled(*, policy: str = NONE, pv: str = PV) -> OutcomeEntry:
    return OutcomeEntry("unmodeled", pv, policy, "synthetic-unmodeled-shape", None, None, None)


def quarantined(*, policy: str = NONE, pv: str = PV) -> OutcomeEntry:
    return OutcomeEntry("quarantine", pv, policy, "instant-out-of-range", "/record/time", None, None)


def jcs(manifest: dict[str, str]) -> bytes:
    """RFC 8785 for a flat object of strings: keys sorted by UTF-16 code units, minimal JSON escaping, UTF-8."""
    def string(value: str) -> str:
        out = ['"']
        for character in value:
            code = ord(character)
            if character in '"\\':
                out.append("\\" + character)
            elif code < 0x20:
                out.append({8: "\\b", 9: "\\t", 10: "\\n", 12: "\\f", 13: "\\r"}.get(code, "\\u%04x" % code))
            else:
                out.append(character)
        return "".join(out) + '"'
    keys = sorted(manifest, key=lambda key: key.encode("utf-16-be"))
    return ("{" + ",".join(string(key) + ":" + string(manifest[key]) for key in keys) + "}").encode("utf-8")


def without_rowid(connection: sqlite3.Connection) -> set[str]:
    """The main schema's WITHOUT ROWID tables, whatever tables a store version defines."""
    return {name for schema_name, name, _kind, _columns, flag, _strict in connection.execute("PRAGMA table_list")
            if schema_name == "main" and flag}


def table_rows(connection: sqlite3.Connection, table: str, columns: str = "*", rowless: set[str] | None = None):
    """Every row of one table: a rowid table's rows lead with their rowid, in rowid order; a WITHOUT ROWID table's
    rows are compared as a sorted list."""
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


def changed_tables(before: dict[str, object], after: dict[str, object]) -> set[str]:
    old, new = before["tables"], after["tables"]  # type: ignore[index]
    return {name for name in set(old) | set(new) if old.get(name) != new.get(name)}  # type: ignore[union-attr]


def blob_text(state: dict[str, object]) -> bytes:
    """Every stored value of the whole database as bytes, for marker searches."""
    parts = []
    for rows in state["tables"].values():  # type: ignore[union-attr]
        for row in rows:
            for value in row:
                parts.append(value if isinstance(value, bytes) else str(value).encode())
    return b"\x00".join(parts)


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "portal.sqlite3"
        self.free = PLENTY
        self.sequences: dict[str, int] = {}
        self.receipts: dict[tuple[str, int], datetime] = {}
        self.minute = 100
        self.installation = INSTALLATION

    # --- @1-@5 building blocks -------------------------------------------------------------------

    def open(self):
        store = open_store(self.path, reserve_bytes=RESERVE, free_bytes=lambda _path: self.free)
        self.addCleanup(store.close)
        return store

    def tick(self) -> datetime:
        self.minute += 1
        return at(self.minute)

    def ready(self):
        store = self.open()
        offer = digest(b"synthetic-enrollment-code")
        store.record_enrollment_offer(offer, at(10_000), at(0))
        store.enroll_installation(offer, INSTALLATION, digest(b"synthetic-fingerprint"), at(0))
        store.activate_installation(INSTALLATION, at(0))
        store.honor_generation(1, b'{"synthetic-advertisement":1}', T0)
        return store

    def switch_installation(self, store) -> None:
        """Retire the delivering installation and make INSTALLATION_B the delivering one."""
        store.begin_draining(self.installation, self.tick())
        store.retire_installation(self.installation, "drained", self.tick())
        offer = digest(b"synthetic-enrollment-code-b")
        store.record_enrollment_offer(offer, at(10_000), self.tick())
        store.enroll_installation(offer, INSTALLATION_B, digest(b"synthetic-fingerprint-b"), self.tick())
        store.activate_installation(INSTALLATION_B, self.tick())
        self.installation = INSTALLATION_B

    def next_sequence(self) -> int:
        self.sequences[self.installation] = self.sequences.get(self.installation, 0) + 1
        return self.sequences[self.installation]

    def archive(self, store, *items: BatchItem, record_type: str = "steps", change: bool = False) -> int:
        sequence = self.next_sequence()
        when = self.tick()
        store.archive_ingest_batch(header(self.installation, sequence, when),
                                   batch(*items, record_type=record_type, change=change, page=sequence))
        self.receipts[(self.installation, sequence)] = when
        return sequence

    def archive_completion(self, store, record_type: str = "steps") -> int:
        sequence = self.next_sequence()
        when = self.tick()
        store.archive_completion(header(self.installation, sequence, when), CompletionRecord(
            "change", None, record_type, f"synthetic-cycle-{self.installation}-{sequence}", 1, 1, 1,
            digest(b"synthetic-final-%d" % sequence)))
        self.receipts[(self.installation, sequence)] = when
        return sequence

    def position(self, sequence: int, item_index: int = 0) -> ItemPosition:
        return ItemPosition(self.installation, sequence, item_index)

    def record_id(self, store, identity: str, record_type: str = "steps", origin: str = ORIGIN_A, kind: str = HC) -> int:
        with store.read() as view:
            found = view.source_record_id(SourceKey(kind, origin, record_type, identity))
        self.assertIsNotNone(found, identity)
        assert found is not None
        return found

    def observe(self, store, identity: str, record_type: str = "steps", origin: str = ORIGIN_A, kind: str = HC) -> int:
        """Archive and index one projection of identity without selecting it: the sequence."""
        data = b'{"synthetic-projection":"%s-%d","marker":"%s"}' % (identity.encode(), self.minute, HEALTH_MARKER)
        sequence = self.archive(store, BatchItem(projection=data), record_type=record_type)
        store.index_items((ProjectionEntry(self.position(sequence), kind, origin, identity, self.tick()),), self.tick())
        return sequence

    def select(self, store, record_id: int, outcome: str = "projection", count: int = 1) -> int:
        with store.read() as view:
            found = view.source_record(record_id)
        assert found is not None
        kind = "retraction" if outcome == "retraction" else "projection"
        evidence = tuple(item.evidence_id for item in found.evidence if item.kind == kind)[-count:]
        return store.record_selection(record_id, outcome, evidence, found.evidence_head, self.tick()).selection_id

    def project(self, store, identity: str, record_type: str = "steps", origin: str = ORIGIN_A,
                kind: str = HC) -> tuple[int, int]:
        """Archive, index, and select one projection of identity: (record_id, selection_id)."""
        self.observe(store, identity, record_type, origin, kind)
        record_id = self.record_id(store, identity, record_type, origin, kind)
        return record_id, self.select(store, record_id)

    def retract(self, store, identity: str, record_type: str = "steps", origin: str = ORIGIN_A) -> tuple[int, int]:
        """Archive and index a retraction of identity and select it: (sequence, selection_id)."""
        sequence = self.archive(store, BatchItem(retracted_identity=identity), record_type=record_type, change=True)
        store.index_items((RetractionEntry(self.position(sequence)),), self.tick())
        return sequence, self.select(store, self.record_id(store, identity, record_type, origin), "retraction")

    def process(self, store, record: tuple[int, int], outcome: OutcomeEntry) -> ProcessingOutcome:
        """Enqueue, claim, and complete one Processing Job for record: the recorded outcome."""
        when = self.tick()
        store.enqueue_processing(*record, outcome.processor_version, 3, when)
        lease = store.claim_processing_job(PROCESSOR, 3600, when)
        assert lease is not None
        self.assertEqual((lease.record_id, lease.selection_id), record)
        return store.complete_processing_job(lease.job_id, lease.fence, outcome, self.tick())

    def fail_processing(self, store, record: tuple[int, int], pv: str = PV) -> int:
        """Enqueue, claim, and fail one Processing Job deterministically: its job_id."""
        when = self.tick()
        store.enqueue_processing(*record, pv, 3, when)
        lease = store.claim_processing_job(PROCESSOR, 3600, when)
        assert lease is not None
        self.assertEqual((lease.record_id, lease.selection_id), record)
        store.fail_processing_job(lease.job_id, lease.fence, "deterministic", "synthetic-defect", None, self.tick())
        return lease.job_id

    def ftz(self, store, zone: str = "Asia/Kolkata", start: datetime | None = None, supersedes: tuple[int, ...] = ()):
        return store.record_fallback_timezone_policy(zone, start, supersedes, {}, 3, self.tick()).policy

    def ssp(self, store, origins=(OA,), first: date | None = None, last: date | None = None,
            supersedes: tuple[int, ...] = (), measure: str = MEASURE, record_type: str = "steps"):
        return store.record_source_selection_policy(measure, record_type, origins, first, last, supersedes, self.tick())

    # --- @6 building blocks -----------------------------------------------------------------------

    def generation(self, store, mapping=None, when: datetime | None = None) -> ProcessorGeneration:
        return store.register_processor_generation(GEN_MAP if mapping is None else mapping, when or self.tick())

    def seeded(self):
        """A store with one processed steps record and a registered generation: (store, generation, outcome)."""
        store = self.ready()
        outcome = self.process(store, self.project(store, "seed"), revision(b"seed"))
        return store, self.generation(store), outcome

    def request(self, store, keys, target: SummaryTarget, max_attempts: int = 3, when: datetime | None = None,
                **kwargs) -> RefreshRequest:
        return store.request_summary_refresh(tuple(keys), target, max_attempts, when or self.tick(), **kwargs)

    def claim(self, store, owner: str = OWNER, seconds: int = LEASE, when: datetime | None = None) -> RefreshLease:
        lease = store.claim_summary_refresh(owner, seconds, when or self.tick())
        self.assertIsNotNone(lease)
        assert lease is not None
        return lease

    def drain(self, store, count: int, owner: str = OWNER, seconds: int = LEASE) -> list[RefreshLease]:
        """Claim exactly count leases, each on a job not yet claimed here, then None. Bounded, so a claim that keeps
        returning a lease fails the test instead of hanging it."""
        leases: list[RefreshLease] = []
        for _ in range(count + 1):
            lease = store.claim_summary_refresh(owner, seconds, self.tick())
            if lease is None:
                break
            self.assertNotIn(lease.job_id, [item.job_id for item in leases], "a job was claimed twice")
            self.assertGreater(lease.fence, 0)
            leases.append(lease)
        else:
            self.fail(f"more than {count} claims returned a lease")
        self.assertEqual(len(leases), count)
        return leases

    def positions(self, store, record_type: str = "steps", pv: str = PV) -> tuple[ProcessedPosition, ...]:
        with store.read() as view:
            return view.processed_positions(record_type, pv)

    def entry(self, store, key: SummaryKey, *, label: bytes = b"s", pv: str = PV, watermark=None, contributors=(),
              ssp: int | None = None, body=DEFAULT, state: str = "present", conditions=()) -> SummaryEntry:
        if watermark is None:
            watermark = self.positions(store, key.record_type, pv)
        return SummaryEntry(
            header_bytes=b'{"synthetic-summary-header":"%s","marker":"%s"}' % (label, SUMMARY_MARKER),
            evidence_bytes=b'{"synthetic-evidence":"%s","marker":"%s"}' % (label, SUMMARY_MARKER),
            body_bytes=(b'{"synthetic-summary-body":"%s","marker":"%s"}' % (label, SUMMARY_MARKER)
                        if body is DEFAULT else body),
            contract_version=CONTRACT, derivation_version=DERIVATION, processor_version=pv,
            source_selection_policy=ssp, primary_state=state, evidence_conditions=tuple(conditions),
            watermark=tuple(watermark), contributors=tuple(contributors),
        )

    def leased(self, store, key: SummaryKey, target: SummaryTarget, **kwargs) -> RefreshLease:
        """Request key under target and claim it; no other job may be eligible."""
        self.request(store, (key,), target, **kwargs)
        lease = self.claim(store, when=kwargs.get("when"))
        self.assertEqual((lease.key, lease.target), (key, target))
        return lease

    def summarize(self, store, key: SummaryKey, target: SummaryTarget, entry: SummaryEntry | None = None,
                  when: datetime | None = None) -> StoredSummary:
        lease = self.leased(store, key, target)
        completion = store.complete_summary_refresh(lease.job_id, lease.fence, entry or self.entry(store, key),
                                                    when or self.tick())
        self.assertIsNotNone(completion.summary)
        assert completion.summary is not None
        return completion.summary

    def job(self, store, job_id: int) -> SummaryRefreshJob:
        with store.read() as view:
            found = view.summary_refresh_job(job_id)
        assert found is not None
        return found

    def jobs(self, store, key: SummaryKey) -> tuple[SummaryRefreshJob, ...]:
        with store.read() as view:
            return view.summary_refresh_jobs(key)

    def view(self, store, name: str, *args):
        with store.read() as view:
            return getattr(view, name)(*args)

    def changes(self, store, limit: int = 10_000) -> EvidenceChanges:
        with store.read() as view:
            return view.evidence_changes(limit)

    # --- assertions -------------------------------------------------------------------------------

    def assertRefused(self, failure: str, operation, *args, **kwargs) -> StoreRefusal:
        before = contents(self.path)
        with self.assertRaises(StoreRefusal) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.failure, failure)
        self.assertEqual(contents(self.path), before, f"{failure} changed the database")
        return caught.exception

    def assertRetryable(self, failure: str, operation, *args, **kwargs) -> StoreRetryable:
        before = contents(self.path)
        with self.assertRaises(StoreRetryable) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.failure, failure)
        self.assertEqual(contents(self.path), before, f"{failure} changed the database")
        return caught.exception

    def assertWritesNothing(self, operation, *args, **kwargs):
        before = contents(self.path)
        result = operation(*args, **kwargs)
        self.assertEqual(contents(self.path), before, "a call that writes nothing changed the database")
        return result

    def install_fault_triggers(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
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
        """Run operation with a fault after 1, 2, ... row writes until it succeeds; every fault must roll back."""
        failures = 0
        for writes_allowed in range(1, 400):
            faults.execute("UPDATE test_fault SET remaining = ?", (writes_allowed,))
            before = contents(self.path)
            try:
                result = operation()
            except sqlite3.DatabaseError:
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

    def key(self, offset: int = 0, record_type: str = "steps", measure: str = MEASURE) -> SummaryKey:
        return SummaryKey(measure, record_type, DAY + timedelta(days=offset))


class R54GovernedScopeContractVersion6(unittest.TestCase):
    def test_contract_6_extends_version_5(self) -> None:
        self.assertEqual(CONTRACT_6["scope"], "operational-store")
        self.assertEqual(CONTRACT_6["version"], "6")
        self.assertEqual(CONTRACT_6["kind"], CONTRACT_5["kind"])
        for name in ("observable_effects", "dependencies"):
            self.assertEqual(CONTRACT_6[name], CONTRACT_5[name], name)
        self.assertEqual([(item["name"], item["mode"]) for item in CONTRACT_6["inputs"]],
                         [(item["name"], item["mode"]) for item in CONTRACT_5["inputs"]])
        prior = CONTRACT_5["named_failures"]
        self.assertEqual(CONTRACT_6["named_failures"][: len(prior)], prior)
        added = CONTRACT_6["named_failures"][len(prior):]
        self.assertEqual(tuple(failure["name"] for failure in added), NEW_FAILURES)
        self.assertEqual({failure["outcome"] for failure in added}, {"invariant-refusal"})
        outputs = [item["name"] for item in CONTRACT_6["outputs"]]
        self.assertEqual(outputs[: len(CONTRACT_5["outputs"])], [item["name"] for item in CONTRACT_5["outputs"]])
        self.assertEqual(outputs[len(CONTRACT_5["outputs"]):], NEW_OUTPUTS)
        owns_5, owns_6 = CONTRACT_5["authority"]["owns"], CONTRACT_6["authority"]["owns"]
        self.assertEqual(owns_6[: len(owns_5)], owns_5)
        appended = " ".join(owns_6[len(owns_5):])
        for phrase in ("Processor Generation", "without activation", "Summary Refresh Jobs", "evidence token",
                       "Data Watermark", "contributor links", "bookmark", "policy"):
            self.assertIn(phrase, appended)
        does_not_own = " ".join(CONTRACT_6["authority"]["does_not_own"])
        for phrase in ("operational-store@7", "operational-store@8", "operational-store@9", "operational-store@10",
                       "parsing or validating summary header"):
            self.assertIn(phrase, does_not_own)
        oracle = CONTRACT_6["acceptance_oracle"]
        self.assertEqual(oracle["specification"], "packs/operational-store-6.spec.md")
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_operational_store_6.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_operational_store_6.sh")
        self.assertEqual([effect["name"] for effect in EFFECTS_6["effects"]],
                         [effect["name"] for effect in CONTRACT_6["observable_effects"]])

    def test_scope_map_row_does_not_contradict_contract_6(self) -> None:
        row = next(line for line in SCOPE_MAP.splitlines() if line.startswith("| `operational-store` |"))
        self.assertIn("| shared |", row)
        self.assertIn("transactional", row)
        self.assertIn("summary", row)
        self.assertIn("processor", row)
        self.assertIn("| — |", row)
        self.assertEqual(CONTRACT_6["dependencies"], [])

    def test_failures_match_contract_6(self) -> None:
        partitions = {
            "startup-refusal": set(store_module.STARTUP_FAILURES),
            "retryable": set(store_module.RETRYABLE_FAILURES),
            "invariant-refusal": set(store_module.INVARIANT_FAILURES),
        }
        for failure in CONTRACT_6["named_failures"]:
            self.assertIn(failure["name"], partitions[failure["outcome"]], failure["name"])
        self.assertLessEqual(set(NEW_FAILURES), set(store_module.FAILURES))
        self.assertLessEqual(set(NEW_FAILURES), set(store_module.INVARIANT_FAILURES))


class R55MigrationFiveIsAdditive(StoreCase):
    def load_fixture(self) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.executescript(FIXTURE.read_text(encoding="utf-8"))
        finally:
            connection.close()

    def test_new_database_records_migrations_1_to_5(self) -> None:
        self.open().close()
        known = [(version, digest(sql.encode("utf-8"))) for version, sql in store_module.MIGRATIONS]
        self.assertGreaterEqual(len(known), 5)
        self.assertEqual([version for version, _ in known[:5]], [1, 2, 3, 4, 5])
        # Migrations 1-4 are @5's, exactly as the published @5 module recorded them in the fixture.
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(FIXTURE.read_text(encoding="utf-8"))
            recorded = connection.execute("SELECT version, checksum FROM store_migrations ORDER BY version").fetchall()
        finally:
            connection.close()
        self.assertEqual(recorded, known[:4])
        state = contents(self.path)
        connection = sqlite3.connect(self.path)
        try:
            recorded = connection.execute("SELECT version, checksum FROM store_migrations ORDER BY version").fetchall()
        finally:
            connection.close()
        self.assertEqual(recorded, known)
        self.assertEqual(state["user_version"], known[-1][0])
        # Migration 5 only creates tables and indexes.
        statements = [statement for statement in store_module.MIGRATIONS[4][1].split(";") if statement.strip()]
        self.assertGreater(len(statements), 0)
        for statement in statements:
            code = "\n".join(line for line in statement.strip().splitlines() if not line.strip().startswith("--")).strip()
            self.assertRegex(code.upper(), r"^CREATE\s+(UNIQUE\s+)?(TABLE|INDEX)\s", code)
        self.assertLess(tables_created_by(4), tables_created_by(5))

    def test_version_5_database_requires_migration_and_is_untouched(self) -> None:
        self.load_fixture()
        before = contents(self.path)
        self.assertEqual(before["user_version"], 4)
        with self.assertRaises(StoreRefusal) as caught:
            open_store(self.path, reserve_bytes=RESERVE, free_bytes=lambda _path: PLENTY)
        self.assertEqual(caught.exception.failure, "store-migration-required")
        self.assertEqual(contents(self.path), before)

    def test_migrated_version_5_database_takes_summary_work(self) -> None:
        self.load_fixture()
        connection = sqlite3.connect(self.path)
        try:
            earlier = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            columns = {table: [row[1] for row in connection.execute(f'PRAGMA table_xinfo("{table}")')] for table in earlier}
            rowless = without_rowid(connection)

            def rows(table: str) -> list[tuple[object, ...]]:
                names = ", ".join(f'"{column}"' for column in columns[table])
                return table_rows(connection, table, names, rowless)

            original = {table: rows(table) for table in earlier if table != "store_migrations"}
            outcomes = connection.execute(
                "SELECT o.outcome_id, o.record_id, r.record_type, o.processor_version FROM processing_outcomes o "
                "JOIN source_records r USING (record_id) ORDER BY o.outcome_id").fetchall()
            selections = connection.execute(
                "SELECT s.selection_id, s.record_id, r.record_type FROM source_selections s "
                "JOIN source_records r USING (record_id) ORDER BY s.selection_id").fetchall()
            failed = connection.execute(
                "SELECT j.job_id, j.record_id, r.record_type, j.processor_version FROM processing_jobs j "
                "JOIN source_records r USING (record_id) WHERE j.state = 'failed' ORDER BY j.job_id").fetchall()
            policies = [row[0] for row in connection.execute(
                "SELECT policy_id FROM policies p WHERE NOT EXISTS "
                "(SELECT 1 FROM policy_supersessions x WHERE x.old_id = p.policy_id) ORDER BY policy_id")]
            self.assertEqual((len(outcomes), len(selections), len(failed), policies), (6, 10, 1, [2, 3]))
            for version, sql in store_module.MIGRATIONS[4:]:
                connection.executescript(sql)
                connection.execute(
                    "INSERT INTO store_migrations (version, checksum, applied_at) VALUES (?, ?, ?)",
                    (version, digest(sql.encode("utf-8")), "2026-11-27T09:00:00.000000Z"),
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
        with store.read() as view:
            # Every existing outcome, selection, and failed job is an evidence change.
            self.assertEqual(view.evidence_changes(1000), EvidenceChanges(EvidenceBookmark(0, 0), (
                *(EvidenceChange("outcome", *row) for row in outcomes),
                *(EvidenceChange("selection", *row, None) for row in selections),
                *(EvidenceChange("failed-job", *row) for row in failed))))
            # Every live policy awaits refresh; the superseded one does not.
            self.assertEqual(view.policies_awaiting_refresh(), (2, 3))
        fixture_a, fixture_b = INSTALLATION, INSTALLATION_B

        def t(clock: str) -> datetime:
            return datetime.fromisoformat(f"2026-11-20T{clock}:00+00:00")

        generation = self.generation(store, when=datetime(2026, 11, 27, 9, 0, tzinfo=UTC))
        with store.read() as view:
            self.assertEqual(view.processor_generations(), (generation,))
            # A's steps position stops at its completion, before the change batch whose projection
            # is still queued; B's covers its weight batch. Steps items set the processed times.
            self.assertEqual(view.processed_positions("steps", PV), (
                ProcessedPosition(fixture_b, 2, t("08:44"), t("08:51")),
                ProcessedPosition(fixture_a, 3, t("08:07"), t("08:29"))))
            # A's unsettled steps batches never hold back its weight position.
            self.assertEqual(view.processed_positions("weight", WPV), (
                ProcessedPosition(fixture_b, 2, t("08:44"), t("08:56")),
                ProcessedPosition(fixture_a, 6, t("08:36"), t("08:36"))))
            target = SummaryTarget(generation.generation_id, 3)
            evidence = view.summary_evidence("daily-steps", "steps", date(2026, 11, 18), date(2026, 11, 19), target, 0, 10)
            outcome = {row.outcome_id: row for record_id in (1, 2, 7) for row in view.processing_outcomes(record_id)}
            self.assertEqual(evidence.items, (
                SummaryEvidenceItem(1, HC, ORIGIN_A, 8, "conflict", None, False, outcome[1]),
                SummaryEvidenceItem(2, HC, ORIGIN_A, 2, "projection", outcome[2], False, outcome[2]),
                SummaryEvidenceItem(7, HC, ORIGIN_B, 9, "projection", outcome[6], False, outcome[6])))
            self.assertEqual(evidence.source_selection_policies, (view.policy(2),))
            self.assertIsNone(evidence.next_after)
            first = view.summary_evidence("daily-steps", "steps", date(2026, 11, 18), date(2026, 11, 19),
                                          SummaryTarget(generation.generation_id, 1), 0, 10)
            self.assertEqual(first.source_selection_policies, (view.policy(1),))
        # The migrated store takes summary work end to end.
        key = SummaryKey("daily-steps", "steps", date(2026, 11, 19))
        self.assertEqual(self.request(store, (key,), target, when=at(1), policy_id=3,
                                      acknowledge=EvidenceAcknowledgement(6, 10, (7,))), RefreshRequest(1, 0, 0))
        lease = self.claim(store, when=at(2))
        completion = store.complete_summary_refresh(lease.job_id, lease.fence, self.entry(
            store, key, ssp=2, watermark=self.positions(store), contributors=(1, 2, 6)), at(3))
        self.assertEqual(completion.summary.contributors, (1, 2, 6))  # type: ignore[union-attr]
        self.assertEqual(self.view(store, "policies_awaiting_refresh"), (2,))
        self.assertEqual(self.changes(store), EvidenceChanges(EvidenceBookmark(6, 10), ()))


class R56RegisteringAProcessorGeneration(StoreCase):
    def test_generation_is_registered_with_its_digest(self) -> None:
        store = self.ready()
        when = datetime(2026, 11, 27, 15, 0, tzinfo=IST)
        manifest = {"weight": WPV, "steps": PV, "heart-rate": HPV}
        generation = store.register_processor_generation(manifest, when)
        self.assertIsInstance(generation, ProcessorGeneration)
        self.assertIsInstance(generation.generation_id, int)
        self.assertGreater(generation.generation_id, 0)
        self.assertEqual(generation.manifest, (("heart-rate", HPV), ("steps", PV), ("weight", WPV)))
        canonical = b'{"heart-rate":"interpret-heart-rate@1","steps":"interpret-steps@1","weight":"interpret-weight@1"}'
        self.assertEqual(jcs(manifest), canonical)
        self.assertEqual(generation.manifest_sha256, digest(canonical))
        self.assertEqual(generation.registered_at, when)
        self.assertEqual(generation.registered_at.utcoffset(), timedelta(0))
        with store.read() as view:
            self.assertEqual(view.processor_generation(generation.generation_id), generation)
        # Identities ascend in registration order; each digest is RFC 8785's, also for
        # non-ASCII text, escaped characters, and keys outside the Basic Multilingual Plane.
        later = []
        for mapping in ({"steps": PV2}, {"pulsé": PV, "pulse": PV2}, {'quote"and\\slash': PV},
                        {"\U0001F600-steps": PV, "ﬁ-steps": PV2}):
            registered = self.generation(store, mapping)
            self.assertEqual(registered.manifest_sha256, digest(jcs(mapping)), mapping)
            self.assertEqual(registered.manifest, tuple(sorted(mapping.items())), mapping)
            later.append(registered)
        self.assertEqual(jcs({"pulsé": PV, "pulse": PV2}), '{"pulse":"interpret-steps@2","pulsé":"interpret-steps@1"}'.encode())
        self.assertTrue(jcs({"\U0001F600-steps": PV, "ﬁ-steps": PV2}).startswith('{"\U0001F600'.encode()))
        # Handwritten RFC 8785 vectors: the five short control escapes, other controls as lowercase \u00xx, DEL and
        # "/" left as they are, and composed and decomposed text kept distinct, decomposed first in UTF-16 order.
        controls = {"a\b\t\n\f\rz": PV, "ctl\x01\x1b\x1f": PV2, "del\x7f": HPV, "slash/type": WPV}
        decomposed = {"\u00e9-steps": PV, "e\u0301-steps": PV2}
        for mapping, canonical in (
            (controls, rb'{"a\b\t\n\f\rz":"interpret-steps@1","ctl\u0001\u001b\u001f":"interpret-steps@2",'
                       b'"del\x7f":"interpret-heart-rate@1","slash/type":"interpret-weight@1"}'),
            (decomposed, b'{"e\xcc\x81-steps":"interpret-steps@2","\xc3\xa9-steps":"interpret-steps@1"}'),
        ):
            registered = self.generation(store, mapping)
            self.assertEqual(jcs(mapping), canonical, mapping)
            self.assertEqual(registered.manifest_sha256, digest(canonical), mapping)
            self.assertEqual(registered.manifest, tuple(sorted(mapping.items())), mapping)
            later.append(registered)
        self.assertEqual(len(dict(later[-1].manifest)), 2)
        identities = [generation.generation_id] + [item.generation_id for item in later]
        self.assertEqual(identities, sorted(identities))
        self.assertEqual(len(set(identities)), len(identities))
        # Any mapping is accepted.
        proxy = self.generation(store, types.MappingProxyType({"sleep-session": "interpret-sleep-session@1"}))
        self.assertEqual(proxy.manifest, (("sleep-session", "interpret-sleep-session@1"),))
        self.assertEqual(self.view(store, "processor_generations"), (generation, *later, proxy))

    def test_identical_manifest_returns_the_existing_generation(self) -> None:
        store = self.ready()
        first = self.generation(store, {"steps": PV, "weight": WPV})
        second = self.generation(store, {"steps": PV2, "weight": WPV})
        self.free = 0
        again = self.assertWritesNothing(store.register_processor_generation, {"weight": WPV, "steps": PV}, self.tick())
        self.assertEqual(again, first)
        self.assertEqual(self.assertWritesNothing(store.register_processor_generation, {"steps": PV2, "weight": WPV},
                                                  self.tick()), second)
        self.free = PLENTY
        # A subset, a superset, or another version is another manifest.
        others = [self.generation(store, mapping) for mapping in (
            {"steps": PV}, {"steps": PV, "weight": WPV, "heart-rate": HPV}, {"steps": PV, "weight": "interpret-weight@2"})]
        identities = [first.generation_id, second.generation_id] + [item.generation_id for item in others]
        self.assertEqual(len(set(identities)), 5)
        self.assertEqual(self.view(store, "processor_generations"), (first, second, *others))

    def test_generation_refusals(self) -> None:
        store = self.ready()
        existing = self.generation(store, {"steps": PV})
        refusals = [
            ("timestamp-invalid", ({"weight": WPV}, NAIVE)),
            *[("processor-generation-invalid", (manifest, self.tick())) for manifest in (
                {}, [("steps", PV)], (("steps", PV),), None, "steps", 5, {"": PV}, {5: PV}, {None: PV},
                {("steps",): PV}, types.MappingProxyType({}),
                # Not Unicode scalar strings: lone surrogates, which RFC 8785 cannot serialize.
                {"\udead": PV}, {"abc\ud800": PV}, {"steps": PV, "\ud83d-steps": PV2},
                {"steps": "interpret-steps@1\udc00"})],
            *[("processor-version-invalid", ({"steps": version}, self.tick())) for version in (
                "interpret-steps", "Interpret-steps@1", "interpret_steps@1", "interpret-steps@x", "@1", "", 5, None,
                "interpret-steps@1 ")],
            ("processor-version-invalid", ({"steps": PV, "weight": "weight"}, self.tick())),
        ]
        for failure, args in refusals:
            with self.subTest(failure=failure, args=args):
                self.assertRefused(failure, store.register_processor_generation, *args)
        self.free = 0
        self.assertRetryable("storage-reserve-exhausted", store.register_processor_generation, {"weight": WPV}, self.tick())
        self.assertEqual(self.view(store, "processor_generations"), (existing,))

    def test_generations_are_immutable_and_inactive(self) -> None:
        store, _, outcome = self.seeded()
        self.assertEqual([item.name for item in dataclasses.fields(ProcessorGeneration)],
                         ["generation_id", "manifest", "manifest_sha256", "registered_at"])
        earlier = tables_created_by(4)
        before = contents(self.path)
        generation = self.generation(store, {"steps": PV2, "weight": WPV})
        after = contents(self.path)
        written = changed_tables(before, after)
        self.assertGreater(len(written), 0)
        # Registration activates nothing: no table of migrations 1-4 changes.
        self.assertEqual(written & earlier, set())
        # The generation's own records: rows registration added that carry its digest or manifest values.
        # Allocator metadata such as sqlite_sequence may change later and is not a generation record.
        markers = (generation.manifest_sha256.encode(), bytes.fromhex(generation.manifest_sha256), PV2.encode(),
                   WPV.encode())

        def own(row) -> bool:
            text = b"\x00".join(value if isinstance(value, bytes) else str(value).encode() for value in row)
            return any(marker in text for marker in markers)

        registered = {name: {row for row in set(after["tables"][name]) - set(before["tables"].get(name, []))  # type: ignore[index,union-attr]
                             if own(row)} for name in written}
        self.assertTrue(any(generation.manifest_sha256.encode() in b"\x00".join(
            value if isinstance(value, bytes) else str(value).encode() for value in row)
            or bytes.fromhex(generation.manifest_sha256) in row for rows in registered.values() for row in rows))
        generations = self.view(store, "processor_generations")
        # Later writes of every kind never change a generation's rows.
        key = self.key()
        target = SummaryTarget(generations[0].generation_id, 0)
        self.summarize(store, key, target, self.entry(store, key, contributors=(outcome.outcome_id,)))
        self.request(store, (key,), SummaryTarget(generation.generation_id, 0))
        self.generation(store, {"steps": PV2})
        self.generation(store, {"steps": PV2, "weight": WPV})
        final = contents(self.path)
        for name, rows in registered.items():
            self.assertLessEqual(rows, set(final["tables"][name]), name)  # type: ignore[index]
        self.assertEqual(self.view(store, "processor_generations")[: len(generations)], generations)


class R57RequestingASummaryRefresh(StoreCase):
    def test_request_creates_one_queued_job_per_key(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        keys = (self.key(0), self.key(1), self.key(0, "weight"))
        when = self.tick()
        self.assertEqual(self.request(store, keys, target, 5, when), RefreshRequest(3, 0, 0))
        identities = []
        for key in keys:
            (job,) = self.jobs(store, key)
            identities.append(job.job_id)
            self.assertIsInstance(job, SummaryRefreshJob)
            self.assertEqual(job, SummaryRefreshJob(
                job_id=job.job_id, key=key, target=target, state="queued", max_attempts=5, attempts=0,
                not_before=when, fence=0, lease_owner=None, lease_expires_at=None, failure=None, enqueued_at=when,
                closed_at=None, history=()))
            self.assertEqual(self.job(store, job.job_id), job)
        self.assertEqual(len(set(identities)), 3)
        # Another target for the same key is another job; a new key and target starts at token 1.
        other = self.generation(store, {"steps": PV2})
        self.assertEqual(self.request(store, (self.key(0),), SummaryTarget(other.generation_id, 0)), RefreshRequest(1, 0, 0))
        self.assertEqual([job.target for job in self.jobs(store, self.key(0))],
                         [target, SummaryTarget(other.generation_id, 0)])
        tokens = {(lease.key, lease.target): lease.token for lease in self.drain(store, 4)}
        self.assertEqual(set(tokens.values()), {1})
        self.assertEqual(len(tokens), 4)

    def test_request_advances_the_token_and_coalesces(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key, other = self.key(0), self.key(1)
        first = self.tick()
        self.assertEqual(self.request(store, (key,), target, 3, first), RefreshRequest(1, 0, 0))
        (job,) = self.jobs(store, key)
        self.assertEqual(self.request(store, (key,), target, 7), RefreshRequest(0, 1, 0))
        self.assertEqual(self.request(store, (other, key), target), RefreshRequest(1, 1, 0))
        # The coalesced job is left exactly as it was: its bound, times, and attempts.
        self.assertEqual(self.jobs(store, key), (job,))
        lease = self.claim(store)
        self.assertEqual((lease.job_id, lease.token), (job.job_id, 3))
        # A token belongs to one key and target: other started at 1, and another target starts at 1.
        second = self.claim(store)
        self.assertEqual((second.key, second.token), (other, 1))
        alternative = SummaryTarget(self.generation(store, {"steps": PV2}).generation_id, 0)
        self.request(store, (key,), alternative)
        self.assertEqual(self.claim(store).token, 1)
        # A delayed queued job stays delayed when a request coalesces behind it.
        store.fail_summary_refresh(second.job_id, second.fence, "transient", "synthetic-busy", at(400), self.tick())
        delayed = self.job(store, second.job_id)
        self.assertEqual(self.request(store, (other,), target), RefreshRequest(0, 1, 0))
        self.assertEqual(self.job(store, second.job_id), delayed)
        self.assertIsNone(store.claim_summary_refresh(OWNER, LEASE, self.tick()))
        self.assertEqual(self.claim(store, when=at(400)).token, 2)

    def test_request_on_a_leased_job_rearms_it(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        lease = self.leased(store, key, target)
        self.assertEqual(lease.token, 1)
        held = self.job(store, lease.job_id)
        self.assertEqual(self.request(store, (key,), target), RefreshRequest(0, 0, 1))
        self.assertEqual(self.request(store, (key,), target), RefreshRequest(0, 0, 1))
        self.assertEqual(self.job(store, lease.job_id), held)
        self.assertEqual(self.jobs(store, key), (held,))
        self.assertIsNone(store.claim_summary_refresh(OTHER_OWNER, LEASE, self.tick()))
        # Its lease holds a stale token, so it runs again under the newest one.
        completion = store.complete_summary_refresh(lease.job_id, lease.fence, self.entry(store, key), self.tick())
        self.assertEqual(completion, RefreshCompletion(None, True))
        again = self.claim(store)
        self.assertEqual((again.job_id, again.token), (lease.job_id, 3))
        stored = store.complete_summary_refresh(again.job_id, again.fence, self.entry(store, key), self.tick()).summary
        self.assertEqual(stored.token, 3)  # type: ignore[union-attr]

    def test_failed_job_does_not_block_a_new_request(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        lease = self.leased(store, key, target)
        failed = store.fail_summary_refresh(lease.job_id, lease.fence, "deterministic", "synthetic-defect", None, self.tick())
        self.assertEqual((failed.state, failed.failure), ("failed", "synthetic-defect"))
        when = self.tick()
        self.assertEqual(self.request(store, (key,), target, 2, when), RefreshRequest(1, 0, 0))
        old, new = self.jobs(store, key)
        self.assertEqual(old, failed)
        self.assertGreater(new.job_id, old.job_id)
        self.assertEqual((new.state, new.max_attempts, new.attempts, new.not_before, new.enqueued_at, new.history),
                         ("queued", 2, 0, when, when, ()))
        retry = self.claim(store)
        self.assertEqual((retry.job_id, retry.token), (new.job_id, 2))

    def test_request_refusals(self) -> None:
        store, generation, _ = self.seeded()
        policy = self.ftz(store)
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        good = dict(keys=(key,), target=target, max_attempts=3)

        def call(keys=DEFAULT, target=DEFAULT, max_attempts=DEFAULT, when=None, **kwargs):
            return store.request_summary_refresh(
                good["keys"] if keys is DEFAULT else keys, good["target"] if target is DEFAULT else target,
                good["max_attempts"] if max_attempts is DEFAULT else max_attempts, when or self.tick(), **kwargs)

        refusals = [
            ("timestamp-invalid", dict(when=NAIVE)),
            *[("summary-request-invalid", dict(keys=(SummaryKey(measure, "steps", DAY),))) for measure in (
                "", "Daily-Steps", "daily_steps", "daily steps", "-daily", "daily-", "a" * 65, None, 5)],
            *[("summary-request-invalid", dict(keys=(SummaryKey(MEASURE, record_type, DAY),))) for record_type in (
                "", None, 5)],
            *[("summary-request-invalid", dict(keys=(SummaryKey(MEASURE, "steps", local_date),))) for local_date in (
                "2026-09-21", datetime(2026, 9, 21, tzinfo=UTC), datetime(2026, 9, 21), None, 20260921)],
            ("summary-request-invalid", dict(keys=[key])),
            ("summary-request-invalid", dict(keys=((MEASURE, "steps", DAY),))),
            ("summary-request-invalid", dict(keys=(key, key))),
            ("summary-request-invalid", dict(keys=())),
            ("summary-request-invalid", dict(keys=None)),
            *[("summary-request-invalid", dict(target=SummaryTarget(value, 0))) for value in (0, -1, "1", 1.5, None)],
            *[("summary-request-invalid", dict(target=SummaryTarget(generation.generation_id, value)))
              for value in (-1, "0", 1.5, None)],
            ("summary-request-invalid", dict(target=(generation.generation_id, 0))),
            *[("summary-request-invalid", dict(max_attempts=value)) for value in (0, 101, 2.5, "3", None)],
            ("summary-request-invalid", dict(acknowledge=(0, 0, ()))),
            ("processor-generation-unknown", dict(target=SummaryTarget(424242, 0))),
            ("policy-unknown", dict(target=SummaryTarget(generation.generation_id, 424242))),
            ("policy-unknown", dict(target=SummaryTarget(generation.generation_id, policy.policy_id), policy_id=424242)),
            ("summary-target-invalid", dict(keys=(SummaryKey(MEASURE, "sleep-session", DAY),))),
            ("summary-target-invalid", dict(keys=(key, SummaryKey(MEASURE, "heart-rate", DAY)))),
            ("summary-target-invalid", dict(policy_id=policy.policy_id)),
        ]
        for failure, kwargs in refusals:
            with self.subTest(failure=failure, kwargs=kwargs):
                self.assertRefused(failure, call, **kwargs)
        self.free = 0
        self.assertRetryable("storage-reserve-exhausted", call)
        self.free = PLENTY
        self.assertEqual(self.jobs(store, key), ())
        # Boundaries that are accepted.
        self.assertEqual(call(keys=(SummaryKey("a" * 64, "steps", DAY),), max_attempts=1), RefreshRequest(1, 0, 0))
        self.assertEqual(call(max_attempts=100), RefreshRequest(1, 0, 0))
        self.assertEqual(call(target=SummaryTarget(generation.generation_id, policy.policy_id)), RefreshRequest(1, 0, 0))
        self.assertEqual(self.jobs(store, key)[0].max_attempts, 100)


class R58ClaimingAndRenewingRefreshLeases(StoreCase):
    def test_claim_takes_the_lowest_eligible_refresh_job(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        keys = (self.key(0), self.key(1), self.key(2))
        for key in keys:
            self.request(store, (key,), target, when=at(200))
        identities = {self.jobs(store, key)[0].job_id: key for key in keys}
        ordered = sorted(identities)
        # The lowest queued job is claimed; the time is normalized to UTC before adding the lease.
        claimed_at = at(201)
        first = store.claim_summary_refresh(OWNER, 60, claimed_at.astimezone(IST))
        self.assertEqual(first, RefreshLease(ordered[0], 1, OWNER, identities[ordered[0]], target, 1,
                                             claimed_at + timedelta(seconds=60)))
        assert first is not None
        self.assertEqual(first.expires_at.utcoffset(), timedelta(0))
        self.assertEqual(self.job(store, first.job_id), SummaryRefreshJob(
            first.job_id, identities[ordered[0]], target, "leased", 3, 1, at(200), 1, OWNER,
            claimed_at + timedelta(seconds=60), None, at(200), None,
            (RefreshAttempt(1, OWNER, 1, claimed_at, claimed_at + timedelta(seconds=60), None, None, None),)))
        # Delayed, it is not eligible until its not_before.
        store.fail_summary_refresh(first.job_id, first.fence, "transient", "synthetic-busy", at(300), at(202))
        second = self.claim(store, OTHER_OWNER, 60, at(203))
        self.assertEqual(second.job_id, ordered[1])
        third = self.claim(store, OWNER, LEASE, at(203.5))
        self.assertEqual(third.job_id, ordered[2])
        # An expired lease is eligible exactly at its expiry, before any later queued job.
        self.request(store, (self.key(3),), target, when=at(203.5))
        reclaimed = self.claim(store, OWNER, LEASE, at(204))
        self.assertEqual((reclaimed.job_id, reclaimed.fence), (ordered[1], 2))
        # A queued job is eligible exactly at its not_before, before a higher queued job.
        self.assertEqual(self.claim(store, OWNER, LEASE, at(300)).job_id, ordered[0])
        self.assertEqual(self.claim(store, OWNER, LEASE, at(300)).key, self.key(3))
        # Nothing eligible: None, writing nothing, also below the reserve.
        self.free = 0
        self.assertIsNone(self.assertWritesNothing(store.claim_summary_refresh, OWNER, LEASE, at(301)))

    def test_lease_carries_the_current_token(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        for _ in range(3):
            self.request(store, (key,), target)
        lease = self.claim(store)
        self.assertEqual(lease.token, 3)
        self.assertEqual(self.job(store, lease.job_id).history[-1].token, 3)
        self.request(store, (key,), target)
        # A renewal keeps the claim's token; only a new claim takes the newer one.
        renewed = store.renew_summary_refresh_lease(lease.job_id, lease.fence, LEASE, self.tick())
        self.assertEqual(renewed.token, 3)
        self.request(store, (key,), target)
        self.assertEqual(store.complete_summary_refresh(lease.job_id, lease.fence, self.entry(store, key), self.tick()),
                         RefreshCompletion(None, True))
        again = self.claim(store)
        self.assertEqual((again.job_id, again.fence, again.token), (lease.job_id, 2, 5))
        self.assertEqual([attempt.token for attempt in self.job(store, lease.job_id).history], [3, 5])

    def test_expired_refresh_lease_is_reclaimed(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        self.request(store, (key,), target, when=at(200))
        first = self.claim(store, OWNER, 60, at(201))
        self.assertIsNone(store.claim_summary_refresh(OTHER_OWNER, 60, at(201.5)))
        expiry = at(201) + timedelta(seconds=60)
        second = self.claim(store, OTHER_OWNER, 120, expiry)
        self.assertEqual(second, RefreshLease(first.job_id, 2, OTHER_OWNER, key, target, 1,
                                              expiry + timedelta(seconds=120)))
        job = self.job(store, first.job_id)
        self.assertEqual((job.state, job.attempts, job.fence, job.lease_owner, job.lease_expires_at),
                         ("leased", 2, 2, OTHER_OWNER, expiry + timedelta(seconds=120)))
        self.assertEqual(job.history, (
            RefreshAttempt(1, OWNER, 1, at(201), expiry, expiry, "lease-expired", None),
            RefreshAttempt(2, OTHER_OWNER, 1, expiry, expiry + timedelta(seconds=120), None, None, None)))
        entry = self.entry(store, key)
        self.assertRefused("lease-not-held", store.complete_summary_refresh, first.job_id, first.fence, entry, at(203))
        self.assertRefused("lease-not-held", store.renew_summary_refresh_lease, first.job_id, first.fence, 60, at(203))
        self.assertRefused("lease-not-held", store.fail_summary_refresh, first.job_id, first.fence, "deterministic",
                           "synthetic-defect", None, at(203))
        self.assertIsNotNone(store.complete_summary_refresh(second.job_id, second.fence, entry, at(203)).summary)

    def test_exhausted_expired_refresh_job_fails(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        self.request(store, (key,), target, 1, at(200))
        self.request(store, (key,), target, 1, at(200))  # an advance before the claim does not count
        first = self.claim(store, OWNER, 60, at(201))
        expiry = at(202)
        # Failing the exhausted job writes, so it is gated by the reserve.
        self.free = 0
        self.assertRetryable("storage-reserve-exhausted", store.claim_summary_refresh, OTHER_OWNER, 60, expiry)
        self.free = PLENTY
        self.assertIsNone(store.claim_summary_refresh(OTHER_OWNER, 60, expiry))
        failed = self.job(store, first.job_id)
        self.assertEqual((failed.state, failed.failure, failed.closed_at, failed.attempts, failed.lease_owner,
                          failed.lease_expires_at), ("failed", "lease-expired-retries-exhausted", expiry, 1, None, None))
        self.assertEqual(failed.history, (RefreshAttempt(1, OWNER, 2, at(201), expiry, expiry, "lease-expired", None),))
        self.assertIsNone(store.claim_summary_refresh(OTHER_OWNER, 60, at(5000)))
        # With the token advanced after the claim, newer evidence gets a fresh run instead (R63).
        other = self.key(1)
        self.request(store, (other,), target, 1, at(210))
        lease = self.claim(store, OWNER, 60, at(211))
        self.request(store, (other,), target, 1, at(211.5))
        again = self.claim(store, OTHER_OWNER, LEASE, at(212))
        self.assertEqual((again.job_id, again.fence, again.token), (lease.job_id, 2, 2))
        job = self.job(store, lease.job_id)
        self.assertEqual((job.state, job.attempts, job.failure, job.closed_at), ("leased", 1, None, None))
        self.assertEqual(job.history[0], RefreshAttempt(1, OWNER, 1, at(211), at(212), at(212), "lease-expired", None))

    def test_renew_extends_a_held_refresh_lease(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        self.request(store, (key,), target, when=at(200))
        lease = self.claim(store, OWNER, 60, at(201))
        renewed = store.renew_summary_refresh_lease(lease.job_id, lease.fence, 300, (at(201) + timedelta(seconds=30)).astimezone(IST))
        self.assertEqual(renewed, RefreshLease(lease.job_id, 1, OWNER, key, target, 1, at(201) + timedelta(seconds=330)))
        job = self.job(store, lease.job_id)
        self.assertEqual((job.state, job.fence, job.attempts, job.lease_expires_at),
                         ("leased", 1, 1, at(201) + timedelta(seconds=330)))
        self.assertEqual(job.history[-1].expires_at, at(201) + timedelta(seconds=60))
        # Still held after expiry, until another claim.
        late = at(220)
        self.assertEqual(store.renew_summary_refresh_lease(lease.job_id, 1, 60, late).expires_at, late + timedelta(seconds=60))
        refusals = [
            ("summary-refresh-job-unknown", (424242, 1, 60, at(221))),
            ("lease-not-held", (lease.job_id, 2, 60, at(221))),
            ("timestamp-invalid", (lease.job_id, 1, 60, NAIVE)),
            *[("summary-request-invalid", (lease.job_id, 1, value, at(221))) for value in (0, 86_401, 1.5, "60", None)],
        ]
        for failure, args in refusals:
            with self.subTest(failure=failure, args=args):
                self.assertRefused(failure, store.renew_summary_refresh_lease, *args)
        self.free = 0
        self.assertRetryable("storage-reserve-exhausted", store.renew_summary_refresh_lease, lease.job_id, 1, 60, at(221))
        self.free = PLENTY
        self.assertEqual(store.renew_summary_refresh_lease(lease.job_id, 1, 86_400, at(221)).expires_at,
                         at(221) + timedelta(days=1))
        self.assertEqual(store.renew_summary_refresh_lease(lease.job_id, 1, 1, at(222)).expires_at,
                         at(222) + timedelta(seconds=1))
        store.complete_summary_refresh(lease.job_id, 1, self.entry(store, key), at(223))
        self.assertRefused("lease-not-held", store.renew_summary_refresh_lease, lease.job_id, 1, 60, at(224))
        # After a reclaim, the earlier fence is no longer held.
        self.request(store, (key,), target, when=at(225))
        lease = self.claim(store, OWNER, 60, at(226))
        self.claim(store, OTHER_OWNER, 60, at(230))
        self.assertRefused("lease-not-held", store.renew_summary_refresh_lease, lease.job_id, lease.fence, 60, at(231))

    def test_two_handles_never_hold_one_refresh_job(self) -> None:
        store, generation, _ = self.seeded()
        other = self.open()
        target = SummaryTarget(generation.generation_id, 0)
        self.request(store, (self.key(),), target, when=at(200))
        first = self.claim(store, OWNER, 60, at(201))
        self.assertIsNone(other.claim_summary_refresh(OTHER_OWNER, 60, at(201)))
        second = other.claim_summary_refresh(OTHER_OWNER, 60, at(203))
        assert second is not None
        self.assertEqual(second.job_id, first.job_id)
        self.assertGreater(second.fence, first.fence)
        self.assertRefused("lease-not-held", store.complete_summary_refresh, first.job_id, first.fence,
                           self.entry(store, self.key()), at(204))
        other.complete_summary_refresh(second.job_id, second.fence, self.entry(store, self.key()), at(204))
        # Stress, not a forced interleaving: two workers on separate handles race to claim
        # twelve jobs, and no job is ever claimed twice. Either may claim all.
        keys = [self.key(offset) for offset in range(1, 13)]
        self.request(store, keys, target, when=at(205))
        jobs = {self.jobs(store, key)[0].job_id for key in keys}
        claims: dict[str, list[int]] = {"left": [], "right": []}
        errors: list[BaseException] = []
        start = threading.Barrier(2, timeout=30)

        def work(name: str) -> None:
            try:
                handle = open_store(self.path, reserve_bytes=RESERVE, free_bytes=lambda _path: PLENTY)
                try:
                    start.wait()
                    # Bounded: twelve claims, the final None, and retries of the permitted store-busy.
                    for _ in range(len(jobs) + 200):
                        try:
                            lease = handle.claim_summary_refresh(name, 3600, at(206))
                        except StoreRetryable as busy:
                            if busy.failure != "store-busy":
                                raise
                            continue
                        if lease is None:
                            break
                        claims[name].append(lease.job_id)
                    else:
                        raise AssertionError(f"{name} never saw the queue empty")
                finally:
                    handle.close()
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)
                start.abort()

        threads = [threading.Thread(target=work, args=(name,), daemon=True) for name in claims]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual([thread.is_alive() for thread in threads], [False, False], "a claim worker did not finish")
        for error in errors:
            if not isinstance(error, threading.BrokenBarrierError):
                raise error
        self.assertEqual(errors, [])
        self.assertEqual(set(claims["left"]) & set(claims["right"]), set())
        self.assertEqual(sorted(claims["left"] + claims["right"]), sorted(jobs))
        for job_id in jobs:
            self.assertEqual(len(self.job(store, job_id).history), 1)


class R59CompletingARefresh(StoreCase):
    def test_current_completion_appends_the_summary(self) -> None:
        store, generation, outcome = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        lease = self.leased(store, key, target, when=at(200))
        entry = self.entry(store, key, contributors=(outcome.outcome_id,), conditions=("late-data", "partial-day"))
        when = datetime(2026, 11, 27, 18, 0, tzinfo=IST)
        completion = store.complete_summary_refresh(lease.job_id, lease.fence, entry, when)
        self.assertIsInstance(completion, RefreshCompletion)
        self.assertFalse(completion.requeued)
        stored = completion.summary
        self.assertIsInstance(stored, StoredSummary)
        assert stored is not None
        self.assertIsInstance(stored.summary_id, int)
        self.assertGreater(stored.summary_id, 0)
        self.assertEqual(stored, StoredSummary(
            summary_id=stored.summary_id, key=key, target=target, token=1,
            header_bytes=entry.header_bytes, evidence_bytes=entry.evidence_bytes, body_bytes=entry.body_bytes,
            contract_version=CONTRACT, derivation_version=DERIVATION, processor_version=PV, source_selection_policy=None,
            primary_state="present", evidence_conditions=("late-data", "partial-day"), watermark=entry.watermark,
            contributors=(outcome.outcome_id,), summary_sha256=digest(entry.header_bytes),
            evidence_sha256=digest(entry.evidence_bytes), body_sha256=digest(entry.body_bytes),  # type: ignore[arg-type]
            job_id=lease.job_id, fence=lease.fence, recorded_at=when))
        self.assertEqual(stored.recorded_at.utcoffset(), timedelta(0))
        self.assertEqual(len(entry.watermark), 1)
        job = self.job(store, lease.job_id)
        self.assertEqual((job.state, job.attempts, job.fence, job.lease_owner, job.lease_expires_at, job.failure,
                          job.closed_at), ("completed", 1, 1, None, None, None, when))
        self.assertEqual(job.history, (RefreshAttempt(1, OWNER, 1, lease.expires_at - timedelta(seconds=LEASE),
                                                      lease.expires_at, when, "completed", None),))
        with store.read() as view:
            self.assertEqual(view.summaries(key), (stored,))
            self.assertEqual(view.latest_summary(key, target), stored)
            counts = view.refresh_counts(when)
        self.assertEqual((counts.summaries, counts.jobs_completed, counts.stale_results), (1, 1, 0))
        # A missing summary has no body.
        other = self.key(1)
        missing = self.summarize(store, other, target, self.entry(store, other, body=None, state="missing"))
        self.assertEqual((missing.body_bytes, missing.body_sha256, missing.primary_state), (None, None, "missing"))
        self.assertRefused("lease-not-held", store.complete_summary_refresh, lease.job_id, lease.fence, entry, self.tick())

    def test_stale_completion_discards_and_requeues(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        lease = self.leased(store, key, target, max_attempts=1)
        self.request(store, (key,), target)
        stale = self.entry(store, key, label=b"stale-result-marker")
        when = self.tick()
        self.assertEqual(store.complete_summary_refresh(lease.job_id, lease.fence, stale, when), RefreshCompletion(None, True))
        job = self.job(store, lease.job_id)
        # Re-queued with a fresh retry budget; the fence stays until the next claim.
        self.assertEqual((job.state, job.attempts, job.not_before, job.lease_owner, job.lease_expires_at, job.failure,
                          job.closed_at, job.fence, job.max_attempts), ("queued", 0, when, None, None, None, None, 1, 1))
        self.assertEqual(job.history[-1], RefreshAttempt(1, OWNER, 1, job.history[-1].claimed_at, lease.expires_at, when,
                                                         "stale", None))
        state = contents(self.path)
        self.assertNotIn(b"stale-result-marker", blob_text(state))
        with store.read() as view:
            self.assertEqual(view.summaries(key), ())
            self.assertIsNone(view.latest_summary(key, target))
            self.assertEqual(view.refresh_counts(when).stale_results, 1)
        again = self.claim(store)
        self.assertEqual((again.job_id, again.fence, again.token), (lease.job_id, 2, 2))
        self.assertEqual(self.job(store, lease.job_id).attempts, 1)
        stored = store.complete_summary_refresh(again.job_id, again.fence, self.entry(store, key), self.tick()).summary
        self.assertEqual((stored.token, stored.fence), (2, 2))  # type: ignore[union-attr]
        self.assertEqual(self.job(store, lease.job_id).state, "completed")

    def test_identical_result_is_not_appended_twice(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        first_entry = self.entry(store, key, label=b"first")
        first = self.summarize(store, key, target, first_entry)
        lease = self.leased(store, key, target)
        self.assertEqual(lease.token, 2)
        before = contents(self.path)
        when = self.tick()
        completion = store.complete_summary_refresh(lease.job_id, lease.fence, first_entry, when)
        self.assertEqual(completion, RefreshCompletion(first, False))
        job = self.job(store, lease.job_id)
        self.assertEqual((job.state, job.closed_at, job.history[-1].result), ("completed", when, "completed"))
        self.assertEqual(self.view(store, "summaries", key), (first,))
        # Only job state changed: no table gained a row.
        after = contents(self.path)
        for name in changed_tables(before, after):
            self.assertEqual(len(after["tables"][name]), len(before["tables"][name]), name)  # type: ignore[index]
        # A different result, the same result under another target, and a result equal to an
        # older (not the latest) revision are each appended.
        second_entry = dataclasses.replace(first_entry, evidence_conditions=("late-data",))
        second = self.summarize(store, key, target, second_entry)
        self.assertEqual((second.token, second.evidence_conditions), (3, ("late-data",)))
        third = self.summarize(store, key, target, first_entry)
        self.assertEqual(third.token, 4)
        self.assertNotEqual(third.summary_id, first.summary_id)
        alternative = SummaryTarget(generation.generation_id, self.ftz(store).policy_id)
        other = self.summarize(store, key, alternative, first_entry)
        self.assertEqual((other.target, other.token), (alternative, 1))
        self.assertEqual([item.summary_id for item in self.view(store, "summaries", key)],
                         [first.summary_id, second.summary_id, third.summary_id, other.summary_id])

    def test_completion_refusals(self) -> None:
        store, generation, outcome = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        lease = self.leased(store, key, target)
        entry = self.entry(store, key)
        refusals = [
            ("summary-refresh-job-unknown", (424242, 1, entry, self.tick())),
            ("lease-not-held", (lease.job_id, 2, entry, self.tick())),
            ("lease-not-held", (lease.job_id, 0, entry, self.tick())),
            ("timestamp-invalid", (lease.job_id, lease.fence, entry, NAIVE)),
            ("summary-entry-invalid", (lease.job_id, lease.fence, dataclasses.replace(entry, contract_version="x"), self.tick())),
            ("summary-watermark-invalid", (lease.job_id, lease.fence, dataclasses.replace(entry, watermark=()), self.tick())),
            ("summary-contributor-invalid", (lease.job_id, lease.fence, dataclasses.replace(entry, contributors=(424242,)),
                                             self.tick())),
        ]
        for failure, args in refusals:
            with self.subTest(failure=failure):
                self.assertRefused(failure, store.complete_summary_refresh, *args)
        self.free = 0
        self.assertRetryable("storage-reserve-exhausted", store.complete_summary_refresh, lease.job_id, lease.fence, entry,
                             self.tick())
        # A stale lease's entry is still checked, and its re-queue is gated too.
        self.free = PLENTY
        self.request(store, (key,), target)
        for failure, bad in (("summary-entry-invalid", dataclasses.replace(entry, header_bytes=b"")),
                             ("summary-watermark-invalid", dataclasses.replace(entry, watermark=())),
                             ("summary-contributor-invalid", dataclasses.replace(entry, contributors=(0,)))):
            with self.subTest(stale=failure):
                self.assertRefused(failure, store.complete_summary_refresh, lease.job_id, lease.fence, bad, self.tick())
        self.free = 0
        self.assertRetryable("storage-reserve-exhausted", store.complete_summary_refresh, lease.job_id, lease.fence, entry,
                             self.tick())
        self.free = PLENTY
        self.assertEqual(self.job(store, lease.job_id).state, "leased")
        self.assertEqual(store.complete_summary_refresh(lease.job_id, lease.fence, entry, self.tick()),
                         RefreshCompletion(None, True))
        self.assertRefused("lease-not-held", store.complete_summary_refresh, lease.job_id, lease.fence, entry, self.tick())
        # A repeat completion after success, and an identical result below the reserve.
        again = self.claim(store)
        stored = store.complete_summary_refresh(again.job_id, again.fence, entry, self.tick()).summary
        self.assertRefused("lease-not-held", store.complete_summary_refresh, again.job_id, again.fence, entry, self.tick())
        lease = self.leased(store, key, target)
        self.free = 0
        self.assertRetryable("storage-reserve-exhausted", store.complete_summary_refresh, lease.job_id, lease.fence, entry,
                             self.tick())
        self.free = PLENTY
        self.assertEqual(store.complete_summary_refresh(lease.job_id, lease.fence, entry, self.tick()).summary, stored)
        self.assertEqual(outcome.kind, "revision")

    def test_reclaimed_refresh_lease_cannot_complete(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        self.request(store, (key,), target, when=at(200))
        first = self.claim(store, OWNER, 60, at(201))
        second = self.claim(store, OTHER_OWNER, 60, at(202))
        self.assertEqual((second.job_id, second.fence, second.token), (first.job_id, 2, 1))
        entry = self.entry(store, key)
        self.assertRefused("lease-not-held", store.complete_summary_refresh, first.job_id, first.fence, entry, at(202.5))
        self.assertRefused("lease-not-held", store.fail_summary_refresh, first.job_id, first.fence, "transient",
                           "synthetic-busy", at(203), at(202.5))
        self.assertRefused("lease-not-held", store.renew_summary_refresh_lease, first.job_id, first.fence, 60, at(202.5))
        stored = store.complete_summary_refresh(second.job_id, second.fence, entry, at(203)).summary
        self.assertEqual((stored.job_id, stored.fence, stored.token), (first.job_id, 2, 1))  # type: ignore[union-attr]


class R60TheSummaryEntry(StoreCase):
    def test_entry_shape_is_checked(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        lease = self.leased(store, key, target)
        entry = self.entry(store, key)
        bad = [
            *[dict(header_bytes=value) for value in (b"", "text", None, bytearray(b"x"), 5)],
            *[dict(evidence_bytes=value) for value in (b"", "text", None, bytearray(b"x"))],
            *[dict(body_bytes=value) for value in (b"", "text", bytearray(b"x"), 5)],
            *[dict(contract_version=value) for value in (
                "summary-contract", "Summary-contract@1", "summary_contract@1", "summary-contract@x", "", None, 1)],
            *[dict(derivation_version=value) for value in ("derive-daily-steps", "derive-daily-steps@", "", None)],
            *[dict(primary_state=value) for value in ("Present", "pre_sent", "-present", "a" * 65, "", None, 5)],
            *[dict(evidence_conditions=value) for value in (
                ("partial-day", "late-data"), ("late-data", "late-data"), ("Late-data",), ("a" * 65,), ("",), (5,),
                ["late-data"], "late-data", None)],
            *[dict(processor_version=value) for value in (PV2, WPV, "", None)],
            dict(source_selection_policy=424242),
        ]
        for change in bad:
            with self.subTest(change=change):
                self.assertRefused("summary-entry-invalid", store.complete_summary_refresh, lease.job_id, lease.fence,
                                   dataclasses.replace(entry, **change), self.tick())
        for value in (None, (1, 2), "entry"):
            with self.subTest(entry=value):
                self.assertRefused("summary-entry-invalid", store.complete_summary_refresh, lease.job_id, lease.fence,
                                   value, self.tick())
        accepted = dataclasses.replace(entry, primary_state="a" * 64, evidence_conditions=("a" * 64, "b"), body_bytes=None)
        stored = store.complete_summary_refresh(lease.job_id, lease.fence, accepted, self.tick()).summary
        self.assertEqual((stored.primary_state, stored.evidence_conditions, stored.body_bytes),  # type: ignore[union-attr]
                         ("a" * 64, ("a" * 64, "b"), None))

    def test_summary_digests_are_computed_by_the_store(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        # The bytes are opaque: not JSON, not UTF-8, with NUL bytes.
        entry = dataclasses.replace(self.entry(store, key), header_bytes=b"\x00\xff synthetic header \x00",
                                    evidence_bytes=b"\xfe\x00synthetic evidence", body_bytes=b"\x80synthetic body\x00")
        stored = self.summarize(store, key, target, entry)
        self.assertEqual((stored.header_bytes, stored.evidence_bytes, stored.body_bytes),
                         (entry.header_bytes, entry.evidence_bytes, entry.body_bytes))
        self.assertEqual((stored.summary_sha256, stored.evidence_sha256, stored.body_sha256),
                         (digest(entry.header_bytes), digest(entry.evidence_bytes), digest(entry.body_bytes)))  # type: ignore[arg-type]
        # Each digest covers its own bytes only.
        other = self.summarize(store, key, target, dataclasses.replace(entry, evidence_bytes=b"other evidence", body_bytes=None))
        self.assertEqual(other.summary_sha256, stored.summary_sha256)
        self.assertEqual(other.evidence_sha256, digest(b"other evidence"))
        self.assertIsNone(other.body_sha256)
        self.assertEqual(self.view(store, "summaries", key), (stored, other))

    def test_processor_version_must_match_the_generation(self) -> None:
        store = self.ready()
        record = self.project(store, "two-versions")
        self.process(store, record, revision(b"v1"))
        self.process(store, record, revision(b"v2", pv=PV2))
        first = self.generation(store, {"steps": PV, "weight": WPV})
        second = self.generation(store, {"steps": PV2, "weight": WPV})
        key = self.key()
        for generation, right, wrong in ((first, PV, PV2), (second, PV2, PV)):
            target = SummaryTarget(generation.generation_id, 0)
            lease = self.leased(store, key, target)
            for version in (wrong, WPV, "interpret-steps@3"):
                with self.subTest(generation=generation.generation_id, version=version):
                    entry = dataclasses.replace(self.entry(store, key, pv=right), processor_version=version)
                    self.assertRefused("summary-entry-invalid", store.complete_summary_refresh, lease.job_id,
                                       lease.fence, entry, self.tick())
            stored = store.complete_summary_refresh(lease.job_id, lease.fence, self.entry(store, key, pv=right),
                                                    self.tick()).summary
            self.assertEqual(stored.processor_version, right)  # type: ignore[union-attr]

    def test_source_selection_policy_must_be_the_applicable_one(self) -> None:
        store, generation, _ = self.seeded()
        self.project(store, "weight-origin", "weight")
        september = (date(2026, 9, 1), date(2026, 9, 30))
        p1 = self.ssp(store, (OA,), *september).policy_id
        f = self.ftz(store).policy_id
        p2 = self.ssp(store, (OA,), *september, supersedes=(p1,)).policy_id
        p3 = self.ssp(store, (OA,), date(2026, 10, 1), date(2026, 10, 31)).policy_id
        p_measure = self.ssp(store, (OA,), *september, measure="other-steps").policy_id
        p_weight = self.ssp(store, (OA,), *september, record_type="weight").policy_id
        cases = [
            (self.key(), 0, None, (p1, p2)),
            (self.key(), p1, p1, (None, p2, f)),
            (self.key(), f, p1, (None, p2)),
            (self.key(), p2, p2, (None, p1, p3)),
            (self.key(), p_weight, p2, (None, p1, p3, p_measure, p_weight)),
            (SummaryKey(MEASURE, "steps", date(2026, 10, 5)), p_weight, p3, (None, p2)),
            (SummaryKey(MEASURE, "steps", date(2026, 9, 30)), p_weight, p2, (None, p3)),
            (SummaryKey(MEASURE, "steps", date(2026, 8, 31)), p_weight, None, (p1, p2, p3)),
            (SummaryKey("other-steps", "steps", DAY), p_weight, p_measure, (None, p2)),
            (SummaryKey(MEASURE, "weight", DAY), p_weight, p_weight, (None, p2)),
            (SummaryKey("weekly-steps", "steps", DAY), p_weight, None, (p2, p_measure)),
        ]
        for key, horizon, right, wrongs in cases:
            target = SummaryTarget(generation.generation_id, horizon)
            pv = generation.manifest[[name for name, _ in generation.manifest].index(key.record_type)][1]
            if key.record_type == "weight" and not self.positions(store, "weight", WPV):
                self.process(store, (self.record_id(store, "weight-origin", "weight"),
                                     self.view(store, "source_record", self.record_id(store, "weight-origin", "weight")).selections[-1].selection_id),
                             revision(b"w", pv=WPV))
            lease = self.leased(store, key, target)
            for wrong in wrongs:
                with self.subTest(key=key, horizon=horizon, wrong=wrong):
                    self.assertRefused("summary-entry-invalid", store.complete_summary_refresh, lease.job_id, lease.fence,
                                       self.entry(store, key, pv=pv, ssp=wrong), self.tick())
            stored = store.complete_summary_refresh(lease.job_id, lease.fence, self.entry(store, key, pv=pv, ssp=right),
                                                    self.tick()).summary
            self.assertEqual(stored.source_selection_policy, right)  # type: ignore[union-attr]

    def test_summaries_are_append_only(self) -> None:
        store, generation, outcome = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        # Tables that only job bookkeeping changes: a request, a claim, a renewal, and a failure.
        before = contents(self.path)
        lease = self.leased(store, self.key(5), target)
        store.renew_summary_refresh_lease(lease.job_id, lease.fence, LEASE, self.tick())
        store.fail_summary_refresh(lease.job_id, lease.fence, "deterministic", "synthetic-defect", None, self.tick())
        bookkeeping = changed_tables(before, contents(self.path))
        first = self.summarize(store, key, target, self.entry(store, key, contributors=(outcome.outcome_id,)))
        stored = contents(self.path)
        # Later completions, stale results, failures, and requests only append to every other table.
        self.summarize(store, key, target, self.entry(store, key, label=b"second"))
        lease = self.leased(store, key, target)
        self.request(store, (key,), target)
        store.complete_summary_refresh(lease.job_id, lease.fence, self.entry(store, key, label=b"stale"), self.tick())
        lease = self.claim(store)
        store.fail_summary_refresh(lease.job_id, lease.fence, "deterministic", "synthetic-defect", None, self.tick())
        self.summarize(store, self.key(1), target, self.entry(store, self.key(1), contributors=()))
        final = contents(self.path)
        for name in set(stored["tables"]) - bookkeeping:  # type: ignore[arg-type]
            self.assertLessEqual(set(stored["tables"][name]), set(final["tables"][name]), name)  # type: ignore[index]
        self.assertEqual(self.view(store, "summaries", key)[0], first)


class R61TheWatermarkMatchesTheStore(StoreCase):
    def test_watermark_must_match_store_positions(self) -> None:
        store = self.ready()
        record = self.project(store, "a-1")
        processed = self.process(store, record, revision(b"a-1"))
        self.archive_completion(store)
        self.project(store, "a-3")  # selected, never processed: holds A's position at 2
        self.switch_installation(store)
        self.process(store, self.project(store, "b-1"), revision(b"b-1"))
        generation = self.generation(store)
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        a_position, b_position = (ProcessedPosition(INSTALLATION, 2, self.receipts[(INSTALLATION, 2)],
                                                    self.receipts[(INSTALLATION, 2)]),
                                  self.positions(store)[0])
        self.assertEqual(self.positions(store), (b_position, a_position))
        a_earlier = ProcessedPosition(INSTALLATION, 1, self.receipts[(INSTALLATION, 1)], processed.recorded_at)
        a_ahead = ProcessedPosition(INSTALLATION, 3, self.receipts[(INSTALLATION, 3)], self.receipts[(INSTALLATION, 3)])
        unknown = ProcessedPosition("ffffffff-0000-4000-8000-000000000000", 1, self.receipts[(INSTALLATION, 1)],
                                    self.receipts[(INSTALLATION, 1)])
        refused = [
            (), (a_position, b_position), (b_position, b_position), (b_position, a_position, unknown),
            (dataclasses.replace(b_position, sequence=99),),
            (dataclasses.replace(b_position, sequence=0),),
            (dataclasses.replace(b_position, receipt_time=b_position.receipt_time + MICRO),),
            (dataclasses.replace(b_position, processed_at=b_position.processed_at + MICRO),),
            (dataclasses.replace(b_position, processed_at=b_position.receipt_time),),
            (b_position, a_ahead),
            (b_position, dataclasses.replace(a_earlier, processed_at=a_position.processed_at)),
            (b_position, dataclasses.replace(a_earlier, processed_at=a_earlier.receipt_time)),
            (b_position, dataclasses.replace(a_earlier, receipt_time=a_position.receipt_time)),
            # The other fold of the repeated hour is another instant, an hour later.
            (ProcessedPosition(INSTALLATION_B, b_position.sequence, repeated(b_position.receipt_time, 1),
                               repeated(b_position.processed_at, 1)),),
            (ProcessedPosition(INSTALLATION_B, b_position.sequence, repeated(b_position.receipt_time, 0),
                               repeated(b_position.processed_at, 1)),),
        ]
        lease = self.leased(store, key, target)
        for watermark in refused:
            with self.subTest(watermark=watermark):
                self.assertRefused("summary-watermark-invalid", store.complete_summary_refresh, lease.job_id,
                                   lease.fence, self.entry(store, key, watermark=watermark), self.tick())
        # Each accepted watermark, and the UTC positions stored for it.
        accepted = [
            ((b_position, a_position), (b_position, a_position)),
            ((b_position,), (b_position,)),
            ((a_position,), (a_position,)),
            ((b_position, a_earlier), (b_position, a_earlier)),
            ((ProcessedPosition(INSTALLATION_B, b_position.sequence, b_position.receipt_time.astimezone(IST),
                                b_position.processed_at.astimezone(IST)),), (b_position,)),
            # The same instants through the repeated hour, which no Python equality matches across zones.
            ((ProcessedPosition(INSTALLATION_B, b_position.sequence, repeated(b_position.receipt_time, 0),
                                repeated(b_position.processed_at, 0)),), (b_position,)),
        ]
        self.assertNotEqual(repeated(b_position.receipt_time, 0), b_position.receipt_time)
        self.assertEqual(repeated(b_position.receipt_time, 0).astimezone(UTC), b_position.receipt_time)
        for watermark, expected in accepted:
            with self.subTest(accepted=watermark):
                stored = store.complete_summary_refresh(lease.job_id, lease.fence,
                                                        self.entry(store, key, watermark=watermark), self.tick()).summary
                self.assertEqual(stored.watermark, expected)  # type: ignore[union-attr]
                for position in stored.watermark:  # type: ignore[union-attr]
                    self.assertEqual(position.receipt_time.utcoffset(), timedelta(0))
                    self.assertEqual(position.processed_at.utcoffset(), timedelta(0))
                lease = self.leased(store, key, target)

    def test_watermark_cannot_run_ahead_of_processing(self) -> None:
        store = self.ready()
        first = self.project(store, "first")
        self.process(store, first, revision(b"first"))
        second = self.project(store, "second")  # sequence 2: unprocessed
        third = self.project(store, "third")
        self.process(store, third, revision(b"third"))  # sequence 3: processed, behind 2
        generation = self.generation(store)
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        (current,) = self.positions(store)
        self.assertEqual(current.sequence, 1)

        def at_sequence(sequence: int, processed_at: datetime | None = None) -> ProcessedPosition:
            receipt = self.receipts[(INSTALLATION, sequence)]
            return ProcessedPosition(INSTALLATION, sequence, receipt, processed_at or receipt)

        lease = self.leased(store, key, target)
        for sequence in (2, 3):
            for processed_at in (None, current.processed_at, self.tick()):
                with self.subTest(sequence=sequence, processed_at=processed_at):
                    self.assertRefused("summary-watermark-invalid", store.complete_summary_refresh, lease.job_id,
                                       lease.fence, self.entry(store, key, watermark=(at_sequence(sequence, processed_at),)),
                                       self.tick())
        # Processing under another version does not move this version's position.
        self.process(store, second, revision(b"second", pv=PV2))
        self.assertEqual(self.positions(store), (current,))
        self.assertEqual(self.positions(store, "steps", PV2), ())  # batch 1 has no outcome under it
        # A weight batch left unprocessed never holds back the steps position (Decision 10).
        self.project(store, "weight-backlog", "weight")
        self.process(store, second, revision(b"second"))
        (moved,) = self.positions(store)
        self.assertEqual(moved.sequence, 4)
        self.assertEqual(moved.receipt_time, self.receipts[(INSTALLATION, 4)])
        stored = store.complete_summary_refresh(lease.job_id, lease.fence, self.entry(store, key, watermark=(moved,)),
                                                self.tick()).summary
        self.assertEqual(stored.watermark, (moved,))  # type: ignore[union-attr]
        self.assertEqual(self.positions(store, "weight", WPV), ())


class R62ContributorLinks(StoreCase):
    def build(self):
        store = self.ready()
        made = {}
        for name, outcome in (
            ("in", revision(b"in")),
            ("span", revision(b"span", start=DAY - timedelta(days=1), end=DAY + timedelta(days=1))),
            ("starts", revision(b"starts", start=DAY, end=DAY + timedelta(days=2))),
            ("ends", revision(b"ends", start=DAY - timedelta(days=2), end=DAY)),
            ("instant", revision(b"instant", instant=True)),
            ("before", revision(b"before", start=DAY - timedelta(days=2), end=DAY - timedelta(days=1))),
            ("after", revision(b"after", start=DAY + timedelta(days=1))),
            ("instant-other", revision(b"instant-other", start=DAY + timedelta(days=1), instant=True)),
            ("unmodeled", unmodeled()),
            ("pending", pending()),
            ("quarantine", quarantined()),
        ):
            made[name] = self.process(store, self.project(store, f"contributor-{name}"), outcome)
        made["weight"] = self.process(store, self.project(store, "contributor-weight", "weight"), revision(b"w", pv=WPV))
        # Another record type under the steps version: only the type check refuses it.
        made["other-type"] = self.process(store, self.project(store, "contributor-other-type", "weight"), revision(b"o"))
        record = (made["in"].record_id, made["in"].selection_id)
        made["other-version"] = self.process(store, record, revision(b"in-v2", pv=PV2))
        return store, made

    def test_contributors_are_linked_to_outcomes(self) -> None:
        store, made = self.build()
        generation = self.generation(store)
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        valid = tuple(sorted(made[name].outcome_id for name in ("in", "span", "starts", "ends", "instant")))
        before = contents(self.path)
        stored = self.summarize(store, key, target, self.entry(store, key, contributors=valid))
        after = contents(self.path)
        self.assertEqual(stored.contributors, valid)
        # One link row per contributor, whatever its layout: some table added by migration 5 gains exactly one row
        # per contributor, each carrying that outcome identity and the summary's identity. Its index is a residual.
        linked = False
        for table in tables_created_by(5) - tables_created_by(4):
            rows = set(after["tables"][table]) - set(before["tables"][table])  # type: ignore[index]
            if len(rows) != len(valid):
                continue
            for column in range(len(next(iter(rows)))):
                values = {row[column] for row in rows}
                if values == set(valid) and all(stored.summary_id in row[:column] + row[column + 1:] for row in rows):
                    linked = True
        self.assertTrue(linked, "no contributor link rows were written")
        missing = self.summarize(store, self.key(30), target, self.entry(store, self.key(30), contributors=(), state="missing"))
        self.assertEqual(missing.contributors, ())
        self.assertEqual([item.contributors for item in self.view(store, "summaries", key)], [valid])

    def test_contributor_checks(self) -> None:
        store, made = self.build()
        generation = self.generation(store)
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        lease = self.leased(store, key, target)
        a, b = made["in"].outcome_id, made["span"].outcome_id
        highest = max(item.outcome_id for item in made.values())
        refused = [
            (b, a), (a, a), (0,), (-1,), ("1",), (1.0,), (None,), (highest + 1,), (a, highest + 100),
            *[(made[name].outcome_id,) for name in (
                "unmodeled", "pending", "quarantine", "weight", "other-type", "other-version", "before", "after",
                "instant-other")],
            (a, made["after"].outcome_id),
        ]
        for contributors in refused:
            with self.subTest(contributors=contributors):
                self.assertRefused("summary-contributor-invalid", store.complete_summary_refresh, lease.job_id,
                                   lease.fence, self.entry(store, key, contributors=contributors), self.tick())
        # Under the other version's generation, that revision is the valid one.
        stored = store.complete_summary_refresh(lease.job_id, lease.fence, self.entry(store, key, contributors=(a, b)),
                                                self.tick()).summary
        self.assertEqual(stored.contributors, (a, b))  # type: ignore[union-attr]
        other = SummaryTarget(self.generation(store, {"steps": PV2}).generation_id, 0)
        lease = self.leased(store, key, other)
        self.assertRefused("summary-contributor-invalid", store.complete_summary_refresh, lease.job_id, lease.fence,
                           self.entry(store, key, pv=PV2, contributors=(a,)), self.tick())
        stored = store.complete_summary_refresh(lease.job_id, lease.fence, self.entry(
            store, key, pv=PV2, contributors=(made["other-version"].outcome_id,)), self.tick()).summary
        self.assertEqual(stored.contributors, (made["other-version"].outcome_id,))  # type: ignore[union-attr]


class R63FailureRetryAndNewerEvidence(StoreCase):
    def test_transient_refresh_failure_requeues_after_backoff(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        self.request(store, (key,), target, 3, at(200))
        lease = self.claim(store, OWNER, LEASE, at(201))
        job = store.fail_summary_refresh(lease.job_id, lease.fence, "transient", "synthetic-busy",
                                         at(210).astimezone(IST), at(202))
        self.assertIsInstance(job, SummaryRefreshJob)
        self.assertEqual(job, self.job(store, lease.job_id))
        self.assertEqual((job.state, job.attempts, job.not_before, job.lease_owner, job.lease_expires_at, job.failure,
                          job.closed_at, job.fence), ("queued", 1, at(210), None, None, None, None, 1))
        self.assertEqual(job.history, (RefreshAttempt(1, OWNER, 1, at(201), at(201) + timedelta(days=1), at(202),
                                                      "transient-failure", "synthetic-busy"),))
        self.assertIsNone(store.claim_summary_refresh(OWNER, LEASE, at(210) - MICRO))
        again = self.claim(store, OWNER, LEASE, at(210))
        self.assertEqual((again.job_id, again.fence), (lease.job_id, 2))
        self.assertEqual(self.job(store, lease.job_id).attempts, 2)
        # A retry at the failure's own time is allowed.
        job = store.fail_summary_refresh(again.job_id, again.fence, "transient", "synthetic-busy", at(211), at(211))
        self.assertEqual((job.state, job.not_before), ("queued", at(211)))

    def test_refresh_retries_are_bounded(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        self.request(store, (key,), target, 2, at(200))
        lease = self.claim(store, OWNER, LEASE, at(201))
        store.fail_summary_refresh(lease.job_id, lease.fence, "transient", "synthetic-busy", at(202), at(201.5))
        lease = self.claim(store, OWNER, LEASE, at(202))
        job = store.fail_summary_refresh(lease.job_id, lease.fence, "transient", "synthetic-busy", at(203), at(202.5))
        self.assertEqual((job.state, job.failure, job.closed_at, job.attempts, job.lease_owner),
                         ("failed", "retries-exhausted", at(202.5), 2, None))
        self.assertEqual([attempt.result for attempt in job.history], ["transient-failure", "transient-failure"])
        self.assertIsNone(store.claim_summary_refresh(OWNER, LEASE, at(203)))
        self.assertIsNone(store.claim_summary_refresh(OWNER, LEASE, at(100_000)))
        self.assertEqual(self.job(store, lease.job_id), job)

    def test_deterministic_refresh_failure_is_terminal(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        self.request(store, (key,), target, 3, at(200))
        lease = self.claim(store, OWNER, LEASE, at(201))
        job = store.fail_summary_refresh(lease.job_id, lease.fence, "deterministic", "synthetic-defect", None, at(202))
        self.assertEqual((job.state, job.failure, job.closed_at, job.attempts, job.lease_owner, job.lease_expires_at),
                         ("failed", "synthetic-defect", at(202), 1, None, None))
        self.assertEqual(job.history[-1], RefreshAttempt(1, OWNER, 1, at(201), at(201) + timedelta(days=1), at(202),
                                                         "deterministic-failure", "synthetic-defect"))
        self.assertIsNone(store.claim_summary_refresh(OWNER, LEASE, at(100_000)))
        self.assertEqual(self.view(store, "refresh_counts", at(203)).jobs_failed, 1)
        self.assertRefused("lease-not-held", store.fail_summary_refresh, lease.job_id, lease.fence, "deterministic",
                           "synthetic-defect", None, at(203))

    def test_stale_failure_runs_again_for_newer_evidence(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        # Transient, with the retry budget used up: re-queued at retry_at with attempts reset.
        key = self.key(0)
        self.request(store, (key,), target, 1, at(200))
        lease = self.claim(store, OWNER, LEASE, at(201))
        self.request(store, (key,), target, 1, at(202))
        job = store.fail_summary_refresh(lease.job_id, lease.fence, "transient", "synthetic-busy", at(210), at(203))
        self.assertEqual((job.state, job.attempts, job.not_before, job.failure, job.closed_at, job.lease_owner),
                         ("queued", 0, at(210), None, None, None))
        self.assertEqual(job.history[-1].result, "transient-failure")
        again = self.claim(store, OWNER, LEASE, at(210))
        self.assertEqual((again.job_id, again.token), (lease.job_id, 2))
        store.complete_summary_refresh(again.job_id, again.fence, self.entry(store, key), at(211))
        # Transient with attempts left: attempts are reset too.
        key = self.key(1)
        self.request(store, (key,), target, 3, at(220))
        lease = self.claim(store, OWNER, LEASE, at(221))
        self.request(store, (key,), target, 3, at(222))
        job = store.fail_summary_refresh(lease.job_id, lease.fence, "transient", "synthetic-busy", at(230), at(223))
        self.assertEqual((job.state, job.attempts, job.not_before), ("queued", 0, at(230)))
        store.complete_summary_refresh(*(lambda l: (l.job_id, l.fence))(self.claim(store, when=at(230))),
                                       self.entry(store, key), at(231))
        # Deterministic: re-queued at the failure's own time, with its reason on the attempt.
        key = self.key(2)
        self.request(store, (key,), target, 3, at(240))
        lease = self.claim(store, OWNER, LEASE, at(241))
        self.request(store, (key,), target, 3, at(242))
        job = store.fail_summary_refresh(lease.job_id, lease.fence, "deterministic", "synthetic-defect", None, at(243))
        self.assertEqual((job.state, job.attempts, job.not_before, job.failure, job.closed_at),
                         ("queued", 0, at(243), None, None))
        self.assertEqual(job.history[-1], RefreshAttempt(1, OWNER, 1, at(241), at(241) + timedelta(days=1), at(243),
                                                         "deterministic-failure", "synthetic-defect"))
        again = self.claim(store, OWNER, LEASE, at(243))
        self.assertEqual((again.job_id, again.token, again.fence), (lease.job_id, 2, 2))
        # A request before the claim is not newer evidence: the failure is terminal.
        store.fail_summary_refresh(again.job_id, again.fence, "deterministic", "synthetic-defect", None, at(244))
        self.assertEqual(self.job(store, lease.job_id).state, "failed")
        self.assertEqual(self.view(store, "refresh_counts", at(245)).jobs_failed, 1)

    def test_refresh_failure_refusals(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        self.request(store, (key,), target, 3, at(200))
        lease = self.claim(store, OWNER, LEASE, at(201))
        job_id, fence = lease.job_id, lease.fence
        refusals = [
            ("summary-refresh-job-unknown", (424242, 1, "deterministic", "synthetic-defect", None, at(202))),
            ("lease-not-held", (job_id, 2, "deterministic", "synthetic-defect", None, at(202))),
            *[("summary-request-invalid", (job_id, fence, kind, "synthetic-defect", None, at(202))) for kind in (
                "fatal", "Transient", "", None)],
            *[("summary-request-invalid", (job_id, fence, "deterministic", reason, None, at(202))) for reason in (
                "Synthetic-defect", "synthetic defect", "a" * 65, "", None, 5)],
            ("summary-request-invalid", (job_id, fence, "transient", "synthetic-busy", None, at(202))),
            ("summary-request-invalid", (job_id, fence, "transient", "synthetic-busy", at(202) - MICRO, at(202))),
            ("summary-request-invalid", (job_id, fence, "deterministic", "synthetic-defect", at(203), at(202))),
            ("timestamp-invalid", (job_id, fence, "deterministic", "synthetic-defect", None, NAIVE)),
            ("timestamp-invalid", (job_id, fence, "transient", "synthetic-busy", NAIVE, at(202))),
        ]
        for failure, args in refusals:
            with self.subTest(failure=failure, args=args):
                self.assertRefused(failure, store.fail_summary_refresh, *args)
        self.free = 0
        self.assertRetryable("storage-reserve-exhausted", store.fail_summary_refresh, job_id, fence, "deterministic",
                             "synthetic-defect", None, at(202))
        self.free = PLENTY
        job = store.fail_summary_refresh(job_id, fence, "deterministic", "a" * 64, None, at(202))
        self.assertEqual(job.failure, "a" * 64)
        self.assertRefused("lease-not-held", store.fail_summary_refresh, job_id, fence, "deterministic",
                           "synthetic-defect", None, at(203))


class R64PolicySummaryWork(StoreCase):
    def test_policy_link_is_recorded_with_its_request(self) -> None:
        store, generation, _ = self.seeded()
        policy = self.ftz(store).policy_id
        self.assertEqual(self.view(store, "policies_awaiting_refresh"), (policy,))
        target = SummaryTarget(generation.generation_id, policy)
        key = self.key()
        # A refused request records no link.
        self.assertRefused("summary-target-invalid", self.request, store, (key, self.key(0, "sleep-session")), target,
                           policy_id=policy)
        self.assertEqual(self.view(store, "policies_awaiting_refresh"), (policy,))
        self.assertEqual(self.request(store, (key,), target, policy_id=policy), RefreshRequest(1, 0, 0))
        self.assertEqual(self.view(store, "policies_awaiting_refresh"), ())
        # A policy may be linked again, also by a request with no keys.
        self.assertEqual(self.request(store, (), target, policy_id=policy), RefreshRequest(0, 0, 0))
        self.assertEqual(self.request(store, (key,), target, policy_id=policy), RefreshRequest(0, 1, 0))
        self.assertEqual(self.view(store, "policies_awaiting_refresh"), ())
        self.assertEqual(self.claim(store).token, 2)

    def test_policies_awaiting_refresh_lists_live_unlinked_policies(self) -> None:
        store, generation, _ = self.seeded()
        f1 = self.ftz(store).policy_id
        s1 = self.ssp(store, (OA,)).policy_id
        s2 = self.ssp(store, (OA,), supersedes=(s1,)).policy_id
        f2 = self.ftz(store, "Europe/London", datetime(2026, 6, 1, tzinfo=UTC)).policy_id
        with store.read() as view:
            self.assertEqual(view.policies_awaiting_refresh(), (f1, s2, f2))
            self.assertEqual(view.refresh_counts(self.tick()).policies_awaiting_refresh, 3)
        target = SummaryTarget(generation.generation_id, f2)
        self.request(store, (), target, policy_id=s2)
        self.assertEqual(self.view(store, "policies_awaiting_refresh"), (f1, f2))
        # Linking a superseded policy changes nothing listed; superseding a policy removes it.
        self.request(store, (), target, policy_id=s1)
        self.assertEqual(self.view(store, "policies_awaiting_refresh"), (f1, f2))
        f3 = self.ftz(store, "UTC", None, (f1,)).policy_id
        self.assertEqual(self.view(store, "policies_awaiting_refresh"), (f2, f3))
        s3 = self.ssp(store, (OA,), supersedes=(s2,)).policy_id
        self.assertEqual(self.view(store, "policies_awaiting_refresh"), (f2, f3, s3))
        self.request(store, (self.key(),), SummaryTarget(generation.generation_id, s3), policy_id=f3)
        self.assertEqual(self.view(store, "policies_awaiting_refresh"), (f2, s3))

    def test_policy_target_must_include_the_policy(self) -> None:
        store, generation, _ = self.seeded()
        f1 = self.ftz(store).policy_id
        f2 = self.ftz(store, "Europe/London", datetime(2026, 6, 1, tzinfo=UTC)).policy_id
        f3 = self.ftz(store, "UTC", datetime(2026, 7, 1, tzinfo=UTC)).policy_id
        key = self.key()
        for horizon in (0, f1):
            with self.subTest(horizon=horizon):
                self.assertRefused("summary-target-invalid", self.request, store, (key,),
                                   SummaryTarget(generation.generation_id, horizon), policy_id=f2)
                self.assertRefused("summary-target-invalid", self.request, store, (),
                                   SummaryTarget(generation.generation_id, horizon), policy_id=f2)
        self.assertEqual(self.view(store, "policies_awaiting_refresh"), (f1, f2, f3))
        self.request(store, (key,), SummaryTarget(generation.generation_id, f2), policy_id=f2)
        self.request(store, (key,), SummaryTarget(generation.generation_id, f3), policy_id=f1)
        self.assertEqual(self.view(store, "policies_awaiting_refresh"), (f3,))


class R65EvidenceChangesAndTheBookmark(StoreCase):
    def build(self):
        """Outcomes, selections of every kind, a failed job, and open and completed jobs that are not changes."""
        store = self.ready()
        steps = self.project(store, "changes-steps")
        weight = self.project(store, "changes-weight", "weight")
        outcomes = [self.process(store, steps, revision(b"s")), self.process(store, weight, revision(b"w", pv=WPV))]
        self.process(store, steps, revision(b"s2", pv=PV2, policy=INFERRED))
        outcomes.append(self.view(store, "processing_outcomes", steps[0])[-1])
        failing = self.project(store, "changes-failing")
        failed = self.fail_processing(store, failing)
        retracted = self.project(store, "changes-retracted")
        _, retraction = self.retract(store, "changes-retracted")
        conflicted = self.project(store, "changes-conflict")
        self.observe(store, "changes-conflict")
        conflict = self.select(store, conflicted[0], "conflict", count=2)
        open_record = self.project(store, "changes-open")
        when = self.tick()
        store.enqueue_processing(*open_record, PV, 3, when)
        self.assertIsNotNone(store.claim_processing_job(PROCESSOR, 86_400, when))  # open, and out of later claims' way
        selections = [
            (steps[1], steps[0], "steps"), (weight[1], weight[0], "weight"), (failing[1], failing[0], "steps"),
            (retracted[1], retracted[0], "steps"), (retraction, retracted[0], "steps"),
            (conflicted[1], conflicted[0], "steps"), (conflict, conflicted[0], "steps"),
            (open_record[1], open_record[0], "steps")]
        expected = (
            *(EvidenceChange("outcome", item.outcome_id, item.record_id,
                             "weight" if item.processor_version == WPV else "steps", item.processor_version)
              for item in outcomes),
            *(EvidenceChange("selection", selection, record_id, record_type, None)
              for selection, record_id, record_type in sorted(selections)),
            EvidenceChange("failed-job", failed, failing[0], "steps", PV),
        )
        return store, expected, failed

    def test_changes_list_outcomes_selections_and_failed_jobs(self) -> None:
        store, expected, _ = self.build()
        self.assertEqual([change.change_id for change in expected[:3]], sorted(change.change_id for change in expected[:3]))
        with store.read() as view:
            self.assertEqual(view.evidence_changes(100), EvidenceChanges(EvidenceBookmark(0, 0), expected))
            for limit in (1, 3, 4, 11, 12):
                with self.subTest(limit=limit):
                    self.assertEqual(view.evidence_changes(limit), EvidenceChanges(EvidenceBookmark(0, 0), expected[:limit]))
            self.assertEqual(view.refresh_counts(self.tick()).evidence_changes_pending, len(expected))

    def test_acknowledgement_moves_the_bookmark_with_the_request(self) -> None:
        store, expected, failed = self.build()
        generation = self.generation(store)
        target = SummaryTarget(generation.generation_id, 0)
        outcomes = [change.change_id for change in expected if change.kind == "outcome"]
        selections = [change.change_id for change in expected if change.kind == "selection"]
        # A partial acknowledgement leaves the later changes listed.
        acknowledgement = EvidenceAcknowledgement(outcomes[1], selections[2], ())
        self.assertEqual(self.request(store, (self.key(),), target, acknowledge=acknowledgement), RefreshRequest(1, 0, 0))
        remaining = tuple(change for change in expected if (change.kind == "outcome" and change.change_id > outcomes[1])
                          or (change.kind == "selection" and change.change_id > selections[2])
                          or change.kind == "failed-job")
        self.assertEqual(self.changes(store), EvidenceChanges(EvidenceBookmark(outcomes[1], selections[2]), remaining))
        # Equal through values are allowed; with no keys, only the acknowledgement is written.
        self.assertEqual(self.request(store, (), target, acknowledge=EvidenceAcknowledgement(
            outcomes[-1], selections[-1], (failed,))), RefreshRequest(0, 0, 0))
        self.assertEqual(self.changes(store), EvidenceChanges(EvidenceBookmark(outcomes[-1], selections[-1]), ()))
        self.request(store, (), target, acknowledge=EvidenceAcknowledgement(outcomes[-1], selections[-1], (failed,)))
        self.assertEqual(self.changes(store).bookmark, EvidenceBookmark(outcomes[-1], selections[-1]))
        # Later changes are listed after the bookmark.
        record = self.project(store, "changes-later")
        later = self.process(store, record, revision(b"later"))
        failed_later = self.fail_processing(store, self.project(store, "changes-later-failing"))
        with store.read() as view:
            changes = view.evidence_changes(100)
            self.assertEqual([(change.kind, change.change_id) for change in changes.changes], [
                ("outcome", later.outcome_id), ("selection", record[1]),
                ("selection", view.source_record(self.record_id(store, "changes-later-failing")).selections[-1].selection_id),
                ("failed-job", failed_later)])
            self.assertEqual(view.refresh_counts(self.tick()).evidence_changes_pending, 4)

    def test_acknowledgement_refusals(self) -> None:
        store, expected, failed = self.build()
        generation = self.generation(store)
        target = SummaryTarget(generation.generation_id, 0)
        outcomes = [change.change_id for change in expected if change.kind == "outcome"]
        selections = [change.change_id for change in expected if change.kind == "selection"]
        self.request(store, (), target, acknowledge=EvidenceAcknowledgement(outcomes[1], selections[1], ()))
        with store.read() as view:
            (open_job,) = [job for job in view.processing_jobs(self.record_id(store, "changes-open"))]
            completed = view.processing_jobs(self.record_id(store, "changes-steps"))[0]
        for acknowledgement in (
            EvidenceAcknowledgement(outcomes[1] - 1, selections[1], ()),
            EvidenceAcknowledgement(outcomes[1], selections[1] - 1, ()),
            EvidenceAcknowledgement(outcomes[-1] + 1, selections[-1], ()),
            EvidenceAcknowledgement(outcomes[-1], selections[-1] + 1, ()),
            EvidenceAcknowledgement(outcomes[-1], selections[-1], (open_job.job_id,)),
            EvidenceAcknowledgement(outcomes[-1], selections[-1], (completed.job_id,)),
            EvidenceAcknowledgement(outcomes[-1], selections[-1], (424242,)),
            EvidenceAcknowledgement(outcomes[-1], selections[-1], (failed, open_job.job_id)),
            EvidenceAcknowledgement(-1, selections[-1], ()),
            EvidenceAcknowledgement(outcomes[-1], "1", ()),  # type: ignore[arg-type]
            EvidenceAcknowledgement(None, selections[-1], ()),  # type: ignore[arg-type]
            EvidenceAcknowledgement(outcomes[-1], selections[-1], [failed]),  # type: ignore[arg-type]
            EvidenceAcknowledgement(outcomes[-1], selections[-1], ("1",)),  # type: ignore[arg-type]
            (outcomes[-1], selections[-1], ()),
        ):
            for keys in ((), (self.key(),)):
                with self.subTest(acknowledgement=acknowledgement, keys=keys):
                    self.assertRefused("summary-request-invalid", self.request, store, keys, target,
                                       acknowledge=acknowledgement)
        self.assertEqual(self.changes(store).bookmark, EvidenceBookmark(outcomes[1], selections[1]))
        self.assertEqual(self.jobs(store, self.key()), ())

    def test_unacknowledged_changes_survive_a_lost_request(self) -> None:
        store, expected, failed = self.build()
        generation = self.generation(store)
        target = SummaryTarget(generation.generation_id, 0)
        outcomes = [change.change_id for change in expected if change.kind == "outcome"]
        selections = [change.change_id for change in expected if change.kind == "selection"]
        acknowledgement = EvidenceAcknowledgement(outcomes[-1], selections[-1], (failed,))
        everything = EvidenceChanges(EvidenceBookmark(0, 0), expected)
        # A refused request moves nothing.
        self.assertRefused("summary-target-invalid", self.request, store, (self.key(0, "sleep-session"),), target,
                           acknowledge=acknowledgement)
        self.assertRefused("processor-generation-unknown", self.request, store, (self.key(),), SummaryTarget(424242, 0),
                           acknowledge=acknowledgement)
        self.free = 0
        self.assertRetryable("storage-reserve-exhausted", self.request, store, (self.key(),), target,
                             acknowledge=acknowledgement)
        self.assertRetryable("storage-reserve-exhausted", self.request, store, (), target, acknowledge=acknowledgement)
        self.free = PLENTY
        self.assertEqual(self.changes(store), everything)
        # A crash part-way through the request moves nothing either.
        faults = self.install_fault_triggers()
        faults.execute("UPDATE test_fault SET remaining = 1")
        with self.assertRaises(sqlite3.DatabaseError):
            self.request(store, (self.key(),), target, acknowledge=acknowledgement)
        faults.execute("UPDATE test_fault SET remaining = 1000000")
        self.assertEqual(self.changes(store), everything)
        self.assertEqual(self.jobs(store, self.key()), ())
        self.request(store, (self.key(),), target, acknowledge=acknowledgement)
        self.assertEqual(self.changes(store), EvidenceChanges(EvidenceBookmark(outcomes[-1], selections[-1]), ()))
        self.assertEqual(self.claim(store).token, 1)


class R66ProcessedPositions(StoreCase):
    def assertAt(self, store, sequence: int | None, installation: str = INSTALLATION, record_type: str = "steps",
                 pv: str = PV) -> None:
        found = [(item.installation_id, item.sequence) for item in self.positions(store, record_type, pv)]
        self.assertEqual(found, [] if sequence is None else [(installation, sequence)])

    def test_position_stops_before_the_first_unsettled_batch(self) -> None:
        store = self.ready()
        first = self.project(store, "p-1")
        self.assertAt(store, None)  # no settled batch yet
        self.process(store, first, revision(b"p-1"))
        self.assertAt(store, 1)
        self.archive_completion(store)
        self.assertAt(store, 2)  # other message kinds never hold it back
        third = self.project(store, "p-3")
        self.assertAt(store, 2)
        self.process(store, self.project(store, "p-4"), revision(b"p-4"))
        self.assertAt(store, 2)  # a settled batch behind an unsettled one does not count
        weight = self.project(store, "w-5", "weight")
        self.assertAt(store, 2)
        self.process(store, third, revision(b"p-3"))
        self.assertAt(store, 5)  # the unsettled weight batch does not hold steps back (Decision 10)
        self.assertAt(store, None, record_type="weight", pv=WPV)
        self.assertAt(store, None, pv=PV2)
        self.process(store, weight, revision(b"w-5", pv=WPV))
        self.assertAt(store, 5, record_type="weight", pv=WPV)
        self.switch_installation(store)
        b_record = self.project(store, "b-1")
        self.assertAt(store, 5)
        self.process(store, b_record, revision(b"b-1"))
        self.assertEqual([(item.installation_id, item.sequence) for item in self.positions(store)],
                         [(INSTALLATION_B, 1), (INSTALLATION, 5)])
        self.assertEqual(self.view(store, "processed_positions", "sleep-session", PV), ())

    def test_settled_items(self) -> None:
        store = self.ready()
        self.process(store, self.project(store, "base"), revision(b"base"))
        self.assertAt(store, 1)
        # Quarantined.
        sequence = self.archive(store, BatchItem(projection=b"synthetic-unreadable"))
        self.assertAt(store, sequence - 1)
        store.quarantine_item(self.position(sequence), "projection-unreadable", "", self.tick())
        self.assertAt(store, sequence)
        # Unindexed, indexed but unselected, and selected but unprocessed are unsettled.
        sequence = self.archive(store, BatchItem(projection=b'{"synthetic-projection":"staged"}'))
        self.assertAt(store, sequence - 1)
        store.index_items((ProjectionEntry(self.position(sequence), HC, ORIGIN_A, "staged", self.tick()),), self.tick())
        self.assertAt(store, sequence - 1)
        staged = (self.record_id(store, "staged"), self.select(store, self.record_id(store, "staged")))
        self.assertAt(store, sequence - 1)
        # An outcome under another version does not settle it; one under the version does.
        self.process(store, staged, revision(b"staged", pv=PV2))
        self.assertAt(store, sequence - 1)
        self.process(store, staged, pending())
        self.assertAt(store, sequence)
        # A failed job under another version does not; the last job under the version failing does.
        failing = self.project(store, "failing")
        sequence = self.sequences[INSTALLATION]
        self.fail_processing(store, failing, PV2)
        self.assertAt(store, sequence - 1)
        self.fail_processing(store, failing, PV)
        self.assertAt(store, sequence)
        # A retraction selection settles the record's projection and the retraction item.
        self.project(store, "retracted")
        sequence = self.sequences[INSTALLATION]
        self.assertAt(store, sequence - 1)
        retraction, _ = self.retract(store, "retracted")
        self.assertAt(store, retraction)
        # A Source Conflict selection recorded from the newest evidence settles both items.
        conflicted = self.project(store, "conflicted")
        sequence = self.sequences[INSTALLATION]
        newer = self.observe(store, "conflicted")
        self.assertAt(store, sequence - 1)
        self.select(store, conflicted[0], "conflict", count=2)
        self.assertAt(store, newer)
        # An indexed retraction that names no Source Record is settled once indexed.
        sequence = self.archive(store, BatchItem(retracted_identity="never-seen"), change=True)
        self.assertAt(store, sequence - 1)
        store.index_items((RetractionEntry(self.position(sequence)),), self.tick())
        self.assertAt(store, sequence)
        # Newer evidence needs a selection recorded from it or later: an older processed selection is not enough.
        renewed = self.project(store, "renewed")
        self.process(store, renewed, revision(b"renewed"))
        settled = self.sequences[INSTALLATION]
        self.assertAt(store, settled)
        newer = self.observe(store, "renewed")
        self.assertAt(store, settled)
        selection = self.select(store, renewed[0])
        self.assertAt(store, settled)
        self.process(store, (renewed[0], selection), revision(b"renewed-2"))
        self.assertAt(store, newer)
        # A batch is settled only when all its items are.
        sequence = self.archive(store, BatchItem(projection=b"synthetic-unreadable-2"),
                                BatchItem(projection=b'{"synthetic-projection":"pair"}'))
        store.quarantine_item(self.position(sequence, 0), "projection-unreadable", "", self.tick())
        self.assertAt(store, sequence - 1)
        store.index_items((ProjectionEntry(self.position(sequence, 1), HC, ORIGIN_A, "pair", self.tick()),), self.tick())
        pair = (self.record_id(store, "pair"), self.select(store, self.record_id(store, "pair")))
        self.assertAt(store, sequence - 1)
        self.process(store, pair, unmodeled())
        self.assertAt(store, sequence)

    def test_position_times(self) -> None:
        store = self.ready()
        first = self.project(store, "t-1")
        weight = self.archive(store, BatchItem(projection=b'{"synthetic-projection":"weight-later"}'), record_type="weight")
        processed = self.process(store, first, revision(b"t-1"))

        def expect(sequence: int, processed_at: datetime) -> None:
            self.assertEqual(self.positions(store), (ProcessedPosition(
                INSTALLATION, sequence, self.receipts[(INSTALLATION, sequence)], processed_at),))

        # Settled after the later receipt: the settlement time.
        expect(weight, processed.recorded_at)
        # A later receipt after every settlement: the receipt time.
        completion = self.archive_completion(store)
        expect(completion, self.receipts[(INSTALLATION, completion)])
        # The earliest witness counts, not a later outcome for the same selection.
        self.process(store, first, revision(b"t-1-again", policy=INFERRED))
        expect(completion, self.receipts[(INSTALLATION, completion)])
        # A quarantine's time.
        sequence = self.archive(store, BatchItem(projection=b"synthetic-unreadable"))
        quarantine = store.quarantine_item(self.position(sequence), "projection-unreadable", "", self.tick())
        expect(sequence, quarantine.recorded_at)
        # A retraction selection's time, for the projection and for the retraction item.
        self.project(store, "t-retracted")
        sequence, selection = self.retract(store, "t-retracted")
        recorded = self.view(store, "source_record", self.record_id(store, "t-retracted")).selections[-1].recorded_at
        expect(sequence, recorded)
        # A failed job's close.
        failing = self.project(store, "t-failing")
        failed = self.fail_processing(store, failing)
        expect(self.sequences[INSTALLATION], self.job_closed(store, failed))
        # An unnamed retraction's indexing time.
        sequence = self.archive(store, BatchItem(retracted_identity="t-never-seen"), change=True)
        indexed = self.tick()
        store.index_items((RetractionEntry(self.position(sequence)),), indexed)
        expect(sequence, indexed)
        # Another type's later settlement never counts.
        weight_record = self.project(store, "t-weight", "weight")
        sequence = self.sequences[INSTALLATION]
        late = self.process(store, weight_record, revision(b"t-weight", pv=WPV))
        expect(sequence, self.receipts[(INSTALLATION, sequence)])
        self.assertGreater(late.recorded_at, self.receipts[(INSTALLATION, sequence)])
        self.assertEqual(selection, self.view(store, "source_record", self.record_id(store, "t-retracted")).selections[-1].selection_id)

    def job_closed(self, store, job_id: int) -> datetime:
        with store.read() as view:
            job = view.processing_job(job_id)
        assert job is not None and job.closed_at is not None
        return job.closed_at

    def test_positions_never_move_back(self) -> None:
        store = self.ready()
        history: list[tuple[ProcessedPosition, ...]] = []

        def step() -> None:
            current = self.positions(store)
            if history:
                previous = {item.installation_id: item for item in history[-1]}
                for item in current:
                    if item.installation_id in previous:
                        before = previous[item.installation_id]
                        self.assertGreaterEqual(item.sequence, before.sequence, current)
                        if item.sequence == before.sequence:
                            # processed_at can only fall, and only when a later write supplies an earlier
                            # timestamp. This test's clock only moves forward, so the position is unchanged.
                            self.assertLessEqual(item.processed_at, before.processed_at)
                            self.assertEqual(item, before)
                self.assertLessEqual(set(previous), {item.installation_id for item in current})
            history.append(current)

        one = self.project(store, "m-1")
        self.process(store, one, revision(b"m-1"))
        step()
        two = self.project(store, "m-2")
        step()
        self.observe(store, "m-1")  # newer evidence for a processed record
        selection = self.select(store, one[0])
        step()
        self.process(store, two, revision(b"m-2"))
        step()
        self.project(store, "m-3", "weight")
        step()
        self.retract(store, "m-2")
        step()
        self.process(store, (one[0], selection), revision(b"m-1-again"))
        step()
        self.process(store, (one[0], selection), revision(b"m-1-v2", pv=PV2))
        self.observe(store, "m-1")
        self.select(store, one[0], "conflict", count=2)
        step()
        self.switch_installation(store)
        self.process(store, self.project(store, "m-b"), revision(b"m-b"))
        step()
        self.project(store, "m-b-2")
        step()
        self.assertEqual([item.sequence for item in history[-1]], [1, self.sequences[INSTALLATION]])
        self.assertGreater(len({item for state in history for item in state}), 3)

    def reopen(self, store):
        store.close()
        return self.open()

    def summarize_at(self, store, watermark: tuple[ProcessedPosition, ...]) -> StoredSummary:
        """Complete a refresh whose watermark is exactly the given positions (R61)."""
        generation = self.view(store, "processor_generations") or (self.generation(store),)
        target = SummaryTarget(generation[0].generation_id, 0)
        stored = self.summarize(store, self.key(), target, self.entry(store, self.key(), watermark=watermark))
        self.assertEqual(stored.watermark, watermark)
        return stored

    def test_a_failure_witness_survives_a_retry(self) -> None:
        store = self.ready()
        record = self.project(store, "retried")
        failed = self.fail_processing(store, record)
        settled = (ProcessedPosition(INSTALLATION, 1, self.receipts[(INSTALLATION, 1)], self.job_closed(store, failed)),)
        self.assertEqual(self.positions(store), settled)
        # A manual retry makes a newer job the last one; the earlier failure still witnesses the item.
        store.enqueue_processing(*record, PV, 3, self.tick())
        self.assertEqual(self.positions(store), settled)
        store = self.reopen(store)
        self.assertEqual(self.positions(store), settled)
        self.summarize_at(store, settled)
        # The retry fails again, and a third run succeeds: later witnesses, so the earliest still sets the time.
        lease = store.claim_processing_job(PROCESSOR, 3600, self.tick())
        assert lease is not None
        self.assertEqual(lease.record_id, record[0])
        store.fail_processing_job(lease.job_id, lease.fence, "deterministic", "synthetic-defect", None, self.tick())
        self.assertEqual(self.positions(store), settled)
        self.process(store, record, revision(b"retried"))
        self.assertEqual(self.positions(store), settled)
        store = self.reopen(store)
        self.summarize_at(store, settled)

    def test_retraction_matches_records_as_of_its_indexing(self) -> None:
        store = self.ready()
        sequence = self.archive(store, BatchItem(retracted_identity="late-identity"), change=True)
        indexed = self.tick()
        store.index_items((RetractionEntry(self.position(sequence)),), indexed)
        settled = (ProcessedPosition(INSTALLATION, sequence, self.receipts[(INSTALLATION, sequence)], indexed),)
        self.assertEqual(self.positions(store), settled)
        # A projection of that identity indexed later is a new obligation at its own position.
        later = self.observe(store, "late-identity")
        record_id = self.record_id(store, "late-identity")
        self.assertEqual(self.positions(store), settled)
        store = self.reopen(store)
        self.assertEqual(self.positions(store), settled)
        self.summarize_at(store, settled)
        self.process(store, (record_id, self.select(store, record_id)), revision(b"late"))
        (moved,) = self.positions(store)
        self.assertEqual(moved.sequence, later)
        store = self.reopen(store)
        self.summarize_at(store, (moved,))

    def test_retraction_needs_every_matching_record(self) -> None:
        store = self.ready()
        first = self.project(store, "shared", origin=ORIGIN_A)
        self.process(store, first, revision(b"a"))
        second = self.project(store, "shared", origin=ORIGIN_B)
        self.process(store, second, revision(b"b"))
        self.assertEqual([item.sequence for item in self.positions(store)], [2])
        sequence = self.archive(store, BatchItem(retracted_identity="shared"), change=True)
        store.index_items((RetractionEntry(self.position(sequence)),), self.tick())
        # The retraction matches both records; one reconciled record is not enough.
        self.select(store, first[0], "retraction")
        self.assertEqual([item.sequence for item in self.positions(store)], [2])
        self.select(store, second[0], "retraction")
        recorded = self.view(store, "source_record", second[0]).selections[-1].recorded_at
        settled = (ProcessedPosition(INSTALLATION, sequence, self.receipts[(INSTALLATION, sequence)], recorded),)
        self.assertEqual(self.positions(store), settled)
        # A record of that identity first observed after the retraction does not reopen it.
        self.observe(store, "shared", origin="org.example.synthetic.third")
        self.assertEqual(self.positions(store), settled)
        self.summarize_at(store, settled)

    def test_settlement_time_is_the_earliest_witness(self) -> None:
        store = self.ready()
        record = self.project(store, "ambiguous-clock")
        self.observe(store, "ambiguous-clock")
        when = self.tick()
        store.enqueue_processing(*record, PV, 3, when)
        lease = store.claim_processing_job(PROCESSOR, 86_400, when)
        assert lease is not None
        found = self.view(store, "source_record", record[0])
        evidence = tuple(item.evidence_id for item in found.evidence if item.kind == "projection")
        store.close()
        early, late = at(200), at(300)
        states, positions = [], []
        # The same prestate, then a failure at a late time and a covering Source Conflict at an early one, in
        # either order: every durable row ends identical, so only the earliest witness time can be the answer.
        for name, order in (("fail-first", "fc"), ("conflict-first", "cf")):
            path = self.path.with_name(f"{name}.sqlite3")
            source, copy = sqlite3.connect(self.path), sqlite3.connect(path)
            try:
                source.backup(copy)
            finally:
                source.close()
                copy.close()
            handle = open_store(path, reserve_bytes=RESERVE, free_bytes=lambda _path: PLENTY)
            try:
                for step in order:
                    if step == "f":
                        handle.fail_processing_job(lease.job_id, lease.fence, "deterministic", "synthetic-defect",
                                                   None, late)
                    else:
                        handle.record_selection(record[0], "conflict", evidence, found.evidence_head, early)
                with handle.read() as view:
                    positions.append(view.processed_positions("steps", PV))
            finally:
                handle.close()
            states.append(contents(path))
        self.assertEqual(states[0], states[1])
        self.assertEqual(positions[0], positions[1])
        self.assertEqual(positions[0], (ProcessedPosition(INSTALLATION, 2, self.receipts[(INSTALLATION, 2)], early),))


class R67SummaryEvidence(StoreCase):
    def make(self, store, name: str, *outcomes: OutcomeEntry, record_type: str = "steps", origin: str = ORIGIN_A):
        record = self.project(store, f"{IDENTITY_MARKER}-{name}", record_type, origin)
        made = [self.process(store, record, outcome) for outcome in outcomes]
        return record, made

    def evidence(self, store, target: SummaryTarget, first: date = DAY - timedelta(days=1),
                 last: date = DAY + timedelta(days=1), after: int = 0, limit: int = 100, measure: str = MEASURE,
                 record_type: str = "steps") -> SummaryEvidence:
        with store.read() as view:
            return view.summary_evidence(measure, record_type, first, last, target, after, limit)

    def test_evidence_lists_records_placed_in_the_range(self) -> None:
        store = self.ready()
        d = lambda offset: DAY + timedelta(days=offset)  # noqa: E731
        a, (oa,) = self.make(store, "a", revision(b"a"))
        self.make(store, "b", revision(b"b", start=d(4)))
        c, (oc,) = self.make(store, "c", revision(b"c", start=d(-3), end=d(-1)))
        dd, (od,) = self.make(store, "d", revision(b"d", start=d(1), instant=True), origin=ORIGIN_B)
        self.make(store, "e", revision(b"e", start=d(2), end=d(3)))
        f, _ = self.make(store, "f", revision(b"f"))
        self.retract(store, f"{IDENTITY_MARKER}-f")
        g, (og,) = self.make(store, "g", revision(b"g"))
        self.observe(store, f"{IDENTITY_MARKER}-g")
        conflict = self.select(store, g[0], "conflict", count=2)
        self.make(store, "h", revision(b"h", pv=WPV), record_type="weight")
        self.make(store, "i", revision(b"i", pv=PV2))
        self.make(store, "j", pending())
        self.make(store, "k", revision(b"k"), revision(b"k-later", policy=INFERRED, start=d(4)))
        l, (_, ol) = self.make(store, "l", revision(b"l", start=d(4)), revision(b"l-later", policy=INFERRED))
        m, (om,) = self.make(store, "m", revision(b"m", start=d(-10), end=d(10)))
        n, (on,) = self.make(store, "n", revision(b"n"))
        self.observe(store, f"{IDENTITY_MARKER}-n")
        n_selection = self.select(store, n[0])
        generation = self.generation(store)
        target = SummaryTarget(generation.generation_id, 0)
        evidence = self.evidence(store, target)
        self.assertIsInstance(evidence, SummaryEvidence)
        self.assertEqual(evidence, SummaryEvidence((
            SummaryEvidenceItem(a[0], HC, ORIGIN_A, a[1], "projection", oa, False, oa),
            SummaryEvidenceItem(c[0], HC, ORIGIN_A, c[1], "projection", oc, False, oc),
            SummaryEvidenceItem(dd[0], HC, ORIGIN_B, dd[1], "projection", od, False, od),
            SummaryEvidenceItem(g[0], HC, ORIGIN_A, conflict, "conflict", None, False, og),
            SummaryEvidenceItem(l[0], HC, ORIGIN_A, l[1], "projection", ol, False, ol),
            SummaryEvidenceItem(m[0], HC, ORIGIN_A, m[1], "projection", om, False, om),
            SummaryEvidenceItem(n[0], HC, ORIGIN_A, n_selection, "projection", None, False, on),
        ), (), None))
        self.assertEqual([item.name for item in dataclasses.fields(SummaryEvidenceItem)], [
            "record_id", "source_system_kind", "origin", "selection_id", "selection_kind", "outcome", "job_failed",
            "last_revision"])
        self.assertNotIn(IDENTITY_MARKER, repr(evidence))
        self.assertNotIn(HEALTH_MARKER.decode(), repr(evidence))
        # A single-day range, and a weight range under the generation's weight version.
        self.assertEqual([item.record_id for item in self.evidence(store, target, d(1), d(1)).items], [dd[0], m[0]])
        self.assertEqual(len(self.evidence(store, target, record_type="weight").items), 1)
        self.assertEqual(len(self.evidence(store, SummaryTarget(self.generation(store, {"steps": PV2}).generation_id, 0)).items), 1)

    def test_evidence_reports_each_record_state(self) -> None:
        store = self.ready()
        one, (r1,) = self.make(store, "1", revision(b"1"))
        two, (r2,) = self.make(store, "2", revision(b"2"))
        self.observe(store, f"{IDENTITY_MARKER}-2")
        two_latest = (two[0], self.select(store, two[0]))
        self.fail_processing(store, two_latest)
        three, (r3,) = self.make(store, "3", revision(b"3"))
        self.observe(store, f"{IDENTITY_MARKER}-3")
        three_latest = (three[0], self.select(store, three[0]))
        p3 = self.process(store, three_latest, pending())
        four, (r4,) = self.make(store, "4", revision(b"4"))
        self.observe(store, f"{IDENTITY_MARKER}-4")
        four_latest = (four[0], self.select(store, four[0]))
        self.fail_processing(store, four_latest, PV2)
        six, (r6, u6) = self.make(store, "6", revision(b"6"), unmodeled(policy=INFERRED))
        seven, (r7, _) = self.make(store, "7", revision(b"7"), revision(b"7-v2", pv=PV2))
        five, (r5,) = self.make(store, "5", revision(b"5"))
        self.observe(store, f"{IDENTITY_MARKER}-5")
        five_latest = (five[0], self.select(store, five[0]))
        self.fail_processing(store, five_latest)
        store.enqueue_processing(*five_latest, PV, 3, self.tick())  # a newer job after the failure
        generation = self.generation(store)
        items = {item.record_id: item for item in self.evidence(store, SummaryTarget(generation.generation_id, 0)).items}
        self.assertEqual(items[one[0]], SummaryEvidenceItem(one[0], HC, ORIGIN_A, one[1], "projection", r1, False, r1))
        self.assertEqual(items[two[0]], SummaryEvidenceItem(two[0], HC, ORIGIN_A, two_latest[1], "projection", None, True, r2))
        self.assertEqual(items[three[0]], SummaryEvidenceItem(three[0], HC, ORIGIN_A, three_latest[1], "projection", p3,
                                                              False, r3))
        self.assertEqual(items[four[0]], SummaryEvidenceItem(four[0], HC, ORIGIN_A, four_latest[1], "projection", None,
                                                             False, r4))
        self.assertEqual(items[six[0]], SummaryEvidenceItem(six[0], HC, ORIGIN_A, six[1], "projection", u6, False, r6))
        self.assertEqual(items[seven[0]], SummaryEvidenceItem(seven[0], HC, ORIGIN_A, seven[1], "projection", r7, False, r7))
        self.assertEqual(items[five[0]], SummaryEvidenceItem(five[0], HC, ORIGIN_A, five_latest[1], "projection", None,
                                                             False, r5))
        self.assertEqual(len(items), 7)
        # Under the other version's generation, seven is placed by its other revision.
        other = SummaryTarget(self.generation(store, {"steps": PV2}).generation_id, 0)
        (only,) = self.evidence(store, other).items
        self.assertEqual((only.record_id, only.last_revision.processor_version), (seven[0], PV2))

    def test_evidence_pages_by_record(self) -> None:
        store = self.ready()
        records = [self.make(store, f"page-{number}", revision(b"p"))[0][0] for number in range(5)]
        self.make(store, "page-out", revision(b"out", start=DAY + timedelta(days=9)))
        generation = self.generation(store)
        target = SummaryTarget(generation.generation_id, 0)
        page = self.evidence(store, target, limit=2)
        self.assertEqual(([item.record_id for item in page.items], page.next_after), (records[:2], records[1]))
        page = self.evidence(store, target, after=records[1], limit=2)
        self.assertEqual(([item.record_id for item in page.items], page.next_after), (records[2:4], records[3]))
        page = self.evidence(store, target, after=records[3], limit=2)
        self.assertEqual(([item.record_id for item in page.items], page.next_after), (records[4:], None))
        page = self.evidence(store, target, after=records[2], limit=100)
        self.assertEqual(([item.record_id for item in page.items], page.next_after), (records[3:], None))
        self.assertEqual(self.evidence(store, target, after=records[-1] + 100), SummaryEvidence((), (), None))
        self.assertEqual([item.record_id for item in self.evidence(store, target, after=records[0] - 1, limit=1).items],
                         records[:1])
        with store.read() as view:
            def call(measure=MEASURE, record_type="steps", first=DAY, last=DAY, target=target, after=0, limit=10):
                return view.summary_evidence(measure, record_type, first, last, target, after, limit)

            refusals = [
                *[("summary-request-invalid", dict(limit=value)) for value in (0, -1, 1.5, "2", None)],
                *[("summary-request-invalid", dict(after=value)) for value in (-1, 1.5, "0", None)],
                ("summary-request-invalid", dict(first=DAY + timedelta(days=1), last=DAY)),
                *[("summary-request-invalid", dict(first=value)) for value in (
                    "2026-09-21", datetime(2026, 9, 21, tzinfo=UTC), datetime(2026, 9, 21), None)],
                *[("summary-request-invalid", dict(last=value)) for value in (
                    "2026-09-21", datetime(2026, 9, 21, tzinfo=UTC), None)],
                ("summary-request-invalid", dict(target=SummaryTarget(0, 0))),
                ("summary-request-invalid", dict(target=SummaryTarget(generation.generation_id, -1))),
                ("processor-generation-unknown", dict(target=SummaryTarget(424242, 0))),
                ("policy-unknown", dict(target=SummaryTarget(generation.generation_id, 424242))),
                ("summary-target-invalid", dict(record_type="sleep-session")),
            ]
            for failure, kwargs in refusals:
                with self.subTest(failure=failure, kwargs=kwargs):
                    self.assertRefused(failure, call, **kwargs)
            self.assertEqual(len(call(first=DAY, last=DAY, limit=1).items), 1)

    def test_evidence_names_policies_live_at_the_horizon(self) -> None:
        store = self.ready()
        self.make(store, "policy-record", revision(b"p"))
        self.make(store, "policy-weight", revision(b"w", pv=WPV), record_type="weight")
        s1 = self.ssp(store, (OA,), date(2026, 9, 1), date(2026, 9, 15)).policy_id
        s2 = self.ssp(store, (OA,), date(2026, 9, 16), date(2026, 9, 30)).policy_id
        f = self.ftz(store).policy_id
        s3 = self.ssp(store, (OA,), date(2026, 9, 16), date(2026, 9, 30), supersedes=(s2,)).policy_id
        s_measure = self.ssp(store, (OA,), date(2026, 9, 1), date(2026, 9, 30), measure="other-steps").policy_id
        s_weight = self.ssp(store, (OA,), date(2026, 9, 1), date(2026, 9, 30), record_type="weight").policy_id
        s_october = self.ssp(store, (OA,), date(2026, 10, 1), date(2026, 10, 31)).policy_id
        s_open = self.ssp(store, (OA,), None, date(2026, 8, 31)).policy_id
        generation = self.generation(store)
        with store.read() as view:
            policy = view.policy
        cases = [
            (0, ()), (s1, (s1,)), (s2, (s1, s2)), (f, (s1, s2)), (s3, (s1, s3)),
            (s_october, (s1, s3)), (s_open, (s1, s3)),
        ]
        for horizon, expected in cases:
            with self.subTest(horizon=horizon):
                found = self.evidence(store, SummaryTarget(generation.generation_id, horizon), date(2026, 9, 10),
                                      date(2026, 9, 20)).source_selection_policies
                with store.read() as view:
                    self.assertEqual(found, tuple(view.policy(identity) for identity in expected))
        # Bounds are inclusive, and policies are ordered by first date, an unbounded start first.
        with store.read() as view:
            found = view.summary_evidence(MEASURE, "steps", date(2026, 8, 31), date(2026, 10, 1),
                                          SummaryTarget(generation.generation_id, s_open), 0, 10).source_selection_policies
            self.assertEqual(found, tuple(view.policy(identity) for identity in (s_open, s1, s3, s_october)))
            found = view.summary_evidence(MEASURE, "steps", date(2026, 9, 15), date(2026, 9, 15),
                                          SummaryTarget(generation.generation_id, s_open), 0, 10).source_selection_policies
            self.assertEqual(found, (view.policy(s1),))
            found = view.summary_evidence("other-steps", "steps", DAY, DAY,
                                          SummaryTarget(generation.generation_id, s_open), 0, 10).source_selection_policies
            self.assertEqual(found, (view.policy(s_measure),))
            found = view.summary_evidence(MEASURE, "weight", DAY, DAY,
                                          SummaryTarget(generation.generation_id, s_open), 0, 10).source_selection_policies
            self.assertEqual(found, (view.policy(s_weight),))
            found = view.summary_evidence("daily-weight", "weight", DAY, DAY,
                                          SummaryTarget(generation.generation_id, s_open), 0, 10).source_selection_policies
            self.assertEqual(found, ())
        self.assertIsNotNone(policy)
        self.assertGreater(s_weight, 0)


class R68GenerationJobAndSummaryReads(StoreCase):
    def test_generation_reads(self) -> None:
        store = self.ready()
        self.assertEqual(self.view(store, "processor_generations"), ())
        first = self.generation(store, {"steps": PV})
        second = self.generation(store, {"steps": PV2})
        with store.read() as view:
            self.assertEqual(view.processor_generations(), (first, second))
            self.assertEqual(view.processor_generation(first.generation_id), first)
            self.assertEqual(view.processor_generation(second.generation_id), second)
            for unknown in (0, -1, second.generation_id + 1, 424242):
                self.assertIsNone(view.processor_generation(unknown))
            self.assertEqual(view.processor_generation(first.generation_id).registered_at.utcoffset(), timedelta(0))

    def test_refresh_job_read_includes_history(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        other_target = SummaryTarget(self.generation(store, {"steps": PV2}).generation_id, 0)
        key = self.key()
        self.request(store, (key,), target, 3, at(200))
        first = self.claim(store, OWNER, 60, at(201))
        second = self.claim(store, OTHER_OWNER, 60, at(202))
        store.fail_summary_refresh(second.job_id, second.fence, "transient", "synthetic-busy", at(203), at(202.5))
        self.request(store, (key,), target, 3, at(202.7))
        third = self.claim(store, OWNER, LEASE, at(203))
        store.complete_summary_refresh(third.job_id, third.fence, self.entry(store, key), at(204))
        self.request(store, (key,), other_target, 3, at(205))
        self.request(store, (self.key(1),), target, 3, at(205))
        with store.read() as view:
            job = view.summary_refresh_job(first.job_id)
            self.assertEqual(job, SummaryRefreshJob(
                first.job_id, key, target, "completed", 3, 3, at(203), 3, None, None, None, at(200), at(204), (
                    RefreshAttempt(1, OWNER, 1, at(201), at(202), at(202), "lease-expired", None),
                    RefreshAttempt(2, OTHER_OWNER, 1, at(202), at(203), at(202.5), "transient-failure", "synthetic-busy"),
                    RefreshAttempt(3, OWNER, 2, at(203), at(203) + timedelta(days=1), at(204), "completed", None))))
            for value in job.history:
                self.assertEqual(value.claimed_at.utcoffset(), timedelta(0))
            jobs = view.summary_refresh_jobs(key)
            self.assertEqual([(item.job_id, item.target) for item in jobs],
                             [(first.job_id, target), (jobs[1].job_id, other_target)])
            self.assertGreater(jobs[1].job_id, first.job_id)
            self.assertEqual(jobs[0], job)
            self.assertIsNone(view.summary_refresh_job(424242))
            self.assertEqual(view.summary_refresh_jobs(self.key(9)), ())
            self.assertEqual(len(view.summary_refresh_jobs(self.key(1))), 1)

    def test_summary_reads(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        policy = self.ftz(store).policy_id
        other_target = SummaryTarget(generation.generation_id, policy)
        key = self.key()
        first = self.summarize(store, key, target, self.entry(store, key, label=b"1"))
        second = self.summarize(store, key, other_target, self.entry(store, key, label=b"2"))
        third = self.summarize(store, key, target, self.entry(store, key, label=b"3"))
        self.summarize(store, self.key(1), target, self.entry(store, self.key(1), label=b"4"))
        with store.read() as view:
            self.assertEqual(view.summaries(key), (first, second, third))
            self.assertEqual(view.latest_summary(key, target), third)
            self.assertEqual(view.latest_summary(key, other_target), second)
            self.assertIsNone(view.latest_summary(key, SummaryTarget(generation.generation_id + 1, 0)))
            self.assertIsNone(view.latest_summary(self.key(9), target))
            self.assertEqual(view.summaries(self.key(9)), ())
            self.assertEqual(view.summaries(SummaryKey("other-steps", "steps", DAY)), ())
            self.assertEqual((first.token, second.token, third.token), (1, 1, 2))
            for item in view.summaries(key):
                self.assertEqual(item.recorded_at.utcoffset(), timedelta(0))
                for position in item.watermark:
                    self.assertEqual(position.receipt_time.utcoffset(), timedelta(0))

    def test_summary_reads_write_nothing(self) -> None:
        store, generation, outcome = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        policy = self.ftz(store).policy_id
        key = self.key()
        self.summarize(store, key, target, self.entry(store, key, contributors=(outcome.outcome_id,)))
        self.request(store, (key,), target)
        self.free = 0
        with store.read() as view:
            for name, args in (
                ("processor_generation", (generation.generation_id,)), ("processor_generation", (424242,)),
                ("processor_generations", ()), ("summary_refresh_job", (1,)), ("summary_refresh_job", (424242,)),
                ("summary_refresh_jobs", (key,)), ("summaries", (key,)), ("latest_summary", (key, target)),
                ("latest_summary", (self.key(9), target)), ("policies_awaiting_refresh", ()), ("evidence_changes", (5,)),
                ("processed_positions", ("steps", PV)), ("processed_positions", ("weight", WPV)),
                ("summary_evidence", (MEASURE, "steps", DAY, DAY, target, 0, 10)),
                ("summary_evidence", (MEASURE, "steps", DAY, DAY, SummaryTarget(generation.generation_id, policy), 0, 1)),
                ("refresh_counts", (self.tick(),)),
            ):
                with self.subTest(name=name, args=args):
                    self.assertWritesNothing(getattr(view, name), *args)
            self.assertRefused("summary-request-invalid", view.summary_evidence, MEASURE, "steps", DAY, DAY, target, 0, 0)
            self.assertRefused("timestamp-invalid", view.refresh_counts, NAIVE)
        self.free = PLENTY
        # A view is one read transaction: it does not see later commits.
        with store.read() as view:
            before = (view.summaries(key), view.summary_refresh_jobs(key), view.evidence_changes(100),
                      view.processor_generations(), view.policies_awaiting_refresh(), view.processed_positions("steps", PV))
            lease = self.claim(store)
            store.complete_summary_refresh(lease.job_id, lease.fence, self.entry(store, key, label=b"later"), self.tick())
            self.generation(store, {"steps": PV2})
            self.request(store, (), SummaryTarget(generation.generation_id, policy), policy_id=policy)
            self.process(store, self.project(store, "later"), revision(b"later"))
            self.assertEqual((view.summaries(key), view.summary_refresh_jobs(key), view.evidence_changes(100),
                              view.processor_generations(), view.policies_awaiting_refresh(),
                              view.processed_positions("steps", PV)), before)


class R69HealthDataFreeCountsAndSurfaces(StoreCase):
    def test_refresh_counts(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        self.generation(store, {"steps": PV2})
        linked = self.ftz(store).policy_id
        self.ftz(store, "UTC", datetime(2026, 6, 1, tzinfo=UTC))
        with store.read() as view:
            self.assertEqual(view.refresh_counts(at(200)), RefreshCounts(0, 0, 0, 0, 0, 0, 0, 0, 2, 2, 2))
        keys = [self.key(offset) for offset in range(7)]
        completed, failed, delayed, leased, expired, stale, ready = keys
        self.summarize(store, completed, target, when=at(201))
        lease = self.leased(store, failed, SummaryTarget(generation.generation_id, linked), when=at(202), policy_id=linked)
        store.fail_summary_refresh(lease.job_id, lease.fence, "deterministic", "synthetic-defect", None, at(202))
        lease = self.leased(store, delayed, target, when=at(203))
        store.fail_summary_refresh(lease.job_id, lease.fence, "transient", "synthetic-busy", at(400), at(203))
        self.request(store, (leased,), target, when=at(204))
        self.claim(store, OWNER, LEASE, at(204))
        self.request(store, (expired,), target, when=at(205))
        self.claim(store, OWNER, 60, at(205))
        lease = self.leased(store, stale, target, when=at(205.5))
        self.request(store, (stale,), target, when=at(205.6))
        store.complete_summary_refresh(lease.job_id, lease.fence, self.entry(store, stale), at(207))
        self.request(store, (ready,), target, when=at(208))
        changes = len(self.changes(store).changes)
        self.assertEqual(changes, 2)
        with store.read() as view:
            # At 205.5 the stale job's re-queue (207) and the ready job (208) are ahead; the 60-second lease is held.
            self.assertEqual(view.refresh_counts(at(205.5)), RefreshCounts(
                jobs_ready=0, jobs_delayed=3, jobs_leased=2, jobs_lease_expired=0, jobs_completed=1, jobs_failed=1,
                stale_results=1, summaries=1, processor_generations=2, policies_awaiting_refresh=1,
                evidence_changes_pending=changes))
            # Boundaries: not_before and expiry are inclusive.
            self.assertEqual(view.refresh_counts(at(208)), RefreshCounts(2, 1, 1, 1, 1, 1, 1, 1, 2, 1, changes))
            self.assertEqual(view.refresh_counts(at(208).astimezone(IST)), view.refresh_counts(at(208)))
            self.assertEqual(view.refresh_counts(at(400)), RefreshCounts(3, 0, 1, 1, 1, 1, 1, 1, 2, 1, changes))
            self.assertEqual(view.refresh_counts(at(400) - MICRO), RefreshCounts(2, 1, 1, 1, 1, 1, 1, 1, 2, 1, changes))
        self.assertEqual([field.type for field in dataclasses.fields(RefreshCounts)], ["int"] * 11)

    def test_summary_surfaces_are_health_data_free(self) -> None:
        with self.assertNoLogs(level="DEBUG"):
            store = self.ready()
            record = self.project(store, f"{IDENTITY_MARKER}-1")
            outcome = self.process(store, record, revision(HEALTH_MARKER))
            generation = self.generation(store)
            target = SummaryTarget(generation.generation_id, 0)
            key = SummaryKey("synthetic-key-marker", "steps", DAY)
            shown: list[object] = [generation]
            lease = self.leased(store, key, target)
            shown.append(lease)
            entry = self.entry(store, key, contributors=(outcome.outcome_id,))
            shown.append(entry)
            refusals = [
                ("summary-entry-invalid", lambda: store.complete_summary_refresh(
                    lease.job_id, lease.fence, dataclasses.replace(entry, contract_version="x"), self.tick())),
                ("summary-entry-invalid", lambda: store.complete_summary_refresh(
                    lease.job_id, lease.fence, dataclasses.replace(entry, primary_state="Synthetic Key Marker"), self.tick())),
                ("summary-watermark-invalid", lambda: store.complete_summary_refresh(
                    lease.job_id, lease.fence, dataclasses.replace(entry, watermark=(dataclasses.replace(
                        entry.watermark[0], sequence=987654321),)), self.tick())),
                ("summary-contributor-invalid", lambda: store.complete_summary_refresh(
                    lease.job_id, lease.fence, dataclasses.replace(entry, contributors=(987654321,)), self.tick())),
                ("summary-refresh-job-unknown", lambda: store.complete_summary_refresh(987654321, 1, entry, self.tick())),
                ("lease-not-held", lambda: store.complete_summary_refresh(lease.job_id, 987654321, entry, self.tick())),
                ("summary-target-invalid", lambda: self.request(store, (SummaryKey("synthetic-key-marker", "unmapped-marker-type", DAY),), target)),
                ("summary-request-invalid", lambda: self.request(store, (SummaryKey("Synthetic Key Marker", "steps", DAY),), target)),
                ("processor-generation-unknown", lambda: self.request(store, (key,), SummaryTarget(987654321, 0))),
                ("policy-unknown", lambda: self.request(store, (key,), SummaryTarget(generation.generation_id, 987654321))),
                ("processor-generation-invalid", lambda: store.register_processor_generation({"": "x"}, self.tick())),
                ("processor-version-invalid", lambda: store.register_processor_generation(
                    {"steps": "synthetic-key-marker"}, self.tick())),
            ]
            for failure, operation in refusals:
                with self.subTest(failure=failure):
                    caught = self.assertRefused(failure, operation)
                    self.assertEqual(caught.args, (failure,))
                    self.assertEqual(str(caught), failure)
                    for forbidden in ("synthetic-key-marker", "Synthetic Key Marker", "unmapped-marker-type", "987654321",
                                      DAY.isoformat(), IDENTITY_MARKER, HEALTH_MARKER.decode(), SUMMARY_MARKER.decode()):
                        self.assertNotIn(forbidden, repr(caught) + str(caught))
                    shown.append(caught)
            self.free = 0
            retryable = self.assertRetryable("storage-reserve-exhausted", store.complete_summary_refresh, lease.job_id,
                                             lease.fence, entry, self.tick())
            self.assertEqual(retryable.args, ("storage-reserve-exhausted",))
            self.free = PLENTY
            completion = store.complete_summary_refresh(lease.job_id, lease.fence, entry, self.tick())
            shown += [completion, completion.summary]
            with store.read() as view:
                shown += [view.summaries(key), view.latest_summary(key, target), view.summary_refresh_jobs(key),
                          view.summary_refresh_job(lease.job_id), view.refresh_counts(self.tick()),
                          view.evidence_changes(100), view.processed_positions("steps", PV),
                          view.summary_evidence("synthetic-key-marker", "steps", DAY, DAY, target, 0, 10),
                          view.processor_generations(), view.policies_awaiting_refresh()]
            self.request(store, (key,), target)
            stale = self.claim(store)
            self.request(store, (key,), target)
            shown.append(store.complete_summary_refresh(stale.job_id, stale.fence, entry, self.tick()))
            shown.append(self.request(store, (key,), target))
        for value in shown:
            text = repr(value) + str(value)
            self.assertNotIn(SUMMARY_MARKER.decode(), text)
            self.assertNotIn(HEALTH_MARKER.decode(), text)
            self.assertNotIn(IDENTITY_MARKER, text)
        counts = self.view(store, "refresh_counts", self.tick())
        self.assertTrue(all(type(value) is int for value in dataclasses.astuple(counts)))
        self.assertIn(SUMMARY_MARKER, completion.summary.header_bytes)  # type: ignore[union-attr]
        for kind in NEW_TYPES:
            self.assertTrue(getattr(kind, "__dataclass_params__").frozen, kind)


class R70AtomicDurableAndSerialized(StoreCase):
    def test_summary_writes_are_atomic_under_injected_failure(self) -> None:
        store = self.ready()
        a = self.process(store, self.project(store, "atomic-a"), revision(b"a"))
        self.switch_installation(store)
        b = self.process(store, self.project(store, "atomic-b"), revision(b"b"))
        failed = self.fail_processing(store, self.project(store, "atomic-failed"))
        policy = self.ftz(store).policy_id
        generation = self.generation(store)
        target = SummaryTarget(generation.generation_id, policy)
        queued, held, fresh = self.key(0), self.key(1), self.key(2)
        exhausted, waiting = self.key(3), self.key(4)
        self.request(store, (held,), target, when=at(200))
        held_lease = self.claim(store, OWNER, LEASE, at(200))
        self.request(store, (exhausted,), target, 1, at(201))
        exhausted_lease = self.claim(store, OWNER, 60, at(201))
        self.request(store, (queued,), target, when=at(202))
        faults = self.install_fault_triggers()
        check = self.assert_every_partial_failure_commits_nothing
        changes = self.changes(store)
        outcomes = max(change.change_id for change in changes.changes if change.kind == "outcome")
        selections = max(change.change_id for change in changes.changes if change.kind == "selection")
        # A registration, and a request that enqueues, coalesces, re-arms, links, and acknowledges.
        registered = check(faults, lambda: store.register_processor_generation({"steps": PV2}, at(203)))
        self.assertEqual(self.view(store, "processor_generations")[-1], registered)
        result = check(faults, lambda: store.request_summary_refresh(
            (fresh, queued, held), target, 3, at(204), policy_id=policy,
            acknowledge=EvidenceAcknowledgement(outcomes, selections, (failed,))))
        self.assertEqual(result, RefreshRequest(1, 1, 1))
        self.assertEqual(self.changes(store), EvidenceChanges(EvidenceBookmark(outcomes, selections), ()))
        self.assertEqual(self.view(store, "policies_awaiting_refresh"), ())
        # A claim that first fails an exhausted expired lease, then leases the next job.
        lease = check(faults, lambda: store.claim_summary_refresh(OTHER_OWNER, LEASE, at(205)))
        self.assertEqual(self.job(store, exhausted_lease.job_id).state, "failed")
        self.assertEqual(lease.key, queued)
        check(faults, lambda: store.renew_summary_refresh_lease(lease.job_id, lease.fence, LEASE, at(206)))
        # A current completion with two watermark positions and two contributors.
        entry = self.entry(store, queued, contributors=(a.outcome_id, b.outcome_id))
        self.assertEqual(len(entry.watermark), 2)
        completion = check(faults, lambda: store.complete_summary_refresh(lease.job_id, lease.fence, entry, at(207)))
        self.assertEqual(self.view(store, "summaries", queued), (completion.summary,))
        # A stale completion, a stale deterministic failure, a transient failure, and an identical result.
        self.assertEqual(check(faults, lambda: store.complete_summary_refresh(
            held_lease.job_id, held_lease.fence, self.entry(store, held), at(208))), RefreshCompletion(None, True))
        lease = self.claim(store, OWNER, LEASE, at(209))
        self.request(store, (lease.key,), target, when=at(209))
        job = check(faults, lambda: store.fail_summary_refresh(lease.job_id, lease.fence, "deterministic",
                                                               "synthetic-defect", None, at(210)))
        self.assertEqual((job.state, job.attempts), ("queued", 0))
        lease = self.claim(store, OWNER, LEASE, at(211))
        job = check(faults, lambda: store.fail_summary_refresh(lease.job_id, lease.fence, "transient", "synthetic-busy",
                                                               at(212), at(211)))
        self.assertEqual(job.state, "queued")
        self.request(store, (queued,), target, when=at(212))
        for _ in range(6):  # at most every key's job, then the re-requested one
            lease = store.claim_summary_refresh(OWNER, LEASE, at(213))
            assert lease is not None
            if lease.key == queued:
                break
            store.complete_summary_refresh(lease.job_id, lease.fence, self.entry(store, lease.key), at(213))
        else:
            self.fail("the re-requested job was never claimed")
        same = check(faults, lambda: store.complete_summary_refresh(lease.job_id, lease.fence, entry, at(214)))
        self.assertEqual(same, completion)
        self.assertLess(tables_created_by(4), self.fault_tables)
        self.assertEqual(waiting, self.key(4))

    def test_summary_refusals_leave_database_unchanged(self) -> None:
        store, generation, outcome = self.seeded()
        failed = self.fail_processing(store, self.project(store, "refused-failed"))
        policy = self.ftz(store).policy_id
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        lease = self.leased(store, key, target)
        entry = self.entry(store, key, contributors=(outcome.outcome_id,))
        refusals = [
            ("timestamp-invalid", lambda: store.register_processor_generation({"steps": PV2}, NAIVE)),
            ("processor-generation-invalid", lambda: store.register_processor_generation({}, self.tick())),
            ("processor-version-invalid", lambda: store.register_processor_generation({"steps": "x"}, self.tick())),
            ("timestamp-invalid", lambda: self.request(store, (key,), target, when=NAIVE)),
            ("summary-request-invalid", lambda: self.request(store, (key, key), target)),
            ("summary-request-invalid", lambda: self.request(store, (key,), target, acknowledge=EvidenceAcknowledgement(
                0, 0, (424242,)))),
            ("processor-generation-unknown", lambda: self.request(store, (key,), SummaryTarget(424242, 0))),
            ("policy-unknown", lambda: self.request(store, (key,), target, policy_id=424242)),
            ("summary-target-invalid", lambda: self.request(store, (key,), target, policy_id=policy)),
            ("summary-request-invalid", lambda: store.claim_summary_refresh("", LEASE, self.tick())),
            ("summary-request-invalid", lambda: store.claim_summary_refresh(OWNER, 0, self.tick())),
            ("timestamp-invalid", lambda: store.claim_summary_refresh(OWNER, LEASE, NAIVE)),
            ("summary-refresh-job-unknown", lambda: store.renew_summary_refresh_lease(424242, 1, LEASE, self.tick())),
            ("lease-not-held", lambda: store.renew_summary_refresh_lease(lease.job_id, 9, LEASE, self.tick())),
            ("summary-entry-invalid", lambda: store.complete_summary_refresh(
                lease.job_id, lease.fence, dataclasses.replace(entry, evidence_bytes=b""), self.tick())),
            ("summary-watermark-invalid", lambda: store.complete_summary_refresh(
                lease.job_id, lease.fence, dataclasses.replace(entry, watermark=entry.watermark * 2), self.tick())),
            ("summary-contributor-invalid", lambda: store.complete_summary_refresh(
                lease.job_id, lease.fence, dataclasses.replace(entry, contributors=(outcome.outcome_id,) * 2), self.tick())),
            ("summary-request-invalid", lambda: store.fail_summary_refresh(
                lease.job_id, lease.fence, "transient", "synthetic-busy", None, self.tick())),
            ("lease-not-held", lambda: store.fail_summary_refresh(
                lease.job_id, 9, "deterministic", "synthetic-defect", None, self.tick())),
        ]
        for failure, operation in refusals:
            with self.subTest(failure=failure):
                self.assertRefused(failure, operation)
        self.free = 0
        for operation in (
            lambda: store.register_processor_generation({"steps": PV2}, self.tick()),
            lambda: self.request(store, (self.key(1),), target),
            lambda: store.renew_summary_refresh_lease(lease.job_id, lease.fence, LEASE, self.tick()),
            lambda: store.complete_summary_refresh(lease.job_id, lease.fence, entry, self.tick()),
            lambda: store.fail_summary_refresh(lease.job_id, lease.fence, "deterministic", "synthetic-defect", None,
                                               self.tick()),
        ):
            self.assertRetryable("storage-reserve-exhausted", operation)
        self.free = PLENTY
        # None of them moved the bookmark, linked the policy, or advanced the token.
        self.assertEqual(self.changes(store).bookmark, EvidenceBookmark(0, 0))
        self.assertIn(failed, [change.change_id for change in self.changes(store).changes if change.kind == "failed-job"])
        self.assertEqual(self.view(store, "policies_awaiting_refresh"), (policy,))
        stored = store.complete_summary_refresh(lease.job_id, lease.fence, entry, self.tick()).summary
        self.assertEqual(stored.token, 1)  # type: ignore[union-attr]

    def test_summary_state_survives_reopen(self) -> None:
        store, generation, outcome = self.seeded()
        failed = self.fail_processing(store, self.project(store, "reopen-failed"))
        policy = self.ftz(store).policy_id
        target = SummaryTarget(generation.generation_id, policy)
        key, held_key = self.key(0), self.key(1)
        self.summarize(store, key, target, self.entry(store, key, contributors=(outcome.outcome_id,)))
        changes = self.changes(store)
        self.request(store, (held_key,), target, policy_id=policy, acknowledge=EvidenceAcknowledgement(
            max(c.change_id for c in changes.changes if c.kind == "outcome"),
            max(c.change_id for c in changes.changes if c.kind == "selection"), (failed,)))
        held = self.claim(store)
        self.request(store, (held_key,), target)
        self.generation(store, {"steps": PV2})

        def snapshot(handle):
            with handle.read() as view:
                return (view.processor_generations(), view.summary_refresh_jobs(key), view.summary_refresh_jobs(held_key),
                        view.summaries(key), view.latest_summary(key, target), view.policies_awaiting_refresh(),
                        view.evidence_changes(100), view.processed_positions("steps", PV),
                        view.summary_evidence(MEASURE, "steps", DAY, DAY, target, 0, 10), view.refresh_counts(at(9000)))

        before = snapshot(store)
        store.close()
        reopened = self.open()
        self.assertEqual(snapshot(reopened), before)
        # The held lease and its stale token survive.
        self.assertEqual(reopened.complete_summary_refresh(held.job_id, held.fence, self.entry(reopened, held_key),
                                                           self.tick()), RefreshCompletion(None, True))
        self.assertEqual(self.claim(reopened).token, 2)
        self.request(reopened, (key,), target)
        self.assertEqual(self.claim(reopened).token, 2)

    def test_racing_requests_leave_one_open_job(self) -> None:
        store, generation, _ = self.seeded()
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        rounds = 6
        totals = RefreshRequest(0, 0, 0)
        for round_number in range(rounds):
            barrier = threading.Barrier(2, timeout=30)
            results: list[object] = [None, None]

            def run(index: int) -> None:
                try:
                    handle = open_store(self.path, reserve_bytes=RESERVE, free_bytes=lambda _path: PLENTY)
                    try:
                        barrier.wait()
                        for _attempt in range(50):
                            try:
                                results[index] = handle.request_summary_refresh((key,), target, 3, at(300 + round_number))
                                break
                            except StoreRetryable as busy:
                                if busy.failure != "store-busy":
                                    raise
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
                if isinstance(result, BaseException):
                    raise result
            for result in results:
                self.assertIsInstance(result, RefreshRequest)
                totals = RefreshRequest(*(x + y for x, y in zip(dataclasses.astuple(totals), dataclasses.astuple(result))))
            jobs = self.jobs(store, key)
            self.assertEqual([job.state for job in jobs], ["queued"])
        self.assertEqual(totals, RefreshRequest(1, 2 * rounds - 1, 0))
        self.assertEqual(self.claim(store, when=at(400)).token, 2 * rounds)

    def test_summary_writes_are_gated_by_the_reserve(self) -> None:
        store, generation, _ = self.seeded()
        failed = self.fail_processing(store, self.project(store, "gated-failed"))
        target = SummaryTarget(generation.generation_id, 0)
        key = self.key()
        first = self.summarize(store, key, target)
        held = self.leased(store, key, target)
        self.request(store, (self.key(1),), target, 1, at(500))
        exhausted = self.claim(store, OWNER, 60, at(500))
        selections = max(c.change_id for c in self.changes(store).changes if c.kind == "selection")
        self.free = 0
        gated = [
            lambda: store.register_processor_generation({"steps": PV2}, self.tick()),
            lambda: self.request(store, (key,), target),
            lambda: self.request(store, (self.key(2),), target),
            lambda: self.request(store, (), target, acknowledge=EvidenceAcknowledgement(0, selections, ())),
            lambda: self.request(store, (), target, acknowledge=EvidenceAcknowledgement(0, 0, (failed,))),
            lambda: store.renew_summary_refresh_lease(held.job_id, held.fence, LEASE, self.tick()),
            lambda: store.complete_summary_refresh(held.job_id, held.fence, self.entry(store, key, label=b"new"), self.tick()),
            lambda: store.complete_summary_refresh(held.job_id, held.fence, self.entry(store, key), self.tick()),
            lambda: store.fail_summary_refresh(held.job_id, held.fence, "transient", "synthetic-busy", at(600), self.tick()),
            lambda: store.fail_summary_refresh(held.job_id, held.fence, "deterministic", "synthetic-defect", None, self.tick()),
            lambda: store.claim_summary_refresh(OTHER_OWNER, LEASE, at(501)),  # fails the exhausted lease
        ]
        for number, operation in enumerate(gated):
            with self.subTest(operation=number):
                self.assertRetryable("storage-reserve-exhausted", operation)
        # Calls that write nothing succeed below the reserve.
        self.assertEqual(self.assertWritesNothing(store.register_processor_generation, GEN_MAP, self.tick()), generation)
        self.assertIsNone(self.assertWritesNothing(store.claim_summary_refresh, OTHER_OWNER, LEASE, at(500.5)))
        # Stale completions and failures are gated too.
        self.free = PLENTY
        self.request(store, (key,), target)
        self.free = 0
        self.assertRetryable("storage-reserve-exhausted", store.complete_summary_refresh, held.job_id, held.fence,
                             self.entry(store, key), self.tick())
        self.assertRetryable("storage-reserve-exhausted", store.fail_summary_refresh, held.job_id, held.fence,
                             "deterministic", "synthetic-defect", None, self.tick())
        self.free = PLENTY
        self.assertEqual(store.complete_summary_refresh(held.job_id, held.fence, self.entry(store, key), self.tick()),
                         RefreshCompletion(None, True))
        self.assertEqual(self.view(store, "summaries", key), (first,))
        self.assertEqual(exhausted.key, self.key(1))


class R71EarlierVersionsUnchanged(StoreCase):
    def test_summary_writes_change_no_earlier_table(self) -> None:
        store, generation, outcome = self.seeded()
        failed = self.fail_processing(store, self.project(store, "earlier-failed"))
        open_record = self.project(store, "earlier-open")
        self.ssp(store, (OA,), date(2026, 9, 1), date(2026, 9, 30))
        policy = self.ftz(store).policy_id
        store.enqueue_processing(*open_record, PV, 3, self.tick())
        earlier = tables_created_by(4)
        self.assertIn("processing_jobs", earlier)
        before = contents(self.path)["tables"]
        changes = self.changes(store)
        target = SummaryTarget(generation.generation_id, policy)
        key = self.key()
        self.generation(store, {"steps": PV2})
        self.request(store, (key, self.key(1)), target, policy_id=policy, acknowledge=EvidenceAcknowledgement(
            max(c.change_id for c in changes.changes if c.kind == "outcome"),
            max(c.change_id for c in changes.changes if c.kind == "selection"), (failed,)))
        lease = self.claim(store)
        store.renew_summary_refresh_lease(lease.job_id, lease.fence, LEASE, self.tick())
        store.complete_summary_refresh(lease.job_id, lease.fence, self.entry(
            store, lease.key, contributors=(outcome.outcome_id,) if lease.key == key else (),
            ssp=self.view(store, "applicable_source_selection", MEASURE, "steps", lease.key.local_date).policy_id),
            self.tick())
        lease = self.claim(store)
        self.request(store, (lease.key,), target)
        store.fail_summary_refresh(lease.job_id, lease.fence, "deterministic", "synthetic-defect", None, self.tick())
        lease = self.claim(store)
        when = self.tick()
        store.fail_summary_refresh(lease.job_id, lease.fence, "transient", "synthetic-busy", when, when)
        with store.read() as view:
            view.processed_positions("steps", PV)
            view.summary_evidence(MEASURE, "steps", DAY, DAY, target, 0, 10)
            view.evidence_changes(10)
            view.refresh_counts(self.tick())
            view.policies_awaiting_refresh()
        after = contents(self.path)["tables"]
        for name in earlier:
            self.assertEqual(after[name], before[name], name)  # type: ignore[index]
        self.assertGreater(len(changed_tables({"tables": before}, {"tables": after})), 0)


if __name__ == "__main__":
    unittest.main()
