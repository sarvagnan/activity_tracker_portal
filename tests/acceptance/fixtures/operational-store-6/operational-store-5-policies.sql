-- Synthetic operational-store@5 database, produced through the published operational-store@5 module's API
-- (src/activity_tracker_portal/operational_store.py and _operational_store_sql.py at d9a5aee, unchanged
-- since 9b77c79) and dumped with sqlite3 iterdump; PRAGMA user_version=4 is appended because iterdump omits it.
-- Installations: A (retired, drained) then B (active). A delivered a steps and a weight snapshot, a snapshot
-- completion, and a steps change cycle; B delivered a steps and a weight snapshot. Outcomes: an inferred and an
-- experienced steps revision, a Policy-Pending fallback-timezone outcome on a record later retracted, a weight
-- revision, a weight Quarantine, and a revision from B's other origin. B re-observed one steps record, which is
-- now a Source Conflict. Jobs: completed, one queued (open), and one failed weight job. One item is never
-- indexed and one is quarantined. Policies: two Source Selection Policies, the first superseded, and one
-- Fallback Timezone Policy. operational-store@6's R55 tests load it to prove that @6 refuses it until migrated
-- and then takes summary work.
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
INSERT INTO "archived_messages" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',1,'ingest-batch','fe1b7fa2234ff0b38fccd90425298e99fe5328070eb1d3b206d3968730a96029',1,'2026-11-20T08:05:00.000000Z',X'7B22696E7374616C6C6174696F6E223A2237633965363637392D373432352D343064652D393434622D653037666331663930616537222C2273657175656E6365223A312C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A317D');
INSERT INTO "archived_messages" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',2,'ingest-batch','d4f79d9e4d13a47ce85d3579f99b727b902b200cac390b2acd292a2415907089',1,'2026-11-20T08:06:00.000000Z',X'7B22696E7374616C6C6174696F6E223A2237633965363637392D373432352D343064652D393434622D653037666331663930616537222C2273657175656E6365223A322C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A327D');
INSERT INTO "archived_messages" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',3,'snapshot-completion','5d43ad9d1f7fab7cd2783aa4ef94b3880ddb2d81f2c3b8c948f0f49f5f051f30',1,'2026-11-20T08:07:00.000000Z',X'7B22696E7374616C6C6174696F6E223A2237633965363637392D373432352D343064652D393434622D653037666331663930616537222C2273657175656E6365223A332C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A337D');
INSERT INTO "archived_messages" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',4,'ingest-batch','480f5ae14f7aed83c3d229f0eb32ce7049d2e5f9e522892bfe4b1660dfb201cf',1,'2026-11-20T08:30:00.000000Z',X'7B22696E7374616C6C6174696F6E223A2237633965363637392D373432352D343064652D393434622D653037666331663930616537222C2273657175656E6365223A342C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A347D');
INSERT INTO "archived_messages" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',5,'ingest-batch','78d91a4ecbe6384d3a8235299b4bed8abdcb230fbff7c6d62be54773f4ff11ff',1,'2026-11-20T08:35:00.000000Z',X'7B22696E7374616C6C6174696F6E223A2237633965363637392D373432352D343064652D393434622D653037666331663930616537222C2273657175656E6365223A352C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A357D');
INSERT INTO "archived_messages" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',6,'ingest-batch','799b3b7b62a89135868c69e68edf3c2dc8cb1c6d7bf0b2dddc5e5b7ff1be9b0b',1,'2026-11-20T08:36:00.000000Z',X'7B22696E7374616C6C6174696F6E223A2237633965363637392D373432352D343064652D393434622D653037666331663930616537222C2273657175656E6365223A362C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A367D');
INSERT INTO "archived_messages" VALUES('16fd2706-8baf-433b-82eb-8c7fada847da',1,'ingest-batch','9f9dab41e68d441ec0c61a99b365417dae3b02e7369aa8e53021e9a8871506ef',1,'2026-11-20T08:43:00.000000Z',X'7B22696E7374616C6C6174696F6E223A2231366664323730362D386261662D343333622D383265622D386337666164613834376461222C2273657175656E6365223A312C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A317D');
INSERT INTO "archived_messages" VALUES('16fd2706-8baf-433b-82eb-8c7fada847da',2,'ingest-batch','25e2dfd9d78220d893bd3faeaceb63278d1013274716ac810c9a10787cad12d2',1,'2026-11-20T08:44:00.000000Z',X'7B22696E7374616C6C6174696F6E223A2231366664323730362D386261662D343333622D383265622D386337666164613834376461222C2273657175656E6365223A322C2273796E746865746963223A747275657D',X'7B2273796E7468657469632D72656365697074223A327D');
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
INSERT INTO "batch_items" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',1,0,X'7B2273796E7468657469632D70726F6A656374696F6E223A2266782D73746570732D31222C226D61726B6572223A2273796E7468657469632D6865616C74682D6D61726B6572227D',NULL);
INSERT INTO "batch_items" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',1,1,X'7B2273796E7468657469632D70726F6A656374696F6E223A2266782D73746570732D32222C226D61726B6572223A2273796E7468657469632D6865616C74682D6D61726B6572227D',NULL);
INSERT INTO "batch_items" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',1,2,X'7B2273796E7468657469632D70726F6A656374696F6E223A2266782D73746570732D33222C226D61726B6572223A2273796E7468657469632D6865616C74682D6D61726B6572227D',NULL);
INSERT INTO "batch_items" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',2,0,X'7B2273796E7468657469632D70726F6A656374696F6E223A2266782D7765696768742D31222C226D61726B6572223A2273796E7468657469632D6865616C74682D6D61726B6572227D',NULL);
INSERT INTO "batch_items" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',2,1,X'7B2273796E7468657469632D70726F6A656374696F6E223A2266782D7765696768742D32222C226D61726B6572223A2273796E7468657469632D6865616C74682D6D61726B6572227D',NULL);
INSERT INTO "batch_items" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',4,0,NULL,'fx-steps-3');
INSERT INTO "batch_items" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',4,1,X'7B2273796E7468657469632D70726F6A656374696F6E223A2266782D73746570732D34222C226D61726B6572223A2273796E7468657469632D6865616C74682D6D61726B6572227D',NULL);
INSERT INTO "batch_items" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',5,0,X'7B2273796E7468657469632D70726F6A656374696F6E223A2266782D73746570732D35222C226D61726B6572223A2273796E7468657469632D6865616C74682D6D61726B6572227D',NULL);
INSERT INTO "batch_items" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',6,0,X'73796E7468657469632D756E7265616461626C652D70726F6A656374696F6E',NULL);
INSERT INTO "batch_items" VALUES('16fd2706-8baf-433b-82eb-8c7fada847da',1,0,X'7B2273796E7468657469632D70726F6A656374696F6E223A2266782D73746570732D312D616761696E222C226D61726B6572223A2273796E7468657469632D6865616C74682D6D61726B6572227D',NULL);
INSERT INTO "batch_items" VALUES('16fd2706-8baf-433b-82eb-8c7fada847da',1,1,X'7B2273796E7468657469632D70726F6A656374696F6E223A2266782D73746570732D6231222C226D61726B6572223A2273796E7468657469632D6865616C74682D6D61726B6572227D',NULL);
INSERT INTO "batch_items" VALUES('16fd2706-8baf-433b-82eb-8c7fada847da',2,0,X'7B2273796E7468657469632D70726F6A656374696F6E223A2266782D7765696768742D33222C226D61726B6572223A2273796E7468657469632D6865616C74682D6D61726B6572227D',NULL);
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
INSERT INTO "canonical_revisions" VALUES(1,X'7B2273796E7468657469632D686561646572223A2266782D73746570732D31227D',X'7B2273796E7468657469632D626F6479223A2266782D73746570732D31227D',1,1790000000000000000,1790000600000000000,'2026-11-19','2026-11-19','experienced','experienced','80e2845899b5f9905736200be17505bd61328c26ad378e7205e9e83eb29b6f8a','2a1d1c8097763403d932f25a8a9822c5370b08ea9368b9b2cbc48ece86d0dcff');
INSERT INTO "canonical_revisions" VALUES(2,X'7B2273796E7468657469632D686561646572223A2266782D73746570732D32227D',X'7B2273796E7468657469632D626F6479223A2266782D73746570732D32227D',1,1790000000000000000,1790000600000000000,'2026-11-18','2026-11-19','experienced','experienced','f175e8f4c96247b6a3832b65e3452e5c39ee107c08857af79dd9d9545ce7cc14','631bd89a7e69995e85f36ec5d3656e461f65def9c5e62867e95af93e74f4fa60');
INSERT INTO "canonical_revisions" VALUES(3,X'7B2273796E7468657469632D686561646572223A2266782D7765696768742D31227D',X'7B2273796E7468657469632D626F6479223A2266782D7765696768742D31227D',1,1790000000000000000,1790000600000000000,'2026-11-19','2026-11-19','experienced','experienced','cc0cb9e8812a90f6c8eca8695e16a896a3a58826d795bc2dc481e42a5f9626a3','3daf0e3a115123bd679e5d082ff48b66812a34bad24e5aaf8da432464b6ccbca');
INSERT INTO "canonical_revisions" VALUES(6,X'7B2273796E7468657469632D686561646572223A2266782D73746570732D6231227D',X'7B2273796E7468657469632D626F6479223A2266782D73746570732D6231227D',1,1790000000000000000,1790000600000000000,'2026-11-19','2026-11-19','experienced','experienced','6744b1db69c22a71139721928131ed18bee2b0b17b68e632e375d1b85ffc067e','f7a25ad7655308b844a5ec9e1594bd990a7d1409acaae85c90dff95daa0c1abb');
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
INSERT INTO "completions" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',3,'snapshot','initial','steps','c-a-steps',1,1,3,'ee886e5953b1cd7fdfad712f817ad6ad7973791b39962227476b6d51b8f7c6c4');
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
INSERT INTO "enrollment_offers" VALUES('7db385c52e7999e069b72928dc1bcb07a69392d5a6aac26f8d4cf30fca3fd1ef','2026-11-21T08:00:00.000000Z','2026-11-20T08:02:00.000000Z');
INSERT INTO "enrollment_offers" VALUES('683b0d1873b089a2faab8bed028af34c337b64545627acd8832d00d9bb7d5e46','2026-11-21T08:00:00.000000Z','2026-11-20T08:41:00.000000Z');
CREATE TABLE fallback_timezone_policies (
    policy_id INTEGER PRIMARY KEY REFERENCES policies (policy_id),
    zone TEXT NOT NULL,
    effective_from TEXT
) STRICT;
INSERT INTO "fallback_timezone_policies" VALUES(3,'Asia/Kolkata',NULL);
CREATE TABLE generations (
    generation INTEGER PRIMARY KEY,
    advertisement BLOB NOT NULL,
    honored_at TEXT NOT NULL,
    stopped_at TEXT
) STRICT;
INSERT INTO "generations" VALUES(1,X'7B2273796E7468657469632D6164766572746973656D656E74223A317D','2026-11-20T08:04:00.000000Z',NULL);
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
INSERT INTO "ingest_batches" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',1,'steps',1,'initial-snapshot','c-a-steps',1,1,'f49a30d1f2a17bd1251e1fec0717ccb11693bdd8989f602d430f432b3c848ec3');
INSERT INTO "ingest_batches" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',2,'weight',1,'initial-snapshot','c-a-weight',1,1,'f49a30d1f2a17bd1251e1fec0717ccb11693bdd8989f602d430f432b3c848ec3');
INSERT INTO "ingest_batches" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',4,'steps',1,'change-cycle','c-a-change',1,1,'f49a30d1f2a17bd1251e1fec0717ccb11693bdd8989f602d430f432b3c848ec3');
INSERT INTO "ingest_batches" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',5,'steps',1,'change-cycle','c-a-change',2,1,'a6d09361bd8c06c5d3c6bcd4ea85674a8945be83e549a12588f269da52356472');
INSERT INTO "ingest_batches" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',6,'steps',1,'change-cycle','c-a-change',3,1,'dffc55ca60486c512ad534569dc9ccd2480070d424a465282bcc3c8ca14712ee');
INSERT INTO "ingest_batches" VALUES('16fd2706-8baf-433b-82eb-8c7fada847da',1,'steps',1,'initial-snapshot','c-b-steps',1,1,'f49a30d1f2a17bd1251e1fec0717ccb11693bdd8989f602d430f432b3c848ec3');
INSERT INTO "ingest_batches" VALUES('16fd2706-8baf-433b-82eb-8c7fada847da',2,'weight',1,'initial-snapshot','c-b-weight',1,1,'f49a30d1f2a17bd1251e1fec0717ccb11693bdd8989f602d430f432b3c848ec3');
CREATE TABLE installation_events (
    event_id INTEGER PRIMARY KEY,
    installation_id TEXT NOT NULL REFERENCES installations (installation_id),
    state TEXT NOT NULL CHECK (state IN ('pending', 'active', 'draining', 'retired', 'withdrawn')),
    at TEXT NOT NULL,
    retirement_kind TEXT CHECK (retirement_kind IN ('drained', 'destructive'))
) STRICT;
INSERT INTO "installation_events" VALUES(1,'7c9e6679-7425-40de-944b-e07fc1f90ae7','pending','2026-11-20T08:02:00.000000Z',NULL);
INSERT INTO "installation_events" VALUES(2,'7c9e6679-7425-40de-944b-e07fc1f90ae7','active','2026-11-20T08:03:00.000000Z',NULL);
INSERT INTO "installation_events" VALUES(3,'7c9e6679-7425-40de-944b-e07fc1f90ae7','draining','2026-11-20T08:38:00.000000Z',NULL);
INSERT INTO "installation_events" VALUES(4,'7c9e6679-7425-40de-944b-e07fc1f90ae7','retired','2026-11-20T08:39:00.000000Z','drained');
INSERT INTO "installation_events" VALUES(5,'16fd2706-8baf-433b-82eb-8c7fada847da','pending','2026-11-20T08:41:00.000000Z',NULL);
INSERT INTO "installation_events" VALUES(6,'16fd2706-8baf-433b-82eb-8c7fada847da','active','2026-11-20T08:42:00.000000Z',NULL);
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
INSERT INTO "installations" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7','f517a7f93b5c65f9f8cfc67cb7e78e28ae17649a4eb14bc47c1fd2c6730d1280','retired','2026-11-20T08:02:00.000000Z','2026-11-20T08:03:00.000000Z','2026-11-20T08:38:00.000000Z','2026-11-20T08:39:00.000000Z','drained',NULL,6,'2026-11-20T08:36:00.000000Z',NULL,NULL,NULL);
INSERT INTO "installations" VALUES('16fd2706-8baf-433b-82eb-8c7fada847da','8519e9284f76b378491d00509faa78ac7105d580625c3e6290dbcb4708be598b','active','2026-11-20T08:41:00.000000Z','2026-11-20T08:42:00.000000Z',NULL,NULL,NULL,NULL,2,'2026-11-20T08:44:00.000000Z',NULL,NULL,NULL);
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
INSERT INTO "item_quarantines" VALUES('7c9e6679-7425-40de-944b-e07fc1f90ae7',6,0,'projection-unreadable','','2026-11-20T08:37:00.000000Z');
CREATE TABLE policies (
    policy_id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('fallback-timezone', 'source-selection')),
    requested_at TEXT NOT NULL
) STRICT;
INSERT INTO "policies" VALUES(1,'source-selection','2026-11-20T08:57:00.000000Z');
INSERT INTO "policies" VALUES(2,'source-selection','2026-11-20T08:58:00.000000Z');
INSERT INTO "policies" VALUES(3,'fallback-timezone','2026-11-20T08:59:00.000000Z');
CREATE TABLE policy_lease_marks (
    job_id INTEGER NOT NULL,
    fence INTEGER NOT NULL,
    PRIMARY KEY (job_id, fence),
    FOREIGN KEY (job_id, fence) REFERENCES processing_attempts (job_id, fence)
) STRICT;
CREATE TABLE policy_origins (
    policy_id INTEGER NOT NULL REFERENCES source_selection_policies (policy_id),
    source_system_kind TEXT NOT NULL,
    origin TEXT NOT NULL,
    PRIMARY KEY (policy_id, source_system_kind, origin)
) STRICT;
INSERT INTO "policy_origins" VALUES(1,'health-connect','org.example.synthetic.source');
INSERT INTO "policy_origins" VALUES(2,'health-connect','org.example.synthetic.other');
INSERT INTO "policy_origins" VALUES(2,'health-connect','org.example.synthetic.source');
CREATE TABLE policy_supersessions (
    old_id INTEGER PRIMARY KEY REFERENCES policies (policy_id),
    new_id INTEGER NOT NULL REFERENCES policies (policy_id),
    CHECK (old_id < new_id)
) STRICT;
INSERT INTO "policy_supersessions" VALUES(1,2);
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
INSERT INTO "processing_attempts" VALUES(1,1,'synthetic-fixture-worker','2026-11-20T08:20:00.000000Z','2026-11-20T08:30:00.000000Z','2026-11-20T08:21:00.000000Z','completed',NULL);
INSERT INTO "processing_attempts" VALUES(2,1,'synthetic-fixture-worker','2026-11-20T08:22:00.000000Z','2026-11-20T08:32:00.000000Z','2026-11-20T08:23:00.000000Z','completed',NULL);
INSERT INTO "processing_attempts" VALUES(3,1,'synthetic-fixture-worker','2026-11-20T08:24:00.000000Z','2026-11-20T08:34:00.000000Z','2026-11-20T08:25:00.000000Z','completed',NULL);
INSERT INTO "processing_attempts" VALUES(4,1,'synthetic-fixture-worker','2026-11-20T08:26:00.000000Z','2026-11-20T08:36:00.000000Z','2026-11-20T08:27:00.000000Z','completed',NULL);
INSERT INTO "processing_attempts" VALUES(5,1,'synthetic-fixture-worker','2026-11-20T08:28:00.000000Z','2026-11-20T08:38:00.000000Z','2026-11-20T08:29:00.000000Z','completed',NULL);
INSERT INTO "processing_attempts" VALUES(6,1,'synthetic-fixture-worker','2026-11-20T08:50:00.000000Z','2026-11-20T09:00:00.000000Z','2026-11-20T08:51:00.000000Z','completed',NULL);
INSERT INTO "processing_attempts" VALUES(7,1,'synthetic-fixture-worker','2026-11-20T08:55:00.000000Z','2026-11-20T09:05:00.000000Z','2026-11-20T08:56:00.000000Z','deterministic-failure','synthetic-defect');
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
INSERT INTO "processing_jobs" VALUES(1,1,'interpret-steps@1',1,'completed',3,1,'2026-11-20T08:20:00.000000Z',1,NULL,NULL,NULL,'2026-11-20T08:20:00.000000Z','2026-11-20T08:21:00.000000Z');
INSERT INTO "processing_jobs" VALUES(2,2,'interpret-steps@1',2,'completed',3,1,'2026-11-20T08:22:00.000000Z',1,NULL,NULL,NULL,'2026-11-20T08:22:00.000000Z','2026-11-20T08:23:00.000000Z');
INSERT INTO "processing_jobs" VALUES(3,4,'interpret-weight@1',4,'completed',3,1,'2026-11-20T08:24:00.000000Z',1,NULL,NULL,NULL,'2026-11-20T08:24:00.000000Z','2026-11-20T08:25:00.000000Z');
INSERT INTO "processing_jobs" VALUES(4,5,'interpret-weight@1',5,'completed',3,1,'2026-11-20T08:26:00.000000Z',1,NULL,NULL,NULL,'2026-11-20T08:26:00.000000Z','2026-11-20T08:27:00.000000Z');
INSERT INTO "processing_jobs" VALUES(5,3,'interpret-steps@1',3,'completed',3,1,'2026-11-20T08:28:00.000000Z',1,NULL,NULL,NULL,'2026-11-20T08:28:00.000000Z','2026-11-20T08:29:00.000000Z');
INSERT INTO "processing_jobs" VALUES(6,7,'interpret-steps@1',9,'completed',3,1,'2026-11-20T08:50:00.000000Z',1,NULL,NULL,NULL,'2026-11-20T08:50:00.000000Z','2026-11-20T08:51:00.000000Z');
INSERT INTO "processing_jobs" VALUES(7,8,'interpret-weight@1',10,'failed',3,1,'2026-11-20T08:55:00.000000Z',1,NULL,NULL,'synthetic-defect','2026-11-20T08:55:00.000000Z','2026-11-20T08:56:00.000000Z');
INSERT INTO "processing_jobs" VALUES(8,6,'interpret-steps@1',7,'queued',3,0,'2026-11-20T09:00:00.000000Z',0,NULL,NULL,NULL,'2026-11-20T09:00:00.000000Z',NULL);
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
INSERT INTO "processing_outcomes" VALUES(1,1,1,1,'interpret-steps@1','fallback-timezone:[["synthetic-ftz-1"],"2026f"]','revision',NULL,NULL,NULL,1,1,'2026-11-20T08:21:00.000000Z');
INSERT INTO "processing_outcomes" VALUES(2,2,2,2,'interpret-steps@1','none','revision',NULL,NULL,NULL,2,1,'2026-11-20T08:23:00.000000Z');
INSERT INTO "processing_outcomes" VALUES(3,4,4,4,'interpret-weight@1','none','revision',NULL,NULL,NULL,3,1,'2026-11-20T08:25:00.000000Z');
INSERT INTO "processing_outcomes" VALUES(4,5,5,5,'interpret-weight@1','none','quarantine','instant-out-of-range','/record/time',NULL,4,1,'2026-11-20T08:27:00.000000Z');
INSERT INTO "processing_outcomes" VALUES(5,3,3,3,'interpret-steps@1','none','policy-pending','fallback-timezone-policy-missing',NULL,'fallback-timezone',5,1,'2026-11-20T08:29:00.000000Z');
INSERT INTO "processing_outcomes" VALUES(6,7,9,9,'interpret-steps@1','none','revision',NULL,NULL,NULL,6,1,'2026-11-20T08:51:00.000000Z');
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
INSERT INTO "selection_evidence" VALUES(8,0,1);
INSERT INTO "selection_evidence" VALUES(8,1,8);
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
INSERT INTO "source_evidence" VALUES(1,'7c9e6679-7425-40de-944b-e07fc1f90ae7',1,0,'projection',1,'steps','fx-steps-1','2026-11-20T08:08:00.000000Z','25224cb669bc852d6797626969a6a8ef0c47a0fd8240fa35a1fe76e3feec5b95','2026-11-20T08:11:00.000000Z');
INSERT INTO "source_evidence" VALUES(2,'7c9e6679-7425-40de-944b-e07fc1f90ae7',1,1,'projection',2,'steps','fx-steps-2','2026-11-20T08:09:00.000000Z','c7c849c15d82d44679cfe83c6b531e1346698faebd7faec062438cc12ff6b73d','2026-11-20T08:11:00.000000Z');
INSERT INTO "source_evidence" VALUES(3,'7c9e6679-7425-40de-944b-e07fc1f90ae7',1,2,'projection',3,'steps','fx-steps-3','2026-11-20T08:10:00.000000Z','d581ae43989335cee190e690369e0862b93cd5ff799d76e65b8a89a5832c8e45','2026-11-20T08:11:00.000000Z');
INSERT INTO "source_evidence" VALUES(4,'7c9e6679-7425-40de-944b-e07fc1f90ae7',2,0,'projection',4,'weight','fx-weight-1','2026-11-20T08:12:00.000000Z','d2c2c262d8629cbe6d9c9faa2cdb56c73d8c3d9188da7b62a8b257d87a9b8dc3','2026-11-20T08:14:00.000000Z');
INSERT INTO "source_evidence" VALUES(5,'7c9e6679-7425-40de-944b-e07fc1f90ae7',2,1,'projection',5,'weight','fx-weight-2','2026-11-20T08:13:00.000000Z','86a2fc2afa567d3883ba8f921c12c7eb70e2c1dc816f13c1a36fd9012163b611','2026-11-20T08:14:00.000000Z');
INSERT INTO "source_evidence" VALUES(6,'7c9e6679-7425-40de-944b-e07fc1f90ae7',4,0,'retraction',NULL,'steps','fx-steps-3',NULL,NULL,'2026-11-20T08:32:00.000000Z');
INSERT INTO "source_evidence" VALUES(7,'7c9e6679-7425-40de-944b-e07fc1f90ae7',4,1,'projection',6,'steps','fx-steps-4','2026-11-20T08:31:00.000000Z','c30b7ce030d8cf035bd308961a16898b296eb100155815c3c497029f104b7eea','2026-11-20T08:32:00.000000Z');
INSERT INTO "source_evidence" VALUES(8,'16fd2706-8baf-433b-82eb-8c7fada847da',1,0,'projection',1,'steps','fx-steps-1','2026-11-20T08:45:00.000000Z','81b1e3048bb5c6fa652919ea657cbc858ca44bc49fa048522a10295fe69b3699','2026-11-20T08:47:00.000000Z');
INSERT INTO "source_evidence" VALUES(9,'16fd2706-8baf-433b-82eb-8c7fada847da',1,1,'projection',7,'steps','fx-steps-b1','2026-11-20T08:46:00.000000Z','9f39d7fd37858e9ebe291e5f993f4badfea0f6b4e796f101d9bdeb48b8e33428','2026-11-20T08:47:00.000000Z');
INSERT INTO "source_evidence" VALUES(10,'16fd2706-8baf-433b-82eb-8c7fada847da',2,0,'projection',8,'weight','fx-weight-3','2026-11-20T08:52:00.000000Z','642729998ae4abacb048ebe787ab27b701da707b82b7247025febccb96548506','2026-11-20T08:53:00.000000Z');
CREATE TABLE source_records (
    record_id INTEGER PRIMARY KEY,
    source_system_kind TEXT NOT NULL,
    origin TEXT NOT NULL,
    record_type TEXT NOT NULL,
    source_identity TEXT NOT NULL,
    UNIQUE (source_system_kind, origin, record_type, source_identity)
) STRICT;
INSERT INTO "source_records" VALUES(1,'health-connect','org.example.synthetic.source','steps','fx-steps-1');
INSERT INTO "source_records" VALUES(2,'health-connect','org.example.synthetic.source','steps','fx-steps-2');
INSERT INTO "source_records" VALUES(3,'health-connect','org.example.synthetic.source','steps','fx-steps-3');
INSERT INTO "source_records" VALUES(4,'health-connect','org.example.synthetic.source','weight','fx-weight-1');
INSERT INTO "source_records" VALUES(5,'health-connect','org.example.synthetic.source','weight','fx-weight-2');
INSERT INTO "source_records" VALUES(6,'health-connect','org.example.synthetic.source','steps','fx-steps-4');
INSERT INTO "source_records" VALUES(7,'health-connect','org.example.synthetic.other','steps','fx-steps-b1');
INSERT INTO "source_records" VALUES(8,'health-connect','org.example.synthetic.source','weight','fx-weight-3');
CREATE TABLE source_selection_policies (
    policy_id INTEGER PRIMARY KEY REFERENCES policies (policy_id),
    measure TEXT NOT NULL,
    record_type TEXT NOT NULL,
    first_date TEXT,
    last_date TEXT,
    CHECK (first_date IS NULL OR last_date IS NULL OR first_date <= last_date)
) STRICT;
INSERT INTO "source_selection_policies" VALUES(1,'daily-steps','steps','2026-11-01','2026-11-30');
INSERT INTO "source_selection_policies" VALUES(2,'daily-steps','steps','2026-11-01','2026-11-30');
CREATE TABLE source_selections (
    selection_id INTEGER PRIMARY KEY,
    record_id INTEGER NOT NULL REFERENCES source_records (record_id),
    outcome TEXT NOT NULL CHECK (outcome IN ('projection', 'retraction', 'conflict')),
    evidence_head INTEGER NOT NULL REFERENCES source_evidence (evidence_id),
    recorded_at TEXT NOT NULL
) STRICT;
INSERT INTO "source_selections" VALUES(1,1,'projection',1,'2026-11-20T08:15:00.000000Z');
INSERT INTO "source_selections" VALUES(2,2,'projection',2,'2026-11-20T08:16:00.000000Z');
INSERT INTO "source_selections" VALUES(3,3,'projection',3,'2026-11-20T08:17:00.000000Z');
INSERT INTO "source_selections" VALUES(4,4,'projection',4,'2026-11-20T08:18:00.000000Z');
INSERT INTO "source_selections" VALUES(5,5,'projection',5,'2026-11-20T08:19:00.000000Z');
INSERT INTO "source_selections" VALUES(6,3,'retraction',6,'2026-11-20T08:33:00.000000Z');
INSERT INTO "source_selections" VALUES(7,6,'projection',7,'2026-11-20T08:34:00.000000Z');
INSERT INTO "source_selections" VALUES(8,1,'conflict',8,'2026-11-20T08:48:00.000000Z');
INSERT INTO "source_selections" VALUES(9,7,'projection',9,'2026-11-20T08:49:00.000000Z');
INSERT INTO "source_selections" VALUES(10,8,'projection',10,'2026-11-20T08:54:00.000000Z');
CREATE TABLE store_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
INSERT INTO "store_migrations" VALUES(1,'45d360c0b2b535d4c18c5337ca1c123e487a3aed65550a38179cab3080c5f811','2026-11-27T16:10:13.952379Z');
INSERT INTO "store_migrations" VALUES(2,'adfbd70824d5d9f029c0d3cc02c034a52cd4a828bfb0b2dbc98b510d7aa7c395','2026-11-27T16:10:13.953184Z');
INSERT INTO "store_migrations" VALUES(3,'107bc74890fdfa5011b20a730246b752984375145f18d8e45f3762eb56ba1da5','2026-11-27T16:10:13.953878Z');
INSERT INTO "store_migrations" VALUES(4,'44bbda76db01d09e64ae4849ea3d870a3121b192ad26920dd7597627928d3437','2026-11-27T16:10:13.954714Z');
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
CREATE INDEX fallback_timezone_start ON fallback_timezone_policies (effective_from, policy_id);
CREATE INDEX source_selection_scope ON source_selection_policies (measure, record_type, first_date, last_date);
CREATE INDEX policy_supersession_history ON policy_supersessions (new_id, old_id);
CREATE INDEX known_origin_cover ON source_records (record_type, source_system_kind, origin);
COMMIT;
PRAGMA user_version=4;
