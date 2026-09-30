"""Put the 0.5 m corridor ground onto the playable heightmap.

The road the player drives is the mesh. This layer is the ground beside and
under it, taken from the raw 0.5 m DGM. It replaces the coarser heightmap and
the road-bed layer inside the corridor. Bridge and gallery span layers stay
on top.

Within 1.1 m outside the carriageway edge, ground may not sit higher than the
mesh surface raster minus one heightmap step. Inside the carriageway it may not
sit higher than that raster minus two steps. The raster is
``road_grid/road_grid_z.tif`` from build_road_grid.py (edge fill and smoothing
already applied); the mesh sits another 4 cm above it. Lower ground is left as
it is. The span is not filled.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\apply_corridor_dgm.py
"""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
import tifffile as tiff
from PIL import Image
from scipy.spatial import cKDTree
from shapely.ops import unary_union

import heightmap_layers as hml
from site_coords import SiteCoords, load_site, processed_dir

RES_M = 0.5
EDGE_M = 1.1
EDGE_STEPS = 1
DECK_STEPS = 2


def _bilinear(arr: np.ndarray, fx: np.ndarray, fy: np.ndarray) -> np.ndarray:
    height, width = arr.shape
    x0 = np.floor(fx).astype(np.int32)
    y0 = np.floor(fy).astype(np.int32)
    x1 = np.clip(x0 + 1, 0, width - 1)
    y1 = np.clip(y0 + 1, 0, height - 1)
    x0 = np.clip(x0, 0, width - 1)
    y0 = np.clip(y0, 0, height - 1)
    tx = (fx - np.floor(fx)).astype(np.float32)
    ty = (fy - np.floor(fy)).astype(np.float32)
    out = (
        arr[y0, x0] * (1.0 - tx) * (1.0 - ty)
        + arr[y0, x1] * tx * (1.0 - ty)
        + arr[y1, x0] * (1.0 - tx) * ty
        + arr[y1, x1] * tx * ty
    )
    return np.asarray(out, dtype=np.float32)


def _carriageway(proc: Path):
    bridged = proc / "dgm_repair_transect" / "carriageway_bridged.gpkg"
    path = bridged if bridged.is_file() else proc / "centerline_shift_taper" / "centerline_shift.gpkg"
    if not path.is_file():
        print("road edge clamp skipped: no carriageway", flush=True)
        return None
    gdf = gpd.read_file(path, layer="carriageway")
    polys = [g for g in gdf.geometry if g is not None and not g.is_empty]
    if not polys:
        return None
    road = unary_union(polys).buffer(0)
    band = road.buffer(EDGE_M).difference(road)
    if band.is_empty:
        return None
    print(f"road edge clamp within {EDGE_M:.1f} m from {path.name}", flush=True)
    return road, band


def _road_surface(proc: Path):
    """Finite road-raster cells. Their Z is the surface the mesh is cut from.

    Preferred: ``road_grid/road_grid_z.tif`` from build_road_grid.py, the
    heights after edge fill and smoothing (the mesh top minus CLEARANCE_M).
    Fallback: the repaired DGM, which differs from the mesh in the outer metre.
    """
    candidates = (
        proc / "road_grid" / "road_grid_z.tif",
        proc / "dgm_repair_transect" / "03_with_bridges.tif",
        proc / "dgm_repair_transect" / "02_repaired.tif",
    )
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        print("road edge clamp skipped: no road surface raster", flush=True)
        return None
    if path.name != "road_grid_z.tif":
        print(
            f"road surface: {path.name} (no road_grid_z.tif; run build_road_grid.py "
            "first so the ceiling follows the mesh edge)",
            flush=True,
        )
    with tiff.TiffFile(path) as src:
        page = src.pages[0]
        z = np.asarray(page.asarray(), dtype=np.float32)
        tie = tuple(page.tags["ModelTiepointTag"].value)
        pix = tuple(page.tags["ModelPixelScaleTag"].value)
    if z.ndim == 3:
        z = z[..., 0]
    res = float(pix[0])
    xmin = float(tie[3])
    ymax = float(tie[4])
    finite = np.isfinite(z) & (z > 50.0) & (z < 4500.0)
    rr, cc = np.nonzero(finite)
    if rr.size == 0:
        return None
    xs = xmin + (cc.astype(np.float64) + 0.5) * res
    ys = ymax - (rr.astype(np.float64) + 0.5) * res
    tree = cKDTree(np.column_stack((xs, ys)))
    print(f"road surface {path.name} cells={rr.size}", flush=True)
    return tree, z[rr, cc].astype(np.float64), z, xmin, ymax, res


