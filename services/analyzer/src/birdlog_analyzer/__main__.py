import argparse
import fcntl
import json
import logging
import resource
import signal
import sys
import threading
import time

import numpy as np

from . import database
from .audio import NAME, read_recording
from .config import Config, RATE, WINDOW
from .model import BirdNET, provision
from .service import Analyzer, healthy, log, status


def main():
    parser = argparse.ArgumentParser(description="Offline Bird Log species analyzer")
    parser.add_argument("command", choices=("run", "once", "health", "prepare-models", "benchmark", "backup"))
    parser.add_argument("path", nargs="?", help="Sample WAV for benchmark, or destination for backup")
    parser.add_argument("--captured-at", help="ISO 8601 timestamp with offset for benchmark files without recorder names")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = None
    try:
        config = Config.from_env(require_deployment=args.command in {"run", "once", "benchmark"})
        logging.getLogger().setLevel(config.log_level)
        if args.command == "health":
            try:
                return 0 if healthy(json.loads(config.status_path.read_text())) else 1
            except (OSError, ValueError):
                return 1
        if args.command == "prepare-models":
            print(json.dumps(provision(config.model_dir), indent=2))
            return 0
        if args.command == "backup":
            if not args.path:
                raise ValueError("backup requires an unused destination path")
            import sqlite3
            from pathlib import Path
            destination = Path(args.path)
            with destination.open("xb"):
                pass
            with sqlite3.connect(config.database_path.resolve().as_uri() + "?mode=ro", uri=True) as source:
                with sqlite3.connect(destination) as target:
                    source.backup(target)
            return 0
        if args.command == "benchmark":
            from pathlib import Path
            if not args.path:
                raise ValueError("benchmark requires a WAV path")
            from datetime import datetime, timezone
            sample_path = Path(args.path)
            match = NAME.fullmatch(sample_path.name)
            if args.captured_at:
                date = datetime.fromisoformat(args.captured_at)
                if date.tzinfo is None:
                    raise ValueError("--captured-at requires a timezone offset")
            elif match:
                date = datetime.strptime(match[1], "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
            else:
                raise ValueError("Use a recorder filename or provide --captured-at for the benchmark")
            captured = date.timestamp()
            audio = read_recording(sample_path, config.max_input_seconds)
            with BirdNET(config) as model:
                started = time.monotonic()
                detections = []
                covered = 0
                for offset in range(0, len(audio), round(config.window_hop_seconds * RATE)):
                    if covered >= len(audio):
                        break
                    covered = min(len(audio), offset + WINDOW)
                    window = audio[offset:offset + WINDOW].astype(np.float32) / 32768
                    window = np.pad(window, (0, WINDOW - len(window)))
                    detections.extend(model.predict(window, captured + offset / RATE))
                elapsed = time.monotonic() - started
                print(json.dumps(dict(model=model.identity, audio_seconds=len(audio) / RATE,
                                      analysis_seconds=elapsed, seconds_per_audio_second=elapsed / (len(audio) / RATE),
                                      max_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                                      detections=detections)))
            return 0
        config.database_path.parent.mkdir(parents=True, exist_ok=True)
        with config.database_path.with_suffix(".analyzer.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            status(config.status_path, state="initializing", model_ready=False)
            stop = threading.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: stop.set())
            with BirdNET(config) as model:
                db = database.connect(config.database_path)
                try:
                    analyzer = Analyzer(config, db, model)
                    analyzer.run(stop, once=args.command == "once")
                finally:
                    db.close()
        return 0
    except Exception as error:
        log("analyzer_failed", error=str(error), error_type=type(error).__name__)
        # A duplicate instance must not overwrite the active instance's status.
        if config and args.command in {"run", "once"} and not isinstance(error, BlockingIOError):
            try:
                status(config.status_path, state="unhealthy", model_ready=False, error=str(error))
            except OSError:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
