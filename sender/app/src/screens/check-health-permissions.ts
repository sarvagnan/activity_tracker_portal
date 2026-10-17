import type { HealthPermissions } from "../native-health-permissions.ts";
import { permissionState, requestableHealthPermissions } from "./sender-screen-model.ts";
import type { PermissionState } from "./sender-screen-model.ts";

/** Check feature availability before requesting optional access; no health data is read. */
export async function checkHealthPermissions(
  native: HealthPermissions, permissions: readonly string[], request = false,
): Promise<PermissionState> {
  try {
    const sdk = await native.sdkStatus();
    if (sdk !== 3) return permissionState(sdk, [], permissions);
    const features = await native.featureStatuses();
    const requestable = requestableHealthPermissions(permissions, features);
    const granted = request ? await native.requestPermissions(requestable) : await native.grantedPermissions();
    return permissionState(sdk, granted, permissions, features);
  } catch { return "failed"; }
}
