"""Trial: slide short centerline pieces sideways onto the flatter DGM strip.

Does not write into a BeamNG level. Each step is a layer in one GeoPackage
plus a roughness GeoTIFF, so the choices can be inspected before anything
is applied.

Pieces are at most 20 m of one GIP OBJECTID: Landes- and Bundesstraßen,
Autobahnen, and Gemeindestraße S-G. S-GW is not included. Each piece is
shifted on its own, from the original axis outward by 10 cm up to 1 m left
and right. The score is the mean plane-residual of the raw 0.5 m DGM inside
a flat-ended buffer of the width under test. A piece whose scores barely
move stays put. A piece with one clear good offset keeps it. A piece with
several good offsets takes the one nearest the mean of its already fixed
neighbours. A piece that stays above the threshold at every offset is
narrowed by 10 cm and searched again, down to 2 m less than the measured
width. The stored centerline then creeps the kept offset along the original
vertices and is buffered at the width that passed.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\shift_centerline_by_roughness.py
"""
from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.ndimage import uniform_filter
from shapely import intersects_xy
from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_bridges import find_gip_geojson  # noqa: E402
from build_road_grid import RES_M, _ensure_raw_mosaic, _load_geotiff, _road_mesh_row  # noqa: E402
from export_gip_width_m import _project, _to_31254  # noqa: E402
from filter_road_corridor import write_referenced_tif  # noqa: E402
from gip_catalog import catalog_segment  # noqa: E402
from gip_road_segments import _gip_segment_width_m  # noqa: E402
from site_coords import load_site, processed_dir, site_slug  # noqa: E402

SEG_MAX_M = 20.0
STEP_M = 0.10
MAX_SHIFT_M = 1.0
ROUGH_WIN = 3
HOT_M = 0.019
GOOD_MEAN_M = 0.008
CLEAR_GAP_M = 0.002
ROUGH_CLIP_M = 0.019
# Share of pixels on the roughness cap a kept placement may contain.
# 1.0 = mean-only rule (kept 27.09, "taper" run); 0.0 = strict trial, rejected.
MAX_FRAC_HOT = 1.0
JOIN_M = 3.0
JOIN_DOT = 0.85
WIDTH_STEP_M = 0.10
MAX_NARROW_M = 2.0
TAPER_M = 20.0
# repair_dgm_transect, blend_bridge_deck and apply_corridor_dgm read this folder.
OUT_DIR_NAME = "centerline_shift_taper"


@dataclass
class Segment:
    seg_id: str
    oid: int
    seq: int
    coords: list[tuple[float, float]]
    s0: float
    s1: float
    width_m: float
    nx: float
    ny: float
    structure: bool
    prev: Segment | None = None
    next: Segment | None = None
    scores: dict[float, dict] = field(default_factory=dict)
    trials: list[dict] = field(default_factory=list)
    good: list[float] = field(default_factory=list)
    best: float = 0.0
    chosen: float = 0.0
    span: float = 0.0
    narrow_m: float = 0.0
    width_used_m: float = 0.0
    passed: bool = False
    kind: str = ""
    target: float | None = None

    @property
    def s_mid(self) -> float:
        return 0.5 * (self.s0 + self.s1)

    @property
    def direction(self) -> tuple[float, float]:
        return (-self.ny, self.nx)


def _offsets() -> list[float]:
    n = int(round(MAX_SHIFT_M / STEP_M))
    out = [0.0]
    for i in range(1, n + 1):
        step = round(i * STEP_M, 2)
        out.append(step)
        out.append(-step)
    return out


def _length(coords: list[tuple[float, float]]) -> float:
    total = 0.0
    for a, b in zip(coords, coords[1:]):
        total += math.hypot(b[0] - a[0], b[1] - a[1])
    return total


def _interp(a, b, t: float) -> tuple[float, float]:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _split(coords: list[tuple[float, float]], max_m: float) -> list[list[tuple[float, float]]]:
    if _length(coords) <= max_m + 1e-6:
        return [coords]
    parts: list[list[tuple[float, float]]] = []
    current = [coords[0]]
    acc = 0.0
    for a, b in zip(coords, coords[1:]):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        if seg < 1e-9:
            continue
        walked = 0.0
        while walked < seg - 1e-6:
            room = max_m - acc
            remain = seg - walked
            if remain <= room + 1e-6:
                current.append(b)
                acc += remain
                walked = seg
                break
            walked += room
            cut = _interp(a, b, walked / seg)
            current.append(cut)
            parts.append(current)
            current = [cut]
            acc = 0.0
    if len(current) >= 2 and _length(current) >= 2.0:
        parts.append(current)
    elif parts and len(current) >= 2:
        parts[-1] = parts[-1] + current[1:]
    return parts or [coords]


