# Draft acceptance pack — `set-fallback-timezone-policy@1`

**Status:** Draft approved by the Operator on 2026-11-25, including the 11 decisions below as recommended; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `set-fallback-timezone-policy` Governed Scope contract, version 1, in `governance/scopes/set-fallback-timezone-policy.1.json`. It is a leaf that depends only on `operational-store@5`, which is frozen and implemented but awaits Gate C. This unit may be drafted and implemented against it, but cannot publish before it.

## What this unit does

A Fallback Timezone Policy names the Subject's timezone. Processing uses it to place a time on a calendar date only when the source gave no offset (ADR 0025). Today nothing records one, so every such record is Policy-Pending (`execute-processing-job@1` Decision 10). `operational-store@5` can now store policies. This unit is the governed way to add one.

It offers two steps, a preview and a confirmation (Decision 1):
1. **Plan.** For a zone and a start, it checks the zone against the pinned timezone data, checks the start, and works out which live policies the new one replaces. It writes nothing.
2. **Set.** The Portal calls it with the plan the Operator confirmed. It checks that the plan still matches the timeline, then records the policy through `operational-store@5`. In the same transaction, the store queues reprocessing for the affected records and marks jobs that are running (`@5` R47, R48).

The new policy is **pending**: requested, but not yet used by ordinary views. It becomes active with its rebuilt results and a Cutover Record, which `operational-store@6` adds. Source offsets always win over the policy (ADR 0025).

## What it does not do

- It does not show anything or collect the confirmation. The Phase 7 Portal page proposes a zone from the browser, shows the plan, and asks the Operator to confirm (ADR 0019, 0025). This unit is what that page calls.
- It does not store policies, check overlaps, choose the affected records, or queue jobs. `operational-store@5` does.
- It does not decide which timeline processing reads while a policy is pending. That is `execute-processing-job@2` and `operational-store@6` (Decision 10).
- It does not activate a policy, write a Cutover Record, or rebuild summaries. `operational-store@6` and the summary units do.
- It does not attribute times or build policy identities. `interpret-source-projection@1` does, and it puts the tzdata release in each identity.
- It does not withdraw a policy without a replacement (Decision 8), and does not handle Source Selection Policies.

## Interface

The module `activity_tracker_portal.set_fallback_timezone_policy` provides:

**Types.**
- `FallbackTimezonePlan(zone, effective_from, replacing, supersedes)`: a frozen dataclass.
  - `zone`: the IANA name as the Operator chose it.
  - `effective_from`: the start as a UTC `datetime`, or `None` for all earlier history.
  - `replacing`: the later policies the Operator chose to replace as well, ascending.
  - `supersedes`: every live policy the new one replaces, ascending. It is `replacing` plus the live policy with the same start, if there is one.
- `FallbackTimezoneResult(policy, repeated, jobs_enqueued, jobs_rearmed)`: a frozen dataclass.
  - `policy`: the store's `FallbackTimezonePolicyRecord`.
  - `repeated`: true when the plan had already been recorded, for example by an earlier attempt whose commit was uncertain.
  - `jobs_enqueued` and `jobs_rearmed`: the store's counts. Both are 0 on a repeat.
- `FallbackTimezoneRefusal(failure)`: the exception for this unit's own failures. Its `failure` attribute and its text are the failure name only.

**Operations.**
- `available_zones() -> tuple[str, ...]`: the zones the Operator may choose, sorted (Decision 2).
- `plan_fallback_timezone_policy(view, zone, effective_from, *, replacing=(), at) -> FallbackTimezonePlan`. `view` is an `operational-store@5` `StoreView`. `at` is the caller's timezone-aware time.
- `set_fallback_timezone_policy(store, plan, *, processor_versions, max_attempts, at) -> FallbackTimezoneResult`. `store` is an `operational-store@5` `OperationalStore`. `processor_versions` and `max_attempts` go to the store unchanged (Decision 7).

**Failures.**
- `FallbackTimezoneRefusal` with one of: `timestamp-invalid`, `timezone-unknown`, `effective-time-invalid`, `replacement-invalid`, `all-history-policy-required`, `policy-unchanged`, or `plan-stale`.
- The store's own exceptions, unchanged: `StoreRefusal` with `policy-request-invalid`, `processor-version-invalid`, or `processing-request-invalid` for bad reprocessing settings; `StoreRetryable` with `store-busy` or `storage-reserve-exhausted` (Decision 11).

## Requirements

### R1 — Governed Scope contract

