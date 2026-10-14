import { encodeMessage } from "@activity-tracker-portal/ingest-protocol";
import { currentSnapshotAttempt } from "../snapshot/begin-initial-snapshot.ts";
import type { SnapshotAttempt } from "../snapshot/begin-initial-snapshot.ts";
import type { InstallationCipher } from "../installation-cipher.ts";
import type { SenderDatabase } from "../storage/database.ts";
import type { Migration } from "../storage/migrations.ts";
import { appendOutboxMessages, cycleProgress, lastOutboxSequence, outboxAad } from "./stage-outbox-batch.ts";
import type { CycleProgress, StagingContext } from "./stage-outbox-batch.ts";

/** The cycle to complete: a snapshot attempt's own cycle or one of its Change Cycles (stage-sync-completion@1). */
export interface CompletionRequest {
  readonly recordType: string;
  readonly cycleId: string;
}

/** The one completion recorded for a cycle, and the Outbox sequence that carries it. */
export interface CycleCompletion {
  readonly cycleId: string;
  readonly recordType: string;
  readonly kind: "snapshot-completion" | "change-completion";
  readonly installationId: string;
  readonly sequence: number;
}

export type CompletionResult =
  | { readonly outcome: "staged"; readonly completion: CycleCompletion }
  | { readonly outcome: "refused" | "conflict" | "cipher-failed" | "storage-failed"; readonly failure: string };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const RECORD_TYPE = /^[a-z][a-z0-9]*(-[a-z0-9]+)*$/;

/** Sender schema version 3: this unit's completion records. */
export const completionMigration: Migration = Object.freeze({
  version: 3,
  async apply(transaction: SenderDatabase): Promise<void> {
    await transaction.execute(`CREATE TABLE cycle_completions (
      cycle_id TEXT PRIMARY KEY NOT NULL, record_type TEXT NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('snapshot-completion', 'change-completion')),
      installation_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK (sequence > 0),
      UNIQUE (installation_id, sequence)) STRICT`);
  },
});

/** Stage the cycle's Snapshot or Change Completion once, after its whole local chain, or nothing. */
export async function stageSyncCompletion(
  database: SenderDatabase,
  cipher: InstallationCipher,
  context: StagingContext,
  request: CompletionRequest,
): Promise<CompletionResult> {
  validateInputs(context, request);
  let state: CompletionState;
  try {
    state = await readState(database, context, request);
  } catch {
    return { outcome: "storage-failed", failure: "storage-failed" };
  }
  const failure = completionRefusal(state, context, request);
  if (failure) return { outcome: "refused", failure };

  // The refusals establish a finished cycle under its current attempt.
  const cycle = state.cycle!;
  const kind = cycle.cycleKind === "change-cycle" ? "change-completion" : "snapshot-completion";
  const completion: CycleCompletion = {
    cycleId: cycle.cycleId, recordType: cycle.recordType, kind,
    installationId: context.installationId, sequence: state.lastSequence + 1,
  };
  const bytes = encodeMessage({
    wire_version: 1, kind, sender_installation: context.installationId,
    sequence: completion.sequence, capability_generation: context.generation.generation,
    record_type: cycle.recordType, cycle_id: cycle.cycleId,
    page_count: cycle.pagesStaged, fragment_count: cycle.fragmentCount,
    record_count: cycle.recordCount, final_chain: cycle.chain,
    ...(kind === "snapshot-completion" ? { snapshot_kind: state.attempt!.kind } : {}),
  });
  let sealed: Uint8Array;
  try {
    const result = await cipher.seal(context.keyAlias, bytes, outboxAad(context.installationId, completion.sequence));
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
    return await database.transaction<CompletionResult>(async (transaction) => {
      const current = await readState(transaction, context, request);
      if (!sameState(state, current) || !await appendOutboxMessages(
        transaction, context.installationId, state.lastSequence, [{ kind, sealed, messageBytes: bytes.length }],
      )) {
        return { outcome: "conflict", failure: "staging-conflict" };
      }
      await transaction.execute(
        `INSERT INTO cycle_completions (cycle_id, record_type, kind, installation_id, sequence)
          VALUES (?, ?, ?, ?, ?)`,
        [completion.cycleId, completion.recordType, completion.kind, completion.installationId, completion.sequence],
      );
      return { outcome: "staged", completion };
    });
  } catch {
    return { outcome: "storage-failed", failure: "storage-failed" };
  }
}

