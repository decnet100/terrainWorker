"""Inject BeamNG WaterBlock / River from landcover lakes + Gewässernetz lines.

Standing water (LN-GWS) → WaterBlock(s) + optional heightmap basin carve.
Terrain Mud stays as shore/bed paint; reflective water needs WaterBlock meshes.

Large lakes: shrink by shore_inset_m, then tile WaterBlocks that overlap the
core. standing_depress_m lowers the DGM under LN-GWS so blocks sit in a hole.
WaterBlock Z comes from densified shoreline samples (not centroid — islands).

After placement, fit_check trims / subdivides / drops blocks that hang over
terrain steps or punch through MeshRoad decks (tunnels under lakes). Run
bridges/galleries before water so MeshRoad NDJSON exists.

Flowing water: Tirol Gewässernetz centerlines (sources.waterways, fetch_waterways.py)
→ River nodes. Landcover LN-GWF stays Mud only (floodplain polygons are too wide).
Each node then snaps sideways onto the lowest heightmap sample within
river_snap_halfwidth_m (thalweg). Segments steeper than river_max_slope_deg
are dropped (waterfall gap).

Seasonal stages live in beamng.water.stages. Bake uses beamng.water.stage
(default spring = high water). Other stages are written to water_stages.json
so a GE script can lower Z / width without a rebuild.

Usage:
  $env:AUTOROAD_SITE='config/sites/fernpass.yaml'
  python tools/fetch_waterways.py
  python tools/build_water.py
"""
from __future__ import annotations

import copy
import json
import math
import re
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

# BeamNG WaterObject look — same fields as a River saved from the editor
# (e.g. bl_giau). Without these the mesh exists but does not render.
_WATER_LOOK = {
    "baseColor": [45, 108, 171, 255],
    "cubemap": "DefaultSkyCubemap",
    "rippleTex": "core/art/water/ripple.dds",
    "foamTex": "core/art/water/foam.dds",
    "depthGradientTex": "core/art/water/depthcolor_ramp.png",
    "waterFogDensity": 2,
    "waterFogDensityOffset": 0.5,
    "Foam": [{}, {}],
    "Ripples (texture animation)": [
        {"rippleDir": [0, 1], "rippleSpeed": -0.065, "rippleTexScale": [7.14, 7.14]},
        {"rippleDir": [0.707, 0.707], "rippleSpeed": 0.09, "rippleTexScale": [6.25, 12.5]},
        {"rippleDir": [0.5, 0.86], "rippleSpeed": 0.04, "rippleTexScale": [50, 50]},
    ],
    "Waves (vertex undulation)": [
        {"waveDir": [0, 1], "waveSpeed": 1},
        {"waveDir": [0.707, 0.707], "waveSpeed": 1},
        {"waveDir": [0.5, 0.86], "waveSpeed": 1},
    ],
}


def _with_water_look(obj: dict) -> dict:
    out = dict(obj)
    for key, val in _WATER_LOOK.items():
        out.setdefault(key, copy.deepcopy(val) if isinstance(val, (list, dict)) else val)
    return out

_DEFAULT_STAGES = {
    "spring": {
        "months": [3, 4, 5],
        "surface_dz_m": 0.0,
        "width_scale": 1.0,
        "depth_scale": 1.0,
        "flow_mps": 2.5,
    },
    "summer": {
        "months": [6, 7, 8],
        "surface_dz_m": -0.35,
        "width_scale": 0.85,
        "depth_scale": 0.85,
        "flow_mps": 1.6,
    },
    "autumn": {
        "months": [9, 10, 11],
        "surface_dz_m": -0.7,
        "width_scale": 0.7,
        "depth_scale": 0.7,
        "flow_mps": 1.2,
    },
    "winter": {
        "months": [12, 1, 2],
        "surface_dz_m": -0.9,
        "width_scale": 0.55,
        "depth_scale": 0.6,
        "flow_mps": 0.8,
    },
}

