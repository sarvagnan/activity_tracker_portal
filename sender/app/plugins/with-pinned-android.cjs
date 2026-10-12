const { withProjectBuildGradle, withAppBuildGradle, withDangerousMod } = require("expo/config-plugins");
const { copyFile, rm } = require("node:fs/promises");
const path = require("node:path");

module.exports = function withPinnedAndroid(config) {
  config = withProjectBuildGradle(config, (config) => {
    let contents = config.modResults.contents;
    contents = contents.replace(
      "classpath('com.android.tools.build:gradle')",
      "classpath('com.android.tools.build:gradle:8.12.0')",
    ).replace(
      "classpath('org.jetbrains.kotlin:kotlin-gradle-plugin')",
      "classpath('org.jetbrains.kotlin:kotlin-gradle-plugin:2.1.20')",
    );
    // React Native configures JDK 17 before evaluating Android libraries. Its root
    // plugin eagerly evaluates :app, so changing toolchains here is already too
    // late. gradle.sh restricts resolution to the checksum-verified local JDK.
    config.modResults.contents = contents;
    return config;
  });
  config = withAppBuildGradle(config, (config) => {
    // This enabling workspace produces unsigned development APKs only.
    config.modResults.contents = config.modResults.contents
      .replace(/    signingConfigs \{\n        debug \{[\s\S]*?\n        \}\n    \}\n/, "")
      .replaceAll("signingConfig signingConfigs.debug", "signingConfig null");
    return config;
  });
  return withDangerousMod(config, ["android", async (config) => {
    const android = config.modRequest.platformProjectRoot;
    await copyFile(
      path.join(config.modRequest.projectRoot, "../toolchain/gradle-wrapper.properties"),
      path.join(android, "gradle/wrapper/gradle-wrapper.properties"),
    );
    await rm(path.join(android, "app/debug.keystore"), { force: true });
    return config;
  }]);
};
