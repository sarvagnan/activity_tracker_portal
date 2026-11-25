# Draft acceptance pack — `interpret-sleep-session@1`

**Status:** Draft for the Operator's Gate A review. The Operator made the decisions marked below on 2026-11-24, before drafting. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `interpret-sleep-session` Governed Scope contract, version 1, in `governance/scopes/interpret-sleep-session.1.json`. It depends on the published `interpret-source-projection@1`. It is drafted in one batch with `interpret-steps@1`, `interpret-heart-rate@1`, and `interpret-weight@1`, which share the conventions in Decision 6.

`interpret-source-projection@1` turns one current Source Projection into exactly one outcome. It owns the envelope, closed-schema validation, the revision header, time attribution of the session's start and end, Policy-Pending, and canonical bytes. A concrete processor owns only the type-specific body, its extra validity rules, and its quarantine reasons (ADR 0024, 0042). Instants nested in the body, such as stages, belong to the processor (parent R7).

This unit is the Sleep Session processor. A session is an interval with an optional title and notes and a list of stages. Each stage has a start, an end, and an SDK stage code; stages carry no offsets. `derive-sleep-summary` later attributes the whole session to its wake date, the header's end local date, and summarizes its stages: same-class overlaps union, differently classified overlaps are unresolved, and uncovered time stays unclassified (ADR 0041). This unit preserves the stage evidence those rules need. It applies none of them.

## Interface

The module `activity_tracker_portal.interpret_sleep_session` provides:
- `PROCESSOR`: an immutable object that satisfies the `interpret_source_projection.Processor` protocol:
  - `record_type` is `"sleep-session"`;
  - `processor_version` is `"interpret-sleep-session@1"`;
  - `supported_schema_versions` is `frozenset({1})`;
  - `body_schema` is `BodySchema("sleep-session", 1, (NumericField("/stages/*/stage", "code", None),))`;
  - `quarantine_reasons` is `frozenset({"sleep-session-empty", "sleep-stage-outside-session", "sleep-stage-reversed", "sleep-stage-empty"})`;
  - `interpret_body(record, temporal)` returns the body mapping or a `Quarantine(reason, pointer)`.

The caller passes `PROCESSOR` to `interpret_source_projection.interpret`.

**Body, version 1.**

```json
{"stages": [{"start_time": "<exact text>", "end_time": "<exact text>", "stage": 4}], "title": "…", "notes": "…"}
```

- `stages` has one entry per projected stage, in source order.
- `start_time` and `end_time` are the stage's exact text from the projection.
- `stage` is the SDK integer code, unchanged, declared as a `code` with no unit. The codes are 0 unknown, 1 awake, 2 sleeping, 3 out of bed, 4 light, 5 deep, 6 REM, and 7 awake in bed (`projection/v1/model.json`, `sleep-stage`).
- `title` and `notes` are the projection's strings, verbatim, present only when the projection has them.

## Requirements

### R1 — Governed Scope contract

The contract has the shared shape, kind `leaf`, version 1, and depends only on `interpret-source-projection@1`. It has no named failures (Decision 7) and no observable effects, and its effects declaration is empty. Its quarantine output names exactly the four declared reasons. It does not contradict the scope-map row.

Proposed tests: `test_contract_shape_and_dependency`, `test_scope_map_row_does_not_contradict_contract`, `test_effects_declaration_is_empty`.

### R2 — Processor identity

- `PROCESSOR` passes `isinstance(PROCESSOR, Processor)` and has the attribute values in the Interface.
- Its processor version is its governed unit identity (parent Decision 7).
- Its declared reasons are exactly the four above. None is a common reason of `interpret-source-projection@1`.

Proposed tests: `test_processor_identity`, `test_declared_reasons_are_exact_and_not_common`.

### R3 — Body

- Every Sleep Session positive vector becomes a Canonical Revision through `interpret`. Vectors without offsets run under a synthetic Fallback Timezone Policy.
- Each stage keeps its exact `start_time` and `end_time` text and its `stage` code as a Python `int`.
- Stages keep source order, including stages listed out of time order.
- Every declared code is kept, including 0, unknown. An undeclared code never reaches the processor: the parent quarantines it as `projection-schema-invalid`.
- An empty `stages` list gives `"stages": []`.
- `title` and `notes` are copied verbatim when present, including quotes, backslashes, control characters, and emoji (vector `sleep-session-text-escaping`). When absent they are omitted, never `null` or `""`.
- The body repeats no header field.

Proposed tests: `test_positive_vectors_become_revisions`, `test_stages_keep_exact_text_and_codes`, `test_stages_keep_source_order`, `test_every_declared_code_is_kept`, `test_empty_stages_give_an_empty_list`, `test_title_and_notes_are_verbatim_or_absent`, `test_body_has_no_header_fields`.

### R4 — Validity

