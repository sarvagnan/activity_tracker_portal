"""range-summary-contract@1: the shared range result every range composition builds on.

It defines the inclusive local-date periods of the presets, the rolling year, calendar years, calendar months,
ISO weeks, and custom ranges; checks the daily Health Summaries given for a period; and counts dates by primary
state, evidence condition, and publication condition, with summed coverage and a `complete` flag
(packs/range-summary-contract-1.spec.md). It never reads a summary body, a clock, a store, or a file.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import wraps
from inspect import signature
from typing import Any

from . import summary_contract as _summary_contract
from .summary_contract import (
    EVIDENCE_CONDITIONS,
    MEASURES,
    PRIMARY_STATES,
    PUBLICATION_CONDITIONS,
    Coverage,
    HealthSummary,
)

CONTRACT_VERSION = "range-summary-contract@1"
PRESET_LENGTHS: tuple[int, ...] = (7, 30, 90)
PERIOD_KINDS: tuple[str, ...] = (
    "last-7-dates", "last-30-dates", "last-90-dates", "last-year", "calendar-year", "calendar-month", "iso-week",
    "custom",
)
MAX_PERIOD_DATES = 366
FAILURES: tuple[str, ...] = ("range-period-invalid", "range-input-invalid")

__all__ = [
    "CONTRACT_VERSION", "EVIDENCE_CONDITIONS", "FAILURES", "MAX_PERIOD_DATES", "MEASURES", "PERIOD_KINDS",
    "PRESET_LENGTHS", "PRIMARY_STATES", "PUBLICATION_CONDITIONS",
    "RangeDay", "RangePeriod", "RangeSummary", "RangeSummaryFailure",
    "calendar_month_period", "calendar_year_period", "custom_period", "iso_week_period", "last_year_period",
    "period_dates", "preset_period", "summarize_range",
]


class RangeSummaryFailure(Exception):
    """A named failure raised instead of a period or range result; it carries only the failure name."""

    def __init__(self, failure: str) -> None:
        super().__init__(failure)
        self.failure = failure


@dataclass(frozen=True)
class RangePeriod:
    """A period kind and its first and last local dates, both inclusive (R4-R8)."""

    kind: str
    first_date: date
    last_date: date


@dataclass(frozen=True)
class RangeDay:
    """One daily Health Summary and the publication conditions the caller attached to it (R9)."""

    summary: HealthSummary
    publication_conditions: Iterable[str]


@dataclass(frozen=True)
class RangeSummary:
    """The shared range result (R10-R14)."""

    contract_version: str
    measure: str
    record_type: str
    period: RangePeriod
    date_count: int
    valued_dates: tuple[date, ...]
    absent_dates: tuple[date, ...]
    primary_state_counts: tuple[tuple[str, int], ...]
    evidence_condition_counts: tuple[tuple[str, int], ...]
    publication_condition_counts: tuple[tuple[str, int], ...]
    coverage: Coverage
    processor_versions: tuple[str, ...]
    derivation_versions: tuple[str, ...]
    complete: bool


class _InvalidPeriod(Exception):
    """Internal refusal, translated only at a public operation's boundary."""


def _failure_boundary(operation: Callable[..., Any]) -> Callable[..., Any]:
    """Expose a named failure without the worker traceback or caller inputs."""
    default_failure = (
        "range-input-invalid" if operation.__name__ == "summarize_range" else "range-period-invalid"
    )
    parameters = signature(operation)

    @wraps(operation)
    def checked(*args: Any, **kwargs: Any) -> Any:
        parameters.bind(*args, **kwargs)
        try:
            return operation(*args, **kwargs)
        except _InvalidPeriod:
            failure = "range-period-invalid"
        except Exception:
            # Includes SummaryContractFailure: dependency failures never escape.
            failure = default_failure
        # The new traceback contains only this frame. Neither the arguments nor
        # the discarded exception (and its worker frames) may remain reachable.
        del args, kwargs
        try:
            raise RangeSummaryFailure(failure)
        except RangeSummaryFailure as exc:
            # The caller may itself be handling an input-bearing exception.
            # Clear context in this handler so even that context cannot escape.
            exc.__context__ = None
            raise

    return checked


def _require_period(condition: bool) -> None:
    if not condition:
        raise _InvalidPeriod()


def _require_input(condition: bool) -> None:
    if not condition:
        raise ValueError()


def _is_date(value: Any) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def _rolling_year_start(today: date) -> date:
    _require_period(today.year > 1)
    day = 28 if (today.month, today.day) == (2, 29) else today.day
    previous = today.replace(year=today.year - 1, day=day)
    return previous + timedelta(days=1)


@_failure_boundary
def preset_period(length: int, today: date) -> RangePeriod:
    """The `length` dates ending at `today` (R4)."""
    _require_period(_is_date(today) and type(length) is int and length in PRESET_LENGTHS)
    first = today - timedelta(days=length - 1)
    return RangePeriod(f"last-{length}-dates", first, today)


@_failure_boundary
def last_year_period(today: date) -> RangePeriod:
    """The rolling year through `today` (R4)."""
    _require_period(_is_date(today))
    return RangePeriod("last-year", _rolling_year_start(today), today)


@_failure_boundary
def calendar_year_period(year: int, today: date) -> RangePeriod:
    """1 January to 31 December of `year`, ending at `today` in the current year (R5)."""
    _require_period(_is_date(today) and type(year) is int and 1 <= year <= today.year)
    return RangePeriod("calendar-year", date(year, 1, 1), min(date(year, 12, 31), today))


