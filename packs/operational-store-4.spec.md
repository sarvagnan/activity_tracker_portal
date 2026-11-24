# Draft acceptance pack — `operational-store@4`

**Status:** Draft approved by the Operator on 2026-11-24, including the 17 decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `operational-store` Governed Scope contract, version 4, in `governance/scopes/operational-store.4.json`. It is the additive successor to the published `operational-store@3`, which it supersedes.

`@3` indexes archived items into Source Records and records which evidence is current. Nothing yet turns a current projection into a Canonical Revision. `@4` adds the durable side of processing:
- **Processing Jobs** with expiring, fenced leases and bounded retry (ADR 0021);
- **outcomes** recorded atomically with a job's completion: Canonical Revisions (ADR 0022, 0024), and Unmodeled, Policy-Pending, and Quarantine outcomes;
- **failed jobs** and their attempt history (ADR 0021, 0023);
- **item quarantine** for archived bytes that cannot be indexed at all.

`interpret-source-projection@1` decides the outcome; this store only records it. `execute-processing-job` and the supervised `portal-worker` are its consumers. On 2026-11-24 the Operator split processing into `@3`, `@4`, and `@5`. Policy records and summary-refresh enqueueing belong to `@5`; Processor Generations and current-view selection to `@6`.

Each outcome names the selection it interprets. `@3` links that selection to its evidence, archived position, and delivery cycle. Phase 6 can therefore count terminal outcomes per cycle page without a new link (Decision 10).

## Interface

Everything in `operational-store@3`'s Interface is unchanged, except that `FAILURES` gains this version's names and `MIGRATIONS` gains migration 3. `pending_items` and `IndexCounts` keep their exact meaning.

The module `activity_tracker_portal.operational_store` adds these frozen dataclasses:
- `RevisionEntry`: the caller's Canonical Revision, which the store never parses.
  - `header_bytes` and `body_bytes`: the exact canonical bytes;
  - `body_version`: the record type's body version;
  - indexed header columns: `start_epoch_ns`, `end_epoch_ns`, `start_local_date`, `end_local_date`, `start_attribution`, and `end_attribution`. The `end_*` columns are all `None` for an instant.
- `OutcomeEntry(kind, processor_version, policy_identity, reason, pointer, policy_kind, revision)`. `kind` is `"revision"`, `"unmodeled"`, `"policy-pending"`, or `"quarantine"`.
- `StoredRevision`: a `RevisionEntry` plus the store's `header_sha256` and `body_sha256`.
- `ProcessingOutcome`: `outcome_id`, `record_id`, `selection_id`, `evidence_id`, `processor_version`, `policy_identity`, `kind`, `reason`, `pointer`, `policy_kind`, `revision` (a `StoredRevision` or `None`), `job_id`, `fence`, and `recorded_at`. A revision's identity is its `outcome_id`.
- `ProcessingAttempt(fence, owner, claimed_at, expires_at, finished_at, result, reason)`. `result` is `None` while open, or `"completed"`, `"transient-failure"`, `"deterministic-failure"`, `"lease-expired"`, or `"superseded"`.
- `ProcessingJob`: `job_id`, `record_id`, `processor_version`, `selection_id` (its current target), `state`, `max_attempts`, `attempts`, `not_before`, `fence`, `lease_owner`, `lease_expires_at`, `failure`, `enqueued_at`, `closed_at`, and `history` (its attempts in order). `state` is `"queued"`, `"leased"`, `"completed"`, `"failed"`, or `"superseded"`.
- `Lease(job_id, fence, owner, record_id, selection_id, evidence_id, processor_version, expires_at)`.
- `ItemQuarantine(position, reason, pointer, recorded_at)`.
- `AwaitingProcessing(record_id, selection_id, processor_version)`.
- `ProcessingCounts`: `jobs_ready`, `jobs_delayed`, `jobs_leased`, `jobs_lease_expired`, `jobs_completed`, `jobs_failed`, `jobs_superseded`, `revisions`, `unmodeled`, `policy_pending`, `quarantined`, `quarantined_items`, and `items_awaiting_index`.

