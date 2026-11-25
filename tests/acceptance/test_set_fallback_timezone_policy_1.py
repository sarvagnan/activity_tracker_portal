"""Acceptance tests for `set-fallback-timezone-policy@1`.

Requirement numbers refer to `packs/set-fallback-timezone-policy-1.spec.md`.
Every database lives in a temporary directory and every value is synthetic.
Stores are built through the published `operational-store@5` API: Source
Records, selections, jobs, and hand-written outcomes come from `@3`'s and `@4`'s
own operations, as the `@5` suite builds them. Every time is the caller's.
"No write" means the whole database, rowids included, is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
import zoneinfo
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from importlib import resources
from pathlib import Path

import tzdata

from activity_tracker_portal import interpret_steps
from activity_tracker_portal.interpret_source_projection import (
    CanonicalRevision,
    FallbackTimezonePolicy,
    ProcessingInput,
    decode_envelope,
    interpret,
)
from activity_tracker_portal.operational_store import (
    BatchItem,
    FallbackTimezonePolicyRecord,
    IngestBatchRecord,
    ItemPosition,
    MessageHeader,
    OriginKey,
    OutcomeEntry,
    ProcessingJob,
    ProjectionEntry,
    RevisionEntry,
    SourceKey,
    StoreRefusal,
    StoreRetryable,
    open_store,
)
from activity_tracker_portal.set_fallback_timezone_policy import (
    FAILURES,
    FallbackTimezonePlan,
    FallbackTimezoneRefusal,
    FallbackTimezoneResult,
    available_zones,
    plan_fallback_timezone_policy,
    set_fallback_timezone_policy,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "governance/scopes/set-fallback-timezone-policy.1.json").read_text(encoding="utf-8"))
STORE_CONTRACT = json.loads((ROOT / "governance/scopes/operational-store.5.json").read_text(encoding="utf-8"))
EFFECTS = json.loads((ROOT / "packs/set-fallback-timezone-policy-1.effects.json").read_text(encoding="utf-8"))
SCOPE_MAP = (ROOT / "docs/governance/scope-map.md").read_text(encoding="utf-8")
VECTORS = {vector["name"]: vector for vector in
           json.loads((ROOT / "projection/v1/vectors/positive.json").read_text(encoding="utf-8"))["vectors"]}

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
NST = timezone(timedelta(hours=-3, minutes=-30))
T0 = datetime(2026, 11, 25, 9, 0, 0, 123456, tzinfo=UTC)
NAIVE = datetime(2026, 11, 25, 9, 0)
RESERVE = 1_000_000
PLENTY = 10**12
INSTALLATION = "0f8fad5b-d9cb-469f-a165-70867728950e"
CYCLE = "e3b0c442-98fc-4c14-9afb-f4c8996fb924"
HC = "health-connect"
HEALTH_MARKER = "synthetic-health-marker"
IDENTITY_MARKER = "synthetic-identity-marker"
ORIGIN_MARKER = "org.example.synthetic-origin-marker"
MARKERS = (HEALTH_MARKER, IDENTITY_MARKER, ORIGIN_MARKER)
ORIGIN = "org.example.synthetic.source"
PV = "interpret-steps@1"
WPV = "interpret-weight@1"
MAP = {"steps": PV, "weight": WPV}
NONE = "none"
INFERRED = 'fallback-timezone:[["synthetic-ftz-1"],"2026f"]'
FTZ_KIND = "fallback-timezone"
OWNER = "synthetic-worker-1"
START_NS = 1_790_000_000_000_000_000
MARCH = datetime(2026, 3, 1, tzinfo=UTC)
JUNE = datetime(2026, 6, 1, tzinfo=UTC)
SEPTEMBER = datetime(2026, 9, 1, tzinfo=UTC)
# The failures this unit raises itself; the rest of the contract's names are the store's, passed through.
OWN_FAILURES = ("timestamp-invalid", "timezone-unknown", "effective-time-invalid", "replacement-invalid",
                "all-history-policy-required", "policy-unchanged", "plan-stale")
# Stands for "the next caller time" where None is itself a value under test.
TICK = object()


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package_zones() -> set[str]:
    """The zone list the pinned tzdata package ships, read from the package itself."""
    text = resources.files("tzdata").joinpath("zones").read_text(encoding="utf-8")
    return {line.strip() for line in text.splitlines() if line.strip()}


def header(sequence: int) -> MessageHeader:
    body = {"synthetic": True, "kind": "ingest-batch", "installation": INSTALLATION, "sequence": sequence}
    data = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return MessageHeader(INSTALLATION, sequence, digest(data), 1, at(sequence), data,
                         b'{"synthetic-receipt":%d}' % sequence)


def batch(item: BatchItem, record_type: str, page: int) -> IngestBatchRecord:
    return IngestBatchRecord(record_type=record_type, projection_schema_version=1,
                             cycle_kind="initial-snapshot", cycle_id=CYCLE, source_page_index=page,  # type: ignore[arg-type]
                             fragment_index=1, previous_chain=digest(b"synthetic-chain-%d" % page), items=(item,))


def revision(*, policy: str = NONE, pv: str = PV) -> OutcomeEntry:
    attribution = "experienced" if policy == NONE else "inferred"
    return OutcomeEntry("revision", pv, policy, None, None, None, RevisionEntry(  # type: ignore[arg-type]
        b'{"synthetic-header":"' + HEALTH_MARKER.encode() + b'"}', b'{"synthetic-body":"' + HEALTH_MARKER.encode() + b'"}',
        1, START_NS, START_NS + 600_000_000_000, date(2026, 9, 21), date(2026, 9, 21), attribution, attribution))


def pending(*, pv: str = PV) -> OutcomeEntry:
    return OutcomeEntry("policy-pending", pv, NONE, "fallback-timezone-policy-missing", None, FTZ_KIND, None)


def without_rowid(connection: sqlite3.Connection) -> set[str]:
    """The main schema's WITHOUT ROWID tables, whatever tables a store version defines."""
    return {name for schema_name, name, _kind, _columns, flag, _strict in connection.execute("PRAGMA table_list")
            if schema_name == "main" and flag}


def table_rows(connection: sqlite3.Connection, table: str, columns: str = "*", rowless: set[str] | None = None):
    """Every row of one table. A rowid table's rows lead with their rowid, in rowid order, because the
    rowid is observable state. A WITHOUT ROWID table has no such identity, so its rows are compared
    as a sorted list."""
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


def offsetless_steps_input() -> ProcessingInput:
    """A synthetic steps projection whose endpoints carry no source offset."""
    value = json.loads(VECTORS["steps-maximal"]["bytes"])
    for name in ("start_zone_offset", "end_zone_offset"):
        value["record"].pop(name, None)
    value["record"]["start_time"] = "2026-07-01T23:30:00Z"
    value["record"]["end_time"] = "2026-07-01T23:50:00Z"
    data = json.dumps(value, separators=(",", ":")).encode()
    envelope = decode_envelope(data, "steps", value["projection_schema_version"])
    return ProcessingInput(envelope.source_system_kind, envelope.origin, "steps", envelope.source_identity, 1, 1, 1,
                           digest(data), envelope.last_modified_time, value["projection_schema_version"], data)


class TimelineOnly:
    """A view that offers only the timeline read: planning needs nothing else (R7)."""

    def __init__(self, timeline: tuple[FallbackTimezonePolicyRecord, ...]) -> None:
        self._timeline = timeline

    def fallback_timezone_timeline(self) -> tuple[FallbackTimezonePolicyRecord, ...]:
        return self._timeline


class Interleaved:
    """A store whose policy write first lets another handle write: a deterministic interleaving
    between the setter's read and its write. Every other attribute is the real store's."""

    def __init__(self, store, before_write) -> None:
        self._store = store
        self._before_write = before_write
        self.writes = 0

    def record_fallback_timezone_policy(self, *args, **kwargs):
        self.writes += 1
        self._before_write()
        return self._store.record_fallback_timezone_policy(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._store, name)


class SetterCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "portal.sqlite3"
        self.free = PLENTY
        self.sequence = 0
        self.minute = 100

    def open(self, path: Path | None = None, busy_timeout_ms: int = 5000):
        store = open_store(path or self.path, reserve_bytes=RESERVE, free_bytes=lambda _path: self.free,
                           busy_timeout_ms=busy_timeout_ms)
        self.addCleanup(store.close)
        return store

    def ready(self):
        store = self.open()
        offer = digest(b"synthetic-enrollment-code")
        store.record_enrollment_offer(offer, at(30), at(0))
        store.enroll_installation(offer, INSTALLATION, digest(b"synthetic-fingerprint"), at(0))
        store.activate_installation(INSTALLATION, at(0))
        store.honor_generation(1, b'{"synthetic-advertisement":1}', T0)
        return store

    def tick(self) -> datetime:
        self.minute += 1
        return at(self.minute)

    def project(self, store, identity: str, record_type: str = "steps", origin: str = ORIGIN) -> tuple[int, int]:
        """Archive, index, and select one synthetic projection: (record_id, selection_id)."""
        self.sequence += 1
        data = b'{"synthetic-projection":"%s","marker":"%s"}' % (identity.encode(), HEALTH_MARKER.encode())
        store.archive_ingest_batch(header(self.sequence), batch(BatchItem(projection=data), record_type, self.sequence))
        when = at(self.sequence)
        store.index_items((ProjectionEntry(ItemPosition(INSTALLATION, self.sequence, 0), HC, origin, identity, when),), when)
        with store.read() as view:
            record_id = view.source_record_id(SourceKey(HC, origin, record_type, identity))
            assert record_id is not None
            found = view.source_record(record_id)
        assert found is not None
        selection = store.record_selection(record_id, "projection", (found.evidence[-1].evidence_id,),
                                           found.evidence_head, when)
        return record_id, selection.selection_id

    def process(self, store, record: tuple[int, int], outcome: OutcomeEntry) -> None:
        when = self.tick()
        store.enqueue_processing(*record, outcome.processor_version, 3, when)
        lease = store.claim_processing_job(OWNER, 60, when)
        assert lease is not None and lease.record_id == record[0]
        store.complete_processing_job(lease.job_id, lease.fence, outcome, when)

    def hold(self, store, record: tuple[int, int]):
        when = self.tick()
        store.enqueue_processing(*record, PV, 3, when)
        lease = store.claim_processing_job(OWNER, 86_400, when)
        assert lease is not None and lease.record_id == record[0]
        return lease

    def jobs(self, store, record_id: int) -> tuple[ProcessingJob, ...]:
        with store.read() as view:
            return view.processing_jobs(record_id)

    def timeline(self, store) -> tuple[FallbackTimezonePolicyRecord, ...]:
        with store.read() as view:
            return view.fallback_timezone_timeline()

    def history(self, store) -> tuple[FallbackTimezonePolicyRecord, ...]:
        with store.read() as view:
            return view.fallback_timezone_policies()

    def plan(self, store, zone: str, start: datetime | None, replacing=(), when: object = TICK):
        moment = self.tick() if when is TICK else when
        with store.read() as view:
            return plan_fallback_timezone_policy(view, zone, start, replacing=replacing, at=moment)

    def confirm(self, store, plan, when: object = TICK, mapping=None, max_attempts: int = 3):
        moment = self.tick() if when is TICK else when
        return set_fallback_timezone_policy(store, plan, processor_versions=MAP if mapping is None else mapping,
                                            max_attempts=max_attempts, at=moment)

    def setp(self, store, zone: str, start: datetime | None, replacing=()) -> FallbackTimezoneResult:
        """Plan and confirm one request through this unit."""
        return self.confirm(store, self.plan(store, zone, start, replacing))

    def direct(self, store, zone: str, start: datetime | None, supersedes=()) -> FallbackTimezonePolicyRecord:
        """A policy recorded straight through the store, as another writer would."""
        return store.record_fallback_timezone_policy(zone, start, supersedes, {}, 3, self.tick()).policy

    def assertRefusal(self, failure: str, operation, *args, **kwargs) -> FallbackTimezoneRefusal:
        before = contents(self.path)
        with self.assertRaises(FallbackTimezoneRefusal) as caught:
            operation(*args, **kwargs)
        self.assertIs(type(caught.exception), FallbackTimezoneRefusal)
        self.assertEqual(caught.exception.failure, failure)
        self.assertEqual(contents(self.path), before, f"{failure} changed the database")
        return caught.exception

    def assertPlanRefused(self, failure: str, store, zone, start, replacing=(), when: object = TICK) -> None:
        self.assertRefusal(failure, self.plan, store, zone, start, replacing, when)

    def assertCleanChain(self, error: BaseException) -> None:
        """R12 as approved: no __cause__, and no __context__ that carries another exception's text. A
        store exception the unit handled must not stay attached, suppressed or not, so the unit raises
        its refusal outside the handler; `raise ... from None` inside it does not meet R12. Neither the
        formatted traceback nor any message in the chain carries a marker. An exception the caller is
        already handling when it calls the unit (ambient caller context) is out of scope, so no test
        calls the unit inside an except block."""
        self.assertIsNone(error.__cause__)
        context = error.__context__
        while context is not None:
            self.assertEqual(str(context), "", "a refusal's context must carry no other exception's text")
            self.assertTrue(all(str(arg) == "" for arg in context.args),
                            "a refusal's context must carry no other exception's text")
            context = context.__cause__ or context.__context__
        text = "".join(traceback.format_exception(error))
        link = error
        while link is not None:
            text += str(link) + repr(link)
            link = link.__cause__ or link.__context__
        for marker in MARKERS:
            self.assertNotIn(marker, text)

    def assertWritesNothing(self, operation, *args, **kwargs):
        before = contents(self.path)
        result = operation(*args, **kwargs)
        self.assertEqual(contents(self.path), before, "a call that writes nothing changed the database")
        return result

    def copy_database(self) -> Path:
        """A copy of the database, taken before a confirmation."""
        target = self.directory / f"control-{self.tick().timestamp()}.sqlite3"
        source, copy = sqlite3.connect(self.path), sqlite3.connect(target)
        try:
            source.backup(copy)
        finally:
            source.close()
            copy.close()
        return target


# R1 — Governed Scope contract


class R1GovernedScopeContract(unittest.TestCase):
    def test_contract_has_required_shape(self) -> None:
        self.assertEqual(set(CONTRACT), {"contract_schema", "scope", "version", "kind", "purpose", "inputs", "outputs",
                                         "named_failures", "observable_effects", "authority", "dependencies",
                                         "acceptance_oracle"})
        self.assertEqual((CONTRACT["scope"], CONTRACT["version"], CONTRACT["kind"]),
                         ("set-fallback-timezone-policy", "1", "leaf"))
        self.assertEqual(CONTRACT["dependencies"], [{"scope": "operational-store", "version": "5"}])
        failures = [(item["name"], item["outcome"]) for item in CONTRACT["named_failures"]]
        self.assertEqual(failures[:7], [
            ("timestamp-invalid", "invariant-refusal"), ("timezone-unknown", "refusal"),
            ("effective-time-invalid", "refusal"), ("replacement-invalid", "refusal"),
            ("all-history-policy-required", "refusal"), ("policy-unchanged", "refusal"), ("plan-stale", "conflict")])
        self.assertEqual(FAILURES, frozenset(OWN_FAILURES))
        self.assertEqual([name for name, _ in failures[:7]], list(OWN_FAILURES))
        # The rest are the store's own failures, with the store's outcome classes.
        store_outcomes = {item["name"]: item["outcome"] for item in STORE_CONTRACT["named_failures"]}
        self.assertEqual({name for name, _ in failures[7:]}, {
            "policy-request-invalid", "processor-version-invalid", "processing-request-invalid", "store-busy",
            "storage-reserve-exhausted"})
        for name, outcome in failures[7:]:
            self.assertEqual(store_outcomes[name], outcome, name)
        self.assertEqual([effect["name"] for effect in CONTRACT["observable_effects"]], ["fallback-timezone-policy-write"])
        oracle = CONTRACT["acceptance_oracle"]
        self.assertEqual(oracle["executable_tests"], {"python": "tests/acceptance/test_set_fallback_timezone_policy_1.py"})
        self.assertEqual(oracle["runner"], "tests/acceptance/run_set_fallback_timezone_policy_1.sh")
        for path in (oracle["specification"], oracle["runner"], *oracle["executable_tests"].values()):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_effects_declaration_matches_contract(self) -> None:
        self.assertEqual(EFFECTS["effects_format"], 1)
        self.assertEqual([effect["name"] for effect in EFFECTS["effects"]],
                         [effect["name"] for effect in CONTRACT["observable_effects"]])
        for effect in EFFECTS["effects"]:
            self.assertEqual((effect["kind"], effect["outcome"]), ("action", "confirmed"))
            self.assertIn("operational-store@5", effect["system"])

    def test_scope_map_row_does_not_contradict_contract(self) -> None:
        rows = [line for line in SCOPE_MAP.splitlines() if line.startswith("| `set-fallback-timezone-policy` |")]
        self.assertEqual(len(rows), 1)
        cells = [cell.strip() for cell in rows[0].strip().strip("|").split("|")]
        self.assertEqual(cells[1], "leaf")
        self.assertLessEqual(set(re.findall(r"`([a-z0-9-]+)`", cells[5])),
                             {item["scope"] for item in CONTRACT["dependencies"]})
        self.assertIn("pending", cells[4])
        self.assertIn("pending", CONTRACT["purpose"])


