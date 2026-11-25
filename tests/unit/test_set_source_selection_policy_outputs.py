"""Unbound R12 regressions for caller-owned state in accepted string subclasses."""

from dataclasses import fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
import unittest

from activity_tracker_portal.operational_store import (
    BatchItem,
    IngestBatchRecord,
    ItemPosition,
    MessageHeader,
    OriginKey,
    ProjectionEntry,
    open_store,
)
from activity_tracker_portal.set_source_selection_policy import (
    SourceSelectionRequest,
    preview_source_selection,
    set_source_selection_policy,
    source_selection_choices,
)

AT = datetime(2026, 11, 25, tzinfo=timezone.utc)
ORIGIN = OriginKey("health-connect", "synthetic-origin")


class TaggedString(str):
    def __new__(cls, value):
        instance = super().__new__(cls, value)
        instance.source_evidence = "synthetic-health-marker"
        return instance

    def __str__(self):
        raise AssertionError("Normalization must preserve the underlying string value")


class SourceSelectionOutputTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = open_store(
            Path(directory.name) / "store.sqlite3", reserve_bytes=0, free_bytes=lambda _: 10**12,
        )
        self.addCleanup(self.store.close)
        self.store.record_enrollment_offer("synthetic-code", AT + timedelta(hours=1), AT)
        self.store.enroll_installation("synthetic-code", "synthetic-installation", "synthetic-fingerprint", AT)
        self.store.activate_installation("synthetic-installation", AT)
        self.store.honor_generation(1, b"synthetic-advertisement", AT)
        envelope = b"synthetic-envelope"
        self.store.archive_ingest_batch(
            MessageHeader(
                installation_id="synthetic-installation", sequence=1,
                message_sha256=hashlib.sha256(envelope).hexdigest(), staged_generation=1,
                receipt_time=AT, envelope=envelope, receipt=b"synthetic-receipt",
            ),
            IngestBatchRecord(
                record_type="steps", projection_schema_version=1, cycle_kind="initial-snapshot",
                cycle_id="synthetic-cycle", source_page_index=1, fragment_index=1,
                previous_chain=hashlib.sha256(b"synthetic-chain").hexdigest(),
                items=(BatchItem(projection=b"synthetic-projection"),),
            ),
        )
        self.store.index_items((ProjectionEntry(
            ItemPosition("synthetic-installation", 1, 0), ORIGIN.source_system_kind, ORIGIN.origin,
            "synthetic-identity", AT,
        ),), AT)
        self.request = SourceSelectionRequest(
            TaggedString("daily-steps"), TaggedString("steps"),
            (OriginKey(TaggedString(ORIGIN.source_system_kind), TaggedString(ORIGIN.origin)),),
            date(2026, 1, 1), date(2026, 12, 31),
        )

    def assert_plain_state(self, value, path="result"):
        if is_dataclass(value) and not isinstance(value, type):
            declared = {field.name for field in fields(value)}
            state = set(getattr(value, "__dict__", {}))
            for cls in type(value).__mro__:
                slots = getattr(cls, "__slots__", ())
                state.update(name for name in ((slots,) if isinstance(slots, str) else slots)
                             if name not in ("__dict__", "__weakref__") and hasattr(value, name))
            self.assertEqual(state, declared, path)
            for name in declared:
                self.assert_plain_state(getattr(value, name), f"{path}.{name}")
        elif type(value) is tuple:
            for index, item in enumerate(value):
                self.assert_plain_state(item, f"{path}[{index}]")
        else:
            self.assertIn(type(value), (str, int, bool, date, datetime, type(None)), path)
            self.assertFalse(getattr(value, "__dict__", {}), path)

    def test_choices_strip_caller_string_state(self):
        set_source_selection_policy(self.store, self.request, (), lambda: AT)
        choices = source_selection_choices(self.store, self.request.measure, self.request.record_type)
        self.assertEqual((choices.measure, choices.record_type), ("daily-steps", "steps"))
        self.assertEqual(choices.origins[0].origin, ORIGIN.origin)
        self.assertEqual(len(choices.timeline), 1)
        self.assert_plain_state(choices)

    def test_previews_strip_caller_string_state(self):
        for repeat in (False, True):
            with self.subTest(repeat=repeat):
                if repeat:
                    set_source_selection_policy(self.store, self.request, (), lambda: AT)
                preview = preview_source_selection(self.store, self.request)
                self.assertEqual(preview.request, self.request)
                self.assertEqual(preview.repeat_of is not None, repeat)
                self.assert_plain_state(preview)

    def test_recorded_and_repeated_results_have_only_declared_state(self):
        for recorded in (True, False):
            with self.subTest(recorded=recorded):
                result = set_source_selection_policy(self.store, self.request, (), lambda: AT)
                self.assertEqual(result.recorded, recorded)
                self.assertEqual(result.policy.measure, "daily-steps")
                self.assertEqual(result.policy.record_type, "steps")
                self.assertEqual(result.policy.origins, (ORIGIN,))
                self.assertEqual(result.policy.first_date, self.request.first_date)
                self.assertEqual(result.policy.last_date, self.request.last_date)
                self.assert_plain_state(result)


if __name__ == "__main__":
    unittest.main()
