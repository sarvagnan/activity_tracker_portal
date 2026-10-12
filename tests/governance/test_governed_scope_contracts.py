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
FAILURE_OUTCOMES = {
    "delivery-block",
    "retryable",
    "staging-refusal",
    "compatibility-refusal",
}


class GovernedScopeContractTest(unittest.TestCase):
    def test_ingest_protocol_contract_has_required_shape(self) -> None:
        path = PROJECT_ROOT / "governance/scopes/ingest-protocol.1.json"
        contract = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(set(contract), REQUIRED_KEYS)
        self.assertEqual(contract["contract_schema"], 1)
        self.assertEqual(contract["scope"], "ingest-protocol")
        self.assertEqual(contract["version"], "1")
        self.assertEqual(contract["kind"], "shared")
        self.assertTrue(contract["purpose"])
        self.assertTrue(contract["inputs"])
        self.assertTrue(contract["outputs"])
        self.assertTrue(contract["named_failures"])
        names = [failure["name"] for failure in contract["named_failures"]]
        self.assertEqual(len(names), len(set(names)))
        for failure in contract["named_failures"]:
            self.assertEqual(set(failure), {"name", "outcome"})
            self.assertIn(failure["outcome"], FAILURE_OUTCOMES)
        self.assertEqual(contract["observable_effects"], [])
        self.assertEqual(contract["dependencies"], [])
        self.assertEqual({item["mode"] for item in contract["inputs"]}, {"pure"})
        self.assertEqual(
            set(contract["authority"]), {"owns", "does_not_own"}
        )
        self.assertTrue(contract["authority"]["owns"])
        self.assertTrue(contract["authority"]["does_not_own"])
        oracle = contract["acceptance_oracle"]
        self.assertNotIn("freeze_state", oracle)
        for path in [oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()]:
            self.assertTrue((PROJECT_ROOT / path).is_file(), path)


if __name__ == "__main__":
    unittest.main()
