"""Acceptance tests for `summary-contract@1`.

Requirement numbers refer to `packs/summary-contract-1.spec.md`. Every input is synthetic and built by
hand: evidence entries, delivery positions, bodies, and policy identities. Expected bytes and digests
come from an RFC 8785 encoder and a model of R4-R11 written in this file, never from the unit's own
encoder. Markers in body values and other inputs prove that failures and reprs carry no input value.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import itertools
import json
import os
import re
import subprocess
import sys
import traceback
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from activity_tracker_portal import set_source_selection_policy
from activity_tracker_portal.summary_contract import (
    COMPUTED_CONDITIONS,
    CONTRACT_VERSION,
    EVIDENCE_CONDITIONS,
    FAILURES,
    MEASURES,
    NO_VALUE_PRECEDENCE,
    PRIMARY_STATES,
    PUBLICATION_CONDITIONS,
    REPORTED_CONDITIONS,
    STANDINGS,
    Coverage,
    DataWatermark,
    DeliveryPosition,
    EvidenceEntry,
    HealthSummary,
    SummaryContractFailure,
    SummaryHeader,
    build_summary,
    check_publication_conditions,
    coverage_of,
    decode_summary,
    primary_state,
)

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CONTRACT = json.loads((ROOT / "governance/scopes/summary-contract.1.json").read_text(encoding="utf-8"))
EFFECTS = json.loads((ROOT / "packs/summary-contract-1.effects.json").read_text(encoding="utf-8"))
SCOPE_MAP = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PDT = timezone(timedelta(hours=-7))
V1 = "summary-contract@1"
KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
INVALID = "summary-input-invalid"
INCONSISTENT = "summary-state-inconsistent"
BYTES_INVALID = "summary-bytes-invalid"
UNSUPPORTED = "summary-version-unsupported"
HEALTH_MARKER = "synthetic-health-marker"
MARKER_VALUE = 7314159
DAY = date(2026, 1, 15)
DERIVATION = "derive-synthetic-steps@1"
PROCESSOR = "interpret-steps@1"
FALLBACK_A = 'fallback-timezone:[["policy-a"],"2025b"]'
FALLBACK_B = 'fallback-timezone:[["policy-b","policy-a"],"2025b"]'
RECEIPT = datetime(2026, 1, 15, 22, 0, 0, 123456, tzinfo=UTC)

# The spec's vocabularies, written out independently of the module.
STANDING_NAMES = ("contributing", "unresolved", "in-conflict", "policy-pending", "failed", "quarantined")
NON_CONTRIBUTING = STANDING_NAMES[1:]
COVERAGE_FIELD = {
    "contributing": "contributing", "unresolved": "unresolved", "in-conflict": "in_conflict",
    "policy-pending": "policy_pending", "failed": "failed", "quarantined": "quarantined",
}
UNRESOLVED_CONDITIONS = ("classification-conflict", "equal-time-conflict", "selection-unresolved", "unallocatable-interval")
HEADER_KEYS = {
    "contract_version", "derivation_version", "measure", "record_type", "local_date", "processor_version",
    "source_selection_policy", "fallback_timezone_identities", "primary_state", "evidence_conditions",
    "coverage", "watermark", "body_sha256",
}
ENTRY_KEYS = {"record_id", "selection_id", "outcome_id", "standing", "processor_version", "policy_identity"}
POSITION_KEYS = {"installation_id", "sequence", "receipt_time", "processed_at"}
# Which of outcome_id, processor_version, policy_identity each standing carries (R5).
CARRIES = {
    "contributing": (True, True, True),
    "unresolved": (True, True, True),
    "in-conflict": (False, False, False),
    "policy-pending": (True, True, True),
    "failed": (False, True, False),
    "quarantined": (True, True, True),
}


# ---- Independent oracles ----

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
    if value is None:
        return "null"
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


def enc(value) -> bytes:
    return canonical(value).encode("utf-8")


def is_canonical(data: bytes) -> bool:
    return enc(json.loads(data)) == data


def utc_text(moment: datetime) -> str:
    m = moment.astimezone(UTC)
    return (f"{m.year:04d}-{m.month:02d}-{m.day:02d}T{m.hour:02d}:{m.minute:02d}:{m.second:02d}"
            f".{m.microsecond:06d}Z")


def decimal(value: int | None) -> str | None:
    return None if value is None else str(value)


def entry_json(e: EvidenceEntry) -> dict:
    return {
        "record_id": decimal(e.record_id), "selection_id": decimal(e.selection_id),
        "outcome_id": decimal(e.outcome_id), "standing": e.standing,
        "processor_version": e.processor_version, "policy_identity": e.policy_identity,
    }


def position_json(p: DeliveryPosition) -> dict:
    return {"installation_id": p.installation_id, "sequence": decimal(p.sequence),
            "receipt_time": utc_text(p.receipt_time), "processed_at": utc_text(p.processed_at)}


def counts_of(entries) -> dict[str, int]:
    return {standing: sum(1 for e in entries if e.standing == standing) for standing in STANDING_NAMES}


def model_state(counts: dict[str, int], has_value: bool) -> str:
    """R4, written out independently. Returns INCONSISTENT where the rule refuses."""
    if has_value:
        if counts["contributing"] == 0:
            return INCONSISTENT
        return "measured" if all(counts[s] == 0 for s in NON_CONTRIBUTING) else "partial"
    if sum(counts.values()) == 0:
        return "missing"
    for standing in ("failed", "quarantined", "policy-pending"):
        if counts[standing]:
            return standing
    if counts["unresolved"] or counts["in-conflict"]:
        return "unresolved"
    return INCONSISTENT


def model(f: dict) -> tuple[dict, bytes]:
    """The header object and manifest bytes that R4-R11 require for valid facts `f`."""
    entries = tuple(f["evidence"])
    manifest = enc({"contract_version": V1,
                    "evidence": [entry_json(e) for e in sorted(entries, key=lambda e: e.record_id)]})
    counts = counts_of(entries)
    state = model_state(counts, f["body"] is not None)
    assert state != INCONSISTENT, "model() is only for consistent facts"
    conditions = set(f["reported_conditions"])
    if counts["in-conflict"]:
        conditions.add("source-conflict")
    if any(e.standing == "contributing" and e.policy_identity != "none" for e in entries):
        conditions.add("inferred-attribution")
    header = {
        "contract_version": V1,
        "derivation_version": f["derivation_version"],
        "measure": f["measure"],
        "record_type": f["record_type"],
        "local_date": f["local_date"].isoformat(),
        "processor_version": f["processor_version"],
        "source_selection_policy": decimal(f["source_selection_policy"]),
        "fallback_timezone_identities": sorted({e.policy_identity for e in entries
                                                if e.policy_identity not in (None, "none")}),
        "primary_state": state,
        "evidence_conditions": sorted(conditions),
        "coverage": counts,
        "watermark": {"evidence_set_sha256": sha(manifest),
                      "positions": [position_json(p) for p in sorted(f["positions"], key=lambda p: p.installation_id)]},
        "body_sha256": None if f["body"] is None else sha(f["body"]),
    }
    return header, manifest


def model_header(f: dict, header: dict) -> SummaryHeader:
    return SummaryHeader(
        contract_version=V1,
        derivation_version=f["derivation_version"],
        measure=f["measure"],
        record_type=f["record_type"],
        local_date=f["local_date"],
        processor_version=f["processor_version"],
        source_selection_policy=f["source_selection_policy"],
        fallback_timezone_identities=tuple(header["fallback_timezone_identities"]),
        primary_state=header["primary_state"],
        evidence_conditions=tuple(header["evidence_conditions"]),
        coverage=Coverage(**{COVERAGE_FIELD[s]: n for s, n in header["coverage"].items()}),
        watermark=DataWatermark(tuple(sorted(f["positions"], key=lambda p: p.installation_id)),
                                header["watermark"]["evidence_set_sha256"]),
        body_sha256=header["body_sha256"],
    )


def json_scalars(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from json_scalars(item)
    elif isinstance(value, list):
        for item in value:
            yield from json_scalars(item)
    else:
        yield value


# ---- Synthetic facts ----

_DEFAULT = object()


def entry(record_id: int, standing: str = "contributing", *, selection_id=_DEFAULT, outcome_id=_DEFAULT,
          processor_version=_DEFAULT, policy_identity=_DEFAULT) -> EvidenceEntry:
    has_outcome, has_processor, has_policy = CARRIES.get(standing, (True, True, True))
    return EvidenceEntry(
        record_id=record_id,
        selection_id=record_id + 500 if selection_id is _DEFAULT else selection_id,
        outcome_id=(record_id + 9000 if has_outcome else None) if outcome_id is _DEFAULT else outcome_id,
        standing=standing,
        processor_version=(PROCESSOR if has_processor else None) if processor_version is _DEFAULT else processor_version,
        policy_identity=("none" if has_policy else None) if policy_identity is _DEFAULT else policy_identity,
    )


def position(installation_id: str = "installation-a", sequence: int = 41, receipt: datetime = RECEIPT,
             processed: datetime | None = None) -> DeliveryPosition:
    return DeliveryPosition(installation_id, sequence, receipt,
                            receipt + timedelta(seconds=3, microseconds=1) if processed is None else processed)


def body_of(value: dict) -> bytes:
    return enc(value)


BODY = body_of({"marker": HEALTH_MARKER, "total": MARKER_VALUE})


def facts(**overrides) -> dict:
    base = {
        "measure": "daily-steps",
        "record_type": "steps",
        "local_date": DAY,
        "derivation_version": DERIVATION,
        "processor_version": PROCESSOR,
        "source_selection_policy": None,
        "evidence": (entry(11), entry(12)),
        "reported_conditions": (),
        "positions": (position(),),
        "body": BODY,
    }
    unknown = set(overrides) - set(base)
    assert not unknown, unknown
    base.update(overrides)
    return base


def evidence_with(counts: dict[str, int], first_id: int = 1) -> tuple[EvidenceEntry, ...]:
    out = []
    record_id = first_id
    for standing in STANDING_NAMES:
        for _ in range(counts.get(standing, 0)):
            out.append(entry(record_id, standing))
            record_id += 1
    return tuple(out)


def mixed_evidence() -> tuple[EvidenceEntry, ...]:
    """Every standing once, a fallback identity on a contributor and on an unresolved record."""
    return (
        entry(17, "failed"),
        entry(12, policy_identity=FALLBACK_A),
        entry(14, "in-conflict"),
        entry(11),
        entry(16, "policy-pending"),
        entry(13, "unresolved", policy_identity=FALLBACK_B),
        entry(15, "quarantined"),
    )


def two_positions() -> tuple[DeliveryPosition, ...]:
    return (
        position("installation-b", 7, datetime(2026, 1, 15, 3, 30, tzinfo=IST),
                 datetime(2026, 1, 14, 22, 0, 1, 500000, tzinfo=UTC)),
        position("installation-a", 12),
    )


def cov(**counts: int) -> Coverage:
    values = {name: 0 for name in COVERAGE_FIELD.values()}
    values.update(counts)
    return Coverage(**values)


def split(summary: HealthSummary) -> tuple[dict, dict, bytes | None]:
    return json.loads(summary.header_bytes), json.loads(summary.evidence_bytes), summary.body_bytes


def stamp(header: dict, manifest: dict, body: bytes | None) -> tuple[bytes, bytes, bytes | None]:
    """Canonical bytes with the header's digests recomputed, so only the intended change is wrong."""
    manifest_bytes = enc(manifest)
    if isinstance(header.get("watermark"), dict) and "evidence_set_sha256" in header["watermark"]:
        header["watermark"]["evidence_set_sha256"] = sha(manifest_bytes)
    if "body_sha256" in header:
        header["body_sha256"] = None if body is None else sha(body)
    return enc(header), manifest_bytes, body