It adds these operations:
- `Store.enqueue_processing(record_id, selection_id, processor_version, max_attempts, at) -> ProcessingJob`
- `Store.claim_processing_job(owner, lease_seconds, at) -> Lease | None`
- `Store.renew_processing_lease(job_id, fence, lease_seconds, at) -> Lease`
- `Store.complete_processing_job(job_id, fence, outcome, at) -> ProcessingOutcome`
- `Store.fail_processing_job(job_id, fence, kind, reason, retry_at, at) -> ProcessingJob`
- `Store.quarantine_item(position, reason, pointer, at) -> ItemQuarantine`
- `StoreView.processing_job(job_id) -> ProcessingJob | None`
- `StoreView.processing_jobs(record_id) -> tuple[ProcessingJob, ...]`
- `StoreView.processing_outcomes(record_id) -> tuple[ProcessingOutcome, ...]`
- `StoreView.latest_outcome(record_id, processor_version) -> ProcessingOutcome | None`
- `StoreView.selections_awaiting_processing(processor_versions, limit) -> tuple[AwaitingProcessing, ...]`
- `StoreView.items_awaiting_index(limit) -> tuple[PendingItem, ...]`
- `StoreView.item_quarantine(position) -> ItemQuarantine | None`
- `StoreView.projection_bytes(evidence_id) -> bytes | None`
- `StoreView.processing_counts(at) -> ProcessingCounts`

Every time argument is timezone-aware and comes from the caller. The store never reads a clock.

## Requirements

`operational-store@1`, `@2`, and `@3`'s requirements R1–R27 carry over, as rebound on 2026-11-24. The `@4` runner re-runs `@3`'s bound runner unchanged, so every earlier suite must still pass exactly.

### R28 — Governed Scope contract, version 4

The `@4` contract keeps `@3`'s inputs and modes, observable effect, and empty dependency list.
- Its named failures begin with `@3`'s, in order and with the same outcome classes, followed by the eleven new invariant refusals.
- Its outputs keep `@3`'s names and add `processing_job`, `processing_lease`, `processing_outcome`, and `item_quarantine`.
- Its `owns` list is `@3`'s, unchanged, with the processing entries appended.
- It does not contradict the `operational-store` scope-map row.

Proposed tests: `test_contract_4_extends_version_3`, `test_scope_map_row_does_not_contradict_contract_4`, `test_failures_match_contract_4`.

### R29 — Migration 3 is additive

- `MIGRATIONS` is `@3`'s migrations followed by migration 3. Migration 3 only creates the processing tables and their indexes. It inserts no row and alters no earlier table.
- A new database records migrations 1, 2, and 3 with their checksums and `user_version` 3.
- A database at migration 2, such as one written by `@3`, refuses with `store-migration-required` and is left unchanged.
- Brought to migration 3 with no backfill, as R17 requires, every earlier row is unchanged and every new table is empty. It then opens, and:
  - every Source Record whose latest selection is a projection awaits processing;
  - every unindexed item awaits indexing.

The test uses a new fixture, a dump of a synthetic database written by the published `@3` module with projection, retraction, and conflict selections.

Proposed tests: `test_new_database_records_migrations_1_to_3`, `test_version_3_database_requires_migration_and_is_untouched`, `test_migrated_version_3_index_awaits_processing`.

### R30 — Enqueueing a Processing Job

`enqueue_processing` creates a queued job for one Source Record and processor version, or returns the open one.
- **Refusals,** each leaving the database unchanged:
  - `timestamp-invalid`;
  - `processing-request-invalid` when `max_attempts` is not an integer from 1 to 100;
  - `processor-version-invalid` when the version is not of the form `<scope>@<version>`, such as `interpret-steps@1`;
  - `source-record-unknown`;
  - `selection-unknown` when the selection does not exist or belongs to another record;
  - `selection-not-latest` when it is not the record's latest selection;
  - `selection-not-projection` when it is a retraction or Source Conflict;
  - `storage-reserve-exhausted`, only when a job would be written.
- **One open job.** While a queued or leased job exists for the record and version, enqueueing returns it and writes nothing.
- **New job.** Otherwise the new job targets the named selection, with `not_before` equal to `at` and no attempts.
- **Outcomes do not block.** An existing outcome for the selection does not refuse a new job. Reprocessing after a policy change, and manual retry, both rely on this.

