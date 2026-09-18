"""Build a far-field backdrop mesh from Copernicus DEM + viewshed mask.

POC: 5x5 (+ local peaks) viewshed with earth curvature, cut invisible cells
and the playable bbox, four quadrant Collada TSStatics, WorldCover or
elevation/slope tint, no collision. Near ring uses Tirol DGM; cells without
coverage stay empty (no Copernicus fill).

Usage:
  cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = \"config/sites/fernpass_mega.yaml\"; python tools\\build_backdrop.py
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import binary_dilation, map_coordinates, zoom

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from fetch_backdrop import (  # noqa: E402
    backdrop_cfg,
    dem_paths,
    fetch_backdrop,
    fetch_near_dgm,
    fetch_swissimage_ortho,
    load_backdrop_dem,
    load_near_dgm,
    mid_extent,
    mid_ortho_path,
    near_extent,
    near_ortho_path,
    ortho_path,
    site_center,
    worldcover_path,
)
from site_coords import SiteCoords, load_site, processed_dir, site_slug  # noqa: E402
from heightmap_layers import load_composed_or_dgm  # noqa: E402

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


def keep_far_mask(
    elev: np.ndarray, count: np.ndarray, meta: dict, site: dict, cfg: dict
) -> np.ndarray:
    min_views = max(1, int(cfg["min_views"]))
    keep = np.isfinite(elev) & (count >= min_views)
    keep = binary_dilation(keep, iterations=4)
    cx, cy = site_center(site)
    radius = float(cfg["radius_m"])
    xx, yy = _world_grids(elev, meta)
    keep &= (xx - cx) ** 2 + (yy - cy) ** 2 <= radius * radius
    far_hole = max(
        float(cfg["hole_pad_m"]),
        float(cfg["mid_m"]) - float(cfg["mid_overlap_m"]),
    )
    keep &= ~_bbox_rect(xx, yy, site, far_hole)
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


def bake_texture(
    elev: np.ndarray,
    wc: np.ndarray | None,
    cell_m: float,
    size: int,
    ortho: np.ndarray | None = None,
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
        shade = 0.78 + 0.22 * tex_hs[..., None]
        return (np.clip(rgb * shade, 0.0, 1.0) * 255.0).astype(np.uint8)

    lut = np.zeros((256, 3), dtype=np.float32)
    # default alpine grey-green
    lut[:] = (0.42, 0.44, 0.36)
    for code, rgb in WC_RGB.items():
        lut[code] = (rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)
    if wc is not None:
        wc_img = Image.fromarray(wc, mode="L").resize((size, size), Image.Resampling.NEAREST)
        codes = np.array(wc_img)
        rgb = lut[codes]
    else:
        # Hypsometric: valley green → alpine rock → snow. No landcover needed.
        z = tex_z
        t = np.clip((z - 800.0) / 1600.0, 0.0, 1.0)
        low = np.array([0.22, 0.32, 0.18], dtype=np.float32)
        mid = np.array([0.45, 0.46, 0.32], dtype=np.float32)
        high = np.array([0.55, 0.54, 0.50], dtype=np.float32)
        rgb = (1 - t)[..., None] * low + t[..., None] * mid
        t2 = np.clip((z - 2000.0) / 500.0, 0.0, 1.0)
        rgb = (1 - t2)[..., None] * rgb + t2[..., None] * high

    rock = np.array([0.52, 0.50, 0.47], dtype=np.float32)
    snow = np.array([0.88, 0.90, 0.92], dtype=np.float32)
    steep_w = np.clip((tex_st - 28.0) / 28.0, 0.0, 1.0)[..., None]
    rgb = (1.0 - 0.75 * steep_w) * rgb + 0.75 * steep_w * rock
    alp = np.clip((tex_z - 1750.0) / 650.0, 0.0, 1.0)[..., None]
    rgb = (1.0 - 0.35 * alp) * rgb + 0.35 * alp * rock
    sn = np.clip((tex_z - 2450.0) / 450.0, 0.0, 1.0)[..., None]
    rgb = (1.0 - sn) * rgb + sn * snow
    shade = 0.52 + 0.48 * tex_hs[..., None]
    rgb = np.clip(rgb * shade, 0.0, 1.0)
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
) -> dict[str, tuple[list[tuple[float, float, float]], list[tuple[int, int, int]], list[tuple[float, float]]]]:
    """Quads assigned by face centre so N/S/E/W splits do not open radial gaps."""
    px, py = float(meta["px"]), float(meta["py"])
    stride = max(1, int(round(mesh_step_m / abs(px))))
    z = elev[::stride, ::stride]
    k = keep[::stride, ::stride]
    h, w = z.shape
    xs = float(meta["origin_x"]) + (np.arange(w, dtype=np.float64) * stride + 0.5) * px
    ys = float(meta["origin_y"]) + (np.arange(h, dtype=np.float64) * stride + 0.5) * py
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


def write_material(path: Path, png_name: str, *, mat: str = MAT_NAME) -> None:
    data: dict = {}
    if path.is_file() and path.stat().st_size:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    data[mat] = {
        "name": mat,
        "mapTo": mat,
        "class": "Material",
        "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"autoroad:backdrop:mat:{mat}")),
        "Stages": [
            {
                "baseColorMap": png_name,
                "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
                "roughnessFactor": 0.94,
                "metallicFactor": 0.0,
            },
            {},
            {},
            {},
        ],
        "castShadows": False,
        "version": 1.5,
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


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
        if not args.skip_ortho:
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

    tex = bake_texture(
        elev, wc, abs(float(meta["px"])), int(cfg["texture_size"]), ortho=ortho
    )
    near_pack = None if args.skip_near_dgm else load_near_dgm(site)
    tex_near = None
    near_meshes: dict = {}
    keep_near_mesh = None
    if near_pack is None:
        print("near LOD skip: no Tirol DGM (outside coverage or fetch failed)")
    else:
        near_elev, near_meta = near_pack
        keep_near_mesh = keep_near_mask(near_elev, near_meta, site, cfg)
        hm_n = int((site.get("beamng") or {}).get("mask_size") or 0)
        hm_rel, _max_h, hm_label = load_composed_or_dgm(processed_dir(site), hm_n)
        print(
            f"near DGM {near_elev.shape} keep={int(keep_near_mesh.sum())}/"
            f"{keep_near_mesh.size} "
            f"z={float(np.nanmin(near_elev)):.1f}..{float(np.nanmax(near_elev)):.1f} "
            f"lip={hm_label}"
        )
        tex_near = bake_texture(
            near_elev,
            None,
            abs(float(near_meta["px"])),
            int(cfg["near_texture_size"]),
            ortho=ortho_near if ortho_near is not None else ortho,
        )
    tex_mid = bake_texture(
        crop_to_extent(elev, meta, mid_extent(site)),
        None,
        abs(float(meta["px"])),
        int(cfg["mid_texture_size"]),
        ortho=ortho_mid if ortho_mid is not None else ortho,
    )
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
        print(f"Meshing near step={cfg['near_step_m']} m (Tirol DGM, lip drop) ...")
        near_meshes = build_meshes(
            near_elev,
            keep_near_mesh,
            near_meta,
            sc,
            z0,
            float(cfg["near_step_m"]),
            split_quadrants=True,
            uv_bbox=near_extent(site),
            snap_inner_bbox=True,
            hm_rel=hm_rel,
            lip_drop_m=float(cfg["near_lip_drop_m"]),
        )
    print(f"Meshing mid step={cfg['mid_step_m']} m (Copernicus, always-keep) ...")
    mid_meshes = build_meshes(
        elev,
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

    png_name = "backdrop_diffuse.png"
    png_near = "backdrop_near.png"
    png_mid = "backdrop_mid.png"
    mesh_dir = proc / "backdrop_meshes"
    mesh_dir.mkdir(parents=True, exist_ok=True)
    for leftover in mesh_dir.glob("backdrop_near_*.dae"):
        leftover.unlink()
    for leftover in mesh_dir.glob("backdrop_mid_*.dae"):
        leftover.unlink()
    Image.fromarray(tex, mode="RGB").save(mesh_dir / png_name)
    mats_path = mesh_dir / "main.materials.json"
    if mats_path.is_file():
        mats_path.unlink()
    write_material(mats_path, png_name, mat=MAT_NAME)
    if tex_near is not None:
        Image.fromarray(tex_near, mode="RGB").save(mesh_dir / png_near)
        write_material(mats_path, png_near, mat=MAT_NEAR)
    Image.fromarray(tex_mid, mode="RGB").save(mesh_dir / png_mid)
    write_material(mats_path, png_mid, mat=MAT_MID)

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
    print("Reload the level in BeamNG (do not save over items.level.json from an old session).")


if __name__ == "__main__":
    main()
