"""Break edges on the raw 0.5 m DGM, chosen against the Landnutzung polygon.

Along each main-road OID, every 1 m, a cross-section on the unfiltered DGM
records the first sustained slope break. The same perpendicular measures the
Landnutzung half-width. The break wins when it lies within 2 m of that
boundary, and when it lies further inside a wider polygon with a sharp angle.
Junctions, bridge spans and galleries keep the polygon. Both offsets stay in
the table. Mesh and guardrails are not changed.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\measure_road_breaks.py

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\measure_road_breaks.py --heights C:\\Users\\chdem\\Documents\\filtered_7c_10d.tif

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\measure_road_breaks.py --heights C:\\Users\\chdem\\Documents\\filtered_7c_10d.tif --tol 4
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import tifffile as tiff
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

import build_decal_roads as bdr
from build_asphalt_meshroads import _is_main_road
from build_road_grid import _arg_value, _ensure_raw_mosaic, _load_geotiff
from diag_road_bed_modes import _gip_polylines
from guardrail_junctions import find_junctions
from measure_gip_widths import _ln_to_31254, _pick_poly, find_landnutzung_geojson, load_street_polygons
from raster_tile_fetch import elevation_nodata_mask
from site_coords import SiteCoords, load_site, processed_dir

RES_M = 0.5
STATION_M = 1.0
SEARCH_M = 12.0
MIN_BREAK_M = 1.5
BREAK_DEG = 10.0
SUSTAIN_DEG = 8.0
SUSTAIN_M = 1.5
INNER_LO_M = 0.5
INNER_HI_M = 2.0
MEDIAN_RADIUS = 3
POLY_TOL_M = 2.0
INSIDE_ANGLE_DEG = 15.0
SLEW_M = 0.4
GAP_HOLD_M = 12.0
POLY_RAY_M = 20.0
JUNCTION_M = 12.0
JUNCTION_JOIN_M = 4.0
Z_SEP_M = 3.0


def _oid(road: dict, index: int) -> str:
    raw = road.get("objectid")
    if raw is None:
        raw = road.get("id")
    try:
        text = str(int(raw))
    except (TypeError, ValueError):
        text = str(raw)
    return f"{text}#{index}"


def _oid_num(label: str) -> str:
    return label.split("#", 1)[0]


def _densify(pts: list, sc: SiteCoords, step: float) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Stations every ``step`` ground metres. Returns s, BeamNG xy, CRS xy."""
    if len(pts) < 2:
        return None
    bxy = np.array([[float(p[0]), float(p[1])] for p in pts], dtype=np.float64)
    cxy = np.array([sc.beamng_to_crs(float(p[0]), float(p[1])) for p in pts], dtype=np.float64)
    seg = np.hypot(np.diff(cxy[:, 0]), np.diff(cxy[:, 1]))
    total = float(seg.sum())
    if total < step:
        return None
    ss = np.arange(0.0, total + 1e-6, step)
    if total - float(ss[-1]) > 0.25:
        ss = np.append(ss, total)
    else:
        ss[-1] = total
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    idx = np.clip(np.searchsorted(cum, ss, side="right") - 1, 0, len(seg) - 1)
    seg_len = seg[idx]
    t = np.divide(ss - cum[idx], seg_len, out=np.zeros_like(ss), where=seg_len > 1e-9)
    c = cxy[idx] + (cxy[idx + 1] - cxy[idx]) * t[:, None]
    b = bxy[idx] + (bxy[idx + 1] - bxy[idx]) * t[:, None]
    return ss, b, c


def _tangents(xy: np.ndarray) -> np.ndarray:
    n = xy.shape[0]
    out = np.zeros((n, 2), dtype=np.float64)
    for i in range(n):
        i0 = max(0, i - 1)
        i1 = min(n - 1, i + 1)
        dx = float(xy[i1, 0] - xy[i0, 0])
        dy = float(xy[i1, 1] - xy[i0, 1])
        length = math.hypot(dx, dy) or 1.0
        out[i, 0] = dx / length
        out[i, 1] = dy / length
    return out


