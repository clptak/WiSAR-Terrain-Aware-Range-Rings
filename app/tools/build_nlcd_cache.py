#!/usr/bin/env python3
# ===============================================================================
# Script:       tools/build_nlcd_cache.py
# Purpose:      Download the Annual NLCD land cover GeoTIFF for CONUS from
#               MRLC and install it as the WiSAR land cover snapshot at
#               /var/www/sar.weleber.net/cache/nlcd/. The pipeline reads a
#               bbox window out of it on every analysis (pipeline/nlcd_cache.py).
#
#               One file, about 1.4 GB zipped. Land cover does not change
#               week to week, so this runs once and then yearly, when MRLC
#               publishes the next annual release. By default the script
#               probes for the newest year MRLC serves; --year pins one.
#
#               Safe to re-run: writes to a temporary file and only replaces
#               the live raster at the end, so a running analysis never sees
#               a half-written file.
#
# Dependencies:
#   - gdalinfo / gdal_translate (apt install gdal-bin) — tiling check and rewrite
#   - requests                                          — download
#
# Usage:
#   First build / yearly refresh:  python3 tools/build_nlcd_cache.py
#   Pin a year:                    python3 tools/build_nlcd_cache.py --year 2024
#   Cron (yearly, Aug 1 04:00 MST): 0 11 1 8 * /var/www/sar.weleber.net/app/venv/bin/python3 /var/www/sar.weleber.net/app/tools/build_nlcd_cache.py >> /var/www/sar.weleber.net/cache/nlcd/build.log 2>&1
#
# Author:       Jamie F. Weleber
# Created:      September 2026 (v1.17)
# ===============================================================================

import os
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

# Must match pipeline/nlcd_cache.py. Duplicated on purpose so cron can run
# this script without the pipeline package on sys.path.
CACHE_DIR = '/var/www/sar.weleber.net/cache/nlcd'
CACHE_TIF = os.path.join(CACHE_DIR, 'nlcd_landcover.tif')
CACHE_TIF_TMP = os.path.join(CACHE_DIR, 'nlcd_landcover.tif.tmp')
CACHE_METADATA = os.path.join(CACHE_DIR, 'nlcd_cache_metadata.json')
CACHE_METADATA_TMP = os.path.join(CACHE_DIR, 'nlcd_cache_metadata.json.tmp')
WORK_DIR = os.path.join(CACHE_DIR, 'work')

# Annual NLCD Collection 1.1, CONUS land cover. The S3 bucket MRLC used to
# publish to now returns 403; the mrlc.gov downloads path is what works and
# it honours a normal User-Agent.
MRLC_URL_TEMPLATE = ('https://www.mrlc.gov/downloads/sciweb1/shared/mrlc/'
                     'data-bundles/Annual_NLCD_LndCov_{year}_CU_C1V1.zip')
USER_AGENT = 'WiSAR-DST/1.17 (+https://sar.weleber.net)'

# Newest year to probe first when --year is not given. MRLC publishes the
# prior calendar year around mid-year, so probing from the current year
# downward finds the latest release.
PROBE_YEARS_BACK = 4

# The zip is ~1.4 GB, the extracted GeoTIFF about the same, and a retile
# (if needed) writes a second copy. 10 GB keeps a comfortable margin.
MIN_FREE_DISK_GB = 10
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
    for exe in ('gdalinfo', 'gdal_translate'):
        if shutil.which(exe) is None:
            logging.error(f"{exe} not found on PATH. Install with: sudo apt install gdal-bin")
            sys.exit(6)
    logging.info("GDAL tools available")


def find_latest_year():
    """Probe MRLC for the newest Annual NLCD release, newest year first."""
    this_year = datetime.now(timezone.utc).year
    for year in range(this_year, this_year - PROBE_YEARS_BACK - 1, -1):
        url = MRLC_URL_TEMPLATE.format(year=year)
        try:
            r = requests.head(url, timeout=30, allow_redirects=True,
                              headers={'User-Agent': USER_AGENT})
            if r.status_code == 200:
                size_gb = int(r.headers.get('content-length', 0)) / 1e9
                logging.info(f"Latest Annual NLCD release found: {year} ({size_gb:.2f} GB)")
                return year
            logging.debug(f"  {year}: HTTP {r.status_code}")
        except requests.RequestException as e:
            logging.debug(f"  {year}: {e}")
    logging.error("Could not find any Annual NLCD release on mrlc.gov")
    sys.exit(4)


