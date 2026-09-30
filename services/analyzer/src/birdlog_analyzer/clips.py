import os
import re
import shutil
import time

import numpy as np
import soundfile as sf

from .config import RATE


class StoragePressure(OSError):
    pass


class Clips:
    def __init__(self, config, db, log):
        self.config, self.db, self.log = config, db, log
        self.root = config.clips_dir
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, identifier):
        if not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise ValueError("Invalid encounter ID")
        return self.root / f"{identifier}.wav"

    def usage(self):
        return sum(path.stat().st_size for path in self.root.iterdir()
                   if not path.is_symlink() and re.fullmatch(r"[0-9a-f]{64}\.(wav|partial)", path.name))

    def expire(self, identifier, reason):
        with self.db:
            self.db.execute("UPDATE encounters SET clip_state='expiring',clip_audio=NULL WHERE id=?", (identifier,))
        self.path(identifier).unlink(missing_ok=True)
        self.path(identifier).with_suffix(".partial").unlink(missing_ok=True)
        with self.db:
            self.db.execute("UPDATE encounters SET clip_state='expired' WHERE id=?", (identifier,))
        self.log("clip_removed", encounter_id=identifier, reason=reason)

    def reconcile(self):
        for path in self.root.glob("*.partial"):
            if not path.is_symlink() and re.fullmatch(r"[0-9a-f]{64}\.partial", path.name):
                path.unlink()
        for row in self.db.execute("SELECT id,clip_state FROM encounters WHERE clip_state IN ('expiring','available')"):
            if row["clip_state"] == "expiring":
                self.expire(row["id"], "interrupted_cleanup")
            elif not self.path(row["id"]).is_file():
                with self.db:
                    self.db.execute("UPDATE encounters SET clip_state='missing' WHERE id=?", (row["id"],))
                self.log("clip_missing", encounter_id=row["id"])

    def cleanup(self):
        cutoff = time.time() - self.config.clip_retention_days * 86400
        while True:
            row = self.db.execute("SELECT id FROM encounters WHERE state='closed' AND clip_state IN ('available','pending') "
                                  "AND end < ? ORDER BY start LIMIT 1", (cutoff,)).fetchone()
            if row is None:
                break
            self.expire(row[0], "retention")
        self.ensure()

    def ensure(self, clip_bytes=0, database_bytes=0):
        reserve = self.config.min_free_disk_mb * 1024**2
        budget = self.config.max_clip_storage_mb * 1024**2
        same_volume = self.root.stat().st_dev == self.config.database_path.parent.stat().st_dev
        while True:
            clip_free = shutil.disk_usage(self.root).free
            db_free = shutil.disk_usage(self.config.database_path.parent).free
            db_needed = reserve + database_bytes + (clip_bytes if same_volume else 0)
            if db_free < db_needed and not same_volume:
                raise StoragePressure("Database volume below free-space reserve")
            if (self.usage() + clip_bytes <= budget and clip_free >= reserve + clip_bytes
                    and db_free >= db_needed):
                return
            row = self.db.execute("SELECT id FROM encounters WHERE clip_state='available' ORDER BY start,id LIMIT 1").fetchone()
            if row is None:
                raise StoragePressure("Clip budget/free-space reserve cannot be satisfied; analysis paused")
            self.expire(row[0], "storage_pressure")

    def publish_pending(self):
        while True:
            row = self.db.execute("SELECT id,clip_audio FROM encounters WHERE state='closed' AND clip_state='pending' "
                                  "ORDER BY start,id LIMIT 1").fetchone()
            if row is None:
                return
            if row["clip_audio"] is None:
                raise ValueError("Pending clip has no durable audio payload")
            final = self.path(row["id"])
            # Pending payload is durable; reclaim interrupted attempts before reserving space.
            # Consumers must only serve rows marked available.
            temporary = final.with_suffix(".partial")
            final.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
            self.ensure(clip_bytes=len(row["clip_audio"]) + 4096)
            samples = np.frombuffer(row["clip_audio"], dtype="<i2")
            sf.write(temporary, samples, RATE, format="WAV", subtype="PCM_16")
            with temporary.open("rb") as source:
                os.fsync(source.fileno())
            os.replace(temporary, final)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            with self.db:
                self.db.execute("UPDATE encounters SET clip_state='available',clip_audio=NULL WHERE id=?", (row["id"],))
            self.log("clip_published", encounter_id=row["id"], frames=len(samples))
