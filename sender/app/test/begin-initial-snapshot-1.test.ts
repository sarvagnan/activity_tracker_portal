// Acceptance tests for the TypeScript part of begin-initial-snapshot@1.
// Requirement numbers refer to packs/begin-initial-snapshot-1.spec.md. Storage is
// the 3ug node:sqlite SenderDatabase substitute; the token issuer is scripted.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import type { TestContext } from "node:test";
import {
  beginInitialSnapshot,
  currentSnapshotAttempt,
  snapshotAttempt,
  snapshotAttemptsMigration,
} from "../src/snapshot/begin-initial-snapshot.ts";
import type {
  BeginOptions,
  BeginRequest,
  BeginResult,
  ChangesTokenIssuer,
  IssuedToken,
  SnapshotAttempt,
} from "../src/snapshot/begin-initial-snapshot.ts";
import type { DatabaseValue, SenderDatabase } from "../src/storage/database.ts";
import { migrate } from "../src/storage/migrations.ts";
import { openNodeDatabase } from "./support/node-database.ts";

const CONTRACT = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../../../governance/scopes/begin-initial-snapshot.1.json"), "utf8"));
const INSTALLATION = "00000000-0000-4000-8000-000000000001";
const OTHER_INSTALLATION = "00000000-0000-4000-8000-000000000002";
const UNREAD_SINCE = 1_791_000_000_123;
const NOW = 1_791_000_005_000;
const SENTINEL = "SENTINEL";
const uuid = (n: number) => `00000000-0000-4000-8000-${n.toString(16).padStart(12, "0")}`;

async function fixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const path = join(directory, "synthetic.sqlite");
  let database = await openNodeDatabase(path);
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.deepEqual(await migrate(database, [snapshotAttemptsMigration]), { status: "ready", version: 1 });
  return {
    get database() { return database; },
    async reopen() { await database.close(); database = await openNodeDatabase(path); return database; },
  };
}

/** A token issuer that answers scripted results in order, then distinct synthetic tokens. */
class ScriptedIssuer implements ChangesTokenIssuer {
  readonly calls: string[] = [];
  private issued = 0;
  private readonly results: (IssuedToken | Error)[];
  private readonly during?: () => Promise<void>;
  constructor(results: (IssuedToken | Error)[] = [], during?: () => Promise<void>) {
    this.results = results;
    this.during = during;
  }
  async issue(recordType: string): Promise<IssuedToken> {
    this.calls.push(recordType);
    await this.during?.();
    if (this.results.length === 0) {
      return { outcome: "issued", changesToken: `synthetic-changes-token-${++this.issued}`, unreadSinceMs: UNREAD_SINCE };
    }
    const next = this.results.shift();
    if (next instanceof Error) throw next;
    return next as IssuedToken;
  }
}

/** UUIDs from a fixed list, then a fixed sequence, so each test knows the identity it will get. */
function options(...uuids: string[]): BeginOptions & { readonly uuidCalls: number } {
  const queue = [...uuids];
  let next = 100;
  let calls = 0;
  return {
    randomUUID: () => { calls++; return queue.shift() ?? uuid(next++); },
    now: () => NOW,
    get uuidCalls() { return calls; },
  };
}

const request = (recordType = "steps", extra: Partial<BeginRequest> = {}): BeginRequest => ({ recordType, installationId: INSTALLATION, ...extra });

async function begin(database: SenderDatabase, issuer: ChangesTokenIssuer = new ScriptedIssuer(), req = request(), opts: BeginOptions = options()): Promise<BeginResult> {
  return beginInitialSnapshot(database, issuer, req, opts);
}

function begun(result: BeginResult): SnapshotAttempt {
  assert.equal(result.outcome, "begun", JSON.stringify(result));
  return (result as { attempt: SnapshotAttempt }).attempt;
}

