"""Select one Source Record's current evidence by source ordering (reconcile-source-record@1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from activity_tracker_portal.operational_store import (
    OperationalStore,
    Selection,
    SourceRecord,
    StoreRefusal,
    StoreRetryable,
)


class ReconcileRefusal(Exception):
    """A named refusal that recorded nothing."""

    def __init__(self, failure: str) -> None:
        super().__init__(failure)
        self.failure = failure


class ReconcileRetryable(Exception):
    """A named retryable failure that recorded nothing."""

    def __init__(self, failure: str) -> None:
        super().__init__(failure)
        self.failure = failure


@dataclass(frozen=True)
class Decision:
    """The current evidence of one Source Record, by operational-store@3 evidence identity, in delivery order."""

    outcome: Literal["projection", "retraction", "conflict"]
    evidence_ids: tuple[int, ...]


@dataclass(frozen=True)
class ReconcileResult:
    """A decision recorded as a selection, and whether it equals the record's previous selection."""

    record_id: int
    decision: Decision
    selection: Selection
    unchanged: bool


def decide(record: SourceRecord) -> Decision:
    """Choose from indexed evidence in the store's delivery order."""
    newest_time: datetime | None = None
    projections: dict[str, int] = {}
    retraction_id: int | None = None

    for evidence in record.evidence:
        if evidence.kind == "projection":
            assert evidence.last_modified_time is not None
            assert evidence.projection_sha256 is not None
            modified_at = evidence.last_modified_time.astimezone(timezone.utc)
            if newest_time is None or modified_at > newest_time:
                newest_time = modified_at
                projections.clear()
                # A strictly newer projection supersedes all preceding retractions.
                retraction_id = None
            if modified_at == newest_time:
                projections.setdefault(evidence.projection_sha256, evidence.evidence_id)
        elif retraction_id is None:
            # The effective time is the running maximum. Keep the first retraction
            # at that time until a strictly newer projection advances it.
            retraction_id = evidence.evidence_id

    # Only a projection creates a Source Record, including when earlier
    # retractions join it. Thus every record has at least one projection.
    assert newest_time is not None
    if retraction_id is not None:
        return Decision("retraction", (retraction_id,))
    evidence_ids = tuple(projections.values())
    return Decision("projection" if len(evidence_ids) == 1 else "conflict", evidence_ids)


def reconcile(store: OperationalStore, record_id: int, decided_at: datetime) -> ReconcileResult:
    """Record one decision against its read evidence head, without retrying."""
    try:
        with store.read() as view:
            record = view.source_record(record_id)
        if record is None:
            raise ReconcileRefusal("source-record-unknown")

        decision = decide(record)
        # Compare with the latest selection in the read snapshot, before the
        # write (which may return that selection for an identical repeat).
        previous = record.selections[-1] if record.selections else None
        unchanged = previous is not None and (
            previous.outcome, previous.evidence_ids,
        ) == (decision.outcome, decision.evidence_ids)
        assert record.evidence_head is not None
        selection = store.record_selection(
            record_id, decision.outcome, decision.evidence_ids, record.evidence_head, decided_at,
        )
        return ReconcileResult(record_id, decision, selection, unchanged)
    except StoreRefusal as refusal:
        if refusal.failure == "selection-stale":
            raise ReconcileRetryable("evidence-moved") from None
        if refusal.failure == "source-record-unknown":
            raise ReconcileRefusal("source-record-unknown") from None
        raise
    except StoreRetryable as retryable:
        raise ReconcileRetryable(retryable.failure) from None
