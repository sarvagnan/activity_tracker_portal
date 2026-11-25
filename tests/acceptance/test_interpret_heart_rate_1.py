"""Acceptance tests for `interpret-heart-rate@1`.

Requirement numbers refer to `packs/interpret-heart-rate-1.spec.md`. Inputs are the synthetic
Heart Rate vectors in `projection/v1/vectors/positive.json` and hand-built synthetic Heart Rate
projections. Every outcome runs end to end through the published `interpret` with this unit's
`PROCESSOR` and synthetic Fallback Timezone Policy timelines. A health-data marker in projection
text fields and an identity marker in Source Identities prove that Quarantines carry neither.

Exact-value body assertions compare RFC 8785 canonical bytes. The local-date oracle below uses
`datetime` for years 1-9999 and treats anything outside them as having no date; year-0000 instants
are compared through the 400-year Gregorian cycle (146,097 days).
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from activity_tracker_portal.interpret_heart_rate import BPM_MAX, BPM_MIN, PROCESSOR
from activity_tracker_portal.interpret_source_projection import (
    UNITS,
    BodySchema,
    CanonicalRevision,
    FallbackTimezonePolicy,
    InterpretFailure,
    NumericField,
    PolicyPending,
    ProcessingInput,
    Processor,
    Quarantine,
    interpret,
)

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PROJECTION = ROOT / "projection/v1"
CONTRACT = json.loads((ROOT / "governance/scopes/interpret-heart-rate.1.json").read_text(encoding="utf-8"))
EFFECTS = json.loads((ROOT / "packs/interpret-heart-rate-1.effects.json").read_text(encoding="utf-8"))
SCOPE_MAP = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
POSITIVE = json.loads((PROJECTION / "vectors/positive.json").read_text(encoding="utf-8"))["vectors"]
HEART_RATE_VECTORS = {vector["name"]: vector for vector in POSITIVE if vector["record_type"] == "heart-rate"}

UTC = timezone.utc
HC = "health-connect"
ORIGIN = "org.example.synthetic.source"
SDK = "androidx.health.connect:connect-client:1.1.0"
HEALTH_MARKER = "synthetic-health-marker"
IDENTITY = "synthetic-identity-marker"
SOURCE_IDENTITY = IDENTITY + "-00000000-0000-4000-8000-000000000071"
LMT = "2026-01-15T09:00:00.123456789Z"
VERSION = "interpret-heart-rate@1"
OUTSIDE = "heart-rate-sample-outside-interval"
BPM_RANGE = "heart-rate-bpm-out-of-range"
REASONS = frozenset({OUTSIDE, BPM_RANGE})
COMMON_REASONS = {
    "projection-not-json", "projection-envelope-invalid", "projection-schema-invalid", "instant-out-of-range",
    "interval-reversed", "record-type-not-modeled", "schema-version-not-modeled", "fallback-timezone-policy-missing",
}
SAMPLE_KEYS = {"time", "beats_per_minute", "local_date"}
KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
POINTER = re.compile(r"^/record/samples/(0|[1-9][0-9]*)/(time|beats_per_minute)$")
INSTANT_TEXT = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d{3}|\d{6}|\d{9}))?Z$")
OFFSET_TEXT = re.compile(r"^([+-])(\d\d):(\d\d)(?::(\d\d))?$")
NAIVE_EPOCH = datetime(1970, 1, 1)
GREGORIAN_CYCLE_SECONDS = 146_097 * 86_400
SAFE = 2**53 - 1

# R7: a 100,000-sample series, end to end through `interpret`. A reference implementation took
# about 3 s on the drafting machine; the limit is 20 times that, so CI stays stable.
LARGE_SERIES_SAMPLES = 100_000
LARGE_SERIES_SECONDS = 60.0

START = "2026-01-15T07:30:00Z"
END = "2026-01-15T08:00:00Z"
SAMPLES = (("2026-01-15T07:35:00Z", 72), ("2026-01-15T07:40:00.250Z", 75))


# ---- Independent oracles ----

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def epoch_ns(text: str) -> int:
    """Exact nanoseconds since the Unix epoch, including year 0000."""
    year, month, day, hour, minute, second, fraction = INSTANT_TEXT.match(text).groups()
    year = int(year)
    cycle = 0
    if year == 0:
        year, cycle = 400, GREGORIAN_CYCLE_SECONDS
    whole = datetime(year, int(month), int(day), int(hour), int(minute), int(second))
    seconds = (whole - NAIVE_EPOCH) // timedelta(seconds=1) - cycle
    return seconds * 10**9 + int((fraction or "").ljust(9, "0"))


def offset_seconds(offset: str) -> int:
    sign, hours, minutes, seconds = OFFSET_TEXT.match(offset).groups()
    value = int(hours) * 3600 + int(minutes) * 60 + int(seconds or 0)
    return -value if sign == "-" else value


def shifted_date(text: str, offset: str) -> str | None:
    """The proleptic Gregorian date of the instant shifted by the offset, or None outside years 1-9999."""
    try:
        return (NAIVE_EPOCH + timedelta(seconds=epoch_ns(text) // 10**9 + offset_seconds(offset))).date().isoformat()
    except OverflowError:
        return None


def expected_sample(time_text: str, bpm, start_offset: str, end_offset: str) -> dict:
    out = {"time": time_text, "beats_per_minute": int(bpm)}
    first, second = shifted_date(time_text, start_offset), shifted_date(time_text, end_offset)
    if first is not None and first == second:
        out["local_date"] = first
    return out


def expected_body(samples, start_offset: str, end_offset: str) -> dict:
    return {"samples": [expected_sample(t, b, start_offset, end_offset) for t, b in samples]}


def expected_outcome(samples, start: str, end: str, start_offset: str, end_offset: str):
    """The whole R4/R5 rule: first fault in traversal order, else the bracketed body."""
    low, high = epoch_ns(start), epoch_ns(end)
    for index, (time_text, bpm) in enumerate(samples):
        if not low <= epoch_ns(time_text) <= high:
            return ("quarantine", OUTSIDE, f"/record/samples/{index}/time")
        if not 1 <= bpm <= 300:
            return ("quarantine", BPM_RANGE, f"/record/samples/{index}/beats_per_minute")
    return ("revision", expected_body(samples, start_offset, end_offset))


def instant_text(ns: int, digits: int = 9) -> str:
    seconds, fraction = divmod(ns, 10**9)
    value = NAIVE_EPOCH + timedelta(seconds=seconds)
    text = (f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
            f"T{value.hour:02d}:{value.minute:02d}:{value.second:02d}")
    if digits:
        text += "." + f"{fraction:09d}"[:digits]
    return text + "Z"


def canonical(value) -> str:
    """RFC 8785 for the values a Heart Rate body can hold: objects, lists, ASCII strings, safe integers."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if type(value) is int:
        assert abs(value) <= SAFE
        return str(value)
    if isinstance(value, str):
        assert value.isascii() and value.isprintable() and '"' not in value and "\\" not in value
        return '"' + value + '"'
    if isinstance(value, list):
        return "[" + ",".join(canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda item: list(item[0].encode("utf-16-be")))
        return "{" + ",".join(canonical(k) + ":" + canonical(v) for k, v in items) + "}"
    raise TypeError(type(value).__name__)


def canonical_bytes(value) -> bytes:
    return canonical(value).encode("utf-8")


def json_numbers(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from json_numbers(item)
    elif isinstance(value, list):
        for item in value:
            yield from json_numbers(item)
    elif type(value) in (int, float):
        yield value


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


def heart_rate(samples=SAMPLES, *, start: str = START, end: str = END, start_offset: str | None = "+05:30",
               end_offset: str | None = "+05:30", identity: str = SOURCE_IDENTITY, device: bool = True,
               provider: str = HEALTH_MARKER, recording_method: int = 2, lmt: str = LMT,
               implementation: str = "0.1.0-synthetic", client_record_id: str | None = HEALTH_MARKER) -> dict:
    """A Heart Rate projection serialized against model order at every depth: `record` before `metadata`,
    `samples` before the endpoints, and `beats_per_minute` before `time`."""
    record = {"samples": [{"beats_per_minute": bpm, "time": time_text} for time_text, bpm in samples]}
    if end_offset is not None:
        record["end_zone_offset"] = end_offset
    record["end_time"] = end
    if start_offset is not None:
        record["start_zone_offset"] = start_offset
    record["start_time"] = start
    metadata = {"recording_method": recording_method, "client_record_version": "3"}
    if device:
        metadata["device"] = {"model": HEALTH_MARKER, "manufacturer": HEALTH_MARKER, "type": 1}
    if client_record_id is not None:
        metadata["client_record_id"] = client_record_id
    metadata.update({"last_modified_time": lmt, "data_origin": {"package_name": ORIGIN}, "id": identity})
    return {
        "record": record,
        "metadata": metadata,
        "provider_version": provider,
        "sdk_version": SDK,
        "projection_implementation_version": implementation,
        "projection_schema_version": 1,
        "record_type": "heart-rate",
    }


def truncated(text: str) -> datetime:
    year, month, day, hour, minute, second, fraction = INSTANT_TEXT.match(text).groups()
    return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second),
                    int((fraction or "").ljust(9, "0")[:6]), tzinfo=UTC)


