#!/usr/bin/env python3
# ===============================================================================
# Script:       tools/build_hydro_cache.py
# Purpose:      Build or refresh the local hydrography snapshot the WiSAR
#               pipeline reads on every analysis (pipeline/nhd_cache.py).
#               Downloads every USGS NHDPlus HR basin (HU4) GeoPackage, one
#               at a time, and streams three layers into a single spatially
#               indexed GeoPackage at /var/www/sar.weleber.net/cache/nhd/:
#
#                 flowlines    NHDFlowline joined to NHDPlusFlowlineVAA for
#                              Strahler stream order
#                 waterbodies  NHDWaterbody (lakes, ponds, reservoirs)
#                 areas        NHDArea (river polygons, rapids, canals)
#
#               All work is done by ogr2ogr, so memory stays flat no matter
#               how big a basin is. Each basin's zip and extracted GeoPackage
#               are deleted as soon as it has been appended; peak disk is
#               one basin (the largest, Ohio 0512, is a 4 GB zip that
#               unpacks to about 8 GB) plus the growing output.
#
#               Full build: 266 basins, ~116 GB downloaded, output ~20 GB,
#               several hours. NHDPlus HR changes rarely, so this runs
#               quarterly, not weekly.
#
#               Safe to re-run: builds nhd_cache.gpkg.tmp and only replaces
#               the live file at the end, so a running analysis never sees
#               a half-built GeoPackage.
#
# Dependencies:
#   - ogr2ogr / ogrinfo (apt install gdal-bin)
#   - requests
#
# Usage:
#   Full build:               python3 tools/build_hydro_cache.py
#   Subset for testing:       python3 tools/build_hydro_cache.py --hu4 1506 1507
#   Whole regions:            python3 tools/build_hydro_cache.py --regions 15 14
#   Cron (quarterly, 1st of Jan/Apr/Jul/Oct 04:00 MST):
#     0 11 1 1,4,7,10 * /var/www/sar.weleber.net/app/venv/bin/python3 /var/www/sar.weleber.net/app/tools/build_hydro_cache.py >> /var/www/sar.weleber.net/cache/nhd/build.log 2>&1
#
# Author:       Jamie F. Weleber
# Created:      September 2026 (v1.17)
# ===============================================================================

import os
import re
import sys
import json
import time
import shutil
import zipfile
import argparse
import logging
import subprocess
from datetime import datetime, timezone

import requests


# ===============================================================================
# STEP 1: Configuration
# ===============================================================================

# Must match pipeline/nhd_cache.py. Duplicated on purpose so cron can run
# this script without the pipeline package on sys.path.
CACHE_DIR = '/var/www/sar.weleber.net/cache/nhd'
CACHE_GPKG = os.path.join(CACHE_DIR, 'nhd_cache.gpkg')
CACHE_GPKG_TMP = os.path.join(CACHE_DIR, 'nhd_cache.gpkg.tmp')
CACHE_METADATA = os.path.join(CACHE_DIR, 'nhd_cache_metadata.json')
CACHE_METADATA_TMP = os.path.join(CACHE_DIR, 'nhd_cache_metadata.json.tmp')
WORK_DIR = os.path.join(CACHE_DIR, 'work')

# USGS staged products bucket. The listing is public and paginated.
S3_BUCKET_URL = 'https://prd-tnm.s3.amazonaws.com/'
S3_PREFIX = 'StagedProducts/Hydrography/NHDPlusHR/VPU/Current/GPKG/'
USER_AGENT = 'WiSAR-DST/1.17 (+https://sar.weleber.net)'

# Basin zips are named NHDPLUS_H_<HU4>_HU4_[<date>_]GPKG.zip
KEY_RE = re.compile(r'NHDPLUS_H_(\d{4})_HU4_(?:(\d{8})_)?GPKG\.zip$')

