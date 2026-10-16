# Draft acceptance pack — `send-outbox-message@1`

**Status:** Draft for Operator review (Gate A), including the nine decisions below. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `send-outbox-message` Governed Scope contract in `governance/scopes/send-outbox-message.1.json`.

The Sender keeps every Outbox Message until a Delivery Receipt for its exact identity allows removal (ADR 0009). It delivers strictly in sequence, retries identical bytes after timeouts and unknown outcomes, and lets a blocked head prevent later delivery rather than skip data. This unit makes one delivery attempt for the oldest Outbox Message of one installation:
1. It opens the head's sealed bytes with the installation key (ADR 0014).
2. It sends exactly those uncompressed bytes to the Portal.
3. It interprets the answer with `ingest-protocol@1`'s `interpretResponse`.

Only a matching Delivery Receipt removes the message. A Delivery Block, or a head the Sender itself cannot open or validate, keeps the message and sets a persistent Operator-action-required head block with one local notification (ADR 0013). `coordinate-sender-sync@1` reads that block as its `deliveryBlocked` condition.

It touches no cursor, snapshot attempt, capability state, or health state. Staging belongs to the stage units, and the Portal side to the `accept-*` units and the ingress shell. The end-to-end proof belongs to `receipt-bound-delivery`. The unit is TypeScript only.

## Interface

**TypeScript**, module `sender/app/src/delivery/send-outbox-message.ts`, in its own directory. It uses:
- `stage-outbox-batch@1`'s Outbox table, `outboxAad`, and `OutboxKind`;
- the protocol library's `decodeMessage`, `messageSha256`, and `interpretResponse`;
- the 3ug storage and cipher seams;
- the shared storage lane, `storageLane` in `storage/storage-lane.ts`.

The declarations are:
- `DeliveryContext`: `{ installationId, keyAlias, portalUrl }`. `portalUrl` is the enrollment offer's `portal_url`, which the protocol limits to a scheme, host, and optional port.
- `OutboxRequest`: `{ method: "POST", url, headers: { "content-type": "application/json" }, body, timeoutMs, maxResponseBytes }`.
- `OutboxTransport`: `send(request): Promise<{ outcome: "response", body: Uint8Array } | { outcome: "failed" }>`. The shell's adapter performs the request. It reads at most `maxResponseBytes + 1` body bytes, and it reports a timeout or network failure as `failed`.
- `DeliveryNotifier`: `notify(failure: string): Promise<void>`.
- The constants:
  - `OUTBOX_ROUTE = "/v1/outbox-messages"`;
  - `DELIVERY_TIMEOUT_MS = 60_000`;
  - `MAX_RESPONSE_BYTES = 4096`.
- `createOutboxSender({ database, cipher, transport, notifier, now, context })` returns:
  - `sendNext()`, which makes one attempt and returns one of:
    - `{ outcome: "empty" }`;
    - `{ outcome: "delivered", sequence, kind }`;
    - `{ outcome: "retryable", sequence, failure }`, where `failure` is `transport-failed`, `receipt-mismatch`, `temporarily-unavailable`, `storage-reserve-exhausted`, or `keystore-unavailable`;
    - `{ outcome: "blocked", sequence, failure }`, where `failure` is the Portal's Delivery Block failure, `head-unreadable`, or `head-invalid`;
    - `{ outcome: "cipher-failed", sequence, failure }`, where `failure` is `missing-key` or `unusable-key`;
    - `{ outcome: "storage-failed" }`.
  - `headBlock()`, which returns the installation's `HeadBlock`, `null`, or `{ outcome: "storage-failed" }`.
- `HeadBlock`: `{ sequence, origin: "portal" | "sender", failure, messageSha256, firstSeenMs, lastSeenMs, count }`. `messageSha256` is `null` when the head could not be opened.
- `deliveryMigration`: Sender schema version 7.

## Requirements

TypeScript `node:test` suites in `send-outbox-message-1.test.ts` use:
- the 3ug `node:sqlite` and `NodeInstallationCipher` substitutes;
- synthetic transport, Portal, and notifier fakes;
- the published `stage-outbox-batch@1`, `stage-sync-completion@1`, and `stage-sync-checkpoint@1`, which stage the messages delivered.