def make_input(projection, **overrides) -> ProcessingInput:
    data = projection if isinstance(projection, bytes) else encode(projection)
    decoded = json.loads(data)
    metadata = decoded["metadata"]
    fields = {
        "source_system_kind": HC,
        "origin": metadata["data_origin"]["package_name"],
        "record_type": "heart-rate",
        "source_identity": metadata["id"],
        "record_id": 71,
        "selection_id": 72,
        "evidence_id": 73,
        "projection_sha256": sha(data),
        "last_modified_time": truncated(metadata["last_modified_time"]),
        "projection_schema_version": 1,
        "projection": data,
    }
    fields.update(overrides)
    return ProcessingInput(**fields)


def run(projection, policies=(), processor=PROCESSOR, **overrides):
    return interpret(make_input(projection, **overrides), processor, list(policies))


def policy(policy_id: str, effective_from: datetime | None, zone: str) -> FallbackTimezonePolicy:
    return FallbackTimezonePolicy(policy_id, effective_from, zone)


UTC_POLICY = policy("policy-synthetic-utc", None, "UTC")
NEW_YORK = policy("policy-synthetic-new-york", None, "America/New_York")
KOLKATA = policy("policy-synthetic-kolkata", None, "Asia/Kolkata")


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
    policy_free = [
        (heart_rate(), ()),
        (heart_rate([("2026-01-15T18:29:59.999999999Z", 61), ("2026-01-15T18:30:00Z", 62)],
                    start="2026-01-15T18:00:00Z", end="2026-01-15T19:00:00Z"), ()),
        (heart_rate([("2026-01-15T22:30:00Z", 70), ("2026-01-15T23:00:00Z", 71)], start="2026-01-15T22:00:00Z",
                    end="2026-01-15T23:30:00Z", start_offset="+01:00", end_offset="-01:00"), ()),
        (heart_rate([("9999-12-31T18:59:59.999999999Z", 80), ("9999-12-31T23:00:00Z", 81)], start="9999-12-31T18:00:00Z",
                    end="9999-12-31T23:30:00Z", start_offset="+05:00", end_offset="+00:00"), ()),
        (heart_rate([("2026-01-15T07:35:00Z", 72), ("2026-01-15T07:35:00Z", 72), ("2026-01-15T07:31:00Z", 60)]), ()),
        (heart_rate([("2026-01-15T07:35:00Z", 72.0), ("2026-01-15T07:36:00Z", 3e2)]), ()),
        (heart_rate([("2026-01-15T07:35:00Z", 72), ("2026-01-15T07:29:59.999999999Z", 73)]), ()),
        (heart_rate([("2026-01-15T07:35:00Z", 72), ("2026-01-15T07:20:00Z", 0)]), ()),
        (heart_rate([("2026-01-15T07:35:00Z", 72.0), ("2026-01-15T07:36:00Z", 301)]), ()),
        (heart_rate([], start=END, end=END), ()),
    ]
    policy_applied = [
        (heart_rate([("1800-01-02T04:56:01Z", 64), ("1800-01-02T04:56:02Z", 65)], start="1800-01-02T04:00:00Z",
                    end="1800-01-02T06:00:00Z", start_offset=None, end_offset=None), (NEW_YORK,)),
        (heart_rate([("2026-03-08T04:30:00Z", 66), ("2026-03-08T05:00:00Z", 67), ("2026-03-08T03:30:00Z", 65)],
                    start="2026-03-08T03:00:00Z", end="2026-03-08T12:00:00Z", start_offset=None, end_offset=None),
         (NEW_YORK,)),
        (heart_rate([("2026-01-15T07:35:00Z", 72)], start_offset=None), (KOLKATA,)),
        (heart_rate([("2026-01-15T07:35:00Z", 72), ("2026-01-15T07:36:00Z", 0)], start_offset=None, end_offset=None),
         (KOLKATA,)),
        (heart_rate([("2026-01-15T08:00:00.000000001Z", 72)], end_offset=None), (UTC_POLICY,)),
    ]
    cases = {"policy-free": policy_free, "policy-applied": policy_applied, "all": policy_free + policy_applied}[phase]
    out = []
    for projection, policies in cases:
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
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(ROOT),
                          timeout=300)
    if done.returncode != 0:
        raise AssertionError(f"subprocess failed ({done.returncode}):\n{done.stderr[-3000:]}")
    return done.stdout.strip().splitlines()[-1]


class Checks(unittest.TestCase):
    def revision(self, projection, policies=(), **overrides) -> CanonicalRevision:
        outcome = run(projection, policies, **overrides)
        self.assertIsInstance(outcome, CanonicalRevision, repr(outcome))
        header = json.loads(outcome.header_bytes)
        self.assertEqual(header["processor_version"], VERSION)
        self.assertEqual(header["body_schema"], {"record_type": "heart-rate", "body_version": 1})
        self.assertEqual(header["body_sha256"], sha(outcome.body_bytes))
        self.assertEqual(outcome.processor_version, VERSION)
        self.assertEqual(outcome.header.body_schema, PROCESSOR.body_schema)
        # The body round-trips to the same RFC 8785 bytes, so it holds only objects, strings, and integers.
        self.assertEqual(canonical_bytes(json.loads(outcome.body_bytes)), outcome.body_bytes)
        self.assertNotIn(b"null", outcome.body_bytes)
        return outcome

    def assert_body(self, projection, expected: dict, policies=()) -> CanonicalRevision:
        outcome = self.revision(projection, policies)
        self.assertEqual(outcome.body_bytes, canonical_bytes(expected))
        return outcome

    def assert_dates(self, projection, dates, policies=()) -> CanonicalRevision:
        """Assert the exact body: every sample unchanged, and exactly these local dates (None means absent)."""
        record = projection["record"]
        samples = [{"time": s["time"], "beats_per_minute": s["beats_per_minute"]} for s in record["samples"]]
        self.assertEqual(len(samples), len(dates))
        for item, date_text in zip(samples, dates):
            if date_text is not None:
                item["local_date"] = date_text
        outcome = self.assert_body(projection, {"samples": samples}, policies)
        body = json.loads(outcome.body_bytes)
        for item, date_text in zip(body["samples"], dates):
            if date_text is None:
                self.assertNotIn("local_date", item)
        return outcome

    def assert_quarantine(self, projection, reason: str, pointer: str, policies=(), identity: str = "none") -> None:
        outcome = run(projection, policies)
        self.assertEqual(outcome, Quarantine(reason, pointer, VERSION, identity))

    def assert_oracle(self, samples, start, end, start_offset, end_offset, policies=(), applied=None) -> None:
        """Compare one outcome with the whole-rule oracle; `applied` gives the applied offsets when inferred."""
        projection = heart_rate(samples, start=start, end=end, start_offset=start_offset, end_offset=end_offset)
        start_applied, end_applied = applied or (start_offset, end_offset)
        expected = expected_outcome(samples, start, end, start_applied, end_applied)
        outcome = run(projection, policies)
        if expected[0] == "revision":
            self.assertIsInstance(outcome, CanonicalRevision, repr(outcome))
            self.assertEqual(outcome.body_bytes, canonical_bytes(expected[1]))
        else:
            self.assertIsInstance(outcome, Quarantine, repr(outcome))
            self.assertEqual((outcome.reason, outcome.pointer, outcome.processor_version), (expected[1], expected[2], VERSION))

    def assert_health_free(self, text: str) -> None:
        self.assertNotIn(HEALTH_MARKER, text)
        self.assertNotIn(IDENTITY, text)


# ---- R1 ----

class R1GovernedScopeContract(unittest.TestCase):
    def test_contract_shape_and_dependency(self) -> None:
        self.assertEqual((CONTRACT["scope"], CONTRACT["version"], CONTRACT["kind"]), ("interpret-heart-rate", "1", "leaf"))
        self.assertEqual(CONTRACT["dependencies"], [{"scope": "interpret-source-projection", "version": "1"}])
        self.assertEqual(CONTRACT["named_failures"], [])
        self.assertEqual(CONTRACT["observable_effects"], [])
        self.assertEqual({item["mode"] for item in CONTRACT["inputs"]}, {"pure"})
        outputs = {item["name"]: item["description"] for item in CONTRACT["outputs"]}
        named = set(re.findall(r"\bheart-rate-[a-z0-9-]*[a-z0-9]\b", outputs["quarantine_outcome"]))
        self.assertEqual(named, set(REASONS))
        self.assertNotIn("heart-rate-interval-empty", json.dumps(CONTRACT))
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["specification"], "packs/interpret-heart-rate-1.spec.md")
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_interpret_heart_rate_1.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_interpret_heart_rate_1.sh")
        self.assertEqual(oracle["languages"], ["python"])

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        row = next(line for line in SCOPE_MAP.splitlines() if line.startswith("| `interpret-heart-rate` |"))
        self.assertIn("| leaf |", row)
        for phrase in ("Heart Rate", "without interpolation", "Canonical Revision", "non-success outcome"):
            self.assertIn(phrase, row)
        self.assertTrue(row.rstrip().endswith("| `interpret-source-projection` |"), row)

    def test_effects_declaration_is_empty(self) -> None:
        self.assertEqual(EFFECTS, {"effects_format": 1, "effects": []})


