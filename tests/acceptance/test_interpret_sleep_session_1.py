"""Acceptance tests for `interpret-sleep-session@1`.

Requirement numbers refer to `packs/interpret-sleep-session-1.spec.md`. Every outcome runs end to end
through the published `interpret_source_projection.interpret` with this unit's `PROCESSOR`. Inputs are
the Sleep Session vectors in `projection/v1/vectors/positive.json` and hand-built synthetic projections.
Title and notes hold a health-data marker, and Source Identities hold an identity marker, so the tests
can prove that Quarantines carry neither.
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
from datetime import date, datetime, timezone
from pathlib import Path

from activity_tracker_portal.interpret_sleep_session import PROCESSOR
from activity_tracker_portal.interpret_source_projection import (
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
CONTRACT = json.loads((ROOT / "governance/scopes/interpret-sleep-session.1.json").read_text(encoding="utf-8"))
EFFECTS = json.loads((ROOT / "packs/interpret-sleep-session-1.effects.json").read_text(encoding="utf-8"))
SCOPE_MAP = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
MODEL = json.loads((PROJECTION / "model.json").read_text(encoding="utf-8"))
POSITIVE = json.loads((PROJECTION / "vectors/positive.json").read_text(encoding="utf-8"))["vectors"]
SLEEP_VECTORS = [vector for vector in POSITIVE if vector["record_type"] == "sleep-session"]

UTC = timezone.utc
VERSION = "interpret-sleep-session@1"
HC = "health-connect"
ORIGIN = "org.example.synthetic.source"
SDK = "androidx.health.connect:connect-client:1.1.0"
HEALTH_MARKER = "synthetic-health-marker"
IDENTITY = "synthetic-identity-marker"
SOURCE_IDENTITY = IDENTITY + "-00000000-0000-4000-8000-000000000001"
LMT = "2026-01-16T09:00:00.123456789Z"
REASONS = frozenset({"sleep-session-empty", "sleep-stage-outside-session", "sleep-stage-reversed", "sleep-stage-empty"})
EMPTY, OUTSIDE, REVERSED, STAGE_EMPTY = ("sleep-session-empty", "sleep-stage-outside-session",
                                         "sleep-stage-reversed", "sleep-stage-empty")
COMMON_REASONS = {
    "projection-not-json", "projection-envelope-invalid", "projection-schema-invalid", "instant-out-of-range",
    "interval-reversed", "record-type-not-modeled", "schema-version-not-modeled", "fallback-timezone-policy-missing",
}
STAGE_CODES = MODEL["constants"]["sleep-stage"]["values"]
KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
INSTANT_TEXT = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d{3}|\d{6}|\d{9}))?Z$")
ABSENT = object()

# Session bounds with nine-digit fractions, so any comparison coarser than a nanosecond is visible.
S = "2026-01-15T22:30:00.000000500Z"
E = "2026-01-16T06:45:00.000000500Z"
S_MINUS_1 = "2026-01-15T22:30:00.000000499Z"
S_PLUS_1 = "2026-01-15T22:30:00.000000501Z"
E_MINUS_1 = "2026-01-16T06:45:00.000000499Z"
E_PLUS_1 = "2026-01-16T06:45:00.000000501Z"
MID = "2026-01-16T02:00:00Z"
MID_LATER = "2026-01-16T03:00:00.250Z"

# An absolute wall-clock time limit for one 100,000-stage interpretation; see R6 and the pack.
LARGE_SESSION_SECONDS = 60.0


# ---- Independent oracles ----

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def epoch_ns(text: str) -> int:
    """Exact nanoseconds since the Unix epoch, including year 0000 (proleptic Gregorian, a leap year)."""
    year, month, day, hour, minute, second, fraction = INSTANT_TEXT.match(text).groups()
    year, shift = int(year), 0
    if year == 0:
        # Year 0000 has the calendar of year 0400, exactly 146,097 days (400 Gregorian years) earlier.
        year, shift = 400, -146097
    days = date(year, int(month), int(day)).toordinal() - date(1970, 1, 1).toordinal() + shift
    seconds = days * 86400 + int(hour) * 3600 + int(minute) * 60 + int(second)
    return seconds * 10**9 + int((fraction or "").ljust(9, "0"))


def text_of(ns: int, width: int = 9) -> str:
    """An instant text for `ns` (years 1-9999) with `width` fraction digits when that is exact, else nine."""
    seconds, fraction = divmod(ns, 10**9)
    days, rest = divmod(seconds, 86400)
    day = date.fromordinal(days + date(1970, 1, 1).toordinal())
    hour, rest = divmod(rest, 3600)
    minute, second = divmod(rest, 60)
    digits = f"{fraction:09d}"
    if width == 0 and fraction == 0:
        tail = ""
    elif width in (3, 6) and digits[width:] == "0" * (9 - width):
        tail = "." + digits[:width]
    else:
        tail = "." + digits
    return f"{day.year:04d}-{day.month:02d}-{day.day:02d}T{hour:02d}:{minute:02d}:{second:02d}{tail}Z"


def es_number(value) -> str:
    """ECMAScript Number::toString for the integers this body holds (RFC 8785)."""
    if type(value) is not int:
        raise TypeError(type(value).__name__)
    return str(value)


def canonical(value) -> str:
    """RFC 8785 text of a JSON value of strings, integers, lists, and objects."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
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


def expected_body(record: dict) -> dict:
    """The version 1 body: each stage's exact text and integer code in source order, title and notes if present."""
    body = {"stages": [{"start_time": stage["start_time"], "end_time": stage["end_time"], "stage": int(stage["stage"])}
                       for stage in record["stages"]]}
    for name in ("title", "notes"):
        if name in record:
            body[name] = record[name]
    return body


def expected_body_bytes(record: dict) -> bytes:
    return canonical(expected_body(record)).encode("utf-8")


def expected_outcome(record: dict) -> tuple:
    """The R4 verdict by exact nanoseconds: ("body", bytes) or ("quarantine", reason, pointer)."""
    start, end = epoch_ns(record["start_time"]), epoch_ns(record["end_time"])
    if end == start:
        return ("quarantine", EMPTY, "/record/end_time")
    for index, stage in enumerate(record["stages"]):
        first, last = epoch_ns(stage["start_time"]), epoch_ns(stage["end_time"])
        if first < start or first > end:
            return ("quarantine", OUTSIDE, f"/record/stages/{index}/start_time")
        if last < first:
            return ("quarantine", REVERSED, f"/record/stages/{index}/end_time")
        if last == first:
            return ("quarantine", STAGE_EMPTY, f"/record/stages/{index}/end_time")
        if last > end:
            return ("quarantine", OUTSIDE, f"/record/stages/{index}/end_time")
    return ("body", expected_body_bytes(record))


