"""set-fallback-timezone-policy@1: plan and record a pending Fallback Timezone Policy
(packs/set-fallback-timezone-policy-1.spec.md).

A read-only plan validates the Operator's zone against the pinned tzdata package,
checks the start, and works out which live policies the new one replaces. Setting
a confirmed plan records it through operational-store@5, which queues the affected
reprocessing in the same transaction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import files

from .operational_store import (
    FallbackTimezonePolicyRecord,
    OperationalStore,
    StoreRefusal,
    StoreView,
)

# This unit's own failure names. The store's failures pass through unchanged.
FAILURES = frozenset(
    {
        "timestamp-invalid",
        "timezone-unknown",
        "effective-time-invalid",
        "replacement-invalid",
        "all-history-policy-required",
        "policy-unchanged",
        "plan-stale",
    }
)
# Zone names the pinned tzdata package lists but the Operator may not choose (Decision 2).
EXCLUDED_ZONES = frozenset({"Factory"})


@dataclass(frozen=True)
class FallbackTimezonePlan:
    """A read-only plan: the zone, the start in UTC (None for all earlier history), the later
    policies named for replacement, and every live policy the new one supersedes, ascending."""

    zone: str
    effective_from: datetime | None
    replacing: tuple[int, ...]
    supersedes: tuple[int, ...]


@dataclass(frozen=True)
class FallbackTimezoneResult:
    """The recorded pending policy, whether it was already recorded, and the store's counts."""

    policy: FallbackTimezonePolicyRecord
    repeated: bool
    jobs_enqueued: int
    jobs_rearmed: int


class FallbackTimezoneRefusal(Exception):
    """One of this unit's named failures; nothing was recorded. Its text is the name only."""

    def __init__(self, failure: str) -> None:
        super().__init__(failure)
        self.failure = failure


def available_zones() -> tuple[str, ...]:
    """Return exact names from the pinned package, including its links."""
    names = files("tzdata").joinpath("zones").read_text(encoding="utf-8").splitlines()
    return tuple(sorted(set(names) - EXCLUDED_ZONES))


def _utc(value: datetime) -> datetime | None:
    """Normalize an aware, representable instant into a plain UTC value."""
    if not isinstance(value, datetime):
        return None
    try:
        # Rebuild before conversion so datetime subclasses cannot carry caller
        # state into a plan, or override the conversion itself.
        plain = datetime(
            value.year, value.month, value.day, value.hour, value.minute,
            value.second, value.microsecond, tzinfo=value.tzinfo, fold=value.fold,
        )
        if plain.utcoffset() is not None:
            return plain.astimezone(timezone.utc)
    except (ValueError, OverflowError, TypeError):
        pass
    return None


def _valid_policy_ids(value: tuple[int, ...]) -> bool:
    return (
        isinstance(value, tuple)
        and all(type(identity) is int and 0 < identity < 2**63 for identity in value)
        and len(set(value)) == len(value)
    )


def _validate_request(
    zone: str,
    effective_from: datetime | None,
    replacing: tuple[int, ...],
    at: datetime,
) -> tuple[str, datetime | None, tuple[int, ...], datetime]:
    instant = _utc(at)
    if instant is None:
        raise FallbackTimezoneRefusal("timestamp-invalid")
    if not isinstance(zone, str):
        raise FallbackTimezoneRefusal("timezone-unknown")
    # str.__str__ reads the string's value without retaining subclass metadata
    # or calling a caller-supplied __str__ override.
    zone = str.__str__(zone)
    if zone not in available_zones():
        raise FallbackTimezoneRefusal("timezone-unknown")
    start = None if effective_from is None else _utc(effective_from)
    if effective_from is not None and (start is None or start > instant):
        raise FallbackTimezoneRefusal("effective-time-invalid")
    if not _valid_policy_ids(replacing):
        raise FallbackTimezoneRefusal("replacement-invalid")
    return zone, start, tuple(identity for identity in replacing), instant


def _plan_from_timeline(
    timeline: tuple[FallbackTimezonePolicyRecord, ...],
    zone: str,
    start: datetime | None,
    replacing: tuple[int, ...],
) -> FallbackTimezonePlan:
    if start is not None and not any(policy.effective_from is None for policy in timeline):
        raise FallbackTimezoneRefusal("all-history-policy-required")
    by_id = {policy.policy_id: policy for policy in timeline}
    for identity in replacing:
        policy = by_id.get(identity)
        if (
            policy is None or policy.effective_from is None
            or (start is not None and policy.effective_from <= start)
        ):
            raise FallbackTimezoneRefusal("replacement-invalid")
    same_start = next((policy for policy in timeline if policy.effective_from == start), None)
    if same_start is not None and same_start.zone == zone and not replacing:
        raise FallbackTimezoneRefusal("policy-unchanged")
    supersedes = replacing + (() if same_start is None else (same_start.policy_id,))
    return FallbackTimezonePlan(zone, start, tuple(sorted(replacing)), tuple(sorted(supersedes)))


def plan_fallback_timezone_policy(
    view: StoreView,
    zone: str,
    effective_from: datetime | None,
    *,
    replacing: tuple[int, ...] = (),
    at: datetime,
) -> FallbackTimezonePlan:
    zone, start, replacing, _ = _validate_request(zone, effective_from, replacing, at)
    return _plan_from_timeline(view.fallback_timezone_timeline(), zone, start, replacing)


def set_fallback_timezone_policy(
    store: OperationalStore,
    plan: FallbackTimezonePlan,
    *,
    processor_versions: Mapping[str, str],
    max_attempts: int,
    at: datetime,
) -> FallbackTimezoneResult:
    zone, start, replacing, instant = _validate_request(
        plan.zone, plan.effective_from, plan.replacing, at,
    )
    if not isinstance(plan.supersedes, tuple):
        raise FallbackTimezoneRefusal("plan-stale")
    if any(identity not in plan.supersedes for identity in replacing):
        raise FallbackTimezoneRefusal("replacement-invalid")
    if not _valid_policy_ids(plan.supersedes):
        raise FallbackTimezoneRefusal("plan-stale")
    supersedes = tuple(identity for identity in plan.supersedes)
    confirmed = FallbackTimezonePlan(zone, start, replacing, supersedes)
    with store.read() as view:
        timeline = view.fallback_timezone_timeline()
    for policy in timeline:
        if (policy.zone, policy.effective_from, policy.supersedes) == (zone, start, supersedes):
            return FallbackTimezoneResult(policy, True, 0, 0)

    proposed = None
    try:
        proposed = _plan_from_timeline(timeline, zone, start, replacing)
    except FallbackTimezoneRefusal:
        pass
    # Raise outside the handler: even a suppressed context would retain text
    # from the refusal that this confirmation translates into plan-stale.
    if proposed != confirmed:
        raise FallbackTimezoneRefusal("plan-stale")

    recording = None
    try:
        recording = store.record_fallback_timezone_policy(
            zone, start, supersedes, processor_versions, max_attempts, instant,
        )
    except StoreRefusal as error:
        if error.failure not in {"policy-overlap", "policy-supersession-invalid", "policy-unknown"}:
            raise
    if recording is None:
        raise FallbackTimezoneRefusal("plan-stale")
    # The store does not expose insertion vs. repeat. Different request times
    # identify concurrent repeats; identical times are the disclosed residual.
    repeated = recording.policy.requested_at != instant
    return FallbackTimezoneResult(
        recording.policy, repeated,
        0 if repeated else recording.jobs_enqueued,
        0 if repeated else recording.jobs_rearmed,
    )
