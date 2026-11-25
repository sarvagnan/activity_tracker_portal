"""Supervise published store operations; processing semantics belong to the handler.

Discovery has a minimum interval even under sustained job load. Every discovery
stage and the claim queue gets a bounded turn, so no stage drains its backlog
before another runs. Future summary/export queues must join these bounded turns.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import re
import signal
import threading
import time
from types import MappingProxyType
from uuid import uuid4

from activity_tracker_portal import operational_store as store_module
from activity_tracker_portal.ingress import PortalWriter, PortalWriterBusy
from activity_tracker_portal.interpret_source_projection import Quarantine, decode_envelope
from activity_tracker_portal.operational_store import Lease, ProjectionEntry, RetractionEntry, StoreRefusal, StoreRetryable
from activity_tracker_portal.reconcile_source_record import ReconcileRefusal, ReconcileRetryable, reconcile

LOG = logging.getLogger("activity_tracker_portal.worker")


@dataclass(frozen=True)
class WorkerSettings:
    """Worker JSON config: required store path/reserve, optional scheduling knobs."""

    store_path: Path
    reserve_bytes: int
    processor_versions: Mapping[str, str] = field(default_factory=dict)
    batch_size: int = 100
    claim_batch_size: int = 10
    discovery_interval: float = 30.0
    idle_seconds: float = 1.0
    max_idle_seconds: float = 30.0
    lease_seconds: int = 60
    max_attempts: int = 3

    def __post_init__(self) -> None:
        try:
            if not str(self.store_path) or "\x00" in str(self.store_path):
                raise ValueError
            if type(self.reserve_bytes) is not int or self.reserve_bytes < 0:
                raise ValueError
            for value, maximum in ((self.batch_size, 1000), (self.claim_batch_size, 1000),
                                   (self.lease_seconds, 86400), (self.max_attempts, 100)):
                if type(value) is not int or not 1 <= value <= maximum:
                    raise ValueError
            for value in (self.discovery_interval, self.idle_seconds, self.max_idle_seconds):
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise ValueError
            if self.max_idle_seconds < self.idle_seconds:
                raise ValueError
            for record_type, version in self.processor_versions.items():
                if not isinstance(record_type, str) or re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", record_type) is None:
                    raise ValueError
                if not isinstance(version, str) or re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*@[0-9]+", version) is None:
                    raise ValueError
            object.__setattr__(self, "processor_versions", MappingProxyType(dict(self.processor_versions)))
            object.__setattr__(self, "store_path", Path(self.store_path))
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise ValueError("invalid worker configuration") from None

    @classmethod
    def from_file(cls, path: str | Path) -> WorkerSettings:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if type(data) is not dict or type(data.get("store_path")) is not str or not data["store_path"]:
                raise ValueError
            return cls(**data)
        except (OSError, UnicodeError, ValueError, TypeError):
            raise ValueError("invalid worker configuration") from None


class Worker:
    """One synchronous lease loop with injectable time, wait, stop, and handler.

    The handler receives only a Lease, outside the writer executor and all
    locks. It owns renewal, outcomes, and failure classification. An unexpected
    handler exception is logged without its text; its lease is left untouched
    for the governed expiry/recovery path.
    """

    def __init__(self, writer: PortalWriter, settings: WorkerSettings, *,
                 handler: Callable[[Lease], None] | None = None,
                 worker_id: str | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], object] | None = None,
                 stop: threading.Event | None = None) -> None:
        self.writer, self.settings, self.handler = writer, settings, handler
        self.worker_id = worker_id if worker_id is not None else f"portal-worker-{uuid4()}"
        if not isinstance(self.worker_id, str) or not self.worker_id:
            raise ValueError("invalid worker identity")
        self.clock, self.monotonic = clock, monotonic
        self.stop = stop if stop is not None else threading.Event()
        self.sleep = sleep if sleep is not None else self.stop.wait
        self._retry_records: list[int] = []
        self._retryable = False
        self._attempt_failed = False
        self._deferred: dict[str, object] = {}

    def _attempt(self, stage, operation):
        self._attempt_failed = False
        try:
            return operation()
        except (StoreRetryable, ReconcileRetryable) as error:
            self._retryable = True
            failure = error.failure if error.failure in store_module.FAILURES | {"evidence-moved"} else "unknown"
            LOG.warning("worker %s retryable: %s", stage, failure)
        except (StoreRefusal, ReconcileRefusal) as error:
            failure = error.failure if error.failure in store_module.FAILURES else "unknown"
            LOG.warning("worker %s refused: %s", stage, failure)
        except Exception:
            # No exception text, traceback, values, positions, or identities.
            LOG.error("worker %s unexpected failure", stage)
        self._attempt_failed = True
        return None

    def _batch(self, stage, items, key, operation) -> None:
        # One bounded lookahead lets even batch_size=1 pass a refusing head.
        # Defer that head for one turn, then retry it; no durable state or
        # domain classification is invented, and memory remains bounded.
        if not items:
            return
        deferred = self._deferred.pop(stage, None)
        ordered = sorted(items, key=lambda item: key(item) == deferred)
        for item in ordered[:self.settings.batch_size]:
            if self.stop.is_set():
                break
            self._attempt(stage, lambda: operation(item))
            if self._attempt_failed:
                self._deferred[stage] = key(item)

    def _read(self, operation):
        with self.writer.store.read() as view:
            return operation(view)

    def _index(self, item) -> None:
        if item.projection is None:
            entry = RetractionEntry(item.position)
        else:
            envelope = decode_envelope(item.projection, item.record_type, item.projection_schema_version)
            if isinstance(envelope, Quarantine):
                self.writer.run(lambda store: store.quarantine_item(
                    item.position, envelope.reason, envelope.pointer, self.clock()))
                return
            entry = ProjectionEntry(item.position, envelope.source_system_kind, envelope.origin,
                                    envelope.source_identity, envelope.last_modified_time)
        # One item per call isolates a refusal without dropping its neighbors.
        self.writer.run(lambda store: store.index_items((entry,), self.clock()))

    def _reconcile(self, record_id: int) -> None:
        try:
            self.writer.run(lambda store: reconcile(store, record_id, self.clock()))
        except ReconcileRetryable as error:
            if error.failure == "evidence-moved":
                self._retry_records.append(record_id)
            raise

    def cycle(self, *, discover: bool) -> int:
        """Give each available stage a bounded turn; return leases dispatched."""
        self._retryable = False
        limit = self.settings.batch_size + 1
        if discover and not self.stop.is_set():
            items = self._attempt("index-discovery", lambda: self._read(lambda view: view.items_awaiting_index(limit)))
            self._batch("index", items or (), lambda item: item.position, self._index)

        records = self._retry_records
        self._retry_records = []
        if discover and not self.stop.is_set():
            found = self._attempt("selection-discovery", lambda: self._read(lambda view: view.records_awaiting_selection(limit)))
            records = list(dict.fromkeys(records + list(found or ())))
        self._batch("reconcile", records, lambda record_id: record_id, self._reconcile)

        if discover and not self.stop.is_set():
            selections = self._attempt("processing-discovery", lambda: self._read(
                lambda view: view.selections_awaiting_processing(self.settings.processor_versions, limit)))
            self._batch("enqueue", selections or (), lambda selection: selection.record_id,
                        lambda selection: self.writer.run(lambda store: store.enqueue_processing(
                    selection.record_id, selection.selection_id, selection.processor_version,
                    self.settings.max_attempts, self.clock())))

        claimed = 0
        if self.handler is not None:
            for _ in range(self.settings.claim_batch_size):
                if self.stop.is_set():
                    break
                lease = self._attempt("claim", lambda: self.writer.run(lambda store: store.claim_processing_job(
                    self.worker_id, self.settings.lease_seconds, self.clock())))
                if lease is None:
                    break
                claimed += 1
                self._attempt("handler", lambda: self.handler(lease))
        return claimed

    def run(self, *, max_cycles: int | None = None) -> None:
        """Run until stopped; max_cycles is a bounded synthetic/supervision seam."""
        if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 0):
            raise ValueError("invalid cycle bound")
        next_discovery = float("-inf")
        idle_delay = self.settings.idle_seconds
        cycles = 0
        while not self.stop.is_set() and (max_cycles is None or cycles < max_cycles):
            discover = self.monotonic() >= next_discovery
            if discover:
                next_discovery = self.monotonic() + self.settings.discovery_interval
            claimed = self.cycle(discover=discover)
            cycles += 1
            if self.stop.is_set() or (max_cycles is not None and cycles >= max_cycles):
                break
            if not claimed or self._retryable:
                # Never oversleep the next discovery turn under idle backoff.
                self.sleep(min(idle_delay, max(0.0, next_discovery - self.monotonic())))
                idle_delay = min(idle_delay * 2, self.settings.max_idle_seconds)
            else:
                idle_delay = self.settings.idle_seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="portal-worker", description="Run the supervised Portal lease worker")
    parser.add_argument("--config", required=True, help="Path to the worker JSON configuration")
    parser.add_argument("--max-cycles", type=int, help="Exit after this many bounded cycles")
    args = parser.parse_args(argv)
    if args.max_cycles is not None and args.max_cycles < 0:
        parser.error("--max-cycles must be non-negative")
    try:
        settings = WorkerSettings.from_file(args.config)
    except ValueError:
        LOG.error("invalid worker configuration")
        return 1

    stop = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        # Startup retryables back off too, without disclosing filesystem errors.
        while not stop.is_set():
            try:
                writer = PortalWriter(settings.store_path, reserve_bytes=settings.reserve_bytes, role="worker")
                break
            except StoreRetryable:
                LOG.warning("worker startup retryable")
                stop.wait(settings.idle_seconds)
            except (StoreRefusal, PortalWriterBusy, OSError):
                LOG.error("worker startup refused")
                return 1
        else:
            return 0
        try:
            Worker(writer, settings, stop=stop).run(max_cycles=args.max_cycles)
        finally:
            writer.close()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0
