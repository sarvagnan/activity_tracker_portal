"""Acceptance tests for `send-outbox-message@1`: the Governed Scope contract.

Requirement numbers refer to `packs/send-outbox-message-1.spec.md`.
The TypeScript suite `send-outbox-message-1.test.ts` checks the module.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/send-outbox-message.1.json").read_text(encoding="utf-8"))
OUTCOMES = {"retryable", "head-block", "cipher-failure", "storage-failure"}
INPUT_MODES = {
    "delivery_context": "pure",
    "outbox_head": "effectful",
    "installation_key": "effectful",
    "portal_transport": "effectful",
    "portal_response": "volatile",
    "local_notifier": "effectful",
    "sender_storage": "effectful",
    "clock": "volatile",
}


class R1GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual(CONTRACT["scope"], "send-outbox-message")
        self.assertEqual(CONTRACT["version"], "1")
        self.assertEqual(CONTRACT["kind"], "leaf")
        self.assertTrue(CONTRACT["purpose"])
        self.assertEqual({item["name"]: item["mode"] for item in CONTRACT["inputs"]}, INPUT_MODES)
        self.assertEqual(
            [output["name"] for output in CONTRACT["outputs"]],
            ["delivery_request", "attempt_outcome", "receipt_bound_removal", "head_block_condition"],
        )
        names = [failure["name"] for failure in CONTRACT["named_failures"]]
        self.assertEqual(len(names), 11)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual({failure["outcome"] for failure in CONTRACT["named_failures"]}, OUTCOMES)
        by_outcome = {
            outcome: sorted(failure["name"] for failure in CONTRACT["named_failures"] if failure["outcome"] == outcome)
            for outcome in OUTCOMES
        }
        self.assertEqual(by_outcome["retryable"], [
            "keystore-unavailable", "receipt-mismatch", "storage-reserve-exhausted", "temporarily-unavailable", "transport-failed",
        ])
        self.assertEqual(by_outcome["head-block"], ["delivery-blocked", "head-invalid", "head-unreadable"])
        self.assertEqual(by_outcome["cipher-failure"], ["missing-key", "unusable-key"])
        self.assertEqual(by_outcome["storage-failure"], ["storage-failed"])
        self.assertEqual(
            [effect["name"] for effect in CONTRACT["observable_effects"]],
            ["portal-delivery-request", "local-notification", "sender-storage-write"],
        )
        self.assertEqual(CONTRACT["dependencies"], [
            {"scope": "ingest-protocol", "version": "1"},
            {"scope": "stage-outbox-batch", "version": "1"},
            {"scope": "stage-sync-completion", "version": "1"},
            {"scope": "stage-sync-checkpoint", "version": "1"},
        ])
        self.assertTrue(CONTRACT["authority"]["owns"])
        for owner in ("ingest-protocol", "stage-outbox-batch", "manage-sender-enrollment", "accept-* units",
                      "Portal ingress shell", "Sender shell enabling task", "coordinate-sender-sync", "receipt-bound-delivery"):
            self.assertTrue(any(owner in entry for entry in CONTRACT["authority"]["does_not_own"]), owner)
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["python", "typescript"])
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `send-outbox-message` |")]
        self.assertEqual(len(rows), 1)
        _, kind, purpose, inputs, outputs, depends_on = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(sorted(depends_on.split(", ")), sorted(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]))
        self.assertIn("byte-stable data or control message", purpose)
        for phrase in ("Oldest Outbox Message", "installation key", "Portal URL from enrollment"):
            self.assertIn(phrase, inputs)
        for phrase in ("Matching Delivery Receipt", "exact uncompressed bytes", "receipt-bound local removal",
                       "persistent head block", "cleared only by that head's receipt"):
            self.assertIn(phrase, outputs)


if __name__ == "__main__":
    unittest.main()