def _left_normal(coords: list[tuple[float, float]]) -> tuple[float, float]:
    dx = coords[-1][0] - coords[0][0]
    dy = coords[-1][1] - coords[0][1]
    span = math.hypot(dx, dy)
    if span < 1e-6:
        return 0.0, 0.0
    return -dy / span, dx / span


_STRUCTURE_OBJEKT = {"S-AT", "S-BT", "S-LT", "S-BG", "S-AB", "S-BB"}


def _is_structure(props: dict) -> bool:
    """Bridge, tunnel and gallery stay at offset 0. The DGM is not the deck."""
    from build_bridges import _structure_kind

    kind = _structure_kind(props)
    if kind in {"bridge", "gallery", "tunnel"}:
        return True
    objekt = str(props.get("OBJEKT") or "").upper().strip()
    if objekt in _STRUCTURE_OBJEKT:
        return True
    text = f"{props.get('KUNSTBAUTEN') or ''} {props.get('OBJEKTBEZEICHNUNG') or ''}".lower()
    return "unterflur" in text or "unterflur" in text.replace("ü", "u")


def _roughness(z: np.ndarray) -> np.ndarray:
    """Absolute deviation from the local 1.5 m mean.

    A constant slope lies on that mean, so it is not an error point. A break
    in the surface (edge, rock, step) is.
    """
    valid = np.isfinite(z) & (z > 50.0) & (z < 4500.0)
    filled = np.where(valid, z, np.float32(0.0)).astype(np.float32)
    weight = valid.astype(np.float32)
    # uniform_filter returns the window mean, so `count` is the valid fraction.
    count = uniform_filter(weight, size=ROUGH_WIN, mode="constant")
    mean = uniform_filter(filled, size=ROUGH_WIN, mode="constant")
    enough = count >= (6.0 / (ROUGH_WIN * ROUGH_WIN))
    local = np.divide(mean, count, out=np.full_like(mean, np.nan), where=enough)
    rough = np.abs(np.asarray(z, dtype=np.float32) - local).astype(np.float32)
    rough[~enough | ~valid] = np.nan
    finite = np.isfinite(rough)
    # A rock pixel can be many times rougher than a slope pixel. Cap at the
    # 95th percentile the quieter of the two shift directions already reaches
    # on a typical piece, so one spike cannot outweigh that side.
    rough[finite] = np.minimum(rough[finite], np.float32(ROUGH_CLIP_M))
    return rough


def _zone_stats(poly, rough: np.ndarray, spec: dict) -> dict | None:
    minx, miny, maxx, maxy = poly.bounds
    res = RES_M
    width = int(spec["width"])
    height = int(spec["height"])
    c0 = max(0, int(math.floor((minx - spec["xmin"]) / res)))
    c1 = min(width, int(math.ceil((maxx - spec["xmin"]) / res)))
    r0 = max(0, int(math.floor((spec["ymax"] - maxy) / res)))
    r1 = min(height, int(math.ceil((spec["ymax"] - miny) / res)))
    if c1 <= c0 or r1 <= r0:
        return None
    vals = rough[r0:r1, c0:c1]
    finite = np.isfinite(vals)
    if not np.any(finite):
        return None
    rr, cc = np.nonzero(finite)
    xs = spec["xmin"] + (c0 + cc + 0.5) * res
    ys = spec["ymax"] - (r0 + rr + 0.5) * res
    inside = intersects_xy(poly, xs, ys)
    if not np.any(inside):
        return None
    sample = vals[rr[inside], cc[inside]].astype(np.float64)
    hot_level = float(np.float32(HOT_M))
    return {
        "n_px": int(sample.size),
        "mean_rough_m": float(sample.mean()),
        "p90_rough_m": float(np.percentile(sample, 90)),
        "frac_hot": float(np.mean(sample >= hot_level - 1e-5)),
    }


def _shift(coords, offset: float, nx: float, ny: float) -> list[tuple[float, float]]:
    return [(x + offset * nx, y + offset * ny) for x, y in coords]


