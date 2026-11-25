from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_KEYS = {
    "contract_schema",
    "scope",
    "version",
    "kind",
    "purpose",
    "inputs",
    "outputs",
    "named_failures",
    "observable_effects",
    "authority",
    "dependencies",
    "acceptance_oracle",
}
SCOPE = "execute-processing-job"
# Raised only when nothing could be written; each is the store's own failure, passed through.
NAMED_FAILURES = [
    ("processing-job-unknown", "invariant-refusal"),
    ("timestamp-invalid", "invariant-refusal"),
    ("store-busy", "retryable"),
    ("storage-reserve-exhausted", "retryable"),
]
DEPENDENCIES = [
    {"scope": "interpret-source-projection", "version": "1"},
    {"scope": "operational-store", "version": "4"},
    {"scope": "interpret-steps", "version": "1"},
    {"scope": "interpret-heart-rate", "version": "1"},
    {"scope": "interpret-sleep-session", "version": "1"},
    {"scope": "interpret-weight", "version": "1"},
]
EFFECTS = ["processing-outcome-write", "processing-failure-write"]
# Recorded job-failure reasons are outputs, not named failures: the job, not the call, fails.
DETERMINISTIC_REASONS = (
    "processor-unavailable",
    "processor-record-type-mismatch",
    "processing-evidence-missing",
    "processing-input-invalid",
    "projection-digest-mismatch",
    "projection-identity-mismatch",
    "processor-invariant-violated",
    "outcome-conflict",
    "outcome-invalid",
)
TRANSIENT_REASON = "interpretation-unexpected-failure"


def load(path: str) -> dict:
    return json.loads((PROJECT_ROOT / path).read_text(encoding="utf-8"))


class ExecuteProcessingJobContractTest(unittest.TestCase):
    def test_execute_processing_job_contract_has_required_shape(self) -> None:
        contract = load("governance/scopes/execute-processing-job.1.json")

        self.assertEqual(set(contract), REQUIRED_KEYS)
        self.assertEqual(contract["contract_schema"], 1)
        self.assertEqual(contract["scope"], SCOPE)
        self.assertEqual(contract["version"], "1")
        self.assertEqual(contract["kind"], "integration")
        self.assertTrue(contract["purpose"])
        self.assertTrue(contract["inputs"])
        self.assertLessEqual({item["mode"] for item in contract["inputs"]}, {"pure", "effectful", "volatile"})
        input_names = [item["name"] for item in contract["inputs"]]
        self.assertEqual(len(input_names), len(set(input_names)))
        self.assertTrue(contract["outputs"])
        output_names = [item["name"] for item in contract["outputs"]]
        self.assertEqual(len(output_names), len(set(output_names)))
        for failure in contract["named_failures"]:
            self.assertEqual(set(failure), {"name", "outcome"})
        self.assertEqual([(item["name"], item["outcome"]) for item in contract["named_failures"]], NAMED_FAILURES)
        for effect in contract["observable_effects"]:
            self.assertEqual(set(effect), {"name", "description"})
        self.assertEqual([effect["name"] for effect in contract["observable_effects"]], EFFECTS)
        self.assertEqual(contract["dependencies"], DEPENDENCIES)
        self.assertEqual(set(contract["authority"]), {"owns", "does_not_own"})
        self.assertTrue(contract["authority"]["owns"])
        self.assertTrue(contract["authority"]["does_not_own"])
        oracle = contract["acceptance_oracle"]
        self.assertNotIn("freeze_state", oracle)
        self.assertEqual(oracle["specification"], "packs/execute-processing-job-1.spec.md")
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_execute_processing_job_1.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_execute_processing_job_1.sh")
        self.assertEqual(oracle["languages"], ["python"])
        self.assertTrue((PROJECT_ROOT / oracle["specification"]).is_file(), oracle["specification"])

    def test_effects_declaration_matches_contract(self) -> None:
        contract = load("governance/scopes/execute-processing-job.1.json")
        effects = load("packs/execute-processing-job-1.effects.json")
        self.assertEqual(set(effects), {"effects_format", "effects"})
        self.assertEqual(effects["effects_format"], 1)
        self.assertEqual([effect["name"] for effect in effects["effects"]],
                         [effect["name"] for effect in contract["observable_effects"]])
        for effect in effects["effects"]:
            self.assertEqual(set(effect), {"kind", "name", "system", "outcome", "detail"})
            self.assertEqual(effect["kind"], "action")
            self.assertEqual(effect["outcome"], "confirmed")
            self.assertIn("operational-store@4", effect["system"])

    def test_failure_reasons_are_declared_once(self) -> None:
        contract = load("governance/scopes/execute-processing-job.1.json")
        outputs = {item["name"]: item["description"] for item in contract["outputs"]}
        spec = (PROJECT_ROOT / "packs/execute-processing-job-1.spec.md").read_text(encoding="utf-8")
        reasons = (*DETERMINISTIC_REASONS, TRANSIENT_REASON)
        self.assertEqual(len(reasons), len(set(reasons)))
        named = {name for name, _ in NAMED_FAILURES}
        for reason in reasons:
            self.assertRegex(reason, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
            self.assertLessEqual(len(reason), 64)
            self.assertNotIn(reason, named)
            self.assertIn(f"`{reason}`", spec)
        for reason in DETERMINISTIC_REASONS:
            self.assertIn(reason, outputs["failed_job"])
        self.assertIn(TRANSIENT_REASON, outputs["scheduled_retry"])
        listed = set(re.findall(r"\b[a-z0-9]+(?:-[a-z0-9]+)+\b", outputs["failed_job"].split("one of", 1)[1]))
        self.assertEqual(listed, set(DETERMINISTIC_REASONS))


if __name__ == "__main__":
    unittest.main()
