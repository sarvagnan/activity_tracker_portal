// Acceptance tests for coordinate-sender-sync@1.
// Requirement numbers refer to packs/coordinate-sender-sync-1.spec.md. Storage is the 3ug node:sqlite
// SenderDatabase substitute and the cipher is the 3ug NodeInstallationCipher. The reader and issuer
// seams are the synthetic fake in support/synthetic-sync-sources.ts; every other unit is the published one.
// The 3ug seam allows one storage operation at a time, so a test whose source hook reads storage runs
// the coordinator with a pool of one, leaving storage idle while the hook runs.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import type { TestContext } from "node:test";
import { decodeMessage } from "@activity-tracker-portal/ingest-protocol";
import type { Message } from "@activity-tracker-portal/ingest-protocol";
import type { CipherResult, InstallationCipher, InstallationKeyStatus } from "../src/installation-cipher.ts";
import { appendOutboxMessages, cycleProgress, lastOutboxSequence, outboxAad, outboxMessages, outboxMigration, stageOutboxBatch } from "../src/outbox/stage-outbox-batch.ts";
import type { OutboxMessage, StagingContext, StagingGeneration } from "../src/outbox/stage-outbox-batch.ts";
import { checkpointMigration, runCheckpoint } from "../src/outbox/stage-sync-checkpoint.ts";
import type { BackgroundScheduling } from "../src/outbox/stage-sync-checkpoint.ts";
import { completionMigration, cycleCompletion } from "../src/outbox/stage-sync-completion.ts";
import { beginInitialSnapshot, currentSnapshotAttempt, snapshotAttempt, snapshotAttemptsMigration } from "../src/snapshot/begin-initial-snapshot.ts";
import type { SnapshotAttempt } from "../src/snapshot/begin-initial-snapshot.ts";
import type { DatabaseRow, DatabaseValue, SenderDatabase } from "../src/storage/database.ts";
import { migrate } from "../src/storage/migrations.ts";
import { createSenderSync, syncMigration, typeBlock } from "../src/sync/coordinate-sender-sync.ts";
import type {
  ChangeReadRequest, RetryResult, RunResult, SenderSync, SenderSyncOptions, SnapshotReadRequest, SyncConditions, TriggerRequest,
} from "../src/sync/coordinate-sender-sync.ts";
import { openNodeDatabase } from "./support/node-database.ts";
import { NodeInstallationCipher } from "./support/node-installation-cipher.ts";
import { SyntheticClock, SyntheticSources, utf8 } from "./support/synthetic-sync-sources.ts";

const CONTRACT = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../../../governance/scopes/coordinate-sender-sync.1.json"), "utf8"));
const MODULE = join(dirname(fileURLToPath(import.meta.url)), "../src/sync/coordinate-sender-sync.ts");
const INSTALLATION = "00000000-0000-4000-8000-000000000001";
const OTHER_INSTALLATION = "00000000-0000-4000-8000-000000000002";
const KEY_ALIAS = "synthetic-installation-key";
const SENTINEL = "SENTINEL";
const MIGRATIONS = [snapshotAttemptsMigration, outboxMigration, completionMigration, checkpointMigration, syncMigration];
const START = Date.UTC(2026, 9, 15, 9, 0, 0, 0);
const DAY = 86_400_000;
const FOUR_TYPES = ["heart-rate", "sleep-session", "steps", "weight"];
const NAMED_TYPES = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"];
const CLEAR: SyncConditions = { collectionEnabled: true, secureLock: true, deliveryBlocked: false, storagePressure: false };
const REFUSAL = { outcome: "projection-refused", failure: "non-finite-value", path: "/record/samples/0/beats_per_minute" };
const NOT_PERMITTED = { outcome: "not-permitted", failure: "read-permission-missing" };
const text = (bytes: Uint8Array | null) => bytes === null ? null : new TextDecoder().decode(bytes);
const iso = (ms: number) => new Date(ms).toISOString();

type Checkpointed = Extract<RunResult, { outcome: "checkpointed" }>;
type Mutable<T> = { -readonly [K in keyof T]: T[K] };

function stagingContext(generation: Partial<StagingGeneration> = {}, overrides: Partial<StagingContext> = {}): StagingContext {
  return {
    installationId: INSTALLATION, keyAlias: KEY_ALIAS,
    generation: {
      generation: 6, maxRecordCount: 100, maxMessageBytes: 65_536,
      projectionSchemas: Object.fromEntries([...FOUR_TYPES, ...NAMED_TYPES].map((type) => [type, [1]])),
      ...generation,
    },
    ...overrides,
  };
}

async function fixture(t: TestContext, options: { types?: string[]; concurrency?: number } = {}) {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const path = join(directory, "synthetic.sqlite");
  let database = await openNodeDatabase(path);
  t.after(async () => {
    try { await database.close(); } catch { /* a test may leave a deliberately hung coordinator */ }
    rmSync(directory, { recursive: true, force: true });
  });
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 5 });
  const cipher = new NodeInstallationCipher();
  assert.equal((await cipher.create(KEY_ALIAS)).status, "ok");
  const clock = new SyntheticClock(START);
  const sources = new SyntheticSources(clock);
  const types = options.types ?? ["steps"];
  let uuid = 0;
  const f = {
    cipher, clock, sources, types,
    conditions: { ...CLEAR } as Mutable<SyncConditions>,
    scheduling: "available" as BackgroundScheduling,
    context: stagingContext(),
    implementationVersion: "synthetic-build-1",
    randomUUID: () => `aaaaaaaa-0000-4000-8000-${(++uuid).toString(16).padStart(12, "0")}`,
    get database() { return database; },
    options(overrides: Partial<SenderSyncOptions> = {}): SenderSyncOptions {
      return {
        database, cipher, sources, recordTypes: types,
        projection: { implementationVersion: f.implementationVersion, schemaVersions: Object.fromEntries(types.map((type) => [type, 1])) },
        context: () => f.context, conditions: () => ({ ...f.conditions }), backgroundScheduling: () => f.scheduling,
        ...(options.concurrency === undefined ? {} : { concurrency: options.concurrency }),
        now: clock.now, monotonicNow: clock.monotonicNow, randomUUID: f.randomUUID,
        ...overrides,
      };
    },
    make(overrides: Partial<SenderSyncOptions> = {}): SenderSync { return createSenderSync(f.options(overrides)); },
    /** A new process on the same file; the old coordinator never resumes. */
    async reopen() { await database.close(); database = await openNodeDatabase(path); f.sync = f.make(); return database; },
    sync: undefined as unknown as SenderSync,
  };
  f.sync = f.make();
  return f;
}
type Fixture = Awaited<ReturnType<typeof fixture>>;

async function ran(pending: Promise<RunResult | RetryResult>): Promise<Checkpointed> {
  const result = await pending;
  assert.equal(result.outcome, "checkpointed", JSON.stringify(result));
  return result as Checkpointed;
}

function outcomes(result: Checkpointed): Record<string, [string, string | null, boolean]> {
  return Object.fromEntries(result.summary.recordTypes.map((entry) => [entry.recordType, [entry.outcome, entry.reason ?? null, entry.sourceDataStaged]]));
}

const foreground = (f: Fixture) => ran(f.sync.trigger({ kind: "foreground" }));

/** Every Outbox row of an installation, opened with its own authenticated data and decoded. */
async function decoded(f: Fixture, installationId = INSTALLATION) {
  const rows = (await outboxMessages(f.database)).filter((row) => row.installationId === installationId);
  const result: { sequence: number; kind: string; message: Message }[] = [];
  for (const row of rows) {
    const plain = await f.cipher.open(KEY_ALIAS, row.sealed, outboxAad(row.installationId, row.sequence));
    assert.equal(plain.status, "ok");
    result.push({ sequence: row.sequence, kind: row.kind, message: decodeMessage((plain as { value: Uint8Array }).value) });
  }
  return result;
}

async function checkpointMessages(f: Fixture, installationId = INSTALLATION): Promise<Message[]> {
  return (await decoded(f, installationId)).filter((entry) => entry.kind === "sync-checkpoint").map((entry) => entry.message);
}

async function attemptOf(f: Fixture, recordType = "steps"): Promise<SnapshotAttempt> {
  const attempt = await currentSnapshotAttempt(f.database, recordType);
  assert.ok(attempt, `no attempt for ${recordType}`);
  return attempt;
}

async function ring(database: SenderDatabase): Promise<string[]> {
  return (await database.query("SELECT record_type FROM sync_types ORDER BY ring_order")).map((row) => row.record_type as string);
}

/**
 * The whole database: the schema version, every schema object, and every row of every table,
 * including sealed bytes, so no unexpected table or write can go unnoticed.
 */
async function contents(database: SenderDatabase) {
  const schema = await database.query("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name");
  const tables: Record<string, unknown> = {};
  for (const { name } of schema.filter((row) => row.type === "table")) {
    tables[name as string] = await database.query(`SELECT * FROM "${name as string}" ORDER BY rowid`);
  }
  return { version: await database.query("PRAGMA user_version"), schema, tables };
}

/** Every row of this unit's tables, with bytes as hex, for scanning. */
async function syncRows(database: SenderDatabase): Promise<string> {
  const tables = await database.query("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'sync\\_%' ESCAPE '\\' ORDER BY name");
  assert.ok(tables.length > 0);
  const dump: Record<string, DatabaseRow[]> = {};
  for (const { name } of tables) dump[name as string] = await database.query(`SELECT * FROM "${name as string}"`);
  return JSON.stringify(dump, (_, value) => value instanceof Uint8Array ? Buffer.from(value).toString("hex") : value);
}

/** Queue synthetic Outbox rows through the published primitive, as a competing writer would. */
async function queue(database: SenderDatabase, installationId: string, ...messages: [OutboxMessage["kind"], number][]) {
  const last = await lastOutboxSequence(database, installationId);
  assert.equal(await database.transaction((tx) => appendOutboxMessages(tx, installationId, last,
    messages.map(([kind, messageBytes], i) => ({ kind, sealed: utf8(`synthetic-sealed-${last + i}`), messageBytes })))), true);
}

/** Every top-level operation after `crashed` first holds never settles, as if the process died there. */
function crashing(database: SenderDatabase, crashed: () => Promise<boolean>) {
  let dead = false;
  let died!: () => void;
  const death = new Promise<void>((resolve) => { died = resolve; });
  const hang = () => new Promise<never>(() => {});
  const after = async <T>(value: T): Promise<T> => {
    if (!dead && await crashed()) { dead = true; died(); }
    return value;
  };
  const wrapped: SenderDatabase = {
    async execute(sql, parameters) { if (dead) return hang(); return after(await database.execute(sql, parameters)); },
    async query(sql, parameters) { if (dead) return hang(); return after(await database.query(sql, parameters)); },
    async transaction(fn) { if (dead) return hang(); return after(await database.transaction(fn)); },
    close: () => database.close(),
  };
  return { database: wrapped, death };
}

const MUTATION = /^\s*(?:INSERT|UPDATE|DELETE|REPLACE)\b/i;

