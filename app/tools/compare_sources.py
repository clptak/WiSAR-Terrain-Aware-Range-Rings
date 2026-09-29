#!/usr/bin/env python3
# ===============================================================================
# Script:       tools/compare_sources.py
# Purpose:      Side-by-side check of the local land cover and hydrography
#               snapshots against the live services they replaced (v1.17).
#               For one IPP it builds the friction surface, cost-distance,
#               TARR contours and Jacobs stream mask twice — once from the
#               MRLC WMS and USGS hydro MapServer, once from the caches —
#               and reports how the results differ.
#
#               Run on the server, where the caches and the venv live:
#                 cd /var/www/sar.weleber.net/app
#                 venv/bin/python3 tools/compare_sources.py --lat 34.9903 --lng -111.7431
#
#               The live fetchers are copied here verbatim from the retired
#               pipeline code so this keeps working as a regression tool
#               after the switch. Live services are flaky; a timeout shows
#               up as "None" or an empty layer on the live side, which is
#               itself part of what this tool demonstrates.
#
# Author:       Jamie F. Weleber
# Created:      September 2026 (v1.17)
# ===============================================================================

import os
import sys
import json
import math
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import rasterio
import requests
import geopandas as gpd
from shapely.geometry import shape

from pipeline.shared import WORK_DIR, get_bbox_from_ipp
from pipeline.downloads import download_dem, download_osm_features, WISAR_USER_AGENT
from pipeline.cost_surface import build_cost_surface
from pipeline.cost_distance import compute_cost_distance
from pipeline.outputs import extract_contour_polygons
from pipeline.jacobs_masks import compute_jacobs_masks
from pipeline import nlcd_cache, nhd_cache


# ===============================================================================
# Live fetchers — verbatim copies of the v1.16 pipeline code
# ===============================================================================

def _pixel_dims(bbox):
    west, south, east, north = bbox
    center_lat = (south + north) / 2
    m_per_deg_lng = 111320 * math.cos(math.radians(center_lat))
    m_per_deg_lat = 110540
    width_px = max(int((east - west) * m_per_deg_lng / 30), 1)
    height_px = max(int((north - south) * m_per_deg_lat / 30), 1)
    if width_px > 1000 or height_px > 1000:
        scale = 1000 / max(width_px, height_px)
        width_px, height_px = int(width_px * scale), int(height_px * scale)
    return width_px, height_px


def live_download_nlcd(bbox, output_path):
    west, south, east, north = bbox
    width_px, height_px = _pixel_dims(bbox)
    url = "https://www.mrlc.gov/geoserver/mrlc_download/NLCD_2021_Land_Cover_L48/ows"
    params = {
        'service': 'WMS', 'version': '1.1.1', 'request': 'GetMap',
        'layers': 'NLCD_2021_Land_Cover_L48',
        'bbox': f'{west},{south},{east},{north}',
        'width': width_px, 'height': height_px,
        'srs': 'EPSG:4326', 'styles': '', 'format': 'image/geotiff',
    }
    try:
        r = requests.get(url, params=params, timeout=120, headers={'User-Agent': WISAR_USER_AGENT})
        r.raise_for_status()
        with open(output_path, 'wb') as f:
            f.write(r.content)
        with rasterio.open(output_path):
            pass
        return output_path
    except Exception as e:
        print(f"  live NLCD failed: {e}")
        return None


