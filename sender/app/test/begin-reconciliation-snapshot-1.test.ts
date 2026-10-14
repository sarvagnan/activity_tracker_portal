// Acceptance tests for begin-reconciliation-snapshot@1.
// Requirement numbers refer to packs/begin-reconciliation-snapshot-1.spec.md. Storage is the 3ug
// node:sqlite SenderDatabase substitute with the production migrations; the token issuer is
// scripted; attempts and pages come from the published begin-initial-snapshot@2 and stage-outbox-batch@1.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import type { TestContext } from "node:test";
import { beginReconciliationSnapshot } from "../src/snapshot/begin-reconciliation-snapshot.ts";
import type { ReconcileRequest, ReconcileResult } from "../src/snapshot/begin-reconciliation-snapshot.ts";
import { beginInitialSnapshot, currentSnapshotAttempt, snapshotAttempt } from "../src/snapshot/begin-initial-snapshot.ts";
import type { BeginOptions, ChangesTokenIssuer, IssuedToken, SnapshotAttempt } from "../src/snapshot/begin-initial-snapshot.ts";
import { changeOrigin, cycleProgress, stageOutboxBatch } from "../src/outbox/stage-outbox-batch.ts";
import type { StagingContext } from "../src/outbox/stage-outbox-batch.ts";
import type { DatabaseValue, SenderDatabase } from "../src/storage/database.ts";
import { migrate } from "../src/storage/migrations.ts";
import { openNodeDatabase } from "./support/node-database.ts";
import { NodeInstallationCipher } from "./support/node-installation-cipher.ts";

