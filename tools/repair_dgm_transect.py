"""Fill rough carriageway edges from a cross-profile blended along the centerline.

Each half of the road is sampled on normals of the centerline. A clean sample
is a support of that half's profile. A rough sample, in particular the edge
that already belongs to the slope, is not. Between supports of the same offset,
at most 50 m apart, the height relative to the centerline is blended along the
arc, so the profile changes from station to station. A rough pixel takes that
blended profile at its own offset. No cross-slope is fitted at the broken station. Pixels outside the carriageway and stations on
a bridge, tunnel or gallery are left alone. Nothing is written into a BeamNG level.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\repair_dgm_transect.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import shapely
from shapely.geometry import LineString, Point

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_road_grid import RES_M, _load_geotiff  # noqa: E402
from filter_road_corridor import write_referenced_tif  # noqa: E402
from repair_dgm_cross_cover import _in_quad  # noqa: E402
from repair_dgm_cross_var import _road_only  # noqa: E402
from repair_dgm_edge_profile import _crop_inside, _densify  # noqa: E402
from repair_dgm_iterate import _deviation, _prepare, _road_mask, _stats  # noqa: E402
from repair_dgm_shoulder import _structure_hits  # noqa: E402
from repair_dgm_side_band import DROP_ROUGH_M  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

STATION_M = 0.5
RAY_STEP_M = 0.25
MAX_HALF_M = 12.0
MAX_GAP_M = 50.0
MIN_SAMPLES = 3
PLOT_OID = 6975
PLOT_FOOT = (28280.0, 238269.0)
PLOT_SPAN_M = 40.0


def _zones(segments) -> list:
    zones = []
    if segments is None or len(segments) == 0:
        return zones
    for rec in segments.itertuples(index=False):
        if int(getattr(rec, "structure") or 0) != 1:
            continue
        width = float(getattr(rec, "width_used_m") or 0.0) or float(getattr(rec, "width_m") or 0.0) or 8.0
        zones.append(rec.geometry.buffer(width * 0.5 + 0.4))
    return zones


def _structure_mask(zones, shape, spec) -> np.ndarray:
    mask = np.zeros(shape, dtype=bool)
    for zone in zones:
        cropped = _crop_inside(zone, spec)
        if cropped is None:
            continue
        inside, r0, c0 = cropped
        r1 = r0 + inside.shape[0]
        c1 = c0 + inside.shape[1]
        mask[r0:r1, c0:c1] |= inside
    return mask


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    runs = []
    n = len(mask)
    i = 0
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i + 1
        while j < n and mask[j]:
            j += 1
        if j - i >= 2:
            runs.append((i, j))
        i = j
    return runs


def _pieces(line: LineString, poly) -> list[LineString]:
    host = poly if poly.is_valid else poly.buffer(0)
    hit = line.intersection(host.buffer(2.0))
    if hit.is_empty:
        return []
    if hit.geom_type == "LineString":
        geoms = [hit]
    elif hit.geom_type == "MultiLineString":
        geoms = list(hit.geoms)
    elif hit.geom_type == "GeometryCollection":
        geoms = [g for g in hit.geoms if g.geom_type == "LineString"]
    else:
        return []
    return [g for g in geoms if g.length >= STATION_M * 2]


def _sample(xy, direction, z, rough, host, spec, offs):
    n = len(xy)
    k_n = len(offs)
    zz = np.full((k_n, n), np.nan, dtype=np.float64)
    rr = np.full((k_n, n), np.nan, dtype=np.float64)
    inside = np.zeros((k_n, n), dtype=bool)
    height, width = z.shape
    for k, dist in enumerate(offs):
        xs = xy[:, 0] + float(dist) * direction[:, 0]
        ys = xy[:, 1] + float(dist) * direction[:, 1]
        cc = np.floor((xs - spec["xmin"]) / RES_M).astype(np.int32)
        rr_i = np.floor((spec["ymax"] - ys) / RES_M).astype(np.int32)
        ok = (rr_i >= 0) & (cc >= 0) & (rr_i < height) & (cc < width)
        if not np.any(ok):
            continue
        xs_ok = xs[ok]
        ys_ok = ys[ok]
        hit = np.asarray(shapely.intersects(host, shapely.points(xs_ok, ys_ok)), dtype=bool)
        take = np.flatnonzero(ok)[hit]
        if len(take) == 0:
            continue
        inside[k, take] = True
        zz[k, take] = z[rr_i[take], cc[take]]
        rr[k, take] = rough[rr_i[take], cc[take]]
    return zz, rr, inside


def _blend_1d(station, valid, values, max_gap) -> np.ndarray:
    out = np.full(len(station), np.nan, dtype=np.float64)
    idx = np.flatnonzero(valid & np.isfinite(values))
    if len(idx) == 0:
        return out
    out[idx] = values[idx]
    if len(idx) < 2:
        return out
    filled = np.interp(station, station[idx], values[idx])
    gaps = station[idx[1:]] - station[idx[:-1]]
    for i0, i1, gap in zip(idx[:-1], idx[1:], gaps):
        if i1 <= i0 + 1 or gap > max_gap or gap < 1e-6:
            continue
        out[i0 + 1 : i1] = filled[i0 + 1 : i1]
    return out


def _blend_rows(station, valid, values, max_gap) -> np.ndarray:
    k_n, n = values.shape
    out = np.full((k_n, n), np.nan, dtype=np.float64)
    for k in range(k_n):
        out[k] = _blend_1d(station, valid[k], values[k], max_gap)
    return out


def _known_half(inside, zz, rr, zc) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Clean samples are the profile. A rough edge sample is not a support."""
    prefix = np.minimum.accumulate(inside, axis=0)
    if len(prefix):
        prefix = prefix.copy()
        prefix[0] = False
    donor = prefix & np.isfinite(zz) & np.isfinite(rr) & (rr < DROP_ROUGH_M) & np.isfinite(zc)
    known = donor.sum(axis=0) >= MIN_SAMPLES
    return prefix, donor, known


