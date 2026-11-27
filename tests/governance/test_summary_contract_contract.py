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
SCOPE = "summary-contract"
CONTRACT_PATH = "governance/scopes/summary-contract.1.json"
SPEC_PATH = "packs/summary-contract-1.spec.md"
EFFECTS_PATH = "packs/summary-contract-1.effects.json"
NAMED_FAILURES = [
    ("summary-input-invalid", "invariant-refusal"),
    ("summary-state-inconsistent", "invariant-refusal"),
    ("summary-bytes-invalid", "invariant-refusal"),
    ("summary-version-unsupported", "refusal"),
]
DEPENDENCIES = [
    {"scope": "interpret-source-projection", "version": "1"},
    {"scope": "set-source-selection-policy", "version": "1"},
    {"scope": "set-fallback-timezone-policy", "version": "1"},
]
PRIMARY_STATES = ("measured", "partial", "missing", "failed", "quarantined", "policy-pending", "unresolved")
STANDINGS = ("contributing", "unresolved", "in-conflict", "policy-pending", "failed", "quarantined")


def load(path: str) -> dict:
    return json.loads((PROJECT_ROOT / path).read_text(encoding="utf-8"))


class SummaryContractContractTest(unittest.TestCase):
    def test_summary_contract_has_required_shape(self) -> None:
        contract = load(CONTRACT_PATH)

        self.assertEqual(set(contract), REQUIRED_KEYS)
        self.assertEqual(contract["contract_schema"], 1)
        self.assertEqual(contract["scope"], SCOPE)
        self.assertEqual(contract["version"], "1")
        self.assertEqual(contract["kind"], "shared")
        self.assertTrue(contract["purpose"])
        self.assertTrue(contract["inputs"])
        self.assertEqual({item["mode"] for item in contract["inputs"]}, {"pure"})
        input_names = [item["name"] for item in contract["inputs"]]
        self.assertEqual(len(input_names), len(set(input_names)))
        self.assertTrue(contract["outputs"])
        output_names = [item["name"] for item in contract["outputs"]]
        self.assertEqual(len(output_names), len(set(output_names)))
        for failure in contract["named_failures"]:
            self.assertEqual(set(failure), {"name", "outcome"})
        self.assertEqual([(item["name"], item["outcome"]) for item in contract["named_failures"]], NAMED_FAILURES)
        self.assertEqual(contract["observable_effects"], [])
        self.assertEqual(contract["dependencies"], DEPENDENCIES)
        self.assertEqual(set(contract["authority"]), {"owns", "does_not_own"})
        self.assertTrue(contract["authority"]["owns"])
        self.assertTrue(contract["authority"]["does_not_own"])
        oracle = contract["acceptance_oracle"]
        self.assertNotIn("freeze_state", oracle)
        self.assertEqual(oracle["specification"], SPEC_PATH)
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_summary_contract_1.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_summary_contract_1.sh")
        self.assertEqual(oracle["languages"], ["python"])
        self.assertTrue((PROJECT_ROOT / oracle["specification"]).is_file(), oracle["specification"])

    def test_effects_declaration_is_empty(self) -> None:
        self.assertEqual(load(EFFECTS_PATH), {"effects_format": 1, "effects": []})

    def test_failures_and_vocabularies_are_documented(self) -> None:
        contract = load(CONTRACT_PATH)
        spec = (PROJECT_ROOT / SPEC_PATH).read_text(encoding="utf-8")
        for name, _ in NAMED_FAILURES:
            self.assertRegex(name, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
            self.assertIn(f"`{name}`", spec)
        state_output = next(item for item in contract["outputs"] if item["name"] == "primary_state")["description"]
        for state in PRIMARY_STATES:
            self.assertIn(state, state_output)
        self.assertIn("PRIMARY_STATES = (" + ", ".join(f'"{state}"' for state in PRIMARY_STATES) + ")", spec)
        self.assertIn("STANDINGS = (" + ", ".join(f'"{standing}"' for standing in STANDINGS) + ")", spec)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        contract = load(CONTRACT_PATH)
        scope_map = (PROJECT_ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        row = next(line for line in scope_map.splitlines() if line.startswith(f"| `{SCOPE}` |"))
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        self.assertEqual(cells[1], "shared")
        listed = set(re.findall(r"`([a-z0-9-]+)`", cells[-1]))
        self.assertEqual(listed, {item["scope"] for item in contract["dependencies"]})


if __name__ == "__main__":
    unittest.main()
