# Activity Tracker Portal

A local-first, single-person system for collecting and inspecting personal health and activity evidence. A project-owned mobile Sender reads permitted Health Connect data and delivers it to a Portal on a Tailscale-reachable home server. The Subject and Operator are exactly the same person.

The project has completed architectural design and is entering governed implementation. It is a wellness and self-analysis tool, not a medical record system, diagnostic system, or medical device.

## Documentation authority

The documents have deliberately different jobs:

- [CONTEXT.md](CONTEXT.md) is the canonical domain glossary.
- [docs/adr/](docs/adr/) records accepted architectural decisions and their trade-offs.
- [docs/governance/scope-map.md](docs/governance/scope-map.md) is the normative functional decomposition used to define Vouch units.
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

Each Vouch unit is the lifecycle carrier for one project-owned Governed Scope contract. The Operator approves an acceptance pack before it is frozen; Claude implements only against the frozen pack. A mismatch becomes a recorded deviation rather than an unreviewed change to the goalposts.

The initial governed sequence is:

1. `ingest-protocol@1`
2. `project-source-record@1`
3. `operational-store@1`
4. `receipt-bound-delivery@1`

After Vouch initialization, read `.vouch/vouch-context.md` before planning or implementing governed work. The vendored `vouch.lock` is the project-pinned executable; no Vouch installation or network access is needed to use it.

## Development status

The repository currently contains the accepted design and initial package skeleton. Physical-device findings are release-qualification evidence, not unresolved architecture: representative Health Connect record shapes, changes, retractions, background execution, interruption, and restart behavior must be exercised before a native-adapter release.
