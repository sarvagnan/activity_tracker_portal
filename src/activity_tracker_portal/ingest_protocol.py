"""Wire Protocol Version 1 for Sender ingress (`ingest-protocol@1`).

This module is pure: it encodes, decodes, and judges messages without storage,
network, or clock effects. Field-level rules live in `protocol/v1/README.md`,
and the shared vectors under `protocol/v1/vectors/` are the oracle.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

Message = dict[str, Any]

MAX_INTEGER = 2**53 - 1
MIN_MESSAGE_BYTES = 4096

OUTBOX_KINDS = frozenset(
    {"capability-acknowledgement", "ingest-batch", "snapshot-completion", "change-completion", "sync-checkpoint"}
)
DELIVERY_BLOCK_FAILURES = frozenset(
    {
        "malformed-message",
        "noncanonical-message",
        "unsupported-wire-protocol",
        "unsupported-source-projection-schema",
        "record-count-limit-exceeded",
        "uncompressed-byte-limit-exceeded",
        "sequence-content-mismatch",
        "sequence-out-of-order",
        "fragment-order-mismatch",
        "completion-identity-mismatch",
        "completion-kind-mismatch",
        "completion-count-mismatch",
        "completion-chain-mismatch",
        "completion-sequence-gap",
    }
)
RETRYABLE_REASONS = frozenset({"storage-reserve-exhausted", "temporarily-unavailable"})
OUTCOMES = frozenset(
    {"not-due", "no-change", "progressed", "completed", "deferred", "blocked", "not-permitted", "failed", "interrupted"}
)
REASON_REQUIRED = frozenset({"deferred", "blocked", "failed"})
REASON_OPTIONAL = frozenset({"progressed", "interrupted"})
REASONS = frozenset(
    {
        "collection-disabled",
        "projection-refusal",
        "record-too-large",
        "permission-missing",
        "delivery-blocked",
        "storage-pressure",
        "screen-lock-removed",
        "changes-token-expired",
        "time-budget-reached",
        "read-failed",
    }
)
CYCLE_KINDS = frozenset({"initial-snapshot", "reconciliation-snapshot", "change-cycle"})
COMPLETION_CYCLE_KINDS = {
    ("snapshot-completion", "initial"): "initial-snapshot",
    ("snapshot-completion", "reconciliation"): "reconciliation-snapshot",
    ("change-completion", None): "change-cycle",
}


class ProtocolRefusal(Exception):
    """A named failure from the `ingest-protocol` Governed Scope contract."""

    def __init__(self, failure: str) -> None:
        super().__init__(failure)
        self.failure = failure


# Field validators


def _pattern(expression: str) -> Callable[[object], bool]:
    compiled = re.compile(expression)
    return lambda value: type(value) is str and compiled.fullmatch(value) is not None


def _is_count(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_INTEGER


def _is_positive(value: object) -> bool:
    return _is_count(value) and value >= 1


def _one_of(choices: frozenset[str]) -> Callable[[object], bool]:
    return lambda value: type(value) is str and value in choices


_is_uuid = _pattern(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_is_sha256 = _pattern(r"[0-9a-f]{64}")
_is_record_type = _pattern(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
_is_base64 = _pattern(r"([A-Za-z0-9+/]{4})+|([A-Za-z0-9+/]{4})*([A-Za-z0-9+/][AQgw]==|[A-Za-z0-9+/]{2}[AEIMQUYcgkosw048]=)")
_is_enrollment_code = _pattern(r"[A-Z2-7]{16}")
_is_portal_url = _pattern(r"https?://[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:[0-9]{1,5})?")
_TIMESTAMP = re.compile(
    r"([0-9]{4})-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z"
)


def _days_in_month(year: int, month: int) -> int:
    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    return 30 if month in (4, 6, 9, 11) else 31


def _is_timestamp(value: object) -> bool:
    if type(value) is not str:
        return False
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        return False
    year, month, day = int(match[1]), int(match[2]), int(match[3])
    return day <= _days_in_month(year, month)


def _is_source_identity(value: object) -> bool:
    return type(value) is str and 1 <= len(value) <= 256 and all(ord(char) >= 0x20 for char in value)


def _is_record_type_name(value: object) -> bool:
    return _is_record_type(value) and len(value) <= 64


def _is_ascending(values: list[Any]) -> bool:
    return all(earlier < later for earlier, later in zip(values, values[1:]))


def _is_version_list(value: object) -> bool:
    return type(value) is list and bool(value) and all(_is_positive(item) for item in value) and _is_ascending(value)


def _has_fields(value: object, required: Mapping[str, Callable[[object], bool]], optional: Mapping[str, Callable[[object], bool]] | None = None) -> bool:
    if type(value) is not dict:
        return False
    optional = optional or {}
    if not set(required) <= set(value) or not set(value) <= set(required) | set(optional):
        return False
    return all(validator(value[name]) for name, validator in {**required, **optional}.items() if name in value)


def _is_projection_schema(value: object) -> bool:
    return _has_fields(value, {"record_type": _is_record_type_name, "versions": _is_version_list})


def _is_projection_schemas(value: object) -> bool:
    return (
        type(value) is list
        and bool(value)
        and all(_is_projection_schema(item) for item in value)
        and _is_ascending([item["record_type"] for item in value])
    )


def _is_item(value: object) -> bool:
    if type(value) is not dict:
        return False
    if value.get("kind") == "projection":
        return _has_fields(value, {"kind": _one_of(frozenset({"projection"})), "data": _is_base64})
    return _has_fields(value, {"kind": _one_of(frozenset({"retraction"})), "source_identity": _is_source_identity})


def _is_items(value: object) -> bool:
    return type(value) is list and all(_is_item(item) for item in value)


def _is_type_status(value: object) -> bool:
    fields = {"record_type": _is_record_type_name, "outcome": _one_of(OUTCOMES), "source_data_staged": lambda v: type(v) is bool}
    if not _has_fields(value, fields, {"reason": _one_of(REASONS)}):
        return False
    if value["outcome"] in REASON_REQUIRED:
        return "reason" in value
    return "reason" not in value or value["outcome"] in REASON_OPTIONAL


def _is_type_statuses(value: object) -> bool:
    return (
        type(value) is list
        and all(_is_type_status(item) for item in value)
        and _is_ascending([item["record_type"] for item in value])
    )


def _is_max_message_bytes(value: object) -> bool:
    return _is_count(value) and value >= MIN_MESSAGE_BYTES


_COMMON = {"wire_version": lambda v: v == 1 and type(v) is int, "kind": lambda v: type(v) is str}
_OUTBOX = {**_COMMON, "sender_installation": _is_uuid, "sequence": _is_positive, "capability_generation": _is_positive}
_COMPLETION = {
    **_OUTBOX,
    "record_type": _is_record_type_name,
    "cycle_id": _is_uuid,
    "page_count": _is_count,
    "fragment_count": _is_count,
    "record_count": _is_count,
    "final_chain": _is_sha256,
}
_KINDS: dict[str, tuple[dict[str, Callable[[object], bool]], dict[str, Callable[[object], bool]]]] = {
    "enrollment-offer": (
        {**_COMMON, "enrollment_code": _is_enrollment_code, "portal_url": _is_portal_url, "expires_at": _is_timestamp},
        {},
    ),
    "enrollment-confirmation": (
        {**_COMMON, "enrollment_code": _is_enrollment_code, "sender_installation": _is_uuid, "fingerprint": _is_sha256},
        {},
    ),
    "capability-advertisement": (
        {
            **_COMMON,
            "generation": _is_positive,
            "wire_versions": _is_version_list,
            "projection_schemas": _is_projection_schemas,
            "max_record_count": _is_positive,
            "max_message_bytes": _is_max_message_bytes,
        },
        {},
    ),
    "capability-acknowledgement": (_OUTBOX, {}),
    "ingest-batch": (
        {
            **_OUTBOX,
            "record_type": _is_record_type_name,
            "projection_schema_version": _is_positive,
            "cycle_kind": _one_of(CYCLE_KINDS),
            "cycle_id": _is_uuid,
            "source_page_index": _is_positive,
            "fragment_index": _is_positive,
            "previous_chain": _is_sha256,
            "items": _is_items,
        },
        {},
    ),
    "snapshot-completion": ({**_COMPLETION, "snapshot_kind": _one_of(frozenset({"initial", "reconciliation"}))}, {}),
    "change-completion": (_COMPLETION, {}),
    "sync-checkpoint": (
        {
            **_OUTBOX,
            "run_sequence": _is_positive,
            "trigger": _one_of(frozenset({"background", "foreground", "manual"})),
            "started_at": _is_timestamp,
            "ended_at": _is_timestamp,
            "background_scheduling": _one_of(frozenset({"available", "restricted", "unregistered"})),
            "record_types": _is_type_statuses,
            "outbox_messages_before_checkpoint": _is_count,
            "outbox_bytes_before_checkpoint": _is_count,
        },
        {"elapsed_ms": _is_count},
    ),
    "delivery-receipt": (
        {**_COMMON, "sender_installation": _is_uuid, "sequence": _is_positive, "message_sha256": _is_sha256},
        {},
    ),
    "retryable-failure": ({**_COMMON, "reason": _one_of(RETRYABLE_REASONS)}, {}),
    "delivery-block": (
        {**_COMMON, "failure": _one_of(DELIVERY_BLOCK_FAILURES), "message_sha256": _is_sha256},
        {"sender_installation": _is_uuid, "sequence": _is_positive},
    ),
}


def _validate(value: object) -> Message:
    """Apply decoding steps 2 to 4 to a parsed value."""
    if type(value) is not dict or type(value.get("wire_version")) is not int:
        raise ProtocolRefusal("malformed-message")
    if value["wire_version"] != 1:
        raise ProtocolRefusal("unsupported-wire-protocol")
    kind = value.get("kind")
    if type(kind) is not str or kind not in _KINDS:
        raise ProtocolRefusal("malformed-message")
    required, optional = _KINDS[kind]
    if not _has_fields(value, required, optional):
        raise ProtocolRefusal("malformed-message")
    if kind == "enrollment-confirmation" and value["fingerprint"] != installation_fingerprint(value["sender_installation"]):
        raise ProtocolRefusal("malformed-message")
    if kind == "delivery-block" and ("sender_installation" in value) != ("sequence" in value):
        raise ProtocolRefusal("malformed-message")
    return value


# Encoding and decoding


class _LexicalError(ValueError):
    pass


def _reject_token(token: str) -> object:
    raise _LexicalError(token)


def _parse_integer(token: str) -> int:
    if token.startswith("-"):
        raise _LexicalError(token)
    number = int(token)
    if number > MAX_INTEGER:
        raise _LexicalError(token)
    return number


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _LexicalError(key)
        result[key] = value
    return result


def _check_scalar_values(value: object) -> None:
    if type(value) is str:
        value.encode("utf-8")  # Refuses lone surrogates from \u escapes.
    elif type(value) is list:
        for item in value:
            _check_scalar_values(item)
    elif type(value) is dict:
        for key, item in value.items():
            key.encode("utf-8")
            _check_scalar_values(item)


def _parse(data: bytes) -> object:
    """Apply decoding step 1."""
    try:
        text = data.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_float=_reject_token,
            parse_int=_parse_integer,
            parse_constant=_reject_token,
        )
        _check_scalar_values(value)
    except (ValueError, RecursionError):
        raise ProtocolRefusal("malformed-message") from None
    return value


def _canonical(value: Message) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def encode_message(value: Mapping[str, Any]) -> bytes:
    """Return the canonical bytes of a valid message value."""
    try:
        return _canonical(_validate(dict(value)))
    except UnicodeEncodeError:
        raise ProtocolRefusal("malformed-message") from None


def decode_message(data: bytes) -> Message:
    """Return the validated message value, or raise ProtocolRefusal."""
    value = _validate(_parse(data))
    if _canonical(value) != data:
        raise ProtocolRefusal("noncanonical-message")
    return value


def message_sha256(data: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 identity of exact message bytes."""
    return hashlib.sha256(data).hexdigest()


