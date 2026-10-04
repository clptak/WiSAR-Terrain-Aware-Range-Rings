"""Runtime settings for the /api/v1 layer, read from the environment once."""
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
API_VERSION = '1.1.0-draft'


def _find_spec():
    # In the image the Dockerfile copies docs/openapi.json next to this file;
    # in a checkout it lives in the repo's docs/ folder.
    for path in (os.environ.get('WISAR_OPENAPI_PATH'),
                 os.path.join(HERE, 'openapi.json'),
                 os.path.join(HERE, '..', '..', 'docs', 'openapi.json')):
        if path and os.path.exists(path):
            return os.path.abspath(path)
    raise RuntimeError('openapi.json not found (set WISAR_OPENAPI_PATH)')


def normalize_origin(value):
    """'https://Map.Example.org/' -> 'https://map.example.org'. Raises ValueError
    for anything that is not a bare http(s) origin."""
    parts = urlsplit((value or '').strip())
    if parts.scheme not in ('http', 'https') or not parts.netloc or parts.path not in ('', '/') \
            or parts.query or parts.fragment:
        raise ValueError(f'not an http(s) origin: {value!r}')
    return f'{parts.scheme}://{parts.netloc.lower()}'


def parse_instances(raw):
    """WISAR_CLOUDTAK_INSTANCES -> [(origin, api_url)].

    Comma-separated entries, each `<web origin>=<API base URL>` or just
    `<web origin>` when CloudTAK's API is served from the same origin, e.g.
    `https://map.a.org=http://cloudtak-api:5000,https://map.b.org`.
    The first entry is the default for requests without a browser Origin.
    """
    out, seen = [], set()
    for entry in (raw or '').split(','):
        entry = entry.strip()
        if not entry:
            continue
        origin, _, api = entry.partition('=')
        try:
            origin = normalize_origin(origin)
        except ValueError as e:
            raise RuntimeError(f'WISAR_CLOUDTAK_INSTANCES: {e}')
        api = (api.strip() or origin).rstrip('/')
        if urlsplit(api).scheme not in ('http', 'https'):
            raise RuntimeError(f'WISAR_CLOUDTAK_INSTANCES: API URL for {origin} must be http(s): {api!r}')
        if origin in seen:
            raise RuntimeError(f'WISAR_CLOUDTAK_INSTANCES: {origin} is listed twice')
        seen.add(origin)
        out.append((origin, api))
    return out


@dataclass
class Settings:
    cloudtak_api_url: str = 'http://api:5000'
    # [(web origin, CloudTAK API base URL)]. Empty = the single CloudTAK at
    # cloudtak_api_url verifies every token (the original behaviour).
    cloudtak_instances: list = field(default_factory=list)
    auth_mode: str = 'cloudtak'           # 'cloudtak' or 'none' (local testing only)
    auth_cache_seconds: int = 300
    cors_origins: list = field(default_factory=list)
    job_ttl_hours: float = 72.0
    jobs_dir: str = '/var/wisar/jobs'
    max_queued: int = 10
    profiles_dir: str = os.path.join(HERE, 'data', 'profiles')
    default_dataset: str = 'koester'
    openapi_path: str = ''
    content_html: str = os.path.join(HERE, '..', 'static', 'index.html')
    sweep_interval_seconds: int = 300
    start_worker: bool = True

    @classmethod
    def from_env(cls):
        env = os.environ.get
        return cls(
            cloudtak_api_url=env('CLOUDTAK_API_URL', 'http://api:5000').rstrip('/'),
            cloudtak_instances=parse_instances(env('WISAR_CLOUDTAK_INSTANCES', '')),
            auth_mode=env('WISAR_AUTH', 'cloudtak').strip().lower(),
            auth_cache_seconds=int(env('WISAR_AUTH_CACHE_SECONDS', '300')),
            cors_origins=[o.strip().rstrip('/') for o in env('WISAR_CORS_ORIGINS', '').split(',') if o.strip()],
            job_ttl_hours=float(env('WISAR_JOB_TTL_HOURS', '72')),
            jobs_dir=env('WISAR_JOBS_DIR', '/var/wisar/jobs'),
            max_queued=int(env('WISAR_MAX_QUEUED', '10')),
            profiles_dir=env('WISAR_PROFILES_DIR', os.path.join(HERE, 'data', 'profiles')),
            default_dataset=env('WISAR_DEFAULT_DATASET', 'koester'),
            openapi_path=_find_spec(),
            content_html=env('WISAR_CONTENT_HTML', os.path.join(HERE, '..', 'static', 'index.html')),
        )

    def validate(self):
        if self.auth_mode not in ('cloudtak', 'none'):
            raise RuntimeError(f"WISAR_AUTH must be 'cloudtak' or 'none', not {self.auth_mode!r}")
        if not self.openapi_path:
            self.openapi_path = _find_spec()
        # Every registered CloudTAK's web origin may call the API from a browser.
        self.cors_origins = list(dict.fromkeys(
            [o.rstrip('/') for o in self.cors_origins] + [o for o, _ in self.cloudtak_instances]))

    @property
    def default_instance(self):
        """Instance key for requests without a browser Origin (and for jobs
        created before instances existed): the first registered origin, or ''
        in single-CloudTAK mode."""
        return self.cloudtak_instances[0][0] if self.cloudtak_instances else ''
