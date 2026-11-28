# Draft acceptance pack — `duplicate-observation-contract@1`

**Status:** Draft approved by the Operator on 2026-11-28, including the 11 decisions below as recommended; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `duplicate-observation-contract` Governed Scope contract, version 1, in `governance/scopes/duplicate-observation-contract.1.json`. It is a shared contract. It depends on the published `summary-contract@1`. `derive-heart-rate-summary` and `derive-weight-series` depend on it.

## What this unit does

Two apps can report the same Heart Rate sample or the same weighing. If both count, a mean, a count, or a latest point is biased. If one is silently dropped, evidence disappears. ADR 0026 and ADR 0041 settle the rule: after source selection, observations whose instants and values are exactly equal form a reversible Duplicate Group and contribute once; observations at the same instant with different values are unresolved, and no tie-break is applied.

This unit is that rule, as one pure function. A derive unit passes it the eligible observations of one record type. It returns:
- **contributions**: one per resolved instant, with its instant, its value, and its members. One member is a plain observation. Two or more members are a Duplicate Group, which contributes once;
- **unresolved ties**: one per instant with two or more different values, listing every observation there, grouped by value. Nothing at that instant contributes;
- **record facts**: which Source Records have an observation in a group, and which in a tie.

Every eligible observation lands in exactly one contribution or tie. Nothing is merged, dropped, or stored. The same set always gives the same result, in any order.

## What it does not do

- It does not choose eligible records. Applying the Source Selection Policy and Amendments, and reading evidence, belong to the derive units and the store (Decision 9).
- It does not read Canonical Revisions. Each derive unit extracts its observations (Decision 9).
- It does not attribute local dates (Decision 10).
- It assigns no summary-contract@1 standing and reports no evidence condition. The derive unit does, from this unit's record facts (Decision 7). It does not define new standings or conditions; it uses `duplicate-grouped` and `equal-time-conflict` as summary-contract@1 defines them.
- It computes no statistic: no sum, count, mean, minimum, maximum, or latest point.
- It does not handle Steps intervals or Sleep sessions (Decision 1), or Source Conflicts, which concern one Source Record's own projections (CONTEXT.md).
- It stores nothing, defines no bytes, and does not schedule recomputation (Decision 8).
- It renders nothing.

## Interface

The module `activity_tracker_portal.duplicate_observation_contract` provides frozen dataclasses, one function, one exception, and constants. It reads no clock, store, file, or network, and does not import `operational_store`.

**Constants.**
- `CONTRACT_VERSION = "duplicate-observation-contract@1"`.
- `GROUP_RULE = "exact-instant-and-value"` and `GROUP_CONFIDENCE = "exact"` (Decision 6).
- `FAILURES = ("duplicate-observation-input-invalid",)`.

**Values.**
- `Observation(record_id, index, source_system_kind, origin, instant_ns, value)`:
  - `record_id`: the Source Record identity, a positive `int`;
  - `index`: the observation's position in its record, a non-negative `int`. A Heart Rate sample's position in the body's `samples` list; 0 for a Weight measurement;
  - `source_system_kind` and `origin`: non-empty strings, as `operational-store@6` returns them;
  - `instant_ns`: the exact instant as integer nanoseconds since the Unix epoch;
  - `value`: the value in the record type's canonical unit, an `int` or a finite `float`, never a `bool`.
- `ObservationRef(record_id, index)`: an observation's identity.
- `Contribution(instant_ns, value, members, origins)`. `members` is a tuple of `ObservationRef`, sorted. `origins` is the sorted distinct `(source_system_kind, origin)` pairs of the members. It has `record_ids`, the sorted distinct record identities, and `is_group`, true with two or more members.
- `TieClass(value, members)`: the observations of a tie that share one value.
- `UnresolvedTie(instant_ns, classes)`: two or more `TieClass`, sorted by value. It has `record_ids`.
- `Resolution(contract_version, rule, confidence, contributions, ties, grouped_record_ids, tied_record_ids)`. It has `groups`, the contributions with two or more members.

**Operation.**
- `resolve(observations) -> Resolution`. `observations` is any iterable of `Observation`.
- `DuplicateObservationFailure(failure)`: its `args` are exactly `(failure,)`.

## Requirements

### R1 — Governed Scope contract

