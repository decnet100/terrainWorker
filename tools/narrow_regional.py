"""Place each station by the share of flat carriageway samples, once.

A sample on the carriageway counts when the DGM slope magnitude stays within
the axial grade plus 15 percentage points. The axial grade is the DGM height
change along the axis over the 4 m to the next station, and that one value
is used for the whole cross profile. The search checks every station out to
2 m each side, in 10 cm steps, and keeps the highest share. An equal share
keeps the smaller offset. A 10 cm narrower width is kept only when its share
is higher, and only for more than two stations within 24 m.

A station that still has no flat sample is a structure. The stretch out to
the next stations that do have a flat sample is removed from the surface.
The axis there stays. Known GIP structures stay excluded. Edge difference
and roughness are not decided in this pass.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\narrow_regional.py
"""
from __future__ import annotations

import math
import sys
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
from shapely.geometry import LineString, Point
from shapely.prepared import prep

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_link_surfaces import (  # noqa: E402
    BEARING_M,
    JUNCTION_MIN_M2,
    SLIVER_M2,
    _cum,
    _parts,
    _plan_junctions,
    _point_at,
    _ray_hits,
    _ribbon,
    _width_at,
    _width_profile,
    _xy,
)
from build_road_grid import _load_geotiff  # noqa: E402
from gip_routing_load import gpkg_path, load_routing  # noqa: E402
from sample_cross_profile import MIN_WIDTH_M  # noqa: E402
from score_link_surfaces import (  # noqa: E402
    ACROSS_STEP_M,
    CROSS_MARK_M,
    EDGE_INSET_M,
    STATION_M,
    STRUCT_SKIP_M,
    _percentiles,
    _roughness,
    _score_chain,
    _z_at,
)
from site_coords import load_site, processed_dir  # noqa: E402

STEP_M = 0.10
SHIFT_M = 1.0
SHIFT_STEP_M = 0.10
FLAT_ADD = 15.0
FLAT_SHIFT_M = 2.0
GAP_M = 24.0
TAPER_M = 12.0
ROUGH_TOL_M = 0.01
HALF_STOP = 0.50
MAX_ROUNDS = 20
OUT_NAMES = ("surface_flat.gpkg", "surface_flat_next.gpkg")


def _runs(width_m, width_along) -> tuple[list[float], list[float]]:
    text = "" if width_along is None else str(width_along)
    if text in ("", "None", "nan"):
        return [0.0], [float(width_m)]
    dist: list[float] = []
    widths: list[float] = []
    for part in text.split(","):
        station, width = part.split(":")
        dist.append(float(station))
        widths.append(float(width))
    return dist or [0.0], widths or [float(width_m)]


def _held(dist, widths, distance: float) -> float:
    """Width holds until the next stored break. It is not spread between breaks."""
    chosen = 0
    for i, station in enumerate(dist):
        if station <= distance + 1e-6:
            chosen = i
        else:
            break
    return float(widths[chosen])


def _vertex_widths(pts, width_m, width_along) -> tuple[list[tuple[float, float]], list[float]]:
    """Step the stored breaks onto the line. A break is two points, not a ramp."""
    run_d, run_w = _runs(width_m, width_along)
    dist = _cum(pts)
    marks = [(d, pts[i], _held(run_d, run_w, d)) for i, d in enumerate(dist)]
    for station, width in zip(run_d[1:], run_w[1:]):
        if station <= 0.0 or station >= dist[-1] - 1e-6:
            continue
        point = _point_at(pts, station)
        marks.append((station, point, _held(run_d, run_w, station - 1e-3)))
        marks.append((station, point, width))
    marks.sort(key=lambda item: (item[0], item[2]))
    out_pts: list[tuple[float, float]] = []
    out_w: list[float] = []
    for _station, point, width in marks:
        if out_pts and math.hypot(point[0] - out_pts[-1][0], point[1] - out_pts[-1][1]) < 0.02:
            if abs(width - out_w[-1]) < 0.02:
                continue
        out_pts.append(point)
        out_w.append(width)
    return out_pts, out_w


def _frame(pts, station: float):
    line = LineString(pts)
    length = float(line.length)
    here = line.interpolate(station)
    before = line.interpolate(max(0.0, station - 1.0))
    after = line.interpolate(min(length, station + 1.0))
    dx = after.x - before.x
    dy = after.y - before.y
    norm = math.hypot(dx, dy)
    if norm < 1e-6:
        return None
    return here.x, here.y, -dy / norm, dx / norm


