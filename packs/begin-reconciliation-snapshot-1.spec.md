# Draft acceptance pack — `begin-reconciliation-snapshot@1`

**Status:** Draft for Operator review (Gate A), including the eight decisions below. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `begin-reconciliation-snapshot` Governed Scope contract in `governance/scopes/begin-reconciliation-snapshot.1.json`.

When an established baseline loses change-token continuity, the Sender can no longer prove it has seen every change (ADR 0048). It must reread the whole record type. Before the first page of that Reconciliation Snapshot, this unit:
- obtains a fresh pre-read changes token;
- commits that token with a new Reconciliation Snapshot identity;
- in the same transaction, supersedes and retains the attempt whose continuity was lost.

So a change that races the reread is not lost: the catch-up Change Cycle drains from the new token (`stage-outbox-batch@1` Decision 4).

The unit is TypeScript only. It reuses `begin-initial-snapshot`'s Kotlin token issuer through the same `ChangesTokenIssuer` seam. It writes only through `begin-initial-snapshot@2`'s `commitSnapshotAttempt`.

## Interface

**TypeScript**, module `sender/app/src/snapshot/begin-reconciliation-snapshot.ts`. It uses:
- `begin-initial-snapshot@2`'s `ChangesTokenIssuer`, `IssuedToken`, `SnapshotAttempt`, `BeginOptions`, `currentSnapshotAttempt`, and `commitSnapshotAttempt`;
- `stage-outbox-batch@1`'s `cycleProgress`;
- the 3ug storage seam.

Declarations:
- `ReconcileRequest`: `{ recordType, installationId, superseding }`. `superseding` is the identity of the record type's current attempt.
- `ReconcileResult` is one of:
  - `{ outcome: "begun", attempt: SnapshotAttempt }`
  - `{ outcome: "refused" | "not-permitted" | "read-failed" | "storage-failed", failure: string }`
- `beginReconciliationSnapshot(database, issuer, request, options)` returns a `ReconcileResult`.

Caller errors throw `TypeError`:
- a `recordType` outside the Wire Protocol pattern;
- an `installationId` or `superseding` that is not a lowercase UUID;
- a `randomUUID` result that is not a lowercase version 4 UUID.

## Requirements

TypeScript `node:test` suites in `begin-reconciliation-snapshot-1.test.ts` use:
- the 3ug `node:sqlite` substitute;
- a scripted issuer;
- injected UUID and time sources;
- `begin-initial-snapshot@1`, `stage-outbox-batch@1`, and the 3ug `NodeInstallationCipher` to create attempts and stage their pages.

`tests/acceptance/test_begin_reconciliation_snapshot_1.py` checks the contract. The runner `tests/acceptance/run_begin_reconciliation_snapshot_1.sh` verifies a fresh JUnit report.

### R1 — Governed Scope contract

The contract names:
- the scope, version, kind, and purpose;
- inputs, with their modes;
- outputs;
- 12 named failures in the outcome classes `refusal`, `not-permitted`, `read-failure`, and `storage-failure`;
- both effects;
- authority;
- the dependencies on `begin-initial-snapshot@2` and `stage-outbox-batch@1`;
- the acceptance oracle.

The scope-map row names those dependencies.

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R2 — Beginning a reconciliation

`begun` is returned only after one transaction has committed a new attempt through `commitSnapshotAttempt`. The attempt has:
- `kind: "reconciliation"`;
- the generated identity;
- the record type and installation;
- the issued token and `unreadSinceMs`;
- `begunAtMs` from `now()`.

The superseded attempt is retained, with `abandonedAtMs` set to that same reading. The new attempt is current, and it survives reopening the database.

Tests: `begin_commits_a_reconciliation_and_supersedes_the_named_attempt`, `the_reconciliation_survives_reopening`, `the_identity_is_the_generated_uuid`.

### R3 — Preconditions, checked before the token request

The request must satisfy three conditions, or it is refused with nothing written and no token requested:
- **`superseding` names the current attempt.** It must name the record type's current attempt. Otherwise the result is `attempt-mismatch`, including when the type has no current attempt.
- **Same installation.** The current attempt must belong to the request's Sender Installation. Otherwise the result is `installation-mismatch`.
- **An established page chain.** The current attempt must be one of these:
  - an attempt whose snapshot page chain `stage-outbox-batch@1` reports as finished;
  - an unfinished Reconciliation Snapshot, which is a reread restarted before it finished.

  An unfinished Initial Snapshot is `baseline-unfinished`. For that case, `coordinate-sender-sync` restarts the Initial Snapshot through `begin-initial-snapshot` instead.

Tests:
- `a_reconciliation_must_name_the_current_attempt`
- `another_installation_is_refused`
- `an_unfinished_initial_snapshot_is_refused`
- `a_finished_initial_or_reconciliation_may_be_reconciled`
- `an_unfinished_reconciliation_may_be_restarted`
- `refusals_request_no_token`

### R4 — The token request and its classification

