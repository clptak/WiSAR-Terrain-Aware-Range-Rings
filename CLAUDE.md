# WiSAR Terrain-Aware Range Rings

Flask backend + single-page Leaflet front end at **sar.weleber.net**, gunicorn
on 127.0.0.1:8000. **Real search-and-rescue people use this.** Treat production
accordingly: it is authoritative, and git mirrors it rather than the reverse.

Shared conventions and deploy rules live in `D:\Projects\CLAUDE.md`.

## What it computes

From an IPP (initial planning point) it builds a terrain friction surface and
runs an anisotropic Dijkstra cost-distance over it, then contours the result:

- **TARR mode** — thresholds cost-distance at Koester Lost Person Behavior
  p25/p50/p75 find distances into three nested polygons.
- **Travel Time mode** — divides cost-distance by a user speed into isochrones.

A Jacobs (2015) terrain-attractor heatmap is drawn over either. Outputs are PNG
overlays, GeoTIFFs, KML/GeoJSON, and a push to CalTopo.

## The three things most likely to bite

**Everything is synchronous.** `/api/analyze` blocks for tens of seconds
to minutes — a pure-Python `heapq` Dijkstra over up to 1000×1000 cells
plus one external HTTP fetch (3DEP, 120 s timeout) and three local
snapshot reads. The front end fakes progress
with an 8-second message rotator; it is not real progress. The long gunicorn and
nginx timeouts that make this work are configured **only on the server**, so any
timeout change there can silently break the app.

**`WORK_DIR` is created once per process, not per run** (`pipeline/shared.py`),
despite a comment claiming otherwise. Intermediate rasters have fixed names
(`dem.tif`, `cost_surface.tif`, `cost_distance.tif`, `jacobs_masks.tif`). **Two
concurrent analyses in one worker overwrite each other.** There is no queue and
no job id in the filenames.

**`analysis_id` is just the rounded coordinates** — `f"{lat:.4f}_{lng:.4f}"`.
Two users starting from the same IPP collide and overwrite each other's results.
The `analyses` dict is global and never evicts, and `/tmp/wisar_results/*.json`
is never cleaned; after a restart those JSONs still reference the old process's
raster paths.

## Code that looks wrong and is not — do not "clean up"

- **Burn order in `build_cost_surface`**: trails and roads are burned *last* at
  impedance 1.0 so bridges and crossings stay passable over water.
- **`_compute_attractor_score_max` uses `max`, not `sum`** — summing would
  double-count Jacobs's overlapping categories.
