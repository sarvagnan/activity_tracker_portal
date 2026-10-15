"""Accept one delivered Snapshot Completion or Change Completion (accept-sync-completion@1)."""

from __future__ import annotations

from datetime import datetime

from activity_tracker_portal import ingest_protocol
from activity_tracker_portal.accept_ingest_batch import DeliveryResponse
from activity_tracker_portal.operational_store import (
    CompletionRecord,
    MessageHeader,
    OperationalStore,
    StoreRefusal,
    StoreRetryable,
    StoreView,
)


def accept_sync_completion(store: OperationalStore, data: bytes, now: datetime) -> DeliveryResponse:
    """Answer one valid completion, or raise ValueError for a caller error."""
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    try:
        message = ingest_protocol.decode_message(data)
    except ingest_protocol.ProtocolRefusal as refusal:
        raise ValueError("data must be a valid Snapshot Completion or Change Completion") from refusal
    if message["kind"] not in ("snapshot-completion", "change-completion"):
        raise ValueError("other message kinds belong to their own accept units")

    sha = ingest_protocol.message_sha256(data)
    try:
        try:
            response = _accept_decoded_completion(store, message, data, sha, now)
        except StoreRefusal as refusal:
            response = _answer_store_refusal(store, message, sha, refusal)

        # Record exactly once after the read view closes, including repeated
        # blocks. A block may never escape without its durable evidence.
        if response.outcome == "block":
            assert response.failure is not None
            store.record_delivery_block(
                message["sender_installation"], message["sequence"], sha, response.failure, now,
            )
        return response
    except StoreRetryable as retryable:
        # Includes contention during reads, archival, rechecks, or block recording.
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


def _completion_failure(view: StoreView, message: ingest_protocol.Message, highest: int) -> str | None:
    """Check sequence, capabilities, and the ordered proof from one snapshot."""
    sequence = message["sequence"]
    if sequence < highest:
        return "sequence-out-of-order"
    generation = next(
        (item for item in view.honored_generations() if item.generation == message["capability_generation"]), None,
    )
    if generation is None:
        return "unsupported-wire-protocol"
    advertisement = ingest_protocol.decode_message(generation.advertisement)
    if message["wire_version"] not in advertisement["wire_versions"]:
        return "unsupported-wire-protocol"

    cycle = view.delivery_cycle(message["sender_installation"], message["cycle_id"])
    if cycle is None:
        counts = (0, 0, 0)
    else:
        if cycle.completion_sequence is not None or cycle.record_type != message["record_type"]:
            return "completion-identity-mismatch"
        expected_kind = ingest_protocol.COMPLETION_CYCLE_KINDS[(message["kind"], message.get("snapshot_kind"))]
        if cycle.cycle_kind != expected_kind:
            return "completion-kind-mismatch"
        # An uncompleted cycle has fragments. Recorded gaps describe missing
        # [gap_after + 1, sequence); intersect them with [first, completion).
        assert cycle.first_sequence is not None
        if sequence > highest + 1 or any(
            max(cycle.first_sequence, gap.gap_after + 1) < min(sequence, gap.sequence)
            for gap in view.sequence_gaps(message["sender_installation"])
        ):
            return "completion-sequence-gap"
        counts = (cycle.last_source_page_index, cycle.fragment_count, cycle.record_count)

    if (message["page_count"], message["fragment_count"], message["record_count"]) != counts:
        return "completion-count-mismatch"
    if cycle is None:
        chain = ingest_protocol.chain_start(message["cycle_id"])
    else:
        assert cycle.last_previous_chain is not None and cycle.last_message_sha256 is not None
        chain = ingest_protocol.chain_next(cycle.last_previous_chain, cycle.last_message_sha256)
    if message["final_chain"] != chain:
        return "completion-chain-mismatch"
    return None


def _accept_decoded_completion(
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
        failure = _completion_failure(view, message, highest)
        if failure is not None:
            return _block(message, sha, failure)

    receipt = ingest_protocol.encode_message({
        "wire_version": 1, "kind": "delivery-receipt", "sender_installation": installation,
        "sequence": sequence, "message_sha256": sha,
    })
    header = MessageHeader(
        installation_id=installation, sequence=sequence, message_sha256=sha,
        staged_generation=message["capability_generation"], receipt_time=now, envelope=data, receipt=receipt,
    )
    completion = CompletionRecord(
        completion_kind="snapshot" if message["kind"] == "snapshot-completion" else "change",
        snapshot_kind=message.get("snapshot_kind"), record_type=message["record_type"], cycle_id=message["cycle_id"],
        page_count=message["page_count"], fragment_count=message["fragment_count"],
        record_count=message["record_count"], final_chain=message["final_chain"],
    )
    # PortalWriter serializes the read/archive pair. The store rechecks its
    # invariants and samples the storage reserve last inside its transaction.
    stored_receipt = store.archive_completion(header, completion)
    return DeliveryResponse("receipt", stored_receipt, None, highest if sequence > highest + 1 else None)


def _answer_store_refusal(
    store: OperationalStore, message: ingest_protocol.Message, sha: str, refusal: StoreRefusal,
) -> DeliveryResponse:
    """Translate only defensive refusals covered by this unit's contract."""
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
        "cycle-already-completed": "completion-identity-mismatch",
    }
    failure = block_failures.get(refusal.failure)
    if failure is None:
        raise refusal
    return _block(message, sha, failure)
