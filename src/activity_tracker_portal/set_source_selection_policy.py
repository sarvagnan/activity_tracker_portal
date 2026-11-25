"""Set Source Selection Policy (set-source-selection-policy@1).

The Operator's side of a Source Selection Policy: the closed measure
vocabulary, the choices and preview the Portal shows, and recording one
confirmed request as a pending policy through operational-store@5
(packs/set-source-selection-policy-1.spec.md).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType

from .operational_store import KnownOrigin, OriginKey, SourceSelectionPolicyRecord

# Each derived measure and the record types it may name (Decisions 2 and 3).
MEASURES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "daily-steps": ("steps",),
    "heart-rate-summary": ("heart-rate",),
    "sleep-summary": ("sleep-session",),
    "weight-series": ("weight",),
})


class SourceSelectionRefusal(Exception):
    """A named refusal that wrote nothing; it carries only the failure name."""

    def __init__(self, failure: str) -> None:
        super().__init__(failure)
        self.failure = failure


@dataclass(frozen=True)
class SourceSelectionRequest:
    """The Operator's request; the operations validate it, construction does not."""

    measure: str
    record_type: str
    origins: tuple[OriginKey, ...]
    first_date: date | None
    last_date: date | None


@dataclass(frozen=True)
class DateRange:
    """Inclusive local dates; None is an open end."""

    first_date: date | None
    last_date: date | None


@dataclass(frozen=True)
class SourceSelectionChoices:
    """The known origins of a record type and the live policies of one measure."""

    measure: str
    record_type: str
    origins: tuple[KnownOrigin, ...]
    timeline: tuple[SourceSelectionPolicyRecord, ...]


@dataclass(frozen=True)
class SourceSelectionPreview:
    """What a request would do, without writing."""

    request: SourceSelectionRequest
    repeat_of: int | None
    supersedes: tuple[int, ...]
    uncovered: tuple[DateRange, ...]


@dataclass(frozen=True)
class SourceSelectionResult:
    """The recorded or repeated pending policy and the dates its supersession leaves uncovered."""

    policy: SourceSelectionPolicyRecord
    recorded: bool
    uncovered: tuple[DateRange, ...]


def source_selection_choices(store: object, measure: str, record_type: str) -> SourceSelectionChoices:
    raise NotImplementedError("set-source-selection-policy@1")


def preview_source_selection(store: object, request: SourceSelectionRequest) -> SourceSelectionPreview:
    raise NotImplementedError("set-source-selection-policy@1")


def set_source_selection_policy(
    store: object,
    request: SourceSelectionRequest,
    confirmed_supersedes: tuple[int, ...],
    clock: Callable[[], datetime],
) -> SourceSelectionResult:
    raise NotImplementedError("set-source-selection-policy@1")
