# Draft acceptance pack — `reconcile-source-record@1`

**Status:** Draft approved by the Operator on 2026-11-24, including the six decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `reconcile-source-record` Governed Scope contract, version 1, in `governance/scopes/reconcile-source-record.1.json`. It depends on the published `operational-store@3`.

A Source Record can be observed many times: by its initial snapshot, by later change cycles, and by a replacement Sender Installation after a reinstall (ADR 0050). Its evidence then arrives in delivery order, which is not always source order. A snapshot page can deliver an older version after a change cycle delivered a newer one. A replacement installation can re-observe a record a retired installation already delivered. This unit decides, from the evidence alone, which evidence is current, and records that decision through `operational-store@3`. The decision is a selection, not an activation. Phase 6's completion units activate selections into current views at cutovers (ADR 0045, 0048, 0051).

## Interface

The module `activity_tracker_portal.reconcile_source_record` provides:
- `Decision(outcome, evidence_ids)`: a frozen dataclass.
  - `outcome` is `"projection"`, `"retraction"`, or `"conflict"`.
  - `evidence_ids` is a tuple of `operational-store@3` evidence identities, in the delivery order of the record's evidence.
- `decide(record: SourceRecord) -> Decision`: a pure function of one `operational-store@3` `SourceRecord`.
- `ReconcileResult(record_id, decision, selection, unchanged)`: a frozen dataclass.
  - `selection` is the `operational-store@3` `Selection` recorded or returned.
  - `unchanged` says whether the decision equals the record's previous selection.
- `reconcile(store, record_id, decided_at) -> ReconcileResult`: reads the record in one read transaction, decides, and records the decision at the evidence head it read.
- `ReconcileRefusal(failure)` and `ReconcileRetryable(failure)`: exceptions carrying one named failure.

## Requirements

### R1 — Governed Scope contract

The contract has the shared shape, kind `leaf`, version 1, and depends only on `operational-store@3`. Its named failures are `source-record-unknown`, an invariant refusal, and `evidence-moved`, `storage-reserve-exhausted`, and `store-busy`, which are retryable. Its one observable effect is the selection write. It does not contradict the scope-map row.

Proposed tests: `test_contract_shape_and_dependency`, `test_scope_map_row_does_not_contradict_contract`, `test_effects_declaration_matches_contract`.

### R2 — Source modification time orders projections

Among a record's projections, the newest by source modification time is current, whatever order they were delivered in and whichever installation delivered them. A projection delivered later with an older modification time never displaces a newer one (Decision 1).

Proposed tests: `test_newest_projection_wins`, `test_later_delivered_older_projection_does_not_win`, `test_installation_does_not_affect_the_choice`.

### R3 — The same content is one assertion

Two or more projections with the newest modification time and the same projection digest are one assertion. This is the usual case when a record is re-observed, for example by a later snapshot or a replacement installation. The decision names the earliest delivered of them, and it is not a conflict (Decision 3).

Proposed tests: `test_identical_reobservation_is_not_a_conflict`, `test_identical_reobservation_names_the_earliest_delivered`.

### R4 — Different content at the same time is a Source Conflict

When projections with the newest modification time carry two or more different digests, the decision is a Source Conflict. It names, in delivery order, the earliest delivered projection of each distinct digest. A conflict is never resolved by arrival order. A newer projection ends it.

Proposed tests: `test_different_content_at_the_newest_time_conflicts`, `test_conflict_names_one_row_per_distinct_content`, `test_newer_projection_ends_a_conflict`.

### R5 — Retractions

A retraction carries no modification time. It supersedes every projection delivered before it: its effective time is the newest modification time among those projections, or earlier than any time if none precede it.
- The record is retracted when it has a retraction whose effective time is at or after every projection's modification time.
- The decision names the earliest delivered retraction with the greatest effective time.
- A projection delivered after a retraction makes the record current again only if its modification time is strictly newer than that retraction's effective time.
- A retraction never erases or hides evidence. It only takes part in the decision (Decision 2).

Proposed tests: `test_retraction_after_projection_retracts`, `test_reprojection_after_retraction_with_newer_time_wins`, `test_stale_projection_after_retraction_does_not_revive`, `test_projection_after_an_earlier_retraction_wins`, `test_latest_effective_retraction_is_named`, `test_retraction_supersedes_a_conflict_it_follows`.

### R6 — `decide` is pure and deterministic