/** Every row of this unit's tables, plus the schema version, for before/after comparison. */
async function contents(database: SenderDatabase) {
  return {
    version: await database.query("PRAGMA user_version"),
    attempts: await database.query("SELECT * FROM snapshot_attempts ORDER BY snapshot_id"),
    current: await database.query("SELECT * FROM current_snapshot_attempts ORDER BY record_type"),
  };
}

const MUTATION = /^\s*(INSERT|UPDATE|DELETE|REPLACE)\b/i;

/** Wraps a database so the given mutating statement, counted from 1, or every query, throws. */
function failing(database: SenderDatabase, rule: { mutation?: number; queries?: boolean }): SenderDatabase & { readonly mutations: number } {
  let mutations = 0;
  const guard = (sql: string) => {
    if (rule.queries && !MUTATION.test(sql)) throw new Error(`${SENTINEL} synthetic read failure`);
    if (MUTATION.test(sql) && ++mutations === rule.mutation) throw new Error(`${SENTINEL} synthetic write failure`);
  };
  const wrap = (inner: SenderDatabase): SenderDatabase => ({
    async execute(sql: string, parameters?: readonly DatabaseValue[]) { guard(sql); return inner.execute(sql, parameters); },
    async query(sql: string, parameters?: readonly DatabaseValue[]) { guard(sql); return inner.query(sql, parameters); },
    transaction<T>(fn: (transaction: SenderDatabase) => T | Promise<T>): Promise<T> {
      return inner.transaction((transaction) => fn(wrap(transaction)));
    },
    close: () => inner.close(),
  });
  return Object.assign(wrap(database), { get mutations() { return mutations; } });
}

// R5 — Beginning a snapshot

test("begin_commits_the_attempt_and_makes_it_current", async (t) => {
  const f = await fixture(t);
  const issuer = new ScriptedIssuer();
  const attempt = begun(await begin(f.database, issuer, request(), options(uuid(0xa1))));
  const expected: SnapshotAttempt = {
    snapshotId: uuid(0xa1), recordType: "steps", kind: "initial", installationId: INSTALLATION,
    changesToken: "synthetic-changes-token-1", unreadSinceMs: UNREAD_SINCE, begunAtMs: NOW, abandonedAtMs: null,
  };
  assert.deepEqual(attempt, expected);
  assert.deepEqual(issuer.calls, ["steps"]);
  assert.deepEqual(await currentSnapshotAttempt(f.database, "steps"), expected);
  assert.deepEqual(await snapshotAttempt(f.database, uuid(0xa1)), expected);
});

test("the_attempt_survives_reopening", async (t) => {
  const f = await fixture(t);
  const attempt = begun(await begin(f.database));
  const reopened = await f.reopen();
  assert.deepEqual(await migrate(reopened, [snapshotAttemptsMigration]), { status: "ready", version: 1 });
  assert.deepEqual(await currentSnapshotAttempt(reopened, "steps"), attempt);
  assert.deepEqual(await snapshotAttempt(reopened, attempt.snapshotId), attempt);
});

test("the_snapshot_identity_is_the_generated_uuid", async (t) => {
  const f = await fixture(t);
  const opts = options("0f8fad5b-d9cb-469f-a165-70867728950e");
  const attempt = begun(await begin(f.database, new ScriptedIssuer(), request(), opts));
  assert.equal(attempt.snapshotId, "0f8fad5b-d9cb-469f-a165-70867728950e");
  assert.equal(opts.uuidCalls, 1);
});

test("begin_records_the_issued_token_and_unread_since", async (t) => {
  const f = await fixture(t);
  const issuer = new ScriptedIssuer([{ outcome: "issued", changesToken: " token with spaces ", unreadSinceMs: -5 }]);
  const attempt = begun(await begin(f.database, issuer));
  assert.equal(attempt.changesToken, " token with spaces ");
  assert.equal(attempt.unreadSinceMs, -5);
  assert.equal(attempt.begunAtMs, NOW);
});

