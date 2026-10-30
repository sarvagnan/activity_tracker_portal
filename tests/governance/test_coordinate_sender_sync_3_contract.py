from __future__ import annotations

import json
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load(version: str) -> dict:
    path = PROJECT_ROOT / f"governance/scopes/coordinate-sender-sync.{version}.json"
    return json.loads(path.read_text(encoding="utf-8"))


class CoordinateSenderSyncThreeContractTest(unittest.TestCase):
    def test_coordinate_sender_sync_3_contract_has_required_shape(self) -> None:
        contract = load("3")
        self.assertEqual(contract["contract_schema"], 1)
        self.assertEqual(contract["scope"], "coordinate-sender-sync")
        self.assertEqual(contract["version"], "3")
        self.assertEqual(contract["kind"], "integration")
        oracle = contract["acceptance_oracle"]
        self.assertEqual(oracle["specification"], "packs/coordinate-sender-sync-3.spec.md")
        self.assertEqual(set(oracle["executable_tests"]), {"python", "typescript"})
        self.assertTrue((PROJECT_ROOT / oracle["specification"]).is_file(), oracle["specification"])

    def test_coordinate_sender_sync_3_is_additive_over_2(self) -> None:
        second, third = load("2"), load("3")
        self.assertEqual(set(third), set(second))
        for key in ("kind", "named_failures", "authority"):
            self.assertEqual(third[key], second[key], key)
        for key in ("inputs", "outputs", "observable_effects"):
            self.assertEqual(
                [(item["name"], item.get("mode")) for item in third[key]],
                [(item["name"], item.get("mode")) for item in second[key]],
                key,
            )
        source_readers = next(item for item in third["inputs"] if item["name"] == "source_readers")
        self.assertIn("quiet change reads", source_readers["description"])
        expected = {(item["scope"], item["version"]) for item in second["dependencies"]}
        expected -= {("stage-outbox-batch", "1"), ("stage-sync-checkpoint", "1")}
        expected |= {("stage-outbox-batch", "2"), ("stage-sync-checkpoint", "2")}
        self.assertEqual({(item["scope"], item["version"]) for item in third["dependencies"]}, expected)


if __name__ == "__main__":
    unittest.main()
