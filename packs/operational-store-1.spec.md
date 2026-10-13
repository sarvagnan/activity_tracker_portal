# Draft acceptance pack — `operational-store@1`

**Status:** Draft approved by the Operator on 2026-10-12, including the five decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approved the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `operational-store` Governed Scope contract in `governance/scopes/operational-store.1.json`. Version 1 covers only the ingest side of the scope-map row: the transactions that `manage-sender-installation`, `accept-ingest-batch`, `accept-sync-completion`, and `accept-sync-checkpoint` need. Processing, publication, and data-rights transactions arrive in `@2` to `@4` (see `docs/governance/delivery-flow.md`), each additive to this Interface.

The store is protocol-agnostic. It never decodes or evaluates Wire Protocol messages; the `accept-*` units decode with `ingest-protocol`, decide the outcome, and pass explicit archival records together with the exact envelope and receipt bytes. The store enforces the invariants that must hold however its callers behave, inside one SQLite transaction per operation.

## Interface

Python module `activity_tracker_portal.operational_store`, standard library only, on the pinned SQLite runtime.

- `open_store(path, *, reserve_bytes, free_bytes=None, busy_timeout_ms=5000) -> OperationalStore` creates a database at the baseline schema or opens one already at it. `free_bytes(path) -> int` is called with the database file path, defaults to the free bytes reported by `shutil.disk_usage` for that file's directory, and exists so tests can supply the volatile input.
- `StoreRefusal(failure)` carries exactly one named failure from the contract; `StoreRetryable(failure)` carries `storage-reserve-exhausted` or `store-busy`.
- Lifecycle: `record_enrollment_offer(code_digest, expires_at, at)`, `enroll_installation(code_digest, installation_id, fingerprint, at)`, `activate_installation(installation_id, at)`, `begin_draining(installation_id, at)`, `retire_installation(installation_id, kind, at)` with `kind` either `drained` or `destructive`, and `withdraw_installation(installation_id, at)`.
- Capability generations: `honor_generation(generation, advertisement, at)` and `stop_honoring_generation(generation, at)`.
- Archival, one method per accepted message kind, each taking a `MessageHeader(installation_id, sequence, message_sha256, staged_generation, receipt_time, envelope, receipt)` plus its kind-specific record: `archive_ingest_batch(header, batch)`, `archive_completion(header, completion)`, `archive_sync_checkpoint(header, checkpoint)`, and `archive_capability_acknowledgement(header)`. Each returns the committed receipt bytes, or the identical prior receipt for an identical retry.
- `record_delivery_block(installation_id, sequence, message_sha256, failure, at)` records a health-data-free block; `installation_id` and `sequence` are optional because a malformed message may not reveal them.
- `read()` is a context manager yielding a `StoreView` bound to one read transaction, exposing `installation`, `installations`, `installation_events`, `accepted_envelopes`, `archived_message`, `archived_items`, `honored_generations`, `latest_acknowledged_generation`, `sequence_gaps`, `delivery_blocks`, `sync_checkpoints`, `storage_reserve`, and `settings`. `OperationalStore.settings` reports the writer connection's live values and `StoreView.settings` its reader connection's. `storage_reserve` samples free space live at each call rather than at view open.
- The record and result types are the frozen dataclasses defined in the module: `MessageHeader`, `BatchItem`, `IngestBatchRecord`, `CompletionRecord`, `RecordTypeOutcome`, `SyncCheckpointRecord`, `InstallationStatus`, `LifecycleEvent`, `ArchivedMessage`, `SequenceGap`, `DeliveryBlockRecord`, `ArchivedCheckpoint`, `HonoredGeneration`, `ReserveStatus`, and `StoreSettings`. `BatchItem` requires exactly one of its two fields, and `CompletionRecord` requires a snapshot kind exactly for snapshot completions; both raise `ValueError` otherwise. The module also exports `FAILURES` partitioned into `STARTUP_FAILURES`, `RETRYABLE_FAILURES`, and `INVARIANT_FAILURES`, and `MIGRATIONS`, the ordered `(version, SQL)` pairs whose SQL the store applies and checksums.

Every time argument must be timezone-aware; the store records UTC RFC 3339 text with microsecond precision. Sender-reported checkpoint times are diagnostic strings preserved verbatim, not Portal times.

