# Draft acceptance pack — `interpret-source-projection@1`

**Status:** Draft approved by the Operator on 2026-11-24, including the 13 decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `interpret-source-projection` Governed Scope contract, version 1, in `governance/scopes/interpret-source-projection.1.json`. It depends on the published `reconcile-source-record@1`.

Phase 4 turns archived Raw Records into Canonical Revisions. `reconcile-source-record@1` decides which projection of a Source Record is current. This unit defines what happens to that projection next. It is the common contract that the four concrete processors (`interpret-steps`, `interpret-heart-rate`, `interpret-sleep-session`, `interpret-weight`) plug into, and that `execute-processing-job` calls. It never persists anything; `operational-store@4` stores what it returns.

A Canonical Revision is "a shared identity, provenance, and temporal header plus a versioned record-type-specific body" (CONTEXT.md; ADR 0022, 0024). This unit owns the header. A concrete processor owns only its body. Every projection reaches exactly one of four outcomes (CONTEXT.md):
- a **Canonical Revision**;
- **Unmodeled**: the projection is valid, but no interpretation exists for it yet (ADR 0010, 0027);
- **Policy-Pending**: interpretation needs a Fallback Timezone Policy that does not exist yet (ADR 0025);
- **Quarantine**: the projection cannot be interpreted safely (ADR 0012, 0021).

A defect rather than bad source data is not an outcome. It raises a named failure, which `execute-processing-job` records as a failed job (ADR 0021).

## Interface

The module `activity_tracker_portal.interpret_source_projection` provides frozen dataclasses, two functions, and one exception.

**Input.**
- `ProcessingInput`: `source_system_kind`, `origin`, `record_type`, `source_identity`, `record_id`, `selection_id`, `evidence_id`, `projection_sha256`, `last_modified_time` (timezone-aware), `projection_schema_version`, and `projection` (the exact archived bytes). These are the values `operational-store@3` holds for one projection selection. The module defines its own type and does not import the store.
- `FallbackTimezonePolicy(policy_id, effective_from, zone)`: `effective_from` is a timezone-aware instant, or `None` for "all earlier history"; `zone` is an IANA name.

**Time.**
- `Instant(text, epoch_ns)`: the projection's exact RFC 3339 text and the same instant in integer nanoseconds since the Unix epoch.
- `Endpoint(instant, source_offset, applied_offset, local_date, attribution, policy_id)`:
  - `source_offset` is the projection's `±hh:mm` offset, or `None`;
  - `applied_offset` is the offset actually used, as `±hh:mm`, or `±hh:mm:ss` for an inferred offset with a nonzero seconds part (R8);
  - `attribution` is `"experienced"` or `"inferred"`;
  - `policy_id` names the Fallback Timezone Policy for an inferred endpoint, and is `None` otherwise.
- `TemporalHeader(shape, start, end, explicit_time)`: `shape` is `"instant"` or `"interval"`. An instant has only `start`; `end` is `None`.

**Processor plug-in.**
- `NumericField(pointer, kind, unit)`: `pointer` is a JSON pointer into the body, with `*` for any list index. `kind` is `"integer"`, `"float"`, or `"code"`. A code has no unit; an integer or float has one.
- `BodySchema(record_type, body_version, numeric_fields)`.
- `Processor`, a protocol with:
  - `record_type`, `processor_version`, `supported_schema_versions`, `body_schema`, `quarantine_reasons`;
  - `interpret_body(record, temporal) -> Mapping | Quarantine`, where `record` is the decoded `record` object of the projection.

**Outcomes.** Every outcome carries `processor_version` and `policy_identity`. The processor version is the given processor's, or `COMMON_VERSION` when there is none or the outcome comes from `decode_envelope`. The policy identity is `"none"` unless a policy was used (R8).
- `CanonicalRevision(header, header_bytes, body_bytes, body_sha256, revision_sha256)`. `header` is a `RevisionHeader` (R10).
- `Unmodeled(reason)`.
- `PolicyPending(policy_kind, reason)`.
- `Quarantine(reason, pointer)`.

**Operations.**
- `decode_envelope(projection, record_type, projection_schema_version) -> Envelope | Quarantine`. `Envelope` has `source_system_kind`, `origin`, `source_identity`, `last_modified_time` (a `datetime`), and `last_modified_instant` (an `Instant`).
- `interpret(input, processor, policies) -> CanonicalRevision | Unmodeled | PolicyPending | Quarantine`. `processor` is the caller's mapped processor, or `None`.
- `InterpretFailure(failure)`: carries one named failure.
- `COMMON_VERSION = "interpret-source-projection@1"` and `UNITS`, the canonical unit vocabulary (R9).

## Requirements

### R1 — Governed Scope contract

The contract has the shared shape, kind `shared`, version 1, and depends only on `reconcile-source-record@1`. Its four named failures are invariant refusals. It has no observable effects, and its effects declaration is empty. It does not contradict the scope-map row.

Proposed tests: `test_contract_shape_and_dependency`, `test_scope_map_row_does_not_contradict_contract`, `test_effects_declaration_is_empty`.

### R2 — Pure and deterministic

- `interpret` and `decode_envelope` read only their arguments, the closed schemas, and the pinned timezone data.
- The same arguments always give an equal outcome with identical bytes, whatever the process, locale, or `TZ` setting.
- They read no clock, store, network, or host timezone database, and write nothing.

Proposed tests: `test_same_input_gives_identical_outcome`, `test_outcome_ignores_host_timezone_and_locale`, `test_interpret_has_no_store_or_clock_dependency`.

