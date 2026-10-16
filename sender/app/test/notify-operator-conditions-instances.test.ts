// Unbound regressions for notify-operator-conditions@1's shared-database claims.
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { TestContext } from "node:test";
import { deliveryMigration } from "../src/delivery/send-outbox-message.ts";
import { enrollmentMigration } from "../src/enrollment/manage-sender-enrollment.ts";
import { createOperatorConditions, operatorConditionsMigration } from "../src/notifications/notify-operator-conditions.ts";
import type { ConditionNotifier, OperatorCondition } from "../src/notifications/notify-operator-conditions.ts";
import { outboxMigration } from "../src/outbox/stage-outbox-batch.ts";
import { checkpointMigration } from "../src/outbox/stage-sync-checkpoint.ts";
import type { OutcomeReason, RunSummary, TypeOutcome } from "../src/outbox/stage-sync-checkpoint.ts";
import { completionMigration } from "../src/outbox/stage-sync-completion.ts";
import { schedulingMigration } from "../src/scheduling/schedule-background-polling.ts";
import { snapshotAttemptsMigration } from "../src/snapshot/begin-initial-snapshot.ts";
import type { SenderDatabase } from "../src/storage/database.ts";
import { migrate } from "../src/storage/migrations.ts";
import { syncMigration } from "../src/sync/coordinate-sender-sync.ts";
import { openNodeDatabase } from "./support/node-database.ts";

const NOW = Date.UTC(2026, 9, 16, 9);
const MIGRATIONS = [
  snapshotAttemptsMigration, outboxMigration, completionMigration, checkpointMigration, syncMigration, schedulingMigration,
  deliveryMigration, enrollmentMigration, operatorConditionsMigration,
];