The checks follow `projection/v1/model.json` order: the session's `end_time` before its `stages`, then each stage in index order, its `start_time` before its `end_time`. The first fault wins. Instants compare by exact nanoseconds.
- **Empty session.** A session whose start equals its end is quarantined as `sleep-session-empty` with the pointer `/record/end_time`.
- **Stage start.** A stage that starts before the session start or after the session end is quarantined as `sleep-stage-outside-session` with the pointer `/record/stages/<i>/start_time`.
- **Stage end.** For the stage's `end_time`, with the pointer `/record/stages/<i>/end_time`:
  - an end before the stage's start is `sleep-stage-reversed`;
  - an end equal to the stage's start is `sleep-stage-empty`;
  - an end after the session end is `sleep-stage-outside-session`.
- **Inclusive bounds.** A stage may start exactly at the session start and end exactly at the session end.
- **Year 0000.** A stage instant in year 0000 is before any session start the parent accepts, so it is `sleep-stage-outside-session`. It never raises.
- **Common outcomes come first.** A reversed session, a missing policy, or an invalid schema is the parent's outcome, even when a stage is also faulty (parent R4).

Proposed tests: `test_empty_session_is_quarantined`, `test_stage_outside_session_is_quarantined`, `test_reversed_stage_is_quarantined`, `test_empty_stage_is_quarantined`, `test_stage_bounds_are_inclusive`, `test_year_zero_stage_is_outside_the_session`, `test_first_fault_follows_traversal_order`, `test_common_outcomes_take_precedence`.

### R5 — No derivation

- Overlapping stages are all kept, whatever their codes. The processor neither rejects nor resolves them (Decision 3).
- Gaps between stages are not filled.
- Codes are not mapped to asleep, awake, or out-of-bed classes.
- No durations, totals, or stage local dates are computed. The wake date is the header's end local date, which the store indexes for Phase 5.
- The session is never split across dates.

Proposed tests: `test_overlapping_stages_are_kept`, `test_gaps_are_not_filled`, `test_body_carries_no_classes_or_durations`.

### R6 — Total, pure, fast, and health-data-free

- For any schema-valid Sleep Session record that reaches it, `interpret_body` returns a body or a declared Quarantine and never raises. An exception would become `processor-invariant-violated`, a failed job (parent R9).
- It reads no clock, store, network, file, or timezone database, and imports no `operational_store`.
- Its work is linear in the number of stages. There is no stage-count limit. The suite interprets a 100,000-stage session end to end within a generous time limit that the executable pack sets.
- Its Quarantines carry only a reason and a field pointer, never a value, a title, or notes.

Proposed tests: `test_schema_valid_edges_never_raise`, `test_processor_is_pure`, `test_large_session_is_interpreted`, `test_outcomes_are_health_data_free`.

## Decisions for the Operator

1. **Empty sessions and stages are quarantined (Operator decision, 2026-11-24).** The drafter remembers that the pinned SDK 1.1.0 requires a session's and each stage's start to be before its end. This is **to be confirmed** from the SDK source in the executable pack, and validity follows the SDK exactly (Decision 6). A zero-length session or stage cannot be placed on a timeline. The alternative, accepting them with zero duration, would hand `derive-sleep-summary` intervals no real record has.
2. **A stage outside the session quarantines the record.** A stage outside its session contradicts the session it belongs to, and the session's wake date would no longer cover it. The alternatives:
   - clip the stage, which changes source evidence;
   - keep it, which lets a session's stages reach outside the session's own interval.
3. **Overlaps are kept.** ADR 0041 assigns overlap handling to the summary: same-class overlaps union, differently classified overlaps are unresolved. The processor keeps every stage so that rule can run. The drafter does not remember whether SDK 1.1.0 also rejects overlapping stages; the executable pack records the answer. If it does, real data has no internal overlaps, and keeping them changes nothing.
4. **Stage codes are SDK integers (Operator decision, 2026-11-24).** The code is lossless and uses the parent's `code` kind. Names such as "light" are presentation. The alternative, symbolic strings, adds a mapping that a future SDK constant would also have to update.
5. **Title and notes are included verbatim (Operator decision, 2026-11-24).** ADR 0024 keeps each type's meaning in its body, and erasure already covers bodies (ADR 0022). They never appear in reasons, pointers, or failures. The alternative, leaving them only in the retained projection, would make the revision an incomplete interpretation of the session.
6. **Shared conventions for the four processors (Operator decision, 2026-11-24).** These hold for `interpret-steps@1`, `interpret-heart-rate@1`, `interpret-sleep-session@1`, and `interpret-weight@1` alike:
   - **Identity.** The processor version is `interpret-<type>@1`. Supported schema versions are `{1}`. The body version is 1.
   - **Field names.** Body fields use the projection's field names verbatim. `NumericField` declares the unit or code; the name does not change.
   - **No header copy.** The body never repeats a header field.
   - **Nested instants.** An instant inside the body keeps the projection's exact RFC 3339 text. It carries no epoch number and is never rounded (ADR 0025).
   - **Absence.** An absent optional field is omitted, never `null`.
   - **Order.** Lists keep source order (`project-source-record@1` R8). Nothing is sorted, merged, or deduplicated.
   - **No derivation.** No totals, statistics, durations, classes, cross-record merging, source selection, unit conversion, rounding, clock, policy, or store reads (ADR 0041, 0043).
   - **SDK bounds.** Validity beyond the schema follows the pinned SDK 1.1.0 constructor rules exactly.
   - **Reasons.** Each reason starts with its type, here `sleep-`, so it never reuses a common reason or collides with another processor's.
   - **Pointers.** A pointer names the offending source field, down to the list element, such as `/record/stages/3/end_time`. With several faults, the first in traversal order wins: `projection/v1/model.json` field order, list elements by index, depth first (`project-source-record@1` R12).
   - **Totality.** `interpret_body` never raises on a schema-valid record (R6).
   - **Nested-instant helper.** See Decision 8.
