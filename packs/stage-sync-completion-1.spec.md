# Draft acceptance pack — `stage-sync-completion@1`

**Status:** Draft approved by the Operator on 2026-10-14, including the nine decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `stage-sync-completion` Governed Scope contract in `governance/scopes/stage-sync-completion.1.json`.

`stage-outbox-batch@1` stages a cycle's pages and marks the cycle finished when its last page is staged. This unit then makes the cycle's completion proof durable, following ADR 0046 and ADR 0012:
- a **Snapshot Completion**, with an explicit `initial` or `reconciliation` kind, or a **Change Completion**;
- carrying the cycle's final page, fragment, and record counts and its final fragment chain, but no source cursor or token;
- sealed, and appended to the Sender Outbox after every one of the cycle's fragments;
- exactly once per cycle.

The Portal blocks a completion whose counts, order, or chain do not match its archived fragments. So the values come only from the cycle's own staged progress.

The unit is TypeScript only. It appends through `stage-outbox-batch@1`'s Outbox primitives, and reads attempt currency through `begin-initial-snapshot@2`.

## Interface

**TypeScript**, module `sender/app/src/outbox/stage-sync-completion.ts`. It uses:
- `stage-outbox-batch@1`'s `StagingContext`, `cycleProgress`, `outboxAad`, `lastOutboxSequence`, and `appendOutboxMessages`;
- `begin-initial-snapshot@2`'s `currentSnapshotAttempt`;
- the protocol library's `encodeMessage`;
- the 3ug storage and cipher seams.

Declarations:
- `CompletionRequest`: `{ recordType, cycleId }`.
- `stageSyncCompletion(database, cipher, context, request)` returns one of:
  - `{ outcome: "staged", completion: CycleCompletion }`
  - `{ outcome: "refused" | "conflict" | "cipher-failed" | "storage-failed", failure: string }`
- `CycleCompletion`: `{ cycleId, recordType, kind: "snapshot-completion" | "change-completion", installationId, sequence }`.
- `cycleCompletion(database, cycleId)` returns the cycle's recorded completion, or `null`.
- `completionMigration`: Sender schema version 3.

Caller errors throw `TypeError`: malformed identities, record types, or context fields, by the same rules as `stage-outbox-batch@1`.

## Requirements

TypeScript `node:test` suites in `stage-sync-completion-1.test.ts` use:
- the 3ug `node:sqlite` and `NodeInstallationCipher` substitutes, with the production migrations;
- `begin-initial-snapshot@2`, `begin-reconciliation-snapshot@1`, and `stage-outbox-batch@1` to create attempts and stage pages.

`tests/acceptance/test_stage_sync_completion_1.py` checks the contract. The runner verifies a fresh JUnit report.

### R1 — Governed Scope contract

The contract names:
- the scope, version, kind, and purpose;
- inputs, with their modes;
- outputs;
- 10 named failures in the outcome classes `refusal`, `conflict`, `cipher-failure`, and `storage-failure`;
- both effects;
- authority;
- the three dependencies;
- the acceptance oracle.

The scope-map row names those dependencies.

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — The completion message

The staged message is exactly `ingest-protocol@1`'s `encodeMessage` of the following:
- **For a snapshot cycle:** a `snapshot-completion` with `snapshot_kind`. That is `initial` for an `initial-snapshot` cycle and `reconciliation` for a `reconciliation-snapshot` cycle.
- **For a Change Cycle:** a `change-completion`.
- **Fields from the context:** `sender_installation`, and the generation as `capability_generation`.
- **Fields from the cycle:**
  - its `record_type` and `cycle_id`;
  - `page_count` set to the cycle's pages staged;
  - its `fragment_count` and `record_count`;
  - `final_chain` set to the cycle's chain value.
- **`sequence`:** one above the installation's Outbox high-water mark.

The message decodes under the protocol library. It holds no position, changes token, or unread-since instant. Its counts and chain equal what the Portal's delivery evaluation accumulates from the cycle's fragments.