Callers own the validity of the records they pass: the `accept-*` units decode and validate messages, and Delivery Block failure names and checkpoint fields come from the closed `ingest-protocol` vocabularies. The store enforces only the invariants named below.

## Requirements

Each requirement must be bound to named executable tests before freeze. Tests use synthetic values in temporary directories and never real health data.

### R1 — Governed Scope contract

The contract names the scope, version, kind, purpose, every input with its mode, outputs, every named failure with exactly one outcome class among `startup-refusal`, `retryable`, and `invariant-refusal`, observable effects, authority, dependencies, and acceptance oracle, and does not contradict the `operational-store` scope-map row.

Proposed tests: `test_governed_scope_contract_has_required_shape`, `test_every_named_failure_has_one_outcome_class`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — Runtime and durability settings

Opening reads the `sqlite3.sqlite_version_info` tuple at call time and refuses a runtime below 3.51.3 with `sqlite-runtime-unsupported` before creating or touching any file, including an existing database. Every connection uses WAL journaling, `synchronous=FULL`, and enforced foreign keys, and waits at least `busy_timeout_ms` for a lock before failing with retryable `store-busy`. `settings` reports these values from the live connections.

Proposed tests: `test_unsupported_sqlite_runtime_refuses_before_creating_file`, `test_runtime_minimum_is_exactly_3_51_3`, `test_unsupported_runtime_leaves_existing_database_untouched`, `test_connections_use_wal_full_sync_and_foreign_keys`, `test_lock_contention_beyond_timeout_is_store_busy`.

### R3 — Baseline schema and migration boundary

The migration record is part of this contract because `migrate-operational-store` and the backup scopes will read it: `PRAGMA user_version` holds the latest applied migration, and the table `store_migrations(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)` holds each applied migration's lowercase SHA-256 of its exact UTF-8 SQL from `MIGRATIONS`. A missing file, or one with no schema objects, is created at the baseline schema in one transaction that records migration 1. An existing database opens only when its recorded migrations equal the known sequence with matching checksums; a strict prefix is `store-migration-required`, any unknown or later migration or a database without `store_migrations` is `store-schema-unsupported`, and a changed checksum is `store-migration-checksum-mismatch`. An open validates an existing schema before changing any persistent setting, so a refused open leaves the logical content, `user_version`, and journal mode unchanged. Opening never upgrades an existing database; that belongs to `migrate-operational-store`.

Proposed tests: `test_new_database_records_baseline_migration_checksum`, `test_reopen_preserves_committed_state`, `test_older_schema_requires_explicit_migration`, `test_unknown_later_schema_is_unsupported`, `test_foreign_database_is_unsupported_and_untouched`, `test_changed_migration_checksum_is_refused`.

### R4 — One-use enrollment codes

The store keeps only an enrollment code's digest, expiry, and consumption time. A code is usable while the enrollment time is earlier than its expiry. `enroll_installation` consumes an unexpired, unconsumed code and creates the pending installation atomically, or fails and changes nothing, checking in order for `enrollment-code-unknown`, `enrollment-code-consumed`, `enrollment-code-expired`, and `installation-exists`. A digest can be offered only once; offering it again, whether consumed or not, is `enrollment-code-exists` and changes nothing.

Proposed tests: `test_enrollment_consumes_code_and_creates_pending_installation_atomically`, `test_enrollment_code_is_single_use`, `test_expired_or_unknown_code_is_refused`, `test_refused_enrollment_does_not_consume_code`, `test_enrollment_code_state_survives_reopen`, `test_enrollment_offer_digest_is_recorded_once`.

### R5 — Sender Installation lifecycle

States are `pending`, `active`, `draining`, `retired`, and `withdrawn`. Allowed transitions are pending to active or withdrawn, active to draining, draining to retired as `drained`, and active or draining to retired as `destructive`; any other is `installation-transition-invalid`. At most one installation is active or draining at once, checked inside the activation transaction, so activation fails with `active-installation-exists` until the previous installation is retired. Every transition appends a lifecycle event with its time. Operational facts are scoped to one installation, so a newly activated installation has no Last Delivery and no checkpoint regardless of retired installations.

