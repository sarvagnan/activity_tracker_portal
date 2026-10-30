# Draft acceptance pack — `manage-sender-enrollment@1`

**Status:** Draft approved by the Operator on 2026-10-16, including the eleven decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `manage-sender-enrollment` Governed Scope contract in `governance/scopes/manage-sender-enrollment.1.json`.

`manage-sender-installation@1` runs the Portal's half of the Operator-mediated lifecycle (ADR 0009). This unit is the Sender's half:
1. It enrolls from an enrollment offer. It requires a secure screen lock (ADR 0014), creates a durable opaque identity, its fingerprint, and a non-exportable installation key, and sends one byte-stable enrollment confirmation until the Portal answers.
2. It adopts the advertised capability generation and acknowledges it as the installation's first Outbox Message (ADR 0031). The Operator's confirmation is learned when that acknowledgement's receipt removes it, because a pending installation's deliveries stay retryable (`manage-sender-installation@1` Decision 1).
3. It supplies the staging and delivery contexts the stage units and `send-outbox-message@1` need, and adopts later generations.
4. It drains, unlinks, or destructively resets its own installation. Draining stops new messages and reports the final sequence that the Operator enters in the Portal (`manage-sender-installation@1` Decision 2). The key and local state are destroyed only on explicit intent.

QR scanning, Sender screens, the HTTP client, and the native Keystore module belong to the Sender shell. The unit is TypeScript only.

## Interface

**TypeScript**, module `sender/app/src/enrollment/manage-sender-enrollment.ts`, in its own directory. It uses:
- the protocol library's `decodeMessage`, `encodeMessage`, and `installationFingerprint`;
- `stage-outbox-batch@1`'s `appendOutboxMessages`, `lastOutboxSequence`, `outboxMessages`, `outboxAad`, `StagingContext`, and `StagingGeneration`;
- the 3ug storage and cipher seams;
- the shared storage lane, `storageLane` in `storage/storage-lane.ts`.

The declarations are:
- `EnrollmentOffer`: `{ kind: "scanned", bytes }`, the canonical enrollment-offer bytes a QR code carries; or `{ kind: "typed", portalUrl, enrollmentCode }`.
- `EnrollmentRequest`: `{ method: "POST" | "GET", url, headers: { "content-type": "application/json" } | {}, body: Uint8Array | null, timeoutMs, maxResponseBytes }`.
- `EnrollmentTransport`: `send(request): Promise<{ outcome: "response", body } | { outcome: "failed" }>`. The adapter reads at most `maxResponseBytes + 1` body bytes, and it reports a timeout or network failure as `failed`.
- The constants:
  - `CONFIRMATION_ROUTE = "/v1/enrollment-confirmation"`;
  - `CAPABILITIES_ROUTE = "/v1/capabilities"`;
  - `ENROLLMENT_TIMEOUT_MS = 30_000`;
  - `MAX_ANSWER_BYTES = 65_536`.
- `EnrollmentStatus`: `{ installationId, fingerprint, keyAlias, portalUrl, phase, refusal, confirmed, stagingContext, deliveryContext, finalSequence }`, where:
  - `phase` is `confirming`, `refused`, `enrolled`, or `draining`;
  - `refusal` is `enrollment-refused` or `capabilities-unsupported` when refused, otherwise `null`;
  - `stagingContext` is a `StagingContext` once a generation is adopted, otherwise `null`;
  - `deliveryContext` is `{ installationId, keyAlias, portalUrl }`;
  - `finalSequence` is set only while draining with an empty Outbox.
- `createSenderEnrollment({ database, cipher, transport, now, randomUUID })` returns:
  - `enroll(offer)`;
  - `confirm()`;
  - `refreshCapabilities()`;
  - `beginDrain()`;
  - `unlink()`;
  - `reset({ acknowledgePossibleLoss: true })`;
  - `status()`, which returns the `EnrollmentStatus`, `null` without an enrollment, or `{ outcome: "storage-failed" }`.

  Each action returns `{ outcome, ... }` with the outcomes the requirements name:
  - a confirmation attempt, from `enroll` or `confirm`, returns `{ outcome: "enrolled", generation }`, `{ outcome: "portal-refused", failure }` naming the Portal refusal, or `{ outcome: "retryable", failure }`;
  - `refreshCapabilities` returns `{ outcome: "adopted", generation }`, or the outcome `current`, `incompatible`, `unavailable`, or `draining`;
  - `beginDrain` returns the outcome `draining`, and `unlink` and `reset` return the outcome `destroyed`.

  A refusal is `{ outcome: "refused", failure }`, naming one of `offer-invalid`, `already-enrolled`, `screen-lock-required`, `not-confirming`, `not-enrolled`, `not-confirmed`, or `not-drained`. A cipher failure is `{ outcome: "cipher-failed", failure }`, naming the cipher's failure. A storage failure is `{ outcome: "storage-failed" }`.
