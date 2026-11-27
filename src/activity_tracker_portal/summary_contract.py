"""summary-contract@1: the common Health Summary every daily derivation returns.

The header names the summary, the versions and policies that produced it, its
primary state, evidence conditions, coverage, and Data Watermark. The evidence
manifest lists every Source Record the derivation attributed to the day. Each
derive unit supplies only its body (packs/summary-contract-1.spec.md).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from functools import wraps
import hashlib
from inspect import signature
import json
import re
from typing import Any

from ._interpret_projection_json import (
    _check_strings, _finite_float, _number, _reject_constant, _unique_object,
)
from .set_source_selection_policy import MEASURES

CONTRACT_VERSION = "summary-contract@1"

PRIMARY_STATES: tuple[str, ...] = (
    "measured", "partial", "missing", "failed", "quarantined", "policy-pending", "unresolved",
)
# The order the rule uses when no value exists (Decision 4).
NO_VALUE_PRECEDENCE: tuple[str, ...] = ("failed", "quarantined", "policy-pending", "unresolved")
STANDINGS: tuple[str, ...] = (
    "contributing", "unresolved", "in-conflict", "policy-pending", "failed", "quarantined",
)
# Conditions a derive unit reports.
REPORTED_CONDITIONS: tuple[str, ...] = (
    "classification-conflict", "duplicate-grouped", "equal-time-conflict", "selection-unresolved",
    "unallocatable-interval",
)
# Conditions this unit computes from the evidence.
COMPUTED_CONDITIONS: tuple[str, ...] = ("inferred-attribution", "source-conflict")
EVIDENCE_CONDITIONS: tuple[str, ...] = tuple(sorted(REPORTED_CONDITIONS + COMPUTED_CONDITIONS))
# Attached by later units at read time; never stored in a summary (Decision 7).
PUBLICATION_CONDITIONS: tuple[str, ...] = (
    "conflicted", "newer-evidence-pending", "reconciliation-failed", "reconciliation-pending",
    "refresh-failed", "superseded",
)
FAILURES: tuple[str, ...] = (
    "summary-input-invalid", "summary-state-inconsistent", "summary-bytes-invalid",
    "summary-version-unsupported",
)

__all__ = [
    "CONTRACT_VERSION", "COMPUTED_CONDITIONS", "EVIDENCE_CONDITIONS", "FAILURES", "MEASURES",
    "NO_VALUE_PRECEDENCE", "PRIMARY_STATES", "PUBLICATION_CONDITIONS", "REPORTED_CONDITIONS", "STANDINGS",
    "Coverage", "DataWatermark", "DeliveryPosition", "EvidenceEntry", "HealthSummary", "SummaryContractFailure",
    "SummaryHeader", "build_summary", "check_publication_conditions", "coverage_of", "decode_summary",
    "primary_state",
]


class SummaryContractFailure(Exception):
    """A named failure raised instead of a summary; it carries only the failure name."""

    def __init__(self, failure: str) -> None:
        super().__init__(failure)
        self.failure = failure


@dataclass(frozen=True)
class EvidenceEntry:
    """One Source Record the derivation attributed to the day, with its standing (R5)."""

    record_id: int
    selection_id: int
    outcome_id: int | None
    standing: str
    processor_version: str | None
    policy_identity: str | None


@dataclass(frozen=True)
class Coverage:
    """Counts of the attributed Source Records by standing (R6)."""

    contributing: int
    unresolved: int
    in_conflict: int
    policy_pending: int
    failed: int
    quarantined: int


@dataclass(frozen=True)
class DeliveryPosition:
    """One Sender Installation's incorporated delivery position for the summary's record type (R9)."""

    installation_id: str
    sequence: int
    receipt_time: datetime
    processed_at: datetime


@dataclass(frozen=True)
class DataWatermark:
    """The incorporated delivery positions, sorted by installation, and the evidence-set digest (R9)."""

    positions: tuple[DeliveryPosition, ...]
    evidence_set_sha256: str


@dataclass(frozen=True)
class SummaryHeader:
    """The Health Summary header: one field per header entry of R8."""

    contract_version: str
    derivation_version: str
    measure: str
    record_type: str
    local_date: date
    processor_version: str
    source_selection_policy: int | None
    fallback_timezone_identities: tuple[str, ...]
    primary_state: str
    evidence_conditions: tuple[str, ...]
    coverage: Coverage
    watermark: DataWatermark
    body_sha256: str | None


@dataclass(frozen=True)
class HealthSummary:
    """An immutable Health Summary; its repr omits the three byte fields."""

    header: SummaryHeader
    header_bytes: bytes = field(repr=False)
    evidence_bytes: bytes = field(repr=False)
    body_bytes: bytes | None = field(repr=False)
    summary_sha256: str


def _failure_boundary(operation: Callable[..., Any]) -> Callable[..., Any]:
    """Discard parser/validation exceptions before exposing a named failure.

    Clear context inside the new failure's own handler, then re-raise it without
    chaining. Raising after the operation's handler alone would still attach an
    exception being handled by the caller (including input-bearing decoder errors).
    """
    decoding = operation.__name__ == "decode_summary"
    parameters = signature(operation)

    @wraps(operation)
    def checked(*args: Any, **kwargs: Any) -> Any:
        parameters.bind(*args, **kwargs)
        try:
            return operation(*args, **kwargs)
        except SummaryContractFailure as exc:
            failure = exc.failure
            if decoding and failure != "summary-version-unsupported":
                failure = "summary-bytes-invalid"
        except Exception:
            failure = "summary-bytes-invalid" if decoding else "summary-input-invalid"
        try:
            raise SummaryContractFailure(failure)
        except SummaryContractFailure as exc:
            exc.__context__ = None
            raise

    return checked


def _require(condition: bool, failure: str = "summary-input-invalid") -> None:
    if not condition:
        raise SummaryContractFailure(failure)


def _positive_int(value: int) -> None:
    _require(type(value) is int and value > 0)


def _decimal_text(value: int) -> str:
    """Encode an identity without Python's process-wide integer digit limit."""
    chunks = []
    while value >= 1_000_000_000:
        value, remainder = divmod(value, 1_000_000_000)
        chunks.append(f"{remainder:09d}")
    return str(value) + "".join(reversed(chunks))


