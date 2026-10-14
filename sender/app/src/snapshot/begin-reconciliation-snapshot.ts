import { commitSnapshotAttempt, currentSnapshotAttempt } from "./begin-initial-snapshot.ts";
import { cycleProgress } from "../outbox/stage-outbox-batch.ts";
import type { BeginOptions, ChangesTokenIssuer, IssuedToken, SnapshotAttempt } from "./begin-initial-snapshot.ts";
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

const RECORD_TYPE = /^[a-z][a-z0-9]*(-[a-z0-9]+)*$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const NOT_PERMITTED_FAILURES = new Set(["read-permission-missing", "history-permission-missing", "read-denied"]);
const READ_FAILURES = new Set([
  "health-connect-unavailable", "health-connect-io-failure", "health-connect-request-rejected",
  "unexpected-read-failure", "source-contract-violation",
]);

/** Pre-check, request a fresh pre-read token, then commit through begin-initial-snapshot@2, or nothing. */
export async function beginReconciliationSnapshot(
  database: SenderDatabase,
  issuer: ChangesTokenIssuer,
  request: ReconcileRequest,
  options: BeginOptions,
): Promise<ReconcileResult> {
  if (request === null || typeof request !== "object") {
    throw new TypeError("Invalid reconciliation request");
  }
  const { recordType, installationId, superseding } = request;
  if (typeof recordType !== "string" || recordType.length > 64 || RECORD_TYPE.exec(recordType)?.[0] !== recordType) {
    throw new TypeError("Invalid record type");
  }
  if (typeof installationId !== "string" || UUID.exec(installationId)?.[0] !== installationId) {
    throw new TypeError("Invalid installation UUID");
  }
  if (typeof superseding !== "string" || UUID.exec(superseding)?.[0] !== superseding) {
    throw new TypeError("Invalid superseding UUID");
  }

  try {
    const failure = await reconciliationRefusal(database, recordType, installationId, superseding);
    if (failure !== null) return { outcome: "refused", failure };
  } catch {
    return { outcome: "storage-failed", failure: "storage-failed" };
  }

  let issuance: IssuedToken;
  try {
    issuance = validateIssuance(await issuer.issue(recordType));
  } catch {
    return { outcome: "read-failed", failure: "unexpected-read-failure" };
  }
  if (issuance.outcome !== "issued") return { outcome: issuance.outcome, failure: issuance.failure };

  const snapshotId = options.randomUUID();
  if (typeof snapshotId !== "string" || UUID_V4.exec(snapshotId)?.[0] !== snapshotId) {
    throw new TypeError("Snapshot identity source must return a lowercase version 4 UUID");
  }

  try {
    return await database.transaction<ReconcileResult>(async (transaction) => {
      const failure = await reconciliationRefusal(transaction, recordType, installationId, superseding);
      if (failure !== null) return { outcome: "refused", failure };

      // Date both attempts only after all preconditions pass on the transaction handle.
      const begunAtMs = options.now();
      const result = await commitSnapshotAttempt(transaction, {
        snapshotId, recordType, kind: "reconciliation", installationId,
        changesToken: issuance.changesToken, unreadSinceMs: issuance.unreadSinceMs, begunAtMs,
      }, superseding);
      if (result.outcome === "refused") return { outcome: "refused", failure: result.failure };
      return { outcome: "begun", attempt: result.attempt };
    });
  } catch {
    return { outcome: "storage-failed", failure: "storage-failed" };
  }
}

/** The same ordered checks apply before issuance and inside the commit transaction. */
async function reconciliationRefusal(
  database: SenderDatabase,
  recordType: string,
  installationId: string,
  superseding: string,
): Promise<string | null> {
  const current = await currentSnapshotAttempt(database, recordType);
  if (current === null || current.snapshotId !== superseding) return "attempt-mismatch";
  if (current.installationId !== installationId) return "installation-mismatch";
  if (current.kind === "initial") {
    const progress = await cycleProgress(database, current.snapshotId);
    if (progress === null || !progress.finished || progress.cycleKind !== "initial-snapshot" ||
        progress.cycleId !== current.snapshotId || progress.snapshotId !== current.snapshotId ||
        progress.recordType !== current.recordType || progress.installationId !== current.installationId) {
      return "baseline-unfinished";
    }
  }
  return null;
}

/** Rebuild issuer outcomes so only the contract's fields and named failures escape. */
function validateIssuance(value: unknown): IssuedToken {
  if (value !== null && typeof value === "object") {
    const result = value as Record<string, unknown>;
    if (result.outcome === "issued" && typeof result.changesToken === "string" && result.changesToken.length > 0 &&
        typeof result.unreadSinceMs === "number" && Number.isSafeInteger(result.unreadSinceMs)) {
      return { outcome: "issued", changesToken: result.changesToken, unreadSinceMs: result.unreadSinceMs };
    }
    if (typeof result.failure === "string" &&
        ((result.outcome === "not-permitted" && NOT_PERMITTED_FAILURES.has(result.failure)) ||
         (result.outcome === "read-failed" && READ_FAILURES.has(result.failure)))) {
      return { outcome: result.outcome, failure: result.failure };
    }
  }
  return { outcome: "read-failed", failure: "source-contract-violation" };
}
