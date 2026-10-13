"""Acceptance tests for `read-snapshot-page@1`: the Governed Scope contract.

Requirement numbers refer to `packs/read-snapshot-page-1.spec.md`. The Kotlin
suite `ReadSnapshotPage1Test` checks the reader itself against a synthetic
Health Connect.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/read-snapshot-page.1.json").read_text(encoding="utf-8"))
OUTCOMES = {"projection-refusal", "not-permitted", "read-failure", "invalid-position"}
INPUT_MODES = {
    "record_type": "pure",
    "snapshot_identity": "pure",
    "snapshot_page_position": "pure",
    "projection_context": "pure",
    "permission_state": "volatile",
    "health_connect_records": "volatile",
}


class R1GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual(CONTRACT["scope"], "read-snapshot-page")
        self.assertEqual(CONTRACT["version"], "1")
        self.assertEqual(CONTRACT["kind"], "leaf")
        self.assertEqual({item["name"]: item["mode"] for item in CONTRACT["inputs"]}, INPUT_MODES)
        names = [failure["name"] for failure in CONTRACT["named_failures"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual({failure["outcome"] for failure in CONTRACT["named_failures"]}, OUTCOMES)
        self.assertEqual([effect["name"] for effect in CONTRACT["observable_effects"]], ["health-connect-read"])
        self.assertEqual(CONTRACT["dependencies"], [{"scope": "project-source-record", "version": "1"}])
        self.assertTrue(CONTRACT["authority"]["owns"])
        self.assertTrue(CONTRACT["authority"]["does_not_own"])
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["kotlin", "python"])
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `read-snapshot-page` |")]
        self.assertEqual(len(rows), 1)
        _, kind, purpose, inputs, outputs, depends_on = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(depends_on, ", ".join(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]))
        self.assertIn("without claiming durable progress", purpose)
        self.assertIn("claims no durable progress", CONTRACT["purpose"])
        for phrase in ("Permission state", "record type", "snapshot identity", "snapshot page position", "volatile Health Connect state"):
            self.assertIn(phrase, inputs)
        for phrase in ("tentative next position", "exhaustion state", "`projection-refusal` with no cursor advancement", "transient Health Connect failure"):
            self.assertIn(phrase, outputs)


if __name__ == "__main__":
    unittest.main()
