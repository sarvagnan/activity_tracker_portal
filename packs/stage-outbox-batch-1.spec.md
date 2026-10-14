# Draft acceptance pack — `stage-outbox-batch@1`

**Status:** Draft approved by the Operator on 2026-10-14, including the nine decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `stage-outbox-batch` Governed Scope contract in `governance/scopes/stage-outbox-batch.1.json`. A page read by `read-snapshot-page@1` or `read-change-page@1` claims no durable progress. This unit makes it durable, and only this unit advances a cycle's cursor. It works in four steps:
1. It partitions the page into numbered Ingest Batch fragments with `ingest-protocol@1`'s partition rule.
2. It seals each fragment independently with AES-256-GCM under the non-exportable installation key.
3. It commits the sealed messages to the Sender Outbox with the cycle's next page position, counts, chain value, and cursor, in one SQLite transaction (ADR 0012, 0014, 0034, 0047).
4. If anything fails, it commits nothing. The cursor then stays where it was, and the same page is read again later.

The unit is TypeScript only. The page arrives as a TypeScript value that mirrors the Kotlin reader's result. The Expo bridge that carries it across, and the decision of when to stage, belong to `coordinate-sender-sync`.

## Interface

**TypeScript**, module `sender/app/src/outbox/stage-outbox-batch.ts`. It uses:
- the 3ug storage seam (`SenderDatabase`, `Migration`);
- the 3ug `InstallationCipher` seam;
- the published protocol library `@activity-tracker-portal/ingest-protocol` (`partitionPage`, `chainStart`, `ProtocolRefusal`);
- `begin-initial-snapshot@1`'s `currentSnapshotAttempt`.

The stub declares every exported type: `ObservedPage` (`SnapshotPage | ChangePage`), `ChangeItem`, `StagingContext`, `StagingGeneration`, `CycleKind`, `CycleProgress`, `ChangeOrigin`, `StageResult`, `OutboxKind`, `OutboxMessage`, and `OutboxRow`.

### The page

`ObservedPage` is one of two shapes. Both carry `recordType`, `cycleId`, `projectionSchemaVersion`, and a one-based `pageIndex`.
- **A snapshot page:**

  ```ts
  { source: "snapshot", readFrom: Uint8Array | null, projections: Uint8Array[],
    nextPosition: Uint8Array, exhausted: boolean }
  ```

  `readFrom` is the position the page was read from, and `null` for the first page.
- **A change page:**

  ```ts
  { source: "change", readFrom: string, items: ChangeItem[],
    nextChangesToken: string, nextUnreadSinceMs: number, hasMore: boolean }
  ```

  `readFrom` is the changes token the page was read from. A `ChangeItem` is either `{ kind: "retraction", sourceIdentity }` or `{ kind: "projection", bytes }`, in the reader's order.

### The staging context

`StagingContext` is:

```ts
{ installationId, keyAlias,
  generation: { generation, maxRecordCount, maxMessageBytes, projectionSchemas } }
```

`projectionSchemas` is a plain object mapping each record type to its supported projection schema versions; only its own properties count. The context comes from `manage-sender-enrollment`'s cached capability generation, and the caller supplies it.

### Functions

- `stageOutboxBatch(database, cipher, context, page)` returns a `StageResult`, which is one of:
  - `{ outcome: "staged", progress: CycleProgress, firstSequence, lastSequence }`
  - `{ outcome: "refused" | "blocked" | "conflict" | "cipher-failed" | "storage-failed", failure: string }`
- `cycleProgress(database, cycleId)` returns a `CycleProgress` or `null`. A `CycleProgress` has:
  - `cycleId`, `recordType`, `cycleKind`, `snapshotId`, and `installationId`;
  - `pagesStaged`, `fragmentCount`, `recordCount`, and `chain`;
  - `position` (snapshot cycles) or `changesToken` and `unreadSinceMs` (Change Cycles);
  - `finished`.
