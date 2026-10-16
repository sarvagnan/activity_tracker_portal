import type { InstallationCipher } from "../installation-cipher.ts";
import { createOutboxSender, DELIVERY_TIMEOUT_MS } from "../delivery/send-outbox-message.ts";
import type { DeliveryNotifier, OutboxSender } from "../delivery/send-outbox-message.ts";
import { ENROLLMENT_TIMEOUT_MS } from "../enrollment/manage-sender-enrollment.ts";
import type { EnrollmentStatus, SenderEnrollment } from "../enrollment/manage-sender-enrollment.ts";
import type { SenderDatabase } from "../storage/database.ts";
import { migrate, senderMigrations } from "../storage/migrations.ts";
import { createSenderSync } from "../sync/coordinate-sender-sync.ts";
import type { RunResult, SenderSync, SyncConditions, SyncSources, TriggerRequest } from "../sync/coordinate-sender-sync.ts";
import { createBackgroundPolling, BACKGROUND_BUDGET_MS } from "../scheduling/schedule-background-polling.ts";
import type { BackgroundPolling, SchedulingNotifier, SchedulingPlatform } from "../scheduling/schedule-background-polling.ts";
import { createHttpTransport } from "../transport/http-transport.ts";
import type { HttpFetch, HttpTransport } from "../transport/http-transport.ts";
import type { BackgroundPollingProvider } from "../background/background-task-body.ts";

export const PROJECTION_IMPLEMENTATION_VERSION = "activity-tracker-sender/0.1.0";
// Reserve 512 MiB: the Outbox can hold a whole Initial Snapshot. ADR 0014
// stops polling before storage exhaustion endangers existing ciphertext.
export const STORAGE_PRESSURE_FREE_BYTES = 536_870_912;

type Status = Awaited<ReturnType<SenderEnrollment["status"]>>;
type DeliveryPass = "drained" | "stopped" | "terminal";
type ShellEnrollment = Pick<SenderEnrollment, "status" | "confirm" | "refreshCapabilities">;

export const MAX_DELIVERY_ATTEMPTS = 64;
// Leave time for receipt bookkeeping after the unit's full network timeout.
export const DELIVERY_FINISH_RESERVE_MS = 1_000;
export type ShellRunResult = RunResult | { readonly outcome: "skipped" };

export interface ComposeSenderOptions {
  readonly openDatabase: () => Promise<SenderDatabase>;
  /** The production boundary constructs the published enrollment on this migrated handle. */
  readonly enrollment: (database: SenderDatabase, transport: HttpTransport) => ShellEnrollment;
  readonly fetch: HttpFetch;
  readonly deliveryNotifier: DeliveryNotifier;
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
  readonly headBlock: (current: Status) => ReturnType<OutboxSender["headBlock"]>;
  readonly availableStorageBytes: () => Promise<number>;
}): Promise<SyncConditions> {
  let current: Status = null;
  let enabled = false;
  let secureLock = false;
  let deliveryBlocked = true;
  let storagePressure = true;
  try { current = await options.status(); enabled = collectionEnabled(current); } catch { /* Unreadable enrollment disables collection. */ }
  try { secureLock = await options.cipher.isDeviceSecure() === true; } catch { /* No secure-lock proof. */ }
  try { deliveryBlocked = await options.headBlock(current) !== null; } catch { /* No readable delivery state. */ }
  try {
    const bytes = await options.availableStorageBytes();
    storagePressure = !Number.isSafeInteger(bytes) || bytes < STORAGE_PRESSURE_FREE_BYTES;
  } catch { /* No free-space proof. */ }
  return { collectionEnabled: enabled, secureLock, deliveryBlocked, storagePressure };
}