/** Fail statements matching `pattern`, after letting the first `spare` top-level transactions through. */
function failingStatements(database: SenderDatabase, pattern: RegExp, spare = 0): SenderDatabase {
  let transactions = 0;
  const guard = (sql: string, armed: boolean) => {
    if (armed && pattern.test(sql)) throw new Error(`${SENTINEL} synthetic write failure`);
  };
  return {
    async execute(sql, parameters) { guard(sql, transactions >= spare); return database.execute(sql, parameters); },
    async query(sql, parameters) { guard(sql, transactions >= spare); return database.query(sql, parameters); },
    async transaction(fn) {
      const armed = transactions++ >= spare;
      return database.transaction((tx) => fn({
        execute: async (sql: string, parameters?: readonly DatabaseValue[]) => { guard(sql, armed); return tx.execute(sql, parameters); },
        query: async (sql: string, parameters?: readonly DatabaseValue[]) => { guard(sql, armed); return tx.query(sql, parameters); },
        transaction: tx.transaction.bind(tx),
        close: tx.close.bind(tx),
      }));
    },
    close: () => database.close(),
  };
}

function guarded(inner: InstallationCipher): { cipher: InstallationCipher; forbidden: string[] } {
  const forbidden: string[] = [];
  return {
    forbidden,
    cipher: {
      seal: (alias, plaintext, aad) => inner.seal(alias, plaintext, aad),
      keyStatus: (alias) => { forbidden.push("keyStatus"); return inner.keyStatus(alias); },
      isDeviceSecure: () => { forbidden.push("isDeviceSecure"); return inner.isDeviceSecure(); },
      create: (alias) => { forbidden.push("create"); return inner.create(alias); },
      delete: (alias) => { forbidden.push("delete"); return inner.delete(alias); },
      open: (alias, sealed, aad) => { forbidden.push("open"); return inner.open(alias, sealed, aad); },
    },
  };
}

class SpyCipher implements InstallationCipher {
  seals = 0;
  private readonly inner: InstallationCipher;
  private readonly script: Map<number, { returns?: unknown; throws?: unknown; before?: () => Promise<void> }>;
  constructor(inner: InstallationCipher, script: Record<number, { returns?: unknown; throws?: unknown; before?: () => Promise<void> }> = {}) {
    this.inner = inner;
    this.script = new Map(Object.entries(script).map(([call, entry]) => [Number(call), entry]));
  }
  async seal(alias: string, plaintext: Uint8Array, aad: Uint8Array): Promise<CipherResult<Uint8Array>> {
    const entry = this.script.get(++this.seals);
    await entry?.before?.();
    if (entry && "throws" in entry) throw entry.throws;
    if (entry && "returns" in entry) return entry.returns as CipherResult<Uint8Array>;
    return this.inner.seal(alias, plaintext, aad);
  }
  create(alias: string) { return this.inner.create(alias); }
  keyStatus(alias: string): Promise<InstallationKeyStatus> { return this.inner.keyStatus(alias); }
  open(alias: string, sealed: Uint8Array, aad: Uint8Array) { return this.inner.open(alias, sealed, aad); }
  delete(alias: string) { return this.inner.delete(alias); }
  isDeviceSecure() { return this.inner.isDeviceSecure(); }
}

const oversized = (request: unknown) => ({
  outcome: "page", projectionSchemaVersion: 1, projections: [new Uint8Array(5_000).fill(0x61)],
  nextPosition: utf8(`synthetic-position:${(request as SnapshotReadRequest).snapshotId}:1`), exhausted: false,
});

/** A type with one staged snapshot page and more to read: 60-second calls under a 100-second budget. */
async function partway(f: Fixture, recordType = "steps", records = 6) {
  f.sources.seed(recordType, records);
  f.sources.callCostMs = 60_000;
  await ran(f.sync.trigger({ kind: "background", budgetMs: 100_000 }));
  f.sources.callCostMs = 0;
  const attempt = await attemptOf(f, recordType);
  const progress = await cycleProgress(f.database, attempt.snapshotId);
  assert.ok(progress && !progress.finished && progress.pagesStaged === 1);
  return { attempt, progress };
}

// R2 — Caller errors

test("malformed_options_are_caller_errors", async (t) => {
  const f = await fixture(t, { types: ["steps", "weight"] });
  const before = await contents(f.database);
  const good = f.options();
  const sources = { issue: f.sources.issue.bind(f.sources), readSnapshotPage: f.sources.readSnapshotPage.bind(f.sources) };
  const bad: unknown[] = [
    null, {}, { ...good, database: null }, { ...good, cipher: undefined }, { ...good, sources },
    { ...good, recordTypes: "steps" }, { ...good, recordTypes: ["steps", "steps"] }, { ...good, recordTypes: ["Steps"] },
    { ...good, recordTypes: ["steps", "heart-rate"] },
    { ...good, projection: { implementationVersion: "", schemaVersions: good.projection.schemaVersions } },
    { ...good, projection: { implementationVersion: "synthetic-build-1", schemaVersions: { steps: 0, weight: 1 } } },
    { ...good, concurrency: 0 }, { ...good, concurrency: 1.5 },
    { ...good, now: 5 }, { ...good, monotonicNow: undefined }, { ...good, randomUUID: undefined },
    { ...good, context: f.context }, { ...good, conditions: CLEAR }, { ...good, backgroundScheduling: "available" },
  ];
  for (const [index, options] of bad.entries()) assert.throws(() => createSenderSync(options as SenderSyncOptions), TypeError, `case ${index}`);
  assert.equal(f.sources.calls.length, 0);
  assert.deepEqual(await contents(f.database), before);
});

test("malformed_triggers_are_caller_errors", async (t) => {
  const f = await fixture(t, { types: ["steps", "weight"] });
  const before = await contents(f.database);
  const bad: unknown[] = [
    null, {}, { kind: "hourly" }, { kind: "background", budgetMs: 0 }, { kind: "background", budgetMs: 1.5 },
    { kind: "foreground", priority: ["steps"] }, { kind: "manual", priority: ["heart-rate"] }, { kind: "manual", priority: "steps" },
  ];
  for (const request of bad) await assert.rejects(f.sync.trigger(request as TriggerRequest), TypeError, JSON.stringify(request));
  await assert.rejects(f.sync.requestRetry("heart-rate"), TypeError);
  for (const now of [Number.NaN, 1.5, 253_402_300_800_000, -62_167_219_200_001]) {
    await assert.rejects(f.make({ now: () => now }).trigger({ kind: "foreground" }), TypeError, String(now));
  }
  assert.equal(f.sources.calls.length, 0);
  assert.deepEqual(await contents(f.database), before);
});

test("unsupported_types_are_caller_errors", async (t) => {
  const f = await fixture(t, { types: ["steps", "weight"] });
  const before = await contents(f.database);
  f.context = stagingContext({ projectionSchemas: { steps: [1] } });
  await assert.rejects(f.sync.trigger({ kind: "foreground" }), TypeError);
  f.context = stagingContext({ projectionSchemas: { steps: [1], weight: [2, 3] } });
  await assert.rejects(f.sync.trigger({ kind: "foreground" }), TypeError);
  assert.equal(f.sources.calls.length, 0);
  assert.deepEqual(await contents(f.database), before);
});

// R3 — The run marker

test("the_run_is_recorded_before_any_source_call", async (t) => {
  const f = await fixture(t, { types: ["steps", "weight"], concurrency: 1 });
  f.scheduling = "restricted";
  const seen: unknown[] = [];
  f.sources.onCall = async () => {
    if (seen.length > 0) return;
    seen.push(await f.database.query("SELECT installation_id, run_sequence, trigger, started_at_ms, background_scheduling FROM sync_runs"));
    seen.push((await f.database.query("SELECT record_type FROM sync_run_types ORDER BY record_type")).map((row) => row.record_type));
    seen.push((await outboxMessages(f.database)).length);
  };
  await ran(f.sync.trigger({ kind: "background" }));
  assert.deepEqual(seen, [
    [{ installation_id: INSTALLATION, run_sequence: 1, trigger: "background", started_at_ms: START, background_scheduling: "restricted" }],
    ["steps", "weight"],
    0,
  ]);
});

test("run_sequences_count_per_installation", async (t) => {
  const f = await fixture(t, { types: [] });
  for (const expected of [1, 2, 3]) assert.equal((await ran(f.sync.trigger({ kind: "background" }))).runSequence, expected);
  f.context = stagingContext({}, { installationId: OTHER_INSTALLATION });
  assert.equal((await ran(f.sync.trigger({ kind: "background" }))).runSequence, 1);
  f.context = stagingContext();
  assert.equal((await ran(f.sync.trigger({ kind: "background" }))).runSequence, 4);
  assert.deepEqual((await checkpointMessages(f)).map((message) => message.run_sequence), [1, 2, 3, 4]);
  assert.deepEqual((await checkpointMessages(f, OTHER_INSTALLATION)).map((message) => message.run_sequence), [1]);
});

test("the_checkpoint_reports_the_run", async (t) => {
  const f = await fixture(t, { types: ["weight", "steps"] });
  f.sources.seed("steps", 3);
  f.sources.callCostMs = 1_500;
  f.scheduling = "unregistered";
  const startMono = f.clock.mono;
  const result = await ran(f.sync.trigger({ kind: "foreground" }));
  const elapsed = f.clock.mono - startMono;
  assert.equal(elapsed, 7 * 1_500);
  assert.deepEqual(result.summary, {
    runSequence: 1, trigger: "foreground", startedAtMs: START, endedAtMs: START + elapsed, elapsedMs: elapsed,
    backgroundScheduling: "unregistered",
    recordTypes: [
      { recordType: "steps", outcome: "completed", sourceDataStaged: true },
      { recordType: "weight", outcome: "completed", sourceDataStaged: false },
    ],
  });
  const [message] = await checkpointMessages(f);
  const { wire_version: _wire, kind, sequence: _sequence, outbox_messages_before_checkpoint: _messages, outbox_bytes_before_checkpoint: _bytes, ...rest } = message;
  assert.equal(kind, "sync-checkpoint");
  assert.deepEqual(
    rest,
    {
      sender_installation: INSTALLATION, capability_generation: 6, run_sequence: 1, trigger: "foreground",
      started_at: iso(START), ended_at: iso(START + elapsed), elapsed_ms: elapsed, background_scheduling: "unregistered",
      record_types: [
        { record_type: "steps", outcome: "completed", source_data_staged: true },
        { record_type: "weight", outcome: "completed", source_data_staged: false },
      ],
    },
  );
});

// R4 — Recovery before reading

test("an_open_run_is_checkpointed_as_interrupted_before_reading", async (t) => {
  const f = await fixture(t, { types: ["steps", "weight"], concurrency: 1 });
  f.sources.seed("steps", 3);
  f.scheduling = "restricted";
  const held = f.sources.hold("snapshot", "steps");
  void f.sync.trigger({ kind: "background", budgetMs: 480_000 });
  await held.arrived;
  f.clock.advance(3_600_000);
  await f.reopen();
  f.scheduling = "available";
  let checkpointsAtFirstCall: number | null = null;
  f.sources.onCall = async () => {
    checkpointsAtFirstCall ??= (await outboxMessages(f.database)).filter((row) => row.kind === "sync-checkpoint").length;
  };
  const result = await foreground(f);
  assert.equal(result.runSequence, 2);
  assert.equal(checkpointsAtFirstCall, 1);
  const [interrupted, second] = await checkpointMessages(f);
  assert.equal(interrupted.run_sequence, 1);
  assert.equal(interrupted.trigger, "background");
  assert.equal(interrupted.started_at, iso(START));
  assert.equal(interrupted.ended_at, iso(START + 3_600_000));
  assert.equal(interrupted.background_scheduling, "restricted");
  assert.equal("elapsed_ms" in interrupted, false);
  assert.deepEqual(interrupted.record_types, [
    { record_type: "steps", outcome: "interrupted", source_data_staged: false },
    { record_type: "weight", outcome: "interrupted", source_data_staged: false },
  ]);
  assert.equal(second.run_sequence, 2);
});

