# Draft acceptance pack — `accept-capability-acknowledgement@1`

**Status:** Draft approved by the Operator on 2026-10-15, including the six decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `accept-capability-acknowledgement` Governed Scope contract in `governance/scopes/accept-capability-acknowledgement.1.json`.

A Sender acknowledges each capability generation it adopts with an ordered, receipt-bound Outbox Message (ADR 0031). Once an installation's acknowledgement of a later generation is archived, `manage-sender-installation@1` may stop honoring earlier ones. On 2026-10-15 the Operator found that no Phase 3 unit accepted this message and chose a small leaf for it, so that each `accept-*` unit keeps one message family.

It is a Python module over the published `ingest-protocol@1`, `manage-sender-installation@1`, and `operational-store@2`, and over `accept-ingest-batch@1`. It shares that unit's response type and its approved lifecycle rules.

## Interface

Python module `activity_tracker_portal.accept_capability_acknowledgement`, standard library only:
- `accept_capability_acknowledgement(store, data, now) -> DeliveryResponse`. It returns `accept-ingest-batch@1`'s `DeliveryResponse`: outcome, exact body bytes, named failure, and accepted gap.
  - It never raises for an outcome.
  - It raises `ValueError` when `now` is not a `datetime`, or is one whose `utcoffset()` is `None`.
  - It raises `ValueError` when `data` is not a valid capability acknowledgement, because the ingress shell decodes the kind first and routes anything else elsewhere (Decision 3).

## Requirements

Each requirement is bound to named executable tests. The Python suite `tests/acceptance/test_accept_capability_acknowledgement_1.py` drives the module against real `operational-store@2` databases in temporary directories, builds messages with `ingest-protocol@1`, and injects the store's free-space reader and a busy store. The runner `tests/acceptance/run_accept_capability_acknowledgement_1.sh` runs it. Fixtures are synthetic.

Assertions that something wrote nothing compare the whole database: `PRAGMA user_version`, every schema object, and every row of every table. Every Delivery Block is checked together with its durable record:
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

The 3 block failures are names from `ingest-protocol@1`'s closed Delivery Block vocabulary. The contract does not contradict the scope-map row. That row keeps the kind and the four dependencies. The test also checks three phrases: "capability acknowledgement" in the purpose, "Capability acknowledgement envelope" in the inputs, and "latest acknowledged generation" in the outputs.

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — Caller errors

The function raises `ValueError` before any store use in two cases: for an invalid `now`, and for bytes that are not a valid capability acknowledgement. The second case covers undecodable or noncanonical bytes, Ingest Batches, completions, and checkpoints.

Tests: `test_invalid_time_is_a_caller_error`, `test_other_bytes_are_caller_errors`.

### R3 — Identity and retry

An acknowledgement's identity is its installation, sequence, and message SHA-256.
- **Identical retry:** a second delivery of identical bytes returns the exact archived receipt and writes nothing. That holds even after the installation stops delivering, its generation stops being honored, or the reserve is exhausted.
- **Conflict:** different bytes at an archived sequence are blocked as `sequence-content-mismatch`.

Tests: `test_identical_retry_returns_the_archived_receipt`, `test_same_sequence_different_bytes_is_blocked`.

### R4 — Installation state

As in `accept-ingest-batch@1`, after identity:
- **Pending:** retryable `temporarily-unavailable`, failure `installation-pending`.
- **Retired or withdrawn:** retryable, failure `installation-ended`.
- **Unknown:** retryable, failure `installation-unknown`.
- **Active or draining:** delivery proceeds.

A retryable answer writes nothing.

Tests: `test_non_delivering_installations_are_retryable`, `test_draining_installation_still_delivers`.

### R5 — Sequence order and gaps

A never-archived sequence below the installation's highest archived sequence is blocked as `sequence-out-of-order`. A sequence more than one above it is accepted, and `gap_after` reports the highest archived sequence, or 0 when none. The store records the gap.

Tests: `test_older_unseen_sequence_is_blocked`, `test_gap_is_accepted_and_reported`.

### R6 — Generation and version

The acknowledgement's staged generation must be honored, and its advertisement must list the message's Wire Protocol Version. Otherwise it is blocked as `unsupported-wire-protocol`.

Tests: `test_unhonored_generation_is_unsupported`.

### R7 — Archival and the acknowledged generation

A newly accepted acknowledgement is archived in one store transaction before its receipt is returned: the exact envelope bytes, the exact receipt bytes, the receipt time `now`, and its staged generation.

Last Delivery becomes `now`. The store's latest acknowledged generation becomes the highest staged generation among archived acknowledgements, and `manage-sender-installation@1`'s `stop_superseded_generation` can then stop older generations. Accepting an acknowledgement stops no generation itself.

