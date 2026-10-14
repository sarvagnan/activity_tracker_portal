# Draft acceptance pack — `stage-sync-checkpoint@1`

**Status:** Draft approved by the Operator on 2026-10-14, including the eight decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `stage-sync-checkpoint` Governed Scope contract in `governance/scopes/stage-sync-checkpoint.1.json`.

Every completed Polling Run is reported, including one recovered as interrupted (ADR 0049). The report is one encrypted, byte-stable Sync Checkpoint. It is health-data-free: `ingest-protocol@1` R8 gives it a closed schema, with no field for source-record counts, tokens, Source Identities, health dates, or health values.

`coordinate-sender-sync` owns the run itself: its run sequence, trigger, times, and each type's outcome. This unit turns a finished run's summary into its one checkpoint. It works in three steps:
1. It samples the installation's queued Outbox message and byte counts before the checkpoint is inserted.
2. It seals the checkpoint.
3. It appends it through `stage-outbox-batch@1`'s Outbox, exactly once per run.

The unit is TypeScript only.

## Interface

**TypeScript**, module `sender/app/src/outbox/stage-sync-checkpoint.ts`. It uses:
- `stage-outbox-batch@1`'s `StagingContext`, `outboxAad`, `lastOutboxSequence`, `outboxMessages`, and `appendOutboxMessages`;
- the protocol library's `encodeMessage`;
- the 3ug storage and cipher seams.

The declarations are:
- `RunSummary`: `{ runSequence, trigger, startedAtMs, endedAtMs, elapsedMs?, backgroundScheduling, recordTypes }`, where:
  - `trigger` is `background`, `foreground`, or `manual`;
  - `backgroundScheduling` is `available`, `restricted`, or `unregistered`;
  - each entry of `recordTypes` is `{ recordType, outcome, reason?, sourceDataStaged }`.
- `stageSyncCheckpoint(database, cipher, context, summary)` returns one of:
  - `{ outcome: "staged", checkpoint: RunCheckpoint }`;
  - `{ outcome: "refused" | "conflict" | "cipher-failed" | "storage-failed", failure }`.
- `RunCheckpoint`: `{ installationId, runSequence, sequence }`.
- `runCheckpoint(database, installationId, runSequence)` returns the recorded checkpoint, or `null`.
- `checkpointMigration`: Sender schema version 4.

Caller errors throw `TypeError`. They cover:
- a malformed context, by `stage-outbox-batch@1`'s rules;
- a run sequence that is not a positive safe integer;
- an unknown trigger, scheduling status, outcome, or reason;
- a reason where the protocol forbids one, or a missing reason where the protocol requires one;
- an instant or elapsed duration outside the protocol's ranges;
- a duplicated record type.

## Requirements

TypeScript `node:test` suites in `stage-sync-checkpoint-1.test.ts` use the 3ug `node:sqlite` and `NodeInstallationCipher` substitutes. They migrate an explicit list to version 4, and use `stage-outbox-batch@1`'s primitives to fill the Outbox. `tests/acceptance/test_stage_sync_checkpoint_1.py` checks the contract. The runner verifies a fresh JUnit report.

### R1 — Governed Scope contract

The contract names:
- the scope, version, kind, and purpose;
- inputs, with their modes;
- outputs;
- 7 named failures in the outcome classes `refusal`, `conflict`, `cipher-failure`, and `storage-failure`;
- both effects;
- authority;
- the two dependencies;
- the acceptance oracle.

It does not contradict the scope-map row.

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — The checkpoint message

The staged message is exactly `ingest-protocol@1`'s `encodeMessage` of a `sync-checkpoint`. Its fields come from three places:
- **The context:** `sender_installation`, and `capability_generation` from the generation.
- **The summary:**
  - `run_sequence` and `trigger`;
  - `started_at` and `ended_at`, as the protocol's UTC millisecond timestamps of the summary's instants;
  - `elapsed_ms`, only when the summary supplies it;
  - `background_scheduling`;
  - `record_types`, sorted ascending by record type. Each entry carries `record_type`, `outcome`, `source_data_staged`, and `reason` only when the summary supplies one.
