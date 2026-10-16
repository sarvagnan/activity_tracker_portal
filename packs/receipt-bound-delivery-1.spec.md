# Draft acceptance pack — `receipt-bound-delivery@1`

**Status:** Draft approved by the Operator on 2026-10-16, including the nine decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `receipt-bound-delivery` Governed Scope contract in `governance/scopes/receipt-bound-delivery.1.json`.

ADR 0009 requires receipt-bound delivery, and ADR 0038 joins Sender transmission and Portal acceptance "by receipt-bound integration evidence". Each side is published and tested alone:
- `send-outbox-message@1` removes a head only on its matching receipt;
- the four `accept-*` units answer only after the Operational Store commits.

Nothing yet shows the two halves agree across a real network and a real store. This unit is that evidence. It:
1. runs the published Sender units against the Portal ingress over real HTTP;
2. injects the network faults a phone sees;
3. restarts either side;
4. checks after every step that a message leaves the Sender only once the Portal has durably archived its exact bytes.

The proof needs one piece nobody has built: the Sender HTTP transport behind the `transport` seams of `send-outbox-message@1` and `manage-sender-enrollment@1`. Both units' contracts leave the HTTP client to the Sender shell enabling task. The transport is written here as that enabling work, and this pack pins only the network-seam behaviour the proof relies on (Decision 1).

## Interface

**TypeScript**, module `sender/app/src/transport/http-transport.ts`, in its own directory. It reaches the network only through an injected `fetch`.
- `HttpRequest`: `{ method: "POST" | "GET", url, headers, body: Uint8Array | null, timeoutMs, maxResponseBytes }`. This is the shape of both `OutboxRequest` and `EnrollmentRequest`.
- `HttpResult`: `{ outcome: "response", body: Uint8Array } | { outcome: "failed" }`.
- `HttpFetch`: the WinterCG `fetch` signature. The shell passes `expo/fetch`, which streams response bodies; the tests pass Node's global `fetch`. Both satisfy `HttpFetch` without a cast, because the outgoing body is a fresh `Uint8Array<ArrayBuffer>` copy, and the typecheck checks it.
- `createHttpTransport({ fetch })` returns `{ send(request): Promise<HttpResult> }`. The same object serves as `OutboxTransport` and `EnrollmentTransport`.

**Python**, test support only: an in-process Portal driven by the oracle. It runs the ingress app from `create_app` under uvicorn on a loopback port, with a controlled clock, the real Operational Store file, and a fault proxy in front.

## Requirements

The nine TypeScript `node:test` tests in `receipt-bound-delivery-1.test.ts` check the transport against real loopback HTTP servers made with `node:http` and `node:net`, and against fake fetches for exchanges no server produces.

The Python suite `tests/acceptance/test_receipt_bound_delivery_1.py`, with 11 tests, checks the contract and the end-to-end evidence. It drives the Sender through a Node driver, `sender/app/test/support/receipt-bound-sender.ts`, one fresh process per step, so every step is also a Sender restart. The driver uses:
- the real stage units, `manage-sender-enrollment@1`, and `send-outbox-message@1`;
- this unit's transport;
- the 3ug `node:sqlite` substitute on a file;
- a synthetic cipher whose key is fixed per alias, so a new process can reopen the Outbox.

Fixtures are synthetic. The runner verifies a fresh JUnit report for the TypeScript suite and a clean Python run, sets per-test timeouts, and ends both under an external deadline.

### R1 — Governed Scope contract

The contract names:
- the scope, version, kind, and purpose;
- inputs, with their modes;
- outputs;
- four named failures in the outcome class `retryable`;
- one effect;
- authority;
- the six dependencies;
- the acceptance oracle.

It does not contradict the scope-map row.

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — Caller errors

`createHttpTransport` throws `TypeError` without a `fetch` function. `send` rejects with `TypeError`, before calling `fetch`, when:
- the method is not `POST` or `GET`;
- the URL is not an absolute `http` or `https` URL;
- the headers are not an object of string values;
- the body is not a `Uint8Array` for `POST`, or not `null` for `GET`;
- `timeoutMs` or `maxResponseBytes` is not a positive safe integer.

