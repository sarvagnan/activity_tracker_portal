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
from itertools import groupby
from math import isfinite
from operator import itemgetter

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


def _validated_observations(observations: Iterable[Observation]) -> list[Observation]:
    """Copy the iterable and validate it before comparing or grouping any values."""
    items = list(observations)
    identities: set[tuple[int, int]] = set()
    record_origins: dict[int, tuple[str, str]] = {}
    value_kind: type | None = None
    for item in items:
        if type(item) is not Observation:
            raise ValueError
        if not (
            type(item.record_id) is int and item.record_id > 0
            and type(item.index) is int and item.index >= 0
            and type(item.instant_ns) is int
            and type(item.source_system_kind) is str and item.source_system_kind
            and type(item.origin) is str and item.origin
            and type(item.value) in (int, float)
        ):
            raise ValueError
        if type(item.value) is float and not isfinite(item.value):
            raise ValueError
        if value_kind is not None and type(item.value) is not value_kind:
            raise ValueError
        value_kind = type(item.value)

        identity = (item.record_id, item.index)
        if identity in identities:
            raise ValueError
        identities.add(identity)
        origin = (item.source_system_kind, item.origin)
        if item.record_id in record_origins and record_origins[item.record_id] != origin:
            raise ValueError
        record_origins[item.record_id] = origin
    return items


def _resolve(observations: Iterable[Observation]) -> Resolution:
    items = _validated_observations(observations)
    # One tagged sort supplies every required order: record facts first, then
    # instants and value classes, with members before their ordered origins.
    entries: list[tuple] = [(0, record_id) for record_id in dict.fromkeys(item.record_id for item in items)]
    for item in items:
        entries.append((1, item.instant_ns, item.value, 0, item.record_id, item.index))
        entries.append((1, item.instant_ns, item.value, 1, item.source_system_kind, item.origin))
    entries.sort()
    ordered_records: list[int] = []
    contributions: list[Contribution] = []
    ties: list[UnresolvedTie] = []
    grouped_records: set[int] = set()
    tied_records: set[int] = set()
    for tag, tagged_entries in groupby(entries, key=itemgetter(0)):
        if tag == 0:
            ordered_records.extend(entry[1] for entry in tagged_entries)
            continue
        for instant, at_instant in groupby(tagged_entries, key=itemgetter(1)):
            classes: list[TieClass] = []
            for value, same_value in groupby(at_instant, key=itemgetter(2)):
                members: list[ObservationRef] = []
                origins: list[tuple[str, str]] = []
                for entry in same_value:
                    if entry[3] == 0:
                        members.append(ObservationRef(entry[4], entry[5]))
                    else:
                        origin = (entry[4], entry[5])
                        # Equal origins are adjacent in this class's entries.
                        if not origins or origins[-1] != origin:
                            origins.append(origin)
                # Normalize every float zero, including singles and tied classes.
                if type(value) is float and value == 0:
                    value = 0.0
                classes.append(TieClass(value, tuple(members)))
            if len(classes) == 1:
                only = classes[0]
                contribution = Contribution(instant, only.value, only.members, tuple(origins))
                contributions.append(contribution)
                if contribution.is_group:
                    grouped_records.update(member.record_id for member in only.members)
            else:
                # Any disagreement makes the whole instant unresolved.
                ties.append(UnresolvedTie(instant, tuple(classes)))
                for item_class in classes:
                    tied_records.update(member.record_id for member in item_class.members)
    return Resolution(
        CONTRACT_VERSION, GROUP_RULE, GROUP_CONFIDENCE,
        tuple(contributions), tuple(ties),
        tuple(record_id for record_id in ordered_records if record_id in grouped_records),
        tuple(record_id for record_id in ordered_records if record_id in tied_records),
    )


def resolve(observations: Iterable[Observation]) -> Resolution:
    """Resolve the eligible observations of one record type (R4-R10)."""
    try:
        return _resolve(observations)
    except Exception:
        pass
    # Discard the internal exception and its input-bearing frames, then remove
    # this boundary's input before constructing the public failure (R12).
    del observations
    try:
        raise DuplicateObservationFailure(FAILURES[0])
    except DuplicateObservationFailure as failure:
        # Even a caller's active exception must not become retained context.
        failure.__context__ = None
        raise
