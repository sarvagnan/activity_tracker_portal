# Draft acceptance pack — `read-change-page@1`

**Status:** Draft approved by the Operator on 2026-10-14, including the six decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `read-change-page` Governed Scope contract in `governance/scopes/read-change-page.1.json`. It is the Kotlin reader that fetches one page of one record type's Change Cycle from Health Connect, projects every upserted record on it through the published `project-source-record@1` (ADR 0010), and turns every deletion into a Source Retraction (ADR 0008). It claims no durable progress: the caller stages the page with its tentative next token (ADR 0012), or discards both.

The reader receives the token and the Change Cycle identity but issues neither. The pre-read tokens belong to `begin-initial-snapshot` and `begin-reconciliation-snapshot` (ADR 0047, 0048). Staging belongs to `stage-outbox-batch` and Change Completion to `stage-sync-completion`. Scheduling, retry, Projection Refusal blocking, permission-gap pauses, and the response to an expired token belong to `coordinate-sender-sync` and the two begin units. Snapshot pages belong to `read-snapshot-page`. Exposing the reader to the TypeScript shell through the Expo module waits for `coordinate-sender-sync`, its first caller.

## Health Connect facts

The pinned SDK is `androidx.health.connect:connect-client:1.1.0` on Android 14 or later, where `getChanges` delegates to the platform Health Connect service. The facts below come from the Android Open Source Project's HealthFitness module (`ChangeLogsHelper`, `ChangeLogsRequestHelper`, `DailyCleanupJob`, `RecordHelper`, `StorageUtils`, and `HealthConnectServiceImpl.getChangeLogs`) and from the pinned SDK's `HealthConnectClientUpsideDownImpl.getChanges`. They shape Decisions 1 to 4, and physical-device qualification confirms them (ADR 0036).

1. **Pages.** A token names a stored request: record types, an origin filter, and a position in Health Connect's change log. Each write logs rows per record type and writing app, of up to 1,000 identifiers each. A page reads the change-log rows after that position until it has collected 1,000 record identifiers, the SDK's fixed page size. Because one row holds up to 1,000 identifiers and the count is checked before a row is added, a page can carry up to 1,999 identifiers. Health Connect stores a new token at the page's last row and reports whether more rows remain. A page that reads no rows returns the request's own token.
2. **Expiry.** Health Connect's daily cleanup deletes change-log rows and token requests 32 days after they were written. A next-page token is stored without a time, so cleanup never deletes it. Reading a next-page token more than 32 days after the changes behind it were logged therefore silently skips the deleted rows, with no expiry reported. Only a token Health Connect cannot find reads as expired.
3. **Expiry reporting.** The SDK reports every invalid-argument error from `getChangeLogs` as `changesTokenExpired`, with no changes and an empty next token. That includes an unknown token, a deleted one, and one issued to another package.
4. **Contents.** A page's upsertions are the records Health Connect holds for the upserted identifiers in its rows *when the page is read*: their current state, in no defined order, each once, and absent when deleted since. Its deletions are the deleted identifiers in its rows, grouped by writing app in no defined order, and repeated when deleted repeatedly. The SDK lists every upsertion, then every deletion. Deletions carry only the identifier.
5. **Identifiers.** A record written with a client record ID gets an identifier derived from its app, record type, and client record ID. An app that deletes a record and writes it again under the same client record ID reuses the identifier.
6. **History.** Without `PERMISSION_READ_HEALTH_DATA_HISTORY`, Health Connect silently omits upserted records that start more than 30 days before the first grant. Deletions are not filtered.
7. **Background.** In the background, `getChangeLogs` requires `READ_HEALTH_DATA_IN_BACKGROUND`, and is a `SecurityException` without it.

## Interface

Kotlin, package `org.activitytracker.healthconnect.changes` in the project-owned Health Connect module, depending only on the access seam and `org.activitytracker.healthconnect.projection`:

- A new seam, `HealthConnectChangeAccess`, declares `suspend fun getGrantedPermissions(): Set<String>` and `suspend fun getChanges(changesToken: String): ChangesResponse`, matching the SDK's `PermissionController` and `HealthConnectClient`. The reader calls nothing else. `HealthConnectAccess` stays unchanged because the published `read-snapshot-page@1` binds a fake that implements it; a production adapter implements both.
- `ChangePageRequest(recordType: String, cycleId: String, changesToken: String, unreadSince: Instant, context: ProjectionContext)`. Construction throws `IllegalArgumentException` in any of these cases, because each is a caller error rather than an outcome:
  - `recordType` is not a key of `PROJECTION_SCHEMA_VERSIONS`;
  - `cycleId` does not match the Wire Protocol `uuid` pattern;
  - `changesToken` is empty.
- `suspend fun readChangePage(access: HealthConnectChangeAccess, clock: Clock, request: ChangePageRequest): ChangePageResult`, where `ChangePageResult` is one of:
  - `ChangePageResult.Page(recordType: String, cycleId: String, projectionSchemaVersion: Int, items: List<ChangeItem>, nextChangesToken: String, nextUnreadSince: Instant, hasMore: Boolean)`
  - `ChangePageResult.TokenExpired(failure: String)`
  - `ChangePageResult.ProjectionRefused(refusal: ProjectionResult.Refused)`
  - `ChangePageResult.NotPermitted(failure: String)`
  - `ChangePageResult.ReadFailed(failure: String)`
- `ChangeItem` is `ChangeItem.Retraction(sourceIdentity: String)` or `ChangeItem.Projection(bytes: ByteArray)`, matching the Wire Protocol's `retraction` and `projection` items.
- `CHANGES_TOKEN_MAX_AGE = Duration.ofDays(30)` and `CLOCK_BACKWARD_TOLERANCE = Duration.ofDays(1)`.

## Requirements

Each requirement is bound to named executable tests. Kotlin JVM tests in `ReadChangePage1Test` drive the reader through a synthetic fake, `ChangeHealthConnect`, that models Health Connect's change log, and the Python suite `tests/acceptance/test_read_change_page_1.py` checks the contract. The runner `tests/acceptance/run_read_change_page_1.sh` runs both. No fixture contains real health data, Device identifiers, or credentials.

### R1 — Governed Scope contract

The contract names the scope, version, kind, purpose, inputs with their modes, outputs, every named failure with one of the outcome classes `token-expired`, `projection-refusal`, `not-permitted`, or `read-failure`, the Health Connect read effect, authority, the `project-source-record@1` dependency, and the acceptance oracle. It does not contradict the `read-change-page` scope-map row.

Tests: `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract` (Python).

### R2 — Request validation

A request names a record type from `PROJECTION_SCHEMA_VERSIONS`, a lowercase UUID Change Cycle identity, and a non-empty token. Anything else throws `IllegalArgumentException` before any Health Connect call.

Tests: `unknown_record_type_is_a_caller_error`, `malformed_cycle_identity_is_a_caller_error`, `empty_changes_token_is_a_caller_error`.

### R3 — Token age

The reader reads the clock once, before any Health Connect call. The token's age is that time minus `unreadSince`. The reader returns `TokenExpired` without calling Health Connect, not even for permissions, in either of these cases:
- `changes-token-too-old`: the age is `CHANGES_TOKEN_MAX_AGE` or more;
- `clock-behind-changes-token`: `unreadSince` is more than `CLOCK_BACKWARD_TOLERANCE` after the clock's time.

An age of exactly 30 days is too old. A clock up to one day behind `unreadSince` reads normally, so ordinary clock corrections between back-to-back pages do not force a reconciliation.

The fake deletes change-log rows and token requests 32 days after they were written, and never deletes a next-page token, as Health Connect does. One test shows that the fake silently drops changes behind a stale next-page token, so the age limit is the only protection.

Tests: `the_clock_is_read_once_before_health_connect`, `a_token_at_the_age_limit_is_refused_without_reading`, `a_token_under_the_age_limit_is_read`, `a_clock_behind_the_token_beyond_tolerance_is_refused_without_reading`, `a_clock_behind_the_token_within_tolerance_is_read`, `the_fake_drops_changes_behind_a_stale_next_page_token`.

### R4 — Full-history permission, checked around the read

