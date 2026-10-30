# Draft acceptance pack — `coordinate-sender-sync@3`

**Status:** Draft approved by the Operator on 2026-10-30, including the five decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `coordinate-sender-sync` Governed Scope contract, version 3, in `governance/scopes/coordinate-sender-sync.3.json`. It is the additive successor to the published `coordinate-sender-sync@2`, which it supersedes.

## Why a successor

`@1`'s R6 stages a check without changes as an empty Change Cycle: one empty Ingest Batch and its Change Completion per unchanged type and run. Its Decision 4 accepted that cost, because only `stage-outbox-batch@1` could persist the next token. It named the alternative: "a `stage-outbox-batch@2` primitive that records a no-change check without staging."

In the emulator smoke run on 2026-10-30 (Beads `hfj`, `gc5`), with all 41 projected types, every idle run staged 83 messages. The Sender shell delivers at most 64 after each run, so the Outbox grew by 19 messages every run, foreground or background, and never caught up. On 2026-10-30 the Operator chose to stop staging those empty cycles, keeping the catch-up cycle after every snapshot. `stage-outbox-batch@2` adds the primitive, `advanceChangeOrigin`. This successor uses it.

## Interface

Everything in `@2`'s Interface is unchanged. `createSenderSync(options)` accepts one more optional option:

```ts
quietChangeReads?: boolean
```

It defaults to `false`, under which the coordinator behaves exactly as `@2`, including `@1`'s R6. The Sender shell sets it to `true` (Beads `2rv.18`). There is no new migration and no new export.

## Terms

- **Quiet read:** the first read of a new Change Cycle, under the fresh identity of R5's third step, that returns no items and no further page.

## Requirements

`@2`'s requirements R1–R28 carry over unchanged when `quietChangeReads` is `false` or absent. The `@3` pack re-runs `@2`'s bound runner unchanged, which itself re-runs `@1`'s, so every `@1` and `@2` test must still pass exactly.

The new requirements are bound to a new TypeScript suite, `sender/app/test/coordinate-sender-sync-3.test.ts`. It uses `@1`'s synthetic sources fake, the 3ug substitutes, and controllable clocks, and opens and decodes every staged message it asserts on. A new Python suite, `tests/acceptance/test_coordinate_sender_sync_3.py`, checks the contract.

### R29 — Governed Scope contract, version 3

The `@3` contract keeps `@2`'s inputs with their modes, and its outputs, named failures, outcome classes, effects, and authority. It names quiet reads in the source-reader input and the staged-progress output. Its dependencies are `@2`'s, with `stage-outbox-batch` and `stage-sync-checkpoint` at version 2. The scope-map row names quiet reads, and still satisfies every published test that pins it.

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R30 — Caller errors

`createSenderSync` throws `TypeError` for a `quietChangeReads` that is neither `undefined` nor a boolean, before any database use.

Tests: `malformed_quiet_option_is_a_caller_error`.

### R31 — A quiet read advances the origin and stages nothing

With `quietChangeReads` true, the coordinator offers a quiet read to `stage-outbox-batch@2`'s `advanceChangeOrigin` instead of staging it. The result decides what happens:
- **`advanced`:** nothing is staged, neither an Ingest Batch nor a Change Completion, and no completion is ever owed for the read. The type is caught up for this run with outcome `no-change` and `sourceDataStaged` false. As after a staged page:
  - it moves to the tail of the round-robin ring;
  - any block on the type is cleared;
  - it takes no further turn in the run.
- **`catch-up-unstaged`:** the read is the type's catch-up cycle, and is staged with `stageOutboxBatch` exactly as in `@2`, followed by its Change Completion.
- **Any other refusal, or `storage-failed`:** handled exactly as `@2` handles the same refusal or failure from `stageOutboxBatch`.

Reads that are not quiet are staged exactly as in `@2`. That covers a read with items, an empty read with a further page, and any later page of an open cycle, even an empty final one. A type's next new Change Cycle reads from the advanced origin. As `@1`'s R5 begins a new Change Cycle each run, every run's change read uses a fresh identity: an advanced identity is never reused.

