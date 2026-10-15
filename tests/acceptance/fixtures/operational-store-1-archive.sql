-- Synthetic operational-store@1 database, produced by the published operational-store@1 module
-- (src/activity_tracker_portal/operational_store.py at bee3744) and dumped with sqlite3 iterdump.
-- operational-store@2's R17 test loads it to prove that @2 reads a database @1 created.
BEGIN TRANSACTION;
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
INSERT INTO "archived_messages" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',1,'ingest-batch','2f940f889ef9a006fd6dd8569caeb50519d77186f9ba7e0869e9b48874cae726',1,'2026-10-12T09:01:00.123456Z',X'7B22696E7374616C6C6174696F6E223A2230663866616435622D643963622D343639662D613136352D373038363737323839353065222C226B696E64223A22696E676573742D6261746368222C2273657175656E6365223A312C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A317D');
INSERT INTO "archived_messages" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',2,'ingest-batch','d77249d0e5f17858f1d182b3e29b68a01e13d7aab92fed38885976ed6cbd8beb',1,'2026-10-12T09:02:00.123456Z',X'7B22696E7374616C6C6174696F6E223A2230663866616435622D643963622D343639662D613136352D373038363737323839353065222C226B696E64223A22696E676573742D6261746368222C2273657175656E6365223A322C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A327D');
INSERT INTO "archived_messages" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',3,'ingest-batch','06d87937ae1d513bbd3d75b0ec053d51c47e14d49c70dc51c01fdc7122325326',1,'2026-10-12T09:03:00.123456Z',X'7B22696E7374616C6C6174696F6E223A2230663866616435622D643963622D343639662D613136352D373038363737323839353065222C226B696E64223A22696E676573742D6261746368222C2273657175656E6365223A332C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A337D');
INSERT INTO "archived_messages" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',4,'ingest-batch','bcf9677e89726d983067a2c40065e86719134516410b33f5de7a7e96090795fa',1,'2026-10-12T09:04:00.123456Z',X'7B22696E7374616C6C6174696F6E223A2230663866616435622D643963622D343639662D613136352D373038363737323839353065222C226B696E64223A22696E676573742D6261746368222C2273657175656E6365223A342C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A347D');
INSERT INTO "archived_messages" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',5,'change-completion','6d4fffc0ded83a9bf3fab3470661c15279ef0d79f34b1202c29db6753d5ac373',1,'2026-10-12T09:05:00.123456Z',X'7B22696E7374616C6C6174696F6E223A2230663866616435622D643963622D343639662D613136352D373038363737323839353065222C226B696E64223A226368616E67652D636F6D706C6574696F6E222C2273657175656E6365223A352C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A357D');
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
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',1,0,X'73796E7468657469632D70726F6A656374696F6E2D30',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',1,1,X'73796E7468657469632D70726F6A656374696F6E2D31',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',2,0,NULL,'synthetic-identity-0');
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',3,0,NULL,'synthetic-identity-1');
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
INSERT INTO "completions" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',5,'change',NULL,'weight','5d41402a-bc4b-4a76-b971-9d911017c592',1,1,1,'3924a403529d9bb0ac80687be0a06a893f4b1495366057b577fdd23b901f846f');
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
CREATE TABLE enrollment_offers (
    code_digest TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
) STRICT;
INSERT INTO "enrollment_offers" VALUES('18aa3e0e123ef373ec2aeecfe9f310cf069e01734edcefc07c6524e0e9956578','2026-10-12T09:30:00.123456Z','2026-10-12T09:00:00.123456Z');
CREATE TABLE generations (
    generation INTEGER PRIMARY KEY,
    advertisement BLOB NOT NULL,
    honored_at TEXT NOT NULL,
    stopped_at TEXT
) STRICT;
INSERT INTO "generations" VALUES(1,X'7B2273796E7468657469632D6164766572746973656D656E74223A317D','2026-10-12T09:00:00.123456Z',NULL);
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
INSERT INTO "ingest_batches" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',1,'steps',1,'initial-snapshot','e3b0c442-98fc-4c14-9afb-f4c8996fb924',1,1,'b3f86551a5526facbd1cf5c79fdb95b37cc62eb31c9e637f13c5ac5dddc0cc3d');
INSERT INTO "ingest_batches" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',2,'weight',1,'change-cycle','5d41402a-bc4b-4a76-b971-9d911017c592',1,1,'7f50b92ba9b38efb602f597fb7207ad7118f5deb7bbc90d503dd906282b3aec8');
INSERT INTO "ingest_batches" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',3,'steps',1,'initial-snapshot','e3b0c442-98fc-4c14-9afb-f4c8996fb924',1,2,'658cfa28b3510d57d758a37bfe6e8904446bde89265dd3deb80382ed8888cc03');
INSERT INTO "ingest_batches" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',4,'steps',1,'initial-snapshot','e3b0c442-98fc-4c14-9afb-f4c8996fb924',2,1,'cfcf54fefdb10395ef35fd57e1f5dbcf485c9824d2a8b1cceb142ce3ce1d8423');
CREATE TABLE installation_events (
    event_id INTEGER PRIMARY KEY,
    installation_id TEXT NOT NULL REFERENCES installations (installation_id),
    state TEXT NOT NULL CHECK (state IN ('pending', 'active', 'draining', 'retired', 'withdrawn')),
    at TEXT NOT NULL,
    retirement_kind TEXT CHECK (retirement_kind IN ('drained', 'destructive'))
) STRICT;
INSERT INTO "installation_events" VALUES(1,'0f8fad5b-d9cb-469f-a165-70867728950e','pending','2026-10-12T09:00:00.123456Z',NULL);
INSERT INTO "installation_events" VALUES(2,'0f8fad5b-d9cb-469f-a165-70867728950e','active','2026-10-12T09:00:00.123456Z',NULL);
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
INSERT INTO "installations" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e','f59a2bda2edd42c738833b2f952956cce2cdb38b99657f22a7f345c0e716ffe2','active','2026-10-12T09:00:00.123456Z','2026-10-12T09:00:00.123456Z',NULL,NULL,NULL,NULL,5,'2026-10-12T09:05:00.123456Z',NULL,NULL,NULL);
CREATE TABLE sequence_gaps (
    installation_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    gap_after INTEGER NOT NULL,
    detected_at TEXT NOT NULL,
    PRIMARY KEY (installation_id, sequence),
    FOREIGN KEY (installation_id, sequence) REFERENCES archived_messages (installation_id, sequence)
) STRICT;
CREATE TABLE store_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
INSERT INTO "store_migrations" VALUES(1,'45d360c0b2b535d4c18c5337ca1c123e487a3aed65550a38179cab3080c5f811','2026-10-15T10:15:45.262835Z');
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
CREATE UNIQUE INDEX one_delivering_installation ON installations ((1))
    WHERE state IN ('active', 'draining');
CREATE INDEX installation_event_order ON installation_events (installation_id, event_id);
CREATE INDEX acknowledged_generations ON archived_messages (staged_generation)
    WHERE kind = 'capability-acknowledgement';
CREATE UNIQUE INDEX delivery_block_identity ON delivery_blocks (
    (installation_id IS NULL), ifnull(installation_id, ''),
    (sequence IS NULL), ifnull(sequence, 0), message_sha256, failure
);
COMMIT;
PRAGMA user_version=1;
