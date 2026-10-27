"""Raw-terrain cross profile about the shelf centerline.

One road is a GIP street code. Kept and culvert stations on that road are
cut into segments of about 100 m. Each segment records the unflattened
terrain height minus the terrain under the centerline, as a histogram over
lateral offset, and the RMSE of left-shoulder height minus right-shoulder
height. Left is the positive normal of the station order.

The terrain raster is the corridor DGM before the shelf. The shelf raster
is not read.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\score_shelf_cross.py
"""
from __future__ import annotations

import argparse
import math
import shutil
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, MultiLineString

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_road_grid import _load_geotiff  # noqa: E402
from gip_routing_load import gpkg_path  # noqa: E402
from score_link_surfaces import _z_at  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

SEGMENT_M = 100.0
TAIL_M = 50.0
OFFSET_M = 8.0
OFFSET_STEP_M = 0.5
DZ_M = 6.0
DZ_STEP_M = 0.25
GAP_M = 7.5
MIN_STATIONS = 3
KEPT = ("kept", "culvert")


def _shelf_path(proc: Path, arg: str | None) -> Path:
    if arg:
        path = Path(arg)
        return path if path.is_absolute() else ROOT / path
    folder = proc / "link_surface"
    for name in ("surface_shelf_smooth.gpkg", "surface_shelf_next.gpkg", "surface_shelf.gpkg"):
        path = folder / name
        if path.is_file():
            return path
    raise SystemExit(f"no shelf geopackage in {folder}")


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    return text


def _links(site: dict) -> dict[int, dict]:
    frame = gpd.read_file(gpkg_path(site), layer="link")
    out: dict[int, dict] = {}
    dupes = 0
    for rec in frame.itertuples(index=False):
        short_id = int(rec.short_id)
        if short_id in out:
            dupes += 1
        code = _text(rec.street_code)
        out[short_id] = {
            "street_code": code,
            "name": _text(rec.name),
            "length_m": float(rec.geometry.length) if rec.geometry is not None else 0.0,
        }
    if dupes:
        print(f"duplicate short ids {dupes}, last link kept", flush=True)
    return out


def _normals(xy: np.ndarray) -> np.ndarray:
    n = len(xy)
    tangent = np.zeros((n, 2), dtype=np.float64)
    if n == 1:
        return tangent
    tangent[0] = xy[1] - xy[0]
    tangent[-1] = xy[-1] - xy[-2]
    if n > 2:
        tangent[1:-1] = xy[2:] - xy[:-2]
    span = np.hypot(tangent[:, 0], tangent[:, 1])
    ok = span > 1e-6
    tangent[ok, 0] /= span[ok]
    tangent[ok, 1] /= span[ok]
    return np.column_stack((-tangent[:, 1], tangent[:, 0]))


def _assign_codes(stations: pd.DataFrame, links: dict[int, dict]) -> pd.DataFrame:
    """Street code of the link that contains each station along its chain."""
    parts = []
    for short_ids, chain in stations.groupby("short_ids", sort=False):
        chain = chain.sort_values("station_m")
        ids = [int(part) for part in str(short_ids).split(",") if part]
        lengths = []
        codes = []
        names = []
        missing = False
        for short_id in ids:
            link = links.get(short_id)
            if link is None:
                missing = True
                break
            lengths.append(link["length_m"])
            codes.append(link["street_code"])
            names.append(link["name"])
        if missing or not lengths:
            continue
        ends = np.cumsum(lengths)
        reach = float(chain["station_m"].max())
        if reach > ends[-1] + 2.0:
            print(
                f"stations reach {reach:.1f} m past link length {ends[-1]:.1f} m, short ids {ids[0]}..{ids[-1]}",
                flush=True,
            )
        index = np.searchsorted(ends, chain["station_m"].to_numpy(), side="right")
        index = np.clip(index, 0, len(codes) - 1)
        piece = chain.copy()
        piece["street_code"] = [codes[i] for i in index]
        piece["road_name"] = [names[i] for i in index]
        parts.append(piece)
    if not parts:
        raise SystemExit("no stations matched a routing link")
    return pd.concat(parts, ignore_index=True)


