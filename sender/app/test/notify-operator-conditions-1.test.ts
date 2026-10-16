// Acceptance tests for notify-operator-conditions@1.
// Requirement numbers refer to packs/notify-operator-conditions-1.spec.md. Storage is the 3ug node:sqlite
// SenderDatabase substitute. The notifier is a synthetic fake defined here, and observations are built
// with the published RunSummary, EnrollmentStatus, and InstallationKeyStatus types.
import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import type { DatabaseSync } from "node:sqlite";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import type { TestContext } from "node:test";
import ts from "typescript";
import { deliveryMigration } from "../src/delivery/send-outbox-message.ts";
import { enrollmentMigration } from "../src/enrollment/manage-sender-enrollment.ts";
import type { EnrollmentPhase, EnrollmentStatus } from "../src/enrollment/manage-sender-enrollment.ts";
import type { InstallationKeyStatus } from "../src/installation-cipher.ts";
import {
  OPERATOR_CONDITIONS, createOperatorConditions, operatorConditionsMigration,
} from "../src/notifications/notify-operator-conditions.ts";
import type {
  ConditionNotifier, ConditionObservation, ConditionRecord, OperatorCondition, OperatorConditions, OperatorConditionsOptions,
} from "../src/notifications/notify-operator-conditions.ts";
import { outboxMigration } from "../src/outbox/stage-outbox-batch.ts";
import { checkpointMigration } from "../src/outbox/stage-sync-checkpoint.ts";
import type { OutcomeReason, RunSummary, TypeOutcome } from "../src/outbox/stage-sync-checkpoint.ts";
import { completionMigration } from "../src/outbox/stage-sync-completion.ts";
import { schedulingMigration } from "../src/scheduling/schedule-background-polling.ts";
import { snapshotAttemptsMigration } from "../src/snapshot/begin-initial-snapshot.ts";
import type { DatabaseRow, DatabaseValue, SenderDatabase } from "../src/storage/database.ts";
import { migrate } from "../src/storage/migrations.ts";
import { storageLane } from "../src/storage/storage-lane.ts";
import { syncMigration } from "../src/sync/coordinate-sender-sync.ts";
import { openNodeDatabase } from "./support/node-database.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const MODULE = join(HERE, "../src/notifications/notify-operator-conditions.ts");
const MODULE_DIRECTORY = dirname(MODULE);
const SOURCE_STORAGE = join(HERE, "../src/storage");
const SENTINEL = "SENTINEL";
const MIGRATIONS = [
  snapshotAttemptsMigration, outboxMigration, completionMigration, checkpointMigration, syncMigration, schedulingMigration,
  deliveryMigration, enrollmentMigration, operatorConditionsMigration,
];
const NOW = Date.UTC(2026, 9, 16, 9, 0, 0, 0);
const IDENTITY = "00000000-0000-4000-8000-0000000000e1";
const ALIAS = `activity-tracker-portal.installation.${IDENTITY}`;
const PORTAL_URL = "http://portal.synthetic.invalid:8443";
const settle = (ms = 20) => new Promise<void>((resolve) => { setTimeout(resolve, ms); });
const RECORD_TYPES = ["active-calories-burned", "distance", "heart-rate", "sleep-session", "steps", "weight"];

// Observations, built with the published types.

type Entry = readonly [TypeOutcome, OutcomeReason?];

function summary(...entries: Entry[]): RunSummary {
  return {
    runSequence: 1, trigger: "background", startedAtMs: NOW - 60_000, endedAtMs: NOW - 30_000,
    backgroundScheduling: "available",
    recordTypes: entries.map(([outcome, reason], index) => ({
      recordType: RECORD_TYPES[index % RECORD_TYPES.length], outcome, ...(reason === undefined ? {} : { reason }), sourceDataStaged: false,
    })),
  };
}

function enrollment(phase: EnrollmentPhase, finalSequence: number | null = null): EnrollmentStatus {
  return {
    installationId: IDENTITY, fingerprint: "0".repeat(64), keyAlias: ALIAS, portalUrl: PORTAL_URL, phase,
    refusal: phase === "refused" ? "enrollment-refused" : null, confirmed: phase === "enrolled" || phase === "draining",
    stagingContext: null, deliveryContext: { installationId: IDENTITY, keyAlias: ALIAS, portalUrl: PORTAL_URL }, finalSequence,
  };
}

const ENROLLED = enrollment("enrolled");
const observation = (overrides: Partial<ConditionObservation> = {}): ConditionObservation => ({ enrollment: ENROLLED, ...overrides });

// Seams

/** Records each call; `failures` makes the next calls throw, and `gate` holds each call until opened. */
class FakeNotifier implements ConditionNotifier {
  readonly calls: unknown[][] = [];
  failures = 0;
  synchronously = false;
  gate: Promise<void> | null = null;
  onNotify: (() => void) | null = null;
  private arrivals: (() => void)[] = [];

