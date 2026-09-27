"""Repeat the carriageway fill until the heights settle, at most three times.

Each pass reads the height raster from the previous pass, drops pixels whose
deviation from the local 1.5 m mean is still at least 1.5 cm, and fills them
the same way: linear along each edge, then a blend of the filtered carriageway
and that edge. Nothing is written into a BeamNG level.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\repair_dgm_iterate.py
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
from repair_dgm_side_band import (  # noqa: E402
    DROP_ROUGH_M,
    _apply_side,
    _drop_farther,
    _edge_samples,
    _fill_interior,
    _inward_strip,
    _pixels,
    _sample_side,
    _split_sides,
)
from site_coords import load_site, processed_dir  # noqa: E402

MAX_PASSES = 10
SETTLE_M = 0.001
WIN = 3


def _deviation(z: np.ndarray) -> np.ndarray:
    """Absolute deviation from the local 1.5 m mean, without the 1.9 cm cap."""
    valid = np.isfinite(z) & (z > 50.0) & (z < 4500.0)
    filled = np.where(valid, z, np.float32(0.0)).astype(np.float32)
    weight = valid.astype(np.float32)
    count = uniform_filter(weight, size=WIN, mode="constant")
    mean = uniform_filter(filled, size=WIN, mode="constant")
    enough = count >= (6.0 / (WIN * WIN))
    local = np.divide(mean, count, out=np.full_like(mean, np.nan), where=enough)
    rough = np.abs(np.asarray(z, dtype=np.float32) - local).astype(np.float32)
    rough[~enough | ~valid] = np.nan
    return rough


def _prepare(roads, lines) -> list[dict]:
    parts = []
    for oid, group in roads.groupby("objectid"):
        hit = lines[lines.objectid == int(oid)]
        if hit.empty:
            continue
        center = hit.iloc[0].geometry
        if center.geom_type == "MultiLineString":
            center = max(center.geoms, key=lambda g: g.length)
        for poly in group.geometry:
            polygons = list(poly.geoms) if poly.geom_type == "MultiPolygon" else [poly]
            for part in polygons:
                sides = _split_sides(part, center)
                if sides is None:
                    print(f"  oid {oid}: edge split skipped", flush=True)
                    continue
                strips = {}
                for name, edge in sides.items():
                    strips[name] = _inward_strip(edge, part)
                parts.append({"oid": int(oid), "poly": part, "sides": sides, "strips": strips})
    return parts


def _road_mask(parts, shape, spec) -> np.ndarray:
    mask = np.zeros(shape, dtype=bool)
    dummy_z = np.ones(shape, dtype=np.float32)
    dummy_r = np.zeros(shape, dtype=np.float32)
    for part in parts:
        got = _pixels(part["poly"], dummy_z, dummy_r, spec)
        if got is None:
            continue
        rows, cols = got[0], got[1]
        mask[rows, cols] = True
    return mask


def _one_pass(working, rough, parts, spec):
    repaired = np.full(working.shape, np.nan, dtype=np.float32)
    klass = np.zeros(working.shape, dtype=np.uint8)
    pending = []
    for part in parts:
        sampled = {}
        for name, edge in part["sides"].items():
            strip = part["strips"][name]
            sampled[name] = None if strip is None else _sample_side(edge, strip, working, rough, spec)
        _drop_farther(sampled, int(spec["width"]))
        edge_xy = []
        edge_z = []
        for name, edge in part["sides"].items():
            sample = sampled[name]
            if sample is None or len(sample["rows"]) == 0:
                continue
            done = _apply_side(sample, edge, repaired, klass)
            samples = _edge_samples(edge, done["centres"], done["filled"])
            if samples is not None:
                edge_xy.append(samples[0])
                edge_z.append(samples[1])
        xy = np.vstack(edge_xy) if edge_xy else np.zeros((0, 2))
        zz = np.concatenate(edge_z) if edge_z else np.zeros(0)
        pending.append((part, xy, zz))
    for part, xy, zz in pending:
        _fill_interior(part["poly"], xy, zz, working, rough, spec, repaired, klass)
    return repaired, klass


def _stats(dev: np.ndarray, mask: np.ndarray) -> dict:
    vals = dev[mask]
    vals = vals[np.isfinite(vals)]
    if len(vals) == 0:
        return {"n": 0, "hot_px": 0, "p50_m": None, "p90_m": None, "p99_m": None}
    return {
        "n": int(len(vals)),
        "hot_px": int(np.count_nonzero(vals >= DROP_ROUGH_M)),
        "p50_m": round(float(np.percentile(vals, 50)), 4),
        "p90_m": round(float(np.percentile(vals, 90)), 4),
        "p99_m": round(float(np.percentile(vals, 99)), 4),
    }


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    raw_path = proc / "corridor50_raw" / "corridor50_raw.tif"
    gpkg_in = proc / "centerline_shift_taper" / "centerline_shift.gpkg"
    for path in (raw_path, gpkg_in):
        if not path.is_file():
            raise SystemExit(f"missing {path}")
    out_dir = proc / "dgm_repair_iter"
    out_dir.mkdir(parents=True, exist_ok=True)

    raw, spec = _load_geotiff(raw_path)
    roads = gpd.read_file(gpkg_in, layer="carriageway")
    lines = gpd.read_file(gpkg_in, layer="centerline")
    print("preparing edges", flush=True)
    parts = _prepare(roads, lines)
    print(f"  {len(parts)} carriageway parts", flush=True)
    mask = _road_mask(parts, raw.shape, spec)
    print(f"  {int(mask.sum())} carriageway pixels", flush=True)

    working = raw.copy()
    done_upto = 0
    for index in range(1, MAX_PASSES + 1):
        repaired_path = out_dir / f"pass_{index}" / "02_repaired.tif"
        if not repaired_path.is_file():
            break
        done_upto = index
    passes = []
    report_path = out_dir / "report.json"
    if done_upto and report_path.is_file():
        previous = json.loads(report_path.read_text(encoding="utf-8"))
        passes = [row for row in previous.get("passes", []) if int(row["pass"]) <= done_upto]
        repaired, _spec = _load_geotiff(out_dir / f"pass_{done_upto}" / "02_repaired.tif")
        keep = np.isfinite(repaired)
        working[keep] = repaired[keep]
        print(f"continuing after pass {done_upto}", flush=True)
    for index in range(done_upto + 1, MAX_PASSES + 1):
        before = _stats(_deviation(working), mask)
        rough = _deviation(working)
        repaired, klass = _one_pass(working, rough, parts, spec)
        updated = working.copy()
        written = klass > 0
        updated[written] = repaired[written]
        delta = np.abs(updated[written].astype(np.float64) - working[written].astype(np.float64))
        changed = int(np.count_nonzero(delta > SETTLE_M))
        max_delta = float(delta.max()) if len(delta) else 0.0
        after = _stats(_deviation(updated), mask)
        pass_dir = out_dir / f"pass_{index}"
        pass_dir.mkdir(parents=True, exist_ok=True)
        shown = np.where(mask, _deviation(updated), np.nan).astype(np.float32)
        write_referenced_tif(pass_dir / "02_repaired.tif", repaired, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
        write_referenced_tif(pass_dir / "01_class.tif", klass, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
        write_referenced_tif(pass_dir / "03_roughness.tif", shown, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
        row = {
            "pass": index,
            "before": before,
            "after": after,
            "changed_px": changed,
            "max_delta_m": round(max_delta, 4),
            "kept_px": int((klass == 1).sum()),
            "edge_filled_px": int((klass == 3).sum()),
            "gap_filled_px": int((klass == 4).sum()),
        }
        passes.append(row)
        print(
            f"pass {index}: hot {before['hot_px']} -> {after['hot_px']}, "
            f"changed {changed}, max {max_delta:.4f} m",
            flush=True,
        )
        working = updated
        if changed == 0:
            break

    report = {
        "carriageway": str(gpkg_in),
        "heights": str(raw_path),
        "drop_rough_m": DROP_ROUGH_M,
        "max_passes": MAX_PASSES,
        "settle_m": SETTLE_M,
        "passes": passes,
        "class": {
            "1": "filtered carriageway pixel, height kept from the input of this pass",
            "3": "rough pixel in the 1 m edge band, height linear along that edge",
            "4": "remaining rough pixel, blend of the filtered carriageway and the interpolated edge",
        },
        "note": (
            "03_roughness.tif is the deviation from the local 1.5 m mean after the pass, "
            "inside the carriageway. The window still includes the roadside, so an edge "
            "pixel can stay hot beside a bank or a step off the road. A pass stops the "
            f"series when no carriageway pixel moves by more than {SETTLE_M:.3f} m."
        ),
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out_dir}")


if __name__ == "__main__":
    main()
