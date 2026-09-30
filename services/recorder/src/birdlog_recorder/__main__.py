import argparse
import fcntl
import json
import logging
import signal
import subprocess
import sys
import threading
import time

from .config import Config
from .status import healthy, log, publish_status


def supervise(config):
    config.recordings_dir.mkdir(parents=True, exist_ok=True)
    with (config.recordings_dir / ".recorder.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = threading.Event()
        child = None

        def shutdown(*_):
            stop.set()
            if child is not None and child.poll() is None:
                child.terminate()

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, shutdown)
        delay = 1
        while not stop.is_set():
            publish_status(config.status_path, state="initializing")
            started = time.monotonic()
            # New process refreshes PortAudio/ALSA discovery after USB reconnect.
            child = subprocess.Popen([sys.executable, "-m", "birdlog_recorder", "worker"], pass_fds=(lock.fileno(),))
            while child.poll() is None and not stop.wait(0.2):
                pass
            if stop.is_set():
                child.terminate()
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                    log("shutdown_timeout", level=logging.ERROR, note="partial chunk left for recovery")
                break
            if time.monotonic() - started >= 60:
                delay = 1
            log("capture_retry", level=logging.WARNING, exit_code=child.returncode, retry_seconds=delay)
            try:
                status = json.loads(config.status_path.read_text())
            except (OSError, ValueError):
                status = {}
            status.pop("updated_at", None)
            status.pop("schema_version", None)
            status.update(state="retrying", retry_seconds=delay)
            publish_status(config.status_path, **status)
            stop.wait(delay)
            delay = min(delay * 2, config.retry_max_seconds)
        publish_status(config.status_path, state="stopped")


def main():
    parser = argparse.ArgumentParser(description="Bird Log continuous USB recorder")
    parser.add_argument("command", choices=["run", "list-devices", "check-device", "health", "worker"], default="run", nargs="?")
    args = parser.parse_args()
    try:
        config = Config.from_env()
        logging.basicConfig(level=config.log_level, format="%(message)s")
        if args.command == "health":
            try:
                ok = healthy(json.loads(config.status_path.read_text()))
            except (OSError, ValueError):
                ok = False
            return 0 if ok else 1
        if args.command == "run":
            supervise(config)
            return 0
        import sounddevice as sd
        from .capture import record, select_device
        if args.command == "list-devices":
            for index, device in enumerate(sd.query_devices()):
                if device["max_input_channels"]:
                    print(json.dumps({"index": index, **device}))
            return 0
        if args.command == "check-device":
            index, device = select_device(sd.query_devices(), config.device_match)
            sd.check_input_settings(device=index, channels=config.channels, dtype="int16", samplerate=config.sample_rate)
            print(json.dumps({"device": device, "sample_rate": config.sample_rate,
                              "capture_channels": config.channels, "output_channels": 1, "dtype": "int16"}))
            return 0
        record(config, sd)
        return 0
    except Exception as error:
        log("recorder_failed", level=logging.ERROR, error=str(error), error_type=type(error).__name__)
        if args.command == "worker":
            try:
                publish_status(config.status_path, state="unhealthy", error=str(error))
            except Exception:
                logging.exception("Could not publish failure status")
        return 1


if __name__ == "__main__":
    sys.exit(main())
