# Draft acceptance pack — `set-source-selection-policy@1`

**Status:** Draft approved by the Operator on 2026-11-25, including the 12 decisions below as recommended; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `set-source-selection-policy` Governed Scope contract, version 1, in `governance/scopes/set-source-selection-policy.1.json`. It is a leaf and depends only on `operational-store@5`, which is merged and awaiting Gate C.

## What this unit does

When two apps record the same kind of data, for example a phone and a watch both counting steps, the Portal will not add them together silently. The Operator decides which origins count, for which summary, and on which dates (ADR 0026). This unit records that decision.

For one derived measure, such as daily Steps, it:
1. shows the Operator the origins the Portal has seen for that record type, and the policies already requested;
2. previews a request: which existing policies it replaces, and which dates it would leave with no policy;
3. records the request as one pending Source Selection Policy, but only if the existing policies still match what the Operator confirmed.

`operational-store@5` already stores policies, keeps them from overlapping, and refuses origins it has never seen. This unit adds the Operator's side: the measure names, the replacement rule, the preview, and the check that the Operator confirmed what is written.

## What it does not do

- It does not rebuild summaries. The pending policy is itself the durable request to rebuild them (`operational-store@5` Decision 1). `operational-store@6` turns it into Summary Refresh Jobs, and the policy activates with its rebuilt results and a Cutover Record (Decision 1).
- It queues no Processing Job. Source selection governs summaries, not interpretation (ADR 0026, `operational-store@5` R44).
- It does not decide how a summary uses the policy, or when overlapping origins make a result unresolved. `summary-contract` and the derive units do.
- It renders no page. The Phase 7 Portal shows the choices and preview and collects the Operator's confirmation (Decision 8).
- It does not withdraw a policy without replacing it, and does not erase policies (Decision 11).
- It does not touch Fallback Timezone Policies. `set-fallback-timezone-policy` does.

## Interface

The module `activity_tracker_portal.set_source_selection_policy` provides:

**Vocabulary (Decisions 2 and 3).**
- `MEASURES`: a read-only mapping from each derived measure to the record types it may name:
  - `"daily-steps"` → `("steps",)`
  - `"heart-rate-summary"` → `("heart-rate",)`
  - `"sleep-summary"` → `("sleep-session",)`
  - `"weight-series"` → `("weight",)`

**Values.** Frozen dataclasses:
- `SourceSelectionRequest(measure, record_type, origins, first_date, last_date)`. `origins` is a tuple of `operational-store@5` `OriginKey`. Each date is a `datetime.date` or `None` for an open end. The operations validate it; constructing one does not.
- `DateRange(first_date, last_date)`: inclusive local dates, each `None` when open.
- `SourceSelectionChoices(measure, record_type, origins, timeline)`. `origins` is a tuple of `operational-store@5` `KnownOrigin` for the record type. `timeline` is the live `SourceSelectionPolicyRecord`s for the measure and record type, open start first and then by first date.
- `SourceSelectionPreview(request, repeat_of, supersedes, uncovered)`:
  - `request` is the request with its origins sorted by kind, then origin;
  - `repeat_of` is the identity of an identical live policy, or `None`;
  - `supersedes` is the identities of the live policies it would replace, ascending;
  - `uncovered` is a tuple of `DateRange`, in date order: dates those policies cover that the request does not.
- `SourceSelectionResult(policy, recorded, uncovered)`. `policy` is the `operational-store@5` `SourceSelectionPolicyRecord`. `recorded` is `False` for a repeat. `uncovered` is as in the preview, and empty for a repeat.

**Operations.** `store` is an `operational-store@5` `OperationalStore`, or the Portal's serialized wrapper of one.
- `source_selection_choices(store, measure, record_type) -> SourceSelectionChoices`
- `preview_source_selection(store, request) -> SourceSelectionPreview`
- `set_source_selection_policy(store, request, confirmed_supersedes, clock) -> SourceSelectionResult`. `confirmed_supersedes` is the tuple of policy identities the Operator confirmed from the preview. `clock` returns a timezone-aware `datetime`.

**Failures (Decision 10).**
- `SourceSelectionRefusal(failure)`: an exception whose `args` is exactly `(failure,)`, for `selection-request-invalid`, `selection-measure-unknown`, `selection-origin-unknown`, and `selection-changed`.
- The store's `StoreRefusal("timestamp-invalid")`, `StoreRetryable("store-busy")`, and `StoreRetryable("storage-reserve-exhausted")` pass through unchanged.