# ---- Synthetic projections and inputs ----

def stage(start: str, end: str, code=4) -> dict:
    return {"start_time": start, "end_time": end, "stage": code}


def session(start: str = S, end: str = E, stages=(), *, title=HEALTH_MARKER, notes=HEALTH_MARKER,
            start_offset: str | None = "+05:30", end_offset: str | None = "+05:30",
            identity: str = SOURCE_IDENTITY) -> dict:
    """A synthetic Sleep Session projection; ABSENT omits title or notes."""
    record = {"start_time": start}
    if start_offset is not None:
        record["start_zone_offset"] = start_offset
    record["end_time"] = end
    if end_offset is not None:
        record["end_zone_offset"] = end_offset
    if title is not ABSENT:
        record["title"] = title
    if notes is not ABSENT:
        record["notes"] = notes
    record["stages"] = [dict(item) for item in stages]
    return {
        "record_type": "sleep-session",
        "projection_schema_version": 1,
        "projection_implementation_version": "0.1.0-synthetic",
        "sdk_version": SDK,
        "provider_version": "synthetic-provider-1",
        "metadata": {
            "id": identity,
            "data_origin": {"package_name": ORIGIN},
            "last_modified_time": LMT,
            "client_record_id": "synthetic-client-record-id",
            "client_record_version": "7",
            "recording_method": 2,
            "device": {"type": 4, "manufacturer": "Synthetic Devices", "model": "SR-1"},
        },
        "record": record,
    }


def reversed_keys(value):
    """The same JSON value with every object's keys in reverse order, so input order disagrees with model order."""
    if isinstance(value, dict):
        return {key: reversed_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [reversed_keys(item) for item in value]
    return value


def encode(projection: dict, *, reverse: bool = False) -> bytes:
    value = reversed_keys(projection) if reverse else projection
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def truncated(text: str) -> datetime:
    year, month, day, hour, minute, second, fraction = INSTANT_TEXT.match(text).groups()
    return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second),
                    int((fraction or "").ljust(9, "0")[:6]), tzinfo=UTC)


def make_input(data: bytes) -> ProcessingInput:
    decoded = json.loads(data)
    metadata = decoded["metadata"]
    return ProcessingInput(
        source_system_kind=HC, origin=metadata["data_origin"]["package_name"], record_type=decoded["record_type"],
        source_identity=metadata["id"], record_id=41, selection_id=42, evidence_id=43,
        projection_sha256=sha(data), last_modified_time=truncated(metadata["last_modified_time"]),
        projection_schema_version=decoded["projection_schema_version"], projection=data,
    )


def run(projection, policies=(), *, reverse: bool = False):
    """Interpret through the published parent with this unit's PROCESSOR."""
    data = projection if isinstance(projection, bytes) else encode(projection, reverse=reverse)
    return interpret(make_input(data), PROCESSOR, list(policies))


def policy(policy_id: str, effective_from: datetime | None, zone: str) -> FallbackTimezonePolicy:
    return FallbackTimezonePolicy(policy_id, effective_from, zone)


UTC_ALWAYS = (policy("policy-utc", None, "UTC"),)


def scenario_summary() -> str:
    """A deterministic summary of representative outcomes, compared across processes and host settings."""
    cases = [
        (session(stages=[stage(S, MID, 2), stage(MID, E, 5)]), ()),
        (session(start_offset=None, end_offset=None, stages=[stage(S, E, 0)]), (policy("policy-kolkata", None, "Asia/Kolkata"),)),
        (session(S, S), ()),
        (session(stages=[stage(S_MINUS_1, MID)]), ()),
        (session(stages=[stage(MID, S)]), ()),
        (session(stages=[stage(MID, MID)]), ()),
        (session(stages=[stage("0000-02-29T00:00:00Z", MID)]), ()),
        (copy.deepcopy(SLEEP_VECTORS[0]["projection"]), UTC_ALWAYS),
    ]
    out = []
    for projection, policies in cases:
        outcome = run(projection, policies)
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
    def revision(self, projection, policies=(), *, reverse: bool = False) -> CanonicalRevision:
        outcome = run(projection, policies, reverse=reverse)
        self.assertIsInstance(outcome, CanonicalRevision, repr(outcome))
        self.assertEqual(outcome.processor_version, VERSION)
        self.assertEqual(outcome.header.body_schema, PROCESSOR.body_schema)
        header = json.loads(outcome.header_bytes)
        self.assertEqual(header["body_schema"], {"record_type": "sleep-session", "body_version": 1})
        self.assertEqual(header["processor_version"], VERSION)
        self.assertEqual(outcome.body_sha256, sha(outcome.body_bytes))
        return outcome

    def assert_body(self, projection, policies=(), *, reverse: bool = False) -> CanonicalRevision:
        """The revision's body is exactly the RFC 8785 bytes of the expected body."""
        outcome = self.revision(projection, policies, reverse=reverse)
        self.assertEqual(outcome.body_bytes, expected_body_bytes(projection["record"]))
        return outcome

    def assert_quarantine(self, projection, reason: str, pointer: str, policies=(), identity: str = "none", *,
                          reverse: bool = False) -> Quarantine:
        outcome = run(projection, policies, reverse=reverse)
        self.assertEqual(outcome, Quarantine(reason, pointer, VERSION, identity))
        self.assert_health_free(repr(outcome))
        return outcome

    def assert_health_free(self, text: str) -> None:
        self.assertNotIn(HEALTH_MARKER, text)
        self.assertNotIn(IDENTITY, text)


# ---- R1 ----

class R1GovernedScopeContract(unittest.TestCase):
    def test_contract_shape_and_dependency(self) -> None:
        self.assertEqual((CONTRACT["scope"], CONTRACT["version"], CONTRACT["kind"]),
                         ("interpret-sleep-session", "1", "leaf"))
        self.assertEqual(CONTRACT["dependencies"], [{"scope": "interpret-source-projection", "version": "1"}])
        self.assertEqual(CONTRACT["named_failures"], [])
        self.assertEqual(CONTRACT["observable_effects"], [])
        self.assertEqual({item["mode"] for item in CONTRACT["inputs"]}, {"pure"})
        outputs = {item["name"]: item["description"] for item in CONTRACT["outputs"]}
        named = set(re.findall(r"\bsleep-[a-z0-9-]*[a-z0-9]\b", outputs["quarantine_outcome"]))
        self.assertEqual(named, set(REASONS))
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["specification"], "packs/interpret-sleep-session-1.spec.md")
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_interpret_sleep_session_1.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_interpret_sleep_session_1.sh")
        self.assertEqual(oracle["languages"], ["python"])

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        row = next(line for line in SCOPE_MAP.splitlines() if line.startswith("| `interpret-sleep-session` |"))
        self.assertIn("| leaf |", row)
        self.assertIn("Sleep Session", row)
        self.assertIn("Canonical Revision", row)
        self.assertTrue(row.rstrip().endswith("| `interpret-source-projection` |"), row)

    def test_effects_declaration_is_empty(self) -> None:
        self.assertEqual(EFFECTS, {"effects_format": 1, "effects": []})