test("interrupted_runs_report_staged_data_exactly", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps", "weight"], concurrency: 1 });
  f.sources.seed("steps", 3);
  // heart-rate stages an empty snapshot and its completion first, then the process dies the
  // moment steps' first page commits, before anything else is written.
  const raw = f.database;
  const crash = crashing(raw, async () => (await outboxMessages(raw)).filter((row) => row.kind === "ingest-batch").length >= 2);
  void f.make({ database: crash.database }).trigger({ kind: "foreground" });
  await crash.death;
  await f.reopen();
  await foreground(f);
  const [interrupted] = await checkpointMessages(f);
  assert.deepEqual(interrupted.record_types, [
    { record_type: "heart-rate", outcome: "interrupted", source_data_staged: false },
    { record_type: "steps", outcome: "interrupted", source_data_staged: true },
    { record_type: "weight", outcome: "interrupted", source_data_staged: false },
  ]);
});

test("a_failed_recovery_reads_nothing", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 3);
  const held = f.sources.hold("snapshot", "steps");
  void f.sync.trigger({ kind: "background" });
  await held.arrived;
  await f.reopen();
  const calls = f.sources.calls.length;
  const before = await contents(f.database);
  const unavailable = new SpyCipher(f.cipher, { 1: { returns: { status: "error", reason: "keystore-unavailable" } } });
  assert.deepEqual(await f.make({ cipher: unavailable }).trigger({ kind: "foreground" }), { outcome: "failed", failure: "keystore-unavailable" });
  assert.equal(f.sources.calls.length, calls);
  assert.deepEqual(await contents(f.database), before);
  assert.equal((await foreground(f)).runSequence, 2);
  assert.deepEqual((await checkpointMessages(f)).map((message) => [message.run_sequence, (message.record_types as Message[])[0].outcome]),
    [[1, "interrupted"], [2, "completed"]]);
});

test("a_checkpointed_run_is_not_recovered", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  await foreground(f);
  // The process dies the moment the second run's checkpoint commits.
  const raw = f.database;
  const crash = crashing(raw, async () => (await outboxMessages(raw)).filter((row) => row.kind === "sync-checkpoint").length >= 2);
  void f.make({ database: crash.database }).trigger({ kind: "background" });
  await crash.death;
  await f.reopen();
  await foreground(f);
  assert.deepEqual((await checkpointMessages(f)).map((message) => [message.run_sequence, message.trigger]),
    [[1, "foreground"], [2, "background"], [3, "foreground"]]);
});

// R5 — One type's traversal

test("a_new_type_begins_an_initial_snapshot", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 3);
  const result = await foreground(f);
  assert.equal(f.sources.calls[0].method, "issue");
  const attempt = await attemptOf(f);
  assert.equal(attempt.kind, "initial");
  assert.equal(attempt.installationId, INSTALLATION);
  assert.equal(attempt.changesToken, f.sources.token("steps", 3));
  assert.deepEqual(f.sources.callsOf("snapshot")[0].request, { recordType: "steps", snapshotId: attempt.snapshotId, position: null });
  assert.deepEqual(outcomes(result).steps, ["completed", null, true]);
});

test("snapshot_pages_resume_from_the_saved_position", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 5);
  f.sources.callCostMs = 60_000;
  const first = await ran(f.sync.trigger({ kind: "background", budgetMs: 150_000 }));
  assert.deepEqual(outcomes(first).steps, ["progressed", "time-budget-reached", true]);
  const attempt = await attemptOf(f);
  const saved = (await cycleProgress(f.database, attempt.snapshotId))!;
  assert.deepEqual([saved.pagesStaged, saved.recordCount, saved.finished], [2, 4, false]);
  f.sources.callCostMs = 0;
  await foreground(f);
  const positions = f.sources.callsOf("snapshot").map((call) => (call.request as SnapshotReadRequest).position);
  assert.deepEqual(positions.map(text), [null, `synthetic-position:${attempt.snapshotId}:2`, `synthetic-position:${attempt.snapshotId}:4`]);
  assert.deepEqual(positions[2], saved.position);
  const done = (await cycleProgress(f.database, attempt.snapshotId))!;
  assert.deepEqual([done.recordCount, done.finished], [5, true]);
});

test("a_finished_snapshot_is_completed_then_caught_up", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 3);
  const result = await foreground(f);
  const attempt = await attemptOf(f);
  const messages = await decoded(f);
  assert.deepEqual(messages.map((entry) => entry.kind),
    ["ingest-batch", "ingest-batch", "snapshot-completion", "ingest-batch", "change-completion", "sync-checkpoint"]);
  assert.equal(messages[2].message.cycle_id, attempt.snapshotId);
  assert.equal(messages[2].message.snapshot_kind, "initial");
  const changes = f.sources.callsOf("change");
  assert.equal(changes.length, 1);
  assert.deepEqual(changes[0].request, {
    recordType: "steps", cycleId: messages[4].message.cycle_id, changesToken: attempt.changesToken, unreadSinceMs: attempt.unreadSinceMs,
  });
  assert.deepEqual(outcomes(result).steps, ["completed", null, true]);
});

test("change_cycles_resume_from_the_saved_token", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  await foreground(f);
  f.sources.put("steps", "a", "b", "c", "d", "e");
  f.sources.callCostMs = 60_000;
  const second = await ran(f.sync.trigger({ kind: "background", budgetMs: 100_000 }));
  assert.deepEqual(outcomes(second).steps, ["progressed", "time-budget-reached", true]);
  const cycleId = (f.sources.callsOf("change")[1].request as ChangeReadRequest).cycleId;
  const saved = (await cycleProgress(f.database, cycleId))!;
  assert.deepEqual([saved.pagesStaged, saved.finished], [2, false]);
  f.sources.callCostMs = 0;
  await foreground(f);
  assert.deepEqual(f.sources.callsOf("change")[3].request, {
    recordType: "steps", cycleId, changesToken: saved.changesToken, unreadSinceMs: saved.unreadSinceMs,
  });
  const done = (await cycleProgress(f.database, cycleId))!;
  assert.deepEqual([done.recordCount, done.finished], [5, true]);
});

test("a_pending_completion_is_staged_first", async (t) => {
  // A pool of one keeps the coordinator idle while the hook reads storage.
  const f = await fixture(t, { types: ["steps"], concurrency: 1 });
  f.sources.seed("steps", 3);
  // The process dies the moment the final snapshot page commits, before its completion.
  const raw = f.database;
  const crash = crashing(raw, async () => (await outboxMessages(raw)).filter((row) => row.kind === "ingest-batch").length >= 2);
  void f.make({ database: crash.database }).trigger({ kind: "foreground" });
  await crash.death;
  await f.reopen();
  const attempt = await attemptOf(f);
  assert.equal((await cycleProgress(f.database, attempt.snapshotId))!.finished, true);
  assert.equal(await cycleCompletion(f.database, attempt.snapshotId), null);
  let completedFirst: boolean | null = null;
  f.sources.onCall = async () => { completedFirst ??= await cycleCompletion(f.database, attempt.snapshotId) !== null; };
  const calls = f.sources.calls.length;
  await foreground(f);
  assert.equal(completedFirst, true);
  assert.deepEqual(f.sources.calls.slice(calls).map((call) => call.method), ["change"]);
});

test("each_run_begins_a_new_change_cycle", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 1);
  await foreground(f);
  f.sources.put("steps", "rec-0002");
  await foreground(f);
  const cycles = (await decoded(f)).filter((entry) => entry.kind === "change-completion").map((entry) => entry.message.cycle_id as string);
  assert.equal(cycles.length, 2);
  assert.notEqual(cycles[0], cycles[1]);
  const next = f.sources.callsOf("change")[1].request as ChangeReadRequest;
  assert.equal(next.cycleId, cycles[1]);
  assert.equal(next.changesToken, (await cycleProgress(f.database, cycles[0]))!.changesToken);
});

// R6 — A check without changes

test("an_unchanged_type_stages_an_empty_change_cycle", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 2);
  await foreground(f);
  const before = (await decoded(f)).length;
  const result = await ran(f.sync.trigger({ kind: "background" }));
  const added = (await decoded(f)).slice(before);
  assert.deepEqual(added.map((entry) => entry.kind), ["ingest-batch", "change-completion", "sync-checkpoint"]);
  const cycle = (await cycleProgress(f.database, added[1].message.cycle_id as string))!;
  assert.deepEqual([cycle.pagesStaged, cycle.recordCount, cycle.finished], [1, 0, true]);
  assert.deepEqual(outcomes(result).steps, ["no-change", null, false]);
});

test("an_unchanged_type_keeps_its_token_fresh", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  await foreground(f);
  for (let run = 0; run < 3; run++) {
    f.clock.advance(20 * DAY);
    await ran(f.sync.trigger({ kind: "background" }));
  }
  assert.deepEqual(f.sources.callsOf("change").map((call) => (call.request as ChangeReadRequest).unreadSinceMs),
    [START, START, START + 20 * DAY, START + 40 * DAY]);
});

// R7 — Fair scheduling

test("one_turn_per_type_at_a_time", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps"], concurrency: 3 });
  for (const type of f.types) f.sources.seed(type, 8);
  await foreground(f);
  assert.equal(f.sources.maxInFlightPerType, 1);
  assert.equal(f.sources.maxInFlight, 2);
});

test("turns_never_exceed_the_pool", async (t) => {
  const f = await fixture(t, { types: FOUR_TYPES });
  for (const type of FOUR_TYPES) f.sources.seed(type, 8);
  const result = await foreground(f);
  assert.equal(f.sources.maxInFlight, 2);
  for (const type of FOUR_TYPES) assert.deepEqual(outcomes(result)[type], ["completed", null, true]);
});

test("the_pool_size_is_configurable", async (t) => {
  for (const concurrency of [1, 3]) {
    const f = await fixture(t, { types: FOUR_TYPES, concurrency });
    for (const type of FOUR_TYPES) f.sources.seed(type, 8);
    await foreground(f);
    assert.equal(f.sources.maxInFlight, concurrency);
  }
});

test("a_staged_page_moves_its_type_to_the_tail", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps", "weight"], concurrency: 1 });
  for (const type of f.types) f.sources.seed(type, 4);
  await foreground(f);
  assert.deepEqual(f.sources.callsOf("snapshot").map((call) => call.recordType),
    ["heart-rate", "steps", "weight", "heart-rate", "steps", "weight"]);
  assert.deepEqual(f.sources.callsOf("change").map((call) => call.recordType), ["heart-rate", "steps", "weight"]);
  assert.deepEqual(await ring(f.database), ["heart-rate", "steps", "weight"]);
});

