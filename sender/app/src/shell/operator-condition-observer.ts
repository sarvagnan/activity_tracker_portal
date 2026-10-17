import type { InstallationCipher } from "../installation-cipher.ts";
import { createOperatorConditions, OPERATOR_CONDITIONS } from "../notifications/notify-operator-conditions.ts";
import type { ConditionNotifier, OperatorCondition } from "../notifications/notify-operator-conditions.ts";
import type { EnrollmentStatus } from "../enrollment/manage-sender-enrollment.ts";
import type { RunSummary } from "../outbox/stage-sync-checkpoint.ts";
import type { SenderDatabase } from "../storage/database.ts";

// A stuck native permission check/post must release the observation queue and
// leave the onset unacknowledged (NOC R5). Late native completion is not success.
export const CONDITION_NOTIFICATION_TIMEOUT_MS = 1_000;
export const CONDITION_FINISH_RESERVE_MS = 1_000;
const CONDITION_READ_TIMEOUT_MS = 1_000;
// Three bounded freshness reads plus all eight possible notifier calls. Storage
// gets the final reserve. This is a caller admission allowance only: admitted
// work belongs to the worker, while each caller independently bounds its wait.
export const CONDITION_OBSERVATION_BUDGET_MS =
  OPERATOR_CONDITIONS.length * CONDITION_NOTIFICATION_TIMEOUT_MS +
  3 * CONDITION_READ_TIMEOUT_MS + CONDITION_FINISH_RESERVE_MS;

async function within<T>(operation: (expired: () => boolean) => Promise<T>, timeoutMs: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let expired = false;
  try {
    return await Promise.race([
      Promise.resolve().then(() => operation(() => expired)),
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => { expired = true; reject(new Error("Sender condition operation timed out")); }, timeoutMs);
      }),
    ]);
  } finally { clearTimeout(timer); }
}

/** Bounds synchronous throws and pending native checks/posts; always clears the timer. */
export function completeWithin(operation: (expired: () => boolean) => Promise<void>): Promise<void> {
  return within(operation, CONDITION_NOTIFICATION_TIMEOUT_MS);
}

/** The native boundary only receives names; no caller text or installation data. */
export function conditionNotifier(native: {
  notificationsEnabled: () => Promise<boolean>;
  postOperatorNotification: (condition: OperatorCondition) => Promise<void>;
}): ConditionNotifier {
  return {
    notify(condition) {
      return completeWithin(async (expired) => {
        if (!(await native.notificationsEnabled())) throw new Error("Sender notifications are not enabled");
        if (expired()) throw new Error("Sender condition notification timed out");
        await native.postOperatorNotification(condition);
      });
    },
  };
}

export function installationIdentity(current: EnrollmentStatus | null): string | null {
  return current === null ? null : JSON.stringify([current.installationId, current.keyAlias]);
}

type Status = EnrollmentStatus | null | { readonly outcome: "storage-failed" };
type Run = { readonly summary: RunSummary; readonly identity: string };

