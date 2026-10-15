import type { SchedulingPlatform } from "../scheduling/schedule-background-polling.ts";

export interface PlatformDependencies {
  taskManager: {
    isTaskRegisteredAsync(name: string): Promise<boolean>;
  };
  backgroundTask: {
    BackgroundTaskStatus: { Restricted: number };
    registerTaskAsync(name: string, options: { minimumInterval: number }): Promise<void>;
    getStatusAsync(): Promise<number>;
  };
  nativeRestriction: {
    isBackgroundRestricted(): Promise<boolean>;
  };
}

/** Platform failures propagate to the polling module's conservative observation. */
export function schedulingPlatform({
  taskManager, backgroundTask, nativeRestriction,
}: PlatformDependencies): SchedulingPlatform {
  return {
    isRegistered: (name) => taskManager.isTaskRegisteredAsync(name),
    register: (name, minutes) => backgroundTask.registerTaskAsync(name, { minimumInterval: minutes }),
    async restriction() {
      const status = await backgroundTask.getStatusAsync();
      if (status === backgroundTask.BackgroundTaskStatus.Restricted) return "restricted";
      return await nativeRestriction.isBackgroundRestricted() ? "restricted" : "available";
    },
  };
}
