# Draft acceptance pack — `operational-store@6`

**Status:** Draft approved by the Operator on 2026-11-27, including the 18 decisions below as recommended; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `operational-store` Governed Scope contract, version 6, in `governance/scopes/operational-store.6.json`. It is the additive successor to the published `operational-store@5`, which it supersedes.

## What `@6` adds

On 2026-11-27 the Operator split publication into four store versions (split A in `docs/governance/delivery-flow.md`). `@6` is the first: summary work, with nothing published. It adds:
- **Processor Generations.** Immutable manifests that map each record type to a processor version (ADR 0022, 0042). `@6` registers and reads them. It never activates one.
- **Summary Refresh Jobs.** One durable job per summary key and target, with `@4`'s fenced, expiring leases and bounded retry. Requests for the same key and target coalesce behind an evidence token (ADR 0021, 0043).
- **Candidate Health Summaries.** Each completed refresh appends an immutable revision, but only if its evidence token is still current. The store keeps the summary as opaque bytes plus indexed header fields, as it keeps Canonical Revisions (`@4` Decision 7). It checks the Data Watermark against its own records and links the summary to the Canonical Revisions that contributed.
- **Policy summary work.** Each pending policy is linked to the refresh requests made for it. A read lists the live policies that still have none (`@5` Decision 1).
- **An evidence-change bookmark.** A read lists the processing outcomes, selections, and failed jobs that no refresh request has acknowledged yet. A request can move the bookmark in its own transaction, so a crash loses no change (Decision 5).
- **Summary evidence reads.** Per-installation processed delivery positions, and the evidence of one measure, record type, local-date range, and target, from one read transaction (ADR 0044).

Its consumers are `refresh-summary-state@1`, which leases the jobs, calls a derive unit, and completes them, and `execute-processing-job@2`, which reads generation manifests.

`summary-contract@1` defines the summary's bytes. The store never imports it and has no Vouch dependency on it: `summary-contract` depends on `set-source-selection-policy`, which depends on this store, so the dependency would be a cycle. The caller passes the header fields the store indexes (Decision 8).

## What stays unchanged

- Every `@1`–`@5` operation, read, type, and refusal is unchanged. Unlike `@5`, `@6` changes no earlier code path. `FAILURES` gains this version's names and `MIGRATIONS` gains migration 5.
- Migration 5 only creates tables and indexes. It inserts no row, alters no earlier table, and backfills nothing.
- Summary writes never change a table created by migrations 1–4.
- Nothing in `@6` is current or published. Every stored summary is a candidate. Activation, current selectors, Cutover Targets, Candidate Builds, Cutover Records, and publication arrive in `@7`.
- The summary key is fixed by `summary-contract@1` Decision 2: one summary per measure, record type, and local date, with no timezone.

## Interface

The module `activity_tracker_portal.operational_store` adds these frozen dataclasses. Byte fields are left out of every `repr`.
- `ProcessorGeneration(generation_id, manifest, manifest_sha256, registered_at)`. `manifest` is a tuple of `(record_type, processor_version)` pairs sorted by record type.
- `SummaryKey(measure, record_type, local_date)`.
- `SummaryTarget(generation_id, policy_horizon)`. `policy_horizon` is the highest policy identity the summary considers, or 0 for none (Decision 2).
- `ProcessedPosition(installation_id, sequence, receipt_time, processed_at)`: one installation's processed delivery position (R66).
- `SummaryEntry`: the caller's summary.
  - `header_bytes`, `evidence_bytes`, and `body_bytes` (`None` when there is no value): exact bytes the store never parses;
  - indexed header fields: `contract_version`, `derivation_version`, `processor_version`, `source_selection_policy` (a policy identity or `None`), `primary_state`, and `evidence_conditions` (a sorted tuple);
  - `watermark`: a tuple of `ProcessedPosition`, sorted by installation;
  - `contributors`: the ascending outcome identities of contributing Canonical Revisions.
- `StoredSummary`: `summary_id`, `key`, `target`, `token`, every `SummaryEntry` field, the store's `summary_sha256`, `evidence_sha256`, and `body_sha256`, and `job_id`, `fence`, and `recorded_at`.
- `RefreshAttempt(fence, owner, token, claimed_at, expires_at, finished_at, result, reason)`. `result` is `None` while open, or `"completed"`, `"stale"`, `"transient-failure"`, `"deterministic-failure"`, or `"lease-expired"`.
- `SummaryRefreshJob`: `job_id`, `key`, `target`, `state` (`"queued"`, `"leased"`, `"completed"`, or `"failed"`), `max_attempts`, `attempts`, `not_before`, `fence`, `lease_owner`, `lease_expires_at`, `failure`, `enqueued_at`, `closed_at`, and `history`.
- `RefreshLease(job_id, fence, owner, key, target, token, expires_at)`.
- `RefreshRequest(jobs_enqueued, jobs_coalesced, leases_rearmed)`: health-data-free counts.
- `RefreshCompletion(summary, requeued)`. `summary` is a `StoredSummary`, or `None` when the result was stale.
- `EvidenceChange(kind, change_id, record_id, record_type, processor_version)`. `kind` is `"outcome"`, `"selection"`, or `"failed-job"`.
- `EvidenceBookmark(outcomes_through, selections_through)` and `EvidenceChanges(bookmark, changes)`.
- `EvidenceAcknowledgement(outcomes_through, selections_through, failed_jobs)`.
- `SummaryEvidenceItem(record_id, source_system_kind, origin, selection_id, selection_kind, outcome, job_failed, last_revision)`, and `SummaryEvidence(items, source_selection_policies, next_after)` (R67).
- `RefreshCounts`: `jobs_ready`, `jobs_delayed`, `jobs_leased`, `jobs_lease_expired`, `jobs_completed`, `jobs_failed`, `stale_results`, `summaries`, `processor_generations`, `policies_awaiting_refresh`, and `evidence_changes_pending`.

It adds these operations:
- `Store.register_processor_generation(manifest, at) -> ProcessorGeneration`
- `Store.request_summary_refresh(keys, target, max_attempts, at, *, policy_id=None, acknowledge=None) -> RefreshRequest`
- `Store.claim_summary_refresh(owner, lease_seconds, at) -> RefreshLease | None`
- `Store.renew_summary_refresh_lease(job_id, fence, lease_seconds, at) -> RefreshLease`
- `Store.complete_summary_refresh(job_id, fence, entry, at) -> RefreshCompletion`
- `Store.fail_summary_refresh(job_id, fence, kind, reason, retry_at, at) -> SummaryRefreshJob`
- `StoreView.processor_generation(generation_id) -> ProcessorGeneration | None`
- `StoreView.processor_generations() -> tuple[ProcessorGeneration, ...]`
- `StoreView.summary_refresh_job(job_id) -> SummaryRefreshJob | None`
- `StoreView.summary_refresh_jobs(key) -> tuple[SummaryRefreshJob, ...]`
- `StoreView.summaries(key) -> tuple[StoredSummary, ...]`
- `StoreView.latest_summary(key, target) -> StoredSummary | None`
- `StoreView.policies_awaiting_refresh() -> tuple[int, ...]`
- `StoreView.evidence_changes(limit) -> EvidenceChanges`
- `StoreView.processed_positions(record_type, processor_version) -> tuple[ProcessedPosition, ...]`
- `StoreView.summary_evidence(measure, record_type, first_date, last_date, target, after, limit) -> SummaryEvidence`
- `StoreView.refresh_counts(at) -> RefreshCounts`

