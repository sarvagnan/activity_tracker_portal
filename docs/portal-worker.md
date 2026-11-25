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
| `batch_size` | `100` | Maximum items per discovery stage (1–1000) |
| `claim_batch_size` | `10` | Maximum claims before yielding another scheduling turn (1–1000) |
| `discovery_interval` | `30` | Minimum seconds between global discovery reads |
| `idle_seconds` | `1` | Initial idle/retryable backoff in seconds |
| `max_idle_seconds` | `30` | Maximum exponential idle backoff in seconds |
| `lease_seconds` | `60` | Lease duration passed to the store (1–86400) |
| `max_attempts` | `3` | Attempt bound passed when enqueueing (1–100) |

Each discovery turn indexes, reconciles, and enqueues bounded batches, then gives
the processing claim queue a bounded turn. Sustained claims cannot suppress
periodic discovery; discovery cannot drain an unbounded backlog before claims
run. No global counts are polled. Idle waits stop at the next discovery deadline.
Evidence that moves during reconciliation is retried on the next turn without
another global scan. Discovery reads at most one extra item per stage so a
refusing head can be deferred for one turn, allowing progress even with a
one-item batch. Refusals are isolated per item; store-busy and reserve
retryables trigger backoff. Logs contain only stage names and allowlisted failure
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
