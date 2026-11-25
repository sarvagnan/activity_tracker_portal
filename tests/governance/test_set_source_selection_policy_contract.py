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
SCOPE = "set-source-selection-policy"
CONTRACT_PATH = "governance/scopes/set-source-selection-policy.1.json"
SPEC_PATH = "packs/set-source-selection-policy-1.spec.md"
EFFECTS_PATH = "packs/set-source-selection-policy-1.effects.json"
# The unit's own refusals come first; the store's failures pass through unchanged.
OWN_REFUSALS = (
    "selection-request-invalid",
    "selection-measure-unknown",
    "selection-origin-unknown",
    "selection-changed",
)
NAMED_FAILURES = [
    *((name, "invariant-refusal") for name in OWN_REFUSALS),
    ("timestamp-invalid", "invariant-refusal"),
    ("store-busy", "retryable"),
    ("storage-reserve-exhausted", "retryable"),
]
DEPENDENCIES = [{"scope": "operational-store", "version": "5"}]
EFFECTS = ["source-selection-policy-write"]
# The draft's closed measure vocabulary: one measure per Phase 5 derive unit.
MEASURES = {
    "daily-steps": "steps",
    "heart-rate-summary": "heart-rate",
    "sleep-summary": "sleep-session",
    "weight-series": "weight",
}


def load(path: str) -> dict:
    return json.loads((PROJECT_ROOT / path).read_text(encoding="utf-8"))


class SetSourceSelectionPolicyContractTest(unittest.TestCase):
    def test_set_source_selection_policy_contract_has_required_shape(self) -> None:
        contract = load(CONTRACT_PATH)

        self.assertEqual(set(contract), REQUIRED_KEYS)
        self.assertEqual(contract["contract_schema"], 1)
        self.assertEqual(contract["scope"], SCOPE)
        self.assertEqual(contract["version"], "1")
        self.assertEqual(contract["kind"], "leaf")
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
        self.assertEqual(oracle["specification"], SPEC_PATH)
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_set_source_selection_policy_1.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_set_source_selection_policy_1.sh")
        self.assertEqual(oracle["languages"], ["python"])
        self.assertTrue((PROJECT_ROOT / oracle["specification"]).is_file(), oracle["specification"])

    def test_effects_declaration_matches_contract(self) -> None:
        contract = load(CONTRACT_PATH)
        effects = load(EFFECTS_PATH)
        self.assertEqual(set(effects), {"effects_format", "effects"})
        self.assertEqual(effects["effects_format"], 1)
        self.assertEqual([effect["name"] for effect in effects["effects"]],
                         [effect["name"] for effect in contract["observable_effects"]])
        for effect in effects["effects"]:
            self.assertEqual(set(effect), {"kind", "name", "system", "outcome", "detail"})
            self.assertEqual(effect["kind"], "action")
            self.assertEqual(effect["outcome"], "confirmed")
            self.assertIn("operational-store@5", effect["system"])

    def test_refusals_are_declared_once_and_documented(self) -> None:
        contract = load(CONTRACT_PATH)
        spec = (PROJECT_ROOT / SPEC_PATH).read_text(encoding="utf-8")
        refusal = next(item for item in contract["outputs"] if item["name"] == "selection_refusal")["description"]
        listed = set(re.findall(r"\bselection-[a-z0-9]+(?:-[a-z0-9]+)*\b", refusal))
        self.assertEqual(listed, set(OWN_REFUSALS))
        for name, _ in NAMED_FAILURES:
            self.assertRegex(name, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
            self.assertIn(f"`{name}`", spec)

    def test_measure_vocabulary_is_stated_consistently(self) -> None:
        contract = load(CONTRACT_PATH)
        spec = (PROJECT_ROOT / SPEC_PATH).read_text(encoding="utf-8")
        vocabulary = next(item for item in contract["inputs"] if item["name"] == "measure_vocabulary")["description"]
        schemas = PROJECT_ROOT / "projection/v1/schemas"
        for measure, record_type in MEASURES.items():
            self.assertRegex(measure, r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
            self.assertLessEqual(len(measure), 64)
            self.assertIn(f"{measure} with {record_type}", vocabulary)
            self.assertIn(f'`"{measure}"` → `("{record_type}",)`', spec)
            self.assertTrue((schemas / f"{record_type}.schema.json").is_file(), record_type)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        contract = load(CONTRACT_PATH)
        scope_map = (PROJECT_ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        row = next(line for line in scope_map.splitlines() if line.startswith(f"| `{SCOPE}` |"))
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        self.assertEqual(cells[1], "leaf")
        listed = set(re.findall(r"`([a-z0-9-]+)`", cells[-1]))
        self.assertTrue(listed)
        self.assertLessEqual(listed, {item["scope"] for item in contract["dependencies"]})


if __name__ == "__main__":
    unittest.main()