Immediately before and immediately after the change read, the reader reads the granted permissions, so a successful read calls the seam exactly three times: permissions, changes, permissions. Both reads must include the type's read permission (`HealthPermission.getReadPermission`) and `PERMISSION_READ_HEALTH_DATA_HISTORY`:

- The read permission missing is `read-permission-missing`.
- Otherwise, the history permission missing is `history-permission-missing`.
- A missing permission before the read means changes are never requested.
- A missing permission after the read discards the page.
- A `SecurityException` from Health Connect is `read-denied`, including a background read without the background permission.

In every case the outcome is `NotPermitted`, with no page and no token.

Tests: `missing_read_permission_reads_no_changes`, `missing_history_permission_reads_no_changes`, `permissions_are_read_before_and_after_the_changes`, `permission_lost_during_the_read_discards_the_page`, `security_exception_is_read_denied`.

### R5 — The read and its expiry

Every page is one `getChanges` call with the request's token. A response reporting `changesTokenExpired` is `TokenExpired("changes-token-expired")`, whatever else it carries. Because the SDK reports every invalid-argument error as expiry, an unknown or corrupt token reads as expired too.

Tests: `one_change_read_uses_the_request_token`, `health_connect_expiry_is_token_expired`, `expiry_wins_over_other_response_fields`, `an_unknown_token_reads_as_expired`.

### R6 — Items and their order

A page's items are, in this order:
1. A `Retraction` for every deleted identifier, in Health Connect's order. An identifier deleted more than once on the page appears once, at its first position.
2. A `Projection` for every upserted record, in Health Connect's order, holding exactly the bytes `projectSourceRecord` returns with the request's `ProjectionContext`.

An identifier both deleted and upserted on one page is retracted, then projected. Health Connect read the upserted record after every change in the page's rows, so the record exists at the read (Decision 2). `projectionSchemaVersion` is the type's entry in `PROJECTION_SCHEMA_VERSIONS`.

If any upserted record refuses, the result is `ProjectionRefused` with the first refusal in projection order, and no token is returned. A page may be empty.

Tests: `retractions_precede_projections`, `each_kind_keeps_health_connect_order`, `repeated_deletions_retract_once`, `a_reused_identifier_is_retracted_then_projected`, `projection_bytes_equal_the_shared_projection`, `one_unprojectable_record_refuses_the_whole_page`, `refusal_is_the_first_in_projection_order`.

### R7 — Next token, continuation, and unread-since

`nextChangesToken` and `hasMore` are the response's. `nextUnreadSince` depends on `hasMore`:
- If more pages remain, it is the request's `unreadSince`. The changes still behind the next token may have been logged before this read, so the age keeps counting from the start of the cycle.
- If no page remains, it is the clock time the reader read before its Health Connect calls. Every change after the next token was then logged after that time.

The reader persists nothing. Re-reading a token against unchanged Health Connect data returns the same items, byte for byte, whenever Health Connect answers in the same order. It defines no upsertion order, so a reread answered in another order keeps that order (R6). Health Connect stores a new next token for each read, so the token string may differ. A token with no changes since returns an empty page with no more pages remaining and the same token.

Tests: `next_token_and_continuation_come_from_health_connect`, `unread_since_carries_through_a_cycle`, `unread_since_restarts_when_no_page_remains`, `rereading_a_token_returns_identical_items`, `no_change_returns_an_empty_final_page`.

### R8 — Convergence

Apply a Change Cycle's pages in order to a replica of the type's records, starting from the records present when the token was issued: a retraction removes its identifier, and a projection sets it. The cycle runs while records are inserted, updated, deleted, and written again under a reused identifier, between reads and during them. After a cycle whose final read saw no concurrent change, the replica holds exactly the projections of the records Health Connect then holds. The randomized test runs twelve seeds: eight with small changes, and four that begin with a 1,200-record write so their cycles span pages.

Tests: `replica_converges_under_concurrent_change`, `a_record_deleted_after_its_upsert_is_only_retracted`.

### R9 — Page size

Health Connect sizes pages; the reader sets no record limit (Decision 5). A page can hold more than 1,000 items. A page can also hold no items while more pages remain, when the records upserted on it were deleted after the page's rows were logged; the reader returns it as an ordinary page, and the cycle continues.