The contract has the shared shape, kind `leaf`, and version 1. It depends on `operational-store` at version 5 and nothing else. Its named failures are the twelve above, with the outcome classes the contract lists. Its one observable effect, `fallback-timezone-policy-write`, matches the effects declaration. It does not contradict the scope-map row.

Proposed tests: `test_contract_has_required_shape`, `test_effects_declaration_matches_contract`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — Allowed zones

- `available_zones()` is the zone list the pinned `tzdata` package ships (`tzdata==2026.6`, IANA `2026f`), minus `Factory`. It is read from the package, never from the host's timezone database. In `2026f` that is 597 names.
- Links are accepted under the name given, for example `Europe/Amsterdam`, `Asia/Calcutta`, and `UTC` (Decision 2).
- A zone not in that list is `timezone-unknown`. That includes `Factory`, a wrong case such as `europe/london`, a non-string, an empty string, a path such as `../UTC` or `Europe//London`, and files in the package that are not zones, such as `leapseconds`, `zone.tab`, and `tzdata.zi`. A name that exists only in the host's timezone database is also refused.
- Every allowed zone is accepted by `interpret-source-projection@1`. This matters because `@5` checks only the zone's form (`@5` Decision 9), and an unknown zone would make `interpret` refuse every input.

Proposed tests: `test_allowed_zones_are_the_pinned_tzdata_list`, `test_links_are_accepted_as_named`, `test_unknown_and_malformed_zones_are_refused`, `test_zone_names_are_case_sensitive`, `test_host_timezone_database_is_never_read`, `test_every_allowed_zone_is_accepted_by_interpret`.

### R3 — Start and request time

- `effective_from` is `None` (all earlier history) or a timezone-aware `datetime` no later than `at` (Decision 4). The plan holds it in UTC.
- These are `effective-time-invalid`: a naive `datetime`, a `date` or other type, a start later than `at`, and a start that cannot be expressed in UTC.
- A naive or non-`datetime` `at` is `timestamp-invalid`.
- Checks run in this order, and the first failure is raised: `at`, zone, start, the form of `replacing`, then the timeline checks of R4–R6.

Proposed tests: `test_all_earlier_history_start`, `test_start_is_held_in_utc`, `test_start_may_equal_request_time`, `test_future_start_is_refused`, `test_invalid_start_is_refused`, `test_naive_request_time_is_refused`, `test_refusal_order`.

### R4 — The first policy covers all earlier history

- When no live policy covers all earlier history, the request must, or it is `all-history-policy-required` (Decision 5).
- So, through this unit, a non-empty timeline always begins with an all-earlier-history policy.

Proposed tests: `test_first_policy_must_cover_all_history`, `test_timeline_always_begins_with_all_history`.

### R5 — What a new policy replaces

- A live policy with the same start is replaced automatically. A new all-earlier-history policy replaces the current one.
- Later live policies stay, unless the Operator names them in `replacing` (Decision 3).
- Earlier policies are never replaced.
- `replacing` must be a tuple of distinct positive integers, each naming a live Fallback Timezone Policy that starts strictly after the new start. For an all-earlier-history request, that means any live policy with a start. Anything else is `replacement-invalid`: an unknown identity, a superseded policy, a Source Selection Policy, a policy with the same or an earlier start, a duplicate, or a list in place of the tuple.
- `supersedes` is ascending.

Proposed tests: `test_same_start_policy_is_replaced`, `test_all_history_policy_replaces_all_history`, `test_later_policies_are_kept_by_default`, `test_named_later_policies_are_replaced`, `test_earlier_policies_are_never_replaced`, `test_invalid_replacements_are_refused`.

### R6 — A request that changes nothing

- A request for the zone that already applies at exactly that start, with nothing in `replacing`, is `policy-unchanged` (Decision 6).
- The same zone and start with policies named in `replacing` is a change and is planned.

Proposed tests: `test_unchanged_request_is_refused`, `test_same_zone_with_named_replacements_is_a_change`.

### R7 — Planning writes nothing

Planning reads only `view.fallback_timezone_timeline()` and the pinned zone list. The whole database is unchanged afterwards, for a plan and for every refusal.

Proposed tests: `test_planning_writes_nothing`.

### R8 — Confirming a plan