def scenario_facts() -> list[dict]:
    return [
        facts(),
        facts(evidence=mixed_evidence(), positions=two_positions(), source_selection_policy=2**70 + 3,
              reported_conditions=frozenset({"selection-unresolved", "equal-time-conflict", "unallocatable-interval",
                                             "classification-conflict"})),
        facts(evidence=(), body=None),
        facts(evidence=(entry(3, "failed"), entry(2, "quarantined"), entry(1, "unresolved")), body=None,
              positions=(position("installation-z", 2**64), position("installation-m", 5),
                         position("installation-q", 9, datetime(2026, 1, 15, 9, 0, tzinfo=PDT)))),
        facts(evidence=(entry(5), entry(4), entry(3, policy_identity=FALLBACK_B)),
              reported_conditions={"duplicate-grouped"}, body=body_of({"total": 0})),
    ]


def scenario_summary() -> str:
    """Every operation over fixed synthetic inputs, rendered as text for comparison across processes."""
    out = []
    for f in scenario_facts():
        summary = build_summary(**f)
        decoded = decode_summary(summary.header_bytes, summary.evidence_bytes, summary.body_bytes)
        out.append([summary.header_bytes.hex(), summary.evidence_bytes.hex(),
                    None if summary.body_bytes is None else summary.body_bytes.hex(),
                    summary.summary_sha256, decoded == summary, repr(summary)])
    out.append(list(check_publication_conditions(frozenset(PUBLICATION_CONDITIONS))))
    out.append(repr(coverage_of(mixed_evidence())))
    out.append([primary_state(Coverage(1, 1, 0, 0, 0, 0), True), primary_state(Coverage(0, 1, 1, 1, 1, 1), False)])
    bad_calls = [
        lambda: build_summary(**facts(reported_conditions=frozenset({"source-conflict", "conflicted"}))),
        lambda: build_summary(**facts(evidence=(entry(1, "failed"),))),
        lambda: decode_summary(b"{}", b"{}", None),
        lambda: check_publication_conditions({"conflicted", "selection-unresolved"}),
    ]
    for call in bad_calls:
        try:
            call()
        except SummaryContractFailure as failure:
            out.append(list(failure.args))
    return json.dumps(out)


def run_python(code: str, env_updates: dict[str, str]) -> tuple[str, str]:
    env = dict(os.environ)
    env.update(env_updates)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(HERE), env.get("PYTHONPATH", "")]))
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(ROOT),
                          timeout=300)
    if done.returncode != 0:
        raise AssertionError(f"subprocess failed ({done.returncode}):\n{done.stderr[-3000:]}")
    return done.stdout, done.stderr


class Checks(unittest.TestCase):
    maxDiff = None

    def built(self, **overrides) -> HealthSummary:
        """Build from facts, check every byte against the model, and check the decoder round-trips it."""
        f = facts(**overrides)
        summary = build_summary(**f)
        header, manifest = model(f)
        self.assertIsInstance(summary, HealthSummary)
        self.assertEqual(json.loads(summary.header_bytes), header)
        self.assertEqual(summary.header_bytes, enc(header))
        self.assertEqual(summary.evidence_bytes, manifest)
        self.assertEqual(summary.body_bytes, f["body"])
        self.assertEqual(summary.summary_sha256, sha(summary.header_bytes))
        self.assertEqual(summary.header, model_header(f, header))
        self.assertEqual(decode_summary(summary.header_bytes, summary.evidence_bytes, summary.body_bytes), summary)
        return summary

    def refused(self, call, *args, **kwargs) -> str:
        with self.assertRaises(SummaryContractFailure) as caught:
            call(*args, **kwargs)
        failure = caught.exception
        self.assertIs(type(failure), SummaryContractFailure)
        self.assertEqual(len(failure.args), 1)
        self.assertIn(failure.args[0], FAILURES)
        return failure.args[0]

    def build_refused(self, **overrides) -> str:
        return self.refused(build_summary, **facts(**overrides))

    def decode_refused(self, header_bytes, evidence_bytes, body_bytes) -> str:
        return self.refused(decode_summary, header_bytes, evidence_bytes, body_bytes)

    def assert_marker_free(self, text: str) -> None:
        self.assertNotIn(HEALTH_MARKER, text)
        self.assertNotIn(str(MARKER_VALUE), text)


# ---- R1 ----

class R1GovernedScopeContract(unittest.TestCase):
    def test_contract_shape_and_dependencies(self) -> None:
        self.assertEqual((CONTRACT["contract_schema"], CONTRACT["scope"], CONTRACT["version"], CONTRACT["kind"]),
                         (1, "summary-contract", "1", "shared"))
        self.assertEqual(CONTRACT["dependencies"], [
            {"scope": "interpret-source-projection", "version": "1"},
            {"scope": "set-source-selection-policy", "version": "1"},
            {"scope": "set-fallback-timezone-policy", "version": "1"},
        ])
        self.assertEqual([(item["name"], item["outcome"]) for item in CONTRACT["named_failures"]], [
            ("summary-input-invalid", "invariant-refusal"),
            ("summary-state-inconsistent", "invariant-refusal"),
            ("summary-bytes-invalid", "invariant-refusal"),
            ("summary-version-unsupported", "refusal"),
        ])
        self.assertEqual(CONTRACT["observable_effects"], [])
        self.assertEqual({item["mode"] for item in CONTRACT["inputs"]}, {"pure"})
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["specification"], "packs/summary-contract-1.spec.md")
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_summary_contract_1.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_summary_contract_1.sh")
        self.assertEqual(oracle["languages"], ["python"])

    def test_effects_declaration_is_empty(self) -> None:
        self.assertEqual(EFFECTS, {"effects_format": 1, "effects": []})

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        row = next(line for line in SCOPE_MAP.splitlines() if line.startswith("| `summary-contract` |"))
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        self.assertEqual(cells[1], "shared")
        self.assertEqual(set(re.findall(r"`([a-z0-9-]+)`", cells[-1])),
                         {item["scope"] for item in CONTRACT["dependencies"]})
        for state in ("measured", "partial", "missing", "failed", "quarantined", "policy-pending", "unresolved"):
            self.assertRegex(row, rf"(?<![a-z-]){state}(?![a-z-])")
        # Decision 3: the row names the state policy-pending, never a bare "pending".
        self.assertNotRegex(row, r"(?<![a-z-])pending(?![a-z-])")
        self.assertIn("Data Watermark", row)


# ---- R2 ----

class R2PureAndDeterministic(Checks):
    def test_same_input_gives_identical_bytes(self) -> None:
        conditions = ["selection-unresolved", "equal-time-conflict"]
        first = self.built(evidence=mixed_evidence(), positions=two_positions(), source_selection_policy=7,
                           reported_conditions=conditions)
        self.assertEqual(conditions, ["selection-unresolved", "equal-time-conflict"])
        # Equal facts in other orders and other objects give the same bytes.
        second = build_summary(**facts(
            evidence=tuple(dataclasses.replace(e) for e in reversed(mixed_evidence())),
            positions=tuple(reversed(two_positions())), source_selection_policy=7,
            reported_conditions=("equal-time-conflict", "selection-unresolved", "selection-unresolved"),
            body=bytes(bytearray(BODY))))
        third = build_summary(**facts(evidence=mixed_evidence(), positions=two_positions(), source_selection_policy=7,
                                      reported_conditions=frozenset(conditions)))
        for other in (second, third):
            self.assertEqual(other, first)
            self.assertEqual((other.header_bytes, other.evidence_bytes, other.body_bytes, other.summary_sha256),
                             (first.header_bytes, first.evidence_bytes, first.body_bytes, first.summary_sha256))
        self.assertEqual(scenario_summary(), scenario_summary())

    def test_result_ignores_host_timezone_locale_and_hash_seed(self) -> None:
        expected = scenario_summary()
        code = "import test_summary_contract_1 as t; print(t.scenario_summary())"
        for settings in (
            {"TZ": "America/Los_Angeles", "LC_ALL": "C", "LANG": "C", "PYTHONHASHSEED": "1"},
            {"TZ": "Asia/Kolkata", "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8", "PYTHONHASHSEED": "2"},
            {"TZ": "Pacific/Chatham", "LC_ALL": "de_DE.UTF-8", "LANG": "de_DE.UTF-8", "PYTHONHASHSEED": "3"},
        ):
            with self.subTest(**settings):
                stdout, _ = run_python(code, settings)
                self.assertEqual(stdout.strip().splitlines()[-1], expected)

    def test_operations_read_no_clock_or_io(self) -> None:
        scenario_summary()
        code = r"""
import json, os, sys, time, logging, zoneinfo
# Modules an implementation commonly needs are imported first. The unit's own import may import
# operational_store through set_source_selection_policy (Decision 16); that import is allowed.
import hashlib, re, decimal, fractions, dataclasses, typing, collections, functools, itertools, math, unicodedata
import copy, enum, struct, base64, datetime, string, operator, bisect, heapq, io, traceback, types, inspect
import contextlib, pathlib, sqlite3, shutil, importlib.resources, collections.abc, numbers, codecs, unittest
host = [os.path.realpath(p) for p in zoneinfo.TZPATH] + [os.path.realpath("/etc/localtime"), "/etc/localtime"]
events = []
phase = ["import"]
WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
def hook(event, args):
    if phase[0] == "done":
        return
    if event == "open":
        path, mode, flags = args
        if isinstance(path, (str, bytes)):
            path = os.fsdecode(path)
            if (mode and any(c in mode for c in "wax+")) or (flags or 0) & WRITE:
                events.append(["write", path])
            real = os.path.realpath(path)
            if any(real == h or real.startswith(h.rstrip("/") + "/") for h in host):
                events.append(["host-timezone", path])
            if phase[0] == "calls" and not path.endswith((".py", ".pyc", ".so")):
                events.append(["read", path])
    elif event.split(".")[0] in ("socket", "sqlite3", "subprocess", "ctypes", "webbrowser", "urllib", "http") or event in (
            "os.system", "os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.chmod", "os.utime", "os.truncate",
            "os.symlink", "os.link", "os.posix_spawn", "os.exec", "os.fork", "shutil.copyfile", "shutil.rmtree"):
        events.append([event])
    elif phase[0] == "calls" and event in ("os.listdir", "os.scandir", "glob.glob", "os.chdir"):
        events.append([event])
def forbidden(*args, **kwargs):
    raise AssertionError("clock read")
def explicit(original, index):
    def guard(*args):
        if len(args) <= index or args[index] is None:
            raise AssertionError("clock read")
        return original(*args)
    return guard
class Environ(collections.abc.MutableMapping):
    def __init__(self, inner):
        self.inner, self.reads = inner, []
    def note(self, key):
        self.reads.append(str(key)[:40])
    def __getitem__(self, key):
        self.note(key); return self.inner[key]
    def __setitem__(self, key, value):
        self.note(key); self.inner[key] = value
    def __delitem__(self, key):
        self.note(key); del self.inner[key]
    def __iter__(self):
        self.note("*"); return iter(self.inner)
    def __len__(self):
        self.note("*"); return len(self.inner)
    def __contains__(self, key):
        self.note(key); return key in self.inner
records = []
class Handler(logging.Handler):
    def emit(self, record):
        records.append(record.name)
logging.getLogger().addHandler(Handler())
logging.getLogger().setLevel(logging.DEBUG)
sys.addaudithook(hook)
for name in ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns", "process_time",
             "process_time_ns", "thread_time", "thread_time_ns", "clock_gettime", "clock_gettime_ns",
             "localtime", "ctime", "mktime"):
    if hasattr(time, name):
        setattr(time, name, forbidden)
time.gmtime = explicit(time.gmtime, 0)
time.asctime = explicit(time.asctime, 0)
time.strftime = explicit(time.strftime, 1)
import activity_tracker_portal.summary_contract as module
import test_summary_contract_1 as t
original = os.environ
recorder = Environ(original)
os.environ = recorder
phase[0] = "calls"
t.scenario_summary()
phase[0] = "done"
os.environ = original
print(json.dumps({"events": events, "environment": recorder.reads, "logs": records}))
"""
        stdout, stderr = run_python(code, {"TZ": "Europe/London"})
        self.assertEqual(stderr, "")
        self.assertEqual(len(stdout.strip().splitlines()), 1, stdout)
        self.assertEqual(json.loads(stdout), {"events": [], "environment": [], "logs": []})

    def test_values_are_frozen(self) -> None:
        moment = datetime(2026, 1, 15, tzinfo=UTC)
        entry_value = EvidenceEntry(1, 2, 3, "contributing", PROCESSOR, "none")
        coverage = Coverage(1, 0, 0, 0, 0, 0)
        delivery = DeliveryPosition("installation-a", 1, moment, moment)
        watermark = DataWatermark((delivery,), "0" * 64)
        header = SummaryHeader(
            contract_version=V1, derivation_version=DERIVATION, measure="daily-steps", record_type="steps",
            local_date=DAY, processor_version=PROCESSOR, source_selection_policy=None,
            fallback_timezone_identities=(), primary_state="measured", evidence_conditions=(),
            coverage=coverage, watermark=watermark, body_sha256="1" * 64)
        summary = HealthSummary(header, b"{}", b"{}", b"{}", "2" * 64)
        expected_fields = {
            EvidenceEntry: ["record_id", "selection_id", "outcome_id", "standing", "processor_version", "policy_identity"],
            Coverage: ["contributing", "unresolved", "in_conflict", "policy_pending", "failed", "quarantined"],
            DeliveryPosition: ["installation_id", "sequence", "receipt_time", "processed_at"],
            DataWatermark: ["positions", "evidence_set_sha256"],
            HealthSummary: ["header", "header_bytes", "evidence_bytes", "body_bytes", "summary_sha256"],
        }
        for value in (entry_value, coverage, delivery, watermark, header, summary):
            with self.subTest(type(value).__name__):
                self.assertTrue(dataclasses.is_dataclass(value))
                self.assertTrue(type(value).__dataclass_params__.frozen)
                names = [item.name for item in dataclasses.fields(value)]
                if type(value) in expected_fields:
                    self.assertEqual(names, expected_fields[type(value)])
                else:
                    self.assertEqual(set(names), HEADER_KEYS)
                for name in names:
                    with self.assertRaises(dataclasses.FrozenInstanceError):
                        setattr(value, name, None)
        self.assertTrue(issubclass(SummaryContractFailure, Exception))
        self.assertEqual(SummaryContractFailure(INVALID).args, (INVALID,))


