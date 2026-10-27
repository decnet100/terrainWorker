"""Group valid check intervals that can bracket a gap.

Along each GIP link, a group is a run of at least --min valid intervals.
The default is 6, which is more than five. Shorter valid runs stay out of
the groups. A gap is whatever sits between two groups, or outside the
outermost group. This does not fit a spline.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\group_check_intervals.py --min 6
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from site_coords import load_site, processed_dir  # noqa: E402

DEFAULT_MIN = 6
TRY_MIN = (3, 4, 5, 6, 8, 10)


def _latest(folder: Path) -> Path:
    names = ("check_grade.gpkg", "check_grade_next.gpkg", "check_grade_change.gpkg")
    found = [folder / name for name in names if (folder / name).is_file()]
    if not found:
        raise SystemExit(f"no check intervals in {folder}")
    return max(found, key=lambda path: path.stat().st_mtime)


def _chain(geoms) -> LineString | None:
    coords = []
    for geom in geoms:
        if geom is None or geom.is_empty:
            continue
        pts = list(geom.coords)
        if coords and pts and coords[-1][:2] == pts[0][:2]:
            pts = pts[1:]
        coords.extend(pts)
    if len(coords) < 2:
        return None
    return LineString(coords)


def _runs(frame: pd.DataFrame) -> list[dict]:
    frame = frame.sort_values("s0_m")
    runs = []
    start = 0
    ok = frame["ok"].to_numpy()
    for i in range(1, len(frame) + 1):
        if i == len(frame) or bool(ok[i]) != bool(ok[start]):
            part = frame.iloc[start:i]
            runs.append(
                {
                    "ok": bool(ok[start]),
                    "n": int(len(part)),
                    "part": part,
                }
            )
            start = i
    return runs


def _gaps(runs: list[dict], min_n: int) -> tuple[list[int], list[tuple[int, int, int]]]:
    groups = [i for i, run in enumerate(runs) if run["ok"] and run["n"] >= min_n]
    if not groups:
        if runs and any(not run["ok"] or run["n"] < min_n for run in runs):
            return [], [(0, len(runs), 0)]
        return [], []
    gaps = []
    if groups[0] > 0:
        gaps.append((0, groups[0], 1))
    for left, right in zip(groups, groups[1:]):
        if right > left + 1:
            gaps.append((left + 1, right, 2))
    if groups[-1] < len(runs) - 1:
        gaps.append((groups[-1] + 1, len(runs), 1))
    return groups, gaps


def _summary(links: list[list[dict]]) -> pd.DataFrame:
    rows = []
    for min_n in TRY_MIN:
        n_group = n_both = n_one = n_none = n_short = 0
        for runs in links:
            groups, gaps = _gaps(runs, min_n)
            n_group += len(groups)
            n_short += sum(1 for run in runs if run["ok"] and run["n"] < min_n)
            for _a, _b, sides in gaps:
                if sides == 2:
                    n_both += 1
                elif sides == 1:
                    n_one += 1
                else:
                    n_none += 1
        rows.append(
            {
                "min_intervals": min_n,
                "groups": n_group,
                "gaps_both_sides": n_both,
                "gaps_one_side": n_one,
                "gaps_no_group": n_none,
                "short_valid_runs": n_short,
            }
        )
    return pd.DataFrame(rows)


def _write(folder: Path, layers: dict[str, gpd.GeoDataFrame]) -> Path:
    dest = None
    for name in ("check_groups.gpkg", "check_groups_next.gpkg", "check_groups_struct.gpkg"):
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
        raise SystemExit("close the open group geopackages and run again")
    first = True
    for name, frame in layers.items():
        if frame.empty:
            continue
        frame.to_file(dest, layer=name, driver="GPKG", mode="w" if first else "a")
        first = False
    if first:
        raise SystemExit("no groups")
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description="Group valid check intervals around gaps.")
    parser.add_argument("--min", type=int, default=DEFAULT_MIN, help="valid intervals required for a group")
    parser.add_argument("--intervals", default=None, help="check geopackage; default is the newest")
    args = parser.parse_args()
    if args.min < 1:
        raise SystemExit("--min must be at least 1")

    site = load_site()
    folder = processed_dir(site) / "link_surface" / "check_grade"
    source = Path(args.intervals) if args.intervals else _latest(folder)
    if not source.is_absolute():
        source = ROOT / source
    print(f"intervals {source}", flush=True)
    frame = gpd.read_file(source, layer="interval")
    links = []
    group_rows = []
    gap_rows = []
    short_rows = []
    for link_id, link in frame.groupby("link_id", sort=False):
        runs = _runs(link)
        links.append(runs)
        groups, gaps = _gaps(runs, args.min)
        head = link.iloc[0]
        for index in groups:
            part = runs[index]["part"]
            geom = _chain(part.geometry)
            if geom is None:
                continue
            group_rows.append(
                {
                    "link_id": link_id,
                    "short_id": int(head.short_id),
                    "street_code": head.street_code,
                    "name": head["name"],
                    "class_code": head.class_code,
                    "s0_m": float(part.s0_m.iloc[0]),
                    "s1_m": float(part.s1_m.iloc[-1]),
                    "n_interval": int(len(part)),
                    "length_m": round(float(part.s1_m.iloc[-1] - part.s0_m.iloc[0]), 1),
                    "min_intervals": args.min,
                    "geometry": geom,
                }
            )
        for run in runs:
            if not run["ok"] or run["n"] >= args.min:
                continue
            part = run["part"]
            geom = _chain(part.geometry)
            if geom is None:
                continue
            short_rows.append(
                {
                    "link_id": link_id,
                    "short_id": int(head.short_id),
                    "street_code": head.street_code,
                    "name": head["name"],
                    "class_code": head.class_code,
                    "s0_m": float(part.s0_m.iloc[0]),
                    "s1_m": float(part.s1_m.iloc[-1]),
                    "n_interval": int(len(part)),
                    "length_m": round(float(part.s1_m.iloc[-1] - part.s0_m.iloc[0]), 1),
                    "geometry": geom,
                }
            )
        for start, stop, sides in gaps:
            parts = [runs[i]["part"] for i in range(start, stop)]
            block = pd.concat(parts)
            geom = _chain(block.geometry)
            if geom is None:
                continue
            reasons = block.loc[block["ok"] == 0, "reason"]
            gap_rows.append(
                {
                    "link_id": link_id,
                    "short_id": int(head.short_id),
                    "street_code": head.street_code,
                    "name": head["name"],
                    "class_code": head.class_code,
                    "s0_m": float(block.s0_m.iloc[0]),
                    "s1_m": float(block.s1_m.iloc[-1]),
                    "n_interval": int(len(block)),
                    "n_invalid": int((block["ok"] == 0).sum()),
                    "n_short_valid": int((block["ok"] == 1).sum()),
                    "groups_beside": int(sides),
                    "length_m": round(float(block.s1_m.iloc[-1] - block.s0_m.iloc[0]), 1),
                    "n_slope_30": int((reasons == "slope_30").sum()),
                    "n_slope_class": int((reasons == "slope_class").sum()),
                    "n_slope_change": int((reasons == "slope_change").sum()),
                    "n_water": int((reasons == "water").sum()),
                    "n_bridge": int((reasons == "bridge").sum()),
                    "n_tunnel": int((reasons == "tunnel").sum()),
                    "n_gallery": int((reasons == "gallery").sum()),
                    "n_no_group": int((reasons == "no_group").sum()),
                    "n_steep_chord": int((reasons == "steep_chord").sum()),
                    "n_off_spline_1m": int((reasons == "off_spline_1m").sum()),
                    "n_off_spline_10pct": int((reasons == "off_spline_10pct").sum()),
                    "n_no_ground": int((reasons == "no_ground").sum()),
                    "geometry": geom,
                }
            )

    summary = _summary(links)
    print(summary.to_string(index=False), flush=True)
    crs = frame.crs
    dest = _write(
        folder,
        {
            "group": gpd.GeoDataFrame(group_rows, crs=crs),
            "gap": gpd.GeoDataFrame(gap_rows, crs=crs),
            "short": gpd.GeoDataFrame(short_rows, crs=crs),
        },
    )
    summary.to_csv(folder / "group_sizes.csv", index=False)
    chosen = summary.loc[summary["min_intervals"] == args.min]
    if not chosen.empty:
        row = chosen.iloc[0]
        print(
            f"min {args.min}: groups {int(row.groups)} "
            f"gaps with a group on both sides {int(row.gaps_both_sides)} "
            f"on one side {int(row.gaps_one_side)} with none {int(row.gaps_no_group)}",
            flush=True,
        )
    print(
        "(open the following in qgis or similar tool to check: "
        f"{dest}, {folder / 'group_sizes.csv'})",
        flush=True,
    )


if __name__ == "__main__":
    main()
