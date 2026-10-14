# Draft acceptance pack — `coordinate-sender-sync@1`

**Status:** Draft approved by the Operator on 2026-10-15, including the fourteen decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `coordinate-sender-sync` Governed Scope contract in `governance/scopes/coordinate-sender-sync.1.json`.

It is the Sender's synchronization module: the one place that background, foreground, and manual triggers call (ADR 0018). It turns them into Polling Runs and closes each run with one Sync Checkpoint (ADR 0049). Within a run it decides each record type's next step and calls the published unit that owns that step:
- `begin-initial-snapshot@2` and `begin-reconciliation-snapshot@1` begin attempts;
- `read-snapshot-page@1` and `read-change-page@1` read pages, through TypeScript seams;
- `stage-outbox-batch@1` stages pages;
- `stage-sync-completion@1` stages completions;
- `stage-sync-checkpoint@1` stages the checkpoint.

The coordinator reads those units' state only through their exported functions, and never seals, reads Health Connect, or writes their tables itself. Its own durable state is:
- its runs;
- the round-robin ring;
- the Change Cycle identities it begins;
- its type blocks.

The unit is TypeScript only.

## Interface

**TypeScript**, module `sender/app/src/sync/coordinate-sender-sync.ts`. It uses:
- `begin-initial-snapshot@2`'s `beginInitialSnapshot`, `currentSnapshotAttempt`, `ChangesTokenIssuer`, and `IssuedToken`;
- `begin-reconciliation-snapshot@1`'s `beginReconciliationSnapshot`;
- `stage-outbox-batch@1`'s `stageOutboxBatch`, `cycleProgress`, `changeOrigin`, `StagingContext`, and `ChangeItem`;
- `stage-sync-completion@1`'s `stageSyncCompletion` and `cycleCompletion`;
- `stage-sync-checkpoint@1`'s `stageSyncCheckpoint`, `runCheckpoint`, `RunSummary`, `CheckpointTrigger`, and `BackgroundScheduling`;
- the protocol library's `messageSha256`;
- the 3ug storage and cipher seams.

The reader seams mirror the Kotlin results of the two readers:
- `SnapshotReadRequest`: `{ recordType, snapshotId, position: Uint8Array | null }`.
- `SnapshotRead` is one of:
  - `{ outcome: "page", projectionSchemaVersion, projections, nextPosition, exhausted }`;
  - `{ outcome: "projection-refused", failure, path }`, carrying `project-source-record@1`'s refusal;
  - `{ outcome: "not-permitted" | "read-failed" | "invalid-position", failure }`.
- `ChangeReadRequest`: `{ recordType, cycleId, changesToken, unreadSinceMs }`.
- `ChangeRead` is one of:
  - `{ outcome: "page", projectionSchemaVersion, items, nextChangesToken, nextUnreadSinceMs, hasMore }`;
  - `{ outcome: "projection-refused", failure, path }`;
  - `{ outcome: "token-expired" | "not-permitted" | "read-failed", failure }`.
- `SyncSources` extends `ChangesTokenIssuer` with `readSnapshotPage(request)` and `readChangePage(request)`.

The coordinator is created once per Sender process:
- `createSenderSync(options)` returns a `SenderSync`. Its options are:
  - `database`, `cipher`, and `sources`;
  - `recordTypes`: the managed record types;
  - `projection`: `{ implementationVersion, schemaVersions }`;
  - `context()`: the `StagingContext`, read when a run opens;
  - `conditions()`: `{ collectionEnabled, secureLock, deliveryBlocked, storagePressure }`, read before every source call and completion;
  - `backgroundScheduling()`: read when a run opens;
  - `concurrency`: optional, default 2;
  - `now()`, `monotonicNow()`, and `randomUUID()`.
- `SenderSync.trigger(request)`, where `request` is `{ kind: "background" | "foreground" | "manual", budgetMs?, priority? }`. `priority` lists record types and is allowed only on a manual trigger. It returns one of:
  - `{ outcome: "checkpointed", runSequence, summary }`, where `summary` is the `RunSummary` staged;
  - `{ outcome: "failed", failure }`.
