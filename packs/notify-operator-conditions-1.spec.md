# Draft acceptance pack — `notify-operator-conditions@1`

**Status:** Draft approved by the Operator on 2026-10-16, including the nine decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `notify-operator-conditions` Governed Scope contract in `governance/scopes/notify-operator-conditions.1.json`.

ADR 0013 lists the Operator-action-required Sender states that raise a local notification. Two published units already notify three of them:
- a Delivery Block, by `send-outbox-message@1`;
- restricted and unregistered background scheduling, by `schedule-background-polling@1`.

The others had no owner. A background Polling Run could reach them and stop collecting indefinitely without telling anyone. The Codex review of PR #39 found this, and the Operator chose a governed unit (Beads `pv2.7`). This unit keeps those conditions durable and notifies each onset once.

## Interface

**TypeScript**, module `sender/app/src/notifications/notify-operator-conditions.ts`, in its own directory.
- `OperatorCondition` is one of:
  - `permission-missing`;
  - `screen-lock-removed`;
  - `storage-pressure`;
  - `record-too-large`;
  - `projection-refused`;
  - `installation-key-unusable`;
  - `enrollment-refused`;
  - `drain-complete`.
- `ConditionNotifier`: `notify(condition): Promise<void>`. It raises one health-data-free local notification.
- `ConditionObservation`: `{ summary?, enrollment, keyStatus? }`, where:
  - `summary` is a `RunSummary` from `stage-sync-checkpoint@1`, present when the observation follows a Polling Run;
  - `enrollment` is `manage-sender-enrollment@1`'s `EnrollmentStatus`, or `null` without an enrollment;
  - `keyStatus` is the cipher's `InstallationKeyStatus` for the enrollment's key alias, when it was read.
- `createOperatorConditions({ database, notifier, now })` returns:
  - `observe(observation)`, which returns `{ outcome: "observed", conditions }` with the active conditions in condition order, or `{ outcome: "storage-failed" }`;
  - `conditions()`, which returns the active conditions, each as `{ condition, firstSeenMs, lastSeenMs, notified }` in condition order, or `{ outcome: "storage-failed" }`.
- `OPERATOR_CONDITIONS`: the eight conditions in condition order, the order listed above.
- `operatorConditionsMigration`: Sender schema version 9.

## Requirements

The 16 TypeScript `node:test` tests in `notify-operator-conditions-1.test.ts` use the 3ug `node:sqlite` substitute and a synthetic notifier. Their observations are built with the published types. Tests migrate an explicit list to version 9. Assertions that something wrote nothing compare the whole database. `tests/acceptance/test_notify_operator_conditions_1.py` checks the contract. The runner verifies a fresh JUnit report under an external deadline. Fixtures are synthetic.

### R1 — Governed Scope contract

The contract names the scope, version, kind, purpose, inputs with their modes, outputs, one named failure, two effects, authority, the three dependencies, and the acceptance oracle. It does not contradict the scope-map row.

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — Caller errors

These throw `TypeError` before any storage or notifier call:
- malformed options;
- an observation that is not an object;
- a `summary` whose `recordTypes` is not an array of objects with a string `outcome` and, when present, a string `reason`;
- an `enrollment` that is neither `null` nor an object with a string `phase`;
- a `keyStatus` other than `present`, `missing`, `unusable`, or `unavailable`;
- a clock reading that is not a safe integer.

Validation checks these types only, not the published vocabulary: an unknown outcome or reason makes no condition (R3). `observe` checks the observation's shape first, then reads the clock once, before any storage or notifier call.

Tests: `malformed_options_and_observations_are_caller_errors`.

### R3 — Conditions from a Polling Run

When the observation carries a `summary`, these conditions are active exactly when some record type in it shows them:
- `permission-missing`: the reason `permission-missing`, or the outcome `not-permitted`;
- `screen-lock-removed`: the reason `screen-lock-removed`;
- `storage-pressure`: the reason `storage-pressure`;
- `record-too-large`: the reason `record-too-large`;
- `projection-refused`: the reason `projection-refusal`.

Without a `summary`, these five keep their state. Unknown outcomes and reasons make no condition. Reasons that are transient or already notified elsewhere make no condition: `time-budget-reached`, `changes-token-expired`, `read-failed`, `delivery-blocked`, and `collection-disabled`.

