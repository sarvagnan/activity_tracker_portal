// Acceptance tests for stage-sync-checkpoint@2: pre-checkpoint counts without loading sealed messages.
// Requirement numbers refer to packs/stage-sync-checkpoint-2.spec.md. stage-sync-checkpoint@1's suite runs
// unchanged beside this one and covers R1-R9. Storage is the 3ug node:sqlite SenderDatabase substitute and the
// cipher is the 3ug NodeInstallationCipher; the Outbox is filled through the published stage-outbox-batch
// primitives, and @1's outboxMessages is the oracle for the counts.
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { TestContext } from "node:test";
import { decodeMessage, encodeMessage } from "@activity-tracker-portal/ingest-protocol";
import type { CipherResult, InstallationCipher, InstallationKeyStatus } from "../src/installation-cipher.ts";
import { appendOutboxMessages, lastOutboxSequence, outboxAad, outboxMessages, outboxMigration } from "../src/outbox/stage-outbox-batch.ts";
import type { OutboxKind, StagingContext } from "../src/outbox/stage-outbox-batch.ts";
import { checkpointMigration, stageSyncCheckpoint } from "../src/outbox/stage-sync-checkpoint.ts";
import type { CheckpointResult, RunSummary } from "../src/outbox/stage-sync-checkpoint.ts";
import { completionMigration } from "../src/outbox/stage-sync-completion.ts";
import { snapshotAttemptsMigration } from "../src/snapshot/begin-initial-snapshot.ts";
import type { DatabaseRow, DatabaseValue, SenderDatabase } from "../src/storage/database.ts";
import { migrate } from "../src/storage/migrations.ts";
import { openNodeDatabase } from "./support/node-database.ts";
import { NodeInstallationCipher } from "./support/node-installation-cipher.ts";

const INSTALLATION = "00000000-0000-4000-8000-000000000001";
const OTHER_INSTALLATION = "00000000-0000-4000-8000-000000000002";
const KEY_ALIAS = "synthetic-installation-key";
const MIGRATIONS = [snapshotAttemptsMigration, outboxMigration, completionMigration, checkpointMigration];
const STARTED = Date.UTC(2026, 9, 30, 9, 0, 0, 123);
const ENDED = Date.UTC(2026, 9, 30, 9, 4, 30, 7);
const KINDS: readonly OutboxKind[] = ["capability-acknowledgement", "ingest-batch", "snapshot-completion", "change-completion", "sync-checkpoint"];
const utf8 = (text: string) => new TextEncoder().encode(text);

type Fixture = Awaited<ReturnType<typeof fixture>>;

async function fixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const database = await openNodeDatabase(join(directory, "synthetic.sqlite"));
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 4 });
  const cipher = new NodeInstallationCipher();
  assert.equal((await cipher.create(KEY_ALIAS)).status, "ok");
  return { cipher, database };
}

function context(installationId = INSTALLATION): StagingContext {
  return {
    installationId, keyAlias: KEY_ALIAS,
    generation: { generation: 6, maxRecordCount: 100, maxMessageBytes: 65_536, projectionSchemas: { steps: [1] } },
  };
}

function summary(runSequence = 1): RunSummary {
  return {
    runSequence, trigger: "background", startedAtMs: STARTED, endedAtMs: ENDED, elapsedMs: 269_884, backgroundScheduling: "available",
    recordTypes: [
      { recordType: "weight", outcome: "no-change", sourceDataStaged: false },
      { recordType: "steps", outcome: "progressed", reason: "time-budget-reached", sourceDataStaged: true },
    ],
  };
}

/** Queue synthetic Outbox rows of known plaintext sizes through the published primitive; their bytes are never opened. */
async function queue(database: SenderDatabase, installationId: string, ...messages: [OutboxKind, number][]) {
  const last = await lastOutboxSequence(database, installationId);
  assert.equal(await database.transaction((tx) => appendOutboxMessages(tx, installationId, last,
    messages.map(([kind, messageBytes], i) => ({ kind, sealed: utf8(`synthetic-sealed-${installationId.slice(-2)}-${last + i + 1}`), messageBytes })))), true);
}

async function remove(database: SenderDatabase, installationId: string, sequence: number) {
  await database.execute("DELETE FROM outbox_messages WHERE installation_id = ? AND sequence = ?", [installationId, sequence]);
}

