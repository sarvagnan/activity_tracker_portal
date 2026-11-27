# Draft acceptance pack — `summary-contract@1`

**Status:** Draft approved by the Operator on 2026-11-27, including the 17 decisions below as recommended; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `summary-contract` Governed Scope contract, version 1, in `governance/scopes/summary-contract.1.json`. It is a shared contract. It depends on the published `interpret-source-projection@1`, `set-source-selection-policy@1`, and `set-fallback-timezone-policy@1`.

## What this unit does

Phase 5 turns Canonical Revisions into Health Summaries. Four derive units calculate them, one per measure. `refresh-summary-state` runs them and `operational-store@6` stores and publishes what they return (ADR 0043). This unit is the common shape they all share, as `interpret-source-projection@1` is for processors.

A Health Summary "carries its primary state, evidence conditions, applicable policies, Processor Generation, Data Watermark, and contributing evidence" (CONTEXT.md). This unit defines:
- the **header**: which summary it is, which versions and policies produced it, its primary state, its conditions, its coverage, and its Data Watermark. It never holds a measured value;
- the **evidence manifest**: every Source Record the derivation attributed to the day, with its standing;
- the **primary-state rule**: one total rule, so every derive unit picks the same state from the same facts;
- the **vocabularies**: closed lists of primary states, standings, evidence conditions, and publication conditions, and how later versions grow them;
- **validation and encoding**: RFC 8785 bytes, a digest, and a decoder for stored bytes.

A derive unit supplies the facts: what it attributed to the day, each item's standing, the conditions it found, and its body of values. This unit checks them, computes the state, coverage and digests, and builds the bytes.

## What it does not do

- It derives nothing. Which records belong to a day, which origins overlap, and what is unresolved are the derive units' (ADR 0041).
- It does not define a body. The values (a Steps total, Heart Rate sums, Sleep durations, a Weight point) belong to each derive unit. This unit checks only that the body is canonical JSON and present exactly when a value exists.
- It does not group duplicates. `duplicate-observation-contract` does. This unit only reserves the condition names it needs (Decision 8).
- It does not compose ranges. `range-summary-contract` does, from daily summaries (Decision 2).
- It stores, selects, and publishes nothing. It does not read the store, compute delivery positions, or decide when a publication condition applies. `operational-store@6` and `refresh-summary-state` do.
- It does not render anything.

## Interface

The module `activity_tracker_portal.summary_contract` provides frozen dataclasses, five functions, one exception, and constants. It reads no clock, store, file, or network.

**Constants.**
- `CONTRACT_VERSION = "summary-contract@1"`.
- `MEASURES`: re-exported from `set_source_selection_policy` (Decision 16).
- `PRIMARY_STATES = ("measured", "partial", "missing", "failed", "quarantined", "policy-pending", "unresolved")`.
- `NO_VALUE_PRECEDENCE = ("failed", "quarantined", "policy-pending", "unresolved")`: the order the rule uses when no value exists (Decision 4).
- `STANDINGS = ("contributing", "unresolved", "in-conflict", "policy-pending", "failed", "quarantined")`.
- `REPORTED_CONDITIONS = ("classification-conflict", "duplicate-grouped", "equal-time-conflict", "selection-unresolved", "unallocatable-interval")`: conditions a derive unit reports.
- `COMPUTED_CONDITIONS = ("inferred-attribution", "source-conflict")`: conditions this unit computes from the evidence.
- `EVIDENCE_CONDITIONS`: the union of the two, sorted.
- `PUBLICATION_CONDITIONS = ("conflicted", "newer-evidence-pending", "reconciliation-failed", "reconciliation-pending", "refresh-failed", "superseded")` (Decision 7).
- `FAILURES`: the four failure names below.

**Values.**
- `EvidenceEntry(record_id, selection_id, outcome_id, standing, processor_version, policy_identity)`. The identities are positive `int`s; `outcome_id`, `processor_version`, and `policy_identity` are `None` where R5 says so.
- `Coverage(contributing, unresolved, in_conflict, policy_pending, failed, quarantined)`: non-negative `int` counts.
- `DeliveryPosition(installation_id, sequence, receipt_time, processed_at)`. `installation_id` is the store's text identity, `sequence` a positive `int`, and both times are timezone-aware.
- `DataWatermark(positions, evidence_set_sha256)`.
- `SummaryHeader`: one field per header entry in R8.
- `HealthSummary(header, header_bytes, evidence_bytes, body_bytes, summary_sha256)`. Its `repr` omits the three byte fields.

**Operations.**
- `coverage_of(evidence) -> Coverage`.
- `primary_state(coverage, has_value) -> str`: the total rule (R4).
- `build_summary(*, measure, record_type, local_date, derivation_version, processor_version, source_selection_policy, evidence, reported_conditions, positions, body) -> HealthSummary`. `source_selection_policy` is a positive `int` policy identity or `None`. `body` is `bytes` or `None`.
- `decode_summary(header_bytes, evidence_bytes, body_bytes) -> HealthSummary`.
- `check_publication_conditions(names) -> tuple[str, ...]`: validates a set of publication conditions and returns it sorted.
- `SummaryContractFailure(failure)`: its `args` are exactly `(failure,)`.

