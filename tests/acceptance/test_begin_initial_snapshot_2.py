"""Acceptance tests for `begin-initial-snapshot@2`: the Governed Scope contract.

Requirement numbers refer to `packs/begin-initial-snapshot-2.spec.md`. The
`@1` suites run unchanged through `run_begin_initial_snapshot_1.sh`, and the
TypeScript suite `begin-initial-snapshot-2.test.ts` checks the new primitive.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/begin-initial-snapshot.2.json").read_text(encoding="utf-8"))
PREVIOUS = json.loads((ROOT / "governance/scopes/begin-initial-snapshot.1.json").read_text(encoding="utf-8"))
OUTCOMES = {"refusal", "not-permitted", "read-failure", "storage-failure"}
INPUT_MODES = {
    "record_type": "pure",
    "sender_installation": "pure",
    "abandoning": "pure",
    "attempt_commit": "pure",
    "snapshot_identity_source": "volatile",
    "current_time": "volatile",
    "permission_state": "volatile",
    "health_connect_token": "volatile",
    "sender_storage": "effectful",
}


class R12GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual(CONTRACT["scope"], "begin-initial-snapshot")
        self.assertEqual(CONTRACT["version"], "2")
        self.assertEqual(CONTRACT["kind"], "leaf")
        self.assertEqual({item["name"]: item["mode"] for item in CONTRACT["inputs"]}, INPUT_MODES)
        # Additive: @1's named failures, effects, and dependency carry over unchanged.
        self.assertEqual(CONTRACT["named_failures"], PREVIOUS["named_failures"])
        self.assertEqual({failure["outcome"] for failure in CONTRACT["named_failures"]}, OUTCOMES)
        self.assertEqual(
            [effect["name"] for effect in CONTRACT["observable_effects"]],
            ["health-connect-token-request", "sender-storage-write"],
        )
        self.assertEqual(CONTRACT["dependencies"], [{"scope": "project-source-record", "version": "1"}])
        self.assertLessEqual({item["name"] for item in PREVIOUS["outputs"]}, {item["name"] for item in CONTRACT["outputs"]})
        self.assertIn("attempt_committed", {item["name"] for item in CONTRACT["outputs"]})
        self.assertTrue(any("in-transaction primitive" in entry for entry in CONTRACT["authority"]["owns"]))
        self.assertTrue(any("begin-reconciliation-snapshot" in entry for entry in CONTRACT["authority"]["does_not_own"]))
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["kotlin", "python", "typescript"])
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `begin-initial-snapshot` |")]
        self.assertEqual(len(rows), 1)
        _, kind, purpose, inputs, outputs, depends_on = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(depends_on, ", ".join(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]))
        self.assertIn("before the first page is read", purpose)
        for phrase in ("Permission state", "record type", "active Sender Installation", "abandonment"):
            self.assertIn(phrase, inputs)
        for phrase in ("New snapshot identity and per-type changes token", "or no snapshot begun", "in-transaction primitive", "Initial or Reconciliation Snapshot attempt", "one-current-attempt rule"):
            self.assertIn(phrase, outputs)


if __name__ == "__main__":
    unittest.main()