/** Bound every wait, including notifier gates, so a regression fails instead of hanging. */
async function bounded<T>(promise: Promise<T>, label: string, ms = 1_000): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([promise, new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} exceeded ${ms}ms`)), ms);
    })]);
  } finally { clearTimeout(timer); }
}

function gate() {
  let enter!: () => void;
  let release!: () => void;
  const entered = new Promise<void>((resolve) => { enter = resolve; });
  const released = new Promise<void>((resolve) => { release = resolve; });
  return { entered, release, async hold() { enter(); await bounded(released, "notifier release", 2_000); } };
}

function summary(...entries: (readonly [TypeOutcome, OutcomeReason?])[]): RunSummary {
  return {
    runSequence: 1, trigger: "background", startedAtMs: NOW - 60_000, endedAtMs: NOW - 30_000,
    backgroundScheduling: "available",
    recordTypes: entries.map(([outcome, reason]) => ({
      recordType: "steps", outcome, ...(reason === undefined ? {} : { reason }), sourceDataStaged: false,
    })),
  };
}

const PERMISSION = { enrollment: null, summary: summary(["not-permitted"]) } as const;
const KEEP = { enrollment: null } as const;
const record = (condition: OperatorCondition, notified: boolean) =>
  ({ condition, firstSeenMs: NOW, lastSeenMs: NOW, notified });

async function fixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), "sender-condition-instances-synthetic-"));
  // The same file-backed 3ug substitute and explicit migration list as the frozen suite.
  const database = await openNodeDatabase(join(directory, "synthetic.sqlite"));
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 9 });
  return {
    database,
    make: (notifier: ConditionNotifier) => createOperatorConditions({ database, notifier, now: () => NOW }),
  };
}

test("two instances notify a concurrent shared onset exactly once", { timeout: 5_000 }, async (t) => {
  const f = await fixture(t);
  const delayed = gate();
  const calls: OperatorCondition[] = [];
  const a = f.make({ notify: async (condition) => { calls.push(condition); await delayed.hold(); } });
  const b = f.make({ notify: async (condition) => { calls.push(condition); } });
  // Both observations start before either has recorded the new onset.
  const first = a.observe(PERMISSION);
  const second = b.observe(PERMISSION);
  try {
    await bounded(delayed.entered, "first notification");
    assert.deepEqual(await bounded(second, "second observation"), { outcome: "observed", conditions: ["permission-missing"] });
    assert.deepEqual(calls, ["permission-missing"]);
    assert.deepEqual(await bounded(b.conditions(), "pending conditions"), [record("permission-missing", false)]);
    delayed.release();
    assert.deepEqual(await bounded(first, "first observation"), { outcome: "observed", conditions: ["permission-missing"] });
    assert.deepEqual(await bounded(b.conditions(), "notified conditions"), [record("permission-missing", true)]);
    assert.equal((await bounded(b.observe(KEEP), "later observation")).outcome, "observed");
    assert.deepEqual(calls, ["permission-missing"]);
  } finally { delayed.release(); await bounded(Promise.all([first, second]), "observation cleanup"); }
});

test("another instance retries after the claiming notifier fails", { timeout: 5_000 }, async (t) => {
  const f = await fixture(t);
  const delayed = gate();
  const attempts: string[] = [];
  const a = f.make({ notify: async () => {
    attempts.push("a"); await delayed.hold(); throw new Error("Synthetic notification failure");
  } });
  const b = f.make({ notify: async () => { attempts.push("b"); } });
  const pending = a.observe(PERMISSION);
  try {
    await bounded(delayed.entered, "claiming notification");
    assert.equal((await bounded(b.observe(KEEP), "observation during claim")).outcome, "observed");
    assert.deepEqual(attempts, ["a"]);
    assert.deepEqual(await bounded(b.conditions(), "unnotified conditions"), [record("permission-missing", false)]);
    delayed.release();
    assert.equal((await bounded(pending, "failed notifier observation")).outcome, "observed");
    assert.equal((await bounded(b.observe(KEEP), "retry observation")).outcome, "observed");
    assert.deepEqual(attempts, ["a", "b"]);
    assert.deepEqual(await bounded(b.conditions(), "retried conditions"), [record("permission-missing", true)]);
  } finally { delayed.release(); await bounded(pending, "observation cleanup"); }
});

test("a pending notifier does not block another instance's observations or conditions", { timeout: 5_000 }, async (t) => {
  const f = await fixture(t);
  const delayed = gate();
  const calls: OperatorCondition[] = [];
  const a = f.make({ notify: async () => { await delayed.hold(); } });
  const b = f.make({ notify: async (condition) => { calls.push(condition); } });
  const pending = a.observe(PERMISSION);
  try {
    await bounded(delayed.entered, "pending notification");
    assert.deepEqual(await bounded(b.conditions(), "independent read"), [record("permission-missing", false)]);
    const different = { enrollment: null, summary: summary(["not-permitted"], ["deferred", "storage-pressure"]) };
    assert.deepEqual(await bounded(b.observe(different), "different condition observation"),
      { outcome: "observed", conditions: ["permission-missing", "storage-pressure"] });
    assert.deepEqual(calls, ["storage-pressure"]);
    assert.deepEqual(await bounded(b.conditions(), "independent updated read"),
      [record("permission-missing", false), record("storage-pressure", true)]);
    delayed.release();
    assert.equal((await bounded(pending, "original observation")).outcome, "observed");
    assert.deepEqual(await bounded(b.conditions(), "completed conditions"),
      [record("permission-missing", true), record("storage-pressure", true)]);
  } finally { delayed.release(); await bounded(pending, "observation cleanup"); }
});

test("a success recording storage failure releases the claim for another instance", { timeout: 5_000 }, async (t) => {
  const f = await fixture(t);
  const execute = f.database.execute.bind(f.database);
  let fail = true;
  t.mock.method(f.database, "execute", async (...args: Parameters<SenderDatabase["execute"]>) => {
    if (fail && /^UPDATE operator_conditions SET notified = 1\b/.test(args[0])) {
      fail = false;
      throw new Error("Synthetic success recording failure");
    }
    return execute(...args);
  });
  const successes: string[] = [];
  const a = f.make({ notify: async () => { successes.push("a"); } });
  const b = f.make({ notify: async () => { successes.push("b"); } });
  assert.deepEqual(await bounded(a.observe(PERMISSION), "failed recording observation"), { outcome: "storage-failed" });
  assert.equal(fail, false, "the failure was injected into the success recording");
  assert.deepEqual(await bounded(b.conditions(), "unrecorded success"), [record("permission-missing", false)]);
  assert.equal((await bounded(b.observe(KEEP), "retry after storage failure")).outcome, "observed");
  assert.deepEqual(successes, ["a", "b"]);
  assert.deepEqual(await bounded(b.conditions(), "recorded retry"), [record("permission-missing", true)]);
});