Tests: `malformed_options_and_requests_are_caller_errors`.

### R3 — One exact request to the given URL

Each `send` calls `fetch` exactly once, with:
- the request's method and URL;
- exactly its headers;
- its body bytes unchanged, or no body for `GET`;
- `redirect: "error"` and `credentials: "omit"`.

The server receives those exact bytes, with no added content encoding. The transport never retries. Mutating the request's body or headers after `send` returns changes nothing that was sent, even for a `fetch` that reads its arguments only after a tick, so the transport copies both before calling `fetch`.

Tests: `the_request_is_sent_exactly_once`, `no_retry_and_no_encoding_is_added`.

### R4 — Redirects are never followed

A 3xx answer, a `fetch` that rejects because of the redirect, a `fetch` that returns any 3xx status itself, or a response marked `redirected` is `failed` (`redirect-refused`). The redirect target receives no request. This keeps health data on the configured Portal (Decision 4). Refusing a 3xx is the only use of the status: Decision 7's "the status code is not read" covers every other status.

Tests: `redirects_are_refused_and_never_followed`.

### R5 — One timeout over the whole exchange

The transport aborts the exchange `timeoutMs` after `send` begins, whether headers or body bytes are outstanding, and returns `failed` (`request-timed-out`). A response completed before the deadline is unaffected. Under mocked timers, the abort comes exactly at the deadline and not one millisecond before, whether headers or body bytes are outstanding. That includes headers that arrive 4,000 ms into a 5,000 ms exchange: the deadline stays 5,000 ms from the call and does not restart. Over real sockets, a stalled exchange ends no earlier than 50 ms before its deadline and within a generous margin after it, on a monotonic clock.

Tests: `a_stalled_exchange_times_out`.

### R6 — A bounded, complete body read

The body is read as a stream. At most `maxResponseBytes + 1` bytes are kept. A chunk may carry more than that, so the read is cancelled as soon as the kept prefix is complete, without waiting for the cancellation to settle. `send-outbox-message@1` and `manage-sender-enrollment@1` treat such a body as oversized. Any other outcome is `failed` (`network-failed` or `response-incomplete`):
- a reset or truncated body;
- a declared length that is not met;
- a body stream that errors, or yields anything but bytes;
- a `fetch` that rejects or throws.

A body that ends normally is returned for every status but a refused 3xx, including an empty body, as the units decide on bytes alone. A response with no body stream, such as a 204, fails closed (Decision 6). Every Portal answer has a body.

Tests: `the_body_read_is_bounded`, `incomplete_or_failed_exchanges_are_failed`, `every_status_returns_its_body`.

### R7 — Every kind delivered end to end

A synthetic installation is enrolled through the Portal's offer, the Sender's `enroll` over HTTP, and the Operator's confirmation. The Sender then stages and delivers:
- the capability acknowledgement;
- an Initial Snapshot's Ingest Batches and Snapshot Completion;
- a Change Cycle's batches and Change Completion;
- a Sync Checkpoint.

Each message is delivered exactly once, and the Sender's Outbox ends empty. The Portal's store holds each message's exact bytes, compared by SHA-256 with the plaintext the Sender staged, and `manage-sender-enrollment@1` reports the installation confirmed.

Tests: `every_kind_is_delivered_end_to_end`.

### R8 — Nothing is lost before its receipt

Each of these leaves the message in the Sender's Outbox, and the next attempt resends identical bytes:
- a pending installation's retryable answers;
- a request lost before the Portal;
- a response lost or truncated after the Portal committed;
- a reset connection;
- a Portal whose store refuses to admit (its storage reserve is exhausted).

The message is removed only on its own receipt: every message the Sender no longer holds had its receipt reach the Sender in full.

Tests: `pending_deliveries_stay_queued`, `lost_requests_and_responses_keep_the_message`, `a_refusing_store_keeps_the_message`.

