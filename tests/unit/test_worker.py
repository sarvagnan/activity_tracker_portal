"""Worker supervisor integration tests with real stores and synthetic projections."""

from contextlib import ExitStack, nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import ast
import fcntl
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from activity_tracker_portal import execute_processing_job as execution, ingress, operational_store as store_module, worker
from activity_tracker_portal.operational_store import BatchItem, IngestBatchRecord, ItemPosition, MessageHeader, SourceKey
from activity_tracker_portal.reconcile_source_record import ReconcileRefusal, ReconcileRetryable

ROOT = Path(__file__).resolve().parents[2]
VECTORS = json.loads((ROOT / "projection/v1/vectors/positive.json").read_text())["vectors"]
NOW = datetime(2026, 11, 25, tzinfo=timezone.utc)
INSTALLATION = "00000000-0000-4000-8000-000000000001"
CYCLE = "00000000-0000-4000-8000-000000000002"
PV = "interpret-steps@1"


class FakeTime:
    def __init__(self):
        self.elapsed = 0.0
        self.waits = []

    def clock(self):
        return NOW + timedelta(seconds=self.elapsed)

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.elapsed += seconds


class DiscoveryTests(unittest.TestCase):
    def test_refuser_memory_and_read_windows_are_bounded_for_every_stage(self):
        fake_time = FakeTime()
        reads = {stage: [] for stage in ("index", "selection", "processing")}
        attempts = {stage: [] for stage in reads}

        class View:
            # Deliberately only the published parameters, with no **kwargs.
            def items_awaiting_index(self, limit):
                reads["index"].append(limit)
                return tuple(SimpleNamespace(position=number) for number in range(1, limit + 1))

            def records_awaiting_selection(self, limit):
                reads["selection"].append(limit)
                return tuple(range(1, limit + 1))

            def selections_awaiting_processing(self, processor_versions, limit):
                reads["processing"].append(limit)
                return tuple(SimpleNamespace(record_id=number, selection_id=number, processor_version=PV)
                             for number in range(1, limit + 1))

            def enqueue_processing(self, record_id, selection_id, version, max_attempts, at):
                fail("processing", record_id)

        def fail(stage, identity):
            attempts[stage].append(identity)
            raise store_module.StoreRefusal("synthetic-health-marker")

        view = View()
        writer = SimpleNamespace(store=SimpleNamespace(read=lambda: nullcontext(view)), run=lambda op: op(view))
        settings = worker.WorkerSettings(Path("synthetic.db"), 0, batch_size=1, max_discovery_window=4,
                                         refusal_retry_seconds=1)
        loop = worker.Worker(writer, settings, clock=fake_time.clock, monotonic=fake_time.monotonic,
                             sleep=fake_time.sleep)
        loop._index = lambda item: fail("index", item.position)
        loop._reconcile = lambda record_id: fail("selection", record_id)
        with self.assertLogs(worker.LOG, level="WARNING") as logs:
            for _ in range(30):
                before = {stage: len(values) for stage, values in attempts.items()}
                loop.cycle(discover=True)
                for stage in reads:
                    self.assertLessEqual(len(loop._refusers[stage]), 3)
                    self.assertLessEqual(len(attempts[stage]) - before[stage], 1)
                fake_time.elapsed += 10
                loop.cycle(discover=False)
        for stage in reads:
            self.assertEqual(len(reads[stage]), 30)
            self.assertEqual(max(reads[stage]), 4)
            self.assertEqual(set(attempts[stage]), {1, 2, 3, 4})
            self.assertTrue(all(attempts[stage].count(number) > 1 for number in (1, 2, 3)))
        self.assertEqual([line for line in logs.output if "window saturated" in line], [
            f"WARNING:activity_tracker_portal.worker:worker {stage} discovery window saturated"
            for stage in ("index", "reconcile", "enqueue")])
        self.assertNotIn("synthetic-health-marker", repr(logs.records))
        self.assertTrue(all(record.exc_info is None for record in logs.records))
        self.assertEqual(fake_time.waits, [])

    def test_refuser_retry_uses_injected_clock_and_success_clears_memory(self):
        fake_time = FakeTime()
        settings = worker.WorkerSettings(Path("synthetic.db"), 0, batch_size=1, refusal_retry_seconds=10)
        view = SimpleNamespace(items_awaiting_index=lambda limit: (SimpleNamespace(position=1),))
        writer = SimpleNamespace(store=SimpleNamespace(read=lambda: nullcontext(view)))
        loop = worker.Worker(writer, settings, clock=fake_time.clock, monotonic=fake_time.monotonic)
        attempts = []
        def operation(item):
            attempts.append(fake_time.elapsed)
            if len(attempts) == 1:
                raise store_module.StoreRefusal("source-key-invalid")
        def turn():
            items = loop._discover("index", lambda view, limit: view.items_awaiting_index(limit),
                                   lambda item: item.position)
            loop._batch("index", items, lambda item: item.position, operation, discovery_stage="index")
        with self.assertLogs(worker.LOG, level="WARNING"):
            turn()
        fake_time.elapsed = 9
        turn()
        self.assertEqual(attempts, [0])
        fake_time.elapsed = 10
        turn()
        self.assertEqual(attempts, [0, 10])
        self.assertEqual(loop._refusers["index"], {})

    def test_worker_calls_only_published_discovery_parameters(self):
        expected = {"items_awaiting_index": 1, "records_awaiting_selection": 1,
                    "selections_awaiting_processing": 2}
        calls = [node for node in ast.walk(ast.parse(Path(worker.__file__).read_text()))
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr in expected]
        self.assertEqual({node.func.attr for node in calls}, set(expected))
        for call in calls:
            self.assertEqual(len(call.args), expected[call.func.attr])
            self.assertEqual(call.keywords, [])


class WorkerCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "portal.db"
        self.settings = worker.WorkerSettings(self.path, 0)
        self.writer = ingress.PortalWriter(self.path, reserve_bytes=0, role="worker")
        self.addCleanup(self.writer.close)
        self.sequence = 0
        self.time = FakeTime()
        def prepare(store):
            digest = hashlib.sha256(b"synthetic-offer").hexdigest()
            store.record_enrollment_offer(digest, NOW + timedelta(hours=1), NOW)
            store.enroll_installation(digest, INSTALLATION, hashlib.sha256(b"synthetic-fingerprint").hexdigest(), NOW)
            store.activate_installation(INSTALLATION, NOW)
            store.honor_generation(1, ingress.ingest_protocol.encode_message({
                "wire_version": 1, "kind": "capability-advertisement", "generation": 1,
                "wire_versions": [1], "projection_schemas": [{"record_type": "steps", "versions": [1]}],
                "max_record_count": 100, "max_message_bytes": 100000,
            }), NOW)
        self.writer.run(prepare)

    def loop(self, **kwargs):
        return worker.Worker(self.writer, kwargs.pop("settings", self.settings),
                             clock=self.time.clock, monotonic=self.time.monotonic,
                             sleep=self.time.sleep, **kwargs)

    def archive(self, *items, record_type="steps", change=False):
        self.sequence += 1
        sequence = self.sequence
        envelope = f"synthetic-envelope-{sequence}".encode()
        header = MessageHeader(INSTALLATION, sequence, hashlib.sha256(envelope).hexdigest(), 1,
                               NOW, envelope, b"synthetic-receipt")
        batch = IngestBatchRecord(record_type, 1, "change-cycle" if change else "initial-snapshot",
                                  CYCLE, sequence, 1, "0" * 64, items)
        self.writer.run(lambda store: store.archive_ingest_batch(header, batch))
        return ItemPosition(INSTALLATION, sequence, 0)

    def projection(self, number=10):
        vector = next(vector for vector in VECTORS if vector["record_type"] == "steps")
        value = json.loads(vector["bytes"])
        value["metadata"]["id"] = f"synthetic-identity-marker-{number}"
        return json.dumps(value).encode()

    def source(self, projection):
        envelope = worker.decode_envelope(projection, "steps", 1)
        key = SourceKey(envelope.source_system_kind, envelope.origin, "steps", envelope.source_identity)
        with self.writer.store.read() as view:
            return view.source_record(view.source_record_id(key))


