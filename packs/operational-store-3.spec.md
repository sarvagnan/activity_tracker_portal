# Draft acceptance pack — `operational-store@3`

**Status:** Draft for the Operator's Gate A review. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `operational-store` Governed Scope contract, version 3, in `governance/scopes/operational-store.3.json`. It is the additive successor to the published `operational-store@2`, which it supersedes.

Phase 4 turns archived Raw Records into current source evidence. Its first consumer, `reconcile-source-record`, selects the current evidence for one Source Record "across observing Sender Installations without letting arrival order overwrite source ordering" (scope map). `@2` keeps every archived item, but only by delivery position: it has no notion of a Source Record, its evidence, or a selection. `@3` adds that index.

On 2026-11-24 the Operator split the planned processing version in three: `@3`, this Source Record index; `@4`, Processing Jobs, leases, Canonical Revisions and outcomes; and `@5`, policy records and summary-refresh enqueueing. The same day the Operator chose to index archived items in a separate transaction that the `portal-worker` runs, so `accept-ingest-batch@1` is unchanged, and to let the store grow by migrations after the `@2` rebind (Beads `activity_tracker_portal-5ed`).

## Interface

Everything in `operational-store@2`'s Interface is unchanged, except that `FAILURES` gains this version's names and `MIGRATIONS` gains migration 2.

The module `activity_tracker_portal.operational_store` adds these frozen dataclasses:
- `ItemPosition(installation_id, sequence, item_index)`: one archived Ingest Batch item.
- `PendingItem`: one archived item not yet indexed: `position`, `record_type`, `projection_schema_version`, `cycle_kind`, `cycle_id`, and exactly one of `projection` (the archived bytes) or `retracted_identity`.
- `ProjectionEntry(position, source_system_kind, origin, source_identity, last_modified_time)`: the caller's extraction from one archived projection. `last_modified_time` is a timezone-aware `datetime`.
- `RetractionEntry(position)`: one archived retraction. Its record type and Source Identity are the archived ones.
- `SourceKey(source_system_kind, origin, record_type, source_identity)`.
- `Evidence`: `evidence_id` (an integer that increases with every indexed item), `kind` (`"projection"` or `"retraction"`), `position`, `cycle_kind`, `cycle_id`, `last_modified_time` and `projection_sha256` (both `None` for a retraction), and `indexed_at`.
- `Selection`: `selection_id`, `outcome` (`"projection"`, `"retraction"`, or `"conflict"`), `evidence_ids`, `evidence_head` (the head it was computed from), and `recorded_at`.
- `SourceRecord`: `record_id`, `key`, `evidence` (in `evidence_id` order), `evidence_head` (the highest `evidence_id` in `evidence`, or `None`), `observing_installations` (sorted), and `selections` (in recording order).
- `IndexResult(created_records, created_evidence)` and `IndexCounts(unindexed_items, source_records, evidence_items, records_awaiting_selection)`.

It adds these operations:
- `Store.index_items(entries, at) -> IndexResult`
- `Store.record_selection(record_id, outcome, evidence_ids, evidence_head, at) -> Selection`
- `StoreView.pending_items(limit) -> tuple[PendingItem, ...]`
- `StoreView.source_record_id(key) -> int | None`
- `StoreView.source_record(record_id) -> SourceRecord | None`
- `StoreView.records_awaiting_selection(limit) -> tuple[int, ...]`
- `StoreView.index_counts() -> IndexCounts`

## Requirements

`operational-store@1` and `@2`'s requirements R1–R17 carry over, as rebound on 2026-11-24. The `@3` runner re-runs `@2`'s bound runner unchanged, so both earlier suites must still pass exactly.

### R18 — Governed Scope contract, version 3

The `@3` contract keeps `@2`'s inputs and modes, observable effect, and empty dependency list. Its named failures begin with `@2`'s, in order and with the same outcome classes, followed by the seven new invariant refusals. Its outputs keep `@2`'s names and add `index_result` and `selection_record`. Its `owns` list begins with `@2`'s, except that its first entry now covers later additive migrations. It does not contradict the `operational-store` scope-map row.

Proposed tests: `test_contract_3_extends_version_2`, `test_scope_map_row_does_not_contradict_contract_3`, `test_failures_match_contract_3`.

### R19 — Migration 2 is additive

`MIGRATIONS` is `@2`'s baseline followed by migration 2, which only creates the index tables and their indexes. A new database records migrations 1 and 2 with their checksums and `user_version` 2. A database at migration 1, such as one written by `@2`, refuses with `store-migration-required` and is left unchanged: opening never upgrades (R3, ADR 0032). Brought to migration 2 with no backfill, as R17 does, it opens, and every item in its archive is pending.

Proposed tests: `test_new_database_records_migrations_1_and_2`, `test_version_2_database_requires_migration_and_is_untouched`, `test_migrated_version_2_archive_is_pending`.

