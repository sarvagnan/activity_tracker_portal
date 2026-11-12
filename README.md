# Activity Tracker Portal

**What it is.** A personal health data portal. It gathers one person's health and activity data from separate sources into one place they own and can inspect. Android Health Connect is the only source so far: a project-owned mobile Sender reads permitted Health Connect data and delivers it to a Portal on a Tailscale-reachable home server.

**Why it exists.** So that a person can inspect and analyse their own health data in a portal they own. It is a wellness and self-analysis tool, not a medical record system, diagnostic system, or medical device.

**How it was built.** Each piece is a [Vouch mk2](https://github.com/sarvagnan/vouch_mk2) unit: acceptance tests frozen first, implemented by GPT 6.1 Sol, approved by GPT 6 Astra, and anchored in CI. As of November 2026 the Sender-to-Portal delivery pipeline is published. Interpretation, summaries, the Portal pages, and export are not built yet; see [Development status](#development-status).

The system is local-first and single-person: the Subject and Operator are exactly the same person.

## Documentation authority

The documents have deliberately different jobs:

- [CONTEXT.md](CONTEXT.md) is the canonical domain glossary.
- [docs/adr/](docs/adr/) records accepted architectural decisions and their trade-offs.
- [docs/governance/scope-map.md](docs/governance/scope-map.md) is the normative functional decomposition used to define Vouch units.
- [docs/governance/delivery-flow.md](docs/governance/delivery-flow.md) defines the per-unit delivery procedure, its Operator gates, and the phase order; Beads holds live status.
- [architecture-plan.html](architecture-plan.html) is a readable, non-normative overview of the accepted architecture.
- [docs/vouch-adoption-log.md](docs/vouch-adoption-log.md) records first-project friction and limitations discovered while using Vouch.

When the overview and a normative document differ, follow the glossary, applicable ADR, and Governed Scope contract.

## Version 1

Version 1 will:

- run the authoritative Portal and SQLite Operational Store on one home server;
- treat Tailscale reachability as the initial trust mechanism for Sender ingress and browser access, while keeping that concern outside domain behavior;
- use an Expo/React Native Sender with a project-owned Kotlin Health Connect adapter, shipping for Android first;
- archive a complete Source Projection for every permitted health-and-fitness type exposed by the pinned stable Health Connect SDK, excluding the separate Medical Records/PHR subsystem;
- preserve Raw Records byte-for-byte while retained and keep source provenance and prior interpretations inspectable;
- process Steps, Heart Rate, Sleep Sessions, and Weight into conservative, evidence-bound summaries;
- render Overview, Records, Summaries, Sources, and Operations through a thin FastAPI and Jinja shell;
- run durable Processing, Summary Refresh, and Export Jobs through SQLite leases in a separately supervised worker; and
- keep Portable Exports, Operational Backups, and Diagnostic Bundles as three distinct contracts.

Manual health entry, generic file import, advanced cross-metric analysis, iOS delivery, Medical Records/PHR, and distributed task infrastructure are not part of the first release.

## Engineering governance

[Vouch mk2](https://github.com/sarvagnan/vouch_mk2) governs every forward behavioral or architectural change at the `standard` assurance tier with CI anchoring. Beads tracks tasks and dependencies; Vouch binds acceptance packs, evidence, lifecycle, and publication gates.

Each Vouch unit is the lifecycle carrier for one project-owned Governed Scope contract. The Operator approves what each unit does at draft review. Claude drafts packs and records Vouch transitions, GPT 6.1 Sol implements only against the frozen pack, and GPT 6 Astra, a separate approver agent, approves each freeze, implementation, merge, and publication. A mismatch becomes a recorded deviation rather than an unreviewed change to the goalposts.

[docs/governance/delivery-flow.md](docs/governance/delivery-flow.md) sequences the units in phases 0 to 9. Every governed unit in phases 0 to 3 is published; phase 4, reconciliation and interpretation, is next.

After Vouch initialization, read `.vouch/vouch-context.md` before planning or implementing governed work. The vendored `vouch.lock` is the project-pinned executable; no Vouch installation or network access is needed to use it.

## Development status

As of 2026-11-12, every governed unit in phases 0 to 3 is published:

- the Wire Protocol Version 1 contract, implemented in Python and TypeScript;
- the Source Record projection and the Operational Store through version 2;
- the Sender's observation, staging, synchronization, background polling, and Operator notifications, exercised against synthetic Health Connect data; and
- Sender installation and enrollment, Portal ingress, and receipt-bound delivery.

Not built yet: reconciliation and interpretation, summaries, completion and cutover, Portal presentation, and export, backup, and erasure (phases 4 to 8). The phase 3 local device smoke delivery, the first run with real Health Connect data, is still open.

`python3 vouch.lock status --root .vouch` lists the published units, and Beads holds live task status.

Physical-device findings are release-qualification evidence, not unresolved architecture: representative Health Connect record shapes, changes, retractions, background execution, interruption, and restart behavior must be exercised before a native-adapter release.