# ---- R2 ----

class R2ProcessorIdentity(unittest.TestCase):
    def test_processor_identity(self) -> None:
        self.assertIsInstance(PROCESSOR, Processor)
        self.assertEqual(PROCESSOR.record_type, "heart-rate")
        self.assertEqual(PROCESSOR.processor_version, VERSION)
        self.assertEqual(PROCESSOR.processor_version, f"{CONTRACT['scope']}@{CONTRACT['version']}")
        self.assertIsInstance(PROCESSOR.body_schema, BodySchema)
        self.assertEqual(PROCESSOR.body_schema,
                         BodySchema("heart-rate", 1, (NumericField("/samples/*/beats_per_minute", "integer", "1/min"),)))
        self.assertIs(type(PROCESSOR.body_schema.body_version), int)
        self.assertIsInstance(PROCESSOR.body_schema.numeric_fields, tuple)
        self.assertIn("1/min", UNITS)
        self.assertTrue(callable(PROCESSOR.interpret_body))
        assert_identity_is_strict(self)
        self.assertEqual(PROCESSOR.processor_version, VERSION)

    def test_declared_reasons_are_exact_and_not_common(self) -> None:
        self.assertIsInstance(PROCESSOR.quarantine_reasons, frozenset)
        self.assertEqual(PROCESSOR.quarantine_reasons, REASONS)
        for reason in PROCESSOR.quarantine_reasons:
            with self.subTest(reason):
                self.assertRegex(reason, KEBAB)
                self.assertTrue(reason.startswith("heart-rate-"), reason)
                self.assertNotIn(reason, COMMON_REASONS)
        self.assertNotIn("heart-rate-interval-empty", PROCESSOR.quarantine_reasons)


# ---- R3 ----

class R3Body(Checks):
    def test_positive_vectors_become_revisions(self) -> None:
        self.assertLessEqual({"heart-rate-minimal", "heart-rate-maximal", "heart-rate-unsorted-samples",
                              "heart-rate-instant-precision"}, set(HEART_RATE_VECTORS))
        for name, vector in sorted(HEART_RATE_VECTORS.items()):
            with self.subTest(name):
                data = vector["bytes"].encode("utf-8")
                self.assertEqual(sha(data), vector["sha256"])
                record = json.loads(data)["record"]
                start_offset = record.get("start_zone_offset", "+00:00")
                end_offset = record.get("end_zone_offset", "+00:00")
                outcome = self.revision(data, (UTC_POLICY,))
                samples = [(s["time"], s["beats_per_minute"]) for s in record["samples"]]
                self.assertEqual(outcome.body_bytes, canonical_bytes(expected_body(samples, start_offset, end_offset)))
                for sample in json.loads(outcome.body_bytes)["samples"]:
                    self.assertIn("local_date", sample)
        minimal = HEART_RATE_VECTORS["heart-rate-minimal"]["bytes"].encode("utf-8")
        self.assertEqual(self.revision(minimal, (UTC_POLICY,)).body_bytes,
                         b'{"samples":[{"beats_per_minute":72,"local_date":"2026-01-15","time":"2026-01-15T07:35:00Z"},'
                         b'{"beats_per_minute":75,"local_date":"2026-01-15","time":"2026-01-15T07:40:00Z"}]}')
        # A synthetic Fallback Timezone Policy supplies the offsets the vector lacks: -08:00 in Los Angeles.
        los_angeles = self.revision(minimal, (policy("policy-synthetic-los-angeles", None, "America/Los_Angeles"),))
        self.assertEqual(los_angeles.body_bytes,
                         b'{"samples":[{"beats_per_minute":72,"local_date":"2026-01-14","time":"2026-01-15T07:35:00Z"},'
                         b'{"beats_per_minute":75,"local_date":"2026-01-14","time":"2026-01-15T07:40:00Z"}]}')
        self.assertIn("policy-synthetic-los-angeles", los_angeles.policy_identity)
        maximal = HEART_RATE_VECTORS["heart-rate-maximal"]["bytes"].encode("utf-8")
        outcome = self.revision(maximal)
        self.assertEqual(outcome.policy_identity, "none")
        self.assertEqual(outcome.body_bytes,
                         b'{"samples":[{"beats_per_minute":72,"local_date":"2026-01-15","time":"2026-01-15T07:35:00Z"},'
                         b'{"beats_per_minute":75,"local_date":"2026-01-15","time":"2026-01-15T07:40:00Z"}]}')

    def test_samples_keep_exact_text_and_values(self) -> None:
        precision = HEART_RATE_VECTORS["heart-rate-instant-precision"]["bytes"].encode("utf-8")
        self.assertEqual(self.revision(precision).body_bytes,
                         b'{"samples":[{"beats_per_minute":72,"local_date":"2026-01-15","time":"2026-01-15T07:35:00.000001Z"},'
                         b'{"beats_per_minute":75,"local_date":"2026-01-15","time":"2026-01-15T07:40:00.999999999Z"}]}')
        samples = [
            ("2026-01-15T07:31:00Z", 1), ("2026-01-15T07:32:00.000Z", 300), ("2026-01-15T07:33:00.000000Z", 59),
            ("2026-01-15T07:34:00.000000000Z", 61), ("2026-01-15T07:35:00.100Z", 100),
            ("2026-01-15T07:36:00.120000Z", 299), ("2026-01-15T07:37:00.123456780Z", 2),
            ("2026-01-15T07:38:00.999999999Z", 180), ("2026-01-15T07:39:00.000000001Z", 181),
        ]
        projection = heart_rate(samples)
        outcome = self.assert_body(projection, expected_body(samples, "+05:30", "+05:30"))
        body = json.loads(outcome.body_bytes)
        self.assertEqual([item["time"] for item in body["samples"]], [t for t, _ in samples])
        self.assertEqual([item["beats_per_minute"] for item in body["samples"]], [b for _, b in samples])
        self.assertEqual(list(json_numbers(body)), [b for _, b in samples])
        # An integral JSON number the closed schema accepts as an integer is the same value, written as an integer.
        floats = heart_rate([("2026-01-15T07:35:00Z", 72.0), ("2026-01-15T07:36:00Z", 300.0), ("2026-01-15T07:37:00Z", 1.0)])
        self.assertIn(b'"beats_per_minute":72.0', encode(floats))
        self.assert_body(floats, expected_body([("2026-01-15T07:35:00Z", 72), ("2026-01-15T07:36:00Z", 300),
                                                ("2026-01-15T07:37:00Z", 1)], "+05:30", "+05:30"))

    def test_samples_keep_source_order(self) -> None:
        unsorted = HEART_RATE_VECTORS["heart-rate-unsorted-samples"]["bytes"].encode("utf-8")
        self.assertEqual(self.revision(unsorted).body_bytes,
                         b'{"samples":[{"beats_per_minute":90,"local_date":"2026-01-15","time":"2026-01-15T07:50:00Z"},'
                         b'{"beats_per_minute":72,"local_date":"2026-01-15","time":"2026-01-15T07:35:00Z"},'
                         b'{"beats_per_minute":75,"local_date":"2026-01-15","time":"2026-01-15T07:40:00Z"}]}')
        # Neither time order nor value order, and dates that are not monotonic in list order.
        samples = [("2026-01-15T18:45:00Z", 64), ("2026-01-15T18:05:00Z", 99), ("2026-01-15T18:50:00.500Z", 61),
                   ("2026-01-15T18:10:00Z", 120), ("2026-01-15T18:31:00Z", 63)]
        projection = heart_rate(samples, start="2026-01-15T18:00:00Z", end="2026-01-15T19:00:00Z")
        self.assert_dates(projection, ["2026-01-16", "2026-01-15", "2026-01-16", "2026-01-15", "2026-01-16"])

    def test_equal_time_samples_are_all_kept(self) -> None:
        samples = [("2026-01-15T07:40:00Z", 80), ("2026-01-15T07:40:00Z", 80), ("2026-01-15T07:40:00Z", 95),
                   ("2026-01-15T07:40:00.000Z", 80), ("2026-01-15T07:40:00.000000000Z", 70),
                   ("2026-01-15T07:35:00Z", 72), ("2026-01-15T07:40:00Z", 80)]
        outcome = self.assert_body(heart_rate(samples), expected_body(samples, "+05:30", "+05:30"))
        self.assertEqual(len(json.loads(outcome.body_bytes)["samples"]), 7)

    def test_empty_samples_give_an_empty_list(self) -> None:
        cases = [
            (heart_rate([]), ()),
            (heart_rate([], start_offset=None, end_offset=None), (NEW_YORK,)),
            (heart_rate([], start=END, end=END), ()),
            (heart_rate([], start_offset="+14:00", end_offset="-12:00"), ()),
        ]
        for projection, policies in cases:
            with self.subTest(projection["record"]):
                self.assertEqual(self.revision(projection, policies).body_bytes, b'{"samples":[]}')

    def test_body_has_no_header_fields(self) -> None:
        # The series spans local midnight, but its samples all fall on the start's local date.
        samples = [("2026-01-15T18:10:00Z", 70), ("2026-01-15T18:20:00.123456789Z", 71)]
        start, end = "2026-01-15T18:00:00.111Z", "2026-01-15T19:00:00.222Z"
        for start_offset, end_offset, policies in (("+05:30", "+05:30", ()), (None, None, (KOLKATA,))):
            with self.subTest(start_offset=start_offset):
                projection = heart_rate(samples, start=start, end=end, start_offset=start_offset, end_offset=end_offset)
                outcome = self.assert_dates(projection, ["2026-01-15", "2026-01-15"], policies)
                body = json.loads(outcome.body_bytes)
                self.assertEqual(set(body), {"samples"})
                for item in body["samples"]:
                    self.assertLessEqual(set(item), SAMPLE_KEYS)
                header = json.loads(outcome.header_bytes)
                self.assertEqual(header["time"]["end"]["local_date"], "2026-01-16")
                strings = set(json_strings(body))
                for value in (start, end, "+05:30", "2026-01-16", ORIGIN, SOURCE_IDENTITY, LMT, HEALTH_MARKER, SDK,
                              VERSION, "heart-rate", "inferred", "experienced", KOLKATA.policy_id, outcome.policy_identity,
                              "interval", "start_time", "end_time", "start_zone_offset", "end_zone_offset", "applied_offset",
                              "attribution", "policy_id", "source_identity", "record_type", "explicit_time"):
                    self.assertNotIn(value, strings)
                self.assertNotIn(b"policy", outcome.body_bytes)
                self.assertNotIn(b"inferred", outcome.body_bytes)


