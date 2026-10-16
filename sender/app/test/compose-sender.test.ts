import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { URL } from "node:url";
import { test } from "node:test";
import type { TestContext } from "node:test";
import { decodeMessage, encodeMessage, messageSha256 } from "@activity-tracker-portal/ingest-protocol";
import { composeSender, PROJECTION_IMPLEMENTATION_VERSION, readSyncConditions, STORAGE_PRESSURE_FREE_BYTES } from "../src/shell/compose-sender.ts";
import type { ComposeSenderOptions, ShellRunResult } from "../src/shell/compose-sender.ts";
import { startForegroundLifecycle } from "../src/shell/foreground-lifecycle.ts";
import { backgroundTaskBody } from "../src/background/background-task-body.ts";
import { createSenderEnrollment } from "../src/enrollment/manage-sender-enrollment.ts";
import type { EnrollmentStatus, SenderEnrollment } from "../src/enrollment/manage-sender-enrollment.ts";
import { createOutboxSender } from "../src/delivery/send-outbox-message.ts";
import { outboxAad, outboxMessages } from "../src/outbox/stage-outbox-batch.ts";
import type { SenderDatabase } from "../src/storage/database.ts";
import { openNodeDatabase } from "./support/node-database.ts";
import { NodeInstallationCipher } from "./support/node-installation-cipher.ts";
import { SyntheticClock, SyntheticSources } from "./support/synthetic-sync-sources.ts";

const INSTALLATION = "00000000-0000-4000-8000-000000000001";
const KEY = "synthetic-key";
const NOW = Date.UTC(2026, 9, 16);
const CLEAR = { collectionEnabled: true, secureLock: true, deliveryBlocked: false, storagePressure: false };
type Status = Awaited<ReturnType<SenderEnrollment["status"]>>;
function enrolled(overrides: Partial<EnrollmentStatus> = {}): EnrollmentStatus {
  return {
    installationId: INSTALLATION, keyAlias: KEY, portalUrl: "https://portal.synthetic.invalid",
    fingerprint: "synthetic-fingerprint", phase: "enrolled", confirmed: true, refusal: null, finalSequence: null,
    stagingContext: { installationId: INSTALLATION, keyAlias: KEY, generation: {
      generation: 1, maxRecordCount: 100, maxMessageBytes: 65_536, projectionSchemas: { steps: [1] },
    } },
    deliveryContext: { installationId: INSTALLATION, keyAlias: KEY, portalUrl: "https://portal.synthetic.invalid" },
    ...overrides,
  };
}

async function fixture(t: TestContext, initial: Status = enrolled()) {
  const directory = mkdtempSync(join(tmpdir(), "sender-shell-"));
  let database: SenderDatabase | undefined;
  t.after(async () => { await database?.close(); rmSync(directory, { recursive: true, force: true }); });
  const cipher = new NodeInstallationCipher({ deviceSecure: true });
  await cipher.create(KEY);
  const clock = new SyntheticClock(NOW);
  const sources = new SyntheticSources(clock);
  const state = {
    status: initial, versions: { steps: 1 } as Record<string, number>, versionReads: 0,
    bytes: STORAGE_PRESSURE_FREE_BYTES, registered: false, registerCalls: 0, platformReads: 0,
    restriction: "available" as "available" | "restricted", notifications: [] as string[], opened: 0,
  };
  const options: ComposeSenderOptions = {
    async openDatabase() { state.opened++; database = await openNodeDatabase(join(directory, "sender.db")); return database; },
    enrollment(handle) { assert.equal(handle, database); return { async status() { return state.status; } }; },
    cipher, sources,
    async projectionSchemaVersions() { state.versionReads++; return state.versions; },
    async availableStorageBytes() { return state.bytes; },
    platform: {
      async isRegistered() { state.platformReads++; return state.registered; },
      async register(name, minutes) {
        assert.equal(name, "activity-tracker-portal.polling"); assert.equal(minutes, 60);
        state.registerCalls++; state.registered = true;
      },
      async restriction() { return state.restriction; },
    },
    notifier: { async notify(condition) { state.notifications.push(condition); } },
    now: clock.now, monotonicNow: clock.monotonicNow, randomUUID,
  };
  return { state, cipher, clock, sources, options, database: () => database! };
}
function checkpoint(result: ShellRunResult) {
  assert.equal(result.outcome, "checkpointed");
  if (result.outcome !== "checkpointed") throw new Error("Expected checkpoint");
  return result;
}
async function runs(database: SenderDatabase) { return database.query("SELECT * FROM sync_runs ORDER BY run_sequence"); }