def _runs(chain: pd.DataFrame) -> list[pd.DataFrame]:
    """Contiguous stations of one street code. A gap starts a new run."""
    chain = chain[chain["street_code"].astype(str).str.len() > 0]
    if chain.empty:
        return []
    chain = chain.sort_values("station_m")
    s = chain["station_m"].to_numpy()
    code = chain["street_code"].to_numpy()
    cuts = [0]
    for i in range(1, len(chain)):
        if code[i] != code[i - 1] or s[i] - s[i - 1] > GAP_M:
            cuts.append(i)
    cuts.append(len(chain))
    return [chain.iloc[a:b] for a, b in zip(cuts, cuts[1:]) if b - a >= MIN_STATIONS]


def _pack(run: pd.DataFrame) -> list[pd.DataFrame]:
    """Cut a run into segments of about 100 m. A short tail joins the previous one."""
    s = run["station_m"].to_numpy()
    n = len(s)
    groups: list[tuple[int, int]] = []
    start = 0
    while start < n:
        i = start + 1
        while i < n and s[i] - s[start] < SEGMENT_M:
            i += 1
        if i < n:
            i += 1
        groups.append((start, i))
        start = i
    if len(groups) >= 2:
        a, b = groups[-1]
        if s[b - 1] - s[a] < TAIL_M:
            p0, _p1 = groups[-2]
            groups[-2] = (p0, b)
            groups.pop()
    return [run.iloc[a:b] for a, b in groups if b - a >= MIN_STATIONS]


def _mode(values: pd.Series) -> str:
    values = values.astype(str)
    values = values[values.str.len() > 0]
    if values.empty:
        return ""
    return str(values.value_counts().index[0])


def _sample(x: np.ndarray, y: np.ndarray, elev, spec) -> np.ndarray:
    return _z_at(np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64), elev, spec)


def _segment_arrays(seg: pd.DataFrame, elev, spec, offsets: np.ndarray):
    xy = np.column_stack([seg.geometry.x.to_numpy(), seg.geometry.y.to_numpy()])
    normal = _normals(xy)
    z0 = _sample(xy[:, 0], xy[:, 1], elev, spec)
    across = []
    for offset in offsets:
        across.append(_sample(xy[:, 0] + normal[:, 0] * offset, xy[:, 1] + normal[:, 1] * offset, elev, spec))
    grid = np.column_stack(across)
    rel = grid - z0[:, None]
    half = seg["width_m"].to_numpy(dtype=np.float64) * 0.5
    z_left = _sample(xy[:, 0] + normal[:, 0] * half, xy[:, 1] + normal[:, 1] * half, elev, spec)
    z_right = _sample(xy[:, 0] - normal[:, 0] * half, xy[:, 1] - normal[:, 1] * half, elev, spec)
    shoulder = z_left - z_right
    return rel, shoulder, half


def _metrics(shoulder: np.ndarray) -> dict:
    ok = np.isfinite(shoulder)
    n = int(ok.sum())
    if n == 0:
        return {"n_shoulder": 0, "mean_left_minus_right_m": None, "median_left_minus_right_m": None, "rmse_left_minus_right_m": None}
    diff = shoulder[ok]
    return {
        "n_shoulder": n,
        "mean_left_minus_right_m": round(float(diff.mean()), 3),
        "median_left_minus_right_m": round(float(np.median(diff)), 3),
        "rmse_left_minus_right_m": round(float(np.sqrt(np.mean(diff * diff))), 3),
    }


