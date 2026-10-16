import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { URL, fileURLToPath } from "node:url";
import test from "node:test";
import type { AndroidManifest, ExportedConfig } from "expo/config-plugins";
import appConfig from "../app.config.ts";

const require = createRequire(import.meta.url);
const withHealthConnect: (config: ExportedConfig) => ExportedConfig = require("../plugins/with-health-connect.cjs");
const permissions: string[] = JSON.parse(readFileSync(
  new URL("../modules/health-connect/health-permissions.json", import.meta.url), "utf8",
));
const rationaleActivity = "org.activitytracker.healthconnect.PermissionsRationaleActivity";

async function apply(manifest: AndroidManifest = {
  manifest: { $: { "xmlns:android": "http://schemas.android.com/apk/res/android" }, queries: [], application: [{ $: { "android:name": ".MainApplication" } }] },
}): Promise<AndroidManifest> {
  const base = { name: "Synthetic Sender", slug: "synthetic-sender" };
  const config = withHealthConnect(base);
  const mod = config.mods!.android!.manifest!;
  const projectRoot = fileURLToPath(new URL("..", import.meta.url));
  return (await mod({
    ...config, modResults: manifest, modRawConfig: base,
    modRequest: { projectRoot, platformProjectRoot: `${projectRoot}/android`,
      platform: "android", modName: "manifest", introspect: true },
  })).modResults;
}

test("registered Health Connect plugin emits exactly the checked-in permission inventory", async () => {
  assert.ok(appConfig.expo.plugins.includes("./plugins/with-health-connect.cjs"));
  const manifest = (await apply()).manifest;
  assert.deepEqual(manifest["uses-permission"]!.map((entry) => entry.$["android:name"]).sort(), [...permissions].sort());
  assert.equal(new Set(permissions).size, permissions.length);
  assert.ok(permissions.every((permission) => permission.startsWith("android.permission.health.READ_")));
  assert.ok(permissions.includes("android.permission.health.READ_MINDFULNESS"));
  assert.ok(permissions.includes("android.permission.health.READ_HEALTH_DATA_HISTORY"));
  assert.ok(permissions.includes("android.permission.health.READ_HEALTH_DATA_IN_BACKGROUND"));
});

test("Health Connect can resolve the rationale activity and protected Android 14 usage alias", async () => {
  const application = (await apply()).manifest.application![0]!;
  assert.deepEqual(application.activity, [{
    $: { "android:name": rationaleActivity, "android:exported": "true" },
    "intent-filter": [{
      action: [{ $: { "android:name": "androidx.health.ACTION_SHOW_PERMISSIONS_RATIONALE" } }],
    }],
  }]);
  assert.deepEqual(application["activity-alias"], [{
    $: {
      "android:name": "ViewPermissionUsageActivity", "android:exported": "true",
      "android:targetActivity": rationaleActivity,
      "android:permission": "android.permission.START_VIEW_PERMISSION_USAGE",
    },
    "intent-filter": [{
      action: [{ $: { "android:name": "android.intent.action.VIEW_PERMISSION_USAGE" } }],
      category: [{ $: { "android:name": "android.intent.category.HEALTH_PERMISSIONS" } }],
    }],
  }]);
});

test("Health Connect manifest additions are idempotent and preserve other permissions and activities", async () => {
  const original: AndroidManifest = { manifest: {
    $: { "xmlns:android": "http://schemas.android.com/apk/res/android" },
    queries: [],
    "uses-permission": [
      { $: { "android:name": "android.permission.POST_NOTIFICATIONS" } },
      { $: { "android:name": permissions[0]! } },
    ],
    application: [{
      $: { "android:name": ".MainApplication" },
      activity: [{ $: { "android:name": ".MainActivity" } }],
      "activity-alias": [{ $: { "android:name": "ExistingAlias", "android:targetActivity": ".MainActivity" } }],
    }],
  } };
  const once = await apply(original);
  const snapshot = structuredClone(once);
  const twice = await apply(once);
  assert.deepEqual(twice, snapshot);
  assert.deepEqual(twice.manifest["uses-permission"]!.map((entry) => entry.$["android:name"]).sort(),
    [...permissions, "android.permission.POST_NOTIFICATIONS"].sort());
  const application = twice.manifest.application![0]!;
  assert.equal(application.activity!.length, 2);
  assert.deepEqual(application.activity![0], { $: { "android:name": ".MainActivity" } });
  assert.equal(application["activity-alias"]!.length, 2);
  assert.equal(application["activity-alias"]![0]!.$!["android:name"], "ExistingAlias");
});
