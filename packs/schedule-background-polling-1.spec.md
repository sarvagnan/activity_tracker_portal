# Draft acceptance pack — `schedule-background-polling@1`

**Status:** Draft approved by the Operator on 2026-10-15, including the ten decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `schedule-background-polling` Governed Scope contract in `governance/scopes/schedule-background-polling.1.json`.

Expo BackgroundTask supplies the Sender's deferrable background scheduling (ADR 0018). This unit adapts it to `coordinate-sender-sync@1`, the one synchronization interface that foreground and manual triggers also call. It does three things:
1. It keeps one hourly background task registered.
2. It turns each background callback into a background trigger with an eight-minute budget.
3. It observes the Background Scheduling Status that each Polling Run reports in its Sync Checkpoint.

When scheduling is restricted or the registration is missing, it keeps a persistent Operator-action-required condition and raises one local notification (ADR 0013, 0049). The status is an observation for the next checkpoint, not a guarantee about the Device's future behavior.

It requests no battery-optimization exemption or foreground service, and it touches no cursor, Outbox Message, or health state. Foreground and manual sync stay available whatever this unit observes. The unit is TypeScript only.

## Expo and Android facts

These come from the pinned `expo-background-task@57.0.21` and `expo-task-manager@57.0.21`, the versions Expo 57 bundles. They shape Decisions 2, 3, and 9, and physical-device qualification confirms them.

1. **Registration.** `BackgroundTask.registerTaskAsync(name, { minimumInterval })` registers a task, defined first with `TaskManager.defineTask`, in persistent storage, and `TaskManager.isTaskRegisteredAsync(name)` reports it. The minimum interval is in minutes: at least 15, defaulting to 12 hours. The operating system treats it as a minimum delay.
2. **Android scheduling.** On Android, a registration enqueues one WorkManager request with the interval as its initial delay, replacing any pending request. Each run enqueues the next. Every request requires a connected network. While the app is in the foreground, a due run is skipped and rescheduled.
3. **Android status.** On Android, `BackgroundTask.getStatusAsync()` always returns `Available`, because WorkManager always exists. It cannot see Android's background restriction, which the user sets in the app's battery settings, or the restricted standby bucket. Both stop deferrable work.
4. **Execution window.** WorkManager allows each run about ten minutes.

## Interface

**TypeScript**, module `sender/app/src/scheduling/schedule-background-polling.ts`. It has its own directory, so neither this unit's boundary check nor the coordinator's reads the other's files. It uses:
- `coordinate-sender-sync@1`'s `SenderSync`;
- `stage-sync-checkpoint@1`'s `BackgroundScheduling`;
- the 3ug storage seam;
- the storage lane that `coordinate-sender-sync@1` serializes Sender storage through, `storageLane` in `sync/sync-storage.ts` (R9).

The seams:
- `SchedulingPlatform`:
  - `isRegistered(taskName): Promise<boolean>`;
  - `register(taskName, minimumIntervalMinutes): Promise<void>`;
  - `restriction(): Promise<"available" | "restricted">`, which combines Expo's status with Android's background restriction and restricted standby bucket (Decision 2).
- `SchedulingNotifier`: `notify(condition: "restricted" | "unregistered"): Promise<void>`.

The constants:
- `POLLING_TASK_NAME = "activity-tracker-portal.polling"`;
- `MINIMUM_INTERVAL_MINUTES = 60`;
- `BACKGROUND_BUDGET_MS = 480_000`.

`createBackgroundPolling({ database, platform, notifier, sync, now })` returns:
- `ensureRegistered()`, which the shell calls when the app starts and when it returns to the foreground. It registers the task if it is missing, then observes. It returns `{ outcome: "observed", status, condition }`, where `condition` is the condition this observation set, or `{ outcome: "storage-failed" }`.
- `observe()`, which is the `backgroundScheduling` option the shell passes to `coordinate-sender-sync@1`. It observes without registering and returns the status.
- `runTask()`, the body of the `TaskManager` task executor. It returns `"success"` or `"failed"`.
- `condition()`, which returns the persistent action-required condition, `"restricted"`, `"unregistered"`, or `null`, or `{ outcome: "storage-failed" }`. Before any observation it is `null`.

