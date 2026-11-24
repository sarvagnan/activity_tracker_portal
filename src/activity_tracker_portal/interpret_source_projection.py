"""interpret-source-projection@1: interpret one reconciled Source Projection into exactly one outcome.

Interface stub for packs/interpret-source-projection-1.spec.md; the implementation follows the frozen pack.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
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


# ---- Operations ----

def decode_envelope(projection: bytes, record_type: str, projection_schema_version: int) -> Envelope | Quarantine:
    raise NotImplementedError("interpret-source-projection@1")


def interpret(
    input: ProcessingInput,
    processor: Processor | None,
    policies: Sequence[FallbackTimezonePolicy],
) -> CanonicalRevision | Unmodeled | PolicyPending | Quarantine:
    raise NotImplementedError("interpret-source-projection@1")
