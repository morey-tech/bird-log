from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
import uuid

import numpy as np
import pytest
import soundfile as sf

from birdlog_analyzer import database
from birdlog_analyzer.audio import discover, read_recording, InvalidAudio
from birdlog_analyzer.clips import StoragePressure
from birdlog_analyzer.config import Config, RATE, WINDOW
from birdlog_analyzer.engine import Engine
from birdlog_analyzer.model import validate_models, week_for
from birdlog_analyzer.service import Analyzer, healthy


class Predictor:
    identity = "fixture-model-v1"

    def __init__(self, results=None):
        self.calls = []
        self.results = results if results is not None else [("Poecile atricapillus_Black-capped Chickadee", 0.9)]

    def predict(self, audio, captured):
        assert len(audio) == WINDOW
        self.calls.append((audio.copy(), captured))
        return self.results


@pytest.fixture
def config(tmp_path):
    result = Config(recordings_dir=tmp_path / "recordings", clips_dir=tmp_path / "clips",
                    database_path=tmp_path / "db" / "log.sqlite3", model_dir=tmp_path / "models",
                    status_path=tmp_path / "status.json", min_free_disk_mb=1, max_clip_storage_mb=100,
                    max_encounter_seconds=6, window_hop_seconds=3, retry_seconds=0.01, scan_seconds=0.01)
    result.recordings_dir.mkdir()
    return result


def recording(config, start, seconds, value=1000):
    date = datetime.fromtimestamp(start, timezone.utc)
    path = config.recordings_dir / date.strftime("%Y/%m/%d") / f"{date:%Y%m%dT%H%M%S.%fZ}_{uuid.uuid4().hex}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.full(round(seconds * RATE), value, dtype=np.int16), RATE, subtype="PCM_16")
    return path


def analyzer(config, predictor=None):
    return Analyzer(config, database.connect(config.database_path), predictor or Predictor())


def test_cross_chunk_context_survives_restart_and_no_duplicates(config):
    start = round(time.time())
    first = recording(config, start, 2)
    model = Predictor()
    app = analyzer(config, model)
    app.scan()
    app.process_next()
    assert not model.calls
    app.db.close()
    second = recording(config, start + 2, 4, 2000)
    app = analyzer(config, model)
    app.run(threading.Event(), once=True)
    assert len(model.calls) == 2
    np.testing.assert_array_equal(model.calls[0][0][:2 * RATE], np.full(2 * RATE, 1000 / 32768))
    np.testing.assert_array_equal(model.calls[0][0][2 * RATE:], np.full(RATE, 2000 / 32768))
    windows = app.db.execute("SELECT * FROM windows ORDER BY start").fetchall()
    assert [(w["start"], w["end"]) for w in windows] == [(start, start + 3), (start + 3, start + 6)]
    sources = json.loads(windows[0]["sources_json"])
    assert [Path(source["path"]).name for source in sources] == [first.name, second.name]
    row = app.db.execute("SELECT * FROM encounters").fetchone()
    assert row["review_status"] == "unreviewed" and row["clip_state"] == "available"
    audio, rate = sf.read(config.clips_dir / row["clip_path"], dtype="int16")
    assert rate == RATE and len(audio) == 6 * RATE and row["padding_limited"] == 1
    app.run(threading.Event(), once=True)
    assert len(model.calls) == 2
    assert app.db.execute("SELECT count(*) FROM encounters").fetchone()[0] == 1
    app.db.close()


def test_gap_flushes_short_window_and_never_joins_audio(config):
    start = round(time.time())
    recording(config, start, 1)
    recording(config, start + 10, 2, 2000)
    app = analyzer(config)
    app.run(threading.Event(), once=True)
    assert app.db.execute("SELECT count(*) FROM encounters").fetchone()[0] == 2
    assert app.db.execute("SELECT start,end FROM gaps").fetchone()[:] == (start + 1, start + 10)
    assert [row[0] for row in app.db.execute("SELECT padded FROM windows")] == [1, 1]
    clips = [sf.read(path, dtype="int16")[0] for path in config.clips_dir.glob("*.wav")]
    assert sorted(len(clip) for clip in clips) == [RATE, 2 * RATE]
    assert all(len(set(clip.tolist())) == 1 for clip in clips)
    app.db.close()


