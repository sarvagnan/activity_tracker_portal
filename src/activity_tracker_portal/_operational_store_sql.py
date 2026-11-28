"""Exact baseline migration for the ingest-side Operational Store."""

from __future__ import annotations

_BASELINE_SQL = """
CREATE TABLE store_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE TABLE enrollment_offers (
    code_digest TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
) STRICT;
CREATE TABLE installations (
    installation_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'active', 'draining', 'retired', 'withdrawn')),
    enrolled_at TEXT NOT NULL,
    activated_at TEXT,
    draining_at TEXT,
    retired_at TEXT,
    retirement_kind TEXT CHECK (retirement_kind IN ('drained', 'destructive')),
    withdrawn_at TEXT,
    highest_sequence INTEGER,
    last_delivery_at TEXT,
    latest_checkpoint_receipt_at TEXT,
    background_scheduling TEXT,
    background_scheduling_receipt_at TEXT
) STRICT;
CREATE UNIQUE INDEX one_delivering_installation ON installations ((1))
    WHERE state IN ('active', 'draining');
CREATE TABLE installation_events (
    event_id INTEGER PRIMARY KEY,
    installation_id TEXT NOT NULL REFERENCES installations (installation_id),
    state TEXT NOT NULL CHECK (state IN ('pending', 'active', 'draining', 'retired', 'withdrawn')),
    at TEXT NOT NULL,
    retirement_kind TEXT CHECK (retirement_kind IN ('drained', 'destructive'))
) STRICT;
CREATE INDEX installation_event_order ON installation_events (installation_id, event_id);
CREATE TABLE generations (
    generation INTEGER PRIMARY KEY,
    advertisement BLOB NOT NULL,
    honored_at TEXT NOT NULL,
    stopped_at TEXT
) STRICT;
CREATE TABLE archived_messages (
    installation_id TEXT NOT NULL REFERENCES installations (installation_id),
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('ingest-batch', 'snapshot-completion', 'change-completion',
                                     'sync-checkpoint', 'capability-acknowledgement')),
    message_sha256 TEXT NOT NULL,
    staged_generation INTEGER NOT NULL REFERENCES generations (generation),
    receipt_time TEXT NOT NULL,
    envelope BLOB NOT NULL,
    receipt BLOB NOT NULL,
    PRIMARY KEY (installation_id, sequence)
) STRICT;
CREATE INDEX acknowledged_generations ON archived_messages (staged_generation)
    WHERE kind = 'capability-acknowledgement';
CREATE TABLE ingest_batches (
    installation_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    record_type TEXT NOT NULL,
    projection_schema_version INTEGER NOT NULL,
    cycle_kind TEXT NOT NULL CHECK (cycle_kind IN ('initial-snapshot', 'reconciliation-snapshot', 'change-cycle')),
    cycle_id TEXT NOT NULL,
    source_page_index INTEGER NOT NULL,
    fragment_index INTEGER NOT NULL,
    previous_chain TEXT NOT NULL,
    PRIMARY KEY (installation_id, sequence),
    FOREIGN KEY (installation_id, sequence) REFERENCES archived_messages (installation_id, sequence)
) STRICT;
CREATE TABLE batch_items (
    installation_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    item_index INTEGER NOT NULL,
    projection BLOB,
    retracted_identity TEXT,
    CHECK ((projection IS NULL) != (retracted_identity IS NULL)),
    PRIMARY KEY (installation_id, sequence, item_index),
    FOREIGN KEY (installation_id, sequence) REFERENCES ingest_batches (installation_id, sequence)
) STRICT;
CREATE TABLE completions (
    installation_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    completion_kind TEXT NOT NULL CHECK (completion_kind IN ('snapshot', 'change')),
    snapshot_kind TEXT CHECK (snapshot_kind IN ('initial', 'reconciliation')),
    record_type TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    fragment_count INTEGER NOT NULL,
    record_count INTEGER NOT NULL,
    final_chain TEXT NOT NULL,
    CHECK ((completion_kind = 'snapshot') = (snapshot_kind IS NOT NULL)),
    PRIMARY KEY (installation_id, sequence),
    UNIQUE (installation_id, cycle_id),
    FOREIGN KEY (installation_id, sequence) REFERENCES archived_messages (installation_id, sequence)
) STRICT;
CREATE TABLE sync_checkpoints (
    installation_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    run_sequence INTEGER NOT NULL,
    trigger TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    elapsed_ms INTEGER,
    background_scheduling TEXT NOT NULL,
    outbox_messages_before_checkpoint INTEGER NOT NULL,
    outbox_bytes_before_checkpoint INTEGER NOT NULL,
    PRIMARY KEY (installation_id, sequence),
    FOREIGN KEY (installation_id, sequence) REFERENCES archived_messages (installation_id, sequence)
) STRICT;
CREATE TABLE checkpoint_outcomes (
    installation_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    outcome_index INTEGER NOT NULL,
    record_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT,
    source_data_staged INTEGER NOT NULL CHECK (source_data_staged IN (0, 1)),
    PRIMARY KEY (installation_id, sequence, outcome_index),
    FOREIGN KEY (installation_id, sequence) REFERENCES sync_checkpoints (installation_id, sequence)
) STRICT;
CREATE TABLE sequence_gaps (
    installation_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    gap_after INTEGER NOT NULL,
    detected_at TEXT NOT NULL,
    PRIMARY KEY (installation_id, sequence),
    FOREIGN KEY (installation_id, sequence) REFERENCES archived_messages (installation_id, sequence)
) STRICT;
CREATE TABLE delivery_blocks (
    block_id INTEGER PRIMARY KEY,
    installation_id TEXT,
    sequence INTEGER,
    message_sha256 TEXT NOT NULL,
    failure TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    count INTEGER NOT NULL CHECK (count > 0)
) STRICT;
CREATE UNIQUE INDEX delivery_block_identity ON delivery_blocks (
    (installation_id IS NULL), ifnull(installation_id, ''),
    (sequence IS NULL), ifnull(sequence, 0), message_sha256, failure
);
"""

