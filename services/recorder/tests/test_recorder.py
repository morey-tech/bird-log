from dataclasses import replace
from datetime import datetime, timezone
import json
import signal
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from birdlog_recorder.capture import AudioQueue, mono, record, select_device
from birdlog_recorder.config import Config
from birdlog_recorder.status import healthy, publish_status
from birdlog_recorder.storage import ChunkWriter, Storage


@pytest.fixture
def config(tmp_path):
    return Config(recordings_dir=tmp_path / "audio", status_path=tmp_path / "status.json",
                  sample_rate=8000, chunk_seconds=0.25, min_free_disk_mb=1)


def test_atomic_chunks_and_short_final_preserve_samples(config):
    storage = Storage(config)
    published = []
    writer = ChunkWriter(config, storage, lambda p, n: published.append((p, n)))
    samples = np.arange(4500, dtype=np.int16)
    start = datetime(2026, 9, 29, 23, 59, 59, 750000, tzinfo=timezone.utc).timestamp()
    writer.write(samples[:1000], start)
    assert not list(config.recordings_dir.rglob("*.wav"))
    assert len(list(config.recordings_dir.rglob("*.partial"))) == 1
    writer.write(samples[1000:], start + 1000 / config.sample_rate)
    assert [n for _, n in published] == [2000, 2000]
    writer.publish()
    assert [n for _, n in published] == [2000, 2000, 500]
    assert not list(config.recordings_dir.rglob("*.partial"))
    np.testing.assert_array_equal(np.concatenate([sf.read(p, dtype="int16")[0] for p, _ in published]), samples)
    assert published[0][0].name.startswith("20260929T235959.750000Z_")
    assert published[1][0].name.startswith("20260930T000000.000000Z_")
    for path, frames in published:
        info = sf.info(path)
        assert (info.channels, info.subtype, info.samplerate, info.frames) == (1, "PCM_16", 8000, frames)


def owned(root, stamp, suffix="wav"):
    date = datetime.fromtimestamp(stamp, timezone.utc)
    path = root / date.strftime("%Y/%m/%d") / f"{date:%Y%m%dT%H%M%S.%fZ}_{'a' * 32}.{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture")
    return path


def test_retention_and_recovery_respect_ownership(config, tmp_path):
    now = 1800000000
    expired = owned(config.recordings_dir, now - 90000)
    recent = owned(config.recordings_dir, now)
    partial = owned(config.recordings_dir, now - 1, "partial")
    unrelated = expired.parent / "other.wav"
    unrelated.write_bytes(b"not owned")
    target = tmp_path / "outside"
    target.mkdir()
    outside = owned(target, now - 100000)
    config.recordings_dir.joinpath("linked").symlink_to(target)
    storage = Storage(config)
    storage.cleanup(recover=True, now=now)
    assert not expired.exists() and not partial.exists()
    assert recent.exists() and unrelated.exists() and outside.exists()
    assert storage.used == len(b"fixture")


def test_low_space_and_budget_stop_writes(config, monkeypatch):
    storage = Storage(config)
    monkeypatch.setattr("birdlog_recorder.storage.shutil.disk_usage", lambda _: SimpleNamespace(free=100))
    with pytest.raises(OSError, match="free space"):
        storage.check()
    monkeypatch.setattr("birdlog_recorder.storage.shutil.disk_usage", lambda _: SimpleNamespace(free=10**12))
    storage.used = config.max_recordings_mb * 1024**2
    with pytest.raises(OSError, match="MAX_RECORDINGS"):
        storage.check()


def test_failed_publication_leaves_only_partial(config, monkeypatch):
    writer = ChunkWriter(config, Storage(config), lambda *_: pytest.fail("published failed file"))
    writer.write(np.zeros(10, dtype=np.int16), 1800000000)
    monkeypatch.setattr("birdlog_recorder.storage.os.replace", lambda *_: (_ for _ in ()).throw(OSError("disk failure")))
    with pytest.raises(OSError):
        writer.publish()
    writer.abort()
    assert not list(config.recordings_dir.rglob("*.wav"))
    assert len(list(config.recordings_dir.rglob("*.partial"))) == 1