Proposed tests: `test_enqueue_creates_a_queued_job`, `test_open_job_is_returned_unchanged`, `test_enqueue_refusals`, `test_enqueue_after_completion_creates_a_new_job`.

### R31 — Claiming and expiring leases

`claim_processing_job` takes the eligible job with the lowest identity, in one transaction.
- **Eligible jobs:** queued jobs whose `not_before` is at or before `at`, and leased jobs whose lease expired at or before `at`.
- **Expired lease.** Its open attempt closes as `lease-expired`. If the job has used all its attempts, it becomes `failed` with `lease-expired-retries-exhausted`, and the claim moves on.
- **Target.** The job is re-targeted to the record's latest selection, so newer evidence coalesces into the open job. If that selection is a retraction or Source Conflict, the job becomes `superseded` and the claim moves on.
- **Lease.** The claim increments the job's fence and its attempts, appends an open attempt, and sets the lease to expire at `at` plus `lease_seconds`. The lease names the target selection and its projection evidence.
- **Nothing eligible.** The claim returns `None` after committing any expiry or supersession it recorded.
- **Refusals:**
  - `timestamp-invalid`;
  - `processing-request-invalid` when the owner is empty or `lease_seconds` is not an integer from 1 to 86,400;
  - `storage-reserve-exhausted` when the claim would write anything.

Proposed tests: `test_claim_takes_the_lowest_eligible_job`, `test_delayed_job_is_not_claimed_early`, `test_expired_lease_is_reclaimed_with_a_higher_fence`, `test_exhausted_expired_job_fails`, `test_claim_retargets_to_the_latest_selection`, `test_retracted_or_conflicted_record_supersedes_the_job`, `test_claim_with_nothing_eligible_returns_none`, `test_two_handles_never_hold_one_job`.

### R32 — Renewing a lease

`renew_processing_lease` sets a held lease to expire at `at` plus `lease_seconds`.
- A lease is held when the job is leased and the fence is its current fence. This is true even after expiry, until another claim takes the job (Decision 4).
- Refusals: `processing-job-unknown`, `lease-not-held`, `processing-request-invalid`, `timestamp-invalid`, and `storage-reserve-exhausted`.

Proposed tests: `test_renew_extends_a_held_lease`, `test_renew_after_reclaim_is_not_held`, `test_expired_but_unclaimed_lease_can_renew`.

### R33 — Completing a job with its outcome

`complete_processing_job` records one outcome for the leased selection and closes the attempt, in one transaction.
- **Outcome key.** An outcome is unique per selection, processor version, and policy identity.
  - With no outcome for the key, the outcome is appended.
  - An identical outcome for the key is not appended again; the existing one is returned.
  - A different outcome for the key is `outcome-conflict`, and nothing changes. Interpretation is deterministic, so this means a defect.
- **Attempt.** The open attempt closes as `completed`.
- **Job.** It depends on the record's latest selection:
  - the leased selection: the job is `completed`;
  - a newer projection selection: the job is re-queued for it, with `not_before` equal to `at` and its attempts reset;
  - a retraction or Source Conflict: the job is `superseded`.

  The outcome stays recorded for the older selection either way.
- **Refusals:** `processing-job-unknown`, `lease-not-held`, `outcome-invalid` (R34), `outcome-conflict`, `timestamp-invalid`, and `storage-reserve-exhausted`. A held lease completes even after expiry, until another claim takes the job.

Proposed tests: `test_completion_records_outcome_and_closes_job`, `test_identical_outcome_is_not_duplicated`, `test_different_outcome_for_the_same_key_conflicts`, `test_newer_selection_requeues_the_job`, `test_retraction_after_lease_supersedes_the_job`, `test_reclaimed_lease_cannot_complete`, `test_each_outcome_kind_is_recorded`.

### R34 — Outcome shape

