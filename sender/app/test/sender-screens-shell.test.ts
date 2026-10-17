import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { URL } from "node:url";
import type { TestContext } from "node:test";
import { composeSender, STORAGE_PRESSURE_FREE_BYTES } from "../src/shell/compose-sender.ts";
import { createFailureObservation } from "../src/shell/failure-observation.ts";
import type { EnrollmentStatus, SenderEnrollment } from "../src/enrollment/manage-sender-enrollment.ts";
import type { SenderDatabase } from "../src/storage/database.ts";
import { openNodeDatabase } from "./support/node-database.ts";
import { NodeInstallationCipher } from "./support/node-installation-cipher.ts";
import { SyntheticClock, SyntheticSources } from "./support/synthetic-sync-sources.ts";

function gate() {
  let release!: () => void;
  return { promise: new Promise<void>((resolve) => { release = resolve; }), release: () => release() };
}
async function fixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), "screens-shell-"));
  const actual = await openNodeDatabase(join(directory, "sender.db"));
  t.after(async () => { await actual.close(); rmSync(directory, { recursive: true, force: true }); });
  const state = { status: null as Awaited<ReturnType<SenderEnrollment["status"]>>, reads: 0, failDelivery: false };
  const database: SenderDatabase = {
    ...actual,
    async query(sql, parameters) {
      if (state.failDelivery && sql.includes("SELECT sequence, kind, sealed")) throw new Error("private health data");
      return actual.query(sql, parameters);
    },
  };
  const result = { outcome: "refused", failure: "not-enrolled" } as const;
  const enrollment: SenderEnrollment = {
    async status() { state.reads++; return state.status; },
    async enroll() { return result; }, async confirm() { return result; },
    async refreshCapabilities() { return { outcome: "current" }; },
    async beginDrain() { return result; }, async unlink() { return result; }, async reset() { return result; },
  };
  const clock = new SyntheticClock(1_792_137_600_000);
  const shell = await composeSender({
    openDatabase: async () => database, enrollment: () => enrollment,
    fetch: async () => { throw new Error("private response"); },
    cipher: new NodeInstallationCipher({ deviceSecure: true }), sources: new SyntheticSources(clock),
    projectionSchemaVersions: async () => ({}), availableStorageBytes: async () => STORAGE_PRESSURE_FREE_BYTES,
    conditionNotifier: { async notify() {} }, deliveryNotifier: { async notify() {} },
    platform: { async isRegistered() { return true; }, async register() {}, async restriction() { return "available"; } },
    notifier: { async notify() {} }, now: clock.now, monotonicNow: clock.monotonicNow, randomUUID,
  });
  return { shell, state, enrollment, result, clock };
}

test("every screen enrollment action preserves the exact published result and observes lifecycle afterward", async (t) => {
  const f = await fixture(t);
  const offer = { kind: "typed", portalUrl: "https://portal.invalid", enrollmentCode: "secret" } as const;
  const acknowledgement = { acknowledgePossibleLoss: true } as const;
  f.enrollment.enroll = async (received) => { assert.strictEqual(received, offer); return f.result; };
  f.enrollment.reset = async (received) => { assert.strictEqual(received, acknowledgement); return f.result; };
  for (const action of [() => f.shell.enroll(offer), () => f.shell.confirm(), () => f.shell.beginDrain(),
    () => f.shell.unlink(), () => f.shell.reset(acknowledgement)]) {
    const reads = f.state.reads;
    assert.strictEqual(await action(), f.result);
    assert.ok(f.state.reads >= reads + 2, "fresh observation reads after action");
  }
  f.state.status = { outcome: "storage-failed" };
  const reads = f.state.reads;
  assert.strictEqual(await f.shell.status(), f.state.status);
  assert.ok(f.state.reads > reads + 1);
});

test("reset and unlink retain the serialized destruction lane", { timeout: 5000 }, async (t) => {
  const f = await fixture(t);
  const held = gate(); const entered = gate(); const calls: string[] = [];
  f.enrollment.reset = async () => { calls.push("reset"); entered.release(); await held.promise; return f.result; };
  f.enrollment.unlink = async () => { calls.push("unlink"); return f.result; };
  const reset = f.shell.reset({ acknowledgePossibleLoss: true });
  await entered.promise;
  const unlink = f.shell.unlink();
  try {
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(calls, ["reset"]);
  } finally { held.release(); }
  assert.strictEqual(await reset, f.result);
  assert.strictEqual(await unlink, f.result);
  assert.deepEqual(calls, ["reset", "unlink"]);
});

test("shell records thrown enrollment failures without retaining error text and still observes", async (t) => {
  const f = await fixture(t);
  const error = new Error("https://secret.invalid?code=private health-record-value");
  f.enrollment.confirm = async () => { throw error; };
  const reads = f.state.reads;
  await assert.rejects(f.shell.confirm(), (actual) => actual === error);
  assert.ok(f.state.reads > reads);
  assert.deepEqual(f.shell.latestFailure(), { kind: "enrollment", failure: "operation-failed", timeMs: f.clock.now() });
});

