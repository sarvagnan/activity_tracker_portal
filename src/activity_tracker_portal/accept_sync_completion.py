"""Accept one delivered Snapshot Completion or Change Completion (accept-sync-completion@1)."""

from __future__ import annotations

from datetime import datetime

from activity_tracker_portal.accept_ingest_batch import DeliveryResponse
from activity_tracker_portal.operational_store import OperationalStore


def accept_sync_completion(store: OperationalStore, data: bytes, now: datetime) -> DeliveryResponse:
    """Answer one delivered Snapshot Completion or Change Completion."""
    raise NotImplementedError("accept-sync-completion@1 is not implemented")