- `SenderSync.requestRetry(recordType)` returns what `trigger` returns, or `{ outcome: "refused", failure: "retry-not-blocked" }`.
- `typeBlock(database, recordType)` returns `{ recordType, cause, failure, path, retryRequested }` or `null`. `cause` is `projection-refusal` or `record-too-large`; `path` is the refused field's JSON path, or `null` for an oversized record.
- `syncMigration`: Sender schema version 5.

## Terms

- **Step:** one of three things: beginning a snapshot attempt; reading and staging one page; or staging one completion.
- **Source call:** a reader call or a token issuance.
- **Turn:** one record type holding one pool slot for its steps (R6).
- **Caught up:** the type staged a Change Completion in this run.
- **Did work:** the type began an attempt, or staged a page or a completion, in this run.
- **Stop cause:** why a type that is not caught up has no further turn in this run.

## Requirements

TypeScript `node:test` suites in `coordinate-sender-sync-1.test.ts` use:
- the 3ug `node:sqlite` and `NodeInstallationCipher` substitutes;
- a synthetic fake of the reader and issuer seams, which models:
  - snapshot traversal with opaque positions;
  - a change log with tokens and unread-since instants;
  - permissions, refusals, and failures;
  - results the test releases one at a time, to control interleaving;
- controllable wall and monotonic clocks;
- a seeded UUID source.

Tests open each staged checkpoint with the test cipher and decode it with the protocol library. They migrate an explicit list to version 5. `tests/acceptance/test_coordinate_sender_sync_1.py` checks the contract. The runner verifies a fresh JUnit report.

### R1 — Governed Scope contract

The contract names:
- the scope, version, kind, and purpose;
- inputs, with their modes;
- outputs;
- 8 named failures in the outcome classes `refusal`, `conflict`, `state-mismatch`, `cipher-failure`, and `storage-failure`;
- three effects;
- authority;
- the eight dependencies;
- the acceptance oracle.

It does not contradict the scope-map row. This draft adds `ingest-protocol` to that row's dependencies (Decision 8).

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — Caller errors

`TypeError` is thrown, with nothing written and no source call made, for:
- malformed options;
- a managed record type that is malformed, duplicated, or missing from `projection.schemaVersions`;
- a trigger of unknown kind, or with a budget that is not a positive safe integer;
- a priority list on a non-manual trigger, or naming an unmanaged type;
- a retry for an unmanaged type;
- a wall-clock reading outside the protocol's instant range.

When a run opens, a managed type whose schema version the context's generation does not support is also a `TypeError`.

Tests: `malformed_options_are_caller_errors`, `malformed_triggers_are_caller_errors`, `unsupported_types_are_caller_errors`.

### R3 — The run marker

Before any source call, one transaction opens the run. It records:
- the run sequence, one above the installation's last, starting at 1;
- the trigger;
- the start time from `now()`;
- the Background Scheduling Status;
- the managed types.

Before a cycle's first read in the run, the coordinator records that cycle's staged item count, so the run's staged items are known from durable state alone.

The run closes with one checkpoint through `stage-sync-checkpoint@1`:
- the end time comes from `now()` at closing;
- `elapsedMs` is the `monotonicNow()` difference;
- every managed type appears once.

Tests: `the_run_is_recorded_before_any_source_call`, `run_sequences_count_per_installation`, `the_checkpoint_reports_the_run`.

### R4 — Recovery before reading

A run with no checkpoint is open. Before opening a new run, the coordinator checkpoints each open run as interrupted:
- every recorded type is `interrupted`, with no reason;
- its trigger, start time, and scheduling status are the recorded ones;
- its end time is the recovery time, and it has no `elapsedMs`;
- each `sourceDataStaged` comes from durable cycle progress, so it is exact across a crash.

If the interrupted checkpoint cannot be staged, the trigger returns that failure, and no new run opens or reads. A run whose checkpoint was staged before a crash is never checkpointed again.

Tests: `an_open_run_is_checkpointed_as_interrupted_before_reading`, `interrupted_runs_report_staged_data_exactly`, `a_failed_recovery_reads_nothing`, `a_checkpointed_run_is_not_recovered`.

### R5 — One type's traversal

