from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import sqlite3
from zoneinfo import ZoneInfo

REVIEWS = ('unreviewed', 'correct', 'incorrect', 'uncertain')
# Never SELECT *: encounters may contain a large pending audio BLOB.
COLUMNS = 'id,species,scientific_name,common_name,start,end,score,model_version,state,clip_state,clip_path,clip_start,clip_end,review_status,review_updated_at,review_version'


class Unavailable(Exception):
    pass


class Conflict(Exception):
    pass


class Store:
    def __init__(self, config):
        self.config = config

    @contextmanager
    def connect(self, write=False):
        if not self.config.database.is_file():
            raise Unavailable('Waiting for the analyzer to initialize metadata.')
        db = None
        try:
            db = sqlite3.connect(self.config.database.resolve().as_uri() + '?mode=rw', uri=True, timeout=0.5)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA busy_timeout=500')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version == 0:
                raise Unavailable('Waiting for the analyzer to initialize metadata.')
            if version != 1:
                raise Unavailable(f'Unsupported metadata schema version {version}; update the web service.')
            if not write:
                db.execute('PRAGMA query_only=ON')
            # Bound expensive queries; the shared connection exists only for this request.
            import time as clock
            deadline = clock.monotonic() + 2
            db.set_progress_handler(lambda: int(clock.monotonic() > deadline), 10000)
            yield db
        except sqlite3.Error as error:
            import logging
            logging.warning('{"event":"database_failure","type":"%s"}', type(error).__name__)
            raise Unavailable('Metadata is busy or unavailable. Please retry shortly.') from error
        finally:
            if db is not None:
                db.close()

    def encounter(self, identifier):
        with self.connect() as db:
            row = db.execute(f'SELECT {COLUMNS} FROM encounters WHERE id=?', (identifier,)).fetchone()
            return dict(row) if row else None

    def detections(self, identifier):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT start,end,score FROM detections WHERE encounter_id=? ORDER BY start,id LIMIT 1001', (identifier,))]

    def review(self, identifier, status, version):
        if status not in REVIEWS or version < 0:
            raise ValueError('Invalid review')
        with self.connect(write=True) as db, db:
            import time as clock
            result = db.execute('UPDATE encounters SET review_status=?,review_updated_at=?,review_version=review_version+1 '
                                'WHERE id=? AND review_version=?', (status, clock.time(), identifier, version))
            if result.rowcount != 1:
                raise Conflict('This encounter changed since you opened it. Reload before reviewing again.')

    def results(self, filters, page=1):
        where, args = filters.sql()
        with self.connect() as db:
            # Consistent list/count/aggregates despite concurrent analyzer commits.
            db.execute('BEGIN')
            total, species_count = db.execute('SELECT count(*),count(DISTINCT species) FROM encounters' + where, args).fetchone()
            rows = [dict(r) for r in db.execute(f'SELECT {COLUMNS} FROM encounters' + where + ' ORDER BY start DESC,id DESC LIMIT ? OFFSET ?',
                                               (*args, self.config.page_size, (page - 1) * self.config.page_size))]
            species = [dict(r) for r in db.execute('SELECT species,common_name,scientific_name,count(*) AS count,min(start) AS first,max(start) AS last '
                                                  'FROM encounters' + where + ' GROUP BY species ORDER BY count DESC,species LIMIT 1000', args)]
            # Local hour starts preserve both the repeated hour and fractional UTC offsets.
            db.create_function('local_hour', 1, lambda epoch: datetime.fromtimestamp(epoch, filters.tz).replace(minute=0, second=0, microsecond=0).timestamp())
            hourly = list(db.execute('SELECT local_hour(start) AS bucket,count(*) AS count FROM encounters' + where + ' GROUP BY bucket ORDER BY bucket', args))
        return rows, total, species, hourly, species_count


class Filters:
    def __init__(self, params, tz, today=False):
        self.tz = ZoneInfo(tz)
        now = datetime.now(self.tz).date()
        self.since = date.fromisoformat(params.get('since') or (now if today else now - timedelta(days=6)).isoformat())
        self.until = date.fromisoformat(params.get('until') or now.isoformat())
        if not date(1970, 1, 1) <= self.since <= self.until < date(2100, 1, 1) or (self.until - self.since).days > 365:
            raise ValueError('Choose an ordered date range of at most 366 days between 1970 and 2099.')
        self.species = params.get('species', '')
        if len(self.species) > 250:
            raise ValueError('Species filter is too long.')
        self.review = params.get('review', '')
        if self.review and self.review not in REVIEWS:
            raise ValueError('Invalid review filter.')
        self.score = float(params.get('score') or 0)
        if not 0 <= self.score <= 1:
            raise ValueError('Minimum score must be between 0 and 1.')

    def sql(self):
        start = datetime.combine(self.since, time(), self.tz).timestamp()
        end = datetime.combine(self.until + timedelta(days=1), time(), self.tz).timestamp()
        sql, args = ' WHERE start>=? AND start<? AND score>=?', [start, end, self.score]
        if self.species:
            sql += ' AND species=?'
            args.append(self.species)
        if self.review:
            sql += ' AND review_status=?'
            args.append(self.review)
        return sql, args

    def params(self):
        return dict(since=self.since.isoformat(), until=self.until.isoformat(), species=self.species, review=self.review, score=str(self.score))

    def chart(self, hourly, daily=False):
        counts = {}
        for epoch, count in hourly:
            local = datetime.fromtimestamp(epoch, self.tz)
            label = local.strftime('%Y-%m-%d' if daily else '%m-%d %H:00 %z')
            counts[label] = counts.get(label, 0) + count
        # Fill empty hours/days, traversing UTC to preserve 23/25-hour DST days.
        start = datetime.combine(self.since, time(), self.tz).astimezone(timezone.utc)
        end = datetime.combine(self.until + timedelta(days=1), time(), self.tz).astimezone(timezone.utc)
        result = {}
        while start < end:
            label = start.astimezone(self.tz).strftime('%Y-%m-%d' if daily else '%m-%d %H:00 %z')
            result[label] = counts.get(label, 0)
            start += timedelta(hours=1)
        return list(result.items())
