# Draft acceptance pack — `project-source-record@1`

**Status:** Draft approved by the Operator on 2026-10-13, including the six decisions below; the executable pack adds the refinements its tests pin down. Amended on 2026-10-13 under deviation DEV-psr-01 to `project-source-record-pack/2`, by Operator decision: the schemas accept only real calendar dates (R5, R11). Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `project-source-record` Governed Scope contract in `governance/scopes/project-source-record.1.json`. It is the pure Kotlin Module that turns one typed Health Connect record into its Source Projection bytes (ADR 0010). Reading pages, cursors, retractions, and page-level refusal handling belong to `read-snapshot-page` and `read-change-page`; staging and delivery belong to the outbox units. The projection is shared by both readers, so they produce identical bytes for the same record.

Version 1 covers every concrete health-and-fitness record class in the pinned `androidx.health.connect:connect-client:1.1.0`, extracted from the library itself: 41 types, of which 21 are instantaneous, 14 interval, and 6 series. The approved draft listed 40; the executable pack's reflection test found `Vo2MaxRecord`, which the draft's class scan had missed because its name contains a digit. The Medical Records/PHR classes in the same library are excluded (ADR 0015).

| Shape | Record types |
|---|---|
| Instantaneous | `basal-body-temperature`, `basal-metabolic-rate`, `blood-glucose`, `blood-pressure`, `body-fat`, `body-temperature`, `body-water-mass`, `bone-mass`, `cervical-mucus`, `heart-rate-variability-rmssd`, `height`, `intermenstrual-bleeding`, `lean-body-mass`, `menstruation-flow`, `ovulation-test`, `oxygen-saturation`, `respiratory-rate`, `resting-heart-rate`, `sexual-activity`, `vo2-max`, `weight` |
| Interval | `active-calories-burned`, `distance`, `elevation-gained`, `exercise-session`, `floors-climbed`, `hydration`, `menstruation-period`, `mindfulness-session`, `nutrition`, `planned-exercise-session`, `sleep-session`, `steps`, `total-calories-burned`, `wheelchair-pushes` |
| Series | `cycling-pedaling-cadence`, `heart-rate`, `power`, `skin-temperature`, `speed`, `steps-cadence` |

Record type names are the SDK class names without `Record`, in kebab case, which matches the Wire Protocol `record_type` pattern.

## Interface

Kotlin, package `org.activitytracker.healthconnect.projection` in the project-owned Health Connect module, with no Android framework calls:

- `ProjectionContext(implementationVersion: String, providerVersion: String)`, whose construction requires both versions to be non-empty and otherwise throws `IllegalArgumentException`; an empty version is a caller error, not a projection refusal.
- `projectSourceRecord(record: Record, context: ProjectionContext): ProjectionResult`, where `ProjectionResult` is either `Projected(recordType: String, schemaVersion: Int, bytes: ByteArray)` or `Refused(failure: String, recordType: String?, path: String)`.
- `PROJECTION_SCHEMA_VERSIONS: Map<String, Int>`, every record type's current schema version, all `1` in this version.

## Requirements

Each requirement must be bound to named executable tests before freeze. Kotlin JVM tests build typed records from synthetic vector descriptions and compare exact bytes; a Python suite validates the same vectors against the shared schemas. No fixture contains real health data, Device identifiers, or credentials.

### R1 — Governed Scope contract

The contract names the scope, version, kind, purpose, inputs with their modes, outputs, every named failure with the outcome class `projection-refusal`, no observable effects, authority, dependencies, and acceptance oracle, and does not contradict the `project-source-record` scope-map row.

Proposed tests: `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract` (Python).

### R2 — Complete coverage of the pinned SDK

Every concrete health-and-fitness record class in the pinned SDK has exactly one closed schema and projects successfully for a valid record. Each schema covers every public property of the record, of its nested types, and of `Metadata`, `DataOrigin`, and `Device`, with the SDK's nullability, found by Kotlin reflection over the pinned SDK so that a missed property fails a test rather than being silently dropped; that is how `PlannedExerciseSessionRecord.hasExplicitTime` (ADR 0025) entered the model. No Medical Records/PHR class is projected, and any other `Record` subclass is `unsupported-record-type`.

Proposed tests: `every_pinned_record_class_has_one_schema`, `every_public_property_maps_to_a_schema_field`, `medical_records_classes_are_not_projected`, `unknown_record_class_is_unsupported`, `every_vector_builds_a_valid_sdk_record`.

