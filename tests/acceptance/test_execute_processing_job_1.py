"""Acceptance tests for `execute-processing-job@1`.

Requirement numbers refer to `packs/execute-processing-job-1.spec.md`. Every store is a
real temporary Operational Store built through the published `operational-store@4`
operations: archive an Ingest Batch, index it with the published envelope decoder,
record a selection, enqueue, and claim. Projections are the synthetic `projection/v1`
vectors with marker strings in their values, origin, and Source Identity. Processors
are the published ones, except where a test registers a deliberately faulty synthetic
processor. Faults are injected by delegating stores, by SQLite triggers, and by a
second connection that holds the write lock. Every time comes from a test clock.

Oracles are independent of the unit: expected outcomes come from calling the
published `interpret` on the leased evidence, and indexed columns come from the
header bytes or the projection text. "Writes nothing" compares the whole database;
"one write" replays the single store call on a copy taken before the execution.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import sqlite3
import tempfile
import traceback
import unittest
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import tzdata

from activity_tracker_portal import ingress
from activity_tracker_portal import interpret_heart_rate, interpret_sleep_session, interpret_steps, interpret_weight
from activity_tracker_portal import operational_store as store_module
from activity_tracker_portal import worker as worker_module
from activity_tracker_portal.execute_processing_job import (
    PROCESSOR_VERSIONS,
    PROCESSORS,
    ExecutionResult,
    RetryPolicy,
    execute_processing_job,
    make_job_handler,
)
from activity_tracker_portal.interpret_source_projection import (
    COMMON_VERSION,
    BodySchema,
    CanonicalRevision,
    FallbackTimezonePolicy,
    NumericField,
    PolicyPending,
    ProcessingInput,
    Quarantine,
    Unmodeled,
    decode_envelope,
    interpret,
)
from activity_tracker_portal.operational_store import (
    BatchItem,
    IngestBatchRecord,
    ItemPosition,
    Lease,
    MessageHeader,
    OutcomeEntry,
    ProcessingJob,
    ProcessingOutcome,
    ProjectionEntry,
    RetractionEntry,
    RevisionEntry,
    SourceKey,
    SourceRecord,
    StoreRefusal,
    StoreRetryable,
    open_store,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/execute-processing-job.1.json").read_text(encoding="utf-8"))
EFFECTS = json.loads((ROOT / "packs/execute-processing-job-1.effects.json").read_text(encoding="utf-8"))
SCOPE_MAP = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
MODEL = json.loads((ROOT / "projection/v1/model.json").read_text(encoding="utf-8"))
SCHEMA_TYPES = {path.name.removesuffix(".schema.json") for path in (ROOT / "projection/v1/schemas").glob("*.schema.json")}
VECTORS = {vector["name"]: vector for vector in
           json.loads((ROOT / "projection/v1/vectors/positive.json").read_text(encoding="utf-8"))["vectors"]}

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 11, 25, 9, 0, 0, 123456, tzinfo=UTC)
NAIVE = datetime(2026, 11, 25, 12, 0)
RESERVE = 1_000_000
PLENTY = 10**12
LEASE = 60
INSTALLATION = "0f8fad5b-d9cb-469f-a165-70867728950e"
CYCLE = "e3b0c442-98fc-4c14-9afb-f4c8996fb924"
CHANGES = "5d41402a-bc4b-4a76-b971-9d911017c592"
OWNER = "synthetic-worker-1"
OTHER_OWNER = "synthetic-worker-2"
HEALTH_MARKER = "synthetic-health-marker"
IDENTITY_MARKER = "synthetic-identity-marker"
ORIGIN_MARKER = "synthetic-origin-marker"
ORIGIN = "org.example." + ORIGIN_MARKER
MARKERS = (HEALTH_MARKER, IDENTITY_MARKER, ORIGIN_MARKER)
SYNTHETIC_VERSION = "interpret-synthetic@1"
MODELED = {"steps": "interpret-steps@1", "heart-rate": "interpret-heart-rate@1",
           "sleep-session": "interpret-sleep-session@1", "weight": "interpret-weight@1"}
MODULES = {"interpret-steps@1": interpret_steps, "interpret-heart-rate@1": interpret_heart_rate,
           "interpret-sleep-session@1": interpret_sleep_session, "interpret-weight@1": interpret_weight}
INTERPRET_FAILURES = ("processing-input-invalid", "projection-digest-mismatch", "projection-identity-mismatch",
                      "processor-invariant-violated")
DETERMINISTIC = ("processor-unavailable", "processor-record-type-mismatch", "processing-evidence-missing",
                 *INTERPRET_FAILURES, "outcome-conflict", "outcome-invalid")
TRANSIENT = "interpretation-unexpected-failure"
# The store's own job-failure reasons, which this unit never writes but may report.
STORE_REASONS = ("retries-exhausted", "lease-expired-retries-exhausted")
DISPOSITIONS = ("outcome-recorded", "retry-scheduled", "job-failed", "already-recorded", "lease-lost")
WRITES = ("complete_processing_job", "fail_processing_job", "renew_processing_lease", "enqueue_processing",
          "claim_processing_job", "quarantine_item", "index_items", "record_selection")
INSTANT_TEXT = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d{3}|\d{6}|\d{9}))?Z$")


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def epoch_ns(text: str) -> int:
    year, month, day, hour, minute, second, fraction = INSTANT_TEXT.match(text).groups()
    whole = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), tzinfo=UTC)
    return (whole - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(seconds=1) * 10**9 + int((fraction or "").ljust(9, "0"))


def projection(record_type: str = "steps", *, number: int = 1, vector: str | None = None,
               last_modified: str | None = None, schema_version: int | None = None,
               drop: tuple[str, ...] = (), **record: object) -> bytes:
    """A synthetic projection/v1 vector with marker strings in its values, origin, and Source Identity."""
    value = json.loads(VECTORS[vector or f"{record_type}-maximal"]["bytes"])
    metadata = value["metadata"]
    metadata["id"] = f"{IDENTITY_MARKER}-{number}"
    metadata["data_origin"]["package_name"] = ORIGIN
    metadata["client_record_id"] = HEALTH_MARKER
    if "device" in metadata:
        metadata["device"]["manufacturer"] = HEALTH_MARKER
    if last_modified is not None:
        metadata["last_modified_time"] = last_modified
    if schema_version is not None:
        value["projection_schema_version"] = schema_version
    for name in drop:
        value["record"].pop(name, None)
    value["record"].update(record)
    return json.dumps(value, separators=(",", ":")).encode()


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


def expected_entry(outcome: object) -> OutcomeEntry:
    """The OutcomeEntry an outcome must be recorded as; indexed columns come from the header bytes."""
    if isinstance(outcome, CanonicalRevision):
        header = json.loads(outcome.header_bytes)
        start, end = header["time"]["start"], header["time"].get("end")
        revision = RevisionEntry(
            outcome.header_bytes, outcome.body_bytes, header["body_schema"]["body_version"],
            epoch_ns(start["instant"]), None if end is None else epoch_ns(end["instant"]),
            date.fromisoformat(start["local_date"]), None if end is None else date.fromisoformat(end["local_date"]),
            start["attribution"], None if end is None else end["attribution"],
        )
        return OutcomeEntry("revision", outcome.processor_version, outcome.policy_identity, None, None, None, revision)
    if isinstance(outcome, Unmodeled):
        return OutcomeEntry("unmodeled", outcome.processor_version, outcome.policy_identity, outcome.reason, None, None, None)
    if isinstance(outcome, PolicyPending):
        return OutcomeEntry("policy-pending", outcome.processor_version, outcome.policy_identity, outcome.reason, None,
                            outcome.policy_kind, None)
    assert isinstance(outcome, Quarantine)
    return OutcomeEntry("quarantine", outcome.processor_version, outcome.policy_identity, outcome.reason, outcome.pointer,
                        None, None)


def stored_entry(outcome: ProcessingOutcome) -> OutcomeEntry:
    revision = outcome.revision
    entry = None if revision is None else RevisionEntry(
        revision.header_bytes, revision.body_bytes, revision.body_version, revision.start_epoch_ns, revision.end_epoch_ns,
        revision.start_local_date, revision.end_local_date, revision.start_attribution, revision.end_attribution,
    )
    return OutcomeEntry(outcome.kind, outcome.processor_version, outcome.policy_identity, outcome.reason, outcome.pointer,
                        outcome.policy_kind, entry)


def chain(error: BaseException) -> list[BaseException]:
    """An exception with every exception linked to it through __cause__ or __context__."""
    found: list[BaseException] = []
    pending: list[BaseException | None] = [error]
    while pending:
        item = pending.pop()
        if item is None or any(item is seen for seen in found):
            continue
        found.append(item)
        pending.extend((item.__cause__, item.__context__))
    return found


class Clock:
    """A test clock that counts its reads."""

    def __init__(self, value: datetime) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return self.value


class Crash(BaseException):
    """A simulated process exit: not an Exception, so nothing may catch it."""


class RaisingVersions:
    """A supported-schema-version collection whose membership test raises: a fault outside interpret's named ones."""

    def __init__(self, error: Callable[[], BaseException]) -> None:
        self.error = error

    def __contains__(self, item: object) -> bool:
        raise self.error()

    def __iter__(self):
        return iter((1,))

    def __len__(self) -> int:
        return 1


@dataclass(frozen=True)
class SyntheticProcessor:
    """A deliberately faulty Steps processor registered under a synthetic version."""

    record_type: str = "steps"
    processor_version: str = SYNTHETIC_VERSION
    supported_schema_versions: object = frozenset({1})
    body_schema: BodySchema = BodySchema("steps", 1, (NumericField("/count", "integer", "count"),))
    quarantine_reasons: frozenset[str] = frozenset()
    body_fault: bool = False
    during: Callable[[], object] | None = None

    def interpret_body(self, record, temporal):
        if self.during is not None:
            self.during()
        if self.body_fault:
            raise ValueError(f"{HEALTH_MARKER} {record['count']}")
        return {"count": record["count"]}


def registry(processor: object, version: str = SYNTHETIC_VERSION):
    return MappingProxyType({**PROCESSORS, version: processor})


class Delegating:
    """Delegates to a real store; records write calls; runs hooks before and after them; can tamper read views."""

    def __init__(self, store, *, before: dict[str, Callable] | None = None, after: dict[str, Callable] | None = None,
                 view: Callable | None = None) -> None:
        self._store = store
        self._before = before or {}
        self._after = after or {}
        self._view = view
        self.calls: list[str] = []

    def read(self):
        return self._read()

    @contextmanager
    def _read(self):
        with self._store.read() as view:
            yield view if self._view is None else self._view(view)

    def __getattr__(self, name: str):
        attribute = getattr(self._store, name)
        if not callable(attribute) or name == "settings":
            return attribute

        def call(*args, **kwargs):
            self.calls.append(name)
            hook = self._before.get(name)
            if hook is not None:
                hook(*args, **kwargs)
            result = attribute(*args, **kwargs)
            hook = self._after.get(name)
            if hook is not None:
                hook(*args, **kwargs)
            return result
        return call


class TamperedView:
    """A read view with some reads replaced."""

    def __init__(self, view, **reads: Callable) -> None:
        self._view = view
        self._reads = reads

    def __getattr__(self, name: str):
        if name in self._reads:
            return lambda *args, **kwargs: self._reads[name](self._view, *args, **kwargs)
        return getattr(self._view, name)


def raised(expected: type[BaseException], operation: Callable[[], object]) -> tuple[BaseException, str]:
    """The expected exception an operation raises, with its real traceback formatted inside the handler.

    assertRaises keeps only a traceback-free copy, so it cannot check what a traceback shows. Any other
    exception propagates unchanged.
    """
    try:
        operation()
    except expected as error:
        return error, "".join(traceback.format_exception(error))
    raise AssertionError(f"{expected.__name__} was not raised")


