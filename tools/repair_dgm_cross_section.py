"""Fill rough edge pixels by extending the measured cross-section.

At each station the good pixels 1.0–2.5 m inside the edge give a height and a
cross-slope. A rough pixel in the outer metre keeps that slope, extended
outward. Where that inner stretch is itself rough, height and slope come from
the last good station before the gap and the first after it: linear up to
20 m, a parabola on the height beyond that. The slope stays linear. Nothing
is written past the last good station, and a single station is not spread
along the side. Pixels that already pass stay at the raw DGM height.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\repair_dgm_cross_section.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
from shapely.geometry import LineString, Point

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_road_grid import RES_M, _load_geotiff  # noqa: E402
from filter_road_corridor import write_referenced_tif  # noqa: E402
from repair_dgm_edge_profile import _densify  # noqa: E402
from repair_dgm_iterate import _deviation, _prepare, _road_mask, _stats  # noqa: E402
from repair_dgm_side_band import DROP_ROUGH_M  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

STATION_M = 0.5
RAY_STEP_M = 0.25
INNER_LO_M = 1.0
INNER_HI_M = 2.5
FILL_TO_M = 1.0
SHORT_GAP_M = 20.0
PARABOLA_WINDOW_M = 40.0
MAX_SLOPE = 0.25
MAX_RMSE_M = 0.04


def _inward(xy: np.ndarray, normals: np.ndarray, poly) -> np.ndarray:
    step = max(1, len(xy) // 30)
    sel = slice(None, None, step)
    plus = shapely.points(xy[sel, 0] + normals[sel, 0], xy[sel, 1] + normals[sel, 1])
    minus = shapely.points(xy[sel, 0] - normals[sel, 0], xy[sel, 1] - normals[sel, 1])
    n_plus = int(np.count_nonzero(shapely.intersects(poly, plus)))
    n_minus = int(np.count_nonzero(shapely.intersects(poly, minus)))
    sign = 1.0 if n_plus >= n_minus else -1.0
    return normals * sign


def _rays(xy, inward, z, rough, road, spec):
    offs = np.arange(0.0, INNER_HI_M + 1e-9, RAY_STEP_M)
    n = len(xy)
    zz = np.full((len(offs), n), np.nan, dtype=np.float32)
    rr = np.full((len(offs), n), np.nan, dtype=np.float32)
    inside = np.zeros((len(offs), n), dtype=bool)
    rows = np.full((len(offs), n), -1, dtype=np.int32)
    cols = np.full((len(offs), n), -1, dtype=np.int32)
    height, width = z.shape
    for k, dist in enumerate(offs):
        xs = xy[:, 0] + dist * inward[:, 0]
        ys = xy[:, 1] + dist * inward[:, 1]
        cc = np.floor((xs - spec["xmin"]) / RES_M).astype(np.int32)
        rr_i = np.floor((spec["ymax"] - ys) / RES_M).astype(np.int32)
        ok = (rr_i >= 0) & (cc >= 0) & (rr_i < height) & (cc < width)
        if not np.any(ok):
            continue
        rows[k, ok] = rr_i[ok]
        cols[k, ok] = cc[ok]
        zz[k, ok] = z[rr_i[ok], cc[ok]]
        rr[k, ok] = rough[rr_i[ok], cc[ok]]
        inside[k, ok] = road[rr_i[ok], cc[ok]]
    return offs, zz, rr, inside, rows, cols


def _fit_station(offs, zz, rr, inside):
    band = (offs >= INNER_LO_M) & (offs <= INNER_HI_M)
    good = inside & np.isfinite(zz) & np.isfinite(rr) & (rr < DROP_ROUGH_M) & band[:, None]
    x = offs.astype(np.float64)[:, None]
    n = good.sum(axis=0).astype(np.float64)
    sx = np.where(good, x, 0.0).sum(axis=0)
    sy = np.where(good, zz, 0.0).sum(axis=0)
    sxx = np.where(good, x * x, 0.0).sum(axis=0)
    sxy = np.where(good, x * zz, 0.0).sum(axis=0)
    den = n * sxx - sx * sx
    slope = np.full(zz.shape[1], np.nan)
    intercept = np.full(zz.shape[1], np.nan)
    ok = (n >= 3) & (den > 1e-4)
    slope[ok] = (n[ok] * sxy[ok] - sx[ok] * sy[ok]) / den[ok]
    intercept[ok] = (sy[ok] - slope[ok] * sx[ok]) / n[ok]
    x_hi = np.where(good, x, -np.inf).max(axis=0)
    x_lo = np.where(good, x, np.inf).min(axis=0)
    span = x_hi - x_lo
    pred = intercept + slope * x
    err2 = np.where(good, (zz - pred) ** 2, 0.0).sum(axis=0)
    rmse = np.sqrt(err2 / np.maximum(n, 1.0))
    valid = ok & (span >= 0.5) & (np.abs(slope) <= MAX_SLOPE) & (rmse <= MAX_RMSE_M)
    intercept = np.where(valid, intercept, np.nan)
    slope = np.where(valid, slope, np.nan)
    return intercept, slope, valid


def _close_gaps(station, intercept, slope, valid):
    out_a = intercept.copy()
    out_b = slope.copy()
    source = np.zeros(len(station), dtype=np.uint8)
    source[valid] = 1
    idx = np.flatnonzero(valid)
    if len(idx) < 2:
        return out_a, out_b, source
    for i0, i1 in zip(idx[:-1], idx[1:]):
        if i1 <= i0 + 1:
            continue
        gap = float(station[i1] - station[i0])
        if gap < 1e-6:
            continue
        mid = np.arange(i0 + 1, i1)
        share = (station[mid] - station[i0]) / gap
        out_b[mid] = slope[i0] * (1.0 - share) + slope[i1] * share
        if gap <= SHORT_GAP_M:
            out_a[mid] = intercept[i0] * (1.0 - share) + intercept[i1] * share
            source[mid] = 2
            continue
        sel = valid & (station >= station[i0] - PARABOLA_WINDOW_M) & (station <= station[i1] + PARABOLA_WINDOW_M)
        if int(sel.sum()) >= 4 and np.any(station[sel] <= station[i0]) and np.any(station[sel] >= station[i1]):
            coef = np.polyfit(station[sel], intercept[sel], 2)
            out_a[mid] = np.polyval(coef, station[mid])
            source[mid] = 3
        else:
            out_a[mid] = intercept[i0] * (1.0 - share) + intercept[i1] * share
            source[mid] = 2
    return out_a, out_b, source


def _paint_side(offs, zz, rr, inside, rows, cols, intercept, slope, source, best_off, best_z, best_src):
    filled = 0
    for k, dist in enumerate(offs):
        if dist > FILL_TO_M + 1e-9:
            continue
        use = inside[k] & np.isfinite(rr[k]) & (rr[k] >= DROP_ROUGH_M) & (source > 0) & np.isfinite(intercept)
        if not np.any(use):
            continue
        height = (intercept + slope * float(dist)).astype(np.float32)
        rr_i = rows[k, use]
        cc = cols[k, use]
        closer = dist < best_off[rr_i, cc]
        if not np.any(closer):
            continue
        rr_i = rr_i[closer]
        cc = cc[closer]
        best_off[rr_i, cc] = dist
        best_z[rr_i, cc] = height[use][closer]
        best_src[rr_i, cc] = source[use][closer]
        filled += int(closer.sum())
    return filled


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    raw_path = proc / "corridor50_raw" / "corridor50_raw.tif"
    gpkg_in = proc / "centerline_shift_taper" / "centerline_shift.gpkg"
    for path in (raw_path, gpkg_in):
        if not path.is_file():
            raise SystemExit(f"missing {path}")
    out_dir = proc / "dgm_repair_cross"
    out_dir.mkdir(parents=True, exist_ok=True)

    raw, spec = _load_geotiff(raw_path)
    rough = _deviation(raw)
    roads = gpd.read_file(gpkg_in, layer="carriageway")
    lines = gpd.read_file(gpkg_in, layer="centerline")
    print("preparing edges", flush=True)
    parts = _prepare(roads, lines)
    road = _road_mask(parts, raw.shape, spec)
    on_road = road & np.isfinite(raw) & np.isfinite(rough)
    before = _stats(rough, on_road)
    print(f"  {len(parts)} parts, hot {before['hot_px']}", flush=True)

    best_off = np.full(raw.shape, np.inf, dtype=np.float32)
    best_z = np.full(raw.shape, np.nan, dtype=np.float32)
    best_src = np.zeros(raw.shape, dtype=np.uint8)
    profile_rows = []
    measured = linear = parabola = 0

    for part in parts:
        for name, edge in part["sides"].items():
            xy, station, normals = _densify([(float(x), float(y)) for x, y in edge.coords], STATION_M)
            if len(xy) < 2 or np.allclose(normals, 0):
                continue
            inward = _inward(xy, normals, part["poly"])
            offs, zz, rr, inside, rows, cols = _rays(xy, inward, raw, rough, road, spec)
            intercept, slope, valid = _fit_station(offs, zz, rr, inside)
            intercept, slope, source = _close_gaps(station, intercept, slope, valid)
            _paint_side(offs, zz, rr, inside, rows, cols, intercept, slope, source, best_off, best_z, best_src)
            measured += int((source == 1).sum())
            linear += int((source == 2).sum())
            parabola += int((source == 3).sum())
            keep = source > 0
            for s, aa, bb, src in zip(station[keep], intercept[keep], slope[keep], source[keep]):
                if not np.isfinite(aa):
                    continue
                point = edge.interpolate(float(min(s, edge.length)))
                profile_rows.append(
                    (
                        Point(float(point.x), float(point.y)),
                        {
                            "objectid": part["oid"],
                            "side": name,
                            "s_m": round(float(s), 2),
                            "z_edge_m": round(float(aa), 3),
                            "slope": round(float(bb), 4),
                            "source": int(src),
                        },
                    )
                )
        print(f"  oid {part['oid']}", flush=True)

    working = raw.copy()
    take = on_road & np.isfinite(best_z) & (rough >= DROP_ROUGH_M)
    working[take] = best_z[take]
    after = _stats(_deviation(working), on_road)
    klass = np.zeros(raw.shape, dtype=np.uint8)
    klass[on_road & (rough < DROP_ROUGH_M)] = 1
    klass[on_road & (rough >= DROP_ROUGH_M) & ~take] = 2
    klass[take & (best_src == 1)] = 3
    klass[take & (best_src == 2)] = 4
    klass[take & (best_src == 3)] = 5
    repaired = np.where(on_road, working, np.nan).astype(np.float32)
    shown = np.where(on_road, _deviation(working), np.nan).astype(np.float32)
    write_referenced_tif(out_dir / "02_repaired.tif", repaired, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    write_referenced_tif(out_dir / "01_class.tif", klass, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    write_referenced_tif(out_dir / "03_roughness.tif", shown, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    if profile_rows:
        gpkg = out_dir / "profile.gpkg"
        if gpkg.exists():
            gpkg.unlink()
        gpd.GeoDataFrame(
            [row for _pt, row in profile_rows],
            geometry=[pt for pt, _row in profile_rows],
            crs="EPSG:31254",
        ).to_file(gpkg, layer="profile", driver="GPKG")
    delta = np.abs(working[take].astype(np.float64) - raw[take].astype(np.float64))
    report = {
        "heights": str(raw_path),
        "carriageway": str(gpkg_in),
        "drop_rough_m": DROP_ROUGH_M,
        "inner_lo_m": INNER_LO_M,
        "inner_hi_m": INNER_HI_M,
        "fill_to_m": FILL_TO_M,
        "short_gap_m": SHORT_GAP_M,
        "before": before,
        "after": after,
        "filled_px": int(take.sum()),
        "left_raw_px": int((klass == 2).sum()),
        "from_measured_px": int((klass == 3).sum()),
        "from_linear_px": int((klass == 4).sum()),
        "from_parabola_px": int((klass == 5).sum()),
        "stations_measured": measured,
        "stations_linear": linear,
        "stations_parabola": parabola,
        "max_delta_m": round(float(delta.max()) if len(delta) else 0.0, 4),
        "class": {
            "1": "passed on the raw DGM, height unchanged",
            "2": "rough edge pixel left at the raw height",
            "3": "filled from the cross-section measured at this station",
            "4": "filled from a cross-section interpolated linearly along the edge",
            "5": "filled from a parabolic height and a linear cross-slope along the edge",
        },
        "note": (
            "The cross-section is a straight grade fitted to good pixels "
            f"{INNER_LO_M:.1f}–{INNER_HI_M:.1f} m inside the edge. Only rough pixels "
            f"in the outer {FILL_TO_M:.1f} m are replaced. Gaps longer than "
            f"{SHORT_GAP_M:.0f} m use a parabola on the edge height."
        ),
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"before": before, "after": after, "filled_px": int(take.sum()), "left_raw_px": int((klass == 2).sum())}, indent=2))
    print(f"Wrote {out_dir}")


if __name__ == "__main__":
    main()