On each turn, a type takes the first applicable step, in this order:
1. **No current attempt:** begin an Initial Snapshot.
2. **A finished cycle without its completion:** stage that completion. A Change Completion makes the type caught up, and the turn ends.
3. **Otherwise:** read and stage one page from the exact saved state:
   - an unfinished snapshot cycle reads from its saved position, or from `null`;
   - an unfinished Change Cycle reads from its saved token and unread-since;
   - otherwise a new Change Cycle reads from `changeOrigin`, under a fresh identity recorded before the read.

A page that finishes its cycle is followed in the same turn by that cycle's completion. A Snapshot Completion leads, on later turns, to its catch-up Change Cycle. Once caught up, a type has no further turn in that run. A later run begins a new Change Cycle.

Tests:
- `a_new_type_begins_an_initial_snapshot`
- `snapshot_pages_resume_from_the_saved_position`
- `a_finished_snapshot_is_completed_then_caught_up`
- `change_cycles_resume_from_the_saved_token`
- `a_pending_completion_is_staged_first`
- `each_run_begins_a_new_change_cycle`

### R6 — A check without changes

A change page with no items and no more pages is staged like any page, and its Change Completion follows. Staging is what persists the next token and unread-since. The type is caught up with outcome `no-change`.

Tests: `an_unchanged_type_stages_an_empty_change_cycle`, `an_unchanged_type_keeps_its_token_fresh`.

### R7 — Fair scheduling

Scheduling limits work as follows:
- At most one turn runs per type at a time.
- At most `concurrency` turns run at once. The default is two, and one is allowed.
- Types take turns in ring order:
  - a turn that staged a page moves its type to the tail;
  - the new order is durable before the next turn starts;
  - new managed types join the tail in ascending order.
- A type that is skipped or stops keeps its place, and claims no slot.
- Concurrent turns never lose a page or conflict with each other. Only source reads overlap: the 3ug seam allows one storage operation at a time, so the coordinator serializes every use of Sender storage, including the published units' calls.
- After a restart, the ring continues where it stood.

Tests:
- `one_turn_per_type_at_a_time`
- `turns_never_exceed_the_pool`
- `the_pool_size_is_configurable`
- `a_staged_page_moves_its_type_to_the_tail`
- `the_ring_survives_a_restart`
- `skipped_types_keep_their_place`
- `concurrent_turns_stage_every_page`

### R8 — Coalesced triggers

A `SenderSync` runs one run at a time. Triggers that arrive during a run are handled as follows:
- **The first one** becomes the single follow-up, which starts after the active run closes.
- **Later ones** merge into that follow-up:
  - its kind is the highest of manual, foreground, and background;
  - it has no budget if any merged trigger had none, and otherwise the latest budget;
  - its priority types accumulate in arrival order, without duplicates.
- **Every merged caller** receives the follow-up's result.
- **The active run** is never cancelled or shortened.

Tests: `overlapping_triggers_coalesce_into_one_follow_up`, `merged_triggers_keep_the_strongest_terms`, `a_trigger_never_cancels_active_work`.

### R9 — One-time manual priority

A manual run's priority types take its first turns, in order, once each. After its turn, a priority type joins the tail like any other type. The other types keep their relative order. Priority lasts only for that run.

Tests: `manual_priority_moves_a_type_forward_once`, `priority_does_not_persist`.

### R10 — Soft deadline

With a budget, once `monotonicNow()` reaches the run's start plus the budget:
- no source call starts;
- a read already in flight is staged;
- a completion owed by a staged final page is still staged.

The outcomes are:
- a type that did work but is not caught up is `progressed` with `time-budget-reached`;
- a type that did no work is `deferred` with `time-budget-reached`.

A run without a budget has no deadline.

Tests: `the_deadline_starts_no_further_source_call`, `an_in_flight_page_is_staged_after_the_deadline`, `unreached_types_are_deferred`.

### R11 — Collection disabled

Before every source call and every completion, the coordinator reads `conditions()`. While collection is disabled:
- no source call starts;
- a read already in flight is staged;
- no completion is staged;
- attempts, positions, and tokens are unchanged.

Each affected type is `deferred` with `collection-disabled`, or `progressed` with `collection-disabled` if it did work. The run still checkpoints. Re-enabling resumes at the exact saved position, and the withheld completion is staged first.