test("the_ring_survives_a_restart", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps", "weight"], concurrency: 1 });
  for (const type of f.types) f.sources.seed(type, 4);
  f.sources.callCostMs = 10_000;
  await ran(f.sync.trigger({ kind: "background", budgetMs: 65_000 }));
  assert.deepEqual(f.sources.calls.map((call) => [call.method, call.recordType]), [
    ["issue", "heart-rate"], ["snapshot", "heart-rate"], ["issue", "steps"], ["snapshot", "steps"],
    ["issue", "weight"], ["snapshot", "weight"], ["snapshot", "heart-rate"],
  ]);
  await f.reopen();
  f.sources.callCostMs = 0;
  const calls = f.sources.calls.length;
  await foreground(f);
  assert.deepEqual(f.sources.calls.slice(calls, calls + 3).map((call) => [call.method, call.recordType]),
    [["snapshot", "steps"], ["snapshot", "weight"], ["change", "heart-rate"]]);
});

test("skipped_types_keep_their_place", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps", "weight"], concurrency: 1 });
  for (const type of f.types) f.sources.seed(type, 2);
  f.sources.next("issue", "heart-rate", NOT_PERMITTED);
  const first = await foreground(f);
  assert.deepEqual(outcomes(first)["heart-rate"], ["not-permitted", null, false]);
  assert.equal((await ring(f.database))[0], "heart-rate");
  const calls = f.sources.calls.length;
  await foreground(f);
  assert.deepEqual([f.sources.calls[calls].method, f.sources.calls[calls].recordType], ["issue", "heart-rate"]);
});

test("concurrent_turns_stage_every_page", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps"] });
  for (const type of f.types) f.sources.seed(type, 6);
  const first = f.sources.hold("snapshot", "heart-rate");
  const second = f.sources.hold("snapshot", "steps");
  const pending = f.sync.trigger({ kind: "foreground" });
  await Promise.all([first.arrived, second.arrived]);
  first.release();
  second.release();
  const result = await ran(pending);
  for (const type of f.types) {
    assert.deepEqual(outcomes(result)[type], ["completed", null, true]);
    const progress = (await cycleProgress(f.database, (await attemptOf(f, type)).snapshotId))!;
    assert.deepEqual([progress.recordCount, progress.finished], [6, true]);
  }
  const sequences = (await outboxMessages(f.database)).map((row) => row.sequence);
  assert.deepEqual(sequences, sequences.map((_, index) => index + 1));
  assert.equal(f.sources.maxInFlight, 2);
});

// R8 — Coalesced triggers

test("overlapping_triggers_coalesce_into_one_follow_up", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 2);
  const held = f.sources.hold("snapshot", "steps");
  const first = f.sync.trigger({ kind: "background" });
  await held.arrived;
  const others = [f.sync.trigger({ kind: "background" }), f.sync.trigger({ kind: "foreground" }), f.sync.trigger({ kind: "background" })];
  held.release();
  const [one, ...rest] = await Promise.all([first, ...others]);
  assert.equal((one as Checkpointed).runSequence, 1);
  for (const result of rest) assert.deepEqual(result, rest[0]);
  assert.equal((rest[0] as Checkpointed).runSequence, 2);
  assert.deepEqual((await checkpointMessages(f)).map((message) => [message.run_sequence, message.trigger]),
    [[1, "background"], [2, "foreground"]]);
});

test("merged_triggers_keep_the_strongest_terms", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps", "weight"], concurrency: 1 });
  await foreground(f);
  const established = f.sources.calls.length;
  for (const type of f.types) f.sources.put(type, "c1");
  f.sources.callCostMs = 60_000;
  // Kind, budget, and priority: manual wins, any unbounded trigger removes the budget, priorities accumulate.
  let held = f.sources.hold("change");
  let first = f.sync.trigger({ kind: "background" });
  await held.arrived;
  let merged = [
    f.sync.trigger({ kind: "background", budgetMs: 100_000 }),
    f.sync.trigger({ kind: "manual", priority: ["weight"] }),
    f.sync.trigger({ kind: "foreground", budgetMs: 50_000 }),
    f.sync.trigger({ kind: "manual", priority: ["steps", "weight"] }),
  ];
  held.release();
  await ran(first);
  let results = await Promise.all(merged);
  for (const result of results) assert.deepEqual(result, results[0]);
  const follow = results[0] as Checkpointed;
  assert.equal(follow.summary.trigger, "manual");
  assert.deepEqual(f.sources.calls.slice(established + 3).map((call) => call.recordType), ["weight", "steps", "heart-rate"]);
  // Budgets: the latest wins when every merged trigger has one. With 60-second calls, a
  // 130-second budget allows three calls and a 70-second budget two.
  for (const type of f.types) f.sources.put(type, "c2", "c3", "c4", "c5", "c6", "c7");
  f.sources.pageSize = 1;
  const partTwo = f.sources.calls.length;
  held = f.sources.hold("change");
  first = f.sync.trigger({ kind: "background", budgetMs: 1 });
  await held.arrived;
  merged = [f.sync.trigger({ kind: "background", budgetMs: 130_000 }), f.sync.trigger({ kind: "background", budgetMs: 70_000 })];
  held.release();
  await ran(first);
  results = await Promise.all(merged);
  assert.equal((results[0] as Checkpointed).runSequence, 5);
  assert.equal(f.sources.calls.length - partTwo, 1 + 2);
});

test("a_trigger_never_cancels_active_work", async (t) => {
  const f = await fixture(t, { types: ["steps", "weight"], concurrency: 1 });
  f.sources.seed("steps", 4);
  f.sources.seed("weight", 4);
  const held = f.sources.hold("snapshot", "steps");
  const first = f.sync.trigger({ kind: "background" });
  await held.arrived;
  const manual = f.sync.trigger({ kind: "manual", priority: ["weight"] });
  held.release();
  const one = await ran(first);
  assert.equal(one.summary.trigger, "background");
  assert.deepEqual(outcomes(one), { steps: ["completed", null, true], weight: ["completed", null, true] });
  const two = await ran(manual);
  assert.deepEqual([two.runSequence, two.summary.trigger], [2, "manual"]);
});

// R9 — One-time manual priority

test("manual_priority_moves_a_type_forward_once", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps", "weight"], concurrency: 1 });
  await foreground(f);
  assert.deepEqual(await ring(f.database), ["heart-rate", "steps", "weight"]);
  f.sources.pageSize = 1;
  f.sources.put("heart-rate", "h1");
  f.sources.put("steps", "s1");
  f.sources.put("weight", "w1", "w2", "w3");
  const calls = f.sources.calls.length;
  await ran(f.sync.trigger({ kind: "manual", priority: ["weight"] }));
  assert.deepEqual(f.sources.calls.slice(calls).map((call) => call.recordType), ["weight", "heart-rate", "steps", "weight", "weight"]);
});

test("priority_does_not_persist", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps", "weight"], concurrency: 1 });
  await foreground(f);
  f.sources.pageSize = 1;
  f.sources.put("heart-rate", "h1");
  f.sources.put("steps", "s1");
  f.sources.put("weight", "w1", "w2", "w3");
  await ran(f.sync.trigger({ kind: "manual", priority: ["weight"] }));
  assert.deepEqual(await ring(f.database), ["heart-rate", "steps", "weight"]);
  for (const type of f.types) f.sources.put(type, "next");
  const calls = f.sources.calls.length;
  await foreground(f);
  assert.deepEqual(f.sources.calls.slice(calls).map((call) => call.recordType), ["heart-rate", "steps", "weight"]);
});

// R10 — Soft deadline

test("the_deadline_starts_no_further_source_call", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 20);
  f.sources.callCostMs = 60_000;
  const starts: number[] = [];
  f.sources.onCall = () => { starts.push(f.clock.mono); };
  const startMono = f.clock.mono;
  // The third call would start exactly when the budget is reached, so it never starts.
  const result = await ran(f.sync.trigger({ kind: "background", budgetMs: 120_000 }));
  assert.deepEqual(starts.map((start) => start - startMono), [0, 60_000]);
  assert.deepEqual(outcomes(result).steps, ["progressed", "time-budget-reached", true]);
});

test("an_in_flight_page_is_staged_after_the_deadline", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 2);
  const held = f.sources.hold("snapshot", "steps");
  const pending = f.sync.trigger({ kind: "background", budgetMs: 480_000 });
  await held.arrived;
  f.clock.advance(600_000);
  held.release();
  const result = await ran(pending);
  assert.deepEqual(outcomes(result).steps, ["progressed", "time-budget-reached", true]);
  const attempt = await attemptOf(f);
  assert.equal((await cycleProgress(f.database, attempt.snapshotId))!.finished, true);
  assert.notEqual(await cycleCompletion(f.database, attempt.snapshotId), null);
  assert.equal(f.sources.callsOf("change").length, 0);
});

test("unreached_types_are_deferred", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps", "weight"], concurrency: 1 });
  f.sources.seed("heart-rate", 4);
  f.sources.callCostMs = 50_000;
  const first = await ran(f.sync.trigger({ kind: "background", budgetMs: 120_000 }));
  assert.deepEqual(outcomes(first), {
    "heart-rate": ["progressed", "time-budget-reached", true],
    steps: ["progressed", "time-budget-reached", false],
    weight: ["deferred", "time-budget-reached", false],
  });
  f.sources.callCostMs = 0;
  const second = await foreground(f);
  for (const type of f.types) assert.equal(outcomes(second)[type][0], "completed");
});

// R11 — Collection disabled

test("a_disabled_run_reads_nothing_and_checkpoints", async (t) => {
  const f = await fixture(t, { types: ["steps", "weight"] });
  f.sources.seed("steps", 2);
  f.conditions.collectionEnabled = false;
  const result = await ran(f.sync.trigger({ kind: "background" }));
  assert.equal(f.sources.calls.length, 0);
  assert.deepEqual(outcomes(result), { steps: ["deferred", "collection-disabled", false], weight: ["deferred", "collection-disabled", false] });
  assert.deepEqual((await decoded(f)).map((entry) => entry.kind), ["sync-checkpoint"]);
});

test("disabling_mid_run_stages_the_in_flight_page", async (t) => {
  const f = await fixture(t, { types: ["steps", "weight"], concurrency: 1 });
  f.sources.seed("steps", 4);
  f.sources.seed("weight", 4);
  const held = f.sources.hold("snapshot", "steps");
  const pending = f.sync.trigger({ kind: "background" });
  await held.arrived;
  f.conditions.collectionEnabled = false;
  const calls = f.sources.calls.length;
  held.release();
  const result = await ran(pending);
  assert.equal(f.sources.calls.length, calls);
  assert.deepEqual(outcomes(result), { steps: ["progressed", "collection-disabled", true], weight: ["deferred", "collection-disabled", false] });
  assert.equal((await cycleProgress(f.database, (await attemptOf(f)).snapshotId))!.pagesStaged, 1);
});

test("no_completion_while_disabled", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 2);
  const held = f.sources.hold("snapshot", "steps");
  const pending = f.sync.trigger({ kind: "background" });
  await held.arrived;
  f.conditions.collectionEnabled = false;
  held.release();
  const result = await ran(pending);
  assert.deepEqual(outcomes(result).steps, ["progressed", "collection-disabled", true]);
  const attempt = await attemptOf(f);
  assert.equal((await cycleProgress(f.database, attempt.snapshotId))!.finished, true);
  assert.equal(await cycleCompletion(f.database, attempt.snapshotId), null);
  assert.deepEqual((await decoded(f)).map((entry) => entry.kind), ["ingest-batch", "sync-checkpoint"]);
});