Every time argument is timezone-aware and comes from the caller. The store never reads a clock.

## Requirements

`operational-store@1`–`@5`'s requirements R1–R53 carry over. The `@6` runner re-runs `@5`'s bound runner unchanged, so every earlier suite must still pass exactly.

### R54 — Governed Scope contract, version 6

The `@6` contract keeps `@5`'s inputs and modes, observable effect, and empty dependency list.
- Its named failures begin with `@5`'s, in order and with the same outcome classes, followed by eight new invariant refusals: `processor-generation-invalid`, `processor-generation-unknown`, `summary-request-invalid`, `summary-target-invalid`, `summary-refresh-job-unknown`, `summary-entry-invalid`, `summary-watermark-invalid`, and `summary-contributor-invalid`.
- Its outputs keep `@5`'s names and add `processor_generation`, `summary_refresh_request`, `summary_refresh_job`, `summary_refresh_lease`, and `summary_revision`.
- Its `owns` list is `@5`'s, unchanged, with the summary entries appended. Its `does_not_own` list names what `@7`, `@8`, `@9`, and `@10` add.
- It does not contradict the `operational-store` scope-map row.

Proposed tests: `test_contract_6_extends_version_5`, `test_scope_map_row_does_not_contradict_contract_6`, `test_failures_match_contract_6`.

### R55 — Migration 5 is additive

- `MIGRATIONS` is `@5`'s migrations followed by migration 5. Each statement of migration 5 is a `CREATE TABLE` or `CREATE [UNIQUE] INDEX`.
- A new database records migrations 1–5 with their checksums and `user_version` 5. Migrations 1–4 keep the checksums the published `@5` module recorded.
- A database at migration 4, such as one written by `@5`, refuses with `store-migration-required` and is left unchanged.
- Brought to migration 5 with no backfill, every earlier row is unchanged and every new table is empty. It then opens, and:
  - its existing outcomes, selections, and failed jobs are all evidence changes (R65);
  - its policies all await refresh (R64);
  - a generation registers, and its processed positions and summary evidence read the fixture's records.

Proposed tests: `test_new_database_records_migrations_1_to_5`, `test_version_5_database_requires_migration_and_is_untouched`, `test_migrated_version_5_database_takes_summary_work`.

### R56 — Registering a Processor Generation

`register_processor_generation(manifest, at)` records an immutable manifest that maps each record type to one processor version.
- Generation identities ascend in registration order. `manifest_sha256` is the SHA-256 of the manifest as an RFC 8785 JSON object.
- A manifest equal to a registered one returns that generation and writes nothing, also below the reserve.
- Registration activates nothing, and no later write changes a generation.
- **Refusals,** each leaving the database unchanged:
  - `timestamp-invalid`;
  - `processor-generation-invalid` for a manifest that is not a non-empty mapping, a record type that is not a non-empty string, or a record type or version that is not a valid Unicode scalar string (one containing a lone surrogate, which RFC 8785 cannot serialize);
  - `processor-version-invalid` for a version not of the form `<scope>@<version>`, as in `@4` R30;
  - `storage-reserve-exhausted` when it would write.

Proposed tests: `test_generation_is_registered_with_its_digest`, `test_identical_manifest_returns_the_existing_generation`, `test_generation_refusals`, `test_generations_are_immutable_and_inactive`.

### R57 — Requesting a summary refresh

`request_summary_refresh` records that each named summary needs recalculating under one target.
- **Token.** For each key, the token for that key and target advances by one. A new key and target starts at 1.
- **One open job per key and target.** Then, for each key:
  - with no queued or leased job, a new queued job is created with the given `max_attempts`, `not_before` equal to `at`, and no attempts (`jobs_enqueued`);
  - a queued job is left as it is and claims the newest token later (`jobs_coalesced`);
  - a leased job is left as it is. Its lease now holds a stale token, so it runs again (R59, R63) (`leases_rearmed`).
- **Failed jobs do not block.** A failed job stays failed, and a new request creates a new job. This is how the Operator's manual retry works (ADR 0021).
- `keys` is a tuple of distinct keys. It may be empty only with `policy_id` or `acknowledge`.
- **Refusals,** each leaving the database unchanged:
  - `timestamp-invalid`;
  - `summary-request-invalid` for a malformed key (a measure that is not lowercase kebab-case of at most 64 characters, an empty or non-string record type, or a `local_date` that is not a `date` or is a `datetime`), duplicate keys, empty keys without a link or acknowledgement, a generation that is not a positive integer, a horizon that is not a non-negative integer, a `max_attempts` that is not an integer from 1 to 100, or a malformed acknowledgement (R65);
  - `processor-generation-unknown` for an unregistered generation;
  - `policy-unknown` for a nonzero horizon, or a `policy_id`, that names no policy;
  - `summary-target-invalid` when the generation does not map a key's record type, or the horizon is below `policy_id` (R64);
  - `storage-reserve-exhausted`.

Proposed tests: `test_request_creates_one_queued_job_per_key`, `test_request_advances_the_token_and_coalesces`, `test_request_on_a_leased_job_rearms_it`, `test_failed_job_does_not_block_a_new_request`, `test_request_refusals`.

### R58 — Claiming and renewing refresh leases

These follow `@4` R31 and R32, on the refresh tables.
- `claim_summary_refresh` takes the eligible job with the lowest identity: a queued job whose `not_before` is at or before `at`, or a leased job whose lease expired at or before `at`.
- An expired lease's attempt closes as `lease-expired`. If the job has used all its attempts it becomes `failed` with `lease-expired-retries-exhausted`, unless its token advanced after that lease's claim (R63).
- The claim increments the fence and attempts, appends an attempt, and records the current token in the lease and the attempt.
- `renew_summary_refresh_lease` extends a held lease. A lease is held while the job is leased and the fence is current, also after expiry until another claim (`@4` Decision 4).
- Refusals: `timestamp-invalid`; `summary-request-invalid` for an empty owner or a `lease_seconds` that is not an integer from 1 to 86,400; `summary-refresh-job-unknown`; `lease-not-held`; and `storage-reserve-exhausted` for any claim or renewal that writes.

Proposed tests: `test_claim_takes_the_lowest_eligible_refresh_job`, `test_lease_carries_the_current_token`, `test_expired_refresh_lease_is_reclaimed`, `test_exhausted_expired_refresh_job_fails`, `test_renew_extends_a_held_refresh_lease`, `test_two_handles_never_hold_one_refresh_job`.

