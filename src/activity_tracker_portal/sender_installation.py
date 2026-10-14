"""Portal side of the Sender Installation lifecycle (manage-sender-installation@1).

The Operational Store owns the lifecycle state machine and its transactions;
this module decides when each transition may happen and answers the Sender
under the Operator-mediated Wire Protocol Version 1 lifecycle.

The ingress shell serializes store writes across these calls and archival.
Lifecycle results are prepared from the checked status before the single
transition write, so no fallible store read follows a successful write.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Literal

from activity_tracker_portal import ingest_protocol
from activity_tracker_portal.operational_store import (
    InstallationStatus,
    OperationalStore,
    StoreRefusal,
    StoreRetryable,
    StoreView,
)

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


def _validate_now(now: datetime) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")


@contextmanager
def _store_outcomes() -> Iterator[None]:
    """Expose only failure names, suppressing the store exception's context."""
    try:
        yield
    except StoreRefusal as refusal:
        raise InstallationRefusal(refusal.failure) from None
    except StoreRetryable as retryable:
        raise InstallationRetryable(retryable.failure) from None


def _advertisement(view: StoreView) -> bytes:
    generations = view.honored_generations()
    if not generations:
        raise InstallationRefusal("no-capability-generation")
    return max(generations, key=lambda item: item.generation).advertisement


def _capability_message(capabilities: Capabilities) -> ingest_protocol.Message:
    return {
        "wire_version": 1,
        "kind": "capability-advertisement",
        "generation": 1,
        "wire_versions": list(capabilities.wire_versions),
        "projection_schemas": [
            {"record_type": record_type, "versions": list(versions)}
            for record_type, versions in capabilities.projection_schemas
        ],
        "max_record_count": capabilities.max_record_count,
        "max_message_bytes": capabilities.max_message_bytes,
    }