Tests:
- `a_quiet_read_stages_nothing`
- `the_catch_up_cycle_is_still_staged`
- `a_quiet_type_reports_no_change`
- `a_quiet_read_moves_the_type_to_the_ring_tail`
- `a_quiet_read_clears_a_retried_block`
- `the_next_cycle_reads_from_the_advanced_origin`
- `reads_with_items_or_more_pages_are_staged`
- `empty_pages_that_are_not_quiet_are_staged`
- `each_quiet_run_uses_a_fresh_cycle_identity`
- `advance_refusals_are_handled_like_staging_refusals`, for a storage failure and for a refusal after the attempt is replaced mid-read

### R32 — An idle run stages only its checkpoint

When every managed type is established and its quiet read finds nothing, a run stages exactly one Outbox Message: its Sync Checkpoint, reporting every type as `no-change`. Across ten such runs the Outbox gains exactly ten messages. The tokens still advance on every run, so a quiet type's token stays fresh (`@1`'s R6 intent).

Tests: `an_idle_run_stages_only_its_checkpoint`, `quiet_types_keep_their_tokens_fresh`.

### R33 — Deadlines, conditions, and recovery

- **Deadlines and conditions.** A quiet read already in flight when the soft deadline passes, or when a condition such as collection-disabled appears, is still advanced, as `@2` still stages a read in flight (R10, R11). It owes nothing afterwards.
- **Recovery.** A run interrupted after an advance is recovered as interrupted (R4), with no completion staged for the quiet read. A run interrupted before the advance commits leaves the origin unchanged, so the next run reads again from it.

Tests: `an_in_flight_quiet_read_is_advanced_after_the_deadline`, `recovery_stages_no_completion_for_a_quiet_read`, `a_crash_before_the_advance_rereads_from_the_old_origin`.

### R34 — The option off is exactly `@2`

With `quietChangeReads` false or absent, a quiet read is staged as an empty Change Cycle with its completion, exactly as `@1`'s R6 requires. Given the same initial state, clocks, identities, and source responses, the database equals the one `@2` leaves, with sealed messages compared after opening them.

Tests: `without_the_option_quiet_reads_are_staged_as_in_r6`.

## Decisions for the Operator

1. **Opt-in, so the published behaviour stays intact.** `@1`'s R6 tests require an empty Change Cycle per unchanged type, and `@2` re-runs them. An option keeps every published test valid and lets the shell switch quiet reads on. *Alternative:* making quiet reads the only behaviour, which would rebind `@1` and `@2` with changed semantics.
2. **The catch-up cycle is always staged.** `stage-outbox-batch@2` refuses a quiet advance until a Change Cycle under the current attempt has finished. The coordinator then stages the read as before, so every snapshot still gets its catch-up Change Completion (ADR 0047).
3. **`no-change` still reports the poll.** The Portal learns that each type was checked from the Sync Checkpoint's per-type outcome, which is unchanged. It no longer receives empty Ingest Batches and Change Completions for unchanged types. No published Portal unit needs them: `accept-sync-checkpoint@1` reads outcomes from the checkpoint, and completion-driven publication is unbuilt.
4. **Supersession.** Vouch supersedes `@2` when `@3` is specified. Its published dependents keep their packs, because `@3`'s interface is a superset of `@2`'s: `schedule-background-polling@1` and `notify-operator-conditions@1`. If Vouch requires it at publication, their packs are rebound with `rebind-pack`, with no change in behaviour. Implementation waits until `stage-outbox-batch@2` and `stage-sync-checkpoint@2` publish.
5. **Delivery stays in the shell.** Delivering until the Outbox drains is the shell's enabling change (Beads `2rv.18`), not this unit's. With quiet reads, an idle run stages one message, so even one delivery pass catches up.

## Ground rules

`@2`'s ground rules apply:
- fixtures are synthetic;
- writes-nothing and rollback assertions compare the whole database;
- source calls yield a macrotask;
- crashes are simulated by hanging storage;
- the runner sets a per-test timeout.

