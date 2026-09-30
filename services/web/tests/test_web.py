from dataclasses import replace
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException

from birdlog_web.app import create_app
from birdlog_web.config import Config
from birdlog_web.seed import seed
from birdlog_web.status import read_status
from birdlog_web.store import Filters, Store, Unavailable


@pytest.fixture
def site(tmp_path):
    seed(tmp_path)
    config = Config(database=tmp_path / 'db/bird-log.sqlite3', clips=tmp_path / 'clips', cache=tmp_path / 'cache',
                    recorder_status=tmp_path / 'status/recorder/status.json', analyzer_status=tmp_path / 'status/analyzer/status.json',
                    hosts=('testserver', '127.0.0.1'), page_size=2)
    app = create_app(config)
    with TestClient(app) as client:
        yield config, app, client


def identifier(config):
    with sqlite3.connect(config.database) as db:
        return db.execute("SELECT id FROM encounters WHERE clip_state='available' ORDER BY start DESC LIMIT 1").fetchone()[0]


def test_demo_schema_matches_analyzer():
    path = Path(__file__).resolve().parents[2] / 'analyzer/src/birdlog_analyzer/database.py'
    spec = importlib.util.spec_from_file_location('contract', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from birdlog_web import seed as seed_module
    assert Path(seed_module.__file__).with_name('schema.sql').read_text() == module.SCHEMA


def test_pages_and_filters(site):
    config, app, client = site
    for path in ('/', '/encounters', '/species', '/status'):
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert 'Bird Log' in response.text
    filters = Filters({'review': 'correct', 'score': '0.8'}, config.timezone)
    rows, total, species, hourly, species_count = app.state.store.results(filters)
    assert all(r['review_status'] == 'correct' and r['score'] >= .8 for r in rows)
    assert total == sum(s['count'] for s in species) == sum(h[1] for h in hourly)
    all_filters = Filters({}, config.timezone)
    first = app.state.store.results(all_filters, 1)[0]
    second = app.state.store.results(all_filters, 2)[0]
    assert len(first) == len(second) == 2
    assert not {r['id'] for r in first} & {r['id'] for r in second}
    assert client.get('/encounters?review=bad').status_code == 400
    assert client.get('/encounters?score=nan').status_code == 400
    assert client.get('/encounters?page=10001').status_code == 400
    assert 'No encounters found' in client.get('/encounters?species=absent').text


@pytest.mark.parametrize('day,hours', [('2026-03-08', 23), ('2026-11-01', 25)])
def test_dst_boundaries(day, hours):
    filters = Filters({'since': day, 'until': day}, 'America/Toronto')
    _, args = filters.sql()
    assert args[1] - args[0] == hours * 3600
    chart = filters.chart([])
    assert len(chart) == hours
    if hours == 25:
        assert any('01:00 -0400' in label for label, _ in chart)
        assert any('01:00 -0500' in label for label, _ in chart)


def test_utc_query_local_day(site):
    config, app, _ = site
    filters = Filters({'since': '2026-11-01', 'until': '2026-11-01'}, config.timezone)
    _, args = filters.sql()
    with sqlite3.connect(config.database) as db:
        ids = [r[0] for r in db.execute('SELECT id FROM encounters LIMIT 4')]
        db.execute('DELETE FROM detections')
        db.execute('DELETE FROM encounters WHERE id NOT IN (?,?,?,?)', ids)
        for key, start in zip(ids, [args[0] - 1, args[0], args[1] - 1, args[1]]):
            db.execute('UPDATE encounters SET start=?,end=? WHERE id=?', (start, start + 3, key))
    rows, total, species, hourly, species_count = app.state.store.results(filters)
    assert total == 2
    assert total == sum(s['count'] for s in species) == sum(h[1] for h in hourly)


def review(client, key, version, status='correct', origin='http://testserver'):
    return client.post(f'/encounters/{key}/review', data={'csrf': client.cookies.get('birdlog_csrf'), 'version': version, 'status': status},
                       headers={'Origin': origin}, follow_redirects=False)


def test_reviews_persist_conflict_and_preserve_evidence(site):
    config, app, client = site
    key = identifier(config)
    row = app.state.store.encounter(key)
    client.get(f'/encounters/{key}')
    before = {k: v for k, v in row.items() if not k.startswith('review_')}
    response = review(client, key, row['review_version'])
    assert response.status_code == 303
    after = Store(config).encounter(key)
    assert after['review_status'] == 'correct' and after['review_updated_at']
    assert {k: v for k, v in after.items() if not k.startswith('review_')} == before
    assert review(client, key, row['review_version'], 'incorrect').status_code == 409
    assert review(client, key, after['review_version'], 'uncertain').status_code == 303
    assert review(client, key, after['review_version'] + 1, 'unreviewed').status_code == 303


def test_review_security_and_escaping(site):
    config, app, client = site
    key = identifier(config)
    with sqlite3.connect(config.database) as db:
        db.execute('UPDATE encounters SET common_name=? WHERE id=?', ('<script>alert(1)</script>', key))
    page = client.get(f'/encounters/{key}')
    assert '&lt;script&gt;' in page.text and '<script>alert(1)</script>' not in page.text
    version = app.state.store.encounter(key)['review_version']
    assert review(client, key, version, origin='https://other.invalid').status_code == 403
    assert review(client, key, version, status='bad').status_code == 400
    assert client.post(f'/encounters/{key}/review', data={'status': 'correct', 'version': version}).status_code == 403
    assert client.get(f'/encounters/{key}/review').status_code == 405
    assert client.get('/', headers={'Host': 'attacker.invalid'}).status_code == 400


def test_db_contention_is_bounded_and_readers_continue(site):
    config, app, client = site
    key = identifier(config)
    client.get(f'/encounters/{key}')
    writer = sqlite3.connect(config.database)
    writer.execute('BEGIN IMMEDIATE')
    try:
        assert client.get('/').status_code == 200
        start = time.monotonic()
        assert review(client, key, 0).status_code == 503
        assert time.monotonic() - start < 2
    finally:
        writer.rollback()
        writer.close()


def test_setup_schema_and_upstream_independence(site):
    config, app, client = site
    config.recorder_status.unlink()
    config.analyzer_status.write_text('{broken')
    assert client.get('/').status_code == 200
    assert client.get('/healthz').json()['ready']
    with sqlite3.connect(config.database) as db:
        db.execute('PRAGMA user_version=2')
    assert client.get('/').status_code == 503
    assert 'Unsupported' in client.get('/').text
    assert client.get('/healthz').json() == {'alive': True, 'ready': False, 'database': 'Unsupported metadata schema version 2; update the web service.'}
    config.database.unlink()
    assert 'Waiting for the analyzer' in client.get('/').text
    assert not config.database.exists()


def test_audio_ranges_and_retention(site):
    config, app, client = site
    key = identifier(config)
    full = client.get(f'/encounters/{key}/audio')
    assert full.status_code == 200 and full.content.startswith(b'RIFF')
    partial = client.get(f'/encounters/{key}/audio', headers={'Range': 'bytes=12-31'})
    assert partial.status_code == 206 and partial.content == full.content[12:32]
    suffix = client.get(f'/encounters/{key}/audio', headers={'Range': 'bytes=-20'})
    assert suffix.content == full.content[-20:]
    assert client.head(f'/encounters/{key}/audio').headers['content-length'] == str(len(full.content))
    assert client.get(f'/encounters/{key}/audio', headers={'Range': 'bytes=999999999-'}).status_code == 416
    (config.clips / f'{key}.wav').unlink()
    assert client.get(f'/encounters/{key}/audio').status_code == 404
    assert 'Audio unavailable' in client.get(f'/encounters/{key}').text


def test_path_rejection_and_open_retention_race(site, tmp_path):
    config, app, client = site
    key = identifier(config)
    path = config.clips / (key + '.wav')
    with app.state.media.open(key) as source:
        path.unlink()
        assert source.read(4) == b'RIFF'  # already-open playback safely finishes
    outside = tmp_path / 'secret.wav'
    outside.write_bytes(b'not public')
    path.symlink_to(outside)
    assert client.get(f'/encounters/{key}/audio').status_code == 404
    with sqlite3.connect(config.database) as db:
        db.execute('UPDATE encounters SET clip_path=? WHERE id=?', ('../secret.wav', key))
    assert client.get(f'/encounters/{key}/audio').status_code == 404
    assert client.get('/encounters/invalid/audio').status_code == 404


def test_spectrogram_cache_expiry_source_change_and_concurrency(site):
    config, app, client = site
    key = identifier(config)
    url = f'/encounters/{key}/spectrogram'
    image = client.get(url)
    assert image.status_code == 200 and image.content.startswith(b'\x89PNG')
    cached = list(config.cache.glob('*.png'))
    assert len(cached) == 1 and client.get(url).content == image.content
    path = config.clips / (key + '.wav')
    import os
    os.utime(path, ns=(time.time_ns(), time.time_ns() + 10))
    assert client.get(url).status_code == 200
    assert len(list(config.cache.glob('*.png'))) == 1
    assert not cached[0].exists()
    app.state.media.lock.acquire()
    try:
        assert client.get(url).status_code == 503
    finally:
        app.state.media.lock.release()
    with sqlite3.connect(config.database) as db:
        db.execute("UPDATE encounters SET clip_state='expired' WHERE id=?", (key,))
    assert client.get(url).status_code == 404
    app.state.media.sweep()
    assert not list(config.cache.glob('*.png'))


def test_status_health_and_staleness(site):
    config, _, _ = site
    doc = json.loads(config.analyzer_status.read_text())
    now = doc['updated_at']
    assert read_status(config.analyzer_status, 'analyzer', now)['health'] == 'healthy'
    assert read_status(config.analyzer_status, 'analyzer', now + 20)['health'] == 'stale'
    doc['state'] = 'retrying'
    config.analyzer_status.write_text(json.dumps(doc))
    assert read_status(config.analyzer_status, 'analyzer', now)['health'] == 'unhealthy'
    doc['updated_at'] = 'invalid'
    config.analyzer_status.write_text(json.dumps(doc))
    assert read_status(config.analyzer_status, 'analyzer', now)['health'] == 'unknown'
    assert read_status(config.recorder_status, 'recorder', now)['health'] == 'healthy'
    assert read_status(config.recorder_status, 'recorder', now + 11)['health'] == 'stale'


def test_config_limits(tmp_path):
    for args in ({'page_size': 101}, {'refresh': 1}, {'hosts': ('*',)}, {'cache_mb': 0}, {'timezone': 'bad/zone'}):
        with pytest.raises((ValueError, KeyError)):
            Config(**args)


def test_initializing_and_minimal_failure_status(site):
    config, _, _ = site
    now = time.time()
    config.analyzer_status.write_text(json.dumps(dict(schema_version=1, updated_at=now, state='initializing', model_ready=False)))
    assert read_status(config.analyzer_status, 'analyzer', now)['health'] == 'initializing'
    assert read_status(config.analyzer_status, 'analyzer', now + 121)['health'] == 'stale'
    config.analyzer_status.write_text(json.dumps(dict(schema_version=1, updated_at=now, state='unhealthy', model_ready=False)))
    assert read_status(config.analyzer_status, 'analyzer', now)['health'] == 'unhealthy'


def test_cache_age_and_size_limits(site):
    config, app, client = site
    key = identifier(config)
    path = config.cache / (key + '-old.png')
    path.write_bytes(b'image')
    partial = config.cache / (key + '-interrupted.partial')
    partial.write_bytes(b'partial')
    import os
    os.utime(path, (0, 0))
    app.state.media.sweep()
    assert not path.exists() and not partial.exists()
    app.state.media.config = replace(config, cache_mb=1)
    path.write_bytes(b'x' * (1024**2 + 1))
    app.state.media.sweep()
    assert not path.exists()


def test_media_failure_does_not_break_browsing(site):
    config, app, client = site
    key = identifier(config)
    (config.clips / f'{key}.wav').write_bytes(b'RIFF malformed')
    assert client.get(f'/encounters/{key}/spectrogram').status_code == 503
    assert client.get(f'/encounters/{key}').status_code == 200
    assert client.get(f'/encounters/{key}/audio').status_code == 200


def test_fractional_offset_hour_grouping(site):
    config, app, _ = site
    filters = Filters({}, 'Asia/Kathmandu')
    _, total, _, hourly, _ = app.state.store.results(filters)
    chart = filters.chart(hourly)
    assert sum(value for _, value in chart) == total


def test_review_form_limits_and_no_mutation_for_invalid_filters(site):
    config, app, client = site
    key = identifier(config)
    client.get(f'/encounters/{key}')
    version = app.state.store.encounter(key)['review_version']
    headers = {'Origin': 'http://testserver', 'Content-Type': 'application/x-www-form-urlencoded'}
    assert client.post(f'/encounters/{key}/review', content='x=' + 'a' * 5000, headers=headers).status_code == 413
    assert client.post(f'/encounters/{key}/review', content='&'.join(f'x{i}=a' for i in range(30)), headers=headers).status_code == 400
    response = client.post(f'/encounters/{key}/review?review=bad', data={'csrf': client.cookies.get('birdlog_csrf'), 'version': version, 'status': 'correct'}, headers={'Origin': 'http://testserver'})
    assert response.status_code == 400
    assert app.state.store.encounter(key)['review_version'] == version
