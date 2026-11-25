# Draft acceptance pack — `interpret-heart-rate@1`

**Status:** Draft approved by the Operator on 2026-11-24, with every decision as recommended and the follow-ups recorded below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `interpret-heart-rate` Governed Scope contract, version 1, in `governance/scopes/interpret-heart-rate.1.json`. It depends on the published `interpret-source-projection@1`. It is drafted in one batch with `interpret-steps@1`, `interpret-sleep-session@1`, and `interpret-weight@1`, which share the conventions in Decision 5.

`interpret-source-projection@1` turns one current Source Projection into exactly one outcome. It owns the envelope, closed-schema validation, the revision header, time attribution of the record's start and end, Policy-Pending, and canonical bytes. A concrete processor owns only the type-specific body, its extra validity rules, and its quarantine reasons (ADR 0024, 0042). Instants nested in the body, such as samples, belong to the processor (parent R7).

This unit is the Heart Rate processor. A Heart Rate record is a series: an interval with samples, each an instant and a beats-per-minute value. Samples carry no offset of their own. `derive-heart-rate-summary` later needs every sample's exact instant, its value, and its local date, to compute daily sums, counts, minima, maxima, and means without interpolation (ADR 0041). This unit supplies the samples and their local dates where the evidence determines them. It computes none of the statistics.

## Interface

The module `activity_tracker_portal.interpret_heart_rate` provides:
- `PROCESSOR`: an immutable object that satisfies the `interpret_source_projection.Processor` protocol:
  - `record_type` is `"heart-rate"`;
  - `processor_version` is `"interpret-heart-rate@1"`;
  - `supported_schema_versions` is `frozenset({1})`;
  - `body_schema` is `BodySchema("heart-rate", 1, (NumericField("/samples/*/beats_per_minute", "integer", "1/min"),))`;
  - `quarantine_reasons` is `frozenset({"heart-rate-sample-outside-interval", "heart-rate-bpm-out-of-range"})`;
  - `interpret_body(record, temporal)` returns the body mapping or a `Quarantine(reason, pointer)`.
- `BPM_MIN` and `BPM_MAX`: the pinned SDK's inclusive beats-per-minute bounds, as Python `int`s.

The caller passes `PROCESSOR` to `interpret_source_projection.interpret`.

**Body, version 1.**

```json
{"samples": [{"time": "<exact instant text>", "beats_per_minute": 72, "local_date": "YYYY-MM-DD"}]}
```

- `samples` has one entry per projected sample, in source order.
- `time` is the sample's exact text from the projection.
- `beats_per_minute` is the projection's value, unchanged, in the canonical unit `1/min`.
- `local_date` is present only when bracketed attribution determines it (R4).

## Requirements

### R1 — Governed Scope contract

The contract has the shared shape, kind `leaf`, version 1, and depends only on `interpret-source-projection@1`. It has no named failures (Decision 6) and no observable effects, and its effects declaration is empty. Its quarantine output names exactly the two declared reasons. It does not contradict the scope-map row.

Proposed tests: `test_contract_shape_and_dependency`, `test_scope_map_row_does_not_contradict_contract`, `test_effects_declaration_is_empty`.

### R2 — Processor identity

- `PROCESSOR` passes `isinstance(PROCESSOR, Processor)` and has the attribute values in the Interface.
- Its processor version is its governed unit identity (parent Decision 7).
- Its declared reasons are exactly the two above. Neither is a common reason of `interpret-source-projection@1`.

Proposed tests: `test_processor_identity`, `test_declared_reasons_are_exact_and_not_common`.

### R3 — Body

- Every Heart Rate positive vector becomes a Canonical Revision through `interpret`. Vectors without offsets run under a synthetic Fallback Timezone Policy.
- Each sample keeps its exact `time` text, including nine-digit and six-digit fractions (vector `heart-rate-instant-precision`), and its `beats_per_minute` as a Python `int`.
- Samples keep source order. The unsorted vector `heart-rate-unsorted-samples` keeps its order.
- Samples at the same instant are all kept, whether their values are equal or different. Nothing is merged or deduplicated.
- An empty `samples` list gives `{"samples": []}`.
- The body repeats no header field.

Proposed tests: `test_positive_vectors_become_revisions`, `test_samples_keep_exact_text_and_values`, `test_samples_keep_source_order`, `test_equal_time_samples_are_all_kept`, `test_empty_samples_give_an_empty_list`, `test_body_has_no_header_fields`.

