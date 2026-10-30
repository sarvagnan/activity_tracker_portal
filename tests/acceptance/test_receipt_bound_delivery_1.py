"""Acceptance tests for `receipt-bound-delivery@1`: the Governed Scope contract and the end-to-end evidence.

Requirement numbers refer to `packs/receipt-bound-delivery-1.spec.md`. The Portal runs in this process: the
ingress app from `create_app`, under uvicorn on a loopback socket, with a controlled clock and a real
Operational Store file. A fault-injecting proxy sits in front of it. The Sender is
`sender/app/test/support/receipt-bound-sender.ts`, one fresh Node process per step, so every step is also a
Sender restart; it composes the published Sender units and reaches the Portal only through this unit's HTTP
transport. The TypeScript suite `receipt-bound-delivery-1.test.ts` checks the transport alone.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

import uvicorn

from activity_tracker_portal import ingress, sender_installation
from activity_tracker_portal.operational_store import OperationalStore, StoreView

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/receipt-bound-delivery.1.json").read_text(encoding="utf-8"))
DRIVER = ROOT / "sender/app/test/support/receipt-bound-sender.ts"
T = TypeVar("T")
START = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)
CAPABILITIES = sender_installation.Capabilities(
    wire_versions=(1,),
    projection_schemas=(("heart-rate", (1,)), ("steps", (1,))),
    max_record_count=4,
    max_message_bytes=65_536,
)
FAULTS = ("pass", "drop-request", "drop-response", "truncate-response", "reset")
INPUT_MODES = {
    "operator_intent": "pure",
    "staged_messages": "effectful",
    "fetch": "effectful",
    "network": "volatile",
    "portal_ingress": "effectful",
    "portal_store": "effectful",
    "portal_clock": "volatile",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_http(connection: socket.socket) -> bytes:
    """One HTTP/1.1 message with a Content-Length body, or what arrived before the peer closed."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(65_536)
        if not chunk:
            return data
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    while len(body) < length:
        chunk = connection.recv(65_536)
        if not chunk:
            break
        body += chunk
    return head + b"\r\n\r\n" + body


def http_body(message: bytes) -> bytes:
    return message.partition(b"\r\n\r\n")[2]