# ---- R2 ----

class R2ProcessorIdentity(unittest.TestCase):
    def test_processor_identity(self) -> None:
        self.assertIsInstance(PROCESSOR, Processor)
        self.assertEqual(PROCESSOR.record_type, "sleep-session")
        self.assertEqual(PROCESSOR.processor_version, VERSION)
        self.assertEqual(PROCESSOR.processor_version, f"{CONTRACT['scope']}@{CONTRACT['version']}")
        self.assertIsInstance(PROCESSOR.supported_schema_versions, frozenset)
        self.assertEqual(PROCESSOR.supported_schema_versions, frozenset({1}))
        self.assertEqual(PROCESSOR.body_schema,
                         BodySchema("sleep-session", 1, (NumericField("/stages/*/stage", "code", None),)))
        self.assertIsInstance(PROCESSOR.body_schema.numeric_fields, tuple)
        self.assertIsInstance(PROCESSOR.quarantine_reasons, frozenset)
        self.assertEqual(PROCESSOR.quarantine_reasons, REASONS)
        for name in ("record_type", "processor_version", "supported_schema_versions", "body_schema",
                     "quarantine_reasons"):
            with self.subTest(attribute=name), self.assertRaises(AttributeError):
                setattr(PROCESSOR, name, getattr(PROCESSOR, name))

    def test_declared_reasons_are_exact_and_not_common(self) -> None:
        self.assertEqual(set(PROCESSOR.quarantine_reasons), set(REASONS))
        self.assertEqual(len(REASONS), 4)
        for reason in PROCESSOR.quarantine_reasons:
            with self.subTest(reason=reason):
                self.assertRegex(reason, KEBAB)
                self.assertTrue(reason.startswith("sleep-"), reason)
                self.assertNotIn(reason, COMMON_REASONS)


# ---- R3 ----