def installation_fingerprint(sender_installation: str) -> str:
    """Return the fingerprint an enrollment confirmation must carry."""
    return hashlib.sha256(sender_installation.encode("utf-8")).hexdigest()


def chain_start(cycle_id: str) -> str:
    """Return the starting fragment-chain value of a cycle."""
    return hashlib.sha256(cycle_id.encode("utf-8")).hexdigest()


def chain_next(previous_chain: str, message_sha256: str) -> str:
    """Return the fragment-chain value after one accepted fragment."""
    return hashlib.sha256(bytes.fromhex(previous_chain) + bytes.fromhex(message_sha256)).hexdigest()


# Delivery evaluation


@dataclass(frozen=True)
class _Cycle:
    record_type: str
    cycle_kind: str | None
    first_sequence: int | None
    page: int
    fragment: int
    chain: str
    page_count: int
    fragment_count: int
    record_count: int
    completed: bool


@dataclass(frozen=True)
class _History:
    accepted: Mapping[int, str] = field(default_factory=dict)
    highest: int = 0
    cycles: Mapping[str, _Cycle] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryState:
    """Honored capability generations and accepted delivery history.

    Callers treat the state as opaque apart from the latest acknowledged
    generation, which the compatibility decision needs.
    """

    generations: Mapping[int, Message]
    histories: Mapping[str, _History] = field(default_factory=dict)
    latest_acknowledged_generation: int | None = None


