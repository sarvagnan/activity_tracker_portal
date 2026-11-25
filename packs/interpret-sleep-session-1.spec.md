# Draft acceptance pack — `interpret-sleep-session@1`

**Status:** Draft approved by the Operator on 2026-11-24, with every decision as recommended and the follow-ups recorded below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

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

1. **Empty sessions and stages are quarantined (Operator decision, 2026-11-24).** The pinned SDK 1.1.0 requires a session's and each stage's start to be before its end, confirmed from `androidx/health/connect/client/records/SleepSessionRecord.kt` lines 51 and 180 in connect-client-1.1.0-sources.jar (SHA-256 `f80f4869872d8480e0e55aa76dd7a8bd6de556cc21d77cb483aae0fa42e00da2`, from Google Maven). Validity follows the SDK exactly (Decision 6). The SDK also requires every stage to lie within the session (lines 58–59), which matches the stage-outside-session quarantine. A zero-length session or stage cannot be placed on a timeline. The alternative, accepting them with zero duration, would hand `derive-sleep-summary` intervals no real record has.
2. **A stage outside the session quarantines the record.** A stage outside its session contradicts the session it belongs to, and the session's wake date would no longer cover it. The alternatives:
   - clip the stage, which changes source evidence;
   - keep it, which lets a session's stages reach outside the session's own interval.
3. **Overlaps are kept, whatever the SDK does (Operator decision, 2026-11-24).** ADR 0041 assigns overlap handling to the summary: same-class overlaps union, differently classified overlaps are unresolved. The processor keeps every stage so that rule can run. SDK 1.1.0 does reject overlapping stages. It sorts the stages by start and requires each stage's end to be no later than the next stage's start (`SleepSessionRecord.kt` lines 53–56). Stages that only touch are allowed. If it does, real data has no internal overlaps, and keeping them changes nothing. The Operator chose to keep overlaps even if the SDK forbids them, as a stated exception to Decision 6: evidence is never discarded, and the summary owns overlap handling.
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

The Operator approved the draft on 2026-11-24. The executable pack adds:
- the suite `tests/acceptance/test_interpret_sleep_session_1.py`: 27 tests across R1–R6, one per proposed test name, with no additions;
- the runner `tests/acceptance/run_interpret_sleep_session_1.sh`;
- the Interface stub `src/activity_tracker_portal/interpret_sleep_session.py`:
  - `PROCESSOR`, an instance of a frozen dataclass with the Interface's real `record_type`, `processor_version`, `supported_schema_versions`, `body_schema`, and `quarantine_reasons`;
  - `interpret_body`, which raises `NotImplementedError("interpret-sleep-session@1")`;
  - a module docstring citing the SDK source below;
- the confirmed SDK rules: `SleepSessionRecord.kt` in connect-client-1.1.0-sources.jar (SHA-256 `f80f4869872d8480e0e55aa76dd7a8bd6de556cc21d77cb483aae0fa42e00da2`), line 51 for the session's start before its end, line 180 for each stage's start before its end, and lines 58–59 for stages inside the session, both ends inclusive. Lines 53–56 reject overlapping stages; this pack keeps them (Decision 3).

The shared helper `_interpret_nested_time` is not part of this pack. The stub does not create it, and no test imports it. Every test runs through the published `interpret`; none calls `interpret_body` directly.

**RED, against the stub.** 22 of the 27 tests fail. The five R1 and R2 tests pass, because they read only the contract, scope map, effects declaration, and identity attributes. Counting subtests, there are 574 reports:
- 164 errors, each `InterpretFailure("processor-invariant-violated")` from `interpret`;
- 409 failures in `test_schema_valid_edges_never_raise`, where the test turns that same refusal into an assertion failure, `interpret raised processor-invariant-violated`;
- 1 failure in `test_outcomes_are_health_data_free`, whose final check that all four reasons were seen fails because every case was refused.

A trace around `interpret_body` records 573 calls, each raising the stub's `NotImplementedError("interpret-sleep-session@1")`, one for each of the 164 + 409 refusals. In `test_common_outcomes_take_precedence` the precedence cases pass, because the parent decides them before it calls the processor; the test fails on its controls, which reach the processor.

