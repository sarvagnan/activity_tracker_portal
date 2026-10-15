// Acceptance tests for schedule-background-polling@1.
// Requirement numbers refer to packs/schedule-background-polling-1.spec.md. Storage is the 3ug node:sqlite
// SenderDatabase substitute. The platform, notifier, and sync seams are synthetic fakes defined here; the
// checkpoint test runs the published coordinate-sender-sync@1 over the synthetic sources in support/.
import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import type { TestContext } from "node:test";
import { decodeMessage } from "@activity-tracker-portal/ingest-protocol";
import ts from "typescript";
import { outboxAad, outboxMessages, outboxMigration } from "../src/outbox/stage-outbox-batch.ts";
import type { StagingContext } from "../src/outbox/stage-outbox-batch.ts";
import { checkpointMigration } from "../src/outbox/stage-sync-checkpoint.ts";
import { completionMigration } from "../src/outbox/stage-sync-completion.ts";
import {
  BACKGROUND_BUDGET_MS, MINIMUM_INTERVAL_MINUTES, POLLING_TASK_NAME, createBackgroundPolling, schedulingMigration,
} from "../src/scheduling/schedule-background-polling.ts";
import type {
  BackgroundPolling, BackgroundPollingOptions, SchedulingCondition, SchedulingNotifier, SchedulingPlatform,
} from "../src/scheduling/schedule-background-polling.ts";
import { snapshotAttemptsMigration } from "../src/snapshot/begin-initial-snapshot.ts";
import type { DatabaseRow, DatabaseValue, SenderDatabase } from "../src/storage/database.ts";
import { migrate } from "../src/storage/migrations.ts";
import { createSenderSync, syncMigration } from "../src/sync/coordinate-sender-sync.ts";
import type { RunResult, SyncConditions, TriggerRequest } from "../src/sync/coordinate-sender-sync.ts";
import { storageLane } from "../src/sync/sync-storage.ts";
import { openNodeDatabase } from "./support/node-database.ts";
import { NodeInstallationCipher } from "./support/node-installation-cipher.ts";
import { SyntheticClock, SyntheticSources } from "./support/synthetic-sync-sources.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const MODULE = join(HERE, "../src/scheduling/schedule-background-polling.ts");
const MODULE_DIRECTORY = dirname(MODULE);
const APP_CONFIG = join(HERE, "../app.config.ts");
const INSTALLATION = "00000000-0000-4000-8000-000000000001";
const KEY_ALIAS = "synthetic-installation-key";
const SENTINEL = "SENTINEL";
const MIGRATIONS = [snapshotAttemptsMigration, outboxMigration, completionMigration, checkpointMigration, syncMigration, schedulingMigration];
const START = Date.UTC(2026, 9, 15, 9, 0, 0, 0);
const HOUR = 3_600_000;
const CLEAR: SyncConditions = { collectionEnabled: true, secureLock: true, deliveryBlocked: false, storagePressure: false };
const BACKGROUND: TriggerRequest = { kind: "background", budgetMs: 480_000 };
const CHECKPOINTED = {
  outcome: "checkpointed", runSequence: 1,
  summary: { runSequence: 1, trigger: "background", startedAtMs: START, endedAtMs: START, backgroundScheduling: "available", recordTypes: [] },
} as const satisfies RunResult;
/** Other units' tables, which this unit never names. */
const OTHER_TABLES = [
  "snapshot_attempts", "current_snapshot_attempts", "outbox_messages", "outbox_high_water", "cycle_progress", "cycle_completions",
  "run_checkpoints", "sync_types", "sync_blocks", "sync_runs", "sync_run_types", "sync_run_cycles",
];

type PlatformMethod = "isRegistered" | "register" | "restriction";
type Script = { readonly throws: unknown; readonly synchronously?: boolean } | { readonly returns: unknown };

/** Registration and restriction state the test sets, with scripted thrown or malformed answers. */
class FakePlatform implements SchedulingPlatform {
  registered = true;
  restricted = false;
  /** Every method called, in order, including those outside the seam. */
  readonly calls: string[] = [];
  /** The task name each registration read asked about. */
  readonly names: unknown[] = [];
  readonly registrations: unknown[][] = [];
  private readonly scripts = new Map<PlatformMethod, Script[]>();

  /** The next call of `method` answers from the script instead of the state. */
  next(method: PlatformMethod, script: Script): void {
    this.scripts.set(method, [...this.scripts.get(method) ?? [], script]);
  }

  isRegistered(taskName: string): Promise<boolean> {
    this.calls.push("isRegistered");
    this.names.push(taskName);
    return this.answer("isRegistered", () => this.registered);
  }

  register(...args: [string, number]): Promise<void> {
    this.calls.push("register");
    this.registrations.push(args);
    return this.answer("register", () => { this.registered = true; });
  }

  restriction(): Promise<"available" | "restricted"> {
    this.calls.push("restriction");
    return this.answer("restriction", () => this.restricted ? "restricted" : "available");
  }

  // Outside the seam: the module must never call these.
  unregister(): Promise<void> { this.calls.push("unregister"); return Promise.resolve(); }
  unregisterTaskAsync(): Promise<void> { this.calls.push("unregisterTaskAsync"); return Promise.resolve(); }
  unregisterAllTasksAsync(): Promise<void> { this.calls.push("unregisterAllTasksAsync"); return Promise.resolve(); }
  requestIgnoreBatteryOptimizations(): Promise<void> { this.calls.push("requestIgnoreBatteryOptimizations"); return Promise.resolve(); }
  startForegroundService(): Promise<void> { this.calls.push("startForegroundService"); return Promise.resolve(); }

