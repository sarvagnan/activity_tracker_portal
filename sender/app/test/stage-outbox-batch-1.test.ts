// Acceptance tests for stage-outbox-batch@1.
// Requirement numbers refer to packs/stage-outbox-batch-1.spec.md. Storage is the 3ug
// node:sqlite SenderDatabase substitute, the cipher is the 3ug NodeInstallationCipher, and
// snapshot attempts come from the published begin-initial-snapshot@1 with a scripted issuer.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import type { TestContext } from "node:test";
import { chainNext, chainStart, decodeMessage, messageSha256, partitionPage } from "@activity-tracker-portal/ingest-protocol";
import type { Message } from "@activity-tracker-portal/ingest-protocol";
import type { CipherResult, InstallationCipher, InstallationKeyStatus } from "../src/installation-cipher.ts";
import {
  appendOutboxMessages,
  changeOrigin,
  cycleProgress,
  lastOutboxSequence,
  outboxAad,
  outboxMessages,
  outboxMigration,
  stageOutboxBatch,
} from "../src/outbox/stage-outbox-batch.ts";
import type {
  ChangeItem,
  ChangePage,
  CycleProgress,
  ObservedPage,
  OutboxMessage,
  SnapshotPage,
  StageResult,
  StagingContext,
  StagingGeneration,
} from "../src/outbox/stage-outbox-batch.ts";
import { beginInitialSnapshot, snapshotAttemptsMigration } from "../src/snapshot/begin-initial-snapshot.ts";
import type { SnapshotAttempt } from "../src/snapshot/begin-initial-snapshot.ts";
import type { DatabaseValue, SenderDatabase } from "../src/storage/database.ts";
import { migrate } from "../src/storage/migrations.ts";
import { openNodeDatabase } from "./support/node-database.ts";
import { NodeInstallationCipher } from "./support/node-installation-cipher.ts";

