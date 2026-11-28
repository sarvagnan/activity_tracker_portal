# Draft acceptance pack — `range-summary-contract@1`

**Status:** Draft approved by the Operator on 2026-11-28, including the 15 decisions below as recommended except Decision 4, where the Operator chose the rolling year; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `range-summary-contract` Governed Scope contract, version 1, in `governance/scopes/range-summary-contract.1.json`. It is a shared contract. It depends on the published `summary-contract@1` and nothing else.

## What this unit does

`summary-contract@1` defines one Health Summary per measure, record type, and local date, and leaves ranges to this unit (its Decision 2). Four compose units will turn daily summaries into range results, one per measure. This unit is the part they share, as `summary-contract@1` is for the derive units.

Summary views offer "inclusive 7-, 30-, and 90-local-date windows through today, a calendar-year window preserving leap-day behavior, and inclusive custom date ranges" (ADR 0019). At Gate A the Operator read the one-year window as a rolling year through today (Decision 4). Weekly, monthly, and custom views "compose attributed local-date results at query time", keep "usable measured contributions alongside counts for every non-measured state and evidence condition", and "never claim an incomplete subtotal is complete" (ADR 0023). This unit defines:
- **periods**: the inclusive first and last local date of each preset, including the rolling one-year preset, and of each calendar year, calendar month, ISO week, and custom range. No period passes the caller's today;
- **input checks**: the daily summaries given for a period belong to it, in order, once each;
- **usable dates**: which dates have a value a compose unit may use;
- **the range result**: dates with no summary, counts of dates by every primary state, evidence condition, and publication condition, summed coverage, and one `complete` flag that is never true for an incomplete range.

It is a pure function over the daily summaries the caller gives it. `operational-store@6` has no presentation reads; those arrive in `@9`, and a later unit will use them to call this one.

## What it does not do

- It computes no measure's aggregate. The Steps sum, the Heart Rate sample population, Sleep durations, and the Weight point series belong to the compose units (Decision 1). It never reads a summary body.
- It does not define or change the daily summary, its primary-state rule, its coverage, or any vocabulary. It uses `summary-contract@1`'s.
- It does not decide when a publication condition applies. The caller attaches them when it reads a summary (`summary-contract@1` Decision 7).
- It reads no clock, store, or file. It does not decide which zone makes a date "today", which range a view opens with, or how anything is drawn.
- It stores nothing. A range result is computed when asked for and then discarded (ADR 0023).

## Interface

The module `activity_tracker_portal.range_summary_contract` provides frozen dataclasses, eight functions, one exception, and constants. It reads no clock, store, file, or network.

**Constants.**
- `CONTRACT_VERSION = "range-summary-contract@1"`.
- `PRESET_LENGTHS = (7, 30, 90)`.
- `PERIOD_KINDS = ("last-7-dates", "last-30-dates", "last-90-dates", "last-year", "calendar-year", "calendar-month", "iso-week", "custom")`.
- `MAX_PERIOD_DATES = 366`.
- `MEASURES`, `PRIMARY_STATES`, `EVIDENCE_CONDITIONS`, and `PUBLICATION_CONDITIONS`: re-exported from `summary_contract`, the very same objects.
- `FAILURES = ("range-period-invalid", "range-input-invalid")`.

**Values.**
- `RangePeriod(kind, first_date, last_date)`: a kind from `PERIOD_KINDS` and two `date`s, both inclusive.
- `RangeDay(summary, publication_conditions)`: a `summary_contract.HealthSummary` and the publication conditions the caller attached to it.
- `RangeSummary`, with exactly these fields:
  - `contract_version`: `CONTRACT_VERSION`;
  - `measure` and `record_type`;
  - `period`: the `RangePeriod`;
  - `date_count`: the number of dates in the period;
  - `valued_dates`: the dates whose summary has a value, ascending;
  - `absent_dates`: the dates with no summary, ascending;
  - `primary_state_counts`, `evidence_condition_counts`, and `publication_condition_counts`: tuples of `(name, count)` pairs, one per name of the vocabulary, in its order;
  - `coverage`: a `summary_contract.Coverage` of summed counts;
  - `processor_versions` and `derivation_versions`: the distinct versions seen, sorted;
  - `complete`: a `bool`.

**Operations.**
- `preset_period(length, today) -> RangePeriod`.
- `last_year_period(today) -> RangePeriod`.
- `calendar_year_period(year, today) -> RangePeriod`.
- `calendar_month_period(year, month, today) -> RangePeriod`.
- `iso_week_period(iso_year, iso_week, today) -> RangePeriod`.
- `custom_period(first_date, last_date, today) -> RangePeriod`.
- `period_dates(period) -> tuple[date, ...]`.
- `summarize_range(*, measure, record_type, period, days) -> RangeSummary`.
- `RangeSummaryFailure(failure)`: its `args` are exactly `(failure,)`.

## Requirements

### R1 — Governed Scope contract

The contract has the shared shape, kind `shared`, and version 1. It depends on `summary-contract` at version 1 and nothing else. Its named failures are `range-period-invalid`, a refusal, and `range-input-invalid`, an invariant refusal (Decision 14). It has no observable effects, and its effects declaration is empty. It does not contradict the scope-map row.

Proposed tests: `test_contract_shape_and_dependencies`, `test_effects_declaration_is_empty`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — Pure and deterministic