_SOURCE_INDEX_SQL = """
CREATE TABLE source_records (
    record_id INTEGER PRIMARY KEY,
    source_system_kind TEXT NOT NULL,
    origin TEXT NOT NULL,
    record_type TEXT NOT NULL,
    source_identity TEXT NOT NULL,
    UNIQUE (source_system_kind, origin, record_type, source_identity)
) STRICT;
CREATE TABLE source_evidence (
    evidence_id INTEGER PRIMARY KEY,
    installation_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    item_index INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('projection', 'retraction')),
    record_id INTEGER REFERENCES source_records (record_id),
    record_type TEXT NOT NULL,
    source_identity TEXT NOT NULL,
    last_modified_time TEXT,
    projection_sha256 TEXT,
    indexed_at TEXT NOT NULL,
    CHECK ((kind = 'projection' AND record_id IS NOT NULL AND last_modified_time IS NOT NULL
            AND projection_sha256 IS NOT NULL)
        OR (kind = 'retraction' AND record_id IS NULL AND last_modified_time IS NULL
            AND projection_sha256 IS NULL)),
    UNIQUE (installation_id, sequence, item_index),
    FOREIGN KEY (installation_id, sequence, item_index)
        REFERENCES batch_items (installation_id, sequence, item_index)
) STRICT;
CREATE INDEX source_projection_evidence ON source_evidence (record_id, evidence_id);
CREATE INDEX source_retraction_evidence ON source_evidence (record_type, source_identity, evidence_id)
    WHERE kind = 'retraction';
CREATE TABLE source_selections (
    selection_id INTEGER PRIMARY KEY,
    record_id INTEGER NOT NULL REFERENCES source_records (record_id),
    outcome TEXT NOT NULL CHECK (outcome IN ('projection', 'retraction', 'conflict')),
    evidence_head INTEGER NOT NULL REFERENCES source_evidence (evidence_id),
    recorded_at TEXT NOT NULL
) STRICT;
CREATE INDEX source_selection_order ON source_selections (record_id, selection_id);
CREATE TABLE selection_evidence (
    selection_id INTEGER NOT NULL REFERENCES source_selections (selection_id),
    evidence_index INTEGER NOT NULL,
    evidence_id INTEGER NOT NULL REFERENCES source_evidence (evidence_id),
    PRIMARY KEY (selection_id, evidence_index),
    UNIQUE (selection_id, evidence_id)
) STRICT;
"""

