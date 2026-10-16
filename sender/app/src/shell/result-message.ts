import type { ShellRunResult } from "./compose-sender.ts";
import type { OutcomeReason, RecordTypeOutcome } from "../outbox/stage-sync-checkpoint.ts";

const REASON_MESSAGES: Readonly<Record<OutcomeReason, string>> = {
  "collection-disabled": "collection disabled",
  "projection-refusal": "record projection blocked",
  "record-too-large": "record exceeds the delivery limit",
  "permission-missing": "Health Connect permission missing",
  "delivery-blocked": "delivery blocked",
  "storage-pressure": "storage pressure; free device storage",
  "screen-lock-removed": "secure screen lock required",
  "changes-token-expired": "source continuity lost; sync again",
  "time-budget-reached": "time budget reached; sync again",
  "read-failed": "Health Connect read failed; will retry",
};

function disposition(entry: RecordTypeOutcome): "success" | "attention" | "retry" {
  if (entry.reason === "read-failed") return "retry";
  if (entry.reason !== undefined && entry.reason !== "time-budget-reached" &&
      entry.reason !== "changes-token-expired") return "attention";
  switch (entry.outcome) {
    case "no-change":
    case "completed":
    case "not-due":
    case "progressed":
    case "interrupted":
      return "success";
    default:
      return "attention";
  }
}

/** Only allowlisted operational text; never include record types, values, or raw failures. */
export function resultMessage(result: ShellRunResult): string {
  if (result.outcome === "skipped") return "Sync waits for a confirmed enrollment.";
  if (result.outcome === "failed") return "Sync could not finish.";
  const attention = result.summary.recordTypes.filter((entry) => disposition(entry) === "attention");
  const retry = result.summary.recordTypes.some((entry) => disposition(entry) === "retry")
    ? `Sync incomplete: ${REASON_MESSAGES["read-failed"]}.` : "";
  if (attention.length === 0) return retry || "Sync finished.";
  const conditions = attention.map((entry) => {
    if (entry.outcome === "not-permitted") return REASON_MESSAGES["permission-missing"];
    if (entry.reason) return REASON_MESSAGES[entry.reason];
    return entry.outcome === "failed" ? "source read failed; sync again" : "sync blocked";
  });
  const message = `Sync needs attention: ${[...new Set(conditions)].join("; ")}.`;
  return retry ? `${message} ${retry}` : message;
}