/** Synchronous ownership hand-off; only the worker reads or applies observations. */
export function createShellConditionObserver(options: {
  database: SenderDatabase;
  notifier: ConditionNotifier;
  cipher: Pick<InstallationCipher, "keyStatus">;
  status: () => Promise<Status>;
  /** Most recent successful shell enrollment observation; admission only. */
  identityHint: () => string | null | undefined;
  now: () => number;
  monotonicNow: () => number;
  onApplied?: (enrollment: EnrollmentStatus | null) => void;
}) {
  let submittingIdentity: string | null = null;
  const conditions = createOperatorConditions({ database: options.database, now: options.now, notifier: {
    notify(condition) {
      return completeWithin(async (expired) => {
        const current = await options.status();
        if (expired() || current && "outcome" in current) throw new Error("Sender condition observation unavailable");
        if (installationIdentity(current) !== submittingIdentity) throw new Error("Sender installation changed before notification");
        await options.notifier.notify(condition);
      });
    },
  } });
  type Signal = { promise: Promise<void>; resolve: () => void };
  let pending: Signal | null = null;
  let running = false;
  let wakeRequested = false;
  let dirty = false;
  let revision = 0;
  let retained: { identity: string; runSequence: number; summary: RunSummary } | null = null;
  let applied: { identity: string; runSequence: number } | null = null;
  const MAX_PASSES_PER_WAKE = 4;

  function completion(): Signal {
    if (!pending) {
      let resolve!: () => void;
      const promise = new Promise<void>((done) => { resolve = done; });
      pending = { promise, resolve };
    }
    return pending;
  }

  // This synchronous call deliberately passes no summary into the worker's
  // async frame. The published unit derives condition flags synchronously;
  // replacing our slot cannot leave an old summary in a suspended continuation.
  function submit(current: EnrollmentStatus | null, keyStatus: Awaited<ReturnType<typeof options.cipher.keyStatus>> | undefined) {
    return conditions.observe({ enrollment: current, keyStatus,
      ...(retained && (applied?.identity !== retained.identity || applied.runSequence < retained.runSequence)
        ? { summary: retained.summary } : {}),
    });
  }

  async function pass(): Promise<boolean> {
    const currency = revision;
    dirty = false;
    try {
      const current = await within(() => options.status(), CONDITION_READ_TIMEOUT_MS);
      if (currency !== revision) { dirty = true; return true; }
      if (current && "outcome" in current) { dirty = true; return false; }
      const identity = installationIdentity(current);
      const keyStatus = current
        ? await within(() => options.cipher.keyStatus(current.keyAlias), CONDITION_READ_TIMEOUT_MS)
        : undefined;
      if (currency !== revision) { dirty = true; return true; }
      const latest = await within(() => options.status(), CONDITION_READ_TIMEOUT_MS);
      if (currency !== revision) { dirty = true; return true; }
      if (latest && "outcome" in latest) { dirty = true; return false; }
      const latestIdentity = installationIdentity(latest);
      // Only this fresh, current worker read can clear a foreign slot. The
      // admission hint never supplies application or clearing authority.
      if (retained && retained.identity !== latestIdentity) retained = null;
      if (applied && applied.identity !== latestIdentity) applied = null;
      if (identity !== latestIdentity) { dirty = true; return false; }
      submittingIdentity = identity;
      const submittedIdentity = retained?.identity;
      const submittedSequence = retained?.runSequence;
      const result = await submit(latest, keyStatus);
      // Requests during application can change the slot, but a lifecycle-only
      // request cannot invalidate this exact summary's successful receipt.
      if (result.outcome === "observed") {
        if (retained?.identity === submittedIdentity && retained?.runSequence === submittedSequence && retained) {
          applied = { identity: retained.identity, runSequence: retained.runSequence };
        }
        options.onApplied?.(latest);
      } else {
        dirty = true;
        return false;
      }
      return true;
    } catch {
      // No automatic retry after an unavailable read/application. A later wake
      // retries the dirty work; failure never affects sync or delivery results.
      dirty = true;
      return false;
    }
  }

  async function work(): Promise<void> {
    try {
      for (let count = 0; count < MAX_PASSES_PER_WAKE && dirty; count++) {
        const signal = pending;
        pending = null;
        wakeRequested = false;
        const succeeded = await pass();
        signal?.resolve();
        if (!succeeded) break;
      }
    } finally {
      running = false;
      // Only a new eligible request can schedule another wake, never a failed
      // read or an expired caller. Each wake has a fixed pass bound.
      if (dirty && wakeRequested) wake();
    }
  }

  function wake(): void {
    wakeRequested = true;
    if (running) return;
    running = true;
    void Promise.resolve().then(work);
  }

  function waitFor(result: Promise<void>, deadline: number): Promise<void> {
    if (!Number.isFinite(deadline)) return result;
    const remaining = Math.max(0, deadline - options.monotonicNow() - CONDITION_FINISH_RESERVE_MS);
    return remaining === 0 ? Promise.resolve() : within(() => result, remaining).catch(() => undefined);
  }

  function observe(run?: Run, deadline = Infinity): Promise<void> {
    // Runs are prepared from the shell's recorded enrollment observation, so a
    // genuine replacement carries the hint. A run prepared from an observation
    // never recorded by the shell would be dropped; all coordinators are built
    // only after status() records that observation, so that path cannot occur.
    if (run && run.identity === options.identityHint() &&
        (retained?.identity !== run.identity || retained.runSequence < run.summary.runSequence)) {
      retained = { identity: run.identity, runSequence: run.summary.runSequence, summary: run.summary };
    }
    revision++;
    dirty = true;
    const eligible = options.monotonicNow() + CONDITION_OBSERVATION_BUDGET_MS <= deadline;
    if (!eligible && !running) return Promise.resolve(); // Record without waking.
    const result = completion().promise;
    if (eligible) wake();
    return waitFor(result, deadline);
  }

  return { observe, conditions: conditions.conditions,
    /** The only owner of a summary is the retained slot, including during waits. */
    retainedSummaryCount: () => retained === null ? 0 : 1,
  };
}
