import type { InstallationCipher } from "../installation-cipher.ts";
import type { ChangeItem, StagingContext } from "../outbox/stage-outbox-batch.ts";
import type { BackgroundScheduling, CheckpointTrigger, RunSummary } from "../outbox/stage-sync-checkpoint.ts";
import type { ChangesTokenIssuer } from "../snapshot/begin-initial-snapshot.ts";
import type { SenderDatabase } from "../storage/database.ts";
import type { Migration } from "../storage/migrations.ts";

/** One snapshot page request, as read-snapshot-page@1 takes it (coordinate-sender-sync@1). */
export interface SnapshotReadRequest {
  readonly recordType: string;
  readonly snapshotId: string;
  /** The saved position, or `null` for the snapshot's first page. */
  readonly position: Uint8Array | null;
}

/** read-snapshot-page@1's result, as the Expo bridge delivers it. */
export type SnapshotRead =
  | {
    readonly outcome: "page";
    readonly projectionSchemaVersion: number;
    readonly projections: readonly Uint8Array[];
    readonly nextPosition: Uint8Array;
    readonly exhausted: boolean;
  }
  | { readonly outcome: "projection-refused"; readonly failure: string; readonly path: string }
  | { readonly outcome: "not-permitted" | "read-failed" | "invalid-position"; readonly failure: string };

/** One change page request, as read-change-page@1 takes it. */
export interface ChangeReadRequest {
  readonly recordType: string;
  readonly cycleId: string;
  readonly changesToken: string;
  readonly unreadSinceMs: number;
}

/** read-change-page@1's result, as the Expo bridge delivers it. */
export type ChangeRead =
  | {
    readonly outcome: "page";
    readonly projectionSchemaVersion: number;
    readonly items: readonly ChangeItem[];
    readonly nextChangesToken: string;
    readonly nextUnreadSinceMs: number;
    readonly hasMore: boolean;
  }
  | { readonly outcome: "projection-refused"; readonly failure: string; readonly path: string }
  | { readonly outcome: "token-expired" | "not-permitted" | "read-failed"; readonly failure: string };

/** The source seams: both readers, and the begin units' changes-token issuer. */
export interface SyncSources extends ChangesTokenIssuer {
  readSnapshotPage(request: SnapshotReadRequest): Promise<SnapshotRead>;
  readChangePage(request: ChangeReadRequest): Promise<ChangeRead>;
}

/** The conditions other units and the Sender shell detect, read before every source call and completion. */
export interface SyncConditions {
  readonly collectionEnabled: boolean;
  readonly secureLock: boolean;
  readonly deliveryBlocked: boolean;
  readonly storagePressure: boolean;
}

/** The Sender build's projection-implementation version and each managed type's projection schema version. */
export interface ProjectionVersions {
  readonly implementationVersion: string;
  readonly schemaVersions: Readonly<Record<string, number>>;
}

export interface SenderSyncOptions {
  readonly database: SenderDatabase;
  readonly cipher: InstallationCipher;
  readonly sources: SyncSources;
  readonly recordTypes: readonly string[];
  readonly projection: ProjectionVersions;
  /** Read once when a run opens. */
  readonly context: () => StagingContext | Promise<StagingContext>;
  readonly conditions: () => SyncConditions | Promise<SyncConditions>;
  /** Read once when a run opens. */
  readonly backgroundScheduling: () => BackgroundScheduling | Promise<BackgroundScheduling>;
  /** The cross-type pool size; 2 when omitted. */
  readonly concurrency?: number;
  readonly now: () => number;
  readonly monotonicNow: () => number;
  readonly randomUUID: () => string;
}

export interface TriggerRequest {
  readonly kind: CheckpointTrigger;
  /** The soft execution budget in milliseconds; none when omitted. */
  readonly budgetMs?: number;
  /** Record types to move forward once; manual triggers only. */
  readonly priority?: readonly string[];
}

export type RunResult =
  | { readonly outcome: "checkpointed"; readonly runSequence: number; readonly summary: RunSummary }
  | { readonly outcome: "failed"; readonly failure: string };

export type RetryResult = RunResult | { readonly outcome: "refused"; readonly failure: "retry-not-blocked" };

export interface SenderSync {
  trigger(request: TriggerRequest): Promise<RunResult>;
  requestRetry(recordType: string): Promise<RetryResult>;
}

export type BlockCause = "projection-refusal" | "record-too-large";

/** A record type's health-data-free block. */
export interface TypeBlock {
  readonly recordType: string;
  readonly cause: BlockCause;
  /** The refusal's failure name, or `indivisible-record-limit-exceeded`. */
  readonly failure: string;
  /** The refused field's JSON path for a Projection Refusal; `null` for an oversized record. */
  readonly path: string | null;
  readonly retryRequested: boolean;
}

const notImplemented = (): never => { throw new Error("coordinate-sender-sync@1 is not implemented"); };

/** Sender schema version 5: this unit's run, ring, cycle, and block records. */
export const syncMigration: Migration = Object.freeze({
  version: 5,
  async apply(_transaction: SenderDatabase): Promise<void> { notImplemented(); },
});

/** Create the Sender's one synchronization module. */
export function createSenderSync(_options: SenderSyncOptions): SenderSync {
  return notImplemented();
}

export async function typeBlock(_database: SenderDatabase, _recordType: string): Promise<TypeBlock | null> {
  return notImplemented();
}
