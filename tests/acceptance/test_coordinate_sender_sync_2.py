"""Acceptance tests for `coordinate-sender-sync@2`: the Governed Scope contract.

Requirement numbers refer to `packs/coordinate-sender-sync-2.spec.md`. The
`@1` suites run unchanged through `run_coordinate_sender_sync_1.sh`, and the
TypeScript suite `coordinate-sender-sync-2.test.ts` checks absolute deadlines.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/coordinate-sender-sync.2.json").read_text(encoding="utf-8"))
PREVIOUS = json.loads((ROOT / "governance/scopes/coordinate-sender-sync.1.json").read_text(encoding="utf-8"))


def described(items: list[dict], name: str) -> str:
    return next(item["description"] for item in items if item["name"] == name)


class R23GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual(CONTRACT["scope"], "coordinate-sender-sync")
        self.assertEqual(CONTRACT["version"], "2")
        self.assertEqual(CONTRACT["kind"], "integration")
        # Additive: @1's named failures, outcome classes, dependencies, and effects carry over unchanged.
        for key in ("named_failures", "observable_effects", "dependencies"):
            self.assertEqual(CONTRACT[key], PREVIOUS[key], key)
        self.assertEqual(len(CONTRACT["named_failures"]), 8)
        for key in ("inputs", "outputs"):
            self.assertEqual(
                [(item["name"], item.get("mode")) for item in CONTRACT[key]],
                [(item["name"], item.get("mode")) for item in PREVIOUS[key]],
                key,
            )
        self.assertIn("an optional absolute deadline on the coordinator's monotonic clock", described(CONTRACT["inputs"], "trigger"))
        self.assertIn("absolute deadlines", described(CONTRACT["inputs"], "clocks_and_identity"))
        self.assertIn(
            "A run whose deadline has passed when it opens still checkpoints, with every type deferred with time-budget-reached",
            described(CONTRACT["outputs"], "run_checkpointed"),
        )
        self.assertIn("bounded by a relative budget, an absolute deadline, or both", CONTRACT["purpose"])
        self.assertTrue(any(
            "a relative budget, an absolute deadline, or both, including how coalesced triggers combine them" in entry
            for entry in CONTRACT["authority"]["owns"]
        ))
        self.assertEqual(len(CONTRACT["authority"]["owns"]), len(PREVIOUS["authority"]["owns"]))
        self.assertEqual(len(CONTRACT["authority"]["does_not_own"]), len(PREVIOUS["authority"]["does_not_own"]))
        scheduling = [entry for entry in CONTRACT["authority"]["does_not_own"] if "schedule-background-polling" in entry]
        self.assertEqual(len(scheduling), 1)
        self.assertIn("the Sender shell turns that window into a background trigger's absolute deadline", scheduling[0])
        for owner in ("manage-sender-enrollment", "send-outbox-message", "stage-outbox-batch"):
            self.assertTrue(any(owner in entry for entry in CONTRACT["authority"]["does_not_own"]), owner)
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["specification"], "packs/coordinate-sender-sync-2.spec.md")
        self.assertEqual(oracle["runner"], "tests/acceptance/run_coordinate_sender_sync_2.sh")
        self.assertEqual(oracle["executable_tests"], {
            "typescript": "sender/app/test/coordinate-sender-sync-2.test.ts",
            "python": "tests/acceptance/test_coordinate_sender_sync_2.py",
        })
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
        self.assertIn("an optional absolute deadline on the monotonic clock", inputs)
        for phrase in (
            "the earlier of a relative budget and an absolute deadline",
            "a coalesced follow-up keeps the earliest absolute deadline unless an unbounded trigger lifts every limit",
            "a run that opens past its deadline still checkpoints with its types deferred",
            "at most one coalesced follow-up",
            "one completed-run result for checkpoint staging",
        ):
            self.assertIn(phrase, outputs)


if __name__ == "__main__":
    unittest.main()
