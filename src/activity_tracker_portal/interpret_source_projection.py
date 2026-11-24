"""interpret-source-projection@1: interpret one reconciled Source Projection into exactly one outcome.

The common layer owns identity, provenance, time attribution, and canonical bytes.
Processors supply only their record-type-specific body.

Policy identities have the canonical form ``fallback-timezone:<JSON>``. The JSON
is the compact UTF-8 array ``[sorted_used_policy_ids, tzdata.IANA_VERSION]``.
Policy IDs appear once, in Unicode code point order; unused policies contribute
nothing. A fully experienced header has the identity ``none``.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from importlib.resources import files as resource_files
import json
import math
import re
from zoneinfo import ZoneInfo

import tzdata

from ._interpret_projection_json import canonical_bytes, parse_object
from ._interpret_projection_schema import projection_schema, schema_issue, schema_pointer
from typing import Any, Literal, Protocol, runtime_checkable

COMMON_VERSION = "interpret-source-projection@1"

UNITS: frozenset[str] = frozenset({
    "g",
    "cal",
    "m",
    "L",
    "m/s",
    "W",
    "mmHg",
    "degC",
    "mmol/L",
    "percent",
    "count",
    "1/min",
    "ns",
    "mL/(kg*min)",
})


class InterpretFailure(Exception):
    """A named failure raised instead of an outcome; it carries no projection bytes, values, or Source Identity."""

    def __init__(self, failure: str) -> None:
        super().__init__(failure)
        self.failure = failure


# ---- Input ----

@dataclass(frozen=True)
class ProcessingInput:
    """One current projection selection as operational-store@3 holds it."""

    source_system_kind: str
    origin: str
    record_type: str
    source_identity: str
    record_id: int
    selection_id: int
    evidence_id: int
    projection_sha256: str
    last_modified_time: datetime
    projection_schema_version: int
    projection: bytes


@dataclass(frozen=True)
class FallbackTimezonePolicy:
    """One effective-dated Fallback Timezone Policy; `effective_from` None means all earlier history."""

    policy_id: str
    effective_from: datetime | None
    zone: str


# ---- Time ----

@dataclass(frozen=True)
class Instant:
    """A projection instant: its exact RFC 3339 text and the same instant in nanoseconds since the Unix epoch."""

    text: str
    epoch_ns: int


@dataclass(frozen=True)
class Endpoint:
    """One endpoint of the temporal header with its offsets, local date, and attribution."""

    instant: Instant
    source_offset: str | None
    applied_offset: str
    local_date: date
    attribution: Literal["experienced", "inferred"]
    policy_id: str | None


@dataclass(frozen=True)
class TemporalHeader:
    """The temporal shape of one projection; an instant has only `start`."""

    shape: Literal["instant", "interval"]
    start: Endpoint
    end: Endpoint | None
    explicit_time: bool


# ---- Processor plug-in ----

@dataclass(frozen=True)
class NumericField:
    """A declared number in a body: a JSON pointer with `*` for any list index, its kind, and its canonical unit."""

    pointer: str
    kind: Literal["integer", "float", "code"]
    unit: str | None


@dataclass(frozen=True)
class BodySchema:
    """A processor's body schema: its record type, body version, and every numeric field."""

    record_type: str
    body_version: int
    numeric_fields: tuple[NumericField, ...]


# ---- Outcomes ----

@dataclass(frozen=True)
class Quarantine:
    """A projection that cannot be interpreted safely; a reason and a field pointer, never a value."""

    reason: str
    pointer: str
    processor_version: str = COMMON_VERSION
    policy_identity: str = "none"


@dataclass(frozen=True)
class Unmodeled:
    """A valid projection with no interpretation yet."""

    reason: str
    processor_version: str = COMMON_VERSION
    policy_identity: str = "none"


@dataclass(frozen=True)
class PolicyPending:
    """A valid, modeled projection whose interpretation needs a policy that does not apply yet."""

    policy_kind: str
    reason: str
    processor_version: str = COMMON_VERSION
    policy_identity: str = "none"


@dataclass(frozen=True)
class RevisionHeader:
    """The common Canonical Revision header (R10); `header_bytes` is its RFC 8785 form."""

    source_system_kind: str
    origin: str
    record_type: str
    source_identity: str
    record_id: int
    selection_id: int
    evidence_id: int
    projection_sha256: str
    projection_schema_version: int
    projection_implementation_version: str
    sdk_version: str
    provider_version: str
    last_modified_time: Instant
    recording_method: int
    device: Mapping[str, Any] | None
    time: TemporalHeader
    header_version: str
    processor_version: str
    body_schema: BodySchema
    policy_identity: str
    body_sha256: str