def test_encounter_duration_bound_and_review_preserved(config):
    start = round(time.time())
    recording(config, start, 3)
    app = analyzer(config)
    app.scan()
    app.process_next()
    with app.db:
        app.db.execute("UPDATE encounters SET review_status='correct',review_version=1")
    recording(config, start + 3, 12)
    app.run(threading.Event(), once=True)
    rows = app.db.execute("SELECT * FROM encounters ORDER BY start").fetchall()
    assert len(rows) == 3
    assert rows[0]["review_status"] == "correct" and rows[0]["review_version"] == 1
    assert all(row["end"] - row["start"] <= 6 for row in rows)
    assert all(row["clip_end"] - row["clip_start"] <= 8 for row in rows)
    app.db.close()


def test_transaction_failure_rolls_back_windows_and_checkpoint(config):
    start = round(time.time())
    path = recording(config, start, 3)
    app = analyzer(config)
    app.scan()
    app.db.execute("CREATE TRIGGER fail_commit BEFORE UPDATE ON recordings BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        app.process_next()
    assert app.db.execute("SELECT count(*) FROM windows").fetchone()[0] == 0
    assert database.snapshot(app.db)[0]["segment"] is None
    app.db.execute("DROP TRIGGER fail_commit")
    app.run(threading.Event(), once=True)
    assert app.db.execute("SELECT count(*) FROM windows").fetchone()[0] == 1
    assert path.exists()
    app.db.close()


def test_clip_recovery_after_rename_before_database_commit(config):
    recording(config, round(time.time()), 3)
    app = analyzer(config)
    app.scan()
    app.process_next()
    app.db.execute("CREATE TRIGGER fail_publish BEFORE UPDATE ON encounters WHEN NEW.clip_state='available' "
                   "BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        app.flush(force=True)
    assert len(list(config.clips_dir.glob("*.wav"))) == 1
    assert app.db.execute("SELECT clip_state FROM encounters").fetchone()[0] == "pending"
    app.db.execute("DROP TRIGGER fail_publish")
    app.clips.reconcile()
    app.clips.publish_pending()
    assert len(list(config.clips_dir.glob("*.wav"))) == 1
    assert app.db.execute("SELECT clip_state,clip_audio FROM encounters").fetchone()[:] == ("available", None)
    app.db.close()


def test_missing_corrupt_and_partial_inputs(config):
    start = round(time.time())
    missing = recording(config, start, 3)
    corrupt = recording(config, start + 3, 3)
    partial = recording(config, start + 6, 3)
    partial.rename(partial.with_suffix(".partial"))
    app = analyzer(config)
    app.scan()
    missing.unlink()
    corrupt.write_bytes(b"not a wave")
    app.run(threading.Event(), once=True)
    assert [row[0] for row in app.db.execute("SELECT state FROM recordings ORDER BY captured_at")] == ["missing", "failed"]
    assert not list(config.clips_dir.glob("*.wav"))
    app.db.close()


def test_retry_limit_and_continue(config):
    class Broken(Predictor):
        def predict(self, audio, captured):
            raise RuntimeError("injected inference failure")
    recording(config, round(time.time()), 3)
    app = analyzer(config, Broken())
    app.run(threading.Event(), once=True)
    row = app.db.execute("SELECT state,attempts FROM recordings").fetchone()
    assert tuple(row) == ("failed", 3)
    assert app.db.execute("SELECT count(*) FROM windows").fetchone()[0] == 0
    app.db.close()


def test_retention_preserves_reviews_and_raw_and_unrelated_files(config):
    raw = recording(config, round(time.time()), 3)
    app = analyzer(config)
    app.run(threading.Event(), once=True)
    identifier = app.db.execute("SELECT id FROM encounters").fetchone()[0]
    interrupted = app.clips.path(identifier).with_suffix(".partial")
    interrupted.write_bytes(b"interrupted publication")
    unrelated = config.clips_dir / "keep.wav"
    unrelated.write_bytes(b"unrelated")
    with app.db:
        app.db.execute("UPDATE encounters SET end=0,review_status='uncertain'")
    app.clips.cleanup()
    row = app.db.execute("SELECT clip_state,review_status FROM encounters").fetchone()
    assert tuple(row) == ("expired", "uncertain")
    assert raw.exists() and unrelated.exists() and not interrupted.exists()
    assert list(config.clips_dir.glob("*.wav")) == [unrelated]
    app.db.close()


def test_low_disk_pauses_before_metadata_commit(config, monkeypatch):
    recording(config, round(time.time()), 3)
    app = analyzer(config)
    app.scan()
    monkeypatch.setattr("birdlog_analyzer.clips.shutil.disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(StoragePressure):
        app.process_next()
    assert app.db.execute("SELECT state FROM recordings").fetchone()[0] == "pending"
    assert app.db.execute("SELECT count(*) FROM detections").fetchone()[0] == 0
    app.db.close()


def test_schema_and_configuration_changes_rejected(config):
    app = analyzer(config)
    with pytest.raises(ValueError, match="settings/model changed"):
        Analyzer(replace(config, min_score=0.7), app.db, Predictor())
    app.db.execute("PRAGMA user_version=999")
    app.db.close()
    with pytest.raises(ValueError, match="schema version"):
        database.connect(config.database_path)


def test_read_validation_and_symlink_discovery(config, tmp_path):
    path = recording(config, round(time.time()), 3)
    assert len(read_recording(path, 3)) == 3 * RATE
    with pytest.raises(InvalidAudio):
        read_recording(path, 2)
    link = config.recordings_dir / "linked"
    link.symlink_to(tmp_path)
    assert len(list(discover(config.recordings_dir))) == 1


def test_seasonal_bins_use_local_recording_date():
    stamp = datetime(2026, 2, 1, 1, tzinfo=timezone.utc).timestamp()
    assert week_for(stamp, "America/Toronto") == 4
    assert week_for(stamp, "UTC") == 5
    assert week_for(datetime(2026, 12, 31, tzinfo=timezone.utc).timestamp(), "UTC") == 48


def test_model_checksums_required(config):
    config.model_dir.mkdir()
    (config.model_dir / "manifest.json").write_text(json.dumps({"version": "2.4", "birdnet": "1.1.1", "files": {}}))
    (config.model_dir / "acoustic.tflite").write_bytes(b"wrong")
    with pytest.raises(ValueError, match="checksum"):
        validate_models(config.model_dir)


def test_health_idle_and_stalled():
    document = dict(schema_version=1, updated_at=100, state="idle", model_ready=True,
                    stall_seconds=30, scan_seconds=5, last_scan_at=100,
                    clip_free_bytes=100, database_free_bytes=100, min_free_bytes=50)
    assert healthy(document, 101)
    assert not healthy(document, 116)
    assert healthy(document | {"state": "processing"}, 125)
    assert not healthy(document | {"state": "processing"}, 131)
    assert not healthy(document | {"state": "unhealthy"}, 101)
    assert not healthy(document | {"database_free_bytes": 1}, 101)
    assert not healthy({}, 101)


@pytest.mark.parametrize("changes", [{"latitude": 91}, {"min_score": float("nan")},
                                      {"max_encounter_seconds": 0}, {"max_input_seconds": 10000},
                                      {"max_species_per_window": 0}, {"clip_padding_seconds": -1}])
def test_config_validation(config, changes):
    with pytest.raises(ValueError):
        replace(config, **changes)


def test_default_overlap_spans_thirty_second_boundary(config):
    config = replace(config, window_hop_seconds=1.5)
    start = round(time.time())
    recording(config, start, 30, 1000)
    recording(config, start + 30, 3, 2000)
    model = Predictor(results=[])
    app = analyzer(config, model)
    app.run(threading.Event(), once=True)
    crossing = [audio for audio, captured in model.calls if captured == start + 28.5]
    assert len(crossing) == 1
    np.testing.assert_array_equal(crossing[0][:72000], np.full(72000, 1000 / 32768))
    np.testing.assert_array_equal(crossing[0][72000:], np.full(72000, 2000 / 32768))
    assert len(model.calls) == 21
    assert app.db.execute("SELECT sum(padded) FROM windows").fetchone()[0] == 0
    app.db.close()
