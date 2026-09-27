"""Carriageway cut from orthophoto color, for inspection in QGIS.

The centerline is taken as lying on the road. A cross-section marks asphalt
where the photo is gray and not in deep shadow. Constant stretches of that
edge are joined. Gaps up to 50 m are interpolated between those stretches.
Longer gaps use the mean half-width of the Landnutzung road surface.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\measure_ortho_edges.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import tifffile as tiff
from shapely.geometry import LineString, Polygon
from shapely import make_valid
from shapely.strtree import STRtree

from build_asphalt_meshroads import _is_main_road
from build_decal_roads import _cfg, stitch_abutting_roads
from diag_road_bed_modes import _gip_polylines
from fetch_road_corridor_ortho import ORIGIN_E, ORIGIN_N, RES_M, SPAN_M, TILE_PX
from measure_gip_widths import _ln_to_31254, _pick_poly, find_landnutzung_geojson, load_street_polygons
from measure_road_breaks import _densify, _oid, _oid_num, _poly_offset, _tangents
from site_coords import SiteCoords, load_site, processed_dir, site_slug

ROOT = Path(__file__).resolve().parents[1]
STATION_M = 1.0
SAMPLE_M = 0.25
SEARCH_M = 14.0
ROAD_SAT = 0.12
ROAD_LUM_MIN = 65.0
ROAD_LUM_MAX = 230.0
HOLE_M = 1.0
CENTER_ATTACH_M = 1.5
MIN_RUN_M = 2.0
CONST_RANGE_M = 0.8
CONST_MIN_M = 15.0
JOIN_TOL_M = 0.8
GAP_MEAN_M = 50.0


def _load_ortho(folder: Path) -> dict[tuple[int, int], np.ndarray]:
    cache: dict[tuple[int, int], np.ndarray] = {}
    for path in folder.glob("r*_c*.tif"):
        stem = path.stem
        row_s, col_s = stem.split("_c")
        cache[(int(row_s[1:]), int(col_s))] = np.asarray(tiff.imread(path))
    return cache


def _sample_rgb(east: np.ndarray, north: np.ndarray, cache: dict) -> np.ndarray:
    east = np.asarray(east, dtype=np.float64).ravel()
    north = np.asarray(north, dtype=np.float64).ravel()
    rgb = np.full((east.size, 3), np.nan, dtype=np.float32)
    col = np.floor((east - ORIGIN_E) / SPAN_M).astype(np.int32)
    row = np.floor((ORIGIN_N - north) / SPAN_M).astype(np.int32)
    key = row.astype(np.int64) * 100000 + col.astype(np.int64)
    order = np.argsort(key, kind="mergesort")
    key_s = key[order]
    cuts = np.flatnonzero(np.diff(key_s)) + 1
    starts = np.concatenate([[0], cuts])
    stops = np.concatenate([cuts, [key_s.size]])
    for a, b in zip(starts, stops):
        idx = order[a:b]
        rr = int(row[idx[0]])
        cc = int(col[idx[0]])
        image = cache.get((rr, cc))
        if image is None:
            continue
        west = ORIGIN_E + cc * SPAN_M
        north0 = ORIGIN_N - rr * SPAN_M
        px = np.rint((east[idx] - west) / RES_M - 0.5).astype(np.int32)
        py = np.rint((north0 - north[idx]) / RES_M - 0.5).astype(np.int32)
        ok = (px >= 0) & (py >= 0) & (px < TILE_PX) & (py < TILE_PX)
        if np.any(ok):
            rgb[idx[ok]] = image[py[ok], px[ok]]
    return rgb


def _road_mask(rgb: np.ndarray) -> np.ndarray:
    red, green, blue = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    lum = 0.3 * red + 0.5 * green + 0.2 * blue
    mx = np.max(rgb, axis=1)
    mn = np.min(rgb, axis=1)
    sat = np.divide(mx - mn, mx, out=np.zeros(rgb.shape[0], dtype=np.float32), where=mx > 8)
    finite = np.isfinite(lum)
    return finite & (sat < ROAD_SAT) & (lum >= ROAD_LUM_MIN) & (lum <= ROAD_LUM_MAX)


def _fill_holes(mask: np.ndarray, step_m: float) -> np.ndarray:
    out = mask.copy()
    hole = max(1, int(round(HOLE_M / step_m)))
    n = out.size
    i = 0
    while i < n:
        if out[i]:
            i += 1
            continue
        j = i
        while j < n and not out[j]:
            j += 1
        if i > 0 and j < n and out[i - 1] and out[j] and (j - i) <= hole:
            out[i:j] = True
        i = j
    return out


def _side_edges(mask: np.ndarray, offsets: np.ndarray) -> tuple[float, float]:
    """Positive metres to the left and to the right of the carriageway run."""
    n = mask.size
    runs: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and mask[j + 1]:
            j += 1
        if abs(float(offsets[j] - offsets[i])) >= MIN_RUN_M:
            runs.append((i, j))
        i = j + 1
    if not runs:
        return float("nan"), float("nan")
    best = None
    for i0, i1 in runs:
        lo = float(offsets[i0])
        hi = float(offsets[i1])
        if lo <= 0.0 <= hi:
            dist = 0.0
        elif hi < 0.0:
            dist = -hi
        else:
            dist = lo
        if dist <= CENTER_ATTACH_M and (best is None or dist < best[0]):
            best = (dist, lo, hi)
    if best is None:
        return float("nan"), float("nan")
    _dist, lo, hi = best
    left = hi if hi > 0.0 else float("nan")
    right = -lo if lo < 0.0 else float("nan")
    return left, right


def _constant_runs(offsets: np.ndarray, station_s: np.ndarray) -> list[dict]:
    runs: list[dict] = []
    n = offsets.size
    i = 0
    while i < n:
        if not math.isfinite(float(offsets[i])):
            i += 1
            continue
        j = i
        lo = hi = float(offsets[i])
        while j + 1 < n and math.isfinite(float(offsets[j + 1])):
            value = float(offsets[j + 1])
            nlo = min(lo, value)
            nhi = max(hi, value)
            if nhi - nlo > CONST_RANGE_M:
                break
            lo, hi = nlo, nhi
            j += 1
        if float(station_s[j] - station_s[i]) >= CONST_MIN_M:
            window = offsets[i : j + 1]
            runs.append(
                {
                    "i0": i,
                    "i1": j,
                    "s0": float(station_s[i]),
                    "s1": float(station_s[j]),
                    "w": float(np.median(window)),
                }
            )
        i = j + 1
    return runs


def _merge_runs(runs: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for run in runs:
        if (
            merged
            and abs(run["w"] - merged[-1]["w"]) <= JOIN_TOL_M
            and run["s0"] - merged[-1]["s1"] <= GAP_MEAN_M
        ):
            prev = merged[-1]
            span_a = max(prev["s1"] - prev["s0"], STATION_M)
            span_b = max(run["s1"] - run["s0"], STATION_M)
            prev["w"] = (prev["w"] * span_a + run["w"] * span_b) / (span_a + span_b)
            prev["s1"] = run["s1"]
            prev["i1"] = run["i1"]
        else:
            merged.append(dict(run))
    return merged


def _paint(offsets: np.ndarray, station_s: np.ndarray, mean_half: float) -> tuple[np.ndarray, list[str]]:
    n = offsets.size
    chosen = np.full(n, np.nan, dtype=np.float64)
    source = [""] * n
    in_run = np.zeros(n, dtype=bool)
    for run in _constant_runs(offsets, station_s):
        in_run[run["i0"] : run["i1"] + 1] = True
    pieces = _merge_runs(_constant_runs(offsets, station_s))
    for piece in pieces:
        for i in range(piece["i0"], piece["i1"] + 1):
            chosen[i] = piece["w"]
            source[i] = "color" if in_run[i] else "joined"

    def fill(i0: int, i1: int, kind: str, w0: float, w1: float) -> None:
        if i1 <= i0:
            return
        span = float(station_s[i1] - station_s[i0]) if i1 < n else 0.0
        for i in range(i0, i1):
            if kind == "interpolated" and span > 1e-6:
                t = (float(station_s[i]) - float(station_s[i0])) / span
                chosen[i] = w0 + t * (w1 - w0)
            else:
                chosen[i] = w0
            source[i] = kind

    if not pieces:
        if math.isfinite(mean_half):
            chosen[:] = mean_half
            source = ["mean_width"] * n
        return chosen, source

    first = pieces[0]
    lead = float(station_s[first["i0"]] - station_s[0])
    if lead > GAP_MEAN_M and math.isfinite(mean_half):
        fill(0, first["i0"], "mean_width", mean_half, mean_half)
    else:
        fill(0, first["i0"], "joined", first["w"], first["w"])

    for left, right in zip(pieces, pieces[1:]):
        gap = float(right["s0"] - left["s1"])
        if gap > GAP_MEAN_M and math.isfinite(mean_half):
            fill(left["i1"] + 1, right["i0"], "mean_width", mean_half, mean_half)
        elif abs(left["w"] - right["w"]) <= JOIN_TOL_M:
            fill(left["i1"] + 1, right["i0"], "joined", left["w"], left["w"])
        else:
            fill(left["i1"], right["i0"], "interpolated", left["w"], right["w"])
            # fill() includes i0, which already holds the piece width. Restore it.
            chosen[left["i1"]] = left["w"]
            source[left["i1"]] = "color" if in_run[left["i1"]] else "joined"

    last = pieces[-1]
    trail = float(station_s[-1] - station_s[last["i1"]])
    if trail > GAP_MEAN_M and math.isfinite(mean_half):
        fill(last["i1"] + 1, n, "mean_width", mean_half, mean_half)
    else:
        fill(last["i1"] + 1, n, "joined", last["w"], last["w"])
    return chosen, source


def _lines(rows: list[dict], side: str) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["oid"], []).append(row)
    out: list[dict] = []
    for group in groups.values():
        run: list[dict] = []

        def emit() -> None:
            if len(run) < 2:
                run.clear()
                return
            out.append(
                {
                    "objectid": _oid_num(run[0]["oid"]),
                    "piece": run[0]["oid"],
                    "str_code": run[0]["str_code"],
                    "side": side,
                    "source": run[0][f"{side}_source"],
                    "width_m": round(float(run[0][f"{side}_m"]), 3),
                    "s0_m": round(float(run[0]["s_m"]), 2),
                    "s1_m": round(float(run[-1]["s_m"]), 2),
                    "geometry": LineString(
                        [(float(item[f"{side}_x"]), float(item[f"{side}_y"])) for item in run]
                    ),
                }
            )
            run.clear()

        prev_s = None
        prev_src = None
        for row in group:
            width = row[f"{side}_m"]
            src = row[f"{side}_source"]
            if not math.isfinite(width) or not src:
                emit()
                prev_s = None
                prev_src = None
                continue
            gap = prev_s is not None and float(row["s_m"]) - float(prev_s) > 1.5
            if run and (src != prev_src or gap):
                emit()
            run.append(row)
            prev_s = float(row["s_m"])
            prev_src = src
        emit()
    return out


def _raw_lines(rows: list[dict], side: str) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["oid"], []).append(row)
    out: list[dict] = []
    for group in groups.values():
        run: list[dict] = []

        def emit() -> None:
            if len(run) < 2:
                run.clear()
                return
            out.append(
                {
                    "objectid": _oid_num(run[0]["oid"]),
                    "piece": run[0]["oid"],
                    "str_code": run[0]["str_code"],
                    "side": side,
                    "geometry": LineString(
                        [(float(item[f"{side}_raw_x"]), float(item[f"{side}_raw_y"])) for item in run]
                    ),
                }
            )
            run.clear()

        prev_s = None
        for row in group:
            if not math.isfinite(row[f"{side}_raw_m"]):
                emit()
                prev_s = None
                continue
            if prev_s is not None and float(row["s_m"]) - float(prev_s) > 1.5:
                emit()
            run.append(row)
            prev_s = float(row["s_m"])
        emit()
    return out


def _ribbons(rows: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["oid"], []).append(row)
    out: list[dict] = []

    def emit(run: list[dict]) -> None:
        if len(run) < 2:
            return
        coords = [(float(row["left_x"]), float(row["left_y"])) for row in run]
        coords += [(float(row["right_x"]), float(row["right_y"])) for row in reversed(run)]
        poly = Polygon(coords)
        if poly.is_empty:
            return
        if not poly.is_valid:
            poly = make_valid(poly)
        parts = [poly] if poly.geom_type == "Polygon" else [
            part for part in getattr(poly, "geoms", []) if getattr(part, "geom_type", "") == "Polygon"
        ]
        for part in parts:
            if part.area < 1.0:
                continue
            out.append(
                {
                    "objectid": _oid_num(run[0]["oid"]),
                    "piece": run[0]["oid"],
                    "str_code": run[0]["str_code"],
                    "s0_m": round(float(run[0]["s_m"]), 2),
                    "s1_m": round(float(run[-1]["s_m"]), 2),
                    "geometry": part,
                }
            )

    for group in groups.values():
        run: list[dict] = []
        prev_s = None
        for row in group:
            ok = math.isfinite(row["left_m"]) and math.isfinite(row["right_m"])
            gap = prev_s is not None and float(row["s_m"]) - float(prev_s) > 1.5
            if run and (not ok or gap):
                emit(run)
                run = []
                prev_s = None
            if not ok:
                continue
            run.append(row)
            prev_s = float(row["s_m"])
        emit(run)
    return out


def _profiles(crs: np.ndarray, left: np.ndarray, cache: dict) -> tuple[np.ndarray, np.ndarray]:
    offsets = np.arange(-SEARCH_M, SEARCH_M + 1e-9, SAMPLE_M)
    east = crs[:, None, 0] + left[:, None, 0] * offsets[None, :]
    north = crs[:, None, 1] + left[:, None, 1] * offsets[None, :]
    rgb = _sample_rgb(east.ravel(), north.ravel(), cache).reshape(east.shape[0], offsets.size, 3)
    left_m = np.full(crs.shape[0], np.nan)
    right_m = np.full(crs.shape[0], np.nan)
    for i in range(crs.shape[0]):
        mask = _fill_holes(_road_mask(rgb[i]), SAMPLE_M)
        left_m[i], right_m[i] = _side_edges(mask, offsets)
    return left_m, right_m


def _recorded_lines(roads: list[dict], sc: SiteCoords) -> list[dict]:
    """Symmetric edges at the width stored on the GIP nodes."""
    out: list[dict] = []
    for index, road in enumerate(roads):
        pts = road.get("pts") or []
        packed = _densify(pts, sc, STATION_M)
        if packed is None or len(pts) < 2:
            continue
        station_s, _beam, crs = packed
        weights = np.array(
            [float(p[3]) if len(p) > 3 else float("nan") for p in pts], dtype=np.float64
        )
        cxy = np.array(
            [sc.beamng_to_crs(float(p[0]), float(p[1])) for p in pts], dtype=np.float64
        )
        seg = np.hypot(np.diff(cxy[:, 0]), np.diff(cxy[:, 1]))
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        idx = np.clip(np.searchsorted(cum, station_s, side="right") - 1, 0, len(seg) - 1)
        seg_len = seg[idx]
        t = np.divide(
            station_s - cum[idx], seg_len, out=np.zeros_like(station_s), where=seg_len > 1e-9
        )
        width = weights[idx] * (1.0 - t) + weights[np.minimum(idx + 1, len(pts) - 1)] * t
        tangent = _tangents(crs)
        left = np.column_stack([-tangent[:, 1], tangent[:, 0]])
        oid = _oid(road, index)
        code = str(road.get("str_code") or "")
        for side, sign in (("left", 1.0), ("right", -1.0)):
            run: list[tuple[float, float]] = []
            run_w: list[float] = []
            s0 = float(station_s[0])
            s1 = s0

            def emit() -> None:
                if len(run) < 2:
                    run.clear()
                    run_w.clear()
                    return
                out.append(
                    {
                        "objectid": _oid_num(oid),
                        "piece": oid,
                        "str_code": code,
                        "side": side,
                        "source": "recorded",
                        "width_m": round(float(np.median(run_w)), 3),
                        "half_m": round(float(np.median(run_w)) * 0.5, 3),
                        "s0_m": round(s0, 2),
                        "s1_m": round(s1, 2),
                        "geometry": LineString(run),
                    }
                )
                run.clear()
                run_w.clear()

            prev_bucket = None
            for k in range(crs.shape[0]):
                full = float(width[k])
                if not math.isfinite(full) or full < 1.5:
                    emit()
                    prev_bucket = None
                    continue
                bucket = round(full, 2)
                if run and prev_bucket is not None and bucket != prev_bucket:
                    emit()
                if not run:
                    s0 = float(station_s[k])
                half = 0.5 * full
                run.append(
                    (
                        float(crs[k, 0]) + sign * float(left[k, 0]) * half,
                        float(crs[k, 1]) + sign * float(left[k, 1]) * half,
                    )
                )
                run_w.append(full)
                s1 = float(station_s[k])
                prev_bucket = bucket
            emit()
    return out


def main() -> None:
    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    cfg = _cfg(site.get("beamng") or {})
    roads = [
        road
        for road in stitch_abutting_roads(_gip_polylines(site, proc), cfg)
        if _is_main_road(road, site)
    ]
    ortho_dir = ROOT / "data" / "raw" / f"ortho_{site_slug(site)}_corridor_wmts"
    if not ortho_dir.is_dir():
        raise SystemExit(f"missing orthophoto tiles: {ortho_dir}")
    print("loading orthophoto tiles", flush=True)
    cache = _load_ortho(ortho_dir)
    print(f"  {len(cache)} tiles", flush=True)

    path = find_landnutzung_geojson(site)
    if path is None:
        raise SystemExit("no Landnutzung geojson for this site")
    geoms, codes = load_street_polygons(path, _ln_to_31254())
    tree = STRtree(geoms)

    rows: list[dict] = []
    half_samples: list[float] = []
    for index, road in enumerate(roads):
        packed = _densify(road.get("pts") or [], sc, STATION_M)
        if packed is None:
            continue
        station_s, _beam, crs = packed
        tangent = _tangents(crs)
        left = np.column_stack([-tangent[:, 1], tangent[:, 0]])
        raw_left, raw_right = _profiles(crs, left, cache)
        oid = _oid(road, index)
        code = str(road.get("str_code") or "")
        obj = str(road.get("objekt") or "")
        poly_left = np.full(station_s.shape[0], np.nan)
        poly_right = np.full(station_s.shape[0], np.nan)
        for k in range(station_s.shape[0]):
            poly, _ln = _pick_poly(
                tree, geoms, codes, float(crs[k, 0]), float(crs[k, 1]), obj
            )
            poly_left[k] = _poly_offset(poly, float(crs[k, 0]), float(crs[k, 1]), float(left[k, 0]), float(left[k, 1]))
            poly_right[k] = _poly_offset(
                poly, float(crs[k, 0]), float(crs[k, 1]), -float(left[k, 0]), -float(left[k, 1])
            )
        both = np.isfinite(poly_left) & np.isfinite(poly_right)
        if np.any(both):
            mean_half = float(np.mean(0.5 * (poly_left[both] + poly_right[both])))
        else:
            mean_half = float("nan")
        if math.isfinite(mean_half):
            half_samples.append(mean_half)
        left_m, left_src = _paint(raw_left, station_s, mean_half)
        right_m, right_src = _paint(raw_right, station_s, mean_half)
        for k in range(station_s.shape[0]):
            cx, cy = float(crs[k, 0]), float(crs[k, 1])
            lx, ly = float(left[k, 0]), float(left[k, 1])
            row = {
                "oid": oid,
                "str_code": code,
                "s_m": round(float(station_s[k]), 2),
                "mean_half_m": mean_half,
                "cx": cx,
                "cy": cy,
                "lx": lx,
                "ly": ly,
                "left_raw_m": float(raw_left[k]),
                "right_raw_m": float(raw_right[k]),
                "left_m": float(left_m[k]),
                "right_m": float(right_m[k]),
                "left_source": left_src[k],
                "right_source": right_src[k],
            }
            for side, sign, width_key, raw_key in (
                ("left", 1.0, "left_m", "left_raw_m"),
                ("right", -1.0, "right_m", "right_raw_m"),
            ):
                width = row[width_key]
                raw = row[raw_key]
                row[f"{side}_x"] = cx + sign * lx * width if math.isfinite(width) else float("nan")
                row[f"{side}_y"] = cy + sign * ly * width if math.isfinite(width) else float("nan")
                row[f"{side}_raw_x"] = cx + sign * lx * raw if math.isfinite(raw) else float("nan")
                row[f"{side}_raw_y"] = cy + sign * ly * raw if math.isfinite(raw) else float("nan")
            rows.append(row)
        print(
            f"  {index + 1}/{len(roads)} oid={_oid_num(oid)} {code} "
            f"stations={station_s.shape[0]} mean_half={mean_half:.2f}"
            if math.isfinite(mean_half)
            else f"  {index + 1}/{len(roads)} oid={_oid_num(oid)} {code} stations={station_s.shape[0]}",
            flush=True,
        )

    if not rows:
        raise SystemExit("no stations")
    site_mean = float(np.mean(half_samples)) if half_samples else float("nan")
    for row in rows:
        if not math.isfinite(row["mean_half_m"]):
            row["mean_half_m"] = site_mean
        if not math.isfinite(site_mean):
            continue
        for side, sign in (("left", 1.0), ("right", -1.0)):
            if row[f"{side}_source"]:
                continue
            row[f"{side}_m"] = site_mean
            row[f"{side}_source"] = "mean_width"
            row[f"{side}_x"] = row["cx"] + sign * row["lx"] * site_mean
            row[f"{side}_y"] = row["cy"] + sign * row["ly"] * site_mean

    out_dir = proc / "ortho_edges"
    out_dir.mkdir(parents=True, exist_ok=True)
    edges = _lines(rows, "left") + _lines(rows, "right")
    raw = _raw_lines(rows, "left") + _raw_lines(rows, "right")
    ribbons = _ribbons(rows)
    recorded = _recorded_lines(roads, sc)
    recorded_path = out_dir / "recorded_widths.gpkg"
    if recorded_path.exists():
        try:
            recorded_path.unlink()
        except OSError:
            recorded_path = out_dir / "recorded_widths_new.gpkg"
    gpd.GeoDataFrame(recorded, geometry="geometry", crs="EPSG:31254").to_file(
        recorded_path, layer="recorded", driver="GPKG"
    )
    gpkg = out_dir / "ortho_edges.gpkg"
    try:
        if gpkg.exists():
            gpkg.unlink()
    except OSError:
        print(f"ortho_edges.gpkg is open, left it unchanged. Recorded widths: {recorded_path}", flush=True)
        print(f"  recorded parts={len(recorded)}", flush=True)
        return
    gpd.GeoDataFrame(edges, geometry="geometry", crs="EPSG:31254").to_file(gpkg, layer="edges", driver="GPKG")
    gpd.GeoDataFrame(raw, geometry="geometry", crs="EPSG:31254").to_file(
        gpkg, layer="raw_edges", driver="GPKG"
    )
    gpd.GeoDataFrame(ribbons, geometry="geometry", crs="EPSG:31254").to_file(
        gpkg, layer="ribbon", driver="GPKG"
    )
    gpd.GeoDataFrame(recorded, geometry="geometry", crs="EPSG:31254").to_file(
        gpkg, layer="recorded", driver="GPKG"
    )

    def counts(side: str) -> dict[str, int]:
        found: dict[str, int] = {}
        for row in rows:
            name = row[f"{side}_source"] or "(none)"
            found[name] = found.get(name, 0) + 1
        return found

    report = {
        "what": (
            "Orthophoto color edge of gray asphalt, ignoring deep shadow. "
            "Constant stretches (range <= 0.8 m over at least 15 m) with a "
            "similar width are joined. Gaps up to 50 m are interpolated. "
            "Longer gaps use the mean half-width of the Landnutzung surface."
        ),
        "crs": "EPSG:31254",
        "const_range_m": CONST_RANGE_M,
        "const_min_m": CONST_MIN_M,
        "join_tol_m": JOIN_TOL_M,
        "gap_mean_m": GAP_MEAN_M,
        "site_mean_half_m": None if not math.isfinite(site_mean) else round(site_mean, 3),
        "stations": len(rows),
        "left_source": counts("left"),
        "right_source": counts("right"),
        "layers": {
            "edges": "cut line, attribute source = color | joined | interpolated | mean_width",
            "raw_edges": "color edge before the constant-width pattern",
            "ribbon": "band between the cut lines",
            "recorded": "edges at the width stored on the GIP nodes, symmetric",
        },
        "gpkg": str(gpkg),
    }
    (out_dir / "ortho_edges_index.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Wrote {gpkg} edges={len(edges)} raw={len(raw)} ribbons={len(ribbons)} "
        f"recorded={len(recorded)} stations={len(rows)}",
        flush=True,
    )
    print(f"  left  {report['left_source']}", flush=True)
    print(f"  right {report['right_source']}", flush=True)


if __name__ == "__main__":
    main()
