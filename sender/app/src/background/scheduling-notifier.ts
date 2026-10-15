import type { SchedulingNotifier } from "../scheduling/schedule-background-polling.ts";

export interface NotifierDependencies {
  notificationsEnabled(): Promise<boolean>;
  postOperatorNotification(condition: "restricted" | "unregistered"): Promise<void>;
}

export function schedulingNotifier(native: NotifierDependencies): SchedulingNotifier {
  return {
    async notify(condition) {
      if (!(await native.notificationsEnabled())) {
        throw new Error("Sender notifications are not enabled");
      }
      await native.postOperatorNotification(condition);
    },
  };
}
