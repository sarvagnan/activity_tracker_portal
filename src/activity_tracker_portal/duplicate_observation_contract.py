"""duplicate-observation-contract@1: one pure rule for equal-time observations.

A derive unit passes the eligible instant observations of one record type.
Observations whose instants and values are exactly equal form a Duplicate
Group that contributes once; observations at one instant with different values
form an unresolved tie that contributes nothing
(packs/duplicate-observation-contract-1.spec.md).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

CONTRACT_VERSION = "duplicate-observation-contract@1"
GROUP_RULE = "exact-instant-and-value"
GROUP_CONFIDENCE = "exact"
FAILURES: tuple[str, ...] = ("duplicate-observation-input-invalid",)

__all__ = [
    "CONTRACT_VERSION", "FAILURES", "GROUP_CONFIDENCE", "GROUP_RULE",
    "Contribution", "DuplicateObservationFailure", "Observation", "ObservationRef", "Resolution", "TieClass",
    "UnresolvedTie", "resolve",
]


class DuplicateObservationFailure(Exception):
    """A named failure raised instead of a result; it carries only the failure name."""

    def __init__(self, failure: str) -> None:
        super().__init__(failure)
        self.failure = failure


# Representations show record identities, positions, and counts only: never an
# instant, a value, or an origin (R12).

@dataclass(frozen=True)
class Observation:
    """One eligible instant observation, as the derive unit extracted it."""

    record_id: int
    index: int
    source_system_kind: str = field(repr=False)
    origin: str = field(repr=False)
    instant_ns: int = field(repr=False)
    value: int | float = field(repr=False)


@dataclass(frozen=True)
class ObservationRef:
    """An observation's identity: its Source Record and its position there."""

    record_id: int
    index: int


@dataclass(frozen=True)
class Contribution:
    """One resolved instant. Two or more members are a Duplicate Group, which contributes once."""

    instant_ns: int = field(repr=False)
    value: int | float = field(repr=False)
    members: tuple[ObservationRef, ...]
    origins: tuple[tuple[str, str], ...] = field(repr=False)

    @property
    def record_ids(self) -> tuple[int, ...]:
        return tuple(sorted({member.record_id for member in self.members}))

    @property
    def is_group(self) -> bool:
        return len(self.members) >= 2


@dataclass(frozen=True)
class TieClass:
    """The observations of an unresolved tie that share one value."""

    value: int | float = field(repr=False)
    members: tuple[ObservationRef, ...]


@dataclass(frozen=True)
class UnresolvedTie:
    """One instant with two or more different values; nothing at it contributes."""

    instant_ns: int = field(repr=False)
    classes: tuple[TieClass, ...]

    @property
    def record_ids(self) -> tuple[int, ...]:
        return tuple(sorted({member.record_id for item in self.classes for member in item.members}))


@dataclass(frozen=True)
class Resolution:
    """Every eligible observation, in exactly one contribution or one tie class."""

    contract_version: str
    rule: str
    confidence: str
    contributions: tuple[Contribution, ...]
    ties: tuple[UnresolvedTie, ...]
    grouped_record_ids: tuple[int, ...]
    tied_record_ids: tuple[int, ...]

    @property
    def groups(self) -> tuple[Contribution, ...]:
        return tuple(item for item in self.contributions if item.is_group)


def resolve(observations: Iterable[Observation]) -> Resolution:
    """Resolve the eligible observations of one record type (R4-R10)."""
    raise NotImplementedError("duplicate-observation-contract@1")
