# Draft acceptance pack — `stage-outbox-batch@2`

**Status:** Draft approved by the Operator on 2026-10-30, including the seven decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `stage-outbox-batch` Governed Scope contract, version 2, in `governance/scopes/stage-outbox-batch.2.json`. It is the additive successor to the published `stage-outbox-batch@1`, which it supersedes.

## Why a successor

An emulator smoke run on 2026-10-30 (Beads `hfj`) found two faults that need new behaviour from this unit.

1. **Idle Sync runs never catch up (Beads `gc5`).** With nothing new in Health Connect, every Polling Run still stages two messages per record type, then its checkpoint. With all 41 projected types that is 83 messages:
   - an Ingest Batch with no items, because `@1` makes a change read durable only by staging it (R2, R4);
   - a Change Completion for that one-page cycle.

   The Sender shell delivers at most 64 messages after each run, so the Outbox grew by 19 messages a run without bound. The Operator decided on 2026-10-30 that a change read finding nothing should, once the type's catch-up cycle has finished, advance the change stream without staging a message. The Sync Checkpoint's per-type `no-change` outcome still tells the Portal the type was polled. Only this unit may advance a cursor, so the advance belongs here. Deciding when to use it belongs to `coordinate-sender-sync@3`.
2. **Status reads load every sealed message (Beads `mfv`, `636`).** `manage-sender-enrollment@1` and `stage-sync-checkpoint@1` answer their questions by calling `outboxMessages`, which returns every sealed BLOB. With about 1,000 queued messages, the Sender ran out of JNI global references and crashed, twice on **Begin draining**. Both units need only counts, byte totals, and whether one sequence is still queued. This unit owns the Outbox table, so it provides those reads (the second option `mfv` names). The two consumers adopt them in their own successors.

## Interface

Everything in `stage-outbox-batch@1`'s Interface is unchanged. Its functions, types, `outboxMigration`, and their behaviour carry over exactly, except that `changeOrigin` and the first-page origin check of `stageOutboxBatch` also honour a quiet advance (R16). Without any quiet advance, both behave exactly as in `@1`.

The module `sender/app/src/outbox/stage-outbox-batch.ts` adds:
- `outboxBacklog(database, installationId)`, which returns `{ messageCount, byteCount }` (R14);
- `outboxHolds(database, installationId, sequence)`, which returns a boolean (R15);
- `advanceChangeOrigin(database, context, page)`, where `page` is a `ChangePage`. It returns an `AdvanceResult`, which is one of:
  - `{ outcome: "advanced", origin: ChangeOrigin }`;
  - `{ outcome: "refused" | "storage-failed", failure: string }`;
- `changeOriginMigration`, which is Sender schema version 10 (R18).

`advanceChangeOrigin` needs no cipher: it seals nothing.

## Requirements

`stage-outbox-batch@1`'s requirements R1–R12 carry over unchanged. The `@2` pack re-runs `@1`'s bound runner unchanged, so every `@1` test must still pass exactly.

The new requirements are bound to a new TypeScript suite, `sender/app/test/stage-outbox-batch-2.test.ts`. It uses `@1`'s 3ug substitutes, `begin-initial-snapshot@2` to create attempts, and `@1`'s staging to build cycles. A new Python suite, `tests/acceptance/test_stage_outbox_batch_2.py`, checks the contract.

### R13 — Governed Scope contract, version 2

The `@2` contract keeps every `@1` input with its mode, and every `@1` output, named failure with its outcome class, and effect. It adds:
- the outputs `change_origin_advanced` and `outbox_backlog`;
- the named failures `change-page-not-quiet` and `catch-up-unstaged`, both refusals;
- the advance and backlog authority.

Its dependencies are `@1`'s, with `begin-initial-snapshot` at version 2. The scope-map row names quiet advances and backlog reads, and still satisfies every published test that pins it.

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R14 — The backlog without ciphertext

`outboxBacklog(database, installationId)` returns the number of the installation's queued Outbox Messages and the sum of their `messageBytes`. It returns `{ messageCount: 0, byteCount: 0 }` when the installation has none, and never counts another installation's messages.

Its answer equals what `@1`'s `outboxMessages` gives for the same installation, which the tests use as the oracle. That holds:
- across several installations;
- after `send-outbox-message`-style removals from the head and from the middle;
- for every Outbox kind.

