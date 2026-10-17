# Draft acceptance pack — `coordinate-sender-sync@2`

**Status:** Draft approved by the Operator on 2026-10-17, including the six decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `coordinate-sender-sync` Governed Scope contract, version 2, in `governance/scopes/coordinate-sender-sync.2.json`. It is the additive successor to the published `coordinate-sender-sync@1`, which it supersedes.

## Why a successor

`@1` measures a run's budget from the moment the run opens, after recovery (R3, R4, R10). Its R8 merges triggers that arrive during a run into one follow-up, which has no budget if any merged trigger had none, and otherwise the latest budget. Both rules are right for a budget, but neither can express the background window: the eight minutes `schedule-background-polling@1` grants a background callback, counted from when the callback starts.

The approver's review of the Sender shell's delivery loop (2rv.9, PR #41) found that a background callback overruns its window in three ways:
- it waits behind an active run, and its budget starts only when its follow-up opens;
- recovery and the run's opening observations take time before the budget starts;
- it merges with a foreground or manual trigger, and R8 removes its budget.

The shell cannot correct this from outside. Holding background triggers back breaks R8's coalescing. Refusing source calls after the window records the stop as `read-failed` instead of R10's `time-budget-reached`. On 2026-10-16 the Operator chose to narrow the shell task and fix the gap in this successor.

## Interface

Everything in `coordinate-sender-sync@1`'s Interface is unchanged. The module, its options, its results, `requestRetry`, `typeBlock`, and `syncMigration` keep their exact signatures and behaviour, and there is no new migration.

`SenderSync.trigger(request)` accepts one more optional field:

```ts
{ kind: "background" | "foreground" | "manual", budgetMs?, deadlineMs?, priority? }
```

`deadlineMs` is an absolute deadline on the coordinator's `monotonicNow()` clock. A trigger may carry a budget, a deadline, both, or neither. A trigger with neither is **unbounded**.

## Requirements

`coordinate-sender-sync@1`'s requirements R1–R22 carry over unchanged for every trigger without `deadlineMs`. The `@2` pack re-runs `@1`'s TypeScript suite unchanged, so every `@1` test must still pass exactly.

The new requirements are bound to a new TypeScript suite, `sender/app/test/coordinate-sender-sync-2.test.ts`. It uses `@1`'s synthetic sources fake, the 3ug substitutes, and controllable clocks, and opens and decodes every staged checkpoint it asserts on. A new Python suite, `tests/acceptance/test_coordinate_sender_sync_2.py`, checks the contract.

### R23 — Governed Scope contract, version 2

The `@2` contract keeps `@1`'s 8 named failures, outcome classes, dependencies, effects, and input and output names. It adds the absolute deadline to the trigger input, the clocks, the checkpointed-run output, and the soft-deadline authority. The scope-map row names the deadline and still satisfies `@1`'s row checks.

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R24 — Caller errors

`TypeError` is thrown, with nothing written and no source call made, for a `deadlineMs` that is not a safe integer. A deadline may already have passed. A deadline is allowed on any trigger kind.

Tests: `malformed_deadlines_are_caller_errors`.

### R25 — The effective deadline

A run's deadline is the earliest of:
- its start plus its budget, measured from the run's opening exactly as in `@1`;
- its absolute deadline.

A run with neither has no deadline. Time spent before the run opens counts against an absolute deadline, but not against a budget. That time includes waiting behind an active run, recovery (R4), and reading the context and the Background Scheduling Status.

At the effective deadline, R10 applies unchanged:
- no source call starts;
- a read already in flight is staged;
- a completion owed by a staged final page is still staged.

A type that did work but is not caught up is `progressed` with `time-budget-reached`. A type that did no work is `deferred` with `time-budget-reached`. A deadline stop is never reported as `read-failed`.

Tests:
- `an_absolute_deadline_bounds_the_run`
- `the_earlier_of_budget_and_deadline_applies`
- `waiting_and_recovery_count_against_the_deadline`
- `deadline_stops_report_time_budget_reached`, which covers token issuance, a snapshot read, and a change read, both before and after the type did work
- `an_in_flight_read_is_staged_after_the_absolute_deadline`

### R26 — A run that opens past its deadline

A run whose absolute deadline has passed when it opens still records its run marker. It makes no source call, and checkpoints with every managed type `deferred` with `time-budget-reached`. Its trigger returns `checkpointed`, like any run.

Tests: `a_run_opening_past_its_deadline_reads_nothing_and_checkpoints`.

### R27 — Coalesced deadlines

R8 still decides the follow-up's kind and priority, and the active run is still never cancelled or shortened. The follow-up's limits are decided this way:
- If any merged trigger is unbounded, the follow-up has neither a budget nor a deadline.
- Otherwise, its budget is the latest budget among the merged triggers that have one, as in R8, and its deadline is the earliest deadline among those that have one.

For triggers without deadlines, this is exactly R8. Every merged caller receives the follow-up's result.

Tests:
- `a_follow_up_keeps_the_earliest_deadline`
- `an_unbounded_trigger_lifts_every_limit`
- `a_deadline_never_shortens_the_active_run`
- `budgets_still_merge_as_in_r8`

### R28 — Deadlines stay in memory

Deadlines are not persisted. The run marker, the coordinator's tables, and the checkpoint carry nothing new. A recovered run (R4) is checkpointed as interrupted whatever its trigger's deadline was.

Tests: `deadlines_write_no_new_state`.

## Decisions for the Operator

1. **An additive absolute deadline instead of redefining the budget.** `budgetMs` keeps `@1`'s meaning, so every `@1` caller and test is unaffected, and `deadlineMs` carries the window. The alternative is to measure `budgetMs` from the trigger call, which changes `@1`'s published behaviour and its tests.
2. **The earliest deadline wins, but an unbounded trigger lifts every limit (R27).** An unbounded trigger comes from a foreground return or a manual sync, while the app is open. Its run belongs to the Operator's session, and a background callback merged into it waits for that run, as in `@1`. The alternative is that a deadline always bounds the follow-up, which would cut a manual sync short at a background callback's window.
3. **A run that opens past its deadline still checkpoints (R26).** The background task then reports a checkpointed run whose outcomes say the window ran out, rather than a failure. The alternative is to settle the trigger without a run, with a new outcome.
4. **The active run is never shortened.** R8 is kept: a later trigger's deadline applies only to the follow-up. The alternative is to let a background deadline shorten the run already in progress, which `@1` forbids.
5. **The Sender shell supplies the deadline.** `schedule-background-polling@1` keeps triggering with its eight-minute budget. The shell's trigger seam adds `deadlineMs`, the callback's start on the monotonic clock plus that budget, so no successor of `schedule-background-polling` is needed. The shell task 2rv.9 then binds the deadline regressions written during PR #41 as shell tests. The alternative is a `schedule-background-polling@2` that passes the deadline itself.
6. **Supersession.** Vouch supersedes `@1` when `@2` is specified, after this draft is approved. The published dependents, `schedule-background-polling@1` and `notify-operator-conditions@1`, keep their packs, because `@2`'s interface is a superset of `@1`'s. If Vouch requires it when `@2` publishes, their packs are rebound with `rebind-pack`, as `manage-sender-installation@1`'s was, with no change in behaviour.

## Ground rules

`@1`'s ground rules apply:
- Fixtures are synthetic.
- Writes-nothing and rollback assertions compare the whole database.
- Source calls yield a macrotask.
- Crashes are simulated by hanging storage.
- The runner sets a per-test timeout.

Deadline tests drive the monotonic clock explicitly, and never depend on real time.

## Freeze prerequisites

Once the Operator approves the draft, the executable pack adds:
- the TypeScript suite `coordinate-sender-sync-2.test.ts`;
- the Python contract suite;
- a runner that runs `@1`'s TypeScript suite unchanged and then `@2`'s.

The published `@1` module is the RED baseline: it ignores `deadlineMs`, so the new tests fail against it, while `@1`'s suite and the Python tests pass. The binding is:
- spec files: the contract and this pack;
- test files: both TypeScript suites, `synthetic-sync-sources.ts`, `node-database.ts`, `node-installation-cipher.ts`, the Python suite, and the runner.

GPT 6.1 Sol's read-only implementability review runs before Gate B.
