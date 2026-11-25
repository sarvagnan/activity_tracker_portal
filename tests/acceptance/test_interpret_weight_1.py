"""Acceptance tests for `interpret-weight@1`.

Requirement numbers refer to `packs/interpret-weight-1.spec.md`. Inputs are the synthetic Weight
vectors in `projection/v1` and hand-built synthetic Weight projections whose mass is written as an
exact JSON number token. Every outcome test runs end to end through the published
`interpret_source_projection.interpret` with this unit's `PROCESSOR`; tests call
`PROCESSOR.interpret_body` directly only to observe the Python type of the body value, totality,
purity, and what a processor Quarantine carries before the common layer rewraps it.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import unittest
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import tzdata
from jsonschema import Draft202012Validator

from activity_tracker_portal.interpret_source_projection import (
    BodySchema,
    CanonicalRevision,
    Endpoint,
    FallbackTimezonePolicy,
    Instant,
    InterpretFailure,
    NumericField,
    PolicyPending,
    ProcessingInput,
    Processor,
    Quarantine,
    TemporalHeader,
    interpret,
)
from activity_tracker_portal.interpret_weight import PROCESSOR, WEIGHT_MAX_GRAMS, WEIGHT_MIN_GRAMS

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PROJECTION = ROOT / "projection/v1"
CONTRACT = json.loads((ROOT / "governance/scopes/interpret-weight.1.json").read_text(encoding="utf-8"))
EFFECTS = json.loads((ROOT / "packs/interpret-weight-1.effects.json").read_text(encoding="utf-8"))
SCOPE_MAP = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
POSITIVE = [vector for vector in json.loads((PROJECTION / "vectors/positive.json").read_text(encoding="utf-8"))["vectors"]
            if vector["record_type"] == "weight"]
SCHEMA = Draft202012Validator(json.loads((PROJECTION / "schemas/weight.schema.json").read_text(encoding="utf-8")))

UTC = timezone.utc
HC = "health-connect"
ORIGIN = "org.example.synthetic.source"
SDK = "androidx.health.connect:connect-client:1.1.0"
HEALTH_MARKER = "synthetic-health-marker"
IDENTITY = "synthetic-identity-marker"
SOURCE_IDENTITY = IDENTITY + "-00000000-0000-4000-8000-000000000001"
LMT = "2026-01-15T09:00:00.123456789Z"
TIME = "2026-01-15T07:30:00.250Z"
VERSION = "interpret-weight@1"
REASON = "weight-out-of-range"
POINTER = "/record/weight_grams"
MASS_SLOT = '"@@mass@@"'
KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
COMMON_REASONS = {
    "projection-not-json",
    "projection-envelope-invalid",
    "projection-schema-invalid",
    "instant-out-of-range",
    "interval-reversed",
    "record-type-not-modeled",
    "schema-version-not-modeled",
    "fallback-timezone-policy-missing",
}
INSTANT_TEXT = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d{3}|\d{6}|\d{9}))?Z$")

# Mass tokens inside the pinned SDK bounds, as the projection may write them.
WHOLE_TOKENS = ("0", "1", "999999", "1000000", "72500", "123457", "9")
FRACTIONAL_TOKENS = ("72500.5", "0.1", "0.5", "72500.12345678902", "5e-324", "2.2250738585072014e-308",
                     "1e-7", "999999.9999999999", "62999.99999999999", "81234.567", "1.5", "333333.3333333333")
# Mass tokens outside them. Each is a distinctive fault number that no outcome may carry.
OUTSIDE_TOKENS = ("-1", "-0.5", "-5e-324", "-72500.5", "1000000.0000000001", "1000000.5", "1000001",
                  "1000000.0001", "2000000", "9007199254740993", "-9007199254740993", "18446744073709551623",
                  "1e300", "-1e300", "1.7976931348623157e308", "-1.7976931348623157e308",
                  "1" + "0" * 400, "-" + "7" * 400, str(2**1024), str(-(2**1024)))


# ---- Independent oracles ----

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def epoch_ns(text: str) -> int:
    year, month, day, hour, minute, second, fraction = INSTANT_TEXT.match(text).groups()
    whole = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), tzinfo=UTC)
    seconds = (whole - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(seconds=1)
    return seconds * 10**9 + int((fraction or "").ljust(9, "0"))


def truncated(text: str) -> datetime:
    year, month, day, hour, minute, second, fraction = INSTANT_TEXT.match(text).groups()
    return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second),
                    int((fraction or "").ljust(9, "0")[:6]), tzinfo=UTC)


def local_date(text: str, offset: str) -> date:
    sign = -1 if offset[0] == "-" else 1
    delta = sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6]))
    return (truncated(text) + delta).date()


def es_number(value) -> str:
    """ECMAScript Number::toString (RFC 8785) of a binary64 value."""
    value = float(value)
    if value == 0:
        return "0"
    if value < 0:
        return "-" + es_number(-value)
    _, digit_tuple, exponent = Decimal(repr(value)).as_tuple()
    digits = "".join(map(str, digit_tuple))
    stripped = digits.rstrip("0") or "0"
    exponent += len(digits) - len(stripped)
    k = len(stripped)
    n = k + exponent
    if k <= n <= 21:
        return stripped + "0" * (n - k)
    if 0 < n <= 21:
        return stripped[:n] + "." + stripped[n:]
    if -6 < n <= 0:
        return "0." + "0" * (-n) + stripped
    mantissa = stripped if k == 1 else stripped[0] + "." + stripped[1:]
    return f"{mantissa}e{'+' if n - 1 >= 0 else '-'}{abs(n - 1)}"


def decoded(token: str):
    """The Python value the common layer's JSON decoder gives a mass token."""
    return json.loads(token)


