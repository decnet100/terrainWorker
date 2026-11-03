"""Measure the plan gap and the height step on the junction/road cut."""
import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
from shapely import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_road_grid as bg

PROC = Path("data/processed/tirol-imst-tarrenz-8192")
gdf = gpd.read_file(PROC / "road_surface_smooth" / "carriageway_smooth.gpkg", layer="carriageway")
junc = gdf[gdf["mesh_kind"] == "junction"]
links = gdf[gdf["mesh_kind"] == "link"]
link_geoms = list(links.geometry)
tree = STRtree(link_geoms)

# Points on the junction outline that lie inside the original carriageway are
# the cut. The new kerb arc lies outside that carriageway and is not a seam.
pre = gpd.read_file(
    PROC / "road_surface_smooth" / "carriageway_smooth.pre_corner.gpkg", layer="carriageway"
)
pre = pre[(pre["mesh_kind"] == "road") & (pre["mesh_layer"].astype(int) == 0)]
pre_geoms = list(pre.geometry)
pre_tree = STRtree(pre_geoms)
bins = np.zeros(5, dtype=np.int64)
uncovered = []
for poly in junc.geometry:
    length = float(poly.exterior.length)
    n = max(12, int(length))
    road_pts = []
    for i in range(n):
        pt = poly.exterior.interpolate(i / n * length)
        hits = np.atleast_1d(pre_tree.query(pt.buffer(0.05)))
        if len(hits) == 0:
            continue
        if min(float(pre_geoms[int(h)].distance(pt)) for h in hits) > 0.02:
            continue
        road_pts.append(pt)
    if len(road_pts) < 4:
        continue
    dists = []
    for pt in road_pts:
        hits = np.atleast_1d(tree.query(pt.buffer(3.0)))
        if len(hits) == 0:
            dists.append(3.0)
            continue
        dists.append(min(float(link_geoms[int(h)].distance(pt)) for h in hits))
    arr = np.asarray(dists)
    bins[0] += int(np.count_nonzero(arr <= 0.01))
    bins[1] += int(np.count_nonzero((arr > 0.01) & (arr <= 0.05)))
    bins[2] += int(np.count_nonzero((arr > 0.05) & (arr <= 0.20)))
    bins[3] += int(np.count_nonzero((arr > 0.20) & (arr <= 0.50)))
    bins[4] += int(np.count_nonzero(arr > 0.50))
    near_pre = [pre_geoms[int(h)] for h in np.atleast_1d(pre_tree.query(poly.buffer(1.0)))]
    near_link = [link_geoms[int(h)] for h in np.atleast_1d(tree.query(poly.buffer(1.0)))]
    if near_pre and near_link:
        original = shapely.union_all(near_pre).intersection(poly.buffer(1.0))
        covered = shapely.union_all([poly, *near_link]).intersection(original)
        missing = original.difference(covered)
        uncovered.append(float(missing.area))

print(
    "cut points  <=1 cm {0}  1-5 cm {1}  5-20 cm {2}  20-50 cm {3}  >50 cm {4}".format(*bins)
)
print(
    f"original road missing inside the junction neighbourhood: "
    f"median {np.median(uncovered):.2f} m2  sum {np.sum(uncovered):.0f} m2  "
    f"junctions {len(uncovered)}"
)

index = json.loads((PROC / "corridor50_raw" / "corridor50_raw_index.json").read_text(encoding="utf-8"))
z, _ = bg._load_geotiff(PROC / "road_surface_smooth" / "04_layer0.tif")
if z.ndim == 3:
    z = z[..., 0]
frame = {k: float(index[k]) for k in ("xmin", "ymin", "xmax", "ymax")}

# Height step at the cut, before and after the per-mesh edge treatment.
# A handful of junctions is enough: the treatment is the same at every cut.
rng = np.random.default_rng(0)
order = rng.choice(len(junc), size=min(12, len(junc)), replace=False)
steps_raw = []
steps_smooth = []
for ji in order:
    row = junc.iloc[int(ji)]
    poly = row.geometry
    hits = np.atleast_1d(tree.query(poly))
    by_key: dict[int, list] = {}
    for h in hits:
        part = links.iloc[int(h)]
        if float(part.geometry.distance(poly)) > 0.05:
            continue
        by_key.setdefault(int(part.mesh_key), []).append(part.geometry)
    if not by_key:
        continue
    jgrid, jraw = None, None
    window = bg._crop_window(z, frame, [poly])
    r0, r1, c0, c1, sub = window
    jgrid = dict(sub)
    jgrid["z"] = np.array(z[r0:r1, c0:c1], dtype=np.float32, copy=True)
    jgrid["polys"] = [poly]
    jgrid["tree"] = STRtree([poly])
    jgrid["mask"] = bg._clip_mask(jgrid["z"], jgrid, [poly], jgrid["tree"])
    j_before = jgrid["z"].copy()

    def sample(grid, x, y):
        return bg._sample_z(grid, x, y)

    for parts in list(by_key.values())[:3]:
        window = bg._crop_window(z, frame, parts)
        r0, r1, c0, c1, sub = window
        lgrid = dict(sub)
        lgrid["z"] = np.array(z[r0:r1, c0:c1], dtype=np.float32, copy=True)
        lgrid["polys"] = parts
        lgrid["tree"] = STRtree(parts)
        lgrid["mask"] = bg._clip_mask(lgrid["z"], lgrid, parts, lgrid["tree"])
        cut = poly.boundary.intersection(shapely.union_all(parts).buffer(0.02))
        if cut.is_empty:
            continue
        length = float(cut.length)
        pts = [cut.interpolate(i / 8 * length) for i in range(9)]
        for pt in pts:
            a = sample(jgrid, pt.x, pt.y)
            b = sample(lgrid, pt.x, pt.y)
            if np.isfinite(a) and np.isfinite(b):
                steps_raw.append(abs(a - b))
        bg._smooth_edge_heights(jgrid)
        bg._smooth_edge_heights(lgrid)
        for pt in pts:
            a = sample(jgrid, pt.x, pt.y)
            b = sample(lgrid, pt.x, pt.y)
            if np.isfinite(a) and np.isfinite(b):
                steps_smooth.append(abs(a - b))
        jgrid["z"] = j_before.copy()

def _stats(name, vals):
    if not vals:
        print(name, "none")
        return
    arr = np.asarray(vals)
    print(
        f"{name} n={arr.size} median {np.median(arr)*100:.1f} cm  "
        f"p90 {np.percentile(arr, 90)*100:.1f} cm  max {arr.max()*100:.1f} cm  "
        f">5 cm {int(np.count_nonzero(arr > 0.05))}  >20 cm {int(np.count_nonzero(arr > 0.20))}"
    )

_stats("height step raw", steps_raw)
_stats("height step after edge", steps_smooth)
