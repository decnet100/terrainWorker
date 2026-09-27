"""Fill only the pixels that were rough on the raw DGM. Passed pixels stay put.

A rough pixel takes the mean of the locked pixels in its 1.5 m window. If that
window has none, the height is interpolated along the nearer edge, and only
between two locked stations. One station is never spread along the side, and
the line is not held past the last locked station. A filled pixel locks, so
the next pass can grow from it. Nothing is written into a BeamNG level.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\repair_dgm_iterate_lock.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
from scipy.ndimage import uniform_filter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_road_grid import RES_M, _load_geotiff  # noqa: E402
from filter_road_corridor import write_referenced_tif  # noqa: E402
from repair_dgm_iterate import _deviation, _prepare, _road_mask, _stats  # noqa: E402
from repair_dgm_side_band import BIN_M, DROP_ROUGH_M, _sample_side  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

MAX_PASSES = 5
WIN = 3


def _cache_strips(parts, z, spec) -> list[dict]:
    rough = np.zeros(z.shape, dtype=np.float32)
    cached = []
    for part in parts:
        for name, edge in part["sides"].items():
            strip = part["strips"][name]
            if strip is None:
                continue
            sample = _sample_side(edge, strip, z, rough, spec)
            if sample is None:
                continue
            cached.append(
                {
                    "rows": sample["rows"],
                    "cols": sample["cols"],
                    "station": sample["station"],
                    "dist": sample["dist"],
                }
            )
    return cached


def _window_fill(working: np.ndarray, locked: np.ndarray, movable: np.ndarray) -> np.ndarray:
    values = np.where(locked, working, 0.0).astype(np.float64)
    weight = locked.astype(np.float64)
    total = uniform_filter(values, size=WIN, mode="constant") * float(WIN * WIN)
    count = uniform_filter(weight, size=WIN, mode="constant") * float(WIN * WIN)
    out = np.full(working.shape, np.nan, dtype=np.float32)
    take = movable & (count >= 1.0)
    out[take] = (total[take] / count[take]).astype(np.float32)
    return out


def _edge_fill(working, locked, movable, strips) -> np.ndarray:
    best_dist = np.full(working.shape, np.inf, dtype=np.float32)
    best_z = np.full(working.shape, np.nan, dtype=np.float32)
    for strip in strips:
        rows = strip["rows"]
        cols = strip["cols"]
        station = strip["station"]
        dist = strip["dist"]
        keep = locked[rows, cols]
        if int(keep.sum()) < 2:
            continue
        bins = np.floor(station[keep] / BIN_M).astype(np.int32)
        order = np.argsort(bins)
        bins = bins[order]
        heights = working[rows[keep], cols[keep]][order]
        uniq, start = np.unique(bins, return_index=True)
        if len(uniq) < 2:
            continue
        med = np.empty(len(uniq), dtype=np.float64)
        ends = np.append(start[1:], len(bins))
        for i, (a, b) in enumerate(zip(start, ends)):
            med[i] = float(np.median(heights[a:b]))
        centres = uniq.astype(np.float64) * BIN_M + 0.5 * BIN_M
        s0 = float(centres[0])
        s1 = float(centres[-1])
        want = movable[rows, cols] & (station >= s0) & (station <= s1)
        if not np.any(want):
            continue
        filled = np.interp(station[want], centres, med).astype(np.float32)
        rr = rows[want]
        cc = cols[want]
        dd = dist[want].astype(np.float32)
        closer = dd < best_dist[rr, cc]
        if not np.any(closer):
            continue
        best_dist[rr[closer], cc[closer]] = dd[closer]
        best_z[rr[closer], cc[closer]] = filled[closer]
    return best_z


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    raw_path = proc / "corridor50_raw" / "corridor50_raw.tif"
    gpkg_in = proc / "centerline_shift_taper" / "centerline_shift.gpkg"
    for path in (raw_path, gpkg_in):
        if not path.is_file():
            raise SystemExit(f"missing {path}")
    out_dir = proc / "dgm_repair_lock"
    out_dir.mkdir(parents=True, exist_ok=True)

    raw, spec = _load_geotiff(raw_path)
    roads = gpd.read_file(gpkg_in, layer="carriageway")
    lines = gpd.read_file(gpkg_in, layer="centerline")
    print("preparing edges", flush=True)
    parts = _prepare(roads, lines)
    mask = _road_mask(parts, raw.shape, spec)
    strips = _cache_strips(parts, raw, spec)
    print(f"  {len(parts)} parts, {int(mask.sum())} pixels, {len(strips)} edge strips", flush=True)

    dev0 = _deviation(raw)
    on_road = mask & np.isfinite(dev0) & np.isfinite(raw)
    locked0 = on_road & (dev0 < DROP_ROUGH_M)
    free0 = on_road & (dev0 >= DROP_ROUGH_M)
    locked = locked0.copy()
    working = raw.copy()
    origin = np.zeros(raw.shape, dtype=np.uint8)
    origin[locked0] = 1
    origin[free0] = 2
    passes = []
    print(
        f"  locked {int(locked0.sum())}, free {int(free0.sum())}",
        flush=True,
    )

    for index in range(1, MAX_PASSES + 1):
        before = _stats(_deviation(working), on_road)
        movable = free0 & ~locked
        window = _window_fill(working, locked, movable)
        took_window = np.isfinite(window)
        working[took_window] = window[took_window]
        origin[took_window] = 4
        locked[took_window] = True
        movable = free0 & ~locked
        edge = _edge_fill(working, locked, movable, strips)
        took_edge = movable & np.isfinite(edge)
        working[took_edge] = edge[took_edge]
        origin[took_edge] = 3
        locked[took_edge] = True
        after = _stats(_deviation(working), on_road)
        changed = int(took_window.sum() + took_edge.sum())
        pass_dir = out_dir / f"pass_{index}"
        pass_dir.mkdir(parents=True, exist_ok=True)
        repaired = np.where(on_road, working, np.nan).astype(np.float32)
        shown = np.where(on_road, _deviation(working), np.nan).astype(np.float32)
        write_referenced_tif(pass_dir / "02_repaired.tif", repaired, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
        write_referenced_tif(pass_dir / "01_class.tif", origin, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
        write_referenced_tif(pass_dir / "03_roughness.tif", shown, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
        row = {
            "pass": index,
            "before": before,
            "after": after,
            "window_filled_px": int(took_window.sum()),
            "edge_filled_px": int(took_edge.sum()),
            "changed_px": changed,
            "still_free_px": int((free0 & ~locked).sum()),
            "locked_px": int(locked.sum()),
        }
        passes.append(row)
        print(
            f"pass {index}: hot {before['hot_px']} -> {after['hot_px']}, "
            f"window {row['window_filled_px']}, edge {row['edge_filled_px']}, "
            f"still free {row['still_free_px']}",
            flush=True,
        )
        if changed == 0:
            break

    report = {
        "carriageway": str(gpkg_in),
        "heights": str(raw_path),
        "drop_rough_m": DROP_ROUGH_M,
        "max_passes": MAX_PASSES,
        "locked_at_start_px": int(locked0.sum()),
        "free_at_start_px": int(free0.sum()),
        "passes": passes,
        "class": {
            "1": "passed on the raw DGM, height never changes",
            "2": "rough on the raw DGM and still at that height",
            "3": "filled along the edge between two locked stations, then locked",
            "4": "filled from locked pixels in the 1.5 m window, then locked",
        },
        "note": (
            "Only pixels at or above 1.5 cm on the raw DGM may change. "
            "A pixel that passed at the start stays at its raw height in every pass. "
            "Edge interpolation does not run from a single station and does not "
            "continue past the last locked station."
        ),
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out_dir}")


if __name__ == "__main__":
    main()
