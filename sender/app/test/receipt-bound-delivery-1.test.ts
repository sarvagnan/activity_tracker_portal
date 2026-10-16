// Acceptance tests for receipt-bound-delivery@1: the Sender HTTP transport's network seam (R2–R6, R14).
// Requirement numbers refer to packs/receipt-bound-delivery-1.spec.md. The transport runs over Node's
// global fetch against real loopback servers made with node:http and node:net, which stall, redirect,
// truncate, reset, and stream as each test needs. The end-to-end evidence (R7–R13) is in
// tests/acceptance/test_receipt_bound_delivery_1.py.
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { createServer as createHttpServer } from "node:http";
import type { IncomingMessage, Server, ServerResponse } from "node:http";
import { createServer as createTcpServer } from "node:net";
import type { AddressInfo, Socket } from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import type { TestContext } from "node:test";
import ts from "typescript";
import { createHttpTransport } from "../src/transport/http-transport.ts";
import type { HttpFetch, HttpFetchInit, HttpRequest, HttpResult, HttpTransport } from "../src/transport/http-transport.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const MODULE = join(HERE, "../src/transport/http-transport.ts");
const MODULE_DIRECTORY = dirname(MODULE);
const settle = (ms = 20) => new Promise<void>((resolve) => { setTimeout(resolve, ms); });
const BYTES = Uint8Array.from([0x7b, 0x22, 0x00, 0xff, 0xfe, 0x80, 0x7d, 0x0a]);

// Servers

interface Seen {
  readonly method: string;
  readonly url: string;
  readonly headers: Record<string, string | string[] | undefined>;
  readonly body: Uint8Array;
}

type Handler = (request: IncomingMessage, response: ServerResponse, body: Uint8Array) => void;

/** A loopback HTTP server that records every request and answers with `handler`. */
async function httpServer(t: TestContext, handler: Handler) {
  const seen: Seen[] = [];
  const sockets = new Set<Socket>();
  const server: Server = createHttpServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => { chunks.push(chunk); });
    request.on("end", () => {
      const body = new Uint8Array(Buffer.concat(chunks));
      seen.push({ method: request.method ?? "", url: request.url ?? "", headers: { ...request.headers }, body });
      handler(request, response, body);
    });
  });
  server.on("connection", (socket) => { sockets.add(socket); socket.on("close", () => { sockets.delete(socket); }); });
  await new Promise<void>((resolve) => { server.listen(0, "127.0.0.1", resolve); });
  t.after(async () => {
    for (const socket of sockets) socket.destroy();
    await new Promise<void>((resolve) => { server.close(() => { resolve(); }); });
  });
  const { port } = server.address() as AddressInfo;
  return { seen, origin: `http://127.0.0.1:${port}` };
}

/** A loopback TCP server that writes raw bytes, for exchanges no HTTP server would produce. */
async function rawServer(t: TestContext, onSocket: (socket: Socket) => void) {
  let connections = 0;
  const sockets = new Set<Socket>();
  const server = createTcpServer((socket) => {
    connections++;
    sockets.add(socket);
    socket.on("close", () => { sockets.delete(socket); });
    socket.on("error", () => {});
    // Wait for the request head before answering.
    socket.once("data", () => { onSocket(socket); });
  });
  await new Promise<void>((resolve) => { server.listen(0, "127.0.0.1", resolve); });
  t.after(async () => {
    for (const socket of sockets) socket.destroy();
    await new Promise<void>((resolve) => { server.close(() => { resolve(); }); });
  });
  const { port } = server.address() as AddressInfo;
  return { origin: `http://127.0.0.1:${port}`, connections: () => connections };
}

const post = (url: string, overrides: Partial<HttpRequest> = {}): HttpRequest => ({
  method: "POST", url, headers: { "content-type": "application/json" }, body: BYTES, timeoutMs: 5_000, maxResponseBytes: 4096, ...overrides,
});
const get = (url: string, overrides: Partial<HttpRequest> = {}): HttpRequest => ({
  method: "GET", url, headers: {}, body: null, timeoutMs: 5_000, maxResponseBytes: 4096, ...overrides,
});

