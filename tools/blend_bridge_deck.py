"""Put each bridge deck into the same raster and clip as the road mesh.

The axis is the bridge piece in the shifted-centerline package (EPSG:31254).
Each cross-section of the surface model is read for the edge: a narrow rise is
a railing, a taller one a wall, and both end the driving surface. The gorge
ends it as well. A vehicle (a step above SPIKE_M) is a hole in the deck, not
an edge of it.

The width is one value per side over the whole piece: the median kerb offset
(a step of 8 to 35 cm across the road, read on at least KERB_MIN_RUN_M of
stations), else the GIP carriageway width; the railing limit (RAIL_PCT of the
per-station reach) can only narrow it. The deck is the axis height plus a
profile per offset; holes in either are closed by linear interpolation along
the road, then median- and Gaussian-filtered along the road. Over the last
metres before the abutment the deck is mixed into the repaired road surface.
One raster, one carriageway clip; the decks alone are the ``bridge_deck``
layer of the same file.

This is not tools/apply_bridge_deck.py. That experiment is rejected.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\blend_bridge_deck.py

Then the mesh, from this raster and this clip:

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\build_road_grid.py --heights "data\\processed\\tirol-imst-tarrenz-8192\\dgm_repair_transect\\03_with_bridges.tif" --clip "data\\processed\\tirol-imst-tarrenz-8192\\dgm_repair_transect\\carriageway_bridged.gpkg" --clip-layer carriageway
"""
from __future__ import annotations

import json
import math
import sys
import warnings
from pathlib import Path

import geopandas as gpd
import numpy as np
import tifffile as tiff
from scipy.ndimage import binary_dilation, gaussian_filter1d
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from filter_road_corridor import write_referenced_tif  # noqa: E402
from gip_bridge_flags import load_flags  # noqa: E402
from probe_bridge_dom import (  # noqa: E402
    RES_M,
    _bilinear,
    _densify,
    _extend,
    _load_dgm,
    _normals,
)
from site_coords import SiteCoords, load_site, processed_dir, site_slug  # noqa: E402
from wcs_terrain import fetch_terrain_coverage  # noqa: E402

STATION_M = 0.5
RAY_M = 0.25
SAMPLE_HALF_M = 14.0
APPROACH_M = 18.0
BLEND_M = 12.0
OPEN_M = 0.40
MIN_OPEN_M = 4.0
MIN_DECK_M = 2.5
SPIKE_M = 0.30
RAIL_MAX_M = 1.50
RISE_M = 0.45
GORGE_M = 1.20
SMOOTH_M = 4.0
PREFILTER_MAX_M = 15.0
PREFILTER_MIN_M = 5.0
# A kerb is a step across the road of this height, read over KERB_STEP_M and
# holding for KERB_HOLD_M behind it. Higher steps are a vehicle or a wall.
KERB_MIN_M = 0.08
KERB_MAX_M = 0.35
KERB_STEP_M = 0.75
KERB_HOLD_M = 0.75
# Stations with a kerb needed on the opening before the kerb sets the width.
# Otherwise the width is the GIP carriageway width.
KERB_MIN_RUN_M = 10.0
# The railing limit per side is this percentile of the per-station deck reach
# on the opening. A railing stands at every station, a parked row does not,
# so a high percentile is the free reach.
RAIL_PCT = 95.0


def _odd(length_m: float, step: float) -> int:
    n = int(round(length_m / step))
    if n % 2 == 0:
        n += 1
    return max(3, n)