`decide` reads only the given `SourceRecord`. Its result depends only on each evidence row's kind, modification time, digest, and delivery position. It does not depend on evidence identities, installation identities, cycle identities, selections, or wall-clock time. Shuffling the order in which the store returned the rows never changes the decision. `decide` never reads projection bytes, and has no `clientRecordId` to use.

Proposed tests: `test_decide_ignores_installation_cycle_and_selection`, `test_decide_is_order_independent`, `test_decide_has_no_store_or_clock_dependency`.

### R7 — Recording and unchanged state

`reconcile` records the decision as a selection at the evidence head it read, and returns it.
- `unchanged` is true when the record's latest earlier selection had the same outcome and evidence.
- The selection is still recorded at a new head, so the record leaves `records_awaiting_selection` (Decision 5).
- At an unchanged head, an identical decision writes nothing and returns the existing selection.
- `reconcile` never changes evidence, and never writes anything other than the one selection.

Proposed tests: `test_reconcile_records_the_decision`, `test_reconcile_reports_unchanged_and_still_advances`, `test_identical_reconcile_writes_nothing`, `test_reconcile_writes_only_one_selection`.

### R8 — Failures

- An unknown record is `source-record-unknown`.
- Evidence indexed between the read and the write is `evidence-moved`, a retryable failure the caller retries; `reconcile` does not retry by itself (Decision 6).
- The store's `storage-reserve-exhausted` and `store-busy` pass through as retryable failures.

Each failure leaves the whole database unchanged, and its text carries no health data or Source Identity.

Proposed tests: `test_unknown_record_is_refused`, `test_evidence_moved_is_retryable_and_writes_nothing`, `test_reserve_and_busy_are_retryable`, `test_failures_are_health_data_free`.

## Decisions for the Operator

1. **Source modification time, not arrival.** Projections are ordered by the source's `last_modified_time`, as the scope requires. The alternative is delivery order, which lets a late snapshot page overwrite a newer change.
2. **The retraction rule.** A retraction cannot carry a modification time, so it is placed directly after the projections delivered before it. A projection delivered later revives the record only with a strictly newer modification time. Health Connect assigns a new modification time to any genuine re-insert, so an equal or older time means a stale re-delivery. The alternatives:
   - order retractions by delivery only, which lets a stale re-delivery revive a deleted record;
   - make a retraction final, which would wrongly ignore a genuine re-insert.
3. **Identical content is not a conflict.** A record re-observed with the same digest and time is the same assertion, which is common after a reinstall (ADR 0050). The alternative, a conflict whenever two rows share a time, would flag every re-observation.
4. **Absence and abandonment wait for Phase 6.** `operational-store@3` stores neither. `complete-reconciliation-snapshot` and the abandonment rules of ADR 0048 add them, together with a successor version of this unit. Until then, evidence from an incomplete or later-abandoned snapshot attempt can be selected here. That is harmless, because no selection is activated before a cutover, and a cutover requires completed attempts. The alternative, inferring abandonment now from `@2`'s delivery-cycle reads, would duplicate Phase 6's rules before they exist.
5. **Unchanged still records.** When the decision does not change, it is recorded at the new head, flagged `unchanged`, so the record stops waiting. The alternative, writing nothing, would leave the record waiting forever.
6. **The caller retries.** `evidence-moved` is returned, not looped on, so `reconcile` stays one short read and one short write. The `portal-worker` retries. The alternative, an internal retry loop, hides contention and can run unbounded.

## Ground rules

- **Data.** Python only, standard library only. Tests use real temporary `operational-store@3` databases, built through the store's own operations with synthetic projections and hand-written index entries. `decide` tests may construct `SourceRecord` values directly.
- **Database checks.** Every "writes nothing" and failure assertion compares the whole database, including migration records and every row.
- **Synthetic markers.** Synthetic modification times use several time zones, to show ordering is by instant. A health-data marker and an identity marker appear in projection bytes and identities, to show results and failures carry neither.
- **Not mechanically provable here**, so the approver confirms these in implementation review:
  - `reconcile` uses one read transaction and one store write;
  - `decide` reads no store and no clock.

## Freeze prerequisites

After Gate A, the executable pack adds:
- the suite `tests/acceptance/test_reconcile_source_record_1.py`, with the tests above;
- the runner `tests/acceptance/run_reconcile_source_record_1.sh`;
- an Interface stub module whose operations raise `NotImplementedError`.

RED must come entirely from the stub, and the implementer's read-only findings must be resolved before freeze.
