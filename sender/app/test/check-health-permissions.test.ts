import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { URL } from "node:url";
import type { HealthPermissions } from "../src/native-health-permissions.ts";
import { checkHealthPermissions } from "../src/screens/check-health-permissions.ts";

const inventory: string[] = JSON.parse(readFileSync(new URL("../modules/health-connect/health-permissions.json", import.meta.url), "utf8"));
const history = "android.permission.health.READ_HEALTH_DATA_HISTORY";
const background = "android.permission.health.READ_HEALTH_DATA_IN_BACKGROUND";
const reads = inventory.filter((permission) => permission !== history && permission !== background);

test("permission requests and checks use feature availability and retain every record read permission", async () => {
  for (const historyStatus of [1, 2]) for (const backgroundStatus of [1, 2]) for (const request of [false, true]) {
    const calls: string[] = [];
    const expected = inventory.filter((permission) => (permission !== history || historyStatus === 2)
      && (permission !== background || backgroundStatus === 2));
    const native: HealthPermissions = {
      async sdkStatus() { calls.push("sdk"); return 3; },
      async featureStatuses() {
        calls.push("features");
        return { READ_HEALTH_DATA_HISTORY: historyStatus, READ_HEALTH_DATA_IN_BACKGROUND: backgroundStatus };
      },
      async grantedPermissions() { calls.push("granted"); return expected; },
      async requestPermissions(permissions) {
        calls.push("request");
        assert.deepEqual(permissions, expected);
        assert.ok(reads.every((permission) => permissions.includes(permission)));
        return permissions;
      },
    };
    assert.equal(await checkHealthPermissions(native, inventory, request), expected.length === inventory.length ? "granted" : "features-unavailable");
    assert.deepEqual(calls, ["sdk", "features", request ? "request" : "granted"]);
    native.grantedPermissions = native.requestPermissions = async () => [];
    assert.equal(await checkHealthPermissions(native, inventory, request), "missing");
  }
});

test("provider unavailable or update-required never checks features or requests permissions", async () => {
  for (const sdk of [1, 2]) for (const request of [false, true]) {
    const unexpected = async (): Promise<never> => { assert.fail("SDK unavailable"); };
    assert.equal(await checkHealthPermissions({ sdkStatus: async () => sdk, featureStatuses: unexpected,
      grantedPermissions: unexpected, requestPermissions: unexpected }, inventory, request), sdk === 1 ? "unavailable" : "update-required");
  }
});

test("SDK, feature, grant, and request failures become failed without exposing native error text", async () => {
  for (const failed of ["sdkStatus", "featureStatuses", "grantedPermissions", "requestPermissions"] as const) {
    const native: HealthPermissions = {
      async sdkStatus() { return 3; },
      async featureStatuses() { return { READ_HEALTH_DATA_HISTORY: 2, READ_HEALTH_DATA_IN_BACKGROUND: 2 }; },
      async grantedPermissions() { return inventory; },
      async requestPermissions() { return inventory; },
    };
    native[failed] = async () => { throw new Error("private native failure"); };
    assert.equal(await checkHealthPermissions(native, inventory, failed === "requestPermissions"), "failed", failed);
  }
});

test("invalid feature statuses fail before asking for permissions", async () => {
  const unexpected = async (): Promise<never> => { assert.fail("invalid feature status"); };
  assert.equal(await checkHealthPermissions({ sdkStatus: async () => 3,
    featureStatuses: async () => ({ READ_HEALTH_DATA_HISTORY: 0, READ_HEALTH_DATA_IN_BACKGROUND: 2 }),
    grantedPermissions: unexpected, requestPermissions: unexpected }, inventory, true), "failed");
});
