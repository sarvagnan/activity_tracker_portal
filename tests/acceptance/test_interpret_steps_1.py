"""Acceptance tests for `interpret-steps@1`.

Requirement numbers refer to `packs/interpret-steps-1.spec.md`. Inputs are the synthetic Steps
vectors in `projection/v1/vectors/positive.json` and hand-built synthetic Steps projections.
Every outcome runs end to end through the published `interpret_source_projection.interpret` with
this unit's `PROCESSOR`, or with a delegating test processor that observes each `interpret_body`
call. Fallback Timezone Policy timelines are synthetic. Health-data and identity markers in
projection text and Source Identities, and distinctive counts in fault cases, prove that
Quarantines carry none of them.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
import unittest
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from activity_tracker_portal.interpret_source_projection import (
    COMMON_VERSION,
    UNITS,
    BodySchema,
    CanonicalRevision,
    FallbackTimezonePolicy,
    NumericField,
    PolicyPending,
    ProcessingInput,
    Processor,
    Quarantine,
    interpret,
)
from activity_tracker_portal.interpret_steps import COUNT_MAX, COUNT_MIN, PROCESSOR

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PROJECTION = ROOT / "projection/v1"
CONTRACT = json.loads((ROOT / "governance/scopes/interpret-steps.1.json").read_text(encoding="utf-8"))
EFFECTS = json.loads((ROOT / "packs/interpret-steps-1.effects.json").read_text(encoding="utf-8"))
SCOPE_MAP = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
MODEL = json.loads((PROJECTION / "model.json").read_text(encoding="utf-8"))
POSITIVE = json.loads((PROJECTION / "vectors/positive.json").read_text(encoding="utf-8"))["vectors"]
STEPS_VECTORS = [vector for vector in POSITIVE if vector["record_type"] == "steps"]

VERSION = "interpret-steps@1"
EMPTY = "steps-interval-empty"
OUT_OF_RANGE = "steps-count-out-of-range"
REASONS = frozenset({EMPTY, OUT_OF_RANGE})
BODY_SCHEMA = BodySchema("steps", 1, (NumericField("/count", "integer", "count"),))
SAFE = 2**53 - 1
UTC = timezone.utc
HC = "health-connect"
ORIGIN = "org.example.synthetic.source"
SDK = "androidx.health.connect:connect-client:1.1.0"
HEALTH_MARKER = "synthetic-health-marker"
IDENTITY = "synthetic-identity-marker"
SOURCE_IDENTITY = IDENTITY + "-00000000-0000-4000-8000-000000000001"
LMT = "2026-01-15T09:00:00.123456789Z"
START = "2026-01-15T07:30:00Z"
END = "2026-01-15T08:00:00Z"
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
# Distinctive counts for fault cases; none occurs elsewhere in an outcome's text.
DISTINCT_HIGH = 7_316_029
DISTINCT_LOW = -6_204_917
DISTINCT_VALID = 58_213


# ---- Independent oracles ----

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def epoch_ns(text: str) -> int:
    year, month, day, hour, minute, second, fraction = INSTANT_TEXT.match(text).groups()
    whole = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), tzinfo=UTC)
    return (whole - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(seconds=1) * 10**9 + int((fraction or "").ljust(9, "0"))


def truncated(text: str) -> datetime:
    year, month, day, hour, minute, second, fraction = INSTANT_TEXT.match(text).groups()
    return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second),
                    int((fraction or "").ljust(9, "0")[:6]), tzinfo=UTC)


def body_bytes(count: int) -> bytes:
    """The RFC 8785 bytes of the Steps body: one integer member, so the form is fixed."""
    assert type(count) is int and abs(count) <= SAFE
    return b'{"count":' + str(count).encode("ascii") + b"}"


def json_strings(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from json_strings(item)
    elif isinstance(value, str):
        yield value


# ---- Synthetic projections and inputs ----

def encode(projection) -> bytes:
    return json.dumps(projection, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def reversed_keys(value):
    """The same JSON value with every object's keys in reverse order, so input order disagrees with the model."""
    if isinstance(value, dict):
        return {key: reversed_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [reversed_keys(item) for item in value]
    return value


def steps(start: str = START, end: str = END, *, start_offset: str | None = "+05:30", end_offset: str | None = "+05:30",
          count=4321, marked: bool = False, device: bool = True, recording_method: int = 2, lmt: str = LMT,
          identity: str = SOURCE_IDENTITY, provider: str = "synthetic-provider-1") -> dict:
    record = {"start_time": start}
    if start_offset is not None:
        record["start_zone_offset"] = start_offset
    record["end_time"] = end
    if end_offset is not None:
        record["end_zone_offset"] = end_offset
    record["count"] = count
    text = HEALTH_MARKER if marked else None
    metadata = {
        "id": identity,
        "data_origin": {"package_name": (HEALTH_MARKER + ".origin") if marked else ORIGIN},
        "last_modified_time": lmt,
        "client_record_id": text or "synthetic-client-record-id",
        "client_record_version": "7",
        "recording_method": recording_method,
    }
    if device:
        metadata["device"] = {"type": 1, "manufacturer": text or "Synthetic Devices", "model": text or "SD-1"}
    return {
        "record_type": "steps",
        "projection_schema_version": 1,
        "projection_implementation_version": text or "0.1.0-synthetic",
        "sdk_version": SDK,
        "provider_version": text or provider,
        "metadata": metadata,
        "record": record,
    }


def make_input(projection) -> ProcessingInput:
    data = projection if isinstance(projection, bytes) else encode(projection)
    decoded = json.loads(data)
    metadata = decoded["metadata"]
    return ProcessingInput(
        source_system_kind=HC, origin=metadata["data_origin"]["package_name"], record_type="steps",
        source_identity=metadata["id"], record_id=41, selection_id=42, evidence_id=43,
        projection_sha256=sha(data), last_modified_time=truncated(metadata["last_modified_time"]),
        projection_schema_version=1, projection=data,
    )


def policy(policy_id: str, effective_from: datetime | None, zone: str) -> FallbackTimezonePolicy:
    return FallbackTimezonePolicy(policy_id, effective_from, zone)


UTC_ALWAYS = (policy("policy-utc", None, "UTC"),)
KOLKATA = (policy("policy-kolkata", None, "Asia/Kolkata"),)


def run(projection, policies=(), processor=PROCESSOR):
    return interpret(make_input(projection), processor, list(policies))


class _OracleSteps:
    """A test-local processor that accepts every count, used only to learn the policy identity the parent assigns."""

    record_type = "steps"
    processor_version = "interpret-steps-oracle@1"
    supported_schema_versions = (1,)
    body_schema = BODY_SCHEMA
    quarantine_reasons = ()

    def interpret_body(self, record, temporal):
        return {"count": record["count"]}


def expected_identity(projection, policies=()) -> str:
    outcome = run(projection, policies, _OracleSteps())
    assert isinstance(outcome, CanonicalRevision), outcome
    return outcome.policy_identity


# ---- The processor boundary (R2, R7) ----
# The four processor suites share this block word for word.

# True only while PROCESSOR.interpret_body runs under `Observed`. The purity subprocess reads it, so it
# can watch the processor's own calls apart from the parent and the suite around them.
IN_PROCESSOR = [False]


class Observed:
    """A test processor that delegates to PROCESSOR and is passed to `interpret`.

    Before each call it deep-copies the exact `record` and `temporal` arguments, and afterwards it checks
    that both are unchanged. It then calls `interpret_body` again with the same objects, which must give
    an equal result. It keeps the processor's own result, before the parent rewraps a Quarantine, and
    every problem it saw, and returns the first result unchanged."""

    def __init__(self) -> None:
        self.record_type = PROCESSOR.record_type
        self.processor_version = PROCESSOR.processor_version
        self.supported_schema_versions = PROCESSOR.supported_schema_versions
        self.body_schema = PROCESSOR.body_schema
        self.quarantine_reasons = PROCESSOR.quarantine_reasons
        self.calls: list = []
        self.problems: list[str] = []

    def _call(self, record, temporal, which: str):
        IN_PROCESSOR[0] = True
        try:
            return PROCESSOR.interpret_body(record, temporal)
        except Exception as error:
            self.problems.append(f"{which} call raised {type(error).__name__}")
            raise
        finally:
            IN_PROCESSOR[0] = False

    def interpret_body(self, record, temporal):
        record_before, temporal_before = copy.deepcopy(record), copy.deepcopy(temporal)
        record_text = json.dumps(record)
        result = self._call(record, temporal, "first")
        if record != record_before or json.dumps(record) != record_text:
            self.problems.append("record changed")
        if temporal != temporal_before or repr(temporal) != repr(temporal_before):
            self.problems.append("temporal header changed")
        again = self._call(record, temporal, "repeated")
        if type(again) is not type(result) or again != result:
            self.problems.append("repeated call differs")
        self.calls.append(result)
        return result


def assert_identity_is_strict(case: unittest.TestCase) -> None:
    """R2: the identity sets have exact types, and no identity attribute, `interpret_body` included, can be assigned."""
    versions = PROCESSOR.supported_schema_versions
    case.assertIs(type(versions), frozenset)
    case.assertEqual(versions, frozenset({1}))
    # `frozenset({True})` and `frozenset({1.0})` both equal `frozenset({1})`, so the element type is checked too.
    case.assertEqual([type(version) for version in versions], [int])
    case.assertIs(type(PROCESSOR.quarantine_reasons), frozenset)
    case.assertEqual({type(reason) for reason in PROCESSOR.quarantine_reasons}, {str})
    replacements = {
        "record_type": "replaced-type", "processor_version": "replaced@2", "supported_schema_versions": frozenset({1, 2}),
        "body_schema": BodySchema("replaced-type", 2, ()), "quarantine_reasons": frozenset(),
        "interpret_body": lambda record, temporal: {},
    }
    for name, value in replacements.items():
        with case.subTest(assign=name):
            original = getattr(PROCESSOR, name)
            own = dict(vars(PROCESSOR)) if hasattr(PROCESSOR, "__dict__") else None
            try:
                setattr(PROCESSOR, name, value)
            except (AttributeError, TypeError):
                case.assertEqual(getattr(PROCESSOR, name), original)
                continue
            # Restore before failing, so a mutable processor does not disturb the other tests.
            try:
                if own is not None:
                    vars(PROCESSOR).clear()
                    vars(PROCESSOR).update(own)
                else:
                    object.__setattr__(PROCESSOR, name, original)
            except Exception:  # noqa: BLE001 - the assignment already failed the test
                pass
            case.fail(f"PROCESSOR.{name} can be assigned")


def assert_raw_quarantine(case: unittest.TestCase, observed: Observed, reason: str, pointer: str, *values: str) -> None:
    """R7: the processor's own Quarantine, before the parent rewraps it, is exactly `Quarantine(reason, pointer)`.
    It keeps the parent's default version and policy identity, and no field, `repr`, or `str` carries a value,
    a health-data marker, or a Source Identity."""
    case.assertEqual(observed.problems, [])
    case.assertEqual(len(observed.calls), 1)
    raw = observed.calls[0]
    case.assertIs(type(raw), Quarantine)
    case.assertEqual(raw, Quarantine(reason, pointer))
    for text in (repr(raw), str(raw), *(str(getattr(raw, field.name)) for field in dataclasses.fields(raw))):
        case.assertNotIn(HEALTH_MARKER, text)
        case.assertNotIn(IDENTITY, text)
        for value in values:
            case.assertNotIn(value, text)


# The purity subprocess's observer. Each suite fills in its unit module and its own test module.
PURITY_OBSERVER = r"""
import json, os, sys, time, zoneinfo
# Modules the suite or an implementation commonly needs, and the published parent, are imported first,
# so that only this unit's own import and calls run under observation.
import jsonschema, tzdata, logging, decimal, fractions, hashlib, re, importlib.resources, pathlib, dataclasses, random
import glob, typing, collections, functools, itertools, calendar, math, unicodedata, copy, enum, struct, base64, datetime
import string, operator, bisect, heapq, io, traceback, unittest, subprocess, tempfile, shutil, uuid, types
import activity_tracker_portal.interpret_source_projection
host = [os.path.realpath(p) for p in zoneinfo.TZPATH] + [os.path.realpath("/etc/localtime"), "/etc/localtime"]
timezone_data = os.path.realpath(os.path.dirname(tzdata.__file__)) + os.sep
WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
LISTING = {"os.listdir", "os.scandir", "glob.glob", "glob.glob/2", "pathlib.Path.glob", "pathlib.Path.rglob"}
phase = ["import"]
inside = [False]  # The suite's IN_PROCESSOR once warm-up is over.
events = []
def hook(event, args):
    if inside[0] and (event == "open" or event in LISTING):
        # During interpret_body nothing is read: no file, directory, or timezone data, pinned or host.
        events.append(["processor-call", phase[0], event, str(args[0]) if args else ""])
    if event == "open":
        path, mode, flags = args
        if isinstance(path, (str, bytes)):
            path = os.fsdecode(path)
            if (mode and any(c in mode for c in "wax+")) or (flags or 0) & WRITE:
                events.append(["write", path])
            real = os.path.realpath(path)
            if any(real == h or real.startswith(h.rstrip("/") + "/") for h in host):
                events.append(["host-timezone", path])
            # Without a policy the parent loads no zone, so no pinned timezone data is read at all.
            if phase[0] == "policy-free" and real.startswith(timezone_data):
                events.append(["timezone-data", phase[0], path])
    elif event.split(".")[0] in ("socket", "sqlite3", "subprocess", "ctypes", "webbrowser", "urllib", "http") or event in (
            "os.system", "os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.chmod", "os.utime", "os.truncate",
            "os.symlink", "os.link", "os.posix_spawn", "os.exec", "os.fork", "shutil.copyfile", "shutil.rmtree"):
        events.append([event])
def forbidden(*args, **kwargs):
    raise AssertionError("clock read")
def explicit(original, index):
    def guard(*args):
        if len(args) <= index or args[index] is None:
            raise AssertionError("clock read")
        return original(*args)
    return guard
before = set(sys.modules)
sys.addaudithook(hook)
for name in ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns", "process_time",
             "process_time_ns", "thread_time", "thread_time_ns", "clock_gettime", "clock_gettime_ns",
             "localtime", "ctime", "mktime"):
    if hasattr(time, name):
        setattr(time, name, forbidden)
time.gmtime = explicit(time.gmtime, 0)
time.asctime = explicit(time.asctime, 0)
time.strftime = explicit(time.strftime, 1)
import activity_tracker_portal.@UNIT@
added = set(sys.modules) - before
import @SUITE@ as t
# Warm-up: a first round of calls may finish lazy imports. The parent reads pinned timezone data for
# its policies outside the processor's calls; the zone cache is then cleared.
phase[0] = "warm-up"
warm = t.scenario_summary("all", t.Observed())
zoneinfo.ZoneInfo.clear_cache()
inside = t.IN_PROCESSOR
observed = t.Observed()
phase[0] = "policy-free"
policy_free = t.scenario_summary("policy-free", observed)
phase[0] = "policy-applied"
policy_applied = t.scenario_summary("policy-applied", observed)
phase[0] = "done"
print(json.dumps({"store": "activity_tracker_portal.operational_store" in sys.modules,
                  "nested_time_helper": "activity_tracker_portal._interpret_nested_time" in added,
                  "events": events, "problems": observed.problems, "calls": len(observed.calls), "warm": warm,
                  "policy-free": policy_free, "policy-applied": policy_applied}))
"""


def purity_observer(unit: str, suite: str) -> str:
    return PURITY_OBSERVER.replace("@UNIT@", unit).replace("@SUITE@", suite)


def scenario_summary(phase: str = "all", processor=PROCESSOR) -> str:
    """Representative outcomes, compared in process and in observed subprocesses (R7). The phase selects the
    cases without policies ("policy-free"), those with policies ("policy-applied"), or both ("all")."""
    cases = [
        (steps(), ()),
        (steps("2026-01-15T18:00:00Z", "2026-01-15T19:00:00Z"), ()),
        (steps(END, END, count=DISTINCT_VALID), ()),
        (steps(count=DISTINCT_LOW), ()),
        (steps(start_offset=None, end_offset=None, count=COUNT_MAX), KOLKATA),
        (steps(count=DISTINCT_HIGH), UTC_ALWAYS),
        (steps(START, START, start_offset=None, count=DISTINCT_VALID), KOLKATA),
        (steps(start_offset=None, count=0), KOLKATA),
    ]
    out = []
    for projection, policies in cases:
        if phase != "all" and (phase == "policy-applied") != bool(policies):
            continue
        outcome = run(projection, policies, processor)
        if isinstance(outcome, CanonicalRevision):
            out.append([outcome.header_bytes.hex(), outcome.body_bytes.hex(), outcome.revision_sha256])
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
    def revision(self, projection, policies=()) -> CanonicalRevision:
        outcome = run(projection, policies)
        self.assertIsInstance(outcome, CanonicalRevision, repr(outcome))
        header = json.loads(outcome.header_bytes)
        self.assertEqual(outcome.processor_version, VERSION)
        self.assertEqual(header["processor_version"], VERSION)
        self.assertEqual(header["header_version"], COMMON_VERSION)
        self.assertEqual(header["body_schema"], {"record_type": "steps", "body_version": 1})
        self.assertEqual(outcome.header.body_schema, BODY_SCHEMA)
        self.assertEqual(header["body_sha256"], sha(outcome.body_bytes))
        self.assertEqual(outcome.body_sha256, sha(outcome.body_bytes))
        self.assertEqual(outcome.revision_sha256, sha(outcome.header_bytes))
        return outcome

    def assert_body(self, outcome: CanonicalRevision, count: int) -> None:
        self.assertEqual(outcome.body_bytes, body_bytes(count))
        decoded = json.loads(outcome.body_bytes)
        self.assertEqual(decoded, {"count": count})
        self.assertIs(type(decoded["count"]), int)

    def quarantine(self, projection, reason: str, pointer: str, policies=()) -> Quarantine:
        outcome = run(projection, policies)
        self.assertEqual(outcome, Quarantine(reason, pointer, VERSION, expected_identity(projection, policies)))
        return outcome

    def assert_health_free(self, text: str, *counts: int) -> None:
        self.assertNotIn(HEALTH_MARKER, text)
        self.assertNotIn(IDENTITY, text)
        for count in counts:
            self.assertNotIn(str(count), text)
            self.assertNotIn(str(abs(count)), text)


# ---- R1 ----

class R1GovernedScopeContract(unittest.TestCase):
    def test_contract_shape_and_dependency(self) -> None:
        self.assertEqual((CONTRACT["scope"], CONTRACT["version"], CONTRACT["kind"]), ("interpret-steps", "1", "leaf"))
        self.assertEqual(CONTRACT["dependencies"], [{"scope": "interpret-source-projection", "version": "1"}])
        self.assertEqual(CONTRACT["named_failures"], [])
        self.assertEqual(CONTRACT["observable_effects"], [])
        self.assertEqual({item["mode"] for item in CONTRACT["inputs"]}, {"pure"})
        outputs = {item["name"]: item["description"] for item in CONTRACT["outputs"]}
        self.assertEqual(set(re.findall(r"\bsteps-[a-z0-9-]*[a-z0-9]\b", outputs["quarantine_outcome"])), set(REASONS))
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["specification"], "packs/interpret-steps-1.spec.md")
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_interpret_steps_1.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_interpret_steps_1.sh")
        self.assertEqual(oracle["languages"], ["python"])

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        row = next(line for line in SCOPE_MAP.splitlines() if line.startswith("| `interpret-steps` |"))
        self.assertIn("| leaf |", row)
        self.assertIn("Steps", row)
        self.assertIn("without deriving totals", row)
        self.assertTrue(row.rstrip().endswith("| `interpret-source-projection` |"), row)

    def test_effects_declaration_is_empty(self) -> None:
        self.assertEqual(EFFECTS, {"effects_format": 1, "effects": []})


