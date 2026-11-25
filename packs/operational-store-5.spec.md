# Draft acceptance pack — `operational-store@5`

**Status:** Draft approved by the Operator on 2026-11-25, including the 16 decisions below as recommended; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `operational-store` Governed Scope contract, version 5, in `governance/scopes/operational-store.5.json`. It is the additive successor to the published `operational-store@4`, which it supersedes.

## What `@5` adds

`@4` records processing outcomes, but nothing records the Operator's policies. `execute-processing-job@1` therefore passes an empty Fallback Timezone Policy timeline, and every record without a source offset is Policy-Pending (its Decision 10). `@5` adds:
- **Policy records.** The Operator's Fallback Timezone Policies (ADR 0025) and Source Selection Policies (ADR 0026), append-only and pending, with explicit supersession.
- **Policy reads.** Policy history, the Fallback Timezone timeline in the exact shape `interpret-source-projection@1` takes, and the Source Selection Policy that applies to one measure, record type, and local date.
- **Known origins.** The origins seen for each record type, which the Operator chooses among when setting a Source Selection Policy.
- **Reprocessing with the policy.** Recording a Fallback Timezone Policy queues, in the same transaction, a Processing Job for every Source Record whose interpretation used or needed a fallback timezone.

The setters own the Operator's flow: `set-fallback-timezone-policy` proposes and confirms a timezone, and `set-source-selection-policy` offers the origins and records the choice. This store only records and reads what they confirm. "Pending" here means requested by the Operator and not yet active. It never means awaiting the Operator's confirmation.

`@5` does **not** add Summary Refresh Jobs. The pending policy itself is the durable record that affected summaries need rebuilding. `@6` adds Summary Refresh Jobs, Processor Generations, Cutover Records, and policy activation, and derives each policy's summary work from it (Decision 1).

## What stays unchanged

- Every `@1`–`@4` operation, read, type, and refusal is unchanged, with the one exception below. `FAILURES` gains this version's names and `MIGRATIONS` gains migration 4.
- One `@4` rule gains a case that only `@5` can create: a job whose lease was held when a Fallback Timezone Policy was recorded is queued again when that lease completes, instead of completed (R48, Decision 4). Without a policy write, completion behaves exactly as in `@4`.
- Migration 4 only creates tables and indexes. It inserts no row, alters no earlier table, and backfills nothing.
- Policy writes never change a table created by migrations 1 or 2. In `@4`'s tables they only add queued jobs.

## Interface

The module `activity_tracker_portal.operational_store` adds these frozen dataclasses:
- `OriginKey(source_system_kind, origin)`: one origin, as a Source Record key names it.
- `FallbackTimezonePolicyRecord`: `policy_id`, `zone` (an IANA name), `effective_from` (a UTC `datetime`, or `None` for all earlier history), `supersedes` (the policy identities it replaced, ascending), `superseded_by` (`None` while live), and `requested_at`.
- `SourceSelectionPolicyRecord`: `policy_id`, `measure` (a derived measure, such as `daily-steps`), `record_type`, `origins` (a sorted tuple of `OriginKey`), `first_date` and `last_date` (inclusive local dates, each `None` when unbounded), `supersedes`, `superseded_by`, and `requested_at`.
- `FallbackTimezoneRecording(policy, jobs_enqueued, jobs_rearmed)`: the recorded policy and two health-data-free counts.
- `KnownOrigin(source_system_kind, record_type, origin, record_count)`.

It adds these operations:
- `Store.record_fallback_timezone_policy(zone, effective_from, supersedes, processor_versions, max_attempts, at) -> FallbackTimezoneRecording`
- `Store.record_source_selection_policy(measure, record_type, origins, first_date, last_date, supersedes, at) -> SourceSelectionPolicyRecord`
- `StoreView.policy(policy_id) -> FallbackTimezonePolicyRecord | SourceSelectionPolicyRecord | None`
- `StoreView.fallback_timezone_policies() -> tuple[FallbackTimezonePolicyRecord, ...]`
- `StoreView.fallback_timezone_timeline() -> tuple[FallbackTimezonePolicyRecord, ...]`
- `StoreView.source_selection_policies(measure=None, record_type=None) -> tuple[SourceSelectionPolicyRecord, ...]`
- `StoreView.source_selection_timeline(measure, record_type) -> tuple[SourceSelectionPolicyRecord, ...]`
- `StoreView.applicable_source_selection(measure, record_type, on) -> SourceSelectionPolicyRecord | None`
- `StoreView.known_origins(record_type=None) -> tuple[KnownOrigin, ...]`

A policy is **live** while no later policy supersedes it. Both kinds share one ascending `policy_id` sequence, so identities give arrival order. Every time argument is timezone-aware and comes from the caller; the store never reads a clock.

The store does not import `interpret-source-projection`, because `operational-store` depends on nothing. A caller turns each timeline entry into that unit's `FallbackTimezonePolicy(str(policy_id), effective_from, zone)` (Decision 5).

## Requirements

