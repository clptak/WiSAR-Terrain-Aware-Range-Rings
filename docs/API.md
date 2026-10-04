# WiSAR headless API (`/api/v1`)

The contract is [`openapi.json`](openapi.json) (OpenAPI 3.1). A running
container serves it at `/api/v1/openapi.json`, with an interactive Swagger UI
at `/api/v1/docs`. This page covers how the API behaves and how to run and
test it.

## How a client uses it

1. `GET /api/v1/profiles` to fill the subject picker (TARR only).
2. `POST /api/v1/tarr/jobs` or `POST /api/v1/travel-time/jobs`. The response
   is `202 Accepted` with a `Location` header and the job, including
   `resolved`: the exact distances, multipliers or speed that will be used.
3. Poll `GET /api/v1/jobs/{id}`, honouring `Retry-After`, until `status` is
   `succeeded` or `failed`. `queue_position` says how many analyses are ahead.
4. Download from `outputs[name].href`.

Every call except `/health`, `/openapi.json` and `/docs` needs
`Authorization: Bearer <CloudTAK token>`. WiSAR checks the token against
CloudTAK `GET /api/login` and caches the answer for 5 minutes.

## Outputs

| Name | TARR | Travel time | Content |
|---|---|---|---|
| `contours.geojson` | yes | yes | RFC 7946 polygons with `callsign`, `remarks`, `color`, `threshold_m` |
| `contours.kml` | yes | yes | Same polygons, KML 2.2, styled |
| `cost-distance.tif` | yes | yes | Float32 cumulative cost from the IPP, flat-ground-equivalent metres |
| `cost-surface.tif` | yes | yes | Float32 friction multiplier per cell |
| `attractor-score.tif` | yes | yes | Float32 0–1 Jacobs (2015) terrain-attractor score (the web heatmap as data) |
| `probability.tif` | yes | no | Band class: 4 inside p25, 3 p25–p50, 2 p50–p75, 1 beyond p75 |
| `overlay-attractor.png` / `.tif` | yes | yes | Terrain Attractor Priority, colored as the web tool's heatmap |
| `overlay-terrain.png` / `.tif` | yes | yes | Terrain Difficulty, colored as the web tool's layer (left out without elevation data) |
| `overlay-probability.png` / `.tif` | yes | no | Probability (TARR bands): the web tool's percentile zones and lines, without labels |

Rasters are EPSG:4326 Cloud-Optimized GeoTIFFs, DEFLATE, lossless, NoData
−9999 (0 for `probability.tif`). The grid is capped at 1000×1000 cells;
`result.cell_size_m` shows when a large analysis was coarsened.

### Colored overlays