# ---- R2 ----

class R2ProcessorIdentity(unittest.TestCase):
    def test_processor_identity(self) -> None:
        self.assertIsInstance(PROCESSOR, Processor)
        self.assertEqual(PROCESSOR.record_type, "steps")
        self.assertIs(type(PROCESSOR.record_type), str)
        self.assertEqual(PROCESSOR.processor_version, VERSION)
        self.assertEqual(PROCESSOR.processor_version, f"{CONTRACT['scope']}@{CONTRACT['version']}")
        assert_identity_is_strict(self)
        self.assertEqual(PROCESSOR.body_schema, BODY_SCHEMA)
        self.assertIs(type(PROCESSOR.body_schema.numeric_fields), tuple)
        self.assertIn(PROCESSOR.body_schema.numeric_fields[0].unit, UNITS)
        self.assertIs(type(PROCESSOR.body_schema.body_version), int)
        # The model names `count` as the Steps record field; the body uses the name verbatim.
        self.assertIn("count", [entry["json"] for entry in MODEL["records"]["steps"]["fields"]])
        self.assertEqual(PROCESSOR.record_type, "steps")
        self.assertEqual(PROCESSOR.processor_version, VERSION)
        self.assertEqual(PROCESSOR.supported_schema_versions, frozenset({1}))
        self.assertEqual(PROCESSOR.quarantine_reasons, REASONS)

    def test_declared_reasons_are_exact_and_not_common(self) -> None:
        self.assertIs(type(PROCESSOR.quarantine_reasons), frozenset)
        self.assertEqual(PROCESSOR.quarantine_reasons, REASONS)
        self.assertFalse(PROCESSOR.quarantine_reasons & COMMON_REASONS)
        outputs = {item["name"]: item["description"] for item in CONTRACT["outputs"]}
        for reason in PROCESSOR.quarantine_reasons:
            with self.subTest(reason=reason):
                self.assertRegex(reason, KEBAB)
                self.assertTrue(reason.startswith("steps-"), reason)
                self.assertIn(reason, outputs["quarantine_outcome"])