def inside(value) -> bool:
    """The pinned SDK 1.1.0 WeightRecord bounds, 0 g and 1,000,000 g inclusive, compared exactly.

    The SDK compares masses with Kotlin's Double.compareTo, which orders -0.0 below 0.0, so a
    negative zero is below the lower bound.
    """
    if value == 0 and isinstance(value, float) and math.copysign(1.0, value) < 0:
        return False
    return 0 <= value <= 1_000_000


def expected_body(token: str) -> bytes:
    """The RFC 8785 body: one key, the float of the decoded mass, never rounded or converted."""
    return b'{"weight_grams":' + es_number(float(decoded(token))).encode("ascii") + b"}"


def fallback_identity(*policy_ids: str) -> str:
    return "fallback-timezone:" + json.dumps([sorted(policy_ids), tzdata.IANA_VERSION], ensure_ascii=False,
                                             separators=(",", ":"))


def json_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from json_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from json_keys(item)


# ---- Synthetic projections and inputs ----

def projection(*, time: str = TIME, offset: str | None = "+05:30", device: bool = True, recording_method: int = 2,
               lmt: str = LMT, identity: str = SOURCE_IDENTITY, provider: str = "synthetic-provider-1",
               extra: dict | None = None) -> dict:
    """A synthetic Weight projection whose mass is the placeholder `MASS_SLOT`."""
    metadata = {
        "id": identity,
        "data_origin": {"package_name": ORIGIN},
        "last_modified_time": lmt,
        "client_record_id": "synthetic-client-record-id",
        "client_record_version": "7",
        "recording_method": recording_method,
    }
    if device:
        metadata["device"] = {"type": 1, "manufacturer": "Synthetic Devices", "model": "SD-1"}
    record = {"time": time}
    if offset is not None:
        record["zone_offset"] = offset
    record["weight_grams"] = MASS_SLOT[1:-1]
    record.update(extra or {})
    return {
        "record_type": "weight",
        "projection_schema_version": 1,
        "projection_implementation_version": "0.1.0-synthetic",
        "sdk_version": SDK,
        "provider_version": provider,
        "metadata": metadata,
        "record": record,
    }


def weight(token: str, **kw) -> bytes:
    """Projection bytes with the mass written as exactly `token`, as an RFC 8785 writer or a forged Sender would."""
    text = json.dumps(projection(**kw), ensure_ascii=False, separators=(",", ":"))
    assert text.count(MASS_SLOT) == 1
    return text.replace(MASS_SLOT, token).encode("utf-8")


def make_input(data: bytes, **overrides) -> ProcessingInput:
    parsed = json.loads(data)
    metadata = parsed["metadata"]
    fields = {
        "source_system_kind": HC,
        "origin": metadata["data_origin"]["package_name"],
        "record_type": parsed["record_type"],
        "source_identity": metadata["id"],
        "record_id": 41,
        "selection_id": 42,
        "evidence_id": 43,
        "projection_sha256": sha(data),
        "last_modified_time": truncated(metadata["last_modified_time"]),
        "projection_schema_version": parsed["projection_schema_version"],
        "projection": data,
    }
    fields.update(overrides)
    return ProcessingInput(**fields)


def policy(policy_id: str, effective_from: datetime | None, zone: str) -> FallbackTimezonePolicy:
    return FallbackTimezonePolicy(policy_id, effective_from, zone)


UTC_ALWAYS = (policy("policy-utc", None, "UTC"),)


def run(data: bytes, policies=(), **overrides):
    return interpret(make_input(data, **overrides), PROCESSOR, list(policies))


def record_of(token: str, *, time: str = TIME, offset: str | None = "+05:30") -> dict:
    """The record object the common layer passes, decoded exactly as it decodes the projection."""
    return json.loads(weight(token, time=time, offset=offset))["record"]


def temporal_for(record: dict) -> TemporalHeader:
    """The instant header the common layer would build: experienced from the offset, else a UTC fallback."""
    text = record["time"]
    offset = record.get("zone_offset")
    applied = offset if offset is not None else "+00:00"
    endpoint = Endpoint(Instant(text, epoch_ns(text)), offset, applied, local_date(text, applied),
                        "experienced" if offset is not None else "inferred", None if offset is not None else "policy-utc")
    return TemporalHeader("instant", endpoint, None, True)