Tests: `a_page_may_exceed_one_thousand_items`, `an_empty_page_with_more_remaining_is_a_page`.

### R10 — Response validation

The reader trusts Health Connect only as far as these rules. A response that breaks any of them is `ReadFailed("source-contract-violation")`, with no page or token:
- it holds an upserted record of another type;
- two of its upserted records share an identifier;
- it holds a deleted identifier that is not a Wire Protocol Source Identity: 1 to 256 Unicode code points, valid Unicode, and none from U+0000 to U+001F;
- it holds a change that is neither an `UpsertionChange` nor a `DeletionChange`;
- its next token is empty;
- it reports more pages but returns the request's own token, which would never progress.

Tests: `upserted_records_of_another_type_are_violations`, `duplicate_upserted_identifiers_are_violations`, `deleted_identifiers_outside_the_source_identity_form_are_violations`, `unknown_change_kinds_are_violations`, `empty_next_tokens_are_violations`, `more_pages_without_a_new_token_are_violations`.

### R11 — Failure classification

An exception from any of the three seam calls (the permission read before, the change read, and the permission read after) maps to exactly one outcome:

| Exception | Outcome |
|---|---|
| `SecurityException` | `NotPermitted("read-denied")` |
| `android.os.RemoteException` or `IllegalStateException` | `ReadFailed("health-connect-unavailable")` |
| `IOException` | `ReadFailed("health-connect-io-failure")` |
| `IllegalArgumentException` | `ReadFailed("health-connect-request-rejected")` |
| any other `Exception` | `ReadFailed("unexpected-read-failure")` |

The SDK reports rate limiting, an internal provider error, and data sync in progress as `IllegalStateException`. A `CancellationException` always propagates as the same instance, and so does a `Throwable` that is not an `Exception`. Because `java.util.concurrent.CancellationException` is an `IllegalStateException`, it must be recognized first. Only exceptions from seam calls are classified; the approver confirms in implementation review that the reader's own defects are not turned into read failures.

Tests: `each_exception_class_maps_to_one_named_failure`, `cancellation_propagates`.

### R12 — Health-data-free outcomes

`TokenExpired`, `ProjectionRefused`, `NotPermitted`, and `ReadFailed` carry only the named failure and, for a refusal, `project-source-record`'s health-data-free refusal. They carry no exception text, record identifiers, times, values, or tokens, so the caller can persist and report them.

Every named failure in the contract is reachable, and the reader returns no failure the contract does not name.

Tests: `non_page_outcomes_carry_only_named_failures`, `every_named_failure_is_reachable_and_in_the_contract`.

### R13 — Order of checks

When several outcomes apply, the first in this order wins:
1. Request validation, at construction (R2).
2. The token's age: `changes-token-too-old`, then `clock-behind-changes-token`. Neither calls Health Connect (R3).
3. The permission read before the change read (R4).
4. The change read and its exception classification (R11).
5. The permission read after the change read (R4).
6. Expiry reported by Health Connect (R5).
7. Response validation (R10).
8. Projection of the page (R6).

Tests: `failures_follow_the_documented_order`.

## Decisions for the Operator

1. **A project-owned token age limit.** Health Connect deletes change logs 32 days after they are written but never expires a next-page token. A Sender that resumes after more than 32 days without reading would therefore silently skip every update and deletion logged in between (Health Connect fact 2), and the Portal would keep stale or deleted records as current with no visible condition. The reader instead carries, with every token, an instant before which no unread change was logged. It refuses a token 30 days or more past that instant as `changes-token-too-old` (R3, R7), which leaves a two-day margin. The caller then follows ADR 0048: a new Initial Snapshot before first publication, or a Reconciliation Snapshot after it.

   The cost: a type left unread for 30 days rereads its whole history, whether its phone was off, its collection disabled, its permission missing, or the type blocked by a Projection Refusal. `begin-initial-snapshot` and `begin-reconciliation-snapshot` must record the time they read before requesting each pre-read token. The guard trusts the device clock, as Health Connect's cleanup does. It tolerates a clock running up to one day behind and treats one further behind as unprovable (`clock-behind-changes-token`). A clock moved forward and back again between reads cannot be detected, which physical-device qualification cannot rule out.

   The alternative is to trust Health Connect's expiry. That is simpler, but accepts silent loss after a long gap.