## Requirements

### R1 — Governed Scope contract

The contract has the shared shape, kind `leaf`, and version 1. It depends only on `operational-store@5`. Its named failures are `selection-request-invalid`, `selection-measure-unknown`, `selection-origin-unknown`, `selection-changed`, and `timestamp-invalid`, which are invariant refusals, and `store-busy` and `storage-reserve-exhausted`, which are retryable. Its one observable effect, `source-selection-policy-write`, matches the effects declaration. It does not contradict the scope-map row: the row's dependencies are among the contract's.

Proposed tests: `test_contract_shape_and_dependency`, `test_scope_map_row_does_not_contradict_contract`, `test_effects_declaration_matches_contract`.

### R2 — Measure vocabulary

- `MEASURES` has exactly the four entries above and cannot be changed.
- Every measure meets `operational-store@5`'s measure rule: lowercase kebab-case of at most 64 characters.
- Every record type is a `projection/v1` record type.

Proposed tests: `test_measures_are_the_four_summaries`, `test_measures_meet_the_store_rule`, `test_measure_record_types_are_projection_types`, `test_measures_are_read_only`.

### R3 — Choices

`source_selection_choices(store, measure, record_type)` reads, in one read view:
- `known_origins(record_type)`, unchanged: every origin with a Source Record of that type, with its record count;
- `source_selection_timeline(measure, record_type)`, unchanged.

It writes nothing and reads no clock. An unknown pair is `selection-measure-unknown`; a non-string measure or record type is `selection-request-invalid`.

Proposed tests: `test_choices_list_known_origins_of_the_record_type`, `test_choices_list_the_live_timeline`, `test_choices_write_nothing`, `test_choices_refuse_unknown_measures`.

### R4 — Request validation

Every operation that takes a request checks it first and writes nothing on refusal:
- `selection-request-invalid` for:
  - a measure or record type that is not a string;
  - origins that are not a non-empty tuple of distinct `OriginKey`s with string fields;
  - a bound that is not exactly a `date` (a `datetime` or a string is refused);
  - a last date before the first date;
  - for `set_source_selection_policy`, a `confirmed_supersedes` that is not a tuple of distinct positive integers.
- `selection-measure-unknown` for a measure not in `MEASURES`, or a record type that measure does not name.

Dates follow Decision 5: inclusive, open at either end, any date Python can hold, and a single day is valid. Origins given in another order are the same request.

Proposed tests: `test_malformed_requests_are_refused`, `test_unknown_measure_or_pair_is_refused`, `test_dates_are_inclusive_and_may_be_open`, `test_origin_order_does_not_matter`.

### R5 — Origins must be known

An origin with no Source Record of the request's record type is `selection-origin-unknown`, in the preview and before any write (Decision 4). An origin known only for another record type is unknown here. One known origin is enough; several are an explicit combination (Decision 6).

Proposed tests: `test_unknown_origin_is_refused_before_writing`, `test_origin_of_another_record_type_is_unknown`, `test_single_and_combined_origins_are_accepted`, `test_every_measure_takes_single_and_combined_origins`.

### R6 — Preview

`preview_source_selection(store, request)` reads one view, writes nothing, and reads no clock.
- `supersedes` lists every live policy of the same measure and record type that shares at least one date with the request (Decision 7). Policies of other measures or record types are never listed.
- `uncovered` lists the dates covered by those policies and not by the request. Live policies never overlap, so there are at most two ranges: one before the request's first date and one after its last.
- If a live policy has the same measure, record type, origins, and dates, `repeat_of` names it, and `supersedes` and `uncovered` are empty (Decision 9).

Proposed tests: `test_preview_supersedes_every_overlapping_live_policy`, `test_preview_reports_uncovered_dates`, `test_preview_of_disjoint_request_supersedes_nothing`, `test_preview_recognizes_a_repeat`, `test_preview_writes_nothing`.

### R7 — Recording

`set_source_selection_policy(store, request, confirmed_supersedes, clock)`:
1. validates the request (R4) and checks its origins (R5) in one read view;
2. returns a repeat if a live policy is identical (R8);
3. computes the supersession as the preview does. If it differs from `confirmed_supersedes` as a set, it refuses with `selection-changed` and writes nothing (Decision 8);
4. reads the clock once and calls `record_source_selection_policy` with the request, the computed supersession ascending, and that time.