  private answer<T>(method: PlatformMethod, normal: () => T): Promise<T> {
    const script = this.scripts.get(method)?.shift();
    if (script === undefined) return Promise.resolve(normal());
    if ("returns" in script) return Promise.resolve(script.returns as T);
    if (script.synchronously) throw script.throws;
    return Promise.reject(script.throws);
  }
}

/**
 * Records every notification attempt; the next `failures` attempts throw, and an unopened gate holds delivery.
 * `onNotify` runs synchronously as each attempt starts, and `arrival()` resolves at the next attempt.
 */
class FakeNotifier implements SchedulingNotifier {
  readonly calls: unknown[][] = [];
  readonly attempts: string[] = [];
  readonly delivered: string[] = [];
  failures = 0;
  synchronously = false;
  gate: Promise<void> | null = null;
  onNotify: (() => void) | null = null;
  private arrivals: (() => void)[] = [];

  arrival(): Promise<void> {
    return new Promise<void>((resolve) => { this.arrivals.push(resolve); });
  }

  notify(...args: [SchedulingCondition]): Promise<void> {
    this.calls.push(args);
    const [condition] = args;
    this.attempts.push(condition);
    this.onNotify?.();
    for (const arrive of this.arrivals.splice(0)) arrive();
    if (this.failures > 0) {
      this.failures--;
      const error = new Error(`${SENTINEL} synthetic notification failure`);
      if (this.synchronously) throw error;
      return Promise.reject(error);
    }
    return (this.gate ?? Promise.resolve()).then(() => { this.delivered.push(condition); });
  }
}

/** Records each trigger and answers with `respond`, which may also reject or throw. */
class FakeSync {
  readonly triggers: TriggerRequest[] = [];
  respond: () => unknown = () => Promise.resolve(CHECKPOINTED);
  // An arrow property, so a proxy sees only the `trigger` access.
  readonly trigger = (request: TriggerRequest): Promise<RunResult> => {
    this.triggers.push(structuredClone(request));
    return this.respond() as Promise<RunResult>;
  };
}

async function fixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const path = join(directory, "synthetic.sqlite");
  let database = await openNodeDatabase(path);
  t.after(async () => {
    try { await database.close(); } catch { /* a test may leave the old process's database closed */ }
    rmSync(directory, { recursive: true, force: true });
  });
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 6 });
  const f = {
    platform: new FakePlatform(),
    notifier: new FakeNotifier(),
    sync: new FakeSync(),
    wall: START,
    now: (): number => f.wall,
    get database(): SenderDatabase { return database; },
    make(overrides: Partial<BackgroundPollingOptions> = {}): BackgroundPolling {
      return createBackgroundPolling({ database, platform: f.platform, notifier: f.notifier, sync: f.sync, now: f.now, ...overrides });
    },
    /** A new process on the same file; the old instance is never used again. */
    async reopen(): Promise<void> { await database.close(); database = await openNodeDatabase(path); f.polling = f.make(); },
    polling: undefined as unknown as BackgroundPolling,
  };
  f.polling = f.make();
  return f;
}
type Fixture = Awaited<ReturnType<typeof fixture>>;

const observed = (status: string, condition: string | null) => ({ outcome: "observed", status, condition });
const settle = (ms = 20) => new Promise<void>((resolve) => { setTimeout(resolve, ms); });

function row(status: string, atMs: number, condition: string | null, notified: string | null): DatabaseRow {
  return { singleton: 1, observed_status: status, observed_at_ms: atMs, action_condition: condition, notified_condition: notified };
}

async function state(f: Fixture): Promise<DatabaseRow[]> {
  return f.database.query("SELECT * FROM scheduling_state ORDER BY rowid");
}

/**
 * The whole database: the schema version, every schema object, and every row of every table,
 * so no unexpected table or write can go unnoticed.
 */
async function contents(database: SenderDatabase) {
  const schema = await database.query("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name");
  const tables: Record<string, unknown> = {};
  for (const { name } of schema.filter((entry) => entry.type === "table")) {
    tables[name as string] = await database.query(`SELECT * FROM "${name as string}" ORDER BY rowid`);
  }
  return { version: await database.query("PRAGMA user_version"), schema, tables };
}

const MUTATION = /^\s*(?:INSERT|UPDATE|DELETE|REPLACE)\b/i;

/** Every write fails while `failing()` holds; reads succeed. */
function failingWrites(database: SenderDatabase, failing: () => boolean = () => true): SenderDatabase {
  const guard = (sql: string) => { if (failing() && MUTATION.test(sql)) throw new Error(`${SENTINEL} synthetic write failure`); };
  return {
    async execute(sql, parameters) { guard(sql); return database.execute(sql, parameters); },
    query: (sql, parameters) => database.query(sql, parameters),
    transaction: (fn) => database.transaction((tx) => fn({
      execute: async (sql: string, parameters?: readonly DatabaseValue[]) => { guard(sql); return tx.execute(sql, parameters); },
      query: (sql: string, parameters?: readonly DatabaseValue[]) => tx.query(sql, parameters),
      transaction: tx.transaction.bind(tx),
      close: tx.close.bind(tx),
    })),
    close: () => database.close(),
  };
}

/** Every operation fails with `error`. */
function brokenDatabase(error: unknown): SenderDatabase {
  const fail = () => Promise.reject(error);
  return { execute: fail, query: fail, transaction: fail, close: fail };
}

/** Logs each top-level operation. */
function logging(database: SenderDatabase, operations: string[]): SenderDatabase {
  return {
    execute: (sql, parameters) => { operations.push(sql); return database.execute(sql, parameters); },
    query: (sql, parameters) => { operations.push(sql); return database.query(sql, parameters); },
    transaction: (fn) => { operations.push("transaction"); return database.transaction(fn); },
    close: () => database.close(),
  };
}