# R2 — Allowed zones


class R2AllowedZones(SetterCase):
    def test_allowed_zones_are_the_pinned_tzdata_list(self) -> None:
        zones = available_zones()
        self.assertIs(type(zones), tuple)
        self.assertEqual(zones, tuple(sorted(package_zones() - {"Factory"})))
        self.assertEqual(tzdata.IANA_VERSION, "2026f")
        self.assertIn("Factory", package_zones())
        self.assertNotIn("Factory", zones)
        for zone in ("Europe/London", "Asia/Kolkata", "Etc/UTC", "Etc/GMT+5", "America/Argentina/Buenos_Aires"):
            self.assertIn(zone, zones)

    def test_links_are_accepted_as_named(self) -> None:
        store = self.ready()
        zic = resources.files("tzdata.zoneinfo").joinpath("tzdata.zi").read_text(encoding="utf-8")
        links = {line.split()[2]: line.split()[1] for line in zic.splitlines() if line.startswith("L ")}
        first = self.setp(store, "UTC", None).policy
        self.assertEqual(first.zone, "UTC")
        start = MARCH
        for link in ("Europe/Amsterdam", "Asia/Calcutta", "US/Eastern", "Atlantic/Reykjavik"):
            with self.subTest(link=link):
                self.assertIn(link, links)  # a link in the pinned data, not a Zone
                self.assertIn(link, available_zones())
                plan = self.plan(store, link, start)
                self.assertEqual(plan.zone, link)
                self.assertEqual(self.confirm(store, plan).policy.zone, link)
                start += timedelta(days=7)
        self.assertEqual([policy.zone for policy in self.timeline(store)],
                         ["UTC", "Europe/Amsterdam", "Asia/Calcutta", "US/Eastern", "Atlantic/Reykjavik"])

    def test_unknown_and_malformed_zones_are_refused(self) -> None:
        store = self.ready()
        self.setp(store, "Europe/London", None)
        names = ("Synthetic/Unknown_Zone", "Factory", "leapseconds", "zone.tab", "zone1970.tab", "zonenow.tab",
                 "iso3166.tab", "tzdata.zi", "__init__.py", "localtime", "posixrules", "", " UTC", "UTC ", "UTC\n",
                 "../UTC", "zoneinfo/UTC", "Europe//London", "/UTC", "UTC/", "Europe/London/", "Etc\\UTC",
                 "Europe/./London", None, 5, b"UTC", ("UTC",))
        for name in names:
            with self.subTest(zone=name):
                self.assertPlanRefused("timezone-unknown", store, name, JUNE)
                plan = FallbackTimezonePlan(name, JUNE, (), ())  # type: ignore[arg-type]
                self.assertRefusal("timezone-unknown", self.confirm, store, plan)
        # leapseconds is a file in the package that passes the store's form check (@5 Decision 9).
        self.assertTrue(resources.files("tzdata.zoneinfo").joinpath("leapseconds").is_file())

    def test_zone_names_are_case_sensitive(self) -> None:
        store = self.ready()
        self.setp(store, "Europe/London", None)
        for name in ("europe/london", "EUROPE/LONDON", "Europe/london", "utc", "etc/utc", "asia/kolkata"):
            with self.subTest(zone=name):
                self.assertPlanRefused("timezone-unknown", store, name, JUNE)
                self.assertRefusal("timezone-unknown", self.confirm, store, FallbackTimezonePlan(name, JUNE, (), ()))
        # On a case-insensitive filesystem, such as macOS's default, the package resource for a
        # case variant opens: a file-existence check would accept it. Matching must be exact.
        # The package's own filesystem decides; on a case-sensitive one the variant is simply absent.
        variant = resources.files("tzdata.zoneinfo").joinpath("europe", "london")
        if variant.is_file():
            with variant.open("rb") as stream:
                self.assertEqual(zoneinfo.ZoneInfo.from_file(stream, key="europe/london").utcoffset(JUNE),
                                 timedelta(hours=1))

    def test_host_timezone_database_is_never_read(self) -> None:
        store = self.ready()
        self.setp(store, "Europe/London", None)
        host = self.directory / "host-zoneinfo"
        (host / "Synthetic").mkdir(parents=True)
        utc = resources.files("tzdata.zoneinfo").joinpath("Etc", "UTC").read_bytes()
        (host / "Synthetic" / "Host_Only").write_bytes(utc)
        (host / "Factory2").write_bytes(utc)
        zoneinfo.reset_tzpath(to=[str(host)])
        self.addCleanup(zoneinfo.ZoneInfo.clear_cache)
        self.addCleanup(zoneinfo.reset_tzpath)
        zoneinfo.ZoneInfo.clear_cache()
        # The host database now offers names the pinned package does not have.
        self.assertEqual(zoneinfo.ZoneInfo("Synthetic/Host_Only").utcoffset(JUNE), timedelta(0))
        self.assertIn("Synthetic/Host_Only", zoneinfo.available_timezones())
        self.assertEqual(available_zones(), tuple(sorted(package_zones() - {"Factory"})))
        for name in ("Synthetic/Host_Only", "Factory2"):
            with self.subTest(zone=name):
                self.assertPlanRefused("timezone-unknown", store, name, JUNE)
                self.assertRefusal("timezone-unknown", self.confirm, store, FallbackTimezonePlan(name, JUNE, (), ()))
        # A host database without the pinned names changes nothing either.
        empty = self.directory / "empty-zoneinfo"
        empty.mkdir()
        zoneinfo.reset_tzpath(to=[str(empty)])
        zoneinfo.ZoneInfo.clear_cache()
        self.assertEqual(self.setp(store, "Asia/Kolkata", JUNE).policy.zone, "Asia/Kolkata")
        # A fresh process whose host database holds the synthetic zone from the start, so no
        # zone list can have been read before the host database changed.
        probe = (
            "import json, sys\n"
            "from datetime import datetime, timezone\n"
            "from activity_tracker_portal.set_fallback_timezone_policy import FallbackTimezoneRefusal, "
            "available_zones, plan_fallback_timezone_policy\n"
            "class View:\n"
            "    def fallback_timezone_timeline(self):\n"
            "        return ()\n"
            "results = {'listed': 'Synthetic/Host_Only' in available_zones(), 'count': len(available_zones())}\n"
            "for name in ('Synthetic/Host_Only', 'Factory2'):\n"
            "    try:\n"
            "        plan_fallback_timezone_policy(View(), name, None, at=datetime(2026, 1, 1, tzinfo=timezone.utc))\n"
            "        results[name] = 'accepted'\n"
            "    except FallbackTimezoneRefusal as error:\n"
            "        results[name] = error.failure\n"
            "print(json.dumps(results))\n"
        )
        environment = dict(os.environ, PYTHONTZPATH=str(host), PYTHONDONTWRITEBYTECODE="1")
        completed = subprocess.run([sys.executable, "-c", probe], env=environment, capture_output=True, text=True,
                                   timeout=120, check=True)
        self.assertEqual(json.loads(completed.stdout), {
            "listed": False, "count": len(package_zones() - {"Factory"}),
            "Synthetic/Host_Only": "timezone-unknown", "Factory2": "timezone-unknown"})

    def test_every_allowed_zone_is_accepted_by_interpret(self) -> None:
        store = self.ready()
        processing_input = offsetless_steps_input()
        zones = available_zones()
        self.assertGreater(len(zones), 400)
        with store.read() as view:
            for zone in zones:
                with self.subTest(zone=zone):
                    plan = plan_fallback_timezone_policy(view, zone, None, at=T0)
                    self.assertEqual((plan.zone, plan.effective_from, plan.supersedes), (zone, None, ()))
                    outcome = interpret(processing_input, interpret_steps.PROCESSOR,
                                        [FallbackTimezonePolicy("1", None, zone)])
                    self.assertIsInstance(outcome, CanonicalRevision)
                    self.assertEqual(outcome.header.time.start.attribution, "inferred")


