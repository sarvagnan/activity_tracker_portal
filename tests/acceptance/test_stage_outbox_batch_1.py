"""Acceptance tests for `stage-outbox-batch@1`: the Governed Scope contract.

Requirement numbers refer to `packs/stage-outbox-batch-1.spec.md`. The
TypeScript suite `stage-outbox-batch-1.test.ts` checks staging itself.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/stage-outbox-batch.1.json").read_text(encoding="utf-8"))
OUTCOMES = {"refusal", "block", "conflict", "cipher-failure", "storage-failure"}
INPUT_MODES = {
    "observed_page": "pure",
    "staging_context": "pure",
    "installation_cipher": "effectful",
    "sender_storage": "effectful",
}


class R1GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual(CONTRACT["scope"], "stage-outbox-batch")
        self.assertEqual(CONTRACT["version"], "1")
        self.assertEqual(CONTRACT["kind"], "leaf")
        self.assertEqual({item["name"]: item["mode"] for item in CONTRACT["inputs"]}, INPUT_MODES)
        names = [failure["name"] for failure in CONTRACT["named_failures"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual({failure["outcome"] for failure in CONTRACT["named_failures"]}, OUTCOMES)
        self.assertIn({"name": "indivisible-record-limit-exceeded", "outcome": "block"}, CONTRACT["named_failures"])
        self.assertEqual(
            [effect["name"] for effect in CONTRACT["observable_effects"]],
            ["installation-key-seal", "sender-storage-write"],
        )
        self.assertEqual(CONTRACT["dependencies"], [
            {"scope": "begin-initial-snapshot", "version": "1"},
            {"scope": "ingest-protocol", "version": "1"},
            {"scope": "read-change-page", "version": "1"},
            {"scope": "read-snapshot-page", "version": "1"},
        ])
        self.assertTrue(CONTRACT["authority"]["owns"])
        self.assertTrue(CONTRACT["authority"]["does_not_own"])
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["python", "typescript"])
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = {
            line.split("|")[1].strip(): [cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in scope_map.splitlines()
            if line.startswith("| `")
        }
        _, kind, purpose, inputs, outputs, depends_on = rows["`stage-outbox-batch`"]
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(
            sorted(depends_on.split(", ")),
            sorted(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]),
        )
        self.assertIn("before advancing its cursor", purpose)
        for phrase in ("snapshot or change-cycle identity", "tentative cursor", "current snapshot attempt", "installation key", "capability generation"):
            self.assertIn(phrase, inputs)
        for phrase in ("byte-stable encrypted Ingest Batch Outbox Messages", "committed together in the Sender Outbox", "pre-read token", "no commit on any failure or concurrent change", "visible block"):
            self.assertIn(phrase, outputs)
        # Decision 2: later stagers append through this unit's Outbox. The manage-sender-enrollment
        # edge waits for that unit's own Gate A, because published manage-sender-installation@1
        # pins its row (DEV-sob-01).
        for scope in ("`stage-sync-completion`", "`stage-sync-checkpoint`", "`send-outbox-message`"):
            self.assertIn("`stage-outbox-batch`", rows[scope][5], scope)


if __name__ == "__main__":
    unittest.main()