/** @1's outboxMessages for one installation: the oracle for the pre-checkpoint counts. */
async function oracle(database: SenderDatabase, installationId: string) {
  const rows = (await outboxMessages(database)).filter((row) => row.installationId === installationId);
  return { messages: rows.length, bytes: rows.reduce((sum, row) => sum + row.messageBytes, 0) };
}

/** The plaintext bytes of the checkpoint the installation staged at `sequence`. */
async function checkpointBytes(f: Fixture, sequence: number, installationId = INSTALLATION): Promise<Uint8Array> {
  const [row] = (await outboxMessages(f.database)).filter((entry) => entry.installationId === installationId && entry.sequence === sequence);
  assert.ok(row, `checkpoint at ${sequence}`);
  assert.equal(row.kind, "sync-checkpoint");
  const plain = await f.cipher.open(KEY_ALIAS, row.sealed, outboxAad(installationId, sequence));
  assert.equal(plain.status, "ok");
  return (plain as { value: Uint8Array }).value;
}

/** The exact checkpoint @1 stages for `summary(runSequence)` with the given pre-checkpoint counts (@1's R2). */
function expectedCheckpoint(installationId: string, sequence: number, runSequence: number, counts: { messages: number; bytes: number }): Uint8Array {
  return encodeMessage({
    wire_version: 1, kind: "sync-checkpoint", sender_installation: installationId, sequence, capability_generation: 6,
    run_sequence: runSequence, trigger: "background", started_at: new Date(STARTED).toISOString(), ended_at: new Date(ENDED).toISOString(),
    elapsed_ms: 269_884, background_scheduling: "available",
    record_types: [
      { record_type: "steps", outcome: "progressed", reason: "time-budget-reached", source_data_staged: true },
      { record_type: "weight", outcome: "no-change", source_data_staged: false },
    ],
    outbox_messages_before_checkpoint: counts.messages, outbox_bytes_before_checkpoint: counts.bytes,
  });
}

/** The whole database: the schema version, every schema object, and every row of every table, sealed bytes included. */
async function contents(database: SenderDatabase) {
  const schema = await database.query("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name");
  const tables: Record<string, unknown> = {};
  for (const { name } of schema.filter((row) => row.type === "table")) {
    tables[name as string] = await database.query(`SELECT * FROM "${name as string}" ORDER BY rowid`);
  }
  return { version: await database.query("PRAGMA user_version"), schema, tables };
}

/** Records every row any query returns, inside or outside a transaction. */
function observing(inner: SenderDatabase): { database: SenderDatabase; rows: DatabaseRow[] } {
  const rows: DatabaseRow[] = [];
  const wrap = (database: SenderDatabase): SenderDatabase => ({
    execute: (sql: string, parameters?: readonly DatabaseValue[]) => database.execute(sql, parameters),
    async query(sql: string, parameters?: readonly DatabaseValue[]) {
      const result = await database.query(sql, parameters);
      rows.push(...result);
      return result;
    },
    transaction: <T>(fn: (transaction: SenderDatabase) => T | Promise<T>) => database.transaction((transaction) => fn(wrap(transaction))),
    close: () => database.close(),
  });
  return { database: wrap(inner), rows };
}

/** Rows that carry a `sealed` value, and every byte-array value any row carries. */
function ciphertext(rows: readonly DatabaseRow[]) {
  return {
    sealedRows: rows.filter((row) => "sealed" in row).length,
    byteArrays: rows.flatMap((row) => Object.values(row)).filter((value) => value instanceof Uint8Array).length,
  };
}

/** A database whose numbered mutating statement, or every read, fails. */
function failing(database: SenderDatabase, rule: { queries?: boolean; mutation?: number }): SenderDatabase {
  let mutations = 0;
  const mutation = /^\s*(?:WITH\b[\s\S]*?\b)?(INSERT|UPDATE|DELETE|REPLACE)\b/i;
  const guard = (sql: string) => {
    if (!mutation.test(sql) && rule.queries) throw new Error("synthetic read failure");
    if (mutation.test(sql) && ++mutations === rule.mutation) throw new Error("synthetic write failure");
  };
  const wrap = (inner: SenderDatabase): SenderDatabase => ({
    async execute(sql: string, parameters?: readonly DatabaseValue[]) { guard(sql); return inner.execute(sql, parameters); },
    async query(sql: string, parameters?: readonly DatabaseValue[]) { guard(sql); return inner.query(sql, parameters); },
    transaction: <T>(fn: (transaction: SenderDatabase) => T | Promise<T>) => inner.transaction((transaction) => fn(wrap(transaction))),
    close: () => inner.close(),
  });
  return wrap(database);
}

