// Production-only boundary. The cores and their tests never load Expo.
import * as BackgroundTask from "expo-background-task";
import * as TaskManager from "expo-task-manager";
import { requireNativeModule } from "expo-modules-core";
import type { DeliveryNotifier } from "../delivery/send-outbox-message.ts";
import { schedulingPlatform } from "./scheduling-platform.ts";
import { schedulingNotifier, type NotifierDependencies } from "./scheduling-notifier.ts";

interface BackgroundSchedulingNative extends NotifierDependencies {
  postOperatorNotification(condition: string): Promise<void>;
  isBackgroundRestricted(): Promise<boolean>;
}

function nativeScheduling(): BackgroundSchedulingNative {
  return requireNativeModule<BackgroundSchedulingNative>("ActivityTrackerBackgroundScheduling");
}

export function expoSchedulingPlatform() {
  return schedulingPlatform({
    taskManager: TaskManager,
    backgroundTask: BackgroundTask,
    nativeRestriction: {
      // Resolve inside each call so missing native code becomes an observation
      // failure, rather than preventing composition or loading the task.
      async isBackgroundRestricted() {
        return nativeScheduling().isBackgroundRestricted();
      },
    },
  });
}

export function expoSchedulingNotifier() {
  return schedulingNotifier({
    async notificationsEnabled() { return nativeScheduling().notificationsEnabled(); },
    async postOperatorNotification(condition) { await nativeScheduling().postOperatorNotification(condition); },
  });
}

/** Delivery owns onset/retry bookkeeping; the native adapter accepts fixed condition text only. */
export function expoDeliveryNotifier(): DeliveryNotifier {
  return {
    async notify(failure) {
      if (!(await nativeScheduling().notificationsEnabled())) {
        throw new Error("Sender notifications are not enabled");
      }
      await nativeScheduling().postOperatorNotification(failure);
    },
  };
}