### R20 — Pending items

`pending_items(limit)` returns archived Ingest Batch items that have no evidence row, in archival order (message archival order, then `item_index`), at most `limit` of them. Completions, Sync Checkpoints, and capability acknowledgements are not items and never appear. A projection item carries its exact archived bytes. The read writes nothing.

Proposed tests: `test_pending_items_are_every_unindexed_item_in_archival_order`, `test_pending_items_respects_limit`, `test_indexed_items_leave_pending`, `test_only_ingest_batch_items_are_pending`, `test_pending_read_writes_nothing`.

### R21 — Indexing projections

`index_items` records one evidence row per entry, in one transaction, and returns how many Source Records and evidence rows it created.

- **Identity.** A projection's Source Record is keyed by source-system kind, origin, the archived record type, and Source Identity. The Sender Installation is never part of the key (ADR 0050).
  - The first entry with a new key creates the Source Record.
  - The same key observed by another installation joins that Source Record, which then lists both installations as observing.
  - A different origin, Source Identity, record type, or source-system kind is a different Source Record.
  - The entry has no `client_record_id`, so it can never contribute to identity.
- **Evidence.** The evidence row keeps the position, the batch's cycle kind and identity, the modification time, and the SHA-256 of the archived projection bytes. Nothing is overwritten, and no evidence is erased.
- **Idempotence.** Re-indexing a position with an identical entry writes nothing and returns zero counts, and the whole database stays unchanged. Repeating an entry within one call counts once.

Proposed tests: `test_new_key_creates_a_source_record`, `test_another_installation_joins_the_same_source_record`, `test_each_key_part_separates_source_records`, `test_evidence_keeps_position_cycle_time_and_digest`, `test_identical_reindex_writes_nothing`, `test_repeated_entry_in_one_call_counts_once`.

### R22 — Indexing retractions

A retraction's evidence row takes its record type and Source Identity from the archived item. It is part of every Source Record with that record type and Source Identity, whatever their origin (Decision 2).
- A retraction indexed before any matching projection still belongs to Source Records created later.
- The evidence order keeps a retraction and a later re-projection of the same identity in delivery order.
- A retraction never deletes, hides, or changes any projection evidence.

Proposed tests: `test_retraction_belongs_to_every_matching_source_record`, `test_retraction_before_projection_still_belongs`, `test_retract_then_reproject_keeps_delivery_order`, `test_retraction_changes_no_projection_evidence`.

### R23 — Index refusals

An index call is refused, with the whole database unchanged, in this order:
1. `timestamp-invalid`: `at` or any `last_modified_time` is naive.
2. `source-key-invalid`: a projection entry's source-system kind, origin, or Source Identity is empty or not a string.
3. `archived-item-unknown`: a position names no archived Ingest Batch item.
4. `index-entry-mismatch`: a projection entry names a retraction item, or a retraction entry names a projection item.
5. `index-entry-conflict`: a position is already indexed, or repeated within the call, with a different entry.
6. `storage-reserve-exhausted`: free space is below the reserve (Decision 5).

The call is all-or-nothing: one refused entry refuses every entry, and a failure injected after any row write commits nothing.

Proposed tests: `test_index_refusal_order`, `test_each_index_refusal_leaves_database_unchanged`, `test_one_bad_entry_refuses_the_whole_call`, `test_index_is_atomic_under_injected_failure`.

### R24 — Source Record reads

- `source_record_id(key)` returns the record's identity, or `None`.
- `source_record(record_id)` returns `None` for an unknown identity. Otherwise it returns the key, every evidence row in `evidence_id` order, the evidence head, the sorted observing installations, and every selection.
- Each read comes from one read transaction, so a write committed during the read is either wholly visible or wholly absent.
- Every read leaves the whole database unchanged.

Proposed tests: `test_source_record_lists_evidence_and_provenance`, `test_unknown_source_record_reads_none`, `test_source_record_read_is_isolated_from_later_commits`, `test_source_record_reads_write_nothing`.

### R25 — Reconciled selections

`record_selection` appends one selection to a Source Record's history and never changes an earlier one. The store checks the shape of a selection. It does not judge which evidence should win; that belongs to `reconcile-source-record`.
- **Outcomes.**
  - `projection` names exactly one projection evidence row of the record.
  - `retraction` names exactly one retraction evidence row that belongs to the record.
  - `conflict` names two or more projection evidence rows of the record.
- **Refusals:**
  - `source-record-unknown` for an unknown record;
  - `selection-evidence-invalid` for any other shape;
  - `selection-stale` when `evidence_head` is not the record's current evidence head;
  - `timestamp-invalid`;
  - `storage-reserve-exhausted`.

  Each leaves the whole database unchanged.
