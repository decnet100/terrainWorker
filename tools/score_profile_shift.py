"""Proof of concept: place a short profile onto the stations around it.

Valid points are the carriageway samples in the latest cross-check geopackage.
Structure stations are left out. Heights come from the 0.5 m DGM, bilinear
between cell centres.

Each link is split into 100 m pieces. In each piece the shortest tenth of the
station spans is marked. A run of marked stations that sit on successive
check intervals keeps one station: the shorter span, or, when the spans are
equal, the station with an odd index along the link. That station's valid
points define a straight cross-section, height against offset from the GIP
centre line. Positive offset is left of the direction of travel.

The same count of valid points is then sought at each of the ten check
intervals within 25 m to either side. A candidate is a contiguous run of that
many points, ordered by offset. The regression is a shape about the middle of
the run, not a height at a fixed distance from the GIP centre line. A free
vertical shift absorbs the axial grade. The run wins whose relative heights
lie closest to that shape. Linear is the first-degree shape. Cubic is the
third-degree shape, and it needs at least four points. The middle of the
winning run, halfway between its outermost offsets, is the centre line
assumed for that check interval. The stored offset is how far this middle
sits from the GIP centre line.

A check interval reached by two or more of these searches keeps every result.
The discrepancy is the absolute difference of two middles. It is a
contradiction when the two middles are not the same point.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\score_profile_shift.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from clip_gip_swiss import exclude_swiss  # noqa: E402
from score_check_grade import _line, _load_terrain, _normal  # noqa: E402
from score_link_surfaces import _z_at  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

SEGMENT_M = 100.0
NEIGHBOR_M = 25.0
NEIGHBOR_N = 10
POINT_NAMES = ("check_cross.gpkg", "check_cross_next.gpkg", "check_cross_side.gpkg")
OUT_NAMES = ("profile_shift.gpkg", "profile_shift_next.gpkg", "profile_shift_side.gpkg")


def _newest_points(folder: Path) -> Path:
    found = [folder / name for name in POINT_NAMES if (folder / name).is_file()]
    if not found:
        raise SystemExit(f"missing cross-check points in {folder}")
    return max(found, key=lambda path: path.stat().st_mtime)


def _destination(folder: Path) -> Path:
    for name in OUT_NAMES:
        candidate = folder / name
        if not candidate.exists():
            return candidate
        try:
            candidate.unlink()
            return candidate
        except PermissionError:
            print(f"{name} is open, trying the next file", flush=True)
    raise SystemExit("close the open profile-shift geopackages and run again")


def _span(offset: np.ndarray) -> float:
    left = offset[offset > 0.0]
    right = offset[offset < 0.0]
    left_m = float(left.max()) if left.size else 0.0
    right_m = float(-right.min()) if right.size else 0.0
    return left_m + right_m


def _middle(offset: np.ndarray) -> float:
    return 0.5 * (float(offset[0]) + float(offset[-1]))


def _local(offset: np.ndarray) -> np.ndarray:
    """Metres from the middle of this run. The GIP centre line is not the origin."""
    return offset - _middle(offset)


def _fit_linear(offset: np.ndarray, height: np.ndarray) -> float | None:
    """Shape z = a * u. u is metres from the middle. The constant is not kept."""
    if offset.size < 2:
        return None
    u = _local(offset)
    var = float(np.dot(u, u))
    if var <= 1e-12:
        return None
    return float(np.dot(u, height - float(height.mean())) / var)


def _fit_cubic(offset: np.ndarray, height: np.ndarray) -> np.ndarray | None:
    """Shape z = c3 u^3 + c2 u^2 + c1 u. The constant is not kept."""
    if offset.size < 4:
        return None
    u = _local(offset)
    if float(np.dot(u, u)) <= 1e-12:
        return None
    try:
        coeff = np.polyfit(u, height - float(height.mean()), 3)
    except np.linalg.LinAlgError:
        return None
    coeff = np.asarray(coeff, dtype=np.float64)
    coeff[-1] = 0.0
    if not np.all(np.isfinite(coeff)):
        return None
    return coeff


def _seeds(station_s: np.ndarray, spans: np.ndarray) -> list[int]:
    """One station index per run of the shortest tenth inside each 100 m piece."""
    order = np.argsort(station_s, kind="mergesort")
    selected: set[int] = set()
    piece_ids = np.floor(station_s / SEGMENT_M).astype(int)
    for piece in np.unique(piece_ids):
        members = np.flatnonzero(piece_ids == piece)
        count = int(math.ceil(0.10 * members.size))
        ranked = sorted(members.tolist(), key=lambda index: (spans[index], 0 if index % 2 else 1, index))
        selected.update(ranked[:count])
    chosen = []
    run: list[int] = []
    rank = {int(index): pos for pos, index in enumerate(order.tolist())}

    def flush() -> None:
        if not run:
            return
        best = min(run, key=lambda index: (spans[index], 0 if index % 2 else 1, index))
        chosen.append(best)
        run.clear()

    for index in order.tolist():
        if int(index) not in selected:
            flush()
            continue
        if run and rank[int(index)] != rank[run[-1]] + 1:
            flush()
        run.append(int(index))
    flush()
    return chosen


def _best_window(
    offset: np.ndarray,
    height: np.ndarray,
    count: int,
    predict,
) -> tuple[float, float, int] | None:
    """Contiguous run of ``count`` points whose shape is closest to ``predict``.

    ``predict`` maps metres from the run's own middle to a relative height.
    A constant height difference is removed, so the axial grade does not
    enter. Ties keep the run whose middle is closer to the GIP centre line.
    The third value is how many runs share the winning residual.
    """
    if offset.size < count or predict is None:
        return None
    best_sse = None
    best_mid = None
    tied = 0
    for start in range(int(offset.size - count + 1)):
        stop = start + count
        x = offset[start:stop]
        z = height[start:stop]
        u = x - _middle(x)
        pred = predict(u)
        shift = float(np.mean(z - pred))
        resid = z - pred - shift
        sse = float(np.dot(resid, resid))
        mid = _middle(x)
        if best_sse is None or sse < best_sse - 1e-9:
            best_sse = sse
            best_mid = mid
            tied = 1
            continue
        if abs(sse - best_sse) > 1e-9:
            continue
        tied += 1
        if abs(mid) < abs(best_mid) - 1e-12:
            best_mid = mid
    return best_mid, best_sse, tied


def _side(station: float, seed: float) -> str:
    if station > seed:
        return "forward"
    if station < seed:
        return "back"
    return "seed"


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    folder = proc / "link_surface" / "check_grade"
    points_path = _newest_points(folder)
    print(f"valid points {points_path}", flush=True)
    terrain_path = proc / "dgm_link15" / "dgm_link15.tif"
    if not terrain_path.is_file():
        raise SystemExit(f"missing link buffer terrain {terrain_path}")
    print(f"terrain {terrain_path}", flush=True)
    elev, spec = _load_terrain(terrain_path)
    print(f"terrain cell {float(spec['resolution_m']):.2f} m", flush=True)
    if abs(float(spec["resolution_m"]) - 0.5) > 1e-6:
        raise SystemExit(f"terrain cell is {spec['resolution_m']} m, expected 0.5 m")
    points = gpd.read_file(points_path, layer="point")
    points = points[points.structure == 0].copy()
    if points.empty:
        raise SystemExit("no valid points")
    points["z"] = _z_at(points.geometry.x.to_numpy(), points.geometry.y.to_numpy(), elev, spec)
    points = points[np.isfinite(points.z.to_numpy())].copy()
    points["link_id"] = points.link_id.astype(str)
    points["s_m"] = points.s_m.astype(float).round(2)
    points["offset_m"] = points.offset_m.astype(float)

    links = gpd.read_file(exclude_swiss(site), layer="link")
    link_geom = {}
    for rec in links.itertuples(index=False):
        line = _line(rec.geometry)
        if line is not None:
            link_geom[str(rec.link_id)] = line

    seed_rows = []
    estimate_rows = []
    for link_id, group in points.groupby("link_id", sort=False):
        line = link_geom.get(str(link_id))
        if line is None:
            continue
        stations = []
        for station, frame in group.groupby("s_m", sort=True):
            frame = frame.sort_values("offset_m")
            offset = frame.offset_m.to_numpy(dtype=np.float64)
            height = frame.z.to_numpy(dtype=np.float64)
            stations.append((float(station), offset, height, frame.iloc[0]))
        if not stations:
            continue
        station_s = np.asarray([item[0] for item in stations], dtype=np.float64)
        spans = np.asarray([_span(item[1]) for item in stations], dtype=np.float64)
        for index in _seeds(station_s, spans):
            station, offset, height, meta = stations[index]
            slope = _fit_linear(offset, height)
            if slope is None:
                continue
            cubic = _fit_cubic(offset, height)
            seed_mid = _middle(offset)

            def linear_at(u, slope=slope):
                return slope * u

            def cubic_at(u, cubic=cubic):
                return np.polyval(cubic, u)

            seed_rows.append(
                {
                    "link_id": str(link_id),
                    "short_id": int(meta.short_id),
                    "class_code": meta.class_code,
                    "name": meta.name,
                    "s_m": round(station, 2),
                    "segment_m": int(math.floor(station / SEGMENT_M) * SEGMENT_M),
                    "span_m": round(float(spans[index]), 2),
                    "n_point": int(offset.size),
                    "fit_slope_pct": round(100.0 * slope, 3),
                    "cubic": 0 if cubic is None else 1,
                    "center_offset_m": round(seed_mid, 3),
                    "x": float(line.interpolate(station).x),
                    "y": float(line.interpolate(station).y),
                }
            )
            neighbors = []
            for other, other_offset, other_height, _other_meta in stations:
                delta = other - station
                if abs(delta) < 1e-6 or delta < -NEIGHBOR_M - 1e-6 or delta > NEIGHBOR_M + 1e-6:
                    continue
                neighbors.append((other, other_offset, other_height, delta))
            forward = [item for item in neighbors if item[3] > 0][:NEIGHBOR_N]
            back = [item for item in neighbors if item[3] < 0][-NEIGHBOR_N:]
            targets = [(station, offset, height, "seed")]
            targets.extend((other, other_offset, other_height, _side(other, station)) for other, other_offset, other_height, _delta in forward + back)
            count = int(offset.size)
            for other, other_offset, other_height, direction in targets:
                if direction == "seed":
                    linear = (seed_mid, 0.0, 1)
                    curved = None if cubic is None else (seed_mid, 0.0, 1)
                else:
                    linear = _best_window(other_offset, other_height, count, linear_at)
                    curved = None if cubic is None else _best_window(other_offset, other_height, count, cubic_at)
                if linear is None:
                    continue
                mid, sse, tied = linear
                if curved is None:
                    cubic_mid, cubic_sse, cubic_tie = None, None, None
                else:
                    cubic_mid, cubic_sse, cubic_tie = curved
                place = line.interpolate(other)
                normal = _normal(line, other)
                estimate_rows.append(
                    {
                        "link_id": str(link_id),
                        "short_id": int(meta.short_id),
                        "class_code": meta.class_code,
                        "name": meta.name,
                        "s_m": round(float(other), 2),
                        "seed_s_m": round(station, 2),
                        "direction": direction,
                        "center_offset_m": round(float(mid), 3),
                        "sse": round(float(sse), 6),
                        "n_tie": int(tied),
                        "center_offset_cubic_m": None if cubic_mid is None else round(float(cubic_mid), 3),
                        "sse_cubic": None if cubic_sse is None else round(float(cubic_sse), 6),
                        "n_tie_cubic": None if cubic_tie is None else int(cubic_tie),
                        "shape_delta_m": None if cubic_mid is None else round(abs(float(mid) - float(cubic_mid)), 3),
                        "n_point": count,
                        "fit_slope_pct": round(100.0 * slope, 3),
                        "x": float(place.x + normal[0] * mid),
                        "y": float(place.y + normal[1] * mid),
                        "cubic_x": None if cubic_mid is None else float(place.x + normal[0] * cubic_mid),
                        "cubic_y": None if cubic_mid is None else float(place.y + normal[1] * cubic_mid),
                        "gip_x": float(place.x),
                        "gip_y": float(place.y),
                    }
                )

    if not estimate_rows:
        raise SystemExit("no profile placed")
    estimates = pd.DataFrame(estimate_rows)
    conflict_rows = []
    for (_link, station), frame in estimates.groupby(["link_id", "s_m"], sort=False):
        if len(frame) < 2:
            continue
        rows = list(frame.itertuples(index=False))
        for left_i, left in enumerate(rows):
            for right in rows[left_i + 1 :]:
                discrepancy = abs(float(left.center_offset_m) - float(right.center_offset_m))
                cubic_a = left.center_offset_cubic_m
                cubic_b = right.center_offset_cubic_m
                if cubic_a is None or cubic_b is None or not np.isfinite(cubic_a) or not np.isfinite(cubic_b):
                    discrepancy_cubic = None
                    contradiction_cubic = 0
                else:
                    discrepancy_cubic = round(abs(float(cubic_a) - float(cubic_b)), 3)
                    contradiction_cubic = 1 if discrepancy_cubic > 1e-6 else 0
                conflict_rows.append(
                    {
                        "link_id": left.link_id,
                        "short_id": int(left.short_id),
                        "class_code": left.class_code,
                        "name": left.name,
                        "s_m": float(left.s_m),
                        "seed_a_m": float(left.seed_s_m),
                        "seed_b_m": float(right.seed_s_m),
                        "direction_a": left.direction,
                        "direction_b": right.direction,
                        "offset_a_m": float(left.center_offset_m),
                        "offset_b_m": float(right.center_offset_m),
                        "discrepancy_m": round(discrepancy, 3),
                        "contradiction": 1 if discrepancy > 1e-6 else 0,
                        "offset_a_cubic_m": None if cubic_a is None else float(cubic_a),
                        "offset_b_cubic_m": None if cubic_b is None else float(cubic_b),
                        "discrepancy_cubic_m": discrepancy_cubic,
                        "contradiction_cubic": contradiction_cubic,
                        "x": float(left.gip_x),
                        "y": float(left.gip_y),
                    }
                )
    conflicts = pd.DataFrame(conflict_rows)
    dest = _destination(folder)
    seed_frame = gpd.GeoDataFrame(
        pd.DataFrame(seed_rows).drop(columns=["x", "y"]),
        geometry=[Point(row["x"], row["y"]) for row in seed_rows],
        crs=points.crs,
    )
    estimate_frame = gpd.GeoDataFrame(
        estimates.drop(columns=["x", "y", "cubic_x", "cubic_y"]),
        geometry=[Point(x, y) for x, y in zip(estimates.x, estimates.y)],
        crs=points.crs,
    )
    curved = estimates[estimates.cubic_x.notna()]
    seed_frame.to_file(dest, layer="seed", driver="GPKG")
    estimate_frame.to_file(dest, layer="estimate", driver="GPKG", mode="a")
    if not curved.empty:
        cubic_frame = gpd.GeoDataFrame(
            curved.drop(columns=["x", "y", "cubic_x", "cubic_y"]),
            geometry=[Point(x, y) for x, y in zip(curved.cubic_x, curved.cubic_y)],
            crs=points.crs,
        )
        cubic_frame.to_file(dest, layer="estimate_cubic", driver="GPKG", mode="a")
    if conflicts.empty:
        print("conflicts 0", flush=True)
    else:
        conflict_frame = gpd.GeoDataFrame(
            conflicts.drop(columns=["x", "y"]),
            geometry=[Point(x, y) for x, y in zip(conflicts.x, conflicts.y)],
            crs=points.crs,
        )
        conflict_frame.to_file(dest, layer="conflict", driver="GPKG", mode="a")
        flagged = int(conflicts.contradiction.sum())
        flagged_cubic = int(conflicts.contradiction_cubic.sum())
        print(
            f"conflicts {len(conflicts)} linear contradictions {flagged} "
            f"cubic contradictions {flagged_cubic}",
            flush=True,
        )
        print(
            f"linear discrepancy median {float(conflicts.discrepancy_m.median()):.2f} m "
            f"cubic discrepancy median {float(conflicts.discrepancy_cubic_m.median()):.2f} m",
            flush=True,
        )
    placed = estimates[estimates.direction != "seed"]
    compared = placed[placed.shape_delta_m.notna()]
    print(f"seeds {len(seed_rows)} estimates {len(estimates)}", flush=True)
    print(
        f"placed linear median {float(placed.center_offset_m.median()):.2f} m "
        f"p90 abs {float(np.quantile(placed.center_offset_m.abs(), 0.9)):.2f} m "
        f"median ties {float(placed.n_tie.median()):.0f}",
        flush=True,
    )
    if not compared.empty:
        cubic_offset = compared.center_offset_cubic_m.astype(float)
        print(
            f"placed cubic median {float(cubic_offset.median()):.2f} m "
            f"p90 abs {float(np.quantile(np.abs(cubic_offset), 0.9)):.2f} m "
            f"median |linear-cubic| {float(compared.shape_delta_m.median()):.2f} m",
            flush=True,
        )
    print(
        "(open the following in qgis or similar tool to check: "
        f"{dest})",
        flush=True,
    )


if __name__ == "__main__":
    main()
