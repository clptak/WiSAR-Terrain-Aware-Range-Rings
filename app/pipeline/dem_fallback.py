# ===============================================================================
# Module:       pipeline/dem_fallback.py
# Purpose:      Failure-only elevation source. When the USGS 3DEP ImageServer
#               does not answer, download_dem() reads the same USGS elevation
#               data from the staged one-degree GeoTIFF tiles on the USGS S3
#               bucket instead, and warps it onto the grid the ImageServer
#               would have returned so nothing downstream notices.
#
#               The tiles are LZW, internally tiled GeoTIFFs with overviews,
#               so GDAL's /vsicurl/ range requests fetch only the blocks under
#               the bbox — about a MB, not the whole tile. Nothing is stored
#               on disk and there is no cron job: elevation does not change,
#               and a national copy would be 103 GB.
#
#               3DEP stays the primary. This module is never touched on a
#               normal analysis.
# Author:       Jamie F. Weleber
# Created:      September 2026
# ===============================================================================

import math                     # Floor for tile naming
import numpy as np              # Destination array for the mosaic
import requests                 # HEAD requests to tell "no tile" from "S3 down"
import rasterio                 # /vsicurl/ windowed reads, GeoTIFF write
from rasterio.transform import from_bounds
from rasterio.warp import reproject, Resampling


# ===============================================================================
# STEP 1: Source constants
# ===============================================================================

# Staged products bucket. {res} is '13' (1/3 arc-second, ~10 m) or '1'
# (1 arc-second, ~30 m); {tile} is the one-degree tile name, e.g. n35w112.
TILE_URL = ('https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/'
            '{res}/TIFF/current/{tile}/USGS_{res}_{tile}.tif')

# Tried in this order per tile. 1 arc-second comes first because it is what
# the ImageServer's answer actually looks like at the 30 m+ cells this
# pipeline requests: measured September 2026 over six areas, a DEM warped
# from the 1 arc-second tiles sat within 0.1–1.5 m RMS of the live one in
# all but the Alaska Range (3.6 m), against 1–4 m RMS from the 1/3
# arc-second tiles, and slope differed by about a third as much. It is also
# a tenth of the bytes. 1/3 arc-second is kept for any tile the 1 arc-second
# set lacks.
RESOLUTIONS = ('1', '13')

# Nodata value of the staged tiles, reused for cells no tile covers (open
# ocean). compute_slope() and compute_cost_distance() turn anything below
# -1000 into NaN, so it needs no special handling downstream.
DEM_NODATA = -999999.0

# Seconds allowed for each HEAD request and for each GDAL range request.
HTTP_TIMEOUT = 30


# ===============================================================================
# STEP 2: Grid and tile helpers
# ===============================================================================

def imageserver_extent(bbox, width_px, height_px):
    """The extent the 3DEP ImageServer actually returns for a bbox and size.

    exportImage keeps the requested pixel count but makes the pixels square
    in degrees, growing the bbox about its centre along whichever axis is
    short. download_dem() sizes the request in metres, so in practice the
    extent grows north-south. Reproducing that here gives the fallback DEM
    the same transform the live one has.

    Returns:
        (west, south, east, north) of the width_px x height_px output grid
    """
    west, south, east, north = bbox
    cell = max((east - west) / width_px, (north - south) / height_px)
    center_lng = (west + east) / 2
    center_lat = (south + north) / 2
    half_w = cell * width_px / 2
    half_h = cell * height_px / 2
    return (center_lng - half_w, center_lat - half_h,
            center_lng + half_w, center_lat + half_h)


def tile_names(extent):
    """Names of the one-degree tiles an extent touches.

    Tiles are named by their north-west corner: n35w112 spans 34–35 N and
    112–111 W.
    """
    west, south, east, north = extent
    names = []
    for lat in range(math.floor(south), math.ceil(north)):
        for lng in range(math.floor(west), math.ceil(east)):
            top = lat + 1
            ns = f"n{top:02d}" if top > 0 else f"s{-top:02d}"
            ew = f"w{-lng:03d}" if lng < 0 else f"e{lng:03d}"
            names.append(ns + ew)
    return names


def _tile_exists(url, user_agent):
    """True if the tile is on the bucket, False on 404, raises otherwise.

    The distinction matters: a 404 is an ocean tile or a gap in 1/3
    arc-second coverage and is skipped, but a timeout or a 5xx must fail
    the whole read rather than leave a silent hole in the DEM.
    """
    r = requests.head(url, timeout=HTTP_TIMEOUT, headers={'User-Agent': user_agent})
    if r.status_code == 404:
        return False
    r.raise_for_status()
    return True