## Requirements

### R1 — Governed Scope contract

The contract has the shared shape, kind `shared`, and version 1. It depends on `interpret-source-projection`, `set-source-selection-policy`, and `set-fallback-timezone-policy`, each at version 1, and nothing else. Its named failures are `summary-input-invalid`, `summary-state-inconsistent`, and `summary-bytes-invalid`, which are invariant refusals, and `summary-version-unsupported`, a refusal. It has no observable effects, and its effects declaration is empty. It does not contradict the scope-map row.

Proposed tests: `test_contract_shape_and_dependencies`, `test_effects_declaration_is_empty`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — Pure and deterministic

- Every operation reads only its arguments. It reads no clock, store, file, network, environment, or host timezone database, writes nothing, and logs nothing.
- The same arguments give equal results with identical bytes, whatever the process, `TZ`, locale, or hash seed.
- All dataclasses are frozen.

Proposed tests: `test_same_input_gives_identical_bytes`, `test_result_ignores_host_timezone_locale_and_hash_seed`, `test_operations_read_no_clock_or_io`, `test_values_are_frozen`.

### R3 — Vocabularies

- The constants hold exactly the names in the Interface, in that order. Every name is lowercase kebab-case.
- `MEASURES` is the very object `set_source_selection_policy` publishes.
- No name is in two vocabularies, except `policy-pending`, `failed`, `quarantined`, and `unresolved`, which are both a primary state and the standing that leads to it. No evidence condition is a publication condition.

Proposed tests: `test_vocabularies_are_exact`, `test_measures_are_the_published_vocabulary`, `test_vocabularies_do_not_collide`.

### R4 — One primary state, by one total rule

`primary_state(coverage, has_value)` returns exactly one state:
1. **A value exists.** There must be at least one contributing record, or the result is `summary-state-inconsistent`. The state is `measured` when every other count is 0, and otherwise `partial`.
2. **No value exists.**
   - With no attributed records at all, the state is `missing`.
   - Otherwise, the first of `failed`, `quarantined`, `policy-pending` whose count is not 0. Then `unresolved` when `unresolved` or `in-conflict` is not 0 (Decisions 4 and 5).
   - Contributing records with no value and nothing else is `summary-state-inconsistent`. A derivation that cannot produce a value must say why, as a non-contributing standing.

A value means `measured` or `partial`. `build_summary` refuses a body for any other state, and requires one for those two (Decision 1). A zero is a value: `measured` with a body whose total is 0 is not `missing` (ADR 0030).

Proposed tests: `test_value_with_only_contributors_is_measured`, `test_value_with_any_non_contributor_is_partial`, `test_nothing_attributed_is_missing`, `test_no_value_precedence_is_total`, `test_in_conflict_without_value_is_unresolved`, `test_value_without_contributor_is_inconsistent`, `test_contributors_without_value_or_reason_is_inconsistent`, `test_rule_is_exhaustive_over_small_counts`.

### R5 — Evidence entries and standings

Each entry is one Source Record the derivation attributed to the day. Its standing says how it counts:

| Standing | Meaning | `outcome_id` | `processor_version` | `policy_identity` |
|---|---|---|---|---|
| `contributing` | Its Canonical Revision contributes to the value, alone or in a Duplicate Group. | required | required | required |
| `unresolved` | It has a revision, but the derivation cannot use it without a rule it lacks: overlapping origins with no applicable policy, an equal-time different value, a differently classified overlap, or an unallocatable interval. | required | required | required |
| `in-conflict` | Its current selection is a Source Conflict, so neither projection contributes (ADR 0041). | `None` | `None` | `None` |
| `policy-pending` | Its outcome is Policy-Pending. | required | required | required |
| `failed` | Its Processing Job failed; there is no outcome. | `None` | required | `None` |
| `quarantined` | Its outcome is Quarantine. | required | required | required |

- Identities are positive Python `int`s, never `bool`.
- Each `record_id` appears at most once.
- Every `processor_version` that is present equals the summary's `processor_version`, so one summary never mixes processor versions (ADR 0042).
- A `policy_identity` is a non-empty string, `"none"` or as `interpret-source-projection@1` wrote it. This unit does not parse it.
- Records excluded by the Source Selection Policy, or later by an Amendment, are not attributed and have no entry (Decision 12).

Proposed tests: `test_each_standing_requires_its_fields`, `test_duplicate_record_is_refused`, `test_mixed_processor_versions_are_refused`, `test_identities_must_be_positive_ints`, `test_policy_identity_is_opaque`.

### R6 — Coverage

`coverage_of(evidence)` counts the entries by standing. Coverage is relative to the retained, processed evidence at the Data Watermark. It says nothing about the real world, wear time, or evidence the source may still hold. An open day is not partial merely because more evidence may arrive later (ADR 0030; Decision 9).

Proposed tests: `test_coverage_counts_entries_by_standing`, `test_coverage_of_nothing_is_all_zero`.