_DEFAULT_WIDTH_BY_TYP = {
    "fluss": 12.0,
    "bach": 4.0,
    "wildbach": 3.5,
    "graben": 2.0,
    "kanal": 6.0,
    "fließgewässer": 6.0,
    "fliesgewasser": 6.0,
    "< 10 km2 gewasser": 4.0,
    "< 10 km2 gewässer": 4.0,
    "10 km2 gewasser": 6.0,
    "10 km2 gewässer": 6.0,
    "100 km2 gewasser": 12.0,
    "100 km2 gewässer": 12.0,
    "1000 km2 gewasser": 20.0,
    "1000 km2 gewässer": 20.0,
}


def _normalize_stages(raw) -> dict[str, dict]:
    out = {k: dict(v) for k, v in _DEFAULT_STAGES.items()}
    if not isinstance(raw, dict):
        return out
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        key = str(name).strip().lower()
        cur = dict(out.get(key) or _DEFAULT_STAGES.get("spring"))
        if spec.get("months") is not None:
            months = spec["months"]
            if isinstance(months, (int, float)):
                cur["months"] = [int(months)]
            else:
                cur["months"] = [int(m) for m in months]
        for fld in ("surface_dz_m", "width_scale", "depth_scale", "flow_mps"):
            if spec.get(fld) is not None:
                cur[fld] = float(spec[fld])
        out[key] = cur
    return out


def _stage_spec(cfg: dict, name: str | None = None) -> dict:
    stages = cfg.get("stages") or _DEFAULT_STAGES
    key = str(name or cfg.get("stage") or "spring").lower()
    return dict(stages.get(key) or stages.get("spring") or _DEFAULT_STAGES["spring"])


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
        "surface_lift_m": float(raw.get("surface_lift_m") or 0.4),
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
        "river_max_width_m": float(raw.get("river_max_width_m") or 16.0),
        "river_depth_m": float(raw.get("river_depth_m") or 1.2),
        "river_node_step_m": float(raw.get("river_node_step_m") or 12.0),
        # Cross-track snap onto the DGM thalweg (0 = keep official midline).
        "river_snap_halfwidth_m": float(raw.get("river_snap_halfwidth_m") or 10.0),
        "river_snap_step_m": float(raw.get("river_snap_step_m") or 1.0),
        "river_max_slope_deg": float(raw.get("river_max_slope_deg") or 35.0),
        "river_depth_frac": float(raw.get("river_depth_frac") or 0.30),
        "stage": str(raw.get("stage") or "spring").lower(),
        "width_by_typ": dict(raw.get("width_by_typ") or {}),
        "stages": _normalize_stages(raw.get("stages")),
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
    return _with_water_look(obj)


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


def _norm_typ(text: str) -> str:
    s = str(text or "").strip().lower().replace("²", "2")
    return s


def _width_for_props(props: dict, cfg: dict) -> float:
    table = {_norm_typ(k): float(v) for k, v in (cfg.get("width_by_typ") or {}).items()}
    # GRKATWRRL is the catchment class; GEW_TYP is almost always "Fließgewässer".
    for key in (props.get("GRKATWRRL"), props.get("GEW_TYP"), props.get("gew_typ")):
        hit = table.get(_norm_typ(key))
        if hit:
            return max(float(cfg["river_min_width_m"]), min(hit, float(cfg["river_max_width_m"])))
        hit = _DEFAULT_WIDTH_BY_TYP.get(_norm_typ(key))
        if hit:
            return max(float(cfg["river_min_width_m"]), min(hit, float(cfg["river_max_width_m"])))
    return max(float(cfg["river_min_width_m"]), min(6.0, float(cfg["river_max_width_m"])))


