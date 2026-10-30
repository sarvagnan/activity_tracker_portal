"""Acceptance tests for `stage-sync-checkpoint@2`: the Governed Scope contract.

Requirement numbers refer to `packs/stage-sync-checkpoint-2.spec.md`. The
TypeScript suite `stage-sync-checkpoint-2.test.ts` checks that staging loads no
sealed message; `stage-sync-checkpoint@1`'s runner covers R1-R9.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIRST = json.loads((ROOT / "governance/scopes/stage-sync-checkpoint.1.json").read_text(encoding="utf-8"))
CONTRACT = json.loads((ROOT / "governance/scopes/stage-sync-checkpoint.2.json").read_text(encoding="utf-8"))


class R10GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual(CONTRACT["scope"], "stage-sync-checkpoint")
        self.assertEqual(CONTRACT["version"], "2")
        self.assertEqual(CONTRACT["kind"], FIRST["kind"])
        self.assertEqual(CONTRACT["named_failures"], FIRST["named_failures"])
        for key in ("inputs", "outputs", "observable_effects"):
            self.assertEqual(
                [(item["name"], item.get("mode")) for item in CONTRACT[key]],
                [(item["name"], item.get("mode")) for item in FIRST[key]],
                key,
            )
        self.assertEqual(CONTRACT["dependencies"], [
            {"scope": "ingest-protocol", "version": "1"},
            {"scope": "stage-outbox-batch", "version": "2"},
        ])
        self.assertEqual(CONTRACT["authority"]["owns"][: len(FIRST["authority"]["owns"])], FIRST["authority"]["owns"])
        self.assertTrue(any("without loading sealed messages" in item for item in CONTRACT["authority"]["owns"]))
        self.assertEqual(CONTRACT["authority"]["does_not_own"], FIRST["authority"]["does_not_own"])
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["python", "typescript"])
        self.assertEqual(oracle["specification"], "packs/stage-sync-checkpoint-2.spec.md")
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `stage-sync-checkpoint` |")]
        self.assertEqual(len(rows), 1)
        _, kind, purpose, inputs, outputs, depends_on = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(
            sorted(depends_on.split(", ")),
            sorted(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]),
        )
        self.assertIn("without carrying health data", purpose)
        self.assertIn("pre-checkpoint outbox message and byte counts read without loading sealed messages", inputs)


if __name__ == "__main__":
    unittest.main()