  arrival(): Promise<void> {
    return new Promise<void>((resolve) => { this.arrivals.push(resolve); });
  }

  notify(...args: [OperatorCondition]): Promise<void> {
    this.calls.push(args);
    this.onNotify?.();
    for (const arrive of this.arrivals.splice(0)) arrive();
    if (this.failures > 0) {
      this.failures--;
      const error = new Error(`${SENTINEL} synthetic notification failure`);
      if (this.synchronously) throw error;
      return Promise.reject(error);
    }
    return this.gate ?? Promise.resolve();
  }

  names(): unknown[] { return this.calls.map((call) => call[0]); }
}

async function fixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const path = join(directory, "synthetic.sqlite");
  let database = await openNodeDatabase(path);
  t.after(async () => {
    try { await database.close(); } catch { /* a test may leave the old process's database closed */ }
    rmSync(directory, { recursive: true, force: true });
  });
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 9 });
  const events: string[] = [];
  const f = {
    events,
    notifier: new FakeNotifier(),
    wall: NOW,
    clockReads: 0,
    now: (): number => { f.clockReads++; events.push("now"); return f.wall; },
    get database(): SenderDatabase { return database; },
    make(overrides: Partial<OperatorConditionsOptions> = {}): OperatorConditions {
      return createOperatorConditions({ database, notifier: f.notifier, now: f.now, ...overrides });
    },
    /** A second connection whose writes SQLite rejects while failing; it starts failing. */
    async writeFailing(): Promise<{ readonly database: SenderDatabase; readonly failing: (on: boolean) => void }> {
      let handle!: DatabaseSync;
      const opened = await openNodeDatabase(path, (raw) => { handle = raw; });
      t.after(async () => { try { await opened.close(); } catch { /* already closed */ } });
      const failing = (on: boolean) => { handle.exec(`PRAGMA query_only = ${on ? "ON" : "OFF"}`); };
      failing(true);
      return { database: opened, failing };
    },
    /** A new process on the same file; the old instance is never used again. */
    async reopen(): Promise<void> { await database.close(); database = await openNodeDatabase(path); f.conditions = f.make(); },
    conditions: undefined as unknown as OperatorConditions,
  };
  f.conditions = f.make();
  return f;
}
type Fixture = Awaited<ReturnType<typeof fixture>>;

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

async function rows(database: SenderDatabase): Promise<DatabaseRow[]> {
  return database.query("SELECT * FROM operator_conditions ORDER BY condition");
}

const record = (condition: OperatorCondition, firstSeenMs: number, lastSeenMs: number, notified: boolean): ConditionRecord =>
  ({ condition, firstSeenMs, lastSeenMs, notified });

/** Every operation fails with `error`. */
function brokenDatabase(error: unknown): SenderDatabase {
  const fail = () => Promise.reject(error);
  return { execute: fail, query: fail, transaction: fail, close: fail };
}

const MUTATION = /^\s*(?:WITH\b[\s\S]*?\b)?(INSERT|UPDATE|DELETE|REPLACE)\b/i;

/** The real database, except that its `mutation`th write, or every commit, fails. */
function failingWrites(database: SenderDatabase, rule: { mutation?: number; commit?: boolean }): SenderDatabase {
  let mutations = 0;
  const guard = (sql: string) => {
    if (MUTATION.test(sql) && ++mutations === rule.mutation) throw new Error(`${SENTINEL} synthetic write failure`);
  };
  const wrap = (inner: SenderDatabase): SenderDatabase => ({
    async execute(sql, parameters) { guard(sql); return inner.execute(sql, parameters); },
    async query(sql, parameters) { guard(sql); return inner.query(sql, parameters); },
    transaction<T>(fn: (transaction: SenderDatabase) => T | Promise<T>): Promise<T> {
      return inner.transaction(async (transaction) => {
        const result = await fn(wrap(transaction));
        if (rule.commit) throw new Error(`${SENTINEL} synthetic commit failure`);
        return result;
      });
    },
    close: () => inner.close(),
  });
  return wrap(database);
}

/** Logs every operation, including those inside transactions, to `operations` and `events`. */
function logging(database: SenderDatabase, operations: string[], events?: string[]): SenderDatabase {
  const log = (entry: string) => { operations.push(entry); events?.push(`storage:${entry}`); };
  const wrap = (inner: SenderDatabase): SenderDatabase => ({
    execute: (sql, parameters) => { log(sql); return inner.execute(sql, parameters); },
    query: (sql, parameters) => { log(sql); return inner.query(sql, parameters); },
    transaction: (fn) => { log("transaction"); return inner.transaction((transaction) => fn(wrap(transaction))); },
    close: () => inner.close(),
  });
  return wrap(database);
}