# R3 — Start and request time


class R3StartAndRequestTime(SetterCase):
    def test_all_earlier_history_start(self) -> None:
        store = self.ready()
        plan = self.plan(store, "Asia/Kolkata", None)
        self.assertEqual(plan, FallbackTimezonePlan("Asia/Kolkata", None, (), ()))
        result = self.confirm(store, plan)
        self.assertIsNone(result.policy.effective_from)
        self.assertEqual(self.timeline(store), (result.policy,))

    def test_start_is_held_in_utc(self) -> None:
        store = self.ready()
        self.setp(store, "Asia/Kolkata", None)
        local = datetime(2026, 6, 1, 5, 30, tzinfo=IST)
        plan = self.plan(store, "Europe/London", local)
        self.assertEqual(plan.effective_from, JUNE)
        self.assertIs(plan.effective_from.tzinfo, UTC)
        self.assertEqual(self.confirm(store, plan).policy.effective_from, JUNE)

    def test_start_may_equal_request_time(self) -> None:
        store = self.ready()
        self.setp(store, "Asia/Kolkata", None)
        when = self.tick()
        plan = self.plan(store, "Europe/London", when.astimezone(NST), when=when)
        self.assertEqual(plan.effective_from, when)
        self.assertEqual(self.confirm(store, plan, when=when).policy.effective_from, when)

    def test_future_start_is_refused(self) -> None:
        store = self.ready()
        self.setp(store, "Asia/Kolkata", None)
        when = self.tick()
        self.assertPlanRefused("effective-time-invalid", store, "Europe/London", when + timedelta(microseconds=1), when=when)
        self.assertPlanRefused("effective-time-invalid", store, "Europe/London",
                               (when + timedelta(days=30)).astimezone(IST), when=when)
        # A plan made earlier is checked again against the confirmation's own time.
        plan = self.plan(store, "Europe/London", when, when=when)
        self.assertRefusal("effective-time-invalid", self.confirm, store, plan, when - timedelta(microseconds=1))

    def test_invalid_start_is_refused(self) -> None:
        store = self.ready()
        self.setp(store, "Asia/Kolkata", None)
        low = datetime(1, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=1)))
        high = datetime(9999, 12, 31, 23, 30, tzinfo=timezone(timedelta(hours=-1)))
        for start in (NAIVE, date(2026, 6, 1), "2026-06-01T00:00:00Z", 1_780_000_000, low, high):
            with self.subTest(start=start):
                self.assertPlanRefused("effective-time-invalid", store, "Europe/London", start)
                self.assertRefusal("effective-time-invalid", self.confirm, store,
                                   FallbackTimezonePlan("Europe/London", start, (), ()))  # type: ignore[arg-type]
        # A start just inside the range is accepted.
        early = datetime(1, 1, 1, 0, 30, tzinfo=timezone(timedelta(minutes=30)))
        self.assertEqual(self.plan(store, "Europe/London", early).effective_from, datetime(1, 1, 1, tzinfo=UTC))

    def test_naive_request_time_is_refused(self) -> None:
        store = self.ready()
        plan = self.plan(store, "Asia/Kolkata", None)
        # An aware time whose UTC value is out of range is timestamp-invalid too (drafter's resolution
        # of implementer review finding 5, accepted at Gate B).
        low = datetime(1, 1, 1, 0, 30, tzinfo=timezone(timedelta(hours=1)))
        high = datetime(9999, 12, 31, 23, 30, tzinfo=timezone(timedelta(hours=-1)))
        for when in (NAIVE, date(2026, 6, 1), "2026-11-25T09:00:00Z", None, low, high):
            with self.subTest(at=when):
                self.assertPlanRefused("timestamp-invalid", store, "Asia/Kolkata", None, when=when)
                self.assertRefusal("timestamp-invalid", self.confirm, store, plan, when)

    def test_refusal_order(self) -> None:
        store = self.ready()
        when = self.tick()
        # Empty timeline: a start is all-history-policy-required, which ranks below every request check.
        cases = [
            ("timestamp-invalid", "Synthetic/Unknown_Zone", NAIVE, NAIVE, [1]),
            ("timezone-unknown", "Synthetic/Unknown_Zone", NAIVE, when, [1]),
            ("effective-time-invalid", "Europe/London", NAIVE, when, [1]),
            ("replacement-invalid", "Europe/London", JUNE, when, [1]),
            ("all-history-policy-required", "Europe/London", JUNE, when, (999,)),
        ]
        for failure, zone, start, moment, replacing in cases:
            with self.subTest(failure=failure):
                self.assertPlanRefused(failure, store, zone, start, replacing, when=moment)
                if failure != "all-history-policy-required":
                    self.assertRefusal(failure, self.confirm, store, FallbackTimezonePlan(zone, start, replacing, ()), moment)
        first = self.setp(store, "Europe/London", None).policy
        # Replacement validity ranks above an unchanged request.
        self.assertPlanRefused("replacement-invalid", store, "Europe/London", None, (first.policy_id,))


# R4 — The first policy covers all earlier history


class R4FirstPolicyCoversAllHistory(SetterCase):
    def test_first_policy_must_cover_all_history(self) -> None:
        store = self.ready()
        self.assertPlanRefused("all-history-policy-required", store, "Asia/Kolkata", JUNE)
        self.assertPlanRefused("all-history-policy-required", store, "Asia/Kolkata", JUNE.astimezone(IST))
        # A timeline written past this unit, with no all-history policy, needs one too.
        later = self.direct(store, "Europe/London", SEPTEMBER)
        self.assertPlanRefused("all-history-policy-required", store, "Asia/Kolkata", JUNE)
        self.assertPlanRefused("all-history-policy-required", store, "Asia/Kolkata", SEPTEMBER)
        first = self.setp(store, "Asia/Kolkata", None).policy
        self.assertEqual(self.timeline(store), (first, later))
        self.assertEqual(self.setp(store, "Asia/Tokyo", JUNE).policy.effective_from, JUNE)

    def test_timeline_always_begins_with_all_history(self) -> None:
        store = self.ready()
        steps = [("Asia/Kolkata", None, ()), ("Europe/London", JUNE, ()), ("Asia/Tokyo", MARCH, ()),
                 ("UTC", None, ()), ("Europe/Paris", JUNE, ()), ("America/New_York", SEPTEMBER, ())]
        for zone, start, replacing in steps:
            self.setp(store, zone, start, replacing)
            timeline = self.timeline(store)
            self.assertIsNone(timeline[0].effective_from)
            self.assertTrue(all(policy.effective_from is not None for policy in timeline[1:]))
        # Replacing every later policy from all earlier history leaves one policy.
        later = tuple(policy.policy_id for policy in self.timeline(store)[1:])
        result = self.setp(store, "Asia/Kolkata", None, later)
        self.assertEqual(self.timeline(store), (result.policy,))


# R5 — What a new policy replaces