_PROCESSING_SQL = """
CREATE TABLE processing_jobs (
    job_id INTEGER PRIMARY KEY,
    record_id INTEGER NOT NULL REFERENCES source_records (record_id),
    processor_version TEXT NOT NULL,
    selection_id INTEGER NOT NULL REFERENCES source_selections (selection_id),
    state TEXT NOT NULL CHECK (state IN ('queued', 'leased', 'completed', 'failed', 'superseded')),
    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 100),
    attempts INTEGER NOT NULL DEFAULT 0,
    not_before TEXT NOT NULL,
    fence INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_at TEXT,
    failure TEXT,
    enqueued_at TEXT NOT NULL,
    closed_at TEXT
) STRICT;
CREATE UNIQUE INDEX processing_open_job ON processing_jobs (record_id, processor_version)
    WHERE state IN ('queued', 'leased');
CREATE INDEX processing_job_record ON processing_jobs (record_id, job_id);
CREATE INDEX processing_job_ready ON processing_jobs (state, not_before, job_id);
CREATE INDEX processing_job_expiry ON processing_jobs (state, lease_expires_at, job_id);
CREATE INDEX processing_job_failed ON processing_jobs (record_id, processor_version, selection_id)
    WHERE state = 'failed';
CREATE TABLE processing_attempts (
    job_id INTEGER NOT NULL REFERENCES processing_jobs (job_id),
    fence INTEGER NOT NULL,
    owner TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    finished_at TEXT,
    result TEXT CHECK (result IN ('completed', 'transient-failure', 'deterministic-failure', 'lease-expired')),
    reason TEXT,
    PRIMARY KEY (job_id, fence)
) STRICT;
CREATE TABLE processing_outcomes (
    outcome_id INTEGER PRIMARY KEY,
    record_id INTEGER NOT NULL REFERENCES source_records (record_id),
    selection_id INTEGER NOT NULL REFERENCES source_selections (selection_id),
    evidence_id INTEGER NOT NULL REFERENCES source_evidence (evidence_id),
    processor_version TEXT NOT NULL,
    policy_identity TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('revision', 'unmodeled', 'policy-pending', 'quarantine')),
    reason TEXT,
    pointer TEXT,
    policy_kind TEXT,
    job_id INTEGER NOT NULL,
    fence INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (selection_id, processor_version, policy_identity),
    FOREIGN KEY (job_id, fence) REFERENCES processing_attempts (job_id, fence)
) STRICT;
CREATE INDEX processing_outcome_record ON processing_outcomes (record_id, outcome_id);
CREATE INDEX processing_outcome_kind ON processing_outcomes (kind);
CREATE TABLE canonical_revisions (
    outcome_id INTEGER PRIMARY KEY REFERENCES processing_outcomes (outcome_id),
    header_bytes BLOB NOT NULL,
    body_bytes BLOB NOT NULL,
    body_version INTEGER NOT NULL CHECK (body_version >= 1),
    start_epoch_ns INTEGER NOT NULL,
    end_epoch_ns INTEGER,
    start_local_date TEXT NOT NULL,
    end_local_date TEXT,
    start_attribution TEXT NOT NULL CHECK (start_attribution IN ('experienced', 'inferred')),
    end_attribution TEXT CHECK (end_attribution IN ('experienced', 'inferred')),
    header_sha256 TEXT NOT NULL,
    body_sha256 TEXT NOT NULL
) STRICT;
CREATE INDEX canonical_revision_instants ON canonical_revisions (start_epoch_ns, end_epoch_ns);
CREATE INDEX canonical_revision_dates ON canonical_revisions (start_local_date, end_local_date);
CREATE TABLE item_quarantines (
    installation_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    item_index INTEGER NOT NULL,
    reason TEXT NOT NULL,
    pointer TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (installation_id, sequence, item_index),
    FOREIGN KEY (installation_id, sequence, item_index)
        REFERENCES batch_items (installation_id, sequence, item_index)
) STRICT;
CREATE INDEX processing_record_type ON source_records (record_type, record_id);
CREATE INDEX processing_latest_selection ON source_selections (record_id, selection_id, outcome);
"""