def _code_digest(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def offer_enrollment(
    store: OperationalStore,
    portal_url: str,
    now: datetime,
    *,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> EnrollmentOffer:
    _validate_now(now)
    chunk = random_bytes(10)
    if not isinstance(chunk, bytes) or len(chunk) != 10:
        raise ValueError("random source must return exactly ten bytes")
    code = base64.b32encode(chunk).decode("ascii")
    expires_at = (now + ENROLLMENT_CODE_LIFETIME).astimezone(timezone.utc)
    expires_at = expires_at.replace(microsecond=expires_at.microsecond // 1000 * 1000)
    try:
        message = ingest_protocol.encode_message({
            "wire_version": 1,
            "kind": "enrollment-offer",
            "enrollment_code": code,
            "portal_url": portal_url,
            "expires_at": expires_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        })
    except ingest_protocol.ProtocolRefusal:
        raise ValueError("invalid enrollment offer configuration") from None
    with _store_outcomes():
        with store.read() as view:
            _advertisement(view)
        store.record_enrollment_offer(_code_digest(code), expires_at, now)
    return EnrollmentOffer(code, expires_at, message)


def answer_enrollment_confirmation(store: OperationalStore, data: bytes, now: datetime) -> ConfirmationResponse:
    _validate_now(now)
    try:
        message = ingest_protocol.decode_message(data)
    except ingest_protocol.ProtocolRefusal as refusal:
        return ConfirmationResponse("refused", None, refusal.failure)
    if message["kind"] != "enrollment-confirmation":
        return ConfirmationResponse("refused", None, "malformed-message")
    try:
        with store.read() as view:
            advertisement = _advertisement(view)
            try:
                status = view.installation(message["sender_installation"])
            except StoreRefusal as refusal:
                if refusal.failure != "installation-unknown":
                    raise
                status = None
            if status is not None:
                if status.state in ("retired", "withdrawn"):
                    return ConfirmationResponse("refused", None, "installation-ended")
                return ConfirmationResponse("pending", advertisement, None)
        store.enroll_installation(
            _code_digest(message["enrollment_code"]), message["sender_installation"], message["fingerprint"], now,
        )
    except (StoreRefusal, InstallationRefusal) as refusal:
        return ConfirmationResponse("refused", None, refusal.failure)
    except StoreRetryable as retryable:
        body = ingest_protocol.encode_message({
            "wire_version": 1, "kind": "retryable-failure", "reason": "temporarily-unavailable",
        })
        return ConfirmationResponse("retryable", body, retryable.failure)
    return ConfirmationResponse("pending", advertisement, None)


def confirm_installation(store: OperationalStore, installation_id: str, fingerprint: str, now: datetime) -> InstallationStatus:
    _validate_now(now)
    with _store_outcomes():
        with store.read() as view:
            status = view.installation(installation_id)
            if status.fingerprint != fingerprint:
                raise InstallationRefusal("fingerprint-mismatch")
        result = replace(status, state="active", activated_at=now.astimezone(timezone.utc), retirement_kind=None)
        store.activate_installation(installation_id, now)
    return result


def withdraw_installation(store: OperationalStore, installation_id: str, now: datetime) -> InstallationStatus:
    _validate_now(now)
    with _store_outcomes():
        with store.read() as view:
            status = view.installation(installation_id)
        result = replace(status, state="withdrawn", withdrawn_at=now.astimezone(timezone.utc), retirement_kind=None)
        store.withdraw_installation(installation_id, now)
    return result


def begin_draining(store: OperationalStore, installation_id: str, now: datetime) -> InstallationStatus:
    _validate_now(now)
    with _store_outcomes():
        with store.read() as view:
            status = view.installation(installation_id)
        result = replace(status, state="draining", draining_at=now.astimezone(timezone.utc), retirement_kind=None)
        store.begin_draining(installation_id, now)
    return result


def retire_drained(store: OperationalStore, installation_id: str, final_sequence: int, now: datetime) -> InstallationStatus:
    _validate_now(now)
    if type(final_sequence) is not int or final_sequence < 0:
        raise ValueError("final_sequence must be a nonnegative integer")
    with _store_outcomes():
        with store.read() as view:
            status = view.installation(installation_id)
            if status.state != "draining":
                raise InstallationRefusal("installation-transition-invalid")
            if (status.highest_sequence or 0) != final_sequence:
                raise InstallationRefusal("drain-sequence-mismatch")
            if view.sequence_gaps(installation_id):
                raise InstallationRefusal("drain-has-gaps")
        result = replace(status, state="retired", retired_at=now.astimezone(timezone.utc), retirement_kind="drained")
        store.retire_installation(installation_id, "drained", now)
    return result


def retire_destructively(
    store: OperationalStore,
    installation_id: str,
    now: datetime,
    *,
    acknowledge_possible_loss: bool,
) -> InstallationStatus:
    _validate_now(now)
    if acknowledge_possible_loss is not True:
        raise InstallationRefusal("loss-not-acknowledged")
    with _store_outcomes():
        with store.read() as view:
            status = view.installation(installation_id)
        result = replace(status, state="retired", retired_at=now.astimezone(timezone.utc), retirement_kind="destructive")
        store.retire_installation(installation_id, "destructive", now)
    return result


def honor_capabilities(store: OperationalStore, capabilities: Capabilities, now: datetime) -> int:
    _validate_now(now)
    try:
        message = _capability_message(capabilities)
        advertisement = ingest_protocol.encode_message(message)
    except (ingest_protocol.ProtocolRefusal, AttributeError, TypeError, ValueError):
        raise ValueError("invalid capabilities") from None
    with _store_outcomes():
        with store.read() as view:
            generations = view.honored_generations()
        if generations:
            highest = max(generations, key=lambda item: item.generation)
            message["generation"] = highest.generation
            if ingest_protocol.encode_message(message) == highest.advertisement:
                return highest.generation
            message["generation"] = highest.generation + 1
            advertisement = ingest_protocol.encode_message(message)
        store.honor_generation(message["generation"], advertisement, now)
    return message["generation"]


def stop_superseded_generation(store: OperationalStore, now: datetime) -> int | None:
    _validate_now(now)
    with _store_outcomes():
        with store.read() as view:
            if any(status.state == "pending" for status in view.installations()):
                raise InstallationRefusal("pending-installation-exists")
            acknowledged = view.latest_acknowledged_generation()
            if acknowledged is None:
                return None
            superseded = [item.generation for item in view.honored_generations() if item.generation < acknowledged]
        if not superseded:
            return None
        generation = min(superseded)
        store.stop_honoring_generation(generation, now)
    return generation


def current_advertisement(store: OperationalStore) -> bytes:
    with _store_outcomes():
        with store.read() as view:
            return _advertisement(view)
