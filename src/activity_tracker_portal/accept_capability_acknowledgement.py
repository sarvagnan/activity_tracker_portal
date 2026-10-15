"""Accept one delivered capability acknowledgement (accept-capability-acknowledgement@1)."""

from __future__ import annotations

from datetime import datetime

from activity_tracker_portal.accept_ingest_batch import DeliveryResponse
from activity_tracker_portal.operational_store import OperationalStore


def accept_capability_acknowledgement(store: OperationalStore, data: bytes, now: datetime) -> DeliveryResponse:
    """Answer one delivered capability acknowledgement."""
    raise NotImplementedError("accept-capability-acknowledgement@1 is not implemented")
