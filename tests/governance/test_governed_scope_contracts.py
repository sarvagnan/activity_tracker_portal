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
INPUT_MODES = {"pure", "effectful", "volatile"}
INGEST_PROTOCOL_OUTCOMES = {
    "delivery-block",
    "retryable",
    "staging-refusal",
    "compatibility-refusal",
}
OPERATIONAL_STORE_OUTCOMES = {
    "startup-refusal",
    "retryable",
    "invariant-refusal",
}


def load_contract(name: str) -> dict:
    path = PROJECT_ROOT / "governance/scopes" / name
    return json.loads(path.read_text(encoding="utf-8"))


class GovernedScopeContractTest(unittest.TestCase):
    def assert_common_shape(self, contract: dict, scope: str, version: str, kind: str, outcomes: set[str]) -> None:
        self.assertEqual(set(contract), REQUIRED_KEYS)
        self.assertEqual(contract["contract_schema"], 1)
        self.assertEqual(contract["scope"], scope)
        self.assertEqual(contract["version"], version)
        self.assertEqual(contract["kind"], kind)
        self.assertTrue(contract["purpose"])
        self.assertTrue(contract["inputs"])
        self.assertTrue(contract["outputs"])
        self.assertTrue(contract["named_failures"])
        names = [failure["name"] for failure in contract["named_failures"]]
        self.assertEqual(len(names), len(set(names)))
        for failure in contract["named_failures"]:
            self.assertEqual(set(failure), {"name", "outcome"})
            self.assertIn(failure["outcome"], outcomes)
        self.assertLessEqual({item["mode"] for item in contract["inputs"]}, INPUT_MODES)
        self.assertEqual(set(contract["authority"]), {"owns", "does_not_own"})
        self.assertTrue(contract["authority"]["owns"])
        self.assertTrue(contract["authority"]["does_not_own"])
        oracle = contract["acceptance_oracle"]
        self.assertNotIn("freeze_state", oracle)
        self.assertTrue((PROJECT_ROOT / oracle["specification"]).is_file(), oracle["specification"])

    def test_ingest_protocol_contract_has_required_shape(self) -> None:
        contract = load_contract("ingest-protocol.1.json")
        self.assert_common_shape(contract, "ingest-protocol", "1", "shared", INGEST_PROTOCOL_OUTCOMES)
        self.assertEqual(contract["observable_effects"], [])
        self.assertEqual(contract["dependencies"], [])
        self.assertEqual({item["mode"] for item in contract["inputs"]}, {"pure"})
        oracle = contract["acceptance_oracle"]
        for path in [oracle["runner"], *oracle["executable_tests"].values()]:
            self.assertTrue((PROJECT_ROOT / path).is_file(), path)

    def test_operational_store_contract_has_required_shape(self) -> None:
        contract = load_contract("operational-store.1.json")
        self.assert_common_shape(contract, "operational-store", "1", "shared", OPERATIONAL_STORE_OUTCOMES)
        self.assertEqual(contract["dependencies"], [])
        self.assertTrue(contract["observable_effects"])
        for effect in contract["observable_effects"]:
            self.assertEqual(set(effect), {"name", "description"})
        self.assertEqual(set(contract["acceptance_oracle"]["executable_tests"]), {"python"})


if __name__ == "__main__":
    unittest.main()