### R3 — Envelope decoding for indexing

`decode_envelope` is the one decoder the `portal-worker` uses to build `operational-store@3` index entries, and `interpret` uses it too (Decision 11).
- It parses strictly: one JSON object, no duplicate keys, no non-finite numbers.
- It requires `record_type` and `projection_schema_version` to equal the batch's.
- It requires a non-empty string `metadata.id` and `metadata.data_origin.package_name`, and a valid `metadata.last_modified_time`.
- It returns the source-system kind `health-connect`, the origin, the Source Identity, and the modification time. The `datetime` is truncated to microseconds, the store's precision; the `Instant` keeps the exact text.
- A `metadata.last_modified_time` in year 0000 is valid in the projection but has no `datetime`. It returns `Quarantine("instant-out-of-range", "/metadata/last_modified_time")` (Operator decision, 2026-11-24; see R7).
- Otherwise it returns `Quarantine("projection-not-json", "")` or `Quarantine("projection-envelope-invalid", pointer)`.
- It does not validate the whole schema. A projection with a usable envelope but an invalid body becomes a Source Record and is quarantined by `interpret`, so its history stays inspectable.

Proposed tests: `test_envelope_yields_index_key_and_time`, `test_unparseable_bytes_are_quarantined`, `test_envelope_field_failures_name_their_pointer`, `test_envelope_disagreeing_with_batch_is_quarantined`, `test_envelope_keeps_exact_instant_text`.

### R4 — One outcome, in a fixed order

`interpret` checks, in this order, and stops at the first that applies:
1. `processing-input-invalid` when the input is malformed:
   - a naive timestamp;
   - a `record_id`, `selection_id`, or `evidence_id` that is not a positive Python `int` (a `bool` is not an `int` here);
   - a policy timeline that is not strictly increasing, has duplicate identities, or names an unknown zone;
   - a processor whose record type differs from the input's.
2. `projection-digest-mismatch` when the bytes' SHA-256 differs from the evidence digest.
3. `projection-identity-mismatch` when `decode_envelope` fails, or its record type, origin, Source Identity, source-system kind, or microsecond modification time differs from the input.
4. Quarantine `projection-schema-invalid` when the projection fails its closed schema (R5).
5. Quarantine `instant-out-of-range` when a temporal-header instant falls in year 0000 (R7).
6. Unmodeled when there is no processor, or it does not support the schema version (R6).
7. Quarantine `interval-reversed` when an interval ends before it starts (R7).
8. Quarantine `instant-out-of-range` when an endpoint's local date falls outside years 1–9999 (R7). Otherwise, Policy-Pending when an endpoint needs a policy that does not apply (R8).
9. The processor's Quarantine, if it returns one (R9).
10. `processor-invariant-violated` when the processor's result breaks R9 or R10.
11. Otherwise, the Canonical Revision.

Steps 5 and 8 were added by the Operator decision on the year range (R7).

Proposed tests: `test_check_order`, `test_each_failure_is_raised_not_returned`, `test_malformed_input_is_refused`.

### R5 — Closed-schema validation of every type

- Every projection is validated against the `projection/v1` closed schema for its record type and schema version, including the 37 types with no processor (Decision 3).
- Every positive vector in `projection/v1/vectors/positive.json` passes.
- Every projection in `projection/v1/vectors/schema-negatives.json` whose envelope is usable is quarantined as `projection-schema-invalid`. The three whose envelope is unusable (`missing-metadata-id`, `wrong-record-type`, `instant-non-leap-february-29`) are handled earlier: `decode_envelope` quarantines them as `projection-envelope-invalid` (R3), and a mismatched identity reaching `interpret` raises `projection-identity-mismatch` (R4). The Operator chose this on 2026-11-24, when the executable pack found that the approved text contradicted R3 and R4.
- The pointer names the first offending field in the traversal order of `project-source-record@1` R12: `metadata` before `record`; fields in `projection/v1/model.json` order; list elements by index; depth first.
- The validator is standard-library code. The suite cross-checks it with the `jsonschema` development dependency on every vector (Decision 2).

Proposed tests: `test_every_positive_vector_is_schema_valid`, `test_every_schema_negative_is_quarantined`, `test_schema_pointer_follows_traversal_order`, `test_validator_agrees_with_jsonschema_on_every_vector`.

### R6 — Unmodeled

- With no processor, a schema-valid projection is `Unmodeled("record-type-not-modeled")` under `COMMON_VERSION`.
- With a processor that does not list the schema version, it is `Unmodeled("schema-version-not-modeled")` under the processor's version.
- Unmodeled builds no header and never needs a policy.

Proposed tests: `test_unmapped_type_is_unmodeled`, `test_unsupported_schema_version_is_unmodeled`, `test_unmodeled_needs_no_policy`.

### R7 — Temporal header