def _buffer(coords, radius: float):
    line = LineString(coords)
    if line.length < 0.5 or radius <= 0:
        return None
    poly = line.buffer(radius, cap_style="flat", join_style="mitre")
    if poly.is_empty:
        return None
    return poly


def _load_segments(site: dict) -> list[Segment]:
    path = find_gip_geojson(site)
    data = json.loads(path.read_text(encoding="utf-8"))
    tf = _to_31254(site, data)
    out: list[Segment] = []
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        if not _road_mesh_row(props):
            continue
        geom = _project(feat.get("geometry") or {}, tf)
        if isinstance(geom, MultiLineString):
            geom = max(geom.geoms, key=lambda part: part.length)
        if geom is None or geom.geom_type != "LineString":
            continue
        try:
            oid = int(props.get("OBJECTID"))
        except (TypeError, ValueError):
            continue
        rec = catalog_segment(oid) or {}
        width = rec.get("width_mean_m")
        if width is None or float(width) < 1.5:
            if str(props.get("OBJEKT") or "").upper().strip() != "S-G":
                continue
            width = _gip_segment_width_m(props, 5.0)
        if width is None or float(width) < 1.5:
            continue
        coords = [(float(x), float(y)) for x, y in geom.coords]
        pieces = _split(coords, SEG_MAX_M)
        cursor = 0.0
        for seq, piece in enumerate(pieces):
            span = _length(piece)
            nx, ny = _left_normal(piece)
            out.append(
                Segment(
                    seg_id=f"{oid}_{seq:03d}",
                    oid=oid,
                    seq=seq,
                    coords=piece,
                    s0=cursor,
                    s1=cursor + span,
                    width_m=round(float(width), 2),
                    nx=nx,
                    ny=ny,
                    structure=_is_structure(props),
                )
            )
            cursor += span
    by_oid: dict[int, list[Segment]] = {}
    for seg in out:
        by_oid.setdefault(seg.oid, []).append(seg)
    for segs in by_oid.values():
        segs.sort(key=lambda s: s.seq)
        road = [s for s in segs if not s.structure]
        for a, b in zip(road, road[1:]):
            a.next = b
            b.prev = a
    _link_across_oids(out)
    return out


def _link_across_oids(segs: list[Segment]) -> None:
    road = [s for s in segs if not s.structure and s.nx != 0.0]
    if len(road) < 2:
        return
    starts = [Point(s.coords[0]) for s in road]
    ends = [Point(s.coords[-1]) for s in road]
    tree_s = STRtree(starts)
    tree_e = STRtree(ends)

    def _nearest(seg: Segment, at_end: bool) -> Segment | None:
        pt = seg.coords[-1] if at_end else seg.coords[0]
        dx, dy = seg.direction
        tree = tree_s if at_end else tree_e
        best: Segment | None = None
        best_d = JOIN_M + 1.0
        for idx in tree.query(Point(pt).buffer(JOIN_M)):
            other = road[int(idx)]
            if other is seg or other.oid == seg.oid:
                continue
            if at_end and (seg.next is not None or other.prev is not None):
                continue
            if not at_end and (seg.prev is not None or other.next is not None):
                continue
            ox, oy = other.direction
            if dx * ox + dy * oy < JOIN_DOT:
                continue
            end = other.coords[0] if at_end else other.coords[-1]
            dist = math.hypot(end[0] - pt[0], end[1] - pt[1])
            if dist < best_d:
                best = other
                best_d = dist
        return best

    for seg in road:
        if seg.next is None:
            other = _nearest(seg, at_end=True)
            if other is not None:
                seg.next = other
                other.prev = seg
        if seg.prev is None:
            other = _nearest(seg, at_end=False)
            if other is not None:
                seg.prev = other
                other.next = seg


def _frame_offset(mine: Segment, other: Segment, value: float) -> float:
    dot = mine.direction[0] * other.direction[0] + mine.direction[1] * other.direction[1]
    return value if dot >= 0.0 else -value


def _measure(seg: Segment, radius: float, rough: np.ndarray, spec: dict) -> dict[float, dict]:
    scores: dict[float, dict] = {}
    for offset in _offsets():
        poly = _buffer(_shift(seg.coords, offset, seg.nx, seg.ny), radius)
        if poly is None:
            continue
        stats = _zone_stats(poly, rough, spec)
        if stats is None:
            continue
        scores[offset] = stats
        seg.trials.append(
            {
                "offset_m": offset,
                "narrow_m": seg.narrow_m,
                "width_used_m": seg.width_used_m,
                **stats,
            }
        )
    return scores


