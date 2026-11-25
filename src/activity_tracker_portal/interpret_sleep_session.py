"""interpret-sleep-session@1: the Sleep Session processor for interpret-source-projection@1.

A Sleep Session record is an interval with an optional title and notes and a list
of stages. The body keeps every stage in source order with its exact instant text
and SDK stage code, and the title and notes verbatim. Nothing is classified,
unioned, clipped, gap-filled, timed, or split across dates.

Validity beyond the closed schema follows the pinned Health Connect SDK 1.1.0
``SleepSessionRecord`` constructors (``androidx/health/connect/client/records/
SleepSessionRecord.kt`` in connect-client-1.1.0-sources.jar, SHA-256
``f80f4869872d8480e0e55aa76dd7a8bd6de556cc21d77cb483aae0fa42e00da2``): line 51
requires the session start before its end, line 180 requires each stage start
before its end, and lines 58-59 require every stage to lie within the session,
both ends inclusive. Lines 53-56 also reject overlapping stages; this processor
keeps overlaps by Operator decision, because the sleep summary owns them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .interpret_source_projection import BodySchema, NumericField, Quarantine, TemporalHeader


@dataclass(frozen=True)
class _SleepSessionProcessor:
    record_type: str = field(default="sleep-session", init=False)
    processor_version: str = field(default="interpret-sleep-session@1", init=False)
    supported_schema_versions: frozenset[int] = field(default=frozenset({1}), init=False)
    body_schema: BodySchema = field(
        default=BodySchema("sleep-session", 1, (NumericField("/stages/*/stage", "code", None),)), init=False,
    )
    quarantine_reasons: frozenset[str] = field(
        default=frozenset({
            "sleep-session-empty",
            "sleep-stage-outside-session",
            "sleep-stage-reversed",
            "sleep-stage-empty",
        }),
        init=False,
    )

    def interpret_body(self, record: Mapping[str, Any], temporal: TemporalHeader) -> Mapping[str, Any] | Quarantine:
        raise NotImplementedError("interpret-sleep-session@1")


PROCESSOR = _SleepSessionProcessor()
