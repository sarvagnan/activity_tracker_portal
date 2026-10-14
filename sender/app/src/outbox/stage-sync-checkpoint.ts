import type { InstallationCipher } from "../installation-cipher.ts";
import type { SenderDatabase } from "../storage/database.ts";
import type { Migration } from "../storage/migrations.ts";
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

const NOT_IMPLEMENTED = "stage-sync-checkpoint@1 is not implemented";

/** Sender schema version 4: this unit's checkpoint records. */
export const checkpointMigration: Migration = Object.freeze({
  version: 4,
  apply(): void {
    throw new Error(NOT_IMPLEMENTED);
  },
});

/** Stage the run's one health-data-free Sync Checkpoint, or nothing. */
export async function stageSyncCheckpoint(
  database: SenderDatabase,
  cipher: InstallationCipher,
  context: StagingContext,
  summary: RunSummary,
): Promise<CheckpointResult> {
  throw new Error(NOT_IMPLEMENTED);
}

export async function runCheckpoint(database: SenderDatabase, installationId: string, runSequence: number): Promise<RunCheckpoint | null> {
  throw new Error(NOT_IMPLEMENTED);
}
