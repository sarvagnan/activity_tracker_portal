import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { URL } from "node:url";
import { test as nodeTest } from "node:test";
import type { TestContext, TestFn, TestOptions } from "node:test";
import { decodeMessage, encodeMessage, messageSha256 } from "@activity-tracker-portal/ingest-protocol";
import { composeSender, MAX_DELIVERY_ATTEMPTS, DELIVERY_FINISH_RESERVE_MS, PROJECTION_IMPLEMENTATION_VERSION, readSyncConditions, STORAGE_PRESSURE_FREE_BYTES } from "../src/shell/compose-sender.ts";
import type { ComposeSenderOptions, ShellRunResult } from "../src/shell/compose-sender.ts";
import { startForegroundLifecycle } from "../src/shell/foreground-lifecycle.ts";
import { backgroundTaskBody } from "../src/background/background-task-body.ts";
import { createSenderEnrollment } from "../src/enrollment/manage-sender-enrollment.ts";
import type { EnrollmentStatus, SenderEnrollment } from "../src/enrollment/manage-sender-enrollment.ts";
import type { HttpFetchInit } from "../src/transport/http-transport.ts";
import { DELIVERY_TIMEOUT_MS } from "../src/delivery/send-outbox-message.ts";
import { BACKGROUND_BUDGET_MS } from "../src/scheduling/schedule-background-polling.ts";
import { createOutboxSender } from "../src/delivery/send-outbox-message.ts";
import { appendOutboxMessages, lastOutboxSequence, outboxAad, outboxMessages } from "../src/outbox/stage-outbox-batch.ts";
import type { SenderDatabase } from "../src/storage/database.ts";
import { openNodeDatabase } from "./support/node-database.ts";
import { NodeInstallationCipher } from "./support/node-installation-cipher.ts";
import { SyntheticClock, SyntheticSources } from "./support/synthetic-sync-sources.ts";

function test(name: string, options: TestFn | TestOptions, body?: TestFn): void {
  if (typeof options === "function") void nodeTest(name, { timeout: 5_000 }, options);
  else void nodeTest(name, { timeout: 5_000, ...options }, body!);
}

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
    enrollment(handle) { assert.equal(handle, database); return {
      async status() { return state.status; },
      async confirm() { return { outcome: "retryable", failure: "transport-failed" }; },
      async refreshCapabilities() { return { outcome: "current" }; },
    }; },
    fetch: async () => { throw new Error("Synthetic offline Portal"); },
    deliveryNotifier: { async notify(failure) { state.notifications.push(failure); } },
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
  assert.equal((await f.database().query("PRAGMA user_version"))[0].user_version, 9);
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

