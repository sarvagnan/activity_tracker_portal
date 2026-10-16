const { withAndroidManifest } = require("expo/config-plugins");
const permissions = require("../modules/health-connect/health-permissions.json");

const rationaleActivity = "org.activitytracker.healthconnect.PermissionsRationaleActivity";

// Declaration only: enrollment owns the interactive permission request flow.
module.exports = function withHealthConnect(config) {
  return withAndroidManifest(config, (config) => {
    const manifest = config.modResults.manifest;
    manifest["uses-permission"] = [
      ...(manifest["uses-permission"] || [])
        .filter((entry) => !permissions.includes(entry.$["android:name"])),
      ...permissions.map((permission) => ({ $: { "android:name": permission } })),
    ];
    const application = manifest.application?.[0];
    if (!application) throw new Error("Health Connect requires an Android application");
    application.activity = [
      ...(application.activity || []).filter((entry) => entry.$["android:name"] !== rationaleActivity),
      {
        $: { "android:name": rationaleActivity, "android:exported": "true" },
        "intent-filter": [{
          action: [{ $: { "android:name": "androidx.health.ACTION_SHOW_PERMISSIONS_RATIONALE" } }],
        }],
      },
    ];
    application["activity-alias"] = [
      ...(application["activity-alias"] || [])
        .filter((entry) => entry.$["android:name"] !== "ViewPermissionUsageActivity"),
      {
        $: {
          "android:name": "ViewPermissionUsageActivity",
          "android:exported": "true",
          "android:targetActivity": rationaleActivity,
          "android:permission": "android.permission.START_VIEW_PERMISSION_USAGE",
        },
        "intent-filter": [{
          action: [{ $: { "android:name": "android.intent.action.VIEW_PERMISSION_USAGE" } }],
          category: [{ $: { "android:name": "android.intent.category.HEALTH_PERMISSIONS" } }],
        }],
      },
    ];
    return config;
  });
};
