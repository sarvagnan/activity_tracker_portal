# Wire Protocol Version 1

This is the normative wire reference for `ingest-protocol@1`. Requirements and their tests live in `packs/ingest-protocol-1.spec.md`; this document fixes the field-level detail those requirements leave to the protocol. Where this document and a vector disagree, the vector's exact bytes are authoritative and the disagreement is a defect to resolve through Vouch.

## Encoding

Every message is one JSON object encoded with RFC 8785 JSON Canonicalization Scheme (JCS) as UTF-8, with no byte-order mark and no trailing newline. Message identity is the lowercase hexadecimal SHA-256 of those exact bytes.

Message values use only objects with ASCII `snake_case` keys, strings, booleans, arrays, and integers from 0 to 2^53−1. There are no floating-point numbers and no `null`; an absent optional field is omitted. Number tokens must be written as `0` or a non-zero digit followed by digits: a sign, fraction, exponent, or value above 2^53−1 is malformed rather than merely noncanonical.

## Decoding order

A decoder applies these checks in order and reports the first failure:

1. The bytes are valid UTF-8, contain exactly one JSON value with only whitespace around it, contain no duplicate object key, and use only permitted number tokens. Otherwise `malformed-message`.
2. The value is an object whose `wire_version` is an integer. Otherwise `malformed-message`.
3. `wire_version` is 1. Otherwise `unsupported-wire-protocol`.
4. `kind` names one of the eleven message kinds, and the value satisfies that kind's schema in `schemas/messages.schema.json` and the rules below that a schema cannot express. Otherwise `malformed-message`.
5. Re-encoding the decoded value reproduces the input bytes exactly. Otherwise `noncanonical-message`.

Rules beyond the schema:

- `wire_versions` and each `versions` list are strictly ascending.
- `projection_schemas` and a checkpoint's `record_types` are strictly ascending by `record_type`.
- Every timestamp names a real calendar instant.
- An enrollment confirmation's `fingerprint` equals the installation fingerprint of its `sender_installation`.

## Field formats

| Format | Rule |
|---|---|
| UUID | Lowercase RFC 9562 text form, used for `sender_installation` and `cycle_id` |
| SHA-256 | 64 lowercase hexadecimal characters |
| Timestamp | UTC with millisecond precision, `YYYY-MM-DDTHH:MM:SS.sssZ` |
| Record type | Lowercase words joined by single hyphens, at most 64 characters, such as `heart-rate`; the type catalog belongs to `project-source-record` |
| Base64 | RFC 4648 standard alphabet with padding and zero unused bits, at least one encoded byte |
| Source Identity | 1 to 256 Unicode code points, none of them a control character below U+0020 |
| Enrollment code | 16 characters from the RFC 4648 base32 alphabet `A–Z2–7` |
| Portal URL | `http` or `https` origin with a lowercase host and optional port, without path |
| Installation fingerprint | SHA-256 of the `sender_installation` UTF-8 bytes |

## Message kinds

Every message has `wire_version` (1) and `kind`. The five Outbox Message kinds also carry `sender_installation`, a positive `sequence`, and the positive `capability_generation` they were staged under.

| Kind | Direction | Additional fields |
|---|---|---|
| `enrollment-offer` | Portal to Sender | `enrollment_code`, `portal_url`, `expires_at` |
| `enrollment-confirmation` | Sender to Portal | `enrollment_code`, `sender_installation`, `fingerprint` |
| `capability-advertisement` | Portal to Sender | `generation`, `wire_versions`, `projection_schemas` (each `record_type` with its `versions`), `max_record_count` (at least 1), `max_message_bytes` (at least 4096) |
| `capability-acknowledgement` | Outbox | none; its `capability_generation` is the acknowledged generation |
| `ingest-batch` | Outbox | `record_type`, `projection_schema_version`, `cycle_kind`, `cycle_id`, `source_page_index`, `fragment_index`, `previous_chain`, `items` |
| `snapshot-completion` | Outbox | `snapshot_kind`, `record_type`, `cycle_id`, `page_count`, `fragment_count`, `record_count`, `final_chain` |
| `change-completion` | Outbox | `record_type`, `cycle_id`, `page_count`, `fragment_count`, `record_count`, `final_chain` |
| `sync-checkpoint` | Outbox | `run_sequence`, `trigger`, `started_at`, `ended_at`, optional `elapsed_ms`, `background_scheduling`, `record_types`, `outbox_messages_before_checkpoint`, `outbox_bytes_before_checkpoint` |
| `delivery-receipt` | Portal to Sender | `sender_installation`, `sequence`, `message_sha256` |
| `retryable-failure` | Portal to Sender | `reason`: `storage-reserve-exhausted` or `temporarily-unavailable` |
| `delivery-block` | Portal to Sender | `failure`, `message_sha256`, and either both or neither of `sender_installation` and `sequence` |

An Ingest Batch item is either `{"kind": "projection", "data": <base64>}` or `{"kind": "retraction", "source_identity": <Source Identity>}`. Projection bytes are opaque to this protocol. `cycle_kind` is `initial-snapshot`, `reconciliation-snapshot`, or `change-cycle`; `snapshot_kind` is `initial` or `reconciliation`. Page and fragment indexes are one-based.