It loads no sealed bytes. Every row the database returns to it is checked, and none may hold a `sealed` value or any byte array. It writes nothing: the whole database, including `PRAGMA user_version` and every row, is the same afterwards.

Tests: `backlog_matches_the_outbox_for_one_installation`, `backlog_is_zero_without_messages`, `backlog_follows_removals`, `backlog_reads_no_sealed_bytes`, `backlog_writes_nothing`.

### R15 — Whether a sequence is queued, without ciphertext

`outboxHolds(database, installationId, sequence)` is `true` exactly when that installation's message with that sequence is still in the Outbox. It is `false`:
- before the sequence is staged;
- after its row is removed;
- for the same sequence under another installation.

Like R14, it loads no sealed bytes and writes nothing.

Tests: `holds_reports_a_queued_sequence`, `holds_is_false_when_absent_removed_or_foreign`, `holds_reads_no_sealed_bytes_and_writes_nothing`.

### R16 — A quiet change read advances the change origin

A **quiet** change read is the first page (`pageIndex` 1) of a Change Cycle identity that has no staged progress, with no items and `hasMore` false.

`advanceChangeOrigin` accepts a quiet read when all of these hold:
- its record type has a current snapshot attempt, begun under the context's installation;
- the attempt's snapshot chain is finished;
- no Change Cycle under the attempt is unfinished;
- a Change Cycle under the attempt has finished, so the catch-up cycle was staged;
- `readFrom` equals the change origin.

It then commits, in one SQLite transaction, the page's `nextChangesToken` and `nextUnreadSinceMs` as the record type's change origin under that attempt, and returns `advanced` with the new origin. Its checks read the state inside that same transaction, so no staging or other advance can land between the checks and the write. It writes:
- no Outbox Message;
- no change to any high-water mark;
- no cycle progress, for the page's identity or any other.

After an advance:
- **`changeOrigin`** reports the advanced token and instant, until a Change Cycle under the attempt finishes after the advance. From then on it reports that cycle's final token, as in `@1`.
- **The next cycle.** A new Change Cycle's first staged page must read from the advanced origin, otherwise `page-position-mismatch` as in `@1`'s R6. That includes a cycle that reuses the quiet read's unstaged identity.
- **Chained advances.** A further quiet read advances from the latest origin.
- **The recheck.** `@1`'s R10 recheck for a new Change Cycle also covers the origin. An advance committed while a first change page is being sealed makes that staging `staging-conflict`, with nothing written.
- **A new attempt.** Once the attempt stops being current, its advance no longer counts. A new attempt's first Change Cycle starts from that attempt's pre-read token.
- **Persistence.** The advance survives reopening the database.
- **Below version 10.** A database that has not reached Sender schema version 10 has no advances table. There, `changeOrigin` and `stageOutboxBatch` behave exactly as in `@1`, because `@1`'s suite, and every published suite that stages through this module, migrate below version 10. `advanceChangeOrigin` returns `storage-failed` and writes nothing.

Tests:
- `a_quiet_read_advances_the_origin`
- `an_advance_stages_nothing`
- `the_next_change_cycle_reads_from_the_advanced_origin`
- `an_unstaged_identity_may_be_reused_after_an_advance`
- `advances_chain_from_the_latest_origin`
- `a_finished_later_cycle_supersedes_the_advance`
- `an_advance_during_sealing_makes_staging_conflict`
- `a_new_attempt_ignores_the_old_advance`
- `an_advance_survives_reopening`
- `a_database_below_version_ten_behaves_as_at_one`

### R17 — Refusals and failures of an advance

An advance that cannot apply returns `refused` with a named failure, and writes nothing:
- `change-page-not-quiet`: the page has items, has more pages, or is not the first page.
- `projection-schema-unsupported`: as in `@1`'s R7.
- `cycle-not-current`: the record type has no current attempt, or the page's identity names a snapshot attempt or a cycle with staged progress under another attempt.
- `installation-mismatch`: the attempt was begun under another installation.
- `cycle-finished`: the identity is a finished Change Cycle's.
- `snapshot-unfinished`: the attempt's snapshot chain is not finished.
- `change-cycle-open`: a Change Cycle under the attempt is unfinished, including the page's own identity if it has staged progress.
- `catch-up-unstaged`: no Change Cycle under the attempt has finished. The catch-up cycle after a snapshot is always staged, even when it is empty (ADR 0047), so the caller stages this read with `stageOutboxBatch`.
- `page-position-mismatch`: `readFrom` is not the change origin.