def raiser(error: BaseException) -> Callable:
    def hook(*args, **kwargs):
        raise error
    return hook


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
        self.ticks = 0
        self.copies = 0

    # ---- building a store through the published operations ----

    def tick(self) -> datetime:
        """Store-building times advance by a millisecond, so a lease or backoff left open never comes due by itself."""
        self.ticks += 1
        return T0 + timedelta(milliseconds=self.ticks)

    def archive(self, item: BatchItem, record_type: str, *, schema_version: int = 1, change: bool = False) -> int:
        self.sequence += 1
        sequence = self.sequence
        envelope = json.dumps({"synthetic": True, "sequence": sequence}, separators=(",", ":")).encode()
        header = MessageHeader(INSTALLATION, sequence, digest(envelope), 1, self.tick(), envelope,
                               b'{"synthetic-receipt":%d}' % sequence)
        batch = IngestBatchRecord(record_type, schema_version, "change-cycle" if change else "initial-snapshot",  # type: ignore[arg-type]
                                  CHANGES if change else CYCLE, sequence, 1, digest(b"synthetic-chain-%d" % sequence), (item,))
        self.store.archive_ingest_batch(header, batch)
        return sequence

    def deliver(self, payload: bytes, record_type: str = "steps", *, schema_version: int = 1,
                entry_version: int | None = None, change: bool = False, **entry_changes: object) -> int:
        """Archive one projection and index it from its decoded envelope; return its Source Record."""
        sequence = self.archive(BatchItem(projection=payload), record_type, schema_version=schema_version, change=change)
        envelope = decode_envelope(payload, record_type, entry_version or schema_version)
        assert not isinstance(envelope, Quarantine), envelope
        entry = ProjectionEntry(ItemPosition(INSTALLATION, sequence, 0), envelope.source_system_kind, envelope.origin,
                                envelope.source_identity, envelope.last_modified_time)
        entry = replace(entry, **entry_changes)
        self.store.index_items((entry,), self.tick())
        with self.store.read() as view:
            record_id = view.source_record_id(SourceKey(entry.source_system_kind, entry.origin, record_type, entry.source_identity))
        assert record_id is not None
        return record_id

    def record(self, record_id: int) -> SourceRecord:
        with self.store.read() as view:
            found = view.source_record(record_id)
        assert found is not None
        return found

    def select(self, record_id: int, evidence_index: int = -1) -> int:
        record = self.record(record_id)
        evidence = record.evidence[evidence_index]
        return self.store.record_selection(record_id, "projection", (evidence.evidence_id,), record.evidence_head,
                                           self.tick()).selection_id

    def retract(self, record_id: int) -> int:
        key = self.record(record_id).key
        sequence = self.archive(BatchItem(retracted_identity=key.source_identity), key.record_type, change=True)
        self.store.index_items((RetractionEntry(ItemPosition(INSTALLATION, sequence, 0)),), self.tick())
        record = self.record(record_id)
        return self.store.record_selection(record_id, "retraction", (record.evidence[-1].evidence_id,),
                                           record.evidence_head, self.tick()).selection_id

    def claim(self, record_id: int, selection_id: int, version: str, *, max_attempts: int = 3) -> Lease:
        when = self.tick()
        self.store.enqueue_processing(record_id, selection_id, version, max_attempts, when)
        lease = self.store.claim_processing_job(OWNER, LEASE, when)
        assert lease is not None and lease.record_id == record_id, lease
        return lease

    def reclaim(self, lease: Lease, owner: str = OTHER_OWNER) -> Lease:
        later = self.store.claim_processing_job(owner, LEASE, lease.expires_at + timedelta(seconds=1))
        assert later is not None and later.job_id == lease.job_id and later.fence == lease.fence + 1, later
        return later

    def leased(self, payload: bytes, record_type: str = "steps", version: str | None = None, *,
               max_attempts: int = 3, **deliver: object) -> Lease:
        record_id = self.deliver(payload, record_type, **deliver)
        return self.claim(record_id, self.select(record_id), version or PROCESSOR_VERSIONS[record_type],
                          max_attempts=max_attempts)

    # ---- reading results ----

    def job(self, job_id: int) -> ProcessingJob:
        with self.store.read() as view:
            found = view.processing_job(job_id)
        assert found is not None
        return found

    def outcomes(self, record_id: int) -> tuple[ProcessingOutcome, ...]:
        with self.store.read() as view:
            return view.processing_outcomes(record_id)

    def interpreted(self, lease: Lease, processor: object, policies: tuple = ()):
        """The published interpret on the leased evidence, read independently of the unit."""
        with self.store.read() as view:
            record = view.source_record(lease.record_id)
            (evidence,) = [item for item in record.evidence if item.evidence_id == lease.evidence_id]
            message = view.archived_message(evidence.position.installation_id, evidence.position.sequence)
            data = view.projection_bytes(lease.evidence_id)
        key = record.key
        return interpret(ProcessingInput(
            key.source_system_kind, key.origin, key.record_type, key.source_identity, lease.record_id,
            lease.selection_id, lease.evidence_id, evidence.projection_sha256, evidence.last_modified_time,
            message.batch.projection_schema_version, data,
        ), processor, policies)

    # ---- executing ----

    def clock_for(self, lease: Lease, seconds: float = -55) -> Clock:
        """A clock inside the lease by default: five seconds after the claim."""
        return Clock(lease.expires_at + timedelta(seconds=seconds))

    def execute(self, lease: Lease, clock: Clock | None = None, store=None, **options) -> ExecutionResult:
        return execute_processing_job(self.store if store is None else store, lease,
                                      self.clock_for(lease) if clock is None else clock, **options)

    def assertResult(self, result: ExecutionResult, lease: Lease, disposition: str, outcome_kind: str | None = None,
                     reason: str | None = None, retry_at: datetime | None = None) -> None:
        self.assertIsInstance(result, ExecutionResult)
        self.assertEqual((result.job_id, result.fence, result.disposition, result.outcome_kind, result.reason, result.retry_at),
                         (lease.job_id, lease.fence, disposition, outcome_kind, reason, retry_at))

    def assertWritesNothing(self, operation: Callable, *args, **kwargs):
        before = contents(self.path)
        result = operation(*args, **kwargs)
        self.assertEqual(contents(self.path), before, "an execution that writes nothing changed the database")
        return result

    def control(self) -> Path:
        """A copy of the database, taken before an execution."""
        self.copies += 1
        target = self.path.with_name(f"control-{self.copies}.sqlite3")
        source = sqlite3.connect(self.path)
        copy = sqlite3.connect(target)
        try:
            source.backup(copy)
        finally:
            copy.close()
            source.close()
        return target

    def replay(self, control: Path, operation: Callable) -> dict[str, object]:
        """The control copy after the one store call the execution should have made."""
        store = open_store(control, reserve_bytes=RESERVE, free_bytes=lambda _path: PLENTY)
        try:
            operation(store)
        finally:
            store.close()
        return contents(control)

    def assertHeldOpen(self, lease: Lease) -> None:
        """The lease is still held and its attempt open: nothing was written for it."""
        job = self.job(lease.job_id)
        self.assertEqual((job.state, job.fence, job.history[-1].result), ("leased", lease.fence, None))


# ---------------------------------------------------------------- R1

class R1GovernedScopeContract(unittest.TestCase):
    def test_contract_shape_and_dependencies(self) -> None:
        self.assertEqual((CONTRACT["contract_schema"], CONTRACT["scope"], CONTRACT["version"], CONTRACT["kind"]),
                         (1, "execute-processing-job", "1", "integration"))
        self.assertEqual(CONTRACT["dependencies"], [
            {"scope": "interpret-source-projection", "version": "1"},
            {"scope": "operational-store", "version": "4"},
            {"scope": "interpret-steps", "version": "1"},
            {"scope": "interpret-heart-rate", "version": "1"},
            {"scope": "interpret-sleep-session", "version": "1"},
            {"scope": "interpret-weight", "version": "1"},
        ])
        self.assertEqual([(item["name"], item["outcome"]) for item in CONTRACT["named_failures"]], [
            ("processing-job-unknown", "invariant-refusal"),
            ("timestamp-invalid", "invariant-refusal"),
            ("store-busy", "retryable"),
            ("storage-reserve-exhausted", "retryable"),
        ])
        self.assertEqual([item["name"] for item in CONTRACT["observable_effects"]],
                         ["processing-outcome-write", "processing-failure-write"])
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["specification"], "packs/execute-processing-job-1.spec.md")
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_execute_processing_job_1.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_execute_processing_job_1.sh")
        # The failure vocabulary the unit records is named in its outputs.
        failed_job = next(item for item in CONTRACT["outputs"] if item["name"] == "failed_job")["description"]
        for reason in DETERMINISTIC:
            self.assertIn(reason, failed_job)
        scheduled = next(item for item in CONTRACT["outputs"] if item["name"] == "scheduled_retry")["description"]
        self.assertIn(TRANSIENT, scheduled)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        row = next(line for line in SCOPE_MAP.splitlines() if line.startswith("| `execute-processing-job` |"))
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        self.assertEqual(cells[1], "integration")
        listed = set(re.findall(r"`([a-z0-9-]+)`", cells[-1]))
        self.assertTrue(listed)
        self.assertLessEqual(listed, {item["scope"] for item in CONTRACT["dependencies"]})

    def test_effects_declaration_matches_contract(self) -> None:
        self.assertEqual(EFFECTS["effects_format"], 1)
        self.assertEqual([(item["kind"], item["name"], item["outcome"]) for item in EFFECTS["effects"]], [
            ("action", "processing-outcome-write", "confirmed"),
            ("action", "processing-failure-write", "confirmed"),
        ])
        self.assertEqual([item["name"] for item in EFFECTS["effects"]],
                         [item["name"] for item in CONTRACT["observable_effects"]])


# ---------------------------------------------------------------- R2

class R2Registry(unittest.TestCase):
    def test_registry_maps_each_version_to_its_processor(self) -> None:
        self.assertEqual(set(PROCESSORS), {*MODULES, COMMON_VERSION})
        self.assertEqual(COMMON_VERSION, "interpret-source-projection@1")
        self.assertIsNone(PROCESSORS[COMMON_VERSION])
        for version, module in MODULES.items():
            with self.subTest(version):
                self.assertIs(PROCESSORS[version], module.PROCESSOR)
                self.assertEqual(PROCESSORS[version].processor_version, version)

    def test_every_projection_type_has_a_processor_version(self) -> None:
        self.assertEqual(set(MODEL["records"]), SCHEMA_TYPES)
        self.assertEqual(len(SCHEMA_TYPES), 41)
        self.assertEqual(set(PROCESSOR_VERSIONS), SCHEMA_TYPES)
        for record_type, version in PROCESSOR_VERSIONS.items():
            with self.subTest(record_type):
                self.assertIn(version, PROCESSORS)
                self.assertEqual(version, MODELED.get(record_type, COMMON_VERSION))
                if PROCESSORS[version] is not None:
                    self.assertEqual(PROCESSORS[version].record_type, record_type)

    def test_registry_is_read_only(self) -> None:
        for mapping, key, value in ((PROCESSORS, "interpret-steps@1", None),
                                    (PROCESSORS, SYNTHETIC_VERSION, None),
                                    (PROCESSOR_VERSIONS, "steps", COMMON_VERSION),
                                    (PROCESSOR_VERSIONS, "synthetic-type", COMMON_VERSION)):
            with self.subTest(key=key):
                before = dict(mapping)
                with self.assertRaises(TypeError):
                    mapping[key] = value  # type: ignore[index]
                with self.assertRaises(TypeError):
                    del mapping["interpret-steps@1" if mapping is PROCESSORS else "steps"]  # type: ignore[attr-defined]
                for name in ("clear", "pop", "popitem", "update", "setdefault"):
                    self.assertFalse(hasattr(mapping, name), name)
                self.assertEqual(dict(mapping), before)