A Sync Checkpoint `record_types` entry carries `record_type`, `outcome`, optional `reason`, and `source_data_staged`. A reason is required when the outcome is `deferred`, `blocked`, or `failed`; optional when it is `progressed` or `interrupted`; and absent otherwise. The closed value lists are those in requirement R8.

A Delivery Block's `failure` is one of the fourteen `delivery-block` names in the Governed Scope contract. It carries `sender_installation` and `sequence` exactly when the blocked bytes decoded as a schema-valid Outbox Message.

## Fragment chain

A cycle's chain starts at SHA-256 of its `cycle_id` UTF-8 bytes. After each accepted fragment, the next value is SHA-256 of the previous value's 32 bytes followed by the fragment's 32-byte message SHA-256. Each fragment's `previous_chain` is the value before it, and a completion's `final_chain` is the value after its last fragment, or the starting value for a cycle with no pages.

## Partitioning

The Sender partitions one source page into consecutive fragments with consecutive sequences starting at the page's first sequence. Each fragment starts empty and takes the next item while it holds fewer than `max_record_count` items and its complete encoding including that item stays within `max_message_bytes`. The next item then starts a new fragment. A page with no items produces exactly one fragment with an empty `items` list. If an item does not fit in an otherwise empty fragment, the whole page is refused with `indivisible-record-limit-exceeded` and nothing is produced.

## Delivery evaluation

The Portal evaluates a delivered message against its honored capability generations and the delivery history of its Sender Installation. A Delivery Block leaves that history unchanged. Checks run in this order:

1. Decoding fails: that failure.
2. The kind is not an Outbox Message kind: `malformed-message`.
3. The sequence was accepted before: identical bytes return the original receipt, and different bytes return `sequence-content-mismatch`. A sequence below the highest accepted sequence that was never accepted returns `sequence-out-of-order`.
4. The staging generation is not honored, or does not include the message's wire version: `unsupported-wire-protocol`.
5. For an Ingest Batch:
   - its record type or projection schema version is not in the staging generation: `unsupported-source-projection-schema`;
   - more items than `max_record_count`: `record-count-limit-exceeded`;
   - more bytes than `max_message_bytes`: `uncompressed-byte-limit-exceeded`;
   - the fragment does not continue its cycle: `fragment-order-mismatch`. A cycle's first fragment must be page 1, fragment 1, with the chain's starting value. A later fragment must match the cycle's kind and record type, carry the current chain value, and be either the next fragment on the current page or fragment 1 of the next page. A fragment for a completed cycle never continues it.
6. For a completion:
   - `completion-identity-mismatch` when the cycle is complete or its record type differs;
   - `completion-kind-mismatch` when its kind does not match the cycle kind of the cycle's fragments;
   - `completion-sequence-gap` when any sequence from the cycle's first fragment up to the completion was never accepted;
   - `completion-count-mismatch` when a count differs from the accepted fragments, which are none for an unknown cycle;
   - `completion-chain-mismatch` when `final_chain` differs from the cycle's chain.
7. Otherwise the message is accepted. A capability acknowledgement raises the latest acknowledged generation to its `capability_generation` when higher. When the sequence exceeds the highest previously accepted sequence by more than one, the evaluation also reports that highest sequence (0 if none) as the gap.

The response is a Delivery Receipt for an accepted message and a Delivery Block otherwise.

## Sender response interpretation

For the message it sent, the Sender treats a Portal response as:

- accepted, when it decodes as a Delivery Receipt echoing the sent installation, sequence, and SHA-256;
- retryable with that reason, when it decodes as a retryable failure;
- blocked with that failure, when it decodes as a Delivery Block whose `message_sha256`, and any identity fields, match the sent message;
- retryable `receipt-mismatch`, for anything else.

## Compatibility decision

A proposed set of honored capability generations is compared with the current set and the latest accepted capability acknowledgement:

1. `capability-generation-regression` when a proposed generation number also present in the current set has different contents, or a new generation number is not greater than every current one.
2. `capability-contraction-blocked` when a current generation is absent from the proposal and no acknowledgement for a later generation has been accepted.
3. Otherwise, accept.

## Vectors

`vectors/manifest.json` lists every vector file with its SHA-256 and the languages that must run it. Message, refusal, and identity vectors apply to both Python and TypeScript. Partition and response-interpretation vectors are Sender behavior and apply to TypeScript. Delivery and compatibility vectors are Portal behavior and apply to Python. `tools/build_vectors.py` regenerates every file deterministically from hand-written cases; it is fixture tooling, and production code must not import it.

All fixtures are synthetic:

- UUIDs start with `00000000-0000-4000-8000-`;
- Portal hosts end in `.invalid`;
- Source Identities start with `synthetic-`;
- projection bytes start with `{"synthetic":`.

The TypeScript implementation runs under Node for tests and under Hermes in the Sender, so it must not depend on Node built-in modules.
