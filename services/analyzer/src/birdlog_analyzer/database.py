import json
import sqlite3
import time


SCHEMA = """
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
"""


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1):
        db.close()
        raise ValueError(f"Unsupported database schema version {version}")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    if version == 0:
        db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "COMMIT;")
    return db


def configure(db, identifier, document):
    existing = db.execute("SELECT id FROM configuration").fetchone()
    if existing and existing[0] != identifier:
        raise ValueError("Analysis settings/model changed; use a new database and clip directory (reanalysis is not supported)")
    with db:
        db.execute("INSERT OR IGNORE INTO configuration VALUES (?,?)", (identifier, document))


def snapshot(db):
    row = db.execute("SELECT document,audio FROM checkpoint WHERE id=1").fetchone()
    if not row:
        return {"segment": None, "last_input_at": None, "last_end": None}, b""
    return json.loads(row[0]), row[1]


def checkpoint(db, state, audio):
    db.execute("INSERT INTO checkpoint VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET document=excluded.document,audio=excluded.audio",
               (json.dumps(state), audio))


def queue_status(db):
    row = db.execute("SELECT count(*),min(captured_at) FROM recordings WHERE state='pending'").fetchone()
    return {"pending_recordings": row[0], "oldest_pending_at": row[1],
            "failed_recordings": db.execute("SELECT count(*) FROM recordings WHERE state='failed'").fetchone()[0],
            "missing_recordings": db.execute("SELECT count(*) FROM recordings WHERE state='missing'").fetchone()[0]}