test("re_enabling_resumes_the_exact_position", async (t) => {
  const f = await fixture(t, { types: ["steps", "weight"], concurrency: 1 });
  f.sources.seed("steps", 2);
  f.sources.seed("weight", 6);
  const held = f.sources.hold("snapshot", "steps");
  const pending = f.sync.trigger({ kind: "background" });
  await held.arrived;
  f.conditions.collectionEnabled = false;
  held.release();
  await ran(pending);
  const attempt = await attemptOf(f);
  const source = async () => JSON.stringify([
    await currentSnapshotAttempt(f.database, "steps"), await currentSnapshotAttempt(f.database, "weight"),
    await cycleProgress(f.database, attempt.snapshotId), await cycleCompletion(f.database, attempt.snapshotId),
  ]);
  const before = await source();
  const calls = f.sources.calls.length;
  await ran(f.sync.trigger({ kind: "background" }));
  assert.equal(await source(), before);
  assert.equal(f.sources.calls.length, calls);
  f.conditions.collectionEnabled = true;
  let completedFirst: boolean | null = null;
  f.sources.onCall = async (call) => {
    if (call.recordType === "steps") completedFirst ??= await cycleCompletion(f.database, attempt.snapshotId) !== null;
  };
  const result = await foreground(f);
  assert.equal(completedFirst, true);
  const stepsCalls = f.sources.calls.slice(calls).filter((call) => call.recordType === "steps");
  assert.deepEqual(stepsCalls.map((call) => call.method), ["change"]);
  assert.equal((stepsCalls[0].request as ChangeReadRequest).changesToken, attempt.changesToken);
  assert.equal((await attemptOf(f)).snapshotId, attempt.snapshotId);
  assert.deepEqual(outcomes(result), { steps: ["completed", null, false], weight: ["completed", null, true] });
});

// R12 — Other conditions

test("operator_action_conditions_block_reads", async (t) => {
  const f = await fixture(t, { types: ["steps", "weight"] });
  f.sources.seed("steps", 2);
  const cases: [Partial<SyncConditions>, string][] = [
    [{ secureLock: false }, "screen-lock-removed"],
    [{ storagePressure: true }, "storage-pressure"],
    [{ deliveryBlocked: true }, "delivery-blocked"],
  ];
  for (const [conditions, reason] of cases) {
    Object.assign(f.conditions, CLEAR, conditions);
    const result = await ran(f.sync.trigger({ kind: "background" }));
    assert.deepEqual(outcomes(result), { steps: ["blocked", reason, false], weight: ["blocked", reason, false] });
  }
  assert.equal(f.sources.calls.length, 0);
  assert.equal((await checkpointMessages(f)).length, 3);
});

test("condition_precedence_is_fixed", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  const cases: [Partial<SyncConditions>, string][] = [
    [{ secureLock: false, storagePressure: true, deliveryBlocked: true, collectionEnabled: false }, "screen-lock-removed"],
    [{ storagePressure: true, deliveryBlocked: true, collectionEnabled: false }, "storage-pressure"],
    [{ deliveryBlocked: true, collectionEnabled: false }, "delivery-blocked"],
  ];
  for (const [conditions, reason] of cases) {
    Object.assign(f.conditions, CLEAR, conditions);
    assert.deepEqual(outcomes(await ran(f.sync.trigger({ kind: "background" }))).steps, ["blocked", reason, false]);
  }
  Object.assign(f.conditions, CLEAR);
  f.sources.seed("steps", 4);
  const held = f.sources.hold("snapshot", "steps");
  const pending = f.sync.trigger({ kind: "background" });
  await held.arrived;
  f.conditions.storagePressure = true;
  held.release();
  assert.deepEqual(outcomes(await ran(pending)).steps, ["progressed", "storage-pressure", true]);
});

// R13 — Permission gaps

test("missing_permission_pauses_at_the_exact_position", async (t) => {
  const f = await fixture(t, { types: ["steps", "weight"], concurrency: 1 });
  const { attempt, progress } = await partway(f);
  f.sources.seed("weight", 2);
  f.sources.next("snapshot", "steps", { outcome: "not-permitted", failure: "history-permission-missing" });
  const result = await foreground(f);
  assert.deepEqual(outcomes(result), { steps: ["not-permitted", null, false], weight: ["completed", null, true] });
  assert.deepEqual(await cycleProgress(f.database, attempt.snapshotId), progress);
  assert.deepEqual((f.sources.callsOf("snapshot", "steps").at(-1)!.request as SnapshotReadRequest).position, progress.position);
});

test("regrant_resumes_the_same_position", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  const { attempt, progress } = await partway(f);
  f.sources.next("snapshot", "steps", NOT_PERMITTED);
  await foreground(f);
  const refused = f.sources.callsOf("snapshot", "steps").at(-1)!.request as SnapshotReadRequest;
  const result = await foreground(f);
  const resumed = f.sources.callsOf("snapshot", "steps").at(-2)!.request as SnapshotReadRequest;
  assert.deepEqual(resumed, refused);
  assert.deepEqual(resumed, { recordType: "steps", snapshotId: attempt.snapshotId, position: progress.position });
  assert.deepEqual(outcomes(result).steps, ["completed", null, true]);
});

test("permission_lost_after_progress_reports_progressed", async (t) => {
  const f = await fixture(t, { types: ["steps", "weight"], concurrency: 1 });
  f.sources.seed("steps", 6);
  f.sources.next("snapshot", "steps", { outcome: "not-permitted", failure: "read-denied" }, 1);
  f.sources.next("issue", "weight", NOT_PERMITTED);
  const result = await foreground(f);
  assert.deepEqual(outcomes(result), { steps: ["progressed", "permission-missing", true], weight: ["not-permitted", null, false] });
  assert.equal(await currentSnapshotAttempt(f.database, "weight"), null);
});

// R14 — Lost continuity

test("an_invalid_position_before_baseline_restarts_the_initial_snapshot", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  const { attempt: old, progress } = await partway(f);
  const calls = f.sources.calls.length;
  f.sources.next("snapshot", "steps", { outcome: "invalid-position", failure: "malformed-position" });
  const result = await foreground(f);
  const current = await attemptOf(f);
  assert.notEqual(current.snapshotId, old.snapshotId);
  assert.equal(current.kind, "initial");
  assert.notEqual((await snapshotAttempt(f.database, old.snapshotId))!.abandonedAtMs, null);
  const run = f.sources.calls.slice(calls);
  assert.deepEqual(run.slice(0, 3).map((call) => [call.method, call.request]), [
    ["snapshot", { recordType: "steps", snapshotId: old.snapshotId, position: progress.position }],
    ["issue", null],
    ["snapshot", { recordType: "steps", snapshotId: current.snapshotId, position: null }],
  ]);
  assert.equal(run.filter((call) => (call.request as SnapshotReadRequest | null)?.snapshotId === old.snapshotId).length, 1);
  assert.deepEqual(outcomes(result).steps, ["completed", null, true]);
});

test("an_expired_token_before_baseline_restarts_the_initial_snapshot", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 2);
  f.sources.callCostMs = 60_000;
  await ran(f.sync.trigger({ kind: "background", budgetMs: 100_000 }));
  f.sources.callCostMs = 0;
  const old = await attemptOf(f);
  assert.notEqual(await cycleCompletion(f.database, old.snapshotId), null);
  f.sources.next("change", "steps", { outcome: "token-expired", failure: "changes-token-too-old" });
  await foreground(f);
  const current = await attemptOf(f);
  assert.notEqual(current.snapshotId, old.snapshotId);
  assert.equal(current.kind, "initial");
  assert.notEqual((await snapshotAttempt(f.database, old.snapshotId))!.abandonedAtMs, null);
});

test("an_expired_token_after_baseline_reconciles", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 2);
  await foreground(f);
  const old = await attemptOf(f);
  f.sources.expire("steps");
  const calls = f.sources.calls.length;
  const result = await foreground(f);
  const current = await attemptOf(f);
  assert.equal(current.kind, "reconciliation");
  assert.notEqual(current.snapshotId, old.snapshotId);
  assert.notEqual((await snapshotAttempt(f.database, old.snapshotId))!.abandonedAtMs, null);
  assert.deepEqual(f.sources.calls.slice(calls).find((call) => call.method === "snapshot")!.request,
    { recordType: "steps", snapshotId: current.snapshotId, position: null });
  const completions = (await decoded(f)).filter((entry) => entry.kind === "snapshot-completion");
  assert.deepEqual(completions.map((entry) => entry.message.snapshot_kind), ["initial", "reconciliation"]);
  assert.deepEqual(outcomes(result).steps, ["completed", null, true]);
});

test("an_invalid_position_during_reconciliation_reconciles_again", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 6);
  await foreground(f);
  f.sources.expire("steps");
  f.sources.callCostMs = 60_000;
  await ran(f.sync.trigger({ kind: "background", budgetMs: 150_000 }));
  f.sources.callCostMs = 0;
  const reconciling = await attemptOf(f);
  assert.equal(reconciling.kind, "reconciliation");
  assert.equal((await cycleProgress(f.database, reconciling.snapshotId))!.pagesStaged, 1);
  f.sources.next("snapshot", "steps", { outcome: "invalid-position", failure: "foreign-position" });
  await foreground(f);
  const current = await attemptOf(f);
  assert.equal(current.kind, "reconciliation");
  assert.notEqual(current.snapshotId, reconciling.snapshotId);
  assert.notEqual((await snapshotAttempt(f.database, reconciling.snapshotId))!.abandonedAtMs, null);
});

test("expiry_is_reported_as_changes_token_expired", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 6);
  await foreground(f);
  f.sources.expire("steps");
  f.sources.callCostMs = 60_000;
  const result = await ran(f.sync.trigger({ kind: "background", budgetMs: 150_000 }));
  assert.deepEqual(outcomes(result).steps, ["progressed", "changes-token-expired", true]);
});

// R15 — Projection Refusal blocks

test("a_refused_page_blocks_only_its_type", async (t) => {
  const f = await fixture(t, { types: ["steps", "weight"], concurrency: 1 });
  const { attempt, progress } = await partway(f);
  f.sources.seed("weight", 2);
  f.sources.next("snapshot", "steps", REFUSAL);
  const result = await foreground(f);
  assert.deepEqual(outcomes(result), { steps: ["blocked", "projection-refusal", false], weight: ["completed", null, true] });
  assert.deepEqual(await typeBlock(f.database, "steps"), {
    recordType: "steps", cause: "projection-refusal", failure: "non-finite-value", path: "/record/samples/0/beats_per_minute", retryRequested: false,
  });
  assert.equal(await typeBlock(f.database, "weight"), null);
  assert.deepEqual(await cycleProgress(f.database, attempt.snapshotId), progress);
  // A refusal after progress in the same run is reported as progressed.
  const g = await fixture(t, { types: ["steps"] });
  g.sources.seed("steps", 4);
  g.sources.next("snapshot", "steps", REFUSAL, 1);
  assert.deepEqual(outcomes(await foreground(g)).steps, ["progressed", "projection-refusal", true]);
});

