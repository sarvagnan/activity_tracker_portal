"""Accept one delivered Sync Checkpoint and report polling state (accept-sync-checkpoint@1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from activity_tracker_portal.accept_ingest_batch import DeliveryResponse
from activity_tracker_portal.operational_store import ArchivedCheckpoint, OperationalStore

Freshness = Literal["never-reported", "current", "delayed", "stale"]


@dataclass(frozen=True)
class SchedulingReport:
    """The last reported Background Scheduling Status, its Portal receipt time, and whether it is stale."""

    status: str
    receipt_time: datetime
    stale: bool


@dataclass(frozen=True)
class PollingState:
    """One Sender Installation's polling state at a Portal read time."""

    installation_id: str
    freshness: Freshness
    latest_checkpoint: ArchivedCheckpoint | None
    first_poll_in_progress: bool
    first_checkpoint_overdue: bool
    background_scheduling: SchedulingReport | None
    conditions: frozenset[str]


def accept_sync_checkpoint(store: OperationalStore, data: bytes, now: datetime) -> DeliveryResponse:
    """Answer one valid Sync Checkpoint, or raise ValueError for a caller error."""
    raise NotImplementedError("accept-sync-checkpoint@1 is not implemented")


def polling_state(store: OperationalStore, installation_id: str, now: datetime) -> PollingState:
    """Report one installation's polling state at `now`, writing nothing."""
    raise NotImplementedError("accept-sync-checkpoint@1 is not implemented")


def clock_conditions(checkpoint: ArchivedCheckpoint, enrolled_at: datetime) -> frozenset[str]:
    """The clock-skew conditions of one archived checkpoint for an installation enrolled at `enrolled_at`."""
    raise NotImplementedError("accept-sync-checkpoint@1 is not implemented")
