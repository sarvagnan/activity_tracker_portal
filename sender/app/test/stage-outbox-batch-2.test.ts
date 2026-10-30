// Acceptance tests for stage-outbox-batch@2: backlog reads without ciphertext and quiet change-origin advances.
// Requirement numbers refer to packs/stage-outbox-batch-2.spec.md. stage-outbox-batch@1's suite runs unchanged
// beside this one and covers R1-R12. Storage is the 3ug node:sqlite SenderDatabase substitute, the cipher is the
// 3ug NodeInstallationCipher, and snapshot attempts come from the published begin-initial-snapshot with a scripted
// issuer. Fixtures migrate with an explicit list of every migration up to this unit's version 10, never the
// production senderMigrations.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import type { TestContext } from "node:test";
import { deliveryMigration } from "../src/delivery/send-outbox-message.ts";
import { enrollmentMigration } from "../src/enrollment/manage-sender-enrollment.ts";
import type { CipherResult, InstallationCipher, InstallationKeyStatus } from "../src/installation-cipher.ts";
import { operatorConditionsMigration } from "../src/notifications/notify-operator-conditions.ts";
import {
  advanceChangeOrigin,
  appendOutboxMessages,
  changeOrigin,
  changeOriginMigration,
  cycleProgress,
  outboxBacklog,
  outboxHolds,
  outboxMessages,
  outboxMigration,
  stageOutboxBatch,
} from "../src/outbox/stage-outbox-batch.ts";
import type {
  AdvanceResult,
  ChangeOrigin,
  ChangePage,
  ObservedPage,
  OutboxKind,
  SnapshotPage,
  StageResult,
  StagingContext,
  StagingGeneration,
} from "../src/outbox/stage-outbox-batch.ts";
import { checkpointMigration } from "../src/outbox/stage-sync-checkpoint.ts";
import { completionMigration } from "../src/outbox/stage-sync-completion.ts";
import { schedulingMigration } from "../src/scheduling/schedule-background-polling.ts";
import { beginInitialSnapshot, snapshotAttemptsMigration } from "../src/snapshot/begin-initial-snapshot.ts";
import type { SnapshotAttempt } from "../src/snapshot/begin-initial-snapshot.ts";
import type { DatabaseRow, DatabaseValue, SenderDatabase } from "../src/storage/database.ts";
import { migrate } from "../src/storage/migrations.ts";
import { syncMigration } from "../src/sync/coordinate-sender-sync.ts";
import { openNodeDatabase } from "./support/node-database.ts";
import { NodeInstallationCipher } from "./support/node-installation-cipher.ts";

const CONTRACT = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../../../governance/scopes/stage-outbox-batch.2.json"), "utf8"));
const NAMED_FAILURES: ReadonlySet<string> = new Set(CONTRACT.named_failures.map((failure: { name: string }) => failure.name));
const INSTALLATION = "00000000-0000-4000-8000-000000000001";
const OTHER_INSTALLATION = "00000000-0000-4000-8000-000000000002";
const KEY_ALIAS = "synthetic-installation-key";
const UNREAD_SINCE = 1_791_000_000_123;
const NOW = 1_791_000_005_000;
const SENTINEL = "SENTINEL";
const BEFORE_TEN = [
  snapshotAttemptsMigration, outboxMigration, completionMigration, checkpointMigration, syncMigration,
  schedulingMigration, deliveryMigration, enrollmentMigration, operatorConditionsMigration,
];
const MIGRATIONS = [...BEFORE_TEN, changeOriginMigration];
const KINDS: readonly OutboxKind[] = ["capability-acknowledgement", "ingest-batch", "snapshot-completion", "change-completion", "sync-checkpoint"];
const ADVANCES_TABLE = `CREATE TABLE change_origin_advances (
  snapshot_id TEXT PRIMARY KEY NOT NULL, record_type TEXT NOT NULL,
  after_lineage INTEGER NOT NULL CHECK (after_lineage > 0),
  changes_token TEXT NOT NULL, unread_since_ms INTEGER NOT NULL,
  FOREIGN KEY (snapshot_id, record_type) REFERENCES snapshot_attempts (snapshot_id, record_type)) STRICT`;
