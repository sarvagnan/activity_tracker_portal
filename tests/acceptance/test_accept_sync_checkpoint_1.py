"""Acceptance tests for `accept-sync-checkpoint@1`.

Requirement numbers refer to `packs/accept-sync-checkpoint-1.spec.md`. Every
database is a real `operational-store@2` database in a temporary directory,
every message is built with `ingest-protocol@1`, every Ingest Batch is archived
through `accept-ingest-batch@1`, and every value is synthetic. Assertions that
something wrote nothing compare the whole database, and every Delivery Block
is checked with its durable record.
"""

from __future__ import annotations

import base64
import collections
import contextlib
import json
import random
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

from activity_tracker_portal import ingest_protocol
from activity_tracker_portal.accept_ingest_batch import DeliveryResponse, accept_ingest_batch
from activity_tracker_portal.accept_sync_checkpoint import (
    PollingState,
    SchedulingReport,
    accept_sync_checkpoint,
    clock_conditions,
    polling_state,
)
from activity_tracker_portal.ingest_protocol import (
    DELIVERY_BLOCK_FAILURES,
    OUTCOMES,
    REASON_OPTIONAL,
    REASON_REQUIRED,
    REASONS,
    chain_start,
    encode_message,
    evaluate_delivery,
    message_sha256,
    new_delivery_state,
)
from activity_tracker_portal.operational_store import (
    ArchivedCheckpoint,
    MessageHeader,
    RecordTypeOutcome,
    StoreRefusal,
    StoreRetryable,
    SyncCheckpointRecord,
    open_store,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/accept-sync-checkpoint.1.json").read_text(encoding="utf-8"))

UTC = timezone.utc
# Whole milliseconds, so Sender times (millisecond precision) can sit exactly on a tolerance.
T0 = datetime(2026, 10, 16, 9, 0, 0, 123000, tzinfo=UTC)
US = timedelta(microseconds=1)
MS = timedelta(milliseconds=1)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
TOLERANCE = 5 * MINUTE
RESERVE = 1_000_000
PLENTY = 10**12
A = "00000000-0000-4000-8000-0000000000a1"
B = "00000000-0000-4000-8000-0000000000b2"
C = "00000000-0000-4000-8000-0000000000c3"
D = "00000000-0000-4000-8000-0000000000d4"
UNKNOWN = "00000000-0000-4000-8000-0000000000e5"
BLOCK_FAILURES = ("sequence-content-mismatch", "sequence-out-of-order", "unsupported-wire-protocol")
RECORD_TYPES = ("heart-rate", "sleep", "steps", "weight")
SCHEDULING_CONDITIONS = {
    "available": frozenset(),
    "restricted": frozenset({"background-scheduling-restricted"}),
    "unregistered": frozenset({"background-scheduling-unregistered"}),
}
EAST = timezone(timedelta(hours=5, minutes=30))
WEST = timezone(timedelta(hours=-7))


def cycle_id(number: int) -> str:
    return f"00000000-0000-4000-8000-{0xc000 + number:012x}"


def advertisement(generation: int, wire_versions: list[int] | None = None) -> dict[str, Any]:
    return {
        "wire_version": 1,
        "kind": "capability-advertisement",
        "generation": generation,
        "wire_versions": [1] if wire_versions is None else wire_versions,
        "projection_schemas": [{"record_type": "steps", "versions": [1]}],
        "max_record_count": 4,
        "max_message_bytes": 8192,
    }


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def when(value: float | datetime) -> datetime:
    return value if isinstance(value, datetime) else at(value)


def stamp(value: datetime) -> str:
    """A Sender time in the protocol's millisecond form."""
    value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def type_status(record_type: str, outcome: str, staged: bool = False, reason: str | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"record_type": record_type, "outcome": outcome, "source_data_staged": staged}
    if reason is not None:
        value["reason"] = reason
    return value


DEFAULT_TYPES = [type_status("steps", "completed", True), type_status("weight", "no-change")]
OMIT = object()


def checkpoint(
    sequence: int, *, generation: int = 1, installation: str = A, run: int = 1, trigger: str = "background",
    started: datetime | str | None = None, ended: datetime | str = T0 + 30_000 * MS, elapsed: Any = OMIT,
    scheduling: str = "available", types: list[dict[str, Any]] | None = None, outbox: tuple[int, int] = (0, 0),
) -> bytes:
    """A Sync Checkpoint; by default the run lasted 30 seconds, ending 30 seconds after T0."""
    if started is None:
        assert isinstance(ended, datetime)
        started = ended - 30_000 * MS
    value: dict[str, Any] = {
        "wire_version": 1, "kind": "sync-checkpoint", "sender_installation": installation, "sequence": sequence,
        "capability_generation": generation, "run_sequence": run, "trigger": trigger,
        "started_at": started if isinstance(started, str) else stamp(started),
        "ended_at": ended if isinstance(ended, str) else stamp(ended),
        "background_scheduling": scheduling, "record_types": DEFAULT_TYPES if types is None else types,
        "outbox_messages_before_checkpoint": outbox[0], "outbox_bytes_before_checkpoint": outbox[1],
    }
    if elapsed is OMIT:
        elapsed = 30_000
    if elapsed is not None:
        value["elapsed_ms"] = elapsed
    return encode_message(value)


def record_of(data: bytes) -> SyncCheckpointRecord:
    """The store's exact checkpoint record for a checkpoint's bytes."""
    message = json.loads(data)
    return SyncCheckpointRecord(
        run_sequence=message["run_sequence"], trigger=message["trigger"], started_at=message["started_at"],
        ended_at=message["ended_at"], elapsed_ms=message.get("elapsed_ms"),
        background_scheduling=message["background_scheduling"],
        record_types=tuple(
            RecordTypeOutcome(item["record_type"], item["outcome"], item.get("reason"), item["source_data_staged"])
            for item in message["record_types"]
        ),
        outbox_messages_before_checkpoint=message["outbox_messages_before_checkpoint"],
        outbox_bytes_before_checkpoint=message["outbox_bytes_before_checkpoint"],
    )


EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def days_from_civil(year: int, month: int, day: int) -> int:
    """Days from 1970-01-01 in the proleptic Gregorian calendar, where year 0000 is a leap year."""
    year -= month <= 2
    era = year // 400
    year_of_era = year - era * 400
    day_of_year = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    return era * 146097 + year_of_era * 365 + year_of_era // 4 - year_of_era // 100 + day_of_year - 719468


def sender_microseconds(value: str) -> int:
    """A Sender time as microseconds from 1970, for any protocol year from 0000, which datetime cannot hold."""
    days = days_from_civil(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    seconds = days * 86400 + int(value[11:13]) * 3600 + int(value[14:16]) * 60 + int(value[17:19])
    return seconds * 1_000_000 + int(value[20:23]) * 1000


def portal_microseconds(value: datetime) -> int:
    return (value - EPOCH) // US


def expected_clock_conditions(item: ArchivedCheckpoint, enrolled_at: datetime) -> frozenset[str]:
    """R10's three rules, with the five-minute tolerance."""
    started, ended = sender_microseconds(item.record.started_at), sender_microseconds(item.record.ended_at)
    tolerance = TOLERANCE // US
    conditions = set()
    if ended - portal_microseconds(item.receipt_time) > tolerance:
        conditions.add("sender-clock-ahead")
    if portal_microseconds(enrolled_at) - ended > tolerance:
        conditions.add("sender-clock-behind")
    if item.record.elapsed_ms is None:
        if started - ended > tolerance:
            conditions.add("sender-times-inconsistent")
    elif abs((ended - started) - item.record.elapsed_ms * 1000) > tolerance:
        conditions.add("sender-times-inconsistent")
    return frozenset(conditions)


def batch(sequence: int, cycle: str, generation: int = 1, installation: str = A) -> bytes:
    return encode_message({
        "wire_version": 1, "kind": "ingest-batch", "sender_installation": installation, "sequence": sequence,
        "capability_generation": generation, "record_type": "steps", "projection_schema_version": 1,
        "cycle_kind": "change-cycle", "cycle_id": cycle, "source_page_index": 1, "fragment_index": 1,
        "previous_chain": chain_start(cycle),
        "items": [{"kind": "projection", "data": base64.b64encode(b"synthetic-%d" % sequence).decode("ascii")}],
    })


def acknowledgement(sequence: int, generation: int = 1, installation: str = A) -> bytes:
    return encode_message({
        "wire_version": 1, "kind": "capability-acknowledgement", "sender_installation": installation,
        "sequence": sequence, "capability_generation": generation,
    })


def receipt_for(data: bytes) -> bytes:
    message = ingest_protocol.decode_message(data)
    return encode_message({
        "wire_version": 1, "kind": "delivery-receipt", "sender_installation": message["sender_installation"],
        "sequence": message["sequence"], "message_sha256": message_sha256(data),
    })


def contents(path: Path) -> dict[str, object]:
    """The whole database: schema version, every schema object, and every row of every table, BLOBs included."""
    connection = sqlite3.connect(path)
    try:
        schema = connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        tables: dict[str, list[tuple[object, ...]]] = {}
        for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
            rows = connection.execute(f'SELECT * FROM "{name}"').fetchall()
            tables[name] = sorted(rows, key=lambda row: [(value is None, type(value).__name__, 0 if value is None else value) for value in row])
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()
    return {"user_version": user_version, "schema": schema, "tables": tables}


def without_blocks(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {**snapshot, "tables": {name: rows for name, rows in snapshot["tables"].items() if name != "delivery_blocks"}}


def copy_database(source: Path, target: Path) -> None:
    with contextlib.closing(sqlite3.connect(source)) as origin, contextlib.closing(sqlite3.connect(target)) as copy:
        origin.backup(copy)


class FreeSpace:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self, _path: object) -> int:
        return self.value


class BusyAt:
    """The real store, except that one operation fails as a busy store would."""

    def __init__(self, store: Any, operation: str) -> None:
        self._store, self._operation = store, operation

    def __getattr__(self, name: str) -> Any:
        if name == self._operation:
            def busy(*_args: Any, **_kwargs: Any) -> Any:
                raise StoreRetryable("store-busy")
            return busy
        return getattr(self._store, name)


class RefusesArchive:
    """The real store, except that archival refuses as the store's own rechecks would."""

    def __init__(self, store: Any, failure: str) -> None:
        self._store, self._failure = store, failure

    def __getattr__(self, name: str) -> Any:
        if name == "archive_sync_checkpoint":
            def refuse(*_args: Any, **_kwargs: Any) -> Any:
                raise StoreRefusal(self._failure)
            return refuse
        return getattr(self._store, name)


class RetiresBeforeArchive:
    """The real store, except that another writer retires the installation just before archival."""

    def __init__(self, store: Any, moment: datetime) -> None:
        self._store, self._moment = store, moment

    def __getattr__(self, name: str) -> Any:
        if name == "archive_sync_checkpoint":
            def archive(header: MessageHeader, record: SyncCheckpointRecord) -> bytes:
                self._store.retire_installation(header.installation_id, "destructive", self._moment)
                return self._store.archive_sync_checkpoint(header, record)
            return archive
        return getattr(self._store, name)


class NoOffset(tzinfo):
    """A timezone that is attached but reports no UTC offset."""

    def utcoffset(self, _value: datetime | None) -> None:
        return None

    def dst(self, _value: datetime | None) -> None:
        return None

    def tzname(self, _value: datetime | None) -> str:
        return "synthetic-no-offset"


class Untouchable:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"store used: {name}")


INVALID_TIMES = (datetime(2026, 10, 16, 9, 0), datetime(2026, 10, 16, 9, 0, tzinfo=NoOffset()), "2026-10-16T09:00:00Z", None)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "portal.sqlite3"
        self.free = FreeSpace(PLENTY)
        self.store = self.open(self.path)
        for generation in (1, 2):
            self.store.honor_generation(generation, encode_message(advertisement(generation)), T0)
        self.cycles = 0

    def open(self, path: Path):
        store = open_store(path, reserve_bytes=RESERVE, free_bytes=self.free, busy_timeout_ms=100)
        self.addCleanup(store.close)
        return store

    def enroll(self, installation: str, minute: float | datetime = 0, store=None) -> None:
        store = store or self.store
        code = message_sha256(b"synthetic-enrollment-code-" + installation.encode())
        store.record_enrollment_offer(code, when(minute) + 30 * MINUTE, when(minute))
        store.enroll_installation(code, installation, ingest_protocol.installation_fingerprint(installation), when(minute))

    def activate(self, installation: str = A, minute: float | datetime = 0, store=None) -> None:
        store = store or self.store
        self.enroll(installation, minute, store)
        store.activate_installation(installation, when(minute))

    def accept(self, data: bytes, minute: float | datetime = 1) -> DeliveryResponse:
        return accept_sync_checkpoint(self.store, data, when(minute))

    def poll(self, moment: float | datetime, installation: str = A) -> PollingState:
        return polling_state(self.store, installation, when(moment))

    def send_batch(self, sequence: int, minute: float | datetime = 0.5, installation: str = A) -> bytes:
        """Archive an Ingest Batch through accept-ingest-batch@1."""
        self.cycles += 1
        data = batch(sequence, cycle_id(self.cycles), installation=installation)
        response = accept_ingest_batch(self.store, data, when(minute))
        self.assertEqual(response.outcome, "receipt", response)
        return data

    def acknowledge(self, sequence: int, generation: int, minute: float = 0.5) -> None:
        """Archive a capability acknowledgement through operational-store@2, as its own accept unit would."""
        data = acknowledgement(sequence, generation)
        self.store.archive_capability_acknowledgement(MessageHeader(
            installation_id=A, sequence=sequence, message_sha256=message_sha256(data), staged_generation=generation,
            receipt_time=at(minute), envelope=data, receipt=receipt_for(data),
        ))

    def latest(self, installation: str = A) -> ArchivedCheckpoint:
        with self.store.read() as view:
            (item,) = view.sync_checkpoints(installation, limit=1)
        return item

    def assertReceipt(self, response: DeliveryResponse, data: bytes, gap_after: int | None = None) -> None:
        self.assertEqual(response, DeliveryResponse("receipt", receipt_for(data), None, gap_after))

    def assertRetryable(self, response: DeliveryResponse, reason: str, failure: str) -> None:
        body = encode_message({"wire_version": 1, "kind": "retryable-failure", "reason": reason})
        self.assertEqual(response, DeliveryResponse("retryable", body, failure, None))

    def block_index(self) -> dict[tuple[object, ...], Any]:
        with self.store.read() as view:
            return {(r.installation_id, r.sequence, r.message_sha256, r.failure): r for r in view.delivery_blocks()}

    def deliverBlocked(self, data: bytes, failure: str, minute: float | datetime = 1, store: Any = None) -> None:
        """Deliver a checkpoint that must be blocked; require the durable record and no other write."""
        message = ingest_protocol.decode_message(data)
        before, records_before = contents(self.path), self.block_index()
        body = encode_message({
            "wire_version": 1, "kind": "delivery-block", "failure": failure, "message_sha256": message_sha256(data),
            "sender_installation": message["sender_installation"], "sequence": message["sequence"],
        })
        response = accept_sync_checkpoint(store or self.store, data, when(minute))
        self.assertEqual(response, DeliveryResponse("block", body, failure, None))
        self.assertEqual(without_blocks(contents(self.path)), without_blocks(before))
        key = (message["sender_installation"], message["sequence"], message_sha256(data), failure)
        records = self.block_index()
        prior = records_before.get(key)
        self.assertEqual(
            (records[key].count, records[key].first_seen_at, records[key].last_seen_at),
            ((prior.count if prior else 0) + 1, prior.first_seen_at if prior else when(minute), when(minute)),
        )
        self.assertEqual({k: v for k, v in records.items() if k != key}, {k: v for k, v in records_before.items() if k != key})


# R1 — Governed Scope contract


class R1GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual((CONTRACT["scope"], CONTRACT["version"], CONTRACT["kind"]), ("accept-sync-checkpoint", "1", "leaf"))
        self.assertTrue(CONTRACT["purpose"])
        self.assertEqual(
            {item["name"]: item["mode"] for item in CONTRACT["inputs"]},
            {"delivered_bytes": "pure", "receipt_time": "pure", "installation_id": "pure", "read_time": "pure",
             "operational_state": "effectful", "free_space": "volatile"},
        )
        self.assertEqual({output["name"] for output in CONTRACT["outputs"]},
                         {"delivery_response", "archived_checkpoint", "delivery_block_record", "polling_state"})
        by_outcome: dict[str, set[str]] = {}
        for failure in CONTRACT["named_failures"]:
            by_outcome.setdefault(failure["outcome"], set()).add(failure["name"])
        self.assertEqual(len(CONTRACT["named_failures"]), 8)
        self.assertEqual(by_outcome["delivery-block"], set(BLOCK_FAILURES))
        self.assertLessEqual(by_outcome["delivery-block"], DELIVERY_BLOCK_FAILURES)
        self.assertEqual(by_outcome["retryable"], {
            "installation-pending", "installation-ended", "installation-unknown", "storage-reserve-exhausted", "store-busy",
        })
        self.assertEqual([effect["name"] for effect in CONTRACT["observable_effects"]], ["operational-store-write"])
        pairs = [(dependency["scope"], dependency["version"]) for dependency in CONTRACT["dependencies"]]
        self.assertEqual(len(pairs), len(set(pairs)))
        self.assertEqual(set(pairs), {("ingest-protocol", "1"), ("accept-ingest-batch", "1"),
                                      ("manage-sender-installation", "1"), ("operational-store", "2")})
        for owner in ("ingest-protocol", "operational-store", "manage-sender-installation", "accept-ingest-batch",
                      "ingress shell", "portal-state-presentation", "stage-sync-checkpoint"):
            self.assertTrue(any(owner in entry for entry in CONTRACT["authority"]["does_not_own"]), owner)
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["python"])
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `accept-sync-checkpoint` |")]
        self.assertEqual(len(rows), 1)
        _, kind, purpose, _inputs, outputs, depends_on = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(sorted(depends_on.split(", ")), sorted(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]))
        self.assertIn("without trusting its wall clock", purpose)
        self.assertIn("from receipt time", outputs)