`set_fallback_timezone_policy`:
1. checks `at`, and the plan's zone, start, and `replacing` form, as R2 and R3 do, and that `replacing` is a subset of `supersedes` (`replacement-invalid` otherwise);
2. reads the timeline in one read view;
3. if a live policy already has the plan's zone, start, and `supersedes`, returns it with `repeated` true and zero counts, and writes nothing (R9);
4. plans the same request again against that read. If the result differs from the given plan, or the request now fails R4, R5, or R6, it raises `plan-stale`;
5. records the policy with `record_fallback_timezone_policy(plan.zone, plan.effective_from, plan.supersedes, processor_versions, max_attempts, at)`.

The result's `policy` is the store's record: live, pending, with `requested_at` equal to `at`. A plan built by hand cannot get round R2–R6: step 1 or step 4 refuses it. A repeat (step 3) must be internally consistent, with `replacing` a subset of `supersedes`, which step 1 checks first. Beyond that, a repeat is recognised by the recorded write content alone: zone, start, and `supersedes`.

Proposed tests: `test_confirmed_plan_is_recorded_pending`, `test_confirmation_supersedes_exactly_the_plan`, `test_confirmation_rechecks_the_request`, `test_hand_built_plan_cannot_bypass_rules`.

### R9 — Repeats and stale plans

- **Repeat.** Confirming a plan that is already recorded returns that policy with `repeated` true and zero counts, and writes nothing, even below the storage reserve. This covers a retry after an uncertain commit.
- **Concurrent repeat.** If the store itself returns an existing identical policy (`@5` R46), because another call recorded it after this call's read, `repeated` is also true. The returned policy was not live in this call's read and its `requested_at` is not `at`.
- **Stale.** If the timeline changed so that the plan no longer fits, the result is `plan-stale` and nothing is written. The Portal shows a fresh plan for the Operator to confirm (Decision 9).
- **Change during the write.** The read and the write are separate transactions. If another write lands between them, the store refuses with `policy-overlap`, `policy-supersession-invalid`, or `policy-unknown`. This unit raises `plan-stale` for each, and nothing is written.

Proposed tests: `test_repeat_of_recorded_plan_writes_nothing`, `test_repeat_succeeds_below_the_reserve`, `test_changed_timeline_makes_plan_stale`, `test_write_between_read_and_commit_is_plan_stale`.

### R10 — Reprocessing goes through the store

- `processor_versions` and `max_attempts` reach the store unchanged, and the result reports the store's `jobs_enqueued` and `jobs_rearmed`.
- End to end, through the published store: a record whose latest outcome used a fallback policy, and a record that is Policy-Pending for `fallback-timezone`, each get a queued job. A record whose times all carry source offsets does not.
- Bad reprocessing settings raise the store's `StoreRefusal` unchanged: `policy-request-invalid`, `processor-version-invalid`, or `processing-request-invalid`. Nothing is written.
- `store-busy` and `storage-reserve-exhausted` raise the store's `StoreRetryable` unchanged. Nothing is written.

Proposed tests: `test_policy_queues_affected_reprocessing`, `test_experienced_records_are_not_queued`, `test_reprocessing_settings_reach_the_store`, `test_invalid_reprocessing_settings_pass_through`, `test_store_retryables_pass_through_and_write_nothing`.

### R11 — One write

A confirmation makes exactly the store's one policy write and nothing else. The test replays the same `record_fallback_timezone_policy` call on a copy of the database taken before the confirmation, and requires the two databases to be identical.

Proposed tests: `test_confirmation_is_one_store_write`.

### R12 — Health-data-free, no clock, no logs

- Plans, results, and failures carry no projection, header, or body bytes, field values, origin, or Source Identity. The store holds synthetic markers in those places, and no `repr`, `str`, or traceback carries them.
- A `FallbackTimezoneRefusal` has `args` equal to `(failure,)`, and no `__cause__` or `__context__` that carries another exception's text.
- Time comes only from `at`. No clock is read, and the result is the same under any host `TZ` setting.
- The unit writes no log records.

Proposed tests: `test_plans_and_results_are_health_data_free`, `test_failures_carry_only_their_name`, `test_no_clock_or_host_timezone_is_read`, `test_unit_emits_no_logs`.

## Decisions for the Operator