- Every operation reads only its arguments. It reads no clock, store, file, network, environment, or host timezone database, writes nothing, and logs nothing.
- The same arguments give equal results, whatever the process, `TZ`, locale, or hash seed.
- All dataclasses are frozen.

Proposed tests: `test_same_input_gives_equal_results`, `test_result_ignores_host_timezone_locale_and_hash_seed`, `test_operations_read_no_clock_or_io`, `test_values_are_frozen`.

### R3 — Constants and vocabularies

- The constants hold exactly the values in the Interface, in that order.
- `MEASURES`, `PRIMARY_STATES`, `EVIDENCE_CONDITIONS`, and `PUBLICATION_CONDITIONS` are the objects `summary_contract` publishes. This unit adds no state or condition name.

Proposed tests: `test_constants_are_exact`, `test_shared_vocabularies_are_summary_contracts`.

### R4 — Presets

`preset_period(length, today)` returns the `length` dates ending at `today`: from `today` minus `length − 1` days through `today`. Its kind is `last-7-dates`, `last-30-dates`, or `last-90-dates`.
- With today 2026-11-28: 7 gives 2026-11-22 to 2026-11-28, 30 gives 2026-10-30 to 2026-11-28, and 90 gives 2026-08-31 to 2026-11-28.
- A leap day is an ordinary date. With today 2028-03-01, 7 gives 2028-02-24 to 2028-03-01, which includes 2028-02-29.
- `range-period-invalid` for any other length (including `True`), a `today` that is not a `date` or is a `datetime`, or a first date before 0001-01-01.

`last_year_period(today)` is the one-year preset: the rolling year through `today`, of kind `last-year` (Decision 4).
- Take the same month and day one year before `today`, reading 29 February as 28 February. The period runs from the day after that date through `today`.
- It has 365 dates, or 366 when a 29 February falls inside it.
- A common year: today 2026-11-28 gives 2025-11-29 to 2026-11-28 (365 dates).
- A period containing 29 February: today 2028-03-01, the day after a leap day, gives 2027-03-02 to 2028-03-01 (366).
- Today is 29 February: today 2028-02-29 gives 2027-03-01 to 2028-02-29 (366).
- 28 February in the year after a leap year: today 2029-02-28 gives 2028-02-29 to 2029-02-28 (366).
- 1 March in the year after a leap year: today 2029-03-01 gives 2028-03-02 to 2029-03-01 (365).
- `range-period-invalid` for a `today` that is not a `date` or is a `datetime`, or a `today` in year 1, which has no year before it.

Proposed tests: `test_presets_end_today_with_exact_length`, `test_presets_cross_month_year_and_leap_day`, `test_other_preset_lengths_are_refused`, `test_today_must_be_a_date`, `test_last_year_in_a_common_year_has_365_dates`, `test_last_year_containing_29_february_has_366_dates`, `test_last_year_on_29_february_starts_1_march`, `test_last_year_on_1_march_after_a_leap_day`, `test_last_year_on_28_february_after_a_leap_year`.

### R5 — Calendar years

`calendar_year_period(year, today)` returns 1 January to 31 December of `year`. It is an ordinary builder, like the calendar month and ISO week builders (Decision 5), and not the one-year preset, which is the rolling year of R4 (Decision 4).
- When `year` is today's year, the period ends at `today`. On 1 January it has one date.
- A whole leap year has 366 dates, including 29 February. Other years have 365. With today 2026-11-28, 2026 gives 332 dates, 2027 is refused, and 2028 is refused.
- `range-period-invalid` for a year after today's year, a year outside 1 to 9999, or a non-`int` year.

Proposed tests: `test_past_calendar_year_is_whole`, `test_leap_year_has_366_dates`, `test_current_calendar_year_ends_today`, `test_future_calendar_year_is_refused`.

### R6 — Calendar months and ISO weeks

Both follow ADR 0023 and the R5 rule for today (Decision 5).
- `calendar_month_period(year, month, today)` returns the first to the last date of the month. February has 29 dates in a leap year.
- `iso_week_period(iso_year, iso_week, today)` returns Monday to Sunday of that ISO week. A week may span two calendar years: 2026-W53 is 2026-12-28 to 2027-01-03. Week 53 exists only in ISO years that have one; 2027-W53 is refused.
- A month or week that contains `today` ends at `today`: with today 2026-11-28, 2026-W48 is 2026-11-23 to 2026-11-28.
- `range-period-invalid` for a month or week that starts after `today`, a month outside 1 to 12, a week that does not exist, or non-`int` arguments.

Proposed tests: `test_calendar_month_boundaries_include_leap_day`, `test_iso_week_is_monday_to_sunday_across_years`, `test_week_53_exists_only_in_long_years`, `test_current_month_and_week_end_today`, `test_future_month_and_week_are_refused`.

### R7 — Custom ranges

`custom_period(first_date, last_date, today)` returns that inclusive range, of kind `custom` (Decision 6).
- `first_date` is at or before `last_date`, which is at or before `today`. One date is a valid range.
- At most `MAX_PERIOD_DATES` (366) dates.
- `range-period-invalid` for a reversed range, a range ending after `today`, a range longer than 366 dates, or arguments that are not `date`s or are `datetime`s.

Proposed tests: `test_custom_range_is_inclusive`, `test_custom_range_may_be_one_date`, `test_custom_range_after_today_is_refused`, `test_reversed_custom_range_is_refused`, `test_custom_range_longer_than_366_dates_is_refused`.

### R8 — Periods given back to this unit

