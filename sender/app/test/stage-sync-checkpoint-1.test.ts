// Acceptance tests for stage-sync-checkpoint@1.
// Requirement numbers refer to packs/stage-sync-checkpoint-1.spec.md. Storage is the 3ug node:sqlite
// SenderDatabase substitute and the cipher is the 3ug NodeInstallationCipher; the Outbox is filled
// through the published stage-outbox-batch@1 primitives.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import type { TestContext } from "node:test";
import { decodeMessage, encodeMessage } from "@activity-tracker-portal/ingest-protocol";
import type { Message } from "@activity-tracker-portal/ingest-protocol";
import type { CipherResult, InstallationCipher, InstallationKeyStatus } from "../src/installation-cipher.ts";
import { checkpointMigration, runCheckpoint, stageSyncCheckpoint } from "../src/outbox/stage-sync-checkpoint.ts";
import type { CheckpointResult, RecordTypeOutcome, RunCheckpoint, RunSummary } from "../src/outbox/stage-sync-checkpoint.ts";
import { completionMigration } from "../src/outbox/stage-sync-completion.ts";
import { appendOutboxMessages, lastOutboxSequence, outboxAad, outboxMessages, outboxMigration } from "../src/outbox/stage-outbox-batch.ts";
import type { OutboxMessage, StagingContext } from "../src/outbox/stage-outbox-batch.ts";
import { snapshotAttemptsMigration } from "../src/snapshot/begin-initial-snapshot.ts";
import type { DatabaseValue, SenderDatabase } from "../src/storage/database.ts";
import { migrate } from "../src/storage/migrations.ts";
import { openNodeDatabase } from "./support/node-database.ts";
import { NodeInstallationCipher } from "./support/node-installation-cipher.ts";

const CONTRACT = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../../../governance/scopes/stage-sync-checkpoint.1.json"), "utf8"));
const MODULE = join(dirname(fileURLToPath(import.meta.url)), "../src/outbox/stage-sync-checkpoint.ts");
const INSTALLATION = "00000000-0000-4000-8000-000000000001";
const OTHER_INSTALLATION = "00000000-0000-4000-8000-000000000002";
const KEY_ALIAS = "synthetic-installation-key";
const SENTINEL = "SENTINEL";
const MIGRATIONS = [snapshotAttemptsMigration, outboxMigration, completionMigration, checkpointMigration];
const STARTED = Date.UTC(2026, 9, 14, 9, 0, 0, 123);
const ENDED = Date.UTC(2026, 9, 14, 9, 4, 30, 7);
const utf8 = (text: string) => new TextEncoder().encode(text);

async function fixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const path = join(directory, "synthetic.sqlite");
  let database = await openNodeDatabase(path);
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 4 });
  const cipher = new NodeInstallationCipher();
  assert.equal((await cipher.create(KEY_ALIAS)).status, "ok");
  return {
    cipher,
    get database() { return database; },
    async reopen() { await database.close(); database = await openNodeDatabase(path); return database; },
  };
}

function context(overrides: Partial<StagingContext> = {}): StagingContext {
  return {
    installationId: INSTALLATION, keyAlias: KEY_ALIAS,
    generation: { generation: 6, maxRecordCount: 100, maxMessageBytes: 65_536, projectionSchemas: { steps: [1] } },
    ...overrides,
  };
}

function summary(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    runSequence: 1, trigger: "background", startedAtMs: STARTED, endedAtMs: ENDED, elapsedMs: 269_884, backgroundScheduling: "available",
    recordTypes: [
      { recordType: "weight", outcome: "no-change", sourceDataStaged: false },
      { recordType: "steps", outcome: "progressed", reason: "time-budget-reached", sourceDataStaged: true },
    ],
    ...overrides,
  };
}

/** Queue synthetic Outbox rows of known plaintext sizes through the published primitive. */
async function queue(database: SenderDatabase, installationId: string, ...messages: [OutboxMessage["kind"], number][]) {
  const last = await lastOutboxSequence(database, installationId);
  assert.equal(await database.transaction((tx) => appendOutboxMessages(tx, installationId, last,
    messages.map(([kind, messageBytes], i) => ({ kind, sealed: utf8(`synthetic-sealed-${last + i}`), messageBytes })))), true);
}

function checkpoint(f: { database: SenderDatabase; cipher: InstallationCipher }, run: RunSummary, options: { cipher?: InstallationCipher; context?: StagingContext; database?: SenderDatabase } = {}): Promise<CheckpointResult> {
  const guard = guarded(options.cipher ?? f.cipher);
  return stageSyncCheckpoint(options.database ?? f.database, guard.cipher, options.context ?? context(), run).then((result) => {
    assert.deepEqual(guard.forbidden, [], "checkpointing only seals");
    return result;
  });
}