def _histogram(rel: np.ndarray, offsets: np.ndarray, edges: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Counts of relative height at each offset, and the median profile."""
    counts = np.zeros((len(offsets), len(edges) - 1), dtype=np.int32)
    median = np.full(len(offsets), np.nan)
    for i in range(len(offsets)):
        column = rel[:, i]
        column = column[np.isfinite(column)]
        if column.size == 0:
            continue
        bins = np.clip(np.digitize(column, edges) - 1, 0, len(edges) - 2)
        inside = (column >= edges[0]) & (column <= edges[-1])
        np.add.at(counts[i], bins[inside], 1)
        median[i] = float(np.median(column))
    return counts, median


def _draw(path: Path, offsets: np.ndarray, edges: np.ndarray, counts: np.ndarray, median: np.ndarray, half: float, title: str) -> None:
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    show = np.ma.masked_equal(counts.T, 0)
    ax.imshow(
        show,
        origin="lower",
        aspect="auto",
        interpolation="nearest",
        extent=(offsets[0] - OFFSET_STEP_M / 2, offsets[-1] + OFFSET_STEP_M / 2, edges[0], edges[-1]),
        cmap="viridis",
    )
    ax.plot(offsets, median, color="white", linewidth=1.4)
    if np.isfinite(half) and half > 0:
        ax.axvline(half, color="0.85", linewidth=0.6)
        ax.axvline(-half, color="0.85", linewidth=0.6)
    ax.axhline(0.0, color="0.6", linewidth=0.4)
    ax.set_xlabel("offset from centerline (m), left positive")
    ax.set_ylabel("raw terrain minus centerline height (m)")
    ax.set_title(title)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=100)
    plt.close(fig)


def _draw_road(path: Path, segments: list[dict], title: str) -> None:
    if not segments:
        return
    fig, ax = plt.subplots(figsize=(7.2, 3.2))
    cursor = 0.0
    s = []
    for row in segments:
        s.append(cursor)
        cursor += max(float(row["length_m"]), OFFSET_STEP_M)
    rmse = [row["rmse_left_minus_right_m"] if row["rmse_left_minus_right_m"] is not None else np.nan for row in segments]
    ax.bar(s, rmse, width=max(8.0, 0.8 * SEGMENT_M), align="edge", color="0.35")
    ax.set_xlabel("distance along the road (m)")
    ax.set_ylabel("RMSE left shoulder minus right (m)")
    ax.set_title(title)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=100)
    plt.close(fig)


def _write_gpkg(folder: Path, segment_rows: list[dict], road_rows: list[dict], crs) -> Path:
    dest = None
    for name in ("segments.gpkg", "segments_next.gpkg"):
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
        raise SystemExit("close the open cross-profile geopackages and run again")
    gpd.GeoDataFrame(segment_rows, crs=crs).to_file(dest, layer="segment", driver="GPKG")
    gpd.GeoDataFrame(road_rows, crs=crs).to_file(dest, layer="road", driver="GPKG", mode="a")
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description="Raw-terrain cross profile of the shelf centerline.")
    parser.add_argument("--stations", default=None, help="shelf geopackage; default is the smoothed shelf")
    args = parser.parse_args()

    site = load_site()
    proc = processed_dir(site)
    stations_path = _shelf_path(proc, args.stations)
    terrain_path = proc / "corridor50_raw" / "corridor50_raw.tif"
    if not terrain_path.is_file():
        raise SystemExit(f"missing unflattened terrain {terrain_path}")
    print(f"stations {stations_path}", flush=True)
    print(f"terrain {terrain_path}", flush=True)
    elev, spec = _load_geotiff(terrain_path)
    links = _links(site)
    frame = gpd.read_file(stations_path, layer="station")
    kept = frame[frame["status"].isin(KEPT)].copy()
    print(f"stations {len(frame)}, kept or culvert {len(kept)}", flush=True)
    coded = _assign_codes(kept, links)
    coded = coded[coded["street_code"].astype(str).str.len() > 0]
    print(f"stations on a street code {len(coded)}", flush=True)

    offsets = np.round(np.arange(-OFFSET_M, OFFSET_M + 1e-9, OFFSET_STEP_M), 2)
    edges = np.round(np.arange(-DZ_M, DZ_M + 1e-9, DZ_STEP_M), 2)
    out_dir = proc / "link_surface" / "cross_quality"
    hist_dir = out_dir / "hist"
    road_dir = out_dir / "roads"
    if hist_dir.exists():
        shutil.rmtree(hist_dir)
    if road_dir.exists():
        shutil.rmtree(road_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    segment_rows = []
    profile_rows = []
    hist_rows = []
    by_road: dict[str, list[dict]] = defaultdict(list)
    crs = frame.crs
    for short_ids, chain in coded.groupby("short_ids", sort=False):
        for run in _runs(chain):
            for seg in _pack(run):
                rel, shoulder, half = _segment_arrays(seg, elev, spec, offsets)
                counts, median = _histogram(rel, offsets, edges)
                metrics = _metrics(shoulder)
                code = str(seg["street_code"].iloc[0])
                name = _mode(seg["road_name"])
                s0 = float(seg["station_m"].iloc[0])
                s1 = float(seg["station_m"].iloc[-1])
                segment_id = f"{code}_{len(segment_rows):04d}"
                half_m = float(np.nanmedian(half))
                title = (
                    f"{code} {name}  {s0:.0f}–{s1:.0f} m  "
                    f"RMSE {metrics['rmse_left_minus_right_m']} m  "
                    f"mean L-R {metrics['mean_left_minus_right_m']} m"
                )
                png = hist_dir / _safe(code) / f"{segment_id}.png"
                _draw(png, offsets, edges, counts, median, half_m, title)
                xy = [(float(pt.x), float(pt.y)) for pt in seg.geometry]
                row = {
                    "segment_id": segment_id,
                    "street_code": code,
                    "name": name,
                    "class_code": _mode(seg["code"]),
                    "s0_m": round(s0, 1),
                    "s1_m": round(s1, 1),
                    "length_m": round(s1 - s0, 1),
                    "n_stations": int(len(seg)),
                    "width_m": round(float(np.nanmedian(seg["width_m"])), 2),
                    "stations": str(stations_path),
                    "terrain": str(terrain_path),
                    "geometry": LineString(xy),
                    **metrics,
                }
                segment_rows.append(row)
                by_road[code].append(row)
                for i, offset in enumerate(offsets):
                    finite = rel[:, i]
                    finite = finite[np.isfinite(finite)]
                    if finite.size == 0:
                        continue
                    profile_rows.append(
                        {
                            "segment_id": segment_id,
                            "street_code": code,
                            "offset_m": float(offset),
                            "n": int(finite.size),
                            "median_dz_m": round(float(np.median(finite)), 3),
                            "p10_dz_m": round(float(np.quantile(finite, 0.10)), 3),
                            "p90_dz_m": round(float(np.quantile(finite, 0.90)), 3),
                        }
                    )
                    occupied = np.flatnonzero(counts[i] > 0)
                    for bin_i in occupied:
                        hist_rows.append(
                            {
                                "segment_id": segment_id,
                                "street_code": code,
                                "offset_m": float(offset),
                                "dz_lo_m": float(edges[bin_i]),
                                "dz_hi_m": float(edges[bin_i + 1]),
                                "count": int(counts[i, bin_i]),
                            }
                        )
    if not segment_rows:
        raise SystemExit("no segment on a street code")

    road_rows = []
    for code, rows in by_road.items():
        rmse = [row["rmse_left_minus_right_m"] for row in rows if row["rmse_left_minus_right_m"] is not None]
        name = _mode(pd.Series([row["name"] for row in rows]))
        _draw_road(road_dir / f"{_safe(code)}.png", rows, f"{code} {name}")
        lines = [row["geometry"] for row in rows]
        road_rows.append(
            {
                "street_code": code,
                "name": name,
                "n_segments": len(rows),
                "length_m": round(sum(row["length_m"] for row in rows), 1),
                "n_stations": int(sum(row["n_stations"] for row in rows)),
                "median_rmse_m": None if not rmse else round(float(np.median(rmse)), 3),
                "mean_rmse_m": None if not rmse else round(float(np.mean(rmse)), 3),
                "geometry": lines[0] if len(lines) == 1 else MultiLineString(lines),
            }
        )
    dest = _write_gpkg(out_dir, segment_rows, road_rows, crs)
    pd.DataFrame(profile_rows).to_csv(out_dir / "profile.csv", index=False)
    pd.DataFrame(hist_rows).to_csv(out_dir / "histogram.csv", index=False)
    rmse_all = [row["rmse_left_minus_right_m"] for row in segment_rows if row["rmse_left_minus_right_m"] is not None]
    print(
        f"roads {len(road_rows)} segments {len(segment_rows)} "
        f"median RMSE {np.median(rmse_all):.3f} m",
        flush=True,
    )
    print(
        "(open the following in qgis or similar tool to check: "
        f"{dest}, {hist_dir})",
        flush=True,
    )


def _safe(code: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in code) or "road"


if __name__ == "__main__":
    main()