def _median_along(grid: np.ndarray, win: int) -> np.ndarray:
    if grid.shape[1] < win:
        win = grid.shape[1] if grid.shape[1] % 2 == 1 else grid.shape[1] - 1
    if win < 3:
        return grid.copy()
    pad = win // 2
    padded = np.pad(grid, ((0, 0), (pad, pad)), constant_values=np.nan)
    windows = np.lib.stride_tricks.sliding_window_view(padded, win, axis=1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmedian(windows, axis=2)


def _median_1d(values: np.ndarray, win: int) -> np.ndarray:
    if len(values) < win:
        win = len(values) if len(values) % 2 == 1 else len(values) - 1
    if win < 3:
        return values.astype(np.float64, copy=True)
    pad = win // 2
    padded = np.pad(values.astype(np.float64), pad, constant_values=np.nan)
    windows = np.lib.stride_tricks.sliding_window_view(padded, win)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmedian(windows, axis=1)


def _smooth_along(grid: np.ndarray, sigma_px: float) -> np.ndarray:
    finite = np.isfinite(grid)
    filled = np.where(finite, grid, 0.0)
    weight = finite.astype(np.float64)
    num = gaussian_filter1d(filled, sigma_px, axis=1, mode="constant", cval=0.0)
    den = gaussian_filter1d(weight, sigma_px, axis=1, mode="constant", cval=0.0)
    out = np.full(grid.shape, np.nan, dtype=np.float64)
    ok = finite & (den > 0.25)
    out[ok] = num[ok] / den[ok]
    return out


def _runs(mask: np.ndarray, station: np.ndarray, min_len: float) -> list[tuple[int, int]]:
    found = []
    n = len(mask)
    i = 0
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i + 1
        while j < n and mask[j]:
            j += 1
        if float(station[j - 1] - station[i]) >= min_len:
            found.append((i, j))
        i = j
    return found


def _chain_segments(group) -> list[np.ndarray]:
    """Join structure pieces that share an endpoint. A gap starts a new chain."""
    chains: list[list[tuple[float, float]]] = []
    for geom in group.geometry:
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type == "MultiLineString":
            parts = list(geom.geoms)
        elif geom.geom_type == "LineString":
            parts = [geom]
        else:
            continue
        for part in parts:
            nxt = [(float(x), float(y)) for x, y in part.coords]
            if len(nxt) < 2:
                continue
            if not chains:
                chains.append(nxt)
                continue
            end = chains[-1][-1]
            if _near(end, nxt[0]):
                chains[-1].extend(nxt[1:])
            elif _near(end, nxt[-1]):
                chains[-1].extend(reversed(nxt[:-1]))
            else:
                chains.append(nxt)
    return [np.asarray(c, dtype=np.float64) for c in chains if len(c) >= 2]


def _near(a, b, tol: float = 2.0) -> bool:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 <= tol * tol


def _snap_box(xmin, ymin, xmax, ymax) -> tuple[float, float, float, float]:
    xmin = math.floor(xmin / RES_M) * RES_M
    ymin = math.floor(ymin / RES_M) * RES_M
    xmax = math.ceil(xmax / RES_M) * RES_M
    ymax = math.ceil(ymax / RES_M) * RES_M
    return xmin, ymin, xmax, ymax


def _covers(box, xmin, ymin, xmax, ymax) -> bool:
    return (
        len(box) == 4
        and box[0] <= xmin + 1.0
        and box[1] <= ymin + 1.0
        and box[2] >= xmax - 1.0
        and box[3] >= ymax - 1.0
    )


def _dom_frame(meta: dict, shape: tuple[int, int]) -> dict:
    """Bare WCS mosaic: row 0 is north. The meta bbox is the outer edge."""
    xmin, ymin, xmax, ymax = (float(v) for v in meta["bbox"])
    height, width = shape
    res_x = (xmax - xmin) / max(width, 1)
    res_y = (ymax - ymin) / max(height, 1)
    if abs(res_x - RES_M) > 1e-3 or abs(res_y - RES_M) > 1e-3:
        raise SystemExit(
            f"DOM pixel {res_x:.4f} x {res_y:.4f} m is not {RES_M}"
        )
    return {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax, "res": RES_M}


def _load_dom(site: dict, oid: int, xmin, ymin, xmax, ymax):
    xmin, ymin, xmax, ymax = _snap_box(xmin, ymin, xmax, ymax)
    path = ROOT / "data" / "raw" / f"dom_{site_slug(site)}_oid{oid}.tif"
    meta_path = path.with_suffix(".meta.json")

    def _read():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        dom = np.asarray(tiff.imread(path), dtype=np.float32)
        if dom.ndim == 3:
            dom = dom[..., 0]
        dom = dom.copy()
        dom[(~np.isfinite(dom)) | (dom < 50.0) | (dom > 4500.0)] = np.nan
        return dom, _dom_frame(meta, dom.shape)

    have = False
    if path.is_file() and meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        box = [float(v) for v in (meta.get("bbox") or [])]
        have = _covers(box, xmin, ymin, xmax, ymax)
        if have:
            width = int(meta.get("width") or 0)
            height = int(meta.get("height") or 0)
            if width and height and len(box) == 4:
                res_x = (box[2] - box[0]) / width
                res_y = (box[3] - box[1]) / height
                have = abs(res_x - RES_M) <= 1e-3 and abs(res_y - RES_M) <= 1e-3
    if not have:
        fetch_terrain_coverage(
            site,
            "dom",
            label=f"DOM {oid}",
            bbox=(xmin, ymin, xmax, ymax),
            resolution_m=RES_M,
            out_path=path,
            allow_incomplete=True,
            force=True,
        )
    return _read()


def _edge_at(z: np.ndarray, trend: np.ndarray, floor: float, j: int) -> dict:
    height = float(z[j] - floor) if np.isfinite(z[j]) else float(trend[j] - floor)
    kind = "rail" if height <= RAIL_MAX_M else "wall"
    return {"kind": kind, "height_m": round(height, 3)}


def _recovers(spike, gorge, trend, floor, j, direction, off) -> bool:
    reach = int(round(2.0 / RAY_M))
    n = len(trend)
    for step in range(1, reach + 1):
        k = j + direction * step
        if k < 0 or k >= n:
            return False
        if gorge[k] or not np.isfinite(trend[k]):
            return False
        if abs(float(off[k])) > abs(float(off[j])) + 2.0:
            return False
        if (not spike[k]) and abs(float(trend[k]) - floor) <= 0.35:
            return True
    return False


def _kerb(z: np.ndarray, off: np.ndarray, center: int, limit: int, direction: int) -> float:
    """Offset of the innermost kerb step on one side, NaN if none.

    Read on a short across-road median so the 0.5 m surface model still shows
    the step. The step must lie between KERB_MIN_M and KERB_MAX_M and the
    level behind it must stay in that band for KERB_HOLD_M; a vehicle or a
    wall behind the step fails the second test.
    """
    fine = _median_1d(z, 3)
    step_n = max(1, int(round(KERB_STEP_M / RAY_M)))
    hold_n = max(1, int(round(KERB_HOLD_M / RAY_M)))
    n = len(z)
    j = center + direction * step_n
    while 0 <= j < n and (j - limit) * direction <= 0:
        if abs(float(off[j])) >= 1.0:
            base = fine[j - direction * step_n]
            rise = fine[j] - base
            if np.isfinite(rise) and KERB_MIN_M <= rise <= KERB_MAX_M:
                ks = [j + direction * k for k in range(1, hold_n + 1)]
                ks = [k for k in ks if 0 <= k < n]
                after = fine[ks] - base if ks else np.array([])
                if (
                    after.size >= max(1, hold_n // 2)
                    and np.all(np.isfinite(after))
                    and np.all(after >= 0.75 * KERB_MIN_M)
                    and np.all(after <= KERB_MAX_M + 0.10)
                ):
                    return abs(float(off[j])) - 0.5 * KERB_STEP_M
        j += direction
    return np.nan


def _deck_section(
    z: np.ndarray, off: np.ndarray, half_cap: float, detect_m: float
) -> tuple[np.ndarray, list[dict], tuple[float, float], tuple[float, float]]:
    """Driving surface of one cross-section. Edge objects end it; a gorge ends it.

    The search runs out to ``detect_m`` so a railing just outside the catalogue
    width is still recorded. The deck itself stays inside ``half_cap``. Returns
    the surface (NaN where it is not deck, including vehicle pixels and their
    flank), the edge objects found, the kerb offset per side and the reach up
    to the edge object per side (positive offsets first; NaN if none).
    """
    deck = np.full(len(z), np.nan, dtype=np.float64)
    labels: list[dict] = []
    none = (np.nan, np.nan)
    finite = np.isfinite(z)
    if int(finite.sum()) < 5:
        return deck, labels, none, none
    trend = _median_1d(z, _odd(1.75, RAY_M))
    band = (np.abs(off) <= min(2.0, half_cap)) & np.isfinite(trend)
    if int(band.sum()) < 3:
        return deck, labels, none, none
    floor = float(np.median(trend[band]))
    center = int(np.argmin(np.where(band, np.abs(off), np.inf)))
    residual = z - trend
    spike = np.isfinite(residual) & (residual > SPIKE_M)
    gorge = (~np.isfinite(z)) | (z < floor - GORGE_M)
    # A vehicle wider than the across-road median window lifts the trend
    # itself, so it is no spike. Anything that high above the inner deck,
    # with a 0.5 m rim for the smeared flank, is not deck.
    high = finite & (z > floor + SPIKE_M)
    high_rim = binary_dilation(high, iterations=2)

    def walk(direction: int) -> int:
        last = center
        i = center
        while True:
            j = i + direction
            if j < 0 or j >= len(z):
                break
            if abs(float(off[j])) > detect_m:
                break
            if gorge[j] or not np.isfinite(trend[j]):
                break
            if float(trend[j]) > floor + RISE_M and abs(float(off[j])) > 1.25:
                rec = _edge_at(z, trend, floor, j)
                rec["side"] = "left" if direction < 0 else "right"
                rec["offset_m"] = round(float(off[j]), 2)
                labels.append(rec)
                break
            if spike[j]:
                if _recovers(spike, gorge, trend, floor, j, direction, off):
                    i = j
                    last = j
                    continue
                rec = _edge_at(z, trend, floor, j)
                rec["side"] = "left" if direction < 0 else "right"
                rec["offset_m"] = round(float(off[j]), 2)
                labels.append(rec)
                break
            i = j
            last = j
        return last

    left = walk(-1)
    right = walk(1)
    while left < center and abs(float(off[left])) > half_cap:
        left += 1
    while right > center and abs(float(off[right])) > half_cap:
        right -= 1
    if right <= left:
        return deck, labels, none, none
    kerbs = (_kerb(z, off, center, right, 1), _kerb(z, off, center, left, -1))
    # The reach is the deck up to the edge object, without the flank rim, so
    # a railing does not cost half a metre of width.
    idx = np.arange(len(z))
    solid = (idx >= left) & (idx <= right) & np.isfinite(trend) & ~gorge & ~high
    pos = idx[solid & (off >= 0.0)]
    neg = idx[solid & (off <= 0.0)]
    reach = (
        float(off[pos.max()]) if pos.size else np.nan,
        float(-off[neg.min()]) if neg.size else np.nan,
    )
    sl = slice(left, right + 1)
    # A narrow spike the walk stepped over (a vehicle that recovers within
    # 2 m) is not deck either; the along-road interpolation fills it.
    use = np.isfinite(trend[sl]) & ~gorge[sl] & ~spike[sl] & ~high_rim[sl]
    piece = np.where(use, trend[sl], np.nan)
    if int(np.isfinite(piece).sum()) < 3:
        return deck, labels, kerbs, reach
    deck[sl] = piece
    return deck, labels, kerbs, reach


def _smoothstep(t: np.ndarray) -> np.ndarray:
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _interp_along(values: np.ndarray, where: np.ndarray) -> np.ndarray:
    """Close holes along the station by linear interpolation between finite
    neighbours, inside ``where`` only and never beyond the outermost finite
    sample."""
    out = values.astype(np.float64, copy=True)
    idx = np.flatnonzero(where)
    if idx.size < 2:
        return out
    seg = out[idx]
    fin = np.isfinite(seg)
    if int(fin.sum()) < 2:
        return out
    lo, hi = idx[fin][0], idx[fin][-1]
    holes = idx[(~fin) & (idx >= lo) & (idx <= hi)]
    if holes.size:
        out[holes] = np.interp(holes, idx[fin], seg[fin])
    return out


def _hold_across(grid: np.ndarray, in_width: np.ndarray) -> np.ndarray:
    """Per station, fill empty offsets inside the width with the nearest
    finite value towards the axis (rows are offsets, columns stations)."""
    out = grid.copy()
    rows = np.flatnonzero(in_width)
    if rows.size == 0:
        return out
    mid = rows[np.argmin(np.abs(rows - (rows[0] + rows[-1]) / 2.0))]
    for k in range(mid + 1, rows[-1] + 1):
        hole = ~np.isfinite(out[k]) & np.isfinite(out[k - 1])
        out[k, hole] = out[k - 1, hole]
    for k in range(mid - 1, rows[0] - 1, -1):
        hole = ~np.isfinite(out[k]) & np.isfinite(out[k + 1])
        out[k, hole] = out[k + 1, hole]
    return out


def _side_half(kerb: np.ndarray, reach: np.ndarray, opening: np.ndarray,
               gip_half: float, half_cap: float) -> tuple[float, str, int, float]:
    """Constant half width of one side over the opening.

    The kerb median sets it when kerbs were read on at least KERB_MIN_RUN_M of
    stations, else the GIP half width. The railing limit (RAIL_PCT of the
    per-station reach) and half_cap can only make it narrower. Returns the
    half width, its source, the kerb station count and the railing limit.
    """
    k = kerb[opening]
    k = k[np.isfinite(k)]
    d = reach[opening]
    d = d[np.isfinite(d)]
    rail = min(half_cap, float(np.percentile(d, RAIL_PCT)) if d.size else half_cap)
    if k.size * STATION_M >= KERB_MIN_RUN_M:
        half, source = float(np.median(k)), "kerb"
    else:
        half, source = gip_half, "gip"
    if rail < half:
        half, source = rail, "rail"
    return max(half, 0.5 * MIN_DECK_M), source, int(k.size), rail


def _pair_halves(left: tuple, right: tuple, gip_half: float) -> tuple[float, float, str, str]:
    """When the railing cuts one GIP half short, the axis is off centre; give
    the deficit to the other side as far as its railing allows."""
    half_l, src_l, _, rail_l = left
    half_r, src_r, _, rail_r = right
    if src_l == "rail" and src_r == "gip":
        half_r = min(rail_r, gip_half + (gip_half - half_l))
        src_r = "gip+shift" if half_r > gip_half else src_r
    elif src_r == "rail" and src_l == "gip":
        half_l = min(rail_l, gip_half + (gip_half - half_r))
        src_l = "gip+shift" if half_l > gip_half else src_l
    return half_l, half_r, src_l, src_r


def _ribbon(xy, normal, station, half_l, half_r, keep) -> Polygon | None:
    valid = keep & np.isfinite(half_l) & np.isfinite(half_r) & ((half_l + half_r) >= MIN_DECK_M)
    runs = _runs(valid, station, MIN_OPEN_M)
    if not runs:
        return None
    i0, i1 = max(runs, key=lambda pair: station[pair[1] - 1] - station[pair[0]])
    left = xy[i0:i1] + normal[i0:i1] * half_l[i0:i1, None]
    right = xy[i0:i1] - normal[i0:i1] * half_r[i0:i1, None]
    ring = np.vstack([left, right[::-1]])
    if len(ring) < 4:
        return None
    poly = Polygon(ring)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty or poly.area < 8.0:
        return None
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda g: g.area)
    return poly


def _polys_of(geom) -> list:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom] if geom.area > 1.0 else []
    if geom.geom_type == "MultiPolygon":
        return [g for g in geom.geoms if g.geom_type == "Polygon" and g.area > 1.0]
    if geom.geom_type == "GeometryCollection":
        out = []
        for part in geom.geoms:
            out.extend(_polys_of(part))
        return out
    return []