1. **A preview, then a confirmation.** "Operator-confirmed" means the Portal page shows the plan and the Operator confirms it. That page arrives in Phase 7. This unit gives it a read-only plan, and a set operation that records only the plan the Operator saw. If the timeline changed in between, the set operation refuses with `plan-stale` instead of recording something the Operator did not see.
   - The plan says which policies the new one replaces. It does not say how many records will be reprocessed: `@5` has no read for that count. The count arrives after the write, as `jobs_enqueued`.
   - Alternative: one call that records directly, with the page working out what to show. The page would then hold a second copy of the replacement rule, which the scope map forbids ("No route or command owns a second implementation of governed behavior").
   - Alternative: also preview the affected-record count. That needs a new store read in a later store version, and would hold this unit back.
   - Consequence: the "Operator surfaces" list in `docs/governance/scope-map.md` does not yet name this unit among those the Portal invokes. ADR 0019 already gives the browser the confirmation. The list should gain it in a separate documentation change.
2. **Any zone name in the pinned timezone data is accepted, links included, except `Factory`.** Names are checked against the pinned `tzdata` package, never the host's database, and stored exactly as chosen. In the pinned data, a link such as `Europe/Amsterdam` is a byte-for-byte copy of its target, so attribution is identical.
   - Alternative: only canonical zones. In the pinned data that would make a Dutch Operator pick `Europe/Brussels`, a Norwegian `Europe/Berlin`, and an Icelander `Africa/Abidjan`, because the timezone database merged those zones. Browsers also propose old names such as `Asia/Calcutta`, which would be refused.
   - Alternative: accept links but store the canonical target. Attribution is the same, but the record no longer shows what the Operator chose.
   - Note for the page: `Etc/GMT+5` means five hours *behind* UTC. That is the database's convention, not this unit's.
3. **What a new policy replaces.** The live policy with the same start is replaced automatically, because two policies cannot share a start. Later policies stay unless the Operator names them. Earlier policies are never touched. `@5` Decision 7 left this choice to the setter.
   - Why: "backdating" a change is ambiguous. Moving "Tokyo since 1 March" earlier than a recorded "Tokyo since 1 June" should keep June (harmless). But if a "London since 2025" policy follows, replacing everything later would silently erase that later move. Keeping later policies is the safe default, and naming them lets the Operator drop one on purpose.
   - Alternative: a new policy replaces every later one ("from this start on, always this zone"). Simpler, but it can silently erase a later move.
   - Alternative: the caller names everything it replaces. That pushes the rule into the Portal page.
4. **The start is "all earlier history" or a moment no later than now.** The page should default the start to now, as ADR 0025 says. Backdating is allowed. A future start is refused.
   - Why: the policy records where the Subject was, and there is no evidence yet for the future. A planned move can be recorded once it happens, and backdated if late.
   - Alternative: allow future starts, so a planned move can be entered in advance. If the plan changes, the Operator must remember to correct it.
5. **The first policy must cover all earlier history.** ADR 0025 says the initial policy covers all retained history without an explicit offset. Requiring it means no record is left Policy-Pending just because it is older than the first policy.
   - Alternative: allow a first policy with a start. Records before it stay Policy-Pending until an all-history policy is added.
6. **A request that changes nothing is refused (`policy-unchanged`).** Recording the same zone at the same start again would create a new policy identity and reprocess every affected record for no change.
   - Alternative: record it anyway. That wastes a full reprocessing run.
   - Alternative: return the existing policy as if just set. That hides the fact that nothing changed.
7. **The caller supplies the processor-version mapping and attempt bound.** The store needs them to queue reprocessing (`@5` R43). The Portal passes the `portal-worker`'s own settings, so jobs are queued under the same versions the worker runs. This keeps this unit's only dependency `operational-store`, as the scope map says.
   - Alternative: import `execute-processing-job`'s registry as a default. That adds a dependency on `execute-processing-job`, and if `execute-processing-job@2` ever depended on this unit, the two scopes would form a cycle.
   - Alternative: queue no reprocessing (an empty mapping). That contradicts the scope row's "affected reinterpretation".
8. **No withdrawal in this version.** A wrong policy is corrected by replacing it: same start with another zone, or a new policy that names the wrong later ones. The all-earlier-history policy can be replaced but not removed. `@5` offers no withdrawal without a replacement.
   - Alternative: add withdrawal now. That needs a new store version first.
9. **A changed timeline means confirm again; a repeated confirmation is safe.** If the timeline changed after the Operator saw the plan, the set operation refuses with `plan-stale`, and the page shows a fresh plan. If the same plan is confirmed twice, for example after a lost response, the second call returns the first policy and writes nothing.
   - Alternative: re-plan silently and record. The Operator might then confirm one replacement and get another.
   - Alternative: pass the store's `policy-overlap`, `policy-supersession-invalid`, and `policy-unknown` through. The page would need to know store internals to explain them.
