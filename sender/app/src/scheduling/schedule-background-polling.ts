import type { BackgroundScheduling } from "../outbox/stage-sync-checkpoint.ts";
import type { SenderDatabase } from "../storage/database.ts";
import type { Migration } from "../storage/migrations.ts";
import type { SenderSync } from "../sync/coordinate-sender-sync.ts";
import { storageLane } from "../sync/sync-storage.ts";

/** The background task's name in Expo TaskManager (schedule-background-polling@1). */
export const POLLING_TASK_NAME = "activity-tracker-portal.polling";
/** The desired minimum interval between background callbacks, in minutes. */
export const MINIMUM_INTERVAL_MINUTES = 60;
/** The soft work budget of each background trigger, within WorkManager's ten-minute window. */
export const BACKGROUND_BUDGET_MS = 480_000;

/** A persistent Operator-action-required scheduling condition. */
export type SchedulingCondition = "restricted" | "unregistered";

/** Expo BackgroundTask and TaskManager, combined with Android's background restriction and standby bucket. */
export interface SchedulingPlatform {
  isRegistered(taskName: string): Promise<boolean>;
  register(taskName: string, minimumIntervalMinutes: number): Promise<void>;
  /** `restricted` when Expo reports restricted, Android restricts the app's background work, or the app is in the restricted standby bucket. */
  restriction(): Promise<"available" | "restricted">;
}

/** Raises one health-data-free local notification naming the condition. */
export interface SchedulingNotifier {
  notify(condition: SchedulingCondition): Promise<void>;
}

export interface BackgroundPollingOptions {
  readonly database: SenderDatabase;
  readonly platform: SchedulingPlatform;
  readonly notifier: SchedulingNotifier;
  readonly sync: Pick<SenderSync, "trigger">;
  readonly now: () => number;
}

export type StorageFailed = { readonly outcome: "storage-failed" };

export type EnsureResult =
  | { readonly outcome: "observed"; readonly status: BackgroundScheduling; readonly condition: SchedulingCondition | null }
  | StorageFailed;

export type BackgroundTaskResult = "success" | "failed";

export interface BackgroundPolling {
  /** At app start and return to the foreground: register the task if it is missing, then observe. */
  ensureRegistered(): Promise<EnsureResult>;
  /** coordinate-sender-sync@1's `backgroundScheduling` option: observe without registering. */
  observe(): Promise<BackgroundScheduling>;
  /** The TaskManager task executor's body; never throws. */
  runTask(): Promise<BackgroundTaskResult>;
  /** The persistent action-required condition. */
  condition(): Promise<SchedulingCondition | null | StorageFailed>;
}

/** Sender schema version 6: this unit's one-row scheduling state. */
export const schedulingMigration: Migration = Object.freeze({
  version: 6,
  async apply(transaction: SenderDatabase): Promise<void> {
    await transaction.execute(`CREATE TABLE scheduling_state (
  singleton INTEGER PRIMARY KEY NOT NULL CHECK (singleton = 1),
  observed_status TEXT NOT NULL CHECK (observed_status IN ('available', 'restricted', 'unregistered')),
  observed_at_ms INTEGER NOT NULL,
  action_condition TEXT CHECK (action_condition IN ('restricted', 'unregistered')),
  notified_condition TEXT CHECK (notified_condition IN ('restricted', 'unregistered')),
  CHECK (action_condition IS (CASE WHEN observed_status = 'available' THEN NULL ELSE observed_status END)),
  CHECK (notified_condition IS NULL OR notified_condition IS action_condition)) STRICT`);
  },
});

function hasMethods(value: unknown, methods: readonly string[]): boolean {
  return typeof value === "object" && value !== null &&
    methods.every((method) => typeof (value as Record<string, unknown>)[method] === "function");
}