### R7 — Evidence conditions

- The derive unit reports conditions from `REPORTED_CONDITIONS`. Anything else, including a computed condition, is `summary-input-invalid`.
- Each reported condition must have evidence behind it, or the result is `summary-state-inconsistent`:
  - `selection-unresolved`, `equal-time-conflict`, `classification-conflict`, and `unallocatable-interval` need at least one `unresolved` entry;
  - `duplicate-grouped` needs at least two `contributing` entries.
- This unit adds the computed conditions:
  - `source-conflict` exactly when an entry is `in-conflict`;
  - `inferred-attribution` exactly when a `contributing` entry's policy identity is not `"none"` (ADR 0025).
- The header lists the conditions sorted, each once. Conditions never change the primary state; the standings do.

Proposed tests: `test_unknown_or_computed_condition_is_refused`, `test_reported_condition_needs_evidence`, `test_source_conflict_is_computed`, `test_inferred_attribution_is_computed`, `test_conditions_are_sorted_and_unique`.

### R8 — The header

The header is RFC 8785 canonical JSON with exactly these keys:
- `contract_version`: `CONTRACT_VERSION`.
- `derivation_version`: the derive unit's `<kebab-case scope>@<positive integer>`, such as `derive-daily-steps@1`.
- `measure` and `record_type`: a pair listed in `MEASURES`.
- `local_date`: `YYYY-MM-DD` (Decision 2).
- `processor_version`: the record type's processor version, in the same form, such as `interpret-steps@1` (Decision 10).
- `source_selection_policy`: the applied policy's identity as a decimal string, or `null` (Decision 11).
- `fallback_timezone_identities`: the distinct policy identities of all entries other than `"none"`, sorted by code point (Decision 11).
- `primary_state`, from R4.
- `evidence_conditions`, from R7.
- `coverage`: an object whose keys are the six standings and whose values are the counts.
- `watermark`: from R9.
- `body_sha256`: the lowercase hex SHA-256 of the body bytes, or `null`.

Store identities and Sender sequences are decimal strings, as in `interpret-source-projection@1` R10, so any size stays exact. Counts are JSON numbers. The header has no floating-point number. `summary_sha256` is the SHA-256 of the header bytes.

Proposed tests: `test_header_keys_are_exact`, `test_header_bytes_are_rfc8785`, `test_identities_are_decimal_text`, `test_version_forms_are_checked`, `test_measure_pair_must_be_listed`, `test_summary_digest_covers_header`.

### R9 — Data Watermark

- `positions` has one `DeliveryPosition` per Sender Installation whose accepted messages the derivation considered, at least one, sorted by `installation_id`, each installation once.
- A position states that every message of that installation carrying the summary's record type, archived with a sequence at or below `sequence`, was indexed and reached a terminal processing outcome. `receipt_time` is that message's Portal receipt time, and `processed_at` is when the last of that processing completed. The caller computes both from the store; this unit checks their form.
- The ingest position and the "highest incorporated Sender sequence" of ADR 0023 are the same value: the Portal archives each message under its installation and Sender sequence, and has no other ingest order (Decision 13).
- Times are timezone-aware, and their UTC instants lie within years 1 to 9999; otherwise the input is `summary-input-invalid`. They are written as UTC RFC 3339 with `Z` and microseconds. `processed_at` is not before `receipt_time`, compared as instants, so a repeated local hour is ordered correctly.
- `evidence_set_sha256` is the SHA-256 of the evidence manifest bytes (R10). The caller does not supply it.
- The watermark is part of the header, so it is part of the summary's identity (Decision 14).
- It never claims that the source has no later, missing, or unavailable evidence, that a sequence gap was filled, or when anything was measured.

Proposed tests: `test_watermark_positions_are_sorted_and_unique`, `test_watermark_needs_a_position`, `test_watermark_times_are_utc_and_ordered`, `test_evidence_digest_is_computed_not_supplied`, `test_watermark_changes_the_summary_digest`, and, added after the implementer's review, `test_watermark_orders_repeated_hour_by_instant` and `test_watermark_times_stay_in_utc_range`.

### R10 — Evidence manifest

- The manifest is RFC 8785 canonical JSON: `{"contract_version": ..., "evidence": [...]}`. Entries are sorted by `record_id` as a number. Each entry has the six R5 fields, with `null` for `None` and identities as decimal strings.
- The header's `evidence_set_sha256` is its digest, and the header's coverage agrees with it.
- An empty manifest is valid: it is the `missing` summary's.

Proposed tests: `test_manifest_is_canonical_and_sorted_numerically`, `test_manifest_digest_is_in_the_header`, `test_empty_manifest_is_missing`.

### R11 — Body