# ---- R3 ----

class R3Body(Checks):
    def test_positive_vectors_become_revisions(self) -> None:
        names = {vector["name"] for vector in STEPS_VECTORS}
        self.assertTrue({"steps-minimal", "steps-maximal"} <= names, names)
        for vector in STEPS_VECTORS:
            with self.subTest(vector=vector["name"]):
                data = vector["bytes"].encode("utf-8")
                self.assertEqual(sha(data), vector["sha256"])
                record = vector["projection"]["record"]
                inferred = "start_zone_offset" not in record or "end_zone_offset" not in record
                policies = KOLKATA if inferred else ()
                outcome = self.revision(data, policies)
                self.assert_body(outcome, record["count"])
                self.assertEqual(outcome.header.time.start.instant.text, record["start_time"])
                self.assertEqual(outcome.header.time.end.instant.text, record["end_time"])
                if inferred:
                    self.assertIn("policy-kolkata", outcome.policy_identity)
                else:
                    self.assertEqual(outcome.policy_identity, "none")
        minimal = next(vector for vector in STEPS_VECTORS if vector["name"] == "steps-minimal")
        self.assertIsInstance(run(minimal["bytes"].encode("utf-8")), PolicyPending)

    def test_body_is_exactly_the_count(self) -> None:
        for count in (COUNT_MIN, COUNT_MIN + 1, 7, 4321, DISTINCT_VALID, 65_536, 999_999, COUNT_MAX):
            for policies, offsets in (((), ("+05:30", "-08:00")), (KOLKATA, (None, None))):
                with self.subTest(count=count, offsets=offsets):
                    outcome = self.revision(steps(start_offset=offsets[0], end_offset=offsets[1], count=count), policies)
                    self.assert_body(outcome, count)
        a = self.revision(steps(count=4321))
        b = self.revision(steps(count=4322))
        self.assertNotEqual(a.body_bytes, b.body_bytes)
        self.assertNotEqual(a.revision_sha256, b.revision_sha256)

    def test_body_has_no_header_fields(self) -> None:
        cases = [(vector["bytes"].encode("utf-8"), KOLKATA) for vector in STEPS_VECTORS]
        cases += [
            (steps(marked=True), ()),
            (steps(start_offset=None, end_offset="-08:00", marked=True), KOLKATA),
            (steps("2026-01-15T18:00:00.123456789Z", "2026-01-16T19:00:00.500Z"), ()),
        ]
        for index, (projection, policies) in enumerate(cases):
            with self.subTest(case=index):
                outcome = self.revision(projection, policies)
                body = json.loads(outcome.body_bytes)
                self.assertEqual(set(body), {"count"})
                header = json.loads(outcome.header_bytes)
                text = outcome.body_bytes.decode("utf-8")
                for value in set(json_strings(header)):
                    if len(value) >= 4 and value != "count":
                        self.assertNotIn(value, text)
                for name in ("time", "start", "end", "instant", "local_date", "offset", "device", "recording_method",
                             "attribution", "policy", "date", "duration", "total"):
                    self.assertNotIn(name, text)


