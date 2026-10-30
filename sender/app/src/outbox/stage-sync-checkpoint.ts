import { encodeMessage } from "@activity-tracker-portal/ingest-protocol";
import type { InstallationCipher } from "../installation-cipher.ts";
import type { SenderDatabase } from "../storage/database.ts";
import type { Migration } from "../storage/migrations.ts";
import { appendOutboxMessages, lastOutboxSequence, outboxAad, outboxBacklog } from "./stage-outbox-batch.ts";
import type { StagingContext } from "./stage-outbox-batch.ts";

export type CheckpointTrigger = "background" | "foreground" | "manual";
export type BackgroundScheduling = "available" | "restricted" | "unregistered";
export type TypeOutcome =
  | "not-due" | "no-change" | "progressed" | "completed" | "deferred" | "blocked" | "not-permitted" | "failed" | "interrupted";
export type OutcomeReason =
  | "collection-disabled" | "projection-refusal" | "record-too-large" | "permission-missing" | "delivery-blocked"
  | "storage-pressure" | "screen-lock-removed" | "changes-token-expired" | "time-budget-reached" | "read-failed";

/** One permitted record type's lifecycle outcome in a Polling Run (stage-sync-checkpoint@1). */
export interface RecordTypeOutcome {
  readonly recordType: string;
  readonly outcome: TypeOutcome;
  readonly reason?: OutcomeReason;
  readonly sourceDataStaged: boolean;
}

/** One finished or recovered-interrupted Polling Run, as coordinate-sender-sync records it. */
export interface RunSummary {
  readonly runSequence: number;
  readonly trigger: CheckpointTrigger;
  readonly startedAtMs: number;
  readonly endedAtMs: number;
  readonly elapsedMs?: number;
  readonly backgroundScheduling: BackgroundScheduling;
  readonly recordTypes: readonly RecordTypeOutcome[];
}

/** The one checkpoint recorded for a run, and the Outbox sequence that carries it. */
export interface RunCheckpoint {
  readonly installationId: string;
  readonly runSequence: number;
  readonly sequence: number;
}

export type CheckpointResult =
  | { readonly outcome: "staged"; readonly checkpoint: RunCheckpoint }
  | { readonly outcome: "refused" | "conflict" | "cipher-failed" | "storage-failed"; readonly failure: string };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const RECORD_TYPE = /^[a-z][a-z0-9]*(-[a-z0-9]+)*$/;
const TRIGGERS: ReadonlySet<string> = new Set(["background", "foreground", "manual"]);
const SCHEDULING: ReadonlySet<string> = new Set(["available", "restricted", "unregistered"]);
const OUTCOMES: ReadonlySet<string> = new Set([
  "not-due", "no-change", "progressed", "completed", "deferred", "blocked", "not-permitted", "failed", "interrupted",
]);
const REASONS: ReadonlySet<string> = new Set([
  "collection-disabled", "projection-refusal", "record-too-large", "permission-missing", "delivery-blocked",
  "storage-pressure", "screen-lock-removed", "changes-token-expired", "time-budget-reached", "read-failed",
]);
const REASON_REQUIRED: ReadonlySet<string> = new Set(["deferred", "blocked", "failed"]);
const REASON_OPTIONAL: ReadonlySet<string> = new Set(["progressed", "interrupted"]);

/** Sender schema version 4: this unit's checkpoint records. */
export const checkpointMigration: Migration = Object.freeze({
  version: 4,
  async apply(transaction: SenderDatabase): Promise<void> {
    await transaction.execute(`CREATE TABLE run_checkpoints (
      installation_id TEXT NOT NULL, run_sequence INTEGER NOT NULL CHECK (run_sequence > 0),
      sequence INTEGER NOT NULL CHECK (sequence > 0),
      PRIMARY KEY (installation_id, run_sequence), UNIQUE (installation_id, sequence)) STRICT`);
  },
});

