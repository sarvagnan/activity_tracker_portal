"""execute-processing-job@1: execute one leased Processing Job (packs/execute-processing-job-1.spec.md).

Interface stub. The registry, the result and retry dataclasses, and the defaults are
the approved interface; every operation raises NotImplementedError until the unit is
implemented against its frozen pack.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Literal

from .interpret_heart_rate import PROCESSOR as _HEART_RATE
from .interpret_sleep_session import PROCESSOR as _SLEEP_SESSION
from .interpret_source_projection import COMMON_VERSION, FallbackTimezonePolicy, Processor
from .interpret_steps import PROCESSOR as _STEPS
from .interpret_weight import PROCESSOR as _WEIGHT
from .operational_store import Lease

_NOT_IMPLEMENTED = "execute-processing-job@1"

# Each published processor version maps to its processor; the common version maps to
# no processor, so interpret records Unmodeled (Decision 2).
PROCESSORS: Mapping[str, Processor | None] = MappingProxyType({
    "interpret-steps@1": _STEPS,
    "interpret-heart-rate@1": _HEART_RATE,
    "interpret-sleep-session@1": _SLEEP_SESSION,
    "interpret-weight@1": _WEIGHT,
    COMMON_VERSION: None,
})

_MODELED = {
    "heart-rate": "interpret-heart-rate@1",
    "sleep-session": "interpret-sleep-session@1",
    "steps": "interpret-steps@1",
    "weight": "interpret-weight@1",
}
_UNMODELED = (
    "active-calories-burned",
    "basal-body-temperature",
    "basal-metabolic-rate",
    "blood-glucose",
    "blood-pressure",
    "body-fat",
    "body-temperature",
    "body-water-mass",
    "bone-mass",
    "cervical-mucus",
    "cycling-pedaling-cadence",
    "distance",
    "elevation-gained",
    "exercise-session",
    "floors-climbed",
    "heart-rate-variability-rmssd",
    "height",
    "hydration",
    "intermenstrual-bleeding",
    "lean-body-mass",
    "menstruation-flow",
    "menstruation-period",
    "mindfulness-session",
    "nutrition",
    "ovulation-test",
    "oxygen-saturation",
    "planned-exercise-session",
    "power",
    "respiratory-rate",
    "resting-heart-rate",
    "sexual-activity",
    "skin-temperature",
    "speed",
    "steps-cadence",
    "total-calories-burned",
    "vo2-max",
    "wheelchair-pushes",
)

# Every projection/v1 record type maps to the processor version its jobs run under.
PROCESSOR_VERSIONS: Mapping[str, str] = MappingProxyType(dict(sorted(
    {**_MODELED, **{record_type: COMMON_VERSION for record_type in _UNMODELED}}.items()
)))

Disposition = Literal["outcome-recorded", "retry-scheduled", "job-failed", "already-recorded", "lease-lost"]


@dataclass(frozen=True)
class RetryPolicy:
    """Transient backoff: base_seconds doubled per attempt used, capped at max_seconds (Decision 8)."""

    base_seconds: int = 60
    max_seconds: int = 3600

    def delay(self, attempt: int) -> timedelta:
        """min(base_seconds * 2 ** (attempt - 1), max_seconds) seconds, for attempt 1 and up."""
        raise NotImplementedError(_NOT_IMPLEMENTED)


@dataclass(frozen=True)
class ExecutionResult:
    """A health-data-free account of one execution."""

    job_id: int
    fence: int
    disposition: Disposition
    outcome_kind: str | None
    reason: str | None
    retry_at: datetime | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def execute_processing_job(
    store: object,
    lease: Lease,
    clock: Callable[[], datetime],
    *,
    processors: Mapping[str, Processor | None] = PROCESSORS,
    policies: Sequence[FallbackTimezonePolicy] = (),
    retry: RetryPolicy = RetryPolicy(),
) -> ExecutionResult:
    """Interpret the leased selection and commit exactly one result under the lease's fence."""
    raise NotImplementedError(_NOT_IMPLEMENTED)


def make_job_handler(
    writer: object, *, clock: Callable[[], datetime] = _utc_now,
) -> Callable[[Lease], ExecutionResult]:
    """A worker handler that runs execute_processing_job through writer.run with the defaults."""
    raise NotImplementedError(_NOT_IMPLEMENTED)
