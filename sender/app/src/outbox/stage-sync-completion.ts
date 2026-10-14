import type { InstallationCipher } from "../installation-cipher.ts";
import type { SenderDatabase } from "../storage/database.ts";
import type { Migration } from "../storage/migrations.ts";
import type { StagingContext } from "./stage-outbox-batch.ts";

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

const NOT_IMPLEMENTED = "stage-sync-completion@1 is not implemented";

/** Sender schema version 3: this unit's completion records. */
export const completionMigration: Migration = Object.freeze({
  version: 3,
  apply(): void {
    throw new Error(NOT_IMPLEMENTED);
  },
});

/** Stage the cycle's Snapshot or Change Completion once, after its whole local chain, or nothing. */
export async function stageSyncCompletion(
  database: SenderDatabase,
  cipher: InstallationCipher,
  context: StagingContext,
  request: CompletionRequest,
): Promise<CompletionResult> {
  throw new Error(NOT_IMPLEMENTED);
}

export async function cycleCompletion(database: SenderDatabase, cycleId: string): Promise<CycleCompletion | null> {
  throw new Error(NOT_IMPLEMENTED);
}