The contract has the shared shape, kind `shared`, and version 1. It depends on `summary-contract` at version 1 and nothing else. Its one named failure is `duplicate-observation-input-invalid`, an invariant refusal. It has no observable effects, and its effects declaration is empty. It does not contradict the scope-map row.

Proposed tests: `test_contract_shape_and_dependency`, `test_effects_declaration_is_empty`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — Pure, deterministic, order-independent

- `resolve` reads only its argument. It reads no clock, store, file, network, environment, or timezone database, writes nothing, and logs nothing.
- Any permutation of the same observations gives an equal result, whatever the process, `TZ`, locale, or hash seed.
- It does not change its input. All dataclasses are frozen.

Proposed tests: `test_result_ignores_input_order`, `test_result_ignores_hash_seed_and_host_timezone`, `test_resolve_reads_no_clock_or_io`, `test_input_is_not_mutated`, `test_values_are_frozen`.

### R3 — Constants

The constants hold exactly the values in the Interface.

Proposed tests: `test_constants_are_exact`.

### R4 — Input validation

Each of these is `duplicate-observation-input-invalid`, and nothing is returned:
- an element that is not an `Observation`;
- a field outside its domain: `record_id` not a positive `int`, `index` not a non-negative `int`, `instant_ns` not an `int`, either string empty or not a `str`, `value` a `bool`, a non-finite `float`, or another type;
- two observations with the same `ObservationRef`, even if identical;
- one `record_id` with two different `(source_system_kind, origin)` pairs;
- `int` and `float` values in one call (Decision 3).

An empty input gives a result with no contributions, ties, or record facts.

Proposed tests: `test_each_field_domain_is_checked`, `test_repeated_observation_is_refused`, `test_record_with_two_origins_is_refused`, `test_mixed_value_kinds_are_refused`, `test_non_observation_is_refused`, `test_empty_input_gives_empty_result`.

### R5 — Exact equality

- Two instants are equal exactly when their `instant_ns` are equal (Decision 2). One nanosecond apart is different.
- Two values are equal exactly when they are equal numbers of the call's one kind. There is no tolerance, rounding, or unit conversion (Decision 3). `-0.0` equals `0.0`; a group of them has the value `0.0`. Integers of any size compare exactly.
- Origin, record, and position are never compared (Decision 4).

Proposed tests: `test_instants_compare_by_nanoseconds`, `test_adjacent_nanoseconds_are_distinct`, `test_values_compare_exactly_without_tolerance`, `test_negative_zero_equals_zero`, `test_large_integers_compare_exactly`, `test_provenance_is_not_compared`.

### R6 — Every observation lands in exactly one place

For each distinct instant among the observations (Decision 5):
- all values equal: one `Contribution` with every observation at that instant as members;
- two or more different values: one `UnresolvedTie`, and no contribution at that instant.

So every observation is a member of exactly one contribution or one tie class. `contributions` and `ties` are each sorted by instant, and no instant appears in both.

Proposed tests: `test_every_observation_is_in_exactly_one_resolution`, `test_single_observation_contributes_alone`, `test_result_matches_reference_model_on_generated_sets`.

### R7 — Duplicate Groups

- A contribution with two or more members is a Duplicate Group. Its members may come from any origins, records, and positions (Decision 4).
- It contributes once: one instant and one value, the members' shared ones. It has no representative member (Decision 6).
- Members are sorted by `record_id`, then `index`, as numbers. `origins` lists the members' distinct origin pairs, sorted.
- Every group in a result is grouped under `GROUP_RULE` with `GROUP_CONFIDENCE`, which `Resolution` states.

Proposed tests: `test_equal_instant_and_value_form_one_group`, `test_group_spans_origins_records_and_positions`, `test_group_contributes_once`, `test_group_has_no_representative`, `test_group_members_are_sorted`, `test_group_lists_its_origins`.

### R8 — Unresolved ties

- An instant with two or more different values is one unresolved tie. Each `TieClass` holds the observations with one value; classes are sorted by value, members as in R7.
- Equal-valued observations at a tied instant are in the same class, not in a group (Decision 5).
- No observation of a tie contributes, and no value is chosen.

Proposed tests: `test_different_values_at_one_instant_are_a_tie`, `test_tie_includes_equal_valued_members`, `test_tie_contributes_nothing`, `test_tie_classes_are_sorted_by_value`.

### R9 — Reversible and local

