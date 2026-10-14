"""Acceptance tests for `begin-initial-snapshot@1`: the Governed Scope contract.

Requirement numbers refer to `packs/begin-initial-snapshot-1.spec.md`. The
Kotlin suite `IssueChangesToken1Test` checks token issuance, and the
TypeScript suite `begin-initial-snapshot-1.test.ts` checks the durable begin.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/begin-initial-snapshot.1.json").read_text(encoding="utf-8"))
OUTCOMES = {"refusal", "not-permitted", "read-failure", "storage-failure"}
INPUT_MODES = {
    "record_type": "pure",
    "sender_installation": "pure",
    "abandoning": "pure",
    "snapshot_identity_source": "volatile",
    "current_time": "volatile",
    "permission_state": "volatile",
    "health_connect_token": "volatile",
    "sender_storage": "effectful",
}


class R1GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual(CONTRACT["scope"], "begin-initial-snapshot")
        self.assertEqual(CONTRACT["version"], "1")
        self.assertEqual(CONTRACT["kind"], "leaf")
        self.assertEqual({item["name"]: item["mode"] for item in CONTRACT["inputs"]}, INPUT_MODES)
        names = [failure["name"] for failure in CONTRACT["named_failures"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual({failure["outcome"] for failure in CONTRACT["named_failures"]}, OUTCOMES)
        self.assertEqual(
            [effect["name"] for effect in CONTRACT["observable_effects"]],
            ["health-connect-token-request", "sender-storage-write"],
        )
        self.assertEqual(CONTRACT["dependencies"], [{"scope": "project-source-record", "version": "1"}])
        self.assertTrue(CONTRACT["authority"]["owns"])
        self.assertTrue(CONTRACT["authority"]["does_not_own"])
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
        for phrase in ("Permission state", "record type", "active Sender Installation", "abandonment", "volatile Health Connect token issuance"):
            self.assertIn(phrase, inputs)
        for phrase in ("New snapshot identity and per-type changes token", "the instant read before it was requested", "committed durably in Sender storage", "or no snapshot begun"):
            self.assertIn(phrase, outputs)


if __name__ == "__main__":
    unittest.main()
