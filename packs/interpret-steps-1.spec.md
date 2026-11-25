# Draft acceptance pack — `interpret-steps@1`

**Status:** Draft approved by the Operator on 2026-11-24, with every decision as recommended and the follow-ups recorded below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `interpret-steps` Governed Scope contract, version 1, in `governance/scopes/interpret-steps.1.json`. It depends on the published `interpret-source-projection@1`. It is drafted in one batch with `interpret-heart-rate@1`, `interpret-sleep-session@1`, and `interpret-weight@1`, which share the conventions in Decision 4.

`interpret-source-projection@1` turns one current Source Projection into exactly one outcome. It owns the envelope, closed-schema validation, the revision header, time attribution, Policy-Pending, and canonical bytes. A concrete processor owns only the type-specific body, its extra validity rules, and its quarantine reasons (ADR 0024, 0042). This unit is the Steps processor. A Steps record is an interval with a step count. The body carries that count and nothing derived from it. Daily totals, overlaps, and day-crossing intervals belong to `derive-daily-steps` (ADR 0041).

## Interface

The module `activity_tracker_portal.interpret_steps` provides:
- `PROCESSOR`: an immutable object that satisfies the `interpret_source_projection.Processor` protocol:
  - `record_type` is `"steps"`;
  - `processor_version` is `"interpret-steps@1"`;
  - `supported_schema_versions` is `frozenset({1})`;
  - `body_schema` is `BodySchema("steps", 1, (NumericField("/count", "integer", "count"),))`;
  - `quarantine_reasons` is `frozenset({"steps-interval-empty", "steps-count-out-of-range"})`;
  - `interpret_body(record, temporal)` returns the body mapping or a `Quarantine(reason, pointer)`.
- `COUNT_MIN` and `COUNT_MAX`: the pinned SDK's inclusive count bounds, as Python `int`s.

The caller passes `PROCESSOR` to `interpret_source_projection.interpret`. Which processor is mapped to a record type belongs to `execute-processing-job`.

**Body, version 1.** `{"count": <int>}`. The value is the projection's `count`, unchanged, in the canonical unit `count`.

## Requirements

### R1 — Governed Scope contract

The contract has the shared shape, kind `leaf`, version 1, and depends only on `interpret-source-projection@1`. It has no named failures (Decision 5) and no observable effects, and its effects declaration is empty. Its quarantine output names exactly the two declared reasons. It does not contradict the scope-map row.

Proposed tests: `test_contract_shape_and_dependency`, `test_scope_map_row_does_not_contradict_contract`, `test_effects_declaration_is_empty`.

### R2 — Processor identity

- `PROCESSOR` passes `isinstance(PROCESSOR, Processor)` and has the attribute values in the Interface.
- Its processor version is its governed unit identity (parent Decision 7).
- Its declared reasons are exactly the two above. Neither is a common reason of `interpret-source-projection@1`.

Proposed tests: `test_processor_identity`, `test_declared_reasons_are_exact_and_not_common`.

### R3 — Body

- Every Steps positive vector in `projection/v1/vectors/positive.json` becomes a Canonical Revision through `interpret`. Vectors without offsets run under a synthetic Fallback Timezone Policy.
- The body is exactly `{"count": n}`, where `n` is the projection's `count` as a Python `int`.
- The body repeats no header field: no instants, offsets, local dates, recording method, or Device.
- The header names `interpret-steps@1` and the body schema `{"record_type": "steps", "body_version": 1}`.

Proposed tests: `test_positive_vectors_become_revisions`, `test_body_is_exactly_the_count`, `test_body_has_no_header_fields`.

### R4 — Count bounds

- A count below `COUNT_MIN` or above `COUNT_MAX` is quarantined as `steps-count-out-of-range` with the pointer `/record/count`.
- The bounds are the pinned Health Connect SDK 1.1.0 `StepsRecord` constructor's bounds, both inclusive (Decision 1). They are 1 and 1,000,000, confirmed from `androidx/health/connect/client/records/StepsRecord.kt` lines 53–54 in connect-client-1.1.0-sources.jar (SHA-256 `f80f4869872d8480e0e55aa76dd7a8bd6de556cc21d77cb483aae0fa42e00da2`, from Google Maven). On API 34 and later the constructor delegates to the platform record instead, and these are its fallback bounds.
- Zero follows the SDK. If the SDK's minimum is 1, a count of 0 is quarantined.
- The tests cover each bound, one beyond each bound, 0, a negative count, and ±(2^53 − 1), which the schema admits.