### R9 — Identical retries, identical receipts

Every request for a sequence carries identical bytes, across Sender restarts. A retry after a lost or truncated response gets a receipt byte-identical to the one the Portal first produced, including after the Portal restarts on the same store. Every receipt the Portal produces for a sequence, in every test, is byte-identical. The Portal archives the message once.

Tests: `retries_get_the_identical_receipt`, `a_portal_restart_keeps_the_receipt`.

### R10 — No receipt before commit

After every step, every message the Sender has removed is archived in the Portal's store with identical bytes. A message the Portal has not committed is still in the Sender's Outbox. The proxy also checks, at the moment each receipt leaves the Portal and before any of it reaches the Sender, that the store already holds the exact message the receipt names. The Portal's store is read through a separate read-only connection. Each staging step's outcome is checked, and its new sequences are reconciled against the Sender's high-water mark and the kinds that step stages.

Tests: covered by every R7–R12 test, and by `faulted_histories_keep_one_durability_meaning`.

### R11 — Last Delivery from the Portal's receipt time

The Portal clock is controlled. After each newly accepted message, of any kind, Last Delivery equals the Portal time at which that message was accepted. These leave it unchanged even when the Portal clock has moved on:
- an identical retry;
- a retryable answer;
- a Delivery Block.

Tests: `last_delivery_follows_new_acceptances_only`.

### R12 — A blocked head stays and stops later messages

A message the Portal blocks stays at the Sender's head with its head block, together with every later message. The Portal records the block. Later messages are not sent while it stands. The test stages an Ingest Batch under a projection schema version that the Portal's generation does not support.

Tests: `a_blocked_head_keeps_every_later_message`.

### R13 — Faulted histories keep one durability meaning

Seeded synthetic histories interleave:
- staging of every kind;
- delivery attempts through each proxy fault;
- Portal restarts;
- Sender restarts.

After every step, R8–R11's invariants hold. Each seed, on its own, must exercise every fault, every kind, a Portal restart, and a retried lost response. A scripted prefix guarantees that for every seed, the random steps follow, and the deliveries that drain the Outbox count too.

Tests: `faulted_histories_keep_one_durability_meaning`.

### R14 — Boundary

The transport module and any helpers in its directory:
- reach the network only through the injected `fetch`, naming no `XMLHttpRequest`, `WebSocket`, or Node global;
- import no other Sender module's values;
- never log;
- touch no storage.

Tests: `the_transport_stays_inside_its_boundary`.

## Decisions for the Operator

1. **The unit owns the evidence; its implementation is the Sender HTTP transport, written as the shell enabling work.** `send-outbox-message@1` and `manage-sender-enrollment@1` leave the HTTP client to the Sender shell enabling task, and the end-to-end proof to this unit. Both are published contracts, so this unit keeps that split.
   - It writes the transport as that enabling work, and this pack pins only the network-seam behaviour the proof depends on (R2–R6).
   - Without a transport the Sender cannot reach the Portal, so RED is genuine.
   - The alternatives are:
     - to govern the HTTP client outright in this unit, which contradicts both published contracts and would need their rebinding;
     - or a proof with no new code, whose RED could rest only on a missing Portal route.
2. **The proof runs over real HTTP and real storage.** Fakes cannot show that the halves agree. It uses:
   - a real loopback socket, with the Portal's `create_app` under uvicorn in the test process and a real Operational Store file;
   - the published Sender units on a real SQLite file;
   - a fresh Node process for every Sender step.
   
   The Portal runs in-process so that the Operator's offer and confirmation go through the same `PortalWriter`, and so that the clock can be controlled. A separate Portal process could not take Operator actions while it holds the writer lock.
3. **Faults are injected by a proxy, not by hooks in the units.** A loopback proxy between Sender and Portal can:
   - drop a request before forwarding it;
   - drop a response after the Portal answered;
   - truncate a response;
   - reset a connection.
   
   The Portal is restarted on the same store, and a store refusal comes from an exhausted storage reserve. No published unit is wrapped or patched.
