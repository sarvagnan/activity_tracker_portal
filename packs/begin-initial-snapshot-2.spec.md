# Draft acceptance pack — `begin-initial-snapshot@2`

**Status:** Draft for Operator review (Gate A), including the five decisions below. Freeze and later lifecycle state are recorded by Vouch (`python3 vouch.lock status --root .vouch`), not in this file.

**Acceptance owner:** Operator, who approves the draft semantics. **Pack drafter:** Claude. **Implementer after freeze:** GPT 6.1 Sol through a T3 delegated task. **Approver for freeze, implementation review, merge, and publication:** GPT 6 Astra through a T3 delegated task, as a separate agent from the drafter and implementer.

This unit carries the `begin-initial-snapshot` Governed Scope contract, version 2, in `governance/scopes/begin-initial-snapshot.2.json`. It is the additive successor to the published `begin-initial-snapshot@1`, which it supersedes.

`@1` owns the Sender's snapshot-attempt tables. Its R8 and Decision 5 let no other module write them. But it can only begin Initial Snapshots, and `begin-reconciliation-snapshot` must commit Reconciliation Snapshot attempts into the same tables under the same one-current-attempt rule. On 2026-10-14 the Operator chose to deliver this successor first. It keeps everything `@1` does and adds one exported primitive that any snapshot unit can call inside its own transaction.

## Interface

Everything in `begin-initial-snapshot@1`'s Interface is unchanged:
- the Kotlin `issueChangesToken` and `TokenIssuance`;
- the TypeScript `beginInitialSnapshot`, `currentSnapshotAttempt`, and `snapshotAttempt`;
- `snapshotAttemptsMigration`, with no new migration;
- the types `IssuedToken`, `ChangesTokenIssuer`, `SnapshotAttempt`, `BeginRequest`, `BeginOptions`, and `BeginResult`.

The TypeScript module `sender/app/src/snapshot/begin-initial-snapshot.ts` adds:
- `NewSnapshotAttempt`:

  ```ts
  { snapshotId, recordType, kind: "initial" | "reconciliation", installationId,
    changesToken, unreadSinceMs, begunAtMs }
  ```

- `CommitAttemptResult`, which is one of:
  - `{ outcome: "committed", attempt: SnapshotAttempt }`
  - `{ outcome: "refused", failure: "attempt-open" | "attempt-mismatch" }`
- `commitSnapshotAttempt(transaction, attempt, superseding)`, where `superseding` is the identity of the current attempt to supersede, or `null`. It returns a `CommitAttemptResult`.

## Requirements

`begin-initial-snapshot@1`'s requirements R1–R11 carry over unchanged. The `@2` pack re-runs `@1`'s bound runner, so its Kotlin, TypeScript, and Python suites must still pass exactly.

The new requirements are bound to a new TypeScript suite, `sender/app/test/begin-initial-snapshot-2.test.ts`, which uses the same 3ug `node:sqlite` substitute. A new Python suite, `tests/acceptance/test_begin_initial_snapshot_2.py`, checks the contract.

### R12 — Governed Scope contract, version 2

The `@2` contract keeps `@1`'s 11 named failures, outcome classes, dependency, and effects. It adds the primitive's input and output and its authority. It narrows the Reconciliation Snapshot exclusion to the reconciliation rules, which stay with `begin-reconciliation-snapshot`.

The scope-map row names the primitive and still satisfies `@1`'s row checks.

Tests (Python): `test_governed_scope_contract_has_required_shape`, `test_scope_map_row_does_not_contradict_contract`.

### R13 — The primitive commits an attempt of either kind

Inside the caller's transaction, `commitSnapshotAttempt` does three things:
- it inserts the attempt exactly as given, with `abandonedAtMs` null;
- it makes the attempt its record type's current attempt;
- it returns the attempt as `currentSnapshotAttempt` and `snapshotAttempt` would read it.

Both kinds are accepted. A committed attempt survives reopening the database.

Tests: `the_primitive_commits_an_initial_attempt`, `the_primitive_commits_a_reconciliation_attempt`, `a_committed_attempt_survives_reopening`.

### R14 — The one-current-attempt rule

