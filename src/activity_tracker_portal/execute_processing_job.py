"""execute-processing-job@1: execute one leased Processing Job (packs/execute-processing-job-1.spec.md).

Assemble the leased input from one store snapshot, interpret it, and persist the
outcome or a bounded failure through the published fenced store operations.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Literal

from .interpret_heart_rate import PROCESSOR as _HEART_RATE
from .interpret_sleep_session import PROCESSOR as _SLEEP_SESSION
from .interpret_source_projection import (
    COMMON_VERSION,
    CanonicalRevision,
    FallbackTimezonePolicy,
    InterpretFailure,
    PolicyPending,
    ProcessingInput,
    Processor,
    Quarantine,
    Unmodeled,
    interpret,
)
from .interpret_steps import PROCESSOR as _STEPS
from .interpret_weight import PROCESSOR as _WEIGHT
from .operational_store import (
    FailureKind,
    Lease,
    OutcomeEntry,
    ProcessingJob,
    RevisionEntry,
    StoreRefusal,
    StoreView,
)

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
        return timedelta(seconds=min(self.base_seconds * 2 ** (attempt - 1), self.max_seconds))


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


def _lease_result(job: ProcessingJob | None, lease: Lease) -> ExecutionResult | None:
    """Recognize a closed own attempt before considering a later claim's fence."""
    if job is None:
        raise StoreRefusal("processing-job-unknown")
    attempt = next((item for item in job.history if item.fence == lease.fence), None)
    if attempt is not None and attempt.result in (
        "completed", "transient-failure", "deterministic-failure",
    ):
        return ExecutionResult(lease.job_id, lease.fence, "already-recorded", None, None, None)
    if (
        job.fence != lease.fence or job.state != "leased"
        or (attempt is not None and attempt.result == "lease-expired")
    ):
        return ExecutionResult(lease.job_id, lease.fence, "lease-lost", None, None, None)
    return None


def _processing_input(view: StoreView, lease: Lease) -> ProcessingInput | None:
    """Read only the leased evidence, including the schema version of its batch."""
    record = view.source_record(lease.record_id)
    if record is None:
        return None
    evidence = next((item for item in record.evidence if item.evidence_id == lease.evidence_id), None)
    if evidence is None:
        return None
    message = view.archived_message(
        installation_id=evidence.position.installation_id, sequence=evidence.position.sequence,
    )
    projection = view.projection_bytes(lease.evidence_id)
    if message is None or message.batch is None or projection is None:
        return None
    return ProcessingInput(
        source_system_kind=record.key.source_system_kind,
        origin=record.key.origin,
        record_type=record.key.record_type,
        source_identity=record.key.source_identity,
        record_id=lease.record_id,
        selection_id=lease.selection_id,
        evidence_id=lease.evidence_id,
        projection_sha256=evidence.projection_sha256,
        last_modified_time=evidence.last_modified_time,
        projection_schema_version=message.batch.projection_schema_version,
        projection=projection,
    )


