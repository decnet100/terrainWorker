"""Flat carriageway shelf from the GIP links and the raw 0.5 m terrain.

Stations every 5 m. A sample every 0.5 m across the GIP width is valid when
its slope is at most the axial grade to the next station, plus 20 percentage
points. The offset with the most valid samples wins. Counts within 1 sample
of that best count, or within 10 percent of it, are tied, and the tie keeps
the smaller height difference of the two carriageway edges. The station is
kept only when the valid samples cover at least 20 percent of the width. On each
unbroken kept stretch that offset is then averaged with its two neighbours,
four times. A shift that is the same all along the stretch stays that shift.
The average stops at a bridge, a structure, or an open end.

The shelf is horizontal across the carriageway at the ground height of the
shifted centerline. The raster writes every cell whose centre lies in the
carriageway or in a 1 m buffer around it, at that centerline height. The
next cell outside the buffer is left empty. A gap of 10 m or less whose ground stays
within 1 m of the chord between the valid stations stays in the road. Any
longer gap, and any gap whose ground falls 1 m or more below that chord, is
an unmarked bridge and is not stamped. A failed stretch with no valid station
on one side is left out and is not a bridge. Known bridges, tunnels and
galleries are left out. Where two carriageways overlap, the shared surface is
a junction and is left empty.

Does not write a BeamNG level.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\build_flat_shelf.py
"""
from __future__ import annotations

import gc
import math
import sys
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union
from shapely.prepared import prep

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from authorities import resolved_working_crs  # noqa: E402
from build_link_surfaces import (  # noqa: E402
    BEARING_M,
    JUNCTION_MIN_M2,
    SLIVER_M2,
    _at_node_end,
    _backoff,
    _chains,
    _code,
    _cum,
    _parts,
    _plan_junctions,
    _point_at,
    _ray_hits,
    _ribbon,
    _xy,
)
from build_road_grid import RES_M, _ensure_raw_mosaic, _load_geotiff  # noqa: E402
from filter_road_corridor import write_referenced_tif  # noqa: E402
from gip_routing_load import gpkg_path, load_routing  # noqa: E402
from score_link_surfaces import _z_at  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

STATION_M = 5.0
ACROSS_M = 0.5
OFFSETS = np.round(np.arange(-2.0, 2.0 + 1e-9, 0.1), 2)
FLAT_ADD = 20.0
FLAT_ADD_PREV = 15.0
SMOOTH_PASSES = 4
MIN_FRACTION = 0.20
SHOULDER_M = 1.0
SPAN_KEEP_M = 10.0
DIP_OPEN_M = 1.0
STRUCT_SKIP_M = 1.0
SIDE_M = (1.0, 2.0, 3.0)
CHUNK_M = 120.0


def _need(width: float) -> int:
    """Valid samples required: 20 percent of the width, rounded up to one sample."""
    return max(1, int(math.ceil(MIN_FRACTION * width / ACROSS_M - 1e-9)))


def _across(half: float) -> np.ndarray:
    """Samples from the right edge to the left edge, including both edges."""
    inside = np.arange(ACROSS_M, half, ACROSS_M)
    across = np.concatenate((-inside[::-1], [0.0], inside)) if inside.size else np.array([0.0])
    return np.unique(np.round(np.concatenate(([-half], across, [half])), 5))


def _slope_percent(xs: np.ndarray, ys: np.ndarray, elev: np.ndarray, spec: dict) -> np.ndarray:
    """Slope magnitude of the raw terrain, in percent, from the neighbouring cells."""
    res = spec["resolution_m"]
    col = np.floor((xs - spec["xmin"]) / res).astype(np.int32)
    row = np.floor((spec["ymax"] - ys) / res).astype(np.int32)
    height, width = elev.shape

    def take(rr, cc):
        out = np.full(xs.shape, np.nan, dtype=np.float64)
        ok = (rr >= 0) & (cc >= 0) & (rr < height) & (cc < width)
        out[ok] = elev[rr[ok], cc[ok]]
        return out

    dzdx = (take(row, col + 1) - take(row, col - 1)) / (2.0 * res)
    dzdy = (take(row - 1, col) - take(row + 1, col)) / (2.0 * res)
    return 100.0 * np.hypot(dzdx, dzdy)


def _struct_kind(text: str) -> str:
    folded = (
        str(text or "")
        .lower()
        .replace("ü", "ue")
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ß", "ss")
    )
    if "galerie" in folded:
        return "gallery"
    if "tunnel" in folded:
        return "tunnel"
    if "bruecke" in folded:
        return "bridge"
    return "structure"


def _normal(line: LineString, station: float) -> tuple[float, float]:
    length = float(line.length)
    before = line.interpolate(max(0.0, station - 1.0))
    after = line.interpolate(min(length, station + 1.0))
    dx = float(after.x - before.x)
    dy = float(after.y - before.y)
    span = math.hypot(dx, dy)
    if span < 1e-6:
        return 0.0, 0.0
    return -dy / span, dx / span


