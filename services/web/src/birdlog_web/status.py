import json
import math
import time


def read_status(path, service, now=None):
    now = time.time() if now is None else now
    result = {'service': service, 'health': 'unknown', 'state': 'unavailable', 'age': None, 'metrics': []}
    try:
        with path.open() as source:
            text = source.read(65537)
        if len(text) > 65536:
            return result
        data = json.loads(text)
        if not isinstance(data, dict) or data.get('schema_version') != 1:
            return result
        def number(key):
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(key)
            return value
        age = now - number('updated_at')
        state = data['state']
        if not isinstance(state, str):
            return result
        result.update(state=state, age=round(age, 1))
        if service == 'recorder':
            limit = 10
            fields = [('last_published_path', 'Last published recording'), ('last_published_at', 'Last published (UTC epoch seconds)'), ('free_bytes', 'Recording volume free bytes'),
                      ('recordings_bytes', 'Raw audio bytes')]
        else:
            limit = (120 if state in ('starting', 'initializing', 'loading') else
                     number('stall_seconds') if state == 'processing' else
                     max(15, 3 * number('scan_seconds')) if 'scan_seconds' in data else 15)
            fields = [('last_processed_recording', 'Last processed recording'), ('last_progress_at', 'Last progress (UTC epoch seconds)'),
                      ('pending_recordings', 'Pending recordings'), ('oldest_pending_at', 'Oldest pending (UTC epoch seconds)'),
                      ('analysis_seconds_per_audio_second', 'Analysis seconds per audio second'),
                      ('clip_free_bytes', 'Clip volume free bytes'), ('database_free_bytes', 'Database volume free bytes'),
                      ('failed_recordings', 'Failed recordings'), ('missing_recordings', 'Missing recordings')]
        for key, _ in fields:
            if data.get(key) is not None:
                if key in ('last_published_path', 'last_processed_recording'):
                    if not isinstance(data[key], str):
                        raise ValueError(key)
                else:
                    number(key)
        result['metrics'] = [(label.replace(' (UTC epoch seconds)', '').replace(' bytes', ' (GiB)'), data.get(key),
                              'time' if key.endswith('_at') else 'bytes' if key.endswith('_bytes') else 'text')
                             for key, label in fields]
        if age < -1 or age > limit:
            result['health'] = 'stale'
            return result
        if state in ('starting', 'initializing', 'loading'):
            result['health'] = 'initializing'
            return result
        if state in ('unhealthy', 'stopped', 'retrying', 'error'):
            result['health'] = 'unhealthy'
            return result
        if service == 'recorder':
            healthy = (state == 'recording' and -1 <= now - number('last_callback_at') <= number('callback_timeout_seconds')
                       and -1 <= now - (number('last_published_at') if data.get('last_published_at') else number('session_started_at'))
                       <= number('chunk_seconds') + number('queue_seconds') + 10
                       and number('free_bytes') >= number('min_free_bytes')
                       and number('recordings_bytes') < number('max_recordings_bytes'))
        else:
            healthy = (state in ('idle', 'processing') and data.get('model_ready') is True
                       and -1 <= now - number('last_scan_at') <= number('stall_seconds')
                       and number('clip_free_bytes') >= number('min_free_bytes')
                       and number('database_free_bytes') >= number('min_free_bytes'))
        result['health'] = 'healthy' if healthy else 'unhealthy'
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return result
