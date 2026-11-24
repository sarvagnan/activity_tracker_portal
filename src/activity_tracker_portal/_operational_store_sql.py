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
