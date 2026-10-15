import assert from "node:assert/strict";
import test from "node:test";
import { schedulingPlatform, type PlatformDependencies } from "../src/background/scheduling-platform.ts";
import {
  schedulingNotifier, type NotifierDependencies,
} from "../src/background/scheduling-notifier.ts";
import { requestNotificationPermission, POST_NOTIFICATIONS, type NotificationPermissions } from "../src/background/request-notification-permission.ts";
import { backgroundTaskBody, type BackgroundPollingProvider } from "../src/background/background-task-body.ts";
import type { BackgroundPolling } from "../src/scheduling/schedule-background-polling.ts";

function platformFake() {
  const calls: unknown[] = [];
  const state = { registered: true, status: 12, restricted: false };
  const dependencies: PlatformDependencies = {
    taskManager: {
      async isTaskRegisteredAsync(name) { calls.push(["registered", name]); return state.registered; },
    },
    backgroundTask: {
      // Deliberately distinct from Expo's values: the adapter must use the injected enum.
      BackgroundTaskStatus: { Restricted: 11 },
      async registerTaskAsync(name, options) { calls.push(["register", name, options]); },
      async getStatusAsync() { calls.push(["status"]); return state.status; },
    },
    nativeRestriction: {
      async isBackgroundRestricted() { calls.push(["native"]); return state.restricted; },
    },
  };
  return { calls, state, dependencies, platform: schedulingPlatform(dependencies) };
}

test("platform forwards registration names, booleans, and minimum interval minutes", async () => {
  const { calls, state, platform } = platformFake();
  assert.equal(await platform.isRegistered("polling"), true);
  state.registered = false;
  assert.equal(await platform.isRegistered("other"), false);
  assert.equal(await platform.register("polling", 60), undefined);
  assert.deepEqual(calls, [
    ["registered", "polling"], ["registered", "other"],
    ["register", "polling", { minimumInterval: 60 }],
  ]);
});

test("platform is available only when Expo and Android are unrestricted", async () => {
  const { calls, platform } = platformFake();
  assert.equal(await platform.restriction(), "available");
  assert.deepEqual(calls, [["status"], ["native"]]);
});

test("Expo restriction wins without consulting Android", async () => {
  const { calls, state, platform } = platformFake();
  state.status = 11;
  assert.equal(await platform.restriction(), "restricted");
  assert.deepEqual(calls, [["status"]]);
});

test("Android restriction overrides Expo availability", async () => {
  const { state, platform } = platformFake();
  state.restricted = true;
  assert.equal(await platform.restriction(), "restricted");
});

for (const synchronous of [false, true]) {
  for (const method of ["registration read", "registration write", "Expo status", "native restriction"] as const) {
    test(`platform propagates ${synchronous ? "thrown" : "rejected"} ${method} errors`, async () => {
      const { dependencies, platform } = platformFake();
      const failure = new Error(method);
      const fail = () => {
        if (synchronous) throw failure;
        return Promise.reject(failure);
      };
      let call: () => Promise<unknown>;
      switch (method) {
        case "registration read":
          dependencies.taskManager.isTaskRegisteredAsync = fail;
          call = () => platform.isRegistered("polling");
          break;
        case "registration write":
          dependencies.backgroundTask.registerTaskAsync = fail;
          call = () => platform.register("polling", 60);
          break;
        case "Expo status":
          dependencies.backgroundTask.getStatusAsync = fail;
          call = () => platform.restriction();
          break;
        case "native restriction":
          dependencies.nativeRestriction.isBackgroundRestricted = fail;
          call = () => platform.restriction();
          break;
      }
      // Wrap in async to check synchronous throws and rejections identically.
      await assert.rejects(async () => call(), (error) => error === failure);
    });
  }
}

function notificationsFake() {
  const calls: unknown[] = [];
  const state = { enabled: true };
  const native: NotifierDependencies = {
    async notificationsEnabled() { calls.push(["enabled"]); return state.enabled; },
    async postOperatorNotification(condition) { calls.push(["post", condition]); },
  };
  return { calls, state, native, notifier: schedulingNotifier(native) };
}