def scenario_summary() -> str:
    """A deterministic summary of representative outcomes, compared inside an observed subprocess."""
    out = []
    for data, policies in (
        (weight("72500.5"), ()),
        (weight("72500", offset=None), UTC_ALWAYS),
        (weight("0"), ()),
        (weight("1000000"), ()),
        (weight("1000001"), ()),
        (weight("1" + "0" * 400, offset=None), UTC_ALWAYS),
        (weight("-0.5"), ()),
    ):
        outcome = run(data, policies)
        if isinstance(outcome, CanonicalRevision):
            out.append([outcome.body_bytes.decode(), outcome.revision_sha256])
        else:
            out.append(repr(outcome))
    return json.dumps(out)


def run_python(code: str, env_updates: dict[str, str]) -> str:
    env = dict(os.environ)
    env.update(env_updates)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(HERE), env.get("PYTHONPATH", "")]))
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=300)
    if done.returncode != 0:
        raise AssertionError(f"subprocess failed ({done.returncode}):\n{done.stderr[-3000:]}")
    return done.stdout.strip().splitlines()[-1]


class Checks(unittest.TestCase):
    def outcome(self, data: bytes, policies=(), **overrides):
        """The outcome of `interpret`; a named failure fails the test with its name."""
        try:
            return run(data, policies, **overrides)
        except InterpretFailure as failure:
            self.fail(f"interpret refused with {failure.failure}")

    def revision(self, data: bytes, policies=(), **overrides) -> CanonicalRevision:
        outcome = self.outcome(data, policies, **overrides)
        self.assertIsInstance(outcome, CanonicalRevision, repr(outcome))
        self.assertEqual(outcome.processor_version, VERSION)
        self.assertEqual(outcome.header.processor_version, VERSION)
        self.assertEqual(outcome.header.body_schema, PROCESSOR.body_schema)
        self.assertEqual(outcome.body_sha256, sha(outcome.body_bytes))
        self.assertEqual(outcome.header.body_sha256, outcome.body_sha256)
        header = json.loads(outcome.header_bytes)
        self.assertEqual((header["record_type"], header["processor_version"], header["body_schema"]),
                         ("weight", VERSION, {"record_type": "weight", "body_version": 1}))
        return outcome

    def assert_body(self, token: str, data: bytes | None = None, policies=()) -> CanonicalRevision:
        outcome = self.revision(data if data is not None else weight(token), policies)
        self.assertEqual(outcome.body_bytes, expected_body(token), token)
        value = json.loads(outcome.body_bytes)["weight_grams"]
        self.assertEqual(value, decoded(token), token)
        return outcome

    def assert_quarantined(self, token: str, data: bytes | None = None, policies=(), identity: str = "none") -> None:
        outcome = self.outcome(data if data is not None else weight(token), policies)
        self.assertEqual(outcome, Quarantine(REASON, POINTER, VERSION, identity), token)

    def body(self, record: dict):
        """Call `interpret_body` directly; raising fails the test."""
        try:
            return PROCESSOR.interpret_body(record, temporal_for(record))
        except Exception as error:  # noqa: BLE001 - R7 totality is the assertion
            self.fail(f"interpret_body raised {type(error).__name__}")

    def assert_float_body(self, token: str, **kw) -> None:
        result = self.body(record_of(token, **kw))
        self.assertIsInstance(result, Mapping, token)
        self.assertNotIsInstance(result, Quarantine, token)
        self.assertEqual(set(result), {"weight_grams"}, token)
        value = result["weight_grams"]
        self.assertIs(type(value), float, token)
        self.assertTrue(math.isfinite(value), token)
        self.assertEqual(value.hex(), float(decoded(token)).hex(), token)
        self.assertEqual(value, decoded(token), token)


# ---- R1 ----