- `period_dates(period)` returns every date of the period, ascending. Its length is the `date_count` that `summarize_range` reports.
- `period_dates` and `summarize_range` refuse, with `range-period-invalid`, a period that no builder could have produced for some today: not a `RangePeriod`, an unknown kind, a first date after the last, more than 366 dates, or dates that disagree with the kind. Examples: a `last-30-dates` period of 29 dates; a `last-year` period that does not start on the day after its last date's month and day one year earlier, with 29 February read as 28 February; a `calendar-year` that does not start on 1 January or ends in another year; a `calendar-month` that does not start on the 1st; an `iso-week` that does not start on a Monday or has more than 7 dates.

Proposed tests: `test_period_dates_are_inclusive_and_ascending`, `test_inconsistent_period_is_refused`.

### R9 — Daily summaries given for a range

`summarize_range` takes `days`, any iterable of `RangeDay` other than a string (Decision 8). Each is checked:
- `summary` is a `summary_contract.HealthSummary` whose header's `contract_version` is `summary-contract@1`;
- its header's `measure` and `record_type` equal the range's, and that pair is listed in `MEASURES`;
- its `local_date` is in the period;
- local dates are strictly ascending, so out-of-order and repeated dates are refused;
- `publication_conditions` is a collection of names from `PUBLICATION_CONDITIONS`, not a bare string. Repeated names count once.

Any violation is `range-input-invalid`. A date with no `RangeDay` is allowed: it is absent (R10). This unit reads only header fields. It does not decode or re-check the summary's bytes, and never reads the body.

Proposed tests: `test_days_must_be_health_summaries_of_the_range_key`, `test_unknown_measure_pair_is_refused`, `test_days_outside_the_period_are_refused`, `test_unordered_or_repeated_dates_are_refused`, `test_publication_conditions_are_checked`, `test_gaps_are_allowed`, `test_bodies_are_not_read`.

### R10 — Dates, states, and usable values

- `date_count` is the number of dates in the period.
- `absent_dates` lists the period's dates that have no summary (Decision 7). An absent date is not `missing`; a `missing` summary is not absent.
- `primary_state_counts` counts the given summaries by primary state, one pair per name of `PRIMARY_STATES`.
- `valued_dates` lists the dates whose summary is `measured` or `partial`, whatever its conditions (Decision 2).
- The counts add up: the state counts plus the absent dates equal `date_count`, and `valued_dates` has as many dates as `measured` and `partial` together.
- With no `days`, every date is absent.

Proposed tests: `test_absent_dates_are_not_missing`, `test_primary_states_are_counted_by_date`, `test_valued_dates_are_measured_or_partial`, `test_counts_add_up_to_the_period`, `test_range_with_no_summaries_is_all_absent`.

### R11 — Condition counts

- `evidence_condition_counts` gives, for each name in `EVIDENCE_CONDITIONS`, the number of given summaries whose header carries it.
- `publication_condition_counts` gives, for each name in `PUBLICATION_CONDITIONS`, the number of `RangeDay`s that carry it. An absent date carries none (Decision 15).
- Every name appears, with 0 when no date carries it. One date can count under several names.
- Conditions never change the state counts or `valued_dates`.

Proposed tests: `test_evidence_conditions_are_counted_by_date`, `test_publication_conditions_are_counted_by_date`, `test_every_name_is_present_with_zero_counts`, `test_conditions_do_not_change_usable_dates`.

### R12 — Coverage and versions

- `coverage` sums each standing's count over the given summaries. It counts record-to-date attributions: a record attributed to two dates counts twice (Decision 10). With no summaries, every count is 0.
- `processor_versions` and `derivation_versions` list the distinct versions in the given headers, sorted by code point, and are empty with no summaries. Mixed versions are reported, not refused (Decision 9).

Proposed tests: `test_coverage_is_summed_across_dates`, `test_mixed_versions_are_reported`.

### R13 — Completeness

`complete` is `True` exactly when every date of the period has a summary, every summary is `measured`, and no date carries a publication condition (Decision 11). Evidence conditions do not affect it: under `summary-contract@1` R4 and R7, a `measured` summary can only carry `duplicate-grouped` or `inferred-attribution`. There is no range-level primary state.

Proposed tests: `test_complete_requires_every_date_measured`, `test_absent_date_makes_range_incomplete`, `test_publication_condition_makes_range_incomplete`.

### R14 — A plain value

`RangeSummary` has exactly the Interface's fields, in that order, and no primary state, bytes, or digest. This unit has no encoder or decoder (Decision 12).

Proposed tests: `test_range_summary_fields_are_exact`.

### R15 — Health-data-free failures

- Every failure is a `RangeSummaryFailure` whose `args` are exactly `(failure,)`, with no `__cause__` or `__context__`, and whose traceback carries no input value.
- The period builders raise only `range-period-invalid`. `summarize_range` raises `range-period-invalid` for its period and `range-input-invalid` for anything else.
- A `SummaryContractFailure` raised inside this unit, such as from checking publication conditions, becomes `range-input-invalid`.

Proposed tests: `test_failures_carry_only_their_name`, `test_summary_contract_failures_are_translated`.

## Decisions for the Operator