A database error at any point, including the commit, is `storage-failed`, and rolls back. Every refusal and failure leaves the whole database unchanged.

Tests:
- `the_catch_up_cycle_is_never_skipped`
- `a_page_that_is_not_quiet_is_refused`
- `advance_refusals_write_nothing`, one case for each refusal
- `advance_storage_errors_roll_back`

### R18 — The Sender schema migration, version 10

`changeOriginMigration` is Sender schema version 10, registered after version 9. It creates exactly this table, compared token for token with whitespace ignored, and nothing else besides its automatic index:

```sql
CREATE TABLE change_origin_advances (
  snapshot_id TEXT PRIMARY KEY NOT NULL, record_type TEXT NOT NULL,
  after_lineage INTEGER NOT NULL CHECK (after_lineage > 0),
  changes_token TEXT NOT NULL, unread_since_ms INTEGER NOT NULL,
  FOREIGN KEY (snapshot_id, record_type) REFERENCES snapshot_attempts (snapshot_id, record_type)) STRICT;
```

Each snapshot attempt has at most one advance. `after_lineage` is the lineage of the latest finished Change Cycle when the advance was made. The advance holds while that is still the latest finished cycle under the attempt, which is how R16's supersession is decided.

Nothing else reads or writes the table except this module's functions. A database already at version 10 is unchanged by the migration. A destructive reset by `manage-sender-enrollment` deletes its rows, because that reset discovers every Sender table.

Tests: `migration_creates_version_ten`, `migration_ten_is_idempotent`.

### R19 — Caller errors, health-data-free outcomes, and order of checks

**Caller errors.** These throw `TypeError` before any database call:
- **`outboxBacklog`, `outboxHolds`:** an installation that is not a lowercase UUID; for `outboxHolds`, a sequence that is not a positive safe integer.
- **`advanceChangeOrigin`:** the page and context caller errors of `@1`'s R12 that apply to a change page and a context, and a page whose `source` is not `change`.

**Health-data-free outcomes.** Every non-`advanced` result carries only a named failure. Every new named failure is reachable, and no other failure is produced.

**Order of checks.** For `advanceChangeOrigin`, the first applicable check in this order wins:
1. caller errors;
2. `change-page-not-quiet`;
3. the generation's projection schemas;
4. reading the state, where a failing read is `storage-failed`, then in this order: `cycle-not-current`, `installation-mismatch`, `cycle-finished`, `snapshot-unfinished`, `change-cycle-open`, `catch-up-unstaged`, `page-position-mismatch`;
5. the commit, with its storage failure.

Tests: `new_caller_errors_touch_no_storage`, `advance_results_carry_only_named_failures`, `new_named_failures_are_reachable_and_in_the_contract`, `advance_checks_follow_the_documented_order`.

## Decisions for the Operator

1. **An origin advance, not an empty cycle.** The quiet read creates no Change Cycle. A finished cycle without fragments was rejected for three reasons:
   - `coordinate-sender-sync@2` treats any finished cycle without a completion as owing one;
   - `stage-sync-completion@1` would stage a completion with no fragments, which the Portal would block;
   - `@1`'s table forbids a cycle with no pages.

   The advance is a separate row per snapshot attempt, so its storage stays bounded, whereas empty cycles add a row per type and run.
2. **The catch-up cycle is always staged (ADR 0047).** After every Initial or Reconciliation Snapshot, first publication waits for the catch-up Change Completion, so the first Change Cycle under each attempt is staged even when empty. Only later cycles may be quiet. *Alternative:* allowing a quiet catch-up, which would leave the Portal waiting for a completion that never comes.
3. **The backlog reads live in the unit that owns the Outbox.** This is the second option Beads `mfv` named. The consumers keep reading the Outbox only through this unit. *Alternative:* consumer successors that query the table directly, which would break `@1`'s Decision 2.
4. **`outboxMessages` stays.** It is unchanged, and remains the tests' oracle and a diagnostic read. Its two production callers move to the backlog reads in their own successors.
5. **The Sender's tenth schema migration** holds the advances. Every earlier table keeps its layout.
6. **Supersession and dependencies.** Vouch supersedes `@1` when `@2` is specified, after this draft is approved. Published dependents keep their packs, because `@2`'s interface is a superset of `@1`'s and nothing changes without a quiet advance. If Vouch requires it when `@2` publishes, their packs are rebound with `rebind-pack`, with no change in behaviour. The `begin-initial-snapshot` dependency moves to the published version 2, which `coordinate-sender-sync@2` already uses.
7. **Consumers follow in their own units.** These are `manage-sender-enrollment@2` and `stage-sync-checkpoint@2` for the backlog reads, and `coordinate-sender-sync@3` for quiet reads. The shell enables quiet reads and delivers until the Outbox drains (Beads `2rv.18`).