# ogr2ogr layer definitions: (output layer, geometry type, SQL over the
# source GeoPackage). Field names are lower-cased to match the reader.
# Source CRS is NAD83 (EPSG:4269); output is WGS84 to match the rest of
# the pipeline. 3D/measured geometries are flattened to XY.
LAYER_SPECS = [
    ('flowlines', 'MULTILINESTRING',
     'SELECT f.Shape, f.GNIS_Name AS gnis_name, f.FType AS ftype, f.FCode AS fcode, '
     'v.StreamOrde AS stream_order '
     'FROM NHDFlowline f LEFT JOIN NHDPlusFlowlineVAA v ON f.NHDPlusID = v.NHDPlusID'),
    ('waterbodies', 'MULTIPOLYGON',
     'SELECT Shape, GNIS_Name AS gnis_name, FType AS ftype, FCode AS fcode, '
     'AreaSqKm AS areasqkm FROM NHDWaterbody'),
    ('areas', 'MULTIPOLYGON',
     'SELECT Shape, GNIS_Name AS gnis_name, FType AS ftype, FCode AS fcode FROM NHDArea'),
]

# Peak: largest basin zip (~4 GB) + its extracted GeoPackage (~8 GB) + the
# finished output (~20 GB) alongside the still-live previous cache (~20 GB).
MIN_FREE_DISK_GB = 40
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY_S = 60


# ===============================================================================
# STEP 2: Helpers
# ===============================================================================

def setup_logging(verbose):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format='[%(asctime)s] %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        stream=sys.stdout,
    )


def check_disk_space(required_gb):
    probe = CACHE_DIR if os.path.isdir(CACHE_DIR) else os.path.dirname(CACHE_DIR)
    free_gb = shutil.disk_usage(probe).free / (1024 ** 3)
    if free_gb < required_gb:
        logging.error(f"Only {free_gb:.1f} GB free at {probe}, need {required_gb} GB. Aborting.")
        sys.exit(3)
    logging.info(f"Disk space OK: {free_gb:.1f} GB free")


def check_gdal_available():
    for exe in ('ogr2ogr', 'ogrinfo'):
        if shutil.which(exe) is None:
            logging.error(f"{exe} not found on PATH. Install with: sudo apt install gdal-bin")
            sys.exit(6)
    result = subprocess.run(['ogrinfo', '--version'], capture_output=True, text=True, timeout=10)
    logging.info(f"GDAL available: {result.stdout.strip()}")


def list_basins():
    """Return {hu4: (key, last_modified, size_bytes)} from the S3 listing.

    A basin can appear more than once when USGS reissues it; keep the most
    recently modified key.
    """
    basins = {}
    token = None
    while True:
        params = {'list-type': '2', 'prefix': S3_PREFIX, 'max-keys': '1000'}
        if token:
            params['continuation-token'] = token
        r = requests.get(S3_BUCKET_URL, params=params, timeout=60,
                         headers={'User-Agent': USER_AGENT})
        r.raise_for_status()
        xml = r.text
        for m in re.finditer(r'<Key>([^<]+)</Key><LastModified>([^<]+)</LastModified>'
                             r'<ETag>[^<]*</ETag><Size>(\d+)</Size>', xml):
            key, modified, size = m.group(1), m.group(2), int(m.group(3))
            km = KEY_RE.search(key)
            if not km:
                continue
            hu4 = km.group(1)
            if hu4 not in basins or modified > basins[hu4][1]:
                basins[hu4] = (key, modified, size)
        tm = re.search(r'<NextContinuationToken>([^<]+)</NextContinuationToken>', xml)
        if tm and '<IsTruncated>true</IsTruncated>' in xml:
            token = tm.group(1)
        else:
            break
    return basins