const ALL_RUN = summary(
  ["blocked", "permission-missing"], ["deferred", "screen-lock-removed"], ["deferred", "storage-pressure"],
  ["blocked", "record-too-large"], ["progressed", "projection-refusal"],
);

// R2 — Caller errors

test("malformed_options_and_observations_are_caller_errors", async (t) => {
  const f = await fixture(t);
  const before = await contents(f.database);
  const operations: string[] = [];
  const database = logging(f.database, operations);
  const without = <T extends object>(value: T, name: string): T => {
    const copy: Record<string, unknown> = {};
    for (const key of Object.keys(value)) if (key !== name) copy[key] = (value as Record<string, unknown>)[key];
    return copy as T;
  };
  const options: unknown[] = [
    null, 7, "options",
    { database: undefined }, { database: without(database, "transaction") }, { database: without(database, "query") },
    { notifier: {} }, { notifier: { notify: "notify" } }, { now: NOW }, { now: undefined },
  ];
  for (const entry of options) {
    const value = entry === null || typeof entry !== "object" ? entry : { database, notifier: f.notifier, now: f.now, ...entry };
    assert.throws(() => createOperatorConditions(value as OperatorConditionsOptions), TypeError, JSON.stringify(entry));
  }
  const conditions = f.make({ database });
  const observations: unknown[] = [
    undefined, null, 7, "observation", [], {}, { enrollment: undefined }, { enrollment: { phase: 1 } }, { enrollment: [] },
    { enrollment: null, summary: null }, { enrollment: null, summary: {} }, { enrollment: null, summary: { recordTypes: "steps" } },
    { enrollment: null, summary: { recordTypes: [null] } }, { enrollment: null, summary: { recordTypes: [{ outcome: 1 }] } },
    { enrollment: null, summary: { recordTypes: [{ outcome: "blocked", reason: 5 }] } },
    { enrollment: null, keyStatus: "gone" }, { enrollment: null, keyStatus: 1 }, { enrollment: null, keyStatus: null },
  ];
  for (const value of observations) {
    await assert.rejects(conditions.observe(value as ConditionObservation), TypeError, JSON.stringify(value));
  }
  assert.equal(f.clockReads, 0);
  for (const reading of [Number.NaN, 1.5, 2 ** 53, -Infinity, "1", null, undefined, 1n]) {
    let reads = 0;
    const instance = f.make({ database, now: () => { reads++; return reading as unknown as number; } });
    await assert.rejects(instance.observe(observation({ summary: ALL_RUN })), TypeError, String(reading));
    assert.equal(reads, 1);
  }
  assert.deepEqual(operations, []);
  assert.deepEqual(f.notifier.calls, []);
  assert.deepEqual(await contents(f.database), before);
  // A valid reading is taken once, before any other call.
  f.events.length = 0;
  const ordered = f.make({ database: logging(f.database, [], f.events) });
  assert.equal((await ordered.observe(observation({ summary: ALL_RUN }))).outcome, "observed");
  assert.equal(f.events[0], "now");
  assert.equal(f.events.filter((event) => event === "now").length, 1);
});

// R3 — Conditions from a Polling Run

test("run_conditions_follow_the_latest_summary", async (t) => {
  const f = await fixture(t);
  assert.deepEqual(OPERATOR_CONDITIONS, [
    "permission-missing", "screen-lock-removed", "storage-pressure", "record-too-large", "projection-refused",
    "installation-key-unusable", "enrollment-refused", "drain-complete",
  ]);
  assert.deepEqual(await f.conditions.observe(observation({ summary: ALL_RUN })), {
    outcome: "observed",
    conditions: ["permission-missing", "screen-lock-removed", "storage-pressure", "record-too-large", "projection-refused"],
  });
  assert.deepEqual(f.notifier.names(), ["permission-missing", "screen-lock-removed", "storage-pressure", "record-too-large", "projection-refused"]);
  // A later run showing one of them keeps it, updates its last seen time, and clears the rest.
  f.wall = NOW + 3_600_000;
  assert.deepEqual(await f.conditions.observe(observation({ summary: summary(["completed"], ["not-permitted"]) })),
    { outcome: "observed", conditions: ["permission-missing"] });
  assert.deepEqual(await f.conditions.conditions(), [record("permission-missing", NOW, NOW + 3_600_000, true)]);
  // Without a summary the run conditions keep their state, and their last seen time.
  f.wall = NOW + 7_200_000;
  assert.deepEqual(await f.conditions.observe(observation()), { outcome: "observed", conditions: ["permission-missing"] });
  assert.deepEqual(await f.conditions.conditions(), [record("permission-missing", NOW, NOW + 3_600_000, true)]);
  // Either form of a missing permission is one condition, and a run that shows none clears it.
  assert.deepEqual(await f.conditions.observe(observation({ summary: summary(["not-permitted", "permission-missing"], ["blocked", "permission-missing"]) })),
    { outcome: "observed", conditions: ["permission-missing"] });
  assert.deepEqual(await f.conditions.observe(observation({ summary: summary() })), { outcome: "observed", conditions: [] });
  assert.deepEqual(await rows(f.database), []);
  assert.equal(f.notifier.calls.length, 5);
  // Each reason alone makes exactly its condition.
  const cases: [Entry, OperatorCondition][] = [
    [["blocked", "permission-missing"], "permission-missing"],
    [["not-permitted"], "permission-missing"],
    [["deferred", "screen-lock-removed"], "screen-lock-removed"],
    [["interrupted", "storage-pressure"], "storage-pressure"],
    [["progressed", "record-too-large"], "record-too-large"],
    [["blocked", "projection-refusal"], "projection-refused"],
  ];
  for (const [entry, condition] of cases) {
    const g = await fixture(t);
    assert.deepEqual(await g.conditions.observe(observation({ summary: summary(entry) })), { outcome: "observed", conditions: [condition] },
      JSON.stringify(entry));
  }
});

