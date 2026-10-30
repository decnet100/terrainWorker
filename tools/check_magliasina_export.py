"""Check the Magliasina export (export_magliasina.py) the way Magliasina's network.py will read it.

Topology (network.nodes_of: line ends closer than SNAP = 0.6 m are one node; only ends are noded; tunnels and
galleries are left out as network.SKIP does):
* near misses: two line ends 0.6-3 m apart (they will not meet);
* T-junctions: a line end within 0.6 m of another line's inside, not at one of its ends (not noded);
* crossings without a node, with the KUNSTBAUTE / STUFE of both lines (fine for a bridge or tunnel);
* dead ends away from the area edge.
Width and axis (network.measure_width, every 2 m on roads that are not bridges, tunnels or galleries):
* the station must lie inside a strada_sentiero polygon;
* its width w along the normal must sit in 0.5 x nominal < w < max(2.2 x nominal, 6) and the offset of the
  axis from the middle under MAX_SHIFT = 1.5 m, else Magliasina falls back to the nominal class width.
Polygons: overlaps between LCSF polygons, invalid ones.

Writes magliasina/check.gpkg (problem points) and magliasina/check.json; prints the summary.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\check_magliasina_export.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from site_coords import load_site, processed_dir  # noqa: E402

SNAP = 0.6
NEAR = 3.0
STEP = 2.0
MAX_SHIFT = 1.5
EDGE_M = 5.0
# Magliasina network.CLASSES nominal widths, roads only
NOMINAL = {"10m Strasse": 10.0, "8m Strasse": 8.0, "6m Strasse": 6.0, "4m Strasse": 4.5, "3m Strasse": 3.2}
NO_WIDTH = {"Bruecke", "Tunnel", "Galerie", "Bruecke mit Galerie", "Steg"}


def main() -> None:
    site = load_site()
    out = processed_dir(site) / "magliasina"
    crs = site.get("crs") or "EPSG:25832"
    box = shapely.box(*map(float, site["bbox"]))
    d = json.loads((out / "tlm" / "TLM_STRASSE.json").read_text(encoding="utf-8"))
    lines, props, tubes = [], [], []
    for f in d["features"]:
        if f["props"].get("KUNSTBAUTE") in ("Tunnel", "Galerie"):
            tubes += [LineString([(c[0], c[1]) for c in part]) for part in f["parts"] if len(part) >= 2]
        for part in f["parts"]:
            if len(part) >= 2 and f["props"].get("KUNSTBAUTE") not in ("Tunnel", "Galerie"):   # network.SKIP
                lines.append(LineString([(c[0], c[1]) for c in part]))
                props.append(f["props"])
    lcsf = gpd.read_file(out / "av" / "LCSF.geojson")
    road = shapely.union_all(list(lcsf[lcsf["Genere"] == "strada_sentiero"].geometry))
    shapely.prepare(road)
    problems = []

    # --- topology ---------------------------------------------------------------------------------
    ends = np.array([c for l in lines for c in (l.coords[0], l.coords[-1])])
    owner = np.repeat(np.arange(len(lines)), 2)
    tree = cKDTree(ends)
    portal = shapely.union_all(tubes).buffer(SNAP) if tubes else shapely.Polygon()
    shapely.prepare(portal)
    near_miss = 0
    degree = np.zeros(len(ends), int)
    for i, p in enumerate(ends):
        close = tree.query_ball_point(p, NEAR)
        snapped = [j for j in close if j != i and np.hypot(*(ends[j] - p)) <= SNAP]
        degree[i] = len(snapped)
        if not snapped and not shapely.contains_xy(portal, p[0], p[1]) and any(j != i and owner[j] != owner[i] for j in close):
            near_miss += 1
            problems.append(("near_miss", Point(p), props[owner[i]]["UUID"]))
    str_tree = shapely.STRtree(lines)
    tjunc = 0
    for i, p in enumerate(ends):
        if degree[i]:
            continue
        pt = Point(p)
        for j in str_tree.query(pt.buffer(SNAP)):
            if j == owner[i] or lines[j].distance(pt) > SNAP:
                continue
            tjunc += 1
            problems.append(("t_junction", pt, props[owner[i]]["UUID"]))
            break
    crossings = Counter()
    for i, l in enumerate(lines):
        for j in str_tree.query(l):
            if j <= i:
                continue
            x = l.intersection(lines[j])
            for q in getattr(x, "geoms", [x]):
                if q.is_empty or q.geom_type != "Point":
                    continue
                if min(np.hypot(*(ends[k] - (q.x, q.y))) for k in (2 * i, 2 * i + 1, 2 * j, 2 * j + 1)) <= SNAP:
                    continue
                kinds = tuple(sorted((props[i]["KUNSTBAUTE"], props[j]["KUNSTBAUTE"])))
                crossings[kinds] += 1
                if kinds == ("Keine", "Keine"):
                    problems.append(("crossing_same_level", q, f"{props[i]['UUID']} x {props[j]['UUID']}"))
    dead = portals = 0
    for i, p in enumerate(ends):
        if degree[i] == 0 and not any(lines[j].distance(Point(p)) <= SNAP for j in str_tree.query(Point(p).buffer(SNAP))
                                      if j != owner[i]):
            if shapely.contains_xy(portal, p[0], p[1]):
                portals += 1                 # the road goes on in a tunnel or gallery Magliasina does not build
            elif box.exterior.distance(Point(p)) > EDGE_M:
                dead += 1
                problems.append(("dead_end", Point(p), props[owner[i]]["UUID"]))

    # --- axis and width ---------------------------------------------------------------------------
    stats = {}
    for l, pr in zip(lines, props):
        cls = pr["OBJEKTART"]
        if cls not in NOMINAL or pr["KUNSTBAUTE"] in NO_WIDTH:
            continue
        nom = NOMINAL[cls]
        key = f"{pr['SRC']}:{cls}"
        s = stats.setdefault(key, Counter())
        for v in np.arange(STEP / 2, l.length, STEP):
            p = l.interpolate(v)
            a = l.interpolate(max(0.0, v - 1.0))
            b = l.interpolate(min(l.length, v + 1.0))
            t = np.array([b.x - a.x, b.y - a.y])
            t /= max(np.linalg.norm(t), 1e-9)
            n = np.array([-t[1], t[0]])
            s["stations"] += 1
            if not shapely.contains_xy(road, p.x, p.y):
                s["outside"] += 1
                problems.append(("axis_outside", p, pr["UUID"]))
                continue
            tt = np.arange(0.0, 15.0, 0.1)
            left = shapely.contains_xy(road, p.x + n[0] * tt, p.y + n[1] * tt)
            right = shapely.contains_xy(road, p.x - n[0] * tt, p.y - n[1] * tt)
            dl = tt[np.argmin(left)] if not left.all() else 15.0
            dr = tt[np.argmin(right)] if not right.all() else 15.0
            w, off = dl + dr, (dl - dr) / 2.0
            if not (0.5 * nom < w < max(2.2 * nom, 6.0)):
                s["width_out"] += 1
                problems.append(("width_out", p, f"{pr['UUID']} w={w:.1f}"))
            elif abs(off) >= MAX_SHIFT:
                s["offset_out"] += 1
                problems.append(("offset_out", p, f"{pr['UUID']} off={off:.1f}"))
            else:
                s["measured"] += 1

    # --- polygons ---------------------------------------------------------------------------------
    geoms = list(lcsf.geometry)
    invalid = int(sum(not g.is_valid for g in geoms))
    ptree = shapely.STRtree(geoms)
    overlap = 0.0
    for i, g in enumerate(geoms):
        for j in ptree.query(g):
            if j > i:
                overlap += g.intersection(geoms[j]).area

    report = {"lines": len(lines), "near_miss_ends": near_miss, "t_junctions_not_noded": tjunc,
              "crossings_without_node": {" x ".join(k): v for k, v in crossings.items()},
              "dead_ends_inside": dead, "ends_at_tunnel_portals": portals, "polygons": len(geoms), "invalid_polygons": invalid,
              "polygon_overlap_m2": round(overlap, 1),
              "width": {k: {**v, "measured_share": round(v["measured"] / max(v["stations"], 1), 3)} for k, v in sorted(stats.items())}}
    (out / "check.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    gpd.GeoDataFrame([{"kind": k, "ref": r, "geometry": g} for k, g, r in problems], crs=crs).to_file(out / "check.gpkg")
    print(json.dumps(report, indent=1, ensure_ascii=False))
    print(f"(open the following in qgis or similar tool to check: {out / 'check.gpkg'})")


if __name__ == "__main__":
    main()