def _profile_at(z_prof, offset, t, i0: int) -> np.ndarray:
    step = RAY_STEP_M
    k0 = np.rint(offset / step).astype(np.int32)
    k_n = z_prof.shape[0]
    best_h = np.full(len(offset), np.nan, dtype=np.float64)
    best_d = np.full(len(offset), np.inf, dtype=np.float64)
    for dk in (-1, 0, 1):
        k = k0 + dk
        ok = (k >= 0) & (k < k_n)
        if not np.any(ok):
            continue
        h0 = np.full(len(offset), np.nan, dtype=np.float64)
        h1 = np.full(len(offset), np.nan, dtype=np.float64)
        h0[ok] = z_prof[k[ok], i0]
        h1[ok] = z_prof[k[ok], i0 + 1]
        dist = np.abs(offset - k.astype(np.float64) * step)
        good = ok & np.isfinite(h0) & np.isfinite(h1) & (dist <= RES_M) & (dist < best_d)
        if not np.any(good):
            continue
        best_h[good] = (1.0 - t[good]) * h0[good] + t[good] * h1[good]
        best_d[good] = dist[good]
    return best_h


def _paint_half(xy, direction, offs, prefix, z_prof, gap, usable, spec, slot) -> int:
    z_out, d_out, written = slot
    height, width = gap.shape
    filled = 0
    outer_k = np.where(prefix, np.arange(len(offs))[:, None], -1).max(axis=0)
    for i in range(len(xy) - 1):
        if not usable[i] or not usable[i + 1]:
            continue
        if outer_k[i] < 1 and outer_k[i + 1] < 1:
            continue
        reach = float(max(offs[outer_k[i]] if outer_k[i] >= 0 else 0.0, offs[outer_k[i + 1]] if outer_k[i + 1] >= 0 else 0.0))
        if reach < RAY_STEP_M:
            continue
        p0 = xy[i]
        p1 = xy[i + 1]
        n0 = direction[i]
        n1 = direction[i + 1]
        span = float(np.hypot(p1[0] - p0[0], p1[1] - p0[1]))
        if span < 1e-4:
            continue
        b0 = p0 + n0 * reach
        b1 = p1 + n1 * reach
        corners = np.vstack([p0, p1, b1, b0])
        xs = (float(p0[0]), float(p1[0]), float(b0[0]), float(b1[0]))
        ys = (float(p0[1]), float(p1[1]), float(b0[1]), float(b1[1]))
        c0 = max(0, int(np.floor((min(xs) - spec["xmin"]) / RES_M)))
        c1 = min(width, int(np.ceil((max(xs) - spec["xmin"]) / RES_M)))
        r0 = max(0, int(np.floor((spec["ymax"] - max(ys)) / RES_M)))
        r1 = min(height, int(np.ceil((spec["ymax"] - min(ys)) / RES_M)))
        if c1 <= c0 or r1 <= r0:
            continue
        sub = gap[r0:r1, c0:c1]
        if not np.any(sub):
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
        keep = sub.ravel() & _in_quad(px, py, corners)
        if not np.any(keep):
            continue
        px = px[keep]
        py = py[keep]
        flat_r = flat_r[keep]
        flat_c = flat_c[keep]
        dx = float(p1[0] - p0[0])
        dy = float(p1[1] - p0[1])
        t = np.clip(((px - p0[0]) * dx + (py - p0[1]) * dy) / (span * span), 0.0, 1.0)
        nx = (1.0 - t) * n0[0] + t * n1[0]
        ny = (1.0 - t) * n0[1] + t * n1[1]
        norm = np.maximum(np.hypot(nx, ny), 1e-6)
        foot_x = p0[0] + t * dx
        foot_y = p0[1] + t * dy
        offset = (px - foot_x) * (nx / norm) + (py - foot_y) * (ny / norm)
        outer_t = (1.0 - t) * (offs[outer_k[i]] if outer_k[i] >= 0 else 0.0) + t * (
            offs[outer_k[i + 1]] if outer_k[i + 1] >= 0 else 0.0
        )
        use = (offset >= -0.05) & (offset <= outer_t + RES_M * 0.5)
        if not np.any(use):
            continue
        height_v = _profile_at(z_prof, offset[use], t[use], i)
        take = np.isfinite(height_v)
        if not np.any(take):
            continue
        rr_i = flat_r[use][take]
        cc_i = flat_c[use][take]
        dist = offset[use][take]
        closer = dist < d_out[rr_i, cc_i]
        if not np.any(closer):
            continue
        z_out[rr_i[closer], cc_i[closer]] = height_v[take][closer].astype(np.float32)
        d_out[rr_i[closer], cc_i[closer]] = dist[closer].astype(np.float32)
        written[rr_i[closer], cc_i[closer]] = True
        filled += int(closer.sum())
    return filled