2. **Retractions before projections.** Health Connect reports each upserted record as it stands when the page is read, but each deletion as a past event, and the SDK lists upsertions first (Health Connect fact 4). When an app deletes a record and writes it again under the same client record ID within one page (fact 5), the SDK's order would end with the record deleted although it exists. Placing every retraction first gives the state Health Connect held at the read, and keeps the deletion as retained evidence (R6, R8). The alternatives are the SDK's order, which loses such records, or dropping a retraction whose identifier the page also projects, which discards the deletion.
3. **Change pages require the full-history permission.** Without it, Health Connect silently omits upserted records that start more than 30 days before the first grant (fact 6). Edits to older records would never reach the Portal, while their deletions still would. As with snapshots, the reader requires the history permission both before and after each read (R4).
4. **A corrupt or unknown token is an expired token.** The SDK cannot distinguish them (fact 3), so this reader has no invalid-position outcome, matching the scope-map row. A damaged saved token therefore costs a reconciliation, never a silent restart.
5. **Health Connect sizes pages.** The SDK fixes the change page size, so pages hold up to 1,999 items, and some hold none while more remain (R9). `stage-outbox-batch` partitions each page by the Portal's advertised limits (ADR 0012). Page memory for large series records and exercise routes is measured during physical-device qualification.
6. **Read failures retry rather than block.** As in `read-snapshot-page@1`, every non-permission error is a `read-failure`, which the caller reports as `failed` with reason `read-failed` and retries on the next eligible run. That includes the deterministic `source-contract-violation`. The alternative is to block the type, which needs a new Sync Checkpoint reason and a Wire Protocol change.

## Ground rules

- Kotlin JVM tests construct SDK records through the SDK's constructors with synthetic values and identifiers, and run with no Android device, emulator, or Context. The SDK keeps `Metadata`'s full constructor internal, so the tests reach it through Kotlin reflection, as `read-snapshot-page@1` does. `ChangesResponse`, `UpsertionChange`, and `DeletionChange` have public constructors, and `Change` is an open interface, so the tests also build a change of neither kind.
- The synthetic fake models the Health Connect change log this pack relies on:
  - change-log rows of up to 1,000 identifiers per write, record type, and writing app, taken to be the record's data origin;
  - pages that collect 1,000 identifiers and mint a new token at their last row;
  - upsertions read at the time of the read, then deletions grouped by writing app, with repeats;
  - cleanup after 32 days that spares next-page tokens;
  - expiry reported through the SDK's invalid-argument conversion;
  - the granted permission set, including the history filter's strict comparison in milliseconds;
  - scripted mutations and exceptions between and during calls.
- The fake returns upsertions and groups of deletions in a scrambled but deterministic order, so a reader that relies on an order Health Connect does not define fails.
- Not mechanically provable here; the approver confirms these in implementation review:
  - the reader never writes to Health Connect, requests a changes token or a permission, or reads anything besides permissions and changes;
  - Health Connect's real paging, cleanup, and expiry behavior matches the fake, which physical-device qualification checks on the Operator's phone.

## Freeze prerequisites

The Operator approved the draft. The executable pack adds:
- the Kotlin acceptance suite and its synthetic fake;
- the Python contract suite;
- the runner;
- the Interface stub `changes/ChangePage.kt` and the seam `HealthConnectChangeAccess`.

Against the stub, every Kotlin test fails. Forty-one fail with the stub's `NotImplementedError`, the three request-validation tests fail because the stub does not validate, and the cancellation test fails because cancellation meets the stub's error. The two Python tests pass, because they check the contract.

The implementer's read-only implementability review found nothing blocking. Its five clarifications are reflected in R3 (the clock is read once, before any Health Connect call), R6 (a second projection context), R7 (a reread keeps each answer's order), and the fake (rows and deletion groups by writing app, and the history filter's strict comparison).

The binding is:
- spec files: the contract and this pack;
- test files: `ReadChangePage1Test.kt`, `ChangeHealthConnect.kt`, `test_read_change_page_1.py`, and the runner.