- `changeOrigin(database, recordType)` returns `{ snapshotId, changesToken, unreadSinceMs }`, where the record type's next new Change Cycle must read from (R6), or `null` when the type has no current snapshot attempt.
- **Shared Outbox primitives** (Decision 2), which later stagers use in their own transactions:
  - `outboxAad(installationId, sequence)`, which is synchronous;
  - `lastOutboxSequence(database, installationId)`, which is 0 before the installation's first message;
  - `appendOutboxMessages(transaction, installationId, expectedLastSequence, messages)`, where each message is `{ kind, sealed, messageBytes }`. It returns `false` and writes nothing when the high-water mark is no longer `expectedLastSequence`.
  - `outboxMessages(database)`, which lists every row as `{ installationId, sequence, kind, sealed, messageBytes }`, ordered by installation and sequence.
- `outboxMigration`, which is Sender schema version 2.

Caller errors throw `TypeError`; R12 lists them.

## Requirements

Each requirement is bound to named executable tests:
- **TypeScript:** `node:test` suites in `stage-outbox-batch-1.test.ts`. They use the 3ug `node:sqlite` `SenderDatabase` substitute, the 3ug `NodeInstallationCipher` substitute, and `begin-initial-snapshot@1` to create attempts.
- **Python:** `tests/acceptance/test_stage_outbox_batch_1.py` checks the contract.

The runner `tests/acceptance/run_stage_outbox_batch_1.sh` runs both. It verifies a fresh JUnit report rather than exit statuses. Fixtures are synthetic.

### R1 — Governed Scope contract

The contract names:
- the scope, version, kind, and purpose;
- inputs, with their modes;
- outputs;
- every named failure, with one of the outcome classes `refusal`, `block`, `conflict`, `cipher-failure`, or `storage-failure`;
- both effects;
- authority;
- the four dependencies;
- the acceptance oracle.

It does not contradict the `stage-outbox-batch` scope-map row, which now names `begin-initial-snapshot` and the Sender Outbox.

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — Fragments follow the protocol partition

Each staged page's fragments are exactly the bytes that `ingest-protocol@1`'s `partitionPage` produces for:
- the context's installation and generation, and the generation's limits;
- the page's record type, projection schema version, cycle kind, cycle identity, and page index;
- a first sequence one above the installation's high-water mark;
- the cycle's saved chain value;
- the page's items in order.

A snapshot projection becomes `{"kind": "projection", "data": <base64>}`. Change items keep the reader's order, retractions before projections. An empty page stages exactly one fragment with no items. Sequences continue across cycles and record types for the same installation, and each installation has its own. A sequence whose row was later removed, as `send-outbox-message` removes delivered rows, is never reused.

Tests: `fragments_match_the_protocol_partition`, `items_keep_their_page_order`, `an_empty_page_stages_one_empty_fragment`, `sequences_continue_across_cycles_and_types`, `removed_sequences_are_never_reused`.

### R3 — Each fragment is sealed with its own authenticated data

Each fragment is sealed once, with `cipher.seal(keyAlias, fragmentBytes, outboxAad(installationId, sequence))`. `outboxAad` binds the sealed bytes to their Sender Installation and sequence. Its bytes are the UTF-8 encoding of `activity-tracker-outbox-v1:<installationId>:<sequence>`, with the sequence in decimal.

A stored row opens to its exact fragment bytes only with its own authenticated data. Opening it with another sequence or installation fails authentication.

No fragment bytes, projection bytes, Source Identities, or message hashes appear in the database outside the sealed blobs. The test checks this by scanning the database file for synthetic sentinels, base64 projections, fragment JSON, and each fragment's SHA-256.

Staging never creates, deletes, or opens with an installation key. Every staging in the suite runs through a guarded cipher that records any such call, and the test fails if one occurs.

Tests: `each_fragment_is_sealed_with_its_own_aad`, `sealed_rows_fail_under_another_sequence`, `staging_never_creates_deletes_or_opens_keys`, `the_database_holds_no_plaintext_items`.

### R4 — One atomic commit

