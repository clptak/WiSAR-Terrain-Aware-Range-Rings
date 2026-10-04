"""Colored map overlays for the CloudTAK plugins (decision: Paul, 2026-10-04).

The web tool draws three raster layers as PNGs from routes in server.py:

- Terrain Attractor Priority   /api/results/<id>/cost_surface.png  (serve_cost_png)
- Terrain Difficulty           /api/results/<id>/terrain.png       (serve_terrain_png)
- TARR percentile bands        /api/results/<id>/percentiles.png   (serve_percentile_png)

Those routes read the legacy result store and the analysis work folder. v1
jobs use neither, and their work folder is deleted once outputs are built, so
the same pictures are rendered here, at job end, while the inputs still
exist. server.py and the pipeline are not changed:

- attractor reuses server._load_jacobs_masks, _compute_attractor_score_max
  and _apply_colormap by import; only the alpha ramp is copied.
- terrain and probability are copied from their routes.

KEEP IN STEP: if the colors or rules in those three routes change upstream,
copy the change here too (the docstring of each function names its source).

Each overlay is written twice: a PNG for the plugins' temporary map preview,
placed with the job's bounds, and an RGBA Cloud-Optimized GeoTIFF that
CloudTAK's Imports can turn into a lasting overlay. The web tool's in-image
percent labels are left out of the probability layer: baked-in text does not
scale on a tiled map, and the plugins already label the contours.
"""
import math
import os

import numpy as np
import rasterio

# id -> (title, TARR only)
OVERLAYS = {
    'attractor': ('Terrain Attractor Priority', False),
    'terrain': ('Terrain Difficulty', False),
    'probability': ('Probability (TARR bands)', True),
}


def png_name(overlay_id):
    return f'overlay-{overlay_id}.png'


def tif_name(overlay_id):
    return f'overlay-{overlay_id}.tif'


def attractor_rgba(score, nodata_mask):
    """serve_cost_png: shared colormap, alpha ramp ALPHA_FLOOR + ALPHA_RANGE *
    score ** ALPHA_GAMMA (60 / 170 / 0.6), NoData transparent."""
    import server  # imported late: server.py is the Flask app module
    score = np.where(nodata_mask, 0.0, score).astype(np.float64)
    height, width = score.shape
    r_arr, g_arr, b_arr = server._apply_colormap(score)
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[:, :, 0] = r_arr.clip(0, 255).astype(np.uint8)
    rgba[:, :, 1] = g_arr.clip(0, 255).astype(np.uint8)
    rgba[:, :, 2] = b_arr.clip(0, 255).astype(np.uint8)
    ALPHA_FLOOR = 60
    ALPHA_RANGE = 170
    ALPHA_GAMMA = 0.6
    rgba[:, :, 3] = np.where(nodata_mask, 0,
                             ALPHA_FLOOR + ALPHA_RANGE * (score ** ALPHA_GAMMA)).clip(0, 255).astype(np.uint8)
    return rgba


