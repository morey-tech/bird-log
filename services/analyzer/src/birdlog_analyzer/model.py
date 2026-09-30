from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
from zoneinfo import ZoneInfo

from .config import RATE, WINDOW

FILES = ("acoustic.tflite", "acoustic.labels.txt", "geo.tflite", "geo.labels.txt")


def sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def provision(directory):
    directory.mkdir(parents=True, exist_ok=True)
    os.environ["BIRDNET_APP_DATA"] = str(directory / "download-cache")
    import birdnet
    for kind in ("acoustic", "geo"):
        model = birdnet.load(kind, "2.4", "tf", library="litert")
        temporary = directory / f"{kind}.tflite.partial"
        shutil.copyfile(model.model_path, temporary)
        os.replace(temporary, directory / f"{kind}.tflite")
        (directory / f"{kind}.labels.txt").write_text("\n".join(model.species_list) + "\n")
    manifest = {"birdnet": "1.1.1", "version": "2.4", "runtime": "ai-edge-litert==2.0.3",
                "license": "CC-BY-NC-SA-4.0", "source": "https://github.com/birdnet-team/birdnet",
                "files": {name: sha256(directory / name) for name in FILES}}
    temporary = directory / "manifest.json.partial"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    os.replace(temporary, directory / "manifest.json")
    return manifest


def validate_models(directory):
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("version") != "2.4" or manifest.get("birdnet") != "1.1.1":
        raise ValueError("Unsupported model manifest; run prepare-models with this analyzer version")
    for name in FILES:
        if sha256(directory / name) != manifest["files"].get(name):
            raise ValueError(f"Model checksum mismatch: {name}")
    return manifest


def week_for(timestamp, timezone):
    date = datetime.fromtimestamp(timestamp, ZoneInfo(timezone))
    # BirdNET uses four seasonal bins per month (1..48), not ISO weeks.
    return (date.month - 1) * 4 + min(4, (date.day - 1) // 7 + 1)


class BirdNET:
    def __init__(self, config):
        self.config = config
        manifest = validate_models(config.model_dir)
        self.identity = "birdnet-2.4/litert-2.0.3/" + hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        self.resources = ExitStack()
        self.week = None
        self.allowed = set()

    def __enter__(self):
        os.environ["BIRDNET_APP_DATA"] = str(self.config.model_dir)
        import birdnet
        logging.getLogger("birdnet").setLevel(logging.WARNING)
        try:
            models = {kind: birdnet.load_custom(kind, "2.4", "tf", self.config.model_dir / f"{kind}.tflite",
                                              self.config.model_dir / f"{kind}.labels.txt", library="litert")
                      for kind in ("acoustic", "geo")}
            if models["acoustic"].get_sample_rate() != RATE or models["acoustic"].get_segment_size_samples() != WINDOW:
                raise ValueError("Model input contract changed")
            self.acoustic = self.resources.enter_context(models["acoustic"].predict_session(
                top_k=None, n_workers=1, n_producers=1, batch_size=1, prefetch_ratio=1,
                max_n_files=1, max_audio_duration_min=0.05, default_confidence_threshold=self.config.min_score,
                bandpass_fmin=0, bandpass_fmax=models["acoustic"].get_sig_fmax(), show_stats=None))
            self.geo = self.resources.enter_context(models["geo"].predict_session(min_confidence=self.config.geo_min_score))
            return self
        except BaseException:
            self.resources.close()
            raise

    def __exit__(self, *args):
        return self.resources.__exit__(*args)

    def predict(self, samples, captured):
        week = week_for(captured, self.config.location_timezone)
        if week != self.week:
            result = self.geo.run(self.config.latitude, self.config.longitude, week=week).to_structured_array()
            self.allowed = set(result["species_name"])
            self.week = week
        result = self.acoustic.run_arrays((samples, RATE)).to_structured_array()
        return [(str(row["species_name"]), float(row["confidence"])) for row in result
                if row["species_name"] in self.allowed]
