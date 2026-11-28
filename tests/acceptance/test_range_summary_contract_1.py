"""Acceptance tests for `range-summary-contract@1`.

Requirement numbers refer to `packs/range-summary-contract-1.spec.md`. Every input is synthetic. Daily summaries
are real Health Summaries built with the published `summary_contract.build_summary` from synthetic evidence and
bodies, and each fixture checks that summary-contract@1 gave it the state and conditions it asked for. Expected
periods come from a calendar model in this file that uses integer day numbers, never `datetime` arithmetic.
Expected range results come from the fixtures' own facts, never from the headers. Markers in bodies, versions,
and dates show that failures carry no input value.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import os
import random
import re
import subprocess
import sys
import traceback
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from activity_tracker_portal import range_summary_contract, summary_contract
from activity_tracker_portal.range_summary_contract import (
    CONTRACT_VERSION,
    EVIDENCE_CONDITIONS,
    FAILURES,
    MAX_PERIOD_DATES,
    MEASURES,
    PERIOD_KINDS,
    PRESET_LENGTHS,
    PRIMARY_STATES,
    PUBLICATION_CONDITIONS,
    RangeDay,
    RangePeriod,
    RangeSummary,
    RangeSummaryFailure,
    calendar_month_period,
    calendar_year_period,
    custom_period,
    iso_week_period,
    last_year_period,
    period_dates,
    preset_period,
    summarize_range,
)
from activity_tracker_portal.summary_contract import (
    Coverage,
    DeliveryPosition,
    EvidenceEntry,
    HealthSummary,
    SummaryContractFailure,
    build_summary,
)

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PACKAGE_DIR = str(Path(range_summary_contract.__file__).resolve().parent)
CONTRACT = json.loads((ROOT / "governance/scopes/range-summary-contract.1.json").read_text(encoding="utf-8"))
EFFECTS = json.loads((ROOT / "packs/range-summary-contract-1.effects.json").read_text(encoding="utf-8"))
SCOPE_MAP = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")

UTC = timezone.utc
V1 = "range-summary-contract@1"
KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
PERIOD_INVALID = "range-period-invalid"
INPUT_INVALID = "range-input-invalid"
TODAY = date(2026, 11, 28)
LAST_DATE = date(9999, 12, 31)
SEED = 20261128

HEALTH_MARKER = "synthetic-health-marker"
MARKER_VALUE = 7314159
MARKER_DATE = date(1987, 6, 5)
MARKER_MOMENT = datetime(1987, 6, 5, 3, 4)
MARKER_TEXTS = (HEALTH_MARKER, str(MARKER_VALUE), "1987-06-05", "1987, 6, 5")
MARKER_DERIVATION = f"{HEALTH_MARKER}@{MARKER_VALUE}"

DERIVATION = "derive-synthetic-steps@1"
FALLBACK = 'fallback-timezone:[["policy-a"],"2025b"]'
RECEIPT = datetime(2026, 1, 15, 22, 0, 0, 123456, tzinfo=UTC)

# The spec's vocabularies, written out independently of both modules.
STATE_NAMES = ("measured", "partial", "missing", "failed", "quarantined", "policy-pending", "unresolved")
EVIDENCE_NAMES = ("classification-conflict", "duplicate-grouped", "equal-time-conflict", "inferred-attribution",
                  "selection-unresolved", "source-conflict", "unallocatable-interval")
UNRESOLVED_REPORTED = frozenset({"classification-conflict", "equal-time-conflict", "selection-unresolved",
                                 "unallocatable-interval"})
COMPUTED = frozenset({"inferred-attribution", "source-conflict"})
PUBLICATION_NAMES = ("conflicted", "newer-evidence-pending", "reconciliation-failed", "reconciliation-pending",
                     "refresh-failed", "superseded")
STANDING_NAMES = ("contributing", "unresolved", "in-conflict", "policy-pending", "failed", "quarantined")
MEASURE_PAIRS = (("daily-steps", "steps"), ("heart-rate-summary", "heart-rate"), ("sleep-summary", "sleep-session"),
                 ("weight-series", "weight"))
PROCESSOR_OF = {"steps": "interpret-steps@1", "heart-rate": "interpret-heart-rate@1",
                "sleep-session": "interpret-sleep-session@1", "weight": "interpret-weight@1"}
KIND_OF_LENGTH = {7: "last-7-dates", 30: "last-30-dates", 90: "last-90-dates"}
RESULT_FIELDS = ["contract_version", "measure", "record_type", "period", "date_count", "valued_dates", "absent_dates",
                 "primary_state_counts", "evidence_condition_counts", "publication_condition_counts", "coverage",
                 "processor_versions", "derivation_versions", "complete"]
# Years around leap, century, and quadricentennial boundaries, and both ends of the date range.
YEARS = (1, 2, 3, 4, 5, 99, 100, 101, 399, 400, 401, 1599, 1600, 1601, 1899, 1900, 1901, 1903, 1904, 1905, 1999,
         2000, 2001, 2004, 2015, 2020, 2024, 2025, 2026, 2027, 2028, 2029, 2030, 2032, 2099, 2100, 2101, 2399, 2400,
         2401, 9998, 9999)


# ---- Independent calendar model ----
#
# Proleptic Gregorian dates as day numbers, with 0001-01-01 (a Monday) as day 0. Only `date(year, month, day)` is
# used, to turn a model result into a value the unit's result can be compared with.

MONTH_LENGTHS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def month_length(year: int, month: int) -> int:
    return 29 if month == 2 and leap(year) else MONTH_LENGTHS[month - 1]


def day_number(year: int, month: int, day: int) -> int:
    past = year - 1
    return (past * 365 + past // 4 - past // 100 + past // 400
            + sum(month_length(year, m) for m in range(1, month)) + day - 1)


def civil(number: int) -> tuple[int, int, int]:
    year = number // 366 + 1
    while day_number(year + 1, 1, 1) <= number:
        year += 1
    rest = number - day_number(year, 1, 1)
    month = 1
    while rest >= month_length(year, month):
        rest -= month_length(year, month)
        month += 1
    return year, month, rest + 1


def as_date(number: int) -> date:
    return date(*civil(number))


def number_of(day: date) -> int:
    return day_number(day.year, day.month, day.day)


def iso_weeks_in(iso_year: int) -> int:
    weekday = day_number(iso_year, 1, 1) % 7  # 0 is Monday
    return 53 if weekday == 3 or (leap(iso_year) and weekday == 2) else 52


def iso_monday(iso_year: int, week: int) -> int:
    jan4 = day_number(iso_year, 1, 4)
    return jan4 - jan4 % 7 + 7 * (week - 1)


def iso_week_of(number: int) -> tuple[int, int]:
    year = civil(number)[0]
    for iso_year in (year + 1, year, year - 1):
        first = iso_monday(iso_year, 1)
        if number >= first:
            return iso_year, (number - first) // 7 + 1
    raise AssertionError("unreachable")


def model_preset(length: int, today: date):
    first = number_of(today) - (length - 1)
    if first < 0:
        return PERIOD_INVALID
    return KIND_OF_LENGTH[length], as_date(first), today


def model_last_year(today: date):
    year, month, day = today.year, today.month, today.day
    if year == 1:
        return PERIOD_INVALID
    if (month, day) == (2, 29):
        day = 28
    return "last-year", as_date(day_number(year - 1, month, day) + 1), today


def model_calendar_year(year: int, today: date):
    if not 1 <= year <= 9999 or year > today.year:
        return PERIOD_INVALID
    return "calendar-year", date(year, 1, 1), today if year == today.year else date(year, 12, 31)


def model_month(year: int, month: int, today: date):
    if not 1 <= year <= 9999 or not 1 <= month <= 12:
        return PERIOD_INVALID
    first = day_number(year, month, 1)
    if first > number_of(today):
        return PERIOD_INVALID
    last = min(day_number(year, month, month_length(year, month)), number_of(today))
    return "calendar-month", as_date(first), as_date(last)


def model_iso_week(iso_year: int, week: int, today: date):
    if not 1 <= iso_year <= 9999 or not 1 <= week <= iso_weeks_in(iso_year):
        return PERIOD_INVALID
    monday = iso_monday(iso_year, week)
    if monday < 0 or monday > number_of(today):
        return PERIOD_INVALID
    return "iso-week", as_date(monday), as_date(min(monday + 6, number_of(today)))


def model_custom(first: date, last: date, today: date):
    a, b = number_of(first), number_of(last)
    if a > b or b > number_of(today) or b - a + 1 > 366:
        return PERIOD_INVALID
    return "custom", first, last


def model_dates(first: date, last: date) -> tuple[date, ...]:
    out = []
    year, month, day = first.year, first.month, first.day
    for _ in range(number_of(last) - number_of(first) + 1):
        out.append(date(year, month, day))
        day += 1
        if day > month_length(year, month):
            day, month = 1, month + 1
            if month > 12:
                month, year = 1, year + 1
    return tuple(out)


def contains_leap_day(first: date, last: date) -> bool:
    a, b = number_of(first), number_of(last)
    return any(leap(year) and a <= day_number(year, 2, 29) <= b for year in range(first.year, last.year + 1))


def model_consistent(kind, first: date, last: date) -> bool:
    """Whether some builder, for some today, gives this period (R8)."""
    a, b = number_of(first), number_of(last)
    if a > b or b - a + 1 > 366:
        return False
    if kind in ("last-7-dates", "last-30-dates", "last-90-dates"):
        return b - a + 1 == {v: k for k, v in KIND_OF_LENGTH.items()}[kind]
    if kind == "last-year":
        return model_last_year(last) == ("last-year", first, last)
    if kind == "calendar-year":
        return (first.month, first.day) == (1, 1) and last.year == first.year
    if kind == "calendar-month":
        return first.day == 1 and (last.year, last.month) == (first.year, first.month)
    if kind == "iso-week":
        return a % 7 == 0 and b - a <= 6
    return kind == "custom"


def generated_todays(count: int, seed: int = SEED) -> list[date]:
    """Edge dates of every boundary year, then seeded dates biased towards those years."""
    rng = random.Random(seed)
    out = []
    for year in YEARS:
        edges = [(1, 1), (1, 2), (1, 4), (2, 28), (3, 1), (12, 28), (12, 31)] + ([(2, 29)] if leap(year) else [])
        out.extend(date(year, m, d) for m, d in edges)
    for _ in range(count):
        year = rng.choice(YEARS) if rng.random() < 0.6 else rng.randint(1, 9999)
        month = rng.randint(1, 12)
        out.append(date(year, month, rng.randint(1, month_length(year, month))))
    return out


def outcome(call, *args):
    """A period as (kind, first, last), or the failure name. Anything else propagates."""
    try:
        period = call(*args)
    except RangeSummaryFailure as failure:
        return failure.args[0] if len(failure.args) == 1 else ("failure-args", len(failure.args))
    if type(period) is not RangePeriod or type(period.first_date) is not date or type(period.last_date) is not date:
        return "not-a-period-of-dates"
    return period.kind, period.first_date, period.last_date


def mismatches(cases) -> list:
    out = []
    for call, args, expected in cases:
        got = outcome(call, *args)
        if got != expected:
            out.append((call.__name__, args, got, expected))
    return out


# ---- Synthetic daily summaries ----

# Which of outcome_id, processor_version, policy_identity each standing carries (summary-contract@1 R5).
CARRIES = {
    "contributing": (True, True, True),
    "unresolved": (True, True, True),
    "in-conflict": (False, False, False),
    "policy-pending": (True, True, True),
    "failed": (False, True, False),
    "quarantined": (True, True, True),
}
STATE_EVIDENCE = {
    "measured": ("contributing", "contributing"),
    "partial": ("contributing", "contributing", "unresolved"),
    "missing": (),
    "failed": ("failed",),
    "quarantined": ("quarantined",),
    "policy-pending": ("policy-pending",),
    "unresolved": ("unresolved",),
}
NO_VALUE_CONDITIONS = UNRESOLVED_REPORTED | {"source-conflict"}
# The evidence conditions each state can carry with this file's evidence.
ALLOWED_CONDITIONS = {
    "measured": frozenset({"duplicate-grouped", "inferred-attribution"}),
    "partial": frozenset(EVIDENCE_NAMES),
    "missing": frozenset(),
    "failed": NO_VALUE_CONDITIONS,
    "quarantined": NO_VALUE_CONDITIONS,
    "policy-pending": NO_VALUE_CONDITIONS,
    "unresolved": NO_VALUE_CONDITIONS,
}


@dataclasses.dataclass(frozen=True)
class DaySpec:
    """The facts of one synthetic daily summary, and the publication conditions the caller attaches to it."""

    local_date: date
    state: str = "measured"
    conditions: frozenset = frozenset()
    publication: tuple = ()
    extra: tuple = ()
    measure: str = "daily-steps"
    record_type: str = "steps"
    processor: str | None = None
    derivation: str = DERIVATION
    first_id: int = 1
    body_total: int = 1


def standings_of(spec: DaySpec) -> list[str]:
    standings = list(STATE_EVIDENCE[spec.state]) + list(spec.extra)
    if spec.conditions & UNRESOLVED_REPORTED and "unresolved" not in standings:
        standings.append("unresolved")
    if "source-conflict" in spec.conditions:
        standings.append("in-conflict")
    return standings


def processor_of(spec: DaySpec) -> str:
    return spec.processor or PROCESSOR_OF[spec.record_type]


def body_of(value: dict) -> bytes:
    """RFC 8785 for flat objects of ASCII keys, strings, and small integers."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@functools.lru_cache(maxsize=None)