/** Each Sync Checkpoint in the Outbox, opened and decoded: run sequence, trigger, and scheduling status. */
async function checkpoints(database: SenderDatabase, cipher: NodeInstallationCipher): Promise<unknown[][]> {
  const result: unknown[][] = [];
  for (const entry of await outboxMessages(database)) {
    if (entry.kind !== "sync-checkpoint") continue;
    const plain = await cipher.open(KEY_ALIAS, entry.sealed, outboxAad(entry.installationId, entry.sequence));
    assert.equal(plain.status, "ok");
    const message = decodeMessage((plain as { value: Uint8Array }).value) as unknown as Record<string, unknown>;
    result.push([message.run_sequence, message.trigger, message.background_scheduling]);
  }
  return result;
}

// R2 — Caller errors

test("malformed_options_are_caller_errors", async (t) => {
  const f = await fixture(t);
  const before = await contents(f.database);
  const valid = { database: f.database, platform: f.platform, notifier: f.notifier, sync: f.sync, now: f.now };
  const malformed: unknown[] = [
    undefined, null, 7, "options",
    { ...valid, database: undefined }, { ...valid, database: { execute: f.database.execute, query: f.database.query } },
    { ...valid, platform: null }, { ...valid, platform: { isRegistered: async () => true, register: async () => {} } },
    { ...valid, notifier: {} }, { ...valid, notifier: { notify: "restricted" } },
    { ...valid, sync: undefined }, { ...valid, sync: { requestRetry: async () => CHECKPOINTED } },
    { ...valid, now: undefined }, { ...valid, now: START },
  ];
  malformed.forEach((options, index) => {
    assert.throws(() => createBackgroundPolling(options as BackgroundPollingOptions), TypeError, `case ${index}`);
  });
  for (const reading of [1.5, Number.NaN, Infinity, -Infinity, 2 ** 53, "1760518800000", null, undefined, 1n]) {
    const polling = f.make({ now: () => reading as number });
    await assert.rejects(async () => polling.ensureRegistered(), TypeError, String(reading));
    await assert.rejects(async () => polling.observe(), TypeError, String(reading));
  }
  assert.deepEqual(f.platform.calls, []);
  assert.deepEqual(f.notifier.calls, []);
  assert.deepEqual(f.sync.triggers, []);
  assert.deepEqual(await contents(f.database), before);
});

// R3 — Registration

test("a_missing_task_is_registered_hourly", async (t) => {
  const f = await fixture(t);
  assert.equal(POLLING_TASK_NAME, "activity-tracker-portal.polling");
  assert.equal(MINIMUM_INTERVAL_MINUTES, 60);
  f.platform.registered = false;
  assert.deepEqual(await f.polling.ensureRegistered(), observed("available", null));
  assert.deepEqual(f.platform.registrations, [["activity-tracker-portal.polling", 60]]);
  // Registration is read, the task registered, then the observation reads registration and restriction.
  assert.deepEqual(f.platform.calls, ["isRegistered", "register", "isRegistered", "restriction"]);
  assert.deepEqual(await f.polling.ensureRegistered(), observed("available", null));
  assert.equal(f.platform.registrations.length, 1);
  assert.deepEqual([...new Set(f.platform.names)], [POLLING_TASK_NAME]);
  assert.deepEqual(f.notifier.calls, []);
});

test("a_registered_task_is_never_registered_again", async (t) => {
  const f = await fixture(t);
  for (let i = 0; i < 3; i++) assert.deepEqual(await f.polling.ensureRegistered(), observed("available", null));
  f.platform.restricted = true;
  assert.deepEqual(await f.polling.ensureRegistered(), observed("restricted", "restricted"));
  await f.reopen();
  assert.deepEqual(await f.polling.ensureRegistered(), observed("restricted", "restricted"));
  assert.deepEqual(f.platform.registrations, []);
  assert.equal(f.platform.calls.includes("register"), false);
});

test("the_task_is_never_unregistered", async (t) => {
  const f = await fixture(t);
  const error = new Error(`${SENTINEL} synthetic platform failure`);
  f.platform.registered = false;
  await f.polling.ensureRegistered();
  f.platform.restricted = true;
  await f.polling.observe();
  f.platform.registered = false;
  f.platform.next("register", { throws: error });
  await f.polling.ensureRegistered();
  await f.polling.ensureRegistered();
  f.platform.next("restriction", { throws: error });
  await f.polling.observe();
  f.platform.restricted = false;
  await f.polling.observe();
  await f.polling.runTask();
  f.sync.respond = () => Promise.resolve({ outcome: "failed", failure: "storage-failed" });
  await f.polling.runTask();
  await f.polling.condition();
  await f.reopen();
  await f.polling.ensureRegistered();
  assert.deepEqual([...new Set(f.platform.calls)].sort(), ["isRegistered", "register", "restriction"]);
  assert.equal(f.platform.registrations.length, 3);
});

test("a_failed_registration_reads_as_unregistered", async (t) => {
  const f = await fixture(t);
  f.platform.registered = false;
  f.platform.next("register", { throws: new Error(`${SENTINEL} synthetic registration failure`) });
  assert.deepEqual(await f.polling.ensureRegistered(), observed("unregistered", "unregistered"));
  assert.deepEqual(f.notifier.attempts, ["unregistered"]);
  assert.equal(await f.polling.condition(), "unregistered");
  f.platform.next("register", { throws: new Error(`${SENTINEL} synthetic registration failure`), synchronously: true });
  assert.deepEqual(await f.polling.ensureRegistered(), observed("unregistered", "unregistered"));
  // A registration that resolves without registering reads the same way.
  f.platform.next("register", { returns: undefined });
  assert.deepEqual(await f.polling.ensureRegistered(), observed("unregistered", "unregistered"));
  assert.equal(f.platform.registered, false);
  assert.deepEqual(f.notifier.attempts, ["unregistered"]);
  // The next start registers it, which clears the condition.
  assert.deepEqual(await f.polling.ensureRegistered(), observed("available", null));
  assert.equal(f.platform.registrations.length, 4);
  assert.equal(await f.polling.condition(), null);
});