- `enrollmentMigration`: Sender schema version 8.

## Requirements

The 39 TypeScript `node:test` tests in `manage-sender-enrollment-1.test.ts` use:
- the 3ug `node:sqlite` and `NodeInstallationCipher` substitutes;
- synthetic transport and Portal fakes, whose answers are built with the protocol library;
- the published `stage-outbox-batch@1` primitives;
- `send-outbox-message@1`, for the test that learns confirmation from a real receipt.

Tests migrate an explicit list to version 8. Assertions that something wrote nothing compare the whole database. `tests/acceptance/test_manage_sender_enrollment_1.py` checks the contract. The runner verifies a fresh JUnit report, sets a per-test timeout, and ends the Node process after an external deadline. Fixtures are synthetic.

### R1 — Governed Scope contract

The contract names:
- the scope, version, kind, and purpose;
- inputs, with their modes;
- outputs;
- 13 named failures in the outcome classes `refusal`, `portal-refusal`, `retryable`, `cipher-failure`, and `storage-failure`;
- three effects;
- authority;
- the two dependencies;
- the acceptance oracle.

It does not contradict the scope-map row.

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — Caller errors

These throw `TypeError` before any transport, cipher, or storage call:
- malformed options;
- an offer that is not one of the two shapes;
- a `reset` without `acknowledgePossibleLoss: true`;
- a clock reading that is not a safe integer;
- a `randomUUID` result that is not a lowercase UUID the protocol accepts.

An offer of the right shape whose content is invalid is a refusal, not a caller error (R3). A scanned offer's shape is a `Uint8Array` of bytes, and a typed offer's is a string Portal URL and a string enrollment code.

`enroll`, `confirm`, and `refreshCapabilities` each read the clock once, before any transport, cipher, or storage call. `enroll` reads `randomUUID()` once there too. The other actions and `status()` never read the clock. A malformed offer or reset argument reads neither.

Tests: `malformed_options_are_caller_errors`, `invalid_seam_results_are_caller_errors`.

### R3 — Enrolling from an offer

`enroll(offer)` checks, in order:
1. **The offer.** A scanned offer must decode as a canonical `enrollment-offer`. A typed offer needs a Portal URL and a 16-character enrollment code that meet the protocol's `enrollment-offer` rules exactly, with no trimming or case folding. Otherwise the result is refused, `offer-invalid`. The offer's expiry is not checked: the Portal's clock decides (Decision 10).
2. **No existing enrollment.** Otherwise the result is refused, `already-enrolled`.
3. **A secure screen lock**, read with the cipher's `isDeviceSecure()`. Only `true` is a secure lock. Otherwise the result is refused, `screen-lock-required`.

Then it creates:
- the identity from `randomUUID()`;
- the fingerprint, with `installationFingerprint`;
- a key under the alias `activity-tracker-portal.installation.<identity>`.

A key that cannot be created is `cipher-failed` with the cipher's reason, and nothing is written. A malformed result reads as `keystore-unavailable`. The unit then records the `confirming` phase with the exact canonical enrollment-confirmation bytes, and makes the first confirmation attempt (R4), whose outcome it returns. If recording fails, it deletes the key it created and returns `storage-failed`. If that deletion also fails or throws, the result is still `storage-failed`. The key is then left unused: no row names it, nothing was sealed with it, and a later enrollment creates a new identity and key. No refusal writes anything or creates a key.

Tests:
- `a_scanned_or_typed_offer_enrolls`
- `invalid_offers_are_refused`
- `enrollment_requires_a_secure_lock`
- `an_existing_enrollment_is_refused`
- `the_identity_fingerprint_and_key_are_recorded`

### R4 — Confirmation attempts