The rule is `@1`'s R6, applied to every caller:
- With `superseding` null, a record type that already has a current attempt is `attempt-open`.
- With `superseding` named, it must be the record type's current attempt, or the result is `attempt-mismatch`. That includes a type with no current attempt, an attempt of another type, and an attempt that is no longer current.
- When `superseding` matches, the superseded attempt is retained. Its `abandonedAtMs` becomes the new attempt's `begunAtMs`, and it stops being current, in the same transaction.

A refusal writes nothing. Record types are independent.

Tests: `an_open_attempt_refuses_an_unnamed_commit`, `superseding_must_name_the_current_attempt`, `a_superseded_attempt_is_abandoned_and_retained`, `refused_commits_write_nothing`, `types_are_independent_for_the_primitive`.

### R15 — Only inside the caller's transaction

The primitive never opens, commits, or rolls back a transaction, and reads no clock or random source. It runs its reads and writes on the handle it is given.
- When the caller's transaction rolls back after a successful commit, nothing remains.
- A database error inside the primitive propagates to the caller unchanged, so the caller's transaction rolls back and classifies the error.
- An attempt identity already used by any attempt is a database error, which propagates.

Tests: `a_callers_rollback_undoes_the_commit`, `database_errors_propagate_to_the_caller`, `a_reused_snapshot_identity_is_rejected`.

### R16 — Caller errors and compatibility

A malformed attempt or `superseding` throws `TypeError` before any database call. That covers:
- an identity, installation, or `superseding` that is not a lowercase UUID;
- a `snapshotId` that is not a lowercase version 4 UUID;
- a record type outside the Wire Protocol pattern;
- an unknown kind;
- an empty or non-string changes token;
- a non-safe-integer instant.

The validation rules are `@1`'s R9, extended to the new fields.

`beginInitialSnapshot` behaves exactly as in `@1`. `@1`'s re-run suites show this, as does the published `stage-outbox-batch@1`, which reads attempts through `currentSnapshotAttempt` and `snapshotAttempt`.

Tests: `malformed_commits_are_caller_errors`, `begin_initial_snapshot_one_behaviour_is_unchanged` (the bound `@1` runner).

## Decisions for the Operator

1. **An additive successor.** `@2` keeps every `@1` export, behavior, and test, and adds no schema migration. The published `stage-outbox-batch@1` keeps its pack. Superseding `@1` blocks only dependents that are not yet published, and none are specified.
2. **The primitive runs only inside its caller's transaction.** The caller supplies every value: the identity, the token, and the times. The caller also does its own token request, preconditions, and failure classification. The primitive enforces only what belongs to the tables' owner: one current attempt per record type, explicit supersession, and retention. This keeps `begin-reconciliation-snapshot`'s rules with that unit.
3. **Supersession is abandonment.** A superseded attempt is retained, with `abandonedAtMs` set to the new attempt's begin time, matching `@1`'s named abandonment. That holds whether the replacement is an Initial Snapshot restart before first publication or a Reconciliation Snapshot after an established baseline. Telling those cases apart belongs to `coordinate-sender-sync` and `begin-reconciliation-snapshot`, following ADR 0047 and 0048.
4. **R8's rule is kept by design.** Other units still never write the attempt tables directly. They call this module's primitive, which is what `@1`'s Decision 5 anticipated.
5. **The `@1` suites are re-bound.** The `@2` runner calls `@1`'s bound runner unchanged, then the new suite. Its binding includes `@1`'s bound files together with the new ones.

## Ground rules

These are `@1`'s ground rules, and the trusted boundary is unchanged.

The new suite injects database failures through a wrapping `SenderDatabase`, and drives the primitive inside transactions it opens itself.

Not mechanically provable here; the approver confirms this in implementation review: the implementation changes nothing in `@1`'s behavior or files beyond adding the primitive. It may refactor `beginInitialSnapshot` to use the primitive, as long as `@1`'s suites still pass.

## Freeze prerequisites

After Operator approval, the executable pack adds:
- the new TypeScript suite, about 14 tests;
- the Python contract suite, 2 tests;
- the runner, which calls `@1`'s runner and then verifies the new suite's fresh JUnit report;
- the stub of `commitSnapshotAttempt`.
