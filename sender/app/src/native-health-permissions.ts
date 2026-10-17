import { requireNativeModule } from "expo-modules-core";
import permissions from "../modules/health-connect/health-permissions.json";

export const SENDER_HEALTH_PERMISSIONS: readonly string[] = Object.freeze([...permissions]);
export interface HealthPermissions {
  sdkStatus(): Promise<number>;
  grantedPermissions(): Promise<readonly string[]>;
  requestPermissions(permissions: readonly string[]): Promise<readonly string[]>;
}
/** Permission names only cross this boundary; SDK_AVAILABLE is 3. */
export function nativeHealthPermissions(): HealthPermissions {
  return requireNativeModule<HealthPermissions>("ActivityTrackerHealthConnect");
}
