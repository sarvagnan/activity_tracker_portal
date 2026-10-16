from __future__ import annotations

import json
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_KEYS = {
    "contract_schema",
    "scope",
    "version",
    "kind",
    "purpose",
    "inputs",
    "outputs",
    "named_failures",
    "observable_effects",
    "authority",
    "dependencies",
    "acceptance_oracle",
}
OUTCOMES = {"retryable"}


class ReceiptBoundDeliveryContractTest(unittest.TestCase):
    def test_receipt_bound_delivery_contract_has_required_shape(self) -> None:
        path = PROJECT_ROOT / "governance/scopes/receipt-bound-delivery.1.json"
        contract = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(set(contract), REQUIRED_KEYS)
        self.assertEqual(contract["contract_schema"], 1)
        self.assertEqual(contract["scope"], "receipt-bound-delivery")
        self.assertEqual(contract["version"], "1")
        self.assertEqual(contract["kind"], "integration")
        self.assertTrue(contract["purpose"])
        self.assertLessEqual({item["mode"] for item in contract["inputs"]}, {"pure", "effectful", "volatile"})
        self.assertTrue(contract["outputs"])
        names = [failure["name"] for failure in contract["named_failures"]]
        self.assertTrue(names)
        self.assertEqual(len(names), len(set(names)))
        for failure in contract["named_failures"]:
            self.assertEqual(set(failure), {"name", "outcome"})
            self.assertIn(failure["outcome"], OUTCOMES)
        self.assertEqual({failure["outcome"] for failure in contract["named_failures"]}, OUTCOMES)
        self.assertTrue(contract["observable_effects"])
        for effect in contract["observable_effects"]:
            self.assertEqual(set(effect), {"name", "description"})
        self.assertEqual(
            contract["dependencies"],
            [
                {"scope": scope, "version": "1"}
                for scope in (
                    "send-outbox-message", "accept-ingest-batch", "accept-sync-completion", "accept-sync-checkpoint",
                    "accept-capability-acknowledgement", "manage-sender-enrollment",
                )
            ],
        )
        self.assertEqual(set(contract["authority"]), {"owns", "does_not_own"})
        self.assertTrue(contract["authority"]["owns"])
        self.assertTrue(contract["authority"]["does_not_own"])
        oracle = contract["acceptance_oracle"]
        self.assertEqual(set(oracle["executable_tests"]), {"python", "typescript"})
        self.assertTrue((PROJECT_ROOT / oracle["specification"]).is_file(), oracle["specification"])


if __name__ == "__main__":
    unittest.main()
