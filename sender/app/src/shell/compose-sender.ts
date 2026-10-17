import { completeWithin, CONDITION_OBSERVATION_BUDGET_MS, createShellConditionObserver, installationIdentity, type ShellConditionNotifier } from "./operator-condition-observer.ts";
import type { OperatorConditions } from "../notifications/notify-operator-conditions.ts";
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
type ShellEnrollment = Pick<SenderEnrollment, "status" | "confirm" | "refreshCapabilities" | "unlink" | "reset">;
// Older shell fixtures supply only the read/confirmation surface. Production
// supplies the full enrollment; destruction is checked when explicitly invoked.
type EnrollmentBoundary = Omit<ShellEnrollment, "unlink" | "reset"> & Partial<Pick<ShellEnrollment, "unlink" | "reset">>;

export const MAX_DELIVERY_ATTEMPTS = 64;
// Leave time for receipt bookkeeping after the unit's full network timeout.
export const DELIVERY_FINISH_RESERVE_MS = 1_000;
export type ShellRunResult = RunResult | { readonly outcome: "skipped" };

export interface ComposeSenderOptions {
  readonly openDatabase: () => Promise<SenderDatabase>;
  /** The production boundary constructs the published enrollment on this migrated handle. */
  readonly enrollment: (database: SenderDatabase, transport: HttpTransport) => EnrollmentBoundary;
  readonly fetch: HttpFetch;
  readonly deliveryNotifier: DeliveryNotifier;
  readonly conditionNotifier: ShellConditionNotifier;
  readonly cipher: InstallationCipher;
  readonly sources: SyncSources;
  readonly projectionSchemaVersions: () => Promise<Readonly<Record<string, number>>>;
  readonly availableStorageBytes: () => Promise<number>;
  readonly platform: SchedulingPlatform;
  readonly notifier: SchedulingNotifier;
  readonly now: () => number;
  readonly monotonicNow: () => number;
  readonly randomUUID: () => string;
  /** Optional health-data-free admission trace; diagnostics cannot affect a run. */
  readonly onRunSubmitted?: (kind: TriggerRequest["kind"]) => void;
}

export interface SenderShell {
  /** App start and every return to active: registration observation, then a foreground trigger. */
  foreground(): Promise<ShellRunResult>;
  manualSync(): Promise<ShellRunResult>;
  readonly backgroundPollingProvider: BackgroundPollingProvider;
  /** 2rv.8 screens call these methods for reset/unlink, never enrollment directly. */
  unlink: SenderEnrollment["unlink"];
  reset: SenderEnrollment["reset"];
  /** Call after other screen enrollment actions to observe their current lifecycle. */
  observeConditions(): Promise<void>;
  conditions: OperatorConditions["conditions"];
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
  let enrollment: EnrollmentBoundary;
  try {
    const migration = await migrate(database, senderMigrations);
    if (migration.status !== "ready") throw new Error("Sender database is frozen");
    enrollment = options.enrollment(database, transport);
  } catch (error) {
    await database.close();
    throw error;
  }

  let installationHint: string | null | undefined;
  async function readStatus(): Promise<Status> {
    try {
      const current = await enrollment.status();
      if (!(current && "outcome" in current)) installationHint = installationIdentity(current);
      return current;
    }
    catch { return { outcome: "storage-failed" }; }
  }

