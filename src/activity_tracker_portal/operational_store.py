"""Operational Store (operational-store@1): ingest-side transactional invariants.

The store is protocol-agnostic. Callers decode and evaluate Wire Protocol
messages and pass explicit archival records with the exact envelope and
receipt bytes; the store enforces the invariants that must hold however its
callers behave, inside one SQLite transaction per operation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from os import PathLike
from typing import Literal

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
MIGRATIONS: tuple[tuple[int, str], ...] = ()

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


class StoreView:
    """Reads bound to one SQLite read transaction."""

    def installation(self, installation_id: str) -> InstallationStatus:
        raise NotImplementedError

    def installations(self) -> tuple[InstallationStatus, ...]:
        raise NotImplementedError

    def installation_events(self, installation_id: str) -> tuple[LifecycleEvent, ...]:
        raise NotImplementedError

    def accepted_envelopes(self, installation_id: str) -> Iterator[tuple[int, bytes]]:
        raise NotImplementedError

    def archived_message(self, installation_id: str, sequence: int) -> ArchivedMessage | None:
        raise NotImplementedError

    def archived_items(self, installation_id: str, sequence: int) -> tuple[BatchItem, ...]:
        raise NotImplementedError

    def honored_generations(self) -> tuple[HonoredGeneration, ...]:
        raise NotImplementedError

    def latest_acknowledged_generation(self) -> int | None:
        raise NotImplementedError

    def sequence_gaps(self, installation_id: str) -> tuple[SequenceGap, ...]:
        raise NotImplementedError

    def delivery_blocks(self, installation_id: str | None = None) -> tuple[DeliveryBlockRecord, ...]:
        raise NotImplementedError

    def sync_checkpoints(
        self, installation_id: str, *, before_sequence: int | None = None, limit: int = 50
    ) -> tuple[ArchivedCheckpoint, ...]:
        raise NotImplementedError

    def storage_reserve(self) -> ReserveStatus:
        raise NotImplementedError

    def settings(self) -> StoreSettings:
        raise NotImplementedError


class OperationalStore:
    """Deep transactional operations over one SQLite Operational Store file."""

    def settings(self) -> StoreSettings:
        raise NotImplementedError

    def record_enrollment_offer(self, code_digest: str, expires_at: datetime, at: datetime) -> None:
        raise NotImplementedError

    def enroll_installation(self, code_digest: str, installation_id: str, fingerprint: str, at: datetime) -> None:
        raise NotImplementedError

    def activate_installation(self, installation_id: str, at: datetime) -> None:
        raise NotImplementedError

    def begin_draining(self, installation_id: str, at: datetime) -> None:
        raise NotImplementedError

    def retire_installation(self, installation_id: str, kind: RetirementKind, at: datetime) -> None:
        raise NotImplementedError

    def withdraw_installation(self, installation_id: str, at: datetime) -> None:
        raise NotImplementedError

    def honor_generation(self, generation: int, advertisement: bytes, at: datetime) -> None:
        raise NotImplementedError

    def stop_honoring_generation(self, generation: int, at: datetime) -> None:
        raise NotImplementedError

    def archive_ingest_batch(self, header: MessageHeader, batch: IngestBatchRecord) -> bytes:
        raise NotImplementedError

    def archive_completion(self, header: MessageHeader, completion: CompletionRecord) -> bytes:
        raise NotImplementedError

    def archive_sync_checkpoint(self, header: MessageHeader, checkpoint: SyncCheckpointRecord) -> bytes:
        raise NotImplementedError

    def archive_capability_acknowledgement(self, header: MessageHeader) -> bytes:
        raise NotImplementedError

    def record_delivery_block(
        self,
        installation_id: str | None,
        sequence: int | None,
        message_sha256: str,
        failure: str,
        at: datetime,
    ) -> None:
        raise NotImplementedError

    def read(self) -> AbstractContextManager[StoreView]:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

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
    raise NotImplementedError