def download_zip(hu4, key, dest_path, expected_size):
    """Stream one basin zip to disk; .part rename on success."""
    url = S3_BUCKET_URL + key
    if os.path.isfile(dest_path) and os.path.getsize(dest_path) == expected_size:
        logging.info(f"  {hu4}: using existing zip")
        return True
    start = time.time()
    tmp_path = dest_path + '.part'
    try:
        with requests.get(url, stream=True, timeout=120,
                          headers={'User-Agent': USER_AGENT}) as r:
            r.raise_for_status()
            done = 0
            with open(tmp_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        done += len(chunk)
        if expected_size and done != expected_size:
            raise IOError(f"size mismatch: got {done}, expected {expected_size}")
        os.replace(tmp_path, dest_path)
        logging.info(f"  {hu4}: downloaded {done / 1e9:.2f} GB in {time.time() - start:.0f} s")
        return True
    except Exception as e:
        logging.warning(f"  {hu4}: download failed: {e}")
        if os.path.isfile(tmp_path):
            os.remove(tmp_path)
        return False


def extract_gpkg(zip_path, dest_dir):
    """Extract the basin GeoPackage from its zip. Returns the .gpkg path."""
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith('.gpkg')]
        if len(names) != 1:
            raise RuntimeError(f"expected one .gpkg in {zip_path}, found {names}")
        name = names[0]
        out_path = os.path.join(dest_dir, os.path.basename(name))
        with zf.open(name) as src, open(out_path, 'wb') as dst:
            shutil.copyfileobj(src, dst, length=16 * 1024 * 1024)
    return out_path


def basin_extent(gpkg_path):
    """Envelope of the basin's WBDHU4 boundary as [west, south, east, north].

    Source CRS is NAD83; the difference from WGS84 is around a metre, which
    is irrelevant for a coverage test.
    """
    result = subprocess.run(['ogrinfo', '-so', gpkg_path, 'WBDHU4'],
                            capture_output=True, text=True, timeout=300)
    m = re.search(r'Extent: \(([-\d.]+), ([-\d.]+)\) - \(([-\d.]+), ([-\d.]+)\)', result.stdout)
    if not m:
        return None
    return [float(m.group(i)) for i in range(1, 5)]


def append_layers(gpkg_path, hu4, target_exists):
    """Run ogr2ogr for each layer spec, appending into the target GeoPackage.

    Returns {layer: seconds_taken}. Raises on ogr2ogr failure so the caller
    can decide whether to skip the basin or abort.
    """
    timings = {}
    for layer, geom_type, sql in LAYER_SPECS:
        cmd = ['ogr2ogr', '-f', 'GPKG']
        if target_exists:
            cmd += ['-update', '-append']
        cmd += [CACHE_GPKG_TMP, gpkg_path,
                '-dialect', 'sqlite', '-sql', sql,
                '-nln', layer, '-nlt', geom_type, '-dim', 'XY',
                '-t_srs', 'EPSG:4326',
                '-gt', '65536', '--config', 'OGR_SQLITE_SYNCHRONOUS', 'OFF']
        if not target_exists:
            cmd += ['-lco', 'SPATIAL_INDEX=YES', '-lco', 'FID=fid']
        t0 = time.time()
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ogr2ogr {layer} failed: {result.stderr.strip()[-500:]}")
        timings[layer] = time.time() - t0
        target_exists = True
    return timings


def layer_counts(gpkg_path):
    """Feature count per output layer, via ogrinfo -so."""
    out = {}
    for layer, _, _ in LAYER_SPECS:
        result = subprocess.run(['ogrinfo', '-so', gpkg_path, layer],
                                capture_output=True, text=True, timeout=600)
        m = re.search(r'Feature Count: (\d+)', result.stdout)
        out[layer] = int(m.group(1)) if m else 0
    return out


def write_metadata(hu4s, hu4_bboxes, source_dates, counts, failed):
    meta = {
        'built_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'product': 'USGS NHDPlus High Resolution, basin (HU4) GeoPackages',
        'hu4s': hu4s,
        'hu4_bboxes': hu4_bboxes,          # hu4 -> [west, south, east, north]
        'source_last_modified': source_dates,   # hu4 -> S3 LastModified
        'feature_counts': counts,
        'failed_hu4s': failed,
        'cache_version': 1,
    }
    with open(CACHE_METADATA_TMP, 'w') as f:
        json.dump(meta, f, indent=2)
    os.replace(CACHE_METADATA_TMP, CACHE_METADATA)
    logging.info(f"Metadata written to {CACHE_METADATA}")


# ===============================================================================
# STEP 3: Main driver
# ===============================================================================

