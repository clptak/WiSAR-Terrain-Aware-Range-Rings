"""Runtime settings for the /api/v1 layer, read from the environment once."""
import os
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
API_VERSION = '1.0.0-draft'


def _find_spec():
    # In the image the Dockerfile copies docs/openapi.json next to this file;
    # in a checkout it lives in the repo's docs/ folder.
    for path in (os.environ.get('WISAR_OPENAPI_PATH'),
                 os.path.join(HERE, 'openapi.json'),
                 os.path.join(HERE, '..', '..', 'docs', 'openapi.json')):
        if path and os.path.exists(path):
            return os.path.abspath(path)
    raise RuntimeError('openapi.json not found (set WISAR_OPENAPI_PATH)')


@dataclass
class Settings:
    cloudtak_api_url: str = 'http://api:5000'
    auth_mode: str = 'cloudtak'           # 'cloudtak' or 'none' (local testing only)
    auth_cache_seconds: int = 300
    cors_origins: list = field(default_factory=list)
    job_ttl_hours: float = 72.0
    jobs_dir: str = '/var/wisar/jobs'
    max_queued: int = 10
    profiles_dir: str = os.path.join(HERE, 'data', 'profiles')
    default_dataset: str = 'koester'
    openapi_path: str = ''
    sweep_interval_seconds: int = 300
    start_worker: bool = True

    @classmethod
    def from_env(cls):
        env = os.environ.get
        return cls(
            cloudtak_api_url=env('CLOUDTAK_API_URL', 'http://api:5000').rstrip('/'),
            auth_mode=env('WISAR_AUTH', 'cloudtak').strip().lower(),
            auth_cache_seconds=int(env('WISAR_AUTH_CACHE_SECONDS', '300')),
            cors_origins=[o.strip().rstrip('/') for o in env('WISAR_CORS_ORIGINS', '').split(',') if o.strip()],
            job_ttl_hours=float(env('WISAR_JOB_TTL_HOURS', '72')),
            jobs_dir=env('WISAR_JOBS_DIR', '/var/wisar/jobs'),
            max_queued=int(env('WISAR_MAX_QUEUED', '10')),
            profiles_dir=env('WISAR_PROFILES_DIR', os.path.join(HERE, 'data', 'profiles')),
            default_dataset=env('WISAR_DEFAULT_DATASET', 'koester'),
            openapi_path=_find_spec(),
        )

    def validate(self):
        if self.auth_mode not in ('cloudtak', 'none'):
            raise RuntimeError(f"WISAR_AUTH must be 'cloudtak' or 'none', not {self.auth_mode!r}")
        if not self.openapi_path:
            self.openapi_path = _find_spec()