### R59 — Completing a refresh

`complete_summary_refresh(job_id, fence, entry, at)` checks the entry (R60–R62) whether or not its token is current, then:
- **Token current:** it appends one immutable summary revision for the key, target, and token, closes the attempt as `completed`, and completes the job. It returns the revision.
- **Token advanced since the claim:** it appends nothing, closes the attempt as `stale`, and queues the job again with `not_before` equal to `at` and its attempts reset. It returns `RefreshCompletion(None, True)`. Nothing of the stale result is kept (Decision 4).
- **Identical result.** An entry equal to the latest stored summary for the same key and target is not appended again: the existing revision is returned, and the job completes.
- Refusals: `summary-refresh-job-unknown`, `lease-not-held`, `timestamp-invalid`, the entry refusals of R60–R62, and `storage-reserve-exhausted`. A repeat completion after success is `lease-not-held`.

Proposed tests: `test_current_completion_appends_the_summary`, `test_stale_completion_discards_and_requeues`, `test_identical_result_is_not_appended_twice`, `test_completion_refusals`, `test_reclaimed_refresh_lease_cannot_complete`.

### R60 — The summary entry

The store checks form and its own facts. It never parses the bytes (Decision 8). `summary-entry-invalid` when:
- the header or evidence bytes are empty or not `bytes`, or the body is neither `None` nor non-empty `bytes`;
- the contract or derivation version is not of the form `<scope>@<version>`;
- `processor_version` differs from the generation's version for the key's record type;
- `primary_state` or a condition is not lowercase kebab-case of at most 64 characters, or the conditions are not strictly ascending;
- `source_selection_policy` is not the identity of the Source Selection Policy that is live at the target's horizon (R67) and applies to the key's measure, record type, and date, or is not `None` when none applies.

The store computes `summary_sha256` from the header bytes, `evidence_sha256` from the evidence bytes, and `body_sha256` from the body, or `None`. Stored summaries are never updated or deleted.

Proposed tests: `test_entry_shape_is_checked`, `test_summary_digests_are_computed_by_the_store`, `test_processor_version_must_match_the_generation`, `test_source_selection_policy_must_be_the_applicable_one`, `test_summaries_are_append_only`.

### R61 — The watermark matches the store

`summary-watermark-invalid` when the watermark is empty, is not sorted by installation, names an installation twice, or has a position the store cannot vouch for. Each position must name an archived message whose sequence is at or below the installation's current processed position for the key's record type and the generation's processor version (R66). Its `receipt_time` must equal that message's receipt time, and its `processed_at` must equal the processed time R66 gives for that sequence. Times are compared as instants: a position may be given in any timezone, including a fold-aware local time in a repeated hour, and the store normalizes it to UTC before comparing. Stored and returned positions are UTC.

Proposed tests: `test_watermark_must_match_store_positions`, `test_watermark_cannot_run_ahead_of_processing`.

### R62 — Contributor links

Each stored summary links to the outcomes in `contributors`. `summary-contributor-invalid` when the list is not strictly ascending positive integers, or an outcome:
- does not exist, or is not a Canonical Revision;
- belongs to a Source Record of another record type;
- has a processor version other than the entry's; or
- has a local-date span, from its start date to its end date (or start date for an instant), that does not include the key's date.

An empty list is valid, as for a `missing` summary. The store writes one link per contributor, carrying the outcome and the summary, and indexes the links by outcome, so later versions can find every summary a revision contributed to (ADR 0023). No `@6` read looks summaries up by outcome: the suite checks that the link rows exist, whatever their layout, and the approver reviews the relation and its leading outcome index (Residuals).

Proposed tests: `test_contributors_are_linked_to_outcomes`, `test_contributor_checks`.

### R63 — Failure, retry, and newer evidence

`fail_summary_refresh` follows `@4` R35:
- a transient failure re-queues at `retry_at` while attempts remain, and otherwise fails with `retries-exhausted`;
- a deterministic failure fails the job with the caller's reason;
- a failed job is never retried automatically.

**Newer evidence always gets a run.** When the token advanced after the lease's claim, a failure still closes its attempt with its result, but the job is queued again with its attempts reset: at `retry_at` for a transient failure, and at `at` for a deterministic one. The same holds for an exhausted expired lease at claim (R58). Otherwise a request made during a failing lease would be lost.

Refusals: `summary-refresh-job-unknown`, `lease-not-held`, `summary-request-invalid` (for an unknown kind, a reason that is not kebab-case of at most 64 characters, a transient failure without a `retry_at` at or after `at`, or a deterministic one with one), `timestamp-invalid`, and `storage-reserve-exhausted`.

Proposed tests: `test_transient_refresh_failure_requeues_after_backoff`, `test_refresh_retries_are_bounded`, `test_deterministic_refresh_failure_is_terminal`, `test_stale_failure_runs_again_for_newer_evidence`, `test_refresh_failure_refusals`.

### R64 — Policy summary work

- A request with `policy_id` links that policy to the request, in the same transaction. The target's horizon must be at least `policy_id`, so the work considers the policy, or the request is `summary-target-invalid`. A policy may be linked more than once.
- `policies_awaiting_refresh()` lists, in ascending order, the live policies of both kinds that have no link. "Live" is `@5`'s meaning: not superseded.
- The caller decides which keys a policy affects. The store does not (Decision 6).

Proposed tests: `test_policy_link_is_recorded_with_its_request`, `test_policies_awaiting_refresh_lists_live_unlinked_policies`, `test_policy_target_must_include_the_policy`.

### R65 — Evidence changes and the bookmark

- `evidence_changes(limit)` returns the stored bookmark and, up to `limit` in total: the processing outcomes after `outcomes_through`, then the selections after `selections_through`, each in ascending identity, then the failed Processing Jobs not yet acknowledged, by job identity. Each change names its Source Record, record type, and, for outcomes and jobs, processor version.
- A request with `acknowledge` moves the bookmark to its two `through` values and marks its `failed_jobs` acknowledged, in the same transaction as the request.
- The acknowledgement is `summary-request-invalid` when a `through` value is below the bookmark or above the highest existing identity, or a listed job is not a failed Processing Job. Acknowledging a job twice is allowed.
- The bookmark starts at 0 and 0, so after migration every earlier change is listed.

Proposed tests: `test_changes_list_outcomes_selections_and_failed_jobs`, `test_acknowledgement_moves_the_bookmark_with_the_request`, `test_acknowledgement_refusals`, `test_unacknowledged_changes_survive_a_lost_request`.

### R66 — Processed positions