# ---- R3 ----

class R3Vocabularies(unittest.TestCase):
    def test_vocabularies_are_exact(self) -> None:
        self.assertEqual(CONTRACT_VERSION, "summary-contract@1")
        self.assertEqual(PRIMARY_STATES, ("measured", "partial", "missing", "failed", "quarantined", "policy-pending",
                                          "unresolved"))
        self.assertEqual(NO_VALUE_PRECEDENCE, ("failed", "quarantined", "policy-pending", "unresolved"))
        self.assertEqual(STANDINGS, ("contributing", "unresolved", "in-conflict", "policy-pending", "failed",
                                     "quarantined"))
        self.assertEqual(REPORTED_CONDITIONS, ("classification-conflict", "duplicate-grouped", "equal-time-conflict",
                                               "selection-unresolved", "unallocatable-interval"))
        self.assertEqual(COMPUTED_CONDITIONS, ("inferred-attribution", "source-conflict"))
        self.assertEqual(EVIDENCE_CONDITIONS, (
            "classification-conflict", "duplicate-grouped", "equal-time-conflict", "inferred-attribution",
            "selection-unresolved", "source-conflict", "unallocatable-interval"))
        self.assertEqual(EVIDENCE_CONDITIONS, tuple(sorted(REPORTED_CONDITIONS + COMPUTED_CONDITIONS)))
        self.assertEqual(PUBLICATION_CONDITIONS, ("conflicted", "newer-evidence-pending", "reconciliation-failed",
                                                  "reconciliation-pending", "refresh-failed", "superseded"))
        self.assertEqual(FAILURES, ("summary-input-invalid", "summary-state-inconsistent", "summary-bytes-invalid",
                                    "summary-version-unsupported"))
        self.assertEqual(list(FAILURES), [item["name"] for item in CONTRACT["named_failures"]])
        for vocabulary in (PRIMARY_STATES, NO_VALUE_PRECEDENCE, STANDINGS, REPORTED_CONDITIONS, COMPUTED_CONDITIONS,
                           EVIDENCE_CONDITIONS, PUBLICATION_CONDITIONS, FAILURES):
            self.assertIs(type(vocabulary), tuple)
            for name in vocabulary:
                self.assertRegex(name, KEBAB)

    def test_measures_are_the_published_vocabulary(self) -> None:
        self.assertIs(MEASURES, set_source_selection_policy.MEASURES)
        for measure, record_type in (("daily-steps", "steps"), ("heart-rate-summary", "heart-rate"),
                                     ("sleep-summary", "sleep-session"), ("weight-series", "weight")):
            self.assertIn(record_type, MEASURES[measure])
        for measure, record_types in MEASURES.items():
            self.assertRegex(measure, KEBAB)
            for record_type in record_types:
                self.assertRegex(record_type, KEBAB)

    def test_vocabularies_do_not_collide(self) -> None:
        vocabularies = {
            "PRIMARY_STATES": PRIMARY_STATES, "STANDINGS": STANDINGS, "EVIDENCE_CONDITIONS": EVIDENCE_CONDITIONS,
            "PUBLICATION_CONDITIONS": PUBLICATION_CONDITIONS, "FAILURES": FAILURES,
        }
        for name, vocabulary in vocabularies.items():
            self.assertEqual(len(set(vocabulary)), len(vocabulary), name)
        shared = {"policy-pending", "failed", "quarantined", "unresolved"}
        for (a, first), (b, second) in itertools.combinations(vocabularies.items(), 2):
            with self.subTest(a=a, b=b):
                expected = shared if {a, b} == {"PRIMARY_STATES", "STANDINGS"} else set()
                self.assertEqual(set(first) & set(second), expected)
        self.assertFalse(set(REPORTED_CONDITIONS) & set(COMPUTED_CONDITIONS))
        self.assertFalse(set(EVIDENCE_CONDITIONS) & set(PUBLICATION_CONDITIONS))


# ---- R4 ----

class R4PrimaryState(Checks):
    def test_value_with_only_contributors_is_measured(self) -> None:
        self.assertEqual(primary_state(cov(contributing=1), True), "measured")
        self.assertEqual(primary_state(cov(contributing=5), True), "measured")
        for evidence in ((entry(1),), (entry(1), entry(2), entry(3))):
            summary = self.built(evidence=evidence)
            self.assertEqual(summary.header.primary_state, "measured")
        # A zero is a value (ADR 0030); an empty object is still a body.
        for body in (body_of({"total": 0}), b"{}"):
            summary = self.built(evidence=(entry(1),), body=body)
            self.assertEqual(summary.header.primary_state, "measured")
            self.assertEqual(summary.header.body_sha256, sha(body))
            self.assertEqual(summary.body_bytes, body)

    def test_value_with_any_non_contributor_is_partial(self) -> None:
        for standing in NON_CONTRIBUTING:
            with self.subTest(standing):
                self.assertEqual(primary_state(cov(contributing=3, **{COVERAGE_FIELD[standing]: 1}), True), "partial")
                summary = self.built(evidence=(entry(1), entry(2), entry(3, standing)))
                self.assertEqual(summary.header.primary_state, "partial")
                self.assertEqual(summary.body_bytes, BODY)
        self.assertEqual(primary_state(Coverage(1, 1, 1, 1, 1, 1), True), "partial")
        self.assertEqual(self.built(evidence=mixed_evidence()).header.primary_state, "partial")

    def test_nothing_attributed_is_missing(self) -> None:
        self.assertEqual(primary_state(cov(), False), "missing")
        summary = self.built(evidence=(), body=None)
        self.assertEqual(summary.header.primary_state, "missing")
        self.assertIsNone(summary.body_bytes)
        self.assertIsNone(summary.header.body_sha256)
        self.assertIsNone(json.loads(summary.header_bytes)["body_sha256"])

    def test_no_value_precedence_is_total(self) -> None:
        reasons = ("failed", "quarantined", "policy-pending", "unresolved", "in-conflict")
        rank = {"failed": 0, "quarantined": 1, "policy-pending": 2, "unresolved": 3, "in-conflict": 3}
        record_id = 100
        for size in range(1, len(reasons) + 1):
            for subset in itertools.combinations(reasons, size):
                for contributors in (0, 1):
                    with self.subTest(subset=subset, contributors=contributors):
                        expected = min(subset, key=lambda s: rank[s])
                        expected = "unresolved" if expected == "in-conflict" else expected
                        coverage = cov(contributing=contributors, **{COVERAGE_FIELD[s]: 1 for s in subset})
                        self.assertEqual(primary_state(coverage, False), expected)
                        evidence = tuple(entry(record_id + i, s) for i, s in enumerate(subset))
                        evidence += tuple(entry(record_id + 10 + i) for i in range(contributors))
                        record_id += 20
                        summary = self.built(evidence=evidence, body=None)
                        self.assertEqual(summary.header.primary_state, expected)
                        self.assertIsNone(summary.body_bytes)
        # Larger counts of a lower-precedence reason never outrank a higher one.
        self.assertEqual(primary_state(cov(failed=1, quarantined=9, policy_pending=9, unresolved=9, in_conflict=9), False),
                         "failed")
        self.assertEqual(primary_state(cov(quarantined=1, policy_pending=9, unresolved=9), False), "quarantined")
        self.assertEqual(primary_state(cov(policy_pending=1, unresolved=9, in_conflict=9), False), "policy-pending")

    def test_in_conflict_without_value_is_unresolved(self) -> None:
        self.assertEqual(primary_state(cov(in_conflict=1), False), "unresolved")
        self.assertEqual(primary_state(cov(contributing=2, in_conflict=1), False), "unresolved")
        summary = self.built(evidence=(entry(1, "in-conflict"),), body=None)
        self.assertEqual(summary.header.primary_state, "unresolved")
        self.assertEqual(summary.header.evidence_conditions, ("source-conflict",))
        summary = self.built(evidence=(entry(1, "in-conflict"), entry(2)), body=None)
        self.assertEqual(summary.header.primary_state, "unresolved")
        summary = self.built(evidence=(entry(1, "in-conflict"), entry(2)))
        self.assertEqual(summary.header.primary_state, "partial")
        self.assertEqual(summary.header.evidence_conditions, ("source-conflict",))

    def test_value_without_contributor_is_inconsistent(self) -> None:
        self.assertEqual(self.refused(primary_state, cov(), True), INCONSISTENT)
        for standing in NON_CONTRIBUTING:
            with self.subTest(standing):
                self.assertEqual(self.refused(primary_state, cov(**{COVERAGE_FIELD[standing]: 2}), True), INCONSISTENT)
                self.assertEqual(self.build_refused(evidence=(entry(1, standing),)), INCONSISTENT)
        self.assertEqual(self.refused(primary_state, Coverage(0, 1, 1, 1, 1, 1), True), INCONSISTENT)
        self.assertEqual(self.build_refused(evidence=()), INCONSISTENT)
        self.assertEqual(self.build_refused(evidence=(entry(1, "failed"),), body=body_of({"total": 0})), INCONSISTENT)

    def test_contributors_without_value_or_reason_is_inconsistent(self) -> None:
        for count in (1, 3):
            self.assertEqual(self.refused(primary_state, cov(contributing=count), False), INCONSISTENT)
        self.assertEqual(self.build_refused(body=None), INCONSISTENT)
        # A condition is not a reason: only a non-contributing standing explains a missing value.
        self.assertEqual(self.build_refused(body=None, reported_conditions=("duplicate-grouped",)), INCONSISTENT)
        self.assertEqual(self.build_refused(body=None, evidence=(entry(1, policy_identity=FALLBACK_A),)), INCONSISTENT)

    def test_rule_is_exhaustive_over_small_counts(self) -> None:
        for counts in itertools.product(range(3), repeat=6):
            named = dict(zip(STANDING_NAMES, counts))
            for has_value in (False, True):
                expected = model_state(named, has_value)
                coverage = Coverage(*counts)
                if expected == INCONSISTENT:
                    self.assertEqual(self.refused(primary_state, coverage, has_value), INCONSISTENT, (counts, has_value))
                else:
                    self.assertEqual(primary_state(coverage, has_value), expected, (counts, has_value))
        for counts in itertools.product(range(2), repeat=6):
            named = dict(zip(STANDING_NAMES, counts))
            for has_value in (False, True):
                with self.subTest(counts=counts, has_value=has_value):
                    overrides = {"evidence": evidence_with(named), "body": BODY if has_value else None}
                    expected = model_state(named, has_value)
                    if expected == INCONSISTENT:
                        self.assertEqual(self.build_refused(**overrides), INCONSISTENT)
                    else:
                        self.assertEqual(self.built(**overrides).header.primary_state, expected)
        # The rule's domain: a Coverage of non-negative int counts and a bool.
        for coverage in (cov(contributing=-1), cov(failed=-1), cov(contributing=True), cov(unresolved=1.0),
                         cov(quarantined="1"), cov(in_conflict=None), (1, 0, 0, 0, 0, 0), {"contributing": 1}):
            with self.subTest(coverage=coverage):
                self.assertEqual(self.refused(primary_state, coverage, True), INVALID)
                self.assertEqual(self.refused(primary_state, coverage, False), INVALID)
        for has_value in (1, 0, None, "true"):
            self.assertEqual(self.refused(primary_state, cov(contributing=1), has_value), INVALID)


