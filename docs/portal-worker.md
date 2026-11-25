# Portal worker

Run the worker as a separately supervised process alongside `portal serve`:

```sh
uv run --frozen portal-worker --config /path/to/worker.json
```

The worker uses its own JSON configuration. `store_path` and `reserve_bytes`
must match the ingress configuration. Paths are relative to the working
directory, as for ingress. The smallest configuration is:

```json
{
  "store_path": "/path/to/portal.db",
  "reserve_bytes": 1000000000
}
```

The command indexes archived items through the published envelope decoder,
quarantines unusable envelopes, reconciles Source Records, and discovers and
claims Processing Jobs. It builds `execute_processing_job.make_job_handler`
with its own worker-role `PortalWriter` and the same clock used for claims.
The governed handler interprets each leased job and records its outcome or
failure. The supervisor ignores the handler's result and owns no job semantics.
No second writer is opened by the handler.

Discovery defaults to the published `execute_processing_job.PROCESSOR_VERSIONS`
map: the four modeled record types use their published processors and the other
projection types use the common interpreter to record Unmodeled. An explicit
`processor_versions` object **replaces** that map; it is not merged with defaults.
For example, `{"steps": "interpret-steps@1"}` discovers only Steps, and `{}`
disables discovery enqueueing while existing jobs can still be claimed. Every
configured entry must match that record type's published mapping. Unknown types,
unregistered versions, and processor substitutions are rejected with the fixed
configuration diagnostic. The processor registry remains governed code.

The former importable `job_handler` configuration field has been removed; a file
containing it is rejected, including a null value. Deployments cannot replace the
governed execution semantics through configuration. For tests and acceptance
packs, `Worker(writer, settings, handler=...)` still accepts a synchronous callable
receiving a `Lease`. That callable runs outside the writer executor and shared
lock and may return any result; the supervisor ignores it. Omitting the handler
on this low-level constructor skips claims. The command always supplies the
governed handler. `main(..., clock=...)` supplies an injected clock to both the
handler and the worker for synthetic checks.

Optional configuration fields and defaults:

| Field | Default | Meaning |
| --- | --- | --- |
| `processor_versions` | Published `PROCESSOR_VERSIONS` | Replacement subset of the governed discovery map; `{}` disables enqueueing |
| `batch_size` | `100` | Maximum item attempts per discovery stage (1–1000) |
| `max_discovery_window` | `10000` | Maximum results requested per stage read (1–10000, at least `batch_size`) |
| `refusal_retry_seconds` | `60` | Positive minimum seconds before a remembered discovery failure is eligible for retry |
| `claim_batch_size` | `10` | Maximum claims before yielding another scheduling turn (1–1000) |
| `discovery_interval` | `30` | Minimum seconds between global discovery reads |
| `idle_seconds` | `1` | Initial idle/retryable backoff in seconds |
| `max_idle_seconds` | `30` | Maximum exponential idle backoff in seconds |
| `lease_seconds` | `60` | Lease duration passed to the store (1–86400) |
| `max_attempts` | `3` | Attempt bound passed when enqueueing (1–100) |

Each discovery turn indexes, reconciles, and enqueues bounded batches, then gives
the processing claim queue a bounded turn. Sustained claims cannot suppress
periodic discovery; discovery cannot drain an unbounded backlog before claims
run. No global counts are polled. Idle waits stop at the next discovery deadline;
retryable failures always wait for the current positive backoff, even when a slow
cycle has passed that deadline. Evidence that moves during reconciliation is
retried on the next turn without another global scan, subject to the stage's
batch bound. Discovery calls only the published store reads, once per stage per
discovery interval, with their published parameters. Claiming stays the hot path
between discovery turns (DEV-os4-01).

Each stage remembers failed item keys and retry deadlines on the injected
monotonic clock. The set holds at most `max_discovery_window - batch_size` keys;
a read requests `batch_size + remembered keys` results, capped by the configured
window. Remembered keys whose deadline has not arrived are skipped. Fresh work
and due retries alternate priority when both exist, and retries rotate in attempt
order, so even a one-item batch makes progress behind multiple refusers while
retrying them. Each turn attempts at most `batch_size` items per stage. Successful
items and remembered keys absent from the next read leave the set. Evidence-moved
retries can run between discovery turns, still within the reconciliation batch
bound. All scheduling memory resets on process restart; it changes no durable
refusal or job semantics.

There is a residual discovery limit: more persistent refusers than
`max_discovery_window - batch_size` can hide later work beyond the bounded prefix.
When an additional failed key cannot be remembered, the worker stops adding keys
and logs `worker <stage> discovery window saturated` once per stage per process
(`index`, `reconcile`, or `enqueue`). Operators can resolve the underlying refusals
or raise the configured window within its limit; governed cursor reads would need
an operational-store successor. Memory and per-turn read results remain bounded
at saturation. Refusals are isolated per item; store-busy and reserve retryables
trigger backoff. Logs contain only fixed stage diagnostics and allowlisted failure
names, without projection values, Source Identities, exception text, or tracebacks.

`SIGINT` and `SIGTERM` stop between operations and interrupt idle waits. A handler
already running must return before shutdown; its lease lifecycle remains the
handler's responsibility. `--max-cycles N` exits after a bounded number of loop
turns for synthetic checks. Each process receives a fresh opaque worker identity.

Ingress retains its `.writer-lock` singleton, and the worker uses `.worker-lock`.
Both use `.store-write-lock` for individual store mutations with a five-second
acquire limit, matching the store's default busy timeout. Contention raises
`store-busy` through the existing retryable adapters. Decoding, other computation,
and handler computation hold no shared write lock. The governed handler uses
the worker writer for its individual mutations. Lock files are retained on disk
to keep their inode identities stable.