def _band_pick(count, edge, width):
    """Offset with the most samples under the limit. Ties use the smaller edge difference."""
    need = _need(width)
    eligible = count >= need
    best = int(count.max()) if count.size else 0
    if np.any(eligible):
        band = eligible & ((best - count <= 1) | ((best - count) < 0.10 * best))
    else:
        band = count == best
    idx = np.flatnonzero(band)
    pick = int(idx[np.lexsort((np.abs(OFFSETS[idx]), edge[idx]))[0]])
    return pick, int(count[pick]), need, bool(np.any(eligible))


def _search_one(x, y, nx, ny, width, axial, elev, spec):
    """Best offset at axial plus 20. Also whether axial plus 15 would have kept the station."""
    half = width / 2.0
    if half < ACROSS_M * 0.5 or axial is None or not np.isfinite(axial):
        return None
    across = _across(half)
    shift = OFFSETS[:, None] + across[None, :]
    slope = _slope_percent(
        (x + nx * shift).ravel(),
        (y + ny * shift).ravel(),
        elev,
        spec,
    ).reshape(shift.shape)
    finite = np.isfinite(slope)
    count_now = ((slope <= axial + FLAT_ADD + 1e-6) & finite).sum(axis=1)
    count_prev = ((slope <= axial + FLAT_ADD_PREV + 1e-6) & finite).sum(axis=1)
    left = _z_at(x + nx * (OFFSETS + half), y + ny * (OFFSETS + half), elev, spec)
    right = _z_at(x + nx * (OFFSETS - half), y + ny * (OFFSETS - half), elev, spec)
    edge = np.abs(left - right)
    edge[~np.isfinite(edge)] = 1.0e9
    pick, n_valid, need, ok = _band_pick(count_now, edge, width)
    _prev_pick, _prev_n, _prev_need, ok_prev = _band_pick(count_prev, edge, width)
    diff = None if edge[pick] >= 1.0e8 else round(float(edge[pick]), 3)
    return float(OFFSETS[pick]), n_valid, need, diff, ok, ok_prev


def _ground(x: float, y: float, elev, spec) -> float:
    z = _z_at(np.array([x]), np.array([y]), elev, spec)
    return float(z[0])


def _sides(x, y, nx, ny, zc, elev, spec) -> dict:
    """Cross slope at 1, 2 and 3 m. Positive means that side is higher than the center."""
    out = {}
    for name, sign in (("l", 1.0), ("r", -1.0)):
        for dist in SIDE_M:
            key = f"{name}{int(dist)}"
            if not np.isfinite(zc):
                out[f"dz_{key}_m"] = None
                out[f"slope_{key}_pct"] = None
                continue
            zg = _ground(x + nx * sign * dist, y + ny * sign * dist, elev, spec)
            if not np.isfinite(zg):
                out[f"dz_{key}_m"] = None
                out[f"slope_{key}_pct"] = None
                continue
            dz = zg - zc
            out[f"dz_{key}_m"] = round(dz, 3)
            out[f"slope_{key}_pct"] = round(100.0 * dz / dist, 2)
    return out


def _dip(line, s0, s1, z0, z1, off0, off1, elev, spec) -> float:
    """Largest drop of the ground below the chord between two valid stations."""
    span = s1 - s0
    if span < 0.5 or not np.isfinite(z0) or not np.isfinite(z1):
        return 0.0
    n = max(2, int(math.ceil(span)))
    drops = []
    for i in range(1, n):
        s = s0 + span * i / n
        t = (s - s0) / span
        point = line.interpolate(s)
        nx, ny = _normal(line, s)
        off = off0 + (off1 - off0) * t
        zg = _ground(float(point.x) + nx * off, float(point.y) + ny * off, elev, spec)
        if not np.isfinite(zg):
            continue
        chord = z0 + (z1 - z0) * t
        drops.append(chord - zg)
    if not drops:
        return 0.0
    return float(max(drops))


def _classify(stations, line, elev, spec) -> None:
    """Mark gaps. A short shallow gap stays. Everything else between valid stations is a bridge."""
    groups: list[list[int]] = []
    current: list[int] = []
    for i, station in enumerate(stations):
        if station["status"] == "structure":
            if current:
                groups.append(current)
                current = []
            continue
        current.append(i)
    if current:
        groups.append(current)
    for group in groups:
        valid = [i for i in group if stations[i]["ok"]]
        if not valid:
            for i in group:
                stations[i]["status"] = "open_end"
            continue
        first, last = valid[0], valid[-1]
        for i in group:
            if i < first or i > last:
                stations[i]["status"] = "open_end"
        for a, b in zip(valid, valid[1:]):
            if b == a + 1:
                continue
            span = stations[b]["s"] - stations[a]["s"]
            dip = _dip(
                line,
                stations[a]["s"],
                stations[b]["s"],
                stations[a]["z"],
                stations[b]["z"],
                stations[a]["offset_m"],
                stations[b]["offset_m"],
                elev,
                spec,
            )
            bridge = span > SPAN_KEEP_M + 1e-6 or dip >= DIP_OPEN_M
            for i in range(a + 1, b):
                if bridge:
                    stations[i]["status"] = "bridge"
                    stations[i]["offset_m"] = 0.0
                    stations[i]["dip_m"] = round(dip, 2)
                    stations[i]["span_m"] = round(span, 1)
                    stations[i]["bridge_reason"] = "drop" if dip >= DIP_OPEN_M else "span"
                else:
                    t = (stations[i]["s"] - stations[a]["s"]) / span
                    stations[i]["status"] = "culvert"
                    stations[i]["offset_m"] = stations[a]["offset_m"] + (
                        stations[b]["offset_m"] - stations[a]["offset_m"]
                    ) * t
                    stations[i]["dip_m"] = round(dip, 2)
                    stations[i]["span_m"] = round(span, 1)