class R5WhatANewPolicyReplaces(SetterCase):
    def build(self):
        store = self.ready()
        first = self.setp(store, "Asia/Kolkata", None).policy
        june = self.setp(store, "Europe/London", JUNE).policy
        september = self.setp(store, "America/New_York", SEPTEMBER).policy
        return store, first, june, september

    def test_same_start_policy_is_replaced(self) -> None:
        store, first, june, september = self.build()
        plan = self.plan(store, "Europe/Paris", JUNE.astimezone(IST))
        self.assertEqual((plan.replacing, plan.supersedes), ((), (june.policy_id,)))
        new = self.confirm(store, plan).policy
        self.assertEqual(new.supersedes, (june.policy_id,))
        self.assertEqual(self.timeline(store), (first, new, september))

    def test_all_history_policy_replaces_all_history(self) -> None:
        store, first, june, september = self.build()
        plan = self.plan(store, "UTC", None)
        self.assertEqual(plan.supersedes, (first.policy_id,))
        new = self.confirm(store, plan).policy
        self.assertEqual(self.timeline(store), (new, june, september))

    def test_later_policies_are_kept_by_default(self) -> None:
        store, first, june, september = self.build()
        plan = self.plan(store, "Asia/Tokyo", MARCH)
        self.assertEqual((plan.replacing, plan.supersedes), ((), ()))
        new = self.confirm(store, plan).policy
        self.assertEqual(self.timeline(store), (first, new, june, september))
        # An all-history request keeps every later policy too.
        plan = self.plan(store, "UTC", None)
        self.assertEqual(plan.supersedes, (first.policy_id,))

    def test_named_later_policies_are_replaced(self) -> None:
        store, first, june, september = self.build()
        plan = self.plan(store, "Asia/Tokyo", MARCH, (september.policy_id, june.policy_id))
        self.assertEqual(plan.replacing, (june.policy_id, september.policy_id))
        self.assertEqual(plan.supersedes, (june.policy_id, september.policy_id))
        new = self.confirm(store, plan).policy
        self.assertEqual(self.timeline(store), (first, new))
        # Same start and a named later policy together, ascending.
        later = self.setp(store, "Europe/Berlin", SEPTEMBER).policy
        plan = self.plan(store, "Asia/Seoul", MARCH, (later.policy_id,))
        self.assertEqual(plan.supersedes, (new.policy_id, later.policy_id))
        self.assertEqual(self.confirm(store, plan).policy.supersedes, (new.policy_id, later.policy_id))

    def test_earlier_policies_are_never_replaced(self) -> None:
        store, first, june, september = self.build()
        for start, named in ((JUNE, first.policy_id), (SEPTEMBER, june.policy_id), (SEPTEMBER, first.policy_id),
                             (JUNE, june.policy_id), (SEPTEMBER + timedelta(days=1), september.policy_id)):
            with self.subTest(start=start, named=named):
                self.assertPlanRefused("replacement-invalid", store, "Asia/Tokyo", start, (named,))
        self.assertPlanRefused("replacement-invalid", store, "UTC", None, (first.policy_id,))
        plan = self.plan(store, "Asia/Tokyo", SEPTEMBER + timedelta(days=1))
        self.confirm(store, plan)
        self.assertEqual(self.timeline(store)[:3], (first, june, september))

    def test_invalid_replacements_are_refused(self) -> None:
        store, first, june, september = self.build()
        superseded = self.setp(store, "Europe/Paris", JUNE).policy.supersedes[0]
        self.assertEqual(superseded, june.policy_id)
        selection = self.ready_selection(store)
        for replacing in ((999,), (superseded,), (selection,), (september.policy_id, september.policy_id),
                          [september.policy_id], {september.policy_id}, september.policy_id, None, (0,), (-1,),
                          (True,), (str(september.policy_id),), (float(september.policy_id),), (2**63,)):
            with self.subTest(replacing=replacing):
                self.assertPlanRefused("replacement-invalid", store, "Asia/Tokyo", MARCH, replacing)

    def ready_selection(self, store) -> int:
        self.project(store, "selection-origin")
        return store.record_source_selection_policy("daily-steps", "steps", (OriginKey(HC, ORIGIN),), None, None, (),
                                                    self.tick()).policy_id


# R6 — A request that changes nothing


class R6RequestThatChangesNothing(SetterCase):
    def test_unchanged_request_is_refused(self) -> None:
        store = self.ready()
        first = self.setp(store, "Asia/Kolkata", None).policy
        june = self.setp(store, "Europe/London", JUNE).policy
        self.assertPlanRefused("policy-unchanged", store, "Asia/Kolkata", None)
        self.assertPlanRefused("policy-unchanged", store, "Europe/London", JUNE.astimezone(IST))
        # A link is a different name, so it is a change (Decision 2).
        self.assertEqual(self.plan(store, "Europe/Amsterdam", JUNE).supersedes, (june.policy_id,))
        # Settles: only a policy starting at exactly that start counts. A zone that an earlier
        # policy already gives is a new policy, not policy-unchanged.
        plan = self.plan(store, "Asia/Kolkata", MARCH)
        self.assertEqual(plan.supersedes, ())
        self.assertEqual(self.confirm(store, plan).policy.zone, "Asia/Kolkata")
        self.assertEqual(self.timeline(store)[0], first)

    def test_same_zone_with_named_replacements_is_a_change(self) -> None:
        store = self.ready()
        first = self.setp(store, "Asia/Kolkata", None).policy
        june = self.setp(store, "Europe/London", JUNE).policy
        plan = self.plan(store, "Asia/Kolkata", None, (june.policy_id,))
        self.assertEqual(plan.supersedes, (first.policy_id, june.policy_id))
        new = self.confirm(store, plan).policy
        self.assertEqual((new.zone, self.timeline(store)), ("Asia/Kolkata", (new,)))


# R7 — Planning writes nothing


class R7PlanningWritesNothing(SetterCase):
    def test_planning_writes_nothing(self) -> None:
        store = self.ready()
        held = self.hold(store, self.project(store, "held"))
        self.process(store, self.project(store, "inferred"), revision(policy=INFERRED))
        self.assertWritesNothing(self.plan, store, "Asia/Kolkata", None)
        first = self.setp(store, "Asia/Kolkata", None).policy
        june = self.setp(store, "Europe/London", JUNE).policy
        for zone, start, replacing in (("UTC", None, ()), ("Asia/Tokyo", MARCH, (june.policy_id,)),
                                       ("Europe/Paris", JUNE, ())):
            with self.subTest(zone=zone):
                self.assertWritesNothing(self.plan, store, zone, start, replacing)
        self.assertEqual(self.timeline(store), (first, june))
        self.assertEqual(self.jobs(store, held.record_id)[-1].state, "leased")
        # Planning reads only the timeline: a view offering nothing else is enough.
        timeline = self.timeline(store)
        plan = plan_fallback_timezone_policy(TimelineOnly(timeline), "Asia/Tokyo", MARCH, replacing=(june.policy_id,),
                                             at=self.tick())
        self.assertEqual(plan, self.plan(store, "Asia/Tokyo", MARCH, (june.policy_id,)))
        with self.assertRaises(FallbackTimezoneRefusal) as caught:
            plan_fallback_timezone_policy(TimelineOnly(()), "Asia/Tokyo", MARCH, at=self.tick())
        self.assertEqual(caught.exception.failure, "all-history-policy-required")


# R8 — Confirming a plan


