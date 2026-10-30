import type { InstallationCipher } from "../installation-cipher.ts";
import { chainStart, partitionPage, ProtocolRefusal } from "@activity-tracker-portal/ingest-protocol";
import { currentSnapshotAttempt, snapshotAttempt } from "../snapshot/begin-initial-snapshot.ts";
import type { SnapshotAttempt } from "../snapshot/begin-initial-snapshot.ts";
import type { DatabaseRow, SenderDatabase } from "../storage/database.ts";
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

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const RECORD_TYPE = /^[a-z][a-z0-9]*(-[a-z0-9]+)*$/;
const OUTBOX_KINDS: ReadonlySet<string> = new Set([
  "capability-acknowledgement", "ingest-batch", "snapshot-completion", "change-completion", "sync-checkpoint",
]);

/** Sender schema version 2: the Sender Outbox, its high-water marks, and cycle progress. */
export const outboxMigration: Migration = Object.freeze({
  version: 2,
  async apply(transaction: SenderDatabase): Promise<void> {
    await transaction.execute(`CREATE TABLE outbox_messages (
      installation_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK (sequence > 0),
      kind TEXT NOT NULL CHECK (kind IN ('capability-acknowledgement', 'ingest-batch', 'snapshot-completion', 'change-completion', 'sync-checkpoint')),
      sealed BLOB NOT NULL, message_bytes INTEGER NOT NULL CHECK (message_bytes > 0),
      PRIMARY KEY (installation_id, sequence)) STRICT`);
    await transaction.execute(`CREATE TABLE outbox_high_water (
      installation_id TEXT PRIMARY KEY NOT NULL, last_sequence INTEGER NOT NULL CHECK (last_sequence > 0)) STRICT`);
    await transaction.execute(`CREATE TABLE cycle_progress (
      cycle_id TEXT PRIMARY KEY NOT NULL, record_type TEXT NOT NULL,
      cycle_kind TEXT NOT NULL CHECK (cycle_kind IN ('initial-snapshot', 'reconciliation-snapshot', 'change-cycle')),
      snapshot_id TEXT NOT NULL, lineage INTEGER NOT NULL CHECK (lineage >= 0), installation_id TEXT NOT NULL,
      pages_staged INTEGER NOT NULL CHECK (pages_staged > 0), fragment_count INTEGER NOT NULL CHECK (fragment_count > 0),
      record_count INTEGER NOT NULL CHECK (record_count >= 0), chain TEXT NOT NULL,
      position BLOB, changes_token TEXT, unread_since_ms INTEGER, finished INTEGER NOT NULL CHECK (finished IN (0, 1)),
      UNIQUE (snapshot_id, lineage),
      FOREIGN KEY (snapshot_id, record_type) REFERENCES snapshot_attempts (snapshot_id, record_type),
      CHECK ((cycle_kind = 'change-cycle') = (lineage > 0)),
      CHECK ((lineage = 0) = (cycle_id = snapshot_id)),
      CHECK (CASE WHEN cycle_kind = 'change-cycle'
        THEN position IS NULL AND changes_token IS NOT NULL AND unread_since_ms IS NOT NULL
        ELSE position IS NOT NULL AND changes_token IS NULL AND unread_since_ms IS NULL END)) STRICT`);
  },
});

/** The authenticated data that binds a sealed Outbox Message to its installation and sequence. */
export function outboxAad(installationId: string, sequence: number): Uint8Array {
  checkUuid(installationId);
  if (!positive(sequence)) throw new TypeError("Invalid Outbox sequence");
  // The prefix, validated UUID, and decimal integer are ASCII, hence already UTF-8.
  return Uint8Array.from(`activity-tracker-outbox-v1:${installationId}:${sequence}`, (char) => char.charCodeAt(0));
}

