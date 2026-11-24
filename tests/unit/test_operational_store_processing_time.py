"""Unbound regressions for elapsed lease durations across DST transitions."""

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from activity_tracker_portal.operational_store import (
    BatchItem,
    IngestBatchRecord,
    ItemPosition,
    MessageHeader,
    ProjectionEntry,
    SourceKey,
    open_store,
)

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
SETUP_AT = datetime(2026, 1, 1, tzinfo=UTC)
CASES = (
    ("second-fold", datetime(2026, 11, 1, 1, 30, tzinfo=NEW_YORK, fold=1), 60),
    ("cross-fold", datetime(2026, 11, 1, 1, 59, 30, tzinfo=NEW_YORK), 60),
    ("cross-gap", datetime(2026, 3, 8, 1, 59, 30, tzinfo=NEW_YORK), 7200),
)


class ProcessingLeaseTimeTests(unittest.TestCase):
    def ready(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = open_store(Path(directory.name) / "store.sqlite3", reserve_bytes=0, free_bytes=lambda _: 10**12)
        self.addCleanup(store.close)
        store.record_enrollment_offer("synthetic-code", SETUP_AT + timedelta(hours=1), SETUP_AT)
        store.enroll_installation("synthetic-code", "synthetic-installation", "synthetic-fingerprint", SETUP_AT)
        store.activate_installation("synthetic-installation", SETUP_AT)
        store.honor_generation(1, b"synthetic-advertisement", SETUP_AT)
        envelope = b"synthetic-envelope"
        store.archive_ingest_batch(
            MessageHeader(
                installation_id="synthetic-installation", sequence=1,
                message_sha256=hashlib.sha256(envelope).hexdigest(), staged_generation=1,
                receipt_time=SETUP_AT, envelope=envelope, receipt=b"synthetic-receipt",
            ),
            IngestBatchRecord(
                record_type="steps", projection_schema_version=1, cycle_kind="initial-snapshot",
                cycle_id="synthetic-cycle", source_page_index=1, fragment_index=1,
                previous_chain=hashlib.sha256(b"synthetic-chain").hexdigest(),
                items=(BatchItem(projection=b"synthetic-projection"),),
            ),
        )
        store.index_items((ProjectionEntry(
            ItemPosition("synthetic-installation", 1, 0), "health-connect", "synthetic-origin",
            "synthetic-identity", SETUP_AT,
        ),), SETUP_AT)
        with store.read() as view:
            record_id = view.source_record_id(SourceKey(
                "health-connect", "synthetic-origin", "steps", "synthetic-identity",
            ))
            self.assertIsNotNone(record_id)
            record = view.source_record(record_id)
        selection = store.record_selection(
            record_id, "projection", (record.evidence[-1].evidence_id,), record.evidence_head, SETUP_AT,
        )
        job = store.enqueue_processing(record_id, selection.selection_id, "interpret-steps@1", 3, SETUP_AT)
        return store, job.job_id

    def assert_held(self, store, lease, at, seconds):
        expected = at.astimezone(UTC) + timedelta(seconds=seconds)
        self.assertEqual(lease.expires_at, expected)
        with store.read() as view:
            job = view.processing_job(lease.job_id)
            self.assertEqual(job.lease_expires_at, expected)
            self.assertEqual(job.state, "leased")
            self.assertEqual(job.fence, lease.fence)
            counts = view.processing_counts(at)
            self.assertEqual(counts.jobs_leased, 1)
            self.assertEqual(counts.jobs_lease_expired, 0)
        # A competing worker cannot reclaim at the caller's instant or just
        # before the full elapsed duration, including across the transition.
        self.assertIsNone(store.claim_processing_job("synthetic-other-worker", 60, at))
        self.assertIsNone(store.claim_processing_job(
            "synthetic-other-worker", 60, expected - timedelta(microseconds=1),
        ))
        reclaimed = store.claim_processing_job("synthetic-other-worker", 60, expected)
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed.job_id, lease.job_id)
        self.assertEqual(reclaimed.fence, lease.fence + 1)

    def test_claim_uses_elapsed_seconds_across_dst(self):
        for name, at, seconds in CASES:
            with self.subTest(name=name):
                store, job_id = self.ready()
                lease = store.claim_processing_job("synthetic-worker", seconds, at)
                self.assertIsNotNone(lease)
                self.assertEqual(lease.job_id, job_id)
                with store.read() as view:
                    attempt = view.processing_job(job_id).history[-1]
                self.assertEqual(attempt.claimed_at, at.astimezone(UTC))
                self.assertEqual(attempt.expires_at, at.astimezone(UTC) + timedelta(seconds=seconds))
                self.assert_held(store, lease, at, seconds)

    def test_renew_uses_elapsed_seconds_across_dst(self):
        for name, at, seconds in CASES:
            with self.subTest(name=name):
                store, job_id = self.ready()
                claimed = store.claim_processing_job(
                    "synthetic-worker", 60, at.astimezone(UTC) - timedelta(seconds=30),
                )
                self.assertIsNotNone(claimed)
                renewed = store.renew_processing_lease(job_id, claimed.fence, seconds, at)
                self.assertEqual(renewed.fence, claimed.fence)
                with store.read() as view:
                    attempt = view.processing_job(job_id).history[-1]
                self.assertEqual(attempt.expires_at, claimed.expires_at)
                self.assert_held(store, renewed, at, seconds)


if __name__ == "__main__":
    unittest.main()