const uuid = (n: number) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, "0")}`;
const utf8 = (text: string) => new TextEncoder().encode(text);
const projection = (n: number) => utf8(`{"synthetic":${n}}`);
const position = (label: string) => utf8(`synthetic-position-${label}`);
const token = (label: string) => `synthetic-changes-token-${label}`;
const tokens = (sql: string) => sql.replace(/\s+/g, " ").replace(/\s*([(),])\s*/g, "$1").trim();

type Fixture = Awaited<ReturnType<typeof fixture>>;

async function fixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const path = join(directory, "synthetic.sqlite");
  let database = await openNodeDatabase(path);
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 10 });
  const cipher = new NodeInstallationCipher();
  assert.equal((await cipher.create(KEY_ALIAS)).status, "ok");
  return {
    path,
    cipher,
    get database() { return database; },
    async reopen() { await database.close(); database = await openNodeDatabase(path); return database; },
  };
}

/** Begin an Initial Snapshot attempt through begin-initial-snapshot with a scripted token. */
async function begin(
  database: SenderDatabase,
  recordType: string,
  snapshotId: string,
  extra: { installationId?: string; abandoning?: string; changesToken?: string } = {},
): Promise<SnapshotAttempt> {
  const issuer = { async issue() {
    return { outcome: "issued" as const, changesToken: extra.changesToken ?? token(`pre-${snapshotId.slice(-2)}`), unreadSinceMs: UNREAD_SINCE };
  } };
  const result = await beginInitialSnapshot(
    database, issuer,
    { recordType, installationId: extra.installationId ?? INSTALLATION, ...(extra.abandoning ? { abandoning: extra.abandoning } : {}) },
    { randomUUID: () => snapshotId, now: () => NOW },
  );
  assert.equal(result.outcome, "begun", JSON.stringify(result));
  return (result as { attempt: SnapshotAttempt }).attempt;
}

/** A Reconciliation Snapshot attempt, written to the published attempt tables as stage-outbox-batch@1's suite does. */
async function reconciliationAttempt(database: SenderDatabase, recordType: string, snapshotId: string): Promise<void> {
  await database.execute(
    `INSERT INTO snapshot_attempts (snapshot_id, record_type, kind, installation_id, changes_token, unread_since_ms, begun_at_ms, abandoned_at_ms)
      VALUES (?, ?, 'reconciliation', ?, ?, ?, ?, NULL)`,
    [snapshotId, recordType, INSTALLATION, token(`pre-${snapshotId.slice(-2)}`), UNREAD_SINCE, NOW],
  );
  await database.execute("INSERT INTO current_snapshot_attempts (record_type, snapshot_id) VALUES (?, ?)", [recordType, snapshotId]);
}

function generation(overrides: Partial<StagingGeneration> = {}): StagingGeneration {
  return { generation: 3, maxRecordCount: 1000, maxMessageBytes: 65_536, projectionSchemas: { steps: [1], weight: [1] }, ...overrides };
}

function context(overrides: Partial<StagingContext> = {}): StagingContext {
  return { installationId: INSTALLATION, keyAlias: KEY_ALIAS, generation: generation(), ...overrides };
}

function snapshotPage(cycleId: string, overrides: Partial<SnapshotPage> = {}): SnapshotPage {
  return {
    source: "snapshot", recordType: "steps", cycleId, projectionSchemaVersion: 1, pageIndex: 1, readFrom: null,
    projections: [projection(1)], nextPosition: position("1"), exhausted: true,
    ...overrides,
  };
}

function changePage(cycleId: string, readFrom: string, overrides: Partial<ChangePage> = {}): ChangePage {
  return {
    source: "change", recordType: "steps", cycleId, projectionSchemaVersion: 1, pageIndex: 1, readFrom,
    items: [{ kind: "projection", bytes: projection(11) }],
    nextChangesToken: token(`${cycleId.slice(-2)}-next`), nextUnreadSinceMs: UNREAD_SINCE + 1000, hasMore: false,
    ...overrides,
  };
}

/** A quiet read: the first page of a fresh identity, with no items and no further page. */
function quietPage(cycleId: string, readFrom: string, label: string, overrides: Partial<ChangePage> = {}): ChangePage {
  return changePage(cycleId, readFrom, { items: [], hasMore: false, nextChangesToken: token(`quiet-${label}`), nextUnreadSinceMs: UNREAD_SINCE + 5000, ...overrides });
}

/** Delegates to a NodeInstallationCipher; a script entry runs a hook before the numbered seal call (from 1). */
class SpyCipher implements InstallationCipher {
  private calls = 0;
  private readonly inner: InstallationCipher;
  private readonly hooks: Record<number, () => Promise<void>>;
  constructor(inner: InstallationCipher, hooks: Record<number, () => Promise<void>>) { this.inner = inner; this.hooks = hooks; }
  async seal(alias: string, plaintext: Uint8Array, aad: Uint8Array): Promise<CipherResult<Uint8Array>> {
    await this.hooks[++this.calls]?.();
    return this.inner.seal(alias, plaintext, aad);
  }
  create(alias: string) { return this.inner.create(alias); }
  keyStatus(alias: string): Promise<InstallationKeyStatus> { return this.inner.keyStatus(alias); }
  open(alias: string, sealed: Uint8Array, aad: Uint8Array) { return this.inner.open(alias, sealed, aad); }
  delete(alias: string) { return this.inner.delete(alias); }
  isDeviceSecure() { return this.inner.isDeviceSecure(); }
}

async function stage(f: Fixture, page: ObservedPage, options: { cipher?: InstallationCipher; context?: StagingContext } = {}): Promise<StageResult> {
  return stageOutboxBatch(f.database, options.cipher ?? f.cipher, options.context ?? context(), page);
}

function staged(result: StageResult): Extract<StageResult, { outcome: "staged" }> {
  assert.equal(result.outcome, "staged", JSON.stringify(result));
  return result as Extract<StageResult, { outcome: "staged" }>;
}

function advanced(result: AdvanceResult): ChangeOrigin {
  assert.equal(result.outcome, "advanced", JSON.stringify(result));
  return (result as Extract<AdvanceResult, { outcome: "advanced" }>).origin;
}

/**
 * A record type whose snapshot is finished and whose catch-up Change Cycle (lineage 1) is staged and finished,
 * so a quiet read may advance its origin. Returns the attempt and the origin after the catch-up.
 */
async function established(f: Fixture, options: { recordType?: string; snapshotId?: string; catchUp?: string } = {}) {
  const recordType = options.recordType ?? "steps";
  const snapshotId = options.snapshotId ?? uuid(0x51);
  const attempt = await begin(f.database, recordType, snapshotId);
  staged(await stage(f, snapshotPage(snapshotId, { recordType })));
  staged(await stage(f, changePage(options.catchUp ?? uuid(0xc1), attempt.changesToken, { recordType, items: [] })));
  const origin = await changeOrigin(f.database, recordType);
  assert.ok(origin);
  return { attempt, origin };
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

/** Rows that carry a `sealed` value or any byte array. */
function ciphertextRows(rows: readonly DatabaseRow[]): DatabaseRow[] {
  return rows.filter((row) => "sealed" in row || Object.values(row).some((value) => value instanceof Uint8Array));
}

const MUTATION = /^\s*(?:WITH\b[\s\S]*?\b)?(INSERT|UPDATE|DELETE|REPLACE)\b/i;

/** Fails one step: a numbered mutating statement, every read, every read inside the transaction, or the commit. */
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

/** Runs `hook` on the inner database just before the first transaction opens, then delegates everything. */
function beforeTransaction(inner: SenderDatabase, hook: () => Promise<void>): SenderDatabase {
  let ran = false;
  return {
    execute: (sql: string, parameters?: readonly DatabaseValue[]) => inner.execute(sql, parameters),
    query: (sql: string, parameters?: readonly DatabaseValue[]) => inner.query(sql, parameters),
    async transaction<T>(fn: (transaction: SenderDatabase) => T | Promise<T>): Promise<T> {
      if (!ran) { ran = true; await hook(); }
      return inner.transaction(fn);
    },
    close: () => inner.close(),
  };
}

/** A database whose every use throws and is counted, to prove a check comes before storage. */
function untouchable(): { database: SenderDatabase; calls: () => number } {
  let calls = 0;
  const refuse = () => { calls++; throw new Error(`${SENTINEL} storage must not be touched`); };
  return {
    calls: () => calls,
    database: { execute: async () => refuse(), query: async () => refuse(), transaction: async () => refuse(), close: async () => undefined },
  };
}

/** Append synthetic sealed rows directly through the shared primitive; their bytes are never opened. */
async function append(database: SenderDatabase, installationId: string, entries: readonly { kind: OutboxKind; bytes: number }[]): Promise<void> {
  const [row] = await database.query("SELECT last_sequence FROM outbox_high_water WHERE installation_id = ?", [installationId]);
  const last = row ? row.last_sequence as number : 0;
  const ok = await database.transaction((transaction) => appendOutboxMessages(transaction, installationId, last,
    entries.map((entry, index) => ({ kind: entry.kind, sealed: utf8(`sealed-${installationId.slice(-2)}-${last + index + 1}`), messageBytes: entry.bytes }))));
  assert.equal(ok, true);
}

/** What @1's outboxMessages gives for one installation: the oracle for R14 and R15. */
async function oracle(database: SenderDatabase, installationId: string) {
  const rows = (await outboxMessages(database)).filter((row) => row.installationId === installationId);
  return { messageCount: rows.length, byteCount: rows.reduce((sum, row) => sum + row.messageBytes, 0), sequences: rows.map((row) => row.sequence) };
}

async function remove(database: SenderDatabase, installationId: string, sequence: number): Promise<void> {
  await database.execute("DELETE FROM outbox_messages WHERE installation_id = ? AND sequence = ?", [installationId, sequence]);
}

// R14 — The backlog without ciphertext

test("backlog_matches_the_outbox_for_one_installation", async (t) => {
  const f = await fixture(t);
  await append(f.database, INSTALLATION, KINDS.map((kind, index) => ({ kind, bytes: 100 + index * 37 })));
  await append(f.database, OTHER_INSTALLATION, [{ kind: "ingest-batch", bytes: 9_999 }, { kind: "sync-checkpoint", bytes: 77 }]);
  await append(f.database, INSTALLATION, [{ kind: "ingest-batch", bytes: 4096 }]);
  for (const installationId of [INSTALLATION, OTHER_INSTALLATION]) {
    const expected = await oracle(f.database, installationId);
    assert.deepEqual(await outboxBacklog(f.database, installationId), { messageCount: expected.messageCount, byteCount: expected.byteCount });
  }
  assert.deepEqual(await outboxBacklog(f.database, INSTALLATION), { messageCount: 6, byteCount: 100 + 137 + 174 + 211 + 248 + 4096 });
});

test("backlog_is_zero_without_messages", async (t) => {
  const f = await fixture(t);
  assert.deepEqual(await outboxBacklog(f.database, INSTALLATION), { messageCount: 0, byteCount: 0 });
  await append(f.database, OTHER_INSTALLATION, [{ kind: "ingest-batch", bytes: 500 }]);
  assert.deepEqual(await outboxBacklog(f.database, INSTALLATION), { messageCount: 0, byteCount: 0 });
  await append(f.database, INSTALLATION, [{ kind: "ingest-batch", bytes: 500 }, { kind: "change-completion", bytes: 60 }]);
  await remove(f.database, INSTALLATION, 1);
  await remove(f.database, INSTALLATION, 2);
  // The high-water mark stays at 2; the backlog follows the rows, not the mark.
  assert.deepEqual(await outboxBacklog(f.database, INSTALLATION), { messageCount: 0, byteCount: 0 });
});

test("backlog_follows_removals", async (t) => {
  const f = await fixture(t);
  await append(f.database, INSTALLATION, [1, 2, 3, 4, 5, 6].map((n) => ({ kind: "ingest-batch" as const, bytes: n * 100 })));
  await remove(f.database, INSTALLATION, 1);
  await remove(f.database, INSTALLATION, 4);
  let expected = await oracle(f.database, INSTALLATION);
  assert.deepEqual(expected.sequences, [2, 3, 5, 6]);
  assert.deepEqual(await outboxBacklog(f.database, INSTALLATION), { messageCount: 4, byteCount: 1600 });
  await append(f.database, INSTALLATION, [{ kind: "sync-checkpoint", bytes: 333 }]);
  expected = await oracle(f.database, INSTALLATION);
  assert.deepEqual(await outboxBacklog(f.database, INSTALLATION), { messageCount: expected.messageCount, byteCount: expected.byteCount });
});

test("backlog_reads_no_sealed_bytes", async (t) => {
  const f = await fixture(t);
  await append(f.database, INSTALLATION, Array.from({ length: 50 }, (_, index) => ({ kind: KINDS[index % KINDS.length], bytes: 64 + index })));
  await append(f.database, OTHER_INSTALLATION, [{ kind: "ingest-batch", bytes: 64 }]);
  const watched = observing(f.database);
  assert.deepEqual(await outboxBacklog(watched.database, INSTALLATION), (({ messageCount, byteCount }) => ({ messageCount, byteCount }))(await oracle(f.database, INSTALLATION)));
  assert.ok(watched.rows.length > 0, "the backlog is read through the database");
  assert.deepEqual(ciphertextRows(watched.rows), []);
});

test("backlog_writes_nothing", async (t) => {
  const f = await fixture(t);
  await established(f);
  await append(f.database, OTHER_INSTALLATION, [{ kind: "ingest-batch", bytes: 64 }]);
  const before = await contents(f.database);
  await outboxBacklog(f.database, INSTALLATION);
  await outboxBacklog(f.database, OTHER_INSTALLATION);
  assert.deepEqual(await contents(f.database), before);
});

// R15 — Whether a sequence is queued, without ciphertext

test("holds_reports_a_queued_sequence", async (t) => {
  const f = await fixture(t);
  await append(f.database, INSTALLATION, KINDS.map((kind) => ({ kind, bytes: 80 })));
  for (const sequence of [1, 2, 3, 4, 5]) assert.equal(await outboxHolds(f.database, INSTALLATION, sequence), true, `sequence ${sequence}`);
});

test("holds_is_false_when_absent_removed_or_foreign", async (t) => {
  const f = await fixture(t);
  await append(f.database, INSTALLATION, [{ kind: "capability-acknowledgement", bytes: 90 }, { kind: "ingest-batch", bytes: 90 }, { kind: "ingest-batch", bytes: 90 }]);
  await append(f.database, OTHER_INSTALLATION, [{ kind: "ingest-batch", bytes: 90 }]);
  assert.equal(await outboxHolds(f.database, INSTALLATION, 4), false, "not yet staged");
  await remove(f.database, INSTALLATION, 1);
  assert.equal(await outboxHolds(f.database, INSTALLATION, 1), false, "removed after its receipt");
  assert.equal(await outboxHolds(f.database, INSTALLATION, 2), true);
  assert.equal(await outboxHolds(f.database, OTHER_INSTALLATION, 2), false, "another installation's sequence");
  assert.equal(await outboxHolds(f.database, OTHER_INSTALLATION, 1), true);
});

test("holds_reads_no_sealed_bytes_and_writes_nothing", async (t) => {
  const f = await fixture(t);
  await append(f.database, INSTALLATION, Array.from({ length: 20 }, () => ({ kind: "ingest-batch" as const, bytes: 70 })));
  const before = await contents(f.database);
  const watched = observing(f.database);
  assert.equal(await outboxHolds(watched.database, INSTALLATION, 7), true);
  assert.equal(await outboxHolds(watched.database, INSTALLATION, 99), false);
  assert.ok(watched.rows.length > 0, "membership is read through the database");
  assert.deepEqual(ciphertextRows(watched.rows), []);
  assert.deepEqual(await contents(f.database), before);
});

// R16 — A quiet change read advances the change origin

test("a_quiet_read_advances_the_origin", async (t) => {
  const f = await fixture(t);
  const { attempt, origin } = await established(f);
  const page = quietPage(uuid(0xc2), origin.changesToken, "a");
  const expected = { snapshotId: attempt.snapshotId, changesToken: page.nextChangesToken, unreadSinceMs: page.nextUnreadSinceMs };
  assert.deepEqual(advanced(await advanceChangeOrigin(f.database, context(), page)), expected);
  assert.deepEqual(await changeOrigin(f.database, "steps"), expected);
});

test("an_advance_stages_nothing", async (t) => {
  const f = await fixture(t);
  const { attempt, origin } = await established(f);
  const before = await contents(f.database);
  const page = quietPage(uuid(0xc2), origin.changesToken, "a");
  advanced(await advanceChangeOrigin(f.database, context(), page));
  const after = await contents(f.database);
  assert.deepEqual(after.schema, before.schema);
  assert.deepEqual(after.version, before.version);
  for (const [table, rows] of Object.entries(before.tables)) {
    if (table !== "change_origin_advances") assert.deepEqual(after.tables[table], rows, table);
  }
  assert.deepEqual(after.tables.change_origin_advances, [{
    snapshot_id: attempt.snapshotId, record_type: "steps", after_lineage: 1,
    changes_token: page.nextChangesToken, unread_since_ms: page.nextUnreadSinceMs,
  }]);
  assert.equal(await cycleProgress(f.database, uuid(0xc2)), null, "the quiet identity stays unstaged");
});

test("the_next_change_cycle_reads_from_the_advanced_origin", async (t) => {
  const f = await fixture(t);
  const { origin } = await established(f);
  const next = advanced(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc2), origin.changesToken, "a")));
  const before = await contents(f.database);
  assert.deepEqual(await stage(f, changePage(uuid(0xc3), origin.changesToken)), { outcome: "refused", failure: "page-position-mismatch" });
  assert.deepEqual(await contents(f.database), before);
  const result = staged(await stage(f, changePage(uuid(0xc3), next.changesToken)));
  assert.equal(result.progress.changesToken, token("c3-next"));
  const [row] = await f.database.query("SELECT lineage FROM cycle_progress WHERE cycle_id = ?", [uuid(0xc3)]);
  assert.equal(row.lineage, 2);
});

test("an_unstaged_identity_may_be_reused_after_an_advance", async (t) => {
  const f = await fixture(t);
  const { origin } = await established(f);
  const next = advanced(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc2), origin.changesToken, "a")));
  const result = staged(await stage(f, changePage(uuid(0xc2), next.changesToken)));
  assert.equal(result.progress.cycleId, uuid(0xc2));
  assert.equal(result.progress.finished, true);
});

test("advances_chain_from_the_latest_origin", async (t) => {
  const f = await fixture(t);
  const { attempt, origin } = await established(f);
  const first = advanced(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc2), origin.changesToken, "a")));
  const before = await contents(f.database);
  assert.deepEqual(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc3), origin.changesToken, "b")),
    { outcome: "refused", failure: "page-position-mismatch" });
  assert.deepEqual(await contents(f.database), before);
  const second = advanced(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc2), first.changesToken, "b")));
  assert.equal(second.changesToken, token("quiet-b"));
  assert.deepEqual(await changeOrigin(f.database, "steps"), second);
  const rows = await f.database.query("SELECT snapshot_id, after_lineage FROM change_origin_advances");
  assert.deepEqual(rows, [{ snapshot_id: attempt.snapshotId, after_lineage: 1 }]);
});

test("a_finished_later_cycle_supersedes_the_advance", async (t) => {
  const f = await fixture(t);
  const { attempt, origin } = await established(f);
  const quiet = advanced(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc2), origin.changesToken, "a")));
  // An unfinished later cycle does not move the origin.
  staged(await stage(f, changePage(uuid(0xc3), quiet.changesToken, { hasMore: true, nextChangesToken: token("c3-1") })));
  assert.deepEqual(await changeOrigin(f.database, "steps"), quiet);
  staged(await stage(f, changePage(uuid(0xc3), token("c3-1"), { pageIndex: 2, nextChangesToken: token("c3-final") })));
  assert.deepEqual(await changeOrigin(f.database, "steps"),
    { snapshotId: attempt.snapshotId, changesToken: token("c3-final"), unreadSinceMs: UNREAD_SINCE + 1000 });
  assert.deepEqual(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc4), quiet.changesToken, "b")),
    { outcome: "refused", failure: "page-position-mismatch" });
  advanced(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc4), token("c3-final"), "b")));
  const rows = await f.database.query("SELECT snapshot_id, after_lineage, changes_token FROM change_origin_advances");
  assert.deepEqual(rows, [{ snapshot_id: attempt.snapshotId, after_lineage: 2, changes_token: token("quiet-b") }]);
});

test("an_advance_during_sealing_makes_staging_conflict", async (t) => {
  const f = await fixture(t);
  const { origin } = await established(f);
  let advance: AdvanceResult | undefined;
  const spy = new SpyCipher(f.cipher, { 1: async () => {
    advance = await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc2), origin.changesToken, "a"));
  } });
  assert.deepEqual(await stage(f, changePage(uuid(0xc3), origin.changesToken), { cipher: spy }), { outcome: "conflict", failure: "staging-conflict" });
  assert.ok(advance);
  const quiet = advanced(advance);
  assert.equal(await cycleProgress(f.database, uuid(0xc3)), null);
  assert.deepEqual(await changeOrigin(f.database, "steps"), quiet);
  assert.deepEqual((await outboxMessages(f.database)).map((row) => row.sequence), [1, 2], "only the snapshot and catch-up fragments");
});

test("an_unread_since_only_advance_during_sealing_makes_staging_conflict", async (t) => {
  const f = await fixture(t);
  const { origin } = await established(f);
  const first = advanced(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc2), origin.changesToken, "a")));
  // A quiet read may return the token it was read from with a later unread-since instant, so only the
  // advance's unread-since changes while the next page is being sealed.
  const same = quietPage(uuid(0xc2), first.changesToken, "b", { nextChangesToken: first.changesToken, nextUnreadSinceMs: first.unreadSinceMs + 4000 });
  let advance: AdvanceResult | undefined;
  const spy = new SpyCipher(f.cipher, { 1: async () => { advance = await advanceChangeOrigin(f.database, context(), same); } });
  assert.deepEqual(await stage(f, changePage(uuid(0xc3), first.changesToken), { cipher: spy }), { outcome: "conflict", failure: "staging-conflict" });
  assert.ok(advance);
  assert.deepEqual(advanced(advance), { snapshotId: origin.snapshotId, changesToken: first.changesToken, unreadSinceMs: first.unreadSinceMs + 4000 });
  assert.equal(await cycleProgress(f.database, uuid(0xc3)), null);
  assert.deepEqual((await outboxMessages(f.database)).map((row) => row.sequence), [1, 2], "only the snapshot and catch-up fragments");
});

test("a_new_attempt_ignores_the_old_advance", async (t) => {
  const f = await fixture(t);
  const { origin } = await established(f);
  advanced(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc2), origin.changesToken, "a")));
  const second = await begin(f.database, "steps", uuid(0x52), { abandoning: uuid(0x51) });
  assert.deepEqual(await changeOrigin(f.database, "steps"),
    { snapshotId: second.snapshotId, changesToken: second.changesToken, unreadSinceMs: second.unreadSinceMs });
  assert.deepEqual(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc3), second.changesToken, "b")),
    { outcome: "refused", failure: "snapshot-unfinished" });
  staged(await stage(f, snapshotPage(second.snapshotId)));
  assert.deepEqual(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc3), second.changesToken, "b")),
    { outcome: "refused", failure: "catch-up-unstaged" });
  staged(await stage(f, changePage(uuid(0xc3), second.changesToken, { items: [], nextChangesToken: token("c3-final") })));
  assert.deepEqual(await changeOrigin(f.database, "steps"),
    { snapshotId: second.snapshotId, changesToken: token("c3-final"), unreadSinceMs: UNREAD_SINCE + 1000 });
});

test("an_advance_survives_reopening", async (t) => {
  const f = await fixture(t);
  const { origin } = await established(f);
  const quiet = advanced(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc2), origin.changesToken, "a")));
  await f.reopen();
  assert.deepEqual(await changeOrigin(f.database, "steps"), quiet);
  staged(await stage(f, changePage(uuid(0xc3), quiet.changesToken)));
});

// R17 — Refusals and failures of an advance

test("the_catch_up_cycle_is_never_skipped", async (t) => {
  const f = await fixture(t);
  // An Initial Snapshot whose snapshot chain is finished but whose catch-up cycle is not yet staged.
  const attempt = await begin(f.database, "steps", uuid(0x51));
  staged(await stage(f, snapshotPage(attempt.snapshotId)));
  const before = await contents(f.database);
  const catchUp = quietPage(uuid(0xc1), attempt.changesToken, "a");
  assert.deepEqual(await advanceChangeOrigin(f.database, context(), catchUp), { outcome: "refused", failure: "catch-up-unstaged" });
  assert.deepEqual(await contents(f.database), before);
  const result = staged(await stage(f, catchUp));
  assert.equal(result.progress.fragmentCount, 1, "an empty catch-up stages one empty fragment");
  // A Reconciliation Snapshot's catch-up is never skipped either.
  await reconciliationAttempt(f.database, "weight", uuid(0x61));
  staged(await stage(f, snapshotPage(uuid(0x61), { recordType: "weight" })));
  const reconciliation = (await changeOrigin(f.database, "weight"))!;
  assert.deepEqual(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xd1), reconciliation.changesToken, "b", { recordType: "weight" })),
    { outcome: "refused", failure: "catch-up-unstaged" });
});

test("a_page_that_is_not_quiet_is_refused", async (t) => {
  const f = await fixture(t);
  const { origin } = await established(f);
  const before = await contents(f.database);
  const notQuiet = { outcome: "refused", failure: "change-page-not-quiet" };
  assert.deepEqual(await advanceChangeOrigin(f.database, context(), changePage(uuid(0xc2), origin.changesToken)), notQuiet, "items");
  assert.deepEqual(await advanceChangeOrigin(f.database, context(),
    changePage(uuid(0xc2), origin.changesToken, { items: [{ kind: "retraction", sourceIdentity: "synthetic-source-1" }] })), notQuiet, "a retraction");
  assert.deepEqual(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc2), origin.changesToken, "a", { hasMore: true })), notQuiet, "more pages");
  assert.deepEqual(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc2), origin.changesToken, "a", { pageIndex: 2 })), notQuiet, "a later page");
  assert.deepEqual(await contents(f.database), before);
});

test("advance_refusals_write_nothing", async (t) => {
  const f = await fixture(t);
  const { origin } = await established(f);
  // weight: an attempt under another installation, its snapshot and catch-up staged under that installation.
  const foreign = await begin(f.database, "weight", uuid(0x71), { installationId: OTHER_INSTALLATION });
  const otherContext = context({ installationId: OTHER_INSTALLATION });
  staged(await stage(f, snapshotPage(foreign.snapshotId, { recordType: "weight" }), { context: otherContext }));
  staged(await stage(f, changePage(uuid(0xe1), foreign.changesToken, { recordType: "weight", items: [] }), { context: otherContext }));
  const withBodyFat = context({ generation: generation({ projectionSchemas: { steps: [1], weight: [1], "body-fat": [1] } }) });
  const cases: [string, ChangePage, StagingContext?][] = [
    ["change-page-not-quiet", changePage(uuid(0xc2), origin.changesToken)],
    ["projection-schema-unsupported", quietPage(uuid(0xc2), origin.changesToken, "a", { projectionSchemaVersion: 9 })],
    ["projection-schema-unsupported", quietPage(uuid(0xc2), origin.changesToken, "a", { recordType: "heart-rate" })],
    // A supported type that was never begun has no current attempt.
    ["cycle-not-current", quietPage(uuid(0xc2), token("none"), "a", { recordType: "body-fat" }), withBodyFat],
    // The page names a snapshot attempt's identity.
    ["cycle-not-current", quietPage(uuid(0x51), origin.changesToken, "a")],
    ["installation-mismatch", quietPage(uuid(0xe2), foreign.changesToken, "a", { recordType: "weight" })],
    ["cycle-finished", quietPage(uuid(0xc1), origin.changesToken, "a")],
    ["page-position-mismatch", quietPage(uuid(0xc2), token("stale"), "a")],
  ];
  for (const [failure, page, pageContext] of cases) {
    const before = await contents(f.database);
    assert.deepEqual(await advanceChangeOrigin(f.database, pageContext ?? context(), page), { outcome: "refused", failure }, failure);
    assert.deepEqual(await contents(f.database), before, failure);
  }
  // snapshot-unfinished and change-cycle-open, including the page's own staged, unfinished identity.
  await begin(f.database, "steps", uuid(0x53), { abandoning: uuid(0x51) });
  let before = await contents(f.database);
  assert.deepEqual(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc5), token("pre-53"), "a")),
    { outcome: "refused", failure: "snapshot-unfinished" });
  assert.deepEqual(await contents(f.database), before);
  staged(await stage(f, snapshotPage(uuid(0x53))));
  staged(await stage(f, changePage(uuid(0xc6), token("pre-53"), { items: [] })));
  staged(await stage(f, changePage(uuid(0xc7), token("c6-next"), { hasMore: true, nextChangesToken: token("c7-1") })));
  before = await contents(f.database);
  for (const cycleId of [uuid(0xc8), uuid(0xc7)]) {
    assert.deepEqual(await advanceChangeOrigin(f.database, context(), quietPage(cycleId, token("c6-next"), "b")),
      { outcome: "refused", failure: "change-cycle-open" }, cycleId);
    assert.deepEqual(await contents(f.database), before, cycleId);
  }
  // cycle-not-current for a staged cycle under the abandoned attempt.
  assert.deepEqual(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc1), token("c6-next"), "b")),
    { outcome: "refused", failure: "cycle-not-current" });
  assert.deepEqual(await contents(f.database), before);
});

test("the_advance_rechecks_currency_and_open_cycles_in_its_transaction", async (t) => {
  const f = await fixture(t);
  const { origin } = await established(f);
  // A Change Cycle opens just before the advance's transaction does.
  const opened = beforeTransaction(f.database, async () => {
    staged(await stage(f, changePage(uuid(0xc3), origin.changesToken, { hasMore: true, nextChangesToken: token("c3-1") })));
  });
  assert.deepEqual(await advanceChangeOrigin(opened, context(), quietPage(uuid(0xc2), origin.changesToken, "a")),
    { outcome: "refused", failure: "change-cycle-open" });
  assert.deepEqual(await f.database.query("SELECT * FROM change_origin_advances"), []);
  // A new attempt replaces the current one just before the advance's transaction opens.
  const g = await fixture(t);
  const second = await established(g);
  let replacement: SnapshotAttempt | undefined;
  const replaced = beforeTransaction(g.database, async () => {
    replacement = await begin(g.database, "steps", uuid(0x52), { abandoning: uuid(0x51) });
  });
  const result = await advanceChangeOrigin(replaced, context(), quietPage(uuid(0xc2), second.origin.changesToken, "a"));
  assert.equal(result.outcome, "refused", JSON.stringify(result));
  assert.deepEqual(await g.database.query("SELECT * FROM change_origin_advances"), []);
  assert.ok(replacement);
  assert.deepEqual(await changeOrigin(g.database, "steps"),
    { snapshotId: replacement.snapshotId, changesToken: replacement.changesToken, unreadSinceMs: replacement.unreadSinceMs });
});

test("advance_storage_errors_roll_back", async (t) => {
  const f = await fixture(t);
  const { origin } = await established(f);
  const rules = [{ queries: true }, { transactionQueries: true }, { mutation: 1 }, { commit: true }];
  for (const rule of rules) {
    const before = await contents(f.database);
    assert.deepEqual(await advanceChangeOrigin(failing(f.database, rule), context(), quietPage(uuid(0xc2), origin.changesToken, "a")),
      { outcome: "storage-failed", failure: "storage-failed" }, JSON.stringify(rule));
    assert.deepEqual(await contents(f.database), before, JSON.stringify(rule));
  }
  advanced(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc2), origin.changesToken, "a")));
});

test("a_database_below_version_ten_behaves_as_at_one", async (t) => {
  // stage-outbox-batch@1's suite, and every published suite that stages through this module, migrates below
  // version 10, where no advance can exist.
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const database = await openNodeDatabase(join(directory, "synthetic.sqlite"));
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.deepEqual(await migrate(database, [snapshotAttemptsMigration, outboxMigration]), { status: "ready", version: 2 });
  const cipher = new NodeInstallationCipher();
  assert.equal((await cipher.create(KEY_ALIAS)).status, "ok");
  const attempt = await begin(database, "steps", uuid(0x51));
  assert.deepEqual(await changeOrigin(database, "steps"),
    { snapshotId: attempt.snapshotId, changesToken: attempt.changesToken, unreadSinceMs: attempt.unreadSinceMs });
  staged(await stageOutboxBatch(database, cipher, context(), snapshotPage(attempt.snapshotId)));
  staged(await stageOutboxBatch(database, cipher, context(), changePage(uuid(0xc1), attempt.changesToken, { items: [] })));
  assert.deepEqual(await changeOrigin(database, "steps"),
    { snapshotId: attempt.snapshotId, changesToken: token("c1-next"), unreadSinceMs: UNREAD_SINCE + 1000 });
  staged(await stageOutboxBatch(database, cipher, context(), changePage(uuid(0xc2), token("c1-next"))));
  const before = await contents(database);
  assert.deepEqual(await advanceChangeOrigin(database, context(), quietPage(uuid(0xc3), token("c2-next"), "a")),
    { outcome: "storage-failed", failure: "storage-failed" });
  assert.deepEqual(await contents(database), before);
});

test("a_failed_chained_advance_keeps_the_previous_origin", async (t) => {
  const f = await fixture(t);
  const { origin } = await established(f);
  const first = advanced(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc2), origin.changesToken, "a")));
  const page = quietPage(uuid(0xc2), first.changesToken, "b");
  const rules: Parameters<typeof failing>[1][] = [{ queries: true }, { transactionQueries: true }, { commit: true }];
  // Fail each mutating statement in turn until the advance needs no more of them.
  for (let mutation = 1; mutation <= 8; mutation++) rules.push({ mutation });
  let succeeded = false;
  for (const rule of rules) {
    const before = await contents(f.database);
    const result = await advanceChangeOrigin(failing(f.database, rule), context(), page);
    if (result.outcome === "advanced") {
      assert.ok(rule.mutation !== undefined && rule.mutation > 1, `only a mutation beyond the advance's last may let it succeed: ${JSON.stringify(rule)}`);
      succeeded = true;
      break;
    }
    assert.deepEqual(result, { outcome: "storage-failed", failure: "storage-failed" }, JSON.stringify(rule));
    assert.deepEqual(await contents(f.database), before, JSON.stringify(rule));
    assert.deepEqual(await changeOrigin(f.database, "steps"), first, JSON.stringify(rule));
  }
  assert.ok(succeeded, "the advance writes at most seven statements");
  assert.equal((await changeOrigin(f.database, "steps"))?.changesToken, token("quiet-b"));
});