def build_day_summary(spec: DaySpec) -> HealthSummary:
    processor = processor_of(spec)
    entries = []
    inferred = "inferred-attribution" in spec.conditions
    for index, standing in enumerate(standings_of(spec)):
        has_outcome, has_processor, has_policy = CARRIES[standing]
        policy = FALLBACK if inferred and standing == "contributing" and not entries else "none"
        record_id = spec.first_id + index
        entries.append(EvidenceEntry(record_id, record_id + 500, record_id + 9000 if has_outcome else None, standing,
                                     processor if has_processor else None, policy if has_policy else None))
    has_value = spec.state in ("measured", "partial")
    summary = build_summary(
        measure=spec.measure, record_type=spec.record_type, local_date=spec.local_date,
        derivation_version=spec.derivation, processor_version=processor, source_selection_policy=None,
        evidence=tuple(entries), reported_conditions=tuple(sorted(spec.conditions - COMPUTED)),
        positions=(DeliveryPosition("installation-a", 41, RECEIPT, RECEIPT + timedelta(seconds=3)),),
        body=body_of({"marker": HEALTH_MARKER, "total": spec.body_total}) if has_value else None,
    )
    # The fixture checks itself against the published unit before any range test relies on it.
    if summary.header.primary_state != spec.state or set(summary.header.evidence_conditions) != set(spec.conditions):
        raise AssertionError(f"fixture gave {summary.header.primary_state} {summary.header.evidence_conditions}")
    return summary


_SPEC_PUBLICATION = object()


def make_day(spec: DaySpec, publication=_SPEC_PUBLICATION) -> RangeDay:
    return RangeDay(build_day_summary(spec), spec.publication if publication is _SPEC_PUBLICATION else publication)


def coverage_counts(spec: DaySpec) -> dict[str, int]:
    standings = standings_of(spec)
    return {standing: standings.count(standing) for standing in STANDING_NAMES}


def expected_summary(period: RangePeriod, specs, measure: str = "daily-steps",
                     record_type: str = "steps") -> RangeSummary:
    """R10-R13 over the fixtures' own facts."""
    dates = model_dates(period.first_date, period.last_date)
    given = {spec.local_date for spec in specs}
    totals = {standing: sum(coverage_counts(spec)[standing] for spec in specs) for standing in STANDING_NAMES}
    absent = tuple(day for day in dates if day not in given)
    return RangeSummary(
        contract_version=V1, measure=measure, record_type=record_type, period=period, date_count=len(dates),
        valued_dates=tuple(sorted(spec.local_date for spec in specs if spec.state in ("measured", "partial"))),
        absent_dates=absent,
        primary_state_counts=tuple((name, sum(1 for spec in specs if spec.state == name)) for name in STATE_NAMES),
        evidence_condition_counts=tuple((name, sum(1 for spec in specs if name in spec.conditions))
                                        for name in EVIDENCE_NAMES),
        publication_condition_counts=tuple((name, sum(1 for spec in specs if name in set(spec.publication)))
                                           for name in PUBLICATION_NAMES),
        coverage=Coverage(*(totals[standing] for standing in STANDING_NAMES)),
        processor_versions=tuple(sorted({processor_of(spec) for spec in specs})),
        derivation_versions=tuple(sorted({spec.derivation for spec in specs})),
        complete=not absent and all(spec.state == "measured" and not spec.publication for spec in specs),
    )


def random_specs(rng: random.Random, dates, density: float = 0.7) -> list[DaySpec]:
    specs = []
    for index, day in enumerate(dates):
        if rng.random() > density:
            continue
        state = rng.choice(STATE_NAMES)
        conditions = frozenset(name for name in sorted(ALLOWED_CONDITIONS[state]) if rng.random() < 0.3)
        specs.append(DaySpec(
            day, state, conditions, tuple(rng.sample(PUBLICATION_NAMES, rng.choice((0, 0, 0, 1, 2)))),
            processor=rng.choice(("interpret-steps@1", "interpret-steps@2", "interpret-steps@10")),
            derivation=rng.choice((DERIVATION, "derive-synthetic-steps@2")), first_id=1 + 10 * index,
            body_total=rng.randint(0, 50000),
        ))
    return specs


def custom(first: date, last: date) -> RangePeriod:
    return RangePeriod("custom", first, last)


def week_of_days(first: date = date(2026, 11, 22)) -> list[date]:
    return [first + timedelta(days=offset) for offset in range(7)]


def summarize(period: RangePeriod, days, measure: str = "daily-steps", record_type: str = "steps") -> RangeSummary:
    return summarize_range(measure=measure, record_type=record_type, period=period, days=days)


class Untouchable:
    """Stands in for a summary's bytes. Any use is recorded, and fails."""

    uses: list[str] = []

    def _use(self, *args, **kwargs):
        Untouchable.uses.append("used")
        raise AssertionError("summary bytes were used")

    __eq__ = __ne__ = __lt__ = __le__ = __gt__ = __ge__ = __hash__ = _use
    __len__ = __iter__ = __bool__ = __bytes__ = __getitem__ = __contains__ = __index__ = _use
    __str__ = __format__ = __buffer__ = __add__ = __radd__ = __getattr__ = _use
    __reduce__ = __reduce_ex__ = __copy__ = __deepcopy__ = _use

    def __repr__(self) -> str:
        return "<untouchable>"


# ---- Scenario shared by the determinism and purity tests ----

def scenario_inputs() -> dict:
    specs = [
        DaySpec(date(2026, 10, 30)),
        DaySpec(date(2026, 11, 1), "partial", frozenset({"source-conflict", "selection-unresolved"}), ("refresh-failed",)),
        DaySpec(date(2026, 11, 2), "missing"),
        DaySpec(date(2026, 11, 5), "failed", extra=("quarantined",)),
        DaySpec(date(2026, 11, 9), "quarantined", publication=("conflicted", "superseded")),
        DaySpec(date(2026, 11, 10), "policy-pending"),
        DaySpec(date(2026, 11, 20), "unresolved", frozenset({"equal-time-conflict"})),
        DaySpec(date(2026, 11, 28), "measured", frozenset({"duplicate-grouped", "inferred-attribution"}),
                processor="interpret-steps@2", derivation="derive-synthetic-steps@2"),
    ]
    return {"period": RangePeriod("last-30-dates", date(2026, 10, 30), TODAY),
            "days": tuple(make_day(spec) for spec in specs),
            "week": tuple(make_day(DaySpec(day)) for day in week_of_days())}


def scenario_text(inputs: dict) -> str:
    """Every operation over fixed synthetic inputs, rendered as text for comparison across processes."""
    out = []
    for call in (lambda: preset_period(7, TODAY), lambda: preset_period(90, date(2028, 3, 1)),
                 lambda: last_year_period(TODAY), lambda: last_year_period(date(2028, 2, 29)),
                 lambda: calendar_year_period(2028, TODAY.replace(year=2029)), lambda: calendar_year_period(2026, TODAY),
                 lambda: calendar_month_period(2028, 2, TODAY.replace(year=2029)),
                 lambda: iso_week_period(2026, 53, date(2027, 1, 1)), lambda: custom_period(date(2026, 1, 1), TODAY, TODAY)):
        period = call()
        out.append([repr(period), [day.isoformat() for day in period_dates(period)]])
    out.append(repr(summarize(inputs["period"], inputs["days"])))
    out.append(repr(summarize(inputs["period"], iter(inputs["days"][2:5]))))
    out.append(repr(summarize(RangePeriod("iso-week", date(2026, 11, 16), date(2026, 11, 22)), ())))
    out.append(repr(summarize(custom(date(2026, 11, 22), TODAY), list(inputs["week"]))))
    bad_calls = [
        lambda: preset_period(8, TODAY),
        lambda: last_year_period(date(1, 6, 1)),
        lambda: iso_week_period(2027, 53, TODAY.replace(year=2028)),
        lambda: custom_period(date(2025, 11, 27), TODAY, TODAY),
        lambda: period_dates(RangePeriod("calendar-month", date(2026, 11, 2), TODAY)),
        lambda: summarize(inputs["period"], tuple(reversed(inputs["days"]))),
        lambda: summarize(inputs["period"], [RangeDay(inputs["days"][0].summary, ("source-conflict",))]),
        lambda: summarize(inputs["period"], (), measure="daily-steps", record_type="heart-rate"),
    ]
    for call in bad_calls:
        try:
            call()
        except RangeSummaryFailure as failure:
            out.append(list(failure.args))
    return json.dumps(out)


def run_python(code: str, env_updates: dict[str, str]) -> tuple[str, str]:
    env = dict(os.environ)
    env.update(env_updates)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(HERE), env.get("PYTHONPATH", "")]))
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(ROOT),
                          timeout=300)
    if done.returncode != 0:
        raise AssertionError(f"subprocess failed ({done.returncode}):\n{done.stderr[-3000:]}")
    return done.stdout, done.stderr


