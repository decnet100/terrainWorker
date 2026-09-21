"""Build a far-field backdrop mesh from Copernicus DEM + viewshed mask.

POC: 5x5 (+ local peaks) viewshed with earth curvature, cut invisible cells
and the playable bbox, four quadrant Collada TSStatics, WorldCover or
elevation/slope tint, no collision. Near ring uses Tirol DGM; cells without
coverage stay empty (no Copernicus fill). If sources.snow is set, the DGM
snow proxy recolours baked textures to match the playable map.

Usage:
  cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = \"config/sites/fernpass_mega.yaml\"; python tools\\build_backdrop.py
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import binary_closing, binary_dilation, gaussian_filter, label, map_coordinates, median_filter, zoom

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from fetch_backdrop import (  # noqa: E402
    backdrop_cfg,
    dem_paths,
    fetch_backdrop,
    fetch_mid_dgm,
    fetch_mid_dom,
    fetch_near_dgm,
    fetch_near_dom,
    fetch_swissimage_ortho,
    load_backdrop_dem,
    load_mid_dgm,
    load_mid_dom,
    load_near_dgm,
    load_near_dom,
    mid_extent,
    mid_ortho_path,
    near_extent,
    padded_extent,
    near_ortho_path,
    ortho_path,
    site_center,
    worldcover_path,
)
from site_coords import SiteCoords, load_site, processed_dir, site_slug  # noqa: E402
from heightmap_layers import load_composed_or_dgm  # noqa: E402
from build_snow_proxy import (  # noqa: E402
    _cfg as snow_proxy_cfg,
    compute_snow_proxy,
)

USER_LEVELS = (
    Path.home()
    / "AppData"
    / "Local"
    / "BeamNG"
    / "BeamNG.drive"
    / "current"
    / "levels"
)

EARTH_R_M = 6371000.0
MAT_NAME = "mat_backdrop_far"
MAT_NEAR = "mat_backdrop_near"
MAT_MID = "mat_backdrop_mid"
# BeamNG Collada / TSMesh uses 16-bit indices. One mesh above this is dropped.
COLLADA_MAX_VERTS = 65535
IDENTITY_ROT = [1, 0, 0, 0, 1, 0, 0, 0, 1]
_TILE_LETTERS = "abcdefghijklmnopqrstuvwxyz"
_TILE_3 = (
    ("nw", "n", "ne"),
    ("w", "c", "e"),
    ("sw", "s", "se"),
)

# Match compose_biomes.BIOME_RGB snow_light / snow_heavy (0–1).
_SNOW_LIGHT_RGB = np.array([200, 210, 230], dtype=np.float32) / 255.0
_SNOW_HEAVY_RGB = np.array([245, 245, 250], dtype=np.float32) / 255.0
_WC_WATER = 80
_WC_NO_SNOW = (10, 20, 80)  # trees, shrub, water — do not paint DGM snow on canopy

# Muted alpine remap of ESA WorldCover classes (not the cartoony legend).
WC_RGB = {
    10: (46, 84, 42),
    20: (90, 100, 48),
    30: (110, 125, 58),
    40: (120, 118, 62),
    50: (120, 112, 108),
    60: (138, 128, 118),
    70: (228, 232, 236),
    80: (48, 78, 110),
    90: (70, 105, 88),
    100: (140, 132, 92),
}


def _heightmap_z0(site: dict) -> float:
    meta_path = processed_dir(site) / "heightmap_meta.json"
    if not meta_path.is_file():
        raise SystemExit(
            f"Missing {meta_path} - run build_smoke.py for this site first "
            "(backdrop Z is relative to the playable heightmap)."
        )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return float(meta["z_min_m"])


def _elev_at(elev: np.ndarray, meta: dict, x: float, y: float) -> float:
    col = (x - float(meta["origin_x"])) / float(meta["px"]) - 0.5
    row = (y - float(meta["origin_y"])) / float(meta["py"]) - 0.5
    h, w = elev.shape
    if not (0 <= row <= h - 1 and 0 <= col <= w - 1):
        return float("nan")
    z = map_coordinates(elev, [[row], [col]], order=1, mode="nearest", prefilter=False)[0]
    return float(z)


def _observer_pixels(elev: np.ndarray, meta: dict, site: dict, cfg: dict) -> list[tuple[float, float]]:
    xmin, ymin, xmax, ymax = map(float, site["bbox"])
    n = max(2, int(cfg["observer_grid"]))
    xs = np.linspace(xmin, xmax, n)
    ys = np.linspace(ymin, ymax, n)
    obs: list[tuple[float, float]] = []
    for y in ys:
        for x in xs:
            z = _elev_at(elev, meta, float(x), float(y))
            if np.isfinite(z):
                obs.append((float(x), float(y)))
    n_peak = max(0, int(cfg["extra_peaks"]))
    if n_peak:
        px, py = float(meta["px"]), float(meta["py"])
        c0 = (xmin - float(meta["origin_x"])) / px
        c1 = (xmax - float(meta["origin_x"])) / px
        r0 = (ymax - float(meta["origin_y"])) / py
        r1 = (ymin - float(meta["origin_y"])) / py
        ra, rb = sorted((int(np.clip(r0, 0, elev.shape[0] - 1)), int(np.clip(r1, 0, elev.shape[0] - 1))))
        ca, cb = sorted((int(np.clip(c0, 0, elev.shape[1] - 1)), int(np.clip(c1, 0, elev.shape[1] - 1))))
        rm = (ra + rb) // 2
        cm = (ca + cb) // 2
        blocks = [
            (ra, rm, ca, cm),
            (ra, rm, cm, cb),
            (rm, rb, ca, cm),
            (rm, rb, cm, cb),
        ]
        for r0b, r1b, c0b, c1b in blocks[:n_peak]:
            sub = elev[r0b : r1b + 1, c0b : c1b + 1]
            if not sub.size or not np.isfinite(sub).any():
                continue
            rr, cc = np.unravel_index(int(np.nanargmax(sub)), sub.shape)
            x = float(meta["origin_x"]) + (c0b + cc + 0.5) * px
            y = float(meta["origin_y"]) + (r0b + rr + 0.5) * py
            obs.append((x, y))
    return obs


def _xy_to_pixel(meta: dict, x: float, y: float) -> tuple[float, float]:
    col = (x - float(meta["origin_x"])) / float(meta["px"]) - 0.5
    row = (y - float(meta["origin_y"])) / float(meta["py"]) - 0.5
    return col, row


def viewshed_one(
    elev: np.ndarray,
    ox: float,
    oy: float,
    extra_z: float,
    cell_m: float,
    max_dist_m: float,
    n_rays: int,
    cc: float,
) -> np.ndarray:
    h, w = elev.shape
    vis = np.zeros((h, w), dtype=bool)
    n_t = max(2, int(math.ceil(max_dist_m / cell_m)))
    t = np.arange(1, n_t + 1, dtype=np.float64)
    dist = t * cell_m
    drop = cc * (dist * dist) / (2.0 * EARTH_R_M)
    angles = np.linspace(0.0, 2.0 * math.pi, n_rays, endpoint=False)
    dc = np.cos(angles)
    dr = -np.sin(angles)  # north = −row
    cols = ox + np.outer(dc, t)
    rows = oy + np.outer(dr, t)
    ci = np.rint(cols).astype(np.int32)
    ri = np.rint(rows).astype(np.int32)
    inside = (ri >= 0) & (ri < h) & (ci >= 0) & (ci < w)
    z = np.full(ri.shape, np.nan, dtype=np.float64)
    z[inside] = elev[ri[inside], ci[inside]]
    iox = int(round(oy))
    ioy = int(round(ox))
    iox = int(np.clip(iox, 0, h - 1))
    ioy = int(np.clip(ioy, 0, w - 1))
    oh = float(elev[iox, ioy]) + extra_z
    if not np.isfinite(oh):
        return vis
    slope = (z - drop[None, :] - oh) / dist[None, :]
    slope[~inside] = -np.inf
    slope[~np.isfinite(slope)] = -np.inf
    running = np.maximum.accumulate(slope, axis=1)
    hit = (slope >= running - 1e-15) & inside
    vis[ri[hit], ci[hit]] = True
    vis[iox, ioy] = True
    return vis


def compute_viewshed(
    elev: np.ndarray, meta: dict, site: dict, cfg: dict
) -> tuple[np.ndarray, list[tuple[float, float]]]:
    obs = _observer_pixels(elev, meta, site, cfg)
    cell = abs(float(meta["px"]))
    n_rays = int(cfg["n_rays"])
    extra = float(cfg["observer_z_extra_m"])
    cc = float(cfg["curvature_cc"])
    radius = float(cfg["radius_m"])
    print(f"Viewshed observers={len(obs)} rays={n_rays} cell={cell:.0f} m cc={cc}")
    acc = np.zeros(elev.shape, dtype=np.uint16)
    for i, (x, y) in enumerate(obs, start=1):
        col, row = _xy_to_pixel(meta, x, y)
        vis = viewshed_one(elev, col, row, extra, cell, radius, n_rays, cc)
        acc += vis.astype(np.uint16)
        if i == 1 or i == len(obs) or i % 5 == 0:
            print(f"  observer {i}/{len(obs)} visible-so-far={int((acc > 0).sum())}")
    return np.clip(acc, 0, 255).astype(np.uint8), obs


def _world_grids(elev: np.ndarray, meta: dict) -> tuple[np.ndarray, np.ndarray]:
    px, py = float(meta["px"]), float(meta["py"])
    cols = np.arange(elev.shape[1], dtype=np.float64)
    rows = np.arange(elev.shape[0], dtype=np.float64)
    xs = float(meta["origin_x"]) + (cols + 0.5) * px
    ys = float(meta["origin_y"]) + (rows + 0.5) * py
    return np.meshgrid(xs, ys)


def _bbox_rect(xx: np.ndarray, yy: np.ndarray, site: dict, pad: float) -> np.ndarray:
    bx0, by0, bx1, by1 = map(float, site["bbox"])
    return (
        (xx >= bx0 - pad)
        & (xx <= bx1 + pad)
        & (yy >= by0 - pad)
        & (yy <= by1 + pad)
    )


def _grid_tile_name(n_tiles: int, ty: int, tx: int) -> str:
    """Letter tile ids — digits in the stem become BeamNG LOD size 0 (unlit)."""
    if n_tiles == 3:
        return _TILE_3[ty][tx]
    return f"{_TILE_LETTERS[ty]}{_TILE_LETTERS[tx]}"


def keep_near_mask(elev: np.ndarray, meta: dict, site: dict, cfg: dict) -> np.ndarray:
    """Ring outside the playable bbox: no viewshed, no overlap under the terrain."""
    xx, yy = _world_grids(elev, meta)
    outer = float(cfg["near_m"])
    return np.isfinite(elev) & _bbox_rect(xx, yy, site, outer) & ~_bbox_rect(xx, yy, site, 0.0)


def _project_to_bbox_edge(x: float, y: float, site: dict) -> tuple[float, float]:
    bx0, by0, bx1, by1 = map(float, site["bbox"])
    inside = (bx0 < x < bx1) and (by0 < y < by1)
    if inside:
        d = (x - bx0, bx1 - x, y - by0, by1 - y)
        i = int(np.argmin(d))
        if i == 0:
            return bx0, y
        if i == 1:
            return bx1, y
        if i == 2:
            return x, by0
        return x, by1
    return float(min(max(x, bx0), bx1)), float(min(max(y, by0), by1))


def _outside_bbox_m(x: float, y: float, site: dict) -> float:
    bx0, by0, bx1, by1 = map(float, site["bbox"])
    ox = max(bx0 - x, x - bx1, 0.0)
    oy = max(by0 - y, y - by1, 0.0)
    if ox > 0.0 or oy > 0.0:
        return float(math.hypot(ox, oy))
    return 0.0


def _hm_abs_at(hm_rel: np.ndarray, sc: SiteCoords, x: float, y: float, z0: float) -> float:
    n = hm_rel.shape[0]
    bx = (x - sc.xmin) / sc.bw * sc.terrain_extent
    by = (y - sc.ymin) / sc.bh * sc.terrain_extent
    col = float(np.clip(bx, 0.0, n - 1.0))
    row = float(np.clip((n - 1.0) - by, 0.0, n - 1.0))
    rel = map_coordinates(
        hm_rel, [[row], [col]], order=1, mode="nearest", prefilter=False
    )[0]
    return float(rel) + z0


def keep_mid_mask(elev: np.ndarray, meta: dict, site: dict, cfg: dict) -> np.ndarray:
    """Always-keep ring from the near outer edge out to mid_m (no viewshed)."""
    xx, yy = _world_grids(elev, meta)
    inner = max(0.0, float(cfg["near_m"]) - float(cfg["near_overlap_m"]))
    outer = float(cfg["mid_m"])
    return np.isfinite(elev) & _bbox_rect(xx, yy, site, outer) & ~_bbox_rect(xx, yy, site, inner)


def _fill_split_channels(
    keep: np.ndarray,
    finite: np.ndarray,
    row_c: int,
    col_c: int,
    max_gap_px: int,
) -> np.ndarray:
    """Fill N/S and E/W bays up to max_gap_px where keep exists on both sides.

    Morphological closing cannot fill a channel that opens into the mid hole.
    The far east 0.9 km gap at the quadrant split is such a bay.
    """
    if max_gap_px <= 0:
        return keep
    out = keep.copy()
    h, w = keep.shape
    row_c = int(np.clip(row_c, 0, h - 1))
    col_c = int(np.clip(col_c, 0, w - 1))
    for c in range(w):
        north = np.flatnonzero(keep[:row_c, c])
        south = np.flatnonzero(keep[row_c + 1 :, c]) + (row_c + 1)
        if north.size == 0 or south.size == 0:
            continue
        r_n = int(north.max())
        r_s = int(south.min())
        if 0 < (r_s - r_n) <= max_gap_px + 1:
            sl = slice(r_n + 1, r_s)
            out[sl, c] = out[sl, c] | finite[sl, c]
    for r in range(h):
        west = np.flatnonzero(keep[r, :col_c])
        east = np.flatnonzero(keep[r, col_c + 1 :]) + (col_c + 1)
        if west.size == 0 or east.size == 0:
            continue
        c_w = int(west.max())
        c_e = int(east.min())
        if 0 < (c_e - c_w) <= max_gap_px + 1:
            sl = slice(c_w + 1, c_e)
            out[r, sl] = out[r, sl] | finite[r, sl]
    return out


def keep_far_mask(
    elev: np.ndarray, count: np.ndarray, meta: dict, site: dict, cfg: dict
) -> np.ndarray:
    min_views = max(1, int(cfg["min_views"]))
    keep = np.isfinite(elev) & (count >= min_views)
    n_raw = int(keep.sum())
    cell = max(1e-3, abs(float(meta["px"])))
    close_m = max(0.0, float(cfg.get("viewshed_close_m", 600.0)))
    pad_m = max(0.0, float(cfg.get("viewshed_pad_m", 100.0)))
    stitch_m = max(0.0, float(cfg.get("viewshed_stitch_m", 2500.0)))
    close_iter = int(round(close_m / (2.0 * cell))) if close_m > 0.0 else 0
    pad_iter = int(round(pad_m / cell)) if pad_m > 0.0 else 0
    stitch_px = int(round(stitch_m / cell)) if stitch_m > 0.0 else 0
    if close_iter > 0:
        keep = binary_closing(keep, iterations=close_iter)
    if pad_iter > 0:
        keep = binary_dilation(keep, iterations=pad_iter)
    n_morph = int(keep.sum())
    cx, cy = site_center(site)
    radius = float(cfg["radius_m"])
    xx, yy = _world_grids(elev, meta)
    keep &= (xx - cx) ** 2 + (yy - cy) ** 2 <= radius * radius
    far_hole = max(
        float(cfg["hole_pad_m"]),
        float(cfg["mid_m"]) - float(cfg["mid_overlap_m"]),
    )
    keep &= ~_bbox_rect(xx, yy, site, far_hole)
    col_c, row_c = _xy_to_pixel(meta, cx, cy)
    n_pre = int(keep.sum())
    keep = _fill_split_channels(
        keep, np.isfinite(elev), int(round(row_c)), int(round(col_c)), stitch_px
    )
    print(
        f"  far keep close={close_iter}px (~{close_m:.0f}m holes) "
        f"pad={pad_iter}px stitch={stitch_px}px "
        f"raw={n_raw} morph={n_morph} hole={n_pre} final={int(keep.sum())}"
    )
    return keep


def _hillshade_slope(elev: np.ndarray, cell_m: float) -> tuple[np.ndarray, np.ndarray]:
    fill = float(np.nanmedian(elev)) if np.isfinite(elev).any() else 0.0
    z = np.where(np.isfinite(elev), elev, fill)
    dz_dy, dz_dx = np.gradient(z, cell_m, cell_m)
    # row grows south → flip so +Y is north
    dz_dy = -dz_dy
    slp = np.arctan(np.hypot(dz_dx, dz_dy))
    asp = np.arctan2(-dz_dx, dz_dy)
    alt = math.radians(45.0)
    az = math.radians(315.0)
    hs = np.sin(alt) * np.cos(slp) + np.cos(alt) * np.sin(slp) * np.cos(az - asp)
    hs = np.clip(hs, 0.0, 1.0)
    hs = np.where(np.isfinite(elev), hs, 0.0)
    deg = np.degrees(slp)
    deg = np.where(np.isfinite(elev), deg, 0.0)
    return hs.astype(np.float32), deg.astype(np.float32)


def _zoom_hw(arr: np.ndarray, size: int) -> np.ndarray:
    if arr.shape[0] == size and arr.shape[1] == size:
        return arr
    return zoom(arr, (size / arr.shape[0], size / arr.shape[1]), order=1)


def _world_sample_grid(
    elev: np.ndarray,
    keep: np.ndarray,
    meta: dict,
    mesh_step_m: float,
    z_order: int = 1,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Regular metric mesh grid. z_order=3 is cubic spline (not DEM-cell stride).

    Stride-slicing ``elev[::k]`` is nearest-neighbour downsample and reads as
    terraces on alpine slopes. Cubic samples the 2 m DGM at each vertex XY.
    """
    px, py = float(meta["px"]), float(meta["py"])
    ox, oy = float(meta["origin_x"]), float(meta["origin_y"])
    h0, w0 = elev.shape
    x0 = ox + 0.5 * px
    y0 = oy + 0.5 * py
    x1 = ox + (w0 - 0.5) * px
    y1 = oy + (h0 - 0.5) * py
    step = max(1e-3, float(mesh_step_m))
    nx = max(2, int(round(abs(x1 - x0) / step)) + 1)
    ny = max(2, int(round(abs(y1 - y0) / step)) + 1)
    xs = np.linspace(x0, x1, nx, dtype=np.float64)
    ys = np.linspace(y0, y1, ny, dtype=np.float64)
    xx, yy = np.meshgrid(xs, ys)
    col = (xx - ox) / px - 0.5
    row = (yy - oy) / py - 0.5
    coords = np.vstack([row.ravel(), col.ravel()])
    finite = np.isfinite(elev)
    fill = float(np.nanmedian(elev)) if finite.any() else 0.0
    zsrc = np.where(finite, elev, fill).astype(np.float64)
    order = max(0, int(z_order))
    z = map_coordinates(
        zsrc,
        coords,
        order=order,
        mode="nearest",
        prefilter=order >= 3,
    ).reshape(ny, nx)
    k = map_coordinates(keep.astype(np.float32), coords, order=0, mode="nearest")
    fin = map_coordinates(finite.astype(np.float32), coords, order=0, mode="nearest")
    k = (k.reshape(ny, nx) > 0.5) & (fin.reshape(ny, nx) > 0.5) & np.isfinite(z)
    return z, k, xs, ys