`staged` is returned only after one SQLite transaction has committed all of the following:
- **Outbox rows:** one per fragment, with its sequence, installation, kind `ingest-batch`, sealed bytes, and plaintext byte length.
- **The high-water mark:** the installation's sequence high-water mark, raised to the last sequence.
- **The cycle's progress:**
  - `pagesStaged` is the page index;
  - the fragment and record counts grow by the page's fragments and items;
  - `chain` is the value after the last fragment;
  - the cursor is the page's `nextPosition`, or its `nextChangesToken` with `nextUnreadSinceMs`;
  - `finished` is `exhausted`, or `!hasMore`.

The progress and Outbox survive reopening the database. The counts and chain equal what the Portal's delivery evaluation would accumulate for the same fragments, so a later completion can carry them.

Tests: `staging_commits_messages_and_progress_together`, `progress_survives_reopening`, `counts_and_chain_accumulate_across_pages`.

### R5 — A snapshot page continues its snapshot

A snapshot page's `cycleId` must be its record type's current snapshot attempt, per `begin-initial-snapshot@1`; otherwise `cycle-not-current`. Further checks:
- **Kind:** the attempt's kind sets the cycle kind. `initial` gives `initial-snapshot`, and `reconciliation` gives `reconciliation-snapshot`.
- **Installation:** an attempt begun under another Sender Installation than the context's is `installation-mismatch`.
- **Position:** the page's `pageIndex` must be one above the cycle's pages staged, and `readFrom` must equal its saved position (`null` before the first page). Otherwise `page-position-mismatch`.
- **Finished cycles:** a page after the exhausted page is `cycle-finished`.
- **Kind of page:** a snapshot page naming a Change Cycle, or a page of another record type, does not name its type's current attempt, so it is `cycle-not-current`.

The first staged page creates the cycle's progress from its empty initial state: no pages, zero counts, and the chain start of the snapshot identity.

Tests: `a_snapshot_page_must_belong_to_the_current_attempt`, `an_abandoned_attempt_stages_nothing`, `the_attempt_kind_sets_the_cycle_kind`, `another_installation_is_refused`, `pages_must_continue_from_the_saved_position`, `an_exhausted_snapshot_takes_no_more_pages`.

### R6 — A Change Cycle continues the change stream

A Change Cycle descends from its record type's current snapshot attempt. Once that attempt stops being current, every page of a cycle under it is `cycle-not-current`.

The first page of a new cycle identity must meet three conditions:
- the attempt's snapshot chain is finished, otherwise `snapshot-unfinished`;
- no earlier Change Cycle under the attempt is unfinished, otherwise `change-cycle-open`;
- `readFrom` equals the change origin, otherwise `page-position-mismatch`. The origin is the final changes token of the latest finished Change Cycle under the attempt, or, if there is none, the attempt's pre-read token. The origin's unread-since instant comes with it, and `changeOrigin` reports both. An unfinished cycle does not move the origin.

Later pages continue as in R5. The saved changes token takes the place of the saved position, and a page after the one without `hasMore` is `cycle-finished`. A change page is `cycle-not-current` when it names:
- any snapshot attempt's identity, whether staged or not, current or abandoned;
- a cycle under another attempt;
- a cycle of another record type.

Change Cycles are ordered by their lineage number, never by identity.

Tests:
- `the_first_change_cycle_starts_from_the_pre_read_token`
- `later_change_cycles_continue_from_the_final_token`
- `a_change_cycle_waits_for_its_snapshot`
- `one_change_cycle_is_open_at_a_time`
- `a_new_attempt_supersedes_older_cycles`
- `change_pages_must_continue_from_the_saved_token`

### R7 — The capability generation

The page's record type, and its projection schema version, must be in the generation's `projectionSchemas`; otherwise `projection-schema-unsupported`. That way the Sender never stages a message the Portal would block as `unsupported-source-projection-schema`.

Every fragment carries the generation number as its `capability_generation`, and is partitioned under that generation's limits.

Tests: `unsupported_projections_are_refused`, `fragments_carry_the_staging_generation`.

### R8 — An indivisible item blocks the page