@dataclass(frozen=True)
class DeliveryEvaluation:
    """The Portal's verdict on one delivered message."""

    outcome: str
    response: bytes
    state: DeliveryState
    failure: str | None = None
    gap_after: int | None = None


def _advertisement(value: Mapping[str, Any]) -> Message:
    advertisement = _validate(dict(value))
    if advertisement["kind"] != "capability-advertisement":
        raise ValueError("an honored generation must be a capability advertisement")
    return advertisement


def new_delivery_state(honored_generations: Sequence[Mapping[str, Any]]) -> DeliveryState:
    """Return an empty delivery history under the given capability advertisements."""
    generations: dict[int, Message] = {}
    for value in honored_generations:
        advertisement = _advertisement(value)
        if advertisement["generation"] in generations:
            raise ValueError(f"duplicate capability generation {advertisement['generation']}")
        generations[advertisement["generation"]] = advertisement
    return DeliveryState(generations=generations)


def _block(state: DeliveryState, data: bytes, failure: str, message: Message | None) -> DeliveryEvaluation:
    block: Message = {"wire_version": 1, "kind": "delivery-block", "failure": failure, "message_sha256": message_sha256(data)}
    if message is not None:
        block["sender_installation"] = message["sender_installation"]
        block["sequence"] = message["sequence"]
    return DeliveryEvaluation(outcome="block", response=_canonical(block), state=state, failure=failure)