# R2 — Caller errors


class R2CallerErrors(Case):
    def test_invalid_time_is_a_caller_error(self) -> None:
        item = ArchivedCheckpoint(1, T0, record_of(checkpoint(1)))
        for now in INVALID_TIMES:
            with self.assertRaises(ValueError):
                accept_sync_checkpoint(Untouchable(), checkpoint(1), now)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                polling_state(Untouchable(), A, now)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                clock_conditions(item, now)  # type: ignore[arg-type]

    def test_other_bytes_are_caller_errors(self) -> None:
        def raw(value: dict[str, Any]) -> bytes:
            return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

        valid = json.loads(checkpoint(1))
        others = [
            b"synthetic bytes that are not json",
            json.dumps(valid, indent=1).encode(),
            raw({**valid, "trigger": "scheduled"}),
            raw({**valid, "background_scheduling": "denied"}),
            raw({**valid, "record_types": [{"record_type": "steps", "outcome": "deferred", "source_data_staged": False}]}),
            raw({**valid, "record_types": list(reversed(valid["record_types"]))}),
            raw({**valid, "started_at": "2026-02-30T09:00:00.000Z"}),
            raw({**valid, "ended_at": "2026-10-16T09:00:30Z"}),
            raw({**valid, "elapsed_ms": -1}),
            raw({**valid, "portal_receipt_time": "2026-10-16T09:00:30.000Z"}),
            batch(1, cycle_id(1)),
            acknowledgement(1),
            encode_message({
                "wire_version": 1, "kind": "snapshot-completion", "sender_installation": A, "sequence": 1,
                "capability_generation": 1, "snapshot_kind": "initial", "record_type": "steps", "cycle_id": cycle_id(1),
                "page_count": 0, "fragment_count": 0, "record_count": 0, "final_chain": chain_start(cycle_id(1)),
            }),
            encode_message({
                "wire_version": 1, "kind": "change-completion", "sender_installation": A, "sequence": 1,
                "capability_generation": 1, "record_type": "steps", "cycle_id": cycle_id(1),
                "page_count": 0, "fragment_count": 0, "record_count": 0, "final_chain": chain_start(cycle_id(1)),
            }),
            encode_message(advertisement(1)),
            receipt_for(checkpoint(1)),
        ]
        for data in others:
            with self.assertRaises(ValueError, msg=data[:80]):
                accept_sync_checkpoint(Untouchable(), data, T0)  # type: ignore[arg-type]


