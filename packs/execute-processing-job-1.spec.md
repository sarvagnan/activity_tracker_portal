# Draft acceptance pack — `execute-processing-job@1`

**Status:** Draft approved by the Operator on 2026-11-25, including the 13 decisions below as recommended; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `execute-processing-job` Governed Scope contract, version 1, in `governance/scopes/execute-processing-job.1.json`. It depends on the published `interpret-source-projection@1` and `operational-store@4`, and on the four published processors `interpret-steps@1`, `interpret-heart-rate@1`, `interpret-sleep-session@1`, and `interpret-weight@1` (Decision 3).

## What this unit does

Phase 4 already has every piece except the one that joins them:
- `operational-store@4` queues Processing Jobs, leases them with a fence, and records outcomes and failures;
- `interpret-source-projection@1` turns one projection into exactly one outcome, or raises a named failure;
- the four processors supply the type-specific bodies;
- the `portal-worker` claims a job and hands its `Lease` to a handler, which nothing yet provides.

This unit is that handler. For one lease it:
1. reads, in one read view, the job and everything `interpret` needs about the leased projection;
2. picks the processor registered for the job's processor version;
3. interprets the projection;
4. commits exactly one result under the lease's fence: the outcome; a transient failure with a retry time; or a deterministic failed job.

If the lease was lost to a later claim, or the store cannot write, it commits nothing.

## What it does not do

- It does not decide outcomes, build headers, or judge bodies. `interpret-source-projection@1` and the processors do.
- It does not queue, claim, expire, or bound jobs, and does not key outcomes. `operational-store@4` does.
- It does not index, reconcile, discover, or enqueue work, or schedule claims. The `portal-worker` does.
- It does not select current results. Processor Generations and current-view selection arrive with `operational-store@6` (Decision 11). Each outcome is recorded under the job's processor version only, as `operational-store@4` Decision 9 intends. Nothing publishes before a cutover (ADR 0045).
- It reads no recorded policy. Policy records arrive with `operational-store@5`, which can only be specified after this unit publishes (Decision 10).
- It offers no manual retry and no Operations view.

## Interface

The module `activity_tracker_portal.execute_processing_job` provides:

**Registry (Decision 2).**
- `PROCESSORS`: a read-only mapping from processor version to processor:
  - `"interpret-steps@1"`, `"interpret-heart-rate@1"`, `"interpret-sleep-session@1"`, and `"interpret-weight@1"` map to each module's published `PROCESSOR`;
  - `COMMON_VERSION` (`"interpret-source-projection@1"`) maps to `None`, so `interpret` returns Unmodeled.
- `PROCESSOR_VERSIONS`: a read-only mapping from each of the 41 `projection/v1` record types to the processor version its jobs run under. `steps`, `heart-rate`, `sleep-session`, and `weight` map to their processors; the other 37 map to `COMMON_VERSION`. Every value is a key of `PROCESSORS`. The worker's discovery map should equal it (Decision 1).

**Retry.**
- `RetryPolicy(base_seconds=60, max_seconds=3600)`: a frozen dataclass. `delay(attempt)` returns `min(base_seconds × 2^(attempt − 1), max_seconds)` seconds as a `timedelta`, for attempt 1 and up (Decision 8).

**Result.**
- `ExecutionResult(job_id, fence, disposition, outcome_kind, reason, retry_at)`: a frozen dataclass.
  - `disposition` is `"outcome-recorded"`, `"retry-scheduled"`, `"job-failed"`, `"already-recorded"`, or `"lease-lost"`.
  - `outcome_kind` is the store's outcome kind for `"outcome-recorded"`, and `None` otherwise.
  - `reason` is the outcome's reason (`None` for a revision), or the job's failure reason.
  - `retry_at` is the time the job may run again for `"retry-scheduled"`, and `None` otherwise.

**Operations.**
- `execute_processing_job(store, lease, clock, *, processors=PROCESSORS, policies=(), retry=RetryPolicy()) -> ExecutionResult`. `store` is an `operational-store@4` `OperationalStore`, or the worker's serialized wrapper of one. `clock` returns a timezone-aware `datetime`. `policies` is the Fallback Timezone Policy timeline passed to `interpret`.
- `make_job_handler(writer, *, clock=<UTC wall clock>) -> Callable[[Lease], ExecutionResult]`. The handler runs `execute_processing_job(store, lease, clock)` through `writer.run`, with the default registry, no policies, and the default retry policy.