/** Node's fetch, recording every call's URL and init. Node's fetch and expo/fetch both satisfy HttpFetch. */
function spyFetch(inner: HttpFetch = fetch) {
  const calls: { url: string; init: HttpFetchInit }[] = [];
  const spy: HttpFetch = (url, init) => {
    calls.push({ url, init: { ...init, headers: { ...init.headers }, ...(init.body ? { body: new Uint8Array(init.body) } : {}) } });
    return inner(url, init);
  };
  return { spy, calls };
}

function transport(fetcher: HttpFetch = fetch): HttpTransport {
  return createHttpTransport({ fetch: fetcher });
}

/** Elapsed monotonic milliseconds since `start`. */
const since = (start: number) => performance.now() - start;

const response = (body: Uint8Array): HttpResult => ({ outcome: "response", body });
const FAILED: HttpResult = { outcome: "failed" };
const utf8 = (text: string) => new TextEncoder().encode(text);

// R2 — Caller errors

test("malformed_options_and_requests_are_caller_errors", async (t) => {
  for (const options of [undefined, null, 7, {}, { fetch: "fetch" }]) {
    assert.throws(() => createHttpTransport(options as never), TypeError, JSON.stringify(options));
  }
  const { origin } = await httpServer(t, (_request, res) => { res.end("ok"); });
  const { spy, calls } = spyFetch();
  const http = transport(spy);
  const requests: unknown[] = [
    undefined, null, 7,
    post(origin, { method: "PUT" as never }), post(origin, { method: "post" as never }),
    get(origin, { method: "DELETE" as never }), get(origin, { method: "get" as never }), get(origin, { method: undefined as never }),
    post("/v1/outbox-messages"), post("ftp://127.0.0.1/"), post(""), post(origin, { url: 7 as never }),
    post(origin, { headers: null as never }), post(origin, { headers: ["content-type"] as never }), post(origin, { headers: { "content-type": 1 } as never }),
    post(origin, { body: null }), post(origin, { body: [1, 2] as never }), post(origin, { body: "bytes" as never }),
    get(origin, { body: new Uint8Array() }), get(origin, { body: undefined as never }),
    post(origin, { timeoutMs: 0 }), post(origin, { timeoutMs: 1.5 }), post(origin, { timeoutMs: 2 ** 53 }), post(origin, { timeoutMs: "5" as never }),
    post(origin, { maxResponseBytes: 0 }), post(origin, { maxResponseBytes: -1 }), post(origin, { maxResponseBytes: Number.NaN }),
  ];
  for (const request of requests) await assert.rejects(http.send(request as HttpRequest), TypeError, JSON.stringify(request));
  assert.deepEqual(calls, []);
});

// R3 — One exact request to the given URL

test("the_request_is_sent_exactly_once", async (t) => {
  const { seen, origin } = await httpServer(t, (_request, res) => { res.end(Buffer.from(BYTES)); });
  const { spy, calls } = spyFetch();
  const http = transport(spy);
  assert.deepEqual(await http.send(post(`${origin}/v1/outbox-messages`)), response(BYTES));
  assert.deepEqual(await http.send(get(`${origin}/v1/capabilities`)), response(BYTES));
  assert.equal(seen.length, 2);
  assert.deepEqual([seen[0].method, seen[0].url, seen[0].body], ["POST", "/v1/outbox-messages", BYTES]);
  assert.equal(seen[0].headers["content-type"], "application/json");
  assert.deepEqual([seen[1].method, seen[1].url, seen[1].body], ["GET", "/v1/capabilities", new Uint8Array()]);
  assert.equal(seen[1].headers["content-type"], undefined);
  assert.equal(calls.length, 2);
  assert.deepEqual({ ...calls[0].init, signal: null }, {
    method: "POST", headers: { "content-type": "application/json" }, body: BYTES, redirect: "error", credentials: "omit", signal: null,
  });
  assert.ok(calls[0].init.signal instanceof AbortSignal);
  assert.deepEqual({ ...calls[1].init, signal: null }, { method: "GET", headers: {}, redirect: "error", credentials: "omit", signal: null });
  assert.equal("body" in calls[1].init, false);
  assert.equal(calls[0].url, `${origin}/v1/outbox-messages`);
  // Mutating the caller's request after send returns changes nothing that was sent.
  const body = new Uint8Array(BYTES);
  const headers: Record<string, string> = { "content-type": "application/json" };
  const sending = http.send(post(`${origin}/v1/enrollment-confirmation`, { body, headers }));
  body.fill(0);
  headers["content-type"] = "text/plain";
  assert.deepEqual(await sending, response(BYTES));
  assert.deepEqual(seen[2].body, BYTES);
  assert.equal(seen[2].headers["content-type"], "application/json");
  // The same holds for a fetch that reads its arguments only after a tick.
  const lazy = transport(async (url, init) => { await settle(10); return fetch(url, init); });
  const lateBody = new Uint8Array(BYTES);
  const lateHeaders: Record<string, string> = { "content-type": "application/json" };
  const late = lazy.send(post(`${origin}/v1/enrollment-confirmation`, { body: lateBody, headers: lateHeaders }));
  lateBody.fill(0);
  lateHeaders["content-type"] = "text/plain";
  assert.deepEqual(await late, response(BYTES));
  assert.deepEqual(seen[3].body, BYTES);
  assert.equal(seen[3].headers["content-type"], "application/json");
});

