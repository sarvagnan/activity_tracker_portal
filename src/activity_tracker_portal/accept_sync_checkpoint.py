"""Accept one delivered Sync Checkpoint and report polling state (accept-sync-checkpoint@1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from activity_tracker_portal import ingest_protocol
from activity_tracker_portal.accept_ingest_batch import DeliveryResponse
from activity_tracker_portal.operational_store import (
    ArchivedCheckpoint,
    MessageHeader,
    RecordTypeOutcome,
    OperationalStore,
    StoreRefusal,
    StoreRetryable,
    SyncCheckpointRecord,
)

Freshness = Literal["never-reported", "current", "delayed", "stale"]


@dataclass(frozen=True)
class SchedulingReport:
    """The last reported Background Scheduling Status, its Portal receipt time, and whether it is stale."""

    status: str
    receipt_time: datetime
    stale: bool


@dataclass(frozen=True)
class PollingState:
    """One Sender Installation's polling state at a Portal read time."""

    installation_id: str
    freshness: Freshness
    latest_checkpoint: ArchivedCheckpoint | None
    first_poll_in_progress: bool
    first_checkpoint_overdue: bool
    background_scheduling: SchedulingReport | None
    conditions: frozenset[str]


def accept_sync_checkpoint(store: OperationalStore, data: bytes, now: datetime) -> DeliveryResponse:
    """Answer one valid Sync Checkpoint, or raise ValueError for a caller error."""
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    try:
        message = ingest_protocol.decode_message(data)
    except ingest_protocol.ProtocolRefusal as refusal:
        raise ValueError("data must be a valid Sync Checkpoint") from refusal
    if message["kind"] != "sync-checkpoint":
        raise ValueError("other message kinds belong to their own accept units")

    sha = ingest_protocol.message_sha256(data)
    try:
        try:
            response = _accept_decoded_checkpoint(store, message, data, sha, now)
        except StoreRefusal as refusal:
            response = _answer_store_refusal(store, message, sha, refusal)

        # Record once, after closing the read snapshot. A block must never be
        # returned without its durable evidence, including on repeated delivery.
        if response.outcome == "block":
            assert response.failure is not None
            store.record_delivery_block(
                message["sender_installation"], message["sequence"], sha, response.failure, now,
            )
        return response
    except StoreRetryable as retryable:
        # Covers reads, archival, defensive rechecks, and block recording.
        return _retryable(retryable.failure)


def _retryable(failure: str) -> DeliveryResponse:
    reason = "storage-reserve-exhausted" if failure == "storage-reserve-exhausted" else "temporarily-unavailable"
    body = ingest_protocol.encode_message({"wire_version": 1, "kind": "retryable-failure", "reason": reason})
    return DeliveryResponse("retryable", body, failure)


def _block(message: ingest_protocol.Message, sha: str, failure: str) -> DeliveryResponse:
    body = ingest_protocol.encode_message({
        "wire_version": 1, "kind": "delivery-block", "failure": failure,
        "sender_installation": message["sender_installation"], "sequence": message["sequence"],
        "message_sha256": sha,
    })
    return DeliveryResponse("block", body, failure)


def _accept_decoded_checkpoint(
    store: OperationalStore, message: ingest_protocol.Message, data: bytes, sha: str, now: datetime,
) -> DeliveryResponse:
    installation, sequence = message["sender_installation"], message["sequence"]
    with store.read() as view:
        archived = view.archived_message(installation, sequence)
        if archived is not None:
            if archived.message_sha256 == sha:
                return DeliveryResponse("receipt", archived.receipt, None)
            return _block(message, sha, "sequence-content-mismatch")

        status = view.installation(installation)
        if status.state == "pending":
            return _retryable("installation-pending")
        if status.state in ("retired", "withdrawn"):
            return _retryable("installation-ended")
        highest = status.highest_sequence or 0
        if sequence < highest:
            return _block(message, sha, "sequence-out-of-order")

        generation = next(
            (item for item in view.honored_generations() if item.generation == message["capability_generation"]), None,
        )
        if generation is None:
            return _block(message, sha, "unsupported-wire-protocol")
        advertisement = ingest_protocol.decode_message(generation.advertisement)
        if message["wire_version"] not in advertisement["wire_versions"]:
            return _block(message, sha, "unsupported-wire-protocol")

    receipt = ingest_protocol.encode_message({
        "wire_version": 1, "kind": "delivery-receipt", "sender_installation": installation,
        "sequence": sequence, "message_sha256": sha,
    })
    header = MessageHeader(
        installation_id=installation, sequence=sequence, message_sha256=sha,
        staged_generation=message["capability_generation"], receipt_time=now, envelope=data, receipt=receipt,
    )
    checkpoint = SyncCheckpointRecord(
        run_sequence=message["run_sequence"], trigger=message["trigger"],
        started_at=message["started_at"], ended_at=message["ended_at"], elapsed_ms=message.get("elapsed_ms"),
        background_scheduling=message["background_scheduling"],
        record_types=tuple(
            RecordTypeOutcome(item["record_type"], item["outcome"], item.get("reason"), item["source_data_staged"])
            for item in message["record_types"]
        ),
        outbox_messages_before_checkpoint=message["outbox_messages_before_checkpoint"],
        outbox_bytes_before_checkpoint=message["outbox_bytes_before_checkpoint"],
    )
    # PortalWriter serializes the read/archive pair. The store rechecks its
    # invariants and checks the volatile reserve last inside the transaction.
    stored_receipt = store.archive_sync_checkpoint(header, checkpoint)
    return DeliveryResponse("receipt", stored_receipt, None, highest if sequence > highest + 1 else None)