**Failures.** The unit defines no exception type of its own. When nothing can be written it raises the store's `StoreRefusal` or `StoreRetryable`, so the worker's existing handling and allowlisted logging apply (Decision 13):
- `StoreRefusal("processing-job-unknown")` and `StoreRefusal("timestamp-invalid")`;
- `StoreRetryable("store-busy")` and `StoreRetryable("storage-reserve-exhausted")`.

**Worker seam.** The worker's `Worker(writer, settings, handler=...)` constructor accepts the handler directly: `Worker(writer, settings, handler=make_job_handler(writer))`. The `portal-worker` command cannot yet do this, because its `job_handler` setting names a callable that receives only a `Lease` and no store (Decision 1).

## Requirements

### R1 — Governed Scope contract

The contract has the shared shape, kind `integration`, and version 1. It depends on `interpret-source-projection@1`, `operational-store@4`, and the four processors at version 1. Its named failures are `processing-job-unknown` and `timestamp-invalid`, which are invariant refusals, and `store-busy` and `storage-reserve-exhausted`, which are retryable. Its two observable effects, `processing-outcome-write` and `processing-failure-write`, match the effects declaration. It does not contradict the scope-map row: the row's dependencies are among the contract's.

Proposed tests: `test_contract_shape_and_dependencies`, `test_scope_map_row_does_not_contradict_contract`, `test_effects_declaration_matches_contract`.

### R2 — Registry

- `PROCESSORS` has exactly the five keys above. Each processor's `processor_version` equals its key, and each is the published module's `PROCESSOR` object.
- `PROCESSOR_VERSIONS` has exactly the record types of `projection/v1/schemas`. Its four modeled entries name their processors and the rest name `COMMON_VERSION`. Every value is a key of `PROCESSORS`, and each modeled processor's `record_type` equals its key.
- Neither mapping can be changed.

Proposed tests: `test_registry_maps_each_version_to_its_processor`, `test_every_projection_type_has_a_processor_version`, `test_registry_is_read_only`.

### R3 — The input comes from the leased evidence

In one read view, the unit reads:
- the job and its attempt history;
- the Source Record's key;
- the leased evidence's source modification time, projection digest, and archived position;
- the projection-schema version of the archived Ingest Batch at that position (Decision 12);
- the exact projection bytes.

It builds `ProcessingInput` from those values and the lease's record, selection, and evidence identities. It interprets the leased selection even when a newer selection exists, because the store re-queues the job for the newer one at completion (`operational-store@4` R33).

Proposed tests: `test_input_is_built_from_the_leased_evidence`, `test_schema_version_comes_from_the_archived_batch`, `test_leased_selection_is_interpreted_when_newer_evidence_exists`.

### R4 — Each outcome is recorded as interpreted

`interpret`'s outcome becomes one `OutcomeEntry`, completed under the lease's fence:
- a Canonical Revision becomes kind `revision` with its exact header and body bytes, its body version, and the indexed columns from its temporal header: the start and end epoch nanoseconds, local dates, and attributions, with every `end_*` column `None` for an instant;
- Unmodeled becomes kind `unmodeled` with its reason;
- Policy-Pending becomes kind `policy-pending` with its reason and policy kind;
- Quarantine becomes kind `quarantine` with its reason and pointer.

The processor version and policy identity are the outcome's own. The result is `"outcome-recorded"` with the kind and reason. Whatever the store then does to the job (completed, re-queued for a newer selection, or superseded by a retraction or Source Conflict) is the store's rule, not this unit's.

Proposed tests: `test_revision_is_recorded_with_its_header_columns`, `test_each_non_revision_outcome_is_recorded`, `test_recorded_bytes_equal_the_interpreted_bytes`, `test_newer_selection_requeues_after_completion`, `test_retraction_after_lease_supersedes_after_completion`, and one per processor, `test_steps_job_records_a_revision`, `test_heart_rate_job_records_a_revision`, `test_sleep_session_job_records_a_revision`, `test_weight_job_records_a_revision`.

### R5 — Unmodeled record types

A job under `COMMON_VERSION` is interpreted with no processor. A schema-valid projection records Unmodeled `record-type-not-modeled` under `COMMON_VERSION`. A schema-invalid one records Quarantine `projection-schema-invalid` under the same version. So every archived record type reaches a terminal outcome (Decision 2).