- **The Outbox:**
  - `sequence`, one above the installation's high-water mark;
  - `outbox_messages_before_checkpoint` and `outbox_bytes_before_checkpoint`, the installation's queued Outbox rows and the sum of their plaintext byte lengths, sampled before the checkpoint's own row exists.

The message decodes under the protocol library, and a run with no permitted record types has an empty list.

Instants must lie within the protocol's four-digit-year range, `0000-01-01T00:00:00.000Z` through `9999-12-31T23:59:59.999Z`. Instants before 1970 and an end before the start are formatted unchanged.

Tests:
- `a_checkpoint_matches_the_protocol`
- `record_types_are_sorted`
- `optional_fields_appear_only_when_supplied`
- `every_outcome_and_reason_is_staged`
- `an_empty_run_is_checkpointed`
- `instants_become_protocol_timestamps`

### R3 — Counts sampled before insertion

The counts cover only rows that are still queued, across every Outbox kind of the context's installation. Other installations' rows are excluded, and the checkpoint's own row is excluded. Rows removed after their receipts no longer count. The next checkpoint's counts include this one.

Tests: `counts_cover_queued_rows_of_the_installation`, `removed_rows_no_longer_count`, `the_next_checkpoint_counts_this_one`.

### R4 — Sealed and appended

The checkpoint is sealed once with `outboxAad(installation, sequence)` and appended through `appendOutboxMessages` with kind `sync-checkpoint`. Opening the row with its own authenticated data yields the exact message bytes. Only `seal` is called on the cipher.

Tests: `the_checkpoint_is_sealed_and_appended`, `staging_only_seals`.

### R5 — Exactly once per run

The checkpoint record commits with the Outbox row. A second checkpoint for the same installation and run sequence is `checkpoint-already-staged`, and nothing is sealed or written. That holds even after the delivered row is removed. Another installation's run with the same number is independent.

Tests: `a_run_is_checkpointed_once`, `the_checkpoint_record_survives_reopening`.

### R6 — Recheck inside the commit

The queued counts, the high-water mark, and the checkpoint record are read before sealing and again inside the commit transaction. Any change is `conflict` with `staging-conflict`, and nothing is written. Afterwards the database holds exactly the competing change. That includes a change landing just before the transaction opens.

Tests: `interleaved_staging_conflicts_without_writing`, `the_recheck_runs_inside_the_commit_transaction`.

### R7 — Nothing staged on failure

Cipher failures are classified as in `stage-outbox-batch@1` R9. A database error at any point is `storage-failed`. The whole database is compared before and after, with every schema object and every row of every table, including sealed bytes.

Tests: `cipher_failures_write_nothing`, `storage_errors_roll_back_everything`.

### R8 — The Sender schema migration

`checkpointMigration` is Sender schema version 4, registered after `stage-sync-completion@1`'s version 3. It references no other unit's table, and leaves every earlier schema object unchanged. It creates exactly this table, which is compared token by token with whitespace ignored, and nothing else besides its automatic indexes:

```sql
CREATE TABLE run_checkpoints (
  installation_id TEXT NOT NULL, run_sequence INTEGER NOT NULL CHECK (run_sequence > 0),
  sequence INTEGER NOT NULL CHECK (sequence > 0),
  PRIMARY KEY (installation_id, run_sequence), UNIQUE (installation_id, sequence)) STRICT;
```

Tests: `migration_creates_version_four`, `migration_is_idempotent`.

### R9 — Caller errors, health-data-free outcomes, order of checks, and module boundary

- Malformed inputs throw `TypeError` before any database or cipher call.
- Every non-`staged` result carries only a named failure. Every named failure is reachable.
- Checks run in this order: caller errors, `checkpoint-already-staged`, sealing, then the commit.
- A static check confirms three things about the module:
  - it names no other unit's table;
  - it appends through `appendOutboxMessages`;
  - it imports only relative modules and the protocol library, with no Node built-ins.

Tests:
- `malformed_summaries_are_caller_errors`
- `non_staged_results_carry_only_named_failures`
- `every_named_failure_is_reachable_and_in_the_contract`
- `checks_follow_the_documented_order`
- `the_module_uses_other_units_tables_only_through_their_functions`

