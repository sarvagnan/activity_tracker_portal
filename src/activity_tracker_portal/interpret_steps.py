"""interpret-steps@1: the Steps processor for interpret-source-projection@1.

A Steps record is an interval with a step count. The body is the source count in
the canonical unit ``count``, and nothing derived from it.

Validity beyond the closed schema follows the pinned Health Connect SDK 1.1.0
``StepsRecord`` constructor (``androidx/health/connect/client/records/StepsRecord.kt``
in connect-client-1.1.0-sources.jar, SHA-256
``f80f4869872d8480e0e55aa76dd7a8bd6de556cc21d77cb483aae0fa42e00da2``): line 49
requires the start before the end, and lines 53-54 bound the count, both ends
inclusive. On API 34 and later the constructor delegates to the platform record;
these are its fallback bounds.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .interpret_source_projection import BodySchema, NumericField, Quarantine, TemporalHeader

COUNT_MIN = 1
COUNT_MAX = 1_000_000


@dataclass(frozen=True)
class _StepsProcessor:
    record_type: str = field(default="steps", init=False)
    processor_version: str = field(default="interpret-steps@1", init=False)
    supported_schema_versions: frozenset[int] = field(default=frozenset({1}), init=False)
    body_schema: BodySchema = field(
        default=BodySchema("steps", 1, (NumericField("/count", "integer", "count"),)), init=False,
    )
    quarantine_reasons: frozenset[str] = field(
        default=frozenset({"steps-interval-empty", "steps-count-out-of-range"}), init=False,
    )

    def interpret_body(self, record: Mapping[str, Any], temporal: TemporalHeader) -> Mapping[str, Any] | Quarantine:
        # The parent supplies both interval endpoints after its common checks.
        if temporal.start.instant.epoch_ns == temporal.end.instant.epoch_ns:
            return Quarantine("steps-interval-empty", "/record/end_time")
        count = record["count"]
        if not COUNT_MIN <= count <= COUNT_MAX:
            return Quarantine("steps-count-out-of-range", "/record/count")
        # An integral JSON float is schema-valid; the body requires a Python int.
        return {"count": int(count)}


PROCESSOR = _StepsProcessor()