Proposed tests: `test_count_bounds_are_the_pinned_sdk_bounds`, `test_count_outside_bounds_is_quarantined`.

### R5 — Empty interval

- A record whose start and end are the same instant is quarantined as `steps-interval-empty` with the pointer `/record/end_time` (Decision 2).
- Instants compare by exact nanoseconds from the temporal header (`Instant.epoch_ns`), not by text. So `…T08:00:00Z` and `…T08:00:00.000Z` are equal.
- An interval one nanosecond long is accepted.
- When both faults are present, the empty interval is reported, because `end_time` precedes `count` in `projection/v1/model.json` (Decision 4, pointers).
- Common outcomes come first. A reversed interval is the parent's `interval-reversed`, and a missing policy is Policy-Pending, even when the count is also out of range (parent R4).

Proposed tests: `test_empty_interval_is_quarantined`, `test_one_nanosecond_interval_is_accepted`, `test_first_fault_follows_traversal_order`, `test_common_outcomes_take_precedence`.

### R6 — No derivation

- The body depends only on the count. Changing offsets, policies, attribution, local dates, recording method, Device, or the leap-day and midnight-crossing vectors changes the header, never the body bytes.
- The processor computes no total, rate, or duration. It does not allocate a count across local dates. A day-crossing interval keeps its whole count, and the header's start and end local dates show the crossing to `derive-daily-steps`.

Proposed tests: `test_body_ignores_time_attribution_and_policies`, `test_day_crossing_interval_is_not_allocated`.

### R7 — Total, pure, and health-data-free

- For any schema-valid Steps record that reaches it, `interpret_body` returns a body or a declared Quarantine and never raises. An exception would become `processor-invariant-violated`, a failed job (parent R9).
- It reads no clock, store, network, or file, and imports no `operational_store`.
- Its Quarantines carry only a reason and a field pointer, never a value.

Proposed tests: `test_schema_valid_edges_never_raise`, `test_processor_is_pure`, `test_outcomes_are_health_data_free`.

## Decisions for the Operator

1. **Validity follows the pinned SDK's bounds exactly (Operator decision, 2026-11-24).** The projection schema pins `sdk_version` to `androidx.health.connect:connect-client:1.1.0`. The Sender reads every record through that SDK's constructors, so real data always lies within their bounds. A value outside them can only come from a defective or forged Sender, and quarantine shows that without passing it on. This includes the lower bound: zero follows the SDK. The alternatives:
   - structural sanity only, such as non-negative counts, which accepts values the source system cannot produce;
   - the schema alone, which admits ±(2^53 − 1).
2. **An empty interval is quarantined.** The SDK requires a Steps start before its end (`StepsRecord.kt` line 49, `require(startTime.isBefore(endTime))`, confirmed). The parent accepts equal endpoints and lets a processor refuse them (parent R7). A step count over no time cannot be placed on a timeline. The alternative, accepting it, would hand `derive-daily-steps` an interval no real record has.
3. **The body is the count only.** The header already carries the interval, offsets, local dates, and attribution, and the store indexes them for Phase 5 (`operational-store@4` Decision 7). Repeating them in the body would create two sources for one fact.
4. **Shared conventions for the four processors (Operator decision, 2026-11-24).** These hold for `interpret-steps@1`, `interpret-heart-rate@1`, `interpret-sleep-session@1`, and `interpret-weight@1` alike:
   - **Identity.** The processor version is `interpret-<type>@1`. Supported schema versions are `{1}`. The body version is 1.
   - **Field names.** Body fields use the projection's field names verbatim. `NumericField` declares the unit; the name does not change.
   - **No header copy.** The body never repeats a header field.
   - **Nested instants.** An instant inside the body keeps the projection's exact RFC 3339 text. It carries no epoch number and is never rounded (ADR 0025).
   - **Absence.** An absent optional field is omitted, never `null`.
   - **Order.** Lists keep source order (`project-source-record@1` R8). Nothing is sorted, merged, or deduplicated.
   - **No derivation.** No totals, statistics, durations, classes, cross-record merging, source selection, unit conversion, rounding, clock, policy, or store reads. Phase 5 owns summaries (ADR 0041, 0043).
   - **SDK bounds.** Validity beyond the schema follows the pinned SDK 1.1.0 constructor rules exactly, as in Decision 1.
   - **Reasons.** Each reason starts with its type, such as `steps-`, so it never reuses a common reason or collides with another processor's.
   - **Pointers.** A pointer names the offending source field, down to the list element, such as `/record/samples/3/time`. With several faults, the first in traversal order wins: `projection/v1/model.json` field order, list elements by index, depth first (`project-source-record@1` R12).
   - **Totality.** `interpret_body` never raises on a schema-valid record (R7).
   - **Nested-instant helper.** The heart-rate and sleep processors share one private module for exact nested-instant arithmetic. Steps reads no nested instant and does not use it.
