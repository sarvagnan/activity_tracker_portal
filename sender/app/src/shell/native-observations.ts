import { requireNativeModule } from "expo-modules-core";

export function nativeProjectionSchemaVersions(): Promise<Readonly<Record<string, number>>> {
  return requireNativeModule<{
    projectionSchemaVersions(): Promise<Record<string, number>>;
  }>("ActivityTrackerHealthConnect").projectionSchemaVersions();
}

export function nativeAvailableStorageBytes(): Promise<number> {
  return requireNativeModule<{
    availableStorageBytes(): Promise<number>;
  }>("ActivityTrackerBackgroundScheduling").availableStorageBytes();
}