def carries_marker(value, depth: int = 0) -> bool:
    """Whether a value, or anything nested in its containers or dataclass fields, is a marker input."""
    if depth > 8:
        return False
    if isinstance(value, str):
        return any(text in value for text in MARKER_TEXTS)
    if isinstance(value, (bytes, bytearray)):
        return any(text.encode() in value for text in MARKER_TEXTS)
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == MARKER_VALUE
    if isinstance(value, date):
        return (value.year, value.month, value.day) == (1987, 6, 5)
    if isinstance(value, dict):
        return any(carries_marker(k, depth + 1) or carries_marker(v, depth + 1) for k, v in value.items())
    if isinstance(value, (tuple, list, set, frozenset)):
        return any(carries_marker(item, depth + 1) for item in value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return any(carries_marker(getattr(value, item.name), depth + 1) for item in dataclasses.fields(value))
    return False


class Checks(unittest.TestCase):
    maxDiff = None

    def refused(self, call, *args, **kwargs) -> str:
        with self.assertRaises(RangeSummaryFailure) as caught:
            call(*args, **kwargs)
        failure = caught.exception
        self.assertIs(type(failure), RangeSummaryFailure)
        self.assertEqual(len(failure.args), 1)
        self.assertIn(failure.args[0], FAILURES)
        return failure.args[0]

    def period_refused(self, call, *args) -> None:
        self.assertEqual(self.refused(call, *args), PERIOD_INVALID, (call.__name__, args))

    def input_refused(self, period: RangePeriod, days, measure: str = "daily-steps", record_type: str = "steps") -> None:
        self.assertEqual(self.refused(summarize_range, measure=measure, record_type=record_type, period=period,
                                      days=days), INPUT_INVALID)

    def assert_period(self, period, kind: str, first: date, last: date, count: int | None = None) -> None:
        self.assertIs(type(period), RangePeriod)
        self.assertEqual((period.kind, period.first_date, period.last_date), (kind, first, last))
        self.assertIs(type(period.first_date), date)
        self.assertIs(type(period.last_date), date)
        dates = period_dates(period)
        self.assertEqual(dates, model_dates(first, last))
        if count is not None:
            self.assertEqual(len(dates), count)

    def assert_cases(self, cases, minimum: int) -> None:
        self.assertGreaterEqual(len(cases), minimum)
        self.assertEqual(mismatches(cases)[:5], [])

    def assert_result_types(self, result: RangeSummary) -> None:
        self.assertIs(type(result), RangeSummary)
        self.assertIs(type(result.date_count), int)
        self.assertIs(type(result.complete), bool)
        for name in ("valued_dates", "absent_dates", "primary_state_counts", "evidence_condition_counts",
                     "publication_condition_counts", "processor_versions", "derivation_versions"):
            self.assertIs(type(getattr(result, name)), tuple, name)
        for day in result.valued_dates + result.absent_dates:
            self.assertIs(type(day), date)
        for pairs in (result.primary_state_counts, result.evidence_condition_counts,
                      result.publication_condition_counts):
            for pair in pairs:
                self.assertIs(type(pair), tuple)
                self.assertIs(type(pair[1]), int)
        self.assertIs(type(result.coverage), Coverage)
        for item in dataclasses.fields(Coverage):
            self.assertIs(type(getattr(result.coverage, item.name)), int)

    def summarized(self, period: RangePeriod, specs, measure: str = "daily-steps", record_type: str = "steps",
                   days=None) -> RangeSummary:
        """Summarize the fixtures' days and compare every field with the model."""
        result = summarize(period, [make_day(spec) for spec in specs] if days is None else days, measure, record_type)
        self.assert_result_types(result)
        self.assertEqual(result, expected_summary(period, specs, measure, record_type))
        return result


# ---- R1 ----

class R1GovernedScopeContract(unittest.TestCase):
    def test_contract_shape_and_dependencies(self) -> None:
        self.assertEqual((CONTRACT["contract_schema"], CONTRACT["scope"], CONTRACT["version"], CONTRACT["kind"]),
                         (1, "range-summary-contract", "1", "shared"))
        self.assertEqual(CONTRACT["dependencies"], [{"scope": "summary-contract", "version": "1"}])
        self.assertEqual([(item["name"], item["outcome"]) for item in CONTRACT["named_failures"]], [
            ("range-period-invalid", "refusal"),
            ("range-input-invalid", "invariant-refusal"),
        ])
        self.assertEqual(CONTRACT["observable_effects"], [])
        self.assertEqual({item["mode"] for item in CONTRACT["inputs"]}, {"pure"})
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["specification"], "packs/range-summary-contract-1.spec.md")
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_range_summary_contract_1.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_range_summary_contract_1.sh")
        self.assertEqual(oracle["languages"], ["python"])

    def test_effects_declaration_is_empty(self) -> None:
        self.assertEqual(EFFECTS, {"effects_format": 1, "effects": []})

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        row = next(line for line in SCOPE_MAP.splitlines() if line.startswith("| `range-summary-contract` |"))
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        self.assertEqual(cells[1], "shared")
        self.assertEqual(set(re.findall(r"`([a-z0-9-]+)`", cells[-1])),
                         {item["scope"] for item in CONTRACT["dependencies"]})
        for length in ("7", "30", "90"):
            self.assertRegex(cells[4], rf"(?<![0-9]){length}(?![0-9])")
        # Decision 4: the one-year preset is the rolling year, of 365 or 366 dates.
        self.assertIn("rolling year", cells[4])
        self.assertIn("365 or 366", cells[4])


# ---- R2 ----

class R2PureAndDeterministic(Checks):
    def test_same_input_gives_equal_results(self) -> None:
        for call, args in ((preset_period, (30, TODAY)), (last_year_period, (date(2028, 2, 29),)),
                           (calendar_year_period, (2026, TODAY)), (calendar_month_period, (2028, 2, LAST_DATE)),
                           (iso_week_period, (2026, 53, LAST_DATE)), (custom_period, (date(2026, 1, 1), TODAY, TODAY))):
            with self.subTest(call=call.__name__):
                first, second = call(*args), call(*args)
                self.assertEqual(first, second)
                self.assertEqual(period_dates(first), period_dates(second))
        inputs = scenario_inputs()
        period = inputs["period"]
        days = list(inputs["days"])
        kept = list(days)
        first = summarize(period, days)
        self.assertEqual(days, kept)
        # Equal days in other containers, other objects, and other condition collections give equal results.
        rebuilt = [RangeDay(dataclasses.replace(day.summary), list(day.publication_conditions) * 2) for day in days]
        variants = [tuple(days), (day for day in days), iter(days), rebuilt,
                    [RangeDay(day.summary, frozenset(day.publication_conditions)) for day in days],
                    [RangeDay(day.summary, set(day.publication_conditions)) for day in days]]
        for variant in variants:
            with self.subTest(variant=type(variant).__name__):
                self.assertEqual(summarize(period, variant), first)
        self.assertEqual(scenario_text(inputs), scenario_text(scenario_inputs()))

    def test_result_ignores_host_timezone_locale_and_hash_seed(self) -> None:
        expected = scenario_text(scenario_inputs())
        code = "import test_range_summary_contract_1 as t; print(t.scenario_text(t.scenario_inputs()))"
        for settings in (
            {"TZ": "America/Los_Angeles", "LC_ALL": "C", "LANG": "C", "PYTHONHASHSEED": "1"},
            {"TZ": "Asia/Kolkata", "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8", "PYTHONHASHSEED": "2"},
            {"TZ": "Pacific/Kiritimati", "LC_ALL": "de_DE.UTF-8", "LANG": "de_DE.UTF-8", "PYTHONHASHSEED": "3"},
        ):
            with self.subTest(**settings):
                stdout, _ = run_python(code, settings)
                self.assertEqual(stdout.strip().splitlines()[-1], expected)

    def test_operations_read_no_clock_or_io(self) -> None:
        scenario_text(scenario_inputs())
        code = r"""
import json, os, sys, time, logging, zoneinfo
# Modules an implementation commonly needs are imported first, then the published summary-contract@1, which may
# import operational_store through set_source_selection_policy.
import hashlib, re, decimal, fractions, dataclasses, typing, collections, functools, itertools, math, unicodedata
import copy, enum, struct, base64, datetime, string, operator, bisect, heapq, io, traceback, types, inspect
import contextlib, pathlib, sqlite3, shutil, importlib.resources, collections.abc, numbers, codecs, unittest
import locale, calendar, _strptime, json.decoder, json.encoder, binascii, textwrap, weakref, array, ipaddress
import random
import activity_tracker_portal.summary_contract
# During the operations, the only file-system activity allowed is the import system loading a module: an implementation
# may import any standard-library or published module lazily. An open, metadata read, or listing is allowed only while
# the import system is importing a module; anything else is flagged, on every call, with no warm-up and no path
# exemption.
# A module is being imported only when the import system's own import routine is on the stack. Loader helper methods
# such as FileLoader.get_data, reached through pkgutil.get_data or importlib.resources, are not imports.
def loading():
    frame = sys._getframe(1)
    while frame is not None:
        code = frame.f_code
        if code.co_filename == "<frozen importlib._bootstrap>" and code.co_name in ("_find_and_load", "_load"):
            return True
        frame = frame.f_back
    return False
busy = [False]
def real(path):
    # realpath itself calls os.lstat, so the guards below pass through while it runs.
    busy[0] = True
    try:
        return os.path.realpath(path)
    finally:
        busy[0] = False
host = [os.path.realpath(p) for p in zoneinfo.TZPATH] + [os.path.realpath("/etc/localtime"), "/etc/localtime"]
events = []
phase = ["import"]
WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
def hook(event, args):
    if phase[0] == "done" or busy[0]:
        return
    if event == "open":
        path, mode, flags = args
        shown = os.fsdecode(path) if isinstance(path, (str, bytes)) else repr(path)
        if phase[0] == "calls" and not loading():
            events.append(["open", shown])
        if isinstance(path, (str, bytes)):
            if ((mode and any(c in mode for c in "wax+")) or (flags or 0) & WRITE) \
                    and not (loading() and ".pyc." in os.path.basename(shown) + "."):
                # The import system may write a module's bytecode cache; nothing else may write.
                events.append(["write", shown])
            resolved = real(shown)
            if any(resolved == h or resolved.startswith(h.rstrip("/") + "/") for h in host):
                events.append(["host-timezone", shown])
    elif event.split(".")[0] in ("socket", "sqlite3", "subprocess", "ctypes", "webbrowser", "urllib", "http") or event in (
            "os.system", "os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.chmod", "os.utime", "os.truncate",
            "os.symlink", "os.link", "os.posix_spawn", "os.exec", "os.fork", "shutil.copyfile", "shutil.rmtree"):
        events.append([event])
    elif phase[0] == "calls" and event in ("os.listdir", "os.scandir", "glob.glob", "os.chdir", "io.open_code") \
            and not loading():
        events.append([event])
def metadata(original, name):
    # CPython raises no audit event for stat-family calls, so they are guarded directly.
    def guard(target, *args, **kwargs):
        if phase[0] == "calls" and not busy[0] and not loading():
            events.append([name, os.fsdecode(target) if isinstance(target, (str, bytes, os.PathLike)) else repr(target)])
        return original(target, *args, **kwargs)
    return guard
for name in ("stat", "lstat", "fstat", "access"):
    setattr(os, name, metadata(getattr(os, name), "os." + name))
def forbidden(*args, **kwargs):
    raise AssertionError("clock read")
def explicit(original, index):
    def guard(*args):
        if len(args) <= index or args[index] is None:
            raise AssertionError("clock read")
        return original(*args)
    return guard
class Environ(collections.abc.MutableMapping):
    def __init__(self, inner):
        self.inner, self.reads = inner, []
    def note(self, key):
        # A module's own top-level code may read the environment while it is being imported.
        if not loading():
            self.reads.append(str(key)[:40])
    def __getitem__(self, key):
        self.note(key); return self.inner[key]
    def __setitem__(self, key, value):
        self.note(key); self.inner[key] = value
    def __delitem__(self, key):
        self.note(key); del self.inner[key]
    def __iter__(self):
        self.note("*"); return iter(self.inner)
    def __len__(self):
        self.note("*"); return len(self.inner)
    def __contains__(self, key):
        self.note(key); return key in self.inner
records = []
class Handler(logging.Handler):
    def emit(self, record):
        records.append(record.name)
logging.getLogger().addHandler(Handler())
logging.getLogger().setLevel(logging.DEBUG)
sys.addaudithook(hook)
for name in ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns", "process_time",
             "process_time_ns", "thread_time", "thread_time_ns", "clock_gettime", "clock_gettime_ns",
             "localtime", "ctime", "mktime"):
    if hasattr(time, name):
        setattr(time, name, forbidden)
time.gmtime = explicit(time.gmtime, 0)
time.asctime = explicit(time.asctime, 0)
time.strftime = explicit(time.strftime, 1)
import activity_tracker_portal.range_summary_contract as module
import test_range_summary_contract_1 as t
# The daily summaries are built with the published summary-contract@1 before the operations run.
inputs = t.scenario_inputs()
original = os.environ
recorder = Environ(original)
os.environ = recorder
phase[0] = "calls"
first = t.scenario_text(inputs)
second = t.scenario_text(inputs)
phase[0] = "done"
os.environ = original
print(json.dumps({"events": events, "environment": recorder.reads, "logs": records, "same": first == second}))
"""
        stdout, stderr = run_python(code, {"TZ": "Europe/London"})
        self.assertEqual(stderr, "")
        self.assertEqual(len(stdout.strip().splitlines()), 1, stdout)
        self.assertEqual(json.loads(stdout), {"events": [], "environment": [], "logs": [], "same": True})

    def test_values_are_frozen(self) -> None:
        summary = build_day_summary(DaySpec(TODAY))
        period = RangePeriod("custom", TODAY, TODAY)
        day = RangeDay(summary, ())
        result = RangeSummary(
            contract_version=V1, measure="daily-steps", record_type="steps", period=period, date_count=1,
            valued_dates=(TODAY,), absent_dates=(), primary_state_counts=(), evidence_condition_counts=(),
            publication_condition_counts=(), coverage=Coverage(2, 0, 0, 0, 0, 0), processor_versions=(),
            derivation_versions=(), complete=True)
        expected_fields = {
            RangePeriod: ["kind", "first_date", "last_date"],
            RangeDay: ["summary", "publication_conditions"],
            RangeSummary: RESULT_FIELDS,
        }
        for value in (period, day, result):
            with self.subTest(type(value).__name__):
                self.assertTrue(dataclasses.is_dataclass(value))
                self.assertTrue(type(value).__dataclass_params__.frozen)
                names = [item.name for item in dataclasses.fields(value)]
                self.assertEqual(names, expected_fields[type(value)])
                for name in names:
                    with self.assertRaises(dataclasses.FrozenInstanceError):
                        setattr(value, name, None)
        self.assertTrue(issubclass(RangeSummaryFailure, Exception))
        self.assertEqual(RangeSummaryFailure(PERIOD_INVALID).args, (PERIOD_INVALID,))


# ---- R3 ----