/** The installation's sequence high-water mark, or 0 before its first Outbox Message. */
export async function lastOutboxSequence(database: SenderDatabase, installationId: string): Promise<number> {
  checkUuid(installationId);
  const [row] = await database.query("SELECT last_sequence FROM outbox_high_water WHERE installation_id = ?", [installationId]);
  return row ? row.last_sequence as number : 0;
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
  checkUuid(installationId);
  if (!Number.isSafeInteger(expectedLastSequence) || expectedLastSequence < 0 || !Array.isArray(messages) || messages.length === 0) {
    throw new TypeError("Invalid Outbox append");
  }
  for (const message of messages) {
    if (!object(message) || !OUTBOX_KINDS.has(message.kind as string) || !nonemptyBytes(message.sealed) || !positive(message.messageBytes)) {
      throw new TypeError("Invalid Outbox message");
    }
  }
  if (await lastOutboxSequence(transaction, installationId) !== expectedLastSequence) return false;
  for (const [index, message] of messages.entries()) {
    await transaction.execute(
      "INSERT INTO outbox_messages (installation_id, sequence, kind, sealed, message_bytes) VALUES (?, ?, ?, ?, ?)",
      [installationId, expectedLastSequence + index + 1, message.kind, message.sealed, message.messageBytes],
    );
  }
  await transaction.execute(
    `INSERT INTO outbox_high_water (installation_id, last_sequence) VALUES (?, ?)
      ON CONFLICT (installation_id) DO UPDATE SET last_sequence = excluded.last_sequence`,
    [installationId, expectedLastSequence + messages.length],
  );
  return true;
}

/** Every Outbox row, ordered by installation and sequence. */
export async function outboxMessages(database: SenderDatabase): Promise<OutboxRow[]> {
  return (await database.query("SELECT * FROM outbox_messages ORDER BY installation_id, sequence")).map((row) => ({
    installationId: row.installation_id as string, sequence: row.sequence as number,
    kind: row.kind as OutboxKind, sealed: row.sealed as Uint8Array, messageBytes: row.message_bytes as number,
  }));
}

export async function cycleProgress(database: SenderDatabase, cycleId: string): Promise<CycleProgress | null> {
  const row = await progressRow(database, cycleId);
  return row ? progressFromRow(row) : null;
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
  const attempt = await currentSnapshotAttempt(database, recordType);
  if (!attempt) return null;
  const [latest] = await database.query(
    "SELECT * FROM cycle_progress WHERE snapshot_id = ? AND lineage > 0 AND finished = 1 ORDER BY lineage DESC LIMIT 1",
    [attempt.snapshotId],
  );
  return {
    snapshotId: attempt.snapshotId,
    changesToken: latest ? latest.changes_token as string : attempt.changesToken,
    unreadSinceMs: latest ? latest.unread_since_ms as number : attempt.unreadSinceMs,
  };
}

