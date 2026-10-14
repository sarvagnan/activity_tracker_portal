// Acceptance tests for the primitive that begin-initial-snapshot@2 adds to @1.
// Requirement numbers refer to packs/begin-initial-snapshot-2.spec.md; @1's own suites run
// unchanged through its bound runner. Storage is the 3ug node:sqlite SenderDatabase substitute.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import type { TestContext } from "node:test";
import {
  beginInitialSnapshot,
  commitSnapshotAttempt,
  currentSnapshotAttempt,
  snapshotAttempt,
  snapshotAttemptsMigration,
} from "../src/snapshot/begin-initial-snapshot.ts";
import type { CommitAttemptResult, NewSnapshotAttempt, SnapshotAttempt } from "../src/snapshot/begin-initial-snapshot.ts";
import type { DatabaseValue, SenderDatabase } from "../src/storage/database.ts";
import { migrate } from "../src/storage/migrations.ts";
import { openNodeDatabase } from "./support/node-database.ts";

const CONTRACT = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../../../governance/scopes/begin-initial-snapshot.2.json"), "utf8"));
const INSTALLATION = "00000000-0000-4000-8000-000000000001";
const OTHER_INSTALLATION = "00000000-0000-4000-8000-000000000002";
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

function attempt(n: number, overrides: Partial<NewSnapshotAttempt> = {}): NewSnapshotAttempt {
  return {
    snapshotId: uuid(n), recordType: "steps", kind: "reconciliation", installationId: INSTALLATION,
    changesToken: `synthetic-changes-token-${n}`, unreadSinceMs: 1_791_000_000_000 + n, begunAtMs: 1_791_000_005_000 + n,
    ...overrides,
  };
}

/** Commit inside a transaction the test opens, as every caller must. */
function commit(database: SenderDatabase, next: NewSnapshotAttempt, superseding: string | null = null): Promise<CommitAttemptResult> {
  return database.transaction((transaction) => commitSnapshotAttempt(transaction, next, superseding));
}

function committed(result: CommitAttemptResult): SnapshotAttempt {
  assert.equal(result.outcome, "committed", JSON.stringify(result));
  return (result as { attempt: SnapshotAttempt }).attempt;
}

const stored = (next: NewSnapshotAttempt, abandonedAtMs: number | null = null): SnapshotAttempt => ({ ...next, abandonedAtMs });

async function contents(database: SenderDatabase) {
  return {
    version: await database.query("PRAGMA user_version"),
    attempts: await database.query("SELECT * FROM snapshot_attempts ORDER BY snapshot_id"),
    current: await database.query("SELECT * FROM current_snapshot_attempts ORDER BY record_type"),
  };
}

