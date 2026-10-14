import type { InstallationCipher } from "../installation-cipher.ts";
import type { SenderDatabase } from "../storage/database.ts";
import type { Migration } from "../storage/migrations.ts";

/** One item of a change page, in the order read-change-page@1 returned it (stage-outbox-batch@1). */
export type ChangeItem =
  | { readonly kind: "retraction"; readonly sourceIdentity: string }
  | { readonly kind: "projection"; readonly bytes: Uint8Array };

interface PageBase {
  readonly recordType: string;
  readonly cycleId: string;
  readonly projectionSchemaVersion: number;
  /** One-based source-page index within the cycle. */
  readonly pageIndex: number;
}

/** One page from read-snapshot-page@1, with the position it was read from (`null` for the first page). */
export interface SnapshotPage extends PageBase {
  readonly source: "snapshot";
  readonly readFrom: Uint8Array | null;
  readonly projections: readonly Uint8Array[];
  readonly nextPosition: Uint8Array;
  readonly exhausted: boolean;
}

/** One page from read-change-page@1, with the changes token it was read from. */
export interface ChangePage extends PageBase {
  readonly source: "change";
  readonly readFrom: string;
  readonly items: readonly ChangeItem[];
  readonly nextChangesToken: string;
  readonly nextUnreadSinceMs: number;
  readonly hasMore: boolean;
}

export type ObservedPage = SnapshotPage | ChangePage;

/** The capability generation a page is staged under, as adopted by manage-sender-enrollment. */
export interface StagingGeneration {
  readonly generation: number;
  readonly maxRecordCount: number;
  readonly maxMessageBytes: number;
  /** Each supported record type with its supported projection schema versions. */
  readonly projectionSchemas: Readonly<Record<string, readonly number[]>>;
}

export interface StagingContext {
  readonly installationId: string;
  readonly keyAlias: string;
  readonly generation: StagingGeneration;
}

export type CycleKind = "initial-snapshot" | "reconciliation-snapshot" | "change-cycle";

export interface CycleProgress {
  readonly cycleId: string;
  readonly recordType: string;
  readonly cycleKind: CycleKind;
  /** The snapshot attempt the cycle descends from; a snapshot cycle's own identity. */
  readonly snapshotId: string;
  readonly installationId: string;
  readonly pagesStaged: number;
  readonly fragmentCount: number;
  readonly recordCount: number;
  readonly chain: string;
  /** A snapshot cycle's saved position; `null` for a Change Cycle. */
  readonly position: Uint8Array | null;
  /** A Change Cycle's saved changes token and unread-since instant; `null` for a snapshot cycle. */
  readonly changesToken: string | null;
  readonly unreadSinceMs: number | null;
  readonly finished: boolean;
}

export type StageResult =
  | { readonly outcome: "staged"; readonly progress: CycleProgress; readonly firstSequence: number; readonly lastSequence: number }
  | { readonly outcome: "refused" | "blocked" | "conflict" | "cipher-failed" | "storage-failed"; readonly failure: string };

export type OutboxKind =
  | "capability-acknowledgement"
  | "ingest-batch"
  | "snapshot-completion"
  | "change-completion"
  | "sync-checkpoint";

export interface OutboxMessage {
  readonly kind: OutboxKind;
  readonly sealed: Uint8Array;
  /** The plaintext message's byte length. */
  readonly messageBytes: number;
}

export interface OutboxRow extends OutboxMessage {
  readonly installationId: string;
  readonly sequence: number;
}

const NOT_IMPLEMENTED = "stage-outbox-batch@1 is not implemented";

/** Sender schema version 2: the Sender Outbox, its high-water marks, and cycle progress. */
export const outboxMigration: Migration = Object.freeze({
  version: 2,
  apply(): void {
    throw new Error(NOT_IMPLEMENTED);
  },
});

/** The authenticated data that binds a sealed Outbox Message to its installation and sequence. */
export function outboxAad(installationId: string, sequence: number): Uint8Array {
  throw new Error(NOT_IMPLEMENTED);
}

/** The installation's sequence high-water mark, or 0 before its first Outbox Message. */
export async function lastOutboxSequence(database: SenderDatabase, installationId: string): Promise<number> {
  throw new Error(NOT_IMPLEMENTED);
}

/**
 * Append sealed messages under the sequences after `expectedLastSequence`, inside the caller's
 * transaction. Returns `false` and writes nothing when the high-water mark has moved.
 */
export async function appendOutboxMessages(
  transaction: SenderDatabase,
  installationId: string,
  expectedLastSequence: number,
  messages: readonly OutboxMessage[],
): Promise<boolean> {
  throw new Error(NOT_IMPLEMENTED);
}

/** Every Outbox row, ordered by installation and sequence. */
export async function outboxMessages(database: SenderDatabase): Promise<OutboxRow[]> {
  throw new Error(NOT_IMPLEMENTED);
}

export async function cycleProgress(database: SenderDatabase, cycleId: string): Promise<CycleProgress | null> {
  throw new Error(NOT_IMPLEMENTED);
}

/** Where the record type's next new Change Cycle must read from, or `null` without a current attempt. */
export interface ChangeOrigin {
  readonly snapshotId: string;
  readonly changesToken: string;
  readonly unreadSinceMs: number;
}

/**
 * The final token of the latest finished Change Cycle under the record type's current snapshot
 * attempt, or else that attempt's pre-read token, each with its unread-since instant.
 */
export async function changeOrigin(database: SenderDatabase, recordType: string): Promise<ChangeOrigin | null> {
  throw new Error(NOT_IMPLEMENTED);
}

/** Stage one observed page: partition, seal, and commit with the cycle's progress, or nothing. */
export async function stageOutboxBatch(
  database: SenderDatabase,
  cipher: InstallationCipher,
  context: StagingContext,
  page: ObservedPage,
): Promise<StageResult> {
  throw new Error(NOT_IMPLEMENTED);
}