// R18 — The Sender schema migration, version 10

test("migration_creates_version_ten", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const database = await openNodeDatabase(join(directory, "synthetic.sqlite"));
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.deepEqual(await migrate(database, BEFORE_TEN), { status: "ready", version: 9 });
  const nine = await database.query("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name");
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 10 });
  const ten = await database.query("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name");
  const added = ten.filter((row) => !nine.some((old) => JSON.stringify(old) === JSON.stringify(row)));
  assert.equal(ten.length - nine.length, added.length, "no earlier schema object changes");
  assert.deepEqual(added.map((row) => [row.type, row.name]).sort(),
    [["index", "sqlite_autoindex_change_origin_advances_1"], ["table", "change_origin_advances"]]);
  assert.equal(tokens(added.find((row) => row.type === "table")!.sql as string), tokens(ADVANCES_TABLE));
});

test("migration_ten_is_idempotent", async (t) => {
  const f = await fixture(t);
  const { origin } = await established(f);
  advanced(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc2), origin.changesToken, "a")));
  const before = await contents(f.database);
  assert.deepEqual(await migrate(f.database, MIGRATIONS), { status: "ready", version: 10 });
  assert.deepEqual(await contents(f.database), before);
});

// R19 — Caller errors, health-data-free outcomes, and order of checks