// R4 — The observation

test("a_registered_unrestricted_task_is_available", async (t) => {
  const f = await fixture(t);
  assert.equal(await f.polling.observe(), "available");
  assert.deepEqual(f.platform.calls, ["isRegistered", "restriction"]);
  assert.deepEqual(f.platform.names, [POLLING_TASK_NAME]);
  assert.equal(await f.polling.condition(), null);
  assert.deepEqual(f.notifier.calls, []);
});

test("a_restricted_platform_is_restricted", async (t) => {
  const f = await fixture(t);
  f.platform.restricted = true;
  assert.equal(await f.polling.observe(), "restricted");
  assert.deepEqual(f.platform.calls, ["isRegistered", "restriction"]);
  assert.equal(await f.polling.condition(), "restricted");
  assert.deepEqual(await f.polling.ensureRegistered(), observed("restricted", "restricted"));
});

test("unregistered_wins_over_restricted", async (t) => {
  const f = await fixture(t);
  f.platform.registered = false;
  f.platform.restricted = true;
  assert.equal(await f.polling.observe(), "unregistered");
  assert.equal(f.platform.calls[0], "isRegistered");
  assert.equal(await f.polling.condition(), "unregistered");
  assert.deepEqual(f.notifier.attempts, ["unregistered"]);
  f.platform.next("restriction", { throws: new Error(`${SENTINEL} synthetic platform failure`) });
  assert.equal(await f.polling.observe(), "unregistered");
  assert.deepEqual(f.notifier.attempts, ["unregistered"]);
});

test("observing_never_registers", async (t) => {
  const f = await fixture(t);
  f.platform.registered = false;
  for (let i = 0; i < 3; i++) assert.equal(await f.polling.observe(), "unregistered");
  assert.equal(f.platform.calls.includes("register"), false);
  assert.deepEqual(f.platform.registrations, []);
});

test("observations_are_recorded_with_their_time", async (t) => {
  const f = await fixture(t);
  assert.deepEqual(await state(f), []);
  assert.equal(await f.polling.condition(), null);
  assert.equal(await f.polling.observe(), "available");
  assert.deepEqual(await state(f), [row("available", START, null, null)]);
  f.wall = START + HOUR;
  f.platform.restricted = true;
  assert.equal(await f.polling.observe(), "restricted");
  assert.deepEqual(await state(f), [row("restricted", START + HOUR, "restricted", "restricted")]);
  f.wall = START + 2 * HOUR + 1;
  f.platform.restricted = false;
  assert.deepEqual(await f.polling.ensureRegistered(), observed("available", null));
  assert.deepEqual(await state(f), [row("available", START + 2 * HOUR + 1, null, null)]);
  // A wall clock that moved backward is recorded as read.
  f.wall = START - HOUR;
  assert.equal(await f.polling.observe(), "available");
  assert.deepEqual(await state(f), [row("available", START - HOUR, null, null)]);
});

// R5 — The status each checkpoint reports

test("the_checkpoint_reports_the_observation_at_run_open", async (t) => {
  const f = await fixture(t);
  const cipher = new NodeInstallationCipher();
  assert.equal((await cipher.create(KEY_ALIAS)).status, "ok");
  const clock = new SyntheticClock(START);
  const sources = new SyntheticSources(clock);
  sources.seed("steps", 3);
  const context: StagingContext = {
    installationId: INSTALLATION, keyAlias: KEY_ALIAS,
    generation: { generation: 6, maxRecordCount: 100, maxMessageBytes: 65_536, projectionSchemas: { steps: [1] } },
  };
  let uuid = 0;
  let polling!: BackgroundPolling;
  // One process: the published coordinator, given this unit's observe() as its scheduling status.
  const start = (database: SenderDatabase) => {
    const sync = createSenderSync({
      database, cipher, sources, recordTypes: ["steps"],
      projection: { implementationVersion: "synthetic-build-1", schemaVersions: { steps: 1 } },
      context: () => context, conditions: () => ({ ...CLEAR }), backgroundScheduling: () => polling.observe(),
      now: clock.now, monotonicNow: clock.monotonicNow,
      randomUUID: () => `aaaaaaaa-0000-4000-8000-${(++uuid).toString(16).padStart(12, "0")}`,
    });
    polling = f.make({ database, sync, now: clock.now });
  };
  start(f.database);
  // Run 1 opens while background work is restricted.
  f.platform.restricted = true;
  assert.equal(await polling.runTask(), "success");
  // Run 2 opens while the task is unregistered, and the process dies during a read.
  f.platform.restricted = false;
  f.platform.registered = false;
  const held = sources.hold("change", "steps");
  void polling.runTask();
  await held.arrived;
  await f.reopen();
  // The next process observes available scheduling; the recovered run keeps the status it opened with.
  f.platform.registered = true;
  start(f.database);
  clock.advance(HOUR);
  assert.equal(await polling.runTask(), "success");
  assert.deepEqual(await checkpoints(f.database, cipher), [
    [1, "background", "restricted"], [2, "background", "unregistered"], [3, "background", "available"],
  ]);
  assert.deepEqual(f.notifier.attempts, ["restricted", "unregistered"]);
  assert.deepEqual(await state(f), [row("available", START + HOUR, null, null)]);
});

