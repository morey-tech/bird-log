
CREATE TABLE configuration(id TEXT PRIMARY KEY, document TEXT NOT NULL);
CREATE TABLE recordings(
 path TEXT PRIMARY KEY, captured_at REAL NOT NULL, frames INTEGER,
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','done','failed','missing')),
 attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
 error TEXT, completed_at REAL
);
CREATE INDEX recording_queue ON recordings(state,captured_at,path);
CREATE TABLE windows(
 id TEXT PRIMARY KEY, start REAL NOT NULL, end REAL NOT NULL, padded INTEGER NOT NULL,
 sources_json TEXT NOT NULL, config_id TEXT NOT NULL REFERENCES configuration(id)
);
CREATE TABLE encounters(
 id TEXT PRIMARY KEY, species TEXT NOT NULL, scientific_name TEXT NOT NULL, common_name TEXT NOT NULL,
 start REAL NOT NULL, end REAL NOT NULL, score REAL NOT NULL,
 config_id TEXT NOT NULL REFERENCES configuration(id), model_version TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('open','closed')),
 clip_path TEXT, clip_state TEXT NOT NULL DEFAULT 'pending'
   CHECK(clip_state IN ('pending','available','expiring','expired','missing')),
 clip_start REAL, clip_end REAL, padding_limited INTEGER NOT NULL DEFAULT 0,
 close_reason TEXT, clip_audio BLOB,
 review_status TEXT NOT NULL DEFAULT 'unreviewed'
   CHECK(review_status IN ('unreviewed','correct','incorrect','uncertain')),
 review_updated_at REAL, review_version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX encounters_time ON encounters(start);
CREATE INDEX encounters_species_time ON encounters(species,start);
CREATE INDEX encounters_clips ON encounters(clip_state,start);
CREATE TABLE detections(
 id TEXT PRIMARY KEY, window_id TEXT NOT NULL REFERENCES windows(id),
 encounter_id TEXT NOT NULL REFERENCES encounters(id), species TEXT NOT NULL,
 start REAL NOT NULL, end REAL NOT NULL, score REAL NOT NULL
);
CREATE INDEX detections_encounter ON detections(encounter_id);
CREATE INDEX detections_species_time ON detections(species,start);
CREATE TABLE checkpoint(id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL, audio BLOB NOT NULL);
CREATE TABLE gaps(id INTEGER PRIMARY KEY, start REAL, end REAL, reason TEXT NOT NULL, observed_at REAL NOT NULL);
PRAGMA user_version=1;
