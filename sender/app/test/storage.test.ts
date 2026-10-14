import assert from "node:assert/strict";
import { mkdtempSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { constants } from "node:sqlite";
import type { DatabaseSync } from "node:sqlite";
import type { TestContext } from "node:test";
import { configuredDatabase } from "../src/storage/connection.ts";
import type { SenderDatabase } from "../src/storage/database.ts";
import { migrate, senderMigrations } from "../src/storage/migrations.ts";
import type { Migration } from "../src/storage/migrations.ts";
import { openNodeDatabase } from "./support/node-database.ts";

async function fixture(t: TestContext, configure?: (database: DatabaseSync) => void) {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const path = join(directory, "synthetic.sqlite");
  let database = await openNodeDatabase(path, configure);
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  return {
    get database() { return database; },
    path,
    async reopen() { await database.close(); database = await openNodeDatabase(path); return database; },
  };
}

const syntheticMigrations: readonly Migration[] = [
  { version: 1, async apply(tx) {
    await tx.execute("CREATE TABLE synthetic (id INTEGER PRIMARY KEY, bytes BLOB NOT NULL)");
    await tx.execute("INSERT INTO synthetic VALUES (?, ?)", [1, new Uint8Array([0, 255, 17])]);
  } },
  { version: 2, async apply(tx) { await tx.execute("ALTER TABLE synthetic ADD COLUMN label TEXT"); } },
];

test("opening applies and verifies DELETE, FULL, and foreign keys on every connection", async (t) => {
  const f = await fixture(t);
  for (let opening = 0; opening < 2; opening++) {
    const db = opening === 0 ? f.database : await f.reopen();
    assert.deepEqual(await db.query("PRAGMA journal_mode"), [{ journal_mode: "delete" }]);
    assert.deepEqual(await db.query("PRAGMA synchronous"), [{ synchronous: 2 }]);
    assert.deepEqual(await db.query("PRAGMA foreign_keys"), [{ foreign_keys: 1 }]);
  }
  await f.database.execute("CREATE TABLE synthetic_parent (id INTEGER PRIMARY KEY)");
  await f.database.execute("CREATE TABLE synthetic_child (parent INTEGER REFERENCES synthetic_parent(id))");
  await assert.rejects(f.database.execute("INSERT INTO synthetic_child VALUES (?)", [1]), /FOREIGN KEY/);
});

test("an open that cannot provide DELETE fails rather than weakening settings", async () => {
  await assert.rejects(openNodeDatabase(":memory:"), /Required SQLite settings unavailable/);
});

for (const setting of ["journal_mode", "synchronous", "foreign_keys"]) {
  test(`opening fails and closes the driver when ${setting} cannot be verified`, async () => {
    let closed = false;
    const required: Record<string, string | number> = { journal_mode: "delete", synchronous: 2, foreign_keys: 1 };
    await assert.rejects(configuredDatabase({
      async execute() {},
      async query(sql) {
        const name = sql.slice("PRAGMA ".length);
        return [{ [name]: name === setting ? "synthetic-invalid-setting" : required[name] }];
      },
      async close() { closed = true; },
    }), /Required SQLite settings unavailable/);
    assert.equal(closed, true);
  });
}

for (const failureAt of ["callback", "commit"]) {
  test(`failed rollback preserves the original ${failureAt} error and poisons the handle`, async () => {
    const original = new Error("synthetic original failure");
    const statements: string[] = [];
    const required: Record<string, string | number> = { journal_mode: "delete", synchronous: 2, foreign_keys: 1 };
    const database = await configuredDatabase({
      async execute(sql) {
        statements.push(sql);
        if (sql === "ROLLBACK") throw new Error("synthetic rollback failure");
        if (sql === "COMMIT" && failureAt === "commit") throw original;
      },
      async query(sql) {
        const name = sql.slice("PRAGMA ".length);
        return [{ [name]: required[name] }];
      },
      async close() {},
    });
    let expired: SenderDatabase | undefined;
    await assert.rejects(database.transaction((tx) => {
      expired = tx;
      if (failureAt === "callback") throw original;
    }), (error) => error === original);
    assert.equal(statements.at(-1), "ROLLBACK");
    await assert.rejects(expired!.query("SELECT 1"), /Transaction has ended/);
    await assertPoisoned(database);
    await database.close();
  });
}

async function assertPoisoned(database: SenderDatabase) {
  const unusable = /Database connection is unusable after a failed rollback; close it/;
  await assert.rejects(database.execute("INSERT INTO retained VALUES ('synthetic-later')"), unusable);
  await assert.rejects(database.query("SELECT 1"), unusable);
  let called = false;
  await assert.rejects(database.transaction(() => { called = true; }), unusable);
  assert.equal(called, false);
}

for (const failureAt of ["callback", "migration"]) {
  test(`real SQLite denied ROLLBACK poisons the handle after ${failureAt} failure until close`, async (t) => {
    let deniedRollbacks = 0;
    let raw!: DatabaseSync;
    const f = await fixture(t, (database) => {
      raw = database;
      database.setAuthorizer((action, arg1) => {
        if (action === constants.SQLITE_TRANSACTION && arg1 === "ROLLBACK") {
          deniedRollbacks++;
          return constants.SQLITE_DENY;
        }
        return constants.SQLITE_OK;
      });
    });
    await f.database.execute("CREATE TABLE retained (value TEXT)");
    await f.database.execute("INSERT INTO retained VALUES (?)", ["synthetic-committed"]);
    const original = new Error("synthetic original callback failure");
    const fail = async (tx: SenderDatabase) => {
      await tx.execute("INSERT INTO retained VALUES (?)", ["synthetic-abandoned"]);
      await tx.execute("CREATE TABLE abandoned (value INTEGER)");
      throw original;
    };
    if (failureAt === "migration") {
      assert.deepEqual(await migrate(f.database, [{ version: 1, apply: fail }]), {
        status: "frozen", version: 0, reason: "migration-failed",
      });
    } else {
      await assert.rejects(f.database.transaction(fail), (error) => error === original);
    }
    assert.equal(deniedRollbacks, 1);
    assert.equal(raw.isTransaction, true);
    await assertPoisoned(f.database);
    // Reopen closes the poisoned connection; SQLite discards the abandoned work.
    const reopened = await f.reopen();
    assert.equal(raw.isOpen, false);
    assert.deepEqual(await reopened.query("SELECT * FROM retained"), [{ value: "synthetic-committed" }]);
    assert.deepEqual(await reopened.query("SELECT name FROM sqlite_schema WHERE name='abandoned'"), []);
    assert.deepEqual(await reopened.query("PRAGMA user_version"), [{ user_version: 0 }]);
  });
}

test("transaction commits all statements atomically and returns the callback result", async (t) => {
  const { database } = await fixture(t);
  const result = await database.transaction(async (tx) => {
    await tx.execute("CREATE TABLE synthetic (value INTEGER)");
    await tx.execute("INSERT INTO synthetic VALUES (?)", [7]);
    await tx.execute("INSERT INTO synthetic VALUES (?)", [8]);
    return "committed";
  });
  assert.equal(result, "committed");
  assert.deepEqual(await database.query("SELECT value FROM synthetic ORDER BY value"), [{ value: 7 }, { value: 8 }]);
});

test("a separate SQLite reader observes all writes only after commit", async (t) => {
  const f = await fixture(t);
  await f.database.execute("CREATE TABLE synthetic (value INTEGER)");
  const reader = await openNodeDatabase(f.path);
  try {
    await reader.execute("PRAGMA busy_timeout=1000");
    await f.database.transaction(async (tx) => {
      await tx.execute("INSERT INTO synthetic VALUES (?)", [1]);
      await tx.execute("INSERT INTO synthetic VALUES (?)", [2]);
      assert.deepEqual(await reader.query("SELECT * FROM synthetic"), []);
    });
    assert.deepEqual(await reader.query("SELECT * FROM synthetic ORDER BY value"), [{ value: 1 }, { value: 2 }]);
  } finally { await reader.close(); }
});

for (const kind of ["throw", "reject"] as const) {
  test(`transaction rolls back DDL and data on ${kind}`, async (t) => {
    const { database } = await fixture(t);
    await database.execute("CREATE TABLE retained (value TEXT)");
    await database.execute("INSERT INTO retained VALUES (?)", ["synthetic-original"]);
    const failure = new Error("synthetic failure");
    await assert.rejects(database.transaction(async (tx) => {
      await tx.execute("CREATE TABLE rolled_back (value INTEGER)");
      await tx.execute("UPDATE retained SET value=?", ["synthetic-changed"]);
      if (kind === "throw") throw failure;
      await Promise.reject(failure);
    }), (error) => error === failure);
    assert.deepEqual(await database.query("SELECT value FROM retained"), [{ value: "synthetic-original" }]);
    assert.deepEqual(await database.query("SELECT name FROM sqlite_schema WHERE name=?", ["rolled_back"]), []);
    await database.transaction(async (tx) => { await tx.execute("INSERT INTO retained VALUES (?)", ["synthetic-next"]); });
    assert.equal((await database.query("SELECT * FROM retained")).length, 2);
  });
}

test("nested transactions and unrelated work cannot join an active transaction", async (t) => {
  const { database } = await fixture(t);
  let released: SenderDatabase | undefined;
  await database.transaction(async (tx) => {
    released = tx;
    await assert.rejects(tx.transaction(() => undefined), /Nested transactions/);
    await assert.rejects(database.transaction(() => undefined), /cannot nest/);
    await assert.rejects(database.execute("CREATE TABLE unintended (value TEXT)"), /already active/);
    await assert.rejects(database.query("SELECT 1"), /already active/);
    await assert.rejects(database.close(), /already active/);
    await tx.execute("CREATE TABLE synthetic (value TEXT)");
  });
  await assert.rejects(released!.query("SELECT 1"), /Transaction has ended/);
  assert.deepEqual(await database.query("SELECT name FROM sqlite_schema WHERE name=?", ["unintended"]), []);
});

test("commit failure from a deferred foreign key rolls the whole transaction back", async (t) => {
  const { database } = await fixture(t);
  await database.execute("CREATE TABLE synthetic_parent (id INTEGER PRIMARY KEY)");
  await database.execute("CREATE TABLE synthetic_child (parent INTEGER REFERENCES synthetic_parent(id) DEFERRABLE INITIALLY DEFERRED)");
  await assert.rejects(database.transaction(async (tx) => {
    await tx.execute("INSERT INTO synthetic_child VALUES (?)", [1]);
  }), /FOREIGN KEY/);
  assert.deepEqual(await database.query("SELECT * FROM synthetic_child"), []);
});

test("BLOBs, subarrays, empty bytes, null, and text round-trip byte-exactly after reopen", async (t) => {
  const f = await fixture(t);
  const allBytes = Uint8Array.from({ length: 256 }, (_, i) => i);
  const offsetBytes = new Uint8Array([99, 0, 128, 255, 99]).subarray(1, 4);
  await f.database.execute("CREATE TABLE synthetic (id INTEGER, bytes BLOB, label TEXT)");
  for (const [id, bytes] of [allBytes, offsetBytes, new Uint8Array(), null].entries()) {
    await f.database.execute("INSERT INTO synthetic VALUES (?, ?, ?)", [id, bytes, "synthetic \u0000 text"]);
  }
  const rows = await (await f.reopen()).query("SELECT * FROM synthetic ORDER BY id");
  for (const [id, bytes] of [allBytes, offsetBytes, new Uint8Array(), null].entries()) {
    assert.deepEqual(rows[id], { id, bytes, label: "synthetic \u0000 text" });
  }
});

test("safe integer limits match protocol numbers exactly and reject larger or fractional values", async (t) => {
  const { database } = await fixture(t);
  await database.execute("CREATE TABLE synthetic (value INTEGER)");
  for (const value of [Number.MAX_SAFE_INTEGER, Number.MIN_SAFE_INTEGER, 0]) {
    assert.deepEqual(await database.query("SELECT typeof(?) AS type", [value]), [{ type: "integer" }]);
    await database.execute("INSERT INTO synthetic VALUES (?)", [value]);
  }
  assert.deepEqual(await database.query("SELECT value FROM synthetic ORDER BY value"), [
    { value: Number.MIN_SAFE_INTEGER }, { value: 0 }, { value: Number.MAX_SAFE_INTEGER },
  ]);
  for (const invalid of [Number.MAX_SAFE_INTEGER + 1, Number.MIN_SAFE_INTEGER - 1, 1.5, NaN, Infinity]) {
    await assert.rejects(database.execute("INSERT INTO synthetic VALUES (?)", [invalid]), TypeError);
    await assert.rejects(database.query("SELECT ?", [invalid]), TypeError);
  }
  await assert.rejects(database.query("SELECT 9007199254740992 AS value"), TypeError);
  await assert.rejects(database.query("SELECT 1.5 AS value"), TypeError);
});

test("production migration creates only begin-initial-snapshot version one tables and indexes", async (t) => {
  const { snapshotAttemptsMigration } = await import("../src/snapshot/begin-initial-snapshot.ts");
  const { database } = await fixture(t);
  assert.deepEqual(senderMigrations, [snapshotAttemptsMigration]);
  assert.deepEqual(await migrate(database), { status: "ready", version: 1 });
  const schema = await database.query("SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY name");
  const normalize = (sql: string) => sql.replace(/\s+/g, " ").trim();
  assert.deepEqual(schema.map((row) => ({ ...row, sql: typeof row.sql === "string" ? normalize(row.sql) : row.sql })), [
    {
      type: "table", name: "current_snapshot_attempts", tbl_name: "current_snapshot_attempts",
      sql: normalize(`CREATE TABLE current_snapshot_attempts (
        record_type TEXT PRIMARY KEY NOT NULL, snapshot_id TEXT NOT NULL UNIQUE,
        FOREIGN KEY (snapshot_id, record_type) REFERENCES snapshot_attempts (snapshot_id, record_type)) STRICT`),
    },
    {
      type: "table", name: "snapshot_attempts", tbl_name: "snapshot_attempts",
      sql: normalize(`CREATE TABLE snapshot_attempts (
        snapshot_id TEXT PRIMARY KEY NOT NULL, record_type TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('initial', 'reconciliation')), installation_id TEXT NOT NULL,
        changes_token TEXT NOT NULL, unread_since_ms INTEGER NOT NULL, begun_at_ms INTEGER NOT NULL,
        abandoned_at_ms INTEGER, UNIQUE (snapshot_id, record_type)) STRICT`),
    },
    { type: "index", name: "sqlite_autoindex_current_snapshot_attempts_1", tbl_name: "current_snapshot_attempts", sql: null },
    { type: "index", name: "sqlite_autoindex_current_snapshot_attempts_2", tbl_name: "current_snapshot_attempts", sql: null },
    { type: "index", name: "sqlite_autoindex_snapshot_attempts_1", tbl_name: "snapshot_attempts", sql: null },
    { type: "index", name: "sqlite_autoindex_snapshot_attempts_2", tbl_name: "snapshot_attempts", sql: null },
  ]);
});

test("migrations run in order and advance user_version in the same transaction", async (t) => {
  const { database } = await fixture(t);
  const observations: number[] = [];
  const migrations = syntheticMigrations.map((migration) => ({ ...migration, async apply(tx: SenderDatabase) {
    observations.push((await tx.query("PRAGMA user_version"))[0].user_version as number);
    await migration.apply(tx);
  } }));
  assert.deepEqual(await migrate(database, migrations), { status: "ready", version: 2 });
  assert.deepEqual(observations, [0, 1]);
  assert.deepEqual(await database.query("PRAGMA user_version"), [{ user_version: 2 }]);
});

test("malformed migration lists throw before any SQL mutation", async (t) => {
  const { database } = await fixture(t);
  for (const versions of [[0], [2], [1, 3], [2, 1], [1, 1], [1.5]]) {
    await assert.rejects(migrate(database, versions.map((version) => ({ version, apply() { throw new Error("must not run"); } }))), TypeError);
  }
  await assert.rejects(migrate(database, new Array<Migration>(1)), TypeError);
  assert.deepEqual(await database.query("PRAGMA user_version"), [{ user_version: 0 }]);
  assert.deepEqual(await database.query("SELECT name FROM sqlite_schema"), []);
});

test("failed migration freezes at the prior version without losing existing bytes", async (t) => {
  const f = await fixture(t);
  const migrations: Migration[] = [syntheticMigrations[0], {
    version: 2, async apply(tx) {
      await tx.execute("UPDATE synthetic SET bytes=?", [new Uint8Array([88])]);
      await tx.execute("CREATE TABLE rolled_back (value TEXT)");
      // Even a premature version change must roll back with the migration.
      await tx.execute("PRAGMA user_version=2");
      throw new Error("synthetic payload must never enter the frozen result");
    },
  }];
  assert.deepEqual(await migrate(f.database, migrations), { status: "frozen", version: 1, reason: "migration-failed" });
  const database = await f.reopen();
  assert.deepEqual(await database.query("PRAGMA user_version"), [{ user_version: 1 }]);
  assert.deepEqual(await database.query("SELECT * FROM synthetic"), [{ id: 1, bytes: new Uint8Array([0, 255, 17]) }]);
  assert.deepEqual(await database.query("SELECT name FROM sqlite_schema WHERE name=?", ["rolled_back"]), []);
});

test("newer schema freezes without applying migrations or changing the database file", async (t) => {
  const f = await fixture(t);
  await migrate(f.database, syntheticMigrations);
  await f.database.execute("PRAGMA user_version=9");
  const database = await f.reopen();
  const before = readFileSync(f.path);
  let applied = false;
  assert.deepEqual(await migrate(database, [{ version: 1, apply() { applied = true; } }]), {
    status: "frozen", version: 9, reason: "newer-schema",
  });
  assert.equal(applied, false);
  assert.deepEqual(readFileSync(f.path), before);
  assert.deepEqual(await database.query("SELECT bytes FROM synthetic"), [{ bytes: new Uint8Array([0, 255, 17]) }]);
});

test("reopening an up-to-date schema does not rerun migrations", async (t) => {
  const f = await fixture(t);
  assert.deepEqual(await migrate(f.database, syntheticMigrations), { status: "ready", version: 2 });
  let applied = false;
  const result = await migrate(await f.reopen(), syntheticMigrations.map((migration) => ({
    version: migration.version, apply() { applied = true; throw new Error("must not rerun"); },
  })));
  assert.deepEqual(result, { status: "ready", version: 2 });
  assert.equal(applied, false);
});