`operational-store@1`–`@4`'s requirements R1–R40 carry over. The `@5` runner re-runs `@4`'s bound runner unchanged, so every earlier suite must still pass exactly.

### R41 — Governed Scope contract, version 5

The `@5` contract keeps `@4`'s inputs and modes, observable effect, and empty dependency list.
- Its named failures begin with `@4`'s, in order and with the same outcome classes, followed by five new invariant refusals: `policy-request-invalid`, `policy-unknown`, `policy-supersession-invalid`, `policy-overlap`, and `policy-origin-unknown`.
- Its outputs keep `@4`'s names and add `policy_record` and `policy_reprocessing`.
- Its `owns` list is `@4`'s, unchanged, with the policy entries appended. Summary Refresh Jobs and policy activation stay in `does_not_own`.
- It does not contradict the `operational-store` scope-map row.

Proposed tests: `test_contract_5_extends_version_4`, `test_scope_map_row_does_not_contradict_contract_5`, `test_failures_match_contract_5`.

### R42 — Migration 4 is additive

- `MIGRATIONS` is `@4`'s migrations followed by migration 4. Each statement of migration 4 is a `CREATE TABLE` or `CREATE [UNIQUE] INDEX`.
- A new database records migrations 1–4 with their checksums and `user_version` 4. Migrations 1–3 keep the checksums the published `@4` module recorded.
- A database at migration 3, such as one written by `@4`, refuses with `store-migration-required` and is left unchanged.
- Brought to migration 4 with no backfill, every earlier row is unchanged and every new table is empty. It then opens, and:
  - `known_origins` reports the fixture's origins;
  - a Fallback Timezone Policy queues exactly the affected records (R47) and marks the fixture's leased job (R48).

The test uses a new fixture, a dump of a synthetic database written by the published `@4` module.

Proposed tests: `test_new_database_records_migrations_1_to_4`, `test_version_4_database_requires_migration_and_is_untouched`, `test_migrated_version_4_database_takes_policies`.

### R43 — Recording a Fallback Timezone Policy

`record_fallback_timezone_policy` appends one pending policy: from `effective_from` onward, or for all earlier history when it is `None`, records without a source offset are attributed in `zone`.
- `supersedes` names the live policies it replaces (R45). It may be empty.
- `processor_versions` maps each record type to the processor version to reprocess under (R47). It may be empty.
- **Refusals,** each leaving the database unchanged:
  - `timestamp-invalid` for a naive `at` or `effective_from`;
  - `policy-request-invalid` for a zone that is not a well-formed IANA name (Decision 9), a `supersedes` that is not a tuple of distinct positive integers, or a mapping key that is not a non-empty string;
  - `processor-version-invalid` for a mapping value not of the form `<scope>@<version>`;
  - `processing-request-invalid` when `max_attempts` is not an integer from 1 to 100;
  - `policy-unknown`, `policy-supersession-invalid`, and `policy-overlap` (R45);
  - `storage-reserve-exhausted` when anything would be written (R46).

Proposed tests: `test_fallback_timezone_policy_is_recorded_pending`, `test_all_earlier_history_policy`, `test_fallback_timezone_refusals`.

### R44 — Recording a Source Selection Policy

`record_source_selection_policy` appends one pending policy: for `measure` and `record_type`, on each local date from `first_date` through `last_date`, only `origins` may contribute. One origin selects it; more than one explicitly permits their combination (ADR 0026).
- It queues no Processing Job: source selection governs aggregates, not interpretation.
- **Refusals,** each leaving the database unchanged:
  - `timestamp-invalid`;
  - `policy-request-invalid` for a measure that is not lowercase kebab-case of at most 64 characters, an empty or non-string record type, an empty or duplicated origin list, a non-`OriginKey` origin, a bound that is not a `date`, or a `last_date` before `first_date`;
  - `policy-origin-unknown` when an origin has no Source Record of that record type (Decision 10);
  - `policy-unknown`, `policy-supersession-invalid`, and `policy-overlap` (R45);
  - `storage-reserve-exhausted`.

Proposed tests: `test_source_selection_policy_is_recorded_pending`, `test_combination_needs_several_origins`, `test_source_selection_refusals`, `test_unknown_origin_is_refused`, `test_source_selection_queues_no_job`.

### R45 — Supersession and overlap

Supersession is explicit: the caller names what a new policy replaces (Decision 7).
- `policy-unknown` when a named policy does not exist.
- `policy-supersession-invalid` when a named policy is of the other kind, is already superseded, or, for Source Selection, has a different measure or record type.
- `policy-overlap` when, after the named policies are superseded, the new policy would apply at the same time as a live one:
  - Fallback Timezone: another live policy starts at the same instant, or both are `None`;
  - Source Selection: another live policy with the same measure and record type shares a local date.
- On success the new policy records each named policy as superseded by it. Superseded policies stay in the history, never change, and never return to live.

Proposed tests: `test_supersession_replaces_named_policies`, `test_supersession_refusals`, `test_overlapping_timezone_start_is_refused`, `test_overlapping_selection_dates_are_refused`, `test_disjoint_selection_intervals_coexist`, `test_superseded_policy_stays_in_history`.

