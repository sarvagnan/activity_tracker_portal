# Draft acceptance pack — `accept-ingest-batch@1`

**Status:** Draft approved by the Operator on 2026-10-15, including the ten decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `accept-ingest-batch` Governed Scope contract in `governance/scopes/accept-ingest-batch.1.json`. It is the Portal side of Ingest Batch delivery (ADR 0009, 0012, 0017, 0020, 0033). It is a Python module over three published units:
- `ingest-protocol@1` owns the message bytes, identities, chain calculation, and closed failure vocabularies.
- `operational-store@2` owns archival and its invariants, and the delivery-cycle read.
- `manage-sender-installation@1` owns the installation lifecycle. Its Decision 1 requires the `accept-*` units to keep a pending installation's deliveries retryable.

This unit decides how one delivered Ingest Batch is answered.

`ingest-protocol@1` already contains an executable reference for this decision, `evaluate_delivery`, but over an in-memory history. This unit applies the same rules to the durable facts in the store, so for a delivering installation its answer is the protocol's answer.

## Interface

Python module `activity_tracker_portal.accept_ingest_batch`, standard library only:
- `DeliveryResponse`, a frozen dataclass:
  - `outcome`: `"receipt"`, `"retryable"`, or `"block"`;
  - `body`: the exact response bytes, a Wire Protocol `delivery-receipt`, `retryable-failure`, or `delivery-block`;
  - `failure`: the named failure from the contract, or `None` for a receipt;
  - `gap_after`: for a newly accepted batch that skipped sequences, the highest sequence archived before it (0 when none); otherwise `None`.
- `accept_ingest_batch(store, data, now) -> DeliveryResponse`, where `store` is an open `OperationalStore` and `data` is the exact uncompressed bytes.
  - It never raises for an outcome.
  - It raises `ValueError` when `now` is not timezone-aware.
  - It raises `ValueError` when `data` is a valid Outbox Message of another kind (a completion, checkpoint, or capability acknowledgement), because the ingress shell routes those to their own units (Decision 5).

## Requirements

Each requirement is bound to named executable tests. The Python suite `tests/acceptance/test_accept_ingest_batch_1.py` drives the module against real `operational-store@2` databases in temporary directories, builds messages with `ingest-protocol@1`, and injects the store's free-space reader and lock contention. The runner `tests/acceptance/run_accept_ingest_batch_1.sh` runs it. Fixtures are synthetic: UUIDs start with `00000000-0000-4000-8000-`, and projections are synthetic bytes.

Assertions that something wrote nothing compare the whole database: `PRAGMA user_version`, every schema object, and every row of every table, BLOBs included.

### R1 — Governed Scope contract

The contract names:
- the scope, version, kind, and purpose;
- the inputs, with their modes;
- the outputs;
- 14 named failures, each in exactly one of the outcome classes `delivery-block` and `retryable`;
- the Operational Store write effect;
- authority;
- the `ingest-protocol@1`, `manage-sender-installation@1`, and `operational-store@2` dependencies;
- the acceptance oracle.

The 9 block failures are names from `ingest-protocol@1`'s closed Delivery Block vocabulary. The contract does not contradict the scope-map row. That row keeps the kind, the three dependencies, and these phrases, which the test checks:
- in the purpose, "idempotently archive, and acknowledge an Ingest Batch";
- in the inputs, "Envelope", "active Sender Installation", "capacity state", and "supported versions";
- in the outputs, "Archived Raw Records plus Delivery Receipt, retryable outcome, or Delivery Block" and "accepted sequence gaps become visible anomalies".

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — Caller errors

The function raises `ValueError` before reading or writing anything:
- when `now` is not a `datetime`, or is one whose `utcoffset()` is `None`;
- when the bytes are a valid Outbox Message that is not an Ingest Batch.

Tests: `test_naive_time_is_a_caller_error`, `test_other_outbox_kinds_are_caller_errors`.

### R3 — Undecodable messages