test("transient_and_elsewhere_notified_reasons_make_no_condition", async (t) => {
  const f = await fixture(t);
  const before = await contents(f.database);
  const quiet = summary(
    ["interrupted", "time-budget-reached"], ["progressed", "changes-token-expired"], ["failed", "read-failed"],
    ["deferred", "delivery-blocked"], ["deferred", "collection-disabled"], ["progressed"], ["completed"], ["no-change"],
    ["not-due"], ["failed"], ["deferred"], ["blocked"],
  );
  assert.deepEqual(await f.conditions.observe(observation({ summary: quiet, keyStatus: "unavailable" })), { outcome: "observed", conditions: [] });
  assert.deepEqual(f.notifier.calls, []);
  assert.deepEqual(await contents(f.database), before);
  // They also clear what an earlier run showed.
  await f.conditions.observe(observation({ summary: ALL_RUN }));
  assert.deepEqual(await f.conditions.observe(observation({ summary: quiet })), { outcome: "observed", conditions: [] });
  assert.deepEqual(await contents(f.database), before);
});

// R4 — Conditions from enrollment and the key

test("lifecycle_and_key_conditions_follow_each_observation", async (t) => {
  const f = await fixture(t);
  const observe = async (value: ConditionObservation) => {
    const result = await f.conditions.observe(value);
    assert.equal(result.outcome, "observed");
    return (result as { conditions: readonly OperatorCondition[] }).conditions;
  };
  assert.deepEqual(await observe({ enrollment: null, keyStatus: "missing" }), []);
  assert.deepEqual(await observe({ enrollment: enrollment("refused"), keyStatus: "missing" }), ["enrollment-refused"]);
  assert.deepEqual(await observe({ enrollment: enrollment("draining") }), []);
  assert.deepEqual(await observe({ enrollment: enrollment("draining", 0) }), ["drain-complete"]);
  assert.deepEqual(await observe({ enrollment: enrollment("draining", 7) }), ["drain-complete"]);
  assert.deepEqual(await observe({ enrollment: enrollment("confirming"), keyStatus: "missing" }), ["installation-key-unusable"]);
  f.wall = NOW + 1_000;
  assert.deepEqual(await observe({ enrollment: ENROLLED, keyStatus: "unusable" }), ["installation-key-unusable"]);
  // An unavailable Keystore, or no key read, keeps the condition and its last seen time.
  f.wall = NOW + 2_000;
  assert.deepEqual(await observe({ enrollment: ENROLLED, keyStatus: "unavailable" }), ["installation-key-unusable"]);
  assert.deepEqual(await observe({ enrollment: ENROLLED }), ["installation-key-unusable"]);
  assert.deepEqual(await f.conditions.conditions(), [record("installation-key-unusable", NOW, NOW + 1_000, true)]);
  assert.deepEqual(await observe({ enrollment: ENROLLED, keyStatus: "present" }), []);
  assert.deepEqual(await observe({ enrollment: ENROLLED, keyStatus: "missing" }), ["installation-key-unusable"]);
  // A refused enrollment has no key condition, and no enrollment clears all three.
  assert.deepEqual(await observe({ enrollment: enrollment("refused") }), ["enrollment-refused"]);
  assert.deepEqual(await observe({ enrollment: enrollment("draining", 3), keyStatus: "unusable" }), ["installation-key-unusable", "drain-complete"]);
  assert.deepEqual(await observe({ enrollment: null }), []);
  assert.deepEqual(await rows(f.database), []);
  assert.deepEqual(f.notifier.names(), [
    "enrollment-refused", "drain-complete", "installation-key-unusable", "installation-key-unusable", "enrollment-refused",
    "installation-key-unusable", "drain-complete",
  ]);
  // Run and lifecycle conditions combine in condition order.
  assert.deepEqual(await observe({ enrollment: enrollment("draining", 4), keyStatus: "unusable", summary: ALL_RUN }), [...OPERATOR_CONDITIONS].filter((condition) => condition !== "enrollment-refused"));
});

