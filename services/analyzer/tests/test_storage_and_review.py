import sqlite3
import threading
import time

from test_analyzer import Predictor, analyzer, config, recording


def test_review_can_be_written_during_inference_and_is_preserved(config):
    start = round(time.time())
    recording(config, start, 3)
    model = Predictor()
    app = analyzer(config, model)
    app.scan()
    app.process_next()
    original = model.predict

    def review_during_prediction(samples, captured):
        with sqlite3.connect(config.database_path, timeout=0.1) as web:
            web.execute("UPDATE encounters SET review_status='incorrect',review_version=review_version+1")
        return original(samples, captured)

    model.predict = review_during_prediction
    recording(config, start + 3, 3)
    app.run(threading.Event(), once=True)
    row = app.db.execute("SELECT review_status,review_version FROM encounters").fetchone()
    assert tuple(row) == ("incorrect", 1)
    app.db.close()


def test_clip_budget_evicts_oldest_without_deleting_metadata(config):
    from dataclasses import replace
    config = replace(config, max_clip_storage_mb=1)
    start = round(time.time())
    for index in range(4):
        recording(config, start + index * 10, 3)
    app = analyzer(config)
    app.run(threading.Event(), once=True)
    rows = app.db.execute("SELECT clip_state FROM encounters ORDER BY start").fetchall()
    assert [row[0] for row in rows] == ["expired", "available", "available", "available"]
    assert app.clips.usage() <= 1024**2
    assert len(list(config.recordings_dir.rglob("*.wav"))) == 4
    app.db.close()


def test_reconcile_missing_and_interrupted_expiration(config):
    start = round(time.time())
    recording(config, start, 3)
    recording(config, start + 10, 3)
    app = analyzer(config)
    app.run(threading.Event(), once=True)
    rows = app.db.execute("SELECT id FROM encounters ORDER BY start").fetchall()
    app.clips.path(rows[0][0]).unlink()
    with app.db:
        app.db.execute("UPDATE encounters SET clip_state='expiring' WHERE id=?", (rows[1][0],))
    app.clips.reconcile()
    assert [row[0] for row in app.db.execute("SELECT clip_state FROM encounters ORDER BY start")] == ["missing", "expired"]
    assert not list(config.clips_dir.glob("*.wav"))
    app.db.close()