def main():
    parser = argparse.ArgumentParser(description='Build the WiSAR NHDPlus HR hydrography cache')
    parser.add_argument('--hu4', nargs='+', default=None,
                        help='Subset of HU4 codes to build (e.g. 1506 1507)')
    parser.add_argument('--regions', nargs='+', default=None,
                        help='Subset of two-digit hydrologic regions (e.g. 15 14)')
    parser.add_argument('--keep-downloads', action='store_true',
                        help='Keep basin zips and GeoPackages after use')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    setup_logging(args.verbose)
    t_total = time.time()
    logging.info("=" * 70)
    logging.info("WiSAR NHDPlus HR Hydrography Cache Build")
    logging.info("=" * 70)

    os.makedirs(WORK_DIR, exist_ok=True)
    check_disk_space(MIN_FREE_DISK_GB)
    check_gdal_available()

    # --- Discover basins ---
    logging.info("Listing NHDPlus HR basin packages...")
    basins = list_basins()
    if not basins:
        logging.error("S3 listing returned no basin packages. Aborting.")
        sys.exit(4)
    selected = sorted(basins)
    if args.hu4:
        selected = [h for h in selected if h in set(args.hu4)]
    if args.regions:
        selected = [h for h in selected if h[:2] in set(args.regions)]
    if not selected:
        logging.error("No basins match the requested subset. Aborting.")
        sys.exit(1)
    total_gb = sum(basins[h][2] for h in selected) / 1e9
    logging.info(f"{len(selected)} basins selected, {total_gb:.1f} GB to download")

    if os.path.isfile(CACHE_GPKG_TMP):
        os.remove(CACHE_GPKG_TMP)

    # --- Stream basins into the temporary GeoPackage ---
    done_hu4s, hu4_bboxes, source_dates, failed = [], {}, {}, []
    target_exists = False
    for i, hu4 in enumerate(selected, 1):
        key, modified, size = basins[hu4]
        logging.info(f"[{i}/{len(selected)}] {hu4} ({size / 1e9:.2f} GB)")
        zip_path = os.path.join(WORK_DIR, os.path.basename(key))
        gpkg_path = None
        try:
            for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
                if download_zip(hu4, key, zip_path, size):
                    break
                if attempt < DOWNLOAD_ATTEMPTS:
                    time.sleep(DOWNLOAD_RETRY_DELAY_S)
            else:
                raise RuntimeError("download failed after retries")

            gpkg_path = extract_gpkg(zip_path, WORK_DIR)
            if not args.keep_downloads:
                os.remove(zip_path)

            timings = append_layers(gpkg_path, hu4, target_exists)
            target_exists = True
            hu4_bboxes[hu4] = basin_extent(gpkg_path)
            source_dates[hu4] = modified
            done_hu4s.append(hu4)
            logging.info(f"  {hu4}: appended "
                         + ", ".join(f"{k} {v:.0f}s" for k, v in timings.items()))
        except Exception as e:
            logging.error(f"  {hu4}: FAILED — {e}")
            failed.append(hu4)
            if not target_exists:
                logging.error("First basin failed; nothing to append to. Aborting.")
                sys.exit(7)
        finally:
            if gpkg_path and os.path.isfile(gpkg_path) and not args.keep_downloads:
                os.remove(gpkg_path)

    if not done_hu4s:
        logging.error("No basins were written. Aborting; the existing cache stays live.")
        sys.exit(5)

    # --- Promote and record ---
    logging.info("Counting features...")
    counts = layer_counts(CACHE_GPKG_TMP)
    logging.info(f"  {counts}")
    logging.info(f"Promoting {CACHE_GPKG_TMP} -> {CACHE_GPKG}")
    os.replace(CACHE_GPKG_TMP, CACHE_GPKG)
    write_metadata(done_hu4s, hu4_bboxes, source_dates, counts, failed)

    size_gb = os.path.getsize(CACHE_GPKG) / 1e9
    logging.info(f"Done: {len(done_hu4s)} basins, {len(failed)} failed, "
                 f"{size_gb:.1f} GB, {(time.time() - t_total) / 60:.0f} min")
    if failed:
        logging.warning(f"Failed basins: {failed}")


if __name__ == '__main__':
    main()