The result has `recorded` `True`, the store's record, and the preview's `uncovered`. `recorded` `True` means this call took the write path and the store returned the identical policy. That is either the policy this call inserted, or an identical one that another request recorded between this call's read and its write, which the store returns from its own repeat check (`operational-store@5` R46). The published store does not say which. Afterwards the store reads the new policy as live and pending, and each superseded policy as superseded by it. The write changes only `operational-store@5`'s policy tables: no Processing Job, no other policy, and no earlier table.

Proposed tests: `test_request_is_recorded_pending`, `test_recording_supersedes_the_confirmed_policies`, `test_stale_confirmation_is_refused`, `test_recording_writes_only_the_policy`, `test_result_reports_uncovered_dates`.

### R8 — Repeats

A request whose measure, record type, origins, and dates equal a live policy in this call's read view returns that policy with `recorded` `False` and empty `uncovered`. It writes nothing and reads no clock, whatever `confirmed_supersedes` says. These guarantees apply to repeats visible in that read. An identical request that lands after the read and before the write is returned through the write path instead (R7): `recorded` is `True`, `uncovered` is what this call computed, and the clock was read once. Either way only one policy is recorded. So a retry after an uncertain commit returns the first result and never records a second policy (Decision 9). A request equal to a superseded policy is new, and supersedes the live one in its place.

Proposed tests: `test_identical_request_writes_nothing`, `test_retry_after_commit_returns_the_same_policy`, `test_request_equal_to_a_superseded_policy_is_new`, `test_identical_request_landing_before_the_write_is_returned`.

### R9 — The timeline changes before the write

Between the read view and the write, another request may record a policy. The store then refuses with `policy-overlap` or `policy-supersession-invalid`. The unit raises `selection-changed` instead, and nothing is written. It does not retry by itself; the Operator previews again (Decisions 8 and 10).

Proposed tests: `test_concurrent_overlapping_policy_is_selection_changed`, `test_concurrently_superseded_policy_is_selection_changed`.

### R10 — Store failures pass through

- A naive clock time raises the store's `timestamp-invalid`.
- `store-busy` and `storage-reserve-exhausted` raise the store's `StoreRetryable` unchanged.
- A store failure raised by a read, on entering the read view or by any read in it, propagates unchanged. Nothing is written and no clock is read. This holds for choices, previews, and recordings.
- **Before commit.** A failure the store raises before its write commits leaves the whole database unchanged. This covers every case above.
- **After commit.** A failure raised after the store's write has committed, while the result is being delivered, propagates unchanged. The policy stays committed, and the unit does not undo it. A retry of the same request is then a repeat (R8), so no second policy is recorded.
- A repeat still succeeds below the reserve, because it writes nothing.

Proposed tests: `test_naive_clock_is_timestamp_invalid`, `test_busy_and_reserve_pass_through_and_write_nothing`, `test_repeat_succeeds_below_the_reserve`, `test_read_failures_pass_through_and_write_nothing`.

### R11 — Time

The unit reads time only from `clock`, at most once per request, immediately before the write. Choices, previews, refusals before the write, and repeats read no time. No other clock or timezone database is read.

Proposed tests: `test_clock_is_read_once_only_when_writing`.

### R12 — Health-data-free surfaces

- Every refusal's `args` is exactly `(failure,)`, and its text is the failure name. It carries no origin, date, measure value, or policy identity.
- A `selection-changed` raised for a store refusal (R9) has no `__cause__`, and its context is suppressed.
- Results carry the Operator's own policy settings, that is the measure, record type, origins, and dates, and Source Record counts. They carry no Source Identity, record time, value, or bytes, either in their declared fields or as any other instance state, including in nested policy and origin records.
- `recorded` reports the path the call took (R7, R8), not whether this call inserted a row; it carries no other information.
- The unit writes no log records.

Tests place markers in origins and Source Identities and check every refusal and log.

Proposed tests: `test_refusals_are_health_data_free`, `test_results_carry_no_source_evidence`, `test_unit_emits_no_logs`.

## Decisions for the Operator

1. **This unit records the policy only; the scope-map row and bead should say so.** The scope-map row and bead `e9v` were written before `operational-store@5` Decision 1. They say the setter "enqueues affected summary rebuilding atomically". Under that decision, the pending policy row is itself the rebuild request: it names the measure, record type, and dates whose summaries need rebuilding. `operational-store@6` turns it into Summary Refresh Jobs. So recording the policy already meets ADR 0026's "together" rule.
   - **Recommended:** keep this unit to recording. After approval, the drafter changes the row's effects to "Requested policy recorded as pending; the pending policy is the durable request to rebuild affected summaries, which `operational-store@6` turns into Summary Refresh Jobs; the prior result remains visible as superseded or pending until the policy and its rebuilt results activate together with a Cutover Record." Bead `e9v`'s description changes to match.
   - Alternative: keep the old wording. It would then contradict the approved `@5` decision and mislead later units.
   - Consequence: nothing summary-related is tested here. Whether summaries are rebuilt is tested in `@6` and `refresh-summary-state`.