test("reading_an_unknown_attempt_returns_null", async (t) => {
  const f = await fixture(t);
  assert.equal(await currentSnapshotAttempt(f.database, "steps"), null);
  assert.equal(await snapshotAttempt(f.database, uuid(0xdead)), null);
});

// R6 — One current attempt per record type

test("a_current_attempt_refuses_a_new_begin", async (t) => {
  const f = await fixture(t);
  begun(await begin(f.database));
  assert.deepEqual(await begin(f.database), { outcome: "refused", failure: "attempt-open" });
});

test("abandoning_must_name_the_current_attempt", async (t) => {
  const f = await fixture(t);
  assert.deepEqual(await begin(f.database, new ScriptedIssuer(), request("steps", { abandoning: uuid(0xa1) })), { outcome: "refused", failure: "attempt-mismatch" });
  const current = begun(await begin(f.database, new ScriptedIssuer(), request(), options(uuid(0xa1))));
  assert.deepEqual(await begin(f.database, new ScriptedIssuer(), request("steps", { abandoning: uuid(0xa2) })), { outcome: "refused", failure: "attempt-mismatch" });
  begun(await begin(f.database, new ScriptedIssuer(), request("weight"), options(uuid(0xb1))));
  assert.deepEqual(await begin(f.database, new ScriptedIssuer(), request("steps", { abandoning: uuid(0xb1) })), { outcome: "refused", failure: "attempt-mismatch" });
  assert.deepEqual(await currentSnapshotAttempt(f.database, "steps"), current);
});

test("a_named_attempt_is_abandoned_and_retained", async (t) => {
  const f = await fixture(t);
  const first = begun(await begin(f.database, new ScriptedIssuer(), request(), options(uuid(0xa1))));
  const later = { ...options(uuid(0xa2)), now: () => NOW + 1000 };
  const second = begun(await begin(f.database, new ScriptedIssuer(), request("steps", { abandoning: first.snapshotId }), later));
  assert.equal(second.snapshotId, uuid(0xa2));
  assert.equal(second.abandonedAtMs, null);
  assert.deepEqual(await currentSnapshotAttempt(f.database, "steps"), second);
  assert.deepEqual(await snapshotAttempt(f.database, first.snapshotId), { ...first, abandonedAtMs: NOW + 1000 });
  const third = begun(await begin(f.database, new ScriptedIssuer(), request("steps", { abandoning: second.snapshotId }), options(uuid(0xa3))));
  assert.deepEqual(await currentSnapshotAttempt(f.database, "steps"), third);
  assert.equal((await snapshotAttempt(f.database, first.snapshotId))?.abandonedAtMs, NOW + 1000);
});

test("refused_begins_request_no_token", async (t) => {
  const f = await fixture(t);
  begun(await begin(f.database, new ScriptedIssuer(), request(), options(uuid(0xa1))));
  for (const req of [request(), request("steps", { abandoning: uuid(0xa9) })]) {
    const issuer = new ScriptedIssuer();
    const opts = options();
    assert.equal((await begin(f.database, issuer, req, opts)).outcome, "refused");
    assert.deepEqual(issuer.calls, []);
    assert.equal(opts.uuidCalls, 0);
  }
});

test("types_are_independent", async (t) => {
  const f = await fixture(t);
  const steps = begun(await begin(f.database, new ScriptedIssuer(), request("steps"), options(uuid(0xa1))));
  const weight = begun(await begin(f.database, new ScriptedIssuer(), request("weight"), options(uuid(0xb1))));
  const heart = begun(await begin(f.database, new ScriptedIssuer(), request("heart-rate", { installationId: OTHER_INSTALLATION }), options(uuid(0xc1))));
  assert.deepEqual(await currentSnapshotAttempt(f.database, "steps"), steps);
  assert.deepEqual(await currentSnapshotAttempt(f.database, "weight"), weight);
  assert.equal(heart.installationId, OTHER_INSTALLATION);
  const replaced = begun(await begin(f.database, new ScriptedIssuer(), request("weight", { abandoning: weight.snapshotId }), options(uuid(0xb2))));
  assert.deepEqual(await currentSnapshotAttempt(f.database, "steps"), steps);
  assert.deepEqual(await currentSnapshotAttempt(f.database, "weight"), replaced);
});