def _line_xy_crs(geom: dict) -> list[np.ndarray]:
    """Line parts as Nx2 arrays in the source CRS (already site CRS after fetch)."""
    gtype = (geom or {}).get("type")
    coords = (geom or {}).get("coordinates")
    parts: list[np.ndarray] = []
    if gtype == "LineString" and coords and len(coords) >= 2:
        parts.append(np.asarray([(float(c[0]), float(c[1])) for c in coords if len(c) >= 2], dtype=np.float64))
    elif gtype == "MultiLineString":
        for part in coords or []:
            if part and len(part) >= 2:
                parts.append(
                    np.asarray([(float(c[0]), float(c[1])) for c in part if len(c) >= 2], dtype=np.float64)
                )
    return [p for p in parts if p.shape[0] >= 2]


def _crs_line_to_beamng(xy: np.ndarray, coords: SiteCoords) -> np.ndarray:
    out = []
    for x, y in xy:
        bx, by = coords.crs_to_beamng(float(x), float(y))
        out.append((bx, by))
    return np.asarray(out, dtype=np.float64)


def _resample_xy(xy: np.ndarray, step_m: float) -> np.ndarray:
    if xy.shape[0] < 2:
        return xy
    seg = np.diff(xy, axis=0)
    leng = np.hypot(seg[:, 0], seg[:, 1])
    total = float(leng.sum())
    if total < 1e-3:
        return xy[:1]
    step = max(2.0, float(step_m))
    n = max(2, int(math.floor(total / step)) + 1)
    targets = np.linspace(0.0, total, n)
    cum = np.concatenate([[0.0], np.cumsum(leng)])
    pts = []
    j = 0
    for t in targets:
        while j + 1 < len(cum) and cum[j + 1] < t:
            j += 1
        span = cum[j + 1] - cum[j] if j + 1 < len(cum) else 0.0
        if span < 1e-9:
            pts.append(xy[min(j, len(xy) - 1)])
            continue
        u = (t - cum[j]) / span
        pts.append(xy[j] * (1.0 - u) + xy[min(j + 1, len(xy) - 1)] * u)
    return np.asarray(pts, dtype=np.float64)


def _polyline_perp(xy: np.ndarray, i: int) -> np.ndarray | None:
    if xy.shape[0] < 2:
        return None
    if i <= 0:
        d = xy[1] - xy[0]
    elif i >= xy.shape[0] - 1:
        d = xy[-1] - xy[-2]
    else:
        d = xy[i + 1] - xy[i - 1]
    leng = float(np.hypot(d[0], d[1]))
    if leng < 1e-6:
        return None
    return np.array((-d[1] / leng, d[0] / leng), dtype=np.float64)


def _snap_to_thalweg(
    xy: np.ndarray,
    z_at,
    halfwidth_m: float,
    step_m: float,
    *,
    max_xy: float,
) -> tuple[np.ndarray, list[float]]:
    """Move each vertex onto the lowest heightmap sample on the local perpendicular.

    Search axis stays the official tangent (not already-snapped neighbours), so
    one node cannot pull the next search sideways.
    """
    if halfwidth_m <= 0 or xy.shape[0] == 0:
        return xy, []
    step = max(0.25, float(step_m))
    n_off = int(math.ceil(float(halfwidth_m) / step))
    out = np.array(xy, dtype=np.float64, copy=True)
    shifts: list[float] = []
    for i, p in enumerate(xy):
        perp = _polyline_perp(xy, i)
        if perp is None:
            shifts.append(0.0)
            continue
        best_xy = np.asarray(p, dtype=np.float64)
        best_z = float(z_at(float(p[0]), float(p[1])))
        for k in range(-n_off, n_off + 1):
            q = p + (k * step) * perp
            if q[0] < 0.0 or q[1] < 0.0 or q[0] > max_xy or q[1] > max_xy:
                continue
            z = float(z_at(float(q[0]), float(q[1])))
            if z < best_z - 1e-4:
                best_z = z
                best_xy = q
        out[i] = best_xy
        shifts.append(float(np.hypot(*(best_xy - p))))
    return out, shifts