2. **A closed list of four measures, owned here.** A measure names which summary a policy applies to. The recommendation is one measure per Phase 5 derive unit: `daily-steps`, `heart-rate-summary`, `sleep-summary`, and `weight-series`. Range summaries are built from these daily results, so they need no measures of their own. `summary-contract` depends on this unit (scope map), so it uses these names rather than defining its own.
   - **Recommended:** the closed list. A typo is refused rather than recorded as a policy that nothing will ever read. A new measure needs a new version of this unit.
   - Alternative: accept any well-formed name now, and let `summary-contract` decide later. A misspelt measure would be stored for good, because policies are append-only.
   - Alternative: wait for `summary-contract` to define the names. That is a cycle, because `summary-contract` depends on this unit.
3. **Each measure names exactly one record type now.** `daily-steps` with `steps`, `heart-rate-summary` with `heart-rate`, `sleep-summary` with `sleep-session`, and `weight-series` with `weight`. The request still carries both, as the store and the glossary do, and a pair not in the list is `selection-measure-unknown`.
   - **Recommended:** carry both and check the pair. A later measure that draws on two record types fits without an interface change.
   - Alternative: the request names only the measure, and the unit fills in the record type. That is one less field for the Portal, but a two-type measure would need a new interface.
4. **Origins must be ones the Portal has already seen for that record type.** This is `operational-store@5` Decision 10. The unit checks it before writing, so the preview can show the problem too. The Operator chooses from a list showing each origin's source-system kind, its name (an app package name), and how many Source Records it has. The list never shows Source Identities, record times, or values.
   - **Recommended:** as above.
   - Alternative: show names only, without counts. Counts help the Operator tell a main source from one used briefly.
   - Alternative: also show each origin's first and last record date. That needs a new store read, and those dates come from health evidence.
5. **Dates are inclusive local dates, and either end may be open.** This is `operational-store@5` Decision 6. A single day is valid. Any date Python can hold (years 1 to 9999) is accepted, including future dates, so the Operator can set a policy ahead of a device change. An open end and the earliest or latest possible date are kept as given, not treated as the same.
   - **Recommended:** as above.
   - Alternative: refuse dates before a fixed year, or in the future. Neither protects anything, because a policy on dates with no data has no effect.
6. **Several origins mean "combine these", for every measure.** ADR 0026 lets the Operator either pick one origin or explicitly allow a combination. How a summary combines them is the derive unit's job: Heart Rate and Weight group exact duplicates, and overlapping Steps become partial or unresolved (ADR 0041). A policy with one origin is also allowed when only one origin is known: it stops a newly seen origin from making the result unresolved.
   - **Recommended:** allow combinations for all four measures.
   - Alternative: allow only one origin for `daily-steps`, since Steps cannot be combined safely. That decides a summary rule here, ahead of `derive-daily-steps`.
7. **A new policy replaces every live policy it overlaps, whole.** Live policies for one measure never share a date, so a request must replace whatever it overlaps. The store can only replace a whole policy, one policy per write. So if an old policy reaches beyond the new dates, those extra dates are left with no policy. The preview and result list them as "uncovered", and the Operator can add a policy for them next.
   - Example: the live policy is "watch, all dates". The Operator requests "new watch, from 1 July 2026". The old policy is replaced, and every date before 1 July 2026 is uncovered until the Operator requests "watch, until 30 June 2026".
   - **Recommended:** as above. Each request is one atomic write, and the Operator sees exactly what changes.
   - Alternative: the unit splits the old policy for the Operator, writing the new policy and then the leftover pieces. The store has no way to write several policies at once, so a crash part-way leaves the same gap. A retry would finish the job, but `@6` could see the gap in between. A later store version could add an atomic multi-policy write.
   - Alternative: the caller passes any supersession list it likes, as the store allows. The Portal would then own the replacement rule, outside any governed unit.
   - Alternative: refuse a request that would leave dates uncovered. The Operator could then never narrow a policy, because narrowing always uncovers dates.
