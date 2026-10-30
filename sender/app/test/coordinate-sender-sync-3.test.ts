// Acceptance tests for coordinate-sender-sync@3: quiet change reads stage nothing.
// Requirement numbers refer to packs/coordinate-sender-sync-3.spec.md. coordinate-sender-sync@2's runner runs
// unchanged beside this one, with @1's, and covers R1-R28 without the option. Storage is the 3ug node:sqlite
// SenderDatabase substitute and the cipher is the 3ug NodeInstallationCipher. The reader and issuer seams are the
// synthetic fake in support/synthetic-sync-sources.ts; every other unit is the published one, including
// stage-outbox-batch@2's change-origin advances. Fixtures migrate with an explicit list to Sender schema version 10.
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { TestContext } from "node:test";
import { decodeMessage } from "@activity-tracker-portal/ingest-protocol";
import type { Message } from "@activity-tracker-portal/ingest-protocol";
import { deliveryMigration } from "../src/delivery/send-outbox-message.ts";
import { enrollmentMigration } from "../src/enrollment/manage-sender-enrollment.ts";
import { operatorConditionsMigration } from "../src/notifications/notify-operator-conditions.ts";
import { changeOrigin, changeOriginMigration, outboxAad, outboxMessages, outboxMigration } from "../src/outbox/stage-outbox-batch.ts";
import type { StagingContext } from "../src/outbox/stage-outbox-batch.ts";
import { checkpointMigration } from "../src/outbox/stage-sync-checkpoint.ts";
import type { BackgroundScheduling } from "../src/outbox/stage-sync-checkpoint.ts";
import { completionMigration } from "../src/outbox/stage-sync-completion.ts";
import { schedulingMigration } from "../src/scheduling/schedule-background-polling.ts";
import { snapshotAttemptsMigration } from "../src/snapshot/begin-initial-snapshot.ts";
import type { DatabaseValue, SenderDatabase } from "../src/storage/database.ts";
import { migrate } from "../src/storage/migrations.ts";
import { createSenderSync, syncMigration, typeBlock } from "../src/sync/coordinate-sender-sync.ts";
import type { ChangeReadRequest, RunResult, SenderSync, SenderSyncOptions, SyncConditions } from "../src/sync/coordinate-sender-sync.ts";
import { openNodeDatabase } from "./support/node-database.ts";
import { NodeInstallationCipher } from "./support/node-installation-cipher.ts";
import { SyntheticClock, SyntheticSources } from "./support/synthetic-sync-sources.ts";

const KEY_ALIAS = "synthetic-installation-key";
const INSTALLATION = "00000000-0000-4000-8000-000000000001";
const MIGRATIONS = [
  snapshotAttemptsMigration, outboxMigration, completionMigration, checkpointMigration, syncMigration,
  schedulingMigration, deliveryMigration, enrollmentMigration, operatorConditionsMigration, changeOriginMigration,
];
const START = Date.UTC(2026, 9, 30, 9, 0, 0, 0);
const DAY = 86_400_000;
const CLEAR: SyncConditions = { collectionEnabled: true, secureLock: true, deliveryBlocked: false, storagePressure: false };
const REFUSAL = { outcome: "projection-refused", failure: "non-finite-value", path: "/record/samples/0/beats_per_minute" };

type Checkpointed = Extract<RunResult, { outcome: "checkpointed" }>;
type Mutable<T> = { -readonly [K in keyof T]: T[K] };
type Fixture = Awaited<ReturnType<typeof fixture>>;

function stagingContext(types: readonly string[]): StagingContext {
  return {
    installationId: INSTALLATION, keyAlias: KEY_ALIAS,
    generation: { generation: 6, maxRecordCount: 100, maxMessageBytes: 65_536, projectionSchemas: Object.fromEntries(types.map((type) => [type, [1]])) },
  };
}

