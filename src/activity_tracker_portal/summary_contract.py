"""summary-contract@1: the common Health Summary every daily derivation returns.

The header names the summary, the versions and policies that produced it, its
primary state, evidence conditions, coverage, and Data Watermark. The evidence
manifest lists every Source Record the derivation attributed to the day. Each
derive unit supplies only its body (packs/summary-contract-1.spec.md).

Interface stub: the operations are not implemented yet.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime

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


def coverage_of(evidence: Iterable[EvidenceEntry]) -> Coverage:
    """Count the entries by standing (R6)."""
    raise NotImplementedError("summary-contract@1")


def primary_state(coverage: Coverage, has_value: bool) -> str:
    """The one total primary-state rule (R4)."""
    raise NotImplementedError("summary-contract@1")


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
    raise NotImplementedError("summary-contract@1")


def decode_summary(header_bytes: bytes, evidence_bytes: bytes, body_bytes: bytes | None) -> HealthSummary:
    """Check stored bytes and decode them back into the same Health Summary (R12)."""
    raise NotImplementedError("summary-contract@1")


def check_publication_conditions(names: Iterable[str]) -> tuple[str, ...]:
    """Validate a set of publication conditions and return it sorted (R13)."""
    raise NotImplementedError("summary-contract@1")