# ---------------------------------------------------------------- R3

class R3InputFromTheLeasedEvidence(StoreCase):
    def test_input_is_built_from_the_leased_evidence(self) -> None:
        newer = projection(last_modified="2026-01-15T10:00:00.123456789Z", count=1200)
        stale = projection(last_modified="2026-01-15T09:00:00Z", count=3400)
        record_id = self.deliver(newer)
        self.deliver(stale)
        record = self.record(record_id)
        self.assertEqual(record.evidence_head, record.evidence[1].evidence_id)
        # The current selection is the newer projection, delivered first; the stale one is the evidence head.
        lease = self.claim(record_id, self.select(record_id, evidence_index=0), "interpret-steps@1")
        self.assertEqual(lease.evidence_id, record.evidence[0].evidence_id)
        result = self.execute(lease)
        self.assertResult(result, lease, "outcome-recorded", "revision")
        (outcome,) = self.outcomes(record_id)
        self.assertEqual((outcome.selection_id, outcome.evidence_id), (lease.selection_id, lease.evidence_id))
        header = json.loads(outcome.revision.header_bytes)
        self.assertEqual(
            {name: header[name] for name in ("source_system_kind", "origin", "record_type", "source_identity", "record_id",
                                             "selection_id", "evidence_id", "projection_sha256", "last_modified_time",
                                             "projection_schema_version", "processor_version")},
            {"source_system_kind": "health-connect", "origin": ORIGIN, "record_type": "steps",
             "source_identity": f"{IDENTITY_MARKER}-1", "record_id": str(lease.record_id),
             "selection_id": str(lease.selection_id), "evidence_id": str(lease.evidence_id),
             "projection_sha256": digest(newer), "last_modified_time": "2026-01-15T10:00:00.123456789Z",
             "projection_schema_version": 1, "processor_version": "interpret-steps@1"})
        self.assertEqual(json.loads(outcome.revision.body_bytes), {"count": 1200})
        self.assertEqual(stored_entry(outcome), expected_entry(self.interpreted(lease, interpret_steps.PROCESSOR)))

    def test_schema_version_comes_from_the_archived_batch(self) -> None:
        # The bytes say version 1 and the archived batch says 2: interpret must see 2.
        lease = self.leased(projection(number=1), schema_version=2, entry_version=1)
        result = self.execute(lease)
        self.assertResult(result, lease, "job-failed", None, "projection-identity-mismatch")
        self.assertEqual(self.outcomes(lease.record_id), ())
        # The bytes say version 2 and the archived batch says 1: still the batch's.
        lease = self.leased(projection(number=2, schema_version=2), schema_version=1, entry_version=2)
        result = self.execute(lease)
        self.assertResult(result, lease, "job-failed", None, "projection-identity-mismatch")
        self.assertEqual(self.outcomes(lease.record_id), ())
        # Agreeing versions interpret, and the header names the batch's version.
        lease = self.leased(projection(number=3))
        self.assertResult(self.execute(lease), lease, "outcome-recorded", "revision")
        (outcome,) = self.outcomes(lease.record_id)
        self.assertEqual(json.loads(outcome.revision.header_bytes)["projection_schema_version"], 1)

    def test_leased_selection_is_interpreted_when_newer_evidence_exists(self) -> None:
        first = projection(last_modified="2026-01-15T09:00:00Z", count=1111)
        lease = self.leased(first)
        newer = self.deliver(projection(last_modified="2026-01-15T11:00:00Z", count=2222), change=True)
        self.assertEqual(newer, lease.record_id)
        newer_selection = self.select(lease.record_id)
        self.assertNotEqual(newer_selection, lease.selection_id)
        result = self.execute(lease)
        self.assertResult(result, lease, "outcome-recorded", "revision")
        (outcome,) = self.outcomes(lease.record_id)
        self.assertEqual((outcome.selection_id, outcome.evidence_id), (lease.selection_id, lease.evidence_id))
        self.assertEqual(json.loads(outcome.revision.body_bytes), {"count": 1111})
        header = json.loads(outcome.revision.header_bytes)
        self.assertEqual((header["selection_id"], header["projection_sha256"]), (str(lease.selection_id), digest(first)))
        self.assertEqual(stored_entry(outcome), expected_entry(self.interpreted(lease, interpret_steps.PROCESSOR)))


# ---------------------------------------------------------------- R4

class R4OutcomesAreRecordedAsInterpreted(StoreCase):
    def assertRevisionRecorded(self, lease: Lease, processor: object, policies: tuple = ()) -> ProcessingOutcome:
        result = self.execute(lease, policies=policies)
        self.assertResult(result, lease, "outcome-recorded", "revision")
        (outcome,) = self.outcomes(lease.record_id)
        expected = self.interpreted(lease, processor, policies)
        self.assertIsInstance(expected, CanonicalRevision)
        self.assertEqual(stored_entry(outcome), expected_entry(expected))
        self.assertEqual((outcome.processor_version, outcome.policy_identity), (lease.processor_version, expected.policy_identity))
        self.assertEqual(self.job(lease.job_id).state, "completed")
        return outcome

    def test_revision_is_recorded_with_its_header_columns(self) -> None:
        # An interval whose offsets put the start on a later local date than the end.
        lease = self.leased(projection(number=1, start_time="2026-01-15T20:00:00Z", start_zone_offset="+05:30",
                                       end_time="2026-01-15T21:00:00.500Z", end_zone_offset="-08:00"))
        revision = self.assertRevisionRecorded(lease, interpret_steps.PROCESSOR).revision
        self.assertEqual(
            (revision.body_version, revision.start_epoch_ns, revision.end_epoch_ns, revision.start_local_date,
             revision.end_local_date, revision.start_attribution, revision.end_attribution),
            (1, epoch_ns("2026-01-15T20:00:00Z"), epoch_ns("2026-01-15T21:00:00.500Z"), date(2026, 1, 16),
             date(2026, 1, 15), "experienced", "experienced"))
        # An instant: every end column is None.
        lease = self.leased(projection("weight", number=2, time="2026-01-15T23:00:00.250Z", zone_offset="+05:30"), "weight")
        revision = self.assertRevisionRecorded(lease, interpret_weight.PROCESSOR).revision
        self.assertEqual(
            (revision.start_epoch_ns, revision.end_epoch_ns, revision.start_local_date, revision.end_local_date,
             revision.start_attribution, revision.end_attribution),
            (epoch_ns("2026-01-15T23:00:00.250Z"), None, date(2026, 1, 16), None, "experienced", None))
        # A source offset at the start only: the end is inferred under the supplied policy.
        policies = (FallbackTimezonePolicy("synthetic-policy-a", None, "Asia/Kolkata"),)
        lease = self.leased(projection(number=3, start_time="2026-01-15T18:00:00Z", start_zone_offset="+00:00",
                                       end_time="2026-01-15T19:00:00Z", drop=("end_zone_offset",)))
        outcome = self.assertRevisionRecorded(lease, interpret_steps.PROCESSOR, policies)
        revision = outcome.revision
        self.assertEqual(
            (revision.start_local_date, revision.end_local_date, revision.start_attribution, revision.end_attribution),
            (date(2026, 1, 15), date(2026, 1, 16), "experienced", "inferred"))
        self.assertEqual(outcome.policy_identity,
                         "fallback-timezone:" + json.dumps([["synthetic-policy-a"], tzdata.IANA_VERSION], separators=(",", ":")))

    def test_each_non_revision_outcome_is_recorded(self) -> None:
        cases = (
            ("unmodeled", projection("distance", number=1), "distance", COMMON_VERSION,
             OutcomeEntry("unmodeled", COMMON_VERSION, "none", "record-type-not-modeled", None, None, None)),
            ("policy-pending", projection(number=2, vector="steps-minimal"), "steps", "interpret-steps@1",
             OutcomeEntry("policy-pending", "interpret-steps@1", "none", "fallback-timezone-policy-missing", None,
                          "fallback-timezone", None)),
            ("quarantine", projection(number=3, count=0), "steps", "interpret-steps@1",
             OutcomeEntry("quarantine", "interpret-steps@1", "none", "steps-count-out-of-range", "/record/count", None, None)),
            ("quarantine", projection(number=4, start_time="2026-01-15T08:00:00Z", end_time="2026-01-15T07:00:00Z"),
             "steps", "interpret-steps@1",
             OutcomeEntry("quarantine", "interpret-steps@1", "none", "interval-reversed", "/record/end_time", None, None)),
        )
        for kind, payload, record_type, version, entry in cases:
            with self.subTest(entry.reason):
                lease = self.leased(payload, record_type, version)
                result = self.execute(lease)
                self.assertResult(result, lease, "outcome-recorded", kind, entry.reason)
                (outcome,) = self.outcomes(lease.record_id)
                self.assertEqual(stored_entry(outcome), entry)
                self.assertEqual(entry, expected_entry(self.interpreted(lease, PROCESSORS[version])))
                self.assertEqual(self.job(lease.job_id).state, "completed")

    def test_recorded_bytes_equal_the_interpreted_bytes(self) -> None:
        for number, (vector, record_type) in enumerate((
            ("steps-maximal", "steps"), ("steps-leap-days", "steps"), ("heart-rate-maximal", "heart-rate"),
            ("heart-rate-instant-precision", "heart-rate"), ("sleep-session-maximal", "sleep-session"),
            ("sleep-session-text-escaping", "sleep-session"), ("weight-maximal", "weight"),
            ("weight-other-context", "weight"),
        ), start=1):
            with self.subTest(vector):
                lease = self.leased(projection(record_type, number=number, vector=vector), record_type)
                self.execute(lease)
                (outcome,) = self.outcomes(lease.record_id)
                expected = self.interpreted(lease, PROCESSORS[lease.processor_version])
                self.assertEqual((outcome.revision.header_bytes, outcome.revision.body_bytes),
                                 (expected.header_bytes, expected.body_bytes))
                self.assertEqual((outcome.revision.header_sha256, outcome.revision.body_sha256),
                                 (digest(expected.header_bytes), expected.body_sha256))

    def test_newer_selection_requeues_after_completion(self) -> None:
        lease = self.leased(projection(last_modified="2026-01-15T09:00:00Z"))
        self.deliver(projection(last_modified="2026-01-15T11:00:00Z", count=2222), change=True)
        newer = self.select(lease.record_id)
        clock = self.clock_for(lease)
        result = self.execute(lease, clock)
        self.assertResult(result, lease, "outcome-recorded", "revision")
        job = self.job(lease.job_id)
        self.assertEqual((job.state, job.selection_id, job.attempts, job.not_before, job.history[-1].result),
                         ("queued", newer, 0, clock.value, "completed"))
        (outcome,) = self.outcomes(lease.record_id)
        self.assertEqual(outcome.selection_id, lease.selection_id)

    def test_retraction_after_lease_supersedes_after_completion(self) -> None:
        lease = self.leased(projection())
        self.retract(lease.record_id)
        result = self.execute(lease)
        self.assertResult(result, lease, "outcome-recorded", "revision")
        job = self.job(lease.job_id)
        self.assertEqual((job.state, job.history[-1].result), ("superseded", "completed"))
        (outcome,) = self.outcomes(lease.record_id)
        self.assertEqual(outcome.selection_id, lease.selection_id)

    def assertProcessorRevision(self, record_type: str, body: dict) -> None:
        version = MODELED[record_type]
        lease = self.leased(projection(record_type), record_type)
        self.assertEqual(lease.processor_version, version)
        outcome = self.assertRevisionRecorded(lease, MODULES[version].PROCESSOR)
        self.assertEqual(outcome.processor_version, version)
        self.assertEqual(json.loads(outcome.revision.header_bytes)["processor_version"], version)
        self.assertEqual(json.loads(outcome.revision.body_bytes), body)

    def test_steps_job_records_a_revision(self) -> None:
        self.assertProcessorRevision("steps", {"count": 4321})

    def test_heart_rate_job_records_a_revision(self) -> None:
        self.assertProcessorRevision("heart-rate", {"samples": [
            {"time": "2026-01-15T07:35:00Z", "beats_per_minute": 72, "local_date": "2026-01-15"},
            {"time": "2026-01-15T07:40:00Z", "beats_per_minute": 75, "local_date": "2026-01-15"},
        ]})

    def test_sleep_session_job_records_a_revision(self) -> None:
        self.assertProcessorRevision("sleep-session", {
            "stages": [{"start_time": "2026-01-15T07:30:00Z", "end_time": "2026-01-15T07:40:00Z", "stage": 2},
                       {"start_time": "2026-01-15T07:40:00Z", "end_time": "2026-01-15T08:00:00Z", "stage": 4}],
            "title": "Synthetic night", "notes": "synthetic notes"})

    def test_weight_job_records_a_revision(self) -> None:
        self.assertProcessorRevision("weight", {"weight_grams": 72500.5})