`schedulingMigration` is Sender schema version 6.

## Requirements

TypeScript `node:test` suites in `schedule-background-polling-1.test.ts` use:
- the 3ug `node:sqlite` substitute;
- synthetic platform, notifier, and sync fakes;
- the published `coordinate-sender-sync@1`, with its synthetic source fake, for the checkpoint integration test.

Tests migrate an explicit list to version 6. `tests/acceptance/test_schedule_background_polling_1.py` checks the contract. The runner verifies a fresh JUnit report and sets a per-test timeout.

### R1 — Governed Scope contract

The contract names:
- the scope, version, kind, and purpose;
- inputs, with their modes;
- outputs;
- 4 named failures in the outcome classes `action-required`, `run-failure`, and `storage-failure`;
- three effects;
- authority;
- the `coordinate-sender-sync@1` dependency;
- the acceptance oracle.

It does not contradict the scope-map row.

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — Caller errors

Malformed options throw `TypeError` before any platform, notifier, sync, or storage call. So does a wall-clock reading that is not a safe integer: each `ensureRegistered()` and `observe()` reads `now()` once, before any platform call, and rejects with `TypeError` without calling the platform, notifier, or storage.

Tests: `malformed_options_are_caller_errors`.

### R3 — Registration

`ensureRegistered()` behaves as follows:
- **A missing task** is registered once, as `POLLING_TASK_NAME` with a 60-minute minimum interval.
- **A registered task** is never registered again, because registering replaces the pending WorkManager request and restarts its delay (Decision 3).
- **No call** ever unregisters the task.
- **A registration that throws** leaves the task missing, and the observation reads it as unregistered. So does one that resolves without registering.
- **An unreadable registration** is not registered over, because registering would replace a pending request. Only a registration read of `false` leads to registering, and the observation then reads the unreadable registration as unregistered (R8).

For a missing task, the platform sees exactly a registration read, the registration, then the observation's registration read and restriction read.

Tests:
- `a_missing_task_is_registered_hourly`
- `a_registered_task_is_never_registered_again`
- `the_task_is_never_unregistered`
- `a_failed_registration_reads_as_unregistered`

### R4 — The observation

An observation reads the registration first and then the platform's restriction. The resulting status is:
- `unregistered` when the task is not registered;
- otherwise `restricted` when the platform reports restricted;
- otherwise `available`.

Each observation is recorded with the `now()` reading taken when it began, even one earlier than the last observation's. `observe()` never registers. An `available` observation makes exactly one registration read and then one restriction read.

Tests:
- `a_registered_unrestricted_task_is_available`
- `a_restricted_platform_is_restricted`
- `unregistered_wins_over_restricted`
- `observing_never_registers`
- `observations_are_recorded_with_their_time`

### R5 — The status each checkpoint reports

Given as `coordinate-sender-sync@1`'s `backgroundScheduling` option, `observe()` supplies the status that the run's Sync Checkpoint reports. That includes a run recovered as interrupted, which keeps the status observed when it opened.

Tests: `the_checkpoint_reports_the_observation_at_run_open`.

### R6 — The action-required condition

The status sets the persistent condition:
- a `restricted` or `unregistered` observation sets the condition to that status;
- an `available` observation clears it.

The notifier is called once at each onset, which is a change from no condition, or from the other condition. A change of condition is an onset even when the previous onset's notification failed. The notification behaves as follows:
- it is called with the condition alone, after the observation is recorded, and its success is then recorded;
- one that throws, synchronously or not, is retried at the next observation until it succeeds;
- if its success cannot be recorded, the next observation notifies again, so delivery is at least once.

The condition and its notification state survive a restart. Observations run one at a time in each instance, so concurrent observations notify an onset once.

Tests:
- `an_action_required_condition_notifies_once`
- `recovery_clears_the_condition`
- `a_new_condition_notifies_again`
- `a_failed_notification_is_retried`
- `the_condition_survives_a_restart`
- `concurrent_observations_notify_once`

