"""Acceptance tests for `set-source-selection-policy@1`.

Requirement numbers refer to `packs/set-source-selection-policy-1.spec.md`.
Every database lives in a temporary directory and every value is synthetic.
Stores are built through the published `operational-store@5` API, and known
origins come from indexing synthetic Source Records. Every time comes from a
caller clock. Every no-write, refusal, and read-only assertion compares the
whole database, rowids included.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import random
import re
import sqlite3
import tempfile
import unittest
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from activity_tracker_portal.operational_store import (
    BatchItem,
    OperationalStore,
    IngestBatchRecord,
    ItemPosition,
    KnownOrigin,
    MessageHeader,
    OriginKey,
    ProjectionEntry,
    SourceSelectionPolicyRecord,
    StoreRefusal,
    StoreRetryable,
    open_store,
)
from activity_tracker_portal.set_source_selection_policy import (
    MEASURES,
    DateRange,
    SourceSelectionChoices,
    SourceSelectionPreview,
    SourceSelectionRefusal,
    SourceSelectionRequest,
    SourceSelectionResult,
    preview_source_selection,
    set_source_selection_policy,
    source_selection_choices,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/set-source-selection-policy.1.json").read_text(encoding="utf-8"))
EFFECTS = json.loads((ROOT / "packs/set-source-selection-policy-1.effects.json").read_text(encoding="utf-8"))
SCOPE_MAP = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
SCHEMAS = ROOT / "projection/v1/schemas"

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 11, 25, 9, 0, 0, 123456, tzinfo=UTC)
WHEN = datetime(2026, 11, 25, 22, 15, 0, 654321, tzinfo=IST)
NAIVE = datetime(2026, 11, 25, 22, 15)
RESERVE = 1_000_000
PLENTY = 10**12
INSTALLATION = "0f8fad5b-d9cb-469f-a165-70867728950e"
CYCLE = "e3b0c442-98fc-4c14-9afb-f4c8996fb924"
HC = "health-connect"
OTHER_KIND = "synthetic-other-kind"
IDENTITY_MARKER = "synthetic-identity-marker"
HEALTH_MARKER = "synthetic-health-marker"
ORIGIN_MARKER = "org.example.synthetic.origin-marker"
ORIGIN_A = "org.example.synthetic.source"
ORIGIN_B = "org.example.synthetic.other"
ORIGIN_SCALE = "org.example.synthetic.scale"
OA = OriginKey(HC, ORIGIN_A)
OB = OriginKey(HC, ORIGIN_B)
OSCALE = OriginKey(HC, ORIGIN_SCALE)
OTHER_KIND_A = OriginKey(OTHER_KIND, ORIGIN_A)
UNKNOWN = OriginKey(HC, ORIGIN_MARKER)
STEPS = ("daily-steps", "steps")
OWN_FAILURES = ("selection-request-invalid", "selection-measure-unknown", "selection-origin-unknown",
                "selection-changed")
EXPECTED_MEASURES = {
    "daily-steps": ("steps",),
    "heart-rate-summary": ("heart-rate",),
    "sleep-summary": ("sleep-session",),
    "weight-series": ("weight",),
}
# Source Records seeded in every store: (record type, origin, how many).
SEED = (("steps", OA, 2), ("steps", OB, 1), ("weight", OSCALE, 1), ("heart-rate", OA, 1), ("sleep-session", OB, 1))
D = date(2026, 1, 1)


def day(offset: int) -> date:
    return D + timedelta(days=offset)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request(origins=(OA,), first=None, last=None, measure="daily-steps", record_type="steps") -> SourceSelectionRequest:
    return SourceSelectionRequest(measure, record_type, origins, first, last)  # type: ignore[arg-type]


def without_rowid(connection: sqlite3.Connection) -> set[str]:
    """The main schema's WITHOUT ROWID tables, whatever tables a store version defines."""
    return {name for schema_name, name, _kind, _columns, flag, _strict in connection.execute("PRAGMA table_list")
            if schema_name == "main" and flag}


def table_rows(connection: sqlite3.Connection, table: str, columns: str = "*", rowless: set[str] | None = None):
    """Every row of one table. A rowid table's rows lead with their rowid, in rowid order, because the
    rowid is observable state. A WITHOUT ROWID table has no such identity, so its rows are compared as
    a sorted list."""
    if table in (without_rowid(connection) if rowless is None else rowless):
        rows = connection.execute(f'SELECT {columns} FROM "{table}"').fetchall()
        return sorted(rows, key=lambda row: [(value is None, type(value).__name__, 0 if value is None else value) for value in row])
    return connection.execute(f'SELECT rowid, {columns} FROM "{table}" ORDER BY rowid').fetchall()


def contents(path: Path) -> dict[str, object]:
    """The whole database: schema version, every schema object, and every row of every table, BLOBs and rowids included."""
    connection = sqlite3.connect(path)
    try:
        schema = connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        rowless = without_rowid(connection)
        tables: dict[str, list[tuple[object, ...]]] = {}
        for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
            tables[name] = table_rows(connection, name, rowless=rowless)
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()
    return {"user_version": user_version, "schema": schema, "tables": tables}


class Clock:
    """A caller clock that counts its reads and logs them in the shared event list."""

    def __init__(self, events: list[str] | None = None, value: datetime = WHEN) -> None:
        self.events = [] if events is None else events
        self.value = value
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        self.events.append("clock")
        return self.value


class FaultyView:
    """A read view whose every read raises the injected failure, whichever published read the unit uses."""

    def __init__(self, view, failure: BaseException) -> None:
        self._view = view
        self._failure = failure

    def __getattr__(self, name: str):
        getattr(self._view, name)  # Only published reads exist.

        def read(*_args, **_kwargs):
            raise self._failure

        return read


class FaultyRead:
    """A read-view context whose entry raises, or whose view's reads raise, the injected failure."""

    def __init__(self, context, failure: BaseException, where: str) -> None:
        self._context = context
        self._failure = failure
        self._where = where

    def __enter__(self):
        if self._where == "enter":
            raise self._failure
        return FaultyView(self._context.__enter__(), self._failure)

    def __exit__(self, *exc_info):
        if self._where == "enter":
            return False
        return self._context.__exit__(*exc_info)


