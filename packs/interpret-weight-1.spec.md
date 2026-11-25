# Draft acceptance pack — `interpret-weight@1`

**Status:** Draft approved by the Operator on 2026-11-24, with every decision as recommended and the follow-ups recorded below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `interpret-weight` Governed Scope contract, version 1, in `governance/scopes/interpret-weight.1.json`. It depends on the published `interpret-source-projection@1`. It is drafted in one batch with `interpret-steps@1`, `interpret-heart-rate@1`, and `interpret-sleep-session@1`, which share the conventions in Decision 3.

`interpret-source-projection@1` turns one current Source Projection into exactly one outcome. It owns the envelope, closed-schema validation, the revision header, time attribution, Policy-Pending, and canonical bytes. A concrete processor owns only the type-specific body, its extra validity rules, and its quarantine reasons (ADR 0024, 0042). This unit is the Weight processor. A Weight record is an instant with a mass in grams. The body carries that mass exactly. Latest-per-day selection, equal-time grouping, and ties belong to `derive-weight-series` (ADR 0041).

## Interface

The module `activity_tracker_portal.interpret_weight` provides:
- `PROCESSOR`: an immutable object that satisfies the `interpret_source_projection.Processor` protocol:
  - `record_type` is `"weight"`;
  - `processor_version` is `"interpret-weight@1"`;
  - `supported_schema_versions` is `frozenset({1})`;
  - `body_schema` is `BodySchema("weight", 1, (NumericField("/weight_grams", "float", "g"),))`;
  - `quarantine_reasons` is `frozenset({"weight-out-of-range"})`;
  - `interpret_body(record, temporal)` returns the body mapping or a `Quarantine(reason, pointer)`.
- `WEIGHT_MIN_GRAMS` and `WEIGHT_MAX_GRAMS`: the pinned SDK's inclusive mass bounds in grams, as exact Python numbers.

The caller passes `PROCESSOR` to `interpret_source_projection.interpret`.

**Body, version 1.** `{"weight_grams": <float>}`. The value equals the projection's `weight_grams`, in the canonical unit `g`, as a Python `float`.

## Requirements

### R1 — Governed Scope contract

The contract has the shared shape, kind `leaf`, version 1, and depends only on `interpret-source-projection@1`. It has no named failures (Decision 4) and no observable effects, and its effects declaration is empty. Its quarantine output names exactly the one declared reason. It does not contradict the scope-map row.

Proposed tests: `test_contract_shape_and_dependency`, `test_scope_map_row_does_not_contradict_contract`, `test_effects_declaration_is_empty`.

### R2 — Processor identity

- `PROCESSOR` passes `isinstance(PROCESSOR, Processor)` and has the attribute values in the Interface.
- Its processor version is its governed unit identity (parent Decision 7).
- Its declared reason is exactly `weight-out-of-range`, which is not a common reason of `interpret-source-projection@1`.

Proposed tests: `test_processor_identity`, `test_declared_reasons_are_exact_and_not_common`.

### R3 — Body

- Every Weight positive vector becomes a Canonical Revision through `interpret`. Vectors without offsets run under a synthetic Fallback Timezone Policy.
- The body is exactly `{"weight_grams": w}`, where `w` is a finite Python `float` equal to the projected value.
- The body repeats no header field.

Proposed tests: `test_positive_vectors_become_revisions`, `test_body_is_exactly_the_mass`, `test_body_has_no_header_fields`.

### R4 — Whole-number masses become exact floats

The projection writes a mass as an RFC 8785 number. A double with no fraction, such as `72500.0`, is written `72500` and decodes as a Python `int` (parent R9). The body declares a `float`, so:
- an `int` mass becomes the `float` of equal value;
- this changes the number's type, not its value or unit, so it is not a conversion under parent Decision 8;
- every `int` within the bounds of R5 is exactly representable as a float, so nothing rounds;
- the bounds are checked on the decoded value *before* any conversion. A huge integer is therefore quarantined and never overflows.

Negative zero needs no rule: RFC 8785 writes it as `0`.

Proposed tests: `test_whole_number_mass_becomes_an_equal_float`, `test_fractional_mass_is_unchanged`, `test_huge_integer_mass_is_quarantined_not_raised`.

### R5 — Bounds

- A mass below `WEIGHT_MIN_GRAMS` or above `WEIGHT_MAX_GRAMS` is quarantined as `weight-out-of-range` with the pointer `/record/weight_grams`.
- The bounds are the pinned Health Connect SDK 1.1.0 `WeightRecord` constructor's, both inclusive (Decision 1). The drafter remembers them as 0 kg and 1000 kg, that is 0 g and 1,000,000 g. These figures are **to be confirmed** from the SDK source in the executable pack.
- Zero follows the SDK. If the SDK's minimum is 0 inclusive, a mass of 0 g is accepted.
- The tests cover each bound, the nearest doubles beyond each bound, a negative mass, and integers beyond 2^53, which the schema admits.
- Common outcomes come first. A year-0000 instant, an out-of-range local date, a missing policy, or an invalid schema is the parent's outcome, even when the mass is also out of range (parent R4).

Proposed tests: `test_mass_bounds_are_the_pinned_sdk_bounds`, `test_mass_outside_bounds_is_quarantined`, `test_common_outcomes_take_precedence`.

### R6 — No derivation

- The body depends only on the mass. Changing the offset, policy, attribution, local date, recording method, or Device changes the header, never the body bytes.
- The processor does not smooth, average, select a latest value, break ties, compute BMI or trend, or convert to kilograms.

