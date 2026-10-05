"""Trial: slide Südtirol Links axes sideways into TrasRoadArea asphalt.

Does not invent a new width. Each vertex moves only along the local left
normal until it sits on the midpoint of the asphalt chord (± search).
Tunnel / bridge axes (LOCATION / kunstbauten) stay put.

Writes under data/processed/<site>/centerline_snap_road_area/:
  report.json, preview.png, roads_beamng_snapped.json

With --apply: also writes roads_beamng.json (backup → roads_beamng_before_snap.json).

    cd C:\\temp\\beamng_autoroad
    $env:AUTOROAD_SITE = \"config/sites/timmelsjoch.yaml\"
    python tools\\snap_centerline_to_road_area.py
    python tools\\snap_centerline_to_road_area.py --apply
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from fetch_suedtirol_road_areas import (  # noqa: E402
    LOC_BRIDGE,
    LOC_GROUND,
    LOC_TUNNEL,
    PART_JUNCTION,
    TYPE_VEHICLE,
    _code,
    raw_geojson_path,
)
from fetch_suedtirol_road_links import axis_is_structure_span  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

OUT_DIR_NAME = "centerline_snap_road_area"
# Hard cap for the lateral probe. The chord must see both asphalt edges;
# a small window mid-biased the midpoint back onto the old axis.
MAX_SHIFT_M = 6.0
STEP_M = 0.10
MIN_CHORD_M = 1.5  # ignore tiny island hits


def _rings_beamng(geom: dict, *, xmin: float, ymin: float) -> list[list[tuple[float, float]]]:
    gtype = (geom or {}).get("type")
    coords = (geom or {}).get("coordinates")
    raw: list = []
    if gtype == "Polygon" and coords:
        raw.append(coords[0])
    elif gtype == "MultiPolygon" and coords:
        for poly in coords:
            if poly:
                raw.append(poly[0])
    out: list[list[tuple[float, float]]] = []
    for ring in raw:
        pts: list[tuple[float, float]] = []
        for c in ring:
            if len(c) < 2:
                continue
            pts.append((float(c[0]) - xmin, float(c[1]) - ymin))
        if len(pts) >= 3:
            out.append(pts)
    return out


def load_asphalt_union(site: dict):
    """Ground + junction vehicle pavement in BeamNG XY (tunnel/bridge omitted)."""
    path = raw_geojson_path(site)
    if not path.is_file():
        raise SystemExit(f"Missing TrasRoadArea geojson: {path}")
    fc = json.loads(path.read_text(encoding="utf-8"))
    xmin, ymin, *_ = (float(v) for v in site["bbox"])
    polys: list[Polygon] = []
    n_skip = 0
    for feat in fc.get("features") or []:
        props = feat.get("properties") or {}
        if _code(props, "TYPE") not in TYPE_VEHICLE:
            n_skip += 1
            continue
        loc = _code(props, "LOC")
        if loc in LOC_TUNNEL or loc in LOC_BRIDGE:
            n_skip += 1
            continue
        if loc and loc not in LOC_GROUND and _code(props, "PART") not in PART_JUNCTION:
            # Unknown LOC: keep only explicit ground / junction.
            if loc not in LOC_GROUND:
                n_skip += 1
                continue
        for ring in _rings_beamng(feat.get("geometry") or {}, xmin=xmin, ymin=ymin):
            poly = Polygon(ring)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty:
                n_skip += 1
                continue
            if isinstance(poly, MultiPolygon):
                polys.extend(p for p in poly.geoms if not p.is_empty)
            else:
                polys.append(poly)
    if not polys:
        raise SystemExit("No ground/junction TrasRoadArea polygons")
    union = unary_union(polys)
    if union.is_empty:
        raise SystemExit("TrasRoadArea union empty")
    print(f"TrasRoadArea asphalt polys={len(polys)} skipped={n_skip}")
    return union


def _tangent(nodes: list, i: int) -> tuple[float, float]:
    n = len(nodes)
    if n < 2:
        return (1.0, 0.0)
    if i <= 0:
        a, b = nodes[0], nodes[1]
    elif i >= n - 1:
        a, b = nodes[-2], nodes[-1]
    else:
        a, b = nodes[i - 1], nodes[i + 1]
    dx = float(b[0]) - float(a[0])
    dy = float(b[1]) - float(a[1])
    L = math.hypot(dx, dy)
    if L < 1e-9:
        return (1.0, 0.0)
    return (dx / L, dy / L)


def _left_normal(tx: float, ty: float) -> tuple[float, float]:
    return (-ty, tx)


def _inside_run_mid(
    x: float,
    y: float,
    nx: float,
    ny: float,
    geom,
    *,
    max_shift_m: float,
    step_m: float,
) -> tuple[float | None, float | None, str]:
    """Return (chosen_offset_m, chord_width_m, kind).

    The probe must reach asphalt *outside* on both sides of the chosen run.
    Otherwise a truncated window places the midpoint near the old axis.
    """
    n = int(round(max_shift_m / step_m))
    offsets = [i * step_m for i in range(-n, n + 1)]
    inside = []
    for o in offsets:
        pt = Point(x + nx * o, y + ny * o)
        inside.append(bool(geom.covers(pt)))
    runs: list[tuple[int, int]] = []
    i = 0
    while i < len(inside):
        if not inside[i]:
            i += 1
            continue
        j = i
        while j < len(inside) and inside[j]:
            j += 1
        runs.append((i, j - 1))
        i = j
    if not runs:
        return None, None, "miss"

    def _bounded(a: int, b: int) -> bool:
        # Both asphalt edges visible (not clipped by the search window).
        return a > 0 and b < len(inside) - 1

    best = None
    best_key = None
    for a, b in runs:
        o0, o1 = offsets[a], offsets[b]
        width = o1 - o0
        if width < MIN_CHORD_M - 1e-9:
            continue
        mid = 0.5 * (o0 + o1)
        contains0 = o0 <= 0.0 <= o1
        bounded = _bounded(a, b)
        # Prefer full chords, then those containing the axis, then nearest mid.
        key = (0 if bounded else 1, 0 if contains0 else 1, abs(mid), -width)
        if best_key is None or key < best_key:
            best_key = key
            kind = "center" if contains0 else "nearest_run"
            if not bounded:
                kind = "clipped_" + kind
            best = (mid, width, kind)
    if best is None:
        a, b = min(runs, key=lambda ab: abs(0.5 * (offsets[ab[0]] + offsets[ab[1]])))
        mid = 0.5 * (offsets[a] + offsets[b])
        return mid, offsets[b] - offsets[a], "tiny"
    return best[0], best[1], best[2]


def snap_road(
    road: dict,
    geom,
    *,
    max_shift_m: float,
    step_m: float,
) -> tuple[dict, list[dict]]:
    nodes = road.get("nodes") or []
    if len(nodes) < 2 or axis_is_structure_span(road):
        return road, []
    new_nodes = []
    events = []
    for i, n in enumerate(nodes):
        x, y = float(n[0]), float(n[1])
        z = n[2] if len(n) > 2 else None
        w = float(n[3]) if len(n) > 3 else 7.0
        tx, ty = _tangent(nodes, i)
        nx, ny = _left_normal(tx, ty)
        off, chord, kind = _inside_run_mid(
            x, y, nx, ny, geom, max_shift_m=max_shift_m, step_m=step_m
        )
        if off is None:
            new_nodes.append([x, y, z, w])
            events.append(
                {
                    "i": i,
                    "offset_m": 0.0,
                    "kind": "miss",
                    "x0": x,
                    "y0": y,
                    "x1": x,
                    "y1": y,
                }
            )
            continue
        x1 = x + nx * off
        y1 = y + ny * off
        new_nodes.append([round(x1, 4), round(y1, 4), z, w])
        events.append(
            {
                "i": i,
                "offset_m": round(float(off), 3),
                "chord_m": None if chord is None else round(float(chord), 3),
                "kind": kind,
                "x0": round(x, 4),
                "y0": round(y, 4),
                "x1": round(x1, 4),
                "y1": round(y1, 4),
            }
        )
    out = dict(road)
    out["nodes"] = new_nodes
    return out, events


def _load_roads(proc: Path) -> dict:
    # Prefer pristine Links cache; fall back to current roads_beamng.
    for name in ("suedtirol_roads_beamng.json", "roads_beamng.json"):
        path = proc / name
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            print(f"Loaded {path.name}: {len(data)} roads")
            return data
    raise SystemExit("No roads_beamng / suedtirol_roads_beamng.json")


def _attach_z_from(source: dict, target: dict) -> None:
    """Copy node Z from a height-filled roads dict onto snapped XY (same keys/order)."""
    n_copy = n_miss = 0
    for rid, road in target.items():
        src = source.get(rid)
        if not src:
            n_miss += 1
            continue
        src_nodes = src.get("nodes") or []
        nodes = road.get("nodes") or []
        for i, n in enumerate(nodes):
            if i >= len(src_nodes):
                break
            z = src_nodes[i][2] if len(src_nodes[i]) > 2 else None
            if z is None:
                continue
            n[2] = float(z)
            n_copy += 1
    print(f"Copied Z onto snapped nodes: {n_copy} (roads without source: {n_miss})")


def _fill_z_from_heightmap(site: dict, roads: dict) -> None:
    """Sample heightmap Z at existing BeamNG XY (do not re-map XY)."""
    from build_smoke import sample_z
    from PIL import Image

    proc = processed_dir(site)
    size = int((site.get("beamng") or {}).get("mask_size") or 2048)
    hm = proc / f"heightmap_{size}.png"
    meta = json.loads((proc / "heightmap_meta.json").read_text(encoding="utf-8"))
    max_h = float(meta["max_height_m"])
    # PNG is already relative to terrain base — do not subtract z_min again.
    elev = np.asarray(Image.open(hm), dtype=np.float64) / 65535.0 * max_h
    n_ok = n_drop = 0
    for road in roads.values():
        nodes = []
        for n in road.get("nodes") or []:
            lx, ly = float(n[0]), float(n[1])
            w = float(n[3]) if len(n) > 3 else 7.0
            z_abs = sample_z(elev, lx, ly)
            if z_abs is None:
                n_drop += 1
                continue
            nodes.append([round(lx, 4), round(ly, 4), round(float(z_abs), 3), round(w, 2)])
            n_ok += 1
        if len(nodes) >= 3:
            zs = [p[2] for p in nodes]
            sm = zs[:]
            for i in range(1, len(zs) - 1):
                sm[i] = 0.25 * zs[i - 1] + 0.5 * zs[i] + 0.25 * zs[i + 1]
            for i, p in enumerate(nodes):
                p[2] = round(sm[i], 3)
        road["nodes"] = nodes
    print(f"Filled Z from heightmap: ok={n_ok} dropped={n_drop}")


def _preview(site: dict, proc: Path, before: dict, after: dict, events_by_road: dict) -> Path:
    size = int((site.get("beamng") or {}).get("mask_size") or 2048)
    mask_p = proc / "mask_road_area.png"
    if mask_p.is_file():
        base = np.asarray(Image.open(mask_p).convert("L").resize((size, size)))
        rgb = np.zeros((size, size, 3), dtype=np.uint8)
        rgb[:] = (20, 20, 24)
        rgb[base > 0] = (55, 55, 60)
    else:
        rgb = np.zeros((size, size, 3), dtype=np.uint8)
        rgb[:] = (20, 20, 24)
    img = Image.fromarray(rgb, mode="RGB")
    draw = ImageDraw.Draw(img)

    def draw_road(roads: dict, color: tuple[int, int, int], width: int = 1) -> None:
        for road in roads.values():
            pts = []
            for n in road.get("nodes") or []:
                # BeamNG Y-up → image row down
                px = float(n[0])
                py = size - float(n[1])
                pts.append((px, py))
            if len(pts) >= 2:
                draw.line(pts, fill=color, width=width)

    draw_road(before, (200, 70, 70), 2)
    draw_road(after, (70, 200, 110), 2)
    # Show largest moves
    moves = []
    for rid, evs in events_by_road.items():
        for e in evs:
            if abs(float(e.get("offset_m") or 0.0)) >= 0.3:
                moves.append(e)
    moves.sort(key=lambda e: -abs(float(e["offset_m"])))
    for e in moves[:80]:
        draw.line(
            [
                (e["x0"], size - e["y0"]),
                (e["x1"], size - e["y1"]),
            ],
            fill=(240, 220, 60),
            width=1,
        )
    out = proc / OUT_DIR_NAME / "preview.png"
    img.save(out)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Write roads_beamng.json")
    ap.add_argument("--max-shift-m", type=float, default=MAX_SHIFT_M)
    ap.add_argument("--step-m", type=float, default=STEP_M)
    args = ap.parse_args()

    site = load_site()
    proc = processed_dir(site)
    out_dir = proc / OUT_DIR_NAME
    out_dir.mkdir(parents=True, exist_ok=True)

    asphalt = load_asphalt_union(site)
    before = _load_roads(proc)
    after: dict = {}
    events_by_road: dict[str, list] = {}
    n_struct = n_moved = n_miss = 0
    offsets: list[float] = []

    for rid, road in before.items():
        if axis_is_structure_span(road):
            after[rid] = road
            n_struct += 1
            continue
        snapped, evs = snap_road(
            road,
            asphalt,
            max_shift_m=float(args.max_shift_m),
            step_m=float(args.step_m),
        )
        after[rid] = snapped
        events_by_road[rid] = evs
        for e in evs:
            off = abs(float(e.get("offset_m") or 0.0))
            if e.get("kind") == "miss":
                n_miss += 1
            elif off >= 0.05:
                n_moved += 1
                offsets.append(float(e["offset_m"]))
            else:
                offsets.append(float(e.get("offset_m") or 0.0))

    snapped_path = out_dir / "roads_beamng_snapped.json"
    snapped_path.write_text(json.dumps(after, indent=2), encoding="utf-8")

    arr = np.array(offsets, dtype=float) if offsets else np.array([0.0])
    report = {
        "site": site.get("name"),
        "max_shift_m": float(args.max_shift_m),
        "step_m": float(args.step_m),
        "roads": len(before),
        "structure_skipped": n_struct,
        "vertices_moved_ge_5cm": n_moved,
        "vertices_miss": n_miss,
        "offset_abs_m": {
            "mean": round(float(np.mean(np.abs(arr))), 3),
            "p50": round(float(np.percentile(np.abs(arr), 50)), 3),
            "p90": round(float(np.percentile(np.abs(arr), 90)), 3),
            "max": round(float(np.max(np.abs(arr))), 3),
        },
        "offset_signed_m": {
            "mean": round(float(np.mean(arr)), 3),
            "p10": round(float(np.percentile(arr, 10)), 3),
            "p90": round(float(np.percentile(arr, 90)), 3),
        },
        "applied": bool(args.apply),
        "note": (
            "Positive offset = left of travel direction. "
            "Tunnel/bridge axes unchanged. Preview: red=before green=after yellow=moves."
        ),
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out_dir / "events.json").write_text(
        json.dumps(events_by_road), encoding="utf-8"
    )
    preview = _preview(site, proc, before, after, events_by_road)
    print(json.dumps(report, indent=2))
    print(f"Wrote {snapped_path}")
    print(f"Wrote {preview}")

    if args.apply:
        dest = proc / "roads_beamng.json"
        bak = proc / "roads_beamng_before_snap.json"
        if dest.is_file() and not bak.is_file():
            shutil.copy2(dest, bak)
            print(f"Backup {bak.name}")
        elif bak.is_file():
            print(f"Keeping existing backup {bak.name}")
        # Re-sample Z at the new XY — old Z was measured on the hang side.
        _fill_z_from_heightmap(site, after)
        snapped_path.write_text(json.dumps(after, indent=2), encoding="utf-8")
        dest.write_text(json.dumps(after, indent=2), encoding="utf-8")
        print(f"Applied -> {dest}")


if __name__ == "__main__":
    main()