def test_selection_and_channel_conversion():
    devices = [{"name": "Speaker", "max_input_channels": 0}, {"name": "Blue Yeti", "max_input_channels": 2}]
    assert select_device(devices, "yeti")[0] == 1
    with pytest.raises(ValueError, match="matched 0"):
        select_device(devices, "absent")
    with pytest.raises(ValueError, match="matched 2"):
        select_device(devices + devices, "Yeti")
    samples = np.array([[32767, 32767], [-32768, -32768], [100, -100]], dtype=np.int16)
    np.testing.assert_array_equal(mono(samples, "downmix"), [32767, -32768, 0])
    np.testing.assert_array_equal(mono(samples, "right"), samples[:, 1])
    np.testing.assert_array_equal(mono(samples, "left"), samples[:, 0])


def test_callback_queue_is_bounded_and_reports_loss(config):
    audio = AudioQueue(replace(config, queue_seconds=0.01))
    timing = SimpleNamespace(inputBufferAdcTime=1, currentTime=1.1)
    samples = np.zeros((1024, 1), dtype=np.int16)
    audio.callback(samples, 1024, timing, False)
    samples[:] = 123
    audio.callback(samples, 1024, timing, True)
    assert audio.blocks.qsize() == 1
    assert audio.dropped_frames == 1024 and audio.overruns == 1
    assert np.all(audio.blocks.get()[2] == 0)
    audio.callback(samples, 1024, timing, False)
    assert audio.blocks.get()[0] == 2048


def test_health_requires_capture_publication_and_storage(config):
    status = dict(schema_version=1, state="recording", updated_at=100, last_callback_at=100,
                  callback_timeout_seconds=5, last_published_at=None, session_started_at=99,
                  chunk_seconds=30, queue_seconds=5, free_bytes=100, min_free_bytes=50,
                  recordings_bytes=10, max_recordings_bytes=1000)
    assert healthy(status, now=100)
    for changes in ({"updated_at": 80}, {"last_callback_at": 90}, {"free_bytes": 49},
                    {"session_started_at": 1}, {"state": "retrying"}, {"recordings_bytes": 1000}):
        assert not healthy(status | changes, now=100)
    assert not healthy({}, now=100)
    publish_status(config.status_path, state="stopped")
    assert json.loads(config.status_path.read_text())["state"] == "stopped"
    assert not config.status_path.with_suffix(".tmp").exists()


@pytest.mark.parametrize("field,value", [("chunk_seconds", 0), ("retention_hours", float("nan")),
                                           ("queue_seconds", -1), ("channel_mode", "stereo"), ("device_match", "")])
def test_bad_configuration(config, field, value):
    with pytest.raises(ValueError):
        replace(config, **{field: value})


@pytest.mark.parametrize("gap", [False, True])
def test_continuous_stream_rotates_and_flushes_on_signal(config, monkeypatch, gap):
    handlers = {}
    monkeypatch.setattr(signal, "signal", lambda sig, handler: handlers.update({sig: handler}))
    samples = np.arange(5000, dtype=np.int16)
    calls = []

    class Stream:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.callback = kwargs["callback"]
            self.offset = 0

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        @property
        def active(self):
            block = samples[self.offset:self.offset + 1000, None]
            self.callback(block, len(block), SimpleNamespace(inputBufferAdcTime=1 + self.offset / 8000, currentTime=1),
                          gap and self.offset == 1000)
            self.offset += len(block)
            if self.offset == len(samples):
                handlers[signal.SIGTERM]()
            return True

    sd = SimpleNamespace(query_devices=lambda: [{"name": "Yeti", "max_input_channels": 1}],
                         check_input_settings=lambda **_: None, InputStream=Stream)
    record(config, sd)
    assert len(calls) == 1  # No stream restart during chunk rotation.
    paths = sorted(config.recordings_dir.rglob("*.wav"))
    assert [sf.info(p).frames for p in paths] == ([1000, 2000, 2000] if gap else [2000, 2000, 1000])
    np.testing.assert_array_equal(np.concatenate([sf.read(p, dtype="int16")[0] for p in paths]), samples)
    assert json.loads(config.status_path.read_text())["state"] == "stopped"
