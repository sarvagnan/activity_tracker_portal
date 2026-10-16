// Unfrozen regressions for manage-sender-enrollment@1's shared-database races.
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import type { TestContext } from "node:test";
import { decodeMessage, encodeMessage, messageSha256 } from "@activity-tracker-portal/ingest-protocol";
import { createOutboxSender, deliveryMigration } from "../src/delivery/send-outbox-message.ts";
import { createSenderEnrollment, enrollmentMigration } from "../src/enrollment/manage-sender-enrollment.ts";
import type {
  ConfirmationResult, EnrollmentStatus, EnrollmentTransport, RefreshResult, SenderEnrollment,
} from "../src/enrollment/manage-sender-enrollment.ts";
import type { InstallationCipher } from "../src/installation-cipher.ts";
import { outboxAad, outboxMessages, outboxMigration } from "../src/outbox/stage-outbox-batch.ts";
import { checkpointMigration } from "../src/outbox/stage-sync-checkpoint.ts";
import { completionMigration } from "../src/outbox/stage-sync-completion.ts";
import { schedulingMigration } from "../src/scheduling/schedule-background-polling.ts";
import { snapshotAttemptsMigration } from "../src/snapshot/begin-initial-snapshot.ts";
import type { SenderDatabase } from "../src/storage/database.ts";
import { migrate } from "../src/storage/migrations.ts";
import { syncMigration } from "../src/sync/coordinate-sender-sync.ts";
import { openNodeDatabase } from "./support/node-database.ts";
import { NodeInstallationCipher } from "./support/node-installation-cipher.ts";

const IDENTITY = "00000000-0000-4000-8000-0000000000e1";
const REPLACEMENT = "00000000-0000-4000-8000-0000000000e2";
const NOW = Date.UTC(2026, 9, 16, 9);
const MIGRATIONS = [
  snapshotAttemptsMigration, outboxMigration, completionMigration, checkpointMigration, syncMigration, schedulingMigration,
  deliveryMigration, enrollmentMigration,
];
const OFFER = { kind: "scanned", bytes: encodeMessage({
  wire_version: 1, kind: "enrollment-offer", enrollment_code: "SYNTHETICCODE234",
  portal_url: "http://portal.synthetic.invalid:8443", expires_at: "2026-10-16T09:15:00.000Z",
}) } as const;

function advertisement(generation: number): Uint8Array {
  return encodeMessage({
    wire_version: 1, kind: "capability-advertisement", generation, wire_versions: [1],
    projection_schemas: [{ record_type: "steps", versions: [1] }], max_record_count: 4, max_message_bytes: 65_536,
  });
}
const RETRYABLE = encodeMessage({ wire_version: 1, kind: "retryable-failure", reason: "temporarily-unavailable" });
const REFUSAL = encodeMessage({ wire_version: 1, kind: "enrollment-offer", enrollment_code: "ZZZZZZZZ22222222",
  portal_url: "http://portal.synthetic.invalid:8443", expires_at: "2026-10-16T09:15:00.000Z" });