8. **The Operator confirms in the Portal; this unit checks that confirmation is still current.** The Phase 7 page shows the preview and asks the Operator to confirm. The confirmed request carries the policy identities the preview said it would replace. If the live policies have changed since, for example from another browser tab, the unit refuses with `selection-changed` and the page previews again.
   - **Recommended:** as above. The unit never writes a replacement the Operator did not see.
   - Alternative: the unit computes the replacement silently at write time. It is simpler, but the Operator may confirm one change and get another.
   - Alternative: a one-use confirmation token, as activation review will have. That is more than a single Operator choosing among a few policies needs.
9. **A request equal to a live policy does nothing.** Same measure, record type, origins (in any order), and dates: the unit returns the live policy and writes nothing. This also makes a retry safe. After a write whose commit was uncertain, the retry finds the new policy live and returns it. The store's own repeat rule could not do this, because it also compares the supersession list, and a retry would compute a different one.
   - **Recommended:** as above.
   - Alternative: rely on the store's rule alone. A retry after a commit would record a second identical policy that replaces the first, and request a needless rebuild.
10. **This unit's own refusal names, and the store's retryable failures passed through.** The unit raises `SourceSelectionRefusal` with `selection-request-invalid`, `selection-measure-unknown`, `selection-origin-unknown`, or `selection-changed`. If the store refuses with `policy-overlap` or `policy-supersession-invalid`, the live policies changed between the read and the write, so the unit raises `selection-changed` instead. The store's `timestamp-invalid`, `store-busy`, and `storage-reserve-exhausted` pass through unchanged, so the Portal handles them as it handles every store write. Any other store refusal would mean a defect here, and also passes through unchanged.
    - **Recommended:** as above. The Portal gets one "preview again" signal.
    - Alternative: pass every store name through unchanged. The Portal would then have to know that `policy-overlap` and `policy-supersession-invalid` mean "preview again".
11. **No withdrawal yet.** `operational-store@5` cannot withdraw a policy without replacing it. The Operator changes a policy by requesting another one. Returning dates to "no policy", so that overlapping origins become unresolved again, is not offered.
    - **Recommended:** defer withdrawal to a later version of this unit and the store, if the Operator needs it.
    - Alternative: add it now. That needs a new store version first.
12. **What the result reports.** The recorded or repeated policy, whether this request wrote it, and the uncovered dates. It does not count affected summaries, because summary identities come with `summary-contract`.
    - **Recommended:** as above.
    - Alternative: also report the number of Source Records of the selected origins in the dates. That needs a new store read and adds nothing the choices do not already show.

## Ground rules

- **Data.** Python only, standard library plus the pinned runtime. Tests use real temporary Operational Store databases through the published `operational-store@5` module. Source Records are created through the store's own operations (enrollment, archival, and `index_items`) from synthetic projections, as the `@5` suite does. Origins and record types are synthetic.
- **Faults.** A delegating store records a competing policy between the read and the write, raises an injected failure on the write, or raises one after the write commits. The reserve is exhausted through the store's `free_bytes` hook, and a real lock held by another connection gives `store-busy`. A test clock counts its calls.
- **Database checks.** Every "writes nothing" assertion compares the whole database, including rowids, as the `@5` suite does.
- **Markers.** Identity markers in origins and Source Identities, and a health-data marker in projection values, show that refusals and logs carry none of them, and that results carry no Source Identity or value.
- **Unbound inputs, reviewed rather than frozen.** The binding is the contract, the spec, the suite, the runner, and the effects declaration. These stay outside it:
  - `docs/governance/scope-map.md`, read by R1's scope-map test;
  - the published code, contracts, and packs of `operational-store`, which their own bindings freeze;
  - the `projection/v1/schemas` directory, which R2 reads for record-type names and `project-source-record` binds;
  - the execution environment: `uv`, `pyproject.toml`, `uv.lock`, and the managed Python and SQLite;
  - `tests/governance/test_set_source_selection_policy_contract.py`, which the governance suite runs and this runner does not.

## Freeze prerequisites

After Gate A, the executable pack adds:
- **The suite.** `tests/acceptance/test_set_source_selection_policy_1.py` has 43 tests across R1–R12, one per proposed name above and no others, grouped in one class per requirement. The implementer's review added three of them.
- **The runner.** `tests/acceptance/run_set_source_selection_policy_1.sh` runs only this suite. `operational-store@5`'s suites run under their own bound runner.
- **The Interface stub,** `src/activity_tracker_portal/set_source_selection_policy.py`:
  - the real `MEASURES`, a read-only mapping, because it is the vocabulary the Operator approved;
  - the five real frozen dataclasses: `SourceSelectionRequest`, `DateRange`, `SourceSelectionChoices`, `SourceSelectionPreview`, and `SourceSelectionResult`;
  - the real `SourceSelectionRefusal`;
  - the three operations, each raising `NotImplementedError("set-source-selection-policy@1")`.

