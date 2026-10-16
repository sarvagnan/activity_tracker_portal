import assert from "node:assert/strict";
import test from "node:test";
import { resultMessage } from "../src/shell/result-message.ts";
import type { ShellRunResult } from "../src/shell/compose-sender.ts";
import type { OutcomeReason, RecordTypeOutcome, TypeOutcome } from "../src/outbox/stage-sync-checkpoint.ts";

function entry(outcome: TypeOutcome, reason?: OutcomeReason): RecordTypeOutcome {
  return { recordType: "synthetic-private-record", outcome, sourceDataStaged: false,
    ...(reason === undefined ? {} : { reason }) };
}
function checkpoint(...recordTypes: RecordTypeOutcome[]): ShellRunResult {
  return { outcome: "checkpointed", runSequence: 1, summary: {
    runSequence: 1, trigger: "manual", startedAtMs: 0, endedAtMs: 1,
    backgroundScheduling: "available", recordTypes,
  } };
}

test("sync finished requires every type to have a successful or recoverable outcome", () => {
  const successes = ["no-change", "progressed", "completed", "not-due", "interrupted"] as const;
  for (const outcome of successes) assert.equal(resultMessage(checkpoint(entry(outcome))), "Sync finished.");
  assert.equal(resultMessage(checkpoint(...successes.map((outcome) => entry(outcome)))), "Sync finished.");
  for (const outcome of ["progressed", "interrupted"] as const) {
    for (const reason of ["changes-token-expired", "time-budget-reached"] as const) {
      assert.equal(resultMessage(checkpoint(entry(outcome, reason))), "Sync finished.");
    }
  }
});

const reasonCases: readonly [OutcomeReason, TypeOutcome, string][] = [
  ["collection-disabled", "deferred", "collection disabled"],
  ["projection-refusal", "blocked", "projection blocked"],
  ["record-too-large", "blocked", "record exceeds the delivery limit"],
  ["permission-missing", "blocked", "Health Connect permission missing"],
  ["delivery-blocked", "blocked", "delivery blocked"],
  ["storage-pressure", "blocked", "storage pressure"],
  ["screen-lock-removed", "blocked", "secure screen lock required"],
  ["time-budget-reached", "deferred", "time budget reached"],
  ["projection-refusal", "progressed", "projection blocked"],
  ["record-too-large", "progressed", "record exceeds the delivery limit"],
  ["permission-missing", "progressed", "Health Connect permission missing"],
];
for (const [reason, outcome, expected] of reasonCases) {
  test(`checkpoint with ${reason} names the condition even after another type succeeds`, () => {
    const message = resultMessage(checkpoint(entry("completed"), entry(outcome, reason)));
    assert.ok(message.startsWith("Sync needs attention:"));
    assert.ok(message.includes(expected));
    assert.ok(!message.includes("Sync finished"));
    assert.ok(!message.includes("synthetic-private-record"));
    assert.equal(resultMessage(checkpoint(entry(outcome, reason))), message);
  });
}

test("not-permitted is actionable without a reason, as emitted by the coordinator", () => {
  assert.equal(resultMessage(checkpoint(entry("not-permitted"))),
    "Sync needs attention: Health Connect permission missing.");
});

test("blocked, failed and deferred outcomes never claim success without a reason", () => {
  for (const outcome of ["blocked", "failed", "deferred"] as const) {
    assert.ok(resultMessage(checkpoint(entry(outcome))).startsWith("Sync needs attention:"));
  }
});

test("multiple conditions remain visible and repeated conditions are deduplicated", () => {
  assert.equal(resultMessage(checkpoint(entry("not-permitted"), entry("blocked", "storage-pressure"), entry("not-permitted"))),
    "Sync needs attention: Health Connect permission missing; storage pressure; free device storage.");
});

test("skipped enrollment and failed runs stay distinct and never display raw failure text", () => {
  assert.equal(resultMessage({ outcome: "skipped" }), "Sync waits for a confirmed enrollment.");
  assert.equal(resultMessage({ outcome: "failed", failure: "synthetic-private-failure" }), "Sync could not finish.");
});

for (const outcome of ["progressed", "interrupted", "failed"] as const) {
  test(`${outcome}/read-failed is incomplete and will retry without requiring action`, () => {
    const message = resultMessage(checkpoint(entry("completed"), entry(outcome, "read-failed")));
    assert.equal(message, "Sync incomplete: Health Connect read failed; will retry.");
    assert.ok(!message.includes("synthetic-private-record"));
  });
}

test("read retry remains visible alongside conditions needing attention", () => {
  assert.equal(resultMessage(checkpoint(entry("progressed", "permission-missing"), entry("interrupted", "read-failed"))),
    "Sync needs attention: Health Connect permission missing. Sync incomplete: Health Connect read failed; will retry.");
});

test("blocking reasons need attention regardless of outcome", () => {
  const outcomes = ["not-due", "no-change", "progressed", "completed", "deferred", "blocked", "not-permitted", "failed", "interrupted"] as const;
  const reasons = ["projection-refusal", "record-too-large", "permission-missing", "delivery-blocked", "storage-pressure", "screen-lock-removed", "collection-disabled"] as const;
  for (const reason of reasons) {
    for (const outcome of outcomes) {
      const message = resultMessage(checkpoint(entry(outcome, reason)));
      assert.ok(message.startsWith("Sync needs attention:"), `${outcome}/${reason}`);
      assert.ok(!message.includes("synthetic-private-record"));
    }
  }
});
