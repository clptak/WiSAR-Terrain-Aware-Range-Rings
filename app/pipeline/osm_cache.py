# ===============================================================================
# Module:       pipeline/osm_cache.py
# Purpose:      Local OSM snapshot: the pipeline's only source of trail, road,
#               waterway and power line features. Reads pre-built state-level
#               extracts from a GeoPackage file that is refreshed weekly by a
#               cron job (see tools/build_osm_cache.py).
#
#               The layers match the dict returned by download_osm_features():
#               trails, roads, waterways, powerlines. From v1.11 to v1.15 this
#               was a failure-only fallback behind the live Overpass API; the
#               live path was retired in v1.16 because its retry chain had
#               become the largest time cost in the pipeline.
# Author:       Jamie F. Weleber
# Created:      April 2026 (v1.11 cache fallback); sole source since v1.16
# ===============================================================================

import os                       # File existence checks, path joins
import json                     # Read cache metadata sidecar
import geopandas as gpd         # GeoDataFrame construction and spatial queries
from shapely.geometry import box # Build bbox polygon for spatial clip
from datetime import datetime, timezone  # Cache age computation


# ===============================================================================
# STEP 1: Cache location constants
# ===============================================================================

# The cache lives next to (not inside) the deployed app directory so the
# rsync deploy cannot touch it. Using an absolute path (not WORK_DIR)
# because the cache MUST persist across pipeline runs. It is regenerable
# from Geofabrik and deliberately not backed up.
CACHE_DIR = '/var/www/sar.weleber.net/cache/osm'
CACHE_GPKG = os.path.join(CACHE_DIR, 'osm_cache.gpkg')
CACHE_METADATA = os.path.join(CACHE_DIR, 'osm_cache_metadata.json')

# Layer names inside the GeoPackage. These intentionally match the dict keys
# returned by download_osm_features().
CACHE_LAYERS = ('trails', 'roads', 'waterways', 'powerlines')


# ===============================================================================
# STEP 2: Cache availability and metadata helpers
# ===============================================================================

def cache_is_available():
    """Check whether a usable cache exists on disk.

    This is a lightweight check used by the downloads module before
    attempting a read. It verifies
    both the GeoPackage and its metadata sidecar exist — missing metadata
    would prevent us from reporting cache age to the user.

    Returns:
        bool: True if both cache files are present and readable.
    """
    return os.path.isfile(CACHE_GPKG) and os.path.isfile(CACHE_METADATA)


