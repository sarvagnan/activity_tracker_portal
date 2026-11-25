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

from ._interpret_nested_time import parse_instant_ns
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
        start_ns = temporal.start.instant.epoch_ns
        end_ns = temporal.end.instant.epoch_ns
        if start_ns == end_ns:
            return Quarantine("sleep-session-empty", "/record/end_time")
        stages: list[dict[str, Any]] = []
        for index, stage in enumerate(record["stages"]):
            stage_start = parse_instant_ns(stage["start_time"])
            if not start_ns <= stage_start <= end_ns:
                return Quarantine("sleep-stage-outside-session", f"/record/stages/{index}/start_time")
            stage_end = parse_instant_ns(stage["end_time"])
            # End faults follow the specified order after an accepted start.
            if stage_end < stage_start:
                return Quarantine("sleep-stage-reversed", f"/record/stages/{index}/end_time")
            if stage_end == stage_start:
                return Quarantine("sleep-stage-empty", f"/record/stages/{index}/end_time")
            if stage_end > end_ns:
                return Quarantine("sleep-stage-outside-session", f"/record/stages/{index}/end_time")
            stages.append({
                "start_time": stage["start_time"],
                "end_time": stage["end_time"],
                "stage": int(stage["stage"]),
            })
        body: dict[str, Any] = {"stages": stages}
        for name in ("title", "notes"):
            if name in record:
                body[name] = record[name]
        return body


PROCESSOR = _SleepSessionProcessor()
