"""Cross-section fill with a variable search and a blend where both edges meet.

The local grade 1.5 m inward of the first good pixel is kept when it is already
smooth. Otherwise the search moves further inward and to neighbouring stations,
and the smoothest single cross-slope wins. The search stops when the chord
between its ends leaves the edge by more than half a pixel, so it does not cut
the inside of a bend. The grade is tied to the first good pixel. Where both
sides claim a pixel, the height is mixed by distance to the two edges.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\repair_dgm_cross_var.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
from scipy.ndimage import uniform_filter
from shapely.geometry import Point

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_road_grid import RES_M, _load_geotiff  # noqa: E402
from filter_road_corridor import write_referenced_tif  # noqa: E402
from repair_dgm_cross_section import _inward  # noqa: E402
from repair_dgm_edge_profile import _densify  # noqa: E402
from repair_dgm_iterate import _deviation, _prepare, _road_mask, _stats  # noqa: E402
from repair_dgm_side_band import DROP_ROUGH_M  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

STATION_M = 0.5
RAY_STEP_M = 0.25
RAY_HI_M = 6.0
LOCAL_M = 1.5
LATERAL_SPANS = (1.5, 2.5, 4.0, 6.0)
SMOOTH_RMSE_M = 0.02
MAX_RMSE_M = 0.04
MAX_SLOPE = 0.25
MIN_SPAN_M = 0.5
CHORD_M = 0.25
SHORT_GAP_M = 20.0
PARABOLA_WINDOW_M = 40.0
HALF_WINDOWS = (1, 2, 4, 8, 12, 16, 24, 32, 48)


def _rays(xy, inward, z, rough, road, spec):
    offs = np.arange(0.0, RAY_HI_M + 1e-9, RAY_STEP_M)
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


def _max_k(xy: np.ndarray) -> np.ndarray:
    n = len(xy)
    max_k = np.zeros(n, dtype=np.int32)
    for k in HALF_WINDOWS:
        if n <= 2 * k:
            break
        i = np.arange(k, n - k)
        a = xy[i - k]
        b = xy[i + k]
        mid = xy[i]
        abx = b[:, 0] - a[:, 0]
        aby = b[:, 1] - a[:, 1]
        length = np.maximum(np.hypot(abx, aby), 1e-6)
        dist = np.abs((mid[:, 0] - a[:, 0]) * aby - (mid[:, 1] - a[:, 1]) * abx) / length
        ok = np.zeros(n, dtype=bool)
        ok[i] = dist <= CHORD_M
        max_k = np.where(ok, k, max_k)
    return max_k


def _chord(xy, i0: int, i1: int) -> float:
    if i1 <= i0 + 1:
        return 0.0
    a = xy[i0]
    b = xy[i1]
    mid = xy[(i0 + i1) // 2]
    abx = float(b[0] - a[0])
    aby = float(b[1] - a[1])
    length = max(float(np.hypot(abx, aby)), 1e-6)
    return abs((float(mid[0] - a[0])) * aby - (float(mid[1] - a[1])) * abx) / length


def _fit_span(offs, zz, good, span: float):
    has = good.any(axis=0)
    first = np.argmax(good, axis=0)
    n = zz.shape[1]
    idx = np.arange(n)
    xa = offs[first]
    za = zz[first, idx].astype(np.float64)
    x = offs.astype(np.float64)[:, None]
    in_win = good & has & (x + 1e-9 >= xa) & (x <= xa + span + 1e-9)
    count = in_win.sum(axis=0).astype(np.float64)
    dx = np.where(in_win, x - xa, 0.0)
    dz = np.where(in_win, zz.astype(np.float64) - za, 0.0)
    denom = (dx * dx).sum(axis=0)
    slope = np.full(n, np.nan)
    ok_den = denom > 1e-4
    slope[ok_den] = (dx * dz).sum(axis=0)[ok_den] / denom[ok_den]
    x_hi = np.where(in_win, x, -np.inf).max(axis=0)
    reach = x_hi - xa
    resid = np.where(in_win, dz - slope * dx, 0.0)
    rmse = np.sqrt((resid * resid).sum(axis=0) / np.maximum(count, 1.0))
    valid = (
        has
        & (count >= 3)
        & ok_den
        & (reach >= MIN_SPAN_M)
        & np.isfinite(slope)
        & (np.abs(slope) <= MAX_SLOPE)
        & (rmse <= MAX_RMSE_M)
    )
    return xa, za, np.where(valid, slope, np.nan), np.where(valid, rmse, np.nan), valid


def _choose_profiles(offs, zz, good, xy):
    n = zz.shape[1]
    slope = np.full(n, np.nan)
    rmse = np.full(n, np.inf)
    xa = np.full(n, np.nan)
    za = np.full(n, np.nan)
    source = np.zeros(n, dtype=np.uint8)
    for span in LATERAL_SPANS:
        xa_s, za_s, b_s, r_s, valid = _fit_span(offs, zz, good, span)
        smooth = valid & np.isfinite(r_s) & (r_s <= SMOOTH_RMSE_M)
        take_smooth = smooth & (source == 0)
        slope[take_smooth] = b_s[take_smooth]
        rmse[take_smooth] = r_s[take_smooth]
        xa[take_smooth] = xa_s[take_smooth]
        za[take_smooth] = za_s[take_smooth]
        source[take_smooth] = 1 if span == LOCAL_M else 2
        better = valid & (source == 0) & np.isfinite(r_s) & (r_s < rmse)
        # Remember the best not-yet-smooth fit; a later smooth window still replaces it.
        hold = better & (source == 0)
        slope[hold] = b_s[hold]
        rmse[hold] = r_s[hold]
        xa[hold] = xa_s[hold]
        za[hold] = za_s[hold]
    # source stays 0 until a smooth window wins or the neighbour search accepts the hold.
    held = (source == 0) & np.isfinite(slope)
    max_k = _max_k(xy)
    donor = np.flatnonzero((source > 0) & (rmse <= SMOOTH_RMSE_M))
    for i in np.flatnonzero(held):
        k = int(max_k[i])
        if k < 1 or len(donor) == 0:
            continue
        window = donor[(donor >= i - k) & (donor <= i + k)]
        if len(window) == 0:
            continue
        best = window[np.argmin(rmse[window])]
        if rmse[best] <= SMOOTH_RMSE_M and rmse[best] < rmse[i]:
            slope[i] = slope[best]
            source[i] = 2
    accept_hold = held & (source == 0) & (rmse <= MAX_RMSE_M)
    source[accept_hold] = 2
    drop = (source == 0) & ~accept_hold
    slope[drop] = np.nan
    return xa, za, slope, source


def _close_gaps(xy, station, xa, za, slope, source):
    valid = source > 0
    out_xa = xa.copy()
    out_za = za.copy()
    out_b = slope.copy()
    out_src = source.copy()
    idx = np.flatnonzero(valid)
    if len(idx) < 2:
        return out_xa, out_za, out_b, out_src
    edge_h = np.full(len(station), np.nan)
    edge_h[valid] = za[valid] - slope[valid] * xa[valid]
    for i0, i1 in zip(idx[:-1], idx[1:]):
        if i1 <= i0 + 1:
            continue
        if _chord(xy, int(i0), int(i1)) > CHORD_M:
            continue
        gap = float(station[i1] - station[i0])
        if gap < 1e-6:
            continue
        mid = np.arange(i0 + 1, i1)
        share = (station[mid] - station[i0]) / gap
        b = slope[i0] * (1.0 - share) + slope[i1] * share
        x_anchor = xa[i0] * (1.0 - share) + xa[i1] * share
        if gap <= SHORT_GAP_M:
            h = edge_h[i0] * (1.0 - share) + edge_h[i1] * share
            src = 3
        else:
            sel = valid & (station >= station[i0] - PARABOLA_WINDOW_M) & (station <= station[i1] + PARABOLA_WINDOW_M)
            if int(sel.sum()) >= 4 and _chord(xy, int(i0), int(i1)) <= CHORD_M:
                coef = np.polyfit(station[sel], edge_h[sel], 2)
                h = np.polyval(coef, station[mid])
                src = 4
            else:
                h = edge_h[i0] * (1.0 - share) + edge_h[i1] * share
                src = 3
        out_b[mid] = b
        out_xa[mid] = x_anchor
        out_za[mid] = h + b * x_anchor
        out_src[mid] = src
    return out_xa, out_za, out_b, out_src


def _propose(offs, rr, inside, rows, cols, xa, za, slope, source, slot):
    z_out, d_out, s_out = slot
    for k, dist in enumerate(offs):
        use = (
            inside[k]
            & np.isfinite(rr[k])
            & (rr[k] >= DROP_ROUGH_M)
            & (source > 0)
            & np.isfinite(za)
            & np.isfinite(slope)
            & np.isfinite(xa)
            & (dist + 1e-6 < xa)
        )
        if not np.any(use):
            continue
        height = (za + slope * (float(dist) - xa)).astype(np.float32)
        rr_i = rows[k, use]
        cc = cols[k, use]
        dd = np.full(rr_i.shape, dist, dtype=np.float32)
        closer = dd < d_out[rr_i, cc]
        if not np.any(closer):
            continue
        z_out[rr_i[closer], cc[closer]] = height[use][closer]
        d_out[rr_i[closer], cc[closer]] = dd[closer]
        s_out[rr_i[closer], cc[closer]] = source[use][closer]


def _road_only(z, road):
    valid = road & np.isfinite(z)
    weight = valid.astype(np.float32)
    filled = np.where(valid, z, 0).astype(np.float32)
    count = uniform_filter(weight, size=3, mode="constant")
    mean = uniform_filter(filled, size=3, mode="constant")
    enough = count >= 6.0 / 9.0
    local = np.divide(mean, count, out=np.full_like(mean, np.nan), where=enough)
    rough = np.abs(np.asarray(z, dtype=np.float32) - local).astype(np.float32)
    rough[~enough | ~np.isfinite(z)] = np.nan
    return rough


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    raw_path = proc / "corridor50_raw" / "corridor50_raw.tif"
    gpkg_in = proc / "centerline_shift_taper" / "centerline_shift.gpkg"
    for path in (raw_path, gpkg_in):
        if not path.is_file():
            raise SystemExit(f"missing {path}")
    out_dir = proc / "dgm_repair_cross_var"
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

    proposal = {
        name: (
            np.full(raw.shape, np.nan, dtype=np.float32),
            np.full(raw.shape, np.inf, dtype=np.float32),
            np.zeros(raw.shape, dtype=np.uint8),
        )
        for name in ("left", "right")
    }
    profile_rows = []
    for part in parts:
        for name, edge in part["sides"].items():
            xy, station, normals = _densify([(float(x), float(y)) for x, y in edge.coords], STATION_M)
            if len(xy) < 2 or np.allclose(normals, 0):
                continue
            inward = _inward(xy, normals, part["poly"])
            offs, zz, rr, inside, rows, cols = _rays(xy, inward, raw, rough, road, spec)
            good = inside & np.isfinite(zz) & np.isfinite(rr) & (rr < DROP_ROUGH_M)
            xa, za, slope, source = _choose_profiles(offs, zz, good, xy)
            xa, za, slope, source = _close_gaps(xy, station, xa, za, slope, source)
            _propose(offs, rr, inside, rows, cols, xa, za, slope, source, proposal[name])
            keep = source > 0
            for s, aa, bb, src in zip(station[keep], za[keep] - slope[keep] * xa[keep], slope[keep], source[keep]):
                if not np.isfinite(aa) or not np.isfinite(bb):
                    continue
                point = edge.interpolate(float(min(max(s, 0.0), edge.length)))
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

    zl, dl, sl = proposal["left"]
    zr, dr, sr = proposal["right"]
    have_l = np.isfinite(zl)
    have_r = np.isfinite(zr)
    both = on_road & (rough >= DROP_ROUGH_M) & have_l & have_r
    only_l = on_road & (rough >= DROP_ROUGH_M) & have_l & ~have_r
    only_r = on_road & (rough >= DROP_ROUGH_M) & have_r & ~have_l
    dl_w = np.maximum(dl, 0.05)
    dr_w = np.maximum(dr, 0.05)
    working = raw.copy()
    working[only_l] = zl[only_l]
    working[only_r] = zr[only_r]
    working[both] = ((zl[both] * dr_w[both] + zr[both] * dl_w[both]) / (dl_w[both] + dr_w[both])).astype(np.float32)
    take = only_l | only_r | both
    after = _stats(_deviation(working), on_road)
    hybrid = raw.copy()
    hybrid[on_road] = working[on_road]
    road_after = _stats(_road_only(hybrid, on_road), on_road)
    road_before = _stats(_road_only(raw, on_road), on_road)

    klass = np.zeros(raw.shape, dtype=np.uint8)
    klass[on_road & (rough < DROP_ROUGH_M)] = 1
    klass[on_road & (rough >= DROP_ROUGH_M) & ~take] = 2
    klass[only_l & (sl == 1)] = 3
    klass[only_r & (sr == 1)] = 3
    klass[only_l & (sl == 2)] = 4
    klass[only_r & (sr == 2)] = 4
    klass[only_l & (sl == 3)] = 5
    klass[only_r & (sr == 3)] = 5
    klass[only_l & (sl == 4)] = 6
    klass[only_r & (sr == 4)] = 6
    klass[both] = 7
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
        "smooth_rmse_m": SMOOTH_RMSE_M,
        "chord_m": CHORD_M,
        "before_full_window": before,
        "after_full_window": after,
        "before_road_only": road_before,
        "after_road_only": road_after,
        "filled_px": int(take.sum()),
        "blended_px": int(both.sum()),
        "left_raw_px": int((klass == 2).sum()),
        "local_px": int((klass == 3).sum()),
        "searched_px": int((klass == 4).sum()),
        "linear_gap_px": int((klass == 5).sum()),
        "parabola_gap_px": int((klass == 6).sum()),
        "max_delta_m": round(float(delta.max()) if len(delta) else 0.0, 4),
        "delta_p50_m": round(float(np.percentile(delta, 50)) if len(delta) else 0.0, 4),
        "delta_p99_m": round(float(np.percentile(delta, 99)) if len(delta) else 0.0, 4),
        "class": {
            "1": "passed on the raw DGM, height unchanged",
            "2": "rough pixel left at the raw height",
            "3": "filled from the local smooth cross-section, tied to the first good pixel",
            "4": "filled from a smoother cross-slope found inward or along the edge",
            "5": "gap along the edge, linear, chord stays inside the corner limit",
            "6": "gap along the edge, parabolic height",
            "7": "both edges claim the pixel, height mixed by distance",
        },
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("before_full_window", "after_full_window", "before_road_only", "after_road_only", "filled_px", "blended_px", "left_raw_px", "max_delta_m", "delta_p50_m", "delta_p99_m")}, indent=2))
    print(f"Wrote {out_dir}")


if __name__ == "__main__":
    main()
