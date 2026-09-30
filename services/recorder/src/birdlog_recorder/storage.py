from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import time
import uuid

import soundfile as sf

from .status import log


# Only this service's exact timestamp + random ID namespace is eligible for cleanup.
NAME = re.compile(r"^(\d{8}T\d{6}\.\d{6}Z)_([0-9a-f]{32})\.(wav|partial)$")


def owned_files(root):
    # Do not traverse symlinks or unrelated directories/files.
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
        for name in files:
            match = NAME.fullmatch(name)
            path = Path(directory) / name
            if not match or path.is_symlink():
                continue
            try:
                stamp = datetime.strptime(match[1], "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if path.parent.relative_to(root).as_posix() == stamp.strftime("%Y/%m/%d"):
                yield path, stamp.timestamp()


class Storage:
    def __init__(self, config):
        self.config = config
        self.root = config.recordings_dir
        self.root.mkdir(parents=True, exist_ok=True)
        self.used = 0
        self.free = 0
        self.last_cleanup = 0

    def cleanup(self, recover=False, now=None):
        now = time.time() if now is None else now
        used = 0
        for path, captured in owned_files(self.root):
            if (recover and path.suffix == ".partial") or (
                path.suffix == ".wav" and captured < now - self.config.retention_hours * 3600
            ):
                path.unlink()
                log("recording_removed", path=str(path), reason="orphan" if path.suffix == ".partial" else "retention")
            else:
                used += path.stat().st_size
        self.used = used
        self.last_cleanup = time.monotonic()
        # Remove empty date directories so they do not accumulate indefinitely.
        for directory, _, _ in os.walk(self.root, topdown=False, followlinks=False):
            path = Path(directory)
            if path != self.root and re.fullmatch(r"\d{4}(/\d{2}){0,2}", path.relative_to(self.root).as_posix()):
                try:
                    path.rmdir()
                except OSError:
                    pass

    def check(self, additional=0):
        if time.monotonic() - self.last_cleanup >= 60:
            self.cleanup()
        self.free = shutil.disk_usage(self.root).free
        if self.free - additional < self.config.min_free_disk_mb * 1024**2:
            raise OSError("free space below MIN_FREE_DISK_MB; capture paused")
        if self.used + additional >= self.config.max_recordings_mb * 1024**2:
            raise OSError("recordings reached MAX_RECORDINGS_MB; capture paused")


class ChunkWriter:
    def __init__(self, config, storage, on_publish):
        self.config, self.storage, self.on_publish = config, storage, on_publish
        self.file = None
        self.path = None
        self.frames = 0

    def open(self, captured):
        stamp = datetime.fromtimestamp(captured, timezone.utc)
        directory = self.config.recordings_dir / stamp.strftime("%Y/%m/%d")
        self.storage.check(4096)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{stamp:%Y%m%dT%H%M%S.%fZ}_{uuid.uuid4().hex}.partial"
        self.file = sf.SoundFile(self.path, mode="x", samplerate=self.config.sample_rate,
                                 channels=1, subtype="PCM_16", format="WAV")
        self.storage.used += 4096  # Conservative header reservation until the next scan.
        self.frames = 0

    def write(self, samples, captured):
        offset = 0
        while offset < len(samples):
            if self.file is None:
                self.open(captured + offset / self.config.sample_rate)
            count = min(len(samples) - offset, self.config.chunk_frames - self.frames)
            self.storage.check(count * 2)
            self.file.write(samples[offset:offset + count])
            self.storage.used += count * 2
            self.frames += count
            offset += count
            if self.frames == self.config.chunk_frames:
                self.publish()

    def publish(self):
        if self.file is None:
            return
        self.file.flush()
        self.file.close()
        self.file = None
        # Finalize and sync the WAV before making it visible to downstream consumers.
        with self.path.open("rb") as file:
            os.fsync(file.fileno())
        final = self.path.with_suffix(".wav")
        os.replace(self.path, final)
        directory_fd = os.open(final.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        self.on_publish(final, self.frames)
        self.path = None

    def abort(self):
        # Failed writes stay unpublished; startup recovery removes the partial file.
        if self.file is not None:
            self.file.close()
            self.file = None
