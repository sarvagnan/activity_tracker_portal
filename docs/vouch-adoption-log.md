# Vouch First-Project Adoption Log

This log records friction encountered while adopting Vouch mk2 in its first external project. Confirmed observations should be relayed to the Vouch project’s `bd4.4` work and used to test whether `vouch-context.md` plus `status` explain each unit’s purpose, dependencies, and blockers.

## 2026-08-17 — CI anchor sequencing is unclear before initialization

**Context**: The adoption guide says anchor mode `ci` means CI advances anchors after observing evidence. Vouch’s generated GitHub workflow verifies the vendored lock and runs `check`, but does not run `advance-anchor` or persist a changed store.

**Evidence**:

- `src/vouch_mk2/init.py` at commit `54260ae` generates only `self-check` and `check --ci --root .vouch` steps.
- `docs/quickstart.md` says CI is the intended anchor operator but explicitly states that `advance-anchor` remains an ordinary command and Vouch cannot prove who ran it.
- The generated workflow omits `--project .`, but this is not a defect: the CLI infers the parent project when the store root’s basename is `.vouch`.

**Friction**: A first adopter must invent how CI’s anchor mutation returns to the committed `.vouch` store, or knowingly advance it outside CI despite the selected mode. The generated workflow and operator documentation do not provide that operating sequence.

**2026-08-21 update**: The current Vouch implementation resolves the mechanics behind this observation. Its generated reusable workflow performs read-only validation with a narrowly scoped pending-anchor allowance, prepares and semantically validates an anchored candidate without write credentials, and uses a separate write-only publisher to push an exact `.vouch/**` bot commit. Standard-tier initialization now requires named project test jobs and prints the same-workflow caller that makes those jobs prerequisites. The resolution was committed locally as `68d76c1`; it is not yet present on GitHub or packaged as a release artifact.

## 2026-08-21 — Current mechanics pass, but the trust root is unreleased

**Context**: The current Vouch implementation is substantially newer than its GitHub remote. A read-only run of its full current pytest suite exited successfully, and the redesigned first-adopter CI path addresses the earlier anchor deadlock.

**Evidence**:

- Local `main` is 41 commits ahead of `origin/main`. The adoption closure is committed as `68d76c1`, and only Beads export metadata remains modified.
- The current product version remains `0.1.0`; there is no release tag, `LICENSE`, released `vouch.lock`, or `docs/adoption.md`.
- Vouch’s own decision sequence still places `release@1`, `adoption-docs@1`, and a real GitHub adoption probe after the completed mechanics.
- `build-lock` deterministically packages current source bytes but neither requires a clean tree nor binds those bytes to a commit or tag.

**Friction**: The Portal can pin commit `68d76c1` and exercise Vouch as a deliberately labelled alpha probe, but it cannot yet use a GitHub-reproducible release with a documented update and rollback path. The adoption decision is therefore “alpha now from an exact local commit” versus “wait for the first release,” not whether the core workflow exists.

**Decision**: Proceed with the alpha adoption probe from exact commit `68d76c1d1160ea6cfe3ab08982e881b55f26a829`. Record the generated lock digest when Vouch is initialized, and continue logging every first-adopter friction point rather than treating current mechanics as a released compatibility promise.

## 2026-08-24 — The candidate pin moved before project initialization

**Context**: The Portal had selected Vouch commit `68d76c1d1160ea6cfe3ab08982e881b55f26a829`, but had not yet built its lock or initialized a store. The Vouch remote has since advanced by seven commits to `75f70a52353a773ab896b79a26396735e4e5a152`, including CI-anchor, hermeticity, and adoption-closure repairs and an explicit gate requiring a real-project pilot before release.

**Evidence**:

- Local Vouch `main` and `origin/main` both resolve to `75f70a52353a773ab896b79a26396735e4e5a152`.
- The live checkout contains unrelated modified Beads and domain-document files, although its source, tests, and workflow are clean.
- Vouch's `build-lock` packages source bytes and does not make a dirty working tree equivalent to a clean commit checkout.

**Friction**: A first adopter making decisions while Vouch is still changing needs an explicit rule for when to refresh an unbuilt candidate pin. Building from the convenient live checkout would weaken the commit-level provenance the project intended. The Portal must either keep the earlier pin or deliberately replace it, and must build from an isolated clean tree at the selected commit.

**Decision**: Replace the unbuilt candidate with exact commit `75f70a52353a773ab896b79a26396735e4e5a152`, conditional on its full suite passing from an isolated clean checkout. Build and record the lock only from that clean tree.

## 2026-08-24 — Physical-device evidence has no typed or attached representation

**Context**: Some Sender behavior can only be qualified against Health Connect on a physical Android device, while GitHub-hosted CI will exercise synthetic and emulator-safe tests.

**Evidence**:

- `record-evidence` must spawn a command and records its rendered command line, exit-derived pass/fail outcome, timestamp, unit, chain parent, and acceptance test-set digest.
- The immutable evidence object does not contain command output, a report digest, screenshot, device identity, or evidence-file attachment.
- Vouch has no manual, device, inconclusive, or infrastructure-error evidence outcome. An unavailable or unauthorized device that exits nonzero is mechanically indistinguishable from a behavioral RED unless the project harness separates preflight.