Tests:
- `a_disabled_run_reads_nothing_and_checkpoints`
- `disabling_mid_run_stages_the_in_flight_page`
- `no_completion_while_disabled`
- `re_enabling_resumes_the_exact_position`

### R12 — Other conditions

The three Operator-action conditions behave as collection-disabled does. Each type is `blocked` with:
- `screen-lock-removed` when the secure lock is gone;
- `storage-pressure` under storage pressure;
- `delivery-blocked` under a Delivery Block.

A type that did work is instead `progressed` with the same reason. When several conditions hold, the first in that order wins, and all three win over collection-disabled.

Tests: `operator_action_conditions_block_reads`, `condition_precedence_is_fixed`.

### R13 — Permission gaps

A `not-permitted` result from a reader or the issuer stops the type for the run. Nothing advances. The type is `not-permitted`, with no reason, or `progressed` with `permission-missing` if it did work. The next run tries the exact same saved position. Other types proceed.

Tests: `missing_permission_pauses_at_the_exact_position`, `regrant_resumes_the_same_position`, `permission_lost_after_progress_reports_progressed`.

### R14 — Lost continuity

An `invalid-position` snapshot read and a `token-expired` change read both escalate. Before the type's baseline is established, the coordinator begins a new Initial Snapshot that abandons the current attempt. The baseline is established once a Change Completion has been staged under an Initial attempt (Decision 7). After that, it begins a Reconciliation Snapshot that supersedes the current attempt. In both cases:
- the old position is never read again;
- the new attempt's first page follows on a later turn of the same run;
- a type that escalated on an expired token reports `progressed` with `changes-token-expired`, ahead of any later stop cause.

A begin that is not permitted or fails to read stops the type as in R13 and R18. A begin that is refused is a run failure (R19).

Tests:
- `an_invalid_position_before_baseline_restarts_the_initial_snapshot`
- `an_expired_token_before_baseline_restarts_the_initial_snapshot`
- `an_expired_token_after_baseline_reconciles`
- `an_invalid_position_during_reconciliation_reconciles_again`
- `expiry_is_reported_as_changes_token_expired`

### R15 — Projection Refusal blocks

A `projection-refused` read stages nothing, and blocks the type. The block's identity is:
- the record type;
- a SHA-256 digest of the saved page position: the snapshot identity and position, or the Change Cycle identity and token;
- the projection schema version;
- the projection-implementation version.

The block stores only that identity, the refusal's failure name, and its field path. The type is `blocked` with `projection-refusal`, or `progressed` with `projection-refusal` if it did work. Later runs skip a blocked type before it claims a slot, with no source call and the same outcome. Other types proceed.

Tests:
- `a_refused_page_blocks_only_its_type`
- `a_blocked_type_spends_no_slot`
- `block_identities_are_health_data_free`
- `an_identical_block_stays_blocked_across_runs`

### R16 — Projection Refusal retries

A blocked type becomes eligible again in two ways.

**A changed version tuple.** When its current identity differs from the stored one, the type gets one automatic retry.

**The Operator's request.** `requestRetry(recordType)`:
1. durably marks the block;
2. starts, or coalesces into, a manual run that gives the type one-time priority.

The retry then reads once, under the same tuple.

What follows depends on the retry's result:
- **Another refusal** stores the new identity, clears the request, and restores the block.
- **A staged page** clears the block, and traversal continues.
- **A `not-permitted` or `read-failed` retry** keeps the request for the next run.

A retry for a type with no Projection Refusal block is refused with `retry-not-blocked`. It writes nothing and starts no run.

Tests:
- `a_changed_version_tuple_retries_once`
- `an_operator_retry_reads_once_with_priority`
- `an_identical_repeat_restores_the_block`
- `a_successful_retry_clears_the_block`
- `a_transient_retry_failure_keeps_the_request`
- `retry_without_a_block_is_refused`

### R17 — Oversized records

`stage-outbox-batch@1`'s `indivisible-record-limit-exceeded` blocks the type with cause `record-too-large`. Its identity is the R15 tuple plus the capability generation. The type is `blocked` with `record-too-large`, or `progressed` with `record-too-large` if it did work. Later runs skip it until the identity changes, through a new generation or new projection versions. `requestRetry` does not apply to it.