An item that cannot fit in an otherwise empty fragment refuses the whole page as `blocked`, with `indivisible-record-limit-exceeded`. Nothing is sealed or written. Staging the same page under a generation with larger limits then succeeds.

Tests: `an_oversized_item_blocks_the_whole_page`, `a_larger_generation_stages_the_blocked_page`.

### R9 — Nothing staged on failure

None of these writes anything: a refusal, a block, a conflict, a cipher failure, or a storage failure. The database is compared before and after each case.

Cipher failures are classified as follows:
- `missing-key` is `installation-key-missing`.
- `unusable-key` is `installation-key-unusable`.
- `keystore-unavailable` is `keystore-unavailable`.
- A cipher that throws any value, or returns anything else, is `unexpected-cipher-failure`. That includes an empty or non-byte sealed value and any other failure reason, such as `authentication-failed`.

A failure while sealing the third of four fragments stores none of them.

A database error at any point is `storage-failed`. That covers:
- a failing read before sealing;
- a failing read or write inside the commit transaction;
- a failing commit.

Tests: `refusals_and_blocks_write_nothing`, `cipher_failures_write_nothing`, `a_mid_page_cipher_failure_stores_no_fragment`, `storage_errors_roll_back_everything`.

### R10 — Staging rechecks its state inside the commit

The cycle's progress, its attempt's currency, and the installation's high-water mark are read before sealing; for a new Change Cycle, so are its snapshot's progress and the latest Change Cycle under the attempt. They are read again inside the commit transaction. If any of them changed, the result is `conflict` with `staging-conflict`, nothing is written, and the sealed bytes are discarded. A retry partitions and seals again from the new state. `appendOutboxMessages` applies the same rule to the high-water mark for every stager.

The recheck must run inside the transaction: a change made just before the transaction opens is a conflict too. Each rechecked state is tested on its own:
- the high-water mark;
- the cycle's progress;
- the attempt's currency;
- for a new Change Cycle, the latest cycle and the snapshot's progress.

Tests: `interleaved_stagings_conflict_without_writing`, `a_retry_after_conflict_stages_from_the_new_state`, `the_recheck_runs_inside_the_commit_transaction`, `append_refuses_a_moved_high_water_mark`.

### R11 — The Sender schema migration

`outboxMigration` is Sender schema version 2, registered after `begin-initial-snapshot@1`'s version 1. It creates exactly these three tables, compared token for token with whitespace ignored, and nothing else besides their automatic indexes:

```sql
CREATE TABLE outbox_messages (
  installation_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK (sequence > 0),
  kind TEXT NOT NULL CHECK (kind IN ('capability-acknowledgement', 'ingest-batch', 'snapshot-completion', 'change-completion', 'sync-checkpoint')),
  sealed BLOB NOT NULL, message_bytes INTEGER NOT NULL CHECK (message_bytes > 0),
  PRIMARY KEY (installation_id, sequence)) STRICT;
CREATE TABLE outbox_high_water (
  installation_id TEXT PRIMARY KEY NOT NULL, last_sequence INTEGER NOT NULL CHECK (last_sequence > 0)) STRICT;
CREATE TABLE cycle_progress (
  cycle_id TEXT PRIMARY KEY NOT NULL, record_type TEXT NOT NULL,
  cycle_kind TEXT NOT NULL CHECK (cycle_kind IN ('initial-snapshot', 'reconciliation-snapshot', 'change-cycle')),
  snapshot_id TEXT NOT NULL, lineage INTEGER NOT NULL CHECK (lineage >= 0), installation_id TEXT NOT NULL,
  pages_staged INTEGER NOT NULL CHECK (pages_staged > 0), fragment_count INTEGER NOT NULL CHECK (fragment_count > 0),
  record_count INTEGER NOT NULL CHECK (record_count >= 0), chain TEXT NOT NULL,
  position BLOB, changes_token TEXT, unread_since_ms INTEGER, finished INTEGER NOT NULL CHECK (finished IN (0, 1)),
  UNIQUE (snapshot_id, lineage),
  FOREIGN KEY (snapshot_id, record_type) REFERENCES snapshot_attempts (snapshot_id, record_type),
  CHECK ((cycle_kind = 'change-cycle') = (lineage > 0)),
  CHECK ((lineage = 0) = (cycle_id = snapshot_id)),
  CHECK (CASE WHEN cycle_kind = 'change-cycle'
    THEN position IS NULL AND changes_token IS NOT NULL AND unread_since_ms IS NOT NULL
    ELSE position IS NOT NULL AND changes_token IS NULL AND unread_since_ms IS NULL END)) STRICT;
```

