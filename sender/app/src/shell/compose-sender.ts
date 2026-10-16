import type { InstallationCipher } from "../installation-cipher.ts";
import { createOutboxSender } from "../delivery/send-outbox-message.ts";
import type { OutboxSender } from "../delivery/send-outbox-message.ts";
import type { EnrollmentStatus, SenderEnrollment } from "../enrollment/manage-sender-enrollment.ts";
import type { SenderDatabase } from "../storage/database.ts";
import { migrate, senderMigrations } from "../storage/migrations.ts";
import { createSenderSync } from "../sync/coordinate-sender-sync.ts";
import type { RunResult, SenderSync, SyncConditions, SyncSources, TriggerRequest } from "../sync/coordinate-sender-sync.ts";
import { createBackgroundPolling } from "../scheduling/schedule-background-polling.ts";
import type { BackgroundPolling, SchedulingNotifier, SchedulingPlatform } from "../scheduling/schedule-background-polling.ts";
import type { BackgroundPollingProvider } from "../background/background-task-body.ts";

export const PROJECTION_IMPLEMENTATION_VERSION = "activity-tracker-sender/0.1.0";
// Reserve 512 MiB: the Outbox can hold a whole Initial Snapshot. ADR 0014
// stops polling before storage exhaustion endangers existing ciphertext.
export const STORAGE_PRESSURE_FREE_BYTES = 536_870_912;

type Status = Awaited<ReturnType<SenderEnrollment["status"]>>;
type EnrollmentReader = Pick<SenderEnrollment, "status">;
export type ShellRunResult = RunResult | { readonly outcome: "skipped" };

export interface ComposeSenderOptions {
  readonly openDatabase: () => Promise<SenderDatabase>;
  /** Construct the published enrollment observer on the same migrated handle. */
  readonly enrollment: (database: SenderDatabase) => EnrollmentReader;
  readonly cipher: InstallationCipher;
  readonly sources: SyncSources;
  readonly projectionSchemaVersions: () => Promise<Readonly<Record<string, number>>>;
  readonly availableStorageBytes: () => Promise<number>;
  readonly platform: SchedulingPlatform;
  readonly notifier: SchedulingNotifier;
  readonly now: () => number;
  readonly monotonicNow: () => number;
  readonly randomUUID: () => string;
}

export interface SenderShell {
  /** App start and every return to active: registration observation, then a foreground trigger. */
  foreground(): Promise<ShellRunResult>;
  manualSync(): Promise<ShellRunResult>;
  readonly backgroundPollingProvider: BackgroundPollingProvider;
}

function isStatus(status: Status): status is EnrollmentStatus {
  return status !== null && !("outcome" in status);
}
function collectionEnabled(status: Status): boolean {
  return isStatus(status) && status.phase === "enrolled" && status.confirmed === true;
}

/** Volatile observations; unknown lock, delivery, or free space always fails closed. */
export async function readSyncConditions(options: {
  readonly status: () => Promise<Status>;
  readonly cipher: Pick<InstallationCipher, "isDeviceSecure">;
  readonly headBlock: () => ReturnType<OutboxSender["headBlock"]>;
  readonly availableStorageBytes: () => Promise<number>;
}): Promise<SyncConditions> {
  let enabled = false;
  let secureLock = false;
  let deliveryBlocked = true;
  let storagePressure = true;
  try { enabled = collectionEnabled(await options.status()); } catch { /* Unreadable enrollment disables collection. */ }
  try { secureLock = await options.cipher.isDeviceSecure() === true; } catch { /* No secure-lock proof. */ }
  try { deliveryBlocked = await options.headBlock() !== null; } catch { /* No readable delivery state. */ }
  try {
    const bytes = await options.availableStorageBytes();
    storagePressure = !Number.isSafeInteger(bytes) || bytes < STORAGE_PRESSURE_FREE_BYTES;
  } catch { /* No free-space proof. */ }
  return { collectionEnabled: enabled, secureLock, deliveryBlocked, storagePressure };
}