def terrain_rgba(cost_path, dem_path):
    """serve_terrain_png: difficulty = max(slope score, friction score),
    0-100, through the web tool's green-to-red stops, alpha 150."""
    from scipy.signal import convolve2d
    with rasterio.open(cost_path) as src:
        friction = src.read(1).astype(np.float64)
        transform = src.transform
        height, width = friction.shape
    with rasterio.open(dem_path) as src:
        dem = src.read(1).astype(np.float64)
        if dem.shape != (height, width):
            from rasterio.warp import reproject, Resampling
            dem2 = np.zeros((height, width), dtype=np.float64)
            reproject(source=rasterio.band(src, 1), destination=dem2,
                      src_transform=src.transform, src_crs=src.crs,
                      dst_transform=transform, dst_crs=src.crs, resampling=Resampling.bilinear)
            dem = dem2
    dem[dem < -1000] = np.nan
    dem[dem > 10000] = np.nan
    center_lat = (transform[5] + transform[5] + transform[4] * height) / 2
    cx = abs(transform[0]) * 111320 * math.cos(math.radians(center_lat))
    cy = abs(transform[4]) * 110540
    kx = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]]) / (8.0 * cx)
    ky = np.array([[-1, -2, -1], [0, 0, 0], [1, 2, 1]]) / (8.0 * cy)
    dzdx = convolve2d(dem, kx, mode='same', boundary='symm')
    dzdy = convolve2d(dem, ky, mode='same', boundary='symm')
    slope_deg = np.degrees(np.arctan(np.sqrt(dzdx ** 2 + dzdy ** 2)))
    slope_score = np.clip(slope_deg * 2.0, 0, 90)
    fric_score = np.clip((friction - 1.0) * 20.0, 0, 95)
    difficulty = np.maximum(slope_score, fric_score)
    nodata_mask = np.isnan(dem) | (friction <= 0) | (friction == -9999)
    norm = np.clip(difficulty / 100.0, 0, 1)
    stops = [(0.0, 20, 140, 40), (0.1, 50, 175, 50), (0.2, 100, 200, 45),
             (0.3, 160, 210, 30), (0.4, 210, 215, 15), (0.5, 240, 195, 0),
             (0.6, 245, 150, 10), (0.7, 235, 100, 15), (0.8, 215, 55, 12),
             (0.9, 185, 25, 10), (1.0, 140, 12, 10)]
    r_arr = np.full_like(norm, 140.0)
    g_arr = np.full_like(norm, 12.0)
    b_arr = np.full_like(norm, 10.0)
    for i in range(len(stops) - 1):
        t0, r0, g0, b0 = stops[i]
        t1, r1, g1, b1 = stops[i + 1]
        mask = (norm >= t0) & (norm < t1) if i < len(stops) - 2 else (norm >= t0) & (norm <= t1)
        frac = np.where(mask, (norm - t0) / (t1 - t0), 0)
        r_arr = np.where(mask, r0 + frac * (r1 - r0), r_arr)
        g_arr = np.where(mask, g0 + frac * (g1 - g0), g_arr)
        b_arr = np.where(mask, b0 + frac * (b1 - b0), b_arr)
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[:, :, 0] = r_arr.clip(0, 255).astype(np.uint8)
    rgba[:, :, 1] = g_arr.clip(0, 255).astype(np.uint8)
    rgba[:, :, 2] = b_arr.clip(0, 255).astype(np.uint8)
    rgba[:, :, 3] = np.where(nodata_mask, 0, 150).astype(np.uint8)
    return rgba


def probability_rgba(prob_path):
    """serve_percentile_png without the in-image labels: filled zones
    (4 red, 3 amber, 2 yellow) and boundary lines, 1-px dilated."""
    from scipy.ndimage import binary_dilation
    with rasterio.open(prob_path) as src:
        data = src.read(1)
    height, width = data.shape
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[data == 4] = [220, 38, 38, 100]
    rgba[data == 3] = [245, 158, 11, 80]
    rgba[data == 2] = [250, 204, 21, 60]
    struct = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=bool)
    for zone_val, color in [(4, [255, 255, 255, 220]), (3, [255, 200, 50, 200]), (2, [255, 100, 30, 200])]:
        mask = (data >= zone_val).astype(np.uint8)
        kernel_h = np.abs(np.diff(mask, axis=1))
        kernel_v = np.abs(np.diff(mask, axis=0))
        edge = np.zeros_like(mask)
        edge[:, :-1] |= kernel_h
        edge[:, 1:] |= kernel_h
        edge[:-1, :] |= kernel_v
        edge[1:, :] |= kernel_v
        edge = binary_dilation(edge, structure=struct, iterations=1)
        rgba[edge] = color
    return rgba


def write_overlay(rgba, ref_path, png_path, tif_path, write_cog):
    """Write the PNG preview and the RGBA COG on ref_path's grid; return the
    overlay's bounds (west/south/east/north)."""
    from PIL import Image
    Image.fromarray(rgba, 'RGBA').save(png_path, format='PNG', optimize=True)
    with rasterio.open(ref_path) as ref:
        profile = ref.profile.copy()
        b = ref.bounds
    if rgba.shape[:2] != (profile['height'], profile['width']):
        raise ValueError(f'overlay is {rgba.shape[:2]}, grid is {(profile["height"], profile["width"])}')
    # alpha='YES' writes the TIFF ExtraSamples tag, so band 4 stays alpha
    # through the COG copy (setting colorinterp alone does not survive it)
    profile.update(driver='GTiff', count=4, dtype='uint8', nodata=None, photometric='RGB', alpha='YES')
    for key in ('blockxsize', 'blockysize', 'tiled', 'compress', 'predictor', 'interleave'):
        profile.pop(key, None)
    tmp = tif_path + '.src.tif'
    with rasterio.open(tmp, 'w', **profile) as dst:
        dst.write(np.moveaxis(rgba, 2, 0))
    write_cog(tmp, tif_path)
    os.remove(tmp)
    return {'west': b.left, 'south': b.bottom, 'east': b.right, 'north': b.top}