  // expoSender owns this migrated handle and this observer for the process. App
  // and TaskManager never open a second handle. Separate processes/handles are
  // outside this composition; Android schedules this module in the app process.
  let observedPhase: string | undefined;
  let phaseChanged = false;
  type ObservationAttempt = { phase?: string; failedRun?: boolean; pending?: Promise<void> };
  function phaseIdentity(current: EnrollmentStatus | null): string {
    return JSON.stringify([installationIdentity(current), current?.phase, current?.finalSequence]);
  }
  const operator = createShellConditionObserver({
    database, notifier: options.conditionNotifier, cipher: options.cipher,
    status, identityHint: () => installationHint, now: options.now, monotonicNow: options.monotonicNow,
    onApplied(current) { phaseChanged = observedPhase !== phaseIdentity(current); },
  });
  function trackPhase(current: Status): Status {
    if (current && "outcome" in current) return current;
    const phase = phaseIdentity(current);
    phaseChanged ||= phase !== observedPhase;
    observedPhase = phase;
    return current;
  }
  async function status(): Promise<Status> { return trackPhase(await readStatus()); }
  async function observeLifecycle(attempt: ObservationAttempt, deadline = Infinity): Promise<void> {
    attempt.phase = observedPhase;
    await (attempt.pending = operator.observe(undefined, deadline));
    // A fresh read inside the observer can discover the transition itself.
    // Only its application receipt clears phaseChanged; this marker just bars
    // retrying that same phase within this trigger after an unavailable read/write.
    attempt.phase = observedPhase;
  }
  async function observePhase(attempt: ObservationAttempt, deadline = Infinity): Promise<void> {
    if (!phaseChanged || attempt.phase === observedPhase) return;
    await observeLifecycle(attempt, deadline);
  }
  async function afterDelivery(attempt: ObservationAttempt, deadline: () => number): Promise<void> {
    // One extra bounded read per pass, independent of Outbox length. Reserve
    // the full observation allowance as well as this read before starting it.
    if (!hasTime(deadline(), CONDITION_OBSERVATION_BUDGET_MS)) return;
    try {
      await completeWithin(async (expired) => {
        const current = await readStatus();
        if (!expired()) trackPhase(current);
      });
    } catch { /* The next trigger can read the lifecycle again. */ }
    // A joining caller can tighten the deadline while the status read awaits.
    // Record through the same observer path, but never put its wait in delivery.
    if (hasTime(deadline(), CONDITION_OBSERVATION_BUDGET_MS)) {
      void observePhase(attempt, deadline()).catch(() => undefined);
    }
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
  let pendingDelivery: { deadline: number; attempt: ObservationAttempt; promise: Promise<DeliveryPass>; resolve: (result: DeliveryPass) => void } | null = null;
  function hasTime(deadline: number, timeout: number): boolean {
    return options.monotonicNow() + timeout + DELIVERY_FINISH_RESERVE_MS <= deadline;
  }
  function deliver(deadline = Infinity, observation: ObservationAttempt = {}): Promise<DeliveryPass> {
    // Joining callers retain one subsequent pass, including after empty,
    // retryable and blocked observations or a delivery-context replacement.
    if (delivery) {
      deliveryDeadline = Math.min(deliveryDeadline, deadline);
      if (!pendingDelivery) {
        let resolve!: (result: DeliveryPass) => void;
        const promise = new Promise<DeliveryPass>((done) => { resolve = done; });
        pendingDelivery = { deadline, attempt: observation, promise, resolve };
      } else pendingDelivery.deadline = Math.min(pendingDelivery.deadline, deadline);
      return waitDelivery(pendingDelivery.promise, deadline, observation);
    }
    deliveryDeadline = deadline;
    delivery = Promise.resolve().then(async (): Promise<DeliveryPass> => {
      if (!hasTime(deliveryDeadline, DELIVERY_TIMEOUT_MS)) return "stopped";
      const current = await status();
      // Enrollment pack R7–R8 / Decisions 4–7: pending acknowledgements and
      // draining messages remain deliverable without opening a Polling Run.
      if (!isStatus(current) || !["enrolled", "draining"].includes(current.phase)) return "stopped";
      const outbox = currentSender(current);
      // Two enrollment reads per admitted delivery pass: this context read and
      // the post-delivery lifecycle read, independent of Outbox length.
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
    }).catch((): DeliveryPass => "terminal").finally(async () => {
      await afterDelivery(observation, () => deliveryDeadline).catch(() => undefined);
      delivery = null;
      const next = pendingDelivery;
      pendingDelivery = null;
      // Consume the signal before opening the next bounded pass. A terminal
      // head cannot repeat it; only another overlapping caller can request one.
      if (next) void deliver(next.deadline, next.attempt).then(next.resolve);
    });
    return waitDelivery(delivery, deadline, observation);
  }

  async function waitDelivery(result: Promise<DeliveryPass>, deadline: number, observation: ObservationAttempt): Promise<DeliveryPass> {
    if (!Number.isFinite(deadline)) {
      const pass = await result;
      await observation.pending;
      await observePhase(observation);
      return pass;
    }
    const remaining = deadline - options.monotonicNow() - DELIVERY_FINISH_RESERVE_MS;
    if (remaining <= 0) return "stopped";
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      return await Promise.race([result, new Promise<DeliveryPass>((resolve) => {
        timer = setTimeout(() => resolve("stopped"), remaining);
      })]);
    } finally { clearTimeout(timer); }
  }