class R1GovernedScopeContract(unittest.TestCase):
    def test_contract_shape_and_dependency(self) -> None:
        self.assertEqual((CONTRACT["contract_schema"], CONTRACT["scope"], CONTRACT["version"], CONTRACT["kind"]),
                         (1, "interpret-weight", "1", "leaf"))
        self.assertEqual(CONTRACT["dependencies"], [{"scope": "interpret-source-projection", "version": "1"}])
        self.assertEqual(CONTRACT["named_failures"], [])
        self.assertEqual(CONTRACT["observable_effects"], [])
        self.assertEqual({item["mode"] for item in CONTRACT["inputs"]}, {"pure"})
        outputs = {item["name"]: item["description"] for item in CONTRACT["outputs"]}
        self.assertEqual(set(re.findall(r"\bweight-[a-z0-9-]*[a-z0-9]\b", outputs["quarantine_outcome"])), {REASON})
        oracle = CONTRACT["acceptance_oracle"]
        self.assertNotIn("freeze_state", oracle)
        self.assertEqual(oracle["specification"], "packs/interpret-weight-1.spec.md")
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_interpret_weight_1.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_interpret_weight_1.sh")
        self.assertEqual(oracle["languages"], ["python"])
        runner = ROOT / oracle["runner"]
        self.assertTrue(os.access(runner, os.X_OK), oracle["runner"])
        self.assertIn("-p 'test_interpret_weight_1.py'", runner.read_text(encoding="utf-8"))

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        rows = [line for line in SCOPE_MAP.splitlines() if line.startswith("| `interpret-weight` |")]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIn("| leaf |", row)
        self.assertIn("Weight", row)
        self.assertIn("Canonical Revision", row)
        self.assertTrue(row.rstrip().endswith("| `interpret-source-projection` |"), row)

    def test_effects_declaration_is_empty(self) -> None:
        self.assertEqual(EFFECTS, {"effects_format": 1, "effects": []})


# ---- R2 ----

class R2ProcessorIdentity(unittest.TestCase):
    def test_processor_identity(self) -> None:
        self.assertIsInstance(PROCESSOR, Processor)
        self.assertEqual(PROCESSOR.record_type, "weight")
        self.assertEqual(PROCESSOR.processor_version, VERSION)
        self.assertEqual(PROCESSOR.processor_version, f"{CONTRACT['scope']}@{CONTRACT['version']}")
        self.assertIsInstance(PROCESSOR.supported_schema_versions, frozenset)
        self.assertEqual(PROCESSOR.supported_schema_versions, frozenset({1}))
        self.assertEqual(PROCESSOR.body_schema, BodySchema("weight", 1, (NumericField("/weight_grams", "float", "g"),)))
        self.assertIsInstance(PROCESSOR.body_schema.numeric_fields, tuple)
        self.assertIsInstance(PROCESSOR.quarantine_reasons, frozenset)
        self.assertEqual(PROCESSOR.quarantine_reasons, frozenset({REASON}))
        self.assertTrue(callable(PROCESSOR.interpret_body))
        for name, value in (("record_type", "steps"), ("processor_version", "interpret-weight@2"),
                            ("supported_schema_versions", frozenset({1, 2})), ("quarantine_reasons", frozenset())):
            with self.subTest(name):
                with self.assertRaises(AttributeError):
                    setattr(PROCESSOR, name, value)
                self.assertNotEqual(getattr(PROCESSOR, name), value)

    def test_declared_reasons_are_exact_and_not_common(self) -> None:
        self.assertEqual(set(PROCESSOR.quarantine_reasons), {REASON})
        for reason in PROCESSOR.quarantine_reasons:
            self.assertRegex(reason, KEBAB)
            self.assertTrue(reason.startswith("weight-"), reason)
            self.assertNotIn(reason, COMMON_REASONS)
        outputs = {item["name"]: item["description"] for item in CONTRACT["outputs"]}
        self.assertIn(REASON, outputs["quarantine_outcome"])


# ---- R3 ----

class R3Body(Checks):
    def test_positive_vectors_become_revisions(self) -> None:
        self.assertTrue(POSITIVE)
        shapes = set()
        for vector in POSITIVE:
            with self.subTest(vector["name"]):
                data = vector["bytes"].encode("utf-8")
                self.assertEqual(sha(data), vector["sha256"])
                record = vector["projection"]["record"]
                offset = record.get("zone_offset")
                shapes.add(offset is None)
                outcome = self.revision(data, () if offset is not None else UTC_ALWAYS)
                token = json.dumps(record["weight_grams"])
                self.assertEqual(outcome.body_bytes, expected_body(token))
                self.assertEqual(outcome.policy_identity, "none" if offset is not None else fallback_identity("policy-utc"))
                header = json.loads(outcome.header_bytes)
                self.assertEqual(header["time"]["shape"], "instant")
                self.assertEqual(header["time"]["start"]["instant"], record["time"])
                self.assertEqual(header["source_identity"], vector["projection"]["metadata"]["id"])
        self.assertEqual(shapes, {True, False}, "vectors with and without an offset")
        minimal = next(vector for vector in POSITIVE if vector["name"] == "weight-minimal")
        self.assertEqual(self.revision(minimal["bytes"].encode("utf-8"), UTC_ALWAYS).body_bytes, b'{"weight_grams":72500.5}')

    def test_body_is_exactly_the_mass(self) -> None:
        pinned = {
            "72500.5": b'{"weight_grams":72500.5}',
            "72500": b'{"weight_grams":72500}',
            "0": b'{"weight_grams":0}',
            "1000000": b'{"weight_grams":1000000}',
            "72500.12345678902": b'{"weight_grams":72500.12345678902}',
            "5e-324": b'{"weight_grams":5e-324}',
            "1e-7": b'{"weight_grams":1e-7}',
            "999999.9999999999": b'{"weight_grams":999999.9999999999}',
        }
        for token, body in pinned.items():
            with self.subTest(token):
                self.assertEqual(expected_body(token), body)
                self.assertEqual(self.assert_body(token).body_bytes, body)
        for token in WHOLE_TOKENS + FRACTIONAL_TOKENS:
            with self.subTest(token=token):
                self.assert_body(token)
                self.assert_float_body(token)

    def test_body_has_no_header_fields(self) -> None:
        maximal = next(vector for vector in POSITIVE if vector["name"] == "weight-maximal")
        cases = [(maximal["bytes"].encode("utf-8"), ()), (weight("72500.5", offset=None), UTC_ALWAYS),
                 (weight("81234", offset="-08:00"), ())]
        for data, policies in cases:
            with self.subTest(sha(data)):
                outcome = self.revision(data, policies)
                body = json.loads(outcome.body_bytes)
                self.assertEqual(set(body), {"weight_grams"})
                header = json.loads(outcome.header_bytes)
                record = json.loads(data)["record"]
                self.assertFalse(set(body) & (set(json_keys(header)) | {"time", "zone_offset", "metadata", "device"}))
                text = outcome.body_bytes.decode("utf-8")
                for value in (record["time"], record["time"][:10], header["time"]["start"]["applied_offset"],
                              header["time"]["start"]["local_date"], header["source_identity"], header["origin"],
                              header["last_modified_time"], "experienced", "inferred", "policy", "null"):
                    self.assertNotIn(value, text)


