"""Terrain cross-section at 20 narrow stations, plus a cubic through the road samples.

The stations are links whose narrowest passing span is already within 0.5 m of
the GIP width. Elevation is the same 1 m terrain the lateral check used, read
at the same 0.5 m offsets, plus 1.5 m past the searched width on each side.
The cubic is fit only through the samples that passed. Nothing is shifted
onto neighbouring stations.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\plot_narrow_cross_section.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from score_check_grade import (  # noqa: E402
    _grade_cap,
    _in_label,
    _label_ranges,
    _line,
    _line_grade,
    _load_terrain,
    _load_water,
    _manual_bridges,
    _normal,
    _reason,
    _samples,
    _structure_pcts,
)
from score_check_lateral import (  # noqa: E402
    _grade_limits,
    _kept_offsets,
    _offsets,
    _side_line,
)
from score_link_surfaces import _z_at  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

TOL_M = 0.5
N_PROFILES = 20
SEED = 20
# Drawn past the searched width so the bank is on the sheet. Not part of the pass test.
EXTRA_STEPS = 3
EXTRA_STEP_M = 0.5


def _pick(links: pd.DataFrame) -> pd.DataFrame:
    """Spread 20 links across classes. Within a class the draw is random."""
    pool = links[(links["width_src"] == "gip") & (links["span_minus_width_m"].abs() <= TOL_M)].copy()
    if pool.empty:
        raise SystemExit("no GIP-width link is within 0.5 m at its narrowest station")
    rng = np.random.default_rng(SEED)
    groups = {code: group.reset_index(drop=True) for code, group in pool.groupby("class_code")}
    codes = sorted(groups)
    quota = {code: min(len(group), N_PROFILES // len(codes)) for code, group in groups.items()}
    left = min(N_PROFILES, len(pool)) - sum(quota.values())
    order = sorted(codes, key=lambda code: len(groups[code]), reverse=True)
    for code in order:
        if left <= 0:
            break
        room = len(groups[code]) - quota[code]
        take = min(room, left)
        quota[code] += take
        left -= take
    parts = []
    for code in codes:
        group = groups[code]
        chosen = rng.choice(len(group), size=quota[code], replace=False)
        parts.append(group.iloc[np.sort(chosen)])
    picked = pd.concat(parts, ignore_index=True)
    print(
        f"pool {len(pool)} links within {TOL_M:.1f} m, drawing {len(picked)} "
        + ", ".join(f"{code} {quota[code]}" for code in codes),
        flush=True,
    )
    return picked


def _prepare(rec, elev, spec, grid, swiss, structure_pcts, manual):
    line = _line(rec.geometry)
    if line is None:
        return None
    sampled = _samples(line, elev, spec, grid, swiss)
    if sampled is None:
        return None
    labels = _label_ranges(str(rec.link_id), float(line.length), structure_pcts, manual)
    structure = [
        _in_label(float(sampled["s"][i]), float(sampled["s"][i + 1]), labels)
        for i in range(len(sampled["s"]) - 1)
    ]
    normal = np.asarray([_normal(line, float(station)) for station in sampled["s"]], dtype=np.float64)
    return line, sampled, structure, normal


def _narrow_station(sampled, structure, normal, width, limit, cap, elev, spec, grid, swiss):
    """Fewest passing samples, then the smaller span, then the earlier station."""
    offsets = _offsets(width)
    x = sampled["x"]
    y = sampled["y"]
    left = []
    right = []
    for offset in offsets:
        left.append(_side_line(x, y, normal, 1.0, offset, elev, spec, grid, swiss))
        right.append(_side_line(x, y, normal, -1.0, offset, elev, spec, grid, swiss))
    center_z = sampled["z"]
    best = None
    for index, blocked in enumerate(structure):
        if blocked:
            continue
        left_kept = _kept_offsets([row[2] for row in left], [row[3] for row in left], index, limit, cap)
        right_kept = _kept_offsets([row[2] for row in right], [row[3] for row in right], index, limit, cap)
        center_ok = not _center_fails(sampled, index, limit, cap)
        n_valid = int(center_ok) + len(left_kept) + len(right_kept)
        if n_valid <= 0:
            continue
        left_m = 0.0 if not left_kept else offsets[left_kept[-1]]
        right_m = 0.0 if not right_kept else offsets[right_kept[-1]]
        key = (n_valid, left_m + right_m, float(sampled["s"][index]))
        if best is None or key < best[0]:
            best = (key, index, left_kept, right_kept, center_ok, left_m, right_m)
    if best is None:
        return None
    _key, index, left_kept, right_kept, center_ok, left_m, right_m = best
    return {
        "index": index,
        "offsets": offsets,
        "left": left,
        "right": right,
        "center_z": float(center_z[index]),
        "center_ok": center_ok,
        "left_kept": left_kept,
        "right_kept": right_kept,
        "left_m": left_m,
        "right_m": right_m,
        "span_m": left_m + right_m,
        "s_m": float(sampled["s"][index]),
        "cx": float(x[index]),
        "cy": float(y[index]),
        "nx": float(normal[index, 0]),
        "ny": float(normal[index, 1]),
    }


def _center_fails(sampled, index, limit, cap) -> bool:
    step = np.hypot(np.diff(sampled["x"]), np.diff(sampled["y"]))
    slopes, water = _line_grade(sampled["z"], sampled["water"], step)
    return bool(_reason(slopes[index], limit, water=bool(water[index]), cap=cap))


def _section(station: dict, elev, spec) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Offset with right positive, elevation, and whether the sample passed."""
    offsets = []
    heights = []
    passed = []
    center = station["center_z"]
    offsets.append(0.0)
    heights.append(0.0 if np.isfinite(center) else np.nan)
    passed.append(bool(station["center_ok"]) and np.isfinite(center))
    for offset_i, offset in enumerate(station["offsets"]):
        for side, sign in (("left", -1.0), ("right", 1.0)):
            xs, ys, _slopes, _water = station[side][offset_i]
            z = _z_at(np.asarray([xs[station["index"]]]), np.asarray([ys[station["index"]]]), elev, spec)[0]
            kept = station[f"{side}_kept"]
            offsets.append(sign * offset)
            heights.append(float(z) - center if np.isfinite(z) and np.isfinite(center) else np.nan)
            passed.append(offset_i in kept and np.isfinite(z))
    outer = float(station["offsets"][-1]) if station["offsets"] else 0.0
    for step_i in range(1, EXTRA_STEPS + 1):
        offset = round(outer + step_i * EXTRA_STEP_M, 2)
        for sign in (-1.0, 1.0):
            plot_x = sign * offset
            wx = station["cx"] - station["nx"] * plot_x
            wy = station["cy"] - station["ny"] * plot_x
            z = _z_at(np.asarray([wx]), np.asarray([wy]), elev, spec)[0]
            offsets.append(plot_x)
            heights.append(float(z) - center if np.isfinite(z) and np.isfinite(center) else np.nan)
            passed.append(False)
    order = np.argsort(offsets)
    return (
        np.asarray(offsets, dtype=np.float64)[order],
        np.asarray(heights, dtype=np.float64)[order],
        np.asarray(passed, dtype=bool)[order],
    )


