// Acceptance tests for manage-sender-enrollment@2: status, draining and unlinking without loading sealed messages.
// Requirement numbers refer to packs/manage-sender-enrollment-2.spec.md. manage-sender-enrollment@1's suite runs
// unchanged beside this one and covers R1-R14. Storage is the 3ug node:sqlite SenderDatabase substitute and the
// cipher is the 3ug NodeInstallationCipher. The Portal is a synthetic fake whose answers are built with the protocol
// library; Outbox rows are appended through stage-outbox-batch's primitives and delivered through
// send-outbox-message@1. @1's outboxMessages is the oracle for every derived answer.
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { TestContext } from "node:test";
import { decodeMessage, encodeMessage, messageSha256 } from "@activity-tracker-portal/ingest-protocol";
import { createOutboxSender, deliveryMigration } from "../src/delivery/send-outbox-message.ts";
import type { SendResult } from "../src/delivery/send-outbox-message.ts";
import { createSenderEnrollment, enrollmentMigration } from "../src/enrollment/manage-sender-enrollment.ts";
import type {
  EnrollmentOffer, EnrollmentStatus, EnrollmentTransport, SenderEnrollment, SenderEnrollmentOptions,
} from "../src/enrollment/manage-sender-enrollment.ts";
import { appendOutboxMessages, lastOutboxSequence, outboxAad, outboxMessages, outboxMigration } from "../src/outbox/stage-outbox-batch.ts";
import type { OutboxKind } from "../src/outbox/stage-outbox-batch.ts";
import { checkpointMigration } from "../src/outbox/stage-sync-checkpoint.ts";
import { completionMigration } from "../src/outbox/stage-sync-completion.ts";
import { schedulingMigration } from "../src/scheduling/schedule-background-polling.ts";
import { snapshotAttemptsMigration } from "../src/snapshot/begin-initial-snapshot.ts";
import type { DatabaseRow, DatabaseValue, SenderDatabase } from "../src/storage/database.ts";
import { migrate } from "../src/storage/migrations.ts";
import { syncMigration } from "../src/sync/coordinate-sender-sync.ts";
import { openNodeDatabase } from "./support/node-database.ts";
import { NodeInstallationCipher } from "./support/node-installation-cipher.ts";

const MIGRATIONS = [
  snapshotAttemptsMigration, outboxMigration, completionMigration, checkpointMigration, syncMigration, schedulingMigration,
  deliveryMigration, enrollmentMigration,
];
const IDENTITY = "00000000-0000-4000-8000-0000000000e1";
const OTHER_INSTALLATION = "00000000-0000-4000-8000-0000000000f1";
const ALIAS = `activity-tracker-portal.installation.${IDENTITY}`;
const CODE = "SYNTHETICCODE234";
const PORTAL_URL = "http://portal.synthetic.invalid:8443";
const NOW = Date.UTC(2026, 9, 30, 9, 0, 0, 0);
const utf8 = (text: string) => new TextEncoder().encode(text);

type Phase = "confirming" | "refused" | "enrolled" | "confirmed" | "draining";
type Fixture = Awaited<ReturnType<typeof fixture>>;

const OFFER: EnrollmentOffer = {
  kind: "scanned",
  bytes: encodeMessage({ wire_version: 1, kind: "enrollment-offer", enrollment_code: CODE, portal_url: PORTAL_URL, expires_at: "2026-10-30T09:15:00.000Z" }),
};
const ADVERTISEMENT = encodeMessage({
  wire_version: 1, kind: "capability-advertisement", generation: 1, wire_versions: [1],
  projection_schemas: [{ record_type: "steps", versions: [1] }], max_record_count: 4, max_message_bytes: 65_536,
});
const RETRYABLE = encodeMessage({ wire_version: 1, kind: "retryable-failure", reason: "temporarily-unavailable" });

function receipt(bytes: Uint8Array): Uint8Array {
  const decoded = decodeMessage(bytes);
  return encodeMessage({
    wire_version: 1, kind: "delivery-receipt", sender_installation: decoded.sender_installation as string,
    sequence: decoded.sequence as number, message_sha256: messageSha256(bytes),
  });
}

