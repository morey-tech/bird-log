import json
import logging
import os
from pathlib import Path
import time


def log(event, level=logging.INFO, **fields):
    logging.log(level, json.dumps({"timestamp": time.time(), "event": event, **fields}))


def publish_status(path, **fields):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"schema_version": 1, "updated_at": time.time(), **fields}) + "\n")
    os.replace(temporary, path)


def healthy(status, now=None):
    now = time.time() if now is None else now
    try:
        def recent(value, limit):
            return value is not None and -1 <= now - value <= limit

        return (
            status["schema_version"] == 1
            and status["state"] == "recording"
            and recent(status["updated_at"], 10)
            and recent(status["last_callback_at"], status["callback_timeout_seconds"])
            and recent(status["last_published_at"] or status["session_started_at"],
                       status["chunk_seconds"] + status["queue_seconds"] + 10)
            and status["free_bytes"] >= status["min_free_bytes"]
            and status["recordings_bytes"] < status["max_recordings_bytes"]
        )
    except (KeyError, TypeError, ValueError):
        return False
