"""Unbound regression checks for the operational-store@6 implementation review."""

from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sqlite3
import traceback
import unittest
from unittest.mock import patch

from activity_tracker_portal import operational_store as m

_SPEC = importlib.util.spec_from_file_location(
    "os6_review_fixtures", Path(__file__).parents[1] / "acceptance/test_operational_store_6.py")
fixtures = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fixtures)
MARKER = "SYNTHETIC-PRIVATE-SUMMARY-93842"


def clone_records(db, count):
    """Clone synthetic relational rows in one transaction, without per-record API writes."""
    tables = ("archived_messages", "ingest_batches", "batch_items", "source_records", "source_evidence",
              "source_selections", "selection_evidence", "processing_jobs", "processing_attempts",
              "processing_outcomes", "canonical_revisions")
    db.execute("BEGIN IMMEDIATE")
    for table in tables:
        columns = [row[1] for row in db.execute("PRAGMA table_info(" + table + ")")]
        base = dict(db.execute("SELECT * FROM " + table + " LIMIT 1").fetchone())
        rows = []
        for identity in range(2, count + 1):
            row = base.copy()
            for name in ("sequence", "record_id", "evidence_id", "selection_id", "evidence_head", "job_id", "outcome_id"):
                if name in row:
                    row[name] = identity
            if "source_identity" in row:
                row["source_identity"] = "synthetic-" + str(identity)
            if "source_page_index" in row:
                row["source_page_index"] = identity
            rows.append(tuple(row[name] for name in columns))
        db.executemany("INSERT INTO " + table + " (" + ",".join(columns) + ") VALUES (" +
                       ",".join("?" for _ in columns) + ")", rows)
    db.execute("UPDATE installations SET highest_sequence = ?", (count,))
    db.execute("COMMIT")