def _acceptable(stats: dict) -> bool:
    return stats["mean_rough_m"] <= GOOD_MEAN_M and stats["frac_hot"] <= MAX_FRAC_HOT


def _classify(seg: Segment) -> str:
    """Set kind from the current scores. 'pass' means some placement is acceptable."""
    if not seg.scores:
        seg.kind = "no_sample"
        seg.chosen = 0.0
        seg.good = []
        seg.passed = False
        return "empty"
    ranked = sorted(seg.scores.items(), key=lambda item: item[1]["mean_rough_m"])
    seg.best = float(ranked[0][0])
    means = [st["mean_rough_m"] for st in seg.scores.values()]
    seg.span = float(max(means) - min(means))
    clean = [off for off, st in seg.scores.items() if _acceptable(st)]
    seg.good = sorted(clean)
    if seg.span < CLEAR_GAP_M:
        if not clean:
            seg.kind = "flat"
            seg.chosen = 0.0
            seg.passed = False
            return "fail"
        seg.passed = True
        nearest = min(clean, key=lambda off: (abs(off), off))
        if abs(nearest) < 1e-9:
            seg.kind = "flat"
            seg.chosen = 0.0
        elif len(clean) == 1:
            seg.kind = "unique"
            seg.chosen = float(clean[0])
        else:
            seg.kind = "several"
            seg.chosen = float(nearest)
        return "pass"
    if not clean:
        seg.kind = "none"
        seg.chosen = 0.0
        seg.passed = False
        return "fail"
    decision = min(clean, key=lambda off: (seg.scores[off]["mean_rough_m"], abs(off)))
    best_score = seg.scores[decision]["mean_rough_m"]
    near = [off for off in clean if seg.scores[off]["mean_rough_m"] <= best_score + CLEAR_GAP_M]
    seg.passed = True
    if len(near) == 1:
        seg.kind = "unique"
        seg.chosen = float(near[0])
    else:
        seg.kind = "several"
        seg.chosen = float(decision)
    return "pass"


def _score_all(segs: list[Segment], rough: np.ndarray, spec: dict) -> None:
    for n, seg in enumerate(segs, start=1):
        if seg.structure or seg.nx == 0.0:
            seg.kind = "structure" if seg.structure else "degenerate"
            seg.chosen = 0.0
            seg.width_used_m = seg.width_m
            continue
        seg.narrow_m = 0.0
        while True:
            seg.width_used_m = round(seg.width_m - seg.narrow_m, 2)
            radius = seg.width_used_m / 2.0
            if radius < 0.5:
                break
            seg.scores = _measure(seg, radius, rough, spec)
            state = _classify(seg)
            if state != "fail":
                break
            if seg.narrow_m >= MAX_NARROW_M - 1e-6:
                break
            seg.narrow_m = round(min(MAX_NARROW_M, seg.narrow_m + WIDTH_STEP_M), 2)
        if n % 200 == 0:
            print(f"  scored {n}/{len(segs)}", flush=True)


def _lock_neighbours(segs: list[Segment]) -> None:
    fixed = {s.seg_id for s in segs if s.kind == "unique"}
    pending = [s for s in segs if s.kind == "several"]
    for _ in range(12):
        progressed = False
        for seg in pending:
            if seg.seg_id in fixed:
                continue
            known = []
            for other in (seg.prev, seg.next):
                if other is None or other.seg_id not in fixed or other.kind == "structure":
                    continue
                known.append(_frame_offset(seg, other, other.chosen))
            if not known:
                continue
            target = sum(known) / len(known)
            seg.target = round(target, 3)
            pick = min(seg.good, key=lambda off: (abs(off - target), abs(off)))
            seg.chosen = float(pick)
            fixed.add(seg.seg_id)
            progressed = True
        if not progressed:
            break
    for seg in pending:
        if seg.seg_id not in fixed:
            # No fixed neighbour. The raw minimum stays in best_m and is not
            # applied: without an anchor it is only the edge of the search.
            seg.kind = "several_unanchored"
            seg.chosen = 0.0