class R3Constants(unittest.TestCase):
    def test_constants_are_exact(self) -> None:
        self.assertEqual(CONTRACT_VERSION, "range-summary-contract@1")
        self.assertEqual(PRESET_LENGTHS, (7, 30, 90))
        self.assertEqual(PERIOD_KINDS, ("last-7-dates", "last-30-dates", "last-90-dates", "last-year",
                                        "calendar-year", "calendar-month", "iso-week", "custom"))
        self.assertIs(type(MAX_PERIOD_DATES), int)
        self.assertEqual(MAX_PERIOD_DATES, 366)
        self.assertEqual(FAILURES, ("range-period-invalid", "range-input-invalid"))
        self.assertEqual(list(FAILURES), [item["name"] for item in CONTRACT["named_failures"]])
        for vocabulary in (PRESET_LENGTHS, PERIOD_KINDS, FAILURES):
            self.assertIs(type(vocabulary), tuple)
        for length in PRESET_LENGTHS:
            self.assertIs(type(length), int)
        for name in PERIOD_KINDS + FAILURES:
            self.assertRegex(name, KEBAB)
        self.assertEqual(len(set(PERIOD_KINDS + FAILURES)), len(PERIOD_KINDS) + len(FAILURES))

    def test_shared_vocabularies_are_summary_contracts(self) -> None:
        self.assertIs(MEASURES, summary_contract.MEASURES)
        self.assertIs(PRIMARY_STATES, summary_contract.PRIMARY_STATES)
        self.assertIs(EVIDENCE_CONDITIONS, summary_contract.EVIDENCE_CONDITIONS)
        self.assertIs(PUBLICATION_CONDITIONS, summary_contract.PUBLICATION_CONDITIONS)
        self.assertIs(range_summary_contract.MEASURES, summary_contract.MEASURES)
        self.assertEqual(PRIMARY_STATES, STATE_NAMES)
        self.assertEqual(EVIDENCE_CONDITIONS, EVIDENCE_NAMES)
        self.assertEqual(PUBLICATION_CONDITIONS, PUBLICATION_NAMES)
        for measure, record_type in MEASURE_PAIRS:
            self.assertIn(record_type, MEASURES[measure])
        self.assertTrue(set(PERIOD_KINDS + FAILURES).isdisjoint(STATE_NAMES + EVIDENCE_NAMES + PUBLICATION_NAMES))


# ---- R4 ----

class R4Presets(Checks):
    def test_presets_end_today_with_exact_length(self) -> None:
        self.assert_period(preset_period(7, TODAY), "last-7-dates", date(2026, 11, 22), TODAY, 7)
        self.assert_period(preset_period(30, TODAY), "last-30-dates", date(2026, 10, 30), TODAY, 30)
        self.assert_period(preset_period(90, TODAY), "last-90-dates", date(2026, 8, 31), TODAY, 90)
        for length in (7, 30, 90):
            with self.subTest(length=length):
                period = preset_period(length, TODAY)
                self.assertEqual(period.last_date, TODAY)
                self.assertEqual(len(period_dates(period)), length)
                self.assertEqual(summarize(period, ()).date_count, length)
        self.assert_cases([(preset_period, (length, today), model_preset(length, today))
                           for today in generated_todays(300) for length in (7, 30, 90)], 900)

    def test_presets_cross_month_year_and_leap_day(self) -> None:
        week = preset_period(7, date(2028, 3, 1))
        self.assert_period(week, "last-7-dates", date(2028, 2, 24), date(2028, 3, 1), 7)
        self.assertIn(date(2028, 2, 29), period_dates(week))
        self.assert_period(preset_period(7, date(2100, 3, 1)), "last-7-dates", date(2100, 2, 23), date(2100, 3, 1), 7)
        self.assert_period(preset_period(7, date(2000, 3, 1)), "last-7-dates", date(2000, 2, 24), date(2000, 3, 1), 7)
        self.assert_period(preset_period(7, date(2027, 1, 3)), "last-7-dates", date(2026, 12, 28), date(2027, 1, 3), 7)
        self.assert_period(preset_period(30, date(2028, 3, 1)), "last-30-dates", date(2028, 2, 1), date(2028, 3, 1), 30)
        self.assert_period(preset_period(90, date(2027, 1, 15)), "last-90-dates", date(2026, 10, 18),
                           date(2027, 1, 15), 90)
        self.assert_period(preset_period(90, LAST_DATE), "last-90-dates", date(9999, 10, 3), LAST_DATE, 90)
        # The first date may be 0001-01-01, never before it.
        self.assert_period(preset_period(7, date(1, 1, 7)), "last-7-dates", date(1, 1, 1), date(1, 1, 7), 7)
        self.period_refused(preset_period, 7, date(1, 1, 6))
        self.period_refused(preset_period, 90, date(1, 3, 30))
        self.assert_period(preset_period(90, date(1, 3, 31)), "last-90-dates", date(1, 1, 1), date(1, 3, 31), 90)
        rng = random.Random(SEED + 4)
        cases = []
        for year in YEARS:
            for _ in range(20):
                today = as_date(rng.randint(day_number(year, 1, 1), day_number(year, 12, 31)))
                cases.extend((preset_period, (length, today), model_preset(length, today)) for length in (7, 30, 90))
        self.assert_cases(cases, 2000)

    def test_other_preset_lengths_are_refused(self) -> None:
        for length in (0, 1, 6, 8, 14, 29, 31, 60, 89, 91, 365, 366, -7, True, False, 7.0, 30.0, "7", None, (7,), [7]):
            with self.subTest(length=repr(length)):
                self.period_refused(preset_period, length, TODAY)

    def test_today_must_be_a_date(self) -> None:
        bad_dates = (datetime(2026, 11, 28), datetime(2026, 11, 28, tzinfo=UTC), "2026-11-28", None, 20261128,
                     TODAY.toordinal(), (2026, 11, 28))
        for bad in bad_dates:
            with self.subTest(today=repr(bad)):
                self.period_refused(preset_period, 7, bad)
                self.period_refused(last_year_period, bad)
                self.period_refused(calendar_year_period, 2026, bad)
                self.period_refused(calendar_month_period, 2026, 11, bad)
                self.period_refused(iso_week_period, 2026, 48, bad)
                self.period_refused(custom_period, date(2026, 11, 1), date(2026, 11, 2), bad)
                # R7: a custom range's own dates are checked the same way.
                self.period_refused(custom_period, bad, TODAY, TODAY)
                self.period_refused(custom_period, date(2026, 11, 1), bad, TODAY)
        # Year 1 has no year before it, so it has no rolling year.
        self.period_refused(last_year_period, date(1, 1, 1))
        self.period_refused(last_year_period, date(1, 12, 31))
        self.assert_period(last_year_period(date(2, 1, 1)), "last-year", date(1, 1, 2), date(2, 1, 1), 365)

    def test_last_year_in_a_common_year_has_365_dates(self) -> None:
        self.assert_period(last_year_period(TODAY), "last-year", date(2025, 11, 29), TODAY, 365)
        self.assert_period(last_year_period(date(2026, 1, 1)), "last-year", date(2025, 1, 2), date(2026, 1, 1), 365)
        self.assert_period(last_year_period(date(2026, 12, 31)), "last-year", date(2026, 1, 1), date(2026, 12, 31), 365)
        self.assert_period(last_year_period(date(2101, 2, 28)), "last-year", date(2100, 3, 1), date(2101, 2, 28), 365)
        self.assert_period(last_year_period(date(1901, 2, 28)), "last-year", date(1900, 3, 1), date(1901, 2, 28), 365)
        self.assert_period(last_year_period(LAST_DATE), "last-year", date(9999, 1, 1), LAST_DATE, 365)
        # Every date of these common years, against the model.
        cases = []
        for year in (1900, 2026, 2027, 2100, 2101):
            for number in range(day_number(year, 1, 1), day_number(year, 12, 31) + 1):
                today = as_date(number)
                cases.append((last_year_period, (today,), model_last_year(today)))
        self.assert_cases(cases, 1800)
        for _, (today,), (_, first, last) in cases:
            span = number_of(last) - number_of(first) + 1
            self.assertEqual(span, 366 if contains_leap_day(first, last) else 365, today)

    def test_last_year_containing_29_february_has_366_dates(self) -> None:
        period = last_year_period(date(2028, 3, 1))
        self.assert_period(period, "last-year", date(2027, 3, 2), date(2028, 3, 1), 366)
        self.assertIn(date(2028, 2, 29), period_dates(period))
        self.assert_period(last_year_period(date(2028, 12, 31)), "last-year", date(2028, 1, 1), date(2028, 12, 31), 366)
        self.assert_period(last_year_period(date(2000, 3, 1)), "last-year", date(1999, 3, 2), date(2000, 3, 1), 366)
        # Every date from a leap year through the year after it, against the model.
        cases = []
        for first_year in (1903, 1999, 2027, 2399):
            for number in range(day_number(first_year, 1, 1), day_number(first_year + 3, 1, 1)):
                today = as_date(number)
                cases.append((last_year_period, (today,), model_last_year(today)))
        cases.extend((last_year_period, (today,), model_last_year(today)) for today in generated_todays(1500, SEED + 1)
                     if today.year > 1)
        self.assert_cases(cases, 5000)
        long_years = 0
        for _, (today,), (_, first, last) in cases:
            has_leap_day = contains_leap_day(first, last)
            self.assertEqual(number_of(last) - number_of(first) + 1, 366 if has_leap_day else 365, today)
            long_years += has_leap_day
        self.assertGreater(long_years, 1000)

    def test_last_year_on_29_february_starts_1_march(self) -> None:
        self.assert_period(last_year_period(date(2028, 2, 29)), "last-year", date(2027, 3, 1), date(2028, 2, 29), 366)
        self.assert_period(last_year_period(date(2000, 2, 29)), "last-year", date(1999, 3, 1), date(2000, 2, 29), 366)
        self.assert_period(last_year_period(date(2400, 2, 29)), "last-year", date(2399, 3, 1), date(2400, 2, 29), 366)
        self.assert_period(last_year_period(date(4, 2, 29)), "last-year", date(3, 3, 1), date(4, 2, 29), 366)
        self.assert_cases([(last_year_period, (date(year, 2, 29),), model_last_year(date(year, 2, 29)))
                           for year in range(4, 10000, 4) if leap(year)], 2400)

    def test_last_year_on_1_march_after_a_leap_day(self) -> None:
        self.assert_period(last_year_period(date(2029, 3, 1)), "last-year", date(2028, 3, 2), date(2029, 3, 1), 365)
        self.assert_period(last_year_period(date(2001, 3, 1)), "last-year", date(2000, 3, 2), date(2001, 3, 1), 365)
        self.assert_period(last_year_period(date(2028, 3, 1)), "last-year", date(2027, 3, 2), date(2028, 3, 1), 366)
        self.assert_cases([(last_year_period, (date(year, 3, 1),), model_last_year(date(year, 3, 1)))
                           for year in range(2, 10000)], 9000)

    def test_last_year_on_28_february_after_a_leap_year(self) -> None:
        period = last_year_period(date(2029, 2, 28))
        self.assert_period(period, "last-year", date(2028, 2, 29), date(2029, 2, 28), 366)
        self.assertEqual(period_dates(period)[0], date(2028, 2, 29))
        self.assert_period(last_year_period(date(2001, 2, 28)), "last-year", date(2000, 2, 29), date(2001, 2, 28), 366)
        self.assert_period(last_year_period(date(2028, 2, 28)), "last-year", date(2027, 3, 1), date(2028, 2, 28), 365)
        self.assert_period(last_year_period(date(2101, 2, 28)), "last-year", date(2100, 3, 1), date(2101, 2, 28), 365)
        self.assert_cases([(last_year_period, (date(year, 2, 28),), model_last_year(date(year, 2, 28)))
                           for year in range(2, 10000)], 9000)


# ---- R5 ----

