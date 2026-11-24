"""Acceptance tests for `interpret-source-projection@1`.

Requirement numbers refer to `packs/interpret-source-projection-1.spec.md`. Inputs are the
synthetic `projection/v1` vectors and hand-built synthetic projections; processors and
Fallback Timezone Policy timelines are test-local. Health-data and identity markers in
projection values and Source Identities prove that outcomes and failures carry neither.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import unittest
from datetime import date, datetime, timedelta, timezone
from importlib import resources
from pathlib import Path

import tzdata
from jsonschema import Draft202012Validator

from activity_tracker_portal.interpret_source_projection import (
    COMMON_VERSION,
    UNITS,
    BodySchema,
    CanonicalRevision,
    Endpoint,
    Envelope,
    FallbackTimezonePolicy,
    Instant,
    InterpretFailure,
    NumericField,
    PolicyPending,
    ProcessingInput,
    Quarantine,
    RevisionHeader,
    TemporalHeader,
    Unmodeled,
    decode_envelope,
    interpret,
)

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PROJECTION = ROOT / "projection/v1"
CONTRACT = json.loads((ROOT / "governance/scopes/interpret-source-projection.1.json").read_text(encoding="utf-8"))
EFFECTS = json.loads((ROOT / "packs/interpret-source-projection-1.effects.json").read_text(encoding="utf-8"))
SCOPE_MAP = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
MODEL = json.loads((PROJECTION / "model.json").read_text(encoding="utf-8"))
POSITIVE = json.loads((PROJECTION / "vectors/positive.json").read_text(encoding="utf-8"))["vectors"]
NEGATIVE = json.loads((PROJECTION / "vectors/schema-negatives.json").read_text(encoding="utf-8"))["vectors"]
VECTORS = {vector["name"]: vector for vector in POSITIVE}
VALIDATORS = {
    record_type: Draft202012Validator(
        json.loads((PROJECTION / f"schemas/{record_type}.schema.json").read_text(encoding="utf-8")))
    for record_type in MODEL["records"]
}

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
HC = "health-connect"
ORIGIN = "org.example.synthetic.source"
SDK = "androidx.health.connect:connect-client:1.1.0"
HEALTH_MARKER = "synthetic-health-marker"
IDENTITY = "synthetic-identity-marker"
SOURCE_IDENTITY = IDENTITY + "-00000000-0000-4000-8000-000000000001"
CLIENT_RECORD_ID = "synthetic-client-record-id-sentinel"
LMT = "2026-01-15T09:00:00.123456789Z"
FALLBACK = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
PROCESSOR_VERSION = "interpret-synthetic@1"
KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
POINTER = re.compile(r"^(/[a-z0-9_]+)*$")
COMMON_REASONS = {
    "projection-not-json",
    "projection-envelope-invalid",
    "projection-schema-invalid",
    "interval-reversed",
    "record-type-not-modeled",
    "schema-version-not-modeled",
    "fallback-timezone-policy-missing",
    "instant-out-of-range",
}
R10_KEYS = {
    "source_system_kind", "origin", "record_type", "source_identity", "record_id",
    "selection_id", "evidence_id", "projection_sha256", "projection_schema_version",
    "projection_implementation_version", "sdk_version", "provider_version", "last_modified_time",
    "recording_method", "device", "time", "header_version", "processor_version", "body_schema",
    "policy_identity", "body_sha256",
}
INSTANT_TEXT = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d{3}|\d{6}|\d{9}))?Z$")


# ---- Independent oracles ----

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def epoch_ns(text: str) -> int:
    year, month, day, hour, minute, second, fraction = INSTANT_TEXT.match(text).groups()
    whole = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), tzinfo=UTC)
    seconds = (whole - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(seconds=1)
    return seconds * 10**9 + int((fraction or "").ljust(9, "0"))


def truncated(text: str) -> datetime:
    """The instant at the store's microsecond precision, truncated, never rounded."""
    year, month, day, hour, minute, second, fraction = INSTANT_TEXT.match(text).groups()
    return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second),
                    int((fraction or "").ljust(9, "0")[:6]), tzinfo=UTC)


def local_date(text: str, offset: str) -> date:
    sign = -1 if offset[0] == "-" else 1
    delta = sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6]))
    return (truncated(text) + delta).date()


def instant(text: str) -> Instant:
    return Instant(text, epoch_ns(text))


def es_number(value) -> str:
    """ECMAScript Number::toString (RFC 8785)."""
    from decimal import Decimal
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