### R46 — Identical repeats and the storage reserve

- **Identical repeat.** A request equal to a live policy returns that policy and writes nothing. Equal means the same kind and `supersedes`, plus the same zone and start for a Fallback Timezone Policy, or the same measure, record type, origins, and dates for a Source Selection Policy. A Fallback Timezone repeat reports zero jobs enqueued and rearmed. This lets a setter retry after an uncertain commit (Decision 11).
- **Reserve.** Every policy write is gated by the storage reserve, as in `@4` Decision 12. An identical repeat writes nothing and succeeds below the reserve.

Proposed tests: `test_identical_policy_repeat_writes_nothing`, `test_policy_writes_are_gated_by_the_reserve`.

### R47 — Affected reprocessing, queued with the policy

In the same transaction as a Fallback Timezone Policy, for each record type and processor version in `processor_versions`, the store queues a job for every Source Record of that type that:
- has a projection as its latest selection;
- has a latest outcome under that version for that selection, as `latest_outcome` reads it, which either has a policy identity other than `"none"` or is Policy-Pending with policy kind `fallback-timezone`; and
- has no open job for that version.

Each new job targets the latest selection, with the given `max_attempts`, `not_before` equal to `at`, and no attempts, exactly like `enqueue_processing`. A record with a queued job gets nothing more, because its claim will read the new timeline.

Not affected (Decision 3):
- records whose outcomes used no policy, because explicit offsets stay authoritative (ADR 0025);
- records with no outcome yet, which `selections_awaiting_processing` already finds;
- records whose failed job left no affected outcome: a failed job does not create work. Existing failed jobs stay failed (`@4` R35). A record whose latest outcome is affected gets a separate new job, even if a later retry of it failed;
- record types missing from the mapping.

`jobs_enqueued` counts the new jobs. A Source Selection Policy queues nothing.

Proposed tests: `test_policy_queues_records_that_used_a_fallback`, `test_policy_queues_policy_pending_records`, `test_experienced_records_are_not_queued`, `test_open_jobs_are_not_duplicated`, `test_failed_and_unprocessed_records_are_not_queued`, `test_only_mapped_types_are_queued`, `test_queued_policy_job_reprocesses_to_a_new_outcome`.

### R48 — Jobs leased during a policy write run again

A job leased when a Fallback Timezone Policy is recorded may be interpreting under the old timeline. So, in the same transaction, the store marks every leased job's current lease (Decision 4).
- When that same lease completes and `@4` would mark the job `completed`, the job is instead queued again for the same selection, with `not_before` equal to the completion's `at` and its attempts reset. The outcome is recorded as usual.
- The mark applies only to the lease held at the write. If the lease expires and another claim takes the job, the new holder reads the new timeline and the mark has no effect.
- A newer selection or a retraction still re-queues or supersedes the job as in `@4` R33. A failure follows `@4` R35.
- `jobs_rearmed` counts the marked leases.

Proposed tests: `test_lease_held_at_policy_write_is_requeued_on_completion`, `test_reclaimed_lease_ignores_the_mark`, `test_mark_does_not_change_failure_or_newer_selection`, `test_completion_without_a_policy_write_is_unchanged`.

### R49 — Policy reads

- `policy` returns `None` for an unknown identity.
- `fallback_timezone_policies` and `source_selection_policies` list every policy of that kind, live and superseded, in arrival order. The Source Selection list may be filtered by measure, record type, or both.
- `fallback_timezone_timeline` lists the live Fallback Timezone Policies, `None` first and then by `effective_from`. This is exactly the shape `interpret-source-projection@1` validates: strictly increasing starts, and `None` only first.
- `source_selection_timeline(measure, record_type)` lists the live policies for that scope, unbounded first and then by `first_date`.
- `applicable_source_selection(measure, record_type, on)` returns the live policy whose dates include `on`, or `None`. With no applicable policy, the aggregate is unresolved when origins overlap; that judgement belongs to the summary units (ADR 0026).
- Every policy read reports pending policies. Which policies are active is `@6`'s read (Decision 8).
- Each read comes from one read transaction and writes nothing. Returned times are UTC.

Proposed tests: `test_policy_history_in_arrival_order`, `test_timeline_matches_the_interpreter_shape`, `test_timeline_drops_superseded_policies`, `test_applicable_source_selection_by_date`, `test_policy_reads_write_nothing`.

### R50 — Known origins

`known_origins(record_type)` lists each distinct source-system kind, record type, and origin among the Source Records, with its record count, ordered by record type, then kind, then origin. `None` lists every record type. Origins never leave the list, because `@3` never deletes a Source Record. It reads `@3`'s Source Records, with one new index and no new table (Decision 12).

Proposed tests: `test_known_origins_by_record_type`, `test_known_origins_count_records`.

### R51 — Health-data-free surfaces

Policy records hold no projection, header, or body bytes and no Source Identity. Refusal text is the failure name only. Tests use synthetic markers in Source Identities and bytes to prove that no new type's `repr` or refusal carries them.

Proposed tests: `test_policy_surfaces_are_health_data_free`.