test("new_caller_errors_touch_no_storage", async () => {
  const { database, calls } = untouchable();
  for (const installationId of ["not-a-uuid", uuid(0xab).toUpperCase(), "", 7]) {
    await assert.rejects(outboxBacklog(database, installationId as string), TypeError, String(installationId));
    await assert.rejects(outboxHolds(database, installationId as string, 1), TypeError, String(installationId));
  }
  for (const sequence of [0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY, 2 ** 53, "1", null]) {
    await assert.rejects(outboxHolds(database, INSTALLATION, sequence as number), TypeError, String(sequence));
  }
  const page = quietPage(uuid(0xc2), token("origin"), "a");
  const malformedPages: unknown[] = [
    snapshotPage(uuid(0xc2)),
    { ...page, source: "other" },
    { ...page, cycleId: "not-a-uuid" },
    { ...page, recordType: "Steps" },
    { ...page, readFrom: "" },
    { ...page, nextChangesToken: 7 },
    { ...page, nextUnreadSinceMs: 1.5 },
    { ...page, hasMore: "no" },
    { ...page, pageIndex: 0 },
    { ...page, projectionSchemaVersion: -1 },
    { ...page, items: [{ kind: "other" }] },
  ];
  for (const malformed of malformedPages) {
    await assert.rejects(advanceChangeOrigin(database, context(), malformed as ChangePage), TypeError, JSON.stringify(malformed));
  }
  const malformedContexts: unknown[] = [
    context({ installationId: "not-a-uuid" }),
    context({ keyAlias: "" }),
    context({ generation: generation({ maxMessageBytes: 100 }) }),
    context({ generation: generation({ projectionSchemas: new Map() as never }) }),
  ];
  for (const malformed of malformedContexts) {
    await assert.rejects(advanceChangeOrigin(database, malformed as StagingContext, page), TypeError, JSON.stringify(malformed));
  }
  assert.equal(calls(), 0);
});

