"""Unbound regressions for health-data-free errors and archival probe failures."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sqlite3
import tempfile
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