def _smooth_offsets(stations: list[dict]) -> None:
    """Average the sideways offset along a kept stretch. A constant shift stays."""
    start = 0
    while start < len(stations):
        if stations[start]["status"] not in ("kept", "culvert"):
            start += 1
            continue
        end = start + 1
        while end < len(stations) and stations[end]["status"] in ("kept", "culvert"):
            end += 1
        values = [float(stations[i]["offset_m"]) for i in range(start, end)]
        for _ in range(SMOOTH_PASSES):
            nxt = values[:]
            last = len(values) - 1
            for i in range(len(values)):
                prev = values[i - 1] if i > 0 else values[i]
                nxt_value = values[i + 1] if i < last else values[i]
                nxt[i] = 0.25 * prev + 0.5 * values[i] + 0.25 * nxt_value
            values = nxt
        for i, value in enumerate(values):
            stations[start + i]["offset_m"] = value
        start = end


def _on_structure(station_s: float, spans: list[tuple[float, float]]) -> bool:
    for a, b in spans:
        if a - STRUCT_SKIP_M <= station_s <= b + STRUCT_SKIP_M:
            return True
    return False


def _assemble(order, links, lines, ends):
    """Orient the links and record width breaks and structure spans along the chain."""
    coords: list[tuple[float, float]] = []
    breaks: list[tuple[float, float]] = []
    spans: list[tuple[float, float, str, str]] = []
    prev = None
    start_node = None
    cursor = 0.0
    for lid in order:
        rec = links[lid]
        raw = list(lines[lid])
        link_len = _cum(raw)[-1]
        a, b = ends[lid]
        reversed_link = False
        if prev is None:
            if len(order) > 1:
                na, nb = ends[order[1]]
                if a in (na, nb):
                    raw = list(reversed(raw))
                    reversed_link = True
                    start_node = b
                    prev = a
                else:
                    start_node = a
                    prev = b
            else:
                start_node = a
                prev = b
        elif a == prev:
            prev = b
        else:
            raw = list(reversed(raw))
            reversed_link = True
            prev = a
        width = float(rec["width_m"])
        if coords and math.hypot(coords[-1][0] - raw[0][0], coords[-1][1] - raw[0][1]) < 0.05:
            raw = raw[1:]
        if not breaks or abs(breaks[-1][1] - width) >= 0.05:
            breaks.append((cursor, width))
        for struct in rec.get("structures") or []:
            lo = min(100.0, max(0.0, float(struct["from_pct"]))) / 100.0
            hi = min(100.0, max(0.0, float(struct["to_pct"]))) / 100.0
            if reversed_link:
                lo, hi = 1.0 - hi, 1.0 - lo
            if hi < lo:
                lo, hi = hi, lo
            if hi - lo < 1e-6:
                continue
            spans.append(
                (
                    cursor + lo * link_len,
                    cursor + hi * link_len,
                    _struct_kind(struct.get("type") or ""),
                    str(struct.get("name") or ""),
                )
            )
        coords.extend(raw)
        cursor += link_len
    return coords, breaks, spans, start_node, prev


def _width_at(breaks, station: float) -> float:
    width = breaks[0][1]
    for at, value in breaks:
        if at <= station + 1e-6:
            width = value
        else:
            break
    return width


