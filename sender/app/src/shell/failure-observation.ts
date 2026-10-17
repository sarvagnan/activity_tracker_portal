/** Volatile, allowlisted operational diagnostics. Never copy a raw failure or error. */
const FAILURE_NAMES = [
  "storage-failed", "offer-invalid", "already-enrolled", "screen-lock-required",
  "not-confirming", "not-enrolled", "not-confirmed", "not-drained",
  "enrollment-refused", "capabilities-unsupported", "transport-failed", "temporarily-unavailable",
  "missing-key", "unusable-key", "keystore-unavailable", "key-exists",
  "receipt-mismatch", "head-unreadable", "head-invalid", "incompatible", "unavailable",
  "portal-blocked", "operation-failed", "storage-reserve-exhausted",
] as const;
export interface ShellFailure {
  readonly kind: "enrollment" | "delivery";
  readonly failure: (typeof FAILURE_NAMES)[number];
  readonly timeMs: number;
}
export function createFailureObservation(now: () => number) {
  let latest: ShellFailure | null = null;
  return {
    record(kind: ShellFailure["kind"], result: unknown): void {
      if (!result || typeof result !== "object" || !("outcome" in result)) return;
      const outcome = result.outcome;
      if (!["storage-failed", "refused", "portal-refused", "retryable", "cipher-failed", "blocked",
        "operation-failed", "incompatible", "unavailable"].includes(String(outcome))) return;
      const name = "failure" in result ? result.failure : outcome;
      const failure = FAILURE_NAMES.find((candidate) => candidate === name)
        ?? (outcome === "blocked" ? "portal-blocked" : "operation-failed");
      latest = Object.freeze({ kind, failure, timeMs: now() });
    },
    latest: (): ShellFailure | null => latest,
  };
}