Tests: `run_conditions_follow_the_latest_summary`, `transient_and_elsewhere_notified_reasons_make_no_condition`.

### R4 — Conditions from enrollment and the key

Every observation updates these:
- `enrollment-refused`: the phase is `refused`;
- `drain-complete`: the phase is `draining` and the final sequence is known, so the Operator must retire the installation in the Portal and unlink it;
- `installation-key-unusable`: an enrollment exists that is not `refused`, and `keyStatus` is `missing` or `unusable`.

Without an enrollment, or with a refused one, `installation-key-unusable` is inactive whatever the key status. Otherwise a `keyStatus` of `unavailable`, or none, keeps its state, because the Keystore may be briefly unavailable. Without an enrollment, all three are inactive.

Tests: `lifecycle_and_key_conditions_follow_each_observation`.

### R5 — One notification per onset

A condition's onset is its becoming active while it was inactive. At each onset the unit:
1. records the condition with its first and last seen times;
2. calls the notifier with the condition alone;
3. records the notification's success.

While the condition stays active, later observations do not notify again. Its last seen time moves only when an observation shows it, not when a condition is merely kept, and it never moves backwards. Onsets are notified in condition order, and a notifier failure for one condition does not stop the others. `observe` still returns `observed` after a notifier failure. If the notifier throws, synchronously or not, every later observation retries it while the condition stays active, until it succeeds. That includes an observation that only keeps the condition, such as one without a summary, and the retry does not advance its last seen time. A condition that clears before its notification succeeds is never notified. A condition that clears and becomes active again is a new onset and notifies again. An onset is identified by its condition and first seen time. Two onsets of one condition at the same millisecond, from different instances, cannot be told apart, so a late success for the first may count for the second.

Tests: `each_onset_notifies_once`, `a_failed_notification_is_retried`, `a_cleared_condition_notifies_again_on_recurrence`.

### R6 — Persistence

Conditions, their seen times, and their notification state survive a restart. A new instance on the same database reads them, does not notify an already-notified condition again, and retries an unnotified one at its next observation.

Tests: `conditions_survive_a_restart`.

### R7 — Reading conditions

`conditions()` returns the active conditions exactly, and issues no write, not even a write that changes nothing.

Tests: `conditions_are_read_exactly`.

### R8 — Storage failures

Each observation commits all of its condition changes, the onsets, clearings, and last seen times, in one transaction before any notification. A storage failure there returns `storage-failed`, leaves the database as it was before the observation, and notifies nothing. Recording a notification's success happens afterwards, in the lane. If that recording fails, the committed conditions stay, and `observe` returns `storage-failed`. A notification whose success cannot be recorded is raised again at the next observation, so delivery is at least once. A storage error of any type, including `TypeError`, is a storage failure.

Tests: `storage_failures_change_nothing`, `an_unrecorded_success_notifies_again`.

### R9 — Concurrency

Every storage operation waits in the shared storage lane, and the notifier never holds it. Observations of one instance run one at a time, so concurrent observations notify an onset once.

Tests: `observations_run_one_at_a_time`, `the_notifier_does_not_hold_the_storage_lane`.

### R10 — The Sender schema migration

`operatorConditionsMigration` is Sender schema version 9, registered after `manage-sender-enrollment@1`'s version 8. It leaves every earlier schema object unchanged, references no other unit's table, and creates exactly this table, with its automatic primary-key index, and nothing else:

```sql
CREATE TABLE operator_conditions (
  condition TEXT PRIMARY KEY NOT NULL CHECK (condition IN ('permission-missing', 'screen-lock-removed', 'storage-pressure',
    'record-too-large', 'projection-refused', 'installation-key-unusable', 'enrollment-refused', 'drain-complete')),
  first_seen_ms INTEGER NOT NULL,
  last_seen_ms INTEGER NOT NULL,
  notified INTEGER NOT NULL CHECK (notified IN (0, 1)),
  CHECK (last_seen_ms >= first_seen_ms)) STRICT
```

A row exists exactly while its condition is active.

Tests: `migration_creates_version_nine`, `migration_is_idempotent`.

### R11 — Boundary

The module:
- imports only types from other Sender units, plus the storage seam and lane as values;
- names no Node or network global;
- names only its own table in SQL;
- never passes anything but a condition name to the notifier: no record type, count, value, or identity.