test("the_commit_rechecks_the_current_attempt", async (t) => {
  const f = await fixture(t);
  // A begin that lands while this one waits for its token wins; this one is refused at commit.
  let inner: BeginResult | undefined;
  const racing = new ScriptedIssuer([], async () => { inner = await begin(f.database, new ScriptedIssuer(), request(), options(uuid(0xa2))); });
  assert.deepEqual(await begin(f.database, racing, request(), options(uuid(0xa1))), { outcome: "refused", failure: "attempt-open" });
  assert.equal(begun(inner!).snapshotId, uuid(0xa2));
  assert.equal((await currentSnapshotAttempt(f.database, "steps"))?.snapshotId, uuid(0xa2));
  assert.equal(await snapshotAttempt(f.database, uuid(0xa1)), null);
  // The same when abandoning: the named attempt was replaced in the meantime.
  let innerAbandon: BeginResult | undefined;
  const racingAbandon = new ScriptedIssuer([], async () => {
    innerAbandon = await begin(f.database, new ScriptedIssuer(), request("steps", { abandoning: uuid(0xa2) }), options(uuid(0xa3)));
  });
  assert.deepEqual(await begin(f.database, racingAbandon, request("steps", { abandoning: uuid(0xa2) }), options(uuid(0xa4))), { outcome: "refused", failure: "attempt-mismatch" });
  assert.equal(begun(innerAbandon!).snapshotId, uuid(0xa3));
  assert.equal((await currentSnapshotAttempt(f.database, "steps"))?.snapshotId, uuid(0xa3));
  assert.equal(await snapshotAttempt(f.database, uuid(0xa4)), null);
});

// R7 — Nothing begun on failure

test("issuer_failures_write_nothing", async (t) => {
  const f = await fixture(t);
  const cases: IssuedToken[] = [
    { outcome: "not-permitted", failure: "read-permission-missing" },
    { outcome: "not-permitted", failure: "history-permission-missing" },
    { outcome: "not-permitted", failure: "read-denied" },
    { outcome: "read-failed", failure: "health-connect-unavailable" },
    { outcome: "read-failed", failure: "health-connect-io-failure" },
    { outcome: "read-failed", failure: "health-connect-request-rejected" },
    { outcome: "read-failed", failure: "unexpected-read-failure" },
    { outcome: "read-failed", failure: "source-contract-violation" },
  ];
  const before = await contents(f.database);
  for (const result of cases) {
    const opts = options();
    assert.deepEqual(await begin(f.database, new ScriptedIssuer([result]), request(), opts), result);
    assert.equal(opts.uuidCalls, 0);
  }
  assert.deepEqual(await contents(f.database), before);
});

test("a_throwing_issuer_is_an_unexpected_read_failure", async (t) => {
  const f = await fixture(t);
  const before = await contents(f.database);
  for (const error of [new Error(`${SENTINEL} bridge`), new TypeError(SENTINEL), new RangeError(SENTINEL)]) {
    assert.deepEqual(await begin(f.database, new ScriptedIssuer([error])), { outcome: "read-failed", failure: "unexpected-read-failure" });
  }
  assert.deepEqual(await contents(f.database), before);
});