Bytes that `ingest-protocol@1` cannot decode, that are not canonical, or that are a valid message of a kind outside the Outbox are answered with a Delivery Block:
- the failure is `malformed-message`, `noncanonical-message`, or `unsupported-wire-protocol` for a Wire Protocol Version the protocol does not know;
- the body's bytes equal `evaluate_delivery`'s for the same bytes, which needs no history for these cases;
- the block carries exactly the identity `evaluate_delivery` gives it: the installation and sequence for a valid but noncanonical Outbox Message, and none otherwise, even when the bytes contain readable identity fields;
- the block is recorded, and nothing else is written.

Tests:
- `test_malformed_bytes_are_blocked`
- `test_noncanonical_batch_is_blocked_with_its_identity`
- `test_non_outbox_message_is_blocked`
- `test_unsupported_wire_version_is_blocked`
- `test_malformed_batch_is_blocked_without_its_readable_identity`

### R4 — Identity and retry

A batch's identity is its installation, sequence, and message SHA-256.
- A second delivery of identical bytes at an archived sequence returns the exact archived receipt bytes and writes nothing. That holds even after the installation stops delivering, its generation stops being honored, or free space falls below the reserve.
- Different bytes at an archived sequence are blocked as `sequence-content-mismatch`.

Tests: `test_identical_retry_returns_the_archived_receipt`, `test_retry_is_answered_after_retirement_and_generation_stop`, `test_same_sequence_different_bytes_is_blocked`.

### R5 — Installation state

After identity, the installation's state decides whether delivery can proceed:
- **Active or draining:** delivery proceeds.
- **Pending:** retryable `temporarily-unavailable`, failure `installation-pending`. The Sender learns of its confirmation only when deliveries start being accepted (`manage-sender-installation@1` Decision 1).
- **Retired or withdrawn:** retryable `temporarily-unavailable`, failure `installation-ended`.
- **Unknown:** retryable `temporarily-unavailable`, failure `installation-unknown`.

A retryable answer writes nothing. A pending installation's batch is accepted once the Operator confirms it.

Tests:
- `test_pending_installation_is_retryable_until_confirmed`
- `test_ended_installations_are_retryable`
- `test_unknown_installation_is_retryable`
- `test_draining_installation_still_delivers`

### R6 — Sequence order and gaps

For a delivering installation:
- A sequence below its highest archived sequence that was never archived is blocked as `sequence-out-of-order`.
- A sequence more than one above it is accepted: `gap_after` names the highest archived sequence, or 0 when none, and the store records the gap. The receipt is unchanged by the gap.

Tests: `test_older_unseen_sequence_is_blocked`, `test_gap_is_accepted_and_reported`.

### R7 — Generation, versions, and limits

The batch is checked against the capability advertisement of the generation it was staged under, in this order:
1. **Generation:** a generation that is not honored, or whose advertisement lacks the batch's Wire Protocol Version, gives `unsupported-wire-protocol`.
2. **Schema:** a Source Projection schema version that the advertisement does not list for the batch's record type gives `unsupported-source-projection-schema`.
3. **Record count:** more items than the advertisement's maximum record count gives `record-count-limit-exceeded`.
4. **Size:** more bytes than its maximum message bytes gives `uncompressed-byte-limit-exceeded`.

A batch exactly at either limit is accepted.

Tests:
- `test_unhonored_generation_is_unsupported`
- `test_unsupported_schema_version_is_blocked`
- `test_limits_are_inclusive`
- `test_over_limit_batches_are_blocked`

### R8 — Fragment continuity

The store's delivery-cycle read for the batch's installation and cycle decides continuity, as in `ingest-protocol@1`:
- **New cycle:** a cycle with no archived fragment or completion starts only at source page 1, fragment 1, with the cycle's starting chain value.
- **Continuation:** a later fragment must meet all of these:
  - its cycle is not completed;
  - it has the same record type and cycle kind;
  - its previous chain equals the chain after the last archived fragment, computed with `chain_next`;
  - it is either the next fragment of the same page or fragment 1 of the next page.

Anything else is blocked as `fragment-order-mismatch`. Other installations' cycles never matter.