class R5CalendarYears(Checks):
    def test_past_calendar_year_is_whole(self) -> None:
        self.assert_period(calendar_year_period(2025, TODAY), "calendar-year", date(2025, 1, 1), date(2025, 12, 31), 365)
        self.assert_period(calendar_year_period(1900, TODAY), "calendar-year", date(1900, 1, 1), date(1900, 12, 31), 365)
        self.assert_period(calendar_year_period(1, TODAY), "calendar-year", date(1, 1, 1), date(1, 12, 31), 365)
        self.assert_period(calendar_year_period(2100, date(2101, 1, 1)), "calendar-year", date(2100, 1, 1),
                           date(2100, 12, 31), 365)
        self.assert_cases([(calendar_year_period, (year, LAST_DATE), model_calendar_year(year, LAST_DATE))
                           for year in range(1, 10000)], 9999)

    def test_leap_year_has_366_dates(self) -> None:
        for year in (4, 400, 1600, 1904, 2000, 2024, 2028, 2400, 9996):
            with self.subTest(year=year):
                period = calendar_year_period(year, LAST_DATE)
                self.assert_period(period, "calendar-year", date(year, 1, 1), date(year, 12, 31), 366)
                self.assertIn(date(year, 2, 29), period_dates(period))
        for year in (1, 100, 1700, 1800, 1900, 2026, 2100, 2200, 2300, 9999):
            with self.subTest(year=year):
                dates = period_dates(calendar_year_period(year, LAST_DATE))
                self.assertEqual(len(dates), 365)
                self.assertNotIn((2, 29), [(day.month, day.day) for day in dates])
        for year in YEARS:
            with self.subTest(year=year):
                self.assertEqual(len(period_dates(calendar_year_period(year, LAST_DATE))), 366 if leap(year) else 365)

    def test_current_calendar_year_ends_today(self) -> None:
        self.assert_period(calendar_year_period(2026, TODAY), "calendar-year", date(2026, 1, 1), TODAY, 332)
        self.assert_period(calendar_year_period(2027, date(2027, 1, 1)), "calendar-year", date(2027, 1, 1),
                           date(2027, 1, 1), 1)
        self.assert_period(calendar_year_period(2028, date(2028, 2, 29)), "calendar-year", date(2028, 1, 1),
                           date(2028, 2, 29), 60)
        self.assert_period(calendar_year_period(2026, date(2026, 12, 31)), "calendar-year", date(2026, 1, 1),
                           date(2026, 12, 31), 365)
        self.assert_period(calendar_year_period(9999, LAST_DATE), "calendar-year", date(9999, 1, 1), LAST_DATE, 365)
        self.assert_cases([(calendar_year_period, (today.year, today), model_calendar_year(today.year, today))
                           for today in generated_todays(500, SEED + 5)], 700)

    def test_future_calendar_year_is_refused(self) -> None:
        self.period_refused(calendar_year_period, 2027, TODAY)
        self.period_refused(calendar_year_period, 2028, TODAY)
        self.period_refused(calendar_year_period, 2027, date(2026, 12, 31))
        for year in (0, -1, 10000, True, False, 2026.0, "2026", None):
            with self.subTest(year=repr(year)):
                self.period_refused(calendar_year_period, year, LAST_DATE)
        self.assert_cases([(calendar_year_period, (today.year + step, today), model_calendar_year(today.year + step, today))
                           for today in generated_todays(300, SEED + 6) for step in (-1, 1, 2)], 1000)


# ---- R6 ----

class R6MonthsAndWeeks(Checks):
    def test_calendar_month_boundaries_include_leap_day(self) -> None:
        february = calendar_month_period(2028, 2, TODAY.replace(year=2029))
        self.assert_period(february, "calendar-month", date(2028, 2, 1), date(2028, 2, 29), 29)
        self.assertEqual(period_dates(february)[-1], date(2028, 2, 29))
        self.assert_period(calendar_month_period(2026, 2, TODAY), "calendar-month", date(2026, 2, 1),
                           date(2026, 2, 28), 28)
        self.assert_period(calendar_month_period(2000, 2, TODAY), "calendar-month", date(2000, 2, 1),
                           date(2000, 2, 29), 29)
        self.assert_period(calendar_month_period(1900, 2, TODAY), "calendar-month", date(1900, 2, 1),
                           date(1900, 2, 28), 28)
        self.assert_period(calendar_month_period(2100, 2, LAST_DATE), "calendar-month", date(2100, 2, 1),
                           date(2100, 2, 28), 28)
        self.assert_period(calendar_month_period(2026, 4, TODAY), "calendar-month", date(2026, 4, 1),
                           date(2026, 4, 30), 30)
        self.assert_period(calendar_month_period(2025, 12, TODAY), "calendar-month", date(2025, 12, 1),
                           date(2025, 12, 31), 31)
        self.assert_period(calendar_month_period(9999, 12, LAST_DATE), "calendar-month", date(9999, 12, 1),
                           LAST_DATE, 31)
        rng = random.Random(SEED + 7)
        years = YEARS + tuple(rng.randint(1, 9999) for _ in range(200))
        self.assert_cases([(calendar_month_period, (year, month, LAST_DATE), model_month(year, month, LAST_DATE))
                           for year in years for month in range(1, 13)], 2900)
        for year in YEARS:
            with self.subTest(year=year):
                self.assertEqual(len(period_dates(calendar_month_period(year, 2, LAST_DATE))), 29 if leap(year) else 28)

    def test_iso_week_is_monday_to_sunday_across_years(self) -> None:
        self.assert_period(iso_week_period(2026, 53, LAST_DATE), "iso-week", date(2026, 12, 28), date(2027, 1, 3), 7)
        self.assert_period(iso_week_period(2027, 1, LAST_DATE), "iso-week", date(2027, 1, 4), date(2027, 1, 10), 7)
        self.assert_period(iso_week_period(2020, 1, LAST_DATE), "iso-week", date(2019, 12, 30), date(2020, 1, 5), 7)
        self.assert_period(iso_week_period(2009, 1, LAST_DATE), "iso-week", date(2008, 12, 29), date(2009, 1, 4), 7)
        self.assert_period(iso_week_period(2020, 53, LAST_DATE), "iso-week", date(2020, 12, 28), date(2021, 1, 3), 7)
        self.assert_period(iso_week_period(2026, 48, LAST_DATE), "iso-week", date(2026, 11, 23), date(2026, 11, 29), 7)
        self.assert_period(iso_week_period(1, 1, LAST_DATE), "iso-week", date(1, 1, 1), date(1, 1, 7), 7)
        # Week 1 and the last week of every ISO year, against the model.
        cases = []
        for iso_year in range(1, 10000):
            for week in (1, iso_weeks_in(iso_year)):
                cases.append((iso_week_period, (iso_year, week, LAST_DATE), model_iso_week(iso_year, week, LAST_DATE)))
        self.assert_cases(cases, 19998)
        # The week of each generated date contains it and starts on a Monday.
        for today in generated_todays(300, SEED + 8):
            iso_year, week = iso_week_of(number_of(today))
            if iso_year > 9999:
                continue
            period = iso_week_period(iso_year, week, LAST_DATE)
            self.assertEqual((number_of(period.first_date) % 7, period.first_date <= today <= period.last_date),
                             (0, True), today)

    def test_week_53_exists_only_in_long_years(self) -> None:
        self.assert_period(iso_week_period(2026, 53, LAST_DATE), "iso-week", date(2026, 12, 28), date(2027, 1, 3), 7)
        self.assert_period(iso_week_period(2015, 53, LAST_DATE), "iso-week", date(2015, 12, 28), date(2016, 1, 3), 7)
        for iso_year in (2027, 2019, 2021, 2024, 2000, 1900):
            with self.subTest(iso_year=iso_year):
                self.period_refused(iso_week_period, iso_year, 53, LAST_DATE)
        for week in (0, -1, 54, True, False, 48.0, "48", None):
            with self.subTest(week=repr(week)):
                self.period_refused(iso_week_period, 2026, week, LAST_DATE)
        for iso_year in (0, -1, 10000, True, 2026.0, "2026", None):
            with self.subTest(iso_year=repr(iso_year)):
                self.period_refused(iso_week_period, iso_year, 1, LAST_DATE)
        cases = [(iso_week_period, (iso_year, 53, LAST_DATE), model_iso_week(iso_year, 53, LAST_DATE))
                 for iso_year in range(1, 10000)]
        self.assert_cases(cases, 9999)
        self.assertGreater(sum(1 for _, _, expected in cases if expected != PERIOD_INVALID), 1700)

    def test_current_month_and_week_end_today(self) -> None:
        self.assert_period(calendar_month_period(2026, 11, TODAY), "calendar-month", date(2026, 11, 1), TODAY, 28)
        self.assert_period(iso_week_period(2026, 48, TODAY), "iso-week", date(2026, 11, 23), TODAY, 6)
        self.assert_period(calendar_month_period(2026, 12, date(2026, 12, 1)), "calendar-month", date(2026, 12, 1),
                           date(2026, 12, 1), 1)
        self.assert_period(iso_week_period(2026, 49, date(2026, 11, 30)), "iso-week", date(2026, 11, 30),
                           date(2026, 11, 30), 1)
        self.assert_period(iso_week_period(2026, 48, date(2026, 11, 29)), "iso-week", date(2026, 11, 23),
                           date(2026, 11, 29), 7)
        self.assert_period(calendar_month_period(2028, 2, date(2028, 2, 29)), "calendar-month", date(2028, 2, 1),
                           date(2028, 2, 29), 29)
        self.assert_period(iso_week_period(2026, 53, date(2026, 12, 31)), "iso-week", date(2026, 12, 28),
                           date(2026, 12, 31), 4)
        self.assert_period(iso_week_period(2026, 53, date(2027, 1, 1)), "iso-week", date(2026, 12, 28),
                           date(2027, 1, 1), 5)
        # The last week of the date range is cut at its last date.
        self.assert_period(iso_week_period(9999, 52, LAST_DATE), "iso-week", date(9999, 12, 27), LAST_DATE, 5)
        cases = []
        for today in generated_todays(400, SEED + 9):
            cases.append((calendar_month_period, (today.year, today.month, today),
                          model_month(today.year, today.month, today)))
            iso_year, week = iso_week_of(number_of(today))
            cases.append((iso_week_period, (iso_year, week, today), model_iso_week(iso_year, week, today)))
        self.assert_cases(cases, 1400)

    def test_future_month_and_week_are_refused(self) -> None:
        self.period_refused(calendar_month_period, 2026, 12, TODAY)
        self.period_refused(calendar_month_period, 2027, 1, TODAY)
        self.period_refused(iso_week_period, 2026, 49, TODAY)
        self.period_refused(iso_week_period, 2026, 49, date(2026, 11, 29))
        self.period_refused(iso_week_period, 2027, 1, TODAY)
        self.period_refused(iso_week_period, 2027, 1, date(2027, 1, 3))
        self.period_refused(calendar_month_period, 2027, 1, date(2026, 12, 31))
        for month in (0, 13, -1, True, False, 11.0, "11", None):
            with self.subTest(month=repr(month)):
                self.period_refused(calendar_month_period, 2026, month, TODAY)
        for year in (0, -1, 10000, True, 2026.0, "2026", None):
            with self.subTest(year=repr(year)):
                self.period_refused(calendar_month_period, year, 1, LAST_DATE)
        cases = []
        for today in generated_todays(300, SEED + 10):
            year, month = (today.year, today.month + 1) if today.month < 12 else (today.year + 1, 1)
            cases.append((calendar_month_period, (year, month, today), model_month(year, month, today)))
            iso_year, week = iso_week_of(number_of(today) + 7)
            cases.append((iso_week_period, (iso_year, week, today), model_iso_week(iso_year, week, today)))
        self.assert_cases(cases, 1000)
        self.assertTrue(all(expected == PERIOD_INVALID for _, _, expected in cases))


# ---- R7 ----