_POLICY_SQL = """
CREATE TABLE policies (
    policy_id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('fallback-timezone', 'source-selection')),
    requested_at TEXT NOT NULL
) STRICT;
CREATE TABLE fallback_timezone_policies (
    policy_id INTEGER PRIMARY KEY REFERENCES policies (policy_id),
    zone TEXT NOT NULL,
    effective_from TEXT
) STRICT;
CREATE INDEX fallback_timezone_start ON fallback_timezone_policies (effective_from, policy_id);
CREATE TABLE source_selection_policies (
    policy_id INTEGER PRIMARY KEY REFERENCES policies (policy_id),
    measure TEXT NOT NULL,
    record_type TEXT NOT NULL,
    first_date TEXT,
    last_date TEXT,
    CHECK (first_date IS NULL OR last_date IS NULL OR first_date <= last_date)
) STRICT;
CREATE INDEX source_selection_scope ON source_selection_policies (measure, record_type, first_date, last_date);
CREATE TABLE policy_origins (
    policy_id INTEGER NOT NULL REFERENCES source_selection_policies (policy_id),
    source_system_kind TEXT NOT NULL,
    origin TEXT NOT NULL,
    PRIMARY KEY (policy_id, source_system_kind, origin)
) STRICT;
CREATE TABLE policy_supersessions (
    old_id INTEGER PRIMARY KEY REFERENCES policies (policy_id),
    new_id INTEGER NOT NULL REFERENCES policies (policy_id),
    CHECK (old_id < new_id)
) STRICT;
CREATE INDEX policy_supersession_history ON policy_supersessions (new_id, old_id);
CREATE TABLE policy_lease_marks (
    job_id INTEGER NOT NULL,
    fence INTEGER NOT NULL,
    PRIMARY KEY (job_id, fence),
    FOREIGN KEY (job_id, fence) REFERENCES processing_attempts (job_id, fence)
) STRICT;
CREATE INDEX known_origin_cover ON source_records (record_type, source_system_kind, origin);
"""


