"""interpret-weight@1: the Weight processor for interpret-source-projection@1.

A Weight record is an instant with a mass in grams. The body carries that mass
exactly, as a float in grams, within the pinned Health Connect SDK 1.1.0
WeightRecord bounds.

Interface stub: the identity and bounds are real; `interpret_body` is not yet
implemented.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .interpret_source_projection import BodySchema, NumericField, Quarantine, TemporalHeader

# WeightRecord in connect-client 1.1.0: weight.zero() and MAX_WEIGHT = 1000.kilograms, both inclusive.
WEIGHT_MIN_GRAMS = 0.0
WEIGHT_MAX_GRAMS = 1_000_000.0


@dataclass(frozen=True)
class _WeightProcessor:
    record_type: str = "weight"
    processor_version: str = "interpret-weight@1"
    supported_schema_versions: frozenset[int] = frozenset({1})
    body_schema: BodySchema = BodySchema("weight", 1, (NumericField("/weight_grams", "float", "g"),))
    quarantine_reasons: frozenset[str] = frozenset({"weight-out-of-range"})

    def interpret_body(self, record: Mapping[str, Any], temporal: TemporalHeader) -> Mapping[str, Any] | Quarantine:
        raise NotImplementedError("interpret-weight@1")


PROCESSOR = _WeightProcessor()
