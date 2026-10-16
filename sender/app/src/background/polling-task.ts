import { expoSender } from "../shell/expo-sender.ts";
import * as TaskManager from "expo-task-manager";
import { BackgroundTaskResult } from "expo-background-task";
import { POLLING_TASK_NAME } from "../scheduling/schedule-background-polling.ts";
import { backgroundTaskBody, type BackgroundPollingProvider } from "./background-task-body.ts";

let provider: BackgroundPollingProvider | null = async () =>
  (await expoSender()).backgroundPollingProvider();

async function currentProvider() {
  return provider ? provider() : null;
}

// TaskManager loads the entry point for headless execution, without mounting App.
TaskManager.defineTask(POLLING_TASK_NAME, backgroundTaskBody(() => currentProvider(), BackgroundTaskResult));

/** Override composition for a host; null deliberately reports a failed task. */
export function setBackgroundPollingProvider(next: BackgroundPollingProvider | null): void {
  provider = next;
}