/** Stage the run's one health-data-free Sync Checkpoint, or nothing. */
export async function stageSyncCheckpoint(
  database: SenderDatabase,
  cipher: InstallationCipher,
  context: StagingContext,
  summary: RunSummary,
): Promise<CheckpointResult> {
  validateInputs(context, summary);
  let state: CheckpointState;
  try {
    state = await readState(database, context.installationId, summary.runSequence);
  } catch {
    return { outcome: "storage-failed", failure: "storage-failed" };
  }
  if (state.checkpoint) return { outcome: "refused", failure: "checkpoint-already-staged" };

  const checkpoint: RunCheckpoint = {
    installationId: context.installationId, runSequence: summary.runSequence, sequence: state.lastSequence + 1,
  };
  const bytes = encodeMessage({
    wire_version: 1, kind: "sync-checkpoint", sender_installation: context.installationId,
    sequence: checkpoint.sequence, capability_generation: context.generation.generation,
    run_sequence: summary.runSequence, trigger: summary.trigger,
    started_at: new Date(summary.startedAtMs).toISOString(), ended_at: new Date(summary.endedAtMs).toISOString(),
    ...(summary.elapsedMs === undefined ? {} : { elapsed_ms: summary.elapsedMs }),
    background_scheduling: summary.backgroundScheduling,
    record_types: [...summary.recordTypes].sort((a, b) => a.recordType < b.recordType ? -1 : 1).map((entry) => ({
      record_type: entry.recordType, outcome: entry.outcome, source_data_staged: entry.sourceDataStaged,
      ...(entry.reason === undefined ? {} : { reason: entry.reason }),
    })),
    outbox_messages_before_checkpoint: state.messageCount, outbox_bytes_before_checkpoint: state.byteCount,
  });
  let sealed: Uint8Array;
  try {
    const result = await cipher.seal(context.keyAlias, bytes, outboxAad(context.installationId, checkpoint.sequence));
    if (object(result) && result.status === "ok" && result.value instanceof Uint8Array && result.value.length > 0) {
      sealed = new Uint8Array(result.value);
    } else {
      let failure = "unexpected-cipher-failure";
      if (object(result) && result.status === "error") {
        if (result.reason === "missing-key") failure = "installation-key-missing";
        if (result.reason === "unusable-key") failure = "installation-key-unusable";
        if (result.reason === "keystore-unavailable") failure = "keystore-unavailable";
      }
      return { outcome: "cipher-failed", failure };
    }
  } catch {
    return { outcome: "cipher-failed", failure: "unexpected-cipher-failure" };
  }

  try {
    return await database.transaction<CheckpointResult>(async (transaction) => {
      // Sealing can interleave with another writer; all sampled state repeats on this handle.
      const current = await readState(transaction, checkpoint.installationId, checkpoint.runSequence);
      if (current.checkpoint !== null || current.lastSequence !== state.lastSequence ||
          current.messageCount !== state.messageCount || current.byteCount !== state.byteCount) {
        return { outcome: "conflict", failure: "staging-conflict" };
      }
      if (!await appendOutboxMessages(transaction, checkpoint.installationId, state.lastSequence,
        [{ kind: "sync-checkpoint", sealed, messageBytes: bytes.length }])) {
        return { outcome: "conflict", failure: "staging-conflict" };
      }
      await transaction.execute(
        "INSERT INTO run_checkpoints (installation_id, run_sequence, sequence) VALUES (?, ?, ?)",
        [checkpoint.installationId, checkpoint.runSequence, checkpoint.sequence],
      );
      return { outcome: "staged", checkpoint };
    });
  } catch {
    return { outcome: "storage-failed", failure: "storage-failed" };
  }
}