# ---- R4 ----

class R4WholeNumberMasses(Checks):
    def test_whole_number_mass_becomes_an_equal_float(self) -> None:
        for token in WHOLE_TOKENS:
            with self.subTest(token):
                self.assertIs(type(decoded(token)), int)
                self.assert_float_body(token)
                self.assert_float_body(token, offset=None)
                outcome = self.assert_body(token)
                self.assertEqual(outcome.body_bytes, b'{"weight_grams":' + token.encode() + b"}")
        # A float token with no fraction gives the same body as the integer token RFC 8785 writes.
        for token, equal in (("72500.0", "72500"), ("1e6", "1000000"), ("1E+6", "1000000"), ("0.0", "0"),
                             ("999999.0", "999999")):
            with self.subTest(token):
                self.assertIs(type(decoded(token)), float)
                self.assertEqual(self.assert_body(token).body_bytes, self.assert_body(equal).body_bytes)
                self.assert_float_body(token)
        # Every integer within the bounds is exactly representable, so nothing rounds.
        for value in (1, 2, 3, 99999, 500000, 999998, 999999, 1000000):
            with self.subTest(value=value):
                self.assert_float_body(str(value))
                self.assertEqual(int(float(value)), value)

    def test_fractional_mass_is_unchanged(self) -> None:
        for token in FRACTIONAL_TOKENS:
            with self.subTest(token):
                self.assertIs(type(decoded(token)), float)
                self.assertEqual(es_number(decoded(token)), token, "the token is already canonical")
                self.assert_float_body(token)
                outcome = self.assert_body(token)
                self.assertEqual(json.loads(outcome.body_bytes)["weight_grams"].hex(), decoded(token).hex())
        # Neighbouring doubles stay distinct: no rounding to any decimal place.
        first = self.assert_body("72500.12345678902").body_bytes
        second = self.assert_body(repr(math.nextafter(72500.12345678902, math.inf))).body_bytes
        self.assertNotEqual(first, second)

    def test_huge_integer_mass_is_quarantined_not_raised(self) -> None:
        for token in ("9007199254740993", "-9007199254740993", "18446744073709551623", str(2**1024),
                      str(-(2**1024)), "1" + "0" * 400, "-" + "7" * 400, "1" + "0" * 308 + "1"):
            with self.subTest(token[:24]):
                self.assertIs(type(decoded(token)), int)
                self.assert_quarantined(token)
                self.assert_quarantined(token, weight(token, offset=None), UTC_ALWAYS, fallback_identity("policy-utc"))
                result = self.body(record_of(token))
                self.assertEqual((type(result), result.reason, result.pointer), (Quarantine, REASON, POINTER))


# ---- R5 ----

