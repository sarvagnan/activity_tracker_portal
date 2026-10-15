# Draft acceptance pack — `accept-sync-completion@1`

**Status:** Draft approved by the Operator on 2026-10-15, including the eight decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `accept-sync-completion` Governed Scope contract in `governance/scopes/accept-sync-completion.1.json`.

After the last fragment of an Initial Snapshot, Reconciliation Snapshot, or Change Cycle, the Sender sends a separate, receipt-bound Snapshot Completion or Change Completion. It carries the cycle's final page, fragment, and record counts and its cumulative fragment-chain digest, but no source cursor or token (ADRs 0012 and 0046). The Portal must acknowledge a completion only when it matches the fragments it has durably archived, so that partial data is never declared complete. Later scopes treat an accepted completion as the delivery boundary for first publication, reconciliation, and ordinary change publication (ADRs 0045, 0047, and 0048).

It is a Python module over the published `ingest-protocol@1`, `manage-sender-installation@1`, `operational-store@2`, and `accept-ingest-batch@1`. It shares that last unit's response type and its approved lifecycle rules, and reads cycles through `operational-store@2`'s delivery-cycle read.

## Interface

Python module `activity_tracker_portal.accept_sync_completion`, standard library only:
- `accept_sync_completion(store, data, now) -> DeliveryResponse`. It returns `accept-ingest-batch@1`'s `DeliveryResponse`: outcome, exact body bytes, named failure, and accepted gap.
  - It never raises for an outcome.
  - It raises `ValueError` when `now` is not a `datetime`, or is one whose `utcoffset()` is `None`.
  - It raises `ValueError` when `data` is not a valid Snapshot Completion or Change Completion, because the ingress shell decodes the kind first and routes anything else elsewhere (Decision 3).

## Requirements

Each requirement is bound to named executable tests. The Python suite `tests/acceptance/test_accept_sync_completion_1.py` drives the module against real `operational-store@2` databases in temporary directories. It builds messages with `ingest-protocol@1`, archives fragments through `accept-ingest-batch@1`, and injects the store's free-space reader and a busy store. The runner `tests/acceptance/run_accept_sync_completion_1.sh` runs it. Fixtures are synthetic.

Assertions that something wrote nothing compare the whole database: `PRAGMA user_version`, every schema object, and every row of every table. Every Delivery Block is checked together with its durable record, including repeats at distinct times:
- installation, sequence, digest, and failure;
- a count raised by one, and the correct first and last seen times;
- nothing else written.

A cycle's archived fragments are the Ingest Batches archived for the same installation and cycle identity.

### R1 — Governed Scope contract

The contract names:
- the scope, version, kind, and purpose;
- inputs, with their modes;
- outputs;
- 13 named failures, each in exactly one of the outcome classes `delivery-block` and `retryable`;
- the Operational Store write effect;
- authority;
- the four dependencies;
- the acceptance oracle.

The 8 block failures are names from `ingest-protocol@1`'s closed Delivery Block vocabulary. The contract does not contradict the scope-map row. That row keeps the kind, the four dependencies, and the phrases "false completeness" and "no source cursor or token".

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — Caller errors

The function raises `ValueError` before any store use in two cases: for an invalid `now`, and for bytes that are not a valid Snapshot Completion or Change Completion. The second case covers undecodable or noncanonical bytes, non-Outbox messages, Ingest Batches, Sync Checkpoints, and capability acknowledgements.

Tests: `test_invalid_time_is_a_caller_error`, `test_other_bytes_are_caller_errors`.

### R3 — Identity and retry

A completion's identity is its installation, sequence, and message SHA-256.
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

A retryable answer writes nothing.

Tests: `test_non_delivering_installations_are_retryable`, `test_draining_installation_still_delivers`.

### R5 — Sequence order and gaps

A never-archived sequence below the installation's highest archived sequence is blocked as `sequence-out-of-order`.

A sequence more than one above the highest is a gap. For a cycle with archived fragments, that gap falls inside the cycle, so R7 blocks the completion. A completion of a cycle with no archived fragments is accepted across a gap: `gap_after` reports the highest archived sequence, or 0 when none, and the store records the gap.

Tests: `test_older_unseen_sequence_is_blocked`, `test_gap_is_accepted_only_for_an_empty_cycle`.

### R6 — Generation and version

The completion's staged generation must be honored, and its advertisement must list the message's Wire Protocol Version. Otherwise the completion is blocked as `unsupported-wire-protocol`.

Tests: `test_unhonored_generation_is_unsupported`.

### R7 — The completion proof

After R3–R6, a completion is checked against its cycle's archived fragments and completion, in this order. The first mismatch blocks it.

1. **Identity:** the cycle already has an archived completion, or its fragments are of another record type: `completion-identity-mismatch`.
2. **Kind:** the fragments' cycle kind must match. An Initial Snapshot needs a Snapshot Completion of kind `initial`, a Reconciliation Snapshot one of kind `reconciliation`, and a Change Cycle a Change Completion. Otherwise `completion-kind-mismatch`.
3. **Sequence continuity:** every sequence from the cycle's first fragment up to the completion's own must be archived, whatever its kind. A missing one, including a gap the completion itself would open, gives `completion-sequence-gap`.
4. **Counts:** the page count must equal the last fragment's source-page index, the fragment count the archived fragments, and the record count their items. Otherwise `completion-count-mismatch`.
5. **Chain:** the final chain must equal the chain after the last fragment, as `ingest-protocol@1` computes it from that fragment's previous chain and message SHA-256. Otherwise `completion-chain-mismatch`.

A cycle with no archived fragments and no completion is empty. Its completion must carry zero counts and the cycle's chain start.

