"""Inject BeamNG WaterBlock / River from Tirol landcover Gewässer polygons.

Standing water (LN-GWS) → WaterBlock(s) + optional heightmap basin carve.
Terrain Mud stays as shore/bed paint; reflective water needs WaterBlock meshes.

Large lakes: shrink by shore_inset_m, then tile WaterBlocks that overlap the
core. standing_depress_m lowers the DGM under LN-GWS so blocks sit in a hole.
WaterBlock Z comes from densified shoreline samples (not centroid — islands).

After placement, fit_check trims / subdivides / drops blocks that hang over
terrain steps or punch through MeshRoad decks (tunnels under lakes). Run
bridges/galleries before water so MeshRoad NDJSON exists.

Flowing water (LN-GWF) → optional River, or WaterBlocks when wide_flowing_min_width_m
is set and the floodplain is wide enough.

Usage:
  $env:AUTOROAD_SITE='config/sites/l13_kuehtai.yaml'
  python tools/build_water.py
"""
from __future__ import annotations

import copy
import json
import math
import sys
import uuid
from pathlib import Path

import numpy as np
from pyproj import Transformer
from shapely.geometry import Polygon
from shapely.validation import make_valid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import SiteCoords, load_site, processed_dir  # noqa: E402
import build_bridges as bb  # noqa: E402
import build_galleries as bg  # noqa: E402
from build_terrain_masks import (  # noqa: E402
    _load_landcover_geojson,
    _tirol_landnutzung_kind,
)

USER_LEVELS = bb.USER_LEVELS


def _cfg(bng: dict) -> dict:
    raw = bng.get("water") or {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        # Landcover "Gewässer fließend" is a floodplain polygon — PCA Rivers become
        # huge flat slabs. Default: terrain Mud only, no River mesh.
        "flowing": str(raw.get("flowing") or "none").lower(),  # none | river
        "standing": str(raw.get("standing") or "waterblock").lower(),  # none | waterblock
        "material": str(raw.get("material") or ""),
        "depth_m": float(raw.get("depth_m") or 3.0),
        "surface_lift_m": float(raw.get("surface_lift_m") or 0.15),
        # Global Z nudge for all WaterBlocks (m). Positive = higher.
        "waterlevel_z_offset_m": float(raw.get("waterlevel_z_offset_m") or 0.0),
        "min_area_m2": float(raw.get("min_area_m2") or 40.0),
        # Single WaterBlock AABB up to this side length; larger lakes are tiled.
        "standing_max_side_m": float(raw.get("standing_max_side_m") or 80.0),
        "standing_tile_m": float(raw.get("standing_tile_m") or 64.0),
        # Positive: erode polygon so Mud rim stays visible around WaterBlocks.
        "shore_inset_m": float(raw.get("shore_inset_m") or 4.0),
        # Grow WaterBlock XY beyond lake AABB (fraction, e.g. 0.08 = +8%).
        "standing_xy_pad": float(raw.get("standing_xy_pad") or 0.0),
        # LN-GWF wider than this (PCA half-width*2) also get WaterBlocks.
        "wide_flowing_min_width_m": float(raw.get("wide_flowing_min_width_m") or 0.0),
        # Lower terrain under LN-GWS lakes so WaterBlocks have a basin (meters).
        "standing_depress_m": float(raw.get("standing_depress_m") or 0.0),
        "standing_depress_blend_m": float(raw.get("standing_depress_blend_m") or 6.0),
        "river_min_length_m": float(raw.get("river_min_length_m") or 12.0),
        "river_min_width_m": float(raw.get("river_min_width_m") or 2.0),
        "river_max_width_m": float(raw.get("river_max_width_m") or 12.0),
        "river_depth_m": float(raw.get("river_depth_m") or 1.5),
        "grid_element_size": float(raw.get("grid_element_size") or 4.0),
        "segment_length": float(raw.get("segment_length") or 8.0),
        "subdivide_length": float(raw.get("subdivide_length") or 2.5),
        # Post-placement fit: hang over terrain steps / punch MeshRoad decks.
        "fit_check": bool(raw.get("fit_check", True)),
        "hang_max_m": float(raw.get("hang_max_m") or 2.5),
        "bank_max_m": float(raw.get("bank_max_m") or 2.5),
        "fit_bad_frac": float(raw.get("fit_bad_frac") or 0.15),
        "fit_sample_m": float(raw.get("fit_sample_m") or 8.0),
        "fit_min_side_m": float(raw.get("fit_min_side_m") or 8.0),
        "fit_subdivide": bool(raw.get("fit_subdivide", True)),
        "fit_shrink": bool(raw.get("fit_shrink", True)),
        "meshroad_clearance_m": float(raw.get("meshroad_clearance_m") or 0.75),
        "fit_min_depth_m": float(raw.get("fit_min_depth_m") or 0.5),
    }


