// Acceptance tests for stage-sync-completion@1.
// Requirement numbers refer to packs/stage-sync-completion-1.spec.md. Storage is the 3ug node:sqlite
// SenderDatabase substitute and the cipher is the 3ug NodeInstallationCipher; attempts and pages come
// from the published begin-initial-snapshot@2, begin-reconciliation-snapshot@1, and stage-outbox-batch@1.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import type { TestContext } from "node:test";
import { chainNext, chainStart, decodeMessage, encodeMessage, messageSha256 } from "@activity-tracker-portal/ingest-protocol";
import type { Message } from "@activity-tracker-portal/ingest-protocol";
import type { CipherResult, InstallationCipher, InstallationKeyStatus } from "../src/installation-cipher.ts";
import { completionMigration, cycleCompletion, stageSyncCompletion } from "../src/outbox/stage-sync-completion.ts";
import type { CompletionRequest, CompletionResult, CycleCompletion } from "../src/outbox/stage-sync-completion.ts";
import { appendOutboxMessages, cycleProgress, lastOutboxSequence, outboxAad, outboxMessages, outboxMigration, stageOutboxBatch } from "../src/outbox/stage-outbox-batch.ts";
import type { ObservedPage, StagingContext } from "../src/outbox/stage-outbox-batch.ts";
import { beginInitialSnapshot, snapshotAttemptsMigration } from "../src/snapshot/begin-initial-snapshot.ts";
import type { SnapshotAttempt } from "../src/snapshot/begin-initial-snapshot.ts";
import { beginReconciliationSnapshot } from "../src/snapshot/begin-reconciliation-snapshot.ts";
import type { DatabaseValue, SenderDatabase } from "../src/storage/database.ts";
import { migrate } from "../src/storage/migrations.ts";
import { openNodeDatabase } from "./support/node-database.ts";
import { NodeInstallationCipher } from "./support/node-installation-cipher.ts";