/** Delegates to the real cipher; a hook runs before the first seal, or the seal returns a scripted failure. */
class HookCipher implements InstallationCipher {
  private readonly inner: InstallationCipher;
  private readonly before?: () => Promise<void>;
  private readonly failure?: CipherResult<Uint8Array>;
  constructor(inner: InstallationCipher, options: { before?: () => Promise<void>; failure?: CipherResult<Uint8Array> }) {
    this.inner = inner; this.before = options.before; this.failure = options.failure;
  }
  async seal(alias: string, plaintext: Uint8Array, aad: Uint8Array): Promise<CipherResult<Uint8Array>> {
    await this.before?.();
    return this.failure ?? this.inner.seal(alias, plaintext, aad);
  }
  create(alias: string) { return this.inner.create(alias); }
  keyStatus(alias: string): Promise<InstallationKeyStatus> { return this.inner.keyStatus(alias); }
  open(alias: string, sealed: Uint8Array, aad: Uint8Array) { return this.inner.open(alias, sealed, aad); }
  delete(alias: string) { return this.inner.delete(alias); }
  isDeviceSecure() { return this.inner.isDeviceSecure(); }
}

function stage(f: Fixture, run: RunSummary, options: { database?: SenderDatabase; cipher?: InstallationCipher; installationId?: string } = {}): Promise<CheckpointResult> {
  return stageSyncCheckpoint(options.database ?? f.database, options.cipher ?? f.cipher, context(options.installationId), run);
}

async function fill(database: SenderDatabase, count: number) {
  for (let start = 0; start < count; start += 100) {
    await queue(database, INSTALLATION, ...Array.from({ length: Math.min(100, count - start) }, (_, i) => [KINDS[(start + i) % KINDS.length], 64 + ((start + i) % 50)] as [OutboxKind, number]));
  }
}

// R11 — No sealed message is loaded

test("staging_loads_no_sealed_message", async (t) => {
  const f = await fixture(t);
  await queue(f.database, INSTALLATION, ...KINDS.map((kind, i) => [kind, 100 + i] as [OutboxKind, number]));
  await queue(f.database, OTHER_INSTALLATION, ["ingest-batch", 900]);
  const watched = observing(f.database);
  const result = await stage(f, summary(), { database: watched.database });
  assert.equal(result.outcome, "staged", JSON.stringify(result));
  assert.ok(watched.rows.length > 0, "the counts are read through the database");
  assert.deepEqual(ciphertext(watched.rows), { sealedRows: 0, byteArrays: 0 });
});

test("failed_stagings_load_no_sealed_message", async (t) => {
  const f = await fixture(t);
  await queue(f.database, INSTALLATION, ["ingest-batch", 300], ["change-completion", 120]);
  await queue(f.database, OTHER_INSTALLATION, ["ingest-batch", 900]);
  assert.equal((await stage(f, summary(1))).outcome, "staged");
  const outcomes: [string, () => Promise<CheckpointResult>, { database: SenderDatabase; rows: DatabaseRow[] }][] = [];
  const already = observing(f.database);
  outcomes.push(["checkpoint-already-staged", () => stage(f, summary(1), { database: already.database }), already]);
  const conflict = observing(f.database);
  outcomes.push(["staging-conflict", () => stage(f, summary(2), {
    database: conflict.database,
    cipher: new HookCipher(f.cipher, { before: () => queue(f.database, INSTALLATION, ["ingest-batch", 50]) }),
  }), conflict]);
  const cipherFailure = observing(f.database);
  outcomes.push(["keystore-unavailable", () => stage(f, summary(3), {
    database: cipherFailure.database,
    cipher: new HookCipher(f.cipher, { failure: { status: "error", reason: "keystore-unavailable" } as CipherResult<Uint8Array> }),
  }), cipherFailure]);
  const storage = observing(f.database);
  outcomes.push(["storage-failed", () => stage(f, summary(4), { database: failing(storage.database, { mutation: 1 }) }), storage]);
  for (const [failure, run, watched] of outcomes) {
    const result = await run();
    assert.equal((result as { failure?: string }).failure, failure, JSON.stringify(result));
    assert.deepEqual(ciphertext(watched.rows), { sealedRows: 0, byteArrays: 0 }, failure);
  }
});