class R5Bounds(Checks):
    def test_mass_bounds_are_the_pinned_sdk_bounds(self) -> None:
        for bound, value in ((WEIGHT_MIN_GRAMS, 0), (WEIGHT_MAX_GRAMS, 1_000_000)):
            self.assertIn(type(bound), (int, float))
            self.assertEqual(bound, value)
        # Both bounds are inclusive, however the bound is written.
        for token in ("0", "0.0", "0e0", "1000000", "1000000.0", "1e6", "1E+6", "1000000e0"):
            with self.subTest(token):
                self.assert_body(token)
                self.assert_float_body(token)
        # The nearest doubles inside each bound are accepted.
        for value in (math.nextafter(0.0, 1.0), math.nextafter(1e6, 0.0), 0.5, 999999.5):
            with self.subTest(value=value):
                self.assert_body(repr(value))
        self.assertEqual(self.assert_body("0").body_bytes, b'{"weight_grams":0}')
        self.assertEqual(self.assert_body("1000000").body_bytes, b'{"weight_grams":1000000}')

    def test_mass_outside_bounds_is_quarantined(self) -> None:
        # The nearest doubles beyond each bound.
        self.assertEqual(decoded("-5e-324"), math.nextafter(0.0, -1.0))
        self.assertEqual(decoded("1000000.0000000001"), math.nextafter(1e6, math.inf))
        for token in OUTSIDE_TOKENS:
            with self.subTest(token[:24]):
                self.assertFalse(inside(decoded(token)))
                self.assert_quarantined(token)
                self.assert_quarantined(token, weight(token, offset=None, device=False), UTC_ALWAYS,
                                        fallback_identity("policy-utc"))
        # Negative zero is below the SDK's lower bound: Kotlin's Double.compareTo orders -0.0 before 0.0.
        # JSON "-0" decodes to the integer 0, which has no sign, and stays accepted.
        self.assertFalse(inside(decoded("-0.0")))
        for token in ("-0.0", "-0e0", "-0.000"):
            with self.subTest(token):
                self.assert_quarantined(token)
        self.assert_body("-0")
        kolkata = (policy("policy-kolkata", None, "Asia/Kolkata"),)
        self.assert_quarantined("-1", weight("-1", offset=None), kolkata, fallback_identity("policy-kolkata"))

    def test_common_outcomes_take_precedence(self) -> None:
        fault = "7777777.25"
        new_york = (policy("policy-new-york", None, "America/New_York"),)
        cases = [
            # (name, projection keyword arguments, policies, common outcome)
            ("year-0000 instant", {"time": "0000-01-01T00:00:00Z"}, (),
             Quarantine("instant-out-of-range", "/record/time", VERSION)),
            ("experienced local date beyond 9999", {"time": "9999-12-31T23:00:00Z", "offset": "+01:00"}, (),
             Quarantine("instant-out-of-range", "/record/time", VERSION)),
            ("inferred local date before 0001", {"time": "0001-01-01T00:00:00Z", "offset": None}, new_york,
             Quarantine("instant-out-of-range", "/record/time", VERSION, fallback_identity("policy-new-york"))),
            ("missing policy", {"offset": None}, (),
             PolicyPending("fallback-timezone", "fallback-timezone-policy-missing", VERSION)),
            ("unknown record field", {"extra": {"note": HEALTH_MARKER}}, (),
             Quarantine("projection-schema-invalid", "/record/note", VERSION)),
            ("invalid offset", {"offset": "+19:00"}, (),
             Quarantine("projection-schema-invalid", "/record/zone_offset", VERSION)),
        ]
        for name, kw, policies, common in cases:
            with self.subTest(name):
                self.assertEqual(self.outcome(weight(fault, **kw), policies), common)
                self.assertEqual(self.outcome(weight("1" + "0" * 400, **kw), policies), common)
        # Controls: without the common fault, the same mass is this processor's Quarantine.
        self.assert_quarantined(fault, weight(fault, time="0001-01-01T00:00:00Z", offset="+00:00"))
        self.assert_quarantined(fault, weight(fault, time="9999-12-31T22:00:00Z", offset="+01:00"))
        self.assert_quarantined(fault, weight(fault, offset=None), new_york, fallback_identity("policy-new-york"))
        self.assert_quarantined(fault)
        # A mass that is not a number is a schema fault, not this processor's.
        self.assertEqual(self.outcome(weight('"7777777"')), Quarantine("projection-schema-invalid", POINTER, VERSION))


# ---- R6 ----

