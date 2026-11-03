"""Where the raw DGM still looks like road, out to either side of a GIP link.

The links are the Austrian pieces left by ``clip_gip_swiss``. Stations stay
on the same 2.5 m spacing as the grade check. At each station the grade test
is repeated every 0.5 m to the side, out to the GIP carriageway width on the
left and again on the right. The slope limit is the one already stored for
that functional class by the grade check. S-B is held at 7.5 percent.
S-L loses 6 percentage points from that limit, S-GW and Weg lose 1.

A sample passes when it is not on water and its grade is at or below the
class cap and the class limit. Two failing samples in a row end that
direction only. The narrow station on a link is the one with the fewest
passing samples. Its span, from the outermost passing sample on the left to
the one on the right, is compared with the GIP width.

The carriageway points use both adopted limits from that spread. S-B is
median plus 1.5 standard deviations. The other classes are median plus three. A sample passes when its longitudinal grade stays
within the deviation limit of the station's normal grade and its cross slope
stays within the cross-slope limit. The normal grade is the one stored for
the check interval. The cross slope is measured from the last sample that
passed both tests, so one rejected step does not become the new road height.
Two rejected steps in a row end that side. The class figure is the median
station on each link. Passing samples are written as points in
``check_cross.gpkg``. Positive offset is left of the direction of travel.
``class_slope_std.csv`` repeats the spread the limits were taken from.
Values above the class cap are left out of that spread, as in the grade check.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\score_check_lateral.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_link_surfaces import _code  # noqa: E402
from clip_gip_swiss import exclude_swiss  # noqa: E402
from score_check_grade import (  # noqa: E402
    _grade_cap,
    _in_label,
    _label_ranges,
    _grade,
    _line,
    _line_grade,
    _load_terrain,
    _load_water,
    _manual_bridges,
    _normal,
    _on_water,
    SIDE_M,
    _reason,
    _samples,
    _structure_pcts,
    _text,
)
from score_link_surfaces import _z_at  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

LATERAL_STEP_M = 0.5
FAIL_RUN = 2
# Absolute slope limit in percent. Replaces the grade-check figure.
LIMIT_PCT = {
    "S-B": 7.5,
}

# Subtracted from the grade-check limit, in percentage points.
# Adopted from class_slope_std.csv. S-B is median + 1.5 standard deviations.
# The other classes stay at median + 3. Deviation of a parallel's
# longitudinal grade from the station normal.
DEV_LIMIT_PCT = {
    "S-B": 4.5,
    "S-L": 3.494,
    "S-GW": 15.065,
    "Sammelstrasse": 16.136,
    "Weg": 17.825,
}
# Cross slope of one 0.5 m step, same source.
CROSS_LIMIT_PCT = {
    "S-B": 6.50,
    "S-L": 14.213,
    "S-GW": 24.767,
    "Sammelstrasse": 22.671,
    "Weg": 41.715,
}
# Full carriageway width, used only when the GIP link has no width.
# Standing orders: S-B 8 m, S-L 6.5 m, S-G 5 m, S-GW 3.75 m, S-F 4 m.
CLASS_WIDTH_M = {
    "S-B": 8.0,
    "S-L": 6.5,
    "S-G": 5.0,
    "S-GW": 3.75,
    "Weg": 4.0,
}
# Foot and trail links are Weg, and most of them have no GIP width.
FC_WIDTH_M = {
    10: 3.5,  # Rad-/Fußweg
    200: 2.5,  # Singletrail
    300: 2.5,  # Wanderweg
}


def _search_width(rec) -> tuple[float, str] | None:
    raw = getattr(rec, "width_m", None)
    try:
        width = float(raw)
    except (TypeError, ValueError):
        width = float("nan")
    if np.isfinite(width) and width > 0.0:
        return width, "gip"
    fc = int(rec.fc)
    if fc in FC_WIDTH_M:
        return FC_WIDTH_M[fc], "class"
    code = _code(fc)
    if code in CLASS_WIDTH_M:
        return CLASS_WIDTH_M[code], "class"
    return None


def _offsets(width: float) -> list[float]:
    out = []
    offset = LATERAL_STEP_M
    while offset <= width + 1e-6:
        out.append(round(offset, 2))
        offset = round(offset + LATERAL_STEP_M, 2)
    return out


def _check_side(sampled: dict, sign: float, offset: float):
    """One ``SIDE_M`` line already built by the grade check. Left is positive."""
    tag = "l" if sign > 0 else "r"
    xs = sampled[f"x{tag}_{offset}"]
    ys = sampled[f"y{tag}_{offset}"]
    z = sampled[f"z{tag}_{offset}"]
    water = sampled[f"w{tag}_{offset}"]
    step = np.hypot(np.diff(xs), np.diff(ys))
    slopes, touched = _line_grade(z, water, step)
    return xs, ys, slopes, touched, z, water


def _load_normals(folder: Path) -> dict[tuple[str, float], float]:
    """Normal longitudinal grade of each check interval that has one."""
    path = folder / "check_normal.gpkg"
    if not path.is_file():
        raise SystemExit(f"missing {path}")
    frame = gpd.read_file(path, layer="interval")
    out = {}
    for row in frame.itertuples(index=False):
        if int(row.n_line) < 1 or int(row.structure) == 1:
            continue
        value = row.normal_pct
        if value is None or not np.isfinite(value):
            continue
        out[(str(row.link_id), round(float(row.s0_m), 2))] = float(value)
    print(f"normal grades {len(out)} from {path.name}", flush=True)
    return out


def _spread(values: list[float], cap: float) -> dict:
    """Median and sample standard deviation, same cap as the grade check."""
    kept = np.asarray([v for v in values if np.isfinite(v) and v <= cap], dtype=np.float64)
    if kept.size == 0:
        return {"n": 0, "median": None, "std": None, "p90": None, "limit": None}
    std = float(kept.std(ddof=1)) if kept.size > 1 else 0.0
    median = float(np.median(kept))
    return {
        "n": int(kept.size),
        "median": round(median, 3),
        "std": round(std, 3),
        "p90": round(float(np.quantile(kept, 0.90)), 3),
        "limit": round(median + 3.0 * std, 3),
    }


def _note_slopes(
    code: str,
    normal: float,
    index: int,
    center_slopes: list[float],
    center_water: list[bool],
    offsets: list[float],
    left_slopes,
    left_water,
    left_z,
    left_point_water,
    right_slopes,
    right_water,
    right_z,
    right_point_water,
    z_center: float,
    long_dev: dict[str, list[float]],
    cross_dev: dict[str, list[float]],
) -> None:
    """Longitudinal deviation from the normal grade, and each 0.5 m cross step."""
    if not center_water[index] and np.isfinite(center_slopes[index]):
        long_dev[code].append(abs(float(center_slopes[index]) - normal))
    sides = (
        (left_slopes, left_water, left_z, left_point_water),
        (right_slopes, right_water, right_z, right_point_water),
    )
    for slopes, step_water, heights, point_water in sides:
        inner = z_center
        for offset_i in range(len(offsets)):
            if not step_water[offset_i][index]:
                slope = slopes[offset_i][index]
                if np.isfinite(slope):
                    long_dev[code].append(abs(float(slope) - normal))
            outer = float(heights[offset_i][index])
            cross = _grade(inner, outer, LATERAL_STEP_M)
            if not point_water[offset_i][index] and np.isfinite(cross):
                cross_dev[code].append(cross)
            if np.isfinite(outer):
                inner = outer


def _side_line(x: np.ndarray, y: np.ndarray, normal: np.ndarray, sign: float, offset: float, elev, spec, grid, swiss):
    xs = x + sign * normal[:, 0] * offset
    ys = y + sign * normal[:, 1] * offset
    z = _z_at(xs, ys, elev, spec)
    water = _on_water(xs, ys, grid, swiss)
    step = np.hypot(np.diff(xs), np.diff(ys))
    slopes, touched = _line_grade(z, water, step)
    return xs, ys, slopes, touched, z, water


def _kept_both(
    heights: list[np.ndarray],
    point_waters: list[np.ndarray],
    long_slopes: list[list[float]],
    long_waters: list[list[bool]],
    center_z: float,
    index: int,
    normal: float,
    dev_limit: float,
    cross_limit: float,
    cap: float,
) -> list[tuple[int, float, float]]:
    """Side offsets inside both adopted limits.

    The cross slope is taken from the last sample that passed both tests.
    The longitudinal value is the deviation of that parallel from the station
    normal. Two failures in a row end the side.
    """
    kept = []
    streak = 0
    ref_z = center_z
    ref_m = 0.0
    dev_cap = max(cap, dev_limit)
    cross_cap = max(cap, cross_limit)
    for offset_i, (z_row, point_water, slopes, step_water) in enumerate(
        zip(heights, point_waters, long_slopes, long_waters)
    ):
        offset_m = (offset_i + 1) * LATERAL_STEP_M
        cross = _grade(ref_z, float(z_row[index]), offset_m - ref_m)
        long_slope = slopes[index]
        dev = abs(float(long_slope) - normal) if np.isfinite(long_slope) else float("nan")
        water = bool(point_water[index]) or bool(step_water[index])
        cross_bad = _reason(cross, cross_limit, water=water, cap=cross_cap)
        dev_bad = _reason(dev, dev_limit, water=water, cap=dev_cap)
        if cross_bad or dev_bad:
            streak += 1
            if streak >= FAIL_RUN:
                break
            continue
        streak = 0
        ref_z = float(z_row[index])
        ref_m = offset_m
        kept.append((offset_i, float(cross), float(dev)))
    return kept


def _kept_offsets(slopes: list[list[float]], waters: list[list[bool]], index: int, limit, cap: float) -> list[int]:
    """Offsets that pass at one station step. Two failures in a row end the list."""
    kept = []
    streak = 0
    for offset_i, (slope_row, water_row) in enumerate(zip(slopes, waters)):
        if _reason(slope_row[index], limit, water=bool(water_row[index]), cap=cap):
            streak += 1
            if streak >= FAIL_RUN:
                break
            continue
        streak = 0
        kept.append(offset_i)
    return kept


def _station_table(held: list[dict], limit: float) -> pd.DataFrame:
    """Same narrow-station rows as the main walk, at one S-B slope limit."""
    cap = _grade_cap("S-B")
    rows = []
    for item in held:
        rec = item["rec"]
        offsets = item["offsets"]
        for index in range(len(item["center_slopes"])):
            left_kept = _kept_offsets(item["left_slopes"], item["left_water"], index, limit, cap)
            right_kept = _kept_offsets(item["right_slopes"], item["right_water"], index, limit, cap)
            center_ok = not _reason(
                item["center_slopes"][index], limit, water=bool(item["center_water"][index]), cap=cap
            )
            left_m = 0.0 if not left_kept else offsets[left_kept[-1]]
            right_m = 0.0 if not right_kept else offsets[right_kept[-1]]
            rows.append(
                {
                    "link_id": str(rec.link_id),
                    "short_id": int(rec.short_id),
                    "street_code": _text(rec.street_code),
                    "name": _text(rec.name),
                    "class_code": "S-B",
                    "width_m": round(float(item["width"]), 2),
                    "width_src": item["width_src"],
                    "s_m": round(float(item["stations"][index]), 2),
                    "structure": 1 if item["structure"][index] else 0,
                    "n_valid": int(center_ok) + len(left_kept) + len(right_kept),
                    "span_m": left_m + right_m,
                }
            )
    return pd.DataFrame(rows)


def _write(folder: Path, frame: gpd.GeoDataFrame, names: tuple[str, ...] | None = None) -> Path:
    dest = None
    if names is None:
        names = ("check_lateral.gpkg", "check_lateral_next.gpkg", "check_lateral_side.gpkg")
    for name in names:
        candidate = folder / name
        if not candidate.exists():
            dest = candidate
            break
        try:
            candidate.unlink()
            dest = candidate
            break
        except PermissionError:
            print(f"{name} is open, trying the next file", flush=True)
    if dest is None:
        raise SystemExit("close the open lateral geopackages and run again")
    frame.to_file(dest, layer="point", driver="GPKG")
    return dest


def _grade_limits(folder: Path) -> dict[str, float | None]:
    """Class slope limits written by the grade check. Missing file stops the run."""
    path = folder / "class_grade.csv"
    if not path.is_file():
        raise SystemExit(f"missing {path}; run tools/score_check_grade.py first")
    frame = pd.read_csv(path)
    print(f"slope limits from {path.name}", flush=True)
    limits = {}
    for row in frame.itertuples(index=False):
        code = str(row.class_code)
        raw = None if pd.isna(row.limit) else float(row.limit)
        if code in LIMIT_PCT:
            limits[code] = LIMIT_PCT[code]
            print(f"  {code} {raw} -> {limits[code]}", flush=True)
            continue
        
       
        limits[code] = raw
        print(f"  {code} {raw}", flush=True)
    return limits


def _share(values: pd.Series, tol: float) -> float:
    if values.empty:
        return float("nan")
    return float((values.abs() <= tol).mean())


def _width_summary(stations: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Narrowest passing station against the GIP width, per link and per class."""
    alive = stations[(stations["structure"] == 0) & (stations["n_valid"] > 0)].copy()
    link_rows = []
    for lid, group in alive.groupby("link_id", sort=False):
        fewest = int(group["n_valid"].min())
        narrow = group[group["n_valid"] == fewest]
        span = float(narrow["span_m"].min())
        width = float(group["width_m"].iloc[0])
        link_rows.append(
            {
                "link_id": lid,
                "short_id": int(group["short_id"].iloc[0]),
                "street_code": group["street_code"].iloc[0],
                "name": group["name"].iloc[0],
                "class_code": group["class_code"].iloc[0],
                "width_src": group["width_src"].iloc[0],
                "width_m": width,
                "n_checkint": int(len(group)),
                "min_n_valid": fewest,
                "min_span_m": span,
                "span_minus_width_m": round(span - width, 2),
                "p10_span_m": round(float(np.percentile(group["span_m"], 10)), 2),
                "median_span_m": round(float(np.median(group["span_m"])), 2),
            }
        )
    links = pd.DataFrame(link_rows)
    gip = links[links["width_src"] == "gip"]
    class_rows = []
    for code, group in gip.groupby("class_code", sort=True):
        delta = group["span_minus_width_m"]
        class_rows.append(
            {
                "class_code": code,
                "n_links": int(len(group)),
                "width_median_m": round(float(group["width_m"].median()), 2),
                "min_span_median_m": round(float(group["min_span_m"].median()), 2),
                "min_n_median": round(float(group["min_n_valid"].median()), 2),
                "delta_p10_m": round(float(np.percentile(delta, 10)), 2),
                "delta_median_m": round(float(delta.median()), 2),
                "delta_p90_m": round(float(np.percentile(delta, 90)), 2),
                "within_0_5m": round(_share(delta, 0.5), 3),
                "within_1m": round(_share(delta, 1.0), 3),
            }
        )
    return links, pd.DataFrame(class_rows)


