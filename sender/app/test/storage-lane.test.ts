import assert from "node:assert/strict";
import { setImmediate } from "node:timers/promises";
import { test } from "node:test";
import type { SenderDatabase } from "../src/storage/database.ts";
import { storageLane } from "../src/storage/storage-lane.ts";
import { storageLane as syncStorageLane } from "../src/sync/sync-storage.ts";

function syntheticDatabase(): SenderDatabase {
  return {
    async execute() {},
    async query() { return []; },
    async transaction(operation) { return operation(this); },
    async close() {},
  };
}

test("one database runs operations in FIFO order and continues after rejection", { timeout: 5_000 }, async () => {
  const lane = storageLane(syntheticDatabase());
  const firstStarted = Promise.withResolvers<void>();
  const secondStarted = Promise.withResolvers<void>();
  const releaseFirst = Promise.withResolvers<void>();
  const releaseSecond = Promise.withResolvers<void>();
  const events: string[] = [];
  const failure = new Error("synthetic operation failed");
  const first = lane(async () => {
    events.push("first:start");
    firstStarted.resolve();
    await releaseFirst.promise;
    events.push("first:reject");
    throw failure;
  });
  const rejected = assert.rejects(first, (error) => error === failure);
  const second = lane(async () => {
    events.push("second:start");
    secondStarted.resolve();
    await releaseSecond.promise;
    events.push("second:end");
    return "second result";
  });
  const third = lane(async () => {
    events.push("third:start");
    return 3;
  });
  try {
    await firstStarted.promise;
    await setImmediate();
    assert.deepEqual(events, ["first:start"]);
    releaseFirst.resolve();
    await rejected;
    await secondStarted.promise;
    await setImmediate();
    assert.deepEqual(events, ["first:start", "first:reject", "second:start"]);
    releaseSecond.resolve();
    assert.deepEqual(await Promise.all([second, third]), ["second result", 3]);
    assert.deepEqual(events, ["first:start", "first:reject", "second:start", "second:end", "third:start"]);
  } finally {
    releaseFirst.resolve();
    releaseSecond.resolve();
  }
});

test("different databases have independent lanes", { timeout: 5_000 }, async () => {
  const firstLane = storageLane(syntheticDatabase());
  const secondLane = storageLane(syntheticDatabase());
  assert.notStrictEqual(firstLane, secondLane);
  const started = Promise.withResolvers<void>();
  const release = Promise.withResolvers<void>();
  let firstActive = false;
  const first = firstLane(async () => {
    firstActive = true;
    started.resolve();
    await release.promise;
    firstActive = false;
  });
  try {
    await started.promise;
    assert.equal(await secondLane(async () => {
      assert.equal(firstActive, true);
      return "independent result";
    }), "independent result");
    assert.equal(firstActive, true);
  } finally {
    release.resolve();
    await first;
  }
});

test("one database always returns the same lane", () => {
  const database = syntheticDatabase();
  assert.strictEqual(storageLane(database), storageLane(database));
});

test("sync-storage re-exports the identical storageLane function", () => {
  assert.strictEqual(syncStorageLane, storageLane);
});
