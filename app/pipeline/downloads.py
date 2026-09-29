# ===============================================================================
# Module:       pipeline/downloads.py
# Purpose:      Data acquisition for the WiSAR analysis pipeline.
#               Downloads elevation (USGS 3DEP), land cover (NLCD), and
#               hydrology (NHD) for the analysis bounding box, and reads
#               trail/road networks and power line corridors (OpenStreetMap)
#               from the weekly local snapshot.
# Author:       Jamie F. Weleber
# Created:      March 2026
# ===============================================================================

import numpy as np              # Array math for raster operations
import rasterio                 # Read/write geospatial rasters (GeoTIFF)
from rasterio.warp import reproject, Resampling  # Reproject rasters between CRS
import requests                 # HTTP client for downloading data from web APIs
import os                       # File path manipulation
import math                     # Trigonometric functions for coordinate math
from shapely.geometry import shape  # Vector geometry construction
import geopandas as gpd         # GeoDataFrames: pandas with geometry columns

from pipeline.shared import WORK_DIR   # Shared temp directory for intermediate files


# ===============================================================================
# MODULE CONSTANTS
# ===============================================================================

# User-Agent identifier sent on every outbound HTTP request (USGS 3DEP,
# MRLC/NLCD, NHD). None of these endpoints require identification today,
# but the URL in the UA gives the service operator a way to contact us if
# our traffic ever causes issues. It was originally mandatory because the
# public Overpass API rejected the default 'python-requests' UA with HTTP
# 406; the live Overpass path was retired in v1.16. Bump the version
# string when cutting a new WiSAR release.
WISAR_USER_AGENT = 'WiSAR-DST/1.16 (+https://sar.weleber.net)'


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

    Args:
        bbox: (west, south, east, north) in decimal degrees
        output_path: Optional path to save the GeoTIFF
    Returns:
        Path to the downloaded DEM GeoTIFF
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
    response = requests.get(url, params=params, timeout=120,
                            headers={'User-Agent': WISAR_USER_AGENT})
    response.raise_for_status()
    with open(output_path, 'wb') as f:
        f.write(response.content)
    with rasterio.open(output_path) as src:
        print(f"  DEM downloaded: {src.width}x{src.height}, CRS: {src.crs}")
    return output_path


# ===============================================================================
# STEP 2: Download land cover data (NLCD)
# ===============================================================================

def download_nlcd(bbox, output_path=None):
    """Download land cover data from the National Land Cover Database (NLCD 2021).

    NLCD classifies every 30m cell in the continental US into one of ~20 land
    cover types (forest, developed, water, etc.). We use these classes to assign
    impedance values that model how difficult each terrain type is to traverse.

    The data is served via WMS (Web Map Service) from the Multi-Resolution Land
    Characteristics Consortium (MRLC). We use nearest-neighbor resampling because
    land cover is categorical data — interpolating between "forest" and "water"
    would produce meaningless intermediate values.

    Args:
        bbox: (west, south, east, north) in decimal degrees
        output_path: Optional path to save the GeoTIFF
    Returns:
        Path to the downloaded NLCD GeoTIFF, or None if download fails
    """
    if output_path is None:
        output_path = os.path.join(WORK_DIR, 'nlcd.tif')
    west, south, east, north = bbox
    center_lat = (south + north) / 2
    m_per_deg_lng = 111320 * math.cos(math.radians(center_lat))
    m_per_deg_lat = 110540
    width_m = (east - west) * m_per_deg_lng
    height_m = (north - south) * m_per_deg_lat
    pixel_size = 30  # NLCD native resolution
    width_px = max(int(width_m / pixel_size), 1)
    height_px = max(int(height_m / pixel_size), 1)
    max_px = 1000
    if width_px > max_px or height_px > max_px:
        scale = max_px / max(width_px, height_px)
        width_px = int(width_px * scale)
        height_px = int(height_px * scale)
    # MRLC WMS endpoint for NLCD 2021 land cover (CONUS extent)
    url = "https://www.mrlc.gov/geoserver/mrlc_download/NLCD_2021_Land_Cover_L48/ows"
    params = {
        'service': 'WMS', 'version': '1.1.1', 'request': 'GetMap',
        'layers': 'NLCD_2021_Land_Cover_L48',
        'bbox': f'{west},{south},{east},{north}',
        'width': width_px, 'height': height_px,
        'srs': 'EPSG:4326', 'styles': '', 'format': 'image/geotiff',
    }
    print(f"  Downloading NLCD: {width_px}x{height_px} pixels...")
    try:
        response = requests.get(url, params=params, timeout=120,
                                headers={'User-Agent': WISAR_USER_AGENT})
        response.raise_for_status()
        with open(output_path, 'wb') as f:
            f.write(response.content)
        with rasterio.open(output_path) as src:
            print(f"  NLCD downloaded: {src.width}x{src.height}")
        return output_path
    except Exception as e:
        # NLCD download can fail if MRLC servers are down — fall back to
        # uniform impedance so the analysis can still run (slope-only mode)
        print(f"  NLCD download failed: {e}. Using uniform impedance.")
        return None


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
# STEP 4: Download hydrology features (NHD)
# ===============================================================================