## Decisions for the Operator

1. **The run summary comes in and the message goes out.** The Polling Run's lifecycle belongs to `coordinate-sender-sync`: the durable run marker before reads, recovery of an open run as interrupted, and each type's outcome and reason. This unit validates and formats; it never decides an outcome.
2. **Appended through `stage-outbox-batch@1`'s primitives,** like `stage-sync-completion@1`. Its scope-map row already depends on `stage-outbox-batch` (DEV-sob-01).
3. **Exactly once per run.** This unit owns a checkpoint record, Sender schema version 4, keyed by installation and run sequence and committed with the Outbox row. A retry after an interruption never stages a second checkpoint for the same run.
4. **What the pre-checkpoint counts mean.** They are the queued rows of the installation across every Outbox kind, and the sum of their plaintext byte lengths, which is what the Portal will receive. Sealed sizes are not used. They are sampled inside the commit, so the checkpoint never counts itself.
5. **This unit puts record types into the protocol's order.** It sorts them ascending, and rejects a duplicate. It validates the protocol's outcome and reason pairing as a caller error, so a malformed summary can never produce a message the Portal blocks.
6. **Sender times are formatted, not judged.** Instants become the protocol's UTC millisecond timestamps. An end before a start is passed through, because clock skew is the Portal's health-data-free diagnostic under ADR 0049, not a Sender refusal.
7. **Seal before the commit, then recheck,** as in `stage-outbox-batch@1` Decision 3.
8. **The migration and the scope-map row.** The migration is registered fourth, and the suite migrates an explicit list. The scope-map row's dependencies already match the contract.

## Ground rules

These are `stage-outbox-batch@1`'s ground rules. The trusted boundary includes the published `stage-outbox-batch@1` module.

Not mechanically provable here; the approver confirms these in implementation review:
- the implementation registers `checkpointMigration` fourth in `senderMigrations`;
- it updates the 3ug storage test's production-migration expectation to version 4;
- those are its only changes outside this module.

## Freeze prerequisites

The Operator approved the draft. The executable pack adds:
- the TypeScript suite, 24 tests;
- the Python contract suite, 2 tests;
- the self-verifying runner;
- the Interface stub `sender/app/src/outbox/stage-sync-checkpoint.ts`.

The suite migrates with the explicit list `[snapshotAttemptsMigration, outboxMigration, completionMigration, checkpointMigration]` to version 4. It fills the Outbox through `appendOutboxMessages`, and it guards the cipher against any key creation, deletion, or opening.

The implementer's read-only implementability review found nothing blocking. Its three clarifications are added:
- the conflict tests now compare the whole database with its state after the competing change;
- `every_outcome_and_reason_is_staged` stages every outcome and every reason;
- the caller errors add NaN, infinite, and unsafe-integer values, and the cipher guard also flags `keyStatus` and `isDeviceSecure`.

The approver's first Gate B review required two corrections, and both are made:
- the before-and-after comparisons now cover the whole database, with every schema object and every row of every table, including sealed bytes;
- the migration test compares the whole schema before and after version 4.

A mutant that creates an extra table and writes to it before returning `staging-conflict` now fails, and so does one whose migration adds an extra table.

A throwaway reference implementation passes all 24 tests and typechecks. Eleven mutants of it each fail at least one test:
- every installation's rows are counted;
- sealed sizes are counted;
- record types are left unsorted;
- reasons are dropped;
- there is no exactly-once check;
- the checkpoint counts itself;
- the recheck runs outside the transaction;
- the recheck ignores byte counts;
- reason-required outcomes are rejected outright;
- an extra table is written on conflict;
- the migration adds an extra table.

Against the stub:
- every TypeScript test fails, because the stub's migration throws and the static check finds no `appendOutboxMessages` call;
- the two Python tests pass, because they check the contract.

The binding is:
- spec files:
  - the contract;
  - this pack;
- test files:
  - `stage-sync-checkpoint-1.test.ts`;
  - its 3ug substitutes, `sender/app/test/support/node-database.ts` and `sender/app/test/support/node-installation-cipher.ts`;
  - `test_stage_sync_checkpoint_1.py` and the runner.