`processed_positions(record_type, processor_version)` returns, sorted by installation, one position per installation that has a settled ingest batch of that record type at or below its position.
*Revised after the implementer's review (findings 1, 2, and 9). This changes the approved text; see "Implementer review resolutions".*
- **Settlement is historical.** Settlement is an irreversible fact for an archived item and processor version, established by witnesses that are never removed:
  - a quarantined item is witnessed by its quarantine;
  - an indexed projection is witnessed through any selection of its Source Record whose evidence head is at least the item's evidence: the selection itself when it is a retraction or Source Conflict, any outcome under the version for that selection, and any Processing Job under the version for that selection that ended `failed`. A later job, such as a manual retry, does not remove an earlier failure witness.
- **Retractions.** A retraction's matching Source Records are fixed as of its indexing: the records of its record type and Source Identity that have projection evidence indexed before the retraction's evidence. With no such record, indexing itself witnesses the retraction. Otherwise it is settled only when every matching record has a witness as above. A projection indexed later, even of the same identity, is a new obligation at its own archived position.
- A batch is settled when all its items are.
- **Position.** The highest archived sequence of the installation below its lowest unsettled batch of the type, or its highest archived sequence when none is unsettled: the archived prefix before the first unsettled batch of the record type. Messages of other types and kinds never hold it back. The watermark therefore asserts that processing reached a terminal witness through that prefix. It does not assert success, nor that newer evidence has been processed.
- **Times.** `receipt_time` is the receipt time of the message at that sequence. An item's settlement time is the earliest caller-supplied timestamp among its witnesses; for a retraction with matching records, it is the latest of those records' earliest witness times. `processed_at` is the latest of the receipt time and the settlement times of the type's items at or below the sequence.
- Positions never move back, because every witness is append-only. For one sequence, `processed_at` may still fall if a later write supplies an earlier timestamp, since callers' times need not be monotone. The store cannot recover commit order: two histories that end in identical rows must give the same answer.
- This needs no change to any `@3`–`@5` writer. It reads existing history only.

Proposed tests: `test_position_stops_before_the_first_unsettled_batch`, `test_settled_items`, `test_position_times`, `test_positions_never_move_back`, `test_a_failure_witness_survives_a_retry`, `test_retraction_matches_records_as_of_its_indexing`, `test_retraction_needs_every_matching_record`, `test_settlement_time_is_the_earliest_witness`.

### R67 — Summary evidence

`summary_evidence(measure, record_type, first_date, last_date, target, after, limit)` lists, in ascending record identity after `after`, up to `limit` Source Records of the type whose latest selection is a projection or Source Conflict and whose last revision under the generation's version has a local-date span that meets the inclusive range. Each item gives:
- the record's source-system kind and origin, but not its Source Identity;
- its latest selection and that selection's kind;
- `outcome`: its latest outcome under the version for that selection, of any policy identity, or `None`;
- `job_failed`: whether the last job under the version for that selection failed;
- `last_revision`: its highest-identity Canonical Revision under the version, which places it in the range.

`source_selection_policies` lists the Source Selection Policies for the measure and record type that are live at the horizon and meet the range, by first date. A policy is live at horizon `h` when its identity is at most `h` and no policy at most `h` supersedes it. `next_after` is the last record identity when more may follow, or `None`.

Refusals: `summary-request-invalid` for a malformed range, `after`, or `limit`, `processor-generation-unknown`, `policy-unknown`, and `summary-target-invalid` as in R57.

Proposed tests: `test_evidence_lists_records_placed_in_the_range`, `test_evidence_reports_each_record_state`, `test_evidence_pages_by_record`, `test_evidence_names_policies_live_at_the_horizon`.

### R68 — Generation, job, and summary reads

- `processor_generation` returns `None` for an unknown identity. `processor_generations` lists all in registration order.
- `summary_refresh_job` returns `None` for an unknown job, or the job with its whole attempt history. `summary_refresh_jobs(key)` lists the key's jobs under every target in identity order.
- `summaries(key)` lists every stored revision of the key under every target in recording order. `latest_summary(key, target)` returns the one with the highest token, or `None`.
- Every read in one view comes from one read transaction and writes nothing. Returned times are UTC.

Proposed tests: `test_generation_reads`, `test_refresh_job_read_includes_history`, `test_summary_reads`, `test_summary_reads_write_nothing`.

### R69 — Health-data-free counts and surfaces

- `refresh_counts(at)` returns only counts. `at` splits ready from delayed jobs and held from expired leases, as in `@4` R38. `stale_results` counts `stale` attempts.
- Summary header, evidence, and body bytes are health-bearing. They never appear in a refusal, a count, or a `repr`. Refusal `args` are exactly `(failure,)`, and refusals carry no key, date, or identity either.
- Tests put synthetic markers in the bytes to prove it.

Proposed tests: `test_refresh_counts`, `test_summary_surfaces_are_health_data_free`.

### R70 — Atomic, durable, and serialized

- Every new write is all-or-nothing, with its tokens, jobs, links, bookmark, watermark, and contributor rows. A fault injected after any row write commits nothing.
- Each refusal leaves the whole database unchanged.
- Generations, tokens, jobs, leases, attempts, summaries, links, and the bookmark read the same after a reopen.
- Two handles requesting the same key and target leave one open job, with the token advanced twice. Two handles never hold one refresh job.
- The storage reserve gates every write, as in `@4` Decision 12. A call that writes nothing succeeds below it.

Proposed tests: `test_summary_writes_are_atomic_under_injected_failure`, `test_summary_refusals_leave_database_unchanged`, `test_summary_state_survives_reopen`, `test_racing_requests_leave_one_open_job`, `test_summary_writes_are_gated_by_the_reserve`.

### R71 — `@1`–`@5` unchanged

Every earlier operation and refusal behaves as before. The runner proves this by re-running `@5`'s bound runner. A test also proves that no `@6` write changes any table created by migrations 1–4.

Proposed tests: `test_summary_writes_change_no_earlier_table`.

## Decisions for the Operator

Each item is a recommendation for the Operator to approve or change.

1. **Nothing publishes in `@6`, and a pending policy gets refresh jobs, not a Cutover Target.** `@5` Decision 1 said `@6` would turn each pending policy into "its Cutover Target and rebuild work". Under split A, Cutover Targets move to `@7`. So `@6` links each pending policy to ordinary refresh jobs whose target includes it, and every result is a candidate.
   - Recommended: as above. As with `@5` Decision 2, early candidates are harmless, because nothing publishes them.
   - Alternative: hold policy summary work until `@7`. Policies then build nothing in Phase 5.
   - Consequence: `@7` must decide whether these candidates can feed a policy's Candidate Build or are rebuilt under its target.
2. **A target is one generation plus a policy horizon.** The horizon is the highest policy identity the summary considers. Policies share one arrival sequence (`@5`), so the policies live at the horizon are fixed forever. `summary-contract@1` Decision 10 keeps both values beside the bytes, not in them.
   - Recommended: the horizon, a single integer.
   - Alternative: an explicit set of policy identities. It could express "policy B without the earlier policy A", which a horizon cannot. But policies activate in arrival order (ADR 0043), so that case should not arise.
   - Alternative: a digest of the set. It is compact, but cannot be read without the set.
