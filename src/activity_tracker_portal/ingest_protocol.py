"""Wire Protocol Version 1 for Sender ingress (`ingest-protocol@1`).

This Interface is declared before its implementation so the acceptance pack can
name it. Every function raises NotImplementedError until the unit is
implemented against the frozen pack. Field-level rules live in
`protocol/v1/README.md`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

Message = dict[str, Any]


class ProtocolRefusal(Exception):
    """A named failure from the `ingest-protocol` Governed Scope contract."""

    def __init__(self, failure: str) -> None:
        super().__init__(failure)
        self.failure = failure


def encode_message(value: Mapping[str, Any]) -> bytes:
    """Return the canonical bytes of a valid message value."""
    raise NotImplementedError


def decode_message(data: bytes) -> Message:
    """Return the validated message value, or raise ProtocolRefusal."""
    raise NotImplementedError


def message_sha256(data: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 identity of exact message bytes."""
    raise NotImplementedError


def installation_fingerprint(sender_installation: str) -> str:
    """Return the fingerprint an enrollment confirmation must carry."""
    raise NotImplementedError


def chain_start(cycle_id: str) -> str:
    """Return the starting fragment-chain value of a cycle."""
    raise NotImplementedError


def chain_next(previous_chain: str, message_sha256: str) -> str:
    """Return the fragment-chain value after one accepted fragment."""
    raise NotImplementedError


class DeliveryState:
    """Honored capability generations and accepted delivery history.

    Callers treat the state as opaque apart from the latest acknowledged
    generation, which the compatibility decision needs.
    """

    @property
    def latest_acknowledged_generation(self) -> int | None:
        raise NotImplementedError


@dataclass(frozen=True)
class DeliveryEvaluation:
    """The Portal's verdict on one delivered message."""

    outcome: str
    response: bytes
    state: DeliveryState
    failure: str | None = None
    gap_after: int | None = None


def new_delivery_state(honored_generations: Sequence[Mapping[str, Any]]) -> DeliveryState:
    """Return an empty delivery history under the given capability advertisements."""
    raise NotImplementedError


def evaluate_delivery(state: DeliveryState, data: bytes) -> DeliveryEvaluation:
    """Evaluate one delivered message; a Delivery Block returns the state unchanged."""
    raise NotImplementedError


def decide_compatibility(
    current: Sequence[Mapping[str, Any]],
    proposed: Sequence[Mapping[str, Any]],
    latest_acknowledged_generation: int | None,
) -> str:
    """Return "accept" or the compatibility-refusal failure name."""
    raise NotImplementedError