class R6NoDerivation(Checks):
    def test_body_ignores_time_attribution_and_policies(self) -> None:
        token = "81234.567"
        kolkata = (policy("policy-kolkata", None, "Asia/Kolkata"),)
        later = (policy("policy-utc", None, "UTC"),
                 policy("policy-auckland", datetime(2026, 1, 1, tzinfo=UTC), "Pacific/Auckland"))
        variants = [
            (weight(token), ()),
            (weight(token, offset="-08:00"), ()),
            (weight(token, offset="+00:00"), ()),
            (weight(token, offset="+18:00"), ()),
            (weight(token, offset=None), UTC_ALWAYS),
            (weight(token, offset=None), kolkata),
            (weight(token, offset=None), later),
            (weight(token, time="2026-01-15T23:59:59.999999999Z"), ()),
            (weight(token, time="1900-03-01T00:00:00Z", offset="-12:00"), ()),
            (weight(token, recording_method=0), ()),
            (weight(token, recording_method=3), ()),
            (weight(token, device=False), ()),
            (weight(token, provider="other-provider-7"), ()),
            (weight(token, lmt="2026-02-01T00:00:00Z"), ()),
            (weight(token, identity="00000000-0000-4000-8000-0000000000aa"), ()),
        ]
        headers = set()
        local_dates = set()
        identities = set()
        for data, policies in variants:
            with self.subTest(sha(data), policies=[item.policy_id for item in policies]):
                outcome = self.assert_body(token, data, policies)
                self.assertEqual(outcome.body_bytes, b'{"weight_grams":81234.567}')
                headers.add(outcome.header_bytes)
                local_dates.add(outcome.header.time.start.local_date)
                identities.add(outcome.policy_identity)
        self.assertEqual(len(headers), len(variants))
        self.assertGreater(len(local_dates), 2)
        self.assertGreater(len(identities), 2)

    def test_body_carries_no_derived_measure(self) -> None:
        # Several masses at one instant, from one origin, in varying order: each body is its own mass.
        tokens = ["72500.5", "80000", "64000.25", "80000", "1000000", "0", "72500.5"]
        sequences = [tokens, list(reversed(tokens)), tokens[3:] + tokens[:3]]
        for sequence in sequences:
            for token in sequence:
                with self.subTest(token=token):
                    outcome = self.assert_body(token)
                    body = json.loads(outcome.body_bytes)
                    self.assertEqual(set(body), {"weight_grams"})
                    if decoded(token):
                        self.assertNotEqual(body["weight_grams"], decoded(token) / 1000)
        self.assertEqual(self.assert_body("80000").body_bytes, b'{"weight_grams":80000}')
        self.assertEqual(self.assert_body("64000.25").body_bytes, b'{"weight_grams":64000.25}')


# ---- R7 ----

