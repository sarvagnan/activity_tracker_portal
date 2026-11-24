"""reconcile-source-record@1: select one Source Record's current evidence by source ordering.

Interface stub for packs/reconcile-source-record-1.spec.md; the implementation follows the frozen pack.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from activity_tracker_portal.operational_store import OperationalStore, Selection, SourceRecord


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
    raise NotImplementedError("reconcile-source-record@1")


def reconcile(store: OperationalStore, record_id: int, decided_at: datetime) -> ReconcileResult:
    raise NotImplementedError("reconcile-source-record@1")