7. **No named failures.** A Quarantine is an outcome, not a failure. The parent's contract likewise lists only its invariant refusals. A defect in this processor surfaces as the parent's `processor-invariant-violated`. So `named_failures` is empty, and the quarantine output names the four reasons. The governance test checks both.
8. **One shared private helper for nested instants.** The parent's instant parser is private. The heart-rate and sleep processors therefore share one private module, `activity_tracker_portal._interpret_nested_time`. It:
   - parses a `projection/v1` instant into epoch nanoseconds, including year 0000, with integer arithmetic and without `datetime`;
   - parses an applied offset, `±hh:mm` or `±hh:mm:ss`, into seconds;
   - gives the proleptic Gregorian date of an instant shifted by an offset, or none outside years 1–9999.

   This processor uses only the first. The helper is implementation, not Interface. No suite imports it; each suite pins its behavior through its processor's outcomes. Vouch could list one file in several packs, because it rejects a duplicate path only within one pack. But this project binds acceptance files, never implementation (`docs/governance/delivery-flow.md`, step 6), so binding the helper would freeze code before it is written. It stays unbound, like the parent's own module. The first of the two processors implemented adds it, and the other reuses it. CI runs both runners, so a later change to it must keep both suites green.

**Known parent behavior, not a decision here.** The parent makes a session Policy-Pending when either endpoint lacks both an offset and an applicable policy, although only the end decides the wake date (parent Decision 4). The Operator chose that rule for every type.

## Ground rules

- **Data.** Python only, standard library only. Inputs are the Sleep Session vectors in `projection/v1/vectors/positive.json` and hand-built synthetic Sleep Session projections. Every outcome test runs end to end through the published `interpret`, with synthetic Fallback Timezone Policy timelines.
- **Time.** Synthetic sessions use nine-digit fractions, equal instants written with different fractions, stages at both session bounds, and instants near years 1 and 9999.
- **Markers.** A health-data marker in `title` and `notes` and an identity marker in Source Identities prove that Quarantines carry neither.
- **Unbound inputs, reviewed rather than frozen.** The binding is the contract, the spec, the suite, the runner, and the effects declaration. These inputs stay outside it:
  - `docs/governance/scope-map.md`, read by R1's scope-map test;
  - the published `interpret-source-projection@1` code, contract, and pack, which its own binding freezes;
  - `projection/v1/`, which `project-source-record@1`'s binding freezes;
  - the shared helper `_interpret_nested_time` (Decision 8);
  - the execution environment: `uv`, `pyproject.toml`, `uv.lock`, and the managed Python;
  - `tests/governance/test_interpret_sleep_session_contract.py`, which the governance suite runs and this runner does not.
- **Not mechanically provable here**, so the approver confirms these in review:
  - the start-before-end rules match the SDK 1.1.0 source the executable pack cites, and the pack records whether the SDK rejects overlapping stages;
  - no I/O, clock, or timezone database access, beyond what the purity test observes;
  - no sorting, merging, clipping, or classification.

## Freeze prerequisites

After Gate A, the executable pack adds:
- the suite `tests/acceptance/test_interpret_sleep_session_1.py`, with the tests above;
- the runner `tests/acceptance/run_interpret_sleep_session_1.sh`;
- the Interface stub `src/activity_tracker_portal/interpret_sleep_session.py`: `PROCESSOR` with its real identity attributes and an `interpret_body` that raises `NotImplementedError`;
- the confirmed SDK rules, citing the `SleepSessionRecord` source in connect-client 1.1.0.

RED must come entirely from the stub. The parent turns any processor exception into `processor-invariant-violated`, so tests that run through `interpret` fail with that failure. The approver confirms at Gate B that each such failure traces to the stub. The implementer's read-only findings must be resolved before freeze.