# ---- R5 ----

class R5EvidenceEntries(Checks):
    def test_each_standing_requires_its_fields(self) -> None:
        names = ("outcome_id", "processor_version", "policy_identity")
        present = {"outcome_id": 77, "processor_version": PROCESSOR, "policy_identity": "none"}
        for standing, carries in CARRIES.items():
            with self.subTest(standing):
                subject = entry(21, standing)
                companions = (entry(22),) if standing == "contributing" else ()
                body = BODY if standing == "contributing" else None
                summary = self.built(evidence=(subject,) + companions, body=body)
                written = json.loads(summary.evidence_bytes)["evidence"][0]
                self.assertEqual(set(written), ENTRY_KEYS)
                for name, carried in zip(names, carries):
                    self.assertEqual(written[name] is not None, carried, name)
                    wrong = dataclasses.replace(subject, **{name: None if carried else present[name]})
                    self.assertEqual(self.build_refused(evidence=(wrong,) + companions, body=body), INVALID, name)
        for standing in ("excluded", "Contributing", "contributing ", "", None, "measured", "source-conflict"):
            with self.subTest(standing=standing):
                self.assertEqual(self.build_refused(evidence=(entry(1), entry(2, standing))), INVALID)
                self.assertEqual(self.refused(coverage_of, (entry(1), entry(2, standing))), INVALID)
        not_entries = ((1, 501, 9001, "contributing", PROCESSOR, "none"), None, "entry")
        for item in not_entries:
            self.assertEqual(self.build_refused(evidence=(entry(1), item)), INVALID)

    def test_duplicate_record_is_refused(self) -> None:
        self.assertEqual(self.build_refused(evidence=(entry(31), entry(31))), INVALID)
        self.assertEqual(self.build_refused(evidence=(entry(31), entry(31, selection_id=999, outcome_id=998))), INVALID)
        self.assertEqual(self.build_refused(evidence=(entry(31), entry(32), entry(31, "failed"))), INVALID)
        self.assertEqual(self.build_refused(evidence=(entry(31, "in-conflict"), entry(31, "unresolved")), body=None),
                         INVALID)

    def test_mixed_processor_versions_are_refused(self) -> None:
        other = "interpret-steps@2"
        for standing in ("contributing", "unresolved", "policy-pending", "failed", "quarantined"):
            with self.subTest(standing):
                self.assertEqual(self.build_refused(evidence=(entry(1), entry(2, standing, processor_version=other))),
                                 INVALID)
        self.assertEqual(self.build_refused(evidence=(entry(1), entry(2, processor_version="interpret-steps"))), INVALID)
        self.assertEqual(self.build_refused(evidence=(entry(1), entry(2, processor_version=""))), INVALID)
        # One version throughout is fine, whichever it is.
        summary = self.built(processor_version=other,
                             evidence=(entry(1, processor_version=other), entry(2, "failed", processor_version=other)))
        self.assertEqual(summary.header.processor_version, other)

    def test_identities_must_be_positive_ints(self) -> None:
        bad = (0, -1, True, False, 1.0, "5", b"5")
        for name in ("record_id", "selection_id", "outcome_id"):
            for value in bad + (None,):
                if name == "outcome_id" and value is None:
                    continue
                with self.subTest(field=name, value=value):
                    wrong = dataclasses.replace(entry(2), **{name: value})
                    self.assertEqual(self.build_refused(evidence=(entry(1), wrong)), INVALID)
        for value in bad:
            with self.subTest(source_selection_policy=value):
                self.assertEqual(self.build_refused(source_selection_policy=value), INVALID)
            with self.subTest(sequence=value):
                self.assertEqual(self.build_refused(positions=(position(sequence=value),)), INVALID)
        # Any positive size stays exact.
        summary = self.built(
            evidence=(entry(2**64 + 7, selection_id=2**80, outcome_id=2**53 + 1), entry(1)),
            source_selection_policy=2**63, positions=(position(sequence=2**70),))
        written = json.loads(summary.evidence_bytes)["evidence"]
        self.assertEqual([item["record_id"] for item in written], ["1", "18446744073709551623"])
        self.assertEqual(summary.header.source_selection_policy, 2**63)

    def test_policy_identity_is_opaque(self) -> None:
        identities = ("none", FALLBACK_A, FALLBACK_B, "x", "fallback-timezone:not-json", "politique-é",
                      "z\U0001F600", "zａ")
        evidence = tuple(entry(i + 1, policy_identity=identity) for i, identity in enumerate(identities))
        summary = self.built(evidence=evidence + (entry(50, policy_identity=FALLBACK_A),))
        written = [item["policy_identity"] for item in json.loads(summary.evidence_bytes)["evidence"]]
        self.assertEqual(written, list(identities) + [FALLBACK_A])
        # Distinct, "none" left out, sorted by code point (U+FF41 before U+1F600, unlike UTF-16 order).
        self.assertEqual(summary.header.fallback_timezone_identities,
                         (FALLBACK_A, FALLBACK_B, "fallback-timezone:not-json", "politique-é", "x", "zａ",
                          "z\U0001F600"))
        for value in ("", 7, b"none", ("none",)):
            with self.subTest(value=value):
                self.assertEqual(self.build_refused(evidence=(entry(1), entry(2, policy_identity=value))), INVALID)


# ---- R6 ----

class R6Coverage(Checks):
    def test_coverage_counts_entries_by_standing(self) -> None:
        evidence = (
            entry(9, "quarantined"), entry(1), entry(5, "in-conflict"), entry(2), entry(7, "failed"),
            entry(4, "unresolved"), entry(8, "quarantined"), entry(3), entry(6, "in-conflict"), entry(10, "policy-pending"),
        )
        expected = Coverage(contributing=3, unresolved=1, in_conflict=2, policy_pending=1, failed=1, quarantined=2)
        self.assertEqual(coverage_of(evidence), expected)
        self.assertEqual(coverage_of(evidence), Coverage(3, 1, 2, 1, 1, 2))
        summary = self.built(evidence=evidence)
        self.assertEqual(summary.header.coverage, expected)
        self.assertEqual(json.loads(summary.header_bytes)["coverage"], {
            "contributing": 3, "unresolved": 1, "in-conflict": 2, "policy-pending": 1, "failed": 1, "quarantined": 2})
        for count in dataclasses.astuple(coverage_of(evidence)):
            self.assertIs(type(count), int)

    def test_coverage_of_nothing_is_all_zero(self) -> None:
        self.assertEqual(coverage_of(()), Coverage(0, 0, 0, 0, 0, 0))
        self.assertEqual(primary_state(coverage_of(()), False), "missing")
        summary = self.built(evidence=(), body=None)
        self.assertEqual(json.loads(summary.header_bytes)["coverage"], {s: 0 for s in STANDING_NAMES})


# ---- R7 ----