def _measure(x, y, nx, ny, width: float, elev, spec, offset: float = 0.0):
    x = x + nx * offset
    y = y + ny * offset
    half = width / 2.0 - EDGE_INSET_M
    if half < 0.4:
        return None
    left_z = float(_z_at(np.array([x + nx * half]), np.array([y + ny * half]), elev, spec)[0])
    right_z = float(_z_at(np.array([x - nx * half]), np.array([y - ny * half]), elev, spec)[0])
    if not np.isfinite(left_z) or not np.isfinite(right_z):
        return None
    offsets = np.arange(-half, half + 1e-6, ACROSS_STEP_M)
    xs = x + nx * offsets
    ys = y + ny * offsets
    zs = _z_at(xs, ys, elev, spec)
    finite = np.isfinite(zs)
    if int(finite.sum()) < 2:
        return None
    off = offsets[finite]
    zz = zs[finite]
    centered = off - off.mean()
    denom = float(np.dot(centered, centered))
    slope = 0.0 if denom < 1e-8 else float(np.dot(centered, zz - zz.mean()) / denom)
    rough = _roughness(xs[finite], ys[finite], elev, spec)
    rough_m = float(np.nanmedian(rough)) if np.any(np.isfinite(rough)) else None
    return {
        "cross": abs(left_z - right_z),
        "rough": rough_m,
        "slope": slope,
        "z_left": left_z,
        "z_right": right_z,
        "half": half,
    }


def _rough_ok(base: dict, trial: dict) -> bool:
    if base["rough"] is None or trial["rough"] is None:
        return True
    return trial["rough"] <= base["rough"] + ROUGH_TOL_M


def _search(x, y, nx, ny, width: float, elev, spec, base: dict) -> dict | None:
    """Offset within 1 m, step 10 cm, that leaves the smallest edge difference."""
    best = None
    offset = -SHIFT_M
    while offset <= SHIFT_M + 1e-9:
        trial = _measure(x, y, nx, ny, width, elev, spec, offset)
        if trial is not None and trial["cross"] < base["cross"] and _rough_ok(base, trial):
            if best is None or trial["cross"] < best["cross"] - 1e-9:
                best = trial
                best["offset"] = offset
            elif abs(trial["cross"] - best["cross"]) <= 1e-9 and abs(offset) < abs(best["offset"]):
                best = trial
                best["offset"] = offset
        offset = round(offset + SHIFT_STEP_M, 2)
    return best


def _take(elev, row, col) -> np.ndarray:
    height, width = elev.shape
    ok = (row >= 0) & (col >= 0) & (row < height) & (col < width)
    out = np.full(row.shape, np.nan, dtype=np.float64)
    out[ok] = elev[row[ok], col[ok]]
    return out


def _slope_percent(xs: np.ndarray, ys: np.ndarray, elev, spec) -> np.ndarray:
    """Slope magnitude of the DGM, in percent, from the neighbouring cells."""
    res = spec["resolution_m"]
    col = np.floor((xs - spec["xmin"]) / res).astype(np.int32)
    row = np.floor((spec["ymax"] - ys) / res).astype(np.int32)
    dzdx = (_take(elev, row, col + 1) - _take(elev, row, col - 1)) / (2.0 * res)
    dzdy = (_take(elev, row - 1, col) - _take(elev, row + 1, col)) / (2.0 * res)
    return 100.0 * np.hypot(dzdx, dzdy)


def _axial_percent(x0, y0, x1, y1, elev, spec) -> float | None:
    """Absolute grade along the axis, in percent, over this ground interval."""
    dist = math.hypot(x1 - x0, y1 - y0)
    if dist < 0.5:
        return None
    z = _z_at(np.array([x0, x1], dtype=np.float64), np.array([y0, y1], dtype=np.float64), elev, spec)
    if not np.isfinite(z).all():
        return None
    return 100.0 * abs(float(z[1] - z[0])) / dist


def _share_profile(x, y, nx, ny, width: float, elev, spec, limit: float):
    """Share of flat samples for each offset out to 2 m. None when the width is too small."""
    half = width / 2.0 - EDGE_INSET_M
    if half < 0.4 or limit is None or not np.isfinite(limit):
        return None
    across = np.arange(-half, half + 1e-6, ACROSS_STEP_M)
    if across.size < 2:
        return None
    offsets = np.round(np.arange(-FLAT_SHIFT_M, FLAT_SHIFT_M + 1e-9, SHIFT_STEP_M), 2)
    shift = offsets[:, None] + across[None, :]
    slope = _slope_percent((x + nx * shift).ravel(), (y + ny * shift).ravel(), elev, spec).reshape(shift.shape)
    finite = np.isfinite(slope)
    total = finite.sum(axis=1)
    flat = ((slope <= limit + 1e-6) & finite).sum(axis=1)
    share = np.divide(flat, total, out=np.zeros(offsets.shape, dtype=np.float64), where=total > 0)
    return offsets, share


def _best_share(profile) -> tuple[float, float, float]:
    """Highest share, then the smaller offset. The third value is the share at offset 0."""
    if profile is None:
        return 0.0, 0.0, 0.0
    offsets, share = profile
    pick = int(np.lexsort((np.abs(offsets), -share))[0])
    here = int(np.argmin(np.abs(offsets)))
    return float(share[pick]), float(offsets[pick]), float(share[here])