test("notifier checks availability and forwards only the condition", async () => {
  const { calls, notifier } = notificationsFake();
  await notifier.notify("restricted");
  await notifier.notify("unregistered");
  assert.deepEqual(calls, [
    ["enabled"], ["post", "restricted"], ["enabled"], ["post", "unregistered"],
  ]);
});

test("disabled notifications throw without posting and a later enabled attempt works", async () => {
  const { calls, state, notifier } = notificationsFake();
  state.enabled = false;
  await assert.rejects(notifier.notify("unregistered"), /notifications are not enabled/);
  assert.deepEqual(calls, [["enabled"]]);
  state.enabled = true;
  await notifier.notify("unregistered");
  assert.deepEqual(calls, [["enabled"], ["enabled"], ["post", "unregistered"]]);
});

for (const synchronous of [false, true]) {
  for (const method of ["notificationsEnabled", "postOperatorNotification"] as const) {
    test(`notifier propagates ${synchronous ? "thrown" : "rejected"} ${method} failure for retry`, async () => {
      const { calls, native, notifier } = notificationsFake();
      const failure = new Error("synthetic platform failure");
      const original = native[method];
      native[method] = (): Promise<never> => {
        if (synchronous) throw failure;
        return Promise.reject(failure);
      };
      await assert.rejects(notifier.notify("restricted"), (error) => error === failure);
      assert.deepEqual(calls, method === "notificationsEnabled" ? [] : [["enabled"]]);
      Object.assign(native, { [method]: original });
      await notifier.notify("restricted");
      assert.deepEqual(calls.at(-1), ["post", "restricted"]);
    });
  }
}

for (const result of ["allowed", "denied", "never_ask_again"]) {
  test(`permission helper requests POST_NOTIFICATIONS and maps ${result}`, async () => {
    const calls: string[] = [];
    const permissions: NotificationPermissions = {
      // Deliberately different from RN's value to prove the injected enum is used.
      RESULTS: { GRANTED: "allowed" },
      async request(permission) { calls.push(permission); return result; },
    };
    assert.equal(await requestNotificationPermission(permissions), result === "allowed");
    assert.deepEqual(calls, [POST_NOTIFICATIONS]);
  });
}

for (const synchronous of [false, true]) {
  test(`permission helper propagates ${synchronous ? "thrown" : "rejected"} request errors`, async () => {
    const failure = new Error("permission request unavailable");
    await assert.rejects(requestNotificationPermission({
      RESULTS: { GRANTED: "allowed" },
      request() {
        if (synchronous) throw failure;
        return Promise.reject(failure);
      },
    }), (error) => error === failure);
  });
}

function polling(runTask: BackgroundPolling["runTask"]): BackgroundPolling {
  return {
    runTask,
    async ensureRegistered() { return { outcome: "observed", status: "available", condition: null }; },
    async observe() { return "available"; },
    async condition() { return null; },
  };
}

for (const outcome of ["success", "failed"] as const) {
  test(`task awaits the provider and maps ${outcome} to the injected Expo result`, async () => {
    const calls: string[] = [];
    const body = backgroundTaskBody(async () => {
      calls.push("provider");
      await Promise.resolve();
      return polling(async () => { calls.push("run"); await Promise.resolve(); return outcome; });
    }, { Success: 101, Failed: 102 });
    assert.equal(await body(), outcome === "success" ? 101 : 102);
    assert.deepEqual(calls, ["provider", "run"]);
  });
}

test("a missing provider and a provider returning null both report Failed", async () => {
  assert.equal(await backgroundTaskBody(null)(), 2);
  assert.equal(await backgroundTaskBody(async () => null)(), 2);
});

for (const synchronous of [false, true]) {
  for (const source of ["provider", "runTask"] as const) {
    test(`task never throws when ${source} ${synchronous ? "throws" : "rejects"}`, async () => {
      const fail = () => {
        if (synchronous) throw new Error("synthetic failure");
        return Promise.reject(new Error("synthetic failure"));
      };
      const provider: BackgroundPollingProvider = source === "provider" ? fail : async () => polling(fail);
      assert.equal(await backgroundTaskBody(provider, { Success: 101, Failed: 102 })(), 102);
    });
  }
}