### R52 — Atomic, durable, and serialized

- Every policy write is all-or-nothing, including its jobs and lease marks. A fault injected after any row write commits nothing.
- Each refusal leaves the whole database unchanged.
- Policies, supersessions, queued jobs, and lease marks read the same after the store is closed and reopened.
- Two handles racing to record overlapping policies never leave both live.

Proposed tests: `test_policy_writes_are_atomic_under_injected_failure`, `test_policy_refusals_leave_database_unchanged`, `test_policy_state_survives_reopen`, `test_racing_overlapping_policies_leave_one_live`.

### R53 — `@1`–`@4` unchanged

Every earlier operation and refusal behaves as before. The runner proves this by re-running `@4`'s bound runner. A test also proves that policy writes never change a table created by migrations 1 or 2, and change `@4`'s tables only by inserting queued jobs.

Proposed tests: `test_policy_writes_change_no_earlier_table`.

## Decisions for the Operator

1. **Summary-refresh work waits for `@6`; the pending policy is the request.** ADR 0026 requires that recording a policy and requesting every affected summary rebuild happen together. `@5` meets that because the pending policy row is itself the durable request: it names the measure, record type, and dates whose summaries need rebuilding, in arrival order. `@6` adds Summary Refresh Jobs and turns each pending policy into its Cutover Target and rebuild work, as ADR 0043 orders policy requests. `@5` cannot define the queue now:
   - a Summary Refresh Job names a Processor Generation (glossary), which arrives in `@6`;
   - a summary's identity comes from `summary-contract`, which is not drafted;
   - its only consumer, `refresh-summary-state`, comes in Phase 5.

   Alternatives:
   - a minimal `summary_refresh_requests` table now. It would copy the policy's scope and wait unused, and `@6` would have to adopt its shape before its consumer exists.
   - full Summary Refresh Jobs now, which would invent `refresh-summary-state`'s behaviour.

   Consequence: the `@5` line in `docs/governance/delivery-flow.md` and the description of bead `8p9.2` should then say "summary rebuilding is derived from pending policies in `@6`". `set-source-selection-policy@1` records the policy only.
2. **A Fallback Timezone Policy queues reprocessing now.** This is what `execute-processing-job@1` Decision 10 and the `set-fallback-timezone-policy` scope row expect. Outcomes are keyed by policy identity and nothing publishes before `@6`, so early reprocessing only adds candidate outcomes. The alternative defers all queueing to `@6`'s policy Cutover Target, which follows ADR 0043's "target before work" more literally. But it leaves `@5` with records only, and `execute-processing-job@2` with nothing to reprocess until `@6`.
3. **"Affected" means: the latest outcome used a fallback timezone or needed one.** Concretely, its policy identity is not `"none"`, or it is Policy-Pending for `fallback-timezone`. This is simple and never misses a record. Records whose times all carry source offsets are never touched. It may re-run records the change does not reach, and those repeat their outcome harmlessly (`@4` R33). The alternatives:
   - narrow by the policy's time window using the revisions' indexed instants. That is less work, but Policy-Pending outcomes carry no instants, and the window depends on neighbouring policies;
   - reprocess every record. That is simpler still, but wasteful.
4. **A lease held during the write runs again.** Without this, a job claimed just before the write would finish under the old timeline, and the record would never be reprocessed. The store marks each held lease in the same transaction, and completion re-queues a marked job (R48). This is the one place a `@4` rule gains a case. The alternatives:
   - the worker checks for a policy change before completing. Its check and its completion are separate transactions, so a write in between is still lost;
   - refuse policy writes while any job is leased. During a large backlog the Operator's request could wait indefinitely.
5. **What a policy holds.**
   - **Fallback Timezone:** a zone and an effective start, or all earlier history. It is not scoped by record type, origin, or Sender Installation: it is the Subject's timezone (ADR 0025), and Source Records are independent of installations (ADR 0050). It stores no tzdata version, because `interpret-source-projection@1` puts the pinned tzdata release in every policy identity it produces. Upgrading tzdata is a governed reinterpretation (ADR 0035).
   - **Source Selection:** a derived measure, a record type, one or more origins, and optional inclusive dates (ADR 0026).
   - **Identity:** one ascending integer sequence across both kinds, giving arrival order. Callers pass `str(policy_id)` to `interpret-source-projection@1`, so policy identities read like `fallback-timezone:[["3"],"2026f"]`.

   Alternatives: store the tzdata release with each policy, for audit only; or give each kind its own sequence, which loses one arrival order.