def _smooth_offsets(stations: list[float], targets: list[float], pinned: list[bool] | None = None) -> list[float]:
    """At most 1 m of sideways change over 12 m, from both ends. Pinned stations stay at 0."""
    out = list(targets)
    hold = pinned or [False] * len(out)
    for i in range(1, len(out)):
        if hold[i]:
            out[i] = 0.0
            continue
        limit = max(0.0, stations[i] - stations[i - 1]) / TAPER_M
        out[i] = min(max(out[i], out[i - 1] - limit), out[i - 1] + limit)
    for i in range(len(out) - 2, -1, -1):
        if hold[i]:
            out[i] = 0.0
            continue
        limit = max(0.0, stations[i + 1] - stations[i]) / TAPER_M
        out[i] = min(max(out[i], out[i + 1] - limit), out[i + 1] + limit)
    return out


def _apply_shift(pts, stations, offsets) -> list[tuple[float, float]]:
    dist = _cum(pts)
    moved = []
    for i, (x, y) in enumerate(pts):
        if i == 0:
            ax, ay, bx, by = pts[0][0], pts[0][1], pts[1][0], pts[1][1]
        elif i == len(pts) - 1:
            ax, ay, bx, by = pts[-2][0], pts[-2][1], pts[-1][0], pts[-1][1]
        else:
            ax, ay, bx, by = pts[i - 1][0], pts[i - 1][1], pts[i + 1][0], pts[i + 1][1]
        span = math.hypot(bx - ax, by - ay)
        if span < 1e-6:
            moved.append((x, y))
            continue
        nx, ny = -(by - ay) / span, (bx - ax) / span
        off = float(np.interp(dist[i], stations, offsets))
        moved.append((x + nx * off, y + ny * off))
    return moved


def _apply_narrow(pts, ws, base, orig_runs, cores: list[tuple[float, float]]):
    dist = _cum(pts)
    length = dist[-1]
    run_d, run_w = orig_runs
    items = [(d, pts[i], ws[i], base[i]) for i, d in enumerate(dist)]
    for s0, s1 in cores:
        for station in (s0 - TAPER_M, s0, s1, s1 + TAPER_M):
            station = min(length, max(0.0, station))
            items.append((station, _point_at(pts, station), None, None))
    items.sort(key=lambda item: (item[0], item[2] is None))
    out_pts, out_w, out_b = [], [], []
    for station, point, width, origin in items:
        if width is None:
            width = _width_at(dist, ws, station)
            origin = _held(run_d, run_w, station)
        tent = max(_tent(station, s0, s1, length) for s0, s1 in cores)
        width = max(HALF_STOP * origin, width - tent)
        if out_pts and math.hypot(point[0] - out_pts[-1][0], point[1] - out_pts[-1][1]) < 0.02:
            if abs(width - out_w[-1]) < 0.02 and abs(origin - out_b[-1]) < 0.02:
                continue
        out_pts.append(point)
        out_w.append(width)
        out_b.append(origin)
    return out_pts, out_w, out_b


def _clusters(stations: list[float]) -> list[list[float]]:
    if not stations:
        return []
    groups = [[stations[0]]]
    for station in stations[1:]:
        if station - groups[-1][-1] <= GAP_M:
            groups[-1].append(station)
        else:
            groups.append([station])
    return [group for group in groups if len(group) > 2]


def _tent(station: float, s0: float, s1: float, length: float) -> float:
    """Additional reduction: full on the core, linear over the taper, else 0."""
    left = max(0.0, s0 - TAPER_M)
    right = min(length, s1 + TAPER_M)
    if station < left or station > right:
        return 0.0
    if station < s0:
        span = s0 - left
        return 0.0 if span < 1e-6 else STEP_M * (station - left) / span
    if station > s1:
        span = right - s1
        return 0.0 if span < 1e-6 else STEP_M * (right - station) / span
    return STEP_M


def _densify(pts, ws, base, cores: list[tuple[float, float]]):
    dist = _cum(pts)
    length = dist[-1]
    extras = []
    for s0, s1 in cores:
        for station in (s0 - TAPER_M, s0, s1, s1 + TAPER_M):
            extras.append(min(length, max(0.0, station)))
    marks = [(d, i) for i, d in enumerate(dist)]
    marks.extend((station, None) for station in extras)
    marks.sort(key=lambda item: (item[0], item[1] is None))
    new_pts = []
    new_ws = []
    new_base = []
    for station, index in marks:
        if index is not None:
            point = pts[index]
            width = ws[index]
            base_w = base[index]
        else:
            point = _point_at(pts, station)
            width = _width_at(dist, ws, station)
            base_w = _width_at(dist, base, station)
        cut = max(_tent(station, s0, s1, length) for s0, s1 in cores)
        width = max(MIN_WIDTH_M, width - cut)
        if new_pts and math.hypot(point[0] - new_pts[-1][0], point[1] - new_pts[-1][1]) < 0.05:
            continue
        new_pts.append(point)
        new_ws.append(width)
        new_base.append(base_w)
    return new_pts, new_ws, new_base


def _on_struct(struct, x: float, y: float) -> bool:
    if struct is None or struct.is_empty:
        return False
    return struct.distance(Point(x, y)) <= STRUCT_SKIP_M


def _terminals(order: list[str], ends: dict[str, tuple[str, str]]) -> list[str]:
    count: dict[str, int] = defaultdict(int)
    for lid in order:
        a, b = ends[lid]
        count[a] += 1
        count[b] += 1
    return [node for node, n in count.items() if n == 1]