class R7EvidenceConditions(Checks):
    def test_unknown_or_computed_condition_is_refused(self) -> None:
        unresolved = (entry(1), entry(2, "unresolved"), entry(3))
        for name in COMPUTED_CONDITIONS + PUBLICATION_CONDITIONS + (
                "Selection-Unresolved", "selection_unresolved", "", "unplaced-evidence", "pending", "measured",
                "unresolved", None, 1):
            with self.subTest(name=name):
                self.assertEqual(self.build_refused(evidence=unresolved, reported_conditions=(name,)), INVALID)
                self.assertEqual(self.build_refused(evidence=unresolved,
                                                    reported_conditions=("selection-unresolved", name)), INVALID)
        # Even where the computed condition would hold.
        self.assertEqual(self.build_refused(evidence=(entry(1), entry(2, "in-conflict")),
                                            reported_conditions=("source-conflict",)), INVALID)
        self.assertEqual(self.build_refused(evidence=(entry(1, policy_identity=FALLBACK_A),),
                                            reported_conditions=("inferred-attribution",)), INVALID)
        # A bare string is not a collection of names.
        self.assertEqual(self.build_refused(evidence=unresolved, reported_conditions="selection-unresolved"), INVALID)

    def test_reported_condition_needs_evidence(self) -> None:
        for name in UNRESOLVED_CONDITIONS:
            with self.subTest(name):
                summary = self.built(evidence=(entry(1), entry(2, "unresolved")), reported_conditions=(name,))
                self.assertEqual(summary.header.evidence_conditions, (name,))
                self.assertEqual(self.build_refused(evidence=(entry(1), entry(2)), reported_conditions=(name,)),
                                 INCONSISTENT)
                for standing in ("in-conflict", "policy-pending", "failed", "quarantined"):
                    self.assertEqual(self.build_refused(evidence=(entry(1), entry(2, standing)),
                                                        reported_conditions=(name,)), INCONSISTENT, standing)
                self.assertEqual(self.build_refused(evidence=(), body=None, reported_conditions=(name,)), INCONSISTENT)
        summary = self.built(evidence=(entry(1), entry(2)), reported_conditions=("duplicate-grouped",))
        self.assertEqual(summary.header.evidence_conditions, ("duplicate-grouped",))
        self.assertEqual(self.built(evidence=(entry(1), entry(2), entry(3, "failed")),
                                    reported_conditions=("duplicate-grouped",)).header.primary_state, "partial")
        for evidence in ((entry(1),), (entry(1), entry(2, "unresolved")), (entry(1), entry(2, "in-conflict")),
                         (entry(1), entry(2, "quarantined"), entry(3, "policy-pending"))):
            self.assertEqual(self.build_refused(evidence=evidence, reported_conditions=("duplicate-grouped",)),
                             INCONSISTENT)

    def test_source_conflict_is_computed(self) -> None:
        self.assertNotIn("source-conflict", self.built(evidence=(entry(1), entry(2, "unresolved"))).header.evidence_conditions)
        for evidence, body in (((entry(1), entry(2, "in-conflict")), BODY),
                               ((entry(2, "in-conflict"),), None),
                               ((entry(2, "in-conflict"), entry(3, "in-conflict"), entry(4, "failed")), None)):
            summary = self.built(evidence=evidence, body=body)
            self.assertIn("source-conflict", summary.header.evidence_conditions)
            self.assertEqual(json.loads(summary.header_bytes)["evidence_conditions"].count("source-conflict"), 1)

    def test_inferred_attribution_is_computed(self) -> None:
        self.assertEqual(self.built(evidence=(entry(1), entry(2))).header.evidence_conditions, ())
        summary = self.built(evidence=(entry(1), entry(2, policy_identity=FALLBACK_A)))
        self.assertEqual(summary.header.evidence_conditions, ("inferred-attribution",))
        self.assertEqual(summary.header.fallback_timezone_identities, (FALLBACK_A,))
        # A non-contributing record's identity is listed, but does not make the value inferred.
        for standing in ("unresolved", "policy-pending", "quarantined"):
            with self.subTest(standing):
                summary = self.built(evidence=(entry(1), entry(2, standing, policy_identity=FALLBACK_B)))
                self.assertEqual(summary.header.evidence_conditions, ())
                self.assertEqual(summary.header.fallback_timezone_identities, (FALLBACK_B,))

    def test_conditions_are_sorted_and_unique(self) -> None:
        reported = ["unallocatable-interval", "selection-unresolved", "equal-time-conflict", "classification-conflict",
                    "duplicate-grouped", "selection-unresolved", "unallocatable-interval"]
        evidence = (entry(1, policy_identity=FALLBACK_A), entry(2), entry(3, "unresolved"), entry(4, "in-conflict"))
        summary = self.built(evidence=evidence, reported_conditions=reported)
        self.assertEqual(summary.header.evidence_conditions, EVIDENCE_CONDITIONS)
        self.assertEqual(json.loads(summary.header_bytes)["evidence_conditions"], [
            "classification-conflict", "duplicate-grouped", "equal-time-conflict", "inferred-attribution",
            "selection-unresolved", "source-conflict", "unallocatable-interval"])
        # Conditions never change the primary state.
        plain = self.built(evidence=evidence[1:3] + (entry(5),))
        conditioned = self.built(evidence=evidence[1:3] + (entry(5),),
                                 reported_conditions=frozenset({"duplicate-grouped", "selection-unresolved"}))
        self.assertEqual(plain.header.primary_state, conditioned.header.primary_state)
        self.assertEqual(plain.header.coverage, conditioned.header.coverage)


# ---- R8 ----

class R8Header(Checks):
    def test_header_keys_are_exact(self) -> None:
        summaries = [self.built(evidence=(), body=None), self.built(),
                     self.built(evidence=mixed_evidence(), positions=two_positions(), source_selection_policy=3,
                                reported_conditions=("selection-unresolved",))]
        for summary in summaries:
            header = json.loads(summary.header_bytes)
            self.assertEqual(set(header), HEADER_KEYS)
            self.assertEqual({field.name for field in dataclasses.fields(summary.header)}, HEADER_KEYS)
            self.assertEqual(set(header["coverage"]), set(STANDING_NAMES))
            self.assertEqual(set(header["watermark"]), {"evidence_set_sha256", "positions"})
            for item in header["watermark"]["positions"]:
                self.assertEqual(set(item), POSITION_KEYS)
            self.assertIsInstance(header["fallback_timezone_identities"], list)
            self.assertIsInstance(header["evidence_conditions"], list)
            numbers = [value for value in json_scalars(header) if isinstance(value, (int, float)) and not isinstance(value, bool)]
            self.assertTrue(all(type(value) is int for value in numbers))
            self.assertEqual(sorted(numbers), sorted(header["coverage"].values()))
        missing = json.loads(summaries[0].header_bytes)
        self.assertIn("source_selection_policy", missing)
        self.assertIsNone(missing["source_selection_policy"])
        self.assertEqual(missing["fallback_timezone_identities"], [])
        self.assertEqual(missing["evidence_conditions"], [])
        self.assertEqual(summaries[0].header.fallback_timezone_identities, ())
        self.assertEqual(summaries[0].header.evidence_conditions, ())

    def test_header_bytes_are_rfc8785(self) -> None:
        summary = self.built(
            local_date=date(2026, 3, 9), source_selection_policy=12, reported_conditions=("selection-unresolved",),
            evidence=(entry(100, "unresolved", policy_identity=FALLBACK_A), entry(10),
                      entry(9, policy_identity=FALLBACK_A)),
            positions=two_positions(), body=b'{"total":4321}')
        fallback = '"fallback-timezone:[[\\"policy-a\\"],\\"2025b\\"]"'
        manifest = (
            '{"contract_version":"summary-contract@1","evidence":['
            '{"outcome_id":"9009","policy_identity":' + fallback + ',"processor_version":"interpret-steps@1",'
            '"record_id":"9","selection_id":"509","standing":"contributing"},'
            '{"outcome_id":"9010","policy_identity":"none","processor_version":"interpret-steps@1",'
            '"record_id":"10","selection_id":"510","standing":"contributing"},'
            '{"outcome_id":"9100","policy_identity":' + fallback + ',"processor_version":"interpret-steps@1",'
            '"record_id":"100","selection_id":"600","standing":"unresolved"}]}').encode("utf-8")
        header = (
            '{"body_sha256":"' + sha(b'{"total":4321}') + '","contract_version":"summary-contract@1",'
            '"coverage":{"contributing":2,"failed":0,"in-conflict":0,"policy-pending":0,"quarantined":0,"unresolved":1},'
            '"derivation_version":"derive-synthetic-steps@1",'
            '"evidence_conditions":["inferred-attribution","selection-unresolved"],'
            '"fallback_timezone_identities":[' + fallback + '],"local_date":"2026-03-09","measure":"daily-steps",'
            '"primary_state":"partial","processor_version":"interpret-steps@1","record_type":"steps",'
            '"source_selection_policy":"12","watermark":{"evidence_set_sha256":"' + sha(manifest) + '","positions":['
            '{"installation_id":"installation-a","processed_at":"2026-01-15T22:00:03.123457Z",'
            '"receipt_time":"2026-01-15T22:00:00.123456Z","sequence":"12"},'
            '{"installation_id":"installation-b","processed_at":"2026-01-14T22:00:01.500000Z",'
            '"receipt_time":"2026-01-14T22:00:00.000000Z","sequence":"7"}]}}').encode("utf-8")
        self.assertEqual(enc(json.loads(header)), header)
        self.assertEqual(summary.evidence_bytes, manifest)
        self.assertEqual(summary.header_bytes, header)
        self.assertEqual(summary.summary_sha256, sha(header))
        self.assertTrue(is_canonical(summary.header_bytes))
        self.assertTrue(is_canonical(summary.evidence_bytes))

    def test_identities_are_decimal_text(self) -> None:
        big = 2**64 + 1
        summary = self.built(evidence=(entry(big, selection_id=big + 1, outcome_id=big + 2), entry(3, "failed")),
                             source_selection_policy=big + 3, positions=(position(sequence=big + 4),))
        header = json.loads(summary.header_bytes)
        manifest = json.loads(summary.evidence_bytes)
        self.assertEqual(header["source_selection_policy"], str(big + 3))
        self.assertEqual(header["watermark"]["positions"][0]["sequence"], str(big + 4))
        self.assertEqual(manifest["evidence"][0], {
            "record_id": "3", "selection_id": "503", "outcome_id": None, "standing": "failed",
            "processor_version": PROCESSOR, "policy_identity": None})
        self.assertEqual(manifest["evidence"][1]["record_id"], str(big))
        self.assertEqual(manifest["evidence"][1]["selection_id"], str(big + 1))
        self.assertEqual(manifest["evidence"][1]["outcome_id"], str(big + 2))
        self.assertIn(b'"sequence":"18446744073709551621"', summary.header_bytes)
        self.assertIn(b'"record_id":"18446744073709551617"', summary.evidence_bytes)

    def test_version_forms_are_checked(self) -> None:
        good = ("a@1", "derive-daily-steps@1", "interpret-steps@12", "x2-y3@907")
        bad = ("", "derive-daily-steps", "derive-daily-steps@", "derive-daily-steps@0", "derive-daily-steps@01",
               "Derive-daily-steps@1", "derive_daily_steps@1", "derive--daily@1", "-derive@1", "derive-@1", "@1",
               "derive@1@2", "derive@1 ", " derive@1", "derive@1\n", "derive@+1", "derive@-1", "derive@1.0",
               "derive@１", "dérive@1", 1, None, b"derive@1")
        for value in good:
            with self.subTest(good=value):
                self.assertEqual(self.built(derivation_version=value).header.derivation_version, value)
                evidence = (entry(1, processor_version=value), entry(2, "failed", processor_version=value))
                self.assertEqual(self.built(processor_version=value, evidence=evidence).header.processor_version, value)
        for value in bad:
            with self.subTest(bad=value):
                self.assertEqual(self.build_refused(derivation_version=value), INVALID)
                evidence = (entry(1, processor_version=value), entry(2, "failed", processor_version=value))
                self.assertEqual(self.build_refused(processor_version=value, evidence=evidence), INVALID)
                self.assertEqual(self.build_refused(processor_version=value, evidence=(entry(1, "in-conflict"),),
                                                    body=None), INVALID)

    def test_measure_pair_must_be_listed(self) -> None:
        for measure, record_types in MEASURES.items():
            for record_type in record_types:
                with self.subTest(measure=measure, record_type=record_type):
                    header = self.built(measure=measure, record_type=record_type).header
                    self.assertEqual((header.measure, header.record_type), (measure, record_type))
        for (measure, _), (_, others) in itertools.permutations(MEASURES.items(), 2):
            for record_type in others:
                if record_type not in MEASURES[measure]:
                    self.assertEqual(self.build_refused(measure=measure, record_type=record_type), INVALID)
        for measure, record_type in (("daily-step", "steps"), ("Daily-Steps", "steps"), ("daily-steps", "Steps"),
                                     ("daily-steps", "step"), (None, "steps"), ("daily-steps", None), ("steps", "daily-steps")):
            self.assertEqual(self.build_refused(measure=measure, record_type=record_type), INVALID)
        for local_date in (datetime(2026, 1, 15, tzinfo=UTC), datetime(2026, 1, 15), "2026-01-15", None, 20260115):
            self.assertEqual(self.build_refused(local_date=local_date), INVALID)

    def test_summary_digest_covers_header(self) -> None:
        base = self.built()
        self.assertEqual(base.summary_sha256, sha(base.header_bytes))
        self.assertNotEqual(base.summary_sha256, sha(base.body_bytes))
        self.assertNotEqual(base.summary_sha256, sha(base.evidence_bytes))
        variants = {
            "body": self.built(body=body_of({"total": 1})),
            "evidence": self.built(evidence=(entry(11), entry(12, selection_id=777))),
            "derivation": self.built(derivation_version="derive-synthetic-steps@2"),
            "date": self.built(local_date=date(2026, 1, 16)),
            "policy": self.built(source_selection_policy=4),
        }
        for name, other in variants.items():
            with self.subTest(name):
                self.assertNotEqual(other.summary_sha256, base.summary_sha256)
                self.assertEqual(other.summary_sha256, sha(other.header_bytes))
        self.assertEqual(variants["evidence"].body_bytes, base.body_bytes)
        self.assertNotEqual(variants["evidence"].evidence_bytes, base.evidence_bytes)
        self.assertEqual(variants["body"].evidence_bytes, base.evidence_bytes)
        self.assertNotEqual(variants["body"].header.body_sha256, base.header.body_sha256)


