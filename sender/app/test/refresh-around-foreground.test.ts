import assert from "node:assert/strict";
import { test } from "node:test";
import { refreshAroundForeground } from "../src/screens/refresh-around-foreground.ts";

const tick = () => new Promise<void>((resolve) => setImmediate(resolve));

test("status, conditions, and permissions publish while foreground never settles", async () => {
  const published: string[] = [];
  const shell = {
    async status() { return "confirming"; },
    async conditions() { return ["permission-missing"]; },
    foreground() { published.push("foreground"); return new Promise<never>(() => {}); },
  };
  void refreshAroundForeground(() => shell.foreground(), async () => {
    published.push("status-start");
    const [status, conditions] = await Promise.all([shell.status(), shell.conditions()]);
    published.push(status, ...conditions);
  }, async () => { published.push("permissions"); });
  await tick();
  assert.deepEqual(published, ["status-start", "permissions", "foreground", "confirming", "permission-missing"]);
});

test("slow pre-refresh and permission checks cannot prevent foreground or post-run refresh", async () => {
  let statuses = 0; let permissions = 0; let runs = 0;
  await refreshAroundForeground(async () => { runs++; },
    () => ++statuses === 1 ? new Promise<never>(() => {}) : Promise.resolve(),
    () => ++permissions === 1 ? new Promise<never>(() => {}) : Promise.resolve());
  assert.deepEqual([statuses, permissions, runs], [2, 2, 1]);
});

test("rejected and synchronously throwing pre-checks cannot prevent foreground or the other checks", async () => {
  for (const synchronous of [false, true]) {
    let statuses = 0; let permissions = 0; let runs = 0;
    await refreshAroundForeground(async () => { runs++; }, () => {
      if (++statuses !== 1) return Promise.resolve();
      if (synchronous) throw new Error("pre-refresh");
      return Promise.reject(new Error("pre-refresh"));
    }, () => {
      if (++permissions !== 1) return Promise.resolve();
      if (synchronous) throw new Error("pre-permissions");
      return Promise.reject(new Error("pre-permissions"));
    });
    assert.deepEqual([statuses, permissions, runs], [2, 2, 1]);
  }
});

test("failing foreground still refreshes status and permissions afterward and reports the failure", async () => {
  for (const synchronous of [false, true]) {
    const calls: string[] = [];
    const failure = new Error("foreground failed");
    await assert.rejects(refreshAroundForeground(() => {
      calls.push("foreground");
      if (synchronous) throw failure;
      return Promise.reject(failure);
    }, async () => { calls.push("status"); }, async () => { calls.push("permissions"); }), (error) => error === failure);
    assert.deepEqual(calls, ["status", "permissions", "foreground", "status", "permissions"]);
  }
});

test("post-run status failure still starts the permission check", async () => {
  let statuses = 0; let permissions = 0;
  await assert.rejects(refreshAroundForeground(async () => {}, () => {
    if (++statuses === 2) throw new Error("post-refresh");
    return Promise.resolve();
  }, async () => { permissions++; }), /post-refresh/);
  assert.equal(permissions, 2);
});