def _outcome_entry(outcome: CanonicalRevision | Unmodeled | PolicyPending | Quarantine) -> OutcomeEntry:
    """Preserve opaque bytes and use the typed temporal header for indexed columns."""
    if isinstance(outcome, CanonicalRevision):
        start, end = outcome.header.time.start, outcome.header.time.end
        revision = RevisionEntry(
            header_bytes=outcome.header_bytes,
            body_bytes=outcome.body_bytes,
            body_version=outcome.header.body_schema.body_version,
            start_epoch_ns=start.instant.epoch_ns,
            end_epoch_ns=None if end is None else end.instant.epoch_ns,
            start_local_date=start.local_date,
            end_local_date=None if end is None else end.local_date,
            start_attribution=start.attribution,
            end_attribution=None if end is None else end.attribution,
        )
        return OutcomeEntry("revision", outcome.processor_version, outcome.policy_identity, None, None, None, revision)
    if isinstance(outcome, Unmodeled):
        return OutcomeEntry(
            "unmodeled", outcome.processor_version, outcome.policy_identity, outcome.reason, None, None, None,
        )
    if isinstance(outcome, PolicyPending):
        return OutcomeEntry(
            "policy-pending", outcome.processor_version, outcome.policy_identity,
            outcome.reason, None, outcome.policy_kind, None,
        )
    return OutcomeEntry(
        "quarantine", outcome.processor_version, outcome.policy_identity,
        outcome.reason, outcome.pointer, None, None,
    )


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
    with store.read() as view:
        job = view.processing_job(lease.job_id)
        prior = _lease_result(job, lease)
        if prior is not None:
            return prior
        processing_input = _processing_input(view, lease)

    reason = None
    kind: FailureKind = "deterministic"
    outcome = None
    if processing_input is None:
        reason = "processing-evidence-missing"
    elif lease.processor_version not in processors:
        reason = "processor-unavailable"
    else:
        processor = processors[lease.processor_version]
        if processor is not None and processor.record_type != processing_input.record_type:
            reason = "processor-record-type-mismatch"
        else:
            try:
                outcome = interpret(processing_input, processor, policies)
            except InterpretFailure as error:
                if error.failure in (
                    "processing-input-invalid", "projection-digest-mismatch",
                    "projection-identity-mismatch", "processor-invariant-violated",
                ):
                    reason = error.failure
                else:
                    reason = "interpretation-unexpected-failure"
                    kind = "transient"
            except Exception:
                reason = "interpretation-unexpected-failure"
                kind = "transient"

    # Leave the interpreter's exception handler before reading time or writing.
    # Otherwise a later store failure could retain health data in its context.
    entry = None if outcome is None else _outcome_entry(outcome)
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise StoreRefusal("timestamp-invalid")
    # Backoff is an elapsed duration, including inside a local DST fold.
    now = now.astimezone(timezone.utc)
    retry_at = now + retry.delay(job.attempts) if kind == "transient" else None
    lease_lost = False
    if entry is not None:
        try:
            recorded = store.complete_processing_job(
                job_id=lease.job_id, fence=lease.fence, outcome=entry, at=now,
            )
        except StoreRefusal as error:
            if error.failure == "lease-not-held":
                lease_lost = True
            elif error.failure in ("outcome-conflict", "outcome-invalid"):
                reason = error.failure
            else:
                raise
        else:
            return ExecutionResult(
                lease.job_id, lease.fence, "outcome-recorded", recorded.kind, recorded.reason, None,
            )

    # An interpretation failure or refused outcome-conflict/outcome-invalid
    # completion gets one failure write. Other write errors propagate unchanged.
    if not lease_lost:
        try:
            failed = store.fail_processing_job(
                job_id=lease.job_id, fence=lease.fence, kind=kind,
                reason=reason, retry_at=retry_at, at=now,
            )
        except StoreRefusal as error:
            if error.failure != "lease-not-held":
                raise
        else:
            if failed.state == "queued":
                return ExecutionResult(
                    lease.job_id, lease.fence, "retry-scheduled", None, reason, failed.not_before,
                )
            return ExecutionResult(lease.job_id, lease.fence, "job-failed", None, failed.failure, None)

    # A competing writer may have closed this very attempt or claimed a new one.
    # Resolve that race from a fresh history snapshot, without another write.
    with store.read() as view:
        prior = _lease_result(view.processing_job(lease.job_id), lease)
    return prior or ExecutionResult(lease.job_id, lease.fence, "lease-lost", None, None, None)


def make_job_handler(
    writer: object, *, clock: Callable[[], datetime] = _utc_now,
) -> Callable[[Lease], ExecutionResult]:
    """A worker handler that runs execute_processing_job through writer.run with the defaults."""
    def handle(lease: Lease) -> ExecutionResult:
        return writer.run(lambda store: execute_processing_job(store, lease, clock))

    return handle
