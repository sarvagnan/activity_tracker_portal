# Draft acceptance pack — `read-snapshot-page@1`

**Status:** Draft for Operator review. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `read-snapshot-page` Governed Scope contract in `governance/scopes/read-snapshot-page.1.json`. It is the Kotlin reader that fetches one page of one record type's Initial Snapshot or Reconciliation Snapshot from Health Connect and projects every record on it through the published `project-source-record@1` (ADR 0010). It claims no durable progress: the caller stages the page and its tentative next position together (ADR 0012, 0047), or discards both.

The reader receives the snapshot identity but does not create it. Snapshot identities and their pre-read changes tokens belong to `begin-initial-snapshot` and `begin-reconciliation-snapshot`. Staging belongs to `stage-outbox-batch`, completion to `stage-sync-completion`, and scheduling, retry, Projection Refusal blocking, permission-gap pauses, and invalid-position escalation to `coordinate-sender-sync`. Change pages and retractions belong to `read-change-page`. Exposing the reader to the TypeScript shell through the Expo module waits for `coordinate-sender-sync`, its first caller.

The pinned SDK is `androidx.health.connect:connect-client:1.1.0` on Android 14 or later, where it delegates to the platform Health Connect service. Facts about that service below are taken from the Android Open Source Project's HealthFitness module (`TimeInstantRangeFilter`, `ReadRecordsRequestUsingFilters`, `PageTokenWrapper`, and `RecordHelper`). They shape Decisions 1 to 3, and physical-device qualification confirms them (ADR 0036).

## Interface

Kotlin, package `org.activitytracker.healthconnect.snapshot` in the project-owned Health Connect module, depending only on the access seam and `org.activitytracker.healthconnect.projection`:

- The existing `HealthConnectAccess` seam gains `suspend fun getGrantedPermissions(): Set<String>`, matching the SDK's `PermissionController`, and the reader calls nothing else on it besides `readRecords`.
- `SnapshotPageRequest(recordType: String, snapshotId: String, position: ByteArray?, context: ProjectionContext)`. Construction throws `IllegalArgumentException` when `recordType` is not a key of `PROJECTION_SCHEMA_VERSIONS` or `snapshotId` is not a lowercase UUID, because those are caller errors rather than outcomes. A `null` position means the snapshot's first page.
- `suspend fun readSnapshotPage(access: HealthConnectAccess, request: SnapshotPageRequest): SnapshotPageResult`, where `SnapshotPageResult` is one of:
  - `Page(recordType: String, snapshotId: String, projectionSchemaVersion: Int, projections: List<ByteArray>, nextPosition: ByteArray, exhausted: Boolean)`
  - `ProjectionRefused(refusal: ProjectionResult.Refused)`
  - `NotPermitted(failure: String)`
  - `ReadFailed(failure: String)`
  - `InvalidPosition(failure: String)`
- `SNAPSHOT_PAGE_RECORD_LIMIT = 1000` and `HEALTH_CONNECT_READ_LIMIT = 5000`.

## Requirements

Each requirement must be bound to named executable tests before freeze. Kotlin JVM tests drive the reader through a synthetic Health Connect fake, and a Python suite checks the contract. No fixture contains real health data, Device identifiers, or credentials.

### R1 — Governed Scope contract

The contract names the scope, version, kind, purpose, inputs with their modes, outputs, every named failure with one of the outcome classes `projection-refusal`, `not-permitted`, `read-failure`, or `invalid-position`, the Health Connect read effect, authority, the `project-source-record@1` dependency, and the acceptance oracle. It does not contradict the `read-snapshot-page` scope-map row.

Proposed tests: `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract` (Python).

### R2 — Request validation

A request names a record type from `PROJECTION_SCHEMA_VERSIONS` and a lowercase UUID snapshot identity; anything else throws `IllegalArgumentException` before any Health Connect call.

