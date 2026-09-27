"""Set the first metre outside the carriageway to the road-edge height.

The height is the cover fill at the lip, constant across that metre. No
outward fall. Structure spans are left alone: a station on a bridge, tunnel
or gallery does not push the ground beside the deck. Nothing is written
into a BeamNG level.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\repair_dgm_shoulder.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
import tifffile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_road_grid import RES_M, _load_geotiff  # noqa: E402
from filter_road_corridor import write_referenced_tif  # noqa: E402
from repair_dgm_cross_cover import _in_quad  # noqa: E402
from repair_dgm_cross_section import _inward  # noqa: E402
from repair_dgm_edge_profile import _densify  # noqa: E402
from repair_dgm_iterate import _prepare, _road_mask  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

STATION_M = 0.5
SHELF_M = 1.0
LIP_IN_M = 0.25


def _structure_hits(xy: np.ndarray, zones: list) -> np.ndarray:
    hit = np.zeros(len(xy), dtype=bool)
    if len(xy) == 0 or not zones:
        return hit
    pts = shapely.points(xy[:, 0], xy[:, 1])
    for zone in zones:
        hit |= np.asarray(shapely.intersects(zone, pts), dtype=bool)
    return hit


def _sample(z: np.ndarray, xy: np.ndarray, inward: np.ndarray, dist: float, spec: dict) -> np.ndarray:
    xs = xy[:, 0] + dist * inward[:, 0]
    ys = xy[:, 1] + dist * inward[:, 1]
    cc = np.floor((xs - spec["xmin"]) / RES_M).astype(np.int32)
    rr = np.floor((spec["ymax"] - ys) / RES_M).astype(np.int32)
    height, width = z.shape
    ok = (rr >= 0) & (cc >= 0) & (rr < height) & (cc < width)
    out = np.full(len(xy), np.nan, dtype=np.float64)
    out[ok] = z[rr[ok], cc[ok]]
    return out


def _paint_shelf(xy, inward, lip, blocked, road, spec, slot) -> None:
    z_out, d_out = slot
    height, width = road.shape
    outward = -inward
    for i in range(len(xy) - 1):
        if blocked[i] or blocked[i + 1]:
            continue
        if not (np.isfinite(lip[i]) and np.isfinite(lip[i + 1])):
            continue
        p0 = np.asarray(xy[i], dtype=np.float64).reshape(2)
        p1 = np.asarray(xy[i + 1], dtype=np.float64).reshape(2)
        n0 = np.asarray(outward[i], dtype=np.float64).reshape(2)
        n1 = np.asarray(outward[i + 1], dtype=np.float64).reshape(2)
        span = float(np.hypot(p1[0] - p0[0], p1[1] - p0[1]))
        if span < 1e-4:
            continue
        b0 = p0 + n0 * SHELF_M
        b1 = p1 + n1 * SHELF_M
        corners = np.vstack([p0, p1, b1, b0])
        xs = (float(p0[0]), float(p1[0]), float(b0[0]), float(b1[0]))
        ys = (float(p0[1]), float(p1[1]), float(b0[1]), float(b1[1]))
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
        outside = ~road[r0:r1, c0:c1]
        if not np.any(outside):
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
        keep = outside.ravel() & _in_quad(px, py, corners)
        if not np.any(keep):
            continue
        px = px[keep]
        py = py[keep]
        flat_r = flat_r[keep]
        flat_c = flat_c[keep]
        dx = float(p1[0] - p0[0])
        dy = float(p1[1] - p0[1])
        t = np.clip(((px - p0[0]) * dx + (py - p0[1]) * dy) / (span * span), 0.0, 1.0)
        ox = (1.0 - t) * n0[0] + t * n1[0]
        oy = (1.0 - t) * n0[1] + t * n1[1]
        norm = np.maximum(np.hypot(ox, oy), 1e-6)
        ox = ox / norm
        oy = oy / norm
        dist = (px - (p0[0] + t * dx)) * ox + (py - (p0[1] + t * dy)) * oy
        use = (dist >= -0.05) & (dist <= SHELF_M + 1e-6)
        if not np.any(use):
            continue
        z_pix = ((1.0 - t) * float(lip[i]) + t * float(lip[i + 1])).astype(np.float32)
        rr_i = flat_r[use]
        cc_i = flat_c[use]
        dd = dist[use]
        zz = z_pix[use]
        closer = dd < d_out[rr_i, cc_i]
        if not np.any(closer):
            continue
        z_out[rr_i[closer], cc_i[closer]] = zz[closer]
        d_out[rr_i[closer], cc_i[closer]] = dd[closer]


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    raw_path = proc / "corridor50_raw" / "corridor50_raw.tif"
    gpkg_in = proc / "centerline_shift_taper" / "centerline_shift.gpkg"
    cover_path = proc / "dgm_repair_cross_cover" / "02_repaired.tif"
    for path in (raw_path, gpkg_in, cover_path):
        if not path.is_file():
            raise SystemExit(f"missing {path}")
    out_dir = proc / "dgm_repair_cross_shoulder"
    out_dir.mkdir(parents=True, exist_ok=True)

    raw, spec = _load_geotiff(raw_path)
    cover = tifffile.imread(cover_path).astype(np.float32)
    if cover.shape != raw.shape:
        raise SystemExit("cover raster does not match the raw DGM")
    surface = raw.copy()
    have = np.isfinite(cover)
    surface[have] = cover[have]

    roads = gpd.read_file(gpkg_in, layer="carriageway")
    lines = gpd.read_file(gpkg_in, layer="centerline")
    segments = gpd.read_file(gpkg_in, layer="segments")
    zones = []
    for rec in segments.itertuples(index=False):
        if int(getattr(rec, "structure") or 0) != 1:
            continue
        width = float(getattr(rec, "width_used_m") or 0.0) or float(getattr(rec, "width_m") or 0.0) or 8.0
        zones.append(rec.geometry.buffer(width * 0.5 + 0.4))
    print(f"structure spans {len(zones)}", flush=True)

    parts = _prepare(roads, lines)
    road = _road_mask(parts, raw.shape, spec)
    z_out = np.full(raw.shape, np.nan, dtype=np.float32)
    d_out = np.full(raw.shape, np.inf, dtype=np.float32)
    for part in parts:
        for edge in part["sides"].values():
            xy, _station, normals = _densify([(float(x), float(y)) for x, y in edge.coords], STATION_M)
            if len(xy) < 2 or np.allclose(normals, 0):
                continue
            inward = _inward(xy, normals, part["poly"])
            lip = _sample(surface, xy, inward, LIP_IN_M, spec)
            blocked = _structure_hits(xy, zones)
            _paint_shelf(xy, inward, lip, blocked, road, spec, (z_out, d_out))
        print(f"  oid {part['oid']}", flush=True)

    shelf = np.isfinite(z_out) & ~road & np.isfinite(raw)
    out = raw.copy()
    out[have & road] = cover[have & road]
    out[shelf] = z_out[shelf]
    delta = np.abs(out[shelf].astype(np.float64) - raw[shelf].astype(np.float64))
    klass = np.zeros(raw.shape, dtype=np.uint8)
    klass[road & np.isfinite(out)] = 1
    klass[shelf] = 2
    write_referenced_tif(out_dir / "02_repaired.tif", np.where(np.isfinite(out), out, np.nan).astype(np.float32), xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    write_referenced_tif(out_dir / "01_class.tif", klass, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    report = {
        "heights": str(raw_path),
        "cover": str(cover_path),
        "shelf_m": SHELF_M,
        "shelf_px": int(shelf.sum()),
        "delta_p50_m": round(float(np.percentile(delta, 50)) if len(delta) else 0.0, 4),
        "delta_p90_m": round(float(np.percentile(delta, 90)) if len(delta) else 0.0, 4),
        "delta_p99_m": round(float(np.percentile(delta, 99)) if len(delta) else 0.0, 4),
        "max_delta_m": round(float(delta.max()) if len(delta) else 0.0, 4),
        "class": {
            "1": "carriageway, cover fill",
            "2": "first metre outside the edge, set to the lip height",
        },
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("shelf_px", "delta_p50_m", "delta_p90_m", "delta_p99_m", "max_delta_m")}, indent=2))
    print(f"Wrote {out_dir}")


if __name__ == "__main__":
    main()