The store checks shape only. It never parses header or body bytes and never judges an outcome (Decision 7). An outcome is `outcome-invalid` when:
- its kind is unknown, or its processor version differs from the job's;
- its policy identity is empty or not a string;
- a revision is missing for kind `revision`, or present for any other kind;
- a revision has empty header or body bytes, a body version below 1, an end before its start, `end_*` columns that are partly present, an attribution other than `experienced` or `inferred`, or a local date that is not a `date`;
- a `reason` is present for a revision, or missing or not lowercase kebab-case of at most 64 characters for any other kind;
- a quarantine has no pointer, or a pointer that is neither empty nor starts with `/`;
- a Policy-Pending outcome has no kebab-case `policy_kind`, or another kind has one.

The store computes `header_sha256` and `body_sha256`. Outcomes and revisions are never updated or deleted.

Proposed tests: `test_outcome_shapes_are_checked`, `test_revision_digests_are_computed_by_the_store`, `test_outcomes_are_append_only`.

### R35 — Failure, retry, and backoff

`fail_processing_job` closes the held attempt with a failure.
- **Transient.** The attempt closes as `transient-failure` with the caller's reason.
  - If the job has attempts left, it is re-queued with `not_before` equal to `retry_at`. The caller chooses the backoff.
  - Otherwise it becomes `failed` with `retries-exhausted`.
- **Deterministic.** The attempt closes as `deterministic-failure`. The job becomes `failed` with the caller's reason.
- **Refusals:**
  - `processing-job-unknown` and `lease-not-held`;
  - `processing-request-invalid` for an unknown kind, a reason that is not kebab-case of at most 64 characters, a transient failure without a `retry_at` at or after `at`, or a deterministic failure with one;
  - `timestamp-invalid` and `storage-reserve-exhausted`.
- **No automatic retry.** A failed job stays failed. Only a new `enqueue_processing` makes another job, as a manual retry or a corrected processor version does (ADR 0021).

Proposed tests: `test_transient_failure_requeues_after_backoff`, `test_retries_are_bounded`, `test_deterministic_failure_is_terminal`, `test_failure_refusals`, `test_failed_job_is_not_retried_automatically`.

### R36 — Items that cannot be indexed

`quarantine_item` records that one archived projection item cannot yield a Source Record key. `interpret-source-projection@1`'s `decode_envelope` supplies the reason and pointer.
- **Refusals:**
  - `timestamp-invalid`;
  - `processing-request-invalid` for a reason or pointer of the wrong form;
  - `archived-item-unknown`;
  - `index-entry-mismatch` when the position is a retraction;
  - `item-already-indexed`;
  - `item-quarantine-conflict` when it is already quarantined differently;
  - `storage-reserve-exhausted`.
- **Idempotence.** An identical repeat returns the existing quarantine and writes nothing.
- **Awaiting index.** `items_awaiting_index(limit)` returns `pending_items` without quarantined items, in the same order.
- **`@3` unchanged.** `pending_items`, `IndexCounts.unindexed_items`, and `index_items` keep their `@3` meaning. A quarantined item is still pending there.
- **Indexing later.** A quarantined item can still be indexed, for example by a corrected decoder. It then leaves the pending set. Its quarantine stays as history.

Proposed tests: `test_quarantined_item_leaves_items_awaiting_index`, `test_pending_items_still_lists_quarantined_items`, `test_item_quarantine_refusals`, `test_identical_item_quarantine_writes_nothing`, `test_quarantined_item_can_still_be_indexed`.

### R37 — Processing reads

- `processing_job` returns `None` for an unknown job. Otherwise it returns the job with its whole attempt history. `processing_jobs` lists a record's jobs in identity order.
- `processing_outcomes` lists every outcome of a record, under every processor version, in recording order, with revision bytes.
- `latest_outcome(record_id, processor_version)` returns the most recent outcome under that version for the record's latest selection, or `None`. It makes no claim about what is current in Portal views (Decision 9).
- `selections_awaiting_processing(processor_versions, limit)` returns, in ascending record identity, each record whose type is in the mapping and whose latest selection is a projection with:
  - no outcome for that selection and version, under any policy identity;
  - no open job for the record and version;
  - no failed job that last targeted that selection under that version.
- `projection_bytes` returns a projection's archived bytes, or `None` for a retraction or an unknown identity.
- Each read comes from one read transaction and writes nothing.