def _sample_z(z: np.ndarray, xmin: float, ymax: float, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    height, width = z.shape
    fc = (xs - xmin) / RES_M - 0.5
    fr = (ymax - ys) / RES_M - 0.5
    c0 = np.floor(fc).astype(np.int32)
    r0 = np.floor(fr).astype(np.int32)
    tx = fc - c0
    ty = fr - r0
    acc = np.zeros(xs.shape, dtype=np.float64)
    weight = np.zeros(xs.shape, dtype=np.float64)
    for dr, dc, wx, wy in (
        (0, 0, 1.0 - tx, 1.0 - ty),
        (0, 1, tx, 1.0 - ty),
        (1, 0, 1.0 - tx, ty),
        (1, 1, tx, ty),
    ):
        rr = r0 + dr
        cc = c0 + dc
        wgt = wx * wy
        valid = (wgt > 0.0) & (rr >= 0) & (cc >= 0) & (rr < height) & (cc < width)
        if not np.any(valid):
            continue
        samples = np.full(xs.shape, np.nan, dtype=np.float64)
        samples[valid] = z[rr[valid], cc[valid]]
        good = valid & np.isfinite(samples)
        acc[good] += wgt[good] * samples[good]
        weight[good] += wgt[good]
    out = np.full(xs.shape, np.nan, dtype=np.float64)
    ok = weight > 0.0
    out[ok] = acc[ok] / weight[ok]
    return out


def _find_breaks(
    profile: np.ndarray, break_deg: float = BREAK_DEG, sustain_deg: float = SUSTAIN_DEG
) -> tuple[np.ndarray, np.ndarray]:
    """First sustained slope break on profiles shaped (station, offset)."""
    n_sta, n_off = profile.shape
    offsets = np.arange(n_off, dtype=np.float64) * RES_M
    finite = np.isfinite(profile)
    dz = profile[:, 1:] - profile[:, :-1]
    seg_ok = finite[:, 1:] & finite[:, :-1]
    slope = np.divide(dz, RES_M, out=np.full_like(dz, np.nan), where=seg_ok)
    angle = np.degrees(np.arctan(slope))
    mid = offsets[:-1]
    inner = (mid + 0.5 * RES_M >= INNER_LO_M) & (mid + 0.5 * RES_M <= INNER_HI_M)
    inner_vals = np.where(inner[None, :], angle, np.nan)
    count = np.sum(np.isfinite(inner_vals), axis=1)
    inner_med = np.full(n_sta, np.nan, dtype=np.float64)
    enough = count >= 2
    if np.any(enough):
        inner_med[enough] = np.nanmedian(inner_vals[enough], axis=1)
    hold_n = max(1, int(round(SUSTAIN_M / RES_M)))
    found = np.zeros(n_sta, dtype=bool)
    out_off = np.full(n_sta, np.nan, dtype=np.float64)
    out_ang = np.full(n_sta, np.nan, dtype=np.float64)
    n_seg = angle.shape[1]
    for i, off in enumerate(mid):
        if off < MIN_BREAK_M or i + hold_n > n_seg:
            continue
        window = angle[:, i : i + hold_n]
        ok = (
            np.isfinite(inner_med)
            & np.isfinite(window).all(axis=1)
            & ~found
        )
        delta0 = np.abs(angle[:, i] - inner_med)
        sustain = np.min(np.abs(window - inner_med[:, None]), axis=1)
        hit = ok & (delta0 >= break_deg) & (sustain >= sustain_deg)
        out_off[hit] = off
        out_ang[hit] = delta0[hit]
        found |= hit
    return out_off, out_ang


def _smooth_offset(raw: np.ndarray, radius: int = MEDIAN_RADIUS) -> np.ndarray:
    out = raw.copy()
    n = raw.shape[0]
    for i in range(n):
        if not math.isfinite(float(raw[i])):
            continue
        window = raw[max(0, i - radius) : min(n, i + radius + 1)]
        window = window[np.isfinite(window)]
        if window.size >= 3:
            out[i] = float(np.median(window))
    return out


def _poly_offset(poly, x: float, y: float, dx: float, dy: float) -> float:
    if poly is None or not poly.covers(Point(x, y)):
        return float("nan")
    ray = LineString([(x, y), (x + dx * POLY_RAY_M, y + dy * POLY_RAY_M)])
    try:
        hit = poly.boundary.intersection(ray)
    except Exception:
        return float("nan")
    best: float | None = None

    def walk(geom) -> None:
        nonlocal best
        if geom is None or geom.is_empty:
            return
        kind = geom.geom_type
        if kind == "Point":
            dist = math.hypot(float(geom.x) - x, float(geom.y) - y)
            if dist > 0.05 and (best is None or dist < best):
                best = dist
        elif kind in ("MultiPoint", "GeometryCollection", "MultiLineString"):
            for part in geom.geoms:
                walk(part)
        elif kind in ("LineString", "LinearRing"):
            for cx, cy in geom.coords:
                dist = math.hypot(float(cx) - x, float(cy) - y)
                if dist > 0.05 and (best is None or dist < best):
                    best = dist

    walk(hit)
    if best is None:
        return float("nan")
    return float(best)


def _decide(
    break_m: float,
    break_deg: float,
    poly_m: float,
    hold: str,
    tol_m: float = POLY_TOL_M,
    break_min: float = BREAK_DEG,
) -> tuple[float, str, str]:
    has_poly = math.isfinite(poly_m)
    if hold:
        if has_poly:
            return poly_m, "polygon", hold
        return float("nan"), "none", hold
    has_break = math.isfinite(break_m) and math.isfinite(break_deg) and break_deg >= break_min
    if not has_break:
        if has_poly:
            return poly_m, "polygon", "no_break"
        return float("nan"), "none", "no_break"
    if not has_poly:
        return break_m, "break", "no_polygon"
    if abs(break_m - poly_m) <= tol_m:
        return break_m, "break", "near"
    if break_m < poly_m - tol_m and break_deg >= INSIDE_ANGLE_DEG:
        return break_m, "break", "inside_wide"
    if break_m > poly_m + tol_m:
        return poly_m, "polygon", "outside"
    return poly_m, "polygon", "inside_weak"


def _hold_mask(beam: np.ndarray, span_tree: STRtree | None, gallery_tree: STRtree | None, junctions: np.ndarray) -> list[str]:
    n = beam.shape[0]
    hold = [""] * n
    if span_tree is not None and len(span_tree.geometries) > 0:
        pts = np.array([Point(float(x), float(y)) for x, y in beam], dtype=object)
        hits = span_tree.query(pts, predicate="intersects")
        for i in np.unique(hits[0]):
            hold[int(i)] = "span"
    if gallery_tree is not None and len(gallery_tree.geometries) > 0:
        pts = np.array([Point(float(x), float(y)) for x, y in beam], dtype=object)
        hits = gallery_tree.query(pts, predicate="intersects")
        for i in np.unique(hits[0]):
            if not hold[int(i)]:
                hold[int(i)] = "gallery"
    if junctions.size:
        dist2 = (beam[:, None, 0] - junctions[None, :, 0]) ** 2 + (
            beam[:, None, 1] - junctions[None, :, 1]
        ) ** 2
        near = dist2.min(axis=1) <= JUNCTION_M * JUNCTION_M
        for i in np.flatnonzero(near):
            if not hold[int(i)]:
                hold[int(i)] = "junction"
    return hold


def _bands(corridors: list[tuple[list[tuple[float, float]], float]]) -> STRtree | None:
    geoms = []
    for xy, half in corridors:
        if len(xy) < 2:
            continue
        line = LineString(xy)
        if line.is_empty or line.length < 0.5:
            continue
        geoms.append(line.buffer(max(float(half), 0.5), cap_style="flat"))
    if not geoms:
        return None
    return STRtree(geoms)


def _side_profiles(
    z: np.ndarray,
    xmin: float,
    ymax: float,
    crs: np.ndarray,
    normal: np.ndarray,
) -> np.ndarray:
    offsets = np.arange(0.0, SEARCH_M + 1e-9, RES_M)
    xs = crs[:, None, 0] + normal[:, None, 0] * offsets[None, :]
    ys = crs[:, None, 1] + normal[:, None, 1] * offsets[None, :]
    return _sample_z(z, xmin, ymax, xs, ys)


def _lines_for_side(
    rows: list[dict],
    side: str,
    *,
    value_key: str | None = None,
    x_key: str | None = None,
    y_key: str | None = None,
    split_source: bool = True,
) -> list[dict]:
    value_key = value_key or f"{side}_chosen_m"
    x_key = x_key or f"{side}_x"
    y_key = y_key or f"{side}_y"
    runs: list[list[dict]] = []
    current: list[dict] = []
    prev_key = None
    prev_s = None
    for row in rows:
        chosen = row[value_key]
        if not math.isfinite(chosen):
            if current:
                runs.append(current)
                current = []
            prev_key = None
            prev_s = None
            continue
        key = (row["oid"], row[f"{side}_source"], row[f"{side}_reason"]) if split_source else (row["oid"],)
        if current and (key != prev_key or float(row["s_m"]) - float(prev_s) > 1.5):
            runs.append(current)
            current = []
        current.append(row)
        prev_key = key
        prev_s = row["s_m"]
    if current:
        runs.append(current)
    out = []
    for run in runs:
        if len(run) < 2:
            continue
        coords = [(float(r[x_key]), float(r[y_key])) for r in run]
        out.append(
            {
                "objectid": _oid_num(run[0]["oid"]),
                "piece": run[0]["oid"],
                "str_code": run[0]["str_code"],
                "side": side,
                "source": run[0][f"{side}_source"],
                "reason": run[0][f"{side}_reason"],
                "s0_m": round(float(run[0]["s_m"]), 2),
                "s1_m": round(float(run[-1]["s_m"]), 2),
                "stations": len(run),
                "geometry": LineString(coords),
            }
        )
    return out


def _fill_short_gaps(values: np.ndarray, station_s: np.ndarray, hold_m: float) -> np.ndarray:
    out = values.astype(np.float64).copy()
    n = out.shape[0]
    i = 0
    while i < n:
        if math.isfinite(float(out[i])):
            i += 1
            continue
        j = i
        while j < n and not math.isfinite(float(out[j])):
            j += 1
        left = float(out[i - 1]) if i > 0 else float("nan")
        right = float(out[j]) if j < n else float("nan")
        if i > 0 and j < n and math.isfinite(left) and math.isfinite(right):
            gap = float(station_s[j] - station_s[i - 1])
            if gap <= hold_m and gap > 1e-6:
                for k in range(i, j):
                    t = (float(station_s[k]) - float(station_s[i - 1])) / gap
                    out[k] = left + (right - left) * t
        elif i > 0 and math.isfinite(left):
            origin = float(station_s[i - 1])
            for k in range(i, j):
                if float(station_s[k]) - origin <= hold_m:
                    out[k] = left
        i = j
    return out


def _rate_limit(values: np.ndarray, step_m: np.ndarray, slew: float) -> np.ndarray:
    out = values.astype(np.float64).copy()
    last = float("nan")
    for i in range(out.shape[0]):
        if not math.isfinite(float(out[i])):
            last = float("nan")
            continue
        if math.isfinite(last):
            cap = slew * max(float(step_m[i]), 1e-3)
            out[i] = min(max(float(out[i]), last - cap), last + cap)
        last = float(out[i])
    last = float("nan")
    for i in range(out.shape[0] - 1, -1, -1):
        if not math.isfinite(float(out[i])):
            last = float("nan")
            continue
        if math.isfinite(last):
            ds = float(step_m[i + 1]) if i + 1 < step_m.shape[0] else 1.0
            cap = slew * max(ds, 1e-3)
            out[i] = min(max(float(out[i]), last - cap), last + cap)
        last = float(out[i])
    return out


def _apply_slew(rows: list[dict], slew: float = SLEW_M, hold_m: float = GAP_HOLD_M) -> None:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["oid"], []).append(row)
    for group in groups.values():
        station_s = np.array([float(row["s_m"]) for row in group], dtype=np.float64)
        step = np.empty(station_s.shape[0], dtype=np.float64)
        step[0] = 1.0
        if station_s.shape[0] > 1:
            step[1:] = np.maximum(np.diff(station_s), 1e-3)
        for side, sign in (("left", 1.0), ("right", -1.0)):
            chosen = np.array([float(row[f"{side}_chosen_m"]) for row in group], dtype=np.float64)
            filled = _fill_short_gaps(chosen, station_s, hold_m)
            limited = _rate_limit(filled, step, slew)
            for i, row in enumerate(group):
                value = float(limited[i])
                row[f"{side}_slew_m"] = value
                if math.isfinite(value):
                    row[f"{side}_sx"] = float(row["cx"]) + sign * float(row["lx"]) * value
                    row[f"{side}_sy"] = float(row["cy"]) + sign * float(row["ly"]) * value
                else:
                    row[f"{side}_sx"] = float("nan")
                    row[f"{side}_sy"] = float("nan")


