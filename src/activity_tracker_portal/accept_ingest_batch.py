"""Accept one delivered Ingest Batch (accept-ingest-batch@1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from activity_tracker_portal.operational_store import OperationalStore

Outcome = Literal["receipt", "retryable", "block"]


@dataclass(frozen=True)
class DeliveryResponse:
    """One protocol answer: its outcome, exact bytes, named failure, and any accepted sequence gap."""

    outcome: Outcome
    body: bytes
    failure: str | None
    gap_after: int | None = None


def accept_ingest_batch(store: OperationalStore, data: bytes, now: datetime) -> DeliveryResponse:
    """Answer one delivered Ingest Batch, or bytes that are not a valid Outbox Message of another kind."""
    raise NotImplementedError("accept-ingest-batch@1 is not implemented")