Tests migrate an explicit list to version 7. Assertions that something wrote nothing compare the whole database. `tests/acceptance/test_send_outbox_message_1.py` checks the contract. The runner verifies a fresh JUnit report and sets a per-test timeout. Fixtures are synthetic.

### R1 — Governed Scope contract

The contract names:
- the scope, version, kind, and purpose;
- inputs, with their modes;
- outputs;
- 11 named failures in the outcome classes `retryable`, `head-block`, `cipher-failure`, and `storage-failure`;
- three effects;
- authority;
- the four dependencies;
- the acceptance oracle.

It does not contradict the scope-map row.

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — Caller errors

Malformed options throw `TypeError` before any transport, notifier, cipher, or storage call. They include:
- a missing seam method;
- a `now` that is not a function;
- an `installationId` that is not a lowercase UUID;
- an empty `keyAlias`;
- a `portalUrl` the protocol's pattern refuses.

`sendNext()` reads `now()` once, before any other call. It rejects with `TypeError`, calling nothing else, when the reading is not a safe integer.

Tests: `malformed_options_are_caller_errors`, `an_invalid_clock_reading_is_a_caller_error`.

### R3 — The oldest message, once per attempt

Each `sendNext()` makes one attempt for the installation's lowest-sequence Outbox row:
1. It opens the sealed bytes with the context's key alias and `outboxAad(installationId, sequence)`.
2. It calls `transport.send` exactly once with `{ method: "POST", url: portalUrl + "/v1/outbox-messages", headers: { "content-type": "application/json" }, body, timeoutMs: 60000, maxResponseBytes: 4096 }`. The body is the exact plaintext: uncompressed, byte for byte the staged message.

Every attempt for the same sequence sends identical bytes. Rows of other installations are never sent. With no row for the installation, `sendNext()` returns `empty`, sends nothing, and writes nothing.

Tests:
- `an_empty_outbox_sends_nothing`
- `the_oldest_message_is_sent_exactly`
- `other_installations_are_never_sent`

### R4 — Receipt-bound removal

When `interpretResponse` accepts the answer, the unit deletes exactly the head's row and returns `delivered` with its sequence and kind. A receipt accepted there echoes the head's installation, sequence, and SHA-256. A head block for that sequence is cleared in the same transaction. Nothing else changes: the high-water mark, every other row, and every other table stay the same. The next attempt sends the next sequence.

Every kind is delivered the same way:
- an Ingest Batch, Snapshot Completion, and Change Completion, staged through `stage-outbox-batch@1` and `stage-sync-completion@1`;
- a Sync Checkpoint, staged through `stage-sync-checkpoint@1`;
- a capability acknowledgement, appended through `stage-outbox-batch@1`'s Outbox.

Tests:
- `a_matching_receipt_removes_exactly_the_head`
- `every_kind_is_delivered_in_sequence`
- `removal_keeps_the_high_water_mark`

### R5 — Everything else retains identical bytes

Each of these answers keeps the head and writes nothing, and the next attempt resends identical bytes:
- **No response.** The transport reports `failed`, rejects, or throws. The outcome is retryable `transport-failed`.
- **Oversized body.** The body is longer than 4096 bytes. It is not decoded, and the outcome is retryable `receipt-mismatch`.
- **Undecodable or mismatched answer.** This covers a body that does not decode, a receipt for another installation, sequence, or digest, a Delivery Block for another message, and any other message kind. These are retryable `receipt-mismatch`, as `interpretResponse` decides.
- **Retryable failure.** The outcome is retryable with the Portal's reason, `temporarily-unavailable` or `storage-reserve-exhausted`.

An existing head block is left unchanged by all of these.

Tests:
- `no_response_keeps_identical_bytes`
- `mismatched_answers_keep_the_head`
- `retryable_failures_keep_the_head`

### R6 — Portal Delivery Blocks

When `interpretResponse` reports a block, the unit:
- keeps the row;
- records the head block: the sequence, origin `portal`, the Portal's failure, the message SHA-256, first and last seen times of `now()`, and a count of 1;
- returns `blocked` with the sequence and failure.

A further block for the same head sets its last seen time, raises its count by one, and takes the latest failure. Every later attempt sends the blocked head again, never a later sequence. When the Portal later accepts that head, its receipt removes the row and clears the block.

