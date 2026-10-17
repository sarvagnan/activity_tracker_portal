// Acceptance tests for coordinate-sender-sync@2: absolute deadlines.
// Requirement numbers refer to packs/coordinate-sender-sync-2.spec.md. coordinate-sender-sync@1's suite runs
// unchanged beside this one and covers R1-R22 for triggers without a deadline. Storage is the 3ug node:sqlite
// SenderDatabase substitute and the cipher is the 3ug NodeInstallationCipher. The reader and issuer seams are
// the synthetic fake in support/synthetic-sync-sources.ts; every other unit is the published one. Deadline tests
// move the synthetic monotonic clock explicitly and never depend on real time.
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { TestContext } from "node:test";
import { decodeMessage } from "@activity-tracker-portal/ingest-protocol";
import type { Message } from "@activity-tracker-portal/ingest-protocol";
import type { CipherResult, InstallationCipher, InstallationKeyStatus } from "../src/installation-cipher.ts";
import { cycleProgress, outboxAad, outboxMessages, outboxMigration } from "../src/outbox/stage-outbox-batch.ts";
import type { StagingContext, StagingGeneration } from "../src/outbox/stage-outbox-batch.ts";
import { checkpointMigration } from "../src/outbox/stage-sync-checkpoint.ts";
import type { BackgroundScheduling } from "../src/outbox/stage-sync-checkpoint.ts";
import { completionMigration, cycleCompletion } from "../src/outbox/stage-sync-completion.ts";
import { currentSnapshotAttempt, snapshotAttemptsMigration } from "../src/snapshot/begin-initial-snapshot.ts";
import type { SenderDatabase } from "../src/storage/database.ts";
import { migrate } from "../src/storage/migrations.ts";
import { createSenderSync, syncMigration } from "../src/sync/coordinate-sender-sync.ts";
import type { RunResult, SenderSync, SenderSyncOptions, SyncConditions, TriggerRequest } from "../src/sync/coordinate-sender-sync.ts";
import { openNodeDatabase } from "./support/node-database.ts";
import { NodeInstallationCipher } from "./support/node-installation-cipher.ts";
import { SyntheticClock, SyntheticSources } from "./support/synthetic-sync-sources.ts";
import type { SourceMethod } from "./support/synthetic-sync-sources.ts";

const INSTALLATION = "00000000-0000-4000-8000-000000000001";
const KEY_ALIAS = "synthetic-installation-key";
const MIGRATIONS = [snapshotAttemptsMigration, outboxMigration, completionMigration, checkpointMigration, syncMigration];
const START = Date.UTC(2026, 9, 17, 9, 0, 0, 0);
const CLEAR: SyncConditions = { collectionEnabled: true, secureLock: true, deliveryBlocked: false, storagePressure: false };
const KINDS = ["background", "foreground", "manual"] as const;

type Checkpointed = Extract<RunResult, { outcome: "checkpointed" }>;
type Mutable<T> = { -readonly [K in keyof T]: T[K] };