# ---- R4 ----

class R4BracketedLocalDates(Checks):
    def test_equal_offsets_date_every_sample(self) -> None:
        cases = [
            # +05:30 across local midnight.
            (heart_rate([("2026-01-15T18:29:59.999999999Z", 61), ("2026-01-15T18:30:00Z", 62), ("2026-01-15T18:00:00Z", 63)],
                        start="2026-01-15T18:00:00Z", end="2026-01-15T19:00:00Z"),
             ["2026-01-15", "2026-01-16", "2026-01-15"]),
            # A negative offset.
            (heart_rate([("2026-01-15T07:59:59.999999999Z", 64), ("2026-01-15T08:00:00Z", 65)], start="2026-01-15T07:00:00Z",
                        end="2026-01-15T09:00:00Z", start_offset="-08:00", end_offset="-08:00"),
             ["2026-01-14", "2026-01-15"]),
            # +00:00 and -00:00 are the same offset written differently.
            (heart_rate([("2026-01-15T23:59:59.999999999Z", 66), ("2026-01-16T00:00:00Z", 67)], start="2026-01-15T23:00:00Z",
                        end="2026-01-16T01:00:00Z", start_offset="+00:00", end_offset="-00:00"),
             ["2026-01-15", "2026-01-16"]),
            # The extreme offsets.
            (heart_rate([("2026-01-15T05:59:59Z", 68), ("2026-01-15T06:00:00Z", 69)], start="2026-01-15T05:00:00Z",
                        end="2026-01-15T07:00:00Z", start_offset="+18:00", end_offset="+18:00"),
             ["2026-01-15", "2026-01-16"]),
            (heart_rate([("2026-01-15T17:59:59Z", 70), ("2026-01-15T18:00:00Z", 71)], start="2026-01-15T17:00:00Z",
                        end="2026-01-15T19:00:00Z", start_offset="-18:00", end_offset="-18:00"),
             ["2026-01-14", "2026-01-15"]),
            # Leap years in the proleptic Gregorian calendar.
            (heart_rate([("2024-02-28T12:00:00Z", 72), ("2024-02-29T12:00:00Z", 73)], start="2024-02-28T00:00:00Z",
                        end="2024-03-01T00:00:00Z", start_offset="+12:00", end_offset="+12:00"),
             ["2024-02-29", "2024-03-01"]),
            (heart_rate([("2100-02-28T12:00:00Z", 74)], start="2100-02-28T00:00:00Z", end="2100-03-01T00:00:00Z",
                        start_offset="+12:00", end_offset="+12:00"), ["2100-03-01"]),
            (heart_rate([("2000-02-28T12:00:00Z", 75), ("1900-02-28T12:00:00Z", 76)], start="1900-02-28T00:00:00Z",
                        end="2000-03-01T00:00:00Z", start_offset="+12:00", end_offset="+12:00"), ["2000-02-29", "1900-03-01"]),
            (heart_rate([("1999-12-31T12:00:00Z", 77), ("1600-02-28T12:00:00Z", 78)], start="1600-01-01T00:00:00Z",
                        end="2000-01-01T00:00:00Z", start_offset="+12:00", end_offset="+12:00"), ["2000-01-01", "1600-02-29"]),
            # The first and last representable dates.
            (heart_rate([("0001-01-01T00:00:00Z", 79), ("0001-03-01T00:00:00.500Z", 80)],
                        start="0001-01-01T00:00:00Z", end="0001-12-31T00:00:00Z", start_offset="+00:00", end_offset="+00:00"),
             ["0001-01-01", "0001-03-01"]),
            (heart_rate([("9999-12-31T23:59:59.999999999Z", 81)], start="9999-12-31T00:00:00Z",
                        end="9999-12-31T23:59:59.999999999Z", start_offset="+00:00", end_offset="+00:00"), ["9999-12-31"]),
            (heart_rate([("1969-12-31T23:59:59.999999999Z", 82), ("1970-01-01T00:00:00Z", 83)], start="1969-12-31T23:00:00Z",
                        end="1970-01-01T01:00:00Z", start_offset="+00:00", end_offset="+00:00"), ["1969-12-31", "1970-01-01"]),
        ]
        for projection, dates in cases:
            with self.subTest(record=projection["record"]):
                self.assert_dates(projection, dates)

    def test_differing_offsets_date_only_agreeing_samples(self) -> None:
        cases = [
            # Travel west: the endpoints' local dates are equal, yet samples between them straddle midnight.
            (heart_rate([("2026-01-15T22:30:00Z", 70), ("2026-01-15T22:59:59.999999999Z", 71), ("2026-01-15T23:00:00Z", 72),
                         ("2026-01-15T23:15:00Z", 73), ("2026-01-15T22:00:00Z", 74)],
                        start="2026-01-15T22:00:00Z", end="2026-01-15T23:30:00Z", start_offset="+01:00", end_offset="-01:00"),
             ["2026-01-15", "2026-01-15", None, None, "2026-01-15"]),
            # Spring forward in New York.
            (heart_rate([("2026-03-08T03:59:59.999999999Z", 60), ("2026-03-08T04:00:00Z", 61), ("2026-03-08T04:59:59.999999999Z", 62),
                         ("2026-03-08T05:00:00Z", 63), ("2026-03-08T11:00:00Z", 64)],
                        start="2026-03-08T03:00:00Z", end="2026-03-08T12:00:00Z", start_offset="-05:00", end_offset="-04:00"),
             ["2026-03-07", None, None, "2026-03-08", "2026-03-08"]),
            # Fall back in New York.
            (heart_rate([("2026-11-01T03:59:59.999999999Z", 65), ("2026-11-01T04:00:00Z", 66), ("2026-11-01T04:59:59.999999999Z", 67),
                         ("2026-11-01T05:00:00Z", 68)],
                        start="2026-11-01T02:00:00Z", end="2026-11-01T08:00:00Z", start_offset="-04:00", end_offset="-05:00"),
             ["2026-10-31", None, None, "2026-11-01"]),
            # One minute apart.
            (heart_rate([("2026-01-15T18:28:59.999999999Z", 69), ("2026-01-15T18:29:00Z", 70), ("2026-01-15T18:29:59.999999999Z", 71),
                         ("2026-01-15T18:30:00Z", 72)],
                        start="2026-01-15T18:00:00Z", end="2026-01-15T19:00:00Z", start_offset="+05:30", end_offset="+05:31"),
             ["2026-01-15", None, None, "2026-01-16"]),
            # More than a day apart: no sample can be dated.
            (heart_rate([("2026-01-15T00:00:00Z", 73), ("2026-01-15T03:00:00Z", 74), ("2026-01-15T06:00:00Z", 75)],
                        start="2026-01-15T00:00:00Z", end="2026-01-15T06:00:00Z", start_offset="+14:00", end_offset="-12:00"),
             [None, None, None]),
            # A zero-length series whose endpoints disagree.
            (heart_rate([("2026-01-15T20:00:00Z", 76), ("2026-01-15T20:00:00.000Z", 77)],
                        start="2026-01-15T20:00:00Z", end="2026-01-15T20:00:00Z", start_offset="+05:30", end_offset="-05:00"),
             [None, None]),
            # Offsets that differ but agree on every sample.
            (heart_rate([("2026-01-15T10:00:00Z", 78), ("2026-01-15T12:00:00Z", 79)],
                        start="2026-01-15T09:00:00Z", end="2026-01-15T13:00:00Z", start_offset="+09:00", end_offset="-09:00"),
             ["2026-01-15", "2026-01-15"]),
        ]
        for projection, dates in cases:
            with self.subTest(record=projection["record"]):
                self.assert_dates(projection, dates)
        # Seeded synthetic series against the whole-rule oracle.
        generator = random.Random(20261124)
        offsets = ["+00:00", "-00:00", "+05:30", "+05:45", "-03:30", "+01:00", "-01:00", "+14:00", "-12:00",
                   "+18:00", "-18:00", "+09:00", "-08:00", "+00:01", "-00:01"]
        base = epoch_ns("2026-01-01T00:00:00Z")
        for case in range(150):
            low = base + generator.randrange(360 * 86_400 * 10**9)
            high = low + generator.choice([0, 1, 10**9, 3600 * 10**9, generator.randrange(3 * 86_400 * 10**9)])
            start, end = instant_text(low), instant_text(high)
            samples = []
            for _ in range(generator.randrange(0, 12)):
                at = generator.choice([low, high, generator.randint(low, high)])
                text = instant_text(at, generator.choice([0, 3, 6, 9]))
                samples.append((text if epoch_ns(text) >= low else instant_text(at), generator.randint(1, 300)))
            with self.subTest(case=case):
                self.assert_oracle(samples, start, end, generator.choice(offsets), generator.choice(offsets))

    def test_seconds_offsets_are_exact(self) -> None:
        # Both endpoints inferred through New York in 1800, at -04:56:02.
        projection = heart_rate([("1800-01-02T04:56:00Z", 60), ("1800-01-02T04:56:01Z", 61), ("1800-01-02T04:56:01.999999999Z", 62),
                                 ("1800-01-02T04:56:02Z", 63), ("1800-01-02T04:00:00Z", 64)],
                                start="1800-01-02T04:00:00Z", end="1800-01-02T06:00:00Z", start_offset=None, end_offset=None)
        outcome = self.assert_dates(projection, ["1800-01-01", "1800-01-01", "1800-01-01", "1800-01-02", "1800-01-01"],
                                    (NEW_YORK,))
        self.assertEqual((outcome.header.time.start.applied_offset, outcome.header.time.end.applied_offset),
                         ("-04:56:02", "-04:56:02"))
        # Start inferred through Kolkata in 1800 at +05:53:28; end experienced at +05:53.
        projection = heart_rate([("1800-01-01T18:06:31Z", 65), ("1800-01-01T18:06:32Z", 66), ("1800-01-01T18:06:59.999999999Z", 67),
                                 ("1800-01-01T18:07:00Z", 68)],
                                start="1800-01-01T17:00:00Z", end="1800-01-01T19:00:00Z", start_offset=None, end_offset="+05:53")
        outcome = self.assert_dates(projection, ["1800-01-01", None, None, "1800-01-02"], (KOLKATA,))
        self.assertEqual((outcome.header.time.start.applied_offset, outcome.header.time.end.applied_offset),
                         ("+05:53:28", "+05:53"))
        # Start experienced at -04:56; end inferred through New York at -04:56:02.
        projection = heart_rate([("1800-01-02T04:55:59.999999999Z", 69), ("1800-01-02T04:56:00Z", 70), ("1800-01-02T04:56:01Z", 71),
                                 ("1800-01-02T04:56:01.999999999Z", 72), ("1800-01-02T04:56:02Z", 73)],
                                start="1800-01-02T04:00:00Z", end="1800-01-02T06:00:00Z", start_offset="-04:56", end_offset=None)
        outcome = self.assert_dates(projection, ["1800-01-01", None, None, None, "1800-01-02"], (NEW_YORK,))
        self.assertEqual((outcome.header.time.start.applied_offset, outcome.header.time.end.applied_offset),
                         ("-04:56", "-04:56:02"))
        # Kolkata in 1900, +05:21:10, at both endpoints.
        projection = heart_rate([("1900-01-01T18:38:49Z", 74), ("1900-01-01T18:38:50Z", 75)],
                                start="1900-01-01T18:00:00Z", end="1900-01-01T19:00:00Z", start_offset=None, end_offset=None)
        outcome = self.assert_dates(projection, ["1900-01-01", "1900-01-02"], (KOLKATA,))
        self.assertEqual(outcome.header.time.end.applied_offset, "+05:21:10")

    def test_out_of_range_shift_omits_the_date(self) -> None:
        cases = [
            # Past 9999-12-31 under the start offset only.
            (heart_rate([("9999-12-31T18:00:00Z", 60), ("9999-12-31T18:59:59.999999999Z", 61), ("9999-12-31T19:00:00Z", 62),
                         ("9999-12-31T23:00:00Z", 63), ("9999-12-31T23:30:00Z", 64)],
                        start="9999-12-31T18:00:00Z", end="9999-12-31T23:30:00Z", start_offset="+05:00", end_offset="+00:00"),
             ["9999-12-31", "9999-12-31", None, None, None]),
            # Before 0001-01-01 under the end offset only.
            (heart_rate([("0001-01-01T00:00:00Z", 65), ("0001-01-01T04:59:59.999999999Z", 66), ("0001-01-01T05:00:00Z", 67),
                         ("0001-01-01T12:00:00Z", 68)],
                        start="0001-01-01T00:00:00Z", end="0001-01-01T12:00:00Z", start_offset="+00:00", end_offset="-05:00"),
             [None, None, "0001-01-01", "0001-01-01"]),
            # One minute past each end.
            (heart_rate([("0001-01-01T00:00:59Z", 69), ("0001-01-01T00:01:00Z", 70)],
                        start="0001-01-01T00:00:00Z", end="0001-01-01T00:02:00Z", start_offset="+00:00", end_offset="-00:01"),
             [None, "0001-01-01"]),
            (heart_rate([("9999-12-31T23:58:59Z", 71), ("9999-12-31T23:59:00Z", 72)],
                        start="9999-12-31T23:58:00Z", end="9999-12-31T23:59:30Z", start_offset="+00:01", end_offset="+00:00"),
             ["9999-12-31", None]),
            # The extreme offsets at the end of the range.
            (heart_rate([("9999-12-31T05:59:59.999999999Z", 73), ("9999-12-31T06:00:00Z", 74), ("9999-12-31T23:59:59.999999999Z", 75)],
                        start="9999-12-31T05:00:00Z", end="9999-12-31T23:59:59.999999999Z", start_offset="+18:00",
                        end_offset="-18:00"),
             [None, None, None]),
        ]
        for projection, dates in cases:
            with self.subTest(record=projection["record"]):
                self.assert_dates(projection, dates)

    def test_inferred_endpoints_bracket_like_experienced_ones(self) -> None:
        samples = [("2026-03-08T03:59:59.999999999Z", 60), ("2026-03-08T04:00:00Z", 61), ("2026-03-08T04:30:00Z", 62),
                   ("2026-03-08T05:00:00Z", 63), ("2026-03-08T03:00:00Z", 64)]
        dates = ["2026-03-07", None, None, "2026-03-08", "2026-03-07"]
        series = dict(start="2026-03-08T03:00:00Z", end="2026-03-08T12:00:00Z")
        experienced = self.assert_dates(heart_rate(samples, start_offset="-05:00", end_offset="-04:00", **series), dates)
        inferred = self.assert_dates(heart_rate(samples, start_offset=None, end_offset=None, **series), dates, (NEW_YORK,))
        start_only = self.assert_dates(heart_rate(samples, start_offset=None, end_offset="-04:00", **series), dates, (NEW_YORK,))
        end_only = self.assert_dates(heart_rate(samples, start_offset="-05:00", end_offset=None, **series), dates, (NEW_YORK,))
        for outcome in (inferred, start_only, end_only):
            self.assertEqual(outcome.body_bytes, experienced.body_bytes)
            self.assertIn(NEW_YORK.policy_id, outcome.policy_identity)
        self.assertEqual(experienced.policy_identity, "none")
        self.assertEqual((inferred.header.time.start.attribution, inferred.header.time.end.attribution), ("inferred", "inferred"))
        self.assertEqual((inferred.header.time.start.applied_offset, inferred.header.time.end.applied_offset), ("-05:00", "-04:00"))
        self.assertEqual((start_only.header.time.start.attribution, start_only.header.time.end.attribution), ("inferred", "experienced"))
        # The same zone under another policy name gives the same body under a different identity.
        renamed = policy("policy-synthetic-renamed", None, "America/New_York")
        other = self.revision(heart_rate(samples, start_offset=None, end_offset=None, **series), (renamed,))
        self.assertEqual(other.body_bytes, inferred.body_bytes)
        self.assertNotEqual(other.policy_identity, inferred.policy_identity)
        self.assertNotEqual(other.revision_sha256, inferred.revision_sha256)
        # A policy that changes the applied offsets changes the body: Chicago moves its clocks an hour later.
        chicago = self.assert_dates(heart_rate(samples, start_offset=None, end_offset=None, **series),
                                    ["2026-03-07", "2026-03-07", "2026-03-07", None, "2026-03-07"],
                                    (policy("policy-synthetic-chicago", None, "America/Chicago"),))
        self.assertEqual((chicago.header.time.start.applied_offset, chicago.header.time.end.applied_offset), ("-06:00", "-05:00"))
        self.assertNotEqual(chicago.body_bytes, inferred.body_bytes)
        utc = self.assert_dates(heart_rate(samples, start_offset=None, end_offset=None, **series), ["2026-03-08"] * 5,
                                (UTC_POLICY,))
        self.assertNotEqual(utc.revision_sha256, inferred.revision_sha256)

    def test_unused_policy_leaves_the_body_unchanged(self) -> None:
        samples = [("2026-01-15T18:29:59.999999999Z", 61), ("2026-01-15T18:30:00Z", 62)]
        series = dict(start="2026-01-15T18:00:00Z", end="2026-01-15T19:00:00Z")
        experienced = heart_rate(samples, **series)
        base = self.assert_dates(experienced, ["2026-01-15", "2026-01-16"])
        for policies in ((policy("policy-synthetic-kiritimati", None, "Pacific/Kiritimati"),),
                         (UTC_POLICY, policy("policy-synthetic-later", datetime(2026, 6, 1, tzinfo=UTC), "Asia/Tokyo"))):
            with self.subTest(policies=[p.policy_id for p in policies]):
                outcome = self.revision(experienced, policies)
                self.assertEqual(outcome, base)
                self.assertEqual((outcome.header_bytes, outcome.body_bytes, outcome.policy_identity),
                                 (base.header_bytes, base.body_bytes, "none"))
        inferred = heart_rate(samples, start_offset=None, end_offset=None, **series)
        used = self.assert_dates(inferred, ["2026-01-15", "2026-01-16"], (KOLKATA,))
        later = policy("policy-synthetic-later", datetime(2026, 1, 15, 19, 0, 0, 1000, tzinfo=UTC), "Asia/Tokyo")
        with_later = self.revision(inferred, (KOLKATA, later))
        self.assertEqual(with_later, used)
        self.assertEqual(with_later.body_bytes, base.body_bytes)
        # A policy that applies but leaves the offsets unchanged leaves the body unchanged.
        london = self.revision(heart_rate([("2026-01-15T23:59:59.999999999Z", 63), ("2026-01-16T00:00:00Z", 64)],
                                          start="2026-01-15T23:00:00Z", end="2026-01-16T01:00:00Z",
                                          start_offset=None, end_offset=None),
                               (policy("policy-synthetic-london", None, "Europe/London"),))
        utc = self.revision(heart_rate([("2026-01-15T23:59:59.999999999Z", 63), ("2026-01-16T00:00:00Z", 64)],
                                       start="2026-01-15T23:00:00Z", end="2026-01-16T01:00:00Z",
                                       start_offset=None, end_offset=None), (UTC_POLICY,))
        self.assertEqual(london.body_bytes, utc.body_bytes)
        self.assertEqual(london.body_bytes, canonical_bytes(expected_body(
            [("2026-01-15T23:59:59.999999999Z", 63), ("2026-01-16T00:00:00Z", 64)], "+00:00", "+00:00")))
        self.assertNotEqual(london.policy_identity, utc.policy_identity)