test("no_retry_and_no_encoding_is_added", async (t) => {
  const { seen, origin } = await httpServer(t, (_request, res) => { res.writeHead(503); res.end(utf8("busy")); });
  assert.deepEqual(await transport().send(post(origin)), response(utf8("busy")));
  assert.equal(seen.length, 1);
  assert.equal(seen[0].headers["content-encoding"], undefined);
  assert.deepEqual(seen[0].body, BYTES);
  // A reset connection is not retried either.
  const raw = await rawServer(t, (socket) => { socket.destroy(); });
  assert.deepEqual(await transport().send(post(raw.origin)), FAILED);
  await settle(100);
  assert.equal(raw.connections(), 1);
});

// R4 — Redirects are never followed

test("redirects_are_refused_and_never_followed", async (t) => {
  const target = await httpServer(t, (_request, res) => { res.end(utf8("leaked")); });
  for (const status of [301, 302, 303, 307, 308]) {
    const { seen, origin } = await httpServer(t, (_request, res) => { res.writeHead(status, { location: `${target.origin}/elsewhere` }); res.end(); });
    assert.deepEqual(await transport().send(post(origin)), FAILED, String(status));
    assert.equal(seen.length, 1, String(status));
  }
  assert.deepEqual(target.seen, []);
  // A redirect status without a location, and a response a fetch reports as redirected, are refused too.
  const bare = await httpServer(t, (_request, res) => { res.writeHead(308); res.end(utf8("moved")); });
  assert.deepEqual(await transport().send(post(bare.origin)), FAILED);
  const redirected: HttpFetch = async () => ({ status: 200, redirected: true, body: new Response(utf8("receipt")).body });
  assert.deepEqual(await transport(redirected).send(post(bare.origin)), FAILED);
  // A fetch that hands back a redirect status itself is refused, whatever the status in 3xx.
  for (const status of [300, 307, 399]) {
    const unfollowed: HttpFetch = async () => ({ status, redirected: false, body: new Response(utf8("receipt")).body });
    assert.deepEqual(await transport(unfollowed).send(post(bare.origin)), FAILED, String(status));
  }
  const notRedirect: HttpFetch = async () => ({ status: 400, redirected: false, body: new Response(utf8("answer")).body });
  assert.deepEqual(await transport(notRedirect).send(post(bare.origin)), response(utf8("answer")));
});

// R5 — One timeout over the whole exchange