- The body is the derive unit's. This unit requires only that it is RFC 8785 canonical bytes of one JSON object, in the number domain of `interpret-source-projection@1` R9 revision bodies. A number token is valid exactly when it denotes a finite binary64 value and is that value's RFC 8785 serialization. So `10000000000000000`, the serialization of `1e16`, is valid, and `9007199254740993` is not, because it is not canonical. Whether a number means an integer or a float is for the derive units to check. The approved draft said "with integers within ±(2^53 − 1)"; the Operator replaced that cap with this rule on 2026-11-27, after Gate B (Astra's finding 1).
- A body is present exactly when the state is `measured` or `partial` (R4).

Proposed tests: `test_body_must_be_a_canonical_object`, `test_body_numbers_are_canonical_binary64`, `test_body_present_only_with_a_value`, and, added after the implementer's review, `test_body_number_vectors_follow_rfc8785` and `test_noncanonical_numbers_are_refused_in_build_and_decode`.

### R12 — Decoding stored bytes

- `decode_summary` parses the three byte strings strictly, checks every rule above, and returns a `HealthSummary` equal to the one `build_summary` builds from the same facts.
- Bytes that are not canonical, a digest that does not match, coverage that disagrees with the manifest, or a primary state that disagrees with R4 are `summary-bytes-invalid`.
- A `contract_version` other than `summary-contract@1` is `summary-version-unsupported`. A later version of this unit must decode every earlier version (Decision 8).

Proposed tests: `test_decode_round_trips`, `test_tampered_bytes_are_refused`, `test_inconsistent_state_bytes_are_refused`, `test_unknown_contract_version_is_unsupported`.

### R13 — Publication conditions

`check_publication_conditions(names)` accepts any set of names from `PUBLICATION_CONDITIONS` and returns them sorted. Anything else is `summary-input-invalid`. Publication conditions are never written into a summary's bytes (Decision 7).

Proposed tests: `test_publication_conditions_are_checked_and_sorted`, `test_publication_conditions_are_not_in_summary_bytes`.

### R14 — Health-data-free failures and safe header

- Every failure is a `SummaryContractFailure` whose `args` are exactly `(failure,)`, with no `__cause__`, and whose traceback carries no input value.
- The header and manifest carry no measured value, origin, Source Identity, record time, or projection, revision, or body bytes. They do carry the local date and Portal identities, so they are not health-data-free in the Cutover Record sense (Decision 15).
- `HealthSummary`'s `repr` omits all bytes.

Proposed tests: `test_failures_carry_only_their_name`, `test_header_and_manifest_carry_no_values_or_origins`, `test_summary_repr_omits_bytes`.

## Decisions for the Operator

1. **This unit owns the header and evidence manifest; each derive unit owns only its body.** The header and manifest are built and checked once, so no derive unit can misstate its state, versions, or watermark. Only `measured` and `partial` carry a body of values. The other states carry no number (ADR 0030, 0043).
   - Alternative: each derive unit builds the whole summary, and this unit only checks it. That repeats the header logic in four places.
   - Alternative: values in the header. The header could then not be shown or indexed without the values.
2. **One summary per measure, record type, and local date, with no timezone.** This matches the store key `operational-store@6` expects (measure, record type, local date) and `operational-store@5` Decision 6, which made policy dates local dates. Materialized summaries are daily (ADR 0023). A local date has no single zone: each record's date comes from its own experienced offset, or from a Fallback Timezone Policy (ADR 0025).
   - Alternative: a first and last date, so ranges reuse the type. `range-summary-contract` composes ranges from daily summaries and defines its own range result, so a span here would be unused, and the store key would not fit.
   - Alternative: add a zone or a UTC window. That would invent a 24-hour window that per-record attribution does not have.
3. **Seven primary states: measured, partial, missing, failed, quarantined, policy-pending, unresolved.** The scope-map row says `pending`. It means evidence awaiting a policy that does not exist (ADR 0030, "missing required policy"), and the presentation row calls it `policy-pending`.
   - **Recommended:** name it `policy-pending`. `pending` alone is easily confused with `newer-evidence-pending`, with a requested policy awaiting activation, and with the "initial pending" of ADR 0043, which means nothing is published yet. After approval, the drafter updates the scope-map row to say `policy-pending`.
   - Alternative: keep `pending` as the row says, defined narrowly.
4. **Which state wins when there is no value: failed, then quarantined, then policy-pending, then unresolved.** With a value, any non-contributing record makes the result `partial`, never `measured`. Without one, the state names the most serious reason a number is missing. The counts stay in the coverage, so nothing is hidden.
   - **Recommended:** failed first, because a Portal defect could hide anything. Quarantined next, because the source data cannot be used. Then policy-pending and unresolved, which the Operator can fix by setting a timezone or choosing a source.
   - Alternative: Operator-fixable first (unresolved, policy-pending, failed, quarantined). This shows what the Operator can do, but lets a defect hide behind a choice.
   - Alternative: each derive unit picks. Ranges would then count the same facts differently.
5. **A Source Conflict counts as a record that does not contribute.** An `in-conflict` record makes a valued day `partial`, or a no-value day `unresolved`, and also sets the `source-conflict` condition. "Measured" means complete relative to retained evidence (ADR 0030), and an `in-conflict` record is retained evidence that cannot be used.
   - Alternative: `source-conflict` as a condition only. A day could then read `measured` while one of its records is in conflict.
   - Note: ADR 0041 says a conflict "preserves the last trusted result". Keeping the prior published summary current with the `conflicted` publication condition is `refresh-summary-state`'s decision. This unit only describes the new derivation truthfully.
6. **Evidence that cannot be placed on a date stays out of daily summaries.** A Policy-Pending, quarantined, failed, or `in-conflict` record has no Canonical Revision, so no local date (`interpret-source-projection@1` builds no header for them). A derive unit may place one where its pack defines how, for example by the record's last revision. Records it cannot place are not in any daily summary. `operational-store@6` and the presentation units count them per record type with the same standing names, so the Overview shows them (scope map, "Operator surfaces").
   - **Recommended:** as above.
   - Alternative: every summary of that type gets an "unplaced evidence" condition. That is more cautious, but one new quarantine would change and republish every daily summary of the type.
   - Alternative: this unit fixes a placement rule now. That decides type-specific behaviour before the derive units are drafted.
   - Consequence: a day can be `measured` while an unplaceable quarantined record of the same type exists. The type-level count is what shows it.
7. **Two kinds of condition: evidence conditions are stored, publication conditions are not.**
   - **Evidence conditions** describe one derivation and are stored in its bytes. Five come from the derive units: `selection-unresolved` (ADR 0026), `equal-time-conflict` and `duplicate-grouped` (ADR 0041, `duplicate-observation-contract`), `classification-conflict` (Sleep, ADR 0041), and `unallocatable-interval` (Steps, ADR 0041). This unit computes two: `source-conflict` (CONTEXT.md) and `inferred-attribution` (ADR 0025).
   - **Publication conditions** describe the current published state and are attached by later units when they read it. They are never stored, so a failed refresh does not need a new summary. The six names come from ADR 0030: `conflicted`, `superseded`, `newer-evidence-pending`, `reconciliation-pending`, `reconciliation-failed`, and `refresh-failed`. `newer-evidence-pending` and `refresh-failed` are also defined by ADR 0043. `superseded` is ADR 0026's prior result under a requested policy. `reconciliation-pending` and `reconciliation-failed` come from CONTEXT.md's Reconciliation Snapshot and Reconciliation Failure. Only the vocabulary lives here. When each applies is for `operational-store@6`, `refresh-summary-state`, and the completion units to decide.
   - **Recommended:** both vocabularies here, so `range-summary-contract` can count every condition while depending only on this unit.
   - Alternative: one stored vocabulary. A refresh failure would then need a new summary revision.
   - Alternative: publication conditions defined by `operational-store@6`. `range-summary-contract` would then need a store dependency.
   - Flag: CONTEXT.md does not define `newer-evidence-pending` or `refresh-failed`. Adding them to the glossary is a separate change.
8. **Vocabularies are closed per contract version and only grow.** Every summary names its contract version. A later version may add names but never removes or changes one, and it must decode every earlier version (R12). All names the ADRs already imply are included now, even those that only later units produce, so `duplicate-observation-contract` and the derive units need no new version of this unit.
   - Alternative: open, namespaced names that any unit may add. Ranges could not count a closed set, and a typo would become a new condition.
   - Alternative: add each name when its unit arrives. Each addition would then need a new version of this unit first.
9. **Coverage means counts of the records attributed to the day, by standing.** It is relative to what the Portal has retained and processed at the watermark. It is not a fraction of the day, wear time, or a claim that the source has nothing more.
   - Alternative: a coverage class such as complete, partial, or none. That repeats the primary state.
   - Alternative: time-based coverage. That claims knowledge of when the device was worn, which the evidence does not show.
10. **The header names the processor version, not the Processor Generation.** A Processor Generation maps every record type to a processor version. If the generation's identity were in every summary's bytes, each new generation would change every summary of every type, even types whose processor did not change. ADR 0022 says unchanged types are not reprocessed. The record type's processor version is what the summary depends on. It lets the store check that a view under a generation selects only matching summaries (ADR 0042), and keeps the selectors distinct (ADR 0044). The header also names the derivation version, because a derive unit upgrade changes results in the same way.
    - **Recommended:** processor and derivation versions in the bytes. The generation identity and the "policy horizon", the highest policy identity a build considered, are the target that `operational-store@6` stores beside the bytes. The store's working assumption puts a generation identity in the summary's target. That fits, as long as it stays outside the bytes.
    - Alternative: the generation identity in the bytes, as the store's research suggested it may be. Then every generation activation must rebuild and republish every summary.
    - Alternative: both, with the generation identity nullable until `@6`.
    - Flag: CONTEXT.md says a Health Summary "carries" its Processor Generation. Under the recommendation, it carries it through its stored target, not in its bytes.
11. **Policy identities in the header.** The header names the applied Source Selection Policy identity, or none. It also lists the distinct Fallback Timezone policy identities of the evidence, copied as opaque text from each outcome. Source Selection dates are local dates, so at most one policy applies to a day (`operational-store@5` Decision 6).
    - **Recommended:** as above. `interpret-source-projection@1` left the exact identity form to its module documentation, so this unit does not parse it.
    - Alternative: merge the fallback identities into one, by parsing their policy lists. That depends on a form no pack pins.
    - Alternative: the policy horizon in the bytes. Then recording any unrelated policy would change every summary.
12. **Evidence is listed in a separate manifest, which the header digests.** The manifest lists every attributed record, contributing or not, with its standing. The header carries its digest, which is also the watermark's evidence-set digest (CONTEXT.md, Data Watermark). The store's working assumption links each summary to its contributing outcome identities. That fits: the store can index the `contributing` entries and keep the full manifest as bytes. Non-contributing entries, and `failed` and `in-conflict` records, which have no outcome identity, stay inspectable from the manifest. Records excluded by policy or Amendment are not listed. The Sources and Records pages show them (ADR 0026).
    - Alternative: the list inside the header. A day with hundreds of records would have a very large header.
    - Alternative: only the digest, with the list kept only in the store's tables. The digest could then not be checked without the store.
13. **The Data Watermark is one position per Sender Installation.** After a Replacement Baseline, evidence from a retired installation stays eligible (ADR 0050, 0051), so one position is not enough. Each position gives a sequence through which every archived message was indexed and terminally processed, that message's receipt time, and the processing completion time (ADR 0023, 0030). The caller computes them; this unit checks their form.
    - **Settled:** the "exact ingest position" and the "highest incorporated Sender sequence" of ADR 0023 are the same value, because the Portal archives messages by installation and Sender sequence and has no other ingest order. The store's proposed watermark entry (installation, sequence, receipt time, processed time) matches. The store repeats the evidence-set digest on each entry, while the header holds it once.
    - Alternative: the highest evidence identity, which is the order the Portal indexed evidence. That is not a delivery position, and a reader could not relate it to the Sender.
    - Alternative: only the latest receipt time. That loses the exact position ADR 0030 requires in detail views.
14. **The watermark is part of the summary's identity.** A summary states exactly what it incorporated. So rederiving after later deliveries gives new bytes, even when the values did not change.
    - **Recommended:** as above. Whether to republish a summary whose only change is its watermark is for `refresh-summary-state` to decide.
    - Alternative: keep the watermark outside the digest, so identical content keeps its identity. An immutable revision would then not bind what it incorporated.
15. **What the header may contain.** It has no measured value, origin, Source Identity, record time, or bytes. It does have the local date and Portal identities, so it must not be copied into health-data-free surfaces such as Cutover Records, Diagnostic Bundles, or logs. Failures carry only their name.
    - Alternative: leave the date out of the header too. The summary would then not say which day it is without its store key.
16. **The measure names come from `set-source-selection-policy@1`.** This unit re-exports its `MEASURES` (`daily-steps`, `heart-rate-summary`, `sleep-summary`, `weight-series`), as that unit's Decision 2 intended.
    - **Recommended:** import the same object, so there is one list.
    - Consequence: importing this unit also imports `operational_store`'s module, without opening a store. The purity test therefore forbids I/O and clocks, but not that import.
    - Alternative: copy the four names here and test that they match. That avoids the import but keeps two lists.
17. **A decoder for stored bytes.** `decode_summary` lets the store, presentation, and export check stored summaries against this contract, and lets later versions read earlier ones.
    - Alternative: no decoder; readers trust the store's columns. Stored bytes could then drift from the contract unnoticed.

## Ground rules

- **Data.** Python only, standard library plus the published modules this unit imports. Tests build evidence entries and bodies by hand. All identities, dates, and times are synthetic. No store is opened.
- **Encoding.** RFC 8785, as in `interpret-source-projection@1`. The module may reuse the package's existing encoder. The suite checks the bytes against hand-written expected bytes.
- **Markers.** Synthetic markers in body values show that no failure or `repr` carries them.
- **Unbound inputs, reviewed rather than frozen.** The binding is the contract, this spec, the suite, the runner, and the effects declaration. These stay outside it:
  - `docs/governance/scope-map.md`, read by R1's scope-map test;
  - the published code, contracts, and packs of `interpret-source-projection@1`, `set-source-selection-policy@1`, and `set-fallback-timezone-policy@1`, which their own bindings freeze;
  - the execution environment: `uv`, `pyproject.toml`, `uv.lock`, and the managed Python;
  - `tests/governance/test_summary_contract_contract.py`, which the governance suite runs and this runner does not.

## Freeze prerequisites

After Gate A, the executable pack adds:
- the suite `tests/acceptance/test_summary_contract_1.py`: 60 tests, one per proposed name in R1–R14, grouped by requirement;
- the runner `tests/acceptance/run_summary_contract_1.sh`, which runs only this suite;
- no fixture files;
- the Interface stub `src/activity_tracker_portal/summary_contract.py`:
  - the real constants, because they are the vocabulary the Operator approves, with `MEASURES` imported from `set_source_selection_policy`;
  - the real frozen dataclasses `EvidenceEntry`, `Coverage`, `DeliveryPosition`, `DataWatermark`, `SummaryHeader`, and `HealthSummary` (its three byte fields have `repr=False`), and the real `SummaryContractFailure`;
  - the five operations, each raising `NotImplementedError("summary-contract@1")`.

**RED, against the stub.**
- 7 tests pass, because they read only the contract, the effects declaration, the scope map, and the constants, or build the dataclasses directly: the three R1 tests, the three R3 tests, and `test_values_are_frozen`.
- The other 53 fail, with 310 failing entries, counting subtests. Every failure is the stub's `NotImplementedError`.
- `tests/unit`, `tests/governance`, and the bound runners of the three dependencies still pass.

**How the suite checks bytes.** Every successful build is compared with a model written in the suite: its own RFC 8785 encoder, the R4 rule, the conditions, the fallback identities, and `hashlib` digests. Every build is then decoded and must equal the original. One test also compares hand-written header and manifest bytes.

**Refinements the tests pin beyond the approved text.** These are the drafter's choices, for the approver to confirm or escalate. None changes approved semantics.
- **Operator clarification at operational-store@6 Gate A, 2026-11-27:** a watermark position covers only the messages carrying the summary's record type (R9, second bullet). No test depends on this wording.
- JSON layout:
  - the watermark is `{"evidence_set_sha256", "positions"}`, and holds the evidence digest once;
  - each position is `{"installation_id", "sequence", "receipt_time", "processed_at"}`;
  - `coverage` keys are the standing names, such as `in-conflict`;
  - `SummaryHeader`'s field names are the 13 header keys.
- Times are always `YYYY-MM-DDTHH:MM:SS.ffffffZ`, with a four-digit year and six fraction digits, even when the microseconds are 0.
- Decimal strings are ASCII digits with no sign and no leading zero. Store identities and sequences may be any positive size.
- Nulls are written, never left out: `source_selection_policy` and `body_sha256` in the header, and the three nullable entry fields. No conditions or identities is `[]`. The decoder refuses an absent key, and a `null` where a standing carries the field.
- Which failure:
  - every R5 entry check and every R9 position check is `summary-input-invalid`;
  - a body with no contributing record is `summary-state-inconsistent`;
  - the decoder raises only `summary-bytes-invalid` or `summary-version-unsupported`.
- The decoder reads `contract_version` first. A header of another version is unsupported even when its keys differ.
- Not pinned:
  - a manifest whose version differs from the header's may be either decoder failure;
  - a non-`bytes` argument to the decoder may be `summary-bytes-invalid` or `summary-input-invalid`;
  - which failure wins when one input breaks two rules.
- Argument domains:
  - `local_date` is a `date`, not a `datetime`;
  - `primary_state` takes a `Coverage` of non-negative `int` counts and a `bool`;
  - `coverage_of` refuses an unknown standing.
- `reported_conditions` and `check_publication_conditions` take any collection, including a list. Duplicate reported conditions are merged. A bare string is refused.
- Order:
  - fallback identities sort by code point, not UTF-16;
  - installation ids are non-empty strings and sort in Python string order.
- Version form: `[a-z0-9]+(-[a-z0-9]+)*@[1-9][0-9]*`, ASCII only, as `interpret-source-projection@1` checks processor versions.
- The header's watermark positions hold their times converted to UTC, and decoded times are UTC. Without this, a built summary whose times fall in a repeated hour could never equal its decoded form, because Python never treats such a time as equal to one in another zone (R12).
- The purity test runs in a subprocess:
  - It first imports the standard modules an implementation commonly needs, then the unit, which may import `operational_store` through `set_source_selection_policy` (Decision 16).
  - At any time, it flags a write open, an open under the host timezone database, and socket, subprocess, SQLite, and file-system change events.
  - It runs every operation twice. The first run is a warm-up, so a lazy standard-library import cannot fail a correct implementation. During it, only standard-library module files may be opened, and only standard-library paths and their parent directories may be inspected by `stat`, `lstat`, `fstat`, or `access`. Anything else is flagged.
  - During the second run, it flags every `open` audit event, by path or by descriptor, with no exemption, which includes an import that opens its source. It also flags every directory listing and import event, and every `os.stat`, `os.lstat`, `os.fstat`, and `os.access` call. CPython raises no audit event for those four, so the test wraps them directly.
  - Through both runs, it flags any environment read, any log record, and anything written to stderr. So a value read from a file or the environment once and cached is caught in the warm-up.
  - It replaces the Python-level clock functions with guards.
  - Reads that raise no audit event, such as a C-level clock read, stay a residual.
- Note for the implementer: the package's existing encoder `_interpret_projection_json.canonical_bytes` has no `null` case, so the module needs a null-aware encoder.

**Passability.** A throwaway reference implementation, kept outside the repository, passes all 60 tests. Of 86 incorrect implementations, 84 fail at least one test, including each of the six that survived the implementer's review. They cover every case listed below and more. The two survivors are equivalent to the reference:
- dropping the bare-string check on reported conditions, since each character is refused anyway;
- a lenient decimal parser in a decoder that rebuilds and compares bytes, since the comparison refuses it anyway.

**Gate B resolutions (round 1).** The approver objected three times, and each is resolved:
1. **R11 authority.** The Operator chose the canonical binary64 rule on 2026-11-27, and R11 records it.
2. **`stat` guard.** CPython raises no audit event for `os.stat` or `os.lstat`, so an operation-time `os.stat(__file__)` passed every test. The purity test now wraps `stat`, `lstat`, `fstat`, and `access` directly. A reentrancy flag lets its own path checks call them. Explicit `stat`, `lstat`, `fstat`, `os.path.exists`, a read cached on the first call, and a directory listing each fail the test.
3. **Lazy standard-library imports.** Even with the extra preloads, an encoder that lazily imported `encodings.unicode_escape` failed purity. The warm-up run now allows standard-library module loading, so lazy imports such as `encodings.unicode_escape` and `email.utils` pass. The second run still allows no import or read at all, so a read of a standard-library source file during an operation still fails.
The scope-map `pending` rename was made at approval (c436cee), so it is no longer listed as outstanding.

**Implementer review resolutions.** The read-only review found six issues; all are resolved.
1. **Body number domain (blocking).** The approved R11 said "with integers within ±(2^53 − 1)", and my first pack refused integer tokens beyond that. A summary body is bytes, so this refused `10000000000000000`, which the revision encoder writes for the float `1e16`. At Gate B the approver escalated the choice, and on 2026-11-27 the Operator chose the canonical binary64 rule below. R11 now states the dependency's domain as a byte rule: a token is valid exactly when it is the RFC 8785 serialization of the finite binary64 value it denotes. Integer-versus-float meaning is left to the derive units. The test that pinned refusing `10**16` is replaced by `test_body_numbers_are_canonical_binary64`, which covers tokens around 2^53, `1e16`, `1e20`, and the fixed/exponent switch at `1e21` and `1e-7`.
2. **Float vectors.** `test_body_number_vectors_follow_rfc8785` pins the 24 finite RFC 8785 Appendix B vectors, from their IEEE 754 bits. They include long significands, the round-to-even example, subnormals, and neighbours of both thresholds. Each vector is checked through build and through direct decode of bytes the suite writes. Other spellings of the same values are refused by both.
3. **Non-canonical bodies.** `test_noncanonical_numbers_are_refused_in_build_and_decode` puts `-0`, `-0.0`, `1.00`, `1.0e1`, `2E-7` and others at nested object and array paths. Each must fail in build, and in decode with correctly recomputed digests.
4. **Repeated hour.** `test_watermark_orders_repeated_hour_by_instant` uses a synthetic fold-aware tzinfo that needs no timezone database. It covers both directions, and the same wall time an hour apart.
5. **Purity oracle.** The file-name exemption is gone, and descriptor-based opens are flagged. The description above now says exactly what is flagged. Both the reference implementation and the reviewer's independent implementation pass it.
6. **UTC range.** R9 now states the year 1–9999 domain. `test_watermark_times_stay_in_utc_range` pins `summary-input-invalid` at both edges, and acceptance of the valid neighbours.

Each of a set of incorrect implementations fails at least one test. They cover at least:
- `measured` despite a non-contributing record, or `missing` for a zero;
- each pairwise swap in the no-value precedence, and `in-conflict` ignored;
- a body allowed with `missing`, or missing with `measured`;
- a standing's required field not checked, duplicates allowed, and mixed processor versions allowed;
- `source-conflict` or `inferred-attribution` accepted from the caller, or not computed;
- a reported condition with no evidence behind it;
- manifest entries sorted as text instead of as numbers;
- identities written as JSON numbers;
- watermark positions not sorted, a duplicate installation, times kept in their local offset, and `processed_at` before `receipt_time`;
- the watermark left out of the digest;
- a publication condition stored in the bytes;
- a decoder that skips the digest, coverage, or state check, or that accepts an unknown version;
- a failure that carries an input value.

The implementer reviews the pack read-only for tests that a correct implementation could not pass, and those findings are resolved before freeze. The approver then confirms at Gate B that the pack carries this draft, that RED is genuine, that the effects declaration is empty as the contract says, and that the residuals below are complete.

## Residuals

Not mechanically provable here; the approver confirms these in implementation review:
- no clock, I/O, environment, or logging. A C-level clock read such as `datetime.now()` cannot be intercepted;
- the module only imports `set_source_selection_policy`'s `MEASURES`, and never opens a store;
- policy identities are copied, never parsed.

Left to other units:
- which records a day attributes, overlap and tie detection, and each body: the four derive units and `duplicate-observation-contract`;
- per-type counts of evidence no derive unit can place (Decision 6): `operational-store@6` and the presentation units;
- computing delivery positions and processing times, storing summaries, the generation and policy-horizon target, publication, and when each publication condition applies: `operational-store@6`, `refresh-summary-state`, and the completion units;
- whether a Source Conflict keeps the prior published summary (Decision 5): `refresh-summary-state`;
- range periods and counts: `range-summary-contract`;
- after approval, documentation changes outside this unit: glossary entries for `newer-evidence-pending` and `refresh-failed` and for how a summary carries its Processor Generation (Decisions 7 and 10).
