// Acceptance tests for send-outbox-message@1.
// Requirement numbers refer to packs/send-outbox-message-1.spec.md. Storage is the 3ug node:sqlite
// SenderDatabase substitute and the cipher is the 3ug NodeInstallationCipher. The transport, Portal, and
// notifier are synthetic fakes defined here; messages are staged through the published stage units or
// appended through stage-outbox-batch@1's Outbox.
import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import type { DatabaseSync } from "node:sqlite";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import type { TestContext } from "node:test";
import { chainStart, decodeMessage, encodeMessage, messageSha256 } from "@activity-tracker-portal/ingest-protocol";
import ts from "typescript";
import {
  DELIVERY_TIMEOUT_MS, MAX_RESPONSE_BYTES, OUTBOX_ROUTE, createOutboxSender, deliveryMigration,
} from "../src/delivery/send-outbox-message.ts";
import type {
  DeliveryNotifier, HeadBlock, OutboxRequest, OutboxSender, OutboxSenderOptions, OutboxTransport, SendResult, TransportResult,
} from "../src/delivery/send-outbox-message.ts";
import type { CipherResult, InstallationCipher, InstallationKeyStatus } from "../src/installation-cipher.ts";
import {
  appendOutboxMessages, lastOutboxSequence, outboxAad, outboxMessages, outboxMigration, stageOutboxBatch,
} from "../src/outbox/stage-outbox-batch.ts";
import type { ObservedPage, OutboxKind, StagingContext } from "../src/outbox/stage-outbox-batch.ts";
import { checkpointMigration, stageSyncCheckpoint } from "../src/outbox/stage-sync-checkpoint.ts";
import { completionMigration, stageSyncCompletion } from "../src/outbox/stage-sync-completion.ts";
import { schedulingMigration } from "../src/scheduling/schedule-background-polling.ts";
import { beginInitialSnapshot, snapshotAttemptsMigration } from "../src/snapshot/begin-initial-snapshot.ts";
import type { SnapshotAttempt } from "../src/snapshot/begin-initial-snapshot.ts";
import type { DatabaseRow, DatabaseValue, SenderDatabase } from "../src/storage/database.ts";
import { migrate } from "../src/storage/migrations.ts";
import { storageLane } from "../src/storage/storage-lane.ts";
import { syncMigration } from "../src/sync/coordinate-sender-sync.ts";
import { openNodeDatabase } from "./support/node-database.ts";
import { NodeInstallationCipher } from "./support/node-installation-cipher.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const MODULE = join(HERE, "../src/delivery/send-outbox-message.ts");
const MODULE_DIRECTORY = dirname(MODULE);
const INSTALLATION = "00000000-0000-4000-8000-000000000001";
const OTHER_INSTALLATION = "00000000-0000-4000-8000-000000000002";
const KEY_ALIAS = "synthetic-installation-key";
const PORTAL_URL = "http://portal.synthetic.invalid:8443";
const URL = "http://portal.synthetic.invalid:8443/v1/outbox-messages";
const SENTINEL = "SENTINEL";
const MIGRATIONS = [
  snapshotAttemptsMigration, outboxMigration, completionMigration, checkpointMigration, syncMigration, schedulingMigration, deliveryMigration,
];
const NOW = Date.UTC(2026, 9, 16, 9, 0, 0, 0);
const KINDS: readonly OutboxKind[] = ["ingest-batch", "snapshot-completion", "change-completion", "sync-checkpoint", "capability-acknowledgement"];
const OTHER_TABLES = [
  "snapshot_attempts", "current_snapshot_attempts", "outbox_high_water", "cycle_progress", "cycle_completions",
  "run_checkpoints", "sync_types", "sync_blocks", "sync_runs", "sync_run_types", "sync_run_cycles", "scheduling_state",
];
const utf8 = (text: string) => new TextEncoder().encode(text);
const uuid = (n: number) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, "0")}`;
const settle = (ms = 20) => new Promise<void>((resolve) => { setTimeout(resolve, ms); });

// Synthetic messages and Portal answers, built with the protocol library.

/** A valid Outbox Message of `kind` at `sequence`. */
function message(kind: OutboxKind, sequence: number, installation = INSTALLATION): Uint8Array {
  const common = { wire_version: 1, kind, sender_installation: installation, sequence, capability_generation: 1 };
  const cycle = uuid(0xc000 + sequence);
  switch (kind) {
    case "capability-acknowledgement":
      return encodeMessage(common);
    case "ingest-batch":
      return encodeMessage({
        ...common, record_type: "steps", projection_schema_version: 1, cycle_kind: "change-cycle", cycle_id: cycle,
        source_page_index: 1, fragment_index: 1, previous_chain: chainStart(cycle),
        items: [{ kind: "projection", data: Buffer.from(`{"synthetic":${sequence}}`).toString("base64") }],
      });
    case "snapshot-completion":
      return encodeMessage({
        ...common, snapshot_kind: "initial", record_type: "steps", cycle_id: cycle,
        page_count: 0, fragment_count: 0, record_count: 0, final_chain: chainStart(cycle),
      });
    case "change-completion":
      return encodeMessage({
        ...common, record_type: "steps", cycle_id: cycle, page_count: 0, fragment_count: 0, record_count: 0, final_chain: chainStart(cycle),
      });
    case "sync-checkpoint":
      return encodeMessage({
        ...common, run_sequence: sequence, trigger: "background", started_at: "2026-10-16T08:59:00.000Z",
        ended_at: "2026-10-16T08:59:30.000Z", background_scheduling: "available", record_types: [],
        outbox_messages_before_checkpoint: 0, outbox_bytes_before_checkpoint: 0,
      });
  }
}

function identity(bytes: Uint8Array): { sender_installation: string; sequence: number; message_sha256: string } {
  const decoded = decodeMessage(bytes);
  return { sender_installation: decoded.sender_installation as string, sequence: decoded.sequence as number, message_sha256: messageSha256(bytes) };
}

const receipt = (bytes: Uint8Array, overrides: Record<string, unknown> = {}) =>
  encodeMessage({ wire_version: 1, kind: "delivery-receipt", ...identity(bytes), ...overrides });
const retryable = (reason: string) => encodeMessage({ wire_version: 1, kind: "retryable-failure", reason });
const deliveryBlock = (bytes: Uint8Array, failure: string, overrides: Record<string, unknown> = {}) =>
  encodeMessage({ wire_version: 1, kind: "delivery-block", failure, ...identity(bytes), ...overrides });

// Seams

type Respond = (request: OutboxRequest) => unknown;

/** Records each request; `respond` answers it, and `gate` holds every answer until opened. */
class FakeTransport implements OutboxTransport {
  readonly requests: OutboxRequest[] = [];
  respond: Respond = (request) => ({ outcome: "response", body: receipt(request.body) });
  gate: Promise<void> | null = null;
  inFlight = 0;
  maxInFlight = 0;
  private arrivals: (() => void)[] = [];

  arrival(): Promise<void> {
    return new Promise<void>((resolve) => { this.arrivals.push(resolve); });
  }

  // An arrow property, so a proxy or spread sees only the `send` access.
  readonly send = (request: OutboxRequest): Promise<TransportResult> => {
    this.requests.push({ ...request, headers: { ...request.headers }, body: new Uint8Array(request.body) });
    this.inFlight++;
    this.maxInFlight = Math.max(this.maxInFlight, this.inFlight);
    for (const arrive of this.arrivals.splice(0)) arrive();
    const answer = this.respond(request);
    const done = () => { this.inFlight--; };
    if (answer instanceof Error) { done(); throw answer; }
    return (this.gate ?? Promise.resolve()).then(() => answer).finally(done) as Promise<TransportResult>;
  };

  bodies(): Uint8Array[] { return this.requests.map((request) => request.body); }
}

class FakeNotifier implements DeliveryNotifier {
  readonly calls: unknown[][] = [];
  readonly delivered: string[] = [];
  failures = 0;
  synchronously = false;
  gate: Promise<void> | null = null;
  onNotify: (() => void) | null = null;
  private arrivals: (() => void)[] = [];

  arrival(): Promise<void> {
    return new Promise<void>((resolve) => { this.arrivals.push(resolve); });
  }

  notify(...args: [string]): Promise<void> {
    this.calls.push(args);
    this.onNotify?.();
    for (const arrive of this.arrivals.splice(0)) arrive();
    if (this.failures > 0) {
      this.failures--;
      const error = new Error(`${SENTINEL} synthetic notification failure`);
      if (this.synchronously) throw error;
      return Promise.reject(error);
    }
    return (this.gate ?? Promise.resolve()).then(() => { this.delivered.push(args[0]); });
  }
}

/** Records every cipher call; `openScript` replaces the numbered open call (from 1). */
class SpyCipher implements InstallationCipher {
  readonly calls: { method: string; alias?: string; aad?: Uint8Array }[] = [];
  readonly openScript = new Map<number, { returns?: unknown; throws?: unknown }>();
  private opens = 0;
  private readonly inner: InstallationCipher;
  constructor(inner: InstallationCipher) { this.inner = inner; }
  async open(alias: string, sealed: Uint8Array, aad: Uint8Array): Promise<CipherResult<Uint8Array>> {
    this.calls.push({ method: "open", alias, aad: new Uint8Array(aad) });
    const entry = this.openScript.get(++this.opens);
    if (entry && "throws" in entry) throw entry.throws;
    if (entry && "returns" in entry) return entry.returns as CipherResult<Uint8Array>;
    return this.inner.open(alias, sealed, aad);
  }
  seal(alias: string, plaintext: Uint8Array, aad: Uint8Array) { this.calls.push({ method: "seal" }); return this.inner.seal(alias, plaintext, aad); }
  create(alias: string) { this.calls.push({ method: "create" }); return this.inner.create(alias); }
  keyStatus(alias: string): Promise<InstallationKeyStatus> { this.calls.push({ method: "keyStatus" }); return this.inner.keyStatus(alias); }
  delete(alias: string) { this.calls.push({ method: "delete" }); return this.inner.delete(alias); }
  isDeviceSecure() { this.calls.push({ method: "isDeviceSecure" }); return this.inner.isDeviceSecure(); }
}

async function fixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const path = join(directory, "synthetic.sqlite");
  let database = await openNodeDatabase(path);
  t.after(async () => {
    try { await database.close(); } catch { /* a test may leave the old process's database closed */ }
    rmSync(directory, { recursive: true, force: true });
  });
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 7 });
  const base = new NodeInstallationCipher();
  assert.equal((await base.create(KEY_ALIAS)).status, "ok");
  const f = {
    base,
    cipher: new SpyCipher(base),
    transport: new FakeTransport(),
    notifier: new FakeNotifier(),
    wall: NOW,
    now: (): number => f.wall,
    /** The plaintext of each appended or staged message, by installation and sequence. */
    plaintexts: new Map<string, Uint8Array>(),
    get database(): SenderDatabase { return database; },
    make(overrides: Partial<OutboxSenderOptions> = {}): OutboxSender {
      return createOutboxSender({
        database, cipher: f.cipher, transport: f.transport, notifier: f.notifier, now: f.now,
        context: { installationId: INSTALLATION, keyAlias: KEY_ALIAS, portalUrl: PORTAL_URL }, ...overrides,
      });
    },
    /**
     * A second connection to the same file. While failing, SQLite itself rejects its writes, so the failure
     * does not depend on how a write is spelled or issued. It starts failing.
     */
    async writeFailing(): Promise<{ readonly database: SenderDatabase; readonly failing: (on: boolean) => void }> {
      let handle!: DatabaseSync;
      const second = await openNodeDatabase(path, (opened) => { handle = opened; });
      t.after(async () => { try { await second.close(); } catch { /* already closed */ } });
      const failing = (on: boolean) => { handle.exec(`PRAGMA query_only = ${on ? "ON" : "OFF"}`); };
      failing(true);
      return { database: second, failing };
    },
    /** A new process on the same file; the old instance is never used again. */
    async reopen(): Promise<void> { await database.close(); database = await openNodeDatabase(path); f.sender = f.make(); },
    sender: undefined as unknown as OutboxSender,
  };
  f.sender = f.make();
  return f;
}
type Fixture = Awaited<ReturnType<typeof fixture>>;

/** Seal `plaintext` under the row's own AAD and append it as the installation's next row. */
async function appendRaw(f: Fixture, kind: OutboxKind, plaintext: Uint8Array, options: { installation?: string; messageBytes?: number } = {}): Promise<number> {
  const installation = options.installation ?? INSTALLATION;
  const last = await lastOutboxSequence(f.database, installation);
  const sealed = await f.base.seal(KEY_ALIAS, plaintext, outboxAad(installation, last + 1));
  assert.equal(sealed.status, "ok");
  assert.equal(await f.database.transaction((tx) => appendOutboxMessages(tx, installation, last, [{
    kind, sealed: (sealed as { value: Uint8Array }).value, messageBytes: options.messageBytes ?? plaintext.length,
  }])), true);
  f.plaintexts.set(`${installation}:${last + 1}`, plaintext);
  return last + 1;
}

/** Append valid messages of the given kinds; returns their plaintexts in order. */
async function append(f: Fixture, kinds: readonly OutboxKind[], installation = INSTALLATION): Promise<Uint8Array[]> {
  const result: Uint8Array[] = [];
  for (const kind of kinds) {
    const sequence = (await lastOutboxSequence(f.database, installation)) + 1;
    const bytes = message(kind, sequence, installation);
    await appendRaw(f, kind, bytes, { installation });
    result.push(bytes);
  }
  return result;
}

const sent = (bytes: Uint8Array): OutboxRequest => ({
  method: "POST", url: URL, headers: { "content-type": "application/json" }, body: bytes, timeoutMs: 60_000, maxResponseBytes: 4096,
});
const respondWith = (body: Uint8Array): Respond => () => ({ outcome: "response", body });

/**
 * The whole database: the schema version, every schema object, and every row of every table,
 * including sealed bytes, so no unexpected table or write can go unnoticed.
 */
async function contents(database: SenderDatabase) {
  const schema = await database.query("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name");
  const tables: Record<string, unknown> = {};
  for (const { name } of schema.filter((entry) => entry.type === "table")) {
    tables[name as string] = await database.query(`SELECT * FROM "${name as string}" ORDER BY rowid`);
  }
  return { version: await database.query("PRAGMA user_version"), schema, tables };
}
type Contents = Awaited<ReturnType<typeof contents>>;

/** `snapshot` with only the given tables replaced. */
function withTables(snapshot: Contents, tables: Record<string, unknown>): Contents {
  return { ...snapshot, tables: { ...snapshot.tables, ...tables } };
}

async function outboxSequences(database: SenderDatabase, installation = INSTALLATION): Promise<number[]> {
  return (await outboxMessages(database)).filter((row) => row.installationId === installation).map((row) => row.sequence);
}

async function blockRows(database: SenderDatabase): Promise<DatabaseRow[]> {
  return database.query("SELECT * FROM outbox_head_block ORDER BY rowid");
}

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

/** Logs each top-level operation. */
function logging(database: SenderDatabase, operations: string[]): SenderDatabase {
  return {
    execute: (sql, parameters) => { operations.push(sql); return database.execute(sql, parameters); },
    query: (sql, parameters) => { operations.push(sql); return database.query(sql, parameters); },
    transaction: (fn) => { operations.push("transaction"); return database.transaction(fn); },
    close: () => database.close(),
  };
}

const block = (sequence: number, failure: string, firstSeenMs: number, lastSeenMs: number, count: number, messageSha256: string | null, origin: "portal" | "sender" = "portal"): HeadBlock =>
  ({ sequence, origin, failure, messageSha256, firstSeenMs, lastSeenMs, count });

// R2 — Caller errors

test("malformed_options_are_caller_errors", async (t) => {
  const f = await fixture(t);
  await append(f, ["ingest-batch"]);
  const operations: string[] = [];
  const database = logging(f.database, operations);
  const context = { installationId: INSTALLATION, keyAlias: KEY_ALIAS, portalUrl: PORTAL_URL };
  const without = <T extends object>(value: T, name: string): T => {
    const copy: Record<string, unknown> = {};
    for (const key of Object.keys(value)) if (key !== name) copy[key] = (value as Record<string, unknown>)[key];
    return copy as T;
  };
  const cases: unknown[] = [
    null, 7, "options",
    { database: without(database, "transaction") },
    { database: without(database, "query") },
    { cipher: { seal: f.cipher.seal.bind(f.cipher) } },
    { transport: {} },
    { transport: { send: "send" } },
    { notifier: {} },
    { now: NOW },
    { context: undefined },
    { context: { ...context, installationId: "00000000-0000-4000-8000-00000000000A" } },
    { context: { ...context, installationId: "not-a-uuid" } },
    { context: { ...context, installationId: 1 } },
    { context: { ...context, keyAlias: "" } },
    { context: { ...context, keyAlias: 1 } },
    { context: { ...context, portalUrl: "http://portal.synthetic.invalid:8443/" } },
    { context: { ...context, portalUrl: "http://portal.synthetic.invalid/api" } },
    { context: { ...context, portalUrl: "ftp://portal.synthetic.invalid" } },
    { context: { ...context, portalUrl: "http://Portal.synthetic.invalid" } },
    { context: { ...context, portalUrl: "http://portal.synthetic.invalid?x=1" } },
    { context: without(context, "portalUrl") },
  ];
  for (const entry of cases) {
    const options = entry === null || typeof entry !== "object" ? entry : { database, cipher: f.cipher, transport: f.transport, notifier: f.notifier, now: f.now, context, ...entry };
    assert.throws(() => createOutboxSender(options as OutboxSenderOptions), TypeError, JSON.stringify(entry));
  }
  assert.deepEqual(operations, []);
  assert.deepEqual(f.transport.requests, []);
  assert.deepEqual(f.notifier.calls, []);
  assert.deepEqual(f.cipher.calls, []);
});

test("an_invalid_clock_reading_is_a_caller_error", async (t) => {
  const f = await fixture(t);
  await append(f, ["ingest-batch"]);
  const before = await contents(f.database);
  const operations: string[] = [];
  const database = logging(f.database, operations);
  for (const reading of [Number.NaN, 1.5, 2 ** 53, -Infinity, "1", null, undefined, 1n]) {
    let reads = 0;
    const sender = f.make({ database, now: () => { reads++; return reading as unknown as number; } });
    await assert.rejects(sender.sendNext(), TypeError, String(reading));
    assert.equal(reads, 1);
  }
  assert.deepEqual(operations, []);
  assert.deepEqual(f.transport.requests, []);
  assert.deepEqual(f.notifier.calls, []);
  assert.deepEqual(f.cipher.calls, []);
  assert.deepEqual(await contents(f.database), before);
  // A valid reading is taken once per attempt, before anything else.
  const order: string[] = [];
  const sender = f.make({ database: logging(f.database, order), now: () => { order.push("now"); return NOW; } });
  assert.equal((await sender.sendNext()).outcome, "delivered");
  assert.equal(order[0], "now");
  assert.equal(order.filter((entry) => entry === "now").length, 1);
});

// R3 — The oldest message, once per attempt

test("an_empty_outbox_sends_nothing", async (t) => {
  const f = await fixture(t);
  const before = await contents(f.database);
  assert.deepEqual(await f.sender.sendNext(), { outcome: "empty" });
  assert.deepEqual(f.transport.requests, []);
  assert.deepEqual(f.cipher.calls, []);
  assert.deepEqual(await contents(f.database), before);
});

test("the_oldest_message_is_sent_exactly", async (t) => {
  const f = await fixture(t);
  assert.equal(OUTBOX_ROUTE, "/v1/outbox-messages");
  assert.equal(DELIVERY_TIMEOUT_MS, 60_000);
  assert.equal(MAX_RESPONSE_BYTES, 4096);
  const [first, second] = await append(f, ["capability-acknowledgement", "ingest-batch", "sync-checkpoint"]);
  f.transport.respond = respondWith(retryable("temporarily-unavailable"));
  for (let attempt = 1; attempt <= 2; attempt++) {
    assert.deepEqual(await f.sender.sendNext(), { outcome: "retryable", sequence: 1, failure: "temporarily-unavailable" });
    assert.equal(f.transport.requests.length, attempt);
  }
  // Exactly the staged bytes, uncompressed, with nothing added to the request.
  assert.deepEqual(f.transport.requests, [sent(first), sent(first)]);
  for (const request of f.transport.requests) assert.deepEqual(Object.keys(request.headers), ["content-type"]);
  assert.deepEqual(f.cipher.calls, [
    { method: "open", alias: KEY_ALIAS, aad: outboxAad(INSTALLATION, 1) }, { method: "open", alias: KEY_ALIAS, aad: outboxAad(INSTALLATION, 1) },
  ]);
  f.transport.respond = (request) => ({ outcome: "response", body: receipt(request.body) });
  assert.deepEqual(await f.sender.sendNext(), { outcome: "delivered", sequence: 1, kind: "capability-acknowledgement" });
  assert.deepEqual(await f.sender.sendNext(), { outcome: "delivered", sequence: 2, kind: "ingest-batch" });
  assert.deepEqual(f.transport.requests.slice(2), [sent(first), sent(second)]);
});

test("other_installations_are_never_sent", async (t) => {
  const f = await fixture(t);
  await append(f, ["ingest-batch", "sync-checkpoint"], OTHER_INSTALLATION);
  const before = await contents(f.database);
  assert.deepEqual(await f.sender.sendNext(), { outcome: "empty" });
  assert.deepEqual(f.transport.requests, []);
  assert.deepEqual(await contents(f.database), before);
  const [own] = await append(f, ["capability-acknowledgement"]);
  assert.deepEqual(await f.sender.sendNext(), { outcome: "delivered", sequence: 1, kind: "capability-acknowledgement" });
  assert.deepEqual(await f.sender.sendNext(), { outcome: "empty" });
  assert.deepEqual(f.transport.bodies(), [own]);
  assert.deepEqual(await outboxSequences(f.database, OTHER_INSTALLATION), [1, 2]);
});

// R4 — Receipt-bound removal

test("a_matching_receipt_removes_exactly_the_head", async (t) => {
  const f = await fixture(t);
  await append(f, ["ingest-batch"], OTHER_INSTALLATION);
  await append(f, ["ingest-batch", "change-completion", "sync-checkpoint"]);
  const before = await contents(f.database);
  let open!: () => void;
  f.transport.gate = new Promise<void>((resolve) => { open = resolve; });
  const entered = f.transport.arrival();
  const attempt = f.sender.sendNext();
  try {
    await entered;
    // Nothing is removed, or even touched, while the Portal has not answered.
    assert.deepEqual(await storageLane(f.database)(() => contents(f.database)), before);
  } finally { open(); }
  assert.deepEqual(await attempt, { outcome: "delivered", sequence: 1, kind: "ingest-batch" });
  const rows = (before.tables.outbox_messages as DatabaseRow[]).filter((row) => !(row.installation_id === INSTALLATION && row.sequence === 1));
  assert.deepEqual(await contents(f.database), withTables(before, { outbox_messages: rows }));
});

function stagingContext(): StagingContext {
  return {
    installationId: INSTALLATION, keyAlias: KEY_ALIAS,
    generation: { generation: 1, maxRecordCount: 4, maxMessageBytes: 65_536, projectionSchemas: { steps: [1] } },
  };
}

async function initialAttempt(database: SenderDatabase): Promise<SnapshotAttempt> {
  const issuer = { async issue() { return { outcome: "issued" as const, changesToken: "synthetic-changes-token-0", unreadSinceMs: 7 }; } };
  const result = await beginInitialSnapshot(database, issuer, { recordType: "steps", installationId: INSTALLATION },
    { randomUUID: () => uuid(0xa1), now: () => NOW });
  assert.equal(result.outcome, "begun", JSON.stringify(result));
  return (result as { attempt: SnapshotAttempt }).attempt;
}

test("every_kind_is_delivered_in_sequence", async (t) => {
  const f = await fixture(t);
  const staging = stagingContext();
  const attempt = await initialAttempt(f.database);
  const snapshotPage: ObservedPage = {
    source: "snapshot", recordType: "steps", cycleId: attempt.snapshotId, projectionSchemaVersion: 1, pageIndex: 1,
    readFrom: null, projections: [utf8('{"synthetic":1}')], nextPosition: utf8("position-1"), exhausted: true,
  };
  const changePage: ObservedPage = {
    source: "change", recordType: "steps", cycleId: uuid(0xc1), projectionSchemaVersion: 1, pageIndex: 1, readFrom: attempt.changesToken,
    items: [{ kind: "retraction", sourceIdentity: "synthetic-source-1" }, { kind: "projection", bytes: utf8('{"synthetic":2}') }],
    nextChangesToken: "synthetic-changes-token-1", nextUnreadSinceMs: 101, hasMore: false,
  };
  assert.equal((await stageOutboxBatch(f.database, f.base, staging, snapshotPage)).outcome, "staged");
  assert.equal((await stageSyncCompletion(f.database, f.base, staging, { recordType: "steps", cycleId: attempt.snapshotId })).outcome, "staged");
  assert.equal((await stageOutboxBatch(f.database, f.base, staging, changePage)).outcome, "staged");
  assert.equal((await stageSyncCompletion(f.database, f.base, staging, { recordType: "steps", cycleId: uuid(0xc1) })).outcome, "staged");
  assert.equal((await stageSyncCheckpoint(f.database, f.base, staging, {
    runSequence: 1, trigger: "manual", startedAtMs: NOW, endedAtMs: NOW + 30_000, elapsedMs: 30_000, backgroundScheduling: "available",
    recordTypes: [{ recordType: "steps", outcome: "completed", sourceDataStaged: true }],
  })).outcome, "staged");
  await append(f, ["capability-acknowledgement"]);
  // The staged plaintexts, opened with their own authenticated data.
  const staged: Uint8Array[] = [];
  for (const row of await outboxMessages(f.database)) {
    const opened = await f.base.open(KEY_ALIAS, row.sealed, outboxAad(row.installationId, row.sequence));
    assert.equal(opened.status, "ok");
    staged.push((opened as { value: Uint8Array }).value);
  }
  const results: SendResult[] = [];
  for (let attempt = 0; attempt < 10; attempt++) {
    const result = await f.sender.sendNext();
    if (result.outcome === "empty") break;
    results.push(result);
  }
  assert.deepEqual(results, ["ingest-batch", "snapshot-completion", "ingest-batch", "change-completion", "sync-checkpoint", "capability-acknowledgement"]
    .map((kind, index) => ({ outcome: "delivered", sequence: index + 1, kind })));
  assert.deepEqual(f.transport.requests, staged.map(sent));
  assert.deepEqual(await outboxMessages(f.database), []);
});

test("removal_keeps_the_high_water_mark", async (t) => {
  const f = await fixture(t);
  await append(f, ["ingest-batch", "change-completion"]);
  assert.equal((await f.sender.sendNext()).outcome, "delivered");
  assert.equal((await f.sender.sendNext()).outcome, "delivered");
  assert.deepEqual(await f.sender.sendNext(), { outcome: "empty" });
  assert.equal(await lastOutboxSequence(f.database, INSTALLATION), 2);
  const [third] = await append(f, ["sync-checkpoint"]);
  assert.deepEqual(await f.sender.sendNext(), { outcome: "delivered", sequence: 3, kind: "sync-checkpoint" });
  assert.deepEqual(f.transport.bodies().at(-1), third);
});

// R5 — Everything else retains identical bytes

test("no_response_keeps_identical_bytes", async (t) => {
  const f = await fixture(t);
  const [first] = await append(f, ["ingest-batch", "sync-checkpoint"]);
  const before = await contents(f.database);
  const answers: Respond[] = [
    () => ({ outcome: "failed" }),
    () => Promise.reject(new Error(`${SENTINEL} synthetic network failure`)),
    () => new Error(`${SENTINEL} synthetic synchronous failure`),
    () => Promise.reject(new TypeError(`${SENTINEL} synthetic fetch failure`)),
    () => undefined,
    () => ({ outcome: "response", body: "not bytes" }),
    () => ({ outcome: "response" }),
    () => ({ outcome: "timeout" }),
  ];
  for (const [index, respond] of answers.entries()) {
    f.transport.respond = respond;
    assert.deepEqual(await f.sender.sendNext(), { outcome: "retryable", sequence: 1, failure: "transport-failed" }, String(index));
    assert.deepEqual(await contents(f.database), before);
  }
  f.transport.respond = (request) => ({ outcome: "response", body: receipt(request.body) });
  assert.deepEqual(await f.sender.sendNext(), { outcome: "delivered", sequence: 1, kind: "ingest-batch" });
  assert.deepEqual(f.transport.bodies(), Array(answers.length + 1).fill(first));
  assert.deepEqual(f.notifier.calls, []);
});

test("mismatched_answers_keep_the_head", async (t) => {
  const f = await fixture(t);
  const [first] = await append(f, ["ingest-batch", "sync-checkpoint"]);
  const own = receipt(first);
  const other = message("ingest-batch", 1, OTHER_INSTALLATION);
  const bodies: Uint8Array[] = [
    new Uint8Array(0),
    utf8("<html>synthetic proxy page</html>"),
    utf8(JSON.stringify(JSON.parse(new TextDecoder().decode(own)), null, 1)),
    new Uint8Array([...own, 0x20]),
    receipt(first, { sender_installation: OTHER_INSTALLATION }),
    receipt(first, { sequence: 2 }),
    receipt(first, { message_sha256: messageSha256(other) }),
    deliveryBlock(first, "sequence-content-mismatch", { sequence: 2 }),
    deliveryBlock(first, "sequence-content-mismatch", { message_sha256: messageSha256(other) }),
    encodeMessage({ wire_version: 1, kind: "delivery-block", failure: "malformed-message", message_sha256: messageSha256(other) }),
    encodeMessage({ wire_version: 1, kind: "enrollment-confirmation", enrollment_code: "AAAAAAAAAAAAAAAA", sender_installation: INSTALLATION,
      fingerprint: messageSha256(utf8(INSTALLATION)) }),
    first,
    new Uint8Array(MAX_RESPONSE_BYTES + 1).fill(0x20),
  ];
  // A head block, once recorded, is left unchanged by every such answer.
  f.transport.respond = respondWith(deliveryBlock(first, "unsupported-wire-protocol"));
  assert.equal((await f.sender.sendNext()).outcome, "blocked");
  const before = await contents(f.database);
  for (const [index, body] of bodies.entries()) {
    f.wall += 1000;
    f.transport.respond = respondWith(body);
    assert.deepEqual(await f.sender.sendNext(), { outcome: "retryable", sequence: 1, failure: "receipt-mismatch" }, String(index));
    assert.deepEqual(await contents(f.database), before, String(index));
  }
  assert.deepEqual(f.transport.bodies(), Array(bodies.length + 1).fill(first));
  assert.deepEqual(await f.sender.headBlock(), block(1, "unsupported-wire-protocol", NOW, NOW, 1, messageSha256(first)));
});

test("retryable_failures_keep_the_head", async (t) => {
  const f = await fixture(t);
  const [first] = await append(f, ["change-completion"]);
  const before = await contents(f.database);
  for (const reason of ["temporarily-unavailable", "storage-reserve-exhausted", "temporarily-unavailable"]) {
    f.transport.respond = respondWith(retryable(reason));
    assert.deepEqual(await f.sender.sendNext(), { outcome: "retryable", sequence: 1, failure: reason });
    assert.deepEqual(await contents(f.database), before);
  }
  assert.deepEqual(f.transport.bodies(), [first, first, first]);
  assert.deepEqual(f.notifier.calls, []);
});

// R6 — Portal Delivery Blocks

test("a_delivery_block_keeps_the_head_and_records_it", async (t) => {
  const f = await fixture(t);
  const [first] = await append(f, ["ingest-batch", "sync-checkpoint"]);
  const before = await contents(f.database);
  f.transport.respond = respondWith(deliveryBlock(first, "sequence-content-mismatch"));
  assert.deepEqual(await f.sender.sendNext(), { outcome: "blocked", sequence: 1, failure: "sequence-content-mismatch" });
  const recorded = { installation_id: INSTALLATION, sequence: 1, origin: "portal", failure: "sequence-content-mismatch",
    message_sha256: messageSha256(first), first_seen_ms: NOW, last_seen_ms: NOW, count: 1, notified: 1 };
  assert.deepEqual(await contents(f.database), withTables(before, { outbox_head_block: [recorded] }));
  assert.deepEqual(await f.sender.headBlock(), block(1, "sequence-content-mismatch", NOW, NOW, 1, messageSha256(first)));
  // A repeat takes its time, count, and latest failure. A block that names no sequence still answers the head by its digest.
  f.wall = NOW + 5_000;
  f.transport.respond = respondWith(encodeMessage({ wire_version: 1, kind: "delivery-block", failure: "malformed-message", message_sha256: messageSha256(first) }));
  assert.deepEqual(await f.sender.sendNext(), { outcome: "blocked", sequence: 1, failure: "malformed-message" });
  assert.deepEqual(await f.sender.headBlock(), block(1, "malformed-message", NOW, NOW + 5_000, 2, messageSha256(first)));
  assert.deepEqual(await outboxSequences(f.database), [1, 2]);
  assert.deepEqual(f.notifier.calls, [["sequence-content-mismatch"]]);
});

test("a_blocked_head_is_retried_and_never_skipped", async (t) => {
  const f = await fixture(t);
  const [first] = await append(f, ["snapshot-completion", "ingest-batch", "sync-checkpoint"]);
  f.transport.respond = respondWith(deliveryBlock(first, "completion-chain-mismatch"));
  for (let attempt = 1; attempt <= 4; attempt++) {
    f.wall = NOW + attempt;
    assert.deepEqual(await f.sender.sendNext(), { outcome: "blocked", sequence: 1, failure: "completion-chain-mismatch" });
  }
  // Lost responses and retryable answers leave the block and send the head again.
  f.transport.respond = () => ({ outcome: "failed" });
  assert.equal((await f.sender.sendNext()).outcome, "retryable");
  f.transport.respond = respondWith(retryable("temporarily-unavailable"));
  assert.equal((await f.sender.sendNext()).outcome, "retryable");
  assert.deepEqual(f.transport.bodies(), Array(6).fill(first));
  assert.deepEqual(await f.sender.headBlock(), block(1, "completion-chain-mismatch", NOW + 1, NOW + 4, 4, messageSha256(first)));
  assert.deepEqual(await outboxSequences(f.database), [1, 2, 3]);
});

test("a_receipt_clears_the_head_block", async (t) => {
  const f = await fixture(t);
  // Another installation's head block at the same sequence is never cleared by this one's receipt.
  const [foreign] = await append(f, ["ingest-batch"], OTHER_INSTALLATION);
  const other = f.make({ context: { installationId: OTHER_INSTALLATION, keyAlias: KEY_ALIAS, portalUrl: PORTAL_URL } });
  f.transport.respond = respondWith(deliveryBlock(foreign, "unsupported-source-projection-schema"));
  assert.equal((await other.sendNext()).outcome, "blocked");
  const [first, second] = await append(f, ["ingest-batch", "change-completion"]);
  const before = await contents(f.database);
  f.transport.respond = respondWith(deliveryBlock(first, "unsupported-source-projection-schema"));
  assert.equal((await f.sender.sendNext()).outcome, "blocked");
  // Corrected Portal code accepts the identical retry.
  f.transport.respond = (request) => ({ outcome: "response", body: receipt(request.body) });
  assert.deepEqual(await f.sender.sendNext(), { outcome: "delivered", sequence: 1, kind: "ingest-batch" });
  assert.equal(await f.sender.headBlock(), null);
  const rows = (before.tables.outbox_messages as DatabaseRow[]).filter((row) => !(row.installation_id === INSTALLATION && row.sequence === 1));
  assert.deepEqual(await contents(f.database), withTables(before, { outbox_messages: rows }));
  assert.deepEqual(await f.sender.sendNext(), { outcome: "delivered", sequence: 2, kind: "change-completion" });
  assert.deepEqual(f.transport.bodies(), [foreign, first, first, second]);
  assert.deepEqual(await other.headBlock(), block(1, "unsupported-source-projection-schema", NOW, NOW, 1, messageSha256(foreign)));
});

test("a_repeated_block_takes_the_latest_answer", async (t) => {
  const f = await fixture(t);
  const [first] = await append(f, ["ingest-batch", "sync-checkpoint"]);
  f.notifier.failures = 1;
  f.transport.respond = respondWith(deliveryBlock(first, "unsupported-wire-protocol"));
  assert.deepEqual(await f.sender.sendNext(), { outcome: "blocked", sequence: 1, failure: "unsupported-wire-protocol" });
  assert.deepEqual(await f.sender.headBlock(), block(1, "unsupported-wire-protocol", NOW, NOW, 1, messageSha256(first)));
  // The same head then cannot be opened: the block takes the latest origin, failure, and digest.
  const [row] = await outboxMessages(f.database);
  const sealed = new Uint8Array(row.sealed);
  sealed[20] ^= 0xff;
  await f.database.execute("UPDATE outbox_messages SET sealed = ? WHERE installation_id = ? AND sequence = 1", [sealed, INSTALLATION]);
  f.wall = NOW + 1_000;
  assert.deepEqual(await f.sender.sendNext(), { outcome: "blocked", sequence: 1, failure: "head-unreadable" });
  assert.deepEqual(await f.sender.headBlock(), block(1, "head-unreadable", NOW, NOW + 1_000, 2, null, "sender"));
  // The onset's retried notification names the block's latest failure.
  assert.deepEqual(f.notifier.calls, [["unsupported-wire-protocol"], ["head-unreadable"]]);
  assert.deepEqual(f.notifier.delivered, ["head-unreadable"]);
});

// R7 — Local head checks

test("an_unavailable_keystore_is_retryable", async (t) => {
  const f = await fixture(t);
  const [first] = await append(f, ["ingest-batch"]);
  const before = await contents(f.database);
  f.base.setUnavailable(true);
  assert.deepEqual(await f.sender.sendNext(), { outcome: "retryable", sequence: 1, failure: "keystore-unavailable" });
  f.base.setUnavailable(false);
  // A cipher that throws or answers malformed reads as an unavailable Keystore.
  f.cipher.openScript.set(2, { throws: new Error(`${SENTINEL} synthetic Keystore failure`) });
  f.cipher.openScript.set(3, { returns: { status: "ok", value: "not bytes" } });
  f.cipher.openScript.set(4, { returns: { status: "error", reason: 7 } });
  f.cipher.openScript.set(5, { returns: undefined });
  for (let attempt = 2; attempt <= 5; attempt++) {
    assert.deepEqual(await f.sender.sendNext(), { outcome: "retryable", sequence: 1, failure: "keystore-unavailable" }, String(attempt));
  }
  assert.deepEqual(f.transport.requests, []);
  assert.deepEqual(await contents(f.database), before);
  assert.deepEqual(await f.sender.sendNext(), { outcome: "delivered", sequence: 1, kind: "ingest-batch" });
  assert.deepEqual(f.transport.bodies(), [first]);
});

test("an_unusable_key_fails_without_blocking", async (t) => {
  const f = await fixture(t);
  await append(f, ["ingest-batch", "sync-checkpoint"]);
  const before = await contents(f.database);
  const missing = f.make({ cipher: new NodeInstallationCipher() });
  assert.deepEqual(await missing.sendNext(), { outcome: "cipher-failed", sequence: 1, failure: "missing-key" });
  f.base.invalidate(KEY_ALIAS);
  assert.deepEqual(await f.sender.sendNext(), { outcome: "cipher-failed", sequence: 1, failure: "unusable-key" });
  assert.equal(await f.sender.headBlock(), null);
  assert.deepEqual(f.transport.requests, []);
  assert.deepEqual(f.notifier.calls, []);
  assert.deepEqual(await contents(f.database), before);
});

test("an_unreadable_head_is_a_local_block", async (t) => {
  for (const corrupt of [(sealed: Uint8Array) => { sealed[20] ^= 0xff; }, (sealed: Uint8Array) => { sealed[0] = 2; }]) {
    const f = await fixture(t);
    await append(f, ["ingest-batch", "sync-checkpoint"]);
    const [row] = await outboxMessages(f.database);
    const sealed = new Uint8Array(row.sealed);
    corrupt(sealed);
    await f.database.execute("UPDATE outbox_messages SET sealed = ? WHERE installation_id = ? AND sequence = 1", [sealed, INSTALLATION]);
    const before = await contents(f.database);
    assert.deepEqual(await f.sender.sendNext(), { outcome: "blocked", sequence: 1, failure: "head-unreadable" });
    const recorded = { installation_id: INSTALLATION, sequence: 1, origin: "sender", failure: "head-unreadable",
      message_sha256: null, first_seen_ms: NOW, last_seen_ms: NOW, count: 1, notified: 1 };
    assert.deepEqual(await contents(f.database), withTables(before, { outbox_head_block: [recorded] }));
    f.wall = NOW + 1_000;
    assert.deepEqual(await f.sender.sendNext(), { outcome: "blocked", sequence: 1, failure: "head-unreadable" });
    assert.deepEqual(await f.sender.headBlock(), block(1, "head-unreadable", NOW, NOW + 1_000, 2, null, "sender"));
    assert.deepEqual(f.transport.requests, []);
    assert.deepEqual(f.notifier.calls, [["head-unreadable"]]);
  }
});

test("a_head_that_is_not_its_row_is_a_local_block", async (t) => {
  const valid = message("ingest-batch", 1);
  const cases: [string, OutboxKind, Uint8Array, number?][] = [
    ["another sequence", "ingest-batch", message("ingest-batch", 2)],
    ["another kind", "sync-checkpoint", valid],
    ["another installation", "ingest-batch", message("ingest-batch", 1, OTHER_INSTALLATION)],
    ["noncanonical bytes", "ingest-batch", utf8(JSON.stringify(JSON.parse(new TextDecoder().decode(valid)), null, 1))],
    ["a different length", "ingest-batch", valid, valid.length + 1],
    ["not an Outbox Message", "ingest-batch", receipt(valid)],
    ["not a message", "ingest-batch", utf8("synthetic bytes that are not a message")],
  ];
  for (const [label, kind, plaintext, messageBytes] of cases) {
    const f = await fixture(t);
    await appendRaw(f, kind, plaintext, messageBytes === undefined ? {} : { messageBytes });
    await append(f, ["sync-checkpoint"]);
    const before = await contents(f.database);
    assert.deepEqual(await f.sender.sendNext(), { outcome: "blocked", sequence: 1, failure: "head-invalid" }, label);
    const recorded = { installation_id: INSTALLATION, sequence: 1, origin: "sender", failure: "head-invalid",
      message_sha256: messageSha256(plaintext), first_seen_ms: NOW, last_seen_ms: NOW, count: 1, notified: 1 };
    assert.deepEqual(await contents(f.database), withTables(before, { outbox_head_block: [recorded] }), label);
    assert.deepEqual(f.transport.requests, [], label);
    assert.deepEqual(f.notifier.calls, [["head-invalid"]], label);
  }
});

// R8 — One notification at each onset

test("a_head_block_notifies_once", async (t) => {
  const f = await fixture(t);
  const [first, second] = await append(f, ["ingest-batch", "sync-checkpoint"]);
  f.transport.respond = respondWith(deliveryBlock(first, "sequence-out-of-order"));
  for (let attempt = 0; attempt < 3; attempt++) await f.sender.sendNext();
  // A new failure for the same head is not a new onset.
  f.transport.respond = respondWith(deliveryBlock(first, "unsupported-wire-protocol"));
  await f.sender.sendNext();
  assert.deepEqual(f.notifier.calls, [["sequence-out-of-order"]]);
  // After a receipt clears it, a block of the next head is.
  f.transport.respond = (request) => ({ outcome: "response", body: receipt(request.body) });
  assert.equal((await f.sender.sendNext()).outcome, "delivered");
  f.transport.respond = respondWith(deliveryBlock(second, "sequence-content-mismatch"));
  await f.sender.sendNext();
  await f.sender.sendNext();
  assert.deepEqual(f.notifier.calls, [["sequence-out-of-order"], ["sequence-content-mismatch"]]);
});

test("a_failed_notification_is_retried", async (t) => {
  for (const synchronously of [false, true]) {
    const f = await fixture(t);
    const [first] = await append(f, ["ingest-batch", "sync-checkpoint"]);
    f.notifier.failures = 5;
    f.notifier.synchronously = synchronously;
    f.transport.respond = respondWith(deliveryBlock(first, "record-count-limit-exceeded"));
    assert.deepEqual(await f.sender.sendNext(), { outcome: "blocked", sequence: 1, failure: "record-count-limit-exceeded" });
    assert.deepEqual((await blockRows(f.database))[0].notified, 0);
    // Every later attempt for the head retries it, whatever its answer, until it succeeds.
    f.base.setUnavailable(true);
    assert.equal((await f.sender.sendNext()).outcome, "retryable");
    f.base.setUnavailable(false);
    assert.equal((await f.make({ cipher: new NodeInstallationCipher() }).sendNext()).outcome, "cipher-failed");
    f.transport.respond = respondWith(retryable("temporarily-unavailable"));
    assert.equal((await f.sender.sendNext()).outcome, "retryable");
    f.transport.respond = () => ({ outcome: "failed" });
    assert.equal((await f.sender.sendNext()).outcome, "retryable");
    assert.deepEqual((await blockRows(f.database))[0].notified, 0);
    f.base.invalidate(KEY_ALIAS);
    assert.equal((await f.sender.sendNext()).outcome, "cipher-failed");
    assert.deepEqual((await blockRows(f.database))[0].notified, 1);
    assert.equal((await f.sender.sendNext()).outcome, "cipher-failed");
    assert.deepEqual(f.notifier.calls, Array(6).fill(["record-count-limit-exceeded"]));
    assert.deepEqual(f.notifier.delivered, ["record-count-limit-exceeded"]);
  }
});

test("a_lost_acknowledgement_notifies_again", async (t) => {
  const f = await fixture(t);
  const [first] = await append(f, ["ingest-batch"]);
  const { database, failing } = await f.writeFailing();
  failing(false);
  const sender = f.make({ database });
  f.transport.respond = respondWith(deliveryBlock(first, "uncompressed-byte-limit-exceeded"));
  // The notification succeeds, but recording that it did fails.
  f.notifier.onNotify = () => { failing(true); };
  assert.deepEqual(await sender.sendNext(), { outcome: "storage-failed" });
  failing(false);
  f.notifier.onNotify = null;
  assert.deepEqual(await f.sender.headBlock(), block(1, "uncompressed-byte-limit-exceeded", NOW, NOW, 1, messageSha256(first)));
  assert.equal((await sender.sendNext()).outcome, "blocked");
  assert.equal((await sender.sendNext()).outcome, "blocked");
  assert.deepEqual(f.notifier.calls, [["uncompressed-byte-limit-exceeded"], ["uncompressed-byte-limit-exceeded"]]);
});

// R9 — Reading the head block

test("the_head_block_is_read_exactly", async (t) => {
  const f = await fixture(t);
  const [first] = await append(f, ["ingest-batch"]);
  assert.equal(await f.sender.headBlock(), null);
  // Another installation's head block is never this installation's.
  const [foreign] = await append(f, ["sync-checkpoint"], OTHER_INSTALLATION);
  const other = f.make({ context: { installationId: OTHER_INSTALLATION, keyAlias: KEY_ALIAS, portalUrl: PORTAL_URL } });
  f.transport.respond = respondWith(deliveryBlock(foreign, "noncanonical-message"));
  assert.equal((await other.sendNext()).outcome, "blocked");
  assert.equal(await f.sender.headBlock(), null);
  f.transport.respond = respondWith(deliveryBlock(first, "fragment-order-mismatch"));
  await f.sender.sendNext();
  f.wall = NOW + 61_000;
  await f.sender.sendNext();
  const before = await contents(f.database);
  for (let read = 0; read < 3; read++) {
    assert.deepEqual(await f.sender.headBlock(), block(1, "fragment-order-mismatch", NOW, NOW + 61_000, 2, messageSha256(first)));
  }
  assert.deepEqual(await contents(f.database), before);
  assert.deepEqual(await other.headBlock(), block(1, "noncanonical-message", NOW, NOW, 1, messageSha256(foreign)));
  for (const failure of [new Error(`${SENTINEL} synthetic storage failure`), new TypeError(`${SENTINEL} synthetic storage value`)]) {
    assert.deepEqual(await f.make({ database: brokenDatabase(failure) }).headBlock(), { outcome: "storage-failed" });
  }
});

test("the_head_block_survives_a_restart", async (t) => {
  const f = await fixture(t);
  const [first] = await append(f, ["ingest-batch", "sync-checkpoint"]);
  f.transport.respond = respondWith(deliveryBlock(first, "completion-count-mismatch"));
  await f.sender.sendNext();
  await f.reopen();
  assert.deepEqual(await f.sender.headBlock(), block(1, "completion-count-mismatch", NOW, NOW, 1, messageSha256(first)));
  // Its notification state survives too: a repeat after the restart does not notify again.
  f.wall = NOW + 1;
  assert.equal((await f.sender.sendNext()).outcome, "blocked");
  assert.deepEqual(f.notifier.calls, [["completion-count-mismatch"]]);
  f.transport.respond = (request) => ({ outcome: "response", body: receipt(request.body) });
  assert.equal((await f.sender.sendNext()).outcome, "delivered");
  await f.reopen();
  assert.equal(await f.sender.headBlock(), null);
});

// R10 — Storage and concurrency

test("storage_failures_change_nothing", async (t) => {
  const f = await fixture(t);
  const [first] = await append(f, ["ingest-batch", "sync-checkpoint"]);
  const before = await contents(f.database);
  for (const failure of [new Error(`${SENTINEL} synthetic storage failure`), new TypeError(`${SENTINEL} synthetic storage value`)]) {
    assert.deepEqual(await f.make({ database: brokenDatabase(failure) }).sendNext(), { outcome: "storage-failed" });
  }
  assert.deepEqual(f.transport.requests, []);
  // Writes start failing only once the Portal has answered, so reads before the request always succeed.
  const { database, failing } = await f.writeFailing();
  failing(false);
  const writes = f.make({ database });
  // A receipt whose removal fails keeps the row; the identical retry gets the identical receipt.
  f.transport.respond = (request) => { failing(true); return { outcome: "response", body: receipt(request.body) }; };
  assert.deepEqual(await writes.sendNext(), { outcome: "storage-failed" });
  assert.deepEqual(await contents(f.database), before);
  // A Delivery Block or a local block that cannot be recorded is not notified.
  failing(false);
  f.transport.respond = () => { failing(true); return { outcome: "response", body: deliveryBlock(first, "sequence-content-mismatch") }; };
  assert.deepEqual(await writes.sendNext(), { outcome: "storage-failed" });
  const wrongKey = f.make({ database, cipher: { ...f.base, open: async () => ({ status: "error", reason: "authentication-failed" }) } as unknown as InstallationCipher });
  assert.deepEqual(await wrongKey.sendNext(), { outcome: "storage-failed" });
  assert.deepEqual(f.notifier.calls, []);
  assert.deepEqual(await contents(f.database), before);
  failing(false);
  f.transport.respond = (request) => ({ outcome: "response", body: receipt(request.body) });
  assert.deepEqual(await writes.sendNext(), { outcome: "delivered", sequence: 1, kind: "ingest-batch" });
  assert.deepEqual(f.transport.bodies(), [first, first, first]);
});

test("removal_and_block_clearing_are_atomic", async (t) => {
  const f = await fixture(t);
  const [first] = await append(f, ["ingest-batch", "sync-checkpoint"]);
  f.transport.respond = respondWith(deliveryBlock(first, "sequence-content-mismatch"));
  assert.equal((await f.sender.sendNext()).outcome, "blocked");
  const before = await contents(f.database);
  f.transport.respond = (request) => ({ outcome: "response", body: receipt(request.body) });
  // A failed commit, and then each write of the removal failing in turn, keep the row and its block together.
  assert.deepEqual(await f.make({ database: failingWrites(f.database, { commit: true }) }).sendNext(), { outcome: "storage-failed" });
  assert.deepEqual(await contents(f.database), before);
  let writes = 0;
  for (let mutation = 1; mutation <= 10; mutation++) {
    const result = await f.make({ database: failingWrites(f.database, { mutation }) }).sendNext();
    if (result.outcome === "delivered") { writes = mutation - 1; break; }
    assert.deepEqual(result, { outcome: "storage-failed" }, String(mutation));
    assert.deepEqual(await contents(f.database), before, String(mutation));
  }
  // Removing the row and clearing its block takes at least two writes, and then both are gone.
  assert.ok(writes >= 2, String(writes));
  assert.equal(await f.sender.headBlock(), null);
  assert.deepEqual(await outboxSequences(f.database), [2]);
});

test("the_network_call_does_not_hold_the_storage_lane", async (t) => {
  const f = await fixture(t);
  await append(f, ["ingest-batch"]);
  const operations: string[] = [];
  const database = logging(f.database, operations);
  const sender = f.make({ database });
  const lane = storageLane(database);
  // While another unit holds the shared lane, the attempt touches neither storage nor the Portal.
  let release!: () => void;
  let acquired!: () => void;
  const holding = new Promise<void>((resolve) => { acquired = resolve; });
  const held = lane(() => new Promise<void>((resolve) => { release = resolve; acquired(); }));
  await holding;
  const attempt = sender.sendNext();
  try {
    await settle();
    assert.deepEqual(operations, []);
    assert.deepEqual(f.transport.requests, []);
  } finally { release(); }
  await held;
  assert.equal((await attempt).outcome, "delivered");
  // While the Portal has not answered, staging proceeds through the lane.
  await append(f, ["sync-checkpoint"]);
  let open!: () => void;
  f.transport.gate = new Promise<void>((resolve) => { open = resolve; });
  const entered = f.transport.arrival();
  const pending = sender.sendNext();
  try {
    await entered;
    assert.equal(await Promise.race([lane(async () => {
      const last = await lastOutboxSequence(database, INSTALLATION);
      const sealed = await f.base.seal(KEY_ALIAS, message("change-completion", last + 1), outboxAad(INSTALLATION, last + 1));
      await database.transaction((tx) => appendOutboxMessages(tx, INSTALLATION, last,
        [{ kind: "change-completion", sealed: (sealed as { value: Uint8Array }).value, messageBytes: message("change-completion", last + 1).length }]));
      return "staged";
    }), settle(1_000).then(() => "held")]), "staged");
  } finally { open(); }
  assert.deepEqual(await pending, { outcome: "delivered", sequence: 2, kind: "sync-checkpoint" });
  assert.deepEqual(await outboxSequences(f.database), [3]);
  // A pending notification never holds the lane either.
  f.transport.gate = null;
  f.transport.respond = (request) => ({ outcome: "response", body: deliveryBlock(request.body, "sequence-content-mismatch") });
  let release2!: () => void;
  f.notifier.gate = new Promise<void>((resolve) => { release2 = resolve; });
  const notifying = f.notifier.arrival();
  const blocked = sender.sendNext();
  try {
    await notifying;
    assert.equal(await Promise.race([lane(async () => "free"), settle(1_000).then(() => "held")]), "free");
  } finally { release2(); }
  assert.deepEqual(await blocked, { outcome: "blocked", sequence: 3, failure: "sequence-content-mismatch" });
});

test("attempts_run_one_at_a_time", async (t) => {
  const f = await fixture(t);
  const [first, second] = await append(f, ["ingest-batch", "change-completion"]);
  let open!: () => void;
  f.transport.gate = new Promise<void>((resolve) => { open = resolve; });
  const entered = f.transport.arrival();
  const attempts = [f.sender.sendNext(), f.sender.sendNext()];
  await entered;
  await settle();
  assert.equal(f.transport.requests.length, 1);
  open();
  assert.deepEqual(await Promise.all(attempts), [
    { outcome: "delivered", sequence: 1, kind: "ingest-batch" }, { outcome: "delivered", sequence: 2, kind: "change-completion" },
  ]);
  assert.equal(f.transport.maxInFlight, 1);
  assert.deepEqual(f.transport.bodies(), [first, second]);
});

test("overlapping_instances_remove_the_head_once", async (t) => {
  const f = await fixture(t);
  const [first, second] = await append(f, ["ingest-batch", "change-completion"]);
  const other = f.make();
  let open!: () => void;
  f.transport.gate = new Promise<void>((resolve) => { open = resolve; });
  const attempts = [f.sender.sendNext(), other.sendNext()];
  for (let waits = 0; f.transport.requests.length < 2; waits++) {
    assert.ok(waits < 200, "both instances send the head");
    await settle(5);
  }
  open();
  assert.deepEqual(await Promise.all(attempts), [
    { outcome: "delivered", sequence: 1, kind: "ingest-batch" }, { outcome: "delivered", sequence: 1, kind: "ingest-batch" },
  ]);
  assert.equal(f.transport.maxInFlight, 2);
  assert.deepEqual(await outboxSequences(f.database), [2]);
  f.transport.gate = null;
  assert.deepEqual(await other.sendNext(), { outcome: "delivered", sequence: 2, kind: "change-completion" });
  assert.deepEqual(f.transport.bodies(), [first, first, second]);
});

// R11 — The Sender schema migration

const DDL = `CREATE TABLE outbox_head_block (
  installation_id TEXT PRIMARY KEY NOT NULL,
  sequence INTEGER NOT NULL CHECK (sequence > 0),
  origin TEXT NOT NULL CHECK (origin IN ('portal', 'sender')),
  failure TEXT NOT NULL,
  message_sha256 TEXT,
  first_seen_ms INTEGER NOT NULL,
  last_seen_ms INTEGER NOT NULL,
  count INTEGER NOT NULL CHECK (count > 0),
  notified INTEGER NOT NULL CHECK (notified IN (0, 1)),
  CHECK ((origin = 'sender') = (failure IN ('head-unreadable', 'head-invalid'))),
  CHECK ((message_sha256 IS NULL) = (failure = 'head-unreadable'))) STRICT`;
/** SQL as its tokens, so whitespace between tokens never matters and literals keep their contents. */
const normalize = (sql: DatabaseValue): DatabaseValue =>
  typeof sql === "string" ? (sql.match(/'(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_]*|[0-9]+|\S/g) ?? []).join(" ") : sql;

async function schemaOf(database: SenderDatabase): Promise<DatabaseRow[]> {
  return (await database.query("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"))
    .map((entry) => ({ ...entry, sql: normalize(entry.sql) }));
}

test("migration_creates_version_seven", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const database = await openNodeDatabase(join(directory, "synthetic.sqlite"));
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.deepEqual(await migrate(database, MIGRATIONS.slice(0, 6)), { status: "ready", version: 6 });
  const before = await schemaOf(database);
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 7 });
  const after = await schemaOf(database);
  const key = (entry: DatabaseRow) => `${entry.type}:${entry.name}`;
  const kept = new Set(before.map(key));
  assert.deepEqual(after.filter((entry) => kept.has(key(entry))), before);
  assert.deepEqual(after.filter((entry) => !kept.has(key(entry))), [
    { type: "index", name: "sqlite_autoindex_outbox_head_block_1", tbl_name: "outbox_head_block", sql: null },
    { type: "table", name: "outbox_head_block", tbl_name: "outbox_head_block", sql: normalize(DDL) },
  ]);
  assert.deepEqual(await database.query("SELECT * FROM outbox_head_block"), []);
  assert.equal(deliveryMigration.version, 7);
});

test("migration_is_idempotent", async (t) => {
  const f = await fixture(t);
  const [first] = await append(f, ["ingest-batch"]);
  f.transport.respond = respondWith(deliveryBlock(first, "sequence-content-mismatch"));
  await f.sender.sendNext();
  const before = await contents(f.database);
  assert.deepEqual(await migrate(f.database, MIGRATIONS), { status: "ready", version: 7 });
  assert.deepEqual(await contents(f.database), before);
});

// R12 — Delivery under faults

/** A deterministic generator, so each seed replays exactly. */
function random(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

/** Archives what it accepts, answers identical retries with the same receipt, and blocks sequences it is told to. */
class FakePortal {
  readonly archived = new Map<number, string>();
  readonly blocked = new Map<number, string>();
  answer(body: Uint8Array): Uint8Array {
    const { sequence, message_sha256: sha } = identity(body);
    const failure = this.blocked.get(sequence);
    if (failure !== undefined) return deliveryBlock(body, failure);
    const archived = this.archived.get(sequence);
    if (archived !== undefined && archived !== sha) return deliveryBlock(body, "sequence-content-mismatch");
    this.archived.set(sequence, sha);
    return receipt(body);
  }
}

test("delivery_survives_faults_in_sequence", async (t) => {
  const events = new Map<string, number>();
  const count = (event: string) => { events.set(event, (events.get(event) ?? 0) + 1); };
  const FAULTS = ["normal", "normal", "normal", "lose-request", "lose-response", "mismatch", "oversized", "retryable"] as const;
  const FAILURES = ["unsupported-wire-protocol", "completion-chain-mismatch", "fragment-order-mismatch"];
  for (let seed = 1; seed <= 6; seed++) {
    const next = random(seed);
    const pick = <T>(items: readonly T[]): T => items[Math.floor(next() * items.length)];
    const f = await fixture(t);
    const portal = new FakePortal();
    let fault: (typeof FAULTS)[number] = "normal";
    f.transport.respond = (request) => {
      if (fault === "lose-request") return { outcome: "failed" };
      if (fault === "mismatch") return { outcome: "response", body: receipt(request.body, { message_sha256: "0".repeat(64) }) };
      if (fault === "oversized") return { outcome: "response", body: new Uint8Array(MAX_RESPONSE_BYTES + 1 + Math.floor(next() * 100)) };
      if (fault === "retryable") return { outcome: "response", body: retryable(pick(["temporarily-unavailable", "storage-reserve-exhausted"])) };
      const answer = portal.answer(request.body);
      return fault === "lose-response" ? { outcome: "failed" } : { outcome: "response", body: answer };
    };
    const staged = new Map<number, { kind: OutboxKind; bytes: Uint8Array }>();
    const removed = new Set<number>();
    const lostResponse = new Set<number>();
    let blockedHead: number | null = null;
    let onsets = 0;
    for (let step = 0; step < 120; step++) {
      f.wall += 1_000;
      if (next() < 0.35) {
        const kind = pick(KINDS);
        const [bytes] = await append(f, [kind]);
        staged.set(identity(bytes).sequence, { kind, bytes });
        continue;
      }
      const remaining = [...staged.keys()].filter((sequence) => !removed.has(sequence));
      const head = remaining.length ? Math.min(...remaining) : null;
      if (head !== null && next() < 0.12) portal.blocked.set(head, pick(FAILURES));
      if (head !== null && portal.blocked.has(head) && next() < 0.3) portal.blocked.delete(head);
      fault = pick(FAULTS);
      const requests = f.transport.requests.length;
      const result = await f.sender.sendNext();
      if (head === null) {
        assert.deepEqual(result, { outcome: "empty" });
        assert.equal(f.transport.requests.length, requests);
        continue;
      }
      // One request, for the head, with its staged bytes.
      assert.equal(f.transport.requests.length, requests + 1, `${seed}:${step}`);
      assert.deepEqual(f.transport.requests.at(-1), sent(staged.get(head)!.bytes), `${seed}:${step}`);
      count(fault);
      if (result.outcome === "delivered") {
        assert.ok(fault === "normal", `${seed}:${step} ${fault}`);
        assert.deepEqual(result, { outcome: "delivered", sequence: head, kind: staged.get(head)!.kind });
        removed.add(head);
        count(`delivered-${result.kind}`);
        if (blockedHead === head) { count("block-cleared"); blockedHead = null; }
        if (lostResponse.has(head)) count("lost-response-receipted");
      } else if (result.outcome === "blocked") {
        assert.equal(fault, "normal", `${seed}:${step}`);
        assert.equal(result.sequence, head);
        if (blockedHead !== head) onsets++;
        blockedHead = head;
        count("blocked");
      } else {
        assert.equal(result.outcome, "retryable", `${seed}:${step} ${JSON.stringify(result)}`);
        assert.notEqual(fault, "normal", `${seed}:${step}`);
        if (fault === "lose-response") lostResponse.add(head);
      }
      // After every attempt: the Outbox, the head block, and notifications agree with what the Sender received.
      assert.deepEqual(await outboxSequences(f.database), [...staged.keys()].filter((sequence) => !removed.has(sequence)).sort((a, b) => a - b));
      const current = await f.sender.headBlock();
      assert.equal(current === null ? null : (current as HeadBlock).sequence, blockedHead, `${seed}:${step}`);
      assert.equal(f.notifier.calls.length, onsets, `${seed}:${step}`);
    }
    // Identical bytes for every request of a sequence.
    const bodies = new Map<number, Uint8Array>();
    for (const request of f.transport.requests) {
      const sequence = identity(request.body).sequence;
      if (bodies.has(sequence)) assert.deepEqual(request.body, bodies.get(sequence));
      bodies.set(sequence, request.body);
    }
  }
  for (const event of ["lose-request", "lose-response", "mismatch", "oversized", "retryable", "blocked", "block-cleared",
    "lost-response-receipted", ...KINDS.map((kind) => `delivered-${kind}`)]) {
    assert.ok((events.get(event) ?? 0) > 0, event);
  }
});

// R13 — Boundary

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
  // The network is reached only through the transport seam, and storage touches only the Outbox and this unit's table.
  const NETWORK = new Set(["fetch", "XMLHttpRequest", "WebSocket", "EventSource", "navigator", "process", "Buffer", "require", "global", "globalThis"]);
  const files = (readdirSync(MODULE_DIRECTORY, { recursive: true }) as string[]).filter((name) => name.endsWith(".ts")).map((name) => join(MODULE_DIRECTORY, name));
  assert.ok(files.includes(MODULE));
  for (const file of files) {
    const code = parse(file);
    for (const table of OTHER_TABLES) {
      for (const text of code.strings) assert.doesNotMatch(text, new RegExp(`\\b${table}\\b`), `${file}: ${table}`);
    }
    for (const { specifier, values, dynamic } of code.dependencies) {
      assert.equal(dynamic, false, `${file}: ${specifier}`);
      // Local helpers stay inside the directory, where this test inspects them too.
      if (specifier.startsWith("./")) { assert.doesNotMatch(specifier, /\.\./, `${file}: ${specifier}`); continue; }
      if (values.length === 0) continue;
      if (/^\.\.\/storage\//.test(specifier)) continue;
      if (specifier === "@activity-tracker-portal/ingest-protocol") {
        for (const value of values) assert.ok(["decodeMessage", "interpretResponse", "messageSha256"].includes(value), `${file}: ${value}`);
        continue;
      }
      assert.equal(specifier, "../outbox/stage-outbox-batch.ts", `${file}: ${specifier}`);
      assert.deepEqual(values, ["outboxAad"], file);
    }
    assert.deepEqual(code.references.filter((name) => NETWORK.has(name)), [], file);
  }
  // At run time, delivery opens only, and never seals, creates, deletes, or inspects keys.
  const f = await fixture(t);
  const [first] = await append(f, ["ingest-batch", "sync-checkpoint"]);
  f.transport.respond = respondWith(deliveryBlock(first, "sequence-content-mismatch"));
  await f.sender.sendNext();
  f.transport.respond = (request) => ({ outcome: "response", body: receipt(request.body) });
  await f.sender.sendNext();
  await f.sender.sendNext();
  await f.sender.headBlock();
  assert.deepEqual([...new Set(f.cipher.calls.map((call) => call.method))], ["open"]);
});