# ---------------------------------------------------------------- R5

class R5UnmodeledRecordTypes(StoreCase):
    def test_unmodeled_type_records_unmodeled_under_the_common_version(self) -> None:
        # Every projection/v1 record type reaches a terminal outcome under its registered version.
        for number, record_type in enumerate(sorted(MODEL["records"]), start=1):
            with self.subTest(record_type):
                lease = self.leased(projection(record_type, number=number), record_type)
                self.assertEqual(lease.processor_version, MODELED.get(record_type, COMMON_VERSION))
                result = self.execute(lease)
                (outcome,) = self.outcomes(lease.record_id)
                if record_type in MODELED:
                    self.assertResult(result, lease, "outcome-recorded", "revision")
                    self.assertEqual(outcome.kind, "revision")
                else:
                    self.assertResult(result, lease, "outcome-recorded", "unmodeled", "record-type-not-modeled")
                    self.assertEqual(stored_entry(outcome), OutcomeEntry(
                        "unmodeled", COMMON_VERSION, "none", "record-type-not-modeled", None, None, None))
                self.assertEqual(self.job(lease.job_id).state, "completed")

    def test_invalid_unmodeled_projection_is_quarantined(self) -> None:
        lease = self.leased(projection("distance", synthetic_extra=HEALTH_MARKER), "distance")
        self.assertEqual(lease.processor_version, COMMON_VERSION)
        result = self.execute(lease)
        expected = self.interpreted(lease, None)
        self.assertIsInstance(expected, Quarantine)
        self.assertEqual(expected.reason, "projection-schema-invalid")
        self.assertResult(result, lease, "outcome-recorded", "quarantine", "projection-schema-invalid")
        (outcome,) = self.outcomes(lease.record_id)
        self.assertEqual(stored_entry(outcome), OutcomeEntry(
            "quarantine", COMMON_VERSION, "none", "projection-schema-invalid", expected.pointer, None, None))
        self.assertEqual(self.job(lease.job_id).state, "completed")


# ---------------------------------------------------------------- R6

# Replaced reads and fault hooks accept any call syntax, so an implementation may call the published
# operations with positional or keyword arguments.

def drop_evidence(evidence_id: int):
    def source_record(view, *args, **kwargs):
        found = view.source_record(*args, **kwargs)
        return None if found is None else replace(found, evidence=tuple(
            item for item in found.evidence if item.evidence_id != evidence_id))
    return source_record


def other_digest(view, *args, **kwargs):
    found = view.source_record(*args, **kwargs)
    return None if found is None else replace(found, evidence=tuple(
        replace(item, projection_sha256=digest(b"synthetic-other-bytes")) for item in found.evidence))


def without_items(view, *args, **kwargs):
    message = view.archived_message(*args, **kwargs)
    return None if message is None or message.batch is None else replace(message, batch=replace(message.batch, items=()))


def nothing(view, *args, **kwargs):
    return None


def no_items(view, *args, **kwargs):
    return ()


MISSING = {
    "source-record": dict(source_record=nothing),
    "archived-batch": dict(archived_message=nothing, archived_items=no_items),
    "projection-bytes": dict(projection_bytes=nothing, archived_message=without_items, archived_items=no_items),
}


class R6DeterministicFailures(StoreCase):
    def assertFailedJob(self, lease: Lease, reason: str, *, store=None, **options) -> None:
        clock = self.clock_for(lease)
        control = self.control()
        result = self.execute(lease, clock, store=store, **options)
        self.assertResult(result, lease, "job-failed", None, reason)
        self.assertEqual(self.outcomes(lease.record_id), ())
        job = self.job(lease.job_id)
        self.assertEqual((job.state, job.failure, job.attempts, job.closed_at), ("failed", reason, 1, clock.value))
        self.assertEqual((job.history[-1].result, job.history[-1].reason), ("deterministic-failure", reason))
        # The whole effect is that one failure write.
        self.assertEqual(contents(self.path), self.replay(control, lambda store: store.fail_processing_job(
            lease.job_id, lease.fence, "deterministic", reason, None, clock.value)))

    def test_interpret_failures_fail_the_job(self) -> None:
        lease = self.leased(projection(number=1))
        self.assertFailedJob(lease, "processing-input-invalid",
                             policies=(FallbackTimezonePolicy("", None, "Asia/Kolkata"),))
        lease = self.leased(projection(number=2))
        tampered = Delegating(self.store, view=lambda view: TamperedView(view, source_record=other_digest))
        self.assertFailedJob(lease, "projection-digest-mismatch", store=tampered)
        lease = self.leased(projection(number=3), last_modified_time=datetime(2026, 1, 15, 9, 0, 1, tzinfo=UTC))
        self.assertFailedJob(lease, "projection-identity-mismatch")
        lease = self.leased(projection(number=4), source_identity=f"{IDENTITY_MARKER}-other")
        self.assertFailedJob(lease, "projection-identity-mismatch")
        record_id = self.deliver(projection(number=5))
        lease = self.claim(record_id, self.select(record_id), SYNTHETIC_VERSION)
        self.assertFailedJob(lease, "processor-invariant-violated", processors=registry(SyntheticProcessor(body_fault=True)))

    def test_unregistered_processor_version_fails_the_job(self) -> None:
        record_id = self.deliver(projection(number=1))
        lease = self.claim(record_id, self.select(record_id), "interpret-steps@2")
        self.assertFailedJob(lease, "processor-unavailable")
        # A registry without the common version cannot serve an unmodeled job.
        lease = self.leased(projection("distance", number=2), "distance")
        without_common = MappingProxyType({version: processor for version, processor in PROCESSORS.items()
                                           if version != COMMON_VERSION})
        self.assertFailedJob(lease, "processor-unavailable", processors=without_common)

    def test_processor_record_type_mismatch_fails_the_job(self) -> None:
        lease = self.leased(projection("weight", number=1), "weight", "interpret-steps@1")
        self.assertFailedJob(lease, "processor-record-type-mismatch")
        record_id = self.deliver(projection(number=2))
        lease = self.claim(record_id, self.select(record_id), SYNTHETIC_VERSION)
        self.assertFailedJob(lease, "processor-record-type-mismatch",
                             processors=registry(SyntheticProcessor(record_type="weight")))

    def test_missing_evidence_fails_the_job(self) -> None:
        for number, (name, reads) in enumerate(MISSING.items(), start=1):
            with self.subTest(name):
                lease = self.leased(projection(number=number))
                self.assertFailedJob(lease, "processing-evidence-missing",
                                     store=Delegating(self.store, view=lambda view, reads=reads: TamperedView(view, **reads)))
        lease = self.leased(projection(number=len(MISSING) + 1))
        with self.subTest("leased-evidence"):
            self.assertFailedJob(lease, "processing-evidence-missing", store=Delegating(
                self.store, view=lambda view: TamperedView(view, source_record=drop_evidence(lease.evidence_id))))

    def test_outcome_conflict_fails_the_job(self) -> None:
        # A different outcome is already recorded under the key: a processor changed without a new version.
        lease = self.leased(projection(number=1))
        self.store.complete_processing_job(lease.job_id, lease.fence, OutcomeEntry(
            "unmodeled", "interpret-steps@1", "none", "synthetic-earlier-outcome", None, None, None), self.tick())
        lease = self.claim(lease.record_id, lease.selection_id, "interpret-steps@1")
        clock = self.clock_for(lease)
        control = self.control()
        result = self.execute(lease, clock)
        self.assertResult(result, lease, "job-failed", None, "outcome-conflict")
        self.assertEqual(len(self.outcomes(lease.record_id)), 1)
        job = self.job(lease.job_id)
        self.assertEqual((job.state, job.failure, job.history[-1].result, job.closed_at),
                         ("failed", "outcome-conflict", "deterministic-failure", clock.value))
        self.assertEqual(contents(self.path), self.replay(control, lambda store: store.fail_processing_job(
            lease.job_id, lease.fence, "deterministic", "outcome-conflict", None, clock.value)))
        # The store refuses a completion as outcome-invalid: recorded under that name.
        for number, failure in enumerate(("outcome-invalid", "outcome-conflict"), start=2):
            with self.subTest(failure):
                lease = self.leased(projection(number=number))
                refusing = Delegating(self.store, before={"complete_processing_job": raiser(StoreRefusal(failure))})
                self.assertFailedJob(lease, failure, store=refusing)
                self.assertEqual(refusing.calls, ["complete_processing_job", "fail_processing_job"])

    def test_failed_job_is_not_retried(self) -> None:
        lease = self.leased(projection(number=1), max_attempts=5)
        result = self.execute(lease, processors=registry(SyntheticProcessor(processor_version="interpret-steps@1", body_fault=True), "interpret-steps@1"))
        self.assertResult(result, lease, "job-failed", None, "processor-invariant-violated")
        job = self.job(lease.job_id)
        # Attempts were left, yet the job failed under its own reason, not retries-exhausted.
        self.assertEqual((job.state, job.attempts, job.max_attempts, job.failure), ("failed", 1, 5, "processor-invariant-violated"))
        self.assertEqual([(item.result, item.reason) for item in job.history],
                         [("deterministic-failure", "processor-invariant-violated")])
        self.assertIsNone(self.store.claim_processing_job(OTHER_OWNER, LEASE, at(100_000)))
        with self.store.read() as view:
            self.assertEqual(view.selections_awaiting_processing({"steps": "interpret-steps@1"}, 10), ())
        self.assertEqual(self.job(lease.job_id).history, job.history)


# ---------------------------------------------------------------- R7

def transient(error: Callable[[], BaseException] = lambda: RuntimeError(HEALTH_MARKER)):
    return registry(SyntheticProcessor(supported_schema_versions=RaisingVersions(error)))