The projection's schema fixes its shape.
- **Shape.** A `time` field gives an instant. `start_time` and `end_time` give an interval; a series is an interval whose samples belong to the body.
- **Instants.** Each instant keeps its exact RFC 3339 text, up to nine fractional digits, and its epoch nanoseconds. Nothing rounds (ADR 0025).
- **Offsets.** Each endpoint's source offset is kept independently, or is `None` when absent. `zone_offset` belongs to the instant; `start_zone_offset` and `end_zone_offset` to their endpoints.
- **Experienced attribution.** With a source offset, the local date is the date of the instant shifted by that offset. The attribution is `experienced`. A source offset is never overridden by a policy.
- **Explicit time.** `explicit_time` is the projection's `has_explicit_time` where the type has it, and `true` otherwise.
- **Reversed interval.** An interval whose end is before its start is quarantined as `interval-reversed` with the pointer `/record/end_time`. An equal start and end is accepted here; a processor may refuse it.
- **Year range (Operator decision, 2026-11-24).** The projection admits years 0000–9999, but a Python `date` or `datetime` holds only years 1–9999. Anything outside that range is quarantined with the common reason `instant-out-of-range` and the pointer of the instant's field, never a value:
  - a temporal-header instant in year 0000, at R4 step 5, before Unmodeled;
  - a derived local date outside years 1–9999, experienced or inferred, at R4 step 8. Examples are `0001-01-01T00:00:00Z` with offset `-00:01`, and `9999-12-31T23:00:00Z` with `+01:00`. This takes precedence over Policy-Pending. Unmodeled derives no local date, so it is unaffected;
  - the envelope's `metadata.last_modified_time` in year 0000. `decode_envelope` quarantines it with this reason rather than `projection-envelope-invalid`, because the bytes are well formed and the reason names the actual limit (R3).

  Only the temporal-header instants (`time`, `start_time`, `end_time`) and the envelope's modification time are checked here. Instants nested in the body, such as samples and stages, belong to the processor.

Proposed tests: `test_instant_and_interval_shapes`, `test_instants_keep_nanosecond_text_and_epoch`, `test_offsets_are_independent_per_endpoint`, `test_experienced_local_date`, `test_explicit_time_flag`, `test_reversed_interval_is_quarantined`, and, for the year-range decision, `test_out_of_range_instants_are_quarantined`.

### R8 — Fallback timezone attribution and Policy-Pending

- **Applicable policy.** For an endpoint without a source offset, the applicable policy is the last one whose `effective_from` is at or before the instant, or a `None` start.
- **Inferred attribution.** With an applicable policy, the applied offset is the zone's UTC offset at that instant. The local date follows from it. The attribution is `inferred` and names the policy.
- **Inferred offsets with seconds (Operator decision, 2026-11-24).** Historical zone offsets can have seconds. For example, `America/New_York` in 1800 is `-04:56:02`. An inferred offset is stored as `±hh:mm`, or as `±hh:mm:ss` when its seconds part is nonzero. The local date uses the exact offset, so at `1800-01-02T04:56:01Z` it is January 1. Source offsets stay minute-precision, as the projection writes them.
- **Timezone data.** Zones load only from the pinned `tzdata` package, never the host database. Its IANA version is part of the policy identity (Decision 5).
- **Policy-Pending.** If any endpoint has no source offset and no applicable policy, the outcome is `PolicyPending("fallback-timezone", "fallback-timezone-policy-missing")` (Decision 4).
- **Policy identity.** The policy identity is `"none"` when every endpoint is experienced. Otherwise it is `"fallback-timezone:"` followed by the sorted policy identities used and the tzdata version, in a documented canonical form. It names only the policies that changed the result, so a policy change never alters a revision that used none.
- **Policy identity on other outcomes.** Every endpoint is attributed before any outcome is chosen. A Policy-Pending or `instant-out-of-range` outcome therefore carries the identity of every policy applied to any endpoint, in the same form. Examples are a pending interval whose other endpoint was inferred, and a local date that a policy pushed out of range. The identity is `"none"` only when no policy was applied. This restores the approved guarantee after GPT 6 Astra's Gate B review.

Proposed tests: `test_inferred_local_date_uses_applicable_policy`, `test_inferred_offset_keeps_seconds` (for the seconds decision), `test_policy_boundary_is_inclusive`, `test_explicit_offset_overrides_policy`, `test_missing_policy_is_policy_pending`, `test_one_missing_endpoint_is_policy_pending`, `test_policy_identity_names_only_policies_used`, `test_zones_load_from_pinned_tzdata`.

### R9 — Processor plug-in and body

- The processor receives the decoded `record` object and the built temporal header. It returns a body mapping or a `Quarantine`.
- A processor's Quarantine reason must be one it declares in `quarantine_reasons`. It cannot reuse a common reason.
- A processor's Quarantine pointer must name a field, never a value (R11). The common layer checks both its grammar and its membership in the source schema. Each segment must be a field name that the projection's closed schema declares at that position, in any branch of a `oneOf`, or a list index. Field names are lowercase letters, digits, and underscores; an index is `0` or has no leading zero. A declared field that is absent from this projection may be named. Otherwise the outcome is `processor-invariant-violated`, and the failure does not carry the pointer.
- The body is JSON-compatible: objects with string keys, lists, strings, booleans, and numbers.
- Every number in the body matches exactly one declared `NumericField`:
  - an `integer` or `code` is a Python `int`, never a `bool`;
  - a `float` is a finite Python `float`, so integer and floating values stay distinct (ADR 0025) even though RFC 8785 prints `72500.0` as `72500`;
  - an `integer` or `float` has a unit from `UNITS`; a `code` has none.
- `UNITS` is the projection's storage units (`project-source-record@1` R6): `g`, `cal`, `m`, `L`, `m/s`, `W`, `mmHg`, `degC`, `mmol/L`, `percent`. It adds `count`, `1/min` for rates such as beats per minute, `ns` for durations, and `mL/(kg*min)`.
- A body value is the projection's value in that unit. Nothing is converted or rounded (Decision 8).
- The body bytes are RFC 8785 canonical UTF-8 JSON.
- **Body integer domain (Operator decision, 2026-11-24).** The body follows the I-JSON safe-integer domain. Every Python `int` in the body lies within ±(2^53 − 1); otherwise the outcome is `processor-invariant-violated`. This also excludes 2^53, although binary64 holds it exactly, because the I-JSON domain is the interoperable one. Finite floats keep their RFC 8785 rendering, so the float `1e16` is valid and is written `10000000000000000`.