test("a_stalled_exchange_times_out", async (t) => {
  // The deadline, exactly: a fetch that settles only when aborted, under mocked timers.
  const flush = async () => { for (let turn = 0; turn < 20; turn++) await Promise.resolve(); };
  t.mock.timers.enable({ apis: ["setTimeout"] });
  try {
    // Headers that never arrive, headers at once and a body that stalls, and headers that arrive 4,000 ms into
    // the exchange and then a body that stalls: the deadline is 5,000 ms from the call in every case.
    for (const stage of ["headers", "body", "late-headers"] as const) {
      let signal: AbortSignal | undefined;
      const stalled: HttpFetch = async (_url, init) => {
        signal = init.signal;
        const aborted = new Promise<never>((_resolve, reject) => {
          init.signal.addEventListener("abort", () => { reject(new Error("synthetic abort")); });
        });
        if (stage === "headers") return aborted;
        if (stage === "late-headers") await new Promise<void>((resolve) => { setTimeout(resolve, 4_000); });
        return {
          status: 200, redirected: false,
          body: new ReadableStream<Uint8Array>({ pull: (controller) => aborted.catch((error) => { controller.error(error); }) }),
        };
      };
      let settled: HttpResult | "pending" = "pending";
      const sending = transport(stalled).send(post("http://127.0.0.1:9/", { timeoutMs: 5_000 })).then((result) => { settled = result; });
      await flush();
      if (stage === "late-headers") {
        t.mock.timers.tick(4_000);
        await flush();
        t.mock.timers.tick(999);
      } else {
        t.mock.timers.tick(4_999);
      }
      await flush();
      assert.equal(signal?.aborted, false, stage);
      assert.equal(settled, "pending", stage);
      t.mock.timers.tick(1);
      await flush();
      await sending;
      assert.equal(signal?.aborted, true, stage);
      assert.deepEqual(settled, FAILED, stage);
    }
  } finally { t.mock.timers.reset(); }
  // Over real sockets, a stalled exchange ends near its deadline: no earlier, and within a generous margin.
  const within = (elapsed: number, timeoutMs: number) => elapsed >= timeoutMs - 50 && elapsed < timeoutMs + 2_000;
  // Headers never arrive.
  const silent = await rawServer(t, () => { /* never answers */ });
  let started = performance.now();
  assert.deepEqual(await transport().send(post(silent.origin, { timeoutMs: 300 })), FAILED);
  let elapsed = since(started);
  assert.ok(within(elapsed, 300), String(elapsed));
  // Headers arrive, and then the body stalls.
  const stalling = await rawServer(t, (socket) => {
    socket.write("HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: 100\r\n\r\n{\"partial\":");
  });
  started = performance.now();
  assert.deepEqual(await transport().send(post(stalling.origin, { timeoutMs: 300 })), FAILED);
  elapsed = since(started);
  assert.ok(within(elapsed, 300), String(elapsed));
  // A body that trickles in, never finishing before the deadline, also fails at the deadline.
  const trickling = await rawServer(t, (socket) => {
    socket.write("HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n");
    const timer = setInterval(() => { if (!socket.destroyed) socket.write("1\r\nx\r\n"); }, 50);
    socket.on("close", () => { clearInterval(timer); });
  });
  started = performance.now();
  assert.deepEqual(await transport().send(post(trickling.origin, { timeoutMs: 400, maxResponseBytes: 1_000_000 })), FAILED);
  elapsed = since(started);
  assert.ok(within(elapsed, 400), String(elapsed));
  // A response completed before the deadline is unaffected.
  const prompt = await httpServer(t, (_request, res) => { setTimeout(() => { res.end(utf8("on time")); }, 50); });
  assert.deepEqual(await transport().send(post(prompt.origin, { timeoutMs: 2_000 })), response(utf8("on time")));
});

// R6 — A bounded, complete body read

test("the_body_read_is_bounded", async (t) => {
  const large = new Uint8Array(10_000).map((_, index) => index % 251);
  const { origin } = await httpServer(t, (_request, res) => { res.end(Buffer.from(large)); });
  assert.deepEqual(await transport().send(post(origin, { maxResponseBytes: 4096 })), response(large.subarray(0, 4097)));
  assert.deepEqual(await transport().send(post(origin, { maxResponseBytes: 10_000 })), response(large));
  assert.deepEqual(await transport().send(post(origin, { maxResponseBytes: 9_999 })), response(large));
  assert.deepEqual(await transport().send(post(origin, { maxResponseBytes: 9_998 })), response(large.subarray(0, 9_999)));
  // An endless body is cut at the limit rather than read to the deadline.
  const endless = await rawServer(t, (socket) => {
    socket.write("HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n");
    const chunk = `400\r\n${"y".repeat(1024)}\r\n`;
    const timer = setInterval(() => { if (!socket.destroyed) socket.write(chunk); }, 5);
    socket.on("close", () => { clearInterval(timer); });
  });
  const started = Date.now();
  const result = await transport().send(post(endless.origin, { maxResponseBytes: 4096, timeoutMs: 10_000 }));
  assert.ok(Date.now() - started < 5_000);
  assert.equal(result.outcome, "response");
  assert.deepEqual((result as { body: Uint8Array }).body, utf8("y".repeat(4097)));
  // The read is cancelled once the oversized prefix is kept, even when one chunk overshoots it, and a cancel
  // that never settles does not hold the answer.
  for (const settles of [true, false]) {
    let cancels = 0;
    let pulls = 0;
    const overshooting: HttpFetch = async () => ({
      status: 200, redirected: false,
      body: new ReadableStream<Uint8Array>({
        pull(controller) { pulls++; controller.enqueue(new Uint8Array(10_000).fill(7)); },
        cancel() { cancels++; return settles ? undefined : new Promise<void>(() => {}); },
      }, { highWaterMark: 0 }),
    });
    const started = performance.now();
    assert.deepEqual(await transport(overshooting).send(post("http://127.0.0.1:9/", { maxResponseBytes: 4096 })),
      response(new Uint8Array(4097).fill(7)), String(settles));
    assert.ok(since(started) < 1_000);
    assert.equal(cancels, 1, String(settles));
    assert.equal(pulls, 1, String(settles));
  }
});

