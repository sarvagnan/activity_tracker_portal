import type { InstallationCipher } from "../installation-cipher.ts";
import type { ChangeItem, StagingContext } from "../outbox/stage-outbox-batch.ts";
import type { BackgroundScheduling, CheckpointTrigger, RunSummary } from "../outbox/stage-sync-checkpoint.ts";
import type { ChangesTokenIssuer } from "../snapshot/begin-initial-snapshot.ts";
import type { SenderDatabase } from "../storage/database.ts";
import { messageSha256 } from "@activity-tracker-portal/ingest-protocol";
import { beginInitialSnapshot, currentSnapshotAttempt } from "../snapshot/begin-initial-snapshot.ts";
import type { SnapshotAttempt } from "../snapshot/begin-initial-snapshot.ts";
import { beginReconciliationSnapshot } from "../snapshot/begin-reconciliation-snapshot.ts";
import { stageOutboxBatch, cycleProgress, changeOrigin, advanceChangeOrigin } from "../outbox/stage-outbox-batch.ts";
import type { CycleProgress, ObservedPage } from "../outbox/stage-outbox-batch.ts";
import { stageSyncCompletion, cycleCompletion } from "../outbox/stage-sync-completion.ts";
import { stageSyncCheckpoint, runCheckpoint } from "../outbox/stage-sync-checkpoint.ts";
import type { OutcomeReason, RecordTypeOutcome } from "../outbox/stage-sync-checkpoint.ts";
import { storageLane, storageDatabase, serializedDatabase } from "./sync-storage.ts";
import { checkOptions, checkContext, checkConditions, checkTrigger, checkInstant, validateRead, utf8 } from "./sync-validation.ts";
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
  /**
   * coordinate-sender-sync@3: advance quiet change reads through stage-outbox-batch@2 instead of staging them.
   * Defaults to false, which keeps @2's behaviour. Interface stub: not yet implemented.
   */
  readonly quietChangeReads?: boolean;
}

