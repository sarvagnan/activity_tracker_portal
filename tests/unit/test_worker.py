"""Worker supervisor integration tests with real stores and synthetic projections."""

from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
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
from unittest.mock import patch

from fastapi.testclient import TestClient

from activity_tracker_portal import ingress, operational_store as store_module, worker
from activity_tracker_portal.operational_store import BatchItem, IngestBatchRecord, ItemPosition, MessageHeader, SourceKey
from activity_tracker_portal.reconcile_source_record import ReconcileRetryable

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
            self.loop().run(max_cycles=4)
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

    def config_file(self):
        path = self.path.with_suffix(".json")
        path.write_text(json.dumps({"store_path": str(self.path), "reserve_bytes": 0}))
        return path

    def test_real_entry_point_help_and_default_bounded_synthetic_loop(self):
        help_result = subprocess.run(["uv", "run", "--frozen", "portal-worker", "--help"],
                                     capture_output=True, text=True, timeout=20)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--config", help_result.stdout)
        self.archive(BatchItem(projection=self.projection()))
        self.writer.close()
        result = subprocess.run(["uv", "run", "--frozen", "portal-worker", "--config", str(self.config_file()),
                                 "--max-cycles", "2"], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        with store_module.open_store(self.path, reserve_bytes=0) as store:
            with store.read() as view:
                self.assertEqual(view.items_awaiting_index(100), ())
                self.assertEqual(view.records_awaiting_selection(100), ())

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
                      {"store_path": "synthetic-health-marker", "reserve_bytes": 0, "processor_versions": {"steps": "BAD"}}):
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "^invalid worker configuration$"):
                worker.WorkerSettings.from_file(path)


if __name__ == "__main__":
    unittest.main()
