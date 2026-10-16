// Thin production boundary. Importing this file does not open storage or load a native module.
import { fetch } from "expo/fetch";
import { uuid } from "expo-modules-core";
import { createSenderEnrollment } from "../enrollment/manage-sender-enrollment.ts";
import { openSenderDatabase } from "../storage/expo-sqlite.ts";
import { nativeInstallationCipher } from "../native-installation-cipher.ts";
import { nativeSyncSources } from "../native-sync-sources.ts";
import { expoConditionNotifier, expoDeliveryNotifier, expoSchedulingNotifier, expoSchedulingPlatform } from "../background/expo-adapters.ts";
import { composeSender, PROJECTION_IMPLEMENTATION_VERSION } from "./compose-sender.ts";
import type { SenderShell } from "./compose-sender.ts";
import { nativeAvailableStorageBytes, nativeProjectionSchemaVersions } from "./native-observations.ts";

let sender: Promise<SenderShell> | null = null;

/** Shared by App and TaskManager in the same Android process, including headless
 * entry-point loads. This is the only production owner of sender.db: no second
 * database handle, observer, or remote-process service is created by either entry. */
export function expoSender(): Promise<SenderShell> {
  sender ??= Promise.resolve().then(() => {
    const cipher = nativeInstallationCipher();
    return composeSender({
      openDatabase: () => openSenderDatabase("sender.db"),
      enrollment: (database, transport) => createSenderEnrollment({
        database, cipher, transport, now: Date.now, randomUUID: () => uuid.v4(),
      }),
      fetch, deliveryNotifier: expoDeliveryNotifier(), conditionNotifier: expoConditionNotifier(),
      cipher, sources: nativeSyncSources(PROJECTION_IMPLEMENTATION_VERSION),
      projectionSchemaVersions: nativeProjectionSchemaVersions,
      availableStorageBytes: nativeAvailableStorageBytes,
      platform: expoSchedulingPlatform(), notifier: expoSchedulingNotifier(),
      now: Date.now, monotonicNow: () => performance.now(), randomUUID: () => uuid.v4(),
    });
  }).catch((error: unknown) => { sender = null; throw error; });
  return sender;
}