# R3 — Identity and retry


class R3IdentityAndRetry(Case):
    def test_identical_retry_returns_the_archived_receipt(self) -> None:
        self.activate()
        data = checkpoint(1, generation=1)
        first = self.accept(data, minute=1)
        self.assertReceipt(first, data)
        before = contents(self.path)
        self.assertEqual(self.accept(data, minute=7), first)
        self.assertEqual(contents(self.path), before)
        # Still answered after its generation stops, the installation retires, and the reserve runs out.
        self.acknowledge(2, generation=2, minute=8)
        self.store.stop_honoring_generation(1, at(9))
        self.store.begin_draining(A, at(10))
        self.store.retire_installation(A, "destructive", at(11))
        self.free.value = RESERVE - 1
        before = contents(self.path)
        self.assertEqual(self.accept(data, minute=12), first)
        self.assertEqual(contents(self.path), before)

    def test_conflict_after_retirement_is_still_blocked(self) -> None:
        self.activate()
        self.accept(checkpoint(1))
        self.store.begin_draining(A, at(2))
        self.store.retire_installation(A, "drained", at(3))
        for minute in (4, 8):
            self.deliverBlocked(checkpoint(1, run=2), "sequence-content-mismatch", minute=minute)
        with self.store.read() as view:
            self.assertEqual([(r.first_seen_at, r.last_seen_at, r.count) for r in view.delivery_blocks()], [(at(4), at(8), 2)])

    def test_same_sequence_different_bytes_is_blocked(self) -> None:
        self.activate()
        self.accept(checkpoint(1))
        for minute in (2, 5, 9):
            self.deliverBlocked(checkpoint(1, scheduling="restricted"), "sequence-content-mismatch", minute=minute)
        # Identity is checked before the generation.
        self.deliverBlocked(checkpoint(1, generation=3), "sequence-content-mismatch", minute=10)
        # A checkpoint at the sequence of an archived Ingest Batch conflicts too.
        self.send_batch(2, minute=10)
        self.deliverBlocked(checkpoint(2), "sequence-content-mismatch", minute=11)
        with self.store.read() as view:
            self.assertEqual(
                sorted((r.sequence, r.first_seen_at, r.last_seen_at, r.count) for r in view.delivery_blocks()),
                [(1, at(2), at(9), 3), (1, at(10), at(10), 1), (2, at(11), at(11), 1)],
            )


# R4 — Installation state


class R4InstallationState(Case):
    def test_non_delivering_installations_are_retryable(self) -> None:
        # Installation state is checked before sequence order and the generation.
        self.enroll(A)
        before = contents(self.path)
        for data in (checkpoint(1), checkpoint(1, generation=3)):
            self.assertRetryable(self.accept(data), "temporarily-unavailable", "installation-pending")
        for data in (checkpoint(1, installation=UNKNOWN), checkpoint(1, generation=3, installation=UNKNOWN)):
            self.assertRetryable(self.accept(data), "temporarily-unavailable", "installation-unknown")
        self.assertEqual(contents(self.path), before)
        self.store.withdraw_installation(A, at(2))
        self.activate(B, minute=3)
        self.assertReceipt(self.accept(checkpoint(3, installation=B, ended=at(3.5)), minute=3.5),
                           checkpoint(3, installation=B, ended=at(3.5)), gap_after=0)
        self.store.begin_draining(B, at(4))
        self.store.retire_installation(B, "drained", at(5))
        before = contents(self.path)
        for data in (checkpoint(1, installation=A), checkpoint(1, generation=3, installation=A),
                     checkpoint(4, installation=B), checkpoint(2, installation=B), checkpoint(4, generation=3, installation=B)):
            self.assertRetryable(self.accept(data, minute=6), "temporarily-unavailable", "installation-ended")
        self.assertEqual(contents(self.path), before)

    def test_draining_installation_still_delivers(self) -> None:
        self.activate()
        self.store.begin_draining(A, at(1))
        data = checkpoint(1, generation=2)
        self.assertReceipt(self.accept(data, minute=2), data)
        self.assertEqual(self.poll(at(3)).latest_checkpoint, ArchivedCheckpoint(1, at(2), record_of(data)))


# R5 — Sequence order and gaps