def _stamp(raster: np.ndarray, frame: dict, xs, ys, zs) -> int:
    if xs.size == 0:
        return 0
    res = float(frame["res"])
    col = np.floor((xs - frame["xmin"]) / res).astype(np.int32)
    row = np.floor((frame["ymax"] - ys) / res).astype(np.int32)
    height, width = raster.shape
    ok = (col >= 0) & (row >= 0) & (col < width) & (row < height) & np.isfinite(zs)
    if not np.any(ok):
        return 0
    acc = np.zeros(raster.shape, dtype=np.float64)
    wgt = np.zeros(raster.shape, dtype=np.float64)
    np.add.at(acc, (row[ok], col[ok]), zs[ok])
    np.add.at(wgt, (row[ok], col[ok]), 1.0)
    hit = wgt > 0.0
    raster[hit] = (acc[hit] / wgt[hit]).astype(np.float32)
    return int(hit.sum())


def _summarize_edges(labels: list[dict]) -> dict:
    rails = [rec for rec in labels if rec["kind"] == "rail"]
    walls = [rec for rec in labels if rec["kind"] == "wall"]

    def _max(rows: list[dict]) -> float | None:
        if not rows:
            return None
        return round(max(rec["height_m"] for rec in rows), 3)

    return {
        "rail_samples": len(rails),
        "wall_samples": len(walls),
        "rail_max_m": _max(rails),
        "wall_max_m": _max(walls),
    }