def _jump_stats(rows: list[dict], side: str, key: str) -> dict:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["oid"], []).append(row)
    jumps: list[float] = []
    for group in groups.values():
        ordered = sorted(group, key=lambda row: float(row["s_m"]))
        for left, right in zip(ordered, ordered[1:]):
            ds = float(right["s_m"]) - float(left["s_m"])
            if ds <= 0.0 or ds > 1.5:
                continue
            a = float(left[key])
            b = float(right[key])
            if math.isfinite(a) and math.isfinite(b):
                jumps.append(abs(b - a))
    if not jumps:
        return {"pairs": 0}
    ordered_j = sorted(jumps)
    n = len(ordered_j)

    def pct(q: float) -> float:
        return ordered_j[min(n - 1, int(q * (n - 1)))]

    return {
        "pairs": n,
        "gt_1m": sum(1 for value in ordered_j if value > 1.0),
        "p95_m": round(pct(0.95), 3),
        "p99_m": round(pct(0.99), 3),
        "max_m": round(ordered_j[-1], 3),
    }


def _ribbons(rows: list[dict]) -> list[dict]:
    from shapely.geometry import Polygon
    from shapely import make_valid

    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["oid"], []).append(row)
    out: list[dict] = []

    def emit(run: list[dict]) -> None:
        if len(run) < 2:
            return
        coords = [(float(row["left_sx"]), float(row["left_sy"])) for row in run]
        coords += [(float(row["right_sx"]), float(row["right_sy"])) for row in reversed(run)]
        poly = Polygon(coords)
        if poly.is_empty:
            return
        if not poly.is_valid:
            poly = make_valid(poly)
        parts = [poly] if poly.geom_type == "Polygon" else [
            part for part in getattr(poly, "geoms", []) if part.geom_type == "Polygon"
        ]
        for part in parts:
            if part.area < 1.0:
                continue
            out.append(
                {
                    "objectid": _oid_num(run[0]["oid"]),
                    "piece": run[0]["oid"],
                    "str_code": run[0]["str_code"],
                    "s0_m": round(float(run[0]["s_m"]), 2),
                    "s1_m": round(float(run[-1]["s_m"]), 2),
                    "geometry": part,
                }
            )

    for group in groups.values():
        run: list[dict] = []
        prev_s = None
        for row in group:
            ok = math.isfinite(float(row["left_slew_m"])) and math.isfinite(float(row["right_slew_m"]))
            gap = prev_s is not None and float(row["s_m"]) - float(prev_s) > 1.5
            if run and (not ok or gap):
                emit(run)
                run = []
                prev_s = None
            if not ok:
                continue
            run.append(row)
            prev_s = float(row["s_m"])
        emit(run)
    return out