### R3 — Common projection envelope

Each projection is one JSON object with `record_type`, `projection_schema_version`, `projection_implementation_version`, `sdk_version` (`androidx.health.connect:connect-client:1.1.0`), `provider_version`, `metadata`, and `record`. `metadata` carries `id`, `data_origin`, `last_modified_time`, `client_record_version`, `recording_method`, and, when present, `client_record_id` and `device` with its `type`, `manufacturer`, and `model`. `record` carries the type's own properties under snake_case names.

Proposed tests: `projection_envelope_carries_versions_and_metadata`, `metadata_device_and_client_id_are_optional`, `projection_context_requires_nonempty_versions`.

### R4 — Canonical bytes

Bytes are RFC 8785 canonical JSON in UTF-8 without a byte-order mark, so equal records produce equal bytes. An integer is a JSON number when its schema bounds it within ±(2^53 − 1), and otherwise a decimal string, which applies to `client_record_version`. A finite double uses the RFC 8785 number form; NaN or infinity is `non-finite-value`. Text must be valid Unicode; a string with an unpaired surrogate is `unrepresentable-text`.

Proposed tests: `bytes_match_rfc8785_canonical_form`, `large_integers_are_decimal_strings`, `non_finite_values_are_refused`, `unpaired_surrogates_are_refused`.

### R5 — Time and duration

Instants are RFC 3339 UTC with `Z` and keep the source precision in groups of three fractional digits, as `java.time.Instant.toString` prints them; a year outside 0000 to 9999 is `unrepresentable-instant`. Dates are proleptic Gregorian, and the schemas accept only real ones: a day past its month's end, or February 29 outside a leap year, is invalid. Zone offsets are separate `±hh:mm` fields, omitted when the source has none; a zero offset is `+00:00`, not `Z`; an offset with nonzero seconds is `unrepresentable-offset`. Durations are whole nanoseconds as decimal strings, computed without overflow, so the SDK's largest duration of `Long.MAX_VALUE` seconds plus 999,999,999 nanoseconds is `9223372036854775807999999999`.

Proposed tests: `instants_keep_source_precision_in_utc`, `zone_offsets_are_separate_and_optional`, `unrepresentable_instants_and_offsets_are_refused`, `durations_are_exact_nanoseconds`.

### R6 — Units without conversion loss

Every measurement is a JSON number in its unit class's storage unit, which Health Connect itself persists and the client constructs on read: for example grams for mass, read through that class's public accessor so records read from Health Connect project without conversion. The field name carries the unit, such as `weight_grams`. The storage units, read from the pinned SDK's `UnitConvertersKt`, are grams for mass, calories (not kilocalories) for energy, meters for length, liters for volume, meters per second for velocity, watts for power, millimeters of mercury for pressure, degrees Celsius for temperature and temperature delta, and millimoles per liter for blood glucose; a percentage is its raw value.

Proposed tests: `measurements_use_documented_storage_units`, `values_read_from_storage_units_project_exactly`.

### R7 — Constants and variants

Integer constants such as exercise type, sleep stage, recording method, Device type, meal type, and measurement location are emitted as the SDK integer. Each schema enumerates the constants the pinned SDK declares for that field, which for every field equal the SDK's platform mapping table plus its `UNKNOWN` value 0, and an undeclared value is `undeclared-constant`. Sealed variants, namely the exercise route result, completion goals, and performance targets, are objects with a `kind` discriminator; an undeclared subclass, including the SDK's own `UnknownGoal` and `UnknownTarget` placeholders for values it could not read, is `undeclared-variant`.

Proposed tests: `constants_are_sdk_integers_within_declared_sets`, `undeclared_constants_are_refused`, `variants_carry_a_kind_discriminator`, `undeclared_variants_are_refused`.

### R8 — Absence, order, and nesting

An absent nullable property is omitted, never `null`; an empty list is `[]`. Lists keep the SDK's order exactly: samples, stages, segments, laps, route locations, deltas, blocks, steps, and targets. Nested objects follow the same rules.

Proposed tests: `absent_properties_are_omitted`, `list_order_is_preserved_exactly`.

### R9 — Exercise routes

An exercise session projects its route result as `data` with its ordered locations, `no-data`, or `consent-required`; the projection never requests route consent and never invents locations.