6. **Source Selection dates are local dates, not instants.** Every summary is a local-date result: daily Steps and Heart Rate, wake-date Sleep, latest-local-day Weight. Ranges compose by date. Date bounds mean a day is never split between two policies. The alternative, instants, matches the Fallback Timezone Policy but leaves `summary-contract` to resolve a day that two policies share. This choice binds `set-source-selection-policy@1` and `summary-contract@1`.
7. **The caller names what a policy supersedes, and the store keeps live policies from overlapping.** The setter decides what a backdated timezone change or a narrower selection replaces. The store only guarantees that the result is unambiguous: one timezone per instant and one selection per date. The alternative is that the store decides, for example "a newer timezone replaces every policy starting at or after it". That puts a setter's meaning into the shared store.
8. **Every policy `@5` records is pending.** Activation needs rebuilt results and a Cutover Record (ADR 0026, 0043, 0044), which arrive in `@6`. `@5`'s timeline therefore lists live requested policies, and `@6` adds the active timeline. Which of the two a processor uses while a policy is pending is for `execute-processing-job@2` and `@6` to decide. The alternative treats recorded policies as active until `@6`. That contradicts the approved rule that a policy activates only with its rebuilt results.
9. **The store checks the zone's form only.** `set-fallback-timezone-policy` validates it against the pinned tzdata. The store uses only the standard library, and the host's timezone database differs from the pinned one. Consequence: a well-formed but unknown zone would make `interpret` refuse every input as `processing-input-invalid`, so the setter's check is load-bearing. The alternative adds `tzdata` to the store's dependencies.
10. **Selected origins must already be known for that record type.** This catches mistakes and keeps every selection tied to evidence. The alternative allows origins not yet seen, so the Operator could choose in advance. That is easy to add later and hard to take back.
11. **Identical repeats are recognized by content.** A request equal to a live policy, including what it supersedes, returns that policy. No token is needed. The alternative is a caller-supplied request key, which every setter would have to create and keep.
12. **Known origins are read from Source Records.** There is no new table, only one index. The alternative, a known-origins table filled during indexing, changes `@3`'s `index_items` and needs a rebind of `@3`.
13. **The storage reserve gates every policy write**, as for every earlier write. A policy that queues many jobs is one large write.
14. **Existing databases must be recreated.** A migration-3 database refuses to open until `migrate-operational-store` arrives in Phase 8, as `@4` Decision 16 chose. No production database exists.
15. **The runner chains.** The `@5` runner runs `@4`'s bound runner unchanged, then the new suite.
16. **Two waiting store follow-ups stay out of `@5`.** Bead `2rs` asks for `after` cursors on the three discovery reads, and bead `ny5` for a narrow read of a job's projection-schema version. Both change interfaces outside policies, and the worker already works around both. Keeping them out keeps `@5` reviewable, as the Operator's split intended. The alternative is to fold them in now and save a later store version.

## Predecessor and dependents

- Every dependent of `@4` is published, the last being `execute-processing-job@1` on 2026-11-25, so `@5` may start (delivery flow, sequencing rules).
- Superseding `@4` leaves every published dependent published, as superseding `@3` did. Seven units depend directly on `operational-store`: the four `accept-*` units, `manage-sender-installation`, `reconcile-source-record`, and `execute-processing-job`. While `@5` is unpublished, a new or re-verified dependent cannot publish. Today that holds back only `@5`'s own dependents: `set-source-selection-policy`, `set-fallback-timezone-policy`, `execute-processing-job@2`, and `operational-store@6`.
- **The DEV-os4-01 lesson.** Amending `@5` after freeze (`respecify`) would invalidate the published dependents, as `@4`'s amendment invalidated seven on 2026-11-25. Each would need `begin-reverify`, a fresh GREEN, and republication in dependency order after `@5` publishes. That is now up to 13 units: the seven above, `receipt-bound-delivery`, `interpret-source-projection`, and the four processors. So the read-only review and Gate B should settle everything before freeze. No re-verification is expected from `supersede` or `specify` themselves.

## Ground rules

These are `@4`'s ground rules. The trusted boundary is unchanged: standard-library SQLite only, synthetic values only, real databases in temporary directories, and whole-database snapshots for every "writes nothing" and refusal assertion. Source Records, selections, and outcomes are built through `@3`'s and `@4`'s own operations, with hand-written outcomes. Zones in tests are real IANA names, but the store never resolves them.

Unbound inputs, reviewed rather than frozen:
- `docs/governance/scope-map.md`, which R41's scope-map test reads;
- the execution environment (`.python-version`, `pyproject.toml`, `uv.lock`, and the managed Python and SQLite runtime), which CI pins;
- `tests/governance/test_operational_store_5_contract.py`, which runs in the governance suite;
- `tests/unit/test_operational_store_hardening.py`, which checks that a new database records every known migration. It is supplementary, not bound acceptance evidence.
- the inputs of R49's integration check, which shows that the timeline is exactly what `interpret` accepts. Each is bound by its own unit:
  - `projection/v1/vectors/positive.json`;
  - the published `interpret-source-projection@1` and `interpret-steps@1` modules;
  - the projection schema loader `_interpret_projection_schema.py`, its JSON helper `_interpret_projection_json.py`, and the schema resources it loads from `projection/v1/schemas/`;
  - the pinned `tzdata` package.

## Freeze prerequisites

After Gate A, the executable pack adds:
- **The suite.** `tests/acceptance/test_operational_store_5.py` has 50 tests across R41–R53. Every proposed test name above is present, plus four more in R48:
  - `test_every_held_lease_is_marked`;
  - `test_marked_completion_reusing_an_outcome_is_requeued`;
  - `test_refused_marked_completion_keeps_its_mark`;
  - `test_each_policy_write_counts_every_held_lease`.

  The last three come from the implementer's review.