class R7CustomRanges(Checks):
    def test_custom_range_is_inclusive(self) -> None:
        self.assert_period(custom_period(date(2026, 11, 1), TODAY, TODAY), "custom", date(2026, 11, 1), TODAY, 28)
        self.assert_period(custom_period(date(2028, 2, 1), date(2028, 3, 1), date(2028, 3, 1)), "custom",
                           date(2028, 2, 1), date(2028, 3, 1), 30)
        # Exactly 366 dates, with and without a 29 February.
        self.assert_period(custom_period(date(2028, 1, 1), date(2028, 12, 31), date(2029, 1, 1)), "custom",
                           date(2028, 1, 1), date(2028, 12, 31), 366)
        self.assert_period(custom_period(date(2025, 11, 28), TODAY, TODAY), "custom", date(2025, 11, 28), TODAY, 366)
        rng = random.Random(SEED + 11)
        cases = []
        for today in generated_todays(400, SEED + 12):
            for span in (rng.randint(1, 370), 366, 367, 1):
                last = number_of(today) - rng.choice((0, 0, 1, 5, -1))
                first = last - span + 1
                if first < 0 or last > number_of(LAST_DATE) or last < 0:
                    continue
                args = (as_date(first), as_date(last), today)
                cases.append((custom_period, args, model_custom(*args)))
        self.assert_cases(cases, 1500)

    def test_custom_range_may_be_one_date(self) -> None:
        self.assert_period(custom_period(TODAY, TODAY, TODAY), "custom", TODAY, TODAY, 1)
        self.assert_period(custom_period(date(2024, 2, 29), date(2024, 2, 29), TODAY), "custom", date(2024, 2, 29),
                           date(2024, 2, 29), 1)
        self.assert_period(custom_period(date(1, 1, 1), date(1, 1, 1), date(1, 1, 1)), "custom", date(1, 1, 1),
                           date(1, 1, 1), 1)
        self.assert_period(custom_period(LAST_DATE, LAST_DATE, LAST_DATE), "custom", LAST_DATE, LAST_DATE, 1)

    def test_custom_range_after_today_is_refused(self) -> None:
        self.period_refused(custom_period, date(2026, 11, 1), date(2026, 11, 29), TODAY)
        self.period_refused(custom_period, date(2026, 11, 29), date(2026, 11, 29), TODAY)
        self.period_refused(custom_period, date(2026, 12, 1), date(2026, 12, 31), TODAY)
        self.period_refused(custom_period, date(2028, 2, 29), date(2028, 3, 1), date(2028, 2, 29))
        self.assert_period(custom_period(date(2026, 11, 1), TODAY, date(2026, 11, 29)), "custom", date(2026, 11, 1),
                           TODAY, 28)

    def test_reversed_custom_range_is_refused(self) -> None:
        self.period_refused(custom_period, TODAY, date(2026, 11, 27), TODAY)
        self.period_refused(custom_period, date(2028, 3, 1), date(2028, 2, 29), TODAY)
        self.period_refused(custom_period, date(2027, 1, 1), date(2026, 12, 31), TODAY)

    def test_custom_range_longer_than_366_dates_is_refused(self) -> None:
        self.period_refused(custom_period, date(2025, 11, 27), TODAY, TODAY)
        self.period_refused(custom_period, date(2027, 12, 31), date(2028, 12, 31), date(2029, 1, 1))
        self.period_refused(custom_period, date(2027, 3, 1), date(2028, 3, 1), date(2028, 3, 1))
        self.period_refused(custom_period, date(1, 1, 1), LAST_DATE, LAST_DATE)
        self.assert_period(custom_period(date(2027, 3, 2), date(2028, 3, 1), date(2028, 3, 1)), "custom",
                           date(2027, 3, 2), date(2028, 3, 1), 366)


# ---- R8 ----

class R8PeriodsGivenBack(Checks):
    def test_period_dates_are_inclusive_and_ascending(self) -> None:
        periods = [
            preset_period(7, TODAY), preset_period(30, date(2028, 3, 15)), preset_period(90, date(2100, 3, 31)),
            last_year_period(TODAY), last_year_period(date(2028, 2, 29)), last_year_period(date(2029, 2, 28)),
            calendar_year_period(2028, LAST_DATE), calendar_year_period(2026, TODAY),
            calendar_month_period(2028, 2, LAST_DATE), calendar_month_period(2026, 11, TODAY),
            iso_week_period(2026, 53, LAST_DATE), iso_week_period(2026, 48, TODAY), iso_week_period(9999, 52, LAST_DATE),
            custom_period(date(2025, 11, 28), TODAY, TODAY), custom_period(TODAY, TODAY, TODAY),
            # Periods a builder gives for some other today.
            RangePeriod("calendar-year", date(2026, 1, 1), date(2026, 3, 5)),
            RangePeriod("calendar-month", date(2026, 2, 1), date(2026, 2, 1)),
            RangePeriod("iso-week", date(2026, 12, 28), date(2026, 12, 30)),
            RangePeriod("last-year", date(1, 1, 2), date(2, 1, 1)),
            RangePeriod("last-7-dates", date(1, 1, 1), date(1, 1, 7)),
        ]
        for period in periods:
            with self.subTest(period=repr(period)):
                dates = period_dates(period)
                self.assertIs(type(dates), tuple)
                self.assertEqual(dates, model_dates(period.first_date, period.last_date))
                for day in dates:
                    self.assertIs(type(day), date)
                self.assertTrue(all(a < b for a, b in zip(dates, dates[1:])))
                self.assertEqual((dates[0], dates[-1]), (period.first_date, period.last_date))
                result = summarize(period, ())
                self.assertEqual(result.date_count, len(dates))
                self.assertEqual(result.absent_dates, dates)

    def test_inconsistent_period_is_refused(self) -> None:
        bad = [
            ("custom", date(2026, 11, 2), date(2026, 11, 1)),
            ("custom", date(2025, 11, 27), TODAY),
            ("last-7-dates", date(2026, 11, 23), TODAY),
            ("last-7-dates", date(2026, 11, 21), TODAY),
            ("last-30-dates", date(2026, 10, 31), TODAY),
            ("last-90-dates", date(2026, 8, 30), TODAY),
            ("last-year", date(2025, 11, 28), TODAY),
            ("last-year", date(2025, 11, 30), TODAY),
            ("last-year", date(2027, 3, 2), date(2028, 2, 29)),
            ("last-year", date(2027, 3, 3), date(2028, 3, 1)),
            ("last-year", date(2028, 3, 1), date(2029, 2, 28)),
            ("last-year", date(1, 1, 1), date(1, 12, 31)),
            ("calendar-year", date(2026, 1, 2), TODAY),
            ("calendar-year", date(2025, 1, 1), date(2026, 1, 1)),
            ("calendar-month", date(2026, 11, 2), TODAY),
            ("calendar-month", date(2026, 1, 1), date(2026, 2, 1)),
            ("iso-week", date(2026, 11, 24), TODAY),
            ("iso-week", date(2026, 11, 22), TODAY),
            ("iso-week", date(2026, 11, 23), date(2026, 11, 30)),
            ("last-14-dates", date(2026, 11, 15), TODAY),
            ("LAST-7-DATES", date(2026, 11, 22), TODAY),
            ("", TODAY, TODAY),
            (None, TODAY, TODAY),
            (HEALTH_MARKER, TODAY, TODAY),
            ("custom", datetime(2026, 11, 1), TODAY),
            ("custom", date(2026, 11, 1), datetime(2026, 11, 28)),
            ("custom", "2026-11-01", TODAY),
            ("custom", date(2026, 11, 1), None),
        ]
        for kind, first, last in bad:
            with self.subTest(kind=kind, first=repr(first), last=repr(last)):
                period = RangePeriod(kind, first, last)
                self.period_refused(period_dates, period)
                self.assertEqual(self.refused(summarize, period, ()), PERIOD_INVALID)
        for other in (("custom", TODAY, TODAY), None, types.SimpleNamespace(kind="custom", first_date=TODAY,
                                                                              last_date=TODAY), "custom"):
            with self.subTest(other=repr(other)):
                self.period_refused(period_dates, other)
                self.assertEqual(self.refused(summarize, other, ()), PERIOD_INVALID)
        # Each kind, near its valid shapes, against the model in both directions.
        rng = random.Random(SEED + 13)
        cases = 0
        for today in generated_todays(150, SEED + 14):
            iso_year, week = iso_week_of(number_of(today))
            built = [preset_period(7, today) if today >= date(1, 1, 7) else None,
                     preset_period(30, today) if today >= date(1, 1, 30) else None,
                     last_year_period(today) if today.year > 1 else None,
                     calendar_year_period(today.year, today), calendar_month_period(today.year, today.month, today),
                     iso_week_period(iso_year, week, today) if iso_year <= 9999 else None,
                     custom_period(today, today, today)]
            for period in filter(None, built):
                for shift_first, shift_last in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1), (7, 0), (-7, 0), (0, 7),
                                                (rng.randint(-400, 400), rng.randint(-400, 400))):
                    a = number_of(period.first_date) + shift_first
                    b = number_of(period.last_date) + shift_last
                    if not (0 <= a <= number_of(LAST_DATE) and 0 <= b <= number_of(LAST_DATE)):
                        continue
                    candidate = RangePeriod(period.kind, as_date(a), as_date(b))
                    expected = model_consistent(candidate.kind, candidate.first_date, candidate.last_date)
                    self.assertEqual(accepted(candidate), "accepted" if expected else PERIOD_INVALID, candidate)
                    cases += 1
        self.assertGreater(cases, 5000)


def accepted(period: RangePeriod):
    """"accepted" when period_dates gives the model's dates, else the failure name."""
    try:
        dates = period_dates(period)
    except RangeSummaryFailure as failure:
        return failure.args[0]
    return "accepted" if dates == model_dates(period.first_date, period.last_date) else "wrong-dates"


# ---- R9 ----

class R9DailySummaries(Checks):
    def setUp(self) -> None:
        self.period = custom(date(2026, 11, 22), TODAY)

    def test_days_must_be_health_summaries_of_the_range_key(self) -> None:
        good = make_day(DaySpec(date(2026, 11, 23)))
        summary = good.summary
        future = dataclasses.replace(summary, header=dataclasses.replace(summary.header,
                                                                          contract_version="summary-contract@2"))
        heart = build_day_summary(DaySpec(date(2026, 11, 23), measure="heart-rate-summary", record_type="heart-rate"))
        forged_type = dataclasses.replace(summary, header=dataclasses.replace(summary.header, record_type="heart-rate"))
        for days in ([RangeDay(None, ())], [RangeDay(summary.header, ())], [RangeDay(summary.header_bytes, ())],
                     [RangeDay(future, ())], [RangeDay(heart, ())], [RangeDay(forged_type, ())],
                     [(summary, ())], [summary], [good, None], [None], "", "abc", HEALTH_MARKER, None, 5, good):
            with self.subTest(days=repr(days)[:80]):
                self.input_refused(self.period, days)
        # Each listed pair, with its own summaries.
        for measure, record_type in MEASURE_PAIRS:
            with self.subTest(measure=measure):
                specs = [DaySpec(date(2026, 11, 22), measure=measure, record_type=record_type),
                         DaySpec(TODAY, "partial", measure=measure, record_type=record_type)]
                self.summarized(self.period, specs, measure, record_type)
                other = "heart-rate-summary" if measure == "daily-steps" else "daily-steps"
                self.input_refused(self.period, [make_day(spec) for spec in specs], other, MEASURES[other][0])

    def test_unknown_measure_pair_is_refused(self) -> None:
        for measure, record_type in (("daily-steps", "heart-rate"), ("steps", "steps"), ("daily-steps", "Steps"),
                                     ("", ""), (None, "steps"), ("daily-steps", None), (HEALTH_MARKER, "steps"),
                                     ("weight-series", "steps"), (("daily-steps",), "steps")):
            with self.subTest(measure=repr(measure), record_type=repr(record_type)):
                self.input_refused(self.period, (), measure, record_type)
        # A summary forged with the same unlisted pair as the range is refused too.
        summary = build_day_summary(DaySpec(date(2026, 11, 23)))
        forged = dataclasses.replace(summary, header=dataclasses.replace(summary.header, measure="daily-weight",
                                                                         record_type="weight"))
        self.input_refused(self.period, [RangeDay(forged, ())], "daily-weight", "weight")

    def test_days_outside_the_period_are_refused(self) -> None:
        for day in (date(2026, 11, 21), date(2026, 11, 29), date(2025, 11, 23), date(2027, 11, 23)):
            with self.subTest(day=day):
                self.input_refused(self.period, [make_day(DaySpec(day))])
                inside = [make_day(DaySpec(date(2026, 11, 22))), make_day(DaySpec(TODAY))]
                self.input_refused(self.period, sorted(inside + [make_day(DaySpec(day))],
                                                       key=lambda item: item.summary.header.local_date))
        self.summarized(self.period, [DaySpec(date(2026, 11, 22)), DaySpec(TODAY)])

    def test_unordered_or_repeated_dates_are_refused(self) -> None:
        first, second = make_day(DaySpec(date(2026, 11, 23))), make_day(DaySpec(date(2026, 11, 24), "missing"))
        third = make_day(DaySpec(date(2026, 11, 25), "partial"))
        for days in ([second, first], [first, first], [first, second, second],
                     [first, make_day(DaySpec(date(2026, 11, 23), "missing"))], [first, third, second],
                     [third, first, second]):
            with self.subTest(order=[day.summary.header.local_date.isoformat() for day in days]):
                self.input_refused(self.period, days)
        self.summarized(self.period, [DaySpec(date(2026, 11, 23)), DaySpec(date(2026, 11, 24), "missing"),
                                      DaySpec(date(2026, 11, 25), "partial")])

    def test_publication_conditions_are_checked(self) -> None:
        spec = DaySpec(date(2026, 11, 23))
        for conditions in (["source-conflict"], ["measured"], ["range-input-invalid"], ["Conflicted"], ["conflicted "],
                           [HEALTH_MARKER], [5], [None], ["conflicted", "unknown"], "conflicted", "", None, 5,
                           b"conflicted"):
            with self.subTest(conditions=repr(conditions)):
                self.input_refused(self.period, [make_day(spec, conditions)])
        for conditions in (["conflicted"], ("superseded", "conflicted"), {"refresh-failed"},
                           frozenset(PUBLICATION_NAMES), ["superseded", "superseded", "conflicted"], [], ()):
            with self.subTest(conditions=repr(conditions)):
                distinct = tuple(sorted(set(conditions)))
                result = self.summarized(self.period, [dataclasses.replace(spec, publication=distinct)],
                                         days=[make_day(spec, conditions)])
                self.assertEqual(dict(result.publication_condition_counts),
                                 {name: int(name in distinct) for name in PUBLICATION_NAMES})

    def test_gaps_are_allowed(self) -> None:
        specs = [DaySpec(date(2026, 11, 23)), DaySpec(date(2026, 11, 25), "missing"), DaySpec(date(2026, 11, 27))]
        result = self.summarized(self.period, specs)
        self.assertEqual(result.absent_dates, (date(2026, 11, 22), date(2026, 11, 24), date(2026, 11, 26), TODAY))
        self.summarized(self.period, [DaySpec(TODAY)])
        self.summarized(self.period, [DaySpec(date(2026, 11, 22))])
        self.summarized(last_year_period(date(2028, 3, 1)), [DaySpec(date(2027, 3, 2)), DaySpec(date(2028, 2, 29)),
                                                             DaySpec(date(2028, 3, 1))])

    def test_bodies_are_not_read(self) -> None:
        specs = [DaySpec(date(2026, 11, 22), body_total=1), DaySpec(date(2026, 11, 23), "partial", body_total=2),
                 DaySpec(date(2026, 11, 24), "missing"), DaySpec(TODAY, body_total=3)]
        expected = self.summarized(self.period, specs)
        # Other body bytes give the same result.
        other_bodies = [dataclasses.replace(spec, body_total=spec.body_total + 9000) for spec in specs]
        self.assertEqual(self.summarized(self.period, other_bodies), expected)
        # Bytes that are not even JSON give the same result; so do stand-ins that fail on any use.
        broken = [RangeDay(dataclasses.replace(day.summary, body_bytes=b"\xff" + HEALTH_MARKER.encode()
                                               if day.summary.body_bytes is not None else None), ())
                  for day in map(make_day, specs)]
        self.assertEqual(summarize(self.period, broken), expected)
        Untouchable.uses.clear()
        trapped = [RangeDay(dataclasses.replace(day.summary, header_bytes=Untouchable(), evidence_bytes=Untouchable(),
                                                body_bytes=Untouchable()), ()) for day in map(make_day, specs)]
        self.assertEqual(summarize(self.period, trapped), expected)
        self.assertEqual(Untouchable.uses, [])