function staged(result: CheckpointResult): RunCheckpoint {
  assert.equal(result.outcome, "staged", JSON.stringify(result));
  return (result as { checkpoint: RunCheckpoint }).checkpoint;
}

/** The checkpoint rows of an installation, opened with their own authenticated data and decoded. */
async function checkpoints(database: SenderDatabase, cipher: InstallationCipher, installationId = INSTALLATION) {
  const rows = (await outboxMessages(database)).filter((row) => row.installationId === installationId && row.kind === "sync-checkpoint");
  const result: { sequence: number; bytes: Uint8Array; message: Message }[] = [];
  for (const row of rows) {
    const plain = await cipher.open(KEY_ALIAS, row.sealed, outboxAad(row.installationId, row.sequence));
    assert.equal(plain.status, "ok");
    const bytes = (plain as { value: Uint8Array }).value;
    result.push({ sequence: row.sequence, bytes, message: decodeMessage(bytes) });
  }
  return result;
}

async function contents(database: SenderDatabase) {
  return {
    version: await database.query("PRAGMA user_version"),
    outbox: await database.query("SELECT installation_id, sequence, kind, message_bytes FROM outbox_messages ORDER BY installation_id, sequence"),
    highWater: await database.query("SELECT * FROM outbox_high_water ORDER BY installation_id"),
    checkpoints: await database.query("SELECT * FROM run_checkpoints ORDER BY installation_id, run_sequence"),
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
  readonly seals: { alias: string; plaintext: Uint8Array; aad: Uint8Array }[] = [];
  private readonly inner: InstallationCipher;
  private readonly script: Map<number, { returns?: unknown; throws?: unknown; before?: () => Promise<void> }>;
  constructor(inner: InstallationCipher, script: Record<number, { returns?: unknown; throws?: unknown; before?: () => Promise<void> }> = {}) {
    this.inner = inner;
    this.script = new Map(Object.entries(script).map(([call, entry]) => [Number(call), entry]));
  }
  async seal(alias: string, plaintext: Uint8Array, aad: Uint8Array): Promise<CipherResult<Uint8Array>> {
    this.seals.push({ alias, plaintext: new Uint8Array(plaintext), aad: new Uint8Array(aad) });
    const entry = this.script.get(this.seals.length);
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

const MUTATION = /^\s*(?:WITH\b[\s\S]*?\b)?(INSERT|UPDATE|DELETE|REPLACE)\b/i;

function failing(database: SenderDatabase, rule: { mutation?: number; queries?: boolean; transactionQueries?: boolean; commit?: boolean }): SenderDatabase {
  let mutations = 0;
  const guard = (sql: string, inTransaction: boolean) => {
    if (!MUTATION.test(sql) && (rule.queries || (rule.transactionQueries && inTransaction))) throw new Error(`${SENTINEL} synthetic read failure`);
    if (MUTATION.test(sql) && ++mutations === rule.mutation) throw new Error(`${SENTINEL} synthetic write failure`);
  };
  const wrap = (inner: SenderDatabase, inTransaction: boolean): SenderDatabase => ({
    async execute(sql: string, parameters?: readonly DatabaseValue[]) { guard(sql, inTransaction); return inner.execute(sql, parameters); },
    async query(sql: string, parameters?: readonly DatabaseValue[]) { guard(sql, inTransaction); return inner.query(sql, parameters); },
    transaction<T>(fn: (transaction: SenderDatabase) => T | Promise<T>): Promise<T> {
      return inner.transaction(async (transaction) => {
        const result = await fn(wrap(transaction, true));
        if (rule.commit) throw new Error(`${SENTINEL} synthetic commit failure`);
        return result;
      });
    },
    close: () => inner.close(),
  });
  return wrap(database, false);
}

function beforeTransaction(database: SenderDatabase, hook: () => Promise<void>): SenderDatabase {
  let pending = true;
  return {
    execute: (sql, parameters) => database.execute(sql, parameters),
    query: (sql, parameters) => database.query(sql, parameters),
    async transaction<T>(fn: (transaction: SenderDatabase) => T | Promise<T>): Promise<T> {
      if (pending) { pending = false; await hook(); }
      return database.transaction(fn);
    },
    close: () => database.close(),
  };
}

function untouchable() {
  let calls = 0;
  const refuse = () => { calls++; throw new Error(`${SENTINEL} storage must not be touched`); };
  const database: SenderDatabase = {
    async execute() { refuse(); },
    async query() { return refuse(); },
    async transaction() { return refuse(); },
    async close() { refuse(); },
  };
  return { database, calls: () => calls };
}

// R2 — The checkpoint message

test("a_checkpoint_matches_the_protocol", async (t) => {
  const f = await fixture(t);
  await queue(f.database, INSTALLATION, ["ingest-batch", 300], ["snapshot-completion", 50]);
  const result = staged(await checkpoint(f, summary({ runSequence: 7 })));
  assert.deepEqual(result, { installationId: INSTALLATION, runSequence: 7, sequence: 3 });
  const [row] = await checkpoints(f.database, f.cipher);
  assert.deepEqual(row.bytes, encodeMessage({
    wire_version: 1, kind: "sync-checkpoint", sender_installation: INSTALLATION, sequence: 3, capability_generation: 6,
    run_sequence: 7, trigger: "background", started_at: "2026-10-14T09:00:00.123Z", ended_at: "2026-10-14T09:04:30.007Z",
    elapsed_ms: 269_884, background_scheduling: "available",
    record_types: [
      { record_type: "steps", outcome: "progressed", reason: "time-budget-reached", source_data_staged: true },
      { record_type: "weight", outcome: "no-change", source_data_staged: false },
    ],
    outbox_messages_before_checkpoint: 2, outbox_bytes_before_checkpoint: 350,
  }));
});

test("record_types_are_sorted", async (t) => {
  const f = await fixture(t);
  const recordTypes: RecordTypeOutcome[] = ["weight", "heart-rate", "steps", "active-calories-burned", "sleep-session"].map((recordType) => ({ recordType, outcome: "not-due", sourceDataStaged: false }));
  staged(await checkpoint(f, summary({ recordTypes })));
  const [row] = await checkpoints(f.database, f.cipher);
  assert.deepEqual((row.message.record_types as Message[]).map((entry) => entry.record_type), ["active-calories-burned", "heart-rate", "sleep-session", "steps", "weight"]);
});

test("optional_fields_appear_only_when_supplied", async (t) => {
  const f = await fixture(t);
  const withoutElapsed = { ...summary({ runSequence: 1, recordTypes: [
    { recordType: "steps", outcome: "progressed", sourceDataStaged: true },
    { recordType: "weight", outcome: "interrupted", reason: "read-failed", sourceDataStaged: false },
  ] }) };
  delete (withoutElapsed as { elapsedMs?: number }).elapsedMs;
  staged(await checkpoint(f, withoutElapsed));
  staged(await checkpoint(f, summary({ runSequence: 2, elapsedMs: 0, recordTypes: [{ recordType: "steps", outcome: "interrupted", sourceDataStaged: false }] })));
  const [first, second] = await checkpoints(f.database, f.cipher);
  assert.ok(!("elapsed_ms" in first.message));
  assert.deepEqual(first.message.record_types, [
    { record_type: "steps", outcome: "progressed", source_data_staged: true },
    { record_type: "weight", outcome: "interrupted", reason: "read-failed", source_data_staged: false },
  ]);
  assert.equal(second.message.elapsed_ms, 0);
  assert.deepEqual(second.message.record_types, [{ record_type: "steps", outcome: "interrupted", source_data_staged: false }]);
});

test("every_outcome_and_reason_is_staged", async (t) => {
  const f = await fixture(t);
  const reasons = ["collection-disabled", "projection-refusal", "record-too-large", "permission-missing", "delivery-blocked",
    "storage-pressure", "screen-lock-removed", "changes-token-expired", "time-budget-reached", "read-failed"] as const;
  const runs: RecordTypeOutcome[][] = [
    [
      { recordType: "a", outcome: "not-due", sourceDataStaged: false },
      { recordType: "b", outcome: "no-change", sourceDataStaged: false },
      { recordType: "c", outcome: "completed", sourceDataStaged: true },
      { recordType: "d", outcome: "not-permitted", sourceDataStaged: false },
      { recordType: "e", outcome: "progressed", sourceDataStaged: true },
      { recordType: "f", outcome: "interrupted", sourceDataStaged: true },
    ],
    ...reasons.map((reason, i) => [
      { recordType: "deferred", outcome: "deferred", reason, sourceDataStaged: false },
      { recordType: "blocked", outcome: "blocked", reason: reasons[(i + 1) % reasons.length], sourceDataStaged: false },
      { recordType: "failed", outcome: "failed", reason: reasons[(i + 2) % reasons.length], sourceDataStaged: i % 2 === 0 },
    ] as RecordTypeOutcome[]),
  ];
  for (const [index, recordTypes] of runs.entries()) staged(await checkpoint(f, summary({ runSequence: index + 1, recordTypes })));
  const rows = await checkpoints(f.database, f.cipher);
  assert.equal(rows.length, runs.length);
  for (const [index, row] of rows.entries()) {
    const expected = [...runs[index]].sort((a, b) => a.recordType < b.recordType ? -1 : 1).map((entry) => ({
      record_type: entry.recordType, outcome: entry.outcome, ...(entry.reason ? { reason: entry.reason } : {}), source_data_staged: entry.sourceDataStaged,
    }));
    assert.deepEqual(row.message.record_types, expected);
  }
});

test("an_empty_run_is_checkpointed", async (t) => {
  const f = await fixture(t);
  staged(await checkpoint(f, summary({ recordTypes: [], trigger: "manual", backgroundScheduling: "unregistered" })));
  const [row] = await checkpoints(f.database, f.cipher);
  assert.deepEqual([row.message.record_types, row.message.trigger, row.message.background_scheduling, row.message.outbox_messages_before_checkpoint, row.message.outbox_bytes_before_checkpoint],
    [[], "manual", "unregistered", 0, 0]);
});

test("instants_become_protocol_timestamps", async (t) => {
  const f = await fixture(t);
  const cases: [number, number, string, string][] = [
    [Date.UTC(1969, 11, 31, 23, 59, 59, 999), Date.UTC(1970, 0, 1), "1969-12-31T23:59:59.999Z", "1970-01-01T00:00:00.000Z"],
    [-62_167_219_200_000, 253_402_300_799_999, "0000-01-01T00:00:00.000Z", "9999-12-31T23:59:59.999Z"],
    [ENDED, STARTED, "2026-10-14T09:04:30.007Z", "2026-10-14T09:00:00.123Z"],
  ];
  for (const [index, [startedAtMs, endedAtMs]] of cases.entries()) staged(await checkpoint(f, summary({ runSequence: index + 1, startedAtMs, endedAtMs })));
  const rows = await checkpoints(f.database, f.cipher);
  assert.deepEqual(rows.map((row) => [row.message.started_at, row.message.ended_at]), cases.map(([, , started, ended]) => [started, ended]));
});

// R3 — Counts sampled before insertion

test("counts_cover_queued_rows_of_the_installation", async (t) => {
  const f = await fixture(t);
  await queue(f.database, INSTALLATION, ["ingest-batch", 1000], ["change-completion", 70], ["capability-acknowledgement", 40], ["snapshot-completion", 90]);
  await queue(f.database, OTHER_INSTALLATION, ["ingest-batch", 5000], ["sync-checkpoint", 600]);
  staged(await checkpoint(f, summary()));
  const [row] = await checkpoints(f.database, f.cipher);
  assert.deepEqual([row.message.outbox_messages_before_checkpoint, row.message.outbox_bytes_before_checkpoint], [4, 1200]);
});

test("removed_rows_no_longer_count", async (t) => {
  const f = await fixture(t);
  await queue(f.database, INSTALLATION, ["ingest-batch", 1000], ["ingest-batch", 200], ["change-completion", 70]);
  await f.database.execute("DELETE FROM outbox_messages WHERE installation_id = ? AND sequence IN (1, 3)", [INSTALLATION]);
  const result = staged(await checkpoint(f, summary()));
  assert.equal(result.sequence, 4, "the high-water mark, not the queued rows, sets the sequence");
  const [row] = await checkpoints(f.database, f.cipher);
  assert.deepEqual([row.message.outbox_messages_before_checkpoint, row.message.outbox_bytes_before_checkpoint], [1, 200]);
});

test("the_next_checkpoint_counts_this_one", async (t) => {
  const f = await fixture(t);
  await queue(f.database, INSTALLATION, ["ingest-batch", 500]);
  staged(await checkpoint(f, summary({ runSequence: 1 })));
  staged(await checkpoint(f, summary({ runSequence: 2 })));
  const [first, second] = await checkpoints(f.database, f.cipher);
  assert.deepEqual([first.message.outbox_messages_before_checkpoint, first.message.outbox_bytes_before_checkpoint], [1, 500]);
  assert.deepEqual([second.message.outbox_messages_before_checkpoint, second.message.outbox_bytes_before_checkpoint], [2, 500 + first.bytes.length]);
});

// R4 — Sealed and appended

test("the_checkpoint_is_sealed_and_appended", async (t) => {
  const f = await fixture(t);
  await queue(f.database, INSTALLATION, ["ingest-batch", 10]);
  const spy = new SpyCipher(f.cipher);
  const result = staged(await checkpoint(f, summary(), { cipher: spy }));
  assert.equal(spy.seals.length, 1);
  assert.equal(spy.seals[0].alias, KEY_ALIAS);
  assert.deepEqual(spy.seals[0].aad, outboxAad(INSTALLATION, result.sequence));
  const row = (await outboxMessages(f.database)).at(-1)!;
  assert.deepEqual([row.sequence, row.kind, row.messageBytes], [result.sequence, "sync-checkpoint", spy.seals[0].plaintext.length]);
  assert.deepEqual((await checkpoints(f.database, f.cipher))[0].bytes, spy.seals[0].plaintext);
  assert.equal(await lastOutboxSequence(f.database, INSTALLATION), result.sequence);
});

test("staging_only_seals", async (t) => {
  const f = await fixture(t);
  const guard = guarded(f.cipher);
  const results = [
    await stageSyncCheckpoint(f.database, guard.cipher, context({ keyAlias: "synthetic-missing-key" }), summary()),
    await stageSyncCheckpoint(failing(f.database, { commit: true }), guard.cipher, context(), summary()),
    await stageSyncCheckpoint(f.database, guard.cipher, context(), summary()),
    await stageSyncCheckpoint(f.database, guard.cipher, context(), summary()),
  ];
  assert.deepEqual(results.map((result) => result.outcome), ["cipher-failed", "storage-failed", "staged", "refused"]);
  assert.deepEqual(guard.forbidden, []);
});

// R5 — Exactly once per run

test("a_run_is_checkpointed_once", async (t) => {
  const f = await fixture(t);
  const first = staged(await checkpoint(f, summary({ runSequence: 3 })));
  const before = await contents(f.database);
  const spy = new SpyCipher(f.cipher);
  assert.deepEqual(await checkpoint(f, summary({ runSequence: 3, trigger: "manual" }), { cipher: spy }), { outcome: "refused", failure: "checkpoint-already-staged" });
  assert.equal(spy.seals.length, 0);
  assert.deepEqual(await contents(f.database), before);
  assert.deepEqual(await runCheckpoint(f.database, INSTALLATION, 3), first);
  assert.equal(await runCheckpoint(f.database, INSTALLATION, 4), null);
  await f.database.execute("DELETE FROM outbox_messages WHERE installation_id = ? AND sequence = ?", [INSTALLATION, first.sequence]);
  assert.deepEqual(await checkpoint(f, summary({ runSequence: 3 })), { outcome: "refused", failure: "checkpoint-already-staged" });
  // Another installation's run with the same number is independent.
  const other = staged(await checkpoint(f, summary({ runSequence: 3 }), { context: context({ installationId: OTHER_INSTALLATION }) }));
  assert.deepEqual(other, { installationId: OTHER_INSTALLATION, runSequence: 3, sequence: 1 });
});

test("the_checkpoint_record_survives_reopening", async (t) => {
  const f = await fixture(t);
  const first = staged(await checkpoint(f, summary({ runSequence: 9 })));
  const reopened = await f.reopen();
  assert.deepEqual(await migrate(reopened, MIGRATIONS), { status: "ready", version: 4 });
  assert.deepEqual(await runCheckpoint(reopened, INSTALLATION, 9), first);
  assert.deepEqual(await checkpoint({ database: reopened, cipher: f.cipher }, summary({ runSequence: 9 })), { outcome: "refused", failure: "checkpoint-already-staged" });
});

// R6 — Recheck inside the commit

test("interleaved_staging_conflicts_without_writing", async (t) => {
  const f = await fixture(t);
  const conflict = { outcome: "conflict", failure: "staging-conflict" };
  let afterCompetitor: Awaited<ReturnType<typeof contents>> | undefined;
  const appending = new SpyCipher(f.cipher, { 1: { before: async () => {
    await queue(f.database, INSTALLATION, ["ingest-batch", 25]);
    afterCompetitor = await contents(f.database);
  } } });
  assert.deepEqual(await checkpoint(f, summary(), { cipher: appending }), conflict);
  assert.deepEqual(await contents(f.database), afterCompetitor, "only the competitor's change remains");
  const racing = new SpyCipher(f.cipher, { 1: { before: async () => {
    staged(await checkpoint(f, summary()));
    afterCompetitor = await contents(f.database);
  } } });
  assert.deepEqual(await checkpoint(f, summary(), { cipher: racing }), conflict);
  assert.deepEqual(await contents(f.database), afterCompetitor, "only the competing checkpoint remains");
  assert.equal((await checkpoints(f.database, f.cipher)).length, 1);
});

test("the_recheck_runs_inside_the_commit_transaction", async (t) => {
  const conflict = { outcome: "conflict", failure: "staging-conflict" };
  for (const [label, change] of [
    ["a row is removed", (database: SenderDatabase) => database.execute("DELETE FROM outbox_messages WHERE installation_id = ? AND sequence = 1", [INSTALLATION])],
    ["a row's size changes", (database: SenderDatabase) => database.execute("UPDATE outbox_messages SET message_bytes = 999 WHERE installation_id = ? AND sequence = 2", [INSTALLATION])],
    ["a row is queued", (database: SenderDatabase) => queue(database, INSTALLATION, ["ingest-batch", 1])],
    ["the run is checkpointed", (database: SenderDatabase) => database.execute("INSERT INTO run_checkpoints (installation_id, run_sequence, sequence) VALUES (?, 1, 99)", [INSTALLATION])],
  ] as const) {
    const f = await fixture(t);
    await queue(f.database, INSTALLATION, ["ingest-batch", 100], ["ingest-batch", 200]);
    let afterChange: Awaited<ReturnType<typeof contents>> | undefined;
    const database = beforeTransaction(f.database, async () => { await change(f.database); afterChange = await contents(f.database); });
    assert.deepEqual(await checkpoint(f, summary(), { database }), conflict, label);
    assert.deepEqual(await contents(f.database), afterChange, label);
    assert.equal((await checkpoints(f.database, f.cipher)).length, 0, label);
  }
});

// R7 — Nothing staged on failure

test("cipher_failures_write_nothing", async (t) => {
  const f = await fixture(t);
  await queue(f.database, INSTALLATION, ["ingest-batch", 10]);
  const before = await contents(f.database);
  const failed = (failure: string) => ({ outcome: "cipher-failed", failure });
  assert.deepEqual(await checkpoint(f, summary(), { context: context({ keyAlias: "synthetic-missing-key" }) }), failed("installation-key-missing"));
  const invalidated = new NodeInstallationCipher();
  await invalidated.create(KEY_ALIAS);
  invalidated.invalidate(KEY_ALIAS);
  assert.deepEqual(await checkpoint(f, summary(), { cipher: invalidated }), failed("installation-key-unusable"));
  const unavailable = new NodeInstallationCipher();
  await unavailable.create(KEY_ALIAS);
  unavailable.setUnavailable(true);
  assert.deepEqual(await checkpoint(f, summary(), { cipher: unavailable }), failed("keystore-unavailable"));
  for (const entry of [
    { throws: new Error(`${SENTINEL} bridge`) }, { throws: undefined }, { returns: { status: "ok", value: new Uint8Array() } },
    { returns: { status: "error", reason: "unsupported-format" } }, { returns: { status: "error", reason: "constructor" } }, { returns: null },
  ]) {
    assert.deepEqual(await checkpoint(f, summary(), { cipher: new SpyCipher(f.cipher, { 1: entry }) }), failed("unexpected-cipher-failure"), JSON.stringify(entry));
  }
  assert.deepEqual(await contents(f.database), before);
});

test("storage_errors_roll_back_everything", async (t) => {
  const f = await fixture(t);
  await queue(f.database, INSTALLATION, ["ingest-batch", 10]);
  const before = await contents(f.database);
  let points = 0;
  for (let mutation = 1; ; mutation++) {
    const result = await checkpoint(f, summary(), { database: failing(f.database, { mutation }) });
    if (result.outcome === "staged") break;
    assert.deepEqual(result, { outcome: "storage-failed", failure: "storage-failed" });
    assert.deepEqual(await contents(f.database), before, `failing mutation ${mutation}`);
    points++;
    assert.ok(mutation < 20, "the checkpoint never committed");
  }
  assert.ok(points >= 3, "the Outbox row, the high-water mark, and the checkpoint record each take a statement");
  const settled = await contents(f.database);
  const spy = new SpyCipher(f.cipher);
  assert.deepEqual(await checkpoint(f, summary({ runSequence: 2 }), { database: failing(f.database, { queries: true }), cipher: spy }), { outcome: "storage-failed", failure: "storage-failed" });
  assert.equal(spy.seals.length, 0);
  for (const rule of [{ transactionQueries: true }, { commit: true }]) {
    assert.deepEqual(await checkpoint(f, summary({ runSequence: 2 }), { database: failing(f.database, rule) }), { outcome: "storage-failed", failure: "storage-failed" }, JSON.stringify(rule));
    assert.deepEqual(await contents(f.database), settled, JSON.stringify(rule));
  }
});

// R8 — The Sender schema migration

const CHECKPOINT_DDL = `CREATE TABLE run_checkpoints (
  installation_id TEXT NOT NULL, run_sequence INTEGER NOT NULL CHECK (run_sequence > 0),
  sequence INTEGER NOT NULL CHECK (sequence > 0),
  PRIMARY KEY (installation_id, run_sequence), UNIQUE (installation_id, sequence)) STRICT`;
const compact = (sql: string) => sql.replace(/\s+/g, "");

test("migration_creates_version_four", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const database = await openNodeDatabase(join(directory, "synthetic.sqlite"));
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.equal(checkpointMigration.version, 4);
  assert.deepEqual(await migrate(database, MIGRATIONS.slice(0, 3)), { status: "ready", version: 3 });
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 4 });
  const [table] = await database.query("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'run_checkpoints'");
  assert.equal(compact(table.sql as string), compact(CHECKPOINT_DDL));
  const created = await database.query("SELECT type, sql FROM sqlite_master WHERE tbl_name = 'run_checkpoints'");
  assert.ok(created.every((row) => row.type === "table" || row.sql === null), "only automatic indexes");
  const insert = (installationId: string, runSequence: number, sequence: number) =>
    database.execute("INSERT INTO run_checkpoints (installation_id, run_sequence, sequence) VALUES (?, ?, ?)", [installationId, runSequence, sequence]);
  await insert(INSTALLATION, 1, 1);
  await insert(OTHER_INSTALLATION, 1, 1);
  await assert.rejects(insert(INSTALLATION, 1, 2), "one checkpoint per run");
  await assert.rejects(insert(INSTALLATION, 2, 1), "one checkpoint per sequence");
  await assert.rejects(insert(INSTALLATION, 0, 3));
  await assert.rejects(insert(INSTALLATION, 3, 0));
});