Proposed tests: `unknown_record_type_is_a_caller_error`, `malformed_snapshot_identity_is_a_caller_error`.

### R3 — Full-history permission, checked around the read

Immediately before and immediately after the record read, the reader reads the granted permissions. Both reads must include the type's read permission (`HealthPermission.getReadPermission`) and `PERMISSION_READ_HEALTH_DATA_HISTORY`:

- The read permission missing is `read-permission-missing`.
- Otherwise, the history permission missing is `history-permission-missing`.
- A missing permission before the read means records are never requested.
- A missing permission after the read discards the page.
- A `SecurityException` from Health Connect is `read-denied`.

In every case the outcome is `NotPermitted` with no page and no position.

Proposed tests: `missing_read_permission_reads_no_records`, `missing_history_permission_reads_no_records`, `permission_lost_during_the_read_discards_the_page`, `security_exception_is_read_denied`.

### R4 — The read request

Every page is one `readRecords` call with these fields:
- the type's SDK class;
- every data origin (an empty origin filter, ADR 0008);
- ascending order;
- no page token.

The first page reads the time range from `Instant.ofEpochMilli(Long.MIN_VALUE)` to `Instant.ofEpochMilli(Long.MAX_VALUE)`, which is every start time Health Connect can store, and requests `SNAPSHOT_PAGE_RECORD_LIMIT` records. A later page starts its range at the position's boundary start time and requests `SNAPSHOT_PAGE_RECORD_LIMIT` plus the number of boundary identifiers, capped at `HEALTH_CONNECT_READ_LIMIT`. A record's start time is `time` for an instantaneous record and `startTime` for an interval or series record.

Proposed tests: `first_page_reads_every_origin_over_the_full_range`, `later_pages_resume_at_the_boundary_start_time`, `no_request_carries_a_page_token`, `record_limit_grows_with_the_boundary_group`.

### R5 — Complete pages

The page is the response's records in Health Connect's order, excluding records at the boundary start time whose identifiers the position lists, up to `SNAPSHOT_PAGE_RECORD_LIMIT` records. Each page record is projected with the request's `ProjectionContext`. If any refuses, the result is `ProjectionRefused` with the first refusal in page order, and no position is returned. Otherwise `projections` holds exactly the bytes `projectSourceRecord` returns, in page order, and `projectionSchemaVersion` is the type's entry in `PROJECTION_SCHEMA_VERSIONS`. Records beyond the page are not projected. A record whose start year falls outside 0000 to 9999 therefore refuses its page through `project-source-record` rather than being skipped.

Proposed tests: `page_bytes_equal_the_shared_projection`, `page_keeps_health_connect_order`, `one_unprojectable_record_refuses_the_whole_page`, `refusal_is_the_first_in_page_order`, `records_beyond_the_page_are_not_projected`.

### R6 — Project-owned position

A position is RFC 8785 canonical JSON in UTF-8 with these keys:
- `position_version`, which is 1;
- `record_type`;
- `snapshot_id`.

Once a page has returned records, it also carries:
- `boundary_start`: the start time of the last record returned, as RFC 3339 UTC in `project-source-record`'s instant form;
- `boundary_record_ids`: the Health Connect identifiers of every record returned so far with exactly that start time, unique and sorted by their UTF-8 bytes.

A `null` request position equals the start form without boundary keys. The next position comes from the last record on the page. Its identifiers include the previous position's when the boundary start time is unchanged. An empty page returns the request's position in canonical form.

The reader returns `InvalidPosition` and reads nothing in either of these cases:
- `malformed-position`: bytes that are not canonical, a missing, extra, or wrongly typed key, an unknown version, an empty or unsorted identifier list, or a boundary start without identifiers.
- `foreign-position`: a position naming another record type or snapshot identity.

Proposed tests: `positions_are_canonical_json`, `null_position_equals_the_start_form`, `next_position_names_the_last_start_time_and_its_identifiers`, `boundary_identifiers_accumulate_within_one_start_time`, `malformed_positions_are_rejected_before_reading`, `foreign_positions_are_rejected_before_reading`.