# ---- R9 ----

class R9DataWatermark(Checks):
    def test_watermark_positions_are_sorted_and_unique(self) -> None:
        positions = (position("installation-c", 3), position("installation-a", 9), position("Installation-z", 1),
                     position("installation-b", 2**40))
        summary = self.built(positions=positions)
        written = json.loads(summary.header_bytes)["watermark"]["positions"]
        self.assertEqual([item["installation_id"] for item in written],
                         ["Installation-z", "installation-a", "installation-b", "installation-c"])
        self.assertEqual([item["sequence"] for item in written], ["1", "9", "1099511627776", "3"])
        self.assertEqual([p.installation_id for p in summary.header.watermark.positions],
                         ["Installation-z", "installation-a", "installation-b", "installation-c"])
        self.assertIs(type(summary.header.watermark.positions), tuple)
        for duplicate in ((position("installation-a", 3), position("installation-a", 3)),
                          (position("installation-a", 3), position("installation-b", 1), position("installation-a", 4))):
            self.assertEqual(self.build_refused(positions=duplicate), INVALID)
        for installation in ("", None, 7, b"installation-a"):
            self.assertEqual(self.build_refused(positions=(position(installation),)), INVALID)
        self.assertEqual(self.build_refused(positions=(position(), ("installation-b", 1, RECEIPT, RECEIPT))), INVALID)

    def test_watermark_needs_a_position(self) -> None:
        self.assertEqual(self.build_refused(positions=()), INVALID)
        self.assertEqual(self.build_refused(positions=(), evidence=(), body=None), INVALID)
        summary = self.built(evidence=(), body=None, positions=(position(),))
        self.assertEqual(len(summary.header.watermark.positions), 1)

    def test_watermark_times_are_utc_and_ordered(self) -> None:
        receipt = datetime(2026, 1, 15, 3, 30, 0, 1, tzinfo=IST)
        cases = (
            (position("installation-a", 1, receipt, receipt), "2026-01-14T22:00:00.000001Z", "2026-01-14T22:00:00.000001Z"),
            (position("installation-a", 1, receipt, datetime(2026, 1, 14, 22, 0, 0, 1, tzinfo=UTC)),
             "2026-01-14T22:00:00.000001Z", "2026-01-14T22:00:00.000001Z"),
            (position("installation-a", 1, datetime(2026, 1, 15, tzinfo=UTC), datetime(2026, 1, 15, 1, tzinfo=PDT)),
             "2026-01-15T00:00:00.000000Z", "2026-01-15T08:00:00.000000Z"),
            (position("installation-a", 1, datetime(999, 5, 6, 7, 8, 9, tzinfo=UTC), datetime(999, 5, 6, 7, 8, 9, tzinfo=UTC)),
             "0999-05-06T07:08:09.000000Z", "0999-05-06T07:08:09.000000Z"),
        )
        for delivery, receipt_text, processed_text in cases:
            with self.subTest(delivery=delivery):
                written = json.loads(self.built(positions=(delivery,)).header_bytes)["watermark"]["positions"][0]
                self.assertEqual((written["receipt_time"], written["processed_at"]), (receipt_text, processed_text))
        # processed_at before receipt_time, compared as instants.
        for processed in (datetime(2026, 1, 14, 22, 0, 0, 0, tzinfo=UTC), datetime(2026, 1, 15, 3, 30, 0, 0, tzinfo=IST),
                          datetime(2026, 1, 14, 14, 59, 59, tzinfo=PDT)):
            self.assertEqual(self.build_refused(positions=(position("installation-a", 1, receipt, processed),)), INVALID)
        naive = datetime(2026, 1, 15, 22, 0)
        for receipt_time, processed_at in ((naive, naive), (naive, RECEIPT), (RECEIPT - timedelta(hours=1), naive),
                                           (DAY, RECEIPT), (RECEIPT.isoformat(), RECEIPT), (RECEIPT, None)):
            self.assertEqual(self.build_refused(positions=(DeliveryPosition("installation-a", 1, receipt_time, processed_at),)),
                             INVALID)

    def test_evidence_digest_is_computed_not_supplied(self) -> None:
        parameters = inspect.signature(build_summary).parameters
        self.assertEqual(list(parameters), ["measure", "record_type", "local_date", "derivation_version",
                                            "processor_version", "source_selection_policy", "evidence",
                                            "reported_conditions", "positions", "body"])
        self.assertTrue(all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters.values()))
        with self.assertRaises(TypeError):
            build_summary(**facts(), evidence_set_sha256="0" * 64)
        summary = self.built(evidence=mixed_evidence())
        self.assertEqual(summary.header.watermark.evidence_set_sha256, sha(summary.evidence_bytes))
        other = self.built(evidence=mixed_evidence(), body=body_of({"total": 1}), positions=two_positions())
        self.assertEqual(other.header.watermark.evidence_set_sha256, summary.header.watermark.evidence_set_sha256)

    def test_watermark_changes_the_summary_digest(self) -> None:
        base = self.built(positions=two_positions())
        later = two_positions()
        variants = {
            "sequence": (later[0], dataclasses.replace(later[1], sequence=13)),
            "receipt": (dataclasses.replace(later[0], receipt_time=later[0].receipt_time + timedelta(microseconds=1)),
                        later[1]),
            "processed": (later[0], dataclasses.replace(later[1], processed_at=later[1].processed_at + timedelta(microseconds=1))),
            "installation": (dataclasses.replace(later[0], installation_id="installation-c"), later[1]),
            "added": later + (position("installation-c", 1),),
            "dropped": later[1:],
        }
        for name, positions in variants.items():
            with self.subTest(name):
                other = self.built(positions=positions)
                self.assertNotEqual(other.header_bytes, base.header_bytes)
                self.assertNotEqual(other.summary_sha256, base.summary_sha256)
                self.assertEqual((other.evidence_bytes, other.body_bytes), (base.evidence_bytes, base.body_bytes))
                self.assertEqual(other.header.primary_state, base.header.primary_state)


# ---- R10 ----

class R10EvidenceManifest(Checks):
    def test_manifest_is_canonical_and_sorted_numerically(self) -> None:
        evidence = (entry(10), entry(9, "unresolved"), entry(100, "failed"), entry(2), entry(1000, "in-conflict"))
        summary = self.built(evidence=evidence)
        manifest = json.loads(summary.evidence_bytes)
        self.assertEqual(set(manifest), {"contract_version", "evidence"})
        self.assertEqual(manifest["contract_version"], V1)
        self.assertEqual([item["record_id"] for item in manifest["evidence"]], ["2", "9", "10", "100", "1000"])
        self.assertTrue(is_canonical(summary.evidence_bytes))
        for item in manifest["evidence"]:
            self.assertEqual(set(item), ENTRY_KEYS)
        self.assertEqual(len(manifest["evidence"]), len(evidence))

    def test_manifest_digest_is_in_the_header(self) -> None:
        summary = self.built(evidence=mixed_evidence())
        header = json.loads(summary.header_bytes)
        self.assertEqual(header["watermark"]["evidence_set_sha256"], sha(summary.evidence_bytes))
        manifest = json.loads(summary.evidence_bytes)
        self.assertEqual(header["coverage"], {s: sum(1 for item in manifest["evidence"] if item["standing"] == s)
                                              for s in STANDING_NAMES})
        self.assertEqual(sum(1 for value in json_scalars(header) if value == sha(summary.evidence_bytes)), 1)

    def test_empty_manifest_is_missing(self) -> None:
        summary = self.built(evidence=(), body=None)
        self.assertEqual(summary.evidence_bytes, b'{"contract_version":"summary-contract@1","evidence":[]}')
        self.assertEqual(summary.header.primary_state, "missing")
        self.assertEqual(summary.header.coverage, Coverage(0, 0, 0, 0, 0, 0))
        self.assertEqual(summary.header.watermark.evidence_set_sha256,
                         sha(b'{"contract_version":"summary-contract@1","evidence":[]}'))


# ---- R11 ----

class R11Body(Checks):
    def test_body_must_be_a_canonical_object(self) -> None:
        good = (b"{}", b'{"a":1.5,"b":[1,-2,{"c":null}],"d":true,"e":false}', '{"n":"é"}'.encode("utf-8"),
                b'{"big":1e+21,"small":1e-7}', '{"\U0001F600":2,"ａ":1}'.encode("utf-8"),
                body_of({"marker": HEALTH_MARKER}))
        for body in good:
            with self.subTest(good=body):
                self.assertEqual(self.built(body=body).body_bytes, body)
        bad = (b"[]", b"1", b'"x"', b"null", b"", b'{"b":1,"a":2}', b'{"a": 1}', b'{"a":1.0}', b'{"a":1E2}',
               b'{"a":1e2}', b'{"a":-0}', b'{"a":0.1e1}', b'{"a":1,"a":1}', b'{"a":NaN}', b'{"a":Infinity}',
               b"{}\n", b" {}", b"\xef\xbb\xbf{}", b'{"n":"\\u00e9"}', b'{"n":"\xff"}', b'{"s":"\\ud800"}',
               '{"ａ":1,"\U0001F600":2}'.encode("utf-8"), b'{"a":"\\/"}', "{}", 0)
        for body in bad:
            with self.subTest(bad=body):
                self.assertEqual(self.build_refused(body=body), INVALID)

    def test_body_integers_stay_in_the_safe_domain(self) -> None:
        for value in (9007199254740991, -9007199254740991, 0):
            body = body_of({"n": value, "list": [value], "nested": {"m": value}})
            self.assertEqual(self.built(body=body).body_bytes, body)
        for value in (9007199254740992, -9007199254740992, 10**16):
            for shape in ({"n": value}, {"list": [1, value]}, {"nested": {"deeper": [{"m": value}]}}):
                with self.subTest(value=value, shape=shape):
                    body = json.dumps(shape, separators=(",", ":"), sort_keys=True).encode()
                    self.assertEqual(self.build_refused(body=body), INVALID)

    def test_body_present_only_with_a_value(self) -> None:
        for standing in NON_CONTRIBUTING:
            with self.subTest(standing):
                self.assertEqual(self.build_refused(evidence=(entry(1, standing),), body=b"{}"), INCONSISTENT)
                summary = self.built(evidence=(entry(1, standing),), body=None)
                self.assertIsNone(summary.body_bytes)
                self.assertIn(summary.header.primary_state, ("failed", "quarantined", "policy-pending", "unresolved"))
        self.assertEqual(self.build_refused(evidence=(entry(1),), body=None), INCONSISTENT)
        summary = self.built(evidence=(entry(1), entry(2, "failed")), body=b"{}")
        self.assertEqual((summary.header.primary_state, summary.body_bytes), ("partial", b"{}"))