Tests: `an_oversized_record_blocks_its_type`, `a_new_generation_retries_an_oversized_record`.

### R18 — Read failures and bridge payloads

A `read-failed` result stops the type for the run with `failed` and `read-failed`, or `progressed` with `read-failed` if it did work. The next run retries the same position.

Reader and issuer results are bridge payloads:
- a seam that throws reads as `read-failed`;
- so does a result with an unknown outcome, a failure name outside its reader's contract, a projection schema version other than the managed one, or a malformed field.

Tests: `read_failures_retry_next_run`, `malformed_reader_results_are_read_failures`, `throwing_seams_are_read_failures`.

### R19 — Run failures

These stop the run:
- a staging `cipher-failed` result, which is classified by its failure name;
- `storage-failed` from any unit or from this unit's own writes;
- a `refused` result from any staging or begin unit, which is `sync-state-mismatch`;
- a `conflict`, but only after two more attempts of the same staging call fail, which is `staging-conflict`.

Once the run stops:
- no new step starts;
- steps already in flight settle;
- no checkpoint is staged;
- the run stays open;
- `trigger` returns `{ outcome: "failed", failure }`.

The next trigger recovers the run under R4.

Tests:
- `cipher_failures_leave_the_run_open`
- `storage_failures_leave_the_run_open`
- `staging_conflicts_retry_twice`
- `dependency_refusals_are_state_mismatches`
- `a_failed_run_is_recovered_next_time`

### R20 — Outcomes and reasons

At closing, each type's outcome comes from the first matching row:

| The type in this run | Outcome | Reason |
|---|---|---|
| Caught up, having staged only empty change pages and their completion | `no-change` | none |
| Caught up otherwise | `completed` | none |
| Did work but not caught up | `progressed` | `changes-token-expired` if it escalated on an expired token, else its stop cause |
| Stopped by the deadline, collection-disabled, or a condition | `deferred` or `blocked`, per R10 to R12 | the stop cause |
| Blocked by a refusal or an oversized record | `blocked` | `projection-refusal` or `record-too-large` |
| Not permitted | `not-permitted` | none |
| A read failed | `failed` | `read-failed` |

`sourceDataStaged` is true when at least one projection or retraction of the type was staged in this run. `interrupted` is used only for recovered runs (R4). v1 never reports `not-due` (Decision 3). Every reported pair satisfies the protocol's outcome and reason pairing.

Tests: `every_outcome_row_is_reachable`, `source_data_staged_counts_items`, `reported_pairs_satisfy_the_protocol`.

### R21 — The Sender schema migration

`syncMigration` is Sender schema version 5, registered after `stage-sync-checkpoint@1`'s version 4. It leaves every earlier schema object unchanged. It creates exactly these tables, compared token by token with whitespace ignored, and nothing else besides their automatic indexes. It references no other unit's table.

```sql
CREATE TABLE sync_types (
  record_type TEXT PRIMARY KEY NOT NULL, ring_order INTEGER NOT NULL UNIQUE CHECK (ring_order > 0),
  change_cycle_id TEXT, change_snapshot_id TEXT, established INTEGER NOT NULL CHECK (established IN (0, 1)),
  CHECK ((change_cycle_id IS NULL) = (change_snapshot_id IS NULL))) STRICT;
CREATE TABLE sync_blocks (
  record_type TEXT PRIMARY KEY NOT NULL REFERENCES sync_types (record_type),
  cause TEXT NOT NULL CHECK (cause IN ('projection-refusal', 'record-too-large')),
  identity TEXT NOT NULL, failure TEXT NOT NULL, path TEXT,
  retry_requested INTEGER NOT NULL CHECK (retry_requested IN (0, 1)),
  CHECK ((cause = 'projection-refusal') = (path IS NOT NULL)),
  CHECK (cause = 'projection-refusal' OR retry_requested = 0)) STRICT;
CREATE TABLE sync_runs (
  installation_id TEXT NOT NULL, run_sequence INTEGER NOT NULL CHECK (run_sequence > 0),
  trigger TEXT NOT NULL CHECK (trigger IN ('background', 'foreground', 'manual')),
  started_at_ms INTEGER NOT NULL,
  background_scheduling TEXT NOT NULL CHECK (background_scheduling IN ('available', 'restricted', 'unregistered')),
  PRIMARY KEY (installation_id, run_sequence)) STRICT;
CREATE TABLE sync_run_types (
  installation_id TEXT NOT NULL, run_sequence INTEGER NOT NULL, record_type TEXT NOT NULL,
  PRIMARY KEY (installation_id, run_sequence, record_type),
  FOREIGN KEY (installation_id, run_sequence) REFERENCES sync_runs (installation_id, run_sequence)) STRICT;
CREATE TABLE sync_run_cycles (
  installation_id TEXT NOT NULL, run_sequence INTEGER NOT NULL, record_type TEXT NOT NULL, cycle_id TEXT NOT NULL,
  records_before INTEGER NOT NULL CHECK (records_before >= 0),
  PRIMARY KEY (installation_id, run_sequence, record_type, cycle_id),
  FOREIGN KEY (installation_id, run_sequence, record_type)
    REFERENCES sync_run_types (installation_id, run_sequence, record_type)) STRICT;
```