### R7 — Background runs

`runTask()` calls `sync.trigger({ kind: "background", budgetMs: BACKGROUND_BUDGET_MS })` once. It returns:
- `"success"` when the run checkpointed;
- `"failed"` when the run failed, the trigger rejected, or the trigger threw.

It never throws to the task executor. It needs no storage of its own, so it reports success for a checkpointed run even when this unit's storage fails.

Tests: `the_background_task_triggers_a_budgeted_background_run`, `a_failed_or_throwing_run_reports_failed`.

### R8 — Platform failures

A platform call that throws or returns a malformed value reads as the worst plausible state:
- for registration, not registered;
- for restriction, restricted.

The observation still completes.

Tests: `platform_errors_read_as_the_worst_status`.

### R9 — Storage

If recording an observation fails:
- `observe()` still returns the observed status, because a checkpoint must report what was observed;
- `ensureRegistered()` and `condition()` return `storage-failed`;
- the notifier is not called, because the condition's onset cannot be recorded.

A storage error of any type, including `TypeError`, is a storage failure, not a caller error.

Every storage operation waits its turn in the storage lane that `coordinate-sender-sync@1` serializes Sender storage through, because the 3ug seam allows one operation at a time. An observation therefore never collides with a Polling Run's storage. A pending notification never holds the lane.

Tests: `storage_failures_keep_the_observation`, `observations_wait_for_the_storage_lane`.

### R10 — The Sender schema migration

`schedulingMigration` is Sender schema version 6, registered after `coordinate-sender-sync@1`'s version 5. It leaves every earlier schema object unchanged. It creates exactly this table, compared token by token with whitespace ignored, and nothing else; its integer primary key needs no automatic index. It references no other unit's table.

```sql
CREATE TABLE scheduling_state (
  singleton INTEGER PRIMARY KEY NOT NULL CHECK (singleton = 1),
  observed_status TEXT NOT NULL CHECK (observed_status IN ('available', 'restricted', 'unregistered')),
  observed_at_ms INTEGER NOT NULL,
  action_condition TEXT CHECK (action_condition IN ('restricted', 'unregistered')),
  notified_condition TEXT CHECK (notified_condition IN ('restricted', 'unregistered')),
  CHECK (action_condition IS (CASE WHEN observed_status = 'available' THEN NULL ELSE observed_status END)),
  CHECK (notified_condition IS NULL OR notified_condition = action_condition)) STRICT
```

The one row holds the last observation and its time, the condition it set, and that condition once its notification succeeded. Notification state is reset at each onset.

Tests: `migration_creates_version_six`, `migration_is_idempotent`.

### R11 — Boundary

Static checks of every file in the module's directory confirm:
- it names no other unit's table;
- it imports only relative modules;
- it uses no Node built-in;
- it imports values only from beside itself, from the storage seam, or `storageLane` from `sync/sync-storage.ts`, and only types from `coordinate-sender-sync`;
- it names no other `coordinate-sender-sync` function.

At run time it reaches the coordinator only through `trigger`, and the platform only through the three seam methods.

Neither the module nor `sender/app/app.config.ts` requests the `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` or `FOREGROUND_SERVICE` permissions, or starts a foreground service.

Tests: `no_battery_exemption_or_foreground_service`, `the_module_stays_inside_its_boundary`.

## Decisions for the Operator

1. **TypeScript seams; the Expo and Android adapters are enabling work.** The unit is tested through `SchedulingPlatform` and `SchedulingNotifier` fakes. Production needs the following:
   - the `expo-background-task`, `expo-task-manager`, and `expo-notifications` dependencies, pinned to Expo 57's bundled versions;
   - Android's notification permission;
   - a small native restriction check, from Decision 2;
   - `TaskManager.defineTask` at module scope;
   - the foreground and manual trigger wiring.

   That carries no governed behavior, so it becomes an enabling Bead, "Wire background polling into the Expo shell", exercised by the device smoke run. The alternative is to install and adapt the packages here, which no device-free test could exercise.
