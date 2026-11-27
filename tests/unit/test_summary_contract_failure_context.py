"""Unbound R14 regressions for failure contexts and implementation frame locals."""

from dataclasses import fields, is_dataclass
from datetime import date
import json
import traceback
import unittest

from activity_tracker_portal import summary_contract
from activity_tracker_portal.summary_contract import (
    Coverage,
    EvidenceEntry,
    SummaryContractFailure,
    build_summary,
    check_publication_conditions,
    coverage_of,
    decode_summary,
    primary_state,
)

MARKER = "synthetic-private-health-value-7314159"
INVALID_BODY = b'{"health":"' + MARKER.encode() + b'",}'


def invalid_build():
    return build_summary(
        measure="daily-steps", record_type="steps", local_date=date(2026, 1, 15),
        derivation_version="derive-daily-steps@1", processor_version="interpret-steps@1",
        source_selection_policy=None,
        evidence=(EvidenceEntry(1, 1, 1, "contributing", "interpret-steps@1", "none"),),
        reported_conditions=(), positions=(), body=INVALID_BODY,
    )


def failure_cases():
    return (
        ("build_summary", invalid_build, "summary-input-invalid"),
        ("coverage_of", lambda: coverage_of([MARKER]), "summary-input-invalid"),
        ("primary_state", lambda: primary_state(Coverage(MARKER, 0, 0, 0, 0, 0), True),
         "summary-input-invalid"),
        ("primary_state inconsistent", lambda: primary_state(Coverage(0, 0, 0, 0, 0, 0), True),
         "summary-state-inconsistent"),
        ("check_publication_conditions", lambda: check_publication_conditions([MARKER]),
         "summary-input-invalid"),
        ("decode_summary", lambda: decode_summary(INVALID_BODY, b"{}", None),
         "summary-bytes-invalid"),
        ("coverage_of keyword", lambda: coverage_of(evidence=[MARKER]),
         "summary-input-invalid"),
        ("primary_state keyword", lambda: primary_state(
            coverage=Coverage(MARKER, 0, 0, 0, 0, 0), has_value=True),
         "summary-input-invalid"),
        ("check_publication_conditions keyword",
         lambda: check_publication_conditions(names=[MARKER]), "summary-input-invalid"),
        ("decode_summary keyword", lambda: decode_summary(
            header_bytes=INVALID_BODY, evidence_bytes=b"{}", body_bytes=None),
         "summary-bytes-invalid"),
        ("decode_summary unsupported", lambda: decode_summary(
            b'{"contract_version":"summary-contract@2"}', b"{}", None),
         "summary-version-unsupported"),
    )


class SummaryContractFailureContextTests(unittest.TestCase):
    def assert_input_free(self, value):
        # Check identity as well as rendering, including input nested in containers
        # or a Coverage instance rather than exposed directly as a frame local.
        self.assertIsNot(value, MARKER)
        self.assertIsNot(value, INVALID_BODY)
        self.assertNotIn(MARKER, repr(value))
        if isinstance(value, dict):
            for key, item in value.items():
                self.assert_input_free(key)
                self.assert_input_free(item)
        elif isinstance(value, (tuple, list, set, frozenset)):
            for item in value:
                self.assert_input_free(item)
        elif is_dataclass(value) and not isinstance(value, type):
            for field in fields(value):
                self.assert_input_free(getattr(value, field.name))

    def assert_safe_failure(self, operation, expected):
        # assertRaises clears the traceback; inspect it while it is still attached.
        try:
            operation()
        except SummaryContractFailure as failure:
            self.assertEqual(failure.failure, expected)
            self.assertEqual(failure.args, (expected,))
            self.assertIsNone(failure.__cause__)
            self.assertIsNone(failure.__context__)
            self.assertNotIn(MARKER, "".join(traceback.format_exception(failure)))
            implementation_tb = None
            implementation_frames = []
            tb = failure.__traceback__
            while tb is not None:
                if tb.tb_frame.f_code.co_filename == summary_contract.__file__:
                    if implementation_tb is None:
                        implementation_tb = tb
                    implementation_frames.append(tb.tb_frame.f_code.co_name)
                    for value in tb.tb_frame.f_locals.values():
                        self.assert_input_free(value)
                tb = tb.tb_next
            # Validation, parser, and nested public-operation frames must all be
            # discarded; only the sanitized outer boundary may remain.
            self.assertEqual(implementation_frames, ["checked"])
            self.assertIsNone(implementation_tb.tb_next)
            # Caller frames own their inputs. Format the implementation suffix
            # with locals while the failure still has its original traceback.
            rendered = "".join(traceback.TracebackException(
                type(failure), failure, implementation_tb, capture_locals=True,
            ).format())
            self.assertNotIn(MARKER, rendered)
        else:
            self.fail("operation did not raise SummaryContractFailure")

    def test_failures_without_a_caller_exception(self):
        for name, operation, expected in failure_cases():
            with self.subTest(operation=name):
                self.assert_safe_failure(operation, expected)

    def test_failures_inside_a_caller_value_error_handler(self):
        for name, operation, expected in failure_cases():
            with self.subTest(operation=name):
                try:
                    raise ValueError(MARKER)
                except ValueError:
                    self.assert_safe_failure(operation, expected)

    def test_failures_inside_a_caller_json_decode_error_handler(self):
        for name, operation, expected in failure_cases():
            with self.subTest(operation=name):
                try:
                    json.loads(INVALID_BODY)
                except json.JSONDecodeError as caller_error:
                    self.assertIn(MARKER, caller_error.doc)
                    self.assert_safe_failure(operation, expected)

    def test_argument_binding_errors_stay_native(self):
        for operation in (build_summary, coverage_of, primary_state,
                          check_publication_conditions, decode_summary):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(TypeError):
                    operation()
                with self.assertRaises(TypeError):
                    operation(*([None] * 20))


if __name__ == "__main__":
    unittest.main()