test("malformed_issuances_are_violations", async (t) => {
  const f = await fixture(t);
  const malformed: unknown[] = [
    { outcome: "issued", changesToken: "", unreadSinceMs: UNREAD_SINCE },
    { outcome: "issued", changesToken: 7, unreadSinceMs: UNREAD_SINCE },
    { outcome: "issued", changesToken: "t", unreadSinceMs: 1.5 },
    { outcome: "issued", changesToken: "t", unreadSinceMs: Number.MAX_SAFE_INTEGER + 1 },
    { outcome: "issued", changesToken: "t", unreadSinceMs: "1" },
    { outcome: "issued", changesToken: "t" },
    { outcome: "not-permitted", failure: "health-connect-unavailable" },
    { outcome: "read-failed", failure: "read-denied" },
    { outcome: "read-failed", failure: "attempt-open" },
    { outcome: "read-failed", failure: `${SENTINEL}-not-a-failure` },
    { outcome: "unknown" },
    null,
    "issued",
  ];
  const before = await contents(f.database);
  for (const result of malformed) {
    const issuer = new ScriptedIssuer([result as IssuedToken]);
    assert.deepEqual(await begin(f.database, issuer), { outcome: "read-failed", failure: "source-contract-violation" }, JSON.stringify(result));
  }
  assert.deepEqual(await contents(f.database), before);
});

test("storage_errors_roll_back_everything", async (t) => {
  const f = await fixture(t);
  const first = begun(await begin(f.database, new ScriptedIssuer(), request(), options(uuid(0xa1))));
  const before = await contents(f.database);
  // Fail each mutating statement in turn until the begin needs no more; nothing may persist.
  let failurePoints = 0;
  for (let mutation = 1; ; mutation++) {
    const database = failing(f.database, { mutation });
    const result = await begin(database, new ScriptedIssuer(), request("steps", { abandoning: first.snapshotId }), options(uuid(0xa2)));
    if (result.outcome === "begun") break;
    assert.deepEqual(result, { outcome: "storage-failed", failure: "storage-failed" });
    assert.deepEqual(await contents(f.database), before, `failing mutation ${mutation}`);
    failurePoints++;
    assert.ok(mutation < 20, "the begin never completed");
  }
  assert.ok(failurePoints >= 2, "an abandoning begin writes the abandonment and the new attempt");
  // A failing read before the token request is a storage failure too, and requests no token.
  const issuer = new ScriptedIssuer();
  const reading = failing(f.database, { queries: true });
  assert.deepEqual(await begin(reading, issuer, request("weight")), { outcome: "storage-failed", failure: "storage-failed" });
  assert.deepEqual(issuer.calls, []);
});

test("refusals_write_nothing", async (t) => {
  const f = await fixture(t);
  begun(await begin(f.database, new ScriptedIssuer(), request(), options(uuid(0xa1))));
  const before = await contents(f.database);
  await begin(f.database, new ScriptedIssuer(), request());
  await begin(f.database, new ScriptedIssuer(), request("steps", { abandoning: uuid(0xa9) }));
  assert.deepEqual(await contents(f.database), before);
});

// R8 — The Sender's first schema migration

test("migration_creates_version_one", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const database = await openNodeDatabase(join(directory, "synthetic.sqlite"));
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.equal(snapshotAttemptsMigration.version, 1);
  assert.deepEqual(await database.query("PRAGMA user_version"), [{ user_version: 0 }]);
  assert.deepEqual(await migrate(database, [snapshotAttemptsMigration]), { status: "ready", version: 1 });
  const tables = await database.query("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name");
  assert.deepEqual(tables.map((row) => row.name), ["current_snapshot_attempts", "snapshot_attempts"]);
});