export function createBackgroundPolling(options: BackgroundPollingOptions): BackgroundPolling {
  if (typeof options !== "object" || options === null ||
      !hasMethods(options.database, ["execute", "query", "transaction", "close"]) ||
      !hasMethods(options.platform, ["isRegistered", "register", "restriction"]) ||
      !hasMethods(options.notifier, ["notify"]) || !hasMethods(options.sync, ["trigger"]) ||
      typeof options.now !== "function") {
    throw new TypeError("Invalid background polling options");
  }
  const { database, platform, notifier, sync, now } = options;
  const lane = storageLane(database);
  let observations: Promise<void> = Promise.resolve();
  // A readable row cannot repair this instance's incompletely recorded observation.
  let recordingFailed = false;

  function readNow(): number {
    const reading = now();
    if (!Number.isSafeInteger(reading)) throw new TypeError("Observation time must be a safe integer");
    return reading;
  }

  function serialize<T>(operation: () => Promise<T>): Promise<T> {
    const result = observations.then(operation);
    observations = result.then(() => undefined, () => undefined);
    return result;
  }

  // Unknown differs from false: only definite absence permits registration.
  async function registration(): Promise<boolean | undefined> {
    try {
      const registered = await platform.isRegistered(POLLING_TASK_NAME);
      return typeof registered === "boolean" ? registered : undefined;
    } catch { return undefined; }
  }

  async function status(): Promise<BackgroundScheduling> {
    const registered = await registration();
    let restriction: "available" | "restricted" = "restricted";
    try {
      if (await platform.restriction() === "available") restriction = "available";
    } catch { /* An unreadable restriction remains restricted. */ }
    return registered !== true ? "unregistered" : restriction;
  }

  async function record(observed: BackgroundScheduling, observedAtMs: number): Promise<EnsureResult> {
    const condition = observed === "available" ? null : observed;
    let needsNotification: boolean;
    try {
      needsNotification = await lane(() => database.transaction(async (transaction) => {
        const [previous] = await transaction.query(
          "SELECT action_condition, notified_condition FROM scheduling_state WHERE singleton = 1",
        );
        const notified = condition !== null && previous?.action_condition === condition &&
          previous.notified_condition === condition;
        // Preserve acknowledgement only within the same onset; recovery clears it too.
        await transaction.execute(`INSERT INTO scheduling_state
          (singleton, observed_status, observed_at_ms, action_condition, notified_condition)
          VALUES (1, ?, ?, ?, ?)
          ON CONFLICT (singleton) DO UPDATE SET
            observed_status = excluded.observed_status, observed_at_ms = excluded.observed_at_ms,
            action_condition = excluded.action_condition, notified_condition = excluded.notified_condition`,
        [observed, observedAtMs, condition, notified ? condition : null]);
        return condition !== null && !notified;
      }));
    } catch {
      recordingFailed = true;
      return { outcome: "storage-failed" };
    }

    if (condition !== null && needsNotification) {
      let delivered = false;
      // The observation queue stays held, but notification delivery never holds the storage lane.
      try { await notifier.notify(condition); delivered = true; }
      catch { /* The durable pending condition is retried at the next observation. */ }
      if (delivered) {
        try {
          await lane(() => database.execute(
            "UPDATE scheduling_state SET notified_condition = ? WHERE singleton = 1 AND action_condition = ?",
            [condition, condition],
          ));
        } catch {
          recordingFailed = true;
          return { outcome: "storage-failed" };
        }
      }
    }
    recordingFailed = false;
    return { outcome: "observed", status: observed, condition };
  }

  return {
    async ensureRegistered() {
      const observedAtMs = readNow();
      return serialize(async () => {
        if (await registration() === false) {
          try { await platform.register(POLLING_TASK_NAME, MINIMUM_INTERVAL_MINUTES); }
          catch { /* Observe the actual registration after the attempt. */ }
        }
        return record(await status(), observedAtMs);
      });
    },
    async observe() {
      const observedAtMs = readNow();
      return serialize(async () => {
        const observed = await status();
        await record(observed, observedAtMs);
        return observed;
      });
    },
    async runTask() {
      try {
        const result = await sync.trigger({ kind: "background", budgetMs: BACKGROUND_BUDGET_MS });
        return result?.outcome === "checkpointed" ? "success" : "failed";
      } catch { return "failed"; }
    },
    async condition() {
      return lane(async () => {
        if (recordingFailed) return { outcome: "storage-failed" } as const;
        try {
          const [row] = await database.query("SELECT action_condition FROM scheduling_state WHERE singleton = 1");
          return (row?.action_condition ?? null) as SchedulingCondition | null;
        } catch { return { outcome: "storage-failed" } as const; }
      });
    },
  };
}