Proposed tests: `test_job_read_includes_history`, `test_outcomes_span_processor_versions`, `test_latest_outcome_follows_the_latest_selection`, `test_selections_awaiting_processing`, `test_failed_and_open_work_is_not_awaiting`, `test_projection_bytes_by_evidence`, `test_processing_reads_write_nothing`.

### R38 — Health-data-free counts and refusals

- `processing_counts(at)` returns only counts. `at` separates ready from delayed jobs, and held from expired leases.
- Refusal text, and the `repr` of every new type, carry no projection, header, or body bytes and no Source Identity. `StoredRevision` and `ProcessingOutcome` omit their bytes from `repr`.
- Tests use synthetic markers in bytes and identities to prove it.

Proposed tests: `test_processing_counts`, `test_processing_surfaces_are_health_data_free`.

### R39 — Atomic and durable

- Every new write is all-or-nothing. A fault injected after any row write commits nothing.
- Each refusal leaves the whole database unchanged.
- Jobs, leases, attempts, outcomes, and item quarantines read the same after the store is closed and reopened. A held lease is still held after a reopen.

Proposed tests: `test_processing_writes_are_atomic_under_injected_failure`, `test_processing_refusals_leave_database_unchanged`, `test_processing_state_survives_reopen`.

### R40 — `@1`, `@2`, and `@3` unchanged

Archival, lifecycle, cycle reads, indexing, selections, and every earlier refusal behave as before. The runner proves this by re-running `@3`'s bound runner. A test also proves that processing and item quarantine never change any table created by migrations 1 or 2. It compares those tables before and after a mixed series of processing calls.

Proposed tests: `test_processing_changes_no_earlier_table`.

## Decisions for the Operator

1. **Explicit enqueue plus a derived read.** The `portal-worker` enqueues after reconciling, and `selections_awaiting_processing` finds anything not yet enqueued. `@3`'s `record_selection` is unchanged, so `reconcile-source-record@1` keeps its single declared effect. R17 forbids backfill, so a migrated database's selections are found only through the read. The alternatives:
   - `record_selection` enqueues atomically, which changes a published operation and needs a record-type-to-processor map in the store;
   - jobs exist only when claimed, which makes the durable need in a Processing Job's definition implicit.
2. **One open job per Source Record and processor version, re-targeted at claim.** A claim always takes the record's latest projection selection, so rapid change cycles coalesce into one job (ADR 0021, 0043). Outcomes are unique per selection, processor version, and policy identity. The alternative, one job per selection, multiplies work for records that change during a cycle.
3. **Retractions and Source Conflicts supersede the job.** Neither has a single current projection, so nothing is interpreted (ADR 0041). The selection itself is that record's terminal state. The alternative records explicit `retracted` and `conflicted` outcomes, which duplicates what the selection already says.
4. **A lease is held while its fence is current.** A holder may renew or complete after expiry, until another claim takes the job. The result is deterministic, so late work is still valid. The alternative refuses anything after expiry, which wastes finished work for no safety gain.
5. **The caller sets the retry bound; the store enforces it.** `max_attempts` is fixed at enqueue, and `retry_at` at each transient failure. The store applies the bound in the same transaction, including when it reclaims an expired lease. That is the only way a crash loop is caught. The alternatives are a store constant, which fixes policy in the store, or a caller-only bound, which cannot see crashed attempts.
6. **Failed is a job state, not an outcome.** A failed job keeps its reason and attempt history, and blocks automatic re-enqueueing. A later success needs a new job, from a manual retry or a corrected processor version (ADR 0021). The alternative, an immutable failed outcome in the outcome key, would block that later success.
7. **Revisions keep indexed header columns and opaque bytes.** The store holds the instants, local dates, and attributions that Phase 5 summaries and `@5` policy reprocessing must query. It never parses the header or body. The alternatives:
   - one opaque blob, which the store could not query without parsing health data;
   - a table per record type, which ties the store to every processor.
