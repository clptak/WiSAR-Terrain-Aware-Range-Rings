"""/api/v1 routes. The contract is docs/openapi.json."""
import os
import time

from flask import Blueprint, current_app, g, jsonify, request, send_file, send_from_directory

from .config import API_VERSION
from .outputs import MEDIA, TARR_ONLY
from .problems import ApiProblem, unprocessable

bp = Blueprint('api_v1', __name__, url_prefix='/api/v1')
PUBLIC = {'api_v1.health', 'api_v1.openapi', 'api_v1.docs', 'api_v1.docs_asset'}
MAX_SPEED_KMH = 20.0
MPH_TO_KMH = 1.609344
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'swagger-ui')


def _ctx():
    return current_app.extensions['wisar_api']


@bp.before_request
def _authenticate():
    if request.method == 'OPTIONS' or request.endpoint in PUBLIC:
        return None
    g.user = _ctx().auth.verify(request.headers.get('Authorization'),
                                request.headers.get('Origin'), request.host)
    return None


# ---- system ----------------------------------------------------------------
@bp.get('/health')
def health():
    ctx = _ctx()
    snaps = _snapshot_status()
    status = 'ok' if all(s['available'] for s in snaps.values()) else 'degraded'
    return jsonify({'status': status, 'version': API_VERSION, 'auth': ctx.settings.auth_mode,
                    'queue': ctx.jobs.stats(), 'snapshots': snaps})


@bp.get('/openapi.json')
def openapi():
    return jsonify(_ctx().spec.public_doc(request.script_root + '/api/v1'))


@bp.get('/docs')
def docs():
    html = DOCS_HTML.replace('{{SPEC}}', request.script_root + '/api/v1/openapi.json')
    return current_app.response_class(html, mimetype='text/html')


@bp.get('/docs/<path:name>')
def docs_asset(name):
    return send_from_directory(STATIC_DIR, name, max_age=86400)


# ---- profiles --------------------------------------------------------------
@bp.get('/profiles')
def profiles():
    return jsonify(_ctx().profiles.listing())


# ---- reference content -----------------------------------------------------
@bp.get('/content/<content_id>')
def content(content_id):
    digest, item = _ctx().content.get(content_id)
    resp = jsonify(item)
    resp.set_etag(f'{digest[:16]}-{content_id}')
    resp.headers['Cache-Control'] = 'private, no-cache'
    return resp.make_conditional(request)


# ---- job creation ----------------------------------------------------------
@bp.post('/tarr/jobs')
def create_tarr_job():
    ctx = _ctx()
    body = _json_body()
    _validate('TarrJobRequest', body)
    resolved = ctx.profiles.resolve(body)
    return _accepted(ctx.jobs.submit('tarr', body, resolved, g.user['email'], g.user['instance']))


@bp.post('/travel-time/jobs')
def create_travel_time_job():
    ctx = _ctx()
    body = _json_body()
    _validate('TravelTimeJobRequest', body)
    speed = body['speed']
    kmh = speed['value'] * MPH_TO_KMH if speed['unit'] == 'mph' else speed['value']
    if kmh > MAX_SPEED_KMH:
        raise unprocessable([{'pointer': '/speed/value', 'detail': 'must be at most 20 km/h (12.4 mph)'}])
    intervals = sorted(_whole(h) for h in body['intervals_hours'])
    min_radius_km = body.get('min_radius_m', 10000) / 1000.0
    resolved = {'speed_kmh': round(kmh, 4), 'speed_mph': round(kmh / MPH_TO_KMH, 2),
                'intervals_hours': intervals, 'min_radius_km': min_radius_km,
                # Same rule as pipeline.run_isochrone_analysis.
                'radius_km': round(max(min_radius_km, kmh * max(intervals) + 2.0), 3)}
    return _accepted(ctx.jobs.submit('travel_time', body, resolved, g.user['email'], g.user['instance']))


# ---- jobs ------------------------------------------------------------------
@bp.get('/jobs')
def list_jobs():
    status, jtype = request.args.get('status'), request.args.get('type')
    errors = []
    if status and status not in ('queued', 'running', 'succeeded', 'failed'):
        errors.append({'pointer': '/status', 'detail': 'unknown status'})
    if jtype and jtype not in ('tarr', 'travel_time'):
        errors.append({'pointer': '/type', 'detail': 'unknown type'})
    if errors:
        raise ApiProblem(422, 'Unprocessable request', 'Invalid query parameters.', errors=errors)
    jobs = _ctx().jobs.list_for(g.user['email'], g.user['instance'], status or None, jtype or None)
    return jsonify({'jobs': [_view(j) for j in jobs]})