5. **No named failures.** A Quarantine is an outcome, not a failure. The parent's contract likewise lists only its invariant refusals, not its quarantine reasons. A defect in this processor surfaces as the parent's `processor-invariant-violated`, which the parent owns. So `named_failures` is empty, and the quarantine output names the two reasons. The governance test checks both. The alternative, listing the reasons as named failures, would describe outcomes as refusals.

## Ground rules

- **Data.** Python only, standard library only. Inputs are the Steps vectors in `projection/v1/vectors/positive.json` and hand-built synthetic Steps projections. Every outcome test runs end to end through the published `interpret`, with synthetic Fallback Timezone Policy timelines where offsets are absent.
- **Markers.** A health-data marker in projection text fields and an identity marker in Source Identities prove that Quarantines carry neither. Counts in fault cases are distinctive values that the tests search for in reasons, pointers, and `repr`.
- **Unbound inputs, reviewed rather than frozen.** The binding is the contract, the spec, the suite, the runner, and the effects declaration. These inputs stay outside it:
  - `docs/governance/scope-map.md`, read by R1's scope-map test;
  - the published `interpret-source-projection@1` code, contract, and pack, which its own binding freezes;
  - `projection/v1/`, which `project-source-record@1`'s binding freezes;
  - the execution environment: `uv`, `pyproject.toml`, `uv.lock`, and the managed Python;
  - `tests/governance/test_interpret_steps_contract.py`, which the governance suite runs and this runner does not.
- **Not mechanically provable here**, so the approver confirms these in review:
  - `COUNT_MIN`, `COUNT_MAX`, and the start-before-end rule match the SDK 1.1.0 source the executable pack cites;
  - no I/O, clock, or host timezone access, beyond what the purity test observes;
  - no conversion or rounding of the count.

## Freeze prerequisites

The Operator approved the draft on 2026-11-24. The executable pack adds:
- the suite `tests/acceptance/test_interpret_steps_1.py`: 19 tests across R1–R7, one per proposed test name, with no additions;
- the runner `tests/acceptance/run_interpret_steps_1.sh`;
- the Interface stub `src/activity_tracker_portal/interpret_steps.py`:
  - `COUNT_MIN = 1` and `COUNT_MAX = 1_000_000`;
  - `PROCESSOR`, an instance of a frozen dataclass with the Interface's real `record_type`, `processor_version`, `supported_schema_versions`, `body_schema`, and `quarantine_reasons`;
  - `interpret_body`, which raises `NotImplementedError("interpret-steps@1")`;
  - a module docstring citing the SDK source below;
- the confirmed SDK rules: `StepsRecord.kt` in connect-client-1.1.0-sources.jar (SHA-256 `f80f4869872d8480e0e55aa76dd7a8bd6de556cc21d77cb483aae0fa42e00da2`), line 49 for start before end and lines 53–54 for the inclusive count bounds 1 and 1,000,000.

The shared nested-instant helper is not part of this pack. Steps does not use it, and no test imports it.

**RED, against the stub.** 14 of the 19 tests fail. Counting subtests, there are 263 errors, each `InterpretFailure("processor-invariant-violated")` from `interpret`. A trace around the parent's processor boundary shows 263 processor calls, each raising the stub's `NotImplementedError("interpret-steps@1")`. There are no assertion failures. The five R1 and R2 tests pass, because they read only the contract, scope map, effects declaration, and identity attributes. In `test_common_outcomes_take_precedence` the precedence cases pass, because the parent decides them before it calls the processor; the test fails on its controls, which reach the processor.

