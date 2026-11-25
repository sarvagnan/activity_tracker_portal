# Governed Delivery Flow

This document defines how each Governed Scope moves from the scope map to a published Vouch unit and the phase order that sequences the remaining units toward version 1. Beads holds live status, ownership, and dependency readiness: each unit has one `Deliver <scope>@<version>` feature under its phase epic, and `bd ready` shows what can start. Vouch holds the lifecycle record. This document holds neither and therefore never records progress.

## Roles

Each role is a separate agent with its own context, as Vouch's method requires: the agent that drafts a pack never implements it, and the implementer never edits a pack. Vouch asks for separated duties between agents, not different models or vendors; its own decision records treat model and vendor differences as a heuristic, and independence stays human attested.

| Role | Agent | Owns |
|---|---|---|
| Operator | the Operator | Gate A for each unit's semantics, and every escalation |
| Drafter and lifecycle | Claude | contracts, packs, Beads, every Vouch transition, commits, pushes, and merges |
| Implementer | GPT 6.1 Sol, delegated through T3 | frozen packs and enabling tasks, editing only unbound files |
| Approver | GPT 6 Astra, delegated through T3 | Gate B freeze approval, implementation review, merge approval, and Gate C acceptance |

The approver escalates to the Operator, and Claude stops, for a deviation, an approver objection that Claude disputes, a change since Gate A that reverses an approved decision or removes a guarantee, an irreversible or destructive action, or a release go/no-go. Clarifications and refinements that keep the approved semantics are the approver's to accept. Each approver verdict is posted on the pull request, and Vouch transitions it authorizes are recorded with `--actor approver:gpt-6-astra`.

## Unit lifecycle

Every unit follows the same eight steps. Claude implemented the worked example, `ingest-protocol@1`, before these roles were assigned on 2026-10-12.

1. **Open.** Claim the unit feature, branch `<scope>-<version>` from `main`, and record the unit:

   ```bash
   python3 vouch.lock specify --root .vouch --name <scope> --version <version> --depends-on <dep> ...
   python3 vouch.lock generate --root .vouch
   ```

   Strict CI ignores a unit that is only specified, and Vouch refuses a strict-CI exception until the unit is frozen, so the exception is recorded at Gate B. Create four children under the unit feature: a `human` decision to review the draft, a task to complete the executable pack, an approver decision to freeze and record RED, and a task to implement.
2. **Draft.** Write `governance/scopes/<scope>.<version>.json` and `packs/<scope>-<version>.spec.md` with the proposed requirements, named failures, and test inventory, and extend `tests/governance/test_governed_scope_contracts.py` for the new contract. Also write `packs/<scope>-<version>.effects.json`, the Vouch effect declaration that carries the contract's `observable_effects`, because the store requires one for every bound freeze. Each effect that reads an external source is an `observation` with its `source` and `capture`. Each effect that changes an external system is an `action` with its `system`, its `outcome` (`confirmed`, `compensated`, or `irreversible`), and a `detail`. A contract with no observable effects declares an empty list. The mapping takes judgment, not a one-to-one copy. Sealing with a Keystore-held key reads that key (an observation), while an injected pure cipher is a transform; listing sealing as observable does not make it an external change. A network request separates what it captures from what it changes remotely, and its outcome must not claim more than the Sender confirms. A contract effect that both creates and deletes something, such as a key lifecycle, declares each separately. Vouch checks the declaration's structure and binds its bytes. Whether it is true and complete stays a review question at Gates A and B.
3. **Gate A: draft approval (Operator).** The Operator approves the draft's semantics and failure vocabulary or requests amendments. Approval authorizes executable pack work only.
4. **Executable pack.** Add acceptance tests in every implementing language, synthetic vectors or fixtures, the runner `tests/acceptance/run_<scope_with_underscores>_<version>.sh`, and Interface stubs. Show genuine RED, in which every failure comes from the stubs. The implementer reviews the pack read-only for tests a correct implementation could not pass, and Claude resolves its findings before freeze.
5. **Gate B: freeze approval (approver).** The approver checks that the pack faithfully carries the Operator-approved draft, that RED is genuine, that the effect declaration matches the contract's `observable_effects`, and that the binding and its stated residuals are complete. On approval, freeze the binding, record the strict-CI exception that CI requires for every frozen unit until it publishes, mark the unit ready, start it, record RED through the runner, and commit the frozen baseline by itself:

   ```bash
   python3 vouch.lock freeze --root .vouch --actor approver:gpt-6-astra --name <scope> --pack-version <scope>-pack/<version> --pack-file <spec> ... --pack-test-file <test> ... --pack-effects-file packs/<scope>-<version>.effects.json
   python3 vouch.lock mark-open-in-ci --root .vouch --name <scope> --reason "<why it may stay open>"
   python3 vouch.lock mark-ready --root .vouch --name <scope>
   python3 vouch.lock start --root .vouch --name <scope>
   python3 vouch.lock record-evidence --root .vouch --unit <scope>@<version> --kind red -- tests/acceptance/<runner>
   python3 vouch.lock advance-anchor --root .vouch --unit-name <scope>
   ```