- **The Outbox** keys each sealed message by installation and sequence, and constrains its kind to the five Outbox Message kinds.
- **The high-water marks** hold each installation's last sequence.
- **Cycle progress** references the snapshot attempt each cycle descends from. `lineage` is 0 for the snapshot cycle itself, whose identity is the attempt's, and numbers the attempt's Change Cycles 1, 2, and so on.

Nothing else reads or writes these tables except this module's functions. A database already at version 2 is unchanged by migration.

Tests: `migration_creates_version_two`, `the_schema_constrains_outbox_kinds`, `migration_is_idempotent`.

### R12 — Caller errors, health-data-free outcomes, and order of checks

**Caller errors.** A malformed page or context throws `TypeError` before any database or cipher call:
- an `installationId` or `cycleId` that is not a lowercase UUID, or an empty or non-string `keyAlias`;
- a `recordType` that does not match the Wire Protocol record-type pattern of at most 64 characters, in the page or as a `projectionSchemas` key;
- a `generation`, `maxRecordCount`, `projectionSchemaVersion`, `pageIndex`, or listed schema version that is not a positive safe integer, a `maxMessageBytes` that is not a safe integer of at least 4096, or `projectionSchemas` that is not a plain object, whose prototype is `Object.prototype` or `null`, of arrays;
- an unknown `source`; a snapshot `readFrom` that is neither `null` nor bytes, `nextPosition` that is not bytes, or a non-boolean `exhausted`;
- a change `readFrom` or `nextChangesToken` that is empty or not a string, a `nextUnreadSinceMs` that is not a safe integer, or a non-boolean `hasMore`;
- a projection that is not non-empty bytes, a Source Identity that is not 1 to 256 Unicode code points free of control characters below U+0020, or an item of another kind.

`outboxAad` throws `TypeError` for a malformed installation or a sequence that is not a positive safe integer. `appendOutboxMessages` throws `TypeError` for a malformed installation, an `expectedLastSequence` that is not a non-negative safe integer, a missing or empty message list, or any message with an unknown kind, empty or non-byte sealed bytes, or a byte length that is not a positive safe integer. It validates every argument and message before any read or write.

**Hermes.** The module runs under Hermes in the Sender. It imports only relative modules and `@activity-tracker-portal/ingest-protocol`, and uses no `Buffer`, `require`, or `process`.

**Health-data-free outcomes.** Every non-`staged` result carries only a named failure. Every named failure is reachable, and no other failure is produced.

**Order of checks.** The first applicable check in this order wins:
1. caller errors;
2. the generation's projection schemas;
3. reading the state, where a failing read is `storage-failed`, then the cycle checks of R5 and R6 in this order: `cycle-not-current`, `installation-mismatch`, `cycle-finished`, `snapshot-unfinished`, `change-cycle-open`, `page-position-mismatch`;
4. partitioning and its block;
5. sealing;
6. the commit, with its recheck and storage failure.

Tests: `malformed_inputs_are_caller_errors`, `non_staged_results_carry_only_named_failures`, `every_named_failure_is_reachable_and_in_the_contract`, `checks_follow_the_documented_order`, `the_module_imports_no_node_built_ins`.

## Decisions for the Operator