1. **Where this unit stops and the compose units start.**
   - **Recommended:** this unit returns the `RangeSummary`: the period, the dates with a value, the dates with no summary, the counts, the summed coverage, and `complete`. Each compose unit calls `summarize_range` on the same days, then reads the bodies of the `valued_dates` to compute its own aggregate and extra counts: the safe known-Steps sum, the Heart Rate sample-weighted mean, count, minimum and maximum, the Sleep durations and session and overlap counts, and the Weight daily points and latest point. Its result is this `RangeSummary` plus its aggregate. This unit never reads a body.
   - Alternative: this unit also takes each compose unit's aggregate and wraps it with the shared fields, as `summary-contract@1` does with a body. That needs an encoding for a value that is never stored (Decision 12).
   - Alternative: this unit only defines periods, and each compose unit counts states and conditions itself. The counting would be written four times and could drift.
   - Consequence: what part of a `partial` day's body is safe to use is each compose unit's decision. ADR 0041 already sets it for Steps ("only explicitly safe known-Steps contributions").

2. **Which dates have a usable value.**
   - **Recommended:** a date has a usable value exactly when its summary is `measured` or `partial`, whatever its conditions. A `partial` day still has a value from its contributing records, and ADR 0041 keeps usable contributions "rather than ... discarding valid days". A result kept visible with a publication condition, such as `refresh-failed` or `conflicted`, is the prior trusted result that ADR 0030 allows when qualified. Its conditions are counted, and the range is not `complete` (Decision 11).
   - Alternative: `measured` only. Partial days' safe contributions would be dropped.
   - Alternative: leave out any date with a publication condition. An ordinary refresh would then empty a range, even though those results are still the published ones.

3. **Who supplies today.**
   - **Recommended:** always the caller, as a `date`. This unit reads no clock and has no default. Which zone or rule decides the local date called "today" is for the presentation units, which need the same answer for Overview's "today's Steps". No ADR fixes it yet.
   - Alternative: read the clock in the Fallback Timezone Policy's zone. That needs the policy and a clock in a pure unit, and cannot be tested deterministically.
   - Alternative: default to `date.today()`. That uses the server's zone, which ADR 0025 rules out for calendar attribution.
   - Consequence: evidence attributed to a local date after the caller's today, for example from a source with an eastern experienced offset, is in no range until that date arrives.

4. **What "one year" means.** The scope-map row said "one year follows calendar-year and leap-day behavior". ADR 0019 says "a calendar-year window preserving leap-day behavior". These can be read in more than one way.
   - **Operator's choice, 2026-11-28: a rolling year through today**, like the other presets. `last_year_period(today)` runs from the day after the same date one year earlier, through today: 365 dates, or 366 when a 29 February falls inside it. When today is 29 February, "one year earlier" is read as 28 February. Examples: today 2028-02-29 gives 2027-03-01 to 2028-02-29 (366 dates); 2029-02-28 gives 2028-02-29 to 2029-02-28 (366); 2029-03-01 gives 2028-03-02 to 2029-03-01 (365). R4 defines it.
   - Consequence: `calendar_year_period` stays, as an ordinary builder beside the month and week builders (Decision 5), but it is not the one-year preset. The scope-map row and the Operator surfaces paragraph now say "rolling" year. ADR 0019 still says "a calendar-year window"; it is not edited here (Residuals).
   - The alternatives the Operator considered, kept as history:
     - The drafter's recommendation: a calendar year, 1 January to 31 December, with the current year ending at today, so the Portal's preset would be the current year to date. Reason given: ADR 0019 and the scope map's Operator surfaces both said "calendar-year", and ADR 0023 uses "calendar boundaries" for months in the same sense.
     - Only the current calendar year to date, with no past years.
     - Always the last 365 dates. Simplest, but ignores leap days, which ADR 0019 says to preserve.

5. **ISO weeks and calendar months.**
   - **Recommended:** include builders for both. ADR 0023 says weekly, monthly, and custom views compose at query time, with ISO Monday-to-Sunday weeks and calendar months. Two small builders fix those boundaries in one place, with the same end-at-today rule as calendar years. No v1 view has to offer them.
   - Alternative: leave them out until a presentation unit offers weekly or monthly views, and add them in a later version. This unit is smaller, but ADR 0023's boundaries stay unpinned until then.

6. **Custom ranges.**
   - **Recommended:** the first date is at or before the last, the last is at or before today, and the range has at most 366 dates, the size of the largest calendar year and of the largest rolling year. Any calendar year fits. Anything else is refused with `range-period-invalid`, which the Portal can show. The limit keeps one range to at most 366 daily summaries per measure.
   - Alternative: no limit. The function stays pure, but the reads and pages that feed it become unbounded.
   - Alternative: a larger limit, such as 3,660 dates (ten years). A later version can raise the limit without breaking anything.
   - Alternative: cut a range that ends after today back to today, instead of refusing it. That hides the Operator's mistake.
   - Alternative: allow dates after today and count them as absent. Absent would then mean both "not built" and "not happened yet".

7. **A date with no summary is absent, not `missing`.**
   - **Recommended:** keep them apart. `missing` is a derivation's statement: it ran at a watermark and found nothing attributed to the day. A date with no summary has no statement at all: it was not built yet, it is before the first publication (ADR 0045), or the caller did not give it. Absent dates are listed in `absent_dates`, not counted as a primary state. Neither is ever zero.
   - Alternative: count absent dates as `missing`. One fewer number, but it claims a derivation that never ran.
   - Alternative: refuse a range with gaps. Callers would have to invent summaries, and no range could be shown while the first summaries are being built.