@dataclass(frozen=True)
class CanonicalRevision:
    """An immutable Canonical Revision: the common header and the processor's body, as canonical bytes."""

    header: RevisionHeader
    header_bytes: bytes
    body_bytes: bytes
    body_sha256: str
    revision_sha256: str
    processor_version: str
    policy_identity: str


@dataclass(frozen=True)
class Envelope:
    """The index key and modification time decoded from one projection."""

    source_system_kind: str
    origin: str
    source_identity: str
    last_modified_time: datetime
    last_modified_instant: Instant


@runtime_checkable
class Processor(Protocol):
    """A concrete processor: it contributes only the record-type-specific body."""

    record_type: str
    processor_version: str
    supported_schema_versions: Collection[int]
    body_schema: BodySchema
    quarantine_reasons: Collection[str]

    def interpret_body(self, record: Mapping[str, Any], temporal: TemporalHeader) -> Mapping[str, Any] | Quarantine:
        ...


# ---- Common validation and exact time arithmetic ----

_NS = 1_000_000_000
_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
_UTC = timezone.utc
_SAFE_INTEGER = 2**53 - 1
_INSTANT = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{3}|[0-9]{6}|[0-9]{9}))?Z"
)
_REASON = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_PROCESSOR_VERSION = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*@[1-9][0-9]*")
_COMMON_REASONS = frozenset({
    "projection-not-json", "projection-envelope-invalid", "projection-schema-invalid",
    "instant-out-of-range", "interval-reversed", "record-type-not-modeled",
    "schema-version-not-modeled", "fallback-timezone-policy-missing",
})


class _InstantOutOfRange(ValueError):
    """A well-formed projection instant outside datetime's domain."""


def _datetime_ns(value: datetime) -> int:
    """Compare aware datetimes as instants without floats or UTC-range overflow."""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError()
    delta = value.replace(tzinfo=None) - datetime(1970, 1, 1) - value.utcoffset()
    return (delta.days * 86400 + delta.seconds) * _NS + delta.microseconds * 1000


