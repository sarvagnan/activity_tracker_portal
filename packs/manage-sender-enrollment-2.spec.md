# Draft acceptance pack — `manage-sender-enrollment@2`

**Status:** Draft approved by the Operator on 2026-10-30, including the four decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `manage-sender-enrollment` Governed Scope contract, version 2, in `governance/scopes/manage-sender-enrollment.2.json`. It is the additive successor to the published `manage-sender-enrollment@1`, which it supersedes.

## Why a successor

`@1` derives two values from the Outbox:
- `confirmed`, which needs the enrollment acknowledgement's row to be gone (R7);
- `finalSequence`, which needs the installation's Outbox to be empty (R8).

It gets both by calling `stage-outbox-batch@1`'s `outboxMessages`, which returns every sealed message of every installation (Beads `mfv`). `status()` runs on every Sync, delivery pass, and screen refresh, and `beginDrain()` and `unlink()` run it inside their own work.

In the emulator smoke run on 2026-10-30 (Beads `hfj`, `636`), about 1,000 queued messages were enough to crash the Sender. Tapping **Begin draining** twice ended in `JNI ERROR (app bug): global reference table overflow`, with 50,367 live byte buffers from these reads. Draining could never start, so the installation could only be reset destructively.

`stage-outbox-batch@2` adds reads that answer the same questions without loading sealed bytes. This successor uses them, and pins that it does.

## Interface

Everything in `@1`'s Interface is unchanged: `createSenderEnrollment`, its options and results, `EnrollmentStatus`, every constant, and `enrollmentMigration`. There is no new migration and no new export.

## Requirements

`@1`'s requirements R1–R14 carry over unchanged. The `@2` pack re-runs `@1`'s bound runner unchanged, so every `@1` test must still pass exactly.

The new requirements are bound to a new TypeScript suite, `sender/app/test/manage-sender-enrollment-2.test.ts`. It uses `@1`'s fakes and 3ug substitutes, and appends Outbox rows through `stage-outbox-batch@2`'s primitives. A new Python suite, `tests/acceptance/test_manage_sender_enrollment_2.py`, checks the contract.

### R15 — Governed Scope contract, version 2

The `@2` contract keeps `@1`'s inputs with their modes, and its outputs, named failures, outcome classes, and effects. Its dependencies are `ingest-protocol@1` and `stage-outbox-batch@2`. It adds the authority to answer from the backlog reads. The scope-map row says the Outbox state is read without loading sealed messages, and still satisfies every published test that pins it.

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R16 — No sealed message is loaded

`status()`, `beginDrain()`, and `unlink()` load no sealed Outbox message, in every phase:
- `confirming`;
- `refused`;
- `enrolled`, before and after confirmation;
- `draining`, with and without queued messages.

That holds for an installation that has queued messages, and alongside another installation's rows. An observing database wrapper records every row any query returns. No recorded row may have a `sealed` key. Also, the number of byte-array values the operation receives must be the same with an empty Outbox as with 500 queued messages. The enrollment row's own stored bytes are allowed.

Tests:
- `status_loads_no_sealed_message`
- `begin_drain_loads_no_sealed_message`
- `unlink_loads_no_sealed_message`
- `reads_do_not_grow_with_the_outbox`

### R17 — The same answers

For every state the tests build, `status()` returns exactly what `@1`'s derivation gives, which the tests compute from `outboxMessages` as the oracle. That covers `confirmed`, `finalSequence`, `phase`, and both contexts.

The tests build those states with a scripted mix of steps:
- staging;
- removing the enrollment acknowledgement and other rows, at the head and in the middle;
- another installation's rows;
- draining, until the Outbox is empty.

`beginDrain()` and `unlink()` reach the same outcomes and refusals as in `@1` for each of them.

Tests: `status_matches_the_outbox_oracle`, `drain_and_unlink_match_the_outbox_oracle`.

### R18 — A large Outbox

With 5,000 queued messages of the enrolled installation, all of these return their correct `@1` answers, with no `storage-failed`:
- `status()`;
- `beginDrain()` on a confirmed installation;
- `status()` again while draining;
- after removing every message, `status()` with its final sequence and `unlink()`.

No timing is asserted.

Tests: `a_large_outbox_drains_and_unlinks`.

## Decisions for the Operator

