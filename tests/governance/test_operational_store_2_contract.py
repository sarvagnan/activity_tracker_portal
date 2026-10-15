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
OUTCOMES = {"startup-refusal", "retryable", "invariant-refusal"}


def load_contract(name: str) -> dict:
    return json.loads((PROJECT_ROOT / "governance/scopes" / name).read_text(encoding="utf-8"))


class OperationalStoreTwoContractTest(unittest.TestCase):
    def test_operational_store_2_contract_has_required_shape(self) -> None:
        contract = load_contract("operational-store.2.json")

        self.assertEqual(set(contract), REQUIRED_KEYS)
        self.assertEqual(contract["contract_schema"], 1)
        self.assertEqual(contract["scope"], "operational-store")
        self.assertEqual(contract["version"], "2")
        self.assertEqual(contract["kind"], "shared")
        self.assertTrue(contract["purpose"])
        self.assertLessEqual({item["mode"] for item in contract["inputs"]}, {"pure", "effectful", "volatile"})
        self.assertTrue(contract["outputs"])
        names = [failure["name"] for failure in contract["named_failures"]]
        self.assertEqual(len(names), len(set(names)))
        for failure in contract["named_failures"]:
            self.assertEqual(set(failure), {"name", "outcome"})
            self.assertIn(failure["outcome"], OUTCOMES)
        for effect in contract["observable_effects"]:
            self.assertEqual(set(effect), {"name", "description"})
        self.assertEqual(contract["dependencies"], [])
        self.assertEqual(set(contract["authority"]), {"owns", "does_not_own"})
        oracle = contract["acceptance_oracle"]
        self.assertNotIn("freeze_state", oracle)
        self.assertEqual(set(oracle["executable_tests"]), {"python"})
        self.assertTrue((PROJECT_ROOT / oracle["specification"]).is_file(), oracle["specification"])

    def test_operational_store_2_keeps_version_1(self) -> None:
        one = load_contract("operational-store.1.json")
        two = load_contract("operational-store.2.json")

        for key in ("inputs", "named_failures", "observable_effects", "dependencies"):
            self.assertEqual(two[key], one[key], key)
        self.assertEqual([item["name"] for item in two["outputs"]], [item["name"] for item in one["outputs"]])
        self.assertEqual(two["authority"]["owns"][: len(one["authority"]["owns"])], one["authority"]["owns"])
        self.assertTrue(any("delivery cycle" in entry for entry in two["authority"]["owns"]))


if __name__ == "__main__":
    unittest.main()