export async function runCheckpoint(database: SenderDatabase, installationId: string, runSequence: number): Promise<RunCheckpoint | null> {
  checkUuid(installationId);
  if (!positive(runSequence)) throw new TypeError("Invalid run sequence");
  const [row] = await database.query(
    "SELECT * FROM run_checkpoints WHERE installation_id = ? AND run_sequence = ?", [installationId, runSequence],
  );
  return row ? {
    installationId: row.installation_id as string, runSequence: row.run_sequence as number, sequence: row.sequence as number,
  } : null;
}

interface CheckpointState {
  readonly messageCount: number;
  readonly byteCount: number;
  readonly lastSequence: number;
  readonly checkpoint: RunCheckpoint | null;
}

async function readState(database: SenderDatabase, installationId: string, runSequence: number): Promise<CheckpointState> {
  const backlog = await outboxBacklog(database, installationId);
  const lastSequence = await lastOutboxSequence(database, installationId);
  const checkpoint = await runCheckpoint(database, installationId, runSequence);
  return { messageCount: backlog.messageCount, byteCount: backlog.byteCount, lastSequence, checkpoint };
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object";
}

function positive(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function checkUuid(value: unknown): void {
  if (typeof value !== "string" || UUID.exec(value)?.[0] !== value) throw new TypeError("Invalid UUID");
}

function checkRecordType(value: unknown): asserts value is string {
  if (typeof value !== "string" || value.length > 64 || RECORD_TYPE.exec(value)?.[0] !== value) throw new TypeError("Invalid record type");
}

function instant(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= -62_167_219_200_000 && value <= 253_402_300_799_999;
}

function validateInputs(context: StagingContext, summary: RunSummary): void {
  if (!object(context)) throw new TypeError("Invalid staging context");
  checkUuid(context.installationId);
  if (typeof context.keyAlias !== "string" || context.keyAlias.length === 0) throw new TypeError("Invalid key alias");
  const generation = context.generation;
  if (!object(generation) || !positive(generation.generation) || !positive(generation.maxRecordCount) ||
      !positive(generation.maxMessageBytes) || generation.maxMessageBytes < 4096) throw new TypeError("Invalid generation");
  const schemas = generation.projectionSchemas;
  if (!object(schemas) || (Object.getPrototypeOf(schemas) !== Object.prototype && Object.getPrototypeOf(schemas) !== null)) {
    throw new TypeError("Invalid projection schemas");
  }
  for (const key of Object.getOwnPropertyNames(schemas)) {
    checkRecordType(key);
    const versions = schemas[key];
    if (!Array.isArray(versions)) throw new TypeError("Invalid schema versions");
    for (const version of versions) if (!positive(version)) throw new TypeError("Invalid schema version");
  }
  if (!object(summary) || !positive(summary.runSequence) || !TRIGGERS.has(summary.trigger) ||
      !SCHEDULING.has(summary.backgroundScheduling) || !instant(summary.startedAtMs) || !instant(summary.endedAtMs) ||
      !Array.isArray(summary.recordTypes)) throw new TypeError("Invalid run summary");
  if (summary.elapsedMs !== undefined && (!Number.isSafeInteger(summary.elapsedMs) || summary.elapsedMs < 0)) {
    throw new TypeError("Invalid elapsed duration");
  }
  const types = new Set<string>();
  for (const entry of summary.recordTypes) {
    if (!object(entry)) throw new TypeError("Invalid record type outcome");
    checkRecordType(entry.recordType);
    if (types.has(entry.recordType) || typeof entry.outcome !== "string" ||
        !OUTCOMES.has(entry.outcome) || typeof entry.sourceDataStaged !== "boolean") {
      throw new TypeError("Invalid record type outcome");
    }
    types.add(entry.recordType);
    if (entry.reason === undefined) {
      if (REASON_REQUIRED.has(entry.outcome)) throw new TypeError("Missing outcome reason");
    } else if (typeof entry.reason !== "string" || !REASONS.has(entry.reason) ||
        (!REASON_REQUIRED.has(entry.outcome) && !REASON_OPTIONAL.has(entry.outcome))) {
      throw new TypeError("Invalid outcome reason");
    }
  }
}
