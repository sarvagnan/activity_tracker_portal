"""interpret-weight@1: the Weight processor for interpret-source-projection@1.

A Weight record is an instant with a mass in grams. The body carries that mass
exactly, as a float in grams, within the pinned Health Connect SDK 1.1.0
WeightRecord bounds.

SDK 1.1.0 ``WeightRecord.kt`` lines 48-49 and 86 specify inclusive 0 to
1000 kg bounds. ``Mass.kt`` lines 70-74 and 93 use Double.compareTo against
positive zero, so decoded negative zero is below the lower bound.
Source jar SHA-256: f80f4869872d8480e0e55aa76dd7a8bd6de556cc21d77cb483aae0fa42e00da2.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import copysign
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
        mass = record["weight_grams"]
        # Check before float conversion: schema-valid integers can be enormous.
        if not WEIGHT_MIN_GRAMS <= mass <= WEIGHT_MAX_GRAMS:
            return Quarantine("weight-out-of-range", "/record/weight_grams")
        if isinstance(mass, float) and mass == 0.0 and copysign(1.0, mass) < 0:
            return Quarantine("weight-out-of-range", "/record/weight_grams")
        return {"weight_grams": float(mass)}


PROCESSOR = _WeightProcessor()
