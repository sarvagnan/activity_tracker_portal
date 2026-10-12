#!/usr/bin/env python3
"""Build the Wire Protocol Version 1 test vectors.

Fixture tooling only: production code must not import this module. Every
expected outcome is written here by hand. The builder only encodes messages,
computes hashes and chain values, and asserts the stated property of each
case, so a mistaken case fails loudly instead of producing a wrong vector.

Usage: build_vectors.py [--check]
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

VECTOR_DIR = Path(__file__).resolve().parents[1] / "vectors"
MAX_INTEGER = 2**53 - 1
BOTH = ["python", "typescript"]

I1 = "00000000-0000-4000-8000-000000000001"
I2 = "00000000-0000-4000-8000-000000000002"
ENROLLMENT_CODE = "SYNTHETICCODE234"
PORTAL_URL = "http://portal.synthetic.invalid:8000"


def cycle(number: int) -> str:
    return f"00000000-0000-4000-8000-1{number:011d}"


# Encoding and identity


def _check_domain(value: object) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, str):
        value.encode("utf-8")  # Refuses lone surrogates.
        return
    if isinstance(value, int):
        assert 0 <= value <= MAX_INTEGER, value
        return
    if isinstance(value, list):
        for item in value:
            _check_domain(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str) and key.isascii(), key
            _check_domain(item)
        return
    raise AssertionError(f"value outside the envelope domain: {value!r}")


def canonical(value: object) -> bytes:
    _check_domain(value)
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def chain_start(cycle_id: str) -> str:
    return sha256_hex(cycle_id.encode("utf-8"))


def chain_next(previous_chain: str, message_sha256: str) -> str:
    return sha256_hex(bytes.fromhex(previous_chain) + bytes.fromhex(message_sha256))


def fingerprint(sender_installation: str) -> str:
    return sha256_hex(sender_installation.encode("utf-8"))


def bytes_field(data: bytes) -> dict[str, str]:
    try:
        return {"text": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"base64": b64(data)}


# Message builders


def projection(label: str, padding: int = 0) -> dict[str, str]:
    raw = b'{"synthetic":' + json.dumps(label + "x" * padding).encode("ascii") + b"}"
    return {"kind": "projection", "data": b64(raw)}


def retraction(label: str) -> dict[str, str]:
    return {"kind": "retraction", "source_identity": f"synthetic-{label}"}


def advertisement(
    generation: int,
    schemas: dict[str, list[int]],
    *,
    max_record_count: int = 3,
    max_message_bytes: int = 4096,
    wire_versions: tuple[int, ...] = (1,),
) -> dict[str, object]:
    return {
        "wire_version": 1,
        "kind": "capability-advertisement",
        "generation": generation,
        "wire_versions": list(wire_versions),
        "projection_schemas": [
            {"record_type": record_type, "versions": versions}
            for record_type, versions in sorted(schemas.items())
        ],
        "max_record_count": max_record_count,
        "max_message_bytes": max_message_bytes,
    }


def outbox(kind: str, sequence: int, *, installation: str = I1, generation: int = 1, **fields: object) -> dict[str, object]:
    return {
        "wire_version": 1,
        "kind": kind,
        "sender_installation": installation,
        "sequence": sequence,
        "capability_generation": generation,
        **fields,
    }


def batch(
    sequence: int,
    *,
    cycle_id: str,
    page: int,
    fragment: int,
    previous_chain: str,
    items: list[dict[str, str]],
    record_type: str = "steps",
    schema: int = 1,
    cycle_kind: str = "initial-snapshot",
    installation: str = I1,
    generation: int = 1,
) -> dict[str, object]:
    return outbox(
        "ingest-batch",
        sequence,
        installation=installation,
        generation=generation,
        record_type=record_type,
        projection_schema_version=schema,
        cycle_kind=cycle_kind,
        cycle_id=cycle_id,
        source_page_index=page,
        fragment_index=fragment,
        previous_chain=previous_chain,
        items=items,
    )


def snapshot_completion(
    sequence: int,
    *,
    snapshot_kind: str,
    cycle_id: str,
    pages: int,
    fragments: int,
    records: int,
    final_chain: str,
    record_type: str = "steps",
    generation: int = 1,
) -> dict[str, object]:
    return outbox(
        "snapshot-completion",
        sequence,
        generation=generation,
        snapshot_kind=snapshot_kind,
        record_type=record_type,
        cycle_id=cycle_id,
        page_count=pages,
        fragment_count=fragments,
        record_count=records,
        final_chain=final_chain,
    )


def change_completion(
    sequence: int,
    *,
    cycle_id: str,
    pages: int,
    fragments: int,
    records: int,
    final_chain: str,
    record_type: str = "steps",
) -> dict[str, object]:
    return outbox(
        "change-completion",
        sequence,
        record_type=record_type,
        cycle_id=cycle_id,
        page_count=pages,
        fragment_count=fragments,
        record_count=records,
        final_chain=final_chain,
    )


def type_status(record_type: str, outcome: str, reason: str | None = None, staged: bool = False) -> dict[str, object]:
    status: dict[str, object] = {"record_type": record_type, "outcome": outcome, "source_data_staged": staged}
    if reason is not None:
        status["reason"] = reason
    return status


def checkpoint(
    sequence: int,
    *,
    trigger: str,
    scheduling: str,
    record_types: list[dict[str, object]],
    elapsed_ms: int | None = None,
    generation: int = 1,
    installation: str = I1,
) -> dict[str, object]:
    message = outbox(
        "sync-checkpoint",
        sequence,
        installation=installation,
        generation=generation,
        run_sequence=sequence,
        trigger=trigger,
        started_at="2030-01-01T00:00:00.000Z",
        ended_at="2030-01-01T00:01:30.250Z",
        background_scheduling=scheduling,
        record_types=sorted(record_types, key=lambda status: status["record_type"]),
        outbox_messages_before_checkpoint=4,
        outbox_bytes_before_checkpoint=18_432,
    )
    if elapsed_ms is not None:
        message["elapsed_ms"] = elapsed_ms
    return message


def simple_checkpoint(sequence: int, **overrides: object) -> dict[str, object]:
    return checkpoint(
        sequence,
        trigger="foreground",
        scheduling="restricted",
        record_types=[type_status("steps", "no-change")],
        **overrides,
    )


def acknowledgement(sequence: int, generation: int) -> dict[str, object]:
    return outbox("capability-acknowledgement", sequence, generation=generation)


def receipt_for(data: bytes) -> dict[str, object]:
    message = json.loads(data)
    return {
        "wire_version": 1,
        "kind": "delivery-receipt",
        "sender_installation": message["sender_installation"],
        "sequence": message["sequence"],
        "message_sha256": sha256_hex(data),
    }


def block_for(data: bytes, failure: str, *, identity: bool) -> dict[str, object]:
    block: dict[str, object] = {
        "wire_version": 1,
        "kind": "delivery-block",
        "failure": failure,
        "message_sha256": sha256_hex(data),
    }
    if identity:
        message = json.loads(data)
        block["sender_installation"] = message["sender_installation"]
        block["sequence"] = message["sequence"]
    return block


def retryable(reason: str) -> dict[str, object]:
    return {"wire_version": 1, "kind": "retryable-failure", "reason": reason}


def chained(cycle_id: str, specs: list[tuple[int, int, int, list[dict[str, str]]]], **fields: object) -> tuple[list[bytes], str]:
    """Encode consecutive fragments of one cycle and return them with the chain after them."""
    chain = chain_start(cycle_id)
    encoded = []
    for sequence, page, fragment, items in specs:
        data = canonical(batch(sequence, cycle_id=cycle_id, page=page, fragment=fragment, previous_chain=chain, items=items, **fields))
        encoded.append(data)
        chain = chain_next(chain, sha256_hex(data))
    return encoded, chain


# Positive messages

CHECKPOINT_FULL = checkpoint(
    5,
    trigger="background",
    scheduling="available",
    elapsed_ms=90_250,
    record_types=[
        type_status("active-calories-burned", "not-due"),
        type_status("blood-pressure", "no-change"),
        type_status("body-fat", "progressed", "time-budget-reached", staged=True),
        type_status("distance", "progressed", staged=True),
        type_status("exercise-session", "completed", staged=True),
        type_status("heart-rate", "deferred", "collection-disabled"),
        type_status("height", "deferred", "storage-pressure"),
        type_status("hydration", "deferred", "screen-lock-removed"),
        type_status("nutrition", "blocked", "projection-refusal"),
        type_status("oxygen-saturation", "blocked", "record-too-large"),
        type_status("respiratory-rate", "blocked", "delivery-blocked"),
        type_status("resting-heart-rate", "not-permitted"),
        type_status("sleep-session", "failed", "read-failed"),
        type_status("steps", "progressed", "changes-token-expired", staged=True),
        type_status("total-calories-burned", "interrupted"),
        type_status("weight", "interrupted", "permission-missing"),
    ],
)
CHECKPOINT_FOREGROUND = simple_checkpoint(6)
CHECKPOINT_MANUAL = checkpoint(7, trigger="manual", scheduling="unregistered", record_types=[], elapsed_ms=0)

ESCAPED_IDENTITY = 'synthetic-"quoted"-\\-ü-雪'
OFFER = {
    "wire_version": 1,
    "kind": "enrollment-offer",
    "enrollment_code": ENROLLMENT_CODE,
    "portal_url": PORTAL_URL,
    "expires_at": "2030-01-01T00:10:00.000Z",
}
CONFIRMATION = {
    "wire_version": 1,
    "kind": "enrollment-confirmation",
    "enrollment_code": ENROLLMENT_CODE,
    "sender_installation": I1,
    "fingerprint": fingerprint(I1),
}
ADVERTISEMENT = advertisement(1, {"heart-rate": [1], "steps": [1, 2]})
BATCH_PROJECTIONS = batch(
    2,
    cycle_id=cycle(1),
    page=1,
    fragment=1,
    previous_chain=chain_start(cycle(1)),
    items=[projection("steps-1"), projection("steps-2")],
)
BATCH_MIXED = batch(
    3,
    cycle_id=cycle(2),
    page=1,
    fragment=1,
    previous_chain=chain_start(cycle(2)),
    cycle_kind="change-cycle",
    record_type="heart-rate",
    items=[{"kind": "retraction", "source_identity": ESCAPED_IDENTITY}, projection("heart-rate-1")],
)
BATCH_EMPTY = batch(4, cycle_id=cycle(3), page=1, fragment=1, previous_chain=chain_start(cycle(3)), items=[])
BATCH_OPAQUE = batch(
    8,
    cycle_id=cycle(4),
    page=1,
    fragment=1,
    previous_chain=chain_start(cycle(4)),
    items=[{"kind": "projection", "data": b64(b'{"synthetic":\xff\xfe\x00')}],
)
SNAPSHOT_INITIAL = snapshot_completion(
    9, snapshot_kind="initial", cycle_id=cycle(1), pages=2, fragments=3, records=6, final_chain=chain_next(chain_start(cycle(1)), "0" * 64)
)
SNAPSHOT_RECONCILIATION = snapshot_completion(
    10, snapshot_kind="reconciliation", cycle_id=cycle(5), pages=1, fragments=1, records=2, final_chain=chain_next(chain_start(cycle(5)), "1" * 64)
)
CHANGE = change_completion(11, cycle_id=cycle(2), pages=1, fragments=1, records=2, final_chain=chain_next(chain_start(cycle(2)), "2" * 64), record_type="heart-rate")
CHANGE_EMPTY = change_completion(12, cycle_id=cycle(6), pages=0, fragments=0, records=0, final_chain=chain_start(cycle(6)))
ACKNOWLEDGEMENT = acknowledgement(1, 1)
RECEIPT = receipt_for(canonical(BATCH_PROJECTIONS))
BLOCK_WITH_IDENTITY = block_for(canonical(SNAPSHOT_INITIAL), "completion-chain-mismatch", identity=True)
BLOCK_WITHOUT_IDENTITY = block_for(b"\xff", "malformed-message", identity=False)

POSITIVE_MESSAGES: list[tuple[str, list[str], dict[str, object]]] = [
    ("enrollment-offer", ["enrollment"], OFFER),
    ("enrollment-confirmation", ["enrollment"], CONFIRMATION),
    ("capability-advertisement", ["capability"], ADVERTISEMENT),
    (
        "capability-advertisement-several-types",
        ["capability"],
        advertisement(2, {"heart-rate": [1], "sleep-session": [1], "steps": [1, 2]}, max_record_count=500, max_message_bytes=1_048_576),
    ),
    ("capability-acknowledgement", ["capability", "acknowledgement"], ACKNOWLEDGEMENT),
    ("ingest-batch-projections", ["ingest-items"], BATCH_PROJECTIONS),
    ("ingest-batch-retraction-and-escaping", ["ingest-items", "escaping"], BATCH_MIXED),
    ("ingest-batch-empty-page", ["ingest-items", "empty-page"], BATCH_EMPTY),
    ("ingest-batch-opaque-projection-bytes", ["ingest-items", "opaque"], BATCH_OPAQUE),
    ("snapshot-completion-initial", ["completion"], SNAPSHOT_INITIAL),
    ("snapshot-completion-reconciliation", ["completion"], SNAPSHOT_RECONCILIATION),
    ("change-completion", ["completion"], CHANGE),
    ("change-completion-empty-cycle", ["completion", "empty-cycle"], CHANGE_EMPTY),
    ("sync-checkpoint-every-outcome-and-reason", ["checkpoint"], CHECKPOINT_FULL),
    ("sync-checkpoint-without-elapsed-duration", ["checkpoint"], CHECKPOINT_FOREGROUND),
    ("sync-checkpoint-no-record-types", ["checkpoint"], CHECKPOINT_MANUAL),
    ("delivery-receipt", ["receipt"], RECEIPT),
    ("retryable-failure-storage-reserve-exhausted", ["retryable"], retryable("storage-reserve-exhausted")),
    ("retryable-failure-temporarily-unavailable", ["retryable"], retryable("temporarily-unavailable")),
    ("delivery-block-with-identity", ["block"], BLOCK_WITH_IDENTITY),
    ("delivery-block-without-identity", ["block"], BLOCK_WITHOUT_IDENTITY),
]


def build_messages() -> dict[str, object]:
    vectors = []
    for name, tags, value in POSITIVE_MESSAGES:
        data = canonical(value)
        vectors.append(
            {
                "name": name,
                "tags": tags,
                "value": value,
                "bytes": bytes_field(data),
                "sha256": sha256_hex(data),
            }
        )
    return {"description": "Positive messages: exact canonical bytes, identity, and decoded value.", "vectors": vectors}


# Refusals


def with_changes(message: dict[str, object], /, **changes: object) -> dict[str, object]:
    changed = dict(message)
    changed.update(changes)
    return changed


def without(message: dict[str, object], key: str) -> dict[str, object]:
    return {k: v for k, v in message.items() if k != key}


def entry_change(message: dict[str, object], index: int, /, **changes: object) -> dict[str, object]:
    record_types = [dict(status) for status in message["record_types"]]
    record_types[index].update(changes)
    return with_changes(message, record_types=record_types)


def replaced(data: bytes, old: bytes, new: bytes) -> bytes:
    assert data.count(old) == 1, old
    return data.replace(old, new)


def noncanonical_spacing(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(", ", ": ")).encode("utf-8")


def unsorted_keys(value: object) -> bytes:
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert data != canonical(value)
    return data


FULL = canonical(CHECKPOINT_FULL)
FOREGROUND = canonical(CHECKPOINT_FOREGROUND)
MALFORMED = "malformed-message"
NONCANONICAL = "noncanonical-message"
UNSUPPORTED_WIRE = "unsupported-wire-protocol"
STEPS_ITEM = projection("steps-1")

# (name, tags, bytes, failure, category). Tags "schema-invalid" and
# "rule-invalid" say whether the JSON Schema alone rejects the parsed value.
REFUSALS: list[tuple[str, list[str], bytes, str, str | None]] = [
    ("invalid-utf8", ["malformed", "lexical"], replaced(FOREGROUND, b'"foreground"', b'"fore\xffground"'), MALFORMED, None),
    ("byte-order-mark", ["malformed", "lexical"], b"\xef\xbb\xbf" + FOREGROUND, MALFORMED, None),
    ("trailing-value", ["malformed", "lexical"], FOREGROUND + b"{}", MALFORMED, None),
    (
        "duplicate-key",
        ["malformed", "lexical"],
        replaced(FOREGROUND, b'"trigger":"foreground"', b'"trigger":"foreground","trigger":"foreground"'),
        MALFORMED,
        None,
    ),
    ("nan-literal", ["malformed", "lexical"], replaced(FULL, b'"elapsed_ms":90250', b'"elapsed_ms":NaN'), MALFORMED, None),
    ("infinity-literal", ["malformed", "lexical"], replaced(FULL, b'"elapsed_ms":90250', b'"elapsed_ms":Infinity'), MALFORMED, None),
    ("fraction-number", ["malformed", "lexical"], replaced(FULL, b'"elapsed_ms":90250', b'"elapsed_ms":90250.0'), MALFORMED, None),
    ("exponent-number", ["malformed", "lexical"], replaced(FULL, b'"elapsed_ms":90250', b'"elapsed_ms":9.025e4'), MALFORMED, None),
    ("negative-number", ["malformed", "lexical"], replaced(FULL, b'"elapsed_ms":90250', b'"elapsed_ms":-1'), MALFORMED, None),
    (
        "integer-above-safe-range",
        ["malformed", "lexical"],
        replaced(FULL, b'"elapsed_ms":90250', b'"elapsed_ms":9007199254740992'),
        MALFORMED,
        None,
    ),
    ("not-an-object", ["malformed", "schema-invalid"], b"[1]", MALFORMED, None),
    ("missing-wire-version", ["malformed", "schema-invalid"], canonical(without(CHECKPOINT_FOREGROUND, "wire_version")), MALFORMED, None),
    ("string-wire-version", ["malformed", "schema-invalid"], canonical(with_changes(CHECKPOINT_FOREGROUND, wire_version="1")), MALFORMED, None),
    ("unknown-kind", ["malformed", "schema-invalid"], canonical(with_changes(CHECKPOINT_FOREGROUND, kind="sync-checkpoints")), MALFORMED, None),
    ("missing-required-field", ["malformed", "schema-invalid"], canonical(without(CHECKPOINT_FOREGROUND, "run_sequence")), MALFORMED, None),
    ("undeclared-field", ["malformed", "schema-invalid"], canonical(with_changes(CHECKPOINT_FOREGROUND, note="synthetic")), MALFORMED, None),
    ("string-sequence", ["malformed", "schema-invalid"], canonical(with_changes(CHECKPOINT_FOREGROUND, sequence="6")), MALFORMED, None),
    ("zero-sequence", ["malformed", "schema-invalid"], canonical(with_changes(CHECKPOINT_FOREGROUND, sequence=0)), MALFORMED, None),
    ("boolean-sequence", ["malformed", "schema-invalid"], canonical(with_changes(CHECKPOINT_FOREGROUND, sequence=True)), MALFORMED, None),
    (
        "timestamp-without-milliseconds",
        ["malformed", "schema-invalid"],
        canonical(with_changes(CHECKPOINT_FOREGROUND, started_at="2030-01-01T00:00:00Z")),
        MALFORMED,
        None,
    ),
    (
        "impossible-calendar-date",
        ["malformed", "rule-invalid"],
        canonical(with_changes(CHECKPOINT_FOREGROUND, started_at="2030-02-30T00:00:00.000Z")),
        MALFORMED,
        None,
    ),
    ("wire-version-2", ["unsupported-wire"], canonical(with_changes(CHECKPOINT_FOREGROUND, wire_version=2)), UNSUPPORTED_WIRE, None),
    (
        "wire-version-2-unknown-shape",
        ["unsupported-wire"],
        canonical({"wire_version": 2, "kind": "future-kind", "anything": True}),
        UNSUPPORTED_WIRE,
        None,
    ),
    ("wire-version-0", ["unsupported-wire"], canonical(with_changes(CHECKPOINT_FOREGROUND, wire_version=0)), UNSUPPORTED_WIRE, None),
    ("whitespace-between-tokens", ["noncanonical"], noncanonical_spacing(CHECKPOINT_FOREGROUND), NONCANONICAL, None),
    ("trailing-newline", ["noncanonical"], FOREGROUND + b"\n", NONCANONICAL, None),
    ("leading-space", ["noncanonical"], b" " + FOREGROUND, NONCANONICAL, None),
    ("unsorted-keys", ["noncanonical"], unsorted_keys(CHECKPOINT_FOREGROUND), NONCANONICAL, None),
    ("escaped-solidus", ["noncanonical"], replaced(canonical(OFFER), b"http://", b"http:\\/\\/"), NONCANONICAL, None),
    ("unnecessary-unicode-escape", ["noncanonical"], replaced(FOREGROUND, b'"foreground"', b'"\\u0066oreground"'), NONCANONICAL, None),
    (
        "escaped-non-ascii",
        ["noncanonical", "escaping"],
        replaced(canonical(BATCH_MIXED), "ü".encode("utf-8"), b"\\u00fc"),
        NONCANONICAL,
        None,
    ),
    (
        "offer-url-with-path",
        ["malformed", "enrollment", "schema-invalid"],
        canonical(with_changes(OFFER, portal_url=PORTAL_URL + "/enroll")),
        MALFORMED,
        None,
    ),
    (
        "offer-short-code",
        ["malformed", "enrollment", "schema-invalid"],
        canonical(with_changes(OFFER, enrollment_code=ENROLLMENT_CODE[:-1])),
        MALFORMED,
        None,
    ),
    (
        "confirmation-fingerprint-mismatch",
        ["malformed", "enrollment", "rule-invalid"],
        canonical(with_changes(CONFIRMATION, fingerprint=fingerprint(I2))),
        MALFORMED,
        None,
    ),
    (
        "confirmation-undeclared-field",
        ["malformed", "enrollment", "schema-invalid"],
        canonical(with_changes(CONFIRMATION, device_name="synthetic phone")),
        MALFORMED,
        None,
    ),
    (
        "advertisement-unsorted-wire-versions",
        ["malformed", "capability", "rule-invalid"],
        canonical(with_changes(ADVERTISEMENT, wire_versions=[2, 1])),
        MALFORMED,
        None,
    ),
    (
        "advertisement-unsorted-record-types",
        ["malformed", "capability", "rule-invalid"],
        canonical(with_changes(ADVERTISEMENT, projection_schemas=list(reversed(ADVERTISEMENT["projection_schemas"])))),
        MALFORMED,
        None,
    ),
    (
        "advertisement-byte-limit-below-floor",
        ["malformed", "capability", "schema-invalid"],
        canonical(with_changes(ADVERTISEMENT, max_message_bytes=4095)),
        MALFORMED,
        None,
    ),
    (
        "acknowledgement-undeclared-field",
        ["malformed", "acknowledgement", "schema-invalid"],
        canonical(with_changes(ACKNOWLEDGEMENT, acknowledged_generation=1)),
        MALFORMED,
        None,
    ),
    (
        "item-unknown-kind",
        ["malformed", "ingest-items", "schema-invalid"],
        canonical(with_changes(BATCH_PROJECTIONS, items=[{"kind": "tombstone", "source_identity": "synthetic-1"}])),
        MALFORMED,
        None,
    ),
    (
        "item-both-shapes",
        ["malformed", "ingest-items", "schema-invalid"],
        canonical(with_changes(BATCH_PROJECTIONS, items=[{**STEPS_ITEM, "source_identity": "synthetic-1"}])),
        MALFORMED,
        None,
    ),
    (
        "item-base64-missing-padding",
        ["malformed", "ingest-items", "schema-invalid"],
        canonical(with_changes(BATCH_PROJECTIONS, items=[{"kind": "projection", "data": STEPS_ITEM["data"].rstrip("=")}])),
        MALFORMED,
        None,
    ),
    (
        "item-base64-nonzero-unused-bits",
        ["malformed", "ingest-items", "schema-invalid"],
        canonical(with_changes(BATCH_PROJECTIONS, items=[{"kind": "projection", "data": "QR=="}])),
        MALFORMED,
        None,
    ),
    (
        "item-empty-projection",
        ["malformed", "ingest-items", "schema-invalid"],
        canonical(with_changes(BATCH_PROJECTIONS, items=[{"kind": "projection", "data": ""}])),
        MALFORMED,
        None,
    ),
    (
        "item-control-character-identity",
        ["malformed", "ingest-items", "schema-invalid"],
        canonical(with_changes(BATCH_PROJECTIONS, items=[{"kind": "retraction", "source_identity": "synthetic-\u0007"}])),
        MALFORMED,
        None,
    ),
    (
        "item-identity-too-long",
        ["malformed", "ingest-items", "schema-invalid"],
        canonical(with_changes(BATCH_PROJECTIONS, items=[{"kind": "retraction", "source_identity": "synthetic-" + "x" * 247}])),
        MALFORMED,
        None,
    ),
    (
        "batch-zero-page-index",
        ["malformed", "ingest-items", "schema-invalid"],
        canonical(with_changes(BATCH_PROJECTIONS, source_page_index=0)),
        MALFORMED,
        None,
    ),
    (
        "batch-uppercase-record-type",
        ["malformed", "ingest-items", "schema-invalid"],
        canonical(with_changes(BATCH_PROJECTIONS, record_type="Steps")),
        MALFORMED,
        None,
    ),
    (
        "snapshot-completion-unknown-kind",
        ["malformed", "completion-fields", "schema-invalid"],
        canonical(with_changes(SNAPSHOT_INITIAL, snapshot_kind="partial")),
        MALFORMED,
        None,
    ),
    (
        "completion-with-source-token",
        ["malformed", "completion-fields", "schema-invalid"],
        canonical(with_changes(SNAPSHOT_INITIAL, source_token="synthetic-token")),
        MALFORMED,
        None,
    ),
    (
        "change-completion-with-snapshot-kind",
        ["malformed", "completion-fields", "schema-invalid"],
        canonical(with_changes(CHANGE, snapshot_kind="initial")),
        MALFORMED,
        None,
    ),
    (
        "checkpoint-source-record-count",
        ["malformed", "checkpoint-privacy", "schema-invalid"],
        canonical(with_changes(CHECKPOINT_FOREGROUND, record_count=12)),
        MALFORMED,
        "source-record-count",
    ),
    (
        "checkpoint-source-token",
        ["malformed", "checkpoint-privacy", "schema-invalid"],
        canonical(with_changes(CHECKPOINT_FOREGROUND, source_token="synthetic-token")),
        MALFORMED,
        "source-token",
    ),
    (
        "checkpoint-source-identity",
        ["malformed", "checkpoint-privacy", "schema-invalid"],
        canonical(entry_change(CHECKPOINT_FOREGROUND, 0, source_identity="synthetic-1")),
        MALFORMED,
        "source-identity",
    ),
    (
        "checkpoint-health-date",
        ["malformed", "checkpoint-privacy", "schema-invalid"],
        canonical(with_changes(CHECKPOINT_FOREGROUND, measured_on="2030-01-01")),
        MALFORMED,
        "health-date",
    ),
    (
        "checkpoint-health-value",
        ["malformed", "checkpoint-privacy", "schema-invalid"],
        canonical(entry_change(CHECKPOINT_FOREGROUND, 0, value=9000)),
        MALFORMED,
        "health-value",
    ),
    (
        "checkpoint-free-text-reason",
        ["malformed", "checkpoint-privacy", "schema-invalid"],
        canonical(entry_change(CHECKPOINT_FOREGROUND, 0, outcome="failed", reason="read timed out after 500 records")),
        MALFORMED,
        "free-text-failure",
    ),
    (
        "checkpoint-free-text-field",
        ["malformed", "checkpoint-privacy", "schema-invalid"],
        canonical(with_changes(CHECKPOINT_FOREGROUND, error="synthetic free text")),
        MALFORMED,
        "free-text-failure",
    ),
    (
        "checkpoint-unknown-outcome",
        ["malformed", "checkpoint-values", "schema-invalid"],
        canonical(entry_change(CHECKPOINT_FOREGROUND, 0, outcome="skipped")),
        MALFORMED,
        None,
    ),
    (
        "checkpoint-unknown-trigger",
        ["malformed", "checkpoint-values", "schema-invalid"],
        canonical(with_changes(CHECKPOINT_FOREGROUND, trigger="push")),
        MALFORMED,
        None,
    ),
    (
        "checkpoint-unknown-scheduling-status",
        ["malformed", "checkpoint-values", "schema-invalid"],
        canonical(with_changes(CHECKPOINT_FOREGROUND, background_scheduling="unknown")),
        MALFORMED,
        None,
    ),
    (
        "checkpoint-reason-required",
        ["malformed", "checkpoint-values", "schema-invalid"],
        canonical(entry_change(CHECKPOINT_FOREGROUND, 0, outcome="blocked")),
        MALFORMED,
        None,
    ),
    (
        "checkpoint-reason-forbidden",
        ["malformed", "checkpoint-values", "schema-invalid"],
        canonical(entry_change(CHECKPOINT_FOREGROUND, 0, outcome="completed", reason="read-failed")),
        MALFORMED,
        None,
    ),
    (
        "checkpoint-unsorted-record-types",
        ["malformed", "checkpoint-values", "rule-invalid"],
        canonical(
            with_changes(
                CHECKPOINT_FOREGROUND,
                record_types=[type_status("steps", "no-change"), type_status("heart-rate", "no-change")],
            )
        ),
        MALFORMED,
        None,
    ),
    (
        "checkpoint-duplicate-record-type",
        ["malformed", "checkpoint-values", "rule-invalid"],
        canonical(
            with_changes(
                CHECKPOINT_FOREGROUND,
                record_types=[type_status("steps", "no-change"), type_status("steps", "no-change")],
            )
        ),
        MALFORMED,
        None,
    ),
    (
        "checkpoint-missing-preinsertion-count",
        ["malformed", "checkpoint-counts", "schema-invalid"],
        canonical(without(CHECKPOINT_FOREGROUND, "outbox_bytes_before_checkpoint")),
        MALFORMED,
        None,
    ),
    (
        "retryable-sender-local-reason",
        ["malformed", "retryable", "schema-invalid"],
        canonical(retryable("receipt-mismatch")),
        MALFORMED,
        None,
    ),
    (
        "block-identity-half-present",
        ["malformed", "block", "schema-invalid"],
        canonical(without(BLOCK_WITH_IDENTITY, "sequence")),
        MALFORMED,
        None,
    ),
    (
        "block-retryable-failure-name",
        ["malformed", "block", "schema-invalid"],
        canonical(with_changes(BLOCK_WITH_IDENTITY, failure="storage-reserve-exhausted")),
        MALFORMED,
        None,
    ),
]


def build_refusals() -> dict[str, object]:
    vectors = []
    for name, tags, data, failure, category in REFUSALS:
        vector: dict[str, object] = {"name": name, "tags": tags, "bytes": bytes_field(data), "failure": failure}
        if category is not None:
            vector["category"] = category
        vectors.append(vector)
    return {"description": "Bytes every decoder must refuse, with the first failure in decoding order.", "vectors": vectors}


# Fingerprints and chains


def build_identities() -> dict[str, object]:
    steps = []
    chain = chain_start(cycle(1))
    for name in ("ingest-batch-projections", "ingest-batch-empty-page", "ingest-batch-opaque-projection-bytes"):
        value = next(value for vector_name, _, value in POSITIVE_MESSAGES if vector_name == name)
        message_sha256 = sha256_hex(canonical(value))
        chain = chain_next(chain, message_sha256)
        steps.append({"message_sha256": message_sha256, "chain": chain})
    return {
        "description": "Installation fingerprints and fragment-chain values.",
        "fingerprints": [
            {"sender_installation": installation, "fingerprint": fingerprint(installation)} for installation in (I1, I2)
        ],
        "chains": [
            {"name": "empty-chain", "cycle_id": cycle(6), "start": chain_start(cycle(6)), "steps": []},
            {"name": "three-fragment-chain", "cycle_id": cycle(1), "start": chain_start(cycle(1)), "steps": steps},
        ],
    }


# Partitioning


def partition_case(
    name: str,
    tags: list[str],
    *,
    items: list[dict[str, str]],
    split: list[int] | None,
    max_record_count: int,
    max_message_bytes: int,
    page: int = 1,
    first_sequence: int = 1,
    previous_chain: str | None = None,
) -> dict[str, object]:
    page_input = {
        "sender_installation": I1,
        "capability_generation": 1,
        "record_type": "steps",
        "projection_schema_version": 1,
        "cycle_kind": "initial-snapshot",
        "cycle_id": cycle(7),
        "source_page_index": page,
        "first_sequence": first_sequence,
        "previous_chain": previous_chain or chain_start(cycle(7)),
        "items": items,
        "max_record_count": max_record_count,
        "max_message_bytes": max_message_bytes,
    }

    def encode(sequence: int, fragment: int, chain: str, fragment_items: list[dict[str, str]]) -> bytes:
        return canonical(
            batch(
                sequence,
                cycle_id=page_input["cycle_id"],
                page=page,
                fragment=fragment,
                previous_chain=chain,
                items=fragment_items,
            )
        )

    if split is None:
        oversized = [item for item in items if len(encode(first_sequence, 1, page_input["previous_chain"], [item])) > max_message_bytes]
        assert oversized, name
        return {"name": name, "tags": tags, "page": page_input, "expected": {"refusal": "indivisible-record-limit-exceeded"}}

    chain = page_input["previous_chain"]
    sequence = first_sequence
    start = 0
    fragments = []
    for fragment, size in enumerate(split, 1):
        fragment_items = items[start : start + size]
        data = encode(sequence, fragment, chain, fragment_items)
        assert len(fragment_items) <= max_record_count and len(data) <= max_message_bytes, name
        if start + size < len(items):
            longer = encode(sequence, fragment, chain, fragment_items + [items[start + size]])
            assert len(fragment_items) == max_record_count or len(longer) > max_message_bytes, name
        fragments.append(bytes_field(data))
        chain = chain_next(chain, sha256_hex(data))
        sequence += 1
        start += size
    assert start == len(items), name
    return {
        "name": name,
        "tags": tags,
        "page": page_input,
        "expected": {"fragments": fragments, "next_chain": chain, "next_sequence": sequence},
    }


def build_partition() -> dict[str, object]:
    large = [projection(f"steps-{n}", padding=1800) for n in range(1, 4)]
    two_large = len(
        canonical(batch(1, cycle_id=cycle(7), page=1, fragment=1, previous_chain=chain_start(cycle(7)), items=large[:2]))
    )
    assert two_large >= 4096
    small = [projection(f"steps-{n}") for n in range(1, 6)]
    vectors = [
        partition_case("single-fragment", ["greedy"], items=small[:2], split=[2], max_record_count=3, max_message_bytes=4096),
        partition_case("split-by-record-count", ["greedy"], items=small, split=[2, 2, 1], max_record_count=2, max_message_bytes=4096),
        partition_case(
            "split-by-bytes-at-exact-limit",
            ["greedy", "byte-limit"],
            items=large,
            split=[2, 1],
            max_record_count=10,
            max_message_bytes=two_large,
        ),
        partition_case(
            "split-by-bytes-one-byte-under",
            ["greedy", "byte-limit"],
            items=large,
            split=[1, 1, 1],
            max_record_count=10,
            max_message_bytes=two_large - 1,
        ),
        partition_case("empty-page", ["empty-page"], items=[], split=[0], max_record_count=3, max_message_bytes=4096),
        partition_case(
            "continues-chain-and-sequence-across-digit-boundary",
            ["greedy", "chain"],
            items=small[:3],
            split=[1, 1, 1],
            max_record_count=1,
            max_message_bytes=4096,
            page=2,
            first_sequence=98,
            previous_chain=chain_next(chain_start(cycle(7)), "3" * 64),
        ),
        partition_case(
            "retractions-count-as-records",
            ["greedy"],
            items=[small[0], retraction("1"), retraction("2")],
            split=[2, 1],
            max_record_count=2,
            max_message_bytes=4096,
        ),
        partition_case(
            "indivisible-record-refuses-whole-page",
            ["indivisible"],
            items=[small[0], projection("steps-huge", padding=4200)],
            split=None,
            max_record_count=3,
            max_message_bytes=4096,
        ),
    ]
    return {"description": "Sender page partitioning: exact fragments, next chain value, and next sequence.", "vectors": vectors}


# Sender response interpretation


def build_responses() -> dict[str, object]:
    sent_data = canonical(BATCH_PROJECTIONS)
    sent = {"sender_installation": I1, "sequence": 2, "message_sha256": sha256_hex(sent_data)}
    other_sha = sha256_hex(canonical(BATCH_EMPTY))
    receipt = receipt_for(sent_data)
    block = block_for(sent_data, "fragment-order-mismatch", identity=True)

    def case(name: str, tags: list[str], response: bytes, expected: dict[str, str]) -> dict[str, object]:
        return {"name": name, "tags": tags, "sent": sent, "response": bytes_field(response), "expected": expected}

    accepted = {"outcome": "accepted"}
    mismatch = {"outcome": "retryable", "failure": "receipt-mismatch"}
    vectors = [
        case("exact-receipt", ["exact-receipt"], canonical(receipt), accepted),
        case("receipt-for-other-hash", ["receipt-mismatch"], canonical(with_changes(receipt, message_sha256=other_sha)), mismatch),
        case("receipt-for-other-sequence", ["receipt-mismatch"], canonical(with_changes(receipt, sequence=3)), mismatch),
        case("receipt-for-other-installation", ["receipt-mismatch"], canonical(with_changes(receipt, sender_installation=I2)), mismatch),
        case("noncanonical-receipt", ["receipt-mismatch"], noncanonical_spacing(receipt), mismatch),
        case(
            "retryable-storage-reserve-exhausted",
            ["retryable"],
            canonical(retryable("storage-reserve-exhausted")),
            {"outcome": "retryable", "failure": "storage-reserve-exhausted"},
        ),
        case(
            "retryable-temporarily-unavailable",
            ["retryable"],
            canonical(retryable("temporarily-unavailable")),
            {"outcome": "retryable", "failure": "temporarily-unavailable"},
        ),
        case("block-for-sent-message", ["block"], canonical(block), {"outcome": "block", "failure": "fragment-order-mismatch"}),
        case(
            "block-by-hash-only",
            ["block"],
            canonical(block_for(sent_data, "malformed-message", identity=False)),
            {"outcome": "block", "failure": "malformed-message"},
        ),
        case("block-for-other-hash", ["receipt-mismatch"], canonical(with_changes(block, message_sha256=other_sha)), mismatch),
        case("block-for-other-sequence", ["receipt-mismatch"], canonical(with_changes(block, sequence=3)), mismatch),
        case("html-error-page", ["receipt-mismatch"], b"<html><body>502 Bad Gateway</body></html>", mismatch),
        case("empty-response", ["receipt-mismatch"], b"", mismatch),
        case("unexpected-kind", ["receipt-mismatch"], canonical(ADVERTISEMENT), mismatch),
    ]
    return {"description": "How the Sender interprets a Portal response to the message it sent.", "vectors": vectors}


# Portal delivery evaluation

GENERATION_1 = advertisement(1, {"heart-rate": [1], "steps": [1, 2]})
GENERATION_2_STRICT = advertisement(2, {"heart-rate": [1], "steps": [2]}, max_record_count=1)


def receipt_step(data: bytes, *, gap_after: int | None = None, latest_acknowledged_generation: int | None = None) -> dict[str, object]:
    expected: dict[str, object] = {"outcome": "receipt", "response": bytes_field(canonical(receipt_for(data)))}
    if gap_after is not None:
        expected["gap_after"] = gap_after
    if latest_acknowledged_generation is not None:
        expected["latest_acknowledged_generation"] = latest_acknowledged_generation
    return {"message": bytes_field(data), "expected": expected}


def block_step(data: bytes, failure: str, *, identity: bool = True) -> dict[str, object]:
    return {
        "message": bytes_field(data),
        "expected": {"outcome": "block", "failure": failure, "response": bytes_field(canonical(block_for(data, failure, identity=identity)))},
    }


def scenario(name: str, tags: list[str], steps: list[dict[str, object]], generations: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {"name": name, "tags": tags, "honored_generations": generations or [GENERATION_1], "steps": steps}


def item_list(prefix: str, count: int) -> list[dict[str, str]]:
    return [projection(f"{prefix}-{n}") for n in range(1, count + 1)]


def build_delivery() -> dict[str, object]:
    c = cycle
    p = item_list

    first, _ = chained(c(10), [(1, 1, 1, p("steps", 2))])
    altered, _ = chained(c(10), [(1, 1, 1, p("steps", 1))])
    checkpoint_1 = canonical(simple_checkpoint(1))
    checkpoint_2 = canonical(simple_checkpoint(2))
    checkpoint_3 = canonical(simple_checkpoint(3))

    snapshot, snapshot_chain = chained(
        c(11),
        [(1, 1, 1, p("steps", 3)), (2, 1, 2, p("steps", 1)), (3, 2, 1, p("steps", 2))],
    )
    snapshot_done = canonical(
        snapshot_completion(4, snapshot_kind="initial", cycle_id=c(11), pages=2, fragments=3, records=6, final_chain=snapshot_chain)
    )

    one, one_chain = chained(c(12), [(1, 1, 1, p("steps", 2))])

    def initial_done(sequence: int, **changes: object) -> bytes:
        fields = {"snapshot_kind": "initial", "cycle_id": c(12), "pages": 1, "fragments": 1, "records": 2, "final_chain": one_chain}
        fields.update(changes)
        return canonical(snapshot_completion(sequence, **fields))

    change_frags, change_chain = chained(
        c(13), [(1, 1, 1, [retraction("1"), projection("heart-rate-1")])], cycle_kind="change-cycle", record_type="heart-rate"
    )
    reconciliation_frags, reconciliation_chain = chained(c(14), [(1, 1, 1, p("steps", 1))], cycle_kind="reconciliation-snapshot")
    change_kind_frags, change_kind_chain = chained(c(15), [(1, 1, 1, p("steps", 1))], cycle_kind="change-cycle")

    start_10 = chain_start(c(10))
    after_first = chain_next(start_10, sha256_hex(first[0]))

    def fragment(sequence: int, page: int, number: int, previous_chain: str, items: list[dict[str, str]], **fields: object) -> bytes:
        return canonical(batch(sequence, cycle_id=c(10), page=page, fragment=number, previous_chain=previous_chain, items=items, **fields))

    scenarios = [
        scenario("first-fragment-accepted", ["receipt-identity"], [receipt_step(first[0])]),
        scenario("identical-retry-returns-same-receipt", ["identical-retry"], [receipt_step(first[0]), receipt_step(first[0])]),
        scenario(
            "same-sequence-different-bytes-blocks",
            ["sequence-content"],
            [receipt_step(first[0]), block_step(altered[0], "sequence-content-mismatch")],
        ),
        scenario("gap-is-reported", ["gap"], [receipt_step(checkpoint_1), receipt_step(checkpoint_3, gap_after=1)]),
        scenario("first-message-after-lost-history-reports-gap", ["gap"], [receipt_step(checkpoint_3, gap_after=0)]),
        scenario(
            "older-unseen-sequence-blocks",
            ["out-of-order"],
            [receipt_step(checkpoint_1), receipt_step(checkpoint_3, gap_after=1), block_step(checkpoint_2, "sequence-out-of-order")],
        ),
        scenario(
            "unhonored-generation-blocks",
            ["versions", "staging-generation"],
            [block_step(canonical(simple_checkpoint(1, generation=2)), "unsupported-wire-protocol")],
        ),
        scenario(
            "projection-schema-independent-of-wire-version",
            ["versions", "no-partial-success"],
            [
                block_step(fragment(1, 1, 1, start_10, p("steps", 1), schema=3), "unsupported-source-projection-schema"),
                receipt_step(fragment(1, 1, 1, start_10, p("steps", 1), schema=2)),
            ],
        ),
        scenario(
            "record-type-outside-generation-blocks",
            ["versions"],
            [block_step(fragment(1, 1, 1, start_10, p("weight", 1), record_type="weight"), "unsupported-source-projection-schema")],
        ),
        scenario(
            "record-count-limit-blocks",
            ["limits", "no-partial-success"],
            [block_step(fragment(1, 1, 1, start_10, p("steps", 4)), "record-count-limit-exceeded"), receipt_step(first[0])],
        ),
        scenario(
            "byte-limit-blocks",
            ["limits"],
            [block_step(fragment(1, 1, 1, start_10, [projection("steps-1", padding=4200)]), "uncompressed-byte-limit-exceeded")],
        ),
        scenario("multi-page-snapshot-completes", ["completion-accepted"], [*map(receipt_step, snapshot), receipt_step(snapshot_done)]),
        scenario(
            "first-fragment-without-starting-chain-blocks",
            ["fragment-order"],
            [block_step(fragment(1, 1, 1, "0" * 64, p("steps", 1)), "fragment-order-mismatch")],
        ),
        scenario(
            "first-fragment-not-on-page-one-blocks",
            ["fragment-order"],
            [block_step(fragment(1, 2, 1, start_10, p("steps", 1)), "fragment-order-mismatch")],
        ),
        scenario(
            "skipped-fragment-blocks",
            ["fragment-order"],
            [receipt_step(first[0]), block_step(fragment(2, 1, 3, after_first, p("steps", 1)), "fragment-order-mismatch")],
        ),
        scenario(
            "skipped-page-blocks",
            ["fragment-order"],
            [receipt_step(first[0]), block_step(fragment(2, 3, 1, after_first, p("steps", 1)), "fragment-order-mismatch")],
        ),
        scenario(
            "wrong-previous-chain-blocks",
            ["fragment-order"],
            [receipt_step(first[0]), block_step(fragment(2, 1, 2, start_10, p("steps", 1)), "fragment-order-mismatch")],
        ),
        scenario(
            "record-type-change-within-cycle-blocks",
            ["fragment-order"],
            [
                receipt_step(first[0]),
                block_step(fragment(2, 1, 2, after_first, p("heart-rate", 1), record_type="heart-rate"), "fragment-order-mismatch"),
            ],
        ),
        scenario(
            "cycle-kind-change-within-cycle-blocks",
            ["fragment-order"],
            [
                receipt_step(first[0]),
                block_step(fragment(2, 1, 2, after_first, p("steps", 1), cycle_kind="change-cycle"), "fragment-order-mismatch"),
            ],
        ),
        scenario(
            "next-fragment-and-next-page-continue-cycle",
            ["fragment-order"],
            [receipt_step(data) for data in snapshot],
        ),
        scenario(
            "fragment-after-completion-blocks",
            ["fragment-after-completion"],
            [
                receipt_step(one[0]),
                receipt_step(initial_done(2)),
                block_step(
                    canonical(batch(3, cycle_id=c(12), page=2, fragment=1, previous_chain=one_chain, items=p("steps", 1))),
                    "fragment-order-mismatch",
                ),
            ],
        ),
        scenario(
            "fragment-after-empty-cycle-completion-blocks",
            ["fragment-after-completion", "empty-cycle"],
            [
                receipt_step(canonical(change_completion(1, cycle_id=c(16), pages=0, fragments=0, records=0, final_chain=chain_start(c(16))))),
                block_step(
                    canonical(
                        batch(2, cycle_id=c(16), page=1, fragment=1, previous_chain=chain_start(c(16)), items=p("steps", 1), cycle_kind="change-cycle")
                    ),
                    "fragment-order-mismatch",
                ),
            ],
        ),
        scenario(
            "completion-record-type-mismatch-blocks",
            ["completion-mismatch"],
            [receipt_step(one[0]), block_step(initial_done(2, record_type="heart-rate"), "completion-identity-mismatch")],
        ),
        scenario(
            "second-completion-blocks",
            ["completion-mismatch"],
            [receipt_step(one[0]), receipt_step(initial_done(2)), block_step(initial_done(3), "completion-identity-mismatch")],
        ),
        scenario(
            "snapshot-kind-mismatch-blocks",
            ["completion-mismatch"],
            [receipt_step(one[0]), block_step(initial_done(2, snapshot_kind="reconciliation"), "completion-kind-mismatch")],
        ),
        scenario(
            "snapshot-completion-for-change-cycle-blocks",
            ["completion-mismatch"],
            [
                receipt_step(change_kind_frags[0]),
                block_step(
                    canonical(
                        snapshot_completion(2, snapshot_kind="initial", cycle_id=c(15), pages=1, fragments=1, records=1, final_chain=change_kind_chain)
                    ),
                    "completion-kind-mismatch",
                ),
            ],
        ),
        scenario(
            "completion-count-mismatch-blocks",
            ["completion-mismatch"],
            [receipt_step(one[0]), block_step(initial_done(2, records=3), "completion-count-mismatch")],
        ),
        scenario(
            "completion-page-count-mismatch-blocks",
            ["completion-mismatch"],
            [receipt_step(one[0]), block_step(initial_done(2, pages=2), "completion-count-mismatch")],
        ),
        scenario(
            "completion-chain-mismatch-blocks",
            ["completion-mismatch"],
            [receipt_step(one[0]), block_step(initial_done(2, final_chain=chain_start(c(12))), "completion-chain-mismatch")],
        ),
        scenario(
            "gap-inside-claimed-chain-blocks-completion",
            ["completion-mismatch", "gap"],
            [
                receipt_step(one[0]),
                receipt_step(canonical(simple_checkpoint(3)), gap_after=1),
                block_step(initial_done(4), "completion-sequence-gap"),
            ],
        ),
        scenario(
            "completion-after-gap-blocks",
            ["completion-mismatch", "gap"],
            [receipt_step(one[0]), block_step(initial_done(3), "completion-sequence-gap")],
        ),
        scenario(
            "empty-change-cycle-completes",
            ["empty-cycle"],
            [receipt_step(canonical(change_completion(1, cycle_id=c(17), pages=0, fragments=0, records=0, final_chain=chain_start(c(17)))))],
        ),
        scenario(
            "empty-cycle-with-wrong-chain-blocks",
            ["empty-cycle", "completion-mismatch"],
            [
                block_step(
                    canonical(change_completion(1, cycle_id=c(17), pages=0, fragments=0, records=0, final_chain=chain_start(c(18)))),
                    "completion-chain-mismatch",
                )
            ],
        ),
        scenario(
            "unknown-cycle-with-nonzero-counts-blocks",
            ["empty-cycle", "completion-mismatch"],
            [
                block_step(
                    canonical(change_completion(1, cycle_id=c(18), pages=1, fragments=1, records=1, final_chain=chain_start(c(18)))),
                    "completion-count-mismatch",
                )
            ],
        ),
        scenario(
            "change-cycle-with-retraction-completes",
            ["completion-accepted"],
            [
                receipt_step(change_frags[0]),
                receipt_step(
                    canonical(
                        change_completion(2, cycle_id=c(13), pages=1, fragments=1, records=2, final_chain=change_chain, record_type="heart-rate")
                    )
                ),
            ],
        ),
        scenario(
            "reconciliation-snapshot-completes",
            ["completion-accepted"],
            [
                receipt_step(reconciliation_frags[0]),
                receipt_step(
                    canonical(
                        snapshot_completion(
                            2, snapshot_kind="reconciliation", cycle_id=c(14), pages=1, fragments=1, records=1, final_chain=reconciliation_chain
                        )
                    )
                ),
            ],
        ),
        scenario(
            "capability-acknowledgement-accepted",
            ["acknowledgement"],
            [receipt_step(canonical(acknowledgement(1, 2)), latest_acknowledged_generation=2)],
            [GENERATION_1, GENERATION_2_STRICT],
        ),
        scenario(
            "acknowledgement-follows-sequence-rules",
            ["acknowledgement", "sequence-content"],
            [
                receipt_step(canonical(acknowledgement(1, 2)), latest_acknowledged_generation=2),
                block_step(canonical(acknowledgement(1, 1)), "sequence-content-mismatch"),
            ],
            [GENERATION_1, GENERATION_2_STRICT],
        ),
        scenario(
            "acknowledgement-of-unhonored-generation-blocks",
            ["acknowledgement", "staging-generation"],
            [block_step(canonical(acknowledgement(1, 3)), "unsupported-wire-protocol")],
            [GENERATION_1, GENERATION_2_STRICT],
        ),
        scenario(
            "limits-follow-staging-generation",
            ["staging-generation", "queued-limits"],
            [
                receipt_step(fragment(1, 1, 1, start_10, p("steps", 3), schema=1, generation=1)),
                block_step(
                    canonical(
                        batch(2, cycle_id=c(19), page=1, fragment=1, previous_chain=chain_start(c(19)), items=p("steps", 2), schema=2, generation=2)
                    ),
                    "record-count-limit-exceeded",
                ),
            ],
            [GENERATION_1, GENERATION_2_STRICT],
        ),
        scenario(
            "projection-schema-follows-staging-generation",
            ["staging-generation"],
            [
                block_step(
                    canonical(
                        batch(1, cycle_id=c(19), page=1, fragment=1, previous_chain=chain_start(c(19)), items=p("steps", 1), schema=1, generation=2)
                    ),
                    "unsupported-source-projection-schema",
                ),
                receipt_step(
                    canonical(
                        batch(1, cycle_id=c(19), page=1, fragment=1, previous_chain=chain_start(c(19)), items=p("steps", 1), schema=2, generation=2)
                    )
                ),
            ],
            [GENERATION_1, GENERATION_2_STRICT],
        ),
        scenario("invalid-bytes-block-without-identity", ["decode"], [block_step(b"\xff", "malformed-message", identity=False)]),
        scenario(
            "noncanonical-message-blocks-with-identity",
            ["decode"],
            [block_step(noncanonical_spacing(simple_checkpoint(1)), "noncanonical-message")],
        ),
        scenario(
            "unsupported-wire-version-blocks-without-identity",
            ["decode", "versions"],
            [block_step(canonical(with_changes(simple_checkpoint(1), wire_version=2)), "unsupported-wire-protocol", identity=False)],
        ),
        scenario(
            "non-outbox-kind-blocks-without-identity",
            ["decode"],
            [block_step(canonical(receipt_for(first[0])), "malformed-message", identity=False)],
        ),
        scenario("checkpoint-accepted", ["checkpoint"], [receipt_step(checkpoint_1)]),
        scenario(
            "installations-have-separate-sequences",
            ["receipt-identity"],
            [receipt_step(checkpoint_1), receipt_step(canonical(simple_checkpoint(1, installation=I2)))],
        ),
    ]
    return {"description": "Portal delivery evaluation: ordered steps with their exact responses.", "vectors": scenarios}


# Compatibility decisions


def build_compatibility() -> dict[str, object]:
    g1 = advertisement(1, {"steps": [1]}, max_record_count=500, max_message_bytes=65_536)
    g1_changed = advertisement(1, {"steps": [1]}, max_record_count=400, max_message_bytes=65_536)
    g2 = advertisement(2, {"steps": [1, 2]}, max_record_count=1000, max_message_bytes=131_072)
    g2_lower = advertisement(2, {"steps": [1]}, max_record_count=100, max_message_bytes=65_536)
    g3 = advertisement(3, {"steps": [2]}, max_record_count=1000, max_message_bytes=131_072)

    def case(name: str, tags: list[str], current: list[dict[str, object]], proposed: list[dict[str, object]], acknowledged: int | None, expected: str) -> dict[str, object]:
        return {
            "name": name,
            "tags": tags,
            "current": current,
            "proposed": proposed,
            "latest_acknowledged_generation": acknowledged,
            "expected": expected,
        }

    regression = "capability-generation-regression"
    blocked = "capability-contraction-blocked"
    vectors = [
        case("first-generation", ["expansion"], [], [g1], None, "accept"),
        case("add-generation", ["expansion"], [g1], [g1, g2], None, "accept"),
        case("unchanged", ["expansion"], [g1, g2], [g1, g2], 1, "accept"),
        case("new-generation-not-greater", ["regression"], [g1, g3], [g1, g2, g3], None, regression),
        case("generation-contents-changed", ["regression"], [g1], [g1_changed], None, regression),
        case("regression-precedes-contraction", ["regression"], [g1, g3], [g2], None, regression),
        case("retire-after-later-acknowledgement", ["retirement"], [g1, g2], [g2], 2, "accept"),
        case("retire-without-acknowledgement", ["retirement"], [g1, g2], [g2], None, blocked),
        case("retire-with-same-generation-acknowledged", ["retirement"], [g1, g2], [g2], 1, blocked),
        case("lower-limit-retiring-old-generation-too-early", ["retirement"], [g1], [g2_lower], 1, blocked),
        case("lower-limit-after-acknowledgement", ["retirement"], [g1, g2_lower], [g2_lower], 2, "accept"),
        case("retire-every-generation", ["retirement"], [g1], [], 1, blocked),
        case("remove-version-by-retiring-generations", ["retirement"], [g1, g2, g3], [g3], 3, "accept"),
    ]
    return {"description": "Portal compatibility decisions for proposed honored capability generations.", "vectors": vectors}


# Files

BUILDERS = {
    "messages.json": (build_messages, BOTH),
    "refusals.json": (build_refusals, BOTH),
    "identities.json": (build_identities, BOTH),
    "partition.json": (build_partition, ["typescript"]),
    "responses.json": (build_responses, ["typescript"]),
    "delivery.json": (build_delivery, ["python"]),
    "compatibility.json": (build_compatibility, ["python"]),
}


def render(document: object) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def build_all() -> dict[str, bytes]:
    files = {path: render(builder()) for path, (builder, _) in BUILDERS.items()}
    manifest = {
        "wire_version": 1,
        "generator": "protocol/v1/tools/build_vectors.py",
        "files": [
            {"path": path, "sha256": sha256_hex(data), "languages": BUILDERS[path][1]} for path, data in files.items()
        ],
    }
    files["manifest.json"] = render(manifest)
    return files


def main(argv: list[str]) -> int:
    files = build_all()
    if argv == ["--check"]:
        stale = [path for path, data in files.items() if not (VECTOR_DIR / path).is_file() or (VECTOR_DIR / path).read_bytes() != data]
        extra = sorted(p.name for p in VECTOR_DIR.glob("*.json") if p.name not in files)
        for path in stale + extra:
            print(f"stale or unexpected vector file: {path}", file=sys.stderr)
        return 1 if stale or extra else 0
    if argv:
        print(__doc__, file=sys.stderr)
        return 2
    VECTOR_DIR.mkdir(parents=True, exist_ok=True)
    for path, data in files.items():
        (VECTOR_DIR / path).write_bytes(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
