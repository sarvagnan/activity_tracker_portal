from __future__ import annotations

import json
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load(version: str) -> dict:
    path = PROJECT_ROOT / f"governance/scopes/coordinate-sender-sync.{version}.json"
    return json.loads(path.read_text(encoding="utf-8"))


class CoordinateSenderSyncTwoContractTest(unittest.TestCase):
    def test_coordinate_sender_sync_2_contract_has_required_shape(self) -> None:
        contract = load("2")
        self.assertEqual(contract["contract_schema"], 1)
        self.assertEqual(contract["scope"], "coordinate-sender-sync")
        self.assertEqual(contract["version"], "2")
        self.assertEqual(contract["kind"], "integration")
        oracle = contract["acceptance_oracle"]
        self.assertEqual(oracle["specification"], "packs/coordinate-sender-sync-2.spec.md")
        self.assertEqual(set(oracle["executable_tests"]), {"python", "typescript"})
        self.assertTrue((PROJECT_ROOT / oracle["specification"]).is_file(), oracle["specification"])

    def test_coordinate_sender_sync_2_is_additive_over_1(self) -> None:
        first, second = load("1"), load("2")
        self.assertEqual(set(second), set(first))
        for key in ("kind", "named_failures", "observable_effects", "dependencies"):
            self.assertEqual(second[key], first[key], key)
        for key in ("inputs", "outputs"):
            self.assertEqual([item["name"] for item in second[key]], [item["name"] for item in first[key]], key)
            self.assertEqual([item.get("mode") for item in second[key]], [item.get("mode") for item in first[key]], key)
        trigger = next(item for item in second["inputs"] if item["name"] == "trigger")
        self.assertIn("absolute deadline", trigger["description"])
        self.assertEqual(len(second["authority"]["owns"]), len(first["authority"]["owns"]))
        self.assertEqual(len(second["authority"]["does_not_own"]), len(first["authority"]["does_not_own"]))


if __name__ == "__main__":
    unittest.main()