Tests: `a_snapshot_completion_matches_the_protocol`, `a_reconciliation_completion_carries_its_kind`, `a_change_completion_matches_the_protocol`, `the_completion_carries_no_cursor_or_token`.

### R3 — Sealed and appended after every fragment

The message is sealed once, with `outboxAad(installation, sequence)`. It is appended through `appendOutboxMessages` with kind `snapshot-completion` or `change-completion`.

The completion's sequence is greater than every sequence of its cycle's fragments, and the high-water mark rises to it. Opening the row with its own authenticated data yields the exact message bytes.

Tests: `the_completion_is_sealed_and_appended`, `the_completion_follows_every_fragment`.

### R4 — Only a finished, current cycle

Each condition below, when it fails, gives a refusal with nothing written and nothing sealed:
- **`cycle-not-current`:**
  - the cycle has no progress and is not the record type's current snapshot attempt;
  - the cycle belongs to another record type;
  - the cycle's snapshot attempt is no longer current, because it was abandoned or superseded.
- **`installation-mismatch`:** the cycle was staged under another Sender Installation than the context's.
- **`chain-incomplete`:**
  - the cycle is not finished;
  - the cycle is the current attempt's own snapshot cycle, but no page has been staged yet.

Tests: `an_unfinished_cycle_is_refused`, `a_cycle_of_a_superseded_attempt_is_refused`, `another_installation_is_refused`, `an_unknown_cycle_is_refused`.

### R5 — Exactly once

The completion and its Outbox row commit together, with this unit's completion record. A cycle that already has a completion is `completion-already-staged`, and nothing new is sealed or written. That holds even after `send-outbox-message` removes the delivered Outbox row. `cycleCompletion` reports the recorded completion, which survives reopening the database.

Tests: `a_cycle_is_completed_once`, `the_completion_record_survives_reopening`.

### R6 — Recheck inside the commit

The following state is read before sealing:
- the attempt's currency;
- the cycle's progress;
- the completion record;
- the high-water mark.

All of it is read again inside the commit transaction. Any change gives `conflict` with `staging-conflict`, and nothing is written. That includes a change that lands just before the transaction opens. `appendOutboxMessages`' own high-water check applies too.

Tests: `interleaved_staging_conflicts_without_writing`, `the_recheck_runs_inside_the_commit_transaction`.

### R7 — Nothing staged on failure

Cipher failures are classified as in `stage-outbox-batch@1` R9. A database error at any point is `storage-failed`. The database is compared before and after.

Tests: `cipher_failures_write_nothing`, `storage_errors_roll_back_everything`.

### R8 — The Sender schema migration

`completionMigration` is Sender schema version 3. It is registered after `stage-outbox-batch@1`'s version 2, and references no other unit's table. It creates exactly this table, compared token for token with whitespace ignored, and nothing else besides its automatic indexes:

```sql
CREATE TABLE cycle_completions (
  cycle_id TEXT PRIMARY KEY NOT NULL, record_type TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('snapshot-completion', 'change-completion')),
  installation_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK (sequence > 0),
  UNIQUE (installation_id, sequence)) STRICT;
```

It holds one completion per cycle and one per Outbox sequence.

Tests: `migration_creates_version_three`, `migration_is_idempotent`.

### R9 — Caller errors, health-data-free outcomes, and order of checks

**Caller errors.** Malformed inputs throw `TypeError` before any database or cipher call.

**Named failures only.** Every non-`staged` result carries only a named failure. Every named failure is reachable, and no other failure is produced.

**Order of checks.** The first applicable check wins:
1. caller errors;
2. the R4 refusals, then `completion-already-staged`, against the state read before sealing;
3. sealing;
4. the commit, with its recheck and storage failure.

**Key use.** The unit only seals with the key; it never creates, deletes, or opens one.