def _creep_line(segs: list[Segment]) -> list[tuple[float, float, float]]:
    """Original vertices, each moved by the offset interpolated along station."""
    flat = [segs[0].coords[0]]
    station = [0.0]
    cursor = 0.0
    for seg in segs:
        for pt in seg.coords[1:]:
            cursor += math.hypot(pt[0] - flat[-1][0], pt[1] - flat[-1][1])
            flat.append(pt)
            station.append(cursor)

    def at(s: float) -> float:
        if s <= segs[0].s_mid:
            return segs[0].chosen
        if s >= segs[-1].s_mid:
            return segs[-1].chosen
        for a, b in zip(segs, segs[1:]):
            if a.s_mid <= s <= b.s_mid and b.s_mid > a.s_mid:
                t = (s - a.s_mid) / (b.s_mid - a.s_mid)
                return a.chosen * (1.0 - t) + b.chosen * t
        return segs[-1].chosen

    normals = []
    for i, pt in enumerate(flat):
        if i == 0:
            a, b = flat[0], flat[1]
        elif i == len(flat) - 1:
            a, b = flat[-2], flat[-1]
        else:
            a, b = flat[i - 1], flat[i + 1]
        dx, dy = b[0] - a[0], b[1] - a[1]
        span = math.hypot(dx, dy)
        normals.append((0.0, 0.0) if span < 1e-6 else (-dy / span, dx / span))
    moved = []
    for pt, s, (nx, ny) in zip(flat, station, normals):
        off = at(s)
        moved.append((pt[0] + off * nx, pt[1] + off * ny, s))
    return moved


def _subline(samples: list[tuple[float, float, float]], s0: float, s1: float) -> list[tuple[float, float]]:
    if s1 <= s0 + 1e-6:
        return []
    pts: list[tuple[float, float]] = []
    for i, (x, y, s) in enumerate(samples):
        if s < s0:
            nxt = samples[i + 1] if i + 1 < len(samples) else None
            if nxt is not None and nxt[2] >= s0 and nxt[2] > s:
                t = (s0 - s) / (nxt[2] - s)
                pts.append((x + (nxt[0] - x) * t, y + (nxt[1] - y) * t))
            continue
        if s <= s1:
            if not pts or math.hypot(x - pts[-1][0], y - pts[-1][1]) > 1e-6:
                pts.append((x, y))
        if s >= s1:
            if s > s1 and i > 0 and pts:
                prev = samples[i - 1]
                if prev[2] < s1 < s:
                    t = (s1 - prev[2]) / (s - prev[2])
                    end = (prev[0] + (x - prev[0]) * t, prev[1] + (y - prev[1]) * t)
                    if math.hypot(end[0] - pts[-1][0], end[1] - pts[-1][1]) > 1e-6:
                        pts.append(end)
            break
    return pts


def _piece_width(seg: Segment) -> float:
    if seg.structure:
        return seg.width_m
    return seg.width_used_m or seg.width_m


def _width_ramps(segs: list[Segment]) -> list[tuple[float, float, float, float]]:
    """Linear width changes. The ramp lies on the wider piece, so the pinch stays narrow."""
    ramps: list[tuple[float, float, float, float]] = []
    for a, b in zip(segs, segs[1:]):
        wa, wb = _piece_width(a), _piece_width(b)
        if abs(wa - wb) < 0.05:
            continue
        if wa > wb:
            span = min(TAPER_M, max(a.s1 - a.s0, 0.0))
            if span < 0.5:
                continue
            ramps.append((b.s0 - span, wa, b.s0, wb))
        else:
            span = min(TAPER_M, max(b.s1 - b.s0, 0.0))
            if span < 0.5:
                continue
            ramps.append((a.s1, wa, a.s1 + span, wb))
    return ramps


def _width_at(segs: list[Segment], ramps: list[tuple[float, float, float, float]], s: float) -> float:
    base = segs[-1]
    for seg in segs:
        if s <= seg.s1 + 1e-6:
            base = seg
            break
    width = _piece_width(base)
    for s0, w0, s1, w1 in ramps:
        if s1 <= s0 or s < s0 - 1e-6 or s > s1 + 1e-6:
            continue
        t = min(1.0, max(0.0, (s - s0) / (s1 - s0)))
        width = min(width, w0 + (w1 - w0) * t)
    return width


def _densify(samples: list[tuple[float, float, float]], step: float) -> list[tuple[float, float, float]]:
    if len(samples) < 2:
        return samples
    out = [samples[0]]
    for (x0, y0, s0), (x1, y1, s1) in zip(samples, samples[1:]):
        dist = s1 - s0
        n = max(1, int(math.ceil(dist / step))) if dist > 1e-6 else 1
        for i in range(1, n + 1):
            t = i / n
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, s0 + (s1 - s0) * t))
    return out