# ---- R12 ----

class R12Decoding(Checks):
    def bases(self) -> dict[str, HealthSummary]:
        return {
            "partial": self.built(evidence=mixed_evidence(), positions=two_positions(), source_selection_policy=7,
                                  reported_conditions=("selection-unresolved",)),
            "measured": self.built(evidence=(entry(2), entry(10, policy_identity=FALLBACK_A))),
            "failed": self.built(evidence=(entry(3, "failed"), entry(4, "unresolved")), body=None),
            "missing": self.built(evidence=(), body=None),
            "no-value": self.built(evidence=(entry(1, "failed"), entry(2, "quarantined"), entry(3, "policy-pending"),
                                             entry(4, "unresolved"), entry(5, "in-conflict"), entry(6)), body=None),
        }

    def test_decode_round_trips(self) -> None:
        facts_list = scenario_facts() + [
            facts(evidence=(entry(1, "quarantined"),), body=None),
            facts(evidence=(entry(1, "policy-pending"), entry(2)), body=None),
            facts(evidence=(entry(1, "in-conflict"),), body=None),
        ]
        states = set()
        for f in facts_list:
            summary = build_summary(**f)
            decoded = decode_summary(bytes(summary.header_bytes), bytes(summary.evidence_bytes),
                                     None if summary.body_bytes is None else bytes(summary.body_bytes))
            self.assertEqual(decoded, summary)
            self.assertEqual(decoded.header, summary.header)
            self.assertEqual((decoded.header_bytes, decoded.evidence_bytes, decoded.body_bytes, decoded.summary_sha256),
                             (summary.header_bytes, summary.evidence_bytes, summary.body_bytes, summary.summary_sha256))
            for moment in itertools.chain.from_iterable((p.receipt_time, p.processed_at)
                                                        for p in decoded.header.watermark.positions):
                self.assertEqual(moment.utcoffset(), timedelta(0))
            states.add(summary.header.primary_state)
        self.assertEqual(states, set(PRIMARY_STATES))
        for name, summary in self.bases().items():
            with self.subTest(name):
                self.assertEqual(decode_summary(*stamp(*split(summary))), summary)

    def test_tampered_bytes_are_refused(self) -> None:
        bases = self.bases()
        partial, measured, failed, missing = bases["partial"], bases["measured"], bases["failed"], bases["missing"]

        def changed(base, mutate=None, *, body=_DEFAULT, restamp=True):
            header, manifest, body_bytes = split(base)
            if mutate is not None:
                mutate(header, manifest)
            if body is not _DEFAULT:
                body_bytes = body
            return stamp(header, manifest, body_bytes) if restamp else (enc(header), enc(manifest), body_bytes)

        def setter(*path, value):
            def mutate(header, manifest):
                target = {"header": header, "manifest": manifest}[path[0]]
                for key in path[1:-1]:
                    target = target[key]
                target[path[-1]] = value
            return mutate

        def deleter(*path):
            def mutate(header, manifest):
                target = {"header": header, "manifest": manifest}[path[0]]
                for key in path[1:-1]:
                    target = target[key]
                del target[path[-1]]
            return mutate

        hb, eb, bb = partial.header_bytes, partial.evidence_bytes, partial.body_bytes
        cases = {
            # Not canonical, or not strict JSON.
            "trailing newline": (hb + b"\n", eb, bb),
            "leading space": (b" " + hb, eb, bb),
            "manifest trailing newline": (hb, eb + b"\n", bb),
            "pretty header": (json.dumps(json.loads(hb), sort_keys=True, indent=1).encode(), eb, bb),
            "unsorted keys": (json.dumps(dict(reversed(json.loads(hb).items())), separators=(",", ":")).encode(), eb, bb),
            "duplicate key": (hb[:-1] + b',"primary_state":"partial"}', eb, bb),
            "duplicate key first": (b'{"body_sha256":null,' + hb[1:], eb, bb),
            "escaped ascii": (hb.replace(b'"partial"', b'"\\u0070artial"'), eb, bb),
            "not utf-8": (hb.replace(b"partial", b"partia\xff"), eb, bb),
            "bom": (b"\xef\xbb\xbf" + hb, eb, bb),
            "header array": (b"[" + hb + b"]", eb, bb),
            "header empty": (b"", eb, bb),
            "nan count": (hb.replace(b'"failed":1', b'"failed":NaN'), eb, bb),
            "body not canonical": (hb, eb, b'{"total": 1}'),
            # Canonical bytes whose content breaks a rule.
            "unknown header key": changed(partial, setter("header", "summary_note", value="x")),
            "publication key": changed(partial, setter("header", "publication_conditions", value=[])),
            "missing body digest key": changed(missing, deleter("header", "body_sha256")),
            "missing policy key": changed(partial, deleter("header", "source_selection_policy")),
            "missing coverage key": changed(partial, deleter("header", "coverage", "in-conflict")),
            "extra coverage key": changed(partial, setter("header", "coverage", "excluded", value=0)),
            "missing watermark key": changed(partial, deleter("header", "watermark", "positions")),
            "unknown manifest key": changed(partial, setter("manifest", "note", value="x")),
            "float count": changed(partial, setter("header", "coverage", "failed", value=1.5)),
            "true count": changed(partial, setter("header", "coverage", "failed", value=True)),
            "string count": changed(partial, setter("header", "coverage", "failed", value="1")),
            "negative count": changed(partial, setter("header", "coverage", "failed", value=-1)),
            "unknown primary state": changed(failed, setter("header", "primary_state", value="pending")),
            "unknown condition": changed(partial, setter("header", "evidence_conditions",
                                                         value=["inferred-attribution", "selection-unresolved",
                                                                "source-conflict", "unplaced-evidence"])),
            "publication condition": changed(partial, setter("header", "evidence_conditions",
                                                             value=["conflicted", "inferred-attribution",
                                                                    "selection-unresolved", "source-conflict"])),
            "computed condition missing": changed(partial, setter("header", "evidence_conditions",
                                                                  value=["inferred-attribution", "selection-unresolved"])),
            "inferred attribution missing": changed(measured, setter("header", "evidence_conditions", value=[])),
            "computed condition extra": changed(failed, setter("header", "evidence_conditions", value=["source-conflict"])),
            "condition without evidence": changed(measured, setter("header", "evidence_conditions",
                                                                   value=["inferred-attribution", "selection-unresolved"])),
            "conditions unsorted": changed(partial, setter("header", "evidence_conditions",
                                                           value=["selection-unresolved", "inferred-attribution",
                                                                  "source-conflict"])),
            "conditions duplicated": changed(partial, setter("header", "evidence_conditions",
                                                             value=["inferred-attribution", "selection-unresolved",
                                                                    "selection-unresolved", "source-conflict"])),
            "fallback identity missing": changed(partial, setter("header", "fallback_timezone_identities", value=[FALLBACK_A])),
            "fallback identity extra": changed(measured, setter("header", "fallback_timezone_identities",
                                                                value=[FALLBACK_A, FALLBACK_B])),
            "fallback none listed": changed(measured, setter("header", "fallback_timezone_identities",
                                                             value=[FALLBACK_A, "none"])),
            "fallback unsorted": changed(partial, setter("header", "fallback_timezone_identities",
                                                         value=[FALLBACK_B, FALLBACK_A])),
            "fallback duplicated": changed(measured, setter("header", "fallback_timezone_identities",
                                                            value=[FALLBACK_A, FALLBACK_A])),
            "identity as number": changed(partial, setter("header", "source_selection_policy", value=7)),
            "identity zero": changed(partial, setter("header", "source_selection_policy", value="0")),
            "evidence digest wrong": changed(partial, setter("header", "watermark", "evidence_set_sha256",
                                                             value=sha(b"other")), restamp=False),
            "evidence digest upper": (hb.replace(sha(eb).encode(), sha(eb).upper().encode()), eb, bb),
            "manifest tampered": changed(partial, setter("manifest", "evidence", 0, "selection_id", value="999"),
                                         restamp=False),
            "body tampered": (hb, eb, body_of({"marker": HEALTH_MARKER, "total": MARKER_VALUE + 1})),
            "body dropped": (hb, eb, None),
            "body added": (failed.header_bytes, failed.evidence_bytes, b"{}"),
            "body digest kept null": changed(failed, body=b"{}", restamp=False),
            "body not canonical restamped": changed(partial, body=b'{"total": 1}'),
            "body array restamped": changed(partial, body=b"[]"),
            "body unsafe integer restamped": changed(partial, body=b'{"total":9007199254740992}'),
            "manifest unsorted": changed(measured, lambda h, m: m["evidence"].reverse()),
            "manifest duplicate": changed(measured, lambda h, m: m["evidence"].append(dict(m["evidence"][1]))),
            "manifest unknown standing": changed(failed, setter("manifest", "evidence", 1, "standing", value="excluded")),
            "manifest entry key missing": changed(failed, deleter("manifest", "evidence", 0, "policy_identity")),
            "manifest entry key extra": changed(failed, setter("manifest", "evidence", 0, "origin", value="x")),
            "manifest null outcome": changed(measured, setter("manifest", "evidence", 0, "outcome_id", value=None)),
            "manifest failed outcome": changed(failed, setter("manifest", "evidence", 0, "outcome_id", value="77")),
            "manifest failed policy": changed(failed, setter("manifest", "evidence", 0, "policy_identity", value="none")),
            "manifest empty policy": changed(measured, setter("manifest", "evidence", 0, "policy_identity", value="")),
            "manifest processor mismatch": changed(failed, setter("manifest", "evidence", 0, "processor_version",
                                                                  value="interpret-steps@2")),
            "header processor mismatch": changed(failed, setter("header", "processor_version", value="interpret-steps@2")),
            "positions empty": changed(missing, setter("header", "watermark", "positions", value=[])),
            "positions unsorted": changed(partial, lambda h, m: h["watermark"]["positions"].reverse()),
            "positions duplicated": changed(partial, lambda h, m: h["watermark"]["positions"].__setitem__(
                1, dict(h["watermark"]["positions"][0], sequence="99"))),
            "installation empty": changed(missing, setter("header", "watermark", "positions", 0, "installation_id", value="")),
            "installation number": changed(missing, setter("header", "watermark", "positions", 0, "installation_id", value=7)),
            "processed before receipt": changed(missing, setter("header", "watermark", "positions", 0, "processed_at",
                                                                value="2026-01-15T22:00:00.123455Z")),
            "local_date short": changed(missing, setter("header", "local_date", value="2026-1-15")),
            "local_date impossible": changed(missing, setter("header", "local_date", value="2026-02-30")),
            "local_date compact": changed(missing, setter("header", "local_date", value="20260115")),
            "local_date with time": changed(missing, setter("header", "local_date", value="2026-01-15T00:00:00")),
            "local_date number": changed(missing, setter("header", "local_date", value=20260115)),
            "measure pair": changed(missing, setter("header", "record_type", value="heart-rate")),
            "derivation form": changed(missing, setter("header", "derivation_version", value="derive-synthetic-steps@01")),
            "processor form": changed(missing, setter("header", "processor_version", value="Interpret-steps@1")),
            "manifest version missing": changed(missing, deleter("manifest", "contract_version")),
        }
        for text in ("2026-01-15T22:00:00.123456+00:00", "2026-01-15T22:00:00Z", "2026-01-15T22:00:00.123Z",
                     "2026-01-15 22:00:00.123456Z", "2026-01-15T22:00:00.123456z", "2026-01-15T22:00:00.123456789Z",
                     "2026-02-30T22:00:00.000000Z", "2026-01-15T24:00:00.000000Z", "2026-01-15T22:00:00.1234567Z", ""):
            cases[f"receipt time {text!r}"] = changed(missing, setter("header", "watermark", "positions", 0,
                                                                      "receipt_time", value=text))
        for text in ("07", "0", "-7", "+7", "7.0", "", " 7", "7 ", "７", "٧", "1_000", "0x7", "7e0"):
            cases[f"policy {text!r}"] = changed(partial, setter("header", "source_selection_policy", value=text))
            cases[f"record {text!r}"] = changed(failed, setter("manifest", "evidence", 1, "record_id", value=text))
            cases[f"outcome {text!r}"] = changed(failed, setter("manifest", "evidence", 1, "outcome_id", value=text))
            cases[f"sequence {text!r}"] = changed(missing, setter("header", "watermark", "positions", 0, "sequence",
                                                                  value=text))
        cases["record as number"] = changed(failed, setter("manifest", "evidence", 0, "record_id", value=3))
        cases["sequence as number"] = changed(missing, setter("header", "watermark", "positions", 0, "sequence", value=41))
        for name, (header_bytes, evidence_bytes, body_bytes) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.decode_refused(header_bytes, evidence_bytes, body_bytes), BYTES_INVALID)
        for value in ("{}", None, 1):
            with self.subTest(header=value):
                self.assertIn(self.decode_refused(value, eb, bb), (BYTES_INVALID, INVALID))
        self.assertIn(self.decode_refused(hb, eb, "{}"), (BYTES_INVALID, INVALID))

    def test_inconsistent_state_bytes_are_refused(self) -> None:
        bases = self.bases()

        def with_header(base, body=_DEFAULT, **changes):
            header, manifest, body_bytes = split(base)
            for key, value in changes.items():
                if key == "coverage":
                    header["coverage"].update(value)
                else:
                    header[key] = value
            return stamp(header, manifest, body_bytes if body is _DEFAULT else body)

        cases = {
            "coverage more contributing": with_header(bases["measured"], coverage={"contributing": 3}),
            "coverage moved": with_header(bases["partial"], coverage={"failed": 0, "quarantined": 2}),
            "coverage hides failure": with_header(bases["failed"], coverage={"failed": 0}, primary_state="unresolved"),
            "measured as partial": with_header(bases["measured"], primary_state="partial"),
            "partial as measured": with_header(bases["partial"], primary_state="measured"),
            "missing with evidence": with_header(bases["failed"], primary_state="missing"),
            "missing as measured": with_header(bases["missing"], primary_state="measured"),
            "missing as unresolved": with_header(bases["missing"], primary_state="unresolved"),
            "body on failed": with_header(bases["failed"], body=b"{}"),
            "body on missing": with_header(bases["missing"], body=b"{}", primary_state="measured"),
            "no body on measured": with_header(bases["measured"], body=None),
            "no body, contributors only": with_header(bases["measured"], body=None, primary_state="unresolved"),
        }
        for state in ("quarantined", "policy-pending", "unresolved", "missing", "partial", "measured"):
            cases[f"failed as {state}"] = with_header(bases["no-value"], primary_state=state)
        for name, triple in cases.items():
            with self.subTest(name):
                self.assertEqual(self.decode_refused(*triple), BYTES_INVALID)
        # The same stamping leaves a genuine summary valid.
        for name, summary in bases.items():
            self.assertEqual(decode_summary(*with_header(summary)), summary)

    def test_unknown_contract_version_is_unsupported(self) -> None:
        bases = self.bases()
        for version in ("summary-contract@2", "summary-contract@3", "summary-contract@10"):
            for name, summary in bases.items():
                with self.subTest(version=version, base=name):
                    header, manifest, body = split(summary)
                    header["contract_version"] = version
                    self.assertEqual(self.decode_refused(*stamp(header, manifest, body)), UNSUPPORTED)
                    # A later version may add or drop keys; the version is read first.
                    header["future_field"] = ["x"]
                    del header["coverage"]
                    self.assertEqual(self.decode_refused(*stamp(header, manifest, body)), UNSUPPORTED)
        header, manifest, body = split(bases["partial"])
        manifest["contract_version"] = "summary-contract@2"
        self.assertIn(self.decode_refused(*stamp(header, manifest, body)), (UNSUPPORTED, BYTES_INVALID))