Tests:
- `test_accepted_acknowledgement_is_archived_exactly`
- `test_acknowledgement_lets_superseded_generations_stop`
- `test_acceptance_stops_no_generation`

### R8 — Capacity and contention

Free space is checked last: a would-be block is still blocked, with its record, when the reserve is exhausted. An otherwise acceptable acknowledgement then gets retryable `storage-reserve-exhausted`.

A busy store at any step — reading, archiving, or recording a block — gets retryable `temporarily-unavailable` with failure `store-busy`. None of these answers writes anything else.

Tests: `test_exhausted_reserve_is_retryable_and_blocks_still_win`, `test_busy_store_is_retryable`.

### R9 — Conformance with the protocol's evaluation

For an active installation, every answer equals `ingest-protocol@1`'s `evaluate_delivery` over the same history: the same outcome, exact body bytes, failure, and gap. Seeded synthetic histories interleave acknowledgements with archived Ingest Batches, and include retries, gaps, conflicts, and unknown generations.

Tests: `test_answers_match_the_protocol_evaluation`.

## Decisions for the Operator

1. **A leaf of its own.** The Operator chose this on 2026-10-15, so that `accept-ingest-batch` keeps one message family.
2. **The same lifecycle rules as `accept-ingest-batch@1`.** Pending, ended, and unknown installations are retryable, following `accept-ingest-batch@1` Decisions 3 and 4 and `manage-sender-installation@1` Decision 1. Blocks are recorded, and free space is checked last.
3. **Only valid acknowledgements reach this unit.** The ingress shell decodes the kind and routes undecodable and non-Outbox bytes to `accept-ingest-batch` (`accept-ingest-batch@1` Decision 5). Anything else reaching this unit is a caller error.
4. **One response type.** The unit returns `accept-ingest-batch@1`'s `DeliveryResponse`, so the ingress shell handles every `accept-*` answer alike. That makes `accept-ingest-batch@1` a dependency. The alternative is a second identical type.
5. **Acceptance stops nothing.** Archiving advances the store's latest acknowledged generation. Stopping a superseded generation stays an Operator-driven `manage-sender-installation@1` intent, under its own rules for delivering and pending installations. The alternative is to stop generations automatically on acceptance, which would move that unit's authority here.
6. **The protocol's answer, from durable facts.** As in `accept-ingest-batch@1` Decision 2, answers for a delivering installation equal `evaluate_delivery`'s (R9).

## Ground rules

- Fixtures are synthetic. Messages are built with `ingest-protocol@1`, and Ingest Batches in conformance histories are archived through `accept-ingest-batch@1`, or through the store directly while that unit is unpublished.
- A busy store is injected with a held write transaction on a second connection and a short store timeout, and with a store proxy whose operation raises the store's retryable `store-busy`.

## Freeze prerequisites

The Operator approved the draft. The executable pack adds:
- the Python suite `tests/acceptance/test_accept_capability_acknowledgement_1.py`, 17 tests;
- the runner `tests/acceptance/run_accept_capability_acknowledgement_1.sh`, which runs the pinned-runtime check and then the suite;
- the Interface stub `src/activity_tracker_portal/accept_capability_acknowledgement.py`, whose function raises `NotImplementedError`.

Every Delivery Block is checked with its durable record, including repeats at distinct times. That follows the pattern `accept-ingest-batch@1`'s Gate B review established. R9's seeded histories cross-check these answers against `evaluate_delivery`:
- 732 receipts, including 133 after gaps and 113 retries;
- every block failure, with 34 repeated blocks;
- 100 interleaved Ingest Batches accepted through `accept-ingest-batch@1`.

The test asserts this coverage.

Against the stub, every behaviour test fails: 14 tests and R9's 8 seeded histories. The two contract tests pass, and the runner exits 1.

A throwaway reference, kept out of the repository, passes all 17 tests. It kills 18 of 19 mutants:
- pending, ended, and unknown installations mishandled;
- the order check removed;
- a gap reported off by one;
- a block unrecorded, recorded with shifted times, recorded twice on repeat, or sent without its identity;
- a busy store swallowed or answered as a block;
- the wrong reserve reason, or the reserve checked before validation;
- other kinds or naive times accepted;
- retries re-evaluated;
- the staged generation ignored;
- a shifted receipt time.

The surviving mutant removes the generation pre-check, and it is equivalent. Nothing is checked after generation, and the store's own `generation-unknown` refusal maps to the same `unsupported-wire-protocol` block and record (Decision 6). The wire-version half of the check cannot be reached, because `ingest-protocol@1` decodes only Wire Protocol Version 1.

GPT 6.1 Sol's read-only implementability review runs before Gate B. The binding is:
- **spec files:** the contract and this pack;
- **test files:** the suite and the runner.

Vouch records `specify` once `accept-ingest-batch@1` is published on main, before the freeze.