**Passability.** A throwaway reference implementation, kept outside the repository, passes all 19 tests in about 0.3 s. The spec sets no performance limit, so the suite has no timing test. All 41 incorrect implementations fail at least one test:
- count bounds: zero accepted, either bound exclusive, the maximum one too high, either constant wrong, bounds applied to the absolute value, out-of-range counts clamped, or raised instead of quarantined;
- empty interval: not checked, compared by text, by record text, at microsecond or second precision, only when the offsets match, or only when the local dates match;
- order and pointers: the count checked first, the empty interval at `/record/start_time`, the count at `/record`, the two reasons swapped;
- body: a copied start time, local date, or offset; a day-crossing flag; a count halved across days; an inferred flag; a `null` member; the count as a float;
- processor: a mutated record, a `Quarantine` subclass that carries the count, mutable or extra schema versions, a mutable processor, numeric fields as a list, reasons as a tuple, an extra declared reason, version `interpret-steps@2`;
- purity: a clock read at import or per call, an `operational_store` import, and an import of the nested-instant helper.

**What the tests pin beyond the approved text.** None of these changes approved semantics.
- **Identity (R2).** `PROCESSOR` is immutable: assigning any identity attribute or `interpret_body` raises `AttributeError` or `TypeError`. `supported_schema_versions` and `quarantine_reasons` are `frozenset`s, `numeric_fields` is a tuple, and `COUNT_MIN` and `COUNT_MAX` are exactly `int`.
- **Body (R3, R6).** The body bytes are exactly `{"count":<n>}`. They stay the same when the offsets, policies, attribution, local dates, recording method, Device, modification time, Source Identity, provider version, and key order change.
- **Empty interval (R5).** Equal instants are empty even when their offsets differ, so their local dates differ, and even when one is inferred. Equal instants written with 0, 3, 6, or 9 fractional digits are empty. A one-nanosecond interval is accepted, including across a year boundary.
- **Traversal (R5).** The empty-interval-first rule holds whatever the key order in the projection bytes.
- **Precedence (R5).** Besides `interval-reversed` and Policy-Pending, a schema-invalid count (2^53, a string, a fraction) and an `instant-out-of-range` endpoint also come before both processor faults. The processor is not called in any of these cases. This follows from parent R4.
- **Policy identity (R4, R5).** A processor Quarantine carries the policy identity the parent assigns to the same projection: `"none"` when both endpoints are experienced, and the policies used otherwise.
- **Totality and purity (R7).**
  - Over 15 counts from −(2^53 − 1) to 2^53 − 1 and 7 intervals, `interpret_body` never raises, returns exactly `{"count": n}` with an `int`, or a plain `Quarantine` with the expected reason and pointer, and leaves its record unchanged.
  - A repeated direct call gives an equal result.
  - In a subprocess, importing the module and running it reads no clock, opens no file for writing, reads no host timezone file, opens no socket, store, or subprocess, and imports neither `operational_store` nor `_interpret_nested_time`. Results match across `TZ`, `LC_ALL`, and `PYTHONHASHSEED`.
- **Health-data-free (R7).** Quarantines have exactly the parent's four fields. Their `repr` and `str`, both from `interpret` and from a direct call, carry no marker, Source Identity, or distinctive count.

**Choices the approver should check.**
- R6 names "the leap-day and midnight-crossing vectors". `projection/v1` has one Steps calendar vector, `steps-leap-days`, and with its `+05:30` offsets it does not cross a local midnight. The day-crossing test therefore runs that vector with its offsets removed under a UTC policy, which crosses from 2000-02-29 to 2000-03-01, alongside synthetic crossings: experienced, inferred through `America/New_York`, three days long, and one nanosecond across midnight.
- R5 says instants compare by `Instant.epoch_ns`. The tests check this through behavior: different texts for one instant, and one-nanosecond intervals. An implementation that parsed the record's text exactly to nanoseconds would also pass. Reading the temporal header remains a review item.

The implementer's read-only findings must be resolved before freeze. The approver confirms at Gate B that each RED failure traces to the stub.
