"""Write the cross-section onto every pixel of the edge gap, then one seam pass.

The gap is the whole strip from the edge to the first good pixel, not the ray
samples. Height is interpolated along the station and by offset from the edge.
A second pass writes only road pixels one step inward of that strip whose
deviation, measured on the carriageway alone, is then at least 1.5 cm. The
profile is not searched again.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\repair_dgm_cross_cover.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
from scipy.ndimage import binary_dilation
from shapely import line_locate_point, points
from shapely.geometry import LineString

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_road_grid import RES_M, _load_geotiff  # noqa: E402
from filter_road_corridor import write_referenced_tif  # noqa: E402
from repair_dgm_cross_section import _inward  # noqa: E402
from repair_dgm_cross_var import (  # noqa: E402
    _choose_profiles,
    _close_gaps,
    _rays,
    _road_only,
)
from repair_dgm_edge_profile import _densify  # noqa: E402
from repair_dgm_iterate import _deviation, _prepare, _road_mask, _stats  # noqa: E402
from repair_dgm_side_band import DROP_ROUGH_M  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

STATION_M = 0.5
SEAM_M = 0.75


def _in_quad(px: np.ndarray, py: np.ndarray, corners: np.ndarray) -> np.ndarray:
    x0 = corners[:, 0]
    y0 = corners[:, 1]
    x1 = np.roll(x0, -1)
    y1 = np.roll(y0, -1)
    cross = (x1 - x0)[None, :] * (py[:, None] - y0) - (y1 - y0)[None, :] * (px[:, None] - x0)
    return np.all(cross >= -1e-4, axis=1) | np.all(cross <= 1e-4, axis=1)


def _paint_strip(xy, inward, xa, za, slope, source, road, spec, slot) -> None:
    z_out, d_out, s_out = slot
    height, width = road.shape
    for i in range(len(xy) - 1):
        if source[i] == 0 or source[i + 1] == 0:
            continue
        if not (np.isfinite(xa[i]) and np.isfinite(xa[i + 1]) and xa[i] > 0.05 and xa[i + 1] > 0.05):
            continue
        p0 = np.asarray(xy[i], dtype=np.float64).reshape(2)
        p1 = np.asarray(xy[i + 1], dtype=np.float64).reshape(2)
        n0 = np.asarray(inward[i], dtype=np.float64).reshape(2)
        n1 = np.asarray(inward[i + 1], dtype=np.float64).reshape(2)
        span = float(np.hypot(p1[0] - p0[0], p1[1] - p0[1]))
        if span < 1e-4:
            continue
        q0 = p0 + n0 * float(xa[i])
        q1 = p1 + n1 * float(xa[i + 1])
        corners = np.vstack([p0, p1, q1, q0])
        xs = (float(p0[0]), float(p1[0]), float(q0[0]), float(q1[0]))
        ys = (float(p0[1]), float(p1[1]), float(q0[1]), float(q1[1]))
        c0 = int(np.floor((float(min(xs)) - float(spec["xmin"])) / float(RES_M)))
        c1 = int(np.ceil((float(max(xs)) - float(spec["xmin"])) / float(RES_M)))
        r0 = int(np.floor((float(spec["ymax"]) - float(max(ys))) / float(RES_M)))
        r1 = int(np.ceil((float(spec["ymax"]) - float(min(ys))) / float(RES_M)))
        if c0 < 0:
            c0 = 0
        if r0 < 0:
            r0 = 0
        if c1 > int(width):
            c1 = int(width)
        if r1 > int(height):
            r1 = int(height)
        if c1 <= c0 or r1 <= r0:
            continue
        cc = np.arange(c0, c1)
        rr = np.arange(r0, r1)
        xx = spec["xmin"] + (cc + 0.5) * RES_M
        yy = spec["ymax"] - (rr + 0.5) * RES_M
        gx, gy = np.meshgrid(xx, yy)
        inside = road[r0:r1, c0:c1]
        if not np.any(inside):
            continue
        flat_r = np.repeat(rr, c1 - c0)
        flat_c = np.tile(cc, r1 - r0)
        px = gx.ravel()
        py = gy.ravel()
        keep = inside.ravel() & _in_quad(px, py, corners)
        if not np.any(keep):
            continue
        px = px[keep]
        py = py[keep]
        flat_r = flat_r[keep]
        flat_c = flat_c[keep]
        dx = float(p1[0] - p0[0])
        dy = float(p1[1] - p0[1])
        t = np.clip(((px - p0[0]) * dx + (py - p0[1]) * dy) / (span * span), 0.0, 1.0)
        inx = (1.0 - t) * inward[i, 0] + t * inward[i + 1, 0]
        iny = (1.0 - t) * inward[i, 1] + t * inward[i + 1, 1]
        norm = np.maximum(np.hypot(inx, iny), 1e-6)
        inx /= norm
        iny /= norm
        foot_x = p0[0] + t * dx
        foot_y = p0[1] + t * dy
        offset = (px - foot_x) * inx + (py - foot_y) * iny
        xa_t = (1.0 - t) * xa[i] + t * xa[i + 1]
        za_t = (1.0 - t) * za[i] + t * za[i + 1]
        b_t = (1.0 - t) * slope[i] + t * slope[i + 1]
        src_t = np.where(t < 0.5, source[i], source[i + 1]).astype(np.uint8)
        use = (offset >= -0.05) & (offset < xa_t) & np.isfinite(za_t) & np.isfinite(b_t)
        if not np.any(use):
            continue
        z_pix = (za_t[use] + b_t[use] * (offset[use] - xa_t[use])).astype(np.float32)
        rr_i = flat_r[use]
        cc_i = flat_c[use]
        closer = offset[use] < d_out[rr_i, cc_i]
        if not np.any(closer):
            continue
        z_out[rr_i[closer], cc_i[closer]] = z_pix[closer]
        d_out[rr_i[closer], cc_i[closer]] = offset[use][closer]
        s_out[rr_i[closer], cc_i[closer]] = src_t[use][closer]


def _paint_seam(xy, inward, xa, za, slope, source, candidates, spec, slot) -> None:
    z_out, d_out, s_out = slot
    if len(xy) < 2 or not np.any(candidates):
        return
    line = LineString(xy)
    height, width = candidates.shape
    pad = 8.0
    minx, miny = xy.min(axis=0) - pad
    maxx, maxy = xy.max(axis=0) + pad
    c0 = max(0, int(np.floor((minx - spec["xmin"]) / RES_M)))
    c1 = min(width, int(np.ceil((maxx - spec["xmin"]) / RES_M)))
    r1 = min(height, int(np.ceil((spec["ymax"] - miny) / RES_M)))
    r0 = max(0, int(np.floor((spec["ymax"] - maxy) / RES_M)))
    if c1 <= c0 or r1 <= r0:
        return
    sub = np.argwhere(candidates[r0:r1, c0:c1])
    if len(sub) == 0:
        return
    rr = sub[:, 0] + r0
    cc = sub[:, 1] + c0
    px = spec["xmin"] + (cc + 0.5) * RES_M
    py = spec["ymax"] - (rr + 0.5) * RES_M
    located = np.asarray(line_locate_point(line, points(px, py)), dtype=np.float64)
    station = np.zeros(len(xy))
    station[1:] = np.cumsum(np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1])))
    j = np.clip(np.searchsorted(station, located, side="right") - 1, 0, len(xy) - 2)
    t = (located - station[j]) / np.maximum(station[j + 1] - station[j], 1e-6)
    t = np.clip(t, 0.0, 1.0)
    inx = (1.0 - t) * inward[j, 0] + t * inward[j + 1, 0]
    iny = (1.0 - t) * inward[j, 1] + t * inward[j + 1, 1]
    norm = np.maximum(np.hypot(inx, iny), 1e-6)
    foot_x = (1.0 - t) * xy[j, 0] + t * xy[j + 1, 0]
    foot_y = (1.0 - t) * xy[j, 1] + t * xy[j + 1, 1]
    offset = (px - foot_x) * (inx / norm) + (py - foot_y) * (iny / norm)
    xa_t = (1.0 - t) * xa[j] + t * xa[j + 1]
    za_t = (1.0 - t) * za[j] + t * za[j + 1]
    b_t = (1.0 - t) * slope[j] + t * slope[j + 1]
    src = np.maximum(source[j], source[j + 1])
    use = (
        (source[j] > 0)
        & (source[j + 1] > 0)
        & (offset >= xa_t)
        & (offset < xa_t + SEAM_M)
        & np.isfinite(za_t)
        & np.isfinite(b_t)
    )
    if not np.any(use):
        return
    z_pix = (za_t[use] + b_t[use] * (offset[use] - xa_t[use])).astype(np.float32)
    # Distance past the anchor, so the nearer edge still wins inside one side.
    past = (offset[use] - xa_t[use]).astype(np.float32)
    rr_i = rr[use]
    cc_i = cc[use]
    closer = past < d_out[rr_i, cc_i]
    if not np.any(closer):
        return
    z_out[rr_i[closer], cc_i[closer]] = z_pix[closer]
    d_out[rr_i[closer], cc_i[closer]] = past[closer]
    s_out[rr_i[closer], cc_i[closer]] = src[use][closer]


def _apply(raw, on_road, rough, zl, zr, dl, dr):
    have_l = np.isfinite(zl)
    have_r = np.isfinite(zr)
    both = on_road & have_l & have_r
    only_l = on_road & have_l & ~have_r
    only_r = on_road & have_r & ~have_l
    dl_w = np.maximum(dl, 0.05)
    dr_w = np.maximum(dr, 0.05)
    working = raw.copy()
    working[only_l] = zl[only_l]
    working[only_r] = zr[only_r]
    if np.any(both):
        working[both] = ((zl[both] * dr_w[both] + zr[both] * dl_w[both]) / (dl_w[both] + dr_w[both])).astype(np.float32)
    return working, only_l, only_r, both


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    raw_path = proc / "corridor50_raw" / "corridor50_raw.tif"
    gpkg_in = proc / "centerline_shift_taper" / "centerline_shift.gpkg"
    for path in (raw_path, gpkg_in):
        if not path.is_file():
            raise SystemExit(f"missing {path}")
    out_dir = proc / "dgm_repair_cross_cover"
    out_dir.mkdir(parents=True, exist_ok=True)

    raw, spec = _load_geotiff(raw_path)
    rough = _deviation(raw)
    roads = gpd.read_file(gpkg_in, layer="carriageway")
    lines = gpd.read_file(gpkg_in, layer="centerline")
    print("preparing edges", flush=True)
    parts = _prepare(roads, lines)
    road = _road_mask(parts, raw.shape, spec)
    on_road = road & np.isfinite(raw) & np.isfinite(rough)
    before = _stats(rough, on_road)
    print(f"  {len(parts)} parts, hot {before['hot_px']}", flush=True)

    def blank():
        return (
            np.full(raw.shape, np.nan, dtype=np.float32),
            np.full(raw.shape, np.inf, dtype=np.float32),
            np.zeros(raw.shape, dtype=np.uint8),
        )

    proposal = {name: blank() for name in ("left", "right")}
    seam_prop = {name: blank() for name in ("left", "right")}
    kept = []
    for part in parts:
        for name, edge in part["sides"].items():
            xy, _station, normals = _densify([(float(x), float(y)) for x, y in edge.coords], STATION_M)
            if len(xy) < 2 or np.allclose(normals, 0):
                continue
            inward = _inward(xy, normals, part["poly"])
            offs, zz, rr, inside, _rows, _cols = _rays(xy, inward, raw, rough, road, spec)
            good = inside & np.isfinite(zz) & np.isfinite(rr) & (rr < DROP_ROUGH_M)
            xa, za, slope, source = _choose_profiles(offs, zz, good, xy)
            xa, za, slope, source = _close_gaps(xy, _station, xa, za, slope, source)
            _paint_strip(xy, inward, xa, za, slope, source, road, spec, proposal[name])
            kept.append((name, xy, inward, xa, za, slope, source))
        print(f"  oid {part['oid']}", flush=True)

    zl, dl, sl = proposal["left"]
    zr, dr, sr = proposal["right"]
    working, only_l, only_r, both = _apply(raw, on_road, rough, zl, zr, dl, dr)
    filled = only_l | only_r | both
    hybrid = raw.copy()
    hybrid[on_road] = working[on_road]
    road_rough = _road_only(hybrid, on_road)
    seam = binary_dilation(filled, iterations=1) & on_road & ~filled & np.isfinite(road_rough) & (road_rough >= DROP_ROUGH_M)
    print(f"  seam candidates {int(seam.sum())}", flush=True)
    for name, xy, inward, xa, za, slope, source in kept:
        _paint_seam(xy, inward, xa, za, slope, source, seam, spec, seam_prop[name])
    szl, sdl, ssl = seam_prop["left"]
    szr, sdr, ssr = seam_prop["right"]
    seam_work, s_only_l, s_only_r, s_both = _apply(working, on_road, road_rough, szl, szr, sdl, sdr)
    # _apply copies its first argument and writes proposals. working already has pass 1.
    working = seam_work
    seam_take = s_only_l | s_only_r | s_both

    after = _stats(_deviation(working), on_road)
    hybrid[on_road] = working[on_road]
    road_after = _stats(_road_only(hybrid, on_road), on_road)
    road_before = _stats(_road_only(raw, on_road), on_road)

    klass = np.zeros(raw.shape, dtype=np.uint8)
    klass[on_road & (rough < DROP_ROUGH_M) & ~filled & ~seam_take] = 1
    klass[on_road & (rough >= DROP_ROUGH_M) & ~filled & ~seam_take] = 2
    for mask, src in ((only_l, sl), (only_r, sr)):
        klass[mask & (src == 1)] = 3
        klass[mask & (src == 2)] = 4
        klass[mask & (src == 3)] = 5
        klass[mask & (src == 4)] = 6
    klass[both] = 7
    klass[seam_take] = 8
    repaired = np.where(on_road, working, np.nan).astype(np.float32)
    shown = np.where(on_road, _deviation(working), np.nan).astype(np.float32)
    write_referenced_tif(out_dir / "02_repaired.tif", repaired, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    write_referenced_tif(out_dir / "01_class.tif", klass, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    write_referenced_tif(out_dir / "03_roughness.tif", shown, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    take = filled | seam_take
    delta = np.abs(working[take].astype(np.float64) - raw[take].astype(np.float64))
    report = {
        "heights": str(raw_path),
        "carriageway": str(gpkg_in),
        "drop_rough_m": DROP_ROUGH_M,
        "seam_m": SEAM_M,
        "before_full_window": before,
        "after_full_window": after,
        "before_road_only": road_before,
        "after_road_only": road_after,
        "strip_px": int(filled.sum()),
        "blended_px": int(both.sum()),
        "seam_px": int(seam_take.sum()),
        "left_raw_px": int((klass == 2).sum()),
        "max_delta_m": round(float(delta.max()) if len(delta) else 0.0, 4),
        "delta_p50_m": round(float(np.percentile(delta, 50)) if len(delta) else 0.0, 4),
        "delta_p99_m": round(float(np.percentile(delta, 99)) if len(delta) else 0.0, 4),
        "class": {
            "1": "passed on the raw DGM and was not in a gap or on the seam",
            "2": "rough pixel left at the raw height",
            "3": "gap pixel, local cross-section",
            "4": "gap pixel, slope taken from the variable search",
            "5": "gap pixel, linear along the edge",
            "6": "gap pixel, parabolic height along the edge",
            "7": "both edges cover the pixel, height mixed by distance",
            "8": "one step inward of the gap, same profile, after the fill made it rough",
        },
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "strip_px": report["strip_px"],
                "seam_px": report["seam_px"],
                "blended_px": report["blended_px"],
                "left_raw_px": report["left_raw_px"],
                "before_full_p99": before["p99_m"],
                "after_full_p99": after["p99_m"],
                "before_road_p99": road_before["p99_m"],
                "after_road_p99": road_after["p99_m"],
                "max_delta_m": report["max_delta_m"],
                "delta_p50_m": report["delta_p50_m"],
                "delta_p99_m": report["delta_p99_m"],
            },
            indent=2,
        )
    )
    print(f"Wrote {out_dir}")


if __name__ == "__main__":
    main()