/** Stage one observed page: partition, seal, and commit with the cycle's progress, or nothing. */
export async function stageOutboxBatch(
  database: SenderDatabase,
  cipher: InstallationCipher,
  context: StagingContext,
  page: ObservedPage,
): Promise<StageResult> {
  validateInputs(context, page);
  const schemas = context.generation.projectionSchemas;
  if (!Object.prototype.hasOwnProperty.call(schemas, page.recordType) || !schemas[page.recordType].includes(page.projectionSchemaVersion)) {
    return { outcome: "refused", failure: "projection-schema-unsupported" };
  }

  let state: StagingState;
  try {
    state = await readState(database, context, page);
  } catch {
    return { outcome: "storage-failed", failure: "storage-failed" };
  }
  const failure = cycleRefusal(state, context, page);
  if (failure) return { outcome: "refused", failure };
  // The cycle checks establish a current attempt and compatible saved progress.
  const attempt = state.attempt!;
  const previous = state.cycle ? progressFromRow(state.cycle) : null;
  const cycleKind: CycleKind = page.source === "change" ? "change-cycle"
    : attempt.kind === "initial" ? "initial-snapshot" : "reconciliation-snapshot";
  const lineage = state.cycle ? state.cycle.lineage as number
    : page.source === "snapshot" ? 0 : (state.latest?.lineage as number | undefined ?? 0) + 1;
  const items = page.source === "snapshot"
    ? page.projections.map((bytes) => ({ kind: "projection", data: base64(bytes) }))
    : page.items.map((item) => item.kind === "retraction"
      ? { kind: "retraction", source_identity: item.sourceIdentity }
      : { kind: "projection", data: base64(item.bytes) });
  let partition: ReturnType<typeof partitionPage>;
  try {
    partition = partitionPage({
      sender_installation: context.installationId, capability_generation: context.generation.generation,
      record_type: page.recordType, projection_schema_version: page.projectionSchemaVersion,
      cycle_kind: cycleKind, cycle_id: page.cycleId, source_page_index: page.pageIndex,
      first_sequence: state.lastSequence + 1, previous_chain: previous?.chain ?? chainStart(page.cycleId), items,
      max_record_count: context.generation.maxRecordCount, max_message_bytes: context.generation.maxMessageBytes,
    });
  } catch (error) {
    if (error instanceof ProtocolRefusal && error.failure === "indivisible-record-limit-exceeded") {
      return { outcome: "blocked", failure: "indivisible-record-limit-exceeded" };
    }
    throw error;
  }
  const progress: CycleProgress = {
    cycleId: page.cycleId, recordType: page.recordType, cycleKind, snapshotId: attempt.snapshotId,
    installationId: context.installationId, pagesStaged: page.pageIndex,
    fragmentCount: (previous?.fragmentCount ?? 0) + partition.fragments.length,
    recordCount: (previous?.recordCount ?? 0) + items.length, chain: partition.next_chain,
    position: page.source === "snapshot" ? new Uint8Array(page.nextPosition) : null,
    changesToken: page.source === "change" ? page.nextChangesToken : null,
    unreadSinceMs: page.source === "change" ? page.nextUnreadSinceMs : null,
    finished: page.source === "snapshot" ? page.exhausted : !page.hasMore,
  };
  const messages: OutboxMessage[] = [];
  try {
    for (const [index, bytes] of partition.fragments.entries()) {
      const result = await cipher.seal(context.keyAlias, bytes, outboxAad(context.installationId, state.lastSequence + index + 1));
      if (object(result) && result.status === "ok" && nonemptyBytes(result.value)) {
        messages.push({ kind: "ingest-batch", sealed: new Uint8Array(result.value), messageBytes: bytes.length });
      } else {
        let failure = "unexpected-cipher-failure";
        if (object(result) && result.status === "error") {
          if (result.reason === "missing-key") failure = "installation-key-missing";
          if (result.reason === "unusable-key") failure = "installation-key-unusable";
          if (result.reason === "keystore-unavailable") failure = "keystore-unavailable";
        }
        return { outcome: "cipher-failed", failure };
      }
    }
  } catch {
    return { outcome: "cipher-failed", failure: "unexpected-cipher-failure" };
  }

  try {
    return await database.transaction<StageResult>(async (transaction) => {
      // All reads, including new-cycle dependencies, repeat on the transaction handle.
      const current = await readState(transaction, context, page);
      if (!sameState(state, current) || !await appendOutboxMessages(transaction, context.installationId, state.lastSequence, messages)) {
        return { outcome: "conflict", failure: "staging-conflict" };
      }
      await transaction.execute(
        `INSERT INTO cycle_progress
          (cycle_id, record_type, cycle_kind, snapshot_id, lineage, installation_id,
           pages_staged, fragment_count, record_count, chain, position, changes_token, unread_since_ms, finished)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT (cycle_id) DO UPDATE SET
            pages_staged = excluded.pages_staged, fragment_count = excluded.fragment_count,
            record_count = excluded.record_count, chain = excluded.chain, position = excluded.position,
            changes_token = excluded.changes_token, unread_since_ms = excluded.unread_since_ms, finished = excluded.finished`,
        [progress.cycleId, progress.recordType, progress.cycleKind, progress.snapshotId, lineage, progress.installationId,
          progress.pagesStaged, progress.fragmentCount, progress.recordCount, progress.chain,
          progress.position, progress.changesToken, progress.unreadSinceMs, progress.finished ? 1 : 0],
      );
      return { outcome: "staged", progress, firstSequence: state.lastSequence + 1, lastSequence: state.lastSequence + messages.length };
    });
  } catch {
    return { outcome: "storage-failed", failure: "storage-failed" };
  }
}


interface StagingState {
  readonly attempt: SnapshotAttempt | null;
  readonly namedAttempt: SnapshotAttempt | null;
  readonly cycle: DatabaseRow | null;
  readonly snapshot: DatabaseRow | null;
  readonly latest: DatabaseRow | null;
  readonly lastSequence: number;
}

async function progressRow(database: SenderDatabase, cycleId: string): Promise<DatabaseRow | null> {
  const [row] = await database.query("SELECT * FROM cycle_progress WHERE cycle_id = ?", [cycleId]);
  return row ?? null;
}