export async function composeSender(options: ComposeSenderOptions): Promise<SenderShell> {
  const transport = createHttpTransport({ fetch: options.fetch });
  const database = await options.openDatabase();
  let enrollment: ShellEnrollment;
  try {
    const migration = await migrate(database, senderMigrations);
    if (migration.status !== "ready") throw new Error("Sender database is frozen");
    enrollment = options.enrollment(database, transport);
  } catch (error) {
    await database.close();
    throw error;
  }

  async function status(): Promise<Status> {
    try { return await enrollment.status(); }
    catch { return { outcome: "storage-failed" }; }
  }

  let sender: OutboxSender | null = null;
  let deliveryIdentity: string | null = null;
  function currentSender(current: EnrollmentStatus): OutboxSender {
    const context = current.deliveryContext;
    const identity = JSON.stringify([context.installationId, context.keyAlias, context.portalUrl]);
    if (!sender || identity !== deliveryIdentity) {
      sender = createOutboxSender({
        database, cipher: options.cipher, context: { ...context },
        transport, notifier: options.deliveryNotifier, now: options.now,
      });
      deliveryIdentity = identity;
    }
    return sender;
  }

  let delivery: Promise<DeliveryPass> | null = null;
  let deliveryDeadline = Infinity;
  let pendingDelivery: { deadline: number; promise: Promise<DeliveryPass>; resolve: (result: DeliveryPass) => void } | null = null;
  function hasTime(deadline: number, timeout: number): boolean {
    return options.monotonicNow() + timeout + DELIVERY_FINISH_RESERVE_MS <= deadline;
  }
  function deliver(deadline = Infinity): Promise<DeliveryPass> {
    // Joining callers retain one subsequent pass, including after empty,
    // retryable and blocked observations or a delivery-context replacement.
    if (delivery) {
      deliveryDeadline = Math.min(deliveryDeadline, deadline);
      if (!pendingDelivery) {
        let resolve!: (result: DeliveryPass) => void;
        const promise = new Promise<DeliveryPass>((done) => { resolve = done; });
        pendingDelivery = { deadline, promise, resolve };
      } else pendingDelivery.deadline = Math.min(pendingDelivery.deadline, deadline);
      return pendingDelivery.promise;
    }
    deliveryDeadline = deadline;
    delivery = Promise.resolve().then(async (): Promise<DeliveryPass> => {
      if (!hasTime(deliveryDeadline, DELIVERY_TIMEOUT_MS)) return "stopped";
      const current = await status();
      // Enrollment pack R7–R8 / Decisions 4–7: pending acknowledgements and
      // draining messages remain deliverable without opening a Polling Run.
      if (!isStatus(current) || !["enrolled", "draining"].includes(current.phase)) return "stopped";
      const outbox = currentSender(current);
      // Read enrollment once per pass: status() materializes the whole Outbox.
      // Delivery pack R3 scopes every head read to this installation; reset or
      // unlink removes its rows, so sendNext then returns empty without sending.
      // The next pass observes enrollment again, including a replacement context.
      for (let attempt = 0; attempt < MAX_DELIVERY_ATTEMPTS; attempt++) {
        if (!hasTime(deliveryDeadline, DELIVERY_TIMEOUT_MS)) return "stopped";
        const result = await outbox.sendNext();
        if (result.outcome === "empty") return "drained";
        // Delivery pack Decisions 3–4: only receipts advance the head. A
        // retryable or blocked head waits for another trigger, never a spin.
        if (result.outcome !== "delivered") return "terminal";
      }
      return "stopped";
    }).catch((): DeliveryPass => "terminal").finally(() => {
      delivery = null;
      const next = pendingDelivery;
      pendingDelivery = null;
      // Consume the signal before opening the next bounded pass. A terminal
      // head cannot repeat it; only another overlapping caller can request one.
      if (next) void deliver(next.deadline).then(next.resolve);
    });
    return delivery;
  }

  let coordinator: SenderSync | null = null;
  let coordinatorIdentity: string | null = null;
  let preparation: Promise<unknown> = Promise.resolve();
  const inFlight = new Set<Promise<RunResult>>();
  function startRun(request: TriggerRequest, onDelivery?: (pass: DeliveryPass) => void): Promise<RunResult> {
    if (!coordinator) throw new Error("Sender coordinator is unavailable");
    const run = coordinator.trigger(request);
    // Adoption and refresh wait only for Polling Runs, including coalesced
    // follow-ups. A slow network drain must not extend the active-run set.
    inFlight.add(run);
    void run.then(() => inFlight.delete(run), () => inFlight.delete(run));
    return run.then(async (result) => {
      // Background delivery belongs to the callback's task-start window.
      if (request.kind !== "background") {
        const pass = await deliver();
        onDelivery?.(pass);
      }
      return result;
    });
  }
  function identity(current: EnrollmentStatus): string {
    const context = current.stagingContext;
    if (!context) throw new Error("Sender staging context is unavailable");
    return JSON.stringify([context.installationId, context.keyAlias, context.generation]);
  }

  // Return the pending run in an envelope: preparation must never await it.
  function prepareAndStart<T>(kind: TriggerRequest["kind"], start: () => Promise<T>, deadline = Infinity): Promise<
    { readonly pending: Promise<T>; readonly refreshAfter: boolean } | { readonly outcome: "skipped" } |
    { readonly outcome: "failed"; readonly failure: "storage-failed" }
  > {
    const ready = preparation.then(async () => {
      let current = await status();
      const refreshAfter = kind === "foreground" && inFlight.size > 0;
      // Register from the initial installation observation before contacting the Portal.
      if (isStatus(current) && kind === "foreground") {
        await currentPolling().ensureRegistered();
        current = await status();
      }
      if (isStatus(current) && (current.phase === "confirming" ||
          kind === "foreground" && !refreshAfter && current.phase === "enrolled")) {
        // Enrollment pack R4 and Decision 6: retry confirmation; refresh on start/return.
        // Adoption must wait for all old-generation runs before preparation can resume.
        if (inFlight.size > 0) {
          await Promise.allSettled(inFlight);
          current = await status();
        }
        try {
          if (isStatus(current) && hasTime(deadline, ENROLLMENT_TIMEOUT_MS)) {
            if (current.phase === "confirming") await enrollment.confirm();
            else if (kind === "foreground" && current.phase === "enrolled") await enrollment.refreshCapabilities();
          }
        } catch { /* An unavailable Portal does not disable the adopted generation. */ }
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
      return { pending: start(), refreshAfter };
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
        async headBlock(latest) {
          if (!isStatus(latest)) return { outcome: "storage-failed" };
          return currentSender(latest).headBlock();
        },
      }),
      backgroundScheduling: () => currentPolling().observe(),
      now: options.now, monotonicNow: options.monotonicNow, randomUUID: options.randomUUID,
    });
    coordinatorIdentity = identity;
  }

  async function trigger(kind: "foreground" | "manual"): Promise<ShellRunResult> {
    let deliveredAfterRefresh = false;
    try {
      let pass: DeliveryPass = "stopped";
      const ready = await prepareAndStart(kind, () => startRun({ kind }, (result) => { pass = result; }));
      if ("pending" in ready) {
        try { return await ready.pending; }
        finally {
          if (ready.refreshAfter) {
            // Preserve coordinator coalescing even when the follow-up rejects.
            // Hold preparation after it settles, so adoption cannot race any run opening.
            const refreshed = preparation.then(async () => {
              await Promise.allSettled(inFlight);
              const current = await status();
              let adopted = false;
              if (isStatus(current) && current.phase === "enrolled") {
                try { adopted = (await enrollment.refreshCapabilities()).outcome === "adopted"; }
                catch { /* Keep the adopted generation. */ }
              }
              // A drained pass may pick up a new acknowledgement. Adoption can
              // also resume a capped pass, but never retries its terminal head.
              if (pass === "drained" || adopted && pass !== "terminal") {
                await deliver();
                deliveredAfterRefresh = true;
              }
            });
            preparation = refreshed.catch(() => undefined);
            await refreshed;
          }
        }
      }
      await deliver();
      return ready;
    } catch (error) {
      // Health Connect/coordinator setup cannot strand an already staged queue.
      // Preserve the caller's original rejection after this failure-safe pass.
      if (!deliveredAfterRefresh) await deliver();
      throw error;
    }
  }

  // The provider is safe for headless execution and for a retained registration
  // after draining or unlinking. A skipped task has no checkpoint and reports failure.
  const backgroundPollingProvider: BackgroundPollingProvider = async () => ({
    ensureRegistered: () => currentPolling().ensureRegistered(),
    observe: () => currentPolling().observe(),
    condition: () => currentPolling().condition(),
    async runTask() {
      const deadline = options.monotonicNow() + BACKGROUND_BUDGET_MS;
      try {
        // Polling Runs follow coordinate-sender-sync@1's relative budget and R8
        // merging; absolute deadlines belong to activity_tracker_portal-pv2.8.
        const ready = await prepareAndStart("background", () => currentPolling().runTask(), deadline);
        const result = "pending" in ready ? await ready.pending : "failed";
        return result;
      } catch { return "failed"; }
      finally { await deliver(deadline); }
    },
  });

  return {
    foreground: () => trigger("foreground"),
    manualSync: () => trigger("manual"),
    backgroundPollingProvider,
  };
}
