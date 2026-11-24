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
NEW_FAILURES = [
    "archived-item-unknown",
    "index-entry-mismatch",
    "index-entry-conflict",
    "source-key-invalid",
    "source-record-unknown",
    "selection-evidence-invalid",
    "selection-stale",
]


def load_contract(name: str) -> dict:
    return json.loads((PROJECT_ROOT / "governance/scopes" / name).read_text(encoding="utf-8"))


class OperationalStoreThreeContractTest(unittest.TestCase):
    def test_operational_store_3_contract_has_required_shape(self) -> None:
        contract = load_contract("operational-store.3.json")

        self.assertEqual(set(contract), REQUIRED_KEYS)
        self.assertEqual(contract["contract_schema"], 1)
        self.assertEqual(contract["scope"], "operational-store")
        self.assertEqual(contract["version"], "3")
        self.assertEqual(contract["kind"], "shared")
        self.assertTrue(contract["purpose"])
        self.assertLessEqual({item["mode"] for item in contract["inputs"]}, {"pure", "effectful", "volatile"})
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

    def test_operational_store_3_extends_version_2(self) -> None:
        two = load_contract("operational-store.2.json")
        three = load_contract("operational-store.3.json")

        for key in ("observable_effects", "dependencies"):
            self.assertEqual(three[key], two[key], key)
        self.assertEqual([(item["name"], item["mode"]) for item in three["inputs"]],
                         [(item["name"], item["mode"]) for item in two["inputs"]])
        self.assertEqual(three["named_failures"][: len(two["named_failures"])], two["named_failures"])
        added = three["named_failures"][len(two["named_failures"]):]
        self.assertEqual([failure["name"] for failure in added], NEW_FAILURES)
        self.assertEqual({failure["outcome"] for failure in added}, {"invariant-refusal"})
        outputs = [item["name"] for item in three["outputs"]]
        self.assertEqual(outputs[: len(two["outputs"])], [item["name"] for item in two["outputs"]])
        self.assertEqual(outputs[len(two["outputs"]):], ["index_result", "selection_record"])
        owns_two, owns_three = two["authority"]["owns"], three["authority"]["owns"]
        self.assertEqual(owns_three[1: len(owns_two)], owns_two[1:])
        self.assertIn("later additive migrations", owns_three[0])
        self.assertTrue(any("Source Record identity" in entry for entry in owns_three[len(owns_two):]))
        effects = json.loads((PROJECT_ROOT / "packs/operational-store-3.effects.json").read_text(encoding="utf-8"))
        self.assertEqual([effect["name"] for effect in effects["effects"]],
                         [effect["name"] for effect in three["observable_effects"]])


if __name__ == "__main__":
    unittest.main()