**Friction**: A project can run an automated ADB or Gradle harness under `record-evidence`, but Vouch proves only the spawned command's exit status. It cannot durably bind a sanitized device report to that evidence or distinguish a test failure from a device-lab failure. The Portal must keep health data and device identifiers out of argv and output, use an independently successful preflight before a physical RED, and report this boundary honestly. Typed inconclusive outcomes and digest-bound evidence attachments are concrete Vouch improvement candidates.

**Decision**: GitHub-hosted CI will bind and execute synthetic contract evidence for every pinned type. A health-data-free automated local harness will qualify representative physical-device shapes and critical change/retraction behavior before native-adapter releases; Vouch will claim only its exit-derived result, and GitHub CI will not be described as reproducing the phone run. A self-hosted device runner is deferred until release frequency justifies it.

## 2026-08-24 — Mk2 no longer communicates Vouch's functional-block unit model

**Context**: While decomposing Portal delivery, the project initially interpreted a current mk2 unit as an independently acceptable behavioral slice. The Operator expected Vouch's earlier definition: a functional block, with or without side effects, whose concrete input and output behavior is contracted before implementation.

**Evidence**:

- The retained Vouch 0.6 kernel requires each unit to be a `transform`, `observation`, or `action`, with declared inputs, outputs, and named failures. Its unit-definition schema additionally records purpose, dependencies, authority, effects, retry, compensation, and acceptance behavior as applicable.
- Mk2's quickstart defines a unit only as "named work with a version." Its `Unit` state and `specify` command carry lifecycle identity, dependencies, pack bindings, and evidence state, but no purpose, functional class, input, output, error, authority, or effect contract.
- Mk2 decision D-8 deliberately dropped the 0.6 unit-definition fields during migration. D-15 parked the rich human system map and made legibility a project responsibility, with an explicit revisit trigger if a real adopter could not answer what a unit is for, what depends on it, or what blocks it.
- The current Vouch checkout's uncommitted `CONTEXT.md` additions discuss future governed scopes and incremental verification; they are not current unit mechanics and do not restore the functional-block definition.

**Friction**: The first external project reached D-15's trigger before defining its first unit: current operator documentation and generated context do not communicate the functional decomposition that the Operator reasonably associates with Vouch. Without external knowledge, an adopter can govern arbitrary work tickets as units and lose concrete input/output/effect boundaries while still satisfying mk2's lifecycle gates.

**Decision**: Follow the accepted target diagram rather than reinstating the 0.6 class taxonomy literally. A current mk2 unit will be the versioned lifecycle carrier for one project-owned Governed Scope contract. Each contract names cohesive behavior, concrete inputs, outputs, failures, observable effects, authority, dependencies, and acceptance oracle; it classifies the scope as leaf, shared, or integration and records pure, effectful, and volatile inputs as contract properties. Project tests validate the contract's shape and mk2 binds its bytes, while semantic adequacy remains Operator-reviewed. Feed the triggered D-15 observation back to Vouch rather than claiming mk2 currently enforces scope semantics.

## 2026-09-05 — Initialization is sound but assumes parent-directory write access

**Context**: After the Portal's architecture grill reached shared understanding, the project bootstrapped Vouch from exact commit `75f70a52353a773ab896b79a26396735e4e5a152`. A GitHub commit archive provided an isolated source tree rather than the dirty live checkout. Its 1,022 tests, strict Pyrefly check, both import-purity contracts, complete formal exploration, and mutation-witness discrimination passed. Two independent lock builds were byte-identical at SHA-256 `7923af176454c3a69975d16efc891f2c52b80465ba4890cb972a6b3ae42d3aac`.

**Evidence**:

- `python3 vouch.lock self-check --expect 7923af176454c3a69975d16efc891f2c52b80465ba4890cb972a6b3ae42d3aac` passed before initialization, and `self-check --expect @.vouch/config.json` passed afterward.
- Initializing `--tier standard --test-job test` produced a store already recording tier `standard`, anchor mode `ci`, and CI policy `strict`; the generated caller correctly makes the project `test` job a prerequisite.
- The first sandboxed attempt failed before publication with `Operation not permitted` for `/Users/sarvagnan/Documents/.vouch-init-*`. Repeating the same command with permission to stage beside the project succeeded.
- Initialization created `.vouch/.lock` but did not add `.vouch/.lock` to the adopter's `.gitignore`; the project added that rule explicitly before any commit.

**Friction**: Atomic initialization stages in the project directory's parent, so an agent allowed to write the target project but not its parent cannot initialize without broader filesystem authority. The failure is explicit and leaves no partial project publication, but neither `--project` nor the quickstart signals that parent access is required. A fresh adopter must also know to ignore the runtime lock separately even though Vouch otherwise emits the store it expects to be committed.

