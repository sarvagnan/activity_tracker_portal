// Thin production boundary. Importing this file does not open storage or load a native module.
import { uuid } from "expo-modules-core";
import { createSenderEnrollment } from "../enrollment/manage-sender-enrollment.ts";
import { openSenderDatabase } from "../storage/expo-sqlite.ts";
import { nativeInstallationCipher } from "../native-installation-cipher.ts";
import { nativeSyncSources } from "../native-sync-sources.ts";
import { expoSchedulingNotifier, expoSchedulingPlatform } from "../background/expo-adapters.ts";
import { composeSender, PROJECTION_IMPLEMENTATION_VERSION } from "./compose-sender.ts";
import type { SenderShell } from "./compose-sender.ts";
import { nativeAvailableStorageBytes, nativeProjectionSchemaVersions } from "./native-observations.ts";

let sender: Promise<SenderShell> | null = null;

/** Shared by App and TaskManager, including a headless entry-point load. */
export function expoSender(): Promise<SenderShell> {
  sender ??= Promise.resolve().then(() => {
    const cipher = nativeInstallationCipher();
    return composeSender({
      openDatabase: () => openSenderDatabase("sender.db"),
      enrollment: (database) => {
        const observer = createSenderEnrollment({
          database, cipher, now: Date.now, randomUUID: () => uuid.v4(),
          // This boundary exposes status only; enrollment transport/screens belong to 2rv.8.
          transport: { async send() { throw new Error("Read-only enrollment observer"); } },
        });
        return { status: () => observer.status() };
      },
      cipher, sources: nativeSyncSources(PROJECTION_IMPLEMENTATION_VERSION),
      projectionSchemaVersions: nativeProjectionSchemaVersions,
      availableStorageBytes: nativeAvailableStorageBytes,
      platform: expoSchedulingPlatform(), notifier: expoSchedulingNotifier(),
      now: Date.now, monotonicNow: () => performance.now(), randomUUID: () => uuid.v4(),
    });
  }).catch((error: unknown) => { sender = null; throw error; });
  return sender;
}