def _hold(sampled, cap_rel, rows, cols, cap, z_min):
    """Lower cells that sit above cap. cap is one absolute elevation per cell."""
    if rows.size == 0:
        return cap_rel, 0
    lower = sampled[rows, cols].astype(np.float64) > cap
    n = int(np.count_nonzero(lower))
    if n == 0:
        return cap_rel, 0
    rr = rows[lower]
    cc = cols[lower]
    cap_l = np.asarray(cap, dtype=np.float64)[lower]
    sampled[rr, cc] = cap_l.astype(np.float32)
    if cap_rel is None:
        cap_rel = np.full(sampled.shape, np.nan, dtype=np.float64)
    cap_rel[rr, cc] = cap_l - z_min
    return cap_rel, n


def main() -> None:
    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "").strip()
    meta = json.loads((proc / "heightmap_meta.json").read_text(encoding="utf-8"))
    size = int(meta.get("heightmap_size_px") or bng.get("mask_size") or 8192)
    extent = float(meta.get("terrain_extent_m") or size)
    max_h = float(meta["max_height_m"])
    z_min = float(meta["z_min_m"])
    scale = extent / (size - 1)

    raw_path = proc / "corridor50_raw" / "corridor50_raw.tif"
    if not raw_path.is_file():
        raise SystemExit(f"missing {raw_path}")
    with tiff.TiffFile(raw_path) as src:
        page = src.pages[0]
        raw = np.asarray(page.asarray(), dtype=np.float32)
        tie = tuple(page.tags["ModelTiepointTag"].value)
        pix = tuple(page.tags["ModelPixelScaleTag"].value)
    if raw.ndim == 3:
        raw = raw[..., 0]
    res = float(pix[0])
    if abs(res - RES_M) > 1.0e-3:
        raise SystemExit(f"{raw_path.name} pixel size is {res}, expected {RES_M}")
    xmin = float(tie[3])
    ymax = float(tie[4])
    height, width = raw.shape
    xmax = xmin + width * res
    ymin = ymax - height * res

    c0 = max(0, int(np.floor((xmin - sc.xmin) / sc.bw * (size - 1))) - 1)
    c1 = min(size, int(np.ceil((xmax - sc.xmin) / sc.bw * (size - 1))) + 2)
    r_north = (size - 1) * (1.0 - (ymax - sc.ymin) / sc.bh)
    r_south = (size - 1) * (1.0 - (ymin - sc.ymin) / sc.bh)
    r0 = max(0, int(np.floor(min(r_north, r_south))) - 1)
    r1 = min(size, int(np.ceil(max(r_north, r_south))) + 2)

    z_u16 = np.zeros((size, size), dtype=np.uint16)
    w_u8 = np.zeros((size, size), dtype=np.uint8)
    n_write = 0
    n_edge = 0
    n_deck = 0
    step = max_h / 65535.0
    geom = _carriageway(proc)
    road = band = None
    if geom is not None:
        road, band = geom
    surface = _road_surface(proc)
    tree = road_z = road_arr = None
    road_xmin = road_ymax = road_res = None
    if surface is not None:
        tree, road_z, road_arr, road_xmin, road_ymax, road_res = surface
    # The band is 1.1 m outside the polygon. The nearest road cell can sit
    # another half cell inside the edge.
    search_m = EDGE_M + RES_M
    print(
        f"edge ceiling is road surface minus {EDGE_STEPS} step ({EDGE_STEPS * step * 100:.2f} cm), "
        f"deck ceiling is road surface minus {DECK_STEPS} steps ({DECK_STEPS * step * 100:.2f} cm)",
        flush=True,
    )
    for r_a in range(r0, r1, 256):
        r_b = min(r1, r_a + 256)
        cc = np.arange(c0, c1, dtype=np.float64)
        rr = np.arange(r_a, r_b, dtype=np.float64)
        bx = cc * scale
        by = (size - 1 - rr) * scale
        crs_x = sc.xmin + bx / extent * sc.bw
        crs_y = sc.ymin + by / extent * sc.bh
        gx, gy = np.meshgrid(crs_x, crs_y)
        inside = (gx >= xmin) & (gx < xmax) & (gy >= ymin) & (gy < ymax)
        fx = (gx - xmin) / res - 0.5
        fy = (ymax - gy) / res - 0.5
        sampled = _bilinear(raw, fx, fy)
        good = inside & np.isfinite(sampled) & (sampled > 50.0) & (sampled < 4500.0)
        cap_rel = None
        if road is not None and road_arr is not None:
            on_road = shapely.contains_xy(road, gx.ravel(), gy.ravel()).reshape(gx.shape)
            deck = on_road & good
            if np.any(deck):
                rz = np.full(gx.shape, np.nan, dtype=np.float64)
                rh, rw = road_arr.shape[:2]
                rx1 = road_xmin + rw * road_res
                ry0 = road_ymax - rh * road_res
                on_grid = deck & (gx >= road_xmin) & (gx < rx1) & (gy >= ry0) & (gy < road_ymax)
                if np.any(on_grid):
                    rz[on_grid] = _bilinear(
                        road_arr,
                        (gx[on_grid] - road_xmin) / road_res - 0.5,
                        (road_ymax - gy[on_grid]) / road_res - 0.5,
                    )
                valid = deck & np.isfinite(rz) & (rz > 50.0) & (rz < 4500.0)
                miss = deck & ~valid
                if tree is not None and np.any(miss):
                    dist, idx = tree.query(
                        np.column_stack((gx[miss], gy[miss])),
                        k=1,
                        distance_upper_bound=search_m,
                        workers=-1,
                    )
                    found = np.isfinite(dist) & (idx < road_z.size)
                    if np.any(found):
                        use = np.flatnonzero(miss)[found]
                        rr_m, cc_m = np.unravel_index(use, miss.shape)
                        rz[rr_m, cc_m] = road_z[idx[found]]
                        valid = deck & np.isfinite(rz) & (rz > 50.0) & (rz < 4500.0)
                if np.any(valid):
                    rows, cols = np.nonzero(valid)
                    cap = rz[rows, cols] - DECK_STEPS * step
                    cap_rel, n = _hold(sampled, cap_rel, rows, cols, cap, z_min)
                    n_deck += n
        if band is not None and tree is not None:
            near = shapely.contains_xy(band, gx.ravel(), gy.ravel()).reshape(gx.shape)
            hit = near & good
            if np.any(hit):
                rows, cols = np.nonzero(hit)
                dist, idx = tree.query(
                    np.column_stack((gx[hit], gy[hit])),
                    k=1,
                    distance_upper_bound=search_m,
                    workers=-1,
                )
                ok = np.isfinite(dist) & (idx < road_z.size)
                if np.any(ok):
                    cap = road_z[idx[ok]] - EDGE_STEPS * step
                    cap_rel, n = _hold(sampled, cap_rel, rows[ok], cols[ok], cap, z_min)
                    n_edge += n
        if not np.any(good):
            continue
        rel = sampled[good].astype(np.float64) - z_min
        bins = hml._encode_z_u16(rel, max_h)
        if cap_rel is not None:
            held = np.isfinite(cap_rel) & good
            if np.any(held):
                # Floor the bin so rounding cannot land above the ceiling.
                floored = np.floor(cap_rel[held] / max_h * 65535.0 + 1.0e-6)
                bins = np.array(bins, copy=True)
                bins[held[good]] = np.clip(floored, 0, 65535).astype(np.uint16)
        z_u16[r_a:r_b, c0:c1][good] = bins
        w_u8[r_a:r_b, c0:c1][good] = 255
        n_write += int(np.count_nonzero(good))
        print(f"  rows {r_a}-{r_b} px={int(np.count_nonzero(good))}", flush=True)

    if n_write == 0:
        raise SystemExit("0.5 m DGM hit no heightmap pixel")
    layers = hml.layers_dir(proc)
    Image.fromarray(z_u16, mode="I;16").save(layers / "corridor_dgm_z.png")
    Image.fromarray(w_u8, mode="L").save(layers / "corridor_dgm_w.png")
    hml._upsert_layer(
        proc,
        {
            "name": "corridor_dgm",
            "mode": "replace",
            "priority": 45,
            "size": size,
            "max_height_m": max_h,
            "z": "heightmap_layers/corridor_dgm_z.png",
            "weight": "heightmap_layers/corridor_dgm_w.png",
            "nz": n_write,
        },
    )
    print(
        f"corridor ground px={n_write} edge_clamped={n_edge} deck_clamped={n_deck} — composing",
        flush=True,
    )
    hml.compose(proc, size=size, max_h=max_h, level_name=level_name or None)


if __name__ == "__main__":
    main()
