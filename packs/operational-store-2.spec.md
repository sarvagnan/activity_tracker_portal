# Draft acceptance pack — `operational-store@2`

**Status:** Draft approved by the Operator on 2026-10-15, including the six decisions below; the executable pack adds the refinements its tests pin down. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `operational-store` Governed Scope contract, version 2, in `governance/scopes/operational-store.2.json`. It is the additive successor to the published `operational-store@1`, which it supersedes.

To accept an Ingest Batch, the Portal must know where that batch's delivery cycle stands: its last accepted fragment and the chain position after it. To accept a completion, the Portal needs the cycle's counts and final chain. `@1` archives every fact needed, but offers no read for them. Its Decision 2 instead had the Portal rebuild `ingest-protocol` delivery state at startup by replaying every accepted envelope.

On 2026-10-15 the Operator chose to deliver this successor first, replacing that decision. Replay grows with history, and it stops being possible once a record deletion erases the envelope that contained the record (ADR 0020). The planned processing, publication, and data-rights versions move to `@3`, `@4`, and `@5`.

## Interface

Everything in `operational-store@1`'s Interface is unchanged:
- `open_store` and every write operation;
- every `StoreView` read;
- the record and result types;
- `FAILURES` and its partition;
- `MIGRATIONS`, with no new migration.

The module `activity_tracker_portal.operational_store` adds:
- `DeliveryCycle`, a frozen dataclass describing one delivery cycle of one installation, from its archived rows:
  - `installation_id` and `cycle_id`;
  - `record_type`: that of the cycle's last archived fragment, or of its completion when it has no fragment;
  - `cycle_kind`: that of its last archived fragment, or `None` when it has none;
  - `first_sequence` and `last_sequence`: the sequences of its first and last archived fragments, or `None`;
  - `last_source_page_index` and `last_fragment_index`: the last fragment's indexes, or `0` and `0`;
  - `last_previous_chain` and `last_message_sha256`: the last fragment's previous chain and message SHA-256, or `None`;
  - `fragment_count`: the number of its archived fragments;
  - `record_count`: the number of items in those fragments, Source Projections and Source Retractions alike;
  - `completion_sequence`: the sequence of its archived completion, or `None`.
- `StoreView.delivery_cycle(installation_id, cycle_id) -> DeliveryCycle | None`. It returns `None` when no archived fragment or completion of that installation carries that cycle identity, including for an unknown installation.

"Last" means highest sequence. The store reports archived facts. It does not decide whether a fragment continues its cycle or whether a completion is true. `accept-ingest-batch` and `accept-sync-completion` decide those from these facts with `ingest-protocol`. Callers compute the chain position after the last fragment as `chain_next(last_previous_chain, last_message_sha256)`.

## Requirements

`operational-store@1`'s requirements R1–R12 carry over unchanged. The `@2` runner re-runs `@1`'s bound runner, so its suite must still pass exactly.

The new requirements are bound to a new Python suite, `tests/acceptance/test_operational_store_2.py`. It uses the store's public operations against real databases in temporary directories, with synthetic values only, and like `@1`'s suite it never imports `ingest_protocol`.

### R13 — Governed Scope contract, version 2

The `@2` contract keeps `@1`'s:
- inputs and their modes;
- named failures and outcome classes;
- observable effect;
- empty dependency list.

It adds the delivery-cycle read to the purpose, the coherent view, and authority. It does not contradict the `operational-store` scope-map row.

Tests: `test_contract_2_keeps_version_1_and_adds_cycle_reads`, `test_scope_map_row_does_not_contradict_contract_2`.

### R14 — A cycle's last fragment and counts

For an installation and cycle identity with archived fragments, `delivery_cycle` reports:
- from the fragment with the highest sequence: its record type, cycle kind, source-page index, fragment index, previous chain, and message SHA-256;
- the first fragment's sequence and the last fragment's sequence;
- the number of fragments, and the number of their items, counting projections and retractions alike. A fragment with no items still counts as a fragment.

Fragments of other cycles and other installations, and other message kinds interleaved between the cycle's fragments, never change the result.

Tests:
- `test_cycle_reports_its_last_fragment_and_chain_inputs`
- `test_cycle_counts_fragments_and_items`
- `test_interleaved_messages_do_not_change_a_cycle`

### R15 — Absence, completion, and scope

The read behaves as follows:
- **No evidence:** `None` when no archived fragment or completion carries the identity, for a known or unknown installation.
- **Completion only:** the record type comes from the completion, the kind is `None`, the counts are zero, and `completion_sequence` is set.
- **Fragments and completion:** both are reported. The record type and kind still come from the last fragment, even when the completion names another record type.
- **Scope:** a cycle identity belongs to one installation. The same identity under another installation is a separate cycle.

Tests:
- `test_unknown_cycle_or_installation_is_none`
- `test_completion_without_fragments`
- `test_completed_cycle_reports_its_completion`
- `test_cycles_are_scoped_to_their_installation`

### R16 — Coherent, durable, and health-data-free

The read belongs to the view's snapshot: a fragment committed after the view opened is not seen. It survives a reopen of the database. A `DeliveryCycle` contains no Source Projection bytes, Source Identities, or health values; it holds identities, a record type name, counts, sequences, and digests.

Tests:
- `test_cycle_read_is_isolated_from_later_commits`
- `test_cycle_read_survives_reopen`
- `test_cycle_read_is_health_data_free`

### R17 — No schema change

`MIGRATIONS` is exactly `@1`'s: one baseline migration with an unchanged checksum. A database created under `@1` therefore opens under `@2`, and its existing archive answers cycle reads with no migration. Opening and reading it changes no schema object, schema version, or migration record.