The result depends only on the eligible set given (Decision 8):
- removing one member of a two-member group leaves a single contribution of the other;
- changing a value, or removing an observation, changes only the resolution at that instant;
- a tie with one class left after removal becomes a contribution;
- calls share no state. A call after another gives the same result as a call alone.

Proposed tests: `test_removing_a_member_reverses_the_group`, `test_changing_a_value_recomputes_the_instant`, `test_other_instants_are_unaffected`, `test_repeated_calls_share_no_state`.

### R10 — Record facts and summary-contract@1

- `grouped_record_ids` is the sorted distinct record identities with an observation in a group. `tied_record_ids` is the same for ties. A record can be in both.
- `Contribution.record_ids` and `UnresolvedTie.record_ids` show which records each involves.
- The suite shows that these facts are enough to meet summary-contract@1 R7 (Decision 7). For one-observation-per-record input, shaped like Weight, it marks tied records `unresolved` and the rest `contributing`, reports `duplicate-grouped` when a group spans two or more records and `equal-time-conflict` when a tie exists, and builds a valid summary through the published `summary_contract.build_summary`.

Proposed tests: `test_record_facts_are_sorted_and_exact`, `test_contribution_record_ids_show_whether_a_group_spans_records`, `test_weight_shaped_result_builds_a_valid_summary`.

### R11 — Scale

A day of Heart Rate can hold hundreds of thousands of samples. `resolve` sorts once and is otherwise linear. The suite resolves 300,000 observations, with groups and ties, within a generous limit that the executable pack sets.

Proposed tests: `test_large_input_is_resolved`.

### R12 — Health-data-free failures and representations

- Every failure is a `DuplicateObservationFailure` whose `args` are exactly `(failure,)`, with no `__cause__`, and whose traceback carries no input value.
- The `repr` of every value type shows record identities, positions, and counts only: no instant, value, or origin (Decision 11).

Proposed tests: `test_failures_carry_only_their_name`, `test_reprs_carry_no_instants_values_or_origins`.

## Decisions for the Operator

1. **Which observations this unit groups.**
   - **Recommended:** Heart Rate samples and Weight measurements only. A Heart Rate observation is one entry of the body's `samples`: its `time` and `beats_per_minute`. A Weight observation is one revision: its header's `time.start.instant` and its body's `weight_grams`.
   - Alternative: also Steps intervals with exactly equal start, end, and count.
   - Alternative: also Sleep sessions with exactly equal start, end, and stages.
   - Consequences: the recommendation matches ADR 0041 and the scope map, where only `derive-heart-rate-summary` and `derive-weight-series` depend on this unit. Two apps that export the identical Steps interval stay under `derive-daily-steps`' overlap rule: partial or unresolved, never counted once. Either alternative adds a dependent and contradicts ADR 0041's Steps and Sleep rules.

2. **How instants are compared.**
   - **Recommended:** by exact integer nanoseconds since the epoch. `12:00:00Z` and `12:00:00.000000000Z` are the same instant.
   - Alternative: by exact text.
   - Alternative: at a coarser common precision, such as milliseconds.
   - Consequences: under the recommendation, two apps that write one reading at different precisions (one truncating to milliseconds) give two instants. Both contribute, with no group and no tie. That is the bias exactness accepts; a single-origin Source Selection Policy avoids it. Exact text is strictly worse: the same instant written with a different number of fraction digits would count twice. A coarser precision invents an equality ADR 0041 does not allow, and can tie readings that really differ.

3. **How values are compared.**
   - **Recommended:** exact numeric equality in the record type's canonical unit (`1/min` integers, gram floats), with no tolerance, rounding, or conversion. One call holds one kind of number. `-0.0` equals `0.0`, as their RFC 8785 bytes do.
   - Alternative: a tolerance per type, such as ±1 bpm or ±50 g.
   - Alternative: compare after rounding to a display precision, such as 0.1 kg.
   - Consequences: under the recommendation, two apps that round the same weighing differently (72500.0 g and 72499.99 g) at one instant are an unresolved tie. The disagreement is visible, and the Operator resolves it by selecting one origin. Either alternative hides the disagreement and must then pick one value for the group, which is the arbitrary tie-break ADR 0041 forbids.