test("a_blocked_type_spends_no_slot", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps"], concurrency: 1 });
  f.sources.next("snapshot", "heart-rate", REFUSAL);
  await foreground(f);
  assert.equal((await ring(f.database))[0], "heart-rate");
  const calls = f.sources.calls.length;
  f.sources.put("steps", "s1");
  const result = await foreground(f);
  assert.deepEqual(f.sources.calls.slice(calls).map((call) => [call.method, call.recordType]), [["change", "steps"]]);
  assert.deepEqual(outcomes(result)["heart-rate"], ["blocked", "projection-refusal", false]);
});

test("block_identities_are_health_data_free", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps"], concurrency: 1 });
  f.sources.seed("heart-rate", 4, "hr-marker");
  f.sources.seed("steps", 2, "steps-marker");
  f.sources.next("snapshot", "heart-rate", REFUSAL, 1);
  await foreground(f);
  f.sources.put("steps", "steps-marker-9");
  f.sources.next("change", "steps", { outcome: "projection-refused", failure: "unrepresentable-text", path: "/record/title" });
  await foreground(f);
  assert.equal((await typeBlock(f.database, "heart-rate"))!.cause, "projection-refusal");
  assert.deepEqual(await typeBlock(f.database, "steps"), {
    recordType: "steps", cause: "projection-refusal", failure: "unrepresentable-text", path: "/record/title", retryRequested: false,
  });
  const rows = await syncRows(f.database);
  for (const marker of ["synthetic-position", "synthetic-token", "synthetic:", "hr-marker", "steps-marker"]) {
    assert.equal(rows.includes(marker), false, marker);
  }
});

test("an_identical_block_stays_blocked_across_runs", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 4);
  f.sources.next("snapshot", "steps", REFUSAL, 1);
  await foreground(f);
  const calls = f.sources.calls.length;
  for (let run = 0; run < 2; run++) {
    assert.deepEqual(outcomes(await ran(f.sync.trigger({ kind: "background" }))).steps, ["blocked", "projection-refusal", false]);
  }
  assert.equal(f.sources.calls.length, calls);
});

// R16 — Projection Refusal retries

test("a_changed_version_tuple_retries_once", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 4);
  f.sources.next("snapshot", "steps", REFUSAL, 1);
  await foreground(f);
  const refused = f.sources.callsOf("snapshot").at(-1)!.request;
  f.implementationVersion = "synthetic-build-2";
  f.sources.next("snapshot", "steps", REFUSAL);
  let calls = f.sources.calls.length;
  assert.deepEqual(outcomes(await ran(f.make().trigger({ kind: "background" }))).steps, ["blocked", "projection-refusal", false]);
  assert.deepEqual(f.sources.calls.slice(calls).map((call) => [call.method, call.request]), [["snapshot", refused]]);
  calls = f.sources.calls.length;
  await ran(f.make().trigger({ kind: "background" }));
  assert.equal(f.sources.calls.length, calls);
  f.implementationVersion = "synthetic-build-3";
  const result = await ran(f.make().trigger({ kind: "background" }));
  assert.deepEqual(f.sources.calls[calls].request, refused);
  assert.equal(await typeBlock(f.database, "steps"), null);
  assert.deepEqual(outcomes(result).steps, ["completed", null, true]);
});

/** heart-rate is never permitted, so it never stages a page and stays at the head of the ring. */
async function blockedBehindHead(f: Fixture) {
  f.sources.seed("steps", 4);
  f.sources.next("issue", "heart-rate", NOT_PERMITTED);
  f.sources.next("snapshot", "steps", REFUSAL, 1);
  await foreground(f);
  assert.equal((await typeBlock(f.database, "steps"))!.cause, "projection-refusal");
  assert.deepEqual(await ring(f.database), ["heart-rate", "steps"]);
  f.sources.next("issue", "heart-rate", NOT_PERMITTED);
  return f.sources.callsOf("snapshot", "steps").at(-1)!.request;
}

test("an_operator_retry_reads_once_with_priority", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps"], concurrency: 1 });
  const refused = await blockedBehindHead(f);
  f.sources.next("snapshot", "steps", REFUSAL);
  let requested: boolean | null = null;
  f.sources.onCall = async () => { requested ??= (await typeBlock(f.database, "steps"))!.retryRequested; };
  const calls = f.sources.calls.length;
  const result = await ran(f.sync.requestRetry("steps"));
  assert.equal(requested, true);
  assert.deepEqual(f.sources.calls.slice(calls).map((call) => [call.method, call.recordType, call.request]),
    [["snapshot", "steps", refused], ["issue", "heart-rate", null]]);
  assert.equal(result.summary.trigger, "manual");
  assert.deepEqual(outcomes(result), { "heart-rate": ["not-permitted", null, false], steps: ["blocked", "projection-refusal", false] });
  assert.equal((await typeBlock(f.database, "steps"))!.retryRequested, false);
});

test("an_identical_repeat_restores_the_block", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps"], concurrency: 1 });
  await blockedBehindHead(f);
  f.sources.next("snapshot", "steps", REFUSAL);
  await ran(f.sync.requestRetry("steps"));
  f.sources.next("issue", "heart-rate", NOT_PERMITTED);
  const calls = f.sources.calls.length;
  const result = await foreground(f);
  assert.deepEqual(f.sources.calls.slice(calls).map((call) => call.recordType), ["heart-rate"]);
  assert.deepEqual(outcomes(result).steps, ["blocked", "projection-refusal", false]);
});

test("a_successful_retry_clears_the_block", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps"], concurrency: 1 });
  const refused = await blockedBehindHead(f);
  const calls = f.sources.calls.length;
  const result = await ran(f.sync.requestRetry("steps"));
  assert.deepEqual(f.sources.calls[calls].request, refused);
  assert.equal(await typeBlock(f.database, "steps"), null);
  assert.deepEqual(outcomes(result).steps, ["completed", null, true]);
});

test("a_transient_retry_failure_keeps_the_request", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps"], concurrency: 1 });
  const refused = await blockedBehindHead(f);
  f.sources.next("snapshot", "steps", { outcome: "read-failed", failure: "health-connect-unavailable" });
  const failed = await ran(f.sync.requestRetry("steps"));
  assert.deepEqual(outcomes(failed).steps, ["failed", "read-failed", false]);
  assert.equal((await typeBlock(f.database, "steps"))!.retryRequested, true);
  f.sources.next("issue", "heart-rate", NOT_PERMITTED);
  f.sources.next("snapshot", "steps", REFUSAL);
  const calls = f.sources.calls.length;
  const result = await foreground(f);
  assert.deepEqual(f.sources.calls.slice(calls).filter((call) => call.recordType === "steps").map((call) => call.request), [refused]);
  assert.deepEqual(outcomes(result).steps, ["blocked", "projection-refusal", false]);
  assert.equal((await typeBlock(f.database, "steps"))!.retryRequested, false);
});

test("retry_without_a_block_is_refused", async (t) => {
  const f = await fixture(t, { types: ["steps", "weight"] });
  f.context = stagingContext({ maxMessageBytes: 4_096 });
  f.sources.seed("steps", 1);
  f.sources.seed("weight", 1);
  f.sources.next("snapshot", "weight", oversized);
  await foreground(f);
  assert.equal((await typeBlock(f.database, "weight"))!.cause, "record-too-large");
  const before = await contents(f.database);
  const calls = f.sources.calls.length;
  for (const type of ["steps", "weight"]) {
    assert.deepEqual(await f.sync.requestRetry(type), { outcome: "refused", failure: "retry-not-blocked" });
  }
  assert.deepEqual(await contents(f.database), before);
  assert.equal(f.sources.calls.length, calls);
});

// R17 — Oversized records

test("an_oversized_record_blocks_its_type", async (t) => {
  const f = await fixture(t, { types: ["steps", "weight"] });
  f.context = stagingContext({ maxMessageBytes: 4_096 });
  f.sources.seed("steps", 3);
  f.sources.seed("weight", 1);
  f.sources.next("snapshot", "steps", oversized);
  const first = await foreground(f);
  assert.deepEqual(outcomes(first), { steps: ["progressed", "record-too-large", false], weight: ["completed", null, true] });
  assert.deepEqual(await typeBlock(f.database, "steps"), {
    recordType: "steps", cause: "record-too-large", failure: "indivisible-record-limit-exceeded", path: null, retryRequested: false,
  });
  const calls = f.sources.calls.length;
  const second = await ran(f.sync.trigger({ kind: "background" }));
  assert.deepEqual(f.sources.calls.slice(calls).map((call) => call.recordType), ["weight"]);
  assert.deepEqual(outcomes(second).steps, ["blocked", "record-too-large", false]);
});

test("a_new_generation_retries_an_oversized_record", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  f.context = stagingContext({ maxMessageBytes: 4_096 });
  f.sources.seed("steps", 3);
  f.sources.next("snapshot", "steps", oversized);
  await foreground(f);
  f.context = stagingContext({ generation: 7, maxMessageBytes: 65_536 });
  f.sources.next("snapshot", "steps", oversized);
  const result = await foreground(f);
  assert.equal(await typeBlock(f.database, "steps"), null);
  assert.deepEqual(outcomes(result).steps, ["completed", null, true]);
});

// R18 — Read failures and bridge payloads

test("read_failures_retry_next_run", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  const { attempt, progress } = await partway(f);
  f.sources.next("snapshot", "steps", { outcome: "read-failed", failure: "health-connect-io-failure" });
  const failed = await foreground(f);
  assert.deepEqual(outcomes(failed).steps, ["failed", "read-failed", false]);
  assert.deepEqual(await cycleProgress(f.database, attempt.snapshotId), progress);
  const result = await foreground(f);
  const [refused, retried] = f.sources.callsOf("snapshot", "steps").slice(-3, -1);
  assert.deepEqual(retried.request, refused.request);
  assert.deepEqual(outcomes(result).steps, ["completed", null, true]);
});