Each attempt POSTs the stored confirmation bytes to `portalUrl + "/v1/enrollment-confirmation"` with `content-type: application/json`, a 30-second timeout, and at most 65,536 answer bytes. The bytes are identical on every attempt. `manage-sender-installation@1` R5 makes a retried confirmation idempotent. The answer decides (Decision 3):
- **A capability advertisement that lists Wire Protocol Version 1:** adoption (R5), and the result `enrolled` with the generation.
- **An advertisement without Version 1:** the phase becomes `refused` with `capabilities-unsupported`.
- **A retryable failure, or no response:** retryable `temporarily-unavailable` or `transport-failed`, with nothing written. A retryable failure of either reason is `temporarily-unavailable`. No response includes a transport that rejects or throws, or that returns anything but a response with a byte body.
- **Anything else,** including an empty body, another message kind, an undecodable body, or more than 65,536 bytes: the phase becomes `refused` with `enrollment-refused`. An answer of exactly 65,536 bytes is read.

`confirm()` repeats an attempt while the phase is `confirming`, and is refused as `not-confirming` otherwise, including without an enrollment.

Tests:
- `the_confirmation_is_sent_exactly_and_identically`
- `an_advertisement_enrolls`
- `retryable_answers_keep_confirming`
- `other_answers_refuse_the_enrollment`
- `an_unsupported_advertisement_is_refused`
- `confirm_only_while_confirming`

### R5 — Adopting a capability generation

Adopting an advertisement, whether from a confirmation or a refresh, records its exact bytes and generation. In the same transaction, it appends one capability acknowledgement at the installation's next Outbox sequence, through `appendOutboxMessages`, sealed with the installation key under `outboxAad`. The acknowledgement is the canonical `capability-acknowledgement` for that installation, sequence, and generation.

The next sequence always comes from the installation's high-water mark. A new identity has no Outbox rows, so the acknowledgement made at enrollment is sequence 1, the installation's first Outbox Message. The test also checks an identity that already has rows, which no Sender unit creates before adoption, and requires the next sequence. The enrollment acknowledgement's sequence is recorded, and later adoptions keep it.

The unit reads the next sequence and seals before the transaction that appends, which rereads the high-water mark. A sealing failure is `cipher-failed` with the cipher's reason, and nothing is written. A result that is not a nonempty byte string reads as `keystore-unavailable`. If the high-water mark moved under the append, nothing is written and the result is `storage-failed`, so a later attempt adopts again.

Tests:
- `adoption_appends_one_sealed_acknowledgement`
- `the_enrollment_acknowledgement_is_the_first_message`
- `adoption_failures_write_nothing`

### R6 — Later generations

`refreshCapabilities()` GETs `portalUrl + "/v1/capabilities"` with no body, a 30-second timeout, and at most 65,536 answer bytes. The results:
- **A newer generation that lists Version 1:** adopted as in R5, and the result is `adopted`.
- **The same or an older generation:** `current`.
- **A newer generation without Version 1:** `incompatible`. Nothing changes, and the Sender keeps staging under its adopted generation.
- **Any other answer, or no response:** `unavailable`.
- **No adopted generation yet:** refused, `not-enrolled`.
- **While draining:** `draining`. No request is sent and nothing is adopted, so the final sequence stays fixed (Decision 6).

The generation is compared before the Wire Protocol versions, and an answer of exactly 65,536 bytes is read. An installation whose confirmation is not yet learned may adopt. Only adoption writes.

Tests:
- `a_newer_generation_is_adopted_and_acknowledged`
- `same_older_and_unusable_generations_change_nothing`
- `refresh_needs_an_enrollment_and_stops_while_draining`

### R7 — The staging and delivery contexts

Once a generation is adopted, `status().stagingContext` is `{ installationId, keyAlias, generation }`. The generation's limits and schemas come from the adopted advertisement:
- `maxRecordCount` and `maxMessageBytes` are its limits;
- `projectionSchemas` maps each record type to its versions.

That is the context `stage-outbox-batch@1` validates. `deliveryContext` is `{ installationId, keyAlias, portalUrl }`, which `send-outbox-message@1` accepts.

`confirmed` is derived from the Outbox, never stored. It is true once a generation is adopted, the high-water mark has reached the enrollment acknowledgement's sequence, and that row is gone, which only its receipt can cause. `status()` writes nothing.