4. **Whether members must differ in origin or record.**
   - **Recommended:** no. Equal-instant, equal-value observations group whatever their origin or record. That includes two samples in one Heart Rate record and two Weight records from one app. The group lists its origins, so a view can say whether it spans origins.
   - Alternative: cross-origin only, as the scope-map row literally reads. Same-origin exact duplicates each contribute.
   - Alternative: cross-record only. Equal samples inside one record each contribute.
   - Consequences: the recommendation follows CONTEXT.md ("from any origins"), ADR 0041 ("across any eligible origins"), and `interpret-heart-rate@1` Decision 4, which kept equal-time samples inside one record so that Phase 5 applies this same rule. After approval the drafter rewords the scope-map row to "Duplicate Group, which may span origins". Under summary-contract@1, a group inside one record has only one contributing record, so its derive unit cannot report `duplicate-grouped` for it; R7 there needs two contributing entries. Its body can still show the group. Under either alternative, an app that writes one reading twice biases counts and means.

5. **An instant with some equal and some different values.**
   - **Recommended:** the instant is resolved as a whole. If all its values are equal, it is one group. If any differ, it is one tie containing every observation there, split into classes by value, and nothing at that instant contributes.
   - Alternative: the value most observations agree on contributes.
   - Alternative: each distinct value contributes once, equal ones deduplicated.
   - Consequences: under the recommendation, one disagreeing app makes the instant unresolved even if two others agree. That is conservative and visible. A majority is an arbitrary tie-break. Counting each distinct value treats a disagreement as two readings, which ADR 0041 calls unresolved.

6. **Group identity, representative, rationale, and confidence.**
   - **Recommended:** a group is identified by its instant and its sorted members. It has no representative: it contributes the shared instant and value. Every group states the rule `exact-instant-and-value` and the confidence `exact`, which ADR 0026 asks for and which are fixed in this version.
   - Alternative: a representative member, such as the lowest record identity, whose provenance the summary uses.
   - Alternative: a content digest as the group's identity.
   - Alternative: no confidence, since it is always exact.
   - Consequences: under the recommendation no member looks preferred, and every contributor can be named. A representative makes one origin look authoritative and changes when records arrive. A digest needs bytes this unit otherwise does not define (Decision 8). Dropping confidence departs from ADR 0026's wording for no gain.

7. **How groups and ties reach a summary-contract@1 summary.**
   - **Recommended:** this unit fixes what contributes (a group once, a tie nothing) and returns which records each involves. It assigns no standing and reports no condition, because summary-contract@1 gives both to the derive unit. The derive units must meet summary-contract@1 R7:
     - `duplicate-grouped` needs two contributing entries, so it is reported only for a group that spans two or more records;
     - `equal-time-conflict` needs an `unresolved` entry, so a derive unit that reports a tie must mark at least one tied record `unresolved`. Under summary-contract@1 R4, a day with a value is then `partial`, and one without is `unresolved`.

     For Weight, one observation per record, the mapping is direct: tied records are `unresolved` and group members `contributing`. For Heart Rate, a standing covers a whole record of many samples. `derive-heart-rate-summary` must choose: make the whole record `unresolved` when any sample ties, withholding its other samples; keep it `contributing` and show the tie only in the body, with no condition, so the day may read `measured`; or ask for a summary-contract@2 with an observation-level count.
   - Alternative: this unit fixes the mapping for every derive unit: a record with any tied observation is `unresolved`, and none of its observations contribute.
   - Alternative: this unit fixes the mapping: a record with a tied observation is `unresolved`, but its other observations still contribute.
   - Consequences: the recommendation keeps standings where summary-contract@1 put them and lets `derive-heart-rate-summary`'s draft weigh its own case. The risk is that the two derive units choose differently. The first alternative is uniform and conservative, but one tied sample withholds a whole Heart Rate series, perhaps a day's worth. The second contradicts summary-contract@1 R5, where `unresolved` means the derivation cannot use the record, while its samples would still be in the value.

8. **Reversibility: a pure function with no stored form.**
   - **Recommended:** `resolve` is a pure function of the eligible set. It keeps no state, stores nothing, merges nothing, and returns new values. Grouping changes after a correction, retraction, exclusion, or deletion because `refresh-summary-state` rederives from current evidence. This unit defines no bytes. Derive units copy what they need into their bodies, as `derive-weight-series`' row already plans. An earlier summary revision's grouping stays reproducible from its manifest's outcome identities and its derivation version, as ADR 0041 requires while evidence is retained.
   - Alternative: a canonical RFC 8785 form of the result, with a digest, that derive units embed in their bodies.
   - Alternative: group rows in the store, updated as evidence changes.
   - Consequences: the recommendation is the smallest unit, but the two derive units may write groups differently in their bodies. A shared form gives presentation and export one format, at the cost of a second encoding and decoder to govern and a body layout this unit would partly dictate. Stored rows make grouping mutable state that must stay in step with evidence, the destructive-merge risk ADR 0026 rules out, and need an operational-store change.

