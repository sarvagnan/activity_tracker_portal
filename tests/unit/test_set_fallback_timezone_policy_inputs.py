"""Unbound regressions for caller-owned values at the timezone setter boundary."""

from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from activity_tracker_portal.operational_store import FallbackTimezonePolicyRecord, open_store
from activity_tracker_portal.set_fallback_timezone_policy import (
    FallbackTimezonePlan,
    FallbackTimezoneRefusal,
    FallbackTimezoneResult,
    plan_fallback_timezone_policy,
    set_fallback_timezone_policy,
)

MARKER = "synthetic-caller-private-marker"
UTC = timezone.utc
AT = datetime(2026, 11, 25, tzinfo=UTC)
JUNE = datetime(2026, 6, 1, tzinfo=UTC)
SEPTEMBER = datetime(2026, 9, 1, tzinfo=UTC)


def forbidden(*args, **kwargs):
    raise AssertionError("caller override was dispatched")


class TaggedTuple(tuple):
    __iter__ = __contains__ = __eq__ = __hash__ = forbidden
    __len__ = __getitem__ = __repr__ = forbidden


class ChangingTuple(tuple):
    """The original validator accepted two traversals, then copied tagged IDs."""

    def __new__(cls, values):
        result = super().__new__(cls, values)
        result.iterations = 0
        result.marker = MARKER
        return result

    def __iter__(self):
        self.iterations += 1
        if self.iterations <= 2:
            return tuple.__iter__(self)
        return iter(tagged(TaggedInt(value)) for value in tuple.__iter__(self))


class ClaimsEveryMember(tuple):
    def __contains__(self, value):
        return True


class TaggedInt(int):
    __eq__ = __hash__ = __int__ = __index__ = forbidden
    __lt__ = __le__ = __gt__ = __ge__ = __repr__ = forbidden


class TaggedString(str):
    __str__ = __eq__ = __hash__ = __iter__ = __contains__ = forbidden
    __repr__ = forbidden


class TaggedDatetime(datetime):
    __eq__ = __hash__ = __lt__ = __le__ = __gt__ = __ge__ = forbidden
    astimezone = utcoffset = replace = __repr__ = forbidden

    # Reading through the subclass must not substitute any timestamp component.
    year = month = day = hour = minute = second = microsecond = property(forbidden)
    tzinfo = fold = property(forbidden)


def tagged(value):
    value.marker = MARKER
    return value


class RecordingStore:
    def __init__(self, store):
        self.store = store
        self.calls = []

    @contextmanager
    def read(self):
        with self.store.read() as view:
            yield view

    def record_fallback_timezone_policy(self, *args):
        self.calls.append(args)
        return self.store.record_fallback_timezone_policy(*args)


class SetFallbackTimezonePolicyInputsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = open_store(Path(directory.name) / "store.sqlite3", reserve_bytes=0,
                                free_bytes=lambda _: 10**12)
        self.addCleanup(self.store.close)

    def seed(self):
        self.store.record_fallback_timezone_policy("UTC", None, (), {}, 3, AT)
        return self.store.record_fallback_timezone_policy(
            "Europe/London", SEPTEMBER, (), {}, 3, AT,
        ).policy.policy_id

    def plan(self, zone="Asia/Tokyo", start=JUNE, replacing=(), at=AT):
        with self.store.read() as view:
            return plan_fallback_timezone_policy(view, zone, start, replacing=replacing, at=at)

    def assert_plain(self, value):
        self.assertNotIn(MARKER, repr(value))
        if is_dataclass(value):
            self.assertIn(type(value), (FallbackTimezonePlan, FallbackTimezoneResult,
                                       FallbackTimezonePolicyRecord))
            self.assertEqual(set(vars(value)), {field.name for field in fields(value)})
            for field in fields(value):
                self.assert_plain(getattr(value, field.name))
        elif type(value) is tuple:
            for member in value:
                self.assert_plain(member)
        elif type(value) is datetime:
            self.assertIs(value.tzinfo, UTC)
            self.assertFalse(hasattr(value, "__dict__"))
        else:
            self.assertIn(type(value), (str, int, bool, type(None)))
            self.assertFalse(hasattr(value, "__dict__"))

    def assert_refusal(self, failure, call, *args, **kwargs):
        with self.assertRaises(FallbackTimezoneRefusal) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.args, (failure,))
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(MARKER, repr(vars(caught.exception)))

    def test_validation_and_output_share_the_base_tuple_snapshot(self):
        later = self.seed()
        replacing = ChangingTuple((later,))
        plan = self.plan(replacing=replacing)
        self.assertEqual((plan.replacing, plan.supersedes), ((later,), (later,)))
        self.assert_plain(plan)
        self.assertEqual(replacing.iterations, 0)

    def test_tuple_overrides_cannot_affect_planning_confirmation_or_repeat(self):
        later = self.seed()
        plan = self.plan(replacing=tagged(TaggedTuple((later,))))
        self.assert_plain(plan)
        confirmed = FallbackTimezonePlan(plan.zone, plan.effective_from,
                                         tagged(TaggedTuple(plan.replacing)),
                                         tagged(TaggedTuple(plan.supersedes)))
        store = RecordingStore(self.store)
        result = set_fallback_timezone_policy(store, confirmed,
                                             processor_versions={}, max_attempts=3, at=AT)
        self.assert_plain(result)
        self.assertEqual(result.policy.supersedes, (later,))
        for value in store.calls[0][:3] + store.calls[0][5:]:
            self.assert_plain(value)
        repeated = set_fallback_timezone_policy(store, confirmed,
                                               processor_versions={}, max_attempts=3, at=AT)
        self.assertTrue(repeated.repeated)
        self.assertEqual(len(store.calls), 1)
        self.assert_plain(repeated)

    def test_contains_override_cannot_forge_a_repeat(self):
        first = self.store.record_fallback_timezone_policy("UTC", None, (), {}, 3, AT).policy
        plan = FallbackTimezonePlan("UTC", None, (999,), tagged(ClaimsEveryMember(())))
        self.assert_refusal("replacement-invalid", set_fallback_timezone_policy, self.store,
                            plan, processor_versions={}, max_attempts=3, at=AT)
        with self.store.read() as view:
            self.assertEqual(view.fallback_timezone_timeline(), (first,))

    def test_subset_failure_keeps_precedence_over_invalid_id_range_or_duplicates(self):
        later = self.seed()
        for supersedes in ((0,), (2**63,), (999, 999)):
            with self.subTest(supersedes=supersedes):
                plan = FallbackTimezonePlan("Asia/Tokyo", JUNE, (later,), supersedes)
                self.assert_refusal("replacement-invalid", set_fallback_timezone_policy,
                                    self.store, plan, processor_versions={}, max_attempts=3, at=AT)
        plan = FallbackTimezonePlan("Asia/Tokyo", JUNE, (later,), (later, later))
        self.assert_refusal("plan-stale", set_fallback_timezone_policy, self.store, plan,
                            processor_versions={}, max_attempts=3, at=AT)

    def test_string_and_datetime_components_use_base_values(self):
        later = self.seed()
        offset = timezone(timedelta(hours=5, minutes=30))
        start = tagged(TaggedDatetime(2026, 6, 1, 5, 30, tzinfo=offset))
        at = tagged(TaggedDatetime(2026, 11, 25, 5, 30, tzinfo=offset))
        zone = tagged(TaggedString("Asia/Tokyo"))
        plan = self.plan(zone=zone, start=start, replacing=(later,), at=at)
        self.assertEqual((plan.zone, plan.effective_from), ("Asia/Tokyo", JUNE))
        self.assert_plain(plan)
        store = RecordingStore(self.store)
        confirmed = FallbackTimezonePlan(zone, start, plan.replacing, plan.supersedes)
        result = set_fallback_timezone_policy(store, confirmed,
                                             processor_versions={}, max_attempts=3, at=at)
        self.assert_plain(result)
        self.assertEqual(store.calls[0][5], AT)
        for value in store.calls[0][:3] + store.calls[0][5:]:
            self.assert_plain(value)

    def test_int_subclasses_are_refused_without_comparison_or_hashing(self):
        later = self.seed()
        identity = tagged(TaggedInt(later))
        self.assert_refusal("replacement-invalid", self.plan, replacing=(identity,))
        plan = FallbackTimezonePlan("Asia/Tokyo", JUNE, (), (identity,))
        self.assert_refusal("plan-stale", set_fallback_timezone_policy, self.store, plan,
                            processor_versions={}, max_attempts=3, at=AT)

    def test_invalid_supersedes_elements_are_never_compared_to_replacing(self):
        later = self.seed()
        plan = FallbackTimezonePlan("Asia/Tokyo", JUNE, (later,),
                                    tagged(TaggedTuple((tagged(TaggedInt(later)),))))
        self.assert_refusal("plan-stale", set_fallback_timezone_policy, self.store, plan,
                            processor_versions={}, max_attempts=3, at=AT)


if __name__ == "__main__":
    unittest.main()
