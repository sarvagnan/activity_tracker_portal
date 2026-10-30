from __future__ import annotations

import json
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load(version: str) -> dict:
    path = PROJECT_ROOT / f"governance/scopes/manage-sender-enrollment.{version}.json"
    return json.loads(path.read_text(encoding="utf-8"))


class ManageSenderEnrollmentTwoContractTest(unittest.TestCase):
    def test_manage_sender_enrollment_2_contract_has_required_shape(self) -> None:
        contract = load("2")
        self.assertEqual(contract["contract_schema"], 1)
        self.assertEqual(contract["scope"], "manage-sender-enrollment")
        self.assertEqual(contract["version"], "2")
        self.assertEqual(contract["kind"], "integration")
        oracle = contract["acceptance_oracle"]
        self.assertEqual(oracle["specification"], "packs/manage-sender-enrollment-2.spec.md")
        self.assertEqual(set(oracle["executable_tests"]), {"python", "typescript"})
        self.assertTrue((PROJECT_ROOT / oracle["specification"]).is_file(), oracle["specification"])

    def test_manage_sender_enrollment_2_is_additive_over_1(self) -> None:
        first, second = load("1"), load("2")
        self.assertEqual(set(second), set(first))
        for key in ("kind", "named_failures"):
            self.assertEqual(second[key], first[key], key)
        for key in ("inputs", "outputs", "observable_effects"):
            self.assertEqual(
                [(item["name"], item.get("mode")) for item in second[key]],
                [(item["name"], item.get("mode")) for item in first[key]],
                key,
            )
        self.assertEqual(second["authority"]["owns"][: len(first["authority"]["owns"])], first["authority"]["owns"])
        self.assertEqual(second["authority"]["does_not_own"], first["authority"]["does_not_own"])
        self.assertEqual(
            sorted((item["scope"], item["version"]) for item in second["dependencies"]),
            [("ingest-protocol", "1"), ("stage-outbox-batch", "2")],
        )


if __name__ == "__main__":
    unittest.main()
