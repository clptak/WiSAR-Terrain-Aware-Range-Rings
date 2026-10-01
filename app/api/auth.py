"""Caller authentication: the CloudTAK user token, verified by CloudTAK itself.

WiSAR forwards the caller's bearer token to CloudTAK GET /api/login, which
answers {email, access} for a valid token. Nothing is stored except a short
in-memory cache keyed by a hash of the token. WISAR_AUTH=none skips the check
(owner 'anonymous') and is meant only for local testing.

Several CloudTAK deployments can share one WiSAR (WISAR_CLOUDTAK_INSTANCES).
The browser's Origin header picks the deployment whose /api/login verifies
the token; an unregistered Origin is refused. Requests without an Origin, or
from WiSAR's own pages (Swagger UI), use the first registered deployment.
A forged Origin gains nothing: the token must still be valid on the
deployment it names. Without WISAR_CLOUDTAK_INSTANCES, CLOUDTAK_API_URL
verifies every token, as before.
"""
import hashlib
import threading
import time
from urllib.parse import urlsplit

import requests

from .config import normalize_origin
from .problems import ApiProblem

ANONYMOUS = {'email': 'anonymous', 'access': 'none', 'instance': ''}


class Authenticator:
    def __init__(self, settings, session=None):
        self.settings = settings
        self.session = session or requests.Session()
        self._cache = {}
        self._lock = threading.Lock()

    def instance_for(self, origin=None, host=None):
        """(instance key, CloudTAK API base URL) for a request.

        origin: the request's Origin header; host: its Host header (to
        recognise WiSAR's own pages)."""
        instances = self.settings.cloudtak_instances
        if not instances:
            return '', self.settings.cloudtak_api_url
        if origin and urlsplit(origin).netloc.lower() != (host or '').lower():
            try:
                key = normalize_origin(origin)
            except ValueError:
                key = None
            for inst_origin, api in instances:
                if inst_origin == key:
                    return inst_origin, api
            raise ApiProblem(403, 'CloudTAK not registered',
                             f'{origin} is not a CloudTAK deployment this WiSAR serves '
                             '(WISAR_CLOUDTAK_INSTANCES).')
        return instances[0]

    def verify(self, authorization_header, origin=None, host=None):
        if self.settings.auth_mode == 'none':
            return dict(ANONYMOUS)
        token = _bearer(authorization_header)
        if not token:
            raise ApiProblem(401, 'Unauthorized', 'Send the CloudTAK token as "Authorization: Bearer <token>".',
                             headers={'WWW-Authenticate': 'Bearer'})
        instance, api_url = self.instance_for(origin, host)
        key = hashlib.sha256(f'{instance}\n{token}'.encode('utf-8')).hexdigest()
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return dict(hit[1])
        user = self._ask_cloudtak(token, api_url)
        user['instance'] = instance
        with self._lock:
            if len(self._cache) > 1000:
                self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            self._cache[key] = (now + self.settings.auth_cache_seconds, user)
        return dict(user)

    def _ask_cloudtak(self, token, api_url):
        url = f'{api_url}/api/login'
        try:
            resp = self.session.get(url, headers={'Authorization': f'Bearer {token}',
                                                  'Accept': 'application/json'}, timeout=15)
        except requests.RequestException as e:
            raise ApiProblem(503, 'Authentication unavailable',
                             f'CloudTAK could not be reached to verify the token ({type(e).__name__}).',
                             headers={'Retry-After': 30})
        if resp.status_code in (401, 403):
            raise ApiProblem(401, 'Unauthorized', 'CloudTAK rejected the token.',
                             headers={'WWW-Authenticate': 'Bearer error="invalid_token"'})
        if resp.status_code != 200:
            raise ApiProblem(503, 'Authentication unavailable',
                             f'CloudTAK answered HTTP {resp.status_code} when verifying the token.',
                             headers={'Retry-After': 30})
        try:
            body = resp.json()
            email = body['email']
        except (ValueError, KeyError, TypeError):
            raise ApiProblem(503, 'Authentication unavailable', 'CloudTAK returned an unexpected login response.')
        return {'email': email, 'access': body.get('access')}


def _bearer(header):
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != 'bearer' or not parts[1].strip():
        return None
    return parts[1].strip()
