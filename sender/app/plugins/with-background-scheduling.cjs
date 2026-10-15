const { withAndroidManifest } = require("expo/config-plugins");

module.exports = function withBackgroundScheduling(config) {
  return withAndroidManifest(config, (config) => {
    const manifest = config.modResults.manifest;
    manifest.$["xmlns:tools"] = "http://schemas.android.com/tools";
    const permission = "android.permission.FOREGROUND_SERVICE";
    manifest["uses-permission"] = (manifest["uses-permission"] || [])
      .filter((entry) => entry.$["android:name"] !== permission);
    manifest["uses-permission"].push({ $: { "android:name": permission, "tools:node": "remove" } });

    // The pinned Expo worker only enqueues ordinary delayed work: it never calls
    // setForeground or requests expedited work. Remove the unused foreground
    // service as well as its permission; leave WorkManager's job service,
    // receivers, RECEIVE_BOOT_COMPLETED and WAKE_LOCK intact for rescheduling.
    const service = "androidx.work.impl.foreground.SystemForegroundService";
    for (const application of manifest.application || []) {
      application.service = (application.service || [])
        .filter((entry) => entry.$["android:name"] !== service);
      application.service.push({ $: { "android:name": service, "tools:node": "remove" } });
    }
    return config;
  });
};