class Spy:
    """A delegating store that logs reads and writes and can inject an interleaving or a failure at the write,
    or a failure at a read: on calling read(), on entering the view, or on any read in it."""

    def __init__(self, store, events: list[str], *, before_write: Callable[[], None] | None = None,
                 fail_with: BaseException | None = None, fail_after: BaseException | None = None,
                 read_fault: tuple[str, BaseException] | None = None) -> None:
        self._store = store
        self.events = events
        self.before_write = before_write
        self.fail_with = fail_with
        self.fail_after = fail_after
        self.read_fault = read_fault
        self.writes: list[dict[str, object]] = []

    def read(self):
        self.events.append("read")
        if self.read_fault is None:
            return self._store.read()
        where, failure = self.read_fault
        if where == "call":
            raise failure
        return FaultyRead(self._store.read(), failure, where)

    def record_source_selection_policy(self, *args, **kwargs):
        self.events.append("write")
        bound = inspect.signature(OperationalStore.record_source_selection_policy).bind(self._store, *args, **kwargs)
        self.writes.append({name: value for name, value in bound.arguments.items() if name != "self"})
        if self.before_write is not None:
            hook, self.before_write = self.before_write, None
            hook()
        if self.fail_with is not None:
            raise self.fail_with
        result = self._store.record_source_selection_policy(*args, **kwargs)
        if self.fail_after is not None:
            raise self.fail_after
        return result

    def __getattr__(self, name: str):
        self.events.append("other:" + name)
        return getattr(self._store, name)


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.free = PLENTY
        self.minute = 0
        self.stores = 0
        self.sequences: dict[int, int] = {}
        self.store, self.path = self.fresh()

    def fresh(self, *, busy_timeout_ms: int = 5000):
        """A new seeded store in its own file: (store, path)."""
        self.stores += 1
        path = Path(self.directory.name) / f"portal-{self.stores}.sqlite3"
        store = open_store(path, reserve_bytes=RESERVE, free_bytes=lambda _path: self.free,
                           busy_timeout_ms=busy_timeout_ms)
        self.addCleanup(store.close)
        offer = digest(b"synthetic-enrollment-code")
        store.record_enrollment_offer(offer, T0 + timedelta(minutes=30), T0)
        store.enroll_installation(offer, INSTALLATION, digest(b"synthetic-fingerprint"), T0)
        store.activate_installation(INSTALLATION, T0)
        store.honor_generation(1, b'{"synthetic-advertisement":1}', T0)
        for record_type, origin, count in SEED:
            self.index(store, record_type, origin, count)
        return store, path

    def index(self, store, record_type: str, origin: OriginKey, count: int) -> None:
        """Archive one synthetic batch of count projections and index each as a Source Record of origin."""
        sequence = self.sequences[id(store)] = self.sequences.get(id(store), 0) + 1
        items = tuple(BatchItem(projection=b'{"synthetic-projection":"%d-%d","marker":"%s"}'
                                % (sequence, index, HEALTH_MARKER.encode())) for index in range(count))
        body = json.dumps({"synthetic": True, "sequence": sequence}, sort_keys=True).encode()
        when = T0 + timedelta(minutes=sequence)
        store.archive_ingest_batch(
            MessageHeader(INSTALLATION, sequence, digest(body), 1, when, body, b'{"synthetic-receipt":%d}' % sequence),
            IngestBatchRecord(record_type, 1, "initial-snapshot", CYCLE, sequence, 1,  # type: ignore[arg-type]
                              digest(b"synthetic-chain-%d" % sequence), items),
        )
        store.index_items(tuple(
            ProjectionEntry(ItemPosition(INSTALLATION, sequence, index), origin.source_system_kind, origin.origin,
                            f"{IDENTITY_MARKER}-{sequence}-{index}", when)
            for index in range(count)), when)

    def tick(self) -> datetime:
        self.minute += 1
        return T0 + timedelta(hours=1, minutes=self.minute)

    def live(self, origins=(OA,), first=None, last=None, supersedes=(), measure="daily-steps", record_type="steps",
             store=None) -> SourceSelectionPolicyRecord:
        """A policy recorded directly through the store, as an earlier request would have left it."""
        return (store or self.store).record_source_selection_policy(
            measure, record_type, tuple(origins), first, last, tuple(supersedes), self.tick())

    def timeline(self, store=None, measure="daily-steps", record_type="steps"):
        with (store or self.store).read() as view:
            return view.source_selection_timeline(measure, record_type)

    def spy(self, **kwargs) -> tuple[Spy, list[str]]:
        events: list[str] = []
        return Spy(self.store, events, **kwargs), events

    def assert_refused(self, failure: str, call: Callable[[], object]) -> SourceSelectionRefusal:
        """call is refused with failure, carries only the name, and leaves the whole database unchanged."""
        before = contents(self.path)
        with self.assertRaises(SourceSelectionRefusal) as caught:
            call()
        refusal = caught.exception
        self.assertIs(type(refusal), SourceSelectionRefusal)
        self.assertEqual(refusal.args, (failure,))
        self.assertEqual(refusal.failure, failure)
        self.assertEqual(str(refusal), failure)
        self.assertIsNone(refusal.__cause__)
        self.assertTrue(refusal.__context__ is None or refusal.__suppress_context__)
        self.assertEqual(contents(self.path), before)
        return refusal

    def assert_set_refused(self, failure: str, req, confirmed=()) -> SourceSelectionRefusal:
        spy, events = self.spy()
        clock = Clock(events)
        refusal = self.assert_refused(failure, lambda: set_source_selection_policy(spy, req, confirmed, clock))
        self.assertEqual(clock.calls, 0)
        self.assertNotIn("write", events)
        return refusal


PLAIN = (str, int, bool, date, datetime, type(None))


