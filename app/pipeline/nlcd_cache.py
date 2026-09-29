# ===============================================================================
# Module:       pipeline/nlcd_cache.py
# Purpose:      Local NLCD land cover snapshot: the pipeline's only source of
#               land cover since v1.17. Reads a bbox window out of a single
#               CONUS GeoTIFF (Annual NLCD, MRLC) that tools/build_nlcd_cache.py
#               downloads once and refreshes yearly.
#
#               Before v1.17 land cover came from the MRLC WMS on every
#               analysis. That request began timing out at 120 s in August
#               2026 and, when it did, the analysis silently ran with uniform
#               impedance. A local file cannot time out.
# Author:       Jamie F. Weleber
# Created:      September 2026 (v1.17)
# ===============================================================================

import os                       # File existence checks, path joins
import json                     # Read cache metadata sidecar
import numpy as np              # Nodata fraction check
import rasterio                 # Windowed raster read/write
from rasterio.windows import Window
from rasterio.warp import transform_bounds


# ===============================================================================
# STEP 1: Cache location constants
# ===============================================================================

# Lives next to (not inside) the deployed app directory so the rsync deploy
# cannot touch it. Must match the path in tools/build_nlcd_cache.py, which is
# kept standalone for cron and does not import this module.
CACHE_DIR = '/var/www/sar.weleber.net/cache/nlcd'
CACHE_TIF = os.path.join(CACHE_DIR, 'nlcd_landcover.tif')
CACHE_METADATA = os.path.join(CACHE_DIR, 'nlcd_cache_metadata.json')

# Annual NLCD land cover nodata value (outside CONUS, and open ocean).
NLCD_NODATA = 250

# Cells of margin read around the requested bbox so nearest-neighbour
# resampling onto the DEM grid never samples past the window edge.
WINDOW_PAD_CELLS = 4


# ===============================================================================
# STEP 2: Availability and metadata helpers
# ===============================================================================

def cache_is_available():
    """True if the GeoTIFF and its metadata sidecar are both on disk."""
    return os.path.isfile(CACHE_TIF) and os.path.isfile(CACHE_METADATA)


def read_cache_metadata():
    """Parsed metadata sidecar, or {} if missing or malformed."""
    if not os.path.isfile(CACHE_METADATA):
        return {}
    try:
        with open(CACHE_METADATA, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARNING: NLCD cache metadata unreadable: {e}")
        return {}


# ===============================================================================
# STEP 3: Windowed read
# ===============================================================================

def load_nlcd_from_cache(bbox, output_path):
    """Clip the CONUS land cover raster to a bbox and write it as a GeoTIFF.

    The clip is written in the raster's native CRS (Albers, EPSG:5070) at
    native 30 m resolution. build_cost_surface() already reprojects whatever
    NLCD raster it is handed onto the DEM grid with nearest-neighbour
    resampling, so nothing downstream needs to know the source changed.
    The old WMS path resampled server-side to the DEM's pixel count; the
    local path hands the reprojection a full-resolution source instead,
    which is the same or better.

    Args:
        bbox: (west, south, east, north) in decimal degrees (EPSG:4326)
        output_path: Where to write the clipped GeoTIFF

    Returns:
        str: output_path if the window held any land cover data.
        None: if the bbox lies entirely outside the raster (Alaska, Hawaii,
              offshore). Callers treat None as "uniform impedance", exactly
              as they did when the WMS request failed.

    Raises:
        FileNotFoundError: if the cache is missing. Callers should check
            cache_is_available() first.
    """
    if not cache_is_available():
        raise FileNotFoundError(f"NLCD cache not found at {CACHE_TIF}")

    west, south, east, north = bbox
    with rasterio.open(CACHE_TIF) as src:
        # Project the bbox into the raster CRS. densify_pts keeps the
        # projected envelope honest for a bbox that curves in Albers.
        px_w, px_s, px_e, px_n = transform_bounds(
            'EPSG:4326', src.crs, west, south, east, north, densify_pts=21)

        # Corner pixels of the projected envelope, padded and clamped to
        # the raster. src.index() returns (row, col); row grows southward.
        r_top, c_left = src.index(px_w, px_n)
        r_bot, c_right = src.index(px_e, px_s)
        r0 = max(min(r_top, r_bot) - WINDOW_PAD_CELLS, 0)
        r1 = min(max(r_top, r_bot) + WINDOW_PAD_CELLS + 1, src.height)
        c0 = max(min(c_left, c_right) - WINDOW_PAD_CELLS, 0)
        c1 = min(max(c_left, c_right) + WINDOW_PAD_CELLS + 1, src.width)
        if r1 <= r0 or c1 <= c0:
            print("  NLCD cache: bbox is outside the CONUS raster extent.")
            return None

        window = Window(c0, r0, c1 - c0, r1 - r0)
        data = src.read(1, window=window)
        nodata = src.nodata if src.nodata is not None else NLCD_NODATA
        valid_fraction = float(np.mean(data != nodata)) if data.size else 0.0
        if valid_fraction == 0.0:
            print("  NLCD cache: window contains no land cover (all nodata).")
            return None

        profile = src.profile.copy()
        profile.update(
            driver='GTiff',
            height=data.shape[0],
            width=data.shape[1],
            transform=src.window_transform(window),
            count=1,
            nodata=nodata,
            compress='lzw',
            tiled=False,
        )
        # A clip is small; drop tiling/blocksize keys inherited from the
        # CONUS profile so GDAL does not complain about block sizes larger
        # than the image.
        for key in ('blockxsize', 'blockysize'):
            profile.pop(key, None)

    with rasterio.open(output_path, 'w', **profile) as dst:
        dst.write(data, 1)

    print(f"  NLCD cache: {data.shape[1]}x{data.shape[0]} native cells, "
          f"{valid_fraction * 100:.0f}% with data")
    return output_path