# ---- R13 ----

class R13PublicationConditions(Checks):
    def test_publication_conditions_are_checked_and_sorted(self) -> None:
        self.assertEqual(check_publication_conditions(frozenset()), ())
        self.assertEqual(check_publication_conditions(set(PUBLICATION_CONDITIONS)), PUBLICATION_CONDITIONS)
        self.assertEqual(check_publication_conditions(tuple(reversed(PUBLICATION_CONDITIONS))), PUBLICATION_CONDITIONS)
        self.assertEqual(check_publication_conditions(["superseded", "conflicted"]), ("conflicted", "superseded"))
        self.assertEqual(check_publication_conditions({"refresh-failed"}), ("refresh-failed",))
        for names in ({"conflicted", "source-conflict"}, {"selection-unresolved"}, {"pending"}, {"Conflicted"}, {""},
                      {None}, {1}, ("conflicted", "stale"), "conflicted"):
            with self.subTest(names=names):
                self.assertEqual(self.refused(check_publication_conditions, names), INVALID)

    def test_publication_conditions_are_not_in_summary_bytes(self) -> None:
        for name in PUBLICATION_CONDITIONS:
            with self.subTest(name):
                self.assertEqual(self.build_refused(evidence=(entry(1), entry(2, "unresolved")),
                                                    reported_conditions=(name,)), INVALID)
        summaries = [self.built(**f) for f in scenario_facts()]
        for summary in summaries:
            for data in (summary.header_bytes, summary.evidence_bytes):
                scalars = set(json_scalars(json.loads(data)))
                self.assertFalse(scalars & set(PUBLICATION_CONDITIONS))
            self.assertFalse(any("publication" in field.name for field in dataclasses.fields(summary.header)))
        header, manifest, body = split(summaries[1])
        header["evidence_conditions"] = sorted(header["evidence_conditions"] + ["superseded"])
        self.assertEqual(self.decode_refused(*stamp(header, manifest, body)), BYTES_INVALID)


# ---- R14 ----

class R14HealthDataFree(Checks):
    def test_failures_carry_only_their_name(self) -> None:
        marker_body = body_of({"note": HEALTH_MARKER, "total": MARKER_VALUE})
        good = self.built(body=marker_body)
        header, manifest, body = split(good)
        marker_header = dict(header, local_date=HEALTH_MARKER)
        marker_record = json.loads(good.evidence_bytes)
        marker_record["evidence"][0]["record_id"] = HEALTH_MARKER
        future = dict(header, contract_version="summary-contract@2", note=HEALTH_MARKER)
        cases = [
            (INVALID, lambda: build_summary(**facts(body=b'{"note": "' + HEALTH_MARKER.encode() + b'"}'))),
            (INVALID, lambda: build_summary(**facts(body=body_of({"total": 2**60, "note": HEALTH_MARKER})))),
            (INVALID, lambda: build_summary(**facts(derivation_version=HEALTH_MARKER))),
            (INVALID, lambda: build_summary(**facts(reported_conditions=(HEALTH_MARKER,)))),
            (INVALID, lambda: build_summary(**facts(evidence=(entry(1), entry(2, HEALTH_MARKER))))),
            (INVALID, lambda: build_summary(**facts(evidence=(entry(1), entry(2, processor_version=HEALTH_MARKER + "@1"))))),
            (INVALID, lambda: build_summary(**facts(evidence=(entry(1), entry(MARKER_VALUE), entry(MARKER_VALUE))))),
            (INVALID, lambda: build_summary(**facts(positions=(position(HEALTH_MARKER), position(HEALTH_MARKER))))),
            (INVALID, lambda: build_summary(**facts(measure=HEALTH_MARKER))),
            (INVALID, lambda: build_summary(**facts(local_date=HEALTH_MARKER))),
            (INVALID, lambda: primary_state(HEALTH_MARKER, True)),
            (INVALID, lambda: coverage_of((entry(1, HEALTH_MARKER),))),
            (INVALID, lambda: check_publication_conditions({HEALTH_MARKER})),
            (INCONSISTENT, lambda: build_summary(**facts(evidence=(entry(MARKER_VALUE, "failed"),), body=marker_body))),
            (INCONSISTENT, lambda: build_summary(**facts(evidence=(entry(MARKER_VALUE),),
                                                         reported_conditions=("selection-unresolved",)))),
            (BYTES_INVALID, lambda: decode_summary(*stamp(dict(marker_header), json.loads(good.evidence_bytes), body))),
            (BYTES_INVALID, lambda: decode_summary(*stamp(json.loads(good.header_bytes), marker_record, body))),
            (BYTES_INVALID, lambda: decode_summary(good.header_bytes, good.evidence_bytes,
                                                   marker_body.replace(b":", b": ", 1))),
            (BYTES_INVALID, lambda: decode_summary(good.header_bytes.replace(b"}", b' "' + HEALTH_MARKER.encode() + b'"}'),
                                                   good.evidence_bytes, body)),
            (BYTES_INVALID, lambda: decode_summary(good.header_bytes, good.evidence_bytes,
                                                   body_of({"note": HEALTH_MARKER, "total": MARKER_VALUE + 1}))),
            (UNSUPPORTED, lambda: decode_summary(*stamp(future, json.loads(good.evidence_bytes), body))),
        ]
        seen = set()
        for expected, call in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(SummaryContractFailure) as caught:
                    call()
                failure = caught.exception
                self.assertEqual(failure.args, (expected,))
                self.assertIsNone(failure.__cause__)
                for text in (str(failure), repr(failure), repr(failure.args), repr(vars(failure)),
                             "".join(traceback.format_exception(failure))):
                    self.assert_marker_free(text)
                seen.add(expected)
        self.assertEqual(seen, set(FAILURES))

    def test_header_and_manifest_carry_no_values_or_origins(self) -> None:
        body = body_of({"note": HEALTH_MARKER, "total": MARKER_VALUE, "series": [MARKER_VALUE, 1]})
        summary = self.built(body=body, evidence=mixed_evidence(), positions=two_positions(),
                             reported_conditions=("equal-time-conflict",))
        for data in (summary.header_bytes, summary.evidence_bytes):
            self.assert_marker_free(data.decode("utf-8"))
            self.assertNotIn(body, data)
            self.assertNotIn(body.hex().encode(), data)
        self.assertEqual(json.loads(summary.header_bytes)["body_sha256"], sha(body))
        keys = set(json_scalars(json.loads(summary.header_bytes))) | set(json_scalars(json.loads(summary.evidence_bytes)))
        for forbidden in ("origin", "source_identity", "value", "values", "time", "start_time", "end_time", "body",
                          "projection", "revision", "data_origin", "client_record_id"):
            self.assertNotIn(forbidden, keys)

    def test_summary_repr_omits_bytes(self) -> None:
        body = body_of({"note": HEALTH_MARKER, "total": MARKER_VALUE})
        summary = self.built(body=body)
        text = repr(summary)
        self.assert_marker_free(text)
        for name in ("header_bytes", "evidence_bytes", "body_bytes"):
            self.assertNotIn(name, text)
        for data in (summary.header_bytes, summary.evidence_bytes, body):
            self.assertNotIn(repr(data), text)
            self.assertNotIn(data.hex(), text)
        self.assertEqual({field.name for field in dataclasses.fields(summary) if not field.repr},
                         {"header_bytes", "evidence_bytes", "body_bytes"})


if __name__ == "__main__":
    unittest.main()
