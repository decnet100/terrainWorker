"""Sweep a constant CRS translation on roads_beamng and recount grade flags.

Modes:
  se  — dE=+s, dN=-s (SE diagonal)
  e   — dE=+s, dN=0  (east only)
  w   — dE=-s, dN=0  (west only)
  n   — dE=0,  dN=+s (north only)
  s   — dE=0,  dN=-s (south only)
  nw  — dE=-s, dN=+s (NW diagonal)

    cd C:\\temp\\beamng_autoroad
    $env:AUTOROAD_SITE = \"config/sites/timmelsjoch.yaml\"
    python tools\\sweep_centerline_offset_grade.py --mode e
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from diag_road_grade import LIMITS, _flag_runs, _grade_stations, _load_elev  # noqa: E402
from site_coords import SiteCoords, load_site, processed_dir  # noqa: E402


def _shift_roads(roads: dict, sc: SiteCoords, d_east: float, d_north: float) -> dict:
    dbx = d_east / sc.bw * sc.terrain_span
    dby = d_north / sc.bh * sc.terrain_span
    out: dict = {}
    for rid, road in roads.items():
        r = dict(road)
        nodes = []
        for n in road.get("nodes") or []:
            nn = list(n)
            nn[0] = float(n[0]) + dbx
            nn[1] = float(n[1]) + dby
            nodes.append(nn)
        r["nodes"] = nodes
        out[rid] = r
    return out


def _count_main(roads: dict, elev, sc: SiteCoords, step_m: float) -> dict:
    stations: list[dict] = []
    for rid, road in roads.items():
        stations.extend(_grade_stations(road, rid, elev, sc, step_m))
    main = [r for r in stations if r["kind"] == "main" and not r["structure"]]
    flags = _flag_runs(stations, min_level="soft")
    main_flags = [f for f in flags if f["kind"] == "main"]
    grades = [r["grade_pct"] for r in main]
    return {
        "stations": len(main),
        "soft_samples": sum(1 for r in main if r["level"] == "soft"),
        "hard_samples": sum(1 for r in main if r["level"] == "hard"),
        "ok_samples": sum(1 for r in main if r["level"] == "ok"),
        "problem_samples": sum(1 for r in main if r["level"] in ("soft", "hard")),
        "flag_runs": len(main_flags),
        "soft_runs": sum(1 for f in main_flags if f["level"] == "soft"),
        "hard_runs": sum(1 for f in main_flags if f["level"] == "hard"),
        "flag_len_m": round(sum(float(f["len_m"]) for f in main_flags), 1),
        "p95_grade_pct": round(float(np.percentile(grades, 95)), 2) if grades else None,
        "max_grade_pct": round(max(grades, default=0.0), 2),
    }


def _delta(mode: str, s: float) -> tuple[float, float, str]:
    if mode == "se":
        return s, -s, "SE diagonal d_east=+s, d_north=-s"
    if mode == "e":
        return s, 0.0, "east only d_east=+s, d_north=0"
    if mode == "w":
        return -s, 0.0, "west only d_east=-s, d_north=0"
    if mode == "n":
        return 0.0, s, "north only d_east=0, d_north=+s"
    if mode == "s":
        return 0.0, -s, "south only d_east=0, d_north=-s"
    if mode == "nw":
        return -s, s, "NW diagonal d_east=-s, d_north=+s"
    raise SystemExit(f"Unknown mode {mode!r}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--mode",
        choices=("se", "e", "w", "n", "s", "nw"),
        default="e",
        help="Translation direction (default: e = east only)",
    )
    ap.add_argument("--s-min", type=float, default=3.0)
    ap.add_argument("--s-max", type=float, default=8.0)
    ap.add_argument("--s-step", type=float, default=0.5)
    args = ap.parse_args()

    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    out_dir = proc / "diag_road_grade"
    out_dir.mkdir(parents=True, exist_ok=True)

    if LIMITS["main"]["soft_pct"] != 15.0 or LIMITS["main"]["hard_pct"] != 18.0:
        raise SystemExit(f"Unexpected main limits: {LIMITS['main']}")

    size = int((site.get("beamng") or {}).get("mask_size") or 2048)
    elev, _ = _load_elev(proc, size)
    roads0 = json.loads((proc / "roads_beamng.json").read_text(encoding="utf-8"))
    step_m = float(sc.square_m)

    _, _, shift_note = _delta(args.mode, 1.0)
    rows: list[dict] = []
    print(
        f"{'s':>5} {'dE':>6} {'dN':>6} {'soft':>6} {'hard':>6} "
        f"{'prob':>6} {'runs':>6} {'len_m':>8} {'p95':>6}",
        flush=True,
    )

    values = [0.0, *np.arange(args.s_min, args.s_max + 1e-9, args.s_step).tolist()]
    for s in values:
        s = round(float(s), 1)
        d_east, d_north, _ = _delta(args.mode, s)
        roads = roads0 if s == 0.0 else _shift_roads(roads0, sc, d_east, d_north)
        c = _count_main(roads, elev, sc, step_m)
        row = {"s_m": s, "d_east_m": d_east, "d_north_m": d_north, **c}
        rows.append(row)
        print(
            f"{s:5.1f} {d_east:6.1f} {d_north:6.1f} "
            f"{c['soft_samples']:6d} {c['hard_samples']:6d} "
            f"{c['problem_samples']:6d} {c['flag_runs']:6d} "
            f"{c['flag_len_m']:8.1f} {c['p95_grade_pct']:6.1f}",
            flush=True,
        )

    stem = f"offset_sweep_{args.mode}"
    payload = {
        "site": site.get("name"),
        "mode": args.mode,
        "limits_pct": LIMITS,
        "shift": shift_note,
        "rows": rows,
    }
    json_path = out_dir / f"{stem}.json"
    csv_path = out_dir / f"{stem}.csv"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    best = min(
        rows,
        key=lambda r: (
            r["problem_samples"],
            r["flag_len_m"],
            r["hard_samples"],
            abs(r["s_m"]),
        ),
    )
    print(f"Wrote {json_path}", flush=True)
    print(
        "Best (min soft+hard samples): "
        f"s={best['s_m']} dE={best['d_east_m']} dN={best['d_north_m']} "
        f"soft={best['soft_samples']} hard={best['hard_samples']} "
        f"runs={best['flag_runs']} len_m={best['flag_len_m']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