export async function composeSender(options: ComposeSenderOptions): Promise<SenderShell> {
  const database = await options.openDatabase();
  let enrollment: EnrollmentReader;
  try {
    const migration = await migrate(database, senderMigrations);
    if (migration.status !== "ready") throw new Error("Sender database is frozen");
    enrollment = options.enrollment(database);
  } catch (error) {
    await database.close();
    throw error;
  }

  async function status(): Promise<Status> {
    try { return await enrollment.status(); }
    catch { return { outcome: "storage-failed" }; }
  }

  let coordinator: SenderSync | null = null;
  let coordinatorIdentity: string | null = null;
  let preparation: Promise<unknown> = Promise.resolve();
  const inFlight = new Set<Promise<RunResult>>();
  function startRun(request: TriggerRequest): Promise<RunResult> {
    if (!coordinator) throw new Error("Sender coordinator is unavailable");
    const result = coordinator.trigger(request);
    inFlight.add(result);
    void result.then(() => inFlight.delete(result), () => inFlight.delete(result));
    return result;
  }
  function identity(current: EnrollmentStatus): string {
    const context = current.stagingContext;
    if (!context) throw new Error("Sender staging context is unavailable");
    return JSON.stringify([context.installationId, context.keyAlias, context.generation]);
  }

  // Return the pending run in an envelope: preparation must never await it.
  function prepareAndStart<T>(kind: TriggerRequest["kind"], start: () => Promise<T>): Promise<
    { readonly pending: Promise<T> } | { readonly outcome: "skipped" } |
    { readonly outcome: "failed"; readonly failure: "storage-failed" }
  > {
    const ready = preparation.then(async () => {
      let current = await status();
      if (isStatus(current) && kind === "foreground") {
        await currentPolling().ensureRegistered();
        current = await status();
      }
      if (isStatus(current) && collectionEnabled(current) &&
          identity(current) !== coordinatorIdentity && inFlight.size > 0) {
        // Holding preparation bars every new trigger from the old instance.
        // Include merged callers: the active result may settle before its follow-up.
        await Promise.allSettled(inFlight);
        current = await status();
      }
      if (current && "outcome" in current) return { outcome: "failed", failure: "storage-failed" } as const;
      if (!collectionEnabled(current) || !isStatus(current)) return { outcome: "skipped" } as const;
      await prepare(current);
      return { pending: start() };
    });
    preparation = ready.catch(() => undefined);
    return ready;
  }

  // Construct lazily: registration is expected only with an active installation.
  let polling: BackgroundPolling | null = null;
  function currentPolling(): BackgroundPolling {
    polling ??= createBackgroundPolling({
      database, platform: options.platform, notifier: options.notifier, now: options.now,
      sync: { trigger: startRun },
    });
    return polling;
  }

  async function prepare(current: EnrollmentStatus): Promise<void> {
    const context = current.stagingContext;
    if (!context) throw new Error("Sender staging context is unavailable");
    const identity = JSON.stringify([context.installationId, context.keyAlias, context.generation]);
    if (coordinator && identity === coordinatorIdentity) return;
    const versions = await options.projectionSchemaVersions();
    const recordTypes = Object.keys(versions).filter((type) =>
      Object.hasOwn(context.generation.projectionSchemas, type) &&
      context.generation.projectionSchemas[type].includes(versions[type]),
    ).sort();
    const schemaVersions = Object.fromEntries(recordTypes.map((type) => [type, versions[type]]));
    coordinator = createSenderSync({
      database, cipher: options.cipher, sources: options.sources, recordTypes,
      projection: { implementationVersion: PROJECTION_IMPLEMENTATION_VERSION, schemaVersions },
      async context() {
        const latest = await status();
        if (!isStatus(latest) || !latest.stagingContext) throw new Error("Sender staging context is unavailable");
        if (!collectionEnabled(latest)) throw new Error("Sender collection is disabled");
        // A refresh between preparation and opening must not use the old managed types.
        if (JSON.stringify([latest.stagingContext.installationId, latest.stagingContext.keyAlias,
          latest.stagingContext.generation]) !== identity) throw new Error("Sender generation changed before run");
        return latest.stagingContext;
      },
      conditions: () => readSyncConditions({
        status, cipher: options.cipher, availableStorageBytes: options.availableStorageBytes,
        async headBlock() {
          const latest = await status();
          if (!isStatus(latest)) return { outcome: "storage-failed" };
          // Only headBlock is exposed here. Delivery composition belongs to 2rv.9.
          return createOutboxSender({
            database, cipher: options.cipher, context: latest.deliveryContext, now: options.now,
            transport: { async send() { throw new Error("Read-only delivery observer"); } },
            notifier: { async notify() { throw new Error("Read-only delivery observer"); } },
          }).headBlock();
        },
      }),
      backgroundScheduling: () => currentPolling().observe(),
      now: options.now, monotonicNow: options.monotonicNow, randomUUID: options.randomUUID,
    });
    coordinatorIdentity = identity;
  }

  async function trigger(kind: "foreground" | "manual"): Promise<ShellRunResult> {
    const ready = await prepareAndStart(kind, () => startRun({ kind }));
    return "pending" in ready ? ready.pending : ready;
  }

  // The provider is safe for headless execution and for a retained registration
  // after draining or unlinking. A skipped task must never stage a checkpoint.
  const backgroundPollingProvider: BackgroundPollingProvider = async () => ({
    ensureRegistered: () => currentPolling().ensureRegistered(),
    observe: () => currentPolling().observe(),
    condition: () => currentPolling().condition(),
    async runTask() {
      try {
        const ready = await prepareAndStart("background", () => currentPolling().runTask());
        return "pending" in ready ? await ready.pending : ready.outcome === "skipped" ? "success" : "failed";
      } catch { return "failed"; }
    },
  });

  return {
    foreground: () => trigger("foreground"),
    manualSync: () => trigger("manual"),
    backgroundPollingProvider,
  };
}