Tests:
- `the_contexts_come_from_the_adopted_generation`
- `confirmation_is_learned_from_the_acknowledgement_receipt`
- `status_writes_nothing`

### R8 — Draining

`beginDrain()` moves an `enrolled` and confirmed installation to `draining`, and repeating it changes nothing. It is refused:
- as `not-enrolled` without an adopted generation;
- as `not-confirmed` before the Operator's confirmation is learned.

While draining, `finalSequence` is `null` until the installation's Outbox is empty. It is then the installation's high-water mark, which the Operator enters in the Portal. Staging stops while draining because the shell runs no Polling Run (Decision 7).

Tests:
- `draining_needs_a_confirmed_enrollment`
- `the_final_sequence_appears_once_the_outbox_is_empty`
- `draining_survives_a_restart`

### R9 — Unlinking and destructive reset

`unlink()` destroys the installation (R10) only while draining with a known final sequence, and is refused as `not-drained` otherwise, including without an enrollment. The Operator confirms on the Sender that the Portal retired the installation (Decision 8).

`reset({ acknowledgePossibleLoss: true })` destroys whatever the phase, including with queued messages. Without an enrollment, it deletes every row and no key. Afterwards `status()` is `null`, and `enroll` may start a new installation.

Tests:
- `unlink_needs_a_complete_drain`
- `reset_destroys_in_any_phase`
- `a_new_installation_can_enroll_after_destruction`

### R10 — Destruction

Destruction proceeds in this order:
1. It deletes the installation key. `keystore-unavailable` is `cipher-failed`, and nothing is destroyed. Any other result that is not success, including a throwing call, reads the same way. A missing or unusable key counts as deleted.
2. In one transaction, it deletes every row of every Sender table, as listed in `sqlite_master`, keeping the schema and its version. Foreign keys stay enforced and are checked at commit: destruction never sets the `foreign_keys` pragma, though it may read it.

A storage failure after the key is gone returns `storage-failed`. Repeating the request completes the destruction. The test adds two pairs of tables with foreign keys, standing in for a later unit's tables. One pair's parent sorts and is created before its child, and the other's after, so no fixed deletion order passes without checking foreign keys at commit.

Tests:
- `destruction_deletes_the_key_then_every_row`
- `an_unavailable_keystore_destroys_nothing`
- `a_failed_deletion_completes_on_retry`

### R11 — Storage and cipher failures

A storage failure in any read or write returns `storage-failed`, and leaves the database as it was before that write. A cipher call that throws reads as `keystore-unavailable`. A storage error of any type, including `TypeError`, is a storage failure, not a caller error.

Tests: `storage_failures_change_nothing`, `throwing_cipher_calls_read_as_unavailable`.

### R12 — Concurrency

Every storage operation waits its turn in the shared storage lane. The transport, the cipher's `isDeviceSecure` and key creation and deletion, and the clock never hold it. The actions of one instance run one at a time, so two concurrent `enroll` calls create one installation, and a refresh, a drain, or a reset waits behind an unanswered request.

Every action waits for a unit that took the lane before it started, and every storage step after a transport or cipher call waits for a unit that took the lane during that call. Each then completes.

Tests: `actions_run_one_at_a_time`, `the_network_call_does_not_hold_the_storage_lane`, `every_storage_operation_waits_for_the_shared_lane`.

### R13 — The Sender schema migration

`enrollmentMigration` is Sender schema version 8, registered after `send-outbox-message@1`'s version 7. It leaves every earlier schema object unchanged, and it references no other unit's table. It creates exactly this table, compared token by token with whitespace ignored, and nothing else. Its integer primary key needs no automatic index.

```sql
CREATE TABLE sender_enrollment (
  singleton INTEGER PRIMARY KEY NOT NULL CHECK (singleton = 1),
  installation_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  key_alias TEXT NOT NULL,
  portal_url TEXT NOT NULL,
  confirmation BLOB NOT NULL,
  phase TEXT NOT NULL CHECK (phase IN ('confirming', 'refused', 'enrolled', 'draining')),
  refusal TEXT CHECK (refusal IN ('enrollment-refused', 'capabilities-unsupported')),
  advertisement BLOB,
  generation INTEGER CHECK (generation > 0),
  acknowledgement_sequence INTEGER CHECK (acknowledgement_sequence > 0),
  enrolled_at_ms INTEGER NOT NULL,
  adopted_at_ms INTEGER,
  CHECK ((phase = 'refused') = (refusal IS NOT NULL)),
  CHECK ((phase IN ('enrolled', 'draining')) = (advertisement IS NOT NULL)),
  CHECK ((advertisement IS NULL) = (generation IS NULL)),
  CHECK ((advertisement IS NULL) = (acknowledgement_sequence IS NULL)),
  CHECK ((advertisement IS NULL) = (adopted_at_ms IS NULL))) STRICT
```

