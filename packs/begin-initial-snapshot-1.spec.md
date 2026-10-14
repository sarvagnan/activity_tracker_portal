# Draft acceptance pack — `begin-initial-snapshot@1`

**Status:** Draft approved by the Operator on 2026-10-14, including the seven decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `begin-initial-snapshot` Governed Scope contract in `governance/scopes/begin-initial-snapshot.1.json`. Before the first page of a record type's Initial Snapshot, the Sender obtains a separate Health Connect changes token and durably commits it with a new snapshot identity (ADR 0008, 0012, 0047). The later catch-up Change Cycle then reads from that token, so an insert, update, or deletion that races the baseline traversal is not lost. If the commit fails, nothing was begun.

The unit has two parts:
- **Kotlin** requests the token. It checks permissions around the request and classifies failures, as the page readers do.
- **TypeScript** issues the snapshot identity and commits it with the token to the Sender's SQLite database.

Exposing the Kotlin part to TypeScript through the Expo module waits for `coordinate-sender-sync`, its first caller, as it does for the readers. That unit also decides when to begin, and when an attempt is to be abandoned.

The unit also supplies the unread-since instant that `read-change-page@1` Decision 1 requires of it: the time read before the token was requested.

## Health Connect facts

On Android 14 or later, the pinned SDK's `getChangesToken` calls the platform's `getChangeLogToken`, according to AOSP `HealthConnectServiceImpl`.
- The token is positioned at the latest change-log row when it is issued, so every later change follows it.
- The request requires the read permission of each requested type. A missing permission is a `SecurityException`.
- The request needs no background permission, and an empty type set is an invalid argument.
- The SDK maps platform errors as it does for reads.

## Interface

**Kotlin**, package `org.activitytracker.healthconnect.token` in the project-owned Health Connect module, using the existing `HealthConnectAccess` seam (`getGrantedPermissions`, `getChangesToken`) and `org.activitytracker.healthconnect.projection`:
- `suspend fun issueChangesToken(access: HealthConnectAccess, clock: Clock, recordType: String): TokenIssuance`. A `recordType` that is not a key of `PROJECTION_SCHEMA_VERSIONS` throws `IllegalArgumentException`.
- `TokenIssuance` is one of:
  - `TokenIssuance.Issued(changesToken: String, unreadSince: Instant)`
  - `TokenIssuance.NotPermitted(failure: String)`
  - `TokenIssuance.ReadFailed(failure: String)`

**TypeScript**, module `sender/app/src/snapshot/begin-initial-snapshot.ts`, over the `SenderDatabase` seam from `sender/app/src/storage`:
- `snapshotAttemptsMigration: Migration`, which is Sender schema migration version 1.
- `ChangesTokenIssuer`, with `issue(recordType: string): Promise<IssuedToken>`. `IssuedToken` is one of:
  - `{ outcome: "issued", changesToken: string, unreadSinceMs: number }`
  - `{ outcome: "not-permitted" | "read-failed", failure: string }`
- `beginInitialSnapshot(database, issuer, request: { recordType, installationId, abandoning? }, options: { randomUUID: () => string, now: () => number }): Promise<BeginResult>`. `BeginResult` is one of:
  - `{ outcome: "begun", attempt: SnapshotAttempt }`
  - `{ outcome: "refused" | "not-permitted" | "read-failed" | "storage-failed", failure: string }`
- `currentSnapshotAttempt(database, recordType)` and `snapshotAttempt(database, snapshotId)` each return a `SnapshotAttempt` or `null`.
- `SnapshotAttempt` has:
  - `snapshotId`, `recordType`, `installationId`, and `changesToken`;
  - `kind`, which is `"initial"` or `"reconciliation"`;
  - `unreadSinceMs` and `begunAtMs`;
  - `abandonedAtMs`, a number or `null`.

  Times are integer milliseconds since the epoch.
- Caller errors throw `TypeError`:
  - a `recordType` that does not match the Wire Protocol record-type pattern;
  - an `installationId` or `abandoning` that is not a lowercase UUID;
  - a `randomUUID` result that is not a lowercase version 4 UUID.

## Requirements

Each requirement is bound to named executable tests:
- **Kotlin:** JVM tests in `IssueChangesToken1Test` use a synthetic Health Connect.
- **TypeScript:** `node:test` suites in `begin-initial-snapshot-1.test.ts` use the `node:sqlite` `SenderDatabase` substitute from the 3ug test support, and a scripted issuer.
- **Python:** `tests/acceptance/test_begin_initial_snapshot_1.py` checks the contract.