def _rings_wgs84(geom: dict) -> list[list[tuple[float, float]]]:
    gtype = (geom or {}).get("type")
    coords = (geom or {}).get("coordinates")
    rings: list[list[tuple[float, float]]] = []
    if gtype == "Polygon" and coords:
        rings.append([(float(c[0]), float(c[1])) for c in coords[0] if len(c) >= 2])
    elif gtype == "MultiPolygon" and coords:
        for poly in coords:
            if poly:
                rings.append([(float(c[0]), float(c[1])) for c in poly[0] if len(c) >= 2])
    return [r for r in rings if len(r) >= 3]


def _to_beamng(
    ring_ll: list[tuple[float, float]],
    to_crs: Transformer,
    coords: SiteCoords,
) -> np.ndarray:
    pts = []
    for lon, lat in ring_ll:
        x, y = to_crs.transform(lon, lat)
        bx, by = coords.crs_to_beamng(float(x), float(y))
        pts.append((bx, by))
    return np.asarray(pts, dtype=np.float64)


def _poly_area(xy: np.ndarray) -> float:
    x, y = xy[:, 0], xy[:, 1]
    return 0.5 * float(np.abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def _densify_ring(xy: np.ndarray, spacing_m: float) -> np.ndarray:
    """Evenly sample along a closed ring (last point may equal first)."""
    if xy.shape[0] < 2:
        return xy
    pts = [xy[0]]
    for i in range(len(xy) - 1):
        p0 = xy[i]
        p1 = xy[i + 1]
        seg = p1 - p0
        length = float(np.hypot(seg[0], seg[1]))
        if length < 1e-6:
            continue
        n = max(1, int(math.ceil(length / max(spacing_m, 0.5))))
        for k in range(1, n + 1):
            t = k / n
            pts.append(p0 + t * seg)
    return np.asarray(pts, dtype=np.float64)


def _lake_surface_z(z_at, xy: np.ndarray, *, spacing_m: float = 8.0) -> float:
    """Estimate flat water surface height from DGM (island-safe).

    Prefer the modal elevation of *interior* samples in the lower half of the
    height range — alpine DGMs are usually flat at the water plane over the
    lake body, while island summits are high outliers. Shore-ring samples are
    a fallback (cliffs can bias them upward).
    """
    from shapely.geometry import Point

    poly = _as_polygon(xy)
    zs: list[float] = []
    if poly is not None and poly.area > 1.0:
        minx, miny, maxx, maxy = poly.bounds
        # ~1 sample / 12 m², capped.
        n = int(max(80, min(1200, poly.area / 12.0)))
        rng = np.random.default_rng(abs(hash((round(minx, 1), round(miny, 1))) ) % (2**32))
        tries = 0
        while len(zs) < n and tries < n * 8:
            tries += 1
            x = float(rng.uniform(minx, maxx))
            y = float(rng.uniform(miny, maxy))
            if poly.contains(Point(x, y)):
                zs.append(float(z_at(x, y)))
    if len(zs) >= 20:
        arr = np.asarray(zs, dtype=np.float64)
        # Keep lower half so island peaks never enter the mode vote.
        lo = arr[arr <= float(np.median(arr))]
        if lo.size == 0:
            lo = arr
        bins = np.round(lo * 4.0) / 4.0  # 0.25 m bins
        vals, counts = np.unique(bins, return_counts=True)
        return float(vals[int(np.argmax(counts))])

    # Fallback: densified exterior ring, low percentile.
    if xy.shape[0] < 3:
        return float(z_at(float(xy[:, 0].mean()), float(xy[:, 1].mean())))
    ring = xy
    if len(ring) > 3 and np.hypot(*(ring[0] - ring[-1])) < 0.05:
        ring = ring[:-1]
    closed = np.vstack([ring, ring[0]])
    samples = _densify_ring(closed, spacing_m)
    shore = np.asarray(
        [float(z_at(float(p[0]), float(p[1]))) for p in samples], dtype=np.float64
    )
    if shore.size == 0:
        return float(z_at(float(xy[:, 0].mean()), float(xy[:, 1].mean())))
    return float(np.percentile(shore, 20))


def _water_block_box(
    name: str, cx: float, cy: float, sx: float, sy: float, z_surf: float, cfg: dict
) -> dict:
    # Volume depth must reach the carved lake bed when standing_depress_m is set.
    depth = max(float(cfg["depth_m"]), float(cfg["standing_depress_m"]) + 0.5)
    pad = max(0.0, float(cfg.get("standing_xy_pad") or 0.0))
    sx_p = max(2.0, sx * (1.0 + pad))
    sy_p = max(2.0, sy * (1.0 + pad))
    z_pos = float(z_surf) + float(cfg.get("waterlevel_z_offset_m") or 0.0)
    obj: dict = {
        "name": name,
        "class": "WaterBlock",
        "__parent": "Water",
        "persistentId": str(uuid.uuid4()),
        "position": [cx, cy, z_pos],
        "rotationMatrix": [1, 0, 0, 0, 1, 0, 0, 0, 1],
        "scale": [sx_p, sy_p, depth],
        "gridElementSize": float(cfg["grid_element_size"]),
    }
    mat = cfg.get("material") or ""
    if mat:
        obj["material"] = mat
    return obj


def _as_polygon(xy: np.ndarray) -> Polygon | None:
    if xy.shape[0] < 3:
        return None
    try:
        poly = make_valid(Polygon(xy))
    except Exception:
        return None
    if poly.is_empty:
        return None
    if poly.geom_type == "Polygon":
        return poly if not poly.is_empty else None
    if poly.geom_type == "MultiPolygon":
        parts = [g for g in poly.geoms if isinstance(g, Polygon) and not g.is_empty]
        if not parts:
            return None
        return max(parts, key=lambda g: g.area)
    return None


def _core_geoms(xy: np.ndarray, shore_inset_m: float) -> list[Polygon]:
    """Erode standing water so Mud rim remains; fall back to full poly if too thin."""
    poly = _as_polygon(xy)
    if poly is None:
        return []
    if shore_inset_m <= 0:
        return [poly]
    eroded = poly.buffer(-float(shore_inset_m))
    if eroded.is_empty:
        return [poly]
    if eroded.geom_type == "Polygon":
        return [eroded]
    if eroded.geom_type == "MultiPolygon":
        return [g for g in eroded.geoms if isinstance(g, Polygon) and not g.is_empty]
    return [poly]


def _pca_width_m(xy: np.ndarray) -> float:
    """Approx floodplain width via PCA (2 * median |lateral|)."""
    if xy.shape[0] < 3:
        return 0.0
    mean = xy.mean(axis=0)
    centered = xy - mean
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    perp = vt[1] if vt.shape[0] > 1 else np.array([-vt[0, 1], vt[0, 0]])
    lat = centered @ perp
    return 2.0 * float(np.percentile(np.abs(lat), 50))


def _standing_water_blocks(name: str, xy: np.ndarray, z_at, cfg: dict) -> list[dict]:
    """One AABB WaterBlock per lake core; tile only if larger than standing_max_side_m.

    Prefer few large blocks (FPS). Dense tiling is only a fallback for huge polys.
    Z is taken once from the full shoreline (not core centroid) so islands/hills
    inside the lake do not lift the water plane.
    """
    from shapely.geometry import box as shapely_box

    cores = _core_geoms(xy, float(cfg["shore_inset_m"]))
    if not cores:
        return []
    max_side = float(cfg["standing_max_side_m"])
    tile = max(8.0, float(cfg["standing_tile_m"]))
    lift = float(cfg["surface_lift_m"])
    min_area = float(cfg["min_area_m2"])
    z_surf = _lake_surface_z(z_at, xy) + lift
    out: list[dict] = []
    for ci, core in enumerate(cores):
        if core.area < min_area * 0.25:
            continue
        minx, miny, maxx, maxy = core.bounds
        sx = float(maxx - minx)
        sy = float(maxy - miny)
        # Place XY at AABB center (coverage); Z stays shoreline water level.
        cx = 0.5 * (minx + maxx)
        cy = 0.5 * (miny + maxy)
        if max(sx, sy) <= max_side:
            out.append(_water_block_box(f"{name}_c{ci}", cx, cy, sx, sy, z_surf, cfg))
            continue
        # Sparse fallback for oversized lakes (axis-aligned tiles, no dense overlap).
        nx = max(1, int(math.ceil(sx / tile)))
        ny = max(1, int(math.ceil(sy / tile)))
        tw = sx / nx
        th = sy / ny
        min_overlap = 0.25 * tw * th
        for iy in range(ny):
            for ix in range(nx):
                tcx = minx + (ix + 0.5) * tw
                tcy = miny + (iy + 0.5) * th
                tile_g = shapely_box(
                    tcx - tw * 0.5, tcy - th * 0.5, tcx + tw * 0.5, tcy + th * 0.5
                )
                try:
                    inter = core.intersection(tile_g)
                except Exception:
                    continue
                if inter.is_empty or float(inter.area) < min_overlap:
                    continue
                out.append(
                    _water_block_box(
                        f"{name}_c{ci}_t{ix}_{iy}", tcx, tcy, tw, th, z_surf, cfg
                    )
                )
    return out


def _river(name: str, xy: np.ndarray, z_at, cfg: dict) -> dict | None:
    """Two-node River along PCA long axis of the polygon."""
    mean = xy.mean(axis=0)
    centered = xy - mean
    # SVD principal axis
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    axis = vt[0]
    proj = centered @ axis
    t0, t1 = float(proj.min()), float(proj.max())
    length = t1 - t0
    if length < float(cfg["river_min_length_m"]):
        return None
    # width ≈ 2 * RMS perpendicular extent (clamped)
    perp = vt[1] if vt.shape[0] > 1 else np.array([-axis[1], axis[0]])
    lat = centered @ perp
    width = max(float(cfg["river_min_width_m"]), 2.0 * float(np.percentile(np.abs(lat), 50)))
    width = min(width, float(cfg["river_max_width_m"]))
    p0 = mean + axis * t0
    p1 = mean + axis * t1
    z0 = float(z_at(float(p0[0]), float(p0[1]))) + float(cfg["surface_lift_m"])
    z1 = float(z_at(float(p1[0]), float(p1[1]))) + float(cfg["surface_lift_m"])
    depth = float(cfg["river_depth_m"])
    nodes = [
        [float(p0[0]), float(p0[1]), z0, width, depth, 0.0, 0.0, 1.0],
        [float(p1[0]), float(p1[1]), z1, width, depth, 0.0, 0.0, 1.0],
    ]
    # downhill flow: higher Z first
    if z0 < z1:
        nodes.reverse()
    obj: dict = {
        "name": name,
        "class": "River",
        "__parent": "Water",
        "persistentId": str(uuid.uuid4()),
        "position": nodes[0][:3],
        "nodes": nodes,
        "SegmentLength": float(cfg["segment_length"]),
        "SubdivideLength": float(cfg["subdivide_length"]),
        "FlowMagnitudePhysics": 1.5,
        "LowLODDistance": 80.0,
    }
    mat = cfg.get("material") or ""
    if mat:
        obj["material"] = mat
    return obj


def _load_water_features(proc: Path) -> list[tuple[str, dict]]:
    index_path = proc / "landcover_index.json"
    if not index_path.is_file():
        raise SystemExit(f"Missing {index_path} — run tools/fetch_landcover.py")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    ln = _load_landcover_geojson(index.get("landnutzung"))
    if not ln:
        raise SystemExit("Landnutzung GeoJSON missing")
    out: list[tuple[str, dict]] = []
    for f in ln.get("features") or []:
        kind = _tirol_landnutzung_kind(f.get("properties") or {})
        if kind not in ("water_standing", "water_flowing"):
            continue
        out.append((kind, f))
    return out


def _load_ndjson_meshroads(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if e.get("class") == "MeshRoad" and len(e.get("nodes") or []) >= 2:
            out.append(e)
    return out


def _load_meshroads(proc: Path, level_name: str) -> list[dict]:
    """Bridges + galleries MeshRoads (level first, then processed sidecars)."""
    found: list[dict] = []
    seen: set[str] = set()
    paths = [
        USER_LEVELS
        / level_name
        / "main"
        / "MissionGroup"
        / "level_objects"
        / "bridges"
        / "items.level.json",
        proc / "bridges_items.level.json",
        USER_LEVELS
        / level_name
        / "main"
        / "MissionGroup"
        / "level_objects"
        / "galleries"
        / "items.level.json",
        proc / "galleries_items.level.json",
    ]
    for path in paths:
        for e in _load_ndjson_meshroads(path):
            name = str(e.get("name") or "")
            if name in seen:
                continue
            seen.add(name)
            found.append(e)
    return found


def _project_meshroad(
    px: float, py: float, nodes: list
) -> tuple[float, float, float, float] | None:
    """Perp. hit on a span segment (t in [0,1]). Returns (dist, z_top, half_w, depth)."""
    best: tuple[float, float, float, float] | None = None
    for a, b in zip(nodes, nodes[1:]):
        ax, ay, az = float(a[0]), float(a[1]), float(a[2])
        bx, by, bz = float(b[0]), float(b[1]), float(b[2])
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        if seg2 < 1e-12:
            continue
        t = ((px - ax) * dx + (py - ay) * dy) / seg2
        if t < 0.0 or t > 1.0:
            continue
        qx, qy = ax + t * dx, ay + t * dy
        dist = math.hypot(px - qx, py - qy)
        z_top = az + t * (bz - az)
        wa = float(a[3]) if len(a) > 3 else 7.5
        wb = float(b[3]) if len(b) > 3 else wa
        half = 0.5 * (wa + t * (wb - wa))
        depth_a = float(a[4]) if len(a) > 4 else 0.4
        depth_b = float(b[4]) if len(b) > 4 else depth_a
        depth = depth_a + t * (depth_b - depth_a)
        if best is None or dist < best[0]:
            best = (dist, z_top, half, depth)
    return best


def _nearest_meshroad_under(
    px: float, py: float, meshroads: list[dict], surface_z: float, pad_m: float = 0.0
) -> float | None:
    """Lowest MeshRoad deck Z under (px,py) that sits below the water surface."""
    best_z: float | None = None
    for e in meshroads:
        hit = _project_meshroad(px, py, e.get("nodes") or [])
        if hit is None:
            continue
        dist, z_top, half, _depth = hit
        if dist > half + max(0.0, pad_m):
            continue
        if z_top >= surface_z - 0.05:
            continue
        if best_z is None or z_top < best_z:
            best_z = float(z_top)
    return best_z


def _block_xy(block: dict) -> tuple[float, float, float, float, float]:
    """cx, cy, sx, sy, surface_z from a WaterBlock entry."""
    pos = block["position"]
    scale = block["scale"]
    return (
        float(pos[0]),
        float(pos[1]),
        float(scale[0]),
        float(scale[1]),
        float(pos[2]),
    )


def _sample_grid(
    cx: float, cy: float, sx: float, sy: float, sample_m: float
) -> list[tuple[float, float]]:
    """Interior sample points over an axis-aligned footprint."""
    spacing = max(2.0, float(sample_m))
    nx = max(2, int(math.ceil(sx / spacing)) + 1)
    ny = max(2, int(math.ceil(sy / spacing)) + 1)
    nx = min(nx, 24)
    ny = min(ny, 24)
    pts: list[tuple[float, float]] = []
    for iy in range(ny):
        ty = (iy + 0.5) / ny
        y = cy - 0.5 * sy + ty * sy
        for ix in range(nx):
            tx = (ix + 0.5) / nx
            x = cx - 0.5 * sx + tx * sx
            pts.append((x, y))
    return pts


def _terrain_bad_mask(
    z_at,
    cx: float,
    cy: float,
    sx: float,
    sy: float,
    surface_z: float,
    cfg: dict,
) -> tuple[float, np.ndarray, int, int]:
    """Return (bad_frac, good_bool[ny,nx], nx, ny)."""
    hang = float(cfg["hang_max_m"])
    bank = float(cfg["bank_max_m"])
    spacing = max(2.0, float(cfg["fit_sample_m"]))
    nx = max(2, int(math.ceil(sx / spacing)) + 1)
    ny = max(2, int(math.ceil(sy / spacing)) + 1)
    nx = min(nx, 24)
    ny = min(ny, 24)
    good = np.ones((ny, nx), dtype=bool)
    n_bad = 0
    for iy in range(ny):
        ty = (iy + 0.5) / ny
        y = cy - 0.5 * sy + ty * sy
        for ix in range(nx):
            tx = (ix + 0.5) / nx
            x = cx - 0.5 * sx + tx * sx
            tz = float(z_at(x, y))
            if (surface_z - tz) > hang or (tz - surface_z) > bank:
                good[iy, ix] = False
                n_bad += 1
    total = nx * ny
    return (float(n_bad) / float(total) if total else 0.0), good, nx, ny


def _shrink_to_good(
    block: dict, good: np.ndarray, nx: int, ny: int, cfg: dict
) -> dict | None:
    """Crop WaterBlock XY to the bounding box of good sample cells."""
    if not np.any(good):
        return None
    rows = np.any(good, axis=1)
    cols = np.any(good, axis=0)
    iy0 = int(np.argmax(rows))
    iy1 = int(ny - 1 - np.argmax(rows[::-1]))
    ix0 = int(np.argmax(cols))
    ix1 = int(nx - 1 - np.argmax(cols[::-1]))
    cx, cy, sx, sy, surface_z = _block_xy(block)
    # Cell centers span the footprint; map cell index range back to meters.
    x0 = cx - 0.5 * sx + ((ix0 + 0.0) / nx) * sx
    x1 = cx - 0.5 * sx + ((ix1 + 1.0) / nx) * sx
    y0 = cy - 0.5 * sy + ((iy0 + 0.0) / ny) * sy
    y1 = cy - 0.5 * sy + ((iy1 + 1.0) / ny) * sy
    new_sx = max(0.0, x1 - x0)
    new_sy = max(0.0, y1 - y0)
    if new_sx * new_sy < float(cfg["min_area_m2"]) * 0.25:
        return None
    if new_sx < float(cfg["fit_min_side_m"]) or new_sy < float(cfg["fit_min_side_m"]):
        return None
    out = copy.deepcopy(block)
    out["position"] = [0.5 * (x0 + x1), 0.5 * (y0 + y1), surface_z]
    out["scale"] = [new_sx, new_sy, float(block["scale"][2])]
    out["persistentId"] = str(uuid.uuid4())
    return out


def _subdivide_2x2(block: dict) -> list[dict]:
    """Split one AABB WaterBlock into four half-size quads."""
    cx, cy, sx, sy, surface_z = _block_xy(block)
    depth = float(block["scale"][2])
    hx, hy = 0.5 * sx, 0.5 * sy
    name = str(block.get("name") or "water")
    kids: list[dict] = []
    for qi, (ox, oy) in enumerate(((-0.5, -0.5), (0.5, -0.5), (-0.5, 0.5), (0.5, 0.5))):
        child = copy.deepcopy(block)
        child["name"] = f"{name}_q{qi}"
        child["position"] = [cx + ox * hx, cy + oy * hy, surface_z]
        child["scale"] = [hx, hy, depth]
        child["persistentId"] = str(uuid.uuid4())
        kids.append(child)
    return kids


def _fit_terrain_one(block: dict, z_at, cfg: dict, *, depth: int = 0) -> list[dict]:
    """Keep / shrink / subdivide / drop one WaterBlock vs terrain basin."""
    cx, cy, sx, sy, surface_z = _block_xy(block)
    min_side = float(cfg["fit_min_side_m"])
    bad_frac, good, nx, ny = _terrain_bad_mask(z_at, cx, cy, sx, sy, surface_z, cfg)
    thr = float(cfg["fit_bad_frac"])
    if bad_frac <= thr:
        if bad_frac > 0.0 and cfg.get("fit_shrink", True):
            shrunk = _shrink_to_good(block, good, nx, ny, cfg)
            if shrunk is not None:
                # Re-check once after shrink; avoid infinite recursion.
                bf2, _, _, _ = _terrain_bad_mask(
                    z_at,
                    float(shrunk["position"][0]),
                    float(shrunk["position"][1]),
                    float(shrunk["scale"][0]),
                    float(shrunk["scale"][1]),
                    float(shrunk["position"][2]),
                    cfg,
                )
                if bf2 <= thr:
                    return [shrunk]
        return [block]

    # Too many bad cells: subdivide if large enough.
    if (
        cfg.get("fit_subdivide", True)
        and depth < 3
        and min(sx, sy) >= 2.0 * min_side
    ):
        kept: list[dict] = []
        for child in _subdivide_2x2(block):
            kept.extend(_fit_terrain_one(child, z_at, cfg, depth=depth + 1))
        return kept

    # Last resort: shrink to good core, else drop.
    if cfg.get("fit_shrink", True):
        shrunk = _shrink_to_good(block, good, nx, ny, cfg)
        if shrunk is not None:
            bf2, _, _, _ = _terrain_bad_mask(
                z_at,
                float(shrunk["position"][0]),
                float(shrunk["position"][1]),
                float(shrunk["scale"][0]),
                float(shrunk["scale"][1]),
                float(shrunk["position"][2]),
                cfg,
            )
            if bf2 <= thr:
                return [shrunk]
    return []


def _clamp_meshroad_depth(
    block: dict, meshroads: list[dict], cfg: dict
) -> dict | None:
    """Reduce scale.z so the volume stays above any MeshRoad under the footprint."""
    if not meshroads:
        return block
    cx, cy, sx, sy, surface_z = _block_xy(block)
    depth = float(block["scale"][2])
    clearance = float(cfg["meshroad_clearance_m"])
    min_depth = float(cfg["fit_min_depth_m"])
    max_depth = depth
    hit_any = False
    for x, y in _sample_grid(cx, cy, sx, sy, float(cfg["fit_sample_m"])):
        z_road = _nearest_meshroad_under(x, y, meshroads, surface_z)
        if z_road is None:
            continue
        hit_any = True
        # Surface already at/under the deck → cannot keep this block.
        if surface_z <= z_road + clearance:
            return None
        allowed = surface_z - (z_road + clearance)
        max_depth = min(max_depth, allowed)
    if not hit_any:
        return block
    if max_depth < min_depth:
        return None
    if max_depth >= depth - 1e-3:
        return block
    out = copy.deepcopy(block)
    out["scale"] = [sx, sy, float(max_depth)]
    out["persistentId"] = str(uuid.uuid4())
    return out


def fit_water_blocks(
    site: dict,
    level_name: str,
    entries: list[dict],
    cfg: dict,
    z_at=None,
) -> list[dict]:
    """Post-pass: terrain basin fit + MeshRoad depth clamp on WaterBlocks."""
    if not cfg.get("fit_check", True):
        return entries

    if z_at is None:
        z_at, _ = bg.load_terrain_z_slope(site)

    proc = processed_dir(site)
    meshroads = _load_meshroads(proc, level_name)
    if not meshroads:
        print(
            "water fit: no MeshRoad items (run bridges/galleries first) — "
            "terrain-only check"
        )

    out: list[dict] = []
    n_keep = n_sub = n_shrink = n_drop_terrain = n_clamp = n_drop_road = 0
    for e in entries:
        if e.get("class") != "WaterBlock":
            out.append(e)
            continue
        fitted = _fit_terrain_one(e, z_at, cfg)
        if not fitted:
            n_drop_terrain += 1
            print(f"water fit drop (terrain): {e.get('name')}")
            continue
        if len(fitted) > 1:
            n_sub += 1
        elif fitted[0] is not e and (
            abs(float(fitted[0]["scale"][0]) - float(e["scale"][0])) > 0.05
            or abs(float(fitted[0]["scale"][1]) - float(e["scale"][1])) > 0.05
        ):
            n_shrink += 1
        else:
            n_keep += 1

        for fb in fitted:
            clamped = _clamp_meshroad_depth(fb, meshroads, cfg)
            if clamped is None:
                n_drop_road += 1
                print(f"water fit drop (meshroad): {fb.get('name')}")
                continue
            if float(clamped["scale"][2]) < float(fb["scale"][2]) - 1e-3:
                n_clamp += 1
                print(
                    f"water fit depth clamp: {fb.get('name')} "
                    f"{float(fb['scale'][2]):.2f} -> {float(clamped['scale'][2]):.2f} m"
                )
            out.append(clamped)

    print(
        f"water fit: keep~{n_keep} subdivide={n_sub} shrink={n_shrink} "
        f"depth_clamp={n_clamp} drop_terrain={n_drop_terrain} "
        f"drop_meshroad={n_drop_road} meshroads={len(meshroads)} "
        f"-> {sum(1 for x in out if x.get('class') == 'WaterBlock')} WaterBlocks"
    )
    return out


def build_entries(site: dict, cfg: dict) -> list[dict]:
    proc = processed_dir(site)
    coords = SiteCoords(site)
    to_crs = Transformer.from_crs("EPSG:4326", coords.crs, always_xy=True)
    z_at, _ = bg.load_terrain_z_slope(site)

    entries: list[dict] = []
    n_block = n_river = n_skip = n_wide = 0
    wide_min = float(cfg["wide_flowing_min_width_m"])
    for i, (kind, feat) in enumerate(_load_water_features(proc)):
        props = feat.get("properties") or {}
        oid = props.get("OBJECTID") or props.get("objectid") or i
        for ri, ring in enumerate(_rings_wgs84(feat.get("geometry") or {})):
            xy = _to_beamng(ring, to_crs, coords)
            area = abs(_poly_area(xy))
            if area < float(cfg["min_area_m2"]):
                continue
            name = f"water_{kind}_{oid}_{ri}"
            as_standing = kind == "water_standing"
            if kind == "water_flowing" and wide_min > 0 and _pca_width_m(xy) >= wide_min:
                as_standing = True
                n_wide += 1

            if as_standing:
                if cfg["standing"] != "waterblock":
                    n_skip += 1
                    continue
                blocks = _standing_water_blocks(name, xy, z_at, cfg)
                if not blocks:
                    n_skip += 1
                    continue
                entries.extend(blocks)
                n_block += len(blocks)
                continue

            if cfg["flowing"] != "river":
                n_skip += 1
                continue
            river = _river(name, xy, z_at, cfg)
            if river is not None:
                entries.append(river)
                n_river += 1
            else:
                n_skip += 1
    print(
        f"Water objects: WaterBlock={n_block} River={n_river} "
        f"skipped={n_skip} wide_flowing->block={n_wide} "
        f"(flowing={cfg['flowing']}, standing={cfg['standing']})"
    )
    return entries


def write_level(level_name: str, entries: list[dict]) -> Path | None:
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        print(f"Level folder missing: {user_level}")
        return None
    group_dir = user_level / "main" / "MissionGroup" / "level_objects" / "Water"
    group_dir.mkdir(parents=True, exist_ok=True)
    items_path = group_dir / "items.level.json"
    with items_path.open("w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")

    lo_items = user_level / "main" / "MissionGroup" / "level_objects" / "items.level.json"
    lines = []
    if lo_items.is_file() and lo_items.stat().st_size:
        lines = [ln for ln in lo_items.read_text(encoding="utf-8").splitlines() if ln.strip()]
    names = set()
    for ln in lines:
        try:
            names.add(json.loads(ln).get("name"))
        except json.JSONDecodeError:
            pass
    if "Water" not in names:
        lines.append(
            json.dumps(
                {
                    "name": "Water",
                    "class": "SimGroup",
                    "__parent": "level_objects",
                    "enabled": "1",
                    "persistentId": str(uuid.uuid4()),
                },
                separators=(",", ":"),
            )
        )
        lo_items.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("Registered SimGroup Water under level_objects")
    return items_path


def _standing_rings_px(
    site: dict, size: int, extent: float, min_area_m2: float
) -> list[list[tuple[float, float]]]:
    """LN-GWS polygon rings in heightmap pixel space (only true standing water)."""
    proc = processed_dir(site)
    coords = SiteCoords(site)
    to_crs = Transformer.from_crs("EPSG:4326", coords.crs, always_xy=True)
    rings_px: list[list[tuple[float, float]]] = []
    for kind, feat in _load_water_features(proc):
        if kind != "water_standing":
            continue
        for ring in _rings_wgs84(feat.get("geometry") or {}):
            xy = _to_beamng(ring, to_crs, coords)
            if abs(_poly_area(xy)) < min_area_m2:
                continue
            pts = []
            for bx, by in xy:
                px = float(bx) / extent * (size - 1)
                py = (1.0 - float(by) / extent) * (size - 1)
                pts.append((px, py))
            if len(pts) >= 3:
                rings_px.append(pts)
    return rings_px


def _load_elev_m(site: dict, level_name: str) -> tuple[np.ndarray, float, int, float, Path]:
    """Load DGM meters (proposals compose separately; never stack on a bake)."""
    import heightmap_layers as hml

    _ = level_name
    proc = processed_dir(site)
    bng = site.get("beamng") or {}
    size = int(bng.get("mask_size") or 512)
    mpp = float(bng.get("meters_per_pixel") or 1.0)
    extent = float(size) * mpp
    elev, max_h = hml.load_dgm(proc, size)
    return elev, max_h, size, extent, proc / f"heightmap_{size}.png"


def depress_standing_lakes(
    site: dict,
    level_name: str,
    cfg: dict,
) -> Path | None:
    """Lower terrain under LN-GWS lakes so WaterBlocks sit in a basin.

    Soft shore via Gaussian blur of the standing mask. Does not touch flowing
    floodplain mud. Writes heightmap_{N}_water.png and syncs level import/.
    """
    from PIL import Image, ImageDraw, ImageFilter

    depress = float(cfg["standing_depress_m"])
    if depress <= 0:
        return None

    elev, max_h, size, extent, src_path = _load_elev_m(site, level_name)
    rings = _standing_rings_px(site, size, extent, float(cfg["min_area_m2"]))
    if not rings:
        print("standing_depress: no LN-GWS polygons — skip")
        return None

    mask_img = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask_img)
    for pts in rings:
        draw.polygon([(float(x), float(y)) for x, y in pts], outline=255, fill=255)

    blend_m = max(0.0, float(cfg["standing_depress_blend_m"]))
    if blend_m > 0:
        # ~1 px ≈ mpp meters; radius ≈ half blend width.
        radius = max(1.0, 0.5 * blend_m / max(extent / max(size - 1, 1), 1e-6))
        weight_img = mask_img.filter(ImageFilter.GaussianBlur(radius=radius))
    else:
        weight_img = mask_img
    weight = np.asarray(weight_img, dtype=np.float64) / 255.0
    n_core = int(np.count_nonzero(np.asarray(mask_img) > 0))
    if n_core == 0:
        print("standing_depress: empty mask — skip")
        return None

    import heightmap_layers as hml

    proc = processed_dir(site)
    delta = np.full(weight.shape, -float(depress), dtype=np.float64)
    hml.write_add_layer(proc, "water", delta, weight, max_h=max_h, size=size)
    carved_path = hml.compose(proc, size=size, max_h=max_h, level_name=level_name)

    # Preview: blue = depressed weight
    preview = np.zeros((size, size, 3), dtype=np.uint8)
    preview[..., 0] = np.clip(weight * 40, 0, 255).astype(np.uint8)
    preview[..., 1] = np.clip(weight * 120, 0, 255).astype(np.uint8)
    preview[..., 2] = np.clip(weight * 255, 0, 255).astype(np.uint8)
    Image.fromarray(preview, mode="RGB").save(proc / "preview_water_depress.png")

    print(
        f"Standing lake depress: -{depress:.1f}m on {n_core} px "
        f"(blend={blend_m:.1f}m, src={src_path.name}) -> {carved_path.name}"
    )
    return carved_path


def main() -> None:
    site = load_site()
    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "").strip()
    if not level_name:
        raise SystemExit("beamng.level_name missing")
    cfg = _cfg(bng)
    if not cfg["enabled"]:
        raise SystemExit("water.enabled is false")

    proc = processed_dir(site)
    # Basin first so import HM is ready; WaterBlock Z still samples pristine surface.
    depress_standing_lakes(site, level_name, cfg)

    entries = build_entries(site, cfg)
    z_at, _ = bg.load_terrain_z_slope(site)
    entries = fit_water_blocks(site, level_name, entries, cfg, z_at=z_at)
    out = proc / "water_items.level.json"
    with out.open("w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")
    print(f"Wrote {out} ({len(entries)} objects)")

    injected = write_level(level_name, entries)
    if injected:
        print(f"Injected: {injected}")
        # Verify Fernstein-sized lakes landed at expected Z (editor must not overwrite).
        for e in entries:
            if "401334" in str(e.get("name") or ""):
                p, s = e["position"], e["scale"]
                print(
                    f"Fernsteinsee block {e['name']}: "
                    f"posZ={p[2]:.3f} xy=({p[0]:.1f},{p[1]:.1f}) "
                    f"scaleXY=({s[0]:.1f},{s[1]:.1f}) depth={s[2]:.1f}"
                )
        # Read-back from disk (catches BeamNG editor holding old MissionGroup).
        try:
            disk = [
                json.loads(ln)
                for ln in injected.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]
            for e in disk:
                if "401334" in str(e.get("name") or ""):
                    print(f"Disk verify 401334 posZ={e['position'][2]:.3f}")
        except OSError as ex:
            print(f"Disk verify failed: {ex}")
        print(
            "Reload the level in BeamNG WITHOUT saving the old MissionGroup "
            "(World Editor can overwrite Water items with stale Z)."
        )


if __name__ == "__main__":
    main()