9. **Who filters the input, and in what shape.**
   - **Recommended:** the derive unit filters and extracts. It passes only observations it would otherwise count: from records whose latest selection has a Canonical Revision under the summary's processor version, whose origin the applicable Source Selection Policy admits (or the single available origin, ADR 0026), and that no Amendment excludes. In-conflict, Policy-Pending, quarantined, and failed records have no revision, so no observations. Each observation is the six fields in the Interface. The derive unit converts the instant text to nanoseconds.
   - Alternative: this unit takes Canonical Revisions and extracts Heart Rate samples and Weight points itself.
   - Alternative: this unit takes the instant text and parses it.
   - Consequences: the recommendation keeps the scope map's single dependency on `summary-contract` and leaves body layouts to the processors. Each derive unit writes its own extraction, and its suite tests it; a wrong conversion is not caught here. The first alternative adds dependencies on `interpret-heart-rate` and `interpret-weight` that the scope map does not list. The second moves a `projection/v1` instant parser into this unit.
   - Note: Amendments reach the store in `operational-store@9`, in Phase 7. Until then no exclusion exists, and this unit does not change when they arrive.

10. **What one call covers: instants, not days.**
    - **Recommended:** a derive unit resolves every eligible observation that could share an instant with one of the day's observations, not only those it has already given the day, and attributes contributions and ties to days afterwards. Observations at one instant usually share a local date, but two origins with different offsets can put one instant on two dates.
    - Alternative: resolve each local date's observations separately.
    - Consequences: under the recommendation, a pair across midnight counts once across a range. The derive unit must then say which day a group or tie belongs to when its members' dates disagree, and how Heart Rate samples without a `local_date` (`interpret-heart-rate@1` R4) take part. Per-day calls are simpler, but such a pair counts once on each of two days, so a range from `compose-heart-rate-range` counts it twice.

11. **Failures and representations.**
    - **Recommended:** one invariant refusal, `duplicate-observation-input-invalid`, for any malformed input. It carries only its name. Representations show record identities, positions, and counts, never instants, values, or origins.
    - Alternative: separate failure names for each fault, such as mixed kinds or a repeated observation.
    - Alternative: ordinary dataclass representations.
    - Consequences: every refusal is a derive-unit defect, never a data condition, because real data always resolves; groups and ties are outcomes, not failures. So one name is enough. Ordinary representations ease debugging but can put health values in logs.

## Ground rules

- **Data.** Python only, standard library plus the published `summary_contract`, which R10 uses. Observations are built by hand. All identities, origins, instants, and values are synthetic. No store is opened.
- **Model.** The suite has its own short reference model of R5–R8 and compares every result with it, including on generated sets with many shared instants.
- **Markers.** Synthetic marker values and origins show that no failure or `repr` carries them.
- **Unbound inputs, reviewed rather than frozen.** The binding is the contract, this spec, the suite, the runner, and the effects declaration. These stay outside it:
  - `docs/governance/scope-map.md`, read by R1's scope-map test;
  - the published code, contract, and pack of `summary-contract@1`, which its own binding freezes;
  - the execution environment: `uv`, `pyproject.toml`, `uv.lock`, and the managed Python;
  - `tests/governance/test_duplicate_observation_contract_contract.py`, which the governance suite runs and this runner does not.

## Freeze prerequisites

After Gate A, the executable pack adds:
- the suite `tests/acceptance/test_duplicate_observation_contract_1.py`: 44 tests, one per proposed name in R1–R12, grouped by requirement;
- the runner `tests/acceptance/run_duplicate_observation_contract_1.sh`, which runs only this suite;
- no fixture files;
- the Interface stub `src/activity_tracker_portal/duplicate_observation_contract.py`:
  - the real constants;
  - the real frozen dataclasses `Observation`, `ObservationRef`, `Contribution`, `TieClass`, `UnresolvedTie`, and `Resolution`, with the R12 representations: `Observation`'s kind, origin, instant, and value, `Contribution`'s instant, value, and origins, `TieClass`'s value, and `UnresolvedTie`'s instant have `repr=False`;
  - the properties `Contribution.record_ids` and `is_group`, `UnresolvedTie.record_ids`, and `Resolution.groups`, computed from the fields;
  - the real `DuplicateObservationFailure`, which also keeps its name as `failure`, as `SummaryContractFailure` does;
  - `resolve`, raising `NotImplementedError("duplicate-observation-contract@1")`.