// R6 — The action-required condition

test("an_action_required_condition_notifies_once", async (t) => {
  const f = await fixture(t);
  f.platform.restricted = true;
  for (let i = 0; i < 3; i++) assert.equal(await f.polling.observe(), "restricted");
  assert.deepEqual(await f.polling.ensureRegistered(), observed("restricted", "restricted"));
  // The notification names only the condition.
  assert.deepEqual(f.notifier.calls, [["restricted"]]);
  assert.deepEqual(f.notifier.delivered, ["restricted"]);
  assert.equal(await f.polling.condition(), "restricted");
});

test("recovery_clears_the_condition", async (t) => {
  const f = await fixture(t);
  f.platform.restricted = true;
  await f.polling.observe();
  f.platform.restricted = false;
  assert.equal(await f.polling.observe(), "available");
  assert.equal(await f.polling.condition(), null);
  assert.deepEqual(await state(f), [row("available", START, null, null)]);
  // A later onset notifies again.
  f.platform.restricted = true;
  await f.polling.observe();
  assert.deepEqual(f.notifier.delivered, ["restricted", "restricted"]);
  // An unregistered task recovers when a later start registers it.
  f.platform.restricted = false;
  f.platform.registered = false;
  f.platform.next("register", { throws: new Error(`${SENTINEL} synthetic registration failure`) });
  assert.deepEqual(await f.polling.ensureRegistered(), observed("unregistered", "unregistered"));
  assert.deepEqual(await f.polling.ensureRegistered(), observed("available", null));
  assert.equal(await f.polling.condition(), null);
  assert.deepEqual(f.notifier.attempts, ["restricted", "restricted", "unregistered"]);
});

test("a_new_condition_notifies_again", async (t) => {
  const f = await fixture(t);
  f.platform.restricted = true;
  await f.polling.observe();
  f.platform.registered = false;
  assert.equal(await f.polling.observe(), "unregistered");
  f.platform.registered = true;
  assert.equal(await f.polling.observe(), "restricted");
  assert.deepEqual(f.notifier.delivered, ["restricted", "unregistered", "restricted"]);
  // A change of condition is an onset even when the previous onset's notification failed.
  f.platform.registered = false;
  f.notifier.failures = 1;
  assert.equal(await f.polling.observe(), "unregistered");
  f.platform.registered = true;
  assert.equal(await f.polling.observe(), "restricted");
  assert.deepEqual(f.notifier.attempts, ["restricted", "unregistered", "restricted", "unregistered", "restricted"]);
  assert.deepEqual(f.notifier.delivered, ["restricted", "unregistered", "restricted", "restricted"]);
});

test("a_failed_notification_is_retried", async (t) => {
  const f = await fixture(t);
  f.platform.restricted = true;
  f.notifier.failures = 2;
  assert.equal(await f.polling.observe(), "restricted");
  assert.deepEqual(await state(f), [row("restricted", START, "restricted", null)]);
  assert.deepEqual(await f.polling.ensureRegistered(), observed("restricted", "restricted"));
  assert.equal(await f.polling.observe(), "restricted");
  assert.equal(await f.polling.observe(), "restricted");
  assert.deepEqual(f.notifier.attempts, ["restricted", "restricted", "restricted"]);
  assert.deepEqual(f.notifier.delivered, ["restricted"]);
  assert.deepEqual(await state(f), [row("restricted", START, "restricted", "restricted")]);
  // A notifier that throws synchronously is retried the same way.
  f.platform.registered = false;
  f.notifier.failures = 1;
  f.notifier.synchronously = true;
  assert.equal(await f.polling.observe(), "unregistered");
  assert.equal(await f.polling.observe(), "unregistered");
  assert.deepEqual(f.notifier.delivered, ["restricted", "unregistered"]);
});

test("the_condition_survives_a_restart", async (t) => {
  const f = await fixture(t);
  f.platform.restricted = true;
  f.notifier.failures = 1;
  assert.equal(await f.polling.observe(), "restricted");
  await f.reopen();
  assert.equal(await f.polling.condition(), "restricted");
  // The pending notification is retried by the new process, then not repeated.
  assert.equal(await f.polling.observe(), "restricted");
  assert.deepEqual(f.notifier.delivered, ["restricted"]);
  await f.reopen();
  assert.equal(await f.polling.condition(), "restricted");
  assert.deepEqual(await f.polling.ensureRegistered(), observed("restricted", "restricted"));
  assert.deepEqual(f.notifier.attempts, ["restricted", "restricted"]);
});

test("concurrent_observations_notify_once", async (t) => {
  const f = await fixture(t);
  f.platform.restricted = true;
  let open!: () => void;
  f.notifier.gate = new Promise<void>((resolve) => { open = resolve; });
  const entered = f.notifier.arrival();
  const pending = Promise.all([f.polling.observe(), f.polling.ensureRegistered(), f.polling.observe()]);
  try {
    // While the first notification is pending, the other observations get time to run; none may notify.
    await entered;
    await settle();
    assert.deepEqual(f.notifier.attempts, ["restricted"]);
  } finally { open(); }
  assert.deepEqual(await pending, ["restricted", observed("restricted", "restricted"), "restricted"]);
  assert.deepEqual(f.notifier.attempts, ["restricted"]);
});