test("migration_is_idempotent", async (t) => {
  const f = await fixture(t);
  staged(await checkpoint(f, summary()));
  const before = await contents(f.database);
  const schema = await f.database.query("SELECT type, name, sql FROM sqlite_master ORDER BY name");
  assert.deepEqual(await migrate(f.database, MIGRATIONS), { status: "ready", version: 4 });
  assert.deepEqual(await contents(f.database), before);
  assert.deepEqual(await f.database.query("SELECT type, name, sql FROM sqlite_master ORDER BY name"), schema);
});

// R9 — Caller errors, health-data-free outcomes, order of checks, and module boundary

test("malformed_summaries_are_caller_errors", async (t) => {
  const f = await fixture(t);
  const probe = untouchable();
  const spy = new SpyCipher(f.cipher);
  const entry = (overrides: Partial<RecordTypeOutcome> & Record<string, unknown>) => ({ recordType: "steps", outcome: "no-change", sourceDataStaged: false, ...overrides }) as RecordTypeOutcome;
  const bad: [unknown, unknown][] = [
    [context({ installationId: "not-a-uuid" }), summary()],
    [context({ keyAlias: "" }), summary()],
    [context({ generation: { generation: 0, maxRecordCount: 1, maxMessageBytes: 4096, projectionSchemas: {} } }), summary()],
    [null, summary()],
    [context(), summary({ runSequence: 0 })],
    [context(), summary({ runSequence: 1.5 })],
    [context(), summary({ trigger: "scheduled" as RunSummary["trigger"] })],
    [context(), summary({ backgroundScheduling: "denied" as RunSummary["backgroundScheduling"] })],
    [context(), summary({ startedAtMs: 1.5 })],
    [context(), summary({ startedAtMs: -62_167_219_200_001 })],
    [context(), summary({ endedAtMs: 253_402_300_800_000 })],
    [context(), summary({ elapsedMs: -1 })],
    [context(), summary({ elapsedMs: 2.5 })],
    [context(), summary({ runSequence: Number.NaN })],
    [context(), summary({ runSequence: Number.MAX_SAFE_INTEGER + 1 })],
    [context(), summary({ startedAtMs: Number.POSITIVE_INFINITY })],
    [context(), summary({ endedAtMs: Number.NaN })],
    [context(), summary({ elapsedMs: Number.POSITIVE_INFINITY })],
    [context(), summary({ elapsedMs: Number.MAX_SAFE_INTEGER + 1 })],
    [context(), summary({ recordTypes: [entry({ recordType: "Steps" })] })],
    [context(), summary({ recordTypes: [entry({}), entry({})] })],
    [context(), summary({ recordTypes: [entry({ outcome: "skipped" as RecordTypeOutcome["outcome"] })] })],
    [context(), summary({ recordTypes: [entry({ outcome: "deferred" })] })],
    [context(), summary({ recordTypes: [entry({ outcome: "blocked" })] })],
    [context(), summary({ recordTypes: [entry({ outcome: "failed" })] })],
    [context(), summary({ recordTypes: [entry({ outcome: "completed", reason: "read-failed" })] })],
    [context(), summary({ recordTypes: [entry({ outcome: "not-permitted", reason: "permission-missing" })] })],
    [context(), summary({ recordTypes: [entry({ outcome: "failed", reason: `${SENTINEL} free text` as RecordTypeOutcome["reason"] })] })],
    [context(), summary({ recordTypes: [entry({ sourceDataStaged: "yes" as unknown as boolean })] })],
    [context(), summary({ recordTypes: null as unknown as RecordTypeOutcome[] })],
    [context(), null],
  ];
  for (const [ctx, run] of bad) {
    await assert.rejects(stageSyncCheckpoint(probe.database, spy, ctx as StagingContext, run as RunSummary), TypeError, JSON.stringify([ctx, run]));
  }
  assert.equal(probe.calls(), 0);
  assert.equal(spy.seals.length, 0);
});

