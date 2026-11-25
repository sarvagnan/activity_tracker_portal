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

This indexes archived items through the published envelope decoder, quarantines
unusable envelopes, and reconciles Source Records. `processor_versions` defaults
to an empty object. A configured map such as `{"steps": "interpret-steps@1"}`
enqueues work under that version. To enable claiming, configure `job_handler`
as an importable `package.module:callable`, for example:

```json
{
  "store_path": "/path/to/portal.db",
  "reserve_bytes": 1000000000,
  "processor_versions": {"steps": "interpret-steps@1"},
  "job_handler": "my_deployment.processing:handle"
}
```

The named synchronous callable receives one `Lease` per claim, outside the writer
executor and shared lock. It owns interpretation, lease renewal, completion, and
failure classification, using the published store APIs and the lease's fence.
The deployment provides its store access and configuration; the supervisor does
not inject a store or decide outcomes. `execute-processing-job@1` will supply the
governed processing semantics; no production handler ships with this supervisor.
The entry point is trusted deployment code: importing its module runs Python code.
Import or callable lookup failures exit with a fixed configuration diagnostic,
before opening the writer. With `job_handler` omitted or null, claiming remains
disabled and a fixed warning is logged once at startup; discovery still runs.
A handler can consume existing jobs without a `processor_versions` map; that map
only controls discovery enqueueing.

Optional configuration fields and defaults:

| Field | Default | Meaning |
| --- | --- | --- |
| `job_handler` | `null` | Importable `package.module:callable` receiving a `Lease`; absent disables claims |
| `processor_versions` | `{}` | Record type to processor version for enqueue discovery |
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
and handlers hold no shared write lock. Lock files are retained on disk to keep
their inode identities stable.
