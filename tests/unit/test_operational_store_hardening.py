"""Unbound regressions for health-data-free errors and archival probe failures."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from activity_tracker_portal import operational_store as store_module
from activity_tracker_portal.operational_store import (
    BatchItem,
    CompletionRecord,
    IngestBatchRecord,
    MessageHeader,
    OperationalStore,
    StoreRefusal,
    StoreRetryable,
    open_store,
)

T0 = datetime(2026, 10, 12, 9, tzinfo=timezone.utc)
PROJECTION = b'{"synthetic-health-value":123}'
IDENTITY = "synthetic-source-identity"
ENVELOPE = b"synthetic-envelope-content"
MARKERS = (PROJECTION.decode(), IDENTITY, ENVELOPE.decode())


def header(sequence: int) -> MessageHeader:
    return MessageHeader(
        installation_id="synthetic-installation",
        sequence=sequence,
        message_sha256=hashlib.sha256(ENVELOPE).hexdigest(),
        staged_generation=1,
        receipt_time=T0 + timedelta(minutes=sequence),
        envelope=ENVELOPE,
        receipt=b"synthetic-receipt",
    )


def batch() -> IngestBatchRecord:
    return IngestBatchRecord(
        record_type="steps",
        projection_schema_version=1,
        cycle_kind="initial-snapshot",
        cycle_id="synthetic-cycle",
        source_page_index=1,
        fragment_index=1,
        previous_chain=hashlib.sha256(b"synthetic-chain").hexdigest(),
        items=(BatchItem(projection=PROJECTION), BatchItem(retracted_identity=IDENTITY)),
    )


def completion() -> CompletionRecord:
    return CompletionRecord(
        completion_kind="snapshot",
        snapshot_kind="initial",
        record_type="steps",
        cycle_id="synthetic-cycle",
        page_count=1,
        fragment_count=1,
        record_count=2,
        final_chain=hashlib.sha256(b"synthetic-final-chain").hexdigest(),
    )


class OperationalStoreHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.free = Mock(return_value=10**12)

    def ready(self, *, default_probe: bool = False) -> OperationalStore:
        self.path = self.directory / ("default.sqlite3" if default_probe else "custom.sqlite3")
        store = open_store(self.path, reserve_bytes=1_000_000, free_bytes=None if default_probe else self.free)
        self.addCleanup(store.close)
        store.record_enrollment_offer("synthetic-code", T0 + timedelta(hours=1), T0)
        store.enroll_installation("synthetic-code", "synthetic-installation", "synthetic-fingerprint", T0)
        store.activate_installation("synthetic-installation", T0)
        store.honor_generation(1, b"synthetic-advertisement", T0)
        return store

    def dump(self) -> tuple[str, ...]:
        connection = sqlite3.connect(self.path)
        try:
            return tuple(connection.iterdump())
        finally:
            connection.close()

    @contextmanager
    def failing_probe(self, default_probe: bool) -> Iterator[Mock]:
        if default_probe:
            with patch.object(store_module.shutil, "disk_usage", side_effect=OSError(" | ".join(MARKERS))) as probe:
                yield probe
        else:
            self.free.side_effect = OSError(" | ".join(MARKERS))
            try:
                yield self.free
            finally:
                self.free.side_effect = None

    def assert_health_data_free(self, error: BaseException) -> None:
        pending = [error]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            for representation in (str(current), repr(current), repr(current.args)):
                for marker in MARKERS:
                    self.assertNotIn(marker, representation)
            for linked in (current.__cause__, current.__context__):
                if linked is not None:
                    pending.append(linked)

    def test_non_sqlite_file_is_refused_without_changes(self) -> None:
        path = self.directory / "non-sqlite.sqlite3"
        before = b"synthetic-non-sqlite-content" * 8
        path.write_bytes(before)
        with self.assertRaises(StoreRefusal) as caught:
            open_store(path, reserve_bytes=1_000_000, free_bytes=self.free)
        self.assertEqual(caught.exception.failure, "store-schema-unsupported")
        self.assertEqual(str(caught.exception), "store-schema-unsupported")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(path.read_bytes(), before)

    def test_corrupted_store_header_is_refused_without_changes(self) -> None:
        path = self.directory / "corrupted.sqlite3"
        with open_store(path, reserve_bytes=1_000_000, free_bytes=self.free):
            pass
        original = path.read_bytes()
        self.assertEqual(original[:16], b"SQLite format 3\x00")
        before = b"synthetic-header" + original[16:]
        path.write_bytes(before)
        with self.assertRaises(StoreRefusal) as caught:
            open_store(path, reserve_bytes=1_000_000, free_bytes=self.free)
        self.assertEqual(caught.exception.failure, "store-schema-unsupported")
        self.assertEqual(str(caught.exception), "store-schema-unsupported")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(path.read_bytes(), before)

    def test_one_byte_file_is_refused_without_changes(self) -> None:
        path = self.directory / "one-byte.sqlite3"
        before = b"x"
        path.write_bytes(before)
        with self.assertRaises(StoreRefusal) as caught:
            with open_store(path, reserve_bytes=1_000_000, free_bytes=self.free):
                pass
        self.assertEqual(caught.exception.failure, "store-schema-unsupported")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(path.read_bytes(), before)

    def test_store_truncated_before_complete_header_is_refused_without_changes(self) -> None:
        baseline = self.directory / "baseline.sqlite3"
        with open_store(baseline, reserve_bytes=1_000_000, free_bytes=self.free):
            pass
        original = baseline.read_bytes()
        self.assertEqual(original[:16], b"SQLite format 3\x00")
        for length in (1, 15):
            with self.subTest(length=length):
                path = self.directory / f"truncated-{length}.sqlite3"
                before = original[:length]
                path.write_bytes(before)
                with self.assertRaises(StoreRefusal) as caught:
                    with open_store(path, reserve_bytes=1_000_000, free_bytes=self.free):
                        pass
                self.assertEqual(caught.exception.failure, "store-schema-unsupported")
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)
                self.assertEqual(path.read_bytes(), before)

    def test_zero_length_file_is_created_at_baseline(self) -> None:
        path = self.directory / "empty.sqlite3"
        path.touch()
        with open_store(path, reserve_bytes=1_000_000, free_bytes=self.free) as store:
            self.assertEqual(store.settings().schema_migrations, ((
                1, hashlib.sha256(store_module.MIGRATIONS[0][1].encode("utf-8")).hexdigest(),
            ),))
        connection = sqlite3.connect(path)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
        finally:
            connection.close()

    def test_competing_foreign_schema_is_refused_before_journal_mode_changes(self) -> None:
        path = self.directory / "foreign-race.sqlite3"
        path.touch()
        configure = store_module._configure
        committed: list[bytes] = []

        def competing_commit(connection: sqlite3.Connection, busy_timeout_ms: int) -> None:
            # The first validation's read transaction has ended, but creation has not begun.
            other = sqlite3.connect(path, isolation_level=None)
            try:
                self.assertEqual(other.execute("PRAGMA journal_mode").fetchone()[0], "delete")
                other.execute("BEGIN IMMEDIATE")
                other.execute("CREATE TABLE foreign_data(value TEXT)")
                other.execute("INSERT INTO foreign_data VALUES ('synthetic')")
                other.commit()
            finally:
                other.close()
            committed.append(path.read_bytes())
            configure(connection, busy_timeout_ms)

        with patch.object(store_module, "_configure", competing_commit):
            with self.assertRaises(StoreRefusal) as caught:
                with open_store(path, reserve_bytes=1_000_000, free_bytes=self.free):
                    pass
        self.assertEqual(caught.exception.failure, "store-schema-unsupported")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        connection = sqlite3.connect(path)
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        finally:
            connection.close()
        self.assertEqual(path.read_bytes(), committed[0])

    def test_concurrent_creation_retries_wal_until_writer_releases_within_timeout(self) -> None:
        path = self.directory / "wal-creation-race.sqlite3"
        empty_validated = threading.Event()
        baseline_committed = threading.Event()
        writer_locked = threading.Event()
        wal_attempted = threading.Event()
        writer_released = threading.Event()
        role = threading.local()
        execute = store_module._execute
        validate = store_module._validate_schema
        wal_attempts: list[float] = []

        def execute_hook(
            connection: sqlite3.Connection, sql: str, parameters: tuple[object, ...] = (),
        ) -> sqlite3.Cursor:
            if role.name == "B" and sql == "BEGIN IMMEDIATE":
                self.assertTrue(baseline_committed.wait(5))
            if role.name == "A" and sql == "PRAGMA journal_mode = WAL":
                wal_attempts.append(time.monotonic())
                try:
                    return execute(connection, sql, parameters)
                finally:
                    # B holds a real writer lock through A's first WAL attempt.
                    wal_attempted.set()
                    self.assertTrue(writer_released.wait(5))
            result = execute(connection, sql, parameters)
            if role.name == "A" and sql == "COMMIT":
                baseline_committed.set()
                self.assertTrue(writer_locked.wait(5))
            if role.name == "B" and sql == "BEGIN IMMEDIATE":
                writer_locked.set()
                self.assertTrue(wal_attempted.wait(5))
            if role.name == "B" and sql == "COMMIT":
                writer_released.set()
            return result

        def validate_hook(connection: sqlite3.Connection, database_path: Path) -> bool:
            existing = validate(connection, database_path)
            if role.name == "B" and not empty_validated.is_set():
                self.assertFalse(existing)
                empty_validated.set()
            return existing

        def opener(name: str) -> store_module.StoreSettings:
            role.name = name
            with open_store(path, reserve_bytes=0, busy_timeout_ms=500) as store:
                return store.settings()

        with patch.object(store_module, "_execute", execute_hook), patch.object(
            store_module, "_validate_schema", validate_hook,
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                try:
                    second = pool.submit(opener, "B")
                    self.assertTrue(empty_validated.wait(5))
                    first = pool.submit(opener, "A")
                    settings = (first.result(timeout=5), second.result(timeout=5))
                finally:
                    # Release scheduling barriers even if an assertion fails.
                    baseline_committed.set()
                    writer_locked.set()
                    wal_attempted.set()
                    writer_released.set()
        self.assertGreaterEqual(len(wal_attempts), 2)
        for opened in settings:
            self.assertEqual(opened.journal_mode, "wal")
            self.assertEqual(opened.synchronous, 2)
            self.assertTrue(opened.foreign_keys)
            self.assertEqual(opened.busy_timeout_ms, 500)

    def test_wal_switch_waits_for_timeout_without_exception_chain(self) -> None:
        path = self.directory / "wal-timeout.sqlite3"
        with open_store(path, reserve_bytes=0):
            pass
        blocker = sqlite3.connect(path, isolation_level=None)
        self.addCleanup(blocker.close)
        self.assertEqual(blocker.execute("PRAGMA journal_mode = DELETE").fetchone()[0], "delete")
        blocker.execute("BEGIN IMMEDIATE")
        busy_timeout_ms = 50
        execute = store_module._execute
        wal_attempts: list[float] = []

        def execute_hook(
            connection: sqlite3.Connection, sql: str, parameters: tuple[object, ...] = (),
        ) -> sqlite3.Cursor:
            if sql == "PRAGMA journal_mode = WAL":
                wal_attempts.append(time.monotonic())
            return execute(connection, sql, parameters)

        try:
            with patch.object(store_module, "_execute", execute_hook):
                with self.assertRaises(StoreRetryable) as caught:
                    with open_store(path, reserve_bytes=0, busy_timeout_ms=busy_timeout_ms):
                        pass
            elapsed = time.monotonic() - wal_attempts[0]
            self.assertGreaterEqual(elapsed, busy_timeout_ms / 1000)
            self.assertLess(elapsed, 1)
            self.assertGreaterEqual(len(wal_attempts), 2)
            self.assertEqual(caught.exception.failure, "store-busy")
            self.assertEqual(str(caught.exception), "store-busy")
            self.assertIsNone(caught.exception.__cause__)
            self.assertIsNone(caught.exception.__context__)
        finally:
            blocker.rollback()

    def test_trigger_error_does_not_expose_archived_health_data(self) -> None:
        store = self.ready()
        connection = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TRIGGER test_health_fault AFTER INSERT ON batch_items "
            "WHEN NEW.retracted_identity IS NOT NULL BEGIN "
            "SELECT RAISE(ABORT, "
            "(SELECT CAST(projection AS TEXT) FROM batch_items "
            "WHERE installation_id = NEW.installation_id AND sequence = NEW.sequence AND item_index = 0) "
            "|| ' | ' || NEW.retracted_identity || ' | ' || "
            "(SELECT CAST(envelope AS TEXT) FROM archived_messages "
            "WHERE installation_id = NEW.installation_id AND sequence = NEW.sequence)); END"
        )
        before = self.dump()
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            store.archive_ingest_batch(header(1), batch())
        self.assertNotIsInstance(caught.exception, StoreRetryable)
        self.assert_health_data_free(caught.exception)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(self.dump(), before)

    def test_identical_retry_returns_receipt_when_probe_fails(self) -> None:
        for default_probe in (False, True):
            with self.subTest(default_probe=default_probe):
                store = self.ready(default_probe=default_probe)
                first = header(1)
                store.archive_ingest_batch(first, batch())
                store.retire_installation(first.installation_id, "destructive", T0 + timedelta(hours=1))
                retry = replace(first, staged_generation=999, receipt=b"synthetic-other-receipt")
                before = self.dump()
                self.free.reset_mock()
                with self.failing_probe(default_probe) as probe:
                    self.assertEqual(store.archive_capability_acknowledgement(retry), first.receipt)
                    probe.assert_called_once_with(str(self.directory) if default_probe else self.path)
                self.assertEqual(self.dump(), before)

    def test_earlier_refusals_win_when_probe_fails(self) -> None:
        for default_probe in (False, True):
            with self.subTest(default_probe=default_probe):
                store = self.ready(default_probe=default_probe)
                store.archive_ingest_batch(header(3), batch())
                store.archive_completion(header(4), completion())
                store.record_enrollment_offer("synthetic-pending-code", T0 + timedelta(hours=1), T0)
                store.enroll_installation("synthetic-pending-code", "synthetic-pending", "synthetic-fingerprint", T0)
                changed = b"synthetic-different-envelope"
                cases = (
                    ("timestamp-invalid", replace(header(5), receipt_time=T0.replace(tzinfo=None))),
                    ("installation-unknown", replace(header(5), installation_id="synthetic-unknown")),
                    ("message-digest-mismatch", replace(header(5), message_sha256="0" * 64)),
                    ("sequence-content-conflict", replace(
                        header(3), envelope=changed, message_sha256=hashlib.sha256(changed).hexdigest(),
                    )),
                    ("installation-not-delivering", replace(header(5), installation_id="synthetic-pending")),
                    ("generation-unknown", replace(header(5), staged_generation=2)),
                    ("sequence-out-of-order", header(2)),
                    ("cycle-already-completed", header(5)),
                )
                before = self.dump()
                for failure, attempt in cases:
                    with self.subTest(failure=failure):
                        self.free.reset_mock()
                        with self.failing_probe(default_probe) as probe:
                            with self.assertRaises(StoreRefusal) as caught:
                                store.archive_completion(attempt, completion())
                            self.assertEqual(caught.exception.failure, failure)
                            self.assert_health_data_free(caught.exception)
                            probe.assert_called_once_with(str(self.directory) if default_probe else self.path)
                        self.assertEqual(self.dump(), before)

    def test_new_write_fails_without_commit_when_probe_fails(self) -> None:
        for default_probe in (False, True):
            with self.subTest(default_probe=default_probe):
                store = self.ready(default_probe=default_probe)
                before = self.dump()
                self.free.reset_mock()
                with self.failing_probe(default_probe) as probe:
                    with self.assertRaises(OSError) as caught:
                        store.archive_ingest_batch(header(1), batch())
                    self.assert_health_data_free(caught.exception)
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertIsNone(caught.exception.__context__)
                    probe.assert_called_once_with(str(self.directory) if default_probe else self.path)
                self.assertEqual(self.dump(), before)
                with store.read() as view:
                    self.assertEqual(tuple(view.accepted_envelopes("synthetic-installation")), ())
                    self.assertIsNone(view.installation("synthetic-installation").last_delivery_at)
                self.assertEqual(store.archive_ingest_batch(header(1), batch()), header(1).receipt)


if __name__ == "__main__":
    unittest.main()
