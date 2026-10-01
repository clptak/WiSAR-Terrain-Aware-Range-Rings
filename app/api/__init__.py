"""Headless /api/v1 for CloudTAK plugins. See docs/openapi.json.

init_api(app) mounts the blueprint on server.py's Flask app, wraps the
legacy routes (see legacy.py), adds CORS for WISAR_CORS_ORIGINS and starts
the job worker. pipeline/ and server.py are not modified.
"""
from types import SimpleNamespace

from flask import request
from werkzeug.exceptions import HTTPException

from .auth import Authenticator
from .config import Settings
from .content import ContentStore
from .jobs import JobManager
from .legacy import wrap_legacy
from .problems import ApiProblem, problem_response
from .profiles import ProfileStore
from .routes import bp
from .runner import run_job
from .schema import Spec

CORS_HEADERS = {
    'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
    'Access-Control-Allow-Headers': 'Authorization, Content-Type',
    'Access-Control-Expose-Headers': 'Location, Retry-After, Content-Disposition, ETag',
    'Access-Control-Max-Age': '600',
}


def init_api(app, settings=None, executor=None, auth=None):
    settings = settings or Settings.from_env()
    settings.validate()
    if settings.auth_mode == 'none':
        print('WARNING: WISAR_AUTH=none - /api/v1 accepts unauthenticated requests. Local testing only.')
    ctx = SimpleNamespace(
        settings=settings,
        spec=Spec(settings.openapi_path),
        profiles=ProfileStore(settings.profiles_dir, settings.default_dataset),
        content=ContentStore(settings.content_html),
        auth=auth or Authenticator(settings),
        jobs=JobManager(settings, executor or run_job),
    )
    app.extensions['wisar_api'] = ctx
    app.register_blueprint(bp)
    wrap_legacy(app, ctx)

    @app.errorhandler(ApiProblem)
    def _problem(p):
        return problem_response(p, instance=request.path)

    @app.errorhandler(HTTPException)
    def _http(e):
        if request.path.startswith('/api/v1'):
            return problem_response(ApiProblem(e.code, e.name, e.description), instance=request.path)
        return e

    @app.after_request
    def _cors(resp):
        origin = request.headers.get('Origin', '').rstrip('/')
        if origin and request.path.startswith('/api/') and origin in settings.cors_origins:
            resp.headers['Access-Control-Allow-Origin'] = origin
            resp.headers.add('Vary', 'Origin')
            for k, v in CORS_HEADERS.items():
                resp.headers[k] = v
        return resp

    if settings.start_worker:
        ctx.jobs.start()
    return ctx
