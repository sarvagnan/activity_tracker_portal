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

test("sync finished requires every type to have a non-failure outcome", () => {
  const successes = ["no-change", "progressed", "completed", "not-due"] as const;
  for (const outcome of successes) assert.equal(resultMessage(checkpoint(entry(outcome))), "Sync finished.");
  assert.equal(resultMessage(checkpoint(...successes.map((outcome) => entry(outcome)))), "Sync finished.");
  // A successful continuity-recovery step can report its reason without failing the run.
  assert.equal(resultMessage(checkpoint(entry("progressed", "changes-token-expired"))), "Sync finished.");
});

const reasonCases: readonly [OutcomeReason, TypeOutcome, string][] = [
  ["collection-disabled", "deferred", "collection disabled"],
  ["projection-refusal", "blocked", "projection blocked"],
  ["record-too-large", "blocked", "record exceeds the delivery limit"],
  ["permission-missing", "blocked", "Health Connect permission missing"],
  ["delivery-blocked", "blocked", "delivery blocked"],
  ["storage-pressure", "blocked", "storage pressure"],
  ["screen-lock-removed", "blocked", "secure screen lock required"],
  ["changes-token-expired", "interrupted", "source continuity lost"],
  ["time-budget-reached", "deferred", "time budget reached"],
  ["read-failed", "failed", "Health Connect read failed"],
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

test("blocked, failed, deferred and interrupted outcomes never claim success without a reason", () => {
  for (const outcome of ["blocked", "failed", "deferred", "interrupted"] as const) {
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
