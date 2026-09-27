"""Rebuild the carriageway edge by walking the local normal.

At each station a ray runs left and right of the centerline and reads the DGM.
The rim is the last clean sample on that ray. Where it falls inward of the
carriageway, the station is a gap: offset and height are interpolated from the
neighbouring good rim samples. Missing pixels between that rim and the
remaining surface take their height from the same ray. Nothing is written
into a BeamNG level.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\repair_dgm_edge_profile.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import tifffile as tiff
from shapely import intersects_xy
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_road_grid import RES_M, _load_geotiff  # noqa: E402
from filter_road_corridor import write_referenced_tif  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

DROP_ROUGH_M = 0.015
EDGE_TOL_M = 1.0
STATION_M = 0.5
RAY_STEP_M = 0.25
MAX_HALF_M = 16.0
OUTSIDE_GAP_M = 1.5


def _load_roughness(path: Path) -> np.ndarray:
    with tiff.TiffFile(path) as src:
        arr = np.asarray(src.pages[0].asarray(), dtype=np.float32)
    if arr.ndim == 3:
        arr = arr[..., 0]
    return arr


def _densify(coords: list[tuple[float, float]], step: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    pts = [coords[0]]
    station = [0.0]
    cursor = 0.0
    for a, b in zip(coords, coords[1:]):
        dist = math.hypot(b[0] - a[0], b[1] - a[1])
        if dist < 1e-6:
            continue
        n = max(1, int(math.ceil(dist / step)))
        for i in range(1, n + 1):
            t = i / n
            pts.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            station.append(cursor + dist * t)
        cursor += dist
    xy = np.asarray(pts, dtype=np.float64)
    s = np.asarray(station, dtype=np.float64)
    normals = np.zeros_like(xy)
    for i in range(len(xy)):
        if i == 0:
            a, b = xy[0], xy[1]
        elif i == len(xy) - 1:
            a, b = xy[-2], xy[-1]
        else:
            a, b = xy[i - 1], xy[i + 1]
        dx, dy = b[0] - a[0], b[1] - a[1]
        span = math.hypot(dx, dy)
        if span >= 1e-6:
            normals[i] = (-dy / span, dx / span)
    return xy, s, normals


def _interp_gaps(values: np.ndarray, good: np.ndarray) -> np.ndarray:
    out = values.astype(np.float64).copy()
    idx = np.flatnonzero(good)
    if len(idx) == 0:
        return out
    out[: idx[0]] = out[idx[0]]
    out[idx[-1] + 1 :] = out[idx[-1]]
    for a, b in zip(idx[:-1], idx[1:]):
        if b <= a + 1:
            continue
        span = b - a
        t = np.arange(1, span) / span
        out[a + 1 : b] = out[a] * (1.0 - t) + out[b] * t
    return out


def _crop_inside(poly, spec: dict) -> tuple[np.ndarray, int, int] | None:
    minx, miny, maxx, maxy = poly.bounds
    width = int(spec["width"])
    height = int(spec["height"])
    c0 = max(0, int(math.floor((minx - spec["xmin"]) / RES_M)) - 2)
    c1 = min(width, int(math.ceil((maxx - spec["xmin"]) / RES_M)) + 2)
    r0 = max(0, int(math.floor((spec["ymax"] - maxy) / RES_M)) - 2)
    r1 = min(height, int(math.ceil((spec["ymax"] - miny) / RES_M)) + 2)
    if c1 <= c0 or r1 <= r0:
        return None
    cc = np.arange(c0, c1)
    rr = np.arange(r0, r1)
    xs = spec["xmin"] + (cc + 0.5) * RES_M
    ys = spec["ymax"] - (rr + 0.5) * RES_M
    xx, yy = np.meshgrid(xs, ys)
    inside = intersects_xy(poly, xx.ravel(), yy.ravel()).reshape(xx.shape)
    if not np.any(inside):
        return None
    return inside, r0, c0


def _ray_table(xy: np.ndarray, normals: np.ndarray, z, rough, inside: np.ndarray, r0: int, c0: int, spec: dict):
    """Walk every station along its normal. Returns per-step samples."""
    n = len(xy)
    steps = int(math.ceil(MAX_HALF_M / RAY_STEP_M))
    signed = np.concatenate([
        -np.arange(steps, 0, -1) * RAY_STEP_M,
        [0.0],
        np.arange(1, steps + 1) * RAY_STEP_M,
    ])
    height, width = z.shape
    # sample[step, station]
    sd = np.full((len(signed), n), np.nan, dtype=np.float64)
    sz = np.full((len(signed), n), np.nan, dtype=np.float32)
    sclean = np.zeros((len(signed), n), dtype=bool)
    srow = np.full((len(signed), n), -1, dtype=np.int32)
    scol = np.full((len(signed), n), -1, dtype=np.int32)
    alive = np.ones(n, dtype=bool)
    been = np.zeros(n, dtype=bool)
    out_run = np.zeros(n, dtype=np.float64)
    # Index ``steps`` is the centre. The negative half decreases toward index 0,
    # so walking that way leaves the centre toward the right.
    for side_steps in (list(range(steps, -1, -1)), list(range(steps, len(signed)))):
        alive[:] = True
        been[:] = False
        out_run[:] = 0.0
        prev_abs = 0.0
        for k in side_steps:
            d = float(signed[k])
            xs = xy[:, 0] + d * normals[:, 0]
            ys = xy[:, 1] + d * normals[:, 1]
            cols = np.floor((xs - spec["xmin"]) / RES_M).astype(np.int32) - c0
            rows = np.floor((spec["ymax"] - ys) / RES_M).astype(np.int32) - r0
            h, w = inside.shape
            on = alive & (rows >= 0) & (cols >= 0) & (rows < h) & (cols < w)
            hit = np.zeros(n, dtype=bool)
            if np.any(on):
                hit[on] = inside[rows[on], cols[on]]
            gr = rows + r0
            gc = cols + c0
            in_raster = on & (gr >= 0) & (gc >= 0) & (gr < height) & (gc < width)
            zz = np.full(n, np.nan, dtype=np.float32)
            rr = np.full(n, np.nan, dtype=np.float32)
            if np.any(in_raster & hit):
                sel = in_raster & hit
                zz[sel] = z[gr[sel], gc[sel]]
                rr[sel] = rough[gr[sel], gc[sel]]
            finite = hit & np.isfinite(zz)
            clean = finite & (~np.isfinite(rr) | (rr < DROP_ROUGH_M))
            sd[k] = np.where(finite, d, np.nan)
            sz[k] = np.where(finite, zz, np.nan)
            sclean[k] = clean
            srow[k] = np.where(finite, gr, -1)
            scol[k] = np.where(finite, gc, -1)
            been |= finite
            step = abs(d) - prev_abs
            prev_abs = abs(d)
            out_run = np.where(finite, 0.0, np.where(been, out_run + step, out_run))
            alive &= out_run <= OUTSIDE_GAP_M
            if not np.any(alive):
                break
    return signed, sd, sz, sclean, srow, scol


def _side_rim(signed: np.ndarray, sd: np.ndarray, sz: np.ndarray, clean: np.ndarray, side: int):
    """Last clean sample and last carriageway sample on one side, per station."""
    n = sd.shape[1]
    on = signed * side > 0.2
    outer = np.full(n, np.nan)
    kept_d = np.full(n, np.nan)
    kept_z = np.full(n, np.nan)
    good = np.zeros(n, dtype=bool)
    if not np.any(on):
        return outer, kept_d, kept_z, good
    block_d = np.where(on[:, None] & np.isfinite(sd), sd, np.nan)
    block_c = np.where(on[:, None] & clean, sd, np.nan)
    # outermost is the max of signed*side
    score = block_d * side
    has = np.isfinite(score)
    if np.any(has):
        score = np.where(has, score, -np.inf)
        pick = np.argmax(score, axis=0)
        take = np.isfinite(block_d[pick, np.arange(n)])
        outer[take] = block_d[pick[take], np.arange(n)[take]]
    score_c = block_c * side
    has_c = np.isfinite(score_c)
    if np.any(has_c):
        score_c = np.where(has_c, score_c, -np.inf)
        pick = np.argmax(score_c, axis=0)
        take = np.isfinite(block_c[pick, np.arange(n)])
        kept_d[take] = block_c[pick[take], np.arange(n)[take]]
        kept_z[take] = sz[pick[take], np.arange(n)[take]]
    good = np.isfinite(kept_d) & np.isfinite(outer) & (np.abs(outer - kept_d) <= EDGE_TOL_M)
    return outer, kept_d, kept_z, good


def _apply_oid(oid: int, line: LineString, poly, z, rough, spec, repaired, klass) -> dict | None:
    xy, station, normals = _densify([(float(x), float(y)) for x, y in line.coords], STATION_M)
    if len(xy) < 2 or np.allclose(normals, 0):
        return None
    cropped = _crop_inside(poly, spec)
    if cropped is None:
        return None
    inside, r0, c0 = cropped
    signed, sd, sz, sclean, srow, scol = _ray_table(xy, normals, z, rough, inside, r0, c0, spec)
    _, left_d, left_z, good_l = _side_rim(signed, sd, sz, sclean, +1)
    _, right_d, right_z, good_r = _side_rim(signed, sd, sz, sclean, -1)
    d_left_i = _interp_gaps(np.where(good_l, left_d, np.nan), good_l)
    z_left_i = _interp_gaps(np.where(good_l, left_z, np.nan), good_l)
    d_right_i = _interp_gaps(np.where(good_r, right_d, np.nan), good_r)
    z_right_i = _interp_gaps(np.where(good_r, right_z, np.nan), good_r)
    gap_l = ~good_l & np.isfinite(d_left_i)
    gap_r = ~good_r & np.isfinite(d_right_i)

    n_keep = n_fill = n_out = 0
    n = len(xy)
    for i in range(n):
        if not np.isfinite(d_left_i[i]) or not np.isfinite(d_right_i[i]):
            continue
        dd = sd[:, i]
        finite = np.isfinite(dd)
        if not np.any(finite):
            continue
        clean = sclean[:, i] & finite
        edge_l = float(d_left_i[i])
        edge_r = float(d_right_i[i])
        if np.any(clean & (dd > 0)):
            edge_l = max(edge_l, float(dd[clean & (dd > 0)].max()))
        if np.any(clean & (dd < 0)):
            edge_r = min(edge_r, float(dd[clean & (dd < 0)].min()))
        uniq_d = [edge_r]
        uniq_z = [float(z_right_i[i])]
        order = np.argsort(dd)
        for k in order:
            if not clean[k]:
                continue
            dv = float(dd[k])
            if dv <= uniq_d[-1] + 0.05 or dv >= edge_l - 0.05:
                continue
            uniq_d.append(dv)
            uniq_z.append(float(sz[k, i]))
        if edge_l > uniq_d[-1] + 1e-3:
            uniq_d.append(edge_l)
            uniq_z.append(float(z_left_i[i]))
        else:
            uniq_d[-1] = max(edge_l, uniq_d[0] + 0.05)
            uniq_z[-1] = float(z_left_i[i])
        span = finite & (dd >= edge_r) & (dd <= edge_l)
        rows = srow[:, i]
        cols = scol[:, i]
        have = span & (rows >= 0)
        keep_here = have & clean
        fill_here = have & ~clean
        if np.any(keep_here):
            repaired[rows[keep_here], cols[keep_here]] = sz[keep_here, i]
            klass[rows[keep_here], cols[keep_here]] = 1
            n_keep += int(keep_here.sum())
        if np.any(fill_here):
            repaired[rows[fill_here], cols[fill_here]] = np.interp(
                dd[fill_here], uniq_d, uniq_z
            ).astype(np.float32)
            klass[rows[fill_here], cols[fill_here]] = 3
            n_fill += int(fill_here.sum())
        outside = finite & ~span & (rows >= 0)
        if np.any(outside):
            klass[rows[outside], cols[outside]] = 2
            n_out += int(outside.sum())
        d_left_i[i] = edge_l
        d_right_i[i] = edge_r

    left_xy = xy + normals * d_left_i[:, None]
    right_xy = xy + normals * d_right_i[:, None]
    return {
        "objectid": oid,
        "left_xy": left_xy,
        "right_xy": right_xy,
        "station": station,
        "z_left": z_left_i,
        "z_right": z_right_i,
        "gap_l": gap_l,
        "gap_r": gap_r,
        "n_keep": n_keep,
        "n_fill": n_fill,
        "n_out": n_out,
    }


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    raw_path = proc / "corridor50_raw" / "corridor50_raw.tif"
    rough_path = proc / "centerline_shift_taper" / "01_roughness.tif"
    gpkg_in = proc / "centerline_shift_taper" / "centerline_shift.gpkg"
    for path in (raw_path, rough_path, gpkg_in):
        if not path.is_file():
            raise SystemExit(f"missing {path}")
    out_dir = proc / "dgm_repair_ray"
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
    totals = {"kept_px": 0, "filled_px": 0, "outside_px": 0, "gap_left": 0, "gap_right": 0}

    for oid, group in roads.groupby("objectid"):
        hit = lines[lines.objectid == int(oid)]
        if hit.empty:
            continue
        line = hit.iloc[0].geometry
        if line.geom_type == "MultiLineString":
            line = max(line.geoms, key=lambda g: g.length)
        poly = unary_union(list(group.geometry))
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        done = _apply_oid(int(oid), line, poly, z, rough, spec, repaired, klass)
        if done is None:
            continue
        totals["kept_px"] += done["n_keep"]
        totals["filled_px"] += done["n_fill"]
        totals["outside_px"] += done["n_out"]
        totals["gap_left"] += int(done["gap_l"].sum())
        totals["gap_right"] += int(done["gap_r"].sum())
        for side, arr, zz, gap in (
            ("left", done["left_xy"], done["z_left"], done["gap_l"]),
            ("right", done["right_xy"], done["z_right"], done["gap_r"]),
        ):
            ok = np.isfinite(arr[:, 0]) & np.isfinite(zz)
            if ok.sum() < 2:
                continue
            line_geoms.append(LineString(arr[ok].tolist()))
            line_rows.append(
                {
                    "objectid": int(oid),
                    "side": side,
                    "n_gap": int(gap.sum()),
                    "length_m": round(float(done["station"][ok][-1] - done["station"][ok][0]), 1),
                }
            )
            for s, pxy, zv, is_gap in zip(done["station"], arr, zz, gap):
                if not np.isfinite(zv):
                    continue
                pt_geoms.append(Point(float(pxy[0]), float(pxy[1])))
                pt_rows.append(
                    {
                        "objectid": int(oid),
                        "side": side,
                        "s_m": round(float(s), 2),
                        "z_m": round(float(zv), 3),
                        "gap": int(bool(is_gap)),
                    }
                )
        print(
            f"  oid {oid}: keep {done['n_keep']} fill {done['n_fill']} "
            f"gap L/R {int(done['gap_l'].sum())}/{int(done['gap_r'].sum())}",
            flush=True,
        )

    write_referenced_tif(out_dir / "02_repaired.tif", repaired, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    write_referenced_tif(out_dir / "01_class.tif", klass, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    gpkg = out_dir / "edge.gpkg"
    if gpkg.exists():
        gpkg.unlink()
    if line_geoms:
        gpd.GeoDataFrame(line_rows, geometry=line_geoms, crs="EPSG:31254").to_file(
            gpkg, layer="edge", driver="GPKG"
        )
    if pt_geoms:
        gpd.GeoDataFrame(pt_rows, geometry=pt_geoms, crs="EPSG:31254").to_file(
            gpkg, layer="edge_pt", driver="GPKG", mode="a"
        )
    report = {
        "carriageway": str(gpkg_in),
        "centerline": "centerline layer of that file",
        "heights": str(raw_path),
        "drop_rough_m": DROP_ROUGH_M,
        "edge_tol_m": EDGE_TOL_M,
        "station_m": STATION_M,
        **totals,
        "class": {
            "1": "kept DGM pixel",
            "2": "outside the reconstructed rim",
            "3": "filled between the reconstructed rim and the remaining surface",
        },
        "note": (
            "Each station casts a ray along its left and right normal and reads the "
            f"DGM every {RAY_STEP_M:.2f} m. The rim is the last clean sample on that "
            "ray. A side is a gap when that sample lies more than "
            f"{EDGE_TOL_M:.0f} m inside the carriageway. Gap offset and height are "
            "linear along the station. Samples between the rim and the remaining "
            "clean pixels take a height interpolated across the ray."
        ),
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(totals, indent=2))
    print(f"Wrote {out_dir}")


if __name__ == "__main__":
    main()