`sync_types` holds the ring, the Change Cycle each type began under its current attempt, and whether its baseline is established (R14). `sync_blocks` holds the type blocks. The three run tables hold each run's marker, types, and the staged item counts recorded before each cycle's first read in the run (R3).

Tests: `migration_creates_version_five`, `migration_is_idempotent`.

### R22 — Health-data-free state, order of checks, and module boundary

The coordinator's own tables hold no position bytes, tokens, Health Connect identifiers, record start times, or health values. A run result or retry result carries only named failures and the checkpointed summary.

Before each step of a turn, the checks run in this order:
1. A run failure.
2. The deadline.
3. Conditions, in R12's precedence, then collection-disabled.
4. The type's block.
5. The step itself.

A static check confirms four things about the module:
- it names no other unit's table;
- it calls each owning unit's begin and staging functions, and never calls the cipher itself;
- it imports only relative modules and the protocol library;
- it uses no Node built-in.

At run time, a guarded cipher confirms that only the staging units' `seal` is used.

Tests: `coordinator_state_is_health_data_free`, `checks_follow_the_documented_order`, `the_module_stays_inside_its_boundary`.

## Decisions for the Operator

1. **TypeScript seams over the Kotlin readers; the Expo bridge is an enabling task.** The coordinator calls `SyncSources`, whose results mirror the readers' Kotlin results, and validates them like the begin units validate issuer payloads (R18). The production adapter that exposes both readers and the token issuer through the Expo module carries no governed behavior. It becomes its own enabling Bead, exercised by the local device smoke run. The alternative is to bridge here, which needs a Kotlin-to-JavaScript test harness that does not exist.
2. **One unit for the whole scope row.** Every behavior in the row feeds the same per-type outcome. A partial coordinator would either retry a Projection Refusal every run, or block it forever. The pack is therefore large, at about 75 TypeScript tests. The alternative is several versions, each published against an incomplete row.
3. **Every run checks every type; v1 never reports `not-due`.** Foreground triggers are rare on a collector the Operator seldom opens, and hourly background runs need every type. The alternative is a minimum per-type interval for foreground triggers, reported as `not-due`.
4. **A check without changes is an empty Change Cycle (R6).** Only `stage-outbox-batch@1` persists the next token and unread-since, so each unchanged type stages one empty Ingest Batch and its Change Completion per run. That is two small messages per type each hour. Without them, an unchanged type's token would age past 30 days, and a quiet type would force a Reconciliation Snapshot. The alternative is a `stage-outbox-batch@2` primitive that records a no-change check without staging.
5. **Permission comes from the readers.** The readers and the issuer already check permissions before any read. A type found not permitted spends one short slot and keeps its ring place. `ingest-protocol@1` forbids a reason on `not-permitted`. The published readers' notes say "`not-permitted` with reason `permission-missing`"; that is reported as `not-permitted` with no reason, or as `progressed` with `permission-missing` after progress (R13). The alternative is a separate permission query seam, checked before a slot is claimed.
6. **Conditions are supplied, and every condition pauses the same way (R11, R12).** Detecting a disabled collection, a removed secure lock, a Delivery Block, or storage pressure belongs to other units and the shell. The coordinator reacts identically to all four:
   - no new source call starts;
   - an in-flight page is staged;
   - completions are withheld;
   - positions are kept;
   - the run checkpoints.

   Collection control is `deferred`, because the Operator chose it. The other three are `blocked`, because they need Operator action.