Proposed tests: `test_unmodeled_type_records_unmodeled_under_the_common_version`, `test_invalid_unmodeled_projection_is_quarantined`.

### R6 — Deterministic failures

Each of these fails the job with `fail_processing_job(kind="deterministic")` and the reason shown. No outcome is recorded, and the store keeps the job failed (`operational-store@4` R35).
- `processor-unavailable`: the job's processor version is not in the registry (Decision 4).
- `processor-record-type-mismatch`: the registered processor's record type differs from the Source Record's (Decision 4).
- `processing-evidence-missing`: the Source Record, its leased evidence, its archived batch, or its projection bytes cannot be read.
- `processing-input-invalid`, `projection-digest-mismatch`, `projection-identity-mismatch`, `processor-invariant-violated`: `interpret` raised that named failure (Decision 5).
- `outcome-conflict`, `outcome-invalid`: the store refused the completion with that name (Decision 5).

The result is `"job-failed"` with the reason.

Proposed tests: `test_interpret_failures_fail_the_job`, `test_unregistered_processor_version_fails_the_job`, `test_processor_record_type_mismatch_fails_the_job`, `test_missing_evidence_fails_the_job`, `test_outcome_conflict_fails_the_job`, `test_failed_job_is_not_retried`.

### R7 — Transient failures and backoff

- Any other exception from `interpret` fails the attempt with `fail_processing_job(kind="transient")`, the reason `interpretation-unexpected-failure`, and `retry_at` equal to the write time plus `retry.delay(attempts)`, where `attempts` is the job's attempts used, this claim included (Decision 6).
- If the job has attempts left, the result is `"retry-scheduled"` with that time. Otherwise the store fails it with `retries-exhausted`, and the result is `"job-failed"` with that reason.
- With the defaults, the delays are 60 s, 120 s, 240 s, and so on, never above 3,600 s.
- A `BaseException` that is not an `Exception`, such as `KeyboardInterrupt`, propagates and writes nothing.

Proposed tests: `test_unexpected_interpretation_error_schedules_a_retry`, `test_backoff_doubles_and_is_capped`, `test_retries_are_bounded_by_the_job`, `test_interrupt_propagates_and_writes_nothing`.

### R8 — The store cannot write

When `complete_processing_job` or `fail_processing_job` raises `store-busy` or `storage-reserve-exhausted`, the unit:
- raises that `StoreRetryable` unchanged;
- does not try another write, so nothing is committed;
- leaves the lease to expire (Decision 7).

A later claim then takes the job with a higher fence. Executing that lease records the same outcome once: the outcome key makes the repeat return the first outcome.

Proposed tests: `test_busy_store_writes_nothing_and_propagates`, `test_reserve_exhaustion_writes_nothing_and_propagates`, `test_reclaimed_job_records_the_same_outcome_once`.

### R9 — Fencing, no renewal, one write

- Every write uses the lease's job and fence. The unit never calls `renew_processing_lease` (Decision 9).
- A lease that expired but was not reclaimed still completes, as `operational-store@4` R32 and Decision 4 allow.
- A lease whose job a later claim took writes nothing. The result is `"lease-lost"`.
- One execution commits at most one write. A refused completion may be followed by one deterministic failure (R6), never by anything else.

Proposed tests: `test_expired_but_held_lease_completes`, `test_reclaimed_lease_is_lost_and_writes_nothing`, `test_one_write_per_execution`, `test_lease_is_never_renewed`.

### R10 — Repeats and crash recovery

- Before interpreting, the unit checks the job's attempt at the lease's fence. If that attempt is already closed as completed, transient-failure, or deterministic-failure, it writes nothing and returns `"already-recorded"`. If the job's fence is higher than the lease's, it writes nothing and returns `"lease-lost"`.
- A write refused with `lease-not-held` is resolved the same way, from the attempt history, and is never retried.
- A crash before the write leaves the lease to expire. The reclaim re-interprets, and the identical outcome is not duplicated.
- A crash after the write leaves the job closed, so nothing is redone.

Proposed tests: `test_repeat_after_commit_is_already_recorded`, `test_repeat_after_reclaim_is_lease_lost`, `test_crash_before_write_recovers_by_reclaim`, `test_crash_after_write_needs_no_recovery`.

### R11 — Policies

