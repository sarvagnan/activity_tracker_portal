"""Operational Store (operational-store@1): ingest-side transactional invariants.

The store is protocol-agnostic. Callers decode and evaluate Wire Protocol
messages and pass explicit archival records with the exact envelope and
receipt bytes; the store enforces the invariants that must hold however its
callers behave, inside one SQLite transaction per operation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
import os
import re
from os import PathLike
import shutil
import sqlite3
import time
from typing import Literal, cast

from ._operational_store_sql import _BASELINE_SQL, _PROCESSING_SQL, _SOURCE_INDEX_SQL

STARTUP_FAILURES = frozenset(
    {
        "sqlite-runtime-unsupported",
        "store-migration-required",
        "store-schema-unsupported",
        "store-migration-checksum-mismatch",
    }
)
RETRYABLE_FAILURES = frozenset({"storage-reserve-exhausted", "store-busy"})
INVARIANT_FAILURES = frozenset(
    {
        "timestamp-invalid",
        "enrollment-code-unknown",
        "enrollment-code-expired",
        "enrollment-code-consumed",
        "enrollment-code-exists",
        "installation-exists",
        "installation-unknown",
        "installation-transition-invalid",
        "active-installation-exists",
        "installation-not-delivering",
        "message-digest-mismatch",
        "sequence-content-conflict",
        "sequence-out-of-order",
        "generation-unknown",
        "generation-not-monotonic",
        "generation-still-required",
        "cycle-already-completed",
        "archived-item-unknown",
        "index-entry-mismatch",
        "index-entry-conflict",
        "source-key-invalid",
        "source-record-unknown",
        "selection-evidence-invalid",
        "selection-stale",
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
    }
)
FAILURES = STARTUP_FAILURES | RETRYABLE_FAILURES | INVARIANT_FAILURES

# Ordered (version, exact SQL) pairs; store_migrations records the SHA-256 of each
# migration's UTF-8 SQL. The baseline is migration 1.
MIGRATIONS: tuple[tuple[int, str], ...] = ((1, _BASELINE_SQL), (2, _SOURCE_INDEX_SQL), (3, _PROCESSING_SQL))

InstallationState = Literal["pending", "active", "draining", "retired", "withdrawn"]
RetirementKind = Literal["drained", "destructive"]
MessageKind = Literal[
    "ingest-batch",
    "snapshot-completion",
    "change-completion",
    "sync-checkpoint",
    "capability-acknowledgement",
]


class StoreRefusal(Exception):
    """A named startup or invariant refusal that changed nothing."""

    def __init__(self, failure: str) -> None:
        super().__init__(failure)
        self.failure = failure


class StoreRetryable(Exception):
    """A named retryable failure that changed nothing."""

    def __init__(self, failure: str) -> None:
        super().__init__(failure)
        self.failure = failure


@dataclass(frozen=True)
class MessageHeader:
    """Identity and exact bytes common to every accepted Outbox Message."""

    installation_id: str
    sequence: int
    message_sha256: str
    staged_generation: int
    receipt_time: datetime
    envelope: bytes
    receipt: bytes


@dataclass(frozen=True)
class BatchItem:
    """Exactly one of an exact Source Projection or a retracted Source Identity."""

    projection: bytes | None = None
    retracted_identity: str | None = None

    def __post_init__(self) -> None:
        if (self.projection is None) == (self.retracted_identity is None):
            raise ValueError("a batch item carries exactly one of projection or retracted_identity")


@dataclass(frozen=True)
class IngestBatchRecord:
    record_type: str
    projection_schema_version: int
    cycle_kind: Literal["initial-snapshot", "reconciliation-snapshot", "change-cycle"]
    cycle_id: str
    source_page_index: int
    fragment_index: int
    previous_chain: str
    items: tuple[BatchItem, ...]


@dataclass(frozen=True)
class CompletionRecord:
    """A Snapshot Completion (with its snapshot kind) or a Change Completion."""

    completion_kind: Literal["snapshot", "change"]
    snapshot_kind: Literal["initial", "reconciliation"] | None
    record_type: str
    cycle_id: str
    page_count: int
    fragment_count: int
    record_count: int
    final_chain: str

    def __post_init__(self) -> None:
        if (self.completion_kind == "snapshot") != (self.snapshot_kind is not None):
            raise ValueError("a snapshot completion carries a snapshot kind and a change completion does not")


@dataclass(frozen=True)
class RecordTypeOutcome:
    record_type: str
    outcome: str
    reason: str | None
    source_data_staged: bool


@dataclass(frozen=True)
class SyncCheckpointRecord:
    """Health-data-free Polling Run report; Sender times are diagnostic claims."""

    run_sequence: int
    trigger: str
    started_at: str
    ended_at: str
    elapsed_ms: int | None
    background_scheduling: str
    record_types: tuple[RecordTypeOutcome, ...]
    outbox_messages_before_checkpoint: int
    outbox_bytes_before_checkpoint: int


@dataclass(frozen=True)
class InstallationStatus:
    installation_id: str
    fingerprint: str
    state: InstallationState
    enrolled_at: datetime
    activated_at: datetime | None
    draining_at: datetime | None
    retired_at: datetime | None
    retirement_kind: RetirementKind | None
    withdrawn_at: datetime | None
    highest_sequence: int | None
    last_delivery_at: datetime | None
    latest_checkpoint_receipt_at: datetime | None
    background_scheduling: str | None
    background_scheduling_receipt_at: datetime | None


@dataclass(frozen=True)
class LifecycleEvent:
    state: InstallationState
    at: datetime
    retirement_kind: RetirementKind | None


@dataclass(frozen=True)
class ArchivedMessage:
    """An archived Outbox Message; exactly one kind-specific record is set for its kind."""

    sequence: int
    kind: MessageKind
    message_sha256: str
    staged_generation: int
    receipt_time: datetime
    envelope: bytes
    receipt: bytes
    batch: IngestBatchRecord | None
    completion: CompletionRecord | None
    checkpoint: SyncCheckpointRecord | None


@dataclass(frozen=True)
class SequenceGap:
    gap_after: int
    sequence: int
    detected_at: datetime


@dataclass(frozen=True)
class DeliveryCycle:
    """One installation's delivery cycle, derived from its archived fragments and completion."""

    installation_id: str
    cycle_id: str
    record_type: str
    cycle_kind: Literal["initial-snapshot", "reconciliation-snapshot", "change-cycle"] | None
    first_sequence: int | None
    last_sequence: int | None
    last_source_page_index: int
    last_fragment_index: int
    last_previous_chain: str | None
    last_message_sha256: str | None
    fragment_count: int
    record_count: int
    completion_sequence: int | None


@dataclass(frozen=True)
class DeliveryBlockRecord:
    installation_id: str | None
    sequence: int | None
    message_sha256: str
    failure: str
    first_seen_at: datetime
    last_seen_at: datetime
    count: int


@dataclass(frozen=True)
class ArchivedCheckpoint:
    sequence: int
    receipt_time: datetime
    record: SyncCheckpointRecord


@dataclass(frozen=True)
class HonoredGeneration:
    generation: int
    advertisement: bytes
    honored_at: datetime


@dataclass(frozen=True)
class ReserveStatus:
    free_bytes: int
    reserve_bytes: int
    admitting: bool


@dataclass(frozen=True)
class StoreSettings:
    journal_mode: str
    synchronous: int
    foreign_keys: bool
    busy_timeout_ms: int
    sqlite_version: str
    schema_migrations: tuple[tuple[int, str], ...]


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StoreRefusal("timestamp-invalid")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _optional_datetime(value: str | None) -> datetime | None:
    return None if value is None else _datetime(value)


def _execute(
    connection: sqlite3.Connection, sql: str, parameters: tuple[object, ...] = ()
) -> sqlite3.Cursor:
    try:
        return connection.execute(sql, parameters)
    except sqlite3.Error as error:
        if getattr(error, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            failure: Exception = StoreRetryable("store-busy")
        else:
            # SQLite messages, including trigger failures, can contain health data.
            failure = type(error)("store-database-error")
    # Raising outside the handler also keeps the original out of __context__.
    raise failure from None


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    try:
        _execute(connection, "BEGIN IMMEDIATE")
        yield
        _execute(connection, "COMMIT")
    except BaseException:
        # RAISE(ABORT) rolls back a statement, not the earlier transaction writes.
        if connection.in_transaction:
            connection.rollback()
        raise


def _connect(path: str | PathLike[str], busy_timeout_ms: int) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None, timeout=busy_timeout_ms / 1000)
    connection.row_factory = sqlite3.Row
    return connection


