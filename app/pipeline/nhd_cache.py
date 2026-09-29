# ===============================================================================
# Module:       pipeline/nhd_cache.py
# Purpose:      Local hydrography snapshot: the pipeline's only source of NHD
#               water features since v1.17. Reads waterbodies, area hydro
#               polygons, and flowlines with Strahler stream order out of a
#               GeoPackage that tools/build_hydro_cache.py assembles from
#               the USGS NHDPlus HR basin (HU4) packages.
#
#               Before v1.17 each analysis made three sequential requests to
#               the USGS hydro MapServer at 60 s apiece. Those began timing
#               out in September 2026 and cost up to three minutes per run.
#               The MapServer's flowline layer was also the 1:100k NHDPlus V2
#               network; the local snapshot is 1:24k NHDPlus HR throughout.
# Author:       Jamie F. Weleber
# Created:      September 2026 (v1.17)
# ===============================================================================

import os                       # File existence checks, path joins
import json                     # Read cache metadata sidecar
import geopandas as gpd         # GeoDataFrame construction and bbox reads


# ===============================================================================
# STEP 1: Cache location constants
# ===============================================================================

# Lives next to (not inside) the deployed app directory so the rsync deploy
# cannot touch it. Must match the path in tools/build_hydro_cache.py, which
# is kept standalone for cron and does not import this module.
CACHE_DIR = '/var/www/sar.weleber.net/cache/nhd'
CACHE_GPKG = os.path.join(CACHE_DIR, 'nhd_cache.gpkg')
CACHE_METADATA = os.path.join(CACHE_DIR, 'nhd_cache_metadata.json')

# Layer names inside the GeoPackage, written by the builder.
LAYER_FLOWLINES = 'flowlines'
LAYER_WATERBODIES = 'waterbodies'
LAYER_AREAS = 'areas'

# Output schema, identical to what the retired live MapServer path built,
# so build_cost_surface() and compute_jacobs_masks() need no changes.
OUTPUT_COLUMNS = ['geometry', 'type', 'ftype', 'name', 'impedance']

# NHDArea FTypes the cost surface treats as barriers. 460 Stream/River and
# 390 Lake/Pond polygons are near-impassable; 431 Rapids and 336 Canal/Ditch
# are heavy but crossable. Unchanged from the live path.
AREA_FTYPES_BARRIER = (460, 390)
AREA_FTYPES_HEAVY = (431, 336)


# ===============================================================================
# STEP 2: Availability and metadata helpers
# ===============================================================================

def cache_is_available():
    """True if the GeoPackage and its metadata sidecar are both on disk."""
    return os.path.isfile(CACHE_GPKG) and os.path.isfile(CACHE_METADATA)