- `make_job_handler` passes an empty Fallback Timezone Policy timeline. A projection with an endpoint that has no source offset records Policy-Pending `fallback-timezone-policy-missing` with policy kind `fallback-timezone` (Decision 10).
- `execute_processing_job` passes the given timeline to `interpret` unchanged. The outcome's policy identity then reaches the outcome key, so a later policy produces a second outcome for the same selection rather than a conflict.
- No Source Selection Policy is read. It governs aggregates, not interpretation (ADR 0026, 0041).

Proposed tests: `test_production_handler_uses_no_policy`, `test_supplied_policies_reach_the_outcome_key`, `test_offsetless_projection_is_policy_pending`.

### R12 — Worker seam

- `make_job_handler(writer, clock=...)` returns a callable that takes a `Lease`, runs the execution through `writer.run`, and returns its result.
- Inside `Worker` with a real `PortalWriter` in the worker role over a temporary store, archived synthetic batches of all four modeled types and one unmodeled type reach a recorded outcome within bounded cycles, with each job completed.
- A `StoreRetryable` from the handler reaches the worker unchanged, so the worker backs off.

Proposed tests: `test_handler_executes_through_the_writer`, `test_worker_drives_archived_batches_to_terminal_outcomes`, `test_handler_passes_store_retryables_to_the_worker`.

### R13 — Time

The unit reads time only from `clock`, once per execution, just before its first write attempt. A refused completion and the deterministic failure that follows it share that time. An execution that returns before any write attempt, such as `"already-recorded"` found before interpreting, reads no time. A naive time raises `timestamp-invalid` and writes nothing. No other clock and no host timezone database is read.

Proposed tests: `test_time_comes_only_from_the_clock`, `test_naive_clock_is_refused`.

### R14 — Health-data-free results, failures, and logs

- `ExecutionResult`'s `repr` and `str` carry no projection, header, or body bytes, field values, origin, or Source Identity.
- Every reason this unit records is one of the allowlisted names in R6 and R7.
- Raised failures carry only the store's failure name. They have no `__cause__`, and no exception text from a processor or from `interpret` is carried in a reason, a result, or a traceback.
- The unit writes no log records. The worker logs only stage and failure names.

Proposed tests: `test_results_are_health_data_free`, `test_recorded_reasons_are_allowlisted`, `test_raised_failures_carry_no_health_data`, `test_unit_emits_no_logs`.

## Decisions for the Operator

1. **How the unit plugs into the worker.** The worker's `job_handler` setting names a callable that receives only a `Lease`. That callable has no safe store access: opening a second worker-role writer fails on the worker's own lock, and an ingress-role writer fails while `portal serve` runs. The worker's `Worker` class already accepts a handler directly, so this pack tests the unit through it (R12). Only the `portal-worker` command needs a change.
   - **Recommended:** a small enabling task, after Gate A and before this unit's implementation merges, makes `portal-worker` wire the governed handler: `make_job_handler(writer)` for claims, and `PROCESSOR_VERSIONS` for discovery unless the configuration overrides it. It keeps the `Worker(..., handler=...)` constructor that R12 uses.
   - Alternative: `job_handler` names a factory called with the worker's writer and clock. This keeps a pluggable seam but makes every deployment configure 41 record types by hand.
   - Alternative: the implementer changes `worker.py` in this unit's branch. It is unbound code, but the change would be reviewed as part of a governed unit rather than as enabling work.
2. **This unit owns the registry, and it covers every record type.** The four modeled types map to their processors. The other 37 map to `COMMON_VERSION`, so they record Unmodeled and reach a terminal outcome. Phase 6 completion needs a terminal outcome for every record on a page.
   - Alternative: register only the four modeled types. Other records would never get a job or an outcome, so no snapshot page containing them could complete.
   - Alternative: leave the map in deployment configuration. Ungoverned configuration would then decide which processor applies.
3. **The four processors become declared dependencies.** The registry imports them, so `vouch specify` would list `interpret-steps`, `interpret-heart-rate`, `interpret-sleep-session`, and `interpret-weight` beside `interpret-source-projection` and `operational-store`. The scope-map graph already draws them into this unit, but the row's "Depends on" column lists only the latter two. The drafter would update that column after approval.
   - Alternative: declare only the two dependencies in the row and import the processors anyway. Vouch would then not know that a processor change affects this unit.
   - Alternative: the registry is injected by the caller and lives elsewhere. That moves Decision 2's ownership out of any governed unit.
