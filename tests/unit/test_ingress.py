"""Ingress adapter and writer serialization tests, using only synthetic data."""

from __future__ import annotations

import asyncio
import gzip
import io
import random
import json
import logging
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from activity_tracker_portal import (
    accept_capability_acknowledgement, accept_ingest_batch, accept_sync_checkpoint,
    accept_sync_completion, cli, ingress, ingest_protocol, operational_store, sender_installation,
)
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

    def test_secret_query_and_bodies_are_not_logged(self) -> None:
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Capture()
        loggers = [logging.getLogger(name) for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error")]
        levels = [logger.level for logger in loggers]
        for logger in loggers:
            logger.addHandler(handler)
            logger.setLevel(logging.DEBUG)
        try:
            # HTTPX's client-side URL summaries are outside the server boundary.
            # Keep all application and Uvicorn loggers enabled for this capture.
            with patch.object(logging.getLogger("httpx"), "disabled", True), TestClient(self.app()) as client:
                offer = self.offer(client.app.state.writer)
                code = "ABCDEFGHIJKLMNOP"
                installation_id = identity(1)
                query = f"?enrollment_code={code}&sender_installation={installation_id}"
                body_marker = b"SYNTHETIC-PRIVATE-BODY"
                pending_body = confirmation(offer.code, 1)
                requests = (
                    ("POST", "/v1/enrollment-confirmation", body_marker, 409),
                    ("POST", "/v1/enrollment-confirmation", body_marker * 4097, 413),
                    ("POST", "/v1/enrollment-confirmation", pending_body, 200),
                    ("GET", "/v1/capabilities", b"", 200),
                    ("POST", "/v1/outbox-messages", body_marker, 409),
                    ("POST", "/v1/outbox-messages", body_marker * 50000, 413),
                    ("POST", "/v1/outbox-messages", outbox("capability-acknowledgement"), 503),
                )
                for method, path, body, status in requests:
                    with self.subTest(method=method, path=path, status=status):
                        records.clear()
                        response = client.request(method, path + query, content=body)
                        self.assertEqual(response.status_code, status)
                        for record in records:
                            # Inspect both formatted output and unformatted log arguments.
                            logged = record.getMessage() + repr(record.__dict__)
                            for secret in (
                                code, installation_id, offer.code, body_marker.decode(),
                                installation_fingerprint(installation_id), pending_body.decode(),
                            ):
                                self.assertNotIn(secret, logged)
                            self.assertNotIn(path, logged)
        finally:
            for logger, level in zip(loggers, levels):
                logger.removeHandler(handler)
                logger.setLevel(level)
            handler.close()

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


def outbox(kind: str, sequence: int = 1, *, cycle: int = 100, **changes) -> bytes:
    """Small synthetic messages; completions describe empty cycles by default."""
    message = {
        "wire_version": 1, "kind": kind, "sender_installation": identity(1),
        "sequence": sequence, "capability_generation": 1,
    }
    if kind == "ingest-batch":
        message.update({
            "record_type": "steps", "projection_schema_version": 1,
            "cycle_kind": "initial-snapshot", "cycle_id": identity(cycle),
            "source_page_index": 1, "fragment_index": 1,
            "previous_chain": ingest_protocol.chain_start(identity(cycle)),
            "items": [{"kind": "retraction", "source_identity": "synthetic-record"}],
        })
    elif kind in ("snapshot-completion", "change-completion"):
        message.update({
            "record_type": "steps", "cycle_id": identity(cycle),
            "page_count": 0, "fragment_count": 0, "record_count": 0,
            "final_chain": ingest_protocol.chain_start(identity(cycle)),
        })
        if kind == "snapshot-completion":
            message["snapshot_kind"] = "initial"
    elif kind == "sync-checkpoint":
        message.update({
            "run_sequence": 1, "trigger": "manual",
            "started_at": "2026-10-14T09:00:00.000Z", "ended_at": "2026-10-14T09:00:00.000Z",
            "elapsed_ms": 0, "background_scheduling": "available",
            "record_types": [{"record_type": "steps", "outcome": "no-change", "source_data_staged": False}],
            "outbox_messages_before_checkpoint": 0, "outbox_bytes_before_checkpoint": 0,
        })
    message.update(changes)
    return encode_message(message)


ACCEPT_UNITS = (
    (accept_ingest_batch, "accept_ingest_batch"),
    (accept_sync_completion, "accept_sync_completion"),
    (accept_sync_checkpoint, "accept_sync_checkpoint"),
    (accept_capability_acknowledgement, "accept_capability_acknowledgement"),
)


class OutboxRouteTests(Case):
    def enroll(self, writer, *, active=True) -> None:
        offer = self.offer(writer)
        answer = writer.run(lambda store: sender_installation.answer_enrollment_confirmation(
            store, confirmation(offer.code, 1), NOW,
        ))
        self.assertEqual(answer.outcome, "pending")
        if active:
            writer.run(lambda store: sender_installation.confirm_installation(
                store, identity(1), installation_fingerprint(identity(1)), NOW,
            ))

    def reference(self):
        writer = ingress.PortalWriter(self.path.with_name("reference.db"), reserve_bytes=0)
        self.addCleanup(writer.close)
        writer.run(lambda store: sender_installation.honor_capabilities(store, self.settings.capabilities, NOW))
        self.enroll(writer)
        return writer

    def assert_answer(self, response, expected) -> None:
        self.assertEqual(response.status_code, {"receipt": 200, "retryable": 503, "block": 409}[expected.outcome])
        self.assertEqual(response.content, expected.body)
        self.assertEqual(response.headers["content-type"], "application/json")

    async def post_asgi(self, app, receive, *, headers=()):
        sent = []
        async def send(message):
            sent.append(message)
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": "/v1/outbox-messages",
            "raw_path": b"/v1/outbox-messages", "query_string": b"", "root_path": "",
            "headers": list(headers), "server": ("testserver", 80), "client": ("testclient", 1),
        }
        await app(scope, receive, send)
        return sent

    def test_slow_preflight_read_keeps_event_loop_responsive(self) -> None:
        app = self.app()
        with TestClient(app):
            real_read = app.state.writer.store.read
            for stage in ("enter", "query"):
                with self.subTest(stage=stage):
                    delays = []
                    def pause():
                        started = time.monotonic()
                        time.sleep(0.2)
                        delays.append(time.monotonic() - started)

                    @contextmanager
                    def slow_read():
                        if stage == "enter":
                            pause()
                        with real_read() as view:
                            real_generations = view.honored_generations
                            def slow_generations():
                                pause()
                                return real_generations()
                            with ExitStack() as stack:
                                if stage == "query":
                                    stack.enter_context(patch.object(view, "honored_generations", side_effect=slow_generations))
                                yield view

                    async def receive():
                        self.fail("body read before transport rejection")

                    async def probe():
                        async def heartbeat():
                            started = time.monotonic()
                            await asyncio.sleep(0.02)
                            return time.monotonic() - started
                        beat = asyncio.create_task(heartbeat())
                        # Start the timer before the request reaches preflight.
                        await asyncio.sleep(0)
                        sent = await self.post_asgi(app, receive, headers=[(b"content-encoding", b"br")])
                        return sent, await beat

                    with patch.object(app.state.writer.store, "read", side_effect=slow_read), patch.object(
                        app.state.writer, "run", side_effect=AssertionError("writer used"),
                    ):
                        sent, gap = asyncio.run(probe())
                    self.assertEqual(sent[0]["status"], 415)
                    self.assertEqual(len(delays), 1)
                    self.assertGreaterEqual(delays[0], 0.2)
                    # Allow 7.5 times the heartbeat interval, still below the
                    # injected blocking delay. No request-duration assertion.
                    self.assertLess(gap, 0.15, f"heartbeat delayed by {gap:.3f}s during {stage}")

    def test_preflight_contention_returns_retryable_without_body_or_writer(self) -> None:
        self.check_preflight_failure(operational_store.StoreRetryable("store-busy"))

    def test_other_preflight_failures_still_propagate(self) -> None:
        self.check_preflight_failure(RuntimeError("synthetic preflight failure"))

    def check_preflight_failure(self, failure) -> None:
        app = self.app()
        with TestClient(app):
            real_read = app.state.writer.store.read
            for stage in ("enter", "query"):
                with self.subTest(stage=stage):
                    @contextmanager
                    def failing_read():
                        if stage == "enter":
                            raise failure
                        with real_read() as view, patch.object(view, "honored_generations", side_effect=failure):
                            yield view

                    reads = []
                    async def receive():
                        reads.append(True)
                        self.fail("body read after preflight failure")

                    with patch.object(app.state.writer.store, "read", side_effect=failing_read) as read, patch.object(
                        app.state.writer, "run", side_effect=AssertionError("writer used"),
                    ) as run:
                        if isinstance(failure, operational_store.StoreRetryable):
                            sent = asyncio.run(self.post_asgi(app, receive))
                            self.assertEqual(sent[0]["status"], 503)
                            self.assertIn((b"content-type", b"application/json"), sent[0]["headers"])
                            self.assertEqual(sent[1]["body"], RETRYABLE)
                            self.assertEqual(sent[1]["body"], ingress.RETRYABLE_BODY)
                        else:
                            with self.assertRaises(type(failure)) as raised:
                                asyncio.run(self.post_asgi(app, receive))
                            self.assertIs(raised.exception, failure)
                        read.assert_called_once_with()
                        run.assert_not_called()
                    self.assertEqual(reads, [])

    def test_each_kind_and_retry_match_direct_units_over_the_same_history(self) -> None:
        app = self.app()
        reference = self.reference()
        batch = outbox("ingest-batch", 2)
        chain = ingest_protocol.chain_next(
            ingest_protocol.chain_start(identity(100)), ingest_protocol.message_sha256(batch),
        )
        history = (
            (outbox("capability-acknowledgement"), 3),
            (batch, 0),
            (outbox("snapshot-completion", 3, page_count=1, fragment_count=1, record_count=1, final_chain=chain), 1),
            (outbox("change-completion", 4, cycle=101), 1),
            (outbox("sync-checkpoint", 5), 2),
        )
        with TestClient(app) as client:
            self.enroll(app.state.writer)
            for data, unit_index in history:
                module, name = ACCEPT_UNITS[unit_index]
                accept = getattr(module, name)
                expected = reference.run(lambda store: accept(store, data, NOW))
                self.assertEqual(expected.outcome, "receipt")
                with self.subTest(kind=ingest_protocol.decode_message(data)["kind"]), ExitStack() as stack:
                    calls = [stack.enter_context(patch.object(mod, attr, wraps=getattr(mod, attr)))
                             for mod, attr in ACCEPT_UNITS]
                    run = stack.enter_context(patch.object(app.state.writer, "run", wraps=app.state.writer.run))
                    for headers in ({}, {"Content-Encoding": "identity"}):
                        for call in calls:
                            call.reset_mock()
                        run.reset_mock()
                        response = client.post("/v1/outbox-messages", content=data, headers=headers)
                        self.assert_answer(response, expected)
                        run.assert_called_once()
                        self.assertEqual([call.call_count for call in calls],
                                         [int(index == unit_index) for index in range(4)])
                        self.assertEqual(calls[unit_index].call_args.args[1:], (data, NOW))
                with app.state.writer.store.read() as view:
                    archived = view.archived_message(identity(1), ingest_protocol.decode_message(data)["sequence"])
                    self.assertEqual(archived.envelope, data)
                    self.assertEqual(archived.receipt, expected.body)

    def test_non_outbox_and_undecodable_bytes_reach_ingest_batch(self) -> None:
        app = self.app()
        reference = self.reference()
        cases = (advertisement(), b"not JSON", b"\xff", b" " + outbox("snapshot-completion"))
        with TestClient(app) as client:
            for data in cases:
                with self.subTest(data=data):
                    expected = reference.run(lambda store: accept_ingest_batch.accept_ingest_batch(store, data, NOW))
                    self.assertEqual(expected.outcome, "block")
                    with ExitStack() as stack:
                        calls = [stack.enter_context(patch.object(mod, attr, wraps=getattr(mod, attr)))
                                 for mod, attr in ACCEPT_UNITS]
                        run = stack.enter_context(patch.object(app.state.writer, "run", wraps=app.state.writer.run))
                        self.assert_answer(client.post("/v1/outbox-messages", content=data), expected)
                        self.assertEqual([call.call_count for call in calls], [1, 0, 0, 0])
                        self.assertEqual(calls[0].call_args.args[1], data)
                        run.assert_called_once()

    def test_pending_installation_returns_exact_retryable_for_every_kind(self) -> None:
        app = self.app()
        with TestClient(app) as client:
            self.enroll(app.state.writer, active=False)
            for kind, index in (("ingest-batch", 0), ("snapshot-completion", 1),
                                ("change-completion", 1), ("sync-checkpoint", 2),
                                ("capability-acknowledgement", 3)):
                with self.subTest(kind=kind):
                    data = outbox(kind)
                    module, name = ACCEPT_UNITS[index]
                    expected = app.state.writer.run(lambda store: getattr(module, name)(store, data, NOW))
                    self.assertEqual(expected.outcome, "retryable")
                    self.assertEqual(expected.failure, "installation-pending")
                    self.assert_answer(client.post("/v1/outbox-messages", content=data), expected)

    def test_body_limit_is_inclusive_and_rejection_never_enters_writer(self) -> None:
        self.settings = replace(self.settings, capabilities=replace(CAPABILITIES, max_message_bytes=4096))
        app = self.app()
        with TestClient(app) as client:
            data = b"x" * 4096
            with patch.object(accept_ingest_batch, "accept_ingest_batch", wraps=accept_ingest_batch.accept_ingest_batch) as accept:
                self.assertEqual(client.post("/v1/outbox-messages", content=data).status_code, 409)
                self.assertEqual(accept.call_args.args[1], data)
            with patch.object(app.state.writer, "run", side_effect=AssertionError("writer used")):
                for data in (b"x" * 4097, iter([b"x" * 2048, b"x" * 2049])):
                    response = client.post("/v1/outbox-messages", content=data)
                    self.assertEqual(response.status_code, 413)
                    self.assertEqual(response.content, b"")

    def test_largest_honored_limit_survives_smaller_configuration(self) -> None:
        self.settings = replace(self.settings, capabilities=replace(CAPABILITIES, max_message_bytes=8192))
        old_app = self.app()
        with TestClient(old_app):
            self.enroll(old_app.state.writer)
        self.settings = replace(self.settings, capabilities=replace(CAPABILITIES, max_message_bytes=4096))
        app = self.app()
        with TestClient(app) as client:
            with app.state.writer.store.read() as view:
                self.assertEqual([g.generation for g in view.honored_generations()], [1, 2])
            data = outbox("ingest-batch", items=[
                {"kind": "retraction", "source_identity": f"synthetic-{number}-" + "x" * 100}
                for number in range(40)
            ])
            self.assertGreater(len(data), 4096)
            self.assertLess(len(data), 8192)
            self.assertEqual(client.post("/v1/outbox-messages", content=data).status_code, 200)
            with patch.object(accept_ingest_batch, "accept_ingest_batch", wraps=accept_ingest_batch.accept_ingest_batch) as accept:
                self.assertEqual(client.post("/v1/outbox-messages", content=b"x" * 8192).status_code, 409)
                self.assertEqual(len(accept.call_args.args[1]), 8192)
            with patch.object(app.state.writer, "run", side_effect=AssertionError("writer used")):
                self.assertEqual(client.post("/v1/outbox-messages", content=b"x" * 8193).status_code, 413)

    def test_configured_limit_applies_without_honored_generations(self) -> None:
        self.settings = replace(self.settings, capabilities=replace(CAPABILITIES, max_message_bytes=4096))
        app = self.app()
        with patch.object(sender_installation, "honor_capabilities", return_value=None), TestClient(app) as client:
            self.assertEqual(client.post("/v1/outbox-messages", content=b"x" * 4096).status_code, 409)
            with patch.object(app.state.writer, "run", side_effect=AssertionError("writer used")):
                self.assertEqual(client.post("/v1/outbox-messages", content=b"x" * 4097).status_code, 413)

    def test_gzip_and_identity_return_same_receipt_and_archive_uncompressed_bytes(self) -> None:
        app = self.app()
        reference = self.reference()
        data = outbox("ingest-batch")
        expected = reference.run(lambda store: accept_ingest_batch.accept_ingest_batch(store, data, NOW))
        with TestClient(app) as client:
            self.enroll(app.state.writer)
            compressed = gzip.compress(data)
            with patch.object(accept_ingest_batch, "accept_ingest_batch", wraps=accept_ingest_batch.accept_ingest_batch) as accept:
                self.assert_answer(client.post("/v1/outbox-messages", content=compressed,
                                               headers={"Content-Encoding": "gzip"}), expected)
                self.assertEqual(accept.call_args.args[1], data)
            self.assert_answer(client.post("/v1/outbox-messages", content=data), expected)
            with app.state.writer.store.read() as view:
                self.assertEqual(view.archived_message(identity(1), 1).envelope, data)

    def test_gzip_inclusive_output_limit_and_incompressible_overhead(self) -> None:
        self.settings = replace(self.settings, capabilities=replace(CAPABILITIES, max_message_bytes=4096))
        app = self.app()
        data = random.Random(42).randbytes(4096)
        compressed = gzip.compress(data)
        self.assertGreater(len(compressed), len(data))
        with TestClient(app) as client:
            with patch.object(accept_ingest_batch, "accept_ingest_batch", wraps=accept_ingest_batch.accept_ingest_batch) as accept:
                response = client.post("/v1/outbox-messages", content=compressed, headers={"Content-Encoding": "GZip"})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(accept.call_args.args[1], data)

    def test_invalid_transport_never_enters_writer(self) -> None:
        self.settings = replace(self.settings, capabilities=replace(CAPABILITIES, max_message_bytes=4096))
        compressed = gzip.compress(b"x" * 4096)
        damaged = bytearray(compressed)
        damaged[-8] ^= 1  # Invalid CRC, with otherwise intact gzip framing.
        cases = (
            (gzip.compress(b"x" * 4097), [("Content-Encoding", "gzip")], 413),
            (gzip.compress(b"x" * 1_000_000), [("Content-Encoding", "gzip")], 413),
            (b"x" * 5121, [("Content-Encoding", "gzip")], 413),
            (b"not gzip", [("Content-Encoding", "gzip")], 400),
            (bytes(damaged), [("Content-Encoding", "gzip")], 400),
            (compressed[:-1], [("Content-Encoding", "gzip")], 400),
            (compressed + b"trailing", [("Content-Encoding", "gzip")], 400),
            (compressed + gzip.compress(b"second member"), [("Content-Encoding", "gzip")], 400),
            (compressed, [("Content-Encoding", "br")], 415),
            (compressed, [("Content-Encoding", "gzip, identity")], 415),
            (compressed, [("Content-Encoding", "gzip"), ("Content-Encoding", "gzip")], 415),
            (b"x", [("Content-Encoding", "")], 415),
        )
        app = self.app()
        with TestClient(app) as client:
            with patch.object(app.state.writer, "run", side_effect=AssertionError("writer used")):
                for data, headers, status in cases:
                    with self.subTest(headers=headers, status=status, length=len(data)):
                        response = client.post("/v1/outbox-messages", content=data, headers=headers)
                        self.assertEqual(response.status_code, status)
                        self.assertEqual(response.content, b"")

    def test_stream_stops_on_first_over_limit_chunk(self) -> None:
        self.settings = replace(self.settings, capabilities=replace(CAPABILITIES, max_message_bytes=4096))
        app = self.app()
        with TestClient(app), patch.object(app.state.writer, "run", side_effect=AssertionError("writer used")):
            for headers, chunks in (
                ([], [b"x" * 4096, b"x"]),
                ([(b"content-encoding", b"gzip")], [b"x" * 5120, b"x"]),
            ):
                with self.subTest(headers=headers):
                    pending = iter(chunks)
                    sent = []
                    async def receive():
                        # A third read fails: the route must stop on chunk two.
                        return {"type": "http.request", "body": next(pending), "more_body": True}
                    async def send(message):
                        sent.append(message)
                    scope = {
                        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                        "method": "POST", "scheme": "http", "path": "/v1/outbox-messages",
                        "raw_path": b"/v1/outbox-messages", "query_string": b"", "root_path": "",
                        "headers": headers, "server": ("testserver", 80), "client": ("testclient", 1),
                    }
                    asyncio.run(app(scope, receive, send))
                    self.assertEqual(sent[0]["status"], 413)
                    self.assertEqual(sent[1]["body"], b"")

    def test_store_busy_returns_exact_retryable_bytes(self) -> None:
        real_open = operational_store.open_store
        def short_timeout(path, *, reserve_bytes):
            return real_open(path, reserve_bytes=reserve_bytes, busy_timeout_ms=50)
        app = self.app()
        with patch.object(operational_store, "open_store", side_effect=short_timeout), TestClient(app) as client:
            self.enroll(app.state.writer)
            connection = sqlite3.connect(self.path, isolation_level=None)
            try:
                connection.execute("BEGIN IMMEDIATE")
                for kind in ("ingest-batch", "snapshot-completion", "change-completion",
                             "sync-checkpoint", "capability-acknowledgement"):
                    with self.subTest(kind=kind):
                        response = client.post("/v1/outbox-messages", content=outbox(kind))
                        self.assertEqual(response.status_code, 503)
                        self.assertEqual(response.content, RETRYABLE)
                        self.assertEqual(response.headers["content-type"], "application/json")
                with app.state.writer.store.read() as view:
                    self.assertIsNone(view.archived_message(identity(1), 1))
            finally:
                connection.rollback()
                connection.close()
            self.assertEqual(client.post("/v1/outbox-messages", content=outbox("ingest-batch")).status_code, 200)

    def test_concurrent_deliveries_and_intents_share_one_writer(self) -> None:
        app = self.app()
        trace = WriteTrace()
        data = outbox("capability-acknowledgement")
        real_accept = accept_capability_acknowledgement.accept_capability_acknowledgement
        count = 6
        barrier = threading.Barrier(count * 2)
        with TestClient(app) as client:
            self.enroll(app.state.writer)
            def traced_accept(store, data, now):
                # Widen the first delivery's read/transaction race window
                # and keep retries inside the same observed call boundary.
                return trace.operation("http", lambda proxy: real_accept(proxy, data, now), store)
            def request(number):
                barrier.wait(timeout=10)
                return client.post("/v1/outbox-messages", content=data)
            def intent(number):
                barrier.wait(timeout=10)
                return app.state.writer.run(lambda store: trace.operation(
                    f"offer-{number}", lambda proxy: sender_installation.offer_enrollment(
                        proxy, URL, NOW, random_bytes=lambda size: bytes([100 + number]) * size,
                    ), store,
                ))
            with patch.object(accept_capability_acknowledgement, "accept_capability_acknowledgement", side_effect=traced_accept):
                with ThreadPoolExecutor(max_workers=count * 2) as callers:
                    requests = [callers.submit(request, number) for number in range(count)]
                    intents = [callers.submit(intent, number) for number in range(count)]
                    receipts = [future.result(timeout=15) for future in requests]
                    for future in intents:
                        future.result(timeout=15)
            self.assertEqual(trace.maximum, 1)
            self.assertEqual(trace.inside, 0)
            self.assertEqual(sum(kind == "enter" for kind, _ in trace.events), count * 2)
            active = None
            for kind, name in trace.events:
                if kind == "enter":
                    self.assertIsNone(active)
                    active = name
                else:
                    self.assertEqual(name, active)
                    if kind == "exit":
                        active = None
            self.assertIsNone(active)
            for response in receipts:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, receipts[0].content)

    def test_clock_is_read_inside_writer_operation(self) -> None:
        moments = []
        writer_thread = []
        def clock():
            moments.append(threading.get_ident())
            return NOW
        app = ingress.create_app(self.settings, clock=clock)
        with TestClient(app) as client:
            app.state.writer.run(lambda store: writer_thread.append(threading.get_ident()))
            moments.clear()
            response = client.post("/v1/outbox-messages", content=b"malformed")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(moments, writer_thread)


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
        with patch("uvicorn.Server") as server, redirect_stdout(stdout), self.assertRaises(SystemExit) as result:
            cli.main(["serve", "--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("--config", stdout.getvalue())
        server.assert_not_called()

    def test_serve_uses_config_and_one_worker_without_reload(self) -> None:
        self.config_path.write_text(json.dumps(self.config()), encoding="utf-8")
        with patch("uvicorn.Server") as server:
            self.assertEqual(cli.main(["serve", "--config", str(self.config_path)]), 0)
        config = server.call_args.args[0]
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8443)
        self.assertEqual(config.workers, 1)
        self.assertFalse(config.reload)
        self.assertFalse(config.access_log)
        self.assertEqual(config.app.state.settings.reserve_bytes, 12345)
        server.return_value.run.assert_called_once_with()

    def test_server_config_is_testable_without_starting_a_server(self) -> None:
        with patch("uvicorn.Server") as server:
            config = ingress.create_server_config(self.settings)
        server.assert_not_called()
        self.assertEqual(config.host, self.settings.host)
        self.assertEqual(config.port, self.settings.port)
        self.assertEqual(config.workers, 1)
        self.assertFalse(config.reload)
        self.assertFalse(config.access_log)
        self.assertEqual(config.app.state.settings, self.settings)
        self.assertFalse(self.path.exists())

    def test_invalid_config_is_reported_without_starting_a_server(self) -> None:
        stderr = io.StringIO()
        self.config_path.write_text('{"SYNTHETIC-SECRET":', encoding="utf-8")
        with patch("uvicorn.Server") as server, redirect_stderr(stderr):
            self.assertEqual(cli.main(["serve", "--config", str(self.config_path)]), 1)
        self.assertEqual(stderr.getvalue(), "invalid Portal configuration\n")
        server.assert_not_called()


if __name__ == "__main__":
    unittest.main()