def _seat(chains: list[dict], plans, nodes) -> tuple[int, float, list[dict]]:
    """Side arms end on the through axis. The through axis is not moved."""
    by_fc: dict[int, list[dict]] = defaultdict(list)
    for chain in chains:
        if chain["start_node"] and chain["end_node"]:
            by_fc[chain["fc"]].append(chain)
    node_j: dict[str, tuple[int, tuple[float, float]]] = {}
    reports = []
    moved = 0
    max_move = 0.0
    for fc in sorted(by_fc):
        ready = by_fc[fc]
        for plan in plans.get(fc, []):
            through = next(
                (
                    chain
                    for chain in ready
                    if plan["through"][0] in chain["order"] and plan["through"][1] in chain["order"]
                ),
                None,
            )
            if through is None:
                continue
            alongs = []
            sides = []
            for sid in plan["sides"]:
                side = next((chain for chain in ready if sid in chain["order"]), None)
                if side is None:
                    continue
                at_start = side["start_node"] == plan["node"]
                seq = side["pts"] if at_start else list(reversed(side["pts"]))
                if len(seq) < 2:
                    continue
                origin = _point_at(seq, min(BEARING_M, max(4.0, _cum(seq)[-1] * 0.5)))
                found = _ray_hits(origin, seq[0], through["pts"], plan["nxy"])
                if found is None:
                    continue
                alongs.append(found[1])
                sides.append(side)
            if not alongs:
                continue
            junction = _point_at(through["pts"], sum(alongs) / len(alongs))
            node_j[plan["node"]] = (fc, junction)
            for side in sides:
                idx = 0 if side["start_node"] == plan["node"] else -1
                if side["start_node"] != plan["node"] and side["end_node"] != plan["node"]:
                    continue
                old = side["pts"][idx]
                shift = math.hypot(old[0] - junction[0], old[1] - junction[1])
                if shift > 0.05:
                    moved += 1
                max_move = max(max_move, shift)
                side["pts"][idx] = junction
            reports.append(
                {
                    "fc": fc,
                    "off_straight_deg": round(plan["dev"], 1),
                    "n_side": len(sides),
                    "through": plan["through"],
                    "geometry": Point(junction),
                }
            )
        for chain in ready:
            for node, idx in ((chain["start_node"], 0), (chain["end_node"], -1)):
                owned = node_j.get(node)
                if owned is None or owned[0] >= fc:
                    continue
                old = chain["pts"][idx]
                shift = math.hypot(old[0] - owned[1][0], old[1] - owned[1][1])
                if shift > 0.05:
                    moved += 1
                max_move = max(max_move, shift)
                chain["pts"][idx] = owned[1]
    return moved, max_move, reports


def _substring(pts, ws, s0: float, s1: float):
    dist = _cum(pts)
    out_p = [_point_at(pts, s0)]
    out_w = [_width_at(dist, ws, s0)]
    for index, station in enumerate(dist):
        if s0 + 1e-6 < station < s1 - 1e-6:
            out_p.append(pts[index])
            out_w.append(ws[index])
    end = _point_at(pts, s1)
    if math.hypot(end[0] - out_p[-1][0], end[1] - out_p[-1][1]) >= 0.02:
        out_p.append(end)
        out_w.append(_width_at(dist, ws, s1))
    return out_p, out_w


def _kept_ribbon(chain):
    """Ribbon of the chain with structure spans removed."""
    pts, ws = chain["pts"], chain["ws"]
    length = _cum(pts)[-1] if pts else 0.0
    spans = [
        (max(0.0, s0), min(length, s1))
        for s0, s1 in chain.get("spans") or []
        if s1 - s0 >= 1.0
    ]
    if length < 1.0 or not spans:
        return _ribbon(pts, ws)
    kept = []
    cursor = 0.0
    for s0, s1 in sorted(spans):
        if s0 > cursor + 0.5:
            kept.append((cursor, s0))
        cursor = max(cursor, s1)
    if length > cursor + 0.5:
        kept.append((cursor, length))
    parts = []
    for s0, s1 in kept:
        poly = _ribbon(*_substring(pts, ws, s0, s1))
        if poly is not None:
            parts.append(poly)
    if not parts:
        return None
    return shapely.unary_union(parts)


def _empty_spans(stations: list[float], empty: set[float], length: float) -> list[tuple[float, float]]:
    """From the station before a run of empty stations to the station after it."""
    spans = []
    index = 0
    while index < len(stations):
        if stations[index] not in empty:
            index += 1
            continue
        end = index
        while end + 1 < len(stations) and stations[end + 1] in empty:
            end += 1
        s0 = stations[index - 1] if index > 0 else 0.0
        s1 = stations[end + 1] if end + 1 < len(stations) else length
        if s1 - s0 >= 1.0:
            spans.append((s0, s1))
        index = end + 1
    return spans