def _stations_for(line, breaks, spans, elev, spec) -> list[dict]:
    length = float(line.length)
    stops = list(np.arange(0.0, length + 1e-6, STATION_M))
    if not stops or length - stops[-1] > 0.5:
        stops.append(length)
    cover = [(a, b) for a, b, _kind, _name in spans]
    rows = []
    for station in stops:
        point = line.interpolate(float(station))
        nx, ny = _normal(line, float(station))
        width = _width_at(breaks, float(station))
        ahead = min(length, float(station) + STATION_M)
        if ahead - float(station) < 0.5:
            ahead = float(station)
            back = max(0.0, float(station) - STATION_M)
            pa, pb = line.interpolate(back), point
        else:
            pa, pb = point, line.interpolate(ahead)
        za = _ground(float(pa.x), float(pa.y), elev, spec)
        zb = _ground(float(pb.x), float(pb.y), elev, spec)
        dist = math.hypot(float(pb.x) - float(pa.x), float(pb.y) - float(pa.y))
        axial = None if dist < 0.5 or not np.isfinite(za) or not np.isfinite(zb) else 100.0 * abs(zb - za) / dist
        row = {
            "s": float(station),
            "x": float(point.x),
            "y": float(point.y),
            "nx": nx,
            "ny": ny,
            "width_m": width,
            "axial_pct": None if axial is None else round(axial, 2),
            "offset_m": 0.0,
            "trial_offset_m": None,
            "n_valid": 0,
            "n_need": _need(width),
            "edge_diff_m": None,
            "ok": False,
            "ok_prev": False,
            "status": "structure" if _on_structure(float(station), cover) else "pending",
            "z": None,
            "dip_m": None,
            "span_m": None,
            "bridge_reason": None,
        }
        if row["status"] != "structure" and abs(nx) + abs(ny) > 0:
            found = _search_one(row["x"], row["y"], nx, ny, width, axial, elev, spec)
            if found is not None:
                offset, n_valid, need, diff, ok, ok_prev = found
                row["trial_offset_m"] = offset
                row["n_valid"] = n_valid
                row["n_need"] = need
                row["edge_diff_m"] = diff
                row["ok"] = ok
                row["ok_prev"] = ok_prev
                row["pass15"] = ok_prev
                row["pass20"] = ok
                if ok:
                    row["offset_m"] = offset
                    row["status"] = "kept"
        rows.append(row)
    for row in rows:
        if row["status"] == "structure":
            continue
        row["z"] = _ground(row["x"] + row["nx"] * row["offset_m"], row["y"] + row["ny"] * row["offset_m"], elev, spec)
        if not np.isfinite(row["z"]):
            row["z"] = None
            row["ok"] = False
            row["ok_prev"] = False
            if row["status"] == "kept":
                row["status"] = "pending"
                row["offset_m"] = 0.0
    _classify(rows, line, elev, spec)
    _smooth_offsets(rows)
    for row in rows:
        if row["status"] in ("culvert", "kept"):
            row["x"] = row["x"] + row["nx"] * row["offset_m"]
            row["y"] = row["y"] + row["ny"] * row["offset_m"]
            zg = _ground(row["x"], row["y"], elev, spec)
            row["z"] = None if not np.isfinite(zg) else zg
        sides = _sides(row["x"], row["y"], row["nx"], row["ny"], row["z"] if row["z"] is not None else np.nan, elev, spec)
        row.update(sides)
        if row["z"] is not None:
            row["z"] = round(float(row["z"]), 3)
    return rows


def _dense_points(line, stations, breaks):
    """Chain vertices plus stations. Shelf points lie between kept or culvert stations."""
    length = float(line.length)
    kept = []
    for a, b in zip(stations, stations[1:]):
        if a["status"] in ("kept", "culvert") and b["status"] in ("kept", "culvert"):
            kept.append((a["s"], b["s"]))
    marks = {round(float(s), 3) for s in _cum(_xy(line))}
    for station in stations:
        marks.add(round(station["s"], 3))
    for at, _width in breaks:
        marks.add(round(at, 3))
    ordered = [s for s in sorted(marks) if -1e-6 <= s <= length + 1e-6]

    def bracket(station_s: float):
        for i in range(1, len(stations)):
            if stations[i]["s"] + 1e-6 >= station_s:
                return stations[i - 1], stations[i]
        return stations[-2], stations[-1]

    def on_shelf(station_s: float) -> bool:
        return any(a - 1e-6 <= station_s <= b + 1e-6 for a, b in kept)

    points = []
    for station_s in ordered:
        left, right = bracket(station_s)
        span = right["s"] - left["s"]
        t = 0.0 if span < 1e-6 else (station_s - left["s"]) / span
        offset = left["offset_m"] + (right["offset_m"] - left["offset_m"]) * t
        z_left = left["z"]
        z_right = right["z"]
        z = None
        if z_left is not None and z_right is not None:
            z = z_left + (z_right - z_left) * t
        elif z_left is not None:
            z = z_left
        elif z_right is not None:
            z = z_right
        base = line.interpolate(station_s)
        nx, ny = _normal(line, station_s)
        points.append(
            {
                "s": station_s,
                "x": float(base.x) + nx * offset,
                "y": float(base.y) + ny * offset,
                "z": z,
                "w": _width_at(breaks, station_s),
                "shelf": on_shelf(station_s) and z is not None,
            }
        )
    return points


def _slice(points, s0: float, s1: float) -> LineString | None:
    if s1 - s0 < 0.5 or len(points) < 2:
        return None
    ss = [p["s"] for p in points]

    def at(station_s: float):
        if station_s <= ss[0]:
            return points[0]["x"], points[0]["y"]
        if station_s >= ss[-1]:
            return points[-1]["x"], points[-1]["y"]
        for i in range(1, len(ss)):
            if ss[i] + 1e-9 >= station_s:
                span = ss[i] - ss[i - 1]
                t = 0.0 if span < 1e-9 else (station_s - ss[i - 1]) / span
                return (
                    points[i - 1]["x"] + t * (points[i]["x"] - points[i - 1]["x"]),
                    points[i - 1]["y"] + t * (points[i]["y"] - points[i - 1]["y"]),
                )
        return points[-1]["x"], points[-1]["y"]

    coords = [at(s0)]
    for point in points:
        if s0 + 1e-4 < point["s"] < s1 - 1e-4:
            coords.append((point["x"], point["y"]))
    coords.append(at(s1))
    simple = [coords[0]]
    for coord in coords[1:]:
        if math.hypot(coord[0] - simple[-1][0], coord[1] - simple[-1][1]) >= 0.05:
            simple.append(coord)
    if len(simple) < 2:
        return None
    return LineString(simple)