class R7TransientFailures(StoreCase):
    def synthetic_lease(self, number: int = 1, *, max_attempts: int = 3) -> Lease:
        record_id = self.deliver(projection(number=number))
        return self.claim(record_id, self.select(record_id), SYNTHETIC_VERSION, max_attempts=max_attempts)

    def test_unexpected_interpretation_error_schedules_a_retry(self) -> None:
        lease = self.synthetic_lease()
        clock = self.clock_for(lease)
        control = self.control()
        result = self.execute(lease, clock, processors=transient())
        retry_at = clock.value + timedelta(seconds=60)
        self.assertResult(result, lease, "retry-scheduled", None, TRANSIENT, retry_at)
        job = self.job(lease.job_id)
        self.assertEqual((job.state, job.not_before, job.attempts, job.failure), ("queued", retry_at, 1, None))
        self.assertEqual((job.history[-1].result, job.history[-1].reason, job.history[-1].finished_at),
                         ("transient-failure", TRANSIENT, clock.value))
        self.assertEqual(self.outcomes(lease.record_id), ())
        self.assertEqual(contents(self.path), self.replay(control, lambda store: store.fail_processing_job(
            lease.job_id, lease.fence, "transient", TRANSIENT, retry_at, clock.value)))
        # Not claimable before the backoff, claimable at it.
        self.assertIsNone(self.store.claim_processing_job(OTHER_OWNER, LEASE, retry_at - timedelta(microseconds=1)))
        self.assertEqual(self.store.claim_processing_job(OTHER_OWNER, LEASE, retry_at).fence, lease.fence + 1)

    def test_backoff_doubles_and_is_capped(self) -> None:
        policy = RetryPolicy()
        self.assertEqual((policy.base_seconds, policy.max_seconds), (60, 3600))
        self.assertEqual([policy.delay(attempt) for attempt in range(1, 11)],
                         [timedelta(seconds=seconds) for seconds in (60, 120, 240, 480, 960, 1920, 3600, 3600, 3600, 3600)])
        self.assertEqual(policy.delay(1000), timedelta(seconds=3600))
        self.assertEqual([RetryPolicy(base_seconds=5, max_seconds=12).delay(attempt) for attempt in (1, 2, 3, 4)],
                         [timedelta(seconds=seconds) for seconds in (5, 10, 12, 12)])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            policy.base_seconds = 1  # type: ignore[misc]
        # Through the store: each retry waits the delay for the attempts used, this claim included.
        lease = self.synthetic_lease(max_attempts=9)
        waits = []
        for attempt in range(1, 9):
            clock = self.clock_for(lease)
            result = self.execute(lease, clock, processors=transient())
            self.assertEqual((result.disposition, self.job(lease.job_id).attempts), ("retry-scheduled", attempt))
            waits.append(result.retry_at - clock.value)
            self.assertEqual(self.job(lease.job_id).not_before, result.retry_at)
            lease = self.store.claim_processing_job(OWNER, LEASE, result.retry_at)
        self.assertEqual(waits, [timedelta(seconds=seconds) for seconds in (60, 120, 240, 480, 960, 1920, 3600, 3600)])
        # A caller's policy is used as given.
        lease = self.synthetic_lease(number=2, max_attempts=9)
        clocks = []
        for _ in range(3):
            clock = self.clock_for(lease)
            result = self.execute(lease, clock, processors=transient(), retry=RetryPolicy(base_seconds=5, max_seconds=12))
            clocks.append(result.retry_at - clock.value)
            lease = self.store.claim_processing_job(OWNER, LEASE, result.retry_at)
        self.assertEqual(clocks, [timedelta(seconds=5), timedelta(seconds=10), timedelta(seconds=12)])
        # A completion that re-queues for a newer selection resets the attempts used: the next
        # claim's first transient failure waits 60 s, although the job's history holds two attempts.
        lease = self.synthetic_lease(number=3)
        self.deliver(projection(number=3, last_modified="2026-01-15T11:00:00Z", count=2222), change=True)
        self.select(lease.record_id)
        self.assertEqual(self.execute(lease, processors=registry(SyntheticProcessor())).disposition, "outcome-recorded")
        job = self.job(lease.job_id)
        self.assertEqual((job.state, job.attempts), ("queued", 0))
        lease = self.store.claim_processing_job(OWNER, LEASE, job.not_before)
        self.assertEqual(lease.job_id, job.job_id)
        clock = self.clock_for(lease)
        result = self.execute(lease, clock, processors=transient())
        job = self.job(lease.job_id)
        self.assertEqual((job.attempts, len(job.history)), (1, 2))
        self.assertEqual((result.disposition, result.retry_at - clock.value), ("retry-scheduled", timedelta(seconds=60)))

    def test_retries_are_bounded_by_the_job(self) -> None:
        lease = self.synthetic_lease(max_attempts=2)
        first = self.execute(lease, processors=transient())
        self.assertEqual(first.disposition, "retry-scheduled")
        lease = self.store.claim_processing_job(OWNER, LEASE, first.retry_at)
        clock = self.clock_for(lease)
        control = self.control()
        result = self.execute(lease, clock, processors=transient())
        self.assertResult(result, lease, "job-failed", None, "retries-exhausted")
        job = self.job(lease.job_id)
        self.assertEqual((job.state, job.failure, job.attempts), ("failed", "retries-exhausted", 2))
        self.assertEqual([(item.result, item.reason) for item in job.history],
                         [("transient-failure", TRANSIENT), ("transient-failure", TRANSIENT)])
        self.assertEqual(contents(self.path), self.replay(control, lambda store: store.fail_processing_job(
            lease.job_id, lease.fence, "transient", TRANSIENT, clock.value + timedelta(seconds=120), clock.value)))
        self.assertIsNone(self.store.claim_processing_job(OWNER, LEASE, at(100_000)))
        # An expired claim counts as an attempt used: after a reclaim, the first transient failure
        # waits 120 s, and the job's bound counts the expiry too.
        lease = self.synthetic_lease(number=2, max_attempts=3)
        lease = self.reclaim(lease)
        clock = self.clock_for(lease)
        result = self.execute(lease, clock, processors=transient())
        self.assertResult(result, lease, "retry-scheduled", None, TRANSIENT, clock.value + timedelta(seconds=120))
        lease = self.store.claim_processing_job(OWNER, LEASE, result.retry_at)
        self.assertResult(self.execute(lease, processors=transient()), lease, "job-failed", None, "retries-exhausted")
        job = self.job(lease.job_id)
        self.assertEqual((job.state, job.failure, job.attempts), ("failed", "retries-exhausted", 3))
        self.assertEqual([(item.result, item.reason) for item in job.history],
                         [("lease-expired", None), ("transient-failure", TRANSIENT), ("transient-failure", TRANSIENT)])

    def test_interrupt_propagates_and_writes_nothing(self) -> None:
        for name, error in (("keyboard-interrupt", KeyboardInterrupt), ("system-exit", SystemExit)):
            with self.subTest(name):
                lease = self.synthetic_lease(number=1 if name == "keyboard-interrupt" else 2)
                clock = self.clock_for(lease)
                recording = Delegating(self.store)
                before = contents(self.path)
                with self.assertRaises(error):
                    self.execute(lease, clock, store=recording, processors=transient(error))
                self.assertEqual(contents(self.path), before)
                self.assertEqual((recording.calls, clock.calls), ([], 0))
                self.assertHeldOpen(lease)


# ---------------------------------------------------------------- R8

class R8TheStoreCannotWrite(StoreCase):
    # Each path's write, and the processors that reach it.
    PATHS = (
        ("outcome", None, ["complete_processing_job"]),
        ("transient", "transient", ["fail_processing_job"]),
        ("deterministic", "deterministic", ["fail_processing_job"]),
    )

    def path_lease(self, path: str, number: int) -> tuple[Lease, dict]:
        record_id = self.deliver(projection(number=number))
        if path == "outcome":
            return self.claim(record_id, self.select(record_id), "interpret-steps@1"), {}
        if path == "transient":
            return self.claim(record_id, self.select(record_id), SYNTHETIC_VERSION), {"processors": transient()}
        return self.claim(record_id, self.select(record_id), "interpret-steps@9"), {}

    def assertPassesThrough(self, failure: str) -> None:
        """Each write path raises the injected retryable object unchanged, tries no other write, and writes nothing."""
        number = 0
        for path, _kind, writes in self.PATHS:
            with self.subTest(path):
                number += 1
                lease, options = self.path_lease(path, number)
                injected = StoreRetryable(failure)
                recording = Delegating(self.store, before={writes[0]: raiser(injected)})
                clock = self.clock_for(lease)
                before = contents(self.path)
                with self.assertRaises(StoreRetryable) as caught:
                    self.execute(lease, clock, store=recording, **options)
                self.assertIs(caught.exception, injected)
                self.assertIsNone(caught.exception.__cause__)
                self.assertEqual(recording.calls, writes, "no other write is tried")
                self.assertEqual(clock.calls, 1)
                self.assertEqual(contents(self.path), before)
                self.assertHeldOpen(lease)
        # A refused completion's deterministic failure passes a retryable through the same way.
        lease, _ = self.path_lease("outcome", number + 1)
        injected = StoreRetryable(failure)
        recording = Delegating(self.store, before={"complete_processing_job": raiser(StoreRefusal("outcome-conflict")),
                                                   "fail_processing_job": raiser(injected)})
        before = contents(self.path)
        with self.assertRaises(StoreRetryable) as caught:
            self.execute(lease, store=recording)
        self.assertIs(caught.exception, injected)
        self.assertEqual(recording.calls, ["complete_processing_job", "fail_processing_job"])
        self.assertEqual(contents(self.path), before)
        self.assertHeldOpen(lease)

    def test_busy_store_writes_nothing_and_propagates(self) -> None:
        # Injected: the store's own retryable object reaches the caller unchanged.
        self.assertPassesThrough("store-busy")
        # Real: another connection holds the write lock.
        lease, _ = self.path_lease("outcome", 50)
        recording = Delegating(self.store)
        before = contents(self.path)
        blocker = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(StoreRetryable) as caught:
                self.execute(lease, store=recording)
        finally:
            blocker.execute("ROLLBACK")
        self.assertEqual(caught.exception.failure, "store-busy")
        self.assertEqual(recording.calls, ["complete_processing_job"])
        self.assertEqual(contents(self.path), before)
        self.assertHeldOpen(lease)

    def test_reserve_exhaustion_writes_nothing_and_propagates(self) -> None:
        self.assertPassesThrough("storage-reserve-exhausted")
        # Real: free space falls below the reserve between the read and the write.
        for number, (path, _kind, writes) in enumerate(self.PATHS, start=60):
            with self.subTest(f"real-{path}"):
                lease, options = self.path_lease(path, number)
                recording = Delegating(self.store, before={writes[0]: lambda *args, **kwargs: setattr(self, "free", RESERVE - 1)})
                before = contents(self.path)
                with self.assertRaises(StoreRetryable) as caught:
                    self.execute(lease, store=recording, **options)
                self.free = PLENTY
                self.assertEqual(caught.exception.failure, "storage-reserve-exhausted")
                self.assertEqual(recording.calls, writes)
                self.assertEqual(contents(self.path), before)
                self.assertHeldOpen(lease)

    def test_reclaimed_job_records_the_same_outcome_once(self) -> None:
        lease = self.leased(projection())
        before = contents(self.path)
        with self.assertRaises(StoreRetryable):
            self.execute(lease, store=Delegating(self.store, before={
                "complete_processing_job": raiser(StoreRetryable("store-busy"))}))
        self.assertEqual(contents(self.path), before)
        self.assertHeldOpen(lease)
        # The lease expires; a later claim takes the job with a higher fence and records the outcome once.
        later = self.reclaim(lease)
        result = self.execute(later)
        self.assertResult(result, later, "outcome-recorded", "revision")
        (outcome,) = self.outcomes(lease.record_id)
        self.assertEqual((outcome.job_id, outcome.fence), (later.job_id, later.fence))
        job = self.job(lease.job_id)
        self.assertEqual([item.result for item in job.history], ["lease-expired", "completed"])
        self.assertEqual(job.state, "completed")
        # Another job for the same selection and version finds the identical outcome and does not append it.
        again = self.claim(lease.record_id, lease.selection_id, "interpret-steps@1")
        self.assertNotEqual(again.job_id, lease.job_id)
        result = self.execute(again)
        self.assertResult(result, again, "outcome-recorded", "revision")
        self.assertEqual(self.outcomes(lease.record_id), (outcome,))
        self.assertEqual(self.job(again.job_id).state, "completed")


