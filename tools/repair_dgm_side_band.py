"""Fill the carriageway from each edge inward, then the remaining interior gaps.

Left and right are separate lines. Each line is buffered 1 m toward the
carriageway. Clean DGM heights in that band become a height along the edge.
Rough pixels drop out, and the gaps are linear in arc length on that side
only. Nothing is written into a BeamNG level.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\repair_dgm_side_band.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
import tifffile as tiff
from scipy.spatial import cKDTree
from shapely import intersects_xy
from shapely.geometry import LineString, Point, Polygon

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_road_grid import RES_M, _load_geotiff  # noqa: E402
from filter_road_corridor import write_referenced_tif  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

DROP_ROUGH_M = 0.015
BAND_M = 1.0
BIN_M = 0.5
ROAD_NEIGHBORS = 8


def _load_roughness(path: Path) -> np.ndarray:
    with tiff.TiffFile(path) as src:
        arr = np.asarray(src.pages[0].asarray(), dtype=np.float32)
    if arr.ndim == 3:
        arr = arr[..., 0]
    return arr


def _cap_index(ring: list[tuple[float, float]], target: tuple[float, float]) -> int:
    tx, ty = target
    best_i = 0
    best_d = 1e300
    n = len(ring)
    for i in range(n):
        a = ring[i]
        b = ring[(i + 1) % n]
        d = math.hypot(0.5 * (a[0] + b[0]) - tx, 0.5 * (a[1] + b[1]) - ty)
        if d < best_d:
            best_d = d
            best_i = i
    return best_i


def _chain(ring: list[tuple[float, float]], after: int, until: int) -> list[tuple[float, float]]:
    n = len(ring)
    start = (after + 1) % n
    end = until % n
    out = [ring[start]]
    i = start
    while i != end:
        i = (i + 1) % n
        out.append(ring[i])
        if len(out) > n:
            return []
    return out


def _is_left(chain: list[tuple[float, float]], line: LineString) -> bool:
    mid = chain[len(chain) // 2]
    station = float(line.project(Point(mid)))
    here = line.interpolate(station)
    ahead = line.interpolate(min(line.length, station + 1.0))
    tx, ty = ahead.x - here.x, ahead.y - here.y
    if math.hypot(tx, ty) < 1e-6:
        ahead = line.interpolate(max(0.0, station - 1.0))
        tx, ty = here.x - ahead.x, here.y - ahead.y
    return tx * (mid[1] - here.y) - ty * (mid[0] - here.x) > 0


def _with_centerline(chain: list[tuple[float, float]], line: LineString) -> list[tuple[float, float]]:
    start = Point(line.coords[0])
    if Point(chain[0]).distance(start) > Point(chain[-1]).distance(start):
        return list(reversed(chain))
    return chain


def _split_sides(poly: Polygon, line: LineString) -> dict[str, LineString] | None:
    ring = [(float(x), float(y)) for x, y in list(poly.exterior.coords)[:-1]]
    if len(ring) < 4:
        return None
    i_start = _cap_index(ring, (float(line.coords[0][0]), float(line.coords[0][1])))
    i_end = _cap_index(ring, (float(line.coords[-1][0]), float(line.coords[-1][1])))
    if i_start == i_end:
        return None
    first = _chain(ring, i_start, i_end)
    second = _chain(ring, i_end, i_start)
    if len(first) < 2 or len(second) < 2:
        return None
    sides: dict[str, list[tuple[float, float]]] = {}
    for chain in (first, second):
        name = "left" if _is_left(chain, line) else "right"
        sides[name] = _with_centerline(chain, line)
    if set(sides) != {"left", "right"}:
        return None
    rebuilt = Polygon(sides["left"] + sides["right"][::-1])
    if rebuilt.is_empty or poly.area <= 0:
        return None
    if not rebuilt.is_valid:
        rebuilt = rebuilt.buffer(0)
    if abs(rebuilt.area - poly.area) / poly.area > 0.02:
        return None
    return {name: LineString(coords) for name, coords in sides.items()}


def _inward_strip(line: LineString, poly: Polygon):
    best = None
    best_area = -1.0
    for sign in (1.0, -1.0):
        geom = line.buffer(sign * BAND_M, single_sided=True, cap_style="flat", join_style="round")
        if geom.is_empty:
            continue
        if not geom.is_valid:
            geom = geom.buffer(0)
        area = float(geom.intersection(poly).area)
        if area > best_area:
            best_area = area
            best = geom
    if best is None or best.is_empty or best_area < 0.5 * BAND_M * line.length * 0.25:
        return None
    return best


def _pixels(poly, z, rough, spec: dict):
    minx, miny, maxx, maxy = poly.bounds
    width = int(spec["width"])
    height = int(spec["height"])
    c0 = max(0, int(math.floor((minx - spec["xmin"]) / RES_M)))
    c1 = min(width, int(math.ceil((maxx - spec["xmin"]) / RES_M)))
    r0 = max(0, int(math.floor((spec["ymax"] - maxy) / RES_M)))
    r1 = min(height, int(math.ceil((spec["ymax"] - miny) / RES_M)))
    if c1 <= c0 or r1 <= r0:
        return None
    cc = np.arange(c0, c1)
    rr = np.arange(r0, r1)
    xs = spec["xmin"] + (cc + 0.5) * RES_M
    ys = spec["ymax"] - (rr + 0.5) * RES_M
    xx, yy = np.meshgrid(xs, ys)
    inside = intersects_xy(poly, xx.ravel(), yy.ravel())
    if not np.any(inside):
        return None
    flat_r = np.repeat(rr, c1 - c0)[inside]
    flat_c = np.tile(cc, r1 - r0)[inside]
    zz = z[flat_r, flat_c]
    rrugh = rough[flat_r, flat_c]
    ok = np.isfinite(zz)
    if not np.any(ok):
        return None
    return flat_r[ok], flat_c[ok], zz[ok], rrugh[ok]


def _sample_side(line: LineString, strip, z, rough, spec: dict) -> dict | None:
    got = _pixels(strip, z, rough, spec)
    if got is None:
        return None
    rows, cols, zz, rrugh = got
    xs = spec["xmin"] + (cols + 0.5) * RES_M
    ys = spec["ymax"] - (rows + 0.5) * RES_M
    pts = shapely.points(xs, ys)
    station = np.asarray(shapely.line_locate_point(line, pts), dtype=np.float64)
    dist = np.asarray(shapely.distance(line, pts), dtype=np.float64)
    near = dist <= BAND_M + 0.5 * RES_M
    if not np.any(near):
        return None
    return {
        "rows": rows[near],
        "cols": cols[near],
        "z": zz[near],
        "rough": rrugh[near],
        "station": station[near],
        "dist": dist[near],
    }


def _drop_farther(sides: dict[str, dict], width: int) -> None:
    names = [name for name, sample in sides.items() if sample is not None]
    if len(names) < 2:
        return
    a, b = sides[names[0]], sides[names[1]]
    key_a = a["rows"].astype(np.int64) * width + a["cols"].astype(np.int64)
    key_b = b["rows"].astype(np.int64) * width + b["cols"].astype(np.int64)
    order = np.argsort(key_b)
    pos = np.searchsorted(key_b[order], key_a)
    pos = np.clip(pos, 0, len(key_b) - 1)
    match = key_b[order][pos] == key_a
    dist_b = np.full(len(key_a), np.inf)
    dist_b[match] = b["dist"][order][pos[match]]
    drop_a = match & (a["dist"] > dist_b)
    drop_b_keys = set(key_a[match & (a["dist"] <= dist_b)].tolist())
    for sample, drop in (
        (a, drop_a),
        (b, np.array([int(k) in drop_b_keys for k in key_b], dtype=bool)),
    ):
        keep = ~drop
        for field in ("rows", "cols", "z", "rough", "station", "dist"):
            sample[field] = sample[field][keep]


def _heights_along(station: np.ndarray, values: np.ndarray, length: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    nbin = int(math.floor(length / BIN_M)) + 1
    centres = np.arange(nbin) * BIN_M
    med = np.full(nbin, np.nan)
    if len(station):
        bins = np.clip(np.floor(station / BIN_M).astype(int), 0, nbin - 1)
        for b in np.unique(bins):
            med[b] = float(np.median(values[bins == b]))
    good = np.isfinite(med)
    filled = med.copy()
    gap = np.zeros(nbin, dtype=bool)
    if int(good.sum()) >= 2:
        known = np.flatnonzero(good)
        filled = np.interp(centres, centres[known], med[known])
        gap[known[0] : known[-1] + 1] = ~good[known[0] : known[-1] + 1]
    elif int(good.sum()) == 1:
        filled[:] = med[good][0]
    return centres, filled, gap


def _apply_side(sample: dict, line: LineString, repaired, klass) -> dict:
    rough = sample["rough"]
    clean = ~np.isfinite(rough) | (rough < DROP_ROUGH_M)
    centres, filled, gap = _heights_along(sample["station"][clean], sample["z"][clean], float(line.length))
    z_at = np.interp(sample["station"], centres, filled) if np.any(np.isfinite(filled)) else np.full(len(sample["station"]), np.nan)
    rows = sample["rows"]
    cols = sample["cols"]
    keep = clean & np.isfinite(sample["z"])
    fill = ~clean & np.isfinite(z_at)
    if np.any(keep):
        repaired[rows[keep], cols[keep]] = sample["z"][keep]
        klass[rows[keep], cols[keep]] = 1
    if np.any(fill):
        repaired[rows[fill], cols[fill]] = z_at[fill].astype(np.float32)
        klass[rows[fill], cols[fill]] = 3
    return {
        "n_keep": int(keep.sum()),
        "n_fill": int(fill.sum()),
        "n_gap": int(gap.sum()),
        "centres": centres,
        "filled": filled,
        "gap": gap,
    }


def _edge_samples(edge: LineString, centres: np.ndarray, filled: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    ok = np.isfinite(filled)
    if not np.any(ok):
        return None
    pts = np.array([(float(p.x), float(p.y)) for p in (edge.interpolate(float(s)) for s in centres[ok])])
    return pts, filled[ok].astype(np.float64)


def _idw(values: np.ndarray, dist: np.ndarray) -> np.ndarray:
    if dist.ndim == 1:
        return values
    weight = 1.0 / np.maximum(dist, 0.05) ** 2
    return (weight * values).sum(axis=1) / weight.sum(axis=1)


def _fill_interior(poly, edge_xy: np.ndarray, edge_z: np.ndarray, z, rough, spec, repaired, klass) -> tuple[int, int]:
    """Keep the filtered carriageway and fill remaining rough pixels from it and the edge."""
    got = _pixels(poly, z, rough, spec)
    if got is None:
        return 0, 0
    rows, cols, zz, rrugh = got
    xs = spec["xmin"] + (cols + 0.5) * RES_M
    ys = spec["ymax"] - (rows + 0.5) * RES_M
    clean = (~np.isfinite(rrugh) | (rrugh < DROP_ROUGH_M)) & (klass[rows, cols] == 0)
    n_keep = 0
    if np.any(clean):
        repaired[rows[clean], cols[clean]] = zz[clean]
        klass[rows[clean], cols[clean]] = 1
        n_keep = int(clean.sum())
    gap = np.isfinite(rrugh) & (rrugh >= DROP_ROUGH_M) & (klass[rows, cols] == 0)
    if not np.any(gap):
        return n_keep, 0
    known = (~np.isfinite(rrugh) | (rrugh < DROP_ROUGH_M)) & np.isfinite(zz)
    if not np.any(known) and len(edge_z) == 0:
        return n_keep, 0
    gap_xy = np.column_stack([xs[gap], ys[gap]])
    z_road = np.full(int(gap.sum()), np.nan)
    d_road = np.full(int(gap.sum()), np.inf)
    if np.any(known):
        k = min(ROAD_NEIGHBORS, int(known.sum()))
        tree = cKDTree(np.column_stack([xs[known], ys[known]]))
        dist, idx = tree.query(gap_xy, k=k)
        z_road = _idw(zz[known][idx], dist)
        d_road = dist if dist.ndim == 1 else dist[:, 0]
    z_edge = np.full(int(gap.sum()), np.nan)
    d_edge = np.full(int(gap.sum()), np.inf)
    if len(edge_z):
        tree = cKDTree(edge_xy)
        dist, idx = tree.query(gap_xy, k=1)
        z_edge = edge_z[idx]
        d_edge = dist
    w_road = 1.0 / np.maximum(d_road, 0.05)
    w_edge = 1.0 / np.maximum(d_edge, 0.05)
    have_road = np.isfinite(z_road)
    have_edge = np.isfinite(z_edge)
    blended = np.full(int(gap.sum()), np.nan)
    both = have_road & have_edge
    blended[both] = (w_road[both] * z_road[both] + w_edge[both] * z_edge[both]) / (w_road[both] + w_edge[both])
    blended[have_road & ~have_edge] = z_road[have_road & ~have_edge]
    blended[have_edge & ~have_road] = z_edge[have_edge & ~have_road]
    take = np.isfinite(blended)
    if not np.any(take):
        return n_keep, 0
    grow = np.flatnonzero(gap)[take]
    repaired[rows[grow], cols[grow]] = blended[take].astype(np.float32)
    klass[rows[grow], cols[grow]] = 4
    return n_keep, int(take.sum())


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    raw_path = proc / "corridor50_raw" / "corridor50_raw.tif"
    rough_path = proc / "centerline_shift_taper" / "01_roughness.tif"
    gpkg_in = proc / "centerline_shift_taper" / "centerline_shift.gpkg"
    for path in (raw_path, rough_path, gpkg_in):
        if not path.is_file():
            raise SystemExit(f"missing {path}")
    out_dir = proc / "dgm_repair_side"
    out_dir.mkdir(parents=True, exist_ok=True)

    z, spec = _load_geotiff(raw_path)
    rough = _load_roughness(rough_path)
    if rough.shape != z.shape:
        raise SystemExit(f"roughness {rough.shape} does not match DGM {z.shape}")
    roads = gpd.read_file(gpkg_in, layer="carriageway")
    lines = gpd.read_file(gpkg_in, layer="centerline")
    repaired = np.full(z.shape, np.nan, dtype=np.float32)
    klass = np.zeros(z.shape, dtype=np.uint8)

    line_geoms = []
    line_rows = []
    pt_geoms = []
    pt_rows = []
    skipped = 0
    totals = {"kept_px": 0, "filled_px": 0, "gap_left": 0, "gap_right": 0}
    pending: list[tuple] = []

    for oid, group in roads.groupby("objectid"):
        hit = lines[lines.objectid == int(oid)]
        if hit.empty:
            skipped += len(group)
            continue
        center = hit.iloc[0].geometry
        if center.geom_type == "MultiLineString":
            center = max(center.geoms, key=lambda g: g.length)
        keep_n = fill_n = 0
        gap_n = {"left": 0, "right": 0}
        for poly in group.geometry:
            if poly.geom_type == "MultiPolygon":
                parts = list(poly.geoms)
            else:
                parts = [poly]
            for part in parts:
                sides = _split_sides(part, center)
                if sides is None:
                    skipped += 1
                    print(f"  oid {oid}: edge split skipped", flush=True)
                    continue
                sampled = {}
                for name, edge in sides.items():
                    strip = _inward_strip(edge, part)
                    sampled[name] = None if strip is None else _sample_side(edge, strip, z, rough, spec)
                _drop_farther(sampled, int(spec["width"]))
                edge_xy: list[np.ndarray] = []
                edge_z: list[np.ndarray] = []
                for name, edge in sides.items():
                    sample = sampled[name]
                    if sample is None or len(sample["rows"]) == 0:
                        continue
                    done = _apply_side(sample, edge, repaired, klass)
                    samples = _edge_samples(edge, done["centres"], done["filled"])
                    if samples is not None:
                        edge_xy.append(samples[0])
                        edge_z.append(samples[1])
                    keep_n += done["n_keep"]
                    fill_n += done["n_fill"]
                    gap_n[name] += done["n_gap"]
                    line_geoms.append(edge)
                    line_rows.append(
                        {
                            "objectid": int(oid),
                            "side": name,
                            "n_gap": done["n_gap"],
                            "length_m": round(float(edge.length), 1),
                        }
                    )
                    for s, zv, is_gap in zip(done["centres"], done["filled"], done["gap"]):
                        if not np.isfinite(zv):
                            continue
                        point = edge.interpolate(float(s))
                        pt_geoms.append(Point(float(point.x), float(point.y)))
                        pt_rows.append(
                            {
                                "objectid": int(oid),
                                "side": name,
                                "s_m": round(float(s), 2),
                                "z_m": round(float(zv), 3),
                                "gap": int(bool(is_gap)),
                            }
                        )
                xy = np.vstack(edge_xy) if edge_xy else np.zeros((0, 2))
                zz = np.concatenate(edge_z) if edge_z else np.zeros(0)
                pending.append((int(oid), part, xy, zz))
        totals["kept_px"] += keep_n
        totals["filled_px"] += fill_n
        totals["gap_left"] += gap_n["left"]
        totals["gap_right"] += gap_n["right"]
        print(
            f"  oid {oid}: keep {keep_n} fill {fill_n} gap L/R {gap_n['left']}/{gap_n['right']}",
            flush=True,
        )

    write_referenced_tif(out_dir / "02_repaired.tif", repaired, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    write_referenced_tif(out_dir / "01_class.tif", klass, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    gpkg = out_dir / "edge.gpkg"
    if gpkg.exists():
        gpkg.unlink()
    if line_geoms:
        gpd.GeoDataFrame(line_rows, geometry=line_geoms, crs="EPSG:31254").to_file(gpkg, layer="edge", driver="GPKG")
    if pt_geoms:
        gpd.GeoDataFrame(pt_rows, geometry=pt_geoms, crs="EPSG:31254").to_file(
            gpkg, layer="edge_pt", driver="GPKG", mode="a"
        )
    unique = {
        "kept_px": int((klass == 1).sum()),
        "filled_px": int((klass == 3).sum()),
        "gap_left": totals["gap_left"],
        "gap_right": totals["gap_right"],
        "skipped_parts": skipped,
    }
    report = {
        "carriageway": str(gpkg_in),
        "heights": str(raw_path),
        "roughness": str(rough_path),
        "drop_rough_m": DROP_ROUGH_M,
        "band_m": BAND_M,
        "bin_m": BIN_M,
        **unique,
        "class": {
            "1": "clean pixel kept in the 1 m band inside that edge",
            "3": "rough pixel in the band, height linear along that edge",
        },
        "note": (
            "Left and right edges are filled separately. Each edge is buffered "
            f"{BAND_M:.0f} m into the carriageway. Heights of pixels below "
            f"{DROP_ROUGH_M:.3f} m roughness are the samples. Gaps are linear in "
            "arc length on that side. A pixel that falls in both bands is used "
            "only by the nearer edge."
        ),
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(unique, indent=2))
    print(f"Wrote {out_dir}")

    full = repaired.copy()
    full_k = klass.copy()
    kept_road = filled_gap = 0
    for oid, part, edge_xy_a, edge_z_a in pending:
        n_keep, n_gap = _fill_interior(part, edge_xy_a, edge_z_a, z, rough, spec, full, full_k)
        kept_road += n_keep
        filled_gap += n_gap
        print(f"  oid {oid}: interior keep {n_keep} gap {n_gap}", flush=True)
    fill_dir = proc / "dgm_repair_side_fill"
    fill_dir.mkdir(parents=True, exist_ok=True)
    write_referenced_tif(fill_dir / "02_repaired.tif", full, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    write_referenced_tif(fill_dir / "01_class.tif", full_k, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    fill_report = {
        "band": str(out_dir),
        "carriageway": str(gpkg_in),
        "heights": str(raw_path),
        "roughness": str(rough_path),
        "drop_rough_m": DROP_ROUGH_M,
        "kept_px": int((full_k == 1).sum()),
        "edge_filled_px": int((full_k == 3).sum()),
        "interior_kept_added_px": kept_road,
        "gap_filled_px": int((full_k == 4).sum()),
        "class": {
            "1": "filtered carriageway pixel, original height",
            "3": "rough pixel in the 1 m edge band, height linear along that edge",
            "4": "remaining rough pixel, blend of the filtered carriageway and the interpolated edge",
        },
        "note": (
            "Pixels under the roughness limit stay at the DGM height. Rough pixels "
            "in the edge band keep the height interpolated along that side. Every "
            "other rough pixel inside the carriageway takes a distance-weighted "
            "blend of the nearby filtered pixels and the nearest interpolated edge height."
        ),
    }
    (fill_dir / "report.json").write_text(json.dumps(fill_report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: fill_report[k] for k in ("kept_px", "edge_filled_px", "gap_filled_px")}, indent=2))
    print(f"Wrote {fill_dir}")


if __name__ == "__main__":
    main()