@_failure_boundary
def calendar_month_period(year: int, month: int, today: date) -> RangePeriod:
    """The first to the last date of the month, ending at `today` in the current month (R6)."""
    _require_period(_is_date(today) and type(year) is int and type(month) is int)
    first = date(year, month, 1)
    _require_period(first <= today)
    last = date(year, month, monthrange(year, month)[1])
    return RangePeriod("calendar-month", first, min(last, today))


@_failure_boundary
def iso_week_period(iso_year: int, iso_week: int, today: date) -> RangePeriod:
    """Monday to Sunday of the ISO week, ending at `today` in the current week (R6)."""
    _require_period(_is_date(today) and type(iso_year) is int and type(iso_week) is int)
    first = date.fromisocalendar(iso_year, iso_week, 1)
    _require_period(first <= today)
    # Cut the ordinal before constructing a date: 9999-W52's Sunday overflows.
    last = date.fromordinal(min(first.toordinal() + 6, today.toordinal()))
    return RangePeriod("iso-week", first, last)


@_failure_boundary
def custom_period(first_date: date, last_date: date, today: date) -> RangePeriod:
    """The inclusive custom range (R7)."""
    _require_period(_is_date(first_date) and _is_date(last_date) and _is_date(today))
    _require_period(first_date <= last_date <= today)
    _require_period((last_date - first_date).days < MAX_PERIOD_DATES)
    return RangePeriod("custom", first_date, last_date)


def _checked_period_dates(period: RangePeriod) -> tuple[date, ...]:
    """Check that a builder could produce this period with today = last_date."""
    _require_period(isinstance(period, RangePeriod))
    first, last = period.first_date, period.last_date
    _require_period(_is_date(first) and _is_date(last))
    _require_period(isinstance(period.kind, str) and period.kind in PERIOD_KINDS)
    count = (last - first).days + 1
    _require_period(1 <= count <= MAX_PERIOD_DATES)
    if period.kind in ("last-7-dates", "last-30-dates", "last-90-dates"):
        _require_period(count == int(period.kind.split("-")[1]))
    elif period.kind == "last-year":
        _require_period(first == _rolling_year_start(last))
    elif period.kind == "calendar-year":
        _require_period(first == date(last.year, 1, 1))
    elif period.kind == "calendar-month":
        _require_period(first == date(last.year, last.month, 1))
    elif period.kind == "iso-week":
        _require_period(first.weekday() == 0 and count <= 7)
    return tuple(date.fromordinal(ordinal) for ordinal in range(first.toordinal(), last.toordinal() + 1))


@_failure_boundary
def period_dates(period: RangePeriod) -> tuple[date, ...]:
    """Every date of the period, ascending (R8)."""
    return _checked_period_dates(period)


_COVERAGE_FIELDS = ("contributing", "unresolved", "in_conflict", "policy_pending", "failed", "quarantined")


@_failure_boundary
def summarize_range(*, measure: str, record_type: str, period: RangePeriod, days: Iterable[RangeDay]) -> RangeSummary:
    """Check the daily summaries given for the period and build the range result (R9-R13)."""
    dates = _checked_period_dates(period)
    _require_input(isinstance(measure, str) and isinstance(record_type, str) and
                   measure in MEASURES and record_type in MEASURES[measure])
    _require_input(not isinstance(days, (str, bytes)))
    states = dict.fromkeys(PRIMARY_STATES, 0)
    evidence = dict.fromkeys(EVIDENCE_CONDITIONS, 0)
    publication = dict.fromkeys(PUBLICATION_CONDITIONS, 0)
    coverage = [0] * len(_COVERAGE_FIELDS)
    seen_dates: set[date] = set()
    valued_dates: list[date] = []
    processor_versions: set[str] = set()
    derivation_versions: set[str] = set()
    previous_date: date | None = None

    # Consume days once. All facts come from headers; never compare or hash a
    # whole HealthSummary, which would also use its opaque byte fields.
    for day in days:
        _require_input(isinstance(day, RangeDay) and isinstance(day.summary, HealthSummary))
        header = day.summary.header
        _require_input(header.contract_version == _summary_contract.CONTRACT_VERSION and
                       header.measure == measure and header.record_type == record_type)
        local_date = header.local_date
        _require_input(_is_date(local_date) and period.first_date <= local_date <= period.last_date)
        _require_input(previous_date is None or previous_date < local_date)
        conditions = _summary_contract.check_publication_conditions(day.publication_conditions)
        previous_date = local_date
        seen_dates.add(local_date)
        states[header.primary_state] += 1
        if header.primary_state in ("measured", "partial"):
            valued_dates.append(local_date)
        for name in header.evidence_conditions:
            evidence[name] += 1
        for name in conditions:
            publication[name] += 1
        for index, field in enumerate(_COVERAGE_FIELDS):
            coverage[index] += getattr(header.coverage, field)
        processor_versions.add(header.processor_version)
        derivation_versions.add(header.derivation_version)

    absent_dates = tuple(local_date for local_date in dates if local_date not in seen_dates)
    return RangeSummary(
        contract_version=CONTRACT_VERSION,
        measure=measure,
        record_type=record_type,
        period=period,
        date_count=len(dates),
        valued_dates=tuple(valued_dates),
        absent_dates=absent_dates,
        primary_state_counts=tuple(states.items()),
        evidence_condition_counts=tuple(evidence.items()),
        publication_condition_counts=tuple(publication.items()),
        coverage=Coverage(*coverage),
        processor_versions=tuple(sorted(processor_versions)),
        derivation_versions=tuple(sorted(derivation_versions)),
        complete=not absent_dates and states["measured"] == len(dates) and not any(publication.values()),
    )