def _align_to_grid(
    src: np.ndarray,
    src_meta: dict,
    dst_shape: tuple[int, int],
    dst_meta: dict,
) -> np.ndarray:
    """Cubic-spline sample src onto the destination DGM grid."""
    h, w = dst_shape
    if src.shape == dst_shape and abs(float(src_meta.get("px", 0)) - float(dst_meta["px"])) < 0.05:
        if (
            abs(float(src_meta["origin_x"]) - float(dst_meta["origin_x"])) < 0.51
            and abs(float(src_meta["origin_y"]) - float(dst_meta["origin_y"])) < 0.51
        ):
            return src
    ox, oy = float(dst_meta["origin_x"]), float(dst_meta["origin_y"])
    px, py = float(dst_meta["px"]), float(dst_meta["py"])
    xs = ox + (np.arange(w, dtype=np.float64) + 0.5) * px
    ys = oy + (np.arange(h, dtype=np.float64) + 0.5) * py
    xx, yy = np.meshgrid(xs, ys)
    col = (xx - float(src_meta["origin_x"])) / float(src_meta["px"]) - 0.5
    row = (yy - float(src_meta["origin_y"])) / float(src_meta["py"]) - 0.5
    coords = np.vstack([row.ravel(), col.ravel()])
    finite = np.isfinite(src)
    fill = float(np.nanmedian(src)) if finite.any() else 0.0
    zsrc = np.where(finite, src, fill).astype(np.float64)
    out = map_coordinates(zsrc, coords, order=3, mode="nearest", prefilter=True).reshape(h, w)
    fin = map_coordinates(finite.astype(np.float32), coords, order=0, mode="nearest").reshape(h, w)
    return np.where(fin > 0.5, out, np.nan)


