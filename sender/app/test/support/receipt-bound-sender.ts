// The Sender side of receipt-bound-delivery@1's end-to-end evidence, driven one process per step by
// tests/acceptance/test_receipt_bound_delivery_1.py, so every step is also a Sender restart. It composes the
// published Sender units over the 3ug node:sqlite substitute and a synthetic cipher with a fixed key, so a new
// process reopens the Outbox, and reaches the Portal only through this unit's HTTP transport over Node's
// fetch. Each command prints one JSON line: its result, and the installation's Outbox afterwards.
//
//   node receipt-bound-sender.ts <state-directory> <command> [json-arguments]
import { createHash, randomUUID } from "node:crypto";
import { join } from "node:path";
import { createOutboxSender, deliveryMigration } from "../../src/delivery/send-outbox-message.ts";
import { createSenderEnrollment, enrollmentMigration } from "../../src/enrollment/manage-sender-enrollment.ts";
import type { EnrollmentStatus } from "../../src/enrollment/manage-sender-enrollment.ts";
import {
  changeOrigin, cycleProgress, lastOutboxSequence, outboxAad, outboxMessages, outboxMigration, stageOutboxBatch,
} from "../../src/outbox/stage-outbox-batch.ts";
import type { StagingContext } from "../../src/outbox/stage-outbox-batch.ts";
import { checkpointMigration, stageSyncCheckpoint } from "../../src/outbox/stage-sync-checkpoint.ts";
import { completionMigration, stageSyncCompletion } from "../../src/outbox/stage-sync-completion.ts";
import { schedulingMigration } from "../../src/scheduling/schedule-background-polling.ts";
import { beginInitialSnapshot, snapshotAttemptsMigration } from "../../src/snapshot/begin-initial-snapshot.ts";
import { migrate } from "../../src/storage/migrations.ts";
import { syncMigration } from "../../src/sync/coordinate-sender-sync.ts";
import { createHttpTransport } from "../../src/transport/http-transport.ts";
import { openNodeDatabase } from "./node-database.ts";
import { NodeInstallationCipher } from "./node-installation-cipher.ts";

const MIGRATIONS = [
  snapshotAttemptsMigration, outboxMigration, completionMigration, checkpointMigration, syncMigration, schedulingMigration,
  deliveryMigration, enrollmentMigration,
];
const KEY = createHash("sha256").update("receipt-bound-delivery synthetic key").digest();
const utf8 = (text: string) => new TextEncoder().encode(text);
const sha256 = (bytes: Uint8Array) => createHash("sha256").update(bytes).digest("hex");

const [stateDirectory, command, rawArguments] = process.argv.slice(2);
const args = rawArguments ? JSON.parse(rawArguments) as Record<string, unknown> : {};
const now = () => typeof args.now === "number" ? args.now : Date.now();

const database = await openNodeDatabase(join(stateDirectory, "sender.sqlite"));
const migration = await migrate(database, MIGRATIONS);
if (migration.status !== "ready") throw new Error(`migration ${migration.status}`);
const cipher = new NodeInstallationCipher({ keyBytes: () => new Uint8Array(KEY), deviceSecure: true });
const transport = createHttpTransport({ fetch });
const enrollment = createSenderEnrollment({ database, cipher, transport, now, randomUUID });

async function currentStatus(): Promise<EnrollmentStatus | null> {
  const status = await enrollment.status();
  if (status !== null && "outcome" in status) throw new Error("enrollment status storage-failed");
  return status;
}

// A new process recreates the fixed key under the enrollment's alias, so the Outbox opens again.
let status = await currentStatus();
if (status !== null && command !== "enroll") await cipher.create(status.keyAlias);

function staging(override?: Partial<StagingContext["generation"]>): StagingContext {
  if (!status?.stagingContext) throw new Error("no staging context");
  const context = status.stagingContext;
  return override ? { ...context, generation: { ...context.generation, ...override } } : context;
}

/** The installation's Outbox: each row's sequence, kind, and the SHA-256 of its opened plaintext. */
async function outbox(): Promise<{ sequence: number; kind: string; sha256: string }[]> {
  const latest = await currentStatus();
  if (latest === null) return [];
  const rows = (await outboxMessages(database)).filter((row) => row.installationId === latest.installationId);
  const listed = [];
  for (const row of rows) {
    const opened = await cipher.open(latest.keyAlias, row.sealed, outboxAad(row.installationId, row.sequence));
    if (opened.status !== "ok") throw new Error(`cannot open ${row.sequence}: ${opened.reason}`);
    listed.push({ sequence: row.sequence, kind: row.kind, sha256: sha256(opened.value) });
  }
  return listed;
}

