"""Portal side of the Sender Installation lifecycle (manage-sender-installation@1).

The Operational Store owns the lifecycle state machine and its transactions;
this module decides when each transition may happen and answers the Sender
under the Operator-mediated Wire Protocol Version 1 lifecycle.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from activity_tracker_portal.operational_store import InstallationStatus, OperationalStore

ENROLLMENT_CODE_LIFETIME = timedelta(minutes=10)


class InstallationRefusal(Exception):
    """A named refusal of an Operator intent that changed nothing."""

    def __init__(self, failure: str) -> None:
        super().__init__(failure)
        self.failure = failure


class InstallationRetryable(Exception):
    """A named transient store condition that changed nothing."""

    def __init__(self, failure: str) -> None:
        super().__init__(failure)
        self.failure = failure


@dataclass(frozen=True)
class Capabilities:
    """The configured content of a capability advertisement, without its generation."""

    wire_versions: tuple[int, ...]
    projection_schemas: tuple[tuple[str, tuple[int, ...]], ...]
    max_record_count: int
    max_message_bytes: int


@dataclass(frozen=True)
class EnrollmentOffer:
    code: str
    expires_at: datetime
    message: bytes


ConfirmationOutcome = Literal["pending", "retryable", "refused"]


@dataclass(frozen=True)
class ConfirmationResponse:
    outcome: ConfirmationOutcome
    body: bytes | None
    failure: str | None


def offer_enrollment(
    store: OperationalStore,
    portal_url: str,
    now: datetime,
    *,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> EnrollmentOffer:
    raise NotImplementedError("manage-sender-installation@1 is not implemented")


def answer_enrollment_confirmation(store: OperationalStore, data: bytes, now: datetime) -> ConfirmationResponse:
    raise NotImplementedError("manage-sender-installation@1 is not implemented")


def confirm_installation(store: OperationalStore, installation_id: str, fingerprint: str, now: datetime) -> InstallationStatus:
    raise NotImplementedError("manage-sender-installation@1 is not implemented")


def withdraw_installation(store: OperationalStore, installation_id: str, now: datetime) -> InstallationStatus:
    raise NotImplementedError("manage-sender-installation@1 is not implemented")


def begin_draining(store: OperationalStore, installation_id: str, now: datetime) -> InstallationStatus:
    raise NotImplementedError("manage-sender-installation@1 is not implemented")


def retire_drained(store: OperationalStore, installation_id: str, final_sequence: int, now: datetime) -> InstallationStatus:
    raise NotImplementedError("manage-sender-installation@1 is not implemented")


def retire_destructively(
    store: OperationalStore,
    installation_id: str,
    now: datetime,
    *,
    acknowledge_possible_loss: bool,
) -> InstallationStatus:
    raise NotImplementedError("manage-sender-installation@1 is not implemented")


def honor_capabilities(store: OperationalStore, capabilities: Capabilities, now: datetime) -> int:
    raise NotImplementedError("manage-sender-installation@1 is not implemented")


def stop_superseded_generation(store: OperationalStore, now: datetime) -> int | None:
    raise NotImplementedError("manage-sender-installation@1 is not implemented")


def current_advertisement(store: OperationalStore) -> bytes:
    raise NotImplementedError("manage-sender-installation@1 is not implemented")
