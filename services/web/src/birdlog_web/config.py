from dataclasses import dataclass
import os
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Config:
    database: Path = Path('/data/db/bird-log.sqlite3')
    clips: Path = Path('/data/clips')
    recorder_status: Path = Path('/status/recorder/status.json')
    analyzer_status: Path = Path('/status/analyzer/status.json')
    cache: Path = Path('/data/web-cache/spectrograms')
    timezone: str = 'America/Toronto'
    page_size: int = 50
    refresh: int = 30
    cache_mb: int = 256
    cache_seconds: int = 3600
    hosts: tuple = ('localhost', '127.0.0.1', '[::1]')
    host: str = '0.0.0.0'
    port: int = 8080
    log_level: str = 'INFO'

    def __post_init__(self):
        ZoneInfo(self.timezone)
        for name, low, high in [('page_size', 1, 100), ('refresh', 10, 3600), ('cache_mb', 1, 4096),
                                ('cache_seconds', 60, 86400), ('port', 1, 65535)]:
            if not low <= getattr(self, name) <= high:
                raise ValueError(f'{name} must be between {low} and {high}')
        if not self.hosts or any(not h or '*' in h or '/' in h for h in self.hosts):
            raise ValueError('WEB_ALLOWED_HOSTS must contain explicit hostnames/IPs without ports')
        if self.log_level not in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
            raise ValueError('Invalid LOG_LEVEL')

    @classmethod
    def from_env(cls):
        fields = {'database': 'DATABASE_PATH', 'clips': 'CLIPS_DIR', 'cache': 'SPECTROGRAM_CACHE_DIR',
                  'recorder_status': 'RECORDER_STATUS_PATH', 'analyzer_status': 'ANALYZER_STATUS_PATH'}
        args = {key: Path(os.environ[env]) for key, env in fields.items() if env in os.environ}
        for key, env in {'page_size': 'PAGE_SIZE', 'refresh': 'REFRESH_SECONDS', 'cache_mb': 'SPECTROGRAM_CACHE_MAX_MB',
                         'cache_seconds': 'SPECTROGRAM_CACHE_TTL_SECONDS', 'port': 'WEB_PORT'}.items():
            if env in os.environ:
                args[key] = int(os.environ[env])
        for key, env in {'timezone': 'DISPLAY_TIMEZONE', 'host': 'WEB_HOST', 'log_level': 'LOG_LEVEL'}.items():
            if env in os.environ:
                args[key] = os.environ[env]
        if 'WEB_ALLOWED_HOSTS' in os.environ:
            args['hosts'] = tuple(h.strip() for h in os.environ['WEB_ALLOWED_HOSTS'].split(','))
        return cls(**args)
