from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo

RATE = 48000
WINDOW = 3 * RATE


@dataclass(frozen=True)
class Config:
    recordings_dir: Path = Path("/data/recordings")
    clips_dir: Path = Path("/data/clips")
    database_path: Path = Path("/data/db/bird-log.sqlite3")
    model_dir: Path = Path("/models")
    status_path: Path = Path("/status/analyzer/status.json")
    latitude: float = 45.4215
    longitude: float = -75.6972
    location_timezone: str = "America/Toronto"
    window_hop_seconds: float = 1.5
    min_score: float = 0.5
    geo_min_score: float = 0.03
    encounter_gap_seconds: float = 3
    max_encounter_seconds: float = 60
    clip_padding_seconds: float = 1
    clip_retention_days: float = 90
    max_clip_storage_mb: int = 10240
    min_free_disk_mb: int = 1024
    max_input_seconds: float = 120
    max_species_per_window: int = 5
    scan_seconds: float = 5
    idle_flush_seconds: float = 90
    max_attempts: int = 3
    retry_seconds: float = 10
    stall_seconds: float = 300
    log_level: str = "INFO"

    def __post_init__(self):
        for name, value in asdict(self).items():
            if isinstance(value, (int, float)) and not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if not -90 <= self.latitude <= 90 or not -180 <= self.longitude <= 180:
            raise ValueError("LATITUDE/LONGITUDE outside valid range")
        for name in ("min_score", "geo_min_score"):
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must be between zero and one")
        for name in ("encounter_gap_seconds", "clip_padding_seconds"):
            if not 0 <= getattr(self, name) <= 60:
                raise ValueError(f"{name} must be between 0 and 60")
        for name in ("clip_retention_days", "max_clip_storage_mb", "min_free_disk_mb", "scan_seconds",
                     "idle_flush_seconds", "max_attempts", "retry_seconds", "stall_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not 3 <= self.max_encounter_seconds <= 300 or not 3 <= self.max_input_seconds <= 300:
            raise ValueError("Input/encounter duration limits must be between 3 and 300 seconds")
        if not 0.5 <= self.window_hop_seconds <= 3:
            raise ValueError("WINDOW_HOP_SECONDS must be between 0.5 and 3")
        if not 1 <= self.max_species_per_window <= 20:
            raise ValueError("MAX_SPECIES_PER_WINDOW must be between 1 and 20")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, or ERROR")
        ZoneInfo(self.location_timezone)

    @classmethod
    def from_env(cls, require_deployment=False):
        defaults = cls()
        values = {}
        for name, default in asdict(defaults).items():
            if name.upper() in os.environ:
                cast = Path if isinstance(default, Path) else type(default)
                values[name] = cast(os.environ[name.upper()])
        if require_deployment:
            for name in ("LATITUDE", "LONGITUDE", "MAX_CLIP_STORAGE_MB"):
                if not os.environ.get(name):
                    raise ValueError(f"Set {name} explicitly for deployment")
        return cls(**values)

    def identity(self, model_id):
        fields = ("latitude", "longitude", "location_timezone", "window_hop_seconds", "min_score", "geo_min_score",
                  "encounter_gap_seconds", "max_encounter_seconds", "clip_padding_seconds", "max_species_per_window")
        document = {name: getattr(self, name) for name in fields}
        document.update(model=model_id, pipeline_version=1, sample_rate=RATE, window_frames=WINDOW)
        serialized = json.dumps(document, sort_keys=True)
        return hashlib.sha256(serialized.encode()).hexdigest(), serialized
