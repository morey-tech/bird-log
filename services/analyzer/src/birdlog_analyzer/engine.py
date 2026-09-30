"""Plan inference outside SQLite write transactions; atomically checkpoint each input."""
import hashlib
import json
import time

import numpy as np

from . import database
from .audio import InvalidAudio
from .config import RATE, WINDOW


def key(*parts):
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


class Engine:
    def __init__(self, config, db, predictor):
        self.config, self.db, self.predictor = config, db, predictor
        self.config_id, _ = config.identity(predictor.identity)
        self.state, audio = database.snapshot(db)
        self.audio = np.frombuffer(audio, dtype="<i2").copy()
        self.encounters = {row["id"]: dict(row) for row in db.execute("SELECT * FROM encounters WHERE state='open'")}
        self.active = {row["species"]: row for row in self.encounters.values()}
        self.windows, self.detections, self.gaps = [], [], []

    def feed(self, path, captured, samples):
        last_end = self.state["last_end"]
        if last_end is not None and captured < last_end - 1 / RATE:
            raise InvalidAudio("Late or overlapping input cannot be appended to the committed timeline")
        segment = self.state["segment"]
        if segment and abs(captured - last_end) > 1 / RATE:
            self.finish("recording_gap")
        if last_end is not None and captured - last_end > 1 / RATE:
            self.gaps.append((last_end, captured, "capture_gap", time.time()))
        if self.state["segment"] is None:
            self.state["segment"] = {"id": path, "origin": captured, "total": 0, "cursor": 0, "covered": 0,
                                     "buffer_start": 0, "sources": []}
            self.audio = np.empty(0, dtype="<i2")
        segment = self.state["segment"]
        segment["sources"].append({"path": path, "start_frame": segment["total"],
                                   "end_frame": segment["total"] + len(samples)})
        self.audio = np.concatenate((self.audio, samples))
        segment["total"] += len(samples)
        self.state["last_end"] = segment["origin"] + segment["total"] / RATE
        self.state["last_input_at"] = time.time()
        while segment["cursor"] + WINDOW <= segment["total"]:
            self.window(WINDOW)
        self.sweep(segment["origin"] + segment["covered"] / RATE)
        keep_seconds = (self.config.max_encounter_seconds + self.config.encounter_gap_seconds
                        + 2 * self.config.clip_padding_seconds + 6)
        keep_from = max(0, segment["total"] - int(keep_seconds * RATE))
        self.audio = self.audio[keep_from - segment["buffer_start"]:]
        segment["buffer_start"] = keep_from
        segment["sources"] = [source for source in segment["sources"] if source["end_frame"] > keep_from]

    def window(self, count):
        segment = self.state["segment"]
        position = segment["cursor"]
        start = segment["origin"] + position / RATE
        end = start + count / RATE
        offset = position - segment["buffer_start"]
        audio = self.audio[offset:offset + count].astype(np.float32) / 32768
        if count < WINDOW:
            audio = np.pad(audio, (0, WINDOW - count))
        results = self.predictor.predict(audio, start)
        window_id = key(self.config_id, segment["id"], position)
        sources = [source for source in segment["sources"]
                   if source["start_frame"] < position + count and source["end_frame"] > position]
        self.windows.append((window_id, start, end, int(count < WINDOW), json.dumps(sources), self.config_id))
        for species, score in sorted(results, key=lambda item: (-item[1], item[0]))[:self.config.max_species_per_window]:
            if not self.config.min_score <= score <= 1:
                continue
            detection_id = key(window_id, species)
            encounter = self.active.get(species)
            if encounter and (start - encounter["end"] > self.config.encounter_gap_seconds + 1e-6
                              or end - encounter["start"] > self.config.max_encounter_seconds + 1e-6):
                self.close(encounter, "gap_or_duration")
                encounter = None
            if encounter is None:
                scientific, separator, common = species.partition("_")
                encounter = dict(id=detection_id, species=species, scientific_name=scientific,
                                 common_name=common if separator else scientific, start=start, end=end,
                                 score=float(score), config_id=self.config_id, model_version=self.predictor.identity,
                                 state="open", clip_path=None, clip_state="pending", clip_start=None,
                                 clip_end=None, padding_limited=0, close_reason=None, clip_audio=None)
                self.encounters[encounter["id"]] = encounter
                self.active[species] = encounter
            encounter["end"] = end
            encounter["score"] = max(encounter["score"], float(score))
            self.detections.append((detection_id, window_id, encounter["id"], species, start, end, float(score)))
        segment["covered"] = max(segment["covered"], position + count)
        segment["cursor"] += round(self.config.window_hop_seconds * RATE)
        self.sweep(end)

    def sweep(self, analyzed_until):
        available_until = self.state["last_end"]
        for encounter in list(self.active.values()):
            silence = analyzed_until - encounter["end"] > self.config.encounter_gap_seconds + 1e-6
            maximum = encounter["end"] - encounter["start"] >= self.config.max_encounter_seconds - 1e-6
            if (silence or maximum) and available_until >= encounter["end"] + self.config.clip_padding_seconds:
                self.close(encounter, "duration" if maximum else "silence")

    def close(self, encounter, reason):
        segment = self.state["segment"]
        first = round((encounter["start"] - segment["origin"] - self.config.clip_padding_seconds) * RATE)
        last = round((encounter["end"] - segment["origin"] + self.config.clip_padding_seconds) * RATE)
        actual_first = max(segment["buffer_start"], first)
        actual_last = min(segment["total"], last)
        clip = self.audio[actual_first - segment["buffer_start"]:actual_last - segment["buffer_start"]]
        encounter.update(state="closed", clip_path=f"{encounter['id']}.wav", clip_state="pending",
                         clip_start=segment["origin"] + actual_first / RATE,
                         clip_end=segment["origin"] + actual_last / RATE,
                         padding_limited=int(first != actual_first or last != actual_last), close_reason=reason,
                         clip_audio=clip.astype("<i2", copy=False).tobytes())
        self.active.pop(encounter["species"], None)

    def finish(self, reason="idle"):
        segment = self.state["segment"]
        if segment is None:
            return
        remainder = segment["total"] - segment["cursor"]
        if segment["total"] > segment["covered"]:
            self.window(remainder)
        for encounter in list(self.active.values()):
            self.close(encounter, reason)
        self.state["segment"] = None
        self.audio = np.empty(0, dtype="<i2")

    def commit(self, path=None, frames=None):
        # Inference has completed. This transaction only stores results and checkpoint.
        columns = ("id", "species", "scientific_name", "common_name", "start", "end", "score", "config_id",
                   "model_version", "state", "clip_path", "clip_state", "clip_start", "clip_end",
                   "padding_limited", "close_reason", "clip_audio")
        updates = ",".join(f"{column}=excluded.{column}" for column in columns[1:])
        with self.db:
            for encounter in self.encounters.values():
                self.db.execute(f"INSERT INTO encounters ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                                f"ON CONFLICT(id) DO UPDATE SET {updates}", [encounter[column] for column in columns])
            self.db.executemany("INSERT INTO windows VALUES(?,?,?,?,?,?)", self.windows)
            self.db.executemany("INSERT INTO detections VALUES(?,?,?,?,?,?,?)", self.detections)
            self.db.executemany("INSERT INTO gaps(start,end,reason,observed_at) VALUES(?,?,?,?)", self.gaps)
            database.checkpoint(self.db, self.state, self.audio.astype("<i2", copy=False).tobytes())
            if path:
                self.db.execute("UPDATE recordings SET state='done',frames=?,error=NULL,completed_at=? WHERE path=?",
                                (frames, time.time(), path))