test("reads_do_not_grow_with_the_outbox", async (t) => {
  const received: { sealedRows: number; byteArrays: number }[] = [];
  for (const size of [0, 500]) {
    const f = await fixture(t);
    await fill(f.database, size);
    const watched = observing(f.database);
    assert.equal((await stage(f, summary(), { database: watched.database })).outcome, "staged", `${size} queued`);
    received.push(ciphertext(watched.rows));
  }
  assert.deepEqual(received[1], received[0]);
  assert.deepEqual(received[0], { sealedRows: 0, byteArrays: 0 });
});

// R12 — The same checkpoint

test("checkpoint_counts_match_the_outbox_oracle", async (t) => {
  const f = await fixture(t);
  const steps: (() => Promise<void>)[] = [
    async () => undefined,
    () => queue(f.database, INSTALLATION, ...KINDS.map((kind, i) => [kind, 200 + i * 11] as [OutboxKind, number])),
    () => queue(f.database, OTHER_INSTALLATION, ["ingest-batch", 4096], ["sync-checkpoint", 333]),
    () => remove(f.database, INSTALLATION, 1),
    () => remove(f.database, INSTALLATION, 3),
    () => queue(f.database, INSTALLATION, ["ingest-batch", 777]),
  ];
  let run = 1;
  for (const step of steps) {
    await step();
    const expected = await oracle(f.database, INSTALLATION);
    const result = await stage(f, summary(run));
    assert.equal(result.outcome, "staged", JSON.stringify(result));
    const sequence = await lastOutboxSequence(f.database, INSTALLATION);
    const bytes = await checkpointBytes(f, sequence);
    assert.deepEqual(bytes, expectedCheckpoint(INSTALLATION, sequence, run, expected), `run ${run}: the exact checkpoint bytes`);
    const message = decodeMessage(bytes) as unknown as Record<string, unknown>;
    assert.deepEqual([message.outbox_messages_before_checkpoint, message.outbox_bytes_before_checkpoint], [expected.messages, expected.bytes], `run ${run}`);
    run++;
  }
  // The other installation's checkpoint counts only its own rows, including none of this one's.
  const expected = await oracle(f.database, OTHER_INSTALLATION);
  assert.equal((await stage(f, summary(1), { installationId: OTHER_INSTALLATION })).outcome, "staged");
  const otherSequence = await lastOutboxSequence(f.database, OTHER_INSTALLATION);
  assert.deepEqual(await checkpointBytes(f, otherSequence, OTHER_INSTALLATION), expectedCheckpoint(OTHER_INSTALLATION, otherSequence, 1, expected));
});

test("the_recheck_still_detects_every_change", async (t) => {
  const changes: [string, (database: SenderDatabase) => Promise<void>][] = [
    ["a new message raises the count and the high-water mark", (database) => queue(database, INSTALLATION, ["ingest-batch", 50])],
    ["a message appended and removed moves only the high-water mark", async (database) => {
      await queue(database, INSTALLATION, ["ingest-batch", 50]);
      await remove(database, INSTALLATION, await lastOutboxSequence(database, INSTALLATION));
    }],
    ["a removed message lowers the count only", (database) => remove(database, INSTALLATION, 2)],
    ["a changed byte length moves only the byte total", (database) => database.execute(
      "UPDATE outbox_messages SET message_bytes = message_bytes + 1 WHERE installation_id = ? AND sequence = 1", [INSTALLATION])],
    ["the run's checkpoint record appears", (database) => database.execute(
      "INSERT INTO run_checkpoints (installation_id, run_sequence, sequence) VALUES (?, 1, 3)", [INSTALLATION])],
  ];
  for (const [label, change] of changes) {
    const f = await fixture(t);
    await queue(f.database, INSTALLATION, ["ingest-batch", 300], ["change-completion", 120]);
    let settled: Awaited<ReturnType<typeof contents>> | undefined;
    const cipher = new HookCipher(f.cipher, { before: async () => { await change(f.database); settled = await contents(f.database); } });
    assert.deepEqual(await stage(f, summary(1), { cipher }), { outcome: "conflict", failure: "staging-conflict" }, label);
    assert.ok(settled);
    assert.deepEqual(await contents(f.database), settled, `${label}: nothing written after the competing change`);
  }
});