def read_cache_metadata():
    """Load the cache metadata sidecar.

    The sidecar records when the cache was built, which source PBF it was
    built from, which states are covered, and feature counts. This is the
    source of truth for "cache age" shown to SAR users in warning messages.

    Returns:
        dict: Parsed metadata, or an empty dict if the file is missing or
              malformed. Callers should check for 'built_at' key presence.
    """
    if not os.path.isfile(CACHE_METADATA):
        return {}
    try:
        with open(CACHE_METADATA, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARNING: Cache metadata unreadable: {e}")
        return {}


def cache_age_days():
    """Return how many days old the current cache is.

    Used by the downloads module to decide whether to warn the coordinator
    that the weekly rebuild has stopped running. Returns None if metadata
    is missing or unparseable — callers should treat None as "unknown age"
    rather than "fresh."

    Returns:
        float or None: Age in days, or None if metadata unavailable.
    """
    meta = read_cache_metadata()
    built_at = meta.get('built_at')
    if not built_at:
        return None
    try:
        # Metadata stores ISO 8601 UTC timestamps, e.g. "2026-04-21T03:15:00Z"
        built_dt = datetime.fromisoformat(built_at.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        return (now - built_dt).total_seconds() / 86400.0
    except (ValueError, TypeError) as e:
        print(f"  WARNING: Cache built_at timestamp unparseable: {e}")
        return None


def cache_covers_bbox(bbox):
    """Check whether the cache's geographic coverage includes the requested bbox.

    The cache only contains the states tools/build_osm_cache.py was told to
    download (all 50 + DC since September 2026; five Western states in
    v1.11). If a SAR mission is run somewhere the cache does not reach, the
    cache can't help — and we must tell the user that rather than silently
    returning empty GeoDataFrames that look identical to "OSM just had no
    data for this area."

    When the metadata carries per-state bboxes (builds from September 2026
    on) the request passes if it intersects any state's bbox. A state bbox
    is still a rectangle, so a request in a corner of a neighbouring state
    that the build skipped could pass and get empty results; that's
    acceptable because "empty from the cache" is indistinguishable from "no
    features in that area" and degrades gracefully. Alaska's bbox spans the
    antimeridian and therefore matches most of the northern hemisphere
    between 51°N and 72°N — same graceful failure.

    Older metadata has only the union envelope; fall back to requiring the
    request to sit fully inside it.

    Args:
        bbox: (west, south, east, north) in decimal degrees

    Returns:
        bool: True if the cache can be expected to hold data for the bbox.
    """
    meta = read_cache_metadata()
    west, south, east, north = bbox

    state_bboxes = meta.get('state_bboxes') or {}
    if state_bboxes:
        for sb in state_bboxes.values():
            if not sb or len(sb) != 4:
                continue
            s_west, s_south, s_east, s_north = sb
            if (west <= s_east and east >= s_west and
                    south <= s_north and north >= s_south):
                return True
        return False

    cache_bbox = meta.get('bbox')
    if not cache_bbox or len(cache_bbox) != 4:
        return False
    c_west, c_south, c_east, c_north = cache_bbox
    return (west >= c_west and east <= c_east and
            south >= c_south and north <= c_north)


# ===============================================================================
# STEP 3: Main cache read function — the drop-in fallback
# ===============================================================================

def load_osm_from_cache(bbox):
    """Load OSM features from the local cache for the given bounding box.

    Called by download_osm_features() on every analysis. Returns a dict
    keyed trails, roads, waterways, powerlines — the shape the retired
    live Overpass path produced, so nothing downstream had to change.

    Each GeoPackage layer has an RTree spatial index built during cache
    construction, so the bbox clip is near-instant even though the source
    layer may contain millions of features spanning all fifty
    states. GeoPandas uses the index automatically via the `bbox` parameter
    of `read_file`.

    Args:
        bbox: (west, south, east, north) in decimal degrees

    Returns:
        Dict with 'trails', 'roads', 'waterways', 'powerlines' keys. Each
        value is a GeoDataFrame. Returns empty GeoDataFrames for any layer
        that fails to read, so a single bad layer degrades the analysis
        rather than aborting it.

    Raises:
        FileNotFoundError: If the cache GeoPackage doesn't exist. Callers
            should call cache_is_available() first to avoid this.
    """
    if not cache_is_available():
        raise FileNotFoundError(f"OSM cache not found at {CACHE_GPKG}")

    west, south, east, north = bbox
    # GeoPandas expects bbox as (minx, miny, maxx, maxy) in the layer's CRS.
    # Our cache is stored in EPSG:4326 (same as the live OSM path), so we
    # can pass the bbox tuple directly without reprojection.
    bbox_tuple = (west, south, east, north)

    # Result dict initialized with empty GeoDataFrames matching the schema
    # of download_osm_features() — any per-layer read failures below will
    # leave these empty entries in place.
    result = {
        'trails': gpd.GeoDataFrame(columns=['geometry', 'type', 'name'], crs='EPSG:4326'),
        'roads': gpd.GeoDataFrame(columns=['geometry', 'type', 'name'], crs='EPSG:4326'),
        'waterways': gpd.GeoDataFrame(columns=['geometry', 'type', 'name', 'width'], crs='EPSG:4326'),
        'powerlines': gpd.GeoDataFrame(columns=['geometry', 'type', 'name'], crs='EPSG:4326'),
    }

    # Read each layer with the bbox filter. Fiona/pyogrio (GeoPandas' I/O
    # backend) pushes the bbox filter down to the GeoPackage RTree index,
    # so we only load features that actually intersect the analysis area.
    for layer_name in CACHE_LAYERS:
        try:
            gdf = gpd.read_file(CACHE_GPKG, layer=layer_name, bbox=bbox_tuple)
            if len(gdf) > 0:
                result[layer_name] = gdf
        except Exception as e:
            # A corrupt or missing layer shouldn't break the whole fallback.
            # Log loudly so the issue is visible in journald, but leave the
            # empty GeoDataFrame in place so the analysis still runs (it
            # will just lack that feature class — indistinguishable from
            # OSM having no features of that class in the area).
            print(f"  WARNING: Cache layer '{layer_name}' read failed: {e}")

    trail_count = len(result['trails'])
    road_count = len(result['roads'])
    ww_count = len(result['waterways'])
    pl_count = len(result['powerlines'])
    print(f"  Cache: {trail_count} trails, {road_count} roads, "
          f"{ww_count} waterways, {pl_count} power lines")

    return result
