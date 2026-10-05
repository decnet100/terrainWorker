"""Provisional longitudinal grade audit along roads_beamng centerlines.

Samples heightmap Z at terrain resolution along each axis. Flags soft/hard
exceedances by provisional Südtirol ↔ ATIP-ish class limits. Writes a GIS
package (same road ids / snapped XY) plus CSV/JSON for inspection.

Outputs under data/processed/<site>/diag_road_grade/:
  road_grade.gpkg
    centerlines  — full polylines (EPSG working CRS), ids match roads_beamng
    stations     — sample points with local grade
    flags        — contiguous soft/hard runs as linestrings
  centerlines.geojson, stations.geojson, flags.geojson
  stations.csv, flags.csv, report.json
  preview.png

    cd C:\\temp\\beamng_autoroad
    $env:AUTOROAD_SITE = \"config/sites/timmelsjoch.yaml\"
    python tools\\diag_road_grade.py
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry import LineString, Point

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_smoke import sample_z  # noqa: E402
from fetch_suedtirol_road_links import axis_is_structure_span  # noqa: E402
from site_coords import SiteCoords, load_site, processed_dir  # noqa: E402

OUT_DIR_NAME = "diag_road_grade"

# Provisional: soft = review, hard = almost certainly wrong for the type.
# Percent slope = 100 * |dz|/ds.
LIMITS = {
    "main": {"soft_pct": 15.0, "hard_pct": 18.0},
    "track": {"soft_pct": 18.0, "hard_pct": 25.0},
    "path": {"soft_pct": 30.0, "hard_pct": 45.0},
    "other": {"soft_pct": 20.0, "hard_pct": 30.0},
}


def _road_kind(road: dict) -> str:
    if axis_is_structure_span(road):
        return "structure"
    if road.get("is_main_road") or str(road.get("str_code") or "").upper().startswith(
        ("L", "B", "A")
    ):
        return "main"
    try:
        klass = int(float(road.get("class")))
    except (TypeError, ValueError):
        klass = None
    hw = str(road.get("highway") or "").lower()
    if klass == 106 or hw == "track":
        return "track"
    if klass in (300, 10) or hw in ("path", "footway", "cycleway"):
        return "path"
    if hw in ("secondary", "primary", "trunk", "motorway", "tertiary"):
        return "main"
    return "other"


def _load_elev(proc: Path, size: int) -> tuple[np.ndarray, float]:
    hm = proc / f"heightmap_{size}.png"
    composed = proc / f"heightmap_{size}_composed.png"
    path = composed if composed.is_file() else hm
    meta = json.loads((proc / "heightmap_meta.json").read_text(encoding="utf-8"))
    max_h = float(meta["max_height_m"])
    elev = np.asarray(Image.open(path), dtype=np.float64) / 65535.0 * max_h
    # Relative PNG — z is already terrain-relative meters.
    return elev, max_h


def _polyline_xyw(road: dict) -> list[tuple[float, float, float]]:
    out = []
    for n in road.get("nodes") or []:
        if len(n) < 2:
            continue
        w = float(n[3]) if len(n) > 3 else 7.0
        out.append((float(n[0]), float(n[1]), w))
    return out


def _resample(
    xy: list[tuple[float, float, float]], step_m: float
) -> list[tuple[float, float, float, float]]:
    """Return (x, y, s, width) at step_m along the polyline."""
    if len(xy) < 2:
        return []
    cum = [0.0]
    for a, b in zip(xy, xy[1:]):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    total = cum[-1]
    if total < 1e-6:
        return []
    samples = []
    s = 0.0
    seg = 0
    while s <= total + 1e-9:
        while seg < len(cum) - 2 and cum[seg + 1] < s - 1e-9:
            seg += 1
        s0, s1 = cum[seg], cum[seg + 1]
        t = 0.0 if s1 <= s0 + 1e-12 else (s - s0) / (s1 - s0)
        t = max(0.0, min(1.0, t))
        x0, y0, w0 = xy[seg]
        x1, y1, w1 = xy[seg + 1]
        samples.append(
            (
                x0 + t * (x1 - x0),
                y0 + t * (y1 - y0),
                s,
                w0 + t * (w1 - w0),
            )
        )
        s += step_m
    if samples[-1][2] < total - 0.01:
        x, y, w = xy[-1]
        samples.append((x, y, total, w))
    return samples


def _grade_stations(
    road: dict,
    rid: str,
    elev: np.ndarray,
    sc: SiteCoords,
    step_m: float,
) -> list[dict]:
    kind = _road_kind(road)
    lim = LIMITS.get(kind, LIMITS["other"])
    xy = _polyline_xyw(road)
    samples = _resample(xy, step_m)
    if len(samples) < 2:
        return []
    zs = []
    for x, y, _s, _w in samples:
        z = sample_z(elev, x, y)
        zs.append(None if z is None else float(z))
    rows = []
    for i in range(len(samples) - 1):
        x0, y0, s0, w0 = samples[i]
        x1, y1, s1, _w1 = samples[i + 1]
        z0, z1 = zs[i], zs[i + 1]
        ds = s1 - s0
        if z0 is None or z1 is None or ds < 1e-6:
            continue
        dz = z1 - z0
        grade = abs(dz) / ds
        grade_pct = 100.0 * grade
        signed_pct = 100.0 * dz / ds
        level = "ok"
        if grade_pct >= lim["hard_pct"]:
            level = "hard"
        elif grade_pct >= lim["soft_pct"]:
            level = "soft"
        cx = 0.5 * (x0 + x1)
        cy = 0.5 * (y0 + y1)
        east, north = sc.terrain_to_crs(cx, cy)
        rows.append(
            {
                "road_id": str(rid),
                "objectid": road.get("objectid"),
                "name": road.get("name") or "",
                "highway": road.get("highway") or "",
                "class": road.get("class"),
                "form": road.get("form"),
                "str_code": road.get("str_code") or "",
                "location": road.get("location"),
                "kunstbauten": road.get("kunstbauten") or "",
                "is_main_road": bool(road.get("is_main_road")),
                "kind": kind,
                "structure": kind == "structure",
                "s_m": round(0.5 * (s0 + s1), 2),
                "s0_m": round(s0, 2),
                "s1_m": round(s1, 2),
                "ds_m": round(ds, 3),
                "dz_m": round(dz, 3),
                "grade_pct": round(grade_pct, 2),
                "signed_pct": round(signed_pct, 2),
                "soft_pct": lim["soft_pct"],
                "hard_pct": lim["hard_pct"],
                "level": level,
                "width_m": round(w0, 2),
                "bx": round(cx, 3),
                "by": round(cy, 3),
                "easting": east,
                "northing": north,
            }
        )
    return rows


def _flag_runs(stations: list[dict], min_level: str = "soft") -> list[dict]:
    """Merge contiguous soft/hard samples into polyline flags."""
    want = {"soft", "hard"} if min_level == "soft" else {"hard"}
    flags = []
    run: list[dict] = []

    def flush() -> None:
        nonlocal run
        if len(run) < 1:
            run = []
            return
        levels = {r["level"] for r in run}
        level = "hard" if "hard" in levels else "soft"
        pts = [(r["easting"], r["northing"]) for r in run]
        # Extend with endpoints for a visible segment.
        if len(pts) == 1:
            # Degenerate: tiny marker line along travel (~ds).
            r = run[0]
            east, north = r["easting"], r["northing"]
            geom = LineString([(east - 0.3, north), (east + 0.3, north)])
        else:
            geom = LineString(pts)
        flags.append(
            {
                "road_id": run[0]["road_id"],
                "objectid": run[0]["objectid"],
                "name": run[0]["name"],
                "kind": run[0]["kind"],
                "structure": run[0]["structure"],
                "level": level,
                "s0_m": run[0]["s0_m"],
                "s1_m": run[-1]["s1_m"],
                "len_m": round(run[-1]["s1_m"] - run[0]["s0_m"], 2),
                "max_grade_pct": round(max(r["grade_pct"] for r in run), 2),
                "mean_grade_pct": round(
                    float(np.mean([r["grade_pct"] for r in run])), 2
                ),
                "n_samples": len(run),
                "geometry": geom,
            }
        )
        run = []

    for row in stations:
        if row["level"] in want and not row["structure"]:
            if run and (
                run[-1]["road_id"] != row["road_id"]
                or abs(row["s0_m"] - run[-1]["s1_m"]) > 1.01
            ):
                flush()
            run.append(row)
        else:
            flush()
    flush()
    return flags


def _centerline_row(rid: str, road: dict, sc: SiteCoords, stations: list[dict]) -> dict | None:
    xy = _polyline_xyw(road)
    if len(xy) < 2:
        return None
    coords = [sc.terrain_to_crs(x, y) for x, y, _w in xy]
    kind = _road_kind(road)
    grades = [r["grade_pct"] for r in stations if r["road_id"] == str(rid)]
    return {
        "road_id": str(rid),
        "objectid": road.get("objectid"),
        "name": road.get("name") or "",
        "highway": road.get("highway") or "",
        "class": road.get("class"),
        "form": road.get("form"),
        "str_code": road.get("str_code") or "",
        "location": road.get("location"),
        "kunstbauten": road.get("kunstbauten") or "",
        "is_main_road": bool(road.get("is_main_road")),
        "kind": kind,
        "structure": kind == "structure",
        "source": road.get("source") or "",
        "len_m": round(
            sum(
                math.hypot(b[0] - a[0], b[1] - a[1])
                for a, b in zip(coords, coords[1:])
            ),
            2,
        ),
        "max_grade_pct": round(max(grades), 2) if grades else None,
        "p95_grade_pct": (
            round(float(np.percentile(grades, 95)), 2) if grades else None
        ),
        "n_soft": sum(1 for r in stations if r["road_id"] == str(rid) and r["level"] == "soft"),
        "n_hard": sum(1 for r in stations if r["road_id"] == str(rid) and r["level"] == "hard"),
        "geometry": LineString(coords),
    }


def _preview(sc: SiteCoords, centerlines, flags, out: Path, size: int) -> None:
    rgb = np.zeros((size, size, 3), dtype=np.uint8)
    rgb[:] = (22, 22, 26)
    img = Image.fromarray(rgb, mode="RGB")
    draw = ImageDraw.Draw(img)

    def to_px(east: float, north: float) -> tuple[float, float]:
        bx, by = sc.crs_to_terrain(east, north)
        return bx, size - by

    for _, row in centerlines.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        pts = [to_px(x, y) for x, y in geom.coords]
        color = (90, 90, 100)
        if row.get("kind") == "main":
            color = (70, 160, 220)
        elif row.get("structure"):
            color = (160, 80, 200)
        if len(pts) >= 2:
            draw.line(pts, fill=color, width=2)
    for _, row in flags.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        pts = [to_px(x, y) for x, y in geom.coords]
        color = (240, 180, 40) if row.get("level") == "soft" else (230, 60, 50)
        if len(pts) >= 2:
            draw.line(pts, fill=color, width=4)
        elif len(pts) == 1:
            x, y = pts[0]
            draw.ellipse([x - 2, y - 2, x + 2, y + 2], fill=color)
    img.save(out)


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--step-m",
        type=float,
        default=None,
        help="Sample step along axis (default: beamng.meters_per_pixel)",
    )
    ap.add_argument(
        "--roads",
        type=str,
        default=None,
        help="Override roads JSON (default: roads_beamng.json)",
    )
    args = ap.parse_args()

    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    out_dir = proc / OUT_DIR_NAME
    out_dir.mkdir(parents=True, exist_ok=True)

    roads_path = Path(args.roads) if args.roads else proc / "roads_beamng.json"
    if not roads_path.is_file():
        raise SystemExit(f"Missing {roads_path}")
    roads = json.loads(roads_path.read_text(encoding="utf-8"))
    step_m = float(args.step_m) if args.step_m else float(sc.square_m)
    size = int((site.get("beamng") or {}).get("mask_size") or 2048)
    elev, _max_h = _load_elev(proc, size)

    all_stations: list[dict] = []
    cl_rows = []
    for rid, road in roads.items():
        st = _grade_stations(road, rid, elev, sc, step_m)
        all_stations.extend(st)
        row = _centerline_row(rid, road, sc, st)
        if row is not None:
            cl_rows.append(row)

    flags = _flag_runs(all_stations, min_level="soft")
    crs = sc.crs

    gdf_cl = gpd.GeoDataFrame(cl_rows, crs=crs)
    gdf_st = gpd.GeoDataFrame(
        [
            {**r, "geometry": Point(r["easting"], r["northing"])}
            for r in all_stations
        ],
        crs=crs,
    )
    gdf_fl = gpd.GeoDataFrame(flags, crs=crs)

    gpkg = out_dir / "road_grade.gpkg"
    try:
        if gpkg.is_file():
            gpkg.unlink()
    except PermissionError:
        gpkg = out_dir / "road_grade_15_18.gpkg"
        print(f"Note: road_grade.gpkg locked — writing {gpkg.name}", flush=True)
        if gpkg.is_file():
            try:
                gpkg.unlink()
            except PermissionError:
                gpkg = out_dir / f"road_grade_{int(__import__('time').time())}.gpkg"
    gdf_cl.to_file(gpkg, layer="centerlines", driver="GPKG")
    gdf_st.to_file(gpkg, layer="stations", driver="GPKG")
    if len(gdf_fl):
        gdf_fl.to_file(gpkg, layer="flags", driver="GPKG")
    else:
        # Empty layer with schema for GIS.
        empty = gpd.GeoDataFrame(
            [
                {
                    "road_id": "",
                    "objectid": None,
                    "name": "",
                    "kind": "",
                    "structure": False,
                    "level": "ok",
                    "s0_m": 0.0,
                    "s1_m": 0.0,
                    "len_m": 0.0,
                    "max_grade_pct": 0.0,
                    "mean_grade_pct": 0.0,
                    "n_samples": 0,
                    "geometry": LineString([(sc.xmin, sc.ymin), (sc.xmin + 1, sc.ymin)]),
                }
            ],
            crs=crs,
        )
        empty = empty.iloc[0:0]
        empty.to_file(gpkg, layer="flags", driver="GPKG")

    gdf_cl.to_file(out_dir / "centerlines_15_18.geojson", driver="GeoJSON")
    gdf_st.to_file(out_dir / "stations_15_18.geojson", driver="GeoJSON")
    gdf_fl.to_file(out_dir / "flags_15_18.geojson", driver="GeoJSON")

    st_fields = [
        "road_id",
        "objectid",
        "name",
        "kind",
        "structure",
        "s_m",
        "s0_m",
        "s1_m",
        "grade_pct",
        "signed_pct",
        "level",
        "soft_pct",
        "hard_pct",
        "location",
        "kunstbauten",
        "easting",
        "northing",
    ]
    _write_csv(out_dir / "stations_15_18.csv", all_stations, st_fields)
    fl_fields = [
        "road_id",
        "objectid",
        "name",
        "kind",
        "structure",
        "level",
        "s0_m",
        "s1_m",
        "len_m",
        "max_grade_pct",
        "mean_grade_pct",
        "n_samples",
    ]
    _write_csv(
        out_dir / "flags_15_18.csv",
        [{k: f.get(k) for k in fl_fields} for f in flags],
        fl_fields,
    )

    main_st = [r for r in all_stations if r["kind"] == "main" and not r["structure"]]
    report = {
        "site": site.get("name"),
        "crs": crs,
        "roads_file": str(roads_path.relative_to(ROOT)).replace("\\", "/"),
        "step_m": step_m,
        "limits_pct": LIMITS,
        "roads": len(roads),
        "centerlines": len(cl_rows),
        "stations": len(all_stations),
        "flags": len(flags),
        "main": {
            "stations": len(main_st),
            "soft": sum(1 for r in main_st if r["level"] == "soft"),
            "hard": sum(1 for r in main_st if r["level"] == "hard"),
            "max_grade_pct": round(max((r["grade_pct"] for r in main_st), default=0.0), 2),
            "p95_grade_pct": (
                round(float(np.percentile([r["grade_pct"] for r in main_st], 95)), 2)
                if main_st
                else None
            ),
        },
        "worst_main_flags": sorted(
            [
                {
                    "road_id": f["road_id"],
                    "objectid": f["objectid"],
                    "name": f["name"],
                    "level": f["level"],
                    "s0_m": f["s0_m"],
                    "s1_m": f["s1_m"],
                    "len_m": f["len_m"],
                    "max_grade_pct": f["max_grade_pct"],
                }
                for f in flags
                if f["kind"] == "main"
            ],
            key=lambda x: -x["max_grade_pct"],
        )[:25],
        "gpkg": str(gpkg.relative_to(ROOT)).replace("\\", "/"),
        "note": (
            "Provisional limits. Structure spans (bridge/tunnel LOCATION) are "
            "excluded from flags. Z from composed heightmap when present."
        ),
    }
    (out_dir / "report_15_18.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    _preview(sc, gdf_cl, gdf_fl, out_dir / "preview_15_18.png", size)

    print(json.dumps(report, indent=2))
    print(f"GIS: {gpkg}")
    print(f"GeoJSON centerlines: {out_dir / 'centerlines_15_18.geojson'}")


if __name__ == "__main__":
    main()
