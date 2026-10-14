"""Acceptance tests for `stage-sync-checkpoint@1`: the Governed Scope contract.

Requirement numbers refer to `packs/stage-sync-checkpoint-1.spec.md`.
The TypeScript suite `stage-sync-checkpoint-1.test.ts` checks the begin.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/stage-sync-checkpoint.1.json").read_text(encoding="utf-8"))
OUTCOMES = {"refusal", "conflict", "cipher-failure", "storage-failure"}
INPUT_MODES = {
    "run_summary": "pure",
    "staging_context": "pure",
    "installation_cipher": "effectful",
    "sender_storage": "effectful",
}


class R1GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual(CONTRACT["scope"], "stage-sync-checkpoint")
        self.assertEqual(CONTRACT["version"], "1")
        self.assertEqual(CONTRACT["kind"], "leaf")
        self.assertEqual({item["name"]: item["mode"] for item in CONTRACT["inputs"]}, INPUT_MODES)
        names = [failure["name"] for failure in CONTRACT["named_failures"]]
        self.assertEqual(len(names), 7)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual({failure["outcome"] for failure in CONTRACT["named_failures"]}, OUTCOMES)
        self.assertEqual(
            sorted(failure["name"] for failure in CONTRACT["named_failures"] if failure["outcome"] == "refusal"),
            ["checkpoint-already-staged"],
        )
        self.assertEqual(
            [effect["name"] for effect in CONTRACT["observable_effects"]],
            ["installation-key-seal", "sender-storage-write"],
        )
        self.assertEqual(CONTRACT["dependencies"], [
            {"scope": "ingest-protocol", "version": "1"},
            {"scope": "stage-outbox-batch", "version": "1"},
        ])
        self.assertTrue(CONTRACT["authority"]["owns"])
        self.assertTrue(any("stage-outbox-batch" in entry for entry in CONTRACT["authority"]["does_not_own"]))
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["python", "typescript"])
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `stage-sync-checkpoint` |")]
        self.assertEqual(len(rows), 1)
        _, kind, purpose, inputs, outputs, depends_on = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(sorted(depends_on.split(", ")), sorted(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]))
        self.assertIn("without carrying health data", purpose)
        for phrase in ("Durable run sequence and trigger", "source-data-staged flag", "pre-checkpoint outbox message and byte counts"):
            self.assertIn(phrase, inputs)
        for phrase in ("byte-stable Sync Checkpoint", "no source-record counts", "Source Identities", "health values"):
            self.assertIn(phrase, outputs)


if __name__ == "__main__":
    unittest.main()
