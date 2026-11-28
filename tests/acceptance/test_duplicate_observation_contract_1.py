"""Acceptance tests for `duplicate-observation-contract@1`.

Requirement numbers refer to `packs/duplicate-observation-contract-1.spec.md`. Every observation is synthetic and
built by hand or by a seeded generator: record identities, positions, source-system kinds, origins, instants, and
values. Expected results come from a short model of R5-R8 written in this file, never from the unit. Marker values
and origins prove that failures, tracebacks, and reprs carry no input.
"""

from __future__ import annotations

import collections
import collections.abc
import dataclasses
import functools
import itertools
import json
import math
import os
import random
import re
import signal
import subprocess
import sys
import time
import traceback
import types
import unittest
import weakref
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from activity_tracker_portal import summary_contract
from activity_tracker_portal.duplicate_observation_contract import (
    CONTRACT_VERSION,
    FAILURES,
    GROUP_CONFIDENCE,
    GROUP_RULE,
    Contribution,
    DuplicateObservationFailure,
    Observation,
    ObservationRef,
    Resolution,
    TieClass,
    UnresolvedTie,
    resolve,
)

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CONTRACT = json.loads((ROOT / "governance/scopes/duplicate-observation-contract.1.json").read_text(encoding="utf-8"))
EFFECTS = json.loads((ROOT / "packs/duplicate-observation-contract-1.effects.json").read_text(encoding="utf-8"))
SCOPE_MAP = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")

V1 = "duplicate-observation-contract@1"
RULE = "exact-instant-and-value"
CONFIDENCE = "exact"
INVALID = "duplicate-observation-input-invalid"

KIND_A = "synthetic-kind-a"
KIND_B = "synthetic-kind-b"
ORIGIN_A = "org.synthetic.alpha"
ORIGIN_B = "org.synthetic.beta"
ORIGIN_C = "org.synthetic.gamma"
# 2026-01-15T00:00:00Z in nanoseconds.
T0 = 1_768_435_200 * 10**9

# Markers: no failure, traceback, or repr may show them.
MARKER_RECORD = 8_314_159_265
MARKER_INDEX = 271_828
MARKER_KIND = "synthetic-kind-marker"
MARKER_ORIGIN = "synthetic-origin-marker"
MARKER_INSTANT = 1_768_473_627_182_818_284
MARKER_INT = 7_314_159
MARKER_FLOAT = 73141.59265

# R11's limit. A reference implementation resolves the 300,000 observations in 1.4 to 1.9 s on the drafting
# machine (Apple M2); the limit is about 16 times the slowest of those runs.
SCALE_SIZE = 300_000
SCALE_LIMIT_SECONDS = 30.0


# ---- Synthetic observations ----

def obs(record_id: int, index: int = 0, instant: int = T0, value=72, origin: str = ORIGIN_A,
        kind: str = KIND_A) -> Observation:
    return Observation(record_id=record_id, index=index, source_system_kind=kind, origin=origin,
                       instant_ns=instant, value=value)


INSTANT_OFFSETS = (0, 1, 2, 999, 1_000, 999_999, 1_000_000, 10**9, 10**9 + 1, -1, -(10**9), 86_400 * 10**9)
INT_VALUES = (-3, 0, 1, 2, 9, 10, 72, 73, 100, 2**53, 2**53 + 1, 10**30)
FLOAT_VALUES = (-0.0, 0.0, 0.1, 0.3, 0.1 + 0.2, -1.5, 5e-324, 72500.0, 72499.99, math.nextafter(72500.0, math.inf),
                1e16, 9.5)
PAIRS = tuple((kind, origin) for kind in (KIND_A, KIND_B, "synthetic-kind-é")
              for origin in (ORIGIN_A, ORIGIN_B, ORIGIN_C, "org.synthetic.Z"))


