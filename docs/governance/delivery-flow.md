# Governed Delivery Flow

This document defines how each Governed Scope moves from the scope map to a published Vouch unit and the phase order that sequences the remaining units toward version 1. Beads holds live status, ownership, and dependency readiness: each unit has one `Deliver <scope>@<version>` feature under its phase epic, and `bd ready` shows what can start. Vouch holds the lifecycle record. This document holds neither and therefore never records progress.

## Unit lifecycle

Every unit follows the same eight steps. The Operator owns the three gates; Claude drafts and implements. The worked example is `ingest-protocol@1`.

1. **Open.** Claim the unit feature, branch `<scope>-<version>` from `main`, and record the unit:

   ```bash
   python3 vouch.lock specify --root .vouch --name <scope> --version <version> --depends-on <dep> ...
   python3 vouch.lock generate --root .vouch
   ```

   Strict CI ignores a unit that is only specified, and Vouch refuses a strict-CI exception until the unit is frozen, so the exception is recorded at Gate B.

   Create four children under the unit feature: a `human` decision to review the draft, a task to complete the executable pack, a `human` decision to freeze and record RED, and a task to implement.
2. **Draft.** Write `governance/scopes/<scope>.<version>.json` and `packs/<scope>-<version>.spec.md` with the proposed requirements, named failures, and test inventory, and extend `tests/governance/test_governed_scope_contracts.py` for the new contract.
3. **Gate A: draft approval.** The Operator approves the draft's semantics and failure vocabulary or requests amendments. Approval authorizes executable pack work only.
4. **Executable pack.** Add acceptance tests in every implementing language, synthetic vectors or fixtures, the runner `tests/acceptance/run_<scope_with_underscores>_<version>.sh`, and Interface stubs. Show genuine RED, in which every failure comes from the stubs, and present the exact file binding.
5. **Gate B: freeze approval.** On approval, freeze the binding, record the strict-CI exception that CI requires for every frozen unit until it publishes, mark the unit ready, start it, record RED through the runner, and commit the frozen baseline by itself:

   ```bash
   python3 vouch.lock freeze --root .vouch --name <scope> --pack-version <scope>-pack/<version> --pack-file <spec> ... --pack-test-file <test> ...
   python3 vouch.lock mark-open-in-ci --root .vouch --name <scope> --reason "<why it may stay open>"
   python3 vouch.lock mark-ready --root .vouch --name <scope>
   python3 vouch.lock start --root .vouch --name <scope>
   python3 vouch.lock record-evidence --root .vouch --unit <scope>@<version> --kind red -- tests/acceptance/<runner>
   ```

6. **Implement.** Change only unbound implementation files, record GREEN through the same runner, mark the unit done, and add the runner to the project CI `test` job in the same branch. Before committing the implementation separately, `generate`, `check --ci --project . --pending-anchor-ok`, and `formal` must pass.
7. **Merge.** Push the branch and open a pull request. After CI passes, the Operator merges by fast-forward or merge commit, never rebase-merge, so the frozen-baseline commit keeps its hash. CI on `main` advances the evidence anchor and pushes a `vouch: advance evidence anchors` commit.
8. **Gate C: acceptance.** The Operator accepts the unit. Pull the anchor commit, then `publish`, which also removes the strict-CI exception, `generate`, and `check --ci --project .`. Commit and push `Publish <scope>@<version>`, close the unit feature and its children, and push Beads with `bd dolt push`.

A mismatch discovered after freeze becomes an `open-deviation`, never an edit to a bound file.

## Sequencing rules

- Specify a unit in Vouch only when its work starts. Vouch refuses a unit whose dependencies are not yet specified, and strict CI requires an exception for every frozen unit that is not yet terminal.
- Publication requires every Vouch dependency to be `published` or `release-bound`. A unit may be drafted and implemented against a frozen upstream pack, but Beads waits for upstream publication so implementation never chases a moving interface.
- `operational-store` grows by versions. A new version requires superseding its predecessor, and a superseded dependency blocks dependent publication until its successor publishes. Each successor therefore waits in Beads until every dependent of the previous version is published. Each version stays additive so published dependents keep their packs; a breaking change would require `rebind-pack` for every affected dependent.
- Enabling tasks pin toolchains and build shells, adapters, and primitives. They carry no governed behavior, and what they build is exercised by unit acceptance packs.
- Repository fixtures and CI evidence are synthetic. Device runs are local and emit only health-data-free output (ADR 0036).
- Independent branches of the graph may proceed in parallel, but at most one unit occupies each branch.

