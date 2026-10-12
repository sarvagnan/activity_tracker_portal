# Personal Health Data Portal

This context describes a local-first, single-person system for inspecting personal activity, body, and food data. It supports wellness and self-analysis, not diagnosis or medical decision-making.

## Language

**Portal**:
The product that collects one person's health and activity data and presents it with inspectable origin and history.
_Avoid_: Daybook, dashboard, medical record system

**Subject**:
The single person whose health and activity data the Portal holds. The Subject and Operator are always the same person.
_Avoid_: Patient, profile, account holder

**Operator**:
The person who controls the Portal installation and its data. The Operator is always the Subject.
_Avoid_: Administrator, user, account

**Device**:
Hardware that records data for the Subject or transfers that data to the Portal; it is never a person or account.
_Avoid_: User, client

**Raw Record**:
A Source Projection preserved byte-for-byte while retained, so its accessible origin and interpretation can be inspected and replayed.
_Avoid_: Immutable record, source truth

**Source Projection**:
A complete, versioned representation of the fields and metadata exposed by a source for one record; it does not claim access to a source's private internal bytes.
_Avoid_: Serialization, canonical record

**Projection Refusal**:
A deterministic inability to represent every record on one source page under the Sender's named projection behavior and schema. It preserves the page position and blocks only that record type until governed behavior changes or the Operator requests one retry.
_Avoid_: Quarantined Record, transient read failure, Delivery Block

**Canonical Revision**:
An immutable, processor-versioned interpretation of a Raw Record, consisting of a shared identity, provenance, and temporal header plus a versioned record-type-specific body. Current views select one active revision, while earlier interpretations remain inspectable for as long as the Raw Record is retained.
_Avoid_: Normalized row, overwritten record, current truth

**Source Selection Policy**:
A versioned, optionally time-bounded rule naming which origins may contribute to a particular derived measure and record type. When overlapping origins exist without an applicable rule, the aggregate is unresolved rather than silently double-counted.
_Avoid_: Deduplication setting, source priority, preferred device

**Fallback Timezone Policy**:
An effective-dated rule naming the Subject's IANA timezone to infer calendar attribution only when source evidence lacks an experienced offset.
_Avoid_: Server timezone, display timezone, current location

**Ingest Batch**:
A bounded, ordered delivery fragment containing source records from one source page and record type. One source page may require several numbered Ingest Batches, but one record is never split between them.
_Avoid_: Upload, sync job

**Outbox Message**:
An encrypted, byte-stable Sender delivery retained until its matching Delivery Receipt permits removal. A message carries one Ingest Batch, Snapshot Completion, Change Completion, Sync Checkpoint, or capability acknowledgement.
_Avoid_: Queue item, cache, pending upload

**Sender**:
The project-owned participant that delivers a Device's source records to the Portal and receives acceptance of each delivery. It is not the authoritative store.
_Avoid_: Client, Android app

**Sender Installation**:
One enrolled operational identity of the Sender. Reinstalling or replacing its Device creates a different Sender Installation.
_Avoid_: Device, account, session

**Sender Synchronization**:
The eventual automatic flow that observes permitted source records, makes delivery messages durable in the Sender outbox, and delivers Ingest Batches, completion evidence, and health-data-free polling status to the Portal.
_Avoid_: Portable Export, Operational Backup, file export

**Polling Run**:
One identified Sender execution of coalesced polling intent across the record types then eligible to be checked. It may finish normally or be closed as interrupted after recovery.
_Avoid_: Change Cycle, synchronization guarantee, delivery deadline

**Initial Snapshot**:
A per-record-type baseline traversal of all source history available to one newly enrolled Sender Installation. It is complete only when the Portal durably accepts explicit completion evidence after every preceding page for that type and its catch-up Change Cycle; its publication behavior differs when replacing an established Sender.
_Avoid_: Operational Backup, Portable Export, complete source history

**Replacement Baseline**:
The completed Initial Snapshot and catch-up Change Cycle of a new Sender Installation when the Portal already has established current results. Previous results remain current while it is built, and absence from the new installation does not prove that earlier source evidence was retracted.
_Avoid_: Reconciliation Snapshot, first publication, Sender restore

**Reconciliation Snapshot**:
A full per-record-type traversal begun after continuity with the source change stream is lost. It reserves a new changes token before its first page and is complete only after the Portal accepts its Snapshot Completion and the Change Completion for the catch-up cycle from that token. Previously current results remain visible with a reconciliation-pending condition while new evidence is matched by Source Identity without erasing retained evidence.
_Avoid_: Initial Snapshot, Change Cycle, replacement import

**Abandoned Attempt Evidence**:
Retained Raw Records and related processing evidence from an incomplete snapshot attempt that has been superseded by a new identity. It remains inspectable with its abandoned condition but cannot become current source evidence or contribute to a Health Summary unless the source record is independently reacquired through a completed attempt.
_Avoid_: Quarantined Record, deleted record, current evidence