test("a_lost_acknowledgement_notifies_again", async (t) => {
  const f = await fixture(t);
  f.platform.restricted = true;
  // Storage fails from the moment the notification is delivered, so only its acknowledgement is lost.
  let failing = false;
  const polling = f.make({ database: failingWrites(f.database, () => failing) });
  f.notifier.onNotify = () => { failing = true; };
  assert.deepEqual(await polling.ensureRegistered(), { outcome: "storage-failed" });
  assert.deepEqual(f.notifier.delivered, ["restricted"]);
  f.notifier.onNotify = null;
  failing = false;
  // The observation and its condition stayed durable; the notification is repeated, then acknowledged.
  assert.deepEqual(await state(f), [row("restricted", START, "restricted", null)]);
  assert.equal(await polling.condition(), "restricted");
  f.wall = START + HOUR;
  assert.equal(await polling.observe(), "restricted");
  assert.deepEqual(f.notifier.delivered, ["restricted", "restricted"]);
  assert.deepEqual(await state(f), [row("restricted", START + HOUR, "restricted", "restricted")]);
  assert.equal(await polling.observe(), "restricted");
  assert.deepEqual(f.notifier.attempts, ["restricted", "restricted"]);
});

// R7 — Background runs

test("the_background_task_triggers_a_budgeted_background_run", async (t) => {
  const f = await fixture(t);
  assert.equal(BACKGROUND_BUDGET_MS, 480_000);
  assert.equal(await f.polling.runTask(), "success");
  assert.deepEqual(f.sync.triggers, [BACKGROUND]);
  // Whatever the platform and storage state, the task only triggers.
  f.platform.restricted = true;
  f.sync.respond = () => Promise.resolve({ ...CHECKPOINTED, runSequence: 2 });
  assert.equal(await f.make({ database: brokenDatabase(new Error(`${SENTINEL} synthetic storage failure`)) }).runTask(), "success");
  assert.deepEqual(f.sync.triggers, [BACKGROUND, BACKGROUND]);
});

test("a_failed_or_throwing_run_reports_failed", async (t) => {
  const f = await fixture(t);
  const responses: (() => unknown)[] = [
    () => Promise.resolve({ outcome: "failed", failure: "storage-failed" }),
    () => Promise.resolve({ outcome: "failed", failure: "installation-key-unusable" }),
    () => Promise.reject(new Error(`${SENTINEL} synthetic run failure`)),
    () => Promise.reject(new TypeError(`${SENTINEL} synthetic caller error`)),
    () => { throw new Error(`${SENTINEL} synthetic synchronous failure`); },
    () => Promise.resolve(undefined),
    () => Promise.resolve({ outcome: "refused", failure: "retry-not-blocked" }),
    () => null,
  ];
  for (const respond of responses) {
    f.sync.respond = respond;
    assert.equal(await f.polling.runTask(), "failed");
  }
  assert.deepEqual(f.sync.triggers, responses.map(() => BACKGROUND));
});

// R8 — Platform failures

test("platform_errors_read_as_the_worst_status", async (t) => {
  const f = await fixture(t);
  const error = new Error(`${SENTINEL} synthetic platform failure`);
  const unreadable: Script[] = [
    { throws: error }, { throws: error, synchronously: true }, { returns: "true" }, { returns: 1 }, { returns: undefined }, { returns: null },
  ];
  for (const script of unreadable) {
    f.platform.next("isRegistered", script);
    assert.equal(await f.polling.observe(), "unregistered", JSON.stringify(script));
  }
  const unknown: Script[] = [
    { throws: error }, { throws: error, synchronously: true }, { returns: "unknown" }, { returns: "Available" }, { returns: undefined }, { returns: true },
  ];
  for (const script of unknown) {
    f.platform.next("restriction", script);
    assert.equal(await f.polling.observe(), "restricted", JSON.stringify(script));
  }
  // The observations still complete: recorded, and notified at each onset.
  assert.deepEqual(await state(f), [row("restricted", START, "restricted", "restricted")]);
  assert.deepEqual(f.notifier.attempts, ["unregistered", "restricted"]);
  // An unreadable registration is not registered over, since registering replaces a pending request.
  f.platform.registered = false;
  f.platform.next("isRegistered", { throws: error });
  assert.deepEqual(await f.polling.ensureRegistered(), observed("unregistered", "unregistered"));
  assert.deepEqual(f.platform.registrations, []);
});

// R9 — Storage

test("storage_failures_keep_the_observation", async (t) => {
  const f = await fixture(t);
  f.platform.restricted = true;
  const before = await contents(f.database);
  const writes = f.make({ database: failingWrites(f.database) });
  assert.equal(await writes.observe(), "restricted");
  assert.deepEqual(await writes.ensureRegistered(), { outcome: "storage-failed" });
  assert.equal(await writes.condition(), null);
  // A storage seam that throws TypeError is still a storage failure, not a caller error.
  for (const failure of [new Error(`${SENTINEL} synthetic storage failure`), new TypeError(`${SENTINEL} synthetic storage value`)]) {
    const broken = f.make({ database: brokenDatabase(failure) });
    assert.equal(await broken.observe(), "restricted");
    assert.deepEqual(await broken.ensureRegistered(), { outcome: "storage-failed" });
    assert.deepEqual(await broken.condition(), { outcome: "storage-failed" });
  }
  assert.deepEqual(f.notifier.calls, []);
  assert.deepEqual(await contents(f.database), before);
  // Once storage recovers, the onset is recorded and notified.
  assert.equal(await f.polling.observe(), "restricted");
  assert.deepEqual(f.notifier.attempts, ["restricted"]);
});