The test proves this with `tests/acceptance/fixtures/operational-store-1-archive.sql`. That file is a dump of a synthetic database produced by the published `@1` module at `bee3744`, with no `delivery_cycle`.

Tests: `test_version_2_keeps_the_baseline_schema`, `test_version_1_database_answers_cycle_reads`.

## Decisions for the Operator

1. **Reads, not replay.** On 2026-10-15 the Operator replaced `@1` Decision 2: the Portal reads cycle state from the store rather than replaying every accepted envelope. `accepted_envelopes` stays available. The alternative was to keep replay, which grows with history and breaks once a deletion erases envelopes.
2. **No schema change.** The read derives everything from the rows `@1` already keeps, through their primary keys, so existing databases open unchanged. A new database needs no migration step, which only arrives with `migrate-operational-store` in Phase 8 (ADR 0032).

   The cost is speed. The primary keys index installation and sequence, not cycle identity:
   - Finding an active cycle's last fragment walks that installation's fragments back from the newest, which is quick.
   - Proving a cycle absent, and finding its first fragment and counts, scan all of that installation's fragments. That is about 7 ms at 30,000 fragments, by Sol's measurement in review.
   - The item count must be driven by fragments: each matching fragment's items counted through the item key. That is about 3 ms. Joining all of the installation's items instead took about 134 ms.

   Because `accept-ingest-batch` reads once per fragment, the work over one snapshot cycle grows with the square of the installation's fragments. That is acceptable for a once-per-installation Initial Snapshot. A later version can add an index once migration exists.

   The alternative is an index added through a second migration. Every existing `@1` database would then refuse to open, with `store-migration-required`, until Phase 8, or would have to be recreated.
3. **The store stays protocol-agnostic.** It reports the last fragment's previous chain and message digest, and callers compute the next chain position with `ingest-protocol`. This keeps `@1` Decision 1: no dependency on `ingest-protocol`. The alternative, computing the chain in the store, would make the store depend on the protocol.
4. **Facts, not verdicts.** The read reports what is archived and never refuses. Absence is `None`, even for an unknown installation, because callers check the installation separately. Continuity and completion validity stay with the `accept-*` units.
5. **The `@1` suite is re-bound.** The `@2` runner calls `@1`'s bound runner unchanged, then runs the new suite. Its binding includes `@1`'s bound files.
6. **Later versions are renumbered.** Processing becomes `@3`, publication `@4`, and data rights `@5`. `docs/governance/delivery-flow.md` now lists `operational-store@2` and the new `accept-capability-acknowledgement` in Phase 3, which the Operator added the same day.

## Ground rules

These are `@1`'s ground rules, and the trusted boundary is unchanged.

The new suite archives synthetic Ingest Batches, completions, checkpoints, and capability acknowledgements through the store's own operations. Chain and digest values are synthetic 64-character hex strings. The store never checks their meaning, and the envelope bytes hash to the header's digest as `@1` requires.

Not mechanically provable here; the approver confirms these in implementation review:
- the implementation changes nothing in `@1`'s behaviour or schema beyond adding the read;
- it counts items by fragment, not by joining all of the installation's items (Decision 2);
- it keeps all aggregation inside `delivery_cycle`, out of opening, view creation, and writes.

## Freeze prerequisites

The Operator approved the draft. The executable pack adds:
- the new suite `tests/acceptance/test_operational_store_2.py`, 14 tests;
- the `@1` fixture, `tests/acceptance/fixtures/operational-store-1-archive.sql` (R17);
- the runner `tests/acceptance/run_operational_store_2.sh`, which runs `@1`'s bound runner unchanged and then the new suite;
- the Interface stub, appended to the published module: `DeliveryCycle`, and a `delivery_cycle` method that raises `NotImplementedError`.

The tests pin these refinements:
- A later fragment that disagrees with earlier ones is still the last, because the store reports facts, not verdicts (Decision 4).
- An empty fragment counts.
- A completion's record type never overrides the fragments'.

Against the stub:
- the 10 R14–R16 tests, and `test_version_1_database_answers_cycle_reads`, fail;
- the two contract tests and `test_version_2_keeps_the_baseline_schema` pass, because they check invariants the stub already keeps;
- `@1`'s runner passes its 53 tests, because `@1`'s behaviour is intact.

The runner therefore exits 1. A throwaway reference, kept out of the repository, passes all 14 new tests and `@1`'s 53. It kills all 15 mutants:
- the first fragment taken for the last;
- the installation ignored in each of the three lookups;
- items counted per fragment as one;
- retractions not counted;
- the completion's record type preferred;
- empty fragments dropped;
- a completion without fragments reported as absent;
- the completion dropped;
- the first sequence taken from the last fragment;
- the kind dropped;
- wrong empty indexes;
- an unknown installation refused;
- the message digest reported as the previous chain.

GPT 6.1 Sol's read-only implementability review found nothing blocking. Its four P2 findings are resolved above:
- the `@1` fixture;
- the fuller cost in Decision 2, with fragment-driven counting;
- the completion-type test;
- the empty-fragment test.

Its two notes are also resolved: the contract test compares unordered collections, and the stub-failure count is corrected.

The binding is:
- **spec files:** this contract and pack, and `@1`'s contract and pack, which R1–R12 carry over and R13 compares against;
- **test files:**
  - the new suite, the `@1` fixture, and the runner;
  - every file `operational-store-pack/1` binds: `test_operational_store_1.py` and `run_operational_store_1.sh`.
