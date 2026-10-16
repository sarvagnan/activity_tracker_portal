// Acceptance tests for manage-sender-enrollment@1.
// Requirement numbers refer to packs/manage-sender-enrollment-1.spec.md. Storage is the 3ug node:sqlite
// SenderDatabase substitute and the cipher is the 3ug NodeInstallationCipher. The transport and Portal are
// synthetic fakes defined here, whose answers are built with the protocol library. Outbox rows are appended
// through stage-outbox-batch@1's primitives, and delivered through send-outbox-message@1.
import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import type { DatabaseSync } from "node:sqlite";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import type { TestContext } from "node:test";
import { decodeMessage, encodeMessage, installationFingerprint, messageSha256 } from "@activity-tracker-portal/ingest-protocol";
import ts from "typescript";
import { createOutboxSender, deliveryMigration } from "../src/delivery/send-outbox-message.ts";
import type { OutboxRequest, SendResult } from "../src/delivery/send-outbox-message.ts";
import {
  CAPABILITIES_ROUTE, CONFIRMATION_ROUTE, ENROLLMENT_TIMEOUT_MS, MAX_ANSWER_BYTES, createSenderEnrollment, enrollmentMigration,
} from "../src/enrollment/manage-sender-enrollment.ts";
import type {
  EnrollmentOffer, EnrollmentPhase, EnrollmentRequest, EnrollmentStatus, EnrollmentTransport, EnrollmentTransportResult,
  SenderEnrollment, SenderEnrollmentOptions,
} from "../src/enrollment/manage-sender-enrollment.ts";
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
const MODULE = join(HERE, "../src/enrollment/manage-sender-enrollment.ts");
const MODULE_DIRECTORY = dirname(MODULE);
const SOURCE_STORAGE = join(HERE, "../src/storage");
const STAGE_OUTBOX_BATCH = join(HERE, "../src/outbox/stage-outbox-batch.ts");
const IDENTITY = "00000000-0000-4000-8000-0000000000e1";
const SECOND_IDENTITY = "00000000-0000-4000-8000-0000000000e2";
const THIRD_IDENTITY = "00000000-0000-4000-8000-0000000000e3";
const OTHER_INSTALLATION = "00000000-0000-4000-8000-0000000000f1";
const OTHER_KEY_ALIAS = "synthetic-other-installation-key";
const ALIAS = `activity-tracker-portal.installation.${IDENTITY}`;
const FINGERPRINT = installationFingerprint(IDENTITY);
const CODE = "SYNTHETICCODE234";
const PORTAL_URL = "http://portal.synthetic.invalid:8443";
const SENTINEL = "SENTINEL";
const MIGRATIONS = [
  snapshotAttemptsMigration, outboxMigration, completionMigration, checkpointMigration, syncMigration, schedulingMigration,
  deliveryMigration, enrollmentMigration,
];
const NOW = Date.UTC(2026, 9, 16, 9, 0, 0, 0);
const utf8 = (text: string) => new TextEncoder().encode(text);
const text = (bytes: Uint8Array) => new TextDecoder().decode(bytes);
const uuid = (n: number) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, "0")}`;
const settle = (ms = 20) => new Promise<void>((resolve) => { setTimeout(resolve, ms); });

// Synthetic offers and Portal answers, built with the protocol library.

function offerBytes(overrides: Record<string, unknown> = {}): Uint8Array {
  return encodeMessage({
    wire_version: 1, kind: "enrollment-offer", enrollment_code: CODE, portal_url: PORTAL_URL,
    expires_at: "2026-10-16T09:15:00.000Z", ...overrides,
  });
}
const OFFER = offerBytes();
const scanned = (bytes: Uint8Array = OFFER): EnrollmentOffer => ({ kind: "scanned", bytes });
const typed = (portalUrl = PORTAL_URL, enrollmentCode = CODE): EnrollmentOffer => ({ kind: "typed", portalUrl, enrollmentCode });

const confirmation = (identity = IDENTITY, code = CODE) => encodeMessage({
  wire_version: 1, kind: "enrollment-confirmation", enrollment_code: code, sender_installation: identity,
  fingerprint: installationFingerprint(identity),
});
const CONFIRMATION = confirmation();

function advertisement(generation = 1, overrides: Record<string, unknown> = {}): Uint8Array {
  return encodeMessage({
    wire_version: 1, kind: "capability-advertisement", generation, wire_versions: [1],
    projection_schemas: [{ record_type: "steps", versions: [1] }], max_record_count: 4, max_message_bytes: 65_536, ...overrides,
  });
}

/** A valid advertisement of exactly `size` bytes. */
function advertisementOfSize(generation: number, size: number): Uint8Array {
  const name = (index: number) => `t${String(index).padStart(5, "0")}`;
  const build = (count: number, extra: number) => advertisement(generation, {
    projection_schemas: Array.from({ length: count }, (_, index) =>
      ({ record_type: index === count - 1 ? name(index) + "a".repeat(extra) : name(index), versions: [1] })),
  });
  for (let count = Math.max(1, Math.floor((size - 400) / 40)); count < 10_000; count++) {
    const room = size - build(count, 0).length;
    assert.ok(room >= 0, "advertisement size unreachable");
    if (room <= 58) {
      const bytes = build(count, room);
      assert.equal(bytes.length, size);
      return bytes;
    }
  }
  throw new Error("advertisement size unreachable");
}

const acknowledgement = (sequence: number, generation: number, identity = IDENTITY) => encodeMessage({
  wire_version: 1, kind: "capability-acknowledgement", sender_installation: identity, sequence, capability_generation: generation,
});
const retryable = (reason = "temporarily-unavailable") => encodeMessage({ wire_version: 1, kind: "retryable-failure", reason });

function identityOf(bytes: Uint8Array): { sender_installation: string; sequence: number; message_sha256: string } {
  const decoded = decodeMessage(bytes);
  return { sender_installation: decoded.sender_installation as string, sequence: decoded.sequence as number, message_sha256: messageSha256(bytes) };
}
const receipt = (bytes: Uint8Array) => encodeMessage({ wire_version: 1, kind: "delivery-receipt", ...identityOf(bytes) });
const deliveryBlock = (bytes: Uint8Array, failure: string) =>
  encodeMessage({ wire_version: 1, kind: "delivery-block", failure, ...identityOf(bytes) });

const confirmationRequest = (identity = IDENTITY, code = CODE, portalUrl = PORTAL_URL): EnrollmentRequest => ({
  method: "POST", url: portalUrl + "/v1/enrollment-confirmation", headers: { "content-type": "application/json" },
  body: confirmation(identity, code), timeoutMs: 30_000, maxResponseBytes: 65_536,
});
const CAPABILITIES_REQUEST: EnrollmentRequest = {
  method: "GET", url: PORTAL_URL + "/v1/capabilities", headers: {}, body: null, timeoutMs: 30_000, maxResponseBytes: 65_536,
};

// Seams

type Respond = (request: EnrollmentRequest) => unknown;
const respondWith = (body: Uint8Array): Respond => () => ({ outcome: "response", body });

/** Records each request; `respond` answers it (an Error is thrown), and `gate` holds every answer until opened. */
class FakeTransport implements EnrollmentTransport {
  readonly requests: EnrollmentRequest[] = [];
  respond: Respond = () => ({ outcome: "response", body: advertisement(1) });
  gate: Promise<void> | null = null;
  /** Overwrite each request body after recording it, as a careless adapter might. */
  vandalize = false;
  inFlight = 0;
  maxInFlight = 0;
  private arrivals: (() => void)[] = [];
  private readonly events: string[];
  constructor(events: string[]) { this.events = events; }

  arrival(): Promise<void> {
    return new Promise<void>((resolve) => { this.arrivals.push(resolve); });
  }

  // An arrow property, so a proxy or spread sees only the `send` access.
  readonly send = (request: EnrollmentRequest): Promise<EnrollmentTransportResult> => {
    this.events.push("transport");
    this.requests.push({
      ...request, headers: { ...request.headers },
      body: request.body instanceof Uint8Array ? new Uint8Array(request.body) : request.body,
    });
    if (this.vandalize && request.body instanceof Uint8Array) request.body.fill(0);
    this.inFlight++;
    this.maxInFlight = Math.max(this.maxInFlight, this.inFlight);
    for (const arrive of this.arrivals.splice(0)) arrive();
    const answer = this.respond(request);
    const done = () => { this.inFlight--; };
    if (answer instanceof Error) { done(); throw answer; }
    return (this.gate ?? Promise.resolve()).then(() => answer).finally(done) as Promise<EnrollmentTransportResult>;
  };
}

type Script = { returns?: unknown; throws?: unknown; before?: () => unknown };
type CipherMethod = "create" | "seal" | "delete" | "isDeviceSecure" | "open" | "keyStatus";

/** Records every cipher call; `script` replaces or precedes a method's next calls, in order. */
class SpyCipher implements InstallationCipher {
  readonly calls: { method: CipherMethod; alias?: string; aad?: Uint8Array }[] = [];
  readonly script = new Map<CipherMethod, Script[]>();
  secure = true;
  private readonly inner: InstallationCipher;
  private readonly events: string[];
  constructor(inner: InstallationCipher, events: string[]) { this.inner = inner; this.events = events; }

  private async run<T>(method: CipherMethod, call: { alias?: string; aad?: Uint8Array }, real: () => Promise<T>): Promise<T> {
    this.calls.push({ method, ...call });
    this.events.push(`cipher.${method}`);
    const entry = this.script.get(method)?.shift();
    if (entry?.before) await entry.before();
    if (entry && "throws" in entry) throw entry.throws;
    if (entry && "returns" in entry) return entry.returns as T;
    return real();
  }
  create(alias: string) { return this.run("create", { alias }, () => this.inner.create(alias)); }
  seal(alias: string, plaintext: Uint8Array, aad: Uint8Array) {
    return this.run("seal", { alias, aad: new Uint8Array(aad) }, () => this.inner.seal(alias, plaintext, aad));
  }
  delete(alias: string) { return this.run("delete", { alias }, () => this.inner.delete(alias)); }
  isDeviceSecure() { return this.run("isDeviceSecure", {}, async () => this.secure); }
  open(alias: string, sealed: Uint8Array, aad: Uint8Array): Promise<CipherResult<Uint8Array>> {
    return this.run("open", { alias }, () => this.inner.open(alias, sealed, aad));
  }
  keyStatus(alias: string): Promise<InstallationKeyStatus> { return this.run("keyStatus", { alias }, () => this.inner.keyStatus(alias)); }
  methods(): string[] { return this.calls.map((call) => call.method); }
}

async function fixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const path = join(directory, "synthetic.sqlite");
  let database = await openNodeDatabase(path);
  t.after(async () => {
    try { await database.close(); } catch { /* a test may leave the old process's database closed */ }
    rmSync(directory, { recursive: true, force: true });
  });
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 8 });
  const events: string[] = [];
  const base = new NodeInstallationCipher();
  assert.equal((await base.create(OTHER_KEY_ALIAS)).status, "ok");
  const f = {
    base,
    events,
    cipher: new SpyCipher(base, events),
    transport: new FakeTransport(events),
    wall: NOW,
    clockReads: 0,
    now: (): number => { f.clockReads++; events.push("now"); return f.wall; },
    identities: [IDENTITY, SECOND_IDENTITY, THIRD_IDENTITY],
    identityReads: 0,
    randomUUID: (): string => { f.identityReads++; events.push("randomUUID"); return f.identities[f.identityReads - 1] ?? uuid(0xe0 + f.identityReads); },
    /** What the Portal answers on the Outbox route; send-outbox-message@1 delivers through it. */
    outboxAnswer: (body: Uint8Array): Uint8Array => receipt(body),
    outboxRequests: [] as OutboxRequest[],
    get database(): SenderDatabase { return database; },
    path,
    make(overrides: Partial<SenderEnrollmentOptions> = {}): SenderEnrollment {
      return createSenderEnrollment({
        database, cipher: f.cipher, transport: f.transport, now: f.now, randomUUID: f.randomUUID, ...overrides,
      });
    },
    /** A second connection to the same file, so its writes bypass this instance's lane. */
    async second(): Promise<{ readonly database: SenderDatabase; readonly failing: (on: boolean) => void }> {
      let handle!: DatabaseSync;
      const opened = await openNodeDatabase(path, (raw) => { handle = raw; });
      t.after(async () => { try { await opened.close(); } catch { /* already closed */ } });
      return { database: opened, failing: (on: boolean) => { handle.exec(`PRAGMA query_only = ${on ? "ON" : "OFF"}`); } };
    },
    /**
     * A second connection whose writes SQLite itself rejects while failing, so the failure does not depend on how a
     * write is spelled or issued. It starts failing.
     */
    async writeFailing(): Promise<{ readonly database: SenderDatabase; readonly failing: (on: boolean) => void }> {
      const opened = await f.second();
      opened.failing(true);
      return opened;
    },
    /** A new process on the same file; the old instance is never used again. */
    async reopen(): Promise<void> { await database.close(); database = await openNodeDatabase(path); f.sender = f.make(); },
    sender: undefined as unknown as SenderEnrollment,
  };
  f.sender = f.make();
  return f;
}
type Fixture = Awaited<ReturnType<typeof fixture>>;

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

/** `snapshot` with every table empty and the schema and its version kept. */
function emptied(snapshot: Contents): Contents {
  return withTables(snapshot, Object.fromEntries(Object.keys(snapshot.tables).map((name) => [name, []])));
}

async function enrollmentRows(database: SenderDatabase): Promise<DatabaseRow[]> {
  return database.query("SELECT * FROM sender_enrollment ORDER BY rowid");
}

async function outboxSequences(database: SenderDatabase, installation = IDENTITY): Promise<number[]> {
  return (await outboxMessages(database)).filter((row) => row.installationId === installation).map((row) => row.sequence);
}

/** The stored enrollment row, as the requirements describe it. */
const row = (overrides: Record<string, DatabaseValue> = {}): DatabaseRow => ({
  singleton: 1, installation_id: IDENTITY, fingerprint: FINGERPRINT, key_alias: ALIAS, portal_url: PORTAL_URL,
  confirmation: CONFIRMATION, phase: "confirming", refusal: null, advertisement: null, generation: null,
  acknowledgement_sequence: null, enrolled_at_ms: NOW, adopted_at_ms: null, ...overrides,
});

const statusOf = (overrides: Partial<EnrollmentStatus> = {}): EnrollmentStatus => ({
  installationId: IDENTITY, fingerprint: FINGERPRINT, keyAlias: ALIAS, portalUrl: PORTAL_URL, phase: "confirming", refusal: null,
  confirmed: false, stagingContext: null, deliveryContext: { installationId: IDENTITY, keyAlias: ALIAS, portalUrl: PORTAL_URL },
  finalSequence: null, ...overrides,
});

const staging = (generation = 1, overrides: Partial<StagingContext["generation"]> = {}, identity = IDENTITY): StagingContext => ({
  installationId: identity, keyAlias: `activity-tracker-portal.installation.${identity}`,
  generation: { generation, maxRecordCount: 4, maxMessageBytes: 65_536, projectionSchemas: { steps: [1] }, ...overrides },
});

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

/** Seal `plaintext` under the row's own AAD and append it as the installation's next row. */
async function appendRaw(f: Fixture, kind: OutboxKind, plaintext: Uint8Array, options: { installation?: string; alias?: string; database?: SenderDatabase } = {}): Promise<number> {
  const installation = options.installation ?? IDENTITY;
  const database = options.database ?? f.database;
  const alias = options.alias ?? (installation === IDENTITY ? ALIAS : OTHER_KEY_ALIAS);
  const last = await lastOutboxSequence(database, installation);
  const sealed = await f.base.seal(alias, plaintext, outboxAad(installation, last + 1));
  assert.equal(sealed.status, "ok");
  assert.equal(await database.transaction((tx) => appendOutboxMessages(tx, installation, last, [{
    kind, sealed: (sealed as { value: Uint8Array }).value, messageBytes: plaintext.length,
  }])), true);
  return last + 1;
}

/** A sync checkpoint for the installation's next sequence, appended as its next Outbox Message. */
async function appendCheckpoint(f: Fixture, installation = IDENTITY, database = f.database): Promise<number> {
  const sequence = (await lastOutboxSequence(database, installation)) + 1;
  return appendRaw(f, "sync-checkpoint", encodeMessage({
    wire_version: 1, kind: "sync-checkpoint", sender_installation: installation, sequence, capability_generation: 1,
    run_sequence: sequence, trigger: "background", started_at: "2026-10-16T08:59:00.000Z", ended_at: "2026-10-16T08:59:30.000Z",
    background_scheduling: "available", record_types: [], outbox_messages_before_checkpoint: 0, outbox_bytes_before_checkpoint: 0,
  }), { installation, database });
}

/** One send-outbox-message@1 attempt for the installation the enrollment reports. */
async function deliverNext(f: Fixture): Promise<SendResult> {
  const status = await f.sender.status();
  assert.ok(status !== null && !("outcome" in status), JSON.stringify(status));
  const sender = createOutboxSender({
    database: f.database, cipher: f.base, now: () => f.wall, notifier: { notify: async () => {} },
    transport: {
      send: async (request) => {
        f.outboxRequests.push({ ...request, body: new Uint8Array(request.body) });
        return { outcome: "response", body: f.outboxAnswer(request.body) };
      },
    },
    context: (status as EnrollmentStatus).deliveryContext,
  });
  return sender.sendNext();
}

async function currentStatus(f: Fixture): Promise<EnrollmentStatus> {
  const status = await f.sender.status();
  assert.ok(status !== null && !("outcome" in status), JSON.stringify(status));
  return status as EnrollmentStatus;
}

type Phase = EnrollmentPhase | "confirmed";

/** Bring a fresh fixture's installation to `phase`; `confirmed` is enrolled with its acknowledgement delivered. */
async function toPhase(f: Fixture, phase: Phase): Promise<void> {
  const respond = f.transport.respond;
  if (phase === "confirming") {
    f.transport.respond = respondWith(retryable());
    assert.deepEqual(await f.sender.enroll(scanned()), { outcome: "retryable", failure: "temporarily-unavailable" });
  } else if (phase === "refused") {
    f.transport.respond = respondWith(new Uint8Array());
    assert.deepEqual(await f.sender.enroll(scanned()), { outcome: "portal-refused", failure: "enrollment-refused" });
  } else {
    assert.deepEqual(await f.sender.enroll(scanned()), { outcome: "enrolled", generation: 1 });
    if (phase !== "enrolled") {
      assert.deepEqual(await deliverNext(f), { outcome: "delivered", sequence: 1, kind: "capability-acknowledgement" });
      if (phase === "draining") assert.deepEqual(await f.sender.beginDrain(), { outcome: "draining" });
    }
  }
  f.transport.respond = respond;
}

const PHASES: readonly Phase[] = ["confirming", "refused", "enrolled", "confirmed", "draining"];

// R2 — Caller errors

test("malformed_options_are_caller_errors", async (t) => {
  const f = await fixture(t);
  await toPhase(f, "confirmed");
  const before = await contents(f.database);
  const operations: string[] = [];
  const database = logging(f.database, operations);
  const requests = f.transport.requests.length;
  f.cipher.calls.length = 0;
  f.clockReads = 0;
  f.identityReads = 0;
  const without = <T extends object>(value: T, name: string): T => {
    const copy: Record<string, unknown> = {};
    for (const key of Object.keys(value)) if (key !== name) copy[key] = (value as Record<string, unknown>)[key];
    return copy as T;
  };
  const cipher = {
    create: f.cipher.create.bind(f.cipher), seal: f.cipher.seal.bind(f.cipher), delete: f.cipher.delete.bind(f.cipher),
    isDeviceSecure: f.cipher.isDeviceSecure.bind(f.cipher),
  };
  const cases: unknown[] = [
    null, 7, "options",
    { database: undefined },
    { database: without(database, "transaction") },
    { database: without(database, "query") },
    { database: without(database, "execute") },
    { cipher: undefined },
    { cipher: without(cipher, "create") },
    { cipher: without(cipher, "seal") },
    { cipher: without(cipher, "delete") },
    { cipher: without(cipher, "isDeviceSecure") },
    { transport: {} },
    { transport: { send: "send" } },
    { now: NOW },
    { now: undefined },
    { randomUUID: IDENTITY },
    { randomUUID: undefined },
  ];
  for (const entry of cases) {
    const options = entry === null || typeof entry !== "object"
      ? entry : { database, cipher: f.cipher, transport: f.transport, now: f.now, randomUUID: f.randomUUID, ...entry };
    assert.throws(() => createSenderEnrollment(options as SenderEnrollmentOptions), TypeError, JSON.stringify(entry));
  }
  // An offer that is neither shape, and a reset without the acknowledgement, are caller errors too.
  const enrollment = f.make({ database });
  const offers: unknown[] = [
    undefined, null, 7, "offer", {}, { kind: "scanned" }, { kind: "scanned", bytes: text(OFFER) }, { kind: "scanned", bytes: [...OFFER] },
    { kind: "typed", portalUrl: PORTAL_URL }, { kind: "typed", enrollmentCode: CODE }, { kind: "typed", portalUrl: 1, enrollmentCode: CODE },
    { kind: "typed", portalUrl: PORTAL_URL, enrollmentCode: null }, { kind: "pasted", bytes: OFFER }, { bytes: OFFER },
  ];
  for (const offer of offers) await assert.rejects(enrollment.enroll(offer as EnrollmentOffer), TypeError, JSON.stringify(offer));
  const acknowledgements: unknown[] = [
    undefined, null, true, {}, { acknowledgePossibleLoss: false }, { acknowledgePossibleLoss: "true" }, { acknowledgePossibleLoss: 1 },
  ];
  for (const value of acknowledgements) {
    await assert.rejects(enrollment.reset(value as { acknowledgePossibleLoss: true }), TypeError, JSON.stringify(value));
  }
  assert.deepEqual(operations, []);
  assert.equal(f.transport.requests.length, requests);
  assert.deepEqual(f.cipher.calls, []);
  assert.equal(f.clockReads, 0);
  assert.equal(f.identityReads, 0);
  assert.deepEqual(await contents(f.database), before);
  assert.equal(await f.base.keyStatus(ALIAS), "present");
});

test("invalid_seam_results_are_caller_errors", async (t) => {
  const f = await fixture(t);
  await toPhase(f, "confirming");
  const before = await contents(f.database);
  const operations: string[] = [];
  const database = logging(f.database, operations);
  const requests = f.transport.requests.length;
  f.cipher.calls.length = 0;
  const actions: [string, (enrollment: SenderEnrollment) => Promise<unknown>][] = [
    ["enroll", (enrollment) => enrollment.enroll(typed())],
    ["confirm", (enrollment) => enrollment.confirm()],
    ["refreshCapabilities", (enrollment) => enrollment.refreshCapabilities()],
  ];
  for (const reading of [Number.NaN, 1.5, 2 ** 53, -Infinity, "1", null, undefined, 1n]) {
    for (const [name, action] of actions) {
      let reads = 0;
      const enrollment = f.make({ database, now: () => { reads++; return reading as unknown as number; } });
      await assert.rejects(action(enrollment), TypeError, `${name} ${String(reading)}`);
      assert.equal(reads, 1, name);
    }
  }
  const identities: unknown[] = [
    "00000000-0000-4000-8000-0000000000E1", `{${IDENTITY}}`, IDENTITY.replaceAll("-", ""), `${IDENTITY}0`, ` ${IDENTITY}`, "", 1, null, undefined,
  ];
  for (const identity of identities) {
    const enrollment = f.make({ database, randomUUID: () => identity as string });
    await assert.rejects(enrollment.enroll(typed()), TypeError, String(identity));
  }
  assert.deepEqual(operations, []);
  assert.equal(f.transport.requests.length, requests);
  assert.deepEqual(f.cipher.calls, []);
  assert.deepEqual(await contents(f.database), before);
  // Enrolling, confirming, and refreshing each read the clock once, before any storage, cipher, or Portal call;
  // enrolling reads the identity source once too. Draining, unlinking, resetting, and status never read the clock.
  const g = await fixture(t);
  const enrollment = g.make({ database: logging(g.database, [], g.events) });
  g.transport.respond = respondWith(retryable());
  const seams = (events: string[]) => events.filter((event) => event !== "now" && event !== "randomUUID");
  for (const [name, action] of [...actions, ["confirm", (instance: SenderEnrollment) => instance.confirm()]] as typeof actions) {
    g.events.length = 0;
    g.clockReads = 0;
    await action(enrollment);
    assert.equal(g.clockReads, 1, name);
    const first = g.events.findIndex((event) => event !== "now" && event !== "randomUUID");
    assert.ok(first === -1 || g.events.slice(0, first).includes("now"), `${name}: ${g.events.join(", ")}`);
    if (name === "enroll") {
      assert.equal(g.identityReads, 1);
      assert.ok(g.events.slice(0, first).includes("randomUUID"), g.events.join(", "));
    }
    assert.ok(seams(g.events).length > 0, name);
  }
  g.clockReads = 0;
  await enrollment.status();
  await enrollment.beginDrain();
  await enrollment.unlink();
  await enrollment.reset({ acknowledgePossibleLoss: true });
  assert.equal(g.clockReads, 0);
  assert.equal(g.identityReads, 1);
});

// R3 — Enrolling from an offer

test("a_scanned_or_typed_offer_enrolls", async (t) => {
  for (const offer of [scanned(), typed()]) {
    const f = await fixture(t);
    assert.deepEqual(await f.sender.enroll(offer), { outcome: "enrolled", generation: 1 }, offer.kind);
    assert.deepEqual(f.transport.requests, [confirmationRequest()], offer.kind);
    assert.equal((await currentStatus(f)).phase, "enrolled");
  }
  // The offer's own Portal URL and code are used, and its expiry is the Portal's to enforce.
  const f = await fixture(t);
  const other = offerBytes({ portal_url: "https://other.synthetic.invalid", enrollment_code: "ZZZZZZZZ22222222", expires_at: "2020-01-01T00:00:00.000Z" });
  assert.deepEqual(await f.sender.enroll(scanned(other)), { outcome: "enrolled", generation: 1 });
  assert.deepEqual(f.transport.requests, [confirmationRequest(IDENTITY, "ZZZZZZZZ22222222", "https://other.synthetic.invalid")]);
  const status = await currentStatus(f);
  assert.equal(status.portalUrl, "https://other.synthetic.invalid");
  assert.deepEqual(status.deliveryContext, { installationId: IDENTITY, keyAlias: ALIAS, portalUrl: "https://other.synthetic.invalid" });
});

test("invalid_offers_are_refused", async (t) => {
  const f = await fixture(t);
  const before = await contents(f.database);
  const operations: string[] = [];
  const enrollment = f.make({ database: logging(f.database, operations) });
  const canonical = text(OFFER);
  const offers: EnrollmentOffer[] = [
    scanned(new Uint8Array()),
    scanned(utf8("not json")),
    scanned(utf8(` ${canonical}`)),
    scanned(utf8(canonical.replace("{", "{ "))),
    scanned(utf8(canonical.replace('"wire_version":1', '"wire_version":2'))),
    scanned(utf8(canonical.replace(CODE, CODE.toLowerCase()))),
    scanned(utf8(canonical.replace(PORTAL_URL, `${PORTAL_URL}/`))),
    scanned(advertisement(1)),
    scanned(CONFIRMATION),
    scanned(retryable()),
    typed(`${PORTAL_URL}/`),
    typed("http://Portal.synthetic.invalid"),
    typed("http://portal.synthetic.invalid/api"),
    typed("http://portal.synthetic.invalid?code=1"),
    typed("ftp://portal.synthetic.invalid"),
    typed("portal.synthetic.invalid"),
    typed(` ${PORTAL_URL}`),
    typed(""),
    typed(PORTAL_URL, CODE.toLowerCase()),
    typed(PORTAL_URL, CODE.slice(1)),
    typed(PORTAL_URL, `${CODE}A`),
    typed(PORTAL_URL, "SYNTHETICCODE018"),
    typed(PORTAL_URL, ` ${CODE}`),
    typed(PORTAL_URL, ""),
  ];
  for (const offer of offers) {
    assert.deepEqual(await enrollment.enroll(offer), { outcome: "refused", failure: "offer-invalid" }, JSON.stringify(offer));
  }
  assert.deepEqual(operations, []);
  assert.deepEqual(f.cipher.calls, []);
  assert.deepEqual(f.transport.requests, []);
  assert.deepEqual(await contents(f.database), before);
  assert.equal(await enrollment.status(), null);
});

test("enrollment_requires_a_secure_lock", async (t) => {
  const f = await fixture(t);
  const before = await contents(f.database);
  f.cipher.secure = false;
  assert.deepEqual(await f.sender.enroll(scanned()), { outcome: "refused", failure: "screen-lock-required" });
  // Only `true` is a secure lock.
  for (const answer of ["true", 1, null, undefined, {}]) {
    f.cipher.script.set("isDeviceSecure", [{ returns: answer }]);
    assert.deepEqual(await f.sender.enroll(typed()), { outcome: "refused", failure: "screen-lock-required" }, String(answer));
  }
  assert.deepEqual(f.cipher.methods(), Array(6).fill("isDeviceSecure"));
  assert.deepEqual(f.transport.requests, []);
  assert.deepEqual(await contents(f.database), before);
  assert.equal(await f.base.keyStatus(ALIAS), "missing");
  // With the lock set, the same offer enrolls.
  f.cipher.secure = true;
  assert.deepEqual(await f.sender.enroll(scanned()), { outcome: "enrolled", generation: 1 });
});

test("an_existing_enrollment_is_refused", async (t) => {
  for (const phase of PHASES) {
    const f = await fixture(t);
    await toPhase(f, phase);
    const before = await contents(f.database);
    const requests = f.transport.requests.length;
    f.cipher.calls.length = 0;
    // The existing enrollment is found before the screen lock is read.
    f.cipher.secure = false;
    assert.deepEqual(await f.sender.enroll(typed()), { outcome: "refused", failure: "already-enrolled" }, phase);
    f.cipher.secure = true;
    assert.deepEqual(await f.sender.enroll(scanned(offerBytes({ enrollment_code: "ZZZZZZZZ22222222" }))),
      { outcome: "refused", failure: "already-enrolled" }, phase);
    // The offer is checked first.
    assert.deepEqual(await f.sender.enroll(typed(PORTAL_URL, "short")), { outcome: "refused", failure: "offer-invalid" }, phase);
    assert.deepEqual(f.cipher.calls, [], phase);
    assert.equal(f.transport.requests.length, requests, phase);
    assert.deepEqual(await contents(f.database), before, phase);
    assert.equal((await currentStatus(f)).installationId, IDENTITY);
  }
});

test("the_identity_fingerprint_and_key_are_recorded", async (t) => {
  const f = await fixture(t);
  const before = await contents(f.database);
  f.transport.respond = respondWith(retryable());
  assert.deepEqual(await f.sender.enroll(scanned()), { outcome: "retryable", failure: "temporarily-unavailable" });
  assert.equal(await f.base.keyStatus(ALIAS), "present");
  assert.deepEqual(f.cipher.calls, [{ method: "isDeviceSecure" }, { method: "create", alias: ALIAS }]);
  assert.deepEqual(await contents(f.database), withTables(before, { sender_enrollment: [row()] }));
  assert.deepEqual(await f.sender.status(), statusOf());
  // A key that cannot be created writes nothing and sends nothing.
  for (const reason of ["key-exists", "unusable-key", "keystore-unavailable"]) {
    const g = await fixture(t);
    g.cipher.script.set("create", [{ returns: { status: "error", reason } }]);
    assert.deepEqual(await g.sender.enroll(scanned()), { outcome: "cipher-failed", failure: reason });
    assert.deepEqual(g.transport.requests, []);
    assert.deepEqual(await contents(g.database), before);
  }
  const g = await fixture(t);
  for (const returns of [undefined, null, "ok", { status: "error", reason: "synthetic" }]) {
    g.cipher.script.set("create", [{ returns }]);
    assert.deepEqual(await g.sender.enroll(scanned()), { outcome: "cipher-failed", failure: "keystore-unavailable" }, String(returns));
  }
  assert.deepEqual(g.transport.requests, []);
  assert.deepEqual(await contents(g.database), before);
  // Recording that fails deletes the key it created, and sends nothing.
  const h = await fixture(t);
  const { database } = await h.writeFailing();
  assert.deepEqual(await h.make({ database }).enroll(scanned()), { outcome: "storage-failed" });
  assert.deepEqual(h.cipher.calls, [
    { method: "isDeviceSecure" }, { method: "create", alias: ALIAS }, { method: "delete", alias: ALIAS },
  ]);
  assert.equal(await h.base.keyStatus(ALIAS), "missing");
  assert.deepEqual(h.transport.requests, []);
  assert.deepEqual(await contents(h.database), before);
});

// R4 — Confirmation attempts

test("the_confirmation_is_sent_exactly_and_identically", async (t) => {
  assert.equal(CONFIRMATION_ROUTE, "/v1/enrollment-confirmation");
  assert.equal(CAPABILITIES_ROUTE, "/v1/capabilities");
  assert.equal(ENROLLMENT_TIMEOUT_MS, 30_000);
  assert.equal(MAX_ANSWER_BYTES, 65_536);
  const f = await fixture(t);
  const answers: unknown[] = [{ outcome: "failed" }, { outcome: "failed" }, { outcome: "response", body: retryable() }];
  f.transport.respond = () => answers.shift() ?? { outcome: "response", body: advertisement(1) };
  f.transport.vandalize = true;
  assert.deepEqual(await f.sender.enroll(typed()), { outcome: "retryable", failure: "transport-failed" });
  assert.deepEqual(await f.sender.confirm(), { outcome: "retryable", failure: "transport-failed" });
  // The stored bytes survive a restart.
  await f.reopen();
  assert.deepEqual(await f.sender.confirm(), { outcome: "retryable", failure: "temporarily-unavailable" });
  assert.deepEqual(await f.sender.confirm(), { outcome: "enrolled", generation: 1 });
  assert.deepEqual(f.transport.requests, Array(4).fill(confirmationRequest()));
  const decoded = decodeMessage(f.transport.requests[0].body as Uint8Array);
  assert.deepEqual(decoded, {
    wire_version: 1, kind: "enrollment-confirmation", enrollment_code: CODE, sender_installation: IDENTITY, fingerprint: FINGERPRINT,
  });
  assert.deepEqual((await enrollmentRows(f.database))[0].confirmation, CONFIRMATION);
});

test("an_advertisement_enrolls", async (t) => {
  const f = await fixture(t);
  const advertised = advertisement(3, { wire_versions: [1, 2] });
  f.transport.respond = respondWith(retryable());
  await f.sender.enroll(scanned());
  f.wall = NOW + 5_000;
  f.transport.respond = respondWith(advertised);
  assert.deepEqual(await f.sender.confirm(), { outcome: "enrolled", generation: 3 });
  assert.deepEqual(await enrollmentRows(f.database), [row({
    phase: "enrolled", advertisement: advertised, generation: 3, acknowledgement_sequence: 1, adopted_at_ms: NOW + 5_000,
  })]);
  assert.deepEqual(await f.sender.status(), statusOf({ phase: "enrolled", stagingContext: staging(3) }));
  // An advertisement listing only Version 1 enrolls at once.
  const g = await fixture(t);
  g.transport.respond = respondWith(advertisement(7));
  assert.deepEqual(await g.sender.enroll(typed()), { outcome: "enrolled", generation: 7 });
  assert.deepEqual(await enrollmentRows(g.database), [row({
    phase: "enrolled", advertisement: advertisement(7), generation: 7, acknowledgement_sequence: 1, adopted_at_ms: NOW,
  })]);
});

test("retryable_answers_keep_confirming", async (t) => {
  const f = await fixture(t);
  await toPhase(f, "confirming");
  const before = await contents(f.database);
  const cases: [string, Respond, string][] = [
    ["temporarily unavailable", respondWith(retryable("temporarily-unavailable")), "temporarily-unavailable"],
    ["storage reserve exhausted", respondWith(retryable("storage-reserve-exhausted")), "temporarily-unavailable"],
    ["no response", () => ({ outcome: "failed" }), "transport-failed"],
    ["a throwing transport", () => new Error(`${SENTINEL} synthetic transport failure`), "transport-failed"],
    ["a rejecting transport", () => Promise.reject(new Error(`${SENTINEL} synthetic rejection`)), "transport-failed"],
    ["no result", () => undefined, "transport-failed"],
    ["no body", () => ({ outcome: "response" }), "transport-failed"],
    ["a text body", () => ({ outcome: "response", body: text(advertisement(1)) }), "transport-failed"],
    ["another outcome", () => ({ outcome: "received", body: advertisement(1) }), "transport-failed"],
  ];
  for (const [name, respond, failure] of cases) {
    f.transport.respond = respond;
    assert.deepEqual(await f.sender.confirm(), { outcome: "retryable", failure }, name);
    assert.deepEqual(await contents(f.database), before);
  }
  assert.equal((await currentStatus(f)).phase, "confirming");
  assert.deepEqual(f.cipher.methods(), ["isDeviceSecure", "create"]);
});

test("other_answers_refuse_the_enrollment", async (t) => {
  const canonical = text(advertisement(1));
  const answers: [string, Uint8Array][] = [
    ["empty", new Uint8Array()],
    ["proxy page", utf8("<html><body>502 Bad Gateway</body></html>")],
    ["noncanonical advertisement", utf8(` ${canonical}`)],
    ["another wire version", utf8(canonical.replace('"wire_version":1', '"wire_version":2'))],
    ["a delivery receipt", receipt(acknowledgement(1, 1))],
    ["a delivery block", deliveryBlock(acknowledgement(1, 1), "malformed-message")],
    ["an enrollment offer", OFFER],
    ["the confirmation itself", CONFIRMATION],
    ["an oversized advertisement", advertisementOfSize(1, MAX_ANSWER_BYTES + 1)],
    ["oversized zeros", new Uint8Array(MAX_ANSWER_BYTES + 1)],
  ];
  for (const [name, body] of answers) {
    const f = await fixture(t);
    const empty = await contents(f.database);
    f.transport.respond = respondWith(body);
    assert.deepEqual(await f.sender.enroll(scanned()), { outcome: "portal-refused", failure: "enrollment-refused" }, name);
    assert.deepEqual(await contents(f.database), withTables(empty, {
      sender_enrollment: [row({ phase: "refused", refusal: "enrollment-refused" })],
    }), name);
    assert.deepEqual(await f.sender.status(), statusOf({ phase: "refused", refusal: "enrollment-refused" }), name);
    assert.equal(f.cipher.methods().includes("seal"), false, name);
  }
  // An advertisement of exactly the largest answer enrolls.
  const f = await fixture(t);
  f.transport.respond = respondWith(advertisementOfSize(1, MAX_ANSWER_BYTES));
  assert.deepEqual(await f.sender.enroll(scanned()), { outcome: "enrolled", generation: 1 });
  // A refusal after a retryable attempt.
  const g = await fixture(t);
  await toPhase(g, "confirming");
  g.transport.respond = respondWith(utf8("Service Unavailable"));
  assert.deepEqual(await g.sender.confirm(), { outcome: "portal-refused", failure: "enrollment-refused" });
  assert.deepEqual(await enrollmentRows(g.database), [row({ phase: "refused", refusal: "enrollment-refused" })]);
});

test("an_unsupported_advertisement_is_refused", async (t) => {
  for (const wireVersions of [[2], [2, 3]]) {
    const f = await fixture(t);
    const empty = await contents(f.database);
    f.transport.respond = respondWith(advertisement(1, { wire_versions: wireVersions }));
    assert.deepEqual(await f.sender.enroll(scanned()), { outcome: "portal-refused", failure: "capabilities-unsupported" });
    assert.deepEqual(await contents(f.database), withTables(empty, {
      sender_enrollment: [row({ phase: "refused", refusal: "capabilities-unsupported" })],
    }));
    assert.deepEqual(await f.sender.status(), statusOf({ phase: "refused", refusal: "capabilities-unsupported" }));
    assert.deepEqual(f.cipher.methods(), ["isDeviceSecure", "create"]);
    // A refused enrollment stays refused.
    f.transport.respond = respondWith(advertisement(1));
    assert.deepEqual(await f.sender.confirm(), { outcome: "refused", failure: "not-confirming" });
    assert.equal(f.transport.requests.length, 1);
  }
});

test("confirm_only_while_confirming", async (t) => {
  const f = await fixture(t);
  const empty = await contents(f.database);
  assert.deepEqual(await f.sender.confirm(), { outcome: "refused", failure: "not-confirming" });
  assert.deepEqual(f.transport.requests, []);
  assert.deepEqual(await contents(f.database), empty);
  for (const phase of ["refused", "enrolled", "confirmed", "draining"] as const) {
    const g = await fixture(t);
    await toPhase(g, phase);
    const before = await contents(g.database);
    const requests = g.transport.requests.length;
    assert.deepEqual(await g.sender.confirm(), { outcome: "refused", failure: "not-confirming" }, phase);
    assert.equal(g.transport.requests.length, requests, phase);
    assert.deepEqual(await contents(g.database), before, phase);
  }
});

// R5 — Adopting a capability generation

test("adoption_appends_one_sealed_acknowledgement", async (t) => {
  const f = await fixture(t);
  await appendCheckpoint(f, OTHER_INSTALLATION);
  const before = await contents(f.database);
  f.transport.respond = respondWith(advertisement(2));
  assert.deepEqual(await f.sender.enroll(scanned()), { outcome: "enrolled", generation: 2 });
  const rows = (await outboxMessages(f.database)).filter((entry) => entry.installationId === IDENTITY);
  const expected = acknowledgement(1, 2);
  assert.equal(rows.length, 1);
  assert.deepEqual({ ...rows[0], sealed: null }, {
    installationId: IDENTITY, sequence: 1, kind: "capability-acknowledgement", sealed: null, messageBytes: expected.length,
  });
  assert.deepEqual(await f.base.open(ALIAS, rows[0].sealed, outboxAad(IDENTITY, 1)), { status: "ok", value: expected });
  assert.deepEqual(f.cipher.calls.filter((call) => call.method === "seal"), [{ method: "seal", alias: ALIAS, aad: outboxAad(IDENTITY, 1) }]);
  const previous = before.tables as Record<string, DatabaseRow[]>;
  assert.deepEqual(await contents(f.database), withTables(before, {
    sender_enrollment: [row({ phase: "enrolled", advertisement: advertisement(2), generation: 2, acknowledgement_sequence: 1, adopted_at_ms: NOW })],
    outbox_messages: [...previous.outbox_messages, {
      installation_id: IDENTITY, sequence: 1, kind: "capability-acknowledgement", sealed: rows[0].sealed, message_bytes: expected.length,
    }],
    outbox_high_water: [...previous.outbox_high_water, { installation_id: IDENTITY, last_sequence: 1 }],
  }));
});

test("the_enrollment_acknowledgement_is_the_first_message", async (t) => {
  const f = await fixture(t);
  await toPhase(f, "enrolled");
  assert.deepEqual(await deliverNext(f), { outcome: "delivered", sequence: 1, kind: "capability-acknowledgement" });
  assert.deepEqual(f.outboxRequests.map((request) => request.body), [acknowledgement(1, 1)]);
  // The sequence comes from the high-water mark, and is recorded.
  const g = await fixture(t);
  assert.equal((await g.base.create(ALIAS)).status, "ok");
  for (let index = 0; index < 3; index++) await appendCheckpoint(g);
  assert.equal((await g.base.delete(ALIAS)).status, "ok");
  assert.deepEqual(await g.sender.enroll(scanned()), { outcome: "enrolled", generation: 1 });
  assert.deepEqual(await outboxSequences(g.database), [1, 2, 3, 4]);
  const [stored] = await enrollmentRows(g.database);
  assert.equal(stored.acknowledgement_sequence, 4);
  const ack = (await outboxMessages(g.database)).find((entry) => entry.sequence === 4)!;
  assert.deepEqual(await g.base.open(ALIAS, ack.sealed, outboxAad(IDENTITY, 4)), { status: "ok", value: acknowledgement(4, 1) });
});

test("adoption_failures_write_nothing", async (t) => {
  const f = await fixture(t);
  await toPhase(f, "confirming");
  f.transport.respond = respondWith(advertisement(1));
  const before = await contents(f.database);
  for (const reason of ["missing-key", "unusable-key", "keystore-unavailable"]) {
    f.cipher.script.set("seal", [{ returns: { status: "error", reason } }]);
    assert.deepEqual(await f.sender.confirm(), { outcome: "cipher-failed", failure: reason });
    assert.deepEqual(await contents(f.database), before, reason);
  }
  for (const entry of [{ throws: new Error(`${SENTINEL} synthetic seal failure`) }, { returns: { status: "ok", value: new Uint8Array() } },
    { returns: { status: "ok", value: "sealed" } }, { returns: undefined }]) {
    f.cipher.script.set("seal", [entry]);
    assert.deepEqual(await f.sender.confirm(), { outcome: "cipher-failed", failure: "keystore-unavailable" }, JSON.stringify(entry));
    assert.deepEqual(await contents(f.database), before);
  }
  // The high-water mark moving while sealing writes nothing.
  const { database: other } = await f.second();
  f.cipher.script.set("seal", [{ before: () => appendCheckpoint(f, IDENTITY, other) }]);
  assert.deepEqual(await f.sender.confirm(), { outcome: "storage-failed" });
  const moved = await contents(f.database);
  const interloper = (moved.tables.outbox_messages as DatabaseRow[]).filter((entry) => entry.installation_id === IDENTITY);
  assert.deepEqual(interloper.map((entry) => entry.sequence), [1]);
  assert.deepEqual(moved, withTables(before, {
    outbox_messages: [...before.tables.outbox_messages as DatabaseRow[], ...interloper],
    outbox_high_water: [{ installation_id: IDENTITY, last_sequence: 1 }],
  }));
  // A failed commit, and then each write of the adoption failing in turn, write nothing.
  assert.deepEqual(await f.make({ database: failingWrites(f.database, { commit: true }) }).confirm(), { outcome: "storage-failed" });
  assert.deepEqual(await contents(f.database), moved);
  let writes = 0;
  for (let mutation = 1; mutation <= 10; mutation++) {
    const result = await f.make({ database: failingWrites(f.database, { mutation }) }).confirm();
    if (result.outcome === "enrolled") { writes = mutation - 1; break; }
    assert.deepEqual(result, { outcome: "storage-failed" }, String(mutation));
    assert.deepEqual(await contents(f.database), moved, String(mutation));
  }
  // The acknowledgement, the high-water mark, and the adoption are at least three writes, and then all are there.
  assert.ok(writes >= 3, String(writes));
  assert.deepEqual(await outboxSequences(f.database), [1, 2]);
  assert.equal((await enrollmentRows(f.database))[0].acknowledgement_sequence, 2);
  assert.ok(f.transport.requests.every((request) => request.body instanceof Uint8Array && text(request.body) === text(CONFIRMATION)));
});

// R6 — Later generations

test("a_newer_generation_is_adopted_and_acknowledged", async (t) => {
  const f = await fixture(t);
  await toPhase(f, "confirmed");
  await appendCheckpoint(f);
  await appendCheckpoint(f);
  f.wall = NOW + 60_000;
  const advertised = advertisement(2, {
    projection_schemas: [{ record_type: "heart-rate", versions: [1] }, { record_type: "steps", versions: [1, 2] }],
    max_record_count: 8, max_message_bytes: 131_072,
  });
  f.transport.respond = respondWith(advertised);
  const requests = f.transport.requests.length;
  assert.deepEqual(await f.sender.refreshCapabilities(), { outcome: "adopted", generation: 2 });
  assert.deepEqual(f.transport.requests.slice(requests), [CAPABILITIES_REQUEST]);
  assert.deepEqual(await outboxSequences(f.database), [2, 3, 4]);
  const ack = (await outboxMessages(f.database)).find((entry) => entry.sequence === 4)!;
  assert.equal(ack.kind, "capability-acknowledgement");
  assert.deepEqual(await f.base.open(ALIAS, ack.sealed, outboxAad(IDENTITY, 4)), { status: "ok", value: acknowledgement(4, 2) });
  // The enrollment acknowledgement's sequence is kept, so the learned confirmation stays.
  assert.deepEqual(await enrollmentRows(f.database), [row({
    phase: "enrolled", advertisement: advertised, generation: 2, acknowledgement_sequence: 1, adopted_at_ms: NOW + 60_000,
  })]);
  assert.deepEqual(await f.sender.status(), statusOf({
    phase: "enrolled", confirmed: true,
    stagingContext: staging(2, { maxRecordCount: 8, maxMessageBytes: 131_072, projectionSchemas: { "heart-rate": [1], steps: [1, 2] } }),
  }));
  // Adopted before the first acknowledgement's receipt, a later one leaves confirmation to the first.
  const g = await fixture(t);
  await toPhase(g, "enrolled");
  g.transport.respond = respondWith(advertisement(2));
  assert.deepEqual(await g.sender.refreshCapabilities(), { outcome: "adopted", generation: 2 });
  assert.equal((await currentStatus(g)).confirmed, false);
  assert.deepEqual(await deliverNext(g), { outcome: "delivered", sequence: 1, kind: "capability-acknowledgement" });
  assert.deepEqual(await outboxSequences(g.database), [2]);
  assert.equal((await currentStatus(g)).confirmed, true);
});

test("same_older_and_unusable_generations_change_nothing", async (t) => {
  const f = await fixture(t);
  f.transport.respond = respondWith(advertisement(3));
  await f.sender.enroll(scanned());
  const before = await contents(f.database);
  const canonical = text(advertisement(4));
  const cases: [string, unknown, string][] = [
    ["the same generation", { outcome: "response", body: advertisement(3) }, "current"],
    ["an older generation", { outcome: "response", body: advertisement(2) }, "current"],
    ["an older unusable generation", { outcome: "response", body: advertisement(2, { wire_versions: [2] }) }, "current"],
    ["the same unusable generation", { outcome: "response", body: advertisement(3, { wire_versions: [2] }) }, "current"],
    ["a newer unusable generation", { outcome: "response", body: advertisement(4, { wire_versions: [2] }) }, "incompatible"],
    ["a retryable failure", { outcome: "response", body: retryable() }, "unavailable"],
    ["no response", { outcome: "failed" }, "unavailable"],
    ["a throwing transport", new Error(`${SENTINEL} synthetic transport failure`), "unavailable"],
    ["a malformed result", { outcome: "response", body: canonical }, "unavailable"],
    ["an empty body", { outcome: "response", body: new Uint8Array() }, "unavailable"],
    ["a proxy page", { outcome: "response", body: utf8("<html>504</html>") }, "unavailable"],
    ["a noncanonical advertisement", { outcome: "response", body: utf8(` ${canonical}`) }, "unavailable"],
    ["an oversized advertisement", { outcome: "response", body: advertisementOfSize(4, MAX_ANSWER_BYTES + 1) }, "unavailable"],
    ["another message", { outcome: "response", body: OFFER }, "unavailable"],
  ];
  for (const [name, answer, outcome] of cases) {
    f.transport.respond = () => answer;
    assert.deepEqual(await f.sender.refreshCapabilities(), { outcome }, name);
    assert.deepEqual(await contents(f.database), before, name);
  }
  assert.equal(f.cipher.methods().filter((method) => method === "seal").length, 1);
  assert.equal((await currentStatus(f)).stagingContext?.generation.generation, 3);
  // An advertisement of exactly the largest answer is read.
  f.transport.respond = respondWith(advertisementOfSize(4, MAX_ANSWER_BYTES));
  assert.deepEqual(await f.sender.refreshCapabilities(), { outcome: "adopted", generation: 4 });
});

test("refresh_needs_an_enrollment_and_stops_while_draining", async (t) => {
  const f = await fixture(t);
  assert.deepEqual(await f.sender.refreshCapabilities(), { outcome: "refused", failure: "not-enrolled" });
  assert.deepEqual(f.transport.requests, []);
  for (const phase of ["confirming", "refused", "draining"] as const) {
    const g = await fixture(t);
    await toPhase(g, phase);
    const before = await contents(g.database);
    const requests = g.transport.requests.length;
    g.transport.respond = respondWith(advertisement(2));
    assert.deepEqual(await g.sender.refreshCapabilities(),
      phase === "draining" ? { outcome: "draining" } : { outcome: "refused", failure: "not-enrolled" }, phase);
    assert.equal(g.transport.requests.length, requests, phase);
    assert.deepEqual(await contents(g.database), before, phase);
  }
  // An enrollment the Operator has not yet confirmed may adopt.
  const g = await fixture(t);
  await toPhase(g, "enrolled");
  g.transport.respond = respondWith(advertisement(2));
  assert.deepEqual(await g.sender.refreshCapabilities(), { outcome: "adopted", generation: 2 });
});

// R7 — The staging and delivery contexts

async function initialAttempt(database: SenderDatabase): Promise<SnapshotAttempt> {
  const issuer = { async issue() { return { outcome: "issued" as const, changesToken: "synthetic-changes-token-0", unreadSinceMs: 7 }; } };
  const result = await beginInitialSnapshot(database, issuer, { recordType: "steps", installationId: IDENTITY },
    { randomUUID: () => uuid(0xa1), now: () => NOW });
  assert.equal(result.outcome, "begun", JSON.stringify(result));
  return (result as { attempt: SnapshotAttempt }).attempt;
}

test("the_contexts_come_from_the_adopted_generation", async (t) => {
  const f = await fixture(t);
  await toPhase(f, "confirming");
  assert.deepEqual(await f.sender.status(), statusOf());
  f.transport.respond = respondWith(advertisement(5, {
    projection_schemas: [{ record_type: "heart-rate", versions: [1, 2] }, { record_type: "steps", versions: [1] }],
    max_record_count: 250, max_message_bytes: 1_048_576,
  }));
  assert.deepEqual(await f.sender.confirm(), { outcome: "enrolled", generation: 5 });
  const status = await currentStatus(f);
  assert.deepEqual(status.stagingContext, {
    installationId: IDENTITY, keyAlias: ALIAS,
    generation: { generation: 5, maxRecordCount: 250, maxMessageBytes: 1_048_576, projectionSchemas: { "heart-rate": [1, 2], steps: [1] } },
  });
  assert.deepEqual(status.deliveryContext, { installationId: IDENTITY, keyAlias: ALIAS, portalUrl: PORTAL_URL });
  // The published stage and delivery units accept them.
  assert.deepEqual(await deliverNext(f), { outcome: "delivered", sequence: 1, kind: "capability-acknowledgement" });
  const attempt = await initialAttempt(f.database);
  const page: ObservedPage = {
    source: "snapshot", recordType: "steps", cycleId: attempt.snapshotId, projectionSchemaVersion: 1, pageIndex: 1,
    readFrom: null, projections: [utf8('{"synthetic":1}')], nextPosition: utf8("position-1"), exhausted: true,
  };
  const staged = await stageOutboxBatch(f.database, f.base, status.stagingContext!, page);
  assert.equal(staged.outcome, "staged", JSON.stringify(staged));
  assert.equal((await stageSyncCompletion(f.database, f.base, status.stagingContext!, { recordType: "steps", cycleId: attempt.snapshotId })).outcome, "staged");
  assert.deepEqual(await deliverNext(f), { outcome: "delivered", sequence: 2, kind: "ingest-batch" });
  assert.deepEqual(await deliverNext(f), { outcome: "delivered", sequence: 3, kind: "snapshot-completion" });
  for (const request of f.outboxRequests) assert.equal(decodeMessage(request.body).capability_generation, 5);
});

test("confirmation_is_learned_from_the_acknowledgement_receipt", async (t) => {
  const f = await fixture(t);
  await toPhase(f, "enrolled");
  // A pending installation's deliveries stay retryable, so its acknowledgement stays queued.
  let confirmedByOperator = false;
  f.outboxAnswer = (body) => confirmedByOperator ? receipt(body) : retryable();
  assert.equal((await currentStatus(f)).confirmed, false);
  assert.deepEqual(await deliverNext(f), { outcome: "retryable", sequence: 1, failure: "temporarily-unavailable" });
  assert.equal((await currentStatus(f)).confirmed, false);
  // A Delivery Block does not remove it either.
  f.outboxAnswer = (body) => deliveryBlock(body, "unsupported-wire-protocol");
  assert.equal((await deliverNext(f)).outcome, "blocked");
  assert.equal((await currentStatus(f)).confirmed, false);
  confirmedByOperator = true;
  f.outboxAnswer = (body) => confirmedByOperator ? receipt(body) : retryable();
  assert.deepEqual(await deliverNext(f), { outcome: "delivered", sequence: 1, kind: "capability-acknowledgement" });
  assert.deepEqual(await f.sender.status(), statusOf({ phase: "enrolled", confirmed: true, stagingContext: staging(1) }));
  await f.reopen();
  assert.equal((await currentStatus(f)).confirmed, true);
  // Both the removal and a high-water mark at the acknowledgement are needed.
  const g = await fixture(t);
  await toPhase(g, "confirmed");
  const { database } = await g.second();
  await database.execute("DELETE FROM outbox_high_water WHERE installation_id = ?", [IDENTITY]);
  assert.equal((await currentStatus(g)).confirmed, false);
  // Another installation's removal never confirms this one.
  const h = await fixture(t);
  await toPhase(h, "enrolled");
  assert.equal((await h.base.create(`activity-tracker-portal.installation.${OTHER_INSTALLATION}`)).status, "ok");
  await appendRaw(h, "capability-acknowledgement", acknowledgement(1, 1, OTHER_INSTALLATION),
    { installation: OTHER_INSTALLATION, alias: `activity-tracker-portal.installation.${OTHER_INSTALLATION}` });
  const other = createOutboxSender({
    database: h.database, cipher: h.base, now: () => NOW, notifier: { notify: async () => {} },
    transport: { send: async (request) => ({ outcome: "response", body: receipt(request.body) }) },
    context: { installationId: OTHER_INSTALLATION, keyAlias: `activity-tracker-portal.installation.${OTHER_INSTALLATION}`, portalUrl: PORTAL_URL },
  });
  assert.equal((await other.sendNext()).outcome, "delivered");
  assert.equal((await currentStatus(h)).confirmed, false);
});

test("status_writes_nothing", async (t) => {
  const f = await fixture(t);
  const empty = await contents(f.database);
  for (let read = 0; read < 3; read++) assert.equal(await f.sender.status(), null);
  assert.deepEqual(await contents(f.database), empty);
  for (const phase of PHASES) {
    const g = await fixture(t);
    await toPhase(g, phase);
    await appendCheckpoint(g);
    const before = await contents(g.database);
    const first = await g.sender.status();
    for (let read = 0; read < 3; read++) assert.deepEqual(await g.sender.status(), first, phase);
    assert.deepEqual(await contents(g.database), before, phase);
    assert.equal(g.transport.requests.length, 1, phase);
    for (const failure of [new Error(`${SENTINEL} synthetic storage failure`), new TypeError(`${SENTINEL} synthetic storage value`)]) {
      assert.deepEqual(await g.make({ database: brokenDatabase(failure) }).status(), { outcome: "storage-failed" });
    }
  }
});

// R8 — Draining

test("draining_needs_a_confirmed_enrollment", async (t) => {
  const f = await fixture(t);
  assert.deepEqual(await f.sender.beginDrain(), { outcome: "refused", failure: "not-enrolled" });
  for (const [phase, failure] of [["confirming", "not-enrolled"], ["refused", "not-enrolled"], ["enrolled", "not-confirmed"]] as const) {
    const g = await fixture(t);
    await toPhase(g, phase);
    const before = await contents(g.database);
    assert.deepEqual(await g.sender.beginDrain(), { outcome: "refused", failure }, phase);
    assert.deepEqual(await contents(g.database), before, phase);
  }
  const g = await fixture(t);
  await toPhase(g, "confirmed");
  await appendCheckpoint(g);
  const before = await contents(g.database);
  assert.deepEqual(await g.sender.beginDrain(), { outcome: "draining" });
  const drained = withTables(before, {
    sender_enrollment: [row({ phase: "draining", advertisement: advertisement(1), generation: 1, acknowledgement_sequence: 1, adopted_at_ms: NOW })],
  });
  assert.deepEqual(await contents(g.database), drained);
  assert.deepEqual(await g.sender.beginDrain(), { outcome: "draining" });
  assert.deepEqual(await contents(g.database), drained);
  assert.deepEqual(await g.sender.status(), statusOf({ phase: "draining", confirmed: true, stagingContext: staging(1) }));
});

test("the_final_sequence_appears_once_the_outbox_is_empty", async (t) => {
  const f = await fixture(t);
  await toPhase(f, "confirmed");
  await appendCheckpoint(f);
  await appendCheckpoint(f);
  await appendCheckpoint(f, OTHER_INSTALLATION);
  assert.deepEqual(await f.sender.beginDrain(), { outcome: "draining" });
  assert.equal((await currentStatus(f)).finalSequence, null);
  assert.deepEqual(await deliverNext(f), { outcome: "delivered", sequence: 2, kind: "sync-checkpoint" });
  assert.equal((await currentStatus(f)).finalSequence, null);
  assert.deepEqual(await deliverNext(f), { outcome: "delivered", sequence: 3, kind: "sync-checkpoint" });
  // Another installation's queued message is not this installation's.
  assert.deepEqual(await outboxSequences(f.database, OTHER_INSTALLATION), [1]);
  assert.deepEqual(await f.sender.status(), statusOf({ phase: "draining", confirmed: true, stagingContext: staging(1), finalSequence: 3 }));
  // An Outbox already empty when draining begins reports at once.
  const g = await fixture(t);
  await toPhase(g, "draining");
  assert.equal((await currentStatus(g)).finalSequence, 1);
  // Only draining reports a final sequence.
  const h = await fixture(t);
  await toPhase(h, "confirmed");
  assert.equal((await currentStatus(h)).finalSequence, null);
});

test("draining_survives_a_restart", async (t) => {
  const f = await fixture(t);
  await toPhase(f, "confirmed");
  await appendCheckpoint(f);
  assert.deepEqual(await f.sender.beginDrain(), { outcome: "draining" });
  await f.reopen();
  assert.deepEqual(await f.sender.status(), statusOf({ phase: "draining", confirmed: true, stagingContext: staging(1) }));
  assert.deepEqual(await f.sender.beginDrain(), { outcome: "draining" });
  assert.deepEqual(await f.sender.refreshCapabilities(), { outcome: "draining" });
  assert.deepEqual(await f.sender.confirm(), { outcome: "refused", failure: "not-confirming" });
  assert.deepEqual(await deliverNext(f), { outcome: "delivered", sequence: 2, kind: "sync-checkpoint" });
  await f.reopen();
  assert.equal((await currentStatus(f)).finalSequence, 2);
});

// R9 — Unlinking and destructive reset

test("unlink_needs_a_complete_drain", async (t) => {
  const f = await fixture(t);
  assert.deepEqual(await f.sender.unlink(), { outcome: "refused", failure: "not-drained" });
  for (const phase of PHASES) {
    const g = await fixture(t);
    await toPhase(g, phase);
    if (phase === "draining") await appendCheckpoint(g);
    const before = await contents(g.database);
    g.cipher.calls.length = 0;
    assert.deepEqual(await g.sender.unlink(), { outcome: "refused", failure: "not-drained" }, phase);
    assert.deepEqual(g.cipher.calls, [], phase);
    assert.deepEqual(await contents(g.database), before, phase);
    assert.equal(await g.base.keyStatus(ALIAS), "present", phase);
    if (phase === "draining") {
      assert.equal((await deliverNext(g)).outcome, "delivered");
      assert.deepEqual(await g.sender.unlink(), { outcome: "destroyed" });
      assert.equal(await g.sender.status(), null);
      assert.equal(await g.base.keyStatus(ALIAS), "missing");
      assert.deepEqual(await contents(g.database), emptied(before));
    }
  }
});

test("reset_destroys_in_any_phase", async (t) => {
  for (const phase of PHASES) {
    const f = await fixture(t);
    await toPhase(f, phase);
    if (phase !== "confirming" && phase !== "refused") await appendCheckpoint(f);
    await appendCheckpoint(f, OTHER_INSTALLATION);
    const before = await contents(f.database);
    assert.deepEqual(await f.sender.reset({ acknowledgePossibleLoss: true }), { outcome: "destroyed" }, phase);
    assert.equal(await f.sender.status(), null, phase);
    assert.equal(await f.base.keyStatus(ALIAS), "missing", phase);
    assert.deepEqual(await contents(f.database), emptied(before), phase);
  }
  // Without an enrollment there is no key to delete, and every row still goes.
  const f = await fixture(t);
  await appendCheckpoint(f, OTHER_INSTALLATION);
  const before = await contents(f.database);
  assert.deepEqual(await f.sender.reset({ acknowledgePossibleLoss: true }), { outcome: "destroyed" });
  assert.deepEqual(f.cipher.calls, []);
  assert.equal(await f.base.keyStatus(OTHER_KEY_ALIAS), "present");
  assert.deepEqual(await contents(f.database), emptied(before));
});

test("a_new_installation_can_enroll_after_destruction", async (t) => {
  const f = await fixture(t);
  await toPhase(f, "draining");
  assert.deepEqual(await f.sender.unlink(), { outcome: "destroyed" });
  assert.deepEqual(await f.sender.enroll(typed()), { outcome: "enrolled", generation: 1 });
  const second = `activity-tracker-portal.installation.${SECOND_IDENTITY}`;
  assert.deepEqual(await f.sender.status(), statusOf({
    installationId: SECOND_IDENTITY, fingerprint: installationFingerprint(SECOND_IDENTITY), keyAlias: second, phase: "enrolled",
    stagingContext: staging(1, {}, SECOND_IDENTITY), deliveryContext: { installationId: SECOND_IDENTITY, keyAlias: second, portalUrl: PORTAL_URL },
  }));
  assert.deepEqual(f.transport.requests.at(-1), confirmationRequest(SECOND_IDENTITY));
  assert.deepEqual(await outboxSequences(f.database, SECOND_IDENTITY), [1]);
  assert.equal(await f.base.keyStatus(ALIAS), "missing");
  assert.equal(await f.base.keyStatus(second), "present");
  assert.deepEqual(await f.sender.reset({ acknowledgePossibleLoss: true }), { outcome: "destroyed" });
  assert.equal(await f.base.keyStatus(second), "missing");
  assert.deepEqual(await f.sender.enroll(scanned()), { outcome: "enrolled", generation: 1 });
  assert.equal((await currentStatus(f)).installationId, THIRD_IDENTITY);
});

// R10 — Destruction

/** Tables a later unit might add: each pair's parent sorts, and is created, on a different side of its child. */
async function laterUnitTables(database: SenderDatabase): Promise<void> {
  await database.execute("CREATE TABLE a_parent (id INTEGER PRIMARY KEY NOT NULL) STRICT");
  await database.execute("CREATE TABLE z_child (id INTEGER PRIMARY KEY NOT NULL, parent INTEGER NOT NULL REFERENCES a_parent (id)) STRICT");
  await database.execute("CREATE TABLE b_child (id INTEGER PRIMARY KEY NOT NULL, parent INTEGER NOT NULL REFERENCES y_parent (id)) STRICT");
  await database.execute("CREATE TABLE y_parent (id INTEGER PRIMARY KEY NOT NULL) STRICT");
  await database.execute("INSERT INTO a_parent (id) VALUES (1)");
  await database.execute("INSERT INTO z_child (id, parent) VALUES (1, 1)");
  await database.execute("INSERT INTO y_parent (id) VALUES (1)");
  await database.execute("INSERT INTO b_child (id, parent) VALUES (1, 1)");
}

test("destruction_deletes_the_key_then_every_row", async (t) => {
  const f = await fixture(t);
  await laterUnitTables(f.database);
  await toPhase(f, "confirmed");
  // Rows in the stage and delivery units' tables, and another installation's.
  const status = await currentStatus(f);
  const attempt = await initialAttempt(f.database);
  assert.equal((await stageOutboxBatch(f.database, f.base, status.stagingContext!, {
    source: "snapshot", recordType: "steps", cycleId: attempt.snapshotId, projectionSchemaVersion: 1, pageIndex: 1,
    readFrom: null, projections: [utf8('{"synthetic":1}')], nextPosition: utf8("position-1"), exhausted: true,
  })).outcome, "staged");
  assert.equal((await stageSyncCompletion(f.database, f.base, status.stagingContext!, { recordType: "steps", cycleId: attempt.snapshotId })).outcome, "staged");
  assert.equal((await stageSyncCheckpoint(f.database, f.base, status.stagingContext!, {
    runSequence: 1, trigger: "manual", startedAtMs: NOW, endedAtMs: NOW + 30_000, backgroundScheduling: "available",
    recordTypes: [{ recordType: "steps", outcome: "completed", sourceDataStaged: true }],
  })).outcome, "staged");
  f.outboxAnswer = (body) => deliveryBlock(body, "unsupported-source-projection-schema");
  assert.equal((await deliverNext(f)).outcome, "blocked");
  await appendCheckpoint(f, OTHER_INSTALLATION);
  const before = await contents(f.database);
  const populated = Object.entries(before.tables).filter(([, rows]) => (rows as unknown[]).length > 0).map(([name]) => name).sort();
  assert.deepEqual(populated, [
    "a_parent", "b_child", "current_snapshot_attempts", "cycle_completions", "cycle_progress", "outbox_head_block", "outbox_high_water",
    "outbox_messages", "run_checkpoints", "sender_enrollment", "snapshot_attempts", "y_parent", "z_child",
  ]);
  const operations: string[] = [];
  f.events.length = 0;
  const enrollment = f.make({ database: logging(f.database, operations, f.events) });
  assert.deepEqual(await enrollment.reset({ acknowledgePossibleLoss: true }), { outcome: "destroyed" });
  assert.deepEqual(f.cipher.calls.filter((call) => call.method === "delete"), [{ method: "delete", alias: ALIAS }]);
  const deletion = f.events.indexOf("cipher.delete");
  const writes = f.events.map((event, index) => [event, index] as const)
    .filter(([event]) => event.startsWith("storage:") && MUTATION.test(event.slice("storage:".length)));
  assert.ok(writes.length > 0);
  assert.ok(writes.every(([, index]) => index > deletion), f.events.join("\n"));
  assert.equal(await f.base.keyStatus(ALIAS), "missing");
  assert.equal(await f.base.keyStatus(OTHER_KEY_ALIAS), "present");
  assert.deepEqual(await contents(f.database), emptied(before));
});

test("an_unavailable_keystore_destroys_nothing", async (t) => {
  for (const action of ["reset", "unlink"] as const) {
    const f = await fixture(t);
    await toPhase(f, "draining");
    const before = await contents(f.database);
    const destroy = () => action === "reset" ? f.sender.reset({ acknowledgePossibleLoss: true }) : f.sender.unlink();
    const entries: Script[] = [
      { returns: { status: "error", reason: "keystore-unavailable" } }, { throws: new Error(`${SENTINEL} synthetic keystore failure`) },
      { returns: undefined }, { returns: { status: "error", reason: "synthetic" } },
    ];
    for (const entry of entries) {
      f.cipher.script.set("delete", [entry]);
      assert.deepEqual(await destroy(), { outcome: "cipher-failed", failure: "keystore-unavailable" }, `${action} ${JSON.stringify(entry)}`);
      assert.deepEqual(await contents(f.database), before, action);
      assert.equal(await f.base.keyStatus(ALIAS), "present", action);
    }
    assert.equal((await currentStatus(f)).phase, "draining");
  }
  // A missing or unusable key counts as deleted.
  for (const reason of ["missing-key", "unusable-key"]) {
    const f = await fixture(t);
    await toPhase(f, "enrolled");
    const before = await contents(f.database);
    f.cipher.script.set("delete", [{ returns: { status: "error", reason } }]);
    assert.deepEqual(await f.sender.reset({ acknowledgePossibleLoss: true }), { outcome: "destroyed" }, reason);
    assert.deepEqual(await contents(f.database), emptied(before), reason);
  }
});

test("a_failed_deletion_completes_on_retry", async (t) => {
  for (const action of ["unlink", "reset"] as const) {
    const f = await fixture(t);
    await toPhase(f, "draining");
    const before = await contents(f.database);
    const destroy = (enrollment: SenderEnrollment) => action === "reset" ? enrollment.reset({ acknowledgePossibleLoss: true }) : enrollment.unlink();
    const { database } = await f.writeFailing();
    assert.deepEqual(await destroy(f.make({ database })), { outcome: "storage-failed" }, action);
    // The key went first, and the rows stay until a repeated request.
    assert.equal(await f.base.keyStatus(ALIAS), "missing", action);
    assert.deepEqual(await contents(f.database), before, action);
    assert.deepEqual(await destroy(f.make({ database: failingWrites(f.database, { commit: true }) })), { outcome: "storage-failed" }, action);
    assert.deepEqual(await contents(f.database), before, action);
    for (const mutation of [1, 2, 3]) {
      assert.deepEqual(await destroy(f.make({ database: failingWrites(f.database, { mutation }) })), { outcome: "storage-failed" }, `${action} ${mutation}`);
      assert.deepEqual(await contents(f.database), before, `${action} ${mutation}`);
    }
    assert.deepEqual(await destroy(f.sender), { outcome: "destroyed" }, action);
    assert.deepEqual(await contents(f.database), emptied(before), action);
    assert.equal(await f.sender.status(), null);
  }
});

// R11 — Storage and cipher failures

test("storage_failures_change_nothing", async (t) => {
  const f = await fixture(t);
  await toPhase(f, "confirmed");
  const before = await contents(f.database);
  const requests = f.transport.requests.length;
  f.cipher.calls.length = 0;
  for (const failure of [new Error(`${SENTINEL} synthetic storage failure`), new TypeError(`${SENTINEL} synthetic storage value`)]) {
    const broken = f.make({ database: brokenDatabase(failure) });
    assert.deepEqual(await broken.enroll(scanned()), { outcome: "storage-failed" });
    assert.deepEqual(await broken.confirm(), { outcome: "storage-failed" });
    assert.deepEqual(await broken.refreshCapabilities(), { outcome: "storage-failed" });
    assert.deepEqual(await broken.beginDrain(), { outcome: "storage-failed" });
    assert.deepEqual(await broken.unlink(), { outcome: "storage-failed" });
    assert.deepEqual(await broken.reset({ acknowledgePossibleLoss: true }), { outcome: "storage-failed" });
    assert.deepEqual(await broken.status(), { outcome: "storage-failed" });
  }
  assert.equal(f.transport.requests.length, requests);
  assert.deepEqual(f.cipher.calls, []);
  assert.deepEqual(await contents(f.database), before);
  // Writes that fail once the Portal has answered, or when draining, change nothing.
  const { database, failing } = await f.writeFailing();
  failing(false);
  const writes = f.make({ database });
  f.transport.respond = () => { failing(true); return { outcome: "response", body: advertisement(2) }; };
  assert.deepEqual(await writes.refreshCapabilities(), { outcome: "storage-failed" });
  assert.deepEqual(await contents(f.database), before);
  failing(true);
  assert.deepEqual(await writes.beginDrain(), { outcome: "storage-failed" });
  assert.deepEqual(await contents(f.database), before);
  const g = await fixture(t);
  await toPhase(g, "confirming");
  const confirming = await contents(g.database);
  const second = await g.writeFailing();
  second.failing(false);
  for (const body of [new Uint8Array(), advertisement(1, { wire_versions: [2] }), advertisement(1)]) {
    g.transport.respond = () => { second.failing(true); return { outcome: "response", body }; };
    assert.deepEqual(await g.make({ database: second.database }).confirm(), { outcome: "storage-failed" });
    assert.deepEqual(await contents(g.database), confirming);
    second.failing(false);
  }
  assert.deepEqual(await g.sender.confirm(), { outcome: "enrolled", generation: 1 });
});

test("throwing_cipher_calls_read_as_unavailable", async (t) => {
  const error = new Error(`${SENTINEL} synthetic cipher failure`);
  const unavailable = { outcome: "cipher-failed", failure: "keystore-unavailable" };
  for (const method of ["isDeviceSecure", "create"] as const) {
    for (const synchronous of [false, true]) {
      const f = await fixture(t);
      const before = await contents(f.database);
      const cipher = {
        create: f.cipher.create.bind(f.cipher), seal: f.cipher.seal.bind(f.cipher), delete: f.cipher.delete.bind(f.cipher),
        isDeviceSecure: f.cipher.isDeviceSecure.bind(f.cipher), open: f.cipher.open.bind(f.cipher), keyStatus: f.cipher.keyStatus.bind(f.cipher),
        [method]: synchronous ? () => { throw error; } : () => Promise.reject(error),
      };
      assert.deepEqual(await f.make({ cipher }).enroll(scanned()), unavailable, `${method} ${synchronous}`);
      assert.deepEqual(f.transport.requests, []);
      assert.deepEqual(await contents(f.database), before);
      assert.equal(await f.base.keyStatus(ALIAS), "missing");
    }
  }
  for (const method of ["seal", "delete"] as const) {
    for (const synchronous of [false, true]) {
      const f = await fixture(t);
      await toPhase(f, method === "seal" ? "confirming" : "confirmed");
      const before = await contents(f.database);
      const cipher = {
        create: f.cipher.create.bind(f.cipher), seal: f.cipher.seal.bind(f.cipher), delete: f.cipher.delete.bind(f.cipher),
        isDeviceSecure: f.cipher.isDeviceSecure.bind(f.cipher), open: f.cipher.open.bind(f.cipher), keyStatus: f.cipher.keyStatus.bind(f.cipher),
        [method]: synchronous ? () => { throw error; } : () => Promise.reject(error),
      };
      const enrollment = f.make({ cipher });
      const result = method === "seal" ? await enrollment.confirm() : await enrollment.reset({ acknowledgePossibleLoss: true });
      assert.deepEqual(result, unavailable, `${method} ${synchronous}`);
      assert.deepEqual(await contents(f.database), before);
      assert.equal(await f.base.keyStatus(ALIAS), "present");
    }
  }
});

// R12 — Concurrency

test("actions_run_one_at_a_time", async (t) => {
  const f = await fixture(t);
  let open!: () => void;
  f.transport.gate = new Promise<void>((resolve) => { open = resolve; });
  const entered = f.transport.arrival();
  const enrolling = [f.sender.enroll(scanned()), f.sender.enroll(typed(PORTAL_URL, "ZZZZZZZZ22222222"))];
  await entered;
  await settle();
  assert.equal(f.transport.requests.length, 1);
  assert.equal(f.cipher.methods().filter((method) => method === "create").length, 1);
  open();
  assert.deepEqual(await Promise.all(enrolling), [{ outcome: "enrolled", generation: 1 }, { outcome: "refused", failure: "already-enrolled" }]);
  assert.equal((await currentStatus(f)).installationId, IDENTITY);
  // Refreshing, draining, and resetting queue behind an unanswered request too.
  f.transport.gate = new Promise<void>((resolve) => { open = resolve; });
  f.transport.respond = respondWith(advertisement(2));
  const arrived = f.transport.arrival();
  const refreshing = f.sender.refreshCapabilities();
  await arrived;
  const later = [f.sender.refreshCapabilities(), f.sender.beginDrain(), f.sender.reset({ acknowledgePossibleLoss: true })];
  await settle();
  assert.equal(f.transport.requests.length, 2);
  assert.equal(f.cipher.methods().includes("delete"), false);
  open();
  assert.deepEqual(await refreshing, { outcome: "adopted", generation: 2 });
  assert.deepEqual(await Promise.all(later), [{ outcome: "current" }, { outcome: "refused", failure: "not-confirmed" }, { outcome: "destroyed" }]);
  assert.equal(f.transport.maxInFlight, 1);
});

test("the_network_call_does_not_hold_the_storage_lane", async (t) => {
  const f = await fixture(t);
  const operations: string[] = [];
  const database = logging(f.database, operations);
  const enrollment = f.make({ database });
  const lane = storageLane(database);
  const free = () => Promise.race([lane(async () => "free"), settle(1_000).then(() => "held")]);
  // While another unit holds the shared lane, enrolling touches neither storage nor the Portal.
  let release!: () => void;
  let acquired!: () => void;
  const holding = new Promise<void>((resolve) => { acquired = resolve; });
  const held = lane(() => new Promise<void>((resolve) => { release = resolve; acquired(); }));
  await holding;
  let gateSecure!: () => void;
  f.cipher.script.set("isDeviceSecure", [{ before: () => new Promise<void>((resolve) => { gateSecure = resolve; }) }]);
  const enrolling = enrollment.enroll(scanned());
  try {
    await settle();
    assert.deepEqual(operations, []);
    assert.deepEqual(f.transport.requests, []);
    assert.deepEqual(f.cipher.calls, []);
  } finally { release(); }
  await held;
  // Reading the screen lock, creating the key, and the Portal's answer each leave the lane free.
  for (let wait = 0; wait < 200 && gateSecure === undefined; wait++) await settle(5);
  assert.ok(gateSecure !== undefined, "the screen lock was never read");
  try { assert.equal(await free(), "free"); } finally { gateSecure(); }
  assert.deepEqual(await enrolling, { outcome: "enrolled", generation: 1 });
  const g = await fixture(t);
  const gLane = storageLane(g.database);
  const gFree = () => Promise.race([gLane(async () => "free"), settle(1_000).then(() => "held")]);
  let gateCreate!: () => void;
  g.cipher.script.set("create", [{ before: () => new Promise<void>((resolve) => { gateCreate = resolve; }) }]);
  let openTransport!: () => void;
  g.transport.gate = new Promise<void>((resolve) => { openTransport = resolve; });
  const entered = g.transport.arrival();
  const pending = g.sender.enroll(scanned());
  for (let wait = 0; wait < 200 && gateCreate === undefined; wait++) await settle(5);
  assert.ok(gateCreate !== undefined, "the key was never created");
  try { assert.equal(await gFree(), "free"); } finally { gateCreate(); }
  await entered;
  try {
    assert.equal(await gFree(), "free");
    // Staging proceeds while the Portal has not answered.
    assert.equal(await Promise.race([gLane(async () => { await appendCheckpoint(g, OTHER_INSTALLATION); return "staged"; }),
      settle(1_000).then(() => "held")]), "staged");
  } finally { openTransport(); }
  assert.deepEqual(await pending, { outcome: "enrolled", generation: 1 });
  // Deleting the key leaves the lane free too.
  let gateDelete!: () => void;
  g.cipher.script.set("delete", [{ before: () => new Promise<void>((resolve) => { gateDelete = resolve; }) }]);
  const destroying = g.sender.reset({ acknowledgePossibleLoss: true });
  for (let wait = 0; wait < 200 && gateDelete === undefined; wait++) await settle(5);
  assert.ok(gateDelete !== undefined, "the key was never deleted");
  try { assert.equal(await gFree(), "free"); } finally { gateDelete(); }
  assert.deepEqual(await destroying, { outcome: "destroyed" });
});

// R13 — The Sender schema migration

const DDL = `CREATE TABLE sender_enrollment (
  singleton INTEGER PRIMARY KEY NOT NULL CHECK (singleton = 1),
  installation_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  key_alias TEXT NOT NULL,
  portal_url TEXT NOT NULL,
  confirmation BLOB NOT NULL,
  phase TEXT NOT NULL CHECK (phase IN ('confirming', 'refused', 'enrolled', 'draining')),
  refusal TEXT CHECK (refusal IN ('enrollment-refused', 'capabilities-unsupported')),
  advertisement BLOB,
  generation INTEGER CHECK (generation > 0),
  acknowledgement_sequence INTEGER CHECK (acknowledgement_sequence > 0),
  enrolled_at_ms INTEGER NOT NULL,
  adopted_at_ms INTEGER,
  CHECK ((phase = 'refused') = (refusal IS NOT NULL)),
  CHECK ((phase IN ('enrolled', 'draining')) = (advertisement IS NOT NULL)),
  CHECK ((advertisement IS NULL) = (generation IS NULL)),
  CHECK ((advertisement IS NULL) = (acknowledgement_sequence IS NULL)),
  CHECK ((advertisement IS NULL) = (adopted_at_ms IS NULL))) STRICT`;
/** SQL as its tokens, so whitespace between tokens never matters and literals keep their contents. */
const normalize = (sql: DatabaseValue): DatabaseValue =>
  typeof sql === "string" ? (sql.match(/'(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_]*|[0-9]+|\S/g) ?? []).join(" ") : sql;

async function schemaOf(database: SenderDatabase): Promise<DatabaseRow[]> {
  return (await database.query("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"))
    .map((entry) => ({ ...entry, sql: normalize(entry.sql) }));
}

test("migration_creates_version_eight", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const database = await openNodeDatabase(join(directory, "synthetic.sqlite"));
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.deepEqual(await migrate(database, MIGRATIONS.slice(0, 7)), { status: "ready", version: 7 });
  const before = await schemaOf(database);
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 8 });
  const after = await schemaOf(database);
  const key = (entry: DatabaseRow) => `${entry.type}:${entry.name}`;
  const kept = new Set(before.map(key));
  assert.deepEqual(after.filter((entry) => kept.has(key(entry))), before);
  assert.deepEqual(after.filter((entry) => !kept.has(key(entry))), [
    { type: "table", name: "sender_enrollment", tbl_name: "sender_enrollment", sql: normalize(DDL) },
  ]);
  assert.deepEqual(await database.query("SELECT * FROM sender_enrollment"), []);
  assert.equal(enrollmentMigration.version, 8);
});

test("migration_is_idempotent", async (t) => {
  const f = await fixture(t);
  await toPhase(f, "draining");
  await appendCheckpoint(f);
  const before = await contents(f.database);
  assert.deepEqual(await migrate(f.database, MIGRATIONS), { status: "ready", version: 8 });
  assert.deepEqual(await contents(f.database), before);
});

// R14 — Boundary

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
  // The network is reached only through the transport seam, and SQL names no other unit's table.
  const NETWORK = new Set(["fetch", "XMLHttpRequest", "WebSocket", "EventSource", "navigator", "process", "Buffer", "require", "global", "globalThis"]);
  const f = await fixture(t);
  const others = (await f.database.query("SELECT name FROM sqlite_master WHERE type = 'table' AND name <> 'sender_enrollment'"))
    .map((entry) => entry.name as string);
  assert.ok(others.length >= 14, others.join(", "));
  const files = (readdirSync(MODULE_DIRECTORY, { recursive: true }) as string[]).filter((name) => name.endsWith(".ts")).map((name) => join(MODULE_DIRECTORY, name));
  assert.ok(files.includes(MODULE));
  for (const file of files) {
    const code = parse(file);
    for (const table of others) {
      for (const string of code.strings) assert.doesNotMatch(string, new RegExp(`\\b${table}\\b`), `${file}: ${table}`);
    }
    for (const { specifier, values, dynamic } of code.dependencies) {
      assert.equal(dynamic, false, `${file}: ${specifier}`);
      if (specifier === "@activity-tracker-portal/ingest-protocol") {
        for (const value of values) assert.ok(["decodeMessage", "encodeMessage", "installationFingerprint"].includes(value), `${file}: ${value}`);
        continue;
      }
      // Type-only imports bring in no values. Relative imports resolve from the importing file, and local
      // helpers stay inside the module's directory, where this test inspects them too.
      if (values.length === 0) continue;
      assert.match(specifier, /^\.{1,2}\//, `${file}: ${specifier}`);
      const target = resolve(dirname(file), specifier);
      if (!relative(MODULE_DIRECTORY, target).startsWith("..")) continue;
      if (!relative(SOURCE_STORAGE, target).startsWith("..")) continue;
      assert.equal(target, STAGE_OUTBOX_BATCH, `${file}: ${specifier}`);
      for (const value of values) {
        assert.ok(["appendOutboxMessages", "lastOutboxSequence", "outboxMessages", "outboxAad"].includes(value), `${file}: ${value}`);
      }
    }
    assert.deepEqual(code.references.filter((name) => NETWORK.has(name)), [], file);
  }
});
