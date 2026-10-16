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
  // Sender storage belongs to one installation. Receiving is not applying:
  // retain one summary even if its caller has no observation budget left.
  let receivedRun: Run | null = null;
  // Install the completion receipt before awaiting the unit. Its immutable
  // result distinguishes applied work from a rejected/failed application,
  // without letting a superseded continuation mutate this marker afterward.
  let appliedRun: { identity: string; runSequence: number; observed: Promise<boolean> } | null = null;
  // A delayed admission identity check cannot overturn a newer verified
  // installation. This marker retains no summary or per-installation history.
  let verifiedInstallation: { identity: string | null; revision: number } | null = null;

  function hasTime(deadline: number): boolean {
    return options.monotonicNow() + CONDITION_OBSERVATION_BUDGET_MS <= deadline;
  }

  async function process(item: Pending): Promise<void> {
    if (!hasTime(item.deadline)) return;
    // The shell queue finishes the prior submission before starting this build,
    // so its receipt is already settled and consumes no native-read allowance.
    const applied = appliedRun ? await appliedRun.observed : false;
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
    // All build mutations follow its last freshness read and revision check.
    // In particular, a stale final status must not discard a replacement run.
    if (item.revision !== revision || !hasTime(item.deadline)) return;
    verifiedInstallation = { identity: latestIdentity, revision: item.revision };
    if (receivedRun?.identity !== latestIdentity) receivedRun = null;
    if (appliedRun?.identity !== latestIdentity) appliedRun = null;
    if (latestIdentity !== identity) return;
    const run = receivedRun && (!appliedRun || !applied || receivedRun.summary.runSequence > appliedRun.runSequence) ? receivedRun : undefined;
    submittingIdentity = identity;
    submittingDeadline = item.deadline;
    submitted = true;
    const result = conditions.observe({ enrollment: latest, keyStatus, ...(run ? { summary: run.summary } : {}) });
    if (run) {
      appliedRun = { identity: run.identity, runSequence: run.summary.runSequence,
        observed: result.then((value) => value.outcome === "observed", () => false) };
    }
    await result;
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

  function retain(run: Run): void {
    if (receivedRun?.identity !== run.identity || receivedRun.summary.runSequence < run.summary.runSequence) {
      receivedRun = run;
    }
  }

  async function receiveReplacement(run: Run, receivedRevision: number): Promise<void> {
    // Identity strings do not order installations. Unlike same-identity runs,
    // a foreign caller must prove it is current before evicting retained work.
    // This admission read is bounded even when observation admission expires.
    try {
      const current = await within(() => options.status(), CONDITION_READ_TIMEOUT_MS);
      if (current && "outcome" in current) return;
      const identity = installationIdentity(current);
      if (identity !== run.identity) return;
      if (verifiedInstallation && receivedRevision < verifiedInstallation.revision &&
          identity !== verifiedInstallation.identity) return;
      verifiedInstallation = { identity, revision: Math.max(receivedRevision, verifiedInstallation?.revision ?? 0) };
      retain(run);
    } catch { /* Unknown status cannot evict retained work. */ }
  }

  function enqueue(deadline: number): Promise<void> {
    if (!pending) {
      let resolve!: () => void;
      const promise = new Promise<void>((done) => { resolve = done; });
      pending = { deadline, revision, promise, resolve };
    } else {
      // Any eligible waiter may admit the retained work. An expiring background
      // caller bounds only its own wait, never a foreground/lifecycle caller.
      pending.deadline = Math.max(pending.deadline, deadline);
      pending.revision = revision;
    }
    const result = pending.promise;
    drain();
    return result;
  }

  function observe(run?: Run, deadline = Infinity): Promise<void> {
    const receivedRevision = ++revision;
    // A request superseding native reads inherits the still-waiting caller's
    // admission. Submitted work keeps its allowance for that observation only.
    const admissionDeadline = Math.max(deadline, active && !submitted ? active.deadline : -Infinity);
    if (run && receivedRun && receivedRun.identity !== run.identity) {
      const result = receiveReplacement(run, receivedRevision).then(() => enqueue(admissionDeadline));
      return waitFor(result, deadline);
    }
    if (run) retain(run);
    return waitFor(enqueue(admissionDeadline), deadline);
  }

  return { observe, conditions: conditions.conditions,
    /** Test-only cardinality inspection; exposes no summaries or installation data. */
    retainedSummaryCount: () => receivedRun === null ? 0 : 1,
  };
}