Fixtures migrate with an explicit list of migrations, including `stage-outbox-batch@2`'s version 10, never the production `senderMigrations`.

## Freeze prerequisites

The Operator approved the draft. The executable pack adds:
- the TypeScript suite `coordinate-sender-sync-3.test.ts`, 17 tests;
- the Python contract suite `tests/acceptance/test_coordinate_sender_sync_3.py`, 2 tests;
- the runner `tests/acceptance/run_coordinate_sender_sync_3.sh`. It runs the version 3 contract check, then `@2`'s bound runner unchanged, which runs `@1`'s, then the `@3` suite under the same process-group supervisor and a 60-second per-test timeout;
- the Interface stub: the optional `quietChangeReads` field on `SenderSyncOptions`, a type with no behaviour.

The suite migrates with an explicit list of all ten Sender migrations to version 10. It drives the published `stage-outbox-batch@2` through the coordinator, and the branch has merged it from `main`.

The published `@2` module, with the stub, is the RED baseline. It ignores the option, so:
- 8 new tests fail: the caller error, staging nothing, clearing a retried block, the advanced origin, refusals handled like staging refusals, an idle run's single checkpoint, the in-flight deadline and condition, and recovery;
- 9 pass by design, because they pin behaviour the quiet path must keep:
  - the catch-up cycle;
  - `no-change` reporting;
  - ring rotation;
  - staging of reads with items;
  - staging of empty pages that are not quiet;
  - a fresh cycle identity each run;
  - token freshness;
  - rereading after a crash before the advance;
  - the option off being `@2`;
- `@2`'s 12 and `@1`'s 76 TypeScript tests pass;
- all three Python contract suites pass.

**The reference run.** A throwaway reference offers a quiet read to `advanceChangeOrigin`:
- it falls back to staging on `catch-up-unstaged`;
- it maps every other non-advance like a staging result;
- it moves the type to the tail and clears its block;
- it retires the read's cycle identity, so the next run begins a fresh one;
- it marks the type caught up.

The reference passes all 17 new tests, `@2`'s runner with `@1`'s 76 and `@2`'s 12, and the whole Sender suite (771 and 398).

Seven mutants each fail at least one test:
- a catch-up refusal failing the run;
- the quiet path ignoring the option;
- a quiet read keeping its ring place;
- a quiet read keeping the block;
- non-catch-up refusals counted as success, Sol's first;
- the quiet read's identity retained, Sol's second;
- no first-page restriction, Sol's third.

The binding is:
- **spec files:** this contract and pack, and `@2`'s and `@1`'s contracts and packs;
- **test files:**
  - `coordinate-sender-sync-3.test.ts`, `test_coordinate_sender_sync_3.py`, and the `@3` runner;
  - every file `coordinate-sender-sync-pack/2` binds: `coordinate-sender-sync-2.test.ts`, `test_coordinate_sender_sync_2.py`, `run_coordinate_sender_sync_2.sh`, and every file `coordinate-sender-sync-pack/1` binds;
  - the new suite also uses `support/synthetic-sync-sources.ts` and the 3ug substitutes, which `@1`'s binding already covers.

**Gate B amendment.** GPT 6 Astra approved the freeze with one documentation amendment, made: the counts above were corrected to 17 tests, 8 failing and 9 passing at RED. One of the review's gaps extended an existing test rather than adding one.

**Implementability review.** GPT 6.1 Sol's read-only review found nothing blocking: Sol's scratch implementation passed the runner and the whole Sender suite. A mutant survived each of its three coverage gaps, which are now tests:
- a real advance refusal, after the attempt is replaced mid-read, handled as `sync-state-mismatch` exactly as staging handles it, with recovery afterwards;
- a fresh cycle identity for each run's quiet read, which R31 now states;
- empty pages that are not quiet, an empty first page with more to come and an empty final later page, staged normally.

Implementation waits until `stage-outbox-batch@2` and `stage-sync-checkpoint@2` publish. The implementation adds the runner to the project CI `test` job.
