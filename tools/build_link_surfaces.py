"""One carriageway polygon per GIP-link chain, cut by every higher rank.

The chain follows the GIP links. At a node with three or more links of one
class, the straightest pair stays one chain and keeps its kink. The other
arms end on that chain after the terrain fit. A lower class takes that point.
Width is the link's carriageway width. A lower polygon is cut on the outline
of every higher polygon.
An overlap of the same rank is written as a junction patch. Both polygons
stay.

Does not write a BeamNG level.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\build_link_surfaces.py
"""
from __future__ import annotations

import argparse
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
from build_ranked_carriageway import _smooth_line  # noqa: E402
from build_road_grid import _ensure_raw_mosaic, _load_geotiff  # noqa: E402
from gip_routing_load import gpkg_path, load_routing  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

# Short label for the functional class. Rank is the class number itself.
# Sammelstraße keeps its own name: it is not a path and it is not innerörtlich.
CODE = {
    0: "S-A",
    1: "S-B",
    2: "zentraloertlich",
    3: "regional",
    4: "S-L",
    5: "S-G",
    6: "Sammelstrasse",
    7: "S-GW",
}
WAY_FROM = 8
JUNCTION_MIN_M2 = 1.0
SLIVER_M2 = 1.0
# Distance used only to read the direction of an arm. The line itself stays.
BEARING_M = 15.0
# A side-arm extension farther than this from the node is ignored.
HIT_MAX_M = 40.0
SURFACE_NAMES = (
    "surface.gpkg",
    "surface_next.gpkg",
    "surface_junc.gpkg",
    "surface_curve.gpkg",
    "surface_meet.gpkg",
    "surface_width.gpkg",
)


def _code(fc: int) -> str:
    if fc >= WAY_FROM:
        return "Weg"
    return CODE.get(fc, f"fc{fc}")


