# Draft acceptance pack — `accept-sync-checkpoint@1`

**Status:** Draft approved by the Operator on 2026-10-16, including the ten decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `accept-sync-checkpoint` Governed Scope contract in `governance/scopes/accept-sync-checkpoint.1.json`.

When a Polling Run finishes, the Sender sends one receipt-bound, health-data-free Sync Checkpoint (ADR 0049). It carries the run sequence and trigger, the Sender's start and end times and monotonic elapsed duration, the Background Scheduling Status observed for that run, each record type's lifecycle outcome, reason, and source-data-staged flag, and pre-checkpoint outbox counts. The Portal must acknowledge it like any Outbox Message. Polling Freshness comes from the Portal's receipt time, not the Sender clock: the Sender's times are diagnostic claims, and implausible skew is a visible condition that never rejects the message.

It is a Python module over the published `ingest-protocol@1`, `manage-sender-installation@1`, `operational-store@2`, and `accept-ingest-batch@1`. It shares that last unit's response type and its approved lifecycle rules. The store already keeps every checkpoint, the latest checkpoint receipt time, and the time-stamped Background Scheduling Status; this unit derives the installation's polling state from them on read.

## Interface

Python module `activity_tracker_portal.accept_sync_checkpoint`, standard library only:
- `accept_sync_checkpoint(store, data, now) -> DeliveryResponse`. It returns `accept-ingest-batch@1`'s `DeliveryResponse`: outcome, exact body bytes, named failure, and accepted gap.
  - It never raises for an outcome.
  - It raises `ValueError` when `now` is not a `datetime`, or is one whose `utcoffset()` is `None`.
  - It raises `ValueError` when `data` is not a valid Sync Checkpoint, because the ingress shell decodes the kind first and routes anything else elsewhere (Decision 3).
- `polling_state(store, installation_id, now) -> PollingState`, a read that writes nothing. It raises `ValueError` for an invalid `now`, as above. The store's own failures propagate (Decision 10).
- `clock_conditions(checkpoint, enrolled_at) -> frozenset[str]`: the clock-skew conditions of one `operational-store@2` `ArchivedCheckpoint` for an installation enrolled at `enrolled_at`. It lets the Operations history show each checkpoint's conditions with the same rule. It raises `ValueError` when `enrolled_at` is not timezone-aware.
- `PollingState`, a frozen dataclass:
  - `installation_id`;
  - `freshness`: `"never-reported"`, `"current"`, `"delayed"`, or `"stale"`;
  - `latest_checkpoint`: the installation's latest `ArchivedCheckpoint`, with its Portal receipt time and exact report, or `None`;
  - `first_poll_in_progress` and `first_checkpoint_overdue`: the never-reported setup states (Decision 7);
  - `background_scheduling`: a `SchedulingReport` of the last reported status, its Portal receipt time, and whether it is stale, or `None`;
  - `conditions`: a `frozenset` drawn from `sender-clock-ahead`, `sender-clock-behind`, `sender-times-inconsistent`, `background-scheduling-restricted`, and `background-scheduling-unregistered`.

## Requirements

Each requirement is bound to named executable tests. The Python suite `tests/acceptance/test_accept_sync_checkpoint_1.py` drives the module against real `operational-store@2` databases in temporary directories. It builds messages with `ingest-protocol@1`, and injects the store's free-space reader and a busy store. The runner `tests/acceptance/run_accept_sync_checkpoint_1.sh` runs it. Fixtures are synthetic.

Assertions that something wrote nothing compare the whole database: `PRAGMA user_version`, every schema object, and every row of every table. Every Delivery Block is checked together with its durable record, including repeats at distinct times:
- installation, sequence, digest, and failure;
- a count raised by one, and the correct first and last seen times;
- nothing else written.

### R1 — Governed Scope contract

The contract names:
- the scope, version, kind, and purpose;
- inputs, with their modes;
- outputs;
- 8 named failures, each in exactly one of the outcome classes `delivery-block` and `retryable`;
- the Operational Store write effect;
- authority;
- the four dependencies;
- the acceptance oracle.

The 3 block failures are names from `ingest-protocol@1`'s closed Delivery Block vocabulary. The contract does not contradict the scope-map row. That row keeps the kind, the four dependencies, and the phrases "without trusting its wall clock" and "from receipt time".

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — Caller errors