The runner `tests/acceptance/run_begin_initial_snapshot_1.sh` runs all three. It verifies fresh reports rather than exit statuses, as `read-change-page@1`'s does. Fixtures are synthetic.

### R1 — Governed Scope contract

The contract names:
- the scope, version, kind, and purpose;
- inputs, with their modes;
- outputs;
- every named failure, with one of the outcome classes `refusal`, `not-permitted`, `read-failure`, or `storage-failure`;
- both effects, the token request and the storage write;
- authority;
- the `project-source-record@1` dependency;
- the acceptance oracle.

It does not contradict the `begin-initial-snapshot` scope-map row, which now names that dependency.

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — Token request (Kotlin)

`issueChangesToken` reads the clock once, before any seam call. It then makes three seam calls, in order:
1. it reads the granted permissions;
2. it requests one token with `ChangesTokenRequest(setOf(the type's SDK class), dataOriginFilters = emptySet())`;
3. it reads the granted permissions again.

`unreadSince` is the clock reading truncated down to whole milliseconds. Truncating makes it earlier, which keeps `read-change-page@1`'s age check conservative. An unknown `recordType` throws `IllegalArgumentException` before any seam call.

Tests: `the_clock_is_read_once_before_any_call`, `one_token_is_requested_for_the_type_and_every_origin`, `every_supported_type_requests_its_sdk_class`, `unread_since_is_truncated_to_milliseconds`, `unknown_record_type_is_a_caller_error`.

### R3 — Full-history permission (Kotlin)

Both permission reads must include the type's read permission and `PERMISSION_READ_HEALTH_DATA_HISTORY`:
- A missing read permission is `read-permission-missing`.
- Otherwise, a missing history permission is `history-permission-missing`.
- A missing permission before the request means no token is requested.
- A missing permission after it discards the token.
- A `SecurityException` is `read-denied`.

Tests: `missing_read_permission_requests_no_token`, `missing_history_permission_requests_no_token`, `permission_lost_during_the_request_discards_the_token`, `security_exception_is_read_denied`.

### R4 — Failure classification and validation (Kotlin)

An exception from any of the three seam calls maps to exactly one failure, as in `read-snapshot-page@1` R11:

| Exception | Failure |
|---|---|
| `SecurityException` | `read-denied` |
| `RemoteException` or `IllegalStateException` | `health-connect-unavailable` |
| `IOException` | `health-connect-io-failure` |
| `IllegalArgumentException` | `health-connect-request-rejected` |
| any other `Exception` | `unexpected-read-failure` |

`CancellationException` and any non-`Exception` `Throwable` propagate as the same instance. An empty token is `source-contract-violation`.

Tests: `each_exception_class_maps_to_one_named_failure`, `cancellation_propagates`, `an_empty_token_is_a_violation`.

### R5 — Beginning a snapshot (TypeScript)

`beginInitialSnapshot` returns `begun` only after one SQLite transaction has committed all of the following:
- A new attempt with the generated `snapshotId`, `kind: "initial"`, the record type, the installation, the issued token and `unreadSinceMs`, `begunAtMs` from `now()`, and no `abandonedAtMs`.
- The attempt made the record type's current attempt.

`begun` carries that attempt, and `currentSnapshotAttempt` and `snapshotAttempt` return it afterwards, including after the database is closed and reopened.

The empty page-chain state is the attempt alone. `stage-outbox-batch` starts from the first page, with zero counts and the Wire Protocol chain start of `snapshotId`, until it commits progress.

Tests: `begin_commits_the_attempt_and_makes_it_current`, `the_attempt_survives_reopening`, `the_snapshot_identity_is_the_generated_uuid`, `begin_records_the_issued_token_and_unread_since`, `reading_an_unknown_attempt_returns_null`.

### R6 — One current attempt per record type (TypeScript)

A record type has at most one current attempt, and attempts of other types are independent.
- **No `abandoning`:** beginning when the type already has a current attempt is `attempt-open`.
- **With `abandoning`:** it must name the type's current attempt, or the result is `attempt-mismatch`. That includes the case where the type has no current attempt.
- **A matched `abandoning`:** the named attempt gets `abandonedAtMs` from `now()`, is retained, and stops being current. The new attempt becomes current. Both changes happen in the same transaction as R5.

