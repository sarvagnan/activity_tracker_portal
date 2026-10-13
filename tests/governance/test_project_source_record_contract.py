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


class ProjectSourceRecordContractTest(unittest.TestCase):
    def test_project_source_record_contract_has_required_shape(self) -> None:
        path = PROJECT_ROOT / "governance/scopes/project-source-record.1.json"
        contract = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(set(contract), REQUIRED_KEYS)
        self.assertEqual(contract["contract_schema"], 1)
        self.assertEqual(contract["scope"], "project-source-record")
        self.assertEqual(contract["version"], "1")
        self.assertEqual(contract["kind"], "shared")
        self.assertTrue(contract["purpose"])
        self.assertEqual({item["mode"] for item in contract["inputs"]}, {"pure"})
        self.assertTrue(contract["outputs"])
        names = [failure["name"] for failure in contract["named_failures"]]
        self.assertTrue(names)
        self.assertEqual(len(names), len(set(names)))
        for failure in contract["named_failures"]:
            self.assertEqual(set(failure), {"name", "outcome"})
            self.assertEqual(failure["outcome"], "projection-refusal")
        self.assertEqual(contract["observable_effects"], [])
        self.assertEqual(contract["dependencies"], [])
        self.assertEqual(set(contract["authority"]), {"owns", "does_not_own"})
        self.assertTrue(contract["authority"]["owns"])
        self.assertTrue(contract["authority"]["does_not_own"])
        oracle = contract["acceptance_oracle"]
        self.assertEqual(set(oracle["executable_tests"]), {"kotlin", "python"})
        self.assertTrue((PROJECT_ROOT / oracle["specification"]).is_file(), oracle["specification"])


if __name__ == "__main__":
    unittest.main()
