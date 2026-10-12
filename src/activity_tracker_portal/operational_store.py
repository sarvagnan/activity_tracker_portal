"""Operational Store (operational-store@1): ingest-side transactional invariants.

The store is protocol-agnostic. Callers decode and evaluate Wire Protocol
messages and pass explicit archival records with the exact envelope and
receipt bytes; the store enforces the invariants that must hold however its
callers behave, inside one SQLite transaction per operation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from os import PathLike
import shutil
import sqlite3
from typing import Literal, cast

from ._operational_store_sql import _BASELINE_SQL

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
    }
)
FAILURES = STARTUP_FAILURES | RETRYABLE_FAILURES | INVARIANT_FAILURES

# Ordered (version, exact SQL) pairs; store_migrations records the SHA-256 of each
# migration's UTF-8 SQL. The baseline is migration 1.
MIGRATIONS: tuple[tuple[int, str], ...] = ((1, _BASELINE_SQL),)

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
    _execute(connection, "PRAGMA journal_mode = WAL")
    _execute(connection, "PRAGMA synchronous = FULL")
    _execute(connection, "PRAGMA foreign_keys = ON")


def _migration_checksums() -> tuple[tuple[int, str], ...]:
    return tuple((version, hashlib.sha256(sql.encode("utf-8")).hexdigest()) for version, sql in MIGRATIONS)


def _validate_schema(connection: sqlite3.Connection) -> bool:
    """Return whether a schema exists, without changing persistent settings."""
    objects = _execute(connection, "SELECT name FROM sqlite_schema").fetchall()
    user_version = _execute(connection, "PRAGMA user_version").fetchone()[0]
    if not objects and user_version == 0:
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
            existing = _validate_schema(connection)
        finally:
            connection.rollback()
        _configure(connection, busy_timeout_ms)
        if not existing:
            with _transaction(connection):
                # A concurrent opener may have created the baseline while we waited.
                if not _validate_schema(connection):
                    for version, sql in MIGRATIONS:
                        for statement in sql.split(";"):
                            if statement.strip():
                                _execute(connection, statement)
                        _execute(
                            connection, "INSERT INTO store_migrations (version, checksum, applied_at) VALUES (?, ?, ?)",
                            (version, hashlib.sha256(sql.encode("utf-8")).hexdigest(), _timestamp(datetime.now(timezone.utc))),
                        )
                        _execute(connection, f"PRAGMA user_version = {version}")
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
