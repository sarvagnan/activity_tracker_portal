"""Ingress adapter and writer serialization tests, using only synthetic data."""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from activity_tracker_portal import cli, ingress, operational_store, sender_installation
from activity_tracker_portal.ingest_protocol import encode_message, installation_fingerprint

NOW = datetime(2026, 10, 14, 9, tzinfo=timezone.utc)
URL = "http://portal.invalid:8443"
CAPABILITIES = sender_installation.Capabilities((1,), (("steps", (1,)),), 500, 1_000_000)
RETRYABLE = b'{"kind":"retryable-failure","reason":"temporarily-unavailable","wire_version":1}'


def identity(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012x}"


def confirmation(code: str, number: int) -> bytes:
    installation_id = identity(number)
    return encode_message({
        "wire_version": 1, "kind": "enrollment-confirmation", "enrollment_code": code,
        "sender_installation": installation_id, "fingerprint": installation_fingerprint(installation_id),
    })


def advertisement(generation: int = 1) -> bytes:
    return encode_message({
        "wire_version": 1, "kind": "capability-advertisement", "generation": generation,
        "wire_versions": [1], "projection_schemas": [{"record_type": "steps", "versions": [1]}],
        "max_record_count": 500, "max_message_bytes": 1_000_000,
    })


class Case(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "portal.db"
        self.config_path = Path(directory.name) / "portal.json"
        self.settings = ingress.PortalSettings(self.path, 0, URL, "127.0.0.1", 8443, CAPABILITIES)

    def config(self) -> dict:
        return {
            "store_path": str(self.path), "reserve_bytes": 12345, "portal_url": URL,
            "host": "127.0.0.1", "port": 8443,
            "capabilities": {
                "wire_versions": [1], "projection_schemas": [{"record_type": "steps", "versions": [1]}],
                "max_record_count": 500, "max_message_bytes": 1_000_000,
            },
        }

    def writer(self) -> ingress.PortalWriter:
        writer = ingress.PortalWriter(self.path, reserve_bytes=0)
        self.addCleanup(writer.close)
        return writer

    def app(self):
        return ingress.create_app(self.settings, clock=lambda: NOW, random_bytes=lambda size: bytes(size))

    def offer(self, writer: ingress.PortalWriter, number: int = 1):
        return writer.run(lambda store: sender_installation.offer_enrollment(
            store, URL, NOW, random_bytes=lambda size: bytes([number]) * size,
        ))


class SettingsTests(Case):
    def test_valid_config_is_frozen_and_has_explicit_policy(self) -> None:
        self.config_path.write_text(json.dumps(self.config()), encoding="utf-8")
        settings = ingress.PortalSettings.from_file(self.config_path)
        self.assertEqual(settings, replace(self.settings, reserve_bytes=12345))
        self.assertIsInstance(settings.capabilities.wire_versions, tuple)
        self.assertEqual(settings.capabilities.projection_schemas, (("steps", (1,)),))
        with self.assertRaises(FrozenInstanceError):
            settings.port = 80

    def test_invalid_config_errors_never_contain_values(self) -> None:
        variants = []
        missing_limit = self.config()
        del missing_limit["capabilities"]["max_message_bytes"]
        variants.append(missing_limit)
        for field, value in (
            ("portal_url", "https://SYNTHETIC-SECRET@portal.invalid"),
            ("reserve_bytes", -1), ("reserve_bytes", True),
            ("port", 65536), ("port", True), ("host", ""), ("store_path", ""),
        ):
            config = self.config()
            config[field] = value
            variants.append(config)
        for field, value in (
            ("max_record_count", 0), ("max_message_bytes", 1),
            ("wire_versions", [True]), ("projection_schemas", [{"record_type": "INVALID TYPE", "versions": [1]}]),
        ):
            config = self.config()
            config["capabilities"][field] = value
            variants.append(config)
        variants.extend([[], None, {"SYNTHETIC-SECRET": "unexpected"}])
        for config in variants:
            with self.subTest(config=config):
                self.config_path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaises(ValueError) as error:
                    ingress.PortalSettings.from_file(self.config_path)
                self.assertEqual(str(error.exception), "invalid Portal configuration")

    def test_unreadable_and_malformed_files_have_safe_errors(self) -> None:
        for content in (None, '{"SYNTHETIC-SECRET":', b"\xff"):
            with self.subTest(content=content):
                if isinstance(content, str):
                    self.config_path.write_text(content, encoding="utf-8")
                elif isinstance(content, bytes):
                    self.config_path.write_bytes(content)
                with self.assertRaisesRegex(ValueError, "^invalid Portal configuration$"):
                    ingress.PortalSettings.from_file(self.config_path)


class RouteTests(Case):
    def test_startup_honors_capabilities_and_restart_keeps_generation(self) -> None:
        for _ in range(2):
            app = self.app()
            with TestClient(app) as client:
                response = client.get("/v1/capabilities")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, advertisement())
                self.assertEqual(response.headers["content-type"], "application/json")
                with app.state.writer.store.read() as view:
                    self.assertEqual([item.generation for item in view.honored_generations()], [1])
        changed = ingress.create_app(replace(self.settings, capabilities=replace(CAPABILITIES, max_record_count=600)), clock=lambda: NOW)
        with TestClient(changed) as client:
            response = client.get("/v1/capabilities")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(response.content)["generation"], 2)

    def test_enrollment_pending_and_retry_return_exact_advertisement(self) -> None:
        app = self.app()
        with TestClient(app) as client:
            offer = self.offer(app.state.writer)
            data = confirmation(offer.code, 1)
            for _ in range(2):
                response = client.post("/v1/enrollment-confirmation", content=data)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, advertisement())
                self.assertEqual(response.headers["content-type"], "application/json")
            with app.state.writer.store.read() as view:
                self.assertEqual(view.installation(identity(1)).state, "pending")
                self.assertEqual(len(view.installations()), 1)

    def test_unknown_consumed_and_malformed_confirmations_are_empty_refusals(self) -> None:
        app = self.app()
        with TestClient(app) as client:
            offer = self.offer(app.state.writer)
            self.assertEqual(client.post("/v1/enrollment-confirmation", content=confirmation(offer.code, 1)).status_code, 200)
            for data in (
                confirmation("AAAAAAAAAAAAAAAA", 2), confirmation(offer.code, 2), b"not JSON",
                b" " + confirmation(offer.code, 1), advertisement(),
            ):
                with self.subTest(body=data):
                    response = client.post("/v1/enrollment-confirmation", content=data)
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.content, b"")
            with app.state.writer.store.read() as view:
                self.assertEqual(len(view.installations()), 1)

    def test_store_busy_returns_exact_retryable_bytes(self) -> None:
        # Keep contention short without changing the shell's production timeout.
        real_open = operational_store.open_store
        def short_timeout(path, *, reserve_bytes):
            return real_open(path, reserve_bytes=reserve_bytes, busy_timeout_ms=50)
        app = self.app()
        with patch.object(operational_store, "open_store", side_effect=short_timeout), TestClient(app) as client:
            offer = self.offer(app.state.writer)
            connection = sqlite3.connect(self.path, isolation_level=None)
            try:
                connection.execute("BEGIN IMMEDIATE")
                response = client.post("/v1/enrollment-confirmation", content=confirmation(offer.code, 1))
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.content, RETRYABLE)
                self.assertEqual(response.headers["content-type"], "application/json")
                with app.state.writer.store.read() as view:
                    self.assertEqual(view.installations(), ())
            finally:
                connection.rollback()
                connection.close()
            self.assertEqual(client.post("/v1/enrollment-confirmation", content=confirmation(offer.code, 1)).status_code, 200)

    def test_body_limit_precedes_any_writer_use_and_is_inclusive(self) -> None:
        app = self.app()
        with TestClient(app) as client:
            with patch.object(app.state.writer, "run", side_effect=AssertionError("writer used")):
                for data in (b"x" * 4097, iter([b"x" * 2048, b"x" * 2049])):
                    response = client.post("/v1/enrollment-confirmation", content=data)
                    self.assertEqual(response.status_code, 413)
                    self.assertEqual(response.content, b"")
            with patch.object(sender_installation, "answer_enrollment_confirmation", wraps=sender_installation.answer_enrollment_confirmation) as answer:
                response = client.post("/v1/enrollment-confirmation", content=b"x" * 4096)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(answer.call_args.args[1], b"x" * 4096)

    def test_capabilities_without_a_generation_return_canonical_retryable_bytes(self) -> None:
        app = self.app()
        # A fixture leaves the real store open but suppresses startup honoring.
        with patch.object(sender_installation, "honor_capabilities", return_value=None), TestClient(app) as client:
            response = client.get("/v1/capabilities")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.content, RETRYABLE)
            self.assertEqual(response.headers["content-type"], "application/json")

    def test_lifespan_releases_writer_on_shutdown_and_startup_failure(self) -> None:
        app = self.app()
        with TestClient(app):
            with self.assertRaises(ingress.PortalWriterBusy):
                self.writer()
        self.writer().close()
        with patch.object(sender_installation, "honor_capabilities", side_effect=RuntimeError("synthetic startup failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic startup failure"):
                with TestClient(self.app()):
                    self.fail("startup unexpectedly succeeded")
        self.writer().close()


class WriteTrace:
    """Observe real governed check/write boundaries while widening their race window."""

    def __init__(self) -> None:
        self.guard = threading.Lock()
        self.inside = 0
        self.maximum = 0
        self.events = []

    def event(self, kind, name) -> None:
        with self.guard:
            self.events.append((kind, name))

    def operation(self, name, call, store):
        with self.guard:
            self.inside += 1
            self.maximum = max(self.maximum, self.inside)
            self.events.append(("enter", name))
        try:
            return call(CheckedStore(store, self, name))
        finally:
            with self.guard:
                self.events.append(("exit", name))
                self.inside -= 1


class CheckedStore:
    """Proxy only the store boundary; governed logic and SQL remain real."""

    def __init__(self, store, trace, name) -> None:
        self.store, self.trace, self.name = store, trace, name

    @contextmanager
    def read(self):
        with self.store.read() as view:
            yield view
        self.trace.event("check", self.name)
        time.sleep(0.02)  # Between the governed snapshot check and its transaction.

    def __getattr__(self, method):
        def write(*args, **kwargs):
            self.trace.event("write", self.name)
            return getattr(self.store, method)(*args, **kwargs)
        return write


class WriterTests(Case):
    def assert_serialized(self, trace, count) -> None:
        self.assertEqual(trace.maximum, 1)
        self.assertEqual(trace.inside, 0)
        self.assertEqual(len(trace.events), count * 4)
        for index in range(0, len(trace.events), 4):
            group = trace.events[index:index + 4]
            self.assertEqual([kind for kind, _ in group], ["enter", "check", "write", "exit"])
            self.assertEqual(len({name for _, name in group}), 1, group)

    def test_concurrent_governed_generation_checks_and_writes_do_not_interleave(self) -> None:
        writer = self.writer()
        trace = WriteTrace()
        count = 8
        barrier = threading.Barrier(count)
        def run(number):
            barrier.wait(timeout=10)
            capabilities = replace(CAPABILITIES, max_record_count=500 + number)
            return writer.run(lambda store: trace.operation(
                str(number), lambda proxy: sender_installation.honor_capabilities(proxy, capabilities, NOW), store,
            ))
        with ThreadPoolExecutor(max_workers=count) as callers:
            generations = list(callers.map(run, range(count)))
        self.assertEqual(sorted(generations), list(range(1, count + 1)))
        self.assert_serialized(trace, count)
        with writer.store.read() as view:
            self.assertEqual(len(view.honored_generations()), count)

    def test_concurrent_http_confirmations_and_lifecycle_calls_share_one_writer(self) -> None:
        app = self.app()
        trace = WriteTrace()
        real_answer = sender_installation.answer_enrollment_confirmation
        count = 6
        with TestClient(app) as client:
            offers = [self.offer(app.state.writer, number) for number in range(1, count + 1)]
            barrier = threading.Barrier(count * 2)
            def traced_answer(store, data, now):
                name = "http-" + json.loads(data)["sender_installation"]
                return trace.operation(name, lambda proxy: real_answer(proxy, data, now), store)
            def request(number):
                barrier.wait(timeout=10)
                return client.post("/v1/enrollment-confirmation", content=confirmation(offers[number].code, number + 1))
            def intent(number):
                barrier.wait(timeout=10)
                return app.state.writer.run(lambda store: trace.operation(
                    f"offer-{number}", lambda proxy: sender_installation.offer_enrollment(
                        proxy, URL, NOW, random_bytes=lambda size: bytes([100 + number]) * size,
                    ), store,
                ))
            with patch.object(sender_installation, "answer_enrollment_confirmation", side_effect=traced_answer):
                with ThreadPoolExecutor(max_workers=count * 2) as callers:
                    requests = [callers.submit(request, number) for number in range(count)]
                    intents = [callers.submit(intent, number) for number in range(count)]
                    for future in requests:
                        response = future.result(timeout=15)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.content, advertisement())
                    for future in intents:
                        future.result(timeout=15)
            self.assert_serialized(trace, count * 2)
            with app.state.writer.store.read() as view:
                self.assertEqual(len(view.installations()), count)

    def test_writer_exclusion_until_close(self) -> None:
        first = self.writer()
        with patch.object(operational_store, "open_store", side_effect=AssertionError("opened before exclusion")):
            with self.assertRaises(ingress.PortalWriterBusy) as error:
                ingress.PortalWriter(self.path, reserve_bytes=0)
        self.assertEqual(error.exception.failure, "portal-writer-already-running")
        first.close()
        self.writer().close()
        first.close()  # Closing twice is harmless; never remove the lock inode.
        self.assertTrue(Path(f"{self.path}.writer-lock").exists())

    def test_failed_store_open_releases_process_lock(self) -> None:
        with patch.object(operational_store, "open_store", side_effect=RuntimeError("synthetic open failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic open failure"):
                ingress.PortalWriter(self.path, reserve_bytes=0)
        self.writer().close()

    def test_failed_operation_does_not_prevent_next_operation(self) -> None:
        writer = self.writer()
        def fail(store):
            raise RuntimeError("synthetic operation failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic operation failure"):
            writer.run(fail)
        self.assertEqual(writer.run(lambda store: sender_installation.honor_capabilities(store, CAPABILITIES, NOW)), 1)
        writer.close()
        with self.assertRaisesRegex(RuntimeError, "portal-writer-closed"):
            writer.run(lambda store: None)


class ServeCommandTests(Case):
    def test_serve_help_does_not_start_a_server(self) -> None:
        stdout = io.StringIO()
        with patch("uvicorn.run") as run, redirect_stdout(stdout), self.assertRaises(SystemExit) as result:
            cli.main(["serve", "--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("--config", stdout.getvalue())
        run.assert_not_called()

    def test_serve_uses_config_and_one_worker_without_reload(self) -> None:
        self.config_path.write_text(json.dumps(self.config()), encoding="utf-8")
        with patch("uvicorn.run") as run:
            self.assertEqual(cli.main(["serve", "--config", str(self.config_path)]), 0)
        self.assertEqual(run.call_args.kwargs, {"host": "127.0.0.1", "port": 8443, "workers": 1})
        self.assertEqual(run.call_args.args[0].state.settings.reserve_bytes, 12345)

    def test_invalid_config_is_reported_without_starting_a_server(self) -> None:
        stderr = io.StringIO()
        self.config_path.write_text('{"SYNTHETIC-SECRET":', encoding="utf-8")
        with patch("uvicorn.run") as run, redirect_stderr(stderr):
            self.assertEqual(cli.main(["serve", "--config", str(self.config_path)]), 1)
        self.assertEqual(stderr.getvalue(), "invalid Portal configuration\n")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