test("advance_results_carry_only_named_failures", async (t) => {
  const f = await fixture(t);
  const { origin } = await established(f);
  const results = [
    await advanceChangeOrigin(f.database, context(), changePage(uuid(0xc2), origin.changesToken)),
    await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc2), token("stale"), "a")),
    await advanceChangeOrigin(failing(f.database, { queries: true }), context(), quietPage(uuid(0xc2), origin.changesToken, "a")),
  ];
  for (const result of results) {
    assert.deepEqual(Object.keys(result).sort(), ["failure", "outcome"]);
    assert.ok(NAMED_FAILURES.has((result as { failure: string }).failure), JSON.stringify(result));
    assert.doesNotMatch(JSON.stringify(result), new RegExp(`${SENTINEL}|synthetic-changes-token|synthetic`));
  }
});

test("new_named_failures_are_reachable_and_in_the_contract", async (t) => {
  const f = await fixture(t);
  const attempt = await begin(f.database, "steps", uuid(0x51));
  staged(await stage(f, snapshotPage(attempt.snapshotId)));
  const produced = new Set<string>();
  const record = (result: AdvanceResult) => { if (result.outcome !== "advanced") produced.add(result.failure); };
  record(await advanceChangeOrigin(f.database, context(), quietPage(uuid(0xc1), attempt.changesToken, "a")));
  record(await advanceChangeOrigin(f.database, context(), changePage(uuid(0xc1), attempt.changesToken)));
  assert.deepEqual([...produced].sort(), ["catch-up-unstaged", "change-page-not-quiet"]);
  for (const name of produced) {
    assert.deepEqual(CONTRACT.named_failures.find((failure: { name: string }) => failure.name === name), { name, outcome: "refusal" });
  }
});