### R7 — Exhaustion and no durable progress

The snapshot is exhausted when Health Connect returns no page token and every non-excluded record of the response is on the page. Reading again from an exhausted page's next position returns an empty exhausted page with the same position. The reader persists nothing. Against unchanged Health Connect data, the same request returns byte-identical results, so an interrupted caller can re-read a page without changing its bytes.

Proposed tests: `exhaustion_requires_no_token_and_no_untaken_records`, `exhausted_position_rereads_as_an_empty_exhausted_page`, `rereading_a_position_is_byte_identical`, `empty_type_is_one_empty_exhausted_page`.

### R8 — Exactly once for unchanged records

Concatenate the pages of a traversal from the first page to exhaustion, while records are inserted, deleted, updated in place, or moved to another start time between and during reads. The result contains, exactly once and in non-decreasing start-time order, every record that was present and unchanged from the first read until exhaustion. Records changed during the traversal may appear zero or more times. The catch-up Change Cycle from the pre-read token reports them (ADR 0047), and Source Identity reconciliation makes repetition safe.

Proposed tests: `unchanged_records_appear_exactly_once_under_concurrent_change`, `deleting_a_returned_boundary_record_skips_nothing`, `moving_a_returned_boundary_record_skips_nothing`.

### R9 — Large equal-time groups

Records sharing one start time may span any number of pages. When the position already lists `HEALTH_CONNECT_READ_LIMIT` boundary identifiers, the reader cannot request past them. In that case it returns `ReadFailed("boundary-group-too-large")` without reading.

Proposed tests: `an_equal_time_group_larger_than_a_page_spans_pages`, `an_unreadable_boundary_group_fails_without_reading`.

### R10 — Response validation

The keyset position is correct only if Health Connect honors the request. A response that breaks any of these rules is `ReadFailed("source-order-violation")`, with no page or position:
- it holds more records than requested;
- it holds a record starting before the requested start;
- it holds records out of non-decreasing start order;
- two of its records share an identifier.

Proposed tests: `oversized_responses_are_violations`, `records_before_the_requested_start_are_violations`, `unordered_responses_are_violations`, `duplicate_identifiers_are_violations`.

### R11 — Failure classification

An exception from any seam call maps to exactly one outcome:

| Exception | Outcome |
|---|---|
| `SecurityException` | `NotPermitted("read-denied")` |
| `android.os.RemoteException` or `IllegalStateException` | `ReadFailed("health-connect-unavailable")` |
| `IOException` | `ReadFailed("health-connect-io-failure")` |
| `IllegalArgumentException` | `ReadFailed("health-connect-request-rejected")` |
| any other `Exception` | `ReadFailed("unexpected-read-failure")` |

The SDK reports rate limiting, an internal provider error, and data sync in progress as `IllegalStateException`. A `CancellationException` always propagates unchanged, and so does a `Throwable` that is not an `Exception`.

Proposed tests: `each_exception_class_maps_to_one_named_failure`, `cancellation_propagates`.

### R12 — Health-data-free outcomes

`ProjectionRefused`, `NotPermitted`, `ReadFailed`, and `InvalidPosition` carry only the named failure and, for a refusal, `project-source-record`'s health-data-free refusal. They carry no exception text, record identifiers, times, values, or position bytes, so the caller can persist and report them.

Proposed tests: `non_page_outcomes_carry_only_named_failures`.

## Decisions for the Operator

