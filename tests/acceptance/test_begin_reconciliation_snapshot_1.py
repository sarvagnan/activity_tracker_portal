"""Acceptance tests for `begin-reconciliation-snapshot@1`: the Governed Scope contract.

Requirement numbers refer to `packs/begin-reconciliation-snapshot-1.spec.md`.
The TypeScript suite `begin-reconciliation-snapshot-1.test.ts` checks the begin.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/begin-reconciliation-snapshot.1.json").read_text(encoding="utf-8"))
OUTCOMES = {"refusal", "not-permitted", "read-failure", "storage-failure"}
INPUT_MODES = {
    "record_type": "pure",
    "sender_installation": "pure",
    "superseding": "pure",
    "snapshot_identity_source": "volatile",
    "current_time": "volatile",
    "health_connect_token": "volatile",
    "sender_storage": "effectful",
}


class R1GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual(CONTRACT["scope"], "begin-reconciliation-snapshot")
        self.assertEqual(CONTRACT["version"], "1")
        self.assertEqual(CONTRACT["kind"], "leaf")
        self.assertEqual({item["name"]: item["mode"] for item in CONTRACT["inputs"]}, INPUT_MODES)
        names = [failure["name"] for failure in CONTRACT["named_failures"]]
        self.assertEqual(len(names), 12)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual({failure["outcome"] for failure in CONTRACT["named_failures"]}, OUTCOMES)
        self.assertEqual(
            sorted(failure["name"] for failure in CONTRACT["named_failures"] if failure["outcome"] == "refusal"),
            ["attempt-mismatch", "baseline-unfinished", "installation-mismatch"],
        )
        self.assertEqual(
            [effect["name"] for effect in CONTRACT["observable_effects"]],
            ["health-connect-token-request", "sender-storage-write"],
        )
        self.assertEqual(CONTRACT["dependencies"], [
            {"scope": "begin-initial-snapshot", "version": "2"},
            {"scope": "stage-outbox-batch", "version": "1"},
        ])
        self.assertTrue(CONTRACT["authority"]["owns"])
        self.assertTrue(any("begin-initial-snapshot" in entry for entry in CONTRACT["authority"]["does_not_own"]))
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["python", "typescript"])
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `begin-reconciliation-snapshot` |")]
        self.assertEqual(len(rows), 1)
        _, kind, purpose, inputs, outputs, depends_on = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(depends_on, ", ".join(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]))
        self.assertIn("loses change-token continuity", purpose)
        for phrase in ("record type", "active Sender Installation", "permission state", "token issuance"):
            self.assertIn(phrase, inputs)
        for phrase in ("Prior attempt retained as abandoned", "fresh pre-read changes token", "or no reconciliation begun", "in-transaction primitive", "unfinished Initial Snapshot"):
            self.assertIn(phrase, outputs)


if __name__ == "__main__":
    unittest.main()