The issuer is called once, after R3 passes. Its outcomes are handled as in `begin-initial-snapshot@1` R7:
- `not-permitted` and `read-failed` pass through when the failure name is one the contract gives that outcome class;
- a malformed result is `read-failed` with `source-contract-violation`;
- an issuer that throws is `read-failed` with `unexpected-read-failure`.

Tests: `issuer_failures_write_nothing`, `a_throwing_issuer_is_an_unexpected_read_failure`, `malformed_issuances_are_violations`.

### R5 — The identity source

`randomUUID` is called once, after the issuer succeeds and before the commit transaction. A result that is not a lowercase version 4 UUID throws `TypeError` and writes nothing.

Tests: `a_bad_uuid_source_is_a_caller_error`.

### R6 — The commit rechecks

Inside the commit transaction, R3's checks run again before `now()` is read. Then the attempt is committed through `commitSnapshotAttempt`.

If the current attempt changed meanwhile, for example because another begin or reconciliation landed, the result is that check's refusal. The issued token is discarded, and nothing is written.

Tests: `the_commit_rechecks_the_preconditions`.

### R7 — Nothing begun on failure

A database error at any point is `storage-failed`, and the transaction rolls back everything, including the supersession. That covers:
- a failing read before the token request, which requests no token;
- any failing read or write inside the transaction;
- a failing commit.

Tests: `storage_errors_roll_back_everything`, `refusals_write_nothing`.

### R8 — Interplay with staging

After a reconciliation begins:
- staging any page of the superseded attempt's cycles is `cycle-not-current` in `stage-outbox-batch@1`;
- the new attempt's first snapshot page stages as a `reconciliation-snapshot` cycle;
- `changeOrigin` reports the new pre-read token.

Tests: `staging_follows_the_reconciliation`.

### R9 — Caller errors, health-data-free outcomes, and order of checks

**Caller errors.** Malformed requests throw `TypeError` before any database or issuer call.

**Health-data-free outcomes.** Every non-`begun` result carries only a named failure. Every named failure is reachable, and no other failure is produced.

**Order of checks.** The first applicable check wins:
1. caller errors;
2. the R3 refusals, in the order `attempt-mismatch`, `installation-mismatch`, `baseline-unfinished`;
3. the issuer outcome;
4. the UUID source;
5. the commit, with its recheck and storage failure.

Tests: `malformed_requests_are_caller_errors`, `non_begun_results_carry_only_named_failures`, `every_named_failure_is_reachable_and_in_the_contract`, `checks_follow_the_documented_order`.

## Decisions for the Operator

1. **TypeScript only, reusing the token issuer.** A Reconciliation Snapshot needs the same pre-read token, permission checks, and failure classification as an Initial Snapshot. So this unit calls `begin-initial-snapshot`'s Kotlin issuer through the `ChangesTokenIssuer` seam, and adds no Kotlin. The Expo bridge waits for `coordinate-sender-sync`.
2. **Writes go through `begin-initial-snapshot@2`'s primitive.** This was the Operator's 2026-10-14 decision. The unit owns no table and no migration, and the one-current-attempt rule stays with the tables' owner.
3. **The baseline floor is a finished page chain, and the caller decides the rest.** This was also the Operator's 2026-10-14 decision. The unit refuses only an unfinished Initial Snapshot. Whether the Portal already holds an established baseline is `coordinate-sender-sync`'s judgment, made from Delivery Receipts once they exist. That covers the case where the Snapshot Completion and the catch-up Change Completion were accepted before first publication.
4. **An unfinished reconciliation may be restarted.** If continuity is lost again during a reread, its own pre-read token can expire. A fresh reconciliation then supersedes the unfinished one, following ADR 0048's "Start Fresh Reconciliation". The baseline it reconciles against is still the earlier finished attempt.
5. **The caller always names the attempt.** A reconciliation never begins without superseding the current attempt. A record type with no attempt needs an Initial Snapshot.
6. **Same Sender Installation only.** A reconciliation reconciles one installation's evidence, and Reconciliation Absences require unchanged installation coverage (ADR 0048). A new installation's baseline is a replacement Initial Snapshot (ADR 0051), so a current attempt from another installation is refused.
7. **The commit-time reading dates both changes.** `now()` is read once, inside the transaction after the recheck. It gives the new attempt's `begunAtMs`, and through the primitive, the superseded attempt's `abandonedAtMs`. This matches `begin-initial-snapshot@1`.
8. **New dependencies.** The scope-map row named none. The unit depends on `begin-initial-snapshot@2` for writes and on `stage-outbox-batch@1` for the page-chain floor, so the row is updated.

## Ground rules

These are `begin-initial-snapshot@1`'s ground rules for its TypeScript part.

The trusted boundary is:
- Node 26.11.1;
- `uv` and the pinned CPython;
- the 3ug seams;
- the published `begin-initial-snapshot@2` and `stage-outbox-batch@1` modules;
- the protocol library.

Not mechanically provable here: the production UUID source is a cryptographically random version 4 generator. The approver confirms this in implementation review.

## Freeze prerequisites

After Operator approval, the executable pack adds:
- the TypeScript suite, about 25 tests;
- the Python contract suite, 2 tests;
- the self-verifying runner;
- the Interface stub.