def live_download_nhd_features(bbox):
    west, south, east, north = bbox
    geom_str = f'{west},{south},{east},{north}'
    base = "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer"
    rows = []

    def q(layer, fields, count):
        params = {'geometry': geom_str, 'geometryType': 'esriGeometryEnvelope',
                  'inSR': '4326', 'outSR': '4326', 'spatialRel': 'esriSpatialRelIntersects',
                  'outFields': fields, 'f': 'geojson', 'returnGeometry': 'true',
                  'resultRecordCount': count}
        r = requests.get(f"{base}/{layer}/query", params=params, timeout=60,
                         headers={'User-Agent': WISAR_USER_AGENT})
        r.raise_for_status()
        return r.json().get('features', [])

    try:
        for f in q(12, 'GNIS_NAME,FTYPE,FCODE,AREASQKM', 500):
            p = f.get('properties', {})
            rows.append({'geometry': shape(f['geometry']), 'type': 'waterbody',
                         'ftype': p.get('FTYPE', 0), 'name': p.get('GNIS_NAME') or 'unnamed',
                         'impedance': 99})
    except Exception as e:
        print(f"  live NHD waterbodies failed: {e}")
    try:
        for f in q(9, 'GNIS_NAME,FTYPE,FCODE', 500):
            p = f.get('properties', {})
            ftype = p.get('FTYPE', 0)
            if ftype in (460, 431, 336, 390):
                rows.append({'geometry': shape(f['geometry']), 'type': 'river_area',
                             'ftype': ftype, 'name': p.get('GNIS_NAME') or 'unnamed',
                             'impedance': 99 if ftype in (460, 390) else 80})
    except Exception as e:
        print(f"  live NHD areas failed: {e}")
    try:
        for f in q(4, 'GNIS_NAME,FTYPE,FCODE,StreamOrde', 1000):
            p = f.get('properties', {})
            order = p.get('StreamOrde', 0) or 0
            g = shape(f['geometry'])
            if g.is_empty:
                continue
            if order >= 7:
                buf, imp = 0.0004, 99
            elif order >= 5:
                buf, imp = 0.0001, 80
            elif order >= 3:
                buf, imp = 0.00005, 60
            else:
                buf, imp = 0.00002, 40
            rows.append({'geometry': g.buffer(buf), 'type': 'flowline', 'ftype': order,
                         'name': p.get('GNIS_NAME') or 'unnamed', 'impedance': imp})
    except Exception as e:
        print(f"  live NHD flowlines failed: {e}")

    cols = ['geometry', 'type', 'ftype', 'name', 'impedance']
    if not rows:
        return gpd.GeoDataFrame(columns=cols, geometry='geometry', crs='EPSG:4326')
    return gpd.GeoDataFrame(rows, geometry='geometry', crs='EPSG:4326')[cols]


# ===============================================================================
# Reporting helpers
# ===============================================================================

def nhd_summary(gdf):
    if gdf is None or len(gdf) == 0:
        return {'total': 0}
    out = {'total': int(len(gdf))}
    for t in ('waterbody', 'river_area', 'flowline'):
        out[t] = int((gdf['type'] == t).sum())
    fl = gdf[gdf['type'] == 'flowline']
    if len(fl):
        out['flowlines_order_ge3'] = int((fl['ftype'] >= 3).sum())
        out['flowlines_order_ge5'] = int((fl['ftype'] >= 5).sum())
    return out


def contour_areas_km2(fc):
    out = {}
    for f in fc.get('features', []):
        g = gpd.GeoSeries([shape(f['geometry'])], crs='EPSG:4326').to_crs('EPSG:5070')
        out[f['properties'].get('percentile', '?')] = round(float(g.area.iloc[0]) / 1e6, 2)
    return out


def raster_diff(path_a, path_b):
    with rasterio.open(path_a) as a, rasterio.open(path_b) as b:
        A, B = a.read(1).astype('float64'), b.read(1).astype('float64')
    if A.shape != B.shape:
        return {'shape_a': A.shape, 'shape_b': B.shape, 'comparable': False}
    finite = np.isfinite(A) & np.isfinite(B)
    diff = np.abs(A - B)[finite]
    return {'cells': int(finite.sum()),
            'cells_differing': int((diff > 1e-6).sum()),
            'pct_differing': round(100 * float((diff > 1e-6).mean()), 2) if diff.size else 0.0,
            'mean_abs_diff': round(float(diff.mean()), 3) if diff.size else 0.0,
            'max_abs_diff': round(float(diff.max()), 3) if diff.size else 0.0}


def stream_mask_cells(masks_path):
    if not masks_path or not os.path.isfile(masks_path):
        return None
    with rasterio.open(masks_path) as src:
        return int((src.read(1) > 0).sum())


# ===============================================================================
# Main
# ===============================================================================