**Snapshot Completion**:
A receipt-bound control message proving that the Portal has durably archived the complete ordered page chain claimed for one record type's explicitly identified Initial Snapshot or Reconciliation Snapshot. It carries the snapshot kind but no source cursor or health value. Initial publication or reconciliation activation also requires the subsequent catch-up Change Cycle.
_Avoid_: Ingest Batch, Delivery Receipt, Data Watermark

**Change Cycle**:
One per-record-type traversal of changes from a durably held source token until the source reports that no page remains. The first cycle after an Initial Snapshot catches changes that occurred during the snapshot.
_Avoid_: Initial Snapshot, polling attempt, Ingest Batch

**Change Completion**:
A receipt-bound control message proving that the Portal has durably archived the complete ordered page chain claimed for one Change Cycle. It contains no source token or health value.
_Avoid_: Snapshot Completion, Ingest Batch, Delivery Receipt

**Sync Checkpoint**:
A receipt-bound, health-data-free report closing one Polling Run, including its run sequence, trigger, timing, Background Scheduling Status, each record type's lifecycle outcome and allowlisted reason, whether source data was staged, and pre-checkpoint outbox counts without source-record counts, source tokens, Source Identities, health dates, or health values. Lifecycle outcomes are not-due, no-change, progressed, completed, deferred, blocked, not-permitted, failed, or interrupted. Portal receipt time is authoritative for Polling Freshness; Sender times and elapsed duration are diagnostic claims.
_Avoid_: Change Completion, Delivery Receipt, Data Watermark

**Polling Freshness**:
The operational age of the active Sender Installation's latest completed and accepted Polling Run, measured from its Sync Checkpoint's Portal receipt time. It is never-reported before that installation has a checkpoint; otherwise, for an hourly schedule, it is current through six hours, delayed after six hours, and stale after 24 hours. It never changes retained health evidence or summary validity.
_Avoid_: Last Delivery, Data Watermark, measurement recency

**Last Delivery**:
The Portal receipt time of the active Sender Installation's most recently accepted Outbox Message, proving recent communication but not a completed Polling Run, current source evidence, or recent measurement.
_Avoid_: Polling Freshness, Data Watermark, measurement time

**Background Scheduling Status**:
A health-data-free Sender observation, reported for a particular Polling Run, of whether background scheduling was then registered and available. Its receipt time remains attached because it is historical evidence rather than a guarantee of the Device's current configuration.
_Avoid_: Current scheduler state, polling guarantee

**Source Identity**:
The identity by which an origin distinguishes one source record from another and communicates its later changes or retraction.
_Avoid_: Portal ID, checksum

**Source Record**:
A continuing source assertion identified by source-system kind, origin, record type, and Source Identity independently of which Sender Installation observed it. Its successive Source Projections, retractions, interpretations, Amendments, and observing-Sender provenance belong to that identity.
_Avoid_: Raw Record, Canonical Revision, data point

**Source Conflict**:
Different source evidence for one Source Record carrying the same source modification time, so neither version can be ordered as newer.
_Avoid_: Duplicate, Quarantined Record, merge conflict

**Duplicate Group**:
A reversible derived association among eligible observations from any origins selected for a calculation that contribute as one because their relevant instants and values are exactly equal, while preserving every contributor and the grouping rationale. It is recalculated whenever eligibility or source evidence changes.
_Avoid_: Canonical merge, Source Record, Source Conflict

**Delivery Receipt**:
The Portal's durable acknowledgement that a specific Outbox Message was accepted, allowing the Sender to stop retrying it.
_Avoid_: HTTP response, success flag

**Delivery Block**:
An Operator-action-required outcome that keeps the oldest Outbox Message intact and prevents later messages from being delivered until the condition is resolved.
_Avoid_: Dropped batch, permanent failure, retry

**Wire Protocol Version**:
The versioned contract governing enrollment, delivery, rejection, and receipt messages between the Sender and Portal. It is independent of a record type's Source Projection schema version.
_Avoid_: API version, app version, schema version

**Authorized Ingress**:
The condition that a delivery is permitted to invoke Portal ingestion by the currently configured reachability or authentication mechanism; it does not itself mean that the delivery was accepted.
_Avoid_: User session, account authentication, delivery success

**Source Retraction**:
Notice that a previously received source record is no longer current. It withdraws that record's contribution without itself destroying the retained Raw Record.
_Avoid_: Deletion, erasure

**Reconciliation Absence**:
Evidence that a previously current Source Record was absent from a completed Reconciliation Snapshot and its catch-up cycle under the same Sender Installation and unchanged full-history, record-type, and origin coverage. It withdraws the prior contribution without claiming that the source emitted a Source Retraction or erasing retained evidence.
_Avoid_: Source Retraction, privacy deletion, missing permission

**Reconciliation Failure**:
A retained failed activation of a completed Reconciliation Snapshot caused by unproven integrity, coverage, or completeness. The established view remains current with a persistent reconciliation-failed condition until governed reconciliation succeeds or the affected retained data is explicitly deleted.
_Avoid_: Failed Health Summary, dismissed warning, forced cutover

