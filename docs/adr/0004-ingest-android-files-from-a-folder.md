---
status: superseded by ADR-0006
---

# Ingest Android batches through a folder

Android will push completed Ingest Batch files into a configured home-server folder using deployment-provided transfer tooling, and the Portal will pull files from that folder for archival and processing. The Portal does not expose a network write API to the Android companion and does not own the transport that makes files arrive. This isolates acquisition from transfer credentials and lets the application process the same file contract regardless of how the deployment synchronizes it.