`accept_sync_checkpoint` raises `ValueError` before any store use in two cases: for an invalid `now`, and for bytes that are not a valid Sync Checkpoint. The second case covers undecodable or noncanonical bytes, non-Outbox messages, Ingest Batches, completions, and capability acknowledgements. `polling_state` raises `ValueError` before any store use for an invalid `now`, and `clock_conditions` for an `enrolled_at` that is not timezone-aware.

Tests: `test_invalid_time_is_a_caller_error`, `test_other_bytes_are_caller_errors`.

### R3 — Identity and retry

A checkpoint's identity is its installation, sequence, and message SHA-256.
- **Identical retry:** a second delivery of identical bytes returns the exact archived receipt and writes nothing. That holds even after the installation stops delivering, its generation stops being honored, or the reserve is exhausted.
- **Conflict:** different bytes at an archived sequence are blocked as `sequence-content-mismatch`. Identity is checked before installation state, so this holds after the installation retires.

Tests:
- `test_identical_retry_returns_the_archived_receipt`
- `test_conflict_after_retirement_is_still_blocked`
- `test_same_sequence_different_bytes_is_blocked`

### R4 — Installation state

As in `accept-ingest-batch@1`, after identity:
- **Pending:** retryable `temporarily-unavailable`, failure `installation-pending`.
- **Retired or withdrawn:** retryable, failure `installation-ended`.
- **Unknown:** retryable, failure `installation-unknown`.
- **Active or draining:** delivery proceeds.

Installation state is checked before sequence order and the generation, so a non-delivering installation's checkpoint is retryable even when it would otherwise be blocked. A retryable answer writes nothing.

Tests: `test_non_delivering_installations_are_retryable`, `test_draining_installation_still_delivers`.

### R5 — Sequence order and gaps

A never-archived sequence below the installation's highest archived sequence is blocked as `sequence-out-of-order`, even when its generation is unknown: order is checked before the generation, as in the protocol. A sequence more than one above the highest is accepted: `gap_after` reports the highest archived sequence, or 0 when none, and the store records the gap.

Tests: `test_older_unseen_sequence_is_blocked`, `test_gap_is_accepted_and_reported`.

### R6 — Generation and version

The checkpoint's staged generation must be honored, and its advertisement must list the message's Wire Protocol Version. Otherwise the checkpoint is blocked as `unsupported-wire-protocol`.

The generation's record-count, message-byte, and Source Projection schema limits govern Ingest Batches only, so they never block a checkpoint (Decision 5). That includes a checkpoint reporting more record types than the record-count limit, longer than the byte limit, or naming record types the advertisement does not list.

Tests: `test_unhonored_generation_is_unsupported`, `test_ingest_batch_limits_do_not_apply`.

### R7 — Archival

A newly accepted checkpoint is archived in one store transaction before its receipt is returned. The store keeps:
- the exact envelope bytes, the exact receipt bytes, the receipt time `now`, and the staged generation;
- the run sequence, trigger, the Sender's start and end times exactly as sent, the elapsed duration or none, the Background Scheduling Status, each record type's outcome, reason, and source-data-staged flag in the message's order, and both pre-checkpoint outbox counts.

Last Delivery, the latest checkpoint receipt, and the Background Scheduling Status's receipt time all become `now`. Nothing else is written: no Health Summary, Data Watermark, Cutover Record, source evidence, or capability generation changes. The test compares the whole database with a copy in which `operational-store@2`'s `archive_sync_checkpoint` archived the exact header and record directly.

Implausible Sender times never block: a checkpoint whose times are reversed, in the future, or before enrollment is accepted and archived exactly (Decision 6).

Tests: `test_accepted_checkpoint_is_archived_exactly`, `test_implausible_sender_times_are_accepted`, `test_acceptance_changes_no_other_state`.

### R8 — Polling Freshness

Polling Freshness belongs to one installation and is measured at the read time from the Portal receipt time of its latest accepted checkpoint (Decision 7). The latest checkpoint is the one with the highest sequence, whose receipt the store keeps as the latest checkpoint receipt. Portal time can step backwards, so it need not be the checkpoint received last by the clock.
- **never-reported** before its first accepted checkpoint, whatever else it has delivered;
- **current** while the age is at most six hours, including when the read time is earlier than the receipt;
- **delayed** while it is more than six hours and at most 24;
- **stale** when it is more than 24 hours.