Tests: `the_module_stays_inside_its_boundary`.

## Decisions for the Operator

1. **A governed Sender leaf with its own durable state.** One notification per onset needs state that survives restarts. Only governed units append Sender migrations, and no unit should read another unit's tables. The Operator chose this unit at the PR #39 review. The alternative was to notify on every run that shows a condition, which repeats hourly notifications.
2. **Which conditions.** The eight above. Delivery Blocks and scheduling conditions stay with the units that already notify them, so nothing is notified twice. The transient reasons make no condition:
   - `read-failed`, a failed read that retries;
   - `time-budget-reached`;
   - `changes-token-expired`, which starts a reconciliation by itself;
   - a briefly unavailable Keystore.
   
   `collection-disabled` makes no condition either, because it only arises while the Operator has not confirmed enrollment, or while draining.
3. **One notification per condition, not per record type.** A missing permission on many types is one onset, not dozens. The notification names the condition alone, with no record types, counts, or values, which keeps it health-data-free (ADR 0013). Sender screens may show which types are affected. The alternative is one notification per type and condition.
4. **Run conditions change only with a run.** The five run-derived conditions update only when the observation carries a Polling Run summary. A run that no longer shows one clears it. An observation without a run, such as at app start, cannot tell whether a permission was granted, so it keeps the last state.
5. **Lifecycle conditions are action required too.** A refused enrollment needs a reset and a new code. A completed drain needs the Operator to enter its final sequence in the Portal and then unlink. These are ADR 0013's "incomplete lifecycle transition".
6. **An unusable or missing key is a condition; an unavailable Keystore is not.** ADR 0014 freezes an installation whose key is unusable until the Operator retires it. A Keystore that is momentarily unavailable is retried, so it neither raises nor clears the condition.
7. **At least once, at most once per onset.** This follows `schedule-background-polling@1` and `send-outbox-message@1`: the notification's success is recorded, a failed one is retried at the next observation that still shows the condition, and one whose success was not recorded may be raised twice.
8. **The shell decides when to observe.** It observes after every Polling Run, foreground, manual, or background, and at app start and foreground return. Wiring that is the Sender shell's enabling work, Beads `2rv.9`, alongside the delivery loop.
9. **The row is new.** The scope-map gains a `notify-operator-conditions` row, and `docs/governance/delivery-flow.md` lists it in Phase 2. No published suite pins either.

## Ground rules

- Fixtures are synthetic.
- Observations are built with the published types.
- The notifier is a synthetic fake.
- A failing database is injected through the 3ug seam.

## Freeze prerequisites

After Gate A, the executable pack adds:
- the TypeScript suite;
- the Python contract test;
- the runner;
- the Interface stub `notify-operator-conditions.ts`, whose functions throw and whose types, constants, and migration are declared.

**Implementability review.** GPT 6.1 Sol's read-only review found five blocking gaps, all now resolved:
- R5 now says every active, unnotified condition is retried at each observation, including one only kept;
- R4 now says a refused enrollment clears the key condition whatever the key status;
- R2 now checks a present `reason` is a string, and says validation checks types, not vocabulary;
- R8 now states the observation's atomic boundary;
- a storage-failure test no longer assumes how many statements an implementation writes.

Its non-blocking findings are added too:
- R5 states that an onset's identity is its condition and first seen time, with the same-millisecond limit;
- a test holds the lane while a notification's success is being recorded;
- `conditions()` is checked to issue no write.

Against the stub, all 16 TypeScript tests fail and the two contract tests pass. A throwaway reference passes every test, and the suite kills all 56 of its non-equivalent mutants. Two more were dropped as equivalent: removing the observation's object check or the summary's shape check still throws `TypeError` from destructuring or iteration. GPT 6.1 Sol's read-only implementability review runs before Gate B. Every dependency is published, so Vouch records `specify` after Gate A. The implementation registers `operatorConditionsMigration` in `senderMigrations` and updates the enabling storage test, as each earlier Sender migration did.

The binding is:
- spec files: the contract and this pack;
- test files: `notify-operator-conditions-1.test.ts`, its 3ug substitute `sender/app/test/support/node-database.ts`, `test_notify_operator_conditions_1.py`, and the runner.