### R4 — Bracketed local dates (Operator decision, 2026-11-24)

A sample has no offset, so its local date can only come from the series' endpoints. For a sample at instant `t`:
- take the date of `t` shifted by the start endpoint's `applied_offset`, and the date of `t` shifted by the end endpoint's `applied_offset`;
- if both dates are within years 1–9999 and equal, write that date as `local_date`;
- otherwise omit `local_date`. Phase 5 then treats the sample's day as unattributable.

Consequences:
- When the two applied offsets are equal, every sample gets a date.
- When they differ, as across a daylight-saving change or travel, a sample gets a date only if both offsets put it on the same date. A sample whose two readings fall on different sides of midnight gets none. The processor never guesses when the offset changed.
- Applied offsets may be `±hh:mm`, or `±hh:mm:ss` for an inferred historical offset (parent R8). The seconds count.
- A shift past 9999-12-31 under one offset omits the date. It never raises. For example, a sample at `9999-12-31T23:00:00Z` with offsets `+05:00` and `+00:00` has no `local_date`.
- A sample's date is inferred whenever either endpoint is inferred. The header records each endpoint's attribution, so the body adds no attribution field.
- The body depends on the applied offsets. A Fallback Timezone Policy that changes an endpoint's offset can therefore change the body. The parent's policy identity already names every policy applied to an endpoint, so the revision identity changes with it. A policy that applies to no endpoint changes nothing.

**Caveat, stated for the Operator.** Equal endpoint offsets do not prove that no offset change happened inside the series. Two changes inside one record, such as a daylight-saving change and its reversal, or travel out and back, go undetected. The samples between them would get a date from the outer offset. This needs a single record spanning both changes, which for daylight saving means months. The draft accepts this.

Proposed tests: `test_equal_offsets_date_every_sample`, `test_differing_offsets_date_only_agreeing_samples`, `test_seconds_offsets_are_exact`, `test_out_of_range_shift_omits_the_date`, `test_inferred_endpoints_bracket_like_experienced_ones`, `test_unused_policy_leaves_the_body_unchanged`.

### R5 — Validity

Each sample is checked in index order, its `time` before its `beats_per_minute` (`projection/v1/model.json` order). The first fault wins.
- **Inside the interval.** A sample before the series start or after its end is quarantined as `heart-rate-sample-outside-interval` with the pointer `/record/samples/<i>/time` (Decision 2). Both ends are inclusive. Instants compare by exact nanoseconds, so equal instants written with different fractions are equal.
- **Bounds.** A `beats_per_minute` below `BPM_MIN` or above `BPM_MAX` is quarantined as `heart-rate-bpm-out-of-range` with the pointer `/record/samples/<i>/beats_per_minute`. The bounds are the pinned SDK 1.1.0 sample constructor's, both inclusive (Decision 1). They are 1 and 300, confirmed from `androidx/health/connect/client/records/HeartRateRecord.kt` lines 125–126 (the `Sample` constructor) in connect-client-1.1.0-sources.jar (SHA-256 `f80f4869872d8480e0e55aa76dd7a8bd6de556cc21d77cb483aae0fa42e00da2`, from Google Maven).
- **Year 0000.** A sample in year 0000 is before any start the parent accepts, so it is `heart-rate-sample-outside-interval`. It never raises.
- **Equal start and end.** A series whose start equals its end is accepted, with every sample at that instant. The SDK requires only that the start is not after the end (`HeartRateRecord.kt` line 35, `require(!startTime.isAfter(endTime))`, confirmed), so equal endpoints are valid. The Operator decided on 2026-11-24 to follow the SDK. If the SDK requires the start to be before the end, the executable pack adds the reason `heart-rate-interval-empty`, with the pointer `/record/end_time`, and the contract's quarantine output names it. It does not, so no reason is added and the contract is unchanged. The SDK constructor does not check sample times against the interval. The inside-the-interval rule is the Operator's Decision 2, not an SDK rule.
- **Common outcomes come first.** A reversed interval, a missing policy, or an invalid schema is the parent's outcome, even when a sample is also faulty (parent R4).