1. **TypeScript only.** Sender storage, the protocol library, and the cipher seam are all TypeScript (ADR 0014, 0016, 0018), so this unit needs no Kotlin. Pages arrive as TypeScript values that mirror the published readers' results. The Expo bridge waits for `coordinate-sender-sync`, as it does for the readers.
2. **This unit owns the Sender Outbox.** It is the first Outbox writer, so it owns:
   - the Outbox table;
   - each installation's sequence high-water mark, which never decreases, so a sequence removed after its receipt is never reused;
   - the sealing and authenticated-data format.

   Later stagers append through its primitives inside their own transactions: `stage-sync-completion`, `stage-sync-checkpoint`, and `manage-sender-enrollment`'s capability acknowledgement. `send-outbox-message` opens and removes rows. The `stage-sync-checkpoint` and `manage-sender-enrollment` rows therefore gain a dependency on `stage-outbox-batch`.

   *Alternative:* a separate `sender-outbox` scope that every stager depends on. That is cleaner in isolation, but it adds a unit, and this one would be its only Phase 2 consumer.
3. **Seal before the commit, then recheck.** The 3ug connection rejects, rather than queues, an operation while another is active. Holding SQLite's write transaction open across Keystore calls, which StrongBox can make slow, would stall all other Sender storage use for that time.

   So the unit:
   - reads its state;
   - partitions and seals outside the transaction;
   - rechecks inside a short commit (R10).

   A concurrent change, such as the other record type's traversal taking the next sequences, is a `staging-conflict`. It writes nothing, and the page is retried.

   *Alternative:* sealing inside the transaction, which needs no conflict outcome.

   Reserving sequences before sealing was rejected, because a failed seal would burn sequences, and the Portal would report them as a delivery gap.
4. **Change Cycles are anchored to the current snapshot attempt** (ADR 0047, 0048):
   - The first Change Cycle drains from the attempt's pre-read token.
   - Each later cycle continues from the previous cycle's final token.
   - A record type has one unfinished Change Cycle at a time, and none starts before its snapshot's page chain is finished.
   - A new attempt, whether abandoned and replaced, or a reconciliation, supersedes every cycle under the old one.

   This makes the race-closing bridge executable at the point where the cursor advances. Whether the Snapshot Completion has been staged or acknowledged before the catch-up cycle starts is `coordinate-sender-sync`'s ordering, because completion state belongs to the downstream `stage-sync-completion`.
5. **Change Cycle identity is the caller's.** `read-change-page@1` already takes the cycle identity as input, so `coordinate-sender-sync` mints a random version 4 identity before the first read. The cycle's first staged page creates its progress. A Change Cycle needs no pre-read token, so it has no separate begin step.
6. **An oversized item blocks the page, not the Sender.** The page is refused whole, and nothing is written, as ADR 0012 requires. Recording and reporting the per-type block belongs to `coordinate-sender-sync`, which reports it in the Sync Checkpoint. A later generation with larger limits stages the same page.
7. **What stays plaintext.** Each sealed blob's authenticated data binds it to its installation and sequence, so a row cannot be swapped or replayed under another sequence (ADR 0014). Outside the blobs, the database holds only health-data-free bookkeeping:
   - each row's sequence, installation, kind, and byte length;
   - each cycle's counts, chain value, and opaque Health Connect position or token.

   It holds no projection bytes, Source Identities, or message hashes.
8. **The Sender's second schema migration.** This unit owns Sender schema version 2, appended after `begin-initial-snapshot@1`'s version 1, as that unit's Decision 5 set out.
9. **A new dependency on `begin-initial-snapshot@1`.** Snapshot currency and the change origin come from its attempts, through `currentSnapshotAttempt`. The scope-map row named no such dependency, so it is updated. Staging a reconciliation snapshot uses the same attempt table, so it needs no dependency on the unpublished `begin-reconciliation-snapshot`.

## Ground rules