**Amendment**:
An append-only Operator-authored note, label, exclusion, or re-inclusion that adjusts how retained data is presented or interpreted without altering its source evidence. Later Amendments may supersede earlier ones, but do not overwrite them.
_Avoid_: Edit, overwrite, correction

**Operator Record**:
An original health or activity assertion entered directly by the Operator and attributed to an explicit Operator-authored source. It is not an Amendment to someone else's source evidence.
_Avoid_: Manual amendment, fabricated Health Connect record

**Quarantined Record**:
A retained Raw Record excluded from canonical and derived views because its Source Projection cannot be interpreted safely, together with a health-data-free reason.
_Avoid_: Rejected record, invalid data

**Unmodeled Record**:
A retained Raw Record whose Source Projection is understood but has no canonical interpretation yet.
_Avoid_: Unsupported record, quarantined record

**Policy-Pending Record**:
A retained Raw Record whose supported interpretation requires an Operator policy that has not yet been defined.
_Avoid_: Unmodeled Record, Quarantined Record, rejected record

**Processing Job**:
A durable claim that retained source data still needs interpretation under a named processor version.
_Avoid_: Task, background callback

**Processor Generation**:
An immutable manifest mapping every supported record type to the processor version active for ordinary Portal views. A new generation may reuse unchanged processor versions, but activates only as one coherent manifest after complete candidate processing and validation.
_Avoid_: Deployment, migration, individual Canonical Revision

**Cutover Target**:
The immutable evidence boundary, synchronization attempt, policy set, and intended activation selected when cutover work begins. Later completed evidence remains outside that target and pending for ordinary publication; a target may be superseded by a fresh governed attempt or invalidated by intersecting privacy deletion, but never edited.
_Avoid_: Candidate Build, current view, quiet period

**Candidate Build**:
An append-only, unpublished set of source selections, Canonical Revisions, and Health Summaries produced for one Cutover Target under named processor versions. A corrected retry creates a successor build rather than changing or reactivating a failed or rejected build.
_Avoid_: Cutover Target, current view, mutable retry

**Compound Cutover**:
The exceptional atomic activation of a corrected reconciliation Candidate Build together with the successor Processor Generation required to make its processor versions current. It does not combine unrelated processor upgrades or policy changes.
_Avoid_: Routine processor upgrade, chained activation

**Activation Approval**:
A one-use, health-data-free Operator decision bound to one immutable Cutover Target, Candidate Build, proposed Processor Generation, and outcome-transition report. It authorizes completed valid but unexpected transitions, never missing, failed, invalid, or incomplete work.
_Avoid_: Force activation, generic confirmation, processor policy

**Cutover Record**:
A health-data-free account of a Processor Generation activation, policy activation, first publication, Reconciliation Snapshot activation, or Replacement Baseline activation, including its time, reason, version selectors, and outcome counts without source evidence.
_Avoid_: View Generation, deployment, database transaction

**Health Summary**:
A rebuildable derived result for a covered interval that carries its primary state, evidence conditions, applicable policies, Processor Generation, Data Watermark, and contributing evidence. Each published result is an immutable revision; one is current while earlier published revisions remain inspectable for as long as their contributing evidence is retained.
_Avoid_: Health fact, source record, authoritative total

**Data Watermark**:
The exact accepted and processed delivery position incorporated into a displayed result, with a digest of its evidence set. Its human presentation uses receipt and processing times; it does not claim that the source has no later or unavailable evidence.
_Avoid_: Measurement time, Initial Snapshot completion, complete coverage

**Summary Refresh Job**:
A durable claim that one affected Health Summary needs recalculation and conditional publication under a named Processor Generation and policy state.
_Avoid_: Processing Job, Health Summary, background callback

**Export Job**:
A durable request, bound at creation to exact package and record-schema versions, to produce one Portable Export from a consistent Portal snapshot independently of the requesting browser connection.
_Avoid_: Download request, Processing Job, background callback

**Deletion Receipt**:
A health-data-free record of a deletion's scope, time, affected record counts, and backup-expiry deadline.
_Avoid_: Tombstone, deleted record

**Deletion Ledger**:
The health-data-free sequence of deletion instructions required to prevent an older restored snapshot from resurrecting erased records.
_Avoid_: Audit log, backup

**Portable Export**:
A versioned, database-independent package of retained health data, provenance, Amendments, and interpretation policies intended for use outside the Portal, but not a supported Portal restoration input. Its logical identity comes from its canonical uncompressed files and manifest checksums rather than its compressed container bytes.
_Avoid_: Backup, database dump, restore archive

**Package ID**:
The SHA-256 identity of a Portable Export's canonical manifest bytes, whose inventory covers every other included file. It represents the complete logical package independently of compressed container bytes or filename.
_Avoid_: ZIP checksum, artifact filename, database identity

**Operational Backup**:
A restorable snapshot of the Portal's operational state, including its SQLite database and applicable Deletion Ledger, managed under the backup retention policy.
_Avoid_: Export, archive

**Diagnostic Bundle**:
A health-data-free package of explicitly allowed versions, counts, states, randomized aliases, and curated failure codes used to troubleshoot either the Portal or Sender.
_Avoid_: Logs, export, crash dump