- **Idempotence.** Repeating the latest selection exactly, at the same head, returns it and writes nothing.
- **Awaiting selection.** `records_awaiting_selection(limit)` returns, in ascending identity, the records that have evidence but no selection at their current evidence head.

Proposed tests: `test_each_selection_outcome_is_recorded`, `test_selection_shapes_are_checked`, `test_stale_selection_is_refused`, `test_selection_history_is_append_only`, `test_identical_selection_writes_nothing`, `test_records_awaiting_selection`, `test_new_evidence_makes_a_selected_record_await_again`.

### R26 — Health-data-free counts and refusals

`index_counts()` returns only counts. Refusal text and the `repr` of `IndexResult`, `IndexCounts`, and every `StoreRefusal` carry no projection bytes and no Source Identity. Tests use a synthetic marker in projection bytes and identities to prove it.

Proposed tests: `test_index_counts`, `test_refusals_and_counts_are_health_data_free`.

### R27 — `@1` and `@2` unchanged

Opening, archival, lifecycle, cycle reads, and every `@1` and `@2` refusal behave as before. Indexing and selection never change any `@1` table. The runner proves the first by re-running `@2`'s bound runner. A test proves the second by comparing every `@1` table before and after a mixed series of index and selection calls.

Proposed tests: `test_index_and_selection_change_no_version_1_table`.

## Decisions for the Operator

1. **The caller extracts the key.** The `portal-worker` decodes each pending projection with the published projection contract and passes its origin, Source Identity, and modification time. That keeps `@1` Decision 1: the store never parses health data or depends on a protocol. The alternative is that the store parses projection JSON itself, which ties the store to the projection format.
2. **Retractions match by record type and Source Identity across origins.** A Health Connect retraction carries only the record's `Metadata.id`. Health Connect assigns those ids itself, unique within the store, so one id under two origins is not expected. If it happens, both Source Records see the retraction, and `reconcile-source-record` can surface it. The alternative is to refuse or quarantine an ambiguous retraction in the store, which turns a store fact into a verdict.
3. **Absence and abandonment arrive later.** Reconciliation Absence comes from a completed Reconciliation Snapshot, and Abandoned Attempt Evidence from a superseded snapshot attempt. Both are produced in Phase 6 (`complete-reconciliation-snapshot`, ADR 0048). `@3` records each evidence row's cycle kind and identity, so a consumer can ask `@2`'s `delivery_cycle` whether that cycle completed. The version that creates absences and abandonment adds them. The alternative is to add empty tables for them now, before any consumer defines them.
4. **Facts, not verdicts.** The store reports positions, cycles, modification times, and digests. It does not order evidence, detect Source Conflicts, or decide what is current; `reconcile-source-record` does. It only refuses a selection whose shape is impossible or whose evidence has moved on.
5. **The storage reserve also gates index and selection writes.** They grow the database, and the reserve exists to keep room for deletion and recovery (ADR 0033). The alternative is to gate only archival, as `@1` does, so that indexing can keep consuming the reserve.
6. **Optimistic concurrency.** A selection names the evidence head it was computed from, and the store refuses it if newer evidence has arrived. Nothing is locked between the read and the write. The alternative, holding a write lock while reconciling, conflicts with short transactions (ADR 0016).
7. **Existing databases must be recreated.** A database at migration 1 refuses to open under `@3`, with `store-migration-required`, until `migrate-operational-store` arrives in Phase 8 (ADR 0032). No production database exists; development and smoke stores are recreated. The alternative is to pull a minimal migrate command forward, which ADR 0032 ties to a verified backup.
8. **The runner chains.** The `@3` runner runs `@2`'s bound runner unchanged, then the new suite. Its binding includes `@2`'s bound files.

## Ground rules

These are `@1` and `@2`'s ground rules, and the trusted boundary is unchanged: standard-library SQLite only, synthetic values only, real databases in temporary directories, and whole-database snapshots for every "writes nothing" and refusal assertion. Index entries are synthetic: the suite archives synthetic Ingest Batches through the store's own operations and passes hand-written extractions. The store never checks them against the projection bytes (Decision 1).

Not mechanically provable here; the approver confirms these in implementation review:
- migration 2 only creates tables and indexes;
- no `@1` or `@2` code path changes behaviour;
- the store never parses projection bytes.

## Freeze prerequisites

After Gate A, the executable pack adds:
- the new suite `tests/acceptance/test_operational_store_3.py`, with the tests above;
- the runner `tests/acceptance/run_operational_store_3.sh`, which runs `@2`'s bound runner unchanged and then the new suite;
- the Interface stub, appended to the published module: the dataclasses, `FAILURES` entries, migration 2's SQL, and operations that raise `NotImplementedError`;
- `packs/operational-store-3.effects.json`, declaring the one confirmed database-write action.

RED must come entirely from the stubs, and the reviewer's read-only findings must be resolved before freeze.
