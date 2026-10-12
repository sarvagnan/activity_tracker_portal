# Draft acceptance pack — `ingest-protocol@1`

**Status:** Operator review amendments applied 2026-10-11 and the amended draft approved by the Operator on 2026-10-12. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator. **Pack drafter:** Claude. **Implementer after freeze:** Claude. The Operator reassigned both roles from Codex on 2026-10-11.

This unit carries the `ingest-protocol` Governed Scope contract in `governance/scopes/ingest-protocol.1.json`. The shared Module is pure: it defines exact language-neutral protocol behavior, while later Modules own Health Connect reads, Source Projection bytes, encryption, persistence, HTTP, Tailscale reachability, and operational effects.

## Requirements

Each requirement must be bound to named executable tests before freeze. Python (Portal) and TypeScript (Sender) are the languages that encode or decode wire messages, and both must consume the same repository vectors; a language-specific test that restates expected values is not an independent oracle. Kotlin produces opaque Source Projection bytes that the TypeScript shell embeds (ADR 0010) and does not implement this protocol.

### R1 — Governed Scope contract

The contract artifact names the scope, version, kind, purpose, every concrete input and its pure/effectful/volatile mode, outputs, every named failure with exactly one outcome class, observable effects, authority, dependencies, and acceptance oracle. The matching row in `docs/governance/scope-map.md` must not contradict it.

Proposed tests: `test_governed_scope_contract_has_required_shape`, `test_every_named_failure_has_one_outcome_class`, `test_scope_map_matches_ingest_protocol_contract`.

### R2 — Closed message union

Wire Protocol Version 1 has a closed discriminator for enrollment offer, enrollment confirmation, capability advertisement, capability acknowledgement, Ingest Batch, Snapshot Completion, Change Completion, Sync Checkpoint, Delivery Receipt, retryable failure, and Delivery Block. Unknown discriminators, missing or undeclared fields, duplicate JSON object keys, invalid UTF-8, trailing bytes, non-finite numbers, and schema-invalid values return `malformed-message`; no participant guesses an interpretation.

The enrollment offer carries the one-use enrollment code, the Portal's tailnet address, and the code's expiry. The enrollment confirmation carries the new Sender Installation's opaque identity and fingerprint. This unit defines their shape only; expiry, single use, and Operator confirmation are enforced by `manage-sender-installation`.

An Ingest Batch carries a closed, ordered list of items. Each item is either a Source Projection, carried as base64 of the exact bytes produced by `project-source-record` and opaque to this unit, or a Source Retraction naming the retracted record's Source Identity.

Proposed tests: `test_every_message_kind_has_positive_canonical_vector`, `test_unknown_or_malformed_message_is_refused_without_partial_value`, `test_enrollment_messages_carry_only_declared_fields`, `test_ingest_batch_items_are_opaque_projections_or_retractions`.

### R3 — Exact bytes and identity

Every message is encoded as RFC 8785 JSON Canonicalization Scheme (JCS) UTF-8 bytes. Envelope numbers are integers from 0 to 2^53−1, and an absent optional field is omitted rather than null, so no envelope depends on floating-point formatting or has two spellings of absence. Positive vectors pin the exact bytes for each message shape: encoding the vector value produces those bytes, and decoding and re-encoding them is byte-identical. Message identity is lowercase hexadecimal SHA-256 of those exact bytes.

Bytes that decode to a valid value but differ from that value's canonical encoding—in whitespace, key order, escaping, or number format—return `noncanonical-message` as a Delivery Block, so one message value has exactly one byte representation and one identity. The vectors, not an implementation's serializer defaults, are authoritative.

Proposed tests: `test_positive_vectors_encode_to_exact_bytes_and_hashes`, `test_decode_reencode_is_byte_identical`, `test_noncanonical_bytes_are_refused`, `test_byte_difference_changes_message_identity`.

### R4 — Independent version axes

Every message identifies its Wire Protocol Version. Every Ingest Batch also identifies exactly one record type and that type's independent Source Projection schema version. Support is judged against the capability generation the message was staged under (R9). A supported wire version with an unsupported projection schema returns `unsupported-source-projection-schema`; an unsupported wire version, or a staging generation the Portal no longer honors, returns `unsupported-wire-protocol`. Both are Delivery Blocks, and neither partially decodes or acknowledges the message.

Proposed tests: `test_wire_and_projection_versions_are_independent`, `test_unsupported_versions_block_without_partial_success`.

### R5 — Identity, retry, and sequence behavior

Every Outbox Message envelope contains its Sender Installation identity and positive sequence number, so the message SHA-256 covers both. Its identity is that installation, sequence, and SHA-256, and a Delivery Receipt echoes the tuple. Retrying identical bytes for an accepted tuple returns the same receipt. Reusing a sequence with different bytes returns `sequence-content-mismatch`.