test("non_staged_results_carry_only_named_failures", async (t) => {
  const f = await fixture(t);
  staged(await checkpoint(f, summary({ runSequence: 1 })));
  const results = [
    await checkpoint(f, summary({ runSequence: 1 })),
    await checkpoint(f, summary({ runSequence: 2 }), { cipher: new SpyCipher(f.cipher, { 1: { throws: new Error(`${SENTINEL} bridge failure`) } }) }),
    await checkpoint(f, summary({ runSequence: 2 }), { database: failing(f.database, { mutation: 1 }) }),
    await checkpoint(f, summary({ runSequence: 2 }), { database: failing(f.database, { queries: true }) }),
  ];
  for (const result of results) {
    assert.notEqual(result.outcome, "staged");
    assert.deepEqual(Object.keys(result).sort(), ["failure", "outcome"]);
    assert.ok(!JSON.stringify(result).includes(SENTINEL), JSON.stringify(result));
  }
});

test("every_named_failure_is_reachable_and_in_the_contract", async (t) => {
  const f = await fixture(t);
  const classes: Record<string, string> = { "refused": "refusal", "conflict": "conflict", "cipher-failed": "cipher-failure", "storage-failed": "storage-failure" };
  const observed = new Set<string>();
  const record = (result: CheckpointResult) => {
    assert.notEqual(result.outcome, "staged", JSON.stringify(result));
    observed.add(`${classes[result.outcome]}:${(result as { failure: string }).failure}`);
  };
  record(await checkpoint(f, summary(), { context: context({ keyAlias: "synthetic-missing-key" }) }));
  const invalidated = new NodeInstallationCipher();
  await invalidated.create(KEY_ALIAS);
  invalidated.invalidate(KEY_ALIAS);
  record(await checkpoint(f, summary(), { cipher: invalidated }));
  record(await checkpoint(f, summary(), { cipher: new SpyCipher(f.cipher, { 1: { returns: { status: "error", reason: "keystore-unavailable" } } }) }));
  record(await checkpoint(f, summary(), { cipher: new SpyCipher(f.cipher, { 1: { throws: new Error("bridge") } }) }));
  record(await checkpoint(f, summary(), { database: failing(f.database, { commit: true }) }));
  record(await checkpoint(f, summary(), { cipher: new SpyCipher(f.cipher, { 1: { before: async () => { await queue(f.database, INSTALLATION, ["ingest-batch", 3]); } } }) }));
  staged(await checkpoint(f, summary()));
  record(await checkpoint(f, summary()));
  const named = new Set((CONTRACT.named_failures as { name: string; outcome: string }[]).map((failure) => `${failure.outcome}:${failure.name}`));
  assert.deepEqual([...observed].sort(), [...named].sort());
});