4. **A job the registry cannot serve fails deterministically.** An unregistered processor version gives `processor-unavailable`; a processor for another record type gives `processor-record-type-mismatch`. Both are configuration or deployment faults that waiting will not fix. Recovery is a manual retry once the code or configuration is corrected.
   - Alternative: treat them as transient. The job retries a few times and then fails with `retries-exhausted`, which hides the cause.
   - Alternative: write nothing and let the lease expire. That burns attempts slowly and records only `lease-expired-retries-exhausted`.
5. **Integrity and processor defects fail deterministically.** All four of `interpret`'s named failures, including `processor-invariant-violated`, are deterministic: the same input gives the same failure (ADR 0021; `interpret-source-projection@1` Decision 9). So are the store's `outcome-conflict` and `outcome-invalid`, which mean a processor changed without a new version, or a mapping defect here. Each is recorded under its own name.
   - Alternative: treat `processor-invariant-violated` as transient. Retrying identical input repeats the same failure, and the final reason becomes `retries-exhausted`.
6. **Other exceptions during interpretation are transient and bounded.** An exception `interpret` does not name, such as `MemoryError`, may not recur. It is recorded as `interpretation-unexpected-failure` with a backoff, and the job's attempt bound caps the cost.
   - Alternative: deterministic. One passing fault would fail the record until a manual retry.
   - Alternative: let it propagate. The worker logs it and the lease expires, so it retries only after the lease time, and the record says only `lease-expired-retries-exhausted`.
7. **When the store cannot write, nothing is written.** A busy store or an exhausted reserve would refuse a failure write for the same reason. The unit passes the store's retryable failure to the worker, which backs off, and the lease expires. A later claim counts as an attempt, so a persistent fault still ends within the job's bound (`operational-store@4` Decision 5). Below the reserve, claims are refused too, so jobs do not burn attempts while space is short.
   - Alternative: retry the write inside the handler. This holds the lease longer and needs its own bound.
   - Alternative: try a transient-failure write first. It almost always fails for the same reason.
8. **Backoff of 60 s, doubling, capped at one hour, with no jitter.** The attempt bound stays the job's own `max_attempts`, which the worker sets at enqueue (default 3). With the defaults a job retries after 60 s and then 120 s. Jitter spreads many workers apart; there is only one.
   - Alternative: a fixed delay, for example 60 s. It is simpler but retries a failing record often if the bound is raised.
   - Alternative: other base or cap values.
9. **No lease renewal.** One job is one projection, bounded by the advertised message size, and interpreting it takes milliseconds. A held lease completes even after expiry until another claim takes it (`operational-store@4` Decision 4). Only one worker process claims, and it runs handlers one at a time, so nothing can take the job while it runs. Every write still carries the fence.
   - Alternative: renew before writing when less than half the lease remains. This adds a write per slow job and protects only against a second worker, which the worker lock prevents.
10. **No recorded policies yet.** Policy records arrive with `operational-store@5`, which can be specified only after this unit publishes. Until then, the production handler passes an empty Fallback Timezone Policy timeline, so any record with an endpoint lacking a source offset is Policy-Pending, as ADR 0025 requires before the Operator confirms a policy. When `set-fallback-timezone-policy` and `@5` arrive, a later version of this unit reads recorded policies, and `@5` enqueues the affected reprocessing; outcomes never block new jobs (`operational-store@4` R30). The core operation already takes a timeline, so tests show that policies flow into the outcome key.
    - Alternative: a fallback timezone in the worker configuration. The Operator would not have confirmed it through the governed setter, it would have no recorded history, and `@5` would have to adopt its identities.
    - Alternative: wait for `@5`. It cannot be specified before this unit publishes, so this is a cycle.
11. **No Processor Generation yet.** Generations arrive with `operational-store@6`, which also waits for this unit. A job already names its processor version, so which generation it serves is decided when it is enqueued, not here. This version records outcomes per processor version only, and selects nothing as current. A later version links work to Cutover Targets and Candidate Builds once `@6` defines them. The scope-map row describes the complete unit, as the `operational-store` row does across its versions.
    - Alternative: a provisional generation selector in this unit. `@6` would have to replace it, as `operational-store@4` Decision 9 found.
    - Alternative: wait for `@6`. That is also a cycle.