These checks run once before the token is requested, so a refused begin requests no token. They run again inside the commit transaction, so a concurrent begin cannot leave two current attempts.

Tests: `a_current_attempt_refuses_a_new_begin`, `abandoning_must_name_the_current_attempt`, `a_named_attempt_is_abandoned_and_retained`, `refused_begins_request_no_token`, `types_are_independent`, `the_commit_rechecks_the_current_attempt`.

### R7 — Nothing begun on failure (TypeScript)

None of the following writes anything:
- a refusal;
- an issuer result of `not-permitted` or `read-failed`;
- an issuer that throws;
- an issuer result that is malformed.

If the issuer throws, the result is `read-failed` with `unexpected-read-failure`. A malformed result is `read-failed` with `source-contract-violation`. That covers an empty or non-string token, an `unreadSinceMs` that is not a safe integer, an unknown outcome, and a failure name the contract does not give that outcome class.

A database error at any point is `storage-failed`. Inside the transaction, it rolls back everything, including a named abandonment, and the issued token is discarded. The database is compared before and after each of these cases.

Tests: `issuer_failures_write_nothing`, `a_throwing_issuer_is_an_unexpected_read_failure`, `malformed_issuances_are_violations`, `storage_errors_roll_back_everything`, `refusals_write_nothing`.

### R8 — The Sender's first schema migration (TypeScript)

`snapshotAttemptsMigration` is Sender schema version 1, applied by the 3ug `migrate` runner. It creates this unit's tables, and nothing reads or writes them except this module.
- **On an empty database,** `migrate([snapshotAttemptsMigration])` reaches version 1.
- **The schema itself** enforces one current attempt per record type. A current attempt must exist and be of that record type, and no attempt is current for two types. The schema also rejects an attempt whose `kind` is not `initial` or `reconciliation`. The tables are:

  ```sql
  CREATE TABLE snapshot_attempts (
    snapshot_id TEXT PRIMARY KEY NOT NULL, record_type TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('initial', 'reconciliation')), installation_id TEXT NOT NULL,
    changes_token TEXT NOT NULL, unread_since_ms INTEGER NOT NULL, begun_at_ms INTEGER NOT NULL,
    abandoned_at_ms INTEGER, UNIQUE (snapshot_id, record_type)) STRICT;
  CREATE TABLE current_snapshot_attempts (
    record_type TEXT PRIMARY KEY NOT NULL, snapshot_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY (snapshot_id, record_type) REFERENCES snapshot_attempts (snapshot_id, record_type)) STRICT;
  ```
- **On a database already at version 1,** migration changes nothing.
- **Later units** append their own migrations after it.

Tests: `migration_creates_version_one`, `the_schema_enforces_one_current_attempt`, `migration_is_idempotent`.

### R9 — Caller errors (TypeScript)

The following throw `TypeError` before any database or issuer call:
- a malformed `recordType`, `installationId`, or `abandoning`;
- a `randomUUID` that returns anything other than a lowercase version 4 UUID.

`randomUUID` is called after the issuer succeeds and before the commit.

Tests: `malformed_requests_are_caller_errors`, `a_bad_uuid_source_is_a_caller_error`.

### R10 — Health-data-free outcomes

The following carry only a named failure: every non-`begun` result, `TokenIssuance.NotPermitted`, and `TokenIssuance.ReadFailed`. They contain no exception text, tokens, or times. Tokens and times appear only in `begun` and `Issued`.

Every named failure is reachable, and no other failure is produced.

Tests: `non_issued_outcomes_carry_only_named_failures` and `every_kotlin_failure_is_reachable_and_in_the_contract` (Kotlin); `non_begun_results_carry_only_named_failures` and `every_named_failure_is_reachable_and_in_the_contract` (TypeScript, which reaches the Kotlin failures through the scripted issuer).

### R11 — Order of checks

Within each part, the first applicable check in this order wins.

**Kotlin:**
1. the unknown record type;
2. permissions before the request;
3. the request and its classification;
4. permissions after the request;
5. the empty token.

**TypeScript:**
1. caller errors;
2. the current-attempt refusals;
3. the issuer outcome and validation;
4. the UUID source;
5. the commit, with its recheck and storage failure.

Tests: `kotlin_checks_follow_the_documented_order`, `typescript_checks_follow_the_documented_order`.

## Decisions for the Operator

