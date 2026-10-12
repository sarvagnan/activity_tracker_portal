"""Acceptance tests for `operational-store@1`.

Requirement numbers refer to `packs/operational-store-1.spec.md`. Every
database lives in a temporary directory, every value is synthetic, and the
store is exercised only through its Interface plus the on-disk migration
record that R3 makes part of the contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from activity_tracker_portal import operational_store as store_module
from activity_tracker_portal.operational_store import (
    BatchItem,
    CompletionRecord,
    DeliveryBlockRecord,
    IngestBatchRecord,
    MessageHeader,
    RecordTypeOutcome,
    StoreRefusal,
    StoreRetryable,
    SyncCheckpointRecord,
    open_store,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/operational-store.1.json").read_text(encoding="utf-8"))

UTC = timezone.utc
T0 = datetime(2026, 10, 12, 9, 0, 0, 123456, tzinfo=UTC)
RESERVE = 1_000_000
PLENTY = 10**12
INSTALLATION = "0f8fad5b-d9cb-469f-a165-70867728950e"
OTHER = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
THIRD = "16fd2706-8baf-433b-82eb-8c7fada847da"
CYCLE = "e3b0c442-98fc-4c14-9afb-f4c8996fb924"
OTHER_CYCLE = "5d41402a-bc4b-4a76-b971-9d911017c592"
HEX = re.compile(r"[0-9a-f]{64}")
HEALTH_MARKER = b"synthetic-health-marker"
IDENTITY_MARKER = "synthetic-identity-marker"


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def code(name: str) -> str:
    return digest(b"synthetic-enrollment-code-" + name.encode())


def fingerprint(installation_id: str) -> str:
    return digest(b"synthetic-fingerprint-" + installation_id.encode())


def envelope(sequence: int, kind: str = "ingest-batch", salt: str = "", installation_id: str = INSTALLATION) -> bytes:
    body = {"synthetic": True, "kind": kind, "installation": installation_id, "sequence": sequence, "salt": salt}
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode()


def header(
    sequence: int,
    *,
    data: bytes | None = None,
    receipt_time: datetime | None = None,
    generation: int = 1,
    installation_id: str = INSTALLATION,
    kind: str = "ingest-batch",
    receipt: bytes | None = None,
) -> MessageHeader:
    data = envelope(sequence, kind, installation_id=installation_id) if data is None else data
    return MessageHeader(
        installation_id=installation_id,
        sequence=sequence,
        message_sha256=digest(data),
        staged_generation=generation,
        receipt_time=at(sequence) if receipt_time is None else receipt_time,
        envelope=data,
        receipt=b'{"synthetic-receipt":%d}' % sequence if receipt is None else receipt,
    )


def batch(*items: BatchItem, cycle_id: str = CYCLE, page: int = 1, fragment: int = 1) -> IngestBatchRecord:
    return IngestBatchRecord(
        record_type="steps",
        projection_schema_version=1,
        cycle_kind="initial-snapshot",
        cycle_id=cycle_id,
        source_page_index=page,
        fragment_index=fragment,
        previous_chain=digest(cycle_id.encode()),
        items=items or (BatchItem(projection=b'{"synthetic-projection":1}'),),
    )


def completion(cycle_id: str = CYCLE, kind: str = "snapshot") -> CompletionRecord:
    return CompletionRecord(
        completion_kind=kind,  # type: ignore[arg-type]
        snapshot_kind="initial" if kind == "snapshot" else None,
        record_type="steps",
        cycle_id=cycle_id,
        page_count=1,
        fragment_count=2,
        record_count=3,
        final_chain=digest(b"synthetic-final-chain-" + cycle_id.encode()),
    )


def checkpoint(run_sequence: int, scheduling: str = "available") -> SyncCheckpointRecord:
    return SyncCheckpointRecord(
        run_sequence=run_sequence,
        trigger="background",
        started_at="2026-10-12T08:59:00.000Z",
        ended_at="2026-10-12T08:59:30.000Z",
        elapsed_ms=30000,
        background_scheduling=scheduling,
        record_types=(
            RecordTypeOutcome(record_type="steps", outcome="progressed", reason="time-budget-reached", source_data_staged=True),
            RecordTypeOutcome(record_type="weight", outcome="no-change", reason=None, source_data_staged=False),
        ),
        outbox_messages_before_checkpoint=4,
        outbox_bytes_before_checkpoint=4096,
    )


class FreeSpace:
    """Volatile free-space input supplied to the store under test."""

    def __init__(self, value: int) -> None:
        self.value = value
        self.paths: list[str] = []

    def __call__(self, path: str | os.PathLike[str]) -> int:
        self.paths.append(os.fspath(path))
        return self.value


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "portal.sqlite3"
        self.free = FreeSpace(PLENTY)

    def open(self, **options: object):
        store = open_store(self.path, reserve_bytes=RESERVE, free_bytes=self.free, **options)  # type: ignore[arg-type]
        self.addCleanup(store.close)
        return store

    def enrolled(self, store, installation_id: str = INSTALLATION, minute: float = 0) -> None:
        name = installation_id
        store.record_enrollment_offer(code(name), at(minute + 30), at(minute))
        store.enroll_installation(code(name), installation_id, fingerprint(installation_id), at(minute))

    def active(self, store, installation_id: str = INSTALLATION, minute: float = 0) -> None:
        self.enrolled(store, installation_id, minute)
        store.activate_installation(installation_id, at(minute))

    def ready(self, **options: object):
        store = self.open(**options)
        self.active(store)
        store.honor_generation(1, b'{"synthetic-advertisement":1}', T0)
        return store

    def external(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(connection.close)
        return connection

    def dump(self) -> list[str]:
        connection = sqlite3.connect(self.path)
        try:
            return [line for line in connection.iterdump() if "test_fault" not in line]
        finally:
            connection.close()

    def persistent_state(self) -> tuple[list[str], int, str]:
        connection = sqlite3.connect(self.path)
        try:
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            connection.close()
        return self.dump(), user_version, journal_mode

    def file_state(self) -> tuple[str, list[str]]:
        return digest(self.path.read_bytes()), sorted(entry.name for entry in self.directory.iterdir())

    def assertRefused(self, failure: str, operation, *args, **kwargs) -> StoreRefusal:
        with self.assertRaises(StoreRefusal) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.failure, failure)
        return caught.exception

    def assertRetryable(self, failure: str, operation, *args, **kwargs) -> StoreRetryable:
        with self.assertRaises(StoreRetryable) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.failure, failure)
        return caught.exception


class R1GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["scope"], "operational-store")
        self.assertEqual(CONTRACT["version"], "1")
        self.assertEqual(CONTRACT["kind"], "shared")
        self.assertEqual(CONTRACT["dependencies"], [])
        self.assertEqual({item["mode"] for item in CONTRACT["inputs"]}, {"pure", "effectful", "volatile"})
        for key in ("purpose", "outputs", "named_failures", "observable_effects"):
            self.assertTrue(CONTRACT[key], key)
        self.assertEqual(set(CONTRACT["authority"]), {"owns", "does_not_own"})
        oracle = CONTRACT["acceptance_oracle"]
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_every_named_failure_has_one_outcome_class(self) -> None:
        by_outcome: dict[str, set[str]] = {}
        names = [failure["name"] for failure in CONTRACT["named_failures"]]
        self.assertEqual(len(names), len(set(names)))
        for failure in CONTRACT["named_failures"]:
            by_outcome.setdefault(failure["outcome"], set()).add(failure["name"])
        self.assertEqual(
            by_outcome,
            {
                "startup-refusal": set(store_module.STARTUP_FAILURES),
                "retryable": set(store_module.RETRYABLE_FAILURES),
                "invariant-refusal": set(store_module.INVARIANT_FAILURES),
            },
        )
        self.assertEqual(set(names), set(store_module.FAILURES))

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `operational-store` |")]
        self.assertEqual(len(rows), 1)
        cells = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(cells[1], CONTRACT["kind"])
        self.assertEqual(cells[-1], "—")
        self.assertEqual(CONTRACT["dependencies"], [])


class R2RuntimeAndDurability(StoreCase):
    def test_unsupported_sqlite_runtime_refuses_before_creating_file(self) -> None:
        with patch.object(sqlite3, "sqlite_version_info", (3, 50, 4)):
            self.assertRefused("sqlite-runtime-unsupported", open_store, self.path, reserve_bytes=RESERVE, free_bytes=self.free)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_runtime_minimum_is_exactly_3_51_3(self) -> None:
        with patch.object(sqlite3, "sqlite_version_info", (3, 51, 2)):
            self.assertRefused("sqlite-runtime-unsupported", open_store, self.path, reserve_bytes=RESERVE, free_bytes=self.free)
        self.assertFalse(self.path.exists())
        with patch.object(sqlite3, "sqlite_version_info", (3, 51, 3)):
            store = self.open()
        self.assertEqual(store.settings().schema_migrations[0][0], 1)

    def test_unsupported_runtime_leaves_existing_database_untouched(self) -> None:
        store = open_store(self.path, reserve_bytes=RESERVE, free_bytes=self.free)
        self.enrolled(store)
        store.close()
        before = self.file_state()
        with patch.object(sqlite3, "sqlite_version_info", (3, 50, 4)):
            self.assertRefused("sqlite-runtime-unsupported", open_store, self.path, reserve_bytes=RESERVE, free_bytes=self.free)
        self.assertEqual(self.file_state(), before)

    def test_connections_use_wal_full_sync_and_foreign_keys(self) -> None:
        store = self.open(busy_timeout_ms=1234)
        with store.read() as view:
            view_settings = view.settings()
        for settings in (store.settings(), view_settings):
            self.assertEqual(settings.journal_mode, "wal")
            self.assertEqual(settings.synchronous, 2)
            self.assertTrue(settings.foreign_keys)
            self.assertEqual(settings.busy_timeout_ms, 1234)
            self.assertEqual(settings.sqlite_version, sqlite3.sqlite_version)
        self.assertEqual(self.external().execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_lock_contention_beyond_timeout_is_store_busy(self) -> None:
        store = self.open(busy_timeout_ms=200)
        blocker = self.external()
        blocker.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        self.assertRetryable("store-busy", store.record_enrollment_offer, code("busy"), at(30), at(0))
        self.assertGreaterEqual(time.monotonic() - started, 0.15)
        blocker.execute("ROLLBACK")
        store.record_enrollment_offer(code("busy"), at(30), at(0))
        store.enroll_installation(code("busy"), INSTALLATION, fingerprint(INSTALLATION), at(1))


class R3BaselineSchemaAndMigrations(StoreCase):
    def migration_rows(self) -> list[tuple[int, str]]:
        return [tuple(row) for row in self.external().execute("SELECT version, checksum FROM store_migrations ORDER BY version")]

    def test_new_database_records_baseline_migration_checksum(self) -> None:
        store = self.open()
        migrations = store.settings().schema_migrations
        self.assertEqual(len(migrations), 1)
        version, checksum = migrations[0]
        self.assertEqual([number for number, _ in store_module.MIGRATIONS], [1])
        self.assertEqual(version, 1)
        self.assertIsNotNone(HEX.fullmatch(checksum))
        self.assertEqual(checksum, digest(store_module.MIGRATIONS[0][1].encode("utf-8")))
        self.assertEqual(self.migration_rows(), [(1, checksum)])
        self.assertEqual(self.external().execute("PRAGMA user_version").fetchone()[0], 1)

    def test_reopen_preserves_committed_state(self) -> None:
        store = open_store(self.path, reserve_bytes=RESERVE, free_bytes=self.free)
        self.enrolled(store)
        first = store.settings().schema_migrations
        store.close()
        reopened = self.open()
        self.assertEqual(reopened.settings().schema_migrations, first)
        with reopened.read() as view:
            status = view.installation(INSTALLATION)
        self.assertEqual(status.state, "pending")
        self.assertEqual(status.fingerprint, fingerprint(INSTALLATION))

    def prepared(self, *statements: str) -> None:
        open_store(self.path, reserve_bytes=RESERVE, free_bytes=self.free).close()
        connection = self.external()
        for statement in statements:
            connection.execute(statement)

    def test_older_schema_requires_explicit_migration(self) -> None:
        self.prepared("DELETE FROM store_migrations", "PRAGMA user_version = 0")
        before = self.persistent_state()
        self.assertRefused("store-migration-required", open_store, self.path, reserve_bytes=RESERVE, free_bytes=self.free)
        self.assertEqual(self.persistent_state(), before)
        self.assertEqual(before[1], 0)

    def test_unknown_later_schema_is_unsupported(self) -> None:
        self.prepared(
            "INSERT INTO store_migrations (version, checksum, applied_at) VALUES (2, '%s', '2026-10-12T09:00:00.000000Z')" % ("f" * 64),
            "PRAGMA user_version = 2",
        )
        before = self.persistent_state()
        self.assertRefused("store-schema-unsupported", open_store, self.path, reserve_bytes=RESERVE, free_bytes=self.free)
        self.assertEqual(self.persistent_state(), before)

    def test_foreign_database_is_unsupported_and_untouched(self) -> None:
        connection = self.external()
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES ('synthetic')")
        connection.close()
        before = self.persistent_state()
        self.assertEqual(before[2], "delete")
        self.assertRefused("store-schema-unsupported", open_store, self.path, reserve_bytes=RESERVE, free_bytes=self.free)
        self.assertEqual(self.persistent_state(), before)

    def test_changed_migration_checksum_is_refused(self) -> None:
        self.prepared("UPDATE store_migrations SET checksum = '%s' WHERE version = 1" % ("0" * 64))
        before = self.persistent_state()
        self.assertRefused("store-migration-checksum-mismatch", open_store, self.path, reserve_bytes=RESERVE, free_bytes=self.free)
        self.assertEqual(self.persistent_state(), before)


class R4OneUseEnrollmentCodes(StoreCase):
    def test_enrollment_consumes_code_and_creates_pending_installation_atomically(self) -> None:
        store = self.open()
        store.record_enrollment_offer(code("a"), at(30), at(0))
        store.enroll_installation(code("a"), INSTALLATION, fingerprint(INSTALLATION), at(5))
        with store.read() as view:
            status = view.installation(INSTALLATION)
            events = view.installation_events(INSTALLATION)
        self.assertEqual(status.state, "pending")
        self.assertEqual(status.fingerprint, fingerprint(INSTALLATION))
        self.assertEqual(status.enrolled_at, at(5))
        self.assertEqual([(event.state, event.at) for event in events], [("pending", at(5))])

    def test_enrollment_code_is_single_use(self) -> None:
        store = self.open()
        store.record_enrollment_offer(code("a"), at(30), at(0))
        store.enroll_installation(code("a"), INSTALLATION, fingerprint(INSTALLATION), at(1))
        self.assertRefused("enrollment-code-consumed", store.enroll_installation, code("a"), OTHER, fingerprint(OTHER), at(2))
        with store.read() as view:
            self.assertRefused("installation-unknown", view.installation, OTHER)

    def test_expired_or_unknown_code_is_refused(self) -> None:
        store = self.open()
        store.record_enrollment_offer(code("a"), at(10), at(0))
        self.assertRefused("enrollment-code-expired", store.enroll_installation, code("a"), INSTALLATION, fingerprint(INSTALLATION), at(10))
        self.assertRefused("enrollment-code-unknown", store.enroll_installation, code("never"), INSTALLATION, fingerprint(INSTALLATION), at(1))
        with store.read() as view:
            self.assertEqual(view.installations(), ())

    def test_refused_enrollment_does_not_consume_code(self) -> None:
        store = self.open()
        self.enrolled(store, INSTALLATION)
        store.record_enrollment_offer(code("fresh"), at(30), at(0))
        self.assertRefused("installation-exists", store.enroll_installation, code("fresh"), INSTALLATION, fingerprint(INSTALLATION), at(1))
        store.enroll_installation(code("fresh"), OTHER, fingerprint(OTHER), at(2))
        with store.read() as view:
            self.assertEqual(view.installation(OTHER).state, "pending")

    def test_enrollment_code_state_survives_reopen(self) -> None:
        store = open_store(self.path, reserve_bytes=RESERVE, free_bytes=self.free)
        store.record_enrollment_offer(code("a"), at(30), at(0))
        store.record_enrollment_offer(code("b"), at(30), at(0))
        store.enroll_installation(code("a"), INSTALLATION, fingerprint(INSTALLATION), at(1))
        store.close()
        reopened = self.open()
        self.assertRefused("enrollment-code-consumed", reopened.enroll_installation, code("a"), OTHER, fingerprint(OTHER), at(2))
        reopened.enroll_installation(code("b"), OTHER, fingerprint(OTHER), at(3))

    def test_enrollment_offer_digest_is_recorded_once(self) -> None:
        store = self.open()
        store.record_enrollment_offer(code("a"), at(30), at(0))
        self.assertRefused("enrollment-code-exists", store.record_enrollment_offer, code("a"), at(60), at(1))
        store.enroll_installation(code("a"), INSTALLATION, fingerprint(INSTALLATION), at(2))
        self.assertRefused("enrollment-code-exists", store.record_enrollment_offer, code("a"), at(90), at(3))
        self.assertRefused("enrollment-code-consumed", store.enroll_installation, code("a"), OTHER, fingerprint(OTHER), at(4))
        store.record_enrollment_offer(code("b"), at(5), at(0))
        self.assertRefused("enrollment-code-exists", store.record_enrollment_offer, code("b"), at(90), at(6))
        self.assertRefused("enrollment-code-expired", store.enroll_installation, code("b"), OTHER, fingerprint(OTHER), at(6))


class R5SenderInstallationLifecycle(StoreCase):
    def test_lifecycle_follows_declared_transitions_only(self) -> None:
        store = self.open()
        self.enrolled(store, INSTALLATION)
        self.assertRefused("installation-transition-invalid", store.begin_draining, INSTALLATION, at(1))
        self.assertRefused("installation-transition-invalid", store.retire_installation, INSTALLATION, "drained", at(1))
        self.assertRefused("installation-transition-invalid", store.retire_installation, INSTALLATION, "destructive", at(1))
        store.activate_installation(INSTALLATION, at(1))
        self.assertRefused("installation-transition-invalid", store.activate_installation, INSTALLATION, at(2))
        self.assertRefused("installation-transition-invalid", store.withdraw_installation, INSTALLATION, at(2))
        self.assertRefused("installation-transition-invalid", store.retire_installation, INSTALLATION, "drained", at(2))
        store.begin_draining(INSTALLATION, at(2))
        self.assertRefused("installation-transition-invalid", store.begin_draining, INSTALLATION, at(3))
        store.retire_installation(INSTALLATION, "drained", at(3))
        for operation in (store.activate_installation, store.begin_draining, store.withdraw_installation):
            self.assertRefused("installation-transition-invalid", operation, INSTALLATION, at(4))
        self.assertRefused("installation-transition-invalid", store.retire_installation, INSTALLATION, "destructive", at(4))

        self.enrolled(store, OTHER, 5)
        store.withdraw_installation(OTHER, at(6))
        self.assertRefused("installation-transition-invalid", store.activate_installation, OTHER, at(7))

        self.active(store, THIRD, 8)
        store.retire_installation(THIRD, "destructive", at(9))
        with store.read() as view:
            self.assertEqual(view.installation(THIRD).retirement_kind, "destructive")
            self.assertEqual(view.installation(INSTALLATION).retirement_kind, "drained")
            self.assertEqual(view.installation(OTHER).state, "withdrawn")
        self.assertRefused("installation-unknown", store.activate_installation, "00000000-0000-4000-8000-000000000000", at(10))

    def test_draining_installation_can_be_retired_destructively(self) -> None:
        store = self.open()
        self.active(store)
        store.begin_draining(INSTALLATION, at(1))
        store.retire_installation(INSTALLATION, "destructive", at(2))
        with store.read() as view:
            status = view.installation(INSTALLATION)
        self.assertEqual((status.state, status.retirement_kind, status.retired_at), ("retired", "destructive", at(2)))

    def test_second_active_or_draining_installation_is_refused(self) -> None:
        store = self.open()
        self.active(store, INSTALLATION)
        self.enrolled(store, OTHER, 1)
        self.assertRefused("active-installation-exists", store.activate_installation, OTHER, at(2))
        store.begin_draining(INSTALLATION, at(3))
        self.assertRefused("active-installation-exists", store.activate_installation, OTHER, at(4))
        store.retire_installation(INSTALLATION, "drained", at(5))
        store.activate_installation(OTHER, at(6))
        with store.read() as view:
            states = {status.installation_id: status.state for status in view.installations()}
        self.assertEqual(states, {INSTALLATION: "retired", OTHER: "active"})

    def test_lifecycle_events_are_append_only_and_ordered(self) -> None:
        store = self.open()
        self.enrolled(store, INSTALLATION, 0)
        store.activate_installation(INSTALLATION, at(1))
        store.begin_draining(INSTALLATION, at(2))
        store.retire_installation(INSTALLATION, "drained", at(3))
        with store.read() as view:
            events = view.installation_events(INSTALLATION)
            status = view.installation(INSTALLATION)
        self.assertEqual(
            [(event.state, event.at, event.retirement_kind) for event in events],
            [("pending", at(0), None), ("active", at(1), None), ("draining", at(2), None), ("retired", at(3), "drained")],
        )
        self.assertEqual(
            (status.enrolled_at, status.activated_at, status.draining_at, status.retired_at),
            (at(0), at(1), at(2), at(3)),
        )

    def test_new_installation_inherits_no_operational_state(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(1), batch())
        store.archive_sync_checkpoint(header(2, kind="sync-checkpoint"), checkpoint(1))
        store.begin_draining(INSTALLATION, at(3))
        store.retire_installation(INSTALLATION, "drained", at(4))
        self.active(store, OTHER, 5)
        with store.read() as view:
            replacement = view.installation(OTHER)
            retired = view.installation(INSTALLATION)
        self.assertIsNone(replacement.highest_sequence)
        self.assertIsNone(replacement.last_delivery_at)
        self.assertIsNone(replacement.latest_checkpoint_receipt_at)
        self.assertIsNone(replacement.background_scheduling)
        self.assertIsNone(replacement.background_scheduling_receipt_at)
        self.assertEqual(retired.highest_sequence, 2)
        self.assertEqual(retired.last_delivery_at, at(2))

    def test_timestamps_must_be_timezone_aware_and_are_returned_in_utc(self) -> None:
        store = self.open()
        naive = datetime(2026, 10, 12, 9, 0)
        self.assertRefused("timestamp-invalid", store.record_enrollment_offer, code("a"), at(30), naive)
        self.assertRefused("timestamp-invalid", store.record_enrollment_offer, code("a"), naive, at(0))
        india = timezone(timedelta(hours=5, minutes=30))
        local = datetime(2026, 10, 12, 14, 30, 0, 654321, tzinfo=india)
        store.record_enrollment_offer(code("a"), local + timedelta(minutes=30), local)
        store.enroll_installation(code("a"), INSTALLATION, fingerprint(INSTALLATION), local)
        self.assertRefused("timestamp-invalid", store.activate_installation, INSTALLATION, naive)
        with store.read() as view:
            enrolled_at = view.installation(INSTALLATION).enrolled_at
        self.assertEqual(enrolled_at, local)
        self.assertEqual(enrolled_at.utcoffset(), timedelta(0))
        self.assertEqual(enrolled_at.microsecond, 654321)


class R6CapabilityGenerations(StoreCase):
    def test_generations_strictly_increase_with_exact_bytes(self) -> None:
        store = self.open()
        first = b'{"synthetic-advertisement":1,"bytes":"\\u00e9"}'
        second = b'{"synthetic-advertisement":2}'
        store.honor_generation(1, first, at(0))
        self.assertRefused("generation-not-monotonic", store.honor_generation, 1, second, at(1))
        store.honor_generation(3, second, at(2))
        self.assertRefused("generation-not-monotonic", store.honor_generation, 2, second, at(3))
        with store.read() as view:
            honored = view.honored_generations()
        self.assertEqual([(item.generation, item.advertisement, item.honored_at) for item in honored], [(1, first, at(0)), (3, second, at(2))])

    def test_generation_retires_only_after_later_acknowledgement(self) -> None:
        store = self.ready()
        store.honor_generation(2, b'{"synthetic-advertisement":2}', at(1))
        self.assertRefused("generation-still-required", store.stop_honoring_generation, 1, at(2))
        store.archive_capability_acknowledgement(header(1, kind="capability-acknowledgement", generation=1))
        self.assertRefused("generation-still-required", store.stop_honoring_generation, 1, at(3))
        store.archive_capability_acknowledgement(header(2, kind="capability-acknowledgement", generation=2))
        with store.read() as view:
            self.assertEqual(view.latest_acknowledged_generation(), 2)
        store.stop_honoring_generation(1, at(4))
        self.assertRefused("generation-still-required", store.stop_honoring_generation, 2, at(5))
        self.assertRefused("generation-unknown", store.stop_honoring_generation, 9, at(5))
        with store.read() as view:
            self.assertEqual([item.generation for item in view.honored_generations()], [2])
        self.assertRefused("generation-not-monotonic", store.honor_generation, 1, b'{"synthetic-advertisement":1}', at(6))

    def test_archival_under_stopped_generation_is_refused(self) -> None:
        store = self.ready()
        first = header(1, generation=1)
        store.archive_ingest_batch(first, batch())
        store.honor_generation(2, b'{"synthetic-advertisement":2}', at(1))
        store.archive_capability_acknowledgement(header(2, kind="capability-acknowledgement", generation=2))
        store.stop_honoring_generation(1, at(3))
        self.assertRefused("generation-unknown", store.archive_ingest_batch, header(3, generation=1), batch(page=2))
        self.assertRefused("generation-unknown", store.stop_honoring_generation, 1, at(4))
        self.assertEqual(store.archive_ingest_batch(header(1, generation=1, receipt_time=at(5)), batch()), first.receipt)
        store.archive_ingest_batch(header(3, generation=2), batch(page=2))

    def test_archival_under_unknown_generation_is_refused(self) -> None:
        store = self.ready()
        self.assertRefused("generation-unknown", store.archive_ingest_batch, header(1, generation=7), batch())
        with store.read() as view:
            self.assertIsNone(view.latest_acknowledged_generation())
            self.assertIsNone(view.installation(INSTALLATION).highest_sequence)


class R7AtomicExactArchival(StoreCase):
    def test_archive_commits_exact_envelope_and_receipt(self) -> None:
        store = self.ready()
        message = header(1, receipt_time=at(7))
        record = batch()
        self.assertEqual(store.archive_ingest_batch(message, record), message.receipt)
        with store.read() as view:
            archived = view.archived_message(INSTALLATION, 1)
            status = view.installation(INSTALLATION)
            self.assertIsNone(view.archived_message(INSTALLATION, 2))
        assert archived is not None
        self.assertEqual(archived.kind, "ingest-batch")
        self.assertEqual(archived.envelope, message.envelope)
        self.assertEqual(archived.receipt, message.receipt)
        self.assertEqual(archived.message_sha256, message.message_sha256)
        self.assertEqual(archived.staged_generation, 1)
        self.assertEqual(archived.receipt_time, at(7))
        self.assertEqual(archived.batch, record)
        self.assertIsNone(archived.completion)
        self.assertIsNone(archived.checkpoint)
        self.assertEqual((status.highest_sequence, status.last_delivery_at), (1, at(7)))

    def test_ingest_batch_items_are_stored_as_separate_exact_blobs(self) -> None:
        store = self.ready()
        items = (
            BatchItem(projection=b'{"synthetic-projection":1}'),
            BatchItem(retracted_identity="synthetic-identity-1"),
            BatchItem(projection=b"\x00\xff\xfe synthetic non-UTF-8 projection"),
            BatchItem(projection=b""),
        )
        store.archive_ingest_batch(header(1), batch(*items))
        with store.read() as view:
            self.assertEqual(view.archived_items(INSTALLATION, 1), items)
            self.assertEqual(view.archived_message(INSTALLATION, 1).batch.items, items)
        connection = self.external()
        columns = [
            (table, column[1])
            for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
            for column in connection.execute(f'PRAGMA table_info("{table}")')
        ]
        for item in items:
            if not item.projection:
                continue
            matches = sum(
                connection.execute(f'SELECT count(*) FROM "{table}" WHERE "{column}" = ?', (item.projection,)).fetchone()[0]
                for table, column in columns
            )
            self.assertEqual(matches, 1, item.projection)

    def test_record_types_reject_invalid_combinations(self) -> None:
        with self.assertRaises(ValueError):
            BatchItem()
        with self.assertRaises(ValueError):
            BatchItem(projection=b"synthetic", retracted_identity="synthetic-identity")
        with self.assertRaises(ValueError):
            CompletionRecord("snapshot", None, "steps", CYCLE, 1, 1, 1, "0" * 64)
        with self.assertRaises(ValueError):
            CompletionRecord("change", "initial", "steps", CYCLE, 1, 1, 1, "0" * 64)

    def install_fault_triggers(self) -> sqlite3.Connection:
        connection = self.external()
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        connection.execute("CREATE TABLE test_fault (remaining INTEGER NOT NULL)")
        connection.execute("INSERT INTO test_fault VALUES (1000000)")
        for table in tables:
            for event in ("INSERT", "UPDATE", "DELETE"):
                connection.execute(
                    f'CREATE TRIGGER "test_fault_{table}_{event}" AFTER {event} ON "{table}" BEGIN '
                    "UPDATE test_fault SET remaining = remaining - 1; "
                    "SELECT RAISE(ABORT, 'injected fault') WHERE (SELECT remaining FROM test_fault) <= 0; END"
                )
        return connection

    def assert_every_partial_failure_commits_nothing(self, faults: sqlite3.Connection, operation) -> None:
        failures = 0
        for writes_allowed in range(1, 200):
            faults.execute("UPDATE test_fault SET remaining = ?", (writes_allowed,))
            before = self.dump()
            try:
                operation()
            except Exception:
                failures += 1
                self.assertEqual(self.dump(), before, f"partial commit after {writes_allowed} writes")
                continue
            break
        else:
            self.fail("operation never completed")
        self.assertGreater(failures, 0)

    def test_failure_mid_transaction_commits_nothing(self) -> None:
        store = self.ready()
        items = tuple(BatchItem(projection=b'{"synthetic-projection":%d}' % index) for index in range(5))
        store.honor_generation(2, b'{"synthetic-advertisement":2}', T0)
        faults = self.install_fault_triggers()
        self.assert_every_partial_failure_commits_nothing(faults, lambda: store.archive_ingest_batch(header(1), batch(*items)))
        self.assert_every_partial_failure_commits_nothing(faults, lambda: store.archive_sync_checkpoint(header(3, kind="sync-checkpoint"), checkpoint(1)))
        self.assert_every_partial_failure_commits_nothing(faults, lambda: store.archive_completion(header(4, kind="snapshot-completion"), completion()))
        self.assert_every_partial_failure_commits_nothing(
            faults, lambda: store.archive_capability_acknowledgement(header(6, kind="capability-acknowledgement", generation=2))
        )
        with store.read() as view:
            status = view.installation(INSTALLATION)
            self.assertEqual(view.archived_items(INSTALLATION, 1), items)
            self.assertEqual(view.archived_message(INSTALLATION, 3).checkpoint, checkpoint(1))
            self.assertEqual(view.archived_message(INSTALLATION, 4).completion, completion())
            self.assertEqual(view.archived_message(INSTALLATION, 6).kind, "capability-acknowledgement")
            self.assertEqual(view.latest_acknowledged_generation(), 2)
            self.assertEqual([(gap.gap_after, gap.sequence) for gap in view.sequence_gaps(INSTALLATION)], [(1, 3), (4, 6)])
        self.assertEqual((status.highest_sequence, status.last_delivery_at), (6, at(6)))
        self.assertEqual((status.latest_checkpoint_receipt_at, status.background_scheduling), (at(3), "available"))

    def test_envelope_digest_mismatch_is_refused(self) -> None:
        store = self.ready()
        message = header(1)
        forged = MessageHeader(**{**{f.name: getattr(message, f.name) for f in fields(message)}, "message_sha256": digest(b"other")})
        self.assertRefused("message-digest-mismatch", store.archive_ingest_batch, forged, batch())
        with store.read() as view:
            self.assertIsNone(view.installation(INSTALLATION).highest_sequence)

    def test_archival_requires_delivering_installation(self) -> None:
        store = self.open()
        store.honor_generation(1, b'{"synthetic-advertisement":1}', T0)
        self.enrolled(store, INSTALLATION)
        self.assertRefused("installation-not-delivering", store.archive_ingest_batch, header(1), batch())
        store.activate_installation(INSTALLATION, at(1))
        store.archive_ingest_batch(header(1), batch())
        store.begin_draining(INSTALLATION, at(2))
        store.archive_ingest_batch(header(2), batch(page=2))
        store.retire_installation(INSTALLATION, "drained", at(3))
        self.assertRefused("installation-not-delivering", store.archive_ingest_batch, header(3), batch(page=3))
        self.enrolled(store, OTHER, 4)
        store.withdraw_installation(OTHER, at(5))
        self.assertRefused("installation-not-delivering", store.archive_ingest_batch, header(1, installation_id=OTHER), batch())
        self.assertRefused("installation-unknown", store.archive_ingest_batch, header(1, installation_id=THIRD), batch())

    def test_completion_records_counts_and_chain_once(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(1), batch())
        snapshot = completion(CYCLE, "snapshot")
        store.archive_completion(header(2, kind="snapshot-completion"), snapshot)
        self.assertRefused("cycle-already-completed", store.archive_completion, header(3, kind="change-completion"), completion(CYCLE, "change"))
        change = completion(OTHER_CYCLE, "change")
        store.archive_completion(header(3, kind="change-completion"), change)
        store.begin_draining(INSTALLATION, at(4))
        store.retire_installation(INSTALLATION, "drained", at(4))
        self.active(store, OTHER, 5)
        store.archive_completion(header(1, kind="snapshot-completion", installation_id=OTHER), snapshot)
        with store.read() as view:
            first = view.archived_message(INSTALLATION, 2)
            second = view.archived_message(INSTALLATION, 3)
            replacement = view.archived_message(OTHER, 1)
        self.assertEqual((first.kind, first.completion, first.batch), ("snapshot-completion", snapshot, None))
        self.assertEqual((second.kind, second.completion), ("change-completion", change))
        self.assertEqual(replacement.completion, snapshot)


class R8IdempotencyOrderingAndGaps(StoreCase):
    def test_identical_retry_returns_prior_receipt_without_write(self) -> None:
        store = self.ready()
        first = header(1, receipt_time=at(1))
        self.assertEqual(store.archive_ingest_batch(first, batch()), first.receipt)
        store.archive_ingest_batch(header(2, receipt_time=at(2)), batch(page=2))
        retry = header(1, receipt_time=at(9), receipt=b'{"synthetic-receipt":"retry"}')
        before = self.dump()
        self.assertEqual(store.archive_ingest_batch(retry, batch()), first.receipt)
        self.assertEqual(self.dump(), before)
        with store.read() as view:
            self.assertEqual(view.installation(INSTALLATION).last_delivery_at, at(2))
            self.assertEqual(view.archived_message(INSTALLATION, 1).receipt_time, at(1))

    def test_conflicting_bytes_at_archived_sequence_are_refused(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(1), batch())
        conflicting = header(1, data=envelope(1, salt="different"))
        self.assertRefused("sequence-content-conflict", store.archive_ingest_batch, conflicting, batch())
        with store.read() as view:
            self.assertEqual(view.archived_message(INSTALLATION, 1).envelope, envelope(1))

    def test_lower_unarchived_sequence_is_out_of_order(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(1), batch())
        store.archive_ingest_batch(header(3), batch(page=3))
        self.assertRefused("sequence-out-of-order", store.archive_ingest_batch, header(2), batch(page=2))
        with store.read() as view:
            self.assertIsNone(view.archived_message(INSTALLATION, 2))

    def test_gap_is_recorded_and_message_still_archived(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(2, receipt_time=at(2)), batch())
        store.archive_ingest_batch(header(3, receipt_time=at(3)), batch(page=2))
        store.archive_ingest_batch(header(6, receipt_time=at(6)), batch(page=3))
        with store.read() as view:
            gaps = view.sequence_gaps(INSTALLATION)
            replay = [sequence for sequence, _ in view.accepted_envelopes(INSTALLATION)]
        self.assertEqual([(gap.gap_after, gap.sequence, gap.detected_at) for gap in gaps], [(0, 2, at(2)), (3, 6, at(6))])
        self.assertEqual(replay, [2, 3, 6])


    def test_refusal_precedence_is_deterministic(self) -> None:
        store = self.ready()
        first = header(1)
        store.archive_ingest_batch(first, batch())
        store.archive_ingest_batch(header(3), batch(page=3))
        self.free.value = 0
        self.assertRefused("sequence-content-conflict", store.archive_ingest_batch, header(1, data=envelope(1, salt="x")), batch())
        self.assertRefused("sequence-out-of-order", store.archive_ingest_batch, header(2), batch(page=2))
        self.assertRefused("generation-unknown", store.archive_ingest_batch, header(4, generation=9), batch(page=4))
        self.assertRetryable("storage-reserve-exhausted", store.archive_ingest_batch, header(4), batch(page=4))
        self.free.value = PLENTY
        store.begin_draining(INSTALLATION, at(5))
        store.retire_installation(INSTALLATION, "drained", at(5))
        self.assertEqual(store.archive_ingest_batch(header(1, receipt_time=at(6)), batch()), first.receipt)
        self.assertRefused("sequence-content-conflict", store.archive_ingest_batch, header(1, data=envelope(1, salt="x")), batch())
        self.assertRefused("installation-not-delivering", store.archive_ingest_batch, header(4), batch(page=4))
        self.assertRefused("installation-unknown", store.archive_ingest_batch, header(1, installation_id=THIRD), batch())
        store.record_enrollment_offer(code("late"), at(10), at(0))
        store.enroll_installation(code("late"), OTHER, fingerprint(OTHER), at(1))
        self.assertRefused("enrollment-code-consumed", store.enroll_installation, code("late"), THIRD, fingerprint(THIRD), at(20))
        self.assertRefused("installation-unknown", store.retire_installation, THIRD, "drained", at(20))
        self.assertRefused("installation-transition-invalid", store.begin_draining, OTHER, at(20))


class R9DeliveryBlocksAndCheckpoints(StoreCase):
    def test_delivery_block_is_health_data_free_and_coalesced(self) -> None:
        store = self.ready()
        blocked = digest(b"synthetic-blocked-envelope")
        store.record_delivery_block(INSTALLATION, 4, blocked, "sequence-content-mismatch", at(1))
        store.record_delivery_block(INSTALLATION, 4, blocked, "sequence-content-mismatch", at(2))
        store.record_delivery_block(INSTALLATION, 4, blocked, "unsupported-wire-protocol", at(3))
        store.record_delivery_block(None, None, digest(b"synthetic-malformed"), "malformed-message", at(4))
        self.assertEqual(
            {field.name for field in fields(DeliveryBlockRecord)},
            {"installation_id", "sequence", "message_sha256", "failure", "first_seen_at", "last_seen_at", "count"},
        )
        with store.read() as view:
            mine = view.delivery_blocks(INSTALLATION)
            everything = view.delivery_blocks()
        self.assertEqual(
            sorted((block.failure, block.first_seen_at, block.last_seen_at, block.count) for block in mine),
            [("sequence-content-mismatch", at(1), at(2), 2), ("unsupported-wire-protocol", at(3), at(3), 1)],
        )
        self.assertEqual(len(everything), 3)
        self.assertIn((None, None, "malformed-message"), {(block.installation_id, block.sequence, block.failure) for block in everything})

    def test_checkpoint_history_is_retained_and_paginated(self) -> None:
        store = self.ready()
        records = {sequence: checkpoint(sequence) for sequence in range(1, 6)}
        for sequence, record in records.items():
            store.archive_sync_checkpoint(header(sequence, kind="sync-checkpoint"), record)
        with store.read() as view:
            newest = view.sync_checkpoints(INSTALLATION, limit=2)
            older = view.sync_checkpoints(INSTALLATION, before_sequence=4, limit=2)
            everything = view.sync_checkpoints(INSTALLATION)
        self.assertEqual([item.sequence for item in newest], [5, 4])
        self.assertEqual([item.sequence for item in older], [3, 2])
        self.assertEqual([item.sequence for item in everything], [5, 4, 3, 2, 1])
        self.assertEqual({item.sequence: item.record for item in everything}, records)
        self.assertEqual({item.sequence: item.receipt_time for item in everything}, {sequence: at(sequence) for sequence in records})

    def test_latest_checkpoint_facts_advance_only_on_new_checkpoint(self) -> None:
        store = self.ready()
        store.archive_sync_checkpoint(header(1, kind="sync-checkpoint", receipt_time=at(1)), checkpoint(1))
        store.archive_ingest_batch(header(2, receipt_time=at(2)), batch())
        store.archive_sync_checkpoint(header(1, kind="sync-checkpoint", receipt_time=at(3)), checkpoint(1))
        with store.read() as view:
            status = view.installation(INSTALLATION)
        self.assertEqual(status.last_delivery_at, at(2))
        self.assertEqual(status.latest_checkpoint_receipt_at, at(1))
        self.assertEqual((status.background_scheduling, status.background_scheduling_receipt_at), ("available", at(1)))
        store.archive_sync_checkpoint(header(3, kind="sync-checkpoint", receipt_time=at(4)), checkpoint(2, "restricted"))
        with store.read() as view:
            status = view.installation(INSTALLATION)
        self.assertEqual(status.latest_checkpoint_receipt_at, at(4))
        self.assertEqual((status.background_scheduling, status.background_scheduling_receipt_at), ("restricted", at(4)))


class R10StorageReserve(StoreCase):
    def test_archival_below_reserve_is_retryable_and_writes_nothing(self) -> None:
        store = self.ready()
        self.free.value = RESERVE - 1
        before = self.dump()
        attempts = (
            lambda: store.archive_ingest_batch(header(1), batch()),
            lambda: store.archive_completion(header(1, kind="snapshot-completion"), completion()),
            lambda: store.archive_sync_checkpoint(header(1, kind="sync-checkpoint"), checkpoint(1)),
            lambda: store.archive_capability_acknowledgement(header(1, kind="capability-acknowledgement")),
        )
        for attempt in attempts:
            self.free.paths.clear()
            self.assertRetryable("storage-reserve-exhausted", attempt)
            self.assertTrue(self.free.paths)
            self.assertEqual(set(self.free.paths), {os.fspath(self.path)})
        self.assertEqual(self.dump(), before)

    def test_non_archival_operations_remain_available_below_reserve(self) -> None:
        store = self.ready()
        self.free.value = 0
        store.record_delivery_block(INSTALLATION, 1, digest(b"synthetic"), "temporarily-unavailable", at(1))
        store.honor_generation(2, b'{"synthetic-advertisement":2}', at(1))
        store.begin_draining(INSTALLATION, at(2))
        store.retire_installation(INSTALLATION, "drained", at(3))
        self.active(store, OTHER, 4)
        with store.read() as view:
            self.assertEqual(view.installation(OTHER).state, "active")

    def test_identical_retry_below_reserve_returns_prior_receipt(self) -> None:
        store = self.ready()
        first = header(1)
        store.archive_ingest_batch(first, batch())
        self.free.value = 0
        self.assertEqual(store.archive_ingest_batch(header(1, receipt_time=at(5)), batch()), first.receipt)

    def test_storage_reserve_status_reports_admission(self) -> None:
        store = self.ready()
        self.free.value = RESERVE
        with store.read() as view:
            self.assertEqual(view.storage_reserve(), store_module.ReserveStatus(RESERVE, RESERVE, True))
        store.archive_ingest_batch(header(1), batch())
        with store.read() as view:
            self.free.value = RESERVE - 1
            self.assertEqual(view.storage_reserve(), store_module.ReserveStatus(RESERVE - 1, RESERVE, False))
            self.free.value = RESERVE + 1
            self.assertEqual(view.storage_reserve(), store_module.ReserveStatus(RESERVE + 1, RESERVE, True))


class R11CoherentReadsAndReplay(StoreCase):
    def test_view_is_isolated_from_later_commits(self) -> None:
        store = self.ready()
        store.archive_ingest_batch(header(1), batch())
        with store.read() as view:
            store.archive_ingest_batch(header(2), batch(page=2))
            store.record_delivery_block(INSTALLATION, 3, digest(b"synthetic"), "malformed-message", at(3))
            self.assertEqual(view.installation(INSTALLATION).highest_sequence, 1)
            self.assertEqual([sequence for sequence, _ in view.accepted_envelopes(INSTALLATION)], [1])
            self.assertIsNone(view.archived_message(INSTALLATION, 2))
            self.assertEqual(view.delivery_blocks(), ())
        with store.read() as fresh:
            self.assertEqual(fresh.installation(INSTALLATION).highest_sequence, 2)
            self.assertEqual(len(fresh.delivery_blocks()), 1)

    def test_accepted_envelopes_replay_in_sequence_order(self) -> None:
        store = self.ready()
        messages = {
            1: (header(1), "batch"),
            2: (header(2, kind="snapshot-completion"), "completion"),
            4: (header(4, kind="sync-checkpoint"), "checkpoint"),
            5: (header(5, kind="capability-acknowledgement"), "ack"),
        }
        store.archive_ingest_batch(messages[1][0], batch())
        store.archive_completion(messages[2][0], completion())
        store.archive_sync_checkpoint(messages[4][0], checkpoint(1))
        store.archive_capability_acknowledgement(messages[5][0])
        store.archive_ingest_batch(messages[1][0], batch())
        store.begin_draining(INSTALLATION, at(6))
        store.retire_installation(INSTALLATION, "drained", at(6))
        self.active(store, OTHER, 7)
        store.archive_ingest_batch(header(1, installation_id=OTHER), batch())
        with store.read() as view:
            replay = list(view.accepted_envelopes(INSTALLATION))
            other = list(view.accepted_envelopes(OTHER))
        self.assertEqual(replay, [(sequence, message.envelope) for sequence, (message, _) in sorted(messages.items())])
        self.assertEqual(other, [(1, envelope(1, installation_id=OTHER))])

    def test_installation_status_reports_operational_facts(self) -> None:
        store = self.ready()
        store.archive_sync_checkpoint(header(1, kind="sync-checkpoint", receipt_time=at(1)), checkpoint(1, "unregistered"))
        store.archive_ingest_batch(header(2, receipt_time=at(2)), batch())
        with store.read() as view:
            status = view.installation(INSTALLATION)
            self.assertEqual(view.installations(), (status,))
        self.assertEqual(status.installation_id, INSTALLATION)
        self.assertEqual(status.state, "active")
        self.assertEqual(status.activated_at, at(0))
        self.assertEqual(status.highest_sequence, 2)
        self.assertEqual(status.last_delivery_at, at(2))
        self.assertEqual(status.latest_checkpoint_receipt_at, at(1))
        self.assertEqual((status.background_scheduling, status.background_scheduling_receipt_at), ("unregistered", at(1)))
        self.assertIsNone(status.draining_at)
        self.assertIsNone(status.retired_at)
        self.assertIsNone(status.withdrawn_at)


class R12HealthDataFreeSurfaces(StoreCase):
    def test_operational_reads_and_refusals_never_contain_health_bytes(self) -> None:
        store = self.ready()
        marked = envelope(1, salt=HEALTH_MARKER.decode())
        items = (BatchItem(projection=b'{"value":"' + HEALTH_MARKER + b'"}'), BatchItem(retracted_identity=IDENTITY_MARKER))
        store.archive_ingest_batch(header(1, data=marked), batch(*items))
        store.archive_sync_checkpoint(header(2, kind="sync-checkpoint"), checkpoint(1))
        store.archive_ingest_batch(header(4), batch(page=2))
        store.record_delivery_block(INSTALLATION, 5, digest(b"synthetic"), "malformed-message", at(5))
        refusals = []
        for attempt in (
            lambda: store.archive_ingest_batch(header(1, data=marked + b" "), batch(*items)),
            lambda: store.archive_ingest_batch(header(3, data=marked), batch(*items)),
            lambda: store.archive_ingest_batch(header(9, data=marked, generation=9), batch(*items)),
        ):
            with self.assertRaises(StoreRefusal) as caught:
                attempt()
            refusals.append(caught.exception)
        with store.read() as view:
            operational = [
                view.installation(INSTALLATION),
                view.installations(),
                view.installation_events(INSTALLATION),
                view.sequence_gaps(INSTALLATION),
                view.delivery_blocks(),
                view.sync_checkpoints(INSTALLATION),
                view.storage_reserve(),
                view.settings(),
                store.settings(),
            ]
            self.assertIn(HEALTH_MARKER, view.archived_message(INSTALLATION, 1).envelope)
            self.assertIn(IDENTITY_MARKER, [item.retracted_identity for item in view.archived_items(INSTALLATION, 1)])
        rendered = repr(operational) + "".join(f"{refusal!s}{refusal!r}{refusal.args!r}" for refusal in refusals)
        self.assertNotIn(HEALTH_MARKER.decode(), rendered)
        self.assertNotIn(IDENTITY_MARKER, rendered)


if __name__ == "__main__":
    unittest.main()