Tests:
- `a_delivery_block_keeps_the_head_and_records_it`
- `a_blocked_head_is_retried_and_never_skipped`
- `a_receipt_clears_the_head_block`

### R7 — Local head checks

The opened head must be the row's own message: canonical bytes that decode as an Outbox Message whose kind, `sender_installation`, and `sequence` equal the row's, and whose length equals the row's message bytes. Nothing is sent unless it is. The cipher results map as follows:
- **`keystore-unavailable`:** retryable `keystore-unavailable`, and nothing is written.
- **`missing-key` or `unusable-key`:** `cipher-failed`, and nothing is written. This fails the whole installation, not the head (Decision 5).
- **`authentication-failed` or `unsupported-format`:** a head block with origin `sender`, failure `head-unreadable`, and a `null` digest.
- **A head that is not the row's own message:** a head block with origin `sender`, failure `head-invalid`, and the plaintext's digest.

Local head blocks repeat, count, notify, and clear as Portal blocks do.

Tests:
- `an_unavailable_keystore_is_retryable`
- `an_unusable_key_fails_without_blocking`
- `an_unreadable_head_is_a_local_block`
- `a_head_that_is_not_its_row_is_a_local_block`

### R8 — One notification at each onset

The notifier is called with the failure alone after a head block is first recorded, and its success is then recorded. That happens at most once per head block, at least once:
- a notifier that throws, synchronously or not, is retried at the next attempt that observes the same block, until it succeeds;
- if its success cannot be recorded, `sendNext()` reports `storage-failed`, and the next attempt notifies again.

A repeated block, including one with a new failure, does not notify again. A new head block after a receipt cleared the last one is a new onset. `sendNext()` resolves only after any notification attempt, and the attempt to record its success, have settled.

Tests:
- `a_head_block_notifies_once`
- `a_failed_notification_is_retried`
- `a_lost_acknowledgement_notifies_again`

### R9 — Reading the head block

`headBlock()` returns the installation's head block exactly as recorded, or `null`, and writes nothing. It returns `storage-failed` when its read fails. The block survives a restart: another instance on the same database reads it.

Tests: `the_head_block_is_read_exactly`, `the_head_block_survives_a_restart`.

### R10 — Storage and concurrency

A storage failure in any read or write returns `storage-failed` and leaves the database as it was before that write. This includes a failed removal after a receipt: the row stays, and the next attempt receives the Portal's identical receipt. A failed block recording does not call the notifier. A storage error of any type, including `TypeError`, is a storage failure, not a caller error.

Every storage operation waits its turn in the shared storage lane. The transport call and the notifier never hold it, so staging proceeds while an attempt waits for the Portal. Attempts of one instance run one at a time. Attempts of separate instances may overlap: they send identical bytes, and the head is removed once.

Tests:
- `storage_failures_change_nothing`
- `the_network_call_does_not_hold_the_storage_lane`
- `attempts_run_one_at_a_time`
- `overlapping_instances_remove_the_head_once`

### R11 — The Sender schema migration

`deliveryMigration` is Sender schema version 7, registered after `schedule-background-polling@1`'s version 6. It leaves every earlier schema object unchanged, and it references no other unit's table. It creates exactly this table, compared token by token with whitespace ignored, and nothing else:

```sql
CREATE TABLE outbox_head_block (
  installation_id TEXT PRIMARY KEY NOT NULL,
  sequence INTEGER NOT NULL CHECK (sequence > 0),
  origin TEXT NOT NULL CHECK (origin IN ('portal', 'sender')),
  failure TEXT NOT NULL,
  message_sha256 TEXT,
  first_seen_ms INTEGER NOT NULL,
  last_seen_ms INTEGER NOT NULL,
  count INTEGER NOT NULL CHECK (count > 0),
  notified INTEGER NOT NULL CHECK (notified IN (0, 1)),
  CHECK ((origin = 'sender') = (failure IN ('head-unreadable', 'head-invalid'))),
  CHECK ((message_sha256 IS NULL) = (failure = 'head-unreadable'))) STRICT
```

Tests: `migration_creates_version_seven`, `migration_is_idempotent`.

### R12 — Delivery under faults