## Ground rules

`@1`'s ground rules apply:
- the 3ug substitutes;
- database failure injection;
- the untouchable database;
- the runner that trusts only a fresh JUnit report.

In addition:
- **Ciphertext checks** use an observing `SenderDatabase` wrapper that records every row any query returns. A backlog read passes only if no recorded row has a `sealed` key or any byte-array value.
- **Writes-nothing assertions** compare the whole database: `PRAGMA user_version`, every `sqlite_master` object, and every row of every table, BLOBs included.
- **Migrations.** Fixtures migrate with an explicit list of the migrations this unit needs, never the production `senderMigrations`.

## Freeze prerequisites

The Operator approved the draft. The executable pack adds:
- the TypeScript suite `stage-outbox-batch-2.test.ts`, 28 tests;
- the Python contract suite `tests/acceptance/test_stage_outbox_batch_2.py`, 2 tests;
- the runner `tests/acceptance/run_stage_outbox_batch_2.sh`. It runs the version 2 contract check, then `@1`'s bound runner unchanged, then the `@2` suite under a 60-second per-test timeout. It passes the suite only when a fresh JUnit report lists exactly its tests, none failed, errored, or skipped;
- the Interface stub, appended to `sender/app/src/outbox/stage-outbox-batch.ts`: `OutboxBacklog`, `AdvanceResult`, the three functions, which throw, and `changeOriginMigration`, whose migration throws.

The suite migrates with the explicit list of all ten Sender migrations to version 10, because migrations must be contiguous from version 1. R16's below-version-10 test migrates to version 2 only.

**The reference run.** A throwaway reference implementation was written before freeze. Its first run found a gap, now closed in R16. `@1`'s suite, and every published suite that stages through this module, migrate below version 10, so `changeOrigin` and `stageOutboxBatch` must behave as `@1` on a database without the advances table. After that, the reference passes:
- all 28 new tests and `@1`'s 44 tests;
- both Python suites;
- the whole Sender suite;
- the typecheck.

Twelve mutants of the reference each fail at least one test:
- skipping the catch-up check;
- `changeOrigin` ignoring advances;
- staging's origin check ignoring advances;
- the recheck ignoring advances;
- advances never superseded by a later finished cycle;
- the backlog loading sealed rows;
- `outboxHolds` ignoring the installation;
- the advance's checks running outside its transaction;
- the catch-up checked before the snapshot;
- the quiet check ignoring `hasMore`;
- the advance ignoring the installation;
- the migration without its lineage check.

**RED against the stub:**
- all 28 new TypeScript tests fail:
  - 26 because the stub migration fails and the fixture expects version 10;
  - `new_caller_errors_touch_no_storage`, because the stub throws a plain `Error` rather than `TypeError`;
  - `a_database_below_version_ten_behaves_as_at_one`, because the stub advance throws;
- `@1`'s 44 TypeScript tests pass;
- both Python suites pass, because they check the contracts.

The binding is:
- **spec files:** this contract and pack, and `@1`'s contract and pack;
- **test files:**
  - `stage-outbox-batch-2.test.ts`, `test_stage_outbox_batch_2.py`, and the `@2` runner;
  - every file `stage-outbox-batch-pack/2` binds: `stage-outbox-batch-1.test.ts`, its 3ug substitutes `sender/app/test/support/node-database.ts` and `sender/app/test/support/node-installation-cipher.ts`, `test_stage_outbox_batch_1.py`, and `run_stage_outbox_batch_1.sh`.

GPT 6.1 Sol's read-only implementability review runs before Gate B. The approver confirms in implementation review that:
- `changeOriginMigration` is registered tenth in `senderMigrations`, the only permitted change to `sender/app/src/storage/migrations.ts`;
- the 3ug storage test's production-migration expectation is updated to version 10 and the new table.

The implementation adds the runner to the project CI `test` job.
