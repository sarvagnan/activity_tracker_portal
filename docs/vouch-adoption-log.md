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
