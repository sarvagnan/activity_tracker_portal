# Sender workspace

Android-only Expo SDK 57 development shell. Use a development build and local
Metro, never Expo Go or EAS. The screen is a placeholder; the native module only
registers and declares a typed Health Connect boundary. Snapshot state, record
reading, projection, permissions, enrollment, storage, and scheduling belong to
later governed units. JVM fixtures contain only empty pages, synthetic tokens,
and synthetic failures. Hosted CI does not qualify physical-device behavior.

Run these commands from the repository/worktree root with Node 26.11.1 and npm
11.20.0 (the workspace checks these versions):

```sh
npm ci --prefix sender
npm run typecheck --prefix sender
npm test --prefix sender
node --test sender/protocol/test/ingest-protocol-1.test.ts
npm run prebuild --prefix sender
npm run test:kotlin --prefix sender
npm run build:android --prefix sender
```

`npm test` runs the app's plain TypeScript `node:test` seam and the existing
protocol suite using Node 26 type stripping. Put durable Sender modules in
`app/src/` and their Node tests in `app/test/`; those modules must remain usable
without loading React Native or Expo. The app imports the unchanged protocol
package through npm workspaces. Kotlin's `HealthConnectAccess` uses the pinned
SDK request/response types; its test-source fake supplies responses through the
same interface without a device, emulator, Android context, or actual reads.
`test:kotlin` runs `:health-connect:testDebugUnitTest` and prints the test count.

## Native generation and toolchains

Continuous Native Generation keeps `app/android/` ignored. Prebuild packs the
lockfile-installed `expo-template-bare-minimum` 57.0.30 into an ignored local
tarball, so generation never follows a moving template tag. The config plugin
reapplies the Gradle wrapper checksum, explicit AGP/Kotlin classpaths, and JDK
17 toolchains. Do not edit generated Android files; change the config/plugin or
project-owned module and regenerate instead.

Pins:

- `app/package.json` and `package-lock.json`: Expo 57.0.28, React Native 0.86.3,
  React 19.2.3, expo-dev-client 57.0.20, expo-modules-core 57.0.22,
  expo-build-properties 57.0.22, template 57.0.30, TypeScript 5.9.3,
  @types/react 19.2.18, and @types/node 26.6.5. The protocol workspace is 0.1.0.
- `app/app.config.ts`: Android minimum API 34, compile/target API 36,
  build-tools 36.0.0, Kotlin 2.1.20, and backup disabled.
- `app/plugins/with-pinned-android.cjs`: AGP 8.12.0, Kotlin 2.1.20, and Gradle
  Java toolchains restricted to Adoptium JDK 17. The locked React Native catalog
  also fixes NDK 27.1.12297006.
- `toolchain/gradle-wrapper.properties`: Gradle 9.3.1 and its distribution SHA-256.
  The wrapper launcher/JAR come from the locked native template.
- `scripts/jdk.sh`: Temurin 17.0.20.1+1, exact release URLs and SHA-256 values
  for macOS arm64 and Linux x86_64. It verifies before extracting under
  `sender/.cache/`; no system JDK installation or floating resolver is used.
- `app/modules/health-connect/android/build.gradle`: stable Health Connect
  connect-client 1.1.0 and JUnit 4.13.2.

`gradle.sh` provisions that JDK before invoking the wrapper, so host JDK 27 is
never used by Android tooling. Gradle toolchain auto-detection/download is off;
only the provisioned JDK is eligible. Gradle caches also stay in `sender/.cache/`.
An existing Android SDK with accepted licenses is required. Set `ANDROID_HOME`
(or `ANDROID_SDK_ROOT`); macOS defaults to `$HOME/Library/Android/sdk`.
AGP may download missing SDK/NDK packages under that SDK using accepted licenses.
The CI runner uses its existing Android SDK and the same project-local JDK script.
No cmdline-tools, EAS account, cloud build, or system JDK install is required.

The config plugin removes the template's debug signing configuration and
keystore. `build:android` produces an **unsigned development APK** at
`sender/app/android/app/build/outputs/apk/debug/app-debug-unsigned.apk`.
Operator-managed signing and physical-device installation are later work; this
workspace creates no release keystore or signing keys. After separately signing
and installing a development APK, start local Metro with:

```sh
npm run start --workspace @activity-tracker-portal/sender --prefix sender
```