test("foreground, manual and background share the strongest unbudgeted follow-up", { timeout: 5_000 }, async (t) => {
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
    // The published polling adapter submits its own unchanged background budget.
    const background = await submissions.submit(() => backgroundTaskBody(shell.backgroundPollingProvider)());
    const settled: string[] = [];
    void foreground.result.then(() => settled.push("foreground"));
    void manual.result.then(() => settled.push("manual"));
    void background.result.then(() => settled.push("background"));
    assert.equal((await runs(f.database())).length, 1);
    activeRead.release();
    assert.equal(checkpoint(await active).runSequence, 1);
    await followUpRead.arrived;
    assert.deepEqual(settled, []); // Every caller awaits the same follow-up checkpoint.
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
    assert.equal(backgroundResult, 1); // R8 keeps the unbudgeted follow-up; delivery still has its task deadline.
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
    enrollment(handle, transport) { return { ...f.options.enrollment(handle, transport), async status() {
      const current = f.state.status;
      if (current && !("outcome" in current) && current.stagingContext?.generation.generation === 2) generationRead();
      return current;
    } }; },
    async projectionSchemaVersions() {
      if (++f.state.versionReads === 2) {
        // Adoption waits for the raw Polling Run/checkpoint, independently of delivery.
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
  test(`${name}: foreground/manual skip and the background provider fails without a checkpoint`, async (t) => {
    const f = await fixture(t, status);
    const shell = await composeSender(f.options);
    assert.deepEqual(await shell.foreground(), { outcome: "skipped" });
    assert.deepEqual(await shell.manualSync(), { outcome: "skipped" });
    assert.equal(await (await shell.backgroundPollingProvider())!.runTask(), "failed");
    assert.equal(await backgroundTaskBody(shell.backgroundPollingProvider)(), 2);
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
  }, enrollment(_handle, transport) { return f.options.enrollment(f.database(), transport); } });
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
  assert.equal(await backgroundTaskBody(shell.backgroundPollingProvider)(), 2);
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
  assert.equal(await backgroundTaskBody(shell.backgroundPollingProvider)(), 2);
  assert.deepEqual(await runs(f.database()), beforeDrain);
  assert.deepEqual(await outboxMessages(f.database()), beforeMessages);
});

function response(body: Uint8Array) {
  return { status: 200, redirected: false, body: new ReadableStream<Uint8Array>({ start(stream) {
    stream.enqueue(body); stream.close();
  } }) };
}
function receipt(body: Uint8Array): Uint8Array {
  const message = decodeMessage(body);
  return encodeMessage({ wire_version: 1, kind: "delivery-receipt",
    sender_installation: message.sender_installation, sequence: message.sequence, message_sha256: messageSha256(body),
  });
}
async function acknowledgements(f: Awaited<ReturnType<typeof fixture>>, count: number, installation = INSTALLATION, key = KEY) {
  for (let n = 0; n < count; n++) {
    const last = await lastOutboxSequence(f.database(), installation);
    const body = encodeMessage({ wire_version: 1, kind: "capability-acknowledgement",
      sender_installation: installation, sequence: last + 1, capability_generation: 1,
    });
    const sealed = await f.cipher.seal(key, body, outboxAad(installation, last + 1));
    assert.equal(sealed.status, "ok");
    if (sealed.status !== "ok") throw new Error("Synthetic sealing failed");
    assert.equal(await f.database().transaction((tx) => appendOutboxMessages(tx, installation, last, [{
      kind: "capability-acknowledgement", sealed: sealed.value, messageBytes: body.length,
    }])), true);
  }
}
function portal(f: Awaited<ReturnType<typeof fixture>>) {
  const requests: { url: string; method: string; body: Uint8Array }[] = [];
  const state = {
    answer: async (_url: string, init: HttpFetchInit) => receipt(init.body!),
    inFlight: 0, maxInFlight: 0,
  };
  return { requests, state, options: { ...f.options, async fetch(url: string, init: HttpFetchInit) {
    requests.push({ url, method: init.method, body: new Uint8Array(init.body!) });
    state.maxInFlight = Math.max(state.maxInFlight, ++state.inFlight);
    try { return response(await state.answer(url, init)); }
    finally { state.inFlight--; }
  } } };
}

for (const trigger of ["foreground", "manual", "background"] as const) {
  test(`${trigger} delivers every staged message and its checkpoint`, async (t) => {
    const f = await fixture(t);
    const p = portal(f);
    const shell = await composeSender(p.options);
    await acknowledgements(f, 3);
    if (trigger === "foreground") checkpoint(await shell.foreground());
    else if (trigger === "manual") checkpoint(await shell.manualSync());
    else assert.equal(await backgroundTaskBody(shell.backgroundPollingProvider)(), 1);
    assert.deepEqual(await outboxMessages(f.database()), []);
    assert.ok(p.requests.length >= 4);
    assert.deepEqual(p.requests.map(({ body }) => decodeMessage(body).sequence),
      Array.from({ length: p.requests.length }, (_, n) => n + 1));
    assert.equal((await f.database().query("SELECT * FROM run_checkpoints")).length, 1);
  });
}

test("a blocked head stops the loop, notifies once, and the next trigger retries it before later messages", async (t) => {
  const f = await fixture(t, enrolled({ phase: "draining" }));
  const p = portal(f);
  p.state.answer = async (_url, init) => encodeMessage({ wire_version: 1, kind: "delivery-block",
    failure: "sequence-out-of-order", message_sha256: messageSha256(init.body!),
    sender_installation: INSTALLATION, sequence: decodeMessage(init.body!).sequence,
  });
  const shell = await composeSender(p.options);
  await acknowledgements(f, 3);
  assert.deepEqual(await shell.manualSync(), { outcome: "skipped" });
  assert.equal(p.requests.length, 1);
  assert.equal((await outboxMessages(f.database())).length, 3);
  assert.deepEqual(f.state.notifications, ["sequence-out-of-order"]);
  p.state.answer = async (_url, init) => receipt(init.body!);
  assert.deepEqual(await shell.manualSync(), { outcome: "skipped" });
  assert.equal(p.requests.length, 4);
  assert.deepEqual(p.requests[0].body, p.requests[1].body);
  assert.deepEqual(await outboxMessages(f.database()), []);
  assert.deepEqual(await f.database().query("SELECT * FROM outbox_head_block"), []);
});

test("retryable delivery stops after one attempt and never changes a successful checkpoint result", async (t) => {
  const f = await fixture(t);
  f.state.registered = true;
  const p = portal(f);
  p.state.answer = async () => encodeMessage({ wire_version: 1, kind: "retryable-failure", reason: "temporarily-unavailable" });
  const shell = await composeSender(p.options);
  await acknowledgements(f, 2);
  const first = checkpoint(await shell.manualSync());
  assert.equal(first.runSequence, 1);
  assert.equal(p.requests.length, 1);
  assert.ok((await outboxMessages(f.database())).length >= 3);
  assert.equal((await f.database().query("SELECT * FROM run_checkpoints")).length, 1);
  assert.deepEqual(f.state.notifications, []);
  p.state.answer = async () => { throw new Error("Synthetic network failure"); };
  assert.equal(checkpoint(await shell.manualSync()).runSequence, 2);
  assert.equal(p.requests.length, 2);
});

test("each loop is bounded and another trigger drains the remainder", async (t) => {
  const f = await fixture(t, enrolled({ phase: "draining" }));
  const p = portal(f);
  const shell = await composeSender(p.options);
  await acknowledgements(f, MAX_DELIVERY_ATTEMPTS + 2);
  await shell.manualSync();
  assert.equal(p.requests.length, MAX_DELIVERY_ATTEMPTS);
  assert.equal((await outboxMessages(f.database())).length, 2);
  await shell.foreground();
  assert.equal(p.requests.length, MAX_DELIVERY_ATTEMPTS + 2);
  assert.deepEqual(await outboxMessages(f.database()), []);
  assert.deepEqual(await runs(f.database()), []);
});

test("background delivery spends only the remaining shared task budget and reserves a full exchange", async (t) => {
  const f = await fixture(t, enrolled({ phase: "draining" }));
  const p = portal(f);
  const began = f.clock.mono;
  p.state.answer = async (_url, init) => {
    assert.ok(f.clock.mono + DELIVERY_TIMEOUT_MS + DELIVERY_FINISH_RESERVE_MS <= began + BACKGROUND_BUDGET_MS);
    f.clock.advance(DELIVERY_TIMEOUT_MS);
    return receipt(init.body!);
  };
  const shell = await composeSender(p.options);
  await acknowledgements(f, 12);
  assert.equal(await backgroundTaskBody(shell.backgroundPollingProvider)(), 2); // Still no Polling Run.
  assert.equal(p.requests.length, 7);
  assert.equal((await outboxMessages(f.database())).length, 5);
  p.state.answer = async (_url, init) => receipt(init.body!);
  await shell.manualSync();
  assert.deepEqual(await outboxMessages(f.database()), []);
});

test("a Polling Run that spends the background budget leaves delivery for the next trigger", async (t) => {
  const f = await fixture(t);
  const p = portal(f);
  f.sources.callCostMs = BACKGROUND_BUDGET_MS;
  const shell = await composeSender(p.options);
  await acknowledgements(f, 2);
  assert.equal(await backgroundTaskBody(shell.backgroundPollingProvider)(), 1);
  assert.equal(p.requests.length, 0);
  assert.ok((await outboxMessages(f.database())).length >= 3);
  f.state.status = enrolled({ phase: "draining" });
  await shell.manualSync();
  assert.deepEqual(await outboxMessages(f.database()), []);
});

for (const afterLastStart of [0, 1]) {
  test(`background delivery after a Polling Run honors the task window: ${afterLastStart}ms after last start`, { timeout: 5_000 }, async (t) => {
    const f = await fixture(t);
    const p = portal(f);
    const starts: number[] = [];
    p.state.answer = async (_url, init) => {
      starts.push(f.clock.mono);
      f.clock.advance(DELIVERY_TIMEOUT_MS);
      return receipt(init.body!);
    };
    const shell = await composeSender(p.options);
    await acknowledgements(f, 2);
    const held = f.sources.hold("snapshot", "steps");
    try {
      const began = f.clock.mono;
      const lastStart = began + BACKGROUND_BUDGET_MS - DELIVERY_TIMEOUT_MS - DELIVERY_FINISH_RESERVE_MS;
      const background = (await shell.backgroundPollingProvider())!.runTask();
      await held.arrived;
      f.clock.advance(lastStart - began + afterLastStart);
      held.release();
      assert.equal(await background, "success");
      assert.equal((await runs(f.database())).length, 1);
      assert.equal((await f.database().query("SELECT * FROM run_checkpoints")).length, 1);
      assert.deepEqual(starts, afterLastStart === 0 ? [lastStart] : []);
      assert.ok(starts.every((start) => start <= lastStart));
      assert.ok((await outboxMessages(f.database())).length > 0);
    } finally { held.release(); }
  });
}

test("overlapping triggers coalesce onto delivery without concurrent sendNext calls", async (t) => {
  const f = await fixture(t, enrolled({ phase: "draining" }));
  const p = portal(f);
  let arrive!: () => void;
  let release!: () => void;
  const arrived = new Promise<void>((resolve) => { arrive = resolve; });
  const released = new Promise<void>((resolve) => { release = resolve; });
  p.state.answer = async (_url, init) => { arrive(); await released; return receipt(init.body!); };
  const shell = await composeSender(p.options);
  await acknowledgements(f, 4);
  try {
    const first = shell.manualSync();
    await arrived;
    const others = [shell.manualSync(), shell.foreground(), backgroundTaskBody(shell.backgroundPollingProvider)()];
    await new Promise<void>((resolve) => setImmediate(resolve));
    assert.equal(p.requests.length, 1);
    release();
    await Promise.all([first, ...others]);
    assert.equal(p.requests.length, 4);
    assert.equal(p.state.maxInFlight, 1);
    assert.deepEqual(await outboxMessages(f.database()), []);
  } finally { release(); }
});

for (const phase of ["confirming", "refused", "absent"] as const) {
  test(`${phase} does not deliver queued messages`, async (t) => {
    const f = await fixture(t, phase === "absent" ? null : enrolled({ phase, confirmed: false, stagingContext: null }));
    const p = portal(f);
    const shell = await composeSender(p.options);
    await acknowledgements(f, 2);
    await shell.foreground(); await shell.manualSync();
    assert.equal(await backgroundTaskBody(shell.backgroundPollingProvider)(), 2);
    assert.equal(p.requests.length, 0);
    assert.equal((await outboxMessages(f.database())).length, 2);
  });
}

test("delivery context replacement uses the new URL, installation and key without overlapping sends", async (t) => {
  const f = await fixture(t, enrolled({ phase: "draining" }));
  const p = portal(f);
  const shell = await composeSender(p.options);
  await acknowledgements(f, 1);
  await shell.manualSync();
  const installation = "00000000-0000-4000-8000-000000000002";
  const key = "replacement-key";
  const url = "https://replacement.synthetic.invalid";
  await f.cipher.create(key);
  f.state.status = enrolled({ phase: "draining", deliveryContext: { installationId: installation, keyAlias: key, portalUrl: url } });
  await acknowledgements(f, 2, installation, key);
  await shell.foreground();
  assert.equal(p.requests[1].url, `${url}/v1/outbox-messages`);
  assert.equal(decodeMessage(p.requests[1].body).sender_installation, installation);
  assert.equal(p.requests.length, 3);
  assert.deepEqual(await outboxMessages(f.database()), []);
});

function advertisement(generation: number, recordType = "steps"): Uint8Array {
  return encodeMessage({ wire_version: 1, kind: "capability-advertisement", generation, wire_versions: [1],
    projection_schemas: [{ record_type: recordType, versions: [1] }], max_record_count: 100, max_message_bytes: 65_536,
  });
}

for (const [transition, status] of [
  ["drain", enrolled({ phase: "draining" })],
  ["reset", null],
] as const) {
  test(`foreground skips ${transition} during registration while an active run defers refresh`, { timeout: 5_000 }, async (t) => {
    const f = await fixture(t);
    let arrive!: () => void;
    let release!: () => void;
    const arrived = new Promise<void>((resolve) => { arrive = resolve; });
    const released = new Promise<void>((resolve) => { release = resolve; });
    let admit!: (result: { outcome: "admitted" }) => void;
    const admitted = new Promise<{ outcome: "admitted" }>((resolve) => { admit = resolve; });
    let watchAdmissions = false;
    let admissions = 0;
    let refreshes = 0;
    const shell = await composeSender({ ...f.options,
      now() {
        // Registration reads its clock before the gate. While the source is
        // held and delivery has not begun, only coordinator admission reads now().
        if (watchAdmissions) { admissions++; admit({ outcome: "admitted" }); }
        return f.clock.now();
      },
      monotonicNow() {
        // Delivery checks its deadline before reading its own wall clock.
        watchAdmissions = false;
        return f.clock.monotonicNow();
      },
      platform: { ...f.options.platform, async register(name, minutes) {
        arrive();
        await released;
        await f.options.platform.register(name, minutes);
      } },
      enrollment(database, transport) {
        return { ...f.options.enrollment(database, transport), async refreshCapabilities() {
          refreshes++;
          return { outcome: "current" };
        } };
      },
    });
    const activeRead = f.sources.hold("snapshot", "steps");
    const active = shell.manualSync();
    let foreground: Promise<ShellRunResult> | undefined;
    try {
      await activeRead.arrived;
      foreground = shell.foreground();
      const result = foreground.then((value) => value, (error: unknown) => ({ outcome: "rejected", error }));
      await arrived;
      assert.equal(f.sources.inFlight, 1, "the initial observation occurs during an active Polling Run");
      assert.equal(refreshes, 0, "capability refresh is deferred");
      f.state.status = status;
      watchAdmissions = true;
      release();
      assert.deepEqual(await Promise.race([result, admitted]), { outcome: "skipped" });
      assert.equal(admissions, 0, "no coordinator trigger is admitted after registration");
      assert.equal(f.sources.inFlight, 1, "skipping does not wait for the active Polling Run");
    } finally {
      watchAdmissions = false;
      release();
      activeRead.release();
      await Promise.allSettled([active, ...(foreground ? [foreground] : [])]);
    }
    assert.equal(refreshes, 0);
    assert.equal((await runs(f.database())).length, 1, "no follow-up Polling Run opens");
  });
}

for (const phase of ["confirming", "enrolled"] as const) {
  for (const foreground of ["start", "return"] as const) {
    test(`foreground ${foreground} registers before the ${phase} Portal request, even while unreachable`, { timeout: 5_000 }, async (t) => {
      const f = await fixture(t, null);
      let enrollment!: SenderEnrollment;
      let testing = false;
      let arrive!: () => void;
      let release!: () => void;
      const arrived = new Promise<void>((resolve) => { arrive = resolve; });
      const released = new Promise<void>((resolve) => { release = resolve; });
      let registrationAtRequest = 0;
      let observationAtRequest: unknown;
      const shell = await composeSender({ ...f.options,
        enrollment(database, transport) {
          enrollment = createSenderEnrollment({ database, transport, cipher: f.cipher,
            now: f.clock.now, randomUUID: () => INSTALLATION });
          return enrollment;
        },
        async fetch(url, init) {
          if (url.endsWith("/v1/outbox-messages")) return response(receipt(init.body!));
          if (testing) {
            registrationAtRequest = f.state.registerCalls;
            observationAtRequest = (await f.database().query("SELECT observed_status FROM scheduling_state"))[0]?.observed_status;
            arrive();
            await released;
            throw new Error("Synthetic unreachable Portal");
          }
          if (phase === "confirming") throw new Error("Synthetic lost confirmation");
          return response(advertisement(1));
        },
      });
      assert.equal((await enrollment.enroll({ kind: "typed", portalUrl: "https://portal.synthetic.invalid",
        enrollmentCode: "SYNTHETICCODE234",
      })).outcome, phase === "confirming" ? "retryable" : "enrolled");
      if (foreground === "return") await shell.foreground();
      const previousRegistrations = f.state.registerCalls;
      f.state.registered = false;
      // A fresh observation must finish on this trigger, even if a prior one exists.
      await f.database().execute("DELETE FROM scheduling_state");
      testing = true;
      const pending = shell.foreground();
      try {
        await arrived;
        assert.equal(registrationAtRequest, previousRegistrations + 1, "registration precedes the Portal request");
        assert.equal(observationAtRequest, "available", "registration observation completes before the Portal request");
        assert.equal(f.state.registered, true, "registration does not wait for the unreachable Portal");
      } finally {
        release();
        await pending;
      }
    });
  }

  test(`foreground registers ${phase} enrollment even when the status read after the Portal request fails`, { timeout: 5_000 }, async (t) => {
    const f = await fixture(t, null);
    let enrollment!: SenderEnrollment;
    let testing = false;
    let portalFailed = false;
    let failedStatusReads = 0;
    const shell = await composeSender({ ...f.options,
      enrollment(database, transport) {
        enrollment = createSenderEnrollment({ database, transport, cipher: f.cipher,
          now: f.clock.now, randomUUID: () => INSTALLATION });
        return { ...enrollment, async status() {
          if (portalFailed) { failedStatusReads++; throw new Error("Synthetic enrollment read failure"); }
          return enrollment.status();
        } };
      },
      async fetch() {
        if (testing) { portalFailed = true; throw new Error("Synthetic unreachable Portal"); }
        if (phase === "confirming") throw new Error("Synthetic lost confirmation");
        return response(advertisement(1));
      },
    });
    assert.equal((await enrollment.enroll({ kind: "typed", portalUrl: "https://portal.synthetic.invalid",
      enrollmentCode: "SYNTHETICCODE234",
    })).outcome, phase === "confirming" ? "retryable" : "enrolled");
    testing = true;
    assert.deepEqual(await shell.foreground(), { outcome: "failed", failure: "storage-failed" });
    assert.ok(failedStatusReads > 0, "the post-request status read failed");
    assert.equal(f.state.registerCalls, 1, "registration uses the initial active-enrollment observation");
    assert.equal(f.state.registered, true);
  });
}

test("the shared HTTP transport retries confirmation on every trigger and refreshes at start and return", async (t) => {
  const f = await fixture(t, null);
  f.state.versions = { steps: 1, weight: 1 };
  const p = portal(f);
  let enrollment!: SenderEnrollment;
  let confirmationAttempts = 0;
  let generation = 1;
  p.state.answer = async (url, init) => {
    if (url.endsWith("/v1/enrollment-confirmation")) {
      if (++confirmationAttempts < 5) throw new Error("Synthetic lost confirmation");
      return advertisement(generation);
    }
    if (init.method === "GET") return advertisement(generation, generation === 3 ? "weight" : "steps");
    return receipt(init.body!);
  };
  const shell = await composeSender({ ...p.options, enrollment(database, transport) {
    enrollment = createSenderEnrollment({ database, transport, cipher: f.cipher, now: f.clock.now, randomUUID: () => INSTALLATION });
    return enrollment;
  } });
  assert.equal((await enrollment.enroll({ kind: "typed", portalUrl: "https://portal.synthetic.invalid",
    enrollmentCode: "SYNTHETICCODE234",
  })).outcome, "retryable");
  assert.deepEqual(await shell.foreground(), { outcome: "skipped" });
  assert.deepEqual(await shell.manualSync(), { outcome: "skipped" });
  assert.equal(await backgroundTaskBody(shell.backgroundPollingProvider)(), 2);
  assert.equal(confirmationAttempts, 4);
  assert.deepEqual(await shell.foreground(), { outcome: "skipped" });
  assert.equal(confirmationAttempts, 5);
  const confirmations = p.requests.filter((request) => request.url.endsWith("/v1/enrollment-confirmation"));
  assert.ok(confirmations.every((request) => Buffer.from(request.body).equals(Buffer.from(confirmations[0].body))));
  const adopted = await enrollment.status();
  assert.ok(adopted && !("outcome" in adopted) && adopted.confirmed);
  assert.deepEqual(await outboxMessages(f.database()), []);
  generation = 2;
  assert.deepEqual(checkpoint(await shell.foreground()).summary.recordTypes.map((type) => type.recordType), ["steps"]);
  const refreshes = () => p.requests.filter((request) => request.method === "GET").length;
  assert.equal(refreshes(), 1);
  checkpoint(await shell.manualSync());
  assert.equal(await backgroundTaskBody(shell.backgroundPollingProvider)(), 1);
  assert.equal(refreshes(), 1);
  generation = 3;
  assert.deepEqual(checkpoint(await shell.foreground()).summary.recordTypes.map((type) => type.recordType), ["weight"]);
  assert.equal(refreshes(), 2);
  assert.deepEqual(await outboxMessages(f.database()), []);
  await enrollment.beginDrain();
  await shell.foreground();
  assert.equal(refreshes(), 2, "enrollment pack Decision 7 freezes the final sequence while draining");
});

test("a foreground refresh arriving during a Polling Run waits for its coalesced follow-up before adoption", async (t) => {
  const f = await fixture(t);
  f.state.versions = { steps: 1, weight: 1 };
  const p = portal(f);
  const submissions = submissionClock(f.clock.now);
  let refreshes = 0;
  const shell = await composeSender({ ...p.options, now: submissions.now, enrollment(database, transport) {
    return { ...f.options.enrollment(database, transport), async refreshCapabilities() {
      assert.equal(f.sources.inFlight, 0);
      assert.equal((await runs(f.database())).length, 2);
      assert.equal((await f.database().query("SELECT * FROM run_checkpoints")).length, 2);
      refreshes++;
      f.state.status = enrolled({ stagingContext: { ...enrolled().stagingContext!, generation: {
        generation: 2, maxRecordCount: 100, maxMessageBytes: 65_536, projectionSchemas: { weight: [1] },
      } } });
      return { outcome: "adopted", generation: 2 };
    } };
  } });
  const held = f.sources.hold("snapshot", "steps");
  try {
    const active = shell.manualSync();
    await held.arrived;
    const returning = await submissions.submit(() => shell.foreground(), 2);
    assert.equal(refreshes, 0);
    held.release();
    checkpoint(await active);
    checkpoint(await returning.result);
    assert.equal(refreshes, 1);
    assert.deepEqual(checkpoint(await shell.manualSync()).summary.recordTypes.map((type) => type.recordType), ["weight"]);
    assert.equal(p.state.maxInFlight, 1);
    assert.deepEqual(await outboxMessages(f.database()), []);
  } finally { held.release(); }
});

test("background confirmation time reduces the delivery budget", async (t) => {
  const f = await fixture(t, enrolled({ phase: "confirming", confirmed: false, stagingContext: null }));
  const p = portal(f);
  p.state.answer = async (_url, init) => { f.clock.advance(DELIVERY_TIMEOUT_MS); return receipt(init.body!); };
  const shell = await composeSender({ ...p.options, enrollment(database, transport) {
    return { ...f.options.enrollment(database, transport), async confirm() {
      f.clock.advance(120_000);
      f.state.status = enrolled({ confirmed: false });
      return { outcome: "enrolled", generation: 1 };
    } };
  } });
  await acknowledgements(f, 12);
  assert.equal(await backgroundTaskBody(shell.backgroundPollingProvider)(), 2);
  assert.equal(p.requests.length, 5);
  assert.equal((await outboxMessages(f.database())).length, 7);
});

for (const remaining of [DELIVERY_TIMEOUT_MS + DELIVERY_FINISH_RESERVE_MS - 1,
  DELIVERY_TIMEOUT_MS + DELIVERY_FINISH_RESERVE_MS]) {
  test(`background attempts require the full timeout and finish reserve: ${remaining}ms remain`, async (t) => {
    const f = await fixture(t, enrolled({ phase: "draining" }));
    const p = portal(f);
    let first = true;
    p.state.answer = async (_url, init) => { f.clock.advance(DELIVERY_TIMEOUT_MS); return receipt(init.body!); };
    const shell = await composeSender({ ...p.options, enrollment(database, transport) {
      return { ...f.options.enrollment(database, transport), async status() {
        if (first) { first = false; f.clock.advance(BACKGROUND_BUDGET_MS - remaining); }
        return f.state.status;
      } };
    } });
    await acknowledgements(f, 2);
    assert.equal(await backgroundTaskBody(shell.backgroundPollingProvider)(), 2);
    assert.equal(p.requests.length, remaining === DELIVERY_TIMEOUT_MS + DELIVERY_FINISH_RESERVE_MS ? 1 : 0);
  });
}

test("a reset during an unanswered request cannot start a second delivery loop", async (t) => {
  const f = await fixture(t, enrolled({ phase: "draining" }));
  const p = portal(f);
  let arrive!: () => void;
  let release!: () => void;
  const arrived = new Promise<void>((resolve) => { arrive = resolve; });
  const released = new Promise<void>((resolve) => { release = resolve; });
  p.state.answer = async (_url, init) => {
    if (p.requests.length === 1) { arrive(); await released; }
    return receipt(init.body!);
  };
  const shell = await composeSender(p.options);
  await acknowledgements(f, 2);
  const installation = "00000000-0000-4000-8000-000000000002";
  const key = "replacement-key";
  await f.cipher.create(key);
  try {
    const first = shell.manualSync();
    await arrived;
    // Reset/unlink deletes the old installation's Outbox before replacement.
    await f.database().execute("DELETE FROM outbox_messages WHERE installation_id = ?", [INSTALLATION]);
    await acknowledgements(f, 2, installation, key);
    f.state.status = enrolled({ phase: "draining", deliveryContext: {
      installationId: installation, keyAlias: key, portalUrl: "https://replacement.synthetic.invalid",
    } });
    const next = shell.manualSync();
    const background = backgroundTaskBody(shell.backgroundPollingProvider)();
    await new Promise<void>((resolve) => setImmediate(resolve));
    assert.equal(p.requests.length, 1);
    release();
    await Promise.all([first, next, background]);
    assert.equal(p.requests.length, 3);
    assert.equal(p.state.maxInFlight, 1);
    assert.ok(p.requests.slice(1).every((request) => request.url === "https://replacement.synthetic.invalid/v1/outbox-messages"));
    assert.deepEqual(await outboxMessages(f.database()), []);
    f.state.status = enrolled({ phase: "draining" });
    await shell.manualSync();
    assert.deepEqual(await outboxMessages(f.database()), []);
  } finally { release(); }
});



test("a retryable old-context request preserves the overlapping replacement delivery trigger", { timeout: 5_000 }, async (t) => {
  const f = await fixture(t, enrolled({ phase: "draining" }));
  const p = portal(f);
  let arrive!: () => void;
  let release!: () => void;
  const arrived = new Promise<void>((resolve) => { arrive = resolve; });
  const released = new Promise<void>((resolve) => { release = resolve; });
  p.state.answer = async (_url, init) => {
    if (p.requests.length === 1) {
      arrive(); await released;
      return encodeMessage({ wire_version: 1, kind: "retryable-failure", reason: "temporarily-unavailable" });
    }
    return receipt(init.body!);
  };
  const shell = await composeSender(p.options);
  await acknowledgements(f, 1);
  const installation = "00000000-0000-4000-8000-000000000002";
  const key = "replacement-key";
  const url = "https://replacement.synthetic.invalid";
  await f.cipher.create(key);
  try {
    const first = shell.manualSync();
    await arrived;
    await acknowledgements(f, 2, installation, key);
    f.state.status = enrolled({ phase: "draining", deliveryContext: { installationId: installation, keyAlias: key, portalUrl: url } });
    const next = shell.manualSync();
    await new Promise<void>((resolve) => setImmediate(resolve));
    release();
    await Promise.all([first, next]);
    assert.equal(p.requests.length, 3, "replacement Portal must receive its queued messages after the retryable old request");
    assert.ok(p.requests.slice(1).every((request) => request.url === `${url}/v1/outbox-messages`));
    assert.equal(p.state.maxInFlight, 1);
    assert.deepEqual((await outboxMessages(f.database())).map((message) => message.installationId), [INSTALLATION]);
  } finally { release(); }
});

for (const terminal of ["blocked", "retryable"] as const) {
  test(`overlapping triggers retain exactly one extra attempt for a ${terminal} head`, { timeout: 5_000 }, async (t) => {
    const f = await fixture(t, enrolled({ phase: "draining" }));
    const p = portal(f);
    let arrive!: () => void;
    let release!: () => void;
    const arrived = new Promise<void>((resolve) => { arrive = resolve; });
    const released = new Promise<void>((resolve) => { release = resolve; });
    p.state.answer = async (_url, init) => {
      if (p.requests.length === 1) { arrive(); await released; }
      return terminal === "retryable"
        ? encodeMessage({ wire_version: 1, kind: "retryable-failure", reason: "temporarily-unavailable" })
        : encodeMessage({ wire_version: 1, kind: "delivery-block", failure: "sequence-out-of-order",
          message_sha256: messageSha256(init.body!), sender_installation: INSTALLATION,
          sequence: decodeMessage(init.body!).sequence });
    };
    const shell = await composeSender(p.options);
    await acknowledgements(f, 2);
    try {
      const first = shell.manualSync();
      await arrived;
      const overlapping = Array.from({ length: 8 }, () => shell.manualSync());
      await new Promise<void>((resolve) => setImmediate(resolve));
      release();
      await Promise.all([first, ...overlapping]);
      assert.equal(p.requests.length, 2, "one overlapping trigger set buys one extra pass, with no retry spin");
      assert.deepEqual(p.requests[0].body, p.requests[1].body);
      assert.equal(p.state.maxInFlight, 1);
      assert.equal((await outboxMessages(f.database())).length, 2);
      assert.deepEqual(f.state.notifications, terminal === "blocked" ? ["sequence-out-of-order"] : []);
      await shell.manualSync();
      assert.equal(p.requests.length, 3, "a later explicit trigger can retry again");
    } finally { release(); }
  });
}

test("work staged just after an empty Outbox observation gets the overlapping trigger's pass", { timeout: 5_000 }, async (t) => {
  const f = await fixture(t, enrolled({ phase: "draining" }));
  const p = portal(f);
  let arrive!: () => void;
  let release!: () => void;
  const arrived = new Promise<void>((resolve) => { arrive = resolve; });
  const released = new Promise<void>((resolve) => { release = resolve; });
  let held = false;
  const shell = await composeSender({ ...p.options, async openDatabase() {
    const database = await f.options.openDatabase();
    const query = database.query.bind(database);
    database.query = async (sql, parameters) => {
      const rows = await query(sql, parameters);
      if (!held && sql.startsWith("SELECT sequence, kind, sealed, message_bytes FROM outbox_messages") && rows.length === 0) {
        held = true; arrive(); await released;
      }
      return rows;
    };
    return database;
  } });
  try {
    const first = shell.manualSync();
    await arrived;
    await acknowledgements(f, 1);
    const next = shell.manualSync();
    await new Promise<void>((resolve) => setImmediate(resolve));
    release();
    await Promise.all([first, next]);
    assert.equal(p.requests.length, 1);
    assert.deepEqual(await outboxMessages(f.database()), []);
    assert.equal(p.state.maxInFlight, 1);
  } finally { release(); }
});

test("coalesced background delivery callers preserve the tightest pending deadline", { timeout: 5_000 }, async (t) => {
  const f = await fixture(t, enrolled({ phase: "draining" }));
  const p = portal(f);
  let arrive!: () => void;
  let release!: () => void;
  const arrived = new Promise<void>((resolve) => { arrive = resolve; });
  const released = new Promise<void>((resolve) => { release = resolve; });
  p.state.answer = async (_url, init) => {
    if (p.requests.length === 1) { arrive(); await released; }
    return receipt(init.body!);
  };
  const shell = await composeSender(p.options);
  await acknowledgements(f, 3);
  try {
    const first = shell.manualSync();
    await arrived;
    const polling = (await shell.backgroundPollingProvider())!;
    const firstBackground = polling.runTask();
    await new Promise<void>((resolve) => setImmediate(resolve));
    f.clock.advance(100_000);
    const laterBackground = polling.runTask();
    const manual = shell.manualSync();
    await new Promise<void>((resolve) => setImmediate(resolve));
    // Enough time for the later background caller, one millisecond too little
    // for the earliest caller's full exchange plus receipt-bookkeeping reserve.
    f.clock.advance(BACKGROUND_BUDGET_MS - 100_000 - DELIVERY_TIMEOUT_MS - DELIVERY_FINISH_RESERVE_MS + 1);
    release();
    const results = await Promise.all([first, firstBackground, laterBackground, manual]);
    assert.deepEqual(results.slice(1, 3), ["failed", "failed"]);
    assert.equal(p.requests.length, 1, "neither the later deadline nor manual intent can extend the pending pass");
    assert.equal((await outboxMessages(f.database())).length, 2);
    await shell.manualSync();
    assert.equal(p.requests.length, 3);
    assert.deepEqual(await outboxMessages(f.database()), []);
  } finally { release(); }
});




test("the pending delivery pass has its own attempt cap and consumes one overlapping trigger set", { timeout: 5_000 }, async (t) => {
  const f = await fixture(t, enrolled({ phase: "draining" }));
  const p = portal(f);
  let arrive!: () => void;
  let release!: () => void;
  const arrived = new Promise<void>((resolve) => { arrive = resolve; });
  const released = new Promise<void>((resolve) => { release = resolve; });
  p.state.answer = async (_url, init) => {
    if (p.requests.length === 1) { arrive(); await released; }
    return receipt(init.body!);
  };
  const shell = await composeSender(p.options);
  await acknowledgements(f, MAX_DELIVERY_ATTEMPTS * 2 + 1);
  try {
    const first = shell.manualSync();
    await arrived;
    const overlapping = Array.from({ length: 4 }, () => shell.manualSync());
    await new Promise<void>((resolve) => setImmediate(resolve));
    release();
    await Promise.all([first, ...overlapping]);
    assert.equal(p.requests.length, MAX_DELIVERY_ATTEMPTS * 2);
    assert.equal((await outboxMessages(f.database())).length, 1);
    assert.equal(p.state.maxInFlight, 1);
    await shell.manualSync();
    assert.deepEqual(await outboxMessages(f.database()), []);
  } finally { release(); }
});

test("only a fresh trigger during the extra retryable pass buys another pass", { timeout: 5_000 }, async (t) => {
  const f = await fixture(t, enrolled({ phase: "draining" }));
  const p = portal(f);
  let firstArrive!: () => void;
  let firstRelease!: () => void;
  let secondArrive!: () => void;
  let secondRelease!: () => void;
  const firstArrived = new Promise<void>((resolve) => { firstArrive = resolve; });
  const firstReleased = new Promise<void>((resolve) => { firstRelease = resolve; });
  const secondArrived = new Promise<void>((resolve) => { secondArrive = resolve; });
  const secondReleased = new Promise<void>((resolve) => { secondRelease = resolve; });
  p.state.answer = async () => {
    if (p.requests.length === 1) { firstArrive(); await firstReleased; }
    if (p.requests.length === 2) { secondArrive(); await secondReleased; }
    return encodeMessage({ wire_version: 1, kind: "retryable-failure", reason: "temporarily-unavailable" });
  };
  const shell = await composeSender(p.options);
  await acknowledgements(f, 1);
  try {
    const first = shell.manualSync();
    await firstArrived;
    const next = shell.manualSync();
    await new Promise<void>((resolve) => setImmediate(resolve));
    firstRelease();
    await secondArrived;
    const fresh = shell.manualSync();
    await new Promise<void>((resolve) => setImmediate(resolve));
    secondRelease();
    await Promise.all([first, next, fresh]);
    assert.equal(p.requests.length, 3);
    assert.equal((await outboxMessages(f.database())).length, 1);
    assert.equal(p.state.maxInFlight, 1);
  } finally { firstRelease(); secondRelease(); }
});


for (const kind of ["manual", "foreground"] as const) {
  test(`round 2: background before ${kind} shares exactly one strongest follow-up`, { timeout: 5_000 }, async (t) => {
    const f = await fixture(t);
    const shell = await composeSender(f.options);
    const held = f.sources.hold("snapshot", "steps");
    try {
      const active = shell.manualSync();
      await held.arrived;
      const background = (await shell.backgroundPollingProvider())!.runTask();
      await new Promise<void>((resolve) => setImmediate(resolve));
      const following = kind === "manual" ? shell.manualSync() : shell.foreground();
      await new Promise<void>((resolve) => setImmediate(resolve));
      held.release();
      checkpoint(await active);
      const result = checkpoint(await following);
      assert.equal(await background, "success");
      assert.equal(result.runSequence, 2);
      assert.equal(result.summary.trigger, kind);
      assert.deepEqual((await runs(f.database())).map((row) => row.trigger), ["manual", kind]);
      assert.equal((await f.database().query("SELECT * FROM run_checkpoints")).length, 2);
    } finally { held.release(); }
  });
}

test("round 2: overlapping background callbacks share one follow-up", { timeout: 5_000 }, async (t) => {
  const f = await fixture(t);
  const shell = await composeSender(f.options);
  const held = f.sources.hold("snapshot", "steps");
  try {
    const active = shell.manualSync();
    await held.arrived;
    const polling = (await shell.backgroundPollingProvider())!;
    const backgrounds = Array.from({ length: 3 }, () => polling.runTask());
    await new Promise<void>((resolve) => setImmediate(resolve));
    held.release();
    checkpoint(await active);
    assert.deepEqual(await Promise.all(backgrounds), ["success", "success", "success"]);
    assert.deepEqual((await runs(f.database())).map((row) => row.trigger), ["manual", "background"]);
  } finally { held.release(); }
});

for (const count of [1, 32]) {
  test(`delivery status reads stay constant for ${count} queued messages`, { timeout: 5_000 }, async (t) => {
    const f = await fixture(t, enrolled({ phase: "draining" }));
    const p = portal(f);
    let reads = 0;
    const shell = await composeSender({ ...p.options, enrollment(database, transport) {
      return { ...f.options.enrollment(database, transport), async status() { reads++; return f.state.status; } };
    } });
    await acknowledgements(f, count);
    await shell.manualSync();
    assert.equal(p.requests.length, count);
    assert.equal(reads, 2, "one preparation read and one delivery-pass read, independent of queue length");
  });
}

for (const trigger of ["foreground", "manual", "background"] as const) {
  test(`${trigger} delivers queued messages when synchronization preparation rejects`, { timeout: 5_000 }, async (t) => {
    const f = await fixture(t);
    const p = portal(f);
    const failure = new Error("Synthetic native projection setup failure");
    const began = f.clock.mono;
    p.state.answer = async (_url, init) => {
      assert.ok(f.clock.mono + DELIVERY_TIMEOUT_MS + DELIVERY_FINISH_RESERVE_MS <= began + BACKGROUND_BUDGET_MS);
      f.clock.advance(DELIVERY_TIMEOUT_MS);
      return receipt(init.body!);
    };
    const shell = await composeSender({ ...p.options, async projectionSchemaVersions() { throw failure; } });
    await acknowledgements(f, 12);
    if (trigger === "background") {
      assert.equal(await (await shell.backgroundPollingProvider())!.runTask(), "failed");
      assert.equal(p.requests.length, 7, "failed setup retains only the shared task window for delivery");
    } else {
      // Foreground/manual remain rejected with the original preparation error.
      p.state.answer = async (_url, init) => receipt(init.body!);
      await assert.rejects(trigger === "foreground" ? shell.foreground() : shell.manualSync(), (error) => error === failure);
      assert.equal(p.requests.length, 12);
    }
    assert.deepEqual(await runs(f.database()), []);
  });
}

for (const refresh of ["current", "adopted", "unavailable"] as const) {
  for (const phase of ["enrolled", "refused"] as const) {
    test(`rejected coalesced foreground run refreshes ${refresh} capabilities once and delivers only in ${phase}`, { timeout: 5_000 }, async (t) => {
      const f = await fixture(t);
      const p = portal(f);
      const submissions = submissionClock(f.clock.now);
      const failure = new TypeError("Synthetic coordinator clock failure");
      let rejectCheckpoint = false;
      let refreshes = 0;
      const refreshInFlight: number[] = [];
      const events: string[] = [];
      const shell = await composeSender({ ...p.options, now() {
        if (rejectCheckpoint) { rejectCheckpoint = false; throw failure; }
        return submissions.now();
      }, enrollment(database, transport) {
        return { ...f.options.enrollment(database, transport), async refreshCapabilities() {
          refreshes++;
          refreshInFlight.push(f.sources.inFlight);
          events.push("refresh");
          f.state.status = enrolled({ phase });
          if (refresh === "unavailable") throw new Error("Synthetic capability refresh failure");
          if (refresh === "current") return { outcome: "current" };
          f.state.status = enrolled({ phase, stagingContext: { ...enrolled().stagingContext!, generation: {
            ...enrolled().stagingContext!.generation, generation: 2,
          } } });
          await acknowledgements(f, 1);
          return { outcome: "adopted", generation: 2 };
        } };
      } });
      const activeRead = f.sources.hold("snapshot", "steps");
      const followUpRead = f.sources.hold("snapshot", "steps");
      const gap = { outcome: "not-permitted", failure: "synthetic-permission-gap" };
      f.sources.next("snapshot", "steps", gap);
      f.sources.next("snapshot", "steps", () => { rejectCheckpoint = true; return gap; });
      try {
        const active = shell.manualSync();
        await activeRead.arrived;
        const returning = await submissions.submit(() => shell.foreground(), 2);
        const rejected = assert.rejects(returning.result, (error) => {
          events.push("rejected");
          return error === failure;
        });
        assert.equal(refreshes, 0);
        activeRead.release();
        checkpoint(await active);
        await followUpRead.arrived;
        await acknowledgements(f, 1);
        p.requests.length = 0;
        p.state.answer = async () => {
          events.push("delivery");
          return encodeMessage({ wire_version: 1, kind: "retryable-failure", reason: "temporarily-unavailable" });
        };
        followUpRead.release();
        await rejected;
        assert.equal(refreshes, 1);
        assert.deepEqual(refreshInFlight, [0]);
        assert.equal(f.sources.inFlight, 0);
        assert.equal(p.requests.length, phase === "enrolled" ? 1 : 0);
        assert.deepEqual(events, phase === "enrolled"
          ? ["refresh", "delivery", "rejected"] : ["refresh", "rejected"]);
      } finally { activeRead.release(); followUpRead.release(); }
    });
  }
}

for (const trigger of ["manual", "background", "foreground"] as const) {
  for (const phase of ["enrolled", "refused"] as const) {
    test(`${trigger} run rejection delivers at most once in ${phase} and skips refresh outside enrollment`, { timeout: 5_000 }, async (t) => {
      const f = await fixture(t);
      const p = portal(f);
      const submissions = submissionClock(f.clock.now);
      const failure = new TypeError("Synthetic coordinator clock failure");
      let rejectCheckpoint = false;
      let refreshes = 0;
      const shell = await composeSender({ ...p.options, now() {
        if (rejectCheckpoint) {
          rejectCheckpoint = false;
          f.state.status = enrolled({ phase });
          throw failure;
        }
        return submissions.now();
      }, enrollment(database, transport) {
        return { ...f.options.enrollment(database, transport), async refreshCapabilities() {
          refreshes++;
          return { outcome: "current" };
        } };
      } });
      const gap = { outcome: "not-permitted", failure: "synthetic-permission-gap" };
      const held = f.sources.hold("snapshot", "steps");
      let followUp: ReturnType<typeof f.sources.hold> | undefined;
      // Foreground must join an active run to defer its refresh.
      if (trigger === "foreground") f.sources.next("snapshot", "steps", gap);
      f.sources.next("snapshot", "steps", () => { rejectCheckpoint = true; return gap; });
      try {
        let active: Promise<ShellRunResult> | undefined;
        if (trigger === "foreground") {
          active = shell.manualSync();
          await held.arrived;
        } else await acknowledgements(f, 1);
        const polling = (await shell.backgroundPollingProvider())!;
        const request = trigger === "foreground" ? () => shell.foreground()
          : trigger === "manual" ? () => shell.manualSync() : () => polling.runTask();
        const submitted = await submissions.submit<unknown>(request, trigger === "foreground" ? 2 : 1);
        const outcome = trigger === "background"
          ? submitted.result.then((result) => { assert.equal(result, "failed"); })
          : assert.rejects(submitted.result, (error) => error === failure);
        if (active) {
          // The second source call remains held while the active trigger drains.
          followUp = f.sources.hold("snapshot", "steps");
          held.release();
          checkpoint(await active);
          await followUp.arrived;
          await acknowledgements(f, 1);
          p.requests.length = 0;
          followUp.release();
        } else { await held.arrived; held.release(); }
        p.state.answer = async () => encodeMessage({ wire_version: 1, kind: "retryable-failure", reason: "temporarily-unavailable" });
        await outcome;
        assert.equal(p.requests.length, phase === "enrolled" ? 1 : 0);
        assert.equal(refreshes, trigger === "foreground" && phase === "enrolled" ? 1 : 0);
      } finally { held.release(); followUp?.release(); }
    });
  }
}

for (const terminal of ["retryable", "blocked"] as const) {
  for (const refresh of ["current", "adopted"] as const) {
    test(`foreground return during an active run attempts its ${terminal} head only once after ${refresh} refresh`, { timeout: 5_000 }, async (t) => {
      const f = await fixture(t);
      const p = portal(f);
      const submissions = submissionClock(f.clock.now);
      p.state.answer = async (_url, init) => terminal === "retryable"
        ? encodeMessage({ wire_version: 1, kind: "retryable-failure", reason: "temporarily-unavailable" })
        : encodeMessage({ wire_version: 1, kind: "delivery-block", failure: "sequence-out-of-order",
          message_sha256: messageSha256(init.body!), sender_installation: INSTALLATION,
          sequence: decodeMessage(init.body!).sequence });
      const shell = await composeSender({ ...p.options, now: submissions.now, enrollment(database, transport) {
        return { ...f.options.enrollment(database, transport), async refreshCapabilities() {
          if (refresh === "current") return { outcome: "current" };
          f.state.status = enrolled({ stagingContext: { ...enrolled().stagingContext!, generation: {
            ...enrolled().stagingContext!.generation, generation: 2,
          } } });
          await acknowledgements(f, 1);
          return { outcome: "adopted", generation: 2 };
        } };
      } });
      await acknowledgements(f, 1);
      const activeRead = f.sources.hold("snapshot", "steps");
      f.sources.next("snapshot", "steps", { outcome: "not-permitted", failure: "synthetic-permission-gap" });
      try {
        const active = shell.manualSync();
        await activeRead.arrived;
        const returning = await submissions.submit(() => shell.foreground(), 2);
        activeRead.release();
        checkpoint(await active);
        checkpoint(await returning.result);
        assert.equal(p.requests.length, 2, "the foreground trigger adds one attempt, with no post-refresh retry");
        assert.deepEqual(p.requests[0].body, p.requests[1].body);
      } finally { activeRead.release(); }
    });
  }
}

test("foreground return during a slow drain refreshes capabilities before opening its Polling Run", { timeout: 5_000 }, async (t) => {
  const f = await fixture(t);
  f.state.versions = { steps: 1, weight: 1 };
  const p = portal(f);
  let arrive!: () => void;
  let release!: () => void;
  const arrived = new Promise<void>((resolve) => { arrive = resolve; });
  const released = new Promise<void>((resolve) => { release = resolve; });
  let refreshes = 0;
  const submissions = submissionClock(f.clock.now);
  p.state.answer = async (_url, init) => {
    if (p.requests.length === 1) { arrive(); await released; }
    return receipt(init.body!);
  };
  const shell = await composeSender({ ...p.options, now: submissions.now, enrollment(database, transport) {
    return { ...f.options.enrollment(database, transport), async refreshCapabilities() {
      assert.equal((await runs(f.database())).length, 1, "refresh must precede the second run");
      refreshes++;
      f.state.status = enrolled({ stagingContext: { ...enrolled().stagingContext!, generation: {
        generation: 2, maxRecordCount: 100, maxMessageBytes: 65_536, projectionSchemas: { weight: [1] },
      } } });
      return { outcome: "adopted", generation: 2 };
    } };
  } });
  try {
    const first = shell.manualSync();
    await arrived;
    assert.equal((await f.database().query("SELECT * FROM run_checkpoints")).length, 1);
    const returning = await submissions.submit(() => shell.foreground(), 2);
    assert.equal(refreshes, 1, "delivery must not count as an in-flight Polling Run");
    release();
    checkpoint(await first);
    assert.deepEqual(checkpoint(await returning.result).summary.recordTypes.map((type) => type.recordType), ["weight"]);
  } finally { release(); }
});


test("source condition checks share one enrollment observation per step", { timeout: 5_000 }, async (t) => {
  const f = await fixture(t);
  let reads = 0;
  const checks: number[] = [];
  const shell = await composeSender({ ...f.options, enrollment(database, transport) {
    return { ...f.options.enrollment(database, transport), async status() { reads++; return f.state.status; } };
  }, async availableStorageBytes() {
    checks.push(reads);
    reads = 0;
    return f.state.bytes;
  } });
  checkpoint(await shell.manualSync());
  assert.ok(checks.length > 1, "exercise repeated condition checks within a run");
  assert.ok(checks.slice(1).every((count) => count === 1), "collection and headBlock share each step's status read");
});
