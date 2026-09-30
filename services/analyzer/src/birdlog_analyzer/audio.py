from datetime import datetime, timezone
import os
from pathlib import Path
import re

import soundfile as sf

from .config import RATE

NAME = re.compile(r"^(\d{8}T\d{6}\.\d{6}Z)_[0-9a-f]{32}\.wav$")


def discover(root):
    if not root.is_dir():
        raise OSError(f"Recordings mount is unavailable: {root}")
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in files:
            path = Path(directory) / name
            match = NAME.fullmatch(name)
            if not match or path.is_symlink():
                continue
            try:
                stamp = datetime.strptime(match[1], "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if path.parent.relative_to(root).as_posix() == stamp.strftime("%Y/%m/%d"):
                yield path.relative_to(root).as_posix(), stamp.timestamp()


class InvalidAudio(ValueError):
    pass


def read_recording(path, max_seconds):
    # Open once so an already-open file remains readable if recorder retention unlinks it.
    with path.open("rb") as source:
        try:
            with sf.SoundFile(source) as audio:
                if (audio.format != "WAV" or audio.subtype != "PCM_16" or audio.channels != 1
                        or audio.samplerate != RATE or not 0 < audio.frames <= RATE * max_seconds):
                    raise InvalidAudio(f"Expected mono PCM_16 WAV at {RATE} Hz, 0 < duration <= {max_seconds}s")
                samples = audio.read(dtype="int16")
                if len(samples) != audio.frames:
                    raise InvalidAudio("Truncated WAV")
                return samples
        except sf.LibsndfileError as error:
            raise InvalidAudio(str(error)) from error