// R5 — One notification per onset

test("each_onset_notifies_once", async (t) => {
  const f = await fixture(t);
  for (let index = 0; index < 4; index++) {
    f.wall = NOW + index * 1_000;
    assert.deepEqual(await f.conditions.observe(observation({ summary: summary(["blocked", "projection-refusal"]) })),
      { outcome: "observed", conditions: ["projection-refused"] });
  }
  assert.deepEqual(f.notifier.calls, [["projection-refused"]]);
  assert.deepEqual(await rows(f.database), [{ condition: "projection-refused", first_seen_ms: NOW, last_seen_ms: NOW + 3_000, notified: 1 }]);
  // A second condition's onset notifies only it.
  assert.equal((await f.conditions.observe(observation({ summary: summary(["blocked", "projection-refusal"], ["deferred", "storage-pressure"]) }))).outcome, "observed");
  assert.deepEqual(f.notifier.calls, [["projection-refused"], ["storage-pressure"]]);
  // A clock that runs backwards never moves a last seen time back.
  f.wall = NOW - 10_000;
  await f.conditions.observe(observation({ summary: summary(["blocked", "projection-refusal"], ["deferred", "storage-pressure"]) }));
  assert.deepEqual((await f.conditions.conditions() as ConditionRecord[]).map((entry) => entry.lastSeenMs), [NOW + 3_000, NOW + 3_000]);
});

test("a_failed_notification_is_retried", async (t) => {
  const f = await fixture(t);
  f.notifier.failures = 1;
  assert.deepEqual(await f.conditions.observe(observation({ summary: summary(["blocked", "permission-missing"], ["deferred", "storage-pressure"]) })),
    { outcome: "observed", conditions: ["permission-missing", "storage-pressure"] });
  // The first failed; the second was still notified.
  assert.deepEqual(f.notifier.calls, [["permission-missing"], ["storage-pressure"]]);
  assert.deepEqual(await f.conditions.conditions(), [
    record("permission-missing", NOW, NOW, false), record("storage-pressure", NOW, NOW, true),
  ]);
  // A synchronous throw is retried too, and then a success is recorded once.
  f.notifier.failures = 1;
  f.notifier.synchronously = true;
  f.wall = NOW + 1_000;
  await f.conditions.observe(observation());
  f.wall = NOW + 2_000;
  await f.conditions.observe(observation());
  await f.conditions.observe(observation());
  assert.deepEqual(f.notifier.names(), ["permission-missing", "storage-pressure", "permission-missing", "permission-missing"]);
  assert.deepEqual(await f.conditions.conditions(), [
    record("permission-missing", NOW, NOW, true), record("storage-pressure", NOW, NOW, true),
  ]);
});

test("a_cleared_condition_notifies_again_on_recurrence", async (t) => {
  const f = await fixture(t);
  const refusal = observation({ summary: summary(["blocked", "record-too-large"]) });
  const clear = observation({ summary: summary(["completed"]) });
  await f.conditions.observe(refusal);
  f.wall = NOW + 1_000;
  await f.conditions.observe(clear);
  f.wall = NOW + 2_000;
  await f.conditions.observe(refusal);
  assert.deepEqual(f.notifier.names(), ["record-too-large", "record-too-large"]);
  assert.deepEqual(await f.conditions.conditions(), [record("record-too-large", NOW + 2_000, NOW + 2_000, true)]);
  // A condition that clears before its notification succeeds is never notified for that onset.
  const g = await fixture(t);
  g.notifier.failures = 1;
  await g.conditions.observe(refusal);
  await g.conditions.observe(clear);
  await g.conditions.observe(clear);
  assert.deepEqual(g.notifier.names(), ["record-too-large"]);
  g.wall = NOW + 5_000;
  await g.conditions.observe(refusal);
  assert.deepEqual(g.notifier.names(), ["record-too-large", "record-too-large"]);
  assert.deepEqual(await g.conditions.conditions(), [record("record-too-large", NOW + 5_000, NOW + 5_000, true)]);
  // A late success for an earlier onset never marks a later onset notified.
  const h = await fixture(t);
  let open!: () => void;
  h.notifier.gate = new Promise<void>((resolve) => { open = resolve; });
  const entered = h.notifier.arrival();
  const first = h.conditions.observe(refusal);
  await entered;
  const second = h.make({ notifier: { async notify() { throw new Error(`${SENTINEL} synthetic notification failure`); } } });
  h.wall = NOW + 1_000;
  await second.observe(clear);
  h.wall = NOW + 2_000;
  await second.observe(refusal);
  open();
  assert.deepEqual(await first, { outcome: "observed", conditions: ["record-too-large"] });
  assert.deepEqual(await h.conditions.conditions(), [record("record-too-large", NOW + 2_000, NOW + 2_000, false)]);
  h.notifier.gate = null;
  await h.conditions.observe(observation());
  assert.deepEqual(await h.conditions.conditions(), [record("record-too-large", NOW + 2_000, NOW + 2_000, true)]);
  assert.equal(h.notifier.calls.length, 2);
});