export interface TriggerRequest {
  readonly kind: CheckpointTrigger;
  /** The soft execution budget in milliseconds; none when omitted. */
  readonly budgetMs?: number;
  /** An absolute soft deadline on `monotonicNow()`; none when omitted (coordinate-sender-sync@2). */
  readonly deadlineMs?: number;
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

/** Sender schema version 5: this unit's run, ring, cycle, and block records. */
export const syncMigration: Migration = Object.freeze({
  version: 5,
  async apply(transaction: SenderDatabase): Promise<void> {
    await transaction.execute(`CREATE TABLE sync_types (
      record_type TEXT PRIMARY KEY NOT NULL, ring_order INTEGER NOT NULL UNIQUE CHECK (ring_order > 0),
      change_cycle_id TEXT, change_snapshot_id TEXT, established INTEGER NOT NULL CHECK (established IN (0, 1)),
      CHECK ((change_cycle_id IS NULL) = (change_snapshot_id IS NULL))) STRICT`);
    await transaction.execute(`CREATE TABLE sync_blocks (
      record_type TEXT PRIMARY KEY NOT NULL REFERENCES sync_types (record_type),
      cause TEXT NOT NULL CHECK (cause IN ('projection-refusal', 'record-too-large')),
      identity TEXT NOT NULL, failure TEXT NOT NULL, path TEXT,
      retry_requested INTEGER NOT NULL CHECK (retry_requested IN (0, 1)),
      CHECK ((cause = 'projection-refusal') = (path IS NOT NULL)),
      CHECK (cause = 'projection-refusal' OR retry_requested = 0)) STRICT`);
    await transaction.execute(`CREATE TABLE sync_runs (
      installation_id TEXT NOT NULL, run_sequence INTEGER NOT NULL CHECK (run_sequence > 0),
      trigger TEXT NOT NULL CHECK (trigger IN ('background', 'foreground', 'manual')),
      started_at_ms INTEGER NOT NULL,
      background_scheduling TEXT NOT NULL CHECK (background_scheduling IN ('available', 'restricted', 'unregistered')),
      PRIMARY KEY (installation_id, run_sequence)) STRICT`);
    await transaction.execute(`CREATE TABLE sync_run_types (
      installation_id TEXT NOT NULL, run_sequence INTEGER NOT NULL, record_type TEXT NOT NULL,
      PRIMARY KEY (installation_id, run_sequence, record_type),
      FOREIGN KEY (installation_id, run_sequence) REFERENCES sync_runs (installation_id, run_sequence)) STRICT`);
    await transaction.execute(`CREATE TABLE sync_run_cycles (
      installation_id TEXT NOT NULL, run_sequence INTEGER NOT NULL, record_type TEXT NOT NULL, cycle_id TEXT NOT NULL,
      records_before INTEGER NOT NULL CHECK (records_before >= 0),
      PRIMARY KEY (installation_id, run_sequence, record_type, cycle_id),
      FOREIGN KEY (installation_id, run_sequence, record_type)
        REFERENCES sync_run_types (installation_id, run_sequence, record_type)) STRICT`);
  },
});

interface TypeRun {
  work: boolean;
  onlyEmptyChanges: boolean;
  caughtUp: boolean;
  stopped: boolean;
  expired: boolean;
  reason?: OutcomeReason;
}

interface SavedStep {
  attempt: SnapshotAttempt | null;
  established: boolean;
  source: "snapshot" | "change";
  cycleId: string | null;
  progress: CycleProgress | null;
  completion: boolean;
  position: Uint8Array | null;
  token: string | null;
  unreadSinceMs: number | null;
}

interface PendingRun {
  request: TriggerRequest;
  resolve: (result: RunResult) => void;
  reject: (error: unknown) => void;
  promise: Promise<RunResult>;
}

/** Create the Sender's one synchronization module. */
export function createSenderSync(options: SenderSyncOptions): SenderSync {
  checkOptions(options);
  const { cipher } = options;
  const database = storageDatabase(options.database);
  const lane = storageLane(options.database);
  // Begins release the lane during issuance. Staging holds it across its full
  // optimistic operation, so our own concurrent turns never compete to stage.
  const beginDatabase = serializedDatabase(options.database);
  const types = [...options.recordTypes].sort();
  const managed = new Set(types);
  const projection = { implementationVersion: options.projection.implementationVersion,
    schemaVersions: { ...options.projection.schemaVersions } };
  let active = false;
  let followUp: PendingRun | null = null;

  async function staged<T extends { outcome: string; failure?: string }>(
    call: () => Promise<T>,
    after?: (result: T) => Promise<void>,
  ): Promise<T> {
    return lane(async () => {
      let result = await call();
      for (let retry = 0; result.outcome === "conflict" && retry < 2; retry++) result = await call();
      await after?.(result);
      return result;
    });
  }

  function dependencyFailure(result: { outcome: string; failure?: string }): string | null {
    if (result.outcome === "refused") return "sync-state-mismatch";
    if (result.outcome === "conflict") return "staging-conflict";
    if (result.outcome === "storage-failed") return "storage-failed";
    if (result.outcome === "cipher-failed") return result.failure!;
    return null;
  }

  async function sourceData(installationId: string, runSequence: number, recordType: string): Promise<boolean> {
    const cycles = await database.query(
      "SELECT cycle_id, records_before FROM sync_run_cycles WHERE installation_id = ? AND run_sequence = ? AND record_type = ?",
      [installationId, runSequence, recordType]);
    for (const cycle of cycles) {
      const progress = await cycleProgress(database, cycle.cycle_id as string);
      if (progress && progress.recordCount > (cycle.records_before as number)) return true;
    }
    return false;
  }

  async function executeRun(request: TriggerRequest): Promise<RunResult> {
    let startedAtMs = options.now();
    checkInstant(startedAtMs);
    const context = await options.context();
    checkContext(context, types, projection.schemaVersions);
    const backgroundScheduling = await options.backgroundScheduling();
    if (!["available", "restricted", "unregistered"].includes(backgroundScheduling)) throw new TypeError("Invalid scheduling status");
    const installationId = context.installationId;
    let startMono = options.monotonicNow();
    let deadline = request.deadlineMs;
    let failure: string | null = null;
    let callerError: TypeError | null = null;
    const stopped = () => failure !== null || callerError !== null;
    const running = new Map<string, Promise<void>>();
    const states = new Map(types.map((type) => [type, {
      work: false, onlyEmptyChanges: true, caughtUp: false, stopped: false, expired: false,
    } as TypeRun]));

    function randomUUID(): string {
      try {
        const value = options.randomUUID();
        if (typeof value !== "string" ||
            /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.exec(value)?.[0] !== value) {
          throw new TypeError("Invalid sync identity");
        }
        return value;
      } catch (error) { if (error instanceof TypeError) callerError ??= error; throw error; }
    }

    async function stageInRun<T extends { outcome: string; failure?: string }>(
      call: () => Promise<T>, after?: (result: T) => Promise<void>,
    ): Promise<T> {
      return staged(call, async (result) => {
        // Publish a dependency failure before releasing the storage lane, so
        // an already-queued completion cannot start after the run has failed.
        failure ??= dependencyFailure(result);
        await after?.(result);
      });
    }

    function canStart(state: TypeRun, owedCompletion = false): boolean {
      if (stopped()) return false;
      if (!owedCompletion && deadline !== undefined && options.monotonicNow() >= deadline) {
        state.reason = "time-budget-reached";
        state.stopped = true;
        return false;
      }
      return true;
    }

    async function guard(state: TypeRun, owedCompletion = false): Promise<boolean> {
      try {
        if (!canStart(state, owedCompletion)) return false;
        const conditions = await options.conditions();
        checkConditions(conditions);
        if (!canStart(state, owedCompletion)) return false;
        const reason = !conditions.secureLock ? "screen-lock-removed" : conditions.storagePressure ? "storage-pressure"
          : conditions.deliveryBlocked ? "delivery-blocked" : !conditions.collectionEnabled ? "collection-disabled" : undefined;
        if (reason !== undefined) { state.reason = reason; state.stopped = true; return false; }
        return true;
      } catch (error) {
        // The begin units catch issuer errors; retain caller errors here so they
        // still stop the pool and reject the trigger after in-flight steps settle.
        if (error instanceof TypeError) callerError ??= error;
        throw error;
      }
    }

    async function savedStep(recordType: string): Promise<SavedStep> {
      return lane(async () => {
        const [row] = await database.query("SELECT * FROM sync_types WHERE record_type = ?", [recordType]);
        const attempt = await currentSnapshotAttempt(database, recordType);
        const base = { attempt, established: row.established === 1 };
        const snapshot = attempt ? await cycleProgress(database, attempt.snapshotId) : null;
        const snapshotCompleted = snapshot?.finished ? await cycleCompletion(database, snapshot.cycleId) : null;
        if (!attempt || !snapshot?.finished || !snapshotCompleted) {
          return { ...base, source: "snapshot", cycleId: attempt?.snapshotId ?? null, progress: snapshot,
            completion: snapshot?.finished ?? false, position: snapshot?.position ?? null, token: null, unreadSinceMs: null };
        }
        const cycleId = row.change_snapshot_id === attempt.snapshotId ? row.change_cycle_id as string | null : null;
        const progress = cycleId ? await cycleProgress(database, cycleId) : null;
        const completed = progress?.finished ? await cycleCompletion(database, progress.cycleId) : null;
        if (completed && attempt.kind === "initial" && !base.established) {
          await database.execute("UPDATE sync_types SET established = 1 WHERE record_type = ?", [recordType]);
          base.established = true;
        }
        const origin = await changeOrigin(database, recordType);
        return { ...base, source: "change", cycleId: completed ? null : cycleId, progress: completed ? null : progress,
          completion: Boolean(progress?.finished && !completed), position: null,
          token: completed ? origin!.changesToken : progress?.changesToken ?? origin!.changesToken,
          unreadSinceMs: completed ? origin!.unreadSinceMs : progress?.unreadSinceMs ?? origin!.unreadSinceMs };
      });
    }

    function identity(recordType: string, step: SavedStep, cause: BlockCause): string {
      const saved = step.source === "snapshot"
        ? [step.attempt?.snapshotId ?? null, step.position === null ? null : Array.from(step.position)]
        : [step.cycleId, step.token];
      return JSON.stringify([recordType, messageSha256(utf8(JSON.stringify(saved))), projection.schemaVersions[recordType],
        projection.implementationVersion, ...(cause === "record-too-large" ? [context.generation.generation] : [])]);
    }

    async function eligible(recordType: string, state: TypeRun): Promise<boolean> {
      if (!await guard(state)) return false;
      const block = await lane(async () => (await database.query("SELECT * FROM sync_blocks WHERE record_type = ?", [recordType]))[0]);
      if (block) {
        const step = await savedStep(recordType);
        if (block.retry_requested !== 1 && block.identity === identity(recordType, step, block.cause as BlockCause)) {
          state.stopped = true;
          state.reason = block.cause as BlockCause;
          return false;
        }
      }
      return !stopped();
    }

    async function block(recordType: string, state: TypeRun, step: SavedStep, cause: BlockCause, name: string, path: string | null) {
      await lane(() => database.execute(
        `INSERT INTO sync_blocks (record_type, cause, identity, failure, path, retry_requested) VALUES (?, ?, ?, ?, ?, 0)
          ON CONFLICT (record_type) DO UPDATE SET cause = excluded.cause, identity = excluded.identity,
            failure = excluded.failure, path = excluded.path, retry_requested = 0`,
        [recordType, cause, identity(recordType, step, cause), name, path]));
      state.stopped = true;
      state.reason = cause;
    }

    async function begin(recordType: string, state: TypeRun, step: SavedStep): Promise<void> {
      if (!await guard(state)) return;
      // The published begin units check storage before calling the issuer. Check
      // again at the actual source boundary, after any wait for that storage.
      const issuer: ChangesTokenIssuer = { issue: async (type) => {
        if (!await guard(state) || !canStart(state)) throw new Error("Source call paused");
        return options.sources.issue(type);
      } };
      const clocks = { randomUUID, now: () => {
        try { const now = options.now(); checkInstant(now); return now; }
        catch (error) { if (error instanceof TypeError) callerError ??= error; throw error; }
      } };
      const result = step.attempt && step.established
        ? await beginReconciliationSnapshot(beginDatabase, issuer,
          { recordType, installationId, superseding: step.attempt.snapshotId }, clocks)
        : await beginInitialSnapshot(beginDatabase, issuer,
          { recordType, installationId, ...(step.attempt ? { abandoning: step.attempt.snapshotId } : {}) }, clocks);
      if (callerError !== null) throw callerError;
      failure ??= dependencyFailure(result);
      if (result.outcome === "begun") {
        state.work = true;
        state.onlyEmptyChanges = false;
        await lane(() => database.execute(
          "UPDATE sync_types SET change_cycle_id = NULL, change_snapshot_id = NULL WHERE record_type = ?", [recordType]));
      } else if (failure === null && !state.stopped) {
        state.stopped = true;
        state.reason = result.outcome === "not-permitted" ? "permission-missing" : "read-failed";
      }
    }

    async function complete(recordType: string, state: TypeRun, step: SavedStep, owed = false): Promise<void> {
      if (!await guard(state, owed)) return;
      const result = await stageInRun(() => {
        if (stopped()) throw new Error("Run already failed");
        return stageSyncCompletion(database, cipher, context, { recordType, cycleId: step.cycleId! });
      });
      failure ??= dependencyFailure(result);
      if (result.outcome === "staged") {
        state.work = true;
        if (step.source === "change") {
          state.caughtUp = true;
          if (step.attempt!.kind === "initial") {
            await lane(() => database.execute("UPDATE sync_types SET established = 1 WHERE record_type = ?", [recordType]));
          }
        } else state.onlyEmptyChanges = false;
      }
    }

    async function turn(recordType: string): Promise<void> {
      const state = states.get(recordType)!;
      try {
        if (!await guard(state)) return;
        const step = await savedStep(recordType);
        if (!step.attempt) { await begin(recordType, state, step); return; }
        if (step.completion) { await complete(recordType, state, step); return; }
        if (!await guard(state)) return;
        if (step.source === "change" && step.cycleId === null) {
          step.cycleId = randomUUID();
          await lane(() => database.execute(
            "UPDATE sync_types SET change_cycle_id = ?, change_snapshot_id = ? WHERE record_type = ?",
            [step.cycleId, step.attempt!.snapshotId, recordType]));
        }
        await lane(() => database.transaction((tx) => tx.execute(
          `INSERT INTO sync_run_cycles (installation_id, run_sequence, record_type, cycle_id, records_before)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING`,
          [installationId, runSequence, recordType, step.cycleId, step.progress?.recordCount ?? 0])));
        if (!await guard(state)) return;
        let payload: unknown;
        // guard itself yields; check again in the source call's own turn.
        if (!canStart(state)) return;
        try {
          payload = step.source === "snapshot"
            ? await options.sources.readSnapshotPage({ recordType, snapshotId: step.attempt.snapshotId, position: step.position })
            : await options.sources.readChangePage({ recordType, cycleId: step.cycleId!, changesToken: step.token!, unreadSinceMs: step.unreadSinceMs! });
        } catch { payload = null; }
        const result = validateRead(payload, step.source, projection.schemaVersions[recordType]);
        if (result.outcome === "projection-refused") {
          await block(recordType, state, step, "projection-refusal", result.failure, result.path);
          return;
        }
        if (result.outcome === "invalid-position" || result.outcome === "token-expired") {
          if (result.outcome === "token-expired") state.expired = true;
          await begin(recordType, state, step);
          return;
        }
        if (result.outcome !== "page") {
          state.stopped = true;
          state.reason = result.outcome === "not-permitted" ? "permission-missing" : "read-failed";
          return;
        }
        const common = { recordType, cycleId: step.cycleId!, projectionSchemaVersion: projection.schemaVersions[recordType],
          pageIndex: (step.progress?.pagesStaged ?? 0) + 1 };
        const page: ObservedPage = "projections" in result
          ? { ...common, source: "snapshot", readFrom: step.position, projections: result.projections,
            nextPosition: result.nextPosition, exhausted: result.exhausted }
          : { ...common, source: "change", readFrom: step.token!, items: result.items, nextChangesToken: result.nextChangesToken,
            nextUnreadSinceMs: result.nextUnreadSinceMs, hasMore: result.hasMore };
        // Reading and staging are one step: an in-flight page still commits if
        // conditions, deadline, or another turn's failure change while it reads.
        async function afterPage(retireCycle = false): Promise<void> {
          // Keep the page commit and durable tail move in the same storage lane.
          // Other turns cannot sample an obsolete ring between these commits.
          await database.transaction(async (tx) => {
            const [tail] = await tx.query("SELECT MAX(ring_order) AS last FROM sync_types");
            await tx.execute("UPDATE sync_types SET ring_order = ? WHERE record_type = ?", [(tail.last as number) + 1, recordType]);
            await tx.execute("DELETE FROM sync_blocks WHERE record_type = ?", [recordType]);
            if (retireCycle) {
              await tx.execute("UPDATE sync_types SET change_cycle_id = NULL, change_snapshot_id = NULL WHERE record_type = ?", [recordType]);
            }
          });
        }
        if (options.quietChangeReads && page.source === "change" && step.progress === null &&
            page.pageIndex === 1 && page.items.length === 0 && !page.hasMore) {
          const advanced = await staged(() => advanceChangeOrigin(database, context, page), async (result) => {
            // Catch-up still owes a staged cycle. Publish every other failure
            // before releasing the lane, just as stageInRun does for staging.
            if (result.outcome === "refused" && result.failure === "catch-up-unstaged") return;
            failure ??= dependencyFailure(result);
            if (result.outcome === "advanced") await afterPage(true);
          });
          if (advanced.outcome === "advanced") {
            state.caughtUp = true;
            return;
          }
          if (advanced.outcome !== "refused" || advanced.failure !== "catch-up-unstaged") return;
        }
        const stagedPage = await stageInRun(() => stageOutboxBatch(database, cipher, context, page), async (result) => {
          if (result.outcome === "staged") await afterPage();
        });
        failure ??= dependencyFailure(stagedPage);
        if (stagedPage.outcome === "blocked") {
          await block(recordType, state, step, "record-too-large", stagedPage.failure, null);
          return;
        }
        if (stagedPage.outcome !== "staged") return;
        state.work = true;
        if (page.source === "snapshot" || page.items.length > 0) state.onlyEmptyChanges = false;
        if (stagedPage.progress.finished) {
          await complete(recordType, state, { ...step, progress: stagedPage.progress, completion: true }, true);
        }
      } catch (error) {
        if (error instanceof TypeError) callerError ??= error;
        else failure ??= "storage-failed";
      }
    }

    let runSequence: number;
    try {
      const runs = await lane(() => database.query("SELECT * FROM sync_runs WHERE installation_id = ? ORDER BY run_sequence", [installationId]));
      for (const run of runs) {
        const sequence = run.run_sequence as number;
        if (await lane(() => runCheckpoint(database, installationId, sequence))) continue;
        const recordTypes = await lane(async () => {
          const rows = await database.query("SELECT record_type FROM sync_run_types WHERE installation_id = ? AND run_sequence = ? ORDER BY record_type", [installationId, sequence]);
          const entries: RecordTypeOutcome[] = [];
          for (const row of rows) entries.push({ recordType: row.record_type as string, outcome: "interrupted",
            sourceDataStaged: await sourceData(installationId, sequence, row.record_type as string) });
          return entries;
        });
        const endedAtMs = options.now();
        checkInstant(endedAtMs);
        const summary: RunSummary = { runSequence: sequence, trigger: run.trigger as CheckpointTrigger,
          startedAtMs: run.started_at_ms as number, endedAtMs, backgroundScheduling: run.background_scheduling as BackgroundScheduling, recordTypes };
        const result = await staged(() => stageSyncCheckpoint(database, cipher, context, summary));
        const recoveryFailure = dependencyFailure(result);
        if (recoveryFailure) return { outcome: "failed", failure: recoveryFailure };
      }
      startedAtMs = options.now();
      checkInstant(startedAtMs);
      startMono = options.monotonicNow();
      // Budgets start at opening; the absolute deadline also counts all earlier waits.
      if (request.budgetMs !== undefined) deadline = Math.min(deadline ?? Infinity, startMono + request.budgetMs);
      runSequence = await lane(() => database.transaction(async (tx) => {
        const [lastRun] = await tx.query("SELECT MAX(run_sequence) AS last FROM sync_runs WHERE installation_id = ?", [installationId]);
        const sequence = (lastRun.last as number | null ?? 0) + 1;
        await tx.execute(`INSERT INTO sync_runs (installation_id, run_sequence, trigger, started_at_ms, background_scheduling)
          VALUES (?, ?, ?, ?, ?)`, [installationId, sequence, request.kind, startedAtMs, backgroundScheduling]);
        const [tail] = await tx.query("SELECT MAX(ring_order) AS last FROM sync_types");
        let order = tail.last as number | null ?? 0;
        for (const type of types) {
          await tx.execute(`INSERT INTO sync_types (record_type, ring_order, change_cycle_id, change_snapshot_id, established)
            VALUES (?, ?, NULL, NULL, 0) ON CONFLICT (record_type) DO NOTHING`, [type, ++order]);
          await tx.execute("INSERT INTO sync_run_types (installation_id, run_sequence, record_type) VALUES (?, ?, ?)", [installationId, sequence, type]);
        }
        return sequence;
      }));
      const priority = [...new Set(request.priority ?? [])];
      while (true) {
        while (!stopped() && running.size < (options.concurrency ?? 2)) {
          const ring = await lane(() => database.query("SELECT record_type FROM sync_types ORDER BY ring_order"));
          const candidates = [...priority, ...ring.map((row) => row.record_type as string)];
          let selected: string | undefined;
          for (const type of candidates) {
            const state = states.get(type);
            if (!state || state.stopped || state.caughtUp || running.has(type)) continue;
            const index = priority.indexOf(type);
            if (index >= 0) priority.splice(index, 1);
            if (await eligible(type, state)) { selected = type; break; }
          }
          if (selected === undefined || stopped()) break;
          const type = selected;
          const pending = turn(type).finally(() => { running.delete(type); });
          running.set(type, pending);
        }
        if (running.size === 0) break;
        await Promise.race(running.values());
      }
      if (callerError !== null) throw callerError;
      if (failure !== null) return { outcome: "failed", failure };
      const recordTypes = await lane(async () => {
        const entries: RecordTypeOutcome[] = [];
        for (const type of types) {
          const state = states.get(type)!;
          const data = await sourceData(installationId, runSequence, type);
          const common = { recordType: type, sourceDataStaged: data };
          if (state.caughtUp) entries.push({ ...common, outcome: state.onlyEmptyChanges ? "no-change" : "completed" });
          else if (state.work) entries.push({ ...common, outcome: "progressed", reason: state.expired ? "changes-token-expired" : state.reason });
          else if (state.reason === "permission-missing") entries.push({ ...common, outcome: "not-permitted" });
          else if (state.reason === "read-failed") entries.push({ ...common, outcome: "failed", reason: state.reason });
          else entries.push({ ...common, outcome: state.reason === "time-budget-reached" || state.reason === "collection-disabled" ? "deferred" : "blocked", reason: state.reason });
        }
        return entries;
      });
      const endedAtMs = options.now();
      checkInstant(endedAtMs);
      const elapsed = options.monotonicNow() - startMono;
      const summary: RunSummary = { runSequence, trigger: request.kind, startedAtMs, endedAtMs,
        ...(elapsed < 0 ? {} : { elapsedMs: Math.round(elapsed) }), backgroundScheduling, recordTypes };
      const result = await staged(() => stageSyncCheckpoint(database, cipher, context, summary));
      const checkpointFailure = dependencyFailure(result);
      return checkpointFailure ? { outcome: "failed", failure: checkpointFailure } : { outcome: "checkpointed", runSequence, summary };
    } catch (error) {
      failure ??= "storage-failed";
      await Promise.all(running.values());
      if (callerError !== null) throw callerError;
      if (error instanceof TypeError) throw error;
      return { outcome: "failed", failure };
    }
  }

  async function drain(request: TriggerRequest): Promise<RunResult> {
    try { return await executeRun(request); }
    finally {
      const next = followUp;
      followUp = null;
      if (next) void drain(next.request).then(next.resolve, next.reject);
      else active = false;
    }
  }

  async function trigger(request: TriggerRequest): Promise<RunResult> {
    checkTrigger(request, managed);
    checkInstant(options.now());
    const copy: TriggerRequest = { kind: request.kind, ...(request.budgetMs === undefined ? {} : { budgetMs: request.budgetMs }),
      ...(request.deadlineMs === undefined ? {} : { deadlineMs: request.deadlineMs }),
      ...(request.priority === undefined ? {} : { priority: [...new Set(request.priority)] }) };
    if (!active) { active = true; return drain(copy); }
    if (!followUp) {
      let resolve!: PendingRun["resolve"];
      let reject!: PendingRun["reject"];
      const promise = new Promise<RunResult>((yes, no) => { resolve = yes; reject = no; });
      followUp = { request: copy, resolve, reject, promise };
    } else {
      const previous = followUp.request;
      const rank = { background: 0, foreground: 1, manual: 2 };
      const unbounded = (previous.budgetMs === undefined && previous.deadlineMs === undefined) ||
        (copy.budgetMs === undefined && copy.deadlineMs === undefined);
      const budgetMs = copy.budgetMs ?? previous.budgetMs;
      const deadlineMs = Math.min(previous.deadlineMs ?? Infinity, copy.deadlineMs ?? Infinity);
      followUp.request = { kind: rank[copy.kind] > rank[previous.kind] ? copy.kind : previous.kind,
        ...(unbounded || budgetMs === undefined ? {} : { budgetMs }),
        ...(unbounded || deadlineMs === Infinity ? {} : { deadlineMs }),
        priority: [...new Set([...(previous.priority ?? []), ...(copy.priority ?? [])])] };
    }
    return followUp.promise;
  }

  return {
    trigger,
    async requestRetry(recordType): Promise<RetryResult> {
      if (!managed.has(recordType)) throw new TypeError("Unmanaged retry type");
      try {
        const marked = await lane(() => database.transaction(async (tx) => {
          const [block] = await tx.query("SELECT cause FROM sync_blocks WHERE record_type = ?", [recordType]);
          if (block?.cause !== "projection-refusal") return false;
          await tx.execute("UPDATE sync_blocks SET retry_requested = 1 WHERE record_type = ?", [recordType]);
          return true;
        }));
        if (!marked) return { outcome: "refused", failure: "retry-not-blocked" };
      } catch { return { outcome: "failed", failure: "storage-failed" }; }
      return trigger({ kind: "manual", priority: [recordType] });
    },
  };
}

export async function typeBlock(database: SenderDatabase, recordType: string): Promise<TypeBlock | null> {
  return storageLane(database)(async () => {
    const [row] = await database.query("SELECT * FROM sync_blocks WHERE record_type = ?", [recordType]);
    return row ? { recordType: row.record_type as string, cause: row.cause as BlockCause, failure: row.failure as string,
      path: row.path as string | null, retryRequested: row.retry_requested === 1 } : null;
  });
}