async function readState(database: SenderDatabase, context: StagingContext, page: ObservedPage): Promise<StagingState> {
  const attempt = await currentSnapshotAttempt(database, page.recordType);
  const cycle = await progressRow(database, page.cycleId);
  const namedAttempt = page.source === "change" ? await snapshotAttempt(database, page.cycleId) : null;
  let snapshot: DatabaseRow | null = null;
  let latest: DatabaseRow | null = null;
  if (page.source === "change" && !cycle && attempt) {
    snapshot = await progressRow(database, attempt.snapshotId);
    const [row] = await database.query(
      "SELECT * FROM cycle_progress WHERE snapshot_id = ? AND lineage > 0 ORDER BY lineage DESC LIMIT 1", [attempt.snapshotId],
    );
    latest = row ?? null;
  }
  return { attempt, namedAttempt, cycle, snapshot, latest, lastSequence: await lastOutboxSequence(database, context.installationId) };
}

function cycleRefusal(state: StagingState, context: StagingContext, page: ObservedPage): string | null {
  const { attempt, cycle, snapshot, latest } = state;
  if (!attempt || attempt.abandonedAtMs !== null) return "cycle-not-current";
  if (page.source === "snapshot") {
    if (page.cycleId !== attempt.snapshotId || (cycle && (cycle.record_type !== page.recordType || cycle.cycle_kind === "change-cycle"))) {
      return "cycle-not-current";
    }
  } else if (state.namedAttempt || (cycle && (cycle.record_type !== page.recordType || cycle.cycle_kind !== "change-cycle" || cycle.snapshot_id !== attempt.snapshotId))) {
    return "cycle-not-current";
  }
  if (attempt.installationId !== context.installationId || (cycle && cycle.installation_id !== context.installationId)) return "installation-mismatch";
  if (cycle?.finished === 1) return "cycle-finished";
  if (page.source === "change" && !cycle) {
    if (snapshot?.finished !== 1) return "snapshot-unfinished";
    if (latest && latest.finished !== 1) return "change-cycle-open";
  }
  if (page.pageIndex !== (cycle ? cycle.pages_staged as number : 0) + 1) return "page-position-mismatch";
  if (page.source === "snapshot") {
    if (!sameBytes(page.readFrom, cycle ? cycle.position as Uint8Array : null)) return "page-position-mismatch";
  } else if (page.readFrom !== (cycle?.changes_token ?? latest?.changes_token ?? attempt.changesToken)) {
    return "page-position-mismatch";
  }
  return null;
}

function sameBytes(left: Uint8Array | null, right: Uint8Array | null): boolean {
  return left === null || right === null ? left === right
    : left.length === right.length && left.every((byte, index) => byte === right[index]);
}

function sameFields(left: object | null, right: object | null): boolean {
  if (left === null || right === null) return left === right;
  const a = left as Record<string, unknown>;
  const b = right as Record<string, unknown>;
  return Object.keys(a).length === Object.keys(b).length && Object.keys(a).every((key) => {
    const value = a[key];
    return value instanceof Uint8Array ? b[key] instanceof Uint8Array && sameBytes(value, b[key]) : value === b[key];
  });
}

function sameState(left: StagingState, right: StagingState): boolean {
  return left.lastSequence === right.lastSequence && sameFields(left.attempt, right.attempt) &&
    sameFields(left.namedAttempt, right.namedAttempt) && sameFields(left.cycle, right.cycle) &&
    sameFields(left.snapshot, right.snapshot) && sameFields(left.latest, right.latest);
}

function progressFromRow(row: DatabaseRow): CycleProgress {
  return {
    cycleId: row.cycle_id as string, recordType: row.record_type as string, cycleKind: row.cycle_kind as CycleKind,
    snapshotId: row.snapshot_id as string, installationId: row.installation_id as string,
    pagesStaged: row.pages_staged as number, fragmentCount: row.fragment_count as number, recordCount: row.record_count as number,
    chain: row.chain as string, position: row.position as Uint8Array | null, changesToken: row.changes_token as string | null,
    unreadSinceMs: row.unread_since_ms as number | null, finished: row.finished === 1,
  };
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object";
}