class R3Body(Checks):
    def test_positive_vectors_become_revisions(self) -> None:
        names = {vector["name"] for vector in SLEEP_VECTORS}
        self.assertLessEqual({"sleep-session-minimal", "sleep-session-maximal", "sleep-session-text-escaping"}, names)
        for vector in SLEEP_VECTORS:
            with self.subTest(vector=vector["name"]):
                projection = copy.deepcopy(vector["projection"])
                record = projection["record"]
                offsets = "start_zone_offset" in record and "end_zone_offset" in record
                data = vector["bytes"].encode("utf-8")
                self.assertEqual(sha(data), vector["sha256"])
                outcome = self.revision(data, () if offsets else UTC_ALWAYS)
                self.assertEqual(outcome.body_bytes, expected_body_bytes(record))
                self.assertEqual(outcome.policy_identity == "none", offsets)
                body = json.loads(outcome.body_bytes)
                self.assertEqual(len(body["stages"]), len(record["stages"]))

    def test_stages_keep_exact_text_and_codes(self) -> None:
        stages = [
            stage(S, "2026-01-15T23:00:00Z", 2),
            stage("2026-01-15T23:00:00.000Z", "2026-01-16T00:00:00.000000Z", 4),
            stage("2026-01-16T00:00:00.000000000Z", "2026-01-16T01:30:00.100Z", 5),
            stage("2026-01-16T01:30:00.100000Z", "2026-01-16T01:30:00.100000001Z", 6),
            stage("2026-01-16T01:30:00.100000001Z", E, 1),
        ]
        projection = session(stages=stages)
        outcome = self.assert_body(projection)
        body = json.loads(outcome.body_bytes)
        for index, (kept, source) in enumerate(zip(body["stages"], stages)):
            with self.subTest(index=index):
                self.assertEqual(kept, source)
                self.assertIs(type(kept["stage"]), int)
        # A JSON integer written with a fraction or an exponent is schema-valid; the body holds the integer code.
        data = encode(session(stages=[stage(S, MID, 4.0), stage(MID, MID_LATER, -0.0), stage(MID_LATER, E, 400.0)]))
        data = data.replace(b'"stage":400.0', b'"stage":6E0')
        self.assertIn(b'"stage":4.0', data)
        self.assertIn(b'"stage":-0.0', data)
        self.assertIn(b'"stage":6E0', data)
        outcome = self.revision(data)
        self.assertEqual(outcome.body_bytes, canonical(expected_body(
            session(stages=[stage(S, MID, 4), stage(MID, MID_LATER, 0), stage(MID_LATER, E, 6)])["record"])).encode())

    def test_stages_keep_source_order(self) -> None:
        stages = [
            stage(MID_LATER, E, 3),
            stage(S, "2026-01-15T23:00:00Z", 6),
            stage("2026-01-16T01:00:00Z", MID, 1),
            stage("2026-01-15T23:00:00Z", "2026-01-16T01:00:00Z", 7),
            stage(MID, MID_LATER, 2),
        ]
        for reverse in (False, True):
            with self.subTest(reverse_key_order=reverse):
                projection = session(stages=stages)
                outcome = self.assert_body(projection, reverse=reverse)
                body = json.loads(outcome.body_bytes)
                self.assertEqual([item["stage"] for item in body["stages"]], [3, 6, 1, 7, 2])
                self.assertEqual([item["start_time"] for item in body["stages"]], [item["start_time"] for item in stages])

    def test_every_declared_code_is_kept(self) -> None:
        self.assertEqual(set(STAGE_CODES.values()), set(range(8)))
        order = [7, 0, 3, 5, 1, 6, 2, 4]
        bounds = [S] + [f"2026-01-16T0{hour}:00:00Z" for hour in range(7)] + [E]
        stages = [stage(bounds[i], bounds[i + 1], code) for i, code in enumerate(order)]
        outcome = self.assert_body(session(stages=stages))
        self.assertEqual([item["stage"] for item in json.loads(outcome.body_bytes)["stages"]], order)
        alone = self.assert_body(session(stages=[stage(S, E, 0)]))
        self.assertEqual(alone.body_bytes, canonical(expected_body(session(stages=[stage(S, E, 0)])["record"])).encode())
        self.assertIn(b'"stage":0', alone.body_bytes)
        # An undeclared code never reaches the processor.
        for code in (8, -1, 99):
            with self.subTest(code=code):
                bad = session(stages=[stage(S, MID, 2), stage(MID, E, code)])
                self.assertEqual(run(bad), Quarantine("projection-schema-invalid", "/record/stages/1/stage", VERSION, "none"))

    def test_empty_stages_give_an_empty_list(self) -> None:
        outcome = self.assert_body(session(stages=[], title=ABSENT, notes=ABSENT))
        self.assertEqual(outcome.body_bytes, b'{"stages":[]}')
        outcome = self.assert_body(session(start_offset=None, end_offset=None, stages=[], title="", notes=ABSENT), UTC_ALWAYS)
        self.assertEqual(outcome.body_bytes, b'{"stages":[],"title":""}')

    def test_title_and_notes_are_verbatim_or_absent(self) -> None:
        escaping = next(vector for vector in SLEEP_VECTORS if vector["name"] == "sleep-session-text-escaping")
        tricky = escaping["projection"]["record"]["title"]
        self.assertTrue(all(part in tricky for part in ('"', "\\", "\t", "\n", "\u0001", "\U0001F3C3")))
        stages = [stage(S, E, 2)]
        cases = {
            "both": (HEALTH_MARKER + " title", HEALTH_MARKER + " notes"),
            "title only": (HEALTH_MARKER, ABSENT),
            "notes only": (ABSENT, HEALTH_MARKER),
            "neither": (ABSENT, ABSENT),
            "empty strings": ("", ""),
            "empty title, absent notes": ("", ABSENT),
            "whitespace kept": ("  " + HEALTH_MARKER + " \n", "\t"),
            "escaping vector text": (tricky, tricky[::-1]),
            "nul and controls": ("\u0000" + HEALTH_MARKER + "\u001f\u007f", "\b\f\r"),
            "unnormalized unicode": ("é é ﬁ Ω", "Å Å"),
            "literal null text": ("null", "None"),
        }
        for name, (title, notes) in cases.items():
            for reverse in (False, True):
                with self.subTest(case=name, reverse_key_order=reverse):
                    projection = session(stages=stages, title=title, notes=notes)
                    outcome = self.assert_body(projection, reverse=reverse)
                    body = json.loads(outcome.body_bytes)
                    for key, value in (("title", title), ("notes", notes)):
                        if value is ABSENT:
                            self.assertNotIn(key, body)
                            self.assertNotIn(f'"{key}"'.encode(), outcome.body_bytes)
                        else:
                            self.assertEqual(body[key], value)
                            self.assertIs(type(body[key]), str)
        vector = escaping["projection"]
        outcome = self.assert_body(vector)
        self.assertEqual(json.loads(outcome.body_bytes)["title"], tricky)

    def test_body_has_no_header_fields(self) -> None:
        header_names = {"start_time", "end_time", "start_zone_offset", "end_zone_offset", "time", "metadata",
                        "record_type", "source_identity", "origin", "local_date", "applied_offset", "instant",
                        "attribution", "policy_id", "device", "recording_method", "last_modified_time",
                        "processor_version", "body_schema", "policy_identity"}
        cases = [
            (session(stages=[stage(S, E, 2)]), ()),
            (session(stages=[stage(S, E, 2)], title=ABSENT, notes=ABSENT), ()),
            (session(start_offset=None, end_offset="-03:00", stages=[stage(S, MID, 1), stage(MID, E, 5)]),
             (policy("policy-kolkata", None, "Asia/Kolkata"),)),
            (session(start_offset=None, end_offset=None, stages=[]), UTC_ALWAYS),
        ]
        for projection, policies in cases:
            with self.subTest(record=sorted(projection["record"])):
                outcome = self.assert_body(projection, policies)
                body = json.loads(outcome.body_bytes)
                self.assertLessEqual(set(body), {"stages", "title", "notes"})
                self.assertTrue(header_names.isdisjoint(body))
                self.assertEqual(set(body) - {"stages"}, {"title", "notes"} & set(projection["record"]))
                for item in body["stages"]:
                    self.assertEqual(set(item), {"start_time", "end_time", "stage"})
                allowed = {item[key] for item in projection["record"]["stages"] for key in ("start_time", "end_time")}
                allowed |= {projection["record"].get(key) for key in ("title", "notes")}
                strings = [body.get("title"), body.get("notes")] + [
                    item[key] for item in body["stages"] for key in ("start_time", "end_time")]
                for value in strings:
                    if value is not None:
                        self.assertIn(value, allowed)
                for header_text in (LMT, "+05:30", "-03:00", ORIGIN, SOURCE_IDENTITY, "policy-", "inferred",
                                    "experienced"):
                    self.assertNotIn(header_text.encode(), outcome.body_bytes)


# ---- R4 ----

