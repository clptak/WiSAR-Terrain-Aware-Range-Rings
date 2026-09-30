# ===============================================================================
# Module:       pipeline/downloads.py
# Purpose:      Data acquisition for the WiSAR analysis pipeline.
#               Downloads elevation (USGS 3DEP) live — the only remaining
#               network request, with the staged USGS tiles as a failure-only
#               fallback — and reads land cover (NLCD), hydrology
#               (NHDPlus HR) and trail/road/power line networks (OSM) from
#               local snapshots under /var/www/sar.weleber.net/cache/.
# Author:       Jamie F. Weleber
# Created:      March 2026
# ===============================================================================

import numpy as np              # Array math for raster operations
import rasterio                 # Read/write geospatial rasters (GeoTIFF)
from rasterio.warp import reproject, Resampling  # Reproject rasters between CRS
import requests                 # HTTP client for downloading data from web APIs
import os                       # File path manipulation
import math                     # Trigonometric functions for coordinate math
import geopandas as gpd         # GeoDataFrames: pandas with geometry columns

from pipeline.shared import WORK_DIR   # Shared temp directory for intermediate files


# ===============================================================================
# MODULE CONSTANTS
# ===============================================================================

# User-Agent identifier sent on every outbound HTTP request (USGS 3DEP,
# and the cache builders in tools/). None of these endpoints require
# identification today, but the URL in the UA gives the service operator a
# way to contact us if our traffic ever causes issues. It was originally mandatory because the
# public Overpass API rejected the default 'python-requests' UA with HTTP
# 406; the live Overpass path was retired in v1.16. Bump the version
# string when cutting a new WiSAR release.
WISAR_USER_AGENT = 'WiSAR-DST/1.17 (+https://sar.weleber.net)'


# ===============================================================================
# STEP 1: Download elevation data (DEM)
# ===============================================================================

def download_dem(bbox, output_path=None):
    """Download elevation data from USGS 3DEP (1/3 arc-second, ~10m native).

    The DEM (Digital Elevation Model) provides elevation values at each cell,
    used for two purposes in this pipeline:
      1. Computing slope for Tobler's Hiking Function in cost-distance
      2. Calculating 3D surface distance (actual ground distance, not just
         horizontal distance) between adjacent cells

    We request it at 30m resolution to match the NLCD land cover grid,
    ensuring both rasters align cell-for-cell without resampling artifacts.

    The 3DEP ImageServer is the primary source. If it fails for any reason
    (timeout, HTTP error, a body that is not a GeoTIFF), USGS elevation is
    read from the staged tiles on the USGS S3 bucket instead and warped
    onto the identical grid — see pipeline/dem_fallback.py. The two agree
    to about a metre, which moves TARR ring areas by 2–3%, so the run
    carries an 'info' note. Only when both fail does the analysis fail.

    Args:
        bbox: (west, south, east, north) in decimal degrees
        output_path: Optional path to save the GeoTIFF
    Returns:
        (path, warnings): the DEM GeoTIFF path, plus a list of user-facing
        warning dicts (one 'info' note when the backup source was used).
    Raises:
        RuntimeError: if both the ImageServer and the staged tiles fail.
    """
    if output_path is None:
        output_path = os.path.join(WORK_DIR, 'dem.tif')
    west, south, east, north = bbox
    # Convert bounding box from degrees to meters to determine pixel count.
    # 111320 m/deg is the approximate meters-per-degree at the equator;
    # the cosine correction adjusts for latitude (longitude degrees shrink poleward).
    center_lat = (south + north) / 2
    m_per_deg_lng = 111320 * math.cos(math.radians(center_lat))
    m_per_deg_lat = 110540
    width_m = (east - west) * m_per_deg_lng
    height_m = (north - south) * m_per_deg_lat
    pixel_size = 30  # Target resolution in meters — matches NLCD native 30m
    width_px = max(int(width_m / pixel_size), 1)
    height_px = max(int(height_m / pixel_size), 1)
    # Cap at 1000px to prevent timeouts on very large requests
    max_px = 1000
    if width_px > max_px or height_px > max_px:
        scale = max_px / max(width_px, height_px)
        width_px = int(width_px * scale)
        height_px = int(height_px * scale)
    # USGS 3DEP ImageServer — a .gov endpoint, important because some SAR
    # agencies (e.g., Sheriff's offices) have firewalls that block non-.gov sites
    url = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
    params = {
        'bbox': f'{west},{south},{east},{north}',
        'bboxSR': '4326',                          # Input coordinates are WGS84
        'size': f'{width_px},{height_px}',
        'imageSR': '4326',                          # Output also in WGS84
        'format': 'tiff',                           # GeoTIFF with embedded georeferencing
        'pixelType': 'F32',                         # 32-bit float for continuous elevation
        'noDataInterpretation': 'esriNoDataMatchAny',
        'interpolation': 'RSP_BilinearInterpolation',  # Bilinear for continuous data (not nearest!)
        'f': 'image'                                # Return raw image bytes, not JSON metadata
    }
    print(f"  Downloading DEM: {width_px}x{height_px} pixels...")
    try:
        # 60 s, not the 120 s this used until the fallback existed: the
        # slowest request on record took 45 s, and a hung ImageServer should
        # hand off to the staged tiles within a minute.
        response = requests.get(url, params=params, timeout=60,
                                headers={'User-Agent': WISAR_USER_AGENT})
        response.raise_for_status()
        with open(output_path, 'wb') as f:
            f.write(response.content)
        # Opening it is the check that the body is a GeoTIFF: the ImageServer
        # can answer HTTP 200 with a JSON error.
        with rasterio.open(output_path) as src:
            print(f"  DEM downloaded: {src.width}x{src.height}, CRS: {src.crs}")
        return output_path, []
    except Exception as primary_error:
        print(f"  WARNING: 3DEP ImageServer failed: {primary_error}. "
              f"Reading the staged USGS tiles instead...")

    from pipeline import dem_fallback
    try:
        dem_fallback.load_dem_from_tiles(bbox, width_px, height_px, output_path,
                                         WISAR_USER_AGENT)
    except Exception as fallback_error:
        print(f"  ERROR: staged USGS tile read failed: {fallback_error}")
        raise RuntimeError(
            'Elevation data unavailable — the USGS 3DEP elevation service and '
            'the backup USGS elevation tiles both failed. Try again in a few '
            'minutes.') from fallback_error
    return output_path, [{
        'severity': 'info',
        'source': 'dem',
        'message': ('Elevation came from the backup source — the USGS 3DEP '
                    'elevation service did not respond, so USGS elevation was '
                    'read from the staged USGS tiles instead. Boundaries may '
                    'differ slightly (a few percent in area) from a normal run.'),
    }]