test("delivery storage failure is visible even when Sync is skipped", async (t) => {
  const f = await fixture(t);
  f.state.status = {
    installationId: randomUUID(), keyAlias: "missing", portalUrl: "https://secret.invalid?code=private",
    fingerprint: "fingerprint", phase: "draining", refusal: null, confirmed: true,
    stagingContext: null, finalSequence: null,
    deliveryContext: { installationId: randomUUID(), keyAlias: "missing", portalUrl: "https://secret.invalid" },
  } satisfies EnrollmentStatus;
  f.state.failDelivery = true;
  const result = await f.shell.manualSync();
  assert.equal(result.outcome, "skipped");
  assert.strictEqual(f.shell.latestSyncResult(), result);
  assert.deepEqual(f.shell.latestFailure(), { kind: "delivery", failure: "storage-failed", timeMs: f.clock.now() });
});

test("failure observation uses only exact allowlisted fields, replaces failures, and never clears on success", () => {
  let now = 1;
  const observation = createFailureObservation(() => now++);
  assert.equal(observation.latest(), null);
  observation.record("delivery", { outcome: "blocked", failure: "private health-record-value https://secret", sequence: 42, body: [1, 2] });
  assert.deepEqual(observation.latest(), { kind: "delivery", failure: "portal-blocked", timeMs: 1 });
  assert.ok(Object.isFrozen(observation.latest()));
  observation.record("delivery", { outcome: "delivered", sequence: 42 });
  assert.equal(observation.latest()?.timeMs, 1);
  observation.record("enrollment", { outcome: "refused", failure: "offer-invalid", portalUrl: "secret" });
  assert.deepEqual(observation.latest(), { kind: "enrollment", failure: "offer-invalid", timeMs: 2 });
  assert.equal(createFailureObservation(() => 1).latest(), null, "no persistence across compositions");
});

test("screens and their controller have no direct enrollment or native destruction access", () => {
  for (const path of ["../App.tsx", "../src/screens/use-sender-screen.ts", "../src/screens/sender-screen-model.ts"]) {
    const source = readFileSync(new URL(path, import.meta.url), "utf8");
    assert.doesNotMatch(source, /createSenderEnrollment|\.enrollment\b|nativeInstallationCipher/);
    assert.doesNotMatch(source, /camera|qr.scann/i);
  }
  const controller = readFileSync(new URL("../src/screens/use-sender-screen.ts", import.meta.url), "utf8");
  for (const action of ["status", "enroll", "confirm", "beginDrain", "unlink", "reset"]) {
    assert.match(controller, new RegExp(`shell\\.${action}\\(`));
  }
});

test("screen actions refresh lifecycle conditions and the installation identity", async (t) => {
  const f = await fixture(t);
  const current: EnrollmentStatus = {
    installationId: randomUUID(), keyAlias: "missing", portalUrl: "https://portal.invalid",
    fingerprint: "compare", phase: "confirming", refusal: null, confirmed: false,
    stagingContext: null, finalSequence: null,
    deliveryContext: { installationId: randomUUID(), keyAlias: "missing", portalUrl: "https://portal.invalid" },
  };
  const enrolled = { outcome: "retryable", failure: "transport-failed" } as const;
  f.enrollment.enroll = async () => { f.state.status = current; return enrolled; };
  assert.strictEqual(await f.shell.enroll({ kind: "typed", portalUrl: "https://portal.invalid", enrollmentCode: "code" }), enrolled);
  assert.strictEqual(await f.shell.status(), current);
  const refused = { outcome: "portal-refused", failure: "enrollment-refused" } as const;
  f.enrollment.confirm = async () => { f.state.status = { ...current, phase: "refused", refusal: "enrollment-refused" }; return refused; };
  assert.strictEqual(await f.shell.confirm(), refused);
  let conditions = await f.shell.conditions();
  assert.ok(Array.isArray(conditions) && conditions.some((row) => row.condition === "enrollment-refused"));
  const drained = { outcome: "draining" } as const;
  f.enrollment.beginDrain = async () => {
    f.state.status = { ...current, phase: "draining", refusal: null, confirmed: true, finalSequence: 9 };
    return drained;
  };
  assert.strictEqual(await f.shell.beginDrain(), drained);
  conditions = await f.shell.conditions();
  assert.ok(Array.isArray(conditions) && conditions.some((row) => row.condition === "drain-complete"));
  assert.ok(Array.isArray(conditions) && !conditions.some((row) => row.condition === "enrollment-refused"));
  const destroyed = { outcome: "destroyed" } as const;
  f.enrollment.unlink = async () => { f.state.status = null; return destroyed; };
  assert.strictEqual(await f.shell.unlink(), destroyed);
  assert.deepEqual(await f.shell.conditions(), []);
});