class R4Validity(Checks):
    def test_empty_session_is_quarantined(self) -> None:
        cases = [
            ("same text", session(S, S)),
            ("same text with stages", session(S, S, [stage(S, S, 2)])),
            ("different fraction text", session("2026-01-16T06:45:00Z", "2026-01-16T06:45:00.000000000Z")),
            ("three and six digits", session("2026-01-16T06:45:00.120Z", "2026-01-16T06:45:00.120000Z",
                                             [stage("2026-01-16T06:45:00.120Z", "2026-01-16T06:45:00.121Z", 4)])),
            ("no title or notes", session(E, E, title=ABSENT, notes=ABSENT)),
        ]
        for name, projection in cases:
            for reverse in (False, True):
                with self.subTest(case=name, reverse_key_order=reverse):
                    self.assert_quarantine(projection, EMPTY, "/record/end_time", reverse=reverse)
        timeline = (policy("policy-kolkata", None, "Asia/Kolkata"),)
        inferred = self.assert_quarantine(session(S, S, start_offset=None, end_offset=None), EMPTY, "/record/end_time",
                                          timeline, identity=self.identity(timeline))
        self.assertNotEqual(inferred.policy_identity, "none")
        # One nanosecond is a session.
        self.assert_body(session(S, S_PLUS_1))
        self.assert_body(session(S, S_PLUS_1, [stage(S, S_PLUS_1, 1)]))

    def identity(self, timeline) -> str:
        """The policy identity a revision has when both endpoints are inferred through this timeline."""
        return self.revision(session(start_offset=None, end_offset=None, stages=[]), timeline).policy_identity

    def test_stage_outside_session_is_quarantined(self) -> None:
        valid = [stage(S, MID, 2), stage(MID, E, 5)]
        cases = [
            ("start 1 ns before session start", stage(S_MINUS_1, MID), "start_time"),
            ("start 1 ns before, same second", stage("2026-01-15T22:30:00.000000499Z", S), "start_time"),
            ("start a day before", stage("2026-01-14T22:30:00Z", MID), "start_time"),
            ("start before and reversed", stage(S_MINUS_1, "2026-01-15T22:00:00Z"), "start_time"),
            ("start 1 ns after session end", stage(E_PLUS_1, "2026-01-16T06:45:00.000000502Z"), "start_time"),
            ("start after end and reversed", stage("2026-01-16T07:00:00Z", "2026-01-16T06:50:00Z"), "start_time"),
            ("start after end and empty", stage(E_PLUS_1, E_PLUS_1), "start_time"),
            ("wholly after the session", stage("2026-01-17T00:00:00Z", "2026-01-17T01:00:00Z"), "start_time"),
            ("covers the session", stage(S_MINUS_1, E_PLUS_1), "start_time"),
            ("end 1 ns after session end", stage(MID, E_PLUS_1), "end_time"),
            ("start at session end, end 1 ns later", stage(E, E_PLUS_1), "end_time"),
            ("start at session start, end a day later", stage(S, "2026-01-17T06:45:00.000000500Z"), "end_time"),
        ]
        for name, faulty, field in cases:
            for position in (0, 2):
                with self.subTest(case=name, index=position):
                    stages = list(valid)
                    stages.insert(position, faulty)
                    self.assert_quarantine(session(stages=stages), OUTSIDE, f"/record/stages/{position}/{field}")

    def test_reversed_stage_is_quarantined(self) -> None:
        cases = [
            ("1 ns reversed", stage(MID_LATER, "2026-01-16T03:00:00.249999999Z")),
            ("fraction texts differ", stage("2026-01-16T03:00:00.250000000Z", "2026-01-16T03:00:00.249Z")),
            ("ends before session start", stage(MID, S_MINUS_1)),
            ("ends a day before session start", stage(S, "2026-01-14T00:00:00Z")),
            ("starts at session end", stage(E, E_MINUS_1)),
        ]
        for name, faulty in cases:
            with self.subTest(case=name):
                self.assert_quarantine(session(stages=[stage(S, MID, 2), faulty]), REVERSED, "/record/stages/1/end_time")

    def test_empty_stage_is_quarantined(self) -> None:
        cases = [
            ("same text", stage(MID, MID)),
            ("different fraction text", stage("2026-01-16T02:00:00.000Z", "2026-01-16T02:00:00.000000000Z")),
            ("at session start", stage(S, S)),
            ("at session start, other text", stage(S, "2026-01-15T22:30:00.000000500Z")),
            ("at session end", stage(E, E)),
        ]
        for name, faulty in cases:
            with self.subTest(case=name):
                self.assert_quarantine(session(stages=[faulty, stage(S, E, 2)]), STAGE_EMPTY, "/record/stages/0/end_time")

    def test_stage_bounds_are_inclusive(self) -> None:
        cases = {
            "exactly the session": [stage(S, E, 2)],
            "session at three bounds": [stage(S, MID, 2), stage(MID, E, 5), stage(S, E, 1)],
            "1 ns at each bound": [stage(S, S_PLUS_1, 1), stage(E_MINUS_1, E, 7)],
            "equal instants, other text": [stage("2026-01-15T22:00:00.000Z", "2026-01-16T07:00:00.000000Z", 2)],
        }
        for name, stages in cases.items():
            with self.subTest(case=name):
                if name == "equal instants, other text":
                    projection = session("2026-01-15T22:00:00Z", "2026-01-16T07:00:00.000000000Z", stages)
                else:
                    projection = session(stages=stages)
                self.assert_body(projection)
        # Whole-second bounds against stages written with every fraction width.
        start, end = "2026-01-15T22:00:00Z", "2026-01-16T07:00:00Z"
        for width in ("", ".000", ".000000", ".000000000"):
            with self.subTest(width=width):
                stages = [stage(f"2026-01-15T22:00:00{width}Z", f"2026-01-16T07:00:00{width}Z", 4)]
                self.assert_body(session(start, end, stages))

    def test_year_zero_stage_is_outside_the_session(self) -> None:
        low_start, low_end = "0001-01-01T00:00:00Z", "0001-01-01T08:00:00Z"
        cases = [
            ("start just before year 1", session(low_start, low_end, [stage("0000-12-31T23:59:59.999999999Z", low_end)],
                                                start_offset="+00:00", end_offset="+00:00"),
             OUTSIDE, "/record/stages/0/start_time"),
            ("start on 0000-02-29", session(stages=[stage("0000-02-29T12:00:00.500Z", MID)]),
             OUTSIDE, "/record/stages/0/start_time"),
            ("start at the first instant", session(stages=[stage(S, MID, 2), stage("0000-01-01T00:00:00Z", "0000-01-01T00:00:00Z")]),
             OUTSIDE, "/record/stages/1/start_time"),
            ("both in year 0000", session(stages=[stage("0000-06-01T00:00:00Z", "0000-07-01T00:00:00Z")]),
             OUTSIDE, "/record/stages/0/start_time"),
            ("end in year 0000", session(low_start, low_end, [stage(low_start, "0000-12-31T23:59:59.999999999Z")],
                                         start_offset="+00:00", end_offset="+00:00"),
             REVERSED, "/record/stages/0/end_time"),
        ]
        for name, projection, reason, pointer in cases:
            with self.subTest(case=name):
                self.assert_quarantine(projection, reason, pointer)
        # The in-range neighbours: the first instant of year 1 and the last of year 9999.
        self.assert_body(session(low_start, low_end, [stage("0001-01-01T00:00:00.000000000Z", low_end, 3)],
                                 start_offset="+00:00", end_offset="+00:00"))
        high_start, high_end = "9999-12-31T20:00:00Z", "9999-12-31T23:59:59.999999999Z"
        self.assert_body(session(high_start, high_end, [stage("9999-12-31T20:00:00.000Z", high_end, 6)],
                                 start_offset="+00:00", end_offset="+00:00"))
        self.assert_quarantine(session(high_start, "9999-12-31T23:59:59.999999998Z", [stage(high_start, high_end)],
                                       start_offset="+00:00", end_offset="+00:00"),
                               OUTSIDE, "/record/stages/0/end_time")

    def test_first_fault_follows_traversal_order(self) -> None:
        ok = [stage(S, MID, 2), stage(MID, E, 5)]
        before = stage(S_MINUS_1, MID)
        after_end = stage(MID, E_PLUS_1)
        rev = stage(MID_LATER, MID)
        empty = stage(MID, MID)
        cases = [
            ("session end before stages", session(S, S, [before, rev]), EMPTY, "/record/end_time"),
            ("session end before a later stage", session(E, E, [stage(E, E_PLUS_1)]), EMPTY, "/record/end_time"),
            ("stage start before its end", session(stages=[stage(S_MINUS_1, S_MINUS_1)]), OUTSIDE, "/record/stages/0/start_time"),
            ("stage start before its end, after session", session(stages=[stage(E_PLUS_1, E)]), OUTSIDE, "/record/stages/0/start_time"),
            ("both ends after the session", session(stages=[stage(E_PLUS_1, "2026-01-16T07:00:00Z")]), OUTSIDE,
             "/record/stages/0/start_time"),
            ("both ends outside the session", session(stages=[ok[0], stage(S_MINUS_1, E_PLUS_1)]), OUTSIDE,
             "/record/stages/1/start_time"),
            ("earlier stage's end before later stage's start",
             session(stages=[ok[0], after_end, before]), OUTSIDE, "/record/stages/1/end_time"),
            ("earlier index wins over earlier time",
             session(stages=[stage(MID_LATER, E_PLUS_1), stage(S_MINUS_1, MID)]), OUTSIDE, "/record/stages/0/end_time"),
            ("reversed before empty before outside",
             session(stages=[ok[0], rev, empty, before, after_end]), REVERSED, "/record/stages/1/end_time"),
            ("empty before reversed", session(stages=[ok[1], empty, rev]), STAGE_EMPTY, "/record/stages/1/end_time"),
            ("outside end before reversed", session(stages=[after_end, rev]), OUTSIDE, "/record/stages/0/end_time"),
        ]
        many = [stage(text_of(epoch_ns(S) + i * 60 * 10**9), text_of(epoch_ns(S) + (i + 1) * 60 * 10**9), i % 8)
                for i in range(12)]
        cases.append(("index 12", session(stages=many + [rev, before]), REVERSED, "/record/stages/12/end_time"))
        cases.append(("index 10", session(stages=many[:10] + [before] + many[10:] + [rev]), OUTSIDE,
                      "/record/stages/10/start_time"))
        for name, projection, reason, pointer in cases:
            self.assertEqual(expected_outcome(projection["record"]), ("quarantine", reason, pointer), name)
            for reverse in (True, False):
                with self.subTest(case=name, reverse_key_order=reverse):
                    self.assert_quarantine(projection, reason, pointer, reverse=reverse)

    def test_common_outcomes_take_precedence(self) -> None:
        faulty = [stage(S_MINUS_1, MID), stage(MID, MID)]
        # interval-reversed: the session's end is before its start.
        self.assertEqual(run(session(E, S, faulty), reverse=True),
                         Quarantine("interval-reversed", "/record/end_time", VERSION, "none"))
        self.assertEqual(run(session(S_PLUS_1, S, [stage(S, S)])),
                         Quarantine("interval-reversed", "/record/end_time", VERSION, "none"))
        # Policy-Pending: an endpoint without offset or policy.
        for offsets in ((None, None), (None, "+05:30"), ("+05:30", None)):
            with self.subTest(offsets=offsets):
                pending = run(session(stages=faulty, start_offset=offsets[0], end_offset=offsets[1]))
                self.assertIsInstance(pending, PolicyPending)
                self.assertEqual((pending.reason, pending.processor_version),
                                 ("fallback-timezone-policy-missing", VERSION))
        self.assertIsInstance(run(session(S, S, start_offset=None, end_offset=None)), PolicyPending)
        # projection-schema-invalid: an undeclared code after an earlier stage fault.
        self.assertEqual(run(session(stages=[faulty[0], stage(MID, E, 8)])),
                         Quarantine("projection-schema-invalid", "/record/stages/1/stage", VERSION, "none"))
        self.assertEqual(run(session(S, S, [stage(MID, E, 8)])),
                         Quarantine("projection-schema-invalid", "/record/stages/0/stage", VERSION, "none"))
        # instant-out-of-range: a session start in year 0000.
        self.assertEqual(run(session("0000-12-31T23:00:00Z", "0001-01-01T08:00:00Z",
                                     [stage("0000-12-31T23:00:00Z", "0001-01-01T00:00:00Z")],
                                     start_offset="+00:00", end_offset="+00:00")),
                         Quarantine("instant-out-of-range", "/record/start_time", VERSION, "none"))
        # Controls: the same stage faults are this processor's once the common fault is removed.
        self.assert_quarantine(session(S, E, faulty), OUTSIDE, "/record/stages/0/start_time")
        self.assert_quarantine(session(S, S_PLUS_1, [stage(S, S)]), STAGE_EMPTY, "/record/stages/0/end_time")
        timeline = (policy("policy-kolkata", None, "Asia/Kolkata"),)
        identity = self.revision(session(start_offset=None, end_offset=None), timeline).policy_identity
        self.assert_quarantine(session(stages=faulty, start_offset=None, end_offset=None), OUTSIDE,
                               "/record/stages/0/start_time", timeline, identity)
        self.assert_quarantine(session(stages=[faulty[0], stage(MID, E, 7)]), OUTSIDE, "/record/stages/0/start_time")
        self.assert_quarantine(session("0001-01-01T00:00:00Z", "0001-01-01T08:00:00Z",
                                       [stage("0000-12-31T23:00:00Z", "0001-01-01T00:00:00Z")],
                                       start_offset="+00:00", end_offset="+00:00"),
                               OUTSIDE, "/record/stages/0/start_time")