function positive(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function nonemptyBytes(value: unknown): value is Uint8Array {
  return value instanceof Uint8Array && value.length > 0;
}

function checkUuid(value: unknown): void {
  if (typeof value !== "string" || UUID.exec(value)?.[0] !== value) throw new TypeError("Invalid UUID");
}

function checkRecordType(value: unknown): void {
  if (typeof value !== "string" || value.length > 64 || RECORD_TYPE.exec(value)?.[0] !== value) throw new TypeError("Invalid record type");
}

function validateInputs(context: StagingContext, page: ObservedPage): void {
  if (!object(context) || !object(page)) throw new TypeError("Invalid staging input");
  checkUuid(context.installationId);
  checkUuid(page.cycleId);
  checkRecordType(page.recordType);
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
  if (!positive(page.projectionSchemaVersion) || !positive(page.pageIndex)) throw new TypeError("Invalid page numbering");
  if (page.source === "snapshot") {
    if (!(page.readFrom === null || page.readFrom instanceof Uint8Array) || !(page.nextPosition instanceof Uint8Array) ||
        typeof page.exhausted !== "boolean" || !Array.isArray(page.projections)) throw new TypeError("Invalid snapshot page");
    for (const projection of page.projections) if (!nonemptyBytes(projection)) throw new TypeError("Invalid projection");
  } else if (page.source === "change") {
    if (typeof page.readFrom !== "string" || page.readFrom.length === 0 || typeof page.nextChangesToken !== "string" ||
        page.nextChangesToken.length === 0 || !Number.isSafeInteger(page.nextUnreadSinceMs) ||
        typeof page.hasMore !== "boolean" || !Array.isArray(page.items)) throw new TypeError("Invalid change page");
    for (const item of page.items) {
      if (!object(item)) throw new TypeError("Invalid change item");
      if (item.kind === "projection") {
        if (!nonemptyBytes(item.bytes)) throw new TypeError("Invalid projection");
      } else if (item.kind === "retraction") {
        if (typeof item.sourceIdentity !== "string") throw new TypeError("Invalid Source Identity");
        let length = 0;
        for (const char of item.sourceIdentity) {
          const codePoint = char.codePointAt(0)!;
          // Iteration combines valid pairs; surrogate code points here are unpaired.
          if (codePoint < 0x20 || (codePoint >= 0xd800 && codePoint <= 0xdfff)) {
            throw new TypeError("Invalid Source Identity");
          }
          length++;
        }
        if (length < 1 || length > 256) throw new TypeError("Invalid Source Identity");
      } else throw new TypeError("Invalid change item kind");
    }
  } else throw new TypeError("Invalid page source");
}

function base64(bytes: Uint8Array): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  let encoded = "";
  for (let index = 0; index < bytes.length; index += 3) {
    const a = bytes[index];
    const b = bytes[index + 1] ?? 0;
    const c = bytes[index + 2] ?? 0;
    encoded += alphabet[a >>> 2] + alphabet[((a & 3) << 4) | (b >>> 4)] +
      (index + 1 < bytes.length ? alphabet[((b & 15) << 2) | (c >>> 6)] : "=") +
      (index + 2 < bytes.length ? alphabet[c & 63] : "=");
  }
  return encoded;
}

// stage-outbox-batch@2 Interface stubs (packs/stage-outbox-batch-2.spec.md). They declare the
// additive interface only; the implementation replaces them after freeze.

/** One installation's queued Outbox Messages, counted without loading any sealed message. */
export interface OutboxBacklog {
  readonly messageCount: number;
  /** The sum of the queued messages' plaintext byte lengths. */
  readonly byteCount: number;
}

export type AdvanceResult =
  | { readonly outcome: "advanced"; readonly origin: ChangeOrigin }
  | { readonly outcome: "refused" | "storage-failed"; readonly failure: string };

/** The installation's queued message count and byte total, without loading sealed bytes (R14). */
export async function outboxBacklog(_database: SenderDatabase, _installationId: string): Promise<OutboxBacklog> {
  throw new Error("stage-outbox-batch@2 is not implemented");
}

/** Whether the installation's message with this sequence is still queued, without loading sealed bytes (R15). */
export async function outboxHolds(_database: SenderDatabase, _installationId: string, _sequence: number): Promise<boolean> {
  throw new Error("stage-outbox-batch@2 is not implemented");
}

/** Advance the record type's change origin past a quiet change read, staging nothing (R16, R17). */
export async function advanceChangeOrigin(
  _database: SenderDatabase,
  _context: StagingContext,
  _page: ChangePage,
): Promise<AdvanceResult> {
  throw new Error("stage-outbox-batch@2 is not implemented");
}

/** Sender schema version 10: quiet change-origin advances (R18). */
export const changeOriginMigration: Migration = Object.freeze({
  version: 10,
  async apply(_transaction: SenderDatabase): Promise<void> {
    throw new Error("stage-outbox-batch@2 is not implemented");
  },
});