- **`JACOBS_STREAM_STRAHLER_MIN = 4`** deliberately deviates from Jacobs's ≥5.
  It was 3 against the 1:100k network; 4 reproduces that field-reviewed
  display on the 1:24k snapshot (numbers in the constant's comment).
- **The endpoint named `cost_surface.png` no longer renders the cost surface**,
  and **`export-tarrs` handles isochrones too** (mode detected by an `hours`
  property, duplicated in `app.js` — keep both in sync). Both names are frozen
  for front-end compatibility.
- **CalTopo TARR descriptions are word-for-word frozen** because field reports
  reference the wording.
- **`WISAR_USER_AGENT`** is still sent to 3DEP and by the cache builders.
  It was mandatory while the live Overpass path existed (406 to the
  default requests UA); that path was retired in v1.16, but keep the header.
- The `.png` routes are more specific than `/<filename>`; do not add a
  `<filename>` variant that shadows them.

## Suspected real bug, verify before touching

`app.js` multiplies p25/p50/p75 by per-band `CALIBRATION_MULTIPLIERS` **and**
sends `params.profile`; `server.py` then applies a *second*, scalar multiplier
(default 1.40) to the already-calibrated values. The two tables disagree in both
shape and values, and the README states calibration is front-end only. This
changes search areas that real teams act on — **verify against production
behaviour before changing either table.**

## Environment and integrations

CalTopo credentials come from `/etc/wisar.env` (root-only, mode 600) via the
systemd unit — `CALTOPO_ACCOUNT_ID`, `CALTOPO_CREDENTIAL_ID`,
`CALTOPO_CREDENTIAL_KEY`. They are **not** in this repo and must never be. The
app only warns if they are missing, so a broken export looks like a silent
no-op.

Every analysis makes exactly one live request: the USGS 3DEP DEM. Land
cover, hydrography and OSM come from the local snapshots below. The live
MRLC WMS and USGS hydro MapServer paths were retired in v1.17 after they
became the pipeline's bottleneck (120 s and 3×60 s timeouts) and, worse,
silently dropped layers on timeout. `tools/compare_sources.py` still
carries copies of those fetchers for regression checks.

`app/requirements.txt` was captured from the production venv. The geospatial
stack is tightly coupled — rasterio, geopandas, pyogrio and fiona all bind the
same GDAL — so upgrade the set together on a rebuilt venv and run a real
analysis before deploying. System packages (`gdal-bin`, `libgdal-dev`,
`python3-gdal`, `libspatialindex-dev`) come from `provision.sh` phase 10.

## The local snapshots

Three caches under `/var/www/sar.weleber.net/cache/`, all outside `app/` so
the deploy cannot touch them, all regenerable and deliberately not backed
up, all read on every analysis. Each has a reader in `pipeline/*_cache.py`
and a standalone builder in `tools/build_*_cache.py` with the cache path
hardcoded in **both** places so cron does not need the package on
`sys.path`. Change one, change both. A missing cache is a warning in the
results panel, not an error.

- **`nlcd/`** — one tiled CONUS GeoTIFF, Annual NLCD 2024, in Albers
  (EPSG:5070). `build_nlcd_cache.py` probes mrlc.gov for the newest year;
  yearly cron. `download_nlcd` returns `(path, warnings)`; None outside
  CONUS, and `build_cost_surface` reprojects onto the DEM grid as before.
- **`nhd/`** — `nhd_cache.gpkg` with `flowlines` (joined to Strahler
  order), `waterbodies`, `areas`, built from all 233 USGS NHDPlus HR basin
  packages by `build_hydro_cache.py` (ogr2ogr, one basin on disk at a
  time, ~110 GB downloaded, ~20 GB output, quarterly cron). **This is
  1:24k; the MapServer flowlines it replaced were 1:100k NHDPlus V2**, so
  stream counts and Strahler orders rose, and `JACOBS_STREAM_STRAHLER_MIN`
  went from 3 to 4 to compensate.
- **`osm/`** — described below.

### The OSM cache

`/var/www/sar.weleber.net/cache/osm/` holds `osm_cache.gpkg` plus metadata,
rebuilt weekly by cron from `tools/build_osm_cache.py` (51 Geofabrik PBFs, every state plus DC,
filtered with `osmium`, streamed as Arrow batches — it OOM'd on California
before the batching rewrite). Since v1.16 it is the **only** OSM source;
there is no live Overpass path. `download_osm_features` keeps its name for
the callers, reads the snapshot, and attaches a warning when it is older
than `OSM_CACHE_STALE_DAYS` (14). If the cron job dies, that warning is
the only symptom.

Cache paths are hardcoded in **two** places — `pipeline/osm_cache.py` and
`tools/build_osm_cache.py` — deliberately, so cron does not need the package on
`sys.path`. Change one, change both.

The ~19 GB cache (all 50 states + DC, ~58 million features, roughly an hour to
rebuild) lives *outside* `app/`, which is why the deploy cannot touch
it. It is regenerable and deliberately not backed up.

## Known stale documentation

`app/static/metadata.html` is orphaned and stale; `app/README.md` describes
modes that no longer exist; several comments in `index.html` describe an inline-
JS layout and a `/api/caltopo` proxy that are both gone. The root `README.md` is
current. Do not trust in-repo prose over the code.

## Resolution caveat

Rasters are capped at 1000 px, which silently degrades cell size. Comments claim
30 m cells, but Travel Time at 3 mph × 12 h forces a ~60 km radius and roughly
120 m cells. Nothing warns the user, and the beta track that used to exist for
testing this was retired — changes go straight to the tool teams rely on.