# ---- R5 ----

class R5Validity(Checks):
    def test_sample_outside_interval_is_quarantined(self) -> None:
        start, end = "2026-01-15T07:30:00.123456789Z", "2026-01-15T08:00:00.987654321Z"
        inside = [("2026-01-15T07:40:00Z", 70), ("2026-01-15T07:50:00Z", 71), (start, 72)]
        for outside in ("2026-01-15T07:30:00.123456788Z", "2026-01-15T08:00:00.987654322Z", "2026-01-15T07:30:00.123456Z",
                        "2026-01-15T07:30:00.123Z", "2026-01-15T07:30:00Z", "2026-01-15T08:00:01Z", "1970-01-01T00:00:00Z",
                        "0001-01-01T00:00:00Z", "9999-12-31T23:59:59.999999999Z", "2026-01-15T01:59:59.999999999Z"):
            with self.subTest(outside):
                self.assert_quarantine(heart_rate(inside + [(outside, 73)], start=start, end=end), OUTSIDE,
                                       "/record/samples/3/time")
                self.assert_quarantine(heart_rate([(outside, 73)] + inside, start=start, end=end), OUTSIDE,
                                       "/record/samples/0/time")
        # Millisecond and microsecond fractions are not nanoseconds.
        self.assert_quarantine(heart_rate([("2026-01-15T07:30:00.000000124Z", 70)], start="2026-01-15T07:30:00.123Z"),
                               OUTSIDE, "/record/samples/0/time")
        self.revision(heart_rate([("2026-01-15T08:00:00.000000124Z", 70)], end="2026-01-15T08:00:00.000123Z"))
        self.revision(heart_rate([("2026-01-15T07:30:00.123000001Z", 70)], start="2026-01-15T07:30:00.123Z"))
        # With inferred endpoints, the Quarantine carries the identity of the policies applied.
        identity = self.revision(heart_rate(inside, start=start, end=end, start_offset=None, end_offset=None),
                                 (KOLKATA,)).policy_identity
        self.assertIn(KOLKATA.policy_id, identity)
        self.assert_quarantine(heart_rate(inside + [(end.replace("321Z", "322Z"), 73)], start=start, end=end,
                                          start_offset=None, end_offset=None),
                               OUTSIDE, "/record/samples/3/time", (KOLKATA,), identity)

    def test_interval_bounds_are_inclusive(self) -> None:
        start, end = "2026-01-15T07:30:00.100Z", "2026-01-15T08:00:00.000000000Z"
        samples = [("2026-01-15T07:30:00.100000000Z", 60), ("2026-01-15T07:30:00.100000Z", 61), (start, 62),
                   ("2026-01-15T08:00:00Z", 63), ("2026-01-15T08:00:00.000Z", 64), ("2026-01-15T08:00:00.000000Z", 65), (end, 66),
                   ("2026-01-15T07:30:00.100000001Z", 67), ("2026-01-15T07:59:59.999999999Z", 68)]
        self.assert_body(heart_rate(samples, start=start, end=end), expected_body(samples, "+05:30", "+05:30"))
        # And the other way round: the start written with nine digits, the end with none.
        start, end = "2026-01-15T07:30:00.000000000Z", "2026-01-15T08:00:00.500Z"
        samples = [("2026-01-15T07:30:00Z", 69), ("2026-01-15T07:30:00.000Z", 70), ("2026-01-15T08:00:00.500000000Z", 71),
                   ("2026-01-15T08:00:00.500000Z", 72)]
        self.assert_body(heart_rate(samples, start=start, end=end), expected_body(samples, "+05:30", "+05:30"))

    def test_bpm_bounds_are_the_pinned_sdk_bounds(self) -> None:
        self.assertIs(type(BPM_MIN), int)
        self.assertIs(type(BPM_MAX), int)
        self.assertEqual((BPM_MIN, BPM_MAX), (1, 300))
        samples = [("2026-01-15T07:35:00Z", BPM_MIN), ("2026-01-15T07:36:00Z", BPM_MAX), ("2026-01-15T07:37:00Z", 2),
                   ("2026-01-15T07:38:00Z", 299)]
        self.assert_body(heart_rate(samples), expected_body(samples, "+05:30", "+05:30"))

    def test_bpm_outside_bounds_is_quarantined(self) -> None:
        inside = [("2026-01-15T07:35:00Z", 72), ("2026-01-15T07:36:00Z", 300)]
        for value in (0, -1, 301, 302, 987654, -SAFE, SAFE, 0.0, 301.0, -1.0):
            with self.subTest(value=value):
                self.assert_quarantine(heart_rate(inside + [("2026-01-15T07:37:00Z", value)] + inside), BPM_RANGE,
                                       "/record/samples/2/beats_per_minute")
                self.assert_quarantine(heart_rate([("2026-01-15T07:37:00Z", value)]), BPM_RANGE,
                                       "/record/samples/0/beats_per_minute")

    def test_year_zero_sample_is_outside_the_interval(self) -> None:
        series = dict(start="0001-01-01T00:00:00Z", end="0001-01-01T01:00:00Z", start_offset="+14:00", end_offset="+00:00")
        for year_zero in ("0000-12-31T23:59:59.999999999Z", "0000-12-31T23:00:00Z", "0000-02-29T12:00:00Z",
                          "0000-01-01T00:00:00Z", "0000-03-01T00:00:00.000Z"):
            with self.subTest(year_zero):
                self.assert_quarantine(heart_rate([("0001-01-01T00:30:00Z", 70), (year_zero, 71)], **series), OUTSIDE,
                                       "/record/samples/1/time")
                self.assert_quarantine(heart_rate([(year_zero, 0)], **series), OUTSIDE, "/record/samples/0/time")
                identity = self.revision(heart_rate([], start="0001-01-01T00:00:00Z", end="0001-01-01T01:00:00Z",
                                                    start_offset=None, end_offset="+00:00"), (UTC_POLICY,)).policy_identity
                self.assert_quarantine(heart_rate([(year_zero, 71)], start="0001-01-01T00:00:00Z", end="0001-01-01T01:00:00Z",
                                                  start_offset=None, end_offset="+00:00"),
                                       OUTSIDE, "/record/samples/0/time", (UTC_POLICY,), identity)
        self.assert_dates(heart_rate([("0001-01-01T00:00:00Z", 70), ("0001-01-01T01:00:00Z", 71)], **series),
                          ["0001-01-01", "0001-01-01"])
        self.assert_dates(heart_rate([("0001-01-01T00:00:00Z", 70)], start="0001-01-01T00:00:00Z", end="0001-01-01T01:00:00Z",
                                     start_offset="+00:00", end_offset="+00:00"), ["0001-01-01"])

    def test_zero_length_series_is_accepted(self) -> None:
        self.assertNotIn("heart-rate-interval-empty", PROCESSOR.quarantine_reasons)
        at = "2026-01-15T08:00:00Z"
        samples = [(at, 70), ("2026-01-15T08:00:00.000Z", 71), ("2026-01-15T08:00:00.000000Z", 72),
                   ("2026-01-15T08:00:00.000000000Z", 73)]
        self.assert_dates(heart_rate(samples, start=at, end=at), ["2026-01-15"] * 4)
        self.assert_dates(heart_rate(samples, start=at, end="2026-01-15T08:00:00.000000000Z"), ["2026-01-15"] * 4)
        self.assert_dates(heart_rate(samples, start="2026-01-15T08:00:00.000Z", end=at, start_offset=None, end_offset=None),
                          ["2026-01-15"] * 4, (NEW_YORK,))
        self.assertEqual(self.revision(heart_rate([], start=at, end=at)).body_bytes, b'{"samples":[]}')
        for outside in ("2026-01-15T07:59:59.999999999Z", "2026-01-15T08:00:00.000000001Z"):
            with self.subTest(outside):
                self.assert_quarantine(heart_rate(samples + [(outside, 74)], start=at, end=at), OUTSIDE,
                                       "/record/samples/4/time")

    def test_first_fault_follows_traversal_order(self) -> None:
        good = ("2026-01-15T07:40:00Z", 72)
        before, after = ("2026-01-15T07:29:59.999999999Z", 72), ("2026-01-15T08:00:00.000000001Z", 72)
        low, high = ("2026-01-15T07:41:00Z", 0), ("2026-01-15T07:42:00Z", 301)
        both = ("2026-01-15T08:00:00.000000001Z", 0)
        cases = [
            ([low, before], BPM_RANGE, "/record/samples/0/beats_per_minute"),
            ([both], OUTSIDE, "/record/samples/0/time"),
            ([good, both, low], OUTSIDE, "/record/samples/1/time"),
            ([good, before, high], OUTSIDE, "/record/samples/1/time"),
            ([good, high, after], BPM_RANGE, "/record/samples/1/beats_per_minute"),
            ([good, good, before, after], OUTSIDE, "/record/samples/2/time"),
            ([good, good, after, before], OUTSIDE, "/record/samples/2/time"),
            ([good, low, high], BPM_RANGE, "/record/samples/1/beats_per_minute"),
            ([good] * 11 + [high, both], BPM_RANGE, "/record/samples/11/beats_per_minute"),
            ([good] * 10 + [both, high], OUTSIDE, "/record/samples/10/time"),
        ]
        for samples, reason, pointer in cases:
            with self.subTest(pointer=pointer, samples=samples):
                projection = heart_rate(samples)
                self.assertLess(encode(projection).index(b'"beats_per_minute"'), encode(projection).index(b'"time"'))
                self.assert_quarantine(projection, reason, pointer)
        generator = random.Random(1124)
        start_ns, end_ns = epoch_ns(START), epoch_ns(END)
        for case in range(150):
            samples = []
            for _ in range(generator.randrange(1, 9)):
                at = generator.choice([start_ns - 1, end_ns + 1, start_ns, end_ns, generator.randint(start_ns - 10**9, end_ns + 10**9)])
                bpm = generator.choice([0, 1, 300, 301, generator.randint(-5, 305), generator.randint(1, 300)])
                samples.append((instant_text(at), bpm))
            with self.subTest(case=case):
                self.assert_oracle(samples, START, END, "+05:30", "-03:00")

    def test_common_outcomes_take_precedence(self) -> None:
        faulty = [("2026-01-15T07:20:00Z", 0), ("2026-01-15T07:40:00Z", 999)]
        # Control: the same samples in a sound series are this processor's Quarantine.
        self.assert_quarantine(heart_rate(faulty), OUTSIDE, "/record/samples/0/time")
        self.assert_quarantine(heart_rate(faulty[1:]), BPM_RANGE, "/record/samples/0/beats_per_minute")
        self.assertEqual(run(heart_rate(faulty, start=END, end=START)),
                         Quarantine("interval-reversed", "/record/end_time", VERSION, "none"))
        self.assertEqual(run(heart_rate(faulty[1:], start="2026-01-15T08:00:00.000000001Z", end=END)),
                         Quarantine("interval-reversed", "/record/end_time", VERSION, "none"))
        self.assertEqual(run(heart_rate(faulty, start_offset=None, end_offset=None)),
                         PolicyPending("fallback-timezone", "fallback-timezone-policy-missing", VERSION, "none"))
        self.assertEqual(run(heart_rate(faulty + [("2026-01-15T07:41:00Z", "72")])),
                         Quarantine("projection-schema-invalid", "/record/samples/2/beats_per_minute", VERSION, "none"))
        self.assertEqual(run(heart_rate(faulty + [("2026-01-15T07:41:00Z", 72.5)])),
                         Quarantine("projection-schema-invalid", "/record/samples/2/beats_per_minute", VERSION, "none"))
        self.assertEqual(run(heart_rate(faulty + [("2026-01-15T07:41:00Z", SAFE + 1)])),
                         Quarantine("projection-schema-invalid", "/record/samples/2/beats_per_minute", VERSION, "none"))
        self.assertEqual(run(heart_rate(faulty + [("2026-01-15T07:41:00+00:00", 72)])),
                         Quarantine("projection-schema-invalid", "/record/samples/2/time", VERSION, "none"))
        self.assertEqual(run(heart_rate([("0000-01-01T00:00:00Z", 0)], start="0000-01-01T00:00:00Z")),
                         Quarantine("instant-out-of-range", "/record/start_time", VERSION, "none"))
        self.assertEqual(run(heart_rate([("9999-12-31T23:30:00Z", 0)], start="9999-12-31T23:00:00Z",
                                        end="9999-12-31T23:59:00Z", start_offset="+00:00", end_offset="+01:00")),
                         Quarantine("instant-out-of-range", "/record/end_time", VERSION, "none"))