class R5SequenceOrderAndGaps(Case):
    def test_older_unseen_sequence_is_blocked(self) -> None:
        self.activate()
        self.accept(checkpoint(1))
        self.accept(checkpoint(4, generation=2))
        self.deliverBlocked(checkpoint(2, generation=2), "sequence-out-of-order", minute=2)
        self.deliverBlocked(checkpoint(3), "sequence-out-of-order", minute=3)
        # Sequence order is checked before the generation.
        self.deliverBlocked(checkpoint(3, generation=3), "sequence-out-of-order", minute=4)

    def test_gap_is_accepted_and_reported(self) -> None:
        self.activate()
        first = checkpoint(2)
        self.assertReceipt(self.accept(first), first, gap_after=0)
        later = checkpoint(5, generation=2)
        self.assertReceipt(self.accept(later), later, gap_after=2)
        following = checkpoint(6)
        self.assertReceipt(self.accept(following), following)
        with self.store.read() as view:
            self.assertEqual([(gap.gap_after, gap.sequence) for gap in view.sequence_gaps(A)], [(0, 2), (2, 5)])


# R6 — Generation and version


class R6GenerationAndVersion(Case):
    def test_unhonored_generation_is_unsupported(self) -> None:
        self.activate()
        self.deliverBlocked(checkpoint(1, generation=3), "unsupported-wire-protocol")
        # A generation that stopped being honored is unsupported for a new checkpoint.
        self.acknowledge(1, generation=2, minute=2)
        self.store.stop_honoring_generation(1, at(3))
        self.deliverBlocked(checkpoint(2, generation=1), "unsupported-wire-protocol", minute=4)
        # A valid advertisement may list only a later Wire Protocol Version.
        self.store.honor_generation(3, encode_message(advertisement(3, wire_versions=[2])), at(5))
        self.deliverBlocked(checkpoint(2, generation=3), "unsupported-wire-protocol", minute=6)
        self.assertReceipt(self.accept(checkpoint(2, generation=2), minute=7), checkpoint(2, generation=2))


# R7 — Archival


