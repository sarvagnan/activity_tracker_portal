"""Acceptance tests for `notify-operator-conditions@1`: the Governed Scope contract.

Requirement numbers refer to `packs/notify-operator-conditions-1.spec.md`.
The TypeScript suite `notify-operator-conditions-1.test.ts` checks the module.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/notify-operator-conditions.1.json").read_text(encoding="utf-8"))
INPUT_MODES = {
    "run_summary": "volatile",
    "enrollment_status": "volatile",
    "key_status": "volatile",
    "local_notifier": "effectful",
    "sender_storage": "effectful",
    "clock": "volatile",
}
CONDITIONS = (
    "permission-missing", "screen-lock-removed", "storage-pressure", "record-too-large", "projection-refused",
    "installation-key-unusable", "enrollment-refused", "drain-complete",
)


class R1GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual(CONTRACT["scope"], "notify-operator-conditions")
        self.assertEqual(CONTRACT["version"], "1")
        self.assertEqual(CONTRACT["kind"], "leaf")
        self.assertIn("ADR 0013", CONTRACT["purpose"])
        self.assertEqual({item["name"]: item["mode"] for item in CONTRACT["inputs"]}, INPUT_MODES)
        self.assertEqual([output["name"] for output in CONTRACT["outputs"]], ["operator_conditions", "onset_notification"])
        self.assertEqual(CONTRACT["named_failures"], [{"name": "storage-failed", "outcome": "storage-failure"}])
        self.assertEqual([effect["name"] for effect in CONTRACT["observable_effects"]], ["local-notification", "sender-storage-write"])
        self.assertEqual(CONTRACT["dependencies"], [
            {"scope": "coordinate-sender-sync", "version": "1"},
            {"scope": "stage-sync-checkpoint", "version": "1"},
            {"scope": "manage-sender-enrollment", "version": "1"},
        ])
        self.assertTrue(CONTRACT["authority"]["owns"])
        for owner in ("coordinate-sender-sync", "stage-sync-checkpoint", "manage-sender-enrollment", "send-outbox-message",
                      "schedule-background-polling", "Sender shell enabling task"):
            self.assertTrue(any(owner in entry for entry in CONTRACT["authority"]["does_not_own"]), owner)
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["python", "typescript"])
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `notify-operator-conditions` |")]
        self.assertEqual(len(rows), 1)
        _, kind, purpose, inputs, outputs, depends_on = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(sorted(depends_on.split(", ")), sorted(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]))
        self.assertIn("notify each onset once", purpose)
        for phrase in ("Polling Run", "enrollment phase", "installation key", "local notifier"):
            self.assertIn(phrase, inputs)
        for condition in CONDITIONS:
            self.assertIn(condition, outputs)
        for phrase in ("one health-data-free local notification at each onset", "never for a Delivery Block or scheduling condition"):
            self.assertIn(phrase, outputs)


if __name__ == "__main__":
    unittest.main()