const issuer = { async issue() { return { outcome: "issued" as const, changesToken: `synthetic-token-${randomUUID()}`, unreadSinceMs: now() }; } };

async function run(): Promise<unknown> {
  switch (command) {
    case "enroll": {
      const result = await enrollment.enroll({ kind: "scanned", bytes: Buffer.from(args.offer as string, "hex") });
      status = await currentStatus();
      return result;
    }
    case "confirm": return enrollment.confirm();
    case "refresh": return enrollment.refreshCapabilities();
    case "status": return status;
    case "outbox": return null;
    case "send": {
      if (!status) throw new Error("not enrolled");
      const sender = createOutboxSender({
        database, cipher, transport, now, notifier: { async notify() {} }, context: status.deliveryContext,
      });
      return { attempt: await sender.sendNext(), headBlock: await sender.headBlock() };
    }
    case "stage-initial": {
      // An Initial Snapshot of `pages` source pages, then its Snapshot Completion.
      if (!status) throw new Error("not enrolled");
      const recordType = args.recordType as string;
      const version = typeof args.version === "number" ? args.version : 1;
      const context = version === 1 ? staging() : staging({ projectionSchemas: { [recordType]: [1, version] } });
      const begun = await beginInitialSnapshot(database, issuer, { recordType, installationId: status.installationId }, { randomUUID, now });
      if (begun.outcome !== "begun") return begun;
      const snapshotId = begun.attempt.snapshotId;
      const pages = args.pages as string[][];
      const staged = [];
      for (const [index, page] of pages.entries()) {
        const progress = await cycleProgress(database, snapshotId);
        const result = await stageOutboxBatch(database, cipher, context, {
          source: "snapshot", recordType, cycleId: snapshotId, projectionSchemaVersion: version, pageIndex: index + 1,
          readFrom: progress?.position ?? null, projections: page.map(utf8), nextPosition: utf8(`position-${index + 1}`),
          exhausted: index === pages.length - 1,
        });
        staged.push(result.outcome);
        if (result.outcome !== "staged") return { staged };
      }
      if (args.complete === false) return { staged };
      return { staged, completion: (await stageSyncCompletion(database, cipher, context, { recordType, cycleId: snapshotId })).outcome };
    }
    case "stage-change": {
      // One Change Cycle page of projections and retractions, then its Change Completion.
      const recordType = args.recordType as string;
      const origin = await changeOrigin(database, recordType);
      if (!origin) throw new Error("no change origin");
      const cycleId = randomUUID();
      const items = (args.items as [string, string][]).map(([kind, value]) =>
        kind === "retraction" ? { kind: "retraction" as const, sourceIdentity: value } : { kind: "projection" as const, bytes: utf8(value) });
      const batch = await stageOutboxBatch(database, cipher, staging(), {
        source: "change", recordType, cycleId, projectionSchemaVersion: 1, pageIndex: 1, readFrom: origin.changesToken, items,
        nextChangesToken: `synthetic-token-${randomUUID()}`, nextUnreadSinceMs: now(), hasMore: false,
      });
      if (batch.outcome !== "staged") return { batch: batch.outcome };
      return { batch: batch.outcome, completion: (await stageSyncCompletion(database, cipher, staging(), { recordType, cycleId })).outcome };
    }
    case "stage-checkpoint": {
      const at = now();
      return (await stageSyncCheckpoint(database, cipher, staging(), {
        runSequence: args.runSequence as number, trigger: "manual", startedAtMs: at - 30_000, endedAtMs: at,
        backgroundScheduling: "available", recordTypes: [{ recordType: "steps", outcome: "completed", sourceDataStaged: true }],
      })).outcome;
    }
    default: throw new Error(`unknown command ${command}`);
  }
}

const result = await run();
const listed = await outbox();
const latest = await currentStatus();
const highWater = latest === null ? 0 : await lastOutboxSequence(database, latest.installationId);
process.stdout.write(`${JSON.stringify({ result, outbox: listed, highWater, status: latest })}\n`);
await database.close();
