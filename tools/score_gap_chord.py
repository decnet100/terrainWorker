"""Height of invalid intervals against the line between valid groups.

A valid group is six or more intervals that passed the grade check, walked in
chain order. Between two such groups, each invalid sample that has a height is
compared with the straight line joining the facing ends of those groups, and
with the cubic through both groups. The difference is the DGM minus that
line, in metres. A negative value means the terrain is below the line.

The chord's overall slope is the rise between those two ends, divided by
the plan distance. It stays within the limit for the class when it is under
15 percent on S-B and S-L, under 20 percent on S-GW and Sammelstrasse, and
under 30 percent on Weg. A steeper chord is kept in the layer and marked
slope_ok 0, so it can be left out later.

One line is written per gap. The line follows the intervals between the two
groups.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\score_gap_chord.py
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.interpolate import CubicSpline
from shapely.geometry import LineString

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from gip_routing_load import gpkg_path  # noqa: E402
from score_check_grade import GROUP_MIN, SLOPE_LIMIT_PCT, _chains, _ends, _line, _text  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

GRADE_NAMES = (
    "check_grade.gpkg",
    "check_grade_next.gpkg",
    "check_grade_change.gpkg",
    "check_grade_recover.gpkg",
)


def _latest(folder: Path) -> Path:
    found = [folder / name for name in GRADE_NAMES if (folder / name).is_file()]
    if not found:
        raise SystemExit(f"no check intervals in {folder}")
    return max(found, key=lambda path: path.stat().st_mtime)


def _write(folder: Path, frame: gpd.GeoDataFrame) -> Path:
    dest = None
    for name in ("todo_gap.gpkg", "todo_gap_next.gpkg", "todo_gap_chord.gpkg"):
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
        raise SystemExit("close the open todo-gap geopackages and run again")
    frame.to_file(dest, layer="gap", driver="GPKG")
    return dest


def _finite(value) -> float | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number


def _overlap_typs(lid: str, s0: float, s1: float, length: float, labels: dict) -> list[str]:
    """Structure types whose placed range overlaps this interval."""
    found = []
    for start, end, typ in labels.get(lid, []):
        span = abs(end - start) / 100.0 * length
        mid = 0.5 * (start + end) / 100.0 * length
        lo = max(0.0, mid - 0.5 * span)
        hi = min(length, lo + span)
        lo = max(0.0, hi - span)
        if s1 > lo and s0 < hi and typ not in found:
            found.append(typ)
    return found


def _chain_rows(chain, by_link, items, labels) -> list[dict]:
    rows = []
    base = 0.0
    for lid, forward in chain:
        part = by_link.get(lid)
        item = items[lid]
        length = float(item["line"].length)
        if part is None or part.empty:
            base += length
            continue
        records = part.to_dict("records")
        if not forward:
            records = list(reversed(records))
        for rec in records:
            s0_m = float(rec["s0_m"])
            s1_m = float(rec["s1_m"])
            if forward:
                s0 = base + s0_m
                s1 = base + s1_m
                z0 = _finite(rec["z0_m"])
                z1 = _finite(rec["z1_m"])
            else:
                s0 = base + (length - s1_m)
                s1 = base + (length - s0_m)
                z0 = _finite(rec["z1_m"])
                z1 = _finite(rec["z0_m"])
            rows.append(
                {
                    "id": lid,
                    "forward": forward,
                    "geom": rec["geometry"],
                    "s0": s0,
                    "s1": s1,
                    "z0": z0,
                    "z1": z1,
                    "ok": int(rec["ok"]) == 1,
                    "reason": _text(rec["reason"]),
                    "structure": int(rec["structure"]) == 1,
                    "typs": _overlap_typs(lid, s0_m, s1_m, length, labels),
                    "code": item["code"],
                    "name": item["name"],
                    "street": item["street"],
                }
            )
        base += length
    return rows


def _spans(rows: list[dict]) -> list[tuple[int, int, float, float, list[tuple[float, float]]]]:
    spans = []
    start = None
    for i, row in enumerate(rows + [None]):
        good = row is not None and row["ok"]
        if good and start is None:
            start = i
        if not good and start is not None:
            if i - start >= GROUP_MIN:
                knots = []
                if rows[start]["z0"] is not None:
                    knots.append((rows[start]["s0"], rows[start]["z0"]))
                for part in rows[start:i]:
                    if part["z1"] is None:
                        continue
                    if knots and part["s1"] <= knots[-1][0] + 1e-3:
                        continue
                    knots.append((part["s1"], part["z1"]))
                spans.append((start, i, rows[start]["s0"], rows[i - 1]["s1"], knots))
            start = None
    return spans


def _samples(row: dict, lo: float, hi: float) -> list[tuple[float, float]]:
    out = []
    for station, height in ((row["s0"], row["z0"]), (row["s1"], row["z1"])):
        if height is None or station <= lo + 1e-3 or station >= hi - 1e-3:
            continue
        out.append((station, height))
    return out


def _gap_line(rows: list[dict]) -> LineString | None:
    coords = []
    for row in rows:
        geom = row["geom"]
        if geom is None or geom.is_empty:
            continue
        pts = list(geom.coords)
        if not row["forward"]:
            pts = list(reversed(pts))
        if coords and pts and coords[-1][:2] == pts[0][:2]:
            pts = pts[1:]
        coords.extend(pts)
    if len(coords) < 2:
        return None
    return LineString(coords)


def _round(values: list[float]) -> tuple[float | None, float | None, float | None]:
    if not values:
        return None, None, None
    return (
        round(float(np.min(values)), 2),
        round(float(np.max(values)), 2),
        round(float(np.mean(values)), 2),
    )


def _gaps(chains, by_link, items, labels) -> tuple[list[dict], Counter]:
    gaps = []
    bare: Counter = Counter()
    for chain_i, chain in enumerate(chains):
        rows = _chain_rows(chain, by_link, items, labels)
        spans = _spans(rows)
        covered = np.zeros(len(rows), dtype=bool)
        for left, right in zip(spans, spans[1:]):
            lo = left[3]
            hi = right[2]
            knots = []
            for station, height in left[4] + right[4]:
                if knots and station <= knots[-1][0] + 1e-3:
                    continue
                knots.append((station, height))
            if hi - lo < 1e-3 or len(knots) < 2 or not left[4] or not right[4]:
                continue
            between = rows[left[1] : right[0]]
            covered[left[1] : right[0]] = True
            predict = CubicSpline([k[0] for k in knots], [k[1] for k in knots], extrapolate=False)
            edge_lo = left[4][-1]
            edge_hi = right[4][0]
            span_s = edge_hi[0] - edge_lo[0]
            cubic = []
            chord = []
            reasons: Counter = Counter()
            codes: Counter = Counter()
            names: Counter = Counter()
            streets: Counter = Counter()
            links: Counter = Counter()
            typs: Counter = Counter()
            n_todo = 0
            n_struct = 0
            n_no_z = 0
            for row in between:
                if row["ok"]:
                    continue
                n_todo += 1
                links[row["id"]] += 1
                if row["structure"]:
                    n_struct += 1
                    for typ in row["typs"]:
                        typs[typ] += 1
                reasons[row["reason"] or ""] += 1
                codes[row["code"]] += 1
                if row["name"]:
                    names[row["name"]] += 1
                if row["street"]:
                    streets[row["street"]] += 1
                samples = _samples(row, lo, hi)
                if row["z0"] is None and row["z1"] is None:
                    n_no_z += 1
                for station, height in samples:
                    predicted = float(predict(station))
                    if np.isfinite(predicted):
                        cubic.append(height - predicted)
                    if span_s > 1e-6:
                        share = (station - edge_lo[0]) / span_s
                        chord.append(height - (edge_lo[1] + share * (edge_hi[1] - edge_lo[1])))
            if n_todo == 0:
                continue
            geom = _gap_line(between)
            if geom is None:
                continue
            cubic_min, cubic_max, cubic_mean = _round(cubic)
            chord_min, chord_max, chord_mean = _round(chord)
            code = codes.most_common(1)[0][0] if codes else ""
            limit = SLOPE_LIMIT_PCT.get(code)
            slope = None if span_s <= 1e-6 else round(100.0 * abs(edge_hi[1] - edge_lo[1]) / span_s, 2)
            slope_ok = None if slope is None or limit is None else int(slope < limit)
            gaps.append(
                {
                    "chain": chain_i,
                    "link_id": links.most_common(1)[0][0],
                    "n_link": len(links),
                    "street_code": streets.most_common(1)[0][0] if streets else "",
                    "name": names.most_common(1)[0][0] if names else "",
                    "class_code": code,
                    "typ": typs.most_common(1)[0][0] if typs else "",
                    "length_m": round(hi - lo, 1),
                    "n_todo": n_todo,
                    "n_struct": n_struct,
                    "n_sample": len(chord),
                    "n_no_z": n_no_z,
                    "reason": reasons.most_common(1)[0][0] if reasons else "",
                    "reasons": ",".join(f"{key}:{count}" for key, count in reasons.most_common()),
                    "chord_slope_pct": slope,
                    "slope_limit_pct": limit,
                    "slope_ok": slope_ok,
                    "chord_min_m": chord_min,
                    "chord_max_m": chord_max,
                    "chord_mean_m": chord_mean,
                    "cubic_min_m": cubic_min,
                    "cubic_max_m": cubic_max,
                    "cubic_mean_m": cubic_mean,
                    "geometry": geom,
                }
            )
        for index, row in enumerate(rows):
            if row["ok"] or covered[index]:
                continue
            bare[row["reason"] or ""] += 1
    return gaps, bare


def _print_summary(frame: gpd.GeoDataFrame, bare: Counter) -> None:
    measured = frame[frame["n_sample"] > 0]
    print(
        f"gaps {len(frame)} with a height {len(measured)}; "
        f"invalid intervals with no group on both sides {sum(bare.values())}",
        flush=True,
    )
    if bare:
        print("no group on both sides: " + ", ".join(f"{key} {count}" for key, count in bare.most_common()), flush=True)
    if measured.empty:
        return
    steep = measured[measured["slope_ok"] == 0]
    print(
        f"chord steeper than the class limit: {len(steep)} gaps, {steep['length_m'].sum():.0f} m",
        flush=True,
    )
    print("chord mean by label, metres (DGM minus the line)", flush=True)
    labels = list(measured.loc[measured["typ"] != "", "typ"].drop_duplicates()) + [""]
    for typ in labels:
        part = measured[measured["typ"] == typ]
        if part.empty:
            continue
        below = int((part["chord_max_m"] < 0).sum())
        above = int((part["chord_min_m"] > 0).sum())
        name = typ or "none"
        print(
            f"  {name}: gaps {len(part)} length {part['length_m'].sum():.0f} m "
            f"mean median {part['chord_mean_m'].median():.2f} "
            f"p10 {part['chord_mean_m'].quantile(0.1):.2f} "
            f"p90 {part['chord_mean_m'].quantile(0.9):.2f} "
            f"entirely below {below} entirely above {above}",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Height of invalid gaps against the line between valid groups.")
    parser.add_argument("--intervals", default=None, help="check geopackage; default is the newest")
    args = parser.parse_args()
    site = load_site()
    folder = processed_dir(site) / "link_surface" / "check_grade"
    source = Path(args.intervals) if args.intervals else _latest(folder)
    if not source.is_absolute():
        source = ROOT / source
    print(f"intervals {source}", flush=True)
    frame = gpd.read_file(source, layer="interval")
    links = gpd.read_file(gpkg_path(site), layer="link")
    nodes = gpd.read_file(gpkg_path(site), layer="node")
    refs = gpd.read_file(gpkg_path(site), layer="bridge_ref")
    by_link = {
        str(lid): part.sort_values("s0_m")
        for lid, part in frame.groupby(frame["link_id"].astype(str), sort=False)
    }
    labels: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
    for row in refs.itertuples(index=False):
        start = float(row.from_pct) if pd.notna(row.from_pct) else 0.0
        end = float(row.to_pct) if pd.notna(row.to_pct) else 100.0
        if abs(end - start) < 0.05:
            continue
        labels[str(row.link_id)].append((start, end, str(row.typ)))
    items = []
    lookup = {}
    for rec in links.itertuples(index=False):
        line = _line(rec.geometry)
        if line is None:
            continue
        lid = str(rec.link_id)
        item = {
            "id": lid,
            "line": line,
            "fc": int(rec.fc),
            "street": _text(rec.street_code),
            "code": str(rec.fc),
            "name": _text(rec.name),
        }
        part = by_link.get(lid)
        if part is not None and not part.empty:
            item["code"] = str(part.iloc[0]["class_code"])
            item["name"] = _text(part.iloc[0]["name"]) or item["name"]
        items.append(item)
        lookup[lid] = item
    ends = _ends(items, nodes)
    chains = _chains(items, ends)
    gaps, bare = _gaps(chains, by_link, lookup, labels)
    out = gpd.GeoDataFrame(gaps, crs=frame.crs)
    if out.empty:
        raise SystemExit("no gap between two valid groups")
    dest = _write(folder, out)
    _print_summary(out, bare)
    print(f"(open the following in qgis or similar tool to check: {dest})", flush=True)


if __name__ == "__main__":
    main()