// The build identity must track the app release, independently of protocol versions.
test("projection implementation identity matches the Sender package version", () => {
  const manifest = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  assert.equal(PROJECTION_IMPLEMENTATION_VERSION, `activity-tracker-sender/${manifest.version}`);
});

test("opens and migrates once; start and foreground return ensure registration and manual triggers share the run sequence", async (t) => {
  const f = await fixture(t);
  const shell = await composeSender(f.options);
  assert.equal((await f.database().query("PRAGMA user_version"))[0].user_version, 8);
  assert.equal(checkpoint(await shell.foreground()).summary.trigger, "foreground");
  assert.equal(checkpoint(await shell.foreground()).runSequence, 2);
  assert.equal(f.state.registerCalls, 1);
  assert.equal(f.state.platformReads, 6); // ensure on both calls, plus both run-open observations
  assert.equal(checkpoint(await shell.manualSync()).summary.trigger, "manual");
  assert.equal(f.state.opened, 1);
  assert.equal(f.state.versionReads, 1);
  assert.deepEqual((await runs(f.database())).map((row) => row.trigger), ["foreground", "foreground", "manual"]);
});

test("managed types intersect exact native schema versions and sort; a new generation rebuilds the coordinator", async (t) => {
  const f = await fixture(t);
  f.state.versions = { weight: 1, steps: 1, "heart-rate": 1, "native-only": 1 };
  f.state.status = enrolled({ stagingContext: { ...enrolled().stagingContext!, generation: {
    generation: 1, maxRecordCount: 100, maxMessageBytes: 65_536,
    projectionSchemas: { weight: [2], steps: [1, 2], "heart-rate": [1], "portal-only": [1] },
  } } });
  const shell = await composeSender(f.options);
  const first = checkpoint(await shell.manualSync());
  assert.deepEqual(first.summary.recordTypes.map((type) => type.recordType), ["heart-rate", "steps"]);
  assert.deepEqual([...new Set(f.sources.calls.map((call) => call.recordType))].sort(), ["heart-rate", "steps"]);
  f.state.status = enrolled({ stagingContext: { ...enrolled().stagingContext!, generation: {
    generation: 2, maxRecordCount: 100, maxMessageBytes: 65_536, projectionSchemas: { weight: [1] },
  } } });
  const second = checkpoint(await shell.manualSync());
  assert.deepEqual(second.summary.recordTypes.map((type) => type.recordType), ["weight"]);
  assert.equal(f.state.versionReads, 2);
});

// The coordinator reads now() synchronously when accepting a trigger. While
// a source read is held, the active run makes no other clock reads.
function submissionClock(now: () => number) {
  let remaining = 0;
  let accepted: (() => void) | undefined;
  return {
    now() {
      if (remaining > 0 && --remaining === 0) accepted?.();
      return now();
    },
    async submit<T>(action: () => Promise<T>, clockReads = 1): Promise<{ result: Promise<T> }> {
      const arrived = new Promise<void>((resolve) => { accepted = resolve; remaining = clockReads; });
      const result = action();
      await arrived;
      return { result };
    },
  };
}