def _line_of(lines, oid: int) -> LineString | None:
    hit = lines[lines.objectid == int(oid)]
    if hit.empty:
        return None
    geom = hit.iloc[0].geometry
    if geom.geom_type == "MultiLineString":
        geom = max(geom.geoms, key=lambda g: g.length)
    if geom.geom_type != "LineString" or geom.length < STATION_M:
        return None
    return geom


def _apply_line(xy, station, normals, host, z, rough, spec, zones, slot, capture):
    offs = np.arange(0.0, MAX_HALF_M + 1e-9, RAY_STEP_M)
    pts = shapely.points(xy[:, 0], xy[:, 1])
    on_line = np.asarray(shapely.intersects(host, pts), dtype=bool)
    if int(on_line.sum()) < 2:
        on_line = np.asarray(shapely.intersects(host.buffer(1.0), pts), dtype=bool)
    blocked = _structure_hits(xy, zones)
    usable = on_line & ~blocked
    left_dir = normals
    right_dir = -normals
    zz_l, rr_l, in_l = _sample(xy, left_dir, z, rough, host, spec, offs)
    zz_r, rr_r, in_r = _sample(xy, right_dir, z, rough, host, spec, offs)
    zc_raw = zz_l[0].copy()
    zc_rough = rr_l[0].copy()
    known_l = np.zeros(len(xy), dtype=bool)
    known_r = np.zeros(len(xy), dtype=bool)
    z_left = np.full_like(zz_l, np.nan)
    z_right = np.full_like(zz_r, np.nan)
    prefix_l = np.zeros_like(in_l)
    prefix_r = np.zeros_like(in_r)
    zc_filled = np.full(len(xy), np.nan, dtype=np.float64)
    for i0, i1 in _runs(usable):
        sl = slice(i0, i1)
        st = station[sl]
        center_clean = in_l[0, sl] & np.isfinite(zc_raw[sl]) & np.isfinite(zc_rough[sl]) & (zc_rough[sl] < DROP_ROUGH_M)
        zc = _blend_1d(st, center_clean, zc_raw[sl], MAX_GAP_M)
        zc_filled[sl] = zc
        for zz, rr, inside, store, known_store, prefix_store in (
            (zz_l[:, sl], rr_l[:, sl], in_l[:, sl], z_left[:, sl], known_l[sl], prefix_l[:, sl]),
            (zz_r[:, sl], rr_r[:, sl], in_r[:, sl], z_right[:, sl], known_r[sl], prefix_r[:, sl]),
        ):
            prefix, donor, known = _known_half(inside, zz, rr, zc)
            prefix_store[:] = prefix
            known_store[:] = known
            dz = np.where(donor, zz - zc, np.nan)
            blended = _blend_rows(st, donor, dz, MAX_GAP_M)
            store[:] = np.where(np.isfinite(blended) & np.isfinite(zc), blended + zc, np.nan)
    _paint_half(xy, left_dir, offs, prefix_l, z_left, slot[3], usable, spec, slot[:3])
    _paint_half(xy, right_dir, offs, prefix_r, z_right, slot[3], usable, spec, slot[:3])
    if capture is not None:
        capture.update(
            {
                "xy": xy,
                "station": station,
                "offs": offs,
                "raw_l": zz_l,
                "raw_r": zz_r,
                "z_l": z_left,
                "z_r": z_right,
                "known_l": known_l,
                "known_r": known_r,
                "rough_l": rr_l,
                "rough_r": rr_r,
            }
        )
    return int(known_l.sum()), int(known_r.sum())


