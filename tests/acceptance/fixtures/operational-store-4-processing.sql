-- Synthetic operational-store@4 database, produced through the published operational-store@4 module's API
-- (src/activity_tracker_portal/operational_store.py and _operational_store_sql.py at 6c64dbd, unchanged
-- since ed7b431) and dumped with sqlite3 iterdump; PRAGMA user_version=3 is appended because iterdump omits it.
-- Source Records: steps from two origins and weight from one. Outcomes: an inferred and an experienced
-- steps revision, a Policy-Pending fallback-timezone outcome, an inferred weight revision, a weight
-- quarantine, and an inferred revision whose record was later retracted. Jobs: completed, failed with no
-- outcome, leased (held for a day) with no outcome, and queued with no outcome. One item is never indexed.
-- operational-store@5's R42 tests load it to prove that @5 refuses it until migrated and then takes policies.
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
INSERT INTO "archived_messages" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',1,'ingest-batch','2f940f889ef9a006fd6dd8569caeb50519d77186f9ba7e0869e9b48874cae726',1,'2026-11-24T09:01:00.123456Z',X'7B22696E7374616C6C6174696F6E223A2230663866616435622D643963622D343639662D613136352D373038363737323839353065222C226B696E64223A22696E676573742D6261746368222C2273657175656E6365223A312C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A317D');
INSERT INTO "archived_messages" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',2,'ingest-batch','d77249d0e5f17858f1d182b3e29b68a01e13d7aab92fed38885976ed6cbd8beb',1,'2026-11-24T09:02:00.123456Z',X'7B22696E7374616C6C6174696F6E223A2230663866616435622D643963622D343639662D613136352D373038363737323839353065222C226B696E64223A22696E676573742D6261746368222C2273657175656E6365223A322C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A327D');
INSERT INTO "archived_messages" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',3,'ingest-batch','06d87937ae1d513bbd3d75b0ec053d51c47e14d49c70dc51c01fdc7122325326',1,'2026-11-24T09:03:00.123456Z',X'7B22696E7374616C6C6174696F6E223A2230663866616435622D643963622D343639662D613136352D373038363737323839353065222C226B696E64223A22696E676573742D6261746368222C2273657175656E6365223A332C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A337D');
INSERT INTO "archived_messages" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',4,'ingest-batch','bcf9677e89726d983067a2c40065e86719134516410b33f5de7a7e96090795fa',1,'2026-11-24T09:04:00.123456Z',X'7B22696E7374616C6C6174696F6E223A2230663866616435622D643963622D343639662D613136352D373038363737323839353065222C226B696E64223A22696E676573742D6261746368222C2273657175656E6365223A342C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A347D');
INSERT INTO "archived_messages" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',5,'ingest-batch','e5d47a43d45eb28feae4a80d29c6bf9156bb7fe64bd6c89647c1fc9dd7a6fd7a',1,'2026-11-24T09:05:00.123456Z',X'7B22696E7374616C6C6174696F6E223A2230663866616435622D643963622D343639662D613136352D373038363737323839353065222C226B696E64223A22696E676573742D6261746368222C2273657175656E6365223A352C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A357D');
INSERT INTO "archived_messages" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',6,'ingest-batch','21032d79276d79cfcbece237c5f32269a333b375d6b505dea88f79cb7ab9a61b',1,'2026-11-24T09:06:00.123456Z',X'7B22696E7374616C6C6174696F6E223A2230663866616435622D643963622D343639662D613136352D373038363737323839353065222C226B696E64223A22696E676573742D6261746368222C2273657175656E6365223A362C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A367D');
INSERT INTO "archived_messages" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',7,'ingest-batch','6f131ddbecd9662138558d3cde86f40094d8e5b1a49951ce78ea46146b29a670',1,'2026-11-24T09:07:00.123456Z',X'7B22696E7374616C6C6174696F6E223A2230663866616435622D643963622D343639662D613136352D373038363737323839353065222C226B696E64223A22696E676573742D6261746368222C2273657175656E6365223A372C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A377D');
INSERT INTO "archived_messages" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',8,'ingest-batch','829a9620f543267d56b21bd3ef33d3391441a2d58f45bda14ce4ba19d7f4ae23',1,'2026-11-24T09:08:00.123456Z',X'7B22696E7374616C6C6174696F6E223A2230663866616435622D643963622D343639662D613136352D373038363737323839353065222C226B696E64223A22696E676573742D6261746368222C2273657175656E6365223A382C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A387D');
INSERT INTO "archived_messages" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',9,'ingest-batch','2c0f4eda0db46c9784d2c856c2f63e3efe9a2caa4a609cbf8d60481fe07575ae',1,'2026-11-24T09:09:00.123456Z',X'7B22696E7374616C6C6174696F6E223A2230663866616435622D643963622D343639662D613136352D373038363737323839353065222C226B696E64223A22696E676573742D6261746368222C2273657175656E6365223A392C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A397D');
INSERT INTO "archived_messages" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',10,'ingest-batch','52f3101975b5690559727005bd0af15b1f530e5a1b4ce0f45ff5f1e4c23db06b',1,'2026-11-24T09:10:00.123456Z',X'7B22696E7374616C6C6174696F6E223A2230663866616435622D643963622D343639662D613136352D373038363737323839353065222C226B696E64223A22696E676573742D6261746368222C2273657175656E6365223A31302C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A31307D');
INSERT INTO "archived_messages" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',11,'ingest-batch','1fdfd78be25e01e84d7bbecf933e17b43aebe085695f256f5938625ab888c5ed',1,'2026-11-24T09:11:00.123456Z',X'7B22696E7374616C6C6174696F6E223A2230663866616435622D643963622D343639662D613136352D373038363737323839353065222C226B696E64223A22696E676573742D6261746368222C2273657175656E6365223A31312C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A31317D');
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
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',1,0,X'7B2273796E7468657469632D70726F6A656374696F6E223A2269642D696E666572726564227D',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',2,0,X'7B2273796E7468657469632D70726F6A656374696F6E223A2269642D657870657269656E636564227D',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',3,0,X'7B2273796E7468657469632D70726F6A656374696F6E223A2269642D70656E64696E67227D',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',4,0,X'7B2273796E7468657469632D70726F6A656374696F6E223A2269642D7765696768742D696E666572726564227D',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',5,0,X'7B2273796E7468657469632D70726F6A656374696F6E223A2269642D7765696768742D71756172616E74696E65227D',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',6,0,X'7B2273796E7468657469632D70726F6A656374696F6E223A2269642D726574726163746564227D',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',7,0,NULL,'id-retracted');
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',8,0,X'7B2273796E7468657469632D70726F6A656374696F6E223A2269642D6661696C6564227D',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',9,0,X'7B2273796E7468657469632D70726F6A656374696F6E223A2269642D6C6561736564227D',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',10,0,X'7B2273796E7468657469632D70726F6A656374696F6E223A2269642D717565756564227D',NULL);
INSERT INTO "batch_items" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',11,0,X'7B2273796E7468657469632D70726F6A656374696F6E223A22756E696E6465786564227D',NULL);
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
INSERT INTO "canonical_revisions" VALUES(1,X'7B2273796E7468657469632D686561646572223A22696E666572726564227D',X'7B2273796E7468657469632D626F6479223A22696E666572726564227D',1,1790000000000000000,1790000600000000000,'2026-09-21','2026-09-21','inferred','inferred','ef52b0ce8eed070ca6ba6caf580aa2635f16aa12457ba5c3e52abb42e9680b0c','3d5ca92742b92507c9ff515bf46cf5bd9b668dffc2826340cd3ad08c8410b9cc');
INSERT INTO "canonical_revisions" VALUES(2,X'7B2273796E7468657469632D686561646572223A22657870657269656E636564227D',X'7B2273796E7468657469632D626F6479223A22657870657269656E636564227D',1,1790000000000000000,1790000600000000000,'2026-09-21','2026-09-21','experienced','experienced','fa520b85c7d2e11eba461cc77035e2c1928cc0401014273c5bd23898b8ef0445','b192f7dbbc108b3962be187a493c211e35b8d0468622683805373ce01bc2eef3');
INSERT INTO "canonical_revisions" VALUES(4,X'7B2273796E7468657469632D686561646572223A227765696768742D696E666572726564227D',X'7B2273796E7468657469632D626F6479223A227765696768742D696E666572726564227D',1,1790000000000000000,1790000600000000000,'2026-09-21','2026-09-21','inferred','inferred','ff8f7724d8d30e6cfd1400445980736d92ae496c617175a7bc82773fb926e6db','36d3b07188335e1ebb410fa35f04aa91573014f8956a234e8336d577e5fd9a51');
INSERT INTO "canonical_revisions" VALUES(6,X'7B2273796E7468657469632D686561646572223A22726574726163746564227D',X'7B2273796E7468657469632D626F6479223A22726574726163746564227D',1,1790000000000000000,1790000600000000000,'2026-09-21','2026-09-21','inferred','inferred','1e3ae3342522130345d5260dfe31e39368e855422b84d330785313ba6a0bfe5d','af43d6f5c05d54e11b44b6749ee1b5e85cc6dbffca8fe2d9fbe6c8463fc61fa5');
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
INSERT INTO "enrollment_offers" VALUES('f6930c7d4a2f859d5214bd5433943aa99184b1bd56e7ce9bac0ffe7548f9787d','2026-11-24T09:30:00.123456Z','2026-11-24T09:00:00.123456Z');
CREATE TABLE generations (
    generation INTEGER PRIMARY KEY,
    advertisement BLOB NOT NULL,
    honored_at TEXT NOT NULL,
    stopped_at TEXT
) STRICT;
INSERT INTO "generations" VALUES(1,X'7B2273796E7468657469632D6164766572746973656D656E74223A317D','2026-11-24T09:00:00.123456Z',NULL);
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
INSERT INTO "ingest_batches" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',2,'steps',1,'initial-snapshot','e3b0c442-98fc-4c14-9afb-f4c8996fb924',2,1,'6a3753dd607f4bc5539d6ae650a22767b595f58f3dbc1ab331d9fde1232a261f');
INSERT INTO "ingest_batches" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',3,'steps',1,'initial-snapshot','e3b0c442-98fc-4c14-9afb-f4c8996fb924',3,1,'6ff9268fb1a7f5085ef71ea951c071f1105ae3bdbf41233e21aa73b15845f1d2');
INSERT INTO "ingest_batches" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',4,'weight',1,'initial-snapshot','e3b0c442-98fc-4c14-9afb-f4c8996fb924',4,1,'87fc65373bd3edb90fdb3b997ba37c03358ebef0c012f791111622daa2729987');
INSERT INTO "ingest_batches" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',5,'weight',1,'initial-snapshot','e3b0c442-98fc-4c14-9afb-f4c8996fb924',5,1,'c9f4b34e04a8b4466892803f188064ccfff2d9df5a24b113d30d540f748dbe63');
INSERT INTO "ingest_batches" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',6,'steps',1,'initial-snapshot','e3b0c442-98fc-4c14-9afb-f4c8996fb924',6,1,'4cebbb445503bdd6cabe6568449d0c1e0e839a2a6f04954d417df372e9a698bf');
INSERT INTO "ingest_batches" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',7,'steps',1,'change-cycle','5d41402a-bc4b-4a76-b971-9d911017c592',7,1,'c85942e2891b25bc393c2868c0d6011bc5cb28ddc333fdc9fe6c99ea46063ff2');
INSERT INTO "ingest_batches" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',8,'steps',1,'initial-snapshot','e3b0c442-98fc-4c14-9afb-f4c8996fb924',8,1,'d64b607ffc8d2434eebeb98598955fa2b02498df908821cd50e9a788fc1f6c59');
INSERT INTO "ingest_batches" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',9,'steps',1,'initial-snapshot','e3b0c442-98fc-4c14-9afb-f4c8996fb924',9,1,'fa8908cedf931325d0a346ad118eeaeb313ffcec073fc0941e67b4b72552f3c7');
INSERT INTO "ingest_batches" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',10,'steps',1,'initial-snapshot','e3b0c442-98fc-4c14-9afb-f4c8996fb924',10,1,'ae92fdd7dd10699d59ae4d5fe4bdf686cca84228265207c3a70f1e00be390e47');
INSERT INTO "ingest_batches" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e',11,'steps',1,'initial-snapshot','e3b0c442-98fc-4c14-9afb-f4c8996fb924',11,1,'5c0a973cd6aca258886e0ae89d2cfe1fb8393e0795b5c5da5f341d9b0297772d');
CREATE TABLE installation_events (
    event_id INTEGER PRIMARY KEY,
    installation_id TEXT NOT NULL REFERENCES installations (installation_id),
    state TEXT NOT NULL CHECK (state IN ('pending', 'active', 'draining', 'retired', 'withdrawn')),
    at TEXT NOT NULL,
    retirement_kind TEXT CHECK (retirement_kind IN ('drained', 'destructive'))
) STRICT;
INSERT INTO "installation_events" VALUES(1,'0f8fad5b-d9cb-469f-a165-70867728950e','pending','2026-11-24T09:00:00.123456Z',NULL);
INSERT INTO "installation_events" VALUES(2,'0f8fad5b-d9cb-469f-a165-70867728950e','active','2026-11-24T09:00:00.123456Z',NULL);
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
INSERT INTO "installations" VALUES('0f8fad5b-d9cb-469f-a165-70867728950e','1f8478b853ce0cb36801e29717dd2cf4142b91f0140267d2841757bca8d76cdb','active','2026-11-24T09:00:00.123456Z','2026-11-24T09:00:00.123456Z',NULL,NULL,NULL,NULL,11,'2026-11-24T09:11:00.123456Z',NULL,NULL,NULL);
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
INSERT INTO "processing_attempts" VALUES(1,1,'synthetic-worker-1','2026-11-24T09:40:00.123456Z','2026-11-24T09:41:00.123456Z','2026-11-24T09:40:00.123456Z','completed',NULL);
INSERT INTO "processing_attempts" VALUES(2,1,'synthetic-worker-1','2026-11-24T09:41:00.123456Z','2026-11-24T09:42:00.123456Z','2026-11-24T09:41:00.123456Z','completed',NULL);
INSERT INTO "processing_attempts" VALUES(3,1,'synthetic-worker-1','2026-11-24T09:42:00.123456Z','2026-11-24T09:43:00.123456Z','2026-11-24T09:42:00.123456Z','completed',NULL);
INSERT INTO "processing_attempts" VALUES(4,1,'synthetic-worker-1','2026-11-24T09:43:00.123456Z','2026-11-24T09:44:00.123456Z','2026-11-24T09:43:00.123456Z','completed',NULL);
INSERT INTO "processing_attempts" VALUES(5,1,'synthetic-worker-1','2026-11-24T09:44:00.123456Z','2026-11-24T09:45:00.123456Z','2026-11-24T09:44:00.123456Z','completed',NULL);
INSERT INTO "processing_attempts" VALUES(6,1,'synthetic-worker-1','2026-11-24T09:45:00.123456Z','2026-11-24T09:46:00.123456Z','2026-11-24T09:45:00.123456Z','completed',NULL);
INSERT INTO "processing_attempts" VALUES(7,1,'synthetic-worker-1','2026-11-24T09:50:00.123456Z','2026-11-24T09:51:00.123456Z','2026-11-24T09:50:00.123456Z','deterministic-failure','synthetic-invariant');
INSERT INTO "processing_attempts" VALUES(8,1,'synthetic-worker-1','2026-11-24T09:51:00.123456Z','2026-11-25T09:51:00.123456Z',NULL,NULL,NULL);
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
INSERT INTO "processing_jobs" VALUES(1,1,'interpret-steps@1',1,'completed',3,1,'2026-11-24T09:40:00.123456Z',1,NULL,NULL,NULL,'2026-11-24T09:40:00.123456Z','2026-11-24T09:40:00.123456Z');
INSERT INTO "processing_jobs" VALUES(2,2,'interpret-steps@1',2,'completed',3,1,'2026-11-24T09:41:00.123456Z',1,NULL,NULL,NULL,'2026-11-24T09:41:00.123456Z','2026-11-24T09:41:00.123456Z');
INSERT INTO "processing_jobs" VALUES(3,3,'interpret-steps@1',3,'completed',3,1,'2026-11-24T09:42:00.123456Z',1,NULL,NULL,NULL,'2026-11-24T09:42:00.123456Z','2026-11-24T09:42:00.123456Z');
INSERT INTO "processing_jobs" VALUES(4,4,'interpret-weight@1',4,'completed',3,1,'2026-11-24T09:43:00.123456Z',1,NULL,NULL,NULL,'2026-11-24T09:43:00.123456Z','2026-11-24T09:43:00.123456Z');
INSERT INTO "processing_jobs" VALUES(5,5,'interpret-weight@1',5,'completed',3,1,'2026-11-24T09:44:00.123456Z',1,NULL,NULL,NULL,'2026-11-24T09:44:00.123456Z','2026-11-24T09:44:00.123456Z');
INSERT INTO "processing_jobs" VALUES(6,6,'interpret-steps@1',6,'completed',3,1,'2026-11-24T09:45:00.123456Z',1,NULL,NULL,NULL,'2026-11-24T09:45:00.123456Z','2026-11-24T09:45:00.123456Z');
INSERT INTO "processing_jobs" VALUES(7,7,'interpret-steps@1',8,'failed',3,1,'2026-11-24T09:50:00.123456Z',1,NULL,NULL,'synthetic-invariant','2026-11-24T09:50:00.123456Z','2026-11-24T09:50:00.123456Z');
INSERT INTO "processing_jobs" VALUES(8,8,'interpret-steps@1',9,'leased',3,1,'2026-11-24T09:51:00.123456Z',1,'synthetic-worker-1','2026-11-25T09:51:00.123456Z',NULL,'2026-11-24T09:51:00.123456Z',NULL);
INSERT INTO "processing_jobs" VALUES(9,9,'interpret-steps@1',10,'queued',3,0,'2026-11-24T09:52:00.123456Z',0,NULL,NULL,NULL,'2026-11-24T09:52:00.123456Z',NULL);
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
INSERT INTO "processing_outcomes" VALUES(1,1,1,1,'interpret-steps@1','fallback-timezone:[["synthetic-ftz-1"],"2026f"]','revision',NULL,NULL,NULL,1,1,'2026-11-24T09:40:00.123456Z');
INSERT INTO "processing_outcomes" VALUES(2,2,2,2,'interpret-steps@1','none','revision',NULL,NULL,NULL,2,1,'2026-11-24T09:41:00.123456Z');
INSERT INTO "processing_outcomes" VALUES(3,3,3,3,'interpret-steps@1','none','policy-pending','fallback-timezone-policy-missing',NULL,'fallback-timezone',3,1,'2026-11-24T09:42:00.123456Z');
INSERT INTO "processing_outcomes" VALUES(4,4,4,4,'interpret-weight@1','fallback-timezone:[["synthetic-ftz-1"],"2026f"]','revision',NULL,NULL,NULL,4,1,'2026-11-24T09:43:00.123456Z');
INSERT INTO "processing_outcomes" VALUES(5,5,5,5,'interpret-weight@1','none','quarantine','synthetic-value-out-of-range','/value',NULL,5,1,'2026-11-24T09:44:00.123456Z');
INSERT INTO "processing_outcomes" VALUES(6,6,6,6,'interpret-steps@1','fallback-timezone:[["synthetic-ftz-1"],"2026f"]','revision',NULL,NULL,NULL,6,1,'2026-11-24T09:45:00.123456Z');
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
INSERT INTO "selection_evidence" VALUES(5,0,5);
INSERT INTO "selection_evidence" VALUES(6,0,6);
INSERT INTO "selection_evidence" VALUES(7,0,7);
INSERT INTO "selection_evidence" VALUES(8,0,8);
INSERT INTO "selection_evidence" VALUES(9,0,9);
INSERT INTO "selection_evidence" VALUES(10,0,10);
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
INSERT INTO "source_evidence" VALUES(1,'0f8fad5b-d9cb-469f-a165-70867728950e',1,0,'projection',1,'steps','id-inferred','2026-11-24T09:01:00.123456Z','533c8025ba8d1c28981d3e14ff578ad49fd545dcabf9b31d836ef0e91307ff05','2026-11-24T09:01:00.123456Z');
INSERT INTO "source_evidence" VALUES(2,'0f8fad5b-d9cb-469f-a165-70867728950e',2,0,'projection',2,'steps','id-experienced','2026-11-24T09:02:00.123456Z','4c632e532c3b7d599dd74fb597be6000ccb2faf849077d55d6a9d8cfb447cdcc','2026-11-24T09:02:00.123456Z');
INSERT INTO "source_evidence" VALUES(3,'0f8fad5b-d9cb-469f-a165-70867728950e',3,0,'projection',3,'steps','id-pending','2026-11-24T09:03:00.123456Z','71393584ec90f10cdc6720f39134948ba9141d76b26b66f6bc08f47be583e81c','2026-11-24T09:03:00.123456Z');
INSERT INTO "source_evidence" VALUES(4,'0f8fad5b-d9cb-469f-a165-70867728950e',4,0,'projection',4,'weight','id-weight-inferred','2026-11-24T09:04:00.123456Z','871d415f9bcce2a1a83526b0b9aa230daf787763b9164c90487f5c9cfee40abf','2026-11-24T09:04:00.123456Z');
INSERT INTO "source_evidence" VALUES(5,'0f8fad5b-d9cb-469f-a165-70867728950e',5,0,'projection',5,'weight','id-weight-quarantine','2026-11-24T09:05:00.123456Z','5ed399ab0b55d98e872384bce8757b55d9a0593d0ede2cf61c083ec5a9ffdc99','2026-11-24T09:05:00.123456Z');
INSERT INTO "source_evidence" VALUES(6,'0f8fad5b-d9cb-469f-a165-70867728950e',6,0,'projection',6,'steps','id-retracted','2026-11-24T09:06:00.123456Z','7df84ede98a0989256111b0e6aa14b482cc11f2cdd1b2614c3ee67a7b327fbef','2026-11-24T09:06:00.123456Z');
INSERT INTO "source_evidence" VALUES(7,'0f8fad5b-d9cb-469f-a165-70867728950e',7,0,'retraction',NULL,'steps','id-retracted',NULL,NULL,'2026-11-24T09:07:00.123456Z');
INSERT INTO "source_evidence" VALUES(8,'0f8fad5b-d9cb-469f-a165-70867728950e',8,0,'projection',7,'steps','id-failed','2026-11-24T09:08:00.123456Z','b6d769083326228ddcc3e1b1d314efbb13056d90d911d50e4ef1f2e732c1f0a6','2026-11-24T09:08:00.123456Z');
INSERT INTO "source_evidence" VALUES(9,'0f8fad5b-d9cb-469f-a165-70867728950e',9,0,'projection',8,'steps','id-leased','2026-11-24T09:09:00.123456Z','0addee86b4e5083498b76dc6a04184e294a943629a7f28008a385bfbc0947fe7','2026-11-24T09:09:00.123456Z');
INSERT INTO "source_evidence" VALUES(10,'0f8fad5b-d9cb-469f-a165-70867728950e',10,0,'projection',9,'steps','id-queued','2026-11-24T09:10:00.123456Z','415c95eef547c86f0173b9fb7368a9aa5aac0e82f1ee8896f5b15cceff521a6d','2026-11-24T09:10:00.123456Z');
CREATE TABLE source_records (
    record_id INTEGER PRIMARY KEY,
    source_system_kind TEXT NOT NULL,
    origin TEXT NOT NULL,
    record_type TEXT NOT NULL,
    source_identity TEXT NOT NULL,
    UNIQUE (source_system_kind, origin, record_type, source_identity)
) STRICT;
INSERT INTO "source_records" VALUES(1,'health-connect','org.example.synthetic.source','steps','id-inferred');
INSERT INTO "source_records" VALUES(2,'health-connect','org.example.synthetic.source','steps','id-experienced');
INSERT INTO "source_records" VALUES(3,'health-connect','org.example.synthetic.other','steps','id-pending');
INSERT INTO "source_records" VALUES(4,'health-connect','org.example.synthetic.source','weight','id-weight-inferred');
INSERT INTO "source_records" VALUES(5,'health-connect','org.example.synthetic.source','weight','id-weight-quarantine');
INSERT INTO "source_records" VALUES(6,'health-connect','org.example.synthetic.source','steps','id-retracted');
INSERT INTO "source_records" VALUES(7,'health-connect','org.example.synthetic.source','steps','id-failed');
INSERT INTO "source_records" VALUES(8,'health-connect','org.example.synthetic.other','steps','id-leased');
INSERT INTO "source_records" VALUES(9,'health-connect','org.example.synthetic.source','steps','id-queued');
CREATE TABLE source_selections (
    selection_id INTEGER PRIMARY KEY,
    record_id INTEGER NOT NULL REFERENCES source_records (record_id),
    outcome TEXT NOT NULL CHECK (outcome IN ('projection', 'retraction', 'conflict')),
    evidence_head INTEGER NOT NULL REFERENCES source_evidence (evidence_id),
    recorded_at TEXT NOT NULL
) STRICT;
INSERT INTO "source_selections" VALUES(1,1,'projection',1,'2026-11-24T09:01:00.123456Z');
INSERT INTO "source_selections" VALUES(2,2,'projection',2,'2026-11-24T09:02:00.123456Z');
INSERT INTO "source_selections" VALUES(3,3,'projection',3,'2026-11-24T09:03:00.123456Z');
INSERT INTO "source_selections" VALUES(4,4,'projection',4,'2026-11-24T09:04:00.123456Z');
INSERT INTO "source_selections" VALUES(5,5,'projection',5,'2026-11-24T09:05:00.123456Z');
INSERT INTO "source_selections" VALUES(6,6,'projection',6,'2026-11-24T09:06:00.123456Z');
INSERT INTO "source_selections" VALUES(7,6,'retraction',7,'2026-11-24T09:07:00.123456Z');
INSERT INTO "source_selections" VALUES(8,7,'projection',8,'2026-11-24T09:08:00.123456Z');
INSERT INTO "source_selections" VALUES(9,8,'projection',9,'2026-11-24T09:09:00.123456Z');
INSERT INTO "source_selections" VALUES(10,9,'projection',10,'2026-11-24T09:10:00.123456Z');
CREATE TABLE store_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
INSERT INTO "store_migrations" VALUES(1,'45d360c0b2b535d4c18c5337ca1c123e487a3aed65550a38179cab3080c5f811','2026-11-25T14:05:57.836325Z');
INSERT INTO "store_migrations" VALUES(2,'adfbd70824d5d9f029c0d3cc02c034a52cd4a828bfb0b2dbc98b510d7aa7c395','2026-11-25T14:05:57.837229Z');
INSERT INTO "store_migrations" VALUES(3,'107bc74890fdfa5011b20a730246b752984375145f18d8e45f3762eb56ba1da5','2026-11-25T14:05:57.838029Z');
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
CREATE UNIQUE INDEX processing_open_job ON processing_jobs (record_id, processor_version)
    WHERE state IN ('queued', 'leased');
CREATE INDEX processing_job_record ON processing_jobs (record_id, job_id);
CREATE INDEX processing_job_ready ON processing_jobs (state, not_before, job_id);
CREATE INDEX processing_job_expiry ON processing_jobs (state, lease_expires_at, job_id);
CREATE INDEX processing_job_failed ON processing_jobs (record_id, processor_version, selection_id)
    WHERE state = 'failed';
CREATE INDEX processing_outcome_record ON processing_outcomes (record_id, outcome_id);
CREATE INDEX processing_outcome_kind ON processing_outcomes (kind);
CREATE INDEX canonical_revision_instants ON canonical_revisions (start_epoch_ns, end_epoch_ns);
CREATE INDEX canonical_revision_dates ON canonical_revisions (start_local_date, end_local_date);
CREATE INDEX processing_record_type ON source_records (record_type, record_id);
CREATE INDEX processing_latest_selection ON source_selections (record_id, selection_id, outcome);
COMMIT;
PRAGMA user_version=3;