def _split_by_slope(xy: np.ndarray, z_at, max_deg: float) -> tuple[list[np.ndarray], int]:
    if xy.shape[0] < 2:
        return [], 0
    zs = [float(z_at(float(p[0]), float(p[1]))) for p in xy]
    segs: list[list[int]] = [[0]]
    n_drop = 0
    for i in range(1, len(xy)):
        dx = float(np.hypot(xy[i, 0] - xy[i - 1, 0], xy[i, 1] - xy[i - 1, 1]))
        dz = abs(zs[i] - zs[i - 1])
        slope = math.degrees(math.atan2(dz, max(dx, 1e-6)))
        if slope > max_deg:
            n_drop += 1
            segs.append([i])
        else:
            segs[-1].append(i)
    out = []
    for idx in segs:
        if len(idx) < 2:
            continue
        out.append(xy[np.asarray(idx, dtype=int)])
    return out, n_drop


def _line_length_m(xy: np.ndarray) -> float:
    if xy.shape[0] < 2:
        return 0.0
    d = np.diff(xy, axis=0)
    return float(np.hypot(d[:, 0], d[:, 1]).sum())


def _apply_stage_nodes(base_nodes: list[list[float]], stage: dict) -> list[list[float]]:
    dz = float(stage.get("surface_dz_m") or 0.0)
    ws = float(stage.get("width_scale") or 1.0)
    ds = float(stage.get("depth_scale") or 1.0)
    out = []
    for n in base_nodes:
        node = list(n)
        node[2] = float(n[2]) + dz
        node[3] = max(0.25, float(n[3]) * ws)
        node[4] = max(0.25, float(n[4]) * ds)
        out.append(node)
    return out


def _river_object_name(oid, pi: int, props: dict) -> str:
    raw = str(props.get("GEW_NAME") or "bach").split("(")[0].strip()
    slug = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_") or "bach"
    if len(slug) > 36:
        slug = slug[:36].rstrip("_")
    return f"river_{slug}_{oid}_{pi}"


def _river_from_line(
    name: str,
    xy: np.ndarray,
    z_at,
    width: float,
    cfg: dict,
    stage: dict,
) -> tuple[dict, list[list[float]]] | None:
    """River along a BeamNG-XY polyline. Returns (object, spring-base nodes)."""
    if xy.shape[0] < 2 or _line_length_m(xy) < float(cfg["river_min_length_m"]):
        return None
    lift = float(cfg["surface_lift_m"])
    depth = max(float(cfg["river_depth_m"]), float(width) * float(cfg["river_depth_frac"]))
    base: list[list[float]] = []
    for p in xy:
        z = float(z_at(float(p[0]), float(p[1]))) + lift
        base.append([float(p[0]), float(p[1]), z, float(width), float(depth), 0.0, 0.0, 1.0])
    if base[0][2] < base[-1][2]:
        base.reverse()
    nodes = _apply_stage_nodes(base, stage)
    obj: dict = {
        "name": name,
        "class": "River",
        "__parent": "water",
        "persistentId": str(uuid.uuid4()),
        "position": nodes[0][:3],
        "nodes": nodes,
        "segmentLength": float(cfg["segment_length"]),
        "subdivideLength": float(cfg["subdivide_length"]),
        "flowMagnitudePhysics": float(stage.get("flow_mps") or 1.5),
        "lowLODDistance": 4000.0,
    }
    mat = cfg.get("material") or ""
    if mat:
        obj["material"] = mat
    return _with_water_look(obj), base


