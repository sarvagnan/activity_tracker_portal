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
SCOPE = "interpret-sleep-session"
# Declared Quarantine reasons are outcomes, not named failures. A processor defect
# surfaces as interpret-source-projection@1's processor-invariant-violated, so this
# contract owns no named failure; the outputs name the reason vocabulary instead.
QUARANTINE_REASONS = (
    "sleep-session-empty",
    "sleep-stage-outside-session",
    "sleep-stage-reversed",
    "sleep-stage-empty",
)
COMMON_REASONS = {
    "projection-not-json",
    "projection-envelope-invalid",
    "projection-schema-invalid",
    "instant-out-of-range",
    "interval-reversed",
    "record-type-not-modeled",
    "schema-version-not-modeled",
    "fallback-timezone-policy-missing",
}


class InterpretSleepSessionContractTest(unittest.TestCase):
    def test_interpret_sleep_session_contract_has_required_shape(self) -> None:
        contract = json.loads((PROJECT_ROOT / "governance/scopes/interpret-sleep-session.1.json").read_text(encoding="utf-8"))

        self.assertEqual(set(contract), REQUIRED_KEYS)
        self.assertEqual(contract["contract_schema"], 1)
        self.assertEqual(contract["scope"], SCOPE)
        self.assertEqual(contract["version"], "1")
        self.assertEqual(contract["kind"], "leaf")
        self.assertTrue(contract["purpose"])
        self.assertTrue(contract["inputs"])
        self.assertEqual({item["mode"] for item in contract["inputs"]}, {"pure"})
        self.assertTrue(contract["outputs"])
        self.assertEqual(contract["named_failures"], [])
        self.assertEqual(contract["observable_effects"], [])
        self.assertEqual(contract["dependencies"], [{"scope": "interpret-source-projection", "version": "1"}])
        self.assertEqual(set(contract["authority"]), {"owns", "does_not_own"})
        self.assertTrue(contract["authority"]["owns"])
        self.assertTrue(contract["authority"]["does_not_own"])
        oracle = contract["acceptance_oracle"]
        self.assertNotIn("freeze_state", oracle)
        self.assertEqual(oracle["specification"], "packs/interpret-sleep-session-1.spec.md")
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_interpret_sleep_session_1.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_interpret_sleep_session_1.sh")
        self.assertEqual(oracle["languages"], ["python"])
        self.assertTrue((PROJECT_ROOT / oracle["specification"]).is_file(), oracle["specification"])
        effects = json.loads((PROJECT_ROOT / "packs/interpret-sleep-session-1.effects.json").read_text(encoding="utf-8"))
        self.assertEqual(effects, {"effects_format": 1, "effects": []})

    def test_quarantine_reasons_are_declared_once_and_not_common(self) -> None:
        contract = json.loads((PROJECT_ROOT / "governance/scopes/interpret-sleep-session.1.json").read_text(encoding="utf-8"))
        outputs = {item["name"]: item["description"] for item in contract["outputs"]}
        spec = (PROJECT_ROOT / "packs/interpret-sleep-session-1.spec.md").read_text(encoding="utf-8")
        self.assertEqual(len(QUARANTINE_REASONS), len(set(QUARANTINE_REASONS)))
        for reason in QUARANTINE_REASONS:
            self.assertRegex(reason, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
            self.assertNotIn(reason, COMMON_REASONS)
            self.assertIn(reason, outputs["quarantine_outcome"])
            self.assertIn(f"`{reason}`", spec)
        named = set(re.findall(r"\bsleep-[a-z0-9-]*[a-z0-9]\b", outputs["quarantine_outcome"]))
        self.assertEqual(named, set(QUARANTINE_REASONS))


if __name__ == "__main__":
    unittest.main()