def _tapered_carriageway(samples: list[tuple[float, float, float]], segs: list[Segment]):
    pts = _densify(samples, 2.0)
    if len(pts) < 2:
        return None
    ramps = _width_ramps(segs)
    left: list[tuple[float, float]] = []
    right: list[tuple[float, float]] = []
    for i, (x, y, s) in enumerate(pts):
        if i == 0:
            ax, ay, bx, by = pts[0][0], pts[0][1], pts[1][0], pts[1][1]
        elif i == len(pts) - 1:
            ax, ay, bx, by = pts[-2][0], pts[-2][1], pts[-1][0], pts[-1][1]
        else:
            ax, ay, bx, by = pts[i - 1][0], pts[i - 1][1], pts[i + 1][0], pts[i + 1][1]
        dx, dy = bx - ax, by - ay
        span = math.hypot(dx, dy)
        if span < 1e-6:
            continue
        nx, ny = -dy / span, dx / span
        half = _width_at(segs, ramps, s) / 2.0
        left.append((x + nx * half, y + ny * half))
        right.append((x - nx * half, y - ny * half))
    if len(left) < 2 or len(right) < 2:
        return None
    poly = Polygon(left + right[::-1])
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return None
    return poly


def _records(geoms, rows) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(rows, geometry=geoms, crs="EPSG:31254")


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    _ensure_raw_mosaic(site, proc)
    raw = proc / "corridor50_raw" / "corridor50_raw.tif"
    if not raw.is_file():
        raise SystemExit(f"raw DGM missing: {raw}")
    out_dir = proc / OUT_DIR_NAME
    out_dir.mkdir(parents=True, exist_ok=True)
    print("roughness from raw 0.5 m DGM", flush=True)
    z, spec = _load_geotiff(raw)
    rough = _roughness(z)
    del z
    finite = np.isfinite(rough)
    sample = rough[finite]
    if sample.size > 2_000_000:
        sample = sample[:: max(1, sample.size // 2_000_000)]
    pct = np.percentile(sample, [50, 90, 99]).tolist() if sample.size else []
    print(
        f"roughness finite {int(finite.sum())}  p50/p90/p99 {['%.4f' % v for v in pct]}",
        flush=True,
    )
    rough_path = out_dir / "01_roughness.tif"
    write_referenced_tif(rough_path, rough, xmin=spec["xmin"], ymax=spec["ymax"], res=RES_M)
    print(f"wrote {rough_path.name}", flush=True)

    segs = _load_segments(site)
    print(f"segments {len(segs)} from {len({s.oid for s in segs})} OBJECTIDs", flush=True)
    _score_all(segs, rough, spec)
    _lock_neighbours(segs)

    by_oid: dict[int, list[Segment]] = {}
    for seg in segs:
        by_oid.setdefault(seg.oid, []).append(seg)

    seg_rows = []
    seg_geoms = []
    trial_rows = []
    trial_geoms = []
    chosen_rows = []
    chosen_geoms = []
    block_rows = []
    block_geoms = []
    line_rows = []
    line_geoms = []
    buf_rows = []
    buf_geoms = []
    for oid, group in by_oid.items():
        group.sort(key=lambda s: s.seq)
        for seg in group:
            seg_geoms.append(LineString(seg.coords))
            seg_rows.append(
                {
                    "seg_id": seg.seg_id,
                    "objectid": seg.oid,
                    "seq": seg.seq,
                    "width_m": seg.width_m,
                    "width_used_m": seg.width_used_m or seg.width_m,
                    "narrow_m": seg.narrow_m,
                    "passed": int(seg.passed),
                    "structure": int(seg.structure),
                    "kind": seg.kind,
                    "chosen_m": seg.chosen,
                    "best_m": seg.best,
                    "n_good": len(seg.good),
                    "score_span_m": round(seg.span, 4),
                    "target_m": seg.target,
                }
            )
            kept_narrow = seg.narrow_m
            for trial in seg.trials:
                trial_geoms.append(LineString(_shift(seg.coords, trial["offset_m"], seg.nx, seg.ny)))
                trial_rows.append(
                    {
                        "seg_id": seg.seg_id,
                        "objectid": seg.oid,
                        "offset_m": trial["offset_m"],
                        "narrow_m": trial["narrow_m"],
                        "width_used_m": trial["width_used_m"],
                        "mean_rough_m": round(trial["mean_rough_m"], 4),
                        "p90_rough_m": round(trial["p90_rough_m"], 4),
                        "frac_hot": round(trial["frac_hot"], 3),
                        "n_px": trial["n_px"],
                        "under_threshold": int(trial["mean_rough_m"] <= GOOD_MEAN_M),
                        "kept_width": int(abs(trial["narrow_m"] - kept_narrow) < 1e-6),
                        "chosen": int(
                            abs(trial["narrow_m"] - kept_narrow) < 1e-6
                            and abs(trial["offset_m"] - seg.chosen) < 1e-6
                        ),
                    }
                )
            shifted = _shift(seg.coords, seg.chosen, seg.nx, seg.ny)
            used = seg.width_used_m or seg.width_m
            chosen_geoms.append(LineString(shifted))
            chosen_rows.append(
                {
                    "seg_id": seg.seg_id,
                    "objectid": seg.oid,
                    "offset_m": seg.chosen,
                    "kind": seg.kind,
                    "width_m": seg.width_m,
                    "width_used_m": used,
                    "narrow_m": seg.narrow_m,
                    "target_m": seg.target,
                    "mean_rough_m": round(seg.scores.get(seg.chosen, {}).get("mean_rough_m", -1), 4)
                    if seg.scores
                    else None,
                }
            )
            poly = _buffer(shifted, used / 2.0)
            if poly is not None and poly.geom_type == "Polygon":
                block_geoms.append(poly)
                block_rows.append(
                    {
                        "seg_id": seg.seg_id,
                        "objectid": seg.oid,
                        "offset_m": seg.chosen,
                        "width_used_m": used,
                        "narrow_m": seg.narrow_m,
                    }
                )
        if any(s.structure for s in group) and all(s.structure for s in group):
            continue
        moved = _creep_line(group)
        if len(moved) < 2:
            continue
        line = LineString([(x, y) for x, y, _s in moved])
        line_geoms.append(line)
        narrowed = [s for s in group if s.narrow_m > 0]
        line_rows.append(
            {
                "objectid": oid,
                "width_m": group[0].width_m,
                "n_segments": len(group),
                "n_narrowed": len(narrowed),
                "mean_abs_offset_m": round(float(np.mean([abs(s.chosen) for s in group])), 3),
            }
        )
        parts = _tapered_carriageway(moved, group)
        if parts is None:
            continue
        road = parts
        if road.geom_type == "Polygon":
            buf_geoms.append(road)
            buf_rows.append({"objectid": oid, "width_m": group[0].width_m, "n_narrowed": len(narrowed), "taper_m": TAPER_M})
        elif road.geom_type == "MultiPolygon":
            for part in road.geoms:
                buf_geoms.append(part)
                buf_rows.append({"objectid": oid, "width_m": group[0].width_m, "n_narrowed": len(narrowed), "taper_m": TAPER_M})

    gpkg = out_dir / "centerline_shift.gpkg"
    if gpkg.exists():
        gpkg.unlink()
    layers = (
        ("segments", seg_geoms, seg_rows),
        ("trials", trial_geoms, trial_rows),
        ("chosen", chosen_geoms, chosen_rows),
        ("chosen_buffer", block_geoms, block_rows),
        ("centerline", line_geoms, line_rows),
        ("carriageway", buf_geoms, buf_rows),
    )
    first = True
    for name, geoms, rows in layers:
        if not geoms:
            print(f"  layer {name} empty", flush=True)
            continue
        _records(geoms, rows).to_file(
            gpkg, layer=name, driver="GPKG", mode="w" if first else "a"
        )
        first = False
        print(f"  layer {name}: {len(geoms)}", flush=True)

    kinds: dict[str, int] = {}
    narrow_hist: dict[str, int] = {}
    for seg in segs:
        kinds[seg.kind] = kinds.get(seg.kind, 0) + 1
        if seg.kind in ("structure", "degenerate", "no_sample"):
            continue
        key = f"{seg.narrow_m:.1f}"
        narrow_hist[key] = narrow_hist.get(key, 0) + 1
    hist: dict[str, int] = {}
    for seg in segs:
        key = f"{seg.chosen:.1f}"
        hist[key] = hist.get(key, 0) + 1
    report = {
        "dgm": str(raw),
        "previous_run": str(proc / "centerline_shift"),
        "roughness": (
            f"Betrag der Abweichung vom Mittel der {ROUGH_WIN * RES_M:.1f} m-Umgebung "
            "im rohen DGM. Eine gleichmäßige Steigung liegt auf diesem Mittel und ist "
            f"kein Fehlerpunkt. Werte über {ROUGH_CLIP_M:.3f} m werden beschnitten, das ist "
            "das 95-%-Niveau der ruhigeren Seite. Eine Lage ist gut, wenn der Mittelwert "
            f"im Breitenpuffer höchstens {GOOD_MEAN_M:.3f} m ist und höchstens der Anteil "
            f"{MAX_FRAC_HOT:.2f} der Pixel auf der Kappung liegt. Liegen alle Lagen weniger als "
            f"{CLEAR_GAP_M:.3f} m auseinander, bleibt das Stück auf der Ausgangslage. "
            "Passiert das bei keiner Breite, wird die "
            f"Fahrbahn in {WIDTH_STEP_M:.2f} m-Schritten bis zu {MAX_NARROW_M:.1f} m schmaler."
        ),
        "segment_max_m": SEG_MAX_M,
        "step_m": STEP_M,
        "max_shift_m": MAX_SHIFT_M,
        "width_step_m": WIDTH_STEP_M,
        "max_narrow_m": MAX_NARROW_M,
        "taper_m": TAPER_M,
        "max_frac_hot": MAX_FRAC_HOT,
        "rough_clip_m": ROUGH_CLIP_M,
        "good_mean_m": GOOD_MEAN_M,
        "clear_gap_m": CLEAR_GAP_M,
        "roughness_p50_p90_p99_m": [round(float(v), 4) for v in pct],
        "segments": len(segs),
        "objectids": len(by_oid),
        "kinds": kinds,
        "narrow_m": narrow_hist,
        "chosen_offset_m": hist,
        "layers": {
            "01_roughness.tif": "roughness raster, clipped",
            "segments": "original pieces, max 20 m, with the width that was kept",
            "trials": "every 10 cm offset at every width that was tried",
            "chosen": "the offset and width kept for that piece",
            "chosen_buffer": "accepted-width buffer of the rigid shifted piece",
            "centerline": "original vertices crept sideways by the interpolated offset",
            "carriageway": "centerline buffered with a taper on the wider side into each pinch",
        },
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"kinds": kinds, "narrow_m": narrow_hist, "chosen_offset_m": hist}, indent=2))
    print(f"Wrote {gpkg}")
    _merge_sg_into_mesh(site, proc, gpkg)