def _load_waterway_lines(site: dict, coords: SiteCoords) -> list[tuple[dict, np.ndarray]]:
    try:
        import fetch_waterways as fw
    except ImportError:
        fw = None
    proc = processed_dir(site)
    path = proc / "waterways.geojson"
    data = None
    if fw is not None:
        data = fw.load_waterways(site)
    elif path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    feats = (data or {}).get("features") or []
    out: list[tuple[dict, np.ndarray]] = []
    for feat in feats:
        props = feat.get("properties") or {}
        for part in _line_xy_crs(feat.get("geometry") or {}):
            xy = _crs_line_to_beamng(part, coords)
            if xy.shape[0] >= 2:
                out.append((props, xy))
    return out


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


def build_entries(site: dict, cfg: dict) -> tuple[list[dict], list[dict]]:
    """Return (scene objects, stage sidecar object records)."""
    proc = processed_dir(site)
    coords = SiteCoords(site)
    to_crs = Transformer.from_crs("EPSG:4326", coords.crs, always_xy=True)
    z_at, _ = bg.load_terrain_z_slope(site)
    stage = _stage_spec(cfg)

    entries: list[dict] = []
    stage_objs: list[dict] = []
    n_block = n_river = n_skip = n_wide = n_steep = 0
    snap_shifts: list[float] = []
    snap_half = float(cfg["river_snap_halfwidth_m"])
    snap_step = float(cfg["river_snap_step_m"])
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
                dz = float(stage.get("surface_dz_m") or 0.0)
                if abs(dz) > 1e-6:
                    for blk in blocks:
                        blk["position"] = [
                            float(blk["position"][0]),
                            float(blk["position"][1]),
                            float(blk["position"][2]) + dz,
                        ]
                entries.extend(blocks)
                n_block += len(blocks)
                for blk in blocks:
                    stage_objs.append(
                        {
                            "name": blk["name"],
                            "class": "WaterBlock",
                            "base_z": float(blk["position"][2]) - dz,
                        }
                    )
                continue

            # LN-GWF floodplain: Mud only. Rivers come from Gewässernetz lines.
            n_skip += 1

    if cfg["flowing"] == "river":
        lines = _load_waterway_lines(site, coords)
        if not lines:
            print(
                "flowing=river but no waterways.geojson — "
                "run python tools\\fetch_waterways.py"
            )
        step = float(cfg["river_node_step_m"])
        max_slope = float(cfg["river_max_slope_deg"])
        max_xy = float(int((site.get("beamng") or {}).get("mask_size") or 512) - 1) * float(
            (site.get("beamng") or {}).get("meters_per_pixel") or 1.0
        )
        for i, (props, xy) in enumerate(lines):
            oid = props.get("OBJECTID") or props.get("GEW_ID") or i
            width = _width_for_props(props, cfg)
            sampled = _resample_xy(xy, step)
            sampled, shifts = _snap_to_thalweg(
                sampled, z_at, snap_half, snap_step, max_xy=max_xy
            )
            snap_shifts.extend(shifts)
            parts, n_drop = _split_by_slope(sampled, z_at, max_slope)
            n_steep += n_drop
            for pi, part in enumerate(parts):
                name = _river_object_name(oid, pi, props)
                built = _river_from_line(name, part, z_at, width, cfg, stage)
                if built is None:
                    n_skip += 1
                    continue
                obj, base = built
                entries.append(obj)
                stage_objs.append(
                    {
                        "name": obj["name"],
                        "class": "River",
                        "gew_id": props.get("GEW_ID"),
                        "gew_name": props.get("GEW_NAME"),
                        "gew_typ": props.get("GEW_TYP"),
                        "base_nodes": base,
                        "base_flow": float((_DEFAULT_STAGES.get("spring") or {}).get("flow_mps") or 2.5),
                    }
                )
                n_river += 1
    if snap_shifts:
        arr = np.asarray(snap_shifts, dtype=np.float64)
        print(
            f"river thalweg snap: half={snap_half:.1f}m step={snap_step:.1f}m "
            f"mean={float(arr.mean()):.2f}m p95={float(np.percentile(arr, 95)):.2f}m "
            f"max={float(arr.max()):.2f}m moved={int(np.count_nonzero(arr > 0.25))}/{arr.size}"
        )
    print(
        f"Water objects: WaterBlock={n_block} River={n_river} "
        f"skipped={n_skip} steep_spans={n_steep} wide_flowing->block={n_wide} "
        f"(flowing={cfg['flowing']}, standing={cfg['standing']}, "
        f"stage={cfg['stage']})"
    )
    return entries, stage_objs


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")