**How the suite checks.**
- **Stores.** Each store is a real temporary database opened through the published `operational-store@5` API. Known origins come from archiving and indexing synthetic Source Records: two `steps` records from one origin, one from a second origin, and one record each for `weight`, `heart-rate`, and `sleep-session`. Live policies are recorded directly through the store, as an earlier request would have left them.
- **Whole-database comparisons.** Every refusal, no-write, and read-only assertion compares the whole database. Rowid tables are compared with their rowids, in rowid order, and `WITHOUT ROWID` tables as sorted rows. The helper is copied from the `@5` suite and reads the table kind from `PRAGMA table_list`, so it pins no table, failure, or migration list.
- **One exact write.** R7 copies the database with SQLite's backup API, makes the expected store call on the copy, and requires the two databases to be identical.
- **Delegating store.** A delegating store logs each read view, clock read, and write, in order. It can also:
  - run another write between the unit's read and its write;
  - raise an injected failure instead of writing;
  - raise one after a write commits;
  - raise an injected failure on calling `read()`, on entering the view, or from any read in the view. The view proxy is generic, so it allows any published read.
- **Interleavings.** Both race orders are deterministic:
  - a write that lands between the preview and the set, in R7;
  - a write that lands between the set's read and its write, in R9: one that overlaps the request, and one that supersedes the confirmed policy;
  - in R8, an identical request that lands between the set's read and its write, both alone and superseding a prior policy.
- **Every measure.** R5 indexes a second origin for each record type. For each of the four measures it runs choices, previews, recordings, and repeats, for one origin and for a combination.
- **Real store faults.** These are not injected:
  - a reserve shortfall through the store's `free_bytes` hook;
  - `store-busy` from another connection holding `BEGIN IMMEDIATE`, against a store opened with a 50 ms busy timeout.
- **Uncovered dates.** These are checked three ways:
  - a table of 37 cases covering open ends, single days, nested policies both ways, adjacent policies, gaps, and the limits of Python dates. Each case is previewed in R6's first test, and previewed and then recorded in its second;
  - a brute-force oracle over a six-day window, with sentinels for open ends. It checks 24 random disjoint timelines against every one of the 34 request ranges with two origin sets, 1,632 previews in all, including repeats. Each preview reports at most two ranges;
  - 30 random recorded requests in R7. Each checks the result, the policy's supersession, and the timeline afterwards.
- **Time.** Every time comes from a caller clock. No test reads the wall clock.

**RED, through the runner, against the stub:**
- 7 tests pass: the three R1 tests and the four R2 tests. They read only the contract, the scope map, the effects declaration, the schemas, `MEASURES`, and the published store.
- 36 tests fail, and every failure, at the test or subtest level (1,874 errors), is `NotImplementedError: set-source-selection-policy@1`. No other cause appears.

In the worktree, with the stub, these all pass:
- `tests/unit`;
- `tests/governance`;
- `tests/acceptance/run_operational_store_5.sh`, including `@1`–`@4`'s bound runners.

**Passability.** All 43 tests pass with each of these, kept out of the repository:
- a throwaway reference;
- the implementer's independent implementation, unchanged since its review;
- the implementer's two legitimate variants: one that calls the store with keyword arguments, and one that finds live policies from the filtered history read.

**Mutants.** The drafter ran 41 wrong-implementation mutants against the reference, and the suite kills all of them, before and after the review changes. The implementer's eight review mutants were also re-run against its implementation: the six wrong ones fail, and the two legitimate variants pass. The drafter's 41 cover:
- **Measures:** an open measure list; a pair check that ignores the record type; choices that skip the pair check.
- **Origins:**
  - unknown origins allowed;
  - origins known for any record type;
  - the source-system kind ignored;
  - choices listing every record type's origins.
- **Supersession:** exclusive start or end dates; replacing only policies inside the request; superseded policies considered.
- **Uncovered dates:** off by one day; the later range missing; the earlier range made open; the date limits ignored; dropped from the result.
- **Confirmation:**
  - the `selection-changed` check missing;
  - the confirmation compared in order rather than as a set;
  - its shape checked only after the repeat check;
  - booleans allowed in it.