def _cum(pts: list[tuple[float, float]]) -> list[float]:
    out = [0.0]
    for a, b in zip(pts, pts[1:]):
        out.append(out[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    return out


def _ribbon(pts: list[tuple[float, float]], widths: list[float]):
    dist = _cum(pts)
    if not dist or dist[-1] < 1.0 or len(pts) < 2:
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
        return [part for part in geom.geoms if part.area >= SLIVER_M2]
    if geom.geom_type == "GeometryCollection":
        out = []
        for part in geom.geoms:
            out.extend(_parts(part))
        return out
    return []


def _xy(line: LineString) -> list[tuple[float, float]]:
    return [(float(x), float(y)) for x, y, *_rest in line.coords]


def _width_at(dist: list[float], widths: list[float], distance: float) -> float:
    if distance <= dist[0]:
        return widths[0]
    if distance >= dist[-1]:
        return widths[-1]
    for i in range(1, len(dist)):
        if dist[i] >= distance:
            span = dist[i] - dist[i - 1]
            if span < 1e-6:
                return widths[i]
            t = (distance - dist[i - 1]) / span
            return widths[i - 1] + (widths[i] - widths[i - 1]) * t
    return widths[-1]


def _width_profile(pts, widths) -> tuple[float, str]:
    """Dominant width, and the station where the width changes along the line."""
    dist = _cum(pts)
    bins: dict[float, float] = defaultdict(float)
    runs: list[tuple[float, float]] = [(0.0, round(float(widths[0]), 2))]
    for i in range(1, len(widths)):
        width = round(float(widths[i]), 2)
        bins[width] += dist[i] - dist[i - 1]
        if abs(width - runs[-1][1]) >= 0.05:
            runs.append((round(dist[i], 1), width))
    dominant = max(bins, key=bins.get) if bins else runs[0][1]
    along = ",".join(f"{station:.1f}:{width:.2f}" for station, width in runs)
    return dominant, along


def _profile_from_links(short_ids: str, line_length: float, by_short: dict[int, tuple[float, float]]):
    parts = []
    for part in str(short_ids).split(","):
        if not part:
            continue
        found = by_short.get(int(part))
        if found is None:
            continue
        parts.append(found)
    if not parts:
        raise SystemExit(f"no width for {short_ids}")
    total = sum(length for _width, length in parts)
    scale = line_length / total if total > 1e-6 else 1.0
    bins: dict[float, float] = defaultdict(float)
    runs: list[tuple[float, float]] = []
    walked = 0.0
    for width, length in parts:
        width = round(float(width), 2)
        bins[width] += length
        if not runs or abs(width - runs[-1][1]) >= 0.05:
            runs.append((round(walked * scale, 1), width))
        walked += length
    dominant = max(bins, key=bins.get)
    along = ",".join(f"{station:.1f}:{width:.2f}" for station, width in runs)
    return dominant, along


def _fit_to_ground(pts, widths, elev, spec):
    """Lateral fit of the chain onto the raw terrain. The link line stays if the fit fails."""
    line = LineString(pts)
    dist = _cum(pts)
    if dist[-1] < 1.0:
        return pts, widths, False
    # The fit uses one width. Take the width that covers the most length.
    bins: dict[float, float] = defaultdict(float)
    for i in range(1, len(dist)):
        bins[round(widths[i], 2)] += dist[i] - dist[i - 1]
    class_width = max(bins, key=bins.get)
    shifted = _smooth_line(line, elev, spec, class_width)
    if not shifted or len(shifted) < 2:
        return pts, widths, False
    sd = _cum(shifted)
    scale = dist[-1] / sd[-1] if sd[-1] > 1e-6 else 1.0
    fitted_w = [_width_at(dist, widths, s * scale) for s in sd]
    return shifted, fitted_w, True


def _dev(b1: float, b2: float) -> float:
    d = abs(b1 - b2) % 360.0
    d = min(d, 360.0 - d)
    return abs(180.0 - d)


def _bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360.0


def _at_node_end(coords: list[tuple[float, float]], node_xy: tuple[float, float]) -> bool:
    d0 = math.hypot(coords[0][0] - node_xy[0], coords[0][1] - node_xy[1])
    d1 = math.hypot(coords[-1][0] - node_xy[0], coords[-1][1] - node_xy[1])
    return d0 <= d1


def _backoff(coords: list[tuple[float, float]], at_start: bool, dist: float):
    """Point `dist` metres from the node, and the coordinates from there to the far end."""
    seq = list(coords) if at_start else list(reversed(coords))
    walked = 0.0
    pt = seq[-1]
    rest = [seq[-1]]
    for i in range(1, len(seq)):
        step = math.hypot(seq[i][0] - seq[i - 1][0], seq[i][1] - seq[i - 1][1])
        if step < 1e-9:
            continue
        if walked + step >= dist or i == len(seq) - 1:
            take = min(max(dist - walked, 0.0), step)
            t = take / step
            pt = (
                seq[i - 1][0] + t * (seq[i][0] - seq[i - 1][0]),
                seq[i - 1][1] + t * (seq[i][1] - seq[i - 1][1]),
            )
            rest = seq[i:] if t > 0.999 else [pt] + seq[i:]
            break
        walked += step
    if not at_start:
        rest = list(reversed(rest))
    return pt, rest


def _cross(p1, p2, p3, p4):
    den = (p1[0] - p2[0]) * (p3[1] - p4[1]) - (p1[1] - p2[1]) * (p3[0] - p4[0])
    if abs(den) < 1e-9:
        return None
    t = ((p1[0] - p3[0]) * (p3[1] - p4[1]) - (p1[1] - p3[1]) * (p3[0] - p4[0])) / den
    return (p1[0] + t * (p2[0] - p1[0]), p1[1] + t * (p2[1] - p1[1]))


def _smooth_curve(pts, joints, passes: int = 8, weight: float = 0.55):
    """Pull each interior vertex toward its neighbours. Junction points stay."""
    if len(pts) < 3:
        return pts
    pinned = [
        any(math.hypot(p[0] - j[0], p[1] - j[1]) < 0.25 for j in joints) for p in pts
    ]
    cur = list(pts)
    for _ in range(passes):
        nxt = [cur[0]]
        for i in range(1, len(cur) - 1):
            if pinned[i]:
                nxt.append(pts[i])
                continue
            mid = ((cur[i - 1][0] + cur[i + 1][0]) * 0.5, (cur[i - 1][1] + cur[i + 1][1]) * 0.5)
            nxt.append(
                (
                    cur[i][0] * (1.0 - weight) + mid[0] * weight,
                    cur[i][1] * (1.0 - weight) + mid[1] * weight,
                )
            )
        nxt.append(cur[-1])
        cur = nxt
    return cur


def _on_segment(a, b, p, eps: float = 0.15) -> bool:
    span = math.hypot(b[0] - a[0], b[1] - a[1])
    return abs(math.hypot(p[0] - a[0], p[1] - a[1]) + math.hypot(b[0] - p[0], b[1] - p[1]) - span) <= eps


def _point_at(pts, dist: float):
    walked = 0.0
    for i in range(1, len(pts)):
        step = math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
        if step < 1e-9:
            continue
        if walked + step >= dist or i == len(pts) - 1:
            t = min(1.0, max(0.0, (dist - walked) / step))
            return (
                pts[i - 1][0] + t * (pts[i][0] - pts[i - 1][0]),
                pts[i - 1][1] + t * (pts[i][1] - pts[i - 1][1]),
            )
        walked += step
    return pts[-1]


def _ray_hits(origin, aim, line, node_xy):
    """Where the line through origin and aim meets `line`, close to the node."""
    best = None
    walked = 0.0
    for i in range(1, len(line)):
        a, b = line[i - 1], line[i]
        step = math.hypot(b[0] - a[0], b[1] - a[1])
        hit = _cross(origin, aim, a, b)
        if hit is not None and _on_segment(a, b, hit):
            along = walked + math.hypot(hit[0] - a[0], hit[1] - a[1])
            away = math.hypot(hit[0] - node_xy[0], hit[1] - node_xy[1])
            if away <= HIT_MAX_M and (best is None or away < best[0]):
                best = (away, along, hit)
        walked += step
    return best


def _plan_junctions(lines, ends, by_fc, nodes):
    """Pick the through pair. Do not change its line."""
    forced: dict[int, dict[str, tuple[str, str]]] = defaultdict(dict)
    plans: dict[int, list[dict]] = defaultdict(list)
    claimed: set[str] = set()
    for fc in sorted(by_fc):
        at: dict[str, list[str]] = defaultdict(list)
        for lid in by_fc[fc]:
            a, b = ends[lid]
            at[a].append(lid)
            at[b].append(lid)
        for nid, lids in at.items():
            if len(lids) < 3 or nid in claimed:
                continue
            nxy = (float(nodes[nid]["x"]), float(nodes[nid]["y"]))
            arms = []
            for lid in lids:
                coords = lines[lid]
                length = _cum(coords)[-1] if len(coords) > 1 else 0.0
                dist = min(BEARING_M, max(4.0, length * 0.6))
                pt, _rest = _backoff(coords, _at_node_end(coords, nxy), dist)
                arms.append({"lid": lid, "bear": _bearing(nxy, pt)})
            best = None
            for i in range(len(arms)):
                for k in range(i + 1, len(arms)):
                    dev = _dev(arms[i]["bear"], arms[k]["bear"])
                    if best is None or dev < best[0]:
                        best = (dev, i, k)
            _dev_deg, i, k = best
            through = (arms[i]["lid"], arms[k]["lid"])
            sides = [arm["lid"] for n, arm in enumerate(arms) if n not in (i, k)]
            forced[fc][nid] = through
            plans[fc].append(
                {"node": nid, "nxy": nxy, "through": through, "sides": sides, "dev": _dev_deg}
            )
            claimed.add(nid)
    return forced, plans


def _chains(ids: list[str], ends: dict[str, tuple[str, str]], forced: dict[str, tuple[str, str]] | None = None) -> list[list[str]]:
    """Links joined where the class has two links, or through the straight pair of a junction."""
    forced = forced or {}
    at: dict[str, list[str]] = defaultdict(list)
    for lid in ids:
        a, b = ends[lid]
        at[a].append(lid)
        at[b].append(lid)
    partner: dict[tuple[str, str], str] = {}
    idset = set(ids)
    for node, (a, b) in forced.items():
        if a in idset and b in idset:
            partner[(node, a)] = b
            partner[(node, b)] = a
    cont = {node for node, lids in at.items() if len(lids) == 2 or any((node, lid) in partner for lid in lids)}
    used: set[str] = set()
    out: list[list[str]] = []

    def walk(start: str, node: str) -> list[str]:
        order = [start]
        prev = node
        cur = start
        while True:
            other = ends[cur][1] if ends[cur][0] == prev else ends[cur][0]
            if other not in cont:
                break
            nxt = partner.get((other, cur))
            if nxt is None and len(at[other]) == 2:
                nxt = at[other][0] if at[other][1] == cur else at[other][1]
            if nxt is None or nxt in used or nxt in order:
                break
            order.append(nxt)
            prev, cur = other, nxt
        return order

    for lid in ids:
        if lid in used:
            continue
        a, b = ends[lid]
        left = walk(lid, b) if a in cont else [lid]
        left = list(reversed(left))
        right = walk(lid, a) if b in cont else []
        order = left + right[1:]
        used.update(order)
        out.append(order)
    return out


def main() -> None:
    site = load_site()
    crs = resolved_working_crs(site)
    topo = load_routing(site)
    links = topo["links"]
    geoms = {row.link_id: row.geometry for row in gpd.read_file(gpkg_path(site), layer="link").itertuples(index=False)}
    proc = processed_dir(site)
    _ensure_raw_mosaic(site, proc)
    elev, spec = _load_geotiff(proc / "corridor50_raw" / "corridor50_raw.tif")

    ends: dict[str, tuple[str, str]] = {}
    lines: dict[str, list[tuple[float, float]]] = {}
    by_fc: dict[int, list[str]] = defaultdict(list)
    for lid, rec in links.items():
        line = geoms.get(lid)
        if line is None or line.is_empty:
            continue
        ends[lid] = (rec["node_from"], rec["node_to"])
        lines[lid] = _xy(line)
        by_fc[int(rec["fc"])].append(lid)

    forced, plans = _plan_junctions(lines, ends, by_fc, topo["nodes"])
    print(f"junctions planned {sum(len(v) for v in plans.values())}", flush=True)
    reports: list[tuple] = []
    node_j: dict[str, tuple[int, tuple[float, float]]] = {}

    axis_rows = []
    fit_rows = []
    raw_by_rank: dict[int, list[tuple[str, Polygon, dict]]] = defaultdict(list)
    n_fit = 0
    n_chain = 0
    for fc in sorted(by_fc):
        ids = by_fc[fc]
        code = _code(fc)
        have = [lid for lid in ids if links[lid].get("width_m")]
        for lid in ids:
            rec = links[lid]
            axis_rows.append(
                {
                    "link_id": lid,
                    "short_id": rec["short_id"],
                    "code": code,
                    "fc": fc,
                    "fc_name": rec["fc_name"],
                    "edge_cat": rec["edge_category"],
                    "width_m": rec["width_m"],
                    "name": rec["name"],
                    "street_code": rec["street_code"],
                    "geometry": LineString(lines[lid]),
                }
            )
        ready = []
        for order in _chains(have, ends, forced.get(fc, {})):
            pts: list[tuple[float, float]] = []
            ws: list[float] = []
            prev = None
            start_node = None
            for lid in order:
                coords = list(lines[lid])
                a, b = ends[lid]
                if prev is None:
                    if len(order) > 1:
                        na, nb = ends[order[1]]
                        if a in (na, nb):
                            coords = list(reversed(coords))
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
                    coords = list(reversed(coords))
                    prev = a
                width = float(links[lid]["width_m"])
                if pts and math.hypot(pts[-1][0] - coords[0][0], pts[-1][1] - coords[0][1]) < 0.05:
                    if abs(ws[-1] - width) >= 0.05:
                        pts.append(pts[-1])
                        ws.append(width)
                    coords = coords[1:]
                pts.extend(coords)
                ws.extend([width] * len(coords))
            if len(pts) != len(ws) or len(pts) < 2 or start_node is None:
                continue
            n_chain += 1
            pts, ws, fitted = _fit_to_ground(pts, ws, elev, spec)
            if fitted:
                n_fit += 1
            pts = _smooth_curve(pts, [])
            if n_chain % 25 == 0:
                print(f"  chains {n_chain}, fitted {n_fit}", flush=True)
            ready.append(
                {
                    "order": order,
                    "pts": pts,
                    "ws": ws,
                    "fitted": fitted,
                    "start_node": start_node,
                    "end_node": prev,
                }
            )
        for plan in plans.get(fc, []):
            through = next(
                (c for c in ready if plan["through"][0] in c["order"] and plan["through"][1] in c["order"]),
                None,
            )
            if through is None:
                continue
            alongs = []
            sides = []
            for sid in plan["sides"]:
                side = next((c for c in ready if sid in c["order"]), None)
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
                if side["start_node"] == plan["node"]:
                    side["pts"][0] = junction
                elif side["end_node"] == plan["node"]:
                    side["pts"][-1] = junction
            la, lb = plan["through"]
            reports.append((fc, plan["node"], junction, la, lb, plan["dev"], len(plan["sides"])))
            pair = {links[la]["short_id"], links[lb]["short_id"]}
            if pair & {1113816, 1392991, 1111050} or {1113816, 1392991, 1111050} & {
                links[s]["short_id"] for s in plan["sides"]
            }:
                print(
                    f"  through {links[la]['short_id']}-{links[lb]['short_id']} "
                    f"off straight {plan['dev']:.1f} deg, side arms {len(plan['sides'])}, "
                    f"junction {junction[0]:.2f} {junction[1]:.2f}",
                    flush=True,
                )
        for chain in ready:
            pts = chain["pts"]
            for node, idx in ((chain["start_node"], 0), (chain["end_node"], -1)):
                owned = node_j.get(node)
                if owned is not None and owned[0] < fc:
                    pts[idx] = owned[1]
            poly = _ribbon(pts, chain["ws"])
            if poly is None:
                continue
            order = chain["order"]
            rec0 = links[order[0]]
            short_ids = ",".join(str(links[lid]["short_id"]) for lid in order)
            width_m, width_along = _width_profile(pts, chain["ws"])
            fit_rows.append(
                {
                    "code": code,
                    "fc": fc,
                    "fitted": int(chain["fitted"]),
                    "width_m": width_m,
                    "width_along": width_along,
                    "short_ids": short_ids,
                    "geometry": LineString(pts),
                }
            )
            raw_by_rank[fc].append(
                (
                    short_ids,
                    poly,
                    {
                        "code": code,
                        "fc": fc,
                        "fc_name": rec0["fc_name"],
                        "n_links": len(order),
                        "width_m": width_m,
                        "width_along": width_along,
                        "fitted": int(chain["fitted"]),
                        "short_ids": short_ids,
                    },
                )
            )

    surface_rows = []
    higher: list[Polygon] = []
    higher_union = None
    for fc in sorted(raw_by_rank):
        for short_ids, poly, props in raw_by_rank[fc]:
            geom = poly
            if higher_union is not None and not higher_union.is_empty:
                geom = poly.difference(higher_union)
            for part in _parts(geom):
                if part.area < SLIVER_M2:
                    continue
                surface_rows.append({**props, "area_m2": round(part.area, 1), "geometry": part})
        higher.extend(poly for _sid, poly, _props in raw_by_rank[fc])
        higher_union = unary_union(higher)

    junction_rows = []
    for fc, items in raw_by_rank.items():
        geoms = [poly for _sid, poly, _props in items]
        if len(geoms) < 2:
            continue
        tree = shapely.STRtree(geoms)
        pairs = tree.query(geoms, predicate="intersects")
        seen = set()
        for i, j in pairs.T:
            a, b = int(i), int(j)
            if a >= b:
                continue
            key = (a, b)
            if key in seen:
                continue
            seen.add(key)
            inter = geoms[a].intersection(geoms[b])
            if inter.is_empty or inter.area < JUNCTION_MIN_M2:
                continue
            pa, pb = items[a][2], items[b][2]
            for part in _parts(inter):
                if part.area < JUNCTION_MIN_M2:
                    continue
                junction_rows.append(
                    {
                        "code": pa["code"],
                        "fc": fc,
                        "a": pa["short_ids"],
                        "b": pb["short_ids"],
                        "area_m2": round(part.area, 1),
                        "geometry": part,
                    }
                )

    out_dir = processed_dir(site) / "link_surface"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "surface.gpkg"
    for name in SURFACE_NAMES:
        dest = out_dir / name
        if not dest.exists():
            break
        try:
            dest.unlink()
            break
        except PermissionError:
            print(f"{name} is open, trying the next file", flush=True)
    point_rows = [
        {
            "fc": fc,
            "off_straight_deg": round(dev, 1),
            "n_side": n_side,
            "through": f"{links[la]['short_id']},{links[lb]['short_id']}",
            "geometry": Point(j),
        }
        for fc, _nid, j, la, lb, dev, n_side in reports
    ]
    gpd.GeoDataFrame(axis_rows, crs=crs).to_file(dest, layer="axis", driver="GPKG")
    if fit_rows:
        gpd.GeoDataFrame(fit_rows, crs=crs).to_file(dest, layer="axis_fit", driver="GPKG")
    if surface_rows:
        gpd.GeoDataFrame(surface_rows, crs=crs).to_file(dest, layer="surface", driver="GPKG")
    if junction_rows:
        gpd.GeoDataFrame(junction_rows, crs=crs).to_file(dest, layer="junction", driver="GPKG")
    if point_rows:
        gpd.GeoDataFrame(point_rows, crs=crs).to_file(dest, layer="junction_point", driver="GPKG")
    print(
        f"links {len(axis_rows)} chains {n_chain} fitted {n_fit} "
        f"surfaces {len(surface_rows)} junctions {len(junction_rows)}",
        flush=True,
    )
    print(
        "(open the following in qgis or similar tool to check: "
        f"{dest})",
        flush=True,
    )


def _latest_surface(out_dir: Path) -> Path:
    found = [out_dir / name for name in SURFACE_NAMES if (out_dir / name).is_file()]
    if not found:
        raise SystemExit(f"no surface file in {out_dir}")
    return found[-1]


def stamp_width() -> None:
    """Write each chain's carriageway width onto the fitted axis. No new fit."""
    site = load_site()
    out_dir = processed_dir(site) / "link_surface"
    src = _latest_surface(out_dir)
    layers: dict[str, gpd.GeoDataFrame] = {}
    for layer in ("axis", "axis_fit", "surface", "junction", "junction_point"):
        try:
            layers[layer] = gpd.read_file(src, layer=layer)
        except Exception:
            continue
    fit = layers["axis_fit"]
    link_rows = gpd.read_file(gpkg_path(site), layer="link")
    by_short: dict[int, tuple[float, float]] = {}
    for rec in link_rows.itertuples(index=False):
        if rec.width_m is None or rec.geometry is None:
            continue
        width = float(rec.width_m)
        if not math.isfinite(width):
            continue
        flat = LineString([(c[0], c[1]) for c in rec.geometry.coords])
        by_short[int(rec.short_id)] = (width, float(flat.length))
    dominant = []
    along = []
    for rec in fit.itertuples(index=False):
        width_m, width_along = _profile_from_links(str(rec.short_ids), float(rec.geometry.length), by_short)
        dominant.append(width_m)
        along.append(width_along)
    fit = fit.copy()
    fit["width_m"] = dominant
    fit["width_along"] = along
    layers["axis_fit"] = fit
    if "surface" in layers:
        by_chain = dict(zip(fit["short_ids"].astype(str), dominant))
        surface = layers["surface"].copy()
        surface["width_m"] = [by_chain.get(str(sid), width) for sid, width in zip(surface["short_ids"], surface["width_m"])]
        along_of = dict(zip(fit["short_ids"].astype(str), along))
        surface["width_along"] = [along_of.get(str(sid)) for sid in surface["short_ids"]]
        layers["surface"] = surface
    tmp = out_dir / "_surface_width_tmp.gpkg"
    if tmp.exists():
        tmp.unlink()
    for layer, frame in layers.items():
        frame.to_file(tmp, layer=layer, driver="GPKG")
    dest = src
    try:
        src.unlink()
        tmp.replace(dest)
    except PermissionError:
        print(f"{src.name} is open, trying the next file", flush=True)
        dest = out_dir / "surface_width.gpkg"
        if dest.exists():
            try:
                dest.unlink()
            except PermissionError as exc:
                raise SystemExit(f"{dest.name} is open as well") from exc
        tmp.replace(dest)
    print(f"width on {len(fit)} axes", flush=True)
    print(f"(open the following in qgis or similar tool to check: {dest})", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--stamp-width", action="store_true", help="store carriageway width on the fitted axis")
    if ap.parse_args().stamp_width:
        stamp_width()
    else:
        main()