# ===============================================================================
# STEP 2: Load land cover data (NLCD)
# ===============================================================================

def download_nlcd(bbox, output_path=None):
    """Clip land cover for the bbox out of the local Annual NLCD snapshot.

    NLCD classifies every 30m cell in the continental US into one of ~20 land
    cover types (forest, developed, water, etc.). We use these classes to assign
    impedance values that model how difficult each terrain type is to traverse.

    History: v1.00–v1.16 requested this from the MRLC WMS on every analysis.
    From August 2026 that request began timing out at 120 s, and when it did
    the analysis silently continued with uniform impedance. As of v1.17 the
    source is a CONUS GeoTIFF installed by tools/build_nlcd_cache.py (see
    pipeline/nlcd_cache.py); the clip is a windowed read that cannot time
    out. The function keeps its "download_" name for its callers.

    Args:
        bbox: (west, south, east, north) in decimal degrees
        output_path: Optional path for the clipped GeoTIFF
    Returns:
        (path_or_None, warnings): the GeoTIFF path in the snapshot's native
        CRS (build_cost_surface reprojects it onto the DEM grid), or None
        for uniform impedance; plus a list of user-facing warning dicts.
    """
    from pipeline import nlcd_cache

    if output_path is None:
        output_path = os.path.join(WORK_DIR, 'nlcd.tif')

    print("  Loading NLCD land cover from the local snapshot...")
    if not nlcd_cache.cache_is_available():
        print("  WARNING: NLCD snapshot not present. Using uniform impedance.")
        return None, [{
            'severity': 'warning',
            'source': 'nlcd',
            'message': ('Land cover unavailable — the NLCD snapshot is not installed '
                        'on this server. Analysis proceeded with uniform terrain '
                        'friction; forest, brush and wetland are not slowing travel '
                        'in these results.'),
        }]
    try:
        path = nlcd_cache.load_nlcd_from_cache(bbox, output_path)
    except Exception as e:
        print(f"  WARNING: NLCD snapshot read failed: {e}. Using uniform impedance.")
        return None, [{
            'severity': 'warning',
            'source': 'nlcd',
            'message': ('Land cover unavailable — reading the NLCD snapshot failed. '
                        'Analysis proceeded with uniform terrain friction; forest, '
                        'brush and wetland are not slowing travel in these results.'),
        }]
    if path is None:
        # Outside the CONUS raster (Alaska, Hawaii, offshore). Same outcome
        # the old L48 WMS layer gave there, but now the coordinator is told.
        return None, [{
            'severity': 'warning',
            'source': 'nlcd',
            'message': ('Land cover unavailable — the NLCD snapshot covers the '
                        'continental US only. Analysis proceeded with uniform '
                        'terrain friction.'),
        }]
    return path, []