const CONTRACT = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../../../governance/scopes/begin-reconciliation-snapshot.1.json"), "utf8"));
const INSTALLATION = "00000000-0000-4000-8000-000000000001";
const OTHER_INSTALLATION = "00000000-0000-4000-8000-000000000002";
const UNREAD_SINCE = 1_791_000_000_123;
const NOW = 1_791_000_005_000;
const SENTINEL = "SENTINEL";
const KEY_ALIAS = "synthetic-installation-key";
const uuid = (n: number) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, "0")}`;

async function fixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const path = join(directory, "synthetic.sqlite");
  let database = await openNodeDatabase(path);
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.deepEqual(await migrate(database), { status: "ready", version: 2 });
  const cipher = new NodeInstallationCipher();
  assert.equal((await cipher.create(KEY_ALIAS)).status, "ok");
  return {
    cipher,
    get database() { return database; },
    async reopen() { await database.close(); database = await openNodeDatabase(path); return database; },
  };
}

/** A token issuer that answers scripted results in order, then distinct synthetic tokens. */
class ScriptedIssuer implements ChangesTokenIssuer {
  readonly calls: string[] = [];
  private issued = 0;
  private readonly results: unknown[];
  private readonly during?: () => Promise<void>;
  constructor(results: unknown[] = [], during?: () => Promise<void>) {
    this.results = results;
    this.during = during;
  }
  async issue(recordType: string): Promise<IssuedToken> {
    this.calls.push(recordType);
    await this.during?.();
    if (this.results.length === 0) {
      return { outcome: "issued", changesToken: `synthetic-reconciliation-token-${++this.issued}`, unreadSinceMs: UNREAD_SINCE };
    }
    const next = this.results.shift();
    if (next instanceof Error || (typeof next === "object" && next !== null && "throws" in next)) {
      throw next instanceof Error ? next : (next as { throws: unknown }).throws;
    }
    return next as IssuedToken;
  }
}

/** UUIDs from a list, then a fixed sequence; a clock that moves on every read and counts its reads. */
function options(...uuids: string[]): BeginOptions & { readonly uuidCalls: number; readonly nowCalls: number } {
  const queue = [...uuids];
  let next = 0x100;
  let uuidCalls = 0;
  let nowCalls = 0;
  return {
    randomUUID: () => { uuidCalls++; return queue.shift() ?? uuid(next++); },
    now: () => NOW + 1000 * ++nowCalls,
    get uuidCalls() { return uuidCalls; },
    get nowCalls() { return nowCalls; },
  };
}

const request = (superseding: string, extra: Partial<ReconcileRequest> = {}): ReconcileRequest =>
  ({ recordType: "steps", installationId: INSTALLATION, superseding, ...extra });

function reconcile(database: SenderDatabase, req: ReconcileRequest, issuer: ChangesTokenIssuer = new ScriptedIssuer(), opts: BeginOptions = options()): Promise<ReconcileResult> {
  return beginReconciliationSnapshot(database, issuer, req, opts);
}

function begun(result: ReconcileResult): SnapshotAttempt {
  assert.equal(result.outcome, "begun", JSON.stringify(result));
  return (result as { attempt: SnapshotAttempt }).attempt;
}

/** An Initial Snapshot attempt through begin-initial-snapshot@2. */
async function initial(database: SenderDatabase, snapshotId: string, extra: { recordType?: string; installationId?: string; abandoning?: string } = {}): Promise<SnapshotAttempt> {
  const issuer = { async issue() { return { outcome: "issued" as const, changesToken: `synthetic-initial-token-${snapshotId.slice(-2)}`, unreadSinceMs: UNREAD_SINCE - 1 }; } };
  const result = await beginInitialSnapshot(
    database, issuer,
    { recordType: extra.recordType ?? "steps", installationId: extra.installationId ?? INSTALLATION, ...(extra.abandoning ? { abandoning: extra.abandoning } : {}) },
    { randomUUID: () => snapshotId, now: () => NOW },
  );
  assert.equal(result.outcome, "begun", JSON.stringify(result));
  return (result as { attempt: SnapshotAttempt }).attempt;
}

/** Stage one snapshot page of the attempt through stage-outbox-batch@1. */
async function stagePage(f: { database: SenderDatabase; cipher: NodeInstallationCipher }, attempt: SnapshotAttempt, exhausted: boolean, pageIndex = 1) {
  const context: StagingContext = {
    installationId: attempt.installationId, keyAlias: KEY_ALIAS,
    generation: { generation: 1, maxRecordCount: 100, maxMessageBytes: 65_536, projectionSchemas: { [attempt.recordType]: [1] } },
  };
  const result = await stageOutboxBatch(f.database, f.cipher, context, {
    source: "snapshot", recordType: attempt.recordType, cycleId: attempt.snapshotId, projectionSchemaVersion: 1,
    pageIndex, readFrom: pageIndex === 1 ? null : new TextEncoder().encode(`position-${pageIndex - 1}`),
    projections: [new TextEncoder().encode(`{"synthetic":${pageIndex}}`)],
    nextPosition: new TextEncoder().encode(`position-${pageIndex}`), exhausted,
  });
  assert.equal(result.outcome, "staged", JSON.stringify(result));
  return result;
}

/** A finished Initial Snapshot: begun and its single page staged as exhausted. */
async function finishedInitial(f: { database: SenderDatabase; cipher: NodeInstallationCipher }, snapshotId: string, extra: { recordType?: string; installationId?: string } = {}) {
  const attempt = await initial(f.database, snapshotId, extra);
  await stagePage(f, attempt, true);
  return attempt;
}

async function contents(database: SenderDatabase) {
  return {
    version: await database.query("PRAGMA user_version"),
    attempts: await database.query("SELECT * FROM snapshot_attempts ORDER BY snapshot_id"),
    current: await database.query("SELECT * FROM current_snapshot_attempts ORDER BY record_type"),
    progress: await database.query("SELECT * FROM cycle_progress ORDER BY cycle_id"),
    outbox: await database.query("SELECT installation_id, sequence FROM outbox_messages ORDER BY installation_id, sequence"),
  };
}

const MUTATION = /^\s*(?:WITH\b[\s\S]*?\b)?(INSERT|UPDATE|DELETE|REPLACE)\b/i;

/**
 * Wraps a database so that one statement or step fails: the given mutating statement, counted
 * from 1; every read outside or inside the transaction; or the commit, simulated by failing after
 * the transaction body succeeds, which the 3ug seam rolls back.
 */
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

/** A database whose every use throws and is counted, to prove validation comes first. */
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

// R2 — Beginning a reconciliation

test("begin_commits_a_reconciliation_and_supersedes_the_named_attempt", async (t) => {
  const f = await fixture(t);
  const baseline = await finishedInitial(f, uuid(0xa1));
  const weight = await initial(f.database, uuid(0xb1), { recordType: "weight" });
  const issuer = new ScriptedIssuer();
  const opts = options(uuid(0xa2));
  const attempt = begun(await reconcile(f.database, request(uuid(0xa1)), issuer, opts));
  const expected: SnapshotAttempt = {
    snapshotId: uuid(0xa2), recordType: "steps", kind: "reconciliation", installationId: INSTALLATION,
    changesToken: "synthetic-reconciliation-token-1", unreadSinceMs: UNREAD_SINCE, begunAtMs: NOW + 1000, abandonedAtMs: null,
  };
  assert.deepEqual(attempt, expected);
  assert.deepEqual(issuer.calls, ["steps"]);
  assert.deepEqual(await currentSnapshotAttempt(f.database, "steps"), expected);
  assert.deepEqual(await snapshotAttempt(f.database, uuid(0xa1)), { ...baseline, abandonedAtMs: NOW + 1000 });
  assert.deepEqual(await currentSnapshotAttempt(f.database, "weight"), weight);
});

test("the_reconciliation_survives_reopening", async (t) => {
  const f = await fixture(t);
  await finishedInitial(f, uuid(0xa1));
  const attempt = begun(await reconcile(f.database, request(uuid(0xa1)), new ScriptedIssuer(), options(uuid(0xa2))));
  const reopened = await f.reopen();
  assert.deepEqual(await migrate(reopened), { status: "ready", version: 2 });
  assert.deepEqual(await currentSnapshotAttempt(reopened, "steps"), attempt);
});

test("the_identity_is_the_generated_uuid", async (t) => {
  const f = await fixture(t);
  await finishedInitial(f, uuid(0xa1));
  const opts = options("0f8fad5b-d9cb-469f-a165-70867728950e");
  assert.equal(begun(await reconcile(f.database, request(uuid(0xa1)), new ScriptedIssuer(), opts)).snapshotId, "0f8fad5b-d9cb-469f-a165-70867728950e");
  assert.equal(opts.uuidCalls, 1);
});

test("the_commit_time_dates_both_changes", async (t) => {
  const f = await fixture(t);
  await finishedInitial(f, uuid(0xa1));
  const opts = options(uuid(0xa2));
  const attempt = begun(await reconcile(f.database, request(uuid(0xa1)), new ScriptedIssuer(), opts));
  assert.equal(opts.nowCalls, 1);
  assert.equal(attempt.begunAtMs, NOW + 1000);
  assert.equal((await snapshotAttempt(f.database, uuid(0xa1)))?.abandonedAtMs, NOW + 1000);
});

// R3 — Preconditions, checked before the token request

test("a_reconciliation_must_name_the_current_attempt", async (t) => {
  const f = await fixture(t);
  const mismatch = { outcome: "refused", failure: "attempt-mismatch" };
  assert.deepEqual(await reconcile(f.database, request(uuid(0xa1))), mismatch, "no current attempt");
  await finishedInitial(f, uuid(0xa1));
  await finishedInitial(f, uuid(0xb1), { recordType: "weight" });
  assert.deepEqual(await reconcile(f.database, request(uuid(0xa9))), mismatch, "an unknown attempt");
  assert.deepEqual(await reconcile(f.database, request(uuid(0xb1))), mismatch, "another type's attempt");
  begun(await reconcile(f.database, request(uuid(0xa1)), new ScriptedIssuer(), options(uuid(0xa2))));
  assert.deepEqual(await reconcile(f.database, request(uuid(0xa1))), mismatch, "an attempt no longer current");
});

test("another_installation_is_refused", async (t) => {
  const f = await fixture(t);
  await finishedInitial(f, uuid(0xa1), { installationId: OTHER_INSTALLATION });
  assert.deepEqual(await reconcile(f.database, request(uuid(0xa1))), { outcome: "refused", failure: "installation-mismatch" });
  begun(await reconcile(f.database, request(uuid(0xa1), { installationId: OTHER_INSTALLATION }), new ScriptedIssuer(), options(uuid(0xa2))));
});

test("an_unfinished_initial_snapshot_is_refused", async (t) => {
  const f = await fixture(t);
  const unfinished = { outcome: "refused", failure: "baseline-unfinished" };
  const attempt = await initial(f.database, uuid(0xa1));
  assert.deepEqual(await reconcile(f.database, request(uuid(0xa1))), unfinished, "no page staged");
  await stagePage(f, attempt, false);
  assert.deepEqual(await reconcile(f.database, request(uuid(0xa1))), unfinished, "a page staged, not exhausted");
  await stagePage(f, attempt, true, 2);
  begun(await reconcile(f.database, request(uuid(0xa1)), new ScriptedIssuer(), options(uuid(0xa2))));
});

test("a_finished_initial_or_reconciliation_may_be_reconciled", async (t) => {
  const f = await fixture(t);
  await finishedInitial(f, uuid(0xa1));
  const first = begun(await reconcile(f.database, request(uuid(0xa1)), new ScriptedIssuer(), options(uuid(0xa2))));
  await stagePage(f, first, true);
  assert.equal((await cycleProgress(f.database, uuid(0xa2)))?.cycleKind, "reconciliation-snapshot");
  const second = begun(await reconcile(f.database, request(uuid(0xa2)), new ScriptedIssuer(), options(uuid(0xa3))));
  assert.equal(second.kind, "reconciliation");
  assert.equal((await snapshotAttempt(f.database, uuid(0xa2)))?.abandonedAtMs, second.begunAtMs);
});

test("an_unfinished_reconciliation_may_be_restarted", async (t) => {
  const f = await fixture(t);
  await finishedInitial(f, uuid(0xa1));
  const first = begun(await reconcile(f.database, request(uuid(0xa1)), new ScriptedIssuer(), options(uuid(0xa2))));
  assert.equal(await cycleProgress(f.database, uuid(0xa2)), null, "no reconciliation page staged");
  assert.equal(first.kind, "reconciliation");
  // Restarted before any page: the earlier finished baseline is still what it reconciles against.
  const restarted = begun(await reconcile(f.database, request(uuid(0xa2)), new ScriptedIssuer(), options(uuid(0xa3))));
  // Restarted again after a page was staged but before the chain finished.
  await stagePage(f, restarted, false);
  assert.equal((await cycleProgress(f.database, uuid(0xa3)))?.finished, false);
  assert.equal(begun(await reconcile(f.database, request(uuid(0xa3)), new ScriptedIssuer(), options(uuid(0xa4)))).snapshotId, uuid(0xa4));
  assert.equal((await snapshotAttempt(f.database, uuid(0xa3)))?.abandonedAtMs !== null, true);
});

test("refusals_request_no_token", async (t) => {
  const f = await fixture(t);
  await initial(f.database, uuid(0xa1));
  await finishedInitial(f, uuid(0xb1), { recordType: "weight", installationId: OTHER_INSTALLATION });
  for (const req of [request(uuid(0xa9)), request(uuid(0xa1)), request(uuid(0xb1), { recordType: "weight" })]) {
    const issuer = new ScriptedIssuer();
    const opts = options();
    assert.equal((await reconcile(f.database, req, issuer, opts)).outcome, "refused");
    assert.deepEqual(issuer.calls, []);
    assert.deepEqual([opts.uuidCalls, opts.nowCalls], [0, 0]);
  }
});

// R4 — The token request and its classification

test("issuer_failures_write_nothing", async (t) => {
  const f = await fixture(t);
  await finishedInitial(f, uuid(0xa1));
  const before = await contents(f.database);
  const failures = (CONTRACT.named_failures as { name: string; outcome: string }[])
    .filter((failure) => failure.outcome === "not-permitted" || failure.outcome === "read-failure")
    .map((failure) => ({ outcome: failure.outcome === "not-permitted" ? "not-permitted" : "read-failed", failure: failure.name }));
  assert.equal(failures.length, 8);
  for (const result of failures) {
    const opts = options();
    assert.deepEqual(await reconcile(f.database, request(uuid(0xa1)), new ScriptedIssuer([result]), opts), result);
    assert.deepEqual([opts.uuidCalls, opts.nowCalls], [0, 0]);
  }
  assert.deepEqual(await contents(f.database), before);
});

test("a_throwing_issuer_is_an_unexpected_read_failure", async (t) => {
  const f = await fixture(t);
  await finishedInitial(f, uuid(0xa1));
  const before = await contents(f.database);
  for (const thrown of [new Error(`${SENTINEL} bridge`), { throws: `${SENTINEL} not an error` }, { throws: null }, { throws: undefined }]) {
    assert.deepEqual(await reconcile(f.database, request(uuid(0xa1)), new ScriptedIssuer([thrown])), { outcome: "read-failed", failure: "unexpected-read-failure" });
  }
  assert.deepEqual(await contents(f.database), before);
});

test("malformed_issuances_are_violations", async (t) => {
  const f = await fixture(t);
  await finishedInitial(f, uuid(0xa1));
  const before = await contents(f.database);
  for (const result of [
    { outcome: "issued", changesToken: "", unreadSinceMs: UNREAD_SINCE },
    { outcome: "issued", changesToken: 7, unreadSinceMs: UNREAD_SINCE },
    { outcome: "issued", changesToken: "t", unreadSinceMs: 1.5 },
    { outcome: "issued", changesToken: "t" },
    { outcome: "not-permitted", failure: "health-connect-unavailable" },
    { outcome: "read-failed", failure: "read-denied" },
    { outcome: "read-failed", failure: "baseline-unfinished" },
    { outcome: "read-failed", failure: `${SENTINEL}-not-a-failure` },
    { outcome: "read-failed", failure: "toString" },
    { outcome: "unknown" },
    null,
    "issued",
  ]) {
    assert.deepEqual(await reconcile(f.database, request(uuid(0xa1)), new ScriptedIssuer([result])), { outcome: "read-failed", failure: "source-contract-violation" }, JSON.stringify(result));
  }
  assert.deepEqual(await contents(f.database), before);
});

// R5 — The identity source

test("a_bad_uuid_source_is_a_caller_error", async (t) => {
  const f = await fixture(t);
  await finishedInitial(f, uuid(0xa1));
  const before = await contents(f.database);
  for (const value of ["", "00000000-0000-1000-8000-000000000001", "00000000-0000-4000-7000-000000000001", "0F8FAD5B-D9CB-469F-A165-70867728950E"]) {
    const issuer = new ScriptedIssuer();
    const opts = options(value);
    await assert.rejects(reconcile(f.database, request(uuid(0xa1)), issuer, opts), TypeError, value);
    assert.deepEqual(issuer.calls, ["steps"]);
    assert.equal(opts.nowCalls, 0);
  }
  assert.deepEqual(await contents(f.database), before);
});

// R6 — The commit rechecks

test("the_commit_rechecks_the_preconditions", async (t) => {
  const f = await fixture(t);
  await finishedInitial(f, uuid(0xa1));
  // Another reconciliation lands while this one waits for its token.
  let inner: ReconcileResult | undefined;
  const racing = new ScriptedIssuer([], async () => { inner = await reconcile(f.database, request(uuid(0xa1)), new ScriptedIssuer(), options(uuid(0xa2))); });
  const opts = options(uuid(0xa3));
  assert.deepEqual(await reconcile(f.database, request(uuid(0xa1)), racing, opts), { outcome: "refused", failure: "attempt-mismatch" });
  assert.equal(begun(inner!).snapshotId, uuid(0xa2));
  assert.equal(opts.nowCalls, 0, "the clock is read only after the recheck passes");
  assert.equal(await snapshotAttempt(f.database, uuid(0xa3)), null);
  // An Initial restart supersedes the attempt while this reconciliation waits; the recheck refuses it.
  const restarting = new ScriptedIssuer([], async () => { await initial(f.database, uuid(0xa4), { abandoning: uuid(0xa2) }); });
  assert.deepEqual(await reconcile(f.database, request(uuid(0xa2)), restarting, options(uuid(0xa5))), { outcome: "refused", failure: "attempt-mismatch" });
  assert.equal((await currentSnapshotAttempt(f.database, "steps"))?.snapshotId, uuid(0xa4));
  assert.equal(await snapshotAttempt(f.database, uuid(0xa5)), null);
});

// R7 — Nothing begun on failure

test("storage_errors_roll_back_everything", async (t) => {
  const f = await fixture(t);
  await finishedInitial(f, uuid(0xa1));
  const before = await contents(f.database);
  let points = 0;
  for (let mutation = 1; ; mutation++) {
    const result = await reconcile(failing(f.database, { mutation }), request(uuid(0xa1)), new ScriptedIssuer(), options(uuid(0xa2)));
    if (result.outcome === "begun") break;
    assert.deepEqual(result, { outcome: "storage-failed", failure: "storage-failed" });
    assert.deepEqual(await contents(f.database), before, `failing mutation ${mutation}`);
    points++;
    assert.ok(mutation < 20, "the reconciliation never completed");
  }
  assert.ok(points >= 2, "the supersession and the new attempt are each written");
  // A failing read before the token request is a storage failure too, and requests no token.
  const issuer = new ScriptedIssuer();
  assert.deepEqual(await reconcile(failing(f.database, { queries: true }), request(uuid(0xa2)), issuer), { outcome: "storage-failed", failure: "storage-failed" });
  assert.deepEqual(issuer.calls, []);
  // So are a failing read inside the transaction, such as the recheck, and a failing commit.
  const settled = await contents(f.database);
  for (const rule of [{ transactionQueries: true }, { commit: true }]) {
    assert.deepEqual(await reconcile(failing(f.database, rule), request(uuid(0xa2)), new ScriptedIssuer(), options(uuid(0xa3))), { outcome: "storage-failed", failure: "storage-failed" }, JSON.stringify(rule));
    assert.deepEqual(await contents(f.database), settled, JSON.stringify(rule));
  }
});

test("refusals_write_nothing", async (t) => {
  const f = await fixture(t);
  await initial(f.database, uuid(0xa1));
  await finishedInitial(f, uuid(0xb1), { recordType: "weight", installationId: OTHER_INSTALLATION });
  const before = await contents(f.database);
  await reconcile(f.database, request(uuid(0xa9)));
  await reconcile(f.database, request(uuid(0xa1)));
  await reconcile(f.database, request(uuid(0xb1), { recordType: "weight" }));
  assert.deepEqual(await contents(f.database), before);
});

// R8 — Interplay with staging

test("staging_follows_the_reconciliation", async (t) => {
  const f = await fixture(t);
  const baseline = await initial(f.database, uuid(0xa1));
  await stagePage(f, baseline, true);
  const context: StagingContext = { installationId: INSTALLATION, keyAlias: KEY_ALIAS, generation: { generation: 1, maxRecordCount: 100, maxMessageBytes: 65_536, projectionSchemas: { steps: [1] } } };
  const changePage = (pageIndex: number, readFrom: string) => ({
    source: "change" as const, recordType: "steps", cycleId: uuid(0xc1), projectionSchemaVersion: 1, pageIndex, readFrom,
    items: [], nextChangesToken: `synthetic-next-${pageIndex}`, nextUnreadSinceMs: 1, hasMore: true,
  });
  // An open Change Cycle under the baseline, whose token then expires.
  assert.equal((await stageOutboxBatch(f.database, f.cipher, context, changePage(1, baseline.changesToken))).outcome, "staged");
  const reconciliation = begun(await reconcile(f.database, request(uuid(0xa1)), new ScriptedIssuer(), options(uuid(0xa2))));
  assert.deepEqual(await changeOrigin(f.database, "steps"), { snapshotId: uuid(0xa2), changesToken: reconciliation.changesToken, unreadSinceMs: UNREAD_SINCE });
  assert.deepEqual(await stageOutboxBatch(f.database, f.cipher, context, changePage(2, "synthetic-next-1")), { outcome: "refused", failure: "cycle-not-current" });
  const staged = await stagePage(f, reconciliation, false);
  assert.equal(staged.outcome === "staged" && staged.progress.cycleKind, "reconciliation-snapshot");
});

// R9 — Caller errors, health-data-free outcomes, and order of checks

test("malformed_requests_are_caller_errors", async () => {
  const probe = untouchable();
  for (const req of [
    request(uuid(0xa1), { recordType: "Steps" }),
    request(uuid(0xa1), { recordType: "a".repeat(65) }),
    request(uuid(0xa1), { installationId: "00000000-0000-4000-8000-00000000000A" }),
    request(uuid(0xa1), { installationId: "not-a-uuid" }),
    request("00000000-0000-4000-8000-0000000000A1"),
    request(""),
    { recordType: "steps", installationId: INSTALLATION } as ReconcileRequest,
    { ...request(uuid(0xa1)), superseding: null as unknown as string },
    null as unknown as ReconcileRequest,
  ]) {
    const issuer = new ScriptedIssuer();
    await assert.rejects(reconcile(probe.database, req, issuer), TypeError, JSON.stringify(req));
    assert.deepEqual(issuer.calls, []);
  }
  assert.equal(probe.calls(), 0);
});

test("non_begun_results_carry_only_named_failures", async (t) => {
  const f = await fixture(t);
  await finishedInitial(f, uuid(0xa1));
  const results = [
    await reconcile(f.database, request(uuid(0xa9))),
    await reconcile(f.database, request(uuid(0xa1)), new ScriptedIssuer([new Error(`${SENTINEL} bridge failure`)])),
    await reconcile(f.database, request(uuid(0xa1)), new ScriptedIssuer([{ outcome: "issued", changesToken: "", unreadSinceMs: UNREAD_SINCE }])),
    await reconcile(failing(f.database, { mutation: 1 }), request(uuid(0xa1)), new ScriptedIssuer([{ outcome: "issued", changesToken: `${SENTINEL}-token`, unreadSinceMs: UNREAD_SINCE }])),
  ];
  for (const result of results) {
    assert.notEqual(result.outcome, "begun");
    assert.deepEqual(Object.keys(result).sort(), ["failure", "outcome"]);
    assert.ok(!JSON.stringify(result).includes(SENTINEL), JSON.stringify(result));
  }
});

test("every_named_failure_is_reachable_and_in_the_contract", async (t) => {
  const f = await fixture(t);
  const classes: Record<string, string> = { "refused": "refusal", "not-permitted": "not-permitted", "read-failed": "read-failure", "storage-failed": "storage-failure" };
  const observed = new Set<string>();
  const record = (result: ReconcileResult) => {
    assert.notEqual(result.outcome, "begun", JSON.stringify(result));
    observed.add(`${classes[result.outcome]}:${(result as { failure: string }).failure}`);
  };
  await initial(f.database, uuid(0xb1), { recordType: "weight" });
  await finishedInitial(f, uuid(0xc1), { recordType: "heart-rate", installationId: OTHER_INSTALLATION });
  await finishedInitial(f, uuid(0xa1));
  record(await reconcile(f.database, request(uuid(0xa9))));
  record(await reconcile(f.database, request(uuid(0xc1), { recordType: "heart-rate" })));
  record(await reconcile(f.database, request(uuid(0xb1), { recordType: "weight" })));
  record(await reconcile(failing(f.database, { mutation: 1 }), request(uuid(0xa1))));
  record(await reconcile(f.database, request(uuid(0xa1)), new ScriptedIssuer([new Error("bridge")])));
  record(await reconcile(f.database, request(uuid(0xa1)), new ScriptedIssuer([{ outcome: "unknown" }])));
  for (const failure of CONTRACT.named_failures as { name: string; outcome: string }[]) {
    if (failure.outcome === "not-permitted") record(await reconcile(f.database, request(uuid(0xa1)), new ScriptedIssuer([{ outcome: "not-permitted", failure: failure.name }])));
    if (failure.outcome === "read-failure") record(await reconcile(f.database, request(uuid(0xa1)), new ScriptedIssuer([{ outcome: "read-failed", failure: failure.name }])));
  }
  const named = new Set((CONTRACT.named_failures as { name: string; outcome: string }[]).map((failure) => `${failure.outcome}:${failure.name}`));
  assert.deepEqual([...observed].sort(), [...named].sort());
});

test("checks_follow_the_documented_order", async (t) => {
  const f = await fixture(t);
  // 1. Caller errors first, even against a broken store.
  await assert.rejects(reconcile(untouchable().database, request("not-a-uuid")), TypeError);
  // 2. The refusals, in order: mismatch before installation before baseline.
  await initial(f.database, uuid(0xa1), { installationId: OTHER_INSTALLATION });
  assert.deepEqual(await reconcile(f.database, request(uuid(0xa9))), { outcome: "refused", failure: "attempt-mismatch" });
  assert.deepEqual(await reconcile(f.database, request(uuid(0xa1))), { outcome: "refused", failure: "installation-mismatch" }, "an unfinished attempt of another installation");
  // A refusal comes before the issuer, and the issuer before the UUID source.
  const refusedIssuer = new ScriptedIssuer([{ outcome: "read-failed", failure: "health-connect-unavailable" }]);
  assert.equal((await reconcile(f.database, request(uuid(0xa1)), refusedIssuer)).outcome, "refused");
  assert.deepEqual(refusedIssuer.calls, []);
  await finishedInitial(f, uuid(0xb1), { recordType: "weight" });
  const deniedOptions = options("not-a-uuid");
  assert.deepEqual(await reconcile(f.database, request(uuid(0xb1), { recordType: "weight" }), new ScriptedIssuer([{ outcome: "not-permitted", failure: "read-denied" }]), deniedOptions),
    { outcome: "not-permitted", failure: "read-denied" });
  assert.equal(deniedOptions.uuidCalls, 0);
  // The UUID source before the commit, and the commit last.
  await assert.rejects(reconcile(failing(f.database, { mutation: 1 }), request(uuid(0xb1), { recordType: "weight" }), new ScriptedIssuer(), options("not-a-uuid")), TypeError);
  assert.deepEqual(await reconcile(failing(f.database, { mutation: 1 }), request(uuid(0xb1), { recordType: "weight" })), { outcome: "storage-failed", failure: "storage-failed" });
});