def _rebuild(chains: list[dict]) -> list[dict]:
    raw: dict[int, list] = defaultdict(list)
    for chain in chains:
        poly = _kept_ribbon(chain)
        chain["raw"] = poly
        chain["poly"] = None
        chain["prep"] = None
        if poly is not None:
            raw[chain["fc"]].append(chain)
    higher = []
    higher_union = None
    for fc in sorted({chain["fc"] for chain in chains}):
        for chain in raw.get(fc, []):
            poly = chain["raw"]
            geom = poly if higher_union is None or higher_union.is_empty else shapely.difference(poly, higher_union)
            if geom is not None and not geom.is_empty:
                geom = shapely.unary_union(geom)
            chain["poly"] = geom
            chain["prep"] = prep(geom) if geom is not None and not geom.is_empty else None
        higher.extend(chain["raw"] for chain in raw.get(fc, []) if chain["raw"] is not None)
        higher_union = shapely.unary_union(higher) if higher else None
    junction_rows = []
    for fc, items in raw.items():
        geoms = [chain["raw"] for chain in items]
        if len(geoms) < 2:
            continue
        pairs = shapely.STRtree(geoms).query(geoms, predicate="intersects")
        seen = set()
        for i, j in pairs.T:
            a, b = int(i), int(j)
            if a >= b or (a, b) in seen:
                continue
            seen.add((a, b))
            inter = geoms[a].intersection(geoms[b])
            if inter.is_empty or inter.area < JUNCTION_MIN_M2:
                continue
            for part in _parts(inter):
                if part.area < JUNCTION_MIN_M2:
                    continue
                junction_rows.append(
                    {
                        "code": items[a]["code"],
                        "fc": fc,
                        "a": items[a]["key"],
                        "b": items[b]["key"],
                        "area_m2": round(float(part.area), 1),
                        "geometry": part,
                    }
                )
    return junction_rows


def _reduction_runs(chain) -> tuple[list[dict], list[dict]]:
    """Final plateau and the linear transitions, read off the vertex widths."""
    dist = _cum(chain["pts"])
    narrow = []
    taper = []
    kind = []
    for i in range(1, len(dist)):
        r0 = chain["base"][i - 1] - chain["ws"][i - 1]
        r1 = chain["base"][i] - chain["ws"][i]
        if r0 >= STEP_M - 0.02 and r1 >= STEP_M - 0.02 and abs(r0 - r1) < 0.02:
            kind.append("narrow")
        elif (r0 >= 0.02 or r1 >= 0.02) and abs(r0 - r1) >= 0.02:
            kind.append("taper")
        else:
            kind.append("full")
    i = 0
    while i < len(kind):
        if kind[i] == "full":
            i += 1
            continue
        j = i
        while j + 1 < len(kind) and kind[j + 1] == kind[i]:
            j += 1
        line = LineString(chain["pts"][i : j + 2])
        if line.length >= 1.0:
            r0 = chain["base"][i] - chain["ws"][i]
            r1 = chain["base"][j + 1] - chain["ws"][j + 1]
            row = {
                "code": chain["code"],
                "name": chain["name"],
                "narrow_m": round(max(r0, r1), 2),
                "station_from": round(dist[i], 1),
                "station_to": round(dist[j + 1], 1),
                "length_m": round(float(line.length), 1),
                "short_ids": chain["key"],
                "geometry": line,
            }
            (narrow if kind[i] == "narrow" else taper).append(row)
        i = j + 1
    return narrow, taper