class R7Archival(Case):
    def test_accepted_checkpoint_is_archived_exactly(self) -> None:
        self.activate()
        types = [
            type_status("heart-rate", "progressed", True, "time-budget-reached"),
            type_status("sleep", "deferred", False, "collection-disabled"),
            type_status("steps", "completed", True),
            type_status("weight", "not-permitted"),
        ]
        data = checkpoint(1, generation=2, run=7, trigger="manual", started="2026-10-16T08:58:00.250Z",
                          ended="2026-10-16T08:59:59.999Z", elapsed=119_749, scheduling="restricted",
                          types=types, outbox=(3, 4096))
        expected = SyncCheckpointRecord(
            7, "manual", "2026-10-16T08:58:00.250Z", "2026-10-16T08:59:59.999Z", 119_749, "restricted",
            (RecordTypeOutcome("heart-rate", "progressed", "time-budget-reached", True),
             RecordTypeOutcome("sleep", "deferred", "collection-disabled", False),
             RecordTypeOutcome("steps", "completed", None, True),
             RecordTypeOutcome("weight", "not-permitted", None, False)),
            3, 4096,
        )
        response = self.accept(data, minute=7)
        self.assertReceipt(response, data)
        with self.store.read() as view:
            archived = view.archived_message(A, 1)
            status = view.installation(A)
            history = view.sync_checkpoints(A)
        assert archived is not None
        self.assertEqual(
            (archived.kind, archived.envelope, archived.receipt, archived.receipt_time, archived.staged_generation,
             archived.message_sha256, archived.batch, archived.completion, archived.checkpoint),
            ("sync-checkpoint", data, response.body, at(7), 2, message_sha256(data), None, None, expected),
        )
        self.assertEqual(history, (ArchivedCheckpoint(1, at(7), expected),))
        self.assertEqual(
            (status.last_delivery_at, status.latest_checkpoint_receipt_at, status.background_scheduling,
             status.background_scheduling_receipt_at, status.highest_sequence),
            (at(7), at(7), "restricted", at(7), 1),
        )
        # No elapsed duration, an optional reason left out or given, an empty report, and a non-UTC receipt time.
        second = checkpoint(2, trigger="foreground", elapsed=None, scheduling="unregistered", types=[
            type_status("steps", "interrupted", True, "screen-lock-removed"), type_status("weight", "progressed", True),
        ])
        third = checkpoint(3, types=[])
        self.assertReceipt(self.accept(second, minute=9), second)
        self.assertReceipt(self.accept(third, at(11).astimezone(EAST)), third)
        with self.store.read() as view:
            self.assertEqual(view.sync_checkpoints(A), (
                ArchivedCheckpoint(3, at(11), SyncCheckpointRecord(
                    1, "background", stamp(T0), stamp(T0 + 30_000 * MS), 30_000, "available", (), 0, 0)),
                ArchivedCheckpoint(2, at(9), SyncCheckpointRecord(
                    1, "foreground", stamp(T0), stamp(T0 + 30_000 * MS), None, "unregistered",
                    (RecordTypeOutcome("steps", "interrupted", "screen-lock-removed", True),
                     RecordTypeOutcome("weight", "progressed", None, True)), 0, 0)),
                ArchivedCheckpoint(1, at(7), expected),
            ))
            status = view.installation(A)
        self.assertEqual(
            (status.last_delivery_at, status.latest_checkpoint_receipt_at, status.background_scheduling,
             status.background_scheduling_receipt_at),
            (at(11), at(11), "available", at(11)),
        )

    def test_implausible_sender_times_are_accepted(self) -> None:
        self.activate(minute=0)
        implausible = [
            checkpoint(1, started=at(2), ended=at(-58), elapsed=1000),           # reversed
            checkpoint(2, ended=at(3) + 2 * DAY),                               # in the future
            checkpoint(3, ended="2020-01-01T00:00:00.000Z", started="2020-01-01T00:00:00.000Z"),  # before enrollment
            checkpoint(4, elapsed=9 * DAY // MS),                               # elapsed disagrees
            checkpoint(5, started="1970-01-01T00:00:00.000Z", ended="9999-12-31T23:59:59.999Z", elapsed=None),
            checkpoint(6, started="0000-02-29T00:00:00.000Z", ended="0000-02-29T00:00:30.000Z"),  # the protocol's first year
        ]
        for minute, data in enumerate(implausible, start=1):
            self.assertReceipt(self.accept(data, minute=minute), data)
            state = self.poll(at(minute))
            self.assertEqual(state.freshness, "current")
            self.assertEqual(state.latest_checkpoint, ArchivedCheckpoint(minute, at(minute), record_of(data)))
            self.assertTrue(state.conditions, minute)

    def test_acceptance_changes_no_other_state(self) -> None:
        """The unit's only write is the store's own archival of the exact header and record."""
        self.activate()
        self.send_batch(1, minute=1)
        self.acknowledge(2, generation=2, minute=2)
        self.enroll(B, minute=3)
        self.deliverBlocked(checkpoint(1), "sequence-content-mismatch", minute=4)
        cases = (
            (checkpoint(3, scheduling="restricted"), 5, None),
            (checkpoint(6, generation=2, elapsed=None), 6, 3),
            (checkpoint(7, scheduling="unregistered", ended=at(9) + DAY), 8, None),
        )
        for number, (data, minute, gap_after) in enumerate(cases):
            if number == 2:
                self.store.begin_draining(A, at(7))
            twin = self.directory / f"twin-{number}.sqlite3"
            copy_database(self.path, twin)
            message = ingest_protocol.decode_message(data)
            with open_store(twin, reserve_bytes=RESERVE, free_bytes=self.free, busy_timeout_ms=100) as twin_store:
                twin_store.archive_sync_checkpoint(MessageHeader(
                    installation_id=A, sequence=message["sequence"], message_sha256=message_sha256(data),
                    staged_generation=message["capability_generation"], receipt_time=at(minute), envelope=data,
                    receipt=receipt_for(data),
                ), record_of(data))
            self.assertReceipt(self.accept(data, minute=minute), data, gap_after)
            self.assertEqual(contents(self.path), contents(twin), number)


# R8 — Polling Freshness


class R8PollingFreshness(Case):
    def test_never_reported_until_the_first_checkpoint(self) -> None:
        self.enroll(A)
        nothing = PollingState(A, "never-reported", None, False, False, None, frozenset())
        self.assertEqual(self.poll(at(1)), nothing)
        self.store.activate_installation(A, at(2))
        self.assertEqual(self.poll(at(3)), nothing)
        self.send_batch(1, minute=4)
        self.acknowledge(2, generation=2, minute=5)
        self.assertEqual(self.poll(at(6)), PollingState(A, "never-reported", None, True, False, None, frozenset()))
        # Neither a blocked nor a retryable checkpoint reports.
        self.deliverBlocked(checkpoint(1), "sequence-content-mismatch", minute=7)
        self.free.value = RESERVE - 1
        self.assertRetryable(self.accept(checkpoint(3), minute=8), "storage-reserve-exhausted", "storage-reserve-exhausted")
        self.free.value = PLENTY
        self.assertEqual(self.poll(at(9)), PollingState(A, "never-reported", None, True, False, None, frozenset()))
        data = checkpoint(3)
        self.accept(data, minute=10)
        self.assertEqual(self.poll(at(11)), PollingState(
            A, "current", ArchivedCheckpoint(3, at(10), record_of(data)), False, False,
            SchedulingReport("available", at(10), False), frozenset(),
        ))

    def test_freshness_boundaries(self) -> None:
        self.activate()
        receipt = at(10)
        self.accept(checkpoint(1), receipt)
        for offset, freshness in (
            (-7 * HOUR, "current"), (-HOUR, "current"), (timedelta(0), "current"), (6 * HOUR - US, "current"), (6 * HOUR, "current"),
            (6 * HOUR + US, "delayed"), (24 * HOUR, "delayed"), (24 * HOUR + US, "stale"), (10 * DAY, "stale"),
        ):
            self.assertEqual(self.poll(receipt + offset).freshness, freshness, offset)
        # Only the instant matters, not the read time's offset.
        self.assertEqual(self.poll((receipt + 6 * HOUR).astimezone(WEST)).freshness, "current")
        self.assertEqual(self.poll((receipt + 6 * HOUR + US).astimezone(EAST)).freshness, "delayed")
        # Only a checkpoint refreshes freshness: a later Ingest Batch does not.
        self.send_batch(2, receipt + 20 * HOUR)
        self.assertEqual(self.poll(receipt + 24 * HOUR + US).freshness, "stale")
        self.accept(checkpoint(3), receipt + 30 * HOUR)
        self.assertEqual(self.poll(receipt + 36 * HOUR).freshness, "current")
        self.assertEqual(self.poll(receipt + 36 * HOUR + US).freshness, "delayed")
        # The latest checkpoint is the one with the highest sequence, even when Portal time steps back.
        stepped_back = checkpoint(4)
        self.accept(stepped_back, receipt + 29 * HOUR)
        state = self.poll(receipt + 35 * HOUR + US)
        self.assertEqual((state.freshness, state.latest_checkpoint),
                         ("delayed", ArchivedCheckpoint(4, receipt + 29 * HOUR, record_of(stepped_back))))

    def test_first_poll_in_progress_and_overdue(self) -> None:
        self.enroll(A, minute=0)
        # A pending installation is never overdue, however long ago it enrolled.
        self.assertEqual(self.poll(at(0) + 100 * HOUR), PollingState(A, "never-reported", None, False, False, None, frozenset()))
        activation = at(0) + 10 * HOUR
        self.store.activate_installation(A, activation)
        # The deadline runs from activation, not enrollment.
        for offset, overdue in ((timedelta(0), False), (24 * HOUR, False), (24 * HOUR + US, True), (100 * HOUR, True)):
            state = self.poll(activation + offset)
            self.assertEqual((state.freshness, state.first_poll_in_progress, state.first_checkpoint_overdue),
                             ("never-reported", False, overdue), offset)
        self.send_batch(1, activation + 25 * HOUR)
        state = self.poll(activation + 26 * HOUR)
        self.assertEqual((state.freshness, state.first_poll_in_progress, state.first_checkpoint_overdue),
                         ("never-reported", True, True))
        # Both are unset once a checkpoint exists, even a stale one.
        self.accept(checkpoint(2, ended=activation + 26 * HOUR), activation + 27 * HOUR)
        state = self.poll(activation + 27 * HOUR + 2 * DAY)
        self.assertEqual((state.freshness, state.first_poll_in_progress, state.first_checkpoint_overdue), ("stale", False, False))
        # A withdrawn installation was never activated, so it is never overdue.
        self.enroll(B, activation + 28 * HOUR)
        self.store.withdraw_installation(B, activation + 29 * HOUR)
        self.assertEqual(self.poll(activation + 300 * HOUR, B),
                         PollingState(B, "never-reported", None, False, False, None, frozenset()))
        # An installation that never reported stays overdue after it drains and retires.
        self.store.retire_installation(A, "destructive", activation + 30 * HOUR)
        replacement = activation + 31 * HOUR
        self.activate(C, replacement)
        self.store.begin_draining(C, replacement + 25 * HOUR)
        draining = self.poll(replacement + 26 * HOUR, C)
        self.store.retire_installation(C, "drained", replacement + 27 * HOUR)
        retired = self.poll(replacement + 28 * HOUR, C)
        for state in (draining, retired):
            self.assertEqual(state, PollingState(C, "never-reported", None, False, True, None, frozenset()))

    def test_freshness_is_scoped_to_one_installation(self) -> None:
        self.activate(A)
        self.accept(checkpoint(1, scheduling="restricted"), minute=1)
        self.store.begin_draining(A, at(2))
        last = checkpoint(2, scheduling="unregistered")
        self.accept(last, minute=3)
        self.store.retire_installation(A, "drained", at(4))
        self.activate(B, minute=5)
        # The replacement starts never-reported, and the retired installation keeps its own state.
        self.assertEqual(self.poll(at(6), B), PollingState(B, "never-reported", None, False, False, None, frozenset()))
        retired = PollingState(A, "current", ArchivedCheckpoint(2, at(3), record_of(last)), False, False,
                               SchedulingReport("unregistered", at(3), False),
                               frozenset({"background-scheduling-unregistered"}))
        self.assertEqual(self.poll(at(6), A), retired)
        first = checkpoint(1, installation=B, ended=at(6.5))
        self.accept(first, minute=7)
        self.assertEqual(self.poll(at(8), A), retired)
        self.assertEqual(self.poll(at(8), B), PollingState(
            B, "current", ArchivedCheckpoint(1, at(7), record_of(first)), False, False,
            SchedulingReport("available", at(7), False), frozenset(),
        ))
        moment = at(3) + 24 * HOUR + US
        self.assertEqual((self.poll(moment, A).freshness, self.poll(moment, B).freshness), ("stale", "delayed"))

    def test_retry_does_not_refresh_freshness(self) -> None:
        self.activate()
        data = checkpoint(1, scheduling="restricted")
        receipt = at(10)
        first = self.accept(data, receipt)
        self.assertEqual(self.accept(data, receipt + 7 * HOUR), first)
        state = self.poll(receipt + 7 * HOUR)
        self.assertEqual((state.freshness, state.latest_checkpoint, state.background_scheduling),
                         ("delayed", ArchivedCheckpoint(1, receipt, record_of(data)), SchedulingReport("restricted", receipt, False)))
        with self.store.read() as view:
            status = view.installation(A)
        self.assertEqual((status.last_delivery_at, status.latest_checkpoint_receipt_at, status.background_scheduling_receipt_at),
                         (receipt, receipt, receipt))
        # Nor does a blocked conflict or a retryable answer.
        self.deliverBlocked(checkpoint(1), "sequence-content-mismatch", receipt + 8 * HOUR)
        self.free.value = RESERVE - 1
        self.assertRetryable(self.accept(checkpoint(2), receipt + 9 * HOUR), "storage-reserve-exhausted", "storage-reserve-exhausted")
        self.assertEqual(self.poll(receipt + 24 * HOUR + US).freshness, "stale")


# R9 — Background Scheduling Status


class R9BackgroundSchedulingStatus(Case):
    def test_scheduling_report_keeps_its_receipt_time(self) -> None:
        self.activate()
        self.assertIsNone(self.poll(at(1)).background_scheduling)
        self.accept(checkpoint(1, scheduling="available"), minute=2)
        self.assertEqual(self.poll(at(3)).background_scheduling, SchedulingReport("available", at(2), False))
        self.send_batch(2, minute=4)
        self.assertEqual(self.poll(at(5)).background_scheduling, SchedulingReport("available", at(2), False))
        self.accept(checkpoint(3, scheduling="restricted"), minute=6)
        state = self.poll(at(6) + 7 * HOUR)
        self.assertEqual((state.freshness, state.background_scheduling),
                         ("delayed", SchedulingReport("restricted", at(6), False)))
        with self.store.read() as view:
            status = view.installation(A)
        self.assertEqual((status.background_scheduling, status.background_scheduling_receipt_at), ("restricted", at(6)))

    def test_restricted_and_unregistered_scheduling_are_conditions(self) -> None:
        self.activate()
        for sequence, scheduling in enumerate(("available", "restricted", "unregistered", "available"), start=1):
            self.accept(checkpoint(sequence, scheduling=scheduling), minute=sequence)
            self.assertEqual(self.poll(at(sequence)).conditions, SCHEDULING_CONDITIONS[scheduling], scheduling)
        # Version 1 always expects the registration, even while collection is disabled for every type.
        disabled = [type_status(name, "deferred", False, "collection-disabled") for name in RECORD_TYPES]
        self.accept(checkpoint(5, scheduling="unregistered", types=disabled), minute=5)
        state = self.poll(at(5))
        self.assertEqual(state.conditions, frozenset({"background-scheduling-unregistered"}))
        assert state.latest_checkpoint is not None
        self.assertEqual(state.latest_checkpoint.record.record_types,
                         tuple(RecordTypeOutcome(name, "deferred", "collection-disabled", False) for name in RECORD_TYPES))

    def test_stale_freshness_makes_the_scheduling_report_stale(self) -> None:
        self.activate()
        receipt = at(1)
        self.accept(checkpoint(1, scheduling="restricted"), receipt)
        for offset, freshness, stale in (
            (timedelta(0), "current", False), (6 * HOUR + US, "delayed", False), (24 * HOUR, "delayed", False),
            (24 * HOUR + US, "stale", True), (30 * DAY, "stale", True),
        ):
            state = self.poll(receipt + offset)
            self.assertEqual((state.freshness, state.background_scheduling),
                             (freshness, SchedulingReport("restricted", receipt, stale)), offset)
            self.assertEqual(state.conditions, frozenset({"background-scheduling-restricted"}), offset)


# R10 — Clock conditions


class R10ClockConditions(Case):
    def test_clock_ahead_of_receipt(self) -> None:
        receipt = at(10)
        for ended, expected in (
            (receipt + TOLERANCE, frozenset()),
            (receipt + TOLERANCE + MS, frozenset({"sender-clock-ahead"})),
            (receipt + 2 * DAY, frozenset({"sender-clock-ahead"})),
        ):
            item = ArchivedCheckpoint(1, receipt, record_of(checkpoint(1, ended=ended)))
            self.assertEqual(clock_conditions(item, at(0)), expected, ended)
            # Only instants matter, not offsets.
            self.assertEqual(clock_conditions(ArchivedCheckpoint(1, receipt.astimezone(EAST), item.record), at(0).astimezone(WEST)),
                             expected, ended)
        # Through acceptance, the polling state reports the latest checkpoint's conditions.
        self.activate()
        self.accept(checkpoint(1, ended=receipt + TOLERANCE + MS), receipt)
        state = self.poll(receipt)
        self.assertEqual((state.freshness, state.conditions), ("current", frozenset({"sender-clock-ahead"})))
        self.accept(checkpoint(2, ended=at(11)), at(11))
        self.assertEqual(self.poll(at(11)).conditions, frozenset())

    def test_clock_behind_enrollment(self) -> None:
        enrolled = at(0)
        for ended, expected in (
            (enrolled - TOLERANCE, frozenset()),
            (enrolled - TOLERANCE - MS, frozenset({"sender-clock-behind"})),
            (enrolled - 365 * DAY, frozenset({"sender-clock-behind"})),
            ("0000-02-29T00:00:30.000Z", frozenset({"sender-clock-behind"})),
        ):
            started = ("0000-02-29T00:00:00.000Z" if isinstance(ended, str) else None)
            item = ArchivedCheckpoint(1, at(130), record_of(checkpoint(1, started=started, ended=ended)))
            self.assertEqual(clock_conditions(item, enrolled), expected, ended)
            self.assertEqual(clock_conditions(item, enrolled.astimezone(EAST)), expected, ended)
        # Through acceptance, enrollment rather than activation is the bound, and the end time is what counts.
        self.enroll(A, minute=0)
        self.store.activate_installation(A, at(120))
        self.accept(checkpoint(1, ended=at(60)), at(130))
        self.assertEqual(self.poll(at(130)).conditions, frozenset())
        self.accept(checkpoint(2, started=at(-60), ended=at(-4), elapsed=56 * 60_000), at(131))
        self.assertEqual(self.poll(at(131)).conditions, frozenset())
        self.accept(checkpoint(3, ended=enrolled - TOLERANCE - MS), at(132))
        self.assertEqual(self.poll(at(132)).conditions, frozenset({"sender-clock-behind"}))
        self.accept(checkpoint(4, started="0000-02-29T00:00:00.000Z", ended="0000-02-29T00:00:30.000Z"), at(133))
        self.assertEqual(self.poll(at(133)).conditions, frozenset({"sender-clock-behind"}))

    def test_inconsistent_run_times(self) -> None:
        start, inconsistent = at(30), frozenset({"sender-times-inconsistent"})
        for ended, elapsed, expected in (
            (start + 30_000 * MS, 30_000 + 300_000, frozenset()),
            (start + 30_000 * MS, 30_000 + 300_001, inconsistent),
            (start + 10 * MINUTE, 300_000, frozenset()),
            (start + 10 * MINUTE, 299_999, inconsistent),
            (start - MINUTE, 60_000, frozenset()),
            (start - 3 * MINUTE, 180_000, inconsistent),
            (start, 0, frozenset()),
            (start - TOLERANCE, None, frozenset()),
            (start - TOLERANCE - MS, None, inconsistent),
            (start + 3 * HOUR, None, frozenset()),
        ):
            item = ArchivedCheckpoint(1, at(300), record_of(checkpoint(1, started=start, ended=ended, elapsed=elapsed)))
            self.assertEqual(clock_conditions(item, at(0)), expected, (ended, elapsed))
        # Year 0000 is a leap year, and runs may cross into year 0001; every such run is also behind enrollment.
        behind = frozenset({"sender-clock-behind"})
        for started, ended, elapsed, expected in (
            ("0000-02-28T12:00:00.000Z", "0000-03-01T12:00:00.000Z", 2 * DAY // MS, behind),
            ("0000-02-28T12:00:00.000Z", "0000-03-01T12:00:00.000Z", DAY // MS, behind | inconsistent),
            ("0000-12-31T23:59:00.000Z", "0001-01-01T00:01:00.000Z", 120_000, behind),
            ("0000-12-31T23:59:00.000Z", "0001-01-01T00:01:00.000Z", None, behind),
            ("0001-01-01T00:01:00.000Z", "0000-12-31T23:59:00.000Z", None, behind),
            ("0001-01-01T00:06:00.001Z", "0000-12-31T23:59:00.000Z", None, behind | inconsistent),
        ):
            item = ArchivedCheckpoint(1, at(300), record_of(checkpoint(1, started=started, ended=ended, elapsed=elapsed)))
            self.assertEqual(clock_conditions(item, at(0)), expected, (started, ended, elapsed))
        self.activate()
        self.accept(checkpoint(1, started=at(1), ended=at(1) + 30_000 * MS, elapsed=330_001), at(2))
        self.assertEqual(self.poll(at(2)).conditions, inconsistent)

    def test_delivery_delay_is_not_skew(self) -> None:
        self.activate()
        ended = at(1)
        # A two-hour run that started before enrollment and reached the Portal nine days later.
        self.accept(checkpoint(1, started=ended - 2 * HOUR, ended=ended, elapsed=2 * 3_600_000), ended + 9 * DAY)
        state = self.poll(ended + 9 * DAY)
        self.assertEqual((state.freshness, state.conditions), ("current", frozenset()))
        assert state.latest_checkpoint is not None
        self.assertEqual(clock_conditions(ArchivedCheckpoint(1, ended + 300 * DAY, state.latest_checkpoint.record), at(0)),
                         frozenset())

    def test_latest_checkpoint_is_reported_exactly(self) -> None:
        self.activate()
        self.accept(checkpoint(1, run=1), minute=1)
        self.send_batch(2, minute=2)
        latest = checkpoint(3, run=2, trigger="manual", started="2026-10-16T09:01:00.001Z", ended="2026-10-16T09:02:59.999Z",
                            elapsed=None, scheduling="restricted", outbox=(5, 7000), types=[
                                type_status("heart-rate", "failed", False, "read-failed"),
                                type_status("sleep", "blocked", True, "delivery-blocked"),
                                type_status("steps", "not-due"),
                            ])
        self.accept(latest, minute=3)
        self.send_batch(4, minute=4)
        expected = ArchivedCheckpoint(3, at(3), SyncCheckpointRecord(
            2, "manual", "2026-10-16T09:01:00.001Z", "2026-10-16T09:02:59.999Z", None, "restricted",
            (RecordTypeOutcome("heart-rate", "failed", "read-failed", False),
             RecordTypeOutcome("sleep", "blocked", "delivery-blocked", True),
             RecordTypeOutcome("steps", "not-due", None, False)),
            5, 7000,
        ))
        state = self.poll(at(5))
        self.assertEqual(state.latest_checkpoint, expected)
        self.assertEqual(self.latest(), expected)
        self.assertEqual(state.conditions, frozenset({"background-scheduling-restricted"}))
        # Portal time steps back: the higher sequence is still the latest.
        later = checkpoint(5, run=3)
        self.accept(later, minute=2)
        self.assertEqual(self.poll(at(6)).latest_checkpoint, ArchivedCheckpoint(5, at(2), record_of(later)))


# R11 — Reading polling state


class R11ReadingPollingState(Case):
    def test_reading_polling_state_writes_nothing(self) -> None:
        # A: retired after reporting implausible times and restricted scheduling; B: active and in its first poll;
        # C: pending; D: withdrawn.
        self.activate(A, minute=0)
        self.accept(checkpoint(1, ended=at(30)), minute=2)
        self.store.begin_draining(A, at(3))
        self.accept(checkpoint(2, scheduling="restricted", ended=at(90)), minute=4)
        self.store.retire_installation(A, "drained", at(5))
        self.activate(B, minute=6)
        self.send_batch(1, minute=7, installation=B)
        self.enroll(C, minute=8)
        self.enroll(D, minute=9)
        self.store.withdraw_installation(D, at(10))
        moments = (at(-60), at(4), at(4) + 7 * HOUR, at(4) + 25 * HOUR, at(6) + 30 * HOUR, at(4) + 400 * DAY)
        before = contents(self.path)
        states = {}
        for installation in (A, B, C, D):
            for moment in moments:
                states[installation, moment] = self.poll(moment, installation)
                self.assertEqual(contents(self.path), before, (installation, moment))
        self.assertEqual({states[A, moment].freshness for moment in moments}, {"current", "delayed", "stale"})
        self.assertTrue(states[A, at(4)].conditions)
        self.assertTrue(states[B, at(6) + 30 * HOUR].first_checkpoint_overdue)
        self.assertTrue(states[B, at(4) + 7 * HOUR].first_poll_in_progress)
        # A plain read: the same answers while another connection holds the write lock.
        holder = sqlite3.connect(self.path, isolation_level=None, timeout=0)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        try:
            for (installation, moment), state in states.items():
                self.assertEqual(self.poll(moment, installation), state, (installation, moment))
        finally:
            holder.execute("ROLLBACK")
        self.assertEqual(contents(self.path), before)

    def test_reading_polling_state_propagates_store_failures(self) -> None:
        self.activate()
        self.accept(checkpoint(1), minute=1)
        before = contents(self.path)
        with self.assertRaises(StoreRefusal) as refused:
            self.poll(at(2), UNKNOWN)
        self.assertEqual(refused.exception.failure, "installation-unknown")
        with self.assertRaises(StoreRetryable) as busy:
            polling_state(BusyAt(self.store, "read"), A, at(2))
        self.assertEqual(busy.exception.failure, "store-busy")
        self.assertEqual(contents(self.path), before)


# R12 — Capacity and contention


class R12CapacityAndContention(Case):
    def test_exhausted_reserve_is_retryable_and_blocks_still_win(self) -> None:
        self.activate()
        self.accept(checkpoint(1))
        self.accept(checkpoint(3))
        self.free.value = RESERVE - 1
        self.deliverBlocked(checkpoint(1, run=5), "sequence-content-mismatch")
        self.deliverBlocked(checkpoint(2), "sequence-out-of-order")
        self.deliverBlocked(checkpoint(4, generation=3), "unsupported-wire-protocol")
        before = contents(self.path)
        for data in (checkpoint(4), checkpoint(9, scheduling="unregistered")):
            self.assertRetryable(self.accept(data, minute=2), "storage-reserve-exhausted", "storage-reserve-exhausted")
        self.assertEqual(contents(self.path), before)
        self.free.value = RESERVE
        self.assertReceipt(self.accept(checkpoint(4)), checkpoint(4))

    def test_busy_store_is_retryable(self) -> None:
        self.activate()
        self.send_batch(1)
        exact, wrong = checkpoint(2), checkpoint(2, generation=3)
        before = contents(self.path)
        holder = sqlite3.connect(self.path, isolation_level=None, timeout=0)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        try:
            for data in (exact, wrong):
                self.assertRetryable(self.accept(data), "temporarily-unavailable", "store-busy")
        finally:
            holder.execute("ROLLBACK")
        for operation, data in (("read", exact), ("read", wrong), ("archive_sync_checkpoint", exact),
                                ("record_delivery_block", wrong)):
            self.assertRetryable(accept_sync_checkpoint(BusyAt(self.store, operation), data, at(2)),
                                 "temporarily-unavailable", "store-busy")
        self.assertEqual(contents(self.path), before)
        # The generation is checked before archival: a busy archive never hides a block.
        busy_archive = BusyAt(self.store, "archive_sync_checkpoint")
        for minute in (3, 6):
            self.deliverBlocked(wrong, "unsupported-wire-protocol", minute=minute, store=busy_archive)
        self.assertEqual(without_blocks(contents(self.path)), without_blocks(before))
        self.assertReceipt(self.accept(exact), exact)

    def test_store_refusals_after_the_read_are_answered(self) -> None:
        self.activate()
        self.send_batch(1)
        data = checkpoint(2)
        for refusal, failure in (
            ("sequence-content-conflict", "sequence-content-mismatch"),
            ("sequence-out-of-order", "sequence-out-of-order"),
            ("generation-unknown", "unsupported-wire-protocol"),
        ):
            self.deliverBlocked(data, failure, store=RefusesArchive(self.store, refusal))
        # Another writer retires the installation after the read: the store refuses, and only the retirement is written.
        twin = self.directory / "retired.sqlite3"
        copy_database(self.path, twin)
        with open_store(twin, reserve_bytes=RESERVE, free_bytes=self.free, busy_timeout_ms=100) as twin_store:
            twin_store.retire_installation(A, "destructive", at(2))
        response = accept_sync_checkpoint(RetiresBeforeArchive(self.store, at(2)), data, at(2))
        self.assertRetryable(response, "temporarily-unavailable", "installation-ended")
        self.assertEqual(contents(self.path), contents(twin))


# R13 — Conformance with the protocol's evaluation


class R13Conformance(Case):
    def test_answers_match_the_protocol_evaluation(self) -> None:
        self.events: collections.Counter[str] = collections.Counter()
        for seed in range(10):
            with self.subTest(seed=seed):
                self.run_history(seed)
        required = (
            "receipt", "gap", "retry", "repeated-block", "batch", *BLOCK_FAILURES,
            *(f"trigger-{trigger}" for trigger in ("background", "foreground", "manual")),
            *(f"scheduling-{status}" for status in SCHEDULING_CONDITIONS),
            *(f"outcome-{outcome}" for outcome in OUTCOMES),
            "no-elapsed", "year-zero", "sender-clock-ahead", "sender-clock-behind", "sender-times-inconsistent",
            *(f"freshness-{freshness}" for freshness in ("never-reported", "current", "delayed", "stale")),
            "first-poll-in-progress", "first-checkpoint-overdue",
        )
        for event in required:
            self.assertGreater(self.events[event], 0, event)

    def records(self, store) -> dict[tuple[object, ...], Any]:
        with store.read() as view:
            return {(r.installation_id, r.sequence, r.message_sha256, r.failure): r for r in view.delivery_blocks()}

    def random_checkpoint(self, rng: random.Random, sequence: int, now: datetime, implausible: bool, generation: int) -> bytes:
        names = sorted(rng.sample(RECORD_TYPES, rng.randint(0, len(RECORD_TYPES))))
        types = []
        for name in names:
            outcome = rng.choice(sorted(OUTCOMES))
            reason = None
            if outcome in REASON_REQUIRED or (outcome in REASON_OPTIONAL and rng.random() < 0.5):
                reason = rng.choice(sorted(REASONS))
            types.append(type_status(name, outcome, rng.random() < 0.5, reason))
        ended = max(now - rng.choice([0, 1, 30, 300, 3000]) * MINUTE, T0)
        duration = rng.randint(0, 600_000)
        elapsed: int | None = None if rng.random() < 0.3 else duration + rng.randint(-60_000, 60_000)
        if elapsed is not None:
            elapsed = max(elapsed, 0)
        text: tuple[str, str] | None = None
        if implausible:
            kind = rng.choice(["ahead", "behind", "elapsed", "reversed", "year-zero"])
            if kind == "ahead":
                ended = now + TOLERANCE + rng.randint(1, 10**8) * MS
            elif kind == "behind":
                ended = T0 - TOLERANCE - rng.randint(1, 10**10) * MS
            elif kind == "elapsed":
                elapsed = duration + 300_001 + rng.randint(0, 10**7)
            elif kind == "reversed":
                elapsed, duration = None, -300_001 - rng.randint(0, 10**7)
            else:
                text = (f"0000-{rng.choice(['01-01', '02-29', '12-31'])}T{rng.randint(0, 23):02d}:00:00.000Z",
                        rng.choice(["0000-12-31T23:59:59.999Z", "0001-01-01T00:00:00.000Z", stamp(ended)]))
        started_at, ended_at = text if text is not None else (ended - duration * MS, ended)
        return checkpoint(
            sequence, generation=generation, run=rng.randint(1, 10**6), trigger=rng.choice(["background", "foreground", "manual"]),
            started=started_at, ended=ended_at, elapsed=elapsed, scheduling=rng.choice(sorted(SCHEDULING_CONDITIONS)),
            types=types, outbox=(rng.randint(0, 50), rng.randint(0, 10**7)),
        )

    def expected_state(self, latest: tuple[int, datetime, bytes] | None, now: datetime, activated: datetime,
                       delivered: bool) -> PollingState:
        if latest is None:
            return PollingState(A, "never-reported", None, delivered, now - activated > 24 * HOUR, None, frozenset())
        sequence, receipt, data = latest
        item = ArchivedCheckpoint(sequence, receipt, record_of(data))
        age = now - receipt
        freshness = "current" if age <= 6 * HOUR else "delayed" if age <= 24 * HOUR else "stale"
        scheduling = item.record.background_scheduling
        return PollingState(
            A, freshness, item, False, False, SchedulingReport(scheduling, receipt, freshness == "stale"),
            expected_clock_conditions(item, T0) | SCHEDULING_CONDITIONS[scheduling],
        )

    def run_history(self, seed: int) -> None:
        rng = random.Random(seed)
        directory = self.directory / f"seed-{seed}"
        directory.mkdir()
        path = directory / "portal.sqlite3"
        store = self.open(path)
        for generation in (1, 2, 3):
            store.honor_generation(generation, encode_message(advertisement(generation)), T0)
        self.activate(A, store=store)
        state = new_delivery_state([advertisement(1), advertisement(2), advertisement(3)])
        sequence, cycles = 0, 0
        sent: list[bytes] = []
        accepted: set[int] = set()
        skipped: list[int] = []
        latest: tuple[int, datetime, bytes] | None = None
        delivered = False
        now = T0
        for step in range(150):
            now += rng.choice([1, 20, 90, 400, 1000, 2000]) * MINUTE
            action = rng.choice(["checkpoint"] * 6 + ["batch"] * 2 + [
                "implausible", "retry", "retry", "conflict", "old", "skip", "unknown-generation",
            ])
            if step == 0 and seed % 2 == 0:
                # Some histories deliver data for more than a day before their first checkpoint.
                now, action = now + 2000 * MINUTE, "batch"
            if action == "skip":
                sequence += 1
                skipped.append(sequence)
                continue
            if action == "batch":
                sequence += 1
                cycles += 1
                data = batch(sequence, cycle_id(cycles), generation=rng.choice([1, 2, 3]))
                expected = evaluate_delivery(state, data)
                self.assertEqual(accept_ingest_batch(store, data, now).body, expected.response)
                self.assertEqual(expected.outcome, "receipt")
                accepted.add(sequence)
                state = expected.state
                delivered = True
                self.events["batch"] += 1
            else:
                if action == "retry" and sent:
                    data = rng.choice(sent)
                elif action == "conflict" and accepted:
                    data = self.random_checkpoint(rng, rng.choice(sorted(accepted)), now, False, rng.choice([1, 2, 3, 4]))
                elif action == "old" and skipped:
                    data = self.random_checkpoint(rng, rng.choice(skipped), now, False, rng.choice([1, 2, 3, 4]))
                else:
                    sequence += 1
                    generation = 4 if action == "unknown-generation" else rng.choice([1, 2, 3])
                    data = self.random_checkpoint(rng, sequence, now, action == "implausible", generation)
                message = ingest_protocol.decode_message(data)
                new = message["sequence"] not in accepted
                expected = evaluate_delivery(state, data)
                records_before = self.records(store)
                database_before = contents(path)
                actual = accept_sync_checkpoint(store, data, now)
                self.assertEqual(
                    (actual.outcome, actual.body, actual.failure, actual.gap_after),
                    (expected.outcome, expected.response, expected.failure, expected.gap_after),
                    (seed, step, action),
                )
                records = self.records(store)
                if expected.outcome == "block":
                    key = (A, message["sequence"], message_sha256(data), expected.failure)
                    prior = records_before.get(key)
                    self.assertEqual(
                        (records[key].count, records[key].first_seen_at, records[key].last_seen_at),
                        ((prior.count if prior else 0) + 1, prior.first_seen_at if prior else now, now),
                    )
                    self.events[expected.failure] += 1
                    if prior is not None:
                        self.events["repeated-block"] += 1
                    records = {k: v for k, v in records.items() if k != key}
                    records_before = {k: v for k, v in records_before.items() if k != key}
                    if new and message["sequence"] == sequence and action not in ("conflict", "old", "retry"):
                        # A blocked new sequence is either reused by the next message or left as a hole.
                        if rng.random() < 0.75:
                            sequence -= 1
                        else:
                            skipped.append(sequence)
                self.assertEqual(records, records_before, (seed, step, action))
                database_after = contents(path)
                if expected.outcome == "block":
                    self.assertEqual(without_blocks(database_after), without_blocks(database_before), (seed, step, action))
                elif not new:
                    self.assertEqual(database_after, database_before, (seed, step, action))
                    self.events["retry"] += 1
                if expected.outcome == "receipt":
                    self.events["receipt"] += 1
                    if expected.gap_after is not None:
                        self.events["gap"] += 1
                    if new:
                        if message["sequence"] in skipped:
                            skipped.remove(message["sequence"])
                        accepted.add(message["sequence"])
                        state = expected.state
                        delivered = True
                        latest = (message["sequence"], now, data)
                        item = ArchivedCheckpoint(message["sequence"], now, record_of(data))
                        self.assertEqual(polling_state(store, A, now).latest_checkpoint, item, (seed, step, action))
                        self.events[f"trigger-{message['trigger']}"] += 1
                        self.events[f"scheduling-{message['background_scheduling']}"] += 1
                        for status in message["record_types"]:
                            self.events[f"outcome-{status['outcome']}"] += 1
                        if "elapsed_ms" not in message:
                            self.events["no-elapsed"] += 1
                        if message["started_at"].startswith("0000"):
                            self.events["year-zero"] += 1
                        for condition in expected_clock_conditions(item, T0):
                            self.events[condition] += 1
                sent.append(data)
            # The polling state at this step and a little later, from the latest accepted checkpoint.
            for moment in (now, now + rng.choice([1, 400, 1000, 2000]) * MINUTE):
                polled = polling_state(store, A, moment)
                self.assertEqual(polled, self.expected_state(latest, moment, T0, delivered), (seed, step, action))
                self.events[f"freshness-{polled.freshness}"] += 1
                self.events["first-poll-in-progress"] += polled.first_poll_in_progress
                self.events["first-checkpoint-overdue"] += polled.first_checkpoint_overdue


if __name__ == "__main__":
    unittest.main()