def _parse_instant(text: str) -> tuple[Instant, datetime]:
    match = _INSTANT.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise ValueError()
    year, month, day, hour, minute, second = map(int, match.groups()[:6])
    # Validate year zero as Gregorian too, before reporting the representation limit.
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    if not (1 <= month <= 12 and 1 <= day <= days[month - 1]
            and hour < 24 and minute < 60 and second < 60):
        raise ValueError()
    if year == 0:
        raise _InstantOutOfRange()
    fraction = int((match[7] or "").ljust(9, "0"))
    value = datetime(year, month, day, hour, minute, second, fraction // 1000, _UTC)
    seconds = ((value.toordinal() - _EPOCH_ORDINAL) * 86400
               + hour * 3600 + minute * 60 + second)
    return Instant(text, seconds * _NS + fraction), value


def _decode_object(obj: Mapping[str, Any], record_type: str, schema_version: int) -> Envelope | Quarantine:
    def invalid(pointer: str) -> Quarantine:
        return Quarantine("projection-envelope-invalid", pointer)

    if not isinstance(obj.get("record_type"), str) or obj["record_type"] != record_type:
        return invalid("/record_type")
    if type(obj.get("projection_schema_version")) is not int or obj["projection_schema_version"] != schema_version:
        return invalid("/projection_schema_version")
    metadata = obj.get("metadata")
    if not isinstance(metadata, dict):
        return invalid("/metadata")
    if not isinstance(metadata.get("id"), str) or not metadata["id"]:
        return invalid("/metadata/id")
    origin = metadata.get("data_origin")
    if not isinstance(origin, dict):
        return invalid("/metadata/data_origin")
    if not isinstance(origin.get("package_name"), str) or not origin["package_name"]:
        return invalid("/metadata/data_origin/package_name")
    try:
        instant, value = _parse_instant(metadata.get("last_modified_time"))
    except _InstantOutOfRange:
        return Quarantine("instant-out-of-range", "/metadata/last_modified_time")
    except ValueError:
        return invalid("/metadata/last_modified_time")
    return Envelope("health-connect", origin["package_name"], metadata["id"], value, instant)


@dataclass(frozen=True)
class _Policy:
    policy_id: str
    effective_ns: int | None
    zone: ZoneInfo


def _load_zone(name: str) -> ZoneInfo:
    # Never call ZoneInfo(name): that consults TZPATH and the host database first.
    # Validate components before joining a package resource, including on Windows.
    if not isinstance(name, str) or "\\" in name or any(
        part in ("", ".", "..") for part in name.split("/")
    ):
        raise ValueError()
    with resource_files("tzdata.zoneinfo").joinpath(*name.split("/")).open("rb") as stream:
        return ZoneInfo.from_file(stream, key=name)


def _validated_input(
    input: ProcessingInput, processor: Processor | None, policies: Sequence[FallbackTimezonePolicy],
) -> tuple[_Policy, ...] | None:
    """Contain malformed caller objects without retaining their exception context."""
    try:
        _datetime_ns(input.last_modified_time)
        if any(type(value) is not int or value <= 0
               for value in (input.record_id, input.selection_id, input.evidence_id)):
            return None
        if not isinstance(input.projection, bytes):
            return None
        if processor is not None and processor.record_type != input.record_type:
            return None
        result = []
        seen = set()
        previous = None
        for index, policy in enumerate(policies):
            if not isinstance(policy.policy_id, str) or not policy.policy_id or policy.policy_id in seen:
                return None
            # Policy identities are caller input and later enter UTF-8 headers.
            if any(0xD800 <= ord(character) <= 0xDFFF for character in policy.policy_id):
                return None
            seen.add(policy.policy_id)
            start = None if policy.effective_from is None else _datetime_ns(policy.effective_from)
            if start is None:
                if index != 0:
                    return None
            elif previous is not None and start <= previous:
                return None
            previous = start
            result.append(_Policy(policy.policy_id, start, _load_zone(policy.zone)))
        return tuple(result)
    except Exception:
        return None


def _policy_identity(used: set[str]) -> str:
    if not used:
        return "none"
    return "fallback-timezone:" + json.dumps(
        [sorted(used), tzdata.IANA_VERSION], ensure_ascii=False, separators=(",", ":")
    )


def _offset_text(seconds: int) -> str:
    magnitude = abs(seconds)
    result = f"{'-' if seconds < 0 else '+'}{magnitude // 3600:02d}:{magnitude // 60 % 60:02d}"
    return result + (f":{magnitude % 60:02d}" if magnitude % 60 else "")


@dataclass(frozen=True)
class _Attributed:
    endpoint: Endpoint | None
    policy_id: str | None = None
    out_of_range: bool = False


def _attribute(instant: Instant, source_offset: str | None, policies: tuple[_Policy, ...]) -> _Attributed:
    # datetime is used only for timezone conversion; exact comparisons use epoch_ns.
    _, value = _parse_instant(instant.text)
    if source_offset is not None:
        seconds = (int(source_offset[1:3]) * 3600 + int(source_offset[4:6]) * 60)
        if source_offset[0] == "-":
            seconds = -seconds
        try:
            local_date = (value + timedelta(seconds=seconds)).date()
        except OverflowError:
            return _Attributed(None, out_of_range=True)
        return _Attributed(Endpoint(instant, source_offset, source_offset, local_date, "experienced", None))
    applicable = None
    for policy in policies:
        if policy.effective_ns is not None and policy.effective_ns > instant.epoch_ns:
            break
        applicable = policy
    if applicable is None:
        return _Attributed(None)
    try:
        local = value.astimezone(applicable.zone)
    except OverflowError:
        # Keep the applied policy even when its local date cannot be represented.
        return _Attributed(None, applicable.policy_id, True)
    delta = local.utcoffset()
    seconds = delta.days * 86400 + delta.seconds
    return _Attributed(Endpoint(
        instant, None, _offset_text(seconds), local.date(), "inferred", applicable.policy_id,
    ), applicable.policy_id)


# ---- Processor boundary ----

def _numeric_pointer(pointer: str) -> tuple[str, ...]:
    """Decode an RFC 6901 pointer; '*' is reserved for list indices."""
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ValueError()
    parts = pointer[1:].split("/") if pointer else []
    if any(re.search(r"~(?![01])", part) for part in parts):
        raise ValueError()
    return tuple(part.replace("~1", "/").replace("~0", "~") for part in parts)


def _body_bytes(body: Mapping[str, Any], schema: BodySchema) -> bytes:
    if not isinstance(body, Mapping):
        raise ValueError()
    declarations = []
    for field in schema.numeric_fields:
        if field.kind not in ("integer", "float", "code"):
            raise ValueError()
        if (field.kind == "code" and field.unit is not None
                or field.kind != "code" and field.unit not in UNITS):
            raise ValueError()
        declarations.append((_numeric_pointer(field.pointer), field.kind))

    def walk(value: Any, path: tuple[str | int, ...]) -> None:
        matches = [kind for parts, kind in declarations if len(parts) == len(path) and all(
            (part == "*" and type(key) is int) or (part != "*" and part == str(key))
            for part, key in zip(parts, path)
        )]
        numeric = type(value) in (int, float)
        if matches:
            if len(matches) != 1:
                raise ValueError()
            expected = float if matches[0] == "float" else int
            if type(value) is not expected:
                raise ValueError()
        if numeric:
            if not matches or (type(value) is int and abs(value) > _SAFE_INTEGER):
                raise ValueError()
            if type(value) is float and not math.isfinite(value):
                raise ValueError()
        elif isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError()
                walk(item, path + (key,))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, path + (index,))
        elif not isinstance(value, (str, bool)):
            raise ValueError()

    walk(body, ())
    return canonical_bytes(body)