def _overview_level(src, dst_cell_deg):
    """Coarsest overview whose cells are still no larger than the output cell.

    The 1000 px cap means a large analysis asks for 100 m+ cells, and
    reading four tiles at full resolution for that is wasted transfer. The
    tiles carry overviews, so read the one closest to the output resolution
    and let the bilinear warp do the rest. None = full resolution.
    """
    level = None
    for i, factor in enumerate(src.overviews(1)):
        if src.res[0] * factor <= dst_cell_deg:
            level = i
    return level


# ===============================================================================
# STEP 3: Windowed read, mosaic and warp
# ===============================================================================

def load_dem_from_tiles(bbox, width_px, height_px, output_path, user_agent):
    """Build the DEM for a bbox from the staged USGS tiles.

    Args:
        bbox: (west, south, east, north) in decimal degrees (EPSG:4326)
        width_px, height_px: Output size, as download_dem() requested it
            from the ImageServer
        output_path: Where to write the GeoTIFF
        user_agent: User-Agent for the HEAD and range requests

    Returns:
        (output_path, resolutions): resolutions is the set of tile
        resolutions used, a subset of {'13', '1'}.

    Raises:
        RuntimeError: if no tile covers the extent, or none of the covered
            cells hold data.
        requests.RequestException, rasterio.errors.RasterioIOError: if the
            bucket is unreachable or a tile that exists cannot be read.
    """
    extent = imageserver_extent(bbox, width_px, height_px)
    dst_transform = from_bounds(*extent, width_px, height_px)
    dst_cell_deg = dst_transform.a
    dst = np.full((height_px, width_px), DEM_NODATA, dtype=np.float32)

    gdal_env = {
        # One GET for the header instead of listing the bucket "directory".
        'GDAL_DISABLE_READDIR_ON_OPEN': 'EMPTY_DIR',
        'CPL_VSIL_CURL_ALLOWED_EXTENSIONS': '.tif',
        'GDAL_HTTP_TIMEOUT': HTTP_TIMEOUT,
        'GDAL_HTTP_CONNECTTIMEOUT': 10,
        'GDAL_HTTP_MAX_RETRY': 2,
        'GDAL_HTTP_RETRY_DELAY': 1,
        'GDAL_HTTP_USERAGENT': user_agent,
        # /vsicurl/ remembers failures for the life of the process. In a
        # long-lived gunicorn worker one S3 hiccup would otherwise poison
        # every later fallback until the next restart.
        'CPL_VSIL_CURL_NON_CACHED': '/vsicurl/https://prd-tnm.s3.amazonaws.com',
    }

    used = set()
    with rasterio.Env(**gdal_env):
        for tile in tile_names(extent):
            for res in RESOLUTIONS:
                url = TILE_URL.format(res=res, tile=tile)
                if not _tile_exists(url, user_agent):
                    continue
                vsi_path = '/vsicurl/' + url
                with rasterio.open(vsi_path) as src:
                    level = _overview_level(src, dst_cell_deg)
                with rasterio.open(vsi_path, overview_level=level) as src:
                    # init_dest_nodata=False: each tile only fills the cells
                    # it covers, so tiles accumulate into one mosaic. The
                    # tiles overlap their neighbours by a few cells, which
                    # keeps the bilinear kernel fed across the seam.
                    reproject(source=rasterio.band(src, 1), destination=dst,
                              src_nodata=src.nodata,
                              dst_transform=dst_transform, dst_crs='EPSG:4326',
                              dst_nodata=DEM_NODATA, init_dest_nodata=False,
                              resampling=Resampling.bilinear)
                print(f"  DEM fallback: {tile} from 1{'/3' if res == '13' else ''} "
                      f"arc-second tile"
                      f"{'' if level is None else f', overview {level}'}")
                used.add(res)
                break
            else:
                print(f"  DEM fallback: no staged tile for {tile} (ocean or uncovered)")

    if not used:
        raise RuntimeError("no staged USGS elevation tile covers this area")
    valid_fraction = float(np.mean(dst != DEM_NODATA))
    if valid_fraction == 0.0:
        raise RuntimeError("staged USGS elevation tiles hold no data for this area")

    with rasterio.open(output_path, 'w', driver='GTiff', dtype='float32', count=1,
                       width=width_px, height=height_px, crs='EPSG:4326',
                       transform=dst_transform, nodata=DEM_NODATA) as out:
        out.write(dst, 1)

    print(f"  DEM fallback: {width_px}x{height_px}, {valid_fraction * 100:.0f}% with data")
    return output_path, used
