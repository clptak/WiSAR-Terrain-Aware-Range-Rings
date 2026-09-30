"""Test fixtures. The pipeline is replaced by a fake that writes small real
GeoTIFFs, so everything downstream of it (queue, COG output, attractor
score, KML, legacy wrapping) runs unmodified. No network, no snapshots."""
import os
import sys
import threading
import time
import types

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'app'))

N = 60  # grid size


def _write(path, arr, dtype='float32', nodata=-9999, count=1):
    with rasterio.open(path, 'w', driver='GTiff', width=N, height=N, count=count, dtype=dtype,
                       crs='EPSG:4326', transform=from_origin(-111.8, 35.0, 0.0003, 0.0003), nodata=nodata) as dst:
        if count == 1:
            dst.write(arr.astype(dtype), 1)
        else:
            dst.write(arr.astype(dtype))


def _fake_rasters(workdir, with_probability):
    yy, xx = np.mgrid[0:N, 0:N]
    cd = np.hypot(yy - N / 2, xx - N / 2) * 30.0
    cd[0, 0] = -9999
    paths = {k: os.path.join(workdir, k + '.tif') for k in ('cost_distance', 'cost_surface', 'probability', 'jacobs')}
    _write(paths['cost_distance'], cd)
    _write(paths['cost_surface'], np.full((N, N), 1.5))
    masks = np.zeros((5, N, N), dtype=np.uint8)
    masks[0, 10:20, :] = 1      # stream
    masks[4, :, 30] = 1         # trail
    masks[1, 10:20, 30] = 1     # intersection
    _write(paths['jacobs'], masks, dtype='uint8', nodata=None, count=5)
    if with_probability:
        _write(paths['probability'], np.where(cd < 300, 4, 1), nodata=0)
    return paths


SQUARE = {'type': 'Polygon', 'coordinates': [[[-111.79, 34.99], [-111.785, 34.99], [-111.785, 34.985],
                                             [-111.79, 34.985], [-111.79, 34.99]],
                                            [[-111.788, 34.988], [-111.787, 34.988], [-111.787, 34.987],
                                             [-111.788, 34.988]]]}


class FakePipeline:
    def __init__(self, workdir):
        self.workdir = workdir
        self.calls = []
        self.delay = 0.0
        self.fail = None
        self.threads = []

    def _common(self, kind, **kw):
        self.calls.append((kind, kw))
        self.threads.append(threading.current_thread().name)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError(self.fail)

    def run_analysis(self, ipp_lat, ipp_lng, pct_25_km, pct_50_km, pct_75_km, radius_km=5.0):
        self._common('tarr', ipp_lat=ipp_lat, ipp_lng=ipp_lng, p=(pct_25_km, pct_50_km, pct_75_km), radius_km=radius_km)
        p = _fake_rasters(self.workdir, True)
        feats = [{'type': 'Feature', 'geometry': SQUARE,
                  'properties': {'percentile': lab, 'threshold_m': km * 1000, 'color': col,
                                 'label_lat': 34.98, 'label_lng': -111.78}}
                 for lab, km, col in (('25%', pct_25_km, '#ffffff'), ('50%', pct_50_km, '#ffca00'),
                                      ('75%', pct_75_km, '#ff6a1a'))]
        return {'cost_distance_path': p['cost_distance'], 'cost_surface_path': p['cost_surface'],
                'probability_path': p['probability'], 'dem_path': p['cost_surface'], 'nlcd_path': None,
                'jacobs_masks_path': p['jacobs'], 'work_dir': self.workdir,
                'contour_geojson': {'type': 'FeatureCollection', 'features': feats},
                'warnings': [{'severity': 'info', 'source': 'dem', 'message': 'test note'}]}

    def run_isochrone_analysis(self, ipp_lat, ipp_lng, base_speed_kmh, time_intervals_hours, radius_km=10.0):
        self._common('iso', ipp_lat=ipp_lat, speed=base_speed_kmh, intervals=time_intervals_hours, radius_km=radius_km)
        p = _fake_rasters(self.workdir, False)
        feats = [{'type': 'Feature', 'geometry': SQUARE,
                  'properties': {'hours': h, 'label': f'{h}h', 'threshold_m': h * base_speed_kmh * 1000,
                                 'color': '#00bcd4', 'label_lat': 34.98, 'label_lng': -111.78}}
                 for h in time_intervals_hours]
        return {'cost_distance_path': p['cost_distance'], 'cost_surface_path': p['cost_surface'],
                'probability_path': None, 'dem_path': p['cost_surface'], 'nlcd_path': None,
                'jacobs_masks_path': p['jacobs'], 'work_dir': self.workdir,
                'contour_geojson': {'type': 'FeatureCollection', 'features': feats}, 'warnings': []}


class FakeAuth:
    """Stands in for Authenticator; tokens map to users."""
    def __init__(self):
        from api.problems import ApiProblem
        self.ApiProblem = ApiProblem
        self.users = {'alice-token': {'email': 'alice@example.org', 'access': 'user'},
                      'bob-token': {'email': 'bob@example.org', 'access': 'user'}}

    def verify(self, header):
        token = (header or '').replace('Bearer ', '')
        if token not in self.users:
            raise self.ApiProblem(401, 'Unauthorized', 'bad token')
        return dict(self.users[token])


@pytest.fixture
def fake_pipeline(tmp_path, monkeypatch):
    fp = FakePipeline(str(tmp_path / 'work'))
    os.makedirs(fp.workdir)
    mod = types.ModuleType('pipeline')
    mod.run_analysis = fp.run_analysis
    mod.run_isochrone_analysis = fp.run_isochrone_analysis
    monkeypatch.setitem(sys.modules, 'pipeline', mod)
    return fp


@pytest.fixture
def make_app(tmp_path, fake_pipeline):
    created = []

    def _make(**overrides):
        import importlib
        import flask
        import server
        importlib.reload(server)  # fresh Flask app per test
        from api import init_api
        from api.config import Settings
        s = Settings(jobs_dir=str(tmp_path / 'jobs'), cors_origins=['https://cloudtak.example.org'],
                     sweep_interval_seconds=3600)
        for k, v in overrides.items():
            setattr(s, k, v)
        ctx = init_api(server.app, settings=s, auth=FakeAuth())
        server.app.config['TESTING'] = True
        created.append(ctx)
        return server.app, ctx
    yield _make
    for ctx in created:
        ctx.jobs.stop()


@pytest.fixture
def client(make_app):
    app, ctx = make_app()
    c = app.test_client()
    c.ctx = ctx
    return c


ALICE = {'Authorization': 'Bearer alice-token'}
BOB = {'Authorization': 'Bearer bob-token'}


def wait_for(client, job_id, headers=ALICE, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f'/api/v1/jobs/{job_id}', headers=headers)
        if r.json['status'] in ('succeeded', 'failed'):
            return r.json
        time.sleep(0.05)
    raise AssertionError('job did not finish')
