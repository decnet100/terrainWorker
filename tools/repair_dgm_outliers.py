"""Drop rough pixels inside the tapered carriageway and fill the holes.

The kept placement still contains pixels that sit well above the rest of the
road. Those are removed. The outer edge is the boundary of what remains.
Interior gaps are filled from the surrounding kept heights. Nothing is written
into a BeamNG level.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\repair_dgm_outliers.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import tifffile as tiff
from scipy.ndimage import binary_fill_holes, label, uniform_filter
from shapely import intersects_xy, linestrings
from shapely.geometry import Polygon
from shapely.ops import polygonize, unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_road_grid import RES_M, _load_geotiff  # noqa: E402
from filter_road_corridor import write_referenced_tif  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

# Local residual at or above this is "still allowed, but clearly off the rest".
DROP_ROUGH_M = 0.015
MIN_ISLAND_PX = 30
FILL_ITERS = 80
SMOOTH_ITERS = 8
EDGE_SIMPLIFY_M = 1.0


def _load_roughness(path: Path) -> np.ndarray:
    with tiff.TiffFile(path) as src:
        arr = np.asarray(src.pages[0].asarray(), dtype=np.float32)
    if arr.ndim == 3:
        arr = arr[..., 0]
    return arr


def _crop_box(poly, spec: dict) -> tuple[int, int, int, int] | None:
    minx, miny, maxx, maxy = poly.bounds
    width = int(spec["width"])
    height = int(spec["height"])
    c0 = max(0, int(math.floor((minx - spec["xmin"]) / RES_M)) - 1)
    c1 = min(width, int(math.ceil((maxx - spec["xmin"]) / RES_M)) + 1)
    r0 = max(0, int(math.floor((spec["ymax"] - maxy) / RES_M)) - 1)
    r1 = min(height, int(math.ceil((spec["ymax"] - miny) / RES_M)) + 1)
    if c1 <= c0 or r1 <= r0:
        return None
    return c0, c1, r0, r1


def _inside(poly, spec: dict, c0: int, c1: int, r0: int, r1: int) -> np.ndarray:
    cc = np.arange(c0, c1)
    rr = np.arange(r0, r1)
    xs = spec["xmin"] + (cc + 0.5) * RES_M
    ys = spec["ymax"] - (rr + 0.5) * RES_M
    xx, yy = np.meshgrid(xs, ys)
    return intersects_xy(poly, xx.ravel(), yy.ravel()).reshape(yy.shape)


def _fill_holes(z: np.ndarray, hole: np.ndarray) -> np.ndarray:
    """Keep known heights fixed. Spread them into `hole` by repeated local means."""
    out = z.astype(np.float32, copy=True)
    out[hole] = np.nan
    for _ in range(FILL_ITERS):
        known = np.isfinite(out)
        if not np.any(hole & ~known):
            break
        weight = known.astype(np.float32)
        filled = np.where(known, out, np.float32(0.0))
        sw = uniform_filter(weight, size=3, mode="constant")
        sm = uniform_filter(filled, size=3, mode="constant")
        upd = hole & ~known & (sw > 1e-6)
        out[upd] = (sm[upd] / sw[upd]).astype(np.float32)
    for _ in range(SMOOTH_ITERS):
        known = np.isfinite(out)
        weight = known.astype(np.float32)
        filled = np.where(known, out, np.float32(0.0))
        sw = uniform_filter(weight, size=3, mode="constant")
        sm = uniform_filter(filled, size=3, mode="constant")
        smooth = hole & known & (sw > 1e-6)
        out[smooth] = (sm[smooth] / sw[smooth]).astype(np.float32)
    return out


def _edge_lines(sel: np.ndarray, x0: float, y_north: float, *, horizontal: bool, offset: int):
    rr, cc = np.nonzero(sel)
    if len(rr) == 0:
        return None
    if horizontal:
        y = y_north - (rr + offset) * RES_M
        x_lo = x0 + cc * RES_M
        coords = np.empty((len(rr), 2, 2), dtype=np.float64)
        coords[:, 0, 0] = x_lo
        coords[:, 0, 1] = y
        coords[:, 1, 0] = x_lo + RES_M
        coords[:, 1, 1] = y
    else:
        x = x0 + (cc + offset) * RES_M
        y_hi = y_north - rr * RES_M
        coords = np.empty((len(rr), 2, 2), dtype=np.float64)
        coords[:, 0, 0] = x
        coords[:, 0, 1] = y_hi
        coords[:, 1, 0] = x
        coords[:, 1, 1] = y_hi - RES_M
    return linestrings(coords)


def _boundary_polygon(mask: np.ndarray, x0: float, y_north: float):
    above = np.zeros_like(mask)
    above[1:] = mask[:-1]
    below = np.zeros_like(mask)
    below[:-1] = mask[1:]
    left = np.zeros_like(mask)
    left[:, 1:] = mask[:, :-1]
    right = np.zeros_like(mask)
    right[:, :-1] = mask[:, 1:]
    parts = [
        _edge_lines(mask & ~above, x0, y_north, horizontal=True, offset=0),
        _edge_lines(mask & ~below, x0, y_north, horizontal=True, offset=1),
        _edge_lines(mask & ~left, x0, y_north, horizontal=False, offset=0),
        _edge_lines(mask & ~right, x0, y_north, horizontal=False, offset=1),
    ]
    parts = [p for p in parts if p is not None]
    if not parts:
        return None
    lines = unary_union(np.concatenate(parts))
    polys = [p for p in polygonize(lines) if p.area > 20.0]
    if not polys:
        return None
    poly = max(polys, key=lambda p: p.area)
    poly = poly.simplify(EDGE_SIMPLIFY_M, preserve_topology=True)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return None
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda p: p.area)
    return poly if isinstance(poly, Polygon) else None


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    raw_path = proc / "corridor50_raw" / "corridor50_raw.tif"
    rough_path = proc / "centerline_shift_taper" / "01_roughness.tif"
    gpkg_in = proc / "centerline_shift_taper" / "centerline_shift.gpkg"
    if not raw_path.is_file() or not rough_path.is_file() or not gpkg_in.is_file():
        raise SystemExit("taper carriageway, roughness, or raw DGM is missing")
    out_dir = proc / "dgm_repair"
    out_dir.mkdir(parents=True, exist_ok=True)

    z, spec = _load_geotiff(raw_path)
    rough = _load_roughness(rough_path)
    if rough.shape != z.shape:
        raise SystemExit(f"roughness {rough.shape} does not match DGM {z.shape}")
    roads = gpd.read_file(gpkg_in, layer="carriageway")
    print(f"carriageway polygons {len(roads)}", flush=True)

    repaired = np.full(z.shape, np.nan, dtype=np.float32)
    klass = np.zeros(z.shape, dtype=np.uint8)
    edge_geoms = []
    edge_rows = []
    n_keep = n_drop = n_fill = 0

    for row in roads.itertuples(index=False):
        poly = row.geometry
        if poly is None or poly.is_empty:
            continue
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda p: p.area)
        box = _crop_box(poly, spec)
        if box is None:
            continue
        c0, c1, r0, r1 = box
        inside = _inside(poly, spec, c0, c1, r0, r1)
        rough_c = rough[r0:r1, c0:c1]
        z_c = z[r0:r1, c0:c1]
        road = inside & np.isfinite(z_c)
        drop = road & np.isfinite(rough_c) & (rough_c >= DROP_ROUGH_M)
        keep = road & ~drop
        if keep.sum() < MIN_ISLAND_PX:
            continue
        lab, nlab = label(keep)
        if nlab:
            counts = np.bincount(lab.ravel())
            tiny = counts < MIN_ISLAND_PX
            tiny[0] = False
            keep = keep & ~tiny[lab]
        if not np.any(keep):
            continue
        region = binary_fill_holes(keep)
        region = region & road
        hole = region & ~keep
        filled = _fill_holes(np.where(keep, z_c, np.nan), hole)

        x0 = spec["xmin"] + c0 * RES_M
        y_north = spec["ymax"] - r0 * RES_M
        edge = _boundary_polygon(region, x0, y_north)
        if edge is not None:
            edge_geoms.append(edge)
            edge_rows.append(
                {
                    "objectid": int(getattr(row, "objectid", 0) or 0),
                    "dropped_px": int(drop.sum()),
                    "filled_px": int(hole.sum()),
                    "kept_px": int(keep.sum()),
                }
            )

        dest_z = repaired[r0:r1, c0:c1]
        dest_k = klass[r0:r1, c0:c1]
        dest_z[keep] = z_c[keep]
        dest_k[keep] = 1
        wrote = hole & np.isfinite(filled)
        dest_z[wrote] = filled[wrote]
        dest_k[wrote] = 3
        dest_k[drop & ~wrote] = 2
        n_keep += int(keep.sum())
        n_drop += int(drop.sum())
        n_fill += int(wrote.sum())
        print(
            f"  oid {getattr(row, 'objectid', '?')}: keep {int(keep.sum())} drop {int(drop.sum())} fill {int(wrote.sum())}",
            flush=True,
        )

    write_referenced_tif(out_dir / "02_repaired.tif", repaired, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    write_referenced_tif(out_dir / "01_class.tif", klass, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    edge_path = out_dir / "edge.gpkg"
    if edge_path.exists():
        edge_path.unlink()
    if edge_geoms:
        gpd.GeoDataFrame(edge_rows, geometry=edge_geoms, crs="EPSG:31254").to_file(
            edge_path, layer="edge", driver="GPKG"
        )
    report = {
        "source_heights": str(raw_path),
        "source_roughness": str(rough_path),
        "carriageway": str(gpkg_in),
        "drop_rough_m": DROP_ROUGH_M,
        "kept_px": n_keep,
        "dropped_px": n_drop,
        "filled_px": n_fill,
        "edges": len(edge_geoms),
        "class": {
            "0": "outside the repaired road",
            "1": "kept DGM pixel",
            "2": "dropped, and not inside the reconstructed edge",
            "3": "gap interpolated from the kept pixels",
        },
        "note": (
            "Pixels whose local residual is at least "
            f"{DROP_ROUGH_M:.3f} m are removed. The edge is the outer boundary of "
            "the remaining pixels, simplified by "
            f"{EDGE_SIMPLIFY_M:.0f} m. Interior gaps are filled by repeated local "
            "means; the kept heights stay fixed. Not injected into a level."
        ),
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("kept_px", "dropped_px", "filled_px", "edges")}, indent=2))
    print(f"Wrote {out_dir}")


if __name__ == "__main__":
    main()