def download_zip(url, dest_path):
    """Stream the release zip to disk with a .part rename on success."""
    logging.info(f"Downloading {url}")
    start = time.time()
    tmp_path = dest_path + '.part'
    try:
        with requests.get(url, stream=True, timeout=120,
                          headers={'User-Agent': USER_AGENT}) as r:
            r.raise_for_status()
            total = int(r.headers.get('content-length', 0))
            done = 0
            last_report = start
            with open(tmp_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        done += len(chunk)
                        now = time.time()
                        if now - last_report > 30 and total:
                            logging.info(f"  {done / 1e9:.2f} / {total / 1e9:.2f} GB")
                            last_report = now
        os.replace(tmp_path, dest_path)
        logging.info(f"  Downloaded {done / 1e9:.2f} GB in {time.time() - start:.0f} s")
        return True
    except Exception as e:
        logging.warning(f"  Download failed: {e}")
        if os.path.isfile(tmp_path):
            os.remove(tmp_path)
        return False


def extract_tif(zip_path, dest_dir):
    """Extract the single land cover .tif from the release zip."""
    with zipfile.ZipFile(zip_path) as zf:
        tifs = [n for n in zf.namelist() if n.lower().endswith('.tif')]
        if len(tifs) != 1:
            logging.error(f"Expected exactly one .tif in the zip, found: {tifs}")
            sys.exit(5)
        name = tifs[0]
        logging.info(f"Extracting {name}")
        out_path = os.path.join(dest_dir, os.path.basename(name))
        with zf.open(name) as src, open(out_path, 'wb') as dst:
            shutil.copyfileobj(src, dst, length=16 * 1024 * 1024)
    return out_path


def raster_info(path):
    """Return (is_tiled, crs_wkt_first_line, size_str) from gdalinfo."""
    result = subprocess.run(['gdalinfo', path], capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        logging.error(f"gdalinfo failed: {result.stderr.strip()}")
        sys.exit(5)
    is_tiled = False
    size_str = ''
    crs_line = ''
    for line in result.stdout.splitlines():
        s = line.strip()
        if s.startswith('Size is'):
            size_str = s
        elif 'Block=' in s:
            # e.g. "Band 1 Block=512x512 Type=Byte" vs "Block=160000x1"
            block = s.split('Block=')[1].split()[0]
            bx, by = block.split('x')
            is_tiled = int(by) > 1
        elif s.startswith('PROJCRS[') or s.startswith('PROJCS[') or s.startswith('ID["EPSG"'):
            crs_line = crs_line or s
    return is_tiled, crs_line, size_str


def write_metadata(year, source_url, size_str, crs_line, retiled):
    meta = {
        'built_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'product': 'Annual NLCD Land Cover, Collection 1.1, CONUS',
        'year': year,
        'source_url': source_url,
        'raster_size': size_str,
        'crs': crs_line,
        'retiled': retiled,
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
    parser = argparse.ArgumentParser(description='Build the WiSAR NLCD land cover cache')
    parser.add_argument('--year', type=int, default=None,
                        help='Annual NLCD year to install (default: newest MRLC serves)')
    parser.add_argument('--keep-zip', action='store_true',
                        help='Keep the downloaded zip after the build')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    setup_logging(args.verbose)
    t0 = time.time()
    logging.info("=" * 70)
    logging.info("WiSAR NLCD Cache Build")
    logging.info("=" * 70)

    os.makedirs(WORK_DIR, exist_ok=True)
    check_disk_space(MIN_FREE_DISK_GB)
    check_gdal_available()

    year = args.year or find_latest_year()
    url = MRLC_URL_TEMPLATE.format(year=year)
    zip_path = os.path.join(WORK_DIR, f'Annual_NLCD_LndCov_{year}_CU_C1V1.zip')

    # --- Download ---
    if os.path.isfile(zip_path):
        logging.info(f"Using existing zip {zip_path}")
    else:
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            if download_zip(url, zip_path):
                break
            if attempt < DOWNLOAD_ATTEMPTS:
                logging.warning(f"Attempt {attempt}/{DOWNLOAD_ATTEMPTS} failed, "
                                f"retrying in {DOWNLOAD_RETRY_DELAY_S} s")
                time.sleep(DOWNLOAD_RETRY_DELAY_S)
        else:
            logging.error("Download failed. The existing cache (if any) stays live.")
            sys.exit(4)

    # --- Extract ---
    tif_path = extract_tif(zip_path, WORK_DIR)
    is_tiled, crs_line, size_str = raster_info(tif_path)
    logging.info(f"Raster: {size_str}; tiled={is_tiled}; {crs_line[:80]}")

    # --- Ensure a tiled layout so bbox window reads touch only nearby blocks ---
    if os.path.isfile(CACHE_TIF_TMP):
        os.remove(CACHE_TIF_TMP)
    retiled = False
    if is_tiled:
        os.replace(tif_path, CACHE_TIF_TMP)
    else:
        logging.info("Source is striped; rewriting as 512x512 tiles (this takes a few minutes)")
        cmd = ['gdal_translate', '-of', 'GTiff', '-co', 'TILED=YES',
               '-co', 'BLOCKXSIZE=512', '-co', 'BLOCKYSIZE=512',
               '-co', 'COMPRESS=LZW', '-co', 'BIGTIFF=IF_SAFER',
               tif_path, CACHE_TIF_TMP]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logging.error(f"gdal_translate failed: {result.stderr.strip()}")
            sys.exit(5)
        os.remove(tif_path)
        retiled = True

    # --- Promote atomically and write metadata ---
    os.replace(CACHE_TIF_TMP, CACHE_TIF)
    logging.info(f"Installed {CACHE_TIF} ({os.path.getsize(CACHE_TIF) / 1e9:.2f} GB)")
    write_metadata(year, url, size_str, crs_line, retiled)

    if not args.keep_zip and os.path.isfile(zip_path):
        os.remove(zip_path)

    logging.info(f"Done in {(time.time() - t0) / 60:.1f} min")


if __name__ == '__main__':
    main()