While never-reported:
- `first_poll_in_progress` is set once the installation has any accepted Outbox Message (a Last Delivery);
- `first_checkpoint_overdue` is set once the read time is more than 24 hours after its activation. That stays true after the installation drains or retires without reporting. A pending or withdrawn installation was never activated, so it is never overdue, and enrollment time never stands in for a checkpoint.

Both are unset once a checkpoint exists. A replacement installation starts never-reported, and a retired installation keeps its own state. An identical retry does not refresh freshness.

Tests:
- `test_never_reported_until_the_first_checkpoint`
- `test_freshness_boundaries`
- `test_first_poll_in_progress_and_overdue`
- `test_freshness_is_scoped_to_one_installation`
- `test_retry_does_not_refresh_freshness`

### R9 — Background Scheduling Status

The polling state reports the last reported Background Scheduling Status with its Portal receipt time, or none before the first checkpoint. It is never presented as current: it is marked stale whenever Polling Freshness is stale. A last report of `restricted` adds the condition `background-scheduling-restricted`, and `unregistered` adds `background-scheduling-unregistered` (Decision 8). `available` adds none.

Tests:
- `test_scheduling_report_keeps_its_receipt_time`
- `test_restricted_and_unregistered_scheduling_are_conditions`
- `test_stale_freshness_makes_the_scheduling_report_stale`

### R10 — Clock conditions

The polling state's conditions include the latest checkpoint's clock conditions. `clock_conditions` derives them for any archived checkpoint, with a tolerance of five minutes (Decision 6). Sender times are compared as UTC instants over the protocol's whole range, years 0000 to 9999 of the proleptic Gregorian calendar, in which 0000 is a leap year. Python's `datetime` cannot hold year 0000, so no valid archived checkpoint may make `clock_conditions` or `polling_state` raise.
- `sender-clock-ahead`: the run's end time is more than five minutes after the checkpoint's Portal receipt time;
- `sender-clock-behind`: the run's end time is more than five minutes before the installation's enrollment;
- `sender-times-inconsistent`: the run's end-minus-start time differs from its elapsed duration by more than five minutes, or, without an elapsed duration, the end is more than five minutes before the start.

An end time long before its receipt is a delivery delay, not skew, and raises nothing. The latest checkpoint is reported exactly, with its per-type outcomes, reasons, flags, and diagnostic Sender times as archived.

Tests:
- `test_clock_ahead_of_receipt`
- `test_clock_behind_enrollment`
- `test_inconsistent_run_times`
- `test_delivery_delay_is_not_skew`
- `test_latest_checkpoint_is_reported_exactly`

### R11 — Reading polling state

`polling_state` writes nothing: every kind of state it reports leaves the whole database unchanged. It is a plain read, so it gives the same answers while another connection holds the write lock. An unknown installation propagates the store's `installation-unknown` refusal, and a busy store its retryable `store-busy` (Decision 10).

Tests: `test_reading_polling_state_writes_nothing`, `test_reading_polling_state_propagates_store_failures`.

### R12 — Capacity and contention

Free space is checked last: a would-be block is still blocked, with its record, when the reserve is exhausted. An otherwise acceptable checkpoint then gets retryable `storage-reserve-exhausted`.

A busy store at any step — reading, archiving, or recording a block — gets retryable `temporarily-unavailable` with failure `store-busy`. None of these answers writes anything else.

The caller serializes writes, so the store's own rechecks should never refuse an archive after this unit's read (`accept-ingest-batch@1` Decision 9). If one does, the unit still answers and never raises: a refused conflict, order, or generation gives the matching block, with its record. An installation that another writer retires between the read and the archive gets retryable `installation-ended`, and only the retirement is written.

Tests:
- `test_exhausted_reserve_is_retryable_and_blocks_still_win`
- `test_busy_store_is_retryable`
- `test_store_refusals_after_the_read_are_answered`

### R13 — Conformance with the protocol's evaluation

For an active installation with storage available, every answer equals `ingest-protocol@1`'s `evaluate_delivery` over the same history: the same outcome, exact body bytes, failure, and gap. The protocol does not model the storage reserve or a busy store, so R12's storage-availability answers are the only exceptions.

Each block and each identical retry is checked against the whole database: a block changes only its own record, and a retry changes nothing. After each newly accepted checkpoint, the polling state reports it as the latest checkpoint. After every step, the polling state at the step's time and at a later time equals the R8–R10 derivation from the latest accepted checkpoint.