# ---- R6 ----

class R6NoDerivation(Checks):
    def test_body_carries_no_statistics(self) -> None:
        samples = [("2026-01-15T07:31:00Z", 70), ("2026-01-15T07:31:01Z", 90), ("2026-01-15T07:44:00Z", 110),
                   ("2026-01-15T07:59:59Z", 70), ("2026-01-15T07:44:00Z", 110)]
        outcome = self.assert_body(heart_rate(samples), expected_body(samples, "+05:30", "+05:30"))
        body = json.loads(outcome.body_bytes)
        self.assertEqual(set(body), {"samples"})
        self.assertEqual(len(body["samples"]), len(samples))
        for item in body["samples"]:
            self.assertLessEqual(set(item), SAMPLE_KEYS)
        self.assertEqual(list(json_numbers(body)), [70, 90, 110, 70, 110])
        for key in ("sum", "count", "min", "max", "mean", "average", "resting", "total", "duration", "minimum", "maximum"):
            self.assertNotIn(f'"{key}'.encode(), outcome.body_bytes)

    def test_body_depends_only_on_samples_and_applied_offsets(self) -> None:
        samples = [("2026-01-15T18:29:59.999999999Z", 61), ("2026-01-15T18:30:00Z", 62), ("2026-01-15T18:10:00.500Z", 63)]
        series = dict(start="2026-01-15T18:00:00Z", end="2026-01-15T19:00:00Z")
        base = self.assert_dates(heart_rate(samples, **series), ["2026-01-15", "2026-01-16", "2026-01-15"])
        same = [
            (heart_rate(samples, identity=IDENTITY + "-other", lmt="2026-02-01T00:00:00Z", device=False, recording_method=0,
                        provider="synthetic-provider-9", implementation="0.9.9-synthetic", client_record_id=None, **series), (), {}),
            (heart_rate(samples, **series), (), {"record_id": 9, "selection_id": 10, "evidence_id": 11}),
            (heart_rate(samples, start="2026-01-15T00:00:00Z", end="2026-01-15T23:59:59.999999999Z"), (), {}),
            (heart_rate(samples, start="2026-01-15T18:10:00.500Z", end="2026-01-15T18:30:00Z"), (), {}),
            (heart_rate(samples, start_offset=None, end_offset=None, **series), (KOLKATA,), {}),
            (heart_rate(samples, start_offset=None, **series), (KOLKATA,), {}),
        ]
        for projection, policies, overrides in same:
            with self.subTest(record=projection["record"], overrides=overrides):
                outcome = run(projection, policies, **overrides)
                self.assertIsInstance(outcome, CanonicalRevision, repr(outcome))
                self.assertEqual(outcome.body_bytes, base.body_bytes)
        changed = copy.deepcopy(samples)
        changed[1] = (changed[1][0], 64)
        outcome = self.assert_dates(heart_rate(changed, **series), ["2026-01-15", "2026-01-16", "2026-01-15"])
        self.assertEqual(outcome.body_bytes, base.body_bytes.replace(b'"beats_per_minute":62', b'"beats_per_minute":64'))
        self.assert_dates(heart_rate(samples, start_offset="-08:00", end_offset="-08:00", **series),
                          ["2026-01-15", "2026-01-15", "2026-01-15"])
        self.assert_dates(heart_rate(samples, start_offset="+05:30", end_offset="+06:00", **series), [None, "2026-01-16", None])


