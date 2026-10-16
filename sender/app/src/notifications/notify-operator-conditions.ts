import type { EnrollmentStatus } from "../enrollment/manage-sender-enrollment.ts";
import type { InstallationKeyStatus } from "../installation-cipher.ts";
import type { RunSummary } from "../outbox/stage-sync-checkpoint.ts";
import type { SenderDatabase } from "../storage/database.ts";
import type { Migration } from "../storage/migrations.ts";

/** The Operator-action-required conditions this unit keeps, in condition order (notify-operator-conditions@1). */
export const OPERATOR_CONDITIONS = Object.freeze([
  "permission-missing",
  "screen-lock-removed",
  "storage-pressure",
  "record-too-large",
  "projection-refused",
  "installation-key-unusable",
  "enrollment-refused",
  "drain-complete",
] as const);

export type OperatorCondition = (typeof OPERATOR_CONDITIONS)[number];

/** Raises one health-data-free local notification naming the condition alone. */
export interface ConditionNotifier {
  notify(condition: OperatorCondition): Promise<void>;
}

export interface ConditionObservation {
  /** The Polling Run this observation follows, if any. */
  readonly summary?: RunSummary;
  /** manage-sender-enrollment@1's status, or `null` without an enrollment. */
  readonly enrollment: EnrollmentStatus | null;
  /** The cipher's status for the enrollment's key alias, when it was read. */
  readonly keyStatus?: InstallationKeyStatus;
}

export interface ConditionRecord {
  readonly condition: OperatorCondition;
  readonly firstSeenMs: number;
  readonly lastSeenMs: number;
  /** Whether this onset's notification succeeded. */
  readonly notified: boolean;
}

export type StorageFailed = { readonly outcome: "storage-failed" };
export type ObserveResult = { readonly outcome: "observed"; readonly conditions: readonly OperatorCondition[] } | StorageFailed;

export interface OperatorConditionsOptions {
  readonly database: SenderDatabase;
  readonly notifier: ConditionNotifier;
  readonly now: () => number;
}

export interface OperatorConditions {
  /** Update the conditions from one observation, and notify each onset not yet notified. */
  observe(observation: ConditionObservation): Promise<ObserveResult>;
  /** The active conditions, in condition order. */
  conditions(): Promise<readonly ConditionRecord[] | StorageFailed>;
}

/** Sender schema version 9: this unit's Operator-action-required conditions. */
export const operatorConditionsMigration: Migration = Object.freeze({
  version: 9,
  apply(): void {
    throw new Error("notify-operator-conditions@1 is not implemented");
  },
});

export function createOperatorConditions(options: OperatorConditionsOptions): OperatorConditions {
  void options;
  throw new Error("notify-operator-conditions@1 is not implemented");
}
