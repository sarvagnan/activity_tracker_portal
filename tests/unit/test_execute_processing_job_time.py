"""Unbound regressions for elapsed retry delays with timezone-aware clocks."""

from datetime import datetime, timedelta, timezone
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from activity_tracker_portal.execute_processing_job import execute_processing_job
from activity_tracker_portal.interpret_source_projection import decode_envelope
from activity_tracker_portal.operational_store import (
    BatchItem,
    IngestBatchRecord,
    ItemPosition,
    MessageHeader,
    ProjectionEntry,
    SourceKey,
    open_store,
)

ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc
SETUP_AT = datetime(2026, 1, 1, tzinfo=UTC)
VERSION = "interpret-synthetic@1"
TRANSIENT = "interpretation-unexpected-failure"


class TransientProcessor:
    """An injected processor whose schema-version lookup fails unexpectedly."""

    record_type = "steps"
    processor_version = VERSION

    @property
    def supported_schema_versions(self):
        raise RuntimeError("synthetic-transient-failure")


class ExecuteProcessingJobTimeTests(unittest.TestCase):
    def ready(self, at):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = open_store(Path(directory.name) / "store.sqlite3", reserve_bytes=0, free_bytes=lambda _: 10**12)
        self.addCleanup(store.close)
        store.record_enrollment_offer("synthetic-code", SETUP_AT + timedelta(hours=1), SETUP_AT)
        store.enroll_installation("synthetic-code", "synthetic-installation", "synthetic-fingerprint", SETUP_AT)
        store.activate_installation("synthetic-installation", SETUP_AT)
        store.honor_generation(1, b"synthetic-advertisement", SETUP_AT)
        vectors = json.loads((ROOT / "projection/v1/vectors/positive.json").read_text())["vectors"]
        projection = next(item["bytes"].encode() for item in vectors if item["name"] == "steps-maximal")
        envelope = decode_envelope(projection, "steps", 1)
        archive = b"synthetic-envelope"
        store.archive_ingest_batch(
            MessageHeader(
                installation_id="synthetic-installation", sequence=1,
                message_sha256=hashlib.sha256(archive).hexdigest(), staged_generation=1,
                receipt_time=SETUP_AT, envelope=archive, receipt=b"synthetic-receipt",
            ),
            IngestBatchRecord(
                record_type="steps", projection_schema_version=1, cycle_kind="initial-snapshot",
                cycle_id="synthetic-cycle", source_page_index=1, fragment_index=1,
                previous_chain=hashlib.sha256(b"synthetic-chain").hexdigest(),
                items=(BatchItem(projection=projection),),
            ),
        )
        store.index_items((ProjectionEntry(
            ItemPosition("synthetic-installation", 1, 0), envelope.source_system_kind, envelope.origin,
            envelope.source_identity, envelope.last_modified_time,
        ),), SETUP_AT)
        with store.read() as view:
            record_id = view.source_record_id(SourceKey(
                envelope.source_system_kind, envelope.origin, "steps", envelope.source_identity,
            ))
            self.assertIsNotNone(record_id)
            record = view.source_record(record_id)
        selection = store.record_selection(
            record_id, "projection", (record.evidence[-1].evidence_id,), record.evidence_head, SETUP_AT,
        )
        job = store.enqueue_processing(record_id, selection.selection_id, VERSION, 3, SETUP_AT)
        lease = store.claim_processing_job("synthetic-worker", 300, at.astimezone(UTC) - timedelta(seconds=5))
        self.assertIsNotNone(lease)
        self.assertEqual(lease.job_id, job.job_id)
        return store, lease

    def assert_elapsed_retry(self, at, expected_retry):
        store, lease = self.ready(at)
        result = execute_processing_job(store, lease, lambda: at, processors={VERSION: TransientProcessor()})
        self.assertEqual(result.disposition, "retry-scheduled")
        self.assertEqual(result.reason, TRANSIENT)
        self.assertEqual(result.retry_at, expected_retry)
        with store.read() as view:
            job = view.processing_job(lease.job_id)
            self.assertEqual(job.state, "queued")
            self.assertEqual(job.not_before, expected_retry)
            self.assertIsNone(job.lease_owner)
            self.assertIsNone(job.lease_expires_at)
            self.assertEqual(job.attempts, 1)
            self.assertEqual(len(job.history), 1)
            attempt = job.history[0]
            self.assertEqual(attempt.result, "transient-failure")
            self.assertEqual(attempt.reason, TRANSIENT)
            self.assertEqual(attempt.finished_at, at.astimezone(UTC))
            self.assertEqual(job.not_before - attempt.finished_at, timedelta(seconds=60))
            self.assertEqual(view.processing_outcomes(lease.record_id), ())
        self.assertIsNone(store.claim_processing_job(
            "synthetic-other-worker", 300, expected_retry - timedelta(microseconds=1),
        ))
        reclaimed = store.claim_processing_job("synthetic-other-worker", 300, expected_retry)
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed.job_id, lease.job_id)
        self.assertEqual(reclaimed.fence, lease.fence + 1)

    def test_transient_failure_requeues_after_elapsed_minute_in_second_dst_fold(self):
        # Read the pinned tzdata package directly, independent of the host TZPATH.
        with files("tzdata.zoneinfo").joinpath("America", "New_York").open("rb") as stream:
            new_york = ZoneInfo.from_file(stream, key="America/New_York")
        at = datetime(2026, 11, 1, 1, 30, tzinfo=new_york, fold=1)
        self.assert_elapsed_retry(at, datetime(2026, 11, 1, 6, 31, tzinfo=UTC))

    def test_transient_failure_requeues_after_elapsed_minute_with_fixed_offset(self):
        at = datetime(2026, 11, 1, 12, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        self.assert_elapsed_retry(at, datetime(2026, 11, 1, 6, 31, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
