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
export const CONDITION_READ_TIMEOUT_MS = 1_000;
// Backlog reads may grow, but a foreground caller must still have a fixed bound.
// Thirty seconds allows adaptation beyond the task's 12-second allowance without
// letting an arbitrarily slow (or never-settling) read strand an unbounded caller.
export const CONDITION_STATUS_READ_CEILING_MS = 30_000;
// Three bounded freshness reads plus all eight possible notifier calls. Storage
// gets the final reserve. Admission and live execution use this allowance;
// each caller independently bounds its own wait.
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

/** Shell-only deadline context; the published notifier contract stays unchanged. */
export interface ConditionNotificationAllowance {
  remainingMs(): number;
  expired(): boolean;
}
export interface ShellConditionNotifier extends ConditionNotifier {
  notify(condition: OperatorCondition, allowance?: ConditionNotificationAllowance): Promise<void>;
}

/** The native boundary only receives names; no caller text or installation data. */
export function conditionNotifier(native: {
  notificationsEnabled: () => Promise<boolean>;
  postOperatorNotification: (condition: OperatorCondition) => Promise<void>;
}): ShellConditionNotifier {
  return {
    notify(condition, allowance) {
      const remaining = Math.min(CONDITION_NOTIFICATION_TIMEOUT_MS, allowance?.remainingMs() ?? Infinity);
      if (remaining <= 0 || allowance?.expired()) return Promise.reject(new Error("Sender condition notification expired"));
      return within(async (expired) => {
        if (!(await native.notificationsEnabled())) throw new Error("Sender notifications are not enabled");
        if (expired() || allowance?.expired() || allowance && allowance.remainingMs() <= 0) throw new Error("Sender condition notification timed out");
        await native.postOperatorNotification(condition);
      }, remaining);
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
  notifier: ShellConditionNotifier;
  cipher: Pick<InstallationCipher, "keyStatus">;
  status: () => Promise<Status>;
  /** Most recent successful shell enrollment observation; admits runs or rejects stale posts. */
  identityHint: () => string | null | undefined;
  now: () => number;
  monotonicNow: () => number;
  onApplied?: (enrollment: EnrollmentStatus | null) => void;
}) {
  let executionDeadline = -Infinity;
  function remainingExecution(): number {
    return executionDeadline - options.monotonicNow() - CONDITION_FINISH_RESERVE_MS;
  }
  function requireExecution(): void {
    if (remainingExecution() <= 0) throw new Error("Sender condition execution allowance expired");
  }
  let statusDurationMs = 0;
  function measuredStatus(): Promise<Status> {
    const began = options.monotonicNow();
    // Attach the measurement to the original read, including late success after
    // its caller timed out. No read or its result is reused by another caller.
    return options.status().then((current) => {
      if (!(current && "outcome" in current)) statusDurationMs = Math.max(0, options.monotonicNow() - began);
      return current;
    });
  }
  function statusBound(): number {
    return Math.min(CONDITION_STATUS_READ_CEILING_MS, Math.max(CONDITION_READ_TIMEOUT_MS, 2 * statusDurationMs));
  }
  async function read<T>(operation: () => Promise<T>, bound: number): Promise<T> {
    requireExecution();
    const result = await within(operation, Math.min(bound, remainingExecution()));
    requireExecution();
    return result;
  }

  // Protect the final status read through settlement of the published observe.
  // Destruction joins this queue synchronously, before it can await anything.
  let applicationTail: Promise<unknown> = Promise.resolve();
  function application<T>(operation: () => Promise<T>): Promise<T> {
    const result = applicationTail.then(operation);
    applicationTail = result.catch(() => undefined);
    return result;
  }

  let submittingIdentity: string | null = null;
  let destructionEpoch = 0;
  let submittingDestructionEpoch = 0;
  const conditions = createOperatorConditions({ database: options.database, now: options.now, notifier: {
    notify(condition) {
      requireExecution();
      const deadline = Math.min(executionDeadline - CONDITION_FINISH_RESERVE_MS,
        options.monotonicNow() + CONDITION_NOTIFICATION_TIMEOUT_MS);
      return within(async (expired) => {
        const allowance: ConditionNotificationAllowance = {
          remainingMs: () => Math.max(0, Math.min(deadline, executionDeadline - CONDITION_FINISH_RESERVE_MS) - options.monotonicNow()),
          expired: () => expired() || remainingExecution() <= 0 || options.monotonicNow() >= deadline,
        };
        // Destruction is serialized with this application; the epoch also bars
        // a request made mid-application. The hint rejects any other identity
        // change observed by the shell; only the final status read authorizes.
        if (submittingDestructionEpoch !== destructionEpoch || options.identityHint() !== submittingIdentity) {
          throw new Error("Sender installation changed before notification");
        }
        if (allowance.expired() || allowance.remainingMs() <= 0) throw new Error("Sender condition notification expired");
        await options.notifier.notify(condition, allowance);
      }, Math.min(CONDITION_NOTIFICATION_TIMEOUT_MS, remainingExecution()));
    },
  } });
  type Signal = { promise: Promise<void>; resolve: () => void; deadline: number };
  let pending: Signal | null = null;
  let submittedSignal: Signal | null = null;
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
      pending = { promise, resolve, deadline: -Infinity };
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

  function settle(): void {
    const signal = pending;
    pending = null;
    signal?.resolve();
  }

  async function pass(): Promise<"superseded" | "applied" | "failed"> {
    const currency = revision;
    dirty = false;
    let readStarted = false;
    try {
      const current = await read(() => { readStarted = true; return measuredStatus(); }, statusBound());
      if (currency !== revision) { dirty = true; return "superseded"; }
      if (current && "outcome" in current) throw new Error("Sender enrollment unavailable");
      const identity = installationIdentity(current);
      const keyStatus = current
        ? await read(() => options.cipher.keyStatus(current.keyAlias), CONDITION_READ_TIMEOUT_MS).catch(() => "unavailable" as const)
        : undefined;
      requireExecution();
      if (currency !== revision) { dirty = true; return "superseded"; }
      return await application(async () => {
        const latest = await read(measuredStatus, statusBound());
        const latestDestructionEpoch = destructionEpoch;
        if (currency !== revision) { dirty = true; return "superseded"; }
        if (latest && "outcome" in latest) throw new Error("Sender enrollment unavailable");
        const latestIdentity = installationIdentity(latest);
        // Only a fresh worker read can clear a foreign slot; the hint cannot.
        if (retained && retained.identity !== latestIdentity) retained = null;
        if (applied && applied.identity !== latestIdentity) applied = null;
        if (identity !== latestIdentity) throw new Error("Sender installation changed");
        requireExecution();
        submittingIdentity = identity;
        submittingDestructionEpoch = latestDestructionEpoch;
        const submittedIdentity = retained?.identity;
        const submittedSequence = retained?.runSequence;
        // A superseded read never detaches its completion. Once submitted,
        // subsequent requests get a new signal for their own coalesced state.
        submittedSignal = pending;
        pending = null;
        const result = await submit(latest, keyStatus);
        requireExecution();
        if (result.outcome !== "observed") throw new Error("Sender conditions unavailable");
        if (retained?.identity === submittedIdentity && retained?.runSequence === submittedSequence && retained) {
          applied = { identity: retained.identity, runSequence: retained.runSequence };
        }
        options.onApplied?.(latest);
        return "applied";
      });
    } catch {
      // A later eligible wake retries dirty state. Read/write failures never
      // affect sync or delivery, and release even an unbounded caller.
      dirty = true;
      // A read that timed out fails this attempt even under continuous new
      // admissions: supersession must not keep a stuck read's waiters forever.
      // If no read could start, a requester arriving after that synchronous
      // refusal still needs its own eligible attempt.
      return !readStarted && currency !== revision ? "superseded" : "failed";
    }
  }

  async function work(): Promise<void> {
    try {
      for (let count = 0; count < MAX_PASSES_PER_WAKE && dirty; count++) {
        wakeRequested = false;
        const outcome = await pass();
        const submitted = submittedSignal;
        submittedSignal = null;
        // Completed requesters cannot lend their allowance to a later slot.
        executionDeadline = pending?.deadline ?? -Infinity;
        submitted?.resolve();
        if (outcome === "failed") {
          if (!submitted) {
            settle();
            executionDeadline = -Infinity;
            wakeRequested = false;
          }
          break;
        }
      }
    } finally {
      running = false;
      // Never carry an unbounded allowance into a later background-only wake.
      if (dirty && wakeRequested) wake();
      else {
        // Hitting the pass bound is a failed attempt, not an application. Leave
        // the slot dirty for a later trigger, but never strand its waiters.
        settle();
        executionDeadline = -Infinity;
      }
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
    const signal = completion();
    if (eligible) {
      signal.deadline = Math.max(signal.deadline, deadline);
      executionDeadline = Math.max(executionDeadline, deadline);
      wake();
    }
    return waitFor(signal.promise, deadline);
  }

  async function destroy<T>(operation: () => Promise<T>): Promise<T> {
    destructionEpoch++;
    revision++;
    dirty = true;
    try { return await application(operation); }
    finally { await observe().catch(() => undefined); }
  }

  return { observe, destroy, conditions: conditions.conditions,
    /** The only owner of a summary is the retained slot, including during waits. */
    retainedSummaryCount: () => retained === null ? 0 : 1,
  };
}
