"""range-summary-contract@1: the shared range result every range composition builds on.

It defines the inclusive local-date periods of the presets, the rolling year, calendar years, calendar months,
ISO weeks, and custom ranges; checks the daily Health Summaries given for a period; and counts dates by primary
state, evidence condition, and publication condition, with summed coverage and a `complete` flag
(packs/range-summary-contract-1.spec.md). It never reads a summary body, a clock, a store, or a file.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

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


def preset_period(length: int, today: date) -> RangePeriod:
    """The `length` dates ending at `today` (R4)."""
    raise NotImplementedError("range-summary-contract@1")


def last_year_period(today: date) -> RangePeriod:
    """The rolling year through `today` (R4)."""
    raise NotImplementedError("range-summary-contract@1")


def calendar_year_period(year: int, today: date) -> RangePeriod:
    """1 January to 31 December of `year`, ending at `today` in the current year (R5)."""
    raise NotImplementedError("range-summary-contract@1")


def calendar_month_period(year: int, month: int, today: date) -> RangePeriod:
    """The first to the last date of the month, ending at `today` in the current month (R6)."""
    raise NotImplementedError("range-summary-contract@1")


def iso_week_period(iso_year: int, iso_week: int, today: date) -> RangePeriod:
    """Monday to Sunday of the ISO week, ending at `today` in the current week (R6)."""
    raise NotImplementedError("range-summary-contract@1")


def custom_period(first_date: date, last_date: date, today: date) -> RangePeriod:
    """The inclusive custom range (R7)."""
    raise NotImplementedError("range-summary-contract@1")


def period_dates(period: RangePeriod) -> tuple[date, ...]:
    """Every date of the period, ascending (R8)."""
    raise NotImplementedError("range-summary-contract@1")


def summarize_range(*, measure: str, record_type: str, period: RangePeriod, days: Iterable[RangeDay]) -> RangeSummary:
    """Check the daily summaries given for the period and build the range result (R9-R13)."""
    raise NotImplementedError("range-summary-contract@1")