test("overlapping foreground, manual and background triggers share one strongest follow-up", { timeout: 5_000 }, async (t) => {
  const f = await fixture(t);
  const submissions = submissionClock(f.clock.now);
  const shell = await composeSender({ ...f.options, now: submissions.now });
  const activeRead = f.sources.hold("snapshot", "steps");
  const followUpRead = f.sources.hold("snapshot", "steps");
  f.sources.next("snapshot", "steps", { outcome: "not-permitted", failure: "synthetic-permission-gap" });
  try {
    const active = shell.manualSync();
    await activeRead.arrived;
    // Registration reads now() once before foreground's trigger is submitted.
    const foreground = await submissions.submit(() => shell.foreground(), 2);
    const manual = await submissions.submit(() => shell.manualSync());
    const background = await submissions.submit(() => backgroundTaskBody(shell.backgroundPollingProvider)());
    const settled: string[] = [];
    void foreground.result.then(() => settled.push("foreground"));
    void manual.result.then(() => settled.push("manual"));
    void background.result.then(() => settled.push("background"));
    assert.equal((await runs(f.database())).length, 1);
    activeRead.release();
    assert.equal(checkpoint(await active).runSequence, 1);
    await followUpRead.arrived;
    assert.deepEqual(settled, []); // Background also awaits the follow-up, not the active result.
    // Unbudgeted foreground/manual terms must remove the background budget.
    f.sources.callCostMs = 480_000;
    followUpRead.release();
    const [foregroundResult, manualResult, backgroundResult] = await Promise.all([
      foreground.result, manual.result, background.result,
    ]);
    const second = checkpoint(foregroundResult);
    assert.strictEqual(manualResult, foregroundResult);
    assert.equal(second.runSequence, 2);
    assert.equal(second.summary.trigger, "manual");
    assert.ok(second.summary.recordTypes.every((type) => type.reason !== "time-budget-reached"));
    assert.equal(backgroundResult, 1);
    assert.deepEqual((await runs(f.database())).map((row) => row.run_sequence), [1, 2]);
    const checkpoints = await f.database().query("SELECT run_sequence FROM run_checkpoints ORDER BY run_sequence");
    assert.deepEqual(checkpoints.map((row) => row.run_sequence), [1, 2]);
    const staged = [];
    for (const message of await outboxMessages(f.database())) {
      const opened = await f.cipher.open(KEY, message.sealed, outboxAad(INSTALLATION, message.sequence));
      assert.equal(opened.status, "ok");
      if (opened.status === "ok") staged.push(decodeMessage(opened.value));
    }
    assert.equal(staged.filter((message) => message.kind === "sync-checkpoint").length, 2);
    assert.equal(f.state.versionReads, 1);
    assert.equal(f.sources.maxInFlight, 1);
  } finally {
    activeRead.release(); followUpRead.release();
  }
});

test("generation replacement waits for the old run and routes waiting triggers to the new coordinator", { timeout: 5_000 }, async (t) => {
  const f = await fixture(t);
  f.state.versions = { steps: 1, weight: 1 };
  let oldSettled = false;
  let replacementPrepared!: () => void;
  const replacement = new Promise<void>((resolve) => { replacementPrepared = resolve; });
  let generationRead!: () => void;
  const observed = new Promise<void>((resolve) => { generationRead = resolve; });
  const shell = await composeSender({ ...f.options,
    enrollment() { return { async status() {
      const current = f.state.status;
      if (current && !("outcome" in current) && current.stagingContext?.generation.generation === 2) generationRead();
      return current;
    } }; },
    async projectionSchemaVersions() {
      if (++f.state.versionReads === 2) {
        assert.equal(oldSettled, true, "replacement must be constructed after the old trigger settles");
        assert.equal(f.sources.inFlight, 0);
        const previous = await runs(f.database());
        assert.equal(previous.length, 1);
        assert.equal((await f.database().query("SELECT * FROM run_checkpoints WHERE run_sequence = 1")).length, 1);
        replacementPrepared();
      }
      return f.state.versions;
    },
  });
  const oldRead = f.sources.hold("snapshot", "steps");
  const newRead = f.sources.hold("snapshot", "weight");
  try {
    const old = shell.manualSync().then((result) => { oldSettled = true; return result; });
    await oldRead.arrived;
    f.state.status = enrolled({ stagingContext: { ...enrolled().stagingContext!, generation: {
      generation: 2, maxRecordCount: 100, maxMessageBytes: 65_536, projectionSchemas: { weight: [1] },
    } } });
    const next = shell.manualSync();
    await observed;
    const background = backgroundTaskBody(shell.backgroundPollingProvider)();
    await new Promise<void>((resolve) => setImmediate(resolve));
    assert.equal(f.state.versionReads, 1);
    assert.equal(oldSettled, false);
    assert.equal((await runs(f.database())).length, 1);
    oldRead.release();
    assert.deepEqual(checkpoint(await old).summary.recordTypes.map((type) => type.recordType), ["steps"]);
    await replacement;
    await newRead.arrived;
    newRead.release();
    const result = checkpoint(await next);
    assert.equal(result.runSequence, 2);
    assert.deepEqual(result.summary.recordTypes.map((type) => type.recordType), ["weight"]);
    assert.equal(await background, 1);
    assert.equal(f.state.versionReads, 2);
    assert.equal(f.sources.maxInFlight, 1);
    assert.equal((await f.database().query(`SELECT * FROM sync_runs AS r WHERE NOT EXISTS
      (SELECT 1 FROM run_checkpoints AS c WHERE c.installation_id = r.installation_id AND c.run_sequence = r.run_sequence)`)).length, 0);
  } finally {
    oldRead.release(); newRead.release();
  }
});