8. **What the caller gives this unit.**
   - **Recommended:** a sequence of `RangeDay(summary, publication_conditions)`. Each `summary` is a `HealthSummary` that `summary-contract@1` built or decoded. The days are strictly ascending by date, each date at most once, all inside the period and of the range's measure and record type. Gaps are allowed (Decision 7). This unit reads only header fields. It does not decode the bytes again, because `build_summary` and `decode_summary` already checked them.
   - Alternative: header facts only, as a small record of date, state, conditions, and coverage. A caller could then pass facts that no summary has.
   - Alternative: the stored bytes, decoded here. Every range would parse up to 366 headers and manifests. Whether presentation reads decode stored summaries is for `operational-store@9` and the presentation units.
   - Alternative: accept any order and sort. That hides a caller defect, and reading in date order is easy for the caller.
   - Consequence: a hand-built `HealthSummary` with inconsistent header fields is not caught here (Residuals).

9. **Days built under different processor or derivation versions.**
   - **Recommended:** report them, do not refuse. `processor_versions` and `derivation_versions` list the distinct versions. ADR 0042 says ordinary views do not mix versions, and the activation units enforce that. But a prior trusted result kept with `conflicted` (ADR 0041) may come from an earlier generation, and refusing would blank the whole range.
   - Alternative: refuse mixed versions with `range-input-invalid`. An activation defect would fail loudly, but so would the case above.
   - Alternative: do not look at versions. Mixing would be invisible.

10. **Coverage across a range.**
    - **Recommended:** sum each standing's count over the given summaries. The sums count record-to-date attributions, and say so: a record attributed to two dates counts twice.
    - Alternative: count distinct records by reading every evidence manifest. Exact, but it parses manifests on every query.
    - Alternative: no coverage in the range result, only date counts. The range would no longer show how much evidence could not be used.

11. **One `complete` flag, and no range-level state.**
    - **Recommended:** `complete` is true exactly when every date has a summary, every summary is `measured`, and no date has a publication condition. There is no single state for the range, because ADR 0041 rules out "collapsing uncertainty into one arbitrary worst state".
    - Alternative: no flag. Each compose unit and view decides from the counts, and four places could disagree about what ADR 0023 calls "complete".
    - Alternative: a flag that ignores publication conditions. A range would read complete while showing results that newer evidence is waiting to replace.
    - Alternative: a range primary state, from `summary-contract@1`'s rule applied to the worst day. ADR 0041 rules it out.

12. **Plain values, not bytes.**
    - **Recommended:** `RangeSummary` is a frozen value in memory, with no canonical bytes, digest, or decoder. ADR 0023 composes ranges "at query time". Nothing stores, exports, or signs a range result. (ADR 0027 says nothing about ranges.)
    - Alternative: RFC 8785 bytes and a digest, as `summary-contract@1` has. They are useful only if a later unit caches or exports ranges, and a later version can add them then.

13. **No combined Data Watermark for a range.**
    - **Recommended:** the range result carries no watermark of its own. Each daily summary keeps its own, and the presentation units decide how a range view shows them (ADR 0030).
    - Alternative: the earliest incorporated receipt time across the given days, as "every date incorporates evidence received at least up to this time". Easy to add, but it chooses a presentation meaning here.
    - Alternative: copy every day's watermark into the range result. That repeats the input.

14. **Two failure names.**
    - **Recommended:** `range-period-invalid`, a refusal, because the Operator can ask for a bad custom range and the Portal should say so; and `range-input-invalid`, an invariant refusal, because only a defective caller passes bad days. Neither carries a date.
    - Alternative: one name for both. A view could not tell a bad request from a defect.
    - Alternative: one name per rule, such as reversed, too long, or after today. More vocabulary for little gain: the view can explain the problem from the request it made.

15. **An absent date carries no publication condition.**
    - **Recommended:** publication conditions attach only to a given summary. Under `summary-contract@1` Decision 7 they describe a published result. A date with nothing published is simply absent here. Whether a view calls it "initial pending" or "initial failed" (ADR 0043) is for the presentation units, from store state.
    - Alternative: let an absent date carry `newer-evidence-pending` or `refresh-failed`, so a range counts dates that are pending or failed before their first publication. That stretches Decision 7 to dates with no published result.

## Ground rules

- **Data.** Python only, standard library plus `summary_contract`. Tests build daily summaries with `summary_contract.build_summary` from synthetic evidence and bodies. All identities, dates, and times are synthetic. No store is opened.
- **Dates.** Tests use fixed `today` values, never the clock, including 2026-11-28, leap days, 1 January, 31 December, and ISO years with 52 and 53 weeks.
- **Markers.** Synthetic markers in body values and versions show that no failure carries them.
- **Unbound inputs, reviewed rather than frozen.** The binding is the contract, this spec, the suite, the runner, and the effects declaration. These stay outside it:
  - `docs/governance/scope-map.md`, read by R1's scope-map test;
  - the published code, contract, and pack of `summary-contract@1`, which its own binding freezes;
  - the execution environment: `uv`, `pyproject.toml`, `uv.lock`, and the managed Python;
  - `tests/governance/test_range_summary_contract_contract.py`, which the governance suite runs and this runner does not.

## Freeze prerequisites