## Phases

Phases group units by the capability they establish. Dependencies, not phase boundaries, decide readiness; a later-phase unit with satisfied dependencies may proceed early.

| Phase | Governed units | Enabling work | Exit |
|---|---|---|---|
| 0. Protocol | `ingest-protocol@1` | Vouch bootstrap and CI | Published. |
| 1. Foundations | `project-source-record@1`, `operational-store@1` | Sender workspace and synthetic test harness; Portal runtime foundation | Both shared roots published with the Sender and Portal toolchains pinned in CI. |
| 2. Sender observation and staging | `begin-initial-snapshot`, `begin-reconciliation-snapshot`, `read-snapshot-page`, `read-change-page`, `stage-outbox-batch`, `stage-sync-completion`, `stage-sync-checkpoint`, `coordinate-sender-sync`, `schedule-background-polling` | Sender durable storage and Keystore primitives | The Sender traverses synthetic Health Connect data and stages byte-stable encrypted Outbox Messages that survive interruption. |
| 3. Receipt-bound delivery | `manage-sender-installation`, `accept-ingest-batch`, `accept-sync-completion`, `accept-sync-checkpoint`, `send-outbox-message`, `receipt-bound-delivery` | Portal ingress HTTP shell; local device smoke delivery | Synthetic delivery archives exact bytes before receipt, and a device smoke run has delivered real data to a development Portal. |
| 4. Reconciliation and interpretation | `operational-store@2`, `reconcile-source-record`, `interpret-source-projection`, `interpret-steps`, `interpret-heart-rate`, `interpret-sleep-session`, `interpret-weight`, `execute-processing-job`, `set-source-selection-policy`, `set-fallback-timezone-policy` | Supervised `portal-worker` | Archived records reconcile and process into Canonical Revisions or named non-success outcomes through leased jobs. |
| 5. Summaries | `operational-store@3`, `summary-contract`, `duplicate-observation-contract`, `range-summary-contract`, `derive-daily-steps`, `derive-heart-rate-summary`, `derive-sleep-summary`, `derive-weight-series`, `compose-steps-range`, `compose-heart-rate-range`, `compose-sleep-range`, `compose-weight-range`, `refresh-summary-state` | — | Candidate Health Summaries refresh from current evidence. |
| 6. Completion and cutover | `complete-initial-snapshot`, `complete-change-cycle`, `complete-replacement-baseline`, `activate-processor-generation`, `complete-reconciliation-snapshot` | — | No partial snapshot, cycle, or processor upgrade can appear current. |
| 7. Portal presentation | `render-portal-view`, `portal-state-presentation`, `record-amendment` | Server-rendered Portal shell | All five destinations render every governed state distinction. |
| 8. Data rights and recovery | `operational-store@4`, `portable-export-contract`, `verify-portable-export`, `produce-portable-export`, `erase-retained-data`, `create-operational-backup`, `restore-operational-backup`, `migrate-operational-store`, `produce-diagnostic-bundle` | — | Export, verification, erasure, backup, restore, and migration work against synthetic state. |
| 9. Version 1 release | — | Physical-device qualification; reproducible signed APK; home-server deployment guide; Vouch v1 release; first production enrollment | The v1 release is complete and the Operator's Sender has finished its first Initial Snapshot. |

`portable-export-contract` and `verify-portable-export` depend on nothing else and may fill gaps in any phase. Exercise Session interpretation follows version 1 once Device evidence exists (ADR 0027).

## operational-store versions

The scope-map row describes the complete store. Its versions add transactions in the order their consumers arrive:

- `@1` covers ingest: Sender Installation lifecycle records, exact Raw Record and completion archival, sequence history and gaps, receipts, Sync Checkpoint history, installation-scoped Polling Freshness and Last Delivery, the storage reserve, coherent reads, and the baseline migration.
- `@2` covers processing: Processing Job queues and leases, reconciled current evidence, Canonical Revisions, quarantine and policy-pending outcomes, policy records, and summary-refresh enqueueing.
- `@3` covers publication: Summary Refresh Jobs, materialized Health Summaries, Cutover Targets, Candidate Builds, Cutover Records, Processor Generations, activation-review tokens, Amendments, and presentation reads.
- `@4` covers data rights: the Deletion Ledger, Export Jobs, intersecting-target invalidation and lease cancellation before erasure, and backup capture metadata.

Each version's draft review may move a transaction between versions when its consumer's contract requires it.