export async function cycleCompletion(database: SenderDatabase, cycleId: string): Promise<CycleCompletion | null> {
  checkUuid(cycleId);
  const [row] = await database.query("SELECT * FROM cycle_completions WHERE cycle_id = ?", [cycleId]);
  return row ? {
    cycleId: row.cycle_id as string, recordType: row.record_type as string,
    kind: row.kind as CycleCompletion["kind"], installationId: row.installation_id as string,
    sequence: row.sequence as number,
  } : null;
}

interface CompletionState {
  readonly attempt: SnapshotAttempt | null;
  readonly cycle: CycleProgress | null;
  readonly completion: CycleCompletion | null;
  readonly lastSequence: number;
}

async function readState(
  database: SenderDatabase,
  context: StagingContext,
  request: CompletionRequest,
): Promise<CompletionState> {
  const attempt = await currentSnapshotAttempt(database, request.recordType);
  const cycle = await cycleProgress(database, request.cycleId);
  const completion = await cycleCompletion(database, request.cycleId);
  const lastSequence = await lastOutboxSequence(database, context.installationId);
  return { attempt, cycle, completion, lastSequence };
}

function completionRefusal(state: CompletionState, context: StagingContext, request: CompletionRequest): string | null {
  const { attempt, cycle, completion } = state;
  if (!attempt || attempt.abandonedAtMs !== null || attempt.recordType !== request.recordType) return "cycle-not-current";
  if (!cycle) return request.cycleId === attempt.snapshotId ? "chain-incomplete" : "cycle-not-current";
  const expectedKind = cycle.cycleId === attempt.snapshotId
    ? attempt.kind === "initial" ? "initial-snapshot" : "reconciliation-snapshot"
    : "change-cycle";
  if (cycle.recordType !== request.recordType || cycle.snapshotId !== attempt.snapshotId || cycle.cycleKind !== expectedKind) {
    return "cycle-not-current";
  }
  if (cycle.installationId !== context.installationId || attempt.installationId !== context.installationId) {
    return "installation-mismatch";
  }
  if (!cycle.finished) return "chain-incomplete";
  if (completion) return "completion-already-staged";
  return null;
}

function sameFields(left: object | null, right: object | null): boolean {
  if (left === null || right === null) return left === right;
  const a = left as Record<string, unknown>;
  const b = right as Record<string, unknown>;
  return Object.keys(a).length === Object.keys(b).length && Object.keys(a).every((key) => {
    const value = a[key];
    const other = b[key];
    return value instanceof Uint8Array
      ? other instanceof Uint8Array && value.length === other.length && value.every((byte, index) => byte === other[index])
      : value === other;
  });
}

function sameState(left: CompletionState, right: CompletionState): boolean {
  return left.lastSequence === right.lastSequence && sameFields(left.attempt, right.attempt) &&
    sameFields(left.cycle, right.cycle) && sameFields(left.completion, right.completion);
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

function checkRecordType(value: unknown): void {
  if (typeof value !== "string" || value.length > 64 || RECORD_TYPE.exec(value)?.[0] !== value) throw new TypeError("Invalid record type");
}

function validateInputs(context: StagingContext, request: CompletionRequest): void {
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
  if (!object(request)) throw new TypeError("Invalid completion request");
  checkRecordType(request.recordType);
  checkUuid(request.cycleId);
}
