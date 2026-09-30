import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Config
from .media import Media, ID
from .status import read_status
from .store import Conflict, Filters, REVIEWS, Store, Unavailable

ROOT = Path(__file__).parent


def create_app(config=None):
    config = config or Config.from_env()
    store = Store(config)
    media = Media(config, store)

    async def cleanup():
        while True:
            await asyncio.sleep(60)
            def sweep():
                if media.lock.acquire(blocking=False):
                    try:
                        media.sweep()
                    except Exception as error:
                        logging.warning(json.dumps({'event': 'cache_cleanup_error', 'type': type(error).__name__}))
                    finally:
                        media.lock.release()
            await asyncio.to_thread(sweep)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(cleanup())
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.media = store, media
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(config.hosts))
    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    templates = Jinja2Templates(directory=ROOT / 'templates')
    templates.env.filters['localtime'] = lambda value: datetime.fromtimestamp(value, ZoneInfo(config.timezone)).strftime('%Y-%m-%d %H:%M:%S %Z (%z)') if value is not None else 'Unavailable'
    templates.env.filters['query'] = lambda value: urlencode(value)

    def render(request, name, status=200, **context):
        token = request.cookies.get('birdlog_csrf', '')
        if not re.fullmatch(r'[0-9a-f]{64}', token):
            token = secrets.token_hex(32)
        response = templates.TemplateResponse(request=request, name=name, context=dict(
            timezone=config.timezone, refresh=config.refresh, csrf=token, reviews=REVIEWS, **context), status_code=status)
        response.set_cookie('birdlog_csrf', token, httponly=True, samesite='strict', secure=request.url.scheme == 'https')
        return response

    @app.middleware('http')
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; media-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Cache-Control'] = 'no-store'
        if response.status_code >= 400:
            logging.warning(json.dumps({'event': 'request_failed', 'method': request.method, 'status': response.status_code}))
        return response

    @app.exception_handler(Unavailable)
    async def unavailable(request, error):
        return render(request, 'message.html', status=503, title='Metadata unavailable', message=str(error))

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        response = render(request, 'message.html', status=error.status_code, title='Request unavailable', message=error.detail)
        response.headers.update(error.headers or {})
        return response

    @app.exception_handler(Exception)
    async def unexpected(request, error):
        logging.error(json.dumps({'event': 'request_error', 'type': type(error).__name__}))
        return render(request, 'message.html', status=500, title='Temporarily unavailable', message='The request could not be completed. Please retry.')

    def filters_for(request, today=False):
        try:
            page = int(request.query_params.get('page', 1))
            if not 1 <= page <= 10000:
                raise ValueError('Page must be between 1 and 10000; narrow your date range for older results.')
            return Filters(request.query_params, config.timezone, today), page
        except (ValueError, OverflowError) as error:
            raise HTTPException(400, str(error)) from None

    def status_data():
        return [read_status(config.recorder_status, 'recorder'), read_status(config.analyzer_status, 'analyzer')]

    @app.get('/healthz')
    def health():
        # Upstream recording/inference health never changes web liveness.
        try:
            with store.connect() as db:
                db.execute('SELECT id FROM encounters LIMIT 1').fetchone()
            return {'alive': True, 'ready': True, 'database': 'ready'}
        except Unavailable as error:
            return JSONResponse({'alive': True, 'ready': False, 'database': str(error)})

    @app.get('/', response_class=HTMLResponse)
    @app.get('/encounters', response_class=HTMLResponse)
    @app.get('/species', response_class=HTMLResponse)
    def listing(request: Request):
        overview = request.url.path == '/'
        history = request.url.path == '/species'
        filters, page = filters_for(request, today=overview)
        rows, total, species, hourly, species_count = store.results(filters, page)
        chart = filters.chart(hourly, daily=history or (filters.until - filters.since).days > 2)
        peak = max((count for _, count in chart), default=1) or 1
        return render(request, 'listing.html', title='Overview' if overview else 'Species history' if history else 'Encounters',
                      filters=filters, rows=rows, total=total, species=species, species_count=species_count, chart=chart, peak=peak,
                      daily=history or (filters.until - filters.since).days > 2, page=page, page_size=config.page_size,
                      params=filters.params(), overview=overview, statuses=status_data(),
                      live=overview, history=history)

    @app.get('/status', response_class=HTMLResponse)
    def system_status(request: Request):
        return render(request, 'status.html', title='System status', statuses=status_data(), live=True)

    @app.get('/encounters/{identifier}', response_class=HTMLResponse)
    def detail(request: Request, identifier: str):
        if not ID.fullmatch(identifier):
            raise HTTPException(404, 'Encounter not found.')
        row = store.encounter(identifier)
        if row is None:
            raise HTTPException(404, 'Encounter not found.')
        filters, page = filters_for(request)
        return render(request, 'detail.html', title=row['common_name'], row=row, detections=store.detections(identifier),
                      available=media.available(identifier), params={**filters.params(), 'page': page},
                      saved=request.query_params.get('saved') == '1')

    @app.post('/encounters/{identifier}/review')
    async def review(request: Request, identifier: str):
        if not ID.fullmatch(identifier):
            raise HTTPException(404, 'Encounter not found.')
        # URL-encoded forms only; no multipart parser or unbounded body allocation.
        if request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
            raise HTTPException(415, 'Unsupported form type.')
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > 4096:
                raise HTTPException(413, 'Review form is too large.')
        try:
            form = parse_qs(payload.decode('utf-8', errors='replace'), max_num_fields=16)
        except ValueError:
            raise HTTPException(400, 'Invalid review form.') from None
        token = form.get('csrf', [''])[0]
        cookie = request.cookies.get('birdlog_csrf', '')
        origin = request.headers.get('origin')
        expected_origin = f'{request.url.scheme}://{request.url.netloc}'
        referer = urlsplit(request.headers.get('referer', ''))
        if (not cookie or not secrets.compare_digest(token, cookie) or
            (origin != expected_origin if origin else f'{referer.scheme}://{referer.netloc}' != expected_origin)
            or request.headers.get('sec-fetch-site') == 'cross-site'):
            raise HTTPException(403, 'Review rejected: reload this page and submit from this dashboard.')
        filters, page = filters_for(request)
        try:
            version = int(form.get('version', ['-1'])[0])
            status = form.get('status', [''])[0]
            await asyncio.to_thread(store.review, identifier, status, version)
        except (ValueError, OverflowError):
            raise HTTPException(400, 'Invalid review state or version.') from None
        except Conflict as error:
            return render(request, 'message.html', status=409, title='Review conflict', message=str(error),
                          retry='/encounters/' + identifier + '?' + str(request.query_params))
        logging.info(json.dumps({'event': 'review_updated', 'encounter_id': identifier, 'review_status': status, 'version': version + 1}))
        # Redirect stays local and only preserves validated filters.
        return RedirectResponse('/encounters/' + identifier + '?' + urlencode({**filters.params(), 'page': page, 'saved': '1'}), status_code=303)

    @app.api_route('/encounters/{identifier}/audio', methods=['GET', 'HEAD'])
    def audio(request: Request, identifier: str):
        source = media.open(identifier)
        size = os.fstat(source.fileno()).st_size
        start, end, code = 0, size - 1, 200
        headers = {'Accept-Ranges': 'bytes', 'Content-Type': 'audio/wav'}
        # Single byte ranges suffice for browser seeking; reject unsupported multipart ranges.
        value = request.headers.get('range')
        if value:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', value)
            try:
                if not match or not any(match.groups()):
                    raise ValueError()
                first, last = match.groups()
                if first:
                    start, end = int(first), min(int(last), end) if last else end
                else:
                    suffix = int(last)
                    if suffix <= 0:
                        raise ValueError()
                    start = max(0, size - suffix)
                if start > end or start >= size:
                    raise ValueError()
            except ValueError:
                source.close()
                return Response(status_code=416, headers={'Content-Range': f'bytes */{size}'})
            code = 206
            headers['Content-Range'] = f'bytes {start}-{end}/{size}'
        headers['Content-Length'] = str(max(0, end - start + 1))
        if request.method == 'HEAD':
            source.close()
            return Response(status_code=code, headers=headers)
        def chunks():
            try:
                source.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = source.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            finally:
                source.close()
        return StreamingResponse(chunks(), status_code=code, headers=headers, background=BackgroundTask(source.close))

    @app.get('/encounters/{identifier}/spectrogram')
    def spectrogram(identifier: str):
        return Response(media.spectrogram(identifier), media_type='image/png')

    return app