def _runs(points) -> list[list[dict]]:
    runs = []
    current = []
    for point in points:
        if point["shelf"]:
            current.append(point)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _with_width_steps(run, breaks) -> tuple[list[tuple[float, float]], list[float]]:
    """Hold the width up to a break, then step. The ribbon does not ramp across the break."""
    xy = [(run[0]["x"], run[0]["y"])]
    widths = [run[0]["w"]]
    for point in run[1:]:
        if abs(point["w"] - widths[-1]) >= 0.05:
            xy.append((point["x"], point["y"]))
            widths.append(widths[-1])
        xy.append((point["x"], point["y"]))
        widths.append(point["w"])
    return xy, widths


def _chunks(run: list[dict]) -> list[list[dict]]:
    """Short pieces so each corridor has a small cell box. The join point is shared."""
    chunks: list[list[dict]] = []
    current = [run[0]]
    walked = 0.0
    for point in run[1:]:
        walked += math.hypot(point["x"] - current[-1]["x"], point["y"] - current[-1]["y"])
        current.append(point)
        if walked >= CHUNK_M:
            chunks.append(current)
            current = [point]
            walked = 0.0
    if len(current) >= 2:
        chunks.append(current)
    elif chunks and len(current) == 1:
        chunks[-1].append(current[0])
    return chunks


def _centerline(run: list[dict]):
    """Plan line and the shelf height along it. Duplicate points keep the later height."""
    xy: list[tuple[float, float]] = []
    zs: list[float] = []
    ss: list[float] = [0.0]
    for point in run:
        if point["z"] is None:
            continue
        if xy and math.hypot(point["x"] - xy[-1][0], point["y"] - xy[-1][1]) < 0.05:
            zs[-1] = float(point["z"])
            continue
        if xy:
            ss.append(ss[-1] + math.hypot(point["x"] - xy[-1][0], point["y"] - xy[-1][1]))
        xy.append((point["x"], point["y"]))
        zs.append(float(point["z"]))
    if len(xy) < 2:
        return None, None, None
    return LineString(xy), np.asarray(ss, dtype=np.float64), np.asarray(zs, dtype=np.float64)


def _fill_polygon(poly, line, ss, zs, spec, grid, r0, c0) -> int:
    """Write every cell whose centre lies in `poly`, at the centerline height there."""
    if poly is None or poly.is_empty or line is None:
        return 0
    res = spec["resolution_m"]
    minx, miny, maxx, maxy = poly.bounds
    c_lo = max(c0, int(math.floor((minx - spec["xmin"]) / res)))
    c_hi = min(c0 + grid.shape[1] - 1, int(math.floor((maxx - spec["xmin"]) / res)))
    r_lo = max(r0, int(math.floor((spec["ymax"] - maxy) / res)))
    r_hi = min(r0 + grid.shape[0] - 1, int(math.floor((spec["ymax"] - miny) / res)))
    if c_hi < c_lo or r_hi < r_lo:
        return 0
    written = 0
    for row in range(r_lo, r_hi + 1):
        cy = spec["ymax"] - (row + 0.5) * res
        cols = np.arange(c_lo, c_hi + 1, dtype=np.int32)
        cx = spec["xmin"] + (cols.astype(np.float64) + 0.5) * res
        inside = shapely.contains_xy(poly, cx, np.full(cx.shape, cy, dtype=np.float64))
        if not np.any(inside):
            continue
        picked = cx[inside]
        dist = shapely.line_locate_point(line, shapely.points(picked, np.full(picked.shape, cy)))
        height = np.interp(np.asarray(dist, dtype=np.float64), ss, zs)
        grid[row - r0, cols[inside] - c0] = height.astype(np.float32)
        written += int(picked.size)
    return written


def _paint_run(run, spec, grid, r0, c0, shoulder: bool) -> int:
    """Fill the carriageway. With `shoulder`, also fill the 1 m buffer around it."""
    written = 0
    for chunk in _chunks(run):
        xy, widths = _with_width_steps(chunk, [])
        line, ss, zs = _centerline(chunk)
        if line is None:
            continue
        for part in _parts(_ribbon(xy, widths)):
            target = part.buffer(SHOULDER_M) if shoulder else part
            for piece in _parts(target):
                written += _fill_polygon(piece, line, ss, zs, spec, grid, r0, c0)
    return written


def _clear_polygon(poly: Polygon, spec, grid, r0, c0) -> int:
    minx, miny, maxx, maxy = poly.bounds
    res = spec["resolution_m"]
    c_lo = max(0, int(math.floor((minx - spec["xmin"]) / res)))
    c_hi = min(spec["width"] - 1, int(math.floor((maxx - spec["xmin"]) / res)))
    r_lo = max(0, int(math.floor((spec["ymax"] - maxy) / res)))
    r_hi = min(spec["height"] - 1, int(math.floor((spec["ymax"] - miny) / res)))
    if c_hi < c_lo or r_hi < r_lo:
        return 0
    prepared = prep(poly)
    cleared = 0
    for row in range(r_lo, r_hi + 1):
        cy = spec["ymax"] - (row + 0.5) * res
        for col in range(c_lo, c_hi + 1):
            cx = spec["xmin"] + (col + 0.5) * res
            if prepared.covers(Point(cx, cy)):
                rr, cc = row - r0, col - c0
                if 0 <= rr < grid.shape[0] and 0 <= cc < grid.shape[1]:
                    grid[rr, cc] = np.nan
                    cleared += 1
    return cleared