1. **Same answers, cheaper reads.** Nothing a caller sees changes. `@2` exists to pin that no sealed message is loaded (R16), which `@1` never promised, and to depend on the reads that make that possible. *Alternative:* changing `@1`'s implementation without a new version. That leaves the guarantee unpinned, and `mfv` asked for the governed lifecycle.
2. **The guarantee is tested by what the database returns, not by timing.** The emulator crash came from live byte buffers, so the tests count what the reads receive. No test measures speed.
3. **Supersession.** Vouch supersedes `@1` when `@2` is specified. Its published dependents keep their packs, because `@2`'s interface is unchanged: `notify-operator-conditions@1` and `receipt-bound-delivery@1`. If Vouch requires it at publication, their packs are rebound with `rebind-pack`, with no change in behaviour. Implementation waits until `stage-outbox-batch@2` publishes.
5. **`@1`'s pack is rebound (Operator decision, 2026-10-30, during the executable pack).** `@1`'s boundary test allowed only four imports from `stage-outbox-batch`, so `@2`'s use of `outboxBacklog` and `outboxHolds` could not pass the `@1` suite this pack re-runs. The allowlist gains those two names, and `@1`'s pack records the rebind. The approver records `rebind-pack` for `manage-sender-enrollment@1` before `@2` is specified, and then a fresh GREEN through `@1`'s bound runner against its unchanged published implementation. The test still forbids other units' tables and limits imports to the Outbox's own functions.
4. **The shell's status reads stay as they are.** `mfv` also notes the shell calls `status()` 20–26 times a trigger. Once each read is cheap, that count stops mattering for memory. Reducing it is a shell change outside this unit, and is left for the shell task (Beads `2rv.18`) if review asks for it.

## Ground rules

`@1`'s ground rules apply. In addition:
- **Ciphertext checks** use an observing `SenderDatabase` wrapper that records every row any query returns, as `stage-outbox-batch@2`'s pack does.
- **Writes-nothing assertions** compare the whole database.
- **Migrations.** Fixtures migrate with an explicit list of migrations, never the production `senderMigrations`.

## Freeze prerequisites

The Operator approved the draft. The executable pack adds:
- the TypeScript suite `manage-sender-enrollment-2.test.ts`, 7 tests;
- the Python contract suite `tests/acceptance/test_manage_sender_enrollment_2.py`, 2 tests;
- the runner `tests/acceptance/run_manage_sender_enrollment_2.sh`. It runs the version 2 contract check, then `@1`'s bound runner unchanged, then the `@2` suite under a 60-second per-test timeout. It passes the suite only when a fresh JUnit report lists exactly its tests, none failed, errored, or skipped.

The suite migrates with `@1`'s explicit list to version 8. It drives the published `@1` enrollment through a synthetic Portal, delivers through `send-outbox-message@1`, and uses `@1`'s `outboxMessages` as the oracle.

There is no new Interface stub. The published `@1` module is the RED baseline, against which:
- R16's 4 tests fail: `@1` loads 26 sealed rows for one status read, and 1,500 across status, drain, and unlink with 500 queued;
- R17's 2 tests and R18's test pass by design, because they pin that `@2` keeps `@1`'s answers;
- `@1`'s 39 tests pass with the rebound allowlist;
- both Python suites pass.

**The reference run.** A throwaway reference answers `confirmed` and `finalSequence` through `stage-outbox-batch@2`'s `outboxHolds` and `outboxBacklog`, taken from that unit's throwaway reference. It passes all 7 new tests and `@1`'s 39.

The first reference run found the allowlist conflict that Decision 5 resolves. Four mutants each fail at least one test:
- confirmation ignoring whether the acknowledgement is queued;
- the final sequence ignoring the backlog;
- asking about the wrong sequence;
- loading the sealed rows as well.

The binding is:
- **spec files:** this contract and pack, and `@1`'s contract and pack;
- **test files:**
  - `manage-sender-enrollment-2.test.ts`, `test_manage_sender_enrollment_2.py`, and the `@2` runner;
  - every file `manage-sender-enrollment-pack/1` binds, as rebound: `manage-sender-enrollment-1.test.ts`, its 3ug substitutes `sender/app/test/support/node-database.ts` and `sender/app/test/support/node-installation-cipher.ts`, `test_manage_sender_enrollment_1.py`, and `run_manage_sender_enrollment_1.sh`.

Implementation waits until `stage-outbox-batch@2` publishes, and this branch then merges `main`. GPT 6.1 Sol's read-only implementability review runs before Gate B. The implementation adds the runner to the project CI `test` job.