@bp.get('/jobs/<job_id>')
def get_job(job_id):
    job = _ctx().jobs.get(job_id)
    resp = jsonify(_view(job))
    if job['status'] in ('queued', 'running'):
        resp.headers['Retry-After'] = '10' if job['status'] == 'running' else '15'
    return resp


@bp.delete('/jobs/<job_id>')
def delete_job(job_id):
    _ctx().jobs.delete(job_id, g.user['email'], g.user['instance'])
    return '', 204


@bp.get('/jobs/<job_id>/outputs/<name>')
def get_output(job_id, name):
    ctx = _ctx()
    job = ctx.jobs.get(job_id)
    if name not in MEDIA or (name in TARR_ONLY and job['type'] != 'tarr'):
        raise ApiProblem(404, 'Not found', f'This job has no output named {name!r}.')
    if job['status'] != 'succeeded':
        raise ApiProblem(409, 'Job not finished' if job['status'] in ('queued', 'running') else 'Job failed',
                         f"Outputs are available only after the job succeeds (status: {job['status']}).")
    path = os.path.join(ctx.jobs.job_dir(job_id), name)
    if not os.path.exists(path):
        raise ApiProblem(404, 'Not found', f'Output {name!r} was not produced for this job.')
    download = f"{job['type']}_{job_id[:8]}_{name}"
    resp = send_file(path, mimetype=MEDIA[name], as_attachment=True, download_name=download,
                     conditional=True, etag=True, max_age=3600)
    resp.headers['Content-Type'] = MEDIA[name]
    return resp


# ---- helpers ---------------------------------------------------------------
def _json_body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApiProblem(400, 'Bad request', 'Send a JSON object with Content-Type: application/json.')
    return body


def _validate(schema, body):
    errors = _ctx().spec.errors(schema, body)
    if errors:
        raise unprocessable(errors)
    if schema == 'TarrJobRequest' and body['subject'].get('kind') == 'custom':
        d = body['subject']['distances']
        if not d['p25'] < d['p50'] < d['p75']:
            raise unprocessable([{'pointer': '/subject/distances',
                                  'detail': 'p25, p50 and p75 must be strictly increasing'}])


def _accepted(job):
    resp = jsonify(_view(_ctx().jobs.get(job['id'])))
    resp.status_code = 202
    resp.headers['Location'] = f"{request.script_root}/api/v1/jobs/{job['id']}"
    resp.headers['Retry-After'] = '15'
    return resp


def _view(job):
    job['links'] = {'self': f"{request.script_root}/api/v1/jobs/{job['id']}"}
    if job.get('outputs'):
        for meta in job['outputs'].values():
            meta['href'] = request.script_root + meta['href']
    return job


def _whole(h):
    return int(h) if float(h).is_integer() else float(h)


_SNAP_CACHE = {'at': 0, 'value': None}


def _snapshot_status():
    """Snapshot presence/age, cached for a minute. Missing = warning in results, not an error."""
    now = time.monotonic()
    if _SNAP_CACHE['value'] is not None and now - _SNAP_CACHE['at'] < 60:
        return _SNAP_CACHE['value']
    out = {}
    for name, attr in (('osm', 'CACHE_GPKG'), ('nlcd', 'CACHE_TIF'), ('nhd', 'CACHE_GPKG')):
        try:
            mod = __import__(f'pipeline.{name}_cache', fromlist=[attr])
            path = getattr(mod, attr)
            available = bool(mod.cache_is_available())
            age = None
            if name == 'osm':
                age = mod.cache_age_days()
            elif available:
                age = (time.time() - os.path.getmtime(path)) / 86400
            out[name] = {'available': available, 'age_days': round(age, 1) if age is not None else None}
        except Exception:
            out[name] = {'available': False, 'age_days': None}
    _SNAP_CACHE.update(at=now, value=out)
    return out


DOCS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WiSAR API v1</title>
<link rel="icon" href="docs/favicon-32x32.png"><link rel="stylesheet" href="docs/swagger-ui.css">
</head><body><div id="swagger-ui"></div>
<script src="docs/swagger-ui-bundle.js"></script>
<script>window.ui = SwaggerUIBundle({url: "{{SPEC}}", dom_id: "#swagger-ui", deepLinking: true,
  persistAuthorization: true, tryItOutEnabled: true, displayRequestDuration: true});</script>
</body></html>
"""
