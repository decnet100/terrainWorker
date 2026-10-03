"""Split the repaired road DGM into a smooth road model and a roughness layer.

A road is a smooth surface by construction: a vertical alignment along the
axis made of grades and long vertical curves, and a cross-section per station
whose crossfall changes slowly along the road. This script fits that model to
every carriageway and keeps only what the model and a gentle low-pass of the
remainder explain. Everything shorter than the cutoff wavelength along the
road, and every outlier such as a parked vehicle, ends up in a residual raster
instead of the driving surface.

Per carriageway (one ``objectid``, or several WFS pieces that share a GIP
routing link):

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
``MAX_HALF_M`` from the axis keep the input height. A bridge that has a DOM
deck (``bridge_deck``) keeps that stamped surface on the overpass mesh. The
road model fits the approaches on either side of it and does not replace the
plate. Where carriageways overlap
(junctions, parallel roads), each model is weighted by the distance to its own
polygon edge, so the mix is continuous across that edge. A crossfall steeper
than ``MAX_CROSSFALL`` is an embankment inside a too-wide polygon and is
clamped. Nothing is written into a BeamNG level.

Municipal roads (GIP ``OBJEKT`` in ``NARROW_OBJEKT``, i.e. ``S-G``) get their
polygon narrowed to the surface the model explains: per 2 m of station and
per side, the outermost 0.5 m offset bin whose median |remainder| stays
within ``ROAD_TOL_M`` is the road edge; the edge line is median- and
Gaussian-filtered along the road, the fit is repeated on the pixels inside,
and a variable-width ribbon replaces the polygon when at least
``NARROW_MIN_M`` was cut somewhere. Pixels outside the ribbon keep the input
height. The result is ``carriageway_smooth.gpkg`` (layer ``carriageway``),
which the mesh and the terrain clamp read instead of
``carriageway_bridged.gpkg``.

When ``gip_routing.json`` is present, each WFS piece is assigned to one
routing link; pieces on that link are one carriageway (one axis, one model).
Overlapping geometry is not a merge key. Bridge codes ``S-BB`` / ``S-AB`` /
``S-LB`` that the centerline shift left out of the clip are put back from the
shift buffer. A road that crosses another without a routing node and at a
different ``LEVEL_INTERMEDIATE`` is tagged ``overpass`` / ``underpass`` and
fitted into separate accumulators: the two surfaces keep their own height, and
``build_road_grid.py`` emits a mesh per layer. The lower polygon is not cut
back.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\smooth_road_surface.py

Then the mesh from the smooth raster and the narrowed polygons:

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\build_road_grid.py --heights "data\\processed\\tirol-imst-tarrenz-8192\\road_surface_smooth\\04_smooth.tif" --clip "data\\processed\\tirol-imst-tarrenz-8192\\road_surface_smooth\\carriageway_smooth.gpkg" --clip-layer carriageway
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
from gip_routing_load import apply_routing_carriageways, merge_centerline_parts  # noqa: E402
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
# Empty cells in a clip polygon (S-BB omitted from the repair mask) take the
# raw 0.5 m DGM when it sits on the same terrace as the finite samples.
FILL_RAW_M = 2.0
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
# Gemeindestraßen: the measured polygon often covers the embankment. The
# carriageway is cut back to the part of the cross-section that lies on the
# model. Walking out from the axis in 0.5 m offset bins over EXTENT_BIN_M of
# station, the edge is the first bin whose median |input - model| exceeds
# ROAD_TOL_M. The edge is smoothed along the road and never moves outward.
NARROW_OBJEKT = {"S-G"}
ROAD_TOL_M = 0.12
EXTENT_BIN_M = 2.0
MIN_HALF_M = 1.5
EDGE_MEDIAN_M = 10.0
EDGE_SIGMA_M = 4.0
NARROW_MIN_M = 0.3
NARROW_ROUNDS = 3
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


class _ZView:
    """Base raster plus a few replaced cells. The base array is not copied."""

    def __init__(self, base: np.ndarray, rows: np.ndarray, cols: np.ndarray, vals: np.ndarray):
        self.base = base
        self.shape = base.shape
        width = int(base.shape[1])
        key = rows.astype(np.int64) * width + cols.astype(np.int64)
        order = np.argsort(key, kind="mergesort")
        key = key[order]
        vals = np.asarray(vals, dtype=np.float64)[order]
        if len(key):
            last = np.r_[key[1:] != key[:-1], True]
            key = key[last]
            vals = vals[last]
        self._key = key
        self._vals = vals

    def __getitem__(self, idx):
        if not (isinstance(idx, tuple) and len(idx) == 2 and isinstance(idx[0], np.ndarray)):
            return self.base[idx]
        rows, cols = idx
        out = np.array(self.base[rows, cols], dtype=np.float64, copy=True)
        if len(self._key) == 0:
            return out
        key = rows.astype(np.int64) * self.shape[1] + cols.astype(np.int64)
        pos = np.searchsorted(self._key, key)
        pos = np.clip(pos, 0, len(self._key) - 1)
        hit = self._key[pos] == key
        out[hit] = self._vals[pos[hit]]
        return out


def _view_from_map(base: np.ndarray, filled: dict) -> np.ndarray | _ZView:
    if not filled:
        return base
    rows = np.fromiter((k[0] for k in filled), dtype=np.int32, count=len(filled))
    cols = np.fromiter((k[1] for k in filled), dtype=np.int32, count=len(filled))
    vals = np.fromiter((filled[k] for k in filled), dtype=np.float64, count=len(filled))
    return _ZView(base, rows, cols, vals)


class SparseAcc:
    """Pixel contributions of one carriageway, applied to the full accumulator later."""

    def __init__(self):
        self.rows: list[np.ndarray] = []
        self.cols: list[np.ndarray] = []
        self.num: list[np.ndarray] = []
        self.den: list[np.ndarray] = []
        self.wsum: list[np.ndarray] = []
        self.z: list[np.ndarray] = []

    def add(self, rows, cols, num, den, wsum, z) -> None:
        self.rows.append(np.asarray(rows, dtype=np.int32))
        self.cols.append(np.asarray(cols, dtype=np.int32))
        self.num.append(np.asarray(num, dtype=np.float64))
        self.den.append(np.asarray(den, dtype=np.float64))
        self.wsum.append(np.asarray(wsum, dtype=np.float64))
        self.z.append(np.asarray(z, dtype=np.float64))

    def packed(self) -> dict | None:
        if not self.rows:
            return None
        return {
            "rows": np.concatenate(self.rows),
            "cols": np.concatenate(self.cols),
            "num": np.concatenate(self.num),
            "den": np.concatenate(self.den),
            "wsum": np.concatenate(self.wsum),
            "z": np.concatenate(self.z),
        }


def _acc_add(acc, rows, cols, zout, blend, w) -> None:
    num = blend * zout
    den = blend
    wsum = blend * w
    if isinstance(acc, SparseAcc):
        acc.add(rows, cols, num, den, wsum, zout)
        return
    np.add.at(acc["num"], (rows, cols), num)
    np.add.at(acc["den"], (rows, cols), den)
    np.add.at(acc["wsum"], (rows, cols), wsum)
    np.minimum.at(acc["lo"], (rows, cols), zout)
    np.maximum.at(acc["hi"], (rows, cols), zout)


def _apply_sparse(acc: dict, packed: dict | None) -> None:
    if not packed:
        return
    rows = packed["rows"]
    cols = packed["cols"]
    np.add.at(acc["num"], (rows, cols), packed["num"])
    np.add.at(acc["den"], (rows, cols), packed["den"])
    np.add.at(acc["wsum"], (rows, cols), packed["wsum"])
    np.minimum.at(acc["lo"], (rows, cols), packed["z"])
    np.maximum.at(acc["hi"], (rows, cols), packed["z"])


def _pixels(polys, z: np.ndarray, spec: dict, skip: np.ndarray | None):
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
    vals = z[rows, cols]
    if skip is None:
        keep = np.isfinite(vals)
    else:
        keep = np.isfinite(vals) & ~np.asarray(skip[rows, cols], dtype=bool)
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


def _robust_fit(design, zin, support, n_st, t_plus, t_minus, u, e):
    """Tukey-reweighted penalised fit on the support pixels. Crossfall clamped."""
    w_fit = support.astype(np.float64)
    sigma = SIGMA_FLOOR_M
    params = None
    for _ in range(ITERATIONS):
        params = _fit(design, w_fit, zin, n_st, t_plus, t_minus)
        zm = _model_at(params, u, t_plus, t_minus, e)
        r = zin - zm
        w_fit, sigma = _tukey(np.where(support, r, np.nan))
    clamped = int(np.count_nonzero(np.abs(params[:, 1:3]) > MAX_CROSSFALL))
    if clamped:
        params[:, 1:3] = np.clip(params[:, 1:3], -MAX_CROSSFALL, MAX_CROSSFALL)
        zm = _model_at(params, u, t_plus, t_minus, e)
        r = zin - zm
    return params, zm, r, w_fit, sigma, clamped


def _road_extent(s, t, r, n_st):
    """Per side and station bin: outer edge of the road on the model, and of the polygon.

    Returns (edge_left, edge_right, poly_left, poly_right) per EXTENT_BIN_M bin.
    NaN where a bin has no pixel on that side.
    """
    n_bin = int(math.ceil(n_st * STATION_M / EXTENT_BIN_M)) + 1
    n_off = int(math.ceil(MAX_HALF_M / OFFSET_BIN_M)) + 1
    i = np.clip(np.floor(s / EXTENT_BIN_M).astype(np.int64), 0, n_bin - 1)
    j = np.clip(np.floor(np.abs(t) / OFFSET_BIN_M).astype(np.int64), 0, n_off - 1)
    out_edges = []
    out_polys = []
    for side in (t >= 0.0, t < 0.0):
        edge = np.full(n_bin, np.nan)
        poly = np.full(n_bin, -np.inf)
        flat = i[side] * n_off + j[side]
        absr = np.abs(r[side])
        order = np.argsort(flat, kind="stable")
        flat_sorted = flat[order]
        absr_sorted = absr[order]
        starts = np.flatnonzero(np.r_[True, flat_sorted[1:] != flat_sorted[:-1]])
        ends = np.r_[starts[1:], len(flat_sorted)]
        med = np.full(n_bin * n_off, np.nan)
        for a, b in zip(starts, ends):
            med[flat_sorted[a]] = np.median(absr_sorted[a:b])
        med = med.reshape(n_bin, n_off)
        has = np.isfinite(med)
        t_side = np.abs(t[side])
        if t_side.size:
            np.maximum.at(poly, i[side], t_side)
        poly = np.where(np.isfinite(poly), poly + 0.5 * OFFSET_BIN_M, np.nan)
        # A bin with no pixel at all is a hole in the raster; the polygon edge
        # there is interpolated later.
        for k in range(n_bin):
            if not np.any(has[k]):
                continue
            reach = 0
            for jj in range(n_off):
                if not has[k, jj]:
                    # One empty bin inside the road is a gap in the raster, not the edge.
                    if jj + 1 < n_off and has[k, jj + 1] and med[k, jj + 1] <= ROAD_TOL_M:
                        continue
                    break
                if med[k, jj] > ROAD_TOL_M:
                    break
                reach = jj + 1
            edge[k] = reach * OFFSET_BIN_M
        out_edges.append(edge)
        out_polys.append(poly)
    return out_edges[0], out_edges[1], out_polys[0], out_polys[1]


def _smooth_edge(edge, poly):
    """Fill, median and Gaussian along the road; clamp to [MIN_HALF_M, polygon]."""
    from scipy.ndimage import gaussian_filter1d, median_filter

    have = np.isfinite(edge) & np.isfinite(poly)
    if not np.any(have):
        return poly.copy()
    idx = np.arange(len(edge))
    filled = np.interp(idx, idx[have], edge[have])
    win = int(round(EDGE_MEDIAN_M / EXTENT_BIN_M)) | 1
    if len(filled) >= win:
        filled = median_filter(filled, size=win, mode="nearest")
    filled = gaussian_filter1d(filled, EDGE_SIGMA_M / EXTENT_BIN_M, mode="nearest")
    poly_filled = np.where(np.isfinite(poly), poly, np.interp(idx, idx[np.isfinite(poly)], poly[np.isfinite(poly)]))
    return np.clip(filled, MIN_HALF_M, poly_filled)


def _ribbon(xy_d, s_d, half_l, half_r, polys):
    """Variable-width ribbon along the axis, cut to the original polygon."""
    n_bin = len(half_l)
    s_bin = (np.arange(n_bin) + 0.5) * EXTENT_BIN_M
    hl = np.interp(s_d, s_bin, half_l)
    hr = np.interp(s_d, s_bin, half_r)
    d = np.gradient(xy_d, axis=0)
    norm = np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)
    nx = -d[:, 1] / norm
    ny = d[:, 0] / norm
    left = np.column_stack([xy_d[:, 0] + nx * hl, xy_d[:, 1] + ny * hl])
    right = np.column_stack([xy_d[:, 0] - nx * hr, xy_d[:, 1] - ny * hr])
    ring = np.vstack([left, right[::-1]])
    poly = shapely.make_valid(shapely.Polygon(ring))
    poly = shapely.union_all([g for g in _iter_polys(poly)])
    # Slightly beyond the axis ends the ribbon has no support; the original
    # polygon ends close them. The polygon also caps the width.
    original = shapely.union_all(polys).buffer(0)
    cut = poly.intersection(original)
    return [g for g in _iter_polys(cut) if g.area >= 1.0]


def _bridge_decks(carriage: Path) -> list:
    """Deck polygons written by blend_bridge_deck.py, empty if the file has
    no such layer."""
    try:
        import pyogrio

        if "bridge_deck" not in {str(row[0]) for row in pyogrio.list_layers(carriage)}:
            return []
        gdf = gpd.read_file(carriage, layer="bridge_deck")
    except Exception:
        return []
    return [g for g in gdf.geometry if g is not None and not g.is_empty]


def _raster_mask(polys, shape, spec) -> np.ndarray:
    """True on cells whose centre lies inside any polygon."""
    mask = np.zeros(shape, dtype=bool)
    for poly in polys:
        for part in _iter_polys(poly):
            got = _crop_inside(part, spec)
            if got is None:
                continue
            inside, r0, c0 = got
            view = mask[r0 : r0 + inside.shape[0], c0 : c0 + inside.shape[1]]
            view |= inside
    return mask


def _keep_deck(acc, polys, z, spec, deck: np.ndarray) -> int:
    """Write the stamped deck into the overpass layer. The road model does not."""
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
        return 0
    rows = np.concatenate(rows_all)
    cols = np.concatenate(cols_all)
    flat = np.unique(rows.astype(np.int64) * z.shape[1] + cols)
    rows = (flat // z.shape[1]).astype(np.int32)
    cols = (flat % z.shape[1]).astype(np.int32)
    keep = deck[rows, cols] & np.isfinite(z[rows, cols])
    rows = rows[keep]
    cols = cols[keep]
    if len(rows) == 0:
        return 0
    zz = z[rows, cols].astype(np.float64)
    w = np.ones(len(rows), dtype=np.float64)
    _acc_add(acc, rows, cols, zz, w, w)
    return int(len(rows))


def _gip_classes(site) -> dict[int, str]:
    """OBJECTID -> OBJEKT from the site's GIP cache."""
    from build_bridges import find_gip_geojson

    data = json.loads(find_gip_geojson(site).read_text(encoding="utf-8"))
    out: dict[int, str] = {}
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        try:
            out[int(props.get("OBJECTID"))] = str(props.get("OBJEKT") or "").upper().strip()
        except (TypeError, ValueError):
            continue
    return out


