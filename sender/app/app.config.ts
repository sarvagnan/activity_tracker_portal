export default {
  expo: {
    name: "Activity Tracker Sender",
    slug: "activity-tracker-sender",
    version: "0.1.0",
    platforms: ["android"],
    android: {
      package: "org.activitytracker.sender",
      allowBackup: false,
      permissions: ["android.permission.POST_NOTIFICATIONS"],
    },
    plugins: [
      ["expo-dev-client", { addGeneratedScheme: false }],
      ["expo-build-properties", {
        android: {
          minSdkVersion: 34,
          compileSdkVersion: 36,
          targetSdkVersion: 36,
          buildToolsVersion: "36.0.0",
          kotlinVersion: "2.1.20",
        },
      }],
      "./plugins/with-pinned-android.cjs",
      "./plugins/with-background-scheduling.cjs",
      "./plugins/with-health-connect.cjs",
      "./plugins/with-portal-network-security.cjs",
    ],
  },
};
