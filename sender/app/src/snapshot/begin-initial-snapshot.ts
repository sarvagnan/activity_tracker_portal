import type { DatabaseRow, SenderDatabase } from "../storage/database.ts";
import type { Migration } from "../storage/migrations.ts";

/** What the Kotlin token issuer reports across the Expo bridge (begin-initial-snapshot@1). */
export type IssuedToken =
  | { readonly outcome: "issued"; readonly changesToken: string; readonly unreadSinceMs: number }
  | { readonly outcome: "not-permitted" | "read-failed"; readonly failure: string };

export interface ChangesTokenIssuer {
  issue(recordType: string): Promise<IssuedToken>;
}

export interface SnapshotAttempt {
  readonly snapshotId: string;
  readonly recordType: string;
  readonly kind: "initial" | "reconciliation";
  readonly installationId: string;
  readonly changesToken: string;
  readonly unreadSinceMs: number;
  readonly begunAtMs: number;
  readonly abandonedAtMs: number | null;
}

export interface BeginRequest {
  readonly recordType: string;
  readonly installationId: string;
  readonly abandoning?: string;
}

export interface BeginOptions {
  readonly randomUUID: () => string;
  readonly now: () => number;
}

export type BeginResult =
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

/** Sender schema version 1: this unit's snapshot-attempt tables. */
export const snapshotAttemptsMigration: Migration = Object.freeze({
  version: 1,
  async apply(transaction: SenderDatabase): Promise<void> {
    await transaction.execute(`CREATE TABLE snapshot_attempts (
      snapshot_id TEXT PRIMARY KEY NOT NULL, record_type TEXT NOT NULL,
      kind TEXT NOT NULL CHECK (kind IN ('initial', 'reconciliation')), installation_id TEXT NOT NULL,
      changes_token TEXT NOT NULL, unread_since_ms INTEGER NOT NULL, begun_at_ms INTEGER NOT NULL,
      abandoned_at_ms INTEGER, UNIQUE (snapshot_id, record_type)) STRICT`);
    await transaction.execute(`CREATE TABLE current_snapshot_attempts (
      record_type TEXT PRIMARY KEY NOT NULL, snapshot_id TEXT NOT NULL UNIQUE,
      FOREIGN KEY (snapshot_id, record_type) REFERENCES snapshot_attempts (snapshot_id, record_type)) STRICT`);
  },
});

