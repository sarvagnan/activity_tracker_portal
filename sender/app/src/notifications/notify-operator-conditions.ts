import { storageLane } from "../storage/storage-lane.ts";
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
  async apply(transaction: SenderDatabase): Promise<void> {
    await transaction.execute(`CREATE TABLE operator_conditions (
  condition TEXT PRIMARY KEY NOT NULL CHECK (condition IN ('permission-missing', 'screen-lock-removed', 'storage-pressure',
    'record-too-large', 'projection-refused', 'installation-key-unusable', 'enrollment-refused', 'drain-complete')),
  first_seen_ms INTEGER NOT NULL,
  last_seen_ms INTEGER NOT NULL,
  notified INTEGER NOT NULL CHECK (notified IN (0, 1)),
  CHECK (last_seen_ms >= first_seen_ms)) STRICT`);
  },
});

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasMethods(value: unknown, methods: readonly string[]): boolean {
  return object(value) && methods.every((method) => typeof value[method] === "function");
}

function checkObservation(observation: ConditionObservation): void {
  if (!object(observation) ||
      !(observation.enrollment === null || object(observation.enrollment) && typeof observation.enrollment.phase === "string") ||
      !(observation.keyStatus === undefined || ["present", "missing", "unusable", "unavailable"].includes(observation.keyStatus))) {
    throw new TypeError("Invalid condition observation");
  }
  if (observation.summary !== undefined) {
    if (!object(observation.summary) || !Array.isArray(observation.summary.recordTypes)) {
      throw new TypeError("Invalid condition run summary");
    }
    for (const entry of observation.summary.recordTypes) {
      if (!object(entry) || typeof entry.outcome !== "string" || entry.reason !== undefined && typeof entry.reason !== "string") {
        throw new TypeError("Invalid condition run outcome");
      }
    }
  }
}

/** An absent entry keeps a condition without advancing its last seen time. */
function observedConditions(observation: ConditionObservation): ReadonlyMap<OperatorCondition, boolean> {
  const states = new Map<OperatorCondition, boolean>();
  const { summary, enrollment, keyStatus } = observation;
  if (summary !== undefined) {
    states.set("permission-missing", summary.recordTypes.some((entry) => entry.outcome === "not-permitted" || entry.reason === "permission-missing"));
    states.set("screen-lock-removed", summary.recordTypes.some((entry) => entry.reason === "screen-lock-removed"));
    states.set("storage-pressure", summary.recordTypes.some((entry) => entry.reason === "storage-pressure"));
    states.set("record-too-large", summary.recordTypes.some((entry) => entry.reason === "record-too-large"));
    states.set("projection-refused", summary.recordTypes.some((entry) => entry.reason === "projection-refusal"));
  }
  states.set("enrollment-refused", enrollment?.phase === "refused");
  states.set("drain-complete", enrollment?.phase === "draining" && enrollment.finalSequence != null);
  if (enrollment === null || enrollment.phase === "refused" || keyStatus === "present") {
    states.set("installation-key-unusable", false);
  } else if (keyStatus === "missing" || keyStatus === "unusable") {
    states.set("installation-key-unusable", true);
  }
  return states;
}

async function readConditions(database: SenderDatabase): Promise<ConditionRecord[]> {
  const rows = await database.query("SELECT condition, first_seen_ms, last_seen_ms, notified FROM operator_conditions");
  return OPERATOR_CONDITIONS.flatMap((condition) => {
    const row = rows.find((entry) => entry.condition === condition);
    return row ? [{ condition, firstSeenMs: row.first_seen_ms as number, lastSeenMs: row.last_seen_ms as number, notified: row.notified === 1 }] : [];
  });
}

export function createOperatorConditions(options: OperatorConditionsOptions): OperatorConditions {
  if (!object(options) || !hasMethods(options.database, ["execute", "query", "transaction", "close"]) ||
      !hasMethods(options.notifier, ["notify"]) || typeof options.now !== "function") {
    throw new TypeError("Invalid Operator conditions options");
  }
  const { database, notifier, now } = options;
  const lane = storageLane(database);
  let observations: Promise<void> = Promise.resolve();

  async function observe(states: ReadonlyMap<OperatorCondition, boolean>, instant: number): Promise<ObserveResult> {
    try {
      const active = await lane(() => database.transaction(async (transaction) => {
        const previous = await readConditions(transaction);
        for (const condition of OPERATOR_CONDITIONS) {
          const state = states.get(condition);
          const row = previous.find((entry) => entry.condition === condition);
          if (state === false && row) {
            await transaction.execute("DELETE FROM operator_conditions WHERE condition = ?", [condition]);
          } else if (state === true && !row) {
            await transaction.execute("INSERT INTO operator_conditions (condition, first_seen_ms, last_seen_ms, notified) VALUES (?, ?, ?, 0)",
              [condition, instant, instant]);
          } else if (state === true && row && instant > row.lastSeenMs) {
            await transaction.execute("UPDATE operator_conditions SET last_seen_ms = ? WHERE condition = ?", [instant, condition]);
          }
        }
        return readConditions(transaction);
      }));
      for (const row of active) {
        if (row.notified) continue;
        // Another instance may clear or replace an onset while an earlier notifier is pending.
        const current = await lane(() => database.query(
          "SELECT condition FROM operator_conditions WHERE condition = ? AND first_seen_ms = ? AND notified = 0",
          [row.condition, row.firstSeenMs],
        ));
        if (current.length === 0) continue;
        try { await notifier.notify(row.condition); }
        catch { continue; }
        await lane(() => database.execute(
          "UPDATE operator_conditions SET notified = 1 WHERE condition = ? AND first_seen_ms = ? AND notified = 0",
          [row.condition, row.firstSeenMs],
        ));
      }
      return { outcome: "observed", conditions: active.map((row) => row.condition) };
    } catch { return { outcome: "storage-failed" }; }
  }

  return {
    async observe(observation) {
      checkObservation(observation);
      const instant = now();
      if (!Number.isSafeInteger(instant)) throw new TypeError("Condition time must be a safe integer");
      const states = observedConditions(observation);
      const result = observations.then(() => observe(states, instant));
      observations = result.then(() => undefined, () => undefined);
      return result;
    },
    async conditions() {
      try { return await lane(() => readConditions(database)); }
      catch { return { outcome: "storage-failed" }; }
    },
  };
}