# ---------------------------------------------------------------- R9

class R9FencingAndOneWrite(StoreCase):
    def test_expired_but_held_lease_completes(self) -> None:
        lease = self.leased(projection())
        clock = self.clock_for(lease, seconds=600)
        recording = Delegating(self.store)
        result = self.execute(lease, clock, store=recording)
        self.assertResult(result, lease, "outcome-recorded", "revision")
        self.assertEqual(recording.calls, ["complete_processing_job"])
        job = self.job(lease.job_id)
        self.assertEqual((job.state, job.fence, job.closed_at), ("completed", lease.fence, clock.value))
        (outcome,) = self.outcomes(lease.record_id)
        self.assertEqual((outcome.fence, outcome.recorded_at), (lease.fence, clock.value))

    def test_reclaimed_lease_is_lost_and_writes_nothing(self) -> None:
        # Found before interpreting.
        lease = self.leased(projection(number=1))
        later = self.reclaim(lease)
        clock = self.clock_for(lease)
        recording = Delegating(self.store)
        result = self.assertWritesNothing(self.execute, lease, clock, store=recording)
        self.assertResult(result, lease, "lease-lost")
        self.assertEqual((recording.calls, clock.calls), ([], 0))
        self.assertEqual(self.job(lease.job_id).fence, later.fence)
        # Found by the write: a claim takes the job between the read and the write.
        for number, (path, write, options) in enumerate((
            ("outcome", "complete_processing_job", {}),
            ("transient", "fail_processing_job", {"processors": transient()}),
            ("deterministic", "fail_processing_job", {}),
        ), start=2):
            with self.subTest(path):
                record_id = self.deliver(projection(number=number))
                version = {"outcome": "interpret-steps@1", "transient": SYNTHETIC_VERSION, "deterministic": "interpret-steps@9"}[path]
                lease = self.claim(record_id, self.select(record_id), version)
                expected: dict[str, object] = {}

                def reclaim(*args, lease=lease, **kwargs) -> None:
                    self.reclaim(lease)
                    expected.update(contents(self.path))
                recording = Delegating(self.store, before={write: reclaim})
                result = self.execute(lease, store=recording, **options)
                self.assertResult(result, lease, "lease-lost")
                self.assertEqual(recording.calls, [write])
                self.assertEqual(contents(self.path), expected)
                self.assertEqual(self.job(lease.job_id).fence, lease.fence + 1)
        # Found by the write: a claim takes the job while the projection is being interpreted.
        for number, body_fault in ((10, False), (11, True)):
            with self.subTest("during-interpretation", body_fault=body_fault):
                record_id = self.deliver(projection(number=number))
                lease = self.claim(record_id, self.select(record_id), SYNTHETIC_VERSION)
                expected = {}

                def reclaim_now(lease=lease, expected=expected) -> None:
                    self.reclaim(lease)
                    expected.update(contents(self.path))
                recording = Delegating(self.store)
                processors = registry(SyntheticProcessor(body_fault=body_fault, during=reclaim_now))
                result = self.execute(lease, store=recording, processors=processors)
                self.assertResult(result, lease, "lease-lost")
                self.assertEqual(len(recording.calls), 1)
                self.assertEqual(contents(self.path), expected)
                later = self.job(lease.job_id)
                self.assertEqual((later.state, later.fence, later.history[-1].result), ("leased", lease.fence + 1, None))
                self.assertEqual(self.outcomes(record_id), ())

    def test_one_write_per_execution(self) -> None:
        cases = (
            ("revision", projection(number=1), "steps", "interpret-steps@1", {}, None),
            ("unmodeled", projection("distance", number=2), "distance", COMMON_VERSION, {}, None),
            ("policy-pending", projection(number=3, vector="steps-minimal"), "steps", "interpret-steps@1", {}, None),
            ("transient", projection(number=4), "steps", SYNTHETIC_VERSION, {"processors": transient()},
             ("transient", TRANSIENT)),
            ("deterministic", projection(number=5), "steps", "interpret-steps@9", {}, ("deterministic", "processor-unavailable")),
        )
        for name, payload, record_type, version, options, failure in cases:
            with self.subTest(name):
                lease = self.leased(payload, record_type, version)
                clock = self.clock_for(lease)
                control = self.control()
                recording = Delegating(self.store)
                self.execute(lease, clock, store=recording, **options)
                if failure is None:
                    entry = expected_entry(self.interpreted(lease, PROCESSORS[version]))
                    self.assertEqual(recording.calls, ["complete_processing_job"])
                    replayed = self.replay(control, lambda store: store.complete_processing_job(
                        lease.job_id, lease.fence, entry, clock.value))
                else:
                    kind, reason = failure
                    retry_at = clock.value + timedelta(seconds=60) if kind == "transient" else None
                    self.assertEqual(recording.calls, ["fail_processing_job"])
                    replayed = self.replay(control, lambda store: store.fail_processing_job(
                        lease.job_id, lease.fence, kind, reason, retry_at, clock.value))
                self.assertEqual(contents(self.path), replayed)
        # A fault inside the store's write transaction commits nothing, and no other write follows.
        for number, (name, table, version, options) in enumerate((
            ("revision-insert", "canonical_revisions", "interpret-steps@1", {}),
            ("failure-update", "processing_jobs", SYNTHETIC_VERSION, {"processors": transient()}),
        ), start=10):
            with self.subTest(name):
                lease = self.leased(projection(number=number), "steps", version)
                event = "INSERT" if table == "canonical_revisions" else "UPDATE"
                with self.trigger(f"BEFORE {event} ON {table}"):
                    recording = Delegating(self.store)
                    before = contents(self.path)
                    exception, text = raised(sqlite3.Error, lambda: self.execute(lease, store=recording, **options))
                    self.assertEqual(contents(self.path), before)
                self.assertEqual(len(recording.calls), 1)
                self.assertNotIn(HEALTH_MARKER, text + "".join(str(item) for item in chain(exception)))
                self.assertHeldOpen(lease)

    @contextmanager
    def trigger(self, when: str):
        """A fault inside the store's write transaction: a trigger that aborts with marker text."""
        def run(sql: str) -> None:
            connection = sqlite3.connect(self.path)
            try:
                connection.execute(sql)
                connection.commit()
            finally:
                connection.close()
        run(f"CREATE TRIGGER synthetic_fault {when} BEGIN SELECT RAISE(ABORT, '{HEALTH_MARKER}'); END")
        try:
            yield
        finally:
            run("DROP TRIGGER synthetic_fault")

    def test_lease_is_never_renewed(self) -> None:
        def forbidden(*args, **kwargs):
            raise Crash()
        for number, (version, options, seconds) in enumerate((
            ("interpret-steps@1", {}, -55),
            ("interpret-steps@1", {}, 3600),
            (SYNTHETIC_VERSION, {"processors": transient()}, 3600),
            ("interpret-steps@9", {}, 3600),
        ), start=1):
            with self.subTest(number=number):
                lease = self.leased(projection(number=number), "steps", version)
                recording = Delegating(self.store, before={"renew_processing_lease": forbidden})
                result = self.execute(lease, self.clock_for(lease, seconds=seconds), store=recording, **options)
                self.assertIn(result.disposition, ("outcome-recorded", "retry-scheduled", "job-failed"))
                self.assertNotIn("renew_processing_lease", recording.calls)
                self.assertEqual(len(recording.calls), 1)
                self.assertEqual(self.job(lease.job_id).history[-1].expires_at, lease.expires_at)


# ---------------------------------------------------------------- R10

class R10RepeatsAndCrashRecovery(StoreCase):
    def test_repeat_after_commit_is_already_recorded(self) -> None:
        for number, (version, options, disposition) in enumerate((
            ("interpret-steps@1", {}, "outcome-recorded"),
            (SYNTHETIC_VERSION, {"processors": transient()}, "retry-scheduled"),
            ("interpret-steps@9", {}, "job-failed"),
        ), start=1):
            with self.subTest(disposition):
                lease = self.leased(projection(number=number), "steps", version)
                self.assertEqual(self.execute(lease, **options).disposition, disposition)
                clock = self.clock_for(lease, seconds=30)
                recording = Delegating(self.store)
                result = self.assertWritesNothing(self.execute, lease, clock, store=recording, **options)
                self.assertResult(result, lease, "already-recorded")
                self.assertEqual((recording.calls, clock.calls), ([], 0))
                if disposition == "retry-scheduled":
                    # Still already-recorded for its own lease after a later claim takes the job.
                    again = self.store.claim_processing_job(OTHER_OWNER, LEASE, self.job(lease.job_id).not_before)
                    self.assertEqual((again.job_id, again.fence), (lease.job_id, lease.fence + 1))
                    result = self.assertWritesNothing(self.execute, lease, processors=transient())
                    self.assertResult(result, lease, "already-recorded")
        # The same lease completes concurrently between the read and the write: the refusal resolves from history.
        lease = self.leased(projection(number=11))
        expected: dict[str, object] = {}

        def complete_first(*args, **kwargs) -> None:
            self.assertEqual(self.execute(lease).disposition, "outcome-recorded")
            expected.update(contents(self.path))
        recording = Delegating(self.store, before={"complete_processing_job": complete_first})
        result = self.execute(lease, store=recording)
        self.assertResult(result, lease, "already-recorded")
        self.assertEqual(recording.calls, ["complete_processing_job"])
        self.assertEqual(contents(self.path), expected)
        self.assertEqual(len(self.outcomes(lease.record_id)), 1)
        # The same lease records its failure concurrently, on either failure path.
        for number, (version, options, disposition) in enumerate((
            (SYNTHETIC_VERSION, {"processors": transient()}, "retry-scheduled"),
            ("interpret-steps@9", {}, "job-failed"),
        ), start=12):
            with self.subTest(f"concurrent-{disposition}"):
                lease = self.leased(projection(number=number), "steps", version)
                closed: dict[str, object] = {}

                def fail_first(*args, lease=lease, options=options, disposition=disposition, closed=closed, **kwargs) -> None:
                    self.assertEqual(self.execute(lease, **options).disposition, disposition)
                    closed.update(contents(self.path))
                recording = Delegating(self.store, before={"fail_processing_job": fail_first})
                result = self.execute(lease, store=recording, **options)
                self.assertResult(result, lease, "already-recorded")
                self.assertEqual(recording.calls, ["fail_processing_job"])
                self.assertEqual(contents(self.path), closed)

    def test_repeat_after_reclaim_is_lease_lost(self) -> None:
        lease = self.leased(projection())
        later = self.reclaim(lease)
        self.assertResult(self.assertWritesNothing(self.execute, lease), lease, "lease-lost")
        self.assertEqual(self.execute(later).disposition, "outcome-recorded")
        # The old lease stays lost after the later lease completes.
        clock = self.clock_for(lease)
        self.assertResult(self.assertWritesNothing(self.execute, lease, clock), lease, "lease-lost")
        self.assertEqual(clock.calls, 0)
        # And the later lease's repeat is already recorded.
        self.assertResult(self.assertWritesNothing(self.execute, later), later, "already-recorded")
        self.assertEqual(len(self.outcomes(lease.record_id)), 1)
        # A claim can close an expired attempt without taking the job, leaving the fence unchanged:
        # when the job has used its attempts, or when its record was retracted meanwhile.
        for number, (name, max_attempts, state) in enumerate((
            ("exhausted", 1, "failed"),
            ("superseded", 3, "superseded"),
        ), start=2):
            with self.subTest(name):
                lease = self.leased(projection(number=number), max_attempts=max_attempts)
                if name == "superseded":
                    self.retract(lease.record_id)
                self.assertIsNone(self.store.claim_processing_job(OTHER_OWNER, LEASE, lease.expires_at + timedelta(seconds=1)))
                job = self.job(lease.job_id)
                self.assertEqual((job.state, job.fence, job.history[-1].result), (state, lease.fence, "lease-expired"))
                clock = self.clock_for(lease)
                recording = Delegating(self.store)
                result = self.assertWritesNothing(self.execute, lease, clock, store=recording)
                self.assertResult(result, lease, "lease-lost")
                self.assertEqual((recording.calls, clock.calls), ([], 0))

    def test_crash_before_write_recovers_by_reclaim(self) -> None:
        for number, (write, version, options) in enumerate((
            ("complete_processing_job", "interpret-steps@1", {}),
            ("fail_processing_job", "interpret-steps@9", {}),
        ), start=1):
            with self.subTest(write):
                lease = self.leased(projection(number=number), "steps", version)
                before = contents(self.path)
                with self.assertRaises(Crash):
                    self.execute(lease, store=Delegating(self.store, before={write: raiser(Crash())}), **options)
                self.assertEqual(contents(self.path), before)
                self.assertHeldOpen(lease)
                later = self.reclaim(lease)
                result = self.execute(later, **options)
                self.assertIn(result.disposition, ("outcome-recorded", "job-failed"))
                job = self.job(lease.job_id)
                self.assertEqual([item.result for item in job.history],
                                 ["lease-expired", "completed" if write == "complete_processing_job" else "deterministic-failure"])
                self.assertLessEqual(len(self.outcomes(lease.record_id)), 1)
                self.assertResult(self.assertWritesNothing(self.execute, lease), lease, "lease-lost")

    def test_crash_after_write_needs_no_recovery(self) -> None:
        for number, (write, version, options, state) in enumerate((
            ("complete_processing_job", "interpret-steps@1", {}, "completed"),
            ("fail_processing_job", "interpret-steps@9", {}, "failed"),
            ("fail_processing_job", SYNTHETIC_VERSION, {"processors": transient()}, "queued"),
        ), start=1):
            with self.subTest(state):
                lease = self.leased(projection(number=number), "steps", version)
                with self.assertRaises(Crash):
                    self.execute(lease, store=Delegating(self.store, after={write: raiser(Crash())}), **options)
                job = self.job(lease.job_id)
                self.assertEqual((job.state, job.fence), (state, lease.fence))
                self.assertIsNotNone(job.history[-1].result)
                # The process restarts: the uncertain exit resolves from the durable record and redoes nothing.
                clock = self.clock_for(lease, seconds=30)
                recording = Delegating(self.store)
                result = self.assertWritesNothing(self.execute, lease, clock, store=recording, **options)
                self.assertResult(result, lease, "already-recorded")
                self.assertEqual((recording.calls, clock.calls), ([], 0))
                self.assertEqual(len(self.outcomes(lease.record_id)), 1 if state == "completed" else 0)
                if state != "queued":
                    self.assertIsNone(self.store.claim_processing_job(OTHER_OWNER, LEASE, at(100_000)))
                else:
                    self.assertEqual(self.job(lease.job_id).not_before, clock.value - timedelta(seconds=25))