3. **The evidence token counts requests per key and target.** Each request adds one. A result is kept only if no request arrived after its claim. The "newest complete evidence boundary" of ADR 0043 is a different thing, and stays with `@7`'s Cutover Targets.
   - Recommended: a per-key-and-target counter.
   - Alternative: a delivery position as the token. That ties every summary to the delivery order, and does not change when a policy or reprocessing changes the evidence.
4. **Newer evidence always gets another run; stale results are thrown away.** A completion whose token is stale stores nothing and queues the job again with a fresh retry budget (ADR 0043; `complete-change-cycle` discards intermediate candidates). A failure with a stale token re-runs the same way, because otherwise the request made during it would be lost.
   - Recommended: as above.
   - Alternative: keep stale results as unpublished candidates. Nothing would read them, and they would be more health data to erase.
   - Alternative: let a stale failure stay failed. The newer evidence then waits for an unrelated later request.
5. **A bookmark of evidence changes tells the worker what still needs refreshing.** The store cannot work out which days a change affects. That needs each measure's attribution rules, such as Sleep's wake date. So the worker requests refreshes explicitly. To survive a crash between a processing write and its request, the store keeps a bookmark. The worker reads the changes after it, works out the affected days, and in one transaction requests those refreshes and moves the bookmark. Changes are new processing outcomes, new selections (including retractions and Source Conflicts), and newly failed jobs.
   - Recommended: as above. No `@3`–`@5` writer changes.
   - Alternative: no bookmark. A crash in that gap leaves a day's candidate out of date until the next change to it.
   - Alternative: `complete_processing_job`, `fail_processing_job`, and `record_selection` request refreshes themselves. That changes three published operations, and the store would need the attribution rules.
   - Alternative: a bookmark of delivery positions. It misses reprocessing of evidence already delivered, such as after a policy.
6. **Callers choose a policy's keys; the store records the link.** Which days a policy affects depends on the measure's attribution and, for a timezone, on reprocessing that has not run yet. The store checks only that the target includes the policy, and lists live policies with no link.
   - Recommended: as above.
   - Alternative: the store computes the keys from the policy's measure, record type, and dates. That works for Source Selection, but not for Fallback Timezone, and it puts attribution in the store.
7. **Generations are registered and read, never activated, in `@6`.** A generation is an immutable manifest, identified by an ascending number and by its digest. An identical manifest returns the existing generation. The first activation happens with first publication in `@7`.
   - Recommended: as above.
   - Alternative: activate the first generation at registration. That is a current-view selector without its Cutover Record (ADR 0044).
   - Consequence: `execute-processing-job@2` chooses which registered generation it processes under. `@6` does not mark one as current.
8. **The store checks a summary without reading its bytes.** The caller passes the header fields to index alongside the bytes. The store checks their form and checks them against its own facts: the processor version against the generation, the applied Source Selection Policy against the horizon (R60), the watermark against its own positions (R61), and the contributors against their outcomes (R62). It computes the digests. Whether the fields match the bytes is `refresh-summary-state`'s to test, using `summary_contract.decode_summary`.
   - Recommended: as above, like Canonical Revisions (`@4` Decision 7).
   - Alternative: the store parses the header with the standard-library JSON parser and compares the fields. That ties the store to `summary-contract`'s header layout with no Vouch dependency to warn of a change. It also loads local dates and identities into the store's code.
   - Alternative: the store imports `summary_contract`. That needs a Vouch dependency, which is a cycle through `set-source-selection-policy`, and a Python import cycle.
   - Consequence: a caller could store bytes whose header disagrees with the fields. `refresh-summary-state`'s pack must test that it cannot.
9. **Contributors are checked against the outcome they name.** Each must be a Canonical Revision of the key's record type, under the generation's processor version for that type, whose local dates include the key's date. Links are kept for later deletion and inspection (ADR 0023).
   - Recommended: as above. A sleep session is attributed to its wake date, its end date, which lies in its span.
   - Alternative: check only that the outcome exists. A derive defect could then link revisions of another type or version.
   - Alternative: also require it to be the record's latest outcome. That would refuse a result that raced newer evidence, which the token already handles.
10. **The watermark positions are the store's, per record type.** `summary-contract@1` R9 gives one position per installation "whose accepted messages the derivation considered", stating that "every message of that installation" at or below it was indexed and terminally processed, and leaves the computation to the caller "from the store". Only the store can compute it set-based and in the same snapshot as the evidence, so `@6` computes it (R66) and refuses a watermark it cannot vouch for (R61).
    - Recommended: a position waits only for ingest batches of the summary's own record type, so a heart-rate backlog does not hold back a steps watermark. This reads R9's "every message" as every message the derivation could consider.
    - Alternative: wait for ingest batches of every type. That matches R9's "every message" most literally, but one type's backlog delays every summary.
    - Flag: whichever is chosen, `refresh-summary-state` copies these positions into the header unchanged.
11. **What the evidence read returns.** For each record placed in the range, it gives facts, not standings: the latest selection, the latest outcome under the version, whether its job failed, and its last revision, which gives its dates. Records with no revision under the version cannot be placed and are left out (`summary-contract@1` Decision 6). Retracted records are left out.
    - Recommended: the latest outcome of any policy identity. While a Fallback Timezone Policy is pending, a candidate therefore reflects it where reprocessing has finished. That is harmless, because nothing publishes. `@7`'s Candidate Builds pin exact outcomes.
    - Alternative: the caller names the policy identity to use. The identity depends on each record's times and the tzdata release, which only `interpret-source-projection` knows.
12. **Repeats.** An identical manifest is a no-op. A result equal to the latest stored summary for its key and target is not stored again. A repeated request advances the token again, which costs at most one extra run. A repeated completion or failure is `lease-not-held`, as in `@4`.
    - Recommended: as above.
    - Alternative: a caller-supplied request key for exact request idempotence. Every caller would have to make and keep one.
13. **The derivation version is an indexed field, not part of the target or the generation.** A Processor Generation maps record types to processor versions (glossary), and `summary-contract@1` puts the derivation version in the header.
    - Recommended: as above.
    - Alternative: generations also map each measure to a derivation version, so a derive upgrade is a generation change. That widens the glossary's meaning.
    - Flag: no unit yet governs how a derive-unit upgrade becomes current. It belongs with `@7` and `activate-processor-generation`.
14. **Per-type counts of evidence no day can place wait for `@9`.** `summary-contract@1` Decision 6 leaves these counts to `@6` or the presentation units. Only the Overview reads them, and it arrives in Phase 7.
    - Recommended: `@9`, with the other presentation reads.
    - Alternative: add the count read now, unused until Phase 7.
