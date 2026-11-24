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
    "selection-unknown",
    "selection-not-latest",
    "selection-not-projection",
    "processor-version-invalid",
    "processing-request-invalid",
    "processing-job-unknown",
    "lease-not-held",
    "outcome-invalid",
    "outcome-conflict",
    "item-already-indexed",
    "item-quarantine-conflict",
]
NEW_OUTPUTS = ["processing_job", "processing_lease", "processing_outcome", "item_quarantine"]


def load_contract(name: str) -> dict:
    return json.loads((PROJECT_ROOT / "governance/scopes" / name).read_text(encoding="utf-8"))


class OperationalStoreFourContractTest(unittest.TestCase):
    def test_operational_store_4_contract_has_required_shape(self) -> None:
        contract = load_contract("operational-store.4.json")

        self.assertEqual(set(contract), REQUIRED_KEYS)
        self.assertEqual(contract["contract_schema"], 1)
        self.assertEqual(contract["scope"], "operational-store")
        self.assertEqual(contract["version"], "4")
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
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_operational_store_4.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_operational_store_4.sh")
        self.assertTrue((PROJECT_ROOT / oracle["specification"]).is_file(), oracle["specification"])

    def test_operational_store_4_extends_version_3(self) -> None:
        three = load_contract("operational-store.3.json")
        four = load_contract("operational-store.4.json")

        for key in ("observable_effects", "dependencies"):
            self.assertEqual(four[key], three[key], key)
        self.assertEqual([(item["name"], item["mode"]) for item in four["inputs"]],
                         [(item["name"], item["mode"]) for item in three["inputs"]])
        self.assertEqual(four["named_failures"][: len(three["named_failures"])], three["named_failures"])
        added = four["named_failures"][len(three["named_failures"]):]
        self.assertEqual([failure["name"] for failure in added], NEW_FAILURES)
        self.assertEqual({failure["outcome"] for failure in added}, {"invariant-refusal"})
        outputs = [item["name"] for item in four["outputs"]]
        self.assertEqual(outputs[: len(three["outputs"])], [item["name"] for item in three["outputs"]])
        self.assertEqual(outputs[len(three["outputs"]):], NEW_OUTPUTS)
        owns_three, owns_four = three["authority"]["owns"], four["authority"]["owns"]
        self.assertEqual(owns_four[: len(owns_three)], owns_three)
        self.assertTrue(any("Processing Jobs" in entry for entry in owns_four[len(owns_three):]))
        self.assertTrue(any("Canonical Revisions" in entry for entry in owns_four[len(owns_three):]))
        self.assertIn("Processing Job", four["purpose"])
        effects = json.loads((PROJECT_ROOT / "packs/operational-store-4.effects.json").read_text(encoding="utf-8"))
        self.assertEqual([effect["name"] for effect in effects["effects"]],
                         [effect["name"] for effect in four["observable_effects"]])


if __name__ == "__main__":
    unittest.main()