def _processor_result(
    processor: Processor, record: Mapping[str, Any], temporal: TemporalHeader,
    source_schema: Mapping[str, Any], version: str, identity: str,
) -> tuple[bytes, BodySchema] | Quarantine | None:
    try:
        result = processor.interpret_body(record, temporal)
        if isinstance(result, Quarantine):
            if (not isinstance(result.reason, str) or _REASON.fullmatch(result.reason) is None
                    or result.reason in _COMMON_REASONS
                    or result.reason not in processor.quarantine_reasons
                    or not schema_pointer(source_schema, result.pointer)):
                return None
            return Quarantine(result.reason, result.pointer, version, identity)
        schema = processor.body_schema
        if (not isinstance(version, str) or _PROCESSOR_VERSION.fullmatch(version) is None
                or schema.record_type != processor.record_type
                or type(schema.body_version) is not int
                or not 1 <= schema.body_version <= _SAFE_INTEGER):
            return None
        return _body_bytes(result, schema), schema
    except Exception:
        # Return across the exception boundary before raising a named failure.
        # Neither an explicit nor implicit exception chain retains processor data.
        return None


# ---- Revision bytes ----

def _endpoint_json(endpoint: Endpoint) -> dict[str, Any]:
    result = {
        "instant": endpoint.instant.text,
        "applied_offset": endpoint.applied_offset,
        "local_date": endpoint.local_date.isoformat(),
        "attribution": endpoint.attribution,
    }
    if endpoint.source_offset is not None:
        result["source_offset"] = endpoint.source_offset
    if endpoint.policy_id is not None:
        result["policy_id"] = endpoint.policy_id
    return result


def _decimal_identity(value: int) -> str:
    # Avoid the interpreter's int-to-string digit limit without changing global
    # settings. Store IDs remain exact even beyond the usual SQLite key domain.
    chunks = []
    while value:
        value, remainder = divmod(value, 1_000_000_000)
        chunks.append(remainder)
    return str(chunks[-1]) + "".join(f"{chunk:09d}" for chunk in reversed(chunks[:-1]))


def _header_bytes(header: RevisionHeader) -> bytes | None:
    result = {field.name: getattr(header, field.name) for field in fields(header)}
    for name in ("record_id", "selection_id", "evidence_id"):
        result[name] = _decimal_identity(result[name])
    result["last_modified_time"] = header.last_modified_time.text
    if header.device is None:
        del result["device"]
    result["body_schema"] = {
        "record_type": header.body_schema.record_type,
        "body_version": header.body_schema.body_version,
    }
    temporal = header.time
    result["time"] = {
        "shape": temporal.shape, "explicit_time": temporal.explicit_time,
        "start": _endpoint_json(temporal.start),
    }
    if temporal.end is not None:
        result["time"]["end"] = _endpoint_json(temporal.end)
    try:
        return canonical_bytes(result)
    except ValueError:
        # Serialization is part of the R10 invariant. Return before constructing
        # its named failure so neither exception chain can retain header data.
        return None


# ---- Operations ----

def decode_envelope(projection: bytes, record_type: str, projection_schema_version: int) -> Envelope | Quarantine:
    """Decode an indexable envelope, leaving body-schema validation to interpret."""
    try:
        obj = parse_object(projection)
    except (ValueError, UnicodeError, RecursionError):
        return Quarantine("projection-not-json", "")
    return _decode_object(obj, record_type, projection_schema_version)