- **Repeats:**
  - a repeat not detected;
  - a repeat that writes or reads the clock;
  - reordered origins not recognized as a repeat;
  - the preview echoing the given origin order.
- **Requests:** `datetime` bounds, reversed dates, and duplicate origins allowed.
- **Store failures:**
  - `policy-overlap` or `policy-supersession-invalid` passed through raw;
  - every store refusal translated;
  - store retryable failures swallowed;
  - `store-busy` retried once;
  - the race retried.
- **Leaks:** an origin in the refusal's arguments, or in an extra attribute on it; a translated refusal chained to the store's.
- **Writes and time:**
  - the clock read before the read view;
  - a second clock read for the write;
  - a second identical write;
  - the supersession passed to the store unsorted.

One mutant first survived: the supersession passed unsorted. The store sorts it, so the database is identical, but R7 step 4 says the call passes it ascending. R7's single-write test now also checks the store call's arguments, and the mutant fails.

**Refinements.** The tests pin these beyond the approved text. None changes approved semantics. Items marked *settles* decide a point the approved text left open, and are flagged for the approver.
- **Request shapes (R4).**
  - These are `selection-request-invalid`:
    - a measure or record type that is `None` or an integer;
    - origins that are `()`, a list, `None`, a bare `OriginKey`, a plain tuple, a string, a duplicate, or an `OriginKey` with a non-string field;
    - an aware or naive `datetime`, a string, or an integer as a date bound;
    - reversed dates, including `date.max` to `date.min`.
  - These confirmations are `selection-request-invalid`: a list, a set, `None`, a scalar, `0`, a negative, a string, a float, a duplicate, `True`, and a nested tuple. That matches the store's rule for `supersedes`.
  - *Settles:* the confirmation's shape is checked before the repeat check. A malformed confirmation is refused even when the request is identical to a live policy, because R4 says every operation checks its inputs first. R8's "whatever `confirmed_supersedes` says" covers any well-formed value.
- **Measure pairs (R3, R4).** A string pair that is not in `MEASURES`, including an empty string or a different letter case, is `selection-measure-unknown`. A non-string measure or record type is `selection-request-invalid`, for the choices as well as for requests.
- **Origins (R5).** "Known" means a Source Record with exactly that source-system kind, origin, and record type. An origin seen only under another record type or another kind is unknown until it is indexed for this record type. An unknown origin is refused before the confirmation is compared, as R7's order of steps states.
- **Date limits (R6, Decision 5).** *Settles:* there are no dates before `date.min` or after `date.max`, so no uncovered range reaches past them:
  - a request from `date.min` over an open-start policy leaves nothing uncovered before it;
  - a request from 2 January of year 1 leaves `(None, date.min)`;
  - the same holds at `date.max`.

  A request from `date.min` to `date.max` is not a repeat of an all-dates policy. It supersedes that policy and leaves nothing uncovered.
- **Gaps (R6).** Dates that already had no policy are never reported as uncovered. Only dates the request takes from a replaced policy are.
- **One read view (R3, R6, R7, R11).** Through the delegating store:
  - choices and previews each open one read view and nothing else;
  - a new policy is exactly a read, then a clock read, then a write;
  - refusals and repeats never write or read the clock.

  This makes part of the first residual mechanical, for calls through the store it is given.
- **The store call (R7 step 4).** The call is checked with its arguments bound by name:
  - the measure, record type, and dates of the request;
  - the origins, compared as a set;
  - the computed supersession, ascending;
  - the clock's time.
- **Store failures (R10, Decision 10).**
  - An injected `store-busy`, `storage-reserve-exhausted`, `policy-request-invalid`, or `policy-origin-unknown` reaches the caller as the very same object, after one write attempt.
  - A naive clock gives the store's own `StoreRefusal("timestamp-invalid")`, not a `SourceSelectionRefusal`.
- **Uncertain commit (R8).** A retryable failure raised after the write commits passes through. Retries then return the committed policy with `recorded` `False` and write nothing.
- **Race (R9).**
  - The `selection-changed` raised for a store refusal is not a `StoreRefusal`. It has no `__cause__`, and its context is suppressed.
  - The write was attempted once and the clock read once.
  - The database holds exactly the landed write.
- **Refusal surfaces (R12).**
  - A refusal's only attribute is `failure`. Its `args` is `(failure,)`, its `str` is the failure name, and neither its `str` nor its `repr` carries an origin, date, policy identity, Source Identity, or health marker.
  - Every one of the four names is produced in the test.
  - The five dataclasses have the Interface's field names, in order, and are frozen.