# ---------------------------------------------------------------- R11

class FakeWriter:
    """A writer whose run executes the operation over a real store, recording each run."""

    def __init__(self, store) -> None:
        self.store = Delegating(store, before={name: self._inside for name in WRITES})
        self.runs = 0
        self.running = False
        self.returned: object = None

    def _inside(self, *args, **kwargs) -> None:
        assert self.running, "a write outside writer.run"

    def run(self, operation):
        self.runs += 1
        self.running = True
        try:
            self.returned = operation(self.store)
            return self.returned
        finally:
            self.running = False


class R11Policies(StoreCase):
    def test_production_handler_uses_no_policy(self) -> None:
        offsetless = self.leased(projection(number=1, vector="steps-minimal"))
        writer = FakeWriter(self.store)
        clock = self.clock_for(offsetless)
        result = make_job_handler(writer, clock=clock)(offsetless)
        self.assertResult(result, offsetless, "outcome-recorded", "policy-pending", "fallback-timezone-policy-missing")
        (outcome,) = self.outcomes(offsetless.record_id)
        self.assertEqual((outcome.policy_identity, outcome.policy_kind), ("none", "fallback-timezone"))
        # A fully experienced projection has the identity none under the production handler too.
        experienced = self.leased(projection(number=2))
        make_job_handler(writer, clock=self.clock_for(experienced))(experienced)
        (outcome,) = self.outcomes(experienced.record_id)
        self.assertEqual((outcome.kind, outcome.policy_identity), ("revision", "none"))

    def test_supplied_policies_reach_the_outcome_key(self) -> None:
        lease = self.leased(projection(vector="steps-minimal"))
        policy_a = (FallbackTimezonePolicy("synthetic-policy-a", None, "Asia/Kolkata"),)
        policy_b = (FallbackTimezonePolicy("synthetic-policy-b", None, "America/Los_Angeles"),)
        identities = []
        for policies in (policy_a, policy_b, ()):
            result = self.execute(lease, policies=policies)
            self.assertEqual(result.disposition, "outcome-recorded")
            identities.append(self.outcomes(lease.record_id)[-1].policy_identity)
            lease = self.claim(lease.record_id, lease.selection_id, "interpret-steps@1")
        self.assertEqual(identities, [
            "fallback-timezone:" + json.dumps([["synthetic-policy-a"], tzdata.IANA_VERSION], separators=(",", ":")),
            "fallback-timezone:" + json.dumps([["synthetic-policy-b"], tzdata.IANA_VERSION], separators=(",", ":")),
            "none",
        ])
        outcomes = self.outcomes(lease.record_id)
        self.assertEqual([item.kind for item in outcomes], ["revision", "revision", "policy-pending"])
        self.assertEqual({item.selection_id for item in outcomes}, {lease.selection_id})
        self.assertEqual([stored_entry(item) for item in outcomes[:2]], [
            expected_entry(self.interpreted(lease, interpret_steps.PROCESSOR, policy_a)),
            expected_entry(self.interpreted(lease, interpret_steps.PROCESSOR, policy_b)),
        ])
        self.assertEqual([item.revision.start_attribution for item in outcomes[:2]], ["inferred", "inferred"])
        # The same timeline again returns the first outcome instead of appending or conflicting.
        self.assertEqual(self.execute(lease, policies=policy_a).disposition, "outcome-recorded")
        self.assertEqual(self.outcomes(lease.record_id), outcomes)

    def test_offsetless_projection_is_policy_pending(self) -> None:
        for number, (vector, record_type) in enumerate((("steps-minimal", "steps"), ("weight-minimal", "weight"),
                                                        ("heart-rate-minimal", "heart-rate"),
                                                        ("sleep-session-minimal", "sleep-session")), start=1):
            with self.subTest(vector):
                lease = self.leased(projection(record_type, number=number, vector=vector), record_type)
                result = self.execute(lease)
                self.assertResult(result, lease, "outcome-recorded", "policy-pending", "fallback-timezone-policy-missing")
                (outcome,) = self.outcomes(lease.record_id)
                self.assertEqual(stored_entry(outcome), OutcomeEntry(
                    "policy-pending", lease.processor_version, "none", "fallback-timezone-policy-missing", None,
                    "fallback-timezone", None))
        # One endpoint with an offset is not enough.
        lease = self.leased(projection(number=9, drop=("end_zone_offset",)))
        self.assertEqual(self.execute(lease).outcome_kind, "policy-pending")


# ---------------------------------------------------------------- R12