A higher sequence after a genuine loss is accepted, and the evaluation additionally reports the gap—the last previously accepted sequence—for the Portal to record. The receipt itself is unchanged, and no later message is renumbered. A sequence below the highest accepted sequence that was never itself accepted returns `sequence-out-of-order`. Both refusals are Delivery Blocks.

Proposed tests: `test_receipt_echoes_exact_message_identity`, `test_identical_retry_returns_same_receipt`, `test_same_sequence_different_bytes_blocks`, `test_gap_is_reported_without_changing_receipt`, `test_older_unseen_sequence_blocks`.

### R6 — Deterministic fragmentation

One projected source page of one record type is partitioned greedily in source order: each fragment takes as many consecutive items as fit within both the maximum record count and the maximum exact uncompressed message bytes, measured on the complete encoded envelope, and the next item starts a new fragment. A page with no items produces exactly one fragment with an empty item list. Every fragment carries its cycle kind (`initial-snapshot`, `reconciliation-snapshot`, or `change-cycle`), cycle identity, record type, source-page index, a one-based fragment index contiguous within its page, and the chain value preceding it.

An item is never split. An item whose single-item fragment would exceed the byte limit returns `indivisible-record-limit-exceeded` as a Sender-side staging refusal: nothing is staged, and the next Sync Checkpoint reports the type `blocked` with reason `record-too-large`.

Each cycle has one fragment chain spanning all its pages. The chain starts at SHA-256 of the cycle identity's UTF-8 bytes. After each fragment, the chain value becomes SHA-256 of the previous chain value's 32 bytes followed by the fragment's 32-byte message SHA-256. Because a fragment carries the value before it, the Portal detects an out-of-order fragment on arrival with `fragment-order-mismatch`, a Delivery Block.

Proposed tests: `test_partition_is_greedy_and_respects_both_limits`, `test_empty_page_is_one_empty_fragment`, `test_fragments_are_contiguous_and_chain_ordered`, `test_chain_starts_from_cycle_identity`, `test_record_is_never_split`, `test_oversized_indivisible_record_is_refused_before_staging`.

### R7 — Receipt-bound completion

Snapshot Completion uses one structural shape with an explicit `initial` or `reconciliation` snapshot kind. Snapshot Completion and Change Completion carry record type, cycle identity, final source-page, fragment, and record counts, and the final chain value; they contain no source cursor, source token, health value, or Source Identity. A cycle with no pages is valid: its completion carries zero counts and the chain's starting value. When an empty cycle is sent is decided by the Sender synchronization scopes, not this unit.

Validation against accepted fragments never force-accepts the original message and returns one of these Delivery Blocks:

- `completion-identity-mismatch` when the record type differs from the cycle's accepted fragments or a different completion for that cycle was already accepted;
- `completion-kind-mismatch` when the completion's kind differs from its fragments' cycle kind;
- `completion-count-mismatch` when any final count differs from the accepted fragments;
- `completion-chain-mismatch` when the final chain value differs from the accepted chain;
- `completion-sequence-gap` when an accepted sequence gap falls within the claimed chain.

A fragment for a cycle whose completion is already accepted returns `fragment-order-mismatch`.

Proposed tests: `test_snapshot_kinds_share_one_closed_shape`, `test_completion_contains_only_allowed_proof_fields`, `test_empty_cycle_completion_is_valid`, `test_completion_matches_complete_fragment_chain`, `test_each_completion_mismatch_blocks_with_its_name`, `test_fragment_after_completion_blocks`.

### R8 — Health-data-free polling checkpoint

One Sync Checkpoint closes one Polling Run and carries its run sequence and trigger, Sender wall-clock start and end, optional monotonic elapsed duration, Background Scheduling Status, each permitted record type's lifecycle outcome and any reason, a separate source-data-staged flag, and Outbox Message and byte counts sampled before checkpoint insertion. Every value list is closed:

- lifecycle outcomes: `not-due`, `no-change`, `progressed`, `completed`, `deferred`, `blocked`, `not-permitted`, `failed`, `interrupted`;
- triggers: `background`, `foreground`, `manual`;
- Background Scheduling Status: `available`, `restricted`, `unregistered`;
- reasons: `collection-disabled`, `projection-refusal`, `record-too-large`, `permission-missing`, `delivery-blocked`, `storage-pressure`, `screen-lock-removed`, `changes-token-expired`, `time-budget-reached`, `read-failed`.

The vectors fix which outcomes carry a reason. Because the schema is closed, source-record counts, source tokens, Source Identities, health dates, health values, and free-text failure strings have no field to occupy, and any attempt to include one returns `malformed-message`. Adding a value to any list requires a new Wire Protocol Version through R10, because an older Portal would otherwise block the checkpoint and everything queued behind it.