test("the_schema_enforces_one_current_attempt", async (t) => {
  const f = await fixture(t);
  const insert = (id: string, kind: string) => f.database.execute(
    "INSERT INTO snapshot_attempts (snapshot_id, record_type, kind, installation_id, changes_token, unread_since_ms, begun_at_ms, abandoned_at_ms) VALUES (?, 'steps', ?, ?, 't', 0, 0, NULL)",
    [id, kind, INSTALLATION],
  );
  await insert(uuid(1), "initial");
  await insert(uuid(2), "reconciliation");
  await assert.rejects(insert(uuid(3), "other"));
  await f.database.execute("INSERT INTO current_snapshot_attempts (record_type, snapshot_id) VALUES ('steps', ?)", [uuid(1)]);
  await assert.rejects(f.database.execute("INSERT INTO current_snapshot_attempts (record_type, snapshot_id) VALUES ('steps', ?)", [uuid(2)]));
  await assert.rejects(f.database.execute("INSERT INTO current_snapshot_attempts (record_type, snapshot_id) VALUES ('weight', ?)", [uuid(1)]));
  await assert.rejects(f.database.execute("INSERT INTO current_snapshot_attempts (record_type, snapshot_id) VALUES ('weight', ?)", [uuid(9)]));
  // A current attempt must be of the record type it is current for.
  await assert.rejects(f.database.execute("INSERT INTO current_snapshot_attempts (record_type, snapshot_id) VALUES ('weight', ?)", [uuid(2)]));
});

test("migration_is_idempotent", async (t) => {
  const f = await fixture(t);
  begun(await begin(f.database));
  const before = await contents(f.database);
  const schema = await f.database.query("SELECT type, name, sql FROM sqlite_master ORDER BY name");
  assert.deepEqual(await migrate(f.database, [snapshotAttemptsMigration]), { status: "ready", version: 1 });
  assert.deepEqual(await contents(f.database), before);
  assert.deepEqual(await f.database.query("SELECT type, name, sql FROM sqlite_master ORDER BY name"), schema);
});

// R9 — Caller errors

test("malformed_requests_are_caller_errors", async (t) => {
  const f = await fixture(t);
  const bad: BeginRequest[] = [
    request(""), request("Steps"), request("steps-"), request("steps record"), request("a".repeat(65)),
    request("steps", { installationId: "00000000-0000-4000-8000-00000000000A" }),
    request("steps", { installationId: "not-a-uuid" }),
    request("steps", { abandoning: "" }),
    request("steps", { abandoning: "{00000000-0000-4000-8000-000000000001}" }),
  ];
  const before = await contents(f.database);
  for (const req of bad) {
    const issuer = new ScriptedIssuer();
    await assert.rejects(begin(f.database, issuer, req), TypeError, JSON.stringify(req));
    assert.deepEqual(issuer.calls, []);
  }
  assert.deepEqual(await contents(f.database), before);
  begun(await begin(f.database, new ScriptedIssuer(), request("a".repeat(64))));
});

test("a_bad_uuid_source_is_a_caller_error", async (t) => {
  const f = await fixture(t);
  const before = await contents(f.database);
  for (const value of ["", "00000000-0000-1000-8000-000000000001", "00000000-0000-4000-C000-000000000001", "0F8FAD5B-D9CB-469F-A165-70867728950E", "not-a-uuid"]) {
    const issuer = new ScriptedIssuer();
    await assert.rejects(begin(f.database, issuer, request(), options(value)), TypeError, value);
    assert.deepEqual(issuer.calls, ["steps"]);
  }
  assert.deepEqual(await contents(f.database), before);
});

// R10 — Health-data-free outcomes

test("non_begun_results_carry_only_named_failures", async (t) => {
  const f = await fixture(t);
  begun(await begin(f.database, new ScriptedIssuer([{ outcome: "issued", changesToken: `${SENTINEL}-token`, unreadSinceMs: UNREAD_SINCE }]), request("weight")));
  const results = [
    await begin(f.database, new ScriptedIssuer(), request("weight")),
    await begin(f.database, new ScriptedIssuer([new Error(`${SENTINEL} bridge failure`)])),
    await begin(f.database, new ScriptedIssuer([{ outcome: "issued", changesToken: "", unreadSinceMs: UNREAD_SINCE }])),
    await begin(failing(f.database, { mutation: 1 }), new ScriptedIssuer([{ outcome: "issued", changesToken: `${SENTINEL}-token`, unreadSinceMs: UNREAD_SINCE }])),
  ];
  for (const result of results) {
    assert.notEqual(result.outcome, "begun");
    assert.deepEqual(Object.keys(result).sort(), ["failure", "outcome"]);
    assert.ok(!JSON.stringify(result).includes(SENTINEL), JSON.stringify(result));
    assert.ok(!JSON.stringify(result).includes(String(UNREAD_SINCE)), JSON.stringify(result));
  }
});

