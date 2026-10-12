"""Acceptance tests for `ingest-protocol@1` on the Python Portal.

The shared vectors under `protocol/v1/vectors/` are the oracle; these tests
only route each vector to the production Interface. Requirement numbers refer
to `packs/ingest-protocol-1.spec.md`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from activity_tracker_portal import ingest_protocol as protocol

ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "protocol/v1/vectors"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def raw(field: dict[str, str]) -> bytes:
    if "text" in field:
        return field["text"].encode("utf-8")
    return base64.b64decode(field["base64"], validate=True)


CONTRACT = load(ROOT / "governance/scopes/ingest-protocol.1.json")
SCHEMA = load(ROOT / "protocol/v1/schemas/messages.schema.json")
MANIFEST = load(VECTORS / "manifest.json")
MESSAGES = load(VECTORS / "messages.json")["vectors"]
REFUSALS = load(VECTORS / "refusals.json")["vectors"]
IDENTITIES = load(VECTORS / "identities.json")
DELIVERY = load(VECTORS / "delivery.json")["vectors"]
COMPATIBILITY = load(VECTORS / "compatibility.json")["vectors"]

MESSAGE_KINDS = {
    "enrollment-offer",
    "enrollment-confirmation",
    "capability-advertisement",
    "capability-acknowledgement",
    "ingest-batch",
    "snapshot-completion",
    "change-completion",
    "sync-checkpoint",
    "delivery-receipt",
    "retryable-failure",
    "delivery-block",
}
OUTBOX_FIELDS = {"wire_version", "kind", "sender_installation", "sequence", "capability_generation"}
COMPLETION_PROOF_FIELDS = {"record_type", "cycle_id", "page_count", "fragment_count", "record_count", "final_chain"}
COMPLETION_MISMATCHES = {
    "completion-identity-mismatch",
    "completion-kind-mismatch",
    "completion-count-mismatch",
    "completion-chain-mismatch",
    "completion-sequence-gap",
}
CHECKPOINT_VALUES = {
    "outcome": {
        "not-due",
        "no-change",
        "progressed",
        "completed",
        "deferred",
        "blocked",
        "not-permitted",
        "failed",
        "interrupted",
    },
    "trigger": {"background", "foreground", "manual"},
    "background_scheduling": {"available", "restricted", "unregistered"},
    "reason": {
        "collection-disabled",
        "projection-refusal",
        "record-too-large",
        "permission-missing",
        "delivery-blocked",
        "storage-pressure",
        "screen-lock-removed",
        "changes-token-expired",
        "time-budget-reached",
        "read-failed",
    },
}
FORBIDDEN_CHECKPOINT_DATA = {
    "source-record-count",
    "source-token",
    "source-identity",
    "health-date",
    "health-value",
    "free-text-failure",
}
OUTCOME_CLASSES = {"delivery-block", "retryable", "staging-refusal", "compatibility-refusal"}
SYNTHETIC_UUID_PREFIX = "00000000-0000-4000-8000-"


def embedded_values(node: object):
    """Yield every JSON value in a vector document, including messages stored as text."""
    yield node
    if isinstance(node, dict):
        children = list(node.values())
    elif isinstance(node, list):
        children = node
    elif isinstance(node, str) and node.startswith("{"):
        try:
            parsed = json.loads(node)
        except ValueError:
            return
        yield from embedded_values(parsed)
        return
    else:
        return
    for child in children:
        yield from embedded_values(child)


class IngestProtocolAcceptanceTest(unittest.TestCase):
    def select(self, vectors: list[dict], tag: str) -> list[dict]:
        selected = [vector for vector in vectors if tag in vector["tags"]]
        self.assertTrue(selected, f"no vectors tagged {tag!r}")
        return selected

    def assert_decodes(self, vector: dict) -> None:
        with self.subTest(vector=vector["name"]):
            self.assertEqual(protocol.decode_message(raw(vector["bytes"])), vector["value"])

    def assert_refused(self, vector: dict) -> None:
        with self.subTest(vector=vector["name"]):
            with self.assertRaises(protocol.ProtocolRefusal) as caught:
                protocol.decode_message(raw(vector["bytes"]))
            self.assertEqual(caught.exception.failure, vector["failure"])

    def run_scenario(self, scenario: dict) -> list[protocol.DeliveryEvaluation]:
        state = protocol.new_delivery_state(scenario["honored_generations"])
        evaluations = []
        for index, step in enumerate(scenario["steps"]):
            with self.subTest(scenario=scenario["name"], step=index):
                expected = step["expected"]
                evaluation = protocol.evaluate_delivery(state, raw(step["message"]))
                self.assertEqual(evaluation.outcome, expected["outcome"])
                self.assertEqual(evaluation.failure, expected.get("failure"))
                self.assertEqual(evaluation.response, raw(expected["response"]))
                self.assertEqual(evaluation.gap_after, expected.get("gap_after"))
                if "latest_acknowledged_generation" in expected:
                    self.assertEqual(
                        evaluation.state.latest_acknowledged_generation,
                        expected["latest_acknowledged_generation"],
                    )
                evaluations.append(evaluation)
                state = evaluation.state
        return evaluations

    def run_scenarios(self, tag: str) -> None:
        for scenario in self.select(DELIVERY, tag):
            self.run_scenario(scenario)

    def check_chains(self) -> None:
        for chain in IDENTITIES["chains"]:
            with self.subTest(chain=chain["name"]):
                value = protocol.chain_start(chain["cycle_id"])
                self.assertEqual(value, chain["start"])
                for step in chain["steps"]:
                    value = protocol.chain_next(value, step["message_sha256"])
                    self.assertEqual(value, step["chain"])

    def run_compatibility(self, tag: str) -> None:
        for vector in self.select(COMPATIBILITY, tag):
            with self.subTest(vector=vector["name"]):
                decision = protocol.decide_compatibility(
                    vector["current"], vector["proposed"], vector["latest_acknowledged_generation"]
                )
                self.assertEqual(decision, vector["expected"])

    # R1 — Governed Scope contract

    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["scope"], "ingest-protocol")
        self.assertEqual(CONTRACT["version"], "1")
        self.assertEqual(CONTRACT["kind"], "shared")
        self.assertEqual({item["mode"] for item in CONTRACT["inputs"]}, {"pure"})
        self.assertEqual(CONTRACT["observable_effects"], [])
        self.assertEqual(CONTRACT["dependencies"], [])
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(set(oracle["languages"]), {"python", "typescript"})
        self.assertTrue((ROOT / oracle["specification"]).is_file())
        self.assertTrue((ROOT / oracle["vectors"]).is_dir())

    def test_every_named_failure_has_one_outcome_class(self) -> None:
        failures = CONTRACT["named_failures"]
        outcomes = {failure["name"]: failure["outcome"] for failure in failures}
        self.assertEqual(len(outcomes), len(failures))
        self.assertLessEqual(set(outcomes.values()), OUTCOME_CLASSES)
        blocks = {name for name, outcome in outcomes.items() if outcome == "delivery-block"}
        self.assertEqual(set(SCHEMA["$defs"]["delivery_block_failure"]["enum"]), blocks)
        wire_retryable = set(SCHEMA["$defs"]["retryable_failure"]["properties"]["reason"]["enum"])
        sender_retryable = {name for name, outcome in outcomes.items() if outcome == "retryable"}
        self.assertEqual(wire_retryable | {"receipt-mismatch"}, sender_retryable)

    def test_scope_map_matches_ingest_protocol_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        row = next(line for line in scope_map.splitlines() if line.startswith("| `ingest-protocol` |"))
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        self.assertEqual(cells[1], CONTRACT["kind"])
        self.assertIn("TypeScript Sender and Python Portal", cells[2])
        self.assertIn("TypeScript Sender and Python Portal", CONTRACT["purpose"])
        self.assertEqual(cells[5], "—")

    # R2 — Closed message union

    def test_every_message_kind_has_positive_canonical_vector(self) -> None:
        self.assertEqual({vector["value"]["kind"] for vector in MESSAGES}, MESSAGE_KINDS)
        for vector in MESSAGES:
            self.assert_decodes(vector)

    def test_unknown_or_malformed_message_is_refused_without_partial_value(self) -> None:
        for vector in self.select(REFUSALS, "malformed"):
            self.assertEqual(vector["failure"], "malformed-message")
            self.assert_refused(vector)

    def test_enrollment_messages_carry_only_declared_fields(self) -> None:
        for vector in self.select(MESSAGES, "enrollment"):
            self.assert_decodes(vector)
        for vector in self.select(REFUSALS, "enrollment"):
            self.assert_refused(vector)
        for entry in IDENTITIES["fingerprints"]:
            with self.subTest(installation=entry["sender_installation"]):
                self.assertEqual(protocol.installation_fingerprint(entry["sender_installation"]), entry["fingerprint"])

    def test_ingest_batch_items_are_opaque_projections_or_retractions(self) -> None:
        for vector in self.select(MESSAGES, "ingest-items"):
            self.assertLessEqual({item["kind"] for item in vector["value"]["items"]}, {"projection", "retraction"})
            self.assert_decodes(vector)
        for vector in self.select(MESSAGES, "opaque"):
            self.assert_decodes(vector)
        for vector in self.select(REFUSALS, "ingest-items"):
            self.assert_refused(vector)

    # R3 — Exact bytes and identity

    def test_positive_vectors_encode_to_exact_bytes_and_hashes(self) -> None:
        for vector in MESSAGES:
            with self.subTest(vector=vector["name"]):
                data = raw(vector["bytes"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), vector["sha256"])
                self.assertEqual(protocol.encode_message(vector["value"]), data)
                self.assertEqual(protocol.message_sha256(data), vector["sha256"])

    def test_decode_reencode_is_byte_identical(self) -> None:
        for vector in MESSAGES:
            with self.subTest(vector=vector["name"]):
                data = raw(vector["bytes"])
                self.assertEqual(protocol.encode_message(protocol.decode_message(data)), data)

    def test_noncanonical_bytes_are_refused(self) -> None:
        for vector in self.select(REFUSALS, "noncanonical"):
            self.assertEqual(vector["failure"], "noncanonical-message")
            self.assert_refused(vector)

    def test_byte_difference_changes_message_identity(self) -> None:
        for vector in MESSAGES:
            with self.subTest(vector=vector["name"]):
                data = raw(vector["bytes"])
                spaced = data[:-1] + b" " + data[-1:]
                self.assertNotEqual(protocol.message_sha256(spaced), vector["sha256"])
                with self.assertRaises(protocol.ProtocolRefusal) as caught:
                    protocol.decode_message(spaced)
                self.assertEqual(caught.exception.failure, "noncanonical-message")

    # R4 — Independent version axes

    def test_wire_and_projection_versions_are_independent(self) -> None:
        for vector in self.select(REFUSALS, "unsupported-wire"):
            self.assertEqual(vector["failure"], "unsupported-wire-protocol")
            self.assert_refused(vector)
        self.run_scenarios("versions")

    def test_unsupported_versions_block_without_partial_success(self) -> None:
        self.run_scenarios("no-partial-success")

    # R5 — Identity, retry, and sequence behavior

    def test_receipt_echoes_exact_message_identity(self) -> None:
        for scenario in self.select(DELIVERY, "receipt-identity"):
            for step in scenario["steps"]:
                message = json.loads(raw(step["message"]))
                receipt = json.loads(raw(step["expected"]["response"]))
                self.assertEqual(receipt["sender_installation"], message["sender_installation"])
                self.assertEqual(receipt["sequence"], message["sequence"])
                self.assertEqual(receipt["message_sha256"], hashlib.sha256(raw(step["message"])).hexdigest())
            self.run_scenario(scenario)

    def test_identical_retry_returns_same_receipt(self) -> None:
        for scenario in self.select(DELIVERY, "identical-retry"):
            first, retry = self.run_scenario(scenario)
            self.assertEqual(first.response, retry.response)

    def test_same_sequence_different_bytes_blocks(self) -> None:
        self.run_scenarios("sequence-content")

    def test_gap_is_reported_without_changing_receipt(self) -> None:
        self.run_scenarios("gap")

    def test_older_unseen_sequence_blocks(self) -> None:
        self.run_scenarios("out-of-order")

    # R6 — Deterministic fragmentation (Portal side)

    def test_fragments_are_contiguous_and_chain_ordered(self) -> None:
        self.run_scenarios("fragment-order")

    def test_chain_starts_from_cycle_identity(self) -> None:
        self.check_chains()

    # R7 — Receipt-bound completion

    def test_snapshot_kinds_share_one_closed_shape(self) -> None:
        snapshots = [vector for vector in MESSAGES if vector["value"]["kind"] == "snapshot-completion"]
        self.assertEqual({vector["value"]["snapshot_kind"] for vector in snapshots}, {"initial", "reconciliation"})
        self.assertEqual(len({frozenset(vector["value"]) for vector in snapshots}), 1)
        for vector in snapshots:
            self.assert_decodes(vector)

    def test_completion_contains_only_allowed_proof_fields(self) -> None:
        for vector in self.select(MESSAGES, "completion"):
            value = vector["value"]
            allowed = OUTBOX_FIELDS | COMPLETION_PROOF_FIELDS
            if value["kind"] == "snapshot-completion":
                allowed = allowed | {"snapshot_kind"}
            self.assertEqual(set(value), allowed)
            self.assert_decodes(vector)
        for vector in self.select(REFUSALS, "completion-fields"):
            self.assert_refused(vector)

    def test_empty_cycle_completion_is_valid(self) -> None:
        for vector in self.select(MESSAGES, "empty-cycle"):
            self.assert_decodes(vector)
        self.run_scenarios("empty-cycle")

    def test_completion_matches_complete_fragment_chain(self) -> None:
        self.run_scenarios("completion-accepted")

    def test_each_completion_mismatch_blocks_with_its_name(self) -> None:
        scenarios = self.select(DELIVERY, "completion-mismatch")
        named = {step["expected"].get("failure") for scenario in scenarios for step in scenario["steps"]}
        self.assertLessEqual(COMPLETION_MISMATCHES, named)
        for scenario in scenarios:
            self.run_scenario(scenario)

    def test_fragment_after_completion_blocks(self) -> None:
        self.run_scenarios("fragment-after-completion")

    # R8 — Health-data-free polling checkpoint

    def test_checkpoint_accepts_every_allowlisted_value(self) -> None:
        checkpoints = self.select(MESSAGES, "checkpoint")
        seen = {field: set() for field in CHECKPOINT_VALUES}
        for vector in checkpoints:
            value = vector["value"]
            seen["trigger"].add(value["trigger"])
            seen["background_scheduling"].add(value["background_scheduling"])
            for status in value["record_types"]:
                seen["outcome"].add(status["outcome"])
                if "reason" in status:
                    seen["reason"].add(status["reason"])
            self.assert_decodes(vector)
        self.assertEqual(seen, CHECKPOINT_VALUES)
        for vector in self.select(REFUSALS, "checkpoint-values"):
            self.assert_refused(vector)

    def test_checkpoint_refuses_each_forbidden_data_category_as_malformed(self) -> None:
        vectors = self.select(REFUSALS, "checkpoint-privacy")
        self.assertEqual({vector["category"] for vector in vectors}, FORBIDDEN_CHECKPOINT_DATA)
        for vector in vectors:
            self.assertEqual(vector["failure"], "malformed-message")
            self.assert_refused(vector)

    def test_checkpoint_counts_are_explicitly_preinsertion(self) -> None:
        for vector in self.select(MESSAGES, "checkpoint"):
            self.assertIn("outbox_messages_before_checkpoint", vector["value"])
            self.assertIn("outbox_bytes_before_checkpoint", vector["value"])
            self.assert_decodes(vector)
        for vector in self.select(REFUSALS, "checkpoint-counts"):
            self.assert_refused(vector)

    # R9 — Capability generations and offline staging

    def test_acknowledgement_is_an_ordered_outbox_message(self) -> None:
        for vector in self.select(MESSAGES, "acknowledgement"):
            self.assertEqual(set(vector["value"]), OUTBOX_FIELDS)
            self.assert_decodes(vector)
        for vector in self.select(REFUSALS, "acknowledgement"):
            self.assert_refused(vector)
        self.run_scenarios("acknowledgement")

    def test_message_is_evaluated_under_its_staging_generation(self) -> None:
        self.run_scenarios("staging-generation")

    def test_new_limits_do_not_reinterpret_queued_messages(self) -> None:
        self.run_scenarios("queued-limits")

    # R10 — Expand–migrate–contract safety

    def test_protocol_expansion_needs_no_coordination(self) -> None:
        self.run_compatibility("expansion")

    def test_capability_generation_cannot_regress(self) -> None:
        self.run_compatibility("regression")

    def test_retiring_a_generation_requires_a_later_acknowledgement(self) -> None:
        self.run_compatibility("retirement")

    # R11 — Outcome trichotomy

    def test_delivery_outcome_is_closed_and_exclusive(self) -> None:
        response_kinds = {"receipt": "delivery-receipt", "block": "delivery-block"}
        for scenario in DELIVERY:
            for evaluation in self.run_scenario(scenario):
                with self.subTest(scenario=scenario["name"]):
                    self.assertIn(evaluation.outcome, response_kinds)
                    self.assertEqual(evaluation.failure is None, evaluation.outcome == "receipt")
                    response = protocol.decode_message(evaluation.response)
                    self.assertEqual(response["kind"], response_kinds[evaluation.outcome])

    def test_retryable_failure_reasons_are_closed(self) -> None:
        vectors = self.select(MESSAGES, "retryable")
        self.assertEqual(
            {vector["value"]["reason"] for vector in vectors},
            {"storage-reserve-exhausted", "temporarily-unavailable"},
        )
        for vector in vectors:
            self.assert_decodes(vector)
        for vector in self.select(REFUSALS, "retryable"):
            self.assert_refused(vector)

    # R12 — Cross-language conformance and privacy

    def test_vector_inventory_is_complete_and_deterministic(self) -> None:
        listed = {entry["path"]: entry for entry in MANIFEST["files"]}
        present = {path.name for path in VECTORS.glob("*.json")} - {"manifest.json"}
        self.assertEqual(set(listed), present)
        for path, entry in listed.items():
            self.assertEqual(hashlib.sha256((VECTORS / path).read_bytes()).hexdigest(), entry["sha256"], path)
            self.assertTrue(entry["languages"])
            self.assertLessEqual(set(entry["languages"]), {"python", "typescript"})
            document = load(VECTORS / path)
            names = [vector["name"] for vector in document.get("vectors", [])]
            self.assertEqual(len(names), len(set(names)), path)

        exercised = {vector["failure"] for vector in REFUSALS}
        exercised |= {step["expected"].get("failure") for scenario in DELIVERY for step in scenario["steps"]}
        exercised |= {vector["expected"] for vector in COMPATIBILITY}
        for vector in load(VECTORS / "responses.json")["vectors"]:
            exercised.add(vector["expected"].get("failure"))
        for vector in load(VECTORS / "partition.json")["vectors"]:
            exercised.add(vector["expected"].get("refusal"))
        self.assertLessEqual({failure["name"] for failure in CONTRACT["named_failures"]}, exercised)

        generator = ROOT / MANIFEST["generator"]
        result = subprocess.run([sys.executable, str(generator), "--check"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_python_conforms_to_shared_vectors(self) -> None:
        for vector in MESSAGES:
            with self.subTest(vector=vector["name"]):
                data = raw(vector["bytes"])
                self.assertEqual(protocol.decode_message(data), vector["value"])
                self.assertEqual(protocol.encode_message(vector["value"]), data)
                self.assertEqual(protocol.message_sha256(data), vector["sha256"])
        for vector in REFUSALS:
            self.assert_refused(vector)
        for entry in IDENTITIES["fingerprints"]:
            self.assertEqual(protocol.installation_fingerprint(entry["sender_installation"]), entry["fingerprint"])
        self.check_chains()
        for scenario in DELIVERY:
            self.run_scenario(scenario)
        for vector in COMPATIBILITY:
            with self.subTest(vector=vector["name"]):
                decision = protocol.decide_compatibility(
                    vector["current"], vector["proposed"], vector["latest_acknowledged_generation"]
                )
                self.assertEqual(decision, vector["expected"])

    def test_positive_vectors_match_json_schema(self) -> None:
        Draft202012Validator.check_schema(SCHEMA)
        validator = Draft202012Validator(SCHEMA)
        for vector in MESSAGES:
            with self.subTest(vector=vector["name"]):
                self.assertTrue(validator.is_valid(vector["value"]))
        for vector in REFUSALS:
            tags = set(vector["tags"])
            if not tags & {"schema-invalid", "rule-invalid", "noncanonical"}:
                continue
            with self.subTest(vector=vector["name"]):
                valid = validator.is_valid(json.loads(raw(vector["bytes"])))
                self.assertEqual(valid, "schema-invalid" not in tags)

    def test_fixtures_are_synthetic_and_secret_free(self) -> None:
        forbidden = [
            re.compile(r"\.ts\.net\b"),
            re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
            re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
            re.compile(r"PRIVATE KEY"),
        ]
        uuid = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
        for path in sorted(VECTORS.glob("*.json")):
            text = path.read_text(encoding="utf-8")
            for pattern in forbidden:
                self.assertIsNone(pattern.search(text), f"{path.name}: {pattern.pattern}")
            for match in uuid.findall(text):
                self.assertTrue(match.startswith(SYNTHETIC_UUID_PREFIX), f"{path.name}: {match}")
            if path.name == "refusals.json":
                continue
            for value in embedded_values(load(path)):
                if not isinstance(value, dict):
                    continue
                if value.get("kind") == "projection" and "data" in value:
                    data = base64.b64decode(value["data"], validate=True)
                    self.assertTrue(data.startswith(b'{"synthetic":'), path.name)
                if "source_identity" in value:
                    self.assertTrue(value["source_identity"].startswith("synthetic-"), path.name)
                if "portal_url" in value:
                    self.assertRegex(value["portal_url"], r"^https?://[^/:]+\.invalid(:\d+)?$")


if __name__ == "__main__":
    unittest.main()
