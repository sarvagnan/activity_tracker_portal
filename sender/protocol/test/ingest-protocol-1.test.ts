// Acceptance tests for `ingest-protocol@1` on the TypeScript Sender.
//
// The shared vectors under protocol/v1/vectors/ are the oracle; these tests
// only route each vector to the production Interface. Requirement numbers refer
// to packs/ingest-protocol-1.spec.md.

import { test, type TestContext } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

import {
  ProtocolRefusal,
  chainNext,
  chainStart,
  decodeMessage,
  encodeMessage,
  installationFingerprint,
  interpretResponse,
  messageSha256,
  partitionPage,
} from "../src/index.ts";

const VECTORS = new URL("../../../protocol/v1/vectors/", import.meta.url);

function load(name: string): any {
  return JSON.parse(readFileSync(new URL(name, VECTORS), "utf8"));
}

function raw(field: { text?: string; base64?: string }): Uint8Array {
  if (field.text !== undefined) {
    return new TextEncoder().encode(field.text);
  }
  return new Uint8Array(Buffer.from(field.base64 as string, "base64"));
}

function hex(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString("hex");
}

function assertBytes(actual: Uint8Array, expected: Uint8Array): void {
  assert.equal(hex(actual), hex(expected));
}

const MESSAGES: any[] = load("messages.json").vectors;
const REFUSALS: any[] = load("refusals.json").vectors;
const IDENTITIES: any = load("identities.json");
const PARTITION: any[] = load("partition.json").vectors;
const RESPONSES: any[] = load("responses.json").vectors;

const MESSAGE_KINDS = [
  "capability-acknowledgement",
  "capability-advertisement",
  "change-completion",
  "delivery-block",
  "delivery-receipt",
  "enrollment-confirmation",
  "enrollment-offer",
  "ingest-batch",
  "retryable-failure",
  "snapshot-completion",
  "sync-checkpoint",
];
const OUTBOX_FIELDS = ["capability_generation", "kind", "sender_installation", "sequence", "wire_version"];
const COMPLETION_PROOF_FIELDS = ["cycle_id", "final_chain", "fragment_count", "page_count", "record_count", "record_type"];

function tagged(vectors: any[], tag: string): any[] {
  const selected = vectors.filter((vector) => vector.tags.includes(tag));
  assert.ok(selected.length > 0, `no vectors tagged ${tag}`);
  return selected;
}

async function each(t: TestContext, vectors: any[], check: (vector: any) => void): Promise<void> {
  for (const vector of vectors) {
    await t.test(vector.name, () => check(vector));
  }
}

function assertDecodes(vector: any): void {
  assert.deepStrictEqual(decodeMessage(raw(vector.bytes)), vector.value);
}

function assertRefused(vector: any): void {
  assert.throws(
    () => decodeMessage(raw(vector.bytes)),
    (error: unknown) => error instanceof ProtocolRefusal && error.failure === vector.failure,
  );
}

function assertPartition(vector: any): void {
  const partition = partitionPage(vector.page);
  assert.deepStrictEqual(
    partition.fragments.map(hex),
    vector.expected.fragments.map((fragment: any) => hex(raw(fragment))),
  );
  assert.equal(partition.next_chain, vector.expected.next_chain);
  assert.equal(partition.next_sequence, vector.expected.next_sequence);
}

function fragmentsOf(vector: any): any[] {
  return vector.expected.fragments.map((fragment: any) => JSON.parse(new TextDecoder().decode(raw(fragment))));
}

function checkChains(): void {
  for (const chain of IDENTITIES.chains) {
    let value = chainStart(chain.cycle_id);
    assert.equal(value, chain.start);
    for (const step of chain.steps) {
      value = chainNext(value, step.message_sha256);
      assert.equal(value, step.chain);
    }
  }
}

// R2 — Closed message union

test("test_every_message_kind_has_positive_canonical_vector", async (t) => {
  assert.deepStrictEqual([...new Set(MESSAGES.map((vector) => vector.value.kind))].sort(), MESSAGE_KINDS);
  await each(t, MESSAGES, assertDecodes);
});

test("test_unknown_or_malformed_message_is_refused_without_partial_value", async (t) => {
  const vectors = tagged(REFUSALS, "malformed");
  assert.ok(vectors.every((vector) => vector.failure === "malformed-message"));
  await each(t, vectors, assertRefused);
});

test("test_enrollment_messages_carry_only_declared_fields", async (t) => {
  await each(t, tagged(MESSAGES, "enrollment"), assertDecodes);
  await each(t, tagged(REFUSALS, "enrollment"), assertRefused);
  for (const entry of IDENTITIES.fingerprints) {
    assert.equal(installationFingerprint(entry.sender_installation), entry.fingerprint);
  }
});