// R6 — Persistence

test("conditions_survive_a_restart", async (t) => {
  const f = await fixture(t);
  f.notifier.failures = 1;
  await f.conditions.observe({ enrollment: enrollment("draining", 9), keyStatus: "unusable", summary: summary(["deferred", "screen-lock-removed"]) });
  assert.deepEqual(f.notifier.names(), ["screen-lock-removed", "installation-key-unusable", "drain-complete"]);
  await f.reopen();
  assert.deepEqual(await f.conditions.conditions(), [
    record("screen-lock-removed", NOW, NOW, false), record("installation-key-unusable", NOW, NOW, true), record("drain-complete", NOW, NOW, true),
  ]);
  // The new instance retries only the unnotified onset, and keeps the run condition without a summary.
  f.wall = NOW + 1_000;
  assert.deepEqual(await f.conditions.observe({ enrollment: enrollment("draining", 9) }),
    { outcome: "observed", conditions: ["screen-lock-removed", "installation-key-unusable", "drain-complete"] });
  assert.deepEqual(f.notifier.names(), ["screen-lock-removed", "installation-key-unusable", "drain-complete", "screen-lock-removed"]);
  await f.reopen();
  await f.conditions.observe({ enrollment: enrollment("draining", 9) });
  assert.equal(f.notifier.calls.length, 4);
  assert.deepEqual(await f.conditions.conditions(), [
    record("screen-lock-removed", NOW, NOW, true), record("installation-key-unusable", NOW, NOW, true), record("drain-complete", NOW, NOW + 1_000, true),
  ]);
});

// R7 — Reading conditions

test("conditions_are_read_exactly", async (t) => {
  const f = await fixture(t);
  assert.deepEqual(await f.conditions.conditions(), []);
  f.notifier.failures = 1;
  await f.conditions.observe({ enrollment: enrollment("refused"), summary: summary(["blocked", "projection-refusal"], ["not-permitted"]) });
  f.wall = NOW + 5_000;
  await f.conditions.observe({ enrollment: enrollment("refused"), summary: summary(["blocked", "projection-refusal"]) });
  const before = await contents(f.database);
  const expected = [record("projection-refused", NOW, NOW + 5_000, true), record("enrollment-refused", NOW, NOW + 5_000, true)];
  for (let read = 0; read < 3; read++) assert.deepEqual(await f.conditions.conditions(), expected);
  assert.deepEqual(await contents(f.database), before);
  assert.deepEqual(await f.make().conditions(), expected);
  for (const failure of [new Error(`${SENTINEL} synthetic storage failure`), new TypeError(`${SENTINEL} synthetic storage value`)]) {
    assert.deepEqual(await f.make({ database: brokenDatabase(failure) }).conditions(), { outcome: "storage-failed" });
  }
});

// R8 — Storage failures

test("storage_failures_change_nothing", async (t) => {
  const f = await fixture(t);
  await f.conditions.observe(observation({ summary: summary(["blocked", "permission-missing"]) }));
  const before = await contents(f.database);
  const calls = f.notifier.calls.length;
  const changed = observation({ summary: summary(["deferred", "storage-pressure"]) });
  for (const failure of [new Error(`${SENTINEL} synthetic storage failure`), new TypeError(`${SENTINEL} synthetic storage value`)]) {
    assert.deepEqual(await f.make({ database: brokenDatabase(failure) }).observe(changed), { outcome: "storage-failed" });
  }
  const { database } = await f.writeFailing();
  assert.deepEqual(await f.make({ database }).observe(changed), { outcome: "storage-failed" });
  assert.deepEqual(await f.make({ database: failingWrites(f.database, { commit: true }) }).observe(changed), { outcome: "storage-failed" });
  for (let mutation = 1; mutation <= 2; mutation++) {
    assert.deepEqual(await f.make({ database: failingWrites(f.database, { mutation }) }).observe(changed), { outcome: "storage-failed" }, String(mutation));
  }
  assert.equal(f.notifier.calls.length, calls);
  assert.deepEqual(await contents(f.database), before);
  assert.deepEqual(await f.conditions.observe(changed), { outcome: "observed", conditions: ["storage-pressure"] });
  assert.deepEqual(f.notifier.names(), ["permission-missing", "storage-pressure"]);
});