/** Every wait, including a held seam, has a deadline; no polling loops are needed. */
async function bounded<T>(promise: Promise<T>, label: string, ms = 2_000): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([promise, new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} exceeded ${ms}ms`)), ms);
    })]);
  } finally { clearTimeout(timer); }
}

function gate() {
  let enter!: () => void;
  let release!: () => void;
  const entered = new Promise<void>((resolve) => { enter = resolve; });
  const released = new Promise<void>((resolve) => { release = resolve; });
  return { entered, release, async hold() { enter(); await bounded(released, "seam release"); } };
}

class Portal implements EnrollmentTransport {
  answer = RETRYABLE;
  pending: ReturnType<typeof gate> | null = null;
  async send() {
    const answer = this.answer;
    const pending = this.pending;
    this.pending = null;
    if (pending) await pending.hold();
    return { outcome: "response", body: answer } as const;
  }
  delay(answer: Uint8Array) {
    this.answer = answer;
    this.pending = gate();
    return this.pending;
  }
}

/** Also holds a successful seal so tests can distinguish a pre-seal check from the transactional check. */
class GatedCipher extends NodeInstallationCipher {
  pending: ReturnType<typeof gate> | null = null;
  override async seal(...args: Parameters<InstallationCipher["seal"]>) {
    const sealed = await super.seal(...args);
    const pending = this.pending;
    this.pending = null;
    if (pending) await pending.hold();
    return sealed;
  }
}

async function fixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), "sender-interleavings-synthetic-"));
  const database = await openNodeDatabase(join(directory, "synthetic.sqlite"));
  t.after(async () => { await database.close(); rmSync(directory, { recursive: true, force: true }); });
  assert.deepEqual(await migrate(database, MIGRATIONS), { status: "ready", version: 8 });
  const cipher = new GatedCipher({ deviceSecure: true });
  const portalA = new Portal();
  const portalB = new Portal();
  const make = (transport: EnrollmentTransport, identity: string) => createSenderEnrollment({
    database, cipher, transport, now: () => NOW, randomUUID: () => identity,
  });
  const a = make(portalA, IDENTITY);
  const b = make(portalB, REPLACEMENT);
  return { database, cipher, portalA, portalB, a, b };
}
type Fixture = Awaited<ReturnType<typeof fixture>>;

/** Schema version, every sqlite_master object, and every row (including ciphertext) in every table. */
async function contents(database: SenderDatabase) {
  const schema = await database.query("SELECT * FROM sqlite_master ORDER BY type, name");
  const tables: Record<string, unknown> = {};
  for (const { name } of schema.filter((entry) => entry.type === "table")) {
    const quoted = (name as string).replaceAll('"', '""');
    tables[name as string] = await database.query(`SELECT * FROM "${quoted}" ORDER BY rowid`);
  }
  return { version: await database.query("PRAGMA user_version"), schema, tables };
}

async function status(sender: SenderEnrollment): Promise<EnrollmentStatus> {
  const current = await sender.status();
  assert.ok(current !== null && !("outcome" in current), JSON.stringify(current));
  return current;
}

async function confirming(f: Fixture) {
  assert.deepEqual(await f.a.enroll(OFFER), { outcome: "retryable", failure: "temporarily-unavailable" });
  assert.equal((await status(f.b)).phase, "confirming");
}

async function enrolled(f: Fixture) {
  f.portalA.answer = advertisement(1);
  assert.deepEqual(await f.a.enroll(OFFER), { outcome: "enrolled", generation: 1 });
}

/** Learn confirmation through the published sender's real receipt handling. */
async function deliverAcknowledgement(f: Fixture) {
  const context = (await status(f.b)).deliveryContext;
  const sender = createOutboxSender({
    database: f.database, cipher: f.cipher, context, now: () => NOW, notifier: { notify: async () => {} },
    transport: { send: async (request) => {
      const message = decodeMessage(request.body);
      return { outcome: "response", body: encodeMessage({
        wire_version: 1, kind: "delivery-receipt", sender_installation: message.sender_installation,
        sequence: message.sequence, message_sha256: messageSha256(request.body),
      }) };
    } },
  });
  assert.equal((await sender.sendNext()).outcome, "delivered");
  assert.equal((await status(f.b)).confirmed, true);
}

async function acknowledgements(f: Fixture) {
  const messages = await outboxMessages(f.database);
  const decoded = [];
  for (const message of messages) {
    assert.equal(message.kind, "capability-acknowledgement");
    const opened = await f.cipher.open(`activity-tracker-portal.installation.${message.installationId}`,
      message.sealed, outboxAad(message.installationId, message.sequence));
    assert.equal(opened.status, "ok");
    if (opened.status === "ok") decoded.push(decodeMessage(opened.value));
  }
  return decoded;
}

test("a delayed refresh cannot reverse another instance's completed drain", { timeout: 5_000 }, async (t) => {
  const f = await fixture(t);
  await enrolled(f);
  await deliverAcknowledgement(f);
  const delayed = f.portalA.delay(advertisement(2));
  const pending = f.a.refreshCapabilities();
  try {
    await bounded(delayed.entered, "refresh request");
    assert.deepEqual(await f.b.beginDrain(), { outcome: "draining" });
    const drained = await status(f.b);
    assert.equal(drained.phase, "draining");
    assert.equal(drained.finalSequence, 1);
    const before = await contents(f.database);
    delayed.release();
    assert.deepEqual(await bounded(pending, "refresh result"), { outcome: "draining" });
    assert.deepEqual(await outboxMessages(f.database), []);
    assert.equal((await status(f.b)).finalSequence, 1);
    assert.deepEqual(await contents(f.database), before);
  } finally { delayed.release(); }
});

for (const [name, answer] of [["advertisement", advertisement(1)], ["refusal", REFUSAL]] as const) {
  test(`a delayed confirmation ${name} cannot change a replacement confirming installation`, { timeout: 5_000 }, async (t) => {
    const f = await fixture(t);
    await confirming(f);
    const delayed = f.portalA.delay(answer);
    const pending = f.a.confirm();
    try {
      await bounded(delayed.entered, "confirmation request");
      assert.deepEqual(await f.b.reset({ acknowledgePossibleLoss: true }), { outcome: "destroyed" });
      assert.deepEqual(await f.b.enroll(OFFER), { outcome: "retryable", failure: "temporarily-unavailable" });
      const replacement = await status(f.b);
      assert.equal(replacement.installationId, REPLACEMENT);
      assert.equal(replacement.phase, "confirming");
      const before = await contents(f.database);
      delayed.release();
      assert.deepEqual(await bounded(pending, "confirmation result"), { outcome: "refused", failure: "not-confirming" });
      assert.deepEqual(await status(f.b), replacement);
      assert.deepEqual(await outboxMessages(f.database), []);
      assert.deepEqual(await contents(f.database), before);
    } finally { delayed.release(); }
  });
}

for (const generation of [2, 3]) {
  test(`a delayed generation 2 refresh is current after another instance adopts generation ${generation}`, { timeout: 5_000 }, async (t) => {
    const f = await fixture(t);
    await enrolled(f);
    const delayed = f.portalA.delay(advertisement(2));
    const pending = f.a.refreshCapabilities();
    try {
      await bounded(delayed.entered, "refresh request");
      f.portalB.answer = advertisement(generation);
      assert.deepEqual(await f.b.refreshCapabilities(), { outcome: "adopted", generation });
      const before = await contents(f.database);
      delayed.release();
      assert.deepEqual(await bounded(pending, "refresh result"), { outcome: "current" });
      const acks = await acknowledgements(f);
      assert.deepEqual(acks.map((ack) => ack.capability_generation), [1, generation]);
      assert.deepEqual(await contents(f.database), before);
    } finally { delayed.release(); }
  });
}

test("a delayed confirmation cannot append a second acknowledgement after another instance confirms", { timeout: 5_000 }, async (t) => {
  const f = await fixture(t);
  await confirming(f);
  const delayed = f.portalA.delay(advertisement(1));
  const pending = f.a.confirm();
  try {
    await bounded(delayed.entered, "confirmation request");
    f.portalB.answer = advertisement(1);
    assert.deepEqual(await f.b.confirm(), { outcome: "enrolled", generation: 1 });
    const before = await contents(f.database);
    delayed.release();
    assert.deepEqual(await bounded(pending, "confirmation result"), { outcome: "refused", failure: "not-confirming" });
    assert.deepEqual((await acknowledgements(f)).map((ack) => ack.capability_generation), [1]);
    assert.deepEqual(await contents(f.database), before);
  } finally { delayed.release(); }
});

test("status reads committed confirming state while its own confirmation is pending", { timeout: 5_000 }, async (t) => {
  const f = await fixture(t);
  await confirming(f);
  const before = await contents(f.database);
  const delayed = f.portalA.delay(advertisement(1));
  const pending = f.a.confirm();
  try {
    await bounded(delayed.entered, "confirmation request");
    const current = await bounded(f.a.status(), "independent status", 500);
    assert.deepEqual(current, await status(f.b));
    assert.equal((current as EnrollmentStatus).phase, "confirming");
    assert.deepEqual(await contents(f.database), before);
  } finally { delayed.release(); await bounded(pending, "confirmation cleanup"); }
});

// These gates stop after successful encryption: only the in-transaction reread can protect the write.
for (const action of ["drain", "refresh", "confirm", "replace"]) {
  test(`adoption revalidates inside the transaction after a concurrent ${action} during sealing`, { timeout: 5_000 }, async (t) => {
    const f = await fixture(t);
    const confirmation = action === "confirm" || action === "replace";
    if (confirmation) await confirming(f);
    else { await enrolled(f); await deliverAcknowledgement(f); }
    f.portalA.answer = advertisement(confirmation ? 1 : 2);
    const delayed = gate();
    f.cipher.pending = delayed;
    const pending: Promise<ConfirmationResult | RefreshResult> = confirmation ? f.a.confirm() : f.a.refreshCapabilities();
    try {
      await bounded(delayed.entered, "acknowledgement seal");
      let expected: object;
      if (action === "drain") {
        assert.deepEqual(await f.b.beginDrain(), { outcome: "draining" });
        expected = { outcome: "draining" };
      } else if (action === "replace") {
        assert.deepEqual(await f.b.reset({ acknowledgePossibleLoss: true }), { outcome: "destroyed" });
        assert.deepEqual(await f.b.enroll(OFFER), { outcome: "retryable", failure: "temporarily-unavailable" });
        expected = { outcome: "refused", failure: "not-confirming" };
      } else {
        f.portalB.answer = advertisement(confirmation ? 1 : 2);
        assert.deepEqual(await (confirmation ? f.b.confirm() : f.b.refreshCapabilities()),
          { outcome: confirmation ? "enrolled" : "adopted", generation: confirmation ? 1 : 2 });
        expected = confirmation ? { outcome: "refused", failure: "not-confirming" } : { outcome: "current" };
      }
      const before = await contents(f.database);
      delayed.release();
      assert.deepEqual(await bounded(pending, "adoption result"), expected);
      assert.deepEqual(await contents(f.database), before);
    } finally { delayed.release(); }
  });
}
