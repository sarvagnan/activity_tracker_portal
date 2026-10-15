import * as TaskManager from "expo-task-manager";
import { BackgroundTaskResult } from "expo-background-task";
import { POLLING_TASK_NAME } from "../scheduling/schedule-background-polling.ts";
import { backgroundTaskBody, type BackgroundPollingProvider } from "./background-task-body.ts";

let provider: BackgroundPollingProvider | null = null;

async function currentProvider() {
  return provider ? provider() : null;
}

// TaskManager loads the entry point for headless execution, without mounting App.
TaskManager.defineTask(POLLING_TASK_NAME, backgroundTaskBody(() => currentProvider(), BackgroundTaskResult));

/** pv2.6 will compose the real provider; no provider means a failed task. */
export function setBackgroundPollingProvider(next: BackgroundPollingProvider | null): void {
  provider = next;
}
