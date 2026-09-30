import json
import os
from pathlib import Path
import subprocess
import sys
import time


def test_missing_device_retry_lock_and_graceful_stop(tmp_path):
    # Exercise real supervisor/worker processes without requiring PortAudio.
    (tmp_path / "sounddevice.py").write_text("def query_devices():\n    return []\n")
    source = Path(__file__).resolve().parents[1] / "src"
    status = tmp_path / "status.json"
    env = os.environ | {
        "PYTHONPATH": os.pathsep.join([str(tmp_path), str(source)]),
        "RECORDINGS_DIR": str(tmp_path / "recordings"),
        "STATUS_PATH": str(status),
        "MIN_FREE_DISK_MB": "1",
        "RETRY_MAX_SECONDS": "1",
    }
    log_path = tmp_path / "log.jsonl"
    with log_path.open("w") as output:
        process = subprocess.Popen([sys.executable, "-m", "birdlog_recorder", "run"],
                                   env=env, stdout=output, stderr=output)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if log_path.read_text().count('"event": "capture_retry"') >= 2:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError(log_path.read_text())
            assert not list((tmp_path / "recordings").rglob("*.wav"))
            duplicate = subprocess.run([sys.executable, "-m", "birdlog_recorder", "run"],
                                       env=env, capture_output=True, text=True, timeout=5)
            assert duplicate.returncode == 1
            assert "BlockingIOError" in duplicate.stderr
            process.terminate()
            assert process.wait(timeout=5) == 0
            assert json.loads(status.read_text())["state"] == "stopped"
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