# ---- R4 ----

class R4CountBounds(Checks):
    def test_count_bounds_are_the_pinned_sdk_bounds(self) -> None:
        self.assertIs(type(COUNT_MIN), int)
        self.assertIs(type(COUNT_MAX), int)
        self.assertEqual((COUNT_MIN, COUNT_MAX), (1, 1_000_000))
        for count in (COUNT_MIN, COUNT_MAX):
            with self.subTest(count=count):
                self.assert_body(self.revision(steps(count=count)), count)
                self.assert_body(self.revision(steps(start_offset=None, end_offset=None, count=count), UTC_ALWAYS), count)
        for count in (COUNT_MIN - 1, COUNT_MAX + 1):
            with self.subTest(count=count):
                self.quarantine(steps(count=count), OUT_OF_RANGE, "/record/count")

    def test_count_outside_bounds_is_quarantined(self) -> None:
        for count in (0, -1, -2, COUNT_MAX + 1, COUNT_MAX + 2, 2**31, 2**32 + 1, DISTINCT_HIGH, DISTINCT_LOW,
                      SAFE, -SAFE, SAFE - 1, -SAFE + 1):
            for offsets, policies in ((("+05:30", "+05:30"), ()), (("-08:00", None), KOLKATA),
                                      ((None, None), UTC_ALWAYS)):
                with self.subTest(count=count, offsets=offsets):
                    projection = steps(start_offset=offsets[0], end_offset=offsets[1], count=count)
                    outcome = self.quarantine(projection, OUT_OF_RANGE, "/record/count", policies)
                    self.assertEqual(outcome.policy_identity == "none", not policies)
        # A one-nanosecond interval with a bad count is still a count fault, not an empty interval.
        self.quarantine(steps("2026-01-15T07:59:59.999999999Z", "2026-01-15T08:00:00Z", count=0), OUT_OF_RANGE, "/record/count")