class R12WorkerSeam(StoreCase):
    def test_handler_executes_through_the_writer(self) -> None:
        lease = self.leased(projection(number=1))
        writer = FakeWriter(self.store)
        clock = self.clock_for(lease)
        before = contents(self.path)
        handler = make_job_handler(writer, clock=clock)
        self.assertEqual(contents(self.path), before, "building the handler changed the database")
        self.assertTrue(callable(handler))
        self.assertEqual((writer.runs, clock.calls), (0, 0))
        result = handler(lease)
        self.assertIs(result, writer.returned)
        self.assertEqual((writer.runs, clock.calls), (1, 1))
        self.assertResult(result, lease, "outcome-recorded", "revision")
        (outcome,) = self.outcomes(lease.record_id)
        self.assertEqual(outcome.recorded_at, clock.value)
        self.assertEqual(writer.store.calls, ["complete_processing_job"])
        # The default clock is the timezone-aware wall clock.
        lease = self.leased(projection(number=2))
        before = datetime.now(UTC)
        result = make_job_handler(writer)(lease)
        after = datetime.now(UTC)
        self.assertEqual(result.disposition, "outcome-recorded")
        (outcome,) = self.outcomes(lease.record_id)
        self.assertTrue(before <= outcome.recorded_at <= after)

    def test_worker_drives_archived_batches_to_terminal_outcomes(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "portal.db"
        writer = ingress.PortalWriter(path, reserve_bytes=0, role="worker")
        self.addCleanup(writer.close)
        now = datetime(2026, 11, 25, tzinfo=UTC)
        elapsed = [0.0]
        clock = lambda: now + timedelta(seconds=elapsed[0])

        def prepare(store) -> None:
            offer = digest(b"synthetic-offer")
            store.record_enrollment_offer(offer, now + timedelta(hours=1), now)
            store.enroll_installation(offer, INSTALLATION, digest(b"synthetic-fingerprint"), now)
            store.activate_installation(INSTALLATION, now)
            store.honor_generation(1, b'{"synthetic-advertisement":1}', now)
        writer.run(prepare)
        record_types = ("steps", "heart-rate", "sleep-session", "weight", "distance")
        for sequence, record_type in enumerate(record_types, start=1):
            envelope = b"synthetic-envelope-%d" % sequence
            header = MessageHeader(INSTALLATION, sequence, digest(envelope), 1, now, envelope, b"synthetic-receipt")
            batch = IngestBatchRecord(record_type, 1, "initial-snapshot", CYCLE, sequence, 1, "0" * 64,
                                      (BatchItem(projection=projection(record_type, number=sequence)),))
            writer.run(lambda store, header=header, batch=batch: store.archive_ingest_batch(header, batch))
        settings = worker_module.WorkerSettings(path, 0, processor_versions=PROCESSOR_VERSIONS)
        loop = worker_module.Worker(writer, settings, handler=make_job_handler(writer, clock=clock), clock=clock,
                                    monotonic=lambda: elapsed[0], sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
        with self.assertNoLogs(worker_module.LOG, level="WARNING"):
            loop.run(max_cycles=3)
        with writer.store.read() as view:
            for number, record_type in enumerate(record_types, start=1):
                with self.subTest(record_type):
                    key = SourceKey("health-connect", ORIGIN, record_type, f"{IDENTITY_MARKER}-{number}")
                    record_id = view.source_record_id(key)
                    self.assertIsNotNone(record_id)
                    (job,) = view.processing_jobs(record_id)
                    self.assertEqual((job.state, job.processor_version), ("completed", PROCESSOR_VERSIONS[record_type]))
                    outcome = view.latest_outcome(record_id, PROCESSOR_VERSIONS[record_type])
                    self.assertEqual(outcome.kind, "unmodeled" if record_type == "distance" else "revision")
            self.assertEqual(view.selections_awaiting_processing(PROCESSOR_VERSIONS, 100), ())

    def test_handler_passes_store_retryables_to_the_worker(self) -> None:
        lease = self.leased(projection(number=1))
        injected = StoreRetryable("store-busy")
        writer = FakeWriter(self.store)
        writer.store = Delegating(self.store, before={"complete_processing_job": raiser(injected)})
        before = contents(self.path)
        with self.assertRaises(StoreRetryable) as caught:
            make_job_handler(writer, clock=self.clock_for(lease))(lease)
        self.assertIs(caught.exception, injected)
        self.assertEqual(contents(self.path), before)
        # Inside the worker, the retryable is logged by name and the worker backs off.
        self.deliver(projection(number=2))
        waits: list[float] = []
        settings = worker_module.WorkerSettings(self.path, 0, processor_versions={"steps": "interpret-steps@1"},
                                                claim_batch_size=1)
        worker_writer = SimpleWriter(self.store, {"complete_processing_job": raiser(StoreRetryable("store-busy"))})
        before = contents(self.path)
        handler = make_job_handler(worker_writer, clock=lambda: at(500))
        self.assertEqual(contents(self.path), before, "building the handler changed the database")
        unchanged: list[bool] = []

        def checked(lease: Lease):
            # Discovery and claims may write; each handler call itself must write nothing.
            before = contents(self.path)
            try:
                return handler(lease)
            finally:
                unchanged.append(contents(self.path) == before)
        loop = worker_module.Worker(worker_writer, settings, handler=checked,
                                    clock=lambda: at(500), monotonic=lambda: 0.0, sleep=waits.append)
        with self.assertLogs(worker_module.LOG, level="WARNING") as logs:
            loop.run(max_cycles=2)
        self.assertTrue(unchanged, "the worker called no handler")
        self.assertTrue(all(unchanged), "a handler call that raised a retryable changed the database")
        self.assertIn("WARNING:activity_tracker_portal.worker:worker handler retryable: store-busy", logs.output)
        self.assertEqual(waits[:1], [settings.idle_seconds])
        for marker in MARKERS:
            self.assertNotIn(marker, "\n".join(logs.output))
        self.assertTrue(all(record.exc_info is None for record in logs.records))


class SimpleWriter:
    """A synchronous writer over one real store, as the worker sees it: read through .store, write through .run."""

    def __init__(self, store, before: dict[str, Callable]) -> None:
        self.store = store
        self._delegating = Delegating(store, before=before)

    def run(self, operation):
        return operation(self._delegating)


# ---------------------------------------------------------------- R13

class R13Time(StoreCase):
    def test_time_comes_only_from_the_clock(self) -> None:
        # A non-UTC aware clock is the instant it names.
        lease = self.leased(projection(number=1))
        clock = Clock((lease.expires_at - timedelta(seconds=50)).astimezone(IST))
        self.execute(lease, clock)
        (outcome,) = self.outcomes(lease.record_id)
        job = self.job(lease.job_id)
        self.assertEqual(clock.calls, 1)
        self.assertEqual((outcome.recorded_at, job.closed_at, job.history[-1].finished_at), (clock.value,) * 3)
        # A refused completion and its deterministic failure share the one time.
        lease = self.leased(projection(number=2))
        clock = self.clock_for(lease)
        self.execute(lease, clock, store=Delegating(self.store, before={
            "complete_processing_job": raiser(StoreRefusal("outcome-conflict"))}))
        job = self.job(lease.job_id)
        self.assertEqual(clock.calls, 1)
        self.assertEqual((job.closed_at, job.history[-1].finished_at), (clock.value, clock.value))
        # A transient failure's retry time is computed from the same reading.
        record_id = self.deliver(projection(number=3))
        lease = self.claim(record_id, self.select(record_id), SYNTHETIC_VERSION)
        clock = self.clock_for(lease)
        result = self.execute(lease, clock, processors=transient())
        self.assertEqual(clock.calls, 1)
        self.assertEqual((result.retry_at, self.job(lease.job_id).history[-1].finished_at),
                         (clock.value + timedelta(seconds=60), clock.value))
        # Executions that return before any write attempt read no time.
        clock = self.clock_for(lease)
        result = self.assertWritesNothing(self.execute, lease, clock, processors=transient())
        self.assertEqual(result.disposition, "already-recorded")
        self.assertEqual(clock.calls, 0)

    def test_naive_clock_is_refused(self) -> None:
        for number, (version, options) in enumerate((
            ("interpret-steps@1", {}),
            (SYNTHETIC_VERSION, {"processors": transient()}),
            ("interpret-steps@9", {}),
        ), start=1):
            with self.subTest(version):
                record_id = self.deliver(projection(number=number))
                lease = self.claim(record_id, self.select(record_id), version)
                clock = Clock(NAIVE)
                before = contents(self.path)
                # The refusal may come from the store's write or from the unit's own check just before it.
                with self.assertRaises(StoreRefusal) as caught:
                    self.execute(lease, clock, **options)
                self.assertEqual(caught.exception.failure, "timestamp-invalid")
                self.assertIsNone(caught.exception.__cause__)
                self.assertEqual(clock.calls, 1)
                self.assertEqual(contents(self.path), before)
                self.assertHeldOpen(lease)


# ---------------------------------------------------------------- R14

class R14HealthDataFree(StoreCase):
    def scenarios(self) -> list[tuple[Lease, ExecutionResult]]:
        """One execution of every disposition, over projections carrying every marker."""
        found = []
        lease = self.leased(projection("sleep-session", number=1, notes=HEALTH_MARKER, title=HEALTH_MARKER), "sleep-session")
        found.append((lease, self.execute(lease)))
        found.append((lease, self.assertWritesNothing(self.execute, lease)))
        record_id = self.deliver(projection(number=2))
        lease = self.claim(record_id, self.select(record_id), SYNTHETIC_VERSION, max_attempts=1)
        found.append((lease, self.execute(lease, processors=registry(SyntheticProcessor(body_fault=True)))))
        record_id = self.deliver(projection(number=3))
        lease = self.claim(record_id, self.select(record_id), SYNTHETIC_VERSION, max_attempts=2)
        first = self.execute(lease, processors=transient())
        found.append((lease, first))
        lease = self.store.claim_processing_job(OWNER, LEASE, first.retry_at)
        found.append((lease, self.execute(lease, processors=transient())))
        lease = self.leased(projection(number=4))
        self.reclaim(lease)
        found.append((lease, self.assertWritesNothing(self.execute, lease)))
        for number, reason in enumerate(("processor-unavailable", "processor-record-type-mismatch"), start=5):
            payload = projection("weight", number=number)
            lease = self.leased(payload, "weight", "interpret-weight@9" if reason == "processor-unavailable" else "interpret-steps@1")
            found.append((lease, self.execute(lease)))
        return found

    def test_results_are_health_data_free(self) -> None:
        self.assertEqual([item.name for item in dataclasses.fields(ExecutionResult)],
                         ["job_id", "fence", "disposition", "outcome_kind", "reason", "retry_at"])
        results = self.scenarios()
        self.assertEqual({result.disposition for _, result in results}, set(DISPOSITIONS))
        for lease, result in results:
            with self.subTest(result.disposition, reason=result.reason):
                self.assertIn(result.disposition, DISPOSITIONS)
                self.assertTrue(result.reason is None or result.reason in (*DETERMINISTIC, TRANSIENT, *STORE_REASONS)
                                or result.outcome_kind is not None)
                if result.disposition in ("already-recorded", "lease-lost"):
                    self.assertEqual((result.outcome_kind, result.reason, result.retry_at), (None, None, None))
                for text in (repr(result), str(result)):
                    for marker in MARKERS:
                        self.assertNotIn(marker, text)
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    result.reason = HEALTH_MARKER  # type: ignore[misc]

    def test_recorded_reasons_are_allowlisted(self) -> None:
        self.scenarios()
        lease = self.leased(projection(number=20))
        self.execute(lease, store=Delegating(self.store, before={
            "complete_processing_job": raiser(StoreRefusal("outcome-invalid"))}))
        connection = sqlite3.connect(self.path)
        try:
            reasons = {row[0] for row in connection.execute("SELECT reason FROM processing_attempts WHERE reason IS NOT NULL")}
            failures = {row[0] for row in connection.execute("SELECT failure FROM processing_jobs WHERE failure IS NOT NULL")}
            rows = [row for table in ("processing_jobs", "processing_attempts")
                    for row in connection.execute(f'SELECT * FROM "{table}"')]
        finally:
            connection.close()
        self.assertLessEqual(reasons, {*DETERMINISTIC, TRANSIENT})
        self.assertLessEqual(failures, {*DETERMINISTIC, *STORE_REASONS})
        self.assertTrue({"processor-invariant-violated", "processor-unavailable", "processor-record-type-mismatch",
                         "outcome-invalid", TRANSIENT} <= reasons)
        for row in rows:
            for value in row:
                for marker in MARKERS:
                    self.assertNotIn(marker, str(value))

    def test_raised_failures_carry_no_health_data(self) -> None:
        def assertClean(error: BaseException, text: str) -> None:
            self.assertIsNotNone(error.__traceback__)
            self.assertIsNone(error.__cause__)
            for item in chain(error):
                text += str(item) + repr(item) + repr(item.args)
            for marker in MARKERS:
                self.assertNotIn(marker, text)
        cases = []
        # A retryable after a processor exception carrying the marker.
        record_id = self.deliver(projection(number=1))
        lease = self.claim(record_id, self.select(record_id), SYNTHETIC_VERSION)
        cases.append((StoreRetryable, "store-busy", lambda: self.execute(lease, store=Delegating(self.store, before={
            "fail_processing_job": raiser(StoreRetryable("store-busy"))}), processors=transient())))
        # A retryable after a processor-invariant failure.
        record_id = self.deliver(projection(number=2))
        lease2 = self.claim(record_id, self.select(record_id), SYNTHETIC_VERSION)
        cases.append((StoreRetryable, "storage-reserve-exhausted", lambda: self.execute(lease2, store=Delegating(
            self.store, before={"fail_processing_job": raiser(StoreRetryable("storage-reserve-exhausted"))}),
            processors=registry(SyntheticProcessor(body_fault=True)))))
        # A naive clock after a transient processor exception.
        record_id = self.deliver(projection(number=3))
        lease3 = self.claim(record_id, self.select(record_id), SYNTHETIC_VERSION)
        cases.append((StoreRefusal, "timestamp-invalid", lambda: self.execute(lease3, Clock(NAIVE), processors=transient())))
        # An unknown job.
        lease4 = self.leased(projection(number=4))
        cases.append((StoreRefusal, "processing-job-unknown", lambda: self.execute(replace(lease4, job_id=lease4.job_id + 1000))))
        for error, failure, operation in cases:
            with self.subTest(failure):
                before = contents(self.path)
                exception, text = raised(error, operation)
                self.assertEqual(exception.failure, failure)
                self.assertEqual(exception.args, (failure,))
                self.assertEqual(contents(self.path), before)
                assertClean(exception, text)

    def test_unit_emits_no_logs(self) -> None:
        with self.assertNoLogs(level=logging.DEBUG):
            results = self.scenarios()
            lease = self.leased(projection(number=30))
            before = contents(self.path)
            with self.assertRaises(StoreRetryable):
                self.execute(lease, store=Delegating(self.store, before={
                    "complete_processing_job": raiser(StoreRetryable("store-busy"))}))
            self.assertEqual(contents(self.path), before)
            make_job_handler(FakeWriter(self.store), clock=self.clock_for(lease))(lease)
        self.assertEqual(len(results), 8)


if __name__ == "__main__":
    unittest.main()
