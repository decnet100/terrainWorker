"""Continue the cross-section inward from the cover fill. No interior median.

Starts from ``dgm_repair_cross_cover``. Pixels that cover already wrote stay.
Inward of that strip the same line continues only while the raw height disagrees
with it, and at most 1.5 m. The walk stops at the first unwritten pixel whose
raw height is already within 1.5 cm of the line. It does not remeasure roughness
and it does not search for a new profile. The median pass on interior spikes is
not applied. The earlier run that included it stays in ``dgm_repair_cross_settle``.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\repair_dgm_cross_settle.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import tifffile
from scipy.ndimage import binary_dilation

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_road_grid import RES_M, _load_geotiff  # noqa: E402
from filter_road_corridor import write_referenced_tif  # noqa: E402
from repair_dgm_cross_cover import _apply, _in_quad  # noqa: E402
from repair_dgm_cross_section import _inward  # noqa: E402
from repair_dgm_cross_var import _choose_profiles, _close_gaps, _rays, _road_only  # noqa: E402
from repair_dgm_edge_profile import _densify  # noqa: E402
from repair_dgm_iterate import _deviation, _prepare, _road_mask, _stats  # noqa: E402
from repair_dgm_side_band import DROP_ROUGH_M  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

STATION_M = 0.5
EXTEND_M = 1.5
SPIKE_M = 0.05
MIN_MEDIAN = 3
MAX_RAD_PX = 4


def _blank(shape):
    return (
        np.full(shape, np.nan, dtype=np.float32),
        np.full(shape, np.inf, dtype=np.float32),
    )


def _extend_band(xy, inward, xa, za, slope, source, raw, written, road, spec, slot) -> None:
    z_out, d_out = slot
    height, width = road.shape
    for i in range(len(xy) - 1):
        if source[i] == 0 or source[i + 1] == 0:
            continue
        if not (
            np.isfinite(xa[i])
            and np.isfinite(xa[i + 1])
            and np.isfinite(za[i])
            and np.isfinite(za[i + 1])
            and np.isfinite(slope[i])
            and np.isfinite(slope[i + 1])
            and xa[i] > 0.05
            and xa[i + 1] > 0.05
        ):
            continue
        p0 = np.asarray(xy[i], dtype=np.float64).reshape(2)
        p1 = np.asarray(xy[i + 1], dtype=np.float64).reshape(2)
        n0 = np.asarray(inward[i], dtype=np.float64).reshape(2)
        n1 = np.asarray(inward[i + 1], dtype=np.float64).reshape(2)
        span = float(np.hypot(p1[0] - p0[0], p1[1] - p0[1]))
        if span < 1e-4:
            continue
        reach0 = float(xa[i]) + EXTEND_M
        reach1 = float(xa[i + 1]) + EXTEND_M
        a0 = p0 + n0 * float(xa[i])
        a1 = p1 + n1 * float(xa[i + 1])
        b0 = p0 + n0 * reach0
        b1 = p1 + n1 * reach1
        corners = np.vstack([a0, a1, b1, b0])
        xs = (float(a0[0]), float(a1[0]), float(b0[0]), float(b1[0]))
        ys = (float(a0[1]), float(a1[1]), float(b0[1]), float(b1[1]))
        c0 = int(np.floor((min(xs) - float(spec["xmin"])) / RES_M))
        c1 = int(np.ceil((max(xs) - float(spec["xmin"])) / RES_M))
        r0 = int(np.floor((float(spec["ymax"]) - max(ys)) / RES_M))
        r1 = int(np.ceil((float(spec["ymax"]) - min(ys)) / RES_M))
        if c0 < 0:
            c0 = 0
        if r0 < 0:
            r0 = 0
        if c1 > width:
            c1 = width
        if r1 > height:
            r1 = height
        if c1 <= c0 or r1 <= r0:
            continue
        inside = road[r0:r1, c0:c1]
        if not np.any(inside):
            continue
        cc = np.arange(c0, c1)
        rr = np.arange(r0, r1)
        xx = spec["xmin"] + (cc + 0.5) * RES_M
        yy = spec["ymax"] - (rr + 0.5) * RES_M
        gx, gy = np.meshgrid(xx, yy)
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
        inx = (1.0 - t) * n0[0] + t * n1[0]
        iny = (1.0 - t) * n0[1] + t * n1[1]
        norm = np.maximum(np.hypot(inx, iny), 1e-6)
        inx = inx / norm
        iny = iny / norm
        offset = (px - (p0[0] + t * dx)) * inx + (py - (p0[1] + t * dy)) * iny
        xa_t = (1.0 - t) * float(xa[i]) + t * float(xa[i + 1])
        za_t = (1.0 - t) * float(za[i]) + t * float(za[i + 1])
        b_t = (1.0 - t) * float(slope[i]) + t * float(slope[i + 1])
        band = (offset + 1e-6 >= xa_t) & (offset < xa_t + EXTEND_M) & np.isfinite(za_t) & np.isfinite(b_t)
        if not np.any(band):
            continue
        sel = np.flatnonzero(band)
        sel = sel[np.argsort(offset[sel], kind="mergesort")]
        raw_z = raw[flat_r[sel], flat_c[sel]].astype(np.float64)
        z_line = za_t[sel] + b_t[sel] * (offset[sel] - xa_t[sel])
        already = written[flat_r[sel], flat_c[sel]]
        finite = np.isfinite(raw_z)
        stop_here = (~already) & ((~finite) | (np.abs(raw_z - z_line) <= DROP_ROUGH_M))
        if np.any(stop_here):
            sel = sel[: int(np.argmax(stop_here))]
            if len(sel) == 0:
                continue
            raw_z = raw[flat_r[sel], flat_c[sel]].astype(np.float64)
            z_line = za_t[sel] + b_t[sel] * (offset[sel] - xa_t[sel])
            already = written[flat_r[sel], flat_c[sel]]
            finite = np.isfinite(raw_z)
        take = (~already) & finite & (np.abs(raw_z - z_line) > DROP_ROUGH_M)
        if not np.any(take):
            continue
        rr_i = flat_r[sel[take]]
        cc_i = flat_c[sel[take]]
        off = offset[sel[take]]
        closer = off < d_out[rr_i, cc_i]
        if not np.any(closer):
            continue
        z_out[rr_i[closer], cc_i[closer]] = z_line[take][closer].astype(np.float32)
        d_out[rr_i[closer], cc_i[closer]] = off[closer]


def _median_spikes(z: np.ndarray, on_road: np.ndarray, written: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Replace unwritten spikes that do not touch the fill. Returns height and mask."""
    rough = _road_only(z, on_road)
    away = on_road & np.isfinite(rough) & (rough >= SPIKE_M) & ~written
    away &= ~binary_dilation(written, iterations=1)
    out = z.copy()
    replaced = np.zeros(z.shape, dtype=bool)
    if not np.any(away):
        return out, replaced
    masked = np.where(on_road & ~away, z, np.nan).astype(np.float32)
    smooth = _road_only(np.where(np.isfinite(masked), masked, np.nan), on_road & ~away)
    good = on_road & ~away & np.isfinite(smooth) & (smooth < DROP_ROUGH_M) & np.isfinite(z)
    ys, xs = np.nonzero(away)
    height, width = z.shape
    for r, c in zip(ys.tolist(), xs.tolist()):
        found = None
        for rad in range(1, MAX_RAD_PX + 1):
            r0 = r - rad if r - rad > 0 else 0
            c0 = c - rad if c - rad > 0 else 0
            r1 = r + rad + 1 if r + rad + 1 < height else height
            c1 = c + rad + 1 if c + rad + 1 < width else width
            win = good[r0:r1, c0:c1]
            if int(win.sum()) < MIN_MEDIAN:
                continue
            found = float(np.median(z[r0:r1, c0:c1][win]))
            break
        if found is None:
            continue
        out[r, c] = found
        replaced[r, c] = True
    return out, replaced


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    raw_path = proc / "corridor50_raw" / "corridor50_raw.tif"
    gpkg_in = proc / "centerline_shift_taper" / "centerline_shift.gpkg"
    cover_dir = proc / "dgm_repair_cross_cover"
    for path in (raw_path, gpkg_in, cover_dir / "02_repaired.tif", cover_dir / "01_class.tif"):
        if not path.is_file():
            raise SystemExit(f"missing {path}")
    out_dir = proc / "dgm_repair_cross_extend"
    out_dir.mkdir(parents=True, exist_ok=True)

    raw, spec = _load_geotiff(raw_path)
    rough = _deviation(raw)
    cover_z = tifffile.imread(cover_dir / "02_repaired.tif").astype(np.float32)
    cover_k = tifffile.imread(cover_dir / "01_class.tif")
    if cover_z.shape != raw.shape or cover_k.shape != raw.shape:
        raise SystemExit("cover rasters do not match the raw DGM")
    base = raw.copy()
    have_cover = np.isfinite(cover_z)
    base[have_cover] = cover_z[have_cover]
    written0 = cover_k >= 3

    roads = gpd.read_file(gpkg_in, layer="carriageway")
    lines = gpd.read_file(gpkg_in, layer="centerline")
    print("preparing edges", flush=True)
    parts = _prepare(roads, lines)
    road = _road_mask(parts, raw.shape, spec)
    on_road = road & np.isfinite(raw) & np.isfinite(rough)
    before_full = _stats(_deviation(base), on_road)
    before_road = _stats(_road_only(base, on_road), on_road)
    print(f"  {len(parts)} parts, cover road-only hot {before_road['hot_px']}", flush=True)

    proposal = {name: _blank(raw.shape) for name in ("left", "right")}
    for part in parts:
        for name, edge in part["sides"].items():
            xy, station, normals = _densify([(float(x), float(y)) for x, y in edge.coords], STATION_M)
            if len(xy) < 2 or np.allclose(normals, 0):
                continue
            inward = _inward(xy, normals, part["poly"])
            offs, zz, rr, inside, _rows, _cols = _rays(xy, inward, raw, rough, road, spec)
            good = inside & np.isfinite(zz) & np.isfinite(rr) & (rr < DROP_ROUGH_M)
            xa, za, slope, source = _choose_profiles(offs, zz, good, xy)
            xa, za, slope, source = _close_gaps(xy, station, xa, za, slope, source)
            _extend_band(xy, inward, xa, za, slope, source, raw, written0, road, spec, proposal[name])
        print(f"  oid {part['oid']}", flush=True)

    zl, dl = proposal["left"]
    zr, dr = proposal["right"]
    working, only_l, only_r, both = _apply(base, on_road, rough, zl, zr, dl, dr)
    extended = (only_l | only_r | both) & ~written0
    working = np.where(on_road, working, base)
    # _apply may also have proposals on pixels cover already wrote; put those back.
    working[written0] = base[written0]
    extended &= ~written0

    print(f"  extended {int(extended.sum())}", flush=True)
    settled = working

    after_full = _stats(_deviation(settled), on_road)
    after_road = _stats(_road_only(settled, on_road), on_road)
    klass = cover_k.astype(np.uint8).copy()
    klass[extended] = 9
    repaired = np.where(on_road, settled, np.nan).astype(np.float32)
    shown = np.where(on_road, _deviation(settled), np.nan).astype(np.float32)
    write_referenced_tif(out_dir / "02_repaired.tif", repaired, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    write_referenced_tif(out_dir / "01_class.tif", klass, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    write_referenced_tif(out_dir / "03_roughness.tif", shown, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    changed = extended
    delta = np.abs(settled[changed].astype(np.float64) - raw[changed].astype(np.float64))
    report = {
        "heights": str(raw_path),
        "cover": str(cover_dir),
        "extend_m": EXTEND_M,
        "agree_m": DROP_ROUGH_M,
        "spike_m": SPIKE_M,
        "before_full_window": before_full,
        "after_full_window": after_full,
        "before_road_only": before_road,
        "after_road_only": after_road,
        "extended_px": int(extended.sum()),
        "blended_px": int((both & extended).sum()),
        "median": "not applied",
        "max_delta_m": round(float(delta.max()) if len(delta) else 0.0, 4),
        "delta_p50_m": round(float(np.percentile(delta, 50)) if len(delta) else 0.0, 4),
        "delta_p99_m": round(float(np.percentile(delta, 99)) if len(delta) else 0.0, 4),
        "class": {
            "1-8": "unchanged from dgm_repair_cross_cover",
            "9": "same cross-section continued inward until the raw height agrees, at most 1.5 m",
        },
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "extended_px": report["extended_px"],
                "before_road_p99": before_road["p99_m"],
                "after_road_p99": after_road["p99_m"],
                "before_full_p99": before_full["p99_m"],
                "after_full_p99": after_full["p99_m"],
                "before_road_hot": before_road["hot_px"],
                "after_road_hot": after_road["hot_px"],
                "max_delta_m": report["max_delta_m"],
            },
            indent=2,
        )
    )
    print(f"Wrote {out_dir}")


if __name__ == "__main__":
    main()