test("observations_wait_for_the_storage_lane", async (t) => {
  const f = await fixture(t);
  const operations: string[] = [];
  const database = logging(f.database, operations);
  const polling = f.make({ database });
  const lane = storageLane(database);
  // While coordinate-sender-sync@1 holds the shared lane, nothing touches storage.
  let release!: () => void;
  let acquired!: () => void;
  const holding = new Promise<void>((resolve) => { acquired = resolve; });
  const held = lane(() => new Promise<void>((resolve) => { release = resolve; acquired(); }));
  await holding;
  f.platform.restricted = true;
  const ensured = polling.ensureRegistered();
  const read = polling.condition();
  try {
    await settle();
    assert.deepEqual(operations, []);
  } finally { release(); }
  await held;
  assert.deepEqual(await ensured, observed("restricted", "restricted"));
  assert.ok([null, "restricted"].includes(await read as string | null));
  assert.ok(operations.length > 0);
  // A pending notification never holds the lane.
  f.platform.restricted = false;
  assert.equal(await polling.observe(), "available");
  let open!: () => void;
  f.notifier.gate = new Promise<void>((resolve) => { open = resolve; });
  f.platform.restricted = true;
  const entered = f.notifier.arrival();
  const restricted = polling.observe();
  try {
    await entered;
    assert.deepEqual(f.notifier.attempts, ["restricted", "restricted"]);
    assert.equal(await Promise.race([lane(async () => "free"), settle(1_000).then(() => "held")]), "free");
  } finally { open(); }
  assert.equal(await restricted, "restricted");
});

// R10 — The Sender schema migration

const DDL = `CREATE TABLE scheduling_state (
  singleton INTEGER PRIMARY KEY NOT NULL CHECK (singleton = 1),
  observed_status TEXT NOT NULL CHECK (observed_status IN ('available', 'restricted', 'unregistered')),
  observed_at_ms INTEGER NOT NULL,
  action_condition TEXT CHECK (action_condition IN ('restricted', 'unregistered')),
  notified_condition TEXT CHECK (notified_condition IN ('restricted', 'unregistered')),
  CHECK (action_condition IS (CASE WHEN observed_status = 'available' THEN NULL ELSE observed_status END)),
  CHECK (notified_condition IS NULL OR notified_condition IS action_condition)) STRICT`;
/** SQL as its tokens, so whitespace between tokens never matters and literals keep their contents. */
const normalize = (sql: DatabaseValue): DatabaseValue =>
  typeof sql === "string" ? (sql.match(/'(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_]*|[0-9]+|\S/g) ?? []).join(" ") : sql;

async function schemaOf(database: SenderDatabase): Promise<DatabaseRow[]> {
  return (await database.query("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"))
    .map((entry) => ({ ...entry, sql: normalize(entry.sql) }));
}

test("migration_creates_version_six", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const database = await openNodeDatabase(join(directory, "synthetic.sqlite"));
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.deepEqual(await migrate(database, MIGRATIONS.slice(0, 5)), { status: "ready", version: 5 });
  const before = await schemaOf(database);
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 6 });
  const after = await schemaOf(database);
  const key = (entry: DatabaseRow) => `${entry.type}:${entry.name}`;
  const kept = new Set(before.map(key));
  assert.deepEqual(after.filter((entry) => kept.has(key(entry))), before);
  const added = after.filter((entry) => !kept.has(key(entry)));
  assert.deepEqual(added, [{ type: "table", name: "scheduling_state", tbl_name: "scheduling_state", sql: normalize(DDL) }]);
  assert.deepEqual(await database.query("SELECT * FROM scheduling_state"), []);
});

test("migration_is_idempotent", async (t) => {
  const f = await fixture(t);
  await f.polling.observe();
  const before = await contents(f.database);
  assert.deepEqual(await migrate(f.database, MIGRATIONS), { status: "ready", version: 6 });
  assert.deepEqual(await contents(f.database), before);
  assert.equal(schedulingMigration.version, 6);
});

// R11 — Boundary

/** A file's code as the TypeScript parser sees it, so comments never count. */
interface Code {
  /** Static imports and re-exports with the value names they bring in, by original name, and dynamic imports or requires. */
  readonly dependencies: { readonly specifier: string; readonly values: readonly string[]; readonly dynamic: boolean }[];
  /** Every identifier, property names included. */
  readonly identifiers: string[];
  /** Identifiers that name a binding or global rather than a property. */
  readonly references: string[];
  /** The text of every string and template literal. */
  readonly strings: string[];
}