test("checks_follow_the_documented_order", async (t) => {
  const f = await fixture(t);
  // 1. Caller errors before any storage use.
  await assert.rejects(stageSyncCheckpoint(untouchable().database, f.cipher, context(), summary({ runSequence: 0 })), TypeError);
  // 2. The refusal before sealing, even with a missing key.
  staged(await checkpoint(f, summary()));
  const spy = new SpyCipher(f.cipher);
  assert.deepEqual(await checkpoint(f, summary(), { cipher: spy, context: context({ keyAlias: "synthetic-missing-key" }) }), { outcome: "refused", failure: "checkpoint-already-staged" });
  assert.equal(spy.seals.length, 0);
  // 3. Sealing before the commit, and 4. the commit last.
  assert.deepEqual(await checkpoint(f, summary({ runSequence: 2 }), { database: failing(f.database, { mutation: 1 }), context: context({ keyAlias: "synthetic-missing-key" }) }),
    { outcome: "cipher-failed", failure: "installation-key-missing" });
  assert.deepEqual(await checkpoint(f, summary({ runSequence: 2 }), { database: failing(f.database, { mutation: 1 }) }), { outcome: "storage-failed", failure: "storage-failed" });
});

test("the_module_uses_other_units_tables_only_through_their_functions", () => {
  const source = readFileSync(MODULE, "utf8");
  for (const table of ["outbox_messages", "outbox_high_water", "cycle_progress", "cycle_completions", "snapshot_attempts", "current_snapshot_attempts"]) {
    assert.doesNotMatch(source, new RegExp(`\\b${table}\\b`), table);
  }
  assert.match(source, /\bappendOutboxMessages\s*\(/);
  const specifiers = [...source.matchAll(/\b(?:from|import)\s*\(?\s*["']([^"']+)["']/g)].map((match) => match[1]);
  assert.deepEqual(specifiers.filter((specifier) => !/^(?:\.{1,2}\/|@activity-tracker-portal\/ingest-protocol$)/.test(specifier)), []);
  assert.doesNotMatch(source, /\b(?:Buffer|require|process)\b/);
});