Proposed tests: `test_sample_outside_interval_is_quarantined`, `test_interval_bounds_are_inclusive`, `test_bpm_bounds_are_the_pinned_sdk_bounds`, `test_bpm_outside_bounds_is_quarantined`, `test_year_zero_sample_is_outside_the_interval`, `test_zero_length_series_is_accepted`, `test_first_fault_follows_traversal_order`, `test_common_outcomes_take_precedence`.

### R6 — No derivation

The processor does not interpolate, resample, time-weight, sort, or deduplicate. It computes no sum, count, minimum, maximum, or mean, and infers no resting rate. Statistics and equal-time duplicate handling belong to `derive-heart-rate-summary` and `duplicate-observation-contract` (ADR 0041).

Proposed tests: `test_body_carries_no_statistics`, `test_body_depends_only_on_samples_and_applied_offsets`.

### R7 — Total, pure, fast, and health-data-free

- For any schema-valid Heart Rate record that reaches it, `interpret_body` returns a body or a declared Quarantine and never raises. An exception would become `processor-invariant-violated`, a failed job (parent R9).
- It reads no clock, store, network, file, or timezone database, and imports no `operational_store`.
- Its work is linear in the number of samples. There is no sample-count limit; the Wire Protocol's maximum message size already bounds a record. The suite interprets a 100,000-sample series end to end within a generous time limit that the executable pack sets.
- Its Quarantines carry only a reason and a field pointer, never a value.

Proposed tests: `test_schema_valid_edges_never_raise`, `test_processor_is_pure`, `test_large_series_is_interpreted`, `test_outcomes_are_health_data_free`.

## Decisions for the Operator

1. **Validity follows the pinned SDK's bounds exactly (Operator decision, 2026-11-24).** The projection schema pins `sdk_version` to connect-client 1.1.0. The Sender reads every record through that SDK's constructors, so real data lies within their bounds. A value outside them can only come from a defective or forged Sender. The bounds are confirmed from the SDK source in the executable pack. The alternatives are structural sanity only, or the schema alone, which admits ±(2^53 − 1) beats per minute.
2. **A sample outside the interval quarantines the record (Operator decision, 2026-11-24).** Bracketed attribution is defined only between the endpoints, so such a sample has no offset evidence at all. The alternatives:
   - keep it with a flag, which leaves a sample whose day cannot be determined;
   - drop it, which breaks "Heart Rate preserves every sample" (ADR 0041).
3. **Bracketed local dates in the processor (Operator decision, 2026-11-24; R4).** Calendar attribution stays in Phase 4, under the revision's policy identity, as ADR 0022 and 0025 intend. A policy change creates new revisions, and summaries are then rebuilt. The rule never invents when an offset changed. The alternatives:
   - no local date in the body, so `derive-heart-rate-summary` attributes samples itself with policies and timezone data, moving time attribution into Phase 5;
   - a successor `interpret-source-projection@2` that passes the policy zone to processors. That is exact, but it supersedes a just-published unit.
4. **Source order, no merging (Operator decision, 2026-11-24).** Lists keep the order `project-source-record@1` R8 preserves. Equal-time samples inside one record stay distinct, so Phase 5 sees them and can apply the same equal-time rules it applies across records.
5. **Shared conventions for the four processors (Operator decision, 2026-11-24).** These hold for `interpret-steps@1`, `interpret-heart-rate@1`, `interpret-sleep-session@1`, and `interpret-weight@1` alike:
   - **Identity.** The processor version is `interpret-<type>@1`. Supported schema versions are `{1}`. The body version is 1.
   - **Field names.** Body fields use the projection's field names verbatim. `NumericField` declares the unit; the name does not change.
   - **No header copy.** The body never repeats a header field.
   - **Nested instants.** An instant inside the body keeps the projection's exact RFC 3339 text. It carries no epoch number and is never rounded (ADR 0025).
   - **Absence.** An absent optional field is omitted, never `null`.
   - **Order.** Lists keep source order. Nothing is sorted, merged, or deduplicated.
   - **No derivation.** No totals, statistics, durations, classes, cross-record merging, source selection, unit conversion, rounding, clock, policy, or store reads (ADR 0041, 0043).
   - **SDK bounds.** Validity beyond the schema follows the pinned SDK 1.1.0 constructor rules exactly, as in Decision 1.
   - **Reasons.** Each reason starts with its type, such as `heart-rate-`, so it never reuses a common reason or collides with another processor's.
   - **Pointers.** A pointer names the offending source field, down to the list element, such as `/record/samples/3/time`. With several faults, the first in traversal order wins: `projection/v1/model.json` field order, list elements by index, depth first (`project-source-record@1` R12).
   - **Totality.** `interpret_body` never raises on a schema-valid record (R7).
   - **Nested-instant helper.** See Decision 7.