def generated(seed: int, size: int, kind: str = "int", instants: int = 6, values: int = 3) -> list[Observation]:
    """A seeded set with many shared instants, adjacent instants, and repeated values. One origin per record."""
    rng = random.Random(seed)
    instant_pool = [T0 + offset for offset in rng.sample(INSTANT_OFFSETS, min(instants, len(INSTANT_OFFSETS)))]
    value_pool = rng.sample(INT_VALUES if kind == "int" else FLOAT_VALUES, values)
    if kind != "int" and rng.random() < 0.5:
        # Both zeros in one pool, so -0.0 and 0.0 often meet at one instant.
        value_pool += [-0.0, 0.0]
    record_pair: dict[int, tuple[str, str]] = {}
    next_index: dict[int, int] = {}
    out = []
    for _ in range(size):
        record_id = rng.randrange(1, max(3, size // 3) + 1)
        pair = record_pair.setdefault(record_id, rng.choice(PAIRS))
        index = next_index.get(record_id, rng.choice((0, 0, 3)))
        next_index[record_id] = index + rng.choice((1, 1, 2, 9))
        out.append(Observation(record_id, index, pair[0], pair[1], rng.choice(instant_pool), rng.choice(value_pool)))
    rng.shuffle(out)
    return out


def generated_sets() -> list[list[Observation]]:
    sets = []
    for seed in range(48):
        rng = random.Random(10_000 + seed)
        sets.append(generated(seed, rng.choice((1, 2, 5, 17, 60, 240)), "int" if seed % 2 else "float",
                              instants=rng.randrange(1, 9), values=rng.randrange(1, 5)))
    return sets


def scale_input() -> list[Observation]:
    """300,000 Heart-Rate-shaped samples: 600 records of 500, about 155,000 distinct instants, groups and ties."""
    rng = random.Random(41)
    pairs = PAIRS[:6]
    out = []
    for k in range(SCALE_SIZE):
        record_id = k // 500 + 1
        pair = pairs[record_id % len(pairs)]
        out.append(Observation(record_id, k % 500, pair[0], pair[1], T0 + rng.randrange(200_000) * 10**9,
                               60 + rng.randrange(3)))
    return out


# ---- Independent model of R5-R8, in plain tuples ----

def num(value) -> tuple[str, str]:
    """A value with its type and exact spelling, so 0.0 and -0.0, and 1 and 1.0, differ."""
    return (type(value).__name__, repr(value))


def plain_value(value):
    # Equal values are one value; a zero float is written as positive zero, as its RFC 8785 bytes are.
    return 0.0 if isinstance(value, float) and value == 0 else value


def model(observations) -> tuple:
    at: dict[int, list[Observation]] = {}
    for item in observations:
        at.setdefault(item.instant_ns, []).append(item)
    contributions, ties = [], []
    for instant in sorted(at):
        classes: dict = {}
        for item in at[instant]:
            classes.setdefault(item.value, []).append(item)  # -0.0 and 0.0 are one key.
        if len(classes) == 1:
            members = at[instant]
            refs = tuple(sorted((m.record_id, m.index) for m in members))
            contributions.append((
                instant,
                num(plain_value(members[0].value)),
                refs,
                tuple(sorted({(m.source_system_kind, m.origin) for m in members})),
                tuple(sorted({m.record_id for m in members})),
                len(members) >= 2,
            ))
        else:
            ordered = sorted(classes.items(), key=lambda item: item[0])
            ties.append((
                instant,
                tuple((num(plain_value(value)), tuple(sorted((m.record_id, m.index) for m in members)))
                      for value, members in ordered),
                tuple(sorted({m.record_id for m in at[instant]})),
            ))
    groups = tuple(c for c in contributions if c[5])
    return (
        V1, RULE, CONFIDENCE, tuple(contributions), tuple(ties), groups,
        tuple(sorted({ref[0] for c in groups for ref in c[2]})),
        tuple(sorted({ref[0] for t in ties for _, refs in t[1] for ref in refs})),
    )


def shape(result: Resolution) -> tuple:
    """The unit's result in the model's plain form, read through every field and property."""
    def refs(members):
        return tuple((m.record_id, m.index) for m in members)

    def contribution(c):
        return (c.instant_ns, num(c.value), refs(c.members), tuple(tuple(p) for p in c.origins), tuple(c.record_ids),
                c.is_group)

    return (
        result.contract_version, result.rule, result.confidence,
        tuple(contribution(c) for c in result.contributions),
        tuple((t.instant_ns, tuple((num(k.value), refs(k.members)) for k in t.classes), tuple(t.record_ids))
              for t in result.ties),
        tuple(contribution(c) for c in result.groups),
        tuple(result.grouped_record_ids), tuple(result.tied_record_ids),
    )


def scenario_inputs() -> list[list[Observation]]:
    return [
        [obs(3, 0, T0, 72), obs(1, 2, T0, 72, ORIGIN_B), obs(2, 0, T0 + 1, 72), obs(4, 0, T0 + 1, 73, ORIGIN_C),
         obs(1, 0, T0 - 1, 9, ORIGIN_B)],
        [obs(1, 0, T0, -0.0), obs(2, 0, T0, 0.0, ORIGIN_B), obs(3, 0, T0 + 5, 72500.0), obs(4, 0, T0 + 5, 72499.99)],
        generated(7, 200, "int"),
        generated(8, 200, "float"),
    ]


def bad_inputs() -> list:
    good = obs(1)
    return [
        [good, (1, 1, KIND_A, ORIGIN_A, T0, 72)],
        [good, obs(2, value=True)],
        [good, obs(2, value=math.nan)],
        [good, dataclasses.replace(good)],
        [good, obs(1, 1, origin=ORIGIN_B)],
        [good, obs(2, value=72.0)],
        None,
    ]


def scenario_text() -> str:
    """Every scenario resolved, rendered as text for comparison across processes."""
    out = []
    for items in scenario_inputs():
        # A frozenset's iteration order follows the hash seed.
        out.append(json.dumps(shape(resolve(frozenset(items)))))
        out.append(repr(resolve(items)))
    for bad in bad_inputs():
        try:
            resolve(bad)
        except DuplicateObservationFailure as failure:
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


def first_difference(actual: tuple, expected: tuple) -> str:
    names = ("contract_version", "rule", "confidence", "contributions", "ties", "groups", "grouped_record_ids",
             "tied_record_ids")
    for name, left, right in zip(names, actual, expected):
        if left != right:
            if isinstance(left, tuple) and isinstance(right, tuple):
                for position, (a, b) in enumerate(zip(left, right)):
                    if a != b:
                        return f"{name}[{position}]: {a!r} != {b!r}"[:2000]
                return f"{name}: length {len(left)} != {len(right)}"
            return f"{name}: {left!r} != {right!r}"[:2000]
    return "none"


class ScaleTimeout(BaseException):
    """Raised by the timer when resolve runs past R11's limit."""


# ---- Health-data-free failures (R12) ----

def marker_observations() -> list[Observation]:
    """Valid observations carrying every marker: a group and a tie."""
    return [
        Observation(MARKER_RECORD, MARKER_INDEX, MARKER_KIND, MARKER_ORIGIN, MARKER_INSTANT, MARKER_INT),
        Observation(MARKER_RECORD + 1, MARKER_INDEX, MARKER_KIND, MARKER_ORIGIN, MARKER_INSTANT, MARKER_INT),
        Observation(MARKER_RECORD + 2, MARKER_INDEX, MARKER_KIND, MARKER_ORIGIN, MARKER_INSTANT + 1, MARKER_INT),
        Observation(MARKER_RECORD + 3, MARKER_INDEX, MARKER_KIND, MARKER_ORIGIN, MARKER_INSTANT + 1, MARKER_INT + 1),
    ]


def marker_texts() -> tuple[str, ...]:
    texts = {MARKER_KIND, MARKER_ORIGIN, str(MARKER_INSTANT), str(MARKER_INSTANT + 1), str(MARKER_INT),
             str(MARKER_INT + 1), repr(MARKER_FLOAT), str(MARKER_INDEX)}
    texts.update(str(MARKER_RECORD + k) for k in range(4))
    return tuple(sorted(texts))


def failure_cases() -> list[tuple[str, object]]:
    """Each malformed input, among marker observations. Each is (name, argument to resolve)."""
    base = marker_observations()
    first = base[0]

    def with_(item):
        return base + [item]

    return [
        ("non-observation element", with_(dataclasses.astuple(first))),
        ("duck-typed element", with_(types.SimpleNamespace(**dataclasses.asdict(first)))),
        ("record_id", with_(dataclasses.replace(first, record_id=str(MARKER_RECORD)))),
        ("index", with_(dataclasses.replace(first, record_id=MARKER_RECORD + 9, index=-MARKER_INDEX))),
        ("source_system_kind", with_(dataclasses.replace(first, record_id=MARKER_RECORD + 9, source_system_kind=""))),
        ("origin", with_(dataclasses.replace(first, record_id=MARKER_RECORD + 9, origin=MARKER_ORIGIN.encode()))),
        ("instant_ns", with_(dataclasses.replace(first, record_id=MARKER_RECORD + 9, instant_ns=float(MARKER_INSTANT)))),
        ("bool value", with_(dataclasses.replace(first, record_id=MARKER_RECORD + 9, value=True))),
        ("non-finite value", with_(dataclasses.replace(first, record_id=MARKER_RECORD + 9, value=math.inf))),
        ("text value", with_(dataclasses.replace(first, record_id=MARKER_RECORD + 9, value=str(MARKER_INT)))),
        ("decimal value", with_(dataclasses.replace(first, record_id=MARKER_RECORD + 9, value=Decimal(MARKER_INT)))),
        ("repeated reference", with_(dataclasses.replace(first, value=MARKER_INT + 1))),
        ("two origins", with_(dataclasses.replace(first, index=MARKER_INDEX + 1, origin=MARKER_ORIGIN + "-b"))),
        ("mixed value kinds", with_(dataclasses.replace(first, record_id=MARKER_RECORD + 9, value=MARKER_FLOAT))),
        ("non-iterable argument", first),
    ]


def referents(value) -> list:
    """The objects `value` holds, for the kinds of holder the walker can open (R12).

    Containers, dict views, dataclasses, objects with a `__dict__` or `__slots__`, exceptions (their arguments,
    context, cause, and traceback), tracebacks and frames (their locals), generators and coroutines (their frame's
    locals), functions (defaults, keyword defaults, and closure cells), cells, bound methods (`__self__` and
    `__func__`), built-in bound methods and method wrappers (`__self__`), `functools.partial`, weak references, and
    built-in iterators whose `__reduce__` names what they iterate. It cannot open itertools iterators, which lost
    `__reduce__` in Python 3.14, or other C-level objects that expose nothing.
    """
    if value is None or isinstance(value, (str, bytes, int, float, complex, type, types.ModuleType)):
        return []
    if isinstance(value, dict):
        return [*value.keys(), *value.values()]
    if isinstance(value, (list, tuple, set, frozenset, collections.deque, collections.abc.KeysView,
                          collections.abc.ValuesView, collections.abc.ItemsView)):
        return list(value)
    if isinstance(value, types.GeneratorType):
        return [value.gi_frame]
    if isinstance(value, types.CoroutineType):
        return [value.cr_frame]
    if isinstance(value, types.AsyncGeneratorType):
        return [value.ag_frame]
    if isinstance(value, types.FrameType):
        return list(value.f_locals.values())
    if isinstance(value, types.TracebackType):
        return [value.tb_frame, value.tb_next]
    if isinstance(value, types.FunctionType):
        return [*(value.__defaults__ or ()), *(value.__kwdefaults__ or {}).values(), *(value.__closure__ or ())]
    if isinstance(value, types.CellType):
        try:
            return [value.cell_contents]
        except ValueError:
            return []
    if isinstance(value, types.MethodType):
        return [value.__self__, value.__func__]
    if isinstance(value, (types.BuiltinMethodType, types.MethodWrapperType)):
        return [value.__self__]
    if isinstance(value, functools.partial):
        return [value.func, *value.args, *value.keywords.values()]
    if isinstance(value, weakref.ref):
        return [value()]
    out = []
    if isinstance(value, BaseException):
        out += [*value.args, value.__cause__, value.__context__, value.__traceback__]
    if dataclasses.is_dataclass(value):
        out += [getattr(value, f.name) for f in dataclasses.fields(value)]
    if isinstance(value, collections.abc.Iterator):
        try:
            reduced = value.__reduce__()
        except Exception:
            reduced = None
        if isinstance(reduced, tuple):
            out += list(reduced[1:])
    if hasattr(value, "__dict__"):
        out += list(vars(value).values())
    for klass in type(value).__mro__:
        slots = vars(klass).get("__slots__", ())
        for name in ((slots,) if isinstance(slots, str) else slots):
            out.append(getattr(value, name, None))
    return out


def held(value, forbidden: set[int], seen: set[int], depth: int = 0) -> bool:
    """Whether `value` is, or holds through the referents above, one of the forbidden objects."""
    if id(value) in forbidden:
        return True
    if depth > 16 or id(value) in seen:
        return False
    seen.add(id(value))
    return any(held(item, forbidden, seen, depth + 1) for item in referents(value))


def input_objects(argument) -> set[int]:
    """Identities of the argument, its elements, and their non-trivial field values."""
    objects = [argument]
    elements = list(argument) if isinstance(argument, list) else [argument]
    for element in elements:
        objects.append(element)
        fields = vars(element).values() if hasattr(element, "__dict__") else (
            element if isinstance(element, tuple) else ())
        for value in fields:
            if isinstance(value, (str, bytes, float, Decimal)) or (isinstance(value, int) and abs(value) > 256):
                objects.append(value)
    return {id(item) for item in objects}


# ---- Checks ----

class Checks(unittest.TestCase):
    maxDiff = None

    def check_types(self, result: Resolution) -> None:
        self.assertIs(type(result), Resolution)
        self.assertEqual((result.contract_version, result.rule, result.confidence), (V1, RULE, CONFIDENCE))
        for name in ("contributions", "ties", "grouped_record_ids", "tied_record_ids", "groups"):
            self.assertIs(type(getattr(result, name)), tuple, name)
        for ids in (result.grouped_record_ids, result.tied_record_ids):
            self.assertTrue(all(type(i) is int for i in ids))

        def check_members(members):
            self.assertIs(type(members), tuple)
            self.assertTrue(members)
            for member in members:
                self.assertIs(type(member), ObservationRef)

        for c in result.contributions:
            self.assertIs(type(c), Contribution)
            check_members(c.members)
            self.assertIs(type(c.origins), tuple)
            for pair in c.origins:
                self.assertIs(type(pair), tuple)
                self.assertEqual([type(part) for part in pair], [str, str])
            self.assertIs(type(c.record_ids), tuple)
            self.assertIs(type(c.is_group), bool)
        for t in result.ties:
            self.assertIs(type(t), UnresolvedTie)
            self.assertIs(type(t.classes), tuple)
            self.assertGreaterEqual(len(t.classes), 2)
            for k in t.classes:
                self.assertIs(type(k), TieClass)
                check_members(k.members)
            self.assertIs(type(t.record_ids), tuple)
        for g in result.groups:
            self.assertIs(type(g), Contribution)

    def resolved(self, observations, given=None) -> Resolution:
        """Resolve, check every field against the model, and return the result."""
        items = list(observations)
        result = resolve(tuple(items) if given is None else given)
        self.check_types(result)
        self.assertEqual(shape(result), model(items))
        return result

    def refused(self, argument) -> None:
        with self.assertRaises(DuplicateObservationFailure) as caught:
            resolve(argument)
        self.assertIs(type(caught.exception), DuplicateObservationFailure)
        self.assertEqual(caught.exception.args, (INVALID,))

    def assert_marker_free(self, text: str) -> None:
        for marker in marker_texts():
            self.assertNotIn(marker, text)


def at_instant(result: Resolution, instant: int):
    found = [c for c in result.contributions if c.instant_ns == instant] + \
            [t for t in result.ties if t.instant_ns == instant]
    return found


def plain_at(result: Resolution) -> dict[int, tuple]:
    """Each instant's resolution in plain form."""
    plain = shape(result)
    out = {c[0]: ("contribution", c) for c in plain[3]}
    out.update({t[0]: ("tie", t) for t in plain[4]})
    return out


# ---- R1 ----

class R1GovernedScopeContract(unittest.TestCase):
    def test_contract_shape_and_dependency(self) -> None:
        self.assertEqual((CONTRACT["contract_schema"], CONTRACT["scope"], CONTRACT["version"], CONTRACT["kind"]),
                         (1, "duplicate-observation-contract", "1", "shared"))
        self.assertEqual(CONTRACT["dependencies"], [{"scope": "summary-contract", "version": "1"}])
        self.assertEqual([(item["name"], item["outcome"]) for item in CONTRACT["named_failures"]],
                         [("duplicate-observation-input-invalid", "invariant-refusal")])
        self.assertEqual(CONTRACT["observable_effects"], [])
        self.assertEqual({item["mode"] for item in CONTRACT["inputs"]}, {"pure"})
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["specification"], "packs/duplicate-observation-contract-1.spec.md")
        self.assertEqual(oracle["executable_tests"],
                         {"python": "tests/acceptance/test_duplicate_observation_contract_1.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_duplicate_observation_contract_1.sh")
        self.assertEqual(oracle["languages"], ["python"])

    def test_effects_declaration_is_empty(self) -> None:
        self.assertEqual(EFFECTS, {"effects_format": 1, "effects": []})

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        row = next(line for line in SCOPE_MAP.splitlines() if line.startswith("| `duplicate-observation-contract` |"))
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        self.assertEqual(cells[1], "shared")
        self.assertEqual(set(re.findall(r"`([a-z0-9-]+)`", cells[-1])),
                         {item["scope"] for item in CONTRACT["dependencies"]})
        self.assertIn("Duplicate Group", row)
        self.assertIn("unresolved tie", row)
        # Decision 4: a group may come from any origins.
        self.assertIn("from any origins", row)


# ---- R2 ----

class R2PureDeterministicOrderIndependent(Checks):
    def test_result_ignores_input_order(self) -> None:
        small = [obs(3, 0, T0, 72), obs(1, 2, T0, 72, ORIGIN_B), obs(2, 0, T0 + 1, 72), obs(4, 0, T0 + 1, 73, ORIGIN_C),
                 obs(5, 0, T0 + 1, 72, ORIGIN_C)]
        first = self.resolved(small)
        for order in itertools.permutations(small):
            self.assertEqual(resolve(list(order)), first)
        floats = [obs(1, 0, T0, -0.0), obs(2, 0, T0, 0.0, ORIGIN_B), obs(3, 0, T0 + 1, 0.0), obs(4, 0, T0 + 1, 1.5),
                  obs(5, 0, T0 + 1, -0.0, ORIGIN_C)]
        first_float = self.resolved(floats)
        for order in itertools.permutations(floats):
            result = resolve(order)
            self.assertEqual(shape(result), shape(first_float))
        for seed in range(6):
            items = generated(100 + seed, 300, "float" if seed % 2 else "int")
            expected = self.resolved(items)
            rng = random.Random(seed)
            for _ in range(8):
                shuffled = list(items)
                rng.shuffle(shuffled)
                self.assertEqual(shape(resolve(shuffled)), shape(expected))
            copies = [dataclasses.replace(item) for item in reversed(items)]
            # A dict is an iterable of its keys.
            keyed = {item: f"metadata-{k}" for k, item in enumerate(items)}
            for given in (copies, tuple(items), iter(items), (item for item in items), frozenset(items), keyed):
                self.assertEqual(shape(resolve(given)), shape(expected))
        first, second = obs(1), obs(2, origin=ORIGIN_B)
        self.assertEqual(len(self.resolved([first, second], given={first: "metadata-a", second: "metadata-b"}).groups), 1)

    def test_result_ignores_hash_seed_and_host_timezone(self) -> None:
        expected = scenario_text()
        code = "import test_duplicate_observation_contract_1 as t; print(t.scenario_text())"
        for settings in (
            {"TZ": "America/Los_Angeles", "LC_ALL": "C", "LANG": "C", "PYTHONHASHSEED": "1"},
            {"TZ": "Asia/Kolkata", "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8", "PYTHONHASHSEED": "2"},
            {"TZ": "Pacific/Chatham", "LC_ALL": "de_DE.UTF-8", "LANG": "de_DE.UTF-8", "PYTHONHASHSEED": "3"},
        ):
            with self.subTest(**settings):
                stdout, _ = run_python(code, settings)
                self.assertEqual(stdout.strip().splitlines()[-1], expected)

    def test_resolve_reads_no_clock_or_io(self) -> None:
        scenario_text()
        code = r"""
import json, os, sys, time, logging, zoneinfo
# Modules an implementation commonly needs are imported first, and summary_contract, which R10 uses.
import hashlib, re, decimal, fractions, dataclasses, typing, collections, functools, itertools, math, unicodedata
import copy, enum, struct, base64, datetime, string, operator, bisect, heapq, io, traceback, types, inspect
import contextlib, pathlib, sqlite3, shutil, importlib.resources, collections.abc, numbers, codecs, unittest
import locale, calendar, _strptime, json.decoder, json.encoder, binascii, textwrap, weakref, array, ipaddress
import random, signal, subprocess, statistics
import activity_tracker_portal.summary_contract
# During the calls, the only file-system activity allowed is the import system loading a module: an implementation
# may import any standard-library or published module lazily. An open, metadata read, or listing is allowed only while
# the import system is importing a module; anything else is flagged, on every call, with no warm-up and no path
# exemption.
# A module is being imported only when the import system's own import routine is on the stack. Loader helper methods
# such as FileLoader.get_data, reached through pkgutil.get_data or importlib.resources, are not imports.
def loading():
    frame = sys._getframe(1)
    while frame is not None:
        code = frame.f_code
        if code.co_filename == "<frozen importlib._bootstrap>" and code.co_name in ("_find_and_load", "_load"):
            return True
        frame = frame.f_back
    return False
busy = [False]
def real(path):
    # realpath itself calls os.lstat, so the guards below pass through while it runs.
    busy[0] = True
    try:
        return os.path.realpath(path)
    finally:
        busy[0] = False
host = [os.path.realpath(p) for p in zoneinfo.TZPATH] + [os.path.realpath("/etc/localtime"), "/etc/localtime"]
events = []
phase = ["import"]
WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
def hook(event, args):
    if phase[0] == "done" or busy[0]:
        return
    if event == "open":
        path, mode, flags = args
        shown = os.fsdecode(path) if isinstance(path, (str, bytes)) else repr(path)
        if phase[0] == "calls" and not loading():
            events.append(["open", shown])
        if isinstance(path, (str, bytes)):
            if ((mode and any(c in mode for c in "wax+")) or (flags or 0) & WRITE) \
                    and not (loading() and ".pyc." in os.path.basename(shown) + "."):
                # The import system may write a module's bytecode cache; nothing else may write.
                events.append(["write", shown])
            resolved = real(shown)
            if any(resolved == h or resolved.startswith(h.rstrip("/") + "/") for h in host):
                events.append(["host-timezone", shown])
    elif event.split(".")[0] in ("socket", "sqlite3", "subprocess", "ctypes", "webbrowser", "urllib", "http") or event in (
            "os.system", "os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.chmod", "os.utime", "os.truncate",
            "os.symlink", "os.link", "os.posix_spawn", "os.exec", "os.fork", "shutil.copyfile", "shutil.rmtree"):
        events.append([event])
    elif phase[0] == "calls" and event in ("os.listdir", "os.scandir", "glob.glob", "os.chdir", "io.open_code") \
            and not loading():
        events.append([event])
def metadata(original, name):
    # CPython raises no audit event for stat-family calls, so they are guarded directly.
    def guard(target, *args, **kwargs):
        if phase[0] == "calls" and not busy[0] and not loading():
            events.append([name, os.fsdecode(target) if isinstance(target, (str, bytes, os.PathLike)) else repr(target)])
        return original(target, *args, **kwargs)
    return guard
for name in ("stat", "lstat", "fstat", "access"):
    setattr(os, name, metadata(getattr(os, name), "os." + name))
# Each clock guard records the read before it raises, so a failure boundary that turns every exception into the
# named refusal cannot hide a clock read on a refusal path.
clocks = []
def forbidden(name):
    def guard(*args, **kwargs):
        if phase[0] != "done":
            clocks.append(name)
        raise AssertionError("clock read")
    return guard
def explicit(original, index, name):
    def guard(*args):
        if len(args) <= index or args[index] is None:
            if phase[0] != "done":
                clocks.append(name)
            raise AssertionError("clock read")
        return original(*args)
    return guard
environment_reads = []
class Environ(collections.abc.MutableMapping):
    def __init__(self, inner):
        self.inner, self.reads = inner, environment_reads
    def note(self, key):
        # A module's own top-level code may read the environment while it is being imported.
        if not loading():
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
        setattr(time, name, forbidden("time." + name))
time.gmtime = explicit(time.gmtime, 0, "time.gmtime")
time.asctime = explicit(time.asctime, 0, "time.asctime")
time.strftime = explicit(time.strftime, 1, "time.strftime")
import activity_tracker_portal.duplicate_observation_contract as module
import test_duplicate_observation_contract_1 as t
# Both the text mapping and, where the platform has one, the bytes mapping are guarded, so os.getenv and
# os.getenvb are covered too.
original = os.environ
os.environ = Environ(original)
original_bytes = os.environb if os.supports_bytes_environ else None
if original_bytes is not None:
    os.environb = Environ(original_bytes)
phase[0] = "calls"
first = t.scenario_text()
second = t.scenario_text()
phase[0] = "done"
os.environ = original
if original_bytes is not None:
    os.environb = original_bytes
print(json.dumps({"events": events, "clock": clocks, "environment": environment_reads, "logs": records,
                  "same": first == second}))
"""
        stdout, stderr = run_python(code, {"TZ": "Europe/London"})
        self.assertEqual(stderr, "")
        self.assertEqual(len(stdout.strip().splitlines()), 1, stdout)
        self.assertEqual(json.loads(stdout), {"events": [], "clock": [], "environment": [], "logs": [], "same": True})

    def test_input_is_not_mutated(self) -> None:
        for seed, kind in ((3, "float"), (4, "int")):
            items = generated(seed, 200, kind)
            items.append(obs(9_999, 0, T0 + 3, -0.0 if kind == "float" else 0))
            given = list(items)
            before = [(id(item), repr(dataclasses.astuple(item))) for item in given]
            result = self.resolved(items, given=given)
            self.assertEqual([(id(item), repr(dataclasses.astuple(item))) for item in given], before)
            for name in ("contributions", "ties"):
                self.assertIsNot(getattr(result, name), given)
            # The result is built from new values, never the input's own objects.
            members = [m for c in result.contributions for m in c.members] + \
                      [m for t in result.ties for k in t.classes for m in k.members]
            self.assertFalse(any(type(m) is Observation for m in members))
        frozen = tuple(generated(5, 50))
        self.resolved(frozen, given=frozen)
        self.assertEqual(frozen, tuple(generated(5, 50)))

    def test_values_are_frozen(self) -> None:
        ref = ObservationRef(1, 0)
        values = (
            Observation(1, 0, KIND_A, ORIGIN_A, T0, 72),
            ref,
            Contribution(T0, 72, (ref,), ((KIND_A, ORIGIN_A),)),
            TieClass(72, (ref,)),
            UnresolvedTie(T0, (TieClass(72, (ref,)), TieClass(73, (ObservationRef(2, 0),)))),
            Resolution(V1, RULE, CONFIDENCE, (), (), (), ()),
        )
        expected_fields = {
            Observation: ["record_id", "index", "source_system_kind", "origin", "instant_ns", "value"],
            ObservationRef: ["record_id", "index"],
            Contribution: ["instant_ns", "value", "members", "origins"],
            TieClass: ["value", "members"],
            UnresolvedTie: ["instant_ns", "classes"],
            Resolution: ["contract_version", "rule", "confidence", "contributions", "ties", "grouped_record_ids",
                         "tied_record_ids"],
        }
        for value in values:
            with self.subTest(type(value).__name__):
                self.assertTrue(dataclasses.is_dataclass(value))
                self.assertTrue(type(value).__dataclass_params__.frozen)
                names = [item.name for item in dataclasses.fields(value)]
                self.assertEqual(names, expected_fields[type(value)])
                for name in names:
                    with self.assertRaises(dataclasses.FrozenInstanceError):
                        setattr(value, name, None)
        # Derived facts are properties, not constructor fields.
        for cls, name in ((Contribution, "record_ids"), (Contribution, "is_group"), (UnresolvedTie, "record_ids"),
                          (Resolution, "groups")):
            self.assertIsInstance(inspect_attr(cls, name), property, name)
        self.assertTrue(issubclass(DuplicateObservationFailure, Exception))
        self.assertEqual(DuplicateObservationFailure(INVALID).args, (INVALID,))


def inspect_attr(cls, name):
    for klass in cls.__mro__:
        if name in vars(klass):
            return vars(klass)[name]
    return None


# ---- R3 ----

class R3Constants(unittest.TestCase):
    def test_constants_are_exact(self) -> None:
        self.assertEqual(CONTRACT_VERSION, "duplicate-observation-contract@1")
        self.assertEqual(GROUP_RULE, "exact-instant-and-value")
        self.assertEqual(GROUP_CONFIDENCE, "exact")
        self.assertEqual(FAILURES, ("duplicate-observation-input-invalid",))
        for value in (CONTRACT_VERSION, GROUP_RULE, GROUP_CONFIDENCE):
            self.assertIs(type(value), str)
        self.assertIs(type(FAILURES), tuple)


# ---- R4 ----

class R4InputValidation(Checks):
    def test_each_field_domain_is_checked(self) -> None:
        base = [obs(1), obs(2, 0, T0, 72, ORIGIN_B), obs(3, 0, T0 + 1, 73)]
        float_base = [obs(1, value=72.5), obs(2, 0, T0, 72.5, ORIGIN_B), obs(3, 0, T0 + 1, 73.5)]
        bad = {
            "record_id": (0, -1, True, False, 1.0, 4.0, "4", None, Decimal(4)),
            "index": (-1, True, False, 0.0, "0", None),
            "source_system_kind": ("", None, b"synthetic-kind-a", 1, (KIND_A,)),
            "origin": ("", None, b"org.synthetic.alpha", 1, (ORIGIN_A,)),
            "instant_ns": (float(T0), 1.0, True, False, str(T0), None, Decimal(T0), Fraction(T0)),
        }
        value_bad = (True, False, "72", None, Decimal(72), Fraction(72), complex(72), [72], b"72")
        float_value_bad = (math.nan, math.inf, -math.inf, True)
        for name, values in bad.items():
            for value in values:
                with self.subTest(field=name, value=repr(value)):
                    changed = dataclasses.replace(obs(4, 0, T0 + 2, 74), **{name: value})
                    self.refused([changed])
                    self.refused(base + [changed])
                    self.refused([changed] + base)
        for value in value_bad:
            with self.subTest(field="value", value=repr(value)):
                changed = obs(4, 0, T0 + 2, value)
                self.refused([changed])
                self.refused(base + [changed])
                self.refused(float_base + [changed])
        for value in float_value_bad:
            with self.subTest(field="value", value=repr(value)):
                self.refused(float_base + [obs(4, 0, T0 + 2, value)])
                self.refused([obs(4, 0, T0 + 2, value)])
        # A bad observation is found anywhere in a large input.
        many = generated(9, 2000, "int")
        self.refused(many + [obs(10**6, 0, T0, True)])
        # The edges of each domain are accepted.
        self.resolved([obs(2**80, 2**70, -(2**80), 2**100), obs(1, 0, 0, -5), obs(1, 1, -1, 0),
                       obs(3, 0, 2**80, 72, "org.synthetic.é", "synthetic-kind-é")])
        self.resolved([obs(1, 0, T0, 5e-324), obs(2, 0, T0 + 1, -1.7976931348623157e308),
                       obs(3, 0, T0 + 2, 1.7976931348623157e308)])

    def test_repeated_observation_is_refused(self) -> None:
        first = obs(1, 0, T0, 72)
        for other in (first, dataclasses.replace(first), obs(1, 0, T0, 73), obs(1, 0, T0 + 5, 72),
                      obs(1, 0, T0 + 5, 99, ORIGIN_A, KIND_A)):
            with self.subTest(other=repr(dataclasses.astuple(other))):
                self.refused([first, other])
                self.refused([obs(2, 0), first, obs(3, 0, T0 + 1), other])
        items = generated(11, 300, "int")
        self.refused(items + [dataclasses.replace(items[137])])
        # The same position in another record, and another position in the same record, are distinct.
        self.resolved([first, obs(2, 0, T0, 72), obs(1, 1, T0, 72)])

    def test_record_with_two_origins_is_refused(self) -> None:
        cases = (
            [obs(1, 0, T0, 72, ORIGIN_A, KIND_A), obs(1, 1, T0, 72, ORIGIN_B, KIND_A)],
            [obs(1, 0, T0, 72, ORIGIN_A, KIND_A), obs(1, 1, T0, 72, ORIGIN_A, KIND_B)],
            [obs(1, 0, T0, 72, ORIGIN_A, KIND_A), obs(1, 1, T0 + 10**9, 80, ORIGIN_B, KIND_B)],
            [obs(1, 0, T0, 72), obs(2, 0, T0, 72), obs(1, 5, T0 + 7, 60, ORIGIN_C)],
        )
        for case in cases:
            with self.subTest(case=len(case)):
                self.refused(case)
                self.refused(list(reversed(case)))
        # Many records may share one origin pair, and one record keeps one pair across its positions.
        self.resolved([obs(1, 0), obs(2, 0), obs(1, 1, T0 + 1), obs(3, 0, T0, 72, ORIGIN_B)])

    def test_mixed_value_kinds_are_refused(self) -> None:
        cases = (
            [obs(1, 0, T0, 72), obs(2, 0, T0 + 1, 72.0)],
            [obs(1, 0, T0, 72), obs(2, 0, T0, 72.0)],
            [obs(1, 0, T0, 0), obs(2, 0, T0, -0.0)],
            [obs(1, 0, T0, 1.5), obs(2, 0, T0 + 1, 2.5), obs(3, 0, T0 + 2, 3)],
            generated(12, 200, "float") + [obs(10**6, 0, T0, 1)],
            generated(13, 200, "int") + [obs(10**6, 0, T0, 1.0)],
        )
        for case in cases:
            with self.subTest(size=len(case)):
                self.refused(case)
                self.refused(list(reversed(case)))

    def test_non_observation_is_refused(self) -> None:
        good = obs(1)
        elements = (
            ObservationRef(1, 0),
            dataclasses.astuple(obs(2)),
            dataclasses.asdict(obs(2)),
            types.SimpleNamespace(**dataclasses.asdict(obs(2))),
            None,
            72,
            "observation",
            [obs(2)],
        )
        for element in elements:
            with self.subTest(element=type(element).__name__):
                self.refused([element])
                self.refused([good, element])
                self.refused([element, good])
        # An argument that is not an iterable of observations (settles). A dict yields its keys.
        for argument in (None, 72, good, "observations", b"observations", {123: good}, {"metadata": good},
                         {good: "metadata", 123: obs(2)}):
            with self.subTest(argument=type(argument).__name__):
                self.refused(argument)

    def test_empty_input_gives_empty_result(self) -> None:
        empty = Resolution(V1, RULE, CONFIDENCE, (), (), (), ())
        for given in ([], (), iter(()), frozenset(), (item for item in ()), {}):
            with self.subTest(given=type(given).__name__):
                result = self.resolved([], given=given)
                self.assertEqual(result, empty)
                self.assertEqual(result.groups, ())


# ---- R5 ----

class R5ExactEquality(Checks):
    def test_instants_compare_by_nanoseconds(self) -> None:
        seconds, millis, nanos = 1_768_435_200, 1_768_435_200_000, 1_768_435_200_000_000_000
        result = self.resolved([obs(1, 0, seconds * 10**9, 72), obs(2, 0, millis * 10**6, 72, ORIGIN_B),
                                obs(3, 0, nanos, 72, ORIGIN_C)])
        self.assertEqual(len(result.contributions), 1)
        self.assertEqual(len(result.contributions[0].members), 3)
        self.assertEqual(result.contributions[0].instant_ns, nanos)
        # Contributions and ties are ordered by instant as numbers, not as text.
        instants = (10**19, 1_000, 999, 10, 9, 0, -1, -(10**9), -(10**20), T0)
        result = self.resolved([obs(k + 1, 0, instant, 72) for k, instant in enumerate(instants)])
        self.assertEqual([c.instant_ns for c in result.contributions], sorted(instants))
        tied = [obs(2 * k + 1, 0, instant, 72) for k, instant in enumerate(instants)] + \
               [obs(2 * k + 2, 0, instant, 73, ORIGIN_B) for k, instant in enumerate(instants)]
        result = self.resolved(tied)
        self.assertEqual([t.instant_ns for t in result.ties], sorted(instants))

    def test_adjacent_nanoseconds_are_distinct(self) -> None:
        instants = (T0 - 1, T0, T0 + 1, T0 + 2, T0 + 999, T0 + 1_000, T0 + 999_999, T0 + 1_000_000,
                    2**53, 2**53 + 1, 2**63 - 1, 2**63, 2**64 + 1)
        same = self.resolved([obs(k + 1, 0, instant, 72, (ORIGIN_A, ORIGIN_B)[k % 2]) for k, instant in enumerate(instants)])
        self.assertEqual(len(same.contributions), len(instants))
        self.assertEqual((same.groups, same.ties), ((), ()))
        different = self.resolved([obs(k + 1, 0, instant, 60 + k) for k, instant in enumerate(instants)])
        self.assertEqual(len(different.contributions), len(instants))
        self.assertEqual(different.ties, ())

    def test_values_compare_exactly_without_tolerance(self) -> None:
        pairs = (
            (72500.0, 72499.99),
            (72500.0, math.nextafter(72500.0, math.inf)),
            (0.3, 0.1 + 0.2),
            (1e16, 1e16 + 2),
            (72.4, 72.6),
            (5e-324, 0.0),
        )
        for left, right in pairs:
            with self.subTest(left=left, right=right):
                result = self.resolved([obs(1, 0, T0, left), obs(2, 0, T0, right, ORIGIN_B)])
                self.assertEqual((len(result.ties), result.contributions), (1, ()))
        for left, right in ((72, 73), (72_500, 72_499), (-1, 1)):
            with self.subTest(left=left, right=right):
                result = self.resolved([obs(1, 0, T0, left), obs(2, 0, T0, right, ORIGIN_B)])
                self.assertEqual((len(result.ties), result.contributions), (1, ()))
        for value in (72500.0, 0.1 + 0.2, 72, 0):
            with self.subTest(equal=value):
                result = self.resolved([obs(1, 0, T0, value), obs(2, 0, T0, value, ORIGIN_B)])
                self.assertEqual(len(result.groups), 1)
                self.assertEqual(num(result.groups[0].value), num(value))

    def test_negative_zero_equals_zero(self) -> None:
        for values in ((-0.0, 0.0), (0.0, -0.0), (-0.0, -0.0), (-0.0, 0.0, -0.0)):
            with self.subTest(values=values):
                result = self.resolved([obs(k + 1, 0, T0, v, (ORIGIN_A, ORIGIN_B)[k % 2]) for k, v in enumerate(values)])
                self.assertEqual(len(result.groups), 1)
                self.assertEqual(num(result.groups[0].value), ("float", "0.0"))
        # A single -0.0 contributes 0.0 (settles).
        single = self.resolved([obs(1, 0, T0, -0.0)])
        self.assertEqual(num(single.contributions[0].value), ("float", "0.0"))
        # In a tie, -0.0 and 0.0 are one class with the value 0.0.
        tie = self.resolved([obs(1, 0, T0, 1.0), obs(2, 0, T0, -0.0), obs(3, 0, T0, 0.0, ORIGIN_B),
                             obs(4, 0, T0, -1.0, ORIGIN_C)])
        self.assertEqual([num(k.value) for k in tie.ties[0].classes],
                         [("float", "-1.0"), ("float", "0.0"), ("float", "1.0")])
        self.assertEqual(tie.ties[0].classes[1].members, (ObservationRef(2, 0), ObservationRef(3, 0)))
        # The smallest subnormal is not zero.
        self.assertEqual(len(self.resolved([obs(1, 0, T0, -0.0), obs(2, 0, T0, 5e-324)]).ties), 1)

    def test_large_integers_compare_exactly(self) -> None:
        for left, right in ((2**53, 2**53 + 1), (2**64, 2**64 + 1), (10**30, 10**30 + 1), (-(2**70), -(2**70) - 1)):
            with self.subTest(left=left):
                tie = self.resolved([obs(1, 0, T0, left), obs(2, 0, T0, right, ORIGIN_B)])
                self.assertEqual(len(tie.ties), 1)
                self.assertEqual([k.value for k in tie.ties[0].classes], sorted((left, right)))
                group = self.resolved([obs(1, 0, T0, right), obs(2, 0, T0, right, ORIGIN_B)])
                self.assertEqual(num(group.groups[0].value), ("int", repr(right)))

    def test_provenance_is_not_compared(self) -> None:
        cases = {
            "two origins": [obs(1, 0, T0, 72, ORIGIN_A), obs(2, 0, T0, 72, ORIGIN_B)],
            "one origin, two records": [obs(1, 0, T0, 72), obs(2, 0, T0, 72)],
            "one record, two positions": [obs(1, 0, T0, 72), obs(1, 7, T0, 72)],
            "two kinds, one origin text": [obs(1, 0, T0, 72, ORIGIN_A, KIND_A), obs(2, 0, T0, 72, ORIGIN_A, KIND_B)],
            "everything differs": [obs(1, 0, T0, 72.5, ORIGIN_A, KIND_A), obs(9, 4, T0, 72.5, ORIGIN_C, KIND_B)],
        }
        for name, items in cases.items():
            with self.subTest(name):
                result = self.resolved(items)
                self.assertEqual(len(result.contributions), 1)
                self.assertTrue(result.contributions[0].is_group)
                self.assertEqual(len(result.contributions[0].members), 2)


# ---- R6 ----

class R6EveryObservationOnce(Checks):
    def test_every_observation_is_in_exactly_one_resolution(self) -> None:
        for number, items in enumerate(generated_sets()):
            with self.subTest(set=number):
                result = self.resolved(items)
                by_ref = {(item.record_id, item.index): item for item in items}
                placed = collections.Counter()
                for c in result.contributions:
                    for m in c.members:
                        placed[(m.record_id, m.index)] += 1
                        self.assertEqual(by_ref[(m.record_id, m.index)].instant_ns, c.instant_ns)
                        self.assertEqual(by_ref[(m.record_id, m.index)].value, c.value)
                for t in result.ties:
                    values = [k.value for k in t.classes]
                    self.assertEqual(len(set(values)), len(values))
                    for k in t.classes:
                        for m in k.members:
                            placed[(m.record_id, m.index)] += 1
                            self.assertEqual(by_ref[(m.record_id, m.index)].instant_ns, t.instant_ns)
                            self.assertEqual(by_ref[(m.record_id, m.index)].value, k.value)
                self.assertEqual(placed, collections.Counter(by_ref.keys()))
                contribution_instants = [c.instant_ns for c in result.contributions]
                tie_instants = [t.instant_ns for t in result.ties]
                self.assertEqual(contribution_instants, sorted(set(contribution_instants)))
                self.assertEqual(tie_instants, sorted(set(tie_instants)))
                self.assertFalse(set(contribution_instants) & set(tie_instants))
                self.assertEqual(set(contribution_instants) | set(tie_instants), {i.instant_ns for i in items})

    def test_single_observation_contributes_alone(self) -> None:
        result = self.resolved([obs(5, 3, T0, 72, ORIGIN_B, KIND_B)])
        self.assertEqual(result, Resolution(V1, RULE, CONFIDENCE,
                                            (Contribution(T0, 72, (ObservationRef(5, 3),), ((KIND_B, ORIGIN_B),)),),
                                            (), (), ()))
        c = result.contributions[0]
        self.assertEqual((c.record_ids, c.is_group, result.groups), ((5,), False, ()))
        apart = self.resolved([obs(k, 0, T0 + k, 72) for k in range(1, 6)])
        self.assertEqual([len(c.members) for c in apart.contributions], [1] * 5)
        self.assertEqual((apart.groups, apart.ties, apart.grouped_record_ids, apart.tied_record_ids), ((), (), (), ()))

    def test_result_matches_reference_model_on_generated_sets(self) -> None:
        sets = generated_sets() + [generated(500 + seed, 1500, "float" if seed % 2 else "int", instants=12, values=4)
                                   for seed in range(12)]
        # The generated sets are dense in shared instants: the model finds many groups and ties in them.
        expected = [model(items) for items in sets]
        self.assertGreater(sum(len(plain[5]) for plain in expected), 40)
        self.assertGreater(sum(len(plain[4]) for plain in expected), 40)
        self.assertGreater(sum(1 for items in sets for item in items if item.value == 0 and math.copysign(1, item.value) < 0), 10)
        for number, items in enumerate(sets):
            with self.subTest(set=number):
                self.resolved(items)


# ---- R7 ----

class R7DuplicateGroups(Checks):
    def test_equal_instant_and_value_form_one_group(self) -> None:
        result = self.resolved([obs(1, 0, T0, 72), obs(2, 0, T0, 72, ORIGIN_B), obs(3, 0, T0, 72, ORIGIN_C)])
        self.assertEqual(len(result.contributions), 1)
        group = result.contributions[0]
        self.assertEqual((group.instant_ns, group.value, group.is_group), (T0, 72, True))
        self.assertEqual(result.groups, result.contributions)
        self.assertEqual(result.grouped_record_ids, (1, 2, 3))

    def test_group_spans_origins_records_and_positions(self) -> None:
        items = [obs(1, 0, T0, 72.5, ORIGIN_A, KIND_A), obs(1, 4, T0, 72.5, ORIGIN_A, KIND_A),
                 obs(2, 0, T0, 72.5, ORIGIN_A, KIND_A), obs(3, 2, T0, 72.5, ORIGIN_B, KIND_B),
                 obs(4, 0, T0, 72.5, ORIGIN_C, KIND_A)]
        result = self.resolved(items)
        group = result.contributions[0]
        self.assertEqual(group.members, (ObservationRef(1, 0), ObservationRef(1, 4), ObservationRef(2, 0),
                                         ObservationRef(3, 2), ObservationRef(4, 0)))
        self.assertEqual(group.origins, ((KIND_A, ORIGIN_A), (KIND_A, ORIGIN_C), (KIND_B, ORIGIN_B)))
        self.assertEqual(group.record_ids, (1, 2, 3, 4))

    def test_group_contributes_once(self) -> None:
        items = [obs(k, 0, T0, 72, (ORIGIN_A, ORIGIN_B, ORIGIN_C)[k % 3]) for k in range(1, 5)] + \
                [obs(5, 0, T0 + 10**9, 80), obs(6, 0, T0 + 2 * 10**9, 90)]
        result = self.resolved(items)
        self.assertEqual([(c.instant_ns, c.value) for c in result.contributions],
                         [(T0, 72), (T0 + 10**9, 80), (T0 + 2 * 10**9, 90)])
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(len(result.groups[0].members), 4)
        self.assertEqual(result.ties, ())

    def test_group_has_no_representative(self) -> None:
        self.assertEqual([f.name for f in dataclasses.fields(Contribution)], ["instant_ns", "value", "members", "origins"])
        # Members in every order, with the lowest record in either origin, give the same group, which names every
        # member and origin, and whose value is the shared one rather than one member's spelling of it.
        left = [obs(1, 0, T0, -0.0, ORIGIN_B), obs(2, 0, T0, 0.0, ORIGIN_A), obs(3, 0, T0, -0.0, ORIGIN_C)]
        right = [obs(1, 0, T0, 0.0, ORIGIN_C), obs(2, 0, T0, -0.0, ORIGIN_B), obs(3, 0, T0, 0.0, ORIGIN_A)]
        for items in (left, right):
            for order in itertools.permutations(items):
                group = self.resolved(order).contributions[0]
                self.assertEqual(group.members, (ObservationRef(1, 0), ObservationRef(2, 0), ObservationRef(3, 0)))
                self.assertEqual(group.origins, ((KIND_A, ORIGIN_A), (KIND_A, ORIGIN_B), (KIND_A, ORIGIN_C)))
                self.assertEqual(num(group.value), ("float", "0.0"))
        for name in ("representative", "primary", "canonical", "leader", "chosen", "winner"):
            self.assertFalse(hasattr(Contribution, name), name)

    def test_group_members_are_sorted(self) -> None:
        items = [obs(100, 0, T0, 72), obs(10, 10, T0, 72), obs(9, 0, T0, 72), obs(10, 2, T0, 72), obs(2, 11, T0, 72),
                 obs(2, 3, T0, 72)]
        expected = (ObservationRef(2, 3), ObservationRef(2, 11), ObservationRef(9, 0), ObservationRef(10, 2),
                    ObservationRef(10, 10), ObservationRef(100, 0))
        for order in (items, list(reversed(items)), sorted(items, key=lambda o: str(o.record_id))):
            group = self.resolved(order).contributions[0]
            self.assertEqual(group.members, expected)
            self.assertEqual(group.record_ids, (2, 9, 10, 100))

    def test_group_lists_its_origins(self) -> None:
        items = [obs(1, 0, T0, 72, "z", "b-kind"), obs(2, 0, T0, 72, "a", "b-kind"), obs(3, 0, T0, 72, "Z", "a-kind"),
                 obs(4, 0, T0, 72, "z", "a-kind"), obs(5, 0, T0, 72, "é", "a-kind"), obs(6, 0, T0, 72, "z", "a-kind"),
                 obs(7, 0, T0, 72, "a", "b-kind")]
        group = self.resolved(items).contributions[0]
        # Distinct pairs, sorted by kind and then origin, by code point.
        self.assertEqual(group.origins, (("a-kind", "Z"), ("a-kind", "z"), ("a-kind", "é"), ("b-kind", "a"),
                                         ("b-kind", "z")))
        single = self.resolved([obs(1, 0, T0, 72, ORIGIN_C, KIND_B)]).contributions[0]
        self.assertEqual(single.origins, ((KIND_B, ORIGIN_C),))


# ---- R8 ----

class R8UnresolvedTies(Checks):
    def test_different_values_at_one_instant_are_a_tie(self) -> None:
        result = self.resolved([obs(1, 0, T0, 73), obs(2, 0, T0, 72, ORIGIN_B)])
        self.assertEqual(result.contributions, ())
        self.assertEqual(result.ties, (UnresolvedTie(T0, (TieClass(72, (ObservationRef(2, 0),)),
                                                          TieClass(73, (ObservationRef(1, 0),)))),))
        self.assertEqual((result.ties[0].record_ids, result.tied_record_ids, result.grouped_record_ids),
                         ((1, 2), (1, 2), ()))

    def test_tie_includes_equal_valued_members(self) -> None:
        for values in ((72, 72, 73), (72, 72, 72, 73), (73, 72, 72, 72, 72), (72.5, 72.5, 72.5, 72.25)):
            with self.subTest(values=values):
                items = [obs(k + 1, 0, T0, v, (ORIGIN_A, ORIGIN_B, ORIGIN_C)[k % 3]) for k, v in enumerate(values)]
                result = self.resolved(items)
                self.assertEqual((result.contributions, result.groups, result.grouped_record_ids), ((), (), ()))
                self.assertEqual(len(result.ties), 1)
                sizes = {k.value: len(k.members) for k in result.ties[0].classes}
                self.assertEqual(sizes, dict(collections.Counter(values)))

    def test_tie_contributes_nothing(self) -> None:
        items = [obs(1, 0, T0, 72), obs(2, 0, T0, 72, ORIGIN_B), obs(3, 0, T0, 73, ORIGIN_C),
                 obs(4, 0, T0 + 1, 80), obs(5, 0, T0 + 2, 90), obs(6, 0, T0 + 2, 90, ORIGIN_B)]
        result = self.resolved(items)
        self.assertEqual([(c.instant_ns, c.value) for c in result.contributions], [(T0 + 1, 80), (T0 + 2, 90)])
        contributing = {m for c in result.contributions for m in c.members}
        self.assertFalse(contributing & {ObservationRef(1, 0), ObservationRef(2, 0), ObservationRef(3, 0)})
        self.assertEqual(result.tied_record_ids, (1, 2, 3))
        self.assertEqual(result.grouped_record_ids, (5, 6))

    def test_tie_classes_are_sorted_by_value(self) -> None:
        for values in ((10, 9, 100, -1, 2, 10), (-0.5, 0.25, -10.0, 3.0, 25.0, 3.0)):
            with self.subTest(values=values):
                items = [obs(20 - k, (5, 12)[k % 2], T0, v) for k, v in enumerate(values)]
                tie = self.resolved(items).ties[0]
                self.assertEqual([k.value for k in tie.classes], sorted(set(values)))
                for k in tie.classes:
                    refs = [(m.record_id, m.index) for m in k.members]
                    self.assertEqual(refs, sorted(refs))
        tie = self.resolved([obs(10, 0, T0, 9), obs(9, 0, T0, 9), obs(100, 0, T0, 9), obs(2, 0, T0, 10)]).ties[0]
        self.assertEqual(tie.classes[0].members, (ObservationRef(9, 0), ObservationRef(10, 0), ObservationRef(100, 0)))


# ---- R9 ----

class R9ReversibleAndLocal(Checks):
    def test_removing_a_member_reverses_the_group(self) -> None:
        a, b = obs(1, 0, T0, 72), obs(2, 0, T0, 72, ORIGIN_B)
        other = obs(3, 0, T0 + 1, 60)
        self.assertEqual(len(self.resolved([a, b, other]).groups), 1)
        for kept in (a, b):
            result = self.resolved([kept, other])
            self.assertEqual(result.contributions[0].members, (ObservationRef(kept.record_id, kept.index),))
            self.assertEqual((result.groups, result.grouped_record_ids), ((), ()))
        c = obs(4, 0, T0, 72, ORIGIN_C)
        self.assertEqual(self.resolved([a, c, other]).groups[0].members, (ObservationRef(1, 0), ObservationRef(4, 0)))
        # A tie with one class left after removal becomes a contribution.
        d = obs(5, 0, T0, 73, ORIGIN_C)
        self.assertEqual(len(self.resolved([a, b, d, other]).ties), 1)
        result = self.resolved([a, b, other])
        self.assertEqual((len(result.groups), result.ties), (1, ()))
        result = self.resolved([d, other])
        self.assertEqual((result.contributions[0].value, result.ties), (73, ()))

    def test_changing_a_value_recomputes_the_instant(self) -> None:
        a, b = obs(1, 0, T0, 72), obs(2, 0, T0, 72, ORIGIN_B)
        others = [obs(3, 0, T0 + 1, 60), obs(4, 0, T0 + 1, 60, ORIGIN_B), obs(5, 0, T0 - 1, 61), obs(6, 0, T0 - 1, 62)]
        grouped = self.resolved([a, b] + others)
        tied = self.resolved([a, dataclasses.replace(b, value=73)] + others)
        again = self.resolved([dataclasses.replace(a, value=73), dataclasses.replace(b, value=73)] + others)
        self.assertEqual(len(at_instant(grouped, T0)), 1)
        self.assertIs(type(at_instant(grouped, T0)[0]), Contribution)
        self.assertIs(type(at_instant(tied, T0)[0]), UnresolvedTie)
        self.assertEqual(at_instant(again, T0)[0].value, 73)
        for result in (tied, again):
            for instant in (T0 + 1, T0 - 1):
                self.assertEqual(at_instant(result, instant), at_instant(grouped, instant))

    def test_other_instants_are_unaffected(self) -> None:
        for seed in range(10):
            with self.subTest(seed=seed):
                items = generated(700 + seed, 120, "int" if seed % 2 else "float", instants=8, values=3)
                before = plain_at(self.resolved(items))
                rng = random.Random(seed)
                target = rng.choice(items)
                values = INT_VALUES if seed % 2 else FLOAT_VALUES
                changes = (
                    [item for item in items if item is not target],
                    [dataclasses.replace(item, value=rng.choice(values)) if item is target else item for item in items],
                    items + [dataclasses.replace(target, record_id=10**6, index=0)],
                )
                for changed in changes:
                    after = plain_at(self.resolved(changed))
                    for instant in set(before) | set(after):
                        if instant != target.instant_ns:
                            self.assertEqual(after.get(instant), before.get(instant))

    def test_repeated_calls_share_no_state(self) -> None:
        first = [obs(1, 0, T0, 72), obs(2, 0, T0, 72, ORIGIN_B)]
        second = [obs(1, 0, T0, 73), obs(2, 0, T0, 72, ORIGIN_B)]
        third = [obs(1, 0, T0, 72), obs(2, 0, T0 + 1, 72, ORIGIN_B)]
        one = self.resolved(first)
        self.resolved(second)
        self.resolved(third)
        with self.assertRaises(DuplicateObservationFailure):
            resolve(first + [obs(1, 0, T0, 74)])
        self.assertEqual(self.resolved(first), one)
        self.resolved(list(reversed(second)))
        # Results after many calls in this process equal those of a fresh process.
        for items in generated_sets()[:10]:
            resolve(items)
        stdout, _ = run_python("import test_duplicate_observation_contract_1 as t; print(t.scenario_text())", {})
        self.assertEqual(stdout.strip().splitlines()[-1], scenario_text())


# ---- R10 ----

class R10RecordFacts(Checks):
    def test_record_facts_are_sorted_and_exact(self) -> None:
        items = [
            obs(100, 0, T0, 72), obs(9, 0, T0, 72, ORIGIN_B),          # group across records 9 and 100
            obs(10, 0, T0 + 1, 72), obs(5, 0, T0 + 1, 73, ORIGIN_B),  # tie: records 5 and 10
            obs(5, 1, T0 + 2, 60, ORIGIN_B), obs(2, 0, T0 + 2, 60),   # record 5 also in a group
            obs(7, 0, T0 + 3, 50), obs(7, 1, T0 + 4, 50),             # record 7: singles only
            obs(30, 0, T0 + 5, 55), obs(30, 1, T0 + 5, 55),           # group inside record 30
        ]
        result = self.resolved(items)
        self.assertEqual(result.grouped_record_ids, (2, 5, 9, 30, 100))
        self.assertEqual(result.tied_record_ids, (5, 10))
        self.assertNotIn(7, result.grouped_record_ids + result.tied_record_ids)

    def test_contribution_record_ids_show_whether_a_group_spans_records(self) -> None:
        within = self.resolved([obs(4, 0, T0, 72), obs(4, 1, T0, 72)]).contributions[0]
        self.assertEqual((within.is_group, within.record_ids), (True, (4,)))
        across = self.resolved([obs(30, 0, T0, 72), obs(4, 0, T0, 72, ORIGIN_B), obs(30, 1, T0, 72)]).contributions[0]
        self.assertEqual((across.is_group, across.record_ids), (True, (4, 30)))
        tie = self.resolved([obs(30, 0, T0, 72), obs(30, 1, T0, 73), obs(4, 0, T0, 72, ORIGIN_B)]).ties[0]
        self.assertEqual(tie.record_ids, (4, 30))
        tie = self.resolved([obs(8, 0, T0, 72), obs(8, 1, T0, 73)]).ties[0]
        self.assertEqual(tie.record_ids, (8,))

    def test_weight_shaped_result_builds_a_valid_summary(self) -> None:
        g = 72500.0
        scenarios = {
            "groups only": ([obs(1, 0, T0, g), obs(2, 0, T0, g, ORIGIN_B), obs(3, 0, T0 + 10**9, g + 100)],
                            "measured", ("duplicate-grouped",)),
            "groups and a tie": ([obs(1, 0, T0, g), obs(2, 0, T0, g, ORIGIN_B), obs(13, 0, T0 + 10**9, g),
                                  obs(12, 0, T0 + 10**9, 72499.99, ORIGIN_B), obs(4, 0, T0 + 2 * 10**9, g)],
                                 "partial", ("duplicate-grouped", "equal-time-conflict")),
            "only a tie": ([obs(1, 0, T0, g), obs(2, 0, T0, g - 1, ORIGIN_B)], "unresolved", ("equal-time-conflict",)),
            "singles": ([obs(1, 0, T0, g), obs(2, 0, T0 + 1, g, ORIGIN_B)], "measured", ()),
        }
        position = summary_contract.DeliveryPosition(
            "installation-a", 41, datetime(2026, 1, 15, 22, tzinfo=timezone.utc),
            datetime(2026, 1, 15, 22, 0, 3, tzinfo=timezone.utc))
        for name, (items, state, conditions) in scenarios.items():
            with self.subTest(name):
                result = self.resolved(items)
                # One observation per record: a record is in a group or a tie, not both.
                self.assertFalse(set(result.grouped_record_ids) & set(result.tied_record_ids))
                tied = set(result.tied_record_ids)
                entries = tuple(summary_contract.EvidenceEntry(
                    item.record_id, item.record_id + 500, item.record_id + 9000,
                    "unresolved" if item.record_id in tied else "contributing", "interpret-weight@1", "none")
                    for item in items)
                reported = set()
                if any(len(group.record_ids) >= 2 for group in result.groups):
                    reported.add("duplicate-grouped")
                if result.ties:
                    reported.add("equal-time-conflict")
                count = len(result.contributions)
                body = ('{"points":%d}' % count).encode() if count else None
                summary = summary_contract.build_summary(
                    measure="weight-series", record_type="weight", local_date=date(2026, 1, 15),
                    derivation_version="derive-weight-series@1", processor_version="interpret-weight@1",
                    source_selection_policy=None, evidence=entries, reported_conditions=reported,
                    positions=(position,), body=body)
                self.assertEqual(summary.header.primary_state, state)
                self.assertEqual(summary.header.evidence_conditions, conditions)
                self.assertEqual(summary.header.coverage.unresolved, len(tied))
        # A group inside one record has one contributing record, which cannot support duplicate-grouped (Decision 4).
        inside = self.resolved([obs(1, 0, T0, 72), obs(1, 1, T0, 72)])
        self.assertEqual(inside.groups[0].record_ids, (1,))
        with self.assertRaises(summary_contract.SummaryContractFailure) as caught:
            summary_contract.build_summary(
                measure="heart-rate-summary", record_type="heart-rate", local_date=date(2026, 1, 15),
                derivation_version="derive-heart-rate-summary@1", processor_version="interpret-heart-rate@1",
                source_selection_policy=None,
                evidence=(summary_contract.EvidenceEntry(1, 501, 9001, "contributing", "interpret-heart-rate@1", "none"),),
                reported_conditions=("duplicate-grouped",), positions=(position,), body=b'{"samples":1}')
        self.assertEqual(caught.exception.args, ("summary-state-inconsistent",))


# ---- R11 ----

class R11Scale(Checks):
    def test_large_input_is_resolved(self) -> None:
        items = scale_input()
        expected = model(items)
        timer = hasattr(signal, "setitimer") and hasattr(signal, "SIGALRM")
        if timer:
            def expire(signum, frame):
                raise ScaleTimeout(f"resolve ran past {SCALE_LIMIT_SECONDS} s")
            previous = signal.signal(signal.SIGALRM, expire)
            signal.setitimer(signal.ITIMER_REAL, SCALE_LIMIT_SECONDS)
        try:
            start = time.perf_counter()
            result = resolve(items)
            elapsed = time.perf_counter() - start
        finally:
            if timer:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous)
        self.assertLess(elapsed, SCALE_LIMIT_SECONDS)
        self.check_types(result)
        actual = shape(result)
        if actual != expected:
            # A full diff of 300,000 observations would take too long to print; name the first difference.
            self.fail(f"result differs from the model: {first_difference(actual, expected)}")
        self.assertGreater(len(result.contributions), 50_000)
        self.assertGreater(len(result.groups), 10_000)
        self.assertGreater(len(result.ties), 10_000)


# ---- R12 ----

class R12HealthDataFree(Checks):
    def captured(self, argument) -> DuplicateObservationFailure:
        # assertRaises clears the traceback, so the failure is caught by hand while its traceback is attached.
        try:
            resolve(argument)
        except DuplicateObservationFailure as failure:
            return failure
        self.fail("resolve did not raise DuplicateObservationFailure")

    def check_failure(self, failure: DuplicateObservationFailure, argument) -> None:
        self.assertIs(type(failure), DuplicateObservationFailure)
        self.assertEqual(failure.args, (INVALID,))
        self.assertIsNone(failure.__cause__)
        self.assertIsNone(failure.__context__)
        for text in (str(failure), repr(failure), repr(failure.args), repr(vars(failure)),
                     "".join(traceback.format_exception(failure))):
            self.assert_marker_free(text)
        # The caller's own frames hold its input; every frame after them belongs to the call.
        tb = failure.__traceback__
        while tb is not None and tb.tb_frame.f_code.co_filename == __file__:
            tb = tb.tb_next
        if tb is None:
            return
        rendered = "".join(traceback.TracebackException(type(failure), failure, tb, capture_locals=True).format())
        self.assert_marker_free(rendered)
        forbidden = input_objects(argument)
        while tb is not None:
            for name, value in tb.tb_frame.f_locals.items():
                self.assertFalse(held(value, forbidden, set()), f"{tb.tb_frame.f_code.co_name}: {name}")
            tb = tb.tb_next

    def test_failures_carry_only_their_name(self) -> None:
        self.resolved(marker_observations())
        for name, argument in failure_cases():
            with self.subTest(name, caller="none"):
                self.check_failure(self.captured(argument), argument)
            with self.subTest(name, caller="handling ValueError"):
                try:
                    raise ValueError(MARKER_ORIGIN, MARKER_INT, marker_observations())
                except ValueError:
                    failure = self.captured(argument)
                self.check_failure(failure, argument)
            with self.subTest(name, caller="handling JSONDecodeError"):
                try:
                    json.loads('{"origin": "%s", "value": %d,}' % (MARKER_ORIGIN, MARKER_INT))
                except json.JSONDecodeError:
                    failure = self.captured(argument)
                self.check_failure(failure, argument)

    def test_reprs_carry_no_instants_values_or_origins(self) -> None:
        ref = ObservationRef(1, 0)
        values = [
            Observation(1, 0, MARKER_KIND, MARKER_ORIGIN, MARKER_INSTANT, MARKER_INT),
            Observation(2, 3, MARKER_KIND, MARKER_ORIGIN, MARKER_INSTANT, MARKER_FLOAT),
            Contribution(MARKER_INSTANT, MARKER_INT, (ref, ObservationRef(2, 0)), ((MARKER_KIND, MARKER_ORIGIN),)),
            Contribution(MARKER_INSTANT, MARKER_FLOAT, (ref,), ((MARKER_KIND, MARKER_ORIGIN),)),
            TieClass(MARKER_INT, (ref,)),
            TieClass(MARKER_FLOAT, (ref,)),
            UnresolvedTie(MARKER_INSTANT, (TieClass(MARKER_INT, (ref,)), TieClass(MARKER_INT + 1, (ObservationRef(2, 0),)))),
        ]
        values.append(Resolution(V1, RULE, CONFIDENCE, (values[2], values[3]), (values[6],), (1, 2), (1, 2)))
        for value in values:
            with self.subTest(type(value).__name__):
                for text in (repr(value), str(value)):
                    self.assertNotIn(MARKER_KIND, text)
                    self.assertNotIn(MARKER_ORIGIN, text)
                    self.assertNotIn(str(MARKER_INSTANT), text)
                    self.assertNotIn(str(MARKER_INT), text)
                    self.assertNotIn(str(MARKER_INT + 1), text)
                    self.assertNotIn(repr(MARKER_FLOAT), text)


if __name__ == "__main__":
    unittest.main()