def _window(chains, spec) -> tuple[int, int, int, int]:
    xs = []
    ys = []
    pad = 4.0
    for chain in chains:
        for point in chain["points"]:
            if not point["shelf"]:
                continue
            xs.append(point["x"])
            ys.append(point["y"])
            pad = max(pad, point["w"] / 2.0 + SHOULDER_M + 1.0)
    if not xs:
        return 0, 0, 1, 1
    res = spec["resolution_m"]
    c0 = max(0, int(math.floor((min(xs) - pad - spec["xmin"]) / res)))
    c1 = min(spec["width"] - 1, int(math.floor((max(xs) + pad - spec["xmin"]) / res)))
    r0 = max(0, int(math.floor((spec["ymax"] - (max(ys) + pad)) / res)))
    r1 = min(spec["height"] - 1, int(math.floor((spec["ymax"] - (min(ys) - pad)) / res)))
    return r0, c0, max(1, r1 - r0 + 1), max(1, c1 - c0 + 1)


def _seat_end(chain, point, xy, elev, spec) -> None:
    """Move a chain end onto the junction and take the ground height there."""
    point["x"], point["y"] = xy
    zg = _ground(xy[0], xy[1], elev, spec)
    if np.isfinite(zg):
        point["z"] = float(zg)
    for station in chain["stations"]:
        if abs(station["s"] - point["s"]) > 0.05:
            continue
        station["x"], station["y"] = xy
        if np.isfinite(zg):
            station["z"] = round(float(zg), 3)
            station.update(_sides(xy[0], xy[1], station["nx"], station["ny"], float(zg), elev, spec))


def _line_rows(stations, points, kind: str) -> list[tuple[float, float, dict]]:
    """Contiguous failed stretches. Bridge lines run from one valid station to the next."""
    rows = []
    i = 0
    while i < len(stations):
        if stations[i]["status"] != kind:
            i += 1
            continue
        j = i
        while j < len(stations) and stations[j]["status"] == kind:
            j += 1
        s0 = stations[i]["s"]
        s1 = stations[j - 1]["s"]
        if i > 0 and stations[i - 1]["status"] in ("kept", "culvert"):
            s0 = stations[i - 1]["s"]
        if j < len(stations) and stations[j]["status"] in ("kept", "culvert"):
            s1 = stations[j]["s"]
        dip = stations[i].get("dip_m")
        span = stations[i].get("span_m")
        reason = stations[i].get("bridge_reason")
        rows.append((s0, s1, {"dip_m": dip, "span_m": span if span is not None else round(s1 - s0, 1), "reason": reason}))
        i = j
    return rows


