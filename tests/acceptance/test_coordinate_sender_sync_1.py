"""Acceptance tests for `coordinate-sender-sync@1`: the Governed Scope contract.

Requirement numbers refer to `packs/coordinate-sender-sync-1.spec.md`.
The TypeScript suite `coordinate-sender-sync-1.test.ts` checks the coordinator.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/coordinate-sender-sync.1.json").read_text(encoding="utf-8"))
OUTCOMES = {"refusal", "conflict", "state-mismatch", "cipher-failure", "storage-failure"}
INPUT_MODES = {
    "trigger": "pure",
    "retry_intent": "pure",
    "managed_record_types": "pure",
    "projection_versions": "pure",
    "concurrency": "pure",
    "staging_context": "volatile",
    "sync_conditions": "volatile",
    "background_scheduling_status": "volatile",
    "source_readers": "effectful",
    "installation_cipher": "effectful",
    "sender_storage": "effectful",
    "clocks_and_identity": "volatile",
}
DEPENDENCIES = [
    "begin-initial-snapshot",
    "begin-reconciliation-snapshot",
    "ingest-protocol",
    "read-change-page",
    "read-snapshot-page",
    "stage-outbox-batch",
    "stage-sync-checkpoint",
    "stage-sync-completion",
]


class R1GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual(CONTRACT["scope"], "coordinate-sender-sync")
        self.assertEqual(CONTRACT["version"], "1")
        self.assertEqual(CONTRACT["kind"], "integration")
        self.assertEqual({item["name"]: item["mode"] for item in CONTRACT["inputs"]}, INPUT_MODES)
        names = [failure["name"] for failure in CONTRACT["named_failures"]]
        self.assertEqual(len(names), 8)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual({failure["outcome"] for failure in CONTRACT["named_failures"]}, OUTCOMES)
        by_outcome = {
            outcome: sorted(failure["name"] for failure in CONTRACT["named_failures"] if failure["outcome"] == outcome)
            for outcome in OUTCOMES
        }
        self.assertEqual(by_outcome["refusal"], ["retry-not-blocked"])
        self.assertEqual(by_outcome["conflict"], ["staging-conflict"])
        self.assertEqual(by_outcome["state-mismatch"], ["sync-state-mismatch"])
        self.assertEqual(by_outcome["storage-failure"], ["storage-failed"])
        self.assertEqual(
            [effect["name"] for effect in CONTRACT["observable_effects"]],
            ["health-connect-read", "installation-key-seal", "sender-storage-write"],
        )
        self.assertEqual(CONTRACT["dependencies"], [{"scope": scope, "version": "2" if scope == "begin-initial-snapshot" else "1"} for scope in DEPENDENCIES])
        self.assertTrue(CONTRACT["authority"]["owns"])
        for owner in ("schedule-background-polling", "manage-sender-enrollment", "send-outbox-message", "stage-outbox-batch"):
            self.assertTrue(any(owner in entry for entry in CONTRACT["authority"]["does_not_own"]), owner)
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["python", "typescript"])
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `coordinate-sender-sync` |")]
        self.assertEqual(len(rows), 1)
        _, kind, purpose, inputs, outputs, depends_on = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(sorted(depends_on.split(", ")), sorted(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]))
        self.assertIn("coalescing overlapping automatic and manual triggers", purpose)
        for phrase in ("Projection Refusal retry intent accepted only from the Sender", "collection-enabled state",
                       "configurable cross-type concurrency defaulting to two"):
            self.assertIn(phrase, inputs)
        for phrase in ("A run marker before reads", "at most one coalesced follow-up", "health-data-free identity",
                       "reports affected types deferred with reason collection-disabled", "one completed-run result for checkpoint staging"):
            self.assertIn(phrase, outputs)


if __name__ == "__main__":
    unittest.main()
