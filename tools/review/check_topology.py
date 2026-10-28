"""Topology check of a road line layer: noding at junctions, dangles, crossings, overlaps.

    python -I check_topology.py FILE [LAYER] [--crs EPSG] [--filter FIELD=V1,V2]
Prints counts; writes FILE_stem_topo.gpkg with the problem points next to FILE.
"""
import sys
from collections import Counter
from pathlib import Path
import geopandas as gpd
import numpy as np
import shapely
from shapely.strtree import STRtree

args = sys.argv[1:]
path = Path(args[0])
layer = args[1] if len(args) > 1 and not args[1].startswith("--") else None
crs = int(args[args.index("--crs") + 1]) if "--crs" in args else None
flt = args[args.index("--filter") + 1] if "--filter" in args else None
g = gpd.read_file(path, layer=layer)
if crs:
    g = g.to_crs(crs)
if flt:
    k, v = flt.split("=")
    g = g[g[k].astype(str).isin(v.split(","))]
g = g[~g.geometry.is_empty].reset_index(drop=True)
lines = [shapely.line_merge(geom) if geom.geom_type == "MultiLineString" else geom for geom in g.geometry]
multi = sum(1 for l in lines if l.geom_type != "LineString")
lines = [max(l.geoms, key=lambda x: x.length) if l.geom_type != "LineString" else l for l in lines]
print(f"{path.name}: {len(lines)} lines, {multi} still multi after merge, total {sum(l.length for l in lines)/1000:.1f} km")

TOL = 0.05
ends = np.array([c for l in lines for c in (l.coords[0][:2], l.coords[-1][:2])])
owner = np.repeat(np.arange(len(lines)), 2)
from scipy.spatial import cKDTree
tree = cKDTree(ends)
deg = np.array([len(tree.query_ball_point(p, TOL)) for p in ends])
print("endpoint degree (incl. itself):", dict(sorted(Counter(deg).items())))
near = np.array([len(tree.query_ball_point(p, 1.0)) for p in ends])
print("endpoints with another endpoint within 1 m but not within 5 cm:", int(((near > deg)).sum()))

str_tree = STRtree(lines)
tjunc, problems = 0, []
for i, p in enumerate(ends):
    if deg[i] > 1:
        continue
    pt = shapely.Point(p)
    for j in str_tree.query(pt.buffer(0.5)):
        if j == owner[i]:
            continue
        d = lines[j].distance(pt)
        if d < 0.5:
            tjunc += 1
            problems.append((pt, "T-junction not noded" if d < TOL else f"gap {d:.2f} m"))
            break
print("dangling ends touching another line's interior (not noded) or within 0.5 m:", tjunc)

cross = 0
for i, l in enumerate(lines):
    for j in str_tree.query(l):
        if j <= i:
            continue
        inter = l.intersection(lines[j])
        if inter.is_empty:
            continue
        pts = [inter] if inter.geom_type == "Point" else [x for x in getattr(inter, "geoms", []) if x.geom_type == "Point"]
        for q in pts:
            if min(q.distance(shapely.Point(e)) for e in (l.coords[0], l.coords[-1], lines[j].coords[0], lines[j].coords[-1])) > TOL:
                cross += 1
                problems.append((q, "interior crossing (bridge/tunnel or missing node)"))
        if inter.geom_type in ("LineString", "MultiLineString") or (hasattr(inter, "geoms") and any(x.geom_type == "LineString" for x in inter.geoms)):
            problems.append((inter.representative_point(), "overlapping lines"))
print("interior crossings:", cross, "| overlapping pairs:", sum(1 for _, k in problems if k == "overlapping lines"))
seg = np.concatenate([np.hypot(*np.diff(np.array(l.coords)[:, :2], axis=0).T) for l in lines])
print(f"vertex spacing m: median {np.median(seg):.1f}, p90 {np.percentile(seg,90):.1f}, max {seg.max():.0f}")
has_z = sum(1 for l in lines if l.has_z)
print("lines with Z:", has_z)
if problems:
    out = path.with_name(path.stem + "_topo.gpkg")
    gpd.GeoDataFrame({"kind": [k for _, k in problems]}, geometry=[p for p, _ in problems], crs=g.crs).to_file(out)
    print("wrote", out.name, Counter(k.split(' ')[0] for _, k in problems))
