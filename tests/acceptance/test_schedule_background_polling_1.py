"""Acceptance tests for `schedule-background-polling@1`: the Governed Scope contract.

Requirement numbers refer to `packs/schedule-background-polling-1.spec.md`.
The TypeScript suite `schedule-background-polling-1.test.ts` checks the module.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/schedule-background-polling.1.json").read_text(encoding="utf-8"))
OUTCOMES = {"action-required", "run-failure", "storage-failure"}
INPUT_MODES = {
    "minimum_interval": "pure",
    "expected_registration": "pure",
    "execution_budget": "pure",
    "scheduling_platform": "effectful",
    "background_callback": "volatile",
    "local_notifier": "effectful",
    "sender_sync": "effectful",
    "sender_storage": "effectful",
    "clock": "volatile",
}


class R1GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual(CONTRACT["scope"], "schedule-background-polling")
        self.assertEqual(CONTRACT["version"], "1")
        self.assertEqual(CONTRACT["kind"], "leaf")
        self.assertTrue(CONTRACT["purpose"])
        self.assertEqual({item["name"]: item["mode"] for item in CONTRACT["inputs"]}, INPUT_MODES)
        self.assertEqual(
            [output["name"] for output in CONTRACT["outputs"]],
            ["registered_task", "background_trigger", "scheduling_observation", "action_required_condition"],
        )
        names = [failure["name"] for failure in CONTRACT["named_failures"]]
        self.assertEqual(len(names), 4)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual({failure["outcome"] for failure in CONTRACT["named_failures"]}, OUTCOMES)
        by_outcome = {
            outcome: sorted(failure["name"] for failure in CONTRACT["named_failures"] if failure["outcome"] == outcome)
            for outcome in OUTCOMES
        }
        self.assertEqual(by_outcome["action-required"], ["background-restricted", "registration-missing"])
        self.assertEqual(by_outcome["run-failure"], ["sync-run-failed"])
        self.assertEqual(by_outcome["storage-failure"], ["storage-failed"])
        self.assertEqual(
            [effect["name"] for effect in CONTRACT["observable_effects"]],
            ["background-task-registration", "local-notification", "sender-storage-write"],
        )
        self.assertEqual(CONTRACT["dependencies"], [{"scope": "coordinate-sender-sync", "version": "1"}])
        self.assertTrue(CONTRACT["authority"]["owns"])
        for owner in ("coordinate-sender-sync", "Sender shell enabling task", "Portal"):
            self.assertTrue(any(owner in entry for entry in CONTRACT["authority"]["does_not_own"]), owner)
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["python", "typescript"])
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `schedule-background-polling` |")]
        self.assertEqual(len(rows), 1)
        _, kind, purpose, inputs, outputs, depends_on = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(sorted(depends_on.split(", ")), sorted(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]))
        self.assertIn("same durable synchronization interface used by foreground and manual triggers", purpose)
        for phrase in ("Desired hourly minimum interval", "expected registration", "eight-minute soft work deadline"):
            self.assertIn(phrase, inputs)
        for phrase in ("One persistently registered background task", "persistent Operator-action-required restricted",
                       "not a future guarantee", "no battery-optimization exemption, foreground service"):
            self.assertIn(phrase, outputs)


if __name__ == "__main__":
    unittest.main()