test("every_named_failure_is_reachable_and_in_the_contract", async (t) => {
  const f = await fixture(t);
  const observed = new Set<string>();
  const record = (result: BeginResult) => {
    const classes: Record<string, string> = { "refused": "refusal", "not-permitted": "not-permitted", "read-failed": "read-failure", "storage-failed": "storage-failure" };
    assert.notEqual(result.outcome, "begun");
    observed.add(`${classes[result.outcome]}:${(result as { failure: string }).failure}`);
  };
  const current = begun(await begin(f.database, new ScriptedIssuer(), request(), options(uuid(0xa1))));
  record(await begin(f.database, new ScriptedIssuer(), request()));
  record(await begin(f.database, new ScriptedIssuer(), request("steps", { abandoning: uuid(0xa9) })));
  record(await begin(failing(f.database, { mutation: 1 }), new ScriptedIssuer(), request("steps", { abandoning: current.snapshotId })));
  record(await begin(f.database, new ScriptedIssuer([new Error("bridge")]), request("weight")));
  record(await begin(f.database, new ScriptedIssuer([{ outcome: "issued", changesToken: "", unreadSinceMs: 0 }]), request("weight")));
  for (const failure of CONTRACT.named_failures as { name: string; outcome: string }[]) {
    if (failure.outcome === "not-permitted") record(await begin(f.database, new ScriptedIssuer([{ outcome: "not-permitted", failure: failure.name }]), request("weight")));
    if (failure.outcome === "read-failure") record(await begin(f.database, new ScriptedIssuer([{ outcome: "read-failed", failure: failure.name }]), request("weight")));
  }
  const named = new Set((CONTRACT.named_failures as { name: string; outcome: string }[]).map((failure) => `${failure.outcome}:${failure.name}`));
  assert.deepEqual([...observed].sort(), [...named].sort());
});

// R11 — Order of checks

test("typescript_checks_follow_the_documented_order", async (t) => {
  const f = await fixture(t);
  begun(await begin(f.database, new ScriptedIssuer(), request(), options(uuid(0xa1))));
  // Caller errors first, even with an open attempt and a failing store.
  const callerIssuer = new ScriptedIssuer();
  await assert.rejects(begin(failing(f.database, { queries: true }), callerIssuer, request("steps", { installationId: "bad" })), TypeError);
  assert.deepEqual(callerIssuer.calls, []);
  // Current-attempt refusals before the issuer.
  const refusedIssuer = new ScriptedIssuer([{ outcome: "read-failed", failure: "health-connect-unavailable" }]);
  assert.deepEqual(await begin(f.database, refusedIssuer, request()), { outcome: "refused", failure: "attempt-open" });
  assert.deepEqual(refusedIssuer.calls, []);
  // The issuer outcome before the UUID source.
  const deniedOptions = options("not-a-uuid");
  assert.deepEqual(await begin(f.database, new ScriptedIssuer([{ outcome: "not-permitted", failure: "read-denied" }]), request("weight"), deniedOptions),
    { outcome: "not-permitted", failure: "read-denied" });
  assert.equal(deniedOptions.uuidCalls, 0);
  // The UUID source before the commit.
  await assert.rejects(begin(failing(f.database, { mutation: 1 }), new ScriptedIssuer(), request("weight"), options("not-a-uuid")), TypeError);
  // The commit last: a storage failure after a valid token and identity.
  assert.deepEqual(await begin(failing(f.database, { mutation: 1 }), new ScriptedIssuer(), request("weight")), { outcome: "storage-failed", failure: "storage-failed" });
});