10. **Position on `execute-processing-job@2`: processing should read the requested timeline.** `@5` Decision 8 leaves open which timeline processing reads while a policy is pending.
    - **Recommended:** `execute-processing-job@2` reads `fallback_timezone_timeline()`, the live requested policies, after its claim commits (`@5` residual). Outcomes are keyed by policy identity, and nothing publishes before `@6` activates a policy with its results. So reading requested policies only produces candidate outcomes. This is the only position under which the reprocessing that `@5` queues does anything before `@6`.
    - Alternative: processing reads only active policies. Nothing is active until `@6`, so every queued job would repeat Policy-Pending, and the reprocessing would have to run again after `@6`.
    - **Dependency:** this unit does not depend on `execute-processing-job@2`, and `execute-processing-job@2` need not depend on this unit in Vouch: it reads the store, and its tests can record policies through the store. But the Portal page that calls this unit should ship only after `execute-processing-job@2` is deployed. Until then, the jobs a policy queues run under `execute-processing-job@1`'s empty timeline. They record Policy-Pending again, and nothing re-queues them until the next policy write. Phase 7 follows Phase 4, so the ordering is natural. It should still be a Beads dependency of the Phase 7 page.
11. **This unit's failures are its own; the store's pass through.** Request problems raise `FallbackTimezoneRefusal` with this unit's names, so the page can explain them. Problems with the caller's reprocessing settings, and `store-busy` and `storage-reserve-exhausted`, raise the store's exceptions unchanged, as `execute-processing-job@1` Decision 13 chose. Every failure's text is the failure name only. It names neither the zone nor any time, though neither is health data.
    - Alternative: wrap every store failure in this unit's type. That is more uniform, but it renames failures the Portal's runtime already handles.

## Ground rules

- **Data.** Python only: the standard library, the pinned `tzdata`, and the published store. Tests use real Operational Store databases in temporary directories. Source Records, selections, and outcomes are built through `@3`'s and `@4`'s own operations, as `@5`'s suite does. Every value is synthetic.
- **Database checks.** "Writes nothing" compares the whole database, with rowids, using the `@5` suite's snapshot method. "One write" replays the same store call on a copy, as `execute-processing-job@1` does.
- **Faults.** `storage-reserve-exhausted` comes from the store's own reserve check with a zero free-space reading. `store-busy` comes from a real write lock held by another SQLite connection. A delegating store (`Interleaved`) writes through a second store handle between this unit's read and its write. Abort triggers fail the store's write partway through.
- **Markers.** Synthetic markers in Source Identities, origins, and projection values show that no plan, result, failure, or traceback carries them.
- **Unbound inputs, reviewed rather than frozen.** The binding is the contract, this spec, the suite, the runner, and the effects declaration. These stay outside it:
  - `docs/governance/scope-map.md`, which R1's scope-map test reads;
  - the published code, contracts, and packs of `operational-store` and `interpret-source-projection@1`, which their own bindings freeze. R2's integration check uses `interpret` and a published processor;
  - the execution environment: `uv`, `pyproject.toml`, `uv.lock` with `tzdata==2026.6`, and the managed Python and SQLite;
  - `tests/governance/test_set_fallback_timezone_policy_contract.py`, which the governance suite runs and this runner does not.

## Freeze prerequisites

After Gate A, the executable pack adds:
- **The suite.** `tests/acceptance/test_set_fallback_timezone_policy_1.py` has 45 tests, one class per requirement, R1–R12. They are exactly the 45 proposed names above, with none added.
- **The runner.** `tests/acceptance/run_set_fallback_timezone_policy_1.sh` runs only this suite. The store's suites run under their own bound runners.
- **No fixture files.** Each test builds its store through the published `operational-store@5` API. Source Records, selections, jobs, and hand-written outcomes come from `@3`'s and `@4`'s operations.
- **The Interface stub,** `src/activity_tracker_portal/set_fallback_timezone_policy.py`:
  - real frozen `FallbackTimezonePlan` and `FallbackTimezoneResult` dataclasses, and the real `FallbackTimezoneRefusal` exception, whose `args` is `(failure,)`;
  - two real constants: `FAILURES`, this unit's seven names, and `EXCLUDED_ZONES`, which is `{"Factory"}`;
  - `available_zones`, `plan_fallback_timezone_policy`, and `set_fallback_timezone_policy`, each raising `NotImplementedError("set-fallback-timezone-policy@1")`.