  let coordinator: SenderSync | null = null;
  let coordinatorIdentity: string | null = null;
  let preparation: Promise<unknown> = Promise.resolve();
  const inFlight = new Set<Promise<RunResult>>();
  let coordinatorInstallation: string | null = null;
  type BackgroundCompletion = { deadline: number; attempt: ObservationAttempt; summaryPending?: boolean; observe?: () => Promise<void> };
  let backgroundCompletion: BackgroundCompletion | null = null;
  function startRun(request: TriggerRequest, observation: ObservationAttempt, onDelivery?: (pass: DeliveryPass) => void): Promise<RunResult> {
    if (!coordinator) throw new Error("Sender coordinator is unavailable");
    const runIdentity = coordinatorInstallation!;
    const completion = request.kind === "background" ? backgroundCompletion : null;
    const run = coordinator.trigger(completion ? {
      ...request,
      // performance.now() can be fractional; the published trigger requires
      // integer milliseconds. Round down to preserve this callback's reserves.
      deadlineMs: Math.floor(completion.deadline - DELIVERY_TIMEOUT_MS - DELIVERY_FINISH_RESERVE_MS - CONDITION_OBSERVATION_BUDGET_MS),
    } : request);
    try { options.onRunSubmitted?.(request.kind); } catch { /* Diagnostics are isolated. */ }
    // Adoption and refresh wait only for Polling Runs, including coalesced
    // follow-ups. A slow network drain must not extend the active-run set.
    inFlight.add(run);
    void run.then(() => inFlight.delete(run), () => inFlight.delete(run));
    return run.then(async (result) => {
      // Hand off before any delivery wait. This request records only; delivery
      // keeps its full exchange window before an eligible lifecycle wake.
      if (result.outcome === "checkpointed") {
        void operator.observe({ summary: result.summary, identity: runIdentity }, options.monotonicNow());
        if (completion) completion.summaryPending = true;
      }
      const observe = () => observeLifecycle(observation, completion?.deadline);
      if (completion) {
        // The callback keeps only a completion request; the observer already
        // owns the summary independently of this caller and its deadline.
        completion.observe = observe;
      } else {
        const pass = await deliver(Infinity, observation);
        onDelivery?.(pass);
        await observe();
      }
      return result;
    }, (error: unknown) => {
      if (completion) completion.observe = () => observeLifecycle(observation, completion.deadline);
      else observation.failedRun = true;
      throw error;
    });
  }

  function identity(current: EnrollmentStatus): string {
    const context = current.stagingContext;
    if (!context) throw new Error("Sender staging context is unavailable");
    return JSON.stringify([context.installationId, context.keyAlias, context.generation]);
  }