6. **No named failures.** A Quarantine is an outcome, not a failure. The parent's contract likewise lists only its invariant refusals. A defect in this processor surfaces as the parent's `processor-invariant-violated`. So `named_failures` is empty, and the quarantine output names the two reasons. The governance test checks both.
7. **One shared private helper for nested instants.** The parent's instant parser is private, and `Endpoint` carries offsets but no zone. The heart-rate and sleep processors therefore share one private module, `activity_tracker_portal._interpret_nested_time`. It:
   - parses a `projection/v1` instant into epoch nanoseconds, including year 0000, with integer arithmetic and without `datetime`;
   - parses an applied offset, `±hh:mm` or `±hh:mm:ss`, into seconds;
   - gives the proleptic Gregorian date of an instant shifted by an offset, or none outside years 1–9999.

   It is implementation, not Interface. No suite imports it; each suite pins its behavior through its processor's outcomes. Vouch could list one file in several packs, because it rejects a duplicate path only within one pack. But this project binds acceptance files, never implementation (`docs/governance/delivery-flow.md`, step 6), so binding the helper would freeze code before it is written. It stays unbound, like the parent's own module. The first of the two processors implemented adds it, and the other reuses it. CI runs both runners, so a later change to it must keep both suites green. The alternatives:
   - a parser per processor, which duplicates the same exact arithmetic twice;
   - importing the parent's private `_parse_instant`, which is not part of its contract and raises on year 0000.

## Ground rules

- **Data.** Python only, standard library only. Inputs are the Heart Rate vectors in `projection/v1/vectors/positive.json` and hand-built synthetic Heart Rate projections. Every outcome test runs end to end through the published `interpret`, with synthetic Fallback Timezone Policy timelines.
- **Time.** Synthetic series use nine-digit fractions, differing start and end offsets, inferred endpoints across a daylight-saving change, historical offsets with seconds, and instants near years 1 and 9999.
- **Markers.** A health-data marker in projection text fields and an identity marker in Source Identities prove that Quarantines carry neither. Fault values are distinctive numbers that the tests search for in reasons, pointers, and `repr`.
- **Unbound inputs, reviewed rather than frozen.** The binding is the contract, the spec, the suite, the runner, and the effects declaration. These inputs stay outside it:
  - `docs/governance/scope-map.md`, read by R1's scope-map test;
  - the published `interpret-source-projection@1` code, contract, and pack, which its own binding freezes;
  - `projection/v1/`, which `project-source-record@1`'s binding freezes;
  - the shared helper `_interpret_nested_time` (Decision 7);
  - the execution environment: `uv`, `pyproject.toml`, `uv.lock`, and the managed Python;
  - `tests/governance/test_interpret_heart_rate_contract.py`, which the governance suite runs and this runner does not.
- **Not mechanically provable here**, so the approver confirms these in review:
  - `BPM_MIN`, `BPM_MAX`, and the start-not-after-end rule match the SDK 1.1.0 source the executable pack cites;
  - no I/O, clock, or timezone database access, beyond what the purity test observes;
  - no sorting, merging, conversion, or rounding.

## Freeze prerequisites

After Gate A, the executable pack adds:
- the suite `tests/acceptance/test_interpret_heart_rate_1.py`, with the tests above;
- the runner `tests/acceptance/run_interpret_heart_rate_1.sh`;
- the Interface stub `src/activity_tracker_portal/interpret_heart_rate.py`: `PROCESSOR` with its real identity attributes and an `interpret_body` that raises `NotImplementedError`, and `BPM_MIN` and `BPM_MAX`;
- the confirmed SDK bounds, citing the `HeartRateRecord` source in connect-client 1.1.0.

RED must come entirely from the stub. The parent turns any processor exception into `processor-invariant-violated`, so tests that run through `interpret` fail with that failure. The approver confirms at Gate B that each such failure traces to the stub. The implementer's read-only findings must be resolved before freeze.
