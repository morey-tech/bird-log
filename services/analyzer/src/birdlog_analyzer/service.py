import json
import logging
import os
import shutil
import sqlite3
import time

from . import database
from .audio import InvalidAudio, discover, read_recording
from .clips import Clips
from .config import RATE
from .engine import Engine


def log(event, **fields):
    logging.info(json.dumps({"timestamp": time.time(), "event": event, **fields}))


def status(path, **fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    temporary.write_text(json.dumps({"schema_version": 1, "updated_at": time.time(), **fields}) + "\n")
    os.replace(temporary, path)


def healthy(document, now=None):
    now = time.time() if now is None else now
    try:
        age = now - document["updated_at"]
        limit = document["stall_seconds"] if document["state"] == "processing" else max(15, 3 * document["scan_seconds"])
        return (document["schema_version"] == 1 and document["model_ready"]
                and document["state"] in ("processing", "idle") and 0 <= age <= limit
                and now - document["last_scan_at"] <= document["stall_seconds"]
                and document["clip_free_bytes"] >= document["min_free_bytes"]
                and document["database_free_bytes"] >= document["min_free_bytes"])
    except (KeyError, TypeError, ValueError):
        return False


class Analyzer:
    def __init__(self, config, db, predictor):
        self.config, self.db, self.predictor = config, db, predictor
        identifier, document = config.identity(predictor.identity)
        database.configure(db, identifier, document)
        log("analyzer_ready", model=predictor.identity, configuration=json.loads(document))
        self.clips = Clips(config, db, log)
        self.last_scan = None
        self.last_progress = None
        self.last_ratio = None
        self.last_recording = None

    def scan(self):
        # Stream discoveries through SQLite instead of keeping the queue in RAM.
        with self.db:
            for path, captured in discover(self.config.recordings_dir):
                self.db.execute("INSERT OR IGNORE INTO recordings(path,captured_at) VALUES(?,?)", (path, captured))
        self.last_scan = time.time()

    def report(self, state, **extra):
        status(self.config.status_path, state=state, model_ready=True, model_version=self.predictor.identity,
               last_scan_at=self.last_scan, last_progress_at=self.last_progress,
               last_processed_recording=self.last_recording, analysis_seconds_per_audio_second=self.last_ratio,
               clip_free_bytes=shutil.disk_usage(self.config.clips_dir).free,
               database_free_bytes=shutil.disk_usage(self.config.database_path.parent).free,
               database_bytes=sum(path.stat().st_size for path in self.config.database_path.parent.glob(self.config.database_path.name + "*")),
               clip_bytes=self.clips.usage(), min_free_bytes=self.config.min_free_disk_mb * 1024**2,
               scan_seconds=self.config.scan_seconds, stall_seconds=self.config.stall_seconds,
               detection_count=self.db.execute("SELECT count(*) FROM detections").fetchone()[0],
               encounter_count=self.db.execute("SELECT count(*) FROM encounters").fetchone()[0],
               **database.queue_status(self.db), **extra)

    def commit(self, engine, path=None, frames=None):
        payload = sum(len(row["clip_audio"] or b"") for row in engine.encounters.values()) + engine.audio.nbytes
        # Reserve room for transaction/WAL copies and metadata; pending clips drain before the next input.
        self.clips.ensure(database_bytes=3 * payload + 1024**2)
        engine.commit(path, frames)
        self.clips.publish_pending()
        self.last_progress = time.time()

    def process_next(self):
        row = self.db.execute("SELECT * FROM recordings WHERE state='pending' ORDER BY captured_at,path LIMIT 1").fetchone()
        if row is None:
            return False
        if row["retry_at"] > time.time():
            self.report("retrying", error=row["error"])
            return True
        self.report("processing")
        started = time.monotonic()
        try:
            samples = read_recording(self.config.recordings_dir / row["path"], self.config.max_input_seconds)
            engine = Engine(self.config, self.db, self.predictor)
            engine.feed(row["path"], row["captured_at"], samples)
        except (FileNotFoundError, InvalidAudio) as error:
            state = "missing" if isinstance(error, FileNotFoundError) else "failed"
            with self.db:
                self.db.execute("UPDATE recordings SET state=?,error=?,attempts=attempts+1 WHERE path=?",
                                (state, str(error), row["path"]))
            log("input_unavailable", path=row["path"], state=state, error=str(error))
            return True
        except sqlite3.Error:
            raise
        except Exception as error:
            # Inference/input I/O failures retry in order, then become explicit failures.
            attempts = row["attempts"] + 1
            state = "failed" if attempts >= self.config.max_attempts else "pending"
            with self.db:
                self.db.execute("UPDATE recordings SET state=?,attempts=?,retry_at=?,error=? WHERE path=?",
                                (state, attempts, time.time() + self.config.retry_seconds, str(error), row["path"]))
            log("input_retry", path=row["path"], attempts=attempts, state=state, error=str(error))
            return True
        # Storage/SQLite failures are service failures, never misclassified as bad inputs.
        self.commit(engine, row["path"], len(samples))
        elapsed = time.monotonic() - started
        self.last_ratio = elapsed / (len(samples) / RATE)
        self.last_recording = row["path"]
        log("recording_analyzed", path=row["path"], seconds=elapsed, audio_seconds=len(samples) / RATE,
            detections=len(engine.detections), windows=len(engine.windows))
        return True

    def flush(self, force=False):
        state, _ = database.snapshot(self.db)
        if state["segment"] and (force or time.time() - state["last_input_at"] >= self.config.idle_flush_seconds):
            engine = Engine(self.config, self.db, self.predictor)
            engine.finish("end_of_input" if force else "idle_timeout")
            self.commit(engine)

    def run(self, stop, once=False):
        self.clips.reconcile()
        last_scan = 0
        while not stop.is_set():
            try:
                if time.monotonic() - last_scan >= self.config.scan_seconds:
                    self.scan()
                    self.clips.cleanup()
                    last_scan = time.monotonic()
                self.clips.publish_pending()
                if self.process_next():
                    # Avoid spinning while the oldest input is in backoff.
                    pending = self.db.execute("SELECT retry_at FROM recordings WHERE state='pending' ORDER BY captured_at,path LIMIT 1").fetchone()
                    if pending and pending[0] > time.time():
                        stop.wait(min(self.config.scan_seconds, pending[0] - time.time()))
                    continue
                self.flush(force=once)
                self.report("idle")
                if once:
                    self.report("stopped")
                    return
                stop.wait(self.config.scan_seconds)
            except (OSError, sqlite3.Error, RuntimeError) as error:
                log("analysis_paused", error=str(error))
                try:
                    self.report("unhealthy", error=str(error))
                except (OSError, sqlite3.Error):
                    log("status_write_failed")
                if once:
                    raise
                stop.wait(self.config.retry_seconds)
        # Context and open encounters are already durable; do not force a boundary at shutdown.
        self.report("stopped")
