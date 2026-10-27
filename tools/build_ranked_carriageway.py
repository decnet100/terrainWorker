"""Carriageway ribbons in rank order, then an overlap check.

Each road is a buffer of variable width along its centerline. Mesh-class
roads use the smoothed lateral fit from ``sample_cross_profile``. Every
other class keeps the GIP axis. Width at a station is the class width, so
the ribbon changes where two pieces of one road meet.

Pieces of the same rank and the same road code are one axis when they
continue through a node within 30 degrees of straight. A lower rank then
takes the node positions those axes already have. An overlap with a higher
or equal ribbon is a collision, except inside the mouth at a shared node.

Does not write a BeamNG level.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\build_ranked_carriageway.py
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import shapely
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from authorities import resolved_working_crs  # noqa: E402
from build_bridges import find_gip_geojson  # noqa: E402
from build_road_grid import _ensure_raw_mosaic, _load_geotiff  # noqa: E402
from gip_road_segments import _gip_segment_width_m  # noqa: E402
from sample_cross_profile import (  # noqa: E402
    MIN_SAMPLES,
    MIN_WIDTH_M,
    STATION_M,
    WIDTH_DELTAS_M,
    _choose,
    _cubic_rmse,
    _line_of,
    _normal,
    _offsets,
    _ribbon_samples,
    _runs,
    _smooth_offsets,
)
from shift_centerline_by_edge import _STRUCTURE_OBJEKT, _load_segments  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

# Lower number is placed first and defines the node for every later rank.
RANK = {
    "S-A": 0,
    "S-AT": 0,
    "S-AR": 1,
    "S-AP": 1,
    "S-B": 2,
    "S-BT": 2,
    "S-BR": 3,
    "S-BP": 3,
    "S-L": 4,
    "S-LT": 4,
    "S-G": 5,
    "S-GB": 5,
    "S-GW": 6,
    "S-F": 7,
    "S-FRW": 8,
    "S-STRAIL": 9,
}
CHAIN_KINK_DEG = 30.0
BLEND_M = 20.0
END_MATCH_M = 3.0
COLLIDE_MIN_M2 = 1.0
FOCUS = {3021, 4486, 4594, 4344, 2856, 219339, 2576, 4487, 9970}


def _rank(objekt: str) -> int:
    return RANK.get(objekt, 50)


def _ang(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def _bearing(x0: float, y0: float, x1: float, y1: float) -> float:
    return math.degrees(math.atan2(x1 - x0, y1 - y0)) % 360.0


def _cum(pts: list[tuple[float, float]]) -> list[float]:
    out = [0.0]
    for a, b in zip(pts, pts[1:]):
        out.append(out[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    return out


def _length(pts: list[tuple[float, float]]) -> float:
    dist = _cum(pts)
    return dist[-1] if dist else 0.0


def _apply_pulls(
    pts: list[tuple[float, float]],
    pulls: list[tuple[int, tuple[float, float]]],
) -> list[tuple[float, float]]:
    """Move the ends toward given points. Overlapping pulls share the point."""
    if not pulls:
        return pts
    dist = _cum(pts)
    total = dist[-1]
    out: list[tuple[float, float]] = []
    for i, (x, y) in enumerate(pts):
        acc_x = 0.0
        acc_y = 0.0
        weight = 0.0
        for which, target in pulls:
            along = dist[i] if which == 0 else total - dist[i]
            w = 0.0 if BLEND_M <= 0 else max(0.0, 1.0 - along / BLEND_M)
            acc_x += w * target[0]
            acc_y += w * target[1]
            weight += w
        if weight <= 1e-9:
            out.append((x, y))
        elif weight <= 1.0:
            out.append(((1.0 - weight) * x + acc_x, (1.0 - weight) * y + acc_y))
        else:
            out.append((acc_x / weight, acc_y / weight))
    return out


def _smooth_line(line: LineString, elev, spec, class_width: float) -> list[tuple[float, float]] | None:
    """Smoothed centerline. None when the DGM has no usable cross-section."""
    widths = []
    for delta in WIDTH_DELTAS_M:
        width = round(class_width + delta, 2)
        if width >= MIN_WIDTH_M - 1e-6:
            widths.append(width)
    if line.length < 1.0 or not widths:
        return None
    count = int(math.floor(line.length / STATION_M))
    stations = [min(line.length, i * STATION_M) for i in range(count + 1)]
    placed: list[dict] = []
    class_rmse: list[dict[float, float]] = []
    offsets = _offsets()
    class_width = round(class_width, 2)
    for distance in stations:
        point = line.interpolate(distance)
        nx, ny = _normal(line, distance)
        if nx == 0.0 and ny == 0.0:
            continue
        here: list[dict] = []
        at_class: dict[float, float] = {}
        for offset in offsets:
            cx = point.x + offset * nx
            cy = point.y + offset * ny
            for width in widths:
                lateral, height = _ribbon_samples(elev, spec, cx, cy, nx, ny, width / 2.0)
                if lateral.size < MIN_SAMPLES:
                    continue
                if lateral[-1] - lateral[0] < width * 0.8:
                    continue
                rmse = round(_cubic_rmse(lateral, height), 4)
                here.append({"offset_m": offset, "width_m": width, "rmse_m": rmse})
                if abs(width - class_width) < 1e-6:
                    at_class[offset] = rmse
        pick = _choose(here)
        if pick is None:
            continue
        placed.append({"x": point.x, "y": point.y, "nx": nx, "ny": ny, "offset_m": pick["offset_m"]})
        class_rmse.append(at_class)
    if len(placed) < 2:
        return None
    smooth = _smooth_offsets([row["offset_m"] for row in placed], class_rmse)
    return [
        (row["x"] + offset * row["nx"], row["y"] + offset * row["ny"])
        for row, offset in zip(placed, smooth)
    ]


def _leave_bearing(pts: list[tuple[float, float]], at_end: str) -> float:
    if len(pts) < 2:
        return 0.0
    if at_end == "start":
        a, b = pts[0], pts[min(len(pts) - 1, 2)]
    else:
        a, b = pts[-1], pts[max(0, len(pts) - 3)]
    return _bearing(a[0], a[1], b[0], b[1])


def _ribbon(pts: list[tuple[float, float]], widths: list[float]):
    dist = _cum(pts)
    if dist[-1] < 1.0:
        return None
    dense_xy: list[tuple[float, float]] = [pts[0]]
    dense_w: list[float] = [widths[0]]
    for i in range(1, len(pts)):
        span = dist[i] - dist[i - 1]
        n = max(1, int(math.ceil(span / 2.0))) if span > 1e-6 else 1
        for k in range(1, n + 1):
            t = k / n
            dense_xy.append(
                (
                    pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * t,
                    pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * t,
                )
            )
            dense_w.append(widths[i - 1] + (widths[i] - widths[i - 1]) * t)
    left: list[tuple[float, float]] = []
    right: list[tuple[float, float]] = []
    for i, ((x, y), width) in enumerate(zip(dense_xy, dense_w)):
        if i == 0:
            ax, ay, bx, by = dense_xy[0][0], dense_xy[0][1], dense_xy[1][0], dense_xy[1][1]
        elif i == len(dense_xy) - 1:
            ax, ay = dense_xy[-2]
            bx, by = dense_xy[-1]
        else:
            ax, ay = dense_xy[i - 1]
            bx, by = dense_xy[i + 1]
        dx, dy = bx - ax, by - ay
        span = math.hypot(dx, dy)
        if span < 1e-6:
            continue
        nx, ny = -dy / span, dx / span
        half = width / 2.0
        left.append((x + nx * half, y + ny * half))
        right.append((x - nx * half, y - ny * half))
    if len(left) < 2 or len(right) < 2:
        return None
    poly = Polygon(left + right[::-1])
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return None
    return poly


def _parts(geom) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return [part for part in geom.geoms if not part.is_empty]
    if geom.geom_type == "GeometryCollection":
        out = []
        for part in geom.geoms:
            out.extend(_parts(part))
        return out
    return []


def main() -> None:
    site = load_site()
    crs = resolved_working_crs(site)
    proc = processed_dir(site)
    vw = gpd.read_file(find_gip_geojson(site))
    if vw.crs is None:
        vw = vw.set_crs(crs)
    elif str(vw.crs) != crs:
        vw = vw.to_crs(crs)
    routing = json.loads((proc / "gip_routing" / "gip_routing.json").read_text(encoding="utf-8"))
    roads_topo = routing["roads"]
    nodes = routing["nodes"]
    segs = _load_segments(site)
    by_oid: dict[int, list] = defaultdict(list)
    for seg in segs:
        by_oid[seg.oid].append(seg)
    for group in by_oid.values():
        group.sort(key=lambda seg: seg.seq)

    _ensure_raw_mosaic(site, proc)
    raw = proc / "corridor50_raw" / "corridor50_raw.tif"
    elev = spec = None
    if raw.is_file():
        elev, spec = _load_geotiff(raw)
        print(f"smoothed axis from {raw.name}", flush=True)
    else:
        print("raw DGM missing, every axis stays on the GIP line", flush=True)

    roads: dict[int, dict] = {}
    for rec in vw.itertuples(index=False):
        try:
            oid = int(rec.OBJECTID)
        except (TypeError, ValueError):
            continue
        objekt = str(getattr(rec, "OBJEKT", "") or "").upper().strip()
        if objekt in _STRUCTURE_OBJEKT:
            continue
        geom = rec.geometry
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type == "MultiLineString":
            geom = max(geom.geoms, key=lambda part: part.length)
        if geom.geom_type != "LineString" or geom.length < 1.0:
            continue
        code = getattr(rec, "STR_CODE", "")
        if code is None or (isinstance(code, float) and math.isnan(code)):
            code = ""
        else:
            code = str(code).strip()
            if code.lower() == "nan":
                code = ""
        props = {"OBJEKT": objekt, "STR_CODE": code}
        group = by_oid.get(oid)
        if group and all(seg.structure for seg in group):
            continue
        if group:
            width = float(group[0].width_m)
        else:
            width = float(_gip_segment_width_m(props, 5.0))
        if width < 1.5:
            continue
        pts: list[tuple[float, float]] | None = None
        source = "gip"
        if group and elev is not None and not all(seg.structure for seg in group):
            runs = _runs(group)
            if len(runs) == 1:
                fitted = _smooth_line(_line_of(runs[0]), elev, spec, width)
                if fitted:
                    pts = fitted
                    source = "smooth"
            elif len(runs) > 1:
                joined: list[tuple[float, float]] = []
                for run in runs:
                    fitted = _smooth_line(_line_of(run), elev, spec, width)
                    if fitted:
                        if joined:
                            fitted = fitted[1:]
                        joined.extend(fitted)
                if len(joined) >= 2:
                    pts = joined
                    source = "smooth"
        if pts is None:
            pts = [(float(x), float(y)) for x, y in geom.coords]
            source = "gip"
        ends: dict[str, str] = {}
        info = roads_topo.get(str(oid)) or {}
        for nid in info.get("nodes") or []:
            node = nodes.get(nid)
            if not node:
                continue
            da = math.hypot(pts[0][0] - node["x"], pts[0][1] - node["y"])
            db = math.hypot(pts[-1][0] - node["x"], pts[-1][1] - node["y"])
            if min(da, db) > END_MATCH_M:
                continue
            side = "start" if da <= db else "end"
            prev = ends.get(nid)
            if prev is None:
                ends[nid] = side
        roads[oid] = {
            "oid": oid,
            "objekt": objekt,
            "str_code": str(props["STR_CODE"] or "").strip(),
            "name": str(getattr(rec, "STRNAME", "") or ""),
            "rank": _rank(objekt),
            "width": width,
            "source": source,
            "pts": pts,
            "ends": ends,
        }
        if len(roads) % 40 == 0:
            print(f"axes {len(roads)}", flush=True)
    print(
        f"axes {len(roads)}  smooth {sum(1 for r in roads.values() if r['source'] == 'smooth')}  "
        f"gip {sum(1 for r in roads.values() if r['source'] == 'gip')}",
        flush=True,
    )

    by_node: dict[str, list[int]] = defaultdict(list)
    for oid, road in roads.items():
        for nid in road["ends"]:
            by_node[nid].append(oid)

    parent = {oid: oid for oid in roads}

    def find(oid: int) -> int:
        while parent[oid] != oid:
            parent[oid] = parent[parent[oid]]
            oid = parent[oid]
        return oid

    link: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for nid, members in by_node.items():
        for i, left in enumerate(members):
            a = roads[left]
            for right in members[i + 1 :]:
                b = roads[right]
                if a["rank"] != b["rank"] or not a["str_code"] or a["str_code"] != b["str_code"]:
                    continue
                if nid not in a["ends"] or nid not in b["ends"]:
                    continue
                kink = abs(
                    _ang(_leave_bearing(a["pts"], a["ends"][nid]), _leave_bearing(b["pts"], b["ends"][nid]))
                    - 180.0
                )
                if kink > CHAIN_KINK_DEG:
                    continue
                link[left].append((right, nid))
                link[right].append((left, nid))
                parent[find(left)] = find(right)

    groups: dict[int, list[int]] = defaultdict(list)
    for oid in roads:
        groups[find(oid)].append(oid)
    for members in groups.values():
        deg = {oid: len(link[oid]) for oid in members}
        if any(n > 2 for n in deg.values()):
            for oid in members:
                link[oid] = []

    chains = []
    seen: set[int] = set()
    for oid, road in roads.items():
        if oid in seen:
            continue
        members = groups[find(oid)]
        if len(members) == 1 or not any(link[m] for m in members):
            seen.add(oid)
            chains.append([oid])
            continue
        start = next((m for m in members if len(link[m]) <= 1), members[0])
        order = [start]
        prev = None
        cursor = start
        while True:
            nxt = [other for other, _nid in link[cursor] if other != prev and other not in order]
            if not nxt:
                break
            prev, cursor = cursor, nxt[0]
            order.append(cursor)
        seen.update(order)
        chains.append(order)

    def orient(oid: int, node_at_end: str | None, node_at_start: str | None) -> list[tuple[float, float]]:
        road = roads[oid]
        pts = list(road["pts"])
        if node_at_start and road["ends"].get(node_at_start) == "end":
            pts.reverse()
        elif node_at_end and road["ends"].get(node_at_end) == "start":
            pts.reverse()
        return pts

    built = []
    for order in chains:
        if len(order) == 1:
            road = roads[order[0]]
            built.append(
                {
                    "oids": order,
                    "pts": list(road["pts"]),
                    "widths": [road["width"]] * len(road["pts"]),
                    "rank": road["rank"],
                    "objekt": road["objekt"],
                    "str_code": road["str_code"],
                    "source": road["source"],
                    "width": road["width"],
                "nodes": set(road["ends"]),
                "node_xy": {},
                "node_half": {},
                "end_start": next((nid for nid, side in road["ends"].items() if side == "start"), None),
                "end_end": next((nid for nid, side in road["ends"].items() if side == "end"), None),
            }
        )
            continue
        pieces: list[tuple[int, list[tuple[float, float]], str | None, str | None]] = []
        for i, oid in enumerate(order):
            prev_node = None
            next_node = None
            for other, nid in link[oid]:
                if i > 0 and other == order[i - 1]:
                    prev_node = nid
                if i + 1 < len(order) and other == order[i + 1]:
                    next_node = nid
            if i == 0:
                outer = [nid for nid in roads[oid]["ends"] if nid != next_node]
                prev_node = outer[0] if outer else None
            if i + 1 == len(order):
                outer = [nid for nid in roads[oid]["ends"] if nid != prev_node]
                next_node = outer[0] if outer else None
            pts = orient(oid, next_node, prev_node)
            pieces.append((oid, pts, prev_node, next_node))
        for i in range(len(pieces) - 1):
            _oid_a, pts_a, _sa, _ea = pieces[i]
            _oid_b, pts_b, _sb, _eb = pieces[i + 1]
            mid = ((pts_a[-1][0] + pts_b[0][0]) / 2.0, (pts_a[-1][1] + pts_b[0][1]) / 2.0)
            pts_a = _apply_pulls(pts_a, [(-1, mid)])
            pts_b = _apply_pulls(pts_b, [(0, mid)])
            pieces[i] = (_oid_a, pts_a, _sa, _ea)
            pieces[i + 1] = (_oid_b, pts_b, _sb, _eb)
        pts: list[tuple[float, float]] = []
        widths: list[float] = []
        for oid, piece, _s, _e in pieces:
            use = piece[1:] if pts else piece
            pts.extend(use)
            widths.extend([roads[oid]["width"]] * len(use))
        node_xy = {}
        node_half = {}
        for oid, piece, n0, n1 in pieces:
            if n0:
                node_xy[n0] = piece[0]
                node_half[n0] = roads[oid]["width"] / 2.0
            if n1:
                node_xy[n1] = piece[-1]
                node_half[n1] = roads[oid]["width"] / 2.0
        first = roads[order[0]]
        built.append(
            {
                "oids": order,
                "pts": pts,
                "widths": widths,
                "rank": first["rank"],
                "objekt": first["objekt"],
                "str_code": first["str_code"],
                "source": "smooth" if all(roads[i]["source"] == "smooth" for i in order) else first["source"],
                "width": first["width"],
                "nodes": set(node_xy),
                "node_xy": node_xy,
                "node_half": node_half,
                "end_start": pieces[0][2],
                "end_end": pieces[-1][3],
            }
        )

    for chain in built:
        if chain["node_xy"]:
            continue
        road = roads[chain["oids"][0]]
        chain["node_xy"] = {
            nid: (chain["pts"][0] if side == "start" else chain["pts"][-1])
            for nid, side in road["ends"].items()
        }
        chain["node_half"] = {nid: road["width"] / 2.0 for nid in road["ends"]}
        chain["nodes"] = set(road["ends"])

    # Single-road orient left the node_xy pointing at the unreversed ends. Good.

    targets: dict[str, tuple[float, float]] = {}
    placed: list[dict] = []
    collisions: list[dict] = []

    def _hit(new: dict, old: dict, inter) -> None:
        shared = new["nodes"] & old["nodes"]
        geom = inter
        if shared:
            discs = []
            for nid in shared:
                half = new["node_half"].get(nid, new["width"] / 2.0) + old["node_half"].get(
                    nid, old["width"] / 2.0
                )
                for center in (new["node_xy"].get(nid), old["node_xy"].get(nid)):
                    if center is not None:
                        discs.append(Point(center).buffer(half))
            if discs:
                geom = inter.difference(unary_union(discs))
        for part in _parts(geom):
            area = float(part.area)
            if area < COLLIDE_MIN_M2:
                continue
            collisions.append(
                {
                    "geom": part,
                    "objectids": ",".join(str(i) for i in new["oids"]),
                    "other_objectids": ",".join(str(i) for i in old["oids"]),
                    "rank": new["rank"],
                    "other_rank": old["rank"],
                    "area_m2": round(area, 2),
                    "str_code": new["str_code"],
                    "other_str_code": old["str_code"],
                }
            )

    ranks = sorted({c["rank"] for c in built})
    for rank in ranks:
        batch = [c for c in built if c["rank"] == rank]
        for chain in batch:
            pts = chain["pts"]
            pulls: list[tuple[int, tuple[float, float]]] = []
            if chain.get("end_start") in targets:
                pulls.append((0, targets[chain["end_start"]]))
            if chain.get("end_end") in targets:
                pulls.append((-1, targets[chain["end_end"]]))
            if pulls:
                chain["pts"] = _apply_pulls(pts, pulls)
                if chain.get("end_start"):
                    chain["node_xy"][chain["end_start"]] = chain["pts"][0]
                if chain.get("end_end"):
                    chain["node_xy"][chain["end_end"]] = chain["pts"][-1]
            widths = chain["widths"]
            if len(widths) != len(chain["pts"]):
                widths = [chain["width"]] * len(chain["pts"])
            chain["poly"] = _ribbon(chain["pts"], widths)
        usable = [c for c in batch if c.get("poly") is not None]
        if placed and usable:
            old_geoms = [c["poly"] for c in placed]
            old_tree = shapely.STRtree(old_geoms)
            for chain in usable:
                for idx in old_tree.query(chain["poly"], predicate="intersects"):
                    inter = chain["poly"].intersection(placed[int(idx)]["poly"])
                    _hit(chain, placed[int(idx)], inter)
        if len(usable) > 1:
            geoms = [c["poly"] for c in usable]
            tree = shapely.STRtree(geoms)
            for i, chain in enumerate(usable):
                for idx in tree.query(chain["poly"], predicate="intersects"):
                    j = int(idx)
                    if j <= i:
                        continue
                    inter = chain["poly"].intersection(usable[j]["poly"])
                    _hit(chain, usable[j], inter)
        fresh: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for chain in usable:
            placed.append(chain)
            for nid, xy in chain["node_xy"].items():
                if nid not in targets:
                    fresh[nid].append(xy)
        for nid, xys in fresh.items():
            targets[nid] = (
                sum(xy[0] for xy in xys) / len(xys),
                sum(xy[1] for xy in xys) / len(xys),
            )
        print(f"rank {rank}: {len(usable)} ribbons, collisions so far {len(collisions)}", flush=True)

    out_dir = proc / "ranked_carriageway"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "carriageway.gpkg"
    if dest.exists():
        try:
            dest.unlink()
        except PermissionError:
            dest = out_dir / "carriageway_next.gpkg"
            print(f"carriageway.gpkg is open, writing {dest.name}", flush=True)
            if dest.exists():
                dest.unlink()

    axis_rows = []
    axis_geoms = []
    poly_rows = []
    poly_geoms = []
    for chain in placed:
        axis_geoms.append(LineString(chain["pts"]))
        axis_rows.append(
            {
                "objectids": ",".join(str(i) for i in chain["oids"]),
                "objekt": chain["objekt"],
                "str_code": chain["str_code"],
                "rank": chain["rank"],
                "width_m": chain["width"],
                "source": chain["source"],
                "length_m": round(_length(chain["pts"]), 1),
            }
        )
        for part in _parts(chain["poly"]):
            poly_geoms.append(part)
            poly_rows.append(axis_rows[-1])
    node_rows = []
    node_geoms = []
    for nid, xy in targets.items():
        node_geoms.append(Point(xy))
        node_rows.append({"node_id": nid, "x": round(xy[0], 2), "y": round(xy[1], 2)})
    col_geoms = [row["geom"] for row in collisions]
    col_rows = [{k: v for k, v in row.items() if k != "geom"} for row in collisions]
    first = True
    for name, geoms, rows in (
        ("axis", axis_geoms, axis_rows),
        ("carriageway", poly_geoms, poly_rows),
        ("node", node_geoms, node_rows),
        ("collision", col_geoms, col_rows),
    ):
        if not geoms:
            print(f"layer {name} empty", flush=True)
            continue
        gpd.GeoDataFrame(rows, geometry=geoms, crs=crs).to_file(
            dest, layer=name, driver="GPKG", mode="w" if first else "a"
        )
        first = False
        print(f"layer {name}: {len(geoms)}", flush=True)
    focus_hits = [
        row
        for row in collisions
        if FOCUS.intersection(int(part) for part in row["objectids"].split(","))
        or FOCUS.intersection(int(part) for part in row["other_objectids"].split(","))
    ]
    focus_hits.sort(key=lambda row: -row["area_m2"])
    print(f"focus collisions {len(focus_hits)}", flush=True)
    for row in focus_hits[:20]:
        print(
            f"  {row['objectids']} ({row['str_code'] or '-'}) x {row['other_objectids']} "
            f"({row['other_str_code'] or '-'}) {row['area_m2']:.1f} m2",
            flush=True,
        )
    print(f"(open the following in qgis or similar tool to check: {dest})", flush=True)


if __name__ == "__main__":
    main()
