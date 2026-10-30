# Draft acceptance pack — `stage-sync-checkpoint@2`

**Status:** Draft approved by the Operator on 2026-10-30, including the three decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `stage-sync-checkpoint` Governed Scope contract, version 2, in `governance/scopes/stage-sync-checkpoint.2.json`. It is the additive successor to the published `stage-sync-checkpoint@1`, which it supersedes.

## Why a successor

`@1` samples the installation's queued Outbox message and byte counts before the checkpoint is inserted (R3), and again inside the commit (R6). It reads them with `stage-outbox-batch@1`'s `outboxMessages`, which returns every sealed message of every installation (Beads `mfv`). Every Polling Run ends with a checkpoint, so every run loaded the whole Outbox's ciphertext twice.

In the emulator smoke run on 2026-10-30 (Beads `hfj`, `636`), this was one of the whole-Outbox reads that, at about 1,000 queued messages, exhausted the Sender's JNI global references and crashed it. `stage-outbox-batch@2` adds a backlog read that returns the same counts without loading sealed bytes. This successor uses it, and pins that it does.

## Interface

Everything in `@1`'s Interface is unchanged: `stageSyncCheckpoint`, its types and results, `runCheckpoint`, and `checkpointMigration`. There is no new migration and no new export.

## Requirements

`@1`'s requirements R1–R9 carry over unchanged. The `@2` pack re-runs `@1`'s bound runner unchanged, so every `@1` test must still pass exactly.

The new requirements are bound to a new TypeScript suite, `sender/app/test/stage-sync-checkpoint-2.test.ts`. It uses `@1`'s 3ug substitutes and appends Outbox rows through `stage-outbox-batch@2`'s primitives. A new Python suite, `tests/acceptance/test_stage_sync_checkpoint_2.py`, checks the contract.

### R10 — Governed Scope contract, version 2

The `@2` contract keeps `@1`'s inputs with their modes, and its outputs, named failures, outcome classes, and effects. Its dependencies are `ingest-protocol@1` and `stage-outbox-batch@2`. It adds the authority to sample counts through the backlog read. The scope-map row says the counts are read without loading sealed messages, and still satisfies every published test that pins it.

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R11 — No sealed message is loaded

Staging a checkpoint loads no sealed Outbox message, before sealing or inside the commit. That holds:
- with the installation's own queued messages of every kind;
- alongside another installation's rows;
- on every outcome: `staged`, `checkpoint-already-staged`, `staging-conflict`, a cipher failure, and `storage-failed`.

An observing database wrapper records every row any query returns. No recorded row may have a `sealed` key. Also, the number of byte-array values the staging receives must be the same with an empty Outbox as with 500 queued messages.

Tests: `staging_loads_no_sealed_message`, `failed_stagings_load_no_sealed_message`, `reads_do_not_grow_with_the_outbox`.

### R12 — The same checkpoint

For every Outbox state the tests build, the staged checkpoint opens to exactly the bytes `@1` would stage, with `outbox_messages_before_checkpoint` and `outbox_bytes_before_checkpoint` equal to the oracle computed from `outboxMessages`. The tests build those states with:
- several installations;
- every Outbox kind;
- removals at the head and in the middle;
- a previous checkpoint.

R6's recheck still detects every competing change it detects in `@1`, now through the backlog read: a change to the counts, the byte total, the high-water mark, or the checkpoint record.

Tests: `checkpoint_counts_match_the_outbox_oracle`, `the_recheck_still_detects_every_change`.

## Decisions for the Operator

1. **Same checkpoint, cheaper reads.** Nothing the Portal receives changes. `@2` exists to pin that no sealed message is loaded (R11), and to depend on the read that makes that possible. *Alternative:* changing `@1`'s implementation without a new version, which leaves the guarantee unpinned.
2. **The guarantee is tested by what the database returns, not by timing,** as in `manage-sender-enrollment@2`.
3. **Supersession.** Vouch supersedes `@1` when `@2` is specified. Its published dependents keep their packs, because `@2`'s interface is unchanged: `coordinate-sender-sync@2` and `notify-operator-conditions@1`. If Vouch requires it at publication, their packs are rebound with `rebind-pack`, with no change in behaviour. Implementation waits until `stage-outbox-batch@2` publishes.

## Ground rules

`@1`'s ground rules apply. In addition:
- **Ciphertext checks** use an observing `SenderDatabase` wrapper that records every row any query returns, as `stage-outbox-batch@2`'s pack does.
- **Writes-nothing assertions** compare the whole database.
- **Migrations.** Fixtures migrate with an explicit list of migrations, never the production `senderMigrations`.

## Freeze prerequisites

The Operator approved the draft. The executable pack adds:
- the TypeScript suite `stage-sync-checkpoint-2.test.ts`, 5 tests;
- the Python contract suite `tests/acceptance/test_stage_sync_checkpoint_2.py`, 2 tests;
- the runner `tests/acceptance/run_stage_sync_checkpoint_2.sh`. It runs the version 2 contract check, then `@1`'s bound runner unchanged, then the `@2` suite under a 60-second per-test timeout. It passes the suite only when a fresh JUnit report lists exactly its tests, none failed, errored, or skipped.

The suite migrates with `@1`'s explicit list to version 4. It fills the Outbox through `appendOutboxMessages`, and uses `@1`'s `outboxMessages` as the oracle.

There is no new Interface stub. The published `@1` module is the RED baseline, against which:
- R11's 3 tests fail, because `@1` loads 12 sealed rows to stage one checkpoint;
- R12's 2 tests pass by design, because they pin that `@2` keeps `@1`'s checkpoint;
- `@1`'s 24 tests pass;
- both Python suites pass.

**The reference run.** A throwaway reference calls `stage-outbox-batch@2`'s `outboxBacklog`, taken from that unit's own throwaway reference, in place of `outboxMessages`. It passes all 5 new tests and `@1`'s 24.

An earlier draft of the reference queried `outbox_messages` directly, and failed `@1`'s `the_module_uses_other_units_tables_only_through_their_functions`. So the implementation must go through `stage-outbox-batch@2`'s reads, and the published suite enforces that.

Three mutants each fail at least one test:
- counting every installation;
- a byte total ignored by the counts;
- loading the sealed rows as well.

The binding is:
- **spec files:** this contract and pack, and `@1`'s contract and pack;
- **test files:**
  - `stage-sync-checkpoint-2.test.ts`, `test_stage_sync_checkpoint_2.py`, and the `@2` runner;
  - every file `stage-sync-checkpoint-pack/1` binds: `stage-sync-checkpoint-1.test.ts`, its 3ug substitutes `sender/app/test/support/node-database.ts` and `sender/app/test/support/node-installation-cipher.ts`, `test_stage_sync_checkpoint_1.py`, and `run_stage_sync_checkpoint_1.sh`.

Implementation waits until `stage-outbox-batch@2` publishes, and this branch then merges `main`. GPT 6.1 Sol's read-only implementability review runs before Gate B. The implementation adds the runner to the project CI `test` job.