class R8ConfirmingAPlan(SetterCase):
    def test_confirmed_plan_is_recorded_pending(self) -> None:
        store = self.ready()
        when = self.tick().astimezone(IST)
        plan = self.plan(store, "Asia/Kolkata", None, when=when)
        result = self.confirm(store, plan, when=when)
        self.assertIsInstance(result, FallbackTimezoneResult)
        self.assertFalse(result.repeated)
        policy = result.policy
        self.assertEqual((policy.zone, policy.effective_from, policy.supersedes, policy.superseded_by),
                         ("Asia/Kolkata", None, (), None))
        self.assertEqual(policy.requested_at, when)
        with store.read() as view:
            self.assertEqual(view.policy(policy.policy_id), policy)
            self.assertEqual(view.fallback_timezone_timeline(), (policy,))
        with self.assertRaises(FrozenInstanceError):
            result.repeated = True  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            plan.zone = "UTC"  # type: ignore[misc]

    def test_confirmation_supersedes_exactly_the_plan(self) -> None:
        store = self.ready()
        first = self.setp(store, "Asia/Kolkata", None).policy
        june = self.setp(store, "Europe/London", JUNE).policy
        september = self.setp(store, "America/New_York", SEPTEMBER).policy
        plan = self.plan(store, "Europe/Paris", JUNE, (september.policy_id,))
        new = self.confirm(store, plan).policy
        self.assertEqual(new.supersedes, plan.supersedes)
        with store.read() as view:
            self.assertEqual(view.policy(june.policy_id).superseded_by, new.policy_id)
            self.assertEqual(view.policy(september.policy_id).superseded_by, new.policy_id)
            self.assertIsNone(view.policy(first.policy_id).superseded_by)
        self.assertEqual(len(self.history(store)), 4)

    def test_confirmation_rechecks_the_request(self) -> None:
        store = self.ready()
        self.setp(store, "Asia/Kolkata", None)
        when = self.tick()
        plan = self.plan(store, "Europe/London", when, when=when)
        self.assertRefusal("timestamp-invalid", self.confirm, store, plan, NAIVE)
        self.assertRefusal("effective-time-invalid", self.confirm, store, plan, when - timedelta(seconds=1))
        self.assertRefusal("timezone-unknown", self.confirm, store, replace(plan, zone="Factory"))
        self.assertRefusal("replacement-invalid", self.confirm, store, replace(plan, replacing=[1]))
        self.assertRefusal("replacement-invalid", self.confirm, store, replace(plan, replacing=(0,)))
        self.assertEqual(self.confirm(store, plan).policy.effective_from, when)

    def test_hand_built_plan_cannot_bypass_rules(self) -> None:
        store = self.ready()
        # No live all-history policy: a hand-built plan with a start is stale, not recorded.
        self.assertRefusal("plan-stale", self.confirm, store, FallbackTimezonePlan("Asia/Kolkata", JUNE, (), ()))
        first = self.setp(store, "Asia/Kolkata", None).policy
        june = self.setp(store, "Europe/London", JUNE).policy
        september = self.setp(store, "America/New_York", SEPTEMBER).policy
        stale = [
            FallbackTimezonePlan("UTC", None, (), ()),                                   # omits the same-start policy
            FallbackTimezonePlan("Asia/Tokyo", MARCH, (), (first.policy_id,)),          # supersedes an earlier policy
            FallbackTimezonePlan("Asia/Tokyo", MARCH, (), (june.policy_id,)),           # a later policy not named
            FallbackTimezonePlan("Asia/Tokyo", MARCH, (first.policy_id,), (first.policy_id,)),  # names an earlier one
            FallbackTimezonePlan("Europe/London", JUNE, (), (june.policy_id,)),         # changes nothing
            FallbackTimezonePlan("Asia/Tokyo", JUNE, (), (june.policy_id, 999)),        # names an unknown one
            FallbackTimezonePlan("Asia/Tokyo", JUNE, (), [june.policy_id]),             # not a tuple
        ]
        for plan in stale:
            with self.subTest(plan=plan):
                self.assertRefusal("plan-stale", self.confirm, store, plan)
        # Drafter's resolution of implementer review finding 4, accepted at Gate B: a plan must be
        # internally consistent, with replacing a subset of supersedes.
        # Otherwise it is replacement-invalid before repeat detection, so a forged repeat is refused.
        for forged in (FallbackTimezonePlan("Asia/Kolkata", None, (999,), ()),
                       FallbackTimezonePlan("Asia/Kolkata", None, (june.policy_id,), ()),
                       FallbackTimezonePlan("Asia/Tokyo", JUNE, (september.policy_id,), (june.policy_id,))):
            with self.subTest(forged=forged):
                self.assertRefusal("replacement-invalid", self.confirm, store, forged)
        # Beyond that, a repeat is recognised by the recorded write content: zone, start, and supersedes.
        genuine = self.assertWritesNothing(self.confirm, store, FallbackTimezonePlan("Asia/Kolkata", None, (), ()))
        self.assertEqual(genuine, FallbackTimezoneResult(first, True, 0, 0))
        self.assertEqual(self.timeline(store), (first, june, september))


# R9 — Repeats and stale plans


class R9RepeatsAndStalePlans(SetterCase):
    def test_repeat_of_recorded_plan_writes_nothing(self) -> None:
        store = self.ready()
        self.process(store, self.project(store, "inferred"), revision(policy=INFERRED))
        plan = self.plan(store, "Asia/Kolkata", None)
        first = self.confirm(store, plan)
        self.assertEqual(first.jobs_enqueued, 1)
        for mapping, bound in ((MAP, 3), ({"weight": WPV}, 9)):
            with self.subTest(mapping=mapping):
                again = self.assertWritesNothing(self.confirm, store, plan, TICK, mapping, bound)
                self.assertEqual(again, FallbackTimezoneResult(first.policy, True, 0, 0))
        june = self.setp(store, "Europe/London", JUNE).policy
        september = self.setp(store, "America/New_York", SEPTEMBER).policy
        plan = self.plan(store, "Europe/Paris", JUNE, (september.policy_id,))
        recorded = self.confirm(store, plan)
        self.assertFalse(recorded.repeated)
        again = self.assertWritesNothing(self.confirm, store, plan)
        self.assertEqual(again, FallbackTimezoneResult(recorded.policy, True, 0, 0))
        self.assertEqual(recorded.policy.supersedes, (june.policy_id, september.policy_id))
        # Once the recorded policy is itself replaced, the old plan is stale, not a repeat.
        self.setp(store, "Europe/Rome", JUNE)
        self.assertRefusal("plan-stale", self.confirm, store, plan)

    def test_repeat_succeeds_below_the_reserve(self) -> None:
        store = self.ready()
        plan = self.plan(store, "Asia/Kolkata", None)
        first = self.confirm(store, plan)
        self.free = 0
        again = self.assertWritesNothing(self.confirm, store, plan)
        self.assertEqual(again, FallbackTimezoneResult(first.policy, True, 0, 0))
        self.free = PLENTY
        # A repeat is found by reading, before any write: it succeeds while another
        # connection holds the write lock, where a write would be store-busy.
        busy = self.open(busy_timeout_ms=50)
        blocker = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            again = self.confirm(busy, plan)
        finally:
            blocker.execute("ROLLBACK")
        self.assertEqual(again, FallbackTimezoneResult(first.policy, True, 0, 0))

    def test_changed_timeline_makes_plan_stale(self) -> None:
        store = self.ready()
        # Another first policy lands between plan and confirmation.
        plan = self.plan(store, "Asia/Kolkata", None)
        other = self.direct(store, "Europe/London", None)
        self.assertRefusal("plan-stale", self.confirm, store, plan)
        self.assertEqual(self.timeline(store), (other,))
        # A policy with the same start lands in between.
        plan = self.plan(store, "Asia/Tokyo", JUNE)
        june = self.direct(store, "Europe/Paris", JUNE)
        self.assertRefusal("plan-stale", self.confirm, store, plan)
        # A policy the plan replaces is replaced by someone else in between.
        september = self.setp(store, "America/New_York", SEPTEMBER).policy
        plan = self.plan(store, "Asia/Tokyo", MARCH, (september.policy_id,))
        self.setp(store, "America/Chicago", SEPTEMBER)
        self.assertRefusal("plan-stale", self.confirm, store, plan)
        plan = self.plan(store, "Europe/Berlin", JUNE)
        self.assertEqual(plan.supersedes, (june.policy_id,))
        self.direct(store, "Europe/Rome", JUNE, (june.policy_id,))
        self.assertRefusal("plan-stale", self.confirm, store, plan)
        # An unrelated later change leaves the plan valid.
        plan = self.plan(store, "Asia/Seoul", MARCH)
        self.setp(store, "Asia/Dubai", datetime(2026, 10, 1, tzinfo=UTC))
        self.assertFalse(self.confirm(store, plan).repeated)
        # The identical plan, recorded by another confirmation in between, is a repeat.
        plan = self.plan(store, "Asia/Singapore", datetime(2026, 4, 1, tzinfo=UTC))
        other = self.confirm(store, plan)
        again = self.assertWritesNothing(self.confirm, store, plan)
        self.assertEqual(again, FallbackTimezoneResult(other.policy, True, 0, 0))

    def test_write_between_read_and_commit_is_plan_stale(self) -> None:
        store = self.ready()
        other_handle = self.open()
        first = self.setp(store, "Asia/Kolkata", None).policy
        june = self.setp(store, "Europe/London", JUNE).policy
        observed: dict[str, object] = {}

        def landing(zone: str, start, supersedes=()):
            def write() -> None:
                other_handle.record_fallback_timezone_policy(zone, start, supersedes, {}, 3, at(5000))
                observed["after"] = contents(self.path)
            return write

        cases = [
            # A policy with the plan's start: the store refuses policy-overlap.
            (self.plan(store, "Asia/Tokyo", MARCH), landing("Europe/Paris", MARCH)),
            # A policy the plan supersedes is superseded first: policy-supersession-invalid.
            (self.plan(store, "Asia/Tokyo", JUNE), landing("Europe/Rome", JUNE, (june.policy_id,))),
        ]
        for plan, write in cases:
            with self.subTest(plan=plan):
                interleaved = Interleaved(store, write)
                with self.assertRaises(FallbackTimezoneRefusal) as caught:
                    self.confirm(interleaved, plan)
                self.assertEqual(caught.exception.failure, "plan-stale")
                self.assertCleanChain(caught.exception)
                self.assertEqual(interleaved.writes, 1)
                self.assertEqual(contents(self.path), observed["after"])
        # policy-unknown cannot come from a real write, because policies are never deleted, so it is
        # injected at the write seam: the store's refusal still maps to plan-stale.
        def refuse_unknown() -> None:
            observed["after"] = contents(self.path)
            raise StoreRefusal("policy-unknown")

        interleaved = Interleaved(store, refuse_unknown)
        with self.assertRaises(FallbackTimezoneRefusal) as caught:
            self.confirm(interleaved, self.plan(store, "Asia/Tokyo", datetime(2026, 4, 1, tzinfo=UTC)))
        self.assertEqual(caught.exception.failure, "plan-stale")
        self.assertCleanChain(caught.exception)
        self.assertEqual(interleaved.writes, 1)
        self.assertEqual(contents(self.path), observed["after"])
        # The identical plan, recorded by another handle in between, is a repeat (@5 R46).
        plan = self.plan(store, "Asia/Seoul", SEPTEMBER)
        interleaved = Interleaved(store, landing("Asia/Seoul", SEPTEMBER))
        result = self.confirm(interleaved, plan)
        self.assertEqual(contents(self.path), observed["after"])
        self.assertTrue(result.repeated)
        self.assertEqual((result.jobs_enqueued, result.jobs_rearmed), (0, 0))
        self.assertEqual(result.policy.requested_at, at(5000))
        self.assertEqual(self.timeline(store)[0], first)