- **The fixture.** `tests/acceptance/fixtures/operational-store-4-processing.sql` was written as the `@3` fixture was. A throwaway script imported the published `@4` module, extracted with `git archive 6c64dbd` (the module is unchanged since `ed7b431`), and wrote a database through its public API only: enrollment, archival, `index_items`, `record_selection`, `enqueue_processing`, `claim_processing_job`, `complete_processing_job`, and `fail_processing_job`. The database was dumped with `sqlite3` `iterdump`, and `PRAGMA user_version=3` was appended. The script stays outside the repository; the file's header comment records the method. The fixture holds:
  - Source Records from two steps origins (5 and 2 records) and one weight origin (2 records);
  - an inferred and an experienced steps revision, a Policy-Pending `fallback-timezone` outcome with identity `"none"`, an inferred weight revision, a weight quarantine, and an inferred revision whose record was later retracted;
  - a failed job with no outcome, a leased job held for a day with no outcome, and a queued job with no outcome;
  - one item that is never indexed.
- **The runner.** `tests/acceptance/run_operational_store_5.sh` runs `@4`'s bound runner unchanged (it runs `@3`'s, `@2`'s and `@1`'s), then the new suite.
- **The Interface stub,** added to the published module:
  - five real frozen dataclasses: `OriginKey`, `FallbackTimezonePolicyRecord`, `SourceSelectionPolicyRecord`, `FallbackTimezoneRecording`, and `KnownOrigin`;
  - nine operations, each raising `NotImplementedError("operational-store@5")`: two on `OperationalStore` and seven on `StoreView`.

  The stub adds no `FAILURES` entries, no migration, and no change to `complete_processing_job`. The implementer adds all three.

**RED, through the runner, against the stub:**
- `@1`–`@4`'s 172 bound tests (53, 15, 45, and 59) pass.
- Of the 50 new tests, 3 pass:
  - the two pure contract tests;
  - `test_completion_without_a_policy_write_is_unchanged`, which uses only `@4` operations and guards against re-queueing every completion.
- 4 fail on an `AssertionError`, because the stub has no new names and no migration 4:
  - `test_failures_match_contract_5`;
  - `test_new_database_records_migrations_1_to_4`;
  - `test_version_4_database_requires_migration_and_is_untouched`, because the stub opens a migration-3 database without refusing;
  - `test_migrated_version_4_database_takes_policies`, because no table is added.
- The other 43 fail with `NotImplementedError("operational-store@5")`. The race test re-raises its workers' unexpected errors, so its failure is the stub's too.

In the worktree, with the stub, these all pass:
- `tests/unit`, including `test_operational_store_hardening.py`. That test compares new databases with the store's own `MIGRATIONS`, so it passes with three migrations now and with four after implementation;
- `tests/governance`;
- the published runners that chain off the store: `execute-processing-job@1`, `reconcile-source-record@1`, the four `accept-*` units, `manage-sender-installation@1`, `receipt-bound-delivery@1`, and `interpret-source-projection@1`.

**Passability.** A throwaway reference, kept out of the repository, passes all five suites (`@1`–`@5`), `tests/unit`, `tests/governance`, and the dependent runners above. It has:
- migration 4: six tables and four indexes, one of them on `source_records`;
- the five names;
- both writes and the seven reads;
- the marked-lease case in `complete_processing_job`.