def read_cache_metadata():
    """Parsed metadata sidecar, or {} if missing or malformed."""
    if not os.path.isfile(CACHE_METADATA):
        return {}
    try:
        with open(CACHE_METADATA, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARNING: NHD cache metadata unreadable: {e}")
        return {}


def cache_covers_bbox(bbox):
    """True if the bbox intersects any basin (HU4) the snapshot contains.

    NHDPlus HR covers CONUS completely, Hawaii and Puerto Rico, and only
    part of Alaska. A basin envelope is a rectangle, so a request just
    outside a basin's real boundary can pass and read empty layers; that
    degrades gracefully and is indistinguishable from "no water here".
    """
    meta = read_cache_metadata()
    west, south, east, north = bbox
    for hb in (meta.get('hu4_bboxes') or {}).values():
        if not hb or len(hb) != 4:
            continue
        h_west, h_south, h_east, h_north = hb
        if west <= h_east and east >= h_west and south <= h_north and north >= h_south:
            return True
    return False


# ===============================================================================
# STEP 3: Flowline buffering — unchanged from the live path
# ===============================================================================

def _flowline_buffer_and_impedance(stream_order):
    """Buffer width (degrees) and impedance for a flowline by Strahler order.

    Kept identical to the retired MapServer path so cost surfaces are
    comparable across the switch. Orders are now computed on the 1:24k
    network, which counts more headwaters than 1:100k did, so a given creek
    may carry a higher order than before.
    """
    if stream_order >= 7:
        return 0.0004, 99     # ~40 m — major river
    if stream_order >= 5:
        return 0.0001, 80     # ~10 m — medium river
    if stream_order >= 3:
        return 0.00005, 60    # ~5 m — moderate creek
    return 0.00002, 40        # ~2 m — small seasonal creek


# ===============================================================================
# STEP 4: Main read function
# ===============================================================================

def _empty_result():
    return gpd.GeoDataFrame(columns=OUTPUT_COLUMNS, geometry='geometry', crs='EPSG:4326')


def load_nhd_from_cache(bbox):
    """Load NHD water features for a bbox as a barrier GeoDataFrame.

    Returns rows shaped exactly like the retired live path produced:
      type       'waterbody' | 'river_area' | 'flowline'
      ftype      NHD FType for polygons; Strahler order for flowlines
      name       GNIS name or 'unnamed'
      impedance  cost-surface impedance (40–99)
    Flowline geometries are pre-buffered by stream order, as before.

    Args:
        bbox: (west, south, east, north) in decimal degrees

    Returns:
        GeoDataFrame in EPSG:4326 with OUTPUT_COLUMNS. Empty if the bbox
        holds no water features. Any single layer that fails to read is
        skipped with a logged warning rather than aborting the analysis.

    Raises:
        FileNotFoundError: if the cache is missing. Callers should check
            cache_is_available() first.
    """
    if not cache_is_available():
        raise FileNotFoundError(f"NHD cache not found at {CACHE_GPKG}")

    west, south, east, north = bbox
    bbox_tuple = (west, south, east, north)
    rows = []
    counts = {'waterbody': 0, 'river_area': 0, 'flowline': 0}

    # --- Waterbodies: every polygon is a near-impassable barrier ---
    try:
        wb = gpd.read_file(CACHE_GPKG, layer=LAYER_WATERBODIES, bbox=bbox_tuple)
        for _, r in wb.iterrows():
            rows.append({
                'geometry': r.geometry,
                'type': 'waterbody',
                'ftype': int(r.get('ftype') or 0),
                'name': r.get('gnis_name') or 'unnamed',
                'impedance': 99,
            })
            counts['waterbody'] += 1
    except Exception as e:
        print(f"  WARNING: NHD cache waterbodies read failed: {e}")

    # --- Area hydro features: river polygons, rapids, canals, lakes ---
    try:
        ar = gpd.read_file(CACHE_GPKG, layer=LAYER_AREAS, bbox=bbox_tuple)
        for _, r in ar.iterrows():
            ftype = int(r.get('ftype') or 0)
            if ftype in AREA_FTYPES_BARRIER:
                imp = 99
            elif ftype in AREA_FTYPES_HEAVY:
                imp = 80
            else:
                continue
            rows.append({
                'geometry': r.geometry,
                'type': 'river_area',
                'ftype': ftype,
                'name': r.get('gnis_name') or 'unnamed',
                'impedance': imp,
            })
            counts['river_area'] += 1
    except Exception as e:
        print(f"  WARNING: NHD cache areas read failed: {e}")

    # --- Flowlines: buffered by Strahler order ---
    try:
        fl = gpd.read_file(CACHE_GPKG, layer=LAYER_FLOWLINES, bbox=bbox_tuple)
        for _, r in fl.iterrows():
            geom = r.geometry
            if geom is None or geom.is_empty:
                continue
            order_raw = r.get('stream_order')
            try:
                order = int(order_raw) if order_raw is not None and order_raw == order_raw else 0
            except (TypeError, ValueError):
                order = 0
            buf, imp = _flowline_buffer_and_impedance(order)
            buffered = geom.buffer(buf)
            if buffered is None or buffered.is_empty:
                continue
            rows.append({
                'geometry': buffered,
                'type': 'flowline',
                'ftype': order,
                'name': r.get('gnis_name') or 'unnamed',
                'impedance': imp,
            })
            counts['flowline'] += 1
    except Exception as e:
        print(f"  WARNING: NHD cache flowlines read failed: {e}")

    print(f"  NHD cache: {sum(counts.values())} features "
          f"({counts['waterbody']} waterbodies, {counts['river_area']} river areas, "
          f"{counts['flowline']} flowlines)")

    if not rows:
        return _empty_result()
    return gpd.GeoDataFrame(rows, geometry='geometry', crs='EPSG:4326')[OUTPUT_COLUMNS]