class R7TotalPureAndHealthDataFree(Checks):
    def test_schema_valid_edges_never_raise(self) -> None:
        tokens = WHOLE_TOKENS + FRACTIONAL_TOKENS + OUTSIDE_TOKENS + (
            "-0", "-0.0", "2.2250738585072014e-308", "4.9e-324", "1e-300", "-1e-300", str(2**53), str(2**63),
            "1" + "0" * 308, "1" + "0" * 309, "100000000000000000000000", "1e+23", "1e21")
        places = (
            {"time": TIME, "offset": "+05:30"},
            {"time": TIME, "offset": None},
            {"time": "0001-01-01T00:00:00Z", "offset": "+00:00"},
            {"time": "9999-12-31T23:59:59.999999999Z", "offset": "+00:00"},
            {"time": "1970-01-01T00:00:00Z", "offset": "-18:00"},
        )
        for token in tokens:
            for place in places:
                with self.subTest(token=token[:24], **place):
                    data = weight(token, **place)
                    self.assertEqual(list(SCHEMA.iter_errors(json.loads(data))), [])
                    value = decoded(token)
                    result = self.body(record_of(token, **place))
                    outcome = self.outcome(data, () if place["offset"] is not None else UTC_ALWAYS)
                    identity = "none" if place["offset"] is not None else fallback_identity("policy-utc")
                    if inside(value):
                        self.assertIsInstance(outcome, CanonicalRevision, repr(outcome))
                        self.assertEqual(outcome.body_bytes, expected_body(token))
                        self.assertEqual(dict(result), {"weight_grams": float(value)})
                        self.assertIs(type(result["weight_grams"]), float)
                    else:
                        self.assertEqual(outcome, Quarantine(REASON, POINTER, VERSION, identity))
                        self.assertEqual((type(result), result.reason, result.pointer), (Quarantine, REASON, POINTER))

    def test_processor_is_pure(self) -> None:
        # The record is not changed, and the same record always gives the same result.
        for token in ("72500", "72500.5", "1" + "0" * 400, "-1", "0", "1000000"):
            with self.subTest(token[:24]):
                record = record_of(token)
                before = copy.deepcopy(record)
                temporal = temporal_for(record)
                first = self.body(record)
                self.assertEqual(record, before)
                self.assertEqual(type(record["weight_grams"]), type(before["weight_grams"]))
                second = self.body(record)
                self.assertEqual(first, second)
                self.assertEqual(temporal, temporal_for(record))
        code = r"""
import json, os, sys, time, zoneinfo
# Modules the suite itself, or an implementation, commonly needs are imported first, so that only the
# implementation module's own import and its calls run under observation.
import jsonschema, tzdata, logging, decimal, fractions, hashlib, re, importlib.resources, pathlib, dataclasses
import typing, collections, functools, itertools, calendar, math, unicodedata, copy, enum, struct, base64, datetime
import string, operator, bisect, heapq, io, traceback, unittest, subprocess, tempfile, shutil, random, uuid, types
host = [os.path.realpath(p) for p in zoneinfo.TZPATH] + [os.path.realpath("/etc/localtime"), "/etc/localtime"]
events = []
strict = [False]
WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
def hook(event, args):
    if event == "open":
        path, mode, flags = args
        if strict[0]:
            events.append(["open-during-interpret-body", os.fsdecode(path) if isinstance(path, (str, bytes)) else "fd"])
        if isinstance(path, (str, bytes)):
            path = os.fsdecode(path)
            if (mode and any(c in mode for c in "wax+")) or (flags or 0) & WRITE:
                events.append(["write", path])
            real = os.path.realpath(path)
            if any(real == h or real.startswith(h.rstrip("/") + "/") for h in host):
                events.append(["host-timezone", path])
    elif event.split(".")[0] in ("socket", "sqlite3", "subprocess", "ctypes", "webbrowser", "urllib", "http") or event in (
            "os.system", "os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.chmod", "os.utime", "os.truncate",
            "os.symlink", "os.link", "os.posix_spawn", "os.exec", "os.fork", "shutil.copyfile", "shutil.rmtree"):
        events.append([event])
    elif strict[0] and event in ("os.listdir", "os.scandir", "glob.glob", "pathlib.Path.glob", "os.walk"):
        events.append(["directory-read-during-interpret-body", event])
def forbidden(*args, **kwargs):
    raise AssertionError("clock read")
def explicit(original, index):
    def guard(*args):
        if len(args) <= index or args[index] is None:
            raise AssertionError("clock read")
        return original(*args)
    return guard
sys.addaudithook(hook)
for name in ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns", "process_time",
             "process_time_ns", "thread_time", "thread_time_ns", "clock_gettime", "clock_gettime_ns",
             "localtime", "ctime", "mktime"):
    if hasattr(time, name):
        setattr(time, name, forbidden)
time.gmtime = explicit(time.gmtime, 0)
time.asctime = explicit(time.asctime, 0)
time.strftime = explicit(time.strftime, 1)
import activity_tracker_portal.interpret_weight as module
import test_interpret_weight_1 as t
summary = t.scenario_summary()
records = [t.record_of(token, offset=offset) for token in ("72500", "72500.5", "0", "1000000", "-1", "1" + "0" * 400)
           for offset in ("+05:30", None)]
warm = [repr(module.PROCESSOR.interpret_body(record, t.temporal_for(record))) for record in records]
strict[0] = True
observed = [repr(module.PROCESSOR.interpret_body(record, t.temporal_for(record))) for record in records]
strict[0] = False
print(json.dumps({"store": "activity_tracker_portal.operational_store" in sys.modules, "events": events,
                  "stable": warm == observed and summary == t.scenario_summary()}))
"""
        result = json.loads(run_python(code, {"TZ": "Europe/London"}))
        self.assertEqual(result, {"store": False, "events": [], "stable": True})

    def test_outcomes_are_health_data_free(self) -> None:
        # Distinctive fault masses; none of their digit runs may appear in any outcome.
        faults = {"7777777.25": "7777777", "-31415.875": "31415", "2718281828459045235360": "2718281828",
                  "-1618033988749.5": "1618033988", "4" + "6" * 399: "66666666", "1000000.0000000001": "0000000001"}
        allowed = {"reason", "pointer", "processor_version", "policy_identity"}
        marked = dict(provider=HEALTH_MARKER, identity=SOURCE_IDENTITY)
        for token, digits in faults.items():
            for offset, policies in (("+05:30", ()), (None, UTC_ALWAYS)):
                with self.subTest(token=token[:24], offset=offset):
                    data = weight(token, offset=offset, **marked)
                    self.assertIn(HEALTH_MARKER.encode(), data)
                    outcome = self.outcome(data, policies)
                    self.assertIsInstance(outcome, Quarantine, repr(outcome))
                    self.assertEqual({field.name for field in dataclasses.fields(outcome)}, allowed)
                    self.assertEqual((outcome.reason, outcome.pointer), (REASON, POINTER))
                    for text in (repr(outcome), str(outcome), outcome.reason, outcome.pointer,
                                 outcome.processor_version, outcome.policy_identity):
                        self.assertNotIn(HEALTH_MARKER, text)
                        self.assertNotIn(IDENTITY, text)
                        self.assertNotIn(digits, text)
                    # What the processor itself returns carries only the reason and the pointer.
                    record = record_of(token, offset=offset)
                    direct = self.body(record)
                    self.assertIsInstance(direct, Quarantine)
                    self.assertEqual((direct.reason, direct.pointer), (REASON, POINTER))
                    for text in (repr(direct), str(direct)):
                        self.assertNotIn(digits, text)
                        self.assertNotIn(HEALTH_MARKER, text)
                        self.assertNotIn(IDENTITY, text)
        # A revision's body carries the mass and nothing from the envelope.
        outcome = self.revision(weight("86420.125", **marked))
        self.assertEqual(outcome.body_bytes, b'{"weight_grams":86420.125}')
        self.assertNotIn(HEALTH_MARKER.encode(), outcome.body_bytes)
        self.assertNotIn(IDENTITY.encode(), outcome.body_bytes)


if __name__ == "__main__":
    unittest.main()