def _configure(connection: sqlite3.Connection, busy_timeout_ms: int) -> None:
    _execute(connection, f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    _execute(connection, "PRAGMA synchronous = FULL")
    _execute(connection, "PRAGMA foreign_keys = ON")


def _enable_wal(connection: sqlite3.Connection, busy_timeout_ms: int) -> None:
    # Journal-mode changes can bypass SQLite's busy handler or return the old mode.
    deadline = time.monotonic() + busy_timeout_ms / 1000
    while True:
        try:
            if _execute(connection, "PRAGMA journal_mode = WAL").fetchone()[0] == "wal":
                return
        except StoreRetryable:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StoreRetryable("store-busy") from None
        time.sleep(min(0.01, remaining))


def _migration_checksums() -> tuple[tuple[int, str], ...]:
    return tuple((version, hashlib.sha256(sql.encode("utf-8")).hexdigest()) for version, sql in MIGRATIONS)


def _validate_schema(connection: sqlite3.Connection, path: str | PathLike[str]) -> bool:
    """Return whether a schema exists, without changing persistent settings."""
    schema_error = False
    try:
        objects = _execute(connection, "SELECT name FROM sqlite_schema").fetchall()
        user_version = _execute(connection, "PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError:
        schema_error = True
    if schema_error:
        raise StoreRefusal("store-schema-unsupported") from None
    if not objects and user_version == 0:
        # SQLite can report short foreign files as empty databases.
        with open(path, "rb") as database:
            header = database.read(16)
        if header and header != b"SQLite format 3\x00":
            raise StoreRefusal("store-schema-unsupported")
        return False
    if not any(row[0] == "store_migrations" for row in objects):
        raise StoreRefusal("store-schema-unsupported")
    try:
        rows = _execute(connection, "SELECT version, checksum FROM store_migrations ORDER BY version").fetchall()
    except sqlite3.DatabaseError:
        raise StoreRefusal("store-schema-unsupported") from None
    recorded = tuple((row[0], row[1]) for row in rows)
    known = _migration_checksums()
    if tuple(version for version, _ in recorded) != tuple(version for version, _ in known[:len(recorded)]):
        raise StoreRefusal("store-schema-unsupported")
    if user_version != (recorded[-1][0] if recorded else 0):
        raise StoreRefusal("store-schema-unsupported")
    if recorded != known[:len(recorded)]:
        raise StoreRefusal("store-migration-checksum-mismatch")
    if len(recorded) < len(known):
        raise StoreRefusal("store-migration-required")
    return True


def _settings(connection: sqlite3.Connection) -> StoreSettings:
    return StoreSettings(
        journal_mode=_execute(connection, "PRAGMA journal_mode").fetchone()[0],
        synchronous=_execute(connection, "PRAGMA synchronous").fetchone()[0],
        foreign_keys=bool(_execute(connection, "PRAGMA foreign_keys").fetchone()[0]),
        busy_timeout_ms=_execute(connection, "PRAGMA busy_timeout").fetchone()[0],
        sqlite_version=_execute(connection, "SELECT sqlite_version()").fetchone()[0],
        schema_migrations=tuple(
            (row[0], row[1])
            for row in _execute(connection, "SELECT version, checksum FROM store_migrations ORDER BY version")
        ),
    )


def _installation(connection: sqlite3.Connection, installation_id: str) -> sqlite3.Row:
    row = _execute(connection, "SELECT * FROM installations WHERE installation_id = ?", (installation_id,)).fetchone()
    if row is None:
        raise StoreRefusal("installation-unknown")
    return row


def _installation_status(row: sqlite3.Row) -> InstallationStatus:
    return InstallationStatus(
        installation_id=row["installation_id"],
        fingerprint=row["fingerprint"],
        state=cast(InstallationState, row["state"]),
        enrolled_at=_datetime(row["enrolled_at"]),
        activated_at=_optional_datetime(row["activated_at"]),
        draining_at=_optional_datetime(row["draining_at"]),
        retired_at=_optional_datetime(row["retired_at"]),
        retirement_kind=cast(RetirementKind | None, row["retirement_kind"]),
        withdrawn_at=_optional_datetime(row["withdrawn_at"]),
        highest_sequence=row["highest_sequence"],
        last_delivery_at=_optional_datetime(row["last_delivery_at"]),
        latest_checkpoint_receipt_at=_optional_datetime(row["latest_checkpoint_receipt_at"]),
        background_scheduling=row["background_scheduling"],
        background_scheduling_receipt_at=_optional_datetime(row["background_scheduling_receipt_at"]),
    )


def _latest_acknowledged_generation(connection: sqlite3.Connection) -> int | None:
    return _execute(
        connection, "SELECT max(staged_generation) FROM archived_messages WHERE kind = 'capability-acknowledgement'"
    ).fetchone()[0]


def _free_disk_bytes(path: str | PathLike[str]) -> int:
    return shutil.disk_usage(os.path.dirname(os.path.abspath(path))).free


def _sample_free_bytes(probe: Callable[[str | PathLike[str]], int], path: str | PathLike[str]) -> int:
    try:
        return probe(path)
    except Exception as error:
        if isinstance(error, OSError):
            failure: Exception = OSError("store-capacity-unavailable")
        else:
            failure = RuntimeError("store-capacity-unavailable")
    raise failure from None


# operational-store@3: the Source Record index (packs/operational-store-3.spec.md).


@dataclass(frozen=True)
class ItemPosition:
    """One archived Ingest Batch item."""

    installation_id: str
    sequence: int
    item_index: int


@dataclass(frozen=True)
class PendingItem:
    """An archived item that has no evidence row yet; exactly one of projection or retracted_identity is set."""

    position: ItemPosition
    record_type: str
    projection_schema_version: int
    cycle_kind: Literal["initial-snapshot", "reconciliation-snapshot", "change-cycle"]
    cycle_id: str
    projection: bytes | None
    retracted_identity: str | None


@dataclass(frozen=True)
class ProjectionEntry:
    """The caller's extraction from one archived projection (Decision 1)."""

    position: ItemPosition
    source_system_kind: str
    origin: str
    source_identity: str
    last_modified_time: datetime


@dataclass(frozen=True)
class RetractionEntry:
    """One archived retraction; its record type and Source Identity are the archived ones."""

    position: ItemPosition


@dataclass(frozen=True)
class SourceKey:
    """A Source Record's identity, independent of Sender Installation (ADR 0050)."""

    source_system_kind: str
    origin: str
    record_type: str
    source_identity: str


@dataclass(frozen=True)
class Evidence:
    """One indexed projection or retraction belonging to a Source Record."""

    evidence_id: int
    kind: Literal["projection", "retraction"]
    position: ItemPosition
    cycle_kind: Literal["initial-snapshot", "reconciliation-snapshot", "change-cycle"]
    cycle_id: str
    last_modified_time: datetime | None
    projection_sha256: str | None
    indexed_at: datetime


@dataclass(frozen=True)
class Selection:
    """One append-only reconciled selection, recorded against the evidence head it was computed from."""

    selection_id: int
    outcome: Literal["projection", "retraction", "conflict"]
    evidence_ids: tuple[int, ...]
    evidence_head: int
    recorded_at: datetime


@dataclass(frozen=True)
class SourceRecord:
    """A Source Record with its evidence in delivery order, its head the highest evidence_id, and its selections in recording order."""

    record_id: int
    key: SourceKey
    evidence: tuple[Evidence, ...]
    evidence_head: int | None
    observing_installations: tuple[str, ...]
    selections: tuple[Selection, ...]


@dataclass(frozen=True)
class IndexResult:
    """How many Source Records and evidence rows one index call created."""

    created_records: int
    created_evidence: int


@dataclass(frozen=True)
class IndexCounts:
    """Health-data-free index counts."""

    unindexed_items: int
    source_records: int
    evidence_items: int
    records_awaiting_selection: int


def _source_record_id(connection: sqlite3.Connection, key: SourceKey) -> int | None:
    row = _execute(
        connection,
        "SELECT record_id FROM source_records WHERE source_system_kind = ? AND origin = ? "
        "AND record_type = ? AND source_identity = ?",
        (key.source_system_kind, key.origin, key.record_type, key.source_identity),
    ).fetchone()
    return None if row is None else row["record_id"]


def _record_evidence(connection: sqlite3.Connection, record: sqlite3.Row) -> tuple[Evidence, ...]:
    # Retractions match at read time, including ones indexed before this record
    # existed. The two branches use their own indexes and cannot overlap.
    rows = _execute(
        connection,
        "SELECT e.*, b.cycle_kind, b.cycle_id FROM ("
        "SELECT * FROM source_evidence WHERE record_id = ? UNION ALL "
        "SELECT * FROM source_evidence WHERE kind = 'retraction' AND record_type = ? AND source_identity = ?"
        ") e JOIN ingest_batches b ON b.installation_id = e.installation_id AND b.sequence = e.sequence "
        "JOIN archived_messages m ON m.installation_id = e.installation_id AND m.sequence = e.sequence "
        "ORDER BY m.rowid, e.item_index",
        (record["record_id"], record["record_type"], record["source_identity"]),
    )
    return tuple(
        Evidence(
            evidence_id=row["evidence_id"], kind=row["kind"],
            position=ItemPosition(row["installation_id"], row["sequence"], row["item_index"]),
            cycle_kind=row["cycle_kind"], cycle_id=row["cycle_id"],
            last_modified_time=_optional_datetime(row["last_modified_time"]),
            projection_sha256=row["projection_sha256"], indexed_at=_datetime(row["indexed_at"]),
        )
        for row in rows
    )


def _selection(connection: sqlite3.Connection, row: sqlite3.Row) -> Selection:
    return Selection(
        selection_id=row["selection_id"], outcome=row["outcome"],
        evidence_ids=tuple(
            item["evidence_id"] for item in _execute(
                connection, "SELECT evidence_id FROM selection_evidence WHERE selection_id = ? ORDER BY evidence_index",
                (row["selection_id"],),
            )
        ),
        evidence_head=row["evidence_head"], recorded_at=_datetime(row["recorded_at"]),
    )


# Heads are based on indexing identity, independently of delivery-order listing.
# Each correlated lookup uses an index on the new tables only.
_EVIDENCE_HEAD_SQL = (
    "nullif(max("
    "coalesce((SELECT max(e.evidence_id) FROM source_evidence e WHERE e.record_id = r.record_id), 0), "
    "coalesce((SELECT max(e.evidence_id) FROM source_evidence e WHERE e.kind = 'retraction' "
    "AND e.record_type = r.record_type AND e.source_identity = r.source_identity), 0)"
    "), 0)"
)
_AWAITING_SELECTION_SQL = (
    "WITH record_heads AS (SELECT r.record_id, " + _EVIDENCE_HEAD_SQL + " AS evidence_head FROM source_records r) "
    "SELECT h.record_id FROM record_heads h WHERE h.evidence_head > 0 AND h.evidence_head IS NOT "
    "(SELECT s.evidence_head FROM source_selections s WHERE s.record_id = h.record_id "
    "ORDER BY s.selection_id DESC LIMIT 1)"
)


# operational-store@4: Processing Jobs, leases, outcomes, and item quarantine
# (packs/operational-store-4.spec.md).

Attribution = Literal["experienced", "inferred"]
OutcomeKind = Literal["revision", "unmodeled", "policy-pending", "quarantine"]
JobState = Literal["queued", "leased", "completed", "failed", "superseded"]
AttemptResult = Literal["completed", "transient-failure", "deterministic-failure", "lease-expired"]
FailureKind = Literal["transient", "deterministic"]


@dataclass(frozen=True)
class RevisionEntry:
    """The caller's Canonical Revision: opaque header and body bytes plus indexed header columns (Decision 7).

    The end_* columns are all None for an instant. The store never parses the bytes, and repr omits them.
    """

    header_bytes: bytes = field(repr=False)
    body_bytes: bytes = field(repr=False)
    body_version: int
    start_epoch_ns: int
    end_epoch_ns: int | None
    start_local_date: date
    end_local_date: date | None
    start_attribution: Attribution
    end_attribution: Attribution | None


@dataclass(frozen=True)
class OutcomeEntry:
    """One interpretation outcome, decided by interpret-source-projection; the store checks its shape only."""

    kind: OutcomeKind
    processor_version: str
    policy_identity: str
    reason: str | None
    pointer: str | None
    policy_kind: str | None
    revision: RevisionEntry | None


@dataclass(frozen=True)
class StoredRevision:
    """A recorded Canonical Revision with the store's digests; repr omits its bytes."""

    header_bytes: bytes = field(repr=False)
    body_bytes: bytes = field(repr=False)
    body_version: int
    start_epoch_ns: int
    end_epoch_ns: int | None
    start_local_date: date
    end_local_date: date | None
    start_attribution: Attribution
    end_attribution: Attribution | None
    header_sha256: str
    body_sha256: str


@dataclass(frozen=True)
class ProcessingOutcome:
    """One immutable outcome for a selection, processor version, and policy identity; a revision's identity is outcome_id."""

    outcome_id: int
    record_id: int
    selection_id: int
    evidence_id: int
    processor_version: str
    policy_identity: str
    kind: OutcomeKind
    reason: str | None
    pointer: str | None
    policy_kind: str | None
    revision: StoredRevision | None
    job_id: int
    fence: int
    recorded_at: datetime


@dataclass(frozen=True)
class ProcessingAttempt:
    """One claim of a Processing Job; result is None while the attempt is open."""

    fence: int
    owner: str
    claimed_at: datetime
    expires_at: datetime
    finished_at: datetime | None
    result: AttemptResult | None
    reason: str | None


@dataclass(frozen=True)
class ProcessingJob:
    """A Processing Job for one Source Record and processor version, with its attempts in order."""

    job_id: int
    record_id: int
    processor_version: str
    selection_id: int
    state: JobState
    max_attempts: int
    attempts: int
    not_before: datetime
    fence: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    failure: str | None
    enqueued_at: datetime
    closed_at: datetime | None
    history: tuple[ProcessingAttempt, ...]


@dataclass(frozen=True)
class Lease:
    """An expiring lease on one job, fenced by the claim that took it, naming what the holder interprets."""

    job_id: int
    fence: int
    owner: str
    record_id: int
    selection_id: int
    evidence_id: int
    processor_version: str
    expires_at: datetime


@dataclass(frozen=True)
class ItemQuarantine:
    """A health-data-free quarantine of one archived projection item that cannot be indexed."""

    position: ItemPosition
    reason: str
    pointer: str
    recorded_at: datetime


@dataclass(frozen=True)
class AwaitingProcessing:
    """A Source Record's latest projection selection that awaits processing under one processor version."""

    record_id: int
    selection_id: int
    processor_version: str


@dataclass(frozen=True)
class ProcessingCounts:
    """Health-data-free processing counts at one caller time."""

    jobs_ready: int
    jobs_delayed: int
    jobs_leased: int
    jobs_lease_expired: int
    jobs_completed: int
    jobs_failed: int
    jobs_superseded: int
    revisions: int
    unmodeled: int
    policy_pending: int
    quarantined: int
    quarantined_items: int
    items_awaiting_index: int


# operational-store@5: policy records, policy reads, known origins, and the
# reprocessing a Fallback Timezone Policy affects (packs/operational-store-5.spec.md).


@dataclass(frozen=True)
class OriginKey:
    """One origin, as a Source Record key names it."""

    source_system_kind: str
    origin: str


@dataclass(frozen=True)
class FallbackTimezonePolicyRecord:
    """A pending Fallback Timezone Policy; effective_from None covers all earlier history, superseded_by None is live."""

    policy_id: int
    zone: str
    effective_from: datetime | None
    supersedes: tuple[int, ...]
    superseded_by: int | None
    requested_at: datetime


@dataclass(frozen=True)
class SourceSelectionPolicyRecord:
    """A pending Source Selection Policy over inclusive local dates; a None bound is unbounded."""

    policy_id: int
    measure: str
    record_type: str
    origins: tuple[OriginKey, ...]
    first_date: date | None
    last_date: date | None
    supersedes: tuple[int, ...]
    superseded_by: int | None
    requested_at: datetime


@dataclass(frozen=True)
class FallbackTimezoneRecording:
    """A recorded Fallback Timezone Policy with health-data-free counts of the jobs queued and leases marked."""

    policy: FallbackTimezonePolicyRecord
    jobs_enqueued: int
    jobs_rearmed: int


@dataclass(frozen=True)
class KnownOrigin:
    """One source-system kind, record type, and origin among the Source Records, with its record count."""

    source_system_kind: str
    record_type: str
    origin: str
    record_count: int



# All routing and counting uses scalar columns; bytes are loaded only by reads
# that explicitly return them. The earlier migrations and write paths are intact.
_CLAIM_PROCESSING_SQL = (
    "SELECT * FROM processing_jobs WHERE job_id = (SELECT min(job_id) FROM ("
    "SELECT job_id FROM processing_jobs WHERE state = 'queued' AND not_before <= ? UNION ALL "
    "SELECT job_id FROM processing_jobs WHERE state = 'leased' AND lease_expires_at <= ?))"
)
_UNINDEXED_PROCESSING_SQL = (
    "NOT EXISTS (SELECT 1 FROM source_evidence e WHERE e.installation_id = i.installation_id "
    "AND e.sequence = i.sequence AND e.item_index = i.item_index) "
    "AND NOT EXISTS (SELECT 1 FROM item_quarantines q WHERE q.installation_id = i.installation_id "
    "AND q.sequence = i.sequence AND q.item_index = i.item_index)"
)
_ITEMS_AWAITING_INDEX_SQL = (
    "WITH positions AS MATERIALIZED ("
    "SELECT i.installation_id, i.sequence, i.item_index, m.rowid AS archive_order "
    "FROM archived_messages m CROSS JOIN batch_items i "
    "ON i.installation_id = m.installation_id AND i.sequence = m.sequence "
    "WHERE " + _UNINDEXED_PROCESSING_SQL + " ORDER BY m.rowid, i.item_index LIMIT ?) "
    "SELECT i.*, b.record_type, b.projection_schema_version, b.cycle_kind, b.cycle_id "
    "FROM positions p JOIN batch_items i USING (installation_id, sequence, item_index) "
    "JOIN ingest_batches b USING (installation_id, sequence) ORDER BY p.archive_order, p.item_index"
)
_PROCESSING_JOB_COUNTS = (
    "state = 'queued' AND not_before <= ?", "state = 'queued' AND not_before > ?",
    "state = 'leased' AND lease_expires_at > ?", "state = 'leased' AND lease_expires_at <= ?",
    "state = 'completed'", "state = 'failed'", "state = 'superseded'",
)


def _awaiting_processing_sql(processor_versions: Mapping[str, str]) -> tuple[str, tuple[object, ...]]:
    # A branch per caller-mapped type uses the type/identity index. UNION ALL
    # merges the branches in record order and applies one limit across types.
    branch = (
        "SELECT r.record_id, s.selection_id, ? AS processor_version FROM source_records r "
        "JOIN source_selections s ON s.selection_id = (SELECT max(latest.selection_id) "
        "FROM source_selections latest WHERE latest.record_id = r.record_id) "
        "WHERE r.record_type = ? AND s.outcome = 'projection' "
        "AND NOT EXISTS (SELECT 1 FROM processing_outcomes o WHERE o.selection_id = s.selection_id "
        "AND o.processor_version = ?) "
        "AND NOT EXISTS (SELECT 1 FROM processing_jobs j WHERE j.record_id = r.record_id "
        "AND j.processor_version = ? AND j.state IN ('queued', 'leased')) "
        "AND NOT EXISTS (SELECT 1 FROM processing_jobs j WHERE j.record_id = r.record_id "
        "AND j.processor_version = ? AND j.selection_id = s.selection_id AND j.state = 'failed')"
    )
    parameters = tuple(value for record_type, version in processor_versions.items()
                       for value in (version, record_type, version, version, version))
    return " UNION ALL ".join(branch for _ in processor_versions) + " ORDER BY 1 LIMIT ?", parameters


def _processing_bound(value: int, maximum: int) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise StoreRefusal("processing-request-invalid")


def _kebab(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 64 and re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value) is not None


def _pointer(value: object) -> bool:
    return isinstance(value, str) and (not value or value.startswith("/"))


def _latest_processing_selection(connection: sqlite3.Connection, record_id: int) -> sqlite3.Row | None:
    return _execute(
        connection, "SELECT selection_id, outcome FROM source_selections WHERE record_id = ? "
        "ORDER BY selection_id DESC LIMIT 1", (record_id,),
    ).fetchone()


def _processing_evidence(connection: sqlite3.Connection, selection_id: int) -> int:
    return _execute(
        connection, "SELECT evidence_id FROM selection_evidence WHERE selection_id = ? AND evidence_index = 0",
        (selection_id,),
    ).fetchone()[0]


def _processing_job(connection: sqlite3.Connection, row: sqlite3.Row) -> ProcessingJob:
    history = tuple(
        ProcessingAttempt(
            item["fence"], item["owner"], _datetime(item["claimed_at"]), _datetime(item["expires_at"]),
            _optional_datetime(item["finished_at"]), item["result"], item["reason"],
        )
        for item in _execute(connection, "SELECT * FROM processing_attempts WHERE job_id = ? ORDER BY fence", (row["job_id"],))
    )
    return ProcessingJob(
        row["job_id"], row["record_id"], row["processor_version"], row["selection_id"], row["state"],
        row["max_attempts"], row["attempts"], _datetime(row["not_before"]), row["fence"], row["lease_owner"],
        _optional_datetime(row["lease_expires_at"]), row["failure"], _datetime(row["enqueued_at"]),
        _optional_datetime(row["closed_at"]), history,
    )


def _held_processing_job(connection: sqlite3.Connection, job_id: int, fence: int) -> sqlite3.Row:
    row = _execute(connection, "SELECT * FROM processing_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        raise StoreRefusal("processing-job-unknown")
    if row["state"] != "leased" or row["fence"] != fence:
        raise StoreRefusal("lease-not-held")
    return row


def _processing_lease(connection: sqlite3.Connection, row: sqlite3.Row) -> Lease:
    return Lease(
        row["job_id"], row["fence"], row["lease_owner"], row["record_id"], row["selection_id"],
        _processing_evidence(connection, row["selection_id"]), row["processor_version"], _datetime(row["lease_expires_at"]),
    )


def _finish_processing_attempt(
    connection: sqlite3.Connection, job: sqlite3.Row, result: AttemptResult, reason: str | None, timestamp: str
) -> None:
    # expires_at is claim-time history: renewal changes only the job's expiry.
    _execute(
        connection, "UPDATE processing_attempts SET finished_at = ?, result = ?, reason = ? "
        "WHERE job_id = ? AND fence = ?", (timestamp, result, reason, job["job_id"], job["fence"]),
    )


def _close_processing_job(
    connection: sqlite3.Connection, job_id: int, state: JobState, timestamp: str, failure: str | None = None
) -> None:
    _execute(
        connection, "UPDATE processing_jobs SET state = ?, closed_at = ?, failure = ?, "
        "lease_owner = NULL, lease_expires_at = NULL WHERE job_id = ?", (state, timestamp, failure, job_id),
    )


def _validate_processing_outcome(outcome: OutcomeEntry, processor_version: str) -> None:
    if not isinstance(outcome, OutcomeEntry):
        raise StoreRefusal("outcome-invalid")
    invalid = (
        outcome.kind not in ("revision", "unmodeled", "policy-pending", "quarantine")
        or outcome.processor_version != processor_version
        or not isinstance(outcome.policy_identity, str) or not outcome.policy_identity
        or (outcome.kind == "revision") != (outcome.revision is not None)
        or (outcome.reason is not None if outcome.kind == "revision" else not _kebab(outcome.reason))
        or (not _kebab(outcome.policy_kind) if outcome.kind == "policy-pending" else outcome.policy_kind is not None)
        or (outcome.kind == "quarantine" and not _pointer(outcome.pointer))
        or (outcome.pointer is not None and not isinstance(outcome.pointer, str))
    )
    revision = outcome.revision
    if revision is not None:
        if not isinstance(revision, RevisionEntry):
            raise StoreRefusal("outcome-invalid")
        ends = (revision.end_epoch_ns, revision.end_local_date, revision.end_attribution)
        invalid |= (
            not isinstance(revision.header_bytes, bytes) or not revision.header_bytes
            or not isinstance(revision.body_bytes, bytes) or not revision.body_bytes
            or type(revision.body_version) is not int or revision.body_version < 1
            or type(revision.start_epoch_ns) is not int or type(revision.start_local_date) is not date
            or revision.start_attribution not in ("experienced", "inferred")
            or (any(value is None for value in ends) and any(value is not None for value in ends))
        )
        if revision.end_epoch_ns is not None:
            invalid |= (
                type(revision.end_epoch_ns) is not int
                or type(revision.start_epoch_ns) is not int
                or revision.end_epoch_ns < revision.start_epoch_ns
                or type(revision.end_local_date) is not date
                or revision.end_attribution not in ("experienced", "inferred")
            )
    if invalid:
        raise StoreRefusal("outcome-invalid")


def _processing_outcome(connection: sqlite3.Connection, row: sqlite3.Row) -> ProcessingOutcome:
    revision_row = _execute(connection, "SELECT * FROM canonical_revisions WHERE outcome_id = ?", (row["outcome_id"],)).fetchone()
    revision = None if revision_row is None else StoredRevision(
        revision_row["header_bytes"], revision_row["body_bytes"], revision_row["body_version"],
        revision_row["start_epoch_ns"], revision_row["end_epoch_ns"], date.fromisoformat(revision_row["start_local_date"]),
        None if revision_row["end_local_date"] is None else date.fromisoformat(revision_row["end_local_date"]),
        revision_row["start_attribution"], revision_row["end_attribution"],
        revision_row["header_sha256"], revision_row["body_sha256"],
    )
    return ProcessingOutcome(
        row["outcome_id"], row["record_id"], row["selection_id"], row["evidence_id"], row["processor_version"],
        row["policy_identity"], row["kind"], row["reason"], row["pointer"], row["policy_kind"], revision,
        row["job_id"], row["fence"], _datetime(row["recorded_at"]),
    )


def _outcome_entry(outcome: ProcessingOutcome) -> OutcomeEntry:
    revision = outcome.revision
    entry = None if revision is None else RevisionEntry(
        revision.header_bytes, revision.body_bytes, revision.body_version, revision.start_epoch_ns,
        revision.end_epoch_ns, revision.start_local_date, revision.end_local_date,
        revision.start_attribution, revision.end_attribution,
    )
    return OutcomeEntry(outcome.kind, outcome.processor_version, outcome.policy_identity,
                        outcome.reason, outcome.pointer, outcome.policy_kind, entry)


def _item_quarantine(connection: sqlite3.Connection, position: ItemPosition) -> ItemQuarantine | None:
    row = _execute(
        connection, "SELECT reason, pointer, recorded_at FROM item_quarantines "
        "WHERE installation_id = ? AND sequence = ? AND item_index = ?",
        (position.installation_id, position.sequence, position.item_index),
    ).fetchone()
    return None if row is None else ItemQuarantine(position, row["reason"], row["pointer"], _datetime(row["recorded_at"]))


class StoreView:
    """Reads bound to one SQLite read transaction."""

    _connection: sqlite3.Connection
    _path: str | PathLike[str]
    _reserve_bytes: int
    _free_bytes: Callable[[str | PathLike[str]], int]

    def installation(self, installation_id: str) -> InstallationStatus:
        return _installation_status(_installation(self._connection, installation_id))

    def installations(self) -> tuple[InstallationStatus, ...]:
        return tuple(
            _installation_status(row)
            for row in _execute(self._connection, "SELECT * FROM installations ORDER BY installation_id")
        )

    def installation_events(self, installation_id: str) -> tuple[LifecycleEvent, ...]:
        return tuple(
            LifecycleEvent(cast(InstallationState, row[0]), _datetime(row[1]), cast(RetirementKind | None, row[2]))
            for row in _execute(
                self._connection,
                "SELECT state, at, retirement_kind FROM installation_events WHERE installation_id = ? ORDER BY event_id",
                (installation_id,),
            )
        )

    def accepted_envelopes(self, installation_id: str) -> Iterator[tuple[int, bytes]]:
        for row in _execute(
            self._connection,
            "SELECT sequence, envelope FROM archived_messages WHERE installation_id = ? ORDER BY sequence",
            (installation_id,),
        ):
            yield row[0], row[1]

    def archived_message(self, installation_id: str, sequence: int) -> ArchivedMessage | None:
        identity = (installation_id, sequence)
        row = _execute(
            self._connection, "SELECT * FROM archived_messages WHERE installation_id = ? AND sequence = ?", identity
        ).fetchone()
        if row is None:
            return None
        batch = None
        completion = None
        checkpoint = None
        if row["kind"] == "ingest-batch":
            record = _execute(
                self._connection, "SELECT * FROM ingest_batches WHERE installation_id = ? AND sequence = ?", identity
            ).fetchone()
            batch = IngestBatchRecord(
                record_type=record["record_type"],
                projection_schema_version=record["projection_schema_version"],
                cycle_kind=record["cycle_kind"],
                cycle_id=record["cycle_id"],
                source_page_index=record["source_page_index"],
                fragment_index=record["fragment_index"],
                previous_chain=record["previous_chain"],
                items=self.archived_items(installation_id, sequence),
            )
        elif row["kind"] in ("snapshot-completion", "change-completion"):
            record = _execute(
                self._connection, "SELECT * FROM completions WHERE installation_id = ? AND sequence = ?", identity
            ).fetchone()
            completion = CompletionRecord(
                completion_kind=record["completion_kind"],
                snapshot_kind=record["snapshot_kind"],
                record_type=record["record_type"],
                cycle_id=record["cycle_id"],
                page_count=record["page_count"],
                fragment_count=record["fragment_count"],
                record_count=record["record_count"],
                final_chain=record["final_chain"],
            )
        elif row["kind"] == "sync-checkpoint":
            record = _execute(
                self._connection, "SELECT * FROM sync_checkpoints WHERE installation_id = ? AND sequence = ?", identity
            ).fetchone()
            checkpoint = self._checkpoint(record)
        return ArchivedMessage(
            sequence=sequence,
            kind=cast(MessageKind, row["kind"]),
            message_sha256=row["message_sha256"],
            staged_generation=row["staged_generation"],
            receipt_time=_datetime(row["receipt_time"]),
            envelope=row["envelope"],
            receipt=row["receipt"],
            batch=batch,
            completion=completion,
            checkpoint=checkpoint,
        )

    def archived_items(self, installation_id: str, sequence: int) -> tuple[BatchItem, ...]:
        return tuple(
            BatchItem(projection=row[0], retracted_identity=row[1])
            for row in _execute(
                self._connection,
                "SELECT projection, retracted_identity FROM batch_items "
                "WHERE installation_id = ? AND sequence = ? ORDER BY item_index",
                (installation_id, sequence),
            )
        )

    def honored_generations(self) -> tuple[HonoredGeneration, ...]:
        return tuple(
            HonoredGeneration(row[0], row[1], _datetime(row[2]))
            for row in _execute(
                self._connection,
                "SELECT generation, advertisement, honored_at FROM generations WHERE stopped_at IS NULL ORDER BY generation",
            )
        )

    def latest_acknowledged_generation(self) -> int | None:
        return _latest_acknowledged_generation(self._connection)

    def sequence_gaps(self, installation_id: str) -> tuple[SequenceGap, ...]:
        return tuple(
            SequenceGap(row[0], row[1], _datetime(row[2]))
            for row in _execute(
                self._connection,
                "SELECT gap_after, sequence, detected_at FROM sequence_gaps WHERE installation_id = ? ORDER BY sequence",
                (installation_id,),
            )
        )

    def delivery_blocks(self, installation_id: str | None = None) -> tuple[DeliveryBlockRecord, ...]:
        sql = "SELECT * FROM delivery_blocks"
        parameters: tuple[object, ...] = ()
        if installation_id is not None:
            sql += " WHERE installation_id = ?"
            parameters = (installation_id,)
        return tuple(
            DeliveryBlockRecord(
                row["installation_id"], row["sequence"], row["message_sha256"], row["failure"],
                _datetime(row["first_seen_at"]), _datetime(row["last_seen_at"]), row["count"],
            )
            for row in _execute(self._connection, sql + " ORDER BY block_id", parameters)
        )

    def sync_checkpoints(
        self, installation_id: str, *, before_sequence: int | None = None, limit: int = 50
    ) -> tuple[ArchivedCheckpoint, ...]:
        sql = (
            "SELECT c.*, m.receipt_time FROM sync_checkpoints c JOIN archived_messages m "
            "USING (installation_id, sequence) WHERE c.installation_id = ?"
        )
        parameters: tuple[object, ...] = (installation_id,)
        if before_sequence is not None:
            sql += " AND c.sequence < ?"
            parameters += (before_sequence,)
        return tuple(
            ArchivedCheckpoint(row["sequence"], _datetime(row["receipt_time"]), self._checkpoint(row))
            for row in _execute(self._connection, sql + " ORDER BY c.sequence DESC LIMIT ?", parameters + (limit,))
        )

    def _checkpoint(self, row: sqlite3.Row) -> SyncCheckpointRecord:
        outcomes = tuple(
            RecordTypeOutcome(item[0], item[1], item[2], bool(item[3]))
            for item in _execute(
                self._connection,
                "SELECT record_type, outcome, reason, source_data_staged FROM checkpoint_outcomes "
                "WHERE installation_id = ? AND sequence = ? ORDER BY outcome_index",
                (row["installation_id"], row["sequence"]),
            )
        )
        return SyncCheckpointRecord(
            run_sequence=row["run_sequence"],
            trigger=row["trigger"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            elapsed_ms=row["elapsed_ms"],
            background_scheduling=row["background_scheduling"],
            record_types=outcomes,
            outbox_messages_before_checkpoint=row["outbox_messages_before_checkpoint"],
            outbox_bytes_before_checkpoint=row["outbox_bytes_before_checkpoint"],
        )

    def delivery_cycle(self, installation_id: str, cycle_id: str) -> DeliveryCycle | None:
        """Report archived cycle facts from this view's snapshot, without replay."""
        identity = (installation_id, cycle_id)
        last = _execute(
            self._connection,
            "SELECT b.sequence, b.record_type, b.cycle_kind, b.source_page_index, b.fragment_index, "
            "b.previous_chain, (SELECT m.message_sha256 FROM archived_messages m "
            "WHERE m.installation_id = b.installation_id AND m.sequence = b.sequence) AS message_sha256 "
            "FROM ingest_batches b WHERE b.installation_id = ? AND b.cycle_id = ? "
            "ORDER BY b.sequence DESC LIMIT 1",
            identity,
        ).fetchone()
        completion = _execute(
            self._connection,
            "SELECT sequence, record_type FROM completions WHERE installation_id = ? AND cycle_id = ?",
            identity,
        ).fetchone()
        if last is None:
            if completion is None:
                return None
            return DeliveryCycle(
                installation_id=installation_id,
                cycle_id=cycle_id,
                record_type=completion["record_type"],
                cycle_kind=None,
                first_sequence=None,
                last_sequence=None,
                last_source_page_index=0,
                last_fragment_index=0,
                last_previous_chain=None,
                last_message_sha256=None,
                fragment_count=0,
                record_count=0,
                completion_sequence=completion["sequence"],
            )

        # Count through each matching fragment's item key, including empty fragments.
        # Do not join all of the installation's items or read their health data.
        counts = _execute(
            self._connection,
            "SELECT min(b.sequence) AS first_sequence, count(*) AS fragment_count, "
            "sum((SELECT count(*) FROM batch_items i "
            "WHERE i.installation_id = b.installation_id AND i.sequence = b.sequence)) AS record_count "
            "FROM ingest_batches b WHERE b.installation_id = ? AND b.cycle_id = ?",
            identity,
        ).fetchone()
        return DeliveryCycle(
            installation_id=installation_id,
            cycle_id=cycle_id,
            record_type=last["record_type"],
            cycle_kind=last["cycle_kind"],
            first_sequence=counts["first_sequence"],
            last_sequence=last["sequence"],
            last_source_page_index=last["source_page_index"],
            last_fragment_index=last["fragment_index"],
            last_previous_chain=last["previous_chain"],
            last_message_sha256=last["message_sha256"],
            fragment_count=counts["fragment_count"],
            record_count=counts["record_count"],
            completion_sequence=None if completion is None else completion["sequence"],
        )

    def pending_items(self, limit: int) -> tuple[PendingItem, ...]:
        return tuple(
            PendingItem(
                position=ItemPosition(row["installation_id"], row["sequence"], row["item_index"]),
                record_type=row["record_type"], projection_schema_version=row["projection_schema_version"],
                cycle_kind=row["cycle_kind"], cycle_id=row["cycle_id"],
                projection=row["projection"], retracted_identity=row["retracted_identity"],
            )
            for row in _execute(
                self._connection,
                "SELECT i.*, b.record_type, b.projection_schema_version, b.cycle_kind, b.cycle_id "
                "FROM batch_items i JOIN ingest_batches b "
                "ON b.installation_id = i.installation_id AND b.sequence = i.sequence "
                "JOIN archived_messages m ON m.installation_id = i.installation_id AND m.sequence = i.sequence "
                "WHERE NOT EXISTS (SELECT 1 FROM source_evidence e WHERE e.installation_id = i.installation_id "
                "AND e.sequence = i.sequence AND e.item_index = i.item_index) "
                "ORDER BY m.rowid, i.item_index LIMIT ?",
                (limit,),
            )
        )

    def source_record_id(self, key: SourceKey) -> int | None:
        return _source_record_id(self._connection, key)

    def source_record(self, record_id: int) -> SourceRecord | None:
        row = _execute(self._connection, "SELECT * FROM source_records WHERE record_id = ?", (record_id,)).fetchone()
        if row is None:
            return None
        evidence = _record_evidence(self._connection, row)
        return SourceRecord(
            record_id=record_id,
            key=SourceKey(row["source_system_kind"], row["origin"], row["record_type"], row["source_identity"]),
            evidence=evidence, evidence_head=max((item.evidence_id for item in evidence), default=None),
            observing_installations=tuple(sorted({item.position.installation_id for item in evidence})),
            selections=tuple(
                _selection(self._connection, selection) for selection in _execute(
                    self._connection, "SELECT * FROM source_selections WHERE record_id = ? ORDER BY selection_id",
                    (record_id,),
                )
            ),
        )

    def records_awaiting_selection(self, limit: int) -> tuple[int, ...]:
        return tuple(
            row["record_id"] for row in _execute(
                self._connection, _AWAITING_SELECTION_SQL + " ORDER BY h.record_id LIMIT ?", (limit,),
            )
        )

    def index_counts(self) -> IndexCounts:
        row = _execute(
            self._connection,
            "SELECT (SELECT count(*) FROM batch_items) - (SELECT count(*) FROM source_evidence) AS unindexed_items, "
            "(SELECT count(*) FROM source_records) AS source_records, "
            "(SELECT count(*) FROM source_evidence) AS evidence_items, "
            "(SELECT count(*) FROM (" + _AWAITING_SELECTION_SQL + ")) AS records_awaiting_selection",
        ).fetchone()
        return IndexCounts(
            row["unindexed_items"], row["source_records"], row["evidence_items"], row["records_awaiting_selection"],
        )

    def processing_job(self, job_id: int) -> ProcessingJob | None:
        row = _execute(self._connection, "SELECT * FROM processing_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return None if row is None else _processing_job(self._connection, row)

    def processing_jobs(self, record_id: int) -> tuple[ProcessingJob, ...]:
        return tuple(
            _processing_job(self._connection, row) for row in _execute(
                self._connection, "SELECT * FROM processing_jobs WHERE record_id = ? ORDER BY job_id", (record_id,),
            )
        )

    def processing_outcomes(self, record_id: int) -> tuple[ProcessingOutcome, ...]:
        return tuple(
            _processing_outcome(self._connection, row) for row in _execute(
                self._connection, "SELECT * FROM processing_outcomes WHERE record_id = ? ORDER BY outcome_id", (record_id,),
            )
        )

    def latest_outcome(self, record_id: int, processor_version: str) -> ProcessingOutcome | None:
        row = _execute(
            self._connection, "SELECT * FROM processing_outcomes WHERE selection_id = "
            "(SELECT max(selection_id) FROM source_selections WHERE record_id = ?) "
            "AND processor_version = ? ORDER BY outcome_id DESC LIMIT 1", (record_id, processor_version),
        ).fetchone()
        return None if row is None else _processing_outcome(self._connection, row)

    def selections_awaiting_processing(
        self, processor_versions: Mapping[str, str], limit: int
    ) -> tuple[AwaitingProcessing, ...]:
        if not processor_versions:
            return ()
        sql, parameters = _awaiting_processing_sql(processor_versions)
        return tuple(
            AwaitingProcessing(row["record_id"], row["selection_id"], row["processor_version"])
            for row in _execute(self._connection, sql, parameters + (limit,))
        )

    def items_awaiting_index(self, limit: int) -> tuple[PendingItem, ...]:
        return tuple(
            PendingItem(
                position=ItemPosition(row["installation_id"], row["sequence"], row["item_index"]),
                record_type=row["record_type"], projection_schema_version=row["projection_schema_version"],
                cycle_kind=row["cycle_kind"], cycle_id=row["cycle_id"],
                projection=row["projection"], retracted_identity=row["retracted_identity"],
            )
            for row in _execute(self._connection, _ITEMS_AWAITING_INDEX_SQL, (limit,))
        )

    def item_quarantine(self, position: ItemPosition) -> ItemQuarantine | None:
        return _item_quarantine(self._connection, position)

    def projection_bytes(self, evidence_id: int) -> bytes | None:
        row = _execute(
            self._connection, "SELECT i.projection FROM source_evidence e JOIN batch_items i "
            "USING (installation_id, sequence, item_index) WHERE e.evidence_id = ? AND e.kind = 'projection'",
            (evidence_id,),
        ).fetchone()
        return None if row is None else row[0]

    def processing_counts(self, at: datetime) -> ProcessingCounts:
        timestamp = _timestamp(at)
        # Only narrow SQL aggregates: no PendingItem, job history, outcome, or
        # revision is materialized merely to count it. Outcomes count history.
        counts = [
            _execute(self._connection, "SELECT count(*) FROM processing_jobs WHERE " + condition,
                     (timestamp,) if "?" in condition else ()).fetchone()[0]
            for condition in _PROCESSING_JOB_COUNTS
        ]
        counts.extend(
            _execute(self._connection, "SELECT count(*) FROM processing_outcomes WHERE kind = ?", (kind,)).fetchone()[0]
            for kind in ("revision", "unmodeled", "policy-pending", "quarantine")
        )
        counts.append(_execute(self._connection, "SELECT count(*) FROM item_quarantines").fetchone()[0])
        counts.append(_execute(
            self._connection, "SELECT count(*) FROM batch_items i WHERE " + _UNINDEXED_PROCESSING_SQL,
        ).fetchone()[0])
        return ProcessingCounts(*counts)

    # ---- operational-store@5 stub ----

    def policy(self, policy_id: int) -> FallbackTimezonePolicyRecord | SourceSelectionPolicyRecord | None:
        raise NotImplementedError("operational-store@5")

    def fallback_timezone_policies(self) -> tuple[FallbackTimezonePolicyRecord, ...]:
        raise NotImplementedError("operational-store@5")

    def fallback_timezone_timeline(self) -> tuple[FallbackTimezonePolicyRecord, ...]:
        raise NotImplementedError("operational-store@5")

    def source_selection_policies(
        self, measure: str | None = None, record_type: str | None = None
    ) -> tuple[SourceSelectionPolicyRecord, ...]:
        raise NotImplementedError("operational-store@5")

    def source_selection_timeline(self, measure: str, record_type: str) -> tuple[SourceSelectionPolicyRecord, ...]:
        raise NotImplementedError("operational-store@5")

    def applicable_source_selection(
        self, measure: str, record_type: str, on: date
    ) -> SourceSelectionPolicyRecord | None:
        raise NotImplementedError("operational-store@5")

    def known_origins(self, record_type: str | None = None) -> tuple[KnownOrigin, ...]:
        raise NotImplementedError("operational-store@5")

    def storage_reserve(self) -> ReserveStatus:
        free = _sample_free_bytes(self._free_bytes, self._path)
        return ReserveStatus(free, self._reserve_bytes, free >= self._reserve_bytes)

    def settings(self) -> StoreSettings:
        return _settings(self._connection)


class OperationalStore:
    """Deep transactional operations over one SQLite Operational Store file."""

    _connection: sqlite3.Connection
    _path: str | PathLike[str]
    _reserve_bytes: int
    _free_bytes: Callable[[str | PathLike[str]], int]
    _busy_timeout_ms: int

    def settings(self) -> StoreSettings:
        return _settings(self._connection)

    def record_enrollment_offer(self, code_digest: str, expires_at: datetime, at: datetime) -> None:
        _timestamp(at)
        expiry = _timestamp(expires_at)
        with _transaction(self._connection):
            if _execute(
                self._connection, "SELECT 1 FROM enrollment_offers WHERE code_digest = ?", (code_digest,)
            ).fetchone() is not None:
                raise StoreRefusal("enrollment-code-exists")
            _execute(
                self._connection,
                "INSERT INTO enrollment_offers (code_digest, expires_at) VALUES (?, ?)",
                (code_digest, expiry),
            )

    def enroll_installation(self, code_digest: str, installation_id: str, fingerprint: str, at: datetime) -> None:
        timestamp = _timestamp(at)
        with _transaction(self._connection):
            offer = _execute(
                self._connection, "SELECT expires_at, consumed_at FROM enrollment_offers WHERE code_digest = ?", (code_digest,)
            ).fetchone()
            if offer is None:
                raise StoreRefusal("enrollment-code-unknown")
            if offer["consumed_at"] is not None:
                raise StoreRefusal("enrollment-code-consumed")
            if timestamp >= offer["expires_at"]:
                raise StoreRefusal("enrollment-code-expired")
            if _execute(
                self._connection, "SELECT 1 FROM installations WHERE installation_id = ?", (installation_id,)
            ).fetchone() is not None:
                raise StoreRefusal("installation-exists")
            _execute(
                self._connection, "UPDATE enrollment_offers SET consumed_at = ? WHERE code_digest = ?", (timestamp, code_digest)
            )
            _execute(
                self._connection,
                "INSERT INTO installations (installation_id, fingerprint, state, enrolled_at) VALUES (?, ?, 'pending', ?)",
                (installation_id, fingerprint, timestamp),
            )
            self._append_event(installation_id, "pending", timestamp)

    def activate_installation(self, installation_id: str, at: datetime) -> None:
        self._transition(installation_id, "active", ("pending",), at)

    def begin_draining(self, installation_id: str, at: datetime) -> None:
        self._transition(installation_id, "draining", ("active",), at)

    def retire_installation(self, installation_id: str, kind: RetirementKind, at: datetime) -> None:
        allowed: tuple[InstallationState, ...]
        if kind == "drained":
            allowed = ("draining",)
        elif kind == "destructive":
            allowed = ("active", "draining")
        else:
            allowed = ()
        self._transition(installation_id, "retired", allowed, at, kind)

    def withdraw_installation(self, installation_id: str, at: datetime) -> None:
        self._transition(installation_id, "withdrawn", ("pending",), at)

    def _append_event(
        self, installation_id: str, state: InstallationState, timestamp: str, kind: RetirementKind | None = None
    ) -> None:
        _execute(
            self._connection,
            "INSERT INTO installation_events (installation_id, state, at, retirement_kind) VALUES (?, ?, ?, ?)",
            (installation_id, state, timestamp, kind),
        )

    def _transition(
        self, installation_id: str, state: InstallationState, allowed: tuple[InstallationState, ...],
        at: datetime, kind: RetirementKind | None = None,
    ) -> None:
        timestamp = _timestamp(at)
        with _transaction(self._connection):
            row = _installation(self._connection, installation_id)
            if row["state"] not in allowed:
                raise StoreRefusal("installation-transition-invalid")
            if state == "active" and _execute(
                self._connection, "SELECT 1 FROM installations WHERE state IN ('active', 'draining')"
            ).fetchone() is not None:
                raise StoreRefusal("active-installation-exists")
            time_column = {
                "active": "activated_at", "draining": "draining_at", "retired": "retired_at", "withdrawn": "withdrawn_at",
            }[state]
            _execute(
                self._connection,
                f"UPDATE installations SET state = ?, {time_column} = ?, retirement_kind = ? WHERE installation_id = ?",
                (state, timestamp, kind, installation_id),
            )
            self._append_event(installation_id, state, timestamp, kind)

    def honor_generation(self, generation: int, advertisement: bytes, at: datetime) -> None:
        timestamp = _timestamp(at)
        with _transaction(self._connection):
            highest = _execute(self._connection, "SELECT max(generation) FROM generations").fetchone()[0]
            if highest is not None and generation <= highest:
                raise StoreRefusal("generation-not-monotonic")
            _execute(
                self._connection,
                "INSERT INTO generations (generation, advertisement, honored_at) VALUES (?, ?, ?)",
                (generation, advertisement, timestamp),
            )

    def stop_honoring_generation(self, generation: int, at: datetime) -> None:
        timestamp = _timestamp(at)
        with _transaction(self._connection):
            if _execute(
                self._connection, "SELECT 1 FROM generations WHERE generation = ? AND stopped_at IS NULL", (generation,)
            ).fetchone() is None:
                raise StoreRefusal("generation-unknown")
            acknowledged = _latest_acknowledged_generation(self._connection)
            if acknowledged is None or acknowledged <= generation:
                raise StoreRefusal("generation-still-required")
            _execute(self._connection, "UPDATE generations SET stopped_at = ? WHERE generation = ?", (timestamp, generation))

    def archive_ingest_batch(self, header: MessageHeader, batch: IngestBatchRecord) -> bytes:
        return self._archive(header, "ingest-batch", batch=batch)

    def archive_completion(self, header: MessageHeader, completion: CompletionRecord) -> bytes:
        kind = "snapshot-completion" if completion.completion_kind == "snapshot" else "change-completion"
        return self._archive(header, kind, completion=completion)

    def archive_sync_checkpoint(self, header: MessageHeader, checkpoint: SyncCheckpointRecord) -> bytes:
        return self._archive(header, "sync-checkpoint", checkpoint=checkpoint)

    def archive_capability_acknowledgement(self, header: MessageHeader) -> bytes:
        return self._archive(header, "capability-acknowledgement")

    def _archive(
        self, header: MessageHeader, kind: MessageKind, *, batch: IngestBatchRecord | None = None,
        completion: CompletionRecord | None = None, checkpoint: SyncCheckpointRecord | None = None,
    ) -> bytes:
        sampled_reserve = False
        try:
            timestamp = _timestamp(header.receipt_time)
            with _transaction(self._connection):
                installation = _installation(self._connection, header.installation_id)
                if hashlib.sha256(header.envelope).hexdigest() != header.message_sha256:
                    raise StoreRefusal("message-digest-mismatch")
                prior = _execute(
                    self._connection,
                    "SELECT message_sha256, receipt FROM archived_messages WHERE installation_id = ? AND sequence = ?",
                    (header.installation_id, header.sequence),
                ).fetchone()
                if prior is not None:
                    if prior["message_sha256"] == header.message_sha256:
                        return prior["receipt"]
                    raise StoreRefusal("sequence-content-conflict")
                if installation["state"] not in ("active", "draining"):
                    raise StoreRefusal("installation-not-delivering")
                if _execute(
                    self._connection, "SELECT 1 FROM generations WHERE generation = ? AND stopped_at IS NULL",
                    (header.staged_generation,),
                ).fetchone() is None:
                    raise StoreRefusal("generation-unknown")
                highest = installation["highest_sequence"]
                if highest is not None and header.sequence <= highest:
                    raise StoreRefusal("sequence-out-of-order")
                if completion is not None and _execute(
                    self._connection, "SELECT 1 FROM completions WHERE installation_id = ? AND cycle_id = ?",
                    (header.installation_id, completion.cycle_id),
                ).fetchone() is not None:
                    raise StoreRefusal("cycle-already-completed")
                sampled_reserve = True
                if _sample_free_bytes(self._free_bytes, self._path) < self._reserve_bytes:
                    raise StoreRetryable("storage-reserve-exhausted")
                _execute(
                    self._connection,
                    "INSERT INTO archived_messages "
                    "(installation_id, sequence, kind, message_sha256, staged_generation, receipt_time, envelope, receipt) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (header.installation_id, header.sequence, kind, header.message_sha256, header.staged_generation,
                     timestamp, header.envelope, header.receipt),
                )
                if batch is not None:
                    self._write_batch(header, batch)
                if completion is not None:
                    self._write_completion(header, completion)
                if checkpoint is not None:
                    self._write_checkpoint(header, checkpoint, timestamp)
                if header.sequence > (highest or 0) + 1:
                    _execute(
                        self._connection,
                        "INSERT INTO sequence_gaps (installation_id, sequence, gap_after, detected_at) VALUES (?, ?, ?, ?)",
                        (header.installation_id, header.sequence, highest or 0, timestamp),
                    )
                _execute(
                    self._connection,
                    "UPDATE installations SET highest_sequence = ?, last_delivery_at = ? WHERE installation_id = ?",
                    (header.sequence, timestamp, header.installation_id),
                )
            return header.receipt
        finally:
            # Even a refused attempt or an identical retry samples the volatile input.
            if not sampled_reserve:
                try:
                    self._free_bytes(self._path)
                except Exception:
                    # Sampling cannot replace an already determined refusal or receipt.
                    pass

    def _write_batch(self, header: MessageHeader, batch: IngestBatchRecord) -> None:
        _execute(
            self._connection,
            "INSERT INTO ingest_batches "
            "(installation_id, sequence, record_type, projection_schema_version, cycle_kind, cycle_id, "
            "source_page_index, fragment_index, previous_chain) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (header.installation_id, header.sequence, batch.record_type, batch.projection_schema_version,
             batch.cycle_kind, batch.cycle_id, batch.source_page_index, batch.fragment_index, batch.previous_chain),
        )
        for index, item in enumerate(batch.items):
            _execute(
                self._connection,
                "INSERT INTO batch_items (installation_id, sequence, item_index, projection, retracted_identity) "
                "VALUES (?, ?, ?, ?, ?)",
                (header.installation_id, header.sequence, index, item.projection, item.retracted_identity),
            )

    def _write_completion(self, header: MessageHeader, completion: CompletionRecord) -> None:
        _execute(
            self._connection,
            "INSERT INTO completions "
            "(installation_id, sequence, completion_kind, snapshot_kind, record_type, cycle_id, "
            "page_count, fragment_count, record_count, final_chain) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (header.installation_id, header.sequence, completion.completion_kind, completion.snapshot_kind,
             completion.record_type, completion.cycle_id, completion.page_count, completion.fragment_count,
             completion.record_count, completion.final_chain),
        )

    def _write_checkpoint(self, header: MessageHeader, checkpoint: SyncCheckpointRecord, timestamp: str) -> None:
        _execute(
            self._connection,
            "INSERT INTO sync_checkpoints "
            "(installation_id, sequence, run_sequence, trigger, started_at, ended_at, elapsed_ms, "
            "background_scheduling, outbox_messages_before_checkpoint, outbox_bytes_before_checkpoint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (header.installation_id, header.sequence, checkpoint.run_sequence, checkpoint.trigger,
             checkpoint.started_at, checkpoint.ended_at, checkpoint.elapsed_ms, checkpoint.background_scheduling,
             checkpoint.outbox_messages_before_checkpoint, checkpoint.outbox_bytes_before_checkpoint),
        )
        for index, outcome in enumerate(checkpoint.record_types):
            _execute(
                self._connection,
                "INSERT INTO checkpoint_outcomes "
                "(installation_id, sequence, outcome_index, record_type, outcome, reason, source_data_staged) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (header.installation_id, header.sequence, index, outcome.record_type, outcome.outcome,
                 outcome.reason, int(outcome.source_data_staged)),
            )
        _execute(
            self._connection,
            "UPDATE installations SET latest_checkpoint_receipt_at = ?, background_scheduling = ?, "
            "background_scheduling_receipt_at = ? WHERE installation_id = ?",
            (timestamp, checkpoint.background_scheduling, timestamp, header.installation_id),
        )

    def record_delivery_block(
        self,
        installation_id: str | None,
        sequence: int | None,
        message_sha256: str,
        failure: str,
        at: datetime,
    ) -> None:
        timestamp = _timestamp(at)
        with _transaction(self._connection):
            _execute(
                self._connection,
                "INSERT INTO delivery_blocks "
                "(installation_id, sequence, message_sha256, failure, first_seen_at, last_seen_at, count) "
                "VALUES (?, ?, ?, ?, ?, ?, 1) ON CONFLICT DO UPDATE SET last_seen_at = excluded.last_seen_at, count = count + 1",
                (installation_id, sequence, message_sha256, failure, timestamp, timestamp),
            )

    def index_items(self, entries: Sequence[ProjectionEntry | RetractionEntry], at: datetime) -> IndexResult:
        timestamp = _timestamp(at)
        entries = tuple(entries)
        modified_times = tuple(
            _timestamp(entry.last_modified_time) if isinstance(entry, ProjectionEntry) else None
            for entry in entries
        )
        for entry in entries:
            if isinstance(entry, ProjectionEntry) and any(
                not isinstance(value, str) or not value
                for value in (entry.source_system_kind, entry.origin, entry.source_identity)
            ):
                raise StoreRefusal("source-key-invalid")

        with _transaction(self._connection):
            # Each validation pass covers the entire call before the next refusal
            # class is considered. No row is written until all passes succeed.
            archived: list[sqlite3.Row] = []
            for entry in entries:
                position = entry.position
                row = _execute(
                    self._connection,
                    "SELECT i.*, b.record_type, m.rowid AS archive_order FROM batch_items i "
                    "JOIN ingest_batches b ON b.installation_id = i.installation_id AND b.sequence = i.sequence "
                    "JOIN archived_messages m ON m.installation_id = i.installation_id AND m.sequence = i.sequence "
                    "WHERE i.installation_id = ? AND i.sequence = ? AND i.item_index = ?",
                    (position.installation_id, position.sequence, position.item_index),
                ).fetchone()
                if row is None:
                    raise StoreRefusal("archived-item-unknown")
                archived.append(row)
            for entry, row in zip(entries, archived):
                if isinstance(entry, ProjectionEntry) != (row["projection"] is not None):
                    raise StoreRefusal("index-entry-mismatch")

            seen: dict[ItemPosition, tuple[object, ...]] = {}
            pending: list[tuple[ProjectionEntry | RetractionEntry, sqlite3.Row, str | None]] = []
            for entry, row, modified in zip(entries, archived, modified_times):
                signature = (
                    ("projection", entry.source_system_kind, entry.origin, entry.source_identity, modified)
                    if isinstance(entry, ProjectionEntry)
                    else ("retraction", None, None, row["retracted_identity"], None)
                )
                position = entry.position
                if position in seen:
                    if seen[position] != signature:
                        raise StoreRefusal("index-entry-conflict")
                    continue
                seen[position] = signature
                prior = _execute(
                    self._connection,
                    "SELECT e.kind, r.source_system_kind, r.origin, e.source_identity, e.last_modified_time "
                    "FROM source_evidence e LEFT JOIN source_records r ON r.record_id = e.record_id "
                    "WHERE e.installation_id = ? AND e.sequence = ? AND e.item_index = ?",
                    (position.installation_id, position.sequence, position.item_index),
                ).fetchone()
                if prior is not None:
                    if tuple(prior) != signature:
                        raise StoreRefusal("index-entry-conflict")
                else:
                    pending.append((entry, row, modified))

            if pending and _sample_free_bytes(self._free_bytes, self._path) < self._reserve_bytes:
                raise StoreRetryable("storage-reserve-exhausted")
            pending.sort(key=lambda item: (item[1]["archive_order"], item[1]["item_index"]))
            created_records = 0
            for entry, row, modified in pending:
                record_id = None
                projection_sha256 = None
                if isinstance(entry, ProjectionEntry):
                    source_key = SourceKey(
                        entry.source_system_kind, entry.origin, row["record_type"], entry.source_identity,
                    )
                    record_id = _source_record_id(self._connection, source_key)
                    if record_id is None:
                        record_id = _execute(
                            self._connection,
                            "INSERT INTO source_records (source_system_kind, origin, record_type, source_identity) "
                            "VALUES (?, ?, ?, ?)",
                            (source_key.source_system_kind, source_key.origin,
                             source_key.record_type, source_key.source_identity),
                        ).lastrowid
                        created_records += 1
                    kind = "projection"
                    source_identity = entry.source_identity
                    projection_sha256 = hashlib.sha256(row["projection"]).hexdigest()
                else:
                    kind = "retraction"
                    source_identity = row["retracted_identity"]
                position = entry.position
                _execute(
                    self._connection,
                    "INSERT INTO source_evidence (installation_id, sequence, item_index, kind, record_id, "
                    "record_type, source_identity, last_modified_time, projection_sha256, indexed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (position.installation_id, position.sequence, position.item_index, kind, record_id,
                     row["record_type"], source_identity, modified, projection_sha256, timestamp),
                )
            return IndexResult(created_records, len(pending))

    def record_selection(
        self, record_id: int, outcome: str, evidence_ids: Sequence[int], evidence_head: int, at: datetime
    ) -> Selection:
        timestamp = _timestamp(at)
        evidence_ids = tuple(evidence_ids)
        with _transaction(self._connection):
            record = _execute(
                self._connection, "SELECT * FROM source_records WHERE record_id = ?", (record_id,),
            ).fetchone()
            if record is None:
                raise StoreRefusal("source-record-unknown")
            expected_kind = "retraction" if outcome == "retraction" else "projection"
            if (
                outcome not in ("projection", "retraction", "conflict")
                or (len(evidence_ids) < 2 if outcome == "conflict" else len(evidence_ids) != 1)
                or any(type(identity) is not int for identity in evidence_ids)
                or len(set(evidence_ids)) != len(evidence_ids)
            ):
                raise StoreRefusal("selection-evidence-invalid")
            # Check only the selected rows and the indexed head while holding
            # the write lock; a selection need not load the record's history.
            for identity in evidence_ids:
                if _execute(
                    self._connection,
                    "SELECT 1 FROM source_evidence WHERE evidence_id = ? AND kind = ? AND "
                    "(record_id = ? OR (kind = 'retraction' AND record_type = ? AND source_identity = ?))",
                    (identity, expected_kind, record_id, record["record_type"], record["source_identity"]),
                ).fetchone() is None:
                    raise StoreRefusal("selection-evidence-invalid")
            current_head = _execute(
                self._connection,
                "SELECT " + _EVIDENCE_HEAD_SQL + " FROM source_records r WHERE r.record_id = ?", (record_id,),
            ).fetchone()[0]
            if evidence_head != current_head:
                raise StoreRefusal("selection-stale")
            latest = _execute(
                self._connection,
                "SELECT * FROM source_selections WHERE record_id = ? ORDER BY selection_id DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            if latest is not None:
                selection = _selection(self._connection, latest)
                if (selection.outcome, selection.evidence_ids, selection.evidence_head) == (
                    outcome, evidence_ids, evidence_head,
                ):
                    return selection
            if _sample_free_bytes(self._free_bytes, self._path) < self._reserve_bytes:
                raise StoreRetryable("storage-reserve-exhausted")
            selection_id = _execute(
                self._connection,
                "INSERT INTO source_selections (record_id, outcome, evidence_head, recorded_at) VALUES (?, ?, ?, ?)",
                (record_id, outcome, evidence_head, timestamp),
            ).lastrowid
            assert selection_id is not None
            for index, identity in enumerate(evidence_ids):
                _execute(
                    self._connection,
                    "INSERT INTO selection_evidence (selection_id, evidence_index, evidence_id) VALUES (?, ?, ?)",
                    (selection_id, index, identity),
                )
            return Selection(
                selection_id, cast(Literal["projection", "retraction", "conflict"], outcome),
                evidence_ids, evidence_head, _datetime(timestamp),
            )

    def _processing_write_reserve(self) -> None:
        if _sample_free_bytes(self._free_bytes, self._path) < self._reserve_bytes:
            raise StoreRetryable("storage-reserve-exhausted")

    def enqueue_processing(
        self, record_id: int, selection_id: int, processor_version: str, max_attempts: int, at: datetime
    ) -> ProcessingJob:
        timestamp = _timestamp(at)
        _processing_bound(max_attempts, 100)
        if not isinstance(processor_version, str) or re.fullmatch(
            r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*@[0-9]+", processor_version,
        ) is None:
            raise StoreRefusal("processor-version-invalid")
        with _transaction(self._connection):
            row = _execute(
                self._connection, "SELECT * FROM processing_jobs WHERE record_id = ? AND processor_version = ? "
                "AND state IN ('queued', 'leased')", (record_id, processor_version),
            ).fetchone()
            if row is not None:
                return _processing_job(self._connection, row)
            if _execute(self._connection, "SELECT 1 FROM source_records WHERE record_id = ?", (record_id,)).fetchone() is None:
                raise StoreRefusal("source-record-unknown")
            selection = _execute(
                self._connection, "SELECT selection_id, outcome FROM source_selections WHERE selection_id = ? AND record_id = ?",
                (selection_id, record_id),
            ).fetchone()
            if selection is None:
                raise StoreRefusal("selection-unknown")
            latest = _latest_processing_selection(self._connection, record_id)
            assert latest is not None
            if latest["selection_id"] != selection_id:
                raise StoreRefusal("selection-not-latest")
            if selection["outcome"] != "projection":
                raise StoreRefusal("selection-not-projection")
            self._processing_write_reserve()
            job_id = _execute(
                self._connection, "INSERT INTO processing_jobs "
                "(record_id, processor_version, selection_id, state, max_attempts, not_before, enqueued_at) "
                "VALUES (?, ?, ?, 'queued', ?, ?, ?)",
                (record_id, processor_version, selection_id, max_attempts, timestamp, timestamp),
            ).lastrowid
            row = _execute(self._connection, "SELECT * FROM processing_jobs WHERE job_id = ?", (job_id,)).fetchone()
            return _processing_job(self._connection, row)

    def claim_processing_job(self, owner: str, lease_seconds: int, at: datetime) -> Lease | None:
        timestamp = _timestamp(at)
        _processing_bound(lease_seconds, 86_400)
        if not isinstance(owner, str) or not owner:
            raise StoreRefusal("processing-request-invalid")
        expiry = _timestamp(at.astimezone(timezone.utc) + timedelta(seconds=lease_seconds))
        with _transaction(self._connection):
            reserve_checked = False
            while True:
                job = _execute(self._connection, _CLAIM_PROCESSING_SQL, (timestamp, timestamp)).fetchone()
                if job is None:
                    return None
                # Expiry exhaustion and supersession also write. Gate them before
                # closing an attempt, even if this call never returns a lease.
                if not reserve_checked:
                    self._processing_write_reserve()
                    reserve_checked = True
                if job["state"] == "leased":
                    _finish_processing_attempt(self._connection, job, "lease-expired", None, timestamp)
                    if job["attempts"] >= job["max_attempts"]:
                        _close_processing_job(
                            self._connection, job["job_id"], "failed", timestamp, "lease-expired-retries-exhausted",
                        )
                        continue
                latest = _latest_processing_selection(self._connection, job["record_id"])
                assert latest is not None
                if latest["outcome"] != "projection":
                    _close_processing_job(self._connection, job["job_id"], "superseded", timestamp)
                    continue
                fence = job["fence"] + 1
                _execute(
                    self._connection, "UPDATE processing_jobs SET selection_id = ?, state = 'leased', "
                    "attempts = attempts + 1, fence = ?, lease_owner = ?, lease_expires_at = ? WHERE job_id = ?",
                    (latest["selection_id"], fence, owner, expiry, job["job_id"]),
                )
                _execute(
                    self._connection, "INSERT INTO processing_attempts (job_id, fence, owner, claimed_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)", (job["job_id"], fence, owner, timestamp, expiry),
                )
                row = _held_processing_job(self._connection, job["job_id"], fence)
                return _processing_lease(self._connection, row)

    def renew_processing_lease(self, job_id: int, fence: int, lease_seconds: int, at: datetime) -> Lease:
        _timestamp(at)
        _processing_bound(lease_seconds, 86_400)
        expiry = _timestamp(at.astimezone(timezone.utc) + timedelta(seconds=lease_seconds))
        with _transaction(self._connection):
            _held_processing_job(self._connection, job_id, fence)
            self._processing_write_reserve()
            _execute(
                self._connection, "UPDATE processing_jobs SET lease_expires_at = ? WHERE job_id = ?", (expiry, job_id),
            )
            return _processing_lease(self._connection, _held_processing_job(self._connection, job_id, fence))

    def complete_processing_job(
        self, job_id: int, fence: int, outcome: OutcomeEntry, at: datetime
    ) -> ProcessingOutcome:
        timestamp = _timestamp(at)
        with _transaction(self._connection):
            job = _held_processing_job(self._connection, job_id, fence)
            _validate_processing_outcome(outcome, job["processor_version"])
            row = _execute(
                self._connection, "SELECT * FROM processing_outcomes WHERE selection_id = ? "
                "AND processor_version = ? AND policy_identity = ?",
                (job["selection_id"], job["processor_version"], outcome.policy_identity),
            ).fetchone()
            recorded = None if row is None else _processing_outcome(self._connection, row)
            if recorded is not None and _outcome_entry(recorded) != outcome:
                raise StoreRefusal("outcome-conflict")
            self._processing_write_reserve()
            if recorded is None:
                outcome_id = _execute(
                    self._connection, "INSERT INTO processing_outcomes "
                    "(record_id, selection_id, evidence_id, processor_version, policy_identity, kind, reason, "
                    "pointer, policy_kind, job_id, fence, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (job["record_id"], job["selection_id"], _processing_evidence(self._connection, job["selection_id"]),
                     job["processor_version"], outcome.policy_identity, outcome.kind, outcome.reason, outcome.pointer,
                     outcome.policy_kind, job_id, fence, timestamp),
                ).lastrowid
                revision = outcome.revision
                if revision is not None:
                    _execute(
                        self._connection, "INSERT INTO canonical_revisions "
                        "(outcome_id, header_bytes, body_bytes, body_version, start_epoch_ns, end_epoch_ns, "
                        "start_local_date, end_local_date, start_attribution, end_attribution, header_sha256, body_sha256) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (outcome_id, revision.header_bytes, revision.body_bytes, revision.body_version,
                         revision.start_epoch_ns, revision.end_epoch_ns, revision.start_local_date.isoformat(),
                         None if revision.end_local_date is None else revision.end_local_date.isoformat(),
                         revision.start_attribution, revision.end_attribution,
                         hashlib.sha256(revision.header_bytes).hexdigest(), hashlib.sha256(revision.body_bytes).hexdigest()),
                    )
                row = _execute(self._connection, "SELECT * FROM processing_outcomes WHERE outcome_id = ?", (outcome_id,)).fetchone()
                recorded = _processing_outcome(self._connection, row)
            _finish_processing_attempt(self._connection, job, "completed", None, timestamp)
            latest = _latest_processing_selection(self._connection, job["record_id"])
            assert latest is not None
            if latest["outcome"] != "projection":
                _close_processing_job(self._connection, job_id, "superseded", timestamp)
            elif latest["selection_id"] == job["selection_id"]:
                _close_processing_job(self._connection, job_id, "completed", timestamp)
            else:
                # Successful completion against older evidence starts a fresh
                # retry budget. Claim-time retargeting deliberately does not.
                _execute(
                    self._connection, "UPDATE processing_jobs SET selection_id = ?, state = 'queued', attempts = 0, "
                    "not_before = ?, lease_owner = NULL, lease_expires_at = NULL, failure = NULL, closed_at = NULL "
                    "WHERE job_id = ?", (latest["selection_id"], timestamp, job_id),
                )
            return recorded

    def fail_processing_job(
        self, job_id: int, fence: int, kind: FailureKind, reason: str, retry_at: datetime | None, at: datetime
    ) -> ProcessingJob:
        timestamp = _timestamp(at)
        retry_timestamp = None if retry_at is None else _timestamp(retry_at)
        if (
            kind not in ("transient", "deterministic") or not _kebab(reason)
            or (kind == "transient" and (retry_timestamp is None or retry_timestamp < timestamp))
            or (kind == "deterministic" and retry_timestamp is not None)
        ):
            raise StoreRefusal("processing-request-invalid")
        with _transaction(self._connection):
            job = _held_processing_job(self._connection, job_id, fence)
            self._processing_write_reserve()
            _finish_processing_attempt(self._connection, job, cast(AttemptResult, kind + "-failure"), reason, timestamp)
            if kind == "deterministic":
                _close_processing_job(self._connection, job_id, "failed", timestamp, reason)
            elif job["attempts"] >= job["max_attempts"]:
                _close_processing_job(self._connection, job_id, "failed", timestamp, "retries-exhausted")
            else:
                _execute(
                    self._connection, "UPDATE processing_jobs SET state = 'queued', not_before = ?, "
                    "lease_owner = NULL, lease_expires_at = NULL, failure = NULL, closed_at = NULL WHERE job_id = ?",
                    (retry_timestamp, job_id),
                )
            row = _execute(self._connection, "SELECT * FROM processing_jobs WHERE job_id = ?", (job_id,)).fetchone()
            return _processing_job(self._connection, row)

    def quarantine_item(self, position: ItemPosition, reason: str, pointer: str, at: datetime) -> ItemQuarantine:
        timestamp = _timestamp(at)
        if not _kebab(reason) or not _pointer(pointer):
            raise StoreRefusal("processing-request-invalid")
        parameters = (position.installation_id, position.sequence, position.item_index)
        with _transaction(self._connection):
            row = _execute(
                self._connection, "SELECT projection IS NOT NULL AS is_projection FROM batch_items "
                "WHERE installation_id = ? AND sequence = ? AND item_index = ?", parameters,
            ).fetchone()
            if row is None:
                raise StoreRefusal("archived-item-unknown")
            if not row["is_projection"]:
                raise StoreRefusal("index-entry-mismatch")
            if _execute(
                self._connection, "SELECT 1 FROM source_evidence WHERE installation_id = ? AND sequence = ? AND item_index = ?",
                parameters,
            ).fetchone() is not None:
                raise StoreRefusal("item-already-indexed")
            existing = _item_quarantine(self._connection, position)
            if existing is not None:
                if (existing.reason, existing.pointer) != (reason, pointer):
                    raise StoreRefusal("item-quarantine-conflict")
                return existing
            self._processing_write_reserve()
            _execute(
                self._connection, "INSERT INTO item_quarantines "
                "(installation_id, sequence, item_index, reason, pointer, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                parameters + (reason, pointer, timestamp),
            )
            return ItemQuarantine(position, reason, pointer, _datetime(timestamp))

    # ---- operational-store@5 stub ----

    def record_fallback_timezone_policy(
        self,
        zone: str,
        effective_from: datetime | None,
        supersedes: tuple[int, ...],
        processor_versions: Mapping[str, str],
        max_attempts: int,
        at: datetime,
    ) -> FallbackTimezoneRecording:
        raise NotImplementedError("operational-store@5")

    def record_source_selection_policy(
        self,
        measure: str,
        record_type: str,
        origins: tuple[OriginKey, ...],
        first_date: date | None,
        last_date: date | None,
        supersedes: tuple[int, ...],
        at: datetime,
    ) -> SourceSelectionPolicyRecord:
        raise NotImplementedError("operational-store@5")

    def read(self) -> AbstractContextManager[StoreView]:
        return self._read_view()

    @contextmanager
    def _read_view(self) -> Iterator[StoreView]:
        connection = _connect(self._path, self._busy_timeout_ms)
        try:
            _configure(connection, self._busy_timeout_ms)
            _execute(connection, "PRAGMA query_only = ON")
            _execute(connection, "BEGIN")
            # BEGIN alone does not establish a WAL snapshot.
            _execute(connection, "SELECT count(*) FROM sqlite_schema").fetchone()
            view = StoreView()
            view._connection = connection
            view._path = self._path
            view._reserve_bytes = self._reserve_bytes
            view._free_bytes = self._free_bytes
            yield view
        finally:
            try:
                if connection.in_transaction:
                    connection.rollback()
            finally:
                connection.close()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> OperationalStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def open_store(
    path: str | PathLike[str],
    *,
    reserve_bytes: int,
    free_bytes: Callable[[str | PathLike[str]], int] | None = None,
    busy_timeout_ms: int = 5000,
) -> OperationalStore:
    """Create a database at the baseline schema or open one already at it."""
    if sqlite3.sqlite_version_info < (3, 51, 3):
        raise StoreRefusal("sqlite-runtime-unsupported")
    connection = _connect(path, busy_timeout_ms)
    try:
        # Validate before WAL can change the persistent journal mode.
        _execute(connection, "BEGIN")
        try:
            existing = _validate_schema(connection, path)
        finally:
            connection.rollback()
        _configure(connection, busy_timeout_ms)
        if not existing:
            with _transaction(connection):
                # A concurrent opener may have created the baseline while we waited.
                if not _validate_schema(connection, path):
                    for version, sql in MIGRATIONS:
                        for statement in sql.split(";"):
                            if statement.strip():
                                _execute(connection, statement)
                        _execute(
                            connection, "INSERT INTO store_migrations (version, checksum, applied_at) VALUES (?, ?, ?)",
                            (version, hashlib.sha256(sql.encode("utf-8")).hexdigest(), _timestamp(datetime.now(timezone.utc))),
                        )
                        _execute(connection, f"PRAGMA user_version = {version}")
        # No schema refusal can occur after changing the persistent journal mode.
        _enable_wal(connection, busy_timeout_ms)
        store = OperationalStore()
        store._connection = connection
        store._path = path
        store._reserve_bytes = reserve_bytes
        store._free_bytes = _free_disk_bytes if free_bytes is None else free_bytes
        store._busy_timeout_ms = busy_timeout_ms
        return store
    except BaseException:
        connection.close()
        raise