Proposed tests: `test_processor_receives_record_and_header`, `test_processor_quarantine_uses_declared_reason`, `test_undeclared_reason_violates_invariant`, `test_undeclared_numeric_field_violates_invariant`, `test_integer_and_float_kinds_are_enforced`, `test_unknown_unit_violates_invariant`, `test_body_bytes_are_canonical`, `test_processor_exception_is_an_invariant_violation`, and `test_processor_pointer_must_name_a_schema_field` (added after the implementer's review).

### R10 — Revision header and identity

The header is RFC 8785 canonical JSON with these fields, and nothing a processor supplies:
- **Identity:** `source_system_kind`, `origin`, `record_type`, `source_identity`, `record_id`.
- **Provenance:**
  - `selection_id`, `evidence_id`, `projection_sha256`;
  - `projection_schema_version`, `projection_implementation_version`, `sdk_version`, `provider_version`;
  - `last_modified_time` (exact text), `recording_method`, and `device` when present.
  - Never `client_record_id` (ADR 0050).
- **Time:** the temporal header of R7 and R8, under the key `time`. This layout was documented after the implementer's review:
  - `time` is `{"shape", "explicit_time", "start", "end"}`, with `end` only for an interval;
  - each endpoint is `{"instant", "applied_offset", "local_date", "attribution"}`, plus `source_offset` and `policy_id` when they are not `None`;
  - `instant` is the exact text and `local_date` is `YYYY-MM-DD`. Epoch nanoseconds are not serialized, because they follow from the exact text.
- **Versions:** `header_version` (`COMMON_VERSION`), `processor_version`, `body_schema` (record type and body version), and `policy_identity`.
- **Store identities as text (Operator decision, 2026-11-24).** `record_id`, `selection_id`, and `evidence_id` are written in the header bytes as decimal strings, such as `"record_id":"9007199254740993"`. That keeps any size exact. SQLite `INTEGER` keys can exceed what an RFC 8785 number holds. In `RevisionHeader` they stay Python `int`s, and R4 step 1 refuses any that is not a positive `int`. Every other integer in the header is schema-bounded and stays a JSON number.
- **Body:** `body_sha256`.

`revision_sha256` is the SHA-256 of the header bytes, so it covers the body. The processor must meet these rules, or the outcome is `processor-invariant-violated`:
- `processor_version` has the form `<scope>@<version>`, for example `interpret-steps@1` (Decision 7);
- the body schema's record type equals the processor's;
- the body schema's `body_version` is a positive Python `int` (not a `bool`) within 2^53 − 1, the I-JSON domain of the Operator's body-integer decision.

The Portal revision identity is assigned when `operational-store@4` stores the revision, not here.

Proposed tests: `test_header_carries_identity_provenance_and_versions`, `test_header_omits_client_record_id`, `test_revision_digest_covers_header_and_body`, `test_processor_version_form_is_checked`, `test_body_schema_must_match_processor_type`, and, for the store-identity decision, `test_store_identities_are_exact_decimal_text`.

### R11 — Health-data-free outcomes and failures

- Quarantine, Unmodeled, and Policy-Pending carry only their reason, pointer, policy kind, processor version, and policy identity.
- `InterpretFailure` text and every outcome's `repr`, except a revision's own header and body bytes, carry no projection bytes, field values, or Source Identity.
- A processor exception's message is not carried forward.
- Reasons are lowercase kebab-case names; pointers name fields, never values.

Proposed tests: `test_outcomes_are_health_data_free`, `test_failures_are_health_data_free`, `test_processor_exception_text_is_not_carried`.

## Decisions for the Operator

1. **The common layer owns the header; a processor returns only its body.** The header's identity, provenance, and time are built and checked once, so no processor can forge them. The alternatives are processors that build a whole revision for a common checker to validate, or an inheritance base class. Either duplicates header logic in four places.
2. **A standard-library closed-schema validator.** It covers the JSON Schema subset `projection/v1` uses: type, required, closed objects, enum, pattern, bounds, and local references. The suite checks that it agrees with `jsonschema` on every vector. This follows the Portal's precedent of explicit decoders (`ingest_protocol`) and adds no production dependency. The alternative is to move `jsonschema` 4.26.0 and its dependencies, including the native `rpds-py`, into production, which ADR 0035 governs as an upgrade.
3. **Every type is validated, including unmodeled ones.** Unmodeled means "understood but not yet interpreted" (CONTEXT.md). An invalid projection of an unmodeled type is therefore quarantined, not Unmodeled. All 41 schemas already exist. The alternative, validating only modeled types, would label invalid data as merely Unmodeled.
4. **Any endpoint without an offset or policy makes the record Policy-Pending.** This is conservative and the same for every type. The alternative lets each processor declare which endpoints need attribution, for example only a sleep session's end (ADR 0041). That is finer, but it moves a common rule into four processors.
5. **Policies are evaluated now, against a pinned `tzdata`.** The input already accepts an effective-dated Fallback Timezone Policy timeline. Tests supply synthetic timelines until `set-fallback-timezone-policy` records real ones. The IANA version is part of the policy identity, so a tzdata upgrade is visible as reinterpretation. This adds `tzdata` as a pinned production dependency (ADR 0035). The alternatives:
   - the host timezone database, which is not reproducible;
   - Policy-Pending for every missing offset now, with evaluation in a later version of this unit. That requires a supersede before policies are useful.
6. **Instants keep their exact text and epoch nanoseconds.** Projections carry up to nine fractional digits, and a Python `datetime` holds six. The alternative, `datetime`, would round, which ADR 0025 forbids.
7. **A processor version is its governed unit identity.** An example is `interpret-steps@1`. A processor change is then a Vouch version whose pack governs its expected outcome transitions (ADR 0042). A type with no processor is Unmodeled under `interpret-source-projection@1`. The alternatives are free-form version strings or a code digest, neither tied to an acceptance oracle.
8. **Bodies are canonical bytes with declared kinds and units, unconverted.** The canonical unit is the projection's storage unit, so interpretation is lossless (ADR 0025). Integer and float are declared per field because canonical JSON cannot carry the difference. Display units stay with presentation. The alternatives are per-type store tables, which tie the store to every type, or converting to display units, which rounds.
9. **Integrity breaks are failures, not quarantine.** The projection was indexed from the same bytes, so a digest or identity mismatch means a defect in the Portal, not bad source data. It is recorded as a failed job until corrected behavior reprocesses it (ADR 0021). The same applies to a processor that breaks the common invariants. The alternative, quarantine, would present a Portal defect as a property of the Subject's data.
10. **One reason grammar.** Common reasons are fixed here. Each processor declares its own, and they may not reuse a common name. Every Quarantine names a JSON pointer but no value, as `project-source-record@1` R12 does. The alternative, free text, cannot be shown as health-data-free.
11. **This unit owns envelope decoding for indexing.** `operational-store@3` left key extraction to the `portal-worker`, an enabling task with no governed behavior. Owning the decoder here means indexing and interpretation agree on every key. Bytes that cannot yield a key are quarantined; `operational-store@4` records that against the archived item. The alternative leaves extraction ungoverned in the worker.
12. **Only a projection is interpreted.** A retraction or a Source Conflict has no single current projection, and neither contributes (ADR 0041). `operational-store@4` handles those selections without calling this unit. The alternative, interpreting them into special outcomes, would invent canonical meaning for evidence that has none.
13. **Python only.** Interpretation happens on the Portal after archival (ADR 0010). No other language consumes it. It reads the projection schemas and vectors that ADR 0040 makes the cross-language contract, but produces no vectors of its own.

## Ground rules

- **Data.** Python only, standard library plus the pinned `tzdata`. Inputs are the synthetic `projection/v1` vectors and hand-built synthetic projections. Tests define synthetic processors; the concrete processors arrive in their own units.
- **Markers.** A health-data marker in projection values and an identity marker in Source Identities prove that outcomes and failures carry neither (R11).
- **Time.** Synthetic instants use nine-digit fractions, several offsets, and policies across a daylight-saving transition. The suite runs under more than one `TZ` value.
- **Unbound inputs, reviewed rather than frozen.** The binding is the contract, the spec, the suite, the runner, and the effects declaration. These inputs stay outside it:
  - `docs/governance/scope-map.md`, read by R1's scope-map test;
  - `projection/v1/`, which `project-source-record@1`'s binding freezes;
  - the execution environment: `uv`, `pyproject.toml`, `uv.lock` including `tzdata` and the development `jsonschema`, and the managed Python;
  - `tests/governance/test_interpret_source_projection_contract.py`, which the governance suite runs and this runner does not.
- **Not mechanically provable here**, so the approver confirms these in implementation review:
  - no I/O, clock, or host timezone access;
  - the module does not import `operational_store`;
  - no conversion or rounding of body values;
  - the canonical policy-identity form is documented in the module.

## Freeze prerequisites

The Operator approved the draft. The executable pack adds:
- the suite `tests/acceptance/test_interpret_source_projection_1.py`, 54 tests across R1–R11, one per proposed test name. Four tests join the 50 drafted names:
  - `test_out_of_range_instants_are_quarantined` and `test_inferred_offset_keeps_seconds`, for the first two Operator decisions below;
  - `test_processor_pointer_must_name_a_schema_field`, for Sol's finding 7;
  - `test_store_identities_are_exact_decimal_text`, for the store-identity decision;
- the runner `tests/acceptance/run_interpret_source_projection_1.sh`;
- the Interface stub `src/activity_tracker_portal/interpret_source_projection.py`: `COMMON_VERSION`, `UNITS`, `InterpretFailure`, the frozen dataclasses `ProcessingInput`, `FallbackTimezonePolicy`, `Instant`, `Endpoint`, `TemporalHeader`, `NumericField`, `BodySchema`, `Quarantine`, `Unmodeled`, `PolicyPending`, `RevisionHeader`, `CanonicalRevision`, and `Envelope`, and the `Processor` protocol, with `decode_envelope` and `interpret` raising `NotImplementedError`. Every outcome carries `processor_version` and `policy_identity`; on `Quarantine`, `Unmodeled`, and `PolicyPending` they default to `COMMON_VERSION` and `"none"`, so a processor returns `Quarantine(reason, pointer)`. `RevisionHeader` has one field per R10 header entry, with the temporal header as `time`, `last_modified_time` as an `Instant`, and `body_schema` as the processor's `BodySchema`;
- `tzdata==2026.6` (IANA release `2026f`) as a pinned runtime dependency in `pyproject.toml` and `uv.lock`, per Decision 5. `jsonschema` stays a development dependency.

RED, against the stub: 51 of the 54 tests fail. Counting subtests, there are 306 errors, each a `NotImplementedError` from the stub, and 2 failures, the subprocess tests of R2 and R8, which fail with `AssertionError` because the subprocess raised the stub's `NotImplementedError`. The three R1 contract tests pass.

A throwaway reference implementation passes all 54 tests. Of 114 incorrect implementations of the high-risk rules, 113 fail at least one test:
- the 74 from the first round, rebuilt against the updated reference;
- 5 that port GPT 6.1 Sol's surviving mutants;
- 13 for the rules of Sol's round: pointer grammar only, pointer must exist, reason grammar unchecked when declared, the year-0000 envelope reported as `projection-envelope-invalid`, the year check after Unmodeled, the year check after the reversed-interval check, Policy-Pending before `instant-out-of-range`, a clamped local date, the wrong out-of-range pointer, an offset truncated to minutes, an offset text without seconds, seconds always written, and an extra key in the `time` layout;
- GPT 6 Astra's 12 independent mutants, rebased onto the updated reference;
- 8 for this Gate B round:
  - `"none"` on an inferred `instant-out-of-range`;
  - `"none"` on a partly inferred Policy-Pending;
  - attribution that stops at the first failing endpoint;
  - store identities as JSON numbers;
  - store identities rounded through a float;
  - store identities unvalidated;
  - zero accepted as a store identity;
  - `bool` accepted as a store identity;
- 2 for the `body_version` check: the check skipped, and a `bool` accepted.

The survivor accepts a `oneOf` that more than one branch matches. Every `oneOf` in `projection/v1` discriminates on a `kind` constant, so no projection can tell it apart.

A variant of the reference that calls `time.gmtime`, `time.strftime`, and `time.asctime` with explicit arguments passed all 53 tests of Sol's round. Sol's original mutants and Sol's independent reference fail against this suite, because they predate the Operator decisions below. Sol's five former survivors fail the tests aimed at them:
- `allow-wide-body-int` fails `test_integer_and_float_kinds_are_enforced`;
- `clock-at-import` fails `test_interpret_has_no_store_or_clock_dependency` with `clock read`;
- `drop-pointer-sanitization` fails `test_processor_pointer_must_name_a_schema_field`;
- `schema-input-key-order` fails `test_schema_pointer_follows_traversal_order`;
- `swapped-byte-endpoints` fails the header tests.

**Operator decisions, 2026-11-24, during the executable pack.**
- **R5 follows R3 and R4 for unusable envelopes** (below).
- **Year range** (Sol finding 2; R3, R4, R7). The projection admits years 0000–9999, but Python `date` and `datetime` hold only years 1–9999. The new common reason `instant-out-of-range` covers the rest:
  - a temporal-header instant in year 0000 is quarantined at R4 step 5, before Unmodeled;
  - a derived local date outside years 1–9999, experienced or inferred, is quarantined at R4 step 8, before Policy-Pending;
  - the pointer is the instant's field.
  `decode_envelope` returns `instant-out-of-range` at `/metadata/last_modified_time` for a year-0000 modification time, rather than `projection-envelope-invalid`, because that names the actual limit. `interpret` therefore refuses such bytes with `projection-identity-mismatch`. The tests cover:
  - year 0000;
  - `0001-01-01T00:00:00Z` with `-00:01`;
  - `9999-12-31T23:00:00Z` with `+01:00`, and inferred through `Asia/Tokyo`;
  - inferred `America/New_York` at `0001-01-01`;
  - the in-range neighbors of each.
- **Inferred offsets with seconds** (Sol finding 3; R8). An inferred offset is `±hh:mm`, or `±hh:mm:ss` when its seconds part is nonzero, and the local date uses the exact offset. Source offsets stay minute-precision. The tests cover:
  - `America/New_York` 1800 at `-04:56:02`, where `1800-01-02T04:56:01Z` is January 1 and the next second is January 2;
  - `Asia/Kolkata` 1800 at `+05:53:28`, across midnight;
  - `Asia/Kolkata` 1900 at `+05:21:10`;
  - a modern offset that keeps `±hh:mm`.

- **Store identities as text** (Astra Gate B finding 2; R4, R10). `record_id`, `selection_id`, and `evidence_id` are decimal strings in the header bytes, so any size stays exact. In memory they stay Python `int`s, and R4 step 1 refuses any that is not a positive `int`, which rules out zero, negatives, `bool`, strings, floats, and `None`. The tests cover each identity at 1, 2^53 − 1, 2^53, 2^53 + 1, 2^63 − 1, and 2^64 + 5. The header text must match exactly, and two identities beyond 2^53 that differ by 1 must give different revisions.
- **Body integer domain** (Astra Gate B finding 3; R9). The body follows the I-JSON safe-integer domain, ±(2^53 − 1). A processor returning an integer outside it raises `processor-invariant-violated`, including 2^53, which binary64 holds exactly. Finite floats keep their RFC 8785 rendering.

**GPT 6 Astra's Gate B objection (2026-11-24): resolutions.**
1. **Policy identity** (high). The approved guarantee is restored without a new decision (R8, "Policy identity on other outcomes"). Every endpoint is attributed before an outcome is chosen. Policy-Pending and `instant-out-of-range` then name every policy applied, in the R8 form, and are `"none"` only when none was applied. New assertions:
   - an inferred overflow through `America/New_York` and `Asia/Tokyo`;
   - an interval with the end out of range and the start inferred, and the reverse;
   - an experienced out-of-range start with an inferred end;
   - Astra's partly inferred pending interval, and one more like it;
   - a pending interval with an experienced start, which stays `"none"`.

   Astra's probe now gives the policy identity in both cases. The previous reference fails 5 of the new tests.
2. **Large store identities** (high). Resolved by the Operator decision above. Astra's probe now round-trips `9007199254740993` exactly.
3. **Body integer domain** (medium). Resolved by the Operator decision above. The body tests were already in place from Sol's round. The same domain now applies to the processor's declared `body_version` (R10). Values of 0, −1, `True`, 2^53, 2^53 + 1, `"1"`, `1.0`, and `None` raise `processor-invariant-violated`, and 1 and 2^53 − 1 are written exactly. These cases are in `test_body_schema_must_match_processor_type`.

**GPT 6.1 Sol's read-only review (2026-11-24): resolutions.**
1. **Integer range** (blocking). The restriction applies to integer values, meaning Python `int`s in the body, not to the tokens RFC 8785 writes for finite floats (R9). The suite's `is_canonical` now requires each number token to be the RFC 8785 rendering of the binary64 value it denotes. Under that rule, `10000000000000000` for the float `1e16` passes and `9007199254740993` fails. Tests cover:
   - ±(2^53 − 1), which pass and are written exactly;
   - ±2^53, ±(2^53 + 1), and 2^64 as `integer`, which fail with `processor-invariant-violated`, as does 2^53 + 1 as a `code`;
   - a wide int inside a list, which fails the same way;
   - the floats `1e16` and `9007199254740994.0`, which pass.
2. **Year range** (blocking). Resolved by the Operator decision above.
3. **Seconds offsets** (blocking). Resolved by the Operator decision above.
4. **Purity oracle** (blocking). `gmtime`, `asctime`, and `strftime` raise only when called without an explicit time argument. The clock functions stay forbidden unconditionally: `time`, `time_ns`, `monotonic`, `perf_counter`, `process_time`, `thread_time`, `clock_gettime`, and their `_ns` forms. `localtime`, `ctime`, and `mktime` stay forbidden.
5. **Traversal order.** Every multi-fault fixture is now serialized in reverse key order at every depth, with `record` before `metadata`, so input order and model order disagree. New cases cover model order inside a list element (`time` before `beats_per_minute`, `segment_type` before `repetitions`) and inside `metadata.device`.
6. **Endpoint association.** The serialized `time` layout is now documented in R10 and checked exactly. Every revision the suite builds must serialize `time` as the documented form of its in-memory temporal header. One fixture has endpoints whose instants, offsets, local dates, and policies all differ.
7. **Processor pointers.** The common layer checks both grammar and membership in the source schema (R9), so a pointer names declared fields, never values. Tests cover the following under a declared reason, and each fails with `processor-invariant-violated` without the pointer appearing in the failure:
   - value-bearing pointers built from record values;
   - malformed segments, leading-zero, negative, and non-numeric indices;
   - empty segments, uppercase names, and escapes;
   - non-string pointers.
   Accepted pointers include the root, declared fields absent from this projection, and fields declared by one `oneOf` branch. A declared reason must still be lowercase kebab-case.
8. **Import-time blind spot.** Taken up. The purity subprocess installs the audit hook and the clock guards before importing the module, after importing common standard-library and third-party modules, so the module's own import is observed. A C-level clock read such as `datetime.now()` remains a review obligation.
9. **R5.** Resolved by the Operator decision recorded in R5.

**Resolved by the Operator on 2026-11-24 (R5 now follows R3 and R4).** R5 said every projection in `schema-negatives.json` is quarantined as `projection-schema-invalid`. That cannot hold for 3 of the 18 negatives, whose envelope is unusable: `missing-metadata-id`, `wrong-record-type`, and `instant-non-leap-february-29`. R3 makes `decode_envelope` quarantine them as `projection-envelope-invalid`. R4 step 3 makes `interpret` raise `projection-identity-mismatch` for them before step 4 validates the schema. The suite follows R3 and R4:
- the other 15 negatives are `projection-schema-invalid` from `interpret`;
- the 3 are `projection-envelope-invalid` from `decode_envelope`, at `/metadata/id`, `/record_type`, and `/metadata/last_modified_time`, and `interpret` refuses them with `projection-identity-mismatch`.

The tests also pin these refinements:
- **Envelope (R3).**
  - Non-JSON bytes are `projection-not-json` with the pointer `""`. These include invalid UTF-8, empty bytes, trailing data, duplicate keys at any depth, `NaN` and `Infinity`, a number that overflows to infinity, and a top-level value that is not an object.
  - A valid `metadata.last_modified_time` has the `projection/v1` instant form: UTC with `Z`, 0, 3, 6, or 9 fractional digits, and a real date. An RFC 3339 instant with a numeric offset is invalid.
  - `projection_schema_version` must be a JSON integer, so `true` and `"1"` are invalid.
  - The pointers for single faults are pinned: `/record_type`, `/projection_schema_version`, `/metadata` when absent or not an object, `/metadata/id`, `/metadata/data_origin` when not an object, `/metadata/data_origin/package_name`, and `/metadata/last_modified_time`. The pointer for several envelope faults at once, and for an absent `data_origin`, is not pinned.
  - A usable envelope with an invalid body still yields its `Envelope`.
- **Schema pointers (R5).**
  - A missing required field is named by its own pointer, and so is an unknown field.
  - A `oneOf` variant failure is pinned only as a prefix, the variant's pointer.
  - Where an unknown or missing field falls in the traversal order, relative to present fields, is not pinned.
  - The `jsonschema` cross-check also covers 7,647 single-field faults derived from every positive vector outside the envelope fields: a field retyped, emptied, or deleted, or an unknown field added. 6,534 of them are invalid. The verdicts must agree, and where `jsonschema` reports one error outside a `oneOf`, the pointer must name the faulted field. A boolean is never an integer or a number.
- **Versions and policy identity on non-revision outcomes (R4, R6, R8).**
  - With a processor, a `projection-schema-invalid`, `interval-reversed`, or `instant-out-of-range` Quarantine and a Policy-Pending carry that processor's version. `record-type-not-modeled` carries `COMMON_VERSION`.
  - The schema-invalid and interval-reversed Quarantines carry the policy identity `"none"`. So do Policy-Pending and `instant-out-of-range` when no policy was applied to any endpoint.
  - Policy-Pending and `instant-out-of-range` name every policy applied to any endpoint (R8). That covers a pending interval whose other endpoint was inferred, a local date a policy pushed out of range, and an interval where one endpoint is out of range and the other inferred. The identity depends only on the set of policies used and the tzdata release, so the tests compare it with the identity of a revision that used the same policies.
  - A processor's Quarantine carries the policy identity of the temporal header it was given, which equals the identity the revision would have had.
- **Malformed input (R4).**
  - The timeline is validated eagerly. A naive `effective_from` is `processing-input-invalid`, and so is a `None` start that is not first, two policies at the same instant in different offsets, or an unknown zone in a policy that would never apply. This holds even when the outcome would otherwise be Unmodeled.
  - Timeline order and `last_modified_time` are compared as instants, whatever offset they are written in. An input time 1 µs away is `projection-identity-mismatch`, and so is a different source-system kind, origin, Source Identity, record type, or schema version.
- **Time (R7, R8).**
  - `Endpoint.applied_offset` is a `±hh:mm` string like `source_offset`, or `±hh:mm:ss` for an inferred offset with seconds; UTC is `+00:00`. `local_date` is a `datetime.date`.
  - Reversed-interval checks and policy boundaries compare at nanosecond precision, with no rounding. An equal start and end written with different text is accepted, and local wall-clock order is irrelevant.
  - A zone that exists only on the host's `PYTHONTZPATH` is an unknown zone, and a planted host copy of a real zone is never read.
- **Policy identity form (R8).** The form is pinned only through its properties:
  - it starts `fallback-timezone:`;
  - it names each policy used exactly once, in sorted order, and no unused policy;
  - it includes `tzdata.IANA_VERSION` (`2026f`), not the package version (`2026.6`);
  - adding a policy that applies to no endpoint leaves the revision bytes unchanged.
  The exact form is left to the module's documentation.
- **Header bytes (R10).**
  - The top-level keys are exactly the R10 names, with `time` for the temporal header. `device` is omitted when absent, never `null`. `body_schema` is `{"record_type", "body_version"}`. `last_modified_time` is the exact text.
  - The layout inside `time` is the documented one in R10, checked exactly.
  - Header and body bytes are RFC 8785: every number token is the rendering of the binary64 value it denotes. Body integers stay within ±(2^53 − 1) (R9). Store identities are decimal strings (R10).
- **Body (R9).**
  - The processor violates the invariant if it returns something other than a mapping, or a body containing `null`, bytes, a date, or a non-string key.
  - Every value at a declared `NumericField` pointer must be a number of its kind, so a string `"5"` at an integer field is a violation.
  - `*` matches list indices only, not object keys.
  - A number matched by two declarations is a violation, including a wildcard and an index that overlap.
  - Booleans at undeclared paths are allowed.
  - An unknown `kind`, an empty unit, or a unit in the wrong case is a violation.
  - `UNITS` is pinned as exactly the 14 units R9 lists.
- **Processor (R9, R10, R11).**
  - Returning a common reason is a violation even when the processor declares it, and so is returning a declared reason that is not lowercase kebab-case. Merely declaring one is not pinned.
  - A processor's pointer must pass the grammar and schema-membership check of R9.
  - Any `Exception` from the processor is `processor-invariant-violated`. The failure has no `__cause__`, and its formatted traceback, including the implicit exception context, carries none of the message.
  - `processor_version` has the form `<kebab-case scope>@<positive integer>`, and the check is pinned only when the processor returns a body.
- **Health-data-free outcomes (R11).** An outcome's dataclass fields are exactly those R11 names. The marker checks cover outcome `repr` and `str`, and failure `str`, `repr`, `args`, attributes, and full traceback.
- **Purity (R2).** All outcome dataclasses are frozen. Three subprocesses vary `TZ`, `LC_ALL`, and `PYTHONHASHSEED`, and their results must be identical. Another subprocess imports the module only after installing an audit hook and patching the `time` module's clock functions to raise. It must observe none of these, during the module's import or its calls:
  - a write-mode open;
  - a read of the host timezone database;
  - socket, sqlite3, or subprocess activity;
  - a clock read through the `time` module. A call to `gmtime`, `asctime`, or `strftime` with an explicit time argument is a pure conversion and is allowed;
  - an import of `operational_store`.
  This covers part of what the Ground rules list as not mechanically provable. The approver still reviews those items, because a C-level clock read such as `datetime.now()` cannot be intercepted.

The implementer reviewed the pack read-only, and the approver objected at Gate B. The findings of both are resolved above. The approver confirms the resolution before freeze.