const MUTATION = /^\s*(?:WITH\b[\s\S]*?\b)?(INSERT|UPDATE|DELETE|REPLACE)\b/i;
const CONTROL = /^\s*(BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b/i;

/** Wraps a database so that its numbered mutating statement (counted from 1) fails. */
function failing(database: SenderDatabase, mutation: number, error: Error): SenderDatabase {
  let mutations = 0;
  const guard = (sql: string) => { if (MUTATION.test(sql) && ++mutations === mutation) throw error; };
  const wrap = (inner: SenderDatabase): SenderDatabase => ({
    async execute(sql: string, parameters?: readonly DatabaseValue[]) { guard(sql); return inner.execute(sql, parameters); },
    async query(sql: string, parameters?: readonly DatabaseValue[]) { guard(sql); return inner.query(sql, parameters); },
    transaction: <T>(fn: (transaction: SenderDatabase) => T | Promise<T>) => inner.transaction((transaction) => fn(wrap(transaction))),
    close: () => inner.close(),
  });
  return wrap(database);
}

/** A transaction handle that records its statements and refuses any nested transaction or close. */
function recording(transaction: SenderDatabase) {
  const statements: string[] = [];
  const forbidden: string[] = [];
  const handle: SenderDatabase = {
    async execute(sql, parameters) { statements.push(sql); return transaction.execute(sql, parameters); },
    async query(sql, parameters) { statements.push(sql); return transaction.query(sql, parameters); },
    async transaction() { forbidden.push("transaction"); throw new Error("The primitive must not open a transaction"); },
    async close() { forbidden.push("close"); throw new Error("The primitive must not close the handle"); },
  };
  return { handle, statements, forbidden };
}

/** A handle whose every use throws and is counted, to prove validation comes first. */
function untouchable() {
  let calls = 0;
  const refuse = () => { calls++; throw new Error(`${SENTINEL} storage must not be touched`); };
  const handle: SenderDatabase = {
    async execute() { refuse(); },
    async query() { return refuse(); },
    async transaction() { return refuse(); },
    async close() {},
  };
  return { handle, calls: () => calls };
}

// R13 — The primitive commits an attempt of either kind

test("the_primitive_commits_an_initial_attempt", async (t) => {
  const f = await fixture(t);
  const next = attempt(0xa1, { kind: "initial", changesToken: " token with spaces ", unreadSinceMs: -5 });
  assert.deepEqual(await commit(f.database, next), { outcome: "committed", attempt: stored(next) });
  assert.deepEqual(await currentSnapshotAttempt(f.database, "steps"), stored(next));
  assert.deepEqual(await snapshotAttempt(f.database, uuid(0xa1)), stored(next));
});

test("the_primitive_commits_a_reconciliation_attempt", async (t) => {
  const f = await fixture(t);
  const next = attempt(0xb1, { recordType: "heart-rate", installationId: OTHER_INSTALLATION });
  assert.deepEqual(committed(await commit(f.database, next)), stored(next));
  assert.deepEqual(await currentSnapshotAttempt(f.database, "heart-rate"), stored(next));
  assert.equal(await currentSnapshotAttempt(f.database, "steps"), null);
});

test("a_committed_attempt_survives_reopening", async (t) => {
  const f = await fixture(t);
  const next = attempt(0xa1);
  committed(await commit(f.database, next));
  const reopened = await f.reopen();
  assert.deepEqual(await migrate(reopened, [snapshotAttemptsMigration]), { status: "ready", version: 1 });
  assert.deepEqual(await currentSnapshotAttempt(reopened, "steps"), stored(next));
});

// R14 — The one-current-attempt rule

test("an_open_attempt_refuses_an_unnamed_commit", async (t) => {
  const f = await fixture(t);
  committed(await commit(f.database, attempt(0xa1, { kind: "initial" })));
  assert.deepEqual(await commit(f.database, attempt(0xa2)), { outcome: "refused", failure: "attempt-open" });
  assert.equal((await currentSnapshotAttempt(f.database, "steps"))?.snapshotId, uuid(0xa1));
  const refusals = (CONTRACT.named_failures as { name: string; outcome: string }[]).filter((failure) => failure.outcome === "refusal");
  assert.deepEqual(refusals.map((failure) => failure.name).sort(), ["attempt-mismatch", "attempt-open"]);
});

test("superseding_must_name_the_current_attempt", async (t) => {
  const f = await fixture(t);
  const mismatch = { outcome: "refused", failure: "attempt-mismatch" };
  assert.deepEqual(await commit(f.database, attempt(0xa1), uuid(0xa9)), mismatch, "no current attempt");
  committed(await commit(f.database, attempt(0xa1)));
  committed(await commit(f.database, attempt(0xb1, { recordType: "weight" })));
  assert.deepEqual(await commit(f.database, attempt(0xa2), uuid(0xb1)), mismatch, "another type's attempt");
  assert.deepEqual(await commit(f.database, attempt(0xa2), uuid(0xa9)), mismatch, "an unknown attempt");
  committed(await commit(f.database, attempt(0xa2), uuid(0xa1)));
  assert.deepEqual(await commit(f.database, attempt(0xa3), uuid(0xa1)), mismatch, "an attempt no longer current");
  assert.equal((await currentSnapshotAttempt(f.database, "steps"))?.snapshotId, uuid(0xa2));
});

test("a_superseded_attempt_is_abandoned_and_retained", async (t) => {
  const f = await fixture(t);
  const initial = attempt(0xa1, { kind: "initial" });
  const first = attempt(0xa2, { begunAtMs: 2_000 });
  const second = attempt(0xa3, { begunAtMs: 3_000 });
  committed(await commit(f.database, initial));
  assert.deepEqual(committed(await commit(f.database, first, uuid(0xa1))), stored(first));
  assert.deepEqual(await snapshotAttempt(f.database, uuid(0xa1)), stored(initial, 2_000));
  committed(await commit(f.database, second, uuid(0xa2)));
  assert.deepEqual(await snapshotAttempt(f.database, uuid(0xa2)), stored(first, 3_000));
  assert.deepEqual(await snapshotAttempt(f.database, uuid(0xa1)), stored(initial, 2_000));
  assert.deepEqual(await currentSnapshotAttempt(f.database, "steps"), stored(second));
});

test("refused_commits_write_nothing", async (t) => {
  const f = await fixture(t);
  committed(await commit(f.database, attempt(0xa1)));
  const before = await contents(f.database);
  for (const [next, superseding] of [[attempt(0xa2), null], [attempt(0xa2), uuid(0xa9)], [attempt(0xa2, { kind: "initial" }), null]] as const) {
    assert.equal((await commit(f.database, next, superseding)).outcome, "refused");
  }
  assert.deepEqual(await contents(f.database), before);
});

test("types_are_independent_for_the_primitive", async (t) => {
  const f = await fixture(t);
  const steps = attempt(0xa1);
  const weight = attempt(0xb1, { recordType: "weight", kind: "initial" });
  committed(await commit(f.database, steps));
  committed(await commit(f.database, weight));
  const replacement = attempt(0xb2, { recordType: "weight" });
  committed(await commit(f.database, replacement, uuid(0xb1)));
  assert.deepEqual(await currentSnapshotAttempt(f.database, "steps"), stored(steps));
  assert.deepEqual(await currentSnapshotAttempt(f.database, "weight"), stored(replacement));
});

test("the_primitive_and_begin_initial_snapshot_share_one_rule", async (t) => {
  const f = await fixture(t);
  const issuer = (token: string) => ({ async issue() { return { outcome: "issued" as const, changesToken: token, unreadSinceMs: 7 }; } });
  const options = (id: string) => ({ randomUUID: () => id, now: () => 9_000 });
  const begun = await beginInitialSnapshot(f.database, issuer("t1"), { recordType: "steps", installationId: INSTALLATION }, options(uuid(0xa1)));
  assert.equal(begun.outcome, "begun");
  assert.deepEqual(await commit(f.database, attempt(0xa2)), { outcome: "refused", failure: "attempt-open" });
  committed(await commit(f.database, attempt(0xa2), uuid(0xa1)));
  assert.deepEqual(
    await beginInitialSnapshot(f.database, issuer("t2"), { recordType: "steps", installationId: INSTALLATION }, options(uuid(0xa3))),
    { outcome: "refused", failure: "attempt-open" },
  );
  const restarted = await beginInitialSnapshot(f.database, issuer("t3"), { recordType: "steps", installationId: INSTALLATION, abandoning: uuid(0xa2) }, options(uuid(0xa4)));
  assert.equal(restarted.outcome, "begun");
  assert.equal((await snapshotAttempt(f.database, uuid(0xa2)))?.abandonedAtMs, 9_000);
  assert.equal((await currentSnapshotAttempt(f.database, "steps"))?.kind, "initial");
});

// R15 — Only inside the caller's transaction

test("a_callers_rollback_undoes_the_commit", async (t) => {
  const f = await fixture(t);
  committed(await commit(f.database, attempt(0xa1)));
  const before = await contents(f.database);
  for (const superseding of [uuid(0xa1), null]) {
    const next = attempt(0xa2, { recordType: superseding ? "steps" : "weight" });
    await assert.rejects(f.database.transaction(async (transaction) => {
      committed(await commitSnapshotAttempt(transaction, next, superseding));
      throw new Error(`${SENTINEL} caller failure after the commit`);
    }), new RegExp(SENTINEL));
    assert.deepEqual(await contents(f.database), before);
  }
});

test("database_errors_propagate_to_the_caller", async (t) => {
  const f = await fixture(t);
  committed(await commit(f.database, attempt(0xa1)));
  const before = await contents(f.database);
  let points = 0;
  for (let mutation = 1; ; mutation++) {
    const error = new Error(`${SENTINEL} synthetic write failure ${mutation}`);
    const database = failing(f.database, mutation, error);
    const result = await database.transaction((transaction) => commitSnapshotAttempt(transaction, attempt(0xa2), uuid(0xa1))).catch((thrown: unknown) => thrown);
    if (result === error) {
      points++;
      assert.deepEqual(await contents(f.database), before, `failing mutation ${mutation}`);
      assert.ok(mutation < 10, "the commit never completed");
      continue;
    }
    committed(result as CommitAttemptResult);
    break;
  }
  assert.ok(points >= 2, "superseding writes the abandonment and the new attempt");
});

test("a_reused_snapshot_identity_is_rejected", async (t) => {
  const f = await fixture(t);
  committed(await commit(f.database, attempt(0xa1)));
  committed(await commit(f.database, attempt(0xa2), uuid(0xa1)));
  const before = await contents(f.database);
  await assert.rejects(commit(f.database, attempt(0xa1, { recordType: "weight" })), "an abandoned attempt's identity");
  await assert.rejects(commit(f.database, attempt(0xa2, { recordType: "weight" })), "the current attempt's identity");
  await assert.rejects(commit(f.database, attempt(0xa2), uuid(0xa2)), "superseding an attempt with its own identity");
  assert.deepEqual(await contents(f.database), before);
});

test("the_primitive_never_opens_a_transaction_or_reads_a_clock", async (t) => {
  const f = await fixture(t);
  committed(await commit(f.database, attempt(0xa1)));
  const originals = { now: Date.now, random: Math.random, uuid: crypto.randomUUID, values: crypto.getRandomValues };
  const trap = () => { throw new Error("The primitive must not read a clock or random source"); };
  const records: ReturnType<typeof recording>[] = [];
  await f.database.transaction(async (transaction) => {
    Date.now = trap;
    Math.random = trap;
    crypto.randomUUID = trap as typeof crypto.randomUUID;
    crypto.getRandomValues = trap as typeof crypto.getRandomValues;
    try {
      for (const [next, superseding] of [[attempt(0xa2), uuid(0xa1)], [attempt(0xa3), null], [attempt(0xb1, { recordType: "weight" }), null]] as const) {
        const record = recording(transaction);
        records.push(record);
        await commitSnapshotAttempt(record.handle, next, superseding);
      }
    } finally {
      Object.assign(Date, { now: originals.now });
      Object.assign(Math, { random: originals.random });
      Object.assign(crypto, { randomUUID: originals.uuid, getRandomValues: originals.values });
    }
  });
  for (const record of records) {
    assert.deepEqual(record.forbidden, []);
    assert.ok(record.statements.length > 0);
    assert.deepEqual(record.statements.filter((sql) => CONTROL.test(sql)), []);
  }
  assert.equal((await currentSnapshotAttempt(f.database, "steps"))?.snapshotId, uuid(0xa2));
  assert.equal((await currentSnapshotAttempt(f.database, "weight"))?.snapshotId, uuid(0xb1));
});

// R16 — Caller errors and compatibility

test("malformed_commits_are_caller_errors", async () => {
  const probe = untouchable();
  const bad: [unknown, unknown][] = [
    [attempt(0xa1, { snapshotId: "00000000-0000-1000-8000-000000000001" }), null],
    [attempt(0xa1, { snapshotId: "00000000-0000-4000-c000-000000000001" }), null],
    [attempt(0xa1, { snapshotId: "00000000-0000-4000-8000-0000000000A1" }), null],
    [attempt(0xa1, { snapshotId: "not-a-uuid" }), null],
    [attempt(0xa1, { recordType: "Steps" }), null],
    [attempt(0xa1, { recordType: "a".repeat(65) }), null],
    [attempt(0xa1, { kind: "other" as NewSnapshotAttempt["kind"] }), null],
    [attempt(0xa1, { installationId: "00000000-0000-4000-8000-00000000000A" }), null],
    [attempt(0xa1, { changesToken: "" }), null],
    [attempt(0xa1, { changesToken: 7 as unknown as string }), null],
    [attempt(0xa1, { unreadSinceMs: 1.5 }), null],
    [attempt(0xa1, { begunAtMs: Number.MAX_SAFE_INTEGER + 1 }), null],
    [attempt(0xa1, { begunAtMs: "1" as unknown as number }), null],
    [attempt(0xa1), ""],
    [attempt(0xa1), "{00000000-0000-4000-8000-000000000001}"],
    [attempt(0xa1), 7],
    [null, null],
    ["attempt", null],
  ];
  for (const [next, superseding] of bad) {
    await assert.rejects(commitSnapshotAttempt(probe.handle, next as NewSnapshotAttempt, superseding as string | null), TypeError, JSON.stringify([next, superseding]));
  }
  assert.equal(probe.calls(), 0);
});