def _answer_store_refusal(
    store: OperationalStore, message: ingest_protocol.Message, sha: str, refusal: StoreRefusal,
) -> DeliveryResponse:
    """Translate defensive store refusals as accept-ingest-batch Decision 9 does."""
    if refusal.failure == "installation-unknown":
        return _retryable("installation-unknown")
    if refusal.failure == "installation-not-delivering":
        try:
            with store.read() as view:
                status = view.installation(message["sender_installation"])
        except StoreRefusal as installation_refusal:
            if installation_refusal.failure == "installation-unknown":
                return _retryable("installation-unknown")
            raise
        return _retryable("installation-pending" if status.state == "pending" else "installation-ended")
    block_failures = {
        "sequence-content-conflict": "sequence-content-mismatch",
        "sequence-out-of-order": "sequence-out-of-order",
        "generation-unknown": "unsupported-wire-protocol",
    }
    failure = block_failures.get(refusal.failure)
    if failure is None:
        raise refusal
    return _block(message, sha, failure)


def polling_state(store: OperationalStore, installation_id: str, now: datetime) -> PollingState:
    """Report one installation's polling state at `now`, writing nothing."""
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    with store.read() as view:
        status = view.installation(installation_id)
        checkpoints = view.sync_checkpoints(installation_id, limit=1)
    latest = checkpoints[0] if checkpoints else None
    if latest is None:
        overdue = status.activated_at is not None and now - status.activated_at > timedelta(hours=24)
        return PollingState(
            installation_id, "never-reported", None, status.last_delivery_at is not None, overdue, None, frozenset(),
        )

    age = now - latest.receipt_time
    freshness: Freshness = "current" if age <= timedelta(hours=6) else "delayed" if age <= timedelta(hours=24) else "stale"
    scheduling = SchedulingReport(latest.record.background_scheduling, latest.receipt_time, freshness == "stale")
    conditions = clock_conditions(latest, status.enrolled_at)
    if scheduling.status in ("restricted", "unregistered"):
        conditions |= {"background-scheduling-" + scheduling.status}
    return PollingState(installation_id, freshness, latest, False, False, scheduling, conditions)


def clock_conditions(checkpoint: ArchivedCheckpoint, enrolled_at: datetime) -> frozenset[str]:
    """The clock-skew conditions of one archived checkpoint for an installation enrolled at `enrolled_at`."""
    if not isinstance(enrolled_at, datetime) or enrolled_at.utcoffset() is None:
        raise ValueError("enrolled_at must be timezone-aware")
    started = _sender_microseconds(checkpoint.record.started_at)
    ended = _sender_microseconds(checkpoint.record.ended_at)
    tolerance = 300_000_000
    conditions = set()
    if ended - _portal_microseconds(checkpoint.receipt_time) > tolerance:
        conditions.add("sender-clock-ahead")
    if _portal_microseconds(enrolled_at) - ended > tolerance:
        conditions.add("sender-clock-behind")
    elapsed = checkpoint.record.elapsed_ms
    if (elapsed is None and started - ended > tolerance) or (
        elapsed is not None and abs(ended - started - elapsed * 1000) > tolerance
    ):
        conditions.add("sender-times-inconsistent")
    return frozenset(conditions)


def _portal_microseconds(value: datetime) -> int:
    offset = value.utcoffset()
    assert offset is not None
    seconds = ((value.toordinal() * 24 + value.hour) * 60 + value.minute) * 60 + value.second
    return seconds * 1_000_000 + value.microsecond - offset // timedelta(microseconds=1)


def _sender_microseconds(value: str) -> int:
    # Extend datetime's ordinal calendar through year 0000, including its leap
    # day. Integer microseconds keep the five-minute boundary exact at any year.
    year, month, day = int(value[:4]), int(value[5:7]), int(value[8:10])
    previous_year = year - 1
    ordinal = 365 * previous_year + previous_year // 4 - previous_year // 100 + previous_year // 400 + day
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    months = (31, 28 + int(leap), 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    ordinal += sum(months[:month - 1])
    seconds = ((ordinal * 24 + int(value[11:13])) * 60 + int(value[14:16])) * 60 + int(value[17:19])
    return seconds * 1_000_000 + int(value[20:23]) * 1000
