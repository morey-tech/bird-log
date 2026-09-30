"""Synthetic development evidence, never actual bird identifications."""
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import wave
from zoneinfo import ZoneInfo


def seed(directory):
    root = Path(directory)
    db_path = root / 'db/bird-log.sqlite3'
    if db_path.exists():
        raise ValueError('Demo database already exists; choose a fresh directory.')
    for child in ('db', 'clips', 'status/recorder', 'status/analyzer', 'web-cache/spectrograms', 'status-examples'):
        (root / child).mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    try:
        db.executescript(Path(__file__).with_name('schema.sql').read_text())
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('INSERT INTO configuration VALUES (?,?)', ('demo', '{"synthetic":true}'))
        species = [('Cardinalis cardinalis', 'Northern Cardinal'), ('Poecile atricapillus', 'Black-capped Chickadee'),
                   ('Turdus migratorius', 'American Robin')]
        today = datetime.now(ZoneInfo('America/Toronto')).replace(hour=7, minute=0, second=0, microsecond=0)
        for i in range(18):
            scientific, common = species[i % 3]
            identifier = hashlib.sha256(f'demo-{i}'.encode()).hexdigest()
            start = (today - timedelta(days=i // 6) + timedelta(minutes=(i % 6) * 35)).timestamp()
            clip_state = ('available', 'missing', 'expired')[i % 3]
            review = ('unreviewed', 'correct', 'incorrect', 'uncertain')[i % 4]
            db.execute('INSERT INTO encounters(id,species,scientific_name,common_name,start,end,score,config_id,model_version,state,clip_path,clip_state,clip_start,clip_end,review_status,review_updated_at,review_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (identifier, scientific + '_' + common, scientific, common, start, start + 3, 0.6 + (i % 4) * .1,
                        'demo', 'synthetic-demo (not BirdNET inference)', 'closed', identifier + '.wav', clip_state,
                        start, start + 3, review, start if review != 'unreviewed' else None, int(review != 'unreviewed')))
            db.execute('INSERT INTO windows VALUES (?,?,?,?,?,?)', (identifier, start, start + 3, 0, '[]', 'demo'))
            db.execute('INSERT INTO detections VALUES (?,?,?,?,?,?,?)', (identifier, identifier, identifier, scientific + '_' + common, start, start + 3, .6 + (i % 4) * .1))
            if clip_state == 'available':
                import numpy as np
                samples = (np.sin(np.arange(48000 * 3) * 2 * np.pi * (1000 + i * 100) / 48000) * 3000).astype('<i2')
                with wave.open(str(root / 'clips' / (identifier + '.wav')), 'wb') as wav:
                    wav.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
                    wav.writeframes(samples.tobytes())
        db.commit()
    finally:
        db.close()
    now = time.time()
    recorder = dict(schema_version=1, updated_at=now, state='recording', last_callback_at=now, callback_timeout_seconds=5,
                    last_published_at=now, session_started_at=now, chunk_seconds=30, queue_seconds=5,
                    free_bytes=20 * 1024**3, min_free_bytes=1024**3, recordings_bytes=1024**3, max_recordings_bytes=10 * 1024**3)
    analyzer = dict(schema_version=1, updated_at=now, state='idle', scan_seconds=5, stall_seconds=300, model_ready=True,
                    last_scan_at=now, last_progress_at=now, last_processed_recording='demo.wav', pending_recordings=0,
                    oldest_pending_at=None, analysis_seconds_per_audio_second=.2, clip_free_bytes=20 * 1024**3,
                    database_free_bytes=20 * 1024**3, min_free_bytes=1024**3, failed_recordings=0, missing_recordings=0)
    for service, document in [('recorder', recorder), ('analyzer', analyzer)]:
        (root / f'status/{service}/status.json').write_text(json.dumps(document))
        (root / f'status-examples/{service}-healthy.json').write_text(json.dumps(document))
        (root / f'status-examples/{service}-stale.json').write_text(json.dumps({**document, 'updated_at': now - 3600}))
        (root / f'status-examples/{service}-unhealthy.json').write_text(json.dumps({**document, 'state': 'unhealthy'}))
    (root / 'status-examples/malformed.json').write_text('{invalid')
    print(f'Synthetic demo created in {root}. Status heartbeats intentionally become stale unless republished.')
