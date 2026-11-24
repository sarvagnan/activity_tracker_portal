-- Synthetic operational-store@3 database, produced by the published operational-store@3 module
-- (src/activity_tracker_portal/operational_store.py at 5ee9db6, unchanged since 1b13103) and dumped with
-- sqlite3 iterdump. It holds projection, retraction, and conflict selections, a selected record whose
-- newer projection was selected again, an indexed record with no selection, and unindexed items.
-- operational-store@4's R29 tests load it to prove that @4 refuses it until migrated and then reads it.
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
INSERT INTO "archived_messages" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',1,'ingest-batch','2f940f889ef9a006fd6dd8569caeb50519d77186f9ba7e0869e9b48874cae726',1,'2026-10-20T09:01:00.123456Z',X'7B22696E7374616C6C6174696F6E223A2230663866616435622D643963622D343639662D613136352D373038363737323839353065222C226B696E64223A22696E676573742D6261746368222C2273657175656E6365223A312C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A317D');
INSERT INTO "archived_messages" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',2,'ingest-batch','d77249d0e5f17858f1d182b3e29b68a01e13d7aab92fed38885976ed6cbd8beb',1,'2026-10-20T09:02:00.123456Z',X'7B22696E7374616C6C6174696F6E223A2230663866616435622D643963622D343639662D613136352D373038363737323839353065222C226B696E64223A22696E676573742D6261746368222C2273657175656E6365223A322C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A327D');
INSERT INTO "archived_messages" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',3,'ingest-batch','06d87937ae1d513bbd3d75b0ec053d51c47e14d49c70dc51c01fdc7122325326',1,'2026-10-20T09:03:00.123456Z',X'7B22696E7374616C6C6174696F6E223A2230663866616435622D643963622D343639662D613136352D373038363737323839353065222C226B696E64223A22696E676573742D6261746368222C2273657175656E6365223A332C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A337D');
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
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',1,0,X'7B2273796E7468657469632D70726F6A656374696F6E223A2261227D',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',1,1,X'7B2273796E7468657469632D70726F6A656374696F6E223A2262227D',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',1,2,X'7B2273796E7468657469632D70726F6A656374696F6E223A2263227D',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',1,3,X'7B2273796E7468657469632D70726F6A656374696F6E223A2264227D',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',1,4,X'7B2273796E7468657469632D70726F6A656374696F6E223A2265227D',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',1,5,X'7B2273796E7468657469632D70726F6A656374696F6E223A22756E696E64657865642D31227D',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',2,0,NULL,'synthetic-id-b');
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',2,1,X'7B2273796E7468657469632D70726F6A656374696F6E223A22612D32227D',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',2,2,X'7B2273796E7468657469632D70726F6A656374696F6E223A22632D32227D',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',3,0,X'7B2273796E7468657469632D70726F6A656374696F6E223A2277227D',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',3,1,X'7B2273796E7468657469632D70726F6A656374696F6E223A22772D756E696E6465786564227D',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',3,2,NULL,'synthetic-id-gone');
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
INSERT INTO "enrollment_offers" VALUES('f6930c7d4a2f859d5214bd5433943aa99184b1bd56e7ce9bac0ffe7548f9787d','2026-10-20T09:30:00.123456Z','2026-10-20T09:00:00.123456Z');
CREATE TABLE generations (
    generation INTEGER PRIMARY KEY,
    advertisement BLOB NOT NULL,
    honored_at TEXT NOT NULL,
    stopped_at TEXT
) STRICT;
INSERT INTO "generations" VALUES(1,X'7B2273796E7468657469632D6164766572746973656D656E74223A317D','2026-10-20T09:00:00.123456Z',NULL);
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
INSERT INTO "ingest_batches" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',1,'steps',1,'initial-snapshot','e3b0c442-98fc-4c14-9afb-f4c8996fb924',1,1,'0bbc75d9c3928ee96d94fcfe5c1d6bb1202d109e7985bf646e4fafde46718e15');
INSERT INTO "ingest_batches" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',2,'steps',1,'change-cycle','5d41402a-bc4b-4a76-b971-9d911017c592',1,1,'0bbc75d9c3928ee96d94fcfe5c1d6bb1202d109e7985bf646e4fafde46718e15');
INSERT INTO "ingest_batches" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',3,'weight',1,'initial-snapshot','e3b0c442-98fc-4c14-9afb-f4c8996fb924',2,1,'6a3753dd607f4bc5539d6ae650a22767b595f58f3dbc1ab331d9fde1232a261f');
CREATE TABLE installation_events (
    event_id INTEGER PRIMARY KEY,
    installation_id TEXT NOT NULL REFERENCES installations (installation_id),
    state TEXT NOT NULL CHECK (state IN ('pending', 'active', 'draining', 'retired', 'withdrawn')),
    at TEXT NOT NULL,
    retirement_kind TEXT CHECK (retirement_kind IN ('drained', 'destructive'))
) STRICT;
INSERT INTO "installation_events" VALUES(1,'0f8fad5b-d9cb-469f-a165-70867728950e','pending','2026-10-20T09:00:00.123456Z',NULL);
INSERT INTO "installation_events" VALUES(2,'0f8fad5b-d9cb-469f-a165-70867728950e','active','2026-10-20T09:00:00.123456Z',NULL);
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
INSERT INTO "installations" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e','1f8478b853ce0cb36801e29717dd2cf4142b91f0140267d2841757bca8d76cdb','active','2026-10-20T09:00:00.123456Z','2026-10-20T09:00:00.123456Z',NULL,NULL,NULL,NULL,3,'2026-10-20T09:03:00.123456Z',NULL,NULL,NULL);
CREATE TABLE selection_evidence (
    selection_id INTEGER NOT NULL REFERENCES source_selections (selection_id),
    evidence_index INTEGER NOT NULL,
    evidence_id INTEGER NOT NULL REFERENCES source_evidence (evidence_id),
    PRIMARY KEY (selection_id, evidence_index),
    UNIQUE (selection_id, evidence_id)
) STRICT;
INSERT INTO "selection_evidence" VALUES(1,0,1);
INSERT INTO "selection_evidence" VALUES(2,0,2);
INSERT INTO "selection_evidence" VALUES(3,0,3);
INSERT INTO "selection_evidence" VALUES(4,0,4);
INSERT INTO "selection_evidence" VALUES(5,0,6);
INSERT INTO "selection_evidence" VALUES(6,0,8);
INSERT INTO "selection_evidence" VALUES(7,0,7);
INSERT INTO "selection_evidence" VALUES(8,0,3);
INSERT INTO "selection_evidence" VALUES(8,1,9);
CREATE TABLE sequence_gaps (
    installation_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    gap_after INTEGER NOT NULL,
    detected_at TEXT NOT NULL,
    PRIMARY KEY (installation_id, sequence),
    FOREIGN KEY (installation_id, sequence) REFERENCES archived_messages (installation_id, sequence)
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
INSERT INTO "source_evidence" VALUES(1,'0f8fad5b-d9cb-469f-a165-70867728950e',1,0,'projection',1,'steps','synthetic-id-a','2026-10-20T09:01:00.123456Z','19be65319013b3a30c7157e3050ac99e0121ba7e87768d9f8d43842869511829','2026-10-20T09:10:00.123456Z');
INSERT INTO "source_evidence" VALUES(2,'0f8fad5b-d9cb-469f-a165-70867728950e',1,1,'projection',2,'steps','synthetic-id-b','2026-10-20T09:01:00.123456Z','0fa7e869c81110b7995e8cc49d1819770e33a9bf0b312a01fe79ab213a157da3','2026-10-20T09:10:00.123456Z');
INSERT INTO "source_evidence" VALUES(3,'0f8fad5b-d9cb-469f-a165-70867728950e',1,2,'projection',3,'steps','synthetic-id-c','2026-10-20T09:01:00.123456Z','34e517c3d8f5b138b9e14408281d3be7ea58c1152b2c91eec09551eaa7d8ca8d','2026-10-20T09:10:00.123456Z');
INSERT INTO "source_evidence" VALUES(4,'0f8fad5b-d9cb-469f-a165-70867728950e',1,3,'projection',4,'steps','synthetic-id-d','2026-10-20T09:01:00.123456Z','ff71932840e282c0ae7a86b8edb31ddc6a09e510836f4b9a603cd8cbc2de5774','2026-10-20T09:10:00.123456Z');
INSERT INTO "source_evidence" VALUES(5,'0f8fad5b-d9cb-469f-a165-70867728950e',1,4,'projection',5,'steps','synthetic-id-e','2026-10-20T09:01:00.123456Z','ccf42deb5be7d868032f52d5f50b3fb4061e0ea8ad6389a2ad208b785a2e65a5','2026-10-20T09:10:00.123456Z');
INSERT INTO "source_evidence" VALUES(6,'0f8fad5b-d9cb-469f-a165-70867728950e',3,0,'projection',6,'weight','synthetic-id-w','2026-10-20T09:01:00.123456Z','8beb049859d509e388730218d3610459cd6429268022590300a368850fe52878','2026-10-20T09:10:00.123456Z');
INSERT INTO "source_evidence" VALUES(7,'0f8fad5b-d9cb-469f-a165-70867728950e',2,0,'retraction',NULL,'steps','synthetic-id-b',NULL,NULL,'2026-10-20T09:12:00.123456Z');
INSERT INTO "source_evidence" VALUES(8,'0f8fad5b-d9cb-469f-a165-70867728950e',2,1,'projection',1,'steps','synthetic-id-a','2026-10-20T09:05:00.123456Z','969b9989d5034bf6ec424e3807cac6a03212357dce2ad46be7ceba4dcd3eb046','2026-10-20T09:12:00.123456Z');
INSERT INTO "source_evidence" VALUES(9,'0f8fad5b-d9cb-469f-a165-70867728950e',2,2,'projection',3,'steps','synthetic-id-c','2026-10-20T09:05:00.123456Z','976b617cc8ddd778e4db8c641448c53a600558e5f5622e99d7d8dbb3f66ce4b9','2026-10-20T09:12:00.123456Z');
CREATE TABLE source_records (
    record_id INTEGER PRIMARY KEY,
    source_system_kind TEXT NOT NULL,
    origin TEXT NOT NULL,
    record_type TEXT NOT NULL,
    source_identity TEXT NOT NULL,
    UNIQUE (source_system_kind, origin, record_type, source_identity)
) STRICT;
INSERT INTO "source_records" VALUES(1,'health-connect','org.example.synthetic.source','steps','synthetic-id-a');
INSERT INTO "source_records" VALUES(2,'health-connect','org.example.synthetic.source','steps','synthetic-id-b');
INSERT INTO "source_records" VALUES(3,'health-connect','org.example.synthetic.source','steps','synthetic-id-c');
INSERT INTO "source_records" VALUES(4,'health-connect','org.example.synthetic.source','steps','synthetic-id-d');
INSERT INTO "source_records" VALUES(5,'health-connect','org.example.synthetic.source','steps','synthetic-id-e');
INSERT INTO "source_records" VALUES(6,'health-connect','org.example.synthetic.source','weight','synthetic-id-w');
CREATE TABLE source_selections (
    selection_id INTEGER PRIMARY KEY,
    record_id INTEGER NOT NULL REFERENCES source_records (record_id),
    outcome TEXT NOT NULL CHECK (outcome IN ('projection', 'retraction', 'conflict')),
    evidence_head INTEGER NOT NULL REFERENCES source_evidence (evidence_id),
    recorded_at TEXT NOT NULL
) STRICT;
INSERT INTO "source_selections" VALUES(1,1,'projection',1,'2026-10-20T09:11:00.123456Z');
INSERT INTO "source_selections" VALUES(2,2,'projection',2,'2026-10-20T09:11:00.123456Z');
INSERT INTO "source_selections" VALUES(3,3,'projection',3,'2026-10-20T09:11:00.123456Z');
INSERT INTO "source_selections" VALUES(4,4,'projection',4,'2026-10-20T09:11:00.123456Z');
INSERT INTO "source_selections" VALUES(5,6,'projection',6,'2026-10-20T09:11:00.123456Z');
INSERT INTO "source_selections" VALUES(6,1,'projection',8,'2026-10-20T09:13:00.123456Z');
INSERT INTO "source_selections" VALUES(7,2,'retraction',7,'2026-10-20T09:13:00.123456Z');
INSERT INTO "source_selections" VALUES(8,3,'conflict',9,'2026-10-20T09:13:00.123456Z');
CREATE TABLE store_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
INSERT INTO "store_migrations" VALUES(1,'45d360c0b2b535d4c18c5337ca1c123e487a3aed65550a38179cab3080c5f811','2026-11-24T12:55:26.249448Z');
INSERT INTO "store_migrations" VALUES(2,'adfbd70824d5d9f029c0d3cc02c034a52cd4a828bfb0b2dbc98b510d7aa7c395','2026-11-24T12:55:26.249929Z');
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
CREATE INDEX source_projection_evidence ON source_evidence (record_id, evidence_id);
CREATE INDEX source_retraction_evidence ON source_evidence (record_type, source_identity, evidence_id)
    WHERE kind = 'retraction';
CREATE INDEX source_selection_order ON source_selections (record_id, selection_id);
COMMIT;
PRAGMA user_version=2;
