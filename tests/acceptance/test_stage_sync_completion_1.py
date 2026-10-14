"""Acceptance tests for `stage-sync-completion@1`: the Governed Scope contract.

Requirement numbers refer to `packs/stage-sync-completion-1.spec.md`.
The TypeScript suite `stage-sync-completion-1.test.ts` checks the begin.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/stage-sync-completion.1.json").read_text(encoding="utf-8"))
OUTCOMES = {"refusal", "conflict", "cipher-failure", "storage-failure"}
INPUT_MODES = {
    "completed_cycle": "pure",
    "staging_context": "pure",
    "installation_cipher": "effectful",
    "sender_storage": "effectful",
}


class R1GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual(CONTRACT["scope"], "stage-sync-completion")
        self.assertEqual(CONTRACT["version"], "1")
        self.assertEqual(CONTRACT["kind"], "leaf")
        self.assertEqual({item["name"]: item["mode"] for item in CONTRACT["inputs"]}, INPUT_MODES)
        names = [failure["name"] for failure in CONTRACT["named_failures"]]
        self.assertEqual(len(names), 10)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual({failure["outcome"] for failure in CONTRACT["named_failures"]}, OUTCOMES)
        self.assertEqual(
            sorted(failure["name"] for failure in CONTRACT["named_failures"] if failure["outcome"] == "refusal"),
            ["chain-incomplete", "completion-already-staged", "cycle-not-current", "installation-mismatch"],
        )
        self.assertEqual(
            [effect["name"] for effect in CONTRACT["observable_effects"]],
            ["installation-key-seal", "sender-storage-write"],
        )
        self.assertEqual(CONTRACT["dependencies"], [
            {"scope": "begin-initial-snapshot", "version": "2"},
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
        rows = [line for line in scope_map.splitlines() if line.startswith("| `stage-sync-completion` |")]
        self.assertEqual(len(rows), 1)
        _, kind, purpose, inputs, outputs, depends_on = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(sorted(depends_on.split(", ")), sorted(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]))
        self.assertIn("only after its entire local page chain", purpose)
        for phrase in ("Completion kind and identity", "final source-page, fragment, and record counts", "no source cursor or token"):
            self.assertIn(phrase, inputs)
        for phrase in ("ordered after every fragment", "local chain is incomplete", "no longer current", "exactly once per cycle"):
            self.assertIn(phrase, outputs)


if __name__ == "__main__":
    unittest.main()