Tests:
- `test_a_cycle_starts_at_its_first_fragment`
- `test_fragments_continue_by_page_and_chain`
- `test_discontinuous_fragments_are_blocked`
- `test_completed_cycle_accepts_no_fragment`
- `test_cycles_of_other_installations_never_matter`

### R9 — Exact archival before receipt

A newly accepted batch is archived in one store transaction before its receipt is returned:
- the exact envelope bytes;
- the exact receipt bytes, and the receipt time `now`;
- the staged generation;
- the batch identity;
- every item in order: each projection item as its exact decoded bytes, and each retraction as its Source Identity.

Last Delivery becomes `now`. The receipt echoes the installation, sequence, and message SHA-256 exactly as `ingest-protocol@1` encodes it.

Tests: `test_accepted_batch_is_archived_exactly`, `test_receipt_echoes_the_message_identity`, `test_last_delivery_is_the_receipt_time`.

### R10 — Capacity and contention

The answer writes nothing in either case below:
- **Storage reserve exhausted:** retryable with reason `storage-reserve-exhausted`, failure `storage-reserve-exhausted`. An identical retry is still answered with its receipt (R4).
- **Store busy:** retryable `temporarily-unavailable`, failure `store-busy`. This applies whether the store is busy while reading, archiving, or recording a block. A block is never answered without its record.

Tests: `test_exhausted_reserve_is_retryable_and_writes_nothing`, `test_busy_store_is_retryable`.

### R11 — Conformance with the protocol's evaluation

For an active installation, every Ingest Batch answer equals `ingest-protocol@1`'s `evaluate_delivery` for the same history, which is built by evaluating the same accepted messages in order: the same outcome, the same exact body bytes, the same failure, and the same reported gap. Generated synthetic histories cover:
- interleaved cycles of two record types;
- retries;
- gaps;
- every batch-level block failure;
- completions archived between fragments;
- fragments refused after a completion.

Clean episodes guarantee completions: a fresh cycle of valid fragments, then its completion. The test asserts this coverage, so a history that stopped exercising any of it fails. Decode-level blocks are covered by R3.

Tests: `test_answers_match_the_protocol_evaluation`.

### R12 — Health-data-free evidence

Delivery Block records and every non-receipt response contain no Source Projection bytes, Source Identities, or health values. Only the archive holds them.

Tests: `test_blocks_and_responses_are_health_data_free`.

## Decisions for the Operator

1. **A Python leaf over three published units.** The store keeps its invariants; this unit adds the protocol's rules and decides the answer. It reads the store through one view, then archives; the store re-checks under its own transaction. The alternative, moving these rules into the store, would make the store depend on the protocol, contrary to `operational-store@1` Decision 1.
2. **The protocol's answer, from durable facts.** For a delivering installation, answers equal `evaluate_delivery`'s over the same history (R11). They are computed from the store, including `operational-store@2`'s delivery-cycle read, not from a replayed in-memory history. Undecodable bytes are answered by `evaluate_delivery` itself, which needs no history for them.
3. **Pending deliveries are retryable.** This is `manage-sender-installation@1` Decision 1's requirement: the Sender learns it is confirmed only when its deliveries start being accepted.
4. **Ended and unknown installations are retryable too.** Wire Protocol Version 1's Delivery Block vocabulary is closed and has no failure for a retired, withdrawn, or unknown installation. The Operator mediates these cases, as `manage-sender-installation@1` decided for the lifecycle. The Sender keeps the message and retries, and the Portal writes nothing. The alternatives are:
   - reuse an existing block failure, which would be false;
   - a Wire Protocol Version 2 with an installation-ended block, an expand–migrate–contract change for later.
5. **Dispatch.** The ingress shell decodes a delivery's kind and routes:
   - completions to `accept-sync-completion`;
   - checkpoints to `accept-sync-checkpoint`;
   - capability acknowledgements to `accept-capability-acknowledgement`;
   - Ingest Batches, and anything not a valid Outbox Message, here.

   This unit answers malformed and non-Outbox bytes so that every delivery gets exactly one protocol answer. The HTTP route is filed as an enabling task for the ingress shell, "Add the Outbox Message delivery route".
