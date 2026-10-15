// Production-only Expo boundary; the coordinator validates reader results.
import { requireNativeModule } from "expo-modules-core";
import type { ChangeRead, SnapshotRead, SyncSources } from "./sync/coordinate-sender-sync.ts";
import type { IssuedToken } from "./snapshot/begin-initial-snapshot.ts";

interface HealthConnectModule {
  readSnapshotPage(
    recordType: string,
    snapshotId: string,
    position: Uint8Array | null,
    implementationVersion: string,
  ): Promise<SnapshotRead>;
  readChangePage(
    recordType: string,
    cycleId: string,
    changesToken: string,
    unreadSinceMs: number,
    implementationVersion: string,
  ): Promise<ChangeRead>;
  issueChangesToken(recordType: string): Promise<IssuedToken>;
}

export function nativeSyncSources(implementationVersion: string): SyncSources {
  const native = requireNativeModule<HealthConnectModule>("ActivityTrackerHealthConnect");
  return {
    readSnapshotPage: (request) => native.readSnapshotPage(
      request.recordType, request.snapshotId, request.position, implementationVersion,
    ),
    readChangePage: (request) => native.readChangePage(
      request.recordType, request.cycleId, request.changesToken, request.unreadSinceMs, implementationVersion,
    ),
    issue: (recordType) => native.issueChangesToken(recordType),
  };
}
