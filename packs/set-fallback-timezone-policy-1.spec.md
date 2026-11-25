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
1. checks `at`, and the plan's zone, start, and `replacing` form, as R2 and R3 do;
2. reads the timeline in one read view;
3. if a live policy already has the plan's zone, start, and `supersedes`, returns it with `repeated` true and zero counts, and writes nothing (R9);
4. plans the same request again against that read. If the result differs from the given plan, or the request now fails R4, R5, or R6, it raises `plan-stale`;
5. records the policy with `record_fallback_timezone_policy(plan.zone, plan.effective_from, plan.supersedes, processor_versions, max_attempts, at)`.

The result's `policy` is the store's record: live, pending, with `requested_at` equal to `at`. A plan built by hand cannot get round R2–R6: step 1 or step 4 refuses it.

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
- **Faults.** A delegating store injects `store-busy`, `storage-reserve-exhausted`, and a policy written between this unit's read and its write.
- **Markers.** Synthetic markers in Source Identities, origins, and projection values show that no plan, result, failure, or traceback carries them.
- **Unbound inputs, reviewed rather than frozen.** The binding is the contract, this spec, the suite, the runner, and the effects declaration. These stay outside it:
  - `docs/governance/scope-map.md`, which R1's scope-map test reads;
  - the published code, contracts, and packs of `operational-store` and `interpret-source-projection@1`, which their own bindings freeze. R2's integration check uses `interpret` and a published processor;
  - the execution environment: `uv`, `pyproject.toml`, `uv.lock` with `tzdata==2026.6`, and the managed Python and SQLite;
  - `tests/governance/test_set_fallback_timezone_policy_contract.py`, which the governance suite runs and this runner does not.

## Freeze prerequisites

After Gate A, the executable pack adds:
- the suite `tests/acceptance/test_set_fallback_timezone_policy_1.py`, one test per proposed name in R1–R12;
- the runner `tests/acceptance/run_set_fallback_timezone_policy_1.sh`, which runs only this suite. The store's suites run under their own bound runners;
- no fixture files. Each test builds its store through published operations;
- the Interface stub `src/activity_tracker_portal/set_fallback_timezone_policy.py`:
  - real frozen `FallbackTimezonePlan` and `FallbackTimezoneResult` dataclasses, and the real `FallbackTimezoneRefusal` exception;
  - `available_zones`, `plan_fallback_timezone_policy`, and `set_fallback_timezone_policy`, each raising `NotImplementedError("set-fallback-timezone-policy@1")`.

**RED, against the stub.** The three R1 tests pass, because they read only the contract, the effects declaration, and the scope map. Every other test fails, and every failure is the stub's `NotImplementedError`. `run_operational_store_5.sh`, `run_interpret_source_projection_1.sh`, `tests/unit`, and `tests/governance` still pass.

Before the read-only review, a throwaway reference implementation, kept out of the repository, passes every test, and a set of incorrect implementations each fails at least one. They cover at least:
- zones checked against the host database, by file existence (which ignores case on macOS), or by the store's form check only; links refused; `Factory` allowed;
- a future start allowed, a naive start allowed, and the start kept in its local offset;
- a first policy with a start allowed;
- every later policy replaced, a later policy kept when named, an earlier policy replaced, and a same-start policy kept;
- an unchanged request recorded;
- a stale plan recorded, a plan built by hand accepted, and the store's concurrent-change refusals passed through;
- a repeat that writes, or that reports counts, or a repeat refused as stale;
- reprocessing settings dropped or replaced;
- a store failure wrapped or swallowed, a refusal carrying the zone, and a log record written.

The implementer reviews the pack read-only for tests that a correct implementation could not pass, and those findings are resolved before freeze. The approver then confirms at Gate B that the pack carries this draft, that RED is genuine, that the effects declaration matches the contract, and that the residuals below are complete.

## Residuals

Not mechanically provable here; the approver confirms these in implementation review:
- only published store operations are used: `read()` views, `fallback_timezone_timeline`, and `record_fallback_timezone_policy`. No SQL and no private store helpers;
- zones are read only from the `tzdata` package's resources, never through `ZoneInfo(name)`, `TZPATH`, or `available_timezones()`, which consult the host;
- no clock, no host timezone, and no logging;
- the R9 "change during the write" test injects one interleaving. It cannot prove every interleaving; the store's single write transaction does.
- `repeated` can be wrong in one case: another call records the identical plan after this call's read, with exactly the same `at`. A single Operator cannot practically do this.

Left to other units:
- the Portal page: proposing a zone from the browser, showing the plan, the confirmation, and CSRF: Phase 7;
- which timeline processing reads while a policy is pending: `execute-processing-job@2` (Decision 10);
- policy activation, the active timeline, Cutover Records, and summary rebuilding: `operational-store@6` and the summary units;
- withdrawal without a replacement: a later store version and a later version of this unit;
- a tzdata upgrade must keep every recorded zone name loadable, or reinterpret under a governed change (ADR 0035);
- erasing policies under privacy deletion: `operational-store@7`.
