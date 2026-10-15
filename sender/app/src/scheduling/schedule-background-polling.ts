import type { BackgroundScheduling } from "../outbox/stage-sync-checkpoint.ts";
import type { SenderDatabase } from "../storage/database.ts";
import type { Migration } from "../storage/migrations.ts";
import type { SenderSync } from "../sync/coordinate-sender-sync.ts";

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
  async apply(_transaction: SenderDatabase): Promise<void> {
    throw new Error("schedule-background-polling@1 is not implemented");
  },
});

export function createBackgroundPolling(_options: BackgroundPollingOptions): BackgroundPolling {
  throw new Error("schedule-background-polling@1 is not implemented");
}
