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
SCOPE = "set-fallback-timezone-policy"
# This unit's own refusals and conflict, then the store's failures passed through unchanged.
NAMED_FAILURES = [
    ("timestamp-invalid", "invariant-refusal"),
    ("timezone-unknown", "refusal"),
    ("effective-time-invalid", "refusal"),
    ("replacement-invalid", "refusal"),
    ("all-history-policy-required", "refusal"),
    ("policy-unchanged", "refusal"),
    ("plan-stale", "conflict"),
    ("policy-request-invalid", "invariant-refusal"),
    ("processor-version-invalid", "invariant-refusal"),
    ("processing-request-invalid", "invariant-refusal"),
    ("store-busy", "retryable"),
    ("storage-reserve-exhausted", "retryable"),
]
STORE_PASS_THROUGH = {
    "policy-request-invalid",
    "processor-version-invalid",
    "processing-request-invalid",
    "store-busy",
    "storage-reserve-exhausted",
}
DEPENDENCIES = [{"scope": "operational-store", "version": "5"}]
EFFECTS = ["fallback-timezone-policy-write"]
OUTPUTS = ["allowed_zones", "timezone_plan", "timezone_result", "timezone_failure"]
SPEC = "packs/set-fallback-timezone-policy-1.spec.md"


def load(path: str) -> dict:
    return json.loads((PROJECT_ROOT / path).read_text(encoding="utf-8"))


def scope_map_row(scope: str) -> list[str]:
    text = (PROJECT_ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith(f"| `{scope}` |"):
            return [cell.strip() for cell in line.strip().strip("|").split("|")]
    raise AssertionError(f"no scope-map row for {scope}")


class SetFallbackTimezonePolicyContractTest(unittest.TestCase):
    def test_contract_has_required_shape(self) -> None:
        contract = load("governance/scopes/set-fallback-timezone-policy.1.json")

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
        self.assertEqual([item["name"] for item in contract["outputs"]], OUTPUTS)
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
        self.assertEqual(oracle["specification"], SPEC)
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_set_fallback_timezone_policy_1.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_set_fallback_timezone_policy_1.sh")
        self.assertEqual(oracle["languages"], ["python"])
        self.assertTrue((PROJECT_ROOT / oracle["specification"]).is_file(), oracle["specification"])

    def test_effects_declaration_matches_contract(self) -> None:
        contract = load("governance/scopes/set-fallback-timezone-policy.1.json")
        effects = load("packs/set-fallback-timezone-policy-1.effects.json")
        self.assertEqual(set(effects), {"effects_format", "effects"})
        self.assertEqual(effects["effects_format"], 1)
        self.assertEqual([effect["name"] for effect in effects["effects"]],
                         [effect["name"] for effect in contract["observable_effects"]])
        for effect in effects["effects"]:
            self.assertEqual(set(effect), {"kind", "name", "system", "outcome", "detail"})
            self.assertEqual(effect["kind"], "action")
            self.assertEqual(effect["outcome"], "confirmed")
            self.assertIn("operational-store@5", effect["system"])

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        contract = load("governance/scopes/set-fallback-timezone-policy.1.json")
        row = scope_map_row(SCOPE)
        self.assertEqual(row[1], "leaf")
        row_dependencies = set(re.findall(r"`([a-z0-9-]+)`", row[5]))
        self.assertLessEqual(row_dependencies, {item["scope"] for item in contract["dependencies"]})
        # Pending until activation with rebuilt results; explicit offsets stay authoritative.
        self.assertIn("pending", row[4])
        self.assertIn("pending", contract["purpose"])
        self.assertIn("offsets stay authoritative", contract["purpose"])
        does_not_own = " ".join(contract["authority"]["does_not_own"])
        for owned_elsewhere in ("operational-store@6", "Cutover Records", "execute-processing-job@2", "Phase 7"):
            self.assertIn(owned_elsewhere, does_not_own)

    def test_failures_are_declared_in_the_spec(self) -> None:
        spec = (PROJECT_ROOT / SPEC).read_text(encoding="utf-8")
        for name, _ in NAMED_FAILURES:
            self.assertRegex(name, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
            self.assertIn(f"`{name}`", spec)
        store = load("governance/scopes/operational-store.5.json")
        store_outcomes = {item["name"]: item["outcome"] for item in store["named_failures"]}
        for name, outcome in NAMED_FAILURES:
            if name in STORE_PASS_THROUGH:
                self.assertEqual(store_outcomes.get(name), outcome, name)


if __name__ == "__main__":
    unittest.main()
