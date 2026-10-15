import { PermissionsAndroid } from "react-native";
import { requestNotificationPermission } from "./request-notification-permission.ts";

/** pv2.6 owns when to invoke this foreground permission request. */
export function requestSenderNotificationPermission(): Promise<boolean> {
  return requestNotificationPermission(PermissionsAndroid);
}
