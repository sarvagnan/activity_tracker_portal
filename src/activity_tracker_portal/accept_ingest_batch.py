"""Accept one delivered Ingest Batch (accept-ingest-batch@1)."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from activity_tracker_portal import ingest_protocol
from activity_tracker_portal.operational_store import (
    BatchItem,
    IngestBatchRecord,
    MessageHeader,
    OperationalStore,
    StoreRefusal,
    StoreRetryable,
    StoreView,
)

Outcome = Literal["receipt", "retryable", "block"]


@dataclass(frozen=True)
class DeliveryResponse:
    """One protocol answer: its outcome, exact bytes, named failure, and any accepted sequence gap."""

    outcome: Outcome
    body: bytes
    failure: str | None
    gap_after: int | None = None


def accept_ingest_batch(store: OperationalStore, data: bytes, now: datetime) -> DeliveryResponse:
    """Answer one delivered Ingest Batch, or bytes that are not a valid Outbox Message of another kind."""
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    try:
        message = ingest_protocol.decode_message(data)
    except ingest_protocol.ProtocolRefusal:
        message = None
    if message is not None and message["kind"] in ingest_protocol.OUTBOX_KINDS - {"ingest-batch"}:
        raise ValueError("other Outbox Message kinds belong to their own accept units")

    try:
        if message is None or message["kind"] != "ingest-batch":
            evaluation = ingest_protocol.evaluate_delivery(ingest_protocol.new_delivery_state([]), data)
            response = DeliveryResponse("block", evaluation.response, evaluation.failure)
        else:
            sha = ingest_protocol.message_sha256(data)
            try:
                response = _accept_decoded_batch(store, message, data, sha, now)
            except StoreRefusal as refusal:
                response = _answer_store_refusal(store, message, sha, refusal)

        # Use the response's identity, including the protocol's identity-less
        # decode failures. Record exactly once, after the read view has closed.
        if response.outcome == "block":
            block = ingest_protocol.decode_message(response.body)
            store.record_delivery_block(
                block.get("sender_installation"), block.get("sequence"),
                block["message_sha256"], block["failure"], now,
            )
        return response
    except StoreRetryable as retryable:
        # This also covers contention while recording a block: no block may
        # escape without its durable evidence.
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


def _batch_failure(
    view: StoreView, message: ingest_protocol.Message, data: bytes, highest: int,
) -> str | None:
    """Apply protocol checks in order, using facts from one store snapshot."""
    if message["sequence"] < highest:
        return "sequence-out-of-order"
    generation = next(
        (item for item in view.honored_generations() if item.generation == message["capability_generation"]), None,
    )
    if generation is None:
        return "unsupported-wire-protocol"
    advertisement = ingest_protocol.decode_message(generation.advertisement)
    if message["wire_version"] not in advertisement["wire_versions"]:
        return "unsupported-wire-protocol"
    versions = next(
        (schema["versions"] for schema in advertisement["projection_schemas"]
         if schema["record_type"] == message["record_type"]), [],
    )
    if message["projection_schema_version"] not in versions:
        return "unsupported-source-projection-schema"
    if len(message["items"]) > advertisement["max_record_count"]:
        return "record-count-limit-exceeded"
    if len(data) > advertisement["max_message_bytes"]:
        return "uncompressed-byte-limit-exceeded"

    cycle = view.delivery_cycle(message["sender_installation"], message["cycle_id"])
    page, fragment = message["source_page_index"], message["fragment_index"]
    if cycle is None:
        continues = (
            page == 1 and fragment == 1
            and message["previous_chain"] == ingest_protocol.chain_start(message["cycle_id"])
        )
    else:
        # A completion-only cycle has no last fragment or chain inputs.
        continues = (
            cycle.completion_sequence is None
            and cycle.cycle_kind == message["cycle_kind"]
            and cycle.record_type == message["record_type"]
            and cycle.last_previous_chain is not None
            and cycle.last_message_sha256 is not None
            and message["previous_chain"] == ingest_protocol.chain_next(
                cycle.last_previous_chain, cycle.last_message_sha256,
            )
            and (page, fragment) in (
                (cycle.last_source_page_index, cycle.last_fragment_index + 1),
                (cycle.last_source_page_index + 1, 1),
            )
        )
    return None if continues else "fragment-order-mismatch"


def _accept_decoded_batch(
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
        failure = _batch_failure(view, message, data, highest)
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
    batch = IngestBatchRecord(
        record_type=message["record_type"], projection_schema_version=message["projection_schema_version"],
        cycle_kind=message["cycle_kind"], cycle_id=message["cycle_id"],
        source_page_index=message["source_page_index"], fragment_index=message["fragment_index"],
        previous_chain=message["previous_chain"],
        items=tuple(
            BatchItem(projection=base64.b64decode(item["data"])) if item["kind"] == "projection"
            else BatchItem(retracted_identity=item["source_identity"])
            for item in message["items"]
        ),
    )
    # PortalWriter serializes the read/archive pair. The store rechecks its
    # invariants and checks the volatile storage reserve inside this transaction.
    stored_receipt = store.archive_ingest_batch(header, batch)
    return DeliveryResponse("receipt", stored_receipt, None, highest if sequence > highest + 1 else None)


def _answer_store_refusal(
    store: OperationalStore, message: ingest_protocol.Message, sha: str, refusal: StoreRefusal,
) -> DeliveryResponse:
    """Translate the store's defensive rechecks into delivery outcomes."""
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
        "cycle-already-completed": "fragment-order-mismatch",
    }
    failure = block_failures.get(refusal.failure)
    if failure is None:
        raise refusal
    return _block(message, sha, failure)