def download_nhd_features(bbox):
    """Download waterbodies and hydrology features from the National Hydrography Dataset.

    NHD provides authoritative water feature boundaries from the USGS. We query
    three layers:
      - Layer 12 (Waterbodies): lakes, ponds, reservoirs as polygons
      - Layer 9 (Area features): rivers and streams as polygon areas
      - Layer 4 (Flowlines): stream/river centerlines with Strahler stream order

    Water features are treated as barriers in the cost surface because lost
    persons generally cannot cross lakes or major rivers on foot. Flowlines are
    buffered proportionally to their stream order — a 7th-order river gets a
    much wider buffer than a 1st-order seasonal creek.

    Args:
        bbox: (west, south, east, north) in decimal degrees
    Returns:
        GeoDataFrame of water feature polygons with impedance values
    """
    from pipeline.shared import repair_geometry  # Shared geometry repair utility

    west, south, east, north = bbox
    geom_str = f'{west},{south},{east},{north}'

    water_features = []

    # --- Sub-step A: Waterbodies (lakes, ponds, reservoirs) ---
    url_wb = "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/12/query"
    params_wb = {
        'geometry': geom_str,
        'geometryType': 'esriGeometryEnvelope',
        'inSR': '4326', 'outSR': '4326',
        'spatialRel': 'esriSpatialRelIntersects',
        'outFields': 'GNIS_NAME,FTYPE,FCODE,AREASQKM',
        'f': 'geojson',
        'returnGeometry': 'true',
        'resultRecordCount': 500,
    }
    print("  Downloading NHD waterbodies...")
    try:
        response = requests.get(url_wb, params=params_wb, timeout=60,
                                headers={'User-Agent': WISAR_USER_AGENT})
        response.raise_for_status()
        data = response.json()
        for f in data.get('features', []):
            ftype = f.get('properties', {}).get('FTYPE', 0)
            name = f.get('properties', {}).get('GNIS_NAME', '') or 'unnamed'
            water_features.append({
                'geometry': shape(f['geometry']),
                'type': 'waterbody',
                'ftype': ftype,
                'name': name,
                'impedance': 99  # Near-impassable barrier
            })
    except Exception as e:
        print(f"  Warning: NHD waterbody download failed: {e}")

    # --- Sub-step B: Area hydrology features (river polygons) ---
    url_area = "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/9/query"
    params_area = {
        'geometry': geom_str,
        'geometryType': 'esriGeometryEnvelope',
        'inSR': '4326', 'outSR': '4326',
        'spatialRel': 'esriSpatialRelIntersects',
        'outFields': 'GNIS_NAME,FTYPE,FCODE',
        'f': 'geojson',
        'returnGeometry': 'true',
        'resultRecordCount': 500,
    }
    print("  Downloading NHD area hydro features...")
    try:
        response = requests.get(url_area, params=params_area, timeout=60,
                                headers={'User-Agent': WISAR_USER_AGENT})
        response.raise_for_status()
        data = response.json()
        for f in data.get('features', []):
            ftype = f.get('properties', {}).get('FTYPE', 0)
            name = f.get('properties', {}).get('GNIS_NAME', '') or 'unnamed'
            # FType 460 = Stream/River, 431 = Rapids, 336 = Canal/Ditch, 390 = Lake
            if ftype in (460, 431, 336, 390):
                imp = 99 if ftype in (460, 390) else 80
                water_features.append({
                    'geometry': shape(f['geometry']),
                    'type': 'river_area',
                    'ftype': ftype,
                    'name': name,
                    'impedance': imp
                })
    except Exception as e:
        print(f"  Warning: NHD area download failed: {e}")

    if water_features:
        gdf = gpd.GeoDataFrame(water_features, crs='EPSG:4326')
    else:
        gdf = gpd.GeoDataFrame(columns=['geometry', 'type', 'ftype', 'name', 'impedance'], crs='EPSG:4326')

    # --- Sub-step C: Flowlines (stream centerlines with Strahler stream order) ---
    # Strahler order indicates stream size: 1st = headwater creek, 7th = major river
    url_fl = "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/4/query"
    params_fl = {
        'geometry': geom_str,
        'geometryType': 'esriGeometryEnvelope',
        'inSR': '4326', 'outSR': '4326',
        'spatialRel': 'esriSpatialRelIntersects',
        'outFields': 'GNIS_NAME,FTYPE,FCODE,StreamOrde',
        'f': 'geojson',
        'returnGeometry': 'true',
        'resultRecordCount': 1000,
    }
    flowline_count = 0
    print("  Downloading NHD flowlines (streams/rivers)...")
    try:
        response = requests.get(url_fl, params=params_fl, timeout=60,
                                headers={'User-Agent': WISAR_USER_AGENT})
        response.raise_for_status()
        data = response.json()
        for feat in data.get('features', []):
            stream_order = feat.get('properties', {}).get('StreamOrde', 0) or 0
            fname = feat.get('properties', {}).get('GNIS_NAME', '') or 'unnamed'
            fgeom = shape(feat['geometry'])
            if fgeom.is_empty:
                continue
            # Buffer and impedance scale with stream order
            if stream_order >= 7:
                buf = 0.0004   # ~40m — major river
                imp = 99
            elif stream_order >= 5:
                buf = 0.0001   # ~10m — medium river
                imp = 80
            elif stream_order >= 3:
                buf = 0.00005  # ~5m — moderate creek
                imp = 60
            else:
                buf = 0.00002  # ~2m — small seasonal creek
                imp = 40
            buffered = fgeom.buffer(buf)
            if buffered and not buffered.is_empty:
                water_features.append({
                    'geometry': buffered,
                    'type': 'flowline',
                    'ftype': stream_order,
                    'name': fname,
                    'impedance': imp
                })
                flowline_count += 1
    except Exception as e:
        print(f"  Warning: NHD flowline download failed: {e}")
    print(f"  NHD flowlines: {flowline_count} features added")
    if water_features:
        gdf = gpd.GeoDataFrame(water_features, crs='EPSG:4326')
    else:
        gdf = gpd.GeoDataFrame(columns=['geometry', 'type', 'ftype', 'name', 'impedance'], crs='EPSG:4326')
    wb_count = sum(1 for w in water_features if w['type']=='waterbody')
    ra_count = sum(1 for w in water_features if w['type']=='river_area')
    print(f"  NHD total: {len(water_features)} features ({wb_count} waterbodies, {ra_count} river areas, {flowline_count} flowlines)")
    return gdf
