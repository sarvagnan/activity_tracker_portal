"""Acceptance tests for `coordinate-sender-sync@3`: the Governed Scope contract.

Requirement numbers refer to `packs/coordinate-sender-sync-3.spec.md`. The
`@2` suites, which run `@1`'s, run unchanged through
`run_coordinate_sender_sync_2.sh`, and the TypeScript suite
`coordinate-sender-sync-3.test.ts` checks quiet change reads.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/coordinate-sender-sync.3.json").read_text(encoding="utf-8"))
PREVIOUS = json.loads((ROOT / "governance/scopes/coordinate-sender-sync.2.json").read_text(encoding="utf-8"))


def described(items: list[dict], name: str) -> str:
    return next(item["description"] for item in items if item["name"] == name)


class R29GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual(CONTRACT["scope"], "coordinate-sender-sync")
        self.assertEqual(CONTRACT["version"], "3")
        self.assertEqual(CONTRACT["kind"], "integration")
        for key in ("named_failures", "observable_effects", "authority"):
            self.assertEqual(CONTRACT[key], PREVIOUS[key], key)
        for key in ("inputs", "outputs"):
            self.assertEqual(
                [(item["name"], item.get("mode")) for item in CONTRACT[key]],
                [(item["name"], item.get("mode")) for item in PREVIOUS[key]],
                key,
            )
        self.assertIn("quiet change reads", described(CONTRACT["inputs"], "source_readers"))
        self.assertIn("with no Ingest Batch and no Change Completion", described(CONTRACT["outputs"], "staged_progress"))
        self.assertIn("opt-in quiet change reads", CONTRACT["purpose"])
        expected = {(item["scope"], item["version"]) for item in PREVIOUS["dependencies"]}
        expected -= {("stage-outbox-batch", "1"), ("stage-sync-checkpoint", "1")}
        expected |= {("stage-outbox-batch", "2"), ("stage-sync-checkpoint", "2")}
        self.assertEqual({(item["scope"], item["version"]) for item in CONTRACT["dependencies"]}, expected)
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["python", "typescript"])
        self.assertEqual(oracle["specification"], "packs/coordinate-sender-sync-3.spec.md")
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `coordinate-sender-sync` |")]
        self.assertEqual(len(rows), 1)
        _, kind, purpose, inputs, outputs, depends_on = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(
            sorted(depends_on.split(", ")),
            sorted(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]),
        )
        self.assertIn("When quiet change reads are enabled", outputs)
        self.assertIn("advances the change origin through stage-outbox-batch without staging a message", outputs)


if __name__ == "__main__":
    unittest.main()
