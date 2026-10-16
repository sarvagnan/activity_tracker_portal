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
// gets the final reserve. The eight-minute deadline is soft, like the existing
// delivery window: do not admit work without this allowance, bound the caller's
// wait, and recheck the deadline before each native post.
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

/** One shell queue outside the unit's R9 queue: at most one active and one pending observation. */
export function createShellConditionObserver(options: {
  database: SenderDatabase;
  notifier: ConditionNotifier;
  cipher: Pick<InstallationCipher, "keyStatus">;
  status: () => Promise<Status>;
  now: () => number;
  monotonicNow: () => number;
}) {
  let submittingIdentity: string | null = null;
  let submittingDeadline = Infinity;
  const conditions = createOperatorConditions({ database: options.database, now: options.now, notifier: {
    notify(condition) {
      // Include the identity read in the bound. A reset while an earlier onset's
      // notifier is running must not start another notification for that identity.
      return completeWithin(async (expired) => {
        const current = await options.status();
        if (expired() || current && "outcome" in current) throw new Error("Sender condition observation unavailable");
        if (installationIdentity(current) !== submittingIdentity) throw new Error("Sender installation changed before notification");
        if (options.monotonicNow() + CONDITION_NOTIFICATION_TIMEOUT_MS + CONDITION_FINISH_RESERVE_MS > submittingDeadline) {
          throw new Error("Sender condition notification deadline reached");
        }
        await options.notifier.notify(condition);
      });
    },
  } });
  type Pending = { deadline: number; revision: number; promise: Promise<void>; resolve: () => void };
  let pending: Pending | null = null;
  let active: Pending | null = null;
  let submitted = false;
  let revision = 0;
  // Receiving is not applying. Keep the newest summary for each supplied
  // identity until fresh status reads select the current installation. A late
  // removed-identity caller must not replace the current one's retained work.
  let receivedRuns = new Map<string, Run>();
  let appliedRun: Run | undefined;

  function hasTime(deadline: number): boolean {
    return options.monotonicNow() + CONDITION_OBSERVATION_BUDGET_MS <= deadline;
  }

  async function process(item: Pending): Promise<void> {
    if (!hasTime(item.deadline)) return;
    const current = await within(() => options.status(), CONDITION_READ_TIMEOUT_MS);
    if (current && "outcome" in current) return; // Unknown is not unlink.
    const identity = installationIdentity(current);
    let keyStatus;
    if (current) {
      try { keyStatus = await within(() => options.cipher.keyStatus(current.keyAlias), CONDITION_READ_TIMEOUT_MS); }
      catch { keyStatus = "unavailable" as const; }
    }
    const latest = await within(() => options.status(), CONDITION_READ_TIMEOUT_MS);
    if (latest && "outcome" in latest) return;
    const latestIdentity = installationIdentity(latest);
    // Only keep the current identity, including when a reset invalidates this
    // build. Unknown status above never discards retained work.
    const received = latestIdentity === null ? undefined : receivedRuns.get(latestIdentity);
    receivedRuns = received ? new Map([[received.identity, received]]) : new Map();
    if (appliedRun?.identity !== latestIdentity) appliedRun = undefined;
    if (latestIdentity !== identity) return;
    // Requests arriving during native reads supersede this unsubmitted build.
    if (item.revision !== revision || !hasTime(item.deadline)) return;
    const run = received && (!appliedRun || received.summary.runSequence > appliedRun.summary.runSequence) ? received : undefined;
    submittingIdentity = identity;
    submittingDeadline = item.deadline;
    submitted = true;
    const result = await conditions.observe({ enrollment: latest, keyStatus, ...(run ? { summary: run.summary } : {}) });
    if (run && result.outcome === "observed") appliedRun = run;
  }

  function drain(): void {
    if (active || !pending) return;
    const item = pending;
    pending = null;
    active = item;
    submitted = false;
    void process(item).catch(() => { /* Conditions never affect sync, delivery, or task results (R8). */ }).finally(() => {
      active = null;
      item.resolve();
      drain(); // Event-driven; each admission consumes one slot, never a retry loop.
    });
  }

  function waitFor(result: Promise<void>, deadline: number): Promise<void> {
    // A background caller may be queued behind foreground work. Its wait ends
    // inside its own task window, independently of other callers' admission.
    if (!Number.isFinite(deadline)) return result;
    const remaining = Math.max(0, deadline - options.monotonicNow() - CONDITION_FINISH_RESERVE_MS);
    return remaining === 0 ? Promise.resolve() : within(() => result, remaining).catch(() => undefined);
  }

  function observe(run?: Run, deadline = Infinity): Promise<void> {
    if (run) {
      const received = receivedRuns.get(run.identity);
      if (!received || received.summary.runSequence < run.summary.runSequence) {
        receivedRuns.set(run.identity, run);
      }
    }
    revision++;
    // A request superseding native reads also inherits the still-waiting
    // caller's admission. Once submitted, that allowance belongs to that
    // observation only; a later background request needs its own allowance.
    const admissionDeadline = Math.max(deadline, active && !submitted ? active.deadline : -Infinity);
    if (!pending) {
      let resolve!: () => void;
      const promise = new Promise<void>((done) => { resolve = done; });
      pending = { deadline: admissionDeadline, revision, promise, resolve };
    } else {
      // Any eligible waiter may admit the retained work. An expiring background
      // caller bounds only its own wait, never a foreground/lifecycle caller.
      pending.deadline = Math.max(pending.deadline, admissionDeadline);
      pending.revision = revision;
    }
    const result = pending.promise;
    drain();
    return waitFor(result, deadline);
  }

  return { observe, conditions: conditions.conditions };
}