def _receipt(message: Message, data: bytes) -> bytes:
    return _canonical(
        {
            "wire_version": 1,
            "kind": "delivery-receipt",
            "sender_installation": message["sender_installation"],
            "sequence": message["sequence"],
            "message_sha256": message_sha256(data),
        }
    )


def _continues(cycle: _Cycle | None, message: Message) -> bool:
    page, fragment = message["source_page_index"], message["fragment_index"]
    if cycle is None:
        return page == 1 and fragment == 1 and message["previous_chain"] == chain_start(message["cycle_id"])
    return (
        not cycle.completed
        and cycle.cycle_kind == message["cycle_kind"]
        and cycle.record_type == message["record_type"]
        and cycle.chain == message["previous_chain"]
        and (page, fragment) in ((cycle.page, cycle.fragment + 1), (cycle.page + 1, 1))
    )


def _completion_failure(cycle: _Cycle | None, message: Message, history: _History) -> str | None:
    if cycle is not None and (cycle.completed or cycle.record_type != message["record_type"]):
        return "completion-identity-mismatch"
    expected_kind = COMPLETION_CYCLE_KINDS[(message["kind"], message.get("snapshot_kind"))]
    if cycle is not None and cycle.cycle_kind != expected_kind:
        return "completion-kind-mismatch"
    if cycle is not None and cycle.first_sequence is not None:
        if any(sequence not in history.accepted for sequence in range(cycle.first_sequence, message["sequence"])):
            return "completion-sequence-gap"
    if cycle is None:
        counts, chain = (0, 0, 0), chain_start(message["cycle_id"])
    else:
        counts, chain = (cycle.page_count, cycle.fragment_count, cycle.record_count), cycle.chain
    if (message["page_count"], message["fragment_count"], message["record_count"]) != counts:
        return "completion-count-mismatch"
    if message["final_chain"] != chain:
        return "completion-chain-mismatch"
    return None