test("an_unrecorded_success_notifies_again", async (t) => {
  const f = await fixture(t);
  const { database, failing } = await f.writeFailing();
  failing(false);
  const instance = f.make({ database });
  // The onset is recorded, then writes fail once the notifier has been called.
  f.notifier.onNotify = () => { failing(true); };
  assert.deepEqual(await instance.observe(observation({ summary: summary(["blocked", "record-too-large"]) })), { outcome: "storage-failed" });
  assert.deepEqual(await f.conditions.conditions(), [record("record-too-large", NOW, NOW, false)]);
  f.notifier.onNotify = null;
  failing(false);
  assert.deepEqual(await instance.observe(observation()), { outcome: "observed", conditions: ["record-too-large"] });
  assert.deepEqual(f.notifier.names(), ["record-too-large", "record-too-large"]);
  assert.deepEqual(await f.conditions.conditions(), [record("record-too-large", NOW, NOW, true)]);
});

// R9 — Concurrency

test("observations_run_one_at_a_time", async (t) => {
  const f = await fixture(t);
  let open!: () => void;
  f.notifier.gate = new Promise<void>((resolve) => { open = resolve; });
  const entered = f.notifier.arrival();
  const value = observation({ summary: summary(["deferred", "screen-lock-removed"]) });
  const pending = [f.conditions.observe(value), f.conditions.observe(value), f.conditions.observe(value)];
  await entered;
  await settle();
  assert.equal(f.notifier.calls.length, 1);
  open();
  for (const result of await Promise.all(pending)) assert.deepEqual(result, { outcome: "observed", conditions: ["screen-lock-removed"] });
  assert.deepEqual(f.notifier.calls, [["screen-lock-removed"]]);
});

test("the_notifier_does_not_hold_the_storage_lane", async (t) => {
  const f = await fixture(t);
  const operations: string[] = [];
  const database = logging(f.database, operations);
  const instance = f.make({ database });
  const lane = storageLane(database);
  // While another unit holds the lane, an observation touches no storage.
  let release!: () => void;
  let acquired!: () => void;
  const holding = new Promise<void>((resolve) => { acquired = resolve; });
  const held = lane(() => new Promise<void>((resolve) => { release = resolve; acquired(); }));
  await holding;
  const observing = instance.observe(observation({ summary: summary(["blocked", "permission-missing"]) }));
  let read: unknown = "unsettled";
  const reading = instance.conditions().then((value) => { read = value; });
  try {
    await settle();
    assert.deepEqual(operations, []);
    assert.deepEqual(f.notifier.calls, []);
    assert.equal(read, "unsettled");
  } finally { release(); }
  await held;
  assert.equal((await observing).outcome, "observed");
  await reading;
  // While the notifier has not settled, the lane is free and conditions can be read.
  let open!: () => void;
  f.notifier.gate = new Promise<void>((resolve) => { open = resolve; });
  const entered = f.notifier.arrival();
  const pending = instance.observe(observation({ summary: summary(["blocked", "permission-missing"], ["deferred", "storage-pressure"]) }));
  try {
    await entered;
    assert.equal(await Promise.race([lane(async () => "free"), settle(1_000).then(() => "held")]), "free");
    assert.equal(await Promise.race([instance.conditions().then(() => "read"), settle(1_000).then(() => "held")]), "read");
  } finally { open(); }
  assert.deepEqual(await pending, { outcome: "observed", conditions: ["permission-missing", "storage-pressure"] });
});

// R10 — The Sender schema migration

const DDL = `CREATE TABLE operator_conditions (
  condition TEXT PRIMARY KEY NOT NULL CHECK (condition IN ('permission-missing', 'screen-lock-removed', 'storage-pressure',
    'record-too-large', 'projection-refused', 'installation-key-unusable', 'enrollment-refused', 'drain-complete')),
  first_seen_ms INTEGER NOT NULL,
  last_seen_ms INTEGER NOT NULL,
  notified INTEGER NOT NULL CHECK (notified IN (0, 1)),
  CHECK (last_seen_ms >= first_seen_ms)) STRICT`;
/** SQL as its tokens, so whitespace between tokens never matters and literals keep their contents. */
const normalize = (sql: DatabaseValue): DatabaseValue =>
  typeof sql === "string" ? (sql.match(/'(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_]*|[0-9]+|\S/g) ?? []).join(" ") : sql;

async function schemaOf(database: SenderDatabase): Promise<DatabaseRow[]> {
  return (await database.query("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"))
    .map((entry) => ({ ...entry, sql: normalize(entry.sql) }));
}

test("migration_creates_version_nine", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const database = await openNodeDatabase(join(directory, "synthetic.sqlite"));
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.deepEqual(await migrate(database, MIGRATIONS.slice(0, 8)), { status: "ready", version: 8 });
  const before = await schemaOf(database);
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 9 });
  const after = await schemaOf(database);
  const key = (entry: DatabaseRow) => `${entry.type}:${entry.name}`;
  const kept = new Set(before.map(key));
  assert.deepEqual(after.filter((entry) => kept.has(key(entry))), before);
  assert.deepEqual(after.filter((entry) => !kept.has(key(entry))), [
    { type: "index", name: "sqlite_autoindex_operator_conditions_1", tbl_name: "operator_conditions", sql: null },
    { type: "table", name: "operator_conditions", tbl_name: "operator_conditions", sql: normalize(DDL) },
  ]);
  assert.deepEqual(await database.query("SELECT * FROM operator_conditions"), []);
  assert.equal(operatorConditionsMigration.version, 9);
});