Seeded synthetic histories interleave staging every kind with attempts against a fake Portal. The fake Portal archives what it accepts and injects faults:
- requests lost before arrival;
- responses lost after archival;
- mismatched receipts and oversized bodies;
- retryable failures;
- Delivery Blocks that later clear.

After every step:
- the Outbox equals the staged messages minus those whose matching receipts the Sender received;
- every request for a sequence carried identical bytes;
- no request carried a later sequence while an earlier one remained;
- the head block exists exactly when the head's last definitive answer was a block, and no receipt has cleared it since.

The test requires each fault, each kind, a block that clears, and a lost response retried to its identical receipt to occur.

Tests: `delivery_survives_faults_in_sequence`.

## Decisions for the Operator

1. **`ingest-protocol@1` interprets the answer, and the HTTP status is not read.** The body is a canonical protocol message, and removal rests on the receipt echoing the head's exact identity. A status that disagreed with its body could only hide an answer: a block sent with 200 would retry silently, and a receipt sent with 503 would never be removed. The alternative is to require 200 for a receipt, 409 for a block, and 503 for a retryable failure.
2. **One HTTP binding, without compression.** The binding is a POST to `{portalUrl}/v1/outbox-messages`:
   - with `content-type: application/json` and the exact uncompressed bytes;
   - with a 60-second timeout, which the adapter enforces;
   - reading a response body of at most 4096 bytes. Every protocol answer fits in under 400 bytes.

   The ingress route (Bead 2rv.4) follows this binding. ADR 0012 permits gzip but does not require it. React Native's `fetch` cannot compress a request body without a new pinned dependency, and messages are capped by the advertised byte limit over the tailnet. The alternative is gzip through a pinned library.
3. **One attempt per call, for the oldest message.** The shell calls `sendNext()` again while it returns `delivered`, and it decides when to attempt delivery: after each Polling Run, at foreground return, and on manual sync. That keeps the Sender shell's cadence out of a governed unit. The alternative is a draining loop with a time budget here.
4. **A head block lasts until the head's receipt, and the blocked head is still retried.** ADR 0046 lets corrected Portal code accept an identical retry. Each attempt therefore resends the blocked head, never a later message. Retryable answers and lost responses prove nothing about the block, so they leave it. The alternative is to stop attempting a blocked head until the Operator acts.
5. **A head the Sender cannot open or validate is a local head block, but a missing or unusable key is not.** Corrupt ciphertext, or a head that is not its row's own message, prevents delivery exactly as a Portal block does. It must stop collection and notify. A missing or unusable key fails every message, not just the head. It is reported as `cipher-failed`, and freezing the installation belongs to `manage-sender-enrollment` (ADR 0014). An unavailable Keystore is temporary and retryable.
6. **One notification at each onset, delivered at least once.** This uses the rule `schedule-background-polling@1` set. A repeated block, even with a new failure, is not a new onset; a new head block after a receipt is. Ordinary retries never notify (ADR 0013).
7. **Advertised capabilities are not consulted.** The Portal evaluates every message against the generation it was staged under (ADR 0031), and the Sender uses the limits only when staging. The scope-map row's inputs therefore drop "advertised protocol capabilities". They name the installation key and the Portal URL instead, and its outputs name the head block and its notification.
8. **Attempts run one at a time per instance, and the network never holds the storage lane.** Staging must not wait up to 60 seconds behind a slow Portal. Identical retries make overlapping instances harmless: the Portal answers each with the same receipt, and removal is idempotent.
9. **Removal and block clearing commit together.** If that commit fails after a receipt, the message stays, and the next attempt receives the Portal's identical receipt (ADR 0009). Nothing is removed before a receipt, and no receipt is lost by retrying.

## Ground rules

- Fixtures are synthetic.
- Messages of every kind are staged through the published stage units.
- The fake Portal builds its answers with the protocol library.
- A busy or failing database is injected through the 3ug seam.

## Freeze prerequisites

After Gate A, the executable pack adds:
- the TypeScript suite;
- the Python contract test;
- the runner;
- the Interface stub `send-outbox-message.ts`, whose functions throw, and whose types and constants are declared.

Against the stub, every behaviour test fails and the two contract tests pass. GPT 6.1 Sol's read-only implementability review runs before Gate B. Every dependency is published, so Vouch records `specify` after Gate A.