Proposed tests: `test_body_ignores_time_attribution_and_policies`, `test_body_carries_no_derived_measure`.

### R7 — Total, pure, and health-data-free

- For any schema-valid Weight record that reaches it, `interpret_body` returns a body or a declared Quarantine and never raises. An exception would become `processor-invariant-violated`, a failed job (parent R9).
- It reads no clock, store, network, or file, and imports no `operational_store`.
- Its Quarantines carry only a reason and a field pointer, never a value.

Proposed tests: `test_schema_valid_edges_never_raise`, `test_processor_is_pure`, `test_outcomes_are_health_data_free`.

## Decisions for the Operator

1. **Validity follows the pinned SDK's bounds exactly, including zero (Operator decision, 2026-11-24).** The projection schema pins `sdk_version` to connect-client 1.1.0. The Sender reads every record through that SDK's constructors, so real data lies within their bounds. A value outside them can only come from a defective or forged Sender. The lower bound is the SDK's as well. The alternatives:
   - requiring a positive mass, which would quarantine a value the source system accepts;
   - the schema alone, which bounds nothing.
2. **The body keeps grams as a float.** Grams are the projection's storage unit, so the value passes through exactly (parent Decision 8; ADR 0025). Declaring the field `float` keeps integer and floating values distinct even when RFC 8785 prints no fraction. The alternative, an `integer` field for whole-number masses, would give one field two kinds.
3. **Shared conventions for the four processors (Operator decision, 2026-11-24).** These hold for `interpret-steps@1`, `interpret-heart-rate@1`, `interpret-sleep-session@1`, and `interpret-weight@1` alike:
   - **Identity.** The processor version is `interpret-<type>@1`. Supported schema versions are `{1}`. The body version is 1.
   - **Field names.** Body fields use the projection's field names verbatim. `NumericField` declares the unit; the name does not change.
   - **No header copy.** The body never repeats a header field.
   - **Nested instants.** An instant inside the body keeps the projection's exact RFC 3339 text. It carries no epoch number and is never rounded (ADR 0025).
   - **Absence.** An absent optional field is omitted, never `null`.
   - **Order.** Lists keep source order (`project-source-record@1` R8). Nothing is sorted, merged, or deduplicated.
   - **No derivation.** No totals, statistics, durations, classes, cross-record merging, source selection, unit conversion, rounding, clock, policy, or store reads (ADR 0041, 0043).
   - **SDK bounds.** Validity beyond the schema follows the pinned SDK 1.1.0 constructor rules exactly, as in Decision 1.
   - **Reasons.** Each reason starts with its type, here `weight-`, so it never reuses a common reason or collides with another processor's.
   - **Pointers.** A pointer names the offending source field. With several faults, the first in traversal order wins (`project-source-record@1` R12).
   - **Totality.** `interpret_body` never raises on a schema-valid record (R7).
   - **Nested-instant helper.** The heart-rate and sleep processors share one private module for exact nested-instant arithmetic. Weight reads no nested instant and does not use it.
4. **No named failures.** A Quarantine is an outcome, not a failure. The parent's contract likewise lists only its invariant refusals. A defect in this processor surfaces as the parent's `processor-invariant-violated`. So `named_failures` is empty, and the quarantine output names the one reason. The governance test checks both.

## Ground rules

- **Data.** Python only, standard library only. Inputs are the Weight vectors in `projection/v1/vectors/positive.json` and hand-built synthetic Weight projections, including whole-number masses written without a fraction. Every outcome test runs end to end through the published `interpret`, with synthetic Fallback Timezone Policy timelines where offsets are absent.
- **Markers.** A health-data marker in projection text fields and an identity marker in Source Identities prove that Quarantines carry neither. Fault masses are distinctive numbers that the tests search for in reasons, pointers, and `repr`.
- **Unbound inputs, reviewed rather than frozen.** The binding is the contract, the spec, the suite, the runner, and the effects declaration. These inputs stay outside it:
  - `docs/governance/scope-map.md`, read by R1's scope-map test;
  - the published `interpret-source-projection@1` code, contract, and pack, which its own binding freezes;
  - `projection/v1/`, which `project-source-record@1`'s binding freezes;
  - the execution environment: `uv`, `pyproject.toml`, `uv.lock`, and the managed Python;
  - `tests/governance/test_interpret_weight_contract.py`, which the governance suite runs and this runner does not.
- **Not mechanically provable here**, so the approver confirms these in review:
  - `WEIGHT_MIN_GRAMS` and `WEIGHT_MAX_GRAMS` match the SDK 1.1.0 source the executable pack cites;
  - no I/O or clock access, beyond what the purity test observes;
  - no conversion or rounding of the mass.

## Freeze prerequisites

After Gate A, the executable pack adds:
- the suite `tests/acceptance/test_interpret_weight_1.py`, with the tests above;
- the runner `tests/acceptance/run_interpret_weight_1.sh`;
- the Interface stub `src/activity_tracker_portal/interpret_weight.py`: `PROCESSOR` with its real identity attributes and an `interpret_body` that raises `NotImplementedError`, and `WEIGHT_MIN_GRAMS` and `WEIGHT_MAX_GRAMS`;
- the confirmed SDK bounds, citing the `WeightRecord` source in connect-client 1.1.0.

RED must come entirely from the stub. The parent turns any processor exception into `processor-invariant-violated`, so tests that run through `interpret` fail with that failure. The approver confirms at Gate B that each such failure traces to the stub. The implementer's read-only findings must be resolved before freeze.
