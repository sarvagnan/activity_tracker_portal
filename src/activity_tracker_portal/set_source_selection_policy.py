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

from .operational_store import (
    KnownOrigin,
    OriginKey,
    SourceSelectionPolicyRecord,
    StoreRefusal,
    StoreView,
)

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


def _validate_measure_pair(measure: str, record_type: str) -> None:
    if not isinstance(measure, str) or not isinstance(record_type, str):
        raise SourceSelectionRefusal("selection-request-invalid")
    if record_type not in MEASURES.get(measure, ()):
        raise SourceSelectionRefusal("selection-measure-unknown")


def _validated_request(request: SourceSelectionRequest) -> SourceSelectionRequest:
    """Validate before reading the store, and give origin order one meaning."""
    if not isinstance(request, SourceSelectionRequest):
        raise SourceSelectionRefusal("selection-request-invalid")
    if (
        not isinstance(request.measure, str) or not isinstance(request.record_type, str)
        or not isinstance(request.origins, tuple) or not request.origins
        or any(
            not isinstance(origin, OriginKey)
            or not isinstance(origin.source_system_kind, str)
            or not isinstance(origin.origin, str)
            for origin in request.origins
        )
        or len(set(request.origins)) != len(request.origins)
        or any(bound is not None and type(bound) is not date
               for bound in (request.first_date, request.last_date))
        or (request.first_date is not None and request.last_date is not None
            and request.last_date < request.first_date)
    ):
        raise SourceSelectionRefusal("selection-request-invalid")
    _validate_measure_pair(request.measure, request.record_type)
    return SourceSelectionRequest(
        request.measure,
        request.record_type,
        tuple(sorted(request.origins, key=lambda origin: (origin.source_system_kind, origin.origin))),
        request.first_date,
        request.last_date,
    )


def _inspect_request(
    view: StoreView, request: SourceSelectionRequest,
) -> tuple[SourceSelectionPreview, SourceSelectionPolicyRecord | None]:
    """Check origins and compute the preview from the caller's single view."""
    known = {
        OriginKey(origin.source_system_kind, origin.origin)
        for origin in view.known_origins(request.record_type)
    }
    if not set(request.origins) <= known:
        raise SourceSelectionRefusal("selection-origin-unknown")

    timeline = view.source_selection_timeline(request.measure, request.record_type)
    # Clip open ends to Python's date domain only for arithmetic. Equality and
    # exposed residuals retain the original bounds, so None stays distinct.
    first = (request.first_date or date.min).toordinal()
    last = (request.last_date or date.max).toordinal()
    supersedes: list[int] = []
    uncovered: list[DateRange] = []
    for policy in timeline:
        if (
            policy.origins == request.origins
            and policy.first_date == request.first_date
            and policy.last_date == request.last_date
        ):
            return SourceSelectionPreview(request, policy.policy_id, (), ()), policy

        policy_first = (policy.first_date or date.min).toordinal()
        policy_last = (policy.last_date or date.max).toordinal()
        if policy_last < first or policy_first > last:
            continue
        supersedes.append(policy.policy_id)
        # Strict comparisons ensure predecessor/successor arithmetic never
        # crosses date.min or date.max. Existing timeline gaps are not added.
        if policy_first < first:
            uncovered.append(DateRange(policy.first_date, date.fromordinal(first - 1)))
        if policy_last > last:
            uncovered.append(DateRange(date.fromordinal(last + 1), policy.last_date))

    # The published timeline is in date order; its disjoint policies can leave
    # at most one residual on either side of the request.
    return SourceSelectionPreview(
        request, None, tuple(sorted(supersedes)), tuple(uncovered),
    ), None


def source_selection_choices(store: object, measure: str, record_type: str) -> SourceSelectionChoices:
    """Offer the known origins and live policies without reading health evidence."""
    _validate_measure_pair(measure, record_type)
    with store.read() as view:
        return SourceSelectionChoices(
            measure, record_type,
            view.known_origins(record_type),
            view.source_selection_timeline(measure, record_type),
        )


def preview_source_selection(store: object, request: SourceSelectionRequest) -> SourceSelectionPreview:
    """Describe a request's replacement and uncovered dates without writing."""
    request = _validated_request(request)
    with store.read() as view:
        preview, _ = _inspect_request(view, request)
    return preview


def set_source_selection_policy(
    store: object,
    request: SourceSelectionRequest,
    confirmed_supersedes: tuple[int, ...],
    clock: Callable[[], datetime],
) -> SourceSelectionResult:
    """Record once if the confirmed supersession is current, or return a repeat."""
    request = _validated_request(request)
    if (
        not isinstance(confirmed_supersedes, tuple)
        or any(type(policy_id) is not int or policy_id <= 0 for policy_id in confirmed_supersedes)
        or len(set(confirmed_supersedes)) != len(confirmed_supersedes)
    ):
        raise SourceSelectionRefusal("selection-request-invalid")

    with store.read() as view:
        preview, repeated = _inspect_request(view, request)
    if repeated is not None:
        return SourceSelectionResult(repeated, False, ())
    if set(preview.supersedes) != set(confirmed_supersedes):
        raise SourceSelectionRefusal("selection-changed")

    at = clock()
    try:
        policy = store.record_source_selection_policy(
            measure=request.measure,
            record_type=request.record_type,
            origins=request.origins,
            first_date=request.first_date,
            last_date=request.last_date,
            supersedes=preview.supersedes,
            at=at,
        )
    except StoreRefusal as refusal:
        if refusal.failure in ("policy-overlap", "policy-supersession-invalid"):
            raise SourceSelectionRefusal("selection-changed") from None
        raise
    # The store may have returned an identical policy that landed after our
    # read. recorded reports the write path, not which caller inserted it.
    return SourceSelectionResult(policy, True, preview.uncovered)