def _align_classes(
    src: np.ndarray,
    src_meta: dict,
    dst_shape: tuple[int, int],
    dst_meta: dict,
) -> np.ndarray:
    """Nearest-neighbor sample class rasters (WorldCover) onto a dest grid."""
    h, w = dst_shape
    if src.shape == dst_shape and abs(float(src_meta.get("px", 0)) - float(dst_meta["px"])) < 0.05:
        if (
            abs(float(src_meta["origin_x"]) - float(dst_meta["origin_x"])) < 0.51
            and abs(float(src_meta["origin_y"]) - float(dst_meta["origin_y"])) < 0.51
        ):
            return src
    ox, oy = float(dst_meta["origin_x"]), float(dst_meta["origin_y"])
    px, py = float(dst_meta["px"]), float(dst_meta["py"])
    xs = ox + (np.arange(w, dtype=np.float64) + 0.5) * px
    ys = oy + (np.arange(h, dtype=np.float64) + 0.5) * py
    xx, yy = np.meshgrid(xs, ys)
    col = (xx - float(src_meta["origin_x"])) / float(src_meta["px"]) - 0.5
    row = (yy - float(src_meta["origin_y"])) / float(src_meta["py"]) - 0.5
    coords = np.vstack([row.ravel(), col.ravel()])
    out = map_coordinates(src.astype(np.float64), coords, order=0, mode="nearest").reshape(h, w)
    return np.rint(out).astype(np.uint8)


def filter_near_canopy(ndsm: np.ndarray, cell_m: float, cfg: dict) -> np.ndarray:
    """Drop <1 m, isolated masts (local-median spikes + tiny blobs), keep forest clumps."""
    min_h = float(cfg.get("near_canopy_min_m", 1.0))
    spike_m = float(cfg.get("near_canopy_spike_m", 8.0))
    med_px = max(1, int(cfg.get("near_canopy_median_px", 5)))
    min_area = float(cfg.get("near_canopy_min_area_m2", 40.0))
    h = np.maximum(np.asarray(ndsm, dtype=np.float32), 0.0)
    local = median_filter(h, size=med_px)
    n_spike = int(np.sum(h > local + spike_m))
    h = np.where(h > local + spike_m, local, h)
    mask = h >= min_h
    labeled, nlab = label(mask)
    min_px = max(1, int(round(min_area / max(cell_m * cell_m, 1e-6))))
    dropped = 0
    if nlab:
        counts = np.bincount(labeled.ravel())
        keep_lbl = counts >= min_px
        keep_lbl[0] = False
        dropped = int(np.sum((counts[1:] > 0) & (counts[1:] < min_px)))
        mask = keep_lbl[labeled]
    out = np.where(mask, h, 0.0).astype(np.float32)
    print(
        f"canopy >={min_h:.0f}m: {100.0 * float(mask.mean()):.1f}% of cells  "
        f"max={float(out.max()):.1f}m  spikes={n_spike}  "
        f"tiny blobs dropped={dropped} (<{min_px} px / {min_area:.0f} m²)"
    )
    return out


def near_surface_with_canopy(
    dgm: np.ndarray,
    dgm_meta: dict,
    site: dict,
    cfg: dict,
    proc: Path,
) -> np.ndarray:
    """DTM + filtered nDSM. Same mesh; Z bumps make the forest ridgeline."""
    if not cfg.get("near_canopy", True):
        print("near canopy skip: near_canopy=false")
        return dgm
    pack = load_near_dom(site)
    if pack is None:
        print("near canopy skip: no near DOM (fetch sources.dom)")
        return dgm
    dom, dom_meta = pack
    dom_a = _align_to_grid(dom, dom_meta, dgm.shape, dgm_meta)
    both = np.isfinite(dgm) & np.isfinite(dom_a)
    ndsm = np.zeros(dgm.shape, dtype=np.float32)
    ndsm[both] = np.maximum(dom_a[both] - dgm[both], 0.0).astype(np.float32)
    cell = abs(float(dgm_meta["px"]))
    canopy = filter_near_canopy(ndsm, cell, cfg)
    hs, _ = _hillshade_slope(dgm, cell)
    grey = (np.clip(hs, 0, 1) * 180).astype(np.float32)
    prev = np.stack([grey, grey, grey], axis=-1)
    t = np.clip(canopy / 25.0, 0.0, 1.0)[..., None]
    tinted = (1.0 - 0.75 * t) * prev + 0.75 * t * np.array([40.0, 120.0, 50.0], dtype=np.float32)
    Image.fromarray(np.clip(tinted, 0, 255).astype(np.uint8), mode="RGB").resize(
        (min(dgm.shape[1], 1024), min(dgm.shape[0], 1024)),
        Image.Resampling.BILINEAR,
    ).save(proc / "preview_backdrop_near_canopy.png")
    print(f"Wrote {proc / 'preview_backdrop_near_canopy.png'}")
    return np.where(np.isfinite(dgm), dgm + canopy, dgm)


def mid_surface_with_canopy(
    elev: np.ndarray,
    elev_meta: dict,
    site: dict,
    cfg: dict,
    proc: Path,
) -> np.ndarray:
    """Copernicus DTM + filtered Tirol nDSM (0 outside AT coverage)."""
    if not cfg.get("mid_canopy", True):
        print("mid canopy skip: mid_canopy=false")
        return elev
    dgm_pack = load_mid_dgm(site)
    dom_pack = load_mid_dom(site)
    if dgm_pack is None or dom_pack is None:
        print("mid canopy skip: no mid DGM/DOM (fetch sources.dom)")
        return elev
    dgm, dgm_meta = dgm_pack
    dom, dom_meta = dom_pack
    if dom.shape != dgm.shape:
        dom = _align_to_grid(dom, dom_meta, dgm.shape, dgm_meta)
    both = np.isfinite(dgm) & np.isfinite(dom)
    ndsm = np.zeros(dgm.shape, dtype=np.float32)
    ndsm[both] = np.maximum(dom[both] - dgm[both], 0.0).astype(np.float32)
    cell = abs(float(dgm_meta["px"]))
    canopy = filter_near_canopy(ndsm, cell, cfg)
    aligned = _align_to_grid(canopy.astype(np.float64), dgm_meta, elev.shape, elev_meta)
    add = np.nan_to_num(aligned, nan=0.0).astype(np.float32)
    hs, _ = _hillshade_slope(elev, abs(float(elev_meta["px"])))
    grey = (np.clip(hs, 0, 1) * 180).astype(np.float32)
    prev = np.stack([grey, grey, grey], axis=-1)
    t = np.clip(add / 25.0, 0.0, 1.0)[..., None]
    tinted = (1.0 - 0.75 * t) * prev + 0.75 * t * np.array([40.0, 120.0, 50.0], dtype=np.float32)
    Image.fromarray(np.clip(tinted, 0, 255).astype(np.uint8), mode="RGB").resize(
        (min(elev.shape[1], 1024), min(elev.shape[0], 1024)),
        Image.Resampling.BILINEAR,
    ).save(proc / "preview_backdrop_mid_canopy.png")
    n_on = int((add >= 1.0).sum())
    print(
        f"Wrote {proc / 'preview_backdrop_mid_canopy.png'}  "
        f"mid nDSM>=1m on Copernicus={n_on} cells"
    )
    return np.where(np.isfinite(elev), elev + add, elev)


def crop_to_extent(
    elev: np.ndarray, meta: dict, extent: tuple[float, float, float, float]
) -> np.ndarray:
    xmin, ymin, xmax, ymax = extent
    px, py = float(meta["px"]), float(meta["py"])
    ox, oy = float(meta["origin_x"]), float(meta["origin_y"])

    def col(x: float) -> int:
        return int(round((x - ox) / px))

    def row(y: float) -> int:
        return int(round((y - oy) / py))

    c0, c1 = sorted((col(xmin), col(xmax)))
    r0, r1 = sorted((row(ymin), row(ymax)))
    c0 = int(np.clip(c0, 0, elev.shape[1] - 1))
    c1 = int(np.clip(c1, c0 + 1, elev.shape[1]))
    r0 = int(np.clip(r0, 0, elev.shape[0] - 1))
    r1 = int(np.clip(r1, r0 + 1, elev.shape[0]))
    return elev[r0:r1, c0:c1]


def _snow_compose_cfg(site: dict) -> tuple[dict | None, float, float]:
    """Same DGM proxy + biome thresholds as the playable map, or None."""
    if not ((site.get("sources") or {}).get("snow")):
        return None, 0.25, 0.55
    compose = ((site.get("beamng") or {}).get("compose") or {})
    light = float(compose.get("snow_light", 0.25))
    heavy = float(compose.get("snow_heavy", 0.55))
    return snow_proxy_cfg(site), light, heavy


def _snow_fraction(
    elev: np.ndarray,
    cell_m: float,
    snow_cfg: dict,
    wc: np.ndarray | None = None,
    no_snow: np.ndarray | None = None,
) -> np.ndarray:
    finite = np.isfinite(elev)
    if not finite.any():
        return np.zeros(elev.shape, dtype=np.float32)
    fill = float(np.nanmedian(elev))
    z = np.where(finite, elev, fill).astype(np.float64)
    snow, _, _ = compute_snow_proxy(z, float(cell_m), snow_cfg)
    snow = np.where(finite, snow, 0.0).astype(np.float32)
    if wc is not None and wc.shape == snow.shape:
        for code in _WC_NO_SNOW:
            snow = np.where(wc == code, 0.0, snow)
    if no_snow is not None and no_snow.shape == snow.shape:
        snow = np.where(no_snow, 0.0, snow)
    return snow