After Gate A, the executable pack adds:
- the suite `tests/acceptance/test_range_summary_contract_1.py`: 58 tests, one per proposed name in R1–R15, grouped by requirement;
- the runner `tests/acceptance/run_range_summary_contract_1.sh`, which runs only this suite;
- no fixture files;
- the Interface stub `src/activity_tracker_portal/range_summary_contract.py`:
  - the real constants, because they are what the Operator approves. `MEASURES`, `PRIMARY_STATES`, `EVIDENCE_CONDITIONS`, and `PUBLICATION_CONDITIONS` are imported from `summary_contract`, so they are the same objects;
  - the real frozen dataclasses `RangePeriod`, `RangeDay`, and `RangeSummary`, with the Interface's fields in its order, and the real `RangeSummaryFailure`. Like `SummaryContractFailure`, it also keeps its name in a `failure` attribute;
  - the eight operations, each raising `NotImplementedError("range-summary-contract@1")`.

**RED, against the stub.**
- 7 tests pass, because they read only the contract, the effects declaration, the scope map, the constants, or the dataclass fields: the three R1 tests, the two R3 tests, `test_values_are_frozen`, and `test_range_summary_fields_are_exact`.
- The other 51 fail, with 390 failing entries, counting subtests: 382 errors and 8 failures. Every failure is the stub's `NotImplementedError`. The 8 failures are the subtests of `test_summary_contract_failures_are_translated`, which catches any exception and finds a `NotImplementedError` where it expects a `RangeSummaryFailure`.
- `tests/unit` (156 tests), `tests/governance` (73), and the bound runner of `summary-contract@1` (60) still pass.

**How the suite checks periods.** The suite has its own calendar model. It counts days from 0001-01-01, a Monday, with integers, and has its own leap-year rule, month lengths, ISO week rule, and builders. It never uses `datetime` arithmetic. Every builder is compared with the model on:
- the worked examples in R4–R7;
- every date of 1900, 2026, 2027, 2100, and 2101, and of the three years starting 1903, 1999, 2027, and 2399, for the rolling year;
- every 29 February, and 28 February and 1 March of every year from 2 to 9999, for the rolling year;
- every calendar year from 1 to 9999; week 1, the last week, and week 53 of every ISO year from 1 to 9999; every month of 42 boundary years and 200 seeded years;
- seeded generated dates. The seeds are fixed. The generator picks the edge dates 1 and 2 January, 4 January, 28 and 29 February, 1 March, 28 and 31 December of the boundary years 1–5, 99–101, 399–401, 1599–1601, 1899–1901, 1903–1905, 1999–2001, 2004, 2015, 2020, 2024–2030, 2032, 2099–2101, 2399–2401, 9998, and 9999, and then dates biased towards those years.
R8's consistency rule is checked in both directions. Periods from each builder, including all three presets, are shifted by a day, a week, or a random amount. Both `period_dates` and `summarize_range` must accept exactly the shifted periods the model says some builder could give for some today, and give the model's dates. Explicit cases include a `last-90-dates` period of 89 dates and one of 91.

**How the suite checks range results.** Every daily summary is a real Health Summary from `summary_contract.build_summary`, built from synthetic evidence. Each fixture first checks that `summary-contract@1` gave it the state and evidence conditions it asked for. The expected `RangeSummary` is computed from the fixture's own facts, never from the headers, and compared field by field, including value types. Ranges with seeded random states, conditions, publication conditions, and versions cover every period kind, up to 366 dates.

**Refinements the tests pin beyond the approved text.** These are the drafter's choices, for the approver to confirm or escalate. None changes approved semantics, and no test needed a change to them.
- Argument domains:
  - *settles*: wherever an `int` is expected, a `bool` is refused: a year, a month, an ISO year, and an ISO week, as well as the preset length the approved text names. A `float` such as `7.0` and a numeric string are refused too.
  - `today` and a custom range's two dates are refused when they are `datetime`s, strings, ordinals, tuples, or `None` (approved). Subclasses of `date` other than `datetime` are not tested.
  - *settles*: the last ISO week of the date range is valid. With today 9999-12-31, 9999-W52 is 9999-12-27 to 9999-12-31. Its Sunday would fall after `date.max`, so an implementation must not compute it before cutting the week at today.
- Result types:
  - *settles*: `valued_dates`, `absent_dates`, the three count fields, and the two version fields are tuples. Each count is a `(str, int)` tuple, `date_count` is an `int`, `complete` is exactly a `bool`, and `coverage` is a `Coverage` of `int`s.
  - `period_dates` returns a tuple of `date`s, as the Interface says.
- What `summarize_range` accepts:
  - `days` may be any iterable other than a string, including a generator or a set, and is read once.
  - *settles*: each item must be a `RangeDay`. A `(summary, conditions)` tuple, a bare `HealthSummary`, or `None` is `range-input-invalid`. So is a `days` that is a single `RangeDay`, `None`, a number, or `""`.
  - *settles*: the range's measure and record type are checked even when `days` is empty.
  - *settles*: `publication_conditions` may be a list, tuple, set, or frozenset. A bare string, including `""`, `bytes`, `None`, and a non-string name are refused.
  - Not pinned: whether `bytes` given as `days` is refused.
