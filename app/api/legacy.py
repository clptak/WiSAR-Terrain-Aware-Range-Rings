"""Wraps the legacy routes in server.py without editing server.py.

- /api/analyze and /api/analyze-isochrone keep their synchronous JSON
  contract, but run on the shared job worker (one analysis at a time).
- Legacy readers of WORK_DIR files take the pipeline lock, so they never read
  a raster while an analysis is rewriting it.
- Every legacy /api route requires the same CloudTAK token as /api/v1, since
  the container is exposed on a public hostname.
- The Leaflet UI is not served: / redirects to the API docs, /static is 404.
"""
import functools

from flask import abort, copy_current_request_context, jsonify, redirect, request

from .problems import ApiProblem

QUEUED = ('run_analysis_endpoint', 'run_isochrone_endpoint')
LOCKED_READERS = ('serve_result', 'serve_cost_png', 'serve_terrain_png', 'serve_percentile_png')
AUTH_ONLY = ('export_tarrs_to_caltopo',)


def wrap_legacy(app, ctx):
    views = app.view_functions

    def authed(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            try:
                ctx.auth.verify(request.headers.get('Authorization'))
            except ApiProblem as p:
                resp = jsonify({'status': 'error', 'message': p.detail or p.title})
                resp.status_code = p.status
                for k, v in p.headers.items():
                    resp.headers[k] = str(v)
                return resp
            return fn(*args, **kwargs)
        return inner

    def queued(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            request.get_data(cache=True)  # read the body before handing the request to the worker
            run = copy_current_request_context(lambda: fn(*args, **kwargs))
            return ctx.jobs.run_inline(run)
        return inner

    def locked(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            with ctx.jobs.pipeline_lock:
                return fn(*args, **kwargs)
        return inner

    for name in QUEUED:
        views[name] = authed(queued(views[name]))
    for name in LOCKED_READERS:
        views[name] = authed(locked(views[name]))
    for name in AUTH_ONLY:
        if name in views:
            views[name] = authed(views[name])
    views['index'] = lambda: redirect('/api/v1/docs', code=302)
    if 'static' in views:
        views['static'] = lambda filename: abort(404)