# ---- R5 ----

class R5EmptyInterval(Checks):
    def test_empty_interval_is_quarantined(self) -> None:
        cases = [
            (steps(END, END), ()),
            (steps(END, "2026-01-15T08:00:00.000Z"), ()),
            (steps("2026-01-15T08:00:00.000000Z", "2026-01-15T08:00:00.000000000Z"), ()),
            (steps("2026-01-15T08:00:00.123456789Z", "2026-01-15T08:00:00.123456789Z"), ()),
            (steps("2026-01-15T08:00:00.120Z", "2026-01-15T08:00:00.120000000Z"), ()),
            # Different offsets, so the local wall times and local dates differ, but the instant is one.
            (steps("2026-01-15T10:00:00Z", "2026-01-15T10:00:00Z", start_offset="+14:00", end_offset="-10:00"), ()),
            (steps("2026-01-15T10:00:00Z", "2026-01-15T10:00:00Z", start_offset="-18:00", end_offset="+18:00"), ()),
            (steps(END, END, start_offset=None, end_offset=None), KOLKATA),
            (steps(END, END, start_offset=None, end_offset="-08:00"), UTC_ALWAYS),
            (steps("2000-02-29T23:45:00Z", "2000-02-29T23:45:00.000Z", count=COUNT_MAX), ()),
            (steps(END, END, count=COUNT_MIN), ()),
            (steps(END, END, count=DISTINCT_VALID), ()),
        ]
        for projection, policies in cases:
            record = projection["record"]
            with self.subTest(start=record["start_time"], end=record["end_time"], offsets=(
                    record.get("start_zone_offset"), record.get("end_zone_offset"))):
                self.assertEqual(epoch_ns(record["start_time"]), epoch_ns(record["end_time"]))
                self.quarantine(projection, EMPTY, "/record/end_time", policies)

    def test_one_nanosecond_interval_is_accepted(self) -> None:
        for start, end, offsets, policies in (
            ("2026-01-15T07:59:59.999999999Z", END, ("+05:30", "+05:30"), ()),
            ("2026-01-15T08:00:00.000000000Z", "2026-01-15T08:00:00.000000001Z", ("+05:30", "+05:30"), ()),
            ("2026-01-15T08:00:00.000Z", "2026-01-15T08:00:00.000000001Z", ("-08:00", "+05:30"), ()),
            ("2026-01-15T08:00:00.123456Z", "2026-01-15T08:00:00.123456001Z", (None, None), KOLKATA),
            ("2026-01-15T18:29:59.999999999Z", "2026-01-15T18:30:00Z", ("+05:30", "+05:30"), ()),
            ("1999-12-31T23:59:59.999999999Z", "2000-01-01T00:00:00Z", (None, "+00:00"), UTC_ALWAYS),
        ):
            with self.subTest(start=start, end=end):
                self.assertEqual(epoch_ns(end) - epoch_ns(start), 1)
                outcome = self.revision(steps(start, end, start_offset=offsets[0], end_offset=offsets[1],
                                              count=DISTINCT_VALID), policies)
                self.assert_body(outcome, DISTINCT_VALID)
                self.assertEqual(outcome.header.time.end.instant.epoch_ns - outcome.header.time.start.instant.epoch_ns, 1)

    def test_first_fault_follows_traversal_order(self) -> None:
        names = [entry["json"] for entry in MODEL["records"]["steps"]["fields"]]
        self.assertLess(names.index("end_time"), names.index("count"))
        for count in (0, -1, COUNT_MAX + 1, DISTINCT_HIGH, SAFE, -SAFE):
            for start, end, offsets, policies in (
                (END, END, ("+05:30", "+05:30"), ()),
                (END, "2026-01-15T08:00:00.000000000Z", ("+14:00", "-10:00"), ()),
                (END, END, (None, None), KOLKATA),
            ):
                projection = steps(start, end, start_offset=offsets[0], end_offset=offsets[1], count=count)
                for label, value in (("model order", projection), ("reverse order", reversed_keys(projection))):
                    with self.subTest(count=count, end=end, order=label):
                        self.quarantine(value, EMPTY, "/record/end_time", policies)
                # Control: the same count over a non-empty interval is a count fault.
                control = steps(start_offset=offsets[0], end_offset=offsets[1], count=count)
                with self.subTest(count=count, control=True):
                    self.quarantine(reversed_keys(control), OUT_OF_RANGE, "/record/count", policies)

    def test_common_outcomes_take_precedence(self) -> None:
        pending = PolicyPending("fallback-timezone", "fallback-timezone-policy-missing", VERSION, "none")
        reversed_ = Quarantine("interval-reversed", "/record/end_time", VERSION, "none")
        cases = [
            # Reversed interval, with a count fault too.
            (steps(END, START, count=0), (), reversed_),
            (steps(END, START, count=DISTINCT_HIGH), (), reversed_),
            (steps("2026-01-15T08:00:00.000000001Z", END, count=COUNT_MAX), (), reversed_),
            # Missing policy, with a count fault, an empty interval, or both.
            (steps(start_offset=None, end_offset=None, count=0), (), pending),
            (steps(END, END, start_offset=None, end_offset=None), (), pending),
            (steps(END, END, start_offset="+05:30", end_offset=None, count=DISTINCT_LOW), (), pending),
            (steps(start_offset=None, end_offset="+05:30", count=COUNT_MAX + 1), (), pending),
            # Schema faults and out-of-range instants come before any processor rule.
            (steps(END, END, count=SAFE + 1), (), Quarantine("projection-schema-invalid", "/record/count", VERSION, "none")),
            (steps(END, END, count="4321"), (), Quarantine("projection-schema-invalid", "/record/count", VERSION, "none")),
            (steps(END, END, count=4321.5), (), Quarantine("projection-schema-invalid", "/record/count", VERSION, "none")),
            (steps("0000-06-01T00:00:00Z", "0000-06-01T00:00:00Z", count=0), (),
             Quarantine("instant-out-of-range", "/record/start_time", VERSION, "none")),
            (steps("9999-12-31T23:00:00Z", "9999-12-31T23:00:00Z", start_offset="+00:00", end_offset="+01:00", count=0), (),
             Quarantine("instant-out-of-range", "/record/end_time", VERSION, "none")),
        ]
        for projection, policies, expected in cases:
            record = projection["record"]
            with self.subTest(expected=expected.reason, record=repr(record)):
                observed = Observed()
                self.assertEqual(run(projection, policies, observed), expected)
                self.assertEqual((observed.calls, observed.problems), ([], []))
        # Controls: once the common condition is removed, this processor reports its own fault.
        self.quarantine(steps(START, END, count=0), OUT_OF_RANGE, "/record/count")
        self.quarantine(steps(start_offset=None, end_offset=None, count=0), OUT_OF_RANGE, "/record/count", KOLKATA)
        self.quarantine(steps(END, END, start_offset=None, end_offset=None), EMPTY, "/record/end_time", UTC_ALWAYS)
        self.quarantine(steps("9999-12-31T22:00:00Z", "9999-12-31T22:00:00Z", start_offset="+00:00", end_offset="+01:00",
                              count=0), EMPTY, "/record/end_time")


