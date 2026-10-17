import type { EnrollmentOffer, SenderEnrollment } from "../enrollment/manage-sender-enrollment.ts";
import type { OperatorCondition, OperatorConditions } from "../notifications/notify-operator-conditions.ts";
import type { ShellFailure } from "../shell/failure-observation.ts";
import type { ShellRunResult } from "../shell/compose-sender.ts";
import { resultMessage } from "../shell/result-message.ts";

export type EnrollmentState = Awaited<ReturnType<SenderEnrollment["status"]>>;
export type ConditionsState = Awaited<ReturnType<OperatorConditions["conditions"]>>;
export type PermissionState = "unknown" | "unavailable" | "update-required" | "missing" | "granted" | "failed";
export type ScreenAction = "enroll" | "confirm" | "beginDrain" | "unlink" | "reset";
export type OfferValidation = { readonly offer: Extract<EnrollmentOffer, { kind: "typed" }>; readonly error: null }
  | { readonly offer: null; readonly error: string };

/** Only input ergonomics here; the published unit still validates the offer. */
export function typedOffer(portalUrl: string, enrollmentCode: string): OfferValidation {
  const url = portalUrl.trim();
  const code = enrollmentCode.trim();
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") throw new Error();
  } catch { return { offer: null, error: "Enter an http or https Portal URL." }; }
  if (!code) return { offer: null, error: "Enter the enrollment code." };
  return { offer: { kind: "typed", portalUrl: url, enrollmentCode: code }, error: null };
}
export function resetAcknowledgement(acknowledged: boolean): { readonly acknowledgePossibleLoss: true } | null {
  return acknowledged ? { acknowledgePossibleLoss: true } : null;
}

const CONDITIONS: Record<OperatorCondition, string> = {
  "permission-missing": "Health Connect access is missing.",
  "screen-lock-removed": "Set a secure device screen lock.",
  "storage-pressure": "Free device storage to continue collection.",
  "record-too-large": "A record exceeds the delivery limit.",
  "projection-refused": "Record projection needs attention.",
  "installation-key-unusable": "The installation key is unusable.",
  "enrollment-refused": "The Portal refused enrollment.",
  "drain-complete": "Draining is complete. Retire this installation in the Portal before unlinking.",
};
const FAILURES: Record<ShellFailure["failure"], string> = {
  "storage-failed": "Sender storage is unavailable.",
  "storage-reserve-exhausted": "The Portal storage reserve is exhausted.",
  "offer-invalid": "The Portal URL or enrollment code was refused.",
  "already-enrolled": "This Sender already has an enrollment.",
  "screen-lock-required": "Set a secure device screen lock before enrolling.",
  "not-confirming": "This enrollment is no longer awaiting confirmation.",
  "not-enrolled": "An enrolled installation is required.",
  "not-confirmed": "Wait for the Portal to confirm this installation.",
  "not-drained": "Finish draining before unlinking.",
  "enrollment-refused": "The Portal refused enrollment.",
  "capabilities-unsupported": "The Portal capabilities are unsupported.",
  "transport-failed": "The Portal could not be reached. Try again.",
  "temporarily-unavailable": "The Portal is temporarily unavailable. Try again.",
  "missing-key": "The installation key is missing.",
  "unusable-key": "The installation key is unusable.",
  "keystore-unavailable": "The device keystore is unavailable.",
  "key-exists": "An installation key already exists.",
  "receipt-mismatch": "The Portal receipt could not be verified.",
  "head-unreadable": "The queued message could not be opened.",
  "head-invalid": "The queued message could not be validated.",
  "incompatible": "The Portal capabilities are incompatible.",
  "unavailable": "The Portal capabilities are unavailable.",
  "portal-blocked": "The Portal blocked delivery.",
  "operation-failed": "The Sender operation could not finish.",
};
const PERMISSIONS: Record<PermissionState, string> = {
  unknown: "Health Connect access has not been checked.",
  unavailable: "Health Connect is unavailable on this device.",
  "update-required": "Install or update the Health Connect provider, then refresh status.",
  missing: "Allow per-record read access, health data history, and background health data access.",
  granted: "All requested Health Connect read, history, and background permissions are granted.",
  failed: "Health Connect access could not be checked or requested. Try again.",
};
export function permissionState(sdkStatus: number, granted: readonly string[], requested: readonly string[]): PermissionState {
  if (sdkStatus === 2) return "update-required";
  if (sdkStatus !== 3) return "unavailable";
  return requested.every((permission) => granted.includes(permission)) ? "granted" : "missing";
}

export function senderScreenModel(input: {
  readonly status: EnrollmentState | undefined;
  readonly conditions: ConditionsState;
  readonly failure: ShellFailure | null;
  readonly sync: ShellRunResult | null;
  readonly permission: PermissionState;
  readonly acknowledgedLoss: boolean;
  readonly portalRetired: boolean;
}) {
  const current = input.status && !("outcome" in input.status) ? input.status : null;
  const phase = input.status === undefined ? "loading" : input.status && "outcome" in input.status ? "unavailable"
    : !current ? "unenrolled" : current.phase === "enrolled" && current.confirmed ? "confirmed" : current.phase;
  const titles = {
    loading: "Loading Sender", unavailable: "Enrollment status unavailable", unenrolled: "Enroll Sender",
    confirming: "Confirming enrollment", refused: "Enrollment refused", enrolled: "Enrolled; awaiting confirmation",
    confirmed: "Enrollment confirmed", draining: "Draining installation",
  };
  const actions: ScreenAction[] = [];
  if (phase === "unenrolled") actions.push("enroll");
  if (phase === "confirming") actions.push("confirm");
  if (phase === "confirmed") actions.push("beginDrain");
  if (phase === "draining" && current?.finalSequence !== null && input.portalRetired) actions.push("unlink");
  if (phase !== "loading" && resetAcknowledgement(input.acknowledgedLoss)) actions.push("reset");
  return {
    phase, title: titles[phase], actions,
    showOffer: phase === "unenrolled", showReset: phase !== "loading",
    showPortalRetired: phase === "draining" && current?.finalSequence !== null,
    fingerprint: current?.fingerprint ?? null,
    refusal: current?.refusal ? FAILURES[current.refusal] : null,
    finalSequence: current?.phase === "draining" && current.finalSequence !== null ? `Final sequence: ${current.finalSequence}` : null,
    lifecycleText: phase === "confirming" ? "Compare the fingerprint with the Portal. Retry confirmation if needed."
      : phase === "enrolled" ? "Compare the fingerprint with the Portal and confirm there. Sync now to check the acknowledgement."
      : phase === "draining" ? "Collection has stopped. Sync now to deliver queued messages. Retire the installation in the Portal after the final sequence is known."
      : phase === "confirmed" ? "Collection is enabled. You can begin draining before retiring this installation." : null,
    conditionTexts: Array.isArray(input.conditions) ? input.conditions.map((entry) => CONDITIONS[entry.condition as OperatorCondition])
      : ["Operator conditions could not be read."],
    syncText: input.sync ? resultMessage(input.sync) : "No Sync result in this process yet.",
    failureText: input.failure ? `${input.failure.kind === "delivery" ? "Delivery" : "Enrollment"}: ${FAILURES[input.failure.failure]}` : "No shell failure observed in this process.",
    failureTime: input.failure ? `Observed at ${new Date(input.failure.timeMs).toISOString()}` : null,
    permissionText: PERMISSIONS[input.permission],
    canRequestPermissions: ["missing", "granted", "failed"].includes(input.permission),
  };
}