const CONTRACT = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../../../governance/scopes/stage-sync-completion.1.json"), "utf8"));
const MODULE = join(dirname(fileURLToPath(import.meta.url)), "../src/outbox/stage-sync-completion.ts");
const INSTALLATION = "00000000-0000-4000-8000-000000000001";
const OTHER_INSTALLATION = "00000000-0000-4000-8000-000000000002";
const KEY_ALIAS = "synthetic-installation-key";
const NOW = 1_791_000_005_000;
const SENTINEL = "SENTINEL";
const MIGRATIONS = [snapshotAttemptsMigration, outboxMigration, completionMigration];
const uuid = (n: number) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, "0")}`;
const utf8 = (text: string) => new TextEncoder().encode(text);

async function fixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const path = join(directory, "synthetic.sqlite");
  let database = await openNodeDatabase(path);
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 3 });
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
    generation: { generation: 5, maxRecordCount: 2, maxMessageBytes: 65_536, projectionSchemas: { "heart-rate": [1], steps: [1], weight: [1] } },
    ...overrides,
  };
}

const token = (label: string) => `synthetic-changes-token-${label}`;

async function initial(database: SenderDatabase, snapshotId: string, extra: { recordType?: string; installationId?: string; abandoning?: string } = {}): Promise<SnapshotAttempt> {
  const issuer = { async issue() { return { outcome: "issued" as const, changesToken: token(snapshotId.slice(-2)), unreadSinceMs: 7 }; } };
  const result = await beginInitialSnapshot(database, issuer,
    { recordType: extra.recordType ?? "steps", installationId: extra.installationId ?? INSTALLATION, ...(extra.abandoning ? { abandoning: extra.abandoning } : {}) },
    { randomUUID: () => snapshotId, now: () => NOW });
  assert.equal(result.outcome, "begun", JSON.stringify(result));
  return (result as { attempt: SnapshotAttempt }).attempt;
}

async function reconcile(database: SenderDatabase, superseding: string, snapshotId: string, recordType = "steps"): Promise<SnapshotAttempt> {
  const issuer = { async issue() { return { outcome: "issued" as const, changesToken: token(snapshotId.slice(-2)), unreadSinceMs: 9 }; } };
  const result = await beginReconciliationSnapshot(database, issuer, { recordType, installationId: INSTALLATION, superseding }, { randomUUID: () => snapshotId, now: () => NOW + 1 });
  assert.equal(result.outcome, "begun", JSON.stringify(result));
  return (result as { attempt: SnapshotAttempt }).attempt;
}

async function stage(f: { database: SenderDatabase; cipher: InstallationCipher }, page: ObservedPage, ctx = context()) {
  const result = await stageOutboxBatch(f.database, f.cipher, ctx, page);
  assert.equal(result.outcome, "staged", JSON.stringify(result));
  return result;
}

const projection = (n: number) => utf8(`{"synthetic":${n}}`);

/** Stage a snapshot page; `projections` items under a two-item fragment limit. */
function snapshotPage(attempt: SnapshotAttempt, pageIndex: number, count: number, exhausted: boolean): ObservedPage {
  return {
    source: "snapshot", recordType: attempt.recordType, cycleId: attempt.snapshotId, projectionSchemaVersion: 1, pageIndex,
    readFrom: pageIndex === 1 ? null : utf8(`position-${pageIndex - 1}`),
    projections: Array.from({ length: count }, (_, i) => projection(pageIndex * 10 + i)), nextPosition: utf8(`position-${pageIndex}`), exhausted,
  };
}

/** A finished snapshot cycle: two pages, 3 + 0 items, so 2 + 1 fragments and 3 records. */
async function finishedSnapshot(f: { database: SenderDatabase; cipher: InstallationCipher }, attempt: SnapshotAttempt, ctx = context()) {
  await stage(f, snapshotPage(attempt, 1, 3, false), ctx);
  return stage(f, snapshotPage(attempt, 2, 0, true), ctx);
}

function changePage(recordType: string, cycleId: string, pageIndex: number, readFrom: string, hasMore: boolean): ObservedPage {
  return {
    source: "change", recordType, cycleId, projectionSchemaVersion: 1, pageIndex, readFrom,
    items: [{ kind: "retraction", sourceIdentity: `synthetic-source-${pageIndex}` }, { kind: "projection", bytes: projection(pageIndex) }],
    nextChangesToken: token(`${cycleId.slice(-2)}-${pageIndex}`), nextUnreadSinceMs: 100 + pageIndex, hasMore,
  };
}

function complete(f: { database: SenderDatabase; cipher: InstallationCipher }, request: CompletionRequest, options: { cipher?: InstallationCipher; context?: StagingContext; database?: SenderDatabase } = {}): Promise<CompletionResult> {
  const guard = guarded(options.cipher ?? f.cipher);
  return stageSyncCompletion(options.database ?? f.database, guard.cipher, options.context ?? context(), request).then((result) => {
    assert.deepEqual(guard.forbidden, [], "completion only seals");
    return result;
  });
}

function staged(result: CompletionResult): CycleCompletion {
  assert.equal(result.outcome, "staged", JSON.stringify(result));
  return (result as { completion: CycleCompletion }).completion;
}

const refused = (failure: string) => ({ outcome: "refused", failure });

/** Each Outbox row of the installation, opened with its own authenticated data and decoded. */
async function opened(database: SenderDatabase, cipher: InstallationCipher, installationId = INSTALLATION) {
  const rows = (await outboxMessages(database)).filter((row) => row.installationId === installationId);
  const result: { sequence: number; kind: string; bytes: Uint8Array; message: Message }[] = [];
  for (const row of rows) {
    const plain = await cipher.open(KEY_ALIAS, row.sealed, outboxAad(row.installationId, row.sequence));
    assert.equal(plain.status, "ok");
    const bytes = (plain as { value: Uint8Array }).value;
    result.push({ sequence: row.sequence, kind: row.kind, bytes, message: decodeMessage(bytes) });
  }
  return result;
}

async function contents(database: SenderDatabase) {
  return {
    version: await database.query("PRAGMA user_version"),
    outbox: await database.query("SELECT installation_id, sequence, kind, message_bytes FROM outbox_messages ORDER BY installation_id, sequence"),
    highWater: await database.query("SELECT * FROM outbox_high_water ORDER BY installation_id"),
    progress: await database.query("SELECT * FROM cycle_progress ORDER BY cycle_id"),
    completions: await database.query("SELECT * FROM cycle_completions ORDER BY cycle_id"),
    attempts: await database.query("SELECT * FROM snapshot_attempts ORDER BY snapshot_id"),
  };
}

/** Delegates to a cipher, recording any create, delete, or open, which a completion must never make. */
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

/** Records seals; a script entry replaces the numbered seal call (from 1) or runs a hook before it. */
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

// R2 — The completion message

test("a_snapshot_completion_matches_the_protocol", async (t) => {
  const f = await fixture(t);
  const attempt = await initial(f.database, uuid(0xa1));
  await finishedSnapshot(f, attempt);
  const progress = (await cycleProgress(f.database, uuid(0xa1)))!;
  const completion = staged(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }));
  const expected = encodeMessage({
    wire_version: 1, kind: "snapshot-completion", sender_installation: INSTALLATION, sequence: 4, capability_generation: 5,
    snapshot_kind: "initial", record_type: "steps", cycle_id: uuid(0xa1),
    page_count: 2, fragment_count: 3, record_count: 3, final_chain: progress.chain,
  });
  const rows = await opened(f.database, f.cipher);
  assert.deepEqual(rows.at(-1)!.bytes, expected);
  assert.deepEqual(completion, { cycleId: uuid(0xa1), recordType: "steps", kind: "snapshot-completion", installationId: INSTALLATION, sequence: 4 });
  // The counts and chain equal what the Portal accumulates from the cycle's fragments.
  let chain = chainStart(uuid(0xa1));
  for (const row of rows.slice(0, -1)) chain = chainNext(chain, messageSha256(row.bytes));
  assert.equal(rows.at(-1)!.message.final_chain, chain);
  assert.equal(rows.at(-1)!.message.record_count, rows.slice(0, -1).reduce((sum, row) => sum + (row.message.items as Message[]).length, 0));
});

test("a_reconciliation_completion_carries_its_kind", async (t) => {
  const f = await fixture(t);
  await finishedSnapshot(f, await initial(f.database, uuid(0xa1)));
  const reconciliation = await reconcile(f.database, uuid(0xa1), uuid(0xa2));
  await finishedSnapshot(f, reconciliation);
  staged(await complete(f, { recordType: "steps", cycleId: uuid(0xa2) }));
  const message = (await opened(f.database, f.cipher)).at(-1)!.message;
  assert.equal(message.kind, "snapshot-completion");
  assert.equal(message.snapshot_kind, "reconciliation");
  assert.equal(message.cycle_id, uuid(0xa2));
});

test("a_change_completion_matches_the_protocol", async (t) => {
  const f = await fixture(t);
  const attempt = await initial(f.database, uuid(0xa1));
  await finishedSnapshot(f, attempt);
  await stage(f, changePage("steps", uuid(0xc1), 1, attempt.changesToken, true));
  await stage(f, changePage("steps", uuid(0xc1), 2, token("c1-1"), false));
  const progress = (await cycleProgress(f.database, uuid(0xc1)))!;
  const completion = staged(await complete(f, { recordType: "steps", cycleId: uuid(0xc1) }));
  assert.equal(completion.kind, "change-completion");
  const expected = encodeMessage({
    wire_version: 1, kind: "change-completion", sender_installation: INSTALLATION, sequence: completion.sequence, capability_generation: 5,
    record_type: "steps", cycle_id: uuid(0xc1), page_count: 2, fragment_count: progress.fragmentCount, record_count: 4, final_chain: progress.chain,
  });
  assert.deepEqual((await opened(f.database, f.cipher)).at(-1)!.bytes, expected);
});

test("the_completion_carries_no_cursor_or_token", async (t) => {
  const f = await fixture(t);
  const attempt = await initial(f.database, uuid(0xa1));
  await finishedSnapshot(f, attempt);
  await stage(f, changePage("steps", uuid(0xc1), 1, attempt.changesToken, false));
  staged(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }));
  staged(await complete(f, { recordType: "steps", cycleId: uuid(0xc1) }));
  const [snapshot, change] = (await opened(f.database, f.cipher)).filter((row) => row.kind !== "ingest-batch");
  assert.deepEqual(Object.keys(snapshot.message).sort(), ["capability_generation", "cycle_id", "final_chain", "fragment_count", "kind", "page_count", "record_count", "record_type", "sender_installation", "sequence", "snapshot_kind", "wire_version"]);
  assert.deepEqual(Object.keys(change.message).sort(), ["capability_generation", "cycle_id", "final_chain", "fragment_count", "kind", "page_count", "record_count", "record_type", "sender_installation", "sequence", "wire_version"]);
  for (const row of [snapshot, change]) {
    const text = new TextDecoder().decode(row.bytes);
    assert.ok(!text.includes("synthetic-changes-token") && !text.includes("position-"), text);
  }
});

// R3 — Sealed and appended after every fragment

test("the_completion_is_sealed_and_appended", async (t) => {
  const f = await fixture(t);
  await finishedSnapshot(f, await initial(f.database, uuid(0xa1)));
  const spy = new SpyCipher(f.cipher);
  const completion = staged(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }, { cipher: spy }));
  assert.equal(spy.seals.length, 1);
  assert.equal(spy.seals[0].alias, KEY_ALIAS);
  assert.deepEqual(spy.seals[0].aad, outboxAad(INSTALLATION, completion.sequence));
  const row = (await outboxMessages(f.database)).at(-1)!;
  assert.deepEqual([row.sequence, row.kind, row.messageBytes], [completion.sequence, "snapshot-completion", spy.seals[0].plaintext.length]);
  assert.deepEqual((await opened(f.database, f.cipher)).at(-1)!.bytes, spy.seals[0].plaintext);
});

test("the_completion_follows_every_fragment", async (t) => {
  const f = await fixture(t);
  const steps = await initial(f.database, uuid(0xa1));
  const weight = await initial(f.database, uuid(0xb1), { recordType: "weight" });
  await stage(f, snapshotPage(steps, 1, 3, true));
  await stage(f, snapshotPage(weight, 1, 1, false));
  const completion = staged(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }));
  const rows = await opened(f.database, f.cipher);
  const fragments = rows.filter((row) => row.message.cycle_id === uuid(0xa1) && row.kind === "ingest-batch").map((row) => row.sequence);
  assert.ok(fragments.every((sequence) => sequence < completion.sequence));
  assert.equal(completion.sequence, 4);
  assert.equal(await lastOutboxSequence(f.database, INSTALLATION), 4);
});

// R4 — Only a finished, current cycle

test("an_unfinished_cycle_is_refused", async (t) => {
  const f = await fixture(t);
  const attempt = await initial(f.database, uuid(0xa1));
  assert.deepEqual(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }), refused("chain-incomplete"), "no page staged");
  await stage(f, snapshotPage(attempt, 1, 1, false));
  assert.deepEqual(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }), refused("chain-incomplete"), "not exhausted");
  await stage(f, snapshotPage(attempt, 2, 1, true));
  await stage(f, changePage("steps", uuid(0xc1), 1, attempt.changesToken, true));
  assert.deepEqual(await complete(f, { recordType: "steps", cycleId: uuid(0xc1) }), refused("chain-incomplete"), "a Change Cycle with more pages");
  staged(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }));
});

test("a_cycle_of_a_superseded_attempt_is_refused", async (t) => {
  const f = await fixture(t);
  const first = await initial(f.database, uuid(0xa1));
  await finishedSnapshot(f, first);
  await stage(f, changePage("steps", uuid(0xc1), 1, first.changesToken, false));
  await reconcile(f.database, uuid(0xa1), uuid(0xa2));
  assert.deepEqual(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }), refused("cycle-not-current"), "the superseded snapshot");
  assert.deepEqual(await complete(f, { recordType: "steps", cycleId: uuid(0xc1) }), refused("cycle-not-current"), "a Change Cycle under it");
  const abandoned = await initial(f.database, uuid(0xb1), { recordType: "weight" });
  await finishedSnapshot(f, abandoned);
  await initial(f.database, uuid(0xb2), { recordType: "weight", abandoning: uuid(0xb1) });
  assert.deepEqual(await complete(f, { recordType: "weight", cycleId: uuid(0xb1) }), refused("cycle-not-current"), "an abandoned Initial Snapshot");
});

test("another_installation_is_refused", async (t) => {
  const f = await fixture(t);
  const attempt = await initial(f.database, uuid(0xa1), { installationId: OTHER_INSTALLATION });
  await finishedSnapshot(f, attempt, context({ installationId: OTHER_INSTALLATION }));
  assert.deepEqual(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }), refused("installation-mismatch"));
  staged(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }, { context: context({ installationId: OTHER_INSTALLATION }) }));
});

test("an_unknown_cycle_is_refused", async (t) => {
  const f = await fixture(t);
  const attempt = await initial(f.database, uuid(0xa1));
  await finishedSnapshot(f, attempt);
  await initial(f.database, uuid(0xb1), { recordType: "weight" });
  assert.deepEqual(await complete(f, { recordType: "steps", cycleId: uuid(0xa9) }), refused("cycle-not-current"), "an unknown identity");
  assert.deepEqual(await complete(f, { recordType: "weight", cycleId: uuid(0xa1) }), refused("cycle-not-current"), "another record type's cycle");
  assert.deepEqual(await complete(f, { recordType: "heart-rate", cycleId: uuid(0xa1) }), refused("cycle-not-current"), "a type with no attempt");
  assert.deepEqual(await complete(f, { recordType: "weight", cycleId: uuid(0xb1) }), refused("chain-incomplete"), "the current attempt before any page");
});

// R5 — Exactly once

test("a_cycle_is_completed_once", async (t) => {
  const f = await fixture(t);
  await finishedSnapshot(f, await initial(f.database, uuid(0xa1)));
  const first = staged(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }));
  const before = await contents(f.database);
  const spy = new SpyCipher(f.cipher);
  assert.deepEqual(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }, { cipher: spy }), refused("completion-already-staged"));
  assert.equal(spy.seals.length, 0);
  assert.deepEqual(await contents(f.database), before);
  assert.deepEqual(await cycleCompletion(f.database, uuid(0xa1)), first);
  assert.equal(await cycleCompletion(f.database, uuid(0xa9)), null);
  // Removing the delivered row, as send-outbox-message does, does not allow a second completion.
  await f.database.execute("DELETE FROM outbox_messages WHERE sequence = ?", [first.sequence]);
  assert.deepEqual(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }), refused("completion-already-staged"));
});

test("the_completion_record_survives_reopening", async (t) => {
  const f = await fixture(t);
  await finishedSnapshot(f, await initial(f.database, uuid(0xa1)));
  const completion = staged(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }));
  const reopened = await f.reopen();
  assert.deepEqual(await migrate(reopened, MIGRATIONS), { status: "ready", version: 3 });
  assert.deepEqual(await cycleCompletion(reopened, uuid(0xa1)), completion);
  assert.deepEqual(await complete({ database: reopened, cipher: f.cipher }, { recordType: "steps", cycleId: uuid(0xa1) }), refused("completion-already-staged"));
});

// R6 — Recheck inside the commit

test("interleaved_staging_conflicts_without_writing", async (t) => {
  const f = await fixture(t);
  const conflict = { outcome: "conflict", failure: "staging-conflict" };
  const steps = await initial(f.database, uuid(0xa1));
  const weight = await initial(f.database, uuid(0xb1), { recordType: "weight" });
  await finishedSnapshot(f, steps);
  // Another record type takes the next sequence while the completion is being sealed.
  const otherType = new SpyCipher(f.cipher, { 1: { before: async () => { await stage(f, snapshotPage(weight, 1, 1, false)); } } });
  assert.deepEqual(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }, { cipher: otherType }), conflict);
  assert.equal(await cycleCompletion(f.database, uuid(0xa1)), null);
  // The same cycle is completed by another call while this one is sealing.
  const racing = new SpyCipher(f.cipher, { 1: { before: async () => { staged(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) })); } } });
  assert.deepEqual(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }, { cipher: racing }), conflict);
  assert.equal((await outboxMessages(f.database)).filter((row) => row.kind === "snapshot-completion").length, 1);
  // The attempt is superseded while the completion is being sealed.
  await stage(f, snapshotPage(weight, 2, 1, true));
  const before = await contents(f.database);
  const superseding = new SpyCipher(f.cipher, { 1: { before: async () => { await reconcile(f.database, uuid(0xb1), uuid(0xb2), "weight"); } } });
  assert.deepEqual(await complete(f, { recordType: "weight", cycleId: uuid(0xb1) }, { cipher: superseding }), conflict);
  const after = await contents(f.database);
  assert.deepEqual([after.outbox, after.completions], [before.outbox, before.completions]);
});

test("the_recheck_runs_inside_the_commit_transaction", async (t) => {
  const conflict = { outcome: "conflict", failure: "staging-conflict" };
  for (const change of [
    "UPDATE cycle_progress SET finished = 0 WHERE cycle_id = ?",
    "UPDATE cycle_progress SET chain = 'synthetic-chain-moved' WHERE cycle_id = ?",
    "INSERT INTO cycle_completions (cycle_id, record_type, kind, installation_id, sequence) VALUES (?, 'steps', 'snapshot-completion', '00000000-0000-4000-8000-000000000001', 999)",
  ]) {
    const f = await fixture(t);
    await finishedSnapshot(f, await initial(f.database, uuid(0xa1)));
    const before = await contents(f.database);
    const database = beforeTransaction(f.database, async () => { await f.database.execute(change, [uuid(0xa1)]); });
    assert.deepEqual(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }, { database }), conflict, change);
    assert.deepEqual((await contents(f.database)).outbox, before.outbox, change);
  }
  const f = await fixture(t);
  await finishedSnapshot(f, await initial(f.database, uuid(0xa1)));
  const database = beforeTransaction(f.database, async () => {
    await f.database.transaction((tx) => appendOutboxMessages(tx, INSTALLATION, 3, [{ kind: "sync-checkpoint", sealed: utf8("synthetic-sealed"), messageBytes: 10 }]));
  });
  assert.deepEqual(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }, { database }), conflict, "the high-water mark moves");
  assert.equal(await cycleCompletion(f.database, uuid(0xa1)), null);
});

// R7 — Nothing staged on failure

test("cipher_failures_write_nothing", async (t) => {
  const f = await fixture(t);
  await finishedSnapshot(f, await initial(f.database, uuid(0xa1)));
  const before = await contents(f.database);
  const request = { recordType: "steps", cycleId: uuid(0xa1) };
  const failed = (failure: string) => ({ outcome: "cipher-failed", failure });
  assert.deepEqual(await complete(f, request, { context: context({ keyAlias: "synthetic-missing-key" }) }), failed("installation-key-missing"));
  const invalidated = new NodeInstallationCipher();
  await invalidated.create(KEY_ALIAS);
  invalidated.invalidate(KEY_ALIAS);
  assert.deepEqual(await complete(f, request, { cipher: invalidated }), failed("installation-key-unusable"));
  const unavailable = new NodeInstallationCipher();
  await unavailable.create(KEY_ALIAS);
  unavailable.setUnavailable(true);
  assert.deepEqual(await complete(f, request, { cipher: unavailable }), failed("keystore-unavailable"));
  for (const entry of [
    { throws: new Error(`${SENTINEL} bridge`) }, { throws: null }, { returns: { status: "ok", value: new Uint8Array() } },
    { returns: { status: "ok", value: "text" } }, { returns: { status: "error", reason: "authentication-failed" } },
    { returns: { status: "error", reason: "toString" } }, { returns: undefined },
  ]) {
    assert.deepEqual(await complete(f, request, { cipher: new SpyCipher(f.cipher, { 1: entry }) }), failed("unexpected-cipher-failure"), JSON.stringify(entry));
  }
  assert.deepEqual(await contents(f.database), before);
});

test("storage_errors_roll_back_everything", async (t) => {
  const f = await fixture(t);
  await finishedSnapshot(f, await initial(f.database, uuid(0xa1)));
  const before = await contents(f.database);
  const request = { recordType: "steps", cycleId: uuid(0xa1) };
  let points = 0;
  for (let mutation = 1; ; mutation++) {
    const result = await complete(f, request, { database: failing(f.database, { mutation }) });
    if (result.outcome === "staged") break;
    assert.deepEqual(result, { outcome: "storage-failed", failure: "storage-failed" });
    assert.deepEqual(await contents(f.database), before, `failing mutation ${mutation}`);
    points++;
    assert.ok(mutation < 20, "the completion never committed");
  }
  assert.ok(points >= 3, "the Outbox row, the high-water mark, and the completion record each take a statement");
  const f2 = await fixture(t);
  await finishedSnapshot(f2, await initial(f2.database, uuid(0xa1)));
  const settled = await contents(f2.database);
  const spy = new SpyCipher(f2.cipher);
  assert.deepEqual(await complete(f2, request, { database: failing(f2.database, { queries: true }), cipher: spy }), { outcome: "storage-failed", failure: "storage-failed" });
  assert.equal(spy.seals.length, 0);
  for (const rule of [{ transactionQueries: true }, { commit: true }]) {
    assert.deepEqual(await complete(f2, request, { database: failing(f2.database, rule) }), { outcome: "storage-failed", failure: "storage-failed" }, JSON.stringify(rule));
    assert.deepEqual(await contents(f2.database), settled, JSON.stringify(rule));
  }
});

// R8 — The Sender schema migration

const COMPLETION_DDL = `CREATE TABLE cycle_completions (
  cycle_id TEXT PRIMARY KEY NOT NULL, record_type TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('snapshot-completion', 'change-completion')),
  installation_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK (sequence > 0),
  UNIQUE (installation_id, sequence)) STRICT`;
const compact = (sql: string) => sql.replace(/\s+/g, "");

test("migration_creates_version_three", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const database = await openNodeDatabase(join(directory, "synthetic.sqlite"));
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.equal(completionMigration.version, 3);
  assert.deepEqual(await migrate(database, MIGRATIONS.slice(0, 2)), { status: "ready", version: 2 });
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 3 });
  const [table] = await database.query("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cycle_completions'");
  assert.equal(compact(table.sql as string), compact(COMPLETION_DDL));
  const created = await database.query("SELECT type, name, sql FROM sqlite_master WHERE tbl_name = 'cycle_completions' ORDER BY name");
  assert.ok(created.every((row) => row.type === "table" || row.sql === null), "only automatic indexes");
  assert.deepEqual((await database.query("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")).map((row) => row.name),
    ["current_snapshot_attempts", "cycle_completions", "cycle_progress", "outbox_high_water", "outbox_messages", "snapshot_attempts"]);
  const insert = (id: string, kind: string, sequence: number) => database.execute(
    "INSERT INTO cycle_completions (cycle_id, record_type, kind, installation_id, sequence) VALUES (?, 'steps', ?, ?, ?)", [id, kind, INSTALLATION, sequence]);
  await insert(uuid(1), "snapshot-completion", 1);
  await insert(uuid(2), "change-completion", 2);
  await assert.rejects(insert(uuid(3), "ingest-batch", 3));
  await assert.rejects(insert(uuid(1), "change-completion", 4), "one completion per cycle");
  await assert.rejects(insert(uuid(5), "change-completion", 2), "one completion per sequence");
  await assert.rejects(insert(uuid(6), "change-completion", 0));
});

test("migration_is_idempotent", async (t) => {
  const f = await fixture(t);
  await finishedSnapshot(f, await initial(f.database, uuid(0xa1)));
  staged(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }));
  const before = await contents(f.database);
  const schema = await f.database.query("SELECT type, name, sql FROM sqlite_master ORDER BY name");
  assert.deepEqual(await migrate(f.database, MIGRATIONS), { status: "ready", version: 3 });
  assert.deepEqual(await contents(f.database), before);
  assert.deepEqual(await f.database.query("SELECT type, name, sql FROM sqlite_master ORDER BY name"), schema);
});

// R9 — Caller errors, health-data-free outcomes, order of checks, and key use

test("malformed_inputs_are_caller_errors", async (t) => {
  const f = await fixture(t);
  const probe = untouchable();
  const spy = new SpyCipher(f.cipher);
  const good = { recordType: "steps", cycleId: uuid(0xa1) };
  const cases: [unknown, unknown][] = [
    [context({ installationId: "not-a-uuid" }), good],
    [context({ installationId: "00000000-0000-4000-8000-00000000000A" }), good],
    [context({ keyAlias: "" }), good],
    [context({ generation: { generation: 0, maxRecordCount: 1, maxMessageBytes: 4096, projectionSchemas: {} } }), good],
    [context({ generation: { generation: 1, maxRecordCount: 1, maxMessageBytes: 4095, projectionSchemas: {} } }), good],
    [context({ generation: { generation: 1, maxRecordCount: 1, maxMessageBytes: 4096, projectionSchemas: new Map() as unknown as Record<string, number[]> } }), good],
    [null, good],
    [context(), { recordType: "Steps", cycleId: uuid(0xa1) }],
    [context(), { recordType: "a".repeat(65), cycleId: uuid(0xa1) }],
    [context(), { recordType: "steps", cycleId: "00000000-0000-4000-8000-0000000000A1" }],
    [context(), { recordType: "steps" }],
    [context(), null],
  ];
  for (const [ctx, request] of cases) {
    await assert.rejects(stageSyncCompletion(probe.database, spy, ctx as StagingContext, request as CompletionRequest), TypeError, JSON.stringify([ctx, request]));
  }
  assert.equal(probe.calls(), 0);
  assert.equal(spy.seals.length, 0);
});

test("non_staged_results_carry_only_named_failures", async (t) => {
  const f = await fixture(t);
  const attempt = await initial(f.database, uuid(0xa1));
  await stage(f, snapshotPage(attempt, 1, 1, false));
  const results = [
    await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }),
    await complete(f, { recordType: "steps", cycleId: uuid(0xa9) }),
  ];
  await stage(f, snapshotPage(attempt, 2, 1, true));
  results.push(
    await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }, { cipher: new SpyCipher(f.cipher, { 1: { throws: new Error(`${SENTINEL} bridge failure`) } }) }),
    await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }, { database: failing(f.database, { mutation: 1 }) }),
    await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }, { database: failing(f.database, { queries: true }) }),
  );
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
  const record = (result: CompletionResult) => {
    assert.notEqual(result.outcome, "staged", JSON.stringify(result));
    observed.add(`${classes[result.outcome]}:${(result as { failure: string }).failure}`);
  };
  const steps = await initial(f.database, uuid(0xa1));
  const other = await initial(f.database, uuid(0xb1), { recordType: "weight", installationId: OTHER_INSTALLATION });
  await finishedSnapshot(f, other, context({ installationId: OTHER_INSTALLATION }));
  const request = { recordType: "steps", cycleId: uuid(0xa1) };
  record(await complete(f, { recordType: "steps", cycleId: uuid(0xa9) }));
  record(await complete(f, request));
  record(await complete(f, { recordType: "weight", cycleId: uuid(0xb1) }));
  await finishedSnapshot(f, steps);
  record(await complete(f, request, { context: context({ keyAlias: "synthetic-missing-key" }) }));
  const invalidated = new NodeInstallationCipher();
  await invalidated.create(KEY_ALIAS);
  invalidated.invalidate(KEY_ALIAS);
  record(await complete(f, request, { cipher: invalidated }));
  record(await complete(f, request, { cipher: new SpyCipher(f.cipher, { 1: { returns: { status: "error", reason: "keystore-unavailable" } } }) }));
  record(await complete(f, request, { cipher: new SpyCipher(f.cipher, { 1: { throws: new Error("bridge") } }) }));
  record(await complete(f, request, { database: failing(f.database, { commit: true }) }));
  record(await complete(f, request, { cipher: new SpyCipher(f.cipher, { 1: { before: async () => {
    await f.database.transaction((tx) => appendOutboxMessages(tx, INSTALLATION, 3, [{ kind: "sync-checkpoint", sealed: utf8("synthetic-sealed"), messageBytes: 10 }]));
  } } }) }));
  staged(await complete(f, request));
  record(await complete(f, request));
  const named = new Set((CONTRACT.named_failures as { name: string; outcome: string }[]).map((failure) => `${failure.outcome}:${failure.name}`));
  assert.deepEqual([...observed].sort(), [...named].sort());
});

test("checks_follow_the_documented_order", async (t) => {
  const f = await fixture(t);
  // 1. Caller errors before any storage use.
  await assert.rejects(stageSyncCompletion(untouchable().database, f.cipher, context(), { recordType: "steps", cycleId: "bad" }), TypeError);
  // 2. The refusals, against the state read before sealing, before any seal: not-current, installation, incomplete, already staged.
  const steps = await initial(f.database, uuid(0xa1), { installationId: OTHER_INSTALLATION });
  const spy = new SpyCipher(f.cipher);
  assert.deepEqual(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }, { cipher: spy, context: context({ keyAlias: "synthetic-missing-key" }) }), refused("chain-incomplete"), "an unstaged current attempt");
  await stage(f, snapshotPage(steps, 1, 1, false), context({ installationId: OTHER_INSTALLATION }));
  assert.deepEqual(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }, { cipher: spy }), refused("installation-mismatch"), "installation before an unfinished chain");
  assert.deepEqual(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }, { cipher: spy, context: context({ installationId: OTHER_INSTALLATION }) }), refused("chain-incomplete"));
  await stage(f, snapshotPage(steps, 2, 1, true), context({ installationId: OTHER_INSTALLATION }));
  staged(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }, { context: context({ installationId: OTHER_INSTALLATION }) }));
  assert.deepEqual(await complete(f, { recordType: "steps", cycleId: uuid(0xa1) }, { cipher: spy }), refused("installation-mismatch"), "installation before already staged");
  assert.equal(spy.seals.length, 0);
  // 3. Sealing before the commit, and 4. the commit last.
  const weight = await initial(f.database, uuid(0xb1), { recordType: "weight" });
  await finishedSnapshot(f, weight);
  assert.deepEqual(await complete(f, { recordType: "weight", cycleId: uuid(0xb1) }, { database: failing(f.database, { mutation: 1 }), context: context({ keyAlias: "synthetic-missing-key" }) }),
    { outcome: "cipher-failed", failure: "installation-key-missing" });
  assert.deepEqual(await complete(f, { recordType: "weight", cycleId: uuid(0xb1) }, { database: failing(f.database, { mutation: 1 }) }), { outcome: "storage-failed", failure: "storage-failed" });
});

test("staging_only_seals", async (t) => {
  const f = await fixture(t);
  await finishedSnapshot(f, await initial(f.database, uuid(0xa1)));
  const guard = guarded(f.cipher);
  const results = [
    await stageSyncCompletion(f.database, guard.cipher, context({ keyAlias: "synthetic-missing-key" }), { recordType: "steps", cycleId: uuid(0xa1) }),
    await stageSyncCompletion(failing(f.database, { commit: true }), guard.cipher, context(), { recordType: "steps", cycleId: uuid(0xa1) }),
    await stageSyncCompletion(f.database, guard.cipher, context(), { recordType: "steps", cycleId: uuid(0xa1) }),
  ];
  assert.deepEqual(results.map((result) => result.outcome), ["cipher-failed", "storage-failed", "staged"]);
  assert.deepEqual(guard.forbidden, []);
});

test("the_module_uses_other_units_tables_only_through_their_functions", () => {
  const source = readFileSync(MODULE, "utf8");
  for (const table of ["outbox_messages", "outbox_high_water", "cycle_progress", "snapshot_attempts", "current_snapshot_attempts"]) {
    assert.doesNotMatch(source, new RegExp(`\\b${table}\\b`), table);
  }
  assert.match(source, /\bappendOutboxMessages\s*\(/);
  const specifiers = [...source.matchAll(/\b(?:from|import)\s*\(?\s*["']([^"']+)["']/g)].map((match) => match[1]);
  assert.deepEqual(specifiers.filter((specifier) => !/^(?:\.{1,2}\/|@activity-tracker-portal\/ingest-protocol$)/.test(specifier)), []);
  assert.doesNotMatch(source, /\b(?:Buffer|require|process)\b/);
});
