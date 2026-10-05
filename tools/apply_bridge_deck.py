"""Rejected bridge-deck experiment. Not the road-mesh method.

The road mesh is the transect in tools/repair_dgm_transect.py plus
build_road_grid.py on 02_repaired.tif and the carriageway clip.
This script uses another axis, the surface model, and another support rule,
and writes an extra polygon. See docs/ROAD_MESH.md. Do not extend it.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\apply_bridge_deck.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import tifffile as tiff
from shapely.geometry import Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from filter_road_corridor import write_referenced_tif  # noqa: E402
from gip_bridge_flags import load_flags  # noqa: E402
from probe_bridge_dom import (  # noqa: E402
    RES_M,
    _bilinear,
    _densify,
    _extend,
    _load_dgm,
    _normals,
    _snap_bbox,
)
from repair_dgm_transect import (  # noqa: E402
    MAX_GAP_M,
    RAY_STEP_M,
    _blend_1d,
    _blend_rows,
    _known_half,
    _paint_half,
)
from site_coords import SiteCoords, load_site, processed_dir, site_slug  # noqa: E402
from wcs_terrain import fetch_terrain_coverage  # noqa: E402

EXTEND_M = 80.0
INNER_M = 4.0
CLEAR_M = 0.4
MIN_RUN_M = 8.0
UNDER_M = 2.5
CLUSTER_M = 0.35
HALF_M = 6.0
# Paint far enough onto the approach that the deck polygon overlaps the road mesh.
OVERLAP_ROAD_M = 12.0
OVERLAP_WALK_M = 40.0


def _bridge_xy(road: dict, sc: SiteCoords) -> np.ndarray:
    xy = [sc.beamng_to_crs(float(n[0]), float(n[1])) for n in road["nodes"]]
    return np.asarray(xy, dtype=np.float64)


def _median1d(values: np.ndarray, win: int = 5) -> np.ndarray:
    out = values.copy()
    half = win // 2
    for i in range(len(values)):
        if not np.isfinite(values[i]):
            continue
        sl = values[max(0, i - half) : i + half + 1]
        sl = sl[np.isfinite(sl)]
        if sl.size:
            out[i] = float(np.median(sl))
    return out


def _runs(open_mask: np.ndarray, station: np.ndarray) -> list[tuple[int, int]]:
    found = []
    n = len(open_mask)
    i = 0
    while i < n:
        if not open_mask[i]:
            i += 1
            continue
        j = i + 1
        while j < n and open_mask[j]:
            j += 1
        if station[j - 1] - station[i] >= MIN_RUN_M:
            found.append((i, j))
        i = j
    return found


def _dom_for(site: dict, oid: int, xmin, ymin, xmax, ymax) -> tuple[np.ndarray, dict]:
    path = ROOT / "data" / "raw" / f"dom_{site_slug(site)}_oid{oid}.tif"
    meta_path = path.with_suffix(".meta.json")
    have = False
    if path.is_file() and meta_path.is_file():
        box = json.loads(meta_path.read_text(encoding="utf-8")).get("bbox") or []
        if len(box) == 4 and box[0] <= xmin + 1 and box[1] <= ymin + 1 and box[2] >= xmax - 1 and box[3] >= ymax - 1:
            have = True
            xmin, ymin, xmax, ymax = (float(v) for v in box)
    if not have:
        if path.is_file():
            path.unlink()
        tiles = path.with_name(path.stem + "_tiles")
        if tiles.is_dir():
            for child in tiles.glob("*.tif"):
                child.unlink()
        fetch_terrain_coverage(
            site,
            "dom",
            label=f"DOM {oid}",
            bbox=(xmin, ymin, xmax, ymax),
            resolution_m=RES_M,
            out_path=path,
            allow_incomplete=True,
            force=True,
        )
    dom = np.asarray(tiff.imread(path), dtype=np.float32)
    if dom.ndim == 3:
        dom = dom[..., 0]
    dom = dom.copy()
    dom[(~np.isfinite(dom)) | (dom < 50.0) | (dom > 4500.0)] = np.nan
    frame = {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax, "res": RES_M}
    return dom, frame


def _deck_series(dom, dom_frame, dgm, dgm_frame, line, normal) -> tuple[np.ndarray, np.ndarray]:
    offsets = np.arange(-INNER_M, INNER_M + 0.25, 0.5)
    deck = np.full(len(line), np.nan, dtype=np.float64)
    ground = _bilinear(dgm, dgm_frame, line[:, 0], line[:, 1])
    for i in range(len(line)):
        pts = line[i] + offsets[:, None] * normal[i]
        z = _bilinear(dom, dom_frame, pts[:, 0], pts[:, 1])
        finite = z[np.isfinite(z)]
        if finite.size < 3:
            continue
        # A vehicle can cover more than half the width. The free lane is the
        # low cluster; samples well above that cluster are not the deck.
        floor = float(np.min(finite))
        kept = finite[finite <= floor + 0.35]
        deck[i] = float(np.median(kept)) if kept.size else floor
    return _median1d(deck), ground


def _sample_sides(arr, frame, line, normal, offs):
    n = len(line)
    left = np.full((len(offs), n), np.nan, dtype=np.float64)
    right = np.full((len(offs), n), np.nan, dtype=np.float64)
    for k, off in enumerate(offs):
        dist = float(off)
        left_pts = line + dist * normal
        right_pts = line - dist * normal
        left[k] = _bilinear(arr, frame, left_pts[:, 0], left_pts[:, 1])
        right[k] = _bilinear(arr, frame, right_pts[:, 0], right_pts[:, 1])
    return left, right


def _deck_supports(dom_z, road_z, opening, floor, blocked):
    """Approach: the repaired road. Span: the corridor, with only the deck as a support.

    A wall or a vehicle stays inside the corridor, the way a rough pixel stays
    inside the carriageway, and is not a support. The gorge is outside.
    """
    inside = np.zeros(dom_z.shape, dtype=bool)
    zz = np.full(dom_z.shape, np.nan, dtype=np.float64)
    rr = np.full(dom_z.shape, np.nan, dtype=np.float64)
    approach = (~opening)[None, :] & np.isfinite(road_z)
    inside[approach] = True
    zz[approach] = road_z[approach]
    rr[approach] = 0.0
    present = (opening & ~blocked & np.isfinite(floor))[None, :] & np.isfinite(dom_z)
    present &= dom_z >= (floor - CLUSTER_M)
    inside[present] = True
    zz[present] = dom_z[present]
    deck = present & (dom_z <= (floor + CLUSTER_M))
    rr[deck] = 0.0
    rr[present & ~deck] = 1.0
    return zz, rr, inside


def _reach_of(prefix, offs) -> np.ndarray:
    """Outermost offset that is still finite all the way out from the centre."""
    reach = np.zeros(prefix.shape[1], dtype=np.float64)
    running = np.ones(prefix.shape[1], dtype=bool)
    for k, off in enumerate(offs):
        if k == 0:
            continue
        running &= prefix[k]
        reach = np.where(running, float(off), reach)
    return reach


def _steady_reach(reach: np.ndarray) -> np.ndarray:
    """Narrowest reach in a 10 m window, so one station cannot step the edge out."""
    win = int(round(10.0 / RES_M))
    if win % 2 == 0:
        win += 1
    half = win // 2
    out = np.zeros_like(reach)
    for i in range(len(reach)):
        if reach[i] <= 0.0:
            continue
        sl = reach[max(0, i - half) : i + half + 1]
        sl = sl[sl > 0.0]
        if sl.size:
            out[i] = float(sl.min())
    return out


def _trim_half(prefix, offs, store):
    """Keep the width of the blended profile. A single station cannot step it out."""
    finite = np.isfinite(store)
    finite = finite.copy()
    if len(finite):
        finite[0] = False
    reach = _steady_reach(_reach_of(finite, offs))
    keep = offs[:, None] <= (reach[None, :] + 1e-6)
    store[:] = np.where(keep, store, np.nan)
    trimmed = prefix & keep
    trimmed = trimmed.copy()
    if len(trimmed):
        trimmed[0] = False
    return trimmed


def _prepare_line(dom, dom_frame, road, road_frame, line, normal, opening) -> dict:
    """One cross-profile along the bridge axis, including the approaches."""
    offs = np.arange(0.0, HALF_M + 1e-9, RAY_STEP_M)
    dom_l, dom_r = _sample_sides(dom, dom_frame, line, normal, offs)
    road_l, road_r = _sample_sides(road, road_frame, line, normal, offs)
    n = len(line)
    inner = offs <= INNER_M
    floor = np.full(n, np.nan, dtype=np.float64)
    floor_support = np.zeros(n, dtype=bool)
    for i in range(n):
        if not opening[i]:
            continue
        col = np.concatenate([dom_l[inner, i], dom_r[inner, i]])
        finite = col[np.isfinite(col)]
        if finite.size < 3:
            continue
        floor[i] = float(np.min(finite))
        floor_support[i] = int(np.sum(finite <= floor[i] + CLUSTER_M)) >= 3
    station = np.arange(n, dtype=np.float64) * RES_M
    floor_filled = _blend_1d(station, floor_support, floor, MAX_GAP_M)
    blocked = np.isfinite(floor) & np.isfinite(floor_filled) & (floor > floor_filled + CLUSTER_M)
    zz_l, rr_l, in_l = _deck_supports(dom_l, road_l, opening, floor, blocked)
    zz_r, rr_r, in_r = _deck_supports(dom_r, road_r, opening, floor, blocked)
    zc = _blend_1d(station, in_l[0] & np.isfinite(zz_l[0]), zz_l[0], MAX_GAP_M)
    zc = np.where(np.isfinite(zc), zc, floor_filled)
    # The center sample only anchors the height. It must not block the ray outward.
    in_l = in_l.copy()
    in_r = in_r.copy()
    in_l[0] = np.isfinite(zc)
    in_r[0] = np.isfinite(zc)
    z_left = np.full_like(zz_l, np.nan)
    z_right = np.full_like(zz_r, np.nan)
    prefix_l = np.zeros_like(in_l)
    prefix_r = np.zeros_like(in_r)
    for zz, rr, inside, store, prefix_store in (
        (zz_l, rr_l, in_l, z_left, prefix_l),
        (zz_r, rr_r, in_r, z_right, prefix_r),
    ):
        prefix, donor, _known = _known_half(inside, zz, rr, zc)
        dz = np.where(donor, zz - zc, np.nan)
        blended = _blend_rows(station, donor, dz, MAX_GAP_M)
        store[:] = np.where(np.isfinite(blended) & np.isfinite(zc), blended + zc, np.nan)
        prefix_store[:] = _trim_half(prefix, offs, store)
    for store, prefix_store in ((z_left, prefix_l), (z_right, prefix_r)):
        store[0] = np.where(np.isfinite(zc), zc, store[0])
        prefix_store[0] = np.isfinite(store[0])
    return {
        "offs": offs,
        "z_left": z_left,
        "z_right": z_right,
        "prefix_l": prefix_l,
        "prefix_r": prefix_r,
        "road_center": road_l[0],
    }


def _half_bits(xy, direction, offs, prefix) -> list:
    bits = []
    outer = np.where(prefix, np.arange(len(offs))[:, None], -1).max(axis=0)
    for i in range(len(xy) - 1):
        if outer[i] < 1 or outer[i + 1] < 1:
            continue
        r0 = float(offs[outer[i]])
        r1 = float(offs[outer[i + 1]])
        if min(r0, r1) < 1.5:
            continue
        p0 = xy[i]
        p1 = xy[i + 1]
        b0 = p0 + direction[i] * r0
        b1 = p1 + direction[i + 1] * r1
        poly = Polygon(
            [
                (float(p0[0]), float(p0[1])),
                (float(p1[0]), float(p1[1])),
                (float(b1[0]), float(b1[1])),
                (float(b0[0]), float(b0[1])),
            ]
        )
        if not poly.is_valid:
            poly = poly.buffer(0)
        if not poly.is_empty:
            bits.append(poly)
    return bits


def _span_bounds(i0: int, i1: int, opening, road_center) -> tuple[int, int]:
    """Cover the opening and enough of the approach that the polygons overlap."""
    n = len(opening)
    walk = int(round(OVERLAP_WALK_M / RES_M))

    def _out(start: int, step: int) -> int:
        i = start
        seen = 0.0
        for _k in range(walk):
            nxt = i + step
            if nxt < 0 or nxt >= n or opening[nxt]:
                break
            i = nxt
            if np.isfinite(road_center[i]):
                seen += RES_M
            if seen >= OVERLAP_ROAD_M:
                break
        return i

    return _out(i0, -1), _out(i1 - 1, 1) + 1


def _window(frame, shape, poly) -> tuple[int, int, int, int]:
    height, width = shape
    minx, miny, maxx, maxy = poly.bounds
    c0 = max(0, int(math.floor((minx - frame["xmin"]) / RES_M)) - 1)
    c1 = min(width, int(math.ceil((maxx - frame["xmin"]) / RES_M)) + 1)
    r0 = max(0, int(math.floor((frame["ymax"] - maxy) / RES_M)) - 1)
    r1 = min(height, int(math.ceil((frame["ymax"] - miny) / RES_M)) + 1)
    return r0, r1, c0, c1


def _raster_gap(shape, frame, poly) -> np.ndarray:
    from shapely import contains_xy

    gap = np.zeros(shape, dtype=bool)
    r0, r1, c0, c1 = _window(frame, shape, poly)
    if c1 <= c0 or r1 <= r0:
        return gap
    cc, rr = np.meshgrid(np.arange(c0, c1), np.arange(r0, r1))
    xs = frame["xmin"] + (cc.ravel().astype(np.float64) + 0.5) * RES_M
    ys = frame["ymax"] - (rr.ravel().astype(np.float64) + 0.5) * RES_M
    inside = contains_xy(poly, xs, ys)
    if np.any(inside):
        gap[rr.ravel()[inside], cc.ravel()[inside]] = True
    return gap


def _restore_under(raster, before, frame, polys, poly_tree, deck_poly, r0, r1, c0, c1) -> int:
    """A carriageway far below the deck is the road under the span. Leave it."""
    from shapely import intersects_xy
    from shapely.geometry import box as shp_box

    sub = raster[r0:r1, c0:c1]
    old = before
    raised = np.isfinite(old) & np.isfinite(sub) & ((sub.astype(np.float64) - old) > UNDER_M)
    if not np.any(raised) or not polys:
        return 0
    rr, cc = np.nonzero(raised)
    xs = frame["xmin"] + (cc.astype(np.float64) + c0 + 0.5) * RES_M
    ys = frame["ymax"] - (rr.astype(np.float64) + r0 + 0.5) * RES_M
    foreign = np.zeros(len(rr), dtype=bool)
    minx, miny, maxx, maxy = deck_poly.bounds
    tile = shp_box(float(minx), float(miny), float(maxx), float(maxy))
    for hit in poly_tree.query(tile):
        foreign |= intersects_xy(polys[int(hit)], xs, ys)
    if not np.any(foreign):
        return 0
    sub[rr[foreign], cc[foreign]] = old[rr[foreign], cc[foreign]]
    return int(np.count_nonzero(foreign))


def _paint_span(raster, frame, polys, poly_tree, line, normal, opening, prepared, i0, i1, scratch):
    a, b = _span_bounds(i0, i1, opening, prepared["road_center"])
    if b - a < 2:
        return None, 0, 0, None
    xy = line[a:b]
    direc = normal[a:b]
    offs = prepared["offs"]
    prefix_l = prepared["prefix_l"][:, a:b]
    prefix_r = prepared["prefix_r"][:, a:b]
    bits = _half_bits(xy, direc, offs, prefix_l) + _half_bits(xy, -direc, offs, prefix_r)
    if not bits:
        return None, 0, 0, None
    deck_poly = unary_union(bits).buffer(0)
    if deck_poly.is_empty:
        return None, 0, 0, None
    gap = _raster_gap(raster.shape, frame, deck_poly)
    r0, r1, c0, c1 = _window(frame, raster.shape, deck_poly)
    before = raster[r0:r1, c0:c1].copy()
    d_out, written = scratch
    usable = np.ones(b - a, dtype=bool)
    slot = (raster, d_out, written)
    _paint_half(xy, direc, offs, prefix_l, prepared["z_left"][:, a:b], gap, usable, frame, slot)
    _paint_half(xy, -direc, offs, prefix_r, prepared["z_right"][:, a:b], gap, usable, frame, slot)
    skipped = _restore_under(raster, before, frame, polys, poly_tree, deck_poly, r0, r1, c0, c1)
    sub = raster[r0:r1, c0:c1]
    changed = np.isfinite(sub) & (~np.isfinite(before) | (np.abs(sub.astype(np.float64) - before) > 1e-3))
    length = float(np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1])).sum())
    half = float(deck_poly.area / max(length, 1.0) / 2.0)
    return deck_poly, int(np.count_nonzero(changed)), skipped, half


def _both_sides_drop(dom, dom_frame, line, normal, deck, i0: int, i1: int) -> bool:
    """A span falls away on both sides. A canopy stays high beside the road."""
    left: list[float] = []
    right: list[float] = []
    for k in range(i0, i1, 4):
        if not np.isfinite(deck[k]):
            continue
        for bucket, sign in ((left, -1.0), (right, 1.0)):
            pt = line[k] + sign * 9.0 * normal[k]
            z = float(_bilinear(dom, dom_frame, np.array([pt[0]]), np.array([pt[1]]))[0])
            if np.isfinite(z):
                bucket.append(float(deck[k] - z))
    if len(left) < 3 or len(right) < 3:
        return False
    return float(np.median(left)) > 2.0 and float(np.median(right)) > 2.0


def main() -> None:
    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    flags = load_flags(site) or {}
    roads = json.loads((proc / "gip_roads_beamng.json").read_text(encoding="utf-8"))
    gpkg = proc / "centerline_shift_taper" / "centerline_shift.gpkg"
    carriage = gpd.read_file(gpkg, layer="carriageway")
    repaired_path = proc / "dgm_repair_transect" / "02_repaired.tif"
    raster, frame = _load_dgm(repaired_path)
    dgm, dgm_frame = _load_dgm(proc / "corridor50_raw" / "corridor50_raw.tif")
    from shapely.strtree import STRtree

    polys = list(carriage.geometry)
    poly_tree = STRtree(polys)
    scratch = (
        np.full(raster.shape, np.inf, dtype=np.float32),
        np.zeros(raster.shape, dtype=bool),
    )

    decks = []
    report = []
    for oid, rec in sorted(flags.items(), key=lambda kv: int(kv[0])):
        if not rec.get("bridge"):
            continue
        road = roads.get(str(oid))
        if road is None:
            continue
        xy0 = _bridge_xy(road, sc)
        if len(xy0) < 2:
            continue
        extended = _extend(xy0, EXTEND_M)
        line, _station = _densify(extended, RES_M)
        normal = _normals(line)
        pad = 16.0
        xmin, ymin, xmax, ymax = _snap_bbox(
            float(line[:, 0].min()) - pad,
            float(line[:, 1].min()) - pad,
            float(line[:, 0].max()) + pad,
            float(line[:, 1].max()) + pad,
            sc.xmin,
            sc.ymin,
            RES_M,
        )
        dom, dom_frame = _dom_for(site, int(oid), xmin, ymin, xmax, ymax)
        deck, ground = _deck_series(dom, dom_frame, dgm, dgm_frame, line, normal)
        opening = np.isfinite(deck) & np.isfinite(ground) & ((deck - ground) > CLEAR_M)
        spans = _runs(opening, np.arange(len(line)) * RES_M)
        if not spans:
            report.append({"oid": int(oid), "name": road.get("kunstbauten"), "painted": 0, "reason": "no opening"})
            print(f"OID {oid} no opening", flush=True)
            continue
        kept = []
        for i0, i1 in spans:
            if _both_sides_drop(dom, dom_frame, line, normal, deck, i0, i1):
                kept.append((i0, i1))
        if not kept:
            report.append({"oid": int(oid), "name": road.get("kunstbauten"), "painted": 0, "reason": "sides do not drop"})
            print(f"OID {oid} sides do not drop", flush=True)
            continue
        kept_open = np.zeros(len(line), dtype=bool)
        for i0, i1 in kept:
            kept_open[i0:i1] = True
        prepared = _prepare_line(dom, dom_frame, raster, frame, line, normal, kept_open)
        painted = skipped = 0
        halves = []
        for i0, i1 in kept:
            deck_poly, n_write, n_skip, half = _paint_span(
                raster, frame, polys, poly_tree, line, normal, kept_open, prepared, i0, i1, scratch
            )
            if deck_poly is None or deck_poly.is_empty:
                continue
            halves.append(round(float(half), 2))
            painted += n_write
            skipped += n_skip
            decks.append(deck_poly)
        lift = deck[opening] - ground[opening]
        report.append(
            {
                "oid": int(oid),
                "name": road.get("kunstbauten"),
                "str_code": rec.get("str_code"),
                "runs_m": [round(float((b - a) * RES_M), 1) for a, b in kept],
                "half_width_m": [round(h, 2) for h in halves],
                "clearance_p50_m": round(float(np.median(lift)), 2) if lift.size else None,
                "painted": painted,
                "under_road_kept": skipped,
            }
        )
        print(
            f"OID {oid} runs {report[-1]['runs_m']} half {report[-1]['half_width_m']} "
            f"painted {painted} under {skipped}",
            flush=True,
        )

    out_dir = proc / "dom_bridge_deck"
    out_dir.mkdir(parents=True, exist_ok=True)
    deck_path = out_dir / "deck.tif"
    write_referenced_tif(deck_path, raster, xmin=frame["xmin"], ymax=frame["ymax"], res=RES_M)
    if decks:
        extra = gpd.GeoDataFrame({"objectid": [-1] * len(decks), "geometry": decks}, crs=carriage.crs)
        merged = gpd.GeoDataFrame(pd.concat([carriage, extra], ignore_index=True), crs=carriage.crs)
    else:
        merged = carriage
    clip_path = out_dir / "clip.gpkg"
    if clip_path.is_file():
        clip_path.unlink()
    merged.to_file(clip_path, layer="carriageway")
    body = {"decks": report, "heights": str(deck_path), "clip": str(clip_path)}
    (out_dir / "report.json").write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {deck_path}", flush=True)
    print(f"Wrote {clip_path}", flush=True)


if __name__ == "__main__":
    main()