for (const [name, status] of [
  ["no enrollment", null],
  ["confirming", enrolled({ phase: "confirming", confirmed: false, stagingContext: null })],
  ["refused", enrolled({ phase: "refused", confirmed: false, refusal: "enrollment-refused", stagingContext: null })],
  ["unconfirmed", enrolled({ confirmed: false })],
  ["draining", enrolled({ phase: "draining" })],
] as const) {
  test(`${name}: foreground/manual skip and the background provider succeeds without a run`, async (t) => {
    const f = await fixture(t, status);
    const shell = await composeSender(f.options);
    assert.deepEqual(await shell.foreground(), { outcome: "skipped" });
    assert.deepEqual(await shell.manualSync(), { outcome: "skipped" });
    assert.equal(await backgroundTaskBody(shell.backgroundPollingProvider)(), 1);
    assert.deepEqual(await runs(f.database()), []);
    assert.deepEqual(await outboxMessages(f.database()), []);
    assert.deepEqual(f.sources.calls, []);
    assert.equal(f.state.versionReads, 0);
    assert.equal(f.state.registerCalls, status === null ? 0 : 1);
  });
}

test("background task uses the real polling adapter and its eight-minute budget", async (t) => {
  const f = await fixture(t);
  f.sources.callCostMs = 480_000;
  const shell = await composeSender(f.options);
  assert.equal(await backgroundTaskBody(shell.backgroundPollingProvider)(), 1);
  const [run] = await runs(f.database());
  assert.equal(run.trigger, "background");
  assert.ok(f.sources.calls.length <= 2);
  const messages = await outboxMessages(f.database());
  const last = messages.at(-1)!;
  const opened = await f.cipher.open(KEY, last.sealed, outboxAad(INSTALLATION, last.sequence));
  assert.equal(opened.status, "ok");
  if (opened.status === "ok") {
    const checkpoint = decodeMessage(opened.value);
    assert.equal(checkpoint.kind, "sync-checkpoint");
    assert.match(new TextDecoder().decode(opened.value), /time-budget-reached/);
  }
});

test("enrollment read failure fails every trigger without a run, including the headless provider", async (t) => {
  const f = await fixture(t, { outcome: "storage-failed" });
  const shell = await composeSender(f.options);
  assert.deepEqual(await shell.foreground(), { outcome: "failed", failure: "storage-failed" });
  assert.deepEqual(await shell.manualSync(), { outcome: "failed", failure: "storage-failed" });
  assert.equal(await backgroundTaskBody(shell.backgroundPollingProvider)(), 2);
  assert.deepEqual(await runs(f.database()), []);
});

test("missing staging context never opens a Polling Run", async (t) => {
  const f = await fixture(t, enrolled({ stagingContext: null }));
  const shell = await composeSender(f.options);
  await assert.rejects(shell.manualSync(), /staging context/);
  assert.equal(await backgroundTaskBody(shell.backgroundPollingProvider)(), 2);
  assert.deepEqual(await runs(f.database()), []);
});

