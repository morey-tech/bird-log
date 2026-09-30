import logging
import math
import queue
import signal
import threading
import time

import numpy as np

from .status import log, publish_status
from .storage import ChunkWriter, Storage


def select_device(devices, match):
    matches = [(i, d) for i, d in enumerate(devices)
               if d["max_input_channels"] > 0 and match.casefold() in d["name"].casefold()]
    if len(matches) != 1:
        raise ValueError(f"AUDIO_DEVICE_MATCH={match!r} matched {len(matches)} input devices; "
                         "use list-devices and choose a unique name substring")
    return matches[0]


def mono(samples, mode):
    if mode == "downmix":
        return np.rint(samples.astype(np.int32).mean(axis=1)).astype(np.int16)
    return samples[:, 1 if mode == "right" else 0]


class AudioQueue:
    """One PortAudio producer, one disk-writing consumer; no callback disk I/O."""

    def __init__(self, config):
        self.blocks = queue.Queue(maxsize=max(1, math.ceil(config.queue_seconds * config.sample_rate / 1024)))
        self.position = 0
        self.last_callback = None
        self.last_callback_monotonic = time.monotonic()
        self.dropped_frames = 0
        self.overruns = 0

    def callback(self, samples, frames, timing, flags):
        self.last_callback = time.time()
        self.last_callback_monotonic = time.monotonic()
        if flags:
            self.overruns += 1
        block = (self.position, self.last_callback + timing.inputBufferAdcTime - timing.currentTime,
                 samples.copy(), bool(flags))
        self.position += frames
        try:
            self.blocks.put_nowait(block)
        except queue.Full:
            self.dropped_frames += frames


def record(config, sd):
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    storage = Storage(config)
    storage.cleanup(recover=True)
    storage.check()
    device, info = select_device(sd.query_devices(), config.device_match)
    sd.check_input_settings(device=device, channels=config.channels, dtype="int16", samplerate=config.sample_rate)
    audio = AudioQueue(config)
    started = time.time()
    published_at = None
    published_path = None
    published_count = 0

    def published(path, frames):
        nonlocal published_at, published_path, published_count
        published_at, published_path = time.time(), str(path)
        published_count += 1
        log("chunk_published", path=str(path), frames=frames, sample_rate=config.sample_rate)

    writer = ChunkWriter(config, storage, published)
    expected = None
    origin = None
    origin_position = 0
    last_report = 0
    reported_loss = (0, 0)

    def consume(block):
        nonlocal expected, origin, origin_position
        position, captured, samples, flagged = block
        if expected is None or position != expected or flagged:
            if expected is not None:
                log("audio_gap", level=logging.WARNING,
                    dropped_frames=max(0, position - expected), host_status=flagged,
                    note="PortAudio loss duration may be unknown")
            writer.publish()
            origin, origin_position = captured, position
        writer.write(mono(samples, config.channel_mode), origin + (position - origin_position) / config.sample_rate)
        expected = position + len(samples)

    def report(state):
        nonlocal reported_loss
        loss = (audio.dropped_frames, audio.overruns)
        if loss != reported_loss:
            log("audio_loss", level=logging.WARNING, dropped_frames=loss[0], host_status_events=loss[1])
            reported_loss = loss
        publish_status(config.status_path, state=state, session_started_at=started,
                       last_callback_at=audio.last_callback, last_published_at=published_at,
                       last_published_path=published_path, chunks_published=published_count,
                       free_bytes=storage.free, min_free_bytes=config.min_free_disk_mb * 1024**2,
                       recordings_bytes=storage.used, max_recordings_bytes=config.max_recordings_mb * 1024**2,
                       chunk_seconds=config.chunk_seconds, queue_seconds=config.queue_seconds,
                       callback_timeout_seconds=config.callback_timeout, queued_blocks=audio.blocks.qsize(),
                       dropped_frames=audio.dropped_frames, host_status_events=audio.overruns)

    log("capture_start", device=info["name"], device_index=device,
        capture_channels=config.channels, output_channels=1, dtype="int16", sample_rate=config.sample_rate,
        channel_mode=config.channel_mode, chunk_seconds=config.chunk_seconds,
        retention_hours=config.retention_hours, max_recordings_mb=config.max_recordings_mb)
    try:
        report("initializing")
        with sd.InputStream(device=device, channels=config.channels, samplerate=config.sample_rate,
                            dtype="int16", blocksize=1024, callback=audio.callback) as stream:
            while not stop.is_set():
                if not stream.active or time.monotonic() - audio.last_callback_monotonic > config.callback_timeout:
                    raise RuntimeError("audio stream stopped or callbacks stalled")
                try:
                    consume(audio.blocks.get(timeout=0.2))
                except queue.Empty:
                    pass
                if time.monotonic() - last_report >= 1:
                    storage.check()
                    report("recording")
                    last_report = time.monotonic()
        # No new callbacks after closing the stream: draining is bounded.
        while not audio.blocks.empty():
            consume(audio.blocks.get_nowait())
        writer.publish()
        report("stopped")
    except Exception:
        # Never publish a partial chunk after a possible storage failure.
        log("capture_interrupted", level=logging.ERROR, unpublished_frames=writer.frames if writer.file else 0,
            queued_blocks_discarded=audio.blocks.qsize(), dropped_frames=audio.dropped_frames,
            host_status_events=audio.overruns, partial_path=str(writer.path))
        writer.abort()
        raise