def instance_state(test: unittest.TestCase, value: object, path: str = "result") -> None:
    """A returned value holds only its declared fields, recursively: no extra instance state anywhere."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        declared = [field.name for field in dataclasses.fields(value)]
        state = set(getattr(value, "__dict__", {}))
        for klass in type(value).__mro__:
            slots = getattr(klass, "__slots__", ())
            state |= {name for name in ((slots,) if isinstance(slots, str) else slots)
                      if name not in ("__dict__", "__weakref__") and hasattr(value, name)}
        test.assertEqual(sorted(state), sorted(declared), path)
        for name in declared:
            instance_state(test, getattr(value, name), f"{path}.{name}")
    elif type(value) is tuple:
        for index, item in enumerate(value):
            instance_state(test, item, f"{path}[{index}]")
    else:
        test.assertIn(type(value), PLAIN, path)


def interval(first: date | None, last: date | None, window: int) -> set[int]:
    """A date interval as indices into a window of days 0..window-1, with -1 for every earlier date
    and window for every later one."""
    low = -1 if first is None else (first - D).days
    high = window if last is None else (last - D).days
    return set(range(low, high + 1))


def runs(indices: set[int], window: int) -> tuple[DateRange, ...]:
    """Maximal runs of indices as DateRanges; the sentinels map back to open ends."""
    result: list[DateRange] = []
    for index in sorted(indices):
        if result and index - 1 in indices:
            result[-1] = DateRange(result[-1].first_date, None if index == window else day(index))
        else:
            result.append(DateRange(None if index == -1 else day(index), None if index == window else day(index)))
    return tuple(result)


def oracle(live: list[SourceSelectionPolicyRecord], req: SourceSelectionRequest, window: int):
    """The approved semantics by brute force: (repeat_of, supersedes, uncovered)."""
    origins = tuple(sorted(req.origins, key=lambda origin: (origin.source_system_kind, origin.origin)))
    for policy in live:
        if (policy.origins, policy.first_date, policy.last_date) == (origins, req.first_date, req.last_date):
            return policy.policy_id, (), ()
    wanted = interval(req.first_date, req.last_date, window)
    replaced = [policy for policy in live if interval(policy.first_date, policy.last_date, window) & wanted]
    covered = set().union(*(interval(policy.first_date, policy.last_date, window) for policy in replaced))
    return None, tuple(sorted(policy.policy_id for policy in replaced)), runs(covered - wanted, window)


def random_timeline(rng: random.Random, window: int) -> list[tuple[tuple[OriginKey, ...], date | None, date | None]]:
    """Disjoint policies over the window: cut it at random boundaries and fill each piece or leave a gap.
    The first piece is open at the start and the last open at the end."""
    cuts = sorted(rng.sample(range(1, window), rng.randint(0, window - 1)))
    bounds = [None, *cuts, None]
    pieces = []
    for low, high in zip(bounds, bounds[1:]):
        if rng.random() < 0.65:
            pieces.append((rng.choice(((OA,), (OB,), (OA, OB))),
                           None if low is None else day(low), None if high is None else day(high - 1)))
    return pieces


def all_ranges(window: int) -> list[tuple[date | None, date | None]]:
    firsts = [None, *map(day, range(window))]
    lasts = [*map(day, range(window)), None]
    return [(first, last) for first in firsts for last in lasts
            if first is None or last is None or first <= last]


# ---------------------------------------------------------------------------
# R1 — Governed Scope contract
# ---------------------------------------------------------------------------


class R1GovernedScopeContract(unittest.TestCase):
    def test_contract_shape_and_dependency(self) -> None:
        self.assertEqual((CONTRACT["scope"], CONTRACT["version"], CONTRACT["kind"]),
                         ("set-source-selection-policy", "1", "leaf"))
        self.assertEqual(CONTRACT["dependencies"], [{"scope": "operational-store", "version": "5"}])
        self.assertEqual([(item["name"], item["outcome"]) for item in CONTRACT["named_failures"]], [
            *((name, "invariant-refusal") for name in OWN_FAILURES),
            ("timestamp-invalid", "invariant-refusal"),
            ("store-busy", "retryable"),
            ("storage-reserve-exhausted", "retryable"),
        ])
        self.assertEqual([effect["name"] for effect in CONTRACT["observable_effects"]],
                         ["source-selection-policy-write"])
        oracle_ = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle_["specification"], "packs/set-source-selection-policy-1.spec.md")
        self.assertEqual(oracle_["executable_tests"], {"python": "tests/acceptance/test_set_source_selection_policy_1.py"})
        self.assertEqual(oracle_["runner"], "tests/acceptance/run_set_source_selection_policy_1.sh")

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        row = next(line for line in SCOPE_MAP.splitlines() if line.startswith("| `set-source-selection-policy` |"))
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        self.assertEqual(cells[1], "leaf")
        listed = set(re.findall(r"`([a-z0-9-]+)`", cells[-1]))
        self.assertTrue(listed)
        self.assertLessEqual(listed, {item["scope"] for item in CONTRACT["dependencies"]})

    def test_effects_declaration_matches_contract(self) -> None:
        self.assertEqual(EFFECTS["effects_format"], 1)
        self.assertEqual([(item["kind"], item["name"], item["outcome"]) for item in EFFECTS["effects"]],
                         [("action", "source-selection-policy-write", "confirmed")])
        self.assertEqual([item["name"] for item in EFFECTS["effects"]],
                         [item["name"] for item in CONTRACT["observable_effects"]])
        self.assertIn("operational-store@5", EFFECTS["effects"][0]["system"])


# ---------------------------------------------------------------------------
# R2 — Measure vocabulary
# ---------------------------------------------------------------------------


class R2MeasureVocabulary(StoreCase):
    def test_measures_are_the_four_summaries(self) -> None:
        self.assertEqual(dict(MEASURES), EXPECTED_MEASURES)
        for record_types in MEASURES.values():
            self.assertIs(type(record_types), tuple)

    def test_measures_meet_the_store_rule(self) -> None:
        for measure, record_types in MEASURES.items():
            self.assertIsInstance(measure, str)
            self.assertLessEqual(len(measure), 64)
            self.assertRegex(measure, r"\A[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
            for record_type in record_types:
                origin = next(origin for kind, origin, _count in SEED if kind == record_type)
                with self.subTest(measure=measure, record_type=record_type):
                    # The published store accepts the pair, so no measure is refused downstream.
                    policy = self.live((origin,), measure=measure, record_type=record_type)
                    self.assertEqual((policy.measure, policy.record_type), (measure, record_type))

    def test_measure_record_types_are_projection_types(self) -> None:
        for record_types in MEASURES.values():
            self.assertTrue(record_types)
            for record_type in record_types:
                self.assertTrue((SCHEMAS / f"{record_type}.schema.json").is_file(), record_type)

    def test_measures_are_read_only(self) -> None:
        with self.assertRaises(TypeError):
            MEASURES["daily-distance"] = ("distance",)  # type: ignore[index]
        with self.assertRaises(TypeError):
            del MEASURES["daily-steps"]  # type: ignore[attr-defined]
        self.assertEqual(dict(MEASURES), EXPECTED_MEASURES)


# ---------------------------------------------------------------------------
# R3 — Choices
# ---------------------------------------------------------------------------


class R3Choices(StoreCase):
    def test_choices_list_known_origins_of_the_record_type(self) -> None:
        choices = source_selection_choices(self.store, *STEPS)
        self.assertIs(type(choices), SourceSelectionChoices)
        self.assertEqual((choices.measure, choices.record_type), STEPS)
        self.assertEqual(choices.origins, (KnownOrigin(HC, "steps", ORIGIN_B, 1), KnownOrigin(HC, "steps", ORIGIN_A, 2)))
        with self.store.read() as view:
            self.assertEqual(choices.origins, view.known_origins("steps"))
        self.assertEqual(source_selection_choices(self.store, "weight-series", "weight").origins,
                         (KnownOrigin(HC, "weight", ORIGIN_SCALE, 1),))
        # Origins indexed later appear, and counts follow the Source Records.
        self.index(self.store, "steps", OTHER_KIND_A, 3)
        self.assertEqual(source_selection_choices(self.store, *STEPS).origins, (
            KnownOrigin(HC, "steps", ORIGIN_B, 1), KnownOrigin(HC, "steps", ORIGIN_A, 2),
            KnownOrigin(OTHER_KIND, "steps", ORIGIN_A, 3)))
        self.assertEqual(source_selection_choices(self.store, *STEPS).timeline, ())

    def test_choices_list_the_live_timeline(self) -> None:
        late = self.live((OA,), day(10), None)
        early = self.live((OB,), None, day(2))
        replaced = self.live((OA, OB), day(3), day(9))
        middle = self.live((OB,), day(3), day(9), supersedes=(replaced.policy_id,))
        self.live((OSCALE,), measure="weight-series", record_type="weight")
        self.live((OA,), measure="heart-rate-summary", record_type="heart-rate")
        choices = source_selection_choices(self.store, *STEPS)
        with self.store.read() as view:
            early, middle, late = (view.policy(policy.policy_id) for policy in (early, middle, late))
            self.assertEqual(choices.timeline, view.source_selection_timeline(*STEPS))
        self.assertEqual(choices.timeline, (early, middle, late))
        weight = source_selection_choices(self.store, "weight-series", "weight")
        self.assertEqual([policy.origins for policy in weight.timeline], [(OSCALE,)])

    def test_choices_write_nothing(self) -> None:
        self.live((OA,), None, day(3))
        before = contents(self.path)
        spy, events = self.spy()
        for measure, record_types in MEASURES.items():
            for record_type in record_types:
                source_selection_choices(spy, measure, record_type)
        self.assertEqual(contents(self.path), before)
        self.assertEqual(events, ["read"] * 4)

    def test_choices_refuse_unknown_measures(self) -> None:
        for measure, record_type in (("daily-distance", "steps"), ("Daily-Steps", "steps"), ("daily-steps", "weight"),
                                     ("weight-series", "steps"), ("daily-steps", ""), ("", "steps"),
                                     ("daily-steps", "Steps")):
            with self.subTest(measure=measure, record_type=record_type):
                self.assert_refused("selection-measure-unknown",
                                    lambda: source_selection_choices(self.store, measure, record_type))
        for measure, record_type in ((None, "steps"), (5, "steps"), ("daily-steps", None), ("daily-steps", ("steps",))):
            with self.subTest(measure=measure, record_type=record_type):
                self.assert_refused("selection-request-invalid",
                                    lambda: source_selection_choices(self.store, measure, record_type))


# ---------------------------------------------------------------------------
# R4 — Request validation
# ---------------------------------------------------------------------------

MALFORMED = [
    request(measure=None), request(measure=5), request(record_type=None), request(record_type=5),
    request(origins=()), request(origins=[OA]), request(origins=None), request(origins=OA),
    request(origins=((HC, ORIGIN_A),)), request(origins=(ORIGIN_A,)), request(origins=(OA, OA)),
    request(origins=(OA, OB, OA)), request(origins=(OriginKey(5, ORIGIN_A),)),  # type: ignore[arg-type]
    request(origins=(OriginKey(HC, None),)),  # type: ignore[arg-type]
    request(first=datetime(2026, 1, 1, tzinfo=UTC)), request(last=datetime(2026, 1, 1)),
    request(first=datetime(2026, 1, 1), last=datetime(2026, 1, 2)), request(first="2026-01-01"),
    request(last="2026-01-01"), request(first=20260101), request(first=day(2), last=day(1)),
    request(first=date.max, last=date.min),
]
BAD_CONFIRMATIONS = ([], [1], {1}, None, 1, (0,), (-1,), ("1",), (1.0,), (1, 1), (True,), ((1,),))


class R4RequestValidation(StoreCase):
    def test_malformed_requests_are_refused(self) -> None:
        self.live((OA,), None, None)
        for index, req in enumerate(MALFORMED):
            with self.subTest(index=index, request=req):
                self.assert_refused("selection-request-invalid", lambda: preview_source_selection(self.store, req))
                self.assert_set_refused("selection-request-invalid", req)
        valid_new = request((OB,), day(1), day(1))
        repeat = request((OA,))
        for confirmed in BAD_CONFIRMATIONS:
            with self.subTest(confirmed=confirmed):
                self.assert_set_refused("selection-request-invalid", valid_new, confirmed)
                # The confirmation is checked first, even for a repeat.
                self.assert_set_refused("selection-request-invalid", repeat, confirmed)

    def test_unknown_measure_or_pair_is_refused(self) -> None:
        for measure, record_type in (("daily-distance", "steps"), ("Daily-Steps", "steps"), ("daily-step", "steps"),
                                     ("daily-steps", "weight"), ("weight-series", "steps"),
                                     ("sleep-summary", "sleep"), ("daily-steps", ""), ("", "steps")):
            req = request(measure=measure, record_type=record_type)
            with self.subTest(measure=measure, record_type=record_type):
                self.assert_refused("selection-measure-unknown", lambda: preview_source_selection(self.store, req))
                self.assert_set_refused("selection-measure-unknown", req)

    def test_dates_are_inclusive_and_may_be_open(self) -> None:
        # Each case replaces what it overlaps, so the last ones leave the timeline checked below.
        cases = [
            (None, None), (None, day(0)), (day(2), day(2)), (day(3), day(5)), (day(6), date(2099, 12, 31)),
            (date(2100, 1, 1), None),
        ]
        for first, last in cases:
            result = set_source_selection_policy(self.store, request((OA,), first, last),
                                                 preview_source_selection(self.store, request((OA,), first, last)).supersedes,
                                                 Clock())
            self.assertEqual((result.policy.first_date, result.policy.last_date), (first, last))
        # The earliest and latest dates Python holds are kept as given, not made open (Decision 5).
        other = request((OA,), date.min, date.max, "weight-series", "weight")
        self.assert_set_refused("selection-origin-unknown", other)
        limits = set_source_selection_policy(self.store, request((OSCALE,), date.min, date.max, "weight-series", "weight"),
                                             (), Clock())
        self.assertEqual((limits.policy.first_date, limits.policy.last_date), (date.min, date.max))
        with self.store.read() as view:
            for on in (day(-5), day(0), day(2), day(3), day(5), day(6), date(2099, 12, 31), date(2100, 1, 1), date.max):
                self.assertEqual(view.applicable_source_selection(*STEPS, on).origins, (OA,))
            self.assertIsNone(view.applicable_source_selection(*STEPS, day(1)))
            self.assertEqual(len(view.source_selection_timeline(*STEPS)), 5)

    def test_origin_order_does_not_matter(self) -> None:
        preview = preview_source_selection(self.store, request((OA, OB), day(1), day(2)))
        self.assertEqual(preview.request, request((OB, OA), day(1), day(2)))
        result = set_source_selection_policy(self.store, request((OA, OB), day(1), day(2)), (), Clock())
        self.assertEqual(result.policy.origins, (OB, OA))
        self.assertEqual(preview_source_selection(self.store, request((OB, OA), day(1), day(2))).repeat_of,
                         result.policy.policy_id)
        before = contents(self.path)
        again = set_source_selection_policy(self.store, request((OB, OA), day(1), day(2)), (), Clock())
        self.assertEqual((again.policy, again.recorded), (result.policy, False))
        self.assertEqual(contents(self.path), before)


# ---------------------------------------------------------------------------
# R5 — Origins must be known
# ---------------------------------------------------------------------------


class R5OriginsMustBeKnown(StoreCase):
    def test_unknown_origin_is_refused_before_writing(self) -> None:
        live = self.live((OA,))
        for origins in ((UNKNOWN,), (OA, UNKNOWN), (UNKNOWN, OB), (OriginKey(HC, ORIGIN_A.upper()),),
                        (OriginKey(HC, ""),), (OriginKey("", ORIGIN_A),)):
            req = request(origins, day(1), day(1))
            with self.subTest(origins=origins):
                self.assert_refused("selection-origin-unknown", lambda: preview_source_selection(self.store, req))
                self.assert_set_refused("selection-origin-unknown", req, (live.policy_id,))
                # Origins are checked before the confirmation (R7 step 1 before step 3).
                self.assert_set_refused("selection-origin-unknown", req, ())

    def test_origin_of_another_record_type_is_unknown(self) -> None:
        for req in (request((OSCALE,)), request((OTHER_KIND_A,)), request((OA,), measure="weight-series", record_type="weight"),
                    request((OA,), measure="sleep-summary", record_type="sleep-session")):
            with self.subTest(request=req):
                self.assert_refused("selection-origin-unknown", lambda: preview_source_selection(self.store, req))
                self.assert_set_refused("selection-origin-unknown", req)
        # Once that kind is indexed for steps, it is known.
        self.index(self.store, "steps", OTHER_KIND_A, 1)
        self.assertTrue(set_source_selection_policy(self.store, request((OTHER_KIND_A,)), (), Clock()).recorded)

    def test_single_and_combined_origins_are_accepted(self) -> None:
        one = set_source_selection_policy(self.store, request((OB,), None, day(4)), (), Clock())
        both = set_source_selection_policy(self.store, request((OA, OB), day(5), None), (), Clock())
        sleep = set_source_selection_policy(self.store, request((OB,), measure="sleep-summary",
                                                                record_type="sleep-session"), (), Clock())
        self.assertEqual((one.policy.origins, both.policy.origins, sleep.policy.origins), ((OB,), (OB, OA), (OB,)))
        self.assertTrue(one.recorded and both.recorded and sleep.recorded)
        self.assertEqual(self.timeline(), (one.policy, both.policy))

    def test_every_measure_takes_single_and_combined_origins(self) -> None:
        # A second origin for each record type, so every measure has a combination to choose.
        second = {"steps": None, "weight": OA, "heart-rate": OB, "sleep-session": OA}
        for record_type, origin in second.items():
            if origin is not None:
                self.index(self.store, record_type, origin, 1)
        for measure, (record_type,) in MEASURES.items():
            first_origin = next(origin for kind, origin, _count in SEED if kind == record_type)
            other = second[record_type] or OB
            both = tuple(sorted((first_origin, other), key=lambda origin: (origin.source_system_kind, origin.origin)))
            with self.subTest(measure=measure):
                choices = source_selection_choices(self.store, measure, record_type)
                self.assertEqual({(origin.source_system_kind, origin.origin) for origin in choices.origins},
                                 {(first_origin.source_system_kind, first_origin.origin),
                                  (other.source_system_kind, other.origin)})
                self.assertEqual(choices.timeline, ())
                single = request((first_origin,), None, day(4), measure, record_type)
                preview = preview_source_selection(self.store, single)
                self.assertEqual((preview.repeat_of, preview.supersedes, preview.uncovered), (None, (), ()))
                recorded = set_source_selection_policy(self.store, single, preview.supersedes, Clock())
                self.assertTrue(recorded.recorded)
                self.assertEqual((recorded.policy.measure, recorded.policy.record_type, recorded.policy.origins,
                                  recorded.policy.first_date, recorded.policy.last_date),
                                 (measure, record_type, (first_origin,), None, day(4)))
                before = contents(self.path)
                again = set_source_selection_policy(self.store, single, (), Clock())
                self.assertEqual((again.policy, again.recorded, again.uncovered), (recorded.policy, False, ()))
                self.assertEqual(contents(self.path), before)
                combined = request(tuple(reversed(both)), day(5), None, measure, record_type)
                preview = preview_source_selection(self.store, combined)
                self.assertEqual((preview.request.origins, preview.supersedes), (both, ()))
                joined = set_source_selection_policy(self.store, combined, (), Clock())
                self.assertTrue(joined.recorded)
                self.assertEqual(joined.policy.origins, both)
                repeat = set_source_selection_policy(self.store, request(both, day(5), None, measure, record_type),
                                                     (), Clock())
                self.assertEqual((repeat.policy, repeat.recorded), (joined.policy, False))
                # A combination over every date replaces both, and its repeat writes nothing.
                everything = request(both, None, None, measure, record_type)
                preview = preview_source_selection(self.store, everything)
                self.assertEqual(preview.supersedes, (recorded.policy.policy_id, joined.policy.policy_id))
                replaced = set_source_selection_policy(self.store, everything, preview.supersedes, Clock())
                self.assertEqual((replaced.recorded, replaced.policy.supersedes, replaced.uncovered),
                                 (True, preview.supersedes, ()))
                self.assertEqual(source_selection_choices(self.store, measure, record_type).timeline,
                                 (replaced.policy,))
                before = contents(self.path)
                self.assertFalse(set_source_selection_policy(self.store, everything, (), Clock()).recorded)
                self.assertEqual(contents(self.path), before)
                for value in (choices, preview, recorded, again, joined, replaced):
                    instance_state(self, value)


# ---------------------------------------------------------------------------
# R6 — Preview
# ---------------------------------------------------------------------------

# (live policies as (first, last), request (first, last), superseded live indices, uncovered (first, last) pairs)
TABLE = [
    # No live policy.
    ([], (None, None), [], []),
    # Open ends.
    ([(None, None)], (None, None), [0], []),
    ([(None, None)], (day(3), None), [0], [(None, day(2))]),
    ([(None, None)], (None, day(3)), [0], [(day(4), None)]),
    ([(None, None)], (day(3), day(3)), [0], [(None, day(2)), (day(4), None)]),
    ([(None, None)], (day(3), day(5)), [0], [(None, day(2)), (day(6), None)]),
    ([(None, day(3))], (day(3), None), [0], [(None, day(2))]),
    ([(None, day(3))], (day(4), None), [], []),
    ([(day(3), None)], (None, day(2)), [], []),
    ([(day(3), None)], (None, day(3)), [0], [(day(4), None)]),
    ([(day(3), None)], (None, None), [0], []),
    # Single days.
    ([(day(3), day(3))], (day(3), day(3)), [0], []),
    ([(day(3), day(3))], (day(2), day(2)), [], []),
    ([(day(3), day(3))], (day(4), day(4)), [], []),
    ([(day(3), day(3))], (day(2), day(4)), [0], []),
    ([(day(2), day(4))], (day(3), day(3)), [0], [(day(2), day(2)), (day(4), day(4))]),
    ([(day(2), day(4))], (day(2), day(2)), [0], [(day(3), day(4))]),
    ([(day(2), day(4))], (day(4), day(4)), [0], [(day(2), day(3))]),
    # Nested both ways.
    ([(day(1), day(8))], (day(3), day(5)), [0], [(day(1), day(2)), (day(6), day(8))]),
    ([(day(3), day(5))], (day(1), day(8)), [0], []),
    ([(day(3), day(5))], (day(3), day(5)), [0], []),
    # Adjacent policies: touching is not overlapping.
    ([(None, day(2)), (day(3), day(5)), (day(6), None)], (day(3), day(5)), [1], []),
    ([(None, day(2)), (day(3), day(5)), (day(6), None)], (day(2), day(5)), [0, 1], [(None, day(1))]),
    ([(None, day(2)), (day(3), day(5)), (day(6), None)], (day(3), day(6)), [1, 2], [(day(7), None)]),
    ([(None, day(2)), (day(3), day(5)), (day(6), None)], (day(2), day(6)), [0, 1, 2], [(None, day(1)), (day(7), None)]),
    ([(None, day(2)), (day(3), day(5)), (day(6), None)], (day(4), day(4)), [1], [(day(3), day(3)), (day(5), day(5))]),
    ([(None, day(2)), (day(6), None)], (day(3), day(5)), [], []),
    ([(day(0), day(0)), (day(1), day(1)), (day(2), day(2))], (day(1), day(1)), [1], []),
    ([(day(0), day(0)), (day(1), day(1)), (day(2), day(2))], (day(0), day(1)), [0, 1], []),
    # Gaps between policies are not reported: only dates the request takes from a replaced policy.
    ([(day(0), day(1)), (day(5), day(6))], (day(1), day(5)), [0, 1], [(day(0), day(0)), (day(6), day(6))]),
    ([(day(0), day(1)), (day(5), day(6))], (None, None), [0, 1], []),
    # The limits of Python dates leave nothing before or after them (Decision 5).
    ([(None, None)], (date.min, date.max), [0], []),
    ([(None, None)], (date.min, day(3)), [0], [(day(4), None)]),
    ([(None, None)], (day(3), date.max), [0], [(None, day(2))]),
    ([(None, None)], (date(1, 1, 2), date(9999, 12, 30)), [0], [(None, date.min), (date.max, None)]),
    ([(date.min, date.max)], (None, None), [0], []),
    ([(date.min, date.max)], (day(3), day(3)), [0], [(date.min, day(2)), (day(4), date.max)]),
]


class R6Preview(StoreCase):
    def check_table(self, mode: str) -> None:
        for number, (live, (first, last), superseded, uncovered) in enumerate(TABLE):
            with self.subTest(case=number):
                store, path = self.fresh()
                policies = [self.live((OA,), low, high, store=store) for low, high in live]
                req = request((OB,), first, last)
                expected_supersedes = tuple(policies[index].policy_id for index in superseded)
                expected_uncovered = tuple(DateRange(low, high) for low, high in uncovered)
                before = contents(path)
                preview = preview_source_selection(store, req)
                self.assertEqual(contents(path), before)
                self.assertIs(type(preview), SourceSelectionPreview)
                self.assertEqual((preview.request, preview.repeat_of, preview.supersedes, preview.uncovered),
                                 (req, None, expected_supersedes, expected_uncovered))
                for item in preview.uncovered:
                    self.assertIs(type(item), DateRange)
                if mode == "set":
                    result = set_source_selection_policy(store, req, expected_supersedes, Clock())
                    self.assertEqual((result.recorded, result.uncovered, result.policy.supersedes),
                                     (True, expected_uncovered, expected_supersedes))

    def test_preview_supersedes_every_overlapping_live_policy(self) -> None:
        self.check_table("preview")
        # Policies of other measures and record types are never listed, whatever their dates.
        self.live((OA,), measure="heart-rate-summary", record_type="heart-rate")
        self.live((OSCALE,), measure="weight-series", record_type="weight")
        self.live((OB,), measure="sleep-summary", record_type="sleep-session")
        own = self.live((OA,), day(2), day(4))
        preview = preview_source_selection(self.store, request((OB,)))
        self.assertEqual((preview.supersedes, preview.uncovered), ((own.policy_id,), ()))
        heart = preview_source_selection(self.store, request((OA,), day(3), day(3), "heart-rate-summary", "heart-rate"))
        self.assertEqual(len(heart.supersedes), 1)
        self.assertNotEqual(heart.supersedes, (own.policy_id,))
        # Superseded policies are history, not live, and are never superseded again.
        newer = self.live((OB,), day(2), day(4), supersedes=(own.policy_id,))
        self.assertEqual(preview_source_selection(self.store, request((OA,))).supersedes, (newer.policy_id,))

    def test_preview_reports_uncovered_dates(self) -> None:
        self.check_table("set")
        # Exhaustively: random disjoint timelines over a window, against every request range.
        rng = random.Random(20261125)
        window = 6
        ranges = all_ranges(window)
        for trial in range(24):
            store, path = self.fresh()
            live = [self.live(origins, first, last, store=store) for origins, first, last in random_timeline(rng, window)]
            before = contents(path)
            for first, last in ranges:
                for origins in ((OA,), (OB, OA)):
                    req = request(origins, first, last)
                    with self.subTest(trial=trial, first=first, last=last, origins=origins):
                        preview = preview_source_selection(store, req)
                        repeat_of, supersedes, uncovered = oracle(live, req, window)
                        self.assertEqual((preview.repeat_of, preview.supersedes, preview.uncovered),
                                         (repeat_of, supersedes, uncovered))
                        self.assertLessEqual(len(preview.uncovered), 2)
            self.assertEqual(contents(path), before)

    def test_preview_of_disjoint_request_supersedes_nothing(self) -> None:
        self.live((OA,), None, day(2))
        self.live((OB,), day(6), day(6))
        self.live((OA,), day(8), None)
        for first, last in ((day(3), day(5)), (day(3), day(3)), (day(5), day(5)), (day(7), day(7))):
            preview = preview_source_selection(self.store, request((OA, OB), first, last))
            self.assertEqual((preview.repeat_of, preview.supersedes, preview.uncovered), (None, (), ()))

    def test_preview_recognizes_a_repeat(self) -> None:
        single = self.live((OA,), day(2), day(2))
        both = self.live((OA, OB), day(3), None)
        self.live((OB,), None, day(1))
        for req, policy in ((request((OA,), day(2), day(2)), single), (request((OB, OA), day(3), None), both),
                            (request((OA, OB), day(3), None), both)):
            preview = preview_source_selection(self.store, req)
            self.assertEqual((preview.repeat_of, preview.supersedes, preview.uncovered), (policy.policy_id, (), ()))
        # Different origins or different dates are not repeats.
        for req in (request((OB,), day(2), day(2)), request((OA,), day(2), day(3)), request((OA, OB), day(4), None),
                    request((OA, OB), day(3), date.max)):
            self.assertIsNone(preview_source_selection(self.store, req).repeat_of)
        # A superseded policy is not repeated; its replacement is superseded instead.
        newer = self.live((OB,), day(2), day(2), supersedes=(single.policy_id,))
        preview = preview_source_selection(self.store, request((OA,), day(2), day(2)))
        self.assertEqual((preview.repeat_of, preview.supersedes), (None, (newer.policy_id,)))

    def test_preview_writes_nothing(self) -> None:
        self.live((OA,), None, day(3))
        self.live((OB,), day(4), None)
        before = contents(self.path)
        spy, events = self.spy()
        for first, last in all_ranges(6):
            preview_source_selection(spy, request((OA, OB), first, last))
        self.assertEqual(contents(self.path), before)
        self.assertEqual(set(events), {"read"})
        self.assertEqual(len(events), len(all_ranges(6)))


# ---------------------------------------------------------------------------
# R7 — Recording
# ---------------------------------------------------------------------------


class R7Recording(StoreCase):
    def test_request_is_recorded_pending(self) -> None:
        clock = Clock()
        result = set_source_selection_policy(self.store, request((OA, OB), day(1), day(9)), (), clock)
        self.assertIs(type(result), SourceSelectionResult)
        self.assertTrue(result.recorded)
        self.assertEqual(result.uncovered, ())
        policy = result.policy
        self.assertIs(type(policy), SourceSelectionPolicyRecord)
        self.assertEqual((policy.measure, policy.record_type, policy.origins, policy.first_date, policy.last_date,
                          policy.supersedes, policy.superseded_by, policy.requested_at),
                         ("daily-steps", "steps", (OB, OA), day(1), day(9), (), None, WHEN))
        self.assertEqual(clock.calls, 1)
        with self.store.read() as view:
            self.assertEqual(view.policy(policy.policy_id), policy)
            self.assertEqual(view.source_selection_timeline(*STEPS), (policy,))
            self.assertEqual(view.source_selection_policies(), (policy,))
            self.assertEqual(view.fallback_timezone_policies(), ())

    def test_recording_supersedes_the_confirmed_policies(self) -> None:
        left = self.live((OA,), None, day(2))
        middle = self.live((OB,), day(3), day(4))
        right = self.live((OA,), day(5), None)
        result = set_source_selection_policy(self.store, request((OA, OB), day(2), day(5)),
                                             (right.policy_id, left.policy_id, middle.policy_id), Clock())
        self.assertEqual(result.policy.supersedes, (left.policy_id, middle.policy_id, right.policy_id))
        self.assertEqual(result.uncovered, (DateRange(None, day(1)), DateRange(day(6), None)))
        self.assertEqual(self.timeline(), (result.policy,))
        with self.store.read() as view:
            for old in (left, middle, right):
                self.assertEqual(view.policy(old.policy_id).superseded_by, result.policy.policy_id)

    def test_stale_confirmation_is_refused(self) -> None:
        left = self.live((OA,), None, day(2))
        right = self.live((OB,), day(5), None)
        req = request((OA, OB), day(2), day(5))
        for confirmed in ((), (left.policy_id,), (right.policy_id,), (left.policy_id, right.policy_id, 999),
                          (999,), (left.policy_id, right.policy_id + 1000)):
            with self.subTest(confirmed=confirmed):
                self.assert_set_refused("selection-changed", req, confirmed)
        # A disjoint request confirmed as replacing something is stale too.
        self.assert_set_refused("selection-changed", request((OA,), day(3), day(4)), (left.policy_id,))
        # A write landing between the preview and the set.
        preview = preview_source_selection(self.store, req)
        self.assertEqual(preview.supersedes, (left.policy_id, right.policy_id))
        landed = self.live((OB,), day(3), day(3))
        self.assert_set_refused("selection-changed", req, preview.supersedes)
        refreshed = preview_source_selection(self.store, req)
        self.assertEqual(refreshed.supersedes, (left.policy_id, right.policy_id, landed.policy_id))
        # A write landing elsewhere does not change this request's replacement.
        self.live((OSCALE,), measure="weight-series", record_type="weight")
        self.live((OA,), day(9), day(9), measure="heart-rate-summary", record_type="heart-rate")
        result = set_source_selection_policy(self.store, req, refreshed.supersedes, Clock())
        self.assertEqual(result.policy.supersedes, refreshed.supersedes)

    def test_recording_writes_only_the_policy(self) -> None:
        left = self.live((OA,), None, day(2))
        right = self.live((OB,), day(5), None)
        twin_path = Path(self.directory.name) / "twin.sqlite3"
        source = sqlite3.connect(self.path)
        target = sqlite3.connect(twin_path)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
        spy, events = self.spy()
        set_source_selection_policy(spy, request((OB, OA), day(2), day(5)), (right.policy_id, left.policy_id),
                                    Clock(events))
        # The same single store call on a copy gives an identical database.
        twin = open_store(twin_path, reserve_bytes=RESERVE, free_bytes=lambda _path: PLENTY)
        self.addCleanup(twin.close)
        twin.record_source_selection_policy("daily-steps", "steps", (OB, OA), day(2), day(5),
                                            (left.policy_id, right.policy_id), WHEN)
        self.assertEqual(contents(self.path), contents(twin_path))
        self.assertEqual(events.count("write"), 1)
        # R7 step 4: the request, the computed supersession ascending, and the clock's time.
        (call,) = spy.writes
        self.assertEqual(set(call["origins"]), {OA, OB})
        self.assertEqual({**call, "origins": None}, {
            "measure": "daily-steps", "record_type": "steps", "origins": None, "first_date": day(2),
            "last_date": day(5), "supersedes": (left.policy_id, right.policy_id), "at": WHEN})
        self.assertFalse([event for event in events if event.startswith("other:")])

    def test_result_reports_uncovered_dates(self) -> None:
        self.check_random_sets()

    def check_random_sets(self) -> None:
        rng = random.Random(1125)
        window = 6
        ranges = all_ranges(window)
        for trial in range(30):
            store, path = self.fresh()
            live = [self.live(origins, first, last, store=store) for origins, first, last in random_timeline(rng, window)]
            first, last = rng.choice(ranges)
            req = request(rng.choice(((OA,), (OB,), (OB, OA))), first, last)
            repeat_of, supersedes, uncovered = oracle(live, req, window)
            with self.subTest(trial=trial, first=first, last=last):
                before = contents(path)
                result = set_source_selection_policy(store, req, supersedes, Clock())
                if repeat_of is not None:
                    self.assertEqual((result.recorded, result.policy.policy_id, result.uncovered),
                                     (False, repeat_of, ()))
                    self.assertEqual(contents(path), before)
                    continue
                self.assertEqual((result.recorded, result.policy.supersedes, result.uncovered),
                                 (True, supersedes, uncovered))
                kept = [policy for policy in live if policy.policy_id not in supersedes]
                expected = sorted([*kept, result.policy],
                                  key=lambda policy: (policy.first_date is not None, policy.first_date or D))
                self.assertEqual(list(self.timeline(store)), expected)


# ---------------------------------------------------------------------------
# R8 — Repeats
# ---------------------------------------------------------------------------


class R8Repeats(StoreCase):
    def test_identical_request_writes_nothing(self) -> None:
        live = self.live((OA, OB), day(2), day(4))
        other = self.live((OA,), day(5), None)
        before = contents(self.path)
        for req in (request((OA, OB), day(2), day(4)), request((OB, OA), day(2), day(4))):
            for confirmed in ((), (live.policy_id,), (other.policy_id,), (999,)):
                with self.subTest(request=req, confirmed=confirmed):
                    spy, events = self.spy()
                    clock = Clock(events)
                    result = set_source_selection_policy(spy, req, confirmed, clock)
                    self.assertEqual((result.policy, result.recorded, result.uncovered), (live, False, ()))
                    self.assertEqual(clock.calls, 0)
                    self.assertNotIn("write", events)
        self.assertEqual(contents(self.path), before)

    def test_retry_after_commit_returns_the_same_policy(self) -> None:
        old = self.live((OA,), None, None)
        req = request((OB,), day(3), day(3))
        uncertain = StoreRetryable("store-busy")
        spy, _events = self.spy(fail_after=uncertain)
        with self.assertRaises(StoreRetryable) as caught:
            set_source_selection_policy(spy, req, (old.policy_id,), Clock())
        self.assertIs(caught.exception, uncertain)
        (committed,) = self.timeline()
        self.assertEqual(committed.supersedes, (old.policy_id,))
        before = contents(self.path)
        for _ in range(2):
            retry = set_source_selection_policy(self.store, req, (old.policy_id,), Clock())
            self.assertEqual((retry.policy, retry.recorded, retry.uncovered), (committed, False, ()))
        self.assertEqual(contents(self.path), before)
        with self.store.read() as view:
            self.assertEqual(len(view.source_selection_policies(*STEPS)), 2)

    def test_request_equal_to_a_superseded_policy_is_new(self) -> None:
        first = self.live((OA,), day(1), day(3))
        second = self.live((OB,), day(1), day(3), supersedes=(first.policy_id,))
        result = set_source_selection_policy(self.store, request((OA,), day(1), day(3)), (second.policy_id,), Clock())
        self.assertTrue(result.recorded)
        self.assertNotIn(result.policy.policy_id, (first.policy_id, second.policy_id))
        self.assertEqual(result.policy.supersedes, (second.policy_id,))
        self.assertEqual(self.timeline(), (result.policy,))

    def test_identical_request_landing_before_the_write_is_returned(self) -> None:
        # Decision 12 as the published store can report it: recorded=True means this call took the write
        # path and the store returned the identical policy, whether this call inserted it or not (R7, R8).
        prior = self.live((OA,), None, None)
        cases = [
            (request((OB, OA), day(2), day(3)), (prior.policy_id,),
             (DateRange(None, day(1)), DateRange(day(4), None))),
            (request((OB,), None, None, "weight-series", "weight"), (), ()),
        ]
        self.index(self.store, "weight", OB, 1)
        for req, supersedes, uncovered in cases:
            with self.subTest(request=req):
                landed: list[SourceSelectionPolicyRecord] = []
                after_landing: dict[str, object] = {}

                def landing() -> None:
                    landed.append(self.live(req.origins, req.first_date, req.last_date, supersedes, req.measure,
                                            req.record_type))
                    after_landing["contents"] = contents(self.path)

                spy, events = self.spy(before_write=landing)
                clock = Clock(events)
                result = set_source_selection_policy(spy, req, supersedes, clock)
                self.assertEqual(events, ["read", "clock", "write"])
                self.assertEqual((result.policy, result.recorded, result.uncovered), (landed[0], True, uncovered))
                self.assertEqual(contents(self.path), after_landing["contents"])
                self.assertEqual(self.timeline(measure=req.measure, record_type=req.record_type), (landed[0],))
                # A later identical request sees it in its read: a repeat with no write and no clock.
                before = contents(self.path)
                spy, events = self.spy()
                again = set_source_selection_policy(spy, req, supersedes, Clock(events))
                self.assertEqual((again.policy, again.recorded, again.uncovered, events),
                                 (landed[0], False, (), ["read"]))
                self.assertEqual(contents(self.path), before)


# ---------------------------------------------------------------------------
# R9 — The timeline changes before the write
# ---------------------------------------------------------------------------


class R9TimelineChangesBeforeTheWrite(StoreCase):
    def race(self, req, confirmed, landing: Callable[[], object]) -> list[str]:
        """Run the set with landing injected between its read and its write: it must be selection-changed,
        leave exactly the landed write, and not retry."""
        after_landing: dict[str, object] = {}

        def hook() -> None:
            landing()
            after_landing["contents"] = contents(self.path)

        spy, events = self.spy(before_write=hook)
        clock = Clock(events)
        with self.assertRaises(SourceSelectionRefusal) as caught:
            set_source_selection_policy(spy, req, confirmed, clock)
        refusal = caught.exception
        self.assertEqual((refusal.args, refusal.failure, str(refusal)), (("selection-changed",), "selection-changed",
                                                                          "selection-changed"))
        self.assertIsNone(refusal.__cause__)
        self.assertTrue(refusal.__suppress_context__)
        self.assertNotIsInstance(refusal, StoreRefusal)
        self.assertEqual(contents(self.path), after_landing["contents"])
        self.assertEqual(events.count("write"), 1)
        self.assertEqual(clock.calls, 1)
        return events

    def test_concurrent_overlapping_policy_is_selection_changed(self) -> None:
        old = self.live((OA,), None, day(2))
        req = request((OB,), day(2), day(6))
        self.assertEqual(preview_source_selection(self.store, req).supersedes, (old.policy_id,))
        self.race(req, (old.policy_id,), lambda: self.live((OA,), day(5), day(5)))
        # With nothing new in the way, the same request now names both.
        landed = self.timeline()[-1]
        result = set_source_selection_policy(self.store, req, (old.policy_id, landed.policy_id), Clock())
        self.assertEqual(result.policy.supersedes, (old.policy_id, landed.policy_id))

    def test_concurrently_superseded_policy_is_selection_changed(self) -> None:
        old = self.live((OA,), None, None)
        req = request((OB,), day(2), day(3))
        self.race(req, (old.policy_id,), lambda: self.live((OA,), day(8), None, supersedes=(old.policy_id,)))
        # A landing write that does not touch this measure lets the set through.
        spy, events = self.spy(before_write=lambda: self.live((OSCALE,), measure="weight-series", record_type="weight"))
        replacement = self.timeline()[0]
        result = set_source_selection_policy(spy, req, (), Clock(events))
        self.assertTrue(result.recorded)
        self.assertEqual(events.count("write"), 1)
        self.assertNotEqual(result.policy.policy_id, replacement.policy_id)


# ---------------------------------------------------------------------------
# R10 — Store failures pass through
# ---------------------------------------------------------------------------


class R10StoreFailuresPassThrough(StoreCase):
    def test_naive_clock_is_timestamp_invalid(self) -> None:
        old = self.live((OA,))
        before = contents(self.path)
        spy, events = self.spy()
        with self.assertRaises(StoreRefusal) as caught:
            set_source_selection_policy(spy, request((OB,), day(1), day(1)), (old.policy_id,),
                                        Clock(events, NAIVE))
        self.assertIs(type(caught.exception), StoreRefusal)
        self.assertEqual((caught.exception.args, caught.exception.failure), (("timestamp-invalid",), "timestamp-invalid"))
        self.assertNotIsInstance(caught.exception, SourceSelectionRefusal)
        self.assertEqual(contents(self.path), before)
        self.assertLessEqual(events.count("write"), 1)

    def test_busy_and_reserve_pass_through_and_write_nothing(self) -> None:
        old = self.live((OA,))
        req = request((OB,), day(1), day(1))
        # Injected at the write: the very object reaches the caller, after one attempt.
        for failure in (StoreRetryable("store-busy"), StoreRetryable("storage-reserve-exhausted"),
                        StoreRefusal("policy-request-invalid"), StoreRefusal("policy-origin-unknown")):
            with self.subTest(failure=failure.failure):
                before = contents(self.path)
                spy, events = self.spy(fail_with=failure)
                with self.assertRaises(type(failure)) as caught:
                    set_source_selection_policy(spy, req, (old.policy_id,), Clock(events))
                self.assertIs(caught.exception, failure)
                self.assertEqual(events.count("write"), 1)
                self.assertEqual(contents(self.path), before)
        # A real reserve shortfall.
        before = contents(self.path)
        self.free = RESERVE - 1
        with self.assertRaises(StoreRetryable) as caught:
            set_source_selection_policy(self.store, req, (old.policy_id,), Clock())
        self.assertEqual((caught.exception.args, caught.exception.failure),
                         (("storage-reserve-exhausted",), "storage-reserve-exhausted"))
        self.assertEqual(contents(self.path), before)
        self.free = PLENTY
        # A real lock held by another writer.
        store, path = self.fresh(busy_timeout_ms=50)
        old = self.live((OA,), store=store)
        before = contents(path)
        blocker = sqlite3.connect(path, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            with self.assertRaises(StoreRetryable) as caught:
                set_source_selection_policy(store, req, (old.policy_id,), Clock())
            self.assertEqual((caught.exception.args, caught.exception.failure), (("store-busy",), "store-busy"))
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(contents(path), before)
        # Once space returns and the lock is gone, the same request records.
        self.assertTrue(set_source_selection_policy(store, req, (old.policy_id,), Clock()).recorded)

    def test_read_failures_pass_through_and_write_nothing(self) -> None:
        old = self.live((OA,), None, day(3))
        calls = {
            "choices": lambda spy, clock: source_selection_choices(spy, *STEPS),
            "preview": lambda spy, clock: preview_source_selection(spy, request((OB,), day(3), day(5))),
            "set": lambda spy, clock: set_source_selection_policy(spy, request((OB,), day(3), day(5)),
                                                                   (old.policy_id,), clock),
            "set repeat": lambda spy, clock: set_source_selection_policy(spy, request((OA,), None, day(3)), (), clock),
        }
        before = contents(self.path)
        for label, call in calls.items():
            for where in ("call", "enter", "view"):
                for failure in (StoreRetryable("store-busy"), StoreRefusal("store-schema-unsupported")):
                    with self.subTest(operation=label, where=where, failure=failure.failure):
                        spy, events = self.spy(read_fault=(where, failure))
                        clock = Clock(events)
                        with self.assertRaises(type(failure)) as caught:
                            call(spy, clock)
                        self.assertIs(caught.exception, failure)
                        self.assertEqual(clock.calls, 0)
                        self.assertNotIn("write", events)
                        self.assertNotIn("clock", events)
                        self.assertEqual(contents(self.path), before)
        # Without a fault the same calls succeed.
        self.assertTrue(set_source_selection_policy(self.store, request((OB,), day(3), day(5)), (old.policy_id,),
                                                    Clock()).recorded)

    def test_repeat_succeeds_below_the_reserve(self) -> None:
        live = self.live((OA,), day(1), day(2))
        before = contents(self.path)
        self.free = 0
        result = set_source_selection_policy(self.store, request((OA,), day(1), day(2)), (), Clock())
        self.assertEqual((result.policy, result.recorded), (live, False))
        preview_source_selection(self.store, request((OB,)))
        source_selection_choices(self.store, *STEPS)
        self.assertEqual(contents(self.path), before)


# ---------------------------------------------------------------------------
# R11 — Time
# ---------------------------------------------------------------------------


class R11Time(StoreCase):
    def test_clock_is_read_once_only_when_writing(self) -> None:
        old = self.live((OA,))
        spy, events = self.spy()
        clock = Clock(events)
        set_source_selection_policy(spy, request((OB,), day(1), day(1)), (old.policy_id,), clock)
        self.assertEqual(events, ["read", "clock", "write"])
        # The recorded time is the clock's.
        with self.store.read() as view:
            (new,) = view.source_selection_timeline(*STEPS)
        self.assertEqual(new.requested_at, WHEN)
        paths = [
            lambda clock: set_source_selection_policy(self.store, request((OA,), day(1), day(1)), (new.policy_id,), clock),
            lambda clock: set_source_selection_policy(self.store, request((OB,), day(1), day(1)), (), clock),
            lambda clock: set_source_selection_policy(self.store, request(origins=()), (), clock),
            lambda clock: set_source_selection_policy(self.store, request(measure="daily-distance"), (), clock),
            lambda clock: set_source_selection_policy(self.store, request((UNKNOWN,)), (), clock),
            lambda clock: set_source_selection_policy(self.store, request((OA,), day(2), day(2)), (999,), clock),
            lambda clock: set_source_selection_policy(self.store, request((OA,)), [], clock),
            lambda clock: set_source_selection_policy(self.store, request((OA,), day(1), day(1)), (), clock),
        ]
        for number, call in enumerate(paths):
            with self.subTest(path=number):
                clock = Clock()
                try:
                    call(clock)
                except SourceSelectionRefusal:
                    pass
                self.assertEqual(clock.calls, 1 if number == 0 else 0)


# ---------------------------------------------------------------------------
# R12 — Health-data-free surfaces
# ---------------------------------------------------------------------------


class R12HealthDataFreeSurfaces(StoreCase):
    def refusals(self) -> list[SourceSelectionRefusal]:
        old = self.live((OA,))
        marked_dates = (date(1987, 6, 5), date(1987, 6, 6))
        calls = [
            lambda: preview_source_selection(self.store, request((UNKNOWN, OA), *marked_dates)),
            lambda: set_source_selection_policy(self.store, request((UNKNOWN,), *marked_dates), (), Clock()),
            lambda: preview_source_selection(self.store, request((OriginKey(HC, ORIGIN_MARKER), OriginKey(HC, ORIGIN_MARKER)))),
            lambda: preview_source_selection(self.store, request((OA,), marked_dates[1], marked_dates[0])),
            lambda: preview_source_selection(self.store, request((OA,), measure=ORIGIN_MARKER)),
            lambda: source_selection_choices(self.store, ORIGIN_MARKER, "steps"),
            lambda: set_source_selection_policy(self.store, request((OB,), *marked_dates), (old.policy_id + 4242,), Clock()),
            lambda: set_source_selection_policy(self.store, request((OB,), *marked_dates), [ORIGIN_MARKER], Clock()),
        ]
        found = []
        for call in calls:
            with self.assertRaises(SourceSelectionRefusal) as caught:
                call()
            found.append(caught.exception)
        spy, _events = self.spy(before_write=lambda: self.live((OA,), date(1987, 6, 6), date(1987, 6, 6),
                                                               supersedes=(old.policy_id,)))
        with self.assertRaises(SourceSelectionRefusal) as caught:
            set_source_selection_policy(spy, request((OB,), *marked_dates), (old.policy_id,), Clock())
        found.append(caught.exception)
        return found

    def test_refusals_are_health_data_free(self) -> None:
        found = self.refusals()
        self.assertEqual({refusal.failure for refusal in found}, set(OWN_FAILURES))
        for refusal in found:
            with self.subTest(failure=refusal.failure):
                self.assertIn(refusal.failure, OWN_FAILURES)
                self.assertEqual(refusal.args, (refusal.failure,))
                self.assertEqual(str(refusal), refusal.failure)
                self.assertEqual(vars(refusal), {"failure": refusal.failure})
                for text in (str(refusal), repr(refusal)):
                    for marker in (ORIGIN_MARKER, "1987", str(4242), IDENTITY_MARKER, HEALTH_MARKER):
                        self.assertNotIn(marker, text)
                self.assertIsNone(refusal.__cause__)
                self.assertTrue(refusal.__context__ is None or refusal.__suppress_context__)

    def test_results_carry_no_source_evidence(self) -> None:
        old = self.live((OA,))
        choices = source_selection_choices(self.store, *STEPS)
        preview = preview_source_selection(self.store, request((OB,), day(1), day(1)))
        result = set_source_selection_policy(self.store, request((OB,), day(1), day(1)), (old.policy_id,), Clock())
        repeat = set_source_selection_policy(self.store, request((OB,), day(1), day(1)), (), Clock())
        self.assertTrue(preview.uncovered and result.uncovered)
        for value in (choices, preview, result, repeat):
            for marker in (IDENTITY_MARKER, HEALTH_MARKER):
                self.assertNotIn(marker, repr(value))
            instance_state(self, value)
        for origin in choices.origins:
            self.assertIs(type(origin), KnownOrigin)
        self.assertEqual([field.name for field in dataclasses.fields(SourceSelectionChoices)],
                         ["measure", "record_type", "origins", "timeline"])
        self.assertEqual([field.name for field in dataclasses.fields(SourceSelectionPreview)],
                         ["request", "repeat_of", "supersedes", "uncovered"])
        self.assertEqual([field.name for field in dataclasses.fields(SourceSelectionResult)],
                         ["policy", "recorded", "uncovered"])
        self.assertEqual([field.name for field in dataclasses.fields(DateRange)], ["first_date", "last_date"])
        self.assertEqual([field.name for field in dataclasses.fields(SourceSelectionRequest)],
                         ["measure", "record_type", "origins", "first_date", "last_date"])
        for kind in (SourceSelectionChoices, SourceSelectionPreview, SourceSelectionResult, DateRange,
                     SourceSelectionRequest):
            self.assertTrue(kind.__dataclass_params__.frozen)  # type: ignore[attr-defined]

    def test_unit_emits_no_logs(self) -> None:
        with self.assertNoLogs(level="DEBUG"):
            self.refusals()
            source_selection_choices(self.store, *STEPS)
            preview = preview_source_selection(self.store, request((OB,), day(9), day(9)))
            set_source_selection_policy(self.store, request((OB,), day(9), day(9)), preview.supersedes, Clock())
            set_source_selection_policy(self.store, request((OB,), day(9), day(9)), preview.supersedes, Clock())


if __name__ == "__main__":
    unittest.main()