**Decision**: Keep the successfully initialized standard/CI store and generated reusable workflow. Treat parent-directory staging and the missing adopter `.gitignore` rule as first-project documentation and ergonomics findings for Vouch; do not weaken the atomic publication design merely to hide the requirement.

## 2026-10-12 — Binding shared runner configuration couples unrelated units

**Context**: Preparing the `ingest-protocol@1` pack binding for freeze. The pack runs Python and TypeScript tests through one runner script, and the Python side depends on a pinned `jsonschema` development dependency.

**Evidence**:

- `check` reports, as the residual of the acceptance-frozen-before-implementation control, that runner configuration such as `pyproject.toml` can deselect a bound test and advises binding those files too.
- `freeze` and `bind-pack` bind whole files, and `check --project .` fails when any bound file's bytes move.
- This pack's execution depends on repository-wide files, namely `pyproject.toml`, `uv.lock`, and `.node-version`, that every later unit will also change, for example when the Portal adds FastAPI.

**Friction**: Binding shared runner configuration makes every later dependency change look like moved goalposts for this unit, while leaving it unbound is exactly the residual Vouch warns about. Vouch offers no way to bind part of a file or to scope a binding to the dependencies one unit uses.

**Decision**: Proposed for the Operator's freeze approval: bind only unit-owned runner configuration, namely `tests/acceptance/run_ingest_protocol_1.sh`, which fixes the exact commands and runs `uv run --frozen`, and `sender/protocol/package.json`. Leave `pyproject.toml`, `uv.lock`, and `.node-version` unbound, and record that residual in the freeze proposal.

## 2026-10-12 — Strict-CI exceptions apply only after freeze

**Context**: Opening `operational-store@1`, the second unit, under the delivery flow that recorded its strict-CI exception at specification.

**Evidence**:

- `mark-open-in-ci` on a unit that is only `specified` fails with `ci-policy.exception-not-open: unit 'operational-store@1' is not post-specified and nonterminal`.
- `check --ci` passes with that unit specified and no exception: `CI policy strict enforced; all post-specified units are terminal or excepted`.

**Friction**: The rule is sound, since a specified unit has nothing frozen to keep open, but neither the command help nor the generated context says that exceptions begin at freeze, so the first written procedure placed the exception one step too early.

**Decision**: Record the exception at Gate B immediately after `freeze`, and update `docs/governance/delivery-flow.md` accordingly.

## 2026-10-13 — Semantic merge refused every incoming strict-CI exception

**Context**: Merging `main` into `project-source-record-1` for PR #8. The two branches had each frozen a unit and recorded its strict-CI exception, so their `.vouch` histories diverged.

**Evidence**:

- `merge-git-stages` failed with `event OpenInCiMarked name='operational-store@1' from revision 15 was refused by decide(): unit.unknown`.
- `OpenInCiMarked` and `OpenInCiCleared` record an exact `NAME@VERSION`, but semantic merge recovered their commands by passing that label as the bare unit name. Both `merge --other` and `merge-git-stages` were affected.
- The pinned tool offered no way through. Any divergent merge in which the incoming branch had frozen a unit under strict CI was refused.

**Friction**: Strict CI requires an exception for every frozen unit, and parallel branches each freeze units, so the first ordinary parallel merge hit the defect.

**Decision**: The Operator chose to fix Vouch first. vouch_mk2 delivered `merge-open-in-ci-replay@1` (D-25), its first unit found by this pilot. Replay resolves the unit by name and is accepted only when the result carries exactly the recorded reference; otherwise it is refused as `merge.conflict`, so an exception never moves to another version. Re-pinning `vouch.lock` is an enabling task (`activity_tracker_portal-ojm`), following the Operator's 2026-10-13 ruling that Vouch artifact upgrades follow ADR 0003's trust-root procedure. PR #8 merges with the fixed tool, keeping its history.

## 2026-10-13 — Evidence recorded after a unit's first anchor was never anchored

**Context**: Gate C for `operational-store@1` after PR #9 added refactor evidence #4–#6 for the approver's startup-refusal findings.

**Evidence**:

- CI on `main` after merging PR #9 pushed no anchor commit. Its anchor plan reported `changed=false`.
- The store's operational-store head was refactor #6, but its anchor was still refactor #3, claimed by run 38297556382 after PR #5.
- The generated anchor plan advances only evidence heads with no run claim. `check.evidence-anchors` reported PASS, because it requires only that a claimed anchor is in the chain, not that it reaches the head.
- Vouch's own repository avoids this by running `advance-anchor` locally after every evidence record, a step its generated workflow and documentation never ask adopters to take.

**Friction**: Unanchored evidence was reported as anchored, and the anchor job sat idle without failing, so nothing signalled the gap before Gate C.

**Decision**: The Operator chose to anchor now and fix Vouch in parallel. A local `advance-anchor` on `main` (`f3a7c5f`) let CI run 38357179289 claim the anchor at refactor #6 (`fb28913`). The delivery flow now runs `advance-anchor` after every `record-evidence`. The upstream fix, in which the check reports a head ahead of its anchor and the workflow advances it, is `vouch_mk2-bd4.4.4`.