1. **Kotlin issues the token and TypeScript commits it.** Health Connect is reachable only from Kotlin, and Sender storage lives in TypeScript (ADR 0014, 0016, 0018). Each part is tested on its own seam. The Expo bridge that joins them waits for `coordinate-sender-sync`, its first caller.
2. **Beginning requires the full-history permission.** Issuing the token itself needs only the read permission. But a snapshot without the history permission would silently miss older records, and `read-snapshot-page@1` refuses it. Checking the history permission before and after the request (R3) avoids beginning a snapshot that cannot proceed.
3. **Unread-since is the instant read before the request, truncated to milliseconds.** That satisfies `read-change-page@1` Decision 1: every change after the token was logged at or after that instant. Truncating only moves it earlier, so the 30-day age limit counts conservatively. It is stored with the token and handed to the catch-up cycle.
4. **Abandonment is explicit.** `begin-initial-snapshot` never decides on its own that an attempt is dead.
   - It refuses while a record type has a current attempt, unless the caller names that attempt.
   - A named attempt is abandoned, not deleted, in the same transaction that begins its replacement.
   - Abandonment applies before first publication, following ADR 0047 and 0048.
   - The caller, `coordinate-sender-sync`, decides when an attempt is to be abandoned. After an established baseline, it calls `begin-reconciliation-snapshot` instead.
5. **The Sender's first schema migration.** This unit owns Sender schema version 1: its snapshot-attempt tables and the one-current-attempt rule. The 3ug enabling task left the migration list empty for governed units to fill. Each later Sender unit appends its own migration and reaches these attempts only through this module's functions, so unit-owned meaning stays with its unit.
6. **A failed commit discards the token.** Health Connect issues tokens freely, so no partial state is ever kept. A failed commit simply means a retry on a later run, which requests a fresh token.
7. **The record-type vocabulary comes from `project-source-record@1`.** The scope-map row named no dependency. The supported record types are `project-source-record@1`'s, so this unit now depends on it, and the row is updated.

## Ground rules

- Kotlin JVM tests use a synthetic Health Connect implementing `HealthConnectAccess`. It records calls, holds a granted-permission set, and scripts tokens and exceptions. It runs with no device or Context.
- TypeScript tests use the 3ug `node:sqlite` `SenderDatabase` substitute in temporary files, a scripted issuer, and injected UUID and time sources. They inject database failures through a wrapping `SenderDatabase`, which fails each mutating statement in turn, or every read.
- The runner does not trust exit statuses. It passes the TypeScript and Kotlin suites only when fresh JUnit reports list exactly the bound suites' tests, none failed, errored, or skipped.
- The trusted toolchain boundary is:
  - Node 26.11.1, pinned by `.node-version`;
  - `uv` and the pinned CPython;
  - the checksum-verified JDK;
  - the generated `sender/app/android` project and its Gradle wrapper;
  - Gradle and its plugins;
  - the 3ug storage seam in `sender/app/src/storage/`, which the implementation uses and registers its migration in.
- Not mechanically provable here; the approver confirms these in implementation review:
  - the Kotlin part reads no records or changes and writes nothing to Health Connect;
  - the production UUID source is a cryptographically random version 4 generator;
  - the implementation registers `snapshotAttemptsMigration` as the first entry of the production migration list, and changes nothing in the trusted toolchain boundary or the 3ug storage seam's behavior.

## Freeze prerequisites

The Operator approved the draft. The executable pack adds:
- the Kotlin suite, 15 tests, and its synthetic `TokenHealthConnect`;
- the TypeScript suite, 24 tests;
- the Python contract suite, 2 tests;
- the self-verifying runner;
- the Interface stubs `token/IssueChangesToken.kt` and `sender/app/src/snapshot/begin-initial-snapshot.ts`.

It pins the table layout in R8, which the schema test relies on, and the validation of issuer failure names in R7.

Against the stubs:
- every Kotlin test fails, 12 with `NotImplementedError`, and the 2 caller-error and ordering tests and the cancellation test because the stub's error comes first;
- every TypeScript test fails, because the stub's migration throws and the 3ug runner reports `frozen`, which the fixture asserts against;
- the two Python tests pass, because they check the contract.

The binding is:
- spec files: the contract and this pack;
- test files:
  - `IssueChangesToken1Test.kt` and `TokenHealthConnect.kt`;
  - `begin-initial-snapshot-1.test.ts` and its database substitute `sender/app/test/support/node-database.ts`;
  - `test_begin_initial_snapshot_1.py` and the runner;
  - `sender/scripts/gradle.sh` and the module's `android/build.gradle`.
