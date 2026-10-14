import type { BeginOptions, ChangesTokenIssuer, SnapshotAttempt } from "./begin-initial-snapshot.ts";
import type { SenderDatabase } from "../storage/database.ts";

/** Begin a Reconciliation Snapshot superseding the named current attempt (begin-reconciliation-snapshot@1). */
export interface ReconcileRequest {
  readonly recordType: string;
  readonly installationId: string;
  /** The identity of the record type's current attempt, whose change-token continuity was lost. */
  readonly superseding: string;
}

export type ReconcileResult =
  | { readonly outcome: "begun"; readonly attempt: SnapshotAttempt }
  | { readonly outcome: "refused" | "not-permitted" | "read-failed" | "storage-failed"; readonly failure: string };

const NOT_IMPLEMENTED = "begin-reconciliation-snapshot@1 is not implemented";

/** Pre-check, request a fresh pre-read token, then commit through begin-initial-snapshot@2, or nothing. */
export async function beginReconciliationSnapshot(
  database: SenderDatabase,
  issuer: ChangesTokenIssuer,
  request: ReconcileRequest,
  options: BeginOptions,
): Promise<ReconcileResult> {
  throw new Error(NOT_IMPLEMENTED);
}