**RED, through the runner, against the stub.**
- 3 tests pass: the R1 tests. They read only the contract, the effects declaration, the scope map, and the stub's `FAILURES`.
- 42 tests fail, and every failure is the stub's `NotImplementedError("set-fallback-timezone-policy@1")`. No failure has another cause. unittest prints `FAILED (errors=47)` because it counts subtests separately.

In the worktree, with the stub, these all pass:
- `run_operational_store_5.sh`, which chains `@1`–`@4`;
- `run_interpret_source_projection_1.sh`;
- `tests/unit` (135 tests) and `tests/governance` (57 tests).

**Passability.** A throwaway reference, kept out of the repository, passes all 45 tests. It:
- reads the zone list from the `tzdata` package's `zones` resource;
- plans as R3–R6 describe;
- in `set`, checks the request, reads the timeline once, returns a live identical plan as a repeat, plans again, and compares the result's `supersedes` with the plan's;
- writes with one `record_fallback_timezone_policy` call, and maps the store's three concurrent-change refusals to `plan-stale`.

**Mutants.** The drafter ran 42 wrong implementations against the reference, and the suite kills all 42. The first 36 were run before the implementer review, 5 more after it, and 1 after Gate B: `raise FallbackTimezoneRefusal("plan-stale") from None` inside the store-refusal handler, which keeps the store exception as a suppressed `__context__` and so does not meet R12. Two of the first 36 survived the first run, and the suite was strengthened to kill them:
- a zone list taken from `zoneinfo.available_timezones()` and cached before the test changed the host path. R2 now also runs a fresh process with `PYTHONTZPATH` pointing at a synthetic host database;
- a repeat that calls the store, which writes nothing on an identical repeat. R9 now confirms a repeat while another connection holds the write lock.

The mutants cover:
- **Zones:** checked against the host database, by `ZoneInfo(name)` or `available_timezones()`; matched case-insensitively; checked by file existence with the store's form check (which accepts `leapseconds`, and case variants on macOS); links canonicalised to their target; links refused; and `Factory` allowed.
- **Start and request time:** a future start allowed; the future compared with the wall clock instead of `at`; the start kept in its local offset; an out-of-range start left as an `OverflowError`; and an out-of-range `at` left to the store.
- **Timeline:** a first policy with a start allowed; every later policy replaced automatically; a same-start policy not replaced; an earlier policy replaceable; `replacing` left unsorted; and the all-history check placed after the replacement check.
- **Unchanged requests:** an unchanged request recorded; and "unchanged" judged by the zone that applies at the start rather than a policy starting there.
- **Stale plans:** a stale plan recorded; a stale plan silently re-planned; the store's concurrent-change refusals passed through; `policy-unknown` alone passed through; and `plan-stale` raised with the store's refusal as its context, unsuppressed or suppressed with `from None`.
- **Repeats:** no repeat check; a repeat written through the store; a repeat reporting counts; `repeated` always false; a concurrent repeat not flagged; and a forged repeat whose `replacing` is not within its `supersedes` accepted.
- **Store failures and settings:** store failures swallowed; retryables wrapped; settings refusals wrapped; reprocessing settings replaced with the worker's defaults; valid attempt bounds capped at 7; every version rewritten to `@1`; and the request not checked again in `set`.
- **Surfaces:** the zone leaked into a refusal's `args`; a log record written; and planning that reads a store method other than the timeline.

**Refinements.** The tests pin the refinements below beyond the approved text. None changes approved semantics. Items that settle a point the text left open are marked *settles*.
- **Zones (R2).**
  - The list is the package's `zones` resource minus `EXCLUDED_ZONES`, returned as a sorted tuple. The suite compares it with that resource, not with a fixed count.
  - These are refused as `timezone-unknown`:
    - whitespace or a newline around a name, `./` segments, a backslash, and a leading or trailing `/`;
    - a non-string zone, including bytes and a tuple;
    - every non-zone file in the package (`zone.tab`, `zone1970.tab`, `zonenow.tab`, `iso3166.tab`, `tzdata.zi`, `__init__.py`);
    - `localtime` and `posixrules`.
  - A name that exists only in a host database is refused, both in-process (after `zoneinfo.reset_tzpath`) and in a fresh process started with `PYTHONTZPATH`. Emptying the host database does not affect a pinned name.