  // Return the pending run in an envelope: preparation must never await it.
  function prepareAndStart<T>(kind: TriggerRequest["kind"], start: () => Promise<T>, observation: ObservationAttempt, deadline = Infinity): Promise<
    { readonly pending: Promise<T>; readonly refreshAfter: boolean } | { readonly outcome: "skipped" } |
    { readonly outcome: "failed"; readonly failure: "storage-failed" }
  > {
    const ready = preparation.then(async () => {
      let current = await status();
      let confirmationAttempted = false;
      // Register from the initial installation observation before contacting the Portal.
      if (isStatus(current) && kind === "foreground") {
        await currentPolling().ensureRegistered();
        current = await status();
      }
      if (isStatus(current) && current.phase === "confirming") {
        // Enrollment pack R4: retry confirmation on every trigger.
        // Adoption must wait for all old-generation runs before preparation can resume.
        if (inFlight.size > 0) {
          await Promise.allSettled(inFlight);
          current = await status();
        }
        try {
          if (isStatus(current) && hasTime(deadline, ENROLLMENT_TIMEOUT_MS)) {
            if (current.phase === "confirming") {
              confirmationAttempted = true;
              await enrollment.confirm();
              if (kind === "background") phaseChanged = true;
              else await observeLifecycle(observation);
            }
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
      // Decide only after registration, confirmation and identity waits. An active
      // run now has this identity's prepared coordinator: coalesce without another
      // await that could let it settle. With no active run, preparation bars new
      // triggers while refresh and coordinator setup await their results.
      const refreshAfter = kind === "foreground" && inFlight.size > 0;
      // Confirmation already requested capabilities; do not request them twice.
      if (kind === "foreground" && !refreshAfter && !confirmationAttempted &&
          isStatus(current) && current.phase === "enrolled") {
        try {
          if (hasTime(deadline, ENROLLMENT_TIMEOUT_MS)) {
            await enrollment.refreshCapabilities();
            await observeLifecycle(observation, deadline);
          }
        } catch { /* An unavailable Portal does not disable the adopted generation. */ }
        current = await status();
      }
      if (kind !== "background") await observePhase(observation);
      if (current && "outcome" in current) return { outcome: "failed", failure: "storage-failed" } as const;
      if (!collectionEnabled(current) || !isStatus(current)) return { outcome: "skipped" } as const;
      if (!refreshAfter) await prepare(current);
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
      sync: { trigger: (request) => startRun(request, backgroundCompletion?.attempt ?? {}) },
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
    coordinatorInstallation = installationIdentity(current);
  }

  async function trigger(kind: "foreground" | "manual"): Promise<ShellRunResult> {
    const observation: ObservationAttempt = {};
    if (kind === "foreground") await observeLifecycle(observation);
    let deliveredAfterRefresh = false;
    try {
      let pass: DeliveryPass = "stopped";
      const ready = await prepareAndStart(kind, () => startRun({ kind }, observation, (result) => { pass = result; }), observation);
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
                try {
                  adopted = (await enrollment.refreshCapabilities()).outcome === "adopted";
                  await observeLifecycle(observation);
                }
                catch { /* Keep the adopted generation. */ }
              }
              // A drained pass may pick up a new acknowledgement. Adoption can
              // also resume a capped pass, but never retries its terminal head.
              if (pass === "drained" || adopted && pass !== "terminal") {
                await deliver(Infinity, observation);
                deliveredAfterRefresh = true;
              }
            });
            preparation = refreshed.catch(() => undefined);
            await refreshed;
          }
        }
      }
      await deliver(Infinity, observation);
      return ready;
    } catch (error) {
      // Health Connect/coordinator setup cannot strand an already staged queue.
      // Preserve the caller's original rejection after this failure-safe pass.
      if (!deliveredAfterRefresh) await deliver(Infinity, observation);
      throw error;
    } finally {
      if (observation.failedRun) await observeLifecycle(observation);
      else await observePhase(observation);
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
      const completion: BackgroundCompletion = { deadline, attempt: {} };
      try {
        // The coordinator's absolute deadline includes preparation and queueing;
        // the polling unit retains its unchanged relative budget and coalescing.
        const ready = await prepareAndStart("background", () => {
          // schedule-background-polling calls sync.trigger synchronously, so
          // startRun captures this callback's context before another admission.
          backgroundCompletion = completion;
          try { return currentPolling().runTask(); }
          finally { backgroundCompletion = null; }
        }, completion.attempt, deadline);
        const result = "pending" in ready ? await ready.pending : "failed";
        return result;
      } catch { return "failed"; }
      finally {
        // Keep the summary's observation allowance when both it and a full
        // exchange fit. If only delivery fits, it retains the last exchange.
        const deliveryDeadline = completion.summaryPending &&
          hasTime(deadline, DELIVERY_TIMEOUT_MS + CONDITION_OBSERVATION_BUDGET_MS)
          ? deadline - CONDITION_OBSERVATION_BUDGET_MS : deadline;
        await deliver(deliveryDeadline, completion.attempt);
        if (completion.observe) await completion.observe();
        // Skipped collection still retries unnotified lifecycle onsets. Delivery
        // may already have started this callback's observation for a new phase.
        else if (!completion.attempt.pending) await observeLifecycle(completion.attempt, deadline);
        await observePhase(completion.attempt, deadline);
        // This is the observer's caller-bounded signal, never its worker promise.
        await completion.attempt.pending;
      }
    },
  });

  await operator.observe(); // App/headless process start, without inventing a run summary.

  return {
    unlink: () => operator.destroy(() => {
      if (!enrollment.unlink) throw new TypeError("Sender enrollment unlink is unavailable");
      return enrollment.unlink();
    }),
    reset: (acknowledgement) => operator.destroy(() => {
      if (!enrollment.reset) throw new TypeError("Sender enrollment reset is unavailable");
      return enrollment.reset(acknowledgement);
    }),
    observeConditions: () => operator.observe(),
    conditions: operator.conditions,
    foreground: () => trigger("foreground"),
    manualSync: () => trigger("manual"),
    backgroundPollingProvider,
  };
}
