# Sender workspace

Android-only Expo SDK 57 development shell. Use a development build and local
Metro, never Expo Go or EAS. The screen is a placeholder. Project-owned native
modules expose typed Health Connect and installation-key boundaries. Storage and
Keystore mechanisms have synthetic CI substitutes. Snapshot state, record
reading, permissions, enrollment, and scheduling belong to later governed units.
Tests use only synthetic fixtures. Hosted CI does not qualify physical-device
behavior.

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
`test:kotlin` runs `:health-connect:testDebugUnitTest` and
`:installation-key:testDebugUnitTest`, printing each module's test count. The
existing CI Sender job runs both through that script; no new CI action is needed.

## Native generation and toolchains

Continuous Native Generation keeps `app/android/` ignored. Prebuild packs the
lockfile-installed `expo-template-bare-minimum` 57.0.30 into an ignored local
tarball, so generation never follows a moving template tag. The config plugin
reapplies the Gradle wrapper checksum and explicit AGP/Kotlin classpaths.
React Native configures JDK 17 toolchains; the launcher restricts their resolution
to the pinned local JDK. Do not edit generated Android files; change the
config/plugin or project-owned module and regenerate instead.

Pins:

- `app/package.json` and `package-lock.json`: Expo 57.0.28, React Native 0.86.3,
  React 19.2.3, expo-dev-client 57.0.20, expo-modules-core 57.0.22,
  expo-sqlite 57.0.4 (SDK 57's bundledNativeModules entry, made exact),
  expo-build-properties 57.0.22, template 57.0.30, TypeScript 5.9.3,
  @types/react 19.2.18, and @types/node 26.6.5. The protocol workspace is 0.1.0.
  The lockfile also fixes expo-sqlite's new transitive await-lock at 2.2.2.
- `app/app.config.ts`: Android minimum API 34, compile/target API 36,
  build-tools 36.0.0, Kotlin 2.1.20, and backup disabled.
- `app/plugins/with-pinned-android.cjs`: AGP 8.12.0 and Kotlin 2.1.20. The locked
  React Native catalog also fixes NDK 27.1.12297006.
- `toolchain/gradle-wrapper.properties`: Gradle 9.3.1 and its distribution SHA-256.
  The wrapper launcher/JAR come from the locked native template.
- `scripts/jdk.sh`: Temurin 17.0.20.1+1, exact release URLs and SHA-256 values
  for macOS arm64 and Linux x86_64. It verifies before extracting under
  `sender/.cache/`; no system JDK installation or floating resolver is used.
- `app/modules/health-connect/android/build.gradle`: stable Health Connect
  connect-client 1.1.0 and JUnit 4.13.2.
- `app/modules/installation-key/android/build.gradle`: JUnit 4.13.2 and
  kotlinx-serialization-json 1.8.1 for synthetic JVM tests, matching versions
  already used by Health Connect. The new project-owned module is 0.1.0.

`gradle.sh` provisions that JDK before invoking the wrapper and sets both
`JAVA_HOME` and `org.gradle.java.home` to it. Gradle toolchain auto-detection and
auto-download are off; the sole installation path is the provisioned JDK. These
settings use system properties so they also apply to included plugin builds.
Java/Kotlin compilation and JVM tests therefore use the same pinned JDK without
rewriting toolchains after React Native finalizes them. Gradle caches also stay
in `sender/.cache/`.
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

## Durable storage seam

`app/src/storage/database.ts` defines `SenderDatabase`: parameterized `execute`
and `query`, an atomic `transaction(callback)`, and `close`. Values are null,
text, safe integer numbers (including the protocol bound `2^53 - 1`), and
`Uint8Array` BLOBs. Fractional and unsafe numbers are rejected on both writes
and reads. `test/support/node-database.ts` uses Node 26's built-in SQLite with
integer reads enabled as BigInts before checked conversion to numbers. It also
binds checked numbers as SQLite INTEGERs, matching Expo Android rather than
Node's default REAL binding for JavaScript numbers.

`app/src/storage/expo-sqlite.ts` is the production-only adapter; plain Node tests
never import it or load Expo/React Native. It opens a dedicated connection with
`useNewConnection: true`. Both adapters use `connection.ts` to apply and verify
`journal_mode=DELETE`, `synchronous=FULL`, and `foreign_keys=ON` at every open.
The Node seam takes a file path; an in-memory database cannot supply the required
DELETE journal and fails on open. SQLite/provider open errors propagate without
reset or fallback.

Use and await the callback's transaction handle for every statement. Nested
transactions, overlapping root operations, root use during a transaction, and
use of an expired callback handle throw as programming errors. The connection
owns transaction control; callers must not issue BEGIN/COMMIT/ROLLBACK themselves
or launch unawaited statements. The wrapper attempts to roll back DDL and data
when the callback throws/rejects or COMMIT fails. If ROLLBACK also fails, the
original callback or COMMIT error propagates and the operation guard is released.
Coordination and scheduling stay with the governed consumers.

`migrations.ts` exposes an ordered, contiguous migration runner. Each migration
and its `PRAGMA user_version` bump commit in one transaction. Failure returns
only `{status: "frozen", version, reason: "migration-failed"}`; a newer schema
returns `newer-schema` without writing. There is no drop/reset/recreate path or
exception-text disclosure. Up-to-date reopens are no-ops. The production
`senderMigrations` list is empty, so this task ships schema version 0 and no
bookkeeping, outbox, sequence, cursor, snapshot, enrollment, or other governed
tables. Tests alone supply synthetic migrations. Later governed units own their
migrations and all reactions to frozen results.

The pinned expo-sqlite Android build compiles its own
`vendor/sqlite3/sqlite3.c`; the accompanying header reports **SQLite 3.50.3**.
This is inside the WAL-reset corruption range described by ADR 0016 and
[SQLite's WAL documentation](https://sqlite.org/wal.html#walresetbug) (fixed in
3.51.3; also backported to 3.50.7 and 3.44.6). The bug requires WAL mode and
overlapping write/checkpoint work on two or more connections. Sender foreground
and background contexts cannot rule that out. Both adapters therefore require
the DELETE rollback journal, which is unaffected by the WAL-reset bug, while
retaining FULL synchronization and foreign keys. WAL may be reconsidered once
the pinned expo-sqlite bundles SQLite 3.51.3 or later, or a fixed backport.

## Installation-key seam

`app/src/installation-cipher.ts` defines `InstallationCipher` without native
imports. `native-installation-cipher.ts` binds it to the separate local Expo
module `modules/installation-key` (`org.activitytracker.installationkey`).
`InstallationKeyStore` is the equivalent Kotlin interface. Operations create an
alias without replacement, report present/missing/unusable/unavailable status,
seal/open bytes with caller-supplied AAD, and explicitly delete a key. Failures
are typed:
`missing-key`, `unusable-key`, `keystore-unavailable`, `authentication-failed`, and
`unsupported-format`; create additionally returns `key-exists` for a duplicate
alias. Delete can remove an unusable key, and returns `missing-key` for an absent
alias. Malformed box validation precedes key lookup. The device-security probe is a plain
`KeyguardManager.isDeviceSecure` observation with no policy.

AndroidKeyStore creates non-exportable AES-256 keys for encrypt/decrypt only,
GCM/NoPadding with a 128-bit tag, randomized encryption required, and a
provider-generated 12-byte IV. Both user authentication and unlocked-device
requirements are explicitly false. StrongBox is explicitly disabled, and create
reports `strongBox: false`; hardware-backed storage is not promised. Key status
probes encryption initialization as well as retrieval so an invalidated key can
be reported unusable. Only permanently invalidated/unrecoverable keys or entries
that are not AES SecretKeys report `unusable-key` / `unusable`. Other security,
provider, and I/O failures report `keystore-unavailable` / `unavailable` through
a shared, JVM-tested exception classifier, preserving the distinction between
transient outages and ADR 0014's retirement condition. Authentication/tag failures
remain `authentication-failed`. No software-key fallback is available in
production.

Sealed box version 1 is `0x01 || 12-byte IV || ciphertext || 16-byte tag`.
AAD is never stored. Empty plaintext is valid. Unknown version bytes return
`unsupported-format`; empty/truncated input, tampering, and wrong AAD return
`authentication-failed`, without exposing partial plaintext or exception text.
The Expo API accepts `Uint8Array` (including offset views) and returns byte-array
success values as `Uint8Array`.

`test/support/node-installation-cipher.ts` uses Node aes-256-gcm and ephemeral
in-memory keys. The Kotlin test-source `SyntheticInstallationKeyStore` uses
javax.crypto AES/GCM/NoPadding. Both reproduce and open the same obviously
synthetic `test/fixtures/sealed-box-v1.json` with fixed key/IV/AAD/plaintext and
expected sealed bytes, and test tampering, wrong AAD, unknown versions,
truncation, missing/unusable keys, and duplicate aliases. Test substitutes may
inject entropy, invalidate an in-memory key, or toggle a transient outage without
changing the key; recovery tests decrypt previously sealed bytes. Those features
never reach the Android implementation. The Node top-level test glob already
includes the new suites; support files and fixtures are imported without being
standalone tests.

## Physical-device qualification still required

Hosted CI compiles AndroidKeyStore but runs only synthetic Node/JVM tests.
Before native release, qualify these primitives on a physical device (ADR 0036):

- Expo byte-array bridging, including offset views, empty arrays, and byte-exact
  BLOB round trips; native SQLite version and all three connection settings.
- Atomic persistence and migration rollback across process death/restart and
  device reboot, with only synthetic stored bytes.
- Alias persistence across process restart, duplicate-create refusal,
  non-exportability, AES-256/GCM parameters, 12-byte randomized IVs, 128-bit tags,
  and the reported absence of StrongBox use.
- Seal/open while securely locked, after unlocking, and after secure-lock
  removal; report the security probe observation without adding lifecycle policy.
- Missing/deleted and unusable/invalidated key outcomes, wrong AAD and tampered
  boxes, and a v1 box remaining readable after an app upgrade.

Use no device identifiers, credentials, signing keys, or personal health data in
fixtures or qualification reports. This task adds no device/emulator test and
makes no claim that CI proves Android Keystore, filesystem durability, or OS lock
behavior.
