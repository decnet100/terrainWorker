"""Sample one road and score lateral placements by a cubic cross-section fit.

Does not write into a BeamNG level. For one GIP OBJECTID the raw centerline
is walked every few metres. At each station the raw 0.5 m DGM is read across
candidate ribbons: lateral offsets from -2 m to +2 m in 0.2 m steps, and the
class width changed by +1 m, 0 m, -1 m and -2 m. Inside each ribbon a cubic
polynomial is fitted to the heights. The residual is the score. The station
keeps the widest ribbon whose residual stays within 3 cm of the lowest
residual at that station, and that ribbon's offset. Those offsets are then
smoothed along the road, one 0.2 m step toward the neighbouring stations
per pass. A step is kept only when the class-width residual at the new
offset stays within 5 cm of that station's best residual. At the smoothed
station the same cubic, fitted inside the class width, is scored in a band
around each edge. A rise of at least 0.20 m that falls back by at least
0.20 m further out is marked as a guardrail. Left is the positive normal.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\sample_cross_profile.py 40079
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, Point

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from authorities import resolved_working_crs  # noqa: E402
from build_road_grid import _ensure_raw_mosaic, _load_geotiff  # noqa: E402
from shift_centerline_by_edge import _load_segments  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

STATION_M = 4.0
OFFSET_STEP_M = 0.2
OFFSET_MAX_M = 2.0
WIDTH_DELTAS_M = (1.0, 0.0, -1.0, -2.0)
SAMPLE_M = 0.25
MIN_WIDTH_M = 2.5
MIN_SAMPLES = 8
# A wider ribbon may sit this far above the lowest residual and still count.
RMSE_SLACK_M = 0.03
# Smoothed offset may sit this far above the station's best class-width residual.
# A few centimetres covers a guardrail or a shadowed retaining face. A 1 m step does not.
SMOOTH_TOL_M = 0.05
SMOOTH_PASSES = 40
# Edge band around the class-width edge. The cubic is fitted inside the inset,
# then the residual is read from the inset out to EDGE_OUT_M past the edge.
EDGE_IN_M = 0.75
EDGE_OUT_M = 1.50
# A guardrail is a rise of at least this much above the cubic that falls
# back by at least this much further out. A bank keeps rising, so it fails the drop.
RAIL_RISE_M = 0.20
RAIL_DROP_M = 0.20


def _cubic_rmse(lateral: np.ndarray, height: np.ndarray) -> float:
    """Root-mean-square residual of a degree-3 polynomial in the lateral coordinate."""
    design = np.column_stack(
        [np.ones_like(lateral), lateral, lateral**2, lateral**3]
    )
    coef, *_ = np.linalg.lstsq(design, height, rcond=None)
    resid = height - design @ coef
    return float(np.sqrt(np.mean(resid**2)))


def _self_check() -> None:
    lateral = np.linspace(-2.5, 2.5, 21)
    flat = _cubic_rmse(lateral, 0.02 * lateral)
    height = 0.02 * lateral
    height[lateral > 1.2] += 2.0
    step = _cubic_rmse(lateral, height)
    if not (flat < 1e-6 and step > 0.15):
        raise SystemExit(f"cubic residual self-check failed: flat {flat:.4f} step {step:.4f}")
    _rail_self_check()


def _cubic_predict(lateral: np.ndarray, height: np.ndarray, query: np.ndarray) -> np.ndarray:
    design = np.column_stack([np.ones_like(lateral), lateral, lateral**2, lateral**3])
    coef, *_ = np.linalg.lstsq(design, height, rcond=None)
    query_design = np.column_stack([np.ones_like(query), query, query**2, query**3])
    return query_design @ coef


def _rail_on_side(
    lateral: np.ndarray,
    height: np.ndarray,
    resid: np.ndarray,
    half: float,
    sign: float,
) -> dict:
    """Peak residual in one edge band, and whether it falls again further out.

    sign is +1 on the left of the road direction and -1 on the right.
    Outward distance is lateral * sign.
    """
    outward = lateral * sign
    band = (outward >= half - EDGE_IN_M - 1e-6) & (outward <= half + EDGE_OUT_M + 1e-6)
    empty = {"rail": 0, "rise_m": None, "drop_m": None, "t_m": None}
    if int(band.sum()) < 3:
        return empty
    idx = np.flatnonzero(band)
    peak_i = int(idx[int(np.argmax(resid[idx]))])
    rise = float(resid[peak_i])
    further = idx[outward[idx] > outward[peak_i] + 1e-6]
    drop = None if further.size == 0 else rise - float(np.min(resid[further]))
    is_rail = rise >= RAIL_RISE_M and drop is not None and drop >= RAIL_DROP_M
    return {
        "rail": 1 if is_rail else 0,
        "rise_m": round(rise, 3),
        "drop_m": None if drop is None else round(drop, 3),
        "t_m": round(float(lateral[peak_i]), 3),
    }


def _edge_rails(lateral: np.ndarray, height: np.ndarray, half: float) -> dict:
    """Residuals of an interior cubic, scored on both edges.

    The cubic is fitted only inside the carriageway, inset by EDGE_IN_M, so a
    rail at the edge does not pull the surface. Left is the positive normal.
    """
    empty = {"rail": 0, "rise_m": None, "drop_m": None, "t_m": None}
    interior = np.abs(lateral) <= half - EDGE_IN_M + 1e-6
    if int(interior.sum()) < MIN_SAMPLES:
        return {"left": dict(empty), "right": dict(empty)}
    resid = height - _cubic_predict(lateral[interior], height[interior], lateral)
    return {
        "left": _rail_on_side(lateral, height, resid, half, 1.0),
        "right": _rail_on_side(lateral, height, resid, half, -1.0),
    }


def _rail_self_check() -> None:
    lateral = np.linspace(-5.5, 5.5, 45)
    road = 0.02 * lateral
    rail = road.copy()
    rail[(lateral >= 3.6) & (lateral <= 4.2)] += 0.45
    found = _edge_rails(lateral, rail, 4.0)
    bank = road.copy()
    bank[lateral > 3.5] += (lateral[lateral > 3.5] - 3.5) * 0.8
    slope = _edge_rails(lateral, bank, 4.0)
    if not (found["left"]["rail"] == 1 and found["right"]["rail"] == 0):
        raise SystemExit(f"rail self-check failed: {found}")
    if slope["left"]["rail"] or slope["right"]["rail"]:
        raise SystemExit(f"bank self-check failed: {slope}")


def _bilinear(z: np.ndarray, spec: dict, x: float, y: float) -> float:
    res = float(spec["resolution_m"])
    fc = (x - float(spec["xmin"])) / res - 0.5
    fr = (float(spec["ymax"]) - y) / res - 0.5
    c0 = int(math.floor(fc))
    r0 = int(math.floor(fr))
    if r0 < 0 or c0 < 0 or r0 + 1 >= z.shape[0] or c0 + 1 >= z.shape[1]:
        return float("nan")
    dc = fc - c0
    dr = fr - r0
    z00 = float(z[r0, c0])
    z10 = float(z[r0, c0 + 1])
    z01 = float(z[r0 + 1, c0])
    z11 = float(z[r0 + 1, c0 + 1])
    if not all(math.isfinite(v) for v in (z00, z10, z01, z11)):
        return float("nan")
    return (
        z00 * (1.0 - dc) * (1.0 - dr)
        + z10 * dc * (1.0 - dr)
        + z01 * (1.0 - dc) * dr
        + z11 * dc * dr
    )


def _offsets() -> list[float]:
    n = int(round(OFFSET_MAX_M / OFFSET_STEP_M))
    return [round(i * OFFSET_STEP_M, 2) for i in range(-n, n + 1)]


def _runs(segs: list) -> list[list]:
    runs: list[list] = []
    current: list = []
    for seg in segs:
        if seg.structure:
            if current:
                runs.append(current)
                current = []
            continue
        current.append(seg)
    if current:
        runs.append(current)
    return runs


def _line_of(run: list) -> LineString:
    coords: list[tuple[float, float]] = []
    for seg in run:
        for x, y in seg.coords:
            if not coords or math.hypot(x - coords[-1][0], y - coords[-1][1]) > 1e-3:
                coords.append((float(x), float(y)))
    return LineString(coords)


def _normal(line: LineString, distance: float) -> tuple[float, float]:
    back = line.interpolate(max(0.0, distance - 1.0))
    fore = line.interpolate(min(line.length, distance + 1.0))
    dx, dy = fore.x - back.x, fore.y - back.y
    span = math.hypot(dx, dy)
    if span < 1e-6:
        return 0.0, 0.0
    return -dy / span, dx / span


def _ribbon_samples(
    z: np.ndarray,
    spec: dict,
    cx: float,
    cy: float,
    nx: float,
    ny: float,
    half: float,
) -> tuple[np.ndarray, np.ndarray]:
    lateral: list[float] = []
    height: list[float] = []
    steps = int(math.floor((2.0 * half) / SAMPLE_M + 1e-6))
    for i in range(steps + 1):
        t = -half + i * SAMPLE_M
        if t > half + 1e-6:
            break
        h = _bilinear(z, spec, cx + t * nx, cy + t * ny)
        if math.isfinite(h):
            lateral.append(t)
            height.append(h)
    return np.asarray(lateral, dtype=np.float64), np.asarray(height, dtype=np.float64)


def _smooth_offsets(offsets: list[float], rmse_at: list[dict[float, float]]) -> list[float]:
    """Move each offset toward its neighbours while the cross-section still fits.

    One pass shifts a station by one grid step toward the mean of the stations
    beside it. The step is kept when the class-width residual stays within
    SMOOTH_TOL_M of the lowest residual at that station. Passes use the offsets
    from the previous pass, so the result does not depend on station order.
    """
    current = list(offsets)
    best = [min(table.values()) if table else float("inf") for table in rmse_at]
    for _ in range(SMOOTH_PASSES):
        proposed = list(current)
        changed = False
        for i, offset in enumerate(current):
            if len(current) == 1:
                break
            if i == 0:
                target = current[1]
            elif i == len(current) - 1:
                target = current[-2]
            else:
                target = 0.5 * (current[i - 1] + current[i + 1])
            delta = target - offset
            if abs(delta) < OFFSET_STEP_M * 0.5:
                continue
            step = OFFSET_STEP_M if delta > 0.0 else -OFFSET_STEP_M
            nxt = round(max(-OFFSET_MAX_M, min(OFFSET_MAX_M, offset + step)), 2)
            score = rmse_at[i].get(nxt)
            if score is None or score > best[i] + SMOOTH_TOL_M + 1e-9:
                continue
            proposed[i] = nxt
            changed = True
        if not changed:
            break
        current = proposed
    return current


def _neighbor_step(offsets: list[float]) -> float:
    if len(offsets) < 2:
        return 0.0
    return max(abs(a - b) for a, b in zip(offsets, offsets[1:]))


def _choose(rows: list[dict]) -> dict | None:
    """Widest ribbon whose residual stays within RMSE_SLACK_M of the lowest."""
    if not rows:
        return None
    lowest = min(row["rmse_m"] for row in rows)
    valid = [row for row in rows if row["rmse_m"] <= lowest + RMSE_SLACK_M]
    valid.sort(key=lambda row: (-row["width_m"], row["rmse_m"], abs(row["offset_m"]), row["offset_m"]))
    return valid[0]


def sample_object(oid: int) -> Path:
    """Score one OBJECTID and write the station grid plus the chosen placement."""
    _self_check()
    site = load_site()
    proc = processed_dir(site)
    _ensure_raw_mosaic(site, proc)
    raw = proc / "corridor50_raw" / "corridor50_raw.tif"
    if not raw.is_file():
        raise SystemExit(f"raw DGM missing: {raw}")
    elev, spec = _load_geotiff(raw)
    group = [seg for seg in _load_segments(site) if seg.oid == oid]
    if not group:
        raise SystemExit(f"OBJECTID {oid} is not in the shifted road set")
    group.sort(key=lambda seg: seg.seq)
    class_width = float(group[0].width_m)
    widths = []
    for delta in WIDTH_DELTAS_M:
        width = round(class_width + delta, 2)
        if width >= MIN_WIDTH_M - 1e-6:
            widths.append(width)
    if not widths:
        raise SystemExit(f"OBJECTID {oid} class width {class_width} m leaves no ribbon")

    grid_rows: list[dict] = []
    grid_geoms = []
    chosen_rows: list[dict] = []
    chosen_geoms = []
    axis_rows: list[dict] = []
    axis_geoms = []
    smooth_rows: list[dict] = []
    smooth_geoms = []
    smooth_axis_rows: list[dict] = []
    smooth_axis_geoms = []
    rail_rows: list[dict] = []
    rail_geoms = []
    offsets = _offsets()
    class_width = round(class_width, 2)
    for run_i, run in enumerate(_runs(group)):
        line = _line_of(run)
        if line.length < 1.0:
            continue
        count = int(math.floor(line.length / STATION_M))
        stations = [min(line.length, i * STATION_M) for i in range(count + 1)]
        placed: list[dict] = []
        class_rmse: list[dict[float, float]] = []
        for distance in stations:
            point = line.interpolate(distance)
            nx, ny = _normal(line, distance)
            if nx == 0.0 and ny == 0.0:
                continue
            here: list[dict] = []
            at_class: dict[float, float] = {}
            for offset in offsets:
                cx = point.x + offset * nx
                cy = point.y + offset * ny
                for width in widths:
                    lateral, height = _ribbon_samples(elev, spec, cx, cy, nx, ny, width / 2.0)
                    if lateral.size < MIN_SAMPLES:
                        continue
                    if lateral[-1] - lateral[0] < width * 0.8:
                        continue
                    rmse = round(_cubic_rmse(lateral, height), 4)
                    row = {
                        "objectid": oid,
                        "run": run_i,
                        "s_m": round(distance, 2),
                        "offset_m": offset,
                        "width_m": width,
                        "width_delta_m": round(width - class_width, 2),
                        "rmse_m": rmse,
                        "n": int(lateral.size),
                        "class_width_m": class_width,
                    }
                    here.append(row)
                    grid_rows.append(row)
                    grid_geoms.append(Point(cx, cy))
                    if abs(width - class_width) < 1e-6:
                        at_class[offset] = rmse
            pick = _choose(here)
            if pick is None:
                continue
            placed.append(
                {
                    "s_m": round(distance, 2),
                    "x": point.x,
                    "y": point.y,
                    "nx": nx,
                    "ny": ny,
                    "offset_m": pick["offset_m"],
                    "width_m": pick["width_m"],
                    "rmse_m": pick["rmse_m"],
                }
            )
            class_rmse.append(at_class)
        if not placed:
            continue
        raw_offsets = [row["offset_m"] for row in placed]
        smooth = _smooth_offsets(raw_offsets, class_rmse)
        shifted: list[tuple[float, float]] = []
        smoothed: list[tuple[float, float]] = []
        rail_left = 0
        rail_right = 0
        half = class_width / 2.0
        for row, offset in zip(placed, smooth):
            sx = row["x"] + row["offset_m"] * row["nx"]
            sy = row["y"] + row["offset_m"] * row["ny"]
            shifted.append((sx, sy))
            mx = row["x"] + offset * row["nx"]
            my = row["y"] + offset * row["ny"]
            smoothed.append((mx, my))
            chosen_rows.append(
                {
                    "objectid": oid,
                    "run": run_i,
                    "s_m": row["s_m"],
                    "offset_m": row["offset_m"],
                    "width_m": row["width_m"],
                    "rmse_m": row["rmse_m"],
                    "class_width_m": class_width,
                }
            )
            chosen_geoms.append(Point(sx, sy))
            smooth_rmse = class_rmse[len(smoothed) - 1].get(offset)
            smooth_rows.append(
                {
                    "objectid": oid,
                    "run": run_i,
                    "s_m": row["s_m"],
                    "offset_m": offset,
                    "raw_offset_m": row["offset_m"],
                    "width_m": class_width,
                    "rmse_m": smooth_rmse,
                    "class_width_m": class_width,
                }
            )
            smooth_geoms.append(Point(mx, my))
            wide_lat, wide_h = _ribbon_samples(
                elev, spec, mx, my, row["nx"], row["ny"], half + EDGE_OUT_M
            )
            rails = _edge_rails(wide_lat, wide_h, half)
            rail_left += rails["left"]["rail"]
            rail_right += rails["right"]["rail"]
            smooth_rows[-1]["rail_left"] = rails["left"]["rail"]
            smooth_rows[-1]["rise_left_m"] = rails["left"]["rise_m"]
            smooth_rows[-1]["rail_right"] = rails["right"]["rail"]
            smooth_rows[-1]["rise_right_m"] = rails["right"]["rise_m"]
            for side_name, side in (("left", rails["left"]), ("right", rails["right"])):
                if not side["rail"] or side["t_m"] is None:
                    continue
                rail_rows.append(
                    {
                        "objectid": oid,
                        "run": run_i,
                        "s_m": row["s_m"],
                        "side": side_name,
                        "rise_m": side["rise_m"],
                        "drop_m": side["drop_m"],
                        "t_m": side["t_m"],
                    }
                )
                rail_geoms.append(Point(mx + side["t_m"] * row["nx"], my + side["t_m"] * row["ny"]))
            mark_l = " rail" if rails["left"]["rail"] else ""
            mark_r = " rail" if rails["right"]["rail"] else ""
            rise_l = rails["left"]["rise_m"]
            rise_r = rails["right"]["rise_m"]
            print(
                f"s {row['s_m']:7.1f}  raw {row['offset_m']:+5.1f}  smooth {offset:+5.1f}  "
                f"rmse {0.0 if smooth_rmse is None else smooth_rmse:.3f}  "
                f"left {0.0 if rise_l is None else rise_l:+5.2f}{mark_l}  "
                f"right {0.0 if rise_r is None else rise_r:+5.2f}{mark_r}",
                flush=True,
            )
        if len(shifted) >= 2:
            axis_geoms.append(LineString(shifted))
            axis_rows.append({"objectid": oid, "run": run_i, "class_width_m": class_width})
            smooth_axis_geoms.append(LineString(smoothed))
            smooth_axis_rows.append({"objectid": oid, "run": run_i, "class_width_m": class_width})
        print(
            f"run {run_i}: neighbour step raw {_neighbor_step(raw_offsets):.1f} m, "
            f"smoothed {_neighbor_step(smooth):.1f} m, "
            f"rail left {rail_left}/{len(placed)}, right {rail_right}/{len(placed)}",
            flush=True,
        )

    out_dir = proc / "cross_profile"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{oid}.gpkg"
    if dest.exists():
        try:
            dest.unlink()
        except PermissionError:
            dest = out_dir / f"{oid}_next.gpkg"
            print(f"{oid}.gpkg is open, writing {dest.name}", flush=True)
            if dest.exists():
                dest.unlink()
    crs = resolved_working_crs(site)
    first = True
    for name, geoms, rows in (
        ("grid", grid_geoms, grid_rows),
        ("estimate", chosen_geoms, chosen_rows),
        ("axis", axis_geoms, axis_rows),
        ("smooth", smooth_geoms, smooth_rows),
        ("axis_smooth", smooth_axis_geoms, smooth_axis_rows),
        ("rail", rail_geoms, rail_rows),
    ):
        if not geoms:
            print(f"layer {name} empty", flush=True)
            continue
        gpd.GeoDataFrame(rows, geometry=geoms, crs=crs).to_file(
            dest, layer=name, driver="GPKG", mode="w" if first else "a"
        )
        first = False
        print(f"layer {name}: {len(geoms)}", flush=True)
    print(
        "(open the following in qgis or similar tool to check: "
        f"{dest})",
        flush=True,
    )
    return dest


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].lstrip("-").isdigit():
        raise SystemExit("usage: python tools/sample_cross_profile.py OBJECTID")
    sample_object(int(sys.argv[1]))


if __name__ == "__main__":
    main()