function parse(file: string): Code {
  const source = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const code: Code = { dependencies: [], identifiers: [], references: [], strings: [] };
  const literal = (node: ts.Node | undefined) => node !== undefined && ts.isStringLiteralLike(node) ? node.text : "<computed>";
  /** An expression without parentheses, type assertions, or non-null assertions around it. */
  const bare = (node: ts.Expression): ts.Expression =>
    ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isTypeAssertionExpression(node) ||
    ts.isNonNullExpression(node) || ts.isSatisfiesExpression(node) ? bare(node.expression) : node;
  const isGlobalThis = (node: ts.Expression) => { const inner = bare(node); return ts.isIdentifier(inner) && inner.text === "globalThis"; };
  const named = (elements: readonly (ts.ImportSpecifier | ts.ExportSpecifier)[]) =>
    elements.filter((element) => !element.isTypeOnly).map((element) => (element.propertyName ?? element.name).text);
  const property = (node: ts.Identifier): boolean => {
    const parent = node.parent;
    if (ts.isPropertyAccessExpression(parent) && parent.name === node) {
      return !isGlobalThis(parent.expression);
    }
    if ((ts.isPropertyAssignment(parent) || ts.isPropertyDeclaration(parent) || ts.isPropertySignature(parent) || ts.isMethodDeclaration(parent) ||
         ts.isMethodSignature(parent) || ts.isGetAccessor(parent) || ts.isSetAccessor(parent)) && parent.name === node) return true;
    if (ts.isQualifiedName(parent) && parent.right === node) return true;
    return (ts.isBindingElement(parent) || ts.isImportSpecifier(parent) || ts.isExportSpecifier(parent)) && parent.propertyName === node;
  };
  const visit = (node: ts.Node): void => {
    if (ts.isImportDeclaration(node)) {
      const clause = node.importClause;
      const values: string[] = [];
      if (clause === undefined) values.push("<side effect>");
      else if (!clause.isTypeOnly) {
        if (clause.name) values.push("default");
        if (clause.namedBindings && ts.isNamespaceImport(clause.namedBindings)) values.push("*");
        if (clause.namedBindings && ts.isNamedImports(clause.namedBindings)) values.push(...named(clause.namedBindings.elements));
      }
      code.dependencies.push({ specifier: literal(node.moduleSpecifier), values, dynamic: false });
    } else if (ts.isExportDeclaration(node) && node.moduleSpecifier !== undefined) {
      const values = node.isTypeOnly ? [] : node.exportClause && ts.isNamedExports(node.exportClause) ? named(node.exportClause.elements) : ["*"];
      code.dependencies.push({ specifier: literal(node.moduleSpecifier), values, dynamic: false });
    } else if (ts.isImportEqualsDeclaration(node)) {
      const reference = node.moduleReference;
      code.dependencies.push({ specifier: ts.isExternalModuleReference(reference) ? literal(reference.expression) : "<alias>", values: ["="], dynamic: true });
    } else if (ts.isCallExpression(node) &&
               (node.expression.kind === ts.SyntaxKind.ImportKeyword || (ts.isIdentifier(node.expression) && node.expression.text === "require"))) {
      code.dependencies.push({ specifier: literal(node.arguments[0]), values: ["*"], dynamic: true });
    } else if (ts.isElementAccessExpression(node) && isGlobalThis(node.expression)) {
      code.references.push("globalThis[]");
    }
    if (ts.isIdentifier(node)) {
      code.identifiers.push(node.text);
      if (!property(node)) code.references.push(node.text);
    }
    if (ts.isStringLiteralLike(node) || ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)) code.strings.push(node.text);
    ts.forEachChild(node, visit);
  };
  visit(source);
  return code;
}

/** Every file the unit owns: the module and any helper beside it. */
function moduleFiles(): string[] {
  const files = readdirSync(MODULE_DIRECTORY).filter((name) => name.endsWith(".ts")).map((name) => join(MODULE_DIRECTORY, name));
  assert.ok(files.includes(MODULE));
  return files;
}

test("no_battery_exemption_or_foreground_service", async (t) => {
  const f = await fixture(t);
  // Through every entry point and state, nothing outside the seams is called.
  f.platform.registered = false;
  await f.polling.ensureRegistered();
  f.platform.restricted = true;
  f.notifier.failures = 1;
  await f.polling.observe();
  await f.polling.ensureRegistered();
  f.platform.registered = false;
  f.platform.next("register", { throws: new Error(`${SENTINEL} synthetic registration failure`) });
  await f.polling.ensureRegistered();
  await f.polling.runTask();
  await f.polling.condition();
  assert.deepEqual([...new Set(f.platform.calls)].sort(), ["isRegistered", "register", "restriction"]);
  // Neither the module's code nor the app config's names either permission or starts a foreground service; comments do not count.
  const forbidden = /IGNORE_?BATTERY_?OPTIMI[SZ]ATION|FOREGROUND_?SERVICE|startForeground/i;
  for (const file of [...moduleFiles(), APP_CONFIG]) {
    const code = parse(file);
    for (const name of [...code.identifiers, ...code.strings]) assert.doesNotMatch(name, forbidden, file);
  }
});

test("the_module_stays_inside_its_boundary", async (t) => {
  const NODE_GLOBALS = new Set(["process", "Buffer", "require", "global", "__dirname", "__filename", "globalThis[]"]);
  const COORDINATOR = /^(?:createSenderSync|requestRetry|typeBlock|syncMigration|serializedDatabase|storageDatabase)$/;
  for (const file of moduleFiles()) {
    const code = parse(file);
    for (const table of OTHER_TABLES) {
      for (const text of code.strings) assert.doesNotMatch(text, new RegExp(`\\b${table}\\b`), `${file}: ${table}`);
    }
    for (const { specifier, values, dynamic } of code.dependencies) {
      // Only static imports of relative modules: no Node built-in and no package.
      assert.equal(dynamic, false, `${file}: ${specifier}`);
      assert.match(specifier, /^\.{1,2}\//, file);
      if (values.length === 0) continue;
      // Values come from beside the module, the storage seam, or the storage lane it shares with the coordinator.
      if (/^\.\/|^\.\.\/storage\//.test(specifier)) continue;
      assert.equal(specifier, "../sync/sync-storage.ts", file);
      assert.deepEqual(values, ["storageLane"], file);
    }
    assert.deepEqual(code.references.filter((name) => NODE_GLOBALS.has(name)), [], file);
    assert.deepEqual([...code.identifiers, ...code.strings].filter((name) => COORDINATOR.test(name)), [], file);
  }
  // At run time, the coordinator is reached only through trigger.
  const f = await fixture(t);
  const touched = new Set<string | symbol>();
  const sync = new Proxy(f.sync, { get(target, property, receiver) { touched.add(property); return Reflect.get(target, property, receiver); } });
  const polling = f.make({ sync });
  f.platform.registered = false;
  await polling.ensureRegistered();
  await polling.observe();
  await polling.runTask();
  await polling.condition();
  assert.deepEqual([...touched], ["trigger"]);
  assert.deepEqual(f.sync.triggers, [BACKGROUND]);
});