class LoopTests(WorkerCase):
    def test_real_positive_vectors_are_indexed_and_reconciled(self):
        for vector in VECTORS:
            self.archive(BatchItem(projection=vector["bytes"].encode()), record_type=vector["record_type"])
        loop = self.loop(settings=replace(self.settings, batch_size=1000))
        loop.run(max_cycles=1)
        with self.writer.store.read() as view:
            self.assertEqual(view.items_awaiting_index(1000), ())
            self.assertEqual(view.records_awaiting_selection(1000), ())
            counts = view.index_counts()
            self.assertEqual(counts.evidence_items, len(VECTORS))
            self.assertGreater(counts.source_records, 20)

    def test_non_json_quarantine_does_not_block_neighbor(self):
        position = self.archive(BatchItem(projection=b"synthetic-health-marker NOT JSON"),
                                BatchItem(projection=self.projection()))
        self.loop().run(max_cycles=1)
        with self.writer.store.read() as view:
            quarantine = view.item_quarantine(position)
            self.assertEqual((quarantine.reason, quarantine.pointer), ("projection-not-json", ""))
            self.assertEqual(view.items_awaiting_index(100), ())
        self.assertEqual(self.source(self.projection()).selections[-1].outcome, "projection")

    def test_retraction_uses_store_semantics(self):
        projection = self.projection()
        self.archive(BatchItem(projection=projection))
        self.loop().run(max_cycles=1)
        identity = worker.decode_envelope(projection, "steps", 1).source_identity
        self.archive(BatchItem(retracted_identity=identity), change=True)
        self.loop().run(max_cycles=1)
        self.assertEqual(self.source(projection).selections[-1].outcome, "retraction")

    def test_enqueue_and_handler_receive_real_lease_outside_writer(self):
        projection = self.projection()
        self.archive(BatchItem(projection=projection))
        leases = []
        thread = threading.get_ident()
        def handler(lease):
            self.assertEqual(threading.get_ident(), thread)
            # A separate writer can mutate while the handler is running.
            ingress_writer.run(lambda store: store.honor_generation(2, b"synthetic-generation-2", NOW))
            leases.append(lease)
        ingress_writer = ingress.PortalWriter(self.path, reserve_bytes=0)
        self.addCleanup(ingress_writer.close)
        settings = replace(self.settings, processor_versions={"steps": PV})
        self.loop(settings=settings, handler=handler, worker_id="synthetic-worker").run(max_cycles=1)
        self.assertEqual(len(leases), 1)
        record = self.source(projection)
        lease = leases[0]
        self.assertEqual((lease.record_id, lease.selection_id, lease.owner, lease.processor_version),
                         (record.record_id, record.selections[-1].selection_id, "synthetic-worker", PV))
        with self.writer.store.read() as view:
            job = view.processing_job(lease.job_id)
            self.assertEqual(job.state, "leased")
            self.assertEqual(view.processing_outcomes(record.record_id), ())

    def test_default_skips_claims_and_empty_map_does_not_enqueue(self):
        self.archive(BatchItem(projection=self.projection()))
        with patch.object(store_module.OperationalStore, "claim_processing_job", side_effect=AssertionError("claim")):
            self.loop(settings=replace(self.settings, processor_versions={})).run(max_cycles=4)
        with self.writer.store.read() as view:
            self.assertEqual(view.processing_counts(NOW).jobs_ready, 0)
        self.assertEqual(self.time.waits, [1.0, 2.0, 4.0])

    def test_discovery_is_periodic_while_claim_path_stays_hot(self):
        for number in range(20):
            self.archive(BatchItem(projection=self.projection(number)))
        settings = replace(self.settings, processor_versions={"steps": PV}, claim_batch_size=1, discovery_interval=5)
        def handler(lease):
            self.time.elapsed += 1
        with ExitStack() as stack:
            spies = {}
            for name in ("items_awaiting_index", "records_awaiting_selection", "selections_awaiting_processing"):
                original = getattr(store_module.StoreView, name)
                spies[name] = stack.enter_context(patch.object(store_module.StoreView, name, autospec=True, side_effect=original))
            counts = stack.enter_context(patch.object(store_module.StoreView, "processing_counts", side_effect=AssertionError("counts")))
            claims = stack.enter_context(patch.object(store_module.OperationalStore, "claim_processing_job", autospec=True,
                                                     side_effect=store_module.OperationalStore.claim_processing_job))
            self.loop(settings=settings, handler=handler).run(max_cycles=12)
            self.assertEqual([spy.call_count for spy in spies.values()], [3, 3, 3])
            self.assertEqual(claims.call_count, 12)
            counts.assert_not_called()
        self.assertEqual(self.time.waits, [])

    def test_idle_backoff_and_discovery_cadence(self):
        with patch.object(store_module.StoreView, "items_awaiting_index", autospec=True,
                          side_effect=store_module.StoreView.items_awaiting_index) as discovery:
            self.loop(settings=replace(self.settings, discovery_interval=10, max_idle_seconds=4)).run(max_cycles=8)
        self.assertEqual(self.time.waits, [1, 2, 4, 3, 4, 4, 2])
        self.assertEqual(discovery.call_count, 3)

    def test_evidence_moved_retries_next_cycle_without_discovery(self):
        self.archive(BatchItem(projection=self.projection()))
        original = worker.reconcile
        calls = []
        def reconcile(store, record_id, at):
            calls.append(record_id)
            if len(calls) == 1:
                raise ReconcileRetryable("evidence-moved")
            return original(store, record_id, at)
        with patch.object(worker, "reconcile", side_effect=reconcile):
            with self.assertLogs(worker.LOG, level="WARNING"):
                self.loop().run(max_cycles=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.source(self.projection()).selections[-1].outcome, "projection")

    def test_retryables_back_off_and_refusals_do_not_drop_neighbors(self):
        self.archive(BatchItem(projection=self.projection(10)), BatchItem(projection=self.projection(11)))
        original = store_module.OperationalStore.index_items
        for error in (store_module.StoreRetryable("storage-reserve-exhausted"),
                      store_module.StoreRetryable("store-busy"), store_module.StoreRefusal("source-key-invalid")):
            calls = []
            def index(store, entries, at):
                calls.append(entries)
                if len(calls) == 1:
                    raise error
                return original(store, entries, at)
            with patch.object(store_module.OperationalStore, "index_items", autospec=True, side_effect=index):
                with self.assertLogs(worker.LOG, level="WARNING"):
                    self.loop(settings=replace(self.settings, discovery_interval=1)).run(max_cycles=2)
            self.assertTrue(self.source(self.projection(11)).selections)
            # Re-archive fresh evidence for the next injected failure.
            self.archive(BatchItem(projection=self.projection(10)), BatchItem(projection=self.projection(11)))
        self.assertTrue(all(wait > 0 for wait in self.time.waits))

    def test_refusing_head_cannot_wedge_a_one_item_batch(self):
        bad = self.archive(BatchItem(projection=self.projection(10)))
        self.archive(BatchItem(projection=self.projection(11)))
        original = store_module.OperationalStore.index_items
        def index(store, entries, at):
            if entries[0].position == bad:
                raise store_module.StoreRefusal("source-key-invalid")
            return original(store, entries, at)
        with patch.object(store_module.OperationalStore, "index_items", autospec=True, side_effect=index):
            with self.assertLogs(worker.LOG, level="WARNING"):
                self.loop(settings=replace(self.settings, batch_size=1, discovery_interval=1)).run(max_cycles=3)
        self.assertTrue(self.source(self.projection(11)).selections)

    def test_two_refusers_cannot_starve_later_items_and_are_retried(self):
        bad = [self.archive(BatchItem(projection=self.projection(number))) for number in (10, 11)]
        self.archive(BatchItem(projection=self.projection(12)))
        original = store_module.OperationalStore.index_items
        attempts = []
        def index(store, entries, at):
            position = entries[0].position
            attempts.append(position)
            if position in bad:
                raise store_module.StoreRefusal("source-key-invalid")
            return original(store, entries, at)
        with patch.object(store_module.OperationalStore, "index_items", autospec=True, side_effect=index):
            with self.assertLogs(worker.LOG, level="WARNING"):
                self.loop(settings=replace(self.settings, batch_size=1, discovery_interval=1,
                                                refusal_retry_seconds=0.5)).run(max_cycles=8)
        record = self.source(self.projection(12))
        self.assertIsNotNone(record, "the valid third item must be discovered past both refusers")
        self.assertTrue(record.selections)
        self.assertTrue(all(attempts.count(position) >= 2 for position in bad), "refusers must be retried")
        self.assertLessEqual(len(attempts), 8, "each cycle must attempt at most batch_size items")

    def test_long_refusing_prefix_keeps_discovery_and_attempts_bounded(self):
        # One archive exercises a refusing prefix spanning items and messages.
        bad = self.archive(*(BatchItem(projection=self.projection(number)) for number in range(10, 19)))
        self.archive(BatchItem(projection=self.projection(19)))
        original_index = store_module.OperationalStore.index_items
        original_read = store_module.StoreView.items_awaiting_index
        attempts, reads = [], []
        def index(store, entries, at):
            position = entries[0].position
            attempts.append(position)
            if position.sequence == bad.sequence:
                raise store_module.StoreRefusal("source-key-invalid")
            return original_index(store, entries, at)
        def read(view, limit):
            items = original_read(view, limit)
            reads.append((limit, len(items)))
            return items
        settings = replace(self.settings, batch_size=2, discovery_interval=1, refusal_retry_seconds=0.5)
        with patch.object(store_module.OperationalStore, "index_items", autospec=True, side_effect=index):
            with patch.object(store_module.StoreView, "items_awaiting_index", autospec=True, side_effect=read):
                with self.assertLogs(worker.LOG, level="WARNING"):
                    self.loop(settings=settings).run(max_cycles=12)
        self.assertTrue(self.source(self.projection(19)).selections)
        self.assertTrue(all(count <= limit <= 11 for limit, count in reads))
        self.assertLessEqual(len(reads), 12, "one read per discovery turn")
        self.assertLessEqual(len(attempts), 24)
        self.assertGreaterEqual(attempts.count(bad), 2)

    def test_two_reconcile_refusers_cannot_starve_later_records(self):
        for number in (10, 11, 12):
            self.archive(BatchItem(projection=self.projection(number)))
        original = worker.reconcile
        attempts = []
        def reconcile(store, record_id, at):
            attempts.append(record_id)
            if record_id in (1, 2):
                raise ReconcileRefusal("source-record-unknown")
            return original(store, record_id, at)
        with patch.object(worker, "reconcile", side_effect=reconcile):
            with self.assertLogs(worker.LOG, level="WARNING"):
                self.loop(settings=replace(self.settings, batch_size=1, discovery_interval=1,
                                                refusal_retry_seconds=0.5)).run(max_cycles=8)
        self.assertTrue(self.source(self.projection(12)).selections)
        self.assertTrue(all(attempts.count(record_id) >= 2 for record_id in (1, 2)))
        self.assertLessEqual(len(attempts), 8)

    def test_two_enqueue_refusers_cannot_starve_later_selections(self):
        for number in (10, 11, 12):
            self.archive(BatchItem(projection=self.projection(number)))
        original = store_module.OperationalStore.enqueue_processing
        attempts = []
        def enqueue(store, record_id, selection_id, version, max_attempts, at):
            attempts.append(record_id)
            if record_id in (1, 2):
                raise store_module.StoreRefusal("processing-request-invalid")
            return original(store, record_id, selection_id, version, max_attempts, at)
        settings = replace(self.settings, processor_versions={"steps": PV}, batch_size=1, discovery_interval=1,
                           refusal_retry_seconds=0.5)
        leases = []
        with patch.object(store_module.OperationalStore, "enqueue_processing", autospec=True, side_effect=enqueue):
            with self.assertLogs(worker.LOG, level="WARNING"):
                self.loop(settings=settings, handler=leases.append).run(max_cycles=8)
        self.assertEqual([lease.record_id for lease in leases], [self.source(self.projection(12)).record_id])
        self.assertTrue(all(attempts.count(record_id) >= 2 for record_id in (1, 2)))
        self.assertLessEqual(len(attempts), 8)

    def test_retryable_cycle_past_discovery_deadline_still_sleeps(self):
        self.archive(BatchItem(projection=self.projection()))
        def index(store, entries, at):
            self.time.elapsed += 2
            raise store_module.StoreRetryable("store-busy")
        settings = replace(self.settings, discovery_interval=1, idle_seconds=0.5)
        with patch.object(store_module.OperationalStore, "index_items", autospec=True, side_effect=index):
            with self.assertLogs(worker.LOG, level="WARNING"):
                self.loop(settings=settings).run(max_cycles=2)
        self.assertEqual(self.time.waits, [0.5])

    def test_real_store_reserve_retryable_recovers_without_classification(self):
        self.archive(BatchItem(projection=self.projection()))
        with patch.object(self.writer.store, "_reserve_bytes", 1), patch.object(self.writer.store, "_free_bytes", return_value=0):
            with self.assertLogs(worker.LOG, level="WARNING") as logs:
                self.loop().run(max_cycles=2)
            self.assertIn("storage-reserve-exhausted", str(logs.output))
        self.assertEqual(self.time.waits, [1])
        self.loop().run(max_cycles=1)
        self.assertTrue(self.source(self.projection()).selections)

    def test_logging_does_not_include_exception_values_or_projection(self):
        self.archive(BatchItem(projection=self.projection()))
        for error in (RuntimeError("synthetic-health-marker"), store_module.StoreRefusal("synthetic-identity-marker-10")):
            with patch.object(store_module.OperationalStore, "index_items", side_effect=error):
                with self.assertLogs(worker.LOG, level="WARNING") as logs:
                    self.loop().run(max_cycles=1)
            output = repr(logs.records)
            self.assertNotIn("synthetic-health-marker", output)
            self.assertNotIn("synthetic-identity-marker", output)
            self.assertTrue(all(record.exc_info is None for record in logs.records))

    def test_handler_error_leaves_lease_for_governed_recovery(self):
        self.archive(BatchItem(projection=self.projection()))
        with self.assertLogs(worker.LOG, level="ERROR") as logs:
            self.loop(settings=replace(self.settings, processor_versions={"steps": PV}),
                      handler=lambda lease: (_ for _ in ()).throw(RuntimeError("synthetic-health-marker"))).run(max_cycles=1)
        with self.writer.store.read() as view:
            self.assertEqual(view.processing_counts(NOW).jobs_leased, 1)
        self.assertNotIn("synthetic-health-marker", str(logs.output))

    def test_stop_flag_stops_between_items_and_during_idle_wait(self):
        stop = threading.Event()
        stop.set()
        with patch.object(self.writer.store, "read", side_effect=AssertionError("read after stop")):
            self.loop(stop=stop).run(max_cycles=5)
        stop.clear()
        loop = self.loop(stop=stop)
        loop.sleep = lambda seconds: stop.set()
        with patch.object(loop, "cycle", wraps=loop.cycle) as cycles:
            loop.run()
        self.assertEqual(cycles.call_count, 1)

    def test_handler_stop_does_not_claim_the_rest_of_the_batch(self):
        self.archive(BatchItem(projection=self.projection(10)), BatchItem(projection=self.projection(11)))
        stop = threading.Event()
        leases = []
        def handler(lease):
            leases.append(lease)
            stop.set()
        self.loop(settings=replace(self.settings, processor_versions={"steps": PV}),
                  handler=handler, stop=stop).run()
        self.assertEqual(len(leases), 1)
        with self.writer.store.read() as view:
            counts = view.processing_counts(NOW)
            self.assertEqual((counts.jobs_leased, counts.jobs_ready), (1, 1))