def _dest(out_dir: Path) -> Path:
    for name in OUT_NAMES:
        dest = out_dir / name
        if not dest.exists():
            return dest
        try:
            dest.unlink()
            return dest
        except PermissionError:
            print(f"{name} is open, trying the next file", flush=True)
    raise SystemExit("surface_flat.gpkg and surface_flat_next.gpkg are open")


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    src = proc / "link_surface" / "surface_width.gpkg"
    fit = gpd.read_file(src, layer="axis_fit")
    surface = gpd.read_file(src, layer="surface")
    elev, spec = _load_geotiff(proc / "corridor50_raw" / "corridor50_raw.tif")
    topo = load_routing(site)
    links = topo["links"]
    nodes = topo["nodes"]
    by_short = {int(rec["short_id"]): lid for lid, rec in links.items()}
    ends = {lid: (rec["node_from"], rec["node_to"]) for lid, rec in links.items()}
    link_geoms = {
        row.link_id: row.geometry
        for row in gpd.read_file(gpkg_path(site), layer="link").itertuples(index=False)
    }
    lines = {}
    by_fc: dict[int, list[str]] = defaultdict(list)
    for lid, rec in links.items():
        geom = link_geoms.get(lid)
        if geom is None or geom.is_empty:
            continue
        lines[lid] = _xy(geom)
        by_fc[int(rec["fc"])].append(lid)
    _forced, plans = _plan_junctions(lines, ends, by_fc, nodes)
    print(f"junctions planned {sum(len(v) for v in plans.values())}", flush=True)

    old_poly: dict[str, list] = defaultdict(list)
    for rec in surface.itertuples(index=False):
        old_poly[str(rec.short_ids)].append(rec.geometry)
    name_of = {}
    try:
        scored = gpd.read_file(proc / "link_surface" / "score.gpkg", layer="chain")
        for rec in scored.itertuples(index=False):
            name = rec.name
            if name is None or (isinstance(name, float) and not np.isfinite(name)):
                name = ""
            text = str(name).strip()
            name_of[str(rec.short_ids)] = "" if text.lower() in ("", "nan", "none") else text
    except Exception:
        pass

    id_to_key: dict[int, str] = {}
    chains = []
    for rec in fit.itertuples(index=False):
        key = str(rec.short_ids)
        pts = _xy(rec.geometry)
        if len(pts) < 2:
            continue
        pts, ws = _vertex_widths(pts, rec.width_m, rec.width_along)
        order = []
        for part in key.split(","):
            if not part:
                continue
            lid = by_short.get(int(part))
            if lid is None:
                order = []
                break
            order.append(lid)
            id_to_key[int(part)] = key
        terminals = _terminals(order, ends) if order else []
        start_node = ""
        end_node = ""
        if len(terminals) >= 2:
            start_node = min(terminals, key=lambda nid: math.hypot(pts[0][0] - nodes[nid]["x"], pts[0][1] - nodes[nid]["y"]))
            end_node = min(terminals, key=lambda nid: math.hypot(pts[-1][0] - nodes[nid]["x"], pts[-1][1] - nodes[nid]["y"]))
        poly = shapely.unary_union(old_poly[key]) if old_poly.get(key) else _ribbon(pts, ws)
        chains.append(
            {
                "key": key,
                "code": rec.code,
                "fc": int(rec.fc),
                "name": name_of.get(key, ""),
                "order": order,
                "pts": pts,
                "ws": ws,
                "base": list(ws),
                "orig_runs": _runs(rec.width_m, rec.width_along),
                "start_node": start_node,
                "end_node": end_node,
                "poly": poly,
                "prep": prep(poly) if poly is not None and not poly.is_empty else None,
                "fitted": int(rec.fitted),
            }
        )

    struct_parts: dict[str, list] = defaultdict(list)
    try:
        refs = gpd.read_file(gpkg_path(site), layer="bridge_ref")
    except Exception:
        refs = gpd.GeoDataFrame()
    for rec in refs.itertuples(index=False):
        rec_link = links.get(str(rec.link_id))
        sid = int(rec_link["short_id"]) if rec_link is not None else None
        key = id_to_key.get(sid) if sid is not None else None
        if key is not None and rec.geometry is not None and not rec.geometry.is_empty:
            struct_parts[key].append(rec.geometry)
    struct_of = {key: shapely.unary_union(parts) for key, parts in struct_parts.items() if parts}
    for chain in chains:
        chain["struct"] = struct_of.get(chain["key"])

    def _share_one(x, y, nx, ny, width, limit) -> float:
        half = width / 2.0 - EDGE_INSET_M
        if half < 0.4 or limit is None or not np.isfinite(limit):
            return 0.0
        across = np.arange(-half, half + 1e-6, ACROSS_STEP_M)
        if across.size < 2:
            return 0.0
        slope = _slope_percent(x + nx * across, y + ny * across, elev, spec)
        finite = np.isfinite(slope)
        total = int(finite.sum())
        if total == 0:
            return 0.0
        return float(np.sum(slope[finite] <= limit + 1e-6) / total)

    problems = []
    seen_problem = set()
    reports = []
    junction_rows = []
    n_station = 0
    n_shift = 0
    n_empty = 0
    n_narrow = 0
    n_core = 0
    n_steep_axis = 0
    axial_values = []
    before_shares = []
    print(f"flat pass, {len(chains)} chains", flush=True)
    for index, chain in enumerate(chains, start=1):
        length = float(LineString(chain["pts"]).length)
        chain["span_frac"] = []
        chain["before_share"] = []
        if length < 2.0 * STATION_M:
            if index % 40 == 0 or index == len(chains):
                print(f"chains {index}/{len(chains)}", flush=True)
            continue
        stations = [0.0]
        stations.extend(
            float(s) for s in np.arange(STATION_M, max(STATION_M, length - STATION_M) + 1e-6, STATION_M)
        )
        if stations[-1] < length - 1e-6:
            stations.append(length)
        dist = _cum(chain["pts"])
        run_d, run_w = chain["orig_runs"]
        plain_at = {station: 0.0 for station in stations}
        narrow_at = {}
        want = []
        empty = set()
        plain_hit = set()
        pinned_at = set()
        for station in stations:
            if station < STATION_M or station > length - STATION_M:
                continue
            frame = _frame(chain["pts"], station)
            if frame is None:
                continue
            x, y, nx, ny = frame
            if _on_struct(chain["struct"], x, y):
                pinned_at.add(station)
                continue
            ahead = _point_at(chain["pts"], min(length, station + STATION_M))
            axial = _axial_percent(x, y, ahead[0], ahead[1], elev, spec)
            n_station += 1
            if axial is None:
                empty.add(station)
                n_empty += 1
                chain["before_share"].append(0.0)
                before_shares.append(0.0)
                continue
            axial_values.append(axial)
            if axial + FLAT_ADD >= 60.0:
                n_steep_axis += 1
            limit = axial + FLAT_ADD
            width = _width_at(dist, chain["ws"], station)
            origin = _held(run_d, run_w, station)
            plain_share, plain_off, share0 = _best_share(
                _share_profile(x, y, nx, ny, width, elev, spec, limit)
            )
            chain["before_share"].append(share0)
            before_shares.append(share0)
            if plain_share > 0.0:
                plain_at[station] = plain_off
                plain_hit.add(station)
            floor = HALF_STOP * origin
            narrow_share = 0.0
            if width - STEP_M < floor - 1e-9:
                mark = (chain["key"], round(station, 1))
                if mark not in seen_problem:
                    seen_problem.add(mark)
                    problems.append(
                        {
                            "code": chain["code"],
                            "name": chain["name"],
                            "station_m": round(station, 1),
                            "width_m": round(width, 2),
                            "original_m": round(origin, 2),
                            "share": round(plain_share, 3),
                            "short_ids": chain["key"],
                            "geometry": Point(x, y),
                        }
                    )
            else:
                narrow_share, narrow_off, _share_n = _best_share(
                    _share_profile(x, y, nx, ny, width - STEP_M, elev, spec, limit)
                )
                if narrow_share > plain_share + 1e-9:
                    narrow_at[station] = narrow_off
                    want.append(station)
            chosen = narrow_share if station in narrow_at else plain_share
            if chosen <= 0.0:
                empty.add(station)
                n_empty += 1
        groups = _clusters(want)
        cores = [(group[0], group[-1]) for group in groups]
        kept_narrow = {
            station
            for station in want
            if any(s0 - 1e-6 <= station <= s1 + 1e-6 for s0, s1 in cores)
        }
        for station in want:
            if station not in kept_narrow and station not in plain_hit and station not in empty:
                empty.add(station)
                n_empty += 1
        spans = _empty_spans(stations, empty, length)
        chain["span_frac"] = [(s0 / length, s1 / length) for s0, s1 in spans]
        n_core += len(cores)
        n_narrow += len(kept_narrow)
        targets = []
        pinned = []
        for station in stations:
            if station in kept_narrow:
                targets.append(narrow_at[station])
            else:
                targets.append(plain_at.get(station, 0.0))
            pinned.append(station in empty or station in pinned_at)
        smooth = _smooth_offsets(stations, targets, pinned)
        n_shift += sum(1 for value in smooth if abs(value) > 0.02)
        if cores:
            chain["pts"], chain["ws"], chain["base"] = _apply_narrow(
                chain["pts"], chain["ws"], chain["base"], chain["orig_runs"], cores
            )
        if max((abs(value) for value in smooth), default=0.0) > 0.02:
            chain["pts"] = _apply_shift(chain["pts"], stations, smooth)
            chain["did"] = True
        elif cores or spans:
            chain["did"] = True
        if index % 40 == 0 or index == len(chains):
            print(f"chains {index}/{len(chains)}", flush=True)
    moved, max_move, reports = _seat(chains, plans, nodes)
    print(f"junction ends moved {moved}, max {max_move:.2f} m", flush=True)
    for chain in chains:
        length = float(LineString(chain["pts"]).length) if len(chain["pts"]) >= 2 else 0.0
        chain["spans"] = [
            (frac0 * length, frac1 * length)
            for frac0, frac1 in chain.get("span_frac") or []
            if length > 0.0 and (frac1 - frac0) * length >= 1.0
        ]
    junction_rows = _rebuild(chains)
    print(
        f"stations {n_station}, shifted {n_shift}, empty {n_empty}, "
        f"narrow stations {n_narrow}, stretches {n_core}, "
        f"axial grade + 15 at or above 60 percent: {n_steep_axis}",
        flush=True,
    )
    if axial_values:
        print(
            f"axial grade percent p50 {np.round(np.median(axial_values), 1)} "
            f"p90 {np.round(np.percentile(axial_values, 90), 1)}",
            flush=True,
        )

    narrow_rows = []
    taper_rows = []
    bauwerk_rows = []
    after_shares = []
    for chain in chains:
        narrow, taper = _reduction_runs(chain)
        narrow_rows.extend(narrow)
        taper_rows.extend(taper)
        length = float(LineString(chain["pts"]).length) if len(chain["pts"]) >= 2 else 0.0
        chain["after_share"] = []
        if length >= 2.0 * STATION_M:
            dist = _cum(chain["pts"])
            for station in np.arange(STATION_M, max(STATION_M, length - STATION_M) + 1e-6, STATION_M):
                station = float(station)
                frame = _frame(chain["pts"], station)
                if frame is None:
                    continue
                x, y, nx, ny = frame
                if _on_struct(chain["struct"], x, y):
                    continue
                ahead = _point_at(chain["pts"], min(length, station + STATION_M))
                axial = _axial_percent(x, y, ahead[0], ahead[1], elev, spec)
                width = _width_at(dist, chain["ws"], station)
                share = 0.0 if axial is None else _share_one(x, y, nx, ny, width, axial + FLAT_ADD)
                chain["after_share"].append(share)
                after_shares.append(share)
        for s0, s1 in chain.get("spans") or []:
            piece = _substring(chain["pts"], chain["ws"], s0, s1)[0]
            if len(piece) < 2:
                continue
            line = LineString(piece)
            if line.length < 1.0:
                continue
            bauwerk_rows.append(
                {
                    "code": chain["code"],
                    "name": chain["name"],
                    "station_from": round(s0, 1),
                    "station_to": round(s1, 1),
                    "length_m": round(float(line.length), 1),
                    "short_ids": chain["key"],
                    "geometry": line,
                }
            )
    if before_shares:
        after_txt = "none" if not after_shares else str(np.round(np.mean(after_shares), 3))
        print(
            f"flat share at offset 0: {np.round(np.mean(before_shares), 3)} "
            f"on {len(before_shares)} stations -> {after_txt} on {len(after_shares)} stations",
            flush=True,
        )
    bau_m = sum(row["length_m"] for row in bauwerk_rows)
    print(
        f"bauwerk pieces {len(bauwerk_rows)}, length {bau_m:.0f} m, "
        f"narrow pieces {len(narrow_rows)}, taper pieces {len(taper_rows)}, "
        f"problem stations at half width {len(problems)}",
        flush=True,
    )
    print("code             len  narrow  flat share          bauwerk_m  name")
    table = []
    for chain in chains:
        length = float(LineString(chain["pts"]).length) if len(chain["pts"]) >= 2 else 0.0
        bau = sum(s1 - s0 for s0, s1 in chain.get("spans") or [])
        if length < 400.0 and bau < 1.0:
            continue
        before = chain.get("before_share") or []
        after = chain.get("after_share") or []
        narrow_m = max((base - width) for base, width in zip(chain["base"], chain["ws"])) if chain["ws"] else 0.0
        table.append(
            (
                str(chain["code"]),
                length,
                narrow_m,
                None if not before else float(np.mean(before)),
                None if not after else float(np.mean(after)),
                bau,
                chain["name"] or chain["key"].split(",")[0],
            )
        )
    for code, length, narrow_m, before, after, bau, name in sorted(table, key=lambda item: -item[1]):
        before_txt = "none" if before is None else f"{before:.3f}"
        after_txt = "none" if after is None else f"{after:.3f}"
        print(
            f"{code:16} {length:6.0f} {narrow_m:5.2f}  {before_txt} -> {after_txt}  {bau:8.0f}  {name}",
            flush=True,
        )

    crs = fit.crs
    dest = _dest(proc / "link_surface")
    gpd.read_file(src, layer="axis").to_file(dest, layer="axis", driver="GPKG")
    fit_rows = []
    surface_rows = []
    for chain in chains:
        width_m, width_along = _width_profile(chain["pts"], chain["ws"])
        fit_rows.append(
            {
                "code": chain["code"],
                "fc": chain["fc"],
                "fitted": chain["fitted"],
                "width_m": width_m,
                "width_along": width_along,
                "short_ids": chain["key"],
                "geometry": LineString(chain["pts"]),
            }
        )
        for part in _parts(chain["poly"]):
            if part.area < SLIVER_M2:
                continue
            surface_rows.append(
                {
                    "code": chain["code"],
                    "fc": chain["fc"],
                    "width_m": width_m,
                    "short_ids": chain["key"],
                    "area_m2": round(float(part.area), 1),
                    "geometry": part,
                }
            )
    gpd.GeoDataFrame(fit_rows, crs=crs).to_file(dest, layer="axis_fit", driver="GPKG")
    if surface_rows:
        gpd.GeoDataFrame(surface_rows, crs=crs).to_file(dest, layer="surface", driver="GPKG")
    if junction_rows:
        gpd.GeoDataFrame(junction_rows, crs=crs).to_file(dest, layer="junction", driver="GPKG")
    point_rows = []
    for row in reports:
        la, lb = row["through"]
        point_rows.append(
            {
                "fc": row["fc"],
                "off_straight_deg": row["off_straight_deg"],
                "n_side": row["n_side"],
                "through": f"{links[la]['short_id']},{links[lb]['short_id']}",
                "geometry": row["geometry"],
            }
        )
    if point_rows:
        gpd.GeoDataFrame(point_rows, crs=crs).to_file(dest, layer="junction_point", driver="GPKG")
    if narrow_rows:
        gpd.GeoDataFrame(narrow_rows, crs=crs).to_file(dest, layer="narrow", driver="GPKG")
    if taper_rows:
        gpd.GeoDataFrame(taper_rows, crs=crs).to_file(dest, layer="taper", driver="GPKG")
    if bauwerk_rows:
        gpd.GeoDataFrame(bauwerk_rows, crs=crs).to_file(dest, layer="bauwerk", driver="GPKG")
    if problems:
        gpd.GeoDataFrame(problems, crs=crs).to_file(dest, layer="problem", driver="GPKG")
    print(f"(open the following in qgis or similar tool to check: {dest}, {src})", flush=True)


if __name__ == "__main__":
    main()