# R10 — Reprocessing goes through the store


class R10ReprocessingThroughTheStore(SetterCase):
    def records(self, store):
        inferred = self.project(store, "inferred")
        self.process(store, inferred, revision(policy=INFERRED))
        waiting = self.project(store, "waiting")
        self.process(store, waiting, pending())
        experienced = self.project(store, "experienced")
        self.process(store, experienced, revision())
        weight = self.project(store, "weight", "weight")
        self.process(store, weight, revision(policy=INFERRED, pv=WPV))
        held = self.hold(store, self.project(store, "held"))
        return inferred, waiting, experienced, weight, held

    def test_policy_queues_affected_reprocessing(self) -> None:
        store = self.ready()
        inferred, waiting, experienced, weight, held = self.records(store)
        when = self.tick()
        result = self.confirm(store, self.plan(store, "Asia/Kolkata", None), when=when)
        self.assertEqual((result.jobs_enqueued, result.jobs_rearmed), (3, 1))
        for record in (inferred, waiting, weight):
            job = self.jobs(store, record[0])[-1]
            self.assertEqual((job.state, job.selection_id, job.not_before, job.attempts), ("queued", record[1], when, 0))
        # The held lease runs again when it completes (@5 R48).
        store.complete_processing_job(held.job_id, held.fence, revision(), self.tick())
        self.assertEqual(self.jobs(store, held.record_id)[-1].state, "queued")

    def test_experienced_records_are_not_queued(self) -> None:
        store = self.ready()
        _inferred, _waiting, experienced, _weight, _held = self.records(store)
        before = self.jobs(store, experienced[0])
        self.setp(store, "Asia/Kolkata", None)
        self.setp(store, "Europe/London", JUNE)
        self.assertEqual(self.jobs(store, experienced[0]), before)
        self.assertEqual(before[-1].state, "completed")

    def test_reprocessing_settings_reach_the_store(self) -> None:
        store = self.ready()
        inferred, waiting, _experienced, weight, _held = self.records(store)
        # Affected records under a version that is not @1, and under another record type, for the
        # attempt-bound edges (1 and 100). The store accepts any well-formed version.
        later_version = self.project(store, "later-version", "heart-rate")
        self.process(store, later_version, revision(policy=INFERRED, pv="interpret-heart-rate@3"))
        lowest_bound = self.project(store, "lowest-bound", "sleep-session")
        self.process(store, lowest_bound, revision(policy=INFERRED, pv="interpret-sleep-session@1"))
        steps_before = (self.jobs(store, inferred[0]), self.jobs(store, waiting[0]))
        result = self.confirm(store, self.plan(store, "Asia/Kolkata", None), mapping={"weight": WPV}, max_attempts=7)
        self.assertEqual((result.jobs_enqueued, result.jobs_rearmed), (1, 1))
        self.assertEqual((self.jobs(store, inferred[0]), self.jobs(store, waiting[0])), steps_before)
        job = self.jobs(store, weight[0])[-1]
        self.assertEqual((job.state, job.processor_version, job.max_attempts), ("queued", WPV, 7))
        # An empty mapping queues nothing but still marks held leases.
        result = self.confirm(store, self.plan(store, "Europe/London", JUNE), mapping={})
        self.assertEqual((result.jobs_enqueued, result.jobs_rearmed), (0, 1))
        # The highest bound and a version that is not @1 reach the store unchanged.
        result = self.confirm(store, self.plan(store, "Europe/Paris", MARCH),
                              mapping={"heart-rate": "interpret-heart-rate@3"}, max_attempts=100)
        self.assertEqual((result.jobs_enqueued, result.jobs_rearmed), (1, 1))
        job = self.jobs(store, later_version[0])[-1]
        self.assertEqual((job.state, job.processor_version, job.max_attempts, job.selection_id),
                         ("queued", "interpret-heart-rate@3", 100, later_version[1]))
        # The lowest bound reaches the store unchanged.
        result = self.confirm(store, self.plan(store, "Europe/Rome", SEPTEMBER),
                              mapping={"sleep-session": "interpret-sleep-session@1"}, max_attempts=1)
        self.assertEqual((result.jobs_enqueued, result.jobs_rearmed), (1, 1))
        job = self.jobs(store, lowest_bound[0])[-1]
        self.assertEqual((job.state, job.processor_version, job.max_attempts), ("queued", "interpret-sleep-session@1", 1))

    def test_invalid_reprocessing_settings_pass_through(self) -> None:
        store = self.ready()
        self.records(store)
        plan = self.plan(store, "Asia/Kolkata", None)
        for failure, mapping, bound in (("processor-version-invalid", {"steps": "synthetic"}, 3),
                                        ("processing-request-invalid", MAP, 0),
                                        ("processing-request-invalid", MAP, 101),
                                        ("policy-request-invalid", {"": PV}, 3)):
            with self.subTest(failure=failure, bound=bound):
                before = contents(self.path)
                with self.assertRaises(StoreRefusal) as caught:
                    self.confirm(store, plan, TICK, mapping, bound)
                self.assertIs(type(caught.exception), StoreRefusal)
                self.assertEqual(caught.exception.failure, failure)
                self.assertEqual(contents(self.path), before)
        self.assertEqual(self.timeline(store), ())

    def test_store_retryables_pass_through_and_write_nothing(self) -> None:
        store = self.ready()
        self.records(store)
        plan = self.plan(store, "Asia/Kolkata", None)
        # Below the storage reserve.
        self.free = 0
        before = contents(self.path)
        with self.assertRaises(StoreRetryable) as caught:
            self.confirm(store, plan)
        self.assertEqual((type(caught.exception), caught.exception.failure), (StoreRetryable, "storage-reserve-exhausted"))
        self.assertEqual(contents(self.path), before)
        self.free = PLENTY
        # Another connection holds the write lock.
        busy = self.open(busy_timeout_ms=50)
        blocker = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(StoreRetryable) as caught:
                self.confirm(busy, plan)
            self.assertEqual((type(caught.exception), caught.exception.failure), (StoreRetryable, "store-busy"))
        finally:
            blocker.execute("ROLLBACK")
        self.assertEqual(contents(self.path), before)
        # A fault partway through the store's write commits nothing and is not swallowed.
        faults = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(faults.close)
        tables = [row[0] for row in faults.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        faults.execute("CREATE TABLE test_fault (remaining INTEGER NOT NULL)")
        faults.execute("INSERT INTO test_fault VALUES (1000000)")
        for table in tables:
            faults.execute(f'CREATE TRIGGER "test_fault_{table}" AFTER INSERT ON "{table}" BEGIN '
                           "UPDATE test_fault SET remaining = remaining - 1; "
                           "SELECT RAISE(ABORT, 'injected fault') WHERE (SELECT remaining FROM test_fault) <= 0; END")
        faulted = 0
        for allowed in range(1, 50):
            faults.execute("UPDATE test_fault SET remaining = ?", (allowed,))
            before = contents(self.path)
            try:
                result = self.confirm(store, plan)
            except sqlite3.Error:
                faulted += 1
                self.assertEqual(contents(self.path), before, f"partial commit after {allowed} writes")
                continue
            break
        self.assertGreater(faulted, 0)
        self.assertEqual((result.jobs_enqueued, result.jobs_rearmed), (3, 1))
        self.assertEqual(self.timeline(store), (result.policy,))


# R11 — One write


class R11OneWrite(SetterCase):
    def test_confirmation_is_one_store_write(self) -> None:
        store = self.ready()
        self.process(store, self.project(store, "inferred"), revision(policy=INFERRED))
        self.process(store, self.project(store, "waiting"), pending())
        self.hold(store, self.project(store, "held"))
        self.setp(store, "Asia/Kolkata", None)
        june = self.setp(store, "Europe/London", JUNE).policy
        september = self.setp(store, "America/New_York", SEPTEMBER).policy
        for zone, start, replacing, mapping, bound in (("UTC", None, (), MAP, 3),
                                                       ("Asia/Tokyo", JUNE, (september.policy_id,), {"steps": PV}, 5)):
            with self.subTest(zone=zone):
                when = self.tick()
                plan = self.plan(store, zone, start, replacing, when=when)
                control = self.copy_database()
                result = self.confirm(store, plan, when, mapping, bound)
                replay = self.open(control)
                expected = replay.record_fallback_timezone_policy(plan.zone, plan.effective_from, plan.supersedes,
                                                                  mapping, bound, when)
                replay.close()
                self.assertEqual((result.policy, result.jobs_enqueued, result.jobs_rearmed),
                                 (expected.policy, expected.jobs_enqueued, expected.jobs_rearmed))
                self.assertEqual(contents(self.path), contents(control))
        self.assertNotIn(june, self.timeline(store))


# R12 — Health-data-free, no clock, no logs


class R12HealthDataFreeNoClockNoLogs(SetterCase):
    def marked_store(self):
        store = self.ready()
        record = self.project(store, f"{IDENTITY_MARKER}-1", origin=ORIGIN_MARKER)
        self.process(store, record, revision(policy=INFERRED))
        self.hold(store, self.project(store, f"{IDENTITY_MARKER}-2", origin=ORIGIN_MARKER))
        return store

    def assertNoMarkers(self, text: str) -> None:
        for marker in MARKERS:
            self.assertNotIn(marker, text)

    def failures(self, store) -> dict[str, BaseException]:
        """One instance of each of this unit's failures, raised by the unit."""
        caught: dict[str, BaseException] = {}

        def run(name: str, operation) -> None:
            try:
                operation()
            except FallbackTimezoneRefusal as error:
                caught[name] = error
            else:
                self.fail(f"{name} was not raised")

        unknown = f"Synthetic/{IDENTITY_MARKER}"
        run("all-history-policy-required", lambda: self.plan(store, "Asia/Kolkata", JUNE))
        first = self.setp(store, "Asia/Kolkata", None).policy
        run("timestamp-invalid", lambda: self.plan(store, "Asia/Kolkata", None, when=NAIVE))
        run("timezone-unknown", lambda: self.plan(store, unknown, None))
        run("effective-time-invalid", lambda: self.plan(store, "Asia/Kolkata", datetime(2090, 1, 1, tzinfo=UTC)))
        run("replacement-invalid", lambda: self.plan(store, "Asia/Tokyo", JUNE, (first.policy_id,)))
        run("policy-unchanged", lambda: self.plan(store, "Asia/Kolkata", None))
        run("plan-stale", lambda: self.confirm(store, FallbackTimezonePlan("UTC", None, (), ())))
        return caught

    def test_plans_and_results_are_health_data_free(self) -> None:
        store = self.marked_store()
        plan = self.plan(store, "Asia/Kolkata", None)
        result = self.confirm(store, plan)
        repeat = self.confirm(store, plan)
        for value in (plan, result, repeat, available_zones()):
            self.assertNoMarkers(repr(value) + str(value))
        self.assertEqual(result.jobs_enqueued, 1)

    def test_failures_carry_only_their_name(self) -> None:
        store = self.marked_store()
        caught = self.failures(store)
        self.assertEqual(sorted(caught), sorted(OWN_FAILURES))
        for name, error in caught.items():
            with self.subTest(failure=name):
                self.assertIs(type(error), FallbackTimezoneRefusal)
                self.assertEqual(error.failure, name)  # type: ignore[attr-defined]
                self.assertEqual(error.args, (name,))
                self.assertEqual(str(error), name)
                self.assertCleanChain(error)
                self.assertNotIn("Asia/Kolkata", str(error) + repr(error))

    def test_no_clock_or_host_timezone_is_read(self) -> None:
        store = self.ready()
        past = datetime(2001, 1, 1, tzinfo=UTC)
        future = datetime(2090, 1, 1, tzinfo=UTC)
        result = self.confirm(store, self.plan(store, "Asia/Kolkata", None, when=past), when=past)
        self.assertEqual(result.policy.requested_at, past)
        # The start is compared with the caller's time, never with a clock.
        self.assertPlanRefused("effective-time-invalid", store, "Europe/London", datetime(2010, 1, 1, tzinfo=UTC), when=past)
        far = datetime(2080, 1, 1, tzinfo=UTC)
        plan = self.plan(store, "Europe/London", far, when=future)
        self.assertEqual(self.confirm(store, plan, when=future).policy.requested_at, future)
        # The host timezone does not change a plan.
        original = os.environ.get("TZ")

        def restore() -> None:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time.tzset()

        self.addCleanup(restore)
        plans = []
        for host in ("UTC", "Pacific/Kiritimati", "America/St_Johns"):
            os.environ["TZ"] = host
            time.tzset()
            plans.append(self.plan(store, "Asia/Tokyo", datetime(2050, 1, 1, 12, tzinfo=IST), when=future))
        self.assertEqual(len(set(plans)), 1)

    def test_unit_emits_no_logs(self) -> None:
        with self.assertNoLogs(level=logging.DEBUG):
            store = self.marked_store()
            self.failures(store)
            plan = self.plan(store, "Europe/London", JUNE)
            self.confirm(store, plan)
            self.confirm(store, plan)
            self.free = 0
            with self.assertRaises(StoreRetryable):
                self.confirm(store, self.plan(store, "UTC", None))
            available_zones()


if __name__ == "__main__":
    unittest.main()