for (const [name, override, expected] of [
  ["secure lock absent", { cipher: { async isDeviceSecure() { return false; } } }, { secureLock: false }],
  ["secure lock throws", { cipher: { async isDeviceSecure(): Promise<boolean> { throw new Error("synthetic lock failure"); } } }, { secureLock: false }],
  ["delivery storage-failed", { async headBlock() { return { outcome: "storage-failed" } as const; } }, { deliveryBlocked: true }],
  ["delivery throws", { async headBlock(): Promise<never> { throw new Error("synthetic read failure"); } }, { deliveryBlocked: true }],
  ["below reserve", { async availableStorageBytes() { return STORAGE_PRESSURE_FREE_BYTES - 1; } }, { storagePressure: true }],
  ["above reserve", { async availableStorageBytes() { return STORAGE_PRESSURE_FREE_BYTES + 1; } }, {}],
  ["free space throws", { async availableStorageBytes(): Promise<number> { throw new Error("synthetic storage failure"); } }, { storagePressure: true }],
  ["free space malformed", { async availableStorageBytes() { return NaN; } }, { storagePressure: true }],
  ["collection unconfirmed", { async status() { return enrolled({ confirmed: false }); } }, { collectionEnabled: false }],
  ["enrollment throws", { async status(): Promise<never> { throw new Error("synthetic read failure"); } }, { collectionEnabled: false }],
] as const) {
  test(`conditions fail closed: ${name}`, async () => {
    assert.deepEqual(await readSyncConditions(Object.assign({
      async status() { return enrolled(); }, cipher: new NodeInstallationCipher({ deviceSecure: true }),
      async headBlock() { return null; }, async availableStorageBytes() { return STORAGE_PRESSURE_FREE_BYTES; },
    }, override)), { ...CLEAR, ...expected });
  });
}

test("all clear conditions at the exact 512 MiB boundary", async () => {
  assert.deepEqual(await readSyncConditions({
    async status() { return enrolled(); }, cipher: new NodeInstallationCipher({ deviceSecure: true }),
    async headBlock() { return null; }, async availableStorageBytes() { return STORAGE_PRESSURE_FREE_BYTES; },
  }), CLEAR);
});

for (const condition of ["secureLock", "storagePressure", "deliveryBlocked"] as const) {
  test(`the composed coordinator stops source access for ${condition}`, async (t) => {
    const f = await fixture(t);
    if (condition === "secureLock") f.cipher.isDeviceSecure = async () => { throw new Error("synthetic lock failure"); };
    const shell = await composeSender({ ...f.options, ...(condition === "storagePressure" ? {
      async availableStorageBytes(): Promise<number> { throw new Error("synthetic storage failure"); },
    } : {}) });
    if (condition === "deliveryBlocked") {
      await f.database().execute(`INSERT INTO outbox_head_block VALUES (?, 1, 'portal', 'installation-not-active', ?, 0, 0, 1, 1)`,
        [INSTALLATION, "a".repeat(64)]);
    }
    const result = checkpoint(await shell.manualSync());
    const reason = { secureLock: "screen-lock-removed", storagePressure: "storage-pressure", deliveryBlocked: "delivery-blocked" }[condition];
    assert.equal(result.summary.recordTypes[0].reason, reason);
    assert.deepEqual(f.sources.calls, []);
  });
}

test("a real headBlock storage read failure blocks sources and reports delivery-blocked", async (t) => {
  const f = await fixture(t);
  const opener = f.options.openDatabase;
  const shell = await composeSender({ ...f.options, async openDatabase() {
    const database = await opener();
    return { ...database, async query(sql, args) {
      if (sql.includes("outbox_head_block")) throw new Error("synthetic read failure");
      return database.query(sql, args);
    } };
  }, enrollment() { return { async status() { return f.state.status; } }; } });
  assert.equal(checkpoint(await shell.manualSync()).summary.recordTypes[0].reason, "delivery-blocked");
  assert.deepEqual(f.sources.calls, []);
});

