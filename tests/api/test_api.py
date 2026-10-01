import json
import os
import threading
import time
import xml.etree.ElementTree as ET

import pytest
import rasterio

from conftest import ALICE, BOB, ROOT, wait_for

IPP = {'lat': 34.9523, 'lon': -111.7610}
HIKER = {'ipp': IPP, 'subject': {'kind': 'listed', 'category': 'Hiker', 'eco_region': 'Dry', 'terrain': 'Mountainous'}}
TT = {'ipp': IPP, 'speed': {'value': 2, 'unit': 'mph'}, 'intervals_hours': [8, 2, 4.5]}


# ---- contract --------------------------------------------------------------
def test_spec_is_valid_openapi_31():
    from openapi_spec_validator import validate
    validate(json.load(open(os.path.join(ROOT, 'docs', 'openapi.json'))))


def test_public_endpoints_need_no_token(client):
    assert client.get('/api/v1/health').status_code == 200
    spec = client.get('/api/v1/openapi.json').json
    assert spec['servers'][0]['url'] == '/api/v1'
    assert b'swagger-ui-bundle.js' in client.get('/api/v1/docs').data
    assert client.get('/api/v1/docs/swagger-ui-bundle.js').status_code == 200


def test_missing_token_is_problem_401(client):
    r = client.get('/api/v1/profiles')
    assert r.status_code == 401
    assert r.mimetype == 'application/problem+json'
    assert r.json['status'] == 401


def test_unknown_v1_route_is_problem_404(client):
    r = client.get('/api/v1/nope', headers=ALICE)
    assert r.status_code == 404 and r.mimetype == 'application/problem+json'


# ---- profiles --------------------------------------------------------------
def test_profiles_listing(client):
    body = client.get('/api/v1/profiles', headers=ALICE).json
    ds = body['datasets'][0]
    assert body['default_dataset'] == 'koester' and ds['id'] == 'koester'
    assert len(ds['categories']) == 28
    hiker = next(c for c in ds['categories'] if c['name'] == 'Hiker')
    assert hiker['calibration'] == {'m25': 1.0, 'm50': 1.1, 'm75': 1.4}
    assert ds['default_calibration'] == {'m25': 1.05, 'm50': 1.35, 'm75': 1.8}


# ---- reference content -----------------------------------------------------
CONTENT_IDS = ['metadata', 'changelog', 'validation', 'tarr-explainer', 'travel-time-explainer', 'scope-note']


@pytest.mark.parametrize('cid', CONTENT_IDS)
def test_content_sections_from_web_tool_page(client, cid):
    r = client.get(f'/api/v1/content/{cid}', headers=ALICE)
    assert r.status_code == 200, r.json
    body = r.json
    assert client.ctx.spec.errors('Content', body) == []
    assert body['id'] == cid and body['html'].strip()
    html = body['html'].lower()
    for banned in ('<script', '<button', '<input', '<form', 'onclick', 'javascript:', '<!--'):
        assert banned not in html, banned
    # every CSS variable the fragment uses has a value
    import re
    used = set(re.findall(r'var\((--[a-z0-9-]+)', body['html']))
    assert used == set(body['css_variables'])
    assert r.headers['ETag'] and r.headers['Cache-Control'] == 'private, no-cache'


def test_content_keeps_the_web_tool_text(client):
    import html as H
    import re
    src = open(os.path.join(ROOT, 'app', 'static', 'index.html'), encoding='utf-8').read()

    def words(h):
        return ' '.join(H.unescape(re.sub(r'<[^>]+>', ' ', re.sub(r'<!--.*?-->', '', h, flags=re.S))).split())
    page = words(src)
    for cid in CONTENT_IDS:
        body = client.get(f'/api/v1/content/{cid}', headers=ALICE).json
        assert words(body['html']) in page, cid
    tt = client.get('/api/v1/content/travel-time-explainer', headers=ALICE).json
    assert tt['title'] == 'Understanding Travel Time analysis'
    assert tt['html'].count('<svg') == 3 and 'viewBox=' in tt['html']  # SVG markup kept as written
    assert 'Got it' not in words(tt['html'])
    note = client.get('/api/v1/content/scope-note', headers=ALICE).json
    assert 'one input among many' in note['html'] and note['html'].startswith('<p')