/** Begin a record type's Initial Snapshot: token first, then one durable commit, or nothing. */
export async function beginInitialSnapshot(
  database: SenderDatabase,
  issuer: ChangesTokenIssuer,
  request: BeginRequest,
  options: BeginOptions,
): Promise<BeginResult> {
  const { recordType, installationId, abandoning } = request;
  if (typeof recordType !== "string" || recordType.length > 64 || RECORD_TYPE.exec(recordType)?.[0] !== recordType) {
    throw new TypeError("Invalid record type");
  }
  if (typeof installationId !== "string" || UUID.exec(installationId)?.[0] !== installationId) {
    throw new TypeError("Invalid installation UUID");
  }
  if (abandoning !== undefined && (typeof abandoning !== "string" || UUID.exec(abandoning)?.[0] !== abandoning)) {
    throw new TypeError("Invalid abandoning UUID");
  }

  try {
    const failure = attemptRefusal(await currentSnapshotAttempt(database, recordType), abandoning);
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
    return await database.transaction<BeginResult>(async (transaction) => {
      const current = await currentSnapshotAttempt(transaction, recordType);
      const failure = attemptRefusal(current, abandoning);
      if (failure !== null) return { outcome: "refused", failure };

      // One commit-time reading dates both the replacement and any named abandonment.
      const begunAtMs = options.now();
      const attempt: SnapshotAttempt = {
        snapshotId, recordType, kind: "initial", installationId,
        changesToken: issuance.changesToken, unreadSinceMs: issuance.unreadSinceMs,
        begunAtMs, abandonedAtMs: null,
      };
      if (current !== null) {
        await transaction.execute("UPDATE snapshot_attempts SET abandoned_at_ms = ? WHERE snapshot_id = ?", [begunAtMs, current.snapshotId]);
      }
      await transaction.execute(
        `INSERT INTO snapshot_attempts
          (snapshot_id, record_type, kind, installation_id, changes_token, unread_since_ms, begun_at_ms, abandoned_at_ms)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
        [snapshotId, recordType, attempt.kind, installationId, attempt.changesToken, attempt.unreadSinceMs, begunAtMs, null],
      );
      await transaction.execute(
        `INSERT INTO current_snapshot_attempts (record_type, snapshot_id) VALUES (?, ?)
          ON CONFLICT (record_type) DO UPDATE SET snapshot_id = excluded.snapshot_id`,
        [recordType, snapshotId],
      );
      return { outcome: "begun", attempt };
    });
  } catch {
    return { outcome: "storage-failed", failure: "storage-failed" };
  }
}

export async function currentSnapshotAttempt(database: SenderDatabase, recordType: string): Promise<SnapshotAttempt | null> {
  const [row] = await database.query(
    `SELECT attempt.* FROM snapshot_attempts AS attempt
      JOIN current_snapshot_attempts AS current
        ON current.snapshot_id = attempt.snapshot_id AND current.record_type = attempt.record_type
      WHERE current.record_type = ?`,
    [recordType],
  );
  return row ? attemptFromRow(row) : null;
}

export async function snapshotAttempt(database: SenderDatabase, snapshotId: string): Promise<SnapshotAttempt | null> {
  const [row] = await database.query("SELECT * FROM snapshot_attempts WHERE snapshot_id = ?", [snapshotId]);
  return row ? attemptFromRow(row) : null;
}

function attemptRefusal(current: SnapshotAttempt | null, abandoning: string | undefined): string | null {
  if (abandoning !== undefined) return current?.snapshotId === abandoning ? null : "attempt-mismatch";
  return current === null ? null : "attempt-open";
}

/** Rebuild failure outcomes so bridge payloads cannot leak extra fields. */
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

function attemptFromRow(row: DatabaseRow): SnapshotAttempt {
  return {
    snapshotId: row.snapshot_id as string,
    recordType: row.record_type as string,
    kind: row.kind as SnapshotAttempt["kind"],
    installationId: row.installation_id as string,
    changesToken: row.changes_token as string,
    unreadSinceMs: row.unread_since_ms as number,
    begunAtMs: row.begun_at_ms as number,
    abandonedAtMs: row.abandoned_at_ms as number | null,
  };
}

/** A complete new attempt of either kind, as a snapshot unit commits it (begin-initial-snapshot@2). */
export interface NewSnapshotAttempt {
  readonly snapshotId: string;
  readonly recordType: string;
  readonly kind: "initial" | "reconciliation";
  readonly installationId: string;
  readonly changesToken: string;
  readonly unreadSinceMs: number;
  readonly begunAtMs: number;
}

export type CommitAttemptResult =
  | { readonly outcome: "committed"; readonly attempt: SnapshotAttempt }
  | { readonly outcome: "refused"; readonly failure: "attempt-open" | "attempt-mismatch" };

/**
 * Inside the caller's transaction, commit `attempt` as its record type's current attempt,
 * superseding and retaining the attempt named by `superseding`; never opens a transaction.
 */
export async function commitSnapshotAttempt(
  transaction: SenderDatabase,
  attempt: NewSnapshotAttempt,
  superseding: string | null,
): Promise<CommitAttemptResult> {
  if (attempt === null || typeof attempt !== "object") {
    throw new TypeError("Invalid snapshot attempt");
  }
  const { snapshotId, recordType, kind, installationId, changesToken, unreadSinceMs, begunAtMs } = attempt;
  if (typeof snapshotId !== "string" || UUID_V4.exec(snapshotId)?.[0] !== snapshotId) {
    throw new TypeError("Invalid snapshot UUID");
  }
  if (typeof recordType !== "string" || recordType.length > 64 || RECORD_TYPE.exec(recordType)?.[0] !== recordType) {
    throw new TypeError("Invalid record type");
  }
  if (kind !== "initial" && kind !== "reconciliation") {
    throw new TypeError("Invalid snapshot kind");
  }
  if (typeof installationId !== "string" || UUID.exec(installationId)?.[0] !== installationId) {
    throw new TypeError("Invalid installation UUID");
  }
  if (typeof changesToken !== "string" || changesToken.length === 0) {
    throw new TypeError("Invalid changes token");
  }
  if (!Number.isSafeInteger(unreadSinceMs) || !Number.isSafeInteger(begunAtMs)) {
    throw new TypeError("Invalid snapshot instant");
  }
  if (superseding !== null && (typeof superseding !== "string" || UUID.exec(superseding)?.[0] !== superseding)) {
    throw new TypeError("Invalid superseding UUID");
  }

  const current = await currentSnapshotAttempt(transaction, recordType);
  if (superseding === null) {
    if (current !== null) return { outcome: "refused", failure: "attempt-open" };
  } else if (current?.snapshotId !== superseding) {
    return { outcome: "refused", failure: "attempt-mismatch" };
  }

  if (current !== null) {
    await transaction.execute("UPDATE snapshot_attempts SET abandoned_at_ms = ? WHERE snapshot_id = ?", [begunAtMs, current.snapshotId]);
  }
  await transaction.execute(
    `INSERT INTO snapshot_attempts
      (snapshot_id, record_type, kind, installation_id, changes_token, unread_since_ms, begun_at_ms, abandoned_at_ms)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
    [snapshotId, recordType, kind, installationId, changesToken, unreadSinceMs, begunAtMs, null],
  );
  await transaction.execute(
    `INSERT INTO current_snapshot_attempts (record_type, snapshot_id) VALUES (?, ?)
      ON CONFLICT (record_type) DO UPDATE SET snapshot_id = excluded.snapshot_id`,
    [recordType, snapshotId],
  );
  return {
    outcome: "committed",
    attempt: { snapshotId, recordType, kind, installationId, changesToken, unreadSinceMs, begunAtMs, abandonedAtMs: null },
  };
}
