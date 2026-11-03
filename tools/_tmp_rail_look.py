"""Print edge-rise scores for one road already sampled by sample_cross_profile.

Reads the smooth layer and, at each station, the raw DGM out to 6 m from
the smoothed axis. A peak is the highest residual of the interior cubic.
Left is the positive normal of the sampled line.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\_tmp_rail_look.py 4580
    python tools\\_tmp_rail_look.py 4580 500 508
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sample_cross_profile as sc
from build_road_grid import _load_geotiff
from site_coords import load_site, processed_dir

LOOK_M = 6.0


def _gpkg(proc: Path, oid: int) -> Path:
    direct = proc / "cross_profile" / f"{oid}.gpkg"
    nxt = proc / "cross_profile" / f"{oid}_next.gpkg"
    if direct.is_file():
        return direct
    if nxt.is_file():
        return nxt
    raise SystemExit(f"no cross_profile for {oid}: {direct}")


def _normal(pts: list, i: int) -> tuple[float, float]:
    a = pts[max(0, i - 1)].geometry
    b = pts[min(len(pts) - 1, i + 1)].geometry
    dx, dy = b.x - a.x, b.y - a.y
    span = math.hypot(dx, dy)
    if span < 1e-6:
        return 0.0, 0.0
    return -dy / span, dx / span


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].lstrip("-").isdigit():
        raise SystemExit("usage: python tools/_tmp_rail_look.py OBJECTID [s_m ...]")
    oid = int(sys.argv[1])
    wanted = [float(arg) for arg in sys.argv[2:]]
    site = load_site()
    proc = processed_dir(site)
    gpkg = _gpkg(proc, oid)
    raw = proc / "corridor50_raw" / "corridor50_raw.tif"
    if not raw.is_file():
        raise SystemExit(f"raw DGM missing: {raw}")
    smooth = gpd.read_file(gpkg, layer="smooth").sort_values("s_m").reset_index(drop=True)
    if "rise_left_m" not in smooth.columns:
        raise SystemExit(f"{gpkg} has no rise_left_m; rerun sample_cross_profile.py {oid}")
    elev, spec = _load_geotiff(raw)
    width = float(smooth.width_m.iloc[0])
    half = width / 2.0
    inset = half - sc.EDGE_IN_M
    print(
        f"{gpkg}  width {width:.1f} m  n {len(smooth)}  "
        f"left p90 {smooth.rise_left_m.quantile(0.9):.3f} max {smooth.rise_left_m.max():.3f}  "
        f"right p90 {smooth.rise_right_m.quantile(0.9):.3f} max {smooth.rise_right_m.max():.3f}"
    )
    print(
        f"band rise>=0.10 left {int((smooth.rise_left_m >= 0.10).sum())} "
        f"right {int((smooth.rise_right_m >= 0.10).sum())}  "
        f">=0.15 left {int((smooth.rise_left_m >= 0.15).sum())} "
        f"right {int((smooth.rise_right_m >= 0.15).sum())}  "
        f"rail left {int(smooth.rail_left.sum())} right {int(smooth.rail_right.sum())}"
    )
    pts = list(smooth.itertuples())
    hits: list[tuple[float, float, float]] = []
    for i, row in enumerate(pts):
        nx, ny = _normal(pts, i)
        if nx == 0.0 and ny == 0.0:
            continue
        lat, height = sc._ribbon_samples(
            elev, spec, row.geometry.x, row.geometry.y, nx, ny, LOOK_M
        )
        interior = np.abs(lat) <= inset + 1e-6
        if int(interior.sum()) < sc.MIN_SAMPLES:
            continue
        resid = height - sc._cubic_predict(lat[interior], height[interior], lat)
        j = int(np.argmax(resid))
        if float(resid[j]) >= sc.RAIL_RISE_M:
            hits.append((float(row.s_m), float(lat[j]), float(resid[j])))
    print(f"whole-transect peaks >= {sc.RAIL_RISE_M:.2f} m: {len(hits)}")
    for s_m, t_m, rise in hits:
        print(f"  s {s_m:7.1f}  t {t_m:+5.2f}  resid {rise:+.3f}")
    for target in wanted:
        i = next((k for k, row in enumerate(pts) if abs(row.s_m - target) < 0.1), None)
        if i is None:
            print(f"s {target:.0f} not on this line")
            continue
        row = pts[i]
        nx, ny = _normal(pts, i)
        lat, height = sc._ribbon_samples(
            elev, spec, row.geometry.x, row.geometry.y, nx, ny, LOOK_M
        )
        interior = np.abs(lat) <= inset + 1e-6
        resid = height - sc._cubic_predict(lat[interior], height[interior], lat)
        z0 = float(height[int(np.argmin(np.abs(lat)))])
        print(f"s {target:.0f}")
        for t_m, z_m, r_m in zip(lat, height, resid):
            if abs(float(t_m)) % 0.5 > 0.05 and float(r_m) < 0.15:
                continue
            print(f"  t {t_m:+5.2f}  dz {z_m - z0:+6.2f}  resid {r_m:+6.3f}")
    print(f"(open the following in qgis or similar tool to check: {gpkg})")


if __name__ == "__main__":
    main()