A naive time is `timestamp-invalid` for every operation; times are returned timezone-aware in UTC with their microseconds. Lifecycle operations check in order for `timestamp-invalid`, `installation-unknown`, `installation-transition-invalid`, and `active-installation-exists`.

Proposed tests: `test_lifecycle_follows_declared_transitions_only`, `test_draining_installation_can_be_retired_destructively`, `test_second_active_or_draining_installation_is_refused`, `test_lifecycle_events_are_append_only_and_ordered`, `test_new_installation_inherits_no_operational_state`, `test_timestamps_must_be_timezone_aware_and_are_returned_in_utc`.

### R6 — Capability generations

Honored generations strictly increase and keep their exact advertisement bytes; a non-increasing generation is `generation-not-monotonic`. The latest acknowledged generation is the highest staged generation among archived capability acknowledgements. A generation may stop being honored only when the latest acknowledged generation is later; otherwise `generation-still-required`. Stopping a generation that is not currently honored, and archiving a new message under one, are `generation-unknown`; identical retries of already archived messages are unaffected (R8).

Proposed tests: `test_generations_strictly_increase_with_exact_bytes`, `test_generation_retires_only_after_later_acknowledgement`, `test_archival_under_unknown_generation_is_refused`, `test_archival_under_stopped_generation_is_refused`.

### R7 — Atomic exact archival

Each archival operation commits in one transaction the exact envelope BLOB, its SHA-256, sequence, kind, staged generation, receipt time, exact receipt bytes, its kind-specific record, the installation's Last Delivery, and any sequence gap; a failure at any point commits nothing. The store verifies that the envelope hashes to the header's `message_sha256` (`message-digest-mismatch`) and that the installation is active or draining (`installation-not-delivering`). An Ingest Batch stores every Source Projection as its own exact BLOB and every Source Retraction as its Source Identity, in item order, with record type, cycle identity and kind, source-page index, fragment index, and previous chain. A completion records its kind, record type, cycle identity, counts, and final chain, and a second completion for the same installation and cycle identity is `cycle-already-completed`; the same cycle identity under another installation is independent.

The atomicity test injects a failure after each successive row write through test-owned SQLite triggers, for each of the four archival operations, and requires the committed database to be unchanged every time. The per-item test finds each Source Projection as an exact column value in the database, not only inside a larger value; redundant exact copies are allowed.

Proposed tests: `test_archive_commits_exact_envelope_and_receipt`, `test_ingest_batch_items_are_stored_as_separate_exact_blobs`, `test_failure_mid_transaction_commits_nothing`, `test_envelope_digest_mismatch_is_refused`, `test_archival_requires_delivering_installation`, `test_completion_records_counts_and_chain_once`, `test_record_types_reject_invalid_combinations`.

### R8 — Idempotency, ordering, and gaps

Retry identity is the installation, sequence, and message SHA-256; the archival method, kind-specific record, staged generation, receipt bytes, and receipt time of a retry are not compared. Archiving an identical message at an archived sequence returns the original receipt without a write, even after the installation stops delivering, its generation stops being honored, or free space falls below the reserve. Archival checks in order for `timestamp-invalid`, `installation-unknown`, `message-digest-mismatch`, an identical retry, `sequence-content-conflict`, `installation-not-delivering`, `generation-unknown`, `sequence-out-of-order`, `cycle-already-completed`, and finally `storage-reserve-exhausted` immediately before writing. Different bytes at an archived sequence are `sequence-content-conflict`. A sequence at or below the installation's highest archived sequence that was never archived is `sequence-out-of-order`. A sequence more than one above the highest records a gap naming the highest archived sequence, or 0 when none, while archiving the message normally, matching the `ingest-protocol` gap report.

Proposed tests: `test_identical_retry_returns_prior_receipt_without_write`, `test_conflicting_bytes_at_archived_sequence_are_refused`, `test_lower_unarchived_sequence_is_out_of_order`, `test_gap_is_recorded_and_message_still_archived`, `test_refusal_precedence_is_deterministic`.

### R9 — Delivery Blocks and Sync Checkpoints