def _break_points(rows: list[dict], side: str, sign: float) -> list[dict]:
    out = []
    nx_key, ny_key = "lx", "ly"
    for row in rows:
        raw = row[f"{side}_break_raw_m"]
        if not math.isfinite(raw):
            continue
        used = row[f"{side}_break_m"]
        if not math.isfinite(used):
            used = raw
        out.append(
            {
                "objectid": _oid_num(row["oid"]),
                "piece": row["oid"],
                "str_code": row["str_code"],
                "side": side,
                "s_m": row["s_m"],
                "break_raw_m": round(float(raw), 3),
                "break_m": round(float(used), 3),
                "break_deg": round(float(row[f"{side}_break_deg"]), 2)
                if math.isfinite(row[f"{side}_break_deg"])
                else None,
                "poly_m": round(float(row[f"{side}_poly_m"]), 3)
                if math.isfinite(row[f"{side}_poly_m"])
                else None,
                "chosen_m": round(float(row[f"{side}_chosen_m"]), 3)
                if math.isfinite(row[f"{side}_chosen_m"])
                else None,
                "accepted": row[f"{side}_source"] == "break",
                "source": row[f"{side}_source"],
                "reason": row[f"{side}_reason"],
                "geometry": Point(
                    float(row["cx"]) + sign * float(row[nx_key]) * float(used),
                    float(row["cy"]) + sign * float(row[ny_key]) * float(used),
                ),
            }
        )
    return out


