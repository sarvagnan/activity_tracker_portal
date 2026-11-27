"""Unbound R14 regressions for failures raised inside caller exception handlers."""

from datetime import date
import json
import traceback
import unittest

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
        ("primary_state", lambda: primary_state(Coverage(0, 0, 0, 0, 0, 0), True),
         "summary-state-inconsistent"),
        ("check_publication_conditions", lambda: check_publication_conditions([MARKER]),
         "summary-input-invalid"),
        ("decode_summary", lambda: decode_summary(INVALID_BODY, b"{}", None),
         "summary-bytes-invalid"),
        ("decode_summary unsupported", lambda: decode_summary(
            b'{"contract_version":"summary-contract@2"}', b"{}", None),
         "summary-version-unsupported"),
    )


class SummaryContractFailureContextTests(unittest.TestCase):
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
