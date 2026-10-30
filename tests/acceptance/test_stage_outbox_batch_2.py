"""Acceptance tests for `stage-outbox-batch@2`: the Governed Scope contract.

Requirement numbers refer to `packs/stage-outbox-batch-2.spec.md`. The
TypeScript suite `stage-outbox-batch-2.test.ts` checks the backlog reads and
quiet change-origin advances; `stage-outbox-batch@1`'s runner covers R1-R12.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIRST = json.loads((ROOT / "governance/scopes/stage-outbox-batch.1.json").read_text(encoding="utf-8"))
CONTRACT = json.loads((ROOT / "governance/scopes/stage-outbox-batch.2.json").read_text(encoding="utf-8"))
NEW_FAILURES = [
    {"name": "change-page-not-quiet", "outcome": "refusal"},
    {"name": "catch-up-unstaged", "outcome": "refusal"},
]


class R13GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual(CONTRACT["scope"], "stage-outbox-batch")
        self.assertEqual(CONTRACT["version"], "2")
        self.assertEqual(CONTRACT["kind"], FIRST["kind"])
        self.assertEqual(
            [(item["name"], item["mode"]) for item in CONTRACT["inputs"]],
            [(item["name"], item["mode"]) for item in FIRST["inputs"]],
        )
        self.assertEqual(
            [item["name"] for item in CONTRACT["outputs"]],
            [item["name"] for item in FIRST["outputs"]] + ["change_origin_advanced", "outbox_backlog"],
        )
        self.assertEqual(CONTRACT["named_failures"], FIRST["named_failures"] + NEW_FAILURES)
        names = [failure["name"] for failure in CONTRACT["named_failures"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(
            [effect["name"] for effect in CONTRACT["observable_effects"]],
            [effect["name"] for effect in FIRST["observable_effects"]],
        )
        self.assertEqual(CONTRACT["dependencies"], [
            {"scope": "begin-initial-snapshot", "version": "2"},
            {"scope": "ingest-protocol", "version": "1"},
            {"scope": "read-change-page", "version": "1"},
            {"scope": "read-snapshot-page", "version": "1"},
        ])
        for key in ("owns", "does_not_own"):
            self.assertEqual(CONTRACT["authority"][key][: len(FIRST["authority"][key])], FIRST["authority"][key], key)
        self.assertTrue(any("catch-up cycle" in item for item in CONTRACT["authority"]["owns"]))
        self.assertTrue(any("backlog" in item for item in CONTRACT["authority"]["owns"]))
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["python", "typescript"])
        self.assertEqual(oracle["specification"], "packs/stage-outbox-batch-2.spec.md")
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `stage-outbox-batch` |")]
        self.assertEqual(len(rows), 1)
        _, kind, purpose, inputs, outputs, depends_on = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(
            sorted(depends_on.split(", ")),
            sorted(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]),
        )
        self.assertIn("before advancing its cursor", purpose)
        for phrase in ("quiet change read", "advance the change origin", "catch-up cycle is still staged", "backlog reads", "without loading sealed messages"):
            self.assertIn(phrase, outputs)


if __name__ == "__main__":
    unittest.main()