def _decimal_value(value: str) -> int:
    _require(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value) is not None)
    result = 0
    for start in range(0, len(value), 9):
        chunk = value[start:start + 9]
        result = result * 10 ** len(chunk) + int(chunk)
    return result


def _version(value: str) -> None:
    _require(isinstance(value, str) and
             re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*@[1-9][0-9]*", value) is not None)


def _canonical(value: Any) -> str:
    """Null-aware RFC 8785, sharing the published binary64 number formatter."""
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in (int, float):
        return _number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        return "{" + ",".join(_canonical(key) + ":" + _canonical(value[key]) for key in keys) + "}"
    raise ValueError()


def _encode(value: Any) -> bytes:
    return _canonical(value).encode("utf-8")


def _load_object(data: bytes, *, binary64: bool = False) -> dict[str, Any]:
    _require(type(data) is bytes)
    value = json.loads(
        data.decode("utf-8"), object_pairs_hook=_unique_object,
        parse_int=_finite_float if binary64 else int,
        parse_float=_finite_float, parse_constant=_reject_constant,
    )
    _require(isinstance(value, dict))
    _check_strings(value)
    return value


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@_failure_boundary
def coverage_of(evidence: Iterable[EvidenceEntry]) -> Coverage:
    """Count the entries by standing (R6)."""
    counts = dict.fromkeys(STANDINGS, 0)
    for entry in evidence:
        _require(isinstance(entry, EvidenceEntry) and entry.standing in STANDINGS)
        counts[entry.standing] += 1
    return Coverage(*(counts[standing] for standing in STANDINGS))


@_failure_boundary
def primary_state(coverage: Coverage, has_value: bool) -> str:
    """The one total primary-state rule (R4)."""
    _require(isinstance(coverage, Coverage) and type(has_value) is bool)
    counts = {standing: getattr(coverage, standing.replace("-", "_")) for standing in STANDINGS}
    _require(all(type(count) is int and count >= 0 for count in counts.values()))
    if has_value:
        _require(coverage.contributing > 0, "summary-state-inconsistent")
        return "partial" if any(counts[standing] for standing in STANDINGS[1:]) else "measured"
    if not any(counts.values()):
        return "missing"
    for state in NO_VALUE_PRECEDENCE:
        if counts[state] or (state == "unresolved" and coverage.in_conflict):
            return state
    raise SummaryContractFailure("summary-state-inconsistent")


def _condition_names(names: Iterable[str], vocabulary: tuple[str, ...]) -> tuple[str, ...]:
    _require(not isinstance(names, (str, bytes)))
    result = set()
    for name in names:
        _require(isinstance(name, str) and name in vocabulary)
        result.add(name)
    return tuple(sorted(result))


@_failure_boundary
def check_publication_conditions(names: Iterable[str]) -> tuple[str, ...]:
    """Validate a set of publication conditions and return it sorted (R13)."""
    return _condition_names(names, PUBLICATION_CONDITIONS)


def _validated_evidence(evidence: Iterable[EvidenceEntry], processor_version: str) -> tuple[EvidenceEntry, ...]:
    entries = tuple(evidence)
    seen = set()
    for entry in entries:
        _require(isinstance(entry, EvidenceEntry))
        _positive_int(entry.record_id)
        _positive_int(entry.selection_id)
        _require(entry.record_id not in seen and entry.standing in STANDINGS)
        seen.add(entry.record_id)
        if entry.standing in ("failed", "in-conflict"):
            _require(entry.outcome_id is None and entry.policy_identity is None)
        else:
            _positive_int(entry.outcome_id)
            _require(isinstance(entry.policy_identity, str) and bool(entry.policy_identity))
        if entry.standing == "in-conflict":
            _require(entry.processor_version is None)
        else:
            _require(isinstance(entry.processor_version, str) and entry.processor_version == processor_version)
    return tuple(sorted(entries, key=lambda entry: entry.record_id))


def _evidence_json(entry: EvidenceEntry) -> dict[str, Any]:
    return {
        "record_id": _decimal_text(entry.record_id),
        "selection_id": _decimal_text(entry.selection_id),
        "outcome_id": _decimal_text(entry.outcome_id) if entry.outcome_id is not None else None,
        "standing": entry.standing,
        "processor_version": entry.processor_version,
        "policy_identity": entry.policy_identity,
    }


def _utc(value: datetime) -> datetime:
    _require(isinstance(value, datetime) and value.utcoffset() is not None)
    # Conversion can overflow at the year boundaries; the public boundary maps
    # that to input-invalid. UTC also makes fold-aware comparisons unambiguous.
    return value.astimezone(timezone.utc)


def _validated_positions(positions: Iterable[DeliveryPosition]) -> tuple[DeliveryPosition, ...]:
    result = []
    seen = set()
    for position in positions:
        _require(isinstance(position, DeliveryPosition))
        _require(isinstance(position.installation_id, str) and bool(position.installation_id)
                 and position.installation_id not in seen)
        seen.add(position.installation_id)
        _positive_int(position.sequence)
        receipt = _utc(position.receipt_time)
        processed = _utc(position.processed_at)
        _require(processed >= receipt)
        result.append(DeliveryPosition(position.installation_id, position.sequence, receipt, processed))
    _require(bool(result))
    return tuple(sorted(result, key=lambda position: position.installation_id))


def _time_text(value: datetime) -> str:
    # strftime's year padding is platform-dependent for years before 1000.
    return (f"{value.year:04d}-{value.month:02d}-{value.day:02d}T"
            f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}.{value.microsecond:06d}Z")


def _position_json(position: DeliveryPosition) -> dict[str, str]:
    return {
        "installation_id": position.installation_id,
        "sequence": _decimal_text(position.sequence),
        "receipt_time": _time_text(position.receipt_time),
        "processed_at": _time_text(position.processed_at),
    }


@_failure_boundary
def build_summary(
    *,
    measure: str,
    record_type: str,
    local_date: date,
    derivation_version: str,
    processor_version: str,
    source_selection_policy: int | None,
    evidence: Iterable[EvidenceEntry],
    reported_conditions: Iterable[str],
    positions: Iterable[DeliveryPosition],
    body: bytes | None,
) -> HealthSummary:
    """Check the facts and build the canonical Health Summary (R4-R11)."""
    _require(isinstance(measure, str) and isinstance(record_type, str)
             and record_type in MEASURES.get(measure, ()))
    _require(type(local_date) is date)
    _version(derivation_version)
    _version(processor_version)
    if source_selection_policy is not None:
        _positive_int(source_selection_policy)

    entries = _validated_evidence(evidence, processor_version)
    coverage = coverage_of(entries)
    state = primary_state(coverage, body is not None)
    conditions = set(_condition_names(reported_conditions, REPORTED_CONDITIONS))
    for condition in conditions:
        supported = coverage.contributing >= 2 if condition == "duplicate-grouped" else coverage.unresolved > 0
        _require(supported, "summary-state-inconsistent")
    if coverage.in_conflict:
        conditions.add("source-conflict")
    if any(entry.standing == "contributing" and entry.policy_identity != "none" for entry in entries):
        conditions.add("inferred-attribution")
    if body is not None:
        # All number tokens, including integer tokens, denote binary64 values.
        # Re-encoding rejects every alternate spelling at every nesting level.
        _require(_encode(_load_object(body, binary64=True)) == body)

    ordered_positions = _validated_positions(positions)
    evidence_bytes = _encode({
        "contract_version": CONTRACT_VERSION,
        "evidence": [_evidence_json(entry) for entry in entries],
    })
    evidence_digest = _digest(evidence_bytes)
    body_digest = _digest(body) if body is not None else None
    fallback_identities = tuple(sorted({
        entry.policy_identity for entry in entries if entry.policy_identity not in (None, "none")
    }))
    ordered_conditions = tuple(sorted(conditions))
    header = SummaryHeader(
        CONTRACT_VERSION, derivation_version, measure, record_type, local_date,
        processor_version, source_selection_policy, fallback_identities, state,
        ordered_conditions, coverage, DataWatermark(ordered_positions, evidence_digest), body_digest,
    )
    header_bytes = _encode({
        "contract_version": CONTRACT_VERSION,
        "derivation_version": derivation_version,
        "measure": measure,
        "record_type": record_type,
        "local_date": local_date.isoformat(),
        "processor_version": processor_version,
        "source_selection_policy": (
            _decimal_text(source_selection_policy) if source_selection_policy is not None else None
        ),
        "fallback_timezone_identities": list(fallback_identities),
        "primary_state": state,
        "evidence_conditions": list(ordered_conditions),
        "coverage": {standing: getattr(coverage, standing.replace("-", "_")) for standing in STANDINGS},
        "watermark": {
            "evidence_set_sha256": evidence_digest,
            "positions": [_position_json(position) for position in ordered_positions],
        },
        "body_sha256": body_digest,
    })
    return HealthSummary(header, header_bytes, evidence_bytes, body, _digest(header_bytes))


@_failure_boundary
def decode_summary(header_bytes: bytes, evidence_bytes: bytes, body_bytes: bytes | None) -> HealthSummary:
    """Check stored bytes and decode them back into the same Health Summary (R12)."""
    header = _load_object(header_bytes)
    # A future version may have an entirely different shape. Dispatch on its
    # version before inspecting this version's keys or enforcing canonicality.
    _require(header["contract_version"] == CONTRACT_VERSION, "summary-version-unsupported")
    manifest = _load_object(evidence_bytes)
    _require(manifest["contract_version"] == CONTRACT_VERSION, "summary-version-unsupported")
    _require(isinstance(manifest["evidence"], list))
    entries = tuple(
        EvidenceEntry(
            _decimal_value(entry["record_id"]), _decimal_value(entry["selection_id"]),
            _decimal_value(entry["outcome_id"]) if entry["outcome_id"] is not None else None,
            entry["standing"], entry["processor_version"], entry["policy_identity"],
        )
        for entry in manifest["evidence"]
    )
    _require(isinstance(header["watermark"]["positions"], list))
    positions = tuple(
        DeliveryPosition(
            position["installation_id"], _decimal_value(position["sequence"]),
            datetime.fromisoformat(position["receipt_time"]), datetime.fromisoformat(position["processed_at"]),
        )
        for position in header["watermark"]["positions"]
    )
    _require(isinstance(header["evidence_conditions"], list))
    conditions = tuple(condition for condition in header["evidence_conditions"] if condition not in COMPUTED_CONDITIONS)
    result = build_summary(
        measure=header["measure"], record_type=header["record_type"],
        local_date=date.fromisoformat(header["local_date"]),
        derivation_version=header["derivation_version"], processor_version=header["processor_version"],
        source_selection_policy=(
            _decimal_value(header["source_selection_policy"]) if header["source_selection_policy"] is not None else None
        ),
        evidence=entries, reported_conditions=conditions, positions=positions, body=body_bytes,
    )
    # Rebuilding verifies exact keys, nulls, decimal and time forms, ordering,
    # computed conditions, coverage, state, and all content digests together.
    _require(result.header_bytes == header_bytes and result.evidence_bytes == evidence_bytes)
    return result