- Bytes are never used. *settles*: the test replaces `header_bytes`, `evidence_bytes`, and `body_bytes` with stand-ins that fail on any use, such as comparison, hashing, length, iteration, or truth value, and expects the same result. So an implementation must not compare or hash whole `HealthSummary` values, which would compare their bytes. Bodies that are not JSON, and different bodies under the same states, also give the same result.
- Failures:
  - `args` are exactly the name. `__cause__` and `__context__` are `None`, also when the call is made inside a caller's `except` block handling a `ValueError`, a `json.JSONDecodeError` whose document holds input, or a `SummaryContractFailure`.
  - The failure's `str`, `repr`, `args`, attributes, and formatted traceback carry no marker. Its `args` and attributes are also walked structurally, not only through their `repr`.
  - Every traceback frame after the caller's own frames (those in the suite's file) is checked, not only the unit's. No local is, or holds, a marker input: the marker string, the marker integer, the marker date, or a `datetime` on that date. That includes a `RangeDay` or `HealthSummary` built with a marker. The traceback from the first such frame, formatted with `TracebackException(capture_locals=True)`, carries no marker either. Unlike `summary-contract@1`'s unbound regression, the suite does not pin how many frames remain.

  "Holds" means what the suite's walker can reach. The walker visits each object once, so cycles end, and stops 16 levels deep. It opens:
  - containers and dict views, dataclasses, and objects with a `__dict__` or `__slots__`;
  - exceptions, through their arguments, context, cause, and traceback; tracebacks and frames, through their locals;
  - generators and coroutines, through their frame's locals;
  - functions, through their defaults, keyword defaults, and closure cells; and cells;
  - bound methods, built-in bound methods, and method wrappers, through `__self__`;
  - `functools.partial` and weak references;
  - built-in iterators whose `__reduce__` names what they iterate, such as list, tuple, and dict iterators, `map`, `zip`, `enumerate`, `filter`, `reversed`, and callable iterators.

  It cannot open itertools iterators, which lost `__reduce__` in Python 3.14, or other C-level objects that expose nothing. Those are a residual.
  - `summarize_range` raises `range-period-invalid` for an invalid period with otherwise valid arguments, and `range-input-invalid` for anything else. Not pinned: which failure wins when one call breaks both.
  - A `SummaryContractFailure` becomes `range-input-invalid`. Invalid publication conditions never raise `SummaryContractFailure`. The suite also replaces the `summary_contract` operations, and any copies of them in this module, with functions that raise `SummaryContractFailure`. The call must then either give the expected result or raise `range-input-invalid`.
  - Not pinned: missing or extra arguments may raise Python's own `TypeError`.
- R14: no public name defined in the module, as opposed to imported into it, contains `encode`, `decode`, `bytes`, `digest`, `sha256`, or `watermark`, or ends in `primary_state`.
- The purity test reuses `summary-contract@1`'s design as it stood after Gate B round 3:
  - It runs in a subprocess with an audit hook. It first imports the standard modules an implementation commonly needs and the published `summary_contract`, then the unit.
  - At any time, it flags a write open, an open under the host timezone database, and socket, subprocess, SQLite, and file-system change events. The only write allowed is the import system writing a `.pyc` bytecode cache.
  - The daily summaries are built before the guarded phase. The operations then run twice, both runs are checked, and their results must be equal.
  - An `open` (by path or by descriptor), a directory listing, or an `os.stat`, `os.lstat`, `os.fstat`, or `os.access` call is allowed only while the import system's `_find_and_load` or `_load` in `<frozen importlib._bootstrap>` is on the stack. CPython raises no audit event for the four metadata calls, so the test wraps them directly, with a reentrancy flag for its own path checks.
  - It flags any environment read outside module loading, any log record, and anything written to stderr. Where the platform has a bytes environment (`os.supports_bytes_environ`), `os.environb` is guarded as well as `os.environ`, so `os.getenv` and `os.getenvb` are both covered. Both are restored afterwards.
  - It replaces the Python-level clock functions with guards. Each guard records the read before it raises, and the test requires the record to be empty, so a failure boundary that turns the guard's error into a named failure still fails the test. The scenario keeps its invalid calls.
  - Reads that raise no audit event, such as `date.today()`, and reads a module makes in its own top-level code while being imported, stay residuals.
- The determinism test runs the same scenario in three processes with different `TZ`, locale, and `PYTHONHASHSEED` settings.
- Timing: no test asserts a duration. Each subprocess has a 300 s limit, far above the reference's run of under 1 s. The whole suite runs in about 4 s on the reference.

**Passability.** A throwaway reference implementation, kept outside the repository, passes all 58 tests. So does Sol's independent implementation, run unchanged against the revised suite, by both the `/tmp` and `/private/tmp` spellings of its path. Of 100 incorrect implementations, 99 fail at least one test. The one that passes keeps the input in an `itertools.chain` in the raising frame. The walker cannot open it, so this is a residual.

Three mutants turned out equivalent to the reference and were replaced with ones that really break the rule:
- one accepted `7.0` as a length, but `date.fromordinal` then refused it anyway;
- one changed a boundary's default failure name, but no exception could reach that default;
- one closed the coroutine that held the input, which releases its frame.