1. **A project-owned position, not Health Connect's page token.** Health Connect's token records the start time of the next record and how many records at that start time were already returned. The next page queries from that start time and skips that many. If a record already returned at that start time is deleted, or edited to another start time, before the next page is read, the skip passes over a different, unchanged record. AOSP's own comment acknowledges records deleted between reads. The changes token reports the deleted or moved record but not the skipped one, so the snapshot silently loses it. In a Reconciliation Snapshot, that loss would become a false Reconciliation Absence and withdraw a real record's contribution.

   The proposed position instead stores the boundary start time and the identifiers already returned at it, re-reads from that time, and drops exactly those identifiers (R6, R8). It cannot expire, so ADR 0008's invalid-position path applies only to corrupt or foreign positions.

   The cost: records sharing one start time are re-read on each page that crosses them, and a group of 5,000 or more cannot be passed (R9). Positions also hold a record start time and Health Connect identifiers. They stay in Sender storage, are never delivered, and `coordinate-sender-sync` must digest them when it builds a Projection Refusal identity.

   The alternative is to store the Health Connect token. That is simpler, but accepts rare silent loss.
2. **Snapshots require the full-history permission.** Without `PERMISSION_READ_HEALTH_DATA_HISTORY`, Health Connect silently drops records that start more than 30 days before the first permission grant, with no error, so a snapshot would look complete while missing years. ADR 0048 requires unchanged full-history coverage before a Reconciliation Absence counts, and Wire Protocol Version 1 has no field to report partial coverage. The reader therefore requires the history permission both before and after each read (R3), and the caller reports `not-permitted` with reason `permission-missing` until it is granted. If the Operator's phone lacks the history feature, no snapshot can run until it is available; physical-device qualification checks this. The alternative is to read 30-day-limited history and add coverage reporting to the protocol later.
3. **The widest time range.** The SDK rejects a range with neither bound. With an open start it reads from 1970, and with an open end it reads to one day past now, which would silently skip future-dated records such as planned exercise sessions. The reader therefore reads every start time Health Connect can store (R4). A record whose year the Source Projection cannot represent refuses its page visibly instead of being skipped.
4. **1,000 records per page.** This is Health Connect's own default; its maximum is 5,000. The limit is a constant in this version. Positions do not depend on it, so a later version can change it without invalidating saved snapshots. Page memory for large series records and exercise routes is measured during physical-device qualification.
5. **Read failures retry rather than block.** Every non-permission error is a `read-failure`, which the caller reports as `failed` with reason `read-failed` and retries on the next eligible run (ADR 0010). That includes the deterministic ones: an ordering violation and an unpassable equal-time group. Both stay visible on every run without a new Sync Checkpoint reason. The alternative is to block the type for deterministic failures, which needs a new reason and a Wire Protocol change.
6. **One reader for both snapshot kinds.** Initial and Reconciliation Snapshots read identically, so the snapshot kind is not an input. The caller attaches the kind when staging.

## Ground rules

- Kotlin JVM tests construct SDK records through the SDK's constructors with synthetic values and identifiers, and run with no Android device, emulator, or Context. The module enables `unitTests.returnDefaultValues` if the tests need an `android.os.RemoteException`.
- The synthetic fake reproduces the AOSP read semantics this pack relies on:
  - records ordered by start time, then insertion order;
  - a time range inclusive of its start and exclusive of its end;
  - the record limit, with a next-page token whenever more records match;
  - the granted permission set, including filtering to 30 days before the first grant when the history permission is absent;
  - scripted mutations and exceptions between and during calls.
- The fake rejects any page token, so a reader that relies on one fails.
- Not mechanically provable here; the approver confirms these in implementation review:
  - the reader never writes to Health Connect, requests permissions, or reads anything besides permissions and records;
  - Health Connect's real ordering, range, and history behavior matches the fake, which physical-device qualification checks on the Operator's phone.

## Freeze prerequisites

The Operator approves this draft. The executable pack then adds:
- the Kotlin acceptance suite and its synthetic fake;
- the Python contract suite;
- the runner `tests/acceptance/run_read_snapshot_page_1.sh`;
- the Interface stubs, including the seam's permission read and the existing fake's implementation of it.

It also obtains the implementer's read-only implementability review, shows genuine RED, and presents the binding for the approver's freeze decision.
