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
    "processor-generation-invalid",
    "processor-generation-unknown",
    "summary-request-invalid",
    "summary-target-invalid",
    "summary-refresh-job-unknown",
    "summary-entry-invalid",
    "summary-watermark-invalid",
    "summary-contributor-invalid",
]
NEW_OUTPUTS = [
    "processor_generation",
    "summary_refresh_request",
    "summary_refresh_job",
    "summary_refresh_lease",
    "summary_revision",
]


def load_contract(name: str) -> dict:
    return json.loads((PROJECT_ROOT / "governance/scopes" / name).read_text(encoding="utf-8"))


class OperationalStoreSixContractTest(unittest.TestCase):
    def test_operational_store_6_contract_has_required_shape(self) -> None:
        contract = load_contract("operational-store.6.json")

        self.assertEqual(set(contract), REQUIRED_KEYS)
        self.assertEqual(contract["contract_schema"], 1)
        self.assertEqual(contract["scope"], "operational-store")
        self.assertEqual(contract["version"], "6")
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
        self.assertEqual(oracle["specification"], "packs/operational-store-6.spec.md")
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_operational_store_6.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_operational_store_6.sh")
        self.assertEqual(oracle["languages"], ["python"])
        self.assertTrue((PROJECT_ROOT / oracle["specification"]).is_file(), oracle["specification"])

    def test_operational_store_6_extends_version_5(self) -> None:
        five = load_contract("operational-store.5.json")
        six = load_contract("operational-store.6.json")

        for key in ("observable_effects", "dependencies"):
            self.assertEqual(six[key], five[key], key)
        self.assertEqual([(item["name"], item["mode"]) for item in six["inputs"]],
                         [(item["name"], item["mode"]) for item in five["inputs"]])
        # Named failures are a superset: @5's, in order and unchanged, then the new ones.
        self.assertEqual(six["named_failures"][: len(five["named_failures"])], five["named_failures"])
        added = six["named_failures"][len(five["named_failures"]):]
        self.assertEqual([failure["name"] for failure in added], NEW_FAILURES)
        self.assertEqual({failure["outcome"] for failure in added}, {"invariant-refusal"})
        outputs = [item["name"] for item in six["outputs"]]
        self.assertEqual(outputs[: len(five["outputs"])], [item["name"] for item in five["outputs"]])
        self.assertEqual(outputs[len(five["outputs"]):], NEW_OUTPUTS)
        owns_five, owns_six = five["authority"]["owns"], six["authority"]["owns"]
        self.assertEqual(owns_six[: len(owns_five)], owns_five)
        appended = owns_six[len(owns_five):]
        self.assertTrue(any("Processor Generation" in entry for entry in appended))
        self.assertTrue(any("Summary Refresh Jobs" in entry for entry in appended))
        self.assertTrue(any("evidence token" in entry for entry in appended))
        self.assertTrue(any("candidate Health Summary" in entry for entry in appended))
        self.assertTrue(any("Data Watermark" in entry for entry in appended))
        self.assertTrue(any("bookmark" in entry for entry in appended))
        self.assertIn("summary work", six["purpose"])
        # Split A (Operator, 2026-11-27): cutover and publication move to @7, review and
        # reconciliation to @8, Amendments and presentation reads to @9, data rights to @10.
        does_not_own = " ".join(six["authority"]["does_not_own"])
        for later, entries in {
            "operational-store@7": ("Cutover Targets", "Candidate Builds", "Cutover Records", "activation", "publication"),
            "operational-store@8": ("activation-review tokens", "Compound Cutover", "Reconciliation"),
            "operational-store@9": ("Amendments", "presentation reads"),
            "operational-store@10": ("Deletion Ledger", "Export Jobs", "backup"),
        }.items():
            self.assertIn(later, does_not_own)
            for entry in entries:
                self.assertIn(entry, does_not_own)
        self.assertNotIn("Summary Refresh Jobs", does_not_own)
        self.assertNotIn("Processor Generations,", does_not_own)
        effects = json.loads((PROJECT_ROOT / "packs/operational-store-6.effects.json").read_text(encoding="utf-8"))
        self.assertEqual(effects["effects_format"], 1)
        self.assertEqual([effect["name"] for effect in effects["effects"]],
                         [effect["name"] for effect in six["observable_effects"]])
        self.assertEqual({effect["kind"] for effect in effects["effects"]}, {"action"})
        self.assertIn("migrations 2, 3, 4, and 5", effects["effects"][0]["detail"])

    def test_operational_store_6_pack_lists_failures_and_decisions(self) -> None:
        six = load_contract("operational-store.6.json")
        spec = (PROJECT_ROOT / "packs/operational-store-6.spec.md").read_text(encoding="utf-8")

        self.assertIn("**Status:** Draft for the Operator's Gate A review.", spec)
        self.assertIn("## Decisions for the Operator", spec)
        for failure in NEW_FAILURES:
            self.assertIn(f"`{failure}`", spec)
        # Requirements continue @5's numbering (R1-R53).
        self.assertIn("### R54 ", spec)
        self.assertNotIn("### R53 ", spec)


if __name__ == "__main__":
    unittest.main()
