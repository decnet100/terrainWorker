"""Measure gaps and height steps on the built road DAE parts.

Upward faces only (driving surface). Open edges are matched to other parts.
A cut that shares XY but not Z is a height step. An unmatched edge far from
the clip outline is a hole. Hairlines between clip polygons are measured
separately.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\check_road_mesh.py
"""
from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from site_coords import SiteCoords, load_site, processed_dir

XY_SEAL_M = 0.01
Z_SEAL_M = 0.01
NEAR_M = 0.25
HOLE_INSIDE_M = 1.25


def _parse_dae(path: Path) -> tuple[np.ndarray, np.ndarray]:
    text = path.read_text(encoding="utf-8")
    pos_m = re.search(
        r'id="[^"]+_a999-mesh-positions-array"[^>]*>([^<]+)',
        text,
    )
    tri_m = re.search(
        r'<geometry id="[^"]+_a999-mesh".*?<triangles[^>]*count="(\d+)"[^>]*>.*?<p>([^<]+)</p>',
        text,
        re.S,
    )
    if pos_m is None or tri_m is None:
        raise SystemExit(f"visual mesh missing in {path.name}")
    pos = np.fromstring(pos_m.group(1), sep=" ", dtype=np.float64)
    if pos.size % 3:
        raise SystemExit(f"odd position count in {path.name}")
    pos = pos.reshape(-1, 3)
    idx = np.fromstring(tri_m.group(2), sep=" ", dtype=np.int32)
    n_tri = int(tri_m.group(1))
    if idx.size != n_tri * 3:
        raise SystemExit(f"index count {idx.size} != {n_tri}*3 in {path.name}")
    return pos, idx.reshape(-1, 3)


def _up_faces(pos: np.ndarray, faces: np.ndarray) -> np.ndarray:
    a = pos[faces[:, 0]]
    b = pos[faces[:, 1]]
    c = pos[faces[:, 2]]
    cross_z = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (
        c[:, 0] - a[:, 0]
    )
    return faces[cross_z > 1.0e-8]