2. **"Restricted" includes Android's own restrictions.** Expo always reports `Available` on Android (fact 3). The platform seam therefore also reports restricted when `ActivityManager.isBackgroundRestricted()` is true, or when the app is in the `RESTRICTED` standby bucket. Both stop deferrable work and need the Operator to change a battery setting. The alternative is to trust Expo's status, so that v1 could never report `restricted` on Android.
3. **Register only when missing.** Registering replaces the pending WorkManager request and restarts its delay (fact 2). Re-registering on every app open could keep pushing the next background run away. The alternative, re-registering every time, guarantees the 60-minute interval is current, at the cost of that delay.
4. **A missing registration heals itself.** `ensureRegistered()` registers a missing task, and the condition is `unregistered` only if it is still missing afterward. That happens, for example, when registration throws. A registration that has silently disappeared is fixed without Operator action. The alternative is to notify on every disappearance, even ones the Sender repairs.
5. **The status is observed for each run.** `observe()` runs when each Polling Run opens. That covers recovered interrupted runs, which carry their opening observation, and the app's start and return to the foreground. It is recorded with its time and never claims to predict the next run (ADR 0049).
6. **One notification per onset.** The notification names only the condition. A failed notification is retried, and recovery clears the condition so that a later onset notifies again. The alternative is to notify on every observation, which ADR 0013 rules out for repeated states.
7. **The background budget is eight minutes.** This leaves two minutes of WorkManager's window for staging and checkpointing (ADR 0018). The task reports success only when the run checkpointed. Expo's result does not change how Android schedules the next run.
8. **Platform errors read as the worst plausible state (R8).** An unreadable registration is unregistered, and an unreadable restriction is restricted, so a broken platform call surfaces as Operator-action-required rather than as a false `available`.
9. **Expo's own constraints are accepted.** Background runs need a connected network and are skipped while the app is in the foreground (fact 2). A foreground trigger covers the second case, and delivery needs the network anyway. Physical-device qualification measures the real cadence. The alternative is a project-owned WorkManager adapter, which replaces Expo BackgroundTask contrary to ADR 0018.
10. **One small table.** The migration adds a one-row `scheduling_state` table holding the last observation, its time, the condition, and the last notified condition. It is Sender schema version 6, registered after `coordinate-sender-sync@1`.

## Ground rules

- The fakes are synthetic. The platform fake scripts registration and restriction, including thrown and malformed results. The notifier fake records each notification and can throw. The sync fake records each trigger and scripts its result. No fixture contains real health data, Device identifiers, or credentials.
- Assertions that something writes nothing compare the whole database: `PRAGMA user_version`, every schema object, and every row of every table.
- A restart is simulated by reopening the database file in a new instance. The checkpoint test also abandons a held Polling Run there, so the next process recovers it as interrupted.
- The runner sets a per-test timeout, so an implementation that hangs fails rather than stalls CI.
- Not mechanically provable here; the approver confirms these in implementation review:
  - the shell defines the task at module scope;
  - the production adapters honor the seams;
  - Expo and Android behave as facts 1 to 4 describe, which physical-device qualification checks.

## Freeze prerequisites

The Operator approved the draft. The executable pack adds:
- the TypeScript suite `schedule-background-polling-1.test.ts`, with its platform, notifier, and sync fakes;
- the Python contract suite;
- the runner;
- the Interface stub `scheduling/schedule-background-polling.ts`, whose migration and functions throw;
- the pinned migration DDL (R10).

Against the stub, every TypeScript test fails, and the two Python tests pass. The binding is:
- spec files: the contract and this pack;
- test files: `schedule-background-polling-1.test.ts`, `synthetic-sync-sources.ts`, `node-database.ts`, `node-installation-cipher.ts`, `test_schedule_background_polling_1.py`, and the runner.

GPT 6.1 Sol's read-only implementability review runs before Gate B. The implementation registers `schedulingMigration` sixth and updates the production-migration test in `storage.test.ts`.