- **Times (R3).**
  - A non-`datetime` `at`, including `None`, a `date`, and a string, is `timestamp-invalid`.
  - **For the approver (drafter's resolution of review finding 5):** an aware `at` whose UTC value is out of range (`0001-01-01T00:30+01:00`, `9999-12-31T23:30-01:00`) is `timestamp-invalid`, in both plan and set.
  - A start equal to `at`, written in another offset, is accepted. A start one microsecond after `at` is refused.
  - A start in year 1 is accepted when its UTC value is still in range (`0001-01-01T00:30+00:30`). A start whose UTC value is out of range, below (`0001-01-01T00:30+01:00`) or above (`9999-12-31T23:30-01:00`), is `effective-time-invalid`.
  - The plan's start has `tzinfo` exactly `timezone.utc`.
- **Refusal order (R3).** `at`, zone, start, and the form of `replacing` come first. Then, among the timeline checks: `all-history-policy-required` before a `replacing` entry that is not live, and that before `policy-unchanged`.
- **`replacing` (R5).** These are `replacement-invalid`:
  - a `bool`, `float`, `str`, `0`, a negative, or `2**63` element;
  - a set, a list, a scalar, or `None` in place of the tuple;
  - a Source Selection Policy's identity, a superseded policy, and a duplicate.

  Input in any order is planned ascending. An all-earlier-history request may name every later policy, leaving one policy in the timeline.
- **Unchanged (R6).**
  - *Settles:* only a live policy starting at exactly the same start counts. A zone that an earlier policy already gives, such as Asia/Kolkata from March under an all-history Asia/Kolkata, is a new policy, not `policy-unchanged`.
  - A link name for the zone that already applies at that start (`Europe/Amsterdam` where `Europe/London` starts) is a change.
- **Planning (R7).** A view that offers only `fallback_timezone_timeline` is enough to plan, so planning reads nothing else.
- **Confirmation (R8).**
  - **For the approver (drafter's resolution of review finding 4):** a plan must be internally consistent: `replacing` must be a subset of `supersedes`, or it is `replacement-invalid`, checked before repeat detection. So a forged repeat such as `replacing=(999,)` with `supersedes=()` is refused. Beyond that, repeat detection compares the recorded write content: zone, start, and `supersedes`.
  - When re-planning fails a timeline check (R4–R6), the result is `plan-stale` with no other name. This also applies to a hand-built plan that never matched, for example a first plan with a start on an empty timeline.
  - A `supersedes` that is a list rather than a tuple is `plan-stale`.
- **Repeats (R9).**
  - A repeat ignores the reprocessing settings: a different mapping or bound is still a repeat.
  - *Settles:* a repeat is recognised only while the recorded policy is live. Once a later request replaces it, the old plan is `plan-stale`.
  - *Settles:* a repeat reads and never writes. It succeeds below the reserve and while another connection holds the write lock.
  - On a concurrent repeat, the returned policy's `requested_at` is the other call's time.
- **Concurrent change (R9).** Two interleavings are injected between the read and the write: a policy with the plan's start (the store's `policy-overlap`), and the supersession of a policy the plan names (`policy-supersession-invalid`). Each gives `plan-stale` under the context rule in Surfaces below, with exactly one store write attempt, and the database exactly as the injected write left it. `policy-unknown` cannot come from a real write, because policies are never deleted, so a delegating store raises it at the write seam; it too gives `plan-stale`.
- **Store failures (R10).** Each of the store's refusals keeps its exact type, `StoreRefusal`, and the same holds for `StoreRetryable`. A `max_attempts` of 101 is `processing-request-invalid`. The bounds 1 and 100 reach the store unchanged, and so does a version that is not `@1` (`interpret-heart-rate@3`, over a record whose latest outcome is under it); each queued job keeps them. A fault injected partway through the store's write propagates as the store's `sqlite3.Error` and commits nothing.
- **Surfaces (R12).**
  - **Exception context, as approved in R12 (restored after Gate B).** Every `FallbackTimezoneRefusal` has `__cause__` `None`, and a `__context__` that is `None` or carries no other exception's text. A store exception the unit handled must not stay attached, suppressed or not. So `raise ... from None` inside the store exception's handler does **not** meet R12: the implementer must raise the refusal outside the handler, for example by setting a flag in the handler and raising after it. Neither the formatted traceback nor any message in the chain carries a marker. An exception the caller is already handling when it calls the unit (ambient caller context) is out of scope: Python attaches it, and the unit cannot prevent that. No test calls the unit inside an `except` block.
  - Its text does not carry the zone.

**Implementer review resolved.** GPT 6.1 Sol reviewed the pack read-only at `b9838ed` and found no passability blocker: its independent implementation passed all 45 tests. The coordinator decided the following, and the suite still has 45 tests:
1. **Attempt bound.** R10 now passes the bounds 100 and 1 through affected records and checks each queued job's bound. Sol's `cap_bound_at_seven` mutant fails.
2. **Processor mapping.** R10 now has an affected record whose latest outcome is under `interpret-heart-rate@3`, and checks that the new job keeps that version. Sol's `force_version_one` mutant fails.
3. **Exception context.** The drafter first relaxed the rule to allow a suppressed store-failure context. GPT 6 Astra objected at Gate B that this weakens approved R12, so the approved guarantee is restored (see Surfaces above): no cause, and no context carrying another exception's text. Ambient caller context stays out of scope. Sol's `suppress_context` variant fails again.
4. **Forged repeats** (drafter's resolution, flagged above for the approver). `replacing` must be a subset of `supersedes`, checked before repeat detection. R8 adds three forged cases, including `replacing=(999,)` with `supersedes=()`. The hand-built case that dropped a named policy is now `replacement-invalid` rather than `plan-stale`. Sol's implementation needed this change; it needed no other.
5. **`at` range** (drafter's resolution, flagged above for the approver). An out-of-range aware `at` is `timestamp-invalid` in plan and set. The upper UTC-overflow start is tested.
6. **`policy-unknown`.** It is injected at the write seam and must give `plan-stale`. Sol's `ignore_unknown_race_mapping` mutant fails.
7. **Same-`at` concurrent repeat.** The residual is reworded as a disclosed limitation.

Sol's implementation, with only the subset check of item 4 added, passes all 45 tests. RED is unchanged: 3 pass and 42 fail, every failure the stub's `NotImplementedError` (`FAILED (errors=47)`).

**Gate B round 1 resolved.** GPT 6 Astra objected at `0dfa9d5` and accepted refinements (b) to (e): the subset check, the `at` and start range, the three unchanged and repeat settlements, and the residual wording.
1. **Exception context** (blocking). R12's approved guarantee is restored, as above. `assertCleanChain` now requires `__cause__` `None` and that no exception reachable from the refusal's `__context__` carries text: every exception reachable through `__cause__` or `__context__` links is visited once, so branches behind a cause are checked and cycles terminate, and each one's `str()` and every argument's `str()` are empty. Empty exceptions, such as `Exception("")` or a chain of empty exceptions, are allowed (Astra Gate B round 2: the round-1 restoration had required `args == ()` and no further links, which was stricter than R12). Astra's independent implementation, which uses `from None`, now fails that check, as intended. Astra's strict-context variant, the drafter's reference, and Sol's patched copy pass.
2. **Attribution.** Three test comments that said "Operator decision" now name the drafter's resolutions accepted at Gate B. No other Operator attribution in the suite or this spec goes beyond the recorded Gate A decisions.

The implementer reviews the pack read-only for tests that a correct implementation could not pass, and those findings are resolved before freeze. The approver then confirms at Gate B that the pack carries this draft, that RED is genuine, that the effects declaration matches the contract, and that the residuals below are complete.

## Residuals

Not mechanically provable here; the approver confirms these in implementation review:
- only published store operations are used: `read()` views, `fallback_timezone_timeline`, and `record_fallback_timezone_policy`. No SQL and no private store helpers;
- zones are read only from the `tzdata` package's resources, never through `ZoneInfo(name)`, `TZPATH`, or `available_timezones()`, which consult the host;
- no clock, no host timezone, and no logging;
- the R9 "change during the write" test injects one interleaving. It cannot prove every interleaving; the store's single write transaction does.
- `repeated` can be wrong in one case, a disclosed limitation: another call records the identical plan after this call's read, with exactly the same `at`. Two retries that reuse one caller timestamp can produce this. The published store's result does not say whether it inserted or returned an existing policy, so the two cases cannot be told apart through the allowed operations. The test injects a different `at`, so it does not prove every concurrent repeat is flagged.

Left to other units:
- the Portal page: proposing a zone from the browser, showing the plan, the confirmation, and CSRF: Phase 7;
- which timeline processing reads while a policy is pending: `execute-processing-job@2` (Decision 10);
- policy activation, the active timeline, Cutover Records, and summary rebuilding: `operational-store@6` and the summary units;
- withdrawal without a replacement: a later store version and a later version of this unit;
- a tzdata upgrade must keep every recorded zone name loadable, or reinterpret under a governed change (ADR 0035);
- erasing policies under privacy deletion: `operational-store@7`.