The 99 cover at least:
- a preset of `length ± 1` dates, or one that ends yesterday, and a `7.0` length accepted;
- a rolling year that starts on the same date one year earlier (366 or 367 dates), is always 365 dates, reads 29 February as 1 March, or starts a day early when today is 28 or 29 February;
- a current calendar year, month, or week that runs past today, a leap year of 365 dates, and a future year accepted;
- an ISO week that starts on Sunday, a week 53 accepted in a 52-week ISO year, week 1 counted from the Monday before 1 January, and a Sunday computed past `date.max`;
- February with 28 dates in a leap year;
- a custom range that is reversed, ends after today, or has 367 dates, accepted;
- `bool`, `datetime`, and `float` arguments accepted;
- a period whose dates disagree with its kind, accepted, for each kind, including a `last-90-dates` period of 1 to 90 dates, and in `summarize_range` alone; an unknown kind accepted;
- an invalid period in `summarize_range` reported as `range-input-invalid`, and a builder raising `range-input-invalid`;
- absent dates counted as `missing`, or `missing` summaries counted as absent;
- `partial` dates left out of `valued_dates`, `failed` dates put in, or dates with a publication condition left out;
- out-of-order, repeated, out-of-period, other-measure, and other-record-type days accepted, a summary of another `summary-contract` version accepted, and a tuple accepted as a `RangeDay`;
- an unlisted measure pair accepted, a bare string accepted as `days` or as publication conditions, and repeated publication conditions counted twice;
- a condition name left out when its count is 0, and evidence conditions counted from one date only;
- coverage not summed, or counted by date;
- `complete` true with an absent date, a `partial` date, or a publication condition, false because of an evidence condition, or an `int`;
- mixed versions refused, not reported, or not sorted;
- a body read to decide a value, and summary bytes checked;
- a `SummaryContractFailure` passed through, a failure that carries a date, a failure with a `__cause__` or a caller's `__context__`, and inputs left in the boundary's or a builder's frame locals;
- inputs kept in the raising frame inside a tuple iterator, a lambda default, a closure cell, a keyword default, a generator, a coroutine, a `functools.partial`, a built-in bound method, a method wrapper, a Python bound method, a slotted object, a `SimpleNamespace`, a `map`, a `zip`, a dict view, an exception's arguments, a weak reference, or the caught inner exception, or in an attribute of the failure itself;
- an environment read through `os.environ`, `os.environb`, or `os.getenvb`; a clock read on a successful call, and through `time.time` or `time.gmtime` only on the refusal path; a `stat` of a file; and `days` read twice.

**Implementer review resolutions.** Sol's read-only review of the pack at 62e635b found no test that a correct implementation could not pass: Sol's independent implementation passed all 58. It made one blocking and three should-fix findings, and each is resolved:
1. **Traceback locals (blocking).** The walker looked only into containers and dataclasses. An implementation that kept its arguments in `iter((args, kwargs))` or in a lambda default passed every test, though the inputs could be recovered from the failure's traceback. The walker is now the one ported from `duplicate-observation-contract@1`. It opens the holders listed under "Failures" above, is cycle-safe, and also walks the failure's own `args` and attributes. Every frame after the caller's own frames is checked, not only the unit's. Both of Sol's mutants now fail, as do 17 more, one for each kind of holder. The claim is narrowed to what the walker can open, and itertools iterators are a residual. The change also fixes a portability fault Sol noted: the suite found the unit's frames by comparing a resolved package path with unresolved file names, so under macOS's `/tmp` symlink it found none. It now skips the caller's frames by their resolved file name.
2. **Clock guards (should-fix).** The guards raised without recording anything. An implementation that read the clock only while refusing a call had the guard's error turned into the expected named failure, and passed. The guards now record each read before raising, and the test requires the record to be empty. Sol's mutant fails, as does one that calls `time.gmtime()` there.
3. **`os.environb` (should-fix).** Only `os.environ` was guarded, so `os.environb` and `os.getenvb` were not covered. Both mappings are now guarded where the platform has a bytes environment, and both are restored. Sol's mutant fails, as does one that calls `os.getenvb`.
4. **R8 and the 90-date preset (should-fix).** The shifted-period check left out `last-90-dates`, so accepting any `last-90-dates` period of 1 to 90 dates passed. The 90-date builder is now in the loop, with its year-1 guard, and an 89-date `last-90-dates` period is refused explicitly. Every generated period is checked through both `period_dates` and `summarize_range`. Sol's mutant fails, as does one that skips the check in `summarize_range` alone.

Sol's five surviving mutants, applied unchanged to a copy of Sol's implementation, each fail the revised suite. RED gained one failing entry, the new 89-date subtest.

The implementer reviews the pack read-only for tests that a correct implementation could not pass, and those findings are resolved before freeze. The approver then confirms at Gate B that the pack carries this draft, that RED is genuine, that the effects declaration is empty as the contract says, and that the residuals below are complete.

## Residuals

Not mechanically provable here; the approver confirms these in implementation review:
- no clock, I/O, environment, or logging. A C-level clock read such as `date.today()` cannot be intercepted;
- the module imports only `summary_contract` beyond the standard library, and never opens a store;
- summaries are trusted as `build_summary` or `decode_summary` returned them (Decision 8). A hand-built `HealthSummary` with forged header fields is not caught;
- bodies are never read. A test changes body bytes and expects the same result, but cannot prove that no code path reads them;
- no failure's traceback keeps input inside an object the suite's walker cannot open, such as an itertools iterator or another C-level object (see "Failures" above).

Left to other units:
- each measure's aggregate and extra counts, and what part of a `partial` body is safe: the four compose units;
- reading daily summaries for a range and attaching their publication conditions: `operational-store@9` and the units that use its presentation reads;
- which zone or rule makes a date "today", the default 30-date view, evidence dated after today, and how periods, absent dates, states, and conditions are drawn: `render-portal-view` and `portal-state-presentation`;
- when each publication condition applies: `operational-store`, `refresh-summary-state`, and the completion units;
- documentation outside this unit: ADR 0019 still says "a calendar-year window preserving leap-day behavior", which the Operator's Decision 4 reads as the rolling year; amending the ADR is the Operator's call. Glossary entries for range, preset, local date, and absent date, which CONTEXT.md does not define, are a separate change. The scope-map wording was updated with this approval.