test("malformed_reader_results_are_read_failures", async (t) => {
  const page = { outcome: "page", projectionSchemaVersion: 1, projections: [utf8("synthetic:x")], nextPosition: utf8("synthetic-position:x"), exhausted: false };
  const snapshots: unknown[] = [
    null, "page", { outcome: "skipped" }, { ...page, projectionSchemaVersion: 2 }, { ...page, projections: [new Uint8Array(0)] },
    { ...page, nextPosition: "synthetic-position" }, { ...page, exhausted: "yes" }, { ...page, projections: "x" },
    { outcome: "not-permitted", failure: "permission-denied" }, { outcome: "invalid-position", failure: "expired-position" },
    { outcome: "token-expired", failure: "changes-token-expired" }, { outcome: "projection-refused", failure: "unknown-failure", path: "" },
    { outcome: "projection-refused", failure: "non-finite-value", path: "/record/value=72" }, { outcome: "projection-refused", failure: "non-finite-value" },
  ];
  const f = await fixture(t, { types: ["steps"] });
  const { attempt, progress } = await partway(f);
  for (const [index, payload] of snapshots.entries()) {
    f.sources.next("snapshot", "steps", payload);
    assert.deepEqual(outcomes(await foreground(f)).steps, ["failed", "read-failed", false], `snapshot case ${index}`);
    assert.deepEqual(await cycleProgress(f.database, attempt.snapshotId), progress);
    assert.equal((await attemptOf(f)).snapshotId, attempt.snapshotId);
    assert.equal(await typeBlock(f.database, "steps"), null);
  }
  const change = { outcome: "page", projectionSchemaVersion: 1, items: [], nextChangesToken: "synthetic-token", nextUnreadSinceMs: START, hasMore: false };
  const changes: unknown[] = [
    { ...change, items: [{ kind: "retraction", sourceIdentity: "bad\u0000" }] }, { ...change, items: [{ kind: "retraction", sourceIdentity: "\ud800" }] },
    { ...change, items: [{ kind: "retraction", sourceIdentity: "" }] }, { ...change, items: [{ kind: "projection", bytes: new Uint8Array(0) }] },
    { ...change, items: [{ kind: "upsert", bytes: utf8("synthetic:x") }] }, { ...change, nextChangesToken: "" }, { ...change, nextUnreadSinceMs: 1.5 },
    { ...change, hasMore: "no" }, { ...change, projectionSchemaVersion: 2 }, { outcome: "token-expired", failure: "token-gone" },
    { outcome: "invalid-position", failure: "malformed-position" }, { outcome: "not-permitted", failure: "boundary-group-too-large" },
  ];
  const g = await fixture(t, { types: ["steps"] });
  await foreground(g);
  const origin = await attemptOf(g);
  g.sources.put("steps", "c1");
  for (const [index, payload] of changes.entries()) {
    g.sources.next("change", "steps", payload);
    assert.deepEqual(outcomes(await foreground(g)).steps, ["failed", "read-failed", false], `change case ${index}`);
    const cycleId = (g.sources.callsOf("change").at(-1)!.request as ChangeReadRequest).cycleId;
    assert.equal(await cycleProgress(g.database, cycleId), null);
    assert.equal((await attemptOf(g)).snapshotId, origin.snapshotId);
    assert.equal(await typeBlock(g.database, "steps"), null);
  }
});

test("throwing_seams_are_read_failures", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps", "weight"] });
  const thrown = () => { throw new Error(`${SENTINEL} bridge failure`); };
  f.sources.next("issue", "heart-rate", thrown);
  f.sources.next("snapshot", "steps", thrown);
  const first = await foreground(f);
  assert.deepEqual(outcomes(first), {
    "heart-rate": ["failed", "read-failed", false], steps: ["progressed", "read-failed", false], weight: ["completed", null, false],
  });
  f.sources.next("change", "weight", thrown);
  const second = await foreground(f);
  assert.deepEqual(outcomes(second), {
    "heart-rate": ["completed", null, false], steps: ["completed", null, false], weight: ["failed", "read-failed", false],
  });
  assert.equal(JSON.stringify([first, second]).includes(SENTINEL), false);
});

// R19 — Run failures

test("cipher_failures_leave_the_run_open", async (t) => {
  const cases: [(f: Fixture) => Partial<SenderSyncOptions>, string][] = [
    [(f) => { f.context = stagingContext({}, { keyAlias: "synthetic-missing-key" }); return {}; }, "installation-key-missing"],
    [(f) => { f.cipher.invalidate(KEY_ALIAS); return {}; }, "installation-key-unusable"],
    [(f) => ({ cipher: new SpyCipher(f.cipher, { 1: { returns: { status: "error", reason: "keystore-unavailable" } } }) }), "keystore-unavailable"],
    [(f) => ({ cipher: new SpyCipher(f.cipher, { 1: { throws: new Error(`${SENTINEL} bridge failure`) } }) }), "unexpected-cipher-failure"],
  ];
  for (const [arrange, failure] of cases) {
    const f = await fixture(t, { types: ["steps", "weight"], concurrency: 1 });
    f.sources.seed("steps", 4);
    f.sources.seed("weight", 4);
    const overrides = arrange(f);
    assert.deepEqual(await f.make(overrides).trigger({ kind: "foreground" }), { outcome: "failed", failure });
    assert.deepEqual(f.sources.calls.map((call) => [call.method, call.recordType]), [["issue", "steps"], ["snapshot", "steps"]], failure);
    assert.equal((await f.database.query("SELECT * FROM sync_runs")).length, 1);
    assert.equal(await runCheckpoint(f.database, INSTALLATION, 1), null);
  }
});

test("storage_failures_leave_the_run_open", async (t) => {
  // The staging unit's own commit fails.
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 2);
  assert.deepEqual(await f.make({ database: failingStatements(f.database, /INSERT INTO outbox_messages/) }).trigger({ kind: "foreground" }),
    { outcome: "failed", failure: "storage-failed" });
  assert.equal((await f.database.query("SELECT * FROM sync_runs")).length, 1);
  assert.deepEqual(await outboxMessages(f.database), []);
  // This unit's own writes fail after the run opens.
  const g = await fixture(t, { types: ["steps"] });
  g.sources.seed("steps", 2);
  assert.deepEqual(await g.make({ database: failingStatements(g.database, /^\s*(?:INSERT|UPDATE|DELETE|REPLACE)\b[\s\S]*\bsync_/i, 1) }).trigger({ kind: "foreground" }),
    { outcome: "failed", failure: "storage-failed" });
  assert.equal((await g.database.query("SELECT * FROM sync_runs")).length, 1);
  assert.equal(await runCheckpoint(g.database, INSTALLATION, 1), null);
  assert.deepEqual((await foreground(g)).runSequence, 2);
  assert.equal((await checkpointMessages(g)).length, 2);
});

test("staging_conflicts_retry_twice", async (t) => {
  for (const [conflicts, expected] of [[2, "checkpointed"], [3, "failed"]] as const) {
    const f = await fixture(t, { types: ["steps"] });
    f.sources.seed("steps", 1);
    const script: Record<number, { before: () => Promise<void> }> = {};
    for (let seal = 1; seal <= conflicts; seal++) script[seal] = { before: () => queue(f.database, INSTALLATION, ["capability-acknowledgement", 7]) };
    const result = await f.make({ cipher: new SpyCipher(f.cipher, script) }).trigger({ kind: "foreground" });
    const attempt = await attemptOf(f);
    if (expected === "checkpointed") {
      assert.equal(result.outcome, "checkpointed", JSON.stringify(result));
      assert.equal((await cycleProgress(f.database, attempt.snapshotId))!.pagesStaged, 1);
    } else {
      assert.deepEqual(result, { outcome: "failed", failure: "staging-conflict" });
      assert.equal(await cycleProgress(f.database, attempt.snapshotId), null);
      assert.equal(await runCheckpoint(f.database, INSTALLATION, 1), null);
    }
  }
});

test("dependency_refusals_are_state_mismatches", async (t) => {
  // Another writer stages the page the coordinator is reading.
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 2);
  const held = f.sources.hold("snapshot", "steps");
  const pending = f.sync.trigger({ kind: "foreground" });
  await held.arrived;
  const attempt = await attemptOf(f);
  assert.equal((await stageOutboxBatch(f.database, f.cipher, f.context, {
    source: "snapshot", recordType: "steps", cycleId: attempt.snapshotId, projectionSchemaVersion: 1, pageIndex: 1,
    readFrom: null, projections: [utf8("synthetic:other-writer")], nextPosition: utf8(`synthetic-position:${attempt.snapshotId}:1`), exhausted: false,
  })).outcome, "staged");
  held.release();
  assert.deepEqual(await pending, { outcome: "failed", failure: "sync-state-mismatch" });
  // Another writer begins the attempt the coordinator is beginning.
  const g = await fixture(t, { types: ["steps"] });
  const issuing = g.sources.hold("issue", "steps");
  const begun = g.sync.trigger({ kind: "foreground" });
  await issuing.arrived;
  const issuer = { issue: async () => ({ outcome: "issued" as const, changesToken: "synthetic-token:other:0:0", unreadSinceMs: START }) };
  assert.equal((await beginInitialSnapshot(g.database, issuer, { recordType: "steps", installationId: INSTALLATION },
    { randomUUID: () => "bbbbbbbb-0000-4000-8000-000000000001", now: () => START })).outcome, "begun");
  issuing.release();
  assert.deepEqual(await begun, { outcome: "failed", failure: "sync-state-mismatch" });
});

test("a_failed_run_is_recovered_next_time", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 4);
  const unusable = new SpyCipher(f.cipher, { 2: { returns: { status: "error", reason: "unusable-key" } } });
  assert.deepEqual(await f.make({ cipher: unusable }).trigger({ kind: "foreground" }), { outcome: "failed", failure: "installation-key-unusable" });
  assert.equal((await foreground(f)).runSequence, 2);
  const [interrupted, second] = await checkpointMessages(f);
  assert.deepEqual(interrupted.record_types, [{ record_type: "steps", outcome: "interrupted", source_data_staged: true }]);
  assert.deepEqual(second.record_types, [{ record_type: "steps", outcome: "completed", source_data_staged: true }]);
});

// R20 — Outcomes and reasons

test("every_outcome_row_is_reachable", async (t) => {
  const f = await fixture(t, { types: NAMED_TYPES, concurrency: 1 });
  f.context = stagingContext({ maxMessageBytes: 4_096 });
  f.sources.seed("bravo", 2);
  f.sources.seed("echo", 4);
  f.sources.seed("foxtrot", 2);
  f.sources.next("issue", "charlie", NOT_PERMITTED);
  f.sources.next("issue", "delta", { outcome: "read-failed", failure: "health-connect-io-failure" });
  f.sources.next("snapshot", "echo", REFUSAL);
  f.sources.next("snapshot", "foxtrot", oversized);
  await foreground(f);
  f.sources.put("bravo", "b3");
  await foreground(f);
  f.sources.put("alpha", ...Array.from({ length: 9 }, (_, i) => `a${i}`));
  f.sources.callCostMs = 60_000;
  await ran(f.sync.trigger({ kind: "background", budgetMs: 100_000 }));
  f.sources.callCostMs = 0;
  for (const conditions of [{ collectionEnabled: false }, { secureLock: false }, { storagePressure: true }, { deliveryBlocked: true }]) {
    Object.assign(f.conditions, CLEAR, conditions);
    await ran(f.sync.trigger({ kind: "background" }));
  }
  Object.assign(f.conditions, CLEAR);
  const held = f.sources.hold("change", "alpha");
  void f.sync.trigger({ kind: "background" });
  await held.arrived;
  await f.reopen();
  await foreground(f);
  const observed = new Set<string>();
  for (const message of await checkpointMessages(f)) {
    for (const entry of message.record_types as Message[]) observed.add(`${entry.outcome}/${entry.reason ?? "-"}`);
  }
  for (const row of [
    "no-change/-", "completed/-", "progressed/time-budget-reached", "progressed/projection-refusal", "progressed/record-too-large",
    "deferred/time-budget-reached", "deferred/collection-disabled", "blocked/screen-lock-removed", "blocked/storage-pressure",
    "blocked/delivery-blocked", "blocked/projection-refusal", "blocked/record-too-large", "not-permitted/-", "failed/read-failed", "interrupted/-",
  ]) assert.ok(observed.has(row), row);
  assert.equal([...observed].some((row) => row.startsWith("not-due/")), false);
});

test("source_data_staged_counts_items", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps", "weight"] });
  f.sources.seed("steps", 1);
  assert.deepEqual(outcomes(await foreground(f)), {
    "heart-rate": ["completed", null, false], steps: ["completed", null, true], weight: ["completed", null, false],
  });
  f.sources.remove("steps", "rec-0001");
  assert.deepEqual(outcomes(await foreground(f)), {
    "heart-rate": ["no-change", null, false], steps: ["completed", null, true], weight: ["no-change", null, false],
  });
});