function stagingContext(generation: Partial<StagingGeneration> = {}): StagingContext {
  return {
    installationId: INSTALLATION, keyAlias: KEY_ALIAS,
    generation: {
      generation: 6, maxRecordCount: 100, maxMessageBytes: 65_536,
      projectionSchemas: { steps: [1], weight: [1] },
      ...generation,
    },
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
    randomUUID: () => `aaaaaaaa-0000-4000-8000-${(++uuid).toString(16).padStart(12, "0")}`,
    get database() { return database; },
    options(overrides: Partial<SenderSyncOptions> = {}): SenderSyncOptions {
      return {
        database, cipher, sources, recordTypes: types,
        projection: { implementationVersion: "synthetic-build-1", schemaVersions: Object.fromEntries(types.map((type) => [type, 1])) },
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

async function ran(pending: Promise<RunResult>): Promise<Checkpointed> {
  const result = await pending;
  assert.equal(result.outcome, "checkpointed", JSON.stringify(result));
  return result as Checkpointed;
}

function outcomes(result: Checkpointed): Record<string, [string, string | null, boolean]> {
  return Object.fromEntries(result.summary.recordTypes.map((entry) => [entry.recordType, [entry.outcome, entry.reason ?? null, entry.sourceDataStaged]]));
}

const foreground = (f: Fixture) => ran(f.sync.trigger({ kind: "foreground" }));

/** Every Outbox message of the installation, opened with its own authenticated data and decoded. */
async function decoded(f: Fixture): Promise<{ sequence: number; kind: string; message: Message }[]> {
  const result: { sequence: number; kind: string; message: Message }[] = [];
  for (const row of await outboxMessages(f.database)) {
    const plain = await f.cipher.open(KEY_ALIAS, row.sealed, outboxAad(row.installationId, row.sequence));
    assert.equal(plain.status, "ok");
    result.push({ sequence: row.sequence, kind: row.kind, message: decodeMessage((plain as { value: Uint8Array }).value) });
  }
  return result;
}

async function checkpointMessages(f: Fixture): Promise<Message[]> {
  return (await decoded(f)).filter((entry) => entry.kind === "sync-checkpoint").map((entry) => entry.message);
}

/** The latest decoded checkpoint's per-type entries, as [outcome, reason, source data staged]. */
async function checkpointed(f: Fixture): Promise<Record<string, [string, string | null, boolean]>> {
  const latest = (await checkpointMessages(f)).at(-1)!;
  return Object.fromEntries((latest.record_types as Message[]).map((entry) =>
    [entry.record_type as string, [entry.outcome as string, (entry.reason as string | undefined) ?? null, entry.source_data_staged as boolean]]));
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

/**
 * The whole database with each sealed Outbox message replaced by its decoded plaintext, so two fixtures whose
 * keys and nonces differ can be compared. Every other byte, row, and schema object is compared as stored.
 */
async function plainContents(f: Fixture) {
  const whole = await contents(f.database);
  const opened = new Map((await decoded(f)).map((entry) => [entry.sequence, entry.message]));
  const outbox = whole.tables.outbox_messages as Record<string, unknown>[];
  whole.tables.outbox_messages = outbox.map((row) => ({ ...row, sealed: opened.get(row.sequence as number) }));
  return whole;
}

class SpyCipher implements InstallationCipher {
  seals = 0;
  private readonly inner: InstallationCipher;
  private readonly before: Map<number, () => void>;
  constructor(inner: InstallationCipher, before: Record<number, () => void>) {
    this.inner = inner;
    this.before = new Map(Object.entries(before).map(([call, hook]) => [Number(call), hook]));
  }
  async seal(alias: string, plaintext: Uint8Array, aad: Uint8Array): Promise<CipherResult<Uint8Array>> {
    this.before.get(++this.seals)?.();
    return this.inner.seal(alias, plaintext, aad);
  }
  create(alias: string) { return this.inner.create(alias); }
  keyStatus(alias: string): Promise<InstallationKeyStatus> { return this.inner.keyStatus(alias); }
  open(alias: string, sealed: Uint8Array, aad: Uint8Array) { return this.inner.open(alias, sealed, aad); }
  delete(alias: string) { return this.inner.delete(alias); }
  isDeviceSecure() { return this.inner.isDeviceSecure(); }
}

/** Record each source call's start on the monotonic clock, relative to `origin`. */
function starts(f: Fixture, origin = f.clock.mono): number[] {
  const seen: number[] = [];
  f.sources.onCall = () => { seen.push(f.clock.mono - origin); };
  return seen;
}

/** An established type with `changes` one-item change pages waiting. */
async function establishedWithChanges(f: Fixture, changes: number, prefix = "c") {
  await foreground(f);
  f.sources.pageSize = 1;
  f.sources.put("steps", ...Array.from({ length: changes }, (_, i) => `${prefix}${i + 1}`));
}

/**
 * Hold an active run at its one change read: a background run with a one-millisecond budget reads exactly one
 * page. Returns the run and the monotonic instant at which its read started.
 */
async function heldActiveRun(f: Fixture) {
  const held = f.sources.hold("change", "steps");
  const active = f.sync.trigger({ kind: "background", budgetMs: 1 });
  await held.arrived;
  return { active, held, m0: f.clock.mono };
}

// R24 — Caller errors

test("malformed_deadlines_are_caller_errors", async (t) => {
  const f = await fixture(t, { types: ["steps", "weight"] });
  f.sources.seed("steps", 2);
  const before = await contents(f.database);
  const bad: unknown[] = [1.5, -0.5, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY, 2 ** 53, -(2 ** 53), "5000", null, true, {}];
  for (const deadlineMs of bad) {
    for (const kind of KINDS) {
      await assert.rejects(f.sync.trigger({ kind, deadlineMs } as unknown as TriggerRequest), TypeError, `${kind} ${String(deadlineMs)}`);
    }
    await assert.rejects(f.sync.trigger({ kind: "background", budgetMs: 480_000, deadlineMs } as unknown as TriggerRequest), TypeError);
  }
  assert.equal(f.sources.calls.length, 0);
  assert.deepEqual(await contents(f.database), before);
  // A deadline may already have passed, may be zero or negative, and is allowed on every trigger kind.
  const accepted: TriggerRequest[] = [
    { kind: "background", deadlineMs: f.clock.mono - 1 },
    { kind: "foreground", deadlineMs: 0, budgetMs: 480_000 },
    { kind: "manual", deadlineMs: -1, priority: ["weight"] },
    { kind: "background", deadlineMs: -Number.MAX_SAFE_INTEGER },
  ];
  for (const [index, request] of accepted.entries()) {
    const result = await ran(f.sync.trigger(request));
    assert.deepEqual([result.runSequence, result.summary.trigger], [index + 1, request.kind]);
    assert.deepEqual(outcomes(result), { steps: ["deferred", "time-budget-reached", false], weight: ["deferred", "time-budget-reached", false] });
  }
  assert.equal(f.sources.calls.length, 0);
  // The largest safe deadline bounds nothing a test can reach.
  const unbounded = await ran(f.sync.trigger({ kind: "foreground", deadlineMs: Number.MAX_SAFE_INTEGER }));
  assert.deepEqual(outcomes(unbounded), { steps: ["completed", null, true], weight: ["completed", null, false] });
});

// R25 — The effective deadline

test("an_absolute_deadline_bounds_the_run", async (t) => {
  // 60-second calls under a deadline 120 seconds after the trigger: the third call would start exactly at
  // the deadline, so it never starts.
  for (const kind of KINDS) {
    const f = await fixture(t, { types: ["steps"] });
    f.sources.seed("steps", 20);
    f.sources.callCostMs = 60_000;
    const seen = starts(f);
    const result = await ran(f.sync.trigger({ kind, deadlineMs: f.clock.mono + 120_000 }));
    assert.deepEqual(seen, [0, 60_000], kind);
    assert.equal(result.summary.trigger, kind);
    assert.equal(result.summary.elapsedMs, 120_000);
    assert.deepEqual(outcomes(result).steps, ["progressed", "time-budget-reached", true], kind);
    assert.deepEqual(await checkpointed(f), { steps: ["progressed", "time-budget-reached", true] }, kind);
  }
});

test("the_earlier_of_budget_and_deadline_applies", async (t) => {
  // [budget, deadline after the trigger, source calls started]; each call takes 60 seconds and the run opens
  // at the trigger, so both limits count from the same instant here.
  const cases: [number, number, number][] = [
    [120_000, 300_000, 2], [300_000, 120_000, 2], [180_000, 180_000, 3], [300_000, 60_000, 1], [60_000, 300_000, 1],
  ];
  for (const [budgetMs, after, calls] of cases) {
    const f = await fixture(t, { types: ["steps"] });
    f.sources.seed("steps", 20);
    f.sources.callCostMs = 60_000;
    const seen = starts(f);
    const result = await ran(f.sync.trigger({ kind: "background", budgetMs, deadlineMs: f.clock.mono + after }));
    assert.deepEqual(seen, Array.from({ length: calls }, (_, i) => i * 60_000), `${budgetMs} ${after}`);
    assert.deepEqual(outcomes(result).steps, ["progressed", "time-budget-reached", calls > 1], `${budgetMs} ${after}`);
  }
});

test("waiting_and_recovery_count_against_the_deadline", async (t) => {
  // 1. Waiting behind an active run. The queued trigger's deadline is 200 seconds after the active run's read
  // started; that read waits 30 seconds and then takes 60, so the follow-up opens 90 seconds in and has 110 left.
  const queue = async (request: (m0: number) => TriggerRequest) => {
    const f = await fixture(t, { types: ["steps"] });
    await establishedWithChanges(f, 8);
    const calls = f.sources.calls.length;
    const { active, held, m0 } = await heldActiveRun(f);
    const queued = f.sync.trigger(request(m0));
    f.clock.advance(30_000);
    f.sources.callCostMs = 60_000;
    held.release();
    assert.deepEqual(outcomes(await ran(active)).steps, ["progressed", "time-budget-reached", true]);
    const follow = await ran(queued);
    return { follow, started: f.sources.calls.slice(calls).map((call) => call.method) };
  };
  const deadline = await queue((m0) => ({ kind: "background", deadlineMs: m0 + 200_000 }));
  assert.deepEqual(deadline.started, ["change", "change", "change"]);
  assert.deepEqual(outcomes(deadline.follow).steps, ["progressed", "time-budget-reached", true]);
  // A budget still counts from the follow-up's opening: 200 seconds allow four 60-second calls.
  const budget = await queue(() => ({ kind: "background", budgetMs: 200_000 }));
  assert.deepEqual(budget.started, ["change", "change", "change", "change", "change"]);

  // 2. Recovery and the opening reads. Reading the context takes 20 seconds and sealing the interrupted run's
  // checkpoint 50, so the run opens 70 seconds after the trigger with 30 seconds of its deadline left.
  const recover = async (request: (m1: number) => TriggerRequest) => {
    const f = await fixture(t, { types: ["steps"] });
    f.sources.seed("steps", 20);
    const held = f.sources.hold("snapshot", "steps");
    void f.sync.trigger({ kind: "background" });
    await held.arrived;
    await f.reopen();
    f.sources.callCostMs = 60_000;
    const calls = f.sources.calls.length;
    const slow = new SpyCipher(f.cipher, { 1: () => f.clock.advance(50_000) });
    const sync = f.make({ cipher: slow, context: () => { f.clock.advance(20_000); return f.context; } });
    const m1 = f.clock.mono;
    const seen = starts(f, m1);
    const result = await ran(sync.trigger(request(m1)));
    assert.deepEqual((await checkpointMessages(f)).map((message) => [message.run_sequence, (message.record_types as Message[])[0].outcome]),
      [[1, "interrupted"], [2, "progressed"]]);
    return { result, seen, calls: f.sources.calls.length - calls };
  };
  const recovered = await recover((m1) => ({ kind: "background", deadlineMs: m1 + 100_000 }));
  assert.deepEqual(recovered.seen, [70_000]);
  assert.deepEqual(outcomes(recovered.result).steps, ["progressed", "time-budget-reached", true]);
  // Recovery does not count against a budget: 100 seconds from the opening allow two calls.
  const budgeted = await recover(() => ({ kind: "background", budgetMs: 100_000 }));
  assert.deepEqual(budgeted.seen, [70_000, 130_000]);
});

test("deadline_stops_report_time_budget_reached", async (t) => {
  // The deadline passes while the coordinator reads its conditions, at each read in turn, so a stop lands before
  // every source call and owed completion in every kind of step. A started read is staged with the completion it
  // owes, so a stop after a type's final change read still catches up. Whatever read it lands on: no source call starts
  // at or after the deadline, the type reports time-budget-reached unless it caught up first, never read-failed,
  // and the decoded checkpoint says the same.
  type Setup = (f: Fixture) => Promise<void>;
  const variants: [string, Setup, string[][]][] = [
    // A new type: issuance before any work, then the snapshot and change reads after it.
    ["new type", async (f) => { f.sources.seed("steps", 2); }, [
      ["deferred"], ["progressed", "issue"], ["progressed", "issue", "snapshot"], ["completed", "issue", "snapshot", "change"],
    ]],
    // A snapshot left partway by an earlier run: its next read before any work in this run.
    ["partway snapshot", async (f) => {
      f.sources.seed("steps", 4);
      f.sources.callCostMs = 60_000;
      await ran(f.sync.trigger({ kind: "background", budgetMs: 100_000 }));
      f.sources.callCostMs = 0;
    }, [["deferred"], ["progressed", "snapshot"], ["completed", "snapshot", "change"]]],
    // An established type: change reads before and after work.
    ["established type", async (f) => { await establishedWithChanges(f, 2); }, [
      ["deferred"], ["progressed", "change"], ["completed", "change", "change"],
    ]],
    // Issuance after work: the second snapshot page reports an invalid position, which restarts the snapshot.
    ["restarted snapshot", async (f) => {
      f.sources.seed("steps", 4);
      f.sources.next("snapshot", "steps", { outcome: "invalid-position", failure: "malformed-position" }, 1);
    }, [["progressed", "issue", "snapshot", "snapshot"], ["progressed", "issue", "snapshot", "snapshot", "issue"]]],
  ];
  for (const [name, setup, expected] of variants) {
    const observed: string[][] = [];
    for (let crossing = 1; crossing <= 64; crossing++) {
      const f = await fixture(t, { types: ["steps"] });
      await setup(f);
      const calls = f.sources.calls.length;
      let reads = 0;
      const deadlineMs = f.clock.mono + 1_000;
      const lateStarts: string[] = [];
      f.sources.onCall = (call) => { if (f.clock.mono >= deadlineMs) lateStarts.push(call.method); };
      const sync = f.make({ conditions: () => {
        if (++reads === crossing) f.clock.advance(1_000);
        return { ...f.conditions };
      } });
      const result = await ran(sync.trigger({ kind: "background", deadlineMs }));
      const label = `${name}, crossing at read ${crossing}`;
      assert.deepEqual(lateStarts, [], label);
      const [outcome, reason, staged] = outcomes(result).steps;
      assert.deepEqual(await checkpointed(f), { steps: [outcome, reason, staged] }, label);
      const methods = f.sources.calls.slice(calls).map((call) => call.method as SourceMethod);
      if (reads < crossing) {
        // The deadline never passed: the run caught up.
        assert.equal(outcome, "completed", label);
        break;
      }
      assert.ok(outcome === "completed" || reason === "time-budget-reached", `${label}: ${outcome} ${reason}`);
      assert.ok(["deferred", "progressed", "completed"].includes(outcome), `${label}: ${outcome}`);
      observed.push([outcome, ...methods]);
    }
    const seen = new Set(observed.map((entry) => JSON.stringify(entry)));
    for (const stop of expected) assert.ok(seen.has(JSON.stringify(stop)), `${name}: no stop ${JSON.stringify(stop)} in ${[...seen].join(" ")}`);
  }
});

test("an_in_flight_read_is_staged_after_the_absolute_deadline", async (t) => {
  // The final snapshot page is in flight when the deadline passes: it is staged, its owed completion is
  // staged, and no change read starts.
  const f = await fixture(t, { types: ["steps"] });
  f.sources.seed("steps", 2);
  const held = f.sources.hold("snapshot", "steps");
  const pending = f.sync.trigger({ kind: "background", deadlineMs: f.clock.mono + 480_000 });
  await held.arrived;
  f.clock.advance(600_000);
  held.release();
  const result = await ran(pending);
  assert.deepEqual(outcomes(result).steps, ["progressed", "time-budget-reached", true]);
  const attempt = await currentSnapshotAttempt(f.database, "steps");
  assert.ok(attempt);
  assert.equal((await cycleProgress(f.database, attempt.snapshotId))!.finished, true);
  assert.notEqual(await cycleCompletion(f.database, attempt.snapshotId), null);
  assert.equal(f.sources.callsOf("change").length, 0);
  assert.deepEqual((await decoded(f)).map((entry) => entry.kind), ["ingest-batch", "snapshot-completion", "sync-checkpoint"]);
});

// R26 — A run that opens past its deadline

test("a_run_opening_past_its_deadline_reads_nothing_and_checkpoints", async (t) => {
  // 1. Triggered past, or exactly at, its deadline: a run marker, no source call, every type deferred.
  const f = await fixture(t, { types: ["steps", "weight"], concurrency: 1 });
  f.sources.seed("steps", 4);
  f.sources.seed("weight", 4);
  f.scheduling = "restricted";
  const deferred = { steps: ["deferred", "time-budget-reached", false], weight: ["deferred", "time-budget-reached", false] };
  for (const [index, deadlineMs] of [f.clock.mono - 1, f.clock.mono].entries()) {
    const result = await ran(f.sync.trigger({ kind: "background", budgetMs: 480_000, deadlineMs }));
    assert.equal(result.runSequence, index + 1);
    assert.deepEqual(outcomes(result), deferred);
    assert.deepEqual(await checkpointed(f), deferred);
    assert.equal(result.summary.elapsedMs, 0);
  }
  assert.deepEqual(await f.database.query("SELECT run_sequence, trigger, background_scheduling FROM sync_runs ORDER BY run_sequence"), [
    { run_sequence: 1, trigger: "background", background_scheduling: "restricted" },
    { run_sequence: 2, trigger: "background", background_scheduling: "restricted" },
  ]);
  assert.equal(f.sources.calls.length, 0);
  assert.deepEqual((await decoded(f)).map((entry) => entry.kind), ["sync-checkpoint", "sync-checkpoint"]);
  // Partway work is kept for the next run.
  const partway = await fixture(t, { types: ["steps"] });
  partway.sources.seed("steps", 6);
  partway.sources.callCostMs = 60_000;
  await ran(partway.sync.trigger({ kind: "background", budgetMs: 100_000 }));
  const calls = partway.sources.calls.length;
  assert.deepEqual(outcomes(await ran(partway.sync.trigger({ kind: "manual", deadlineMs: partway.clock.mono }))).steps,
    ["deferred", "time-budget-reached", false]);
  assert.equal(partway.sources.calls.length, calls);
  partway.sources.callCostMs = 0;
  assert.deepEqual(outcomes(await foreground(partway)).steps, ["completed", null, true]);

  // 2. Queued until its deadline passed: the follow-up opens, reads nothing, and checkpoints.
  const g = await fixture(t, { types: ["steps"] });
  await establishedWithChanges(g, 4);
  const { active, held, m0 } = await heldActiveRun(g);
  const queued = g.sync.trigger({ kind: "background", deadlineMs: m0 + 10_000 });
  g.clock.advance(20_000);
  held.release();
  await ran(active);
  const before = g.sources.calls.length;
  const follow = await ran(queued);
  assert.deepEqual(outcomes(follow), { steps: ["deferred", "time-budget-reached", false] });
  assert.equal(g.sources.calls.length, before);
  assert.deepEqual((await checkpointMessages(g)).map((message) => message.run_sequence), [1, 2, 3]);
});

// R27 — Coalesced deadlines

test("a_follow_up_keeps_the_earliest_deadline", async (t) => {
  const f = await fixture(t, { types: ["steps"] });
  await establishedWithChanges(f, 8);
  const calls = f.sources.calls.length;
  const { active, held, m0 } = await heldActiveRun(f);
  // A trigger with only a budget is not unbounded, so it keeps the earliest deadline.
  const merged = [
    f.sync.trigger({ kind: "background", deadlineMs: m0 + 1_000_000 }),
    f.sync.trigger({ kind: "foreground", deadlineMs: m0 + 130_000 }),
    f.sync.trigger({ kind: "background", deadlineMs: m0 + 400_000 }),
    f.sync.trigger({ kind: "background", budgetMs: 1_000_000 }),
  ];
  f.sources.callCostMs = 60_000;
  held.release();
  await ran(active);
  const results = await Promise.all(merged);
  for (const result of results) assert.deepEqual(result, results[0]);
  const follow = results[0] as Checkpointed;
  assert.deepEqual([follow.runSequence, follow.summary.trigger], [3, "foreground"]);
  // The follow-up opens 60 seconds in: calls start at 60 and 120 seconds, and the deadline stops a third at 180.
  assert.equal(f.sources.calls.length - calls, 1 + 2);
  assert.deepEqual(outcomes(follow).steps, ["progressed", "time-budget-reached", true]);
});

test("an_unbounded_trigger_lifts_every_limit", async (t) => {
  const run = async (requests: (m0: number) => TriggerRequest[]) => {
    const f = await fixture(t, { types: ["steps"] });
    await establishedWithChanges(f, 6);
    const calls = f.sources.calls.length;
    const { active, held, m0 } = await heldActiveRun(f);
    const merged = requests(m0).map((request) => f.sync.trigger(request));
    f.sources.callCostMs = 60_000;
    held.release();
    await ran(active);
    const results = await Promise.all(merged);
    for (const result of results) assert.deepEqual(result, results[0]);
    return { follow: results[0] as Checkpointed, calls: f.sources.calls.length - calls };
  };
  // Without an unbounded trigger, the deadline bounds the follow-up.
  const bounded = await run((m0) => [{ kind: "background", deadlineMs: m0 + 130_000 }]);
  assert.equal(bounded.calls, 1 + 2);
  assert.deepEqual(outcomes(bounded.follow).steps, ["progressed", "time-budget-reached", true]);
  // An unbounded manual or foreground trigger, before or after the bounded ones, lifts both the budget and the
  // deadline: the follow-up reads all five remaining pages and catches up.
  const lifted: ((m0: number) => TriggerRequest[])[] = [
    (m0) => [{ kind: "background", budgetMs: 50_000, deadlineMs: m0 + 130_000 }, { kind: "manual" }, { kind: "background", deadlineMs: m0 + 70_000 }],
    (m0) => [{ kind: "foreground" }, { kind: "background", deadlineMs: m0 + 70_000 }],
  ];
  for (const [index, requests] of lifted.entries()) {
    const { follow, calls } = await run(requests);
    assert.equal(calls, 1 + 5, `case ${index}`);
    assert.equal(follow.summary.trigger, index === 0 ? "manual" : "foreground");
    assert.deepEqual(outcomes(follow).steps, ["completed", null, true], `case ${index}`);
  }
});

test("a_deadline_never_shortens_the_active_run", async (t) => {
  for (const request of [{ kind: "foreground" }, { kind: "background", budgetMs: 480_000 }] as TriggerRequest[]) {
    const f = await fixture(t, { types: ["steps", "weight"], concurrency: 1 });
    f.sources.seed("steps", 4);
    f.sources.seed("weight", 4);
    const held = f.sources.hold("snapshot", "steps");
    const active = f.sync.trigger(request);
    await held.arrived;
    // A trigger whose deadline passes while the active run is still reading.
    const queued = f.sync.trigger({ kind: "background", deadlineMs: f.clock.mono + 1 });
    f.clock.advance(100_000);
    held.release();
    const one = await ran(active);
    assert.deepEqual(outcomes(one), { steps: ["completed", null, true], weight: ["completed", null, true] }, request.kind);
    const calls = f.sources.calls.length;
    const two = await ran(queued);
    assert.deepEqual(outcomes(two), { steps: ["deferred", "time-budget-reached", false], weight: ["deferred", "time-budget-reached", false] });
    assert.equal(f.sources.calls.length, calls);
  }
});

test("budgets_still_merge_as_in_r8", async (t) => {
  // With deadlines far away, the follow-up's budget is the latest among the merged triggers that have one; a
  // trigger with only a deadline neither lifts nor replaces it. 60-second calls: a 70-second budget allows two
  // calls and a 130-second budget three.
  const cases: [string, (m0: number) => TriggerRequest[], number][] = [
    ["deadline last", (m0) => [{ kind: "background", budgetMs: 130_000 }, { kind: "background", budgetMs: 70_000 },
      { kind: "background", deadlineMs: m0 + 10_000_000 }], 2],
    ["deadline first", (m0) => [{ kind: "background", deadlineMs: m0 + 10_000_000 }, { kind: "background", budgetMs: 130_000 }], 3],
    ["both", (m0) => [{ kind: "background", budgetMs: 70_000, deadlineMs: m0 + 10_000_000 }, { kind: "foreground", budgetMs: 130_000 },
      { kind: "background", deadlineMs: m0 + 20_000_000 }], 3],
  ];
  for (const [name, requests, expected] of cases) {
    const f = await fixture(t, { types: ["steps"] });
    await establishedWithChanges(f, 8);
    const calls = f.sources.calls.length;
    const { active, held, m0 } = await heldActiveRun(f);
    const merged = requests(m0).map((request) => f.sync.trigger(request));
    f.sources.callCostMs = 60_000;
    held.release();
    await ran(active);
    const results = await Promise.all(merged);
    for (const result of results) assert.deepEqual(result, results[0], name);
    assert.equal(f.sources.calls.length - calls, 1 + expected, name);
    assert.deepEqual(outcomes(results[0] as Checkpointed).steps, ["progressed", "time-budget-reached", true], name);
  }
});

// R28 — Deadlines stay in memory

test("deadlines_write_no_new_state", async (t) => {
  // A run bounded by a deadline leaves exactly the database a run bounded by the same budget leaves.
  const bounded = async (request: (mono: number) => TriggerRequest) => {
    const f = await fixture(t, { types: ["steps", "weight"], concurrency: 1 });
    f.sources.seed("steps", 20);
    f.sources.callCostMs = 60_000;
    const result = await ran(f.sync.trigger(request(f.clock.mono)));
    return { result, database: await plainContents(f), calls: f.sources.calls.length };
  };
  const byDeadline = await bounded((mono) => ({ kind: "background", deadlineMs: mono + 150_000 }));
  const byBudget = await bounded(() => ({ kind: "background", budgetMs: 150_000 }));
  assert.equal(byDeadline.calls, 3);
  assert.deepEqual(byDeadline.result, byBudget.result);
  assert.deepEqual(byDeadline.database, byBudget.database);
  // A run interrupted under a deadline is recovered as interrupted, exactly as one without a deadline.
  const recovered = async (request: TriggerRequest) => {
    const f = await fixture(t, { types: ["steps"] });
    f.sources.seed("steps", 4);
    const held = f.sources.hold("snapshot", "steps");
    void f.sync.trigger({ ...request, ...(request.deadlineMs === undefined ? {} : { deadlineMs: f.clock.mono + request.deadlineMs }) });
    await held.arrived;
    await f.reopen();
    f.clock.advance(3_600_000);
    await foreground(f);
    return plainContents(f);
  };
  const interrupted = await recovered({ kind: "background", deadlineMs: 1_000 });
  assert.deepEqual(interrupted, await recovered({ kind: "background" }));
  const tables = interrupted.tables as Record<string, unknown[]>;
  assert.deepEqual(Object.keys(tables).filter((name) => name.startsWith("sync_")).sort(),
    ["sync_blocks", "sync_run_cycles", "sync_run_types", "sync_runs", "sync_types"]);
});