def _junction_xy(roads: list[dict]) -> np.ndarray:
    axes = []
    for i, road in enumerate(roads):
        pts = road.get("pts") or []
        if len(pts) < 2:
            continue
        widths = [float(p[3]) for p in pts if len(p) >= 4]
        axes.append(
            {
                "id": _oid(road, i),
                "id_s": _oid(road, i),
                "xy": [(float(p[0]), float(p[1])) for p in pts],
                "nodes": pts,
                "half_w": 0.5 * (max(widths) if widths else 8.0),
                "ends": [],
            }
        )
    from guardrail_junctions import _ends

    for axis in axes:
        axis["ends"] = _ends(axis["xy"])
    meetings = find_junctions(axes, join_m=JUNCTION_JOIN_M, z_sep_m=Z_SEP_M)
    if not meetings:
        return np.zeros((0, 2), dtype=np.float64)
    return np.array([[float(j["xy"][0]), float(j["xy"][1])] for j in meetings], dtype=np.float64)


def main() -> None:
    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    cfg = bdr._cfg(site.get("beamng") or {})
    min_length = float(cfg.get("min_length_m") or 8.0)
    roads = [r for r in _gip_polylines(site, proc) if _is_main_road(r, site)]
    kept = []
    for road in roads:
        pts = road.get("pts") or []
        length = 0.0
        for a, b in zip(pts, pts[1:]):
            length += math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
        if length >= min_length:
            kept.append(road)
    roads = kept
    if not roads:
        raise SystemExit("no main-road centerlines")

    heights_arg = _arg_value("--heights")
    if heights_arg:
        heights_path = Path(heights_arg)
        if not heights_path.is_file():
            raise SystemExit(f"heights raster missing: {heights_path}")
        z, index = _load_geotiff(heights_path)
        ref = json.loads(
            (proc / "corridor50_road" / "corridor50_road_index.json").read_text(encoding="utf-8")
        )
        for key in ("xmin", "ymin", "xmax", "ymax", "width", "height"):
            if index[key] != ref[key]:
                raise SystemExit(
                    f"heights frame {key}={index[key]} does not match corridor {ref[key]}"
                )
        height_label = heights_path.name
        out_name = f"road_breaks_{heights_path.stem}"
    else:
        raw_dir, index = _ensure_raw_mosaic(site, proc)
        z = np.asarray(tiff.imread(raw_dir / index["raster"]), dtype=np.float32)
        if z.ndim == 3:
            z = z[..., 0]
        bad = elevation_nodata_mask(z)
        if np.any(bad):
            z = z.copy()
            z[bad] = np.nan
        height_label = "raw 0.5 m DGM"
        out_name = "road_breaks"
        heights_path = raw_dir / index["raster"]
    tol_arg = _arg_value("--tol")
    tol_m = float(tol_arg) if tol_arg else POLY_TOL_M
    if tol_m <= 0:
        raise SystemExit("--tol must be positive")
    if abs(tol_m - POLY_TOL_M) > 1e-6:
        out_name = f"{out_name}_tol{tol_m:g}m"
    deg_arg = _arg_value("--break-deg")
    break_deg = float(deg_arg) if deg_arg else BREAK_DEG
    sustain_deg = SUSTAIN_DEG if deg_arg is None else max(4.0, break_deg - 2.0)
    if break_deg <= 0:
        raise SystemExit("--break-deg must be positive")
    if abs(break_deg - BREAK_DEG) > 1e-6:
        out_name = f"{out_name}_deg{break_deg:g}"
    xmin = float(index["xmin"])
    ymax = float(index["ymax"])

    path = find_landnutzung_geojson(site)
    if path is None:
        raise SystemExit("no Landnutzung geojson for this site")
    geoms, codes = load_street_polygons(path, _ln_to_31254())
    if not geoms:
        raise SystemExit(f"no street polygons in {path.name}")
    poly_tree = STRtree(geoms)

    span_tree = _bands(
        bdr._load_bridge_bands(proc, pad_m=0.0, extend_m=0.0, prefer_under=False)
    )
    gallery_tree = _bands(bdr._load_gallery_corridors(proc, cfg))
    junctions = _junction_xy(roads)
    print(
        f"break measure: {height_label}, tol=+/-{tol_m:g} m, break={break_deg:g} deg, "
        f"{len(roads)} roads, junctions={len(junctions)}, "
        f"spans={'yes' if span_tree is not None else 'no'}, "
        f"galleries={'yes' if gallery_tree is not None else 'no'}",
        flush=True,
    )

    rows: list[dict] = []
    for i, road in enumerate(roads):
        packed = _densify(road.get("pts") or [], sc, STATION_M)
        if packed is None:
            continue
        ss, beam, crs = packed
        tang = _tangents(crs)
        left = np.column_stack([-tang[:, 1], tang[:, 0]])
        holds = _hold_mask(beam, span_tree, gallery_tree, junctions)
        left_z = _side_profiles(z, xmin, ymax, crs, left)
        right_z = _side_profiles(z, xmin, ymax, crs, -left)
        l_raw, l_ang = _find_breaks(left_z, break_deg, sustain_deg)
        r_raw, r_ang = _find_breaks(right_z, break_deg, sustain_deg)
        l_off = _smooth_offset(l_raw)
        r_off = _smooth_offset(r_raw)
        oid = _oid(road, i)
        code = str(road.get("str_code") or "")
        obj = str(road.get("objekt") or "")
        for k in range(ss.shape[0]):
            cx, cy = float(crs[k, 0]), float(crs[k, 1])
            lx, ly = float(left[k, 0]), float(left[k, 1])
            poly, _ln = _pick_poly(poly_tree, geoms, codes, cx, cy, obj)
            l_poly = _poly_offset(poly, cx, cy, lx, ly)
            r_poly = _poly_offset(poly, cx, cy, -lx, -ly)
            l_chosen, l_src, l_reason = _decide(
                float(l_off[k]), float(l_ang[k]), l_poly, holds[k], tol_m, break_deg
            )
            r_chosen, r_src, r_reason = _decide(
                float(r_off[k]), float(r_ang[k]), r_poly, holds[k], tol_m, break_deg
            )
            rows.append(
                {
                    "oid": oid,
                    "str_code": code,
                    "objekt": obj,
                    "s_m": round(float(ss[k]), 2),
                    "hold": holds[k],
                    "cx": cx,
                    "cy": cy,
                    "lx": lx,
                    "ly": ly,
                    "left_break_raw_m": float(l_raw[k]),
                    "left_break_m": float(l_off[k]),
                    "left_break_deg": float(l_ang[k]),
                    "left_poly_m": l_poly,
                    "left_chosen_m": l_chosen,
                    "left_source": l_src,
                    "left_reason": l_reason,
                    "left_x": cx + lx * l_chosen if math.isfinite(l_chosen) else float("nan"),
                    "left_y": cy + ly * l_chosen if math.isfinite(l_chosen) else float("nan"),
                    "right_break_raw_m": float(r_raw[k]),
                    "right_break_m": float(r_off[k]),
                    "right_break_deg": float(r_ang[k]),
                    "right_poly_m": r_poly,
                    "right_chosen_m": r_chosen,
                    "right_source": r_src,
                    "right_reason": r_reason,
                    "right_x": cx - lx * r_chosen if math.isfinite(r_chosen) else float("nan"),
                    "right_y": cy - ly * r_chosen if math.isfinite(r_chosen) else float("nan"),
                }
            )
        print(f"  {i + 1}/{len(roads)} oid={_oid_num(oid)} {code} stations={ss.shape[0]}", flush=True)

    if not rows:
        raise SystemExit("no stations")

    before = {
        "left": _jump_stats(rows, "left", "left_chosen_m"),
        "right": _jump_stats(rows, "right", "right_chosen_m"),
    }
    _apply_slew(rows)
    after = {
        "left": _jump_stats(rows, "left", "left_slew_m"),
        "right": _jump_stats(rows, "right", "right_slew_m"),
    }
    print(f"  offset jumps before slew {before}", flush=True)
    print(f"  offset jumps after slew  {after}", flush=True)

    out_dir = proc / out_name
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "road_breaks.csv"
    fields = [
        "oid",
        "str_code",
        "objekt",
        "s_m",
        "hold",
        "cx",
        "cy",
        "left_break_raw_m",
        "left_break_m",
        "left_break_deg",
        "left_poly_m",
        "left_chosen_m",
        "left_slew_m",
        "left_source",
        "left_reason",
        "right_break_raw_m",
        "right_break_m",
        "right_break_deg",
        "right_poly_m",
        "right_chosen_m",
        "right_slew_m",
        "right_source",
        "right_reason",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = {}
            for key in fields:
                value = row[key]
                if isinstance(value, float):
                    out[key] = round(value, 3) if math.isfinite(value) else ""
                else:
                    out[key] = value
            writer.writerow(out)

    edges = _lines_for_side(rows, "left") + _lines_for_side(rows, "right")
    smooth = _lines_for_side(
        rows, "left", value_key="left_slew_m", x_key="left_sx", y_key="left_sy", split_source=False
    ) + _lines_for_side(
        rows, "right", value_key="right_slew_m", x_key="right_sx", y_key="right_sy", split_source=False
    )
    ribbons = _ribbons(rows)
    breaks = _break_points(rows, "left", 1.0) + _break_points(rows, "right", -1.0)
    gpkg = out_dir / "road_breaks.gpkg"
    if gpkg.exists():
        gpkg.unlink()
    gpd.GeoDataFrame(edges, geometry="geometry", crs="EPSG:31254").to_file(
        gpkg, layer="edges", driver="GPKG"
    )
    gpd.GeoDataFrame(smooth, geometry="geometry", crs="EPSG:31254").to_file(
        gpkg, layer="edges_smooth", driver="GPKG"
    )
    gpd.GeoDataFrame(ribbons, geometry="geometry", crs="EPSG:31254").to_file(
        gpkg, layer="ribbon", driver="GPKG"
    )
    gpd.GeoDataFrame(breaks, geometry="geometry", crs="EPSG:31254").to_file(
        gpkg, layer="breaks", driver="GPKG"
    )

    def _count(side: str, key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            name = row[f"{side}_{key}"] or "(open)"
            counts[name] = counts.get(name, 0) + 1
        return counts

    report = {
        "what": (
            "Per main-road OID, slope breaks on the height raster and the "
            "Landnutzung offset on the same perpendicular. source=break when "
            "the break is within 2 m of the polygon edge, or further inside a "
            "wider polygon at >=15°. Junctions, spans and galleries keep the polygon."
        ),
        "heights": height_label,
        "heights_path": str(heights_path),
        "crs": "EPSG:31254",
        "station_m": STATION_M,
        "search_m": SEARCH_M,
        "break_deg": break_deg,
        "sustain_m": SUSTAIN_M,
        "sustain_deg": sustain_deg,
        "poly_tol_m": tol_m,
        "slew_m_per_m": SLEW_M,
        "gap_hold_m": GAP_HOLD_M,
        "jumps_before": before,
        "jumps_after": after,
        "inside_angle_deg": INSIDE_ANGLE_DEG,
        "median_radius_stations": MEDIAN_RADIUS,
        "offsets": "ground metres, EPSG:31254",
        "roads": len(roads),
        "stations": len(rows),
        "edge_parts": len(edges),
        "smooth_parts": len(smooth),
        "ribbons": len(ribbons),
        "break_points": len(breaks),
        "left_source": _count("left", "source"),
        "right_source": _count("right", "source"),
        "left_reason": _count("left", "reason"),
        "right_reason": _count("right", "reason"),
        "gpkg": str(gpkg),
        "layers": {
            "edges": "chosen left/right line before the longitudinal limit",
            "edges_smooth": "same line after at most 0.4 m lateral change per metre",
            "ribbon": "carriageway band used to clip the mesh",
            "breaks": "measured break, accepted=true where it won",
        },
        "csv": str(csv_path),
        "mesh": "unchanged",
        "guardrails": "unchanged",
    }
    (out_dir / "road_breaks_index.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Wrote {gpkg} edges={len(edges)} smooth={len(smooth)} ribbons={len(ribbons)} "
        f"breaks={len(breaks)} stations={len(rows)}",
        flush=True,
    )
    print(f"  left  {report['left_source']}  {report['left_reason']}", flush=True)
    print(f"  right {report['right_source']}  {report['right_reason']}", flush=True)


if __name__ == "__main__":
    main()
