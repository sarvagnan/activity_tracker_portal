import { requireNativeModule } from "expo-modules-core";
import permissions from "../modules/health-connect/health-permissions.json";

export const SENDER_HEALTH_PERMISSIONS: readonly string[] = Object.freeze([...permissions]);
export type HealthPermissionFeature = "READ_HEALTH_DATA_HISTORY" | "READ_HEALTH_DATA_IN_BACKGROUND";
export type HealthFeatureStatuses = Readonly<Record<HealthPermissionFeature, number>>;
export interface HealthPermissions {
  sdkStatus(): Promise<number>;
  featureStatuses(): Promise<HealthFeatureStatuses>;
  grantedPermissions(): Promise<readonly string[]>;
  requestPermissions(permissions: readonly string[]): Promise<readonly string[]>;
}
/** Only permission/feature names and status ints cross this boundary; SDK_AVAILABLE is 3. */
export function nativeHealthPermissions(): HealthPermissions {
  return requireNativeModule<HealthPermissions>("ActivityTrackerHealthConnect");
}
