import type { SenderDatabase } from "../storage/database.ts";
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

const NOT_IMPLEMENTED = "begin-initial-snapshot@1 is not implemented";

/** Sender schema version 1: this unit's snapshot-attempt tables. */
export const snapshotAttemptsMigration: Migration = Object.freeze({
  version: 1,
  apply(): void {
    throw new Error(NOT_IMPLEMENTED);
  },
});

/** Begin a record type's Initial Snapshot: token first, then one durable commit, or nothing. */
export async function beginInitialSnapshot(
  database: SenderDatabase,
  issuer: ChangesTokenIssuer,
  request: BeginRequest,
  options: BeginOptions,
): Promise<BeginResult> {
  throw new Error(NOT_IMPLEMENTED);
}

export async function currentSnapshotAttempt(database: SenderDatabase, recordType: string): Promise<SnapshotAttempt | null> {
  throw new Error(NOT_IMPLEMENTED);
}

export async function snapshotAttempt(database: SenderDatabase, snapshotId: string): Promise<SnapshotAttempt | null> {
  throw new Error(NOT_IMPLEMENTED);
}