# ===============================================================================
# STEP 3: Load trail/road networks and power line corridors (OpenStreetMap)
# ===============================================================================

# Age beyond which the weekly OSM snapshot is reported as stale. The cache
# is rebuilt every Sunday by cron, so anything past two weeks means at least
# two consecutive builds failed. With no live Overpass path left, a broken
# cron job would otherwise be invisible to the coordinator.
OSM_CACHE_STALE_DAYS = 14


def _empty_osm_features():
    """Empty GeoDataFrames in the schema build_cost_surface() expects."""
    return {
        'trails': gpd.GeoDataFrame(columns=['geometry','type','name'], crs='EPSG:4326'),
        'roads': gpd.GeoDataFrame(columns=['geometry','type','name'], crs='EPSG:4326'),
        'waterways': gpd.GeoDataFrame(columns=['geometry','type','name','width'], crs='EPSG:4326'),
        'powerlines': gpd.GeoDataFrame(columns=['geometry','type','name'], crs='EPSG:4326'),
    }


def download_osm_features(bbox):
    """Load trail, road, waterway, and power line features from the local OSM cache.

    OSM is the primary source for trail and road networks because it has the
    most complete open dataset for backcountry trails — USGS topographic
    maps don't include many user-maintained trails that hikers actually use.

    Power lines (power=line and power=minor_line) are included because
    high-voltage transmission line corridors have maintained cleared
    rights-of-way that function as travel aids. Lost persons may follow
    these corridors as navigational features — they are both physically
    passable (cleared vegetation) and psychologically attractive (human-made
    linear features). IGT4SAR (Ferguson 2012) modeled power line ROWs as
    reduced-impedance travel corridors.

    History: v1.00–v1.15 queried the public Overpass API live, with the
    local cache as a failure-only fallback (v1.11+). The public mirrors
    failed often enough that the retry chain became the single largest
    time cost in the pipeline, so as of v1.16 the weekly Geofabrik snapshot
    (see pipeline/osm_cache.py and tools/build_osm_cache.py) is the only
    source. Data up to a week old is operationally equivalent for trail
    and road networks. The function keeps its "download_" name because
    callers and the package re-exports reference it.

    Args:
        bbox: (west, south, east, north) in decimal degrees
    Returns:
        Dict with 'trails', 'roads', 'waterways', 'powerlines' GeoDataFrames,
        plus an internal '_warnings' key listing any data-source issues for
        surfacing to the user. The '_warnings' key is stripped before the
        dict reaches build_cost_surface(), which doesn't expect it.
    """
    from pipeline import osm_cache

    print("  Loading OSM trails, roads, waterways, and power lines from the weekly snapshot...")

    if not osm_cache.cache_is_available():
        print("  WARNING: OSM cache not present. Analysis will proceed without trail data.")
        result = _empty_osm_features()
        result['_warnings'] = [{
            'severity': 'warning',
            'source': 'osm',
            'message': ('OSM trail/road data unavailable — the weekly OSM snapshot '
                        'is not installed on this server. Analysis proceeded using '
                        'land cover only; trail corridors will not appear in results.'),
        }]
        return result

    if not osm_cache.cache_covers_bbox(bbox):
        meta = osm_cache.read_cache_metadata()
        state_list = meta.get('states', [])
        # With the whole country cached the list is 51 slugs long;
        # a count reads better in the warning banner than the roll call.
        if len(state_list) > 12:
            states = f'{len(state_list)} US states'
        else:
            states = ', '.join(state_list) or 'unknown'
        print(f"  WARNING: Analysis bbox outside cache coverage ({states}). "
              f"Proceeding without trail data.")
        result = _empty_osm_features()
        result['_warnings'] = [{
            'severity': 'warning',
            'source': 'osm',
            'message': (f'OSM trail/road data unavailable — the weekly OSM snapshot '
                        f'does not cover this area (snapshot covers {states}). '
                        f'Analysis proceeded using land cover only; trail corridors '
                        f'will not appear in results.'),
        }]
        return result

    try:
        cached = osm_cache.load_osm_from_cache(bbox)
    except Exception as e:
        # Cache read failed despite the availability check passing —
        # probably a filesystem/permissions issue. Degrade to empty
        # results with a clear warning; don't crash the analysis.
        print(f"  WARNING: OSM cache read failed: {e}")
        result = _empty_osm_features()
        result['_warnings'] = [{
            'severity': 'warning',
            'source': 'osm',
            'message': ('OSM trail/road data unavailable — reading the weekly OSM '
                        'snapshot failed. Analysis proceeded using land cover '
                        'only; trail corridors will not appear in results.'),
        }]
        return result

    age = osm_cache.cache_age_days()
    meta = osm_cache.read_cache_metadata()
    built_at = meta.get('built_at', 'unknown date')
    # Strip the time portion for a cleaner user-facing date
    built_date = built_at.split('T')[0] if 'T' in built_at else built_at
    age_str = f"{age:.1f} days old" if age is not None else "age unknown"
    print(f"  Snapshot built {built_date} ({age_str})")

    cached['_warnings'] = []
    if age is None or age > OSM_CACHE_STALE_DAYS:
        # A fresh snapshot needs no note. A stale one means the weekly
        # rebuild has been failing and trails added or rerouted since the
        # build date are missing — worth telling the coordinator.
        cached['_warnings'].append({
            'severity': 'warning',
            'source': 'osm',
            'message': (f'OSM trail/road data may be out of date — the weekly OSM '
                        f'snapshot was built {built_date} ({age_str}) and has not '
                        f'been refreshed. Trails mapped since then are missing.'),
        })
    return cached