**Mutants.** The drafter ran 64 wrong-implementation mutants against the reference. The suite kills 63 of them, and it still does after the review changes. They cover:
- a backfilling migration, and one that alters an earlier table;
- a split, non-atomic policy write;
- "affected" too wide (every outcome; any Policy-Pending kind; any processor version; the record's latest outcome over any selection) and too narrow (missing Policy-Pending; missing Quarantine and Unmodeled; the first outcome instead of the latest; skipping records with failed jobs);
- duplicated open jobs, and an ignored retry bound;
- a missed mark, and marks limited to mapped versions, unexpired leases, or affected records;
- re-queueing an unmarked lease or every completion after a policy; a re-queue that keeps attempts or the old `not_before`; a marked lease that completes; and a mark checked before a newer selection;
- overlap allowed, overlap with exclusive date bounds, `None` starts exempt, and overlap that ignores supersession;
- supersession of unknown, other-kind, or other-scope policies, and an unknown policy reported as invalid;
- repeats written twice, repeats that ignore `supersedes` or origin order, and repeats that report counts;
- reserve bypass on either write, and a reserve check before the repeat check;
- timelines in arrival order, with all earlier history last, with superseded policies, or with starts stored in their local offset;
- the applicable read ignoring supersession or using an exclusive last date;
- unknown origins allowed, or known for any record type;
- "active instead of pending": a new policy that supersedes every live one, and a requested time taken from the start;
- a missing `superseded_by`, and history without superseded policies;
- values leaked into refusal `args` or text;
- a lax zone form, and zones checked against the host database (Decision 9);
- reversed dates and duplicate origins allowed;
- known origins that ignore the kind or count evidence, and unsorted origins;
- a Source Selection write that marks leases.

The one survivor is equivalent. It drops the "latest selection is a projection" condition from the affected query, but `@4` records outcomes only for projection selections, so a retraction or Source Conflict has no outcome to match.

Three gaps were found and closed during this run:
- two mutants that re-queue unmarked completions made a drain loop spin forever, so the suite hung instead of failing. Both loops are now bounded and fail with a message;
- a repeat test sent origins already in sorted order, so a mutant that compared unsorted origins survived. The repeat now uses another order.

**Refinements.** The tests pin the refinements below beyond the approved text. None changes approved semantics; items that settle a point the text left open are marked *settles*.
- **Request shapes (R43, R44).**
  - `supersedes` is a tuple of distinct positive `int`s, on both writes. These are `policy-request-invalid`:
    - zero, negatives, `str`, `float`, `None`, and duplicate elements;
    - a list, a set, a scalar, or `None` in place of the tuple.

    A record's `supersedes` is ascending.
  - A zone is one or more segments of ASCII letters, digits, `_`, `+`, and `-`, joined by single `/`. A leading, trailing, or doubled `/`, a `.` or `..` segment, a backslash, a space, a control character, and non-ASCII are `policy-request-invalid`. Control characters include a final or leading newline (`"UTC\n"`), CR and CRLF, tab, and DEL, so the whole string must match. `UTC`, `Etc/GMT+5`, `America/Argentina/Buenos_Aires`, `America/Port-au-Prince`, and the well-formed but unknown `Synthetic/Unknown_Zone` are accepted (Decision 9).
  - A mapping key that is empty or not a string is `policy-request-invalid`. Mapping values follow `@4`'s processor-version grammar. `max_attempts` follows `@4`: 1 and 100 are accepted, and 0, 101, `2.5`, and `"3"` are refused.
  - A measure follows `@4`'s reason rule: lowercase kebab-case of at most 64 characters.
  - Origins are a tuple of `OriginKey` whose fields are strings. These are `policy-request-invalid`: an empty tuple, a duplicate, a plain tuple or string in place of an `OriginKey`, a non-string field, and a list in place of the tuple. A record's origins are sorted by kind, then origin, in code point order.
  - A date bound given as a string is `policy-request-invalid`. A single-day policy, with the first date equal to the last, is valid.
  - **For the approver: settles a point the approved text left open.** A `datetime` in either bound, aware or naive, is `policy-request-invalid`, and nothing is written. Decision 6 makes the bounds local dates, and `datetime` is a subclass of `date`.
- **Times.** `effective_from` and `requested_at` are returned in UTC. The same instant written in another offset is the same start, for both repeats and overlap. A microsecond apart is a different start.
- **Known origin (R44, R50).** "Known" means a Source Record with exactly that source-system kind, origin, and record type. `record_count` counts Source Records, including retracted and never-selected ones. A retraction with no Source Record adds no origin. Rows are ordered by record type, kind, and origin in code point order.
- **Supersession (R45).**
  - A policy may supersede a live policy it does not overlap.
  - Overlap is checked after the named policies are removed.
  - A list that mixes a valid and an unknown identity is `policy-unknown`.
  - **For the approver: settles a point the approved text left open.** In a well-formed tuple, `policy-unknown` wins over `policy-supersession-invalid`. This holds in either order and on both writes, and nothing is written. A malformed list is `policy-request-invalid` first.
- **Repeats (R46).** *Settles:* the mapping and `max_attempts` are not part of a Fallback Timezone Policy, so a repeat with a different mapping or bound is still a repeat and writes nothing. Origins given in another order are the same policy. Only live policies are repeated. A repeat of a superseding request returns it, even though the policies it names are now superseded. A request equal to a superseded policy is a new request.
- **Affected records (R47).**
  - A policy identity other than `"none"` makes any outcome kind affected: revision, Unmodeled, Quarantine, or Policy-Pending. A Policy-Pending outcome of another policy kind with identity `"none"` is not affected.
  - Only the mapped version's latest outcome for the latest selection counts. A later `"none"` outcome for the same selection makes the record unaffected.
  - An open job under another processor version does not stand in for the mapped one.
  - A record with an affected outcome whose later retry failed is affected, because it has no open job.
  - A new job has no lease, failure, `closed_at`, or history. Its `not_before` and `enqueued_at` both equal `at`.
- **Marks (R48).**
  - *Settles:* every job in state `leased` is marked, whatever its processor version and whether or not its lease has expired. The fixture's expired lease is marked.
  - Renewal keeps the mark. The completed attempt keeps the `claimed_at` and `expires_at` of its original claim, never the renewal's, including after a reopen (`@4` R32).
  - A marked completion re-queues for the same selection, with attempts 0, `not_before` equal to the completion's `at`, and no owner, expiry, failure, or `closed_at`. The attempt closes as `completed`, and the job keeps its fence until the next claim.
  - A transient failure of a marked lease keeps `@4` R35 (attempts kept, `not_before` equal to `retry_at`).
  - A marked completion whose outcome already exists reuses that outcome: the outcome history is unchanged and no outcome is appended. The job still re-queues with attempts 0, and the attempt closes.
  - A refused marked completion writes nothing. That covers `storage-reserve-exhausted`, `outcome-invalid`, and `outcome-conflict`, and it keeps the mark: a later valid completion of the same lease re-queues.
  - *Settles:* every distinct Fallback Timezone write counts, in `jobs_rearmed`, every currently leased job, including a fence an earlier write already marked. An identical repeat returns 0. However many writes counted it, a marked lease re-queues its job once.
- **Source Selection writes (R44).** A Source Selection write leaves every table of migrations 1–3 unchanged, so it marks no lease either.
- **Reads (R49).**
  - `policy` returns `None` for 0, negatives, and unknown identities.
  - `source_selection_policies("unknown-measure")` is `()`.
  - Both date bounds are inclusive.
  - A read view does not see later commits.
- **Surfaces (R51).** Each new refusal's `args` is exactly `(failure,)`, and its `str` is the failure name. The five new types are frozen.
- **Atomicity (R52).**
  - The fault-injection sweep requires at least one injected fault on each write path, and a full rollback after every one. It prescribes no minimum number of physical row writes. An earlier draft required at least six and three, which pinned the storage layout rather than atomicity, so it was relaxed in the review.
  - Completing a marked lease is also all-or-nothing.
  - In the race test, the loser is `policy-overlap`, or `store-busy` if the lock wait expires.
- **Earlier tables (R53).** While policies are written, only one `@4` table changes, and only by gaining the queued jobs. Lease marks therefore live in a new table.

**Implementer review resolved.** GPT 6.1 Sol reviewed the pack read-only at `f61415f`. There were no passability blockers: Sol's independent implementation passed all 219 tests then, and it passes all 222 now without change. The coordinator decided the following:
1. **Marked completion that reuses an outcome.** Added `test_marked_completion_reusing_an_outcome_is_requeued`.
2. **Refusals on a marked completion keep the mark.** Added `test_refused_marked_completion_keeps_its_mark`, with whole-database comparisons below the reserve and for `outcome-invalid` and `outcome-conflict`, then a valid re-queueing completion.
3. **Claim-time attempt history.** The renewed-marked-lease test compares the complete `ProcessingAttempt` with the original claim's times, also after a reopen.
4. **`jobs_rearmed`.** The rule is now stated above and tested by `test_each_policy_write_counts_every_held_lease`. It is no longer left open.
5. **R47 wording.** Clarified that a failed job does not create work, and that a policy write may insert a separate new job. This is wording only.
6. **Zone form.** Final newline, CR, CRLF, tab, and DEL are refused. The unknown-zone acceptance stays.
7. **Containers.** A list, set, scalar, or `None` for `supersedes`, on both writes, and a list for origins are refused.
8. **`datetime` as a date bound.** Refused (see Refinements, flagged for the approver).
9. **Supersession precedence.** `policy-unknown` wins (see Refinements, flagged for the approver).
10. **R49.** Kept as the integration check. Its transitive inputs are listed under the unbound inputs.
11. **Race test.** No change. A residual is added for implementation review.
12. **Fault-sweep minima.** Relaxed (see Refinements).
13. **No backfill.** No change: the no-backfill rule inherited from `@4` stands for later versions.

Sol's eleven mutants, including the five survivors, now all fail. The five former survivors are `list_supersedes_allowed`, `zone_final_newline_allowed`, `marked_identical_outcome_completes`, `marked_refusal_consumes_mark`, and `marked_history_rewrites_expiry`; the last failed through the full runner.

## Residuals

Not mechanically provable here; the approver confirms these in implementation review:
- migration 4 only creates tables and indexes;
- no `@1`–`@4` code path changes, except completion of a marked lease (R48);
- the store never parses a policy identity, header, or body. R47 only compares a policy identity with `"none"`;
- repeat detection, validation, insertion, queueing, and lease marking happen in one SQLite write transaction, with no process-local lock substituted for SQLite's serialization. The race test is bounded stress and cannot force the interleaving between the overlap check and the insertion;
- the affected-record query and lease marking are set-based SQL over indexes, with no per-record Python loop and no bytes loaded. `known_origins` is an aggregate over a covering index. As in DEV-os4-01, these may grow with retained history.

Left to other units:
- policy activation, the active timeline, Cutover Records, and Summary Refresh Jobs: `operational-store@6`;
- which timeline ordinary processing uses while a policy is pending: `execute-processing-job@2` and `@6`. `execute-processing-job@2` must read the timeline after its claim commits, because R48 covers only leases held at the write;
- proposing and confirming a timezone and validating it against tzdata: `set-fallback-timezone-policy`;
- choosing origins and what a policy supersedes: the setters;
- withdrawing a policy without a replacement is not offered. A later version adds it if a setter needs it;
- erasing policies, lease marks, and attempt history under privacy deletion: `@7`.