# ---- R10 ----

class R10DatesAndStates(Checks):
    def setUp(self) -> None:
        self.period = custom(date(2026, 11, 22), TODAY)

    def test_absent_dates_are_not_missing(self) -> None:
        specs = [DaySpec(date(2026, 11, 22), "missing"), DaySpec(date(2026, 11, 24), "missing"),
                 DaySpec(date(2026, 11, 25))]
        result = self.summarized(self.period, specs)
        self.assertEqual(result.absent_dates, (date(2026, 11, 23), date(2026, 11, 26), date(2026, 11, 27), TODAY))
        self.assertEqual(dict(result.primary_state_counts)["missing"], 2)
        self.assertNotIn(date(2026, 11, 22), result.absent_dates)
        only_missing = self.summarized(self.period, [DaySpec(day, "missing") for day in week_of_days()])
        self.assertEqual((only_missing.absent_dates, dict(only_missing.primary_state_counts)["missing"]), ((), 7))
        self.assertFalse(only_missing.complete)

    def test_primary_states_are_counted_by_date(self) -> None:
        specs = [DaySpec(day, state) for day, state in zip(week_of_days(), STATE_NAMES)]
        result = self.summarized(self.period, specs)
        self.assertEqual(result.primary_state_counts, tuple((name, 1) for name in STATE_NAMES))
        result = self.summarized(self.period, [DaySpec(day, "partial") for day in week_of_days()[:5]])
        self.assertEqual(dict(result.primary_state_counts), {name: 5 if name == "partial" else 0 for name in STATE_NAMES})
        rng = random.Random(SEED + 15)
        for index in range(12):
            with self.subTest(sample=index):
                period = preset_period(30, date(2028, 3, 1) + timedelta(days=index * 31))
                self.summarized(period, random_specs(rng, period_dates(period)))

    def test_valued_dates_are_measured_or_partial(self) -> None:
        specs = [
            DaySpec(date(2026, 11, 22), "partial", frozenset({"source-conflict", "selection-unresolved"}),
                    ("refresh-failed", "conflicted")),
            DaySpec(date(2026, 11, 23), "failed"), DaySpec(date(2026, 11, 24), "quarantined"),
            DaySpec(date(2026, 11, 25), "measured", frozenset({"inferred-attribution"}), ("superseded",)),
            DaySpec(date(2026, 11, 26), "policy-pending"), DaySpec(date(2026, 11, 27), "unresolved"),
            DaySpec(TODAY, "missing"),
        ]
        result = self.summarized(self.period, specs)
        self.assertEqual(result.valued_dates, (date(2026, 11, 22), date(2026, 11, 25)))

    def test_counts_add_up_to_the_period(self) -> None:
        rng = random.Random(SEED + 16)
        periods = [last_year_period(date(2028, 3, 1)), calendar_year_period(2028, LAST_DATE), preset_period(90, TODAY),
                   calendar_month_period(2028, 2, LAST_DATE), iso_week_period(2026, 53, LAST_DATE),
                   custom_period(date(2025, 11, 28), TODAY, TODAY), preset_period(7, TODAY)]
        for period in periods:
            for density in (0.0, 0.5, 1.0):
                with self.subTest(period=repr(period), density=density):
                    result = self.summarized(period, random_specs(rng, period_dates(period), density))
                    states = dict(result.primary_state_counts)
                    self.assertEqual(sum(states.values()) + len(result.absent_dates), result.date_count)
                    self.assertEqual(len(result.valued_dates), states["measured"] + states["partial"])
                    self.assertEqual(result.date_count, len(period_dates(period)))

    def test_range_with_no_summaries_is_all_absent(self) -> None:
        for days in ((), [], iter(()), (day for day in ()), set()):
            with self.subTest(days=type(days).__name__):
                result = self.summarized(self.period, [], days=days)
                self.assertEqual(result.absent_dates, period_dates(self.period))
                self.assertEqual((result.valued_dates, result.processor_versions, result.derivation_versions,
                                  result.coverage, result.complete), ((), (), (), Coverage(0, 0, 0, 0, 0, 0), False))


# ---- R11 ----

class R11ConditionCounts(Checks):
    def setUp(self) -> None:
        self.period = custom(date(2026, 11, 22), TODAY)

    def test_evidence_conditions_are_counted_by_date(self) -> None:
        specs = [
            DaySpec(date(2026, 11, 22), "partial", frozenset({"source-conflict", "selection-unresolved",
                                                              "duplicate-grouped"})),
            DaySpec(date(2026, 11, 23), "measured", frozenset({"duplicate-grouped", "inferred-attribution"})),
            DaySpec(date(2026, 11, 24), "unresolved", frozenset({"source-conflict", "equal-time-conflict"})),
            DaySpec(date(2026, 11, 26), "failed", frozenset({"classification-conflict", "unallocatable-interval"})),
            DaySpec(TODAY, "partial", frozenset(EVIDENCE_NAMES)),
        ]
        result = self.summarized(self.period, specs)
        self.assertEqual(result.evidence_condition_counts, (
            ("classification-conflict", 2), ("duplicate-grouped", 3), ("equal-time-conflict", 2),
            ("inferred-attribution", 2), ("selection-unresolved", 2), ("source-conflict", 3),
            ("unallocatable-interval", 2)))

    def test_publication_conditions_are_counted_by_date(self) -> None:
        specs = [
            DaySpec(date(2026, 11, 22), publication=("conflicted", "superseded")),
            DaySpec(date(2026, 11, 23), "partial", publication=("refresh-failed",)),
            DaySpec(date(2026, 11, 25), "missing", publication=("newer-evidence-pending", "conflicted")),
            DaySpec(TODAY, "failed", publication=PUBLICATION_NAMES),
        ]
        days = [make_day(spec, list(spec.publication) * 3) for spec in specs]
        result = self.summarized(self.period, specs, days=days)
        self.assertEqual(result.publication_condition_counts, (
            ("conflicted", 3), ("newer-evidence-pending", 2), ("reconciliation-failed", 1),
            ("reconciliation-pending", 1), ("refresh-failed", 2), ("superseded", 2)))
        # An absent date carries none.
        self.assertEqual(len(result.absent_dates), 3)

    def test_every_name_is_present_with_zero_counts(self) -> None:
        for specs in ([], [DaySpec(date(2026, 11, 23))], [DaySpec(TODAY, "missing", publication=("superseded",))]):
            with self.subTest(days=len(specs)):
                result = self.summarized(self.period, specs)
                self.assertEqual([name for name, _ in result.primary_state_counts], list(STATE_NAMES))
                self.assertEqual([name for name, _ in result.evidence_condition_counts], list(EVIDENCE_NAMES))
                self.assertEqual([name for name, _ in result.publication_condition_counts], list(PUBLICATION_NAMES))
        self.assertEqual(self.summarized(self.period, []).evidence_condition_counts,
                         tuple((name, 0) for name in EVIDENCE_NAMES))

    def test_conditions_do_not_change_usable_dates(self) -> None:
        plain = [DaySpec(date(2026, 11, 22)), DaySpec(date(2026, 11, 23), "partial"),
                 DaySpec(date(2026, 11, 24), "unresolved"), DaySpec(date(2026, 11, 25), "failed")]
        conditioned = [
            DaySpec(date(2026, 11, 22), conditions=frozenset({"duplicate-grouped", "inferred-attribution"}),
                    publication=PUBLICATION_NAMES),
            DaySpec(date(2026, 11, 23), "partial", frozenset(EVIDENCE_NAMES), ("conflicted",)),
            DaySpec(date(2026, 11, 24), "unresolved", frozenset({"source-conflict"}), ("refresh-failed",)),
            DaySpec(date(2026, 11, 25), "failed", frozenset({"selection-unresolved"}), ("superseded",)),
        ]
        first, second = self.summarized(self.period, plain), self.summarized(self.period, conditioned)
        self.assertEqual((second.valued_dates, second.primary_state_counts, second.absent_dates),
                         (first.valued_dates, first.primary_state_counts, first.absent_dates))
        self.assertEqual(second.valued_dates, (date(2026, 11, 22), date(2026, 11, 23)))


# ---- R12 ----

class R12CoverageAndVersions(Checks):
    def setUp(self) -> None:
        self.period = custom(date(2026, 11, 22), TODAY)

    def test_coverage_is_summed_across_dates(self) -> None:
        specs = [
            DaySpec(date(2026, 11, 22), extra=("contributing",) * 3),
            DaySpec(date(2026, 11, 23), "partial", frozenset({"source-conflict"}), extra=("quarantined",)),
            DaySpec(date(2026, 11, 24), "failed", extra=("policy-pending", "unresolved", "failed")),
            DaySpec(date(2026, 11, 26), "unresolved", frozenset({"source-conflict"})),
            DaySpec(TODAY, "missing"),
        ]
        result = self.summarized(self.period, specs)
        self.assertEqual(result.coverage, Coverage(contributing=7, unresolved=3, in_conflict=2, policy_pending=1,
                                                   failed=2, quarantined=1))
        # The same record attributed to two dates counts twice.
        twice = [DaySpec(date(2026, 11, 22), first_id=40), DaySpec(date(2026, 11, 23), first_id=40)]
        self.assertEqual(self.summarized(self.period, twice).coverage, Coverage(4, 0, 0, 0, 0, 0))
        rng = random.Random(SEED + 17)
        for index in range(6):
            with self.subTest(sample=index):
                self.summarized(self.period, random_specs(rng, period_dates(self.period)))

    def test_mixed_versions_are_reported(self) -> None:
        specs = [
            DaySpec(date(2026, 11, 22), processor="interpret-steps@2", derivation="derive-synthetic-steps@10"),
            DaySpec(date(2026, 11, 23), "missing", processor="interpret-steps@10"),
            DaySpec(date(2026, 11, 24), "partial", processor="interpret-steps@1", derivation="derive-synthetic-steps@9"),
            DaySpec(date(2026, 11, 25), processor="interpret-steps@2", derivation="derive-synthetic-steps@10"),
            DaySpec(TODAY, "failed", processor="interpret-other@1"),
        ]
        result = self.summarized(self.period, specs)
        self.assertEqual(result.processor_versions,
                         ("interpret-other@1", "interpret-steps@1", "interpret-steps@10", "interpret-steps@2"))
        self.assertEqual(result.derivation_versions,
                         ("derive-synthetic-steps@1", "derive-synthetic-steps@10", "derive-synthetic-steps@9"))
        single = self.summarized(self.period, [DaySpec(date(2026, 11, 22)), DaySpec(TODAY)])
        self.assertEqual((single.processor_versions, single.derivation_versions),
                         (("interpret-steps@1",), (DERIVATION,)))