15. **The storage reserve gates every summary write**, as for every earlier write (`@4` Decision 12). A request for many keys is one large write. The alternative exempts lease bookkeeping, which lets bookkeeping use up the reserve.
16. **Existing databases must be recreated.** A migration-4 database refuses to open until `migrate-operational-store` arrives in Phase 8, as `@5` Decision 14 chose. No production database exists. The alternative is an in-place upgrade now, which takes that unit's work early.
17. **The runner chains.** The `@6` runner runs `@5`'s bound runner unchanged, then the new suite. The alternative, a separate runner, would let an earlier suite stop running.
18. **The waiting store follow-ups stay out of `@6`.** Bead `2rs` asks for `after` cursors on the three discovery reads, and bead `ny5` for a narrow read of a job's projection-schema version. Both change interfaces outside summary work, and the worker works around both. The alternative folds them in now and saves a later store version, at the cost of a larger review.

## Predecessor and dependents

- Every dependent of `@5` is published, the last being `set-fallback-timezone-policy@1` on 2026-11-26 (`e5e358b`), so `@6` may start (delivery flow, sequencing rules).
- Nine units depend directly on `operational-store`, all published: the four `accept-*` units, `manage-sender-installation`, `reconcile-source-record`, `execute-processing-job`, `set-source-selection-policy`, and `set-fallback-timezone-policy`. Superseding `@5` leaves them published, as superseding `@3` and `@4` did. No re-verification is expected from `supersede` or `specify`; `status` confirms it after they run.
- While `@6` is unpublished, a new or re-verified dependent of `operational-store` cannot publish. Today that holds back only `@6`'s own dependents: `refresh-summary-state@1`, `execute-processing-job@2`, and `operational-store@7`. `summary-contract` and the derive units do not depend on the store.
- **The DEV-os4-01 lesson.** Amending `@6` after freeze (`respecify`) would invalidate the published dependents, as `@4`'s amendment invalidated seven on 2026-11-25. Each would need `begin-reverify`, a fresh GREEN, and republication in dependency order after `@6` publishes. That is now 15 units: the nine above, `receipt-bound-delivery`, `interpret-source-projection`, and the four processors. So the read-only review and Gate B should settle everything before freeze.

## Ground rules

These are `@5`'s ground rules. The trusted boundary is unchanged: standard-library SQLite only, synthetic values only, real databases in temporary directories, and whole-database snapshots, with each rowid table's rowids in rowid order and `WITHOUT ROWID` tables as sorted rows, for every "writes nothing" and refusal assertion. Source Records, selections, jobs, outcomes, and policies are built through `@3`–`@5`'s own operations, with hand-written outcomes. Summary entries are hand-written marker bytes. The suite never imports `summary_contract`, because the store must not depend on it.

Unbound inputs, reviewed rather than frozen:
- `docs/governance/scope-map.md`, which R54's scope-map test reads;
- the execution environment (`.python-version`, `pyproject.toml`, `uv.lock`, and the managed Python and SQLite runtime), which CI pins;
- `tests/governance/test_operational_store_6_contract.py`, which runs in the governance suite;
- `tests/unit/test_operational_store_hardening.py`, which checks that a new database records every known migration. It is supplementary, not bound acceptance evidence.

## Freeze prerequisites

After Gate A, the executable pack adds:
- **The suite.** `tests/acceptance/test_operational_store_6.py` has 71 tests across R54–R71, exactly the proposed test names above, four of them added in R66 after the implementer's review. It has no timing limits. Every claim-drain loop is bounded by the number of jobs it expects plus the final `None`, and the race threads also have a 60-second join limit.
- **The fixture.** `tests/acceptance/fixtures/operational-store-6/operational-store-5-policies.sql`. It sits under the version's own fixture directory, not at the top-level path the draft named, because the drafter's brief limited fixtures to that directory. A throwaway script outside the repository imported the published `@5` module (extracted with `git archive d9a5aee`; the module is unchanged since `9b77c79`) and wrote the database through its public API only. The database was dumped with `iterdump`, and `PRAGMA user_version=4` was appended. The header comment records the method. The fixture holds:
  - installation A, retired after draining, and installation B, active. A delivered a steps and a weight snapshot, a snapshot completion, and a steps change cycle. B delivered a steps and a weight snapshot;
  - an inferred and an experienced steps revision, a Policy-Pending outcome on a record later retracted, a weight revision, a weight Quarantine, and a revision from B's other origin. B re-observed one record, whose latest selection is now a Source Conflict;
  - one failed weight job and one queued steps job;
  - one never-indexed item and one quarantined item;
  - two Source Selection Policies, the first superseded, and one Fallback Timezone Policy.

  R55's expected positions and evidence come from R66 and R67 applied to this fixture by hand. For steps, A stops at its completion (sequence 3) before the queued change batch; B reaches sequence 2. For weight, A's unsettled steps batches do not hold its weight position back (sequence 6).
- **The runner.** `tests/acceptance/run_operational_store_6.sh` runs `@5`'s bound runner unchanged (it runs `@4`'s, `@3`'s, `@2`'s, and `@1`'s), then the new suite.
- **The Interface stub,** added to the published module:
  - three type aliases (`RefreshJobState`, `RefreshAttemptResult`, and `EvidenceChangeKind`);
  - the eighteen Interface dataclasses, real and frozen. Byte fields are `field(repr=False)`. `SummaryEvidenceItem.outcome` and `last_revision` are `ProcessingOutcome`s, and `SummaryEvidence.source_selection_policies` is a tuple of `SourceSelectionPolicyRecord`;
  - the seventeen operations, each raising `NotImplementedError("operational-store@6")`: six on `OperationalStore` and eleven on `StoreView`.

  The stub adds no `FAILURES` entries, no migration, and no change to any `@1`–`@5` code path. `_operational_store_sql.py` is unchanged. The implementer adds the names and migration 5.

**RED, through the runner, against the stub:**
- `@1`–`@5`'s 222 bound tests (53, 15, 45, 59, and 50) pass.
- Of the 71 new tests, 2 pass: the pure contract tests `test_contract_6_extends_version_5` and `test_scope_map_row_does_not_contradict_contract_6`.
- 4 fail on an `AssertionError`, because the stub has no new names and no migration 5:
  - `test_failures_match_contract_6`;
  - `test_new_database_records_migrations_1_to_5`;
  - `test_version_5_database_requires_migration_and_is_untouched`, because the stub opens a migration-4 database without refusing;
  - `test_migrated_version_5_database_takes_summary_work`, because no table is added.
- The other 65 fail with `NotImplementedError("operational-store@6")`. The race tests re-raise their workers' unexpected errors. The fault-injection helper treats only `sqlite3.DatabaseError` as an injected fault, so the stub's error is never mistaken for one.

In the worktree, with the stub, these all pass: `tests/unit` (145), `tests/governance` (65), and every other acceptance suite under `tests/acceptance`. The one exception is `receipt_bound_delivery_1`, whose 9 tests need the Sender's `node_modules`, which the drafting worktree lacks (`ERR_MODULE_NOT_FOUND`). It is unrelated to the store.