A Delivery Block record contains only the installation, sequence, message SHA-256, named failure, and first and last seen times; repeating an identical block updates the last seen time and count. It never stores envelope bytes. Every archived Sync Checkpoint is retained with its run sequence, trigger, Sender times, optional elapsed duration, Background Scheduling Status, per-type outcomes, and outbox counts, plus its Portal receipt time; the installation's latest checkpoint receipt time and last reported Background Scheduling Status update only from a newly archived checkpoint. History pages newest first by sequence.

Proposed tests: `test_delivery_block_is_health_data_free_and_coalesced`, `test_checkpoint_history_is_retained_and_paginated`, `test_latest_checkpoint_facts_advance_only_on_new_checkpoint`.

### R10 — Storage reserve

Before any write, every archival attempt calls `free_bytes` with the database file path and compares the result with `reserve_bytes`; below the reserve it fails with retryable `storage-reserve-exhausted` and writes nothing. Free bytes equal to the reserve still admit archival. An identical retry, which writes nothing, still returns its prior receipt below the reserve. Lifecycle, generation, and Delivery Block operations stay available below the reserve, so the Operator can still act. `storage_reserve` reports free bytes, the reserve, and whether archival is admitted.

Proposed tests: `test_archival_below_reserve_is_retryable_and_writes_nothing`, `test_non_archival_operations_remain_available_below_reserve`, `test_identical_retry_below_reserve_returns_prior_receipt`, `test_storage_reserve_status_reports_admission`.

### R11 — Coherent reads and replay

A `StoreView` establishes its snapshot when it opens and sees exactly the state committed then, even while later writes commit. `accepted_envelopes(installation_id)` yields every archived sequence and exact envelope in ascending order, so callers can rebuild `ingest-protocol` delivery state; `honored_generations` and `latest_acknowledged_generation` supply its capability inputs. `installation` reports state, fingerprint, lifecycle times, highest sequence, Last Delivery, latest checkpoint receipt time, and the last reported Background Scheduling Status with its receipt time.

Proposed tests: `test_view_is_isolated_from_later_commits`, `test_accepted_envelopes_replay_in_sequence_order`, `test_installation_status_reports_operational_facts`.

### R12 — Health-data-free operational surfaces

Lifecycle events, Delivery Blocks, sequence gaps, Sync Checkpoint records, storage-reserve status, settings, and refusal messages contain no Source Projection bytes, Source Identities, or health values. Only `accepted_envelopes`, `archived_message`, and `archived_items` return health-bearing bytes.

Proposed tests: `test_operational_reads_and_refusals_never_contain_health_bytes`.

## Decisions for the Operator

1. **Protocol-agnostic store.** The store takes explicit archival records rather than decoding envelopes, so a later Wire Protocol Version changes the `accept-*` mappings but not the store, and the scope keeps no dependency on `ingest-protocol`. The cost is that the store trusts its callers' item extraction beyond the envelope digest check.
2. **Delivery state by replay.** `ingest-protocol@1` keeps `DeliveryState` opaque, so the Portal rebuilds it at startup by replaying `accepted_envelopes` in order. Replay cost grows with history, roughly tens of seconds for a year of hourly delivery; a later protocol version could expose a serialized state if that becomes material.
3. **Enrollment codes live in the store** as digests with expiry and consumption, so single use survives a Portal restart.
4. **Delivery Blocks keep no envelope bytes.** The Sender retains the blocked message (ADR 0012); the Portal keeps only health-data-free evidence.
5. **The reserve has no default.** Deployment configuration supplies `reserve_bytes`; the deployment guide will recommend a value.

## Ground rules

- Standard library only; no ORM or generic repository layer (ADR 0023).
- Tests create databases under temporary directories, inject `free_bytes`, and use fixed timezone-aware times; they never sleep to test lock contention beyond the configured timeout.
- Fixtures are synthetic. Envelope fixtures may come from `protocol/v1/vectors/` because those are synthetic, but the store tests must not import `ingest_protocol`.

## Freeze prerequisites

The Operator approves this draft, then the executable pack adds `tests/acceptance/test_operational_store_1.py`, the runner `tests/acceptance/run_operational_store_1.sh`, and Interface stubs, shows genuine RED in which every failure comes from the stubs, and presents the exact file binding for a separate freeze approval.
