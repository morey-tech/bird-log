from dataclasses import dataclass
import math
import os
from pathlib import Path


@dataclass(frozen=True)
class Config:
    device_match: str = "Yeti"
    sample_rate: int = 48000
    channel_mode: str = "mono"
    chunk_seconds: float = 30
    recordings_dir: Path = Path("/data/recordings")
    status_path: Path = Path("/status/recorder/status.json")
    retention_hours: float = 24
    min_free_disk_mb: int = 1024
    max_recordings_mb: int = 10000
    queue_seconds: float = 5
    callback_timeout: float = 5
    retry_max_seconds: float = 60
    log_level: str = "INFO"

    @property
    def channels(self):
        return 1 if self.channel_mode == "mono" else 2

    @property
    def chunk_frames(self):
        return round(self.chunk_seconds * self.sample_rate)

    def __post_init__(self):
        if not self.device_match.strip():
            raise ValueError("AUDIO_DEVICE_MATCH must not be empty")
        if self.channel_mode not in {"mono", "left", "right", "downmix"}:
            raise ValueError("AUDIO_CHANNEL_MODE must be mono, left, right, or downmix")
        for name in ("sample_rate", "chunk_seconds", "retention_hours", "min_free_disk_mb",
                     "max_recordings_mb", "queue_seconds", "callback_timeout", "retry_max_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.chunk_frames < 1:
            raise ValueError("CHUNK_SECONDS must contain at least one sample")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, or ERROR")

    @classmethod
    def from_env(cls):
        fields = {
            "device_match": ("AUDIO_DEVICE_MATCH", str),
            "sample_rate": ("AUDIO_SAMPLE_RATE", int),
            "channel_mode": ("AUDIO_CHANNEL_MODE", str),
            "chunk_seconds": ("CHUNK_SECONDS", float),
            "recordings_dir": ("RECORDINGS_DIR", Path),
            "status_path": ("STATUS_PATH", Path),
            "retention_hours": ("RETENTION_HOURS", float),
            "min_free_disk_mb": ("MIN_FREE_DISK_MB", int),
            "max_recordings_mb": ("MAX_RECORDINGS_MB", int),
            "queue_seconds": ("QUEUE_SECONDS", float),
            "callback_timeout": ("CALLBACK_TIMEOUT_SECONDS", float),
            "retry_max_seconds": ("RETRY_MAX_SECONDS", float),
            "log_level": ("LOG_LEVEL", str),
        }
        return cls(**{key: cast(os.environ[env]) for key, (env, cast) in fields.items() if env in os.environ})