def main() -> None:
    site = load_site()
    crs = resolved_working_crs(site)
    topo = load_routing(site)
    links = topo["links"]
    geoms = {
        row.link_id: row.geometry
        for row in gpd.read_file(gpkg_path(site), layer="link").itertuples(index=False)
    }
    proc = processed_dir(site)
    _ensure_raw_mosaic(site, proc)
    elev, spec = _load_geotiff(proc / "corridor50_raw" / "corridor50_raw.tif")
    print(f"flat shelf: stations {STATION_M:.0f} m, a 4 m road needs {_need(4.0)} valid samples", flush=True)

    ends = {}
    lines = {}
    by_fc = defaultdict(list)
    for lid, rec in links.items():
        line = geoms.get(lid)
        if line is None or line.is_empty or not rec.get("width_m"):
            continue
        ends[lid] = (rec["node_from"], rec["node_to"])
        lines[lid] = _xy(line)
        by_fc[int(rec["fc"])].append(lid)
    skipped = sum(1 for rec in links.values() if not rec.get("width_m"))
    forced, plans = _plan_junctions(lines, ends, by_fc, topo["nodes"])
    print(f"links with width {len(lines)}, without width {skipped}, junctions planned {sum(len(v) for v in plans.values())}", flush=True)

    node_j: dict[str, tuple[int, tuple[float, float]]] = {}
    reports = []
    chains = []
    for fc in sorted(by_fc):
        code = _code(fc)
        ready = []
        for order in _chains(by_fc[fc], ends, forced.get(fc, {})):
            coords, breaks, spans, start_node, end_node = _assemble(order, links, lines, ends)
            if len(coords) < 2 or start_node is None:
                continue
            line = LineString(coords)
            if line.length < 1.0:
                continue
            stations = _stations_for(line, breaks, spans, elev, spec)
            points = _dense_points(line, stations, breaks)
            short_ids = ",".join(str(links[lid]["short_id"]) for lid in order)
            ready.append(
                {
                    "order": order,
                    "points": points,
                    "stations": stations,
                    "spans": spans,
                    "start_node": start_node,
                    "end_node": end_node,
                    "fc": fc,
                    "code": code,
                    "short_ids": short_ids,
                    "name": links[order[0]].get("name") or "",
                }
            )
            if (len(chains) + len(ready)) % 25 == 0:
                print(f"  chains {len(chains) + len(ready)}", flush=True)
        for plan in plans.get(fc, []):
            through = next(
                (c for c in ready if plan["through"][0] in c["order"] and plan["through"][1] in c["order"]),
                None,
            )
            if through is None:
                continue
            through_xy = [(p["x"], p["y"]) for p in through["points"]]
            alongs = []
            sides = []
            for sid in plan["sides"]:
                side = next((c for c in ready if sid in c["order"]), None)
                if side is None:
                    continue
                at_start = side["start_node"] == plan["node"]
                seq = [(p["x"], p["y"]) for p in side["points"]]
                seq = seq if at_start else list(reversed(seq))
                if len(seq) < 2:
                    continue
                origin = _point_at(seq, min(BEARING_M, max(4.0, _cum(seq)[-1] * 0.5)))
                found = _ray_hits(origin, seq[0], through_xy, plan["nxy"])
                if found is None:
                    continue
                alongs.append(found[1])
                sides.append((side, at_start))
            if not alongs:
                continue
            junction = _point_at(through_xy, sum(alongs) / len(alongs))
            node_j[plan["node"]] = (fc, junction)
            for side, at_start in sides:
                target = side["points"][0] if at_start else side["points"][-1]
                _seat_end(side, target, junction, elev, spec)
            reports.append((fc, plan["dev"], junction, plan["through"], len(plan["sides"])))
        for chain in ready:
            for node, idx in ((chain["start_node"], 0), (chain["end_node"], -1)):
                owned = node_j.get(node)
                if owned is not None and owned[0] < fc:
                    _seat_end(chain, chain["points"][idx], owned[1], elev, spec)
            chains.append(chain)
    print(f"chains {len(chains)}", flush=True)

    surface_by_chain = []
    structure_rows = []
    bridge_rows = []
    open_rows = []
    station_rows = []
    axis_rows = []
    for chain in chains:
        points = chain["points"]
        xy = [(p["x"], p["y"]) for p in points]
        if len(xy) >= 2:
            axis_rows.append(
                {
                    "code": chain["code"],
                    "fc": chain["fc"],
                    "short_ids": chain["short_ids"],
                    "name": chain["name"],
                    "geometry": LineString(xy),
                }
            )
        pieces = []
        for run in _runs(points):
            run_xy, run_w = _with_width_steps(run, [])
            poly = _ribbon(run_xy, run_w)
            for part in _parts(poly):
                if part.area < SLIVER_M2:
                    continue
                pieces.append(part)
        surface_by_chain.append((chain, pieces))
        for s0, s1, kind, name in chain["spans"]:
            geom = _slice(points, s0, s1)
            if geom is None:
                continue
            structure_rows.append(
                {
                    "kind": kind,
                    "name": name,
                    "code": chain["code"],
                    "short_ids": chain["short_ids"],
                    "length_m": round(s1 - s0, 1),
                    "geometry": geom,
                }
            )
        for s0, s1, props in _line_rows(chain["stations"], points, "bridge"):
            geom = _slice(points, s0, s1)
            if geom is None:
                continue
            bridge_rows.append(
                {
                    "reason": props["reason"],
                    "dip_m": props["dip_m"],
                    "span_m": props["span_m"],
                    "code": chain["code"],
                    "short_ids": chain["short_ids"],
                    "geometry": geom,
                }
            )
        for s0, s1, props in _line_rows(chain["stations"], points, "open_end"):
            geom = _slice(points, s0, s1)
            if geom is None:
                continue
            open_rows.append(
                {
                    "code": chain["code"],
                    "short_ids": chain["short_ids"],
                    "length_m": round(s1 - s0, 1),
                    "geometry": geom,
                }
            )
        for station in chain["stations"]:
            station_rows.append(
                {
                    "code": chain["code"],
                    "fc": chain["fc"],
                    "short_ids": chain["short_ids"],
                    "station_m": round(station["s"], 1),
                    "status": station["status"],
                    "width_m": station["width_m"],
                    "offset_m": round(station["offset_m"], 2),
                    "trial_offset_m": station["trial_offset_m"],
                    "n_valid": station["n_valid"],
                    "n_need": station["n_need"],
                    "edge_diff_m": station["edge_diff_m"],
                    "axial_pct": station["axial_pct"],
                    "z_m": station["z"],
                    "dip_m": station["dip_m"],
                    "span_m": station["span_m"],
                    "bridge_reason": station["bridge_reason"],
                    **{key: station.get(key) for key in (
                        "dz_l1_m", "dz_l2_m", "dz_l3_m", "dz_r1_m", "dz_r2_m", "dz_r3_m",
                        "slope_l1_pct", "slope_l2_pct", "slope_l3_pct",
                        "slope_r1_pct", "slope_r2_pct", "slope_r3_pct",
                    )},
                    "geometry": Point(station["x"], station["y"]),
                }
            )

    chain_geoms = []
    chain_index = []
    for index, (_chain, pieces) in enumerate(surface_by_chain):
        if not pieces:
            continue
        chain_geoms.append(unary_union(pieces))
        chain_index.append(index)
    junction_rows = []
    junction_geoms = []
    if len(chain_geoms) >= 2:
        tree = shapely.STRtree(chain_geoms)
        pairs = tree.query(chain_geoms, predicate="intersects")
        seen = set()
        for i, j in pairs.T:
            a, b = int(i), int(j)
            if a >= b or (a, b) in seen:
                continue
            seen.add((a, b))
            inter = chain_geoms[a].intersection(chain_geoms[b])
            if inter.is_empty or inter.area < JUNCTION_MIN_M2:
                continue
            ca = surface_by_chain[chain_index[a]][0]
            cb = surface_by_chain[chain_index[b]][0]
            for part in _parts(inter):
                if part.area < JUNCTION_MIN_M2:
                    continue
                junction_geoms.append(part)
                junction_rows.append(
                    {
                        "a": ca["short_ids"],
                        "b": cb["short_ids"],
                        "code_a": ca["code"],
                        "code_b": cb["code"],
                        "area_m2": round(part.area, 1),
                        "geometry": part,
                    }
                )
    junction_union = unary_union(junction_geoms) if junction_geoms else None
    surface_rows = []
    for chain, pieces in surface_by_chain:
        for poly in pieces:
            geom = poly if junction_union is None else poly.difference(junction_union)
            for part in _parts(geom):
                if part.area < SLIVER_M2:
                    continue
                surface_rows.append(
                    {
                        "code": chain["code"],
                        "fc": chain["fc"],
                        "short_ids": chain["short_ids"],
                        "name": chain["name"],
                        "area_m2": round(part.area, 1),
                        "geometry": part,
                    }
                )

    del elev
    gc.collect()
    r0, c0, height, width = _window(chains, spec)
    print(f"raster window {width} x {height} cells", flush=True)
    nbytes = height * width * 4
    grid_path = proc / "link_surface" / "_shelf_grid.dat"
    if nbytes > 256_000_000:
        grid = np.memmap(grid_path, dtype=np.float32, mode="w+", shape=(height, width))
        grid[:] = np.nan
    else:
        grid = np.full((height, width), np.nan, dtype=np.float32)
    painted = 0
    for chain in chains:
        for run in _runs(chain["points"]):
            painted += _paint_run(run, spec, grid, r0, c0, shoulder=True)
    # The carriageway is painted again so a neighbour's shoulder cannot leave its height there.
    for chain in chains:
        for run in _runs(chain["points"]):
            _paint_run(run, spec, grid, r0, c0, shoulder=False)
    cleared = 0
    for poly in junction_geoms:
        cleared += _clear_polygon(poly, spec, grid, r0, c0)
    print(f"painted cells {painted}, junction cells cleared {cleared}", flush=True)

    out_dir = proc / "link_surface"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = None
    for name in ("surface_shelf.gpkg", "surface_shelf_next.gpkg", "surface_shelf_smooth.gpkg"):
        candidate = out_dir / name
        if not candidate.exists():
            dest = candidate
            break
        try:
            candidate.unlink()
            dest = candidate
            break
        except PermissionError:
            print(f"{name} is open, trying the next file", flush=True)
    if dest is None:
        raise SystemExit("close the open shelf geopackages and run again")
    layers = {
        "axis": axis_rows,
        "surface": surface_rows,
        "junction": junction_rows,
        "bridge": bridge_rows,
        "open_end": open_rows,
        "structure": structure_rows,
        "station": station_rows,
    }
    first = True
    for name, rows in layers.items():
        if not rows:
            continue
        frame = gpd.GeoDataFrame(rows, crs=crs)
        frame.to_file(dest, layer=name, driver="GPKG", mode="w" if first else "a")
        first = False
    if first:
        raise SystemExit("no shelf geometry")
    raster_path = dest.with_suffix(".tif")
    xmin = spec["xmin"] + c0 * RES_M
    ymax = spec["ymax"] - r0 * RES_M
    write_referenced_tif(raster_path, np.asarray(grid), xmin=xmin, ymax=ymax, res=RES_M)
    if isinstance(grid, np.memmap):
        grid.flush()
        del grid
        gc.collect()
        try:
            grid_path.unlink()
        except OSError:
            pass
    counts = defaultdict(int)
    for row in station_rows:
        counts[row["status"]] += 1
    bridge_m = sum(float(row["span_m"] or 0.0) for row in bridge_rows)
    print(
        f"stations {len(station_rows)} kept {counts['kept']} culvert {counts['culvert']} "
        f"bridge {counts['bridge']} open_end {counts['open_end']} structure {counts['structure']}",
        flush=True,
    )
    pass_prev = sum(1 for chain in chains for station in chain["stations"] if station.get("pass15"))
    pass_now = sum(1 for chain in chains for station in chain["stations"] if station.get("pass20"))
    added_kept = sum(
        1
        for chain in chains
        for station in chain["stations"]
        if station.get("pass20") and not station.get("pass15") and station.get("ok")
    )
    print(
        f"slope rule: axial+{FLAT_ADD_PREV:.0f} passes {pass_prev}, "
        f"axial+{FLAT_ADD:.0f} passes {pass_now}, added {pass_now - pass_prev}, "
        f"of which {added_kept} still have ground height",
        flush=True,
    )
    print(
        f"surface parts {len(surface_rows)} junctions {len(junction_rows)} "
        f"unmarked bridges {len(bridge_rows)} ({bridge_m:.0f} m) "
        f"known structures {len(structure_rows)}",
        flush=True,
    )
    print(
        "(open the following in qgis or similar tool to check: "
        f"{dest}, {raster_path})",
        flush=True,
    )


if __name__ == "__main__":
    main()