Tests: `migration_creates_version_eight`, `migration_is_idempotent`.

### R14 — Boundary

The module reaches the network only through the transport seam: it names no `fetch`, `XMLHttpRequest`, `WebSocket`, or Node global. Its value imports are:
- the protocol library's `decodeMessage`, `encodeMessage`, and `installationFingerprint`;
- `stage-outbox-batch@1`'s `appendOutboxMessages`, `lastOutboxSequence`, `outboxMessages`, and `outboxAad`;
- the storage seam and lane;
- its own helpers in its directory, which follow the same rules.

Its SQL names only its own table, apart from destruction, which reads the table list from `sqlite_master` and names no other table.

Tests: `the_module_stays_inside_its_boundary`.

## Decisions for the Operator

1. **Resolve `manage-sender-installation@1`'s pin on this row by rebinding it (DEV-sob-01, Bead `ae2`).** This unit appends its acknowledgement through `stage-outbox-batch@1`'s Outbox (that unit's Decision 2), so its row must name `stage-outbox-batch` as a dependency. The published `manage-sender-installation@1` suite pins this neighbouring row's dependencies to exactly `ingest-protocol`.
   - After Gate A, its assertion is changed to require only `ingest-protocol` among the dependencies, as `begin-reconciliation-snapshot@1`'s pin was on 2026-10-14. Then the pack is rebound with Vouch `rebind-pack` under the approver actor, and a fresh GREEN is recorded.
   - Only then does the row gain `stage-outbox-batch`. The draft row keeps its dependencies unchanged so that every published suite still passes.
   - The alternatives are a `manage-sender-installation@2` successor, or leaving the row on `ingest-protocol` and moving the acknowledgement to the shell, outside governance.
2. **The Sender's local lifecycle.** The phases are `confirming`, `refused`, `enrolled`, and `draining`, followed by destruction. A refused enrollment stays refused until the Operator resets it and enrolls again with a new code. Ended identities never return to the Portal (`manage-sender-installation@1` Decision 4).
3. **The answer body decides, as `manage-sender-installation@1` Decision 1 says.** Only an advertisement or a retryable failure keeps an enrollment alive. Anything else refuses it, because the Portal answers a refusal with no protocol bytes. The HTTP status is not read, as in `send-outbox-message@1` Decision 1. One consequence is that a proxy error page refuses an enrollment, and the Operator issues a new code. The alternative is to retry non-protocol answers until the code expires.
4. **The acknowledgement is the first Outbox Message, and confirmation is learned from its receipt.** Adoption stages the acknowledgement atomically. It is sequence 1 because nothing can be staged before a generation exists, and the Portal keeps a pending installation's deliveries retryable. Its removal is therefore the first proof that the Operator confirmed the installation, with no new protocol message. The alternative is a Wire Protocol Version 2 status query, which was declined at `manage-sender-installation@1` Gate A.
5. **Collection waits for confirmation.** The shell enables collection only while `confirmed` and not draining. A withdrawn or never-confirmed enrollment then leaves only its acknowledgement queued, rather than a whole Initial Snapshot. The alternative is to start the Initial Snapshot while pending, which saves one delivery round after the Operator confirms.
6. **Later generations are adopted only before draining, and only when newer.** The shell decides when to refresh, for example at app start and foreground return. An advertisement without Wire Protocol Version 1 is not adopted, and the Sender keeps its old generation, which the Portal keeps honoring until it is acknowledged past (ADR 0031).
7. **Draining stops all Polling Runs, so the final sequence stays fixed.** Disabling collection alone would still stage a Sync Checkpoint for every run, and the high-water mark would keep moving. While draining, the shell runs no Polling Run and adopts no generation. The final sequence is the high-water mark once the Outbox is empty, and the Operator enters it in the Portal.
8. **Unlinking trusts the Operator's word that the Portal retired the installation, but requires an empty Outbox.** Wire Protocol Version 1 has no retirement message. With nothing left to send, the only cost of an early unlink is a Portal installation that the Operator must still retire. Destructive reset is the only path with queued messages, and it requires an explicit acknowledgement of possible loss.
9. **Destruction deletes the key first, then every row of every Sender table.** Deleting the key makes any queued ciphertext unreadable at once. Deleting rows, rather than the database file, keeps the migrated schema, and a repeated request completes a partly failed destruction. Rows are read from `sqlite_master`, so tables added by later units are covered. The alternative is to delete the database file in the shell, outside governance.
10. **Enrollment requires a secure screen lock, and the offer's expiry is the Portal's to enforce.** ADR 0014 requires the lock. The Sender's clock may be wrong, and the Portal refuses an expired code with `enrollment-code-expired` anyway.
11. **The row's other text changes now.** Its inputs and outputs are reworded to name the phases, the first acknowledgement, the learned confirmation, the contexts, and destruction. The phrases `manage-sender-installation@1` pins, the kind `integration` and the purpose's "drain, unlink, or destructively reset", are kept.

## Ground rules

- Fixtures are synthetic.
- Outbox rows are appended through `stage-outbox-batch@1`'s primitives.
- The fake Portal builds its answers with the protocol library.
- A failing database is injected through the 3ug seam.

## Freeze prerequisites

After Gate A, the executable pack adds:
- the TypeScript suite;
- the Python contract test;
- the runner;
- the Interface stub `manage-sender-enrollment.ts`, whose functions throw, and whose types and constants are declared.

It also adds the approved resolution of Decision 1. GPT 6 Astra approved rebinding `manage-sender-installation@1`, whose suite now requires `ingest-protocol` among this row's dependencies. A fresh GREEN was recorded against its unchanged implementation, and the row then gained `stage-outbox-batch`.

**Implementability review.** GPT 6.1 Sol's read-only review found two blocking gaps, both now resolved:
- R5 promised that the enrollment acknowledgement is the first message, while a test requires the next sequence for an identity that already has rows. R5 now says the sequence always comes from the high-water mark, and is 1 for a new identity.
- R3 did not say what happens when deleting the key after a failed recording also fails. R3 now keeps `storage-failed` and leaves the unused key, a test covers both failures together, and the contract's key-lifecycle effect names this deletion.

Its non-blocking findings are added too:
- the adoption failure cases repeat through `refreshCapabilities()`, keeping the adopted generation and the recorded sequence;
- the write-failure loops allow up to 63 writes, rather than nine;
- destruction is checked never to turn foreign keys off, and they hold afterwards.

Against the stub, all 39 TypeScript tests fail and the two contract tests pass. A throwaway reference passes every test, and the suite kills all 123 of its mutants. GPT 6.1 Sol's read-only implementability review runs before Gate B. Every dependency is published, so Vouch records `specify` after Gate A. The implementation registers `enrollmentMigration` in `senderMigrations` and updates the enabling storage test, as each earlier Sender migration did. It also adds this unit's runner to the CI `sender` job.

The binding is:
- spec files: the contract and this pack;
- test files:
  - `manage-sender-enrollment-1.test.ts`;
  - its 3ug substitutes, `sender/app/test/support/node-database.ts` and `sender/app/test/support/node-installation-cipher.ts`;
  - `test_manage_sender_enrollment_1.py`;
  - the runner.

## Rebind after publication

On the Operator's decision of 2026-10-30, this pack was rebound after publication. Its boundary test, `the_module_stays_inside_its_boundary`, allowed the module to import only four functions from `stage-outbox-batch`: `appendOutboxMessages`, `lastOutboxSequence`, `outboxMessages`, and `outboxAad`. Its successor `manage-sender-enrollment@2` answers status, drain, and unlink through `stage-outbox-batch@2`'s backlog reads, `outboxBacklog` and `outboxHolds`, so that no read loads a sealed Outbox message (Beads `mfv`, `636`). The `@2` pack re-runs this suite unchanged.

The rebind changes only that allowlist, which gains those two names. The test still forbids the module to name any other unit's table, and still limits its imports to the Outbox's own functions. Every requirement, decision, test name, and assertion about this unit's behavior is unchanged.
