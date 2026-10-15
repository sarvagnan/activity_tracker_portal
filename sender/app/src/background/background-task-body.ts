import type { BackgroundPolling } from "../scheduling/schedule-background-polling.ts";

export type BackgroundPollingProvider = () => Promise<BackgroundPolling | null>;

// Wire values in the pinned expo-background-task 57.0.21. Production also passes
// the real enum, keeping this core executable in Node without loading Expo.
const taskResults = { Success: 1, Failed: 2 } as const;

export function backgroundTaskBody(
  provider: BackgroundPollingProvider | null,
  results: { Success: number; Failed: number } = taskResults,
): () => Promise<number> {
  return async () => {
    try {
      const polling = await provider?.();
      if (!polling) return results.Failed;
      return await polling.runTask() === "success" ? results.Success : results.Failed;
    } catch {
      return results.Failed;
    }
  };
}