Proposed tests: `exercise_route_results_project_by_kind`.

### R10 — Determinism and purity

The same record and context always produce the same bytes regardless of default locale, time zone, or process; projection performs no I/O and reads no clock, package manager, or system property. Tests observe determinism and context propagation; freedom from I/O and ambient reads is confirmed by the approver's implementation review because no test can observe its absence.

Proposed tests: `projection_is_independent_of_locale_and_time_zone`, `projection_reads_only_its_inputs`.

### R11 — Shared schemas and vectors

`projection/v1/tools/build_projection.py` generates, and with `--check` verifies, everything else under `projection/v1/`: `model.json`, the field-by-field model the Kotlin tests read; one closed JSON Schema Draft 2020-12 per type in `schemas/`, with `additionalProperties: false` throughout; `vectors/positive.json`, 98 synthetic projections with exact bytes and SHA-256, a minimal and a maximal record for every type plus tagged vectors for numbers (including `5e-324`, `1e+23`, `1e+21`, negative zero, and both sides of `1e-6`), text, precision, offsets, leap days, durations (including one beyond `Long` nanoseconds), non-chronological list order, variants, routes, and context; `vectors/refusals.json`, 13 SDK-level refusal cases covering every named failure and the refusal traversal order; `vectors/schema-negatives.json`, 18 projections the schemas must reject, including impossible calendar dates; and `manifest.json`. The Python suite validates every positive vector against its schema, rejects every schema negative, and confirms canonical form and the manifest; the Kotlin suite builds each vector's SDK record through the SDK's own constructors and compares the projection's bytes exactly.

Proposed tests: `every_vector_projects_to_its_expected_bytes` (Kotlin); `test_every_record_type_has_one_closed_schema`, `test_schemas_are_closed`, `test_positive_vectors_conform_to_their_schemas`, `test_schema_negatives_are_rejected`, `test_vectors_are_canonical`, `test_vector_manifest_matches_files`, `test_refusal_vectors_cover_every_named_failure` (Python).

### R12 — Health-data-free refusals

A refusal carries only the failure name, the record type when known, and the JSON path of the offending field, never field values, so the page reader can persist a health-data-free refusal identity. When several fields would refuse, the refusal names the first in a fixed traversal: `metadata` before `record`; within an object, fields in the order `projection/v1/model.json` declares them, not serialization order; list elements by index; depth first.

Proposed tests: `refusals_carry_no_field_values`.

## Decisions for the Operator

1. **All 41 types in version 1.** ADR 0015 already requires every stable type. Splitting into versions would force a supersede, which blocks every dependent Sender unit from publishing until the successor publishes.
2. **Storage units, not value-and-unit pairs.** Each measurement is emitted in the unit Health Connect persists, through its public accessor, so data read from Health Connect is exact and each field has one documented unit (ADR 0025). The alternative, emitting the producer's original unit, would need private SDK state.
3. **Undeclared constants and variants refuse.** A value the pinned SDK does not declare, such as a new exercise type written by a newer provider, blocks that type until a Sender release adds it, as ADR 0010's closed-schema rule requires, rather than archiving an integer nobody can interpret.
4. **Absent means omitted.** Optional properties are omitted rather than `null`, matching Wire Protocol Version 1.
5. **Exercise routes are archived.** Route locations are sensitive, but they are public record fields and ADR 0010 requires lossless projection. A `consent-required` result is archived without coordinates.
6. **Provider version is a caller input.** The Health Connect provider version is read once per Polling Run by the caller and passed in, keeping projection pure.

## Ground rules

- Kotlin JVM tests construct SDK record objects through the SDK's constructors, by Kotlin reflection from the vectors and model, with no Android device, emulator, or Context. Refusal vectors substitute one SDK value that has no JSON form, such as NaN or an SDK `Unknown` variant, at a JSON pointer.
- Synthetic values only; route coordinates in fixtures are synthetic points.
- Schemas and vectors are generated by a checked-in generator from the pinned SDK inventory and are reviewed as bound files.

## Freeze prerequisites

The Operator approves this draft. The executable pack then adds the per-type schemas, the vectors and their generator, the Kotlin and Python acceptance tests, the runner, and Interface stubs; obtains the implementer's read-only implementability review; shows genuine RED; and presents the binding for the approver's freeze decision.