async function fixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), "sender-synthetic-"));
  const database = await openNodeDatabase(join(directory, "synthetic.sqlite"));
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 8 });
  const cipher = new NodeInstallationCipher({ deviceSecure: true });
  const portal = { answer: ADVERTISEMENT };
  const transport: EnrollmentTransport = { send: async () => ({ outcome: "response", body: portal.answer }) };
  const make = (overrides: Partial<SenderEnrollmentOptions> = {}): SenderEnrollment => createSenderEnrollment({
    database, cipher, transport, now: () => NOW, randomUUID: () => IDENTITY, ...overrides,
  });
  return { database, cipher, portal, make, sender: make() };
}

/** One send-outbox-message@1 delivery of the installation's head, answered with its receipt. */
async function deliverNext(f: Fixture): Promise<SendResult> {
  const status = await f.sender.status();
  assert.ok(status !== null && !("outcome" in status), JSON.stringify(status));
  return createOutboxSender({
    database: f.database, cipher: f.cipher, now: () => NOW, notifier: { notify: async () => {} },
    transport: { send: async (request) => ({ outcome: "response", body: receipt(request.body) }) },
    context: (status as EnrollmentStatus).deliveryContext,
  }).sendNext();
}

/** Bring a fresh fixture's installation to `phase`; `confirmed` is enrolled with its acknowledgement delivered. */
async function toPhase(f: Fixture, phase: Phase): Promise<void> {
  if (phase === "confirming") {
    f.portal.answer = RETRYABLE;
    assert.equal((await f.sender.enroll(OFFER)).outcome, "retryable");
  } else if (phase === "refused") {
    f.portal.answer = new Uint8Array();
    assert.equal((await f.sender.enroll(OFFER)).outcome, "portal-refused");
  } else {
    assert.deepEqual(await f.sender.enroll(OFFER), { outcome: "enrolled", generation: 1 });
    if (phase !== "enrolled") {
      assert.deepEqual(await deliverNext(f), { outcome: "delivered", sequence: 1, kind: "capability-acknowledgement" });
      if (phase === "draining") assert.deepEqual(await f.sender.beginDrain(), { outcome: "draining" });
    }
  }
  f.portal.answer = ADVERTISEMENT;
}

/** Append synthetic rows in batches; their sealed bytes are never opened, so they need not be real ciphertext. */
async function queue(database: SenderDatabase, installationId: string, count: number, kind: OutboxKind = "ingest-batch"): Promise<void> {
  for (let done = 0; done < count; done += 500) {
    const last = await lastOutboxSequence(database, installationId);
    const batch = Array.from({ length: Math.min(500, count - done) }, (_, index) => ({
      kind, sealed: utf8(`synthetic-sealed-${installationId.slice(-2)}-${last + index + 1}`), messageBytes: 64 + index % 50,
    }));
    assert.equal(await database.transaction((tx) => appendOutboxMessages(tx, installationId, last, batch)), true);
  }
}

/** A real sealed message, so send-outbox-message@1 can deliver it. */
async function queueSealed(f: Fixture, plaintext: Uint8Array): Promise<number> {
  const last = await lastOutboxSequence(f.database, IDENTITY);
  const sealed = await f.cipher.seal(ALIAS, plaintext, outboxAad(IDENTITY, last + 1));
  assert.equal(sealed.status, "ok");
  assert.equal(await f.database.transaction((tx) => appendOutboxMessages(tx, IDENTITY, last, [{
    kind: "sync-checkpoint", sealed: (sealed as { value: Uint8Array }).value, messageBytes: plaintext.length,
  }])), true);
  return last + 1;
}

function checkpointBytes(sequence: number): Uint8Array {
  return encodeMessage({
    wire_version: 1, kind: "sync-checkpoint", sender_installation: IDENTITY, sequence, capability_generation: 1,
    run_sequence: sequence, trigger: "background", started_at: "2026-10-30T08:59:00.000Z", ended_at: "2026-10-30T08:59:30.000Z",
    background_scheduling: "available", record_types: [], outbox_messages_before_checkpoint: 0, outbox_bytes_before_checkpoint: 0,
  });
}