6. **Implement.** Delegate the frozen pack to the implementer with the pack, its Interface, and the rule that only unbound implementation files may change. Claude confirms `check.pack-files-match` still passes, records GREEN through the same runner and advances the anchor, marks the unit done, and adds the runner to the project CI `test` job in the same branch. Before committing the implementation separately, `generate`, `check --ci --project . --pending-anchor-ok`, and `formal` must pass. The approver then reviews the implementation against the frozen pack; requested changes go back to the implementer as a new round.
7. **Merge (approver).** Push the branch and open a pull request. After CI passes and the approver approves the pull request, Claude merges it with a merge commit, never rebase-merge, so the frozen-baseline commit keeps its hash. CI on `main` advances the evidence anchor and pushes a `vouch: advance evidence anchors` commit.
8. **Gate C: acceptance (approver).** Once the anchor commit has landed and CI on `main` passed, the approver accepts the unit. Pull the anchor commit, then `publish --actor approver:gpt-6-astra`, which also removes the strict-CI exception, `generate`, and `check --ci --project .`. Commit and push `Publish <scope>@<version>`, close the unit feature and its children, and push Beads with `bd dolt push`.

Every `record-evidence`, including refactor evidence recorded for review fixes, is followed by `python3 vouch.lock advance-anchor --root .vouch --unit-name <scope>` before `generate`. The local advance moves the anchor to the new head without a run claim, so the next CI run on `main` claims it. Since the 2026-11-23 re-pin, Vouch no longer depends on this step: `check.evidence-anchors` reports an anchor that lags its head as not checked, and CI's anchor plan also claims such a head (D-34, which settled vouch_mk2-bd4.4.4). The local advance stays so that each commit's store already points its anchor at its head.

