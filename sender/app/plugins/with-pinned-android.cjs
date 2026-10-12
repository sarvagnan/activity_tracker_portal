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
    if (!contents.includes("// Sender JDK toolchain")) {
      contents += `
// Sender JDK toolchain: the launcher provisions the exact Temurin patch locally.
subprojects { project ->
  project.plugins.withId('org.jetbrains.kotlin.android') {
    project.kotlin.jvmToolchain {
      languageVersion = JavaLanguageVersion.of(17)
      vendor = JvmVendorSpec.ADOPTIUM
    }
  }
}
`;
    }
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