def _one_chain(
    xy0: np.ndarray,
    *,
    oid: int,
    half_cap: float,
    gip_half: float,
    dom,
    dom_frame,
    road,
    road_frame,
    dgm,
    dgm_frame,
) -> dict | None:
    span = float(np.hypot(np.diff(xy0[:, 0]), np.diff(xy0[:, 1])).sum())
    if span < MIN_OPEN_M:
        return None
    extended = _extend(xy0, APPROACH_M)
    line, station = _densify(extended, STATION_M)
    station = station - APPROACH_M
    normal = _normals(line)
    offs = np.arange(-SAMPLE_HALF_M, SAMPLE_HALF_M + RAY_M * 0.5, RAY_M)
    n_st = len(line)
    n_off = len(offs)
    dom_g = np.full((n_off, n_st), np.nan, dtype=np.float64)
    road_g = np.full((n_off, n_st), np.nan, dtype=np.float64)
    dgm_g = np.full((n_off, n_st), np.nan, dtype=np.float64)
    for k, dist in enumerate(offs):
        pts = line + float(dist) * normal
        dom_g[k] = _bilinear(dom, dom_frame, pts[:, 0], pts[:, 1])
        road_g[k] = _bilinear(road, road_frame, pts[:, 0], pts[:, 1])
        dgm_g[k] = _bilinear(dgm, dgm_frame, pts[:, 0], pts[:, 1])

    win_m = min(PREFILTER_MAX_M, max(PREFILTER_MIN_M, span / 3.0))
    filtered = _median_along(dom_g, _odd(win_m, STATION_M))
    center = int(np.argmin(np.abs(offs)))
    inner = np.abs(offs) <= 1.0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        center_dom = np.nanmedian(filtered[inner], axis=0)
    center_dgm = dgm_g[center]
    # Only the structure itself is pure deck. An approach that is still a
    # little above the ground model is the blend into the repaired road.
    on_span = (station >= -1.0) & (station <= span + 1.0)
    open_pt = (
        np.isfinite(center_dom)
        & np.isfinite(center_dgm)
        & ((center_dom - center_dgm) > OPEN_M)
        & on_span
    )
    runs = _runs(open_pt, station, MIN_OPEN_M)
    if not runs:
        return {
            "oid": oid,
            "skipped": "no_opening",
            "span_m": round(span, 2),
        }
    opening = np.zeros(n_st, dtype=bool)
    for i0, i1 in runs:
        opening[i0:i1] = True
    open_idx = np.flatnonzero(opening)
    dist = np.min(np.abs(station[:, None] - station[open_idx][None, :]), axis=1)
    write = opening | (dist <= BLEND_M)

    # Edge objects are read on the raw cross-section. The along-road median
    # runs afterwards, so a railing that follows the span is still visible here
    # and a vehicle, which does not, falls out of the deck that remains.
    deck = np.full_like(dom_g, np.nan)
    labels: list[dict] = []
    reach_l = np.full(n_st, np.nan)
    reach_r = np.full(n_st, np.nan)
    kerb_l = np.full(n_st, np.nan)
    kerb_r = np.full(n_st, np.nan)
    for i in range(n_st):
        section, found, kerbs, reach = _deck_section(dom_g[:, i], offs, half_cap, SAMPLE_HALF_M)
        deck[:, i] = section
        kerb_l[i], kerb_r[i] = kerbs
        reach_l[i], reach_r[i] = reach
        if write[i]:
            labels.extend(found)

    # One width per side over the whole piece. A bridge does not change width
    # where a vehicle stands; the kerb or the catalogue width decides, the
    # railing can only narrow it.
    side_l = _side_half(kerb_l, reach_l, opening, gip_half, half_cap)
    side_r = _side_half(kerb_r, reach_r, opening, gip_half, half_cap)
    kerb_n_l, kerb_n_r = side_l[2], side_r[2]
    const_l, const_r, source_l, source_r = _pair_halves(side_l, side_r, gip_half)
    use_l = np.where(write, const_l, np.nan)
    use_r = np.where(write, const_r, np.nan)
    # Heights are built and stamped one sample past the edge, so every raster
    # cell whose centre lies inside the polygon gets a deck height.
    in_width = (offs >= -const_r - RAY_M - 1e-6) & (offs <= const_l + RAY_M + 1e-6)

    # Heights as axis line plus profile. A vehicle leaves a hole in the
    # profile that the neighbouring stations close along the road; the
    # crossfall of the deck is kept, not replaced by the axis height.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        axis_z = np.nanmedian(deck[inner], axis=0)
    axis_z = _interp_along(axis_z, write)
    rel = deck - axis_z[None, :]
    rel[~in_width, :] = np.nan
    filled_px = 0
    for k in np.flatnonzero(in_width):
        before = np.isfinite(rel[k])
        rel[k] = _interp_along(rel[k], write)
        filled_px += int(np.count_nonzero(np.isfinite(rel[k]) & ~before & write))
    # The flank rim beside a railing is empty at every station, so nothing
    # along the road can fill it. Hold the last profile value across.
    rel = _hold_across(rel, in_width)
    win_n = _odd(win_m, STATION_M)
    axis_z = _median_1d(axis_z, win_n)
    axis_z = _smooth_along(axis_z[None, :], SMOOTH_M / STATION_M)[0]
    rel = _median_along(rel, win_n)
    rel = _smooth_along(rel, SMOOTH_M / STATION_M)
    deck = axis_z[None, :] + rel

    weight = np.zeros(n_st, dtype=np.float64)
    outside = (~opening) & write
    weight[outside] = _smoothstep(dist[outside] / BLEND_M)
    blended = deck.copy()
    for i in np.flatnonzero(write):
        w = float(weight[i])
        if w <= 0.0:
            continue
        road_row = road_g[:, i]
        deck_row = blended[:, i]
        both = np.isfinite(deck_row) & np.isfinite(road_row)
        deck_row = deck_row.copy()
        deck_row[both] = (1.0 - w) * deck_row[both] + w * road_row[both]
        blended[:, i] = deck_row

    xs = []
    ys = []
    zs = []
    center_z = blended[center]
    axis_fill_px = 0
    # Stations 0.5 m apart on a skewed axis miss some 0.5 m raster cells.
    # The half stations in between close those gaps.
    mid_line = 0.5 * (line[:-1] + line[1:])
    mid_normal = normal[:-1] + normal[1:]
    mid_normal /= np.maximum(np.hypot(mid_normal[:, 0], mid_normal[:, 1]), 1e-9)[:, None]
    mid_grid = 0.5 * (blended[:, :-1] + blended[:, 1:])
    mid_write = write[:-1] & write[1:]
    passes = (
        (line, normal, blended, write, center_z, use_l, use_r),
        (
            mid_line,
            mid_normal,
            mid_grid,
            mid_write,
            0.5 * (center_z[:-1] + center_z[1:]),
            use_l[:-1],
            use_r[:-1],
        ),
    )
    for p_line, p_normal, p_grid, p_write, p_center, p_l, p_r in passes:
        for i in np.flatnonzero(p_write):
            lo = -float(p_r[i]) if np.isfinite(p_r[i]) else 0.0
            hi = float(p_l[i]) if np.isfinite(p_l[i]) else 0.0
            if hi - lo < MIN_DECK_M:
                continue
            take = (offs >= lo - RAY_M - 1e-6) & (offs <= hi + RAY_M + 1e-6)
            zz = p_grid[:, i].copy()
            # Last resort at the piece ends, where no neighbour exists to
            # interpolate from. Counted in the report.
            missing = take & ~np.isfinite(zz) & np.isfinite(p_center[i])
            axis_fill_px += int(np.count_nonzero(missing))
            zz[missing] = p_center[i]
            take = take & np.isfinite(zz)
            if not np.any(take):
                continue
            pts = p_line[i] + offs[take, None] * p_normal[i]
            xs.append(pts[:, 0])
            ys.append(pts[:, 1])
            zs.append(zz[take])
    if not xs:
        return {"oid": oid, "skipped": "empty_deck", "span_m": round(span, 2)}
    ribbon = _ribbon(line, normal, station, use_l, use_r, write)
    if ribbon is None:
        return {"oid": oid, "skipped": "no_ribbon", "span_m": round(span, 2)}

    on_open = opening & np.isfinite(center_z) & np.isfinite(center_dgm)
    mid = int(np.flatnonzero(opening)[len(np.flatnonzero(opening)) // 2]) if np.any(opening) else 0
    width = use_l + use_r
    width_open = width[opening & np.isfinite(width)]
    report = {
        "oid": oid,
        "span_m": round(span, 2),
        "opening_m": round(float(station[opening][-1] - station[opening][0]) if np.any(opening) else 0.0, 2),
        "deck_width_m": None if width_open.size == 0 else round(float(np.median(width_open)), 2),
        "gip_width_m": round(2.0 * gip_half, 2),
        "half_left_m": round(const_l, 2),
        "half_right_m": round(const_r, 2),
        "width_source_left": source_l,
        "width_source_right": source_r,
        "kerb_stations_left": kerb_n_l,
        "kerb_stations_right": kerb_n_r,
        "interpolated_px": filled_px,
        "axis_fill_px": axis_fill_px,
        "mid_deck_m": None if not np.isfinite(center_z[mid]) else round(float(center_z[mid]), 3),
        "mid_ground_m": None if not np.isfinite(center_dgm[mid]) else round(float(center_dgm[mid]), 3),
        "samples": int(sum(len(v) for v in xs)),
        "prefilter_m": round(win_m, 2),
        **_summarize_edges(labels),
    }
    if np.any(on_open):
        report["mid_clearance_m"] = round(float(np.median((center_z - center_dgm)[on_open])), 3)
    return {
        "report": report,
        "ribbon": ribbon,
        "xs": np.concatenate(xs),
        "ys": np.concatenate(ys),
        "zs": np.concatenate(zs),
        "line": LineString(xy0),
        "half_cap": half_cap,
    }


def _bridge_groups(segments, flags: dict) -> list[tuple[int, object, float]]:
    groups = []
    for key, rec in flags.items():
        if not rec.get("bridge"):
            continue
        try:
            oid = int(key)
        except (TypeError, ValueError):
            continue
        rows = segments[segments.objectid == oid]
        if rows.empty:
            continue
        marked = rows[rows.structure == 1]
        use = marked if not marked.empty else rows
        use = use.sort_values("seq") if "seq" in use.columns else use
        width = float(np.median(use.width_m.to_numpy(dtype=float)))
        if not np.isfinite(width) or width < 2.0:
            width = 8.0
        groups.append((oid, use, width))
    return groups


def _cut_clip(roads: gpd.GeoDataFrame, zones: dict[int, object], ribbons: list[tuple[int, Polygon]]):
    geoms = []
    oids = []
    for rec in roads.itertuples(index=False):
        geom = rec.geometry
        if geom is None or geom.is_empty:
            continue
        try:
            oid = int(rec.objectid)
        except (TypeError, ValueError):
            oid = -1
        zone = zones.get(oid)
        if zone is not None and geom.intersects(zone):
            geom = geom.difference(zone)
        for part in _polys_of(geom):
            geoms.append(part)
            oids.append(oid)
    for oid, ribbon in ribbons:
        for part in _polys_of(ribbon):
            geoms.append(part)
            oids.append(oid)
    return geoms, oids


def main() -> None:
    only = None
    if "--oid" in sys.argv:
        only = int(sys.argv[sys.argv.index("--oid") + 1])
    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    flags = load_flags(site)
    if not flags:
        raise SystemExit("gip_bridge_flags.json missing")
    repaired_path = proc / "dgm_repair_transect" / "02_repaired.tif"
    raw_path = proc / "corridor50_raw" / "corridor50_raw.tif"
    merged = proc / "centerline_shift_taper" / "centerline_shift_with_sg.gpkg"
    gpkg = merged if merged.is_file() else proc / "centerline_shift_taper" / "centerline_shift.gpkg"
    for path in (repaired_path, raw_path, gpkg):
        if not path.is_file():
            raise SystemExit(f"missing {path}")

    road, road_frame = _load_dgm(repaired_path)
    dgm, dgm_frame = _load_dgm(raw_path)
    segments = gpd.read_file(gpkg, layer="segments")
    roads = gpd.read_file(gpkg, layer="carriageway")
    out = road.copy()
    zones: dict[int, object] = {}
    ribbons: list[tuple[int, Polygon]] = []
    reports = []
    pad = SAMPLE_HALF_M + 2.0

    for oid, group, width in _bridge_groups(segments, flags):
        if only is not None and oid != only:
            continue
        half_cap = width * 0.5 + 1.0
        pieces = []
        for chain in _chain_segments(group):
            xmin = float(chain[:, 0].min()) - pad
            xmax = float(chain[:, 0].max()) + pad
            ymin = float(chain[:, 1].min()) - pad
            ymax = float(chain[:, 1].max()) + pad
            # The approach extension sits outside the piece. Cover it.
            extra = APPROACH_M + 2.0
            dom, dom_frame = _load_dom(
                site, oid, xmin - extra, ymin - extra, xmax + extra, ymax + extra
            )
            built = _one_chain(
                chain,
                oid=oid,
                half_cap=half_cap,
                gip_half=width * 0.5,
                dom=dom,
                dom_frame=dom_frame,
                road=road,
                road_frame=road_frame,
                dgm=dgm,
                dgm_frame=dgm_frame,
            )
            if built is None:
                continue
            if built.get("skipped"):
                reports.append(built)
                print(f"oid {oid} skipped: {built['skipped']}", flush=True)
                continue
            pieces.append(built)
        if not pieces:
            continue
        zone_parts = []
        for rec in group.itertuples(index=False):
            geom = rec.geometry
            if geom is None or geom.is_empty:
                continue
            zone_parts.append(geom.buffer(half_cap, cap_style="flat"))
        zone = unary_union(zone_parts) if zone_parts else None
        if zone is not None and not zone.is_empty:
            zones[oid] = zone
            # The clip drops this footprint from the bridge polygon. Do not
            # blank the raster: a Gemeindestraße beside the bridge shares
            # those cells and would lose its height.
        for built in pieces:
            _stamp(out, road_frame, built["xs"], built["ys"], built["zs"])
            ribbons.append((oid, built["ribbon"]))
            reports.append(built["report"])
            gap = None
            if not roads.empty:
                gap = float(built["ribbon"].distance(unary_union(list(roads.geometry))))
            built["report"]["approach_gap_m"] = None if gap is None else round(gap, 2)
            print(
                f"oid {oid} opening {built['report'].get('opening_m')} m "
                f"width {built['report'].get('deck_width_m')} m "
                f"(gip {built['report'].get('gip_width_m')}, "
                f"L {built['report'].get('width_source_left')} "
                f"R {built['report'].get('width_source_right')}, "
                f"interpolated {built['report'].get('interpolated_px')} px) "
                f"rail {built['report'].get('rail_samples')} "
                f"wall {built['report'].get('wall_samples')} "
                f"gap {built['report'].get('approach_gap_m')}",
                flush=True,
            )

    out_dir = proc / "dgm_repair_transect"
    out_dir.mkdir(parents=True, exist_ok=True)
    height_path = out_dir / "03_with_bridges.tif"
    write_referenced_tif(
        height_path,
        out,
        xmin=float(road_frame["xmin"]),
        ymax=float(road_frame["ymax"]),
        res=float(road_frame["res"]),
        epsg=int(str(sc.crs).split(":")[-1]),
    )
    geoms, oids = _cut_clip(roads, zones, ribbons)
    clip_path = out_dir / "carriageway_bridged.gpkg"
    if clip_path.exists():
        clip_path.unlink()
    gpd.GeoDataFrame(
        {"objectid": oids},
        geometry=geoms,
        crs="EPSG:31254",
    ).to_file(clip_path, layer="carriageway", driver="GPKG")
    # The decks alone, for steps that must leave the bridge surface untouched
    # (smooth_road_surface.py protects these polygons, not only the GIP zone).
    if ribbons:
        gpd.GeoDataFrame(
            {"objectid": [oid for oid, _ in ribbons]},
            geometry=[ribbon for _, ribbon in ribbons],
            crs="EPSG:31254",
        ).to_file(clip_path, layer="bridge_deck", driver="GPKG")
    report_path = out_dir / "bridge_deck_report.json"
    report_path.write_text(json.dumps({"bridges": reports}, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {height_path}", flush=True)
    print(f"Wrote {clip_path} polygons={len(geoms)}", flush=True)
    print(f"Wrote {report_path}", flush=True)


if __name__ == "__main__":
    main()