test("advance_checks_follow_the_documented_order", async (t) => {
  const f = await fixture(t);
  const { origin } = await established(f);
  const foreign = await begin(f.database, "weight", uuid(0x71), { installationId: OTHER_INSTALLATION });
  const refused = (failure: string) => ({ outcome: "refused", failure });
  const ask = (page: ChangePage, database: SenderDatabase = f.database) => advanceChangeOrigin(database, context(), page);
  // 2 before 3: a page that is not quiet under an unsupported schema.
  assert.deepEqual(await ask(changePage(uuid(0xc2), origin.changesToken, { projectionSchemaVersion: 9 })), refused("change-page-not-quiet"));
  // 3 before 4: an unsupported schema, with a failing database.
  assert.deepEqual(await ask(quietPage(uuid(0xc2), origin.changesToken, "a", { projectionSchemaVersion: 9 }), failing(f.database, { queries: true })),
    refused("projection-schema-unsupported"));
  // 4: a failing read before any cycle check.
  assert.deepEqual(await ask(quietPage(uuid(0x51), token("stale"), "a"), failing(f.database, { queries: true })),
    { outcome: "storage-failed", failure: "storage-failed" });
  // cycle-not-current before installation-mismatch: the foreign attempt's own identity.
  assert.deepEqual(await ask(quietPage(foreign.snapshotId, token("stale"), "a", { recordType: "weight" })), refused("cycle-not-current"));
  // installation-mismatch before snapshot-unfinished and the position.
  assert.deepEqual(await ask(quietPage(uuid(0xe2), token("stale"), "a", { recordType: "weight" })), refused("installation-mismatch"));
  // cycle-finished before the position.
  assert.deepEqual(await ask(quietPage(uuid(0xc1), token("stale"), "a")), refused("cycle-finished"));
  // snapshot-unfinished before catch-up-unstaged and the position.
  await begin(f.database, "steps", uuid(0x52), { abandoning: uuid(0x51) });
  assert.deepEqual(await ask(quietPage(uuid(0xc3), token("stale"), "a")), refused("snapshot-unfinished"));
  // catch-up-unstaged before the position.
  staged(await stage(f, snapshotPage(uuid(0x52))));
  assert.deepEqual(await ask(quietPage(uuid(0xc3), token("stale"), "a")), refused("catch-up-unstaged"));
  // change-cycle-open before the position.
  staged(await stage(f, changePage(uuid(0xc3), token("pre-52"), { items: [] })));
  staged(await stage(f, changePage(uuid(0xc4), token("c3-next"), { hasMore: true })));
  assert.deepEqual(await ask(quietPage(uuid(0xc5), token("stale"), "a")), refused("change-cycle-open"));
});
