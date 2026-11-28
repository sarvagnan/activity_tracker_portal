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
SCOPE = "duplicate-observation-contract"
CONTRACT_PATH = "governance/scopes/duplicate-observation-contract.1.json"
SPEC_PATH = "packs/duplicate-observation-contract-1.spec.md"
EFFECTS_PATH = "packs/duplicate-observation-contract-1.effects.json"
NAMED_FAILURES = [("duplicate-observation-input-invalid", "invariant-refusal")]
DEPENDENCIES = [{"scope": "summary-contract", "version": "1"}]
OUTPUTS = ["contributions", "unresolved_ties", "record_facts", "duplicate_observation_failure"]
# Names summary-contract@1 reserved for this unit's consumers; this unit uses them, never redefines them.
SUMMARY_CONTRACT_CONDITIONS = ("duplicate-grouped", "equal-time-conflict")


def load(path: str) -> dict:
    return json.loads((PROJECT_ROOT / path).read_text(encoding="utf-8"))


class DuplicateObservationContractContractTest(unittest.TestCase):
    def test_duplicate_observation_contract_has_required_shape(self) -> None:
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
        self.assertEqual([item["name"] for item in contract["outputs"]], OUTPUTS)
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
        self.assertEqual(
            oracle["executable_tests"], {"python": "tests/acceptance/test_duplicate_observation_contract_1.py"}
        )
        self.assertEqual(oracle["runner"], "tests/acceptance/run_duplicate_observation_contract_1.sh")
        self.assertEqual(oracle["languages"], ["python"])
        self.assertTrue((PROJECT_ROOT / oracle["specification"]).is_file(), oracle["specification"])

    def test_effects_declaration_is_empty(self) -> None:
        self.assertEqual(load(EFFECTS_PATH), {"effects_format": 1, "effects": []})

    def test_failures_and_interface_are_documented(self) -> None:
        spec = (PROJECT_ROOT / SPEC_PATH).read_text(encoding="utf-8")
        self.assertIn("**Status:** Draft for the Operator's Gate A review.", spec)
        for name, _ in NAMED_FAILURES:
            self.assertRegex(name, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
            self.assertIn(f"`{name}`", spec)
        self.assertIn('`CONTRACT_VERSION = "duplicate-observation-contract@1"`', spec)
        self.assertIn('`GROUP_RULE = "exact-instant-and-value"`', spec)
        self.assertIn('`GROUP_CONFIDENCE = "exact"`', spec)
        self.assertIn('`FAILURES = ("duplicate-observation-input-invalid",)`', spec)
        for heading in ("## Decisions for the Operator", "## Freeze prerequisites", "## Residuals"):
            self.assertIn(heading, spec)

    def test_requirements_are_numbered_and_each_proposes_tests(self) -> None:
        spec = (PROJECT_ROOT / SPEC_PATH).read_text(encoding="utf-8")
        numbers = [int(n) for n in re.findall(r"^### R(\d+) — ", spec, flags=re.MULTILINE)]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
        sections = re.split(r"^### R\d+ — ", spec, flags=re.MULTILINE)[1:]
        names: list[str] = []
        for section in sections:
            section = section.split("\n## ", 1)[0]
            line = next((ln for ln in section.splitlines() if ln.startswith("Proposed tests: ")), None)
            self.assertIsNotNone(line, section.splitlines()[0])
            names.extend(re.findall(r"`(test_[a-z0-9_]+)`", line))
        self.assertEqual(len(names), len(set(names)))
        self.assertIn(f"{len(names)} tests, one per proposed name", spec)

    def test_condition_names_are_summary_contract_names(self) -> None:
        from activity_tracker_portal import summary_contract

        spec = (PROJECT_ROOT / SPEC_PATH).read_text(encoding="utf-8")
        for name in SUMMARY_CONTRACT_CONDITIONS:
            self.assertIn(name, summary_contract.REPORTED_CONDITIONS)
            self.assertIn(f"`{name}`", spec)
        self.assertIn("contributing", summary_contract.STANDINGS)
        self.assertIn("unresolved", summary_contract.STANDINGS)

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