# ---- R7 ----

class R7TotalPureFastHealthDataFree(Checks):
    def test_schema_valid_edges_never_raise(self) -> None:
        cases = [
            # (samples, start, end, start_offset, end_offset, policies, applied)
            ([("2026-01-15T07:35:00Z", SAFE)], START, END, "+05:30", "+05:30", (), None),
            ([("2026-01-15T07:35:00Z", -SAFE)], START, END, "+05:30", "+05:30", (), None),
            ([("2026-01-15T07:35:00Z", 0)], START, END, "+05:30", "+05:30", (), None),
            ([("2026-01-15T07:35:00Z", float(SAFE))], START, END, "+05:30", "+05:30", (), None),
            ([("2026-01-15T07:35:00Z", -0.0)], START, END, "+05:30", "+05:30", (), None),
            ([("2026-01-15T07:35:00Z", 3e2)], START, END, "+05:30", "+05:30", (), None),
            ([("0000-02-29T12:00:00Z", 72)], "0001-01-01T00:00:00Z", "9999-12-31T23:59:59.999999999Z", "+18:00", "-18:00", (), None),
            ([("0000-01-01T00:00:00Z", SAFE)], "0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z", "+00:00", "+00:00", (), None),
            ([("9999-12-31T23:59:59.999999999Z", 72)], "0001-01-01T00:00:00Z", "9999-12-31T23:59:59.999999999Z", "+18:00", "-18:00",
             (), None),
            ([("0001-01-01T00:00:00Z", 72), ("9999-12-31T23:59:59.999999999Z", 73), ("5000-06-15T12:00:00.000001Z", 74)],
             "0001-01-01T00:00:00Z", "9999-12-31T23:59:59.999999999Z", "+00:00", "-00:00", (), None),
            ([("0001-01-01T00:00:00Z", 72), ("9999-12-31T23:59:59.999999999Z", 73), ("5000-06-15T12:00:00Z", 74)],
             "0001-01-01T00:00:00Z", "9999-12-31T23:59:59.999999999Z", "+00:00", "-17:59", (), None),
            ([("9999-12-31T23:59:59.999999999Z", 72)], "9999-12-31T23:59:59.999999999Z", "9999-12-31T23:59:59.999999999Z",
             "+00:00", "+00:00", (), None),
            ([("0001-01-01T00:00:00Z", 72)], "0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z", "+00:00", "+00:00", (), None),
            ([], "0001-01-01T00:00:00Z", "9999-12-31T23:59:59.999999999Z", "+18:00", "-18:00", (), None),
            ([("2400-02-29T23:59:59.999Z", 72), ("2000-02-29T00:00:00.000000Z", 300)], "2000-02-29T00:00:00Z",
             "2400-02-29T23:59:59.999999999Z", "+00:00", "+00:00", (), None),
            ([("1800-01-02T04:56:01Z", 72), ("1800-01-02T04:56:02Z", 301)], "1800-01-02T04:00:00Z", "1800-01-02T06:00:00Z",
             None, None, (NEW_YORK,), ("-04:56:02", "-04:56:02")),
            ([("1800-01-02T04:56:01Z", 72), ("1800-01-02T04:56:02Z", 300)], "1800-01-02T04:00:00Z", "1800-01-02T06:00:00Z",
             None, None, (NEW_YORK,), ("-04:56:02", "-04:56:02")),
            ([("0001-01-01T10:00:00Z", 72)], "0001-01-01T05:00:00Z", "0001-01-01T12:00:00Z", None, None, (NEW_YORK,),
             ("-04:56:02", "-04:56:02")),
        ]
        for samples, start, end, start_offset, end_offset, policies, applied in cases:
            with self.subTest(samples=samples, start=start, end=end, offsets=(start_offset, end_offset)):
                try:
                    outcome = run(heart_rate(samples, start=start, end=end, start_offset=start_offset, end_offset=end_offset),
                                  policies)
                except InterpretFailure as failure:
                    self.fail(f"raised {failure.failure}")
                self.assertIsInstance(outcome, (CanonicalRevision, Quarantine), repr(outcome))
                if isinstance(outcome, Quarantine):
                    self.assertIn(outcome.reason, PROCESSOR.quarantine_reasons)
                self.assert_oracle(samples, start, end, start_offset, end_offset, policies, applied)

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
        # In a subprocess: importing the module and running it reads no clock, writes nothing, and opens no host
        # timezone file, socket, store, or subprocess. After warm-up, the processor's own calls open no file, list
        # no directory, and read no timezone data, pinned or host, with or without policies.
        result = json.loads(run_python(purity_observer("interpret_heart_rate", "test_interpret_heart_rate_1"),
                                       {"TZ": "Pacific/Chatham", "LC_ALL": "C", "PYTHONHASHSEED": "7"}))
        result.pop("nested_time_helper")
        self.assertEqual(result, {"store": False, "events": [], "problems": [], "calls": len(observed.calls), **expected})

    def test_large_series_is_interpreted(self) -> None:
        start_ns = epoch_ns("2026-03-08T00:00:00Z")
        step = 500_000_001
        samples = [(instant_text(start_ns + index * step), 40 + index % 220) for index in range(LARGE_SERIES_SAMPLES)]
        end = instant_text(start_ns + LARGE_SERIES_SAMPLES * step)
        projection = heart_rate(samples, start="2026-03-08T00:00:00Z", end=end, start_offset="-05:00", end_offset="-04:00")
        data = encode(projection)
        expected = canonical_bytes(expected_body(samples, "-05:00", "-04:00"))
        processing_input = make_input(data)
        began = time.perf_counter()
        outcome = interpret(processing_input, PROCESSOR, [])
        elapsed = time.perf_counter() - began
        self.assertIsInstance(outcome, CanonicalRevision, repr(outcome)[:200])
        self.assertEqual(outcome.body_bytes, expected)
        body = json.loads(outcome.body_bytes)
        self.assertEqual(len(body["samples"]), LARGE_SERIES_SAMPLES)
        undated = sum("local_date" not in item for item in body["samples"])
        self.assertEqual(undated, 7200)  # 04:00 to 05:00 UTC, one sample every 500,000,001 ns
        self.assertLess(elapsed, LARGE_SERIES_SECONDS)

    def test_outcomes_are_health_data_free(self) -> None:
        fault_time = "2026-01-15T06:54:32.987654321Z"
        fault_value = 987654
        series = dict(start="2026-01-15T07:30:00Z", end="2026-01-15T08:00:00Z")
        cases = [
            (heart_rate([("2026-01-15T07:35:00Z", 72), (fault_time, 73)], **series), (), OUTSIDE, "/record/samples/1/time"),
            (heart_rate([("2026-01-15T07:35:00Z", 72), ("2026-01-15T07:36:00Z", fault_value)], **series), (), BPM_RANGE,
             "/record/samples/1/beats_per_minute"),
            (heart_rate([(fault_time, fault_value)], start_offset=None, end_offset=None, **series), (KOLKATA,), OUTSIDE,
             "/record/samples/0/time"),
            (heart_rate([("2026-01-15T07:36:00Z", -fault_value)], start_offset=None, **series), (KOLKATA,), BPM_RANGE,
             "/record/samples/0/beats_per_minute"),
        ]
        for projection, policies, reason, pointer in cases:
            with self.subTest(pointer=pointer, policies=bool(policies)):
                self.assertIn(HEALTH_MARKER.encode(), encode(projection))
                observed = Observed()
                outcome = run(projection, policies, observed)
                self.assertIsInstance(outcome, Quarantine)
                self.assertEqual({field.name for field in dataclasses.fields(outcome)},
                                 {"reason", "pointer", "processor_version", "policy_identity"})
                self.assertEqual((outcome.reason, outcome.pointer, outcome.processor_version), (reason, pointer, VERSION))
                self.assertRegex(outcome.reason, KEBAB)
                self.assertRegex(outcome.pointer, POINTER)
                for text in (repr(outcome), str(outcome), outcome.policy_identity):
                    self.assert_health_free(text)
                    for value in (fault_time, "987654", "06:54:32", "07:35:00", "07:36:00", "72", "73", ORIGIN):
                        self.assertNotIn(value, text)
                # What the processor itself returned, before the parent rewrapped it.
                assert_raw_quarantine(self, observed, reason, pointer, fault_time, "987654", "06:54:32", "07:35:00",
                                      "07:36:00", "72", "73", ORIGIN)


if __name__ == "__main__":
    unittest.main()