def _tint_snow(
    rgb: np.ndarray,
    elev: np.ndarray,
    cell_m: float,
    size: int,
    snow_cfg: dict | None,
    snow_light: float,
    snow_heavy: float,
    wc: np.ndarray | None,
    no_snow: np.ndarray | None = None,
) -> np.ndarray:
    """Lerp baked RGB toward biome snow colours using the DGM proxy."""
    if snow_cfg is None:
        return rgb
    snow_img = _zoom_hw(_snow_fraction(elev, cell_m, snow_cfg, wc, no_snow), size)
    lo = float(snow_light)
    hi = float(snow_heavy)
    t = np.clip((snow_img - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
    t = t * t * (3.0 - 2.0 * t)
    col = (1.0 - t)[..., None] * _SNOW_LIGHT_RGB + t[..., None] * _SNOW_HEAVY_RGB
    amount = np.where(snow_img >= lo, 0.22 + 0.73 * t, 0.0)[..., None]
    return np.clip((1.0 - amount) * rgb + amount * col, 0.0, 1.0)


# Sampled from the playable wedge (dry_meadow olive-brown) vs Swissimage green.
_PLAYABLE_OLIVE = np.array([120, 108, 64], dtype=np.float32) / 255.0
_SNOW_GRADE_RGB = np.array([245, 248, 255], dtype=np.float32) / 255.0


def _detect_green_w(rgb: np.ndarray) -> np.ndarray:
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    share = g / (r + g + b + 1e-5)
    return np.clip((share - (1.0 / 3.0)) / 0.03, 0.0, 1.0)


def _tint_class(
    rgb: np.ndarray, mask: np.ndarray, target: np.ndarray, mix: float, dark: float
) -> np.ndarray:
    graded = (1.0 - float(mix)) * rgb + float(mix) * target.astype(np.float32)
    graded = graded * (1.0 - float(np.clip(dark, 0.0, 1.0)))
    return np.where(mask[..., None], graded, rgb)


def _forest_olive_rgb(cfg_or_rgb) -> np.ndarray:
    raw = cfg_or_rgb
    if isinstance(raw, dict):
        raw = raw.get("forest_olive")
    if raw is None:
        return _PLAYABLE_OLIVE
    arr = np.asarray(list(raw)[:3], dtype=np.float32)
    if float(np.max(arr)) > 1.5:
        arr = arr / 255.0
    return arr


def _apply_detect_grade(
    rgb: np.ndarray,
    elev: np.ndarray,
    cell_m: float,
    snow_cfg: dict | None,
    snow_cut: float,
    snow_mix: float,
    grass_cut: float,
    grass_mix: float,
    grass_dark: float,
    forest_mask: np.ndarray | None = None,
    forest_mix: float | None = None,
    forest_dark: float | None = None,
    forest_olive: np.ndarray | None = None,
) -> np.ndarray:
    """Meadow olive, then BEV forest tint, snow on top."""
    is_meadow = _detect_green_w(rgb) >= float(grass_cut)
    forest = None
    if forest_mask is not None:
        forest = np.asarray(forest_mask, dtype=bool)
        if forest.shape[:2] != rgb.shape[:2]:
            forest = np.array(
                Image.fromarray(forest.astype(np.uint8) * 255, mode="L").resize(
                    (rgb.shape[1], rgb.shape[0]), Image.Resampling.NEAREST
                )
            ) > 127
        is_meadow = is_meadow & ~forest
    out = _tint_class(rgb, is_meadow, _PLAYABLE_OLIVE, grass_mix, grass_dark)
    if forest is not None:
        folive = forest_olive if forest_olive is not None else _PLAYABLE_OLIVE
        fmix = grass_mix if forest_mix is None else float(forest_mix)
        fdark = grass_dark if forest_dark is None else float(forest_dark)
        out = _tint_class(out, forest, folive, fmix, fdark)
    if snow_cfg is not None:
        snow = _zoom_hw(_snow_fraction(elev, cell_m, snow_cfg, None, None), rgb.shape[0])
        if snow.shape[:2] != rgb.shape[:2]:
            snow = np.array(
                Image.fromarray((np.clip(snow, 0, 1) * 255).astype(np.uint8)).resize(
                    (rgb.shape[1], rgb.shape[0]), Image.Resampling.BILINEAR
                ),
                dtype=np.float32,
            ) / 255.0
        sm = float(np.clip(snow_mix, 0.0, 1.0)) * (snow >= float(snow_cut))
        out = (1.0 - sm[..., None]) * out + sm[..., None] * _SNOW_GRADE_RGB
    return np.clip(out, 0.0, 1.0)


def _green_share_mask(rgb: np.ndarray) -> np.ndarray:
    """Soft 0–1: G share of RGB above gray (1/3). Rock/snow ~0, meadow/forest high."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    tot = r + g + b + 1e-5
    share = g / tot
    return np.clip((share - (1.0 / 3.0) - 0.010) / 0.075, 0.0, 1.0).astype(np.float32)


def _snow_block_map(
    elev: np.ndarray,
    cell_m: float,
    size: int,
    snow_cfg: dict | None,
    snow_light: float,
    wc: np.ndarray | None,
    no_snow: np.ndarray | None = None,
) -> np.ndarray:
    """1 where the DGM snow proxy reaches compose snow_light (soft below)."""
    if snow_cfg is None:
        return np.zeros((size, size), dtype=np.float32)
    snow = _zoom_hw(_snow_fraction(elev, cell_m, snow_cfg, wc, no_snow), size)
    return np.clip(snow / max(float(snow_light), 1e-4), 0.0, 1.0).astype(np.float32)


def _effect_mask(rgb: np.ndarray, snow_block: np.ndarray) -> np.ndarray:
    return (_green_share_mask(rgb) * (1.0 - snow_block)).astype(np.float32)


def _apply_base_color_masked(
    rgb: np.ndarray, base_color: list[float] | None, mask: np.ndarray
) -> np.ndarray:
    if not base_color:
        return rgb
    bc = np.asarray(base_color[:3], dtype=np.float32)
    if float(np.max(np.abs(bc - 1.0))) < 1e-3:
        return rgb
    m = mask.astype(np.float32)[..., None]
    tinted = np.clip(rgb * bc, 0.0, 1.0)
    return (1.0 - m) * rgb + m * tinted


def _grade_playable_olive(rgb: np.ndarray, mix: float, mask: np.ndarray) -> np.ndarray:
    w = mask.astype(np.float32) * float(mix)
    return np.clip((1.0 - w[..., None]) * rgb + w[..., None] * _PLAYABLE_OLIVE, 0.0, 1.0)


def _grade_ortho(rgb: np.ndarray, mode: str, mix: float, mask: np.ndarray) -> np.ndarray:
    mode = (mode or "none").strip().lower()
    if mode in ("playable_olive", "d"):
        return _grade_playable_olive(rgb, mix, mask)
    return rgb


def _bbox_lip_weight(
    elev: np.ndarray,
    meta: dict,
    size: int,
    bbox: tuple[float, float, float, float],
    fade_m: float,
) -> np.ndarray:
    """1 at the playable bbox edge, 0 by fade_m outside. Matches the near-ring seam."""
    h0, w0 = elev.shape
    px, py = float(meta["px"]), float(meta["py"])
    ox, oy = float(meta["origin_x"]), float(meta["origin_y"])
    xs = ox + np.linspace(0.5, w0 - 0.5, size, dtype=np.float64) * px
    ys = oy + np.linspace(0.5, h0 - 0.5, size, dtype=np.float64) * py
    xx, yy = np.meshgrid(xs, ys)
    bx0, by0, bx1, by1 = (float(v) for v in bbox)
    dx = np.maximum(bx0 - xx, 0.0) + np.maximum(xx - bx1, 0.0)
    dy = np.maximum(by0 - yy, 0.0) + np.maximum(yy - by1, 0.0)
    dist = np.sqrt(dx * dx + dy * dy)
    fade = max(float(fade_m), 1.0)
    return np.clip(1.0 - dist / fade, 0.0, 1.0).astype(np.float32)


def _grade_near_lip(
    rgb: np.ndarray, weight: np.ndarray, olive: float, darken: float, mask: np.ndarray
) -> np.ndarray:
    w = (weight * mask).astype(np.float32)[..., None]
    out = (1.0 - float(olive) * w) * rgb + float(olive) * w * _PLAYABLE_OLIVE
    return np.clip(out * (1.0 - float(darken) * w), 0.0, 1.0)


def _normal_map(elev: np.ndarray, cell_m: float, size: int, strength: float = 0.65) -> np.ndarray:
    """Tangent-space PNG (DirectX Y) from DEM slopes so ToD lights the ring like terrain."""
    fill = float(np.nanmedian(elev)) if np.isfinite(elev).any() else 0.0
    z = np.where(np.isfinite(elev), elev, fill).astype(np.float32)
    z = gaussian_filter(z, sigma=1.4)
    dz_dy, dz_dx = np.gradient(z, cell_m, cell_m)
    dz_dy = -dz_dy
    dx = _zoom_hw(dz_dx.astype(np.float32), size)
    dy = _zoom_hw(dz_dy.astype(np.float32), size)
    s = max(float(strength), 1e-4)
    nx = -dx * s
    ny = -dy * s
    nz = np.ones_like(nx)
    nlen = np.sqrt(nx * nx + ny * ny + nz * nz)
    nlen = np.maximum(nlen, 1e-6)
    nx, ny, nz = nx / nlen, ny / nlen, nz / nlen
    rgb = np.stack(
        [
            nx * 0.5 + 0.5,
            -ny * 0.5 + 0.5,
            nz * 0.5 + 0.5,
        ],
        axis=-1,
    )
    return (np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)


def bake_texture(
    elev: np.ndarray,
    wc: np.ndarray | None,
    cell_m: float,
    size: int,
    ortho: np.ndarray | None = None,
    albedo_gain: float = 1.0,
    snow_cfg: dict | None = None,
    snow_light: float = 0.25,
    snow_heavy: float = 0.55,
    ortho_grade: str = "none",
    ortho_hillshade: float = 0.10,
    ortho_gamma: float = 1.0,
    olive_mix: float = 0.68,
    elev_meta: dict | None = None,
    playable_bbox: tuple[float, float, float, float] | None = None,
    lip_fade_m: float = 0.0,
    lip_olive: float = 0.40,
    lip_darken: float = 0.12,
    base_color: list[float] | None = None,
    debug_green_black: bool = False,
    no_snow: np.ndarray | None = None,
    detect_grade: bool = False,
    grass_cut: float = 0.5,
    grass_mix: float = 0.4,
    grass_dark: float = 0.4,
    snow_cut: float = 0.50,
    snow_mix: float = 0.65,
    snow_elev: np.ndarray | None = None,
    forest_mask: np.ndarray | None = None,
    forest_mix: float | None = None,
    forest_dark: float | None = None,
    forest_olive: np.ndarray | None = None,
) -> np.ndarray:
    hs, steep = _hillshade_slope(elev, cell_m)
    tex_hs = _zoom_hw(hs.astype(np.float32), size)
    tex_z = _zoom_hw(np.nan_to_num(elev, nan=0.0).astype(np.float32), size)
    tex_st = _zoom_hw(steep.astype(np.float32), size)

    if ortho is not None:
        if ortho.shape[0] != size or ortho.shape[1] != size:
            ortho = np.array(
                Image.fromarray(ortho, mode="RGB").resize((size, size), Image.Resampling.BILINEAR)
            )
        rgb = ortho.astype(np.float32) / 255.0
        gamma = float(ortho_gamma)
        if abs(gamma - 1.0) > 1e-4:
            rgb = np.clip(rgb, 0.0, 1.0) ** gamma
        # Photo is already sunlit. Keep albedo almost flat so ToD + normals shade it;
        # albedo_gain still stops the engine sun from blowing the photo out.
        amp = float(np.clip(ortho_hillshade, 0.0, 1.0))
        shade = float(albedo_gain) * ((1.0 - amp) + amp * tex_hs[..., None])
        rgb = np.clip(rgb * shade, 0.0, 1.0)
        snow_block = _snow_block_map(elev, cell_m, size, snow_cfg, snow_light, wc, no_snow)
        if detect_grade:
            raw = ortho.astype(np.float32) / 255.0
            rgb = _apply_detect_grade(
                raw,
                snow_elev if snow_elev is not None else elev,
                cell_m,
                snow_cfg,
                snow_cut,
                snow_mix,
                grass_cut,
                grass_mix,
                grass_dark,
                forest_mask=forest_mask,
                forest_mix=forest_mix,
                forest_dark=forest_dark,
                forest_olive=forest_olive,
            )
            rgb = np.clip(rgb * shade, 0.0, 1.0)
            emask = np.zeros(rgb.shape[:2], dtype=np.float32)
        elif debug_green_black:
            r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
            share = g / (r + g + b + 1e-5)
            w = np.clip((share - (1.0 / 3.0)) / 0.03, 0.0, 1.0)
            rgb = rgb * (1.0 - w[..., None])
            emask = np.zeros(rgb.shape[:2], dtype=np.float32)
        else:
            emask = _effect_mask(rgb, snow_block)
            rgb = _grade_ortho(rgb, ortho_grade, olive_mix, emask)
            if (
                elev_meta is not None
                and playable_bbox is not None
                and float(lip_fade_m) > 0.0
            ):
                lip_w = _bbox_lip_weight(elev, elev_meta, size, playable_bbox, lip_fade_m)
                rgb = _grade_near_lip(rgb, lip_w, lip_olive, lip_darken, emask)
    else:
        lut = np.zeros((256, 3), dtype=np.float32)
        # default alpine grey-green
        lut[:] = (0.42, 0.44, 0.36)
        for code, rgb_wc in WC_RGB.items():
            lut[code] = (rgb_wc[0] / 255.0, rgb_wc[1] / 255.0, rgb_wc[2] / 255.0)
        if wc is not None:
            wc_img = Image.fromarray(wc, mode="L").resize((size, size), Image.Resampling.NEAREST)
            codes = np.array(wc_img)
            rgb = lut[codes]
        else:
            # Hypsometric: valley green → alpine rock. Snow comes from the proxy.
            z = tex_z
            t = np.clip((z - 800.0) / 1600.0, 0.0, 1.0)
            low = np.array([0.22, 0.32, 0.18], dtype=np.float32)
            mid = np.array([0.45, 0.46, 0.32], dtype=np.float32)
            high = np.array([0.55, 0.54, 0.50], dtype=np.float32)
            rgb = (1 - t)[..., None] * low + t[..., None] * mid
            t2 = np.clip((z - 2000.0) / 500.0, 0.0, 1.0)
            rgb = (1 - t2)[..., None] * rgb + t2[..., None] * high

        rock = np.array([0.52, 0.50, 0.47], dtype=np.float32)
        steep_w = np.clip((tex_st - 28.0) / 28.0, 0.0, 1.0)[..., None]
        rgb = (1.0 - 0.75 * steep_w) * rgb + 0.75 * steep_w * rock
        alp = np.clip((tex_z - 1750.0) / 650.0, 0.0, 1.0)[..., None]
        rgb = (1.0 - 0.35 * alp) * rgb + 0.35 * alp * rock
        if snow_cfg is None:
            snow = np.array([0.88, 0.90, 0.92], dtype=np.float32)
            sn = np.clip((tex_z - 2450.0) / 450.0, 0.0, 1.0)[..., None]
            rgb = (1.0 - sn) * rgb + sn * snow
        shade = 0.52 + 0.48 * tex_hs[..., None]
        rgb = np.clip(rgb * shade, 0.0, 1.0)
        snow_block = _snow_block_map(elev, cell_m, size, snow_cfg, snow_light, wc, no_snow)
        emask = _effect_mask(rgb, snow_block)

    if not detect_grade:
        rgb = _tint_snow(
            rgb, elev, cell_m, size, snow_cfg, snow_light, snow_heavy, wc, no_snow
        )
    rgb = _apply_base_color_masked(rgb, base_color, emask)
    return (rgb * 255.0).astype(np.uint8)


def _crs_to_beamng(sc: SiteCoords, x: float, y: float) -> tuple[float, float]:
    return sc.crs_to_beamng(x, y)


def build_meshes(
    elev: np.ndarray,
    keep: np.ndarray,
    meta: dict,
    sc: SiteCoords,
    z0: float,
    mesh_step_m: float,
    *,
    split_quadrants: bool = True,
    split_tiles: int | None = None,
    uv_bbox: tuple[float, float, float, float] | None = None,
    snap_inner_bbox: bool = False,
    hm_rel: np.ndarray | None = None,
    lip_drop_m: float = 0.0,
    z_order: int = 1,
) -> dict[str, tuple[list[tuple[float, float, float]], list[tuple[int, int, int]], list[tuple[float, float]]]]:
    """Quads assigned by face centre so N/S/E/W splits do not open radial gaps."""
    z, k, xs, ys = _world_sample_grid(elev, keep, meta, mesh_step_m, z_order=z_order)
    h, w = z.shape
    if uv_bbox is None:
        uv_xmin, uv_ymin, uv_xmax, uv_ymax = [float(v) for v in meta["bbox"]]
    else:
        uv_xmin, uv_ymin, uv_xmax, uv_ymax = uv_bbox
    span_x = max(uv_xmax - uv_xmin, 1.0)
    span_y = max(uv_ymax - uv_ymin, 1.0)
    cx, cy = site_center(sc.site)
    n_tiles = int(split_tiles) if split_tiles is not None else (2 if split_quadrants else 1)
    n_tiles = max(1, n_tiles)
    if n_tiles == 2:
        names: tuple[str, ...] = ("nw", "ne", "sw", "se")
    elif n_tiles == 1:
        names = ("all",)
    else:
        names = tuple(
            _grid_tile_name(n_tiles, ty, tx)
            for ty in range(n_tiles)
            for tx in range(n_tiles)
        )
    buckets: dict[str, dict] = {
        q: {
            "verts": [],
            "uvs": [],
            "faces": [],
            "index": np.full((h, w), -1, dtype=np.int32),
        }
        for q in names
    }

    def tile_of(x: float, y: float) -> str:
        if n_tiles == 1:
            return "all"
        if n_tiles == 2:
            ns = "n" if y >= cy else "s"
            ew = "e" if x >= cx else "w"
            return ns + ew
        tx = int(np.clip((x - uv_xmin) / span_x * n_tiles, 0, n_tiles - 1))
        ty = int(np.clip((y - uv_ymin) / span_y * n_tiles, 0, n_tiles - 1))
        return _grid_tile_name(n_tiles, ty, tx)

    def ensure_vert(q: str, r: int, c: int) -> int:
        b = buckets[q]
        idx = int(b["index"][r, c])
        if idx >= 0:
            return idx
        x, y = float(xs[c]), float(ys[r])
        z_abs = float(z[r, c])
        if snap_inner_bbox and _outside_bbox_m(x, y, sc.site) <= mesh_step_m * 1.25:
            x, y = _project_to_bbox_edge(x, y, sc.site)
            if hm_rel is not None:
                hm_abs = _hm_abs_at(hm_rel, sc, x, y, z0)
                z_abs = min(z_abs, hm_abs) - float(lip_drop_m)
        bx, by = _crs_to_beamng(sc, x, y)
        bz = z_abs - z0
        u = (x - uv_xmin) / span_x
        t = (y - uv_ymin) / span_y
        idx = len(b["verts"])
        b["index"][r, c] = idx
        b["verts"].append((bx, by, bz))
        b["uvs"].append((u, t))
        return idx

    for r in range(h - 1):
        for c in range(w - 1):
            corners = ((r, c), (r, c + 1), (r + 1, c + 1), (r + 1, c))  # NW NE SE SW
            if not all(k[rr, cc] and np.isfinite(z[rr, cc]) for rr, cc in corners):
                continue
            mx = 0.25 * sum(float(xs[cc]) for _rr, cc in corners)
            my = 0.25 * sum(float(ys[rr]) for rr, _cc in corners)
            q = tile_of(mx, my)
            nw = ensure_vert(q, r, c)
            ne = ensure_vert(q, r, c + 1)
            se = ensure_vert(q, r + 1, c + 1)
            sw = ensure_vert(q, r + 1, c)
            buckets[q]["faces"].append((nw, sw, se))
            buckets[q]["faces"].append((nw, se, ne))

    out: dict[str, tuple] = {}
    for q, b in buckets.items():
        if b["faces"]:
            out[q] = (b["verts"], b["faces"], b["uvs"])
            print(f"  mesh {q}: verts={len(b['verts'])} tris={len(b['faces'])}")
    return out  # type: ignore[return-value]


def write_collada(
    path: Path,
    verts: list[tuple[float, float, float]],
    faces: list[tuple[int, int, int]],
    uvs: list[tuple[float, float]],
    *,
    mat: str,
    stem: str,
    png_name: str,
) -> None:
    n_vert = len(verts)
    n_tri = len(faces)
    if n_vert > COLLADA_MAX_VERTS:
        raise SystemExit(
            f"{stem}: {n_vert} verts exceeds BeamNG 16-bit Collada limit "
            f"({COLLADA_MAX_VERTS}) — split the mesh"
        )
    lod = f"{stem}_a999"
    pos_vals = " ".join(f"{x:.3f} {y:.3f} {z:.3f}" for x, y, z in verts)
    uv_vals = " ".join(f"{u:.5f} {v:.5f}" for u, v in uvs)
    p_vals = " ".join(f"{a} {b} {c}" for a, b, c in faces)
    m = escape(mat)
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor><authoring_tool>beamng_autoroad build_backdrop</authoring_tool></contributor>
    <unit name="meter" meter="1"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_images>
    <image id="{m}-image" name="{m}-image">
      <init_from>{escape(png_name)}</init_from>
    </image>
  </library_images>
  <library_effects>
    <effect id="{m}-effect">
      <profile_COMMON>
        <newparam sid="{m}-surface"><surface type="2D"><init_from>{m}-image</init_from></surface></newparam>
        <newparam sid="{m}-sampler"><sampler2D><source>{m}-surface</source></sampler2D></newparam>
        <technique sid="common">
          <lambert>
            <diffuse><texture texture="{m}-sampler" texcoord="UVSET0"/></diffuse>
          </lambert>
        </technique>
      </profile_COMMON>
    </effect>
  </library_effects>
  <library_materials>
    <material id="{m}-material" name="{m}">
      <instance_effect url="#{m}-effect"/>
    </material>
  </library_materials>
  <library_geometries>
    <geometry id="{lod}-mesh" name="{lod}-mesh">
      <mesh>
        <source id="{lod}-mesh-positions">
          <float_array id="{lod}-mesh-positions-array" count="{n_vert * 3}">{pos_vals}</float_array>
          <technique_common>
            <accessor source="#{lod}-mesh-positions-array" count="{n_vert}" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{lod}-mesh-map-0">
          <float_array id="{lod}-mesh-map-0-array" count="{n_vert * 2}">{uv_vals}</float_array>
          <technique_common>
            <accessor source="#{lod}-mesh-map-0-array" count="{n_vert}" stride="2">
              <param name="S" type="float"/><param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{lod}-mesh-vertices">
          <input semantic="POSITION" source="#{lod}-mesh-positions"/>
        </vertices>
        <triangles material="{m}" count="{n_tri}">
          <input semantic="VERTEX" source="#{lod}-mesh-vertices" offset="0"/>
          <input semantic="TEXCOORD" source="#{lod}-mesh-map-0" offset="0" set="0"/>
          <p>{p_vals}</p>
        </triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="base00" name="base00" type="NODE">
        <node id="start01" name="start01" type="NODE">
          <node id="{lod}" name="{lod}" type="NODE">
            <instance_geometry url="#{lod}-mesh">
              <bind_material>
                <technique_common>
                  <instance_material symbol="{m}" target="#{m}-material">
                    <bind_vertex_input semantic="UVSET0" input_semantic="TEXCOORD" input_set="0"/>
                  </instance_material>
                </technique_common>
              </bind_material>
            </instance_geometry>
          </node>
        </node>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene>
    <instance_visual_scene url="#Scene"/>
  </scene>
</COLLADA>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-8", newline="\n")


def _tex_level_path(level_name: str, name: str) -> str:
    if level_name:
        return f"/levels/{level_name}/art/shapes/backdrop/{name}"
    return name


def write_material(
    path: Path,
    png_name: str,
    *,
    mat: str = MAT_NAME,
    normal_name: str | None = None,
    level_name: str = "",
    base_color: list[float] | None = None,
    detail_normal: str | None = None,
    detail_scale: list[float] | None = None,
) -> None:
    data: dict = {}
    if path.is_file() and path.stat().st_size:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    stage: dict = {
        "baseColorMap": _tex_level_path(level_name, png_name),
        "baseColorFactor": list(base_color or [1.0, 1.0, 1.0])[:3] + [1.0],
        "roughnessFactor": 0.88,
        "metallicFactor": 0.0,
        "emissiveFactor": [0.0, 0.0, 0.0],
    }
    if normal_name:
        stage["normalMap"] = _tex_level_path(level_name, normal_name)
    if detail_normal:
        stage["detailNormalMap"] = detail_normal
        stage["detailScale"] = list(detail_scale or [320.0, 320.0])
    data[mat] = {
        "name": mat,
        "mapTo": mat,
        "class": "Material",
        "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"autoroad:backdrop:mat:{mat}")),
        "Stages": [stage, {}, {}, {}],
        "annotation": "GRASS",
        "castShadows": False,
        "version": 1.5,
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _write_ring_maps(
    mesh_dir: Path,
    *,
    tex: np.ndarray,
    nrm: np.ndarray,
    tex_near: np.ndarray | None,
    nrm_near: np.ndarray | None,
    tex_mid: np.ndarray,
    nrm_mid: np.ndarray,
    level_name: str = "",
    material_color: list[float] | None = None,
    name_suffix: str = "",
) -> tuple[str, str, str]:
    png_name = f"backdrop_diffuse{name_suffix}.png"
    png_n = f"backdrop_diffuse_n{name_suffix}.png"
    png_near = f"backdrop_near{name_suffix}.png"
    png_near_n = f"backdrop_near_n{name_suffix}.png"
    png_mid = f"backdrop_mid{name_suffix}.png"
    png_mid_n = f"backdrop_mid_n{name_suffix}.png"
    mesh_dir.mkdir(parents=True, exist_ok=True)
    mats_path = mesh_dir / "main.materials.json"
    if mats_path.is_file():
        mats_path.unlink()
    Image.fromarray(tex, mode="RGB").save(mesh_dir / png_name)
    Image.fromarray(nrm, mode="RGB").save(mesh_dir / png_n)
    write_material(
        mats_path,
        png_name,
        mat=MAT_NAME,
        normal_name=png_n,
        level_name=level_name,
        base_color=material_color,
    )
    if tex_near is not None and nrm_near is not None:
        Image.fromarray(tex_near, mode="RGB").save(mesh_dir / png_near)
        Image.fromarray(nrm_near, mode="RGB").save(mesh_dir / png_near_n)
        write_material(
            mats_path,
            png_near,
            mat=MAT_NEAR,
            normal_name=png_near_n,
            level_name=level_name,
            base_color=material_color,
            detail_normal="/assets/materials/terrain/grass/ind_grass/t_ind_grass_nm.png",
            detail_scale=[320.0, 320.0],
        )
    Image.fromarray(tex_mid, mode="RGB").save(mesh_dir / png_mid)
    Image.fromarray(nrm_mid, mode="RGB").save(mesh_dir / png_mid_n)
    write_material(
        mats_path, png_mid, mat=MAT_MID, normal_name=png_mid_n, level_name=level_name, base_color=material_color
    )
    return png_name, png_near, png_mid


def _tex_suffix(cfg: dict) -> str:
    """New PNG names bust the engine filename cache."""
    if cfg.get("detect_grade"):
        src = "b" if str(cfg.get("forest_detect") or "green") == "bev" else "g"
        return (
            f"_m{int(round(float(cfg.get('grass_mix', 0.4)) * 100)):02d}"
            f"d{int(round(float(cfg.get('grass_dark', 0.4)) * 100)):02d}"
            f"s{int(round(float(cfg.get('snow_cut', 0.5)) * 100)):02d}"
            f"{src}"
            f"{int(round(float(cfg.get('forest_mix', 0.55)) * 100)):02d}"
            f"{int(round(float(cfg.get('forest_dark', 0.28)) * 100)):02d}"
        )
    bits = []
    if cfg.get("debug_green_black"):
        bits.append("g2b")
    if not cfg.get("snow_tint", True):
        bits.append("ns")
    return "_" + "".join(bits) if bits else ""


def _retarget_dae_pngs(mesh_dir: Path, suffix: str) -> None:
    """Point existing Collada init_from at the current albedo/normal filenames."""
    pat = re.compile(r"(backdrop_(?:near|mid|diffuse)(?:_n)?)(?:_[A-Za-z0-9]+)?\.png")
    n = 0
    for path in mesh_dir.glob("backdrop*.dae"):
        txt = path.read_text(encoding="utf-8")
        nxt = pat.sub(lambda m: f"{m.group(1)}{suffix}.png", txt)
        if nxt != txt:
            path.write_text(nxt, encoding="utf-8", newline="\n")
            n += 1
    print(f"Retargeted {n} DAEs to *{suffix}.png")


def _stamp_dae_cache(mesh_dir: Path, stamp: str) -> None:
    marker = f"<!-- autoroad {stamp} -->"
    n = 0
    for path in mesh_dir.glob("backdrop*.dae"):
        txt = path.read_text(encoding="utf-8")
        start = txt.find("<!-- autoroad ")
        if start >= 0:
            end = txt.find("-->", start)
            if end >= 0:
                txt = txt[:start] + marker + txt[end + 3 :]
            else:
                txt = txt.replace("</COLLADA>", f"  {marker}\n</COLLADA>")
        else:
            txt = txt.replace("</COLLADA>", f"  {marker}\n</COLLADA>")
        path.write_text(txt, encoding="utf-8", newline="\n")
        n += 1
    print(f"Stamped {n} DAEs cache={stamp}")


def _copy_backdrop_textures(level_name: str, mesh_dir: Path, *, also_dae: bool = False) -> None:
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        print(f"Level folder missing: {user_level} - maps in processed only")
        return
    art = user_level / "art" / "shapes" / "backdrop"
    art.mkdir(parents=True, exist_ok=True)
    n = 0
    suffixes = {".png", ".json", ".dae"} if also_dae else {".png", ".json"}
    for src in mesh_dir.iterdir():
        if src.suffix.lower() in suffixes:
            (art / src.name).write_bytes(src.read_bytes())
            n += 1
    print(f"Copied {n} backdrop files -> {art}")


def _read_ndjson(path: Path) -> list[dict]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    rows = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return rows


def _write_ndjson(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")


def ensure_visible_distance(level_name: str, need_m: float) -> None:
    path = (
        USER_LEVELS
        / level_name
        / "main"
        / "MissionGroup"
        / "level_objects"
        / "sky_and_sun"
        / "items.level.json"
    )
    if not path.is_file():
        print(f"No LevelInfo at {path}")
        return
    want = max(7500.0, float(need_m) * 1.12)
    rows = _read_ndjson(path)
    changed = False
    for row in rows:
        if row.get("class") != "LevelInfo":
            continue
        cur = float(row.get("visibleDistance") or 0)
        if want > cur + 1:
            row["visibleDistance"] = round(want, 1)
            changed = True
            print(f"{level_name}: visibleDistance {cur:.0f} -> {want:.0f} m")
    if changed:
        _write_ndjson(path, rows)


def inject_backdrop(level_name: str, entries: list[dict]) -> Path | None:
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        print(f"Level folder missing: {user_level}")
        return None
    group_dir = user_level / "main" / "MissionGroup" / "level_objects" / "backdrop"
    _write_ndjson(group_dir / "items.level.json", entries)
    lo_items = user_level / "main" / "MissionGroup" / "level_objects" / "items.level.json"
    rows = _read_ndjson(lo_items)
    names = {r.get("name") for r in rows}
    if "backdrop" not in names:
        rows.append(
            {
                "name": "backdrop",
                "class": "SimGroup",
                "__parent": "level_objects",
                "enabled": "1",
                "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "autoroad:backdrop:group")),
            }
        )
        _write_ndjson(lo_items, rows)
        print("Registered SimGroup backdrop under level_objects")
    return group_dir / "items.level.json"


def _ts_entry(level_name: str, stem: str, pid_key: str) -> dict:
    return {
        "name": stem,
        "class": "TSStatic",
        "__parent": "backdrop",
        "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, pid_key)),
        "position": [0, 0, 0],
        "rotationMatrix": IDENTITY_ROT,
        "scale": [1.0, 1.0, 1.0],
        "shapeName": f"/levels/{level_name}/art/shapes/backdrop/{stem}.dae",
        "collisionType": "None",
        "decalType": "None",
        "castShadows": False,
        "useInstanceRenderData": True,
        "isRenderEnabled": True,
    }


def write_previews(
    proc: Path,
    elev: np.ndarray,
    count: np.ndarray,
    keep_far: np.ndarray,
    keep_near: np.ndarray,
    meta: dict,
    site: dict,
    observers: list[tuple[float, float]],
    tex: np.ndarray,
    tex_near: np.ndarray | None = None,
    keep_mid: np.ndarray | None = None,
    tex_mid: np.ndarray | None = None,
) -> None:
    hs, _ = _hillshade_slope(elev, abs(float(meta["px"])))
    grey = (np.clip(hs, 0, 1) * 200).astype(np.uint8)
    rgb = np.stack([grey, grey, grey], axis=-1)
    vis = count > 0
    rgb[vis] = (rgb[vis] * 0.55 + np.array([40, 140, 70]) * 0.45).astype(np.uint8)
    rgb[keep_far] = (rgb[keep_far] * 0.35 + np.array([70, 170, 90]) * 0.65).astype(np.uint8)
    if keep_mid is not None:
        rgb[keep_mid] = (rgb[keep_mid] * 0.3 + np.array([200, 160, 50]) * 0.7).astype(np.uint8)
    rgb[keep_near] = (rgb[keep_near] * 0.25 + np.array([70, 200, 230]) * 0.75).astype(np.uint8)
    img = Image.fromarray(rgb, mode="RGB")
    draw = ImageDraw.Draw(img)
    h, w = elev.shape
    px, py = float(meta["px"]), float(meta["py"])

    def to_px(x: float, y: float) -> tuple[int, int]:
        c = (x - float(meta["origin_x"])) / px
        r = (y - float(meta["origin_y"])) / py
        return int(c), int(r)

    bx0, by0, bx1, by1 = map(float, site["bbox"])
    corners = [to_px(bx0, by1), to_px(bx1, by1), to_px(bx1, by0), to_px(bx0, by0)]
    draw.polygon(corners, outline=(220, 40, 40))
    for x, y in observers:
        cx, cy = to_px(x, y)
        draw.ellipse((cx - 2, cy - 2, cx + 2, cy + 2), fill=(255, 220, 40))
    img.resize((min(w, 1024), min(h, 1024)), Image.Resampling.BILINEAR).save(
        proc / "preview_backdrop_viewshed.png"
    )
    Image.fromarray(tex, mode="RGB").save(proc / "preview_backdrop_diffuse.png")
    if tex_near is not None:
        Image.fromarray(tex_near, mode="RGB").save(proc / "preview_backdrop_near.png")
    if tex_mid is not None:
        Image.fromarray(tex_mid, mode="RGB").save(proc / "preview_backdrop_mid.png")
    Image.fromarray((np.clip(count.astype(np.float32) / max(int(count.max()), 1), 0, 1) * 255).astype(np.uint8)).save(
        proc / "preview_backdrop_count.png"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-inject", action="store_true")
    ap.add_argument("--mesh-step", type=float, default=None)
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--skip-worldcover", action="store_true")
    ap.add_argument("--skip-ortho", action="store_true")
    ap.add_argument("--skip-near-dgm", action="store_true")
    ap.add_argument(
        "--textures-only",
        action="store_true",
        help="Rebake PNG/materials only (no viewshed or mesh). Faster lighting iteration.",
    )
    args = ap.parse_args()

    site = load_site()
    sc = SiteCoords(site)
    cfg = backdrop_cfg(site)
    if args.mesh_step is not None:
        cfg["mesh_step_m"] = float(args.mesh_step)
    level_name = str((site.get("beamng") or {}).get("level_name") or "").strip()
    if not level_name:
        raise SystemExit("beamng.level_name missing")
    proc = processed_dir(site)
    print(f"Site: {site_slug(site)}  level={level_name}")

    tif, _meta_p = dem_paths(site)
    if not args.skip_fetch or not tif.is_file():
        fetch_backdrop(
            site,
            skip_worldcover=args.skip_worldcover,
            skip_ortho=args.skip_ortho,
            skip_near_dgm=args.skip_near_dgm,
        )
    else:
        if not args.skip_near_dgm:
            fetch_near_dgm(site)
            fetch_near_dom(site)
            if cfg.get("mid_canopy", True):
                fetch_mid_dgm(site)
                fetch_mid_dom(site)
        if not args.skip_ortho:
            fetch_swissimage_ortho(
                site,
                dest=near_ortho_path(site),
                size=int(cfg["near_texture_size"]),
                extent=near_extent(site),
                label="Swissimage near",
            )
            fetch_swissimage_ortho(
                site,
                dest=mid_ortho_path(site),
                size=int(cfg["mid_texture_size"]),
                extent=mid_extent(site),
                label="Swissimage mid",
            )

    elev, meta = load_backdrop_dem(site)
    z0 = _heightmap_z0(site)
    print(f"DEM {elev.shape} z={np.nanmin(elev):.1f}..{np.nanmax(elev):.1f}  playable z0={z0:.2f}")

    if args.textures_only:
        count = np.zeros(elev.shape, dtype=np.uint16)
        observers = []
        keep_far = np.zeros(elev.shape, dtype=bool)
        keep_mid = np.zeros(elev.shape, dtype=bool)
        keep_near = np.zeros(elev.shape, dtype=bool)
        print("textures-only: skip viewshed")
    else:
        count, observers = compute_viewshed(elev, meta, site, cfg)
        keep_far = keep_far_mask(elev, count, meta, site, cfg)
        keep_mid = keep_mid_mask(elev, meta, site, cfg)
        keep_near = keep_near_mask(elev, meta, site, cfg)
        print(
            f"far keep={int(keep_far.sum())}/{keep_far.size} "
            f"({100.0 * keep_far.mean():.1f}%)  "
            f"mid keep={int(keep_mid.sum())}  "
            f"near keep={int(keep_near.sum())}  "
            f"near={cfg['near_m']:.0f}m mid={cfg['mid_m']:.0f}m"
        )

    wc = None
    wc_p = worldcover_path(site)
    if wc_p.is_file() and not args.skip_worldcover:
        import tifffile as tiff

        wc = np.asarray(tiff.imread(wc_p))
        if wc.shape != elev.shape:
            wc = np.array(Image.fromarray(wc).resize((elev.shape[1], elev.shape[0]), Image.Resampling.NEAREST))
        print(f"WorldCover classes on grid: {(wc > 0).sum()}")

    ortho = None
    op = ortho_path(site)
    if op.is_file() and not args.skip_ortho:
        ortho = np.asarray(Image.open(op).convert("RGB"))
        print(f"Ortho {op.name} {ortho.shape}")
    ortho_near = None
    nop = near_ortho_path(site)
    if nop.is_file() and not args.skip_ortho:
        ortho_near = np.asarray(Image.open(nop).convert("RGB"))
        print(f"Ortho near {nop.name} {ortho_near.shape}")
    ortho_mid = None
    mop = mid_ortho_path(site)
    if mop.is_file() and not args.skip_ortho:
        ortho_mid = np.asarray(Image.open(mop).convert("RGB"))
        print(f"Ortho mid {mop.name} {ortho_mid.shape}")

    snow_cfg, snow_light, snow_heavy = _snow_compose_cfg(site)
    if not cfg.get("snow_tint", True) and not cfg.get("detect_grade"):
        snow_cfg = None
        print("backdrop snow tint OFF")
    cell_m = abs(float(meta["px"]))
    if snow_cfg is not None:
        snow_raw = _snow_fraction(elev, cell_m, snow_cfg, None)
        snow_far = _snow_fraction(elev, cell_m, snow_cfg, wc)
        print(
            f"backdrop snow preset={snow_cfg['preset']}  "
            f"light={snow_light:.2f} heavy={snow_heavy:.2f}  "
            f"far raw>=light={100.0 * float((snow_raw >= snow_light).mean()):.1f}%  "
            f"wc-excl>=light={100.0 * float((snow_far >= snow_light).mean()):.1f}%  "
            f">=heavy={100.0 * float((snow_far >= snow_heavy).mean()):.1f}%"
        )
        hs_s, _ = _hillshade_slope(elev, cell_m)
        grey = (np.clip(hs_s, 0, 1) * 180).astype(np.float32)
        prev = np.stack([grey, grey, grey], axis=-1)
        t = np.clip(snow_far, 0.0, 1.0)[..., None]
        snow_col = np.array([245.0, 248.0, 255.0], dtype=np.float32)
        tinted = (1.0 - 0.85 * t) * prev + 0.85 * t * snow_col
        Image.fromarray(np.clip(tinted, 0, 255).astype(np.uint8), mode="RGB").resize(
            (min(elev.shape[1], 1024), min(elev.shape[0], 1024)),
            Image.Resampling.BILINEAR,
        ).save(proc / "preview_backdrop_snow.png")
        print(f"Wrote {proc / 'preview_backdrop_snow.png'}")
    else:
        print("backdrop snow skip: no sources.snow")

    bake_kw = dict(
        snow_cfg=snow_cfg,
        snow_light=snow_light,
        snow_heavy=snow_heavy,
        albedo_gain=float(cfg["albedo_gain"]),
        ortho_grade=str(cfg.get("ortho_grade") or "none"),
        ortho_hillshade=float(cfg.get("ortho_hillshade", 0.10)),
        ortho_gamma=float(cfg.get("ortho_gamma", 1.0)),
        olive_mix=float(cfg.get("olive_mix", 0.68)),
        base_color=list(cfg.get("base_color") or [1.0, 1.0, 1.0]),
        debug_green_black=bool(cfg.get("debug_green_black", False)),
        detect_grade=bool(cfg.get("detect_grade", False)),
        grass_cut=float(cfg.get("grass_cut", 0.5)),
        grass_mix=float(cfg.get("grass_mix", 0.4)),
        grass_dark=float(cfg.get("grass_dark", 0.4)),
        snow_cut=float(cfg.get("snow_cut", 0.50)),
        snow_mix=float(cfg.get("snow_mix", 0.65)),
        forest_mix=float(cfg.get("forest_mix", cfg.get("grass_mix", 0.55))),
        forest_dark=float(cfg.get("forest_dark", cfg.get("grass_dark", 0.28))),
        forest_olive=_forest_olive_rgb(cfg),
    )
    use_bev = bool(cfg.get("detect_grade")) and str(cfg.get("forest_detect") or "green") == "bev"
    if use_bev:
        from fetch_bev_landcover import load_bev_forest_mask  # noqa: WPS433

        print("detect-grade forest class = BEV hoch+mittel")
    nrm_strength = float(cfg.get("normal_strength", 0.65))
    print(
        f"ortho grade={bake_kw['ortho_grade']} mix={bake_kw['olive_mix']:.2f}  "
        f"gain={float(cfg['albedo_gain']):.2f} gamma={bake_kw['ortho_gamma']:.2f}  "
        f"hillshade={bake_kw['ortho_hillshade']:.2f}  "
        f"normal={nrm_strength:.2f} near_n={int(cfg.get('near_normal_size') or cfg['near_texture_size'])}  "
        f"lip={float(cfg.get('lip_fade_m', 800.0)):.0f}m"
        + ("  DEBUG green->black" if bake_kw.get("debug_green_black") else "")
        + (
            f"  detect-grade forest={cfg.get('forest_detect', 'green')} "
            f"mix={bake_kw['grass_mix']:.2f}/{bake_kw['grass_dark']:.2f} "
            f"snow-cut={bake_kw['snow_cut']:.2f} snow-mix={bake_kw['snow_mix']:.2f}"
            if bake_kw.get("detect_grade")
            else ""
        )
    )
    far_forest = (
        load_bev_forest_mask(site, (int(cfg["texture_size"]), int(cfg["texture_size"])), padded_extent(site))
        if use_bev
        else None
    )
    tex = bake_texture(
        elev,
        wc,
        cell_m,
        int(cfg["texture_size"]),
        ortho=ortho,
        forest_mask=far_forest,
        **bake_kw,
    )
    nrm = _normal_map(elev, cell_m, int(cfg["texture_size"]), nrm_strength)
    near_pack = None if args.skip_near_dgm else load_near_dgm(site)
    tex_near = None
    nrm_near = None
    near_meshes: dict = {}
    keep_near_mesh = None
    if near_pack is None:
        print("near LOD skip: no Tirol DGM (outside coverage or fetch failed)")
    else:
        near_elev, near_meta = near_pack
        keep_near_mesh = keep_near_mask(near_elev, near_meta, site, cfg)
        near_surf = near_surface_with_canopy(near_elev, near_meta, site, cfg, proc)
        hm_n = int((site.get("beamng") or {}).get("mask_size") or 0)
        hm_rel, _max_h, hm_label = load_composed_or_dgm(processed_dir(site), hm_n)
        print(
            f"near DGM {near_elev.shape} keep={int(keep_near_mesh.sum())}/"
            f"{keep_near_mesh.size} "
            f"z={float(np.nanmin(near_elev)):.1f}..{float(np.nanmax(near_elev)):.1f} "
            f"surf={float(np.nanmin(near_surf)):.1f}..{float(np.nanmax(near_surf)):.1f} "
            f"lip={hm_label}"
        )
        near_wc = (
            _align_classes(wc, meta, near_elev.shape, near_meta) if wc is not None else None
        )
        canopy_m = np.nan_to_num(near_surf.astype(np.float32) - near_elev.astype(np.float32), nan=0.0)
        no_snow_near = canopy_m >= 1.0
        if near_wc is not None:
            print(
                f"near snow exclude  wc10/20/80="
                f"{int(((near_wc == 10) | (near_wc == 20) | (near_wc == 80)).sum())}  "
                f"canopy>=1m={int(no_snow_near.sum())}"
            )
        near_sz = int(cfg["near_texture_size"])
        near_forest = (
            load_bev_forest_mask(site, (near_sz, near_sz), near_extent(site)) if use_bev else None
        )
        tex_near = bake_texture(
            near_surf,
            near_wc,
            abs(float(near_meta["px"])),
            near_sz,
            ortho=ortho_near if ortho_near is not None else ortho,
            elev_meta=near_meta,
            playable_bbox=tuple(float(v) for v in site["bbox"]),
            lip_fade_m=float(cfg.get("lip_fade_m", 800.0)),
            lip_olive=float(cfg.get("lip_olive", 0.40)),
            lip_darken=float(cfg.get("lip_darken", 0.12)),
            no_snow=no_snow_near,
            snow_elev=near_elev,
            forest_mask=near_forest,
            **bake_kw,
        )
        if cfg.get("debug_lip_stripe"):
            lip_w = _bbox_lip_weight(
                near_surf,
                near_meta,
                tex_near.shape[0],
                tuple(float(v) for v in site["bbox"]),
                120.0,
            )
            tex_near = tex_near.copy()
            tex_near[lip_w >= 0.35] = (220, 0, 180)
            print("debug lip stripe ON (magenta) — texture-load test")
        nrm_near = _normal_map(
            near_surf,
            abs(float(near_meta["px"])),
            int(cfg.get("near_normal_size") or cfg["near_texture_size"]),
            nrm_strength,
        )
    mid_surf = mid_surface_with_canopy(elev, meta, site, cfg, proc)
    mid_elev = crop_to_extent(mid_surf, meta, mid_extent(site))
    mid_wc = crop_to_extent(wc, meta, mid_extent(site)) if wc is not None else None
    mid_sz = int(cfg["mid_texture_size"])
    mid_forest = (
        load_bev_forest_mask(site, (mid_sz, mid_sz), mid_extent(site)) if use_bev else None
    )
    tex_mid = bake_texture(
        mid_elev,
        mid_wc,
        cell_m,
        mid_sz,
        ortho=ortho_mid if ortho_mid is not None else ortho,
        forest_mask=mid_forest,
        **bake_kw,
    )
    nrm_mid = _normal_map(mid_elev, cell_m, int(cfg["mid_texture_size"]), nrm_strength)
    if args.textures_only:
        Image.fromarray(tex, mode="RGB").save(proc / "preview_backdrop_diffuse.png")
        if tex_near is not None:
            Image.fromarray(tex_near, mode="RGB").save(proc / "preview_backdrop_near.png")
        if nrm_near is not None:
            Image.fromarray(nrm_near, mode="RGB").save(proc / "preview_backdrop_near_n.png")
        Image.fromarray(tex_mid, mode="RGB").save(proc / "preview_backdrop_mid.png")
        mesh_dir = proc / "backdrop_meshes"
        _write_ring_maps(
            mesh_dir,
            tex=tex,
            nrm=nrm,
            tex_near=tex_near,
            nrm_near=nrm_near,
            tex_mid=tex_mid,
            nrm_mid=nrm_mid,
            level_name=level_name,
            material_color=[1.0, 1.0, 1.0],
            name_suffix=_tex_suffix(cfg),
        )
        stamp = (
            f"gain{float(cfg['albedo_gain']):.2f}"
            f"_g{float(cfg.get('ortho_gamma', 1.0)):.2f}"
            f"_n{int(cfg.get('near_normal_size') or cfg['near_texture_size'])}"
            f"{_tex_suffix(cfg)}"
        )
        _retarget_dae_pngs(mesh_dir, _tex_suffix(cfg))
        _stamp_dae_cache(mesh_dir, stamp)
        if args.no_inject:
            print("Skipped level inject (--no-inject)")
            return
        _copy_backdrop_textures(level_name, mesh_dir, also_dae=True)
        print("Backdrop textures+DAE retargeted (no remesh).")
        return
    write_previews(
        proc,
        elev,
        count,
        keep_far,
        keep_near,
        meta,
        site,
        observers,
        tex,
        tex_near,
        keep_mid=keep_mid,
        tex_mid=tex_mid,
    )

    print(f"Meshing far step={cfg['mesh_step_m']} m ...")
    meshes = build_meshes(elev, keep_far, meta, sc, z0, float(cfg["mesh_step_m"]))
    if keep_near_mesh is not None:
        print(
            f"Meshing near step={cfg['near_step_m']} m cubic spline "
            f"(Tirol DGM+canopy, lip drop, tiles={int(cfg['near_tiles'])}) ..."
        )
        near_meshes = build_meshes(
            near_surf,
            keep_near_mesh,
            near_meta,
            sc,
            z0,
            float(cfg["near_step_m"]),
            split_tiles=int(cfg["near_tiles"]),
            uv_bbox=near_extent(site),
            snap_inner_bbox=True,
            hm_rel=hm_rel,
            lip_drop_m=float(cfg["near_lip_drop_m"]),
            z_order=3,
        )
        for q, (verts, _faces, _uvs) in near_meshes.items():
            if len(verts) > COLLADA_MAX_VERTS:
                print(
                    f"  WARN {q}: {len(verts)} verts > {COLLADA_MAX_VERTS} "
                    "(raise near_tiles or near_step_m)"
                )
    print(f"Meshing mid step={cfg['mid_step_m']} m (Copernicus+nDSM, always-keep) ...")
    mid_meshes = build_meshes(
        mid_surf,
        keep_mid,
        meta,
        sc,
        z0,
        float(cfg["mid_step_m"]),
        split_tiles=int(cfg["mid_tiles"]),
        uv_bbox=mid_extent(site),
    )
    if not meshes and not near_meshes and not mid_meshes:
        raise SystemExit("No backdrop triangles - viewshed mask empty?")

    mesh_dir = proc / "backdrop_meshes"
    mesh_dir.mkdir(parents=True, exist_ok=True)
    for leftover in mesh_dir.glob("backdrop_near_*.dae"):
        leftover.unlink()
    for leftover in mesh_dir.glob("backdrop_mid_*.dae"):
        leftover.unlink()
    png_name, png_near, png_mid = _write_ring_maps(
        mesh_dir,
        level_name=level_name,
        tex=tex,
        nrm=nrm,
        tex_near=tex_near,
        nrm_near=nrm_near,
        tex_mid=tex_mid,
        nrm_mid=nrm_mid,
        material_color=[1.0, 1.0, 1.0],
        name_suffix=_tex_suffix(cfg),
    )

    ts_entries = []
    for q, (verts, faces, uvs) in meshes.items():
        stem = f"backdrop_{q}"
        dae = mesh_dir / f"{stem}.dae"
        write_collada(dae, verts, faces, uvs, mat=MAT_NAME, stem=stem, png_name=png_name)
        print(f"Wrote {dae} ({dae.stat().st_size / 1e6:.2f} MB)")
        ts_entries.append(_ts_entry(level_name, stem, f"autoroad:backdrop:{q}"))
    for q, (verts, faces, uvs) in near_meshes.items():
        stem = f"backdrop_near_{q}"
        dae = mesh_dir / f"{stem}.dae"
        write_collada(dae, verts, faces, uvs, mat=MAT_NEAR, stem=stem, png_name=png_near)
        print(f"Wrote {dae} ({dae.stat().st_size / 1e6:.2f} MB)")
        ts_entries.append(_ts_entry(level_name, stem, f"autoroad:backdrop:near:{q}"))
    for q, (verts, faces, uvs) in mid_meshes.items():
        stem = f"backdrop_mid_{q}"
        dae = mesh_dir / f"{stem}.dae"
        write_collada(dae, verts, faces, uvs, mat=MAT_MID, stem=stem, png_name=png_mid)
        print(f"Wrote {dae} ({dae.stat().st_size / 1e6:.2f} MB)")
        ts_entries.append(_ts_entry(level_name, stem, f"autoroad:backdrop:mid:{q}"))

    meta_out = {
        "site": site_slug(site),
        "level_name": level_name,
        "z0_m": z0,
        "observers": len(observers),
        "keep_far_cells": int(keep_far.sum()),
        "keep_mid_cells": int(keep_mid.sum()),
        "keep_near_cells": int(keep_near_mesh.sum()) if keep_near_mesh is not None else 0,
        "quadrants": {q: {"verts": len(v), "tris": len(f)} for q, (v, f, _u) in meshes.items()},
        "mid": {q: {"verts": len(v), "tris": len(f)} for q, (v, f, _u) in mid_meshes.items()},
        "near": {
            q: {"verts": len(v), "tris": len(f)} for q, (v, f, _u) in near_meshes.items()
        },
        "texture": (
            "SWISSIMAGE Hintergrund"
            if ortho is not None
            else "WorldCover+tint" if wc is not None else "elevation/slope tint"
        ),
        "source_dem": "copernicus_glo30",
        "source_near_dem": "tirol_wcs_dgm" if near_pack is not None else None,
        "cfg": cfg,
    }
    (proc / "backdrop_meta.json").write_text(json.dumps(meta_out, indent=2), encoding="utf-8")

    if args.no_inject:
        print("Skipped level inject (--no-inject)")
        return

    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        print(f"Level folder missing: {user_level} - meshes in processed only")
        return
    art = user_level / "art" / "shapes" / "backdrop"
    art.mkdir(parents=True, exist_ok=True)
    keep_art = {src.name for src in mesh_dir.iterdir()}
    for dest in list(art.glob("backdrop_near*.dae")) + list(art.glob("backdrop_mid*.dae")):
        if dest.name not in keep_art:
            dest.unlink()
    for src in mesh_dir.iterdir():
        if src.suffix.lower() in {".dae", ".png", ".json"}:
            (art / src.name).write_bytes(src.read_bytes())
    inject_backdrop(level_name, ts_entries)
    ensure_visible_distance(level_name, float(cfg["radius_m"]) + 0.6 * sc.terrain_extent)
    print(f"Injected {len(ts_entries)} backdrop TSStatics -> {level_name}")
    print("Quit BeamNG fully and restart (do not save over items.level.json from an old session).")


if __name__ == "__main__":
    main()
