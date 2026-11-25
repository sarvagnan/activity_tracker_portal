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
    "policy-request-invalid",
    "policy-unknown",
    "policy-supersession-invalid",
    "policy-overlap",
    "policy-origin-unknown",
]
NEW_OUTPUTS = ["policy_record", "policy_reprocessing"]


def load_contract(name: str) -> dict:
    return json.loads((PROJECT_ROOT / "governance/scopes" / name).read_text(encoding="utf-8"))


class OperationalStoreFiveContractTest(unittest.TestCase):
    def test_operational_store_5_contract_has_required_shape(self) -> None:
        contract = load_contract("operational-store.5.json")

        self.assertEqual(set(contract), REQUIRED_KEYS)
        self.assertEqual(contract["contract_schema"], 1)
        self.assertEqual(contract["scope"], "operational-store")
        self.assertEqual(contract["version"], "5")
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
        self.assertEqual(oracle["specification"], "packs/operational-store-5.spec.md")
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_operational_store_5.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_operational_store_5.sh")
        self.assertEqual(oracle["languages"], ["python"])
        self.assertTrue((PROJECT_ROOT / oracle["specification"]).is_file(), oracle["specification"])

    def test_operational_store_5_extends_version_4(self) -> None:
        four = load_contract("operational-store.4.json")
        five = load_contract("operational-store.5.json")

        for key in ("observable_effects", "dependencies"):
            self.assertEqual(five[key], four[key], key)
        self.assertEqual([(item["name"], item["mode"]) for item in five["inputs"]],
                         [(item["name"], item["mode"]) for item in four["inputs"]])
        # Named failures are a superset: @4's, in order and unchanged, then the new ones.
        self.assertEqual(five["named_failures"][: len(four["named_failures"])], four["named_failures"])
        added = five["named_failures"][len(four["named_failures"]):]
        self.assertEqual([failure["name"] for failure in added], NEW_FAILURES)
        self.assertEqual({failure["outcome"] for failure in added}, {"invariant-refusal"})
        outputs = [item["name"] for item in five["outputs"]]
        self.assertEqual(outputs[: len(four["outputs"])], [item["name"] for item in four["outputs"]])
        self.assertEqual(outputs[len(four["outputs"]):], NEW_OUTPUTS)
        owns_four, owns_five = four["authority"]["owns"], five["authority"]["owns"]
        self.assertEqual(owns_five[: len(owns_four)], owns_four)
        appended = owns_five[len(owns_four):]
        self.assertTrue(any("Fallback Timezone Policy" in entry for entry in appended))
        self.assertTrue(any("Source Selection Polic" in entry for entry in appended))
        self.assertTrue(any("supersession" in entry for entry in appended))
        self.assertTrue(any("origins known" in entry for entry in appended))
        self.assertIn("policies", five["purpose"])
        # Summary Refresh Jobs and policy activation stay with operational-store@6 (Decisions 1 and 8).
        does_not_own = " ".join(five["authority"]["does_not_own"])
        self.assertIn("Summary Refresh Jobs", does_not_own)
        self.assertIn("policy activation", does_not_own)
        self.assertNotIn("policy records", does_not_own)
        effects = json.loads((PROJECT_ROOT / "packs/operational-store-5.effects.json").read_text(encoding="utf-8"))
        self.assertEqual(effects["effects_format"], 1)
        self.assertEqual([effect["name"] for effect in effects["effects"]],
                         [effect["name"] for effect in five["observable_effects"]])
        self.assertEqual({effect["kind"] for effect in effects["effects"]}, {"action"})


if __name__ == "__main__":
    unittest.main()