# ---- R6 ----

class R6NoDerivation(Checks):
    def test_body_ignores_time_attribution_and_policies(self) -> None:
        base = self.revision(steps())
        self.assert_body(base, 4321)
        leap = next(vector for vector in STEPS_VECTORS if vector["name"] == "steps-leap-days")["projection"]
        leap_inferred = copy.deepcopy(leap)
        del leap_inferred["record"]["start_zone_offset"], leap_inferred["record"]["end_zone_offset"]
        switching = (policy("policy-zulu", None, "UTC"),
                     policy("policy-alpha", datetime(2026, 1, 15, 7, 45, tzinfo=UTC), "America/New_York"))
        variants = [
            (steps(start_offset="+00:00", end_offset="-08:00"), ()),
            (steps(start_offset="-12:00", end_offset="+14:00"), ()),
            (steps(start_offset="+18:00", end_offset="-18:00"), ()),
            (steps(start_offset=None, end_offset=None), UTC_ALWAYS),
            (steps(start_offset=None, end_offset=None), KOLKATA),
            (steps(start_offset=None, end_offset=None), (policy("policy-ny", None, "America/New_York"),)),
            (steps(start_offset=None, end_offset=None), (policy("policy-kiritimati", None, "Pacific/Kiritimati"),)),
            (steps(start_offset=None, end_offset=None), switching),
            (steps(start_offset="+05:30", end_offset=None), UTC_ALWAYS),
            (steps("2026-01-15T18:00:00Z", "2026-01-15T19:00:00Z"), ()),
            (steps("2026-01-15T07:30:00.000000001Z", "2026-01-16T08:00:00.999999999Z"), ()),
            (steps(recording_method=0), ()),
            (steps(recording_method=3), ()),
            (steps(device=False), ()),
            (steps(marked=True), ()),
            (steps(lmt="2027-06-01T00:00:00Z"), ()),
            (steps(identity="00000000-0000-4000-8000-0000000000ff"), ()),
            (steps(provider="synthetic-provider-2"), ()),
            (reversed_keys(steps(start_offset="-03:30", end_offset="+09:45")), ()),
            (leap, ()),
            (leap_inferred, UTC_ALWAYS),
            (leap_inferred, (policy("policy-honolulu", None, "Pacific/Honolulu"),)),
        ]
        variants += [(vector["bytes"].encode("utf-8"), UTC_ALWAYS) for vector in STEPS_VECTORS]
        for index, (projection, policies) in enumerate(variants):
            with self.subTest(variant=index):
                outcome = self.revision(projection, policies)
                self.assert_body(outcome, 4321)
                self.assertEqual(outcome.body_sha256, base.body_sha256)
                self.assertNotEqual(outcome.header_bytes, base.header_bytes)

    def test_day_crossing_interval_is_not_allocated(self) -> None:
        leap = next(vector for vector in STEPS_VECTORS if vector["name"] == "steps-leap-days")["projection"]
        leap_inferred = copy.deepcopy(leap)
        del leap_inferred["record"]["start_zone_offset"], leap_inferred["record"]["end_zone_offset"]
        new_york = (policy("policy-ny", None, "America/New_York"),)
        cases = [
            (steps("2026-01-15T18:00:00Z", "2026-01-15T19:00:00Z", count=4321), (), "2026-01-15", "2026-01-16", 4321),
            (steps("2026-01-14T12:00:00Z", "2026-01-17T12:00:00Z", start_offset="+00:00", end_offset="+00:00",
                   count=987_654), (), "2026-01-14", "2026-01-17", 987_654),
            (steps("2026-01-16T04:30:00Z", "2026-01-16T05:30:00Z", start_offset=None, end_offset=None, count=COUNT_MAX),
             new_york, "2026-01-15", "2026-01-16", COUNT_MAX),
            (steps("2026-01-15T18:29:59.999999999Z", "2026-01-15T18:30:00Z", count=COUNT_MIN), (),
             "2026-01-15", "2026-01-16", COUNT_MIN),
            (leap_inferred, UTC_ALWAYS, "2000-02-29", "2000-03-01", 4321),
            (steps("2025-12-31T23:59:00Z", "2026-01-01T00:01:00Z", start_offset=None, end_offset="+00:00", count=120),
             UTC_ALWAYS, "2025-12-31", "2026-01-01", 120),
        ]
        for projection, policies, start_date, end_date, count in cases:
            with self.subTest(start_date=start_date, end_date=end_date):
                outcome = self.revision(projection, policies)
                self.assert_body(outcome, count)
                time = json.loads(outcome.header_bytes)["time"]
                self.assertEqual((time["start"]["local_date"], time["end"]["local_date"]), (start_date, end_date))
                self.assertEqual((outcome.header.time.start.local_date, outcome.header.time.end.local_date),
                                 (date.fromisoformat(start_date), date.fromisoformat(end_date)))