class GovernedHandlerScheduleTests(WorkerCase):
    def governed_loop(self, **kwargs):
        return self.loop(handler=execution.make_job_handler(self.writer, clock=self.time.clock), **kwargs)

    def jobs(self, count):
        with self.writer.store.read() as view:
            return tuple(view.processing_job(number) for number in range(1, count + 1))

    def test_several_processing_failures_do_not_starve_a_valid_job(self):
        for number in range(4):
            self.archive(BatchItem(projection=self.projection(number)))
        original = execution.interpret
        calls = []
        def interpret(value, *args):
            calls.append(value.record_id)
            if len(calls) <= 3:
                raise execution.InterpretFailure("processor-invariant-violated")
            return original(value, *args)
        with patch.object(execution, "interpret", side_effect=interpret):
            self.governed_loop(settings=replace(self.settings, claim_batch_size=2)).run(max_cycles=2)
        self.assertEqual(calls, [1, 2, 3, 4])
        self.assertEqual([job.state for job in self.jobs(4)], ["failed"] * 3 + ["completed"])
        self.assertTrue(all(job.failure == "processor-invariant-violated" for job in self.jobs(3)))
        self.assertEqual(self.time.waits, [])

    def test_transient_processing_failures_later_succeed_on_governed_schedule(self):
        for number in range(3):
            self.archive(BatchItem(projection=self.projection(number)))
        original = execution.interpret
        calls = {number: [] for number in range(1, 4)}
        def interpret(value, *args):
            calls[value.record_id].append(self.time.elapsed)
            if len(calls[value.record_id]) == 1:
                raise RuntimeError("synthetic-health-marker")
            return original(value, *args)
        with patch.object(execution, "interpret", side_effect=interpret):
            self.governed_loop().run(max_cycles=15)
        jobs = self.jobs(3)
        self.assertTrue(all(job.state == "completed" and job.attempts == 2 for job in jobs))
        for job in jobs:
            self.assertEqual([attempt.result for attempt in job.history], ["transient-failure", "completed"])
            self.assertGreaterEqual(calls[job.record_id][1], 60)
        self.assertTrue(all(wait > 0 for wait in self.time.waits))

    def test_handler_store_busy_reaches_worker_backoff_and_recovers(self):
        self.archive(BatchItem(projection=self.projection()))
        original = store_module.OperationalStore.complete_processing_job
        calls = []
        def complete(store, **kwargs):
            calls.append(self.time.elapsed)
            if len(calls) == 1:
                raise store_module.StoreRetryable("store-busy")
            return original(store, **kwargs)
        settings = replace(self.settings, lease_seconds=2, claim_batch_size=1)
        with patch.object(store_module.OperationalStore, "complete_processing_job", autospec=True, side_effect=complete):
            with self.assertLogs(worker.LOG, level="WARNING") as logs:
                self.governed_loop(settings=settings).run(max_cycles=4)
        self.assertEqual(logs.output, ["WARNING:activity_tracker_portal.worker:worker handler retryable: store-busy"])
        self.assertEqual(self.time.waits, [1, 2])
        job, = self.jobs(1)
        self.assertEqual((job.state, job.attempts), ("completed", 2))
        self.assertEqual([attempt.result for attempt in job.history], ["lease-expired", "completed"])
        with self.writer.store.read() as view:
            self.assertEqual(len(view.processing_outcomes(job.record_id)), 1)

    def test_handler_overrun_keeps_positive_backoff_and_runs_due_discovery(self):
        self.archive(BatchItem(projection=self.projection()))
        original = store_module.OperationalStore.complete_processing_job
        calls = []
        def complete(store, **kwargs):
            calls.append(self.time.elapsed)
            if len(calls) == 1:
                self.time.elapsed += 3
                raise store_module.StoreRetryable("store-busy")
            return original(store, **kwargs)
        settings = replace(self.settings, lease_seconds=2, claim_batch_size=1, discovery_interval=1,
                           idle_seconds=0.5)
        with patch.object(store_module.OperationalStore, "complete_processing_job", autospec=True, side_effect=complete):
            with patch.object(store_module.StoreView, "items_awaiting_index", autospec=True,
                              side_effect=store_module.StoreView.items_awaiting_index) as discovery:
                with self.assertLogs(worker.LOG, level="WARNING"):
                    self.governed_loop(settings=settings).run(max_cycles=2)
        self.assertEqual(self.time.waits, [0.5])
        self.assertEqual(discovery.call_count, 2)
        self.assertEqual(calls, [0, 3.5])
        job, = self.jobs(1)
        self.assertEqual((job.state, job.attempts), ("completed", 2))


