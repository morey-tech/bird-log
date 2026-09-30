"""Only analyzer-owned, finalized flat WAV files are readable. No browser paths."""
import hashlib
import io
import logging
import os
import re
import stat
import threading
import time
import wave

from fastapi import HTTPException

from .store import Unavailable

ID = re.compile(r'[0-9a-f]{64}')


class Media:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.lock = threading.Lock()
        config.cache.mkdir(parents=True, exist_ok=True)

    def open(self, identifier):
        if not ID.fullmatch(identifier):
            raise HTTPException(404, 'Encounter not found.')
        row = self.store.encounter(identifier)
        if not row:
            raise HTTPException(404, 'Encounter not found.')
        # Analyzer names clips from stable IDs; reject even DB-sourced arbitrary paths.
        if row['state'] != 'closed' or row['clip_state'] != 'available' or row['clip_path'] != identifier + '.wav':
            raise HTTPException(404, 'Audio is unavailable or has expired.')
        root_fd = None
        try:
            root_fd = os.open(self.config.clips, os.O_RDONLY | os.O_DIRECTORY)
            fd = os.open(identifier + '.wav', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
            source = os.fdopen(fd, 'rb')
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 24 * 1024**2:
                source.close()
                raise HTTPException(404, 'Audio is unavailable.')
            return source
        except OSError:
            raise HTTPException(404, 'Audio is unavailable or has expired.') from None
        finally:
            if root_fd is not None:
                os.close(root_fd)

    def available(self, identifier):
        try:
            with self.open(identifier):
                return True
        except HTTPException:
            return False

    def sweep(self):
        # Caller holds the single generation/cache lock. No retained image is served without a live source check.
        entries = []
        for partial in self.config.cache.glob('*.partial'):
            partial.unlink(missing_ok=True)
        for p in self.config.cache.glob('*.png'):
            try:
                identifier = p.name.split('-')[0]
                info = p.lstat()
                if p.is_symlink() or time.time() - info.st_mtime > self.config.cache_seconds or not self.available(identifier):
                    p.unlink(missing_ok=True)
                else:
                    entries.append((info.st_mtime, info.st_size, p))
            except (OSError, HTTPException, Unavailable):
                p.unlink(missing_ok=True)
        total = sum(size for _, size, _ in entries)
        entries.sort()
        while entries and (total > self.config.cache_mb * 1024**2 or len(entries) > 512):
            _, size, p = entries.pop(0)
            p.unlink(missing_ok=True)
            total -= size

    def spectrogram(self, identifier):
        if not self.lock.acquire(blocking=False):
            raise HTTPException(503, 'Spectrogram generation is busy; retry shortly.', headers={'Retry-After': '5'})
        try:
            with self.open(identifier) as source:
                info = os.fstat(source.fileno())
                fingerprint = hashlib.sha256(f'{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}'.encode()).hexdigest()[:24]
                path = self.config.cache / f'{identifier}-{fingerprint}.png'
                self.sweep()
                if path.is_file() and not path.is_symlink():
                    return path.read_bytes()
                with wave.open(source) as wav:
                    if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 48000 or not 0 < wav.getnframes() <= 48000 * 125:
                        raise ValueError('Unsupported clip format/duration')
                    import numpy as np
                    samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype('float32') / 32768
                from matplotlib.figure import Figure
                from matplotlib.backends.backend_agg import FigureCanvasAgg
                figure = Figure(figsize=(9, 3), dpi=100, layout='constrained')
                FigureCanvasAgg(figure)
                axis = figure.subplots()
                axis.specgram(samples, NFFT=1024, Fs=48000, noverlap=512, cmap='magma', vmin=-100, vmax=-20)
                axis.set(xlabel='Time from clip start (seconds)', ylabel='Frequency (Hz)', ylim=(0, 15000))
                output = io.BytesIO()
                figure.savefig(output, format='png')
                payload = output.getvalue()
                # Retention may have deleted or replaced the source during generation.
                with self.open(identifier) as current:
                    current_info = os.fstat(current.fileno())
                    if (current_info.st_ino, current_info.st_mtime_ns) != (info.st_ino, info.st_mtime_ns):
                        raise HTTPException(404, 'Audio changed; retry shortly.')
                for old in self.config.cache.glob(identifier + '-*.png'):
                    old.unlink(missing_ok=True)
                temporary = path.with_suffix('.partial')
                temporary.write_bytes(payload)
                os.replace(temporary, path)
                self.sweep()
                return payload
        except HTTPException:
            raise
        except Exception as error:
            logging.warning('{"event":"spectrogram_error","type":"%s"}', type(error).__name__)
            raise HTTPException(503, 'Spectrogram unavailable; audio and metadata remain accessible.') from error
        finally:
            self.lock.release()
