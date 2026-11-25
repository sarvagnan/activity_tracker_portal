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

Negative zero follows the SDK. SDK 1.1.0 compares masses with Kotlin's `Double.compareTo`, which orders `-0.0` below `0.0` (`units/Mass.kt` lines 70–74, with `zero()` built from `0.0` at line 93, and `records/Utils.kt` `requireNotLess`), so a decoded `-0.0` is quarantined as `weight-out-of-range`. JSON `-0` decodes to the integer 0, which has no sign, and is accepted. An RFC 8785 writer never writes `-0.0`.

Proposed tests: `test_whole_number_mass_becomes_an_equal_float`, `test_fractional_mass_is_unchanged`, `test_huge_integer_mass_is_quarantined_not_raised`.

### R5 — Bounds

- A mass below `WEIGHT_MIN_GRAMS` or above `WEIGHT_MAX_GRAMS` is quarantined as `weight-out-of-range` with the pointer `/record/weight_grams`.
- The bounds are the pinned Health Connect SDK 1.1.0 `WeightRecord` constructor's, both inclusive (Decision 1). They are 0 kg (`weight.zero()`) and 1000 kg (`MAX_WEIGHT = 1000.kilograms`), that is 0 g and 1,000,000 g, confirmed from `androidx/health/connect/client/records/WeightRecord.kt` lines 48–49 and 86 in connect-client-1.1.0-sources.jar (SHA-256 `f80f4869872d8480e0e55aa76dd7a8bd6de556cc21d77cb483aae0fa42e00da2`, from Google Maven). On API 34 and later the constructor delegates to the platform record instead, and these are its fallback bounds.
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

The executable pack, drafted on 2026-11-25 against `d8f6ce3`, adds:
- the suite `tests/acceptance/test_interpret_weight_1.py`: 19 tests, one class per requirement, with exactly the proposed names (R1 3, R2 2, R3 3, R4 3, R5 3, R6 2, R7 3);
- the runner `tests/acceptance/run_interpret_weight_1.sh`;
- the Interface stub `src/activity_tracker_portal/interpret_weight.py`. `PROCESSOR` is a frozen dataclass instance with the Interface's identity: `record_type` `"weight"`, `processor_version` `"interpret-weight@1"`, `supported_schema_versions` `frozenset({1})`, `body_schema` `BodySchema("weight", 1, (NumericField("/weight_grams", "float", "g"),))`, and `quarantine_reasons` `frozenset({"weight-out-of-range"})`. `WEIGHT_MIN_GRAMS` is `0.0` and `WEIGHT_MAX_GRAMS` is `1_000_000.0`. `interpret_body` raises `NotImplementedError("interpret-weight@1")`;
- the confirmed SDK bounds in R5, citing `WeightRecord.kt` in connect-client 1.1.0.

Weight reads no nested instant, so it neither uses nor tests the shared `_interpret_nested_time` helper.

**RED on the stub.** 5 tests pass and 14 fail. Counting subtests, there are 427 reports: 424 failures and 3 errors. The 5 passing tests are R1's three contract tests and R2's two identity tests, which check the contract, the effects declaration, the scope map, and the stub's real identity. Every report traces to the stub:
- 136 failures run through `interpret`, which turns the stub's exception into `processor-invariant-violated`. The suite reports these as "interpret refused with processor-invariant-violated".
- 3 errors are the same `InterpretFailure("processor-invariant-violated")`, raised in `test_processor_is_pure` by its in-process scenario runs, which call `interpret` without the failure-to-assertion wrapper. The purity subprocess is not reached.
- 287 failures call `interpret_body` directly and report "interpret_body raised NotImplementedError".
- 1 failure is R6's count of distinct headers. It is 0 because the stub produces no revision.

A trace around the stub and the parent's processor boundary records 426 `NotImplementedError("interpret-weight@1")` raises: 139 inside `interpret`, each followed by `processor-invariant-violated`, and 287 from direct calls.

In `test_common_outcomes_take_precedence`, the precedence checks pass on the stub because the parent never calls the processor for them. The test fails on its control, which expects the processor's own Quarantine once the common fault is removed.

**Passability.** A throwaway reference implementation passes all 19 tests in under a second. It checks `0 <= mass <= 1_000_000` on the decoded value, also quarantines a decoded float `-0.0`, and returns `{"weight_grams": float(mass)}`. Without the `-0.0` check, 8 subtests fail. The implementer's independent implementation also passes. The suite killed all 40 wrong implementations tried on the first draft, before the `-0.0` refinement, including:
- each bound made exclusive or moved by one, or by a tiny tolerance;
- bounds in kilograms, a positive-mass rule, and comparing `int(mass)`;
- conversion to kilograms, rounding, truncation, and an `int` body value;
- header fields, `null`, a unit, or a renamed key in the body;
- a wrong pointer or reason;
- converting before the bounds check;
- a value inside the Quarantine;
- running averages and latest-value state;
- rejecting `int` masses;
- mutating the record;
- reading a file or clock, or importing `operational_store`;
- a body or bound that depends on the offset or attribution;
- wrong bound constants.