A mismatch discovered after freeze becomes an `open-deviation` for the Operator, never an edit to a bound file. If the Operator amends the pack, Claude records `adjudicate-amend`, `close-deviation` and `respecify` before any bound file changes, and the approver re-freezes the amended pack. GREEN is then recorded again on the bound runner. Since the 2026-11-23 re-pin, Vouch enforces this: after a re-freeze that changes the pack, `publish` is refused until a GREEN for the current pack is recorded (D-36, which settled vouch_mk2-bd4.4.5), and a rebind of a published unit likewise needs an explicit GREEN, which a REFACTOR record no longer satisfies (D-51). Enabling tasks skip the Vouch steps but follow the same implement, approver review, and merge sequence. An enabling task has no frozen pack, so nothing probes its edge cases before the code exists. For scheduling, retry, or concurrency logic, the implementer therefore adds adversarial tests: several failing items in a row, items that later succeed, and cycles that overrun their deadline. Every approver re-review covers the whole change, not only the latest fix.

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
| 2. Sender observation and staging | `begin-initial-snapshot`, `begin-reconciliation-snapshot`, `read-snapshot-page`, `read-change-page`, `stage-outbox-batch`, `stage-sync-completion`, `stage-sync-checkpoint`, `coordinate-sender-sync`, `schedule-background-polling`, `notify-operator-conditions` | Sender durable storage and Keystore primitives | The Sender traverses synthetic Health Connect data and stages byte-stable encrypted Outbox Messages that survive interruption. |
| 3. Receipt-bound delivery | `manage-sender-installation`, `manage-sender-enrollment`, `operational-store@2`, `accept-ingest-batch`, `accept-sync-completion`, `accept-sync-checkpoint`, `accept-capability-acknowledgement`, `send-outbox-message`, `receipt-bound-delivery` | Portal ingress HTTP shell; local device smoke delivery | Synthetic delivery archives exact bytes before receipt, and a device smoke run has delivered real data to a development Portal. |
| 4. Reconciliation and interpretation | `operational-store@3`, `operational-store@4`, `operational-store@5`, `reconcile-source-record`, `interpret-source-projection`, `interpret-steps`, `interpret-heart-rate`, `interpret-sleep-session`, `interpret-weight`, `execute-processing-job`, `set-source-selection-policy`, `set-fallback-timezone-policy` | Supervised `portal-worker` | Archived records reconcile and process into Canonical Revisions or named non-success outcomes through leased jobs. |
| 5. Summaries | `operational-store@6`, `summary-contract`, `duplicate-observation-contract`, `range-summary-contract`, `derive-daily-steps`, `derive-heart-rate-summary`, `derive-sleep-summary`, `derive-weight-series`, `compose-steps-range`, `compose-heart-rate-range`, `compose-sleep-range`, `compose-weight-range`, `refresh-summary-state` | — | Candidate Health Summaries refresh from current evidence. |
| 6. Completion and cutover | `complete-initial-snapshot`, `complete-change-cycle`, `complete-replacement-baseline`, `activate-processor-generation`, `complete-reconciliation-snapshot` | — | No partial snapshot, cycle, or processor upgrade can appear current. |
| 7. Portal presentation | `render-portal-view`, `portal-state-presentation`, `record-amendment` | Server-rendered Portal shell | All five destinations render every governed state distinction. |
| 8. Data rights and recovery | `operational-store@7`, `portable-export-contract`, `verify-portable-export`, `produce-portable-export`, `erase-retained-data`, `create-operational-backup`, `restore-operational-backup`, `migrate-operational-store`, `produce-diagnostic-bundle` | — | Export, verification, erasure, backup, restore, and migration work against synthetic state. |
| 9. Version 1 release | — | Physical-device qualification; reproducible signed APK; home-server deployment guide; Vouch v1 release; first production enrollment | The v1 release is complete and the Operator's Sender has finished its first Initial Snapshot. |

`portable-export-contract` and `verify-portable-export` depend on nothing else and may fill gaps in any phase. Exercise Session interpretation follows version 1 once Device evidence exists (ADR 0027).

## operational-store versions

The scope-map row describes the complete store. Its versions add transactions in the order their consumers arrive:

- `@1` covers ingest: Sender Installation lifecycle records, exact Raw Record and completion archival, sequence history and gaps, receipts, Sync Checkpoint history, installation-scoped Polling Freshness and Last Delivery, the storage reserve, coherent reads, and the baseline migration.
- `@2` covers delivery-cycle reads: each cycle's last archived fragment, its chain inputs, its fragment and record counts, and its completion, derived from the rows `@1` keeps without a schema change. The `accept-*` units use them instead of replaying archived envelopes (Operator decision, 2026-10-15).
- `@3` covers the Source Record index and reconciled evidence: Source Records keyed independently of Sender Installation, append-only evidence with observing-Sender provenance, an idempotent transaction that indexes archived items, and reconciled-selection history. It adds migration 2.
- `@4` covers processing: Processing Job queues and expiring leases, Canonical Revisions, and Unmodeled, Policy-Pending, quarantine and failed outcomes.
- `@5` covers policies: source-selection and fallback-timezone policy records, and atomic enqueueing of affected reprocessing and summary rebuilding.
- `@6` covers publication: Summary Refresh Jobs, materialized Health Summaries, Cutover Targets, Candidate Builds, Cutover Records, Processor Generations, activation-review tokens, Amendments, and presentation reads.
- `@7` covers data rights: the Deletion Ledger, Export Jobs, intersecting-target invalidation and lease cancellation before erasure, and backup capture metadata.

Each version's draft review may move a transaction between versions when its consumer's contract requires it. The Operator split processing into `@3`, `@4` and `@5` on 2026-11-24, so each draft stays reviewable. Archived items are indexed by their own transaction, which the `portal-worker` runs, so `accept-ingest-batch@1` is unchanged. A version that adds a migration relies on the published suites allowing later migrations and failure names, which the `@2` rebind (activity_tracker_portal-5ed) establishes before `@3` is specified.