**Passability.** A throwaway reference outside the repository passes all six suites through the runner, `tests/unit`, `tests/governance`, and the dependent acceptance suites. It has:
- migration 5: eleven tables and seven indexes, none on an earlier table;
- the eight names;
- the seventeen operations.

**Mutants.** The drafter ran 59 wrong-implementation mutants against the reference, and the final suite kills all 59: the original 49 and 10 added after the review. Three valid variants from the implementer's review pass: an `AUTOINCREMENT` generation identity, a renamed contributor-link column, and a claim that first returns the permitted `store-busy`. The mutants cover:
- the token: a stale result stored, a token that does not advance, a token advanced by a refused request, and a renewal that reports the current token;
- re-runs for newer evidence: a stale failure left failed; stale completions or failures that keep their attempts; an exhausted expired lease that ignores the token or never fails;
- coalescing: two open jobs per key and target (index dropped), and a coalesced request that moves `not_before`;
- the bookmark: moved in its own transaction, or moved backward;
- evidence changes that miss selections, retractions, or failed jobs, or ignore acknowledgements;
- contributors of any type, version, or date, or checked only against the start date;
- watermarks: positions held back by other record types (Decision 10), a watermark ahead of processing accepted, and processed times unchecked;
- positions: settled by any selection, settled only by the latest selection (positions move back), the latest settlement time, or the highest settled batch;
- generations: a duplicate generation for an identical manifest, digests over unsorted keys, and keys in code point order instead of UTF-16 order;
- policies: a policy above the horizon, "live" judged now (in the entry check and in the evidence read), links without the horizon check, superseded policies listed as awaiting, and the applied policy unchecked;
- evidence placed by its first revision, and retracted records included;
- the reserve: skipped on renewal, on a stale completion, or on a claim, and checked before the identical-manifest repeat;
- a read that writes, a migration that alters an earlier table, a backfilling migration, entry bytes in a refusal's `args`, and identical results appended twice or matched against any earlier revision.

- after the implementer's review:
  - the review's survivors: missing contributor link rows, a noncanonical control escape (and uppercase hexadecimal), an existential retraction, and wall-time watermark equality;
  - the two R66 regressions: settlement by the last job only, and retraction matches judged now;
  - lone surrogates left unchecked;
  - the latest of several failures used as the settlement time;
  - a claim that keeps returning a lease. It now fails the suite quickly instead of hanging.

Before the review, one mutant survived the first run: contributors of any record type. The only other-type contributor also had another processor version, so the version check refused it first. The contributor test now includes a weight revision recorded under the steps version.