test("AppState start and foreground transitions trigger once each, and cleanup removes the listener", async () => {
  let listener: (state: string) => void = () => {};
  let removed = false;
  let calls = 0;
  const stop = startForegroundLifecycle({ currentState: "active", addEventListener(event, callback) {
    assert.equal(event, "change"); listener = callback; return { remove() { removed = true; } };
  } }, async () => { calls++; }, () => assert.fail("Unexpected foreground error"));
  await Promise.resolve();
  assert.equal(calls, 1);
  listener("active"); listener("inactive"); listener("background"); listener("active"); listener("active");
  await Promise.resolve();
  assert.equal(calls, 2);
  stop(); assert.equal(removed, true);
});

test("AppState errors are reported and later returns still work", async () => {
  let listener: (state: string) => void = () => {};
  let errors = 0;
  let calls = 0;
  const stop = startForegroundLifecycle({ currentState: "active", addEventListener(_event, callback) {
    listener = callback; return { remove() {} };
  } }, async () => { calls++; if (calls === 1) throw new Error("synthetic start failure"); }, () => { errors++; });
  await new Promise<void>((resolve) => setImmediate(resolve));
  listener("background"); listener("active");
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(errors, 1); assert.equal(calls, 2); stop();
});

test("a frozen newer database closes and never constructs enrollment", async (t) => {
  const f = await fixture(t);
  const database = await f.options.openDatabase();
  await database.execute("PRAGMA user_version=999");
  let closed = false;
  await assert.rejects(composeSender({ ...f.options, async openDatabase() { return {
    ...database, async close() { closed = true; },
  }; }, enrollment() { assert.fail("Frozen storage cannot supply enrollment"); } }), /frozen/);
  assert.equal(closed, true);
});

test("published enrollment confirmation gates collection until its acknowledgement receipt, then drain stops all runs", async (t) => {
  const f = await fixture(t, null);
  let enrollment!: SenderEnrollment;
  const shell = await composeSender({ ...f.options, enrollment(database) {
    enrollment = createSenderEnrollment({ database, cipher: f.cipher, now: f.clock.now, randomUUID: () => INSTALLATION,
      transport: { async send() { return { outcome: "response", body: encodeMessage({
        wire_version: 1, kind: "capability-advertisement", generation: 1, wire_versions: [1],
        projection_schemas: [{ record_type: "steps", versions: [1] }], max_record_count: 100, max_message_bytes: 65_536,
      }) }; } },
    });
    return enrollment;
  } });
  assert.equal((await enrollment.enroll({ kind: "typed", portalUrl: "https://portal.synthetic.invalid", enrollmentCode: "SYNTHETICCODE234" })).outcome, "enrolled");
  assert.deepEqual(await shell.foreground(), { outcome: "skipped" });
  assert.equal(await backgroundTaskBody(shell.backgroundPollingProvider)(), 1);
  const status = await enrollment.status();
  assert.ok(status && !("outcome" in status));
  const delivery = createOutboxSender({ database: f.database(), cipher: f.cipher, context: status.deliveryContext, now: f.clock.now,
    notifier: { async notify() { assert.fail("No delivery block expected"); } },
    transport: { async send(request) {
      const message = decodeMessage(request.body);
      return { outcome: "response", body: encodeMessage({ wire_version: 1, kind: "delivery-receipt",
        sender_installation: message.sender_installation, sequence: message.sequence, message_sha256: messageSha256(request.body),
      }) };
    } },
  });
  assert.equal((await delivery.sendNext()).outcome, "delivered");
  checkpoint(await shell.manualSync());
  const beforeDrain = await runs(f.database());
  const beforeMessages = await outboxMessages(f.database());
  assert.equal((await enrollment.beginDrain()).outcome, "draining");
  assert.deepEqual(await shell.foreground(), { outcome: "skipped" });
  assert.deepEqual(await shell.manualSync(), { outcome: "skipped" });
  assert.equal(await backgroundTaskBody(shell.backgroundPollingProvider)(), 1);
  assert.deepEqual(await runs(f.database()), beforeDrain);
  assert.deepEqual(await outboxMessages(f.database()), beforeMessages);
});