def run_variant(tag, bbox, dem_path, osm_features, nlcd_path, nhd_features,
                lat, lng, p25, p50, p75):
    t0 = time.time()
    cost_path = build_cost_surface(dem_path, nlcd_path, osm_features, nhd_features=nhd_features,
                                   output_path=os.path.join(WORK_DIR, f'cost_surface_{tag}.tif'))
    cd_path = compute_cost_distance(cost_path, lat, lng, dem_path,
                                    output_path=os.path.join(WORK_DIR, f'cost_distance_{tag}.tif'))
    contours = extract_contour_polygons(cd_path, p25, p50, p75)
    masks_path = None
    try:
        masks_path = compute_jacobs_masks(cost_distance_path=cd_path, dem_path=dem_path,
                                          osm_features=osm_features, nhd_features=nhd_features,
                                          output_path=os.path.join(WORK_DIR, f'jacobs_{tag}.tif'))
    except Exception as e:
        print(f"  [{tag}] jacobs masks failed: {e}")
    return {'cost_path': cost_path, 'cd_path': cd_path,
            'areas_km2': contour_areas_km2(contours),
            'stream_mask_cells': stream_mask_cells(masks_path),
            'seconds': round(time.time() - t0, 1)}


def main():
    ap = argparse.ArgumentParser(description='Compare live vs cached NLCD/NHD for one IPP')
    ap.add_argument('--lat', type=float, required=True)
    ap.add_argument('--lng', type=float, required=True)
    ap.add_argument('--p25', type=float, default=1.0, help='km, already calibrated')
    ap.add_argument('--p50', type=float, default=2.0)
    ap.add_argument('--p75', type=float, default=4.0)
    ap.add_argument('--json', default=None, help='Write the report to this path too')
    args = ap.parse_args()

    bbox = get_bbox_from_ipp(args.lat, args.lng, args.p75 + 2.0)
    print(f"bbox: {bbox}")
    report = {'ipp': [args.lat, args.lng], 'bbox': list(bbox),
              'percentiles_km': [args.p25, args.p50, args.p75]}

    dem_path = download_dem(bbox)
    osm = download_osm_features(bbox)
    osm.pop('_warnings', None)

    # --- Land cover ---
    t0 = time.time()
    live_nlcd = live_download_nlcd(bbox, os.path.join(WORK_DIR, 'nlcd_live.tif'))
    report['nlcd_live'] = {'path': live_nlcd, 'seconds': round(time.time() - t0, 1)}
    t0 = time.time()
    cache_nlcd = None
    if nlcd_cache.cache_is_available():
        cache_nlcd = nlcd_cache.load_nlcd_from_cache(bbox, os.path.join(WORK_DIR, 'nlcd_cache.tif'))
    report['nlcd_cache'] = {'path': cache_nlcd, 'seconds': round(time.time() - t0, 1),
                            'metadata': nlcd_cache.read_cache_metadata()}

    # --- Hydrography ---
    t0 = time.time()
    live_nhd = live_download_nhd_features(bbox)
    report['nhd_live'] = {**nhd_summary(live_nhd), 'seconds': round(time.time() - t0, 1)}
    t0 = time.time()
    cache_nhd = nhd_cache.load_nhd_from_cache(bbox) if nhd_cache.cache_is_available() else None
    report['nhd_cache'] = {**nhd_summary(cache_nhd), 'seconds': round(time.time() - t0, 1)}

    # --- Full pipeline both ways ---
    print("\n=== live variant ===")
    live = run_variant('live', bbox, dem_path, osm, live_nlcd, live_nhd,
                       args.lat, args.lng, args.p25, args.p50, args.p75)
    print("\n=== cache variant ===")
    cache = run_variant('cache', bbox, dem_path, osm, cache_nlcd, cache_nhd,
                        args.lat, args.lng, args.p25, args.p50, args.p75)
    report['live'] = {k: v for k, v in live.items() if not k.endswith('_path')}
    report['cache'] = {k: v for k, v in cache.items() if not k.endswith('_path')}
    report['cost_surface_diff'] = raster_diff(live['cost_path'], cache['cost_path'])
    report['cost_distance_diff'] = raster_diff(live['cd_path'], cache['cd_path'])

    print("\n" + "=" * 70)
    print(json.dumps(report, indent=2, default=str))
    if args.json:
        with open(args.json, 'w') as f:
            json.dump(report, f, indent=2, default=str)
        print(f"report written to {args.json}")


if __name__ == '__main__':
    main()