const CONTRACT = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../../../governance/scopes/stage-outbox-batch.1.json"), "utf8"));
const INSTALLATION = "00000000-0000-4000-8000-000000000001";
const OTHER_INSTALLATION = "00000000-0000-4000-8000-000000000002";
const KEY_ALIAS = "synthetic-installation-key";
const UNREAD_SINCE = 1_791_000_000_123;
const NOW = 1_791_000_005_000;
const SENTINEL = "SENTINEL";
const MIGRATIONS = [snapshotAttemptsMigration, outboxMigration];
const uuid = (n: number) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, "0")}`;
const utf8 = (text: string) => new TextEncoder().encode(text);
const base64 = (bytes: Uint8Array) => Buffer.from(bytes).toString("base64");
/** A synthetic projection; `pad` lengthens it to exercise the byte limit. */
const projection = (n: number, pad = 0) => utf8(`{"synthetic":${n}${pad > 0 ? `,"pad":"${"x".repeat(pad)}"` : ""}}`);
const position = (label: string) => utf8(`synthetic-position-${label}`);
const token = (label: string) => `synthetic-changes-token-${label}`;

async function fixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const path = join(directory, "synthetic.sqlite");
  let database = await openNodeDatabase(path);
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 2 });
  const cipher = new NodeInstallationCipher();
  assert.equal((await cipher.create(KEY_ALIAS)).status, "ok");
  return {
    path,
    cipher,
    get database() { return database; },
    async reopen() { await database.close(); database = await openNodeDatabase(path); return database; },
  };
}

/** Begin an Initial Snapshot attempt through begin-initial-snapshot@1 with a scripted token. */
async function begin(
  database: SenderDatabase,
  recordType: string,
  snapshotId: string,
  extra: { installationId?: string; abandoning?: string; changesToken?: string; unreadSinceMs?: number } = {},
): Promise<SnapshotAttempt> {
  const issuer = { async issue() {
    return { outcome: "issued" as const, changesToken: extra.changesToken ?? token(snapshotId.slice(-2)), unreadSinceMs: extra.unreadSinceMs ?? UNREAD_SINCE };
  } };
  const result = await beginInitialSnapshot(
    database, issuer,
    { recordType, installationId: extra.installationId ?? INSTALLATION, ...(extra.abandoning ? { abandoning: extra.abandoning } : {}) },
    { randomUUID: () => snapshotId, now: () => NOW },
  );
  assert.equal(result.outcome, "begun", JSON.stringify(result));
  return (result as { attempt: SnapshotAttempt }).attempt;
}

/**
 * A Reconciliation Snapshot attempt. begin-reconciliation-snapshot is not yet delivered, so the
 * fixture writes the published attempt tables directly, as that unit will through their module.
 */
async function reconciliationAttempt(database: SenderDatabase, recordType: string, snapshotId: string): Promise<void> {
  await database.execute(
    `INSERT INTO snapshot_attempts (snapshot_id, record_type, kind, installation_id, changes_token, unread_since_ms, begun_at_ms, abandoned_at_ms)
      VALUES (?, ?, 'reconciliation', ?, ?, ?, ?, NULL)`,
    [snapshotId, recordType, INSTALLATION, token(snapshotId.slice(-2)), UNREAD_SINCE, NOW],
  );
  await database.execute("INSERT INTO current_snapshot_attempts (record_type, snapshot_id) VALUES (?, ?)", [recordType, snapshotId]);
}

function generation(overrides: Partial<StagingGeneration> = {}): StagingGeneration {
  return {
    generation: 3, maxRecordCount: 1000, maxMessageBytes: 65_536,
    projectionSchemas: { "heart-rate": [1], steps: [1], weight: [1, 2] },
    ...overrides,
  };
}

function context(overrides: Partial<StagingContext> = {}): StagingContext {
  return { installationId: INSTALLATION, keyAlias: KEY_ALIAS, generation: generation(), ...overrides };
}

function snapshotPage(cycleId: string, overrides: Partial<SnapshotPage> = {}): SnapshotPage {
  return {
    source: "snapshot", recordType: "steps", cycleId, projectionSchemaVersion: 1, pageIndex: 1, readFrom: null,
    projections: [projection(1), projection(2)], nextPosition: position("1"), exhausted: false,
    ...overrides,
  };
}

function changePage(cycleId: string, readFrom: string, overrides: Partial<ChangePage> = {}): ChangePage {
  return {
    source: "change", recordType: "steps", cycleId, projectionSchemaVersion: 1, pageIndex: 1, readFrom,
    items: [{ kind: "retraction", sourceIdentity: "synthetic-source-1" }, { kind: "projection", bytes: projection(11) }],
    nextChangesToken: token(`${cycleId.slice(-2)}-1`), nextUnreadSinceMs: UNREAD_SINCE + 1000, hasMore: false,
    ...overrides,
  };
}

interface SealCall { readonly alias: string; readonly plaintext: Uint8Array; readonly aad: Uint8Array }

/**
 * Delegates to a NodeInstallationCipher, recording every seal. A script entry replaces the
 * numbered seal call (counted from 1) with a returned value, a thrown error, or a hook that
 * runs before the call is delegated.
 */
class SpyCipher implements InstallationCipher {
  readonly seals: SealCall[] = [];
  private readonly inner: NodeInstallationCipher;
  private readonly script: Map<number, { returns?: unknown; throws?: unknown; before?: () => Promise<void> }>;
  constructor(inner: NodeInstallationCipher, script: Record<number, { returns?: unknown; throws?: unknown; before?: () => Promise<void> }> = {}) {
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

/**
 * Passes every call through to `inner`, recording each call the unit is never allowed to make:
 * creating, deleting, or opening with an installation key.
 */
function guarded(inner: InstallationCipher): { cipher: InstallationCipher; forbidden: string[] } {
  const forbidden: string[] = [];
  return {
    forbidden,
    cipher: {
      seal: (alias, plaintext, aad) => inner.seal(alias, plaintext, aad),
      keyStatus: (alias) => inner.keyStatus(alias),
      isDeviceSecure: () => inner.isDeviceSecure(),
      create: (alias) => { forbidden.push("create"); return inner.create(alias); },
      delete: (alias) => { forbidden.push("delete"); return inner.delete(alias); },
      open: (alias, sealed, aad) => { forbidden.push("open"); return inner.open(alias, sealed, aad); },
    },
  };
}

/** Stage through a guarded cipher, failing if the unit created, deleted, or opened with a key. */
async function stage(
  f: { database: SenderDatabase; cipher: InstallationCipher },
  page: ObservedPage,
  options: { cipher?: InstallationCipher; context?: StagingContext; database?: SenderDatabase } = {},
): Promise<StageResult> {
  const guard = guarded(options.cipher ?? f.cipher);
  const result = await stageOutboxBatch(options.database ?? f.database, guard.cipher, options.context ?? context(), page);
  assert.deepEqual(guard.forbidden, [], "staging only seals");
  return result;
}

function staged(result: StageResult): Extract<StageResult, { outcome: "staged" }> {
  assert.equal(result.outcome, "staged", JSON.stringify(result));
  return result as Extract<StageResult, { outcome: "staged" }>;
}

/** Each Outbox row opened with its own authenticated data, as bytes and as a decoded message. */
async function opened(database: SenderDatabase, cipher: InstallationCipher, installationId = INSTALLATION) {
  const rows = (await outboxMessages(database)).filter((row) => row.installationId === installationId);
  const result: { sequence: number; bytes: Uint8Array; message: Message }[] = [];
  for (const row of rows) {
    const plain = await cipher.open(KEY_ALIAS, row.sealed, outboxAad(row.installationId, row.sequence));
    assert.equal(plain.status, "ok", `sequence ${row.sequence}`);
    const bytes = (plain as { value: Uint8Array }).value;
    result.push({ sequence: row.sequence, bytes, message: decodeMessage(bytes) });
  }
  return result;
}

/** Every row of every table this unit and begin-initial-snapshot@1 own, for before/after comparison. */
async function contents(database: SenderDatabase) {
  return {
    version: await database.query("PRAGMA user_version"),
    outbox: await database.query("SELECT * FROM outbox_messages ORDER BY installation_id, sequence"),
    highWater: await database.query("SELECT * FROM outbox_high_water ORDER BY installation_id"),
    progress: await database.query("SELECT * FROM cycle_progress ORDER BY cycle_id"),
    attempts: await database.query("SELECT * FROM snapshot_attempts ORDER BY snapshot_id"),
    current: await database.query("SELECT * FROM current_snapshot_attempts ORDER BY record_type"),
  };
}

const MUTATION = /^\s*(?:WITH\b[\s\S]*?\b)?(INSERT|UPDATE|DELETE|REPLACE)\b/i;

/**
 * Wraps a database so that one statement or step fails: the given mutating statement, counted
 * from 1; every read outside or inside the transaction; or the commit, simulated by failing after
 * the transaction body succeeds, which the 3ug seam rolls back.
 */
function failing(database: SenderDatabase, rule: { mutation?: number; queries?: boolean; transactionQueries?: boolean; commit?: boolean; everything?: boolean }): SenderDatabase {
  let mutations = 0;
  const guard = (sql: string, inTransaction: boolean) => {
    if (rule.everything) throw new Error(`${SENTINEL} synthetic database failure`);
    if (!MUTATION.test(sql) && (rule.queries || (rule.transactionQueries && inTransaction))) throw new Error(`${SENTINEL} synthetic read failure`);
    if (MUTATION.test(sql) && ++mutations === rule.mutation) throw new Error(`${SENTINEL} synthetic write failure`);
  };
  const wrap = (inner: SenderDatabase, inTransaction: boolean): SenderDatabase => ({
    async execute(sql: string, parameters?: readonly DatabaseValue[]) { guard(sql, inTransaction); return inner.execute(sql, parameters); },
    async query(sql: string, parameters?: readonly DatabaseValue[]) { guard(sql, inTransaction); return inner.query(sql, parameters); },
    transaction<T>(fn: (transaction: SenderDatabase) => T | Promise<T>): Promise<T> {
      if (rule.everything) return Promise.reject(new Error(`${SENTINEL} synthetic database failure`));
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

/** A database whose every use throws and is counted, to prove a check comes before storage. */
function untouchable(): { database: SenderDatabase; calls: () => number } {
  let calls = 0;
  const refuse = () => { calls++; throw new Error(`${SENTINEL} storage must not be touched`); };
  return {
    database: {
      async execute() { refuse(); },
      async query() { return refuse(); },
      async transaction() { return refuse(); },
      async close() {},
    },
    calls: () => calls,
  };
}

/** Runs `hook` once, just before the wrapped database opens its next transaction. */
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

/** The protocol partition the unit must reproduce for one page. */
function expectedPartition(page: ObservedPage, cycleKind: string, firstSequence: number, previousChain: string, gen = generation()) {
  const items: Message[] = page.source === "snapshot"
    ? page.projections.map((bytes) => ({ kind: "projection", data: base64(bytes) }))
    : page.items.map((item) => item.kind === "retraction"
      ? { kind: "retraction", source_identity: item.sourceIdentity }
      : { kind: "projection", data: base64(item.bytes) });
  return partitionPage({
    sender_installation: INSTALLATION, capability_generation: gen.generation, record_type: page.recordType,
    projection_schema_version: page.projectionSchemaVersion, cycle_kind: cycleKind as "initial-snapshot", cycle_id: page.cycleId,
    source_page_index: page.pageIndex, first_sequence: firstSequence, previous_chain: previousChain, items,
    max_record_count: gen.maxRecordCount, max_message_bytes: gen.maxMessageBytes,
  });
}

const refused = (failure: string) => ({ outcome: "refused", failure });

// R2 — Fragments follow the protocol partition

test("fragments_match_the_protocol_partition", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  await begin(f.database, "weight", uuid(0xb1));
  // Split by record count, then by bytes.
  const byCount = generation({ maxRecordCount: 2 });
  const countPage = snapshotPage(uuid(0xa1), { projections: [1, 2, 3, 4, 5].map((n) => projection(n)) });
  const first = staged(await stage(f, countPage, { context: context({ generation: byCount }) }));
  const bySize = generation({ maxMessageBytes: 4096 });
  const sizePage = snapshotPage(uuid(0xb1), { recordType: "weight", projections: [1, 2, 3].map((n) => projection(n, 1800)) });
  const second = staged(await stage(f, sizePage, { context: context({ generation: bySize }) }));
  const countExpected = expectedPartition(countPage, "initial-snapshot", 1, chainStart(uuid(0xa1)), byCount);
  const sizeExpected = expectedPartition(sizePage, "initial-snapshot", 4, chainStart(uuid(0xb1)), bySize);
  assert.equal(countExpected.fragments.length, 3);
  assert.equal(sizeExpected.fragments.length, 3);
  const rows = await opened(f.database, f.cipher);
  assert.deepEqual(rows.map((row) => row.bytes), [...countExpected.fragments, ...sizeExpected.fragments]);
  assert.deepEqual([first.firstSequence, first.lastSequence, second.firstSequence, second.lastSequence], [1, 3, 4, 6]);
  assert.equal(first.progress.chain, countExpected.next_chain);
  assert.equal(second.progress.chain, sizeExpected.next_chain);
});

test("items_keep_their_page_order", async (t) => {
  const f = await fixture(t);
  const attempt = await begin(f.database, "steps", uuid(0xa1));
  staged(await stage(f, snapshotPage(uuid(0xa1), { exhausted: true })));
  const items: ChangeItem[] = [
    { kind: "retraction", sourceIdentity: "synthetic-source-9" },
    { kind: "retraction", sourceIdentity: "synthetic-source-3" },
    { kind: "projection", bytes: projection(7) },
    { kind: "projection", bytes: projection(5) },
    { kind: "projection", bytes: projection(6) },
  ];
  staged(await stage(f, changePage(uuid(0xc1), attempt.changesToken, { items }), { context: context({ generation: generation({ maxRecordCount: 2 }) }) }));
  const rows = (await opened(f.database, f.cipher)).filter((row) => row.message.cycle_id === uuid(0xc1));
  assert.equal(rows.length, 3);
  assert.deepEqual(rows.flatMap((row) => row.message.items as Message[]), [
    { kind: "retraction", source_identity: "synthetic-source-9" },
    { kind: "retraction", source_identity: "synthetic-source-3" },
    { kind: "projection", data: base64(projection(7)) },
    { kind: "projection", data: base64(projection(5)) },
    { kind: "projection", data: base64(projection(6)) },
  ]);
});

test("an_empty_page_stages_one_empty_fragment", async (t) => {
  const f = await fixture(t);
  const attempt = await begin(f.database, "steps", uuid(0xa1));
  const snapshot = staged(await stage(f, snapshotPage(uuid(0xa1), { projections: [], exhausted: true })));
  assert.deepEqual([snapshot.firstSequence, snapshot.lastSequence], [1, 1]);
  assert.deepEqual([snapshot.progress.fragmentCount, snapshot.progress.recordCount, snapshot.progress.finished], [1, 0, true]);
  const change = staged(await stage(f, changePage(uuid(0xc1), attempt.changesToken, { items: [] })));
  assert.deepEqual([change.firstSequence, change.lastSequence, change.progress.recordCount], [2, 2, 0]);
  const rows = await opened(f.database, f.cipher);
  assert.deepEqual(rows.map((row) => row.message.items), [[], []]);
});

test("sequences_continue_across_cycles_and_types", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  await begin(f.database, "weight", uuid(0xb1));
  await begin(f.database, "heart-rate", uuid(0xd1), { installationId: OTHER_INSTALLATION });
  const oneEach = context({ generation: generation({ maxRecordCount: 1 }) });
  const steps1 = staged(await stage(f, snapshotPage(uuid(0xa1)), { context: oneEach }));
  const weight1 = staged(await stage(f, snapshotPage(uuid(0xb1), { recordType: "weight", projections: [projection(3)] })));
  const steps2 = staged(await stage(f, snapshotPage(uuid(0xa1), { pageIndex: 2, readFrom: position("1"), nextPosition: position("2") })));
  const other = staged(await stage(f, snapshotPage(uuid(0xd1), { recordType: "heart-rate" }), { context: context({ installationId: OTHER_INSTALLATION }) }));
  assert.deepEqual(
    [steps1, weight1, steps2, other].map((result) => [result.firstSequence, result.lastSequence]),
    [[1, 2], [3, 3], [4, 4], [1, 1]],
  );
  assert.equal(await lastOutboxSequence(f.database, INSTALLATION), 4);
  assert.equal(await lastOutboxSequence(f.database, OTHER_INSTALLATION), 1);
  assert.equal(await lastOutboxSequence(f.database, uuid(0xee)), 0);
  assert.deepEqual((await opened(f.database, f.cipher)).map((row) => row.message.sequence), [1, 2, 3, 4]);
  assert.deepEqual((await opened(f.database, f.cipher, OTHER_INSTALLATION)).map((row) => row.message.sender_installation), [OTHER_INSTALLATION]);
});

test("removed_sequences_are_never_reused", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  staged(await stage(f, snapshotPage(uuid(0xa1))));
  // send-outbox-message removes rows after their receipts; the high-water mark stays.
  await f.database.execute("DELETE FROM outbox_messages");
  const next = staged(await stage(f, snapshotPage(uuid(0xa1), { pageIndex: 2, readFrom: position("1"), nextPosition: position("2") })));
  assert.deepEqual([next.firstSequence, next.lastSequence], [2, 2]);
  assert.equal(await lastOutboxSequence(f.database, INSTALLATION), 2);
});

// R3 — Each fragment is sealed with its own authenticated data

test("each_fragment_is_sealed_with_its_own_aad", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  const spy = new SpyCipher(f.cipher);
  staged(await stage(f, snapshotPage(uuid(0xa1), { projections: [1, 2, 3].map((n) => projection(n)) }), {
    cipher: spy, context: context({ generation: generation({ maxRecordCount: 1 }) }),
  }));
  const rows = await opened(f.database, f.cipher);
  assert.equal(spy.seals.length, 3);
  spy.seals.forEach((call, index) => {
    assert.equal(call.alias, KEY_ALIAS);
    assert.deepEqual(call.plaintext, rows[index].bytes);
    assert.deepEqual(call.aad, outboxAad(INSTALLATION, index + 1));
  });
  assert.deepEqual(outboxAad(INSTALLATION, 1), utf8(`activity-tracker-outbox-v1:${INSTALLATION}:1`));
  assert.deepEqual(outboxAad(OTHER_INSTALLATION, 9_007_199_254_740_991), utf8(`activity-tracker-outbox-v1:${OTHER_INSTALLATION}:9007199254740991`));
});

test("sealed_rows_fail_under_another_sequence", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  staged(await stage(f, snapshotPage(uuid(0xa1)), { context: context({ generation: generation({ maxRecordCount: 1 }) }) }));
  const [first, second] = await outboxMessages(f.database);
  assert.equal((await f.cipher.open(KEY_ALIAS, first.sealed, outboxAad(INSTALLATION, 1))).status, "ok");
  assert.deepEqual(await f.cipher.open(KEY_ALIAS, first.sealed, outboxAad(INSTALLATION, 2)), { status: "error", reason: "authentication-failed" });
  assert.deepEqual(await f.cipher.open(KEY_ALIAS, first.sealed, outboxAad(OTHER_INSTALLATION, 1)), { status: "error", reason: "authentication-failed" });
  assert.deepEqual(await f.cipher.open(KEY_ALIAS, second.sealed, outboxAad(INSTALLATION, 1)), { status: "error", reason: "authentication-failed" });
});

test("staging_never_creates_deletes_or_opens_keys", async (t) => {
  const f = await fixture(t);
  const attempt = await begin(f.database, "steps", uuid(0xa1));
  const guard = guarded(f.cipher);
  const results = [
    await stageOutboxBatch(f.database, guard.cipher, context(), snapshotPage(uuid(0xa1), { exhausted: true })),
    await stageOutboxBatch(f.database, guard.cipher, context(), changePage(uuid(0xc1), attempt.changesToken)),
    await stageOutboxBatch(f.database, guard.cipher, context({ keyAlias: "synthetic-missing-key" }), changePage(uuid(0xc2), token("c1-1"))),
    await stageOutboxBatch(failing(f.database, { commit: true }), guard.cipher, context(), changePage(uuid(0xc2), token("c1-1"))),
  ];
  assert.deepEqual(results.map((result) => result.outcome), ["staged", "staged", "cipher-failed", "storage-failed"]);
  assert.deepEqual(guard.forbidden, []);
  assert.equal(await f.cipher.keyStatus(KEY_ALIAS), "present");
});

test("the_database_holds_no_plaintext_items", async (t) => {
  const f = await fixture(t);
  const attempt = await begin(f.database, "steps", uuid(0xa1));
  const projections = [1, 2, 3].map((n) => utf8(`{"synthetic":${n},"marker":"${SENTINEL}-projection-${n}"}`));
  staged(await stage(f, snapshotPage(uuid(0xa1), { projections, exhausted: true })));
  staged(await stage(f, changePage(uuid(0xc1), attempt.changesToken, {
    items: [{ kind: "retraction", sourceIdentity: `synthetic-${SENTINEL}-source` }, { kind: "projection", bytes: projections[0] }],
  })));
  const fragments = (await opened(f.database, f.cipher)).map((row) => row.bytes);
  // The DELETE journal leaves every committed page in the database file itself.
  const file = Buffer.from(readFileSync(f.path));
  const forbidden = [
    SENTINEL, '"items":[', ...projections.map(base64), ...fragments.map((bytes) => messageSha256(bytes)),
  ];
  for (const text of forbidden) assert.equal(file.indexOf(text), -1, `the database file contains ${text}`);
});

// R4 — One atomic commit

test("staging_commits_messages_and_progress_together", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  const gen = generation({ maxRecordCount: 2 });
  const page = snapshotPage(uuid(0xa1), { projections: [1, 2, 3].map((n) => projection(n)) });
  const result = staged(await stage(f, page, { context: context({ generation: gen }) }));
  const expected = expectedPartition(page, "initial-snapshot", 1, chainStart(uuid(0xa1)), gen);
  const progress: CycleProgress = {
    cycleId: uuid(0xa1), recordType: "steps", cycleKind: "initial-snapshot", snapshotId: uuid(0xa1), installationId: INSTALLATION,
    pagesStaged: 1, fragmentCount: 2, recordCount: 3, chain: expected.next_chain,
    position: position("1"), changesToken: null, unreadSinceMs: null, finished: false,
  };
  assert.deepEqual(result, { outcome: "staged", progress, firstSequence: 1, lastSequence: 2 });
  assert.deepEqual(await cycleProgress(f.database, uuid(0xa1)), progress);
  const rows = await outboxMessages(f.database);
  assert.deepEqual(rows.map(({ sealed, ...row }) => row), [
    { installationId: INSTALLATION, sequence: 1, kind: "ingest-batch", messageBytes: expected.fragments[0].length },
    { installationId: INSTALLATION, sequence: 2, kind: "ingest-batch", messageBytes: expected.fragments[1].length },
  ]);
  assert.ok(rows.every((row) => row.sealed instanceof Uint8Array && row.sealed.length > 0));
  assert.equal(await lastOutboxSequence(f.database, INSTALLATION), 2);
  assert.equal(await cycleProgress(f.database, uuid(0xdead)), null);
});

test("progress_survives_reopening", async (t) => {
  const f = await fixture(t);
  const attempt = await begin(f.database, "steps", uuid(0xa1));
  staged(await stage(f, snapshotPage(uuid(0xa1), { exhausted: true })));
  const change = staged(await stage(f, changePage(uuid(0xc1), attempt.changesToken, { hasMore: true })));
  const before = { outbox: await outboxMessages(f.database), snapshot: await cycleProgress(f.database, uuid(0xa1)) };
  const reopened = await f.reopen();
  assert.deepEqual(await migrate(reopened, MIGRATIONS), { status: "ready", version: 2 });
  assert.deepEqual(await outboxMessages(reopened), before.outbox);
  assert.deepEqual(await cycleProgress(reopened, uuid(0xa1)), before.snapshot);
  assert.deepEqual(await cycleProgress(reopened, uuid(0xc1)), change.progress);
  assert.equal(await lastOutboxSequence(reopened, INSTALLATION), change.lastSequence);
  const next = staged(await stage({ database: reopened, cipher: f.cipher }, changePage(uuid(0xc1), change.progress.changesToken!, { pageIndex: 2 })));
  assert.equal(next.firstSequence, change.lastSequence + 1);
});

test("counts_and_chain_accumulate_across_pages", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  const ctx = context({ generation: generation({ maxRecordCount: 2 }) });
  staged(await stage(f, snapshotPage(uuid(0xa1)), { context: ctx }));
  staged(await stage(f, snapshotPage(uuid(0xa1), { pageIndex: 2, readFrom: position("1"), projections: [], nextPosition: position("2") }), { context: ctx }));
  const last = staged(await stage(f, snapshotPage(uuid(0xa1), {
    pageIndex: 3, readFrom: position("2"), projections: [3, 4, 5].map((n) => projection(n)), nextPosition: position("3"), exhausted: true,
  }), { context: ctx }));
  const rows = await opened(f.database, f.cipher);
  assert.deepEqual(rows.map((row) => [row.message.source_page_index, row.message.fragment_index]), [[1, 1], [2, 1], [3, 1], [3, 2]]);
  let chain = chainStart(uuid(0xa1));
  for (const row of rows) {
    assert.equal(row.message.previous_chain, chain);
    chain = chainNext(chain, messageSha256(row.bytes));
  }
  assert.deepEqual(last.progress, {
    cycleId: uuid(0xa1), recordType: "steps", cycleKind: "initial-snapshot", snapshotId: uuid(0xa1), installationId: INSTALLATION,
    pagesStaged: 3, fragmentCount: 4, recordCount: 5, chain, position: position("3"), changesToken: null, unreadSinceMs: null, finished: true,
  });
});

// R5 — A snapshot page continues its snapshot

test("a_snapshot_page_must_belong_to_the_current_attempt", async (t) => {
  const f = await fixture(t);
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1))), refused("cycle-not-current"));
  await begin(f.database, "steps", uuid(0xa1));
  await begin(f.database, "weight", uuid(0xb1));
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa2))), refused("cycle-not-current"));
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xb1))), refused("cycle-not-current"));
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1), { recordType: "weight" })), refused("cycle-not-current"));
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1), { recordType: "heart-rate" })), refused("cycle-not-current"));
  assert.deepEqual(await outboxMessages(f.database), []);
});

test("an_abandoned_attempt_stages_nothing", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  staged(await stage(f, snapshotPage(uuid(0xa1))));
  await begin(f.database, "steps", uuid(0xa2), { abandoning: uuid(0xa1) });
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1), { pageIndex: 2, readFrom: position("1") })), refused("cycle-not-current"));
  const replacement = staged(await stage(f, snapshotPage(uuid(0xa2))));
  assert.equal(replacement.firstSequence, 2);
  assert.equal(replacement.progress.snapshotId, uuid(0xa2));
  const rows = await opened(f.database, f.cipher);
  assert.equal(rows.at(-1)!.message.previous_chain, chainStart(uuid(0xa2)));
  // The abandoned attempt's progress is retained unchanged.
  assert.equal((await cycleProgress(f.database, uuid(0xa1)))?.pagesStaged, 1);
});

test("the_attempt_kind_sets_the_cycle_kind", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  await reconciliationAttempt(f.database, "weight", uuid(0xb1));
  const initial = staged(await stage(f, snapshotPage(uuid(0xa1))));
  const reconciliation = staged(await stage(f, snapshotPage(uuid(0xb1), { recordType: "weight", exhausted: true })));
  assert.equal(initial.progress.cycleKind, "initial-snapshot");
  assert.equal(reconciliation.progress.cycleKind, "reconciliation-snapshot");
  const change = staged(await stage(f, changePage(uuid(0xc1), token("b1"), { recordType: "weight" })));
  assert.equal(change.progress.cycleKind, "change-cycle");
  const kinds = (await opened(f.database, f.cipher)).map((row) => [row.message.cycle_id, row.message.cycle_kind]);
  assert.deepEqual(kinds, [[uuid(0xa1), "initial-snapshot"], [uuid(0xb1), "reconciliation-snapshot"], [uuid(0xc1), "change-cycle"]]);
});

test("another_installation_is_refused", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1), { installationId: OTHER_INSTALLATION });
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1))), refused("installation-mismatch"));
  const other = context({ installationId: OTHER_INSTALLATION });
  staged(await stage(f, snapshotPage(uuid(0xa1), { exhausted: true }), { context: other }));
  assert.deepEqual(await stage(f, changePage(uuid(0xc1), token("a1"))), refused("installation-mismatch"));
  const change = staged(await stage(f, changePage(uuid(0xc1), token("a1"), { hasMore: true }), { context: other }));
  assert.deepEqual(await stage(f, changePage(uuid(0xc1), change.progress.changesToken!, { pageIndex: 2 })), refused("installation-mismatch"));
  assert.equal(await lastOutboxSequence(f.database, INSTALLATION), 0);
});

test("pages_must_continue_from_the_saved_position", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  const mismatch = refused("page-position-mismatch");
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1), { pageIndex: 2 })), mismatch);
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1), { readFrom: position("0") })), mismatch);
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1), { readFrom: new Uint8Array() })), mismatch);
  staged(await stage(f, snapshotPage(uuid(0xa1))));
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1))), mismatch);
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1), { pageIndex: 2, readFrom: position("x") })), mismatch);
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1), { pageIndex: 2, readFrom: utf8("synthetic-position-1 ") })), mismatch);
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1), { pageIndex: 2, readFrom: null })), mismatch);
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1), { pageIndex: 3, readFrom: position("1") })), mismatch);
  const second = staged(await stage(f, snapshotPage(uuid(0xa1), { pageIndex: 2, readFrom: position("1"), nextPosition: position("2") })));
  assert.deepEqual([second.progress.pagesStaged, second.progress.position], [2, position("2")]);
});

test("an_exhausted_snapshot_takes_no_more_pages", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  const last = staged(await stage(f, snapshotPage(uuid(0xa1), { exhausted: true })));
  assert.equal(last.progress.finished, true);
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1), { pageIndex: 2, readFrom: position("1") })), refused("cycle-finished"));
});

// R6 — A Change Cycle continues the change stream

test("the_first_change_cycle_starts_from_the_pre_read_token", async (t) => {
  const f = await fixture(t);
  const attempt = await begin(f.database, "steps", uuid(0xa1), { changesToken: token("pre-read"), unreadSinceMs: UNREAD_SINCE });
  assert.deepEqual(await changeOrigin(f.database, "steps"), { snapshotId: uuid(0xa1), changesToken: token("pre-read"), unreadSinceMs: UNREAD_SINCE });
  assert.equal(await changeOrigin(f.database, "weight"), null);
  staged(await stage(f, snapshotPage(uuid(0xa1), { exhausted: true })));
  const mismatch = refused("page-position-mismatch");
  assert.deepEqual(await stage(f, changePage(uuid(0xc1), token("other"))), mismatch);
  assert.deepEqual(await stage(f, changePage(uuid(0xc1), attempt.changesToken, { pageIndex: 2 })), mismatch);
  const page = changePage(uuid(0xc1), attempt.changesToken, { nextChangesToken: token("c1-1"), nextUnreadSinceMs: UNREAD_SINCE + 7, hasMore: true });
  const result = staged(await stage(f, page));
  const expected = expectedPartition(page, "change-cycle", 2, chainStart(uuid(0xc1)));
  assert.deepEqual(result.progress, {
    cycleId: uuid(0xc1), recordType: "steps", cycleKind: "change-cycle", snapshotId: uuid(0xa1), installationId: INSTALLATION,
    pagesStaged: 1, fragmentCount: 1, recordCount: 2, chain: expected.next_chain,
    position: null, changesToken: token("c1-1"), unreadSinceMs: UNREAD_SINCE + 7, finished: false,
  });
  assert.deepEqual((await opened(f.database, f.cipher)).at(-1)!.bytes, expected.fragments[0]);
  // An unfinished cycle does not move the origin.
  assert.deepEqual(await changeOrigin(f.database, "steps"), { snapshotId: uuid(0xa1), changesToken: token("pre-read"), unreadSinceMs: UNREAD_SINCE });
});

test("later_change_cycles_continue_from_the_final_token", async (t) => {
  const f = await fixture(t);
  const attempt = await begin(f.database, "steps", uuid(0xa1));
  staged(await stage(f, snapshotPage(uuid(0xa1), { exhausted: true })));
  // Cycle identities are deliberately out of order, so only lineage orders the cycles.
  staged(await stage(f, changePage(uuid(0xc9), attempt.changesToken, { nextChangesToken: token("c9-1"), hasMore: true })));
  staged(await stage(f, changePage(uuid(0xc9), token("c9-1"), { pageIndex: 2, nextChangesToken: token("c9-2"), nextUnreadSinceMs: UNREAD_SINCE + 2 })));
  assert.deepEqual(await changeOrigin(f.database, "steps"), { snapshotId: uuid(0xa1), changesToken: token("c9-2"), unreadSinceMs: UNREAD_SINCE + 2 });
  assert.deepEqual(await stage(f, changePage(uuid(0xc2), attempt.changesToken)), refused("page-position-mismatch"));
  assert.deepEqual(await stage(f, changePage(uuid(0xc2), token("c9-1"))), refused("page-position-mismatch"));
  const second = staged(await stage(f, changePage(uuid(0xc2), token("c9-2"), { nextChangesToken: token("c2-1"), nextUnreadSinceMs: UNREAD_SINCE + 3, hasMore: true })));
  assert.equal(second.progress.chain, expectedPartition(changePage(uuid(0xc2), token("c9-2")), "change-cycle", 4, chainStart(uuid(0xc2))).next_chain);
  // While the second cycle is unfinished, the origin stays at the first cycle's final token.
  assert.deepEqual(await changeOrigin(f.database, "steps"), { snapshotId: uuid(0xa1), changesToken: token("c9-2"), unreadSinceMs: UNREAD_SINCE + 2 });
  staged(await stage(f, changePage(uuid(0xc2), token("c2-1"), { pageIndex: 2, nextChangesToken: token("c2-2"), nextUnreadSinceMs: UNREAD_SINCE + 4 })));
  assert.deepEqual(await changeOrigin(f.database, "steps"), { snapshotId: uuid(0xa1), changesToken: token("c2-2"), unreadSinceMs: UNREAD_SINCE + 4 });
  staged(await stage(f, changePage(uuid(0xc5), token("c2-2"))));
  assert.deepEqual(await f.database.query("SELECT cycle_id, lineage FROM cycle_progress ORDER BY lineage"), [
    { cycle_id: uuid(0xa1), lineage: 0 }, { cycle_id: uuid(0xc9), lineage: 1 }, { cycle_id: uuid(0xc2), lineage: 2 }, { cycle_id: uuid(0xc5), lineage: 3 },
  ]);
});

test("a_change_cycle_waits_for_its_snapshot", async (t) => {
  const f = await fixture(t);
  const attempt = await begin(f.database, "steps", uuid(0xa1));
  assert.deepEqual(await stage(f, changePage(uuid(0xc1), attempt.changesToken)), refused("snapshot-unfinished"));
  staged(await stage(f, snapshotPage(uuid(0xa1))));
  assert.deepEqual(await stage(f, changePage(uuid(0xc1), attempt.changesToken)), refused("snapshot-unfinished"));
  staged(await stage(f, snapshotPage(uuid(0xa1), { pageIndex: 2, readFrom: position("1"), exhausted: true })));
  staged(await stage(f, changePage(uuid(0xc1), attempt.changesToken)));
});

test("one_change_cycle_is_open_at_a_time", async (t) => {
  const f = await fixture(t);
  const attempt = await begin(f.database, "steps", uuid(0xa1));
  staged(await stage(f, snapshotPage(uuid(0xa1), { exhausted: true })));
  staged(await stage(f, changePage(uuid(0xc1), attempt.changesToken, { nextChangesToken: token("c1-1"), hasMore: true })));
  assert.deepEqual(await stage(f, changePage(uuid(0xc2), token("c1-1"))), refused("change-cycle-open"));
  assert.deepEqual(await stage(f, changePage(uuid(0xc2), attempt.changesToken)), refused("change-cycle-open"));
  staged(await stage(f, changePage(uuid(0xc1), token("c1-1"), { pageIndex: 2, nextChangesToken: token("c1-2") })));
  staged(await stage(f, changePage(uuid(0xc2), token("c1-2"))));
});

test("a_new_attempt_supersedes_older_cycles", async (t) => {
  const f = await fixture(t);
  const first = await begin(f.database, "steps", uuid(0xa1));
  staged(await stage(f, snapshotPage(uuid(0xa1), { exhausted: true })));
  staged(await stage(f, changePage(uuid(0xc1), first.changesToken, { nextChangesToken: token("c1-1"), hasMore: true })));
  const second = await begin(f.database, "steps", uuid(0xa2), { abandoning: uuid(0xa1), changesToken: token("a2") });
  assert.deepEqual(await changeOrigin(f.database, "steps"), { snapshotId: uuid(0xa2), changesToken: token("a2"), unreadSinceMs: UNREAD_SINCE });
  assert.deepEqual(await stage(f, changePage(uuid(0xc1), token("c1-1"), { pageIndex: 2 })), refused("cycle-not-current"));
  assert.deepEqual(await stage(f, changePage(uuid(0xc3), second.changesToken)), refused("snapshot-unfinished"));
  staged(await stage(f, snapshotPage(uuid(0xa2), { exhausted: true })));
  assert.deepEqual(await stage(f, changePage(uuid(0xc3), token("c1-1"))), refused("page-position-mismatch"));
  const replacement = staged(await stage(f, changePage(uuid(0xc3), second.changesToken)));
  assert.equal(replacement.progress.snapshotId, uuid(0xa2));
});

test("change_pages_must_continue_from_the_saved_token", async (t) => {
  const f = await fixture(t);
  const attempt = await begin(f.database, "steps", uuid(0xa1));
  await begin(f.database, "weight", uuid(0xb1));
  staged(await stage(f, snapshotPage(uuid(0xa1), { exhausted: true })));
  staged(await stage(f, changePage(uuid(0xc1), attempt.changesToken, { nextChangesToken: token("c1-1"), hasMore: true })));
  const mismatch = refused("page-position-mismatch");
  assert.deepEqual(await stage(f, changePage(uuid(0xc1), token("wrong"), { pageIndex: 2 })), mismatch);
  assert.deepEqual(await stage(f, changePage(uuid(0xc1), token("c1-1"), { pageIndex: 3 })), mismatch);
  assert.deepEqual(await stage(f, changePage(uuid(0xc1), attempt.changesToken)), mismatch);
  // A page must name a current cycle of its own kind and record type. A snapshot attempt's
  // identity, staged or not, current or abandoned, never names a Change Cycle.
  assert.deepEqual(await stage(f, changePage(uuid(0xa1), token("c1-1"), { pageIndex: 2 })), refused("cycle-not-current"));
  assert.deepEqual(await stage(f, changePage(uuid(0xa1), attempt.changesToken)), refused("cycle-not-current"));
  assert.deepEqual(await stage(f, changePage(uuid(0xb1), token("b1"), { recordType: "weight" })), refused("cycle-not-current"));
  assert.deepEqual(await stage(f, changePage(uuid(0xb1), attempt.changesToken)), refused("cycle-not-current"));
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xc1), { pageIndex: 2, readFrom: position("1") })), refused("cycle-not-current"));
  assert.deepEqual(await stage(f, changePage(uuid(0xc1), token("c1-1"), { pageIndex: 2, recordType: "weight" })), refused("cycle-not-current"));
  staged(await stage(f, changePage(uuid(0xc1), token("c1-1"), { pageIndex: 2, nextChangesToken: token("c1-2") })));
  assert.deepEqual(await stage(f, changePage(uuid(0xc1), token("c1-2"), { pageIndex: 3 })), refused("cycle-finished"));
});

// R7 — The capability generation

test("unsupported_projections_are_refused", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "weight", uuid(0xb1));
  await begin(f.database, "constructor", uuid(0xe1));
  const unsupported = refused("projection-schema-unsupported");
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1), { projectionSchemaVersion: 2 })), unsupported);
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1), { recordType: "sleep-session" })), unsupported);
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xe1), { recordType: "constructor" })), unsupported);
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xb1), { recordType: "weight" }), { context: context({ generation: generation({ projectionSchemas: {} }) }) }), unsupported);
  staged(await stage(f, snapshotPage(uuid(0xb1), { recordType: "weight", projectionSchemaVersion: 2 })));
});

test("fragments_carry_the_staging_generation", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  const gen = generation({ generation: 7, maxRecordCount: 2 });
  staged(await stage(f, snapshotPage(uuid(0xa1), { projections: [1, 2, 3, 4, 5].map((n) => projection(n)) }), { context: context({ generation: gen }) }));
  const rows = await opened(f.database, f.cipher);
  assert.deepEqual(rows.map((row) => row.message.capability_generation), [7, 7, 7]);
  assert.deepEqual(rows.map((row) => (row.message.items as Message[]).length), [2, 2, 1]);
});

// R8 — An indivisible item blocks the page

test("an_oversized_item_blocks_the_whole_page", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  const before = await contents(f.database);
  const spy = new SpyCipher(f.cipher);
  const page = snapshotPage(uuid(0xa1), { projections: [projection(1), projection(2, 5000), projection(3)] });
  assert.deepEqual(await stage(f, page, { cipher: spy, context: context({ generation: generation({ maxMessageBytes: 4096 }) }) }),
    { outcome: "blocked", failure: "indivisible-record-limit-exceeded" });
  assert.equal(spy.seals.length, 0);
  assert.deepEqual(await contents(f.database), before);
});

test("a_larger_generation_stages_the_blocked_page", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  const page = snapshotPage(uuid(0xa1), { projections: [projection(1), projection(2, 5000), projection(3)] });
  assert.equal((await stage(f, page, { context: context({ generation: generation({ maxMessageBytes: 4096 }) }) })).outcome, "blocked");
  const result = staged(await stage(f, page, { context: context({ generation: generation({ generation: 4, maxMessageBytes: 8192 }) }) }));
  assert.equal(result.progress.recordCount, 3);
});

// R9 — Nothing staged on failure

test("refusals_and_blocks_write_nothing", async (t) => {
  const f = await fixture(t);
  const attempt = await begin(f.database, "steps", uuid(0xa1));
  await begin(f.database, "weight", uuid(0xb1), { installationId: OTHER_INSTALLATION });
  staged(await stage(f, snapshotPage(uuid(0xa1), { exhausted: true })));
  staged(await stage(f, changePage(uuid(0xc1), attempt.changesToken, { nextChangesToken: token("c1-1"), hasMore: true })));
  const before = await contents(f.database);
  const spy = new SpyCipher(f.cipher);
  const cases: [ObservedPage, StagingContext?][] = [
    [snapshotPage(uuid(0xa9))],
    [snapshotPage(uuid(0xb1), { recordType: "weight" })],
    [snapshotPage(uuid(0xa1), { pageIndex: 2, readFrom: position("1") })],
    [changePage(uuid(0xc2), token("c1-1"))],
    [changePage(uuid(0xc1), token("wrong"), { pageIndex: 2 })],
    [snapshotPage(uuid(0xa1), { projectionSchemaVersion: 9 })],
    [changePage(uuid(0xc1), token("c1-1"), { pageIndex: 2, items: [{ kind: "projection", bytes: projection(1, 5000) }] }), context({ generation: generation({ maxMessageBytes: 4096 }) })],
  ];
  for (const [page, ctx] of cases) {
    const result = await stage(f, page, { cipher: spy, ...(ctx ? { context: ctx } : {}) });
    assert.ok(result.outcome === "refused" || result.outcome === "blocked", JSON.stringify(result));
  }
  assert.equal(spy.seals.length, 0);
  assert.deepEqual(await contents(f.database), before);
});

test("cipher_failures_write_nothing", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  const before = await contents(f.database);
  const page = snapshotPage(uuid(0xa1));
  const failed = (failure: string) => ({ outcome: "cipher-failed", failure });
  assert.deepEqual(await stage(f, page, { context: context({ keyAlias: "synthetic-missing-key" }) }), failed("installation-key-missing"));
  const invalidated = new NodeInstallationCipher();
  await invalidated.create(KEY_ALIAS);
  invalidated.invalidate(KEY_ALIAS);
  assert.deepEqual(await stage(f, page, { cipher: invalidated }), failed("installation-key-unusable"));
  const unavailable = new NodeInstallationCipher();
  await unavailable.create(KEY_ALIAS);
  unavailable.setUnavailable(true);
  assert.deepEqual(await stage(f, page, { cipher: unavailable }), failed("keystore-unavailable"));
  const unexpected: { returns?: unknown; throws?: unknown }[] = [
    { throws: new Error(`${SENTINEL} bridge failure`) },
    { throws: `${SENTINEL} not an error` },
    { throws: null },
    { returns: { status: "ok", value: `${SENTINEL} not bytes` } },
    { returns: { status: "ok", value: new Uint8Array() } },
    { returns: { status: "error", reason: "authentication-failed" } },
    { returns: { status: "error", reason: "unsupported-format" } },
    { returns: { status: "error", reason: `${SENTINEL}-reason` } },
    { returns: { status: "error", reason: "toString" } },
    { returns: null },
    { returns: undefined },
  ];
  for (const entry of unexpected) {
    assert.deepEqual(await stage(f, page, { cipher: new SpyCipher(f.cipher, { 1: entry }) }), failed("unexpected-cipher-failure"), JSON.stringify(entry));
  }
  assert.deepEqual(await contents(f.database), before);
});

test("a_mid_page_cipher_failure_stores_no_fragment", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  const before = await contents(f.database);
  const ctx = context({ generation: generation({ maxRecordCount: 1 }) });
  const page = snapshotPage(uuid(0xa1), { projections: [1, 2, 3, 4].map((n) => projection(n)) });
  const spy = new SpyCipher(f.cipher, { 3: { returns: { status: "error", reason: "keystore-unavailable" } } });
  assert.deepEqual(await stage(f, page, { cipher: spy, context: ctx }), { outcome: "cipher-failed", failure: "keystore-unavailable" });
  assert.equal(spy.seals.length, 3);
  assert.deepEqual(await contents(f.database), before);
  const retried = staged(await stage(f, page, { context: ctx }));
  assert.deepEqual([retried.firstSequence, retried.lastSequence], [1, 4]);
});

test("storage_errors_roll_back_everything", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  const ctx = context({ generation: generation({ maxRecordCount: 1 }) });
  staged(await stage(f, snapshotPage(uuid(0xa1)), { context: ctx }));
  const before = await contents(f.database);
  // Fail each mutating statement in turn until the staging needs no more; nothing may persist.
  const page = snapshotPage(uuid(0xa1), { pageIndex: 2, readFrom: position("1"), projections: [3, 4, 5].map((n) => projection(n)), nextPosition: position("2") });
  let failurePoints = 0;
  for (let mutation = 1; ; mutation++) {
    const result = await stage(f, page, { database: failing(f.database, { mutation }), context: ctx });
    if (result.outcome === "staged") break;
    assert.deepEqual(result, { outcome: "storage-failed", failure: "storage-failed" });
    assert.deepEqual(await contents(f.database), before, `failing mutation ${mutation}`);
    failurePoints++;
    assert.ok(mutation < 30, "the staging never completed");
  }
  assert.ok(failurePoints >= 3, "the Outbox, the high-water mark, and the progress each take a statement");
  // A failing read before sealing is a storage failure too, and seals nothing.
  const settled = await contents(f.database);
  const next = snapshotPage(uuid(0xa1), { pageIndex: 3, readFrom: position("2"), nextPosition: position("3") });
  const spy = new SpyCipher(f.cipher);
  assert.deepEqual(await stage(f, next, { database: failing(f.database, { queries: true }), cipher: spy }), { outcome: "storage-failed", failure: "storage-failed" });
  assert.equal(spy.seals.length, 0);
  // So are a failing read inside the commit transaction, such as its recheck, and a failing commit.
  for (const rule of [{ transactionQueries: true }, { commit: true }]) {
    assert.deepEqual(await stage(f, next, { database: failing(f.database, rule) }), { outcome: "storage-failed", failure: "storage-failed" }, JSON.stringify(rule));
    assert.deepEqual(await contents(f.database), settled, JSON.stringify(rule));
  }
});

// R10 — Staging rechecks its state inside the commit

test("interleaved_stagings_conflict_without_writing", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  await begin(f.database, "weight", uuid(0xb1));
  const conflict = { outcome: "conflict", failure: "staging-conflict" };
  // Another record type takes the next sequences while this page is being sealed.
  const otherType = new SpyCipher(f.cipher, { 1: { before: async () => {
    staged(await stage(f, snapshotPage(uuid(0xb1), { recordType: "weight" })));
  } } });
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1)), { cipher: otherType }), conflict);
  assert.equal(await cycleProgress(f.database, uuid(0xa1)), null);
  assert.deepEqual((await outboxMessages(f.database)).map((row) => row.sequence), [1]);
  // The same cycle advances while this page is being sealed.
  const sameCycle = new SpyCipher(f.cipher, { 1: { before: async () => {
    staged(await stage(f, snapshotPage(uuid(0xb1), { recordType: "weight", pageIndex: 2, readFrom: position("1"), nextPosition: position("2") })));
  } } });
  const settled = await contents(f.database);
  const page2 = snapshotPage(uuid(0xb1), { recordType: "weight", pageIndex: 2, readFrom: position("1"), nextPosition: position("2") });
  assert.deepEqual(await stage(f, page2, { cipher: sameCycle }), conflict);
  assert.equal((await cycleProgress(f.database, uuid(0xb1)))?.pagesStaged, 2);
  assert.equal((await outboxMessages(f.database)).length, settled.outbox.length + 1);
  // The attempt is abandoned while this page is being sealed.
  const abandoned = new SpyCipher(f.cipher, { 1: { before: async () => {
    await begin(f.database, "steps", uuid(0xa2), { abandoning: uuid(0xa1) });
  } } });
  const beforeAbandon = await contents(f.database);
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1)), { cipher: abandoned }), conflict);
  const after = await contents(f.database);
  assert.deepEqual([after.outbox, after.highWater, after.progress], [beforeAbandon.outbox, beforeAbandon.highWater, beforeAbandon.progress]);
});

test("a_retry_after_conflict_stages_from_the_new_state", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  await begin(f.database, "weight", uuid(0xb1));
  const racing = new SpyCipher(f.cipher, { 1: { before: async () => {
    staged(await stage(f, snapshotPage(uuid(0xb1), { recordType: "weight" }), { context: context({ generation: generation({ maxRecordCount: 1 }) }) }));
  } } });
  assert.equal((await stage(f, snapshotPage(uuid(0xa1)), { cipher: racing })).outcome, "conflict");
  const retried = staged(await stage(f, snapshotPage(uuid(0xa1))));
  assert.deepEqual([retried.firstSequence, retried.lastSequence], [3, 3]);
  const rows = await opened(f.database, f.cipher);
  assert.deepEqual(rows.map((row) => [row.sequence, row.message.cycle_id]), [[1, uuid(0xb1)], [2, uuid(0xb1)], [3, uuid(0xa1)]]);
  // After an abandonment conflict, the retry is refused because the attempt is no longer current.
  const abandoning = new SpyCipher(f.cipher, { 1: { before: async () => { await begin(f.database, "steps", uuid(0xa2), { abandoning: uuid(0xa1) }); } } });
  const page2 = snapshotPage(uuid(0xa1), { pageIndex: 2, readFrom: position("1") });
  assert.equal((await stage(f, page2, { cipher: abandoning })).outcome, "conflict");
  assert.deepEqual(await stage(f, page2), refused("cycle-not-current"));
});

test("the_recheck_runs_inside_the_commit_transaction", async (t) => {
  const conflict = { outcome: "conflict", failure: "staging-conflict" };
  const outer = (f: { database: SenderDatabase }) => f.database;
  // Each state the recheck covers changes on its own, just before the commit transaction opens.
  {
    const f = await fixture(t);
    await begin(f.database, "steps", uuid(0xa1));
    await begin(f.database, "weight", uuid(0xb1));
    const database = beforeTransaction(outer(f), async () => {
      staged(await stage(f, snapshotPage(uuid(0xb1), { recordType: "weight" })));
    });
    assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1)), { database }), conflict);
    assert.equal(await cycleProgress(f.database, uuid(0xa1)), null);
  }
  {
    const f = await fixture(t);
    await begin(f.database, "steps", uuid(0xa1));
    staged(await stage(f, snapshotPage(uuid(0xa1))));
    const outbox = await outboxMessages(f.database);
    const database = beforeTransaction(outer(f), async () => {
      await f.database.execute("UPDATE cycle_progress SET chain = ? WHERE cycle_id = ?", [chainStart(uuid(0xff)), uuid(0xa1)]);
    });
    assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1), { pageIndex: 2, readFrom: position("1") }), { database }), conflict);
    assert.deepEqual(await outboxMessages(f.database), outbox);
    assert.equal((await cycleProgress(f.database, uuid(0xa1)))?.pagesStaged, 1);
  }
  {
    const f = await fixture(t);
    await begin(f.database, "steps", uuid(0xa1));
    const database = beforeTransaction(outer(f), async () => { await begin(f.database, "steps", uuid(0xa2), { abandoning: uuid(0xa1) }); });
    assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1)), { database }), conflict);
    assert.deepEqual(await outboxMessages(f.database), []);
  }
  for (const change of [
    "UPDATE cycle_progress SET changes_token = 'synthetic-changes-token-moved' WHERE cycle_id = ?",
    "UPDATE cycle_progress SET chain = 'synthetic-chain-moved' WHERE cycle_id = (SELECT snapshot_id FROM cycle_progress WHERE cycle_id = ?)",
  ]) {
    // A new Change Cycle depends on the latest finished cycle and on its snapshot's progress.
    const f = await fixture(t);
    const attempt = await begin(f.database, "steps", uuid(0xa1));
    staged(await stage(f, snapshotPage(uuid(0xa1), { exhausted: true })));
    staged(await stage(f, changePage(uuid(0xc1), attempt.changesToken, { nextChangesToken: token("c1-1") })));
    const before = await outboxMessages(f.database);
    const database = beforeTransaction(outer(f), async () => { await f.database.execute(change, [uuid(0xc1)]); });
    assert.deepEqual(await stage(f, changePage(uuid(0xc2), token("c1-1")), { database }), conflict, change);
    assert.deepEqual(await outboxMessages(f.database), before);
    assert.equal(await cycleProgress(f.database, uuid(0xc2)), null);
  }
});

test("append_refuses_a_moved_high_water_mark", async (t) => {
  const f = await fixture(t);
  const message = (kind: OutboxMessage["kind"], n: number): OutboxMessage => ({ kind, sealed: utf8(`synthetic-sealed-${n}`), messageBytes: 100 + n });
  assert.equal(await f.database.transaction((tx) => appendOutboxMessages(tx, INSTALLATION, 0, [message("capability-acknowledgement", 1)])), true);
  assert.equal(await lastOutboxSequence(f.database, INSTALLATION), 1);
  const before = await contents(f.database);
  assert.equal(await f.database.transaction((tx) => appendOutboxMessages(tx, INSTALLATION, 0, [message("sync-checkpoint", 2)])), false);
  assert.equal(await f.database.transaction((tx) => appendOutboxMessages(tx, INSTALLATION, 2, [message("sync-checkpoint", 2)])), false);
  assert.deepEqual(await contents(f.database), before);
  assert.equal(await f.database.transaction((tx) => appendOutboxMessages(tx, INSTALLATION, 1, [message("snapshot-completion", 2), message("change-completion", 3), message("sync-checkpoint", 4)])), true);
  assert.equal(await f.database.transaction((tx) => appendOutboxMessages(tx, OTHER_INSTALLATION, 0, [message("ingest-batch", 5)])), true);
  assert.deepEqual(await outboxMessages(f.database), [
    { installationId: INSTALLATION, sequence: 1, ...message("capability-acknowledgement", 1) },
    { installationId: INSTALLATION, sequence: 2, ...message("snapshot-completion", 2) },
    { installationId: INSTALLATION, sequence: 3, ...message("change-completion", 3) },
    { installationId: INSTALLATION, sequence: 4, ...message("sync-checkpoint", 4) },
    { installationId: OTHER_INSTALLATION, sequence: 1, ...message("ingest-batch", 5) },
  ]);
  await begin(f.database, "steps", uuid(0xa1));
  assert.equal(staged(await stage(f, snapshotPage(uuid(0xa1)))).firstSequence, 5);
  // Every argument, and every message in the list, is validated before any read or write.
  const probe = untouchable();
  const valid = message("sync-checkpoint", 6);
  const invalid: unknown[][] = [
    [INSTALLATION, 5, []],
    [INSTALLATION, 5, "not-a-list"],
    [INSTALLATION, -1, [valid]],
    [INSTALLATION, 1.5, [valid]],
    [INSTALLATION, Number.MAX_SAFE_INTEGER + 1, [valid]],
    ["not-a-uuid", 5, [valid]],
    [INSTALLATION, 5, [{ ...valid, kind: "delivery-receipt" }]],
    [INSTALLATION, 5, [{ ...valid, sealed: new Uint8Array() }]],
    [INSTALLATION, 5, [{ ...valid, sealed: "synthetic-sealed" }]],
    [INSTALLATION, 5, [{ ...valid, messageBytes: 0 }]],
    [INSTALLATION, 5, [{ ...valid, messageBytes: 1.5 }]],
    [INSTALLATION, 5, [{ ...valid, messageBytes: Number.MAX_SAFE_INTEGER + 1 }]],
    [INSTALLATION, 5, [valid, null]],
    [INSTALLATION, 5, [valid, { ...valid, kind: "ingest" }]],
  ];
  for (const [installation, expected, messages] of invalid) {
    await assert.rejects(appendOutboxMessages(probe.database, installation as string, expected as number, messages as OutboxMessage[]), TypeError, JSON.stringify([expected, messages]));
  }
  assert.equal(probe.calls(), 0);
});

// R11 — The Sender schema migration

const OUTBOX_DDL = [
  `CREATE TABLE cycle_progress (
    cycle_id TEXT PRIMARY KEY NOT NULL, record_type TEXT NOT NULL,
    cycle_kind TEXT NOT NULL CHECK (cycle_kind IN ('initial-snapshot', 'reconciliation-snapshot', 'change-cycle')),
    snapshot_id TEXT NOT NULL, lineage INTEGER NOT NULL CHECK (lineage >= 0), installation_id TEXT NOT NULL,
    pages_staged INTEGER NOT NULL CHECK (pages_staged > 0), fragment_count INTEGER NOT NULL CHECK (fragment_count > 0),
    record_count INTEGER NOT NULL CHECK (record_count >= 0), chain TEXT NOT NULL,
    position BLOB, changes_token TEXT, unread_since_ms INTEGER, finished INTEGER NOT NULL CHECK (finished IN (0, 1)),
    UNIQUE (snapshot_id, lineage),
    FOREIGN KEY (snapshot_id, record_type) REFERENCES snapshot_attempts (snapshot_id, record_type),
    CHECK ((cycle_kind = 'change-cycle') = (lineage > 0)),
    CHECK ((lineage = 0) = (cycle_id = snapshot_id)),
    CHECK (CASE WHEN cycle_kind = 'change-cycle'
      THEN position IS NULL AND changes_token IS NOT NULL AND unread_since_ms IS NOT NULL
      ELSE position IS NOT NULL AND changes_token IS NULL AND unread_since_ms IS NULL END)) STRICT`,
  `CREATE TABLE outbox_high_water (
    installation_id TEXT PRIMARY KEY NOT NULL, last_sequence INTEGER NOT NULL CHECK (last_sequence > 0)) STRICT`,
  `CREATE TABLE outbox_messages (
    installation_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK (sequence > 0),
    kind TEXT NOT NULL CHECK (kind IN ('capability-acknowledgement', 'ingest-batch', 'snapshot-completion', 'change-completion', 'sync-checkpoint')),
    sealed BLOB NOT NULL, message_bytes INTEGER NOT NULL CHECK (message_bytes > 0),
    PRIMARY KEY (installation_id, sequence)) STRICT`,
];
const compact = (sql: string) => sql.replace(/\s+/g, "");

test("migration_creates_version_two", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const database = await openNodeDatabase(join(directory, "synthetic.sqlite"));
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.equal(outboxMigration.version, 2);
  assert.deepEqual(await migrate(database, [snapshotAttemptsMigration]), { status: "ready", version: 1 });
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 2 });
  const tables = await database.query("SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name");
  assert.deepEqual(tables.map((row) => row.name), ["current_snapshot_attempts", "cycle_progress", "outbox_high_water", "outbox_messages", "snapshot_attempts"]);
  const owned = tables.filter((row) => ["cycle_progress", "outbox_high_water", "outbox_messages"].includes(row.name as string));
  assert.deepEqual(owned.map((row) => compact(row.sql as string)), OUTBOX_DDL.map(compact));
  const indexes = await database.query("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name IN ('cycle_progress', 'outbox_high_water', 'outbox_messages') ORDER BY name");
  assert.ok(indexes.every((row) => row.sql === null), "only automatic indexes");
  assert.deepEqual(await database.query("SELECT name FROM sqlite_master WHERE type IN ('trigger', 'view')"), []);
});

test("the_schema_constrains_outbox_kinds", async (t) => {
  const f = await fixture(t);
  const insert = (sequence: number, kind: string) => f.database.execute(
    "INSERT INTO outbox_messages (installation_id, sequence, kind, sealed, message_bytes) VALUES (?, ?, ?, ?, 10)",
    [INSTALLATION, sequence, kind, utf8("synthetic-sealed")],
  );
  const kinds = ["capability-acknowledgement", "ingest-batch", "snapshot-completion", "change-completion", "sync-checkpoint"];
  for (const [index, kind] of kinds.entries()) await insert(index + 1, kind);
  for (const kind of ["delivery-receipt", "enrollment-confirmation", "retryable-failure", "Ingest-Batch"]) await assert.rejects(insert(9, kind), kind);
  await assert.rejects(insert(1, "ingest-batch"));
  await assert.rejects(insert(0, "ingest-batch"));
  await begin(f.database, "steps", uuid(0xa1));
  const progress = (values: Record<string, DatabaseValue>) => {
    const row: Record<string, DatabaseValue> = {
      cycle_id: uuid(0xa1), record_type: "steps", cycle_kind: "initial-snapshot", snapshot_id: uuid(0xa1), lineage: 0,
      installation_id: INSTALLATION, pages_staged: 1, fragment_count: 1, record_count: 0, chain: chainStart(uuid(0xa1)),
      position: utf8("p"), changes_token: null, unread_since_ms: null, finished: 0, ...values,
    };
    const columns = Object.keys(row);
    return f.database.execute(`INSERT INTO cycle_progress (${columns.join(", ")}) VALUES (${columns.map(() => "?").join(", ")})`, Object.values(row));
  };
  await assert.rejects(progress({ cycle_kind: "other" }));
  await assert.rejects(progress({ snapshot_id: uuid(0xa9), cycle_id: uuid(0xa9) }), "the snapshot attempt must exist");
  await assert.rejects(progress({ record_type: "weight" }), "the attempt must be of the cycle's record type");
  await assert.rejects(progress({ cycle_id: uuid(0xc1) }), "a snapshot cycle is its attempt");
  await assert.rejects(progress({ changes_token: "t", unread_since_ms: 1 }));
  await assert.rejects(progress({ position: null }));
  await assert.rejects(progress({ cycle_id: uuid(0xc1), cycle_kind: "change-cycle", lineage: 1 }), "a Change Cycle has a token and no position");
  await assert.rejects(progress({ cycle_id: uuid(0xc1), cycle_kind: "change-cycle", lineage: 0, position: null, changes_token: "t", unread_since_ms: 1 }));
  await progress({});
  await progress({ cycle_id: uuid(0xc1), cycle_kind: "change-cycle", lineage: 1, position: null, changes_token: "t", unread_since_ms: 1 });
  await assert.rejects(progress({ cycle_id: uuid(0xc2), cycle_kind: "change-cycle", lineage: 1, position: null, changes_token: "t", unread_since_ms: 1 }), "lineage is unique per attempt");
});

test("migration_is_idempotent", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  staged(await stage(f, snapshotPage(uuid(0xa1))));
  const before = await contents(f.database);
  const schema = await f.database.query("SELECT type, name, sql FROM sqlite_master ORDER BY name");
  assert.deepEqual(await migrate(f.database, MIGRATIONS), { status: "ready", version: 2 });
  assert.deepEqual(await contents(f.database), before);
  assert.deepEqual(await f.database.query("SELECT type, name, sql FROM sqlite_master ORDER BY name"), schema);
});

// R12 — Caller errors, health-data-free outcomes, and order of checks

test("malformed_inputs_are_caller_errors", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  const probe = untouchable();
  const broken = probe.database;
  const spy = new SpyCipher(f.cipher);
  const contexts: unknown[] = [
    context({ installationId: "00000000-0000-4000-8000-00000000000A" }),
    context({ installationId: "not-a-uuid" }),
    context({ keyAlias: "" }),
    context({ keyAlias: 7 as unknown as string }),
    context({ generation: generation({ generation: 0 }) }),
    context({ generation: generation({ generation: 1.5 }) }),
    context({ generation: generation({ maxRecordCount: 0 }) }),
    context({ generation: generation({ maxMessageBytes: 4095 }) }),
    context({ generation: generation({ maxMessageBytes: Number.MAX_SAFE_INTEGER + 1 }) }),
    context({ generation: generation({ projectionSchemas: null as unknown as Record<string, number[]> }) }),
    context({ generation: generation({ projectionSchemas: [] as unknown as Record<string, number[]> }) }),
    context({ generation: generation({ projectionSchemas: new (class Schemas { steps = [1]; })() as unknown as Record<string, number[]> }) }),
    context({ generation: generation({ projectionSchemas: new Map([["steps", [1]]]) as unknown as Record<string, number[]> }) }),
    context({ generation: generation({ projectionSchemas: { Steps: [1] } }) }),
    context({ generation: generation({ projectionSchemas: { steps: [0] } }) }),
    context({ generation: generation({ projectionSchemas: { steps: "1" as unknown as number[] } }) }),
    null,
  ];
  for (const ctx of contexts) {
    await assert.rejects(stageOutboxBatch(broken, spy, ctx as StagingContext, snapshotPage(uuid(0xa1))), TypeError, JSON.stringify(ctx));
  }
  const tooLong = "x".repeat(257);
  const pages: unknown[] = [
    snapshotPage(uuid(0xa1), { recordType: "Steps" }),
    snapshotPage(uuid(0xa1), { recordType: "a".repeat(65) }),
    snapshotPage("00000000-0000-4000-8000-0000000000A1"),
    snapshotPage(uuid(0xa1), { projectionSchemaVersion: 0 }),
    snapshotPage(uuid(0xa1), { pageIndex: 0 }),
    snapshotPage(uuid(0xa1), { pageIndex: 1.5 }),
    { ...snapshotPage(uuid(0xa1)), source: "other" },
    snapshotPage(uuid(0xa1), { readFrom: "synthetic-position" as unknown as Uint8Array }),
    snapshotPage(uuid(0xa1), { nextPosition: null as unknown as Uint8Array }),
    snapshotPage(uuid(0xa1), { exhausted: "no" as unknown as boolean }),
    snapshotPage(uuid(0xa1), { projections: [new Uint8Array()] }),
    snapshotPage(uuid(0xa1), { projections: ["text" as unknown as Uint8Array] }),
    changePage(uuid(0xc1), ""),
    changePage(uuid(0xc1), token("a1"), { nextChangesToken: "" }),
    changePage(uuid(0xc1), token("a1"), { nextUnreadSinceMs: 1.5 }),
    changePage(uuid(0xc1), token("a1"), { hasMore: 1 as unknown as boolean }),
    changePage(uuid(0xc1), token("a1"), { items: [{ kind: "retraction", sourceIdentity: "" }] }),
    changePage(uuid(0xc1), token("a1"), { items: [{ kind: "retraction", sourceIdentity: "synthetic-\u0001" }] }),
    changePage(uuid(0xc1), token("a1"), { items: [{ kind: "retraction", sourceIdentity: tooLong }] }),
    changePage(uuid(0xc1), token("a1"), { items: [{ kind: "projection", bytes: new Uint8Array() }] }),
    changePage(uuid(0xc1), token("a1"), { items: [{ kind: "other" } as unknown as ChangeItem] }),
    null,
  ];
  for (const page of pages) {
    await assert.rejects(stageOutboxBatch(broken, spy, context(), page as ObservedPage), TypeError, JSON.stringify(page));
  }
  assert.equal(spy.seals.length, 0);
  assert.equal(probe.calls(), 0);
  for (const [installation, sequence] of [["not-a-uuid", 1], [INSTALLATION, 0], [INSTALLATION, 1.5], [INSTALLATION, Number.MAX_SAFE_INTEGER + 1]] as const) {
    assert.throws(() => outboxAad(installation, sequence), TypeError);
  }
  // Boundary values are accepted: a 64-character record type, a 256-code-point Source Identity, and a negative instant.
  const longType = "a".repeat(64);
  await begin(f.database, longType, uuid(0xe1));
  staged(await stage(f, snapshotPage(uuid(0xe1), { recordType: longType, exhausted: true }), { context: context({ generation: generation({ projectionSchemas: { [longType]: [1] } }) }) }));
  const identity = `synthetic-${"\u{1F600}".repeat(246)}`;
  assert.equal([...identity].length, 256);
  staged(await stage(f, changePage(uuid(0xc1), token("e1"), { recordType: longType, items: [{ kind: "retraction", sourceIdentity: identity }], nextUnreadSinceMs: -5 }), {
    context: context({ generation: generation({ projectionSchemas: { [longType]: [1] } }) }),
  }));
});

test("non_staged_results_carry_only_named_failures", async (t) => {
  const f = await fixture(t);
  const attempt = await begin(f.database, "steps", uuid(0xa1));
  await begin(f.database, "weight", uuid(0xb1));
  const results: StageResult[] = [
    await stage(f, changePage(uuid(0xc1), `${SENTINEL}-token`)),
    await stage(f, snapshotPage(uuid(0xa1), { readFrom: utf8(SENTINEL) })),
    await stage(f, snapshotPage(uuid(0xa1), { projections: [utf8(`{"synthetic":"${SENTINEL}${"x".repeat(5000)}"}`)] }), { context: context({ generation: generation({ maxMessageBytes: 4096 }) }) }),
    await stage(f, snapshotPage(uuid(0xa1)), { cipher: new SpyCipher(f.cipher, { 1: { throws: new Error(`${SENTINEL} bridge failure`) } }) }),
    await stage(f, snapshotPage(uuid(0xa1)), { database: failing(f.database, { mutation: 1 }) }),
    await stage(f, snapshotPage(uuid(0xa1)), { database: failing(f.database, { queries: true }) }),
    await stage(f, snapshotPage(uuid(0xa1)), { cipher: new SpyCipher(f.cipher, { 1: { before: async () => {
      staged(await stage(f, snapshotPage(uuid(0xb1), { recordType: "weight" })));
    } } }) }),
  ];
  assert.equal(attempt.snapshotId, uuid(0xa1));
  for (const result of results) {
    assert.notEqual(result.outcome, "staged");
    assert.deepEqual(Object.keys(result).sort(), ["failure", "outcome"]);
    assert.ok(!JSON.stringify(result).includes(SENTINEL), JSON.stringify(result));
  }
});

test("every_named_failure_is_reachable_and_in_the_contract", async (t) => {
  const f = await fixture(t);
  const classes: Record<string, string> = { "refused": "refusal", "blocked": "block", "conflict": "conflict", "cipher-failed": "cipher-failure", "storage-failed": "storage-failure" };
  const observed = new Set<string>();
  const record = (result: StageResult) => {
    assert.notEqual(result.outcome, "staged", JSON.stringify(result));
    observed.add(`${classes[result.outcome]}:${(result as { failure: string }).failure}`);
  };
  const attempt = await begin(f.database, "steps", uuid(0xa1));
  await begin(f.database, "weight", uuid(0xb1), { installationId: OTHER_INSTALLATION });
  record(await stage(f, snapshotPage(uuid(0xa9))));
  record(await stage(f, snapshotPage(uuid(0xb1), { recordType: "weight" })));
  record(await stage(f, changePage(uuid(0xc1), attempt.changesToken)));
  record(await stage(f, snapshotPage(uuid(0xa1), { pageIndex: 2 })));
  record(await stage(f, snapshotPage(uuid(0xa1), { projectionSchemaVersion: 9 })));
  record(await stage(f, snapshotPage(uuid(0xa1), { projections: [projection(1, 5000)] }), { context: context({ generation: generation({ maxMessageBytes: 4096 }) }) }));
  record(await stage(f, snapshotPage(uuid(0xa1)), { context: context({ keyAlias: "synthetic-missing-key" }) }));
  const invalidated = new NodeInstallationCipher();
  await invalidated.create(KEY_ALIAS);
  invalidated.invalidate(KEY_ALIAS);
  record(await stage(f, snapshotPage(uuid(0xa1)), { cipher: invalidated }));
  record(await stage(f, snapshotPage(uuid(0xa1)), { cipher: new SpyCipher(f.cipher, { 1: { returns: { status: "error", reason: "keystore-unavailable" } } }) }));
  record(await stage(f, snapshotPage(uuid(0xa1)), { cipher: new SpyCipher(f.cipher, { 1: { throws: new Error("bridge") } }) }));
  record(await stage(f, snapshotPage(uuid(0xa1)), { database: failing(f.database, { commit: true }) }));
  record(await stage(f, snapshotPage(uuid(0xa1)), { cipher: new SpyCipher(f.cipher, { 1: { before: async () => {
    await f.database.transaction((tx) => appendOutboxMessages(tx, INSTALLATION, 0, [{ kind: "sync-checkpoint", sealed: utf8("synthetic-sealed"), messageBytes: 10 }]));
  } } }) }));
  staged(await stage(f, snapshotPage(uuid(0xa1), { exhausted: true })));
  record(await stage(f, snapshotPage(uuid(0xa1), { pageIndex: 2, readFrom: position("1") })));
  staged(await stage(f, changePage(uuid(0xc1), attempt.changesToken, { hasMore: true })));
  record(await stage(f, changePage(uuid(0xc2), attempt.changesToken)));
  const named = new Set((CONTRACT.named_failures as { name: string; outcome: string }[]).map((failure) => `${failure.outcome}:${failure.name}`));
  assert.deepEqual([...observed].sort(), [...named].sort());
});

test("checks_follow_the_documented_order", async (t) => {
  const f = await fixture(t);
  await begin(f.database, "steps", uuid(0xa1));
  await begin(f.database, "weight", uuid(0xb1), { installationId: OTHER_INSTALLATION });
  const probe = untouchable();
  const broken = probe.database;
  const oversized = { projections: [projection(1, 5000)] };
  const small = context({ generation: generation({ maxMessageBytes: 4096 }) });
  // 1. Caller errors before everything, even an unsupported type and a broken store.
  await assert.rejects(stageOutboxBatch(broken, f.cipher, context(), snapshotPage(uuid(0xa1), { recordType: "Sleep", projectionSchemaVersion: 9 })), TypeError);
  // 2. The generation's projection schemas before any storage read.
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1), { projectionSchemaVersion: 9 }), { database: broken }), refused("projection-schema-unsupported"));
  assert.equal(probe.calls(), 0);
  // 3. Reading the state, then the cycle checks, before partitioning.
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1), oversized), { database: failing(f.database, { queries: true }), context: small }), { outcome: "storage-failed", failure: "storage-failed" });
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa9), oversized), { context: small }), refused("cycle-not-current"));
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xb9), { recordType: "weight" })), refused("cycle-not-current"));
  const other = context({ installationId: OTHER_INSTALLATION });
  staged(await stage(f, snapshotPage(uuid(0xb1), { recordType: "weight", exhausted: true }), { context: other }));
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xb1), { recordType: "weight", pageIndex: 9 })), refused("installation-mismatch"));
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xb1), { recordType: "weight", pageIndex: 9 }), { context: other }), refused("cycle-finished"));
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1), { ...oversized, pageIndex: 2 }), { context: small }), refused("page-position-mismatch"));
  // 4. Partitioning before sealing.
  const spy = new SpyCipher(f.cipher);
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1), oversized), { cipher: spy, context: context({ keyAlias: "synthetic-missing-key", generation: small.generation }) }),
    { outcome: "blocked", failure: "indivisible-record-limit-exceeded" });
  assert.equal(spy.seals.length, 0);
  // 5. Sealing before the commit.
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1)), { database: failing(f.database, { mutation: 1 }), context: context({ keyAlias: "synthetic-missing-key" }) }),
    { outcome: "cipher-failed", failure: "installation-key-missing" });
  // 6. The commit last.
  assert.deepEqual(await stage(f, snapshotPage(uuid(0xa1)), { database: failing(f.database, { mutation: 1 }) }), { outcome: "storage-failed", failure: "storage-failed" });
  staged(await stage(f, snapshotPage(uuid(0xa1))));
});

test("the_module_imports_no_node_built_ins", () => {
  // The module runs under Hermes in the Sender, which has no Node built-in modules.
  const source = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../src/outbox/stage-outbox-batch.ts"), "utf8");
  const specifiers = [...source.matchAll(/\b(?:from|import)\s*\(?\s*["']([^"']+)["']/g)].map((match) => match[1]);
  assert.ok(specifiers.length > 0);
  const allowed = /^(?:\.{1,2}\/|@activity-tracker-portal\/ingest-protocol$)/;
  assert.deepEqual(specifiers.filter((specifier) => !allowed.test(specifier)), []);
  assert.doesNotMatch(source, /\b(?:Buffer|require|process)\b/);
});