- **Logs (R12).** No log record at any level is emitted during choices, previews, recordings, repeats, or any refusal.
- **Identical landing (R7, R8, R12).** **For the approver: this narrows Decision 12.** Decision 12 describes `recorded` as "whether this request wrote it". The published store cannot report that: when an identical request lands between this call's read and its write, the store's repeat check returns the landed policy. The coordinator therefore decided what the flag means:
  - `recorded` `True` means this call took the write path and the store returned the identical policy, whether this call inserted it or an identical request landed first;
  - the repeat guarantees, which are `recorded` `False`, empty `uncovered`, and no clock read, apply to repeats visible in the call's read;
  - an identical landing returns the landed policy with `recorded` `True`, the uncovered dates this call computed, and events read, clock, write. The database holds only the landed write, and a later identical request is an ordinary repeat.

  If the flag must mean actual insertion, the store would have to report it in a later version.
- **Before and after commit (R10, effects, contract).** This is wording only, and the approved behaviour is unchanged. A failure before the store's write commits leaves the whole database unchanged. A failure raised after the commit, while the result is being delivered, propagates as the same object and leaves the policy committed, and a retry is a repeat.
- **Read failures (R10).** A failure raised by a read, on calling `read()`, on entering the view, or from any read in it, reaches the caller as the same object. This holds for choices, previews, new recordings, and repeats. Nothing is written, no clock is read, and the database is unchanged. Besides `store-busy`, the test injects a store refusal, `store-schema-unsupported`, which passes through under Decision 10 as well.
- **Instance state (R12).** Every returned value holds only its declared fields, recursively: the choices, previews, results, and repeats, with their nested policy, origin, and date-range records. Neither instance dictionaries nor slots may hold anything else, so no evidence can be attached to a result.

**Implementer review resolved.** GPT 6.1 Sol reviewed the pack read-only at `9de998d`. Its independent implementation passed all 40 tests then, and it passes all 43 now without change. RED was confirmed. The coordinator decided the following:
1. **Blocking: uncertain commit against the "commits nothing" wording.** The effects declaration, R10, and the contract's `store_failure`, `recorded_policy`, and observable-effect descriptions now separate a failure before commit (nothing written) from one after commit (the policy stays committed, the same exception propagates, and a retry is a repeat). This is wording only.
2. **Concurrent identical request.** Settled as the identical-landing refinement above, and flagged for the approver. R7, R8, and R12 state it, and `test_identical_request_landing_before_the_write_is_returned` covers it, alone and superseding a prior policy. The implementer's implementation already behaves this way.
3. **Every measure.** Added `test_every_measure_takes_single_and_combined_origins`. Its mutants `reject_heart_rate_recording` and `reject_nonsteps_combinations` now fail.
4. **Instance state.** The result oracle now checks the instance state of every returned value, recursively. The mutant `extra_output_evidence` now fails.
5. **Read faults.** Added `test_read_failures_pass_through_and_write_nothing`, through a generic view proxy. The mutants `translate_read_retryable` and `read_failure_translated` now fail.
6. **Uncovered dates and races.** The review found no passability error and no flakiness. No change.
7. **Call style and inventories.** The review found nothing pinned that should not be. No change.

The implementer reviews the pack read-only for tests that a correct implementation could not pass, and those findings are resolved before freeze. The approver then confirms at Gate B that the pack carries the approved draft, that RED is genuine, that the effects declaration matches the contract, and that the residuals below are complete.

## Residuals

Not mechanically provable here, so the approver confirms these in implementation review:
- each operation reads one view and makes at most one store write. The tests check this through the delegating store, but not for a store reached any other way;
- only published `operational-store@5` operations are used: no SQL and no private store helpers;
- no clock other than the injected one, and no logging;
- `MEASURES` matches the Phase 5 derive units; `summary-contract@1` confirms it when drafted.

Left to other units:
- turning pending policies into Summary Refresh Jobs, activation, the active timeline, and Cutover Records: `operational-store@6` and `refresh-summary-state`;
- how summaries apply a policy and when overlapping origins are unresolved: `summary-contract`, `duplicate-observation-contract`, and the derive units;
- the Sources page, its confirmation form, and CSRF: the Phase 7 Portal units;
- an atomic multi-policy write, if the Operator prefers automatic splitting (Decision 7): a later store version;
- withdrawal (Decision 11) and erasure under privacy deletion: later versions and `operational-store@7`.
