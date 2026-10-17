import assert from "node:assert/strict";
import { test } from "node:test";
import type { EnrollmentStatus } from "../src/enrollment/manage-sender-enrollment.ts";
import { OPERATOR_CONDITIONS } from "../src/notifications/notify-operator-conditions.ts";
import { permissionState, resetAcknowledgement, senderScreenModel, typedOffer } from "../src/screens/sender-screen-model.ts";

const status: EnrollmentStatus = {
  installationId: "private-installation", keyAlias: "private-key", portalUrl: "https://private.invalid?code=secret",
  fingerprint: "compare-this-fingerprint", phase: "enrolled", refusal: null, confirmed: false,
  stagingContext: null, deliveryContext: { installationId: "private-installation", keyAlias: "private-key", portalUrl: "secret" },
  finalSequence: null,
};
function model(current: EnrollmentStatus | null | { outcome: "storage-failed" } | undefined = status, acknowledgedLoss = false, portalRetired = false) {
  return senderScreenModel({ status: current, conditions: [], failure: null, sync: null, permission: "missing", acknowledgedLoss, portalRetired });
}
test("typed enrollment trims fields and accepts only an http(s) URL and nonempty code", () => {
  assert.deepEqual(typedOffer("  https://portal.invalid/base  ", "  code  ").offer,
    { kind: "typed", portalUrl: "https://portal.invalid/base", enrollmentCode: "code" });
  assert.ok(typedOffer("http://portal.invalid", "code").offer);
  for (const url of ["", "portal.invalid", "file:///tmp/secret", "javascript:secret", "ftp://portal.invalid"]) {
    assert.equal(typedOffer(url, "code").offer, null);
  }
  assert.equal(typedOffer("https://portal.invalid", " \t ").offer, null);
  // The unit owns restrictions on credentials, query strings, fragments, and code format.
  assert.ok(typedOffer("https://user:pass@portal.invalid?secret=1#part", "arbitrary code").offer);
});
test("every lifecycle state has its screen and only the eligible lifecycle actions", () => {
  assert.equal(senderScreenModel({ status: undefined, conditions: [], failure: null, sync: null, permission: "unknown", acknowledgedLoss: false, portalRetired: false }).phase, "loading");
  assert.equal(model({ outcome: "storage-failed" }).phase, "unavailable");
  assert.deepEqual(model(null).actions, ["enroll"]);
  assert.deepEqual(model({ ...status, phase: "confirming" }).actions, ["confirm"]);
  assert.deepEqual(model().actions, []);
  assert.equal(model().phase, "enrolled");
  assert.deepEqual(model({ ...status, confirmed: true }).actions, ["beginDrain"]);
  assert.equal(model({ ...status, confirmed: true }).phase, "confirmed");
  assert.deepEqual(model({ ...status, phase: "draining" }).actions, []);
  assert.equal(model({ ...status, phase: "draining" }).finalSequence, null);
  const drained = { ...status, phase: "draining", finalSequence: 0 } as const;
  assert.equal(model(drained).finalSequence, "Final sequence: 0");
  assert.deepEqual(model(drained).actions, []);
  assert.deepEqual(model(drained, false, true).actions, ["unlink"]);
  assert.deepEqual(model({ ...drained, finalSequence: null }, false, true).actions, []);
});
test("fingerprint and both Portal refusal reasons are shown without private context", () => {
  assert.equal(model().fingerprint, status.fingerprint);
  for (const refusal of ["enrollment-refused", "capabilities-unsupported"] as const) {
    const view = model({ ...status, phase: "refused", refusal });
    assert.equal(view.phase, "refused");
    assert.ok(view.refusal);
    assert.doesNotMatch(JSON.stringify(view), /private|secret/);
  }
});
test("destructive reset requires explicit acknowledgement in every non-loading state", () => {
  assert.equal(resetAcknowledgement(false), null);
  assert.deepEqual(resetAcknowledgement(true), { acknowledgePossibleLoss: true });
  for (const current of [null, { outcome: "storage-failed" } as const, status,
    ...["confirming", "refused", "draining"].map((phase) => ({ ...status, phase } as EnrollmentStatus))]) {
    assert.ok(!model(current).actions.includes("reset"));
    assert.ok(model(current, true).actions.includes("reset"));
  }
});
test("status screen shows active conditions, latest Sync result and a fixed failure observation", () => {
  const view = senderScreenModel({ status, conditions: OPERATOR_CONDITIONS.map((condition) => ({ condition, firstSeenMs: 1, lastSeenMs: 1, notified: true })),
    failure: { kind: "delivery", failure: "storage-failed", timeMs: 0 }, sync: { outcome: "failed", failure: "private-health-data" },
    permission: "missing", acknowledgedLoss: false, portalRetired: false });
  assert.equal(view.conditionTexts.length, OPERATOR_CONDITIONS.length);
  assert.ok(view.conditionTexts.every((text) => typeof text === "string"));
  assert.equal(view.syncText, "Sync could not finish.");
  assert.equal(view.failureText, "Delivery: Sender storage is unavailable.");
  assert.equal(view.failureTime, "Observed at 1970-01-01T00:00:00.000Z");
  assert.doesNotMatch(JSON.stringify(view), /private|secret/);
  assert.match(senderScreenModel({ status: null, conditions: { outcome: "storage-failed" }, failure: null, sync: null,
    permission: "unavailable", acknowledgedLoss: false, portalRetired: false }).conditionTexts[0], /could not be read/);
});
test("permission model distinguishes provider availability, partial access, and full access", () => {
  const requested = ["READ_STEPS", "READ_HEALTH_DATA_HISTORY", "READ_HEALTH_DATA_IN_BACKGROUND"];
  assert.equal(permissionState(1, [], requested), "unavailable");
  assert.equal(permissionState(2, [], requested), "update-required");
  assert.equal(permissionState(3, ["READ_STEPS"], requested), "missing");
  assert.equal(permissionState(3, requested, requested), "granted");
  for (const permission of ["unavailable", "update-required", "unknown", "missing", "granted", "failed"] as const) {
    const view = senderScreenModel({ status: null, conditions: [], failure: null, sync: null, permission, acknowledgedLoss: false, portalRetired: false });
    assert.ok(view.permissionText);
    assert.equal(view.canRequestPermissions, ["missing", "granted", "failed"].includes(permission));
  }
});
