import assert from "node:assert/strict";
import { test } from "node:test";
import type { EnrollmentStatus } from "../src/enrollment/manage-sender-enrollment.ts";
import { OPERATOR_CONDITIONS } from "../src/notifications/notify-operator-conditions.ts";
import { permissionState, requestableHealthPermissions, resetAcknowledgement, senderScreenModel, typedOffer } from "../src/screens/sender-screen-model.ts";

const status: EnrollmentStatus = {
  installationId: "private-installation", keyAlias: "private-key", portalUrl: "https://private.invalid?code=secret",
  fingerprint: "compare-this-fingerprint", phase: "enrolled", refusal: null, confirmed: false,
  stagingContext: null, deliveryContext: { installationId: "private-installation", keyAlias: "private-key", portalUrl: "secret" },
  finalSequence: null,
};
function model(current: EnrollmentStatus | null | { outcome: "storage-failed" } | undefined = status, acknowledgedLoss = false, portalRetired = false) {
  return senderScreenModel({ status: current, conditions: [], failure: null, sync: null, permission: "missing", acknowledgedLoss, portalRetired });
}
test("typed enrollment trims fields and accepts https or Tailscale-only http with a nonempty code", () => {
  assert.deepEqual(typedOffer("  https://portal.invalid/base  ", "  code  ").offer,
    { kind: "typed", portalUrl: "https://portal.invalid/base", enrollmentCode: "code" });
  for (const url of ["http://portal.tail1234.ts.net", "http://ts.net", "http://PORTAL.TS.NET:8000/base", "https://portal.local"]) {
    assert.ok(typedOffer(url, "code").offer, url);
  }
  for (const url of ["", "portal.invalid", "file:///tmp/secret", "javascript:secret", "ftp://portal.invalid",
    "http://portal.local", "http://evilts.net", "http://ts.net.example.com", "http://10.0.2.2:8000"]) {
    assert.equal(typedOffer(url, "code").offer, null, url);
    assert.match(typedOffer(url, "code").error!, /https.*http only.*ts\.net/);
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
  const requested = ["READ_STEPS", "READ_HEALTH_DATA_HISTORY", "READ_HEALTH_DATA_IN_BACKGROUND"].map((name) => `android.permission.health.${name}`);
  const features = { READ_HEALTH_DATA_HISTORY: 2, READ_HEALTH_DATA_IN_BACKGROUND: 2 };
  assert.equal(permissionState(1, [], requested), "unavailable");
  assert.equal(permissionState(2, [], requested), "update-required");
  assert.equal(permissionState(3, [requested[0]], requested, features), "missing");
  assert.equal(permissionState(3, requested, requested, features), "granted");
  assert.equal(permissionState(3, requested, requested), "failed", "feature check must complete");
  assert.equal(permissionState(3, requested, requested, { ...features, READ_HEALTH_DATA_HISTORY: 0 }), "failed");
  for (const permission of ["unavailable", "update-required", "features-unavailable", "unknown", "missing", "granted", "failed"] as const) {
    const view = senderScreenModel({ status: null, conditions: [], failure: null, sync: null, permission, acknowledgedLoss: false, portalRetired: false });
    assert.ok(view.permissionText);
    assert.equal(view.canRequestPermissions, ["missing", "granted", "failed"].includes(permission));
    if (permission === "features-unavailable") {
      assert.match(view.permissionText, /Full-history or background access.*Health Connect update/);
      assert.match(view.permissionText, /Android 14\+.*Google Play system update/);
    }
  }
});
test("feature availability filters only optional permissions and missing requestable access takes precedence", () => {
  const reads = ["android.permission.health.READ_STEPS", "android.permission.health.READ_WEIGHT"];
  const history = "android.permission.health.READ_HEALTH_DATA_HISTORY";
  const background = "android.permission.health.READ_HEALTH_DATA_IN_BACKGROUND";
  const requested = [...reads, history, background];
  for (const historyStatus of [1, 2]) for (const backgroundStatus of [1, 2]) {
    const features = { READ_HEALTH_DATA_HISTORY: historyStatus, READ_HEALTH_DATA_IN_BACKGROUND: backgroundStatus };
    const available = [...reads, ...(historyStatus === 2 ? [history] : []), ...(backgroundStatus === 2 ? [background] : [])];
    assert.deepEqual(requestableHealthPermissions(requested, features), available);
    const expected = historyStatus === 2 && backgroundStatus === 2 ? "granted" : "features-unavailable";
    assert.equal(permissionState(3, available, requested, features), expected);
    // Even a stale grant of an unavailable feature cannot imply full access.
    assert.equal(permissionState(3, requested, requested, features), expected);
    for (const missing of available) {
      assert.equal(permissionState(3, available.filter((permission) => permission !== missing), requested, features), "missing");
    }
  }
});