def _plot_oid(capture: dict, out_dir: Path) -> dict:
    station = capture["station"]
    xy = capture["xy"]
    fx, fy = PLOT_FOOT
    i_foot = int(np.argmin((xy[:, 0] - fx) ** 2 + (xy[:, 1] - fy) ** 2))
    s_foot = float(station[i_foot])
    window = np.abs(station - s_foot) <= PLOT_SPAN_M
    offs = capture["offs"]
    rough = capture["rough_l"]
    raw = capture["raw_l"]
    prof = capture["z_l"]
    # Outermost left offset that is rough at the foot itself.
    k_pick = 1
    for k, _dist in enumerate(offs):
        if k == 0:
            continue
        if np.isfinite(rough[k, i_foot]) and rough[k, i_foot] >= DROP_ROUGH_M:
            k_pick = k
    i_gap = i_foot

    fig, ax = plt.subplots(figsize=(8.0, 3.6))
    sel = np.flatnonzero(window)
    ax.plot(station[sel], raw[k_pick, sel], color="#8a8f98", linewidth=1.0, label="Rohgelände")
    ax.plot(station[sel], prof[k_pick, sel], color="#1f4e79", linewidth=1.4, label="gemischtes Profil")
    donor_k = np.isfinite(rough[k_pick]) & (rough[k_pick] < DROP_ROUGH_M)
    ax.scatter(
        station[sel][donor_k[sel]],
        raw[k_pick, sel][donor_k[sel]],
        s=8,
        color="#2e7d32",
        label="saubere Stützstelle",
        zorder=3,
    )
    ax.axvline(station[i_gap], color="#9a3412", linewidth=0.8)
    ax.set_xlabel("Station m")
    ax.set_ylabel("Höhe m")
    ax.set_title(f"OID {PLOT_OID}, links, {offs[k_pick]:.2f} m von der Centerline")
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    along_path = out_dir / "oid6975_laengs.png"
    fig.savefig(along_path, dpi=120)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.4, 4.0))
    signed_l = offs
    signed_r = -offs
    ax.plot(signed_r, capture["raw_r"][:, i_gap], color="#8a8f98", linewidth=1.0, label="Rohgelände")
    ax.plot(signed_l, raw[:, i_gap], color="#8a8f98", linewidth=1.0)
    ax.plot(signed_r, capture["z_r"][:, i_gap], color="#1f4e79", linewidth=1.4, label="gemischtes Profil")
    ax.plot(signed_l, prof[:, i_gap], color="#1f4e79", linewidth=1.4)
    ax.axvline(0.0, color="#c5c9d0", linewidth=0.6)
    ax.set_xlabel("Abstand von der Centerline m, links positiv")
    ax.set_ylabel("Höhe m")
    ax.set_title(f"OID {PLOT_OID}, Station {station[i_gap]:.1f} m")
    ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    cross_path = out_dir / "oid6975_querprofil.png"
    fig.savefig(cross_path, dpi=120)
    plt.close(fig)
    return {
        "foot_xy": [fx, fy],
        "foot_station_m": round(s_foot, 2),
        "gap_station_m": round(float(station[i_gap]), 2),
        "left_offset_m": round(float(offs[k_pick]), 2),
        "along": str(along_path),
        "cross": str(cross_path),
        "known_left_in_window": int(donor_k[window].sum()),
    }


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    raw_path = proc / "corridor50_raw" / "corridor50_raw.tif"
    gpkg_in = proc / "centerline_shift_taper" / "centerline_shift.gpkg"
    for path in (raw_path, gpkg_in):
        if not path.is_file():
            raise SystemExit(f"missing {path}")
    out_dir = proc / "dgm_repair_transect"
    out_dir.mkdir(parents=True, exist_ok=True)

    raw, spec = _load_geotiff(raw_path)
    roads = gpd.read_file(gpkg_in, layer="carriageway")
    lines = gpd.read_file(gpkg_in, layer="centerline")
    try:
        segments = gpd.read_file(gpkg_in, layer="segments")
    except Exception:
        segments = None
    zones = _zones(segments)
    print(f"structure spans {len(zones)}", flush=True)
    print("preparing edges", flush=True)
    parts = _prepare(roads, lines)
    road = _road_mask(parts, raw.shape, spec)
    # Road-only roughness has to see the carriageway, not the whole raster.
    rough = _road_only(raw, road)
    struct = _structure_mask(zones, raw.shape, spec) if zones else np.zeros(raw.shape, dtype=bool)
    gap = road & ~struct & np.isfinite(rough) & (rough >= DROP_ROUGH_M)
    on = road & ~struct & np.isfinite(rough)
    before = _stats(rough, on)
    before_full = _stats(_deviation(raw), on)
    print(f"  {len(parts)} parts, hot {before['hot_px']}", flush=True)

    z_out = np.full(raw.shape, np.nan, dtype=np.float32)
    d_out = np.full(raw.shape, np.inf, dtype=np.float32)
    written = np.zeros(raw.shape, dtype=bool)
    known_left = known_right = 0
    capture = None
    capture_dist = 1e300
    lines_by_oid: dict[int, LineString | None] = {}
    for part in parts:
        oid = part["oid"]
        if oid not in lines_by_oid:
            lines_by_oid[oid] = _line_of(lines, oid)
        line = lines_by_oid[oid]
        if line is None:
            continue
        host = part["poly"] if part["poly"].is_valid else part["poly"].buffer(0)
        part_known_l = part_known_r = 0
        for piece in _pieces(line, host):
            xy, station, normals = _densify([(float(x), float(y)) for x, y in piece.coords], STATION_M)
            if len(xy) < 2 or np.allclose(normals, 0):
                continue
            hold = {} if oid == PLOT_OID else None
            n_l, n_r = _apply_line(
                xy,
                station,
                normals,
                host,
                raw,
                rough,
                spec,
                zones,
                (z_out, d_out, written, gap),
                hold,
            )
            part_known_l += n_l
            part_known_r += n_r
            if hold is not None:
                fx, fy = PLOT_FOOT
                dist = float(np.min((hold["xy"][:, 0] - fx) ** 2 + (hold["xy"][:, 1] - fy) ** 2))
                if dist < capture_dist:
                    capture = hold
                    capture_dist = dist
        known_left += part_known_l
        known_right += part_known_r
        print(f"  oid {oid} known L/R {part_known_l}/{part_known_r}", flush=True)

    working = raw.copy()
    take = written & gap & np.isfinite(z_out)
    working[take] = z_out[take]
    after_rough = _road_only(working, road)
    after = _stats(after_rough, on)
    after_full = _stats(_deviation(working), on)
    klass = np.zeros(raw.shape, dtype=np.uint8)
    klass[on & (rough < DROP_ROUGH_M)] = 1
    klass[on & (rough >= DROP_ROUGH_M) & ~take] = 2
    klass[take] = 3
    repaired = np.where(road, working, np.nan).astype(np.float32)
    shown = np.where(on, after_rough, np.nan).astype(np.float32)
    write_referenced_tif(out_dir / "02_repaired.tif", repaired, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    write_referenced_tif(out_dir / "01_class.tif", klass, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    write_referenced_tif(out_dir / "03_roughness.tif", shown, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    plot_info = _plot_oid(capture, out_dir) if capture is not None else None
    delta = np.abs(working[take].astype(np.float64) - raw[take].astype(np.float64))
    report = {
        "heights": str(raw_path),
        "carriageway": str(gpkg_in),
        "drop_rough_m": DROP_ROUGH_M,
        "station_m": STATION_M,
        "ray_step_m": RAY_STEP_M,
        "max_gap_m": MAX_GAP_M,
        "before_road_only": before,
        "after_road_only": after,
        "before_full_window": before_full,
        "after_full_window": after_full,
        "filled_px": int(take.sum()),
        "left_raw_px": int((klass == 2).sum()),
        "known_stations_left": known_left,
        "known_stations_right": known_right,
        "max_delta_m": round(float(delta.max()) if len(delta) else 0.0, 4),
        "delta_p50_m": round(float(np.percentile(delta, 50)) if len(delta) else 0.0, 4),
        "delta_p90_m": round(float(np.percentile(delta, 90)) if len(delta) else 0.0, 4),
        "oid6975": plot_info,
        "class": {
            "1": "smooth on the raw DGM, height unchanged",
            "2": "rough pixel left raw, no known profile of this half measured that offset",
            "3": "filled from the half-profile blended along the centerline",
        },
        "note": (
            "Supports are clean samples of one half, under 1.5 cm in the "
            "carriageway-only 1.5 m window. A rough edge sample is not a support. "
            "The shape is the height relative to the centerline, blended along "
            "the arc between supports of the same offset and the same half, at "
            "most 50 m. Only rough pixels take that height. No cross-slope is "
            "fitted at the gap. An offset that no nearby support measured stays raw."
        ),
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "before": before,
                "after": after,
                "filled_px": int(take.sum()),
                "left_raw_px": int((klass == 2).sum()),
                "max_delta_m": report["max_delta_m"],
            },
            indent=2,
        )
    )
    print(f"Wrote {out_dir}")


if __name__ == "__main__":
    main()
