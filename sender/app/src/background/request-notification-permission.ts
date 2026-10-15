export const POST_NOTIFICATIONS = "android.permission.POST_NOTIFICATIONS";

export interface NotificationPermissions {
  request(permission: typeof POST_NOTIFICATIONS): Promise<string>;
  RESULTS: { GRANTED: string };
}

/** Called by the foreground shell, never by the background task. */
export async function requestNotificationPermission(permissions: NotificationPermissions): Promise<boolean> {
  return await permissions.request(POST_NOTIFICATIONS) === permissions.RESULTS.GRANTED;
}