- TypeScript tests use:
  - the 3ug `node:sqlite` substitute in temporary files;
  - the 3ug `NodeInstallationCipher`, with real AES-256-GCM, its synthetic invalidation and outage, and a recording `SpyCipher` that delegates to it. The spy can replace a numbered seal call with a returned value or a thrown value, or run a hook before it;
  - `begin-initial-snapshot@1` with a scripted issuer to create and abandon attempts. A Reconciliation Snapshot attempt is written directly to that unit's tables, because `begin-reconciliation-snapshot` is not yet delivered.

  They inject database failures through a wrapping `SenderDatabase`. It fails each mutating statement in turn, every read, every read inside the transaction, or the commit. A mutating statement is one that begins with `INSERT`, `UPDATE`, `DELETE`, or `REPLACE`, possibly after a `WITH` clause.

  An untouchable database counts and refuses every use, which proves a check comes before storage.

  Concurrency is simulated in two ways:
  - a seal hook that stages or begins on the same connection while the page is being sealed;
  - a hook that changes state just before the commit transaction opens.
- The runner does not trust exit statuses. It passes the suite only when a fresh JUnit report lists exactly the bound suite's tests, none failed, errored, or skipped.
- The trusted toolchain boundary is:
  - Node 26.11.1, pinned by `.node-version`;
  - `uv` and the pinned CPython;
  - the 3ug storage and cipher seams in `sender/app/src/storage/` and `sender/app/src/installation-cipher.ts`;
  - the published `@activity-tracker-portal/ingest-protocol` library and `begin-initial-snapshot@1`'s module.
- Not mechanically provable here; the approver confirms these in implementation review:
  - the production cipher is the Keystore-backed `nativeInstallationCipher()`, which the caller supplies;
  - the implementation registers `outboxMigration` second in the production migration list `senderMigrations`, after `snapshotAttemptsMigration`. In `sender/app/src/storage/migrations.ts`, that import and list entry, with its comment, is the only change permitted. Nothing else in the seams or published modules changes;
  - the implementation updates the 3ug storage test's production-migration expectation to version 2 and this unit's tables, which is the only change it makes to that unbound test.

## Freeze prerequisites

The Operator approved the draft. The executable pack adds:
- the TypeScript suite, 44 tests;
- the Python contract suite, 2 tests;
- the self-verifying runner;
- the Interface stub `sender/app/src/outbox/stage-outbox-batch.ts`.

It also pins:
- the exact `outboxAad` bytes (R3);
- the R11 table layout;
- the caller-error list and the order of the cycle checks (R12);
- `changeOrigin`, which reports R6's origin.

The implementer's read-only implementability review found one blocking test: `storage_errors_roll_back_everything` required five mutating statements, where a correct implementation may use three. It now requires one for each table, and the mutation detector recognizes a leading `WITH`.

Its coverage findings are added:
- the recheck must run inside the transaction, and each rechecked state is tested on its own;
- lineage, not identity, orders Change Cycles, and the origin holds while a later cycle is unfinished;
- a change page naming an unstaged snapshot attempt is refused;
- `appendOutboxMessages` validates every message before storage;
- caller errors and unsupported projections touch no storage;
- the module avoids Node built-ins.

The permitted edit to `migrations.ts` is now explicit.

The approver's first Gate B review required two more checks, both added:
- every staging runs through a cipher guard that fails on any key creation, deletion, or opening;
- a class instance or `Map` as `projectionSchemas` is a caller error.

A throwaway reference implementation passed all 44 tests and typechecks. Nine mutants of it each failed at least one test:
- reading `projectionSchemas` with `in`, which inherits `constructor`;
- skipping the recheck;
- moving the recheck outside the transaction;
- rechecking only the high-water mark;
- counting a Source Identity's UTF-16 code units;
- ordering Change Cycles by identity;
- omitting the snapshot-identity refusal;
- encoding base64 with `Buffer`;
- opening each sealed row after sealing it, to verify it.

Against the stub:
- 43 TypeScript tests fail, because the stub's migration throws and the 3ug runner reports `frozen`, which the fixture asserts against;
- `the_module_imports_no_node_built_ins` passes, because it checks the module's imports and is a constraint rather than behavior;
- the two Python tests pass, because they check the contract.

The binding is:
- spec files: the contract and this pack;
- test files:
  - `stage-outbox-batch-1.test.ts`;
  - its 3ug substitutes `sender/app/test/support/node-database.ts` and `sender/app/test/support/node-installation-cipher.ts`;
  - `test_stage_outbox_batch_1.py` and the runner.