**Refinements.** The tests pin these points beyond the approved text. Apart from the R66 revision and the R56 lone-surrogate refusal, which "Implementer review resolutions" flags, none changes approved semantics. Items marked *settles* decide a point the text left open, for the approver to confirm.
- **Generations (R56).**
  - Any `Mapping` is accepted. An empty mapping is `processor-generation-invalid`, as is a list or tuple of pairs, `None`, a string, or a key that is empty or not a string. The key check comes before the version check.
  - The digest is RFC 8785 in full: non-ASCII as literal UTF-8, `"` and `\` escaped, and keys in UTF-16 code-unit order, so a key outside the Basic Multilingual Plane sorts before U+FB01.
  - `manifest` is sorted in code point order. `registered_at` is UTC.
  - A subset, a superset, or a changed version is a new generation.
- **Request shapes (R57).**
  - `keys` must be a tuple of `SummaryKey`.
  - A `local_date` that is a `datetime` (aware or naive), a string, or an integer is refused.
  - `target` must be a `SummaryTarget`, with `type(...) is int` fields.
  - `max_attempts` accepts 1 and 100, and refuses 0, 101, `2.5`, `"3"`, and `None`.
  - An acknowledgement must be an `EvidenceAcknowledgement` with integer `through` values and a tuple of integer job identities.
  - A request with no keys and only `policy_id` returns `RefreshRequest(0, 0, 0)`.
- **Coalescing (R57).** A coalesced queued job keeps its `max_attempts`, `not_before`, and `enqueued_at`, so a job delayed by a transient failure stays delayed.
- **Claims and leases (R58).**
  - A queued job is eligible exactly at `not_before`, and an expired lease exactly at its expiry.
  - Expiry is computed in UTC.
  - A claim with nothing eligible returns `None` and writes nothing, also below the reserve. A claim that only fails an exhausted expired lease is gated by the reserve, and returns `None` when it succeeds.
  - An exhausted expired lease whose token advanced is re-queued and claimed in the same call: attempts 1, the next fence, and the current token.
  - The attempt keeps its claim-time `expires_at` after renewal.
  - Renewal accepts 1 and 86,400 seconds.
  - *Settles:* a renewal returns the claim's token, not the current one.
- **Completion (R59).**
  - A stale completion keeps the job's fence until the next claim and leaves `max_attempts` unchanged. No byte of the stale entry is stored anywhere in the database.
  - An identical result adds no row to any table. It is compared only with the highest-token summary for the key and target, so a result equal to an older revision is appended.
  - The reserve gates stale and identical completions too.
- **The entry (R60).**
  - Byte fields must be `bytes`; a `bytearray` is refused.
  - `evidence_conditions` must be a tuple; a list or string is refused.
  - Bytes are opaque: NUL and non-UTF-8 bytes are stored exactly.
  - An unknown policy identity is refused.
  - A Fallback Timezone Policy's identity can be the horizon.
  - A policy above the horizon never applies, even if it is live now.
- **The watermark (R61).**
  - Times in another offset for the same instant are accepted, and stored and returned in UTC. That includes a fold-aware time through a synthetic repeated hour; the other fold, an hour later, is refused.
  - A position at an earlier sequence at or below the current one is accepted with that sequence's own processed time.
  - Sequence 0, an unarchived sequence, and an installation with no position are refused.
- **Contributors (R62).**
  - Both span ends are inclusive. An instant is placed by its start date.
  - The record-type check stands on its own, as the survivor above showed.
  - Link rows: some migration-5 table gains one row per contributor carrying the outcome and the summary's identity. Column names and the index are not checked; the index is a residual.
- **Failure (R63).** `retry_at` equal to `at` is allowed. A naive `retry_at` is `timestamp-invalid`.
- **Evidence changes (R65).**
  - `limit` truncates across the three groups in order.
  - `evidence_changes_pending` counts every pending change.
  - Leased, queued, and completed jobs are not changes.
  - Acknowledgement refusals apply with and without keys. A listed job that is not failed is refused, also next to a failed one.
- **Positions (R66).**
  - An installation with no settled batch of the type is absent.
  - Witness times: an outcome's `recorded_at`, the quarantine's `recorded_at`, the retraction or Source Conflict selection's `recorded_at`, a failed job's `closed_at`, and an unnamed retraction's `indexed_at`. The earliest witness sets an item's time, so later outcomes and failures never move it.
  - Settlements of other types never count.
- **Summary evidence (R67).**
  - `after` is an integer of at least 0, and `limit` an integer of at least 1. `datetime` bounds and a malformed target are `summary-request-invalid`.
  - *Settles:* `source_selection_policies` are the records `policy()` returns now, so `superseded_by` may name a policy above the horizon. Policies are ordered by first date, an unbounded start first.
  - `next_after` is the last identity when another record follows. The tests avoid the exactly-at-limit case, so a look-ahead and a "full page" rule both pass.
- **Reads (R68).** `processor_generation` returns `None` for 0, negatives, and unknown identities. `refresh_counts` with a naive time is `timestamp-invalid`.
- **Surfaces (R69).** The operations emit no log record at any level. Refusals carry no key, date, identity, or marker in `repr` or `str`.
- **Races (R70).** A racing request that gets `store-busy` retries, so the totals are exact: `RefreshRequest(1, 2n - 1, 0)` over n rounds, and the next claim's token is 2n.

**Left open, not pinned.** These are ambiguities in the approved text; the suite accepts either reading:
- whether a non-exhausted expired lease whose token advanced resets its attempts at claim;
- whether a request with no keys, whose acknowledgement and link change nothing, needs the reserve;
- argument validation for `evidence_changes(limit)` and `processed_positions`;
- whether `next_after` is set on an exactly full last page.

The reviewer would reset the retry budget on every stale expiry, to match "newer evidence gets a fresh run". This is recorded as a note only; the suite still allows a partly used budget.

**Implementer review resolutions.** GPT 6.1 Sol reviewed the pack read-only at `08507be`, with an independent implementation that passed all 289 tests. Sol found 2 blocking and 7 should-fix findings, and gave 2 notes. They are resolved as follows:
1. **Blocking: R66 positions could move back.** A manual retry after a failed job, and a later projection of a retracted identity, each removed a position. R66 now defines settlement historically: witnesses are append-only, any failed job is a witness, and a retraction's matching records are fixed as of its indexing.
   - Added `test_a_failure_witness_survives_a_retry` and `test_retraction_matches_records_as_of_its_indexing`. Both reopen the store and complete a refresh whose watermark is the resulting positions.
   - **For the approver and Operator: this changes approved semantics.** The approved text said "whose last job under the version failed" and "an indexed retraction that names no Source Record". It now says "any failed job" and "no record observed before the retraction". The watermark now asserts terminal processing through the prefix, not current success.
2. **Blocking: "first became settled" cannot be recovered.** Caller times need not be monotone, and no commit order is shared across selections, outcomes, and failed jobs. Sol showed two orders that end in identical rows. An item's settlement time is now the earliest caller-supplied witness time, and `processed_at` may fall if a later write supplies an earlier timestamp.
   - Added `test_settlement_time_is_the_earliest_witness`, which applies both orders to copies of one prestate.
   - The equal-sequence oracle now notes that only a forward-moving clock keeps `processed_at` fixed.
   - **This also changes approved semantics**, from "first became settled" to "earliest witness timestamp".
3. **Generation immutability.** The physical check now covers only the rows registration added that carry the generation's digest or manifest values, so allocator metadata such as `sqlite_sequence` may change. Whole-database comparisons for no-write calls are unchanged.
4. **"Indexed by outcome."**
   - The column-name check is dropped.
   - A schema-agnostic check now requires one link row per contributor, carrying the outcome and summary identities. It kills the skipped-link survivor.
   - The relation and its leading outcome index are an approver residual. No public read looks summaries up by outcome, and none is added.
5. **Claim race.** Workers retry only `store-busy`, within a bounded loop, and count successful claims. The exactly-once assertions are kept.
6. **Drain loops.** The main-thread loops are bounded by the expected jobs plus the final `None`, with unique-job and fence checks. The claim worker is bounded as well.
7. **RFC 8785.**
   - Keys or versions with lone surrogates are `processor-generation-invalid`, and the whole database is unchanged. This adds a refusal case to R56; a lone-surrogate version was previously `processor-version-invalid` by grammar.
   - Added handwritten vectors: `\b \t \n \f \r`, `\u0001 \u001b \u001f` in lowercase, DEL and `/` unescaped, and composed and decomposed `é` kept distinct.
8. **Repeated hour.** A fold-aware synthetic zone is used in R61. The same instant is accepted and stored in UTC; the other fold is refused. R61 now says times are compared as instants.
9. **One retraction, several records.** Every record in the as-of-indexing matching set must have a witness. Added `test_retraction_needs_every_matching_record`, with two origins and then a third origin observed later.
10. **Note: unpinned choices.** They stay unpinned. Sol's preference is recorded above.
11. **Note: bookmark, migration, fixture, and earlier versions.** No change needed.

The implementer then reviews the pack read-only for tests a correct implementation could not pass, and those findings are resolved before freeze.

## Residuals

Not mechanically provable here; the approver confirms these in implementation review:
- migration 5 only creates tables and indexes;
- no `@1`–`@5` code path changes;
- the store never parses summary header, evidence, or body bytes, or a policy identity;
- contributor links are a relation of (outcome, summary) rows with an index that leads with the outcome identity, so every summary a revision contributed to can be found (R62). No `@6` read exposes this lookup; the suite only checks that the link rows exist;
- each write's checks, token advance, job changes, links, and bookmark happen in one SQLite write transaction, with no process-local lock. The race tests are bounded stress and cannot force an interleaving;
- `processed_positions`, `summary_evidence`, `evidence_changes`, the watermark check, and the counts are set-based SQL over indexes, with no per-record Python loop and no bytes loaded except the outcomes `summary_evidence` returns. As in DEV-os4-01, positions and change reads may grow with retained history. The `portal-worker` calls them per refresh cycle, not in a tight loop.

Left to other units:
- which keys and target an evidence change or policy affects, attribution, derivation, and when to request: `refresh-summary-state` and the derive units;
- whether the indexed fields match the bytes: `refresh-summary-state`'s pack, through `summary_contract.decode_summary`;
- which generation and timeline processing uses: `execute-processing-job@2`;
- Cutover Targets, Candidate Builds, current selectors, activation, Cutover Records, ordinary set publication, publication conditions, and choosing outcomes by policy state: `@7` and `refresh-summary-state@2`;
- review tokens, rejection, Compound Cutover, and reconciliation facts: `@8`;
- Amendments, including excluding amended records from summary evidence, the presentation reads, and per-type unplaced evidence counts: `@9`;
- erasing summaries, contributor links, refresh attempts, and acknowledgements under privacy deletion: `@10`.

Conflicts in the scope map that `@6` does not settle, for `@7`'s draft:
- ordinary publication appears in two rows: `refresh-summary-state` publishes each revision, while `complete-change-cycle` publishes the affected set atomically;
- activation appears in the `refresh-summary-state` row and in the `complete-*` and `activate-processor-generation` rows;
- "evidence token" means both a per-identity coalescing token (`@6`) and the newest complete evidence boundary (`@7`);
- reconciliation items have no store version until `@8`.
