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
from datetime import datetime

from .operational_store import FallbackTimezonePolicyRecord, OperationalStore, StoreView

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
    raise NotImplementedError("set-fallback-timezone-policy@1")


def plan_fallback_timezone_policy(
    view: StoreView,
    zone: str,
    effective_from: datetime | None,
    *,
    replacing: tuple[int, ...] = (),
    at: datetime,
) -> FallbackTimezonePlan:
    raise NotImplementedError("set-fallback-timezone-policy@1")


def set_fallback_timezone_policy(
    store: OperationalStore,
    plan: FallbackTimezonePlan,
    *,
    processor_versions: Mapping[str, str],
    max_attempts: int,
    at: datetime,
) -> FallbackTimezoneResult:
    raise NotImplementedError("set-fallback-timezone-policy@1")