12. **The projection-schema version comes from the archived batch.** `interpret` checks that the bytes agree with the batch's version, so it must be the batch's. The only published read that returns it is `archived_message`, which also loads the whole batch envelope and its items. One job therefore reads one batch, at most the advertised message size. A narrow evidence read can come with a later store version.
    - Alternative: take the version from the projection bytes. That makes `interpret`'s identity check compare the bytes with themselves.
    - Alternative: wait for a narrow read. No store successor can be specified before this unit publishes.
13. **Definite results are returned; the store's failures pass through.** A lost lease or an already-recorded result is an expected outcome, so it is returned. When nothing could be written, the store's own `StoreRefusal` or `StoreRetryable` is raised unchanged, which the worker already handles and logs by name. There is no new exception type.
    - Alternative: this unit's own exception types, as `reconcile-source-record@1` has. The worker would then log them as unexpected failures and would not back off, until the worker is changed.

## Ground rules

- **Data.** Python only, standard library plus the pinned runtime. Tests use real temporary Operational Store databases, built through the published store and worker-indexing operations from synthetic `projection/v1` vectors and hand-built synthetic projections. Every processor is the real published one, except where a test registers a deliberately faulty synthetic processor.
- **Faults.** A delegating store injects `store-busy`, `storage-reserve-exhausted`, `lease-not-held`, a reclaim between the read and the write, and a crash after a commit. A test clock controls every time.
- **Database checks.** Every "writes nothing" assertion compares the whole database. Every "one write" assertion replays the same store call on a copy taken before the execution and requires identical databases, as `reconcile-source-record@1` does.
- **Markers.** A health-data marker in projection values, and identity markers in origins and Source Identities, show that results, raised failures, tracebacks, recorded reasons, and logs carry none of them. A synthetic processor raises an exception whose text carries the marker.
- **Unbound inputs, reviewed rather than frozen.** The binding is the contract, the spec, the suite, the runner, and the effects declaration. These stay outside it:
  - `docs/governance/scope-map.md`, read by R1's scope-map test;
  - the published code, contracts, and packs of every dependency, which their own bindings freeze;
  - `src/activity_tracker_portal/worker.py` and `ingress.py`, enabling code whose `Worker(writer, settings, handler=...)` constructor and `PortalWriter` R12 uses. A later worker change must keep them;
  - the execution environment: `uv`, `pyproject.toml`, `uv.lock`, the managed Python and SQLite;
  - `tests/governance/test_execute_processing_job_contract.py`, which the governance suite runs and this runner does not.
- **Not mechanically provable here**, so the approver confirms these in implementation review:
  - one read view and at most one successful write per execution;
  - only published store operations are used: no SQL, no private store helpers, and never the discovery reads `items_awaiting_index` or `selections_awaiting_processing` (DEV-os4-01);
  - no clock other than the injected one, no host timezone database, no logging;
  - no header or body bytes are parsed; indexed columns come from `interpret`'s returned header;
  - the registry matches the published processor units.

## Freeze prerequisites

The executable pack adds:
- the suite `tests/acceptance/test_execute_processing_job_1.py`: 53 tests, one per proposed name in R1–R14, grouped by requirement;
- the runner `tests/acceptance/run_execute_processing_job_1.sh`, which runs only this suite. Upstream suites run under their own bound runners;
- no fixture files. Each test builds its store through the published store operations, from the `projection/v1` positive vectors with marker strings put into their values, origin, and Source Identity;
- the Interface stub `src/activity_tracker_portal/execute_processing_job.py`:
  - real `PROCESSORS` (five keys) and `PROCESSOR_VERSIONS` (all 41 record types), each a `MappingProxyType`, because they are the registry this draft approves;
  - real frozen `ExecutionResult` and `RetryPolicy` dataclasses, with `RetryPolicy.delay` raising `NotImplementedError("execute-processing-job@1")`;
  - `execute_processing_job` and `make_job_handler` (default clock: the UTC wall clock) raising the same `NotImplementedError`.

**RED, against the stub.** 6 tests pass: the three R1 tests and the three R2 tests, which read only the contract, the scope map, the effects declaration, `projection/v1`, and the registry. 47 tests fail, and each failure is the stub's `NotImplementedError`:
- 42 at `execute_processing_job`;
- 4 at `make_job_handler`: `test_production_handler_uses_no_policy` and the three R12 tests;
- 1 at `RetryPolicy.delay`: `test_backoff_doubles_and_is_capped`.

No failure has another cause. unittest prints `FAILED (errors=131)` because it counts subtests separately. `run_operational_store_4.sh`, `run_interpret_source_projection_1.sh`, and the four processor runners pass, as do `tests/unit` and `tests/governance`.