6. **Blocks are recorded; retryable answers are not.** Every Delivery Block becomes a health-data-free store record, coalesced by the store, so the Operations page can show it. Retryable answers leave no record, because the condition is transient or Operator-mediated.
7. **The receipt time is the Portal's `now`.** Last Delivery advances only when a new batch is archived (ADR 0049's freshness reads it), never on a retry.
8. **Items are archived as exact bytes.** A projection item's base64 `data` is decoded and stored as its own exact BLOB. A retraction is stored as its Source Identity. The whole envelope is stored too (ADR 0020).
9. **Writes are serialized by the caller.** The Portal ingress shell runs each governed call through `PortalWriter`, so no write interleaves between this unit's read and its archive. The store's own checks still refuse anything inconsistent: a store refusal that should not occur maps to the matching block or retryable answer.
10. **Free space is checked by the store, last.** This follows `operational-store@1`'s order: the reserve is checked immediately before writing, so a batch that would be blocked is blocked even when space is low (ADR 0033).

## Ground rules

- Fixtures are synthetic. Projections are arbitrary synthetic bytes, because the store and this unit never interpret them.
- The suite builds every message with `ingest-protocol@1`'s `encode_message`, and computes chains with `chain_start` and `chain_next`.
- Lock contention is injected in two ways:
  - by holding a write transaction on a second connection, with a 100 ms store timeout;
  - by a store proxy whose `read` or `record_delivery_block` raises the store's retryable `store-busy`.
- R11's histories come from fixed seeds. Each has 120 steps over two record types, with deliberate defects:
  - retries, gaps, and old and conflicting sequences;
  - broken chains and orders;
  - fragments after completion;
  - unsupported schemas and generations;
  - over-limit batches.

  Completions are archived as `accept-sync-completion` would archive them.
- Not mechanically provable here; the approver confirms this in implementation review: the ingress shell's dispatch, decompression, and request limits belong to the enabling task.

## Freeze prerequisites

The Operator approved the draft. The executable pack adds:
- the Python suite `tests/acceptance/test_accept_ingest_batch_1.py`, 34 tests;
- the runner `tests/acceptance/run_accept_ingest_batch_1.sh`, which runs the pinned-runtime check and then the suite;
- the Interface stub `src/activity_tracker_portal/accept_ingest_batch.py`, whose function raises `NotImplementedError`.

The tests pin these refinements:
- other installations' cycles never matter (R8);
- a busy store at any step is retryable, and a block is never answered without its record (R10);
- decode-level blocks include `unsupported-wire-protocol`, and carry exactly `evaluate_delivery`'s identity (R3).

Against the stub, every behaviour test fails: 31 tests and R11's 8 seeded histories. The two contract tests pass, and the runner exits 1.

A throwaway reference, kept out of the repository, passes all 34 tests. Its R11 histories cross-check about 1,020 answers against `evaluate_delivery`. They include every batch-level block failure, 235 receipts after gaps, 62 retries, 43 archived completions, and 81 fragments refused after completion. The reference kills all 27 mutants:
- ended installations blocked, pending ones delivered, and unknown ones raising;
- installation state checked before identity;
- each order, generation, schema, and limit check removed;
- inclusive limits made exclusive, and the two limits swapped;
- the page, chain, type, kind, completed-cycle, and first-fragment continuity rules relaxed;
- projections stored as base64 text;
- a gap reported off by one;
- a block unrecorded or without its identity;
- the wrong retryable reason;
- a busy read answered as a block;
- other kinds and naive times accepted;
- retries re-evaluated;
- a shifted receipt time.

GPT 6.1 Sol's read-only implementability review found no blocker. Its three P2 findings are resolved above:
- R11 now archives completions, and asserts its coverage;
- the decode-level `unsupported-wire-protocol` block is covered;
- block identity is exactly `evaluate_delivery`'s.

Its two notes are resolved in R1 and R2.

The binding is:
- **spec files:** the contract and this pack;
- **test files:** the suite and the runner.