# ---- R7 ----

COUNTS = (-SAFE, -SAFE + 1, -2**31, -COUNT_MAX - 1, -1, 0, 1, 2, 999_999, 1_000_000, 1_000_001, 2**31 - 1, 2**32,
          SAFE - 1, SAFE)
INTERVALS = (
    (END, END, ("+05:30", "+05:30"), ()),
    (END, "2026-01-15T08:00:00.000000000Z", ("+05:30", "-08:00"), ()),
    ("2026-01-15T07:59:59.999999999Z", END, ("+05:30", "+05:30"), ()),
    ("1999-12-31T23:59:59.999999999Z", "2000-01-01T00:00:00Z", (None, None), UTC_ALWAYS),
    ("0001-01-01T00:00:00Z", "9999-12-31T23:59:59.999999999Z", ("+00:00", "+00:00"), ()),
    ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00.000Z", (None, None), UTC_ALWAYS),
    ("2000-02-29T23:45:00Z", "2000-03-01T00:15:00Z", (None, "+05:30"), KOLKATA),
)


class R7TotalPureAndHealthDataFree(Checks):
    def test_schema_valid_edges_never_raise(self) -> None:
        for count in COUNTS:
            for start, end, offsets, policies in INTERVALS:
                with self.subTest(count=count, start=start, end=end):
                    projection = steps(start, end, start_offset=offsets[0], end_offset=offsets[1], count=count)
                    # `Observed` also checks that the record and temporal header are unchanged and that a
                    # repeated call with the same objects gives an equal result.
                    observed = Observed()
                    outcome = run(projection, policies, observed)
                    self.assertEqual(observed.problems, [])
                    self.assertEqual(len(observed.calls), 1)
                    result = observed.calls[0]
                    if epoch_ns(start) == epoch_ns(end):
                        expected = (EMPTY, "/record/end_time")
                    elif not COUNT_MIN <= count <= COUNT_MAX:
                        expected = (OUT_OF_RANGE, "/record/count")
                    else:
                        expected = None
                    if expected is None:
                        self.assertIsInstance(result, Mapping)
                        self.assertEqual(dict(result), {"count": count})
                        self.assertIs(type(result["count"]), int)
                        self.assertIsInstance(outcome, CanonicalRevision)
                        self.assert_body(outcome, count)
                    else:
                        self.assertIs(type(result), Quarantine)
                        self.assertEqual((result.reason, result.pointer), expected)
                        self.assertEqual(outcome, Quarantine(*expected, VERSION, expected_identity(projection, policies)))

    def test_processor_is_pure(self) -> None:
        # In process, through `interpret`: on revision and Quarantine paths, with and without policies, every
        # call leaves its exact record and temporal header unchanged, and a repeated call gives an equal result.
        for phase in ("policy-free", "policy-applied"):
            with self.subTest(phase=phase):
                observed = Observed()
                self.assertEqual(scenario_summary(phase, observed), scenario_summary(phase))
                self.assertEqual(observed.problems, [])
                self.assertEqual({type(result) is Quarantine for result in observed.calls}, {True, False})
        observed = Observed()
        expected = {phase: scenario_summary(phase) for phase in ("policy-free", "policy-applied")}
        expected["warm"] = scenario_summary("all", observed)
        # In subprocesses: importing the module and running it reads no clock, writes nothing, and opens no host
        # timezone file, socket, store, or subprocess. After warm-up, the processor's own calls open no file, list
        # no directory, and read no timezone data, pinned or host, with or without policies.
        code = purity_observer("interpret_steps", "test_interpret_steps_1")
        for settings in ({"TZ": "Pacific/Chatham", "LC_ALL": "C", "PYTHONHASHSEED": "1"},
                         {"TZ": "America/Los_Angeles", "LC_ALL": "en_US.UTF-8", "PYTHONHASHSEED": "2"}):
            with self.subTest(**settings):
                result = json.loads(run_python(code, settings))
                self.assertEqual(result, {"store": False, "nested_time_helper": False, "events": [], "problems": [],
                                          "calls": len(observed.calls), **expected})

    def test_outcomes_are_health_data_free(self) -> None:
        cases = [
            (steps(count=DISTINCT_HIGH, marked=True), (), OUT_OF_RANGE, DISTINCT_HIGH),
            (steps(count=DISTINCT_LOW, marked=True), (), OUT_OF_RANGE, DISTINCT_LOW),
            (reversed_keys(steps(count=DISTINCT_HIGH, marked=True, start_offset=None)), KOLKATA, OUT_OF_RANGE, DISTINCT_HIGH),
            (steps(END, END, count=DISTINCT_VALID, marked=True), (), EMPTY, DISTINCT_VALID),
            (steps(END, END, count=DISTINCT_HIGH, marked=True, start_offset=None, end_offset=None), KOLKATA, EMPTY,
             DISTINCT_HIGH),
        ]
        fields = {"reason", "pointer", "processor_version", "policy_identity"}
        for projection, policies, reason, count in cases:
            with self.subTest(reason=reason, count=count):
                observed = Observed()
                outcome = run(projection, policies, observed)
                self.assertIs(type(outcome), Quarantine)
                self.assertEqual({field.name for field in dataclasses.fields(outcome)}, fields)
                self.assertEqual(outcome.reason, reason)
                self.assertIn(outcome.pointer, {"/record/count", "/record/end_time"})
                for text in (repr(outcome), str(outcome), outcome.reason, outcome.pointer, outcome.policy_identity):
                    self.assert_health_free(text, count)
                # What the processor itself returned, before the parent rewrapped it.
                assert_raw_quarantine(self, observed, outcome.reason, outcome.pointer, str(count), str(abs(count)))


if __name__ == "__main__":
    unittest.main()