/** A coordinator over a fresh database; `quiet` sets quietChangeReads, and `undefined` omits it. */
async function fixture(t: TestContext, options: { types?: string[]; concurrency?: number; quiet?: boolean | undefined } = {}) {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const path = join(directory, "synthetic.sqlite");
  let database = await openNodeDatabase(path);
  t.after(async () => {
    try { await database.close(); } catch { /* a test may leave a deliberately hung coordinator */ }
    rmSync(directory, { recursive: true, force: true });
  });
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 10 });
  const cipher = new NodeInstallationCipher();
  assert.equal((await cipher.create(KEY_ALIAS)).status, "ok");
  const clock = new SyntheticClock(START);
  const sources = new SyntheticSources(clock);
  const types = options.types ?? ["steps"];
  const quiet = "quiet" in options ? options.quiet : true;
  let uuid = 0;
  const f = {
    cipher, clock, sources, types,
    conditions: { ...CLEAR } as Mutable<SyncConditions>,
    scheduling: "available" as BackgroundScheduling,
    randomUUID: () => `aaaaaaaa-0000-4000-8000-${(++uuid).toString(16).padStart(12, "0")}`,
    get database() { return database; },
    options(overrides: Partial<SenderSyncOptions> = {}): SenderSyncOptions {
      return {
        database, cipher, sources, recordTypes: types,
        projection: { implementationVersion: "synthetic-build-1", schemaVersions: Object.fromEntries(types.map((type) => [type, 1])) },
        context: () => stagingContext(types), conditions: () => ({ ...f.conditions }), backgroundScheduling: () => f.scheduling,
        ...(options.concurrency === undefined ? {} : { concurrency: options.concurrency }),
        now: clock.now, monotonicNow: clock.monotonicNow, randomUUID: f.randomUUID,
        ...(quiet === undefined ? {} : { quietChangeReads: quiet }),
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

async function ran(pending: Promise<RunResult>): Promise<Checkpointed> {
  const result = await pending;
  assert.equal(result.outcome, "checkpointed", JSON.stringify(result));
  return result as Checkpointed;
}

const foreground = (f: Fixture) => ran(f.sync.trigger({ kind: "foreground" }));
const background = (f: Fixture) => ran(f.sync.trigger({ kind: "background" }));

function outcomes(result: Checkpointed): Record<string, [string, string | null, boolean]> {
  return Object.fromEntries(result.summary.recordTypes.map((entry) => [entry.recordType, [entry.outcome, entry.reason ?? null, entry.sourceDataStaged]]));
}

/** Every Outbox message, opened with its own authenticated data and decoded. */
async function decoded(f: Fixture): Promise<{ sequence: number; kind: string; message: Message }[]> {
  const result: { sequence: number; kind: string; message: Message }[] = [];
  for (const row of await outboxMessages(f.database)) {
    const plain = await f.cipher.open(KEY_ALIAS, row.sealed, outboxAad(row.installationId, row.sequence));
    assert.equal(plain.status, "ok");
    result.push({ sequence: row.sequence, kind: row.kind, message: decodeMessage((plain as { value: Uint8Array }).value) });
  }
  return result;
}

/** The whole database: the schema version, every schema object, and every row of every table, including sealed bytes. */
async function contents(database: SenderDatabase) {
  const schema = await database.query("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name");
  const tables: Record<string, unknown> = {};
  for (const { name } of schema.filter((row) => row.type === "table")) {
    tables[name as string] = await database.query(`SELECT * FROM "${name as string}" ORDER BY rowid`);
  }
  return { version: await database.query("PRAGMA user_version"), schema, tables };
}

/** The whole database with each sealed message replaced by its decoded plaintext, so two fixtures compare. */
async function plainContents(f: Fixture) {
  const whole = await contents(f.database);
  const opened = new Map((await decoded(f)).map((entry) => [entry.sequence, entry.message]));
  const outbox = whole.tables.outbox_messages as Record<string, unknown>[];
  whole.tables.outbox_messages = outbox.map((row) => ({ ...row, sealed: opened.get(row.sequence as number) }));
  return whole;
}

async function ring(database: SenderDatabase): Promise<string[]> {
  return (await database.query("SELECT record_type FROM sync_types ORDER BY ring_order")).map((row) => row.record_type as string);
}

/** Wraps a database so that every mutating statement naming `table` fails. */
function failingWritesTo(database: SenderDatabase, table: string): SenderDatabase {
  const mutation = /^\s*(?:WITH\b[\s\S]*?\b)?(INSERT|UPDATE|DELETE|REPLACE)\b/i;
  const guard = (sql: string) => { if (mutation.test(sql) && new RegExp(`\\b${table}\\b`).test(sql)) throw new Error("synthetic write failure"); };
  const wrap = (inner: SenderDatabase): SenderDatabase => ({
    async execute(sql: string, parameters?: readonly DatabaseValue[]) { guard(sql); return inner.execute(sql, parameters); },
    query: (sql: string, parameters?: readonly DatabaseValue[]) => inner.query(sql, parameters),
    transaction: <T>(fn: (transaction: SenderDatabase) => T | Promise<T>) => inner.transaction((transaction) => fn(wrap(transaction))),
    close: () => inner.close(),
  });
  return wrap(database);
}

const kinds = (entries: readonly { kind: string }[]) => entries.map((entry) => entry.kind);

/** The change-origin advances stage-outbox-batch@2 recorded, as [record type, lineage it follows]. */
async function advances(database: SenderDatabase): Promise<[string, number][]> {
  return (await database.query("SELECT record_type, after_lineage FROM change_origin_advances ORDER BY record_type"))
    .map((row) => [row.record_type as string, row.after_lineage as number]);
}

// R30 — Caller errors

test("malformed_quiet_option_is_a_caller_error", async (t) => {
  const f = await fixture(t);
  const before = await contents(f.database);
  for (const bad of ["yes", 1, 0, null, {}]) {
    assert.throws(() => f.make({ quietChangeReads: bad as never }), TypeError, JSON.stringify(bad));
  }
  assert.deepEqual(await contents(f.database), before);
  assert.equal(f.sources.calls.length, 0);
});

// R31 — A quiet read advances the origin and stages nothing

test("a_quiet_read_stages_nothing", async (t) => {
  const f = await fixture(t);
  f.sources.seed("steps", 2);
  await foreground(f);
  const before = await decoded(f);
  const changeReads = f.sources.callsOf("change").length;
  await background(f);
  assert.equal(f.sources.callsOf("change").length, changeReads + 1, "the type was read");
  const added = (await decoded(f)).slice(before.length);
  assert.deepEqual(kinds(added), ["sync-checkpoint"], "neither an Ingest Batch nor a Change Completion");
  const cycles = await f.database.query("SELECT cycle_id FROM cycle_progress WHERE cycle_kind = 'change-cycle'");
  assert.equal(cycles.length, 1, "only the catch-up cycle has progress");
});

test("the_catch_up_cycle_is_still_staged", async (t) => {
  const f = await fixture(t, { types: ["steps", "weight"] });
  f.sources.seed("steps", 2);
  const result = await foreground(f);
  const messages = await decoded(f);
  for (const type of ["steps", "weight"]) {
    const change = messages.filter((entry) => entry.message.record_type === type && entry.message.cycle_kind === "change-cycle");
    assert.equal(change.length, 1, `${type}: one empty catch-up batch`);
    assert.equal(messages.filter((entry) => entry.kind === "change-completion" && entry.message.record_type === type).length, 1, `${type}: its completion`);
  }
  assert.deepEqual(outcomes(result), { steps: ["completed", null, true], weight: ["completed", null, false] });
});

test("a_quiet_type_reports_no_change", async (t) => {
  const f = await fixture(t, { types: ["steps", "weight"] });
  f.sources.seed("steps", 2);
  await foreground(f);
  const result = await background(f);
  assert.deepEqual(outcomes(result), { steps: ["no-change", null, false], weight: ["no-change", null, false] });
  const checkpoint = (await decoded(f)).at(-1)!;
  assert.equal(checkpoint.kind, "sync-checkpoint");
  assert.deepEqual((checkpoint.message.record_types as Message[]).map((entry) => [entry.record_type, entry.outcome, entry.source_data_staged]),
    [["steps", "no-change", false], ["weight", "no-change", false]]);
});

test("a_quiet_read_moves_the_type_to_the_ring_tail", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps", "weight"], concurrency: 1 });
  await foreground(f);
  const before = await ring(f.database);
  const calls = f.sources.calls.length;
  await background(f);
  assert.deepEqual(f.sources.calls.slice(calls).map((call) => [call.method, call.recordType]),
    before.map((type) => ["change", type]), "each type takes one turn, in ring order");
  assert.deepEqual(await ring(f.database), before, "each quiet turn moved its type to the tail, in turn order");
  // A type with changes between quiet ones still rotates after them.
  f.sources.seed("steps", 1);
  await background(f);
  assert.deepEqual(await ring(f.database), before);
});

test("a_quiet_read_clears_a_retried_block", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  await foreground(f);
  f.sources.next("change", "steps", REFUSAL);
  assert.deepEqual(outcomes(await background(f)).steps, ["blocked", "projection-refusal", false]);
  assert.notEqual(await typeBlock(f.database, "steps"), null);
  const before = (await decoded(f)).length;
  const result = await ran(f.sync.requestRetry("steps") as Promise<RunResult>);
  assert.deepEqual(outcomes(result).steps, ["no-change", null, false]);
  assert.equal(await typeBlock(f.database, "steps"), null, "the quiet retry read cleared the block");
  assert.deepEqual(kinds((await decoded(f)).slice(before)), ["sync-checkpoint"]);
});

test("the_next_cycle_reads_from_the_advanced_origin", async (t) => {
  const f = await fixture(t);
  await foreground(f);
  const catchUp = (await changeOrigin(f.database, "steps"))!;
  // A later quiet read reports a later unread-since instant, so the advanced origin differs from the catch-up's.
  f.clock.advance(60_000);
  await background(f);
  const advanced = (await changeOrigin(f.database, "steps"))!;
  const quietRead = f.sources.callsOf("change").at(-1)!.request as ChangeReadRequest;
  assert.equal(quietRead.changesToken, catchUp.changesToken, "the quiet read started from the catch-up's final token");
  assert.deepEqual(await advances(f.database), [["steps", 1]]);
  assert.equal(advanced.unreadSinceMs, START + 60_000);
  f.sources.seed("steps", 1);
  const result = await background(f);
  const read = f.sources.callsOf("change").at(-1)!.request as ChangeReadRequest;
  assert.deepEqual([read.changesToken, read.unreadSinceMs], [advanced.changesToken, advanced.unreadSinceMs]);
  assert.deepEqual(outcomes(result).steps, ["completed", null, true]);
});

test("reads_with_items_or_more_pages_are_staged", async (t) => {
  const f = await fixture(t);
  await foreground(f);
  f.sources.pageSize = 1;
  f.sources.put("steps", "c1", "c2");
  const before = (await decoded(f)).length;
  const result = await background(f);
  const added = (await decoded(f)).slice(before);
  assert.deepEqual(kinds(added), ["ingest-batch", "ingest-batch", "change-completion", "sync-checkpoint"]);
  assert.deepEqual(outcomes(result).steps, ["completed", null, true]);
});

test("advance_refusals_are_handled_like_staging_refusals", async (t) => {
  // A storage failure while advancing fails the run as a storage failure while staging does.
  const results: unknown[] = [];
  for (const [quiet, table] of [[true, "change_origin_advances"], [false, "outbox_messages"]] as const) {
    const f = await fixture(t, { quiet });
    await foreground(f);
    const before = await plainContents(f);
    const failing = f.make({ database: failingWritesTo(f.database, table) });
    const result = await failing.trigger({ kind: "background" });
    results.push(result);
    const after = await plainContents(f);
    assert.deepEqual(after.tables.outbox_messages, before.tables.outbox_messages, `${table}: nothing delivered was staged`);
    assert.deepEqual(after.tables.cycle_progress, before.tables.cycle_progress, `${table}: no cycle progress`);
  }
  assert.deepEqual(results[0], results[1]);
  assert.deepEqual(results[0], { outcome: "failed", failure: "storage-failed" });
});

// R32 — An idle run stages only its checkpoint

test("an_idle_run_stages_only_its_checkpoint", async (t) => {
  const f = await fixture(t, { types: ["heart-rate", "steps", "weight"] });
  f.sources.seed("steps", 2);
  await foreground(f);
  const before = (await decoded(f)).length;
  for (let run = 0; run < 10; run++) {
    f.clock.advance(3_600_000);
    const result = await background(f);
    assert.deepEqual(Object.values(outcomes(result)).map(([outcome]) => outcome), ["no-change", "no-change", "no-change"]);
  }
  const added = (await decoded(f)).slice(before);
  assert.equal(added.length, 10);
  assert.deepEqual(new Set(kinds(added)), new Set(["sync-checkpoint"]));
});

test("quiet_types_keep_their_tokens_fresh", async (t) => {
  const f = await fixture(t);
  await foreground(f);
  for (let run = 0; run < 3; run++) {
    f.clock.advance(20 * DAY);
    await background(f);
  }
  assert.deepEqual(f.sources.callsOf("change").map((call) => (call.request as ChangeReadRequest).unreadSinceMs),
    [START, START, START + 20 * DAY, START + 40 * DAY]);
});

// R33 — Deadlines, conditions, and recovery

test("an_in_flight_quiet_read_is_advanced_after_the_deadline", async (t) => {
  const f = await fixture(t);
  await foreground(f);
  const origin = (await changeOrigin(f.database, "steps"))!;
  const before = (await decoded(f)).length;
  const held = f.sources.hold("change", "steps");
  const pending = f.sync.trigger({ kind: "background", budgetMs: 480_000 });
  await held.arrived;
  f.clock.advance(600_000);
  held.release();
  const result = await ran(pending);
  assert.deepEqual(outcomes(result).steps, ["no-change", null, false]);
  assert.deepEqual(await advances(f.database), [["steps", 1]], "the in-flight read was advanced");
  assert.equal((await changeOrigin(f.database, "steps"))!.snapshotId, origin.snapshotId);
  assert.deepEqual(kinds((await decoded(f)).slice(before)), ["sync-checkpoint"]);
  // Collection disabled while a quiet read is in flight: it is still advanced, and owes nothing.
  const g = await fixture(t);
  await foreground(g);
  const gOrigin = (await changeOrigin(g.database, "steps"))!;
  const gBefore = (await decoded(g)).length;
  const gHeld = g.sources.hold("change", "steps");
  const gPending = g.sync.trigger({ kind: "background" });
  await gHeld.arrived;
  g.conditions.collectionEnabled = false;
  gHeld.release();
  await ran(gPending);
  assert.deepEqual(await advances(g.database), [["steps", 1]], "the in-flight read was advanced");
  assert.equal((await changeOrigin(g.database, "steps"))!.snapshotId, gOrigin.snapshotId);
  assert.deepEqual(kinds((await decoded(g)).slice(gBefore)), ["sync-checkpoint"]);
});

test("recovery_stages_no_completion_for_a_quiet_read", async (t) => {
  const f = await fixture(t, { types: ["steps", "weight"], concurrency: 1 });
  await foreground(f);
  const before = (await decoded(f)).length;
  // Steps reads quietly and advances; the run then hangs on weight's read and the process dies.
  const held = f.sources.hold("change", "weight");
  void f.sync.trigger({ kind: "background" });
  await held.arrived;
  const advanced = (await changeOrigin(f.database, "steps"))!;
  await f.reopen();
  const result = await foreground(f);
  const added = (await decoded(f)).slice(before);
  assert.deepEqual(kinds(added), ["sync-checkpoint", "sync-checkpoint"], "the interrupted run's checkpoint, then this run's");
  assert.deepEqual((added[0].message.record_types as Message[]).map((entry) => entry.outcome), ["interrupted", "interrupted"]);
  assert.deepEqual(outcomes(result), { steps: ["no-change", null, false], weight: ["no-change", null, false] });
  const stepsRead = f.sources.callsOf("change", "steps").at(-1)!.request as ChangeReadRequest;
  assert.equal(stepsRead.changesToken, advanced.changesToken, "the next run read steps from its advanced origin");
});

test("a_crash_before_the_advance_rereads_from_the_old_origin", async (t) => {
  const f = await fixture(t);
  await foreground(f);
  const origin = (await changeOrigin(f.database, "steps"))!;
  const held = f.sources.hold("change", "steps");
  void f.sync.trigger({ kind: "background" });
  const call = await held.arrived;
  await f.reopen();
  assert.deepEqual(await changeOrigin(f.database, "steps"), origin, "nothing was advanced");
  await foreground(f);
  const reread = f.sources.callsOf("change", "steps").at(-1)!.request as ChangeReadRequest;
  assert.equal(reread.changesToken, (call.request as ChangeReadRequest).changesToken);
  assert.equal(reread.changesToken, origin.changesToken);
});

// R34 — The option off is exactly @2

test("without_the_option_quiet_reads_are_staged_as_in_r6", async (t) => {
  const absent = await fixture(t, { quiet: undefined });
  const off = await fixture(t, { quiet: false });
  for (const f of [absent, off]) {
    f.sources.seed("steps", 2);
    await foreground(f);
    const before = (await decoded(f)).length;
    const result = await background(f);
    assert.deepEqual(kinds((await decoded(f)).slice(before)), ["ingest-batch", "change-completion", "sync-checkpoint"]);
    assert.deepEqual(outcomes(result).steps, ["no-change", null, false]);
  }
  assert.deepEqual(await plainContents(absent), await plainContents(off));
});