test("test_ingest_batch_items_are_opaque_projections_or_retractions", async (t) => {
  for (const vector of tagged(MESSAGES, "ingest-items")) {
    for (const item of vector.value.items) {
      assert.ok(item.kind === "projection" || item.kind === "retraction");
    }
  }
  await each(t, tagged(MESSAGES, "ingest-items"), assertDecodes);
  await each(t, tagged(MESSAGES, "opaque"), assertDecodes);
  await each(t, tagged(REFUSALS, "ingest-items"), assertRefused);
});

// R3 — Exact bytes and identity

test("test_positive_vectors_encode_to_exact_bytes_and_hashes", async (t) => {
  await each(t, MESSAGES, (vector) => {
    const bytes = raw(vector.bytes);
    assert.equal(createHash("sha256").update(bytes).digest("hex"), vector.sha256);
    assertBytes(encodeMessage(vector.value), bytes);
    assert.equal(messageSha256(bytes), vector.sha256);
  });
});

test("test_decode_reencode_is_byte_identical", async (t) => {
  await each(t, MESSAGES, (vector) => {
    const bytes = raw(vector.bytes);
    assertBytes(encodeMessage(decodeMessage(bytes)), bytes);
  });
});

test("test_noncanonical_bytes_are_refused", async (t) => {
  const vectors = tagged(REFUSALS, "noncanonical");
  assert.ok(vectors.every((vector) => vector.failure === "noncanonical-message"));
  await each(t, vectors, assertRefused);
});

test("test_byte_difference_changes_message_identity", async (t) => {
  await each(t, MESSAGES, (vector) => {
    const bytes = raw(vector.bytes);
    const spaced = new Uint8Array([...bytes.subarray(0, -1), 0x20, bytes[bytes.length - 1]]);
    assert.notEqual(messageSha256(spaced), vector.sha256);
    assert.throws(
      () => decodeMessage(spaced),
      (error: unknown) => error instanceof ProtocolRefusal && error.failure === "noncanonical-message",
    );
  });
});

// R4 — Independent version axes (Sender side)

test("test_wire_and_projection_versions_are_independent", async (t) => {
  const vectors = tagged(REFUSALS, "unsupported-wire");
  assert.ok(vectors.every((vector) => vector.failure === "unsupported-wire-protocol"));
  await each(t, vectors, assertRefused);
});

// R6 — Deterministic fragmentation

test("test_partition_is_greedy_and_respects_both_limits", async (t) => {
  await each(t, tagged(PARTITION, "greedy"), (vector) => {
    for (const fragment of vector.expected.fragments) {
      const bytes = raw(fragment);
      assert.ok(bytes.length <= vector.page.max_message_bytes);
      assert.ok(JSON.parse(new TextDecoder().decode(bytes)).items.length <= vector.page.max_record_count);
    }
    assertPartition(vector);
  });
});

test("test_empty_page_is_one_empty_fragment", async (t) => {
  await each(t, tagged(PARTITION, "empty-page"), (vector) => {
    assert.deepStrictEqual(fragmentsOf(vector).map((fragment) => fragment.items), [[]]);
    assertPartition(vector);
  });
  await each(t, tagged(MESSAGES, "empty-page"), assertDecodes);
});

test("test_fragments_are_contiguous_and_chain_ordered", async (t) => {
  await each(
    t,
    PARTITION.filter((vector) => vector.expected.fragments),
    (vector) => {
      const partition = partitionPage(vector.page);
      let chain = vector.page.previous_chain;
      partition.fragments.forEach((bytes, index) => {
        const fragment = decodeMessage(bytes);
        assert.equal(fragment.fragment_index, index + 1);
        assert.equal(fragment.sequence, vector.page.first_sequence + index);
        assert.equal(fragment.source_page_index, vector.page.source_page_index);
        assert.equal(fragment.previous_chain, chain);
        chain = chainNext(chain, messageSha256(bytes));
      });
      assert.equal(partition.next_chain, chain);
    },
  );
});

test("test_chain_starts_from_cycle_identity", () => {
  checkChains();
});

test("test_record_is_never_split", async (t) => {
  await each(
    t,
    PARTITION.filter((vector) => vector.expected.fragments),
    (vector) => {
      assert.deepStrictEqual(
        fragmentsOf(vector).flatMap((fragment) => fragment.items),
        vector.page.items,
      );
      assertPartition(vector);
    },
  );
});

test("test_oversized_indivisible_record_is_refused_before_staging", async (t) => {
  await each(t, tagged(PARTITION, "indivisible"), (vector) => {
    assert.equal(vector.expected.refusal, "indivisible-record-limit-exceeded");
    assert.throws(
      () => partitionPage(vector.page),
      (error: unknown) => error instanceof ProtocolRefusal && error.failure === vector.expected.refusal,
    );
  });
});

// R7 — Receipt-bound completion (Sender side)

test("test_snapshot_kinds_share_one_closed_shape", async (t) => {
  const snapshots = MESSAGES.filter((vector) => vector.value.kind === "snapshot-completion");
  assert.deepStrictEqual(snapshots.map((vector) => vector.value.snapshot_kind).sort(), ["initial", "reconciliation"]);
  assert.equal(new Set(snapshots.map((vector) => Object.keys(vector.value).sort().join())).size, 1);
  await each(t, snapshots, assertDecodes);
});

