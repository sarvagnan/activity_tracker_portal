"""Acceptance tests for `project-source-record@1` on the shared schemas and vectors.

Requirement numbers refer to `packs/project-source-record-1.spec.md`. These
tests check the language-neutral contract under `projection/v1/`; the Kotlin
suite checks that the Sender's projection produces the vectors' exact bytes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
PROJECTION = ROOT / "projection/v1"
CONTRACT = json.loads((ROOT / "governance/scopes/project-source-record.1.json").read_text(encoding="utf-8"))


def load(relative: str) -> dict:
    return json.loads((PROJECTION / relative).read_text(encoding="utf-8"))


def generator():
    spec = importlib.util.spec_from_file_location("build_projection", PROJECTION / "tools/build_projection.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODEL = load("model.json")
POSITIVE = load("vectors/positive.json")["vectors"]
NEGATIVE = load("vectors/schema-negatives.json")["vectors"]
REFUSALS = load("vectors/refusals.json")["vectors"]


def schema(record_type: str) -> dict:
    return load(f"schemas/{record_type}.schema.json")


class R1GovernedScopeContract(unittest.TestCase):
    def test_governed_scope_contract_has_required_shape(self) -> None:
        self.assertEqual(CONTRACT["scope"], "project-source-record")
        self.assertEqual(CONTRACT["version"], "1")
        self.assertEqual(CONTRACT["kind"], "shared")
        self.assertEqual(CONTRACT["dependencies"], [])
        self.assertEqual(CONTRACT["observable_effects"], [])
        self.assertEqual({item["mode"] for item in CONTRACT["inputs"]}, {"pure"})
        self.assertEqual({failure["outcome"] for failure in CONTRACT["named_failures"]}, {"projection-refusal"})
        names = [failure["name"] for failure in CONTRACT["named_failures"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual({refusal["failure"] for refusal in REFUSALS}, set(names))
        oracle = CONTRACT["acceptance_oracle"]
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        scope_map = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
        rows = [line for line in scope_map.splitlines() if line.startswith("| `project-source-record` |")]
        self.assertEqual(len(rows), 1)
        cells = [cell.strip() for cell in rows[0].strip("|").split("|")]
        self.assertEqual(cells[1], CONTRACT["kind"])
        self.assertEqual(cells[-1], "—")
        self.assertIn("projection refusal", cells[4])


class R11SharedSchemasAndVectors(unittest.TestCase):
    def test_every_record_type_has_one_closed_schema(self) -> None:
        files = sorted(path.name.removesuffix(".schema.json") for path in (PROJECTION / "schemas").glob("*.schema.json"))
        self.assertEqual(files, sorted(MODEL["records"]))
        for record_type in MODEL["records"]:
            document = schema(record_type)
            Draft202012Validator.check_schema(document)
            self.assertEqual(document["properties"]["record_type"], {"const": record_type})

    def test_schemas_are_closed(self) -> None:
        def walk(node, where):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False, where)
                for key, value in node.items():
                    walk(value, f"{where}/{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{where}/{index}")

        for record_type in MODEL["records"]:
            walk(schema(record_type), record_type)

    def test_positive_vectors_conform_to_their_schemas(self) -> None:
        covered = set()
        for vector in POSITIVE:
            errors = sorted(Draft202012Validator(schema(vector["record_type"])).iter_errors(json.loads(vector["bytes"])), key=str)
            self.assertEqual(errors, [], vector["name"])
            covered.add(vector["record_type"])
        self.assertEqual(covered, set(MODEL["records"]))

    def test_schema_negatives_are_rejected(self) -> None:
        self.assertTrue(NEGATIVE)
        for vector in NEGATIVE:
            self.assertFalse(Draft202012Validator(schema(vector["record_type"])).is_valid(vector["projection"]), vector["name"])

    def test_vectors_are_canonical(self) -> None:
        canonical = generator().canonical_bytes
        for vector in POSITIVE:
            data = vector["bytes"].encode("utf-8")
            self.assertEqual(json.loads(data), vector["projection"], vector["name"])
            self.assertEqual(canonical(json.loads(data)), data, vector["name"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), vector["sha256"], vector["name"])
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"), vector["name"])

    def test_vector_manifest_matches_files(self) -> None:
        manifest = load("manifest.json")
        for relative, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((PROJECTION / relative).read_bytes()).hexdigest(), digest, relative)
        expected = {name for name in generator().outputs() if name != "manifest.json"}
        self.assertEqual(set(manifest["files"]), expected)
        for name, text in generator().outputs().items():
            self.assertEqual((PROJECTION / name).read_text(encoding="utf-8"), text, f"{name} is stale; rerun the generator")

    def test_refusal_vectors_cover_every_named_failure(self) -> None:
        names = {vector["name"] for vector in POSITIVE}
        for refusal in REFUSALS:
            self.assertIn(refusal["base"], names, refusal["name"])
        self.assertEqual({refusal["failure"] for refusal in REFUSALS}, {failure["name"] for failure in CONTRACT["named_failures"]})


if __name__ == "__main__":
    unittest.main()