test("migration_is_idempotent", async (t) => {
  const f = await fixture(t);
  await f.conditions.observe({ enrollment: enrollment("draining", 2), summary: ALL_RUN });
  const before = await contents(f.database);
  assert.deepEqual(await migrate(f.database, MIGRATIONS), { status: "ready", version: 9 });
  assert.deepEqual(await contents(f.database), before);
});

// R11 — Boundary

/** A file's code as the TypeScript parser sees it, so comments never count. */
interface Code {
  readonly dependencies: { readonly specifier: string; readonly values: readonly string[]; readonly dynamic: boolean }[];
  readonly references: string[];
  readonly strings: string[];
}

function parse(file: string): Code {
  const source = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const code: Code = { dependencies: [], references: [], strings: [] };
  const literal = (node: ts.Node | undefined) => node !== undefined && ts.isStringLiteralLike(node) ? node.text : "<computed>";
  const named = (elements: readonly (ts.ImportSpecifier | ts.ExportSpecifier)[]) =>
    elements.filter((element) => !element.isTypeOnly).map((element) => (element.propertyName ?? element.name).text);
  const property = (node: ts.Identifier): boolean => {
    const parent = node.parent;
    if (ts.isPropertyAccessExpression(parent) && parent.name === node) return true;
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
      code.dependencies.push({ specifier: "<import equals>", values: ["="], dynamic: true });
    } else if (ts.isCallExpression(node) &&
               (node.expression.kind === ts.SyntaxKind.ImportKeyword || (ts.isIdentifier(node.expression) && node.expression.text === "require"))) {
      code.dependencies.push({ specifier: literal(node.arguments[0]), values: ["*"], dynamic: true });
    }
    if (ts.isIdentifier(node) && !property(node)) code.references.push(node.text);
    if (ts.isStringLiteralLike(node) || ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)) code.strings.push(node.text);
    ts.forEachChild(node, visit);
  };
  visit(source);
  return code;
}

test("the_module_stays_inside_its_boundary", async (t) => {
  const NETWORK = new Set(["fetch", "XMLHttpRequest", "WebSocket", "EventSource", "navigator", "process", "Buffer", "require", "global", "globalThis", "console"]);
  const f = await fixture(t);
  const others = (await f.database.query("SELECT name FROM sqlite_master WHERE type = 'table' AND name <> 'operator_conditions'"))
    .map((entry) => entry.name as string);
  assert.ok(others.length >= 15, others.join(", "));
  const files = (readdirSync(MODULE_DIRECTORY, { recursive: true }) as string[]).filter((name) => name.endsWith(".ts")).map((name) => join(MODULE_DIRECTORY, name));
  assert.ok(files.includes(MODULE));
  for (const file of files) {
    const code = parse(file);
    for (const table of others) {
      for (const string of code.strings) assert.doesNotMatch(string, new RegExp(`\\b${table}\\b`), `${file}: ${table}`);
    }
    for (const { specifier, values, dynamic } of code.dependencies) {
      assert.equal(dynamic, false, `${file}: ${specifier}`);
      // Type-only imports bring in no values; value imports stay in the module's directory or the storage seam.
      if (values.length === 0) continue;
      assert.match(specifier, /^\.{1,2}\//, `${file}: ${specifier}`);
      const target = resolve(dirname(file), specifier);
      if (!relative(MODULE_DIRECTORY, target).startsWith("..")) continue;
      assert.ok(!relative(SOURCE_STORAGE, target).startsWith(".."), `${file}: ${specifier}`);
    }
    assert.deepEqual(code.references.filter((name) => NETWORK.has(name)), [], file);
  }
  // At run time the notifier only ever receives one condition name.
  await f.conditions.observe({ enrollment: enrollment("draining", 5), keyStatus: "unusable", summary: ALL_RUN });
  await f.conditions.observe({ enrollment: enrollment("refused") });
  assert.ok(f.notifier.calls.length >= 8);
  for (const call of f.notifier.calls) {
    assert.equal(call.length, 1);
    assert.ok((OPERATOR_CONDITIONS as readonly unknown[]).includes(call[0]), JSON.stringify(call));
  }
});