8. **Policy identity is opaque text.** `interpret-source-projection@1` defines it, and `@5` keys its policy records to match. There is no foreign key. The alternative, a minimal policy table now, takes `@5`'s work before its consumer exists.
9. **No "current" before Processor Generations.** `@4` records revisions under processor versions and reads the latest per version. Which version is current is `@6`'s selector (ADR 0044), and nothing publishes before a cutover (ADR 0045). The alternative, a provisional per-type selector, would need replacing in `@6`.
10. **Cycle-level counts wait for their consumer.** Outcomes link to selections, which `@3` links to evidence, positions, and cycles, so Phase 6 can count terminal outcomes per cycle page. The aggregate read arrives in `@6` with its consumer, as the delivery flow permits. The alternative adds an unused read now.
11. **Quarantine for items that cannot be indexed.** Bytes that are not JSON, or lack a usable Source Identity, can never become a Source Record. Without this, they stay pending forever and no snapshot page containing them could ever be complete. A new `items_awaiting_index` read excludes them, so `@3`'s `pending_items` keeps its bound meaning. The alternatives are to change `pending_items`, which needs a rebind of `@3`, or to leave such items stuck.
12. **The storage reserve gates every processing write.** This matches `@3` Decision 5. A stalled worker is safe: its lease expires and the work resumes once space returns. A call that writes nothing still succeeds below the reserve. The alternative exempts lease and failure bookkeeping, which lets the reserve be consumed by bookkeeping.
13. **Per-queue tables with shared private lease code.** ADR 0021 keeps each queue's scheduling and domain states separate while sharing lease machinery. `@6`'s Summary Refresh Jobs and `@7`'s Export Jobs reuse the code, not the table. The alternative, one generic lease table, mixes their states.
14. **Attempt history is kept.** Each claim appends an attempt with its owner, times, result, and health-data-free reason, linked to the Source Record so that `@7` deletion can erase it (ADR 0023). The alternative keeps only the job's current state, which loses the failure history the Operations page needs.
15. **Caller time.** Leases expire at the caller's `at` plus a duration. The store never reads a clock, as in every earlier version. Tests therefore control time exactly.
16. **Existing databases must be recreated.** A database at migration 2 refuses to open until `migrate-operational-store` arrives in Phase 8 (ADR 0032), as `@3` Decision 7 chose. No production database exists.
17. **The runner chains.** The `@4` runner runs `@3`'s bound runner unchanged, then the new suite. Its binding includes `@3`'s bound files.

## Ground rules

These are `@3`'s ground rules, and the trusted boundary is unchanged: standard-library SQLite only, synthetic values only, real databases in temporary directories, and whole-database snapshots for every "writes nothing" and refusal assertion. Outcome entries are synthetic. The suite builds Source Records and selections through `@3`'s own operations and passes hand-written outcomes with marker bytes. The store never checks them against `interpret-source-projection@1` (Decision 7).

Unbound inputs, reviewed rather than frozen:
- `docs/governance/scope-map.md`, which R28's scope-map test reads;
- the execution environment (`.python-version`, `pyproject.toml`, `uv.lock`, and the managed Python and SQLite runtime), which CI pins;
- `tests/governance/test_operational_store_4_contract.py`, which runs in the governance suite, not in this pack's runner.

Not mechanically provable here; the approver confirms these in implementation review:
- migration 3 only creates tables and indexes;
- no `@1`, `@2`, or `@3` code path changes behavior;
- the store never parses projection, header, or body bytes;
- the queue reads and counts use indexes rather than scanning all history. Astra's `@3` review warned against global scans at about 300,000 records.

## Freeze prerequisites

After Gate A, the executable pack adds:
- the new suite `tests/acceptance/test_operational_store_4.py`, covering R28–R40;
- the fixture `tests/acceptance/fixtures/operational-store-3-index.sql`, a synthetic database written by the published `@3` module (R29);
- the runner `tests/acceptance/run_operational_store_4.sh`, which runs `@3`'s bound runner unchanged and then the new suite;
- the Interface stub, appended to the published module: the new dataclasses and operations, each raising `NotImplementedError`. The stub adds no `FAILURES` entries and no migration; the implementer adds both.

RED must come from the stubs alone, and the 113 bound tests of `@1`–`@3` must still pass. The implementer then reviews the pack read-only for tests a correct implementation could not pass, and those findings are resolved before freeze.