**Reference and mutants.** A throwaway reference implementation, kept out of the repository, passes all 53 tests. 48 incorrect implementations were tried, and each fails at least one test. They include every case in the list below, and also: no cap on the backoff, a fixed delay, and an off-by-one delay; a retry write after a busy store; a write under the job's current fence instead of the lease's; a raise instead of resolving `lease-not-held`; a transient failure written after a database error; a schema version hard-coded to 1; interpreting the evidence head or the latest selection instead of the leased one; a mutable registry; a handler that bypasses `writer.run`; a naive default clock; an interrupt caught as transient; an unknown job reported as `lease-lost`; and a store write made while an interpretation exception is being handled. No mutant survived.

The draft's required list:
- recording the wrong kind, version, policy identity, or indexed column;
- a transient where a deterministic failure belongs, and the reverse;
- a retry after a busy or reserve failure;
- a write after a lost lease;
- a renewal;
- a second write after a refused completion;
- a schema version taken from the bytes;
- a missing Unmodeled registration;
- policies dropped or invented;
- a clock read per write, or before an execution that writes nothing;
- exception text carried into a reason.

**Refinements the tests pin beyond the approved text.** The first three fill gaps in the approved text. The approver should confirm them, because each could be read as new semantics:
1. *Flag.* An exception from a store write that is neither `StoreRefusal` nor `StoreRetryable`, such as a database error raised by a trigger, propagates unchanged and is followed by no other write. R9 allows only a deterministic failure after a refused completion; this is not a refusal. The test is in `test_one_write_per_execution`.
2. *Flag.* A `"retry-scheduled"` result's `reason` is `interpretation-unexpected-failure`, the reason recorded on the attempt. The job itself records no failure while it is re-queued, so "the job's failure reason" would otherwise be `None`.
3. *Flag.* `"already-recorded"` and `"lease-lost"` results have `outcome_kind`, `reason`, and `retry_at` all `None`.
4. A result always carries the lease's `job_id` and `fence`, including `"lease-lost"`.
5. When the lease's own attempt is already closed, the result is `"already-recorded"` even if a later claim has since taken the job. The closed-attempt check comes before the fence check (R10's order).
6. A claim that takes the job during interpretation is found at the write, through `lease-not-held`. The result is `"lease-lost"`, after one write attempt.
7. Raised failures have no `__cause__`. No exception in their `__context__` chain, and no text of their formatted traceback, carries a marker. In practice the unit must not make its write while handling the exception from `interpret`.
8. `SystemExit` propagates and writes nothing, as `KeyboardInterrupt` does. A `BaseException` during interpretation reads no time.
9. A job under `COMMON_VERSION`, run with a registry that lacks it, fails with `processor-unavailable`.
10. The registry mappings raise `TypeError` on item assignment and deletion, and have no mutating methods.
11. `make_job_handler` makes no call until it is invoked. It then makes one `writer.run` per lease, makes every write inside it, and returns that run's result object. Its default clock is the timezone-aware wall clock.
12. Inside `Worker`, a `StoreRetryable` from the handler is logged as `worker handler retryable: <name>`, and the next wait is the configured `idle_seconds`.
13. The R12 worker test configures discovery with `processor_versions=PROCESSOR_VERSIONS`, because `worker.py` stays unchanged until enabling task 64r. It expects every job completed within three cycles, and no worker warning.

**How faults are injected, for the implementer's review.** The store cannot hold a missing Source Record, evidence row, batch, or projection, or a stored digest that does not match the bytes. Tests therefore replace published reads in a delegating store:
- `source_record`, for the record, its evidence, and its digest;
- `archived_message` and `archived_items`, for the batch and its schema version;
- `projection_bytes`, with the batch's items emptied too, for the bytes.

An implementation that reads this evidence by any other published read would escape these faults. None exists for these values. A transient fault comes from a synthetic processor whose `supported_schema_versions` membership test raises. `interpret` reads that attribute outside its processor boundary, and it turns any exception raised from `interpret_body` into `processor-invariant-violated`.

The implementer reviews the pack read-only for tests that a correct implementation could not pass, and those findings are resolved before freeze. The approver then confirms at Gate B that the pack carries this draft, that RED is genuine, that the effects declaration matches the contract, that the refinements above are acceptable, and that the residuals above are complete.