**RED, against the stub.**
- 6 tests pass, because they read only the contract, the effects declaration, the scope map, and the constants, or build the dataclasses directly: the three R1 tests, `test_constants_are_exact`, `test_values_are_frozen`, and `test_reprs_carry_no_instants_values_or_origins`.
- The other 38 fail, with 262 failing entries, counting subtests. Every failure is the stub's `NotImplementedError`.
- `tests/unit`, `tests/governance`, and the bound runner of `summary-contract@1` still pass.

**How the suite checks results.** Most tests compare the whole result with a model of R5–R8 written in the suite. The model groups by instant, splits by value with a `dict`, and writes everything as plain tuples, including each value's type and `repr`. So `0.0` and `-0.0`, and `1` and `1.0`, differ. The comparison reads every field and property. The model also runs on 60 seeded generated sets, of up to 1,500 observations, with many shared and adjacent instants, and on R11's 300,000 observations.

**Refinements the tests pin beyond the approved text.** These are the drafter's choices, for the approver to confirm or escalate. No test needed a change to approved semantics.
- **Zero** (*settles*): any contribution or tie class whose value is a float zero has the value `0.0`, including a single `-0.0` observation and a group of only `-0.0`. The text said this only of a group. So the value never depends on which member is read first, and it matches the RFC 8785 bytes.
- **Types** (*settles*): every sequence field and property is a `tuple`. Members are `ObservationRef`. Origin pairs are 2-tuples of `str`. `is_group` is a `bool`.
- **Origin order** (*settles*): origin pairs sort by kind, then origin, in code-point order.
- **`bool`** (*settles*): `True` and `False` are refused in `record_id`, `index`, and `instant_ns`, as well as in `value`.
- **Argument** (*settles*): an argument that is not an iterable of `Observation` is `duplicate-observation-input-invalid`. That includes `None`, an `int`, and a bare `Observation`. Any iterable is accepted, including a one-shot iterator, a generator, a `frozenset`, and a `dict`.
- **Domains, as the text gives them:**
  - any `int` instant is accepted, including negative ones and ones beyond 64 bits;
  - any `int` value is accepted, including negative values and zero;
  - any finite `float` value is accepted, including subnormals and the largest finite value;
  - origins and kinds may hold any non-empty text, including non-ASCII.
- **Validation:** a bad observation is refused wherever it is, including last in 2,000. Not pinned: subclasses of `Observation`, `int`, `float`, or `str`; an iterable that raises while it is read; a call with no argument.
- **Failures** (*settles*): R12 says "no `__cause__`". The suite also requires `__context__` to be `None`, as summary-contract@1's implementation review required. It checks 15 kinds of malformed input among marker observations, with markers in record identities, positions, kinds, origins, instants, and values. Each is checked three ways: in a normal call, while the caller handles a `ValueError`, and while it handles a `JSONDecodeError`. Each frame in the failure's `__traceback__` after the caller's own frames is checked, not only this module's:
  - no local is, or contains, an input object;
  - `TracebackException(capture_locals=True)` output from those frames carries no marker.
- **Representations:** the suite checks only that no instant, value, kind, or origin appears. It treats the source-system kind like the origin (*settles*). The stub's `Resolution` repr also shows its three constant strings.
- **Order of results:** contributions and ties are ordered by instant as numbers. Members are ordered by record and position as numbers, and tie classes by value as numbers. The tests use orders where text and number disagree.
- **R10:** for Weight-shaped input, `duplicate-grouped` is reported when a group spans two or more records, and `equal-time-conflict` when any tie exists. The body is a synthetic `{"points":N}`. The test also shows Decision 4's consequence: a group inside one record, reported as `duplicate-grouped`, is refused by `summary_contract.build_summary` as `summary-state-inconsistent`.
- **R11:**
  - the input is 600 records of 500 samples, with about 155,000 distinct instants, about 20,000 groups, and about 69,000 ties;
  - the limit is 30 s of wall time for one call, in the test process;
  - where the platform has `SIGALRM`, a timer stops a call that runs past the limit;
  - the reference implementation took 1.4 to 1.9 s on the drafting machine (Apple M2), so the limit is about 16 times its slowest run;
  - "sorts once and is otherwise linear" is not checked directly. A quadratic grouping fails the limit.