_SUMMARY_SQL = """
CREATE INDEX summary_retraction_identity ON source_records (record_type, source_identity, record_id);
CREATE TABLE processor_generations (
    generation_id INTEGER PRIMARY KEY,
    manifest_sha256 TEXT NOT NULL UNIQUE,
    registered_at TEXT NOT NULL
) STRICT;
CREATE TABLE processor_manifest (
    generation_id INTEGER NOT NULL REFERENCES processor_generations (generation_id),
    record_type TEXT NOT NULL,
    processor_version TEXT NOT NULL,
    PRIMARY KEY (generation_id, record_type)
) STRICT;
CREATE TABLE summary_tokens (
    work_id INTEGER PRIMARY KEY,
    measure TEXT NOT NULL,
    record_type TEXT NOT NULL,
    local_date TEXT NOT NULL,
    generation_id INTEGER NOT NULL REFERENCES processor_generations (generation_id),
    policy_horizon INTEGER NOT NULL CHECK (policy_horizon >= 0),
    token INTEGER NOT NULL CHECK (token > 0),
    UNIQUE (measure, record_type, local_date, generation_id, policy_horizon)
) STRICT;
CREATE TABLE summary_refresh_jobs (
    job_id INTEGER PRIMARY KEY,
    work_id INTEGER NOT NULL REFERENCES summary_tokens (work_id),
    state TEXT NOT NULL CHECK (state IN ('queued', 'leased', 'completed', 'failed')),
    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 100),
    attempts INTEGER NOT NULL DEFAULT 0,
    not_before TEXT NOT NULL,
    fence INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_at TEXT,
    failure TEXT,
    enqueued_at TEXT NOT NULL,
    closed_at TEXT
) STRICT;
CREATE UNIQUE INDEX summary_open_job ON summary_refresh_jobs (work_id)
    WHERE state IN ('queued', 'leased');
CREATE INDEX summary_job_history ON summary_refresh_jobs (work_id, job_id);
CREATE INDEX summary_job_ready ON summary_refresh_jobs (state, not_before, job_id);
CREATE INDEX summary_job_expiry ON summary_refresh_jobs (state, lease_expires_at, job_id);
CREATE TABLE summary_refresh_attempts (
    job_id INTEGER NOT NULL REFERENCES summary_refresh_jobs (job_id),
    fence INTEGER NOT NULL,
    owner TEXT NOT NULL,
    token INTEGER NOT NULL,
    claimed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    finished_at TEXT,
    result TEXT CHECK (result IN ('completed', 'stale', 'transient-failure', 'deterministic-failure', 'lease-expired')),
    reason TEXT,
    PRIMARY KEY (job_id, fence)
) STRICT;
CREATE TABLE health_summaries (
    summary_id INTEGER PRIMARY KEY,
    work_id INTEGER NOT NULL REFERENCES summary_tokens (work_id),
    token INTEGER NOT NULL,
    header_bytes BLOB NOT NULL,
    evidence_bytes BLOB NOT NULL,
    body_bytes BLOB,
    contract_version TEXT NOT NULL,
    derivation_version TEXT NOT NULL,
    processor_version TEXT NOT NULL,
    source_selection_policy INTEGER REFERENCES source_selection_policies (policy_id),
    primary_state TEXT NOT NULL,
    summary_sha256 TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    body_sha256 TEXT,
    job_id INTEGER NOT NULL,
    fence INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (work_id, token),
    FOREIGN KEY (job_id, fence) REFERENCES summary_refresh_attempts (job_id, fence)
) STRICT;
CREATE TABLE summary_conditions (
    summary_id INTEGER NOT NULL REFERENCES health_summaries (summary_id),
    condition TEXT NOT NULL,
    PRIMARY KEY (summary_id, condition)
) STRICT;
CREATE TABLE summary_watermarks (
    summary_id INTEGER NOT NULL REFERENCES health_summaries (summary_id),
    installation_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    receipt_time TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (summary_id, installation_id),
    FOREIGN KEY (installation_id, sequence) REFERENCES archived_messages (installation_id, sequence)
) STRICT;
CREATE TABLE summary_contributors (
    outcome_id INTEGER NOT NULL REFERENCES canonical_revisions (outcome_id),
    summary_id INTEGER NOT NULL REFERENCES health_summaries (summary_id),
    PRIMARY KEY (outcome_id, summary_id)
) STRICT;
CREATE INDEX summary_contributor_summary ON summary_contributors (summary_id, outcome_id);
CREATE TABLE summary_policy_links (
    link_id INTEGER PRIMARY KEY,
    policy_id INTEGER NOT NULL REFERENCES policies (policy_id),
    requested_at TEXT NOT NULL
) STRICT;
CREATE INDEX summary_policy_link_policy ON summary_policy_links (policy_id);
CREATE TABLE summary_evidence_bookmark (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    outcomes_through INTEGER NOT NULL,
    selections_through INTEGER NOT NULL
) STRICT;
CREATE TABLE summary_failed_acknowledgements (
    job_id INTEGER PRIMARY KEY REFERENCES processing_jobs (job_id)
) STRICT;
"""