def _oid_members(group) -> list[int]:
    """Original WFS ids in a routing-merged carriageway row."""
    oid = int(group.iloc[0].objectid)
    if "members" not in group.columns:
        return [oid]
    raw = group.iloc[0].members
    if raw is None or str(raw).strip() == "":
        return [oid]
    try:
        return [int(x) for x in str(raw).split(",") if x.strip()]
    except ValueError:
        return [oid]


def _fill_nan_from_raw(z: np.ndarray, raw: np.ndarray | None, polys: list, spec: dict):
    """Raw DGM in NaN clip cells that sit on the same terrace.

    The result is a view over the original raster. Hole cells are stored
    beside it, so a worker can read a shared raster without copying it.
    """
    if raw is None or z is raw:
        return z
    base = z.base if isinstance(z, _ZView) else z
    filled = {}
    if isinstance(z, _ZView) and len(z._key):
        width = int(base.shape[1])
        filled = {
            (int(k // width), int(k % width)): float(v)
            for k, v in zip(z._key.tolist(), z._vals.tolist())
        }
    changed = False
    for poly in polys:
        got = _crop_inside(poly, spec)
        if got is None:
            continue
        inside, r0, c0 = got
        sl = base[r0 : r0 + inside.shape[0], c0 : c0 + inside.shape[1]]
        sl_raw = raw[r0 : r0 + inside.shape[0], c0 : c0 + inside.shape[1]]
        local = np.array(sl, dtype=np.float64, copy=True)
        if filled:
            for (r, c), value in filled.items():
                if r0 <= r < r0 + local.shape[0] and c0 <= c < c0 + local.shape[1]:
                    local[r - r0, c - c0] = value
        finite = inside & np.isfinite(local)
        hole = inside & ~np.isfinite(local) & np.isfinite(sl_raw)
        if not np.any(hole):
            continue
        if np.any(finite):
            med = float(np.median(local[finite]))
            hole = hole & (np.abs(sl_raw - med) <= FILL_RAW_M)
            if not np.any(hole):
                continue
        rr, cc = np.nonzero(hole)
        for r, c in zip(rr.tolist(), cc.tolist()):
            filled[(r + r0, c + c0)] = float(sl_raw[r, c])
        changed = True
    if not changed and not isinstance(z, _ZView):
        return z
    return _view_from_map(base, filled)


def _stamp_cover_clearance(
    z: np.ndarray,
    spec: dict,
    members: list[int],
    site: dict,
    raw: np.ndarray | None,
) -> np.ndarray:
    """Set the underpass floor to cover DGM minus the stored clearance."""
    if raw is None:
        return z
    from gip_bridge_flags import cover_bore_geoms

    bores = cover_bore_geoms(site, set(members))
    if not bores:
        return z
    base = z.base if isinstance(z, _ZView) else z
    filled: dict[tuple[int, int], float] = {}
    if isinstance(z, _ZView) and len(z._key):
        width = int(base.shape[1])
        filled = {
            (int(k // width), int(k % width)): float(v)
            for k, v in zip(z._key.tolist(), z._vals.tolist())
        }
    hit_any = False
    for bore in bores:
        got = _crop_inside(bore["geometry"], spec)
        if got is None:
            continue
        inside, r0, c0 = got
        sl_raw = raw[r0 : r0 + inside.shape[0], c0 : c0 + inside.shape[1]]
        hit = inside & np.isfinite(sl_raw)
        if not np.any(hit):
            continue
        clearance = float(bore["clear_height_m"])
        rr, cc = np.nonzero(hit)
        for r, c in zip(rr.tolist(), cc.tolist()):
            filled[(r + r0, c + c0)] = float(sl_raw[r, c]) - clearance
        hit_any = True
    if not hit_any:
        return z
    return _view_from_map(base, filled)


def _mesh_meta(group, oid: int) -> tuple[str, int, int]:
    kind = "road"
    if "mesh_kind" in group.columns:
        kind = str(group.iloc[0].mesh_kind or "road").strip() or "road"
    key = 0
    if "mesh_key" in group.columns:
        try:
            key = int(group.iloc[0].mesh_key)
        except (TypeError, ValueError):
            key = 0
    if kind != "road" and key == 0:
        key = int(oid)
    layer = 0
    if "mesh_layer" in group.columns:
        try:
            layer = int(group.iloc[0].mesh_layer)
        except (TypeError, ValueError):
            layer = 0
    return kind, key, layer


def _empty_acc(shape: tuple[int, int]) -> dict:
    return {
        "num": np.zeros(shape, dtype=np.float64),
        "den": np.zeros(shape, dtype=np.float64),
        "wsum": np.zeros(shape, dtype=np.float64),
        "lo": np.full(shape, np.inf, dtype=np.float64),
        "hi": np.full(shape, -np.inf, dtype=np.float64),
    }


def _finish_acc(acc: dict, z_in: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Composite one accumulator; fade pixels where two models of the same layer disagree."""
    written = acc["den"] > 0
    z_out = z_in.astype(np.float32).copy()
    z_out[written] = (acc["num"][written] / acc["den"][written]).astype(np.float32)
    spread = np.where(written, acc["hi"] - acc["lo"], 0.0)
    fade = np.clip((spread - KEEP_M) / KEEP_M, 0.0, 1.0)
    conflict = written & (fade > 0.0)
    z_out[conflict] = ((1.0 - fade[conflict]) * z_out[conflict] + fade[conflict] * z_in[conflict]).astype(np.float32)
    return z_out, written, conflict, fade


def _iter_polys(geom):
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if hasattr(geom, "geoms"):
        out = []
        for g in geom.geoms:
            out.extend(_iter_polys(g))
        return out
    return []


def _process(oid, line, polys, z, spec, skip, acc, want_capture, narrow=False):
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
    params, zm, r, w_fit, sigma, clamped = _robust_fit(design, zin, support, n_st, t_plus, t_minus, u, e)
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
    new_polys = None
    narrow_m = 0.0
    half_l = half_r = None
    full_in = None
    if narrow:
        # Find the road inside the polygon, refit on it, repeat. The embankment
        # pulls the first crossfall; on the second round it is outside the support.
        for _ in range(NARROW_ROUNDS):
            edge_l, edge_r, poly_l, poly_r = _road_extent(s, t, r, n_st)
            half_l = _smooth_edge(edge_l, poly_l)
            half_r = _smooth_edge(edge_r, poly_r)
            k = np.clip(np.floor(s / EXTENT_BIN_M).astype(np.int64), 0, len(half_l) - 1)
            limit = np.where(t >= 0.0, half_l[k], half_r[k])
            inside = np.abs(t) <= limit + 0.5 * OFFSET_BIN_M
            sup2 = support & inside
            if float(np.mean(sup2)) < MIN_SUPPORT_SHARE * 0.5:
                break
            params, zm, r, w_fit, sigma, clamped = _robust_fit(design, zin, sup2, n_st, t_plus, t_minus, u, e)
        narrow_m = float(np.nanmax(np.r_[poly_l - half_l, poly_r - half_r])) if half_l is not None else 0.0
        if narrow_m >= NARROW_MIN_M:
            new_polys = _ribbon(xy_d, s_d, half_l, half_r, polys)
            if new_polys:
                full_in = (u.copy(), t.copy(), r.copy())
                keep = shapely.contains_xy(shapely.union_all(new_polys), x, y)
                rows, cols, x, y, zin, s, t, e, u, t_plus, t_minus, zm, r, to_edge = (
                    a[keep] for a in (rows, cols, x, y, zin, s, t, e, u, t_plus, t_minus, zm, r, to_edge)
                )
                if len(zin) < 12:
                    return None
            else:
                new_polys = None
                narrow_m = 0.0
        else:
            narrow_m = 0.0
    w = _keep_weight(r)
    lp, lp_grid = _kept_remainder(u, t, r, w, n_st)
    zout = zm + lp
    blend = np.minimum(to_edge, BLEND_REACH_M) ** 2 + 1e-3
    _acc_add(acc, rows, cols, zout, blend, w)
    out = {
        "oid": int(oid),
        "px": int(len(zin)),
        "outliers": int(np.count_nonzero(w <= 0.0)),
        "sigma_m": round(float(sigma), 4),
        "support_share": round(float(np.mean(support)), 3),
        "fit_share": round(fit_share, 3),
        "clamped_half_stations": clamped,
        "narrow_m": round(narrow_m, 2),
        "stations": int(n_st),
        "axis": (xy_d, s_d, n_d),
        "polys": new_polys,
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
            "half_l": half_l if new_polys else None,
            "half_r": half_r if new_polys else None,
            "full": full_in if new_polys else None,
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
    if cap.get("full") is not None:
        # Whole polygon in the first panel, so the cut-away embankment is visible.
        fu, ft, fr = cap["full"]
        num_in, den_in = _bin_grid(fu, ft, fr, np.ones(len(fu)), n_st)
    else:
        num_in, den_in = _bin_grid(cap["u"], cap["t"], cap["z_in"] - cap["model"], ones, n_st)
    num_out, den = _bin_grid(cap["u"], cap["t"], cap["z_out"] - cap["model"], ones, n_st)
    num_rm, _ = _bin_grid(cap["u"], cap["t"], cap["z_in"] - cap["z_out"], ones, n_st)
    panels = []
    for num, d in ((num_in, den_in), (num_out, den), (num_rm, den)):
        g = np.where(d > 0, num / np.maximum(d, 1e-12), np.nan) * 100.0
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
    half_l = cap.get("half_l")
    half_r = cap.get("half_r")
    for ax, g, title in zip(axes, panels, titles):
        im = ax.imshow(g, extent=extent, aspect="auto", cmap="RdBu_r", vmin=-5.0, vmax=5.0, interpolation="nearest")
        if half_l is not None:
            s_bin = (np.arange(len(half_l)) + 0.5) * EXTENT_BIN_M
            ax.plot(s_bin, half_l, color="black", linewidth=0.8)
            ax.plot(s_bin, -half_r, color="black", linewidth=0.8, label="kept carriageway edge")
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


def _plain(value: float, unit: str) -> str:
    text = f"{value:.10g}"
    return f"{text}{unit}"


def _plot_wheel(before, after, out_dir: Path) -> str:
    from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

    fig, ax = plt.subplots(figsize=(7.0, 4.2))
    _ccdf(ax, before, "before (repaired DGM)", "#8a8f98")
    _ccdf(ax, after, "after (smooth surface)", "#0b3d91")
    for step, ls in zip(BUMP_STEPS_M, ("--", ":")):
        ax.axvline(step * 100.0, color="#c62828", linewidth=0.8, linestyle=ls, label=_plain(step * 100.0, "cm"))
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(0.01, 100.0)
    ax.set_ylim(1e-6, 1.0)
    ax.xaxis.set_major_locator(LogLocator(base=10.0))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _pos: _plain(v, "cm")))
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.yaxis.set_major_locator(LogLocator(base=10.0))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _pos: _plain(v * 100.0, "%")))
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.set_xlabel(f"height difference along road within {STATION_M:g}m")
    ax.set_ylabel("share of locations at or above")
    ax.set_title("wheel ride steps, middle of road")
    ax.grid(True, which="both", linewidth=0.3, alpha=0.5)
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    path = out_dir / "wheelpath_second_difference.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return str(path)


def _smooth_worker(payload: dict) -> tuple:
    from proc_pool import array, extra

    info = extra()
    spec = info["spec"]
    site = info["site"]
    z_in = array("z_in")
    z_ground = array("z_ground")
    z_raw = array("z_raw") if info["has_raw"] else None
    struct = array("struct")
    deck = array("deck") if info["has_deck"] else None
    kind = payload["kind"]
    polys = payload["polys"]
    if kind == "overpass" and deck is not None:
        skip = deck
    elif kind in {"underpass", "overpass", "span"}:
        skip = None
    else:
        skip = struct
    z_fit = z_ground if kind == "underpass" else z_in
    z_fit = _fill_nan_from_raw(z_fit, z_raw, polys, spec)
    if kind == "underpass":
        z_fit = _stamp_cover_clearance(z_fit, spec, payload["members"], site, z_raw)
    acc = SparseAcc()
    res = _process(
        payload["oid"],
        payload["line"],
        polys,
        z_fit,
        spec,
        skip,
        acc,
        payload["want_plot"],
        narrow=payload["narrow"],
    )
    deck_px = 0
    if kind == "overpass" and deck is not None:
        deck_px = _keep_deck(acc, polys, z_fit, spec, deck)
    return acc.packed(), res, deck_px


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--heights", type=Path, default=None, help="input raster, default 03_with_bridges.tif")
    ap.add_argument("--oid", type=int, action="append", default=None, help="process only these objectids")
    ap.add_argument("--plot-oid", type=int, action="append", default=None, help="objectids to plot, default 6975")
    ap.add_argument("--replot", action="store_true", help="redraw the two site-wide plots from the existing 04_smooth.tif, no fit")
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
    z_ground = z_in
    ground_path = repair / "02_repaired.tif"
    if ground_path.is_file() and Path(heights).resolve() != ground_path.resolve():
        zg, spec_g = _load_geotiff(ground_path)
        if zg.shape == z_in.shape and spec_g.get("xmin") == spec["xmin"] and spec_g.get("ymax") == spec["ymax"]:
            z_ground = zg
            print(f"underpass heights {ground_path.name} (DGM before deck stamp)", flush=True)
        else:
            print("02_repaired.tif frame differs, underpass uses the input raster", flush=True)
    z_raw = None
    raw_path = proc / "corridor50_raw" / "corridor50_raw.tif"
    if raw_path.is_file():
        zr, spec_r = _load_geotiff(raw_path)
        if zr.shape == z_in.shape and spec_r.get("xmin") == spec["xmin"] and spec_r.get("ymax") == spec["ymax"]:
            z_raw = zr
    roads = gpd.read_file(carriage, layer="carriageway")
    roads, routing_stats = apply_routing_carriageways(roads, site)
    if routing_stats.get("used"):
        print(
            f"routing: {routing_stats['groups']} groups, "
            f"{routing_stats['merged_groups']} merged from {routing_stats['merged_oids']} pieces, "
            f"{routing_stats.get('grade_overpass', 0)} overpass / "
            f"{routing_stats.get('grade_underpass', 0)} underpass meshes "
            f"(no XY clip)",
            flush=True,
        )
    lines = gpd.read_file(axes_path, layer="centerline")
    try:
        segments = gpd.read_file(axes_path, layer="segments")
    except Exception:
        segments = None
    zones = _zones(segments)
    # The bridge deck polygon reaches past the GIP zone (railing wider than
    # the carriageway). Protect that rim too, but only beside the span: the
    # deck's approach overlap stays open to the road model.
    decks = _bridge_decks(carriage)
    if decks and zones:
        near_zone = shapely.union_all(zones).buffer(1.0)
        for deck in decks:
            rim = deck.intersection(near_zone)
            if not rim.is_empty:
                zones.append(rim)
    struct = _structure_mask(zones, z_in.shape, spec) if zones else np.zeros(z_in.shape, dtype=bool)
    # The full ribbon, including the blend into the road. The overpass mesh
    # keeps this surface. Ordinary roads still use the zone mask above.
    deck_mask = _raster_mask(decks, z_in.shape, spec) if decks else None
    print(
        f"input {heights.name} {z_in.shape}, structure spans {len(zones)} "
        f"(bridge decks {len(decks)})",
        flush=True,
    )

    if args.replot:
        z_out, _ = _load_geotiff(out_dir / "04_smooth.tif")
        road = np.isfinite(z_out)
        z_out = np.where(road, z_out, z_in).astype(np.float32)
        on = road & np.isfinite(z_in) & ~struct
        report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
        skip_oids = {int(d["oid"]) for d in report.get("unfit_carriageways", [])}
        axes = []
        for oid, geom in zip(lines.objectid, lines.geometry):
            if int(oid) in skip_oids:
                continue
            line = _line_of(lines, int(oid))
            if line is not None:
                axes.append(_axis(line))
        struct_wide = _structure_mask([zone.buffer(2.0) for zone in zones], z_in.shape, spec) if zones else struct
        wheel_before, wheel_after = _wheel_paths(axes, z_in, z_out, spec, road & ~struct_wide)
        _plot_roughness(_road_only(z_in, road)[on], _road_only(z_out, road)[on], out_dir)
        _plot_wheel(wheel_before, wheel_after, out_dir)
        print(f"Redrew site plots in {out_dir} in {time.time() - t0:.0f} s", flush=True)
        return

    accs = [_empty_acc(z_in.shape), _empty_acc(z_in.shape)]
    classes = _gip_classes(site)
    per_oid = []
    unfit = []
    axes = []
    captures = {}
    skipped = 0
    deck_kept = 0
    final_polys: dict[int, tuple[list, float, str, int, int]] = {}
    asked = set(int(x) for x in (args.oid or []))
    groups = list(roads.groupby("objectid"))
    members_of = {int(oid): _oid_members(group) for oid, group in groups}
    mesh_of = {int(oid): _mesh_meta(group, int(oid)) for oid, group in groups}
    jobs: list[dict] = []
    for oid, group in groups:
        oid = int(oid)
        members = _oid_members(group)
        kind, mesh_key, mesh_layer = mesh_of[oid]
        polys = []
        for geom in group.geometry:
            if geom is None or geom.is_empty:
                continue
            polys.extend(list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom])
        final_polys[oid] = (polys, 0.0, kind, mesh_key, mesh_layer)
        if asked and asked.isdisjoint(set(members) | {oid}):
            continue
        pieces = [_line_of(lines, m) for m in members]
        line = merge_centerline_parts(pieces) if any(p is not None for p in pieces) else None
        if line is None or not polys:
            skipped += 1
            continue
        narrow = (
            kind == "road"
            and all(classes.get(m, "") in NARROW_OBJEKT for m in members)
            and bool(members)
        )
        jobs.append(
            {
                "oid": oid,
                "kind": kind,
                "mesh_key": mesh_key,
                "mesh_layer": mesh_layer,
                "members": members,
                "polys": polys,
                "line": line,
                "narrow": narrow,
                "want_plot": bool(plot_oids & (set(members) | {oid})),
            }
        )
    while len(accs) <= max((job["mesh_layer"] for job in jobs), default=0):
        accs.append(_empty_acc(z_in.shape))
    from proc_pool import Pool, workers

    arrays = {"z_in": z_in, "z_ground": z_ground, "struct": struct}
    info = {"spec": spec, "site": site, "has_raw": z_raw is not None, "has_deck": deck_mask is not None}
    if z_raw is not None:
        arrays["z_raw"] = z_raw
    if deck_mask is not None:
        arrays["deck"] = deck_mask
    n_workers = min(workers(), len(jobs)) if jobs else 1
    print(f"fitting {len(jobs)} carriageways on {n_workers} workers", flush=True)
    with Pool(arrays, info, n=n_workers) as pool:
        for n, (packed, res, deck_px) in enumerate(pool.imap(_smooth_worker, jobs, chunksize=4), start=1):
            job = jobs[n - 1]
            _apply_sparse(accs[job["mesh_layer"]], packed)
            deck_kept += deck_px
            if res is None:
                if deck_px == 0:
                    skipped += 1
                continue
            if res.get("unfit"):
                res.pop("axis", None)
                unfit.append(res)
                continue
            axes.append(res.pop("axis"))
            new_polys = res.pop("polys", None)
            if new_polys:
                final_polys[job["oid"]] = (
                    new_polys,
                    res["narrow_m"],
                    job["kind"],
                    job["mesh_key"],
                    job["mesh_layer"],
                )
            res["objekt"] = classes.get(job["oid"], "")
            res["members"] = job["members"]
            cap = res.pop("capture", None)
            if cap is not None:
                captures[job["oid"]] = cap
            per_oid.append(res)
            if n % 200 == 0 or n == len(jobs):
                print(f"  {n}/{len(jobs)} carriageways, {time.time() - t0:.0f} s", flush=True)

    road = np.zeros(z_in.shape, dtype=bool)
    gpkg_rows = []
    for oid, (polys, narrow_m, kind, mesh_key, mesh_layer) in final_polys.items():
        for poly in polys:
            got = _crop_inside(poly, spec)
            if got is not None:
                inside, r0, c0 = got
                road[r0 : r0 + inside.shape[0], c0 : c0 + inside.shape[1]] |= inside
            gpkg_rows.append(
                {
                    "objectid": oid,
                    "members": ",".join(str(x) for x in members_of.get(oid, [oid])),
                    "narrowed": int(narrow_m > 0.0),
                    "narrow_m": round(narrow_m, 2),
                    "mesh_kind": kind,
                    "mesh_key": int(mesh_key),
                    "mesh_layer": int(mesh_layer),
                    "geometry": poly,
                }
            )
    carriage_out = out_dir / "carriageway_smooth.gpkg"
    if carriage_out.exists():
        carriage_out.unlink()
    gpd.GeoDataFrame(gpkg_rows, geometry="geometry", crs=roads.crs).to_file(carriage_out, layer="carriageway", driver="GPKG")
    narrowed = [d for d in per_oid if d["narrow_m"] > 0.0]
    print(f"carriageways narrowed: {len(narrowed)}, wrote {carriage_out.name}", flush=True)

    finished = [_finish_acc(acc, z_in) for acc in accs]
    z_layers = [item[0] for item in finished]
    written_layers = [item[1] for item in finished]
    conflict_layers = [item[2] for item in finished]
    fade_layers = [item[3] for item in finished]
    written = np.zeros(z_in.shape, dtype=bool)
    conflict = np.zeros(z_in.shape, dtype=bool)
    fade_any = np.zeros(z_in.shape, dtype=np.float64)
    z_out = z_in.astype(np.float32).copy()
    for z_l, w_l, c_l, f_l in finished:
        z_out[w_l] = z_l[w_l]
        written |= w_l
        conflict |= c_l
        fade_any = np.maximum(fade_any, f_l)
    z_under = z_layers[1] if len(z_layers) > 1 else z_in.astype(np.float32)
    written_under = written_layers[1] if len(written_layers) > 1 else np.zeros(z_in.shape, dtype=bool)
    den = np.zeros(z_in.shape, dtype=np.float64)
    wsum = np.zeros(z_in.shape, dtype=np.float64)
    for acc in accs:
        den += acc["den"]
        wsum += acc["wsum"]
    weight = np.full(z_in.shape, np.nan, dtype=np.float32)
    has_w = den > 0
    weight[has_w] = (wsum[has_w] / den[has_w]).astype(np.float32)
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
        f"p90 {before_ro['p90_m']} -> {after_ro['p90_m']} m, p99 {before_ro['p99_m']} -> {after_ro['p99_m']} m, "
        f"deck pixels kept {deck_kept}",
        flush=True,
    )

    write_referenced_tif(out_dir / "04_smooth.tif", np.where(road, z_out, np.nan).astype(np.float32), xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    write_referenced_tif(
        out_dir / "04_layer0.tif",
        np.where(road, z_layers[0], np.nan).astype(np.float32),
        xmin=spec["xmin"],
        ymax=spec["ymax"],
        res=RES_M,
    )
    write_referenced_tif(
        out_dir / "04_under.tif",
        np.where(written_under, z_under, np.nan).astype(np.float32),
        xmin=spec["xmin"],
        ymax=spec["ymax"],
        res=RES_M,
    )
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
        "routing": routing_stats,
        "iterations": ITERATIONS,
        "carriageways": len(per_oid),
        "skipped": skipped,
        "unfit": len(unfit),
        "unfit_px_kept": int(sum(d["px"] for d in unfit)),
        "unfit_carriageways": sorted(unfit, key=lambda d: -d["px"]),
        "written_px": int(written.sum()),
        "structure_px_kept": int((road & struct).sum()),
        "conflict_px": int(conflict.sum()),
        "conflict_px_full_input": int(np.count_nonzero(fade_any >= 1.0)),
        "underpass_px": int(written_under.sum()),
        "deck_px_kept": int(deck_kept),
        "mesh_layers": len(accs),
        "outlier_px": int(np.count_nonzero(kept <= 0.0)),
        "carriageways_with_clamped_crossfall": int(sum(1 for d in per_oid if d["clamped_half_stations"])),
        "narrow_objekt": sorted(NARROW_OBJEKT),
        "road_tol_m": ROAD_TOL_M,
        "narrowed_carriageways": len(narrowed),
        "narrowed_max_m": round(max((d["narrow_m"] for d in narrowed), default=0.0), 2),
        "most_narrowed": sorted(narrowed, key=lambda d: -d["narrow_m"])[:15],
        "carriageway_out": str(carriage_out),
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
            "carriageways of the same mesh layer disagree by more than keep_m the output fades "
            "back to the input (conflict_px). WFS pieces on one GIP routing link share a model; "
            "a grade-separated overpass and underpass keep their full polygons and their own "
            "height (04_under.tif) so build_road_grid can emit two meshes at that XY. "
            "The wheel-path metric runs over fitted carriageways, 2 m clear of structure spans."
        ),
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {out_dir / '04_smooth.tif'}, {out_dir / '04_layer0.tif'}, {out_dir / '04_under.tif'}, {out_dir / '04_residual.tif'}, report.json, {len(plots)} plots in {report['seconds']} s", flush=True)


if __name__ == "__main__":
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        main()
