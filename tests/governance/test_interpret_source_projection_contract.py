from __future__ import annotations

import json
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
NAMED_FAILURES = [
    "processing-input-invalid",
    "projection-digest-mismatch",
    "projection-identity-mismatch",
    "processor-invariant-violated",
]


class InterpretSourceProjectionContractTest(unittest.TestCase):
    def test_interpret_source_projection_contract_has_required_shape(self) -> None:
        contract = json.loads(
            (PROJECT_ROOT / "governance/scopes/interpret-source-projection.1.json").read_text(encoding="utf-8")
        )

        self.assertEqual(set(contract), REQUIRED_KEYS)
        self.assertEqual(contract["contract_schema"], 1)
        self.assertEqual(contract["scope"], "interpret-source-projection")
        self.assertEqual(contract["version"], "1")
        self.assertEqual(contract["kind"], "shared")
        self.assertTrue(contract["purpose"])
        self.assertEqual({item["mode"] for item in contract["inputs"]}, {"pure"})
        self.assertTrue(contract["outputs"])
        names = [failure["name"] for failure in contract["named_failures"]]
        self.assertEqual(names, NAMED_FAILURES)
        for failure in contract["named_failures"]:
            self.assertEqual(set(failure), {"name", "outcome"})
            self.assertEqual(failure["outcome"], "invariant-refusal")
        self.assertEqual(contract["observable_effects"], [])
        self.assertEqual(contract["dependencies"], [{"scope": "reconcile-source-record", "version": "1"}])
        self.assertEqual(set(contract["authority"]), {"owns", "does_not_own"})
        self.assertTrue(contract["authority"]["owns"])
        self.assertTrue(contract["authority"]["does_not_own"])
        oracle = contract["acceptance_oracle"]
        self.assertNotIn("freeze_state", oracle)
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_interpret_source_projection_1.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_interpret_source_projection_1.sh")
        self.assertEqual(oracle["languages"], ["python"])
        self.assertTrue((PROJECT_ROOT / oracle["specification"]).is_file(), oracle["specification"])
        effects = json.loads((PROJECT_ROOT / "packs/interpret-source-projection-1.effects.json").read_text(encoding="utf-8"))
        self.assertEqual(effects, {"effects_format": 1, "effects": []})


if __name__ == "__main__":
    unittest.main()