def interpret(
    input: ProcessingInput,
    processor: Processor | None,
    policies: Sequence[FallbackTimezonePolicy],
) -> CanonicalRevision | Unmodeled | PolicyPending | Quarantine:
    """Interpret in R4 order; integrity or processor defects raise named failures."""
    timeline = _validated_input(input, processor, policies)
    if timeline is None:
        raise InterpretFailure("processing-input-invalid")
    if sha256(input.projection).hexdigest() != input.projection_sha256:
        raise InterpretFailure("projection-digest-mismatch")
    envelope = decode_envelope(input.projection, input.record_type, input.projection_schema_version)
    if isinstance(envelope, Quarantine) or (
        envelope.source_system_kind != input.source_system_kind
        or envelope.origin != input.origin or envelope.source_identity != input.source_identity
        or _datetime_ns(envelope.last_modified_time) != _datetime_ns(input.last_modified_time)
    ):
        raise InterpretFailure("projection-identity-mismatch")
    projection = parse_object(input.projection)
    version = processor.processor_version if processor is not None else COMMON_VERSION
    schema = projection_schema(input.record_type, input.projection_schema_version)
    issue = schema_issue(projection, schema) if schema is not None else "/record_type"
    if issue is not None:
        return Quarantine("projection-schema-invalid", issue, version)
    record = projection["record"]
    interval = "start_time" in record
    time_fields = ("start_time", "end_time") if interval else ("time",)
    instants = []
    for name in time_fields:
        try:
            instants.append(_parse_instant(record[name])[0])
        except _InstantOutOfRange:
            return Quarantine("instant-out-of-range", "/record/" + name, version)
        except ValueError:
            # Keep temporal validation governed even if schema and parser drift.
            return Quarantine("projection-schema-invalid", "/record/" + name, version)
    if processor is None:
        return Unmodeled("record-type-not-modeled")
    if input.projection_schema_version not in processor.supported_schema_versions:
        return Unmodeled("schema-version-not-modeled", version)
    if interval and instants[1].epoch_ns < instants[0].epoch_ns:
        return Quarantine("interval-reversed", "/record/end_time", version)
    offset_fields = ("start_zone_offset", "end_zone_offset") if interval else ("zone_offset",)
    attributed = [_attribute(instant, record.get(name), timeline)
                  for instant, name in zip(instants, offset_fields)]
    identity = _policy_identity({entry.policy_id for entry in attributed if entry.policy_id is not None})
    for name, entry in zip(time_fields, attributed):
        if entry.out_of_range:
            return Quarantine("instant-out-of-range", "/record/" + name, version, identity)
    if any(entry.endpoint is None for entry in attributed):
        return PolicyPending("fallback-timezone", "fallback-timezone-policy-missing", version, identity)
    temporal = TemporalHeader(
        "interval" if interval else "instant", attributed[0].endpoint,
        attributed[1].endpoint if interval else None, record.get("has_explicit_time", True),
    )
    result = _processor_result(processor, record, temporal, schema, version, identity)
    if result is None:
        raise InterpretFailure("processor-invariant-violated")
    if isinstance(result, Quarantine):
        return result
    body_bytes, body_schema = result
    body_digest = sha256(body_bytes).hexdigest()
    metadata = projection["metadata"]
    header = RevisionHeader(
        source_system_kind=input.source_system_kind, origin=input.origin,
        record_type=input.record_type, source_identity=input.source_identity,
        record_id=input.record_id, selection_id=input.selection_id, evidence_id=input.evidence_id,
        projection_sha256=input.projection_sha256, projection_schema_version=input.projection_schema_version,
        projection_implementation_version=projection["projection_implementation_version"],
        sdk_version=projection["sdk_version"], provider_version=projection["provider_version"],
        last_modified_time=envelope.last_modified_instant, recording_method=metadata["recording_method"],
        device=metadata.get("device"), time=temporal, header_version=COMMON_VERSION,
        processor_version=version, body_schema=body_schema, policy_identity=identity, body_sha256=body_digest,
    )
    header_bytes = _header_bytes(header)
    if header_bytes is None:
        raise InterpretFailure("processor-invariant-violated")
    return CanonicalRevision(
        header, header_bytes, body_bytes, body_digest, sha256(header_bytes).hexdigest(), version, identity,
    )