# ---- R13 ----

class R13Completeness(Checks):
    def setUp(self) -> None:
        self.period = custom(date(2026, 11, 22), TODAY)

    def test_complete_requires_every_date_measured(self) -> None:
        measured = [DaySpec(day) for day in week_of_days()]
        self.assertIs(self.summarized(self.period, measured).complete, True)
        # Evidence conditions a measured summary can carry do not affect it.
        with_conditions = [dataclasses.replace(spec, conditions=frozenset({"duplicate-grouped", "inferred-attribution"}))
                           for spec in measured]
        self.assertIs(self.summarized(self.period, with_conditions).complete, True)
        for state in STATE_NAMES[1:]:
            for position in (0, 3, 6):
                with self.subTest(state=state, position=position):
                    specs = list(measured)
                    specs[position] = DaySpec(specs[position].local_date, state)
                    self.assertIs(self.summarized(self.period, specs).complete, False)
        self.assertIs(self.summarized(RangePeriod("custom", TODAY, TODAY), [DaySpec(TODAY)]).complete, True)

    def test_absent_date_makes_range_incomplete(self) -> None:
        days = week_of_days()
        for gap in range(7):
            with self.subTest(gap=gap):
                specs = [DaySpec(day) for index, day in enumerate(days) if index != gap]
                result = self.summarized(self.period, specs)
                self.assertIs(result.complete, False)
                self.assertEqual(result.absent_dates, (days[gap],))
        self.assertIs(self.summarized(self.period, []).complete, False)

    def test_publication_condition_makes_range_incomplete(self) -> None:
        for name in PUBLICATION_NAMES:
            for position in (0, 6):
                with self.subTest(condition=name, position=position):
                    specs = [DaySpec(day, publication=(name,) if index == position else ())
                             for index, day in enumerate(week_of_days())]
                    self.assertIs(self.summarized(self.period, specs).complete, False)


# ---- R14 ----

class R14PlainValue(unittest.TestCase):
    def test_range_summary_fields_are_exact(self) -> None:
        self.assertTrue(dataclasses.is_dataclass(RangeSummary))
        self.assertTrue(RangeSummary.__dataclass_params__.frozen)
        self.assertEqual([item.name for item in dataclasses.fields(RangeSummary)], RESULT_FIELDS)
        # No encoder, decoder, digest, or range state is defined here. Names imported from elsewhere are not checked.
        defined = [name for name, value in vars(range_summary_contract).items()
                   if not name.startswith("_") and getattr(value, "__module__", None) == range_summary_contract.__name__]
        self.assertIn("summarize_range", defined)
        for name in defined:
            self.assertNotRegex(name.lower(), r"encode|decode|bytes|digest|sha256|watermark|primary_state$")


# ---- R15 ----

def marker_summary(**overrides) -> HealthSummary:
    return build_day_summary(DaySpec(MARKER_DATE, derivation=MARKER_DERIVATION, body_total=MARKER_VALUE, **overrides))


def marker_failure_cases() -> list:
    period = custom(date(1987, 6, 1), MARKER_DATE)
    day = RangeDay(marker_summary(), ())
    earlier = RangeDay(build_day_summary(DaySpec(date(1987, 6, 2), derivation=MARKER_DERIVATION,
                                                 body_total=MARKER_VALUE)), ())
    heart = RangeDay(marker_summary(measure="heart-rate-summary", record_type="heart-rate"), ())
    future = RangeDay(dataclasses.replace(day.summary, header=dataclasses.replace(
        day.summary.header, contract_version="summary-contract@2")), ())
    after = RangeDay(build_day_summary(DaySpec(date(1987, 6, 6), derivation=MARKER_DERIVATION,
                                               body_total=MARKER_VALUE)), ())
    return [
        (PERIOD_INVALID, lambda: preset_period(MARKER_VALUE, MARKER_DATE)),
        (PERIOD_INVALID, lambda: preset_period(7, MARKER_MOMENT)),
        (PERIOD_INVALID, lambda: preset_period(7, HEALTH_MARKER)),
        (PERIOD_INVALID, lambda: last_year_period(MARKER_MOMENT)),
        (PERIOD_INVALID, lambda: last_year_period(HEALTH_MARKER)),
        (PERIOD_INVALID, lambda: calendar_year_period(MARKER_VALUE, MARKER_DATE)),
        (PERIOD_INVALID, lambda: calendar_year_period(1988, MARKER_DATE)),
        (PERIOD_INVALID, lambda: calendar_month_period(1987, MARKER_VALUE, MARKER_DATE)),
        (PERIOD_INVALID, lambda: calendar_month_period(1987, 7, MARKER_DATE)),
        (PERIOD_INVALID, lambda: iso_week_period(1987, MARKER_VALUE, MARKER_DATE)),
        (PERIOD_INVALID, lambda: iso_week_period(1987, 30, MARKER_DATE)),
        (PERIOD_INVALID, lambda: custom_period(MARKER_DATE, date(1987, 6, 4), date(1987, 6, 6))),
        (PERIOD_INVALID, lambda: custom_period(date(1986, 6, 3), MARKER_DATE, MARKER_DATE)),
        (PERIOD_INVALID, lambda: custom_period(MARKER_DATE, date(1987, 6, 6), MARKER_DATE)),
        (PERIOD_INVALID, lambda: custom_period(MARKER_MOMENT, MARKER_MOMENT, MARKER_DATE)),
        (PERIOD_INVALID, lambda: period_dates(custom(MARKER_DATE, date(1987, 6, 4)))),
        (PERIOD_INVALID, lambda: period_dates(RangePeriod(HEALTH_MARKER, MARKER_DATE, MARKER_DATE))),
        (PERIOD_INVALID, lambda: period_dates(RangePeriod("last-7-dates", date(1987, 5, 31), MARKER_DATE))),
        (PERIOD_INVALID, lambda: summarize(RangePeriod("iso-week", MARKER_DATE, MARKER_DATE), [day])),
        (INPUT_INVALID, lambda: summarize(period, [day], measure=HEALTH_MARKER)),
        (INPUT_INVALID, lambda: summarize(period, [day, earlier])),
        (INPUT_INVALID, lambda: summarize(period, [day, day])),
        (INPUT_INVALID, lambda: summarize(period, [RangeDay(day.summary, {HEALTH_MARKER})])),
        (INPUT_INVALID, lambda: summarize(period, [RangeDay(day.summary, HEALTH_MARKER)])),
        (INPUT_INVALID, lambda: summarize(period, [RangeDay(day.summary, [MARKER_VALUE])])),
        (INPUT_INVALID, lambda: summarize(period, HEALTH_MARKER)),
        (INPUT_INVALID, lambda: summarize(period, [after])),
        (INPUT_INVALID, lambda: summarize(period, [(day.summary, ())])),
        (INPUT_INVALID, lambda: summarize(period, [heart])),
        (INPUT_INVALID, lambda: summarize(period, [future])),
        (INPUT_INVALID, lambda: summarize(period, [RangeDay(MARKER_DATE, ())])),
    ]


class R15HealthDataFree(Checks):
    def assert_input_free(self, call, expected: str) -> None:
        # assertRaises clears the traceback; inspect it while it is still attached.
        try:
            call()
        except RangeSummaryFailure as failure:
            self.assertIs(type(failure), RangeSummaryFailure)
            self.assertEqual(failure.args, (expected,))
            self.assertIsNone(failure.__cause__)
            self.assertIsNone(failure.__context__)
            for text in (str(failure), repr(failure), repr(failure.args), repr(vars(failure)),
                         "".join(traceback.format_exception(failure))):
                self.assertFalse(carries_marker(text), text[-2000:])
            first = None
            tb = failure.__traceback__
            while tb is not None:
                if tb.tb_frame.f_code.co_filename.startswith(PACKAGE_DIR):
                    first = first or tb
                    for name, value in tb.tb_frame.f_locals.items():
                        self.assertFalse(carries_marker(value), f"local {name!r} of {tb.tb_frame.f_code.co_name}")
                tb = tb.tb_next
            self.assertIsNotNone(first)
            # Caller frames own their inputs. Format the unit's frames with their locals.
            rendered = "".join(traceback.TracebackException(type(failure), failure, first, capture_locals=True).format())
            self.assertFalse(carries_marker(rendered), rendered[-2000:])
        else:
            self.fail("operation did not raise RangeSummaryFailure")

    def test_failures_carry_only_their_name(self) -> None:
        cases = marker_failure_cases()
        seen = set()
        for index, (expected, call) in enumerate(cases):
            with self.subTest(case=index, context="none"):
                self.assert_input_free(call, expected)
            with self.subTest(case=index, context="ValueError"):
                try:
                    raise ValueError(HEALTH_MARKER, MARKER_DATE)
                except ValueError:
                    self.assert_input_free(call, expected)
            with self.subTest(case=index, context="JSONDecodeError"):
                try:
                    json.loads('{"health":"' + HEALTH_MARKER + '",}')
                except json.JSONDecodeError as caller_error:
                    self.assertIn(HEALTH_MARKER, caller_error.doc)
                    self.assert_input_free(call, expected)
            with self.subTest(case=index, context="SummaryContractFailure"):
                try:
                    raise SummaryContractFailure("summary-input-invalid")
                except SummaryContractFailure:
                    self.assert_input_free(call, expected)
            seen.add(expected)
        self.assertEqual(seen, set(FAILURES))

    def test_summary_contract_failures_are_translated(self) -> None:
        period = custom(date(2026, 11, 22), TODAY)
        spec = DaySpec(date(2026, 11, 23))
        for conditions in (["source-conflict"], ["measured"], [5], [None], "conflicted", None,
                           ("conflicted", "range-input-invalid")):
            with self.subTest(conditions=repr(conditions)):
                try:
                    summarize(period, [make_day(spec, conditions)])
                except Exception as failure:  # noqa: BLE001 - the type is what is checked
                    self.assertIs(type(failure), RangeSummaryFailure)
                    self.assertNotIsInstance(failure, SummaryContractFailure)
                    self.assertEqual(failure.args, (INPUT_INVALID,))
                    self.assertIsNone(failure.__cause__)
                    self.assertIsNone(failure.__context__)
                else:
                    self.fail("invalid publication conditions were accepted")
        # Whatever summary-contract@1 operation the unit calls, its failure becomes range-input-invalid.
        days = [make_day(spec, ("conflicted",)), make_day(DaySpec(TODAY, "partial"))]
        expected = expected_summary(period, [dataclasses.replace(spec, publication=("conflicted",)),
                                             DaySpec(TODAY, "partial")])

        def raising(*args, **kwargs):
            raise SummaryContractFailure("summary-input-invalid")

        patched = []
        for module in (summary_contract, range_summary_contract):
            for name in ("check_publication_conditions", "coverage_of", "primary_state", "build_summary",
                         "decode_summary"):
                if hasattr(module, name):
                    patched.append((module, name, getattr(module, name)))
                    setattr(module, name, raising)
        try:
            try:
                result = summarize(period, days)
            except Exception as failure:  # noqa: BLE001 - the type is what is checked
                self.assertIs(type(failure), RangeSummaryFailure)
                self.assertEqual(failure.args, (INPUT_INVALID,))
                self.assertIsNone(failure.__cause__)
                self.assertIsNone(failure.__context__)
            else:
                self.assertEqual(result, expected)
        finally:
            for module, name, original in patched:
                setattr(module, name, original)


if __name__ == "__main__":
    unittest.main()
