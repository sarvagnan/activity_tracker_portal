"""Accept one delivered capability acknowledgement (accept-capability-acknowledgement@1)."""

from __future__ import annotations

from datetime import datetime

from activity_tracker_portal import ingest_protocol
from activity_tracker_portal.accept_ingest_batch import DeliveryResponse
from activity_tracker_portal.operational_store import (
    MessageHeader,
    OperationalStore,
    StoreRefusal,
    StoreRetryable,
)


def accept_capability_acknowledgement(store: OperationalStore, data: bytes, now: datetime) -> DeliveryResponse:
    """Answer one valid capability acknowledgement, or raise for a caller error."""
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    try:
        message = ingest_protocol.decode_message(data)
    except ingest_protocol.ProtocolRefusal as refusal:
        raise ValueError("data must be a valid capability acknowledgement") from refusal
    if message["kind"] != "capability-acknowledgement":
        raise ValueError("other message kinds belong to their own accept units")

    sha = ingest_protocol.message_sha256(data)
    try:
        try:
            response = _accept_decoded_acknowledgement(store, message, data, sha, now)
        except StoreRefusal as refusal:
            response = _answer_store_refusal(store, message, sha, refusal)

        # Record once, after closing the read snapshot. A block must never be
        # returned without its durable evidence, including on repeated delivery.
        if response.outcome == "block":
            block = ingest_protocol.decode_message(response.body)
            store.record_delivery_block(
                block["sender_installation"], block["sequence"],
                block["message_sha256"], block["failure"], now,
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


def _accept_decoded_acknowledgement(
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
    # PortalWriter serializes the read/archive pair. The store rechecks its
    # invariants and checks the volatile reserve last inside the transaction.
    stored_receipt = store.archive_capability_acknowledgement(header)
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
        "message-digest-mismatch": "malformed-message",
        "sequence-content-conflict": "sequence-content-mismatch",
        "sequence-out-of-order": "sequence-out-of-order",
        "generation-unknown": "unsupported-wire-protocol",
    }
    failure = block_failures.get(refusal.failure)
    if failure is None:
        raise refusal
    return _block(message, sha, failure)
