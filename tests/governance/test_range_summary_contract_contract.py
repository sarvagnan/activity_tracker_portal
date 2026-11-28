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
SCOPE = "range-summary-contract"
CONTRACT_PATH = "governance/scopes/range-summary-contract.1.json"
SPEC_PATH = "packs/range-summary-contract-1.spec.md"
EFFECTS_PATH = "packs/range-summary-contract-1.effects.json"
NAMED_FAILURES = [
    ("range-period-invalid", "refusal"),
    ("range-input-invalid", "invariant-refusal"),
]
DEPENDENCIES = [
    {"scope": "summary-contract", "version": "1"},
]
PRESET_LENGTHS = (7, 30, 90)
PERIOD_KINDS = (
    "last-7-dates",
    "last-30-dates",
    "last-90-dates",
    "calendar-year",
    "calendar-month",
    "iso-week",
    "custom",
)


def load(path: str) -> dict:
    return json.loads((PROJECT_ROOT / path).read_text(encoding="utf-8"))


class RangeSummaryContractContractTest(unittest.TestCase):
    def test_range_summary_contract_has_required_shape(self) -> None:
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
        self.assertIn("today", input_names)
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
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_range_summary_contract_1.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_range_summary_contract_1.sh")
        self.assertEqual(oracle["languages"], ["python"])
        self.assertTrue((PROJECT_ROOT / oracle["specification"]).is_file(), oracle["specification"])

    def test_effects_declaration_is_empty(self) -> None:
        self.assertEqual(load(EFFECTS_PATH), {"effects_format": 1, "effects": []})

    def test_failures_and_period_kinds_are_documented(self) -> None:
        contract = load(CONTRACT_PATH)
        spec = (PROJECT_ROOT / SPEC_PATH).read_text(encoding="utf-8")
        for name, _ in NAMED_FAILURES:
            self.assertRegex(name, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
            self.assertIn(f"`{name}`", spec)
        period_output = next(item for item in contract["outputs"] if item["name"] == "range_period")["description"]
        for kind in PERIOD_KINDS:
            self.assertRegex(kind, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
            self.assertIn(kind, period_output)
        self.assertIn("PERIOD_KINDS = (" + ", ".join(f'"{kind}"' for kind in PERIOD_KINDS) + ")", spec)
        self.assertIn("PRESET_LENGTHS = (" + ", ".join(str(length) for length in PRESET_LENGTHS) + ")", spec)
        self.assertIn("MAX_PERIOD_DATES = 366", spec)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        contract = load(CONTRACT_PATH)
        scope_map = (PROJECT_ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        row = next(line for line in scope_map.splitlines() if line.startswith(f"| `{SCOPE}` |"))
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        self.assertEqual(cells[1], "shared")
        listed = set(re.findall(r"`([a-z0-9-]+)`", cells[-1]))
        self.assertEqual(listed, {item["scope"] for item in contract["dependencies"]})
        for length in PRESET_LENGTHS:
            self.assertIn(str(length), cells[4])


if __name__ == "__main__":
    unittest.main()