`result.overlays` lists the colored map layers drawn for the job, each with
`id`, `title`, `png`, `geotiff` and `bounds`. They are rendered at job end
by `app/api/overlays.py` with the same colors and rules as the web tool's
`cost_surface.png`, `terrain.png` and `percentiles.png` routes (those read the
legacy result store, which v1 jobs don't use). The PNG is an RGBA image on the
job grid for a temporary map preview: draw it over `bounds`. The `.tif` is the
same picture as an RGBA Cloud-Optimized GeoTIFF that CloudTAK's Imports can
turn into a lasting overlay. A layer that can't be drawn is left out of the
list and reported in `result.warnings` (`source: overlay`); it never fails
the job. The coloring is copied from `server.py`: keep it in step when those
routes change.

## Subjects and calibration (TARR)

- **Listed**: `{"kind": "listed", "category", "eco_region", "terrain"}` from
  a dataset in `app/api/data/profiles/`. Variant lookup and per-band
  calibration match the web tool exactly.
- **Custom**: `{"kind": "custom", "name", "distances": {"p25", "p50", "p75", "p90", "unit": "km"|"mi"}}`.
  `p90` is optional (see below).
- `calibration`: `auto` (default; listed → category or dataset-default
  multipliers, custom → none), `global` (dataset default), `none`.

### The 90% ring (custom subjects)

The Arizona table has a 90% distance that Koester data doesn't. A custom
subject may send it as `p90` (greater than `p75`). It is an API addition;
Jamie's pipeline is unchanged:

- The analysis radius becomes `p90 + 2 km` instead of `p75 + 2 km`, so the
  grid can be slightly coarser for every ring (1000-cell cap).
- The pipeline runs with 25/50/75 as usual. The API then cuts a fourth ring,
  `percentile: "90%"`, color `#e5383b`, from the same cost-distance raster by
  calling the pipeline's own `extract_contour_polygons` with the 90% distance,
  so the cut and smoothing are identical.
- `p90` is never calibrated: no dataset has a 90% multiplier. If calibration
  moves `p75` to or past it, the ring is dropped, `final_distances_km` has no
  `p90`, and the job carries a `p90` warning. The job still succeeds.
- `probability.tif` and the probability overlay keep their four classes
  (class 1 is everything beyond 75%).

To add a dataset, drop another `<id>.json` in `app/api/data/profiles/` with
the same shape as `koester.json`. It appears in `GET /profiles` on restart.

## Several CloudTAK deployments

One WiSAR can serve more than one CloudTAK. List them in
`WISAR_CLOUDTAK_INSTANCES` as `<web origin>=<API base URL>` pairs separated by
commas, or just the web origin when CloudTAK's API is on the same origin:

```
WISAR_CLOUDTAK_INSTANCES=https://map.a.org=http://cloudtak-api:5000,https://map.b.org
```

- The browser's `Origin` header picks the CloudTAK whose `GET /api/login`
  checks the token. A token is only accepted by the deployment that issued it.
- An origin that isn't listed gets `403 CloudTAK not registered`.
- Requests without an `Origin` (curl, `api_smoke.sh`) and WiSAR's own Swagger
  UI use the first entry.
- Every listed origin is allowed for CORS; `WISAR_CORS_ORIGINS` is still read
  and added to them.
- Jobs belong to the user on their deployment: the same email on two
  deployments sees only its own jobs. Jobs carry `instance` (the origin).
  Jobs created before this setting existed belong to the first entry.
- All deployments share one queue (`WISAR_MAX_QUEUED`) and run one analysis
  at a time.
- A forged `Origin` from a non-browser client gains nothing: the token must
  still be valid on the deployment it names.

Without the setting, `CLOUDTAK_API_URL` verifies every token and `instance`
is `null`, as before.

## Reference content

`GET /api/v1/content/{id}` returns one section of the web tool's reference
text so a plugin can show it in a modal: `metadata`, `changelog`,
`validation`, `tarr-explainer`, `travel-time-explainer` and `scope-note` (the
note under the mode choice on the start screen).

The sections are read from `app/static/index.html` when requested, so they
follow upstream edits to that page; the page itself is not served. Each one
is the modal's inner HTML with its Close / Got it button, scripts, form
controls and inline event handlers removed. The inline styles use the page's
CSS variables, and `css_variables` gives their values; the colours were
chosen for a dark background. Responses carry an `ETag` (send it back in
`If-None-Match` for a `304`). If upstream renames a modal, that section
returns `503` until `ITEMS` in `app/api/content.py` is updated.

## Queue and retention

One analysis runs at a time, first in first out, including the legacy
`/api/analyze` and `/api/analyze-isochrone` routes. Those keep their original
synchronous response but wait their turn on the same queue. More than
`WISAR_MAX_QUEUED` (10) waiting v1 jobs returns `503` with `Retry-After`.
Finished jobs are deleted after `WISAR_JOB_TTL_HOURS` (72) and then return
`410`. Jobs that were queued or running when the container restarted come
back as `failed` with the title `Interrupted`.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `CLOUDTAK_API_URL` | `http://api:5000` | Where WiSAR reaches CloudTAK to verify tokens (single-CloudTAK mode) |
| `WISAR_CLOUDTAK_INSTANCES` | *(empty)* | Several CloudTAK deployments sharing this WiSAR; see below. Replaces `CLOUDTAK_API_URL` when set |
| `WISAR_CORS_ORIGINS` | *(empty)* | Comma-separated browser origins allowed to call `/api` |
| `WISAR_JOB_TTL_HOURS` | `72` | Retention of finished jobs |
| `WISAR_MAX_QUEUED` | `10` | Queue limit for v1 jobs |
| `WISAR_AUTH` | `cloudtak` | `none` disables authentication. Local testing only. |
| `WISAR_JOBS_DIR` | `/var/wisar/jobs` | Job records and outputs (the `wisar-jobs` volume) |
| `WISAR_CONTENT_HTML` | `static/index.html` in the app | Web tool page the reference content is read from |

## Testing

Unit and contract tests. The pipeline is faked, so no snapshots or network
are needed; everything after it runs for real:

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r app/requirements.txt -r tests/requirements.txt
pytest tests/api
```

End to end against a running container (real pipeline and data):

```sh
docker compose up -d --build
WISAR_URL=http://localhost:8000 WISAR_TOKEN=<CloudTAK token> tests/api_smoke.sh travel-time
WISAR_URL=http://localhost:8000 WISAR_TOKEN=<CloudTAK token> tests/api_smoke.sh tarr
```

Or open `http://localhost:8000/api/v1/docs`, click **Authorize**, paste a
CloudTAK token and use **Try it out**.

## Legacy routes

`/api/analyze`, `/api/analyze-isochrone`, `/api/results/…` and
`/api/caltopo/export-tarrs` still work with their original request and
response shapes, and now also require the CloudTAK token. They key results by
the IPP rounded to 4 decimals, so two runs from the same IPP overwrite each
other's saved results, and any later analysis overwrites the working rasters
their `/api/results/…` routes read. `/api/v1` has neither problem. The
Leaflet UI is not served: `/` redirects to `/api/v1/docs`.