Proposed tests: `test_checkpoint_accepts_every_allowlisted_value`, `test_checkpoint_refuses_each_forbidden_data_category_as_malformed`, `test_checkpoint_counts_are_explicitly_preinsertion`.

### R9 — Capability generations and offline staging

A capability advertisement has a monotonically increasing generation, accepted Wire Protocol Versions, accepted per-type Source Projection schema versions, maximum record count, and maximum exact uncompressed message bytes. The Sender durably caches an advertised generation for offline staging and acknowledges it with a capability acknowledgement, an Outbox Message under the same sequence, ordering, retry, and receipt rules as every other.

Every Outbox Message records the capability generation it was staged under, and the Portal evaluates its versions and limits against that generation. New limits apply only to messages staged afterwards, and no compatibility decision rewrites queued bytes. Queuing the acknowledgement and switching staging to the new generation in one Sender transaction belongs to the Sender scope that stages it, not to this unit.

Proposed tests: `test_acknowledgement_is_an_ordered_outbox_message`, `test_message_is_evaluated_under_its_staging_generation`, `test_new_limits_do_not_reinterpret_queued_messages`.

### R10 — Expand–migrate–contract safety

The Portal honors a set of capability generations. A proposed advertisement returns exactly one compatibility decision:

- `capability-generation-regression` when its generation is not greater than every earlier generation;
- `capability-contraction-blocked` when it stops honoring a generation before the Portal has accepted a capability acknowledgement for a later generation;
- otherwise, accept.

Strict ordering makes that acknowledgement the proof: every message staged under the retired generation or earlier was queued before it and has therefore already been accepted. Adding a version or raising a limit is a new generation and needs no coordinated deployment. Removing a version or lowering a limit reaches queued messages only once every generation that allowed it is retired under this rule. The protocol makes no claim about when deployment may remove code beyond that evidence.

Proposed tests: `test_protocol_expansion_needs_no_coordination`, `test_capability_generation_cannot_regress`, `test_retiring_a_generation_requires_a_later_acknowledgement`.

### R11 — Outcome trichotomy

Every evaluation of a delivered message returns exactly one of Delivery Receipt, retryable failure, or Delivery Block. A retryable failure carries exactly one reason, `storage-reserve-exhausted` or `temporarily-unavailable`; when the Portal issues each is decided by the accepting scopes. A Delivery Block is Operator-action-required and preserves the blocked message's identity.

On the Sender, a response that decodes as a retryable failure or Delivery Block is that outcome. A response counts as acceptance only if it decodes as a Delivery Receipt echoing the exact installation, sequence, and SHA-256 of the message sent; any other response, including a receipt for a different message, returns the retryable `receipt-mismatch`. Mapping unknown transport outcomes to identical-byte retry belongs to `send-outbox-message`, not this unit.

Proposed tests: `test_delivery_outcome_is_closed_and_exclusive`, `test_retryable_failure_reasons_are_closed`, `test_only_an_exact_receipt_counts_as_acceptance`.

### R12 — Cross-language conformance and privacy

The repository contains versioned JSON Schemas plus canonical positive and negative byte vectors under `protocol/v1/`. The Python and TypeScript implementations each run every applicable vector and report the same exact bytes, hashes, decoded values, and named outcomes. Kotlin joins these vectors only if it later encodes or decodes wire messages. Fixtures are synthetic and contain no personal health payloads, real device identifiers, credentials, tailnet names, or secrets.

Proposed tests: `test_vector_inventory_is_complete_and_deterministic`, `test_python_conforms_to_shared_vectors`, `test_typescript_conforms_to_shared_vectors`, `test_fixtures_are_synthetic_and_secret_free`.

## Ground rules

- This pack governs the pure shared protocol Module. It does not accept an HTTP endpoint, database transaction, Health Connect read, cryptographic keystore operation, or UI as evidence for this unit.
- Portable Export schemas and fixtures are a different contract and cannot satisfy these tests.
- Tests exercise the same Interface used by production encoders and decoders; a vector-only special case is not conformant.
- Exact failure names and their outcome classes in `governance/scopes/ingest-protocol.1.json` are part of the proposed contract. Changing one before freeze is ordinary review; changing one after freeze requires a Vouch deviation and amended pack.
- This unit introduces the project's first TypeScript package—a standalone protocol library, not the Expo app—and its Node toolchain in CI.
- Health Connect physical-device qualification is not evidence for this pure unit. It gates the later native Source Projection Adapter release.

## Freeze prerequisites

The Operator must approve this draft. Claude must then add the executable Python and TypeScript test files and the complete initial vector inventory, verify every tested Interface name against the real tree, demonstrate a genuine RED against the pre-implementation tree, and propose the exact pack binding—including this spec, the Governed Scope contract, executable acceptance tests, vector manifest, and runner configuration. Only the Operator may authorize the subsequent freeze.
