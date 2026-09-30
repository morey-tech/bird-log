"""Standalone browser check: python services/web/tests/browser_smoke.py (Chromium required)."""
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright
from birdlog_web.seed import seed


def wait_for(locator, predicate):
    for _ in range(100):
        if locator.evaluate(predicate):
            return
        time.sleep(.1)
    raise AssertionError(f"Browser condition timed out: {predicate}")


def main():
    with tempfile.TemporaryDirectory(prefix='birdlog-browser-') as folder:
        root = Path(folder)
        seed(root)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {**os.environ, 'WEB_HOST': '127.0.0.1', 'WEB_PORT': str(port), 'DATABASE_PATH': str(root / 'db/bird-log.sqlite3'),
               'CLIPS_DIR': str(root / 'clips'), 'RECORDER_STATUS_PATH': str(root / 'status/recorder/status.json'),
               'ANALYZER_STATUS_PATH': str(root / 'status/analyzer/status.json'), 'SPECTROGRAM_CACHE_DIR': str(root / 'cache')}
        server = subprocess.Popen([sys.executable, '-m', 'birdlog_web'], env=env, stdout=subprocess.DEVNULL)
        base = f'http://127.0.0.1:{port}'
        try:
            for _ in range(100):
                try:
                    urllib.request.urlopen(base + '/healthz', timeout=.5).close()
                    break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError('Server did not start')
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
                page = browser.new_page(viewport={'width': 1280, 'height': 900})
                errors = []
                external = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: external.append(request.url) if not request.url.startswith(base) else None)
                page.goto(base)
                assert page.get_by_role('heading', name='Overview', exact=True).is_visible()
                page.screenshot(path='/tmp/bird-log-web-desktop.png', full_page=True)
                page.get_by_label('Review', exact=True).select_option('correct')
                page.get_by_role('button', name='Apply filters').click()
                assert 'review=correct' in page.url
                assert page.locator('.badge').all_text_contents() == ['correct'] * page.locator('.badge').count()
                page.get_by_label('Species', exact=True).fill('absent')
                page.get_by_role('button', name='Apply filters').click()
                assert page.get_by_text('No encounters found.', exact=True).is_visible()
                with sqlite3.connect(root / 'db/bird-log.sqlite3') as db:
                    key = db.execute("SELECT id FROM encounters WHERE clip_state='available' LIMIT 1").fetchone()[0]
                page.goto(base + '/encounters/' + key)
                page.locator('audio').evaluate('(audio) => audio.play()')
                wait_for(page.locator("audio"), "audio => audio.currentTime > 0")
                page.locator('audio').evaluate('(audio) => { audio.pause(); audio.currentTime = 1.5; }')
                wait_for(page.locator("audio"), "audio => audio.currentTime >= 1.4")
                wait_for(page.locator(".spectrogram"), "image => image.naturalWidth > 0")
                page.get_by_label('Review status').select_option('uncertain')
                page.get_by_role('button', name='Save review').click()
                assert page.get_by_text('Review saved.', exact=True).is_visible()
                page.reload()
                assert page.get_by_label('Review status').input_value() == 'uncertain'
                (root / 'clips' / f'{key}.wav').unlink()
                page.reload()
                assert page.get_by_text('Audio unavailable:', exact=False).is_visible()
                page.set_viewport_size({'width': 390, 'height': 844})
                page.goto(base + '/status')
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.screenshot(path='/tmp/bird-log-web-mobile.png', full_page=True)
                with sqlite3.connect(root / 'db/bird-log.sqlite3') as db:
                    db.execute('PRAGMA user_version=99')
                page.goto(base)
                assert page.get_by_text('Unsupported metadata schema', exact=False).is_visible()
                assert not errors, errors
                assert not external, external
                browser.close()
            print('Browser smoke passed: filters, empty/error states, playback/seek, spectrogram, review persistence, mobile layout, offline assets.')
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == '__main__':
    main()
