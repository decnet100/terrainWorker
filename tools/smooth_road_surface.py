"""Split the repaired road DGM into a smooth road model and a roughness layer.

A road is a smooth surface by construction: a vertical alignment along the
axis made of grades and long vertical curves, and a cross-section per station
whose crossfall changes slowly along the road. This script fits that model to
every carriageway and keeps only what the model and a gentle low-pass of the
remainder explain. Everything shorter than the cutoff wavelength along the
road, and every outlier such as a parked vehicle, ends up in a residual raster
instead of the driving surface.

Per carriageway (one ``objectid``):

* every road pixel gets a station ``s`` on the axis and a signed offset ``t``
  (left positive);
* the model ``z = a(s) + b_L(s) * max(t, 0) + b_R(s) * min(t, 0)`` is solved for
  all stations at once as a penalised least-squares problem. The penalty is on
  the second difference along the station (a Whittaker smoother). Unlike a
  Gaussian, this reproduces parabolas, so crest and sag curves keep their
  height; only waves shorter than the cutoff are attenuated;
* pixels far from the model lose weight (Tukey biweight, 3 to 4 passes) and
  drop out. Nothing is masked by a roughness threshold beforehand;
* the remainder ``z - model`` is low-passed in road coordinates, long along
  the road and short across, with the same weights. That keeps bays, turn
  lanes and widenings the two-line cross-section does not know;
* output ``z_out = model + low-passed remainder``. The removed layer
  ``z_in - z_out`` is written as its own raster and can be re-applied later,
  scaled and masked, as a controlled roughness layer.

Structure spans (bridge, tunnel, gallery) and pixels farther than
``MAX_HALF_M`` from the axis keep the input height. Where carriageways overlap
(junctions, parallel roads), each model is weighted by the distance to its own
polygon edge, so the mix is continuous across that edge. A crossfall steeper
than ``MAX_CROSSFALL`` is an embankment inside a too-wide polygon and is
clamped. Nothing is written into a BeamNG level.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\smooth_road_surface.py

Then the mesh from the smooth raster:

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\build_road_grid.py --heights "data\\processed\\tirol-imst-tarrenz-8192\\road_surface_smooth\\04_smooth.tif" --clip "data\\processed\\tirol-imst-tarrenz-8192\\dgm_repair_transect\\carriageway_bridged.gpkg" --clip-layer carriageway
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import warnings
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import scipy.sparse as sp
import shapely
from scipy.ndimage import gaussian_filter, map_coordinates
from scipy.sparse.linalg import spsolve

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_road_grid import RES_M, _load_geotiff  # noqa: E402
from filter_road_corridor import write_referenced_tif  # noqa: E402
from repair_dgm_cross_var import _road_only  # noqa: E402
from repair_dgm_edge_profile import _crop_inside, _densify  # noqa: E402
from repair_dgm_iterate import _deviation, _stats  # noqa: E402
from repair_dgm_side_band import DROP_ROUGH_M  # noqa: E402
from repair_dgm_transect import _line_of, _structure_mask, _zones  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

STATION_M = 0.5
# Along-track cutoff wavelength (half power) of the axis height. A vertical
# curve of R = 250 m keeps its height; a 4 m pothole does not.
CUTOFF_AXIS_M = 10.0
# The crossfall of each half changes over tens of metres (superelevation run-off).
CUTOFF_SLOPE_M = 30.0
# Sigma of the kept remainder, in road coordinates. Long along, short across.
RESID_ALONG_M = 2.0
RESID_ACROSS_M = 1.0
OFFSET_BIN_M = 0.5
MAX_HALF_M = 8.0
# Tukey biweight: weight 0 beyond TUKEY_C * sigma. Sigma comes from the MAD of
# the remainder, floored so a very clean road does not reject 1 cm texture.
TUKEY_C = 4.685
SIGMA_FLOOR_M = 0.010
SIGMA_CAP_M = 0.050
ITERATIONS = 4
# The robust fit has to keep a consensus. If most support pixels lose their
# weight, the polygon does not hold a road surface (rock cut, misplaced stub);
# the carriageway keeps its input height. A wide sigma alone is not a reason:
# a too-wide polygon with an embankment still holds a road in its middle.
MIN_FIT_SHARE = 0.35
# The remainder is kept with its own biweight on a physical scale: a 5 cm
# one-lane overlay stays, a vehicle or a wall does not. Beyond KEEP_M the
# pixel is an outlier and takes the model plus the neighbours' remainder.
KEEP_M = 0.08
# The outer metre of the carriageway is mixed with the shoulder and is not a
# support of the cross-section (build_road_grid refills that band anyway).
# It still receives an output height.
EDGE_SUPPORT_M = 1.0
MIN_SUPPORT_SHARE = 0.3
# A crossfall beyond this is an embankment inside a too-wide polygon, not road.
MAX_CROSSFALL = 0.15
# Where carriageways overlap, each model is weighted by the distance to its own
# polygon edge, so the weight is continuous across that edge.
BLEND_REACH_M = 2.0
# Wheel paths for the ride metric, offsets from the axis in metres.
WHEEL_OFFSETS_M = (-2.5, -1.0, 1.0, 2.5)
BUMP_STEPS_M = (0.005, 0.010)
PLOT_OID = 6975
PLOT_FOOT = (28280.0, 238269.0)
PLOT_SPAN_M = 60.0
OUT_DIR_NAME = "road_surface_smooth"


def _lambda(cutoff_m: float) -> float:
    """Whittaker penalty for a half-power wavelength, in station units."""
    n = cutoff_m / STATION_M
    return (n / (2.0 * math.pi)) ** 4


def _pixels(polys, z: np.ndarray, spec: dict, skip: np.ndarray):
    rows_all = []
    cols_all = []
    for poly in polys:
        got = _crop_inside(poly, spec)
        if got is None:
            continue
        inside, r0, c0 = got
        rr, cc = np.nonzero(inside)
        rows_all.append(rr + r0)
        cols_all.append(cc + c0)
    if not rows_all:
        return None
    rows = np.concatenate(rows_all)
    cols = np.concatenate(cols_all)
    flat = np.unique(rows.astype(np.int64) * z.shape[1] + cols)
    rows = (flat // z.shape[1]).astype(np.int32)
    cols = (flat % z.shape[1]).astype(np.int32)
    keep = np.isfinite(z[rows, cols]) & ~skip[rows, cols]
    rows = rows[keep]
    cols = cols[keep]
    if len(rows) == 0:
        return None
    x = spec["xmin"] + (cols + 0.5) * RES_M
    y = spec["ymax"] - (rows + 0.5) * RES_M
    return rows, cols, x, y, z[rows, cols].astype(np.float64)


def _axis(line):
    xy_d, s_d, n_d = _densify([(float(x), float(y)) for x, y in line.coords], STATION_M)
    return xy_d, s_d, n_d


def _normal_at(s, s_d, n_d):
    nx = np.interp(s, s_d, n_d[:, 0])
    ny = np.interp(s, s_d, n_d[:, 1])
    norm = np.maximum(np.hypot(nx, ny), 1e-9)
    return nx / norm, ny / norm


def _locate(line, x, y, s_d, n_d):
    """Station, signed offset and, beyond the axis ends, the overshoot along the tangent."""
    pts = shapely.points(x, y)
    s = np.asarray(shapely.line_locate_point(line, pts), dtype=np.float64)
    foot = shapely.line_interpolate_point(line, s)
    fx = np.asarray(shapely.get_x(foot), dtype=np.float64)
    fy = np.asarray(shapely.get_y(foot), dtype=np.float64)
    nx, ny = _normal_at(s, s_d, n_d)
    t = (x - fx) * nx + (y - fy) * ny
    # Tangent in the direction of travel is the normal turned right.
    along = (x - fx) * ny - (y - fy) * nx
    length = float(line.length)
    at_end = (s <= 1e-6) | (s >= length - 1e-6)
    e = np.where(at_end, along, 0.0)
    return s, t, e


def _design(s, t, n_st):
    u = np.clip(s / STATION_M, 0.0, n_st - 1.0 - 1e-9)
    i0 = np.floor(u).astype(np.int64)
    f = u - i0
    t_plus = np.maximum(t, 0.0)
    t_minus = np.minimum(t, 0.0)
    n = len(s)
    rows = np.repeat(np.arange(n), 6)
    cols = np.empty(n * 6, dtype=np.int64)
    vals = np.empty(n * 6, dtype=np.float64)
    base0 = 3 * i0
    base1 = 3 * (i0 + 1)
    cols[0::6] = base0
    cols[1::6] = base0 + 1
    cols[2::6] = base0 + 2
    cols[3::6] = base1
    cols[4::6] = base1 + 1
    cols[5::6] = base1 + 2
    vals[0::6] = 1.0 - f
    vals[1::6] = (1.0 - f) * t_plus
    vals[2::6] = (1.0 - f) * t_minus
    vals[3::6] = f
    vals[4::6] = f * t_plus
    vals[5::6] = f * t_minus
    return sp.csr_matrix((vals, (rows, cols)), shape=(n, 3 * n_st)), u, t_plus, t_minus


def _penalty(n_st, lam):
    m = n_st - 2
    blocks = []
    for k in range(3):
        i = np.arange(m)
        rows = np.repeat(i, 3)
        cols = np.empty(m * 3, dtype=np.int64)
        cols[0::3] = 3 * i + k
        cols[1::3] = 3 * (i + 1) + k
        cols[2::3] = 3 * (i + 2) + k
        vals = np.tile(np.array([1.0, -2.0, 1.0]), m)
        d2 = sp.csr_matrix((vals, (rows, cols)), shape=(m, 3 * n_st))
        blocks.append(lam[k] * (d2.T @ d2))
    return blocks[0] + blocks[1] + blocks[2]


def _fit(design, w, z, n_st, t_plus, t_minus):
    """Weighted penalised least squares for (a, b_L, b_R) at every station."""
    per_station = max(1.0, len(z) / n_st)
    wn = w / per_station
    # Scale the slope penalties so the cutoff holds for a unit data weight per
    # station: the slope columns carry t^2, the axis column carries 1.
    m_plus = max(float(np.sum(wn * t_plus * t_plus)) / n_st, 0.1)
    m_minus = max(float(np.sum(wn * t_minus * t_minus)) / n_st, 0.1)
    lam = (_lambda(CUTOFF_AXIS_M), _lambda(CUTOFF_SLOPE_M) * m_plus, _lambda(CUTOFF_SLOPE_M) * m_minus)
    weighted = design.T @ sp.diags(wn)
    normal = (weighted @ design) + _penalty(n_st, lam) + sp.identity(3 * n_st) * 1e-8
    rhs = weighted @ z
    sol = spsolve(normal.tocsc(), rhs)
    return np.asarray(sol, dtype=np.float64).reshape(n_st, 3)


def _model_at(params, u, t_plus, t_minus, e):
    i0 = np.floor(u).astype(np.int64)
    f = u - i0
    p = params[i0] * (1.0 - f)[:, None] + params[i0 + 1] * f[:, None]
    z = p[:, 0] + p[:, 1] * t_plus + p[:, 2] * t_minus
    # Pixels past the axis ends continue on the end grade of the axis height.
    n = len(params)
    grade0 = (params[1, 0] - params[0, 0]) / STATION_M
    grade1 = (params[n - 1, 0] - params[n - 2, 0]) / STATION_M
    return z + e * np.where(u < 0.5 * (n - 1), grade0, grade1)


def _keep_weight(r):
    """Biweight with a fixed physical scale for the kept remainder. Zero at KEEP_M."""
    v = np.abs(r) / KEEP_M
    w = np.where(v < 1.0, (1.0 - v * v) ** 2, 0.0)
    w[~np.isfinite(r)] = 0.0
    return w


def _tukey(r):
    finite = np.isfinite(r)
    if not np.any(finite):
        return np.zeros_like(r), SIGMA_FLOOR_M
    sigma = 1.4826 * float(np.median(np.abs(r[finite])))
    sigma = min(max(sigma, SIGMA_FLOOR_M), SIGMA_CAP_M)
    v = np.abs(r) / (TUKEY_C * sigma)
    w = np.where(v < 1.0, (1.0 - v * v) ** 2, 0.0)
    w[~finite] = 0.0
    return w, sigma


def _bin_grid(u, t, values, w, n_st):
    n_t = int(round(2.0 * MAX_HALF_M / OFFSET_BIN_M)) + 1
    i = np.clip(np.rint(u).astype(np.int64), 0, n_st - 1)
    j = np.clip(np.rint((t + MAX_HALF_M) / OFFSET_BIN_M).astype(np.int64), 0, n_t - 1)
    flat = i * n_t + j
    num = np.bincount(flat, weights=w * values, minlength=n_st * n_t).reshape(n_st, n_t)
    den = np.bincount(flat, weights=w, minlength=n_st * n_t).reshape(n_st, n_t)
    return num, den


def _kept_remainder(u, t, r, w, n_st):
    """Low-pass of the remainder in (station, offset), weighted, sampled back."""
    num, den = _bin_grid(u, t, r, w, n_st)
    sig = (RESID_ALONG_M / STATION_M, RESID_ACROSS_M / OFFSET_BIN_M)
    num_f = gaussian_filter(num, sig, mode="constant", cval=0.0)
    den_f = gaussian_filter(den, sig, mode="constant", cval=0.0)
    grid = np.where(den_f > 0.05, num_f / np.maximum(den_f, 1e-12), 0.0)
    coords = np.vstack([u, (t + MAX_HALF_M) / OFFSET_BIN_M])
    lp = map_coordinates(grid, coords, order=1, mode="nearest")
    return np.asarray(lp, dtype=np.float64), grid


def _process(oid, line, polys, z, spec, skip, acc, want_capture):
    got = _pixels(polys, z, spec, skip)
    if got is None or line.length < 2.0 * STATION_M:
        return None
    rows, cols, x, y, zin = got
    xy_d, s_d, n_d = _axis(line)
    n_st = max(4, int(math.ceil(line.length / STATION_M)) + 1)
    s, t, e = _locate(line, x, y, s_d, n_d)
    near = np.abs(t) <= MAX_HALF_M
    if int(near.sum()) < 12:
        return None
    rows, cols, x, y, zin, s, t, e = (a[near] for a in (rows, cols, x, y, zin, s, t, e))
    design, u, t_plus, t_minus = _design(s, t, n_st)
    boundary = shapely.union_all([p.boundary for p in polys])
    to_edge = np.asarray(shapely.distance(boundary, shapely.points(x, y)), dtype=np.float64)
    support = to_edge >= EDGE_SUPPORT_M
    if float(np.mean(support)) < MIN_SUPPORT_SHARE:
        support = np.ones(len(zin), dtype=bool)
    w_fit = support.astype(np.float64)
    sigma = SIGMA_FLOOR_M
    params = None
    for _ in range(ITERATIONS):
        params = _fit(design, w_fit, zin, n_st, t_plus, t_minus)
        zm = _model_at(params, u, t_plus, t_minus, e)
        r = zin - zm
        w_fit, sigma = _tukey(np.where(support, r, np.nan))
    fit_share = float(np.mean(w_fit[support] > 0.0)) if np.any(support) else 0.0
    if fit_share < MIN_FIT_SHARE:
        return {
            "oid": int(oid),
            "unfit": True,
            "px": int(len(zin)),
            "sigma_m": round(float(sigma), 4),
            "fit_share": round(fit_share, 3),
            "z_span_m": round(float(zin.max() - zin.min()), 2),
            "axis": (xy_d, s_d, n_d),
        }
    clamped = int(np.count_nonzero(np.abs(params[:, 1:3]) > MAX_CROSSFALL))
    if clamped:
        params[:, 1:3] = np.clip(params[:, 1:3], -MAX_CROSSFALL, MAX_CROSSFALL)
        zm = _model_at(params, u, t_plus, t_minus, e)
        r = zin - zm
    w = _keep_weight(r)
    lp, lp_grid = _kept_remainder(u, t, r, w, n_st)
    zout = zm + lp
    blend = np.minimum(to_edge, BLEND_REACH_M) ** 2 + 1e-3
    np.add.at(acc["num"], (rows, cols), blend * zout)
    np.add.at(acc["den"], (rows, cols), blend)
    np.add.at(acc["wsum"], (rows, cols), blend * w)
    np.minimum.at(acc["lo"], (rows, cols), zout)
    np.maximum.at(acc["hi"], (rows, cols), zout)
    out = {
        "oid": int(oid),
        "px": int(len(zin)),
        "outliers": int(np.count_nonzero(w <= 0.0)),
        "sigma_m": round(float(sigma), 4),
        "support_share": round(float(np.mean(support)), 3),
        "fit_share": round(fit_share, 3),
        "clamped_half_stations": clamped,
        "stations": int(n_st),
        "axis": (xy_d, s_d, n_d),
    }
    if want_capture:
        out["capture"] = {
            "s": s,
            "t": t,
            "x": x,
            "y": y,
            "u": u,
            "z_in": zin,
            "model": zm,
            "kept": lp,
            "z_out": zout,
            "w": w,
            "w_fit": w_fit,
            "params": params,
            "n_st": n_st,
            "lp_grid": lp_grid,
        }
    return out


def _bilinear(arr, spec, x, y):
    col = (x - spec["xmin"]) / RES_M - 0.5
    row = (spec["ymax"] - y) / RES_M - 0.5
    c0 = np.floor(col).astype(np.int64)
    r0 = np.floor(row).astype(np.int64)
    h, wd = arr.shape
    ok = (c0 >= 0) & (r0 >= 0) & (c0 + 1 < wd) & (r0 + 1 < h)
    out = np.full(len(x), np.nan, dtype=np.float64)
    if not np.any(ok):
        return out
    tc = col[ok] - c0[ok]
    tr = row[ok] - r0[ok]
    a = arr[r0[ok], c0[ok]]
    b = arr[r0[ok], c0[ok] + 1]
    c = arr[r0[ok] + 1, c0[ok]]
    d = arr[r0[ok] + 1, c0[ok] + 1]
    out[ok] = a * (1 - tc) * (1 - tr) + b * tc * (1 - tr) + c * (1 - tc) * tr + d * tc * tr
    return out


def _wheel_paths(axes, z_in, z_out, spec, road):
    """|second difference| of the height along each wheel path, before and after."""
    before = []
    after = []
    for xy_d, s_d, n_d in axes:
        n_st = int(math.floor(float(s_d[-1]) / STATION_M)) + 1
        if n_st < 5:
            continue
        s = np.arange(n_st) * STATION_M
        cx = np.interp(s, s_d, xy_d[:, 0])
        cy = np.interp(s, s_d, xy_d[:, 1])
        nx, ny = _normal_at(s, s_d, n_d)
        for off in WHEEL_OFFSETS_M:
            x = cx + off * nx
            y = cy + off * ny
            cc = np.clip(np.floor((x - spec["xmin"]) / RES_M).astype(np.int64), 0, road.shape[1] - 1)
            rr = np.clip(np.floor((spec["ymax"] - y) / RES_M).astype(np.int64), 0, road.shape[0] - 1)
            on = road[rr, cc]
            for src, sink in ((z_in, before), (z_out, after)):
                v = _bilinear(src, spec, x, y)
                v[~on] = np.nan
                d2 = v[:-2] - 2.0 * v[1:-1] + v[2:]
                d2 = d2[np.isfinite(d2)]
                if d2.size:
                    sink.append(np.abs(d2))
    before = np.concatenate(before) if before else np.zeros(0)
    after = np.concatenate(after) if after else np.zeros(0)
    return before, after


def _dist_stats(v: np.ndarray) -> dict:
    if v.size == 0:
        return {"n": 0}
    out = {
        "n": int(v.size),
        "p50_m": round(float(np.percentile(v, 50)), 4),
        "p90_m": round(float(np.percentile(v, 90)), 4),
        "p99_m": round(float(np.percentile(v, 99)), 4),
        "max_m": round(float(v.max()), 4),
    }
    for step in BUMP_STEPS_M:
        out[f"share_over_{step * 100:.1f}cm"] = round(float(np.mean(v >= step)), 5)
    return out


def _plot_along(cap, oid, out_dir: Path) -> str:
    s = cap["s"]
    lane = np.abs(cap["t"] - 1.0) <= 0.25
    idx = np.flatnonzero(lane)
    idx = idx[np.argsort(s[idx])]
    s_all = s[idx]
    if s_all.size == 0:
        return ""
    if float(s_all[-1] - s_all[0]) > 2.0 * PLOT_SPAN_M:
        # Window around the pixel nearest the reference foot point.
        fx, fy = PLOT_FOOT
        near = int(np.argmin((cap["x"] - fx) ** 2 + (cap["y"] - fy) ** 2))
        s0 = float(s[near]) if np.hypot(cap["x"][near] - fx, cap["y"][near] - fy) < 30.0 else float(np.median(s_all))
        idx = idx[np.abs(s_all - s0) <= PLOT_SPAN_M]
    st = s[idx]
    # The grade hides centimetres. Show heights minus the straight line through
    # the output over this window; the line is the same for all three curves.
    trend = np.polyval(np.polyfit(st, cap["z_out"][idx], 1), st)
    fig, (ax0, ax1) = plt.subplots(2, 1, figsize=(9.0, 6.0), sharex=True, height_ratios=[2.2, 1.0])
    ax0.plot(st, (cap["z_in"][idx] - trend) * 100.0, color="#8a8f98", linewidth=0.9, label="input DGM")
    ax0.plot(st, (cap["model"][idx] - trend) * 100.0, color="#1f77b4", linewidth=1.0, linestyle="--", label="road model")
    ax0.plot(st, (cap["z_out"][idx] - trend) * 100.0, color="#0b3d91", linewidth=1.5, label="output surface")
    out = cap["w"][idx] <= 0.0
    if np.any(out):
        ax0.scatter(st[out], (cap["z_in"][idx][out] - trend[out]) * 100.0, s=10, color="#c62828", zorder=4, label=f"rejected pixel (> {KEEP_M * 100:.0f} cm off the model)")
    grade = float(np.polyfit(st, cap["z_out"][idx], 1)[0]) * 100.0
    ax0.set_ylabel("height minus linear trend [cm]")
    ax0.set_title(f"Wheel path, 1.0 m left of the axis, objectid {oid}, grade {grade:+.1f} %")
    ax0.legend(frameon=False, fontsize=8, loc="best")
    removed = (cap["z_in"][idx] - cap["z_out"][idx]) * 100.0
    ax1.axhline(0.0, color="#c5c9d0", linewidth=0.6)
    ax1.plot(st, removed, color="#c62828", linewidth=0.9, label="removed layer (input - output)")
    ax1.plot(st, cap["kept"][idx] * 100.0, color="#2e7d32", linewidth=0.9, label="kept remainder (output - model)")
    ax1.set_ylim(-8, 8)
    ax1.set_ylabel("[cm]")
    ax1.set_xlabel("station along the axis [m]")
    ax1.legend(frameon=False, fontsize=8, loc="best")
    fig.tight_layout()
    path = out_dir / f"along_wheelpath_oid{oid}.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return str(path)


def _plot_cross(cap, oid, out_dir: Path) -> str:
    s = cap["s"]
    # Station with the most rejected pixels, else the middle of the axis.
    n_st = cap["n_st"]
    i = np.clip(np.rint(cap["u"]).astype(np.int64), 0, n_st - 1)
    rejected = np.bincount(i, weights=(cap["w"] <= 0.0).astype(float), minlength=n_st)
    pick = int(np.argmax(rejected)) if rejected.max() > 0 else n_st // 2
    sel = np.flatnonzero(np.abs(cap["u"] - pick) <= 0.5)
    if sel.size < 4:
        return ""
    order = np.argsort(cap["t"][sel])
    sel = sel[order]
    t = cap["t"][sel]
    base = float(np.median(cap["model"][sel]))
    fig, ax = plt.subplots(figsize=(7.0, 4.2))
    w = cap["w"][sel]
    ok = w > 0.0
    ax.scatter(t[ok], (cap["z_in"][sel][ok] - base) * 100.0, s=14, color="#8a8f98", label="input pixel")
    if np.any(~ok):
        ax.scatter(t[~ok], (cap["z_in"][sel][~ok] - base) * 100.0, s=16, color="#c62828", label=f"rejected pixel (> {KEEP_M * 100:.0f} cm off the model)")
    ax.plot(t, (cap["model"][sel] - base) * 100.0, color="#1f77b4", linestyle="--", linewidth=1.0, label="road model (two-line crossfall)")
    ax.plot(t, (cap["z_out"][sel] - base) * 100.0, color="#0b3d91", linewidth=1.5, label="output surface")
    ax.axvline(0.0, color="#c5c9d0", linewidth=0.6)
    ax.set_xlabel("offset from the axis [m], left positive")
    ax.set_ylabel(f"height above {base:.2f} m [cm]")
    ax.set_title(f"Cross-section at station {pick * STATION_M:.1f} m, objectid {oid}")
    ax.legend(frameon=False, fontsize=8, loc="best")
    fig.tight_layout()
    path = out_dir / f"cross_section_oid{oid}.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return str(path)


def _plot_unrolled(cap, oid, out_dir: Path) -> str:
    n_st = cap["n_st"]
    ones = np.ones(len(cap["u"]))
    num_in, den = _bin_grid(cap["u"], cap["t"], cap["z_in"] - cap["model"], ones, n_st)
    num_out, _ = _bin_grid(cap["u"], cap["t"], cap["z_out"] - cap["model"], ones, n_st)
    num_rm, _ = _bin_grid(cap["u"], cap["t"], cap["z_in"] - cap["z_out"], ones, n_st)
    have = den > 0
    panels = []
    for num in (num_in, num_out, num_rm):
        g = np.where(have, num / np.maximum(den, 1e-12), np.nan) * 100.0
        panels.append(g.T[::-1])  # offset on the vertical axis, left at the top
    extent = (0.0, n_st * STATION_M, -MAX_HALF_M, MAX_HALF_M)
    titles = (
        "input minus road model",
        "output minus road model (kept remainder)",
        "removed layer (input minus output)",
    )
    length_m = n_st * STATION_M
    width_in = min(14.0, max(7.0, length_m / 12.0))
    fig, axes = plt.subplots(3, 1, figsize=(width_in, 6.6), sharex=True)
    for ax, g, title in zip(axes, panels, titles):
        im = ax.imshow(g, extent=extent, aspect="auto", cmap="RdBu_r", vmin=-5.0, vmax=5.0, interpolation="nearest")
        ax.set_ylim(-5.5, 5.5)
        ax.set_ylabel("offset [m]")
        ax.set_title(title, fontsize=10, loc="left")
    axes[-1].set_xlabel("station along the axis [m]")
    cbar = fig.colorbar(im, ax=axes, fraction=0.03, pad=0.02)
    cbar.set_label("[cm]")
    fig.suptitle(f"Carriageway unrolled to road coordinates, objectid {oid}", fontsize=11)
    path = out_dir / f"unrolled_oid{oid}.png"
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return str(path)


def _ccdf(ax, values, label, color):
    v = np.sort(values[np.isfinite(values)])
    if v.size == 0:
        return
    share = 1.0 - np.arange(v.size) / v.size
    ax.plot(v * 100.0, share, color=color, linewidth=1.4, label=label)


def _plot_roughness(before, after, out_dir: Path) -> str:
    fig, ax = plt.subplots(figsize=(7.0, 4.2))
    _ccdf(ax, before, "before (repaired DGM)", "#8a8f98")
    _ccdf(ax, after, "after (smooth surface)", "#0b3d91")
    ax.axvline(DROP_ROUGH_M * 100.0, color="#c62828", linewidth=0.8, linestyle="--", label=f"threshold {DROP_ROUGH_M * 100:.1f} cm")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(0.1, 100.0)
    ax.set_ylim(1e-6, 1.0)
    ax.set_xlabel("deviation from the local 1.5 m mean [cm]")
    ax.set_ylabel("share of road pixels at or above")
    ax.set_title("Road-pixel roughness, before and after")
    ax.grid(True, which="both", linewidth=0.3, alpha=0.5)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    path = out_dir / "roughness_before_after.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return str(path)


def _plot_wheel(before, after, out_dir: Path) -> str:
    fig, ax = plt.subplots(figsize=(7.0, 4.2))
    _ccdf(ax, before, "before (repaired DGM)", "#8a8f98")
    _ccdf(ax, after, "after (smooth surface)", "#0b3d91")
    for step, ls in zip(BUMP_STEPS_M, ("--", ":")):
        ax.axvline(step * 100.0, color="#c62828", linewidth=0.8, linestyle=ls, label=f"{step * 100:.1f} cm")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(0.01, 100.0)
    ax.set_ylim(1e-6, 1.0)
    ax.set_xlabel(f"|second difference| along the wheel path, {STATION_M} m step [cm]")
    ax.set_ylabel("share of stations at or above")
    ax.set_title("Wheel-path ride metric, offsets " + ", ".join(f"{o:+.1f}" for o in WHEEL_OFFSETS_M) + " m")
    ax.grid(True, which="both", linewidth=0.3, alpha=0.5)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    path = out_dir / "wheelpath_second_difference.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return str(path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--heights", type=Path, default=None, help="input raster, default 03_with_bridges.tif")
    ap.add_argument("--oid", type=int, action="append", default=None, help="process only these objectids")
    ap.add_argument("--plot-oid", type=int, action="append", default=None, help="objectids to plot, default 6975")
    args = ap.parse_args()

    site = load_site()
    proc = processed_dir(site)
    repair = proc / "dgm_repair_transect"
    heights = args.heights
    if heights is None:
        heights = repair / "03_with_bridges.tif"
        if not heights.is_file():
            heights = repair / "02_repaired.tif"
    carriage = repair / "carriageway_bridged.gpkg"
    if not carriage.is_file():
        carriage = proc / "centerline_shift_taper" / "centerline_shift.gpkg"
    merged = proc / "centerline_shift_taper" / "centerline_shift_with_sg.gpkg"
    axes_path = merged if merged.is_file() else proc / "centerline_shift_taper" / "centerline_shift.gpkg"
    for path in (heights, carriage, axes_path):
        if not path.is_file():
            raise SystemExit(f"missing {path}")
    # A restricted run is a trial; it must not overwrite the site result.
    out_dir = proc / (OUT_DIR_NAME + ("_trial" if args.oid else ""))
    out_dir.mkdir(parents=True, exist_ok=True)
    plot_oids = set(args.plot_oid or [PLOT_OID])

    t0 = time.time()
    z_in, spec = _load_geotiff(heights)
    roads = gpd.read_file(carriage, layer="carriageway")
    lines = gpd.read_file(axes_path, layer="centerline")
    try:
        segments = gpd.read_file(axes_path, layer="segments")
    except Exception:
        segments = None
    zones = _zones(segments)
    struct = _structure_mask(zones, z_in.shape, spec) if zones else np.zeros(z_in.shape, dtype=bool)
    print(f"input {heights.name} {z_in.shape}, structure spans {len(zones)}", flush=True)

    acc = {
        "num": np.zeros(z_in.shape, dtype=np.float64),
        "den": np.zeros(z_in.shape, dtype=np.float64),
        "wsum": np.zeros(z_in.shape, dtype=np.float64),
        "lo": np.full(z_in.shape, np.inf, dtype=np.float64),
        "hi": np.full(z_in.shape, -np.inf, dtype=np.float64),
    }
    road = np.zeros(z_in.shape, dtype=bool)
    per_oid = []
    unfit = []
    axes = []
    captures = {}
    skipped = 0
    groups = list(roads.groupby("objectid"))
    for n_done, (oid, group) in enumerate(groups, start=1):
        oid = int(oid)
        polys = []
        for geom in group.geometry:
            if geom is None or geom.is_empty:
                continue
            polys.extend(list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom])
        for poly in polys:
            got = _crop_inside(poly, spec)
            if got is not None:
                inside, r0, c0 = got
                road[r0 : r0 + inside.shape[0], c0 : c0 + inside.shape[1]] |= inside
        if args.oid and oid not in args.oid:
            continue
        line = _line_of(lines, oid)
        if line is None or not polys:
            skipped += 1
            continue
        res = _process(oid, line, polys, z_in, spec, struct, acc, oid in plot_oids)
        if res is None:
            skipped += 1
            continue
        if res.get("unfit"):
            res.pop("axis")
            unfit.append(res)
            continue
        axes.append(res.pop("axis"))
        cap = res.pop("capture", None)
        if cap is not None:
            captures[oid] = cap
        per_oid.append(res)
        if n_done % 200 == 0:
            print(f"  {n_done}/{len(groups)} carriageways, {time.time() - t0:.0f} s", flush=True)

    written = acc["den"] > 0
    z_out = z_in.astype(np.float32).copy()
    z_out[written] = (acc["num"][written] / acc["den"][written]).astype(np.float32)
    # Two carriageways claiming one pixel at different heights (terrace, wall
    # between parallel roads): the road surface is undefined there. Fade back
    # to the measured input as the disagreement grows past KEEP_M.
    spread = np.where(written, acc["hi"] - acc["lo"], 0.0)
    fade = np.clip((spread - KEEP_M) / KEEP_M, 0.0, 1.0)
    conflict = written & (fade > 0.0)
    z_out[conflict] = ((1.0 - fade[conflict]) * z_out[conflict] + fade[conflict] * z_in[conflict]).astype(np.float32)
    weight = np.full(z_in.shape, np.nan, dtype=np.float32)
    weight[written] = (acc["wsum"][written] / acc["den"][written]).astype(np.float32)
    removed = np.full(z_in.shape, np.nan, dtype=np.float32)
    removed[written] = (z_in[written] - z_out[written]).astype(np.float32)
    on = road & np.isfinite(z_in) & ~struct

    before_ro = _stats(_road_only(z_in, road), on)
    after_ro = _stats(_road_only(z_out, road), on)
    before_full = _stats(_deviation(z_in), on)
    after_full = _stats(_deviation(z_out), on)
    # Stations next to a structure span compare a kept pixel with a smoothed one;
    # that step is the portal, not the road. Keep them out of the ride metric.
    struct_wide = _structure_mask([zone.buffer(2.0) for zone in zones], z_in.shape, spec) if zones else struct
    ride_ok = road & ~struct_wide
    wheel_before, wheel_after = _wheel_paths(axes, z_in, z_out, spec, ride_ok)
    print(
        f"road-only roughness: hot {before_ro['hot_px']} -> {after_ro['hot_px']} of {after_ro['n']}, "
        f"p90 {before_ro['p90_m']} -> {after_ro['p90_m']} m, p99 {before_ro['p99_m']} -> {after_ro['p99_m']} m",
        flush=True,
    )

    write_referenced_tif(out_dir / "04_smooth.tif", np.where(road, z_out, np.nan).astype(np.float32), xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    write_referenced_tif(out_dir / "04_residual.tif", removed, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    write_referenced_tif(out_dir / "04_weight.tif", weight, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)

    plots = {
        "roughness": _plot_roughness(_road_only(z_in, road)[on], _road_only(z_out, road)[on], out_dir),
        "wheel_path": _plot_wheel(wheel_before, wheel_after, out_dir),
    }
    for oid, cap in captures.items():
        plots[f"along_oid{oid}"] = _plot_along(cap, oid, out_dir)
        plots[f"cross_oid{oid}"] = _plot_cross(cap, oid, out_dir)
        plots[f"unrolled_oid{oid}"] = _plot_unrolled(cap, oid, out_dir)

    rm = np.abs(removed[written])
    kept = weight[written]
    report = {
        "heights": str(heights),
        "carriageway": str(carriage),
        "axes": str(axes_path),
        "station_m": STATION_M,
        "cutoff_axis_m": CUTOFF_AXIS_M,
        "cutoff_slope_m": CUTOFF_SLOPE_M,
        "remainder_sigma_along_m": RESID_ALONG_M,
        "remainder_sigma_across_m": RESID_ACROSS_M,
        "max_half_m": MAX_HALF_M,
        "tukey_c": TUKEY_C,
        "sigma_floor_m": SIGMA_FLOOR_M,
        "sigma_cap_m": SIGMA_CAP_M,
        "keep_m": KEEP_M,
        "edge_support_m": EDGE_SUPPORT_M,
        "max_crossfall": MAX_CROSSFALL,
        "blend_reach_m": BLEND_REACH_M,
        "iterations": ITERATIONS,
        "carriageways": len(per_oid),
        "skipped": skipped,
        "unfit": len(unfit),
        "unfit_px_kept": int(sum(d["px"] for d in unfit)),
        "unfit_carriageways": sorted(unfit, key=lambda d: -d["px"]),
        "written_px": int(written.sum()),
        "structure_px_kept": int((road & struct).sum()),
        "conflict_px": int(conflict.sum()),
        "conflict_px_full_input": int(np.count_nonzero(fade >= 1.0)),
        "outlier_px": int(np.count_nonzero(kept <= 0.0)),
        "carriageways_with_clamped_crossfall": int(sum(1 for d in per_oid if d["clamped_half_stations"])),
        "removed_abs": _dist_stats(rm),
        "before_road_only": before_ro,
        "after_road_only": after_ro,
        "before_full_window": before_full,
        "after_full_window": after_full,
        "wheel_path_before": _dist_stats(wheel_before),
        "wheel_path_after": _dist_stats(wheel_after),
        "worst_carriageways": sorted(per_oid, key=lambda d: -d["outliers"])[:15],
        "plots": plots,
        "seconds": round(time.time() - t0, 1),
        "note": (
            "z_out = penalised two-line road model + weighted low-pass of the remainder. "
            "04_residual.tif is input minus output, the removed roughness layer. "
            "04_weight.tif is the keep weight of the remainder (biweight, zero at keep_m); "
            "zero means the pixel was an outlier and took the model. "
            "Structure spans and unfit carriageways keep the input height. Where overlapping "
            "carriageways disagree by more than keep_m the output fades back to the input (conflict_px). "
            "The wheel-path metric runs over fitted carriageways, 2 m clear of structure spans."
        ),
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {out_dir / '04_smooth.tif'}, {out_dir / '04_residual.tif'}, report.json, {len(plots)} plots in {report['seconds']} s", flush=True)


if __name__ == "__main__":
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        main()