def _cubic(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, float] | None:
    good = np.isfinite(x) & np.isfinite(y)
    if int(good.sum()) < 4:
        return None
    coeff = np.polyfit(x[good], y[good], 3)
    residual = y[good] - np.polyval(coeff, x[good])
    rmse = float(np.sqrt(np.mean(residual**2)))
    return coeff, rmse


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    folder = proc / "link_surface" / "check_grade"
    terrain_path = proc / "dgm_link15" / "dgm_link15.tif"
    clipped = proc / "gip_routing" / "gip_routing_clipped.gpkg"
    if not terrain_path.is_file():
        raise SystemExit(f"missing {terrain_path}")
    if not clipped.is_file():
        raise SystemExit(f"missing {clipped}")
    summary = pd.read_csv(folder / "lateral_width_link.csv")
    picked = _pick(summary)
    elev, spec = _load_terrain(terrain_path)
    print(f"terrain cell {spec['resolution_m']:.2f} m", flush=True)
    grid, swiss = _load_water(site)
    manual = _manual_bridges()
    structure_pcts = _structure_pcts(clipped)
    limits = _grade_limits(folder)
    import geopandas as gpd

    links = gpd.read_file(clipped, layer="link")
    links = links[links["link_id"].astype(str).isin(set(picked["link_id"].astype(str)))]
    by_id = {str(rec.link_id): rec for rec in links.itertuples(index=False)}

    profiles = []
    for row in picked.itertuples(index=False):
        rec = by_id.get(str(row.link_id))
        if rec is None:
            print(f"missing geometry {row.link_id}", flush=True)
            continue
        prepared = _prepare(rec, elev, spec, grid, swiss, structure_pcts, manual)
        if prepared is None:
            continue
        _line_geom, sampled, structure, normal = prepared
        code = str(row.class_code)
        cap = _grade_cap(code)
        station = _narrow_station(
            sampled, structure, normal, float(row.width_m), limits.get(code), cap, elev, spec, grid, swiss
        )
        if station is None:
            print(f"no narrow station {row.short_id}", flush=True)
            continue
        x, y, passed = _section(station, elev, spec)
        fit = _cubic(x[passed], y[passed])
        profiles.append(
            {
                "row": row,
                "code": code,
                "x": x,
                "y": y,
                "passed": passed,
                "fit": fit,
                "s_m": station["s_m"],
                "span_m": station["span_m"],
                "left_m": station["left_m"],
                "right_m": station["right_m"],
            }
        )
        delta = station["span_m"] - float(row.min_span_m)
        if abs(delta) > 0.05:
            print(
                f"span mismatch {row.short_id}: recomputed {station['span_m']:.2f} "
                f"table {float(row.min_span_m):.2f}",
                flush=True,
            )

    if len(profiles) < N_PROFILES:
        print(f"plotted {len(profiles)} of {N_PROFILES}", flush=True)
    _draw(folder, profiles)
    _write_table(folder, profiles)


