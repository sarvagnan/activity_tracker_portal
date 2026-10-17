"""Synthetic smoke-tool coverage: temporary stores and loopback sockets only."""

from __future__ import annotations

import asyncio
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import re
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from activity_tracker_portal import ingress, ingest_protocol, operational_store as os_store, sender_installation
from tools.device_smoke import fault_proxy, portal_smoke

NOW = datetime.now(timezone.utc)
IDENTITY = "00000000-0000-4000-8000-000000000001"
FINGERPRINT = ingest_protocol.installation_fingerprint(IDENTITY)
BODY = b'{"synthetic-response":true}'


class PortalSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = self.directory / "portal.json"
        code, _, _ = self.call("init", "--dir", str(self.directory), "--portal-url", "http://smoke.example.ts.net:8766", "--reserve-bytes", "0")
        self.assertEqual(code, 0)
        self.settings = ingress.PortalSettings.from_file(self.config)

    def call(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = portal_smoke.main(list(args))
        return result, stdout.getvalue(), stderr.getvalue()

    def command(self, command, *args):
        return self.call(command, "--config", str(self.config), *args)

    def writer(self):
        return ingress.PortalWriter(self.settings.store_path, reserve_bytes=0)

    def enroll(self):
        code, output, _ = self.command("offer")
        self.assertEqual(code, 0)
        offered = re.search(r"Enrollment code: (\S+)", output).group(1)
        message = ingest_protocol.encode_message({
            "wire_version": 1, "kind": "enrollment-confirmation", "enrollment_code": offered,
            "sender_installation": IDENTITY, "fingerprint": FINGERPRINT,
        })
        writer = self.writer()
        try:
            response = writer.run(lambda store: sender_installation.answer_enrollment_confirmation(store, message, NOW))
            self.assertEqual(response.outcome, "pending")
        finally:
            writer.close()
        return offered

    def activate(self):
        code = self.enroll()
        self.assertEqual(self.command("confirm", "--installation", IDENTITY, "--fingerprint", FINGERPRINT)[0], 0)
        return code

    def test_init_all_schema_types_and_existing_refusal(self):
        schemas = Path(__file__).resolve().parents[2] / "projection/v1/schemas"
        expected = tuple((name, (1,)) for name in sorted(file.name.removesuffix(".schema.json") for file in schemas.glob("*.schema.json")))
        self.assertEqual(self.settings.capabilities.projection_schemas, expected)
        self.assertEqual(len(expected), 41)
        self.assertEqual(self.settings.store_path, (self.directory / "store.sqlite3").resolve())
        before = self.config.read_bytes()
        code, output, error = self.call("init", "--dir", str(self.directory), "--portal-url", "http://other.test")
        self.assertEqual((code, output, error), (2, "", "refused: config-already-exists\n"))
        self.assertEqual(before, self.config.read_bytes())

    def test_invalid_init_removes_config_and_invalid_config_is_private(self):
        other = self.directory / "bad"
        code, _, error = self.call("init", "--dir", str(other), "--portal-url", "secret-invalid-url")
        self.assertEqual(code, 2)
        self.assertNotIn("secret", error)
        self.assertFalse((other / "portal.json").exists())
        self.config.write_text("secret-invalid-config")
        self.assertEqual(self.command("status"), (2, "", "refused: invalid-configuration-or-io\n"))

    def test_offer_pending_status_wrong_and_right_fingerprints(self):
        self.assertEqual(self.command("status")[1], "no installations\n")
        self.enroll()
        code, output, _ = self.command("status")
        self.assertEqual(code, 0)
        for text in ("installation 1", f"id: {IDENTITY}", f"fingerprint: {FINGERPRINT}", "state: pending",
                     "enrolled_at:", "activated_at:", "draining_at:", "retired_at:", "retirement_kind:",
                     "last_delivery_at:", "latest_checkpoint_receipt_at:", "background_scheduling:",
                     "sequence_gaps: 0", "delivery_blocks: 0"):
            self.assertIn(text, output)
        self.assertEqual(self.command("confirm", "--installation", IDENTITY, "--fingerprint", "wrong"),
                         (2, "", "refused: fingerprint-mismatch\n"))
        self.assertEqual(self.command("confirm", "--installation", IDENTITY, "--fingerprint", FINGERPRINT),
                         (0, f"installation {IDENTITY} is now active\n", ""))

    def test_drain_and_retire(self):
        self.activate()
        self.assertEqual(self.command("drain", "--installation", IDENTITY)[1], f"installation {IDENTITY} is now draining\n")
        self.assertEqual(self.command("retire", "--installation", IDENTITY, "--final-sequence", "1")[2], "refused: drain-sequence-mismatch\n")
        self.assertEqual(self.command("retire", "--installation", IDENTITY, "--final-sequence", "0")[0], 0)
        self.assertIn("retirement_kind: drained", self.command("status")[1])

    def test_destructive_retirement_ack_required(self):
        self.activate()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self.command("retire-destructively", "--installation", IDENTITY)
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(self.command("retire-destructively", "--installation", IDENTITY, "--acknowledge-possible-loss")[0], 0)
        self.assertIn("retirement_kind: destructive", self.command("status")[1])

    def test_withdraw_pending(self):
        self.enroll()
        self.assertEqual(self.command("withdraw", "--installation", IDENTITY)[1], f"installation {IDENTITY} is now withdrawn\n")

    def test_busy_exact_error_and_retryable_mapping(self):
        writer = self.writer()
        try:
            self.assertEqual(self.command("status"), (3, "", "The Portal is running: stop portal serve, then retry.\n"))
        finally:
            writer.close()
        for exception in (os_store.StoreRetryable, sender_installation.InstallationRetryable):
            with patch.object(sender_installation, "honor_capabilities", side_effect=exception("store-busy")):
                self.assertEqual(self.command("offer"), (4, "", "retryable: store-busy\n"))

    def populate(self):
        writer = self.writer()
        def write(store):
            def header(sequence):
                envelope = f"synthetic-private-envelope-{sequence}".encode()
                return os_store.MessageHeader(IDENTITY, sequence, hashlib.sha256(envelope).hexdigest(), 1,
                                              NOW, envelope, b"synthetic-private-receipt")
            store.archive_ingest_batch(header(1), os_store.IngestBatchRecord(
                "steps", 1, "initial-snapshot", "private-cycle", 1, 1, "private-chain",
                (os_store.BatchItem(projection=b'{"private-health-value":123}'),
                 os_store.BatchItem(retracted_identity="private-retracted-identity"))))
            store.archive_completion(header(2), os_store.CompletionRecord(
                "snapshot", "initial", "steps", "private-cycle", 1, 1, 2, "private-chain"))
            store.archive_sync_checkpoint(header(4), os_store.SyncCheckpointRecord(
                7, "manual", "private-started-at", "private-ended-at", 42, "available",
                (os_store.RecordTypeOutcome("steps", "caught-up", None, True),
                 os_store.RecordTypeOutcome("weight", "deferred", "time-budget-reached", False)), 2, 300))
            store.archive_capability_acknowledgement(header(5))
            for _ in range(2):
                store.record_delivery_block(IDENTITY, 6, "private-digest", "synthetic-failure", NOW)
        try:
            writer.run(write)
        finally:
            writer.close()

    def test_report_redacts_and_aggregates_and_reads_are_byte_identical(self):
        offered = self.activate()
        self.populate()
        def files():
            return {file.name: file.read_bytes() for file in self.directory.glob("store.sqlite3*")}
        before = files()
        for command in ("report", "status"):
            code, output, error = self.command(command)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(files(), before)
            if command == "report":
                for forbidden in (IDENTITY, FINGERPRINT, self.settings.portal_url, offered, "private-"):
                    self.assertNotIn(forbidden, output)
                for expected in ("## installation 1", "ingest-batch×1", "missing×1", "sync-checkpoint×1",
                                 "capability-acknowledgement×1", "initial-snapshot×1", "projections: 1", "retractions: 1",
                                 "snapshot/initial×1", "completion_record_counts: snapshot/initial×2", "run_sequence=7",
                                 "trigger=manual", "elapsed_ms=42", "background_scheduling=available",
                                 "outbox_messages_before_checkpoint=2", "outbox_bytes_before_checkpoint=300",
                                 "caught-up×1", "deferred/time-budget-reached×1", "synthetic-failure×2",
                                 "honored_capability_generations", "storage_reserve: admitting=true"):
                    self.assertIn(expected, output)


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append((self.command, self.path, body, dict(self.headers)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    do_GET = do_POST

    def log_message(self, *_):
        pass


class FaultProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        self.upstream.requests = []
        self.thread = threading.Thread(target=self.upstream.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.proxy = fault_proxy.FaultProxy(f"http://127.0.0.1:{self.upstream.server_port}", self.directory)
        self.server = await self.proxy.start("127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.output = io.StringIO()
        self.capture = redirect_stdout(self.output)
        self.capture.__enter__()

    async def asyncTearDown(self):
        try:
            await self.proxy.close()
            await asyncio.to_thread(self.upstream.shutdown)
            self.upstream.server_close()
            self.thread.join(timeout=2)
            self.assertFalse(self.thread.is_alive())
        finally:
            self.capture.__exit__(None, None, None)
            self.temporary.cleanup()

    async def open(self, mode="pass", *, method="POST", path="/v1/outbox-messages", body=b"synthetic-private-body", raw=None):
        if mode is not None:
            fault_proxy.set_fault(self.directory, mode)
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.port), 2)
        data = raw if raw is not None else (
            f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(body)}\r\n"
            "Content-Type: application/json\r\nContent-Encoding: identity\r\nAccept-Encoding: gzip\r\n\r\n".encode() + body)
        writer.write(data)
        await writer.drain()
        return reader, writer

    async def response(self, **kwargs):
        reader, writer = await self.open(**kwargs)
        try:
            data = await asyncio.wait_for(reader.read(), 3)
        finally:
            writer.close()
            await writer.wait_closed()
        await self.logged()
        header, body = data.split(b"\r\n\r\n", 1)
        self.assertIn(b"Connection: close", header)
        return header, body

    async def logged(self, count=None):
        for _ in range(200):
            path = self.directory / "requests.log"
            entries = path.read_text().splitlines() if path.exists() else []
            if (count is None and not self.proxy.tasks) or (count is not None and len(entries) >= count):
                return entries
            await asyncio.sleep(0.01)
        self.fail("proxy request did not finish within 2 s")

    async def test_pass_exact_and_forwarded_headers(self):
        header, body = await self.response()
        self.assertIn(b"200 OK", header)
        self.assertEqual(body, BODY)
        method, path, received, values = self.upstream.requests[0]
        self.assertEqual((method, path, received), ("POST", "/v1/outbox-messages", b"synthetic-private-body"))
        self.assertEqual(values["accept-encoding"], "identity")
        self.assertEqual(values["content-type"], "application/json")
        self.assertEqual(values["content-encoding"], "identity")
        log = (self.directory / "requests.log").read_text()
        self.assertIn("upstream=200", log)
        self.assertIn(f"resp_bytes_sent={len(BODY)} ended=complete", log)
        self.assertEqual(self.output.getvalue(), log)
        self.assertNotIn("private-body", log)
        self.assertNotIn(hashlib.sha256(b"synthetic-private-body").hexdigest(), log)

    async def test_drop_forwards_without_response_and_is_one_shot(self):
        fault_proxy.set_fault(self.directory, "drop")
        for method, path in (("GET", "/v1/capabilities"), ("POST", "/v1/enrollment-confirmation")):
            _, body = await self.response(mode=None, method=method, path=path, body=b"")
            self.assertEqual(body, BODY)
            self.assertTrue((self.directory / "fault").exists())
        reader, writer = await self.open(mode=None)
        try:
            try:
                self.assertEqual(await asyncio.wait_for(reader.read(1), 3), b"")
            except ConnectionResetError:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionResetError:
                pass
        entries = await self.logged(3)
        self.assertEqual(self.upstream.requests[-1][:3],
                         ("POST", "/v1/outbox-messages", b"synthetic-private-body"))
        self.assertIn("mode=drop", entries[-1])
        self.assertIn("upstream=200 resp_bytes_sent=0 ended=dropped", entries[-1])
        self.assertFalse((self.directory / "fault").exists())
        header, body = await self.response(mode=None)
        self.assertIn(b"200 OK", header)
        self.assertEqual(body, BODY)
        self.assertIn("mode=pass", (await self.logged(4))[-1])
        self.assertEqual(len(self.upstream.requests), 4)

    async def test_only_post_bodies_are_numbered_with_interleaved_gets(self):
        for method, body in (("GET", b""), ("POST", b""), ("GET", b""),
                             ("POST", b"different-body"), ("GET", b""), ("POST", b"")):
            await self.response(method=method, path="/v1/capabilities" if method == "GET" else "/v1/outbox-messages",
                                body=body)
        entries = await self.logged(6)
        self.assertEqual([entry.split(" body=", 1)[1] for entry in entries],
                         ["-", "1#1", "-", "2#1", "-", "1#2"])
        self.assertEqual(len(self.proxy.bodies), 2)

    async def test_truncate_and_small_body(self):
        header, body = await self.response(mode="truncate")
        length = int(re.search(rb"Content-Length: (\d+)", header).group(1))
        self.assertEqual(length, len(BODY))
        self.assertEqual(body, BODY[:len(BODY) // 2])
        with patch.object(self.proxy, "forward", return_value=(200, "application/json", b"x")):
            _, body = await self.response(mode="truncate")
        self.assertEqual(body, b"")

    async def test_stall_socket_timeout_then_client_closed(self):
        # Run a blocking socket client in a worker so the proxy event loop lives.
        fault_proxy.set_fault(self.directory, "stall")
        def client():
            with socket.create_connection(("127.0.0.1", self.port), timeout=1) as connection:
                connection.sendall(b"POST /v1/outbox-messages HTTP/1.1\r\nHost: localhost\r\nContent-Length: 1\r\n\r\nx")
                data = b""
                while b"\r\n\r\n" not in data:
                    data += connection.recv(1)
                self.assertEqual(connection.recv(1), BODY[:1])
                with self.assertRaises(socket.timeout):
                    connection.recv(1)
        with patch.object(fault_proxy, "STALL_SECONDS", 3):
            await asyncio.wait_for(asyncio.to_thread(client), 2.5)
            entries = await self.logged()
        self.assertIn("resp_bytes_sent=1 ended=client-closed", entries[-1])

    async def test_stall_timeout(self):
        with patch.object(fault_proxy, "STALL_SECONDS", 0.05):
            header, body = await self.response(mode="stall")
        self.assertIn(b"200 OK", header)
        self.assertEqual(body, BODY[:1])
        self.assertIn("ended=timeout", (await self.logged())[-1])

    async def test_oversize_and_refuse_large_abort_promptly(self):
        for mode, status in (("oversize", b"200"), ("refuse-large", b"409")):
            reader, writer = await self.open(mode)
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            self.assertIn(status, header)
            self.assertNotIn(b"Content-Length", header)
            self.assertEqual(await asyncio.wait_for(reader.readexactly(100), 2), b" " * 100)
            writer.close()
            await writer.wait_closed()
            entry = (await self.logged())[-1]
            self.assertIn("ended=client-closed", entry)
            sent = int(re.search(r"resp_bytes_sent=(\d+)", entry).group(1))
            self.assertGreaterEqual(sent, 100)
            self.assertLess(sent, 1024 * 1024)

    async def test_redirect_and_identical_body_attempts(self):
        header, body = await self.response(mode="redirect")
        self.assertIn(b"307 Temporary Redirect", header)
        self.assertIn(b"Location: /v1/outbox-messages", header)
        self.assertEqual(len(body), 65536)
        await self.response(mode=None)
        entries = await self.logged(2)
        self.assertIn("body=1#1", entries[0])
        self.assertIn("body=1#2", entries[1])
        self.assertIn("mode=pass", entries[1])
        self.assertEqual(len(self.upstream.requests), 2)

    async def test_one_shot_bypasses_enrollment_and_capabilities(self):
        fault_proxy.set_fault(self.directory, "truncate")
        for method, path in (("POST", "/v1/enrollment-confirmation"), ("GET", "/v1/capabilities")):
            _, body = await self.response(mode=None, method=method, path=path)
            self.assertEqual(body, BODY)
            self.assertTrue((self.directory / "fault").exists())
        _, body = await self.response(mode=None)
        self.assertLess(len(body), len(BODY))
        self.assertFalse((self.directory / "fault").exists())
        _, body = await self.response(mode=None)
        self.assertEqual(body, BODY)
        self.assertEqual(len(self.upstream.requests), 4)

    async def test_set_fault_cli_clear_and_atomic_file(self):
        for mode in fault_proxy.MODES:
            self.assertEqual(fault_proxy.main(["set-fault", "--state-dir", str(self.directory), mode]), 0)
            self.assertEqual((self.directory / "fault").read_text().strip(), mode)
        self.assertEqual(fault_proxy.main(["set-fault", "--state-dir", str(self.directory), "clear"]), 0)
        self.assertFalse((self.directory / "fault").exists())
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_chunked_and_malformed_requests(self):
        raw = (b"POST /v1/outbox-messages HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n"
               b"3\r\nabc\r\n2;extension=yes\r\nde\r\n0\r\nTrailer: value\r\n\r\n")
        _, body = await self.response(raw=raw)
        self.assertEqual(body, BODY)
        self.assertEqual(self.upstream.requests[-1][2], b"abcde")
        malformed = [b"broken\r\n\r\n", b"GET / HTTP/1.1\r\nBadHeader\r\n\r\n",
                     b"POST / HTTP/1.1\r\nContent-Length: nope\r\n\r\n",
                     b"POST / HTTP/1.1\r\nContent-Length: 0\r\nContent-Length: 1\r\n\r\n",
                     b"POST / HTTP/1.1\r\nContent-Length: 0\r\nTransfer-Encoding: chunked\r\n\r\n",
                     b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\nZ\r\n",
                     b"GET / HTTP/1.1\r\nX: " + b"x" * 65537 + b"\r\n\r\n"]
        for request in malformed:
            header, _ = await self.response(raw=request)
            self.assertIn(b"400 Bad Request", header)
            self.assertTrue((await self.logged())[-1].endswith(" body=-"))
        self.assertEqual(len(self.proxy.bodies), 1)

    async def test_request_body_limit_length_and_chunked(self):
        for request in (
            f"POST / HTTP/1.1\r\nContent-Length: {fault_proxy.MAX_REQUEST_BYTES + 1}\r\n\r\n".encode(),
            f"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n{fault_proxy.MAX_REQUEST_BYTES + 1:x}\r\n".encode(),
        ):
            header, body = await self.response(raw=request)
            self.assertIn(b"HTTP/1.1 413 ", header)
            self.assertEqual(body, b"")
            self.assertTrue((await self.logged())[-1].endswith(" body=-"))
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(self.proxy.bodies, {})

    async def test_unreachable_upstream_502(self):
        with socket.socket() as unavailable:
            unavailable.bind(("127.0.0.1", 0))
            self.proxy.port = unavailable.getsockname()[1]
        header, body = await self.response()
        self.assertIn(b"502 Bad Gateway", header)
        self.assertEqual(body, b"")
        self.assertIn("upstream=error", (await self.logged())[-1])


if __name__ == "__main__":
    unittest.main()