test("incomplete_or_failed_exchanges_are_failed", async (t) => {
  // A declared length that is not met, and a reset mid-body.
  const short = await rawServer(t, (socket) => {
    socket.end("HTTP/1.1 200 OK\r\ncontent-length: 100\r\n\r\n{\"wire_version\":1");
  });
  assert.deepEqual(await transport().send(post(short.origin)), FAILED);
  const reset = await rawServer(t, (socket) => {
    socket.write("HTTP/1.1 200 OK\r\ncontent-length: 100\r\n\r\n{\"wire_version\":1", () => { socket.destroy(); });
  });
  assert.deepEqual(await transport().send(post(reset.origin)), FAILED);
  const chunked = await rawServer(t, (socket) => {
    socket.end("HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n10\r\n{\"wire_version\":");
  });
  assert.deepEqual(await transport().send(post(chunked.origin)), FAILED);
  // Nothing listening.
  const closed = await rawServer(t, () => {});
  const unreachable = closed.origin;
  await settle();
  const refused = await transport().send(post(unreachable.replace(/:\d+$/, ":1")));
  assert.deepEqual(refused, FAILED);
  // A fetch that throws, rejects, returns no body, or whose stream errors.
  const throwing: HttpFetch = () => { throw new Error("synthetic synchronous failure"); };
  const rejecting: HttpFetch = () => Promise.reject(new TypeError("synthetic network failure"));
  const bodiless: HttpFetch = async () => ({ status: 200, redirected: false, body: null });
  const erroring: HttpFetch = async () => ({
    status: 200, redirected: false,
    body: new ReadableStream<Uint8Array>({ pull(controller) { controller.error(new Error("synthetic stream failure")); } }),
  });
  const nonsense: HttpFetch = async () => undefined as never;
  const textual: HttpFetch = async () => ({
    status: 200, redirected: false,
    body: new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue("receipt" as never); controller.close(); } }),
  });
  for (const fetcher of [throwing, rejecting, bodiless, erroring, nonsense, textual]) {
    assert.deepEqual(await transport(fetcher).send(post("http://127.0.0.1:9/")), FAILED);
  }
});

test("every_status_returns_its_body", async (t) => {
  for (const [status, body] of [[200, "receipt"], [409, "block"], [413, ""], [415, ""], [500, "Internal Server Error"], [503, "retry"]] as const) {
    const { origin } = await httpServer(t, (_request, res) => { res.writeHead(status); res.end(utf8(body)); });
    assert.deepEqual(await transport().send(post(origin)), response(utf8(body)), String(status));
  }
  // A response with no body stream, such as a 204, fails closed (Decision 6); every Portal answer has a body.
  const empty = await httpServer(t, (_request, res) => { res.writeHead(204); res.end(); });
  assert.deepEqual(await transport().send(post(empty.origin)), FAILED);
});

// R14 — Boundary

/** A file's code as the TypeScript parser sees it, so comments never count. */
interface Code {
  readonly dependencies: { readonly specifier: string; readonly values: readonly string[]; readonly dynamic: boolean }[];
  readonly references: string[];
}