Seeded synthetic histories interleave checkpoints with Ingest Batches, accepted through `accept-ingest-batch@1`, while Portal time advances by minutes to days. They include every trigger, scheduling status, and lifecycle outcome, implausible Sender times including year 0000, retries, conflicts, older unseen sequences, gaps, and unknown generations. The test requires each block failure, a gap, a retry, and a repeated block to occur, and also each clock condition, each freshness, a first poll in progress, and an overdue first checkpoint.

Tests: `test_answers_match_the_protocol_evaluation`.

## Decisions for the Operator

1. **One leaf for acceptance and polling state.** The scope-map row assigns both to this unit. Freshness is derived at read time from the durable receipt time instead of being stored as a label, so it ages without any write, and `portal-state-presentation` only renders it. The alternative moves the derivation into presentation and changes both rows.
2. **The same lifecycle rules as `accept-ingest-batch@1`.** Pending, ended, and unknown installations are retryable, following `accept-ingest-batch@1` Decisions 3 and 4 and `manage-sender-installation@1` Decision 1. Blocks are recorded, and free space is checked last. The scope-map row's "active" installation becomes "delivering", which includes draining.
3. **Only valid Sync Checkpoints reach this unit.** The ingress shell decodes the kind and routes undecodable and non-Outbox bytes to `accept-ingest-batch` (`accept-ingest-batch@1` Decision 5). Anything else reaching this unit is a caller error.
4. **One response type.** The unit returns `accept-ingest-batch@1`'s `DeliveryResponse`, so the ingress shell handles every `accept-*` answer alike. The scope-map row gains the `accept-ingest-batch` dependency, as `accept-capability-acknowledgement@1` and `accept-sync-completion@1` did.
5. **The protocol's answer, from durable facts.** As in `accept-ingest-batch@1` Decision 2, answers for a delivering installation equal `evaluate_delivery`'s (R13). The protocol has no checkpoint-specific check, so the run sequence, trigger, Sender times, scheduling status, and outcomes never block.
6. **Implausible Sender times are conditions, with a five-minute tolerance.** Phones normally keep network time, so drift beyond five minutes is implausible, but the conditions never block or change freshness. There are three rules:
   - a run cannot end after its checkpoint reached the Portal (`sender-clock-ahead`);
   - nor before the installation enrolled (`sender-clock-behind`);
   - its wall-clock duration should match its monotonic elapsed duration (`sender-times-inconsistent`, which catches a clock change mid-run).

   An end long before receipt is never skew, because the Outbox may hold a checkpoint for days behind older messages. The alternatives are another tolerance, or flagging only reversed times.
7. **Freshness boundaries and never-reported states.**
   - Exactly six hours is still current, and exactly 24 hours still delayed.
   - The Portal cannot see the Sender's open run, so any accepted delivery is its evidence of a first poll in progress.
   - The 24-hour first-checkpoint deadline runs from activation, as ADR 0049 states; a pending installation has none.
   - Freshness never carries across a replacement.
8. **Restricted and unregistered scheduling are both conditions.** Version 1 always expects the hourly background registration, even while collection is disabled (ADR 0049), so an unregistered report is always unexpected. The report keeps its receipt time and turns stale with stale freshness. Collection-disabled stays visible through each type's `deferred` outcome with reason `collection-disabled`, which presentation reads from the latest checkpoint.
9. **The receipt time is the Portal's `now`.** As in `accept-ingest-batch@1` Decision 7, Last Delivery, the latest checkpoint receipt, and the Background Scheduling Status's receipt time advance only when a new checkpoint is archived, never on a retry.
10. **Polling-state reads are plain reads.** They write nothing. The store's own failures, such as an unknown installation or a busy store, propagate to the Overview or Operations caller, which has no Delivery Response to give.

## Ground rules

- Fixtures are synthetic. Messages are built with `ingest-protocol@1`, and Ingest Batches are archived through the published `accept-ingest-batch@1`.
- A busy store is injected with a held write transaction on a second connection and a short store timeout, and with a store proxy whose operation raises the store's retryable `store-busy`.

## Freeze prerequisites

After Gate A, the executable pack adds:
- the Python suite;
- the runner;
- the Interface stub `accept_sync_checkpoint.py`, whose functions raise `NotImplementedError` and whose dataclasses are defined.

Against the stub, every behaviour test fails and the two contract tests pass. GPT 6.1 Sol's read-only implementability review runs before Gate B. Every dependency is published, so Vouch records `specify` after Gate A.