def _ensure_simgroup(items_path: Path, name: str, parent: str) -> bool:
    lines: list[str] = []
    if items_path.is_file() and items_path.stat().st_size:
        lines = [ln for ln in items_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    names = set()
    for ln in lines:
        try:
            names.add(json.loads(ln).get("name"))
        except json.JSONDecodeError:
            pass
    if name in names:
        return False
    items_path.parent.mkdir(parents=True, exist_ok=True)
    lines.append(
        json.dumps(
            {
                "name": name,
                "class": "SimGroup",
                "__parent": parent,
                "enabled": "1",
                "persistentId": str(uuid.uuid4()),
            },
            separators=(",", ":"),
        )
    )
    items_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def write_level(level_name: str, entries: list[dict]) -> Path | None:
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        print(f"Level folder missing: {user_level}")
        return None
    rivers = [e for e in entries if e.get("class") == "River"]
    blocks = [e for e in entries if e.get("class") != "River"]
    for e in rivers:
        e["__parent"] = "water"
    for e in blocks:
        e["__parent"] = "Water"

    # Official maps keep River under MissionGroup/water (River Editor looks there).
    river_dir = user_level / "main" / "MissionGroup" / "water"
    river_items = river_dir / "items.level.json"
    _write_jsonl(river_items, rivers)
    mg_items = user_level / "main" / "MissionGroup" / "items.level.json"
    if _ensure_simgroup(mg_items, "water", "MissionGroup"):
        print("Registered SimGroup water under MissionGroup")

    # Standing WaterBlocks stay where the existing Imst tree already shows them.
    block_dir = user_level / "main" / "MissionGroup" / "level_objects" / "Water"
    block_items = block_dir / "items.level.json"
    _write_jsonl(block_items, blocks)
    lo_items = user_level / "main" / "MissionGroup" / "level_objects" / "items.level.json"
    if _ensure_simgroup(lo_items, "Water", "level_objects"):
        print("Registered SimGroup Water under level_objects")
    print(f"Rivers -> {river_items} ({len(rivers)})")
    print(f"WaterBlocks -> {block_items} ({len(blocks)})")
    return river_items if rivers else block_items


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
                # Pixel c is drawn at c * square_m (extent / size).
                px = float(bx) / extent * size
                py = (size - 1) - float(by) / extent * size
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
        radius = max(1.0, 0.5 * blend_m / max(extent / max(size, 1), 1e-6))
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

    entries, stage_objs = build_entries(site, cfg)
    z_at, _ = bg.load_terrain_z_slope(site)
    entries = fit_water_blocks(site, level_name, entries, cfg, z_at=z_at)
    kept_names = {str(e.get("name")) for e in entries}
    stage_objs = [o for o in stage_objs if str(o.get("name")) in kept_names]
    out = proc / "water_items.level.json"
    with out.open("w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")
    print(f"Wrote {out} ({len(entries)} objects)")
    stages_payload = {
        "stage": cfg["stage"],
        "attribution": "Land Tirol — Gewässernetz, CC BY 3.0 AT",
        "stages": cfg["stages"],
        "objects": stage_objs,
    }
    stages_path = proc / "water_stages.json"
    stages_path.write_text(json.dumps(stages_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {stages_path} ({len(stage_objs)} staged objects)")

    injected = write_level(level_name, entries)
    user_level = USER_LEVELS / level_name
    if user_level.is_dir():
        (user_level / "water_stages.json").write_text(
            json.dumps(stages_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
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