def _draw(folder: Path, profiles: list[dict]) -> None:
    cols = 4
    rows = int(np.ceil(len(profiles) / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(14, 3.1 * rows), constrained_layout=True)
    flat = np.atleast_1d(axes).ravel()
    for axis in flat[len(profiles) :]:
        axis.set_visible(False)
    for axis, item in zip(flat, profiles):
        row = item["row"]
        x, y, passed = item["x"], item["y"], item["passed"]
        finite = np.isfinite(y)
        axis.axvspan(-float(row.width_m) / 2.0, float(row.width_m) / 2.0, color="0.75", alpha=0.35, lw=0)
        axis.plot(x[finite], y[finite], color="0.45", lw=1.0, marker="o", ms=3.0)
        if passed.any():
            axis.plot(x[passed], y[passed], color="#d2691e", lw=0, marker="o", ms=5.5)
            axis.axvline(-item["left_m"], color="#d2691e", lw=0.8, ls="--")
            axis.axvline(item["right_m"], color="#d2691e", lw=0.8, ls="--")
        fit = item["fit"]
        rmse_cm = float("nan")
        if fit is not None:
            coeff, rmse = fit
            rmse_cm = rmse * 100.0
            span = x[passed]
            grid = np.linspace(float(np.min(span)), float(np.max(span)), 80)
            axis.plot(grid, np.polyval(coeff, grid), color="#1f4e79", lw=1.6)
        kept = y[passed & np.isfinite(y)]
        relief = float(np.nanmax(kept) - np.nanmin(kept)) * 100.0 if kept.size else float("nan")
        axis.set_title(
            f"{item['code']}  {int(row.short_id)}   s {item['s_m']:.0f} m\n"
            f"GIP {float(row.width_m):.1f} m, Spanne {item['span_m']:.1f} m, "
            f"Rest {rmse_cm:.0f} cm, Höhe {relief:.0f} cm",
            fontsize=8,
        )
        finite_x = x[np.isfinite(x)]
        x_lo = float(np.min(finite_x))
        x_hi = float(np.max(finite_x))
        pad = 0.04 * (x_hi - x_lo) if x_hi > x_lo else 0.5
        x_lo -= pad
        x_hi += pad
        half = 0.5 * (x_hi - x_lo)
        axis.set_xlim(x_lo, x_hi)
        axis.set_ylim(-half, half)
        axis.set_aspect("equal", adjustable="box")
        axis.set_xlabel("Versatz, rechts positiv (m)", fontsize=8)
        axis.set_ylabel("Höhe gegen Achse (m)", fontsize=8)
        axis.tick_params(labelsize=7)
        axis.axhline(0.0, color="0.7", lw=0.6)
        axis.grid(True, lw=0.3, color="0.85")
    fig.suptitle(
        "Grau: Gelände. Orange: gültige Proben. Blau: kubisch durch diese Proben. "
        "Band: GIP-Breite, mittig auf der Achse.",
        fontsize=10,
    )
    dest = folder / "narrow_cross_section.png"
    fig.savefig(dest, dpi=140, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {dest}", flush=True)


def _write_table(folder: Path, profiles: list[dict]) -> None:
    rows = []
    for item in profiles:
        row = item["row"]
        fit = item["fit"]
        coeff = [float("nan")] * 4 if fit is None else [float(v) for v in fit[0]]
        rmse = float("nan") if fit is None else float(fit[1])
        rows.append(
            {
                "link_id": row.link_id,
                "short_id": int(row.short_id),
                "class_code": item["code"],
                "width_m": float(row.width_m),
                "s_m": round(item["s_m"], 2),
                "left_m": item["left_m"],
                "right_m": item["right_m"],
                "span_m": item["span_m"],
                "table_span_m": float(row.min_span_m),
                "n_passed": int(item["passed"].sum()),
                "cubic_rmse_m": None if not np.isfinite(rmse) else round(rmse, 4),
                "cubic_x3": None if not np.isfinite(coeff[0]) else round(coeff[0], 6),
                "cubic_x2": None if not np.isfinite(coeff[1]) else round(coeff[1], 6),
                "cubic_x1": None if not np.isfinite(coeff[2]) else round(coeff[2], 6),
                "cubic_x0": None if not np.isfinite(coeff[3]) else round(coeff[3], 6),
            }
        )
    dest = folder / "narrow_cross_section.csv"
    pd.DataFrame(rows).to_csv(dest, index=False)
    print(f"wrote {dest}", flush=True)
    print(
        "(open the following in qgis or similar tool to check: "
        f"{folder / 'narrow_cross_section.png'}, {dest})",
        flush=True,
    )


if __name__ == "__main__":
    main()