# ---- R5 ----

class R5NoDerivation(Checks):
    def test_overlapping_stages_are_kept(self) -> None:
        cases = {
            "same code overlap": [stage(S, MID_LATER, 4), stage(MID, E, 4)],
            "different code overlap": [stage(S, MID_LATER, 4), stage(MID, E, 1)],
            "exact duplicates": [stage(S, MID, 5), stage(S, MID, 5), stage(MID, E, 6)],
            "duplicate interval, other codes": [stage(S, MID, 5), stage(S, MID, 6)],
            "nested": [stage(S, E, 2), stage(MID, MID_LATER, 6), stage("2026-01-16T02:30:00Z", MID_LATER, 3)],
            "nested, later first": [stage(MID, MID_LATER, 6), stage(S, E, 2)],
            "the SDK's rejected case": [stage(S, MID_LATER, 4), stage(MID, E, 5)],
            "touching": [stage(S, MID, 4), stage(MID, E, 5)],
            "1 ns overlap": [stage(S, "2026-01-16T02:00:00.000000001Z", 4), stage(MID, E, 5)],
            "same instant, other text": [stage(S, "2026-01-16T02:00:00.000Z", 4), stage(MID, E, 5), stage(MID, E, 6)],
        }
        for name, stages in cases.items():
            for reverse in (False, True):
                with self.subTest(case=name, reverse_key_order=reverse):
                    outcome = self.assert_body(session(stages=stages), reverse=reverse)
                    body = json.loads(outcome.body_bytes)
                    self.assertEqual(body["stages"], [{**item, "stage": int(item["stage"])} for item in stages])

    def test_gaps_are_not_filled(self) -> None:
        cases = {
            "gap at start": [stage(MID, E, 4)],
            "gap at end": [stage(S, MID, 4)],
            "gap in the middle": [stage(S, MID, 4), stage(MID_LATER, E, 5)],
            "gaps everywhere": [stage("2026-01-15T23:00:00Z", MID, 6), stage(MID_LATER, "2026-01-16T05:00:00Z", 2)],
            "1 ns gap": [stage(S, "2026-01-16T01:59:59.999999999Z", 4), stage(MID, E, 5)],
            "single interior stage": [stage(MID, MID_LATER, 0)],
        }
        for name, stages in cases.items():
            with self.subTest(case=name):
                outcome = self.assert_body(session(stages=stages))
                body = json.loads(outcome.body_bytes)
                self.assertEqual(len(body["stages"]), len(stages))
                self.assertEqual(body["stages"], stages)

    def test_body_carries_no_classes_or_durations(self) -> None:
        # The session crosses local midnight: 04:00 to 12:15 on the 16th at +05:30, after starting late on the 15th UTC.
        stages = [stage(S, "2026-01-15T23:00:00Z", 1), stage("2026-01-15T23:00:00Z", MID, 4),
                  stage(MID, MID_LATER, 5), stage("2026-01-16T02:30:00Z", "2026-01-16T04:00:00Z", 6),
                  stage("2026-01-16T05:00:00Z", E, 3)]
        for offsets in (("+05:30", "+05:30"), ("-08:00", "+09:00"), (None, None)):
            with self.subTest(offsets=offsets):
                timeline = UTC_ALWAYS if offsets == (None, None) else ()
                projection = session(stages=stages, start_offset=offsets[0], end_offset=offsets[1])
                outcome = self.assert_body(projection, timeline)
                body = json.loads(outcome.body_bytes)
                self.assertEqual(set(body), {"stages", "title", "notes"})
                strings = {HEALTH_MARKER} | {item[key] for item in stages for key in ("start_time", "end_time")}
                numbers = {item["stage"] for item in stages}

                def walk(value):
                    if isinstance(value, dict):
                        for item in value.values():
                            walk(item)
                    elif isinstance(value, list):
                        for item in value:
                            walk(item)
                    elif isinstance(value, str):
                        self.assertIn(value, strings)
                    else:
                        self.assertIs(type(value), int)
                        self.assertIn(value, numbers)

                walk(body)
                for word in (b"asleep", b"awake", b"class", b"duration", b"total", b"date", b"wake", b"gap",
                             b"unclassified", b"local"):
                    self.assertNotIn(word, outcome.body_bytes)
                # One revision; the wake date is the header's end local date.
                self.assertEqual(outcome.header.time.shape, "interval")
                self.assertEqual(outcome.header.time.end.instant.text, E)