function parse(file: string): Code {
  const source = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const code: Code = { dependencies: [], references: [] };
  const literal = (node: ts.Node | undefined) => node !== undefined && ts.isStringLiteralLike(node) ? node.text : "<computed>";
  const named = (elements: readonly (ts.ImportSpecifier | ts.ExportSpecifier)[]) =>
    elements.filter((element) => !element.isTypeOnly).map((element) => (element.propertyName ?? element.name).text);
  const property = (node: ts.Identifier): boolean => {
    const parent = node.parent;
    if (ts.isPropertyAccessExpression(parent) && parent.name === node) return true;
    if ((ts.isPropertyAssignment(parent) || ts.isPropertyDeclaration(parent) || ts.isPropertySignature(parent) || ts.isMethodDeclaration(parent) ||
         ts.isMethodSignature(parent) || ts.isGetAccessor(parent) || ts.isSetAccessor(parent)) && parent.name === node) return true;
    if (ts.isQualifiedName(parent) && parent.right === node) return true;
    return (ts.isBindingElement(parent) || ts.isImportSpecifier(parent) || ts.isExportSpecifier(parent)) && parent.propertyName === node;
  };
  const visit = (node: ts.Node): void => {
    if (ts.isImportDeclaration(node)) {
      const clause = node.importClause;
      const values: string[] = [];
      if (clause === undefined) values.push("<side effect>");
      else if (!clause.isTypeOnly) {
        if (clause.name) values.push("default");
        if (clause.namedBindings && ts.isNamespaceImport(clause.namedBindings)) values.push("*");
        if (clause.namedBindings && ts.isNamedImports(clause.namedBindings)) values.push(...named(clause.namedBindings.elements));
      }
      code.dependencies.push({ specifier: literal(node.moduleSpecifier), values, dynamic: false });
    } else if (ts.isExportDeclaration(node) && node.moduleSpecifier !== undefined) {
      const values = node.isTypeOnly ? [] : node.exportClause && ts.isNamedExports(node.exportClause) ? named(node.exportClause.elements) : ["*"];
      code.dependencies.push({ specifier: literal(node.moduleSpecifier), values, dynamic: false });
    } else if (ts.isImportEqualsDeclaration(node)) {
      code.dependencies.push({ specifier: "<import equals>", values: ["="], dynamic: true });
    } else if (ts.isCallExpression(node) &&
               (node.expression.kind === ts.SyntaxKind.ImportKeyword || (ts.isIdentifier(node.expression) && node.expression.text === "require"))) {
      code.dependencies.push({ specifier: literal(node.arguments[0]), values: ["*"], dynamic: true });
    }
    if (ts.isIdentifier(node) && !property(node)) code.references.push(node.text);
    ts.forEachChild(node, visit);
  };
  visit(source);
  return code;
}

test("the_transport_stays_inside_its_boundary", async (t) => {
  // The injected fetch is the only way out: no other network, Node, logging, or storage global.
  const FORBIDDEN = new Set([
    "XMLHttpRequest", "WebSocket", "EventSource", "navigator", "process", "Buffer", "require", "global", "globalThis",
    "console", "localStorage", "sessionStorage", "indexedDB", "caches",
  ]);
  const files = (readdirSync(MODULE_DIRECTORY, { recursive: true }) as string[]).filter((name) => name.endsWith(".ts")).map((name) => join(MODULE_DIRECTORY, name));
  assert.ok(files.includes(MODULE));
  for (const file of files) {
    const code = parse(file);
    for (const { specifier, values, dynamic } of code.dependencies) {
      assert.equal(dynamic, false, `${file}: ${specifier}`);
      // No value import at all: only types, or helpers in this directory.
      if (values.length === 0) continue;
      assert.match(specifier, /^\.\//, `${file}: ${specifier}`);
    }
    assert.deepEqual(code.references.filter((name) => FORBIDDEN.has(name)), [], file);
  }
  // At run time, every exchange goes through the injected fetch, once.
  const { origin } = await httpServer(t, (_request, res) => { res.end(utf8("ok")); });
  const { spy, calls } = spyFetch();
  const http = transport(spy);
  await http.send(post(origin));
  await http.send(get(origin));
  assert.equal(calls.length, 2);
});