async function remove(database: SenderDatabase, installationId: string, sequence: number): Promise<void> {
  await database.execute("DELETE FROM outbox_messages WHERE installation_id = ? AND sequence = ?", [installationId, sequence]);
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

const sealedRows = (rows: readonly DatabaseRow[]) => rows.filter((row) => "sealed" in row).length;
const byteArrays = (rows: readonly DatabaseRow[]) => rows.flatMap((row) => Object.values(row)).filter((value) => value instanceof Uint8Array).length;

/** An enrollment instance whose every read is recorded. */
function watched(f: Fixture) {
  const observed = observing(f.database);
  return { sender: f.make({ database: observed.database }), rows: observed.rows };
}

/** What @1 derives from the Outbox: confirmation once the acknowledgement's row is gone, and the drained final sequence. */
async function oracle(f: Fixture): Promise<{ confirmed: boolean; finalSequence: number | null }> {
  const [row] = await f.database.query("SELECT phase, advertisement, acknowledgement_sequence FROM sender_enrollment");
  if (!row || row.advertisement === null) return { confirmed: false, finalSequence: null };
  const rows = (await outboxMessages(f.database)).filter((entry) => entry.installationId === IDENTITY);
  const last = await lastOutboxSequence(f.database, IDENTITY);
  const acknowledgement = row.acknowledgement_sequence as number;
  return {
    confirmed: last >= acknowledgement && !rows.some((entry) => entry.sequence === acknowledgement),
    finalSequence: row.phase === "draining" && rows.length === 0 ? last : null,
  };
}

async function statusOf(sender: SenderEnrollment): Promise<EnrollmentStatus> {
  const status = await sender.status();
  assert.ok(status !== null && !("outcome" in status), JSON.stringify(status));
  return status as EnrollmentStatus;
}

// R16 — No sealed message is loaded

test("status_loads_no_sealed_message", async (t) => {
  for (const phase of ["confirming", "refused", "enrolled", "confirmed", "draining"] as const) {
    const f = await fixture(t);
    await toPhase(f, phase);
    if (phase === "enrolled" || phase === "confirmed") await queue(f.database, IDENTITY, 20);
    await queue(f.database, OTHER_INSTALLATION, 5);
    const view = watched(f);
    await view.sender.status();
    assert.ok(view.rows.length > 0, `${phase}: status reads the database`);
    assert.equal(sealedRows(view.rows), 0, phase);
  }
  // Draining with messages still queued, before the final sequence is known.
  const f = await fixture(t);
  await toPhase(f, "confirmed");
  await queue(f.database, IDENTITY, 20);
  assert.deepEqual(await f.sender.beginDrain(), { outcome: "draining" });
  const view = watched(f);
  assert.equal((await statusOf(view.sender)).finalSequence, null);
  assert.equal(sealedRows(view.rows), 0, "draining with queued messages");
});

test("begin_drain_loads_no_sealed_message", async (t) => {
  for (const queued of [0, 50]) {
    const f = await fixture(t);
    await toPhase(f, "confirmed");
    await queue(f.database, IDENTITY, queued);
    await queue(f.database, OTHER_INSTALLATION, 5);
    const view = watched(f);
    assert.deepEqual(await view.sender.beginDrain(), { outcome: "draining" });
    assert.equal(sealedRows(view.rows), 0, `${queued} queued`);
  }
  // A refused drain, before confirmation is learned, loads none either.
  const f = await fixture(t);
  await toPhase(f, "enrolled");
  await queue(f.database, IDENTITY, 50);
  const view = watched(f);
  assert.deepEqual(await view.sender.beginDrain(), { outcome: "refused", failure: "not-confirmed" });
  assert.equal(sealedRows(view.rows), 0, "refused drain");
});

test("unlink_loads_no_sealed_message", async (t) => {
  const f = await fixture(t);
  await toPhase(f, "confirmed");
  await queue(f.database, IDENTITY, 50);
  await queue(f.database, OTHER_INSTALLATION, 5);
  assert.deepEqual(await f.sender.beginDrain(), { outcome: "draining" });
  let view = watched(f);
  assert.deepEqual(await view.sender.unlink(), { outcome: "refused", failure: "not-drained" });
  assert.equal(sealedRows(view.rows), 0, "refused unlink");
  await f.database.execute("DELETE FROM outbox_messages WHERE installation_id = ?", [IDENTITY]);
  view = watched(f);
  assert.deepEqual(await view.sender.unlink(), { outcome: "destroyed" });
  assert.equal(sealedRows(view.rows), 0, "unlink");
});

test("reads_do_not_grow_with_the_outbox", async (t) => {
  const received: Record<string, number>[] = [];
  for (const queued of [0, 500]) {
    const f = await fixture(t);
    await toPhase(f, "confirmed");
    await queue(f.database, IDENTITY, queued);
    const status = watched(f);
    await status.sender.status();
    const drain = watched(f);
    assert.deepEqual(await drain.sender.beginDrain(), { outcome: "draining" });
    const unlink = watched(f);
    assert.equal((await unlink.sender.unlink()).outcome, queued === 0 ? "destroyed" : "refused");
    received.push({
      status: byteArrays(status.rows), drain: byteArrays(drain.rows), unlink: byteArrays(unlink.rows),
      sealed: sealedRows([...status.rows, ...drain.rows, ...unlink.rows]),
    });
  }
  assert.equal(received[1].sealed, 0);
  assert.deepEqual({ status: received[1].status, drain: received[1].drain }, { status: received[0].status, drain: received[0].drain });
  // Unlink destroys an empty installation but refuses one with 500 queued, so it may read fewer rows there, never more.
  assert.ok(received[1].unlink <= received[0].unlink, JSON.stringify(received));
});

// R17 — The same answers

test("status_matches_the_outbox_oracle", async (t) => {
  const f = await fixture(t);
  const check = async (label: string) => {
    const status = await statusOf(f.sender);
    const expected = await oracle(f);
    assert.deepEqual({ confirmed: status.confirmed, finalSequence: status.finalSequence }, expected, label);
  };
  await toPhase(f, "enrolled");
  await check("enrolled, acknowledgement queued");
  await queueSealed(f, checkpointBytes(2));
  await queue(f.database, OTHER_INSTALLATION, 3);
  await check("a later message and another installation's rows");
  assert.equal((await deliverNext(f)).outcome, "delivered");
  await check("the acknowledgement's receipt");
  await queue(f.database, IDENTITY, 4);
  await remove(f.database, IDENTITY, 4);
  await check("a removal in the middle");
  await remove(f.database, IDENTITY, 2);
  await check("a removal at the head");
  assert.deepEqual(await f.sender.beginDrain(), { outcome: "draining" });
  await check("draining with queued messages");
  for (const sequence of [3, 5, 6]) {
    await remove(f.database, IDENTITY, sequence);
    await check(`draining after removing ${sequence}`);
  }
  assert.equal((await statusOf(f.sender)).finalSequence, 6);
  await remove(f.database, OTHER_INSTALLATION, 1);
  await check("another installation's removal");
});

test("drain_and_unlink_match_the_outbox_oracle", async (t) => {
  const f = await fixture(t);
  await toPhase(f, "enrolled");
  await queue(f.database, OTHER_INSTALLATION, 3);
  assert.equal((await oracle(f)).confirmed, false);
  assert.deepEqual(await f.sender.beginDrain(), { outcome: "refused", failure: "not-confirmed" });
  assert.equal((await deliverNext(f)).outcome, "delivered");
  assert.equal((await oracle(f)).confirmed, true);
  await queue(f.database, IDENTITY, 3);
  assert.deepEqual(await f.sender.beginDrain(), { outcome: "draining" });
  assert.equal((await oracle(f)).finalSequence, null);
  assert.deepEqual(await f.sender.unlink(), { outcome: "refused", failure: "not-drained" });
  for (const sequence of [2, 3]) await remove(f.database, IDENTITY, sequence);
  assert.equal((await oracle(f)).finalSequence, null, "one message still queued");
  assert.deepEqual(await f.sender.unlink(), { outcome: "refused", failure: "not-drained" });
  await remove(f.database, IDENTITY, 4);
  assert.equal((await oracle(f)).finalSequence, 4);
  assert.deepEqual(await f.sender.unlink(), { outcome: "destroyed" });
  assert.equal(await f.sender.status(), null);
});

// R18 — A large Outbox

test("a_large_outbox_drains_and_unlinks", async (t) => {
  const f = await fixture(t);
  await toPhase(f, "confirmed");
  await queue(f.database, IDENTITY, 5000);
  let status = await statusOf(f.sender);
  assert.deepEqual({ confirmed: status.confirmed, finalSequence: status.finalSequence, phase: status.phase }, { confirmed: true, finalSequence: null, phase: "enrolled" });
  assert.deepEqual(await f.sender.beginDrain(), { outcome: "draining" });
  status = await statusOf(f.sender);
  assert.deepEqual({ finalSequence: status.finalSequence, phase: status.phase }, { finalSequence: null, phase: "draining" });
  assert.deepEqual(await f.sender.unlink(), { outcome: "refused", failure: "not-drained" });
  await f.database.execute("DELETE FROM outbox_messages WHERE installation_id = ?", [IDENTITY]);
  assert.equal((await statusOf(f.sender)).finalSequence, 5001);
  assert.deepEqual(await f.sender.unlink(), { outcome: "destroyed" });
});