def test_content_etag_and_304(client):
    r = client.get('/api/v1/content/metadata', headers=ALICE)
    r2 = client.get('/api/v1/content/metadata', headers={**ALICE, 'If-None-Match': r.headers['ETag']})
    assert r2.status_code == 304 and not r2.data


def test_content_needs_token_and_known_id(client):
    assert client.get('/api/v1/content/metadata').status_code == 401
    r = client.get('/api/v1/content/nope', headers=ALICE)
    assert r.status_code == 404 and r.mimetype == 'application/problem+json'


def test_content_follows_page_changes_and_reports_missing_sections(make_app, tmp_path):
    page = tmp_path / 'index.html'
    page.write_text('<html><head><style>:root{--text-muted:#123456}</style></head><body>'
                    '<div class="modal-overlay hidden" id="metadataModal" onclick="x()">'
                    '<div class="modal"><h2>Old</h2><p style="color:var(--text-muted)">one</p>'
                    '<div style="text-align:center;"><button onclick="y()">Close</button></div></div></div>'
                    '</body></html>', encoding='utf-8')
    app, _ = make_app(content_html=str(page))
    c = app.test_client()
    body = c.get('/api/v1/content/metadata', headers=ALICE).json
    assert body['title'] == 'Old' and 'Close' not in body['html'] and '<div' not in body['html']
    assert body['css_variables'] == {'--text-muted': '#123456'}
    page.write_text(page.read_text().replace('Old', 'New'), encoding='utf-8')
    os.utime(page, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    assert c.get('/api/v1/content/metadata', headers=ALICE).json['title'] == 'New'
    r = c.get('/api/v1/content/changelog', headers=ALICE)   # section not in this page
    assert r.status_code == 503 and r.mimetype == 'application/problem+json'
    page.unlink()
    assert c.get('/api/v1/content/metadata', headers=ALICE).status_code == 503


# ---- TARR ------------------------------------------------------------------
def test_tarr_listed_end_to_end(client, fake_pipeline):
    r = client.post('/api/v1/tarr/jobs', json=HIKER, headers=ALICE)
    assert r.status_code == 202, r.json
    assert r.headers['Location'].endswith('/api/v1/jobs/' + r.json['id'])
    res = r.json['resolved']
    assert res['calibration_applied'] == 'category'
    assert res['source_distances_km']['p25'] == 1.61
    assert res['final_distances_km'] == {'p25': 1.61, 'p50': 3.542, 'p75': 9.016, 'unit': 'km'}
    assert res['radius_km'] == 11.016

    job = wait_for(client, r.json['id'])
    assert job['status'] == 'succeeded', job['error']
    assert fake_pipeline.calls[0][1]['p'] == (1.61, 3.542, 9.016)
    assert fake_pipeline.threads == ['wisar-job-worker']
    assert set(job['outputs']) == {'contours.geojson', 'contours.kml', 'cost-distance.tif',
                                   'cost-surface.tif', 'attractor-score.tif', 'probability.tif'}
    assert job['result']['crs'] == 'EPSG:4326' and job['result']['contour_count'] == 3
    assert job['result']['warnings'][0]['message'] == 'test note'
    assert job['expires_at'] is not None

    gj = client.get(job['outputs']['contours.geojson']['href'], headers=ALICE)
    assert gj.status_code == 200 and gj.mimetype == 'application/geo+json'
    props = gj.json['features'][0]['properties']
    assert props['callsign'] == '25% Percentile TARR'
    assert props['remarks'] == 'Threshold: 1.61 km cost-distance'
    # CloudTAK styling: per-ring colour, outline only
    colors = [f['properties']['stroke'] for f in gj.json['features']]
    assert colors == ['#ffffff', '#ffca00', '#ff6a1a']
    assert all(f['properties']['fill'] == f['properties']['color'] and f['properties']['fill-opacity'] == 0.1
               and f['properties']['stroke-width'] == 3 for f in gj.json['features'])

    kml = client.get(job['outputs']['contours.kml']['href'], headers=ALICE)
    root = ET.fromstring(kml.data)
    ns = {'k': 'http://www.opengis.net/kml/2.2'}
    assert len(root.findall('.//k:Placemark', ns)) == 3
    assert root.find('.//k:innerBoundaryIs', ns) is not None


def _download(client, href, tmp_path, name):
    r = client.get(href, headers=ALICE)
    assert r.status_code == 200
    assert r.headers['Content-Type'] == 'image/tiff; application=geotiff; profile=cloud-optimized'
    path = tmp_path / name
    path.write_bytes(r.data)
    with rasterio.open(path) as src:
        return src.profile, src.read(1), src.tags(ns='IMAGE_STRUCTURE')


def test_geotiffs_are_lossless_cogs(client, tmp_path, fake_pipeline):
    job = wait_for(client, client.post('/api/v1/tarr/jobs', json=HIKER, headers=ALICE).json['id'])
    prof, data, ims = _download(client, job['outputs']['cost-distance.tif']['href'], tmp_path, 'cd.tif')
    assert prof['compress'].lower() == 'deflate' and prof['tiled']
    assert ims.get('LAYOUT') == 'COG'
    with rasterio.open(os.path.join(fake_pipeline.workdir, 'cost_distance.tif')) as src:
        assert (src.read(1) == data).all()      # bit-identical to the pipeline raster
    # attractor score: intersection 1.0, trail 0.55, stream 0.28, nodata corner -9999
    _, score, _ = _download(client, job['outputs']['attractor-score.tif']['href'], tmp_path, 'as.tif')
    assert score[15, 30] == pytest.approx(1.0)
    assert score[40, 30] == pytest.approx(0.55)
    assert score[15, 5] == pytest.approx(0.28)
    assert score[0, 0] == -9999


def test_tarr_variant_fallback_matches_web_tool(client):
    body = dict(HIKER, subject={'kind': 'listed', 'category': 'Hiker', 'eco_region': 'Urban'})
    res = client.post('/api/v1/tarr/jobs', json=body, headers=ALICE).json['resolved']
    assert res['variant'] == {'eco_region': None, 'terrain': None}
    assert res['source_distances_km']['p75'] == 3.22


def test_uncalibrated_category_uses_dataset_default(client):
    body = dict(HIKER, subject={'kind': 'listed', 'category': 'Runner'})
    res = client.post('/api/v1/tarr/jobs', json=body, headers=ALICE).json['resolved']
    assert res['calibration_applied'] == 'dataset_default'
    assert res['multipliers'] == {'m25': 1.05, 'm50': 1.35, 'm75': 1.8}


def test_custom_profile_miles_uncalibrated_by_default(client):
    body = {'ipp': IPP, 'subject': {'kind': 'custom', 'name': 'AZ Hiker',
                                    'distances': {'p25': 1, 'p50': 2, 'p75': 4, 'unit': 'mi'}}}
    res = client.post('/api/v1/tarr/jobs', json=body, headers=ALICE).json['resolved']
    assert res['calibration_applied'] == 'none'
    assert res['final_distances_km'] == {'p25': 1.6093, 'p50': 3.2187, 'p75': 6.4374, 'unit': 'km'}
    assert res['subject_label'] == 'AZ Hiker'


def test_custom_profile_global_calibration(client):
    body = {'ipp': IPP, 'calibration': 'global',
            'subject': {'kind': 'custom', 'name': 'X', 'distances': {'p25': 1, 'p50': 2, 'p75': 4}}}
    res = client.post('/api/v1/tarr/jobs', json=body, headers=ALICE).json['resolved']
    assert res['calibration_applied'] == 'global'
    assert res['final_distances_km']['p75'] == 7.2


@pytest.mark.parametrize('body,pointer', [
    ({'ipp': {'lat': 34.9, 'lng': -111.7}, 'subject': HIKER['subject']}, '/ipp/lon'),
    ({'ipp': {'lat': 95, 'lon': -111.7}, 'subject': HIKER['subject']}, '/ipp/lat'),
    ({'ipp': IPP, 'subject': {'kind': 'custom', 'name': 'x', 'distances': {'p25': 3, 'p50': 2, 'p75': 4}}}, '/subject/distances'),
    ({'ipp': IPP, 'subject': {'kind': 'custom', 'name': 'x', 'distances': {'p25': 1, 'p50': 2}}}, '/subject/distances/p75'),
    ({'ipp': IPP, 'subject': {'kind': 'listed', 'category': 'Unicorn'}}, '/subject/category'),
    ({'ipp': IPP, 'subject': {'kind': 'other'}}, '/subject/kind'),
    ({'ipp': IPP, 'subject': HIKER['subject'], 'dataset': 'nope'}, '/dataset'),
    ({'ipp': IPP, 'subject': HIKER['subject'], 'calibration': 'double'}, '/calibration'),
])
def test_tarr_validation_errors(client, body, pointer):
    r = client.post('/api/v1/tarr/jobs', json=body, headers=ALICE)
    assert r.status_code == 422, r.json
    assert r.mimetype == 'application/problem+json'
    assert pointer in [e['pointer'] for e in r.json['errors']], r.json


def test_non_json_body_is_400(client):
    r = client.post('/api/v1/tarr/jobs', data='nope', headers=ALICE)
    assert r.status_code == 400 and r.mimetype == 'application/problem+json'


def test_failed_pipeline_reports_problem(client, fake_pipeline):
    fake_pipeline.fail = 'Elevation data unavailable'
    job = wait_for(client, client.post('/api/v1/tarr/jobs', json=HIKER, headers=ALICE).json['id'])
    assert job['status'] == 'failed'
    assert job['error']['detail'] == 'Elevation data unavailable'
    r = client.get(f"/api/v1/jobs/{job['id']}/outputs/contours.geojson", headers=ALICE)
    assert r.status_code == 409


# ---- travel time -----------------------------------------------------------
def test_travel_time_end_to_end(client, fake_pipeline):
    r = client.post('/api/v1/travel-time/jobs', json=TT, headers=ALICE)
    assert r.status_code == 202, r.json
    res = r.json['resolved']
    assert res['intervals_hours'] == [2, 4.5, 8]
    assert res['speed_kmh'] == 3.2187 and res['speed_mph'] == 2.0
    assert res['radius_km'] == round(3.218688 * 8 + 2, 3)
    job = wait_for(client, r.json['id'])
    assert job['status'] == 'succeeded', job['error']
    assert 'probability.tif' not in job['outputs']
    kind, kw = fake_pipeline.calls[0]
    assert kind == 'iso' and kw['intervals'] == [2, 4.5, 8] and kw['radius_km'] == 10.0
    gj = client.get(job['outputs']['contours.geojson']['href'], headers=ALICE).json
    assert [f['properties']['callsign'] for f in gj['features']] == ['2h', '4.5h', '8h']
    assert gj['features'][0]['properties']['remarks'] == 'Travel time: 2h at flat-ground speed'
    assert {f['properties']['fill-opacity'] for f in gj['features']} == {0.1}
    assert {f['properties']['stroke-width'] for f in gj['features']} == {3}
    r = client.get(f"/api/v1/jobs/{job['id']}/outputs/probability.tif", headers=ALICE)
    assert r.status_code == 404


@pytest.mark.parametrize('patch,pointer', [
    ({'speed': {'value': 13, 'unit': 'mph'}}, '/speed/value'),
    ({'speed': {'value': 2}}, '/speed/unit'),
    ({'intervals_hours': []}, '/intervals_hours'),
    ({'intervals_hours': [73]}, '/intervals_hours/0'),
    ({'intervals_hours': [1, 1]}, '/intervals_hours'),
    ({'min_radius_m': 10}, '/min_radius_m'),
])
def test_travel_time_validation(client, patch, pointer):
    r = client.post('/api/v1/travel-time/jobs', json=dict(TT, **patch), headers=ALICE)
    assert r.status_code == 422, r.json
    assert pointer in [e['pointer'] for e in r.json['errors']], r.json


# ---- job lifecycle ---------------------------------------------------------
def test_jobs_run_one_at_a_time_in_order(client, fake_pipeline):
    fake_pipeline.delay = 0.3
    ids = [client.post('/api/v1/travel-time/jobs', json=TT, headers=ALICE).json['id'] for _ in range(3)]
    time.sleep(0.1)
    third = client.get(f'/api/v1/jobs/{ids[2]}', headers=ALICE)
    assert third.json['status'] == 'queued' and third.json['queue_position'] == 2
    assert third.headers['Retry-After'] == '15'
    jobs = [wait_for(client, i) for i in ids]
    assert [j['started_at'] for j in jobs] == sorted(j['started_at'] for j in jobs)
    assert all(j['status'] == 'succeeded' for j in jobs)


def test_list_jobs_is_per_owner(client):
    a = client.post('/api/v1/tarr/jobs', json=HIKER, headers=ALICE).json['id']
    client.post('/api/v1/tarr/jobs', json=HIKER, headers=BOB)
    assert [j['id'] for j in client.get('/api/v1/jobs', headers=ALICE).json['jobs']] == [a]
    # anyone authenticated can read a job by id (sharing within an incident)
    assert client.get(f'/api/v1/jobs/{a}', headers=BOB).status_code == 200


def test_delete_rules(client, fake_pipeline):
    fake_pipeline.delay = 0.5
    running = client.post('/api/v1/tarr/jobs', json=HIKER, headers=ALICE).json['id']
    queued = client.post('/api/v1/tarr/jobs', json=HIKER, headers=ALICE).json['id']
    time.sleep(0.1)
    assert client.delete(f'/api/v1/jobs/{queued}', headers=BOB).status_code == 403
    assert client.delete(f'/api/v1/jobs/{running}', headers=ALICE).status_code == 409
    assert client.delete(f'/api/v1/jobs/{queued}', headers=ALICE).status_code == 204
    assert client.get(f'/api/v1/jobs/{queued}', headers=ALICE).status_code == 404
    wait_for(client, running)
    time.sleep(0.2)
    assert len(fake_pipeline.calls) == 1


def test_queue_full_is_503(make_app, fake_pipeline):
    app, ctx = make_app(max_queued=2)
    c = app.test_client()
    fake_pipeline.delay = 0.5
    codes = []
    for _ in range(4):
        codes.append(c.post('/api/v1/tarr/jobs', json=HIKER, headers=ALICE).status_code)
        time.sleep(0.05)
    assert codes == [202, 202, 202, 503]  # first is already running, two wait


def test_expired_job_is_410(client):
    jid = client.post('/api/v1/tarr/jobs', json=HIKER, headers=ALICE).json['id']
    wait_for(client, jid)
    client.ctx.jobs._jobs[jid]['expires_at'] = '2000-01-01T00:00:00Z'
    assert client.ctx.jobs.sweep() == [jid]
    assert not os.path.exists(client.ctx.jobs.job_dir(jid))
    assert client.get(f'/api/v1/jobs/{jid}', headers=ALICE).status_code == 410


def test_restart_marks_in_flight_jobs_failed(make_app):
    app, ctx = make_app(start_worker=False)
    jid = app.test_client().post('/api/v1/tarr/jobs', json=HIKER, headers=ALICE).json['id']
    app2, ctx2 = make_app(start_worker=False)          # same jobs_dir = a restart
    job = app2.test_client().get(f'/api/v1/jobs/{jid}', headers=ALICE).json
    assert job['status'] == 'failed' and job['error']['title'] == 'Interrupted'


# ---- legacy routes ---------------------------------------------------------
def test_legacy_analyze_runs_on_worker_with_same_contract(client, fake_pipeline):
    body = {'ipp': {'lat': 34.9523, 'lng': -111.7610}, 'percentiles': {'p25': 1, 'p50': 2, 'p75': 3}}
    assert client.post('/api/analyze', json=body).status_code == 401
    r = client.post('/api/analyze', json=body, headers=ALICE)
    assert r.status_code == 200, r.json
    assert r.json['status'] == 'ok' and r.json['analysis_id'] == '34.9523_-111.7610'
    assert fake_pipeline.threads == ['wisar-job-worker']


def test_legacy_and_v1_never_overlap(client, fake_pipeline):
    fake_pipeline.delay = 0.3
    active, peak = [0], [0]
    orig = fake_pipeline._common

    def tracking(kind, **kw):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        try:
            orig(kind, **kw)
        finally:
            active[0] -= 1
    fake_pipeline._common = tracking
    jid = client.post('/api/v1/tarr/jobs', json=HIKER, headers=ALICE).json['id']
    body = {'ipp': {'lat': 34.9523, 'lng': -111.7610}, 'speed': 3, 'speed_unit': 'kmh', 'intervals': [1]}
    out = {}
    t = threading.Thread(target=lambda: out.update(r=client.application.test_client().post(
        '/api/analyze-isochrone', json=body, headers=ALICE)))
    t.start(); t.join(15)
    wait_for(client, jid)
    assert out['r'].status_code == 200 and out['r'].json['mode'] == 'isochrone'
    assert peak[0] == 1 and len(fake_pipeline.calls) == 2


def test_ui_is_not_served(client):
    r = client.get('/')
    assert r.status_code == 302 and r.headers['Location'].endswith('/api/v1/docs')
    assert client.get('/static/app.js').status_code == 404


# ---- CORS ------------------------------------------------------------------
def test_cors_allowed_origin_and_preflight(client):
    origin = {'Origin': 'https://cloudtak.example.org'}
    pre = client.options('/api/v1/tarr/jobs', headers={**origin, 'Access-Control-Request-Method': 'POST'})
    assert pre.status_code == 200
    assert pre.headers['Access-Control-Allow-Origin'] == 'https://cloudtak.example.org'
    assert 'Authorization' in pre.headers['Access-Control-Allow-Headers']
    r = client.post('/api/v1/tarr/jobs', json=HIKER, headers={**ALICE, **origin})
    assert 'Location' in r.headers['Access-Control-Expose-Headers']
    other = client.get('/api/v1/health', headers={'Origin': 'https://evil.example.com'})
    assert 'Access-Control-Allow-Origin' not in other.headers


# ---- auth against CloudTAK -------------------------------------------------
class _Resp:
    def __init__(self, code, body=None):
        self.status_code, self._body = code, body

    def json(self):
        return self._body


class _Session:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, 0

    def get(self, url, headers, timeout):
        self.calls += 1
        assert url == 'http://api:5000/api/login'
        assert headers['Authorization'] == 'Bearer tok'
        if self.exc:
            raise self.exc
        return self.resp


def test_authenticator_against_cloudtak_login():
    import requests
    from api.auth import Authenticator
    from api.config import Settings
    from api.problems import ApiProblem
    ok = _Session(_Resp(200, {'email': 'a@b.org', 'access': 'user'}))
    auth = Authenticator(Settings(), session=ok)
    assert auth.verify('Bearer tok')['email'] == 'a@b.org'
    auth.verify('Bearer tok')
    assert ok.calls == 1  # cached
    for session, status in ((_Session(_Resp(401)), 401), (_Session(_Resp(500)), 503),
                            (_Session(exc=requests.ConnectionError()), 503)):
        with pytest.raises(ApiProblem) as e:
            Authenticator(Settings(), session=session).verify('Bearer tok')
        assert e.value.status == status
    with pytest.raises(ApiProblem) as e:
        Authenticator(Settings(), session=ok).verify('Basic abc')
    assert e.value.status == 401


# ---- several CloudTAK deployments -------------------------------------------
class _MultiSession:
    """Fake CloudTAKs: {api_url: {token: email}}."""
    def __init__(self, logins):
        self.logins, self.calls = logins, []

    def get(self, url, headers, timeout):
        api = url[:-len('/api/login')]
        self.calls.append(api)
        email = self.logins.get(api, {}).get(headers['Authorization'][7:])
        return _Resp(200, {'email': email, 'access': 'user'}) if email else _Resp(401)


A, B = 'https://map.a.org', 'https://map.b.org'
INSTANCES = f'{A}=http://cloudtak-api:5000, {B}'


def test_parse_instances():
    from api.config import parse_instances
    assert parse_instances(INSTANCES) == [(A, 'http://cloudtak-api:5000'), (B, B)]
    assert parse_instances('HTTPS://Map.A.org/=https://api.a.org/') == [(A, 'https://api.a.org')]
    assert parse_instances('') == []
    for bad in ('map.a.org', 'https://map.a.org/path', f'{A},{A}', f'{A}=ftp://x'):
        with pytest.raises(RuntimeError):
            parse_instances(bad)


def _multi_app(make_app, logins):
    from api.auth import Authenticator
    from api.config import parse_instances
    session = _MultiSession(logins)
    app, ctx = make_app(auth_factory=lambda s: Authenticator(s, session=session),
                        cloudtak_instances=parse_instances(INSTANCES), cors_origins=[])
    return app.test_client(), ctx, session


def test_origin_selects_the_cloudtak_that_verifies_the_token(make_app):
    c, ctx, session = _multi_app(make_app, {'http://cloudtak-api:5000': {'ta': 'pat@x.org'}, B: {'tb': 'pat@x.org'}})
    assert c.get('/api/v1/profiles', headers={'Authorization': 'Bearer ta', 'Origin': A}).status_code == 200
    assert c.get('/api/v1/profiles', headers={'Authorization': 'Bearer tb', 'Origin': B}).status_code == 200
    # a token is only good on the deployment that issued it
    assert c.get('/api/v1/profiles', headers={'Authorization': 'Bearer ta', 'Origin': B}).status_code == 401
    assert session.calls == ['http://cloudtak-api:5000', B, B]
    # no Origin (curl, smoke test) and WiSAR's own pages use the first deployment
    assert c.get('/api/v1/profiles', headers={'Authorization': 'Bearer ta'}).status_code == 200
    own = {'Authorization': 'Bearer ta', 'Origin': 'https://localhost'}
    assert c.get('/api/v1/profiles', headers=own, base_url='https://localhost').status_code == 200


def test_unregistered_origin_is_403(make_app):
    c, ctx, session = _multi_app(make_app, {B: {'tb': 'pat@x.org'}})
    r = c.get('/api/v1/profiles', headers={'Authorization': 'Bearer tb', 'Origin': 'https://evil.example'})
    assert r.status_code == 403 and r.json['title'] == 'CloudTAK not registered'
    assert 'Access-Control-Allow-Origin' not in r.headers
    assert session.calls == []
    r = c.post('/api/analyze', json={}, headers={'Authorization': 'Bearer tb', 'Origin': 'https://evil.example'})
    assert r.status_code == 403  # legacy routes too


def test_registered_origins_get_cors(make_app):
    c, ctx, _ = _multi_app(make_app, {})
    for origin in (A, B):
        r = c.open('/api/v1/profiles', method='OPTIONS', headers={'Origin': origin, 'Access-Control-Request-Method': 'GET'})
        assert r.headers['Access-Control-Allow-Origin'] == origin


def test_jobs_belong_to_user_and_deployment(make_app):
    c, ctx, _ = _multi_app(make_app, {'http://cloudtak-api:5000': {'ta': 'pat@x.org'}, B: {'tb': 'pat@x.org'}})
    ha = {'Authorization': 'Bearer ta', 'Origin': A}
    hb = {'Authorization': 'Bearer tb', 'Origin': B}
    ja = c.post('/api/v1/tarr/jobs', json=HIKER, headers=ha).json
    jb = c.post('/api/v1/tarr/jobs', json=HIKER, headers=hb).json
    assert (ja['owner'], ja['instance']) == ('pat@x.org', A)
    assert (jb['owner'], jb['instance']) == ('pat@x.org', B)
    assert [j['id'] for j in c.get('/api/v1/jobs', headers=ha).json['jobs']] == [ja['id']]
    assert [j['id'] for j in c.get('/api/v1/jobs', headers=hb).json['jobs']] == [jb['id']]
    wait_for(c, ja['id'], headers=ha)
    wait_for(c, jb['id'], headers=hb)
    assert c.delete(f"/api/v1/jobs/{ja['id']}", headers=hb).status_code == 403
    assert c.delete(f"/api/v1/jobs/{ja['id']}", headers=ha).status_code == 204
    assert ctx.spec.errors('Job', jb) == []


def test_jobs_from_before_instances_belong_to_the_first_deployment(make_app):
    c, ctx, _ = _multi_app(make_app, {'http://cloudtak-api:5000': {'ta': 'pat@x.org'}, B: {'tb': 'pat@x.org'}})
    old = ctx.jobs.submit('tarr', {}, {}, 'pat@x.org')  # as stored before: no instance
    ctx.jobs._jobs[old['id']].pop('instance', None)
    ids = lambda h: [j['id'] for j in c.get('/api/v1/jobs', headers=h).json['jobs']]
    assert old['id'] in ids({'Authorization': 'Bearer ta', 'Origin': A})
    assert old['id'] not in ids({'Authorization': 'Bearer tb', 'Origin': B})


def test_single_cloudtak_mode_is_unchanged(client):
    j = client.post('/api/v1/tarr/jobs', json=HIKER, headers=ALICE).json
    assert j['instance'] is None
    r = client.get('/api/v1/profiles', headers={**ALICE, 'Origin': 'https://anything.example'})
    assert r.status_code == 200  # no origin check without WISAR_CLOUDTAK_INSTANCES


# ---- responses match the published contract --------------------------------
def test_responses_match_spec_schemas(client, fake_pipeline):
    spec = client.ctx.spec
    tarr = wait_for(client, client.post('/api/v1/tarr/jobs', json=HIKER, headers=ALICE).json['id'])
    tt = wait_for(client, client.post('/api/v1/travel-time/jobs', json=TT, headers=ALICE).json['id'])
    fake_pipeline.fail = 'boom'
    failed = wait_for(client, client.post('/api/v1/tarr/jobs', json=HIKER, headers=ALICE).json['id'])
    for job in (tarr, tt, failed):
        assert spec.errors('Job', job) == [], (job['type'], spec.errors('Job', job))
    assert spec.errors('ProfileDatasetList', client.get('/api/v1/profiles', headers=ALICE).json) == []
    assert spec.errors('Health', client.get('/api/v1/health').json) == []
    gj = client.get(tarr['outputs']['contours.geojson']['href'], headers=ALICE).json
    assert spec.errors('ContourCollection', gj) == []
    problem = client.post('/api/v1/tarr/jobs', json={}, headers=ALICE).json
    assert spec.errors('Problem', problem) == []
