// Unbound regression coverage for Source Identity Unicode validation (R12).
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { TestContext } from "node:test";
import { decodeMessage } from "@activity-tracker-portal/ingest-protocol";
import type { InstallationCipher } from "../src/installation-cipher.ts";
import { outboxAad, outboxMessages, stageOutboxBatch } from "../src/outbox/stage-outbox-batch.ts";
import type { ChangePage, StagingContext } from "../src/outbox/stage-outbox-batch.ts";
import { beginInitialSnapshot } from "../src/snapshot/begin-initial-snapshot.ts";
import type { SenderDatabase } from "../src/storage/database.ts";
import { migrate } from "../src/storage/migrations.ts";
import { openNodeDatabase } from "./support/node-database.ts";
import { NodeInstallationCipher } from "./support/node-installation-cipher.ts";

const INSTALLATION = "00000000-0000-4000-8000-000000000001";
const SNAPSHOT = "00000000-0000-4000-8000-000000000002";
const CHANGE = "00000000-0000-4000-8000-000000000003";
const CONTEXT: StagingContext = {
  installationId: INSTALLATION, keyAlias: "synthetic-unicode-key",
  generation: { generation: 1, maxRecordCount: 100, maxMessageBytes: 4096, projectionSchemas: { steps: [1] } },
};

async function fixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const path = join(directory, "synthetic.sqlite");
  const database = await openNodeDatabase(path);
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.deepEqual(await migrate(database), { status: "ready", version: 2 });
  const cipher = new NodeInstallationCipher();
  assert.equal((await cipher.create(CONTEXT.keyAlias)).status, "ok");
  const begun = await beginInitialSnapshot(
    database,
    { async issue() { return { outcome: "issued" as const, changesToken: "synthetic-before", unreadSinceMs: 0 }; } },
    { recordType: "steps", installationId: INSTALLATION },
    { randomUUID: () => SNAPSHOT, now: () => 1 },
  );
  assert.equal(begun.outcome, "begun");
  assert.equal((await stageOutboxBatch(database, cipher, CONTEXT, {
    source: "snapshot", recordType: "steps", cycleId: SNAPSHOT, projectionSchemaVersion: 1,
    pageIndex: 1, readFrom: null, projections: [], nextPosition: new Uint8Array(), exhausted: true,
  })).outcome, "staged");
  return { database, cipher, path };
}

function changePage(sourceIdentity: string): ChangePage {
  return {
    source: "change", recordType: "steps", cycleId: CHANGE, projectionSchemaVersion: 1,
    pageIndex: 1, readFrom: "synthetic-before", items: [{ kind: "retraction", sourceIdentity }],
    nextChangesToken: "synthetic-after", nextUnreadSinceMs: 1, hasMore: false,
  };
}

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

/** Count every storage and cipher method, delegating to the real synthetic boundaries. */
function recording(database: SenderDatabase, cipher: InstallationCipher) {
  const storageCalls: string[] = [];
  const cipherCalls: string[] = [];
  const storage: SenderDatabase = {
    execute: (sql, parameters) => { storageCalls.push("execute"); return database.execute(sql, parameters); },
    query: (sql, parameters) => { storageCalls.push("query"); return database.query(sql, parameters); },
    transaction: (fn) => { storageCalls.push("transaction"); return database.transaction(fn); },
    close: () => { storageCalls.push("close"); return database.close(); },
  };
  const key: InstallationCipher = {
    create: (alias) => { cipherCalls.push("create"); return cipher.create(alias); },
    keyStatus: (alias) => { cipherCalls.push("keyStatus"); return cipher.keyStatus(alias); },
    seal: (alias, plaintext, aad) => { cipherCalls.push("seal"); return cipher.seal(alias, plaintext, aad); },
    open: (alias, sealed, aad) => { cipherCalls.push("open"); return cipher.open(alias, sealed, aad); },
    delete: (alias) => { cipherCalls.push("delete"); return cipher.delete(alias); },
    isDeviceSecure: () => { cipherCalls.push("isDeviceSecure"); return cipher.isDeviceSecure(); },
  };
  return { database: storage, cipher: key, storageCalls, cipherCalls };
}

for (const [name, identity] of [
  ["lone_high_surrogate", "\ud800"],
  ["lone_low_surrogate", "\udc00"],
  ["reversed_surrogate_pair", "\udc00\ud800"],
  ["lone_surrogate_after_valid_text", "synthetic-\ud83d\ude00\ud800"],
] as const) {
  test(`source_identity_rejects_${name}_before_effects`, async (t) => {
    const f = await fixture(t);
    const before = await contents(f.database);
    const bytesBefore = readFileSync(f.path);
    const spy = recording(f.database, f.cipher);
    await assert.rejects(stageOutboxBatch(spy.database, spy.cipher, CONTEXT, changePage(identity)), TypeError);
    assert.deepEqual(spy.storageCalls, []);
    assert.deepEqual(spy.cipherCalls, []);
    assert.deepEqual(await contents(f.database), before);
    assert.deepEqual(readFileSync(f.path), bytesBefore);
  });
}

test("source_identity_with_valid_surrogate_pair_stages", async (t) => {
  const f = await fixture(t);
  const identity = "synthetic-\ud83d\ude00";
  const result = await stageOutboxBatch(f.database, f.cipher, CONTEXT, changePage(identity));
  assert.equal(result.outcome, "staged");
  const rows = await outboxMessages(f.database);
  assert.deepEqual(rows.map((row) => row.sequence), [1, 2]);
  const row = rows[1];
  const opened = await f.cipher.open(CONTEXT.keyAlias, row.sealed, outboxAad(row.installationId, row.sequence));
  assert.equal(opened.status, "ok");
  if (opened.status !== "ok") throw new Error("Synthetic ciphertext did not open");
  assert.deepEqual(decodeMessage(opened.value).items, [{ kind: "retraction", source_identity: identity }]);
});