**Other units' tables.** The module reaches them only through those units' functions. A static check confirms three things:
- its source names none of their tables;
- it appends through `appendOutboxMessages`;
- it imports only relative modules and the protocol library, without Node built-ins.

Tests:
- `malformed_inputs_are_caller_errors`
- `non_staged_results_carry_only_named_failures`
- `every_named_failure_is_reachable_and_in_the_contract`
- `checks_follow_the_documented_order`
- `staging_only_seals`
- `the_module_uses_other_units_tables_only_through_their_functions`

## Decisions for the Operator

1. **TypeScript only.** The unit uses the published Outbox and progress functions. It owns no Kotlin.
2. **It appends through `stage-outbox-batch@1`'s primitives.** That is the Outbox ownership the Operator approved in that unit's Decision 2. This unit seals with the same authenticated data, and appends inside its own transaction with `appendOutboxMessages`.
3. **Seal before the commit, then recheck.** This matches `stage-outbox-batch@1` Decision 3: a concurrent change is `staging-conflict`, and the caller retries.
4. **Only while the cycle's snapshot attempt is current.** Evidence of an abandoned or superseded attempt is retained but never completed, so it can never look complete or publish (ADR 0047, 0048).
5. **Exactly once per cycle.** This unit owns a completion record, Sender schema version 3, committed with the Outbox row. A retry after an interruption is `completion-already-staged` and never stages a second completion. `cycleCompletion` lets the caller see which sequence carries it.
6. **Every value comes from the cycle's own staged progress.** The page, fragment, and record counts and the final chain come from that progress, never from the caller. So the Portal's completion checks hold by construction. The completion carries no cursor or token (ADR 0046).
7. **Ordering by sequence.** A finished cycle can stage no further fragments. So the next Outbox sequence orders the completion after every fragment of its cycle. Whether a catch-up Change Cycle waits for the Snapshot Completion to be acknowledged is `coordinate-sender-sync`'s ordering (ADR 0047).
8. **The generation stamps the message.** The completion carries the context's capability generation. A completion holds no projection, so no projection-schema check applies.
9. **A new dependency on `begin-initial-snapshot@2`.** Attempt currency comes from it, so the scope-map row is updated.

## Ground rules

These are `stage-outbox-batch@1`'s ground rules. The trusted boundary also includes the published `begin-initial-snapshot@2`, `begin-reconciliation-snapshot@1`, and `stage-outbox-batch@1` modules.

Not mechanically provable here; the approver confirms it in implementation review: the implementation registers `completionMigration` third in `senderMigrations`. It updates the 3ug storage test's production-migration expectation to version 3. These are its only changes outside this module.

## Freeze prerequisites

The Operator approved the draft. The executable pack adds:
- the TypeScript suite, 24 tests;
- the Python contract suite, 2 tests;
- the self-verifying runner;
- the Interface stub `sender/app/src/outbox/stage-sync-completion.ts`.

The suite's fixture migrates explicitly with `[snapshotAttemptsMigration, outboxMigration, completionMigration]` to version 3. It drives the published `begin-initial-snapshot@2`, `begin-reconciliation-snapshot@1`, and `stage-outbox-batch@1`, and guards the cipher against any key creation, deletion, or opening.

A throwaway reference implementation passes all 24 tests and typechecks. Six mutants of it each fail at least one test:
- `page_count` taken from the fragment count;
- `snapshot_kind` fixed as `initial`;
- no attempt-currency check;
- no exactly-once check;
- the recheck run outside the transaction;
- the completion record written outside the Outbox transaction.

Against the stub:
- every TypeScript test fails, because the stub's migration throws and the static check finds no `appendOutboxMessages` call;
- the two Python tests pass, because they check the contract.

The binding is:
- spec files: the contract and this pack;
- test files:
  - `stage-sync-completion-1.test.ts`;
  - its 3ug substitutes `sender/app/test/support/node-database.ts` and `sender/app/test/support/node-installation-cipher.ts`;
  - `test_stage_sync_completion_1.py` and the runner.