class OperationalStore6ReviewTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.StoreCase()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.store, self.generation, self.outcome = self.case.seeded()
        self.target = m.SummaryTarget(self.generation.generation_id, 0)
        self.key = self.case.key()

    def assert_clean_failure(self, operation, expected_type, expected_name):
        try:
            operation()
        except expected_type as error:
            self.assertEqual(error.args, (expected_name,))
            self.assertIsNone(error.__context__)
            self.assertIsNone(error.__cause__)
            tb = error.__traceback__
            while tb is not None and tb.tb_frame.f_globals.get("__name__") != m.__name__:
                tb = tb.tb_next
            self.assertIsNotNone(tb)
            rendered = "".join(traceback.TracebackException(
                type(error), error, tb, capture_locals=True).format())
            self.assertNotIn(MARKER, rendered)
            while tb is not None:
                self.assertEqual(tb.tb_frame.f_code.co_name, "checked")
                tb = tb.tb_next
        else:
            self.fail("operation did not fail")

    def check_both_caller_contexts(self, operation, expected_type, expected_name):
        self.assert_clean_failure(operation, expected_type, expected_name)
        try:
            json.loads(MARKER)
        except json.JSONDecodeError:
            self.assert_clean_failure(operation, expected_type, expected_name)

    def test_all_seventeen_boundaries_discard_inputs_context_and_internal_frames(self):
        c, s = self.case, self.store
        lease = c.leased(s, self.key, self.target)
        entry = c.entry(s, self.key, label=MARKER.encode(), contributors=(self.outcome.outcome_id,))
        with s.read() as view:
            operations = (
                lambda: s.register_processor_generation({MARKER: fixtures.PV}, c.tick()),
                lambda: s.request_summary_refresh((m.SummaryKey(fixtures.MEASURE, MARKER, fixtures.DAY),), self.target, 3, c.tick()),
                lambda: s.claim_summary_refresh(MARKER, 60, c.tick()),
                lambda: s.renew_summary_refresh_lease(lease.job_id, lease.fence, 60, c.tick()),
                lambda: s.complete_summary_refresh(lease.job_id, lease.fence, entry, c.tick()),
                lambda: s.fail_summary_refresh(lease.job_id, lease.fence, "deterministic", "synthetic-defect", None, c.tick()),
                lambda: view.processor_generation(self.generation.generation_id),
                lambda: view.processor_generations(),
                lambda: view.summary_refresh_job(lease.job_id),
                lambda: view.summary_refresh_jobs(self.key),
                lambda: view.summaries(self.key),
                lambda: view.latest_summary(self.key, self.target),
                lambda: view.policies_awaiting_refresh(),
                lambda: view.evidence_changes(10),
                lambda: view.processed_positions(MARKER, fixtures.PV),
                lambda: view.summary_evidence(fixtures.MEASURE, MARKER, fixtures.DAY, fixtures.DAY, self.target, 0, 10),
                lambda: view.refresh_counts(c.tick()),
            )
            failures = ((m.StoreRefusal("summary-request-invalid"), m.StoreRefusal, "summary-request-invalid"),
                        (m.StoreRetryable("store-busy"), m.StoreRetryable, "store-busy"),
                        (sqlite3.OperationalError(MARKER), sqlite3.OperationalError, "store-database-error"),
                        (OSError(MARKER), OSError, "store-capacity-unavailable"),
                        (RuntimeError(MARKER), RuntimeError, "store-database-error"))
            for index, operation in enumerate(operations):
                for failure, expected_type, name in failures:
                    with self.subTest(operation=index, failure=expected_type.__name__):
                        with patch.object(m, "_execute", side_effect=failure):
                            self.check_both_caller_contexts(operation, expected_type, name)

    def test_validation_and_sql_trigger_failures_drop_summary_bytes(self):
        c, s = self.case, self.store
        lease = c.leased(s, self.key, self.target)
        self.check_both_caller_contexts(
            lambda: s.complete_summary_refresh(lease.job_id, lease.fence, MARKER.encode(), c.tick()),
            m.StoreRefusal, "summary-entry-invalid")
        entry = c.entry(s, self.key, label=MARKER.encode(), contributors=(self.outcome.outcome_id,))
        s._connection.execute("CREATE TRIGGER review_fail BEFORE INSERT ON health_summaries "
                              "BEGIN SELECT RAISE(ABORT, '" + MARKER + "'); END")
        self.check_both_caller_contexts(
            lambda: s.complete_summary_refresh(lease.job_id, lease.fence, entry, c.tick()),
            sqlite3.IntegrityError, "store-database-error")
        self.assertFalse(s._connection.in_transaction)
        self.assertEqual(s._connection.execute("SELECT count(*) FROM health_summaries").fetchone()[0], 0)

    def test_argument_binding_remains_type_error_before_any_query(self):
        with patch.object(m, "_execute") as query:
            with self.assertRaises(TypeError):
                self.store.request_summary_refresh(unexpected=MARKER)
            with self.assertRaises(TypeError):
                self.store.complete_summary_refresh(1, 2, at=fixtures.T0)
            query.assert_not_called()

    def test_obligations_query_uses_identity_covering_index(self):
        plan = self.store._connection.execute(
            "EXPLAIN QUERY PLAN " + m._PROCESSED_PREFIX_SQL + "SELECT * FROM positions",
            m._processed_parameters("steps", fixtures.PV)).fetchall()
        details = "\n".join(row[3] for row in plan)
        self.assertIn("USING COVERING INDEX summary_retraction_identity (record_type=? AND source_identity=?)", details)

    def test_evidence_page_exceeding_runtime_parameter_limit(self):
        db = self.store._connection
        count = 16_384  # Two distinct outcomes per record exceed 32,766 parameters.
        clone_records(db, count)
        db.execute("BEGIN IMMEDIATE")
        for table in ("processing_jobs", "processing_attempts", "processing_outcomes"):
            columns = [row[1] for row in db.execute("PRAGMA table_info(" + table + ")")]
            rows = []
            for original in db.execute("SELECT * FROM " + table).fetchall():
                row = dict(original)
                row["job_id"] += count
                if table == "processing_outcomes":
                    row.update(outcome_id=row["outcome_id"] + count, policy_identity="synthetic-next",
                               kind="unmodeled", reason="synthetic-unmodeled")
                rows.append(tuple(row[name] for name in columns))
            db.executemany("INSERT INTO " + table + " (" + ",".join(columns) + ") VALUES (" +
                           ",".join("?" for _ in columns) + ")", rows)
        db.execute("COMMIT")
        self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        with self.store.read() as view:
            # Pin the reproducer's ceiling even on a differently compiled SQLite.
            view._connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 32_766)
            result = view.summary_evidence(fixtures.MEASURE, "steps", fixtures.DAY, fixtures.DAY, self.target, 0, count)
        self.assertEqual(len(result.items), count)
        self.assertIsNone(result.next_after)
        self.assertEqual(result.items[0].outcome.kind, "unmodeled")
        self.assertEqual(result.items[-1].last_revision.outcome_id, count)

    def test_contributors_and_failed_acknowledgements_span_chunks(self):
        db, c, s = self.store._connection, self.case, self.store
        count = 65
        clone_records(db, count)
        lease = c.leased(s, self.key, self.target)
        entry = c.entry(s, self.key, contributors=tuple(range(1, count + 1)))
        db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 32)
        summary = s.complete_summary_refresh(lease.job_id, lease.fence, entry, c.tick()).summary
        self.assertEqual(summary.contributors, entry.contributors)
        db.execute("UPDATE processing_jobs SET state = 'failed'")
        ack = m.EvidenceAcknowledgement(0, 0, tuple(range(1, count + 1)))
        s.request_summary_refresh((), self.target, 3, c.tick(), acknowledge=ack)
        self.assertEqual(db.execute("SELECT count(*) FROM summary_failed_acknowledgements").fetchone()[0], count)
        # Validation must still inspect the final chunk and roll back completely.
        bad = replace(ack, failed_jobs=ack.failed_jobs + (count + 1,))
        with self.assertRaises(m.StoreRefusal) as caught:
            s.request_summary_refresh((self.key,), self.target, 3, c.tick(), acknowledge=bad)
        self.assertEqual(caught.exception.failure, "summary-request-invalid")
        lease = c.leased(s, self.key, self.target)
        bad_entry = replace(entry, contributors=entry.contributors + (count + 1,))
        with self.assertRaises(m.StoreRefusal) as caught:
            s.complete_summary_refresh(lease.job_id, lease.fence, bad_entry, c.tick())
        self.assertEqual(caught.exception.failure, "summary-contributor-invalid")

    def test_watermark_spans_chunks_and_checks_last_position(self):
        db, c, s = self.store._connection, self.case, self.store
        # Extra installations have an empty, settled batch, so no processing
        # rows or additional Source Records are required for their positions.
        tables = ("installations", "archived_messages", "ingest_batches")
        db.execute("BEGIN IMMEDIATE")
        for table in tables:
            columns = [row[1] for row in db.execute("PRAGMA table_info(" + table + ")")]
            base = dict(db.execute("SELECT * FROM " + table + " LIMIT 1").fetchone())
            rows = []
            for number in range(20):
                row = base.copy()
                row["installation_id"] = f"synthetic-extra-{number:03}"
                if table == "installations":
                    row["state"] = "retired"
                if "record_count" in row:
                    row["record_count"] = 0
                rows.append(tuple(row[name] for name in columns))
            db.executemany("INSERT INTO " + table + " (" + ",".join(columns) + ") VALUES (" +
                           ",".join("?" for _ in columns) + ")", rows)
        db.execute("COMMIT")
        self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        entry = c.entry(s, self.key)
        self.assertEqual(len(entry.watermark), 21)
        db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 32)
        lease = c.leased(s, self.key, self.target)
        result = s.complete_summary_refresh(lease.job_id, lease.fence, entry, c.tick())
        self.assertEqual(result.summary.watermark, entry.watermark)
        lease = c.leased(s, self.key, self.target)
        last = replace(entry.watermark[-1], sequence=999)
        bad = replace(entry, watermark=entry.watermark[:-1] + (last,))
        with self.assertRaises(m.StoreRefusal) as caught:
            s.complete_summary_refresh(lease.job_id, lease.fence, bad, c.tick())
        self.assertEqual(caught.exception.failure, "summary-watermark-invalid")


if __name__ == "__main__":
    unittest.main()