**Passability.** A throwaway reference implementation, kept outside the repository with its own private nested-instant helper, passes all 27 tests. Of 53 incorrect implementations, 52 fail at least one test:
- validity: an empty stage or session accepted; either session bound off by one in either check; the stage start's upper bound unchecked; an end before the session start reported as outside; an empty stage reported as reversed; the reversed check before the start check; the session check after the stages; a stage end clipped to the session;
- overlaps and gaps: overlaps quarantined, in source order or after an SDK-style sort; same-code overlaps merged; duplicates removed; stages sorted by instant or by text; gaps filled with code 0; code 0 stages dropped;
- instants: compared without the fraction, at whole seconds, truncated or rounded to microseconds, through float seconds, as text, or parsed through `datetime`; the session compared by text; year 0000 raising in the helper;
- traversal and pointers: all starts before all ends; reverse index order; time order; the index off by one; reversed, empty, or session faults at the wrong field; a stage pointer without its field; the fault chosen by key order in the projection bytes;
- body: header instants or an offset copied in; `null` or `""` for an absent title or notes; an empty title dropped; title stripped or NFC-normalized; notes dropped; stage instants rewritten to nine digits, trimmed, or cut to milliseconds; a float code passed through; a class or duration added;
- complexity: a quadratic scan.

The survivor builds the stage list by copying it on every append, which is quadratic. It takes about 20 s for 100,000 stages against the reference's 2.6–3.1 s, so it stays under the limit below. The approver confirms linear work in review.

**What the tests pin beyond the approved text.** None of these changes approved semantics.
- **Identity (R2).** `PROCESSOR` is immutable: assigning any identity attribute raises `AttributeError`. `supported_schema_versions` and `quarantine_reasons` are `frozenset`s, and `numeric_fields` is a tuple.
- **Codes (R3, R6).** The parent's schema accepts an integral JSON number written with a fraction or exponent, such as `5.0`, `-0.0`, `7e0`, or `30E-1`, so it reaches the processor as a Python `float`. The body holds the `int` code. Passing the float through would raise `processor-invariant-violated` on a schema-valid record.
- **Exact bytes (R3, R5).** Every body is compared with the RFC 8785 bytes of an independently built expected body.
- **Title and notes (R3).** Empty strings stay `""`. Whitespace, NUL and other controls, and unnormalized Unicode stay as written. Absent fields are omitted. This holds whatever the key order in the projection bytes.
- **Bounds (R4).** Each bound is tested one nanosecond inside and outside, with nine-digit session fractions, and with equal instants written with 0, 3, 6, or 9 fractional digits. A stage that starts at the session end is outside at its `end_time` if it ends later, and empty if it ends there.
- **Session fault (R4).** A session whose start and end are the same instant in different texts is empty. The empty-session fault wins even when every stage is also faulty.
- **Stage faults (R4).** A stage whose start is outside the session is reported at `start_time`, even when its end is also outside, reversed, or empty. A reversed stage that also ends before the session start is reversed, not outside.
- **Traversal (R4).** Every multi-fault fixture runs with its keys in model order and reversed at every depth. Fixtures cover stages listed out of time order and indices 10 and 12.
- **Precedence (R4).** Besides `interval-reversed` and Policy-Pending, a schema-invalid stage code and an `instant-out-of-range` session start come before stage faults. Each has a control that reaches the processor.
- **Policy identity (R4, R6).** A processor Quarantine carries the policy identity the parent assigns: `"none"` when both endpoints are experienced, and the policies used otherwise.
- **Totality (R6).** Besides fixed edges near years 0000, 1, and 9999, 400 seeded sessions with stages near every bound are checked against an independent oracle of the R4 rules. Each gives exactly the oracle's body or Quarantine.
- **Purity (R6).** A subprocess, as in the parent pack, imports the module after installing an audit hook and clock guards, and runs representative outcomes under two `TZ`, `LC_ALL`, and `PYTHONHASHSEED` settings. It must see no clock read, write, host timezone read, socket, store, or subprocess, no `operational_store` import, and the same results as in-process.
- **Large session (R6).** Two 100,000-stage sessions, one a revision and one quarantined at stage 99,999, must each be interpreted within 60 s. The reference takes 2.6–3.1 s on the drafting machine, and up to 5.5 s under load.
- **Health-data-free (R6).** Quarantines for all four reasons have exactly the parent's four fields. Their `repr`, `str`, and fields carry no marker, Source Identity, title or notes text, or instant text.

**Choices the approver should check.**
- **Year 0000 end.** R4's year-0000 bullet says a year-0000 stage instant is `sleep-stage-outside-session`. That holds for a stage start. For a stage whose start is inside the session and whose end is in year 0000, R4's stage-end rules make it `sleep-stage-reversed` at `end_time`, because the end is before the start. The suite pins `sleep-stage-reversed` for that case. The bullet's "it never raises" holds for both.
- **Instant source.** R4 says instants compare by exact nanoseconds. The tests check this through behavior. Whether the session bounds come from the temporal header or from the record's own text cannot be told apart and remains a review item.

The implementer's read-only findings must be resolved before freeze. The approver confirms at Gate B that each RED failure traces to the stub.