class ProcessTests(WorkerCase):
    def test_singleton_roles_coexist_and_release_on_close(self):
        ingress_writer = ingress.PortalWriter(self.path, reserve_bytes=0)
        self.addCleanup(ingress_writer.close)
        with self.assertRaises(ingress.PortalWriterBusy):
            ingress.PortalWriter(self.path, reserve_bytes=0, role="worker")
        with self.assertRaises(ingress.PortalWriterBusy):
            ingress.PortalWriter(self.path, reserve_bytes=0)
        self.writer.close()
        successor = ingress.PortalWriter(self.path, reserve_bytes=0, role="worker")
        successor.close()

    def test_lock_is_per_store_call_and_timeout_is_retryable(self):
        other = ingress.PortalWriter(self.path, reserve_bytes=0)
        self.addCleanup(other.close)
        # No lock is held across non-store computation within run().
        self.writer.run(lambda store: other.run(lambda peer: peer.honor_generation(2, b"synthetic", NOW)))
        lock = Path(f"{self.path}.store-write-lock").open("a+b")
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original = ingress._store_write_lock
        with patch.object(ingress, "_store_write_lock", side_effect=lambda file: original(file, timeout=0.02)):
            started = time.monotonic()
            with self.assertRaises(store_module.StoreRetryable) as error:
                self.writer.run(lambda store: store.honor_generation(3, b"synthetic", NOW))
            self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(error.exception.failure, "store-busy")
        fcntl.flock(lock, fcntl.LOCK_UN)
        self.writer.run(lambda store: store.honor_generation(3, b"synthetic", NOW))

    def test_ingress_busy_lock_returns_existing_retryable_response(self):
        settings = ingress.PortalSettings(self.path, 0, "http://portal.invalid:8443", "127.0.0.1", 8443,
                                         ingress.sender_installation.Capabilities((1,), (("steps", (1,)),), 100, 100000))
        app = ingress.create_app(settings, clock=lambda: NOW)
        with TestClient(app) as client:
            lock = Path(f"{self.path}.store-write-lock").open("a+b")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Invalid input still needs a durable Delivery Block, which contends.
                original = ingress._store_write_lock
                with patch.object(ingress, "_store_write_lock", side_effect=lambda file: original(file, timeout=0.02)):
                    response = client.post("/v1/outbox-messages", content=b"synthetic-invalid")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.content, ingress.RETRYABLE_BODY)
            finally:
                lock.close()

    def config_file(self, **options):
        path = self.path.with_suffix(".json")
        path.write_text(json.dumps({"store_path": str(self.path), "reserve_bytes": 0, **options}))
        return path

    def test_real_entry_point_help_and_default_bounded_synthetic_loop(self):
        help_result = subprocess.run(["uv", "run", "--frozen", "portal-worker", "--help"],
                                     capture_output=True, text=True, timeout=20)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--config", help_result.stdout)
        self.archive(BatchItem(projection=self.projection()))
        self.writer.close()
        result = subprocess.run(["uv", "run", "--frozen", "portal-worker", "--config", str(self.config_file()),
                                 "--max-cycles", "1"], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        with store_module.open_store(self.path, reserve_bytes=0) as store, store.read() as view:
            self.assertEqual(view.items_awaiting_index(100), ())
            self.assertEqual(view.records_awaiting_selection(100), ())
            counts = view.processing_counts(datetime.now(timezone.utc))
            self.assertEqual((counts.jobs_ready, counts.jobs_leased), (0, 0))
            outcome, = view.processing_outcomes(1)
            self.assertEqual((outcome.processor_version, outcome.kind), (PV, "policy-pending"))

    def test_real_entry_point_processes_seeded_jobs_while_ingress_writer_runs(self):
        # Explicit offsets produce a revision without a recorded fallback policy.
        projection = json.loads(self.projection())
        projection["record"].update(start_zone_offset="+00:00", end_zone_offset="+00:00")
        self.archive(BatchItem(projection=json.dumps(projection).encode()))
        distance = next(vector for vector in VECTORS if vector["record_type"] == "distance")
        self.archive(BatchItem(projection=distance["bytes"].encode()), record_type="distance")
        # Seed queued jobs through the public constructor seam without claiming.
        self.loop().run(max_cycles=1)
        with self.writer.store.read() as view:
            jobs = tuple(view.processing_job(number) for number in (1, 2))
            self.assertTrue(all(job.state == "queued" for job in jobs))
        self.writer.close()
        with ExitStack() as stack:
            ingress_writer = ingress.PortalWriter(self.path, reserve_bytes=0)
            stack.callback(ingress_writer.close)
            result = subprocess.run(["uv", "run", "--frozen", "portal-worker", "--config",
                                     str(self.config_file(processor_versions={}, claim_batch_size=1)),
                                     "--max-cycles", "2"], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((result.stdout, result.stderr), ("", ""))
            with ingress_writer.store.read() as view:
                for seeded, kind in zip(jobs, ("revision", "unmodeled"), strict=True):
                    job = view.processing_job(seeded.job_id)
                    self.assertEqual((job.state, job.attempts), ("completed", 1))
                    attempt, = job.history
                    outcome, = view.processing_outcomes(job.record_id)
                    self.assertEqual((attempt.fence, attempt.result), (job.fence, "completed"))
                    self.assertEqual((outcome.job_id, outcome.fence, outcome.selection_id, outcome.kind),
                                     (job.job_id, job.fence, job.selection_id, kind))
                    self.assertEqual(outcome.processor_version, seeded.processor_version)
                    if kind == "revision":
                        self.assertIsNotNone(outcome.revision)
                    else:
                        self.assertEqual(outcome.reason, "record-type-not-modeled")
            # The ingress writer stays usable after the child exits.
            ingress_writer.run(lambda store: store.honor_generation(2, b"synthetic-generation-2", NOW))
            replacement = ingress.PortalWriter(self.path, reserve_bytes=0, role="worker")
            replacement.close()

    def test_command_builds_handler_with_its_writer_and_shared_clock(self):
        with patch.object(worker, "PortalWriter") as writer, patch.object(worker, "Worker") as loop:
            with patch.object(execution, "make_job_handler") as factory:
                self.assertEqual(worker.main(["--config", str(self.config_file()), "--max-cycles", "1"],
                                             clock=self.time.clock), 0)
        factory.assert_called_once_with(writer.return_value, clock=self.time.clock)
        args, options = loop.call_args
        self.assertIs(args[0], writer.return_value)
        self.assertEqual(args[1].processor_versions, execution.PROCESSOR_VERSIONS)
        self.assertIs(options["handler"], factory.return_value)
        self.assertEqual(options["clock"], self.time.clock)
        loop.return_value.run.assert_called_once_with(max_cycles=1)
        writer.return_value.close.assert_called_once_with()

    def test_removed_handler_configuration_is_rejected_before_store_open(self):
        with patch.object(worker, "PortalWriter") as writer, self.assertLogs(worker.LOG, level="ERROR") as logs:
            self.assertEqual(worker.main(["--config", str(self.config_file(job_handler="synthetic_health_marker:handle"))]), 1)
        writer.assert_not_called()
        self.assertEqual(logs.output, ["ERROR:activity_tracker_portal.worker:invalid worker configuration"])
        self.assertTrue(all(record.exc_info is None for record in logs.records))

    def test_discovery_map_defaults_and_explicit_replacement(self):
        self.assertEqual(self.settings.processor_versions, execution.PROCESSOR_VERSIONS)
        for mapping in ({}, {"steps": PV}):
            with self.subTest(mapping=mapping):
                settings = worker.WorkerSettings.from_file(self.config_file(processor_versions=mapping))
                self.assertEqual(settings.processor_versions, mapping)
                with self.assertRaises(TypeError):
                    settings.processor_versions["steps"] = PV

    def test_entry_point_sigterm_exits_and_releases_singleton(self):
        self.writer.close()
        process = subprocess.Popen([str(Path(sys.executable).parent / "portal-worker"), "--config", str(self.config_file())],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        # Wait until it owns its singleton lock, then stop an idle wait.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            lock = Path(f"{self.path}.worker-lock").open("a+b")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                break
            finally:
                lock.close()
            time.sleep(0.01)
        else:
            self.fail("worker did not start")
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        replacement = ingress.PortalWriter(self.path, reserve_bytes=0, role="worker")
        replacement.close()

    def test_configuration_validation_is_health_data_free(self):
        path = self.path.with_suffix(".json")
        for value in ({}, [], {"store_path": "synthetic-health-marker", "reserve_bytes": True},
                      {"store_path": "synthetic-health-marker", "reserve_bytes": 0, "idle_seconds": 0},
                      *({"store_path": "synthetic-health-marker", "reserve_bytes": 0, **option}
                        for option in ({"max_discovery_window": 0}, {"max_discovery_window": 10001},
                                       {"max_discovery_window": 99}, {"max_discovery_window": True},
                                       {"refusal_retry_seconds": 0}, {"refusal_retry_seconds": float("inf")})),
                      {"store_path": "synthetic-health-marker", "reserve_bytes": 0, "processor_versions": {"steps": "BAD"}},
                      *({"store_path": "synthetic-health-marker", "reserve_bytes": 0, "processor_versions": mapping}
                        for mapping in ({"steps": "interpret-source-projection@1"}, {"steps": "interpret-steps@2"},
                                        {"synthetic-type": PV}, None, [])),
                      *({"store_path": "synthetic-health-marker", "reserve_bytes": 0, "job_handler": value}
                        for value in (None, "synthetic_module:handle", 42, "", "module", "module:", "module:function:extra", "../module:handle"))):
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "^invalid worker configuration$"):
                worker.WorkerSettings.from_file(path)


if __name__ == "__main__":
    unittest.main()