def evaluate_delivery(state: DeliveryState, data: bytes) -> DeliveryEvaluation:
    """Evaluate one delivered message; a Delivery Block returns the state unchanged."""
    try:
        message = _validate(_parse(data))
    except ProtocolRefusal as refusal:
        return _block(state, data, refusal.failure, None)
    outbox = message["kind"] in OUTBOX_KINDS
    if _canonical(message) != data:
        return _block(state, data, "noncanonical-message", message if outbox else None)
    if not outbox:
        return _block(state, data, "malformed-message", None)

    sha = message_sha256(data)
    installation, sequence = message["sender_installation"], message["sequence"]
    history = state.histories.get(installation, _History())
    if sequence in history.accepted:
        if history.accepted[sequence] == sha:
            return DeliveryEvaluation(outcome="receipt", response=_receipt(message, data), state=state)
        return _block(state, data, "sequence-content-mismatch", message)
    if sequence < history.highest:
        return _block(state, data, "sequence-out-of-order", message)
    generation = state.generations.get(message["capability_generation"])
    if generation is None or message["wire_version"] not in generation["wire_versions"]:
        return _block(state, data, "unsupported-wire-protocol", message)

    cycles = dict(history.cycles)
    latest = state.latest_acknowledged_generation
    kind = message["kind"]
    if kind == "ingest-batch":
        versions = next(
            (schema["versions"] for schema in generation["projection_schemas"] if schema["record_type"] == message["record_type"]),
            [],
        )
        if message["projection_schema_version"] not in versions:
            return _block(state, data, "unsupported-source-projection-schema", message)
        if len(message["items"]) > generation["max_record_count"]:
            return _block(state, data, "record-count-limit-exceeded", message)
        if len(data) > generation["max_message_bytes"]:
            return _block(state, data, "uncompressed-byte-limit-exceeded", message)
        cycle = cycles.get(message["cycle_id"])
        if not _continues(cycle, message):
            return _block(state, data, "fragment-order-mismatch", message)
        cycles[message["cycle_id"]] = _Cycle(
            record_type=message["record_type"],
            cycle_kind=message["cycle_kind"],
            first_sequence=sequence if cycle is None else cycle.first_sequence,
            page=message["source_page_index"],
            fragment=message["fragment_index"],
            chain=chain_next(message["previous_chain"], sha),
            page_count=message["source_page_index"],
            fragment_count=(0 if cycle is None else cycle.fragment_count) + 1,
            record_count=(0 if cycle is None else cycle.record_count) + len(message["items"]),
            completed=False,
        )
    elif kind in ("snapshot-completion", "change-completion"):
        cycle = cycles.get(message["cycle_id"])
        failure = _completion_failure(cycle, message, history)
        if failure is not None:
            return _block(state, data, failure, message)
        if cycle is None:
            cycles[message["cycle_id"]] = _Cycle(
                record_type=message["record_type"],
                cycle_kind=None,
                first_sequence=None,
                page=0,
                fragment=0,
                chain=message["final_chain"],
                page_count=0,
                fragment_count=0,
                record_count=0,
                completed=True,
            )
        else:
            cycles[message["cycle_id"]] = dataclasses.replace(cycle, completed=True)
    elif kind == "capability-acknowledgement":
        latest = max(latest or 0, message["capability_generation"])

    gap_after = history.highest if sequence > history.highest + 1 else None
    accepted = {**history.accepted, sequence: sha}
    histories = {**state.histories, installation: _History(accepted=accepted, highest=sequence, cycles=cycles)}
    next_state = DeliveryState(generations=state.generations, histories=histories, latest_acknowledged_generation=latest)
    return DeliveryEvaluation(outcome="receipt", response=_receipt(message, data), state=next_state, gap_after=gap_after)


# Compatibility


def decide_compatibility(
    current: Sequence[Mapping[str, Any]],
    proposed: Sequence[Mapping[str, Any]],
    latest_acknowledged_generation: int | None,
) -> str:
    """Return "accept" or the compatibility-refusal failure name."""
    honored = {advertisement["generation"]: _advertisement(advertisement) for advertisement in current}
    candidates: dict[int, Message] = {}
    for value in proposed:
        advertisement = _advertisement(value)
        if advertisement["generation"] in candidates:
            return "capability-generation-regression"
        candidates[advertisement["generation"]] = advertisement
    highest = max(honored, default=0)
    for generation, advertisement in candidates.items():
        if generation in honored:
            if advertisement != honored[generation]:
                return "capability-generation-regression"
        elif generation <= highest:
            return "capability-generation-regression"
    for generation in honored:
        if generation not in candidates:
            if latest_acknowledged_generation is None or latest_acknowledged_generation <= generation:
                return "capability-contraction-blocked"
    return "accept"
