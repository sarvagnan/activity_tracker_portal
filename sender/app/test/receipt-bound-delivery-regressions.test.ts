// Unbound implementation-review regressions for receipt-bound-delivery@1.
import assert from "node:assert/strict";
import { test } from "node:test";
import { Worker } from "node:worker_threads";
import { createHttpTransport } from "../src/transport/http-transport.ts";
import type { HttpFetch, HttpFetchResponse, HttpRequest, HttpResult } from "../src/transport/http-transport.ts";

const INTERVAL = 2_147_483_647;
const FAILED = { outcome: "failed" } as const;
const request = (timeoutMs = 1_000): HttpRequest => ({
  method: "GET", url: "https://portal.synthetic.invalid/receipt", headers: {}, body: null,
  timeoutMs, maxResponseBytes: 4096,
});
const flush = async () => { for (let turn = 0; turn < 20; turn++) await Promise.resolve(); };

function gate<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((release) => { resolve = release; });
  return { promise, resolve };
}

for (const timeoutMs of [INTERVAL + 1, Number.MAX_SAFE_INTEGER]) {
  test(`large timeout ${timeoutMs} expires exactly at its deadline`, { timeout: 30_000 }, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    let signal: AbortSignal | undefined;
    const fetch: HttpFetch = async (_url, init) => {
      signal = init.signal;
      return new Promise(() => {});
    };
    let settled: HttpResult | "pending" = "pending";
    const sending = createHttpTransport({ fetch }).send(request(timeoutMs)).then((result) => { settled = result; });
    let remaining = timeoutMs;
    // Mock timers schedule a chained timeout relative to the current tick, so tick each interval.
    const intervals = Math.floor((timeoutMs - 1) / INTERVAL);
    for (let interval = 0; interval < intervals; interval++) {
      t.mock.timers.tick(INTERVAL);
      remaining -= INTERVAL;
      if (interval < 2) {
        await flush();
        assert.equal(settled, "pending");
        assert.equal(signal?.aborted, false);
      }
    }
    t.mock.timers.tick(remaining - 1);
    await flush();
    assert.equal(settled, "pending");
    assert.equal(signal?.aborted, false);
    t.mock.timers.tick(1);
    await sending;
    assert.deepEqual(settled, FAILED);
    assert.equal(signal?.aborted, true);
  });

  test(`large timeout ${timeoutMs} clears every timer after a completed response`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const scheduling = t.mock.method(globalThis, "setTimeout");
    const clearing = t.mock.method(globalThis, "clearTimeout");
    const response = gate<HttpFetchResponse>();
    let signal: AbortSignal | undefined;
    const fetch: HttpFetch = async (_url, init) => { signal = init.signal; return response.promise; };
    const sending = createHttpTransport({ fetch }).send(request(timeoutMs));
    t.mock.timers.tick(INTERVAL);
    await flush();
    response.resolve({ status: 200, redirected: false, body: new Response(new Uint8Array([1, 2, 3])).body });
    assert.deepEqual(await sending, { outcome: "response", body: new Uint8Array([1, 2, 3]) });
    assert.equal(signal?.aborted, false);
    assert.ok(clearing.mock.callCount() > 0);
    const scheduled = scheduling.mock.callCount();
    t.mock.timers.tick(Number.MAX_SAFE_INTEGER);
    await flush();
    assert.equal(signal?.aborted, false);
    assert.equal(scheduling.mock.callCount(), scheduled, "no surviving timer may schedule another interval");
  });
}

test("endless immediately available empty chunks cannot starve the deadline", { timeout: 3_000 }, async () => {
  // The watchdog lives outside the starving event loop, so the previous bug fails instead of hanging the suite.
  const worker = new Worker(`
    const { parentPort } = require("node:worker_threads");
    (async () => {
      const { createHttpTransport } = await import(${JSON.stringify(new URL("../src/transport/http-transport.ts", import.meta.url).href)});
      let cancels = 0;
      const fetch = async () => ({ status: 200, redirected: false,
        body: new ReadableStream({
          pull(controller) { controller.enqueue(new Uint8Array()); },
          cancel() { cancels++; return new Promise(() => {}); },
        }, { highWaterMark: 0 }),
      });
      const started = performance.now();
      const result = await createHttpTransport({ fetch }).send(${JSON.stringify(request(20))});
      parentPort.postMessage({ result, cancels, elapsed: performance.now() - started });
    })().catch((error) => { throw error; });
  `, { eval: true });
  let watchdog: ReturnType<typeof setTimeout> | undefined;
  try {
    const observed = await new Promise<{ result: HttpResult; cancels: number; elapsed: number }>((resolve, reject) => {
      watchdog = setTimeout(() => reject(new Error("empty chunks starved the 20ms deadline for 1,500ms")), 1_500);
      worker.once("message", resolve);
      worker.once("error", reject);
      worker.once("exit", (code) => { if (code !== 0) reject(new Error(`worker exited ${code}`)); });
    });
    assert.deepEqual(observed.result, FAILED);
    assert.equal(observed.cancels, 1);
    assert.ok(observed.elapsed >= 20 && observed.elapsed < 750, String(observed.elapsed));
  } finally {
    clearTimeout(watchdog);
    await worker.terminate();
  }
});

test("finite empty runs across task yields preserve the exact interleaved body", { timeout: 3_000 }, async () => {
  const runs = 3;
  const emptiesPerRun = 2_049;
  let reads = 0;
  let cancels = 0;
  const fetch: HttpFetch = async () => ({ status: 200, redirected: false,
    body: new ReadableStream<Uint8Array>({
      pull(controller) {
        const position = reads++;
        if (position === runs * (emptiesPerRun + 1)) { controller.close(); return; }
        controller.enqueue(position % (emptiesPerRun + 1) === emptiesPerRun
          ? new Uint8Array([Math.floor(position / (emptiesPerRun + 1)), 255]) : new Uint8Array());
      },
      cancel() { cancels++; },
    }, { highWaterMark: 0 }),
  });
  assert.deepEqual(await createHttpTransport({ fetch }).send(request()),
    { outcome: "response", body: new Uint8Array([0, 255, 1, 255, 2, 255]) });
  assert.equal(reads, runs * (emptiesPerRun + 1) + 1);
  assert.equal(cancels, 0);
});

for (const kind of ["3xx", "redirected", "late"] as const) {
  test(`${kind} response cancels its streaming body without awaiting cancellation`, { timeout: 2_000 }, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const response = gate<HttpFetchResponse>();
    let cancels = 0;
    let pulls = 0;
    let signal: AbortSignal | undefined;
    const body = new ReadableStream<Uint8Array>({
      pull(controller) { pulls++; controller.enqueue(new Uint8Array([1])); },
      cancel() { cancels++; return new Promise<void>(() => {}); },
    }, { highWaterMark: 0 });
    const fetch: HttpFetch = async (_url, init) => {
      signal = init.signal;
      // Deliberately ignore abort: a late fetch result must still have its body disposed of.
      return response.promise;
    };
    let settled: HttpResult | "pending" = "pending";
    const sending = createHttpTransport({ fetch }).send(request(10)).then((result) => { settled = result; });
    if (kind === "late") {
      t.mock.timers.tick(10);
      await sending;
      assert.deepEqual(settled, FAILED);
      assert.equal(signal?.aborted, true);
    }
    response.resolve({ status: kind === "3xx" ? 308 : 200, redirected: kind === "redirected", body });
    await flush();
    assert.deepEqual(settled, FAILED, "send must settle even though cancel never does");
    assert.equal(cancels, 1);
    assert.equal(pulls, 0);
    await sending;
  });
}