- **Scope map:** the R1 test requires the row to name a "Duplicate Group" and an "unresolved tie", and not to say "cross-origin" (Decision 4).
- **The purity test** is summary-contract@1's, unchanged in its rules:
  - it runs in a subprocess with an audit hook;
  - `os.stat`, `os.lstat`, `os.fstat`, and `os.access` are wrapped directly, with a reentrancy flag;
  - file-system activity and environment reads are allowed only while `_find_and_load` or `_load` in `<frozen importlib._bootstrap>` is on the stack;
  - the only write allowed is a `.pyc` written by the loader;
  - the Python-level clock functions are replaced with guards;
  - any log record or stderr output is flagged.

  It preloads `summary_contract`, which R10 uses, and `random`, `signal`, `subprocess`, and `statistics`. It runs every scenario twice, including failing calls, checks both runs, and checks that their results are equal.

**Passability.** A throwaway reference implementation, kept outside the repository, passes all 44 tests. Each of 54 incorrect implementations fails at least one test. A 55th, which lazily imports `email.utils` and `xml.dom.minidom` during a call, passes, as a correct implementation may. The 54 are:
- **Comparison:**
  - instants sorted as text, truncated to milliseconds, or compared as floats;
  - values compared with a relative or absolute tolerance, or as floats;
  - `-0.0` not equal to `0.0`;
  - a single `-0.0`, or a tie class's `-0.0`, kept negative.
- **Grouping:**
  - same-origin duplicates each contributing;
  - the majority value winning a tie;
  - each distinct value contributing once;
  - a representative member, a representative's origin, or a representative's spelling of the value;
  - a duplicate observation dropped silently.
- **Order:**
  - members unsorted, or sorted as text;
  - tie classes sorted as text;
  - record facts unsorted;
  - origins unsorted, or not distinct;
  - a value that depends on input order.
- **Types and facts:**
  - origin pairs as lists, or members as a list;
  - singles counted as grouped;
  - tied records taken from one class only.
- **Validation:** accepting a `bool` value, a `bool` index, `nan`, an empty origin, record 0, a `float` instant, mixed `int` and `float` values, a repeated `ObservationRef`, or a record with two origins. Also: checking only the first element, and a native `TypeError` for a non-iterable argument.
- **State and input:**
  - mutating the input list;
  - a result cache shared between calls;
  - quadratic grouping.
- **Failures and reprs:**
  - raising `from None`, which leaves `__context__` set;
  - keeping a caller's exception as context;
  - keeping the input in the raising frame's locals;
  - raising from a deeper frame that holds the input;
  - a `repr` that shows a tie value, origins, or an instant.
- **Purity:** reading the clock, reading the environment, calling `os.stat` on the module's own file, caching a file read on the first call, logging, and writing to stderr.

The implementer reviews the pack read-only for tests that a correct implementation could not pass, and those findings are resolved before freeze. The approver then confirms at Gate B that the pack carries this draft, that RED is genuine, that the effects declaration is empty as the contract says, and that the residuals below are complete.

## Residuals

Not mechanically provable here; the approver confirms these in implementation review:
- no clock, I/O, environment, or logging. A C-level clock read cannot be intercepted;
- the module does not import `operational_store`.

Left to other units:
- choosing eligible records, extracting observations, converting instants, and attributing local dates: `derive-heart-rate-summary` and `derive-weight-series` (Decisions 9 and 10);
- standings and conditions from the record facts, and Heart Rate's per-record standing choice (Decision 7): the same two derive units;
- whether a summary body carries groups and ties, and in what form (Decision 8): the same two derive units;
- recomputation after evidence changes: `refresh-summary-state` and the store;
- Amendment exclusions: `record-amendment` and `operational-store@9`;
- after approval, documentation changes outside this unit: the scope-map row wording (Decision 4), and glossary entries for an unresolved tie and the `equal-time-conflict` condition, which CONTEXT.md does not define.
