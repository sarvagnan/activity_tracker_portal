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

Proposed tests: `test_unknown_origin_is_refused_before_writing`, `test_origin_of_another_record_type_is_unknown`, `test_single_and_combined_origins_are_accepted`.

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

The result has `recorded` `True`, the store's record, and the preview's `uncovered`. Afterwards the store reads the new policy as live and pending, and each superseded policy as superseded by it. The write changes only `operational-store@5`'s policy tables: no Processing Job, no other policy, and no earlier table.

Proposed tests: `test_request_is_recorded_pending`, `test_recording_supersedes_the_confirmed_policies`, `test_stale_confirmation_is_refused`, `test_recording_writes_only_the_policy`, `test_result_reports_uncovered_dates`.

### R8 — Repeats

A request whose measure, record type, origins, and dates equal a live policy returns that policy with `recorded` `False` and empty `uncovered`. It writes nothing and reads no clock, whatever `confirmed_supersedes` says. So a retry after an uncertain commit returns the first result and never records a second policy (Decision 9). A request equal to a superseded policy is new, and supersedes the live one in its place.

Proposed tests: `test_identical_request_writes_nothing`, `test_retry_after_commit_returns_the_same_policy`, `test_request_equal_to_a_superseded_policy_is_new`.

### R9 — The timeline changes before the write

Between the read view and the write, another request may record a policy. The store then refuses with `policy-overlap` or `policy-supersession-invalid`. The unit raises `selection-changed` instead, and nothing is written. It does not retry by itself; the Operator previews again (Decisions 8 and 10).

Proposed tests: `test_concurrent_overlapping_policy_is_selection_changed`, `test_concurrently_superseded_policy_is_selection_changed`.

### R10 — Store failures pass through

- A naive clock time raises the store's `timestamp-invalid`.
- `store-busy` and `storage-reserve-exhausted` raise the store's `StoreRetryable` unchanged.
- In each case nothing is written. A repeat still succeeds below the reserve, because it writes nothing.

Proposed tests: `test_naive_clock_is_timestamp_invalid`, `test_busy_and_reserve_pass_through_and_write_nothing`, `test_repeat_succeeds_below_the_reserve`.

### R11 — Time

The unit reads time only from `clock`, at most once per request, immediately before the write. Choices, previews, refusals before the write, and repeats read no time. No other clock or timezone database is read.

Proposed tests: `test_clock_is_read_once_only_when_writing`.

### R12 — Health-data-free surfaces

- Every refusal's `args` is exactly `(failure,)`, and its text is the failure name. It carries no origin, date, measure value, or policy identity.
- A `selection-changed` raised for a store refusal (R9) has no `__cause__`, and its context is suppressed.
- Results carry the Operator's own policy settings, that is the measure, record type, origins, and dates, and Source Record counts. They carry no Source Identity, record time, value, or bytes.
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
- **Faults.** A delegating store records a competing policy between the read and the write, and raises `store-busy` on the write. The reserve is exhausted through the store's `free_bytes` hook. A test clock counts its calls.
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
- the suite `tests/acceptance/test_set_source_selection_policy_1.py`, one test per proposed name in R1–R12;
- the runner `tests/acceptance/run_set_source_selection_policy_1.sh`, which runs only this suite. `operational-store@5`'s suites run under their own bound runner;
- the Interface stub `src/activity_tracker_portal/set_source_selection_policy.py`:
  - the real `MEASURES`, because it is the vocabulary this draft approves;
  - the real dataclasses and `SourceSelectionRefusal`;
  - the three operations raising `NotImplementedError("set-source-selection-policy@1")`.

**RED, against the stub.** The three R1 tests and the four R2 tests pass, because they read only the contract, the scope map, the effects declaration, the schemas, and `MEASURES`. Every other test fails, and every failure traces to the stub's `NotImplementedError`, raised directly or seen as a missing policy after the call. Every upstream bound runner still passes.

Before freeze, a throwaway reference implementation, kept out of the repository, passes every test. A set of incorrect implementations covers at least:
- an open measure vocabulary, and a pair check that ignores the record type;
- origins checked for any record type, or not checked before the write;
- a supersession that misses a straddling policy, takes another measure's or record type's policy, or uses exclusive date bounds;
- uncovered dates that are missing, off by one day, or include dates the request covers;
- the confirmed supersession ignored, or compared in order rather than as a set;
- a repeat that writes, reads the clock, or depends on the confirmed supersession;
- a retry that records a second policy;
- the store's race refusals passed through, or retried;
- a clock read during a preview or a refusal;
- a Processing Job queued, or a second write;
- an origin or date in refusal text.

Each must fail at least one test.

The implementer reviews the pack read-only for tests that a correct implementation could not pass, and those findings are resolved before freeze. The approver then confirms at Gate B that the pack carries this draft, that RED is genuine, that the effects declaration matches the contract, and that the residuals below are complete.

## Residuals

Not mechanically provable here, so the approver confirms these in implementation review:
- each operation reads one view and makes at most one store write;
- only published `operational-store@5` operations are used: no SQL and no private store helpers;
- no clock other than the injected one, and no logging;
- `MEASURES` matches the Phase 5 derive units; `summary-contract@1` confirms it when drafted.

Left to other units:
- turning pending policies into Summary Refresh Jobs, activation, the active timeline, and Cutover Records: `operational-store@6` and `refresh-summary-state`;
- how summaries apply a policy and when overlapping origins are unresolved: `summary-contract`, `duplicate-observation-contract`, and the derive units;
- the Sources page, its confirmation form, and CSRF: the Phase 7 Portal units;
- an atomic multi-policy write, if the Operator prefers automatic splitting (Decision 7): a later store version;
- withdrawal (Decision 11) and erasure under privacy deletion: later versions and `operational-store@7`.