test("reported_pairs_satisfy_the_protocol", async (t) => {
  const f = await fixture(t, { types: FOUR_TYPES });
  let seed = 7;
  const random = () => { seed = (seed * 1_103_515_245 + 12_345) % 2_147_483_648; return seed / 2_147_483_648; };
  const results: Checkpointed[] = [];
  for (let run = 0; run < 16; run++) {
    Object.assign(f.conditions, CLEAR, random() < 0.2 ? { collectionEnabled: false } : {}, random() < 0.1 ? { deliveryBlocked: true } : {});
    for (const type of FOUR_TYPES) {
      if (random() < 0.5) f.sources.seed(type, Math.floor(random() * 5));
      if (random() < 0.15) f.sources.next("snapshot", type, { outcome: "read-failed", failure: "health-connect-unavailable" });
      if (random() < 0.15) f.sources.next("change", type, NOT_PERMITTED);
      if (random() < 0.1) f.sources.expire(type);
    }
    f.sources.callCostMs = random() < 0.5 ? 30_000 : 0;
    const budget = random() < 0.5 ? { budgetMs: 100_000 } : {};
    results.push(await ran(f.sync.trigger({ kind: random() < 0.5 ? "background" : "foreground", ...budget })));
  }
  const required = new Set(["deferred", "blocked", "failed"]);
  const forbidden = new Set(["not-due", "no-change", "completed", "not-permitted"]);
  const messages = await checkpointMessages(f);
  assert.equal(messages.length, results.length);
  for (const [index, message] of messages.entries()) {
    const entries = message.record_types as Message[];
    assert.deepEqual(entries.map((entry) => entry.record_type), FOUR_TYPES);
    for (const entry of entries) {
      assert.notEqual(entry.outcome, "not-due");
      if (required.has(entry.outcome as string)) assert.equal(typeof entry.reason, "string", JSON.stringify(entry));
      if (forbidden.has(entry.outcome as string)) assert.equal("reason" in entry, false, JSON.stringify(entry));
    }
    assert.deepEqual(entries, results[index].summary.recordTypes.map((entry) => ({
      record_type: entry.recordType, outcome: entry.outcome, ...(entry.reason ? { reason: entry.reason } : {}), source_data_staged: entry.sourceDataStaged,
    })));
  }
});

// R21 — The Sender schema migration

const DDL = [
  `CREATE TABLE sync_types (
    record_type TEXT PRIMARY KEY NOT NULL, ring_order INTEGER NOT NULL UNIQUE CHECK (ring_order > 0),
    change_cycle_id TEXT, change_snapshot_id TEXT, established INTEGER NOT NULL CHECK (established IN (0, 1)),
    CHECK ((change_cycle_id IS NULL) = (change_snapshot_id IS NULL))) STRICT`,
  `CREATE TABLE sync_blocks (
    record_type TEXT PRIMARY KEY NOT NULL REFERENCES sync_types (record_type),
    cause TEXT NOT NULL CHECK (cause IN ('projection-refusal', 'record-too-large')),
    identity TEXT NOT NULL, failure TEXT NOT NULL, path TEXT,
    retry_requested INTEGER NOT NULL CHECK (retry_requested IN (0, 1)),
    CHECK ((cause = 'projection-refusal') = (path IS NOT NULL)),
    CHECK (cause = 'projection-refusal' OR retry_requested = 0)) STRICT`,
  `CREATE TABLE sync_runs (
    installation_id TEXT NOT NULL, run_sequence INTEGER NOT NULL CHECK (run_sequence > 0),
    trigger TEXT NOT NULL CHECK (trigger IN ('background', 'foreground', 'manual')),
    started_at_ms INTEGER NOT NULL,
    background_scheduling TEXT NOT NULL CHECK (background_scheduling IN ('available', 'restricted', 'unregistered')),
    PRIMARY KEY (installation_id, run_sequence)) STRICT`,
  `CREATE TABLE sync_run_types (
    installation_id TEXT NOT NULL, run_sequence INTEGER NOT NULL, record_type TEXT NOT NULL,
    PRIMARY KEY (installation_id, run_sequence, record_type),
    FOREIGN KEY (installation_id, run_sequence) REFERENCES sync_runs (installation_id, run_sequence)) STRICT`,
  `CREATE TABLE sync_run_cycles (
    installation_id TEXT NOT NULL, run_sequence INTEGER NOT NULL, record_type TEXT NOT NULL, cycle_id TEXT NOT NULL,
    records_before INTEGER NOT NULL CHECK (records_before >= 0),
    PRIMARY KEY (installation_id, run_sequence, record_type, cycle_id),
    FOREIGN KEY (installation_id, run_sequence, record_type)
      REFERENCES sync_run_types (installation_id, run_sequence, record_type)) STRICT`,
];
const normalize = (sql: DatabaseValue): DatabaseValue => typeof sql === "string" ? sql.replace(/\s+/g, " ").replace(/\( /g, "(").replace(/ \)/g, ")").trim() : sql;

async function schemaOf(database: SenderDatabase): Promise<DatabaseRow[]> {
  return (await database.query("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"))
    .map((row) => ({ ...row, sql: normalize(row.sql) }));
}

test("migration_creates_version_five", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const database = await openNodeDatabase(join(directory, "synthetic.sqlite"));
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.deepEqual(await migrate(database, MIGRATIONS.slice(0, 4)), { status: "ready", version: 4 });
  const before = await schemaOf(database);
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 5 });
  const after = await schemaOf(database);
  const key = (row: DatabaseRow) => `${row.type}:${row.name}`;
  const kept = new Set(before.map(key));
  assert.deepEqual(after.filter((row) => kept.has(key(row))), before);
  const added = after.filter((row) => !kept.has(key(row)));
  assert.deepEqual(added.filter((row) => row.type === "table").map((row) => row.sql).sort(), DDL.map(normalize).sort());
  assert.deepEqual(added.filter((row) => row.type !== "table").map((row) => [row.type, row.name, row.sql]).sort(), [
    ["index", "sqlite_autoindex_sync_blocks_1", null], ["index", "sqlite_autoindex_sync_run_cycles_1", null],
    ["index", "sqlite_autoindex_sync_run_types_1", null], ["index", "sqlite_autoindex_sync_runs_1", null],
    ["index", "sqlite_autoindex_sync_types_1", null], ["index", "sqlite_autoindex_sync_types_2", null],
  ]);
});

test("migration_is_idempotent", async (t) => {
  const f = await fixture(t);
  const before = await contents(f.database);
  assert.deepEqual(await migrate(f.database, MIGRATIONS), { status: "ready", version: 5 });
  assert.deepEqual(await contents(f.database), before);
  assert.equal(syncMigration.version, 5);
});

// R22 — Health-data-free state, order of checks, and module boundary

test("coordinator_state_is_health_data_free", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps", "weight"] });
  f.sources.seed("heart-rate", 5, "hr-marker");
  f.sources.seed("steps", 3, "steps-marker");
  f.sources.next("snapshot", "weight", REFUSAL);
  const results = [await foreground(f)];
  f.sources.put("steps", "steps-marker-new");
  f.sources.remove("heart-rate", "hr-marker-0001");
  f.sources.callCostMs = 60_000;
  results.push(await ran(f.sync.trigger({ kind: "background", budgetMs: 100_000 })));
  const rows = await syncRows(f.database);
  const reported = JSON.stringify(results);
  for (const marker of ["synthetic-position", "synthetic-token", "synthetic:", "hr-marker", "steps-marker"]) {
    assert.equal(rows.includes(marker), false, marker);
    assert.equal(reported.includes(marker), false, marker);
  }
});

test("checks_follow_the_documented_order", async (t) => {
  // 1. Caller errors before recovery: an open run stays open.
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 2);
  const held = f.sources.hold("snapshot", "steps");
  void f.sync.trigger({ kind: "background" });
  await held.arrived;
  await f.reopen();
  const before = await contents(f.database);
  await assert.rejects(f.make().trigger({ kind: "hourly" } as unknown as TriggerRequest), TypeError);
  assert.deepEqual(await contents(f.database), before);
  // 2. A run failure stops further turns.
  const g = await fixture(t, { types: ["steps", "weight"], concurrency: 1 });
  g.sources.seed("steps", 2);
  const failing = new SpyCipher(g.cipher, { 1: { returns: { status: "error", reason: "unusable-key" } } });
  assert.deepEqual(await g.make({ cipher: failing }).trigger({ kind: "foreground" }), { outcome: "failed", failure: "installation-key-unusable" });
  assert.deepEqual(g.sources.calls.map((call) => call.recordType), ["steps", "steps"]);
  // 3. The deadline before conditions.
  const h = await fixture(t, { types: ["steps"] });
  h.sources.callCostMs = 60_000;
  h.sources.onCall = (call) => { if (call.method === "issue") h.conditions.collectionEnabled = false; };
  assert.deepEqual(outcomes(await ran(h.sync.trigger({ kind: "background", budgetMs: 30_000 }))).steps, ["progressed", "time-budget-reached", false]);
  // 4. Conditions before the type's block.
  const k = await fixture(t, { types: ["steps"] });
  k.sources.next("snapshot", "steps", REFUSAL);
  await foreground(k);
  assert.equal((await typeBlock(k.database, "steps"))!.cause, "projection-refusal");
  k.conditions.storagePressure = true;
  assert.deepEqual(outcomes(await ran(k.sync.trigger({ kind: "background" }))).steps, ["blocked", "storage-pressure", false]);
});

test("the_module_stays_inside_its_boundary", async (t) => {
  const source = readFileSync(MODULE, "utf8");
  for (const table of ["outbox_messages", "outbox_high_water", "cycle_progress", "cycle_completions", "snapshot_attempts", "current_snapshot_attempts", "run_checkpoints"]) {
    assert.doesNotMatch(source, new RegExp(`\\b${table}\\b`), table);
  }
  for (const call of ["beginInitialSnapshot", "beginReconciliationSnapshot", "stageOutboxBatch", "stageSyncCompletion", "stageSyncCheckpoint"]) {
    assert.match(source, new RegExp(`\\b${call}\\s*\\(`), call);
  }
  assert.doesNotMatch(source, /\bcipher\s*\.\s*(?:seal|open|keyStatus|isDeviceSecure|create|delete)\b/);
  const specifiers = [...source.matchAll(/\b(?:from|import)\s*\(?\s*["']([^"']+)["']/g)].map((match) => match[1]);
  assert.deepEqual(specifiers.filter((specifier) => !/^(?:\.{1,2}\/|@activity-tracker-portal\/ingest-protocol$)/.test(specifier)), []);
  assert.doesNotMatch(source, /\b(?:Buffer|require|process)\b/);
  // At run time, the cipher is used only for sealing, by the staging units.
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 3);
  const guard = guarded(f.cipher);
  await ran(f.make({ cipher: guard.cipher }).trigger({ kind: "foreground" }));
  assert.deepEqual(guard.forbidden, []);
  const named = (CONTRACT.named_failures as { name: string }[]).map((failure) => failure.name);
  assert.deepEqual(named.slice().sort(), [
    "installation-key-missing", "installation-key-unusable", "keystore-unavailable", "retry-not-blocked", "staging-conflict",
    "storage-failed", "sync-state-mismatch", "unexpected-cipher-failure",
  ]);
});