# ===============================================================================
# STEP 4: Load hydrology features (NHD)
# ===============================================================================

def download_nhd_features(bbox):
    """Load waterbodies, area hydro polygons and flowlines from the local NHD snapshot.

    Water features are treated as barriers in the cost surface because lost
    persons generally cannot cross lakes or major rivers on foot. Flowlines are
    buffered proportionally to their Strahler stream order — a 7th-order river
    gets a much wider buffer than a 1st-order seasonal creek.

    History: v1.00–v1.16 made three sequential requests to the USGS hydro
    MapServer (waterbodies, areas, flowlines) at 60 s each; in September
    2026 those began timing out and cost up to three minutes per analysis,
    and a timed-out layer was silently dropped. As of v1.17 the source is a
    GeoPackage built from the USGS NHDPlus HR basin packages by
    tools/build_hydro_cache.py (see pipeline/nhd_cache.py). Note the
    resolution change: the MapServer's flowline layer was the 1:100k
    NHDPlus V2 network; the snapshot is 1:24k throughout, so it carries
    more headwater streams and a given creek may hold a higher Strahler
    order than before. The function keeps its "download_" name for its
    callers.

    Args:
        bbox: (west, south, east, north) in decimal degrees
    Returns:
        (GeoDataFrame, warnings): water feature polygons with 'type', 'ftype',
        'name' and 'impedance' columns (empty frame if none), plus a list of
        user-facing warning dicts.
    """
    from pipeline import nhd_cache

    print("  Loading NHD hydrography from the local snapshot...")
    empty = gpd.GeoDataFrame(columns=['geometry', 'type', 'ftype', 'name', 'impedance'],
                             geometry='geometry', crs='EPSG:4326')

    if not nhd_cache.cache_is_available():
        print("  WARNING: NHD snapshot not present. Proceeding without water barriers.")
        return empty, [{
            'severity': 'warning',
            'source': 'nhd',
            'message': ('Hydrography unavailable — the NHD snapshot is not installed on '
                        'this server. Lakes, rivers and streams are not acting as '
                        'barriers in these results.'),
        }]

    if not nhd_cache.cache_covers_bbox(bbox):
        meta = nhd_cache.read_cache_metadata()
        print(f"  WARNING: bbox outside NHD snapshot coverage "
              f"({len(meta.get('hu4s', []))} basins). Proceeding without water barriers.")
        return empty, [{
            'severity': 'warning',
            'source': 'nhd',
            'message': ('Hydrography unavailable — the NHD snapshot does not cover this '
                        'area. Lakes, rivers and streams are not acting as barriers in '
                        'these results.'),
        }]

    try:
        gdf = nhd_cache.load_nhd_from_cache(bbox)
    except Exception as e:
        print(f"  WARNING: NHD snapshot read failed: {e}. Proceeding without water barriers.")
        return empty, [{
            'severity': 'warning',
            'source': 'nhd',
            'message': ('Hydrography unavailable — reading the NHD snapshot failed. '
                        'Lakes, rivers and streams are not acting as barriers in '
                        'these results.'),
        }]
    return gdf, []