def _boundary_edges(pos: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Open edges as (x0,y0,z0,x1,y1,z1), endpoints ordered in XY."""
    counts: dict[tuple[int, int], int] = {}
    for tri in faces:
        i, j, k = int(tri[0]), int(tri[1]), int(tri[2])
        for u, v in ((i, j), (j, k), (k, i)):
            key = (u, v) if u < v else (v, u)
            counts[key] = counts.get(key, 0) + 1
    rows = []
    for (u, v), n in counts.items():
        if n != 1:
            continue
        p = pos[u]
        q = pos[v]
        if (p[0], p[1]) <= (q[0], q[1]):
            rows.append((p[0], p[1], p[2], q[0], q[1], q[2]))
        else:
            rows.append((q[0], q[1], q[2], p[0], p[1], p[2]))
    if not rows:
        return np.zeros((0, 6), dtype=np.float64)
    return np.asarray(rows, dtype=np.float64)


def _kind_of(name: str) -> str:
    if name.startswith("over"):
        return "overpass"
    if name.startswith("under"):
        return "underpass"
    if name.startswith("span"):
        return "span"
    return "road"


def _cell(x: float, y: float, step: float) -> tuple[int, int]:
    return int(math.floor(x / step)), int(math.floor(y / step))


def _pct(arr: np.ndarray, p: float) -> float:
    if arr.size == 0:
        return float("nan")
    return float(np.percentile(arr, p))


def _poly_hairlines(proc: Path) -> dict:
    import geopandas as gpd
    import shapely
    from shapely.ops import unary_union

    gpkg = proc / "road_surface_smooth" / "carriageway_smooth.gpkg"
    gdf = gpd.read_file(gpkg, layer="carriageway")
    at = gdf[gdf["mesh_kind"].isin(["road", "link", "junction"])]
    union = unary_union([g for g in at.geometry if g is not None and not g.is_empty])
    out = {"components": 0, "close_2cm_m2": 0.0, "close_5cm_m2": 0.0}
    if union.is_empty:
        return out
    parts = list(union.geoms) if union.geom_type == "MultiPolygon" else [union]
    out["components"] = len(parts)
    for r, key in ((0.02, "close_2cm_m2"), (0.05, "close_5cm_m2")):
        closed = shapely.make_valid(union.buffer(r).buffer(-r))
        filled = shapely.make_valid(closed.difference(union))
        out[key] = round(float(filled.area), 3)
    return out


def main() -> None:
    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    out_dir = proc / "road_grid"
    index = json.loads((out_dir / "road_grid_index.json").read_text(encoding="utf-8"))
    tiles = index.get("tiles") or []
    print(f"parts {len(tiles)} verts {index.get('verts')} tris {index.get('tris')}", flush=True)

    edges_by: list[tuple[str, str, np.ndarray]] = []
    n_up = 0
    for item in tiles:
        name = str(item["name"])
        path = out_dir / f"{name}.dae"
        if not path.is_file():
            print(f"missing {path.name}", flush=True)
            continue
        pos, faces = _parse_dae(path)
        up = _up_faces(pos, faces)
        n_up += int(up.shape[0])
        edges = _boundary_edges(pos, up)
        edges_by.append((name, _kind_of(name), edges))
        print(f"  {name} up_tris={up.shape[0]} open_edges={edges.shape[0]}", flush=True)

    all_e = []
    owners = []
    kinds = []
    for name, kind, edges in edges_by:
        if edges.size == 0:
            continue
        all_e.append(edges)
        owners.extend([name] * int(edges.shape[0]))
        kinds.extend([kind] * int(edges.shape[0]))
    if not all_e:
        raise SystemExit("no boundary edges")
    e = np.vstack(all_e)
    owners = np.asarray(owners)
    kinds = np.asarray(kinds)
    mx = 0.5 * (e[:, 0] + e[:, 3])
    my = 0.5 * (e[:, 1] + e[:, 4])
    mz = 0.5 * (e[:, 2] + e[:, 5])
    n = int(e.shape[0])
    print(f"upward tris {n_up}, open edges {n}", flush=True)

    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i in range(n):
        buckets[_cell(mx[i], my[i], NEAR_M)].append(i)

    sealed = 0
    z_off = []
    xy_gap = []
    unmatched = []
    for i in range(n):
        best_j = -1
        best_d = 1.0e9
        best_dz = 0.0
        cx0, cy0 = _cell(mx[i], my[i], NEAR_M)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in buckets.get((cx0 + dx, cy0 + dy), ()):
                    if j == i or owners[j] == owners[i]:
                        continue
                    d = math.hypot(mx[i] - mx[j], my[i] - my[j])
                    if d < best_d:
                        best_d = d
                        best_j = j
                        best_dz = abs(float(mz[i] - mz[j]))
        if best_j < 0 or best_d > NEAR_M:
            unmatched.append(i)
            continue
        pair = (owners[i], owners[best_j], kinds[i], kinds[best_j])
        if best_d <= XY_SEAL_M:
            if best_dz <= Z_SEAL_M:
                sealed += 1
            else:
                z_off.append((best_dz, best_d, *pair, float(mx[i]), float(my[i])))
        else:
            xy_gap.append((best_d, best_dz, *pair, float(mx[i]), float(my[i])))

    print(
        f"matched same XY +/-{XY_SEAL_M*100:.0f} mm, |dz|<={Z_SEAL_M*100:.0f} mm: {sealed}",
        flush=True,
    )
    print(f"same XY, height step >={Z_SEAL_M*100:.0f} mm: {len(z_off)}", flush=True)
    print(
        f"other-part edge {XY_SEAL_M*100:.0f}-{NEAR_M*100:.0f} mm away: {len(xy_gap)}",
        flush=True,
    )
    print(f"no other-part edge within {NEAR_M*100:.0f} mm: {len(unmatched)}", flush=True)

    def _pair_key(a: str, b: str) -> str:
        return " / ".join(sorted((a, b)))

    if z_off:
        dz = np.array([row[0] for row in z_off])
        print(
            f"  height step cm: p50 {np.median(dz)*100:.1f}  p90 {_pct(dz,90)*100:.1f}  "
            f"p99 {_pct(dz,99)*100:.1f}  max {dz.max()*100:.1f}  n>5cm {int((dz>0.05).sum())}  "
            f"n>20cm {int((dz>0.20).sum())}",
            flush=True,
        )
        by = Counter(_pair_key(row[4], row[5]) for row in z_off)
        print("  height steps by mesh kind:", dict(by), flush=True)
        same = [row for row in z_off if row[4] == "road" and row[5] == "road"]
        print(f"  road-road height steps: {len(same)}", flush=True)
        if same:
            d2 = np.array([row[0] for row in same])
            print(
                f"    cm p50 {np.median(d2)*100:.1f}  p90 {_pct(d2,90)*100:.1f}  "
                f"max {d2.max()*100:.1f}",
                flush=True,
            )
        worst = sorted(z_off, key=lambda r: -r[0])[:8]
        for row in worst:
            x, y = sc.terrain_to_crs(row[6], row[7])
            print(
                f"    dz {row[0]*100:.1f} cm  {row[2]} / {row[3]}  CRS {x:.1f},{y:.1f}",
                flush=True,
            )
    if xy_gap:
        dxy = np.array([row[0] for row in xy_gap])
        dz = np.array([row[1] for row in xy_gap])
        print(
            f"  plan gap cm: p50 {np.median(dxy)*100:.1f}  p90 {_pct(dxy,90)*100:.1f}  "
            f"max {dxy.max()*100:.1f}  with |dz|>5cm {int((dz>0.05).sum())}",
            flush=True,
        )
        by = Counter(_pair_key(row[4], row[5]) for row in xy_gap)
        print("  plan gaps by mesh kind:", dict(by), flush=True)
        same = [row for row in xy_gap if row[4] == "road" and row[5] == "road"]
        print(f"  road-road plan gaps: {len(same)}", flush=True)
        if same:
            d2 = np.array([row[0] for row in same])
            z2 = np.array([row[1] for row in same])
            print(
                f"    cm p50 {np.median(d2)*100:.1f}  p90 {_pct(d2,90)*100:.1f}  "
                f"max {d2.max()*100:.1f}  |dz|>1cm {int((z2>0.01).sum())}  "
                f"|dz|>5cm {int((z2>0.05).sum())}",
                flush=True,
            )
        worst = sorted(xy_gap, key=lambda r: -r[0])[:8]
        for row in worst:
            x, y = sc.terrain_to_crs(row[6], row[7])
            print(
                f"    xy {row[0]*100:.1f} cm dz {row[1]*100:.1f} cm  "
                f"{row[2]} / {row[3]}  CRS {x:.1f},{y:.1f}",
                flush=True,
            )

    holes = 0
    hole_at = []
    if unmatched:
        import geopandas as gpd
        from shapely.geometry import Point, box
        from shapely.strtree import STRtree

        gdf = gpd.read_file(
            proc / "road_surface_smooth" / "carriageway_smooth.gpkg",
            layer="carriageway",
        )
        polys = [g for g in gdf.geometry if g is not None and not g.is_empty]
        tree = STRtree(polys)
        bbox = (
            float(sc.xmin),
            float(sc.ymin),
            float(sc.xmax),
            float(sc.ymax),
        )
        for i in unmatched:
            x, y = sc.terrain_to_crs(float(mx[i]), float(my[i]))
            if min(x - bbox[0], bbox[2] - x, y - bbox[1], bbox[3] - y) <= 1.0:
                continue
            pt = Point(x, y)
            near = False
            for idx in tree.query(box(x - 2.0, y - 2.0, x + 2.0, y + 2.0)):
                if polys[int(idx)].boundary.distance(pt) <= HOLE_INSIDE_M:
                    near = True
                    break
            if not near:
                holes += 1
                if len(hole_at) < 8:
                    hole_at.append((x, y, owners[i], kinds[i]))
        print(
            f"unmatched edges not on clip outline (>{HOLE_INSIDE_M:.2f} m inside): {holes}",
            flush=True,
        )
        for x, y, name, kind in hole_at:
            print(f"    hole {kind} {name}  CRS {x:.1f},{y:.1f}", flush=True)

    hair = _poly_hairlines(proc)
    print(
        f"at-grade clip: {hair['components']} components, "
        f"2 cm close fills {hair['close_2cm_m2']} m2, "
        f"5 cm close fills {hair['close_5cm_m2']} m2",
        flush=True,
    )


if __name__ == "__main__":
    main()