Tests:
- `test_exact_cycles_are_accepted`, covering all three cycle kinds;
- `test_empty_cycle_completion`;
- `test_completed_cycle_takes_no_second_completion`;
- `test_other_record_type_is_an_identity_mismatch`;
- `test_kind_must_match_the_cycle`;
- `test_missing_sequence_in_the_cycle_is_a_gap`;
- `test_counts_must_match_the_archived_fragments`;
- `test_final_chain_must_match`;
- `test_completion_failures_follow_protocol_order`.

### R8 — Archival and the completed cycle

A newly accepted completion is archived in one store transaction before its receipt is returned. The store keeps:
- the exact envelope bytes, the exact receipt bytes, the receipt time `now`, and the staged generation;
- the completion and snapshot kind, record type, cycle identity, the three counts, and the final chain.

Nothing else from the message is stored, and it has no source cursor or token. Last Delivery becomes `now`.

The store's delivery-cycle read then reports the cycle as completed at the completion's sequence. Accepting a completion triggers no processing, publication, or cutover.

Tests: `test_accepted_completion_is_archived_exactly`, `test_completion_closes_the_cycle`.

### R9 — Capacity and contention

Free space is checked last: a would-be block is still blocked, with its record, when the reserve is exhausted. An otherwise acceptable completion then gets retryable `storage-reserve-exhausted`.

A busy store at any step — reading, archiving, or recording a block — gets retryable `temporarily-unavailable` with failure `store-busy`. None of these answers writes anything else.

Tests: `test_exhausted_reserve_is_retryable_and_blocks_still_win`, `test_busy_store_is_retryable`.

### R10 — Conformance with the protocol's evaluation

For an active installation with storage available, every answer equals `ingest-protocol@1`'s `evaluate_delivery` over the same history: the same outcome, exact body bytes, failure, and gap. The protocol does not model the storage reserve or a busy store, so R9's storage-availability answers are the only exceptions.

Each block and each identical retry is checked against the whole database: a block changes only its own record, and a retry changes nothing.

Seeded synthetic histories interleave fragments, accepted through `accept-ingest-batch@1`, with completions. They cover several concurrent cycles of every kind and record type, and include:
- exact, empty, and second completions;
- wrong kinds, record types, counts, and chains;
- holes left by skipped sequences and by blocked messages;
- fragments sent after their cycle completed;
- retries, conflicts, and unknown generations.

The test requires each completion failure, and acceptance of each cycle kind, to occur.

Tests: `test_answers_match_the_protocol_evaluation`.

## Decisions for the Operator

1. **One leaf for both completion kinds.** Snapshot and Change Completions share one envelope and one proof, so one unit validates them, as the scope map already plans. Duplicated completion validation is what ADR 0046 avoids. The alternative is a unit per kind.
2. **The same lifecycle rules as `accept-ingest-batch@1`.** Pending, ended, and unknown installations are retryable, following `accept-ingest-batch@1` Decisions 3 and 4 and `manage-sender-installation@1` Decision 1. Blocks are recorded, and free space is checked last. Like `accept-capability-acknowledgement@1`, the unit therefore depends on `manage-sender-installation@1`. The scope-map row gains that dependency, and its "active" installation becomes "delivering", which includes draining.
3. **Only valid completions reach this unit.** The ingress shell decodes the kind and routes undecodable and non-Outbox bytes to `accept-ingest-batch` (`accept-ingest-batch@1` Decision 5). Anything else reaching this unit is a caller error.
4. **One response type.** The unit returns `accept-ingest-batch@1`'s `DeliveryResponse`, so the ingress shell handles every `accept-*` answer alike.
5. **The protocol's answer, from durable facts.** As in `accept-ingest-batch@1` Decision 2, answers for a delivering installation equal `evaluate_delivery`'s (R10). They come from the store's delivery-cycle read and its sequence history, not from a replayed history.
6. **Any hole inside a cycle blocks its completion.** This is `ingest-protocol@1`'s rule, and this unit applies it unchanged. A sequence missing between a cycle's first fragment and its completion makes the completion unacceptable, whatever kind of message is missing, because the Portal cannot prove nothing of the cycle was lost.
   - Under ADR 0046, a blocked completion is never rewritten, skipped, or force-accepted.
   - Corrected Portal code may accept an identical retry. Otherwise, explicit Sender retirement or reset leaves the cycle incomplete and starts a new installation and Initial Snapshot.
   - The alternative, counting only the cycle's own fragments, would deviate from the published protocol and need a new Wire Protocol Version.
7. **One completion closes a cycle.** After a cycle's completion is archived, the cycle is closed:
   - a second completion at another sequence is blocked as `completion-identity-mismatch`;
   - `accept-ingest-batch@1` blocks later fragments as `fragment-order-mismatch`.

   Acceptance triggers no processing, publication, or cutover; the `complete-*` scopes read completed cycles from the store.
8. **The receipt time is the Portal's `now`.** As in `accept-ingest-batch@1` Decision 7, Last Delivery advances only when a new completion is archived, never on a retry.

## Ground rules

- Fixtures are synthetic. Messages are built with `ingest-protocol@1`, and fragments are archived through the published `accept-ingest-batch@1`.
- A busy store is injected with a held write transaction on a second connection and a short store timeout, and with a store proxy whose operation raises the store's retryable `store-busy`.

## Freeze prerequisites

After Gate A, the executable pack adds:
- the Python suite;
- the runner;
- the Interface stub `accept_sync_completion.py`, whose function raises `NotImplementedError`.

Against the stub, every behaviour test fails and the two contract tests pass. GPT 6.1 Sol's read-only implementability review runs before Gate B. Every dependency is published, so Vouch records `specify` after Gate A.
