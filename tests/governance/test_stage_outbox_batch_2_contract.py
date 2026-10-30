from __future__ import annotations

import json
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load(version: str) -> dict:
    path = PROJECT_ROOT / f"governance/scopes/stage-outbox-batch.{version}.json"
    return json.loads(path.read_text(encoding="utf-8"))


class StageOutboxBatchTwoContractTest(unittest.TestCase):
    def test_stage_outbox_batch_2_contract_has_required_shape(self) -> None:
        contract = load("2")
        self.assertEqual(contract["contract_schema"], 1)
        self.assertEqual(contract["scope"], "stage-outbox-batch")
        self.assertEqual(contract["version"], "2")
        self.assertEqual(contract["kind"], "leaf")
        oracle = contract["acceptance_oracle"]
        self.assertEqual(oracle["specification"], "packs/stage-outbox-batch-2.spec.md")
        self.assertEqual(set(oracle["executable_tests"]), {"python", "typescript"})
        self.assertTrue((PROJECT_ROOT / oracle["specification"]).is_file(), oracle["specification"])

    def test_stage_outbox_batch_2_is_additive_over_1(self) -> None:
        first, second = load("1"), load("2")
        self.assertEqual(set(second), set(first))
        self.assertEqual(second["kind"], first["kind"])
        self.assertEqual(
            [(item["name"], item.get("mode")) for item in second["inputs"]],
            [(item["name"], item.get("mode")) for item in first["inputs"]],
        )
        self.assertEqual(
            [item["name"] for item in second["outputs"]],
            [item["name"] for item in first["outputs"]] + ["change_origin_advanced", "outbox_backlog"],
        )
        self.assertEqual(
            second["named_failures"],
            first["named_failures"] + [
                {"name": "change-page-not-quiet", "outcome": "refusal"},
                {"name": "catch-up-unstaged", "outcome": "refusal"},
            ],
        )
        self.assertEqual([item["name"] for item in second["observable_effects"]], [item["name"] for item in first["observable_effects"]])
        self.assertEqual(second["authority"]["owns"][: len(first["authority"]["owns"])], first["authority"]["owns"])
        self.assertEqual(
            second["authority"]["does_not_own"][: len(first["authority"]["does_not_own"])], first["authority"]["does_not_own"]
        )
        self.assertEqual(
            {(item["scope"], item["version"]) for item in second["dependencies"]},
            {(item["scope"], item["version"]) for item in first["dependencies"]} - {("begin-initial-snapshot", "1")}
            | {("begin-initial-snapshot", "2")},
        )


if __name__ == "__main__":
    unittest.main()
