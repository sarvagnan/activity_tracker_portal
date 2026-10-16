"""Acceptance tests for `manage-sender-enrollment@1`: the Governed Scope contract.

Requirement numbers refer to `packs/manage-sender-enrollment-1.spec.md`.
The TypeScript suite `manage-sender-enrollment-1.test.ts` checks the module.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/manage-sender-enrollment.1.json").read_text(encoding="utf-8"))
OUTCOMES = {"refusal", "portal-refusal", "retryable", "cipher-failure", "storage-failure"}
INPUT_MODES = {
    "enrollment_offer": "pure",
    "operator_intent": "pure",
    "secure_lock": "volatile",
    "identity_source": "volatile",
    "installation_key": "effectful",
    "portal_transport": "effectful",
    "portal_response": "volatile",
    "sender_storage": "effectful",
    "clock": "volatile",
}


class R1GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["contract_schema"], 1)
        self.assertEqual(CONTRACT["scope"], "manage-sender-enrollment")
        self.assertEqual(CONTRACT["version"], "1")
        self.assertEqual(CONTRACT["kind"], "integration")
        self.assertIn("drain, unlink, or destructively reset", CONTRACT["purpose"])
        self.assertEqual({item["name"]: item["mode"] for item in CONTRACT["inputs"]}, INPUT_MODES)
        self.assertEqual(
            [output["name"] for output in CONTRACT["outputs"]],
            ["installation_identity", "enrollment_state", "capability_acknowledgement", "sync_contexts", "local_destruction"],
        )
        names = [failure["name"] for failure in CONTRACT["named_failures"]]
        self.assertEqual(len(names), 13)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual({failure["outcome"] for failure in CONTRACT["named_failures"]}, OUTCOMES)
        by_outcome = {
            outcome: sorted(failure["name"] for failure in CONTRACT["named_failures"] if failure["outcome"] == outcome)
            for outcome in OUTCOMES
        }
        self.assertEqual(by_outcome["refusal"], [
            "already-enrolled", "not-confirmed", "not-confirming", "not-drained", "not-enrolled", "offer-invalid", "screen-lock-required",
        ])
        self.assertEqual(by_outcome["portal-refusal"], ["capabilities-unsupported", "enrollment-refused"])
        self.assertEqual(by_outcome["retryable"], ["temporarily-unavailable", "transport-failed"])
        self.assertEqual(by_outcome["cipher-failure"], ["cipher-failed"])
        self.assertEqual(by_outcome["storage-failure"], ["storage-failed"])
        self.assertEqual(
            [effect["name"] for effect in CONTRACT["observable_effects"]],
            ["installation-key-lifecycle", "portal-enrollment-request", "sender-storage-write"],
        )
        self.assertEqual(CONTRACT["dependencies"], [
            {"scope": "ingest-protocol", "version": "1"},
            {"scope": "stage-outbox-batch", "version": "1"},
        ])
        self.assertTrue(CONTRACT["authority"]["owns"])
        for owner in ("ingest-protocol", "stage-outbox-batch", "manage-sender-installation", "send-outbox-message",
                      "coordinate-sender-sync", "Sender shell enabling task"):
            self.assertTrue(any(owner in entry for entry in CONTRACT["authority"]["does_not_own"]), owner)
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["languages"], ["python", "typescript"])
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `manage-sender-enrollment` |")]
        self.assertEqual(len(rows), 1)
        _, kind, purpose, inputs, outputs, depends_on = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(kind, CONTRACT["kind"])
        self.assertEqual(sorted(depends_on.split(", ")), sorted(f"`{dependency['scope']}`" for dependency in CONTRACT["dependencies"]))
        self.assertIn("drain, unlink, or destructively reset", purpose)
        for phrase in ("enrollment offer", "secure screen lock", "Operator intent", "Portal responses", "installation key"):
            self.assertIn(phrase, inputs)
        for phrase in ("byte-stable enrollment confirmation", "first Outbox Message", "Operator confirmed it",
                       "staging and delivery contexts", "final sequence once the Outbox is empty", "only on explicit intent"):
            self.assertIn(phrase, outputs)


if __name__ == "__main__":
    unittest.main()
