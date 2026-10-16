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
  "read-failed": "Health Connect read failed; sync again",
};

function successful(entry: RecordTypeOutcome): boolean {
  return entry.outcome === "no-change" || entry.outcome === "progressed" ||
    entry.outcome === "completed" || entry.outcome === "not-due";
}

/** Only allowlisted operational text; never include record types, values, or raw failures. */
export function resultMessage(result: ShellRunResult): string {
  if (result.outcome === "skipped") return "Sync waits for a confirmed enrollment.";
  if (result.outcome === "failed") return "Sync could not finish.";
  const attention = result.summary.recordTypes.filter((entry) => !successful(entry));
  if (attention.length === 0) return "Sync finished.";
  const conditions = attention.map((entry) => {
    if (entry.outcome === "not-permitted") return REASON_MESSAGES["permission-missing"];
    if (entry.reason) return REASON_MESSAGES[entry.reason];
    return entry.outcome === "interrupted" ? "sync interrupted; sync again"
      : entry.outcome === "failed" ? "source read failed; sync again" : "sync blocked";
  });
  return `Sync needs attention: ${[...new Set(conditions)].join("; ")}.`;
}