# ---- R6 ----

class R6TotalPureFastHealthDataFree(Checks):
    def test_schema_valid_edges_never_raise(self) -> None:
        edges = [
            session(stages=[stage("0000-01-01T00:00:00Z", "9999-12-31T23:59:59.999999999Z")]),
            session(stages=[stage(S, "9999-12-31T23:59:59.999999999Z")]),
            session(stages=[stage("9999-12-31T23:59:59.999999999Z", "0000-01-01T00:00:00Z")]),
            session(stages=[stage(S, "0000-01-01T00:00:00.000000001Z")]),
            session(stages=[stage("0000-02-29T23:59:59.999Z", "0000-03-01T00:00:00Z")]),
            session(stages=[stage(S, E, 0)] * 3, title="", notes=""),
            session(S, S, [stage("0000-01-01T00:00:00Z", "0000-01-01T00:00:00Z")]),
            session("0001-01-01T00:00:00Z", "9999-12-31T23:59:59.999999999Z",
                    [stage("0001-01-01T00:00:00Z", "9999-12-31T23:59:59.999999999Z", 7),
                     stage("2000-02-29T00:00:00Z", "2400-02-29T00:00:00Z", 0)],
                    start_offset="+00:00", end_offset="+00:00"),
            session("0001-01-01T00:00:00Z", "0001-01-01T00:00:00.000000001Z",
                    [stage("0000-12-31T23:59:59.999999999Z", "0001-01-01T00:00:00.000000001Z")],
                    start_offset="+00:00", end_offset="+00:00"),
        ]
        for index, projection in enumerate(edges):
            with self.subTest(edge=index):
                self.check_against_oracle(projection)
        # Integer codes written as JSON floats are schema-valid and keep their integer code.
        for written, code in ((b"5.0", 5), (b"-0.0", 0), (b"7e0", 7), (b"0.0", 0), (b"30E-1", 3), (b"1.00", 1)):
            with self.subTest(code=written):
                data = encode(session(stages=[stage(S, E, 424242)])).replace(b"424242", written)
                outcome = self.revision(data)
                self.assertEqual(outcome.body_bytes, expected_body_bytes(session(stages=[stage(S, E, code)])["record"]))
        # A seeded sample of schema-valid sessions near every bound, against the independent oracle.
        rng = random.Random(20261124)
        base = epoch_ns(S)
        for case in range(400):
            length = rng.choice([0, 1, 2, 10**9, 3600 * 10**9, 8 * 3600 * 10**9 + 7])
            start = base + rng.choice([0, 1, 999, 10**9 - 1]) * rng.choice([0, 1])
            end = start + length
            points = [start - 10**9, start - 1, start, start + 1, (start + end) // 2, end - 1, end, end + 1, end + 10**9]
            stages = []
            for _ in range(rng.choice([0, 1, 2, 3, 5])):
                first, last = rng.choice(points), rng.choice(points)
                if rng.random() < 0.3:
                    last = first + rng.choice([-1, 0, 1, 60 * 10**9])
                stages.append(stage(text_of(first, rng.choice([0, 3, 6, 9])), text_of(last, rng.choice([0, 3, 6, 9])),
                                    rng.randrange(8)))
            if rng.random() < 0.05:
                stages.append(stage("0000-12-31T23:59:59.999999999Z", text_of(start, 9)))
            projection = session(text_of(start, rng.choice([0, 3, 6, 9])), text_of(end, rng.choice([0, 3, 6, 9])),
                                 stages, title=rng.choice([HEALTH_MARKER, ABSENT, ""]),
                                 notes=rng.choice([HEALTH_MARKER, ABSENT]))
            with self.subTest(case=case):
                self.check_against_oracle(projection, reverse=bool(case % 2))

    def check_against_oracle(self, projection, *, reverse: bool = False) -> None:
        try:
            outcome = run(projection, reverse=reverse)
        except InterpretFailure as failure:
            self.fail(f"interpret raised {failure.failure}")
        expected = expected_outcome(projection["record"])
        if expected[0] == "body":
            self.assertIsInstance(outcome, CanonicalRevision, repr(outcome))
            self.assertEqual(outcome.body_bytes, expected[1])
        else:
            self.assertEqual(outcome, Quarantine(expected[1], expected[2], VERSION, "none"))

    def test_processor_is_pure(self) -> None:
        expected = scenario_summary()
        self.assertEqual(scenario_summary(), expected)
        code = r"""
import json, os, sys, time, zoneinfo
# Modules the suite itself, or an implementation, commonly needs are imported first, so that only the
# implementation modules' own imports and their calls run under observation.
import tzdata, logging, decimal, fractions, hashlib, re, importlib.resources, pathlib, dataclasses, random
import typing, collections, functools, itertools, calendar, math, unicodedata, copy, enum, struct, base64, datetime
import string, operator, bisect, heapq, io, traceback, unittest, subprocess, tempfile, shutil, uuid, types
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
import activity_tracker_portal.interpret_sleep_session as module
import test_interpret_sleep_session_1 as t
summary = t.scenario_summary()
print(json.dumps({"store": "activity_tracker_portal.operational_store" in sys.modules, "events": events,
                  "summary": summary}))
"""
        for settings in ({"TZ": "Pacific/Chatham", "LC_ALL": "C", "PYTHONHASHSEED": "1"},
                         {"TZ": "America/Los_Angeles", "LC_ALL": "en_US.UTF-8", "PYTHONHASHSEED": "2"}):
            with self.subTest(**settings):
                result = json.loads(run_python(code, settings))
                self.assertEqual(result, {"store": False, "events": [], "summary": expected})

    def test_large_session_is_interpreted(self) -> None:
        count = 100_000
        base = epoch_ns("2026-01-15T20:00:00Z")
        widths = (0, 3, 6, 9)
        stages = [stage(text_of(base + i * 10**9, widths[i % 4]), text_of(base + (i + 1) * 10**9 + (i % 3 == 0) * 5 * 10**8,
                                                                            widths[(i + 1) % 4]), i % 8)
                  for i in range(count - 1)]
        stages.append(stage(text_of(base, 9), text_of(base + count * 10**9, 0), 0))
        start, end = text_of(base), text_of(base + count * 10**9)
        projection = session(start, end, stages)
        data = encode(projection)
        began = time.perf_counter()
        outcome = run(data)
        elapsed = time.perf_counter() - began
        self.assertIsInstance(outcome, CanonicalRevision, repr(outcome))
        self.assertLess(elapsed, LARGE_SESSION_SECONDS)
        self.assertEqual(outcome.body_bytes, expected_body_bytes(projection["record"]))
        self.assertEqual(len(json.loads(outcome.body_bytes)["stages"]), count)
        # The last stage is checked too: a fault there names its index.
        faulty = copy.deepcopy(projection)
        faulty["record"]["stages"][-1]["end_time"] = text_of(base + count * 10**9 + 1)
        began = time.perf_counter()
        outcome = run(faulty)
        elapsed = time.perf_counter() - began
        self.assertEqual(outcome, Quarantine(OUTSIDE, f"/record/stages/{count - 1}/end_time", VERSION, "none"))
        self.assertLess(elapsed, LARGE_SESSION_SECONDS)

    def test_outcomes_are_health_data_free(self) -> None:
        marker_title = f"{HEALTH_MARKER} /record/title {S_MINUS_1} \"quoted\""
        cases = [
            (session(S, S, [stage(S, S)], title=marker_title), EMPTY, "/record/end_time"),
            (session(stages=[stage(S, MID), stage(S_MINUS_1, E)], title=marker_title), OUTSIDE, "/record/stages/1/start_time"),
            (session(stages=[stage(MID, E_PLUS_1)], title=marker_title), OUTSIDE, "/record/stages/0/end_time"),
            (session(stages=[stage(MID_LATER, MID)], title=marker_title), REVERSED, "/record/stages/0/end_time"),
            (session(stages=[stage(MID, MID)], title=marker_title), STAGE_EMPTY, "/record/stages/0/end_time"),
            (session(start_offset=None, end_offset=None, stages=[stage(MID, MID)], title=marker_title, notes=marker_title),
             STAGE_EMPTY, "/record/stages/0/end_time"),
        ]
        seen = set()
        for projection, reason, pointer in cases:
            with self.subTest(reason=reason, pointer=pointer):
                policies = UTC_ALWAYS if "start_zone_offset" not in projection["record"] else ()
                outcome = run(projection, policies)
                self.assertIsInstance(outcome, Quarantine, repr(outcome))
                self.assertEqual({field.name for field in dataclasses.fields(outcome)},
                                 {"reason", "pointer", "processor_version", "policy_identity"})
                self.assertEqual((outcome.reason, outcome.pointer, outcome.processor_version),
                                 (reason, pointer, VERSION))
                self.assertRegex(outcome.pointer, r"^/record/(end_time|stages/(0|[1-9][0-9]*)/(start_time|end_time))$")
                seen.add(outcome.reason)
                record = projection["record"]
                values = [record["start_time"], record["end_time"], record.get("title"), record.get("notes")]
                values += [item[key] for item in record["stages"] for key in ("start_time", "end_time")]
                for text in (repr(outcome), str(outcome), outcome.reason, outcome.pointer, outcome.policy_identity):
                    self.assert_health_free(text)
                    self.assertNotIn("quoted", text)
                    for value in values:
                        if value:
                            self.assertNotIn(value, text)
        self.assertEqual(seen, set(REASONS))


if __name__ == "__main__":
    unittest.main()