class Proxy:
    """A loopback proxy that injects one fault per connection and records every exchange."""

    def __init__(self) -> None:
        self.upstream_port = 0
        self.faults: deque[str] = deque()
        self.exchanges: list[dict[str, Any]] = []
        # Called with each upstream answer before the client sees any of it.
        self.on_response: Callable[[bytes], None] | None = None
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self._closed = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _serve(self) -> None:
        while not self._closed:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        with client:
            client.settimeout(30)
            request = read_http(client)
            # Every Sender route is under /v1/. Another local process probing this port, with an
            # empty connection or `GET /`, must neither take the attempt's fault nor count as an exchange.
            line = request.partition(b"\r\n")[0].split(b" ")
            if len(line) != 3 or not line[1].startswith(b"/v1/"):
                return
            fault = self.faults.popleft() if self.faults else "pass"
            exchange: dict[str, Any] = {"fault": fault, "request": http_body(request), "response": None}
            self.exchanges.append(exchange)
            if fault == "drop-request":
                return
            with socket.create_connection(("127.0.0.1", self.upstream_port), timeout=30) as upstream:
                upstream.sendall(request)
                response = read_http(upstream)
            exchange["response"] = http_body(response)
            if self.on_response is not None:
                self.on_response(exchange["response"])
            if fault == "pass":
                client.sendall(response)
            elif fault == "truncate-response":
                head, _, body = response.partition(b"\r\n\r\n")
                client.sendall(head + b"\r\n\r\n" + body[: max(1, len(body) // 2)])
            elif fault == "reset":
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, (1).to_bytes(4, "little") + (0).to_bytes(4, "little"))
            # drop-response: close without answering.

    def close(self) -> None:
        self._closed = True
        self._listener.close()


class Portal:
    """The ingress app under uvicorn on loopback, with a controlled clock and a real store file."""

    def __init__(self, store_path: Path, proxy: Proxy) -> None:
        self.store_path = store_path
        self.proxy = proxy
        self.now = START
        self.app: Any = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self, reserve_bytes: int = 0) -> None:
        listener = socket.create_server(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        settings = ingress.PortalSettings(self.store_path, reserve_bytes, self.proxy.url, "127.0.0.1", port, CAPABILITIES)
        self.app = ingress.create_app(settings, clock=lambda: self.now)
        self._server = uvicorn.Server(uvicorn.Config(self.app, lifespan="on", access_log=False, log_level="critical"))
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [listener]}, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 15
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise AssertionError("the Portal did not start")
            time.sleep(0.01)
        self.proxy.upstream_port = port

    def stop(self) -> None:
        assert self._server is not None and self._thread is not None
        self._server.should_exit = True
        self._thread.join(15)
        if self._thread.is_alive():
            raise AssertionError("the Portal did not stop")

    def restart(self, reserve_bytes: int = 0) -> None:
        self.stop()
        self.start(reserve_bytes)

    def run(self, operation: Callable[[OperationalStore], T]) -> T:
        return self.app.state.writer.run(operation)

    @contextmanager
    def read(self) -> Iterator[StoreView]:
        with self.app.state.writer.store.read() as view:
            yield view


class Case(unittest.TestCase):
    """One Portal, proxy, and Sender per test, with the evidence's invariants checked after every step."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="receipt-bound-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        (self.directory / "sender").mkdir()
        self.proxy = Proxy()
        self.addCleanup(self.proxy.close)
        self.portal = Portal(self.directory / "portal.sqlite3", self.proxy)
        self.portal.start()
        self.addCleanup(self._stop)
        self.installation: str | None = None
        self.staged: dict[int, str] = {}
        self.kinds: dict[int, str] = {}
        self.accepted_at: dict[int, datetime] = {}
        self.run_sequence = 0
        self.high_water = 0
        self.violations: list[str] = []
        self.proxy.on_response = self.receipt_crossed

    def _stop(self) -> None:
        try:
            self.portal.stop()
        except AssertionError:
            pass

    # The Sender

    def expected_kinds(self, command: str, arguments: dict[str, Any]) -> list[str] | None:
        if command == "enroll":
            return ["capability-acknowledgement"]
        if command == "stage-initial":
            completion = [] if arguments.get("complete") is False else ["snapshot-completion"]
            return ["ingest-batch"] * len(arguments["pages"]) + completion
        if command == "stage-change":
            return ["ingest-batch", "change-completion"]
        if command == "stage-checkpoint":
            return ["sync-checkpoint"]
        return None

    def check_staged(self, command: str, output: dict[str, Any]) -> None:
        result = output["result"]
        if command == "stage-initial":
            self.assertTrue(result["staged"] and all(outcome == "staged" for outcome in result["staged"]), result)
            self.assertIn(result.get("completion", "staged"), ("staged",), result)
        elif command == "stage-change":
            self.assertEqual((result["batch"], result["completion"]), ("staged", "staged"), result)
        elif command == "stage-checkpoint":
            self.assertEqual(result, "staged")

    def sender(self, command: str, **arguments: Any) -> dict[str, Any]:
        process = subprocess.run(
            ["node", str(DRIVER), str(self.directory / "sender"), command, json.dumps(arguments)],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        if process.returncode != 0:
            raise AssertionError(f"Sender {command} failed: {process.stderr[-2000:]}")
        output = json.loads(process.stdout.strip().splitlines()[-1])
        self.check_staged(command, output)
        # Every newly allocated sequence is still held, with the kinds the command stages, before any delivery.
        expected = self.expected_kinds(command, arguments)
        if expected is not None:
            listed = {row["sequence"]: row["kind"] for row in output["outbox"]}
            self.assertEqual(output["highWater"] - self.high_water, len(expected), command)
            self.assertEqual([listed.get(sequence) for sequence in range(self.high_water + 1, output["highWater"] + 1)], expected, command)
        else:
            self.assertEqual(output["highWater"], self.high_water, command)
        self.high_water = output["highWater"]
        for row in output["outbox"]:
            # A row first seen is newly staged; its bytes must never change afterwards.
            self.staged.setdefault(row["sequence"], row["sha256"])
            self.kinds.setdefault(row["sequence"], row["kind"])
        self.check(output)
        return output

    def enroll(self, *, confirm: bool = True) -> str:
        offer = self.portal.run(lambda store: sender_installation.offer_enrollment(
            store, self.proxy.url, self.portal.now, random_bytes=lambda count: b"\x07" * count,
        ))
        enrolled = self.sender("enroll", offer=offer.message.hex())
        self.assertEqual(enrolled["result"], {"outcome": "enrolled", "generation": 1})
        status = enrolled["status"]
        self.installation = status["installationId"]
        if confirm:
            self.confirm(status)
        return status["installationId"]

    def confirm(self, status: dict[str, Any] | None = None) -> None:
        status = status or self.sender("status")["status"]
        self.portal.run(lambda store: sender_installation.confirm_installation(
            store, status["installationId"], status["fingerprint"], self.portal.now,
        ))

    def probe(self) -> None:
        """Reach the proxy as another local process would, and wait until it has closed each connection."""
        for payload in (b"", b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"):
            with socket.create_connection(("127.0.0.1", self.proxy.port), timeout=30) as connection:
                connection.sendall(payload)
                connection.shutdown(socket.SHUT_WR)
                while connection.recv(65_536):
                    pass

    def send(self, fault: str = "pass") -> dict[str, Any]:
        # The fault applies to this attempt's request only; an attempt on an empty Outbox makes none.
        self.proxy.faults.clear()
        self.proxy.faults.append(fault)
        if fault != "pass":
            self.probe()
        before = self.archived()
        try:
            output = self.sender("send")
        finally:
            self.proxy.faults.clear()
        for sequence in self.archived().keys() - before.keys():
            self.accepted_at[sequence] = self.portal.now
        self.check_last_delivery()
        return output

    def deliver_all(self, limit: int = 20) -> list[dict[str, Any]]:
        attempts = []
        for _ in range(limit):
            output = self.send()
            attempts.append(output["result"]["attempt"])
            if output["result"]["attempt"]["outcome"] == "empty":
                return attempts
        raise AssertionError("the Outbox never emptied")

    def checkpoint(self) -> dict[str, Any]:
        self.run_sequence += 1
        return self.sender("stage-checkpoint", runSequence=self.run_sequence)

    def tick(self, seconds: int = 60) -> datetime:
        self.portal.now += timedelta(seconds=seconds)
        return self.portal.now

    # The Portal

    def archived(self) -> dict[int, str]:
        if self.installation is None:
            return {}
        with self.portal.read() as view:
            return {sequence: sha256(envelope) for sequence, envelope in view.accepted_envelopes(self.installation)}

    def last_delivery(self) -> datetime | None:
        assert self.installation is not None
        with self.portal.read() as view:
            return view.installation(self.installation).last_delivery_at

    # The invariants (R8–R11)

    def receipt_crossed(self, response: bytes) -> None:
        """At the moment a receipt leaves the Portal, the store must already hold the exact message it names."""
        try:
            answer = json.loads(response)
        except ValueError:
            return
        if not isinstance(answer, dict) or answer.get("kind") != "delivery-receipt":
            return
        try:
            with self.portal.read() as view:
                envelopes = dict(view.accepted_envelopes(answer["sender_installation"]))
        except Exception as error:  # noqa: BLE001 - any failure to prove commit is a violation
            self.violations.append(f"receipt {answer.get('sequence')} could not be checked: {type(error).__name__}")
            return
        envelope = envelopes.get(answer["sequence"])
        if envelope is None or sha256(envelope) != answer["message_sha256"]:
            self.violations.append(f"receipt {answer['sequence']} preceded the Portal's commit")

    def receipts(self) -> tuple[dict[int, bytes], dict[int, list[bytes]]]:
        """Fully forwarded receipts by sequence, and every receipt the Portal produced, by sequence."""
        forwarded: dict[int, bytes] = {}
        produced: dict[int, list[bytes]] = {}
        for exchange in self.proxy.exchanges:
            if exchange["response"] is None:
                continue
            try:
                answer = json.loads(exchange["response"])
            except ValueError:
                continue
            if not isinstance(answer, dict) or answer.get("kind") != "delivery-receipt" or answer.get("sender_installation") != self.installation:
                continue
            produced.setdefault(answer["sequence"], []).append(exchange["response"])
            if exchange["fault"] == "pass":
                forwarded[answer["sequence"]] = exchange["response"]
        return forwarded, produced

    def check(self, output: dict[str, Any]) -> None:
        self.assertEqual(self.violations, [])
        held = {row["sequence"]: row["sha256"] for row in output["outbox"]}
        archived = self.archived()
        forwarded, produced = self.receipts()
        # Every receipt for a sequence is byte-identical, however many attempts produced it.
        for sequence, answers in produced.items():
            self.assertEqual(len(set(answers)), 1, f"sequence {sequence} got different receipts")
        for sequence, digest in self.staged.items():
            # Kept bytes never change, and archived bytes are the staged bytes.
            if sequence in held:
                self.assertEqual(held[sequence], digest, f"sequence {sequence} changed in the Outbox")
            if sequence in archived:
                self.assertEqual(archived[sequence], digest, f"sequence {sequence} archived different bytes")
            # Nothing is lost before its receipt, and nothing is removed before the Portal committed it,
            # or before its own receipt reached the Sender in full.
            if sequence not in held:
                self.assertIn(sequence, archived, f"sequence {sequence} left the Sender without being archived")
                self.assertIn(sequence, forwarded, f"sequence {sequence} left the Sender without its receipt")
        for sequence in archived:
            self.assertIn(sequence, self.staged, f"the Portal archived unknown sequence {sequence}")
        # Every request for a sequence carried identical bytes.
        bodies: dict[int, bytes] = {}
        for exchange in self.proxy.exchanges:
            try:
                message = json.loads(exchange["request"])
            except ValueError:
                continue
            if "sequence" in message and message.get("sender_installation") == self.installation:
                previous = bodies.setdefault(message["sequence"], exchange["request"])
                self.assertEqual(previous, exchange["request"], f"sequence {message['sequence']} was resent with different bytes")

    def check_last_delivery(self) -> None:
        if self.installation is None:
            return
        expected = max(self.accepted_at.values(), default=None)
        self.assertEqual(self.last_delivery(), expected)

    def responses(self, sequence: int) -> list[bytes]:
        found = []
        for exchange in self.proxy.exchanges:
            try:
                message = json.loads(exchange["request"])
            except ValueError:
                continue
            if message.get("sequence") == sequence and exchange["response"] is not None:
                found.append(exchange["response"])
        return found


class R1GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual(CONTRACT["scope"], "receipt-bound-delivery")
        self.assertEqual(CONTRACT["version"], "1")
        self.assertEqual(CONTRACT["kind"], "integration")
        self.assertIn("one durability meaning", CONTRACT["purpose"])
        self.assertEqual({item["name"]: item["mode"] for item in CONTRACT["inputs"]}, INPUT_MODES)
        self.assertEqual([output["name"] for output in CONTRACT["outputs"]], ["http_transport", "durability_evidence"])
        self.assertEqual(
            sorted(failure["name"] for failure in CONTRACT["named_failures"]),
            ["network-failed", "redirect-refused", "request-timed-out", "response-incomplete"],
        )
        self.assertEqual({failure["outcome"] for failure in CONTRACT["named_failures"]}, {"retryable"})
        self.assertEqual([effect["name"] for effect in CONTRACT["observable_effects"]], ["portal-http-request"])
        self.assertEqual(CONTRACT["dependencies"], [
            {"scope": scope, "version": "1"}
            for scope in ("send-outbox-message", "accept-ingest-batch", "accept-sync-completion", "accept-sync-checkpoint",
                          "accept-capability-acknowledgement", "manage-sender-enrollment")
        ])
        self.assertTrue(CONTRACT["authority"]["owns"])
        for owner in ("ingest-protocol", "stage-outbox-batch", "send-outbox-message", "accept-ingest-batch", "manage-sender-enrollment",
                      "Portal ingress shell", "Sender shell enabling task", "coordinate-sender-sync"):
            self.assertTrue(any(owner in entry for entry in CONTRACT["authority"]["does_not_own"]), owner)
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["python", "typescript"])
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `receipt-bound-delivery` |")]
        self.assertEqual(len(rows), 1)
        _, kind, purpose, inputs, outputs, depends_on = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(sorted(depends_on.split(", ")), sorted(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]))
        self.assertIn("one durability meaning for data and control messages", purpose)
        for phrase in ("Sender HTTP transport", "faulty network", "Portal ingress and Operational Store"):
            self.assertIn(phrase, inputs)
        for phrase in ("No loss before receipt", "stable identical retries", "no receipt before local archival",
                       "Last Delivery advanced from the Portal receipt time", "follows no redirect", "no response"):
            self.assertIn(phrase, outputs)


class R7EveryKind(Case):
    def test_every_kind_is_delivered_end_to_end(self) -> None:
        self.enroll()
        self.sender("stage-initial", recordType="steps", pages=[['{"synthetic":1}', '{"synthetic":2}'], ['{"synthetic":3}']])
        self.sender("stage-change", recordType="steps", items=[["projection", '{"synthetic":4}'], ["retraction", "synthetic-source-1"]])
        self.checkpoint()
        self.assertEqual(
            [self.kinds[sequence] for sequence in sorted(self.kinds)],
            ["capability-acknowledgement", "ingest-batch", "ingest-batch", "snapshot-completion", "ingest-batch",
             "change-completion", "sync-checkpoint"],
        )
        attempts = self.deliver_all()
        self.assertEqual(
            [attempt for attempt in attempts if attempt["outcome"] == "delivered"],
            [{"outcome": "delivered", "sequence": sequence, "kind": self.kinds[sequence]} for sequence in sorted(self.kinds)],
        )
        final = self.sender("status")
        self.assertEqual(final["outbox"], [])
        self.assertTrue(final["status"]["confirmed"])
        self.assertEqual(self.archived(), self.staged)


class R8NothingLost(Case):
    def test_pending_deliveries_stay_queued(self) -> None:
        self.enroll(confirm=False)
        for _ in range(3):
            attempt = self.send()["result"]["attempt"]
            self.assertEqual(attempt, {"outcome": "retryable", "sequence": 1, "failure": "temporarily-unavailable"})
        self.assertEqual(self.archived(), {})
        self.assertFalse(self.sender("status")["status"]["confirmed"])
        self.confirm()
        self.assertEqual(self.send()["result"]["attempt"], {"outcome": "delivered", "sequence": 1, "kind": "capability-acknowledgement"})
        self.assertTrue(self.sender("status")["status"]["confirmed"])

    def test_lost_requests_and_responses_keep_the_message(self) -> None:
        self.enroll()
        self.deliver_all()
        self.checkpoint()
        for fault in ("drop-request", "reset", "drop-response", "truncate-response"):
            output = self.send(fault)
            self.assertEqual(output["result"]["attempt"], {"outcome": "retryable", "sequence": 2, "failure": "transport-failed"}, fault)
            self.assertEqual([row["sequence"] for row in output["outbox"]], [2], fault)
        # The lost response came after the Portal committed it; the Sender still holds it until a receipt.
        self.assertIn(2, self.archived())
        self.assertEqual(self.send()["result"]["attempt"], {"outcome": "delivered", "sequence": 2, "kind": "sync-checkpoint"})
        self.assertEqual(self.sender("outbox")["outbox"], [])

    def test_a_refusing_store_keeps_the_message(self) -> None:
        self.enroll()
        self.deliver_all()
        self.checkpoint()
        self.portal.restart(reserve_bytes=10**18)
        for _ in range(2):
            attempt = self.send()["result"]["attempt"]
            self.assertEqual(attempt, {"outcome": "retryable", "sequence": 2, "failure": "storage-reserve-exhausted"})
        self.assertNotIn(2, self.archived())
        self.portal.restart()
        self.assertEqual(self.send()["result"]["attempt"], {"outcome": "delivered", "sequence": 2, "kind": "sync-checkpoint"})


class R9IdenticalRetries(Case):
    def test_retries_get_the_identical_receipt(self) -> None:
        self.enroll()
        self.deliver_all()
        self.sender("stage-initial", recordType="steps", pages=[['{"synthetic":1}']])
        self.send("drop-response")
        self.send("truncate-response")
        self.assertEqual(self.send()["result"]["attempt"]["outcome"], "delivered")
        receipts = self.responses(2)
        self.assertEqual(len(receipts), 3)
        self.assertEqual(len(set(receipts)), 1)
        self.assertEqual(json.loads(receipts[0])["kind"], "delivery-receipt")
        with self.portal.read() as view:
            self.assertEqual([sequence for sequence, _ in view.accepted_envelopes(self.installation)].count(2), 1)

    def test_a_portal_restart_keeps_the_receipt(self) -> None:
        self.enroll()
        self.deliver_all()
        self.checkpoint()
        self.send("drop-response")
        self.portal.restart()
        self.tick()
        self.assertEqual(self.send()["result"]["attempt"], {"outcome": "delivered", "sequence": 2, "kind": "sync-checkpoint"})
        receipts = self.responses(2)
        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[0], receipts[1])


class R11LastDelivery(Case):
    def test_last_delivery_follows_new_acceptances_only(self) -> None:
        self.enroll()
        accepted = self.tick()
        self.send()
        self.assertEqual(self.last_delivery(), accepted)
        # A lost response's retry, at a later Portal time, keeps the first acceptance's time.
        self.checkpoint()
        accepted = self.tick()
        self.send("drop-response")
        self.assertEqual(self.last_delivery(), accepted)
        self.tick()
        self.send()
        self.assertEqual(self.last_delivery(), accepted)
        # A retryable answer leaves it unchanged.
        self.sender("stage-initial", recordType="steps", pages=[['{"synthetic":5}']])
        self.portal.restart(reserve_bytes=10**18)
        self.tick()
        self.assertEqual(self.send()["result"]["attempt"]["outcome"], "retryable")
        self.assertEqual(self.last_delivery(), accepted)
        # New acceptances of data and control messages advance it.
        self.portal.restart()
        accepted = self.tick()
        self.assertEqual(self.send()["result"]["attempt"]["kind"], "ingest-batch")
        self.assertEqual(self.last_delivery(), accepted)
        accepted = self.tick()
        self.assertEqual(self.send()["result"]["attempt"]["kind"], "snapshot-completion")
        self.assertEqual(self.last_delivery(), accepted)
        self.sender("stage-change", recordType="steps", items=[["retraction", "synthetic-source-2"]])
        self.send()
        accepted = self.tick()
        self.assertEqual(self.send()["result"]["attempt"]["kind"], "change-completion")
        self.assertEqual(self.last_delivery(), accepted)
        # A Delivery Block leaves it unchanged.
        self.sender("stage-initial", recordType="heart-rate", version=2, pages=[['{"synthetic":6}']], complete=False)
        self.tick()
        self.assertEqual(self.send()["result"]["attempt"]["outcome"], "blocked")
        self.assertEqual(self.last_delivery(), accepted)


class R12BlockedHead(Case):
    def test_a_blocked_head_keeps_every_later_message(self) -> None:
        self.enroll()
        self.deliver_all()
        self.sender("stage-initial", recordType="heart-rate", version=2, pages=[['{"synthetic":7}']], complete=False)
        self.checkpoint()
        for _ in range(2):
            output = self.send()
            self.assertEqual(output["result"]["attempt"], {"outcome": "blocked", "sequence": 2, "failure": "unsupported-source-projection-schema"})
            self.assertEqual([row["sequence"] for row in output["outbox"]], [2, 3])
            self.assertEqual(output["result"]["headBlock"]["sequence"], 2)
        # Only the blocked head was ever sent, and the Portal recorded its block.
        sent = {json.loads(exchange["request"]).get("sequence") for exchange in self.proxy.exchanges}
        self.assertNotIn(3, sent)
        self.assertEqual(self.archived().keys(), {1})
        with self.portal.read() as view:
            blocks = view.delivery_blocks(self.installation)
        self.assertEqual([(block.sequence, block.failure) for block in blocks], [(2, "unsupported-source-projection-schema")])


class R13FaultedHistories(Case):
    def test_faulted_histories_keep_one_durability_meaning(self) -> None:
        required = (*FAULTS, "restart", "retried-lost-response", "capability-acknowledgement", "ingest-batch",
                    "snapshot-completion", "change-completion", "sync-checkpoint")
        for seed in (1, 2, 3):
            if seed > 1:
                self.tearDownCase()
                self.setUp()
            seen = self.history(seed)
            for event in required:
                self.assertIn(event, seen, f"seed {seed}: {event}")

    def tearDownCase(self) -> None:
        self.doCleanups()

    def history(self, seed: int) -> set[str]:
        rng = random.Random(seed)
        events: set[str] = set()
        lost: set[int] = set()

        def attempt(fault: str) -> None:
            output = self.send(fault)
            outcome = output["result"]["attempt"]
            events.add(fault)
            if outcome["outcome"] == "delivered":
                events.add(outcome["kind"])
                if outcome["sequence"] in lost:
                    events.add("retried-lost-response")
            elif fault == "drop-response" and outcome["outcome"] == "retryable":
                lost.add(outcome["sequence"])

        self.enroll()
        self.sender("stage-initial", recordType="steps", pages=[['{"synthetic":1}']])
        # A scripted prefix gives every seed each fault, a restart, and a retried lost response.
        for fault in ("pass", "drop-request", "reset", "truncate-response", "drop-response"):
            self.tick()
            attempt(fault)
        self.portal.restart()
        events.add("restart")
        attempt("pass")
        self.sender("stage-change", recordType="steps", items=[["retraction", f"synthetic-source-{seed}"]])
        self.checkpoint()
        for _ in range(30):
            self.tick(rng.randint(1, 600))
            action = rng.random()
            if action < 0.25:
                if rng.random() < 0.5:
                    self.checkpoint()
                else:
                    self.sender("stage-change", recordType="steps", items=[["projection", f'{{"synthetic":{rng.randint(1, 999)}}}']])
            elif action < 0.31:
                self.portal.restart()
                events.add("restart")
            else:
                attempt(rng.choice(FAULTS + ("pass", "pass")))
        # Drain what is left; its deliveries count, and the evidence still holds.
        for _ in range(60):
            if not self.sender("outbox")["outbox"]:
                break
            attempt("pass")
        self.assertEqual(self.sender("outbox")["outbox"], [])
        self.assertEqual(self.archived(), self.staged)
        return events


if __name__ == "__main__":
    unittest.main()
