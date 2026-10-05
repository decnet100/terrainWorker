"""One mesh outline per junction, and one outline per road outside it.

Roads that meet at a node are clipped to a disc around that node and joined.
A round closing of that piece, with the class radius, lays the arc on the real
kink of the outline. Nearby nodes share one outline. Each remaining road is
its own mesh. The level is unchanged until build_road_grid.py runs.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\build_junction_meshes.py
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
import shapely
from shapely import BufferJoinStyle
from shapely.geometry import Point, Polygon

sys.path.insert(0, str(Path(__file__).resolve().parent))
from site_coords import load_site, processed_dir

RADIUS = {"S-G": 6.0, "S-L": 8.0, "S-B": 10.0}
REACH_EXTRA_M = 18.0
HOLE_KEEP_M2 = 15.0
_CODE_B = re.compile(r"^B\d", re.I)
_CODE_L = re.compile(r"^L\d", re.I)
_CODE_A = re.compile(r"^A\d", re.I)
_SKIP_OBJEKT = frozenset(
    {
        "S-A", "S-AB", "S-BB", "S-LB", "S-GB",
        "S-F", "S-GW", "S-FRW", "S-STRAIL",
        "S-AR", "S-AP", "S-BR", "S-BP",
    }
)


def _radius(objekt: str, str_code: str | None) -> float | None:
    obj = (objekt or "").upper().strip()
    code = (str_code or "").upper().strip()
    if obj in _SKIP_OBJEKT or _CODE_A.match(code):
        return None
    if _CODE_B.match(code) or obj == "S-B":
        return RADIUS["S-B"]
    if _CODE_L.match(code) or obj == "S-L":
        return RADIUS["S-L"]
    if obj == "S-G":
        return RADIUS["S-G"]
    return None


def _parts(geom):
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return [g for g in geom.geoms if not g.is_empty]
    if geom.geom_type == "GeometryCollection":
        out = []
        for g in geom.geoms:
            out.extend(_parts(g))
        return out
    return []


def _close_local(local, radius: float):
    """Round the concave corners. Islands larger than a kerb stay open."""
    holes = []
    for part in _parts(local):
        for ring in part.interiors:
            hole = Polygon(ring)
            if not hole.is_empty and hole.area >= HOLE_KEEP_M2:
                holes.append(hole)
    closed = shapely.make_valid(
        local.buffer(radius, join_style=BufferJoinStyle.round).buffer(
            -radius, join_style=BufferJoinStyle.round
        )
    )
    if holes:
        closed = shapely.make_valid(closed.difference(shapely.union_all(holes)))
    return closed


def _self_check() -> None:
    from shapely.geometry import box

    union = box(0, -4, 80, 4).union(box(-4, 0, 4, 60))
    local = union.intersection(Point(0, 0).buffer(22))
    closed = _close_local(local, 6.0)
    added = closed.difference(local)
    kept = [p for p in _parts(added) if p.area >= 1.0]
    area = sum(p.area for p in kept)
    # Two real kerb returns of a 6 m quarter, about 7.7 m² each.
    if not 10.0 < area < 20.0:
        raise SystemExit(f"junction self-check added {area:.1f} m²")


def _tfw(path: Path):
    vals = [float(x) for x in path.with_suffix(".tfw").read_text().split()]
    return tuple(vals[:6])


def _paint(arr, tf, poly) -> int:
    """New kerb pixels take a distance-weighted height from the existing road."""
    from shapely import intersects_xy

    a, _d, _b, e, c0, f0 = tf
    minx, miny, maxx, maxy = poly.bounds
    pad = 8.0
    c_lo = max(0, int((minx - pad - c0) / a) - 1)
    c_hi = min(arr.shape[1] - 1, int((maxx + pad - c0) / a) + 1)
    r_lo, r_hi = sorted((int((maxy + pad - f0) / e), int((miny - pad - f0) / e)))
    r_lo = max(0, r_lo - 1)
    r_hi = min(arr.shape[0] - 1, r_hi + 1)
    cols = np.arange(c_lo, c_hi + 1)
    rows = np.arange(r_lo, r_hi + 1)
    cc, rr = np.meshgrid(cols, rows)
    xs = c0 + cc * a
    ys = f0 + rr * e
    window = arr[r_lo : r_hi + 1, c_lo : c_hi + 1]
    known = np.isfinite(window)
    if not np.any(known):
        return 0
    from scipy.spatial import cKDTree

    tree = cKDTree(np.column_stack((xs[known], ys[known])))
    known_z = window[known]
    inside = intersects_xy(poly, xs.ravel(), ys.ravel()).reshape(xs.shape)
    targets = inside & ~known
    if not np.any(targets):
        return 0
    dist, idx = tree.query(np.column_stack((xs[targets], ys[targets])), k=8, distance_upper_bound=10.0)
    if dist.ndim == 1:
        dist = dist[:, None]
        idx = idx[:, None]
    weight = np.where(np.isfinite(dist), 1.0 / (dist + 0.4), 0.0)
    weight[idx >= known_z.size] = 0.0
    safe = np.clip(idx, 0, known_z.size - 1)
    num = (weight * known_z[safe]).sum(axis=1)
    den = weight.sum(axis=1)
    ok = den > 0.0
    flat = np.flatnonzero(targets)
    use = flat[ok]
    if use.size == 0:
        return 0
    rr_i, cc_i = np.unravel_index(use, targets.shape)
    arr[r_lo + rr_i, c_lo + cc_i] = (num[ok] / den[ok]).astype(np.float32)
    return int(ok.sum())


def main() -> None:
    import geopandas as gpd
    import pandas as pd
    from scipy.spatial import cKDTree

    _self_check()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    site = load_site()
    proc = processed_dir(site)
    smooth_dir = proc / "road_surface_smooth"
    backup = smooth_dir / "carriageway_smooth.pre_corner.gpkg"
    gpkg = backup if backup.is_file() else smooth_dir / "carriageway_smooth.gpkg"
    gdf = gpd.read_file(gpkg, layer="carriageway")
    road = gdf[(gdf["mesh_kind"] == "road") & (gdf["mesh_layer"].astype(int) == 0)]
    polys: dict[int, list] = {}
    for rec in road.itertuples(index=False):
        polys.setdefault(int(rec.objectid), []).extend(_parts(rec.geometry))
    covers = {oid: shapely.make_valid(shapely.union_all(parts)) for oid, parts in polys.items()}

    cl = gpd.read_file(proc / "centerline_shift_taper" / "centerline_shift.gpkg", layer="centerline")
    lines = {}
    for rec in cl.itertuples(index=False):
        geom = rec.geometry
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type == "MultiLineString":
            geom = max(geom.geoms, key=lambda g: g.length)
        lines[int(rec.objectid)] = geom

    routing = json.loads((proc / "gip_routing" / "gip_routing.json").read_text(encoding="utf-8"))
    roads = routing["roads"]
    nodes = routing["nodes"]
    at_node: dict[str, list[int]] = {}
    for oid, info in roads.items():
        oid_i = int(oid)
        if oid_i not in lines or oid_i not in covers:
            continue
        for nid in info.get("nodes") or []:
            at_node.setdefault(nid, []).append(oid_i)

    junctions = []
    for nid, members in at_node.items():
        node = nodes.get(nid)
        if node is None:
            continue
        origin = (float(node["x"]), float(node["y"]))
        point = Point(origin)
        arms = []
        seen = set()
        for oid in members:
            if oid in seen or lines[oid].distance(point) > 4.0:
                continue
            seen.add(oid)
            info = roads[str(oid)]
            arms.append(
                {
                    "oid": oid,
                    "poly": covers[oid],
                    "radius": _radius(str(info.get("objekt") or ""), info.get("str_code")),
                }
            )
        if len(arms) < 3:
            continue
        radii = [arm["radius"] for arm in arms if arm["radius"] is not None]
        junctions.append(
            {
                "nid": nid,
                "origin": origin,
                "arms": arms,
                "radius": max(radii) if radii else RADIUS["S-G"],
                "name": str(node.get("name") or ""),
            }
        )
    if len(junctions) < 2:
        raise SystemExit("no junctions")

    coords = np.array([j["origin"] for j in junctions], dtype=np.float64)
    tree = cKDTree(coords)
    parent = list(range(len(junctions)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, junction in enumerate(junctions):
        reach = junction["radius"] + REACH_EXTRA_M
        for j in tree.query_ball_point(junction["origin"], reach + RADIUS["S-B"] + REACH_EXTRA_M):
            if j <= i:
                continue
            other = junctions[j]["radius"] + REACH_EXTRA_M
            dist = math.hypot(coords[i, 0] - coords[j, 0], coords[i, 1] - coords[j, 1])
            if dist < reach + other:
                parent[find(j)] = find(i)

    clusters: dict[int, list[int]] = {}
    for i in range(len(junctions)):
        clusters.setdefault(find(i), []).append(i)

    zones = []
    added_polys = []
    for group in clusters.values():
        members = [junctions[i] for i in group]
        radius = max(item["radius"] for item in members)
        reach = shapely.union_all(
            [Point(item["origin"]).buffer(item["radius"] + REACH_EXTRA_M) for item in members]
        )
        arms = {}
        for item in members:
            for arm in item["arms"]:
                arms[arm["oid"]] = arm["poly"]
        full = shapely.make_valid(shapely.union_all(list(arms.values())))
        local = shapely.make_valid(full.intersection(reach))
        if local.is_empty:
            continue
        closed = _close_local(local, radius)
        kept = [
            part for part in _parts(closed.difference(local))
            if 1.0 <= part.area <= radius * radius
        ]
        zone = shapely.make_valid(shapely.union_all([local, *kept])) if kept else local
        pieces = [part for part in _parts(zone) if part.area >= 1.0]
        if not pieces:
            continue
        zone = pieces[0] if len(pieces) == 1 else shapely.union_all(pieces)
        zones.append(
            {
                "poly": zone,
                "radius": radius,
                "oids": sorted(arms),
                "name": members[0]["name"],
                "added": sum(part.area for part in kept),
            }
        )
        added_polys.extend(kept)

    added_area = sum(item["added"] for item in zones)
    print(
        f"junctions {len(junctions)} in {len(clusters)} clusters, "
        f"meshes {len(zones)}, kerb added {added_area:.0f} m²"
    )
    for row in zones[:8]:
        print(
            f"  R={row['radius']:.0f} m arms {len(row['oids'])} "
            f"added {row['added']:.1f} m² {row['name']}"
        )
    if args.dry_run or not zones:
        print("dry run" if args.dry_run else "no junction meshes")
        return

    zone_geoms = [item["poly"] for item in zones]
    spatial = shapely.STRtree(zone_geoms)
    from shapely.ops import snap

    # Shared vertices with the junction outline: each road end is snapped onto
    # the zone ring before the mesh clip sees two crooked boundaries.
    SNAP_TO_ZONE_M = 0.05
    link_rows = []
    for oid, poly in covers.items():
        geom = poly
        hit = list(np.atleast_1d(spatial.query(geom, predicate="intersects")))
        for index in hit:
            geom = shapely.make_valid(geom.difference(zone_geoms[int(index)]))
        if hit:
            zone_u = (
                zone_geoms[int(hit[0])]
                if len(hit) == 1
                else shapely.union_all([zone_geoms[int(i)] for i in hit])
            )
            try:
                geom = shapely.make_valid(snap(geom, zone_u, SNAP_TO_ZONE_M))
            except Exception:
                pass
        for part in _parts(geom):
            if part.area < 1.0:
                continue
            link_rows.append(
                {
                    "objectid": oid,
                    "members": str(oid),
                    "narrowed": 0,
                    "narrow_m": 0.0,
                    "mesh_kind": "link",
                    "mesh_key": oid,
                    "mesh_layer": 0,
                    "geometry": part,
                }
            )
    junction_rows = []
    for i, item in enumerate(zones):
        for part in _parts(item["poly"]):
            if part.area < 1.0:
                continue
            junction_rows.append(
                {
                    "objectid": 880_000_000 + i,
                    "members": ",".join(str(oid) for oid in item["oids"]),
                    "narrowed": 0,
                    "narrow_m": 0.0,
                    "mesh_kind": "junction",
                    "mesh_key": i + 1,
                    "mesh_layer": 0,
                    "geometry": part,
                }
            )
    other = gdf[~((gdf["mesh_kind"] == "road") & (gdf["mesh_layer"].astype(int) == 0))]
    out = pd.concat(
        [
            other,
            gpd.GeoDataFrame(link_rows, geometry="geometry", crs=gdf.crs),
            gpd.GeoDataFrame(junction_rows, geometry="geometry", crs=gdf.crs),
        ],
        ignore_index=True,
    )
    dest = smooth_dir / "carriageway_smooth.gpkg"
    if dest.exists():
        dest.unlink()
    out.to_file(dest, layer="carriageway", driver="GPKG")
    print(f"roads {len(link_rows)} parts, junctions {len(junction_rows)} parts -> {dest.name}")

    import tifffile as tiff
    from filter_road_corridor import write_referenced_tif

    for name in ("04_smooth.tif", "04_layer0.tif"):
        src = smooth_dir / name.replace(".tif", ".pre_corner.tif")
        if not src.is_file():
            src = smooth_dir / name
        arr = np.asarray(tiff.imread(src), dtype=np.float32)
        if arr.ndim == 3:
            arr = arr[..., 0]
        tf = _tfw(src)
        a, _d, _b, _e, c0, f0 = tf
        res = abs(float(a))
        xmin = float(c0) - 0.5 * res
        ymax = float(f0) + 0.5 * res
        written = 0
        for poly in added_polys:
            written += _paint(arr, tf, poly)
        write_referenced_tif(smooth_dir / name, arr, xmin=xmin, ymax=ymax, res=res)
        print(f"kerb heights {written} px -> {name}")


if __name__ == "__main__":
    main()