test("test_completion_contains_only_allowed_proof_fields", async (t) => {
  for (const vector of tagged(MESSAGES, "completion")) {
    const allowed = [...OUTBOX_FIELDS, ...COMPLETION_PROOF_FIELDS];
    if (vector.value.kind === "snapshot-completion") {
      allowed.push("snapshot_kind");
    }
    assert.deepStrictEqual(Object.keys(vector.value).sort(), allowed.sort());
  }
  await each(t, tagged(MESSAGES, "completion"), assertDecodes);
  await each(t, tagged(REFUSALS, "completion-fields"), assertRefused);
});

test("test_empty_cycle_completion_is_valid", async (t) => {
  await each(t, tagged(MESSAGES, "empty-cycle"), assertDecodes);
});

// R8 — Health-data-free polling checkpoint

test("test_checkpoint_accepts_every_allowlisted_value", async (t) => {
  await each(t, tagged(MESSAGES, "checkpoint"), assertDecodes);
  await each(t, tagged(REFUSALS, "checkpoint-values"), assertRefused);
});

test("test_checkpoint_refuses_each_forbidden_data_category_as_malformed", async (t) => {
  const vectors = tagged(REFUSALS, "checkpoint-privacy");
  assert.equal(new Set(vectors.map((vector) => vector.category)).size, 6);
  await each(t, vectors, assertRefused);
});

test("test_checkpoint_counts_are_explicitly_preinsertion", async (t) => {
  for (const vector of tagged(MESSAGES, "checkpoint")) {
    assert.ok("outbox_messages_before_checkpoint" in vector.value);
    assert.ok("outbox_bytes_before_checkpoint" in vector.value);
  }
  await each(t, tagged(MESSAGES, "checkpoint"), assertDecodes);
  await each(t, tagged(REFUSALS, "checkpoint-counts"), assertRefused);
});

// R9 — Capability generations and offline staging (Sender side)

test("test_acknowledgement_is_an_ordered_outbox_message", async (t) => {
  for (const vector of tagged(MESSAGES, "acknowledgement")) {
    assert.deepStrictEqual(Object.keys(vector.value).sort(), [...OUTBOX_FIELDS].sort());
  }
  await each(t, tagged(MESSAGES, "acknowledgement"), assertDecodes);
  await each(t, tagged(REFUSALS, "acknowledgement"), assertRefused);
});

// R11 — Outcome trichotomy (Sender side)

test("test_delivery_outcome_is_closed_and_exclusive", async (t) => {
  await each(t, RESPONSES, (vector) => {
    const outcome: any = interpretResponse(vector.sent, raw(vector.response));
    assert.ok(["accepted", "retryable", "block"].includes(outcome.outcome));
    assert.equal("failure" in outcome, outcome.outcome !== "accepted");
  });
});

test("test_retryable_failure_reasons_are_closed", async (t) => {
  const vectors = tagged(MESSAGES, "retryable");
  assert.deepStrictEqual(
    vectors.map((vector) => vector.value.reason).sort(),
    ["storage-reserve-exhausted", "temporarily-unavailable"],
  );
  await each(t, vectors, assertDecodes);
  await each(t, tagged(REFUSALS, "retryable"), assertRefused);
});

test("test_only_an_exact_receipt_counts_as_acceptance", async (t) => {
  for (const vector of RESPONSES) {
    assert.equal(vector.expected.outcome === "accepted", vector.tags.includes("exact-receipt"));
  }
  await each(t, RESPONSES, (vector) => {
    assert.deepStrictEqual(interpretResponse(vector.sent, raw(vector.response)), vector.expected);
  });
});

// R12 — Cross-language conformance

test("test_typescript_conforms_to_shared_vectors", async (t) => {
  await each(t, MESSAGES, (vector) => {
    const bytes = raw(vector.bytes);
    assert.deepStrictEqual(decodeMessage(bytes), vector.value);
    assertBytes(encodeMessage(vector.value), bytes);
    assert.equal(messageSha256(bytes), vector.sha256);
  });
  await each(t, REFUSALS, assertRefused);
  for (const entry of IDENTITIES.fingerprints) {
    assert.equal(installationFingerprint(entry.sender_installation), entry.fingerprint);
  }
  checkChains();
  await each(t, PARTITION, (vector) => {
    if (vector.expected.refusal) {
      assert.throws(
        () => partitionPage(vector.page),
        (error: unknown) => error instanceof ProtocolRefusal && error.failure === vector.expected.refusal,
      );
    } else {
      assertPartition(vector);
    }
  });
  await each(t, RESPONSES, (vector) => {
    assert.deepStrictEqual(interpretResponse(vector.sent, raw(vector.response)), vector.expected);
  });
});