That harness was not kept, so the 40 were not rerun. Against the revised suite, the implementer's 17 incorrect variants and 4 identity variants all fail. So do 12 written against the processor boundary: `supported_schema_versions` as `frozenset({True})` or `frozenset({1.0})`; an assignable `body_schema` or `interpret_body`; a raw Quarantine carrying record text in its version and policy identity; a changed temporal header; a record changed after the body is built; and, on every call, a file read, a pinned `tzdata` read, a `tzdata` read only when the instant is inferred, a `ZoneInfo` lookup, or a directory listing. Five variants pass, as expected: three are correct (integer bound constants, a float copied through its shortest text, explicit `gmtime(0)`/`strftime` calls), and two are the review-only purity gaps (`datetime.now()` per call, a file read once at import).

**Refinements the tests pin down.** None changes the approved semantics.
- The mass is checked as the common layer decodes it. So `0.0`, `0e0`, `1e6`, and `1000000.0` are inside the bounds, and the body bytes match those of the canonical tokens `0` and `1000000`.
- Direct calls must return a mapping with exactly the key `weight_grams` whose value is a finite Python `float`, bit-identical to `float` of the decoded mass.
- The processor's own Quarantine, from a direct call or captured through `interpret` before the common layer rewraps it, is exactly `Quarantine("weight-out-of-range", "/record/weight_grams")`, with the common layer's default version and policy identity. Its fields and `repr` carry no part of the mass.
- `PROCESSOR` is immutable: assigning any identity attribute, `body_schema` and `interpret_body` included, raises `AttributeError` or `TypeError`. `supported_schema_versions` is exactly a `frozenset` whose only element is an `int` 1, so `True` and `1.0` fail. `quarantine_reasons` is exactly a `frozenset` of `str`.
- `WEIGHT_MIN_GRAMS` and `WEIGHT_MAX_GRAMS` may be `int` or `float`, but not `bool`. They must equal 0 and 1,000,000.
- The suite passes `interpret` a delegating test processor that wraps `PROCESSOR`. It deep-copies the exact `record` and `temporal` arguments before each call and compares them afterwards, then calls `interpret_body` again with the same objects and requires an equal result. This holds on revision and Quarantine paths, with and without policies.
- In a subprocess, importing the module and running it reads no patched clock, writes nothing, opens no host timezone file, socket, store, or subprocess, and never imports `operational_store`. After a warm-up round, the processor's own calls open no file, list no directory, and read no timezone data, pinned or host. The common layer may read pinned `tzdata` for its policies outside those calls; without a policy, nothing reads it.
- Totality is checked on 52 masses and 5 places. The masses include subnormals, the largest doubles, integers beyond 2^53, 2^1024, and 400-digit integers. The places cover an offset, no offset, years 0001 and 9999, and `-18:00`. Every case is first confirmed schema-valid with `jsonschema`.
- A decoded `-0.0` (written `-0.0`, `-0e0`, or `-0.000`) is quarantined, because the SDK orders it below 0. This was settled by the pack drafter from the SDK source under Decision 1, not left open. JSON `-0` is the integer 0 and is accepted.

**Implementer review resolved.** GPT 6.1 Sol's read-only review found no blocker. Its findings and their fixes:
1. *Immutability omitted `body_schema` and `interpret_body`* (should-fix). Both are now assigned in R2, and the processor is restored if an assignment is accepted. Sol's two mutable-attribute variants now fail.
2. *Schema-version element types were unchecked* (should-fix). The only element must be an `int`. Sol's `frozenset({True})` and `frozenset({1.0})` variants now fail.
3. *Stale RED and passability evidence* (note). The RED counts and the reference description above are refreshed; `-0.0` is quarantined.
4. *Signed zero and gram bounds verified* (note). The citation now names `units/Mass.kt` lines 70–74 and 93.
5. *Purity partly review-only* (note). The review obligation stays. The boundary observation above is now shared word for word with the other three processor suites.

The sibling reviews' fixes are applied here too: the raw Quarantine check through `interpret` and the delegating processor's temporal-header and repeat-call checks.

RED must come entirely from the stub. The parent turns any processor exception into `processor-invariant-violated`, so tests that run through `interpret` fail with that failure. The approver confirms at Gate B that each such failure traces to the stub. The implementer's read-only findings are resolved.