7. **The baseline is judged on the Sender.** ADR 0048's "before first publication" means, on the Sender, before the Initial attempt's first Change Completion is staged. The Portal cannot publish earlier. A Reconciliation for a baseline still being delivered is ordered after it, so it is safe. The alternative is to wait for Delivery Receipts, which arrive in Phase 3.
8. **Refusal identities hold digests.** `read-snapshot-page@1` Decision 1 requires the coordinator to digest positions, because they hold record start times and Health Connect identifiers. The identity uses the protocol library's `messageSha256`, which adds `ingest-protocol` to the scope-map row's dependencies. No published test reads that row.
9. **Oversized records block like refusals (R17).** Nothing on the Sender can change the outcome, so there is no Operator retry. A new capability generation or projection version retries automatically. The alternative is to re-read the page on every run.
10. **Run failures leave the run open (R19).** The protocol has no per-type reason for a storage or key failure. A failing run therefore ends without a checkpoint, and the next trigger recovers it as interrupted. If recovery cannot checkpoint either, for example after the key becomes unusable, no new run starts. That matches ADR 0014's frozen installation. Staging conflicts are retried twice first.
11. **Follow-ups live in memory; the retry request is durable.** The OS or the Operator triggers again after a process death, so only the Operator's retry request is stored, with its block. It lasts until a read evaluates it.
12. **The soft deadline stops source calls, not staging (R10).** A finished cycle is never left waiting a run for its completion. Collection-disabled differs: there, completions are withheld until collection is re-enabled.
13. **Reasons have a fixed precedence (R20).** For `progressed`, `changes-token-expired` comes first, because a Reconciliation is the most consequential event. Otherwise the reason is the type's stop cause.
14. **Recovered runs report only what is provable (R4).** Every type is `interrupted`. The end time is the recovery time, and there is no elapsed duration. `sourceDataStaged` comes from durable cycle progress, recorded before each cycle's first read in the run.

## Ground rules

- Every fixture is synthetic. Fake positions and tokens are opaque test strings and bytes, and fake projections are arbitrary bytes, because the coordinator never inspects them. No fixture contains real health data, Device identifiers, or credentials.
- Assertions that something writes nothing, or that a failure rolls back, compare the whole database before and after:
  - `PRAGMA user_version`;
  - every schema object;
  - every row of every table, including sealed bytes.
- The fake answers each source call after yielding a macrotask, so concurrent turns genuinely overlap. A test whose source hook reads storage runs with a pool of one, because the 3ug seam allows one storage operation at a time.
- Crashes are simulated by a storage wrapper whose every top-level operation hangs after a chosen commit, followed by reopening the database in a new coordinator.
- The runner sets a per-test timeout, so an implementation that hangs fails rather than stalls CI.
- Not mechanically provable here; the approver confirms these in implementation review:
  - the production shell supplies real conditions, clocks, and Background Scheduling Status;
  - the coordinator runs on Hermes, which the static check covers only in part.

## Freeze prerequisites

The Operator approved the draft. The executable pack adds:
- the TypeScript suite `coordinate-sender-sync-1.test.ts` and its synthetic fake `support/synthetic-sync-sources.ts`;
- the Python contract suite;
- the runner;
- the Interface stub `sync/coordinate-sender-sync.ts`, whose migration and functions throw;
- the pinned migration DDL (R21).

Against the stub, every TypeScript test fails, and the two Python tests pass. The binding is:
- spec files: the contract and this pack;
- test files: `coordinate-sender-sync-1.test.ts`, `synthetic-sync-sources.ts`, `node-database.ts`, `node-installation-cipher.ts`, `test_coordinate_sender_sync_1.py`, and the runner. GPT 6.1 Sol's read-only implementability review runs before Gate B. The implementation registers `syncMigration` fifth and updates the production-migration test in `storage.test.ts`.