def _median_class(links: pd.DataFrame) -> pd.DataFrame:
    """Class table from each link's median station, against the GIP width."""
    if links.empty:
        return pd.DataFrame()
    gip = links[links["width_src"] == "gip"].copy()
    if gip.empty:
        return pd.DataFrame()
    gip["median_minus_width_m"] = (gip["median_span_m"] - gip["width_m"]).round(2)
    rows = []
    for code, group in gip.groupby("class_code", sort=True):
        delta = group["median_minus_width_m"]
        rows.append(
            {
                "class_code": code,
                "n_links": int(len(group)),
                "width_median_m": round(float(group["width_m"].median()), 2),
                "median_span_m": round(float(group["median_span_m"].median()), 2),
                "min_span_median_m": round(float(group["min_span_m"].median()), 2),
                "delta_p10_m": round(float(np.percentile(delta, 10)), 2),
                "delta_median_m": round(float(delta.median()), 2),
                "delta_p90_m": round(float(np.percentile(delta, 90)), 2),
                "within_0_5m": round(_share(delta, 0.5), 3),
                "within_1m": round(_share(delta, 1.0), 3),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    terrain_path = proc / "dgm_link15" / "dgm_link15.tif"
    if not terrain_path.is_file():
        raise SystemExit(f"missing link buffer terrain {terrain_path}; run tools/mask_dgm_by_links.py")
    print(f"terrain {terrain_path}", flush=True)
    elev, spec = _load_terrain(terrain_path)
    grid, swiss = _load_water(site)
    manual = _manual_bridges()
    gip = exclude_swiss(site)
    folder = proc / "link_surface" / "check_grade"
    folder.mkdir(parents=True, exist_ok=True)
    limits = _grade_limits(folder)
    normal_at = _load_normals(folder)
    long_dev: dict[str, list[float]] = defaultdict(list)
    cross_dev: dict[str, list[float]] = defaultdict(list)
    links = gpd.read_file(gip, layer="link")
    structure_pcts = _structure_pcts(gip)
    print(f"links {len(links)}", flush=True)

    prepared = []
    skipped: dict[str, int] = defaultdict(int)
    for rec in links.itertuples(index=False):
        line = _line(rec.geometry)
        if line is None:
            continue
        found = _search_width(rec)
        if found is None:
            skipped[_code(int(rec.fc))] += 1
            continue
        width, width_src = found
        sampled = _samples(line, elev, spec, grid, swiss)
        if sampled is None:
            continue
        code = _code(int(rec.fc))
        center_step = np.hypot(np.diff(sampled["x"]), np.diff(sampled["y"]))
        center_slopes, center_water = _line_grade(sampled["z"], sampled["water"], center_step)
        labels = _label_ranges(str(rec.link_id), float(line.length), structure_pcts, manual)
        structure = [
            _in_label(float(sampled["s"][i]), float(sampled["s"][i + 1]), labels)
            for i in range(len(center_slopes))
        ]
        prepared.append(
            {
                "rec": rec,
                "code": code,
                "width": width,
                "width_src": width_src,
                "line": line,
                "sampled": sampled,
                "center_slopes": center_slopes,
                "center_water": center_water,
                "structure": structure,
            }
        )
    if skipped:
        print("no width: " + ", ".join(f"{code} {n}" for code, n in sorted(skipped.items())), flush=True)

    rows: dict[str, list] = {
        "link_id": [],
        "short_id": [],
        "street_code": [],
        "name": [],
        "class_code": [],
        "fc_name": [],
        "s_m": [],
        "offset_m": [],
        "width_m": [],
        "width_src": [],
        "slope_pct": [],
        "x": [],
        "y": [],
    }
    station_rows = []
    cross_rows = []
    cross_pts: dict[str, list] = {
        "link_id": [],
        "short_id": [],
        "street_code": [],
        "name": [],
        "class_code": [],
        "s_m": [],
        "offset_m": [],
        "width_m": [],
        "width_src": [],
        "slope_pct": [],
        "long_dev_pct": [],
        "structure": [],
        "x": [],
        "y": [],
    }
    sb_hold = []

    def add(rec, code, width, width_src, station, offset, slope, x, y) -> None:
        rows["link_id"].append(str(rec.link_id))
        rows["short_id"].append(int(rec.short_id))
        rows["street_code"].append(_text(rec.street_code))
        rows["name"].append(_text(rec.name))
        rows["class_code"].append(code)
        rows["fc_name"].append(_text(rec.fc_name))
        rows["s_m"].append(round(float(station), 2))
        rows["offset_m"].append(offset)
        rows["width_m"].append(round(float(width), 2))
        rows["width_src"].append(width_src)
        rows["slope_pct"].append(None if not np.isfinite(slope) else round(float(slope), 2))
        rows["x"].append(float(x))
        rows["y"].append(float(y))

    for item in prepared:
        rec = item["rec"]
        code = item["code"]
        sampled = item["sampled"]
        limit = limits.get(code)
        cap = _grade_cap(code)
        stations = sampled["s"]
        x = sampled["x"]
        y = sampled["y"]
        normal = np.asarray([_normal(item["line"], float(station)) for station in stations], dtype=np.float64)
        offsets = _offsets(item["width"])
        left_x = []
        left_y = []
        left_z = []
        left_slopes = []
        left_water = []
        left_point_water = []
        right_x = []
        right_y = []
        right_z = []
        right_slopes = []
        right_water = []
        right_point_water = []
        for offset in offsets:
            check_offset = next((side for side in SIDE_M if abs(side - offset) < 1e-9), None)
            if check_offset is None:
                xs, ys, slopes, water, heights, point_water = _side_line(
                    x, y, normal, 1.0, offset, elev, spec, grid, swiss
                )
            else:
                xs, ys, slopes, water, heights, point_water = _check_side(sampled, 1.0, check_offset)
            left_x.append(xs)
            left_y.append(ys)
            left_z.append(heights)
            left_slopes.append(slopes)
            left_water.append(water)
            left_point_water.append(point_water)
            if check_offset is None:
                xs, ys, slopes, water, heights, point_water = _side_line(
                    x, y, normal, -1.0, offset, elev, spec, grid, swiss
                )
            else:
                xs, ys, slopes, water, heights, point_water = _check_side(sampled, -1.0, check_offset)
            right_x.append(xs)
            right_y.append(ys)
            right_z.append(heights)
            right_slopes.append(slopes)
            right_water.append(water)
            right_point_water.append(point_water)

        # A station already written from the previous step keeps that point.
        seen: set[tuple[int, float]] = set()

        def mark(index: int, offset: float, slope: float, xs: np.ndarray, ys: np.ndarray) -> None:
            key = (index, offset)
            if key in seen:
                return
            seen.add(key)
            add(rec, code, item["width"], item["width_src"], stations[index], offset, slope, xs[index], ys[index])

        def put(index: int, offset: float, slope: float, dev: float, px: float, py: float) -> None:
            cross_pts["link_id"].append(str(rec.link_id))
            cross_pts["short_id"].append(int(rec.short_id))
            cross_pts["street_code"].append(_text(rec.street_code))
            cross_pts["name"].append(_text(rec.name))
            cross_pts["class_code"].append(code)
            cross_pts["s_m"].append(round(float(stations[index]), 2))
            cross_pts["offset_m"].append(offset)
            cross_pts["width_m"].append(round(float(item["width"]), 2))
            cross_pts["width_src"].append(item["width_src"])
            cross_pts["slope_pct"].append(round(float(slope), 2))
            cross_pts["long_dev_pct"].append(round(float(dev), 2))
            cross_pts["structure"].append(1 if item["structure"][index] else 0)
            cross_pts["x"].append(float(px))
            cross_pts["y"].append(float(py))

        center_slopes = item["center_slopes"]
        center_water = item["center_water"]
        for index, slope in enumerate(center_slopes):
            if _reason(slope, limit, water=bool(center_water[index]), cap=cap):
                continue
            mark(index, 0.0, slope, x, y)
            mark(index + 1, 0.0, slope, x, y)
        for index in range(len(center_slopes)):
            normal = normal_at.get((str(rec.link_id), round(float(stations[index]), 2)))
            if normal is not None:
                _note_slopes(
                    code,
                    normal,
                    index,
                    center_slopes,
                    center_water,
                    offsets,
                    left_slopes,
                    left_water,
                    left_z,
                    left_point_water,
                    right_slopes,
                    right_water,
                    right_z,
                    right_point_water,
                    float(sampled["z"][index]),
                    long_dev,
                    cross_dev,
                )
            left_kept = _kept_offsets(left_slopes, left_water, index, limit, cap)
            right_kept = _kept_offsets(right_slopes, right_water, index, limit, cap)
            for offset_i in left_kept:
                offset = offsets[offset_i]
                slope = left_slopes[offset_i][index]
                mark(index, offset, slope, left_x[offset_i], left_y[offset_i])
                mark(index + 1, offset, slope, left_x[offset_i], left_y[offset_i])
            for offset_i in right_kept:
                offset = -offsets[offset_i]
                slope = right_slopes[offset_i][index]
                mark(index, offset, slope, right_x[offset_i], right_y[offset_i])
                mark(index + 1, offset, slope, right_x[offset_i], right_y[offset_i])
            center_ok = not _reason(
                center_slopes[index], limit, water=bool(center_water[index]), cap=cap
            )
            left_m = 0.0 if not left_kept else offsets[left_kept[-1]]
            right_m = 0.0 if not right_kept else offsets[right_kept[-1]]
            dev_limit = DEV_LIMIT_PCT.get(code)
            cross_limit = CROSS_LIMIT_PCT.get(code)
            normal_pct = normal_at.get((str(rec.link_id), round(float(stations[index]), 2)))
            center_dev = (
                abs(float(center_slopes[index]) - normal_pct)
                if normal_pct is not None and np.isfinite(center_slopes[index])
                else float("nan")
            )
            center_dev_ok = (
                dev_limit is not None
                and normal_pct is not None
                and not _reason(
                    center_dev,
                    dev_limit,
                    water=bool(center_water[index]),
                    cap=max(cap, dev_limit),
                )
            )
            if center_dev_ok and cross_limit is not None:
                left_cross = _kept_both(
                    left_z,
                    left_point_water,
                    left_slopes,
                    left_water,
                    float(sampled["z"][index]),
                    index,
                    normal_pct,
                    dev_limit,
                    cross_limit,
                    cap,
                )
                right_cross = _kept_both(
                    right_z,
                    right_point_water,
                    right_slopes,
                    right_water,
                    float(sampled["z"][index]),
                    index,
                    normal_pct,
                    dev_limit,
                    cross_limit,
                    cap,
                )
                cross_left = 0.0 if not left_cross else offsets[left_cross[-1][0]]
                cross_right = 0.0 if not right_cross else offsets[right_cross[-1][0]]
                put(index, 0.0, 0.0, center_dev, x[index], y[index])
                for offset_i, cross_slope, dev in left_cross:
                    put(
                        index,
                        offsets[offset_i],
                        cross_slope,
                        dev,
                        left_x[offset_i][index],
                        left_y[offset_i][index],
                    )
                for offset_i, cross_slope, dev in right_cross:
                    put(
                        index,
                        -offsets[offset_i],
                        cross_slope,
                        dev,
                        right_x[offset_i][index],
                        right_y[offset_i][index],
                    )
                cross_rows.append(
                    {
                        "link_id": str(rec.link_id),
                        "short_id": int(rec.short_id),
                        "street_code": _text(rec.street_code),
                        "name": _text(rec.name),
                        "class_code": code,
                        "width_m": round(float(item["width"]), 2),
                        "width_src": item["width_src"],
                        "s_m": round(float(stations[index]), 2),
                        "structure": 1 if item["structure"][index] else 0,
                        "n_valid": 1 + len(left_cross) + len(right_cross),
                        "left_m": cross_left,
                        "right_m": cross_right,
                        "span_m": cross_left + cross_right,
                    }
                )
            station_rows.append(
                {
                    "link_id": str(rec.link_id),
                    "short_id": int(rec.short_id),
                    "street_code": _text(rec.street_code),
                    "name": _text(rec.name),
                    "class_code": code,
                    "width_m": round(float(item["width"]), 2),
                    "width_src": item["width_src"],
                    "s_m": round(float(stations[index]), 2),
                    "structure": 1 if item["structure"][index] else 0,
                    "n_valid": int(center_ok) + len(left_kept) + len(right_kept),
                    "left_m": left_m,
                    "right_m": right_m,
                    "span_m": left_m + right_m,
                }
            )
        if code == "S-B":
            sb_hold.append(
                {
                    "rec": rec,
                    "width": item["width"],
                    "width_src": item["width_src"],
                    "structure": item["structure"],
                    "stations": stations,
                    "offsets": offsets,
                    "center_slopes": center_slopes,
                    "center_water": center_water,
                    "left_slopes": left_slopes,
                    "left_water": left_water,
                    "right_slopes": right_slopes,
                    "right_water": right_water,
                }
            )

    folder = proc / "link_surface" / "check_grade"
    folder.mkdir(parents=True, exist_ok=True)
    if not rows["x"]:
        raise SystemExit("no lateral points passed")
    frame = gpd.GeoDataFrame(
        {key: value for key, value in rows.items() if key not in ("x", "y")},
        geometry=gpd.points_from_xy(rows["x"], rows["y"]),
        crs=links.crs,
    )
    dest = _write(folder, frame)
    by_class = frame.groupby("class_code").size()
    print(f"points {len(frame)} on {frame.link_id.nunique()} links", flush=True)
    print("points by class: " + ", ".join(f"{code} {int(n)}" for code, n in by_class.items()), flush=True)
    print(f"wrote {dest}", flush=True)
    link_sum, class_sum = _width_summary(pd.DataFrame(station_rows))
    link_path = folder / "lateral_width_link.csv"
    class_path = folder / "lateral_width_class.csv"
    link_sum.to_csv(link_path, index=False)
    class_sum.to_csv(class_path, index=False)
    gip_n = int((link_sum["width_src"] == "gip").sum()) if not link_sum.empty else 0
    print(f"width comparison on {gip_n} links with a GIP width", flush=True)
    if not class_sum.empty:
        print(class_sum.to_string(index=False), flush=True)
    trial_rows = []
    current = class_sum[class_sum["class_code"] == "S-B"] if not class_sum.empty else class_sum
    if not current.empty:
        row = current.iloc[0].to_dict()
        row["limit_pct"] = limits.get("S-B")
        trial_rows.append(row)
    # for pct in SB_TRIAL_PCT:
    #     _links, summary = _width_summary(_station_table(sb_hold, pct))
    #     if summary.empty:
    #         continue
    #     row = summary.iloc[0].to_dict()
    #     row["limit_pct"] = pct
    #     trial_rows.append(row)
    trials = pd.DataFrame(trial_rows)
    trial_path = folder / "lateral_width_sb_trials.csv"
    if not trials.empty:
        trials.to_csv(trial_path, index=False)
        print("S-B narrow-station trials", flush=True)
        print(trials.to_string(index=False), flush=True)
    print("adopted limits, S-B median plus 1.5 standard deviations, otherwise 3", flush=True)
    for code in CROSS_LIMIT_PCT:
        print(f"  {code} long_dev {DEV_LIMIT_PCT[code]} cross {CROSS_LIMIT_PCT[code]}", flush=True)
    cross_link, _cross_narrow = _width_summary(pd.DataFrame(cross_rows))
    cross_class = _median_class(cross_link)
    cross_link_path = folder / "lateral_cross_link.csv"
    cross_class_path = folder / "lateral_cross_class.csv"
    cross_link.to_csv(cross_link_path, index=False)
    cross_class.to_csv(cross_class_path, index=False)
    cross_n = int((cross_link["width_src"] == "gip").sum()) if not cross_link.empty else 0
    print(f"cross-slope width at the median station, {cross_n} links with a GIP width", flush=True)
    if not cross_class.empty:
        print(cross_class.to_string(index=False), flush=True)
    if not cross_pts["x"]:
        raise SystemExit("no cross-slope points passed")
    cross_frame = gpd.GeoDataFrame(
        {key: value for key, value in cross_pts.items() if key not in ("x", "y")},
        geometry=gpd.points_from_xy(cross_pts["x"], cross_pts["y"]),
        crs=links.crs,
    )
    cross_dest = _write(
        folder,
        cross_frame,
        ("check_cross.gpkg", "check_cross_next.gpkg", "check_cross_side.gpkg"),
    )
    print(f"cross points {len(cross_frame)} on {cross_frame.link_id.nunique()} links", flush=True)
    print(f"wrote {cross_dest}", flush=True)
    spread_rows = []
    for measure, pool in (("long_dev", long_dev), ("cross", cross_dev)):
        for code in sorted(pool):
            row = _spread(pool[code], _grade_cap(code))
            row["measure"] = measure
            row["class_code"] = code
            spread_rows.append(row)
    spread = pd.DataFrame(spread_rows)
    spread_path = folder / "class_slope_std.csv"
    spread.to_csv(spread_path, index=False)
    print("slope spread, values above the class cap left out", flush=True)
    if not spread.empty:
        print(spread.to_string(index=False), flush=True)
    print(
        "(open the following in qgis or similar tool to check: "
        f"{cross_dest}, {cross_link_path}, {cross_class_path}, {spread_path})",
        flush=True,
    )


if __name__ == "__main__":
    main()
