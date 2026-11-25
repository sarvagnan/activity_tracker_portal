"""interpret-heart-rate@1: the Heart Rate processor for interpret-source-projection@1.

A Heart Rate record is a series: an interval with samples, each an instant and a
beats-per-minute value. The body keeps every sample in source order with its exact
instant text and value, and a local date only where the start and end applied
offsets give the same date. Nothing is interpolated, sorted, deduplicated, or
summarized.

Validity beyond the closed schema follows the pinned Health Connect SDK 1.1.0
``HeartRateRecord`` (``androidx/health/connect/client/records/HeartRateRecord.kt``
in connect-client-1.1.0-sources.jar, SHA-256
``f80f4869872d8480e0e55aa76dd7a8bd6de556cc21d77cb483aae0fa42e00da2``): line 35
requires only that the start is not after the end, and lines 125-126, in the
``Sample`` constructor, bound beats per minute, both ends inclusive. Samples must
lie within the series interval, both ends inclusive, by Operator decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ._interpret_nested_time import local_date, offset_seconds, parse_instant_ns
from .interpret_source_projection import BodySchema, NumericField, Quarantine, TemporalHeader

BPM_MIN = 1
BPM_MAX = 300


@dataclass(frozen=True)
class _HeartRateProcessor:
    record_type: str = field(default="heart-rate", init=False)
    processor_version: str = field(default="interpret-heart-rate@1", init=False)
    supported_schema_versions: frozenset[int] = field(default=frozenset({1}), init=False)
    body_schema: BodySchema = field(
        default=BodySchema("heart-rate", 1, (NumericField("/samples/*/beats_per_minute", "integer", "1/min"),)),
        init=False,
    )
    quarantine_reasons: frozenset[str] = field(
        default=frozenset({"heart-rate-sample-outside-interval", "heart-rate-bpm-out-of-range"}), init=False,
    )

    def interpret_body(self, record: Mapping[str, Any], temporal: TemporalHeader) -> Mapping[str, Any] | Quarantine:
        start_ns = temporal.start.instant.epoch_ns
        end_ns = temporal.end.instant.epoch_ns
        start_offset = offset_seconds(temporal.start.applied_offset)
        end_offset = offset_seconds(temporal.end.applied_offset)
        samples: list[dict[str, Any]] = []
        for index, sample in enumerate(record["samples"]):
            time_text = sample["time"]
            instant_ns = parse_instant_ns(time_text)
            if not start_ns <= instant_ns <= end_ns:
                return Quarantine("heart-rate-sample-outside-interval", f"/record/samples/{index}/time")
            bpm = sample["beats_per_minute"]
            if not BPM_MIN <= bpm <= BPM_MAX:
                return Quarantine("heart-rate-bpm-out-of-range", f"/record/samples/{index}/beats_per_minute")
            entry = {"time": time_text, "beats_per_minute": int(bpm)}
            start_date = local_date(instant_ns, start_offset)
            end_date = local_date(instant_ns, end_offset)
            if start_date is not None and start_date == end_date:
                entry["local_date"] = start_date
            samples.append(entry)
        return {"samples": samples}


PROCESSOR = _HeartRateProcessor()