4. **The transport never follows a redirect.** A redirected Outbox Message would carry health data to a host the Operator never configured. It is reported as no response, so the message stays, and the Operator sees delivery stall rather than data leak. The alternative is to follow same-origin redirects only.
5. **One timeout covers the whole exchange.** `timeoutMs` bounds headers and body together, so a Portal that sends headers and then stalls cannot hold a delivery attempt open. The alternative is a separate read timeout.
6. **The transport reads bodies as streams, and the shell passes `expo/fetch`.** React Native's built-in `fetch` cannot stream a response body, so it could not bound the read. The transport therefore requires a streaming `fetch` and fails closed on one that lacks a readable body. The device smoke run checks `expo/fetch` on Android. The alternative is to buffer whole bodies, which leaves the read unbounded.
7. **The status code is not read, as `send-outbox-message@1` Decision 1 decided.** The transport returns any completed body. The Portal route's statuses (200, 503, 409) are for people and proxies.
8. **The evidence checks durability, not timing.** It promises nothing about how fast a message is delivered, as ADR 0009's synchronization is eventual. It checks only that removal never precedes archival, that nothing is lost, and that retries stay identical.
9. **The row's text changes now.** Its inputs name the Sender HTTP transport and the Portal ingress, and its outputs name the transport's pinned behaviour. Its dependencies gain `manage-sender-enrollment`, through whose `enroll` the proof's installation enrolls and whose transport seam this transport also serves. No published suite pins this row.

## Ground rules

- Fixtures are synthetic.
- The Portal and its store run on loopback only.
- The Sender driver's cipher is a test substitute with fixed keys.
- Nothing is mocked inside a published unit.

## Freeze prerequisites

After Gate A, the executable pack adds:
- the TypeScript suite;
- the Python suite;
- the Node Sender driver;
- the runner;
- the Interface stub `http-transport.ts`, whose `send` throws and whose types are declared.

**Implementability review.** GPT 6.1 Sol's read-only review found three blocking gaps, all now resolved:
- R4 reads the status only to refuse 3xx, consistently with Decision 7;
- the contract and R6 bound the bytes kept, not the bytes a stream chunk carries, and cancel at once;
- the outgoing body type is `Uint8Array<ArrayBuffer>`, so both fetches satisfy `HttpFetch` without a cast.

Its non-blocking findings are added too:
- the proxy checks the commit at the moment a receipt leaves the Portal;
- every removal must match a fully forwarded receipt, and receipts are compared throughout;
- each seed's coverage is asserted on its own;
- staging outcomes and new sequences are reconciled;
- the abort signal and the read cancellation, including one that never settles, are observed;
- the deadline is checked exactly under mocked timers, with generous monotonic bounds on real sockets;
- the device smoke run (Beads `kv7`) must exercise `expo/fetch` on Android for a truncation before the reader is obtained, a stalled body's abort, and an oversized body's cancellation.

Against the stub, all nine transport tests and all nine end-to-end tests fail, and the two contract tests pass. A throwaway reference transport passes every test in about 40 seconds. The transport suite kills all 29 of its non-equivalent mutants, and the end-to-end suite is run for any mutant the transport suite misses. The runner gives each half an external deadline, because the end-to-end suite starts Node processes. The Portal's Outbox Message route (Beads `2rv.4`) is merged (PR #38), and this branch includes it. GPT 6.1 Sol's read-only implementability review runs before Gate B. Every dependency is published, so Vouch records `specify` after Gate A.

The binding is:
- spec files: the contract and this pack;
- test files:
  - `receipt-bound-delivery-1.test.ts`;
  - `test_receipt_bound_delivery_1.py`;
  - the Sender driver `sender/app/test/support/receipt-bound-sender.ts`;
  - the 3ug substitutes it uses, `sender/app/test/support/node-database.ts` and `sender/app/test/support/node-installation-cipher.ts`;
  - the runner.