def canonical(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return es_number(value)
    if isinstance(value, str):
        out = []
        for ch in value:
            if ch in '"\\':
                out.append("\\" + ch)
            elif ch in "\b\f\n\r\t":
                out.append({"\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch])
            elif ord(ch) < 0x20:
                out.append(f"\\u{ord(ch):04x}")
            else:
                out.append(ch)
        return '"' + "".join(out) + '"'
    if isinstance(value, list):
        return "[" + ",".join(canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda item: list(item[0].encode("utf-16-be")))
        return "{" + ",".join(canonical(k) + ":" + canonical(v) for k, v in items) + "}"
    raise TypeError(type(value).__name__)


def is_canonical(data: bytes) -> bool:
    """RFC 8785 form: every number token is the ECMAScript rendering of the binary64 value it denotes.

    An integer token beyond what binary64 holds exactly, such as 9007199254740993, fails; the token
    10000000000000000 that RFC 8785 writes for the float 1e16 passes.
    """
    return canonical(json.loads(data)).encode("utf-8") == data


def endpoint_json(endpoint: Endpoint) -> dict:
    """The documented serialized form of one endpoint in the header's `time` object."""
    out = {"instant": endpoint.instant.text, "applied_offset": endpoint.applied_offset,
           "local_date": endpoint.local_date.isoformat(), "attribution": endpoint.attribution}
    if endpoint.source_offset is not None:
        out["source_offset"] = endpoint.source_offset
    if endpoint.policy_id is not None:
        out["policy_id"] = endpoint.policy_id
    return out


def time_json(temporal: TemporalHeader) -> dict:
    """The documented serialized form of the temporal header: `end` is present only for an interval."""
    out = {"shape": temporal.shape, "explicit_time": temporal.explicit_time, "start": endpoint_json(temporal.start)}
    if temporal.end is not None:
        out["end"] = endpoint_json(temporal.end)
    return out


def json_values(value):
    """Every scalar and key in a JSON value."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from json_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from json_values(item)
    else:
        yield value


def json_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from json_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from json_keys(item)


# ---- Synthetic projections and inputs ----

def encode(projection) -> bytes:
    return json.dumps(projection, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def envelope(record_type: str, record: dict, *, device: bool = True, lmt: str = LMT,
             identity: str = SOURCE_IDENTITY, provider: str = "synthetic-provider-1") -> dict:
    metadata = {
        "id": identity,
        "data_origin": {"package_name": ORIGIN},
        "last_modified_time": lmt,
        "client_record_id": CLIENT_RECORD_ID,
        "client_record_version": "7",
        "recording_method": 2,
    }
    if device:
        metadata["device"] = {"type": 1, "manufacturer": "Synthetic Devices", "model": "SD-1"}
    return {
        "record_type": record_type,
        "projection_schema_version": 1,
        "projection_implementation_version": "0.1.0-synthetic",
        "sdk_version": SDK,
        "provider_version": provider,
        "metadata": metadata,
        "record": record,
    }


def timed(record: dict, **times) -> dict:
    for key, value in times.items():
        if value is not None:
            record[key] = value
    return record


def steps(start: str = "2026-01-15T07:30:00Z", end: str = "2026-01-15T08:00:00Z", *,
          start_offset: str | None = "+05:30", end_offset: str | None = "+05:30", count: int = 4321, **kw) -> dict:
    record = timed({"start_time": start}, start_zone_offset=start_offset)
    record["end_time"] = end
    timed(record, end_zone_offset=end_offset)
    record["count"] = count
    return envelope("steps", record, **kw)


def weight(time: str = "2026-01-15T07:30:00.250Z", *, offset: str | None = "+05:30", grams: float = 72500.5, **kw) -> dict:
    return envelope("weight", timed({"time": time}, zone_offset=offset) | {"weight_grams": grams}, **kw)


def sleep(*, title: str = HEALTH_MARKER, start_offset: str | None = "+05:30", end_offset: str | None = "+05:30") -> dict:
    record = timed({"start_time": "2026-01-15T07:30:00Z"}, start_zone_offset=start_offset)
    record["end_time"] = "2026-01-15T08:00:00Z"
    timed(record, end_zone_offset=end_offset)
    record.update({"title": title, "notes": HEALTH_MARKER, "stages": [
        {"start_time": "2026-01-15T07:30:00Z", "end_time": "2026-01-15T07:40:00Z", "stage": 2},
        {"start_time": "2026-01-15T07:40:00Z", "end_time": "2026-01-15T08:00:00Z", "stage": 4}]})
    return envelope("sleep-session", record, provider=HEALTH_MARKER)


def vector(name: str) -> dict:
    return copy.deepcopy(VECTORS[name]["projection"])


def make_input(projection, **overrides) -> ProcessingInput:
    data = projection if isinstance(projection, bytes) else encode(projection)
    try:
        decoded = json.loads(data)
    except ValueError:
        decoded = {}
    decoded = decoded if isinstance(decoded, dict) else {}
    metadata = decoded.get("metadata") if isinstance(decoded.get("metadata"), dict) else {}
    origin = metadata.get("data_origin") if isinstance(metadata.get("data_origin"), dict) else {}
    try:
        modified = truncated(metadata["last_modified_time"])
    except (KeyError, TypeError, AttributeError, ValueError):
        modified = FALLBACK
    fields = {
        "source_system_kind": HC,
        "origin": origin.get("package_name", ORIGIN),
        "record_type": decoded.get("record_type", "steps"),
        "source_identity": metadata.get("id", SOURCE_IDENTITY),
        "record_id": 41,
        "selection_id": 42,
        "evidence_id": 43,
        "projection_sha256": sha(data),
        "last_modified_time": modified,
        "projection_schema_version": decoded.get("projection_schema_version", 1),
        "projection": data,
    }
    fields.update(overrides)
    return ProcessingInput(**fields)


def run(projection, processor=None, policies=(), **overrides):
    return interpret(make_input(projection, **overrides), processor, list(policies))


def policy(policy_id: str, effective_from: datetime | None, zone: str) -> FallbackTimezonePolicy:
    return FallbackTimezonePolicy(policy_id, effective_from, zone)


UTC_ALWAYS = (policy("policy-utc", None, "UTC"),)


class SyntheticProcessor:
    """A test-local concrete processor that records what it receives."""

    def __init__(self, record_type: str = "steps", *, version: str = PROCESSOR_VERSION, supported=(1,),
                 fields=(), body=None, reasons=(), body_record_type: str | None = None, body_version: int = 1) -> None:
        self.record_type = record_type
        self.processor_version = version
        self.supported_schema_versions = tuple(supported)
        self.body_schema = BodySchema(body_record_type or record_type, body_version, tuple(fields))
        self.quarantine_reasons = tuple(reasons)
        self._body = body if callable(body) else (lambda record, temporal, value=body: copy.deepcopy(value or {}))
        self.calls: list = []

    def interpret_body(self, record, temporal):
        self.calls.append((copy.deepcopy(record), temporal))
        return self._body(record, temporal)


STEPS_FIELDS = (NumericField("/count", "integer", "count"),)


def steps_processor(**kw) -> SyntheticProcessor:
    kw.setdefault("fields", STEPS_FIELDS)
    kw.setdefault("body", lambda record, temporal: {"count": record["count"]})
    return SyntheticProcessor("steps", **kw)


def body_processor(body, fields, **kw) -> SyntheticProcessor:
    return SyntheticProcessor("steps", fields=fields, body=lambda record, temporal: copy.deepcopy(body), **kw)


def scenario_summary() -> str:
    """A deterministic summary of representative outcomes, compared across processes and host settings."""
    new_york = (policy("policy-zulu", None, "UTC"), policy("policy-alpha", datetime(2026, 3, 1, tzinfo=UTC), "America/New_York"))
    out = []
    cases = [
        (steps(), steps_processor(), ()),
        (steps("2026-02-20T12:00:00.000000001Z", "2026-03-08T04:30:00.999999999Z", start_offset=None, end_offset=None),
         steps_processor(), new_york),
        (weight("2026-01-15T18:20:00.123456789Z", offset=None), SyntheticProcessor("weight"),
         (policy("policy-kathmandu", None, "Asia/Kathmandu"),)),
        (weight(offset=None), SyntheticProcessor("weight"), ()),
        (weight("1800-01-02T04:56:01Z", offset=None), SyntheticProcessor("weight"), (policy("policy-new-york", None, "America/New_York"),)),
        (weight("9999-12-31T23:00:00Z", offset="+01:00"), SyntheticProcessor("weight"), ()),
        (sleep(), None, ()),
        (steps(count="4321"), None, ()),
    ]
    for projection, processor, policies in cases:
        outcome = run(projection, processor, policies)
        if isinstance(outcome, CanonicalRevision):
            out.append([outcome.header_bytes.hex(), outcome.body_bytes.hex(), outcome.revision_sha256,
                        outcome.policy_identity])
        else:
            out.append(repr(outcome))
    out.append(repr(decode_envelope(encode(steps()), "steps", 1)))
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
    def refused(self, projection, processor=None, policies=(), **overrides) -> str:
        with self.assertRaises(InterpretFailure) as caught:
            run(projection, processor, policies, **overrides)
        return caught.exception.failure

    def revision(self, projection, processor=None, policies=(), **overrides) -> CanonicalRevision:
        outcome = run(projection, processor if processor is not None else steps_processor(), policies, **overrides)
        self.assertIsInstance(outcome, CanonicalRevision, repr(outcome))
        self.assertEqual(json.loads(outcome.header_bytes)["time"], time_json(outcome.header.time))
        return outcome

    def assert_health_free(self, text: str) -> None:
        self.assertNotIn(HEALTH_MARKER, text)
        self.assertNotIn(IDENTITY, text)


# ---- R1 ----

class R1GovernedScopeContract(unittest.TestCase):
    def test_contract_shape_and_dependency(self) -> None:
        self.assertEqual((CONTRACT["scope"], CONTRACT["version"], CONTRACT["kind"]), ("interpret-source-projection", "1", "shared"))
        self.assertEqual(CONTRACT["dependencies"], [{"scope": "reconcile-source-record", "version": "1"}])
        self.assertEqual({failure["name"]: failure["outcome"] for failure in CONTRACT["named_failures"]}, {
            "processing-input-invalid": "invariant-refusal",
            "projection-digest-mismatch": "invariant-refusal",
            "projection-identity-mismatch": "invariant-refusal",
            "processor-invariant-violated": "invariant-refusal",
        })
        self.assertEqual(CONTRACT["observable_effects"], [])
        self.assertEqual({item["mode"] for item in CONTRACT["inputs"]}, {"pure"})
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["specification"], "packs/interpret-source-projection-1.spec.md")
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_interpret_source_projection_1.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_interpret_source_projection_1.sh")
        self.assertEqual(oracle["languages"], ["python"])

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        row = next(line for line in SCOPE_MAP.splitlines() if line.startswith("| `interpret-source-projection` |"))
        self.assertIn("| shared |", row)
        for outcome in ("Canonical Revision", "Unmodeled", "Policy-Pending", "Quarantine", "no persistence effect"):
            self.assertIn(outcome, row)
        self.assertTrue(row.rstrip().endswith("| `reconcile-source-record` |"), row)

    def test_effects_declaration_is_empty(self) -> None:
        self.assertEqual(EFFECTS, {"effects_format": 1, "effects": []})


# ---- R2 ----

class R2PureAndDeterministic(Checks):
    def test_same_input_gives_identical_outcome(self) -> None:
        timeline = [policy("policy-zulu", None, "UTC"), policy("policy-alpha", datetime(2026, 1, 15, 7, 45, tzinfo=UTC), "Asia/Kolkata")]
        projection = steps(start_offset=None, end_offset=None)
        first = interpret(make_input(projection), steps_processor(), timeline)
        second = interpret(make_input(copy.deepcopy(projection)), steps_processor(), tuple(copy.deepcopy(timeline)))
        self.assertIsInstance(first, CanonicalRevision)
        self.assertEqual(first, second)
        self.assertEqual((first.header_bytes, first.body_bytes, first.revision_sha256),
                         (second.header_bytes, second.body_bytes, second.revision_sha256))
        self.assertEqual(timeline[1], policy("policy-alpha", datetime(2026, 1, 15, 7, 45, tzinfo=UTC), "Asia/Kolkata"))
        for outcome in (first, first.header, first.header.time, first.header.time.start, first.header.time.start.instant,
                        decode_envelope(encode(projection), "steps", 1), Unmodeled("record-type-not-modeled"),
                        Quarantine("interval-reversed", "/record/end_time"),
                        PolicyPending("fallback-timezone", "fallback-timezone-policy-missing")):
            field = dataclasses.fields(outcome)[0].name
            with self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(outcome, field, None)
        self.assertEqual(scenario_summary(), scenario_summary())

    def test_outcome_ignores_host_timezone_and_locale(self) -> None:
        expected = scenario_summary()
        code = "import test_interpret_source_projection_1 as t; print(t.scenario_summary())"
        for settings in (
            {"TZ": "America/Los_Angeles", "LC_ALL": "C", "LANG": "C", "PYTHONHASHSEED": "1"},
            {"TZ": "Asia/Kolkata", "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8", "PYTHONHASHSEED": "2"},
            {"TZ": "Pacific/Chatham", "LC_ALL": "de_DE.UTF-8", "LANG": "de_DE.UTF-8", "PYTHONHASHSEED": "3"},
        ):
            with self.subTest(**settings):
                self.assertEqual(run_python(code, settings), expected)

    def test_interpret_has_no_store_or_clock_dependency(self) -> None:
        code = r"""
import json, os, sys, time, zoneinfo
# Modules the suite itself, or an implementation, commonly needs are imported first, so that only the
# implementation module's own import and its calls run under observation.
import jsonschema, tzdata, logging, decimal, fractions, hashlib, re, importlib.resources, pathlib, dataclasses
import typing, collections, functools, itertools, calendar, math, unicodedata, copy, enum, struct, base64, datetime
import string, operator, bisect, heapq, io, traceback, unittest, subprocess, tempfile, shutil, random, uuid, types
host = [os.path.realpath(p) for p in zoneinfo.TZPATH] + [os.path.realpath("/etc/localtime"), "/etc/localtime"]
events = []
WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
def hook(event, args):
    if event == "open":
        path, mode, flags = args
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
def forbidden(*args, **kwargs):
    raise AssertionError("clock read")
def explicit(original, index):
    # A pure conversion or formatting call is allowed only with an explicit time argument.
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
import activity_tracker_portal.interpret_source_projection as module
import test_interpret_source_projection_1 as t
t.scenario_summary()
print(json.dumps({"store": "activity_tracker_portal.operational_store" in sys.modules, "events": events}))
"""
        result = json.loads(run_python(code, {"TZ": "Europe/London"}))
        self.assertEqual(result, {"store": False, "events": []})


# ---- R3 ----

class R3EnvelopeDecoding(Checks):
    def test_envelope_yields_index_key_and_time(self) -> None:
        for vector_ in POSITIVE:
            projection = vector_["projection"]
            metadata = projection["metadata"]
            decoded = decode_envelope(vector_["bytes"].encode("utf-8"), vector_["record_type"], 1)
            self.assertEqual(decoded, Envelope(HC, metadata["data_origin"]["package_name"], metadata["id"],
                                               truncated(metadata["last_modified_time"]), instant(metadata["last_modified_time"])),
                             vector_["name"])
            self.assertIsNotNone(decoded.last_modified_time.utcoffset(), vector_["name"])
        # Envelope decoding is not schema validation: a usable envelope with an invalid body still yields its key.
        for name in ("unknown-record-field", "unknown-envelope-field", "missing-required-field", "integer-as-string",
                     "instant-april-31", "client-record-version-number"):
            negative = next(item for item in NEGATIVE if item["name"] == name)
            decoded = decode_envelope(encode(negative["projection"]), negative["record_type"], 1)
            self.assertIsInstance(decoded, Envelope, name)
            self.assertEqual(decoded.source_identity, negative["projection"]["metadata"]["id"], name)
        decoded = decode_envelope(encode(steps()), "steps", 1)
        self.assertEqual(decoded, Envelope(HC, ORIGIN, SOURCE_IDENTITY, datetime(2026, 1, 15, 9, 0, 0, 123456, tzinfo=UTC),
                                           Instant(LMT, epoch_ns(LMT))))

    def test_unparseable_bytes_are_quarantined(self) -> None:
        good = encode(steps()).decode("utf-8")
        cases = {
            "not json": b"synthetic-health-marker",
            "empty": b"",
            "invalid utf-8": b'{"record_type":"steps\xff"}',
            "array": b"[" + good.encode() + b"]",
            "string": b'"synthetic-health-marker"',
            "trailing data": good.encode() + b" {}",
            "duplicate top-level key": b'{"record_type":"steps",' + good[1:].encode(),
            "duplicate nested key": good.replace('"metadata":{', '"metadata":{"id":"synthetic-health-marker",', 1).encode(),
            "NaN": good.replace('"count":4321', '"count":NaN').encode(),
            "Infinity": good.replace('"count":4321', '"count":-Infinity').encode(),
            "overflowing number": good.replace('"count":4321', '"count":1e400').encode(),
        }
        for name, data in cases.items():
            with self.subTest(name):
                outcome = decode_envelope(data, "steps", 1)
                self.assertEqual(outcome, Quarantine("projection-not-json", ""))
                self.assert_health_free(repr(outcome))

    def test_envelope_field_failures_name_their_pointer(self) -> None:
        def mutated(change):
            projection = steps()
            change(projection)
            return encode(projection)

        cases = {
            "/record_type": [lambda p: p.pop("record_type"), lambda p: p.__setitem__("record_type", 7)],
            "/projection_schema_version": [lambda p: p.pop("projection_schema_version"),
                                           lambda p: p.__setitem__("projection_schema_version", "1"),
                                           lambda p: p.__setitem__("projection_schema_version", True)],
            "/metadata": [lambda p: p.pop("metadata"), lambda p: p.__setitem__("metadata", [HEALTH_MARKER])],
            "/metadata/id": [lambda p: p["metadata"].pop("id"), lambda p: p["metadata"].__setitem__("id", ""),
                             lambda p: p["metadata"].__setitem__("id", 12345)],
            "/metadata/data_origin": [lambda p: p["metadata"].__setitem__("data_origin", HEALTH_MARKER)],
            "/metadata/data_origin/package_name": [lambda p: p["metadata"]["data_origin"].pop("package_name"),
                                                   lambda p: p["metadata"]["data_origin"].__setitem__("package_name", ""),
                                                   lambda p: p["metadata"]["data_origin"].__setitem__("package_name", 5)],
            "/metadata/last_modified_time": [
                lambda p: p["metadata"].pop("last_modified_time"),
                lambda p: p["metadata"].__setitem__("last_modified_time", HEALTH_MARKER),
                lambda p: p["metadata"].__setitem__("last_modified_time", 1768467600),
                lambda p: p["metadata"].__setitem__("last_modified_time", "2026-02-30T09:00:00Z"),
                lambda p: p["metadata"].__setitem__("last_modified_time", "2025-02-29T09:00:00Z"),
                lambda p: p["metadata"].__setitem__("last_modified_time", "2026-01-15T14:30:00+05:30"),
                lambda p: p["metadata"].__setitem__("last_modified_time", "2026-01-15T09:00:00.12Z"),
                lambda p: p["metadata"].__setitem__("last_modified_time", "2026-01-15 09:00:00Z"),
                lambda p: p["metadata"].__setitem__("last_modified_time", "2026-01-15T09:00:00Z+05:30"),
                lambda p: p["metadata"].__setitem__("last_modified_time", " 2026-01-15T09:00:00Z"),
            ],
        }
        for pointer, changes in cases.items():
            for index, change in enumerate(changes):
                with self.subTest(pointer=pointer, case=index):
                    outcome = decode_envelope(mutated(change), "steps", 1)
                    self.assertEqual(outcome, Quarantine("projection-envelope-invalid", pointer))
                    self.assert_health_free(repr(outcome))

    def test_envelope_disagreeing_with_batch_is_quarantined(self) -> None:
        data = encode(steps())
        self.assertEqual(decode_envelope(data, "weight", 1), Quarantine("projection-envelope-invalid", "/record_type"))
        self.assertEqual(decode_envelope(data, "steps", 2), Quarantine("projection-envelope-invalid", "/projection_schema_version"))
        self.assertIsInstance(decode_envelope(data, "steps", 1), Envelope)
        height = next(item for item in NEGATIVE if item["name"] == "wrong-record-type")
        self.assertEqual(decode_envelope(encode(height["projection"]), "weight", 1),
                         Quarantine("projection-envelope-invalid", "/record_type"))

    def test_envelope_keeps_exact_instant_text(self) -> None:
        for text, micro in (
            ("2026-01-15T09:00:00.123456789Z", 123456),
            ("2026-01-15T09:00:00.999999999Z", 999999),
            ("2026-01-15T09:00:00.120Z", 120000),
            ("2026-01-15T09:00:00.000000Z", 0),
            ("2026-01-15T09:00:00Z", 0),
            ("2000-02-29T23:59:59.000000001Z", 0),
        ):
            with self.subTest(text):
                decoded = decode_envelope(encode(steps(lmt=text)), "steps", 1)
                self.assertIsInstance(decoded, Envelope)
                self.assertEqual(decoded.last_modified_instant, Instant(text, epoch_ns(text)))
                self.assertEqual(decoded.last_modified_time, truncated(text))
                self.assertEqual(decoded.last_modified_time.microsecond, micro)
        self.assertNotEqual(decode_envelope(encode(steps(lmt="2026-01-15T09:00:00.000Z")), "steps", 1).last_modified_instant,
                            decode_envelope(encode(steps(lmt="2026-01-15T09:00:00Z")), "steps", 1).last_modified_instant)


# ---- R4 ----

class R4OneOutcomeInFixedOrder(Checks):
    def test_check_order(self) -> None:
        good = steps()
        invalid = steps(count="4321")
        reversed_ = steps("2026-01-15T08:00:00Z", "2026-01-15T07:30:00Z", start_offset=None, end_offset=None)
        # 1 before 2
        self.assertEqual(self.refused(good, last_modified_time=datetime(2026, 1, 15, 9, 0, 0, 123456),
                                      projection_sha256="0" * 64), "processing-input-invalid")
        self.assertEqual(self.refused(good, steps_processor(), [policy("a", None, "Mars/Olympus_Mons")],
                                      projection_sha256="0" * 64), "processing-input-invalid")
        # 2 before 3
        self.assertEqual(self.refused(good, projection_sha256=sha(b"other"), origin="org.example.other"),
                         "projection-digest-mismatch")
        # 3 before 4
        self.assertEqual(self.refused(invalid, source_identity="other-identity"), "projection-identity-mismatch")
        # 4 before 5, with and without a processor
        self.assertEqual(run(invalid), Quarantine("projection-schema-invalid", "/record/count"))
        self.assertEqual(run(invalid, steps_processor(supported=(2,))),
                         Quarantine("projection-schema-invalid", "/record/count", PROCESSOR_VERSION))
        # 5 before 6 and 7
        self.assertEqual(run(reversed_), Unmodeled("record-type-not-modeled"))
        self.assertEqual(run(reversed_, steps_processor(supported=(2,))),
                         Unmodeled("schema-version-not-modeled", PROCESSOR_VERSION))
        # 6 before 7
        processor = steps_processor()
        self.assertEqual(run(reversed_, processor), Quarantine("interval-reversed", "/record/end_time", PROCESSOR_VERSION))
        # 7 before 8: the processor is not called
        quarantining = steps_processor(reasons=("synthetic-refusal",),
                                       body=lambda record, temporal: Quarantine("synthetic-refusal", "/record/count"))
        self.assertEqual(run(steps(start_offset=None, end_offset=None), quarantining),
                         PolicyPending("fallback-timezone", "fallback-timezone-policy-missing", PROCESSOR_VERSION))
        self.assertEqual(processor.calls, [])
        self.assertEqual(quarantining.calls, [])
        # 8, 9, and 10
        self.assertEqual(run(good, quarantining), Quarantine("synthetic-refusal", "/record/count", PROCESSOR_VERSION))
        self.assertEqual(len(quarantining.calls), 1)
        self.assertEqual(self.refused(good, steps_processor(body=lambda record, temporal: {"count": 1.5})),
                         "processor-invariant-violated")
        self.assertIsInstance(run(good, steps_processor()), CanonicalRevision)

    def test_each_failure_is_raised_not_returned(self) -> None:
        good = steps()
        cases = {
            "processing-input-invalid": lambda: run(good, SyntheticProcessor("weight")),
            "projection-digest-mismatch": lambda: run(good, projection_sha256=sha(encode(good) + b" ")),
            "projection-identity-mismatch": lambda: run(good, source_identity=SOURCE_IDENTITY + "-other"),
            "processor-invariant-violated": lambda: run(good, steps_processor(body=lambda record, temporal: [record["count"]])),
        }
        for name, call in cases.items():
            with self.subTest(name):
                with self.assertRaises(InterpretFailure) as caught:
                    call()
                self.assertEqual(caught.exception.failure, name)
                self.assertIsInstance(caught.exception, Exception)
        mismatches = {
            "record_type": "weight",
            "projection_schema_version": 2,
            "source_system_kind": "synthetic-other-kind",
            "origin": "org.example.other",
            "source_identity": SOURCE_IDENTITY + "-other",
            "last_modified_time": truncated(LMT) + timedelta(microseconds=1),
        }
        for field, value in mismatches.items():
            with self.subTest(identity=field):
                self.assertEqual(self.refused(good, None, (), **{field: value}), "projection-identity-mismatch")

    def test_malformed_input_is_refused(self) -> None:
        good = steps(start_offset=None, end_offset=None)
        t1 = datetime(2026, 1, 1, tzinfo=UTC)
        t2 = datetime(2026, 2, 1, tzinfo=UTC)
        timelines = {
            "naive effective_from": [policy("p1", datetime(2026, 1, 1), "UTC")],
            "decreasing": [policy("p1", t2, "UTC"), policy("p2", t1, "UTC")],
            "equal": [policy("p1", t1, "UTC"), policy("p2", t1, "Asia/Kolkata")],
            "equal instant in another offset": [policy("p1", t1, "UTC"), policy("p2", t1.astimezone(IST), "UTC")],
            "two open starts": [policy("p1", None, "UTC"), policy("p2", None, "UTC")],
            "open start after a dated one": [policy("p1", t1, "UTC"), policy("p2", None, "UTC")],
            "duplicate identity": [policy("p1", None, "UTC"), policy("p1", t1, "Asia/Kolkata")],
            "unknown zone": [policy("p1", None, "Mars/Olympus_Mons")],
            "unknown zone in an unused policy": [policy("p1", None, "UTC"), policy("p2", datetime(2030, 1, 1, tzinfo=UTC), "Not/AZone")],
        }
        for name, timeline in timelines.items():
            for processor in (None, steps_processor()):
                with self.subTest(name, processor=processor is not None):
                    self.assertEqual(self.refused(good, processor, timeline), "processing-input-invalid")
                    if processor is not None:
                        self.assertEqual(processor.calls, [])
        with self.subTest("naive last_modified_time"):
            self.assertEqual(self.refused(steps(), None, (), last_modified_time=datetime(2026, 1, 15, 9, 0, 0, 123456)),
                             "processing-input-invalid")
        with self.subTest("processor for another record type"):
            self.assertEqual(self.refused(steps(), SyntheticProcessor("weight")), "processing-input-invalid")
        with self.subTest("valid timeline is accepted"):
            timeline = [policy("p0", None, "UTC"), policy("p1", t1, "Asia/Kolkata"), policy("p2", t2, "UTC")]
            self.assertIsInstance(run(good, steps_processor(), timeline), CanonicalRevision)
            self.assertIsInstance(run(steps(), None, (), last_modified_time=truncated(LMT).astimezone(IST)), Unmodeled)


# ---- R5 ----

NEGATIVE_POINTERS = {
    "unknown-record-field": "/record/cadence",
    "unknown-envelope-field": "/extra",
    "missing-required-field": "/record/count",
    "null-optional-field": "/record/title",
    "undeclared-constant-value": "/record/stages/0/stage",
    "offset-without-colon": "/record/start_zone_offset",
    "instant-with-offset": "/record/time",
    "instant-two-fraction-digits": "/record/time",
    "client-record-version-number": "/metadata/client_record_version",
    "integer-as-string": "/record/count",
    "instant-february-30": "/record/time",
    "instant-april-31": "/record/end_time",
    "instant-century-february-29": "/record/samples/0/time",
}
NEGATIVE_VARIANTS = {
    "unknown-variant-kind": "/record/exercise_route_result",
    "variant-extra-field": "/record/exercise_route_result",
}
# Schema negatives whose envelope is unusable: decode_envelope quarantines them (R3), so interpret
# refuses them as projection-identity-mismatch (R4 step 3) and they never reach schema validation.
NEGATIVE_ENVELOPES = {
    "missing-metadata-id": "/metadata/id",
    "wrong-record-type": "/record_type",
    "instant-non-leap-february-29": "/metadata/last_modified_time",
}
ENVELOPE_PATHS = {
    ("record_type",), ("projection_schema_version",), ("metadata",), ("metadata", "id"),
    ("metadata", "data_origin"), ("metadata", "data_origin", "package_name"), ("metadata", "last_modified_time"),
}


def pointer_of(path) -> str:
    return "".join(f"/{part}" for part in path)


def nodes(value, path=()):
    yield path, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from nodes(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from nodes(item, path + (index,))


def replacements(value):
    if isinstance(value, bool):
        return ["true", 0]
    if isinstance(value, int):
        return [True, "1", 2**60, 1.5]
    if isinstance(value, float):
        return ["1.5", True]
    if isinstance(value, str):
        return [12345, "", HEALTH_MARKER]
    if isinstance(value, dict):
        return ["x", []]
    return [{}, "x"]


def at(projection, path):
    for part in path:
        projection = projection[part]
    return projection


def single_faults(projection):
    """(pointer of the fault, mutated projection) for single-field faults outside the envelope."""
    for path, value in list(nodes(projection)):
        if path and path not in ENVELOPE_PATHS:
            for replacement in replacements(value):
                mutated = copy.deepcopy(projection)
                at(mutated, path[:-1])[path[-1]] = replacement
                yield path, mutated
            if isinstance(path[-1], str):
                mutated = copy.deepcopy(projection)
                del at(mutated, path[:-1])[path[-1]]
                yield path, mutated
        if isinstance(value, dict):
            mutated = copy.deepcopy(projection)
            at(mutated, path)["zz_synthetic_field"] = 1
            yield path + ("zz_synthetic_field",), mutated


class R5ClosedSchemaValidation(Checks):
    def test_every_positive_vector_is_schema_valid(self) -> None:
        self.assertEqual({vector_["record_type"] for vector_ in POSITIVE}, set(MODEL["records"]))
        for vector_ in POSITIVE:
            data = vector_["bytes"].encode("utf-8")
            self.assertEqual(interpret(make_input(data), None, []), Unmodeled("record-type-not-modeled"), vector_["name"])
            outcome = interpret(make_input(data), SyntheticProcessor(vector_["record_type"]), list(UTC_ALWAYS))
            self.assertIsInstance(outcome, CanonicalRevision, vector_["name"])
            self.assertEqual(outcome.body_bytes, b"{}", vector_["name"])

    def test_every_schema_negative_is_quarantined(self) -> None:
        self.assertEqual({item["name"] for item in NEGATIVE},
                         set(NEGATIVE_POINTERS) | set(NEGATIVE_VARIANTS) | set(NEGATIVE_ENVELOPES))
        for item in NEGATIVE:
            name, data = item["name"], encode(item["projection"])
            self.assertFalse(VALIDATORS[item["record_type"]].is_valid(item["projection"]), name)
            if name in NEGATIVE_ENVELOPES:
                self.assertEqual(decode_envelope(data, item["record_type"], 1),
                                 Quarantine("projection-envelope-invalid", NEGATIVE_ENVELOPES[name]), name)
                self.assertEqual(self.refused(data, record_type=item["record_type"]), "projection-identity-mismatch", name)
                continue
            for processor in (None, SyntheticProcessor(item["record_type"])):
                outcome = run(data, processor, UTC_ALWAYS)
                self.assertIsInstance(outcome, Quarantine, name)
                self.assertEqual(outcome.reason, "projection-schema-invalid", name)
                self.assertEqual(outcome.policy_identity, "none", name)
                self.assertEqual(outcome.processor_version, COMMON_VERSION if processor is None else PROCESSOR_VERSION, name)
                if name in NEGATIVE_POINTERS:
                    self.assertEqual(outcome.pointer, NEGATIVE_POINTERS[name], name)
                else:
                    self.assertTrue(outcome.pointer.startswith(NEGATIVE_VARIANTS[name]), (name, outcome.pointer))
                if processor is not None:
                    self.assertEqual(processor.calls, [], name)

    def test_schema_pointer_follows_traversal_order(self) -> None:
        def reversed_keys(value):
            if isinstance(value, dict):
                return {key: reversed_keys(value[key]) for key in reversed(list(value))}
            if isinstance(value, list):
                return [reversed_keys(item) for item in value]
            return value

        def faulted(base: dict, *changes) -> dict:
            projection = copy.deepcopy(base)
            for path, value in changes:
                at(projection, path[:-1])[path[-1]] = value
            # Every object, nested ones included, is serialized in reverse model order, so input order
            # and model order disagree; `record` also precedes `metadata`.
            return reversed_keys(projection)

        cases = [
            # metadata before record
            (faulted(vector("steps-maximal"), (("record", "start_time"), "x"), (("metadata", "recording_method"), 99)),
             "/metadata/recording_method"),
            # model order within metadata, not alphabetical: recording_method before device
            (faulted(vector("steps-maximal"), (("metadata", "device", "type"), 99), (("metadata", "recording_method"), 99)),
             "/metadata/recording_method"),
            # model order within record, not alphabetical: start_time before count
            (faulted(vector("steps-maximal"), (("record", "count"), "x"), (("record", "start_time"), "x")),
             "/record/start_time"),
            (faulted(vector("steps-maximal"), (("record", "count"), "x"), (("record", "end_zone_offset"), "x")),
             "/record/end_zone_offset"),
            # list elements by index, then field order within the element
            (faulted(vector("heart-rate-maximal"), (("record", "samples", 1, "time"), "x"),
                     (("record", "samples", 0, "beats_per_minute"), "x")),
             "/record/samples/0/beats_per_minute"),
            (faulted(vector("exercise-session-maximal"), (("record", "segments", 1, "segment_type"), 9999),
                     (("record", "segments", 0, "repetitions"), "x")),
             "/record/segments/0/repetitions"),
            # model order within a list element, not input order
            (faulted(vector("heart-rate-maximal"), (("record", "samples", 0, "beats_per_minute"), "x"),
                     (("record", "samples", 0, "time"), "x")),
             "/record/samples/0/time"),
            (faulted(vector("exercise-session-maximal"), (("record", "segments", 0, "repetitions"), "x"),
                     (("record", "segments", 0, "segment_type"), 9999)),
             "/record/segments/0/segment_type"),
            (faulted(vector("steps-maximal"), (("metadata", "device", "model"), 5), (("metadata", "device", "type"), 99)),
             "/metadata/device/type"),
            # depth first: a nested fault in an earlier field precedes a shallow fault in a later one
            (faulted(vector("exercise-session-maximal"), (("record", "planned_exercise_session_id"), 5),
                     (("record", "segments", 0, "segment_type"), 9999)),
             "/record/segments/0/segment_type"),
            (faulted(vector("sleep-session-maximal"), (("record", "stages", 1, "stage"), 99), (("record", "title"), 5)),
             "/record/title"),
        ]
        for projection, pointer in cases:
            with self.subTest(pointer):
                self.assertEqual(list(projection)[0], "record")
                self.assertEqual(run(projection), Quarantine("projection-schema-invalid", pointer))

    def test_validator_agrees_with_jsonschema_on_every_vector(self) -> None:
        for item in POSITIVE + NEGATIVE:
            if item["name"] in NEGATIVE_ENVELOPES:
                continue
            expected_valid = VALIDATORS[item["record_type"]].is_valid(item["projection"])
            outcome = run(encode(item["projection"]))
            self.assertEqual(isinstance(outcome, Unmodeled), expected_valid, item["name"])
        checked = 0
        for item in POSITIVE:
            validator = VALIDATORS[item["record_type"]]
            for path, mutated in single_faults(item["projection"]):
                where = (item["name"], pointer_of(path))
                errors = list(validator.iter_errors(mutated))
                outcome = run(encode(mutated))
                if not errors:
                    self.assertEqual(outcome, Unmodeled("record-type-not-modeled"), where)
                    continue
                checked += 1
                self.assertIsInstance(outcome, Quarantine, where)
                self.assertEqual(outcome.reason, "projection-schema-invalid", where)
                variants = [error for error in errors if error.validator == "oneOf"]
                if variants:
                    self.assertTrue(outcome.pointer.startswith(pointer_of(variants[0].absolute_path)), (where, outcome.pointer))
                else:
                    self.assertEqual(outcome.pointer, pointer_of(path), where)
        self.assertGreater(checked, 1000)


# ---- R6 ----

class R6Unmodeled(Checks):
    def test_unmapped_type_is_unmodeled(self) -> None:
        for projection in (steps(), weight(), sleep(), vector("exercise-session-maximal"), vector("heart-rate-minimal")):
            outcome = run(projection)
            self.assertEqual(outcome, Unmodeled("record-type-not-modeled", COMMON_VERSION, "none"))

    def test_unsupported_schema_version_is_unmodeled(self) -> None:
        processor = steps_processor(version="interpret-steps@3", supported=(2, 3))
        self.assertEqual(run(steps(), processor), Unmodeled("schema-version-not-modeled", "interpret-steps@3", "none"))
        self.assertEqual(processor.calls, [])
        self.assertEqual(run(steps(), steps_processor(supported=())), Unmodeled("schema-version-not-modeled", PROCESSOR_VERSION))
        self.assertIsInstance(run(steps(), steps_processor(supported=(2, 1))), CanonicalRevision)

    def test_unmodeled_needs_no_policy(self) -> None:
        for vector_ in POSITIVE:
            if not vector_["name"].endswith("-minimal"):
                continue
            data = vector_["bytes"].encode("utf-8")
            self.assertEqual(interpret(make_input(data), None, []), Unmodeled("record-type-not-modeled"), vector_["name"])
            processor = SyntheticProcessor(vector_["record_type"], supported=(7,))
            self.assertEqual(interpret(make_input(data), processor, []), Unmodeled("schema-version-not-modeled", PROCESSOR_VERSION),
                             vector_["name"])
        timeline = [policy("policy-alpha", None, "Asia/Kolkata")]
        self.assertEqual(run(steps(start_offset=None, end_offset=None), None, timeline), Unmodeled("record-type-not-modeled"))


# ---- R7 ----

class R7TemporalHeader(Checks):
    def test_instant_and_interval_shapes(self) -> None:
        for vector_ in POSITIVE:
            if not vector_["name"].endswith("-maximal"):
                continue
            record = vector_["projection"]["record"]
            shape = MODEL["records"][vector_["record_type"]]["shape"]
            outcome = interpret(make_input(vector_["bytes"].encode("utf-8")), SyntheticProcessor(vector_["record_type"]), [])
            self.assertIsInstance(outcome, CanonicalRevision, vector_["name"])
            temporal = outcome.header.time
            if shape == "instantaneous":
                self.assertEqual(temporal.shape, "instant", vector_["name"])
                self.assertIsNone(temporal.end, vector_["name"])
                self.assertEqual((temporal.start.instant.text, temporal.start.source_offset),
                                 (record["time"], record.get("zone_offset")), vector_["name"])
            else:
                self.assertEqual(temporal.shape, "interval", vector_["name"])
                self.assertEqual((temporal.start.instant.text, temporal.start.source_offset),
                                 (record["start_time"], record.get("start_zone_offset")), vector_["name"])
                self.assertEqual((temporal.end.instant.text, temporal.end.source_offset),
                                 (record["end_time"], record.get("end_zone_offset")), vector_["name"])
        series = self.revision(vector("heart-rate-maximal"), SyntheticProcessor("heart-rate"))
        self.assertEqual(series.header.time.shape, "interval")
        self.assertNotIn("samples", json.loads(series.header_bytes))
        self.assertNotIn("beats_per_minute", set(json_keys(json.loads(series.header_bytes))))
        instant_ = self.revision(weight(), SyntheticProcessor("weight"))
        self.assertEqual(instant_.header.time.shape, "instant")
        self.assertIsNone(instant_.header.time.end)

    def test_instants_keep_nanosecond_text_and_epoch(self) -> None:
        for start, end in (
            ("2026-01-15T07:30:00.123456789Z", "2026-01-15T08:00:00.120Z"),
            ("2026-01-15T07:30:00.000000001Z", "2026-01-15T07:30:00.000000002Z"),
            ("2000-02-29T23:45:00Z", "2000-03-01T00:15:00.000000Z"),
        ):
            with self.subTest(start=start, end=end):
                outcome = self.revision(steps(start, end))
                self.assertEqual(outcome.header.time.start.instant, Instant(start, epoch_ns(start)))
                self.assertEqual(outcome.header.time.end.instant, Instant(end, epoch_ns(end)))
                values = set(json_values(json.loads(outcome.header_bytes)))
                self.assertIn(start, values)
                self.assertIn(end, values)
        a = self.revision(steps("2026-01-15T07:30:00.000000001Z", "2026-01-15T08:00:00Z"))
        b = self.revision(steps("2026-01-15T07:30:00.000000002Z", "2026-01-15T08:00:00Z"))
        self.assertEqual(b.header.time.start.instant.epoch_ns - a.header.time.start.instant.epoch_ns, 1)
        self.assertNotEqual(a.revision_sha256, b.revision_sha256)
        c = self.revision(steps("2026-01-15T07:30:00.000Z", "2026-01-15T08:00:00Z"))
        d = self.revision(steps("2026-01-15T07:30:00Z", "2026-01-15T08:00:00Z"))
        self.assertEqual(c.header.time.start.instant.epoch_ns, d.header.time.start.instant.epoch_ns)
        self.assertNotEqual(c.header_bytes, d.header_bytes)

    def test_offsets_are_independent_per_endpoint(self) -> None:
        outcome = self.revision(steps(start_offset="+05:30", end_offset="-08:00"))
        self.assertEqual((outcome.header.time.start.source_offset, outcome.header.time.end.source_offset), ("+05:30", "-08:00"))
        self.assertEqual((outcome.header.time.start.applied_offset, outcome.header.time.end.applied_offset), ("+05:30", "-08:00"))
        zero = self.revision(steps(start_offset="+00:00", end_offset="-00:30"))
        self.assertEqual((zero.header.time.start.applied_offset, zero.header.time.end.applied_offset), ("+00:00", "-00:30"))
        mixed = self.revision(steps(start_offset=None, end_offset="+09:00"), steps_processor(), UTC_ALWAYS)
        self.assertEqual(mixed.header.time.start, Endpoint(instant("2026-01-15T07:30:00Z"), None, "+00:00", date(2026, 1, 15),
                                                           "inferred", "policy-utc"))
        self.assertEqual(mixed.header.time.end, Endpoint(instant("2026-01-15T08:00:00Z"), "+09:00", "+09:00", date(2026, 1, 15),
                                                         "experienced", None))
        point = self.revision(weight(offset="-03:30"), SyntheticProcessor("weight"))
        self.assertEqual(point.header.time.start.source_offset, "-03:30")
        self.assertEqual(point.header.time, TemporalHeader("instant", Endpoint(
            instant("2026-01-15T07:30:00.250Z"), "-03:30", "-03:30", date(2026, 1, 15), "experienced", None), None, True))

    def test_experienced_local_date(self) -> None:
        cases = [
            ("2026-01-15T18:29:59.999999999Z", "+05:30", date(2026, 1, 15)),
            ("2026-01-15T18:30:00Z", "+05:30", date(2026, 1, 16)),
            ("2026-01-15T07:59:59.999999999Z", "-08:00", date(2026, 1, 14)),
            ("2026-01-15T08:00:00Z", "-08:00", date(2026, 1, 15)),
            ("2026-01-15T10:14:59.999999999Z", "+13:45", date(2026, 1, 15)),
            ("2026-01-15T10:15:00Z", "+14:00", date(2026, 1, 16)),
            ("2028-02-28T23:30:00Z", "+01:00", date(2028, 2, 29)),
            ("2026-01-15T03:00:00Z", "-03:30", date(2026, 1, 14)),
        ]
        timeline = [policy("policy-alpha", None, "Pacific/Kiritimati")]
        for text, offset, expected in cases:
            with self.subTest(text=text, offset=offset):
                self.assertEqual(local_date(text, offset), expected)
                outcome = self.revision(weight(text, offset=offset), SyntheticProcessor("weight"), timeline)
                self.assertEqual(outcome.header.time.start,
                                 Endpoint(instant(text), offset, offset, expected, "experienced", None))
                self.assertEqual(outcome.policy_identity, "none")

    def test_explicit_time_flag(self) -> None:
        explicit = vector("planned-exercise-session-maximal")
        explicit["record"]["has_explicit_time"] = True
        date_only = vector("planned-exercise-session-maximal")
        date_only["record"]["has_explicit_time"] = False
        processor = SyntheticProcessor("planned-exercise-session")
        a = self.revision(explicit, processor)
        b = self.revision(date_only, processor)
        self.assertIs(a.header.time.explicit_time, True)
        self.assertIs(b.header.time.explicit_time, False)
        self.assertNotEqual(a.header_bytes, b.header_bytes)
        self.assertTrue(any(value is False for value in json_values(json.loads(b.header_bytes))))
        self.assertIs(self.revision(steps()).header.time.explicit_time, True)
        self.assertIs(self.revision(weight(), SyntheticProcessor("weight")).header.time.explicit_time, True)
        self.assertIs(self.revision(vector("sleep-session-maximal"), SyntheticProcessor("sleep-session")).header.time.explicit_time, True)

    def test_reversed_interval_is_quarantined(self) -> None:
        processor = steps_processor()
        reversed_ = steps("2026-01-15T07:30:00.000000002Z", "2026-01-15T07:30:00.000000001Z")
        self.assertEqual(run(reversed_, processor), Quarantine("interval-reversed", "/record/end_time", PROCESSOR_VERSION, "none"))
        self.assertEqual(run(steps("2026-01-15T08:00:00Z", "2026-01-15T07:30:00Z", start_offset="+05:30", end_offset="-08:00"),
                             processor, UTC_ALWAYS),
                         Quarantine("interval-reversed", "/record/end_time", PROCESSOR_VERSION, "none"))
        self.assertEqual(processor.calls, [])
        equal = self.revision(steps("2026-01-15T07:30:00.500Z", "2026-01-15T07:30:00.500000Z"), processor)
        self.assertEqual(equal.header.time.start.instant.epoch_ns, equal.header.time.end.instant.epoch_ns)
        self.assertEqual(len(processor.calls), 1)
        # The interval check compares instants, not local wall-clock times.
        across = self.revision(steps("2026-01-15T07:30:00Z", "2026-01-15T07:40:00Z", start_offset="+05:30", end_offset="-08:00"))
        self.assertGreater(across.header.time.end.instant.epoch_ns, across.header.time.start.instant.epoch_ns)


    def test_out_of_range_instants_are_quarantined(self) -> None:
        """Operator decision, 2026-11-24: instants and derived local dates stay within years 1 to 9999."""
        out = "instant-out-of-range"
        weigh = SyntheticProcessor("weight")
        # A header instant in year 0000 is out of range before Unmodeled, whatever its offset.
        for text, offset in (("0000-02-29T07:30:00Z", "+05:30"), ("0000-12-31T20:00:00Z", "+05:30"), ("0000-01-01T00:00:00Z", None)):
            with self.subTest(text=text, offset=offset):
                self.assertEqual(run(weight(text, offset=offset), weigh, UTC_ALWAYS), Quarantine(out, "/record/time", PROCESSOR_VERSION, "none"))
                self.assertEqual(run(weight(text, offset=offset)), Quarantine(out, "/record/time"))
        self.assertEqual(run(steps("0000-12-31T23:00:00Z", "0001-01-01T01:00:00Z")), Quarantine(out, "/record/start_time"))
        # It precedes interval-reversed.
        self.assertEqual(run(steps("0001-01-01T00:00:00Z", "0000-12-31T23:00:00Z"), steps_processor()),
                         Quarantine(out, "/record/end_time", PROCESSOR_VERSION, "none"))
        # A derived local date outside years 1 to 9999, experienced or inferred.
        experienced = [
            ("0001-01-01T00:00:00Z", "-00:01", None),
            ("0001-01-01T00:00:59.999999999Z", "-00:01", None),
            ("0001-01-01T00:01:00Z", "-00:01", date(1, 1, 1)),
            ("0001-01-01T00:00:00Z", "+00:00", date(1, 1, 1)),
            ("9999-12-31T23:00:00Z", "+01:00", None),
            ("9999-12-31T22:59:59.999999999Z", "+01:00", date(9999, 12, 31)),
            ("9999-12-31T23:59:59.999999999Z", "-00:01", date(9999, 12, 31)),
            ("9999-12-31T23:59:59.999999999Z", "+00:00", date(9999, 12, 31)),
        ]
        for text, offset, expected in experienced:
            with self.subTest(text=text, offset=offset):
                processor = SyntheticProcessor("weight")
                if expected is None:
                    self.assertEqual(run(weight(text, offset=offset), processor), Quarantine(out, "/record/time", PROCESSOR_VERSION, "none"))
                    self.assertEqual(processor.calls, [])
                    # Unmodeled derives no local date.
                    self.assertEqual(run(weight(text, offset=offset)), Unmodeled("record-type-not-modeled"))
                else:
                    outcome = self.revision(weight(text, offset=offset), processor)
                    self.assertEqual(outcome.header.time.start, Endpoint(instant(text), offset, offset, expected, "experienced", None))
        inferred = [
            ("0001-01-01T00:00:00Z", NEW_YORK, None),
            ("0001-01-01T00:00:00Z", "UTC", date(1, 1, 1)),
            ("9999-12-31T23:00:00Z", "Asia/Tokyo", None),
            ("9999-12-31T14:59:59.999999999Z", "Asia/Tokyo", date(9999, 12, 31)),
            ("9999-12-31T23:00:00Z", "UTC", date(9999, 12, 31)),
        ]
        for text, zone, expected in inferred:
            with self.subTest(text=text, zone=zone):
                timeline = [policy("policy-alpha", None, zone)]
                outcome = run(weight(text, offset=None), SyntheticProcessor("weight"), timeline)
                if expected is None:
                    self.assertIsInstance(outcome, Quarantine)
                    self.assertEqual((outcome.reason, outcome.pointer, outcome.processor_version), (out, "/record/time", PROCESSOR_VERSION))
                else:
                    self.assertIsInstance(outcome, CanonicalRevision)
                    self.assertEqual((outcome.header.time.start.local_date, outcome.header.time.start.attribution), (expected, "inferred"))
        # Per endpoint, and before Policy-Pending.
        self.assertEqual(run(steps("9999-12-31T20:00:00Z", "9999-12-31T21:00:00Z", start_offset="-05:00", end_offset="+05:30"),
                             steps_processor()), Quarantine(out, "/record/end_time", PROCESSOR_VERSION, "none"))
        self.assertEqual(run(steps("9999-12-31T20:00:00Z", "9999-12-31T21:00:00Z", start_offset="+05:30", end_offset=None),
                             steps_processor()), Quarantine(out, "/record/start_time", PROCESSOR_VERSION, "none"))
        self.assertEqual(run(steps("9999-12-31T20:00:00Z", "9999-12-31T21:00:00Z", start_offset=None, end_offset="+05:30"),
                             steps_processor()), Quarantine(out, "/record/end_time", PROCESSOR_VERSION, "none"))
        # The envelope's modification time: decode_envelope quarantines it, so interpret refuses the identity.
        for text in ("0000-01-01T00:00:00Z", "0000-02-29T09:00:00.123456789Z"):
            with self.subTest(last_modified_time=text):
                self.assertEqual(decode_envelope(encode(steps(lmt=text)), "steps", 1), Quarantine(out, "/metadata/last_modified_time"))
                self.assertEqual(self.refused(steps(lmt=text)), "projection-identity-mismatch")
        for text, expected in (("0001-01-01T00:00:00Z", datetime(1, 1, 1, tzinfo=UTC)),
                               ("9999-12-31T23:59:59.999999999Z", datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC))):
            with self.subTest(last_modified_time=text):
                decoded = decode_envelope(encode(steps(lmt=text)), "steps", 1)
                self.assertEqual((decoded.last_modified_time, decoded.last_modified_instant), (expected, Instant(text, epoch_ns(text))))


# ---- R8 ----

NEW_YORK = "America/New_York"


class R8FallbackTimezoneAttribution(Checks):
    def test_inferred_local_date_uses_applicable_policy(self) -> None:
        cases = [
            ("2026-03-08T04:30:00Z", NEW_YORK, "-05:00", date(2026, 3, 7)),
            ("2026-03-08T06:59:59.999999999Z", NEW_YORK, "-05:00", date(2026, 3, 8)),
            ("2026-03-08T07:00:00Z", NEW_YORK, "-04:00", date(2026, 3, 8)),
            ("2026-11-01T04:30:00Z", NEW_YORK, "-04:00", date(2026, 11, 1)),
            ("2026-11-01T06:00:00Z", NEW_YORK, "-05:00", date(2026, 11, 1)),
            ("2026-01-15T02:00:00Z", "America/St_Johns", "-03:30", date(2026, 1, 14)),
            ("2026-01-15T18:10:00Z", "Asia/Kathmandu", "+05:45", date(2026, 1, 15)),
            ("2026-01-15T18:20:00.123456789Z", "Asia/Kathmandu", "+05:45", date(2026, 1, 16)),
            ("2026-01-15T10:00:00Z", "UTC", "+00:00", date(2026, 1, 15)),
        ]
        for text, zone, offset, expected in cases:
            with self.subTest(text=text, zone=zone):
                timeline = [policy("policy-alpha", None, zone)]
                outcome = self.revision(weight(text, offset=None), SyntheticProcessor("weight"), timeline)
                self.assertEqual(outcome.header.time.start, Endpoint(instant(text), None, offset, expected, "inferred", "policy-alpha"))
                values = set(json_values(json.loads(outcome.header_bytes)))
                for value in (text, offset, expected.isoformat(), "inferred", "policy-alpha"):
                    self.assertIn(value, values)
        interval = self.revision(steps("2026-03-08T04:30:00Z", "2026-03-08T07:30:00Z", start_offset=None, end_offset=None),
                                 steps_processor(), [policy("policy-alpha", None, NEW_YORK)])
        self.assertEqual((interval.header.time.start.applied_offset, interval.header.time.start.local_date),
                         ("-05:00", date(2026, 3, 7)))
        self.assertEqual((interval.header.time.end.applied_offset, interval.header.time.end.local_date),
                         ("-04:00", date(2026, 3, 8)))

    def test_inferred_offset_keeps_seconds(self) -> None:
        """Operator decision, 2026-11-24: an inferred offset keeps a nonzero seconds part, and the local date uses it."""
        cases = [
            ("1800-01-02T04:56:01Z", NEW_YORK, "-04:56:02", date(1800, 1, 1)),
            ("1800-01-02T04:56:02Z", NEW_YORK, "-04:56:02", date(1800, 1, 2)),
            ("1800-01-02T04:56:01.999999999Z", NEW_YORK, "-04:56:02", date(1800, 1, 1)),
            ("1800-01-01T18:06:31Z", "Asia/Kolkata", "+05:53:28", date(1800, 1, 1)),
            ("1800-01-01T18:06:32Z", "Asia/Kolkata", "+05:53:28", date(1800, 1, 2)),
            ("1900-01-01T12:00:00Z", "Asia/Kolkata", "+05:21:10", date(1900, 1, 1)),
            ("2026-01-15T12:00:00Z", NEW_YORK, "-05:00", date(2026, 1, 15)),
        ]
        for text, zone, offset, expected in cases:
            with self.subTest(text=text, zone=zone):
                outcome = self.revision(weight(text, offset=None), SyntheticProcessor("weight"), [policy("policy-alpha", None, zone)])
                self.assertEqual(outcome.header.time.start, Endpoint(instant(text), None, offset, expected, "inferred", "policy-alpha"))
                self.assertEqual(json.loads(outcome.header_bytes)["time"]["start"]["applied_offset"], offset)


    def test_policy_boundary_is_inclusive(self) -> None:
        boundary = datetime(2026, 6, 1, tzinfo=UTC)
        for effective in (boundary, boundary.astimezone(IST)):
            timeline = [policy("policy-zulu", None, "UTC"), policy("policy-alpha", effective, "Asia/Kolkata")]
            for text, expected in (
                ("2026-06-01T00:00:00Z", "policy-alpha"),
                ("2026-06-01T00:00:00.000000Z", "policy-alpha"),
                ("2026-06-01T00:00:00.000000001Z", "policy-alpha"),
                ("2026-05-31T23:59:59.999999999Z", "policy-zulu"),
            ):
                with self.subTest(text=text, effective=effective.isoformat()):
                    outcome = self.revision(weight(text, offset=None), SyntheticProcessor("weight"), timeline)
                    self.assertEqual(outcome.header.time.start.policy_id, expected)
        later = [policy("policy-alpha", datetime(2026, 6, 1, 0, 0, 0, 1, tzinfo=UTC), "Asia/Kolkata")]
        self.assertEqual(run(weight("2026-06-01T00:00:00.000000999Z", offset=None), SyntheticProcessor("weight"), later),
                         PolicyPending("fallback-timezone", "fallback-timezone-policy-missing", PROCESSOR_VERSION))
        self.assertEqual(self.revision(weight("2026-06-01T00:00:00.000001Z", offset=None), SyntheticProcessor("weight"),
                                       later).header.time.start.policy_id, "policy-alpha")

    def test_explicit_offset_overrides_policy(self) -> None:
        timeline = [policy("policy-zulu", None, "Pacific/Kiritimati"), policy("policy-alpha", datetime(2026, 1, 15, 7, 45, tzinfo=UTC), NEW_YORK)]
        alone = self.revision(steps(), steps_processor(), [])
        with_policies = self.revision(steps(), steps_processor(), timeline)
        self.assertEqual(with_policies.header.time, alone.header.time)
        self.assertEqual(with_policies.header_bytes, alone.header_bytes)
        self.assertEqual(with_policies.policy_identity, "none")
        self.assertEqual(with_policies.header.time.start.attribution, "experienced")
        mixed = self.revision(steps(end_offset=None), steps_processor(), timeline)
        self.assertEqual(mixed.header.time.start, Endpoint(instant("2026-01-15T07:30:00Z"), "+05:30", "+05:30",
                                                           date(2026, 1, 15), "experienced", None))
        self.assertEqual(mixed.header.time.end, Endpoint(instant("2026-01-15T08:00:00Z"), None, "-05:00",
                                                         date(2026, 1, 15), "inferred", "policy-alpha"))

    def test_missing_policy_is_policy_pending(self) -> None:
        pending = PolicyPending("fallback-timezone", "fallback-timezone-policy-missing", PROCESSOR_VERSION, "none")
        for timeline in ([], [policy("policy-alpha", datetime(2026, 1, 16, tzinfo=UTC), "UTC")]):
            processor = SyntheticProcessor("weight")
            self.assertEqual(run(weight(offset=None), processor, timeline), pending)
            self.assertEqual(processor.calls, [])
        for vector_ in POSITIVE:
            if vector_["name"].endswith("-minimal"):
                self.assertEqual(interpret(make_input(vector_["bytes"].encode()), SyntheticProcessor(vector_["record_type"]), []),
                                 pending, vector_["name"])

    def test_one_missing_endpoint_is_policy_pending(self) -> None:
        processor = steps_processor()
        expected = ("fallback-timezone", "fallback-timezone-policy-missing", PROCESSOR_VERSION)
        outcome = run(steps(start_offset="+05:30", end_offset=None), processor, [])
        self.assertEqual(outcome, PolicyPending(*expected, "none"))
        outcome = run(steps(start_offset=None, end_offset="+05:30"), processor, [])
        self.assertEqual(outcome, PolicyPending(*expected, "none"))
        between = [policy("policy-alpha", datetime(2026, 1, 15, 7, 45, tzinfo=UTC), "UTC")]
        outcome = run(steps(start_offset=None, end_offset=None), processor, between)
        self.assertIsInstance(outcome, PolicyPending)
        self.assertEqual((outcome.policy_kind, outcome.reason, outcome.processor_version), expected)
        self.assertEqual(processor.calls, [])

    def test_policy_identity_names_only_policies_used(self) -> None:
        split = datetime(2026, 1, 15, 7, 45, tzinfo=UTC)
        unused = policy("policy-kilo-unused", datetime(2027, 1, 1, tzinfo=UTC), "Asia/Kolkata")
        timeline = [policy("policy-zulu", None, "UTC"), policy("policy-alpha", split, NEW_YORK)]
        bare = steps(start_offset=None, end_offset=None)
        both = self.revision(bare, steps_processor(), timeline)
        identity = both.policy_identity
        self.assertTrue(identity.startswith("fallback-timezone:"), identity)
        self.assertIn(tzdata.IANA_VERSION, identity)
        self.assertLess(identity.index("policy-alpha"), identity.index("policy-zulu"))
        self.assertEqual((identity.count("policy-alpha"), identity.count("policy-zulu")), (1, 1))
        self.assertEqual(json.loads(both.header_bytes)["policy_identity"], identity)
        self.assertEqual((both.header.policy_identity, both.processor_version), (identity, PROCESSOR_VERSION))
        self.assertEqual(self.revision(bare, steps_processor(), timeline + [unused]).header_bytes, both.header_bytes)
        only_end = self.revision(steps(end_offset=None), steps_processor(), timeline)
        self.assertIn("policy-alpha", only_end.policy_identity)
        self.assertNotIn("policy-zulu", only_end.policy_identity)
        self.assertNotEqual(only_end.policy_identity, identity)
        only_start = self.revision(steps(start_offset=None), steps_processor(), timeline)
        self.assertIn("policy-zulu", only_start.policy_identity)
        self.assertNotIn("policy-alpha", only_start.policy_identity)
        shared = self.revision(bare, steps_processor(), [policy("policy-zulu", None, "UTC")])
        self.assertEqual(shared.policy_identity.count("policy-zulu"), 1)
        self.assertNotEqual(shared.policy_identity, identity)
        rezoned = self.revision(bare, steps_processor(), [policy("policy-zulu", None, "UTC"), policy("policy-alpha", split, "Asia/Tokyo")])
        self.assertNotEqual(rezoned.header_bytes, both.header_bytes)
        experienced = self.revision(steps(), steps_processor(), timeline + [unused])
        self.assertEqual(experienced.policy_identity, "none")
        self.assertEqual(experienced.header_bytes, self.revision(steps(), steps_processor(), []).header_bytes)

    def test_zones_load_from_pinned_tzdata(self) -> None:
        planted = Path(tempfile.mkdtemp(prefix="isp-host-tz-"))
        self.addCleanup(shutil.rmtree, planted, True)
        fake = (resources.files("tzdata") / "zoneinfo" / "Etc" / "GMT-3").read_bytes()
        for name in ("America/New_York", "Asia/Kathmandu", "Synthetic/Planted", "UTC"):
            (planted / name).parent.mkdir(parents=True, exist_ok=True)
            (planted / name).write_bytes(fake)
        code = r"""
import json, zoneinfo
from datetime import datetime, timezone
assert zoneinfo.ZoneInfo("America/New_York").utcoffset(datetime(2026, 3, 8)).total_seconds() == 10800
assert zoneinfo.ZoneInfo("Synthetic/Planted") is not None
import test_interpret_source_projection_1 as t
from activity_tracker_portal.interpret_source_projection import InterpretFailure
out = {}
for zone in ("America/New_York", "Asia/Kathmandu", "UTC"):
    start = t.run(t.weight("2026-03-08T04:30:00Z", offset=None), t.SyntheticProcessor("weight"),
                  [t.policy("policy-alpha", None, zone)]).header.time.start
    out[zone] = [start.applied_offset, start.local_date.isoformat()]
try:
    t.run(t.weight(offset=None), t.SyntheticProcessor("weight"), [t.policy("policy-alpha", None, "Synthetic/Planted")])
    out["planted"] = "accepted"
except InterpretFailure as failure:
    out["planted"] = failure.failure
print(json.dumps(out))
"""
        result = json.loads(run_python(code, {"PYTHONTZPATH": str(planted), "TZ": "America/New_York"}))
        self.assertEqual(result, {
            "America/New_York": ["-05:00", "2026-03-07"],
            "Asia/Kathmandu": ["+05:45", "2026-03-08"],
            "UTC": ["+00:00", "2026-03-08"],
            "planted": "processing-input-invalid",
        })
        outcome = self.revision(weight(offset=None), SyntheticProcessor("weight"), UTC_ALWAYS)
        self.assertIn(tzdata.IANA_VERSION, outcome.policy_identity)


# ---- R9 ----

class R9ProcessorPlugIn(Checks):
    def test_processor_receives_record_and_header(self) -> None:
        projection = steps("2026-01-15T07:30:00.123456789Z", "2026-01-15T08:00:00Z", end_offset=None)
        processor = steps_processor()
        outcome = self.revision(projection, processor, UTC_ALWAYS)
        self.assertEqual(len(processor.calls), 1)
        record, temporal = processor.calls[0]
        self.assertEqual(record, projection["record"])
        self.assertIsInstance(temporal, TemporalHeader)
        self.assertEqual(temporal, outcome.header.time)
        self.assertEqual(temporal.end.attribution, "inferred")
        self.assertEqual(outcome.body_bytes, b'{"count":4321}')
        series = SyntheticProcessor("heart-rate")
        self.revision(vector("heart-rate-maximal"), series)
        self.assertEqual(series.calls[0][0], vector("heart-rate-maximal")["record"])

    def test_processor_quarantine_uses_declared_reason(self) -> None:
        timeline = [policy("policy-alpha", None, "Asia/Kolkata")]
        processor = steps_processor(version="interpret-steps@2", reasons=("synthetic-count-negative", "synthetic-other"),
                                    body=lambda record, temporal: Quarantine("synthetic-count-negative", "/record/count"))
        experienced = run(steps(count=-5), processor)
        self.assertEqual(experienced, Quarantine("synthetic-count-negative", "/record/count", "interpret-steps@2", "none"))
        inferred = run(steps(count=-5, start_offset=None, end_offset=None), processor, timeline)
        self.assertEqual((inferred.reason, inferred.pointer, inferred.processor_version),
                         ("synthetic-count-negative", "/record/count", "interpret-steps@2"))
        revision = self.revision(steps(start_offset=None, end_offset=None), steps_processor(), timeline)
        self.assertEqual(inferred.policy_identity, revision.policy_identity)
        self.assertNotEqual(inferred.policy_identity, "none")

    def test_processor_pointer_must_name_a_schema_field(self) -> None:
        """The common layer checks a processor's pointer for grammar and for membership in the source schema."""
        value_marker = "syntheticvaluemarker"

        def quarantining(record_type, pointer):
            return SyntheticProcessor(record_type, reasons=("synthetic-refusal",), body=lambda record, temporal: Quarantine(
                "synthetic-refusal", pointer(record) if callable(pointer) else pointer))

        night = sleep(title=value_marker)
        accepted = [
            ("sleep-session", night, ""), ("sleep-session", night, "/record"), ("sleep-session", night, "/record/title"),
            ("sleep-session", night, "/record/stages/1/stage"), ("sleep-session", night, "/record/stages/0"),
            ("sleep-session", night, "/metadata/device/model"), ("sleep-session", night, "/record_type"),
            # declared by the schema though absent from this projection
            ("steps", steps(device=False), "/metadata/device/manufacturer"),
            # declared by one variant of a oneOf
            ("exercise-session", vector("exercise-session-maximal"), "/record/exercise_route_result/kind"),
        ]
        for record_type, projection, pointer in accepted:
            with self.subTest("accepted", pointer=pointer):
                self.assertEqual(run(projection, quarantining(record_type, pointer)),
                                 Quarantine("synthetic-refusal", pointer, PROCESSOR_VERSION, "none"))
        rejected = [
            lambda record: "/record/" + record["title"],
            lambda record: "/record/title/" + record["title"],
            lambda record: "/record/" + record["notes"],
            "/record/notes/x", "record/title", "/record/stages/01/stage", "/record/stages/-1/stage", "/record/stages/x",
            "/record/stages/1/", "/record/Title", "/record/title~1", "/record//title", "/records", " /record/title",
            "/metadata/client_record_id/x", "/record/stage", None, 5,
        ]
        for index, pointer in enumerate(rejected):
            with self.subTest("rejected", case=index):
                with self.assertRaises(InterpretFailure) as caught:
                    run(night, quarantining("sleep-session", pointer))
                failure = caught.exception
                self.assertEqual(failure.failure, "processor-invariant-violated")
                for text in (str(failure), repr(failure), repr(vars(failure)), "".join(traceback.format_exception(failure))):
                    self.assertNotIn(value_marker, text)
                    self.assert_health_free(text)


    def test_undeclared_reason_violates_invariant(self) -> None:
        for reasons, returned in (
            ((), "synthetic-count-negative"),
            (("synthetic-other",), "synthetic-count-negative"),
            (("interval-reversed",), "interval-reversed"),
            (("projection-schema-invalid",), "projection-schema-invalid"),
            (("record-type-not-modeled",), "record-type-not-modeled"),
            (("fallback-timezone-policy-missing",), "fallback-timezone-policy-missing"),
            (("instant-out-of-range",), "instant-out-of-range"),
            (("Synthetic Reason",), "Synthetic Reason"),
            (("synthetic_reason",), "synthetic_reason"),
            (("",), ""),
        ):
            with self.subTest(returned=returned, declared=reasons):
                processor = steps_processor(reasons=reasons, body=lambda record, temporal, r=returned: Quarantine(r, "/record/count"))
                self.assertEqual(self.refused(steps(), processor), "processor-invariant-violated")

    def test_undeclared_numeric_field_violates_invariant(self) -> None:
        samples = {"samples": [{"bpm": 60, "label": "a"}, {"bpm": 61, "flag": True}], "count": 3}
        wildcard = (NumericField("/samples/*/bpm", "integer", "1/min"), NumericField("/count", "integer", "count"))
        self.assertIsInstance(run(steps(), body_processor(samples, wildcard)), CanonicalRevision)
        cases = {
            "undeclared top-level number": ({"count": 3, "extra": 1}, STEPS_FIELDS),
            "undeclared nested number": ({"count": 3, "nested": {"count": 1}}, STEPS_FIELDS),
            "undeclared list number": ({"count": 3, "values": [1]}, STEPS_FIELDS),
            "number matching no wildcard": ({"samples": [{"bpm": 60, "other": 2}], "count": 3}, wildcard),
            "two declarations match one number": ({"count": 3}, STEPS_FIELDS + (NumericField("/count", "integer", "count"),)),
            "wildcard and index both match": (samples, wildcard + (NumericField("/samples/0/bpm", "integer", "1/min"),)),
            "wildcard is not a key": ({"samples": {"x": {"bpm": 60}}, "count": 3}, wildcard),
            "pointer does not match a prefix": ({"count": 3, "counter": 4}, STEPS_FIELDS),
            "body is not a mapping": ([3], STEPS_FIELDS),
            "null value": ({"count": 3, "label": None}, STEPS_FIELDS),
            "non-string key": ({"count": 3, 7: "x"}, STEPS_FIELDS),
            "bytes value": ({"count": 3, "label": b"a"}, STEPS_FIELDS),
            "date value": ({"count": 3, "label": date(2026, 1, 15)}, STEPS_FIELDS),
        }
        for name, (body, fields) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.refused(steps(), body_processor(body, fields)), "processor-invariant-violated")
        self.assertEqual(self.refused(steps(), steps_processor(body=lambda record, temporal: None)), "processor-invariant-violated")

    def test_integer_and_float_kinds_are_enforced(self) -> None:
        def body(value, kind, unit):
            return body_processor({"v": value}, (NumericField("/v", kind, unit),))

        safe = 2**53 - 1
        accepted = [(5, "integer", "count"), (-0, "integer", "count"), (72500.0, "float", "g"), (0.5, "float", "percent"),
                    (2, "code", None), (-1.5, "float", "degC"), (safe, "integer", "count"), (-safe, "integer", "count"),
                    (safe, "code", None), (1e16, "float", "g"), (9007199254740994.0, "float", "g"), (-1e300, "float", "g")]
        for value, kind, unit in accepted:
            with self.subTest("accepted", value=value, kind=kind):
                self.assertIsInstance(run(steps(), body(value, kind, unit)), CanonicalRevision)
        rejected = [(True, "integer", "count"), (5.0, "integer", "count"), (5, "float", "g"), (False, "float", "g"),
                    (math.nan, "float", "g"), (math.inf, "float", "g"), (-math.inf, "float", "g"),
                    (True, "code", None), (2.0, "code", None), ("5", "integer", "count"), (5, "count", "count"),
                    (safe + 1, "integer", "count"), (safe + 2, "integer", "count"), (-(safe + 1), "integer", "count"),
                    (-(safe + 2), "integer", "count"), (2**64, "integer", "count"), (safe + 2, "code", None)]
        for value, kind, unit in rejected:
            with self.subTest("rejected", value=value, kind=kind):
                self.assertEqual(self.refused(steps(), body(value, kind, unit)), "processor-invariant-violated")
        integer = self.revision(steps(), body(72500, "integer", "g"))
        floating = self.revision(steps(), body(72500.0, "float", "g"))
        self.assertEqual((integer.body_bytes, floating.body_bytes), (b'{"v":72500}', b'{"v":72500}'))
        boundary = self.revision(steps(), body_processor({"v": [safe, -safe]}, (NumericField("/v/*", "integer", "count"),)))
        self.assertEqual(boundary.body_bytes, b'{"v":[9007199254740991,-9007199254740991]}')
        wide_float = self.revision(steps(), body(1e16, "float", "g"))
        self.assertEqual(wide_float.body_bytes, b'{"v":10000000000000000}')
        self.assertTrue(is_canonical(wide_float.body_bytes))
        self.assertEqual(self.refused(steps(), body_processor({"v": [1, safe + 2]}, (NumericField("/v/*", "integer", "count"),))),
                         "processor-invariant-violated")
        self.assertIsInstance(run(steps(), body_processor({"flag": True, "v": 1}, (NumericField("/v", "integer", "count"),))),
                              CanonicalRevision)

    def test_unknown_unit_violates_invariant(self) -> None:
        self.assertEqual(set(UNITS), {"g", "cal", "m", "L", "m/s", "W", "mmHg", "degC", "mmol/L", "percent",
                                      "count", "1/min", "ns", "mL/(kg*min)"})
        for unit in sorted(UNITS):
            with self.subTest(unit=unit):
                self.assertIsInstance(run(steps(), body_processor({"v": 1.5}, (NumericField("/v", "float", unit),))),
                                      CanonicalRevision)
                self.assertIsInstance(run(steps(), body_processor({"v": 2}, (NumericField("/v", "integer", unit),))),
                                      CanonicalRevision)
        for value, kind, unit in ((1.5, "float", "kcal"), (1.5, "float", "kg"), (2, "integer", "bpm"), (2, "integer", None),
                                  (1.5, "float", None), (2, "integer", ""), (2, "code", "count"), (2, "integer", "G")):
            with self.subTest(kind=kind, unit=unit):
                self.assertEqual(self.refused(steps(), body_processor({"v": value}, (NumericField("/v", kind, unit),))),
                                 "processor-invariant-violated")

    def test_body_bytes_are_canonical(self) -> None:
        body = {"z": 1, "a": [1e21, 1e-7, 0.1, 72500.0, -0.0, 5e-324, 1.5, 1e16, 123456789.125], "é": "é中\u0001\"\\\n",
                "\U0001F600": True, "Ａ": False, "count": 4321, "nested": {"b": [], "a": {}}}
        fields = (NumericField("/z", "integer", "count"), NumericField("/a/*", "float", "g"),
                  NumericField("/count", "integer", "count"))
        outcome = self.revision(steps(), body_processor(body, fields))
        expected = ('{"a":[1e+21,1e-7,0.1,72500,0,5e-324,1.5,10000000000000000,123456789.125],"count":4321,'
                    '"nested":{"a":{},"b":[]},"z":1,"é":"é中\\u0001\\"\\\\\\n","\U0001F600":true,"Ａ":false}').encode("utf-8")
        self.assertEqual(outcome.body_bytes, expected)
        self.assertEqual(canonical(body).encode("utf-8"), expected)
        self.assertEqual(outcome.body_sha256, sha(expected))
        self.assertTrue(is_canonical(outcome.header_bytes))
        self.assertTrue(is_canonical(outcome.body_bytes))
        self.assertFalse(is_canonical(b'{"v":9007199254740993}'))
        self.assertFalse(outcome.body_bytes.startswith(b"\xef\xbb\xbf"))

    def test_processor_exception_is_an_invariant_violation(self) -> None:
        def explode(record, temporal):
            raise ValueError(f"{HEALTH_MARKER} {record['title']}")

        for error in (ValueError, KeyError, ZeroDivisionError, RecursionError):
            with self.subTest(error.__name__):
                processor = SyntheticProcessor("sleep-session", body=lambda record, temporal, e=error: (_ for _ in ()).throw(
                    e(f"{HEALTH_MARKER} {record['title']}")))
                self.assertEqual(self.refused(sleep(), processor), "processor-invariant-violated")
        self.assertEqual(self.refused(sleep(), SyntheticProcessor("sleep-session", body=explode)), "processor-invariant-violated")


# ---- R10 ----

class R10RevisionHeaderAndIdentity(Checks):
    def test_header_carries_identity_provenance_and_versions(self) -> None:
        projection = steps("2026-01-15T07:30:00.123456789Z", "2026-01-15T08:00:00Z", start_offset="+05:30", end_offset=None)
        processor = steps_processor(version="interpret-steps@4", body_version=3)
        outcome = self.revision(projection, processor, UTC_ALWAYS, record_id=501, selection_id=502, evidence_id=503)
        data = encode(projection)
        temporal = TemporalHeader("interval",
                                  Endpoint(instant("2026-01-15T07:30:00.123456789Z"), "+05:30", "+05:30", date(2026, 1, 15), "experienced", None),
                                  Endpoint(instant("2026-01-15T08:00:00Z"), None, "+00:00", date(2026, 1, 15), "inferred", "policy-utc"),
                                  True)
        self.assertEqual(outcome.header, RevisionHeader(
            source_system_kind=HC, origin=ORIGIN, record_type="steps", source_identity=SOURCE_IDENTITY, record_id=501,
            selection_id=502, evidence_id=503, projection_sha256=sha(data), projection_schema_version=1,
            projection_implementation_version="0.1.0-synthetic", sdk_version=SDK, provider_version="synthetic-provider-1",
            last_modified_time=instant(LMT), recording_method=2,
            device={"type": 1, "manufacturer": "Synthetic Devices", "model": "SD-1"}, time=temporal,
            header_version=COMMON_VERSION, processor_version="interpret-steps@4", body_schema=processor.body_schema,
            policy_identity=outcome.policy_identity, body_sha256=sha(b'{"count":4321}')))
        self.assertTrue(is_canonical(outcome.header_bytes))
        header = json.loads(outcome.header_bytes)
        self.assertEqual(set(header), R10_KEYS)
        self.assertEqual({key: header[key] for key in R10_KEYS - {"time"}}, {
            "source_system_kind": HC, "origin": ORIGIN, "record_type": "steps", "source_identity": SOURCE_IDENTITY,
            "record_id": 501, "selection_id": 502, "evidence_id": 503, "projection_sha256": sha(data),
            "projection_schema_version": 1, "projection_implementation_version": "0.1.0-synthetic", "sdk_version": SDK,
            "provider_version": "synthetic-provider-1", "last_modified_time": LMT, "recording_method": 2,
            "device": {"type": 1, "manufacturer": "Synthetic Devices", "model": "SD-1"}, "header_version": COMMON_VERSION,
            "processor_version": "interpret-steps@4", "body_schema": {"record_type": "steps", "body_version": 3},
            "policy_identity": outcome.policy_identity, "body_sha256": sha(b'{"count":4321}'),
        })
        self.assertEqual(header["time"], {
            "shape": "interval", "explicit_time": True,
            "start": {"instant": "2026-01-15T07:30:00.123456789Z", "source_offset": "+05:30", "applied_offset": "+05:30",
                      "local_date": "2026-01-15", "attribution": "experienced"},
            "end": {"instant": "2026-01-15T08:00:00Z", "applied_offset": "+00:00", "local_date": "2026-01-15",
                    "attribution": "inferred", "policy_id": "policy-utc"},
        })
        # Endpoints whose instants, dates, offsets, and policies all differ cannot be swapped unnoticed.
        split = datetime(2026, 3, 8, 12, 0, tzinfo=UTC)
        apart = self.revision(steps("2026-03-08T04:30:00Z", "2026-03-08T20:00:00Z", start_offset=None, end_offset=None),
                              steps_processor(), [policy("policy-zulu", None, "America/New_York"),
                                                  policy("policy-alpha", split, "Asia/Kolkata")])
        self.assertEqual(json.loads(apart.header_bytes)["time"], {
            "shape": "interval", "explicit_time": True,
            "start": {"instant": "2026-03-08T04:30:00Z", "applied_offset": "-05:00", "local_date": "2026-03-07",
                      "attribution": "inferred", "policy_id": "policy-zulu"},
            "end": {"instant": "2026-03-08T20:00:00Z", "applied_offset": "+05:30", "local_date": "2026-03-09",
                    "attribution": "inferred", "policy_id": "policy-alpha"},
        })
        point = self.revision(weight(), SyntheticProcessor("weight"))
        self.assertEqual(json.loads(point.header_bytes)["time"], {
            "shape": "instant", "explicit_time": True,
            "start": {"instant": "2026-01-15T07:30:00.250Z", "source_offset": "+05:30", "applied_offset": "+05:30",
                      "local_date": "2026-01-15", "attribution": "experienced"},
        })
        self.assertEqual((outcome.processor_version, outcome.policy_identity),
                         ("interpret-steps@4", outcome.header.policy_identity))
        no_device = self.revision(steps(device=False), steps_processor())
        self.assertIsNone(no_device.header.device)
        self.assertEqual(set(json.loads(no_device.header_bytes)), R10_KEYS - {"device"})

    def test_header_omits_client_record_id(self) -> None:
        processor = steps_processor(body=lambda record, temporal: {"count": record["count"], "note": "synthetic-body-sentinel"})
        outcome = self.revision(steps(), processor)
        self.assertNotIn(b"client_record_id", outcome.header_bytes)
        self.assertNotIn(CLIENT_RECORD_ID.encode(), outcome.header_bytes)
        self.assertNotIn(b"client_record_version", outcome.header_bytes)
        self.assertNotIn(b"synthetic-body-sentinel", outcome.header_bytes)
        self.assertNotIn(CLIENT_RECORD_ID, repr(outcome.header))
        self.assertIn(b"synthetic-body-sentinel", outcome.body_bytes)

    def test_revision_digest_covers_header_and_body(self) -> None:
        one = self.revision(steps(count=4321))
        self.assertEqual(one.revision_sha256, sha(one.header_bytes))
        self.assertEqual(one.body_sha256, sha(one.body_bytes))
        self.assertEqual(json.loads(one.header_bytes)["body_sha256"], one.body_sha256)
        self.assertRegex(one.revision_sha256, r"^[0-9a-f]{64}$")
        other_body = self.revision(steps(count=4321), steps_processor(body=lambda record, temporal: {"count": 4322}))
        self.assertNotEqual(other_body.revision_sha256, one.revision_sha256)
        a, b = json.loads(one.header_bytes), json.loads(other_body.header_bytes)
        self.assertNotEqual(a.pop("body_sha256"), b.pop("body_sha256"))
        self.assertEqual(a, b)
        for changed in (steps_processor(body_version=2), steps_processor(version="interpret-steps@2")):
            self.assertNotEqual(self.revision(steps(count=4321), changed).revision_sha256, one.revision_sha256)
        for override in ({"record_id": 99}, {"selection_id": 99}, {"evidence_id": 99}):
            self.assertNotEqual(self.revision(steps(count=4321), **override).revision_sha256, one.revision_sha256)

    def test_processor_version_form_is_checked(self) -> None:
        for version in ("interpret-steps@1", "interpret-synthetic@12", "interpret-heart-rate@2"):
            with self.subTest(version):
                self.assertEqual(self.revision(steps(), steps_processor(version=version)).header.processor_version, version)
        for version in ("", "1", "1.0", "interpret-steps", "interpret-steps@", "@1", "interpret steps@1", "interpret-steps@1@2",
                        "interpret-steps@x", COMMON_VERSION.replace("@1", "@") + "x"):
            with self.subTest(version):
                self.assertEqual(self.refused(steps(), steps_processor(version=version)), "processor-invariant-violated")

    def test_body_schema_must_match_processor_type(self) -> None:
        self.assertEqual(self.refused(steps(), steps_processor(body_record_type="weight")), "processor-invariant-violated")
        self.assertIsInstance(run(steps(), steps_processor(body_record_type="steps")), CanonicalRevision)


# ---- R11 ----

class R11HealthDataFree(Checks):
    def outcomes(self):
        timeline = [policy("policy-alpha", None, "Asia/Kolkata")]
        quarantining = SyntheticProcessor("sleep-session", reasons=("synthetic-title-refused",),
                                          body=lambda record, temporal: Quarantine("synthetic-title-refused", "/record/title"))
        bad_offset = sleep()
        bad_offset["record"]["start_zone_offset"] = HEALTH_MARKER
        bad_lmt = sleep()
        bad_lmt["metadata"]["last_modified_time"] = HEALTH_MARKER
        return [
            run(sleep()),
            run(sleep(), SyntheticProcessor("sleep-session", supported=(2,))),
            run(sleep(start_offset=None), SyntheticProcessor("sleep-session")),
            run(sleep(start_offset=None, end_offset=None), quarantining, timeline),
            run(sleep(), quarantining),
            run(bad_offset),
            run(bad_offset, SyntheticProcessor("sleep-session")),
            decode_envelope(encode(bad_lmt), "sleep-session", 1),
            decode_envelope(HEALTH_MARKER.encode(), "sleep-session", 1),
            decode_envelope(encode(sleep()), "weight", 1),
        ]

    def test_outcomes_are_health_data_free(self) -> None:
        allowed = {
            Quarantine: {"reason", "pointer", "processor_version", "policy_identity"},
            Unmodeled: {"reason", "processor_version", "policy_identity"},
            PolicyPending: {"policy_kind", "reason", "processor_version", "policy_identity"},
        }
        seen = set()
        for outcome in self.outcomes():
            seen.add(type(outcome))
            self.assertIn(type(outcome), allowed)
            self.assertEqual({field.name for field in dataclasses.fields(outcome)}, allowed[type(outcome)])
            self.assert_health_free(repr(outcome))
            self.assert_health_free(str(outcome))
            self.assertRegex(outcome.reason, KEBAB)
            if isinstance(outcome, Quarantine):
                self.assertRegex(outcome.pointer, POINTER)
            if isinstance(outcome, PolicyPending):
                self.assertRegex(outcome.policy_kind, KEBAB)
        self.assertEqual(seen, set(allowed))

    def test_failures_are_health_data_free(self) -> None:
        projection = sleep()
        data = encode(projection)
        cases = {
            "processing-input-invalid": lambda: run(data, SyntheticProcessor("steps")),
            "processing-input-invalid ": lambda: run(data, None, [policy(HEALTH_MARKER, None, HEALTH_MARKER)]),
            "projection-digest-mismatch": lambda: run(data, projection_sha256=sha(data + b"\n")),
            "projection-identity-mismatch": lambda: run(data, source_identity=IDENTITY + "-other"),
            "projection-identity-mismatch ": lambda: run(data, origin=HEALTH_MARKER),
            "processor-invariant-violated": lambda: run(data, SyntheticProcessor(
                "sleep-session", body=lambda record, temporal: {"title": record["title"], "n": 1})),
            "processor-invariant-violated  ": lambda: run(data, SyntheticProcessor(
                "sleep-session", reasons=("synthetic-declared",), body=lambda record, temporal: Quarantine(HEALTH_MARKER, "/record/title"))),
        }
        for name, call in cases.items():
            with self.subTest(name):
                with self.assertRaises(InterpretFailure) as caught:
                    call()
                failure = caught.exception
                self.assertEqual(failure.failure, name.strip())
                self.assertRegex(failure.failure, KEBAB)
                for text in (str(failure), repr(failure), repr(failure.args),
                             "".join(traceback.format_exception(failure))):
                    self.assert_health_free(text)

    def test_processor_exception_text_is_not_carried(self) -> None:
        def explode(record, temporal):
            raise RuntimeError(f"{HEALTH_MARKER} {record['title']} {IDENTITY}")

        with self.assertRaises(InterpretFailure) as caught:
            run(sleep(), SyntheticProcessor("sleep-session", body=explode))
        failure = caught.exception
        self.assertEqual(failure.failure, "processor-invariant-violated")
        self.assertIsNone(failure.__cause__)
        rendered = "".join(traceback.format_exception(failure))
        self.assert_health_free(rendered)
        for text in (str(failure), repr(failure), repr(failure.args), repr(vars(failure))):
            self.assert_health_free(text)


if __name__ == "__main__":
    unittest.main()