def _sg_objectids(site: dict) -> set[int]:
    path = find_gip_geojson(site)
    data = json.loads(path.read_text(encoding="utf-8"))
    out: set[int] = set()
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        if str(props.get("OBJEKT") or "").upper().strip() != "S-G":
            continue
        try:
            out.add(int(props.get("OBJECTID")))
        except (TypeError, ValueError):
            continue
    return out


def _merge_sg_into_mesh(site: dict, proc: Path, src: Path) -> None:
    """Add S-G pieces to the package the road mesh reads.

    Landes- and Bundesstraßen in that package stay as they are. S-GW is not
    in ``src`` and is not copied.
    """
    dest = proc / "centerline_shift_taper" / "centerline_shift.gpkg"
    out = proc / "centerline_shift_taper" / "centerline_shift_with_sg.gpkg"
    if not dest.is_file() or dest.resolve() == src.resolve():
        return
    sg = _sg_objectids(site)
    layers = [str(name) for name in gpd.list_layers(dest)["name"]]
    frames: list[tuple[str, gpd.GeoDataFrame]] = []
    added = 0
    for name in layers:
        frame = gpd.read_file(dest, layer=name)
        if name in ("segments", "centerline", "carriageway") and "objectid" in frame.columns:
            extra = gpd.read_file(src, layer=name)
            have = {int(v) for v in frame.objectid}
            keep = extra.objectid.map(lambda v: int(v) in sg and int(v) not in have)
            extra = extra.loc[keep]
            if not extra.empty:
                frame = gpd.GeoDataFrame(
                    pd.concat([frame, extra], ignore_index=True),
                    geometry="geometry",
                    crs=frame.crs,
                )
                if name == "carriageway":
                    added = len(extra)
        frames.append((name, frame))
    if out.exists():
        out.unlink()
    first = True
    for name, frame in frames:
        frame.to_file(out, layer=name, driver="GPKG", mode="w" if first else "a")
        first = False
    print(f"S-G carriageways added: {added} -> {out.name}", flush=True)


if __name__ == "__main__":
    main()
