"""Sample DecalRoad carriageway profile from the live BeamNG level (or build copy).

Reads asphalt DecalRoads (drivability > 0) from
  <user_levels>/<level>/…/roads/items.level.json
(fallback: data/processed/<site>/decal_roads_items.level.json).

Emits a long-format CSV: one row per (road, s, lateral offset) that still lies
on the authored ribbon (|offset| <= half_width). Offsets outside the road are
omitted so gaps/thresholds are easy to spot.

Also prints a short anomaly report (node ΔZ jumps, width pinches, end-to-end
XY gaps between consecutive asphalt strips).

Examples:
  set AUTOROAD_SITE=config/sites/fernpass_mega.yaml
  python tools/diag_decal_road_profile.py
  python tools/diag_decal_road_profile.py --offsets 0,-2,2,-3.5 --step 2
  python tools/diag_decal_road_profile.py --name Brunnwald --include-edges
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from site_coords import load_site, processed_dir  # noqa: E402

USER_LEVELS = (
    Path.home()
    / "AppData"
    / "Local"
    / "BeamNG"
    / "BeamNG.drive"
    / "current"
    / "levels"
)


def _roads_items_path(level_name: str, proc: Path) -> Path:
    live = (
        USER_LEVELS
        / level_name
        / "main"
        / "MissionGroup"
        / "level_objects"
        / "roads"
        / "items.level.json"
    )
    if live.is_file():
        return live
    copy = proc / "decal_roads_items.level.json"
    if copy.is_file():
        return copy
    raise SystemExit(
        f"No DecalRoad items found.\n  tried: {live}\n  tried: {copy}"
    )


def _load_ndjson(path: Path) -> list[dict]:
    out: list[dict] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _asphalt_roads(entries: list[dict], name_filter: str | None) -> list[dict]:
    roads: list[dict] = []
    for e in entries:
        if e.get("class") != "DecalRoad":
            continue
        if float(e.get("drivability") or 0.0) <= 0:
            continue
        name = str(e.get("name") or "")
        if name_filter and name_filter.lower() not in name.lower():
            continue
        nodes = e.get("nodes") or []
        if len(nodes) < 2:
            continue
        roads.append(e)
    return roads


def _cum_s(nodes: list[list[float]]) -> list[float]:
    cum = [0.0]
    for a, b in zip(nodes, nodes[1:]):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    return cum


def _interp_at_s(
    nodes: list[list[float]], cum: list[float], s: float
) -> tuple[float, float, float, float, float, float]:
    """Return x,y,z,w,tx,ty at arc length s (tangent unit in XY)."""
    total = cum[-1]
    if total <= 1e-9:
        n = nodes[0]
        return float(n[0]), float(n[1]), float(n[2]), float(n[3]), 1.0, 0.0
    s = max(0.0, min(total, s))
    i = 0
    while i + 1 < len(cum) and cum[i + 1] < s - 1e-12:
        i += 1
    if i + 1 >= len(nodes):
        i = len(nodes) - 2
    a, b = nodes[i], nodes[i + 1]
    seg = cum[i + 1] - cum[i]
    t = 0.0 if seg < 1e-12 else (s - cum[i]) / seg
    x = a[0] + t * (b[0] - a[0])
    y = a[1] + t * (b[1] - a[1])
    z = a[2] + t * (b[2] - a[2])
    w = a[3] + t * (b[3] - a[3])
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = math.hypot(dx, dy)
    if L < 1e-9:
        # Fall back to longer window.
        for j in range(max(0, i - 1), min(len(nodes) - 1, i + 2)):
            dx = nodes[j + 1][0] - nodes[j][0]
            dy = nodes[j + 1][1] - nodes[j][1]
            L = math.hypot(dx, dy)
            if L > 1e-9:
                break
    if L < 1e-9:
        tx, ty = 1.0, 0.0
    else:
        tx, ty = dx / L, dy / L
    return x, y, z, w, tx, ty


def _parse_offsets(raw: str | None) -> list[float] | None:
    if raw is None or not str(raw).strip():
        return None
    vals: list[float] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        vals.append(float(part))
    return vals


def _load_z_at(proc: Path, size: int, extent: float):
    """Prefer road_bed → water → bridge_conform → pristine. Returns (z_at, label)|None."""
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return None

    meta_path = proc / "heightmap_meta.json"
    if not meta_path.is_file():
        return None
    max_h = float(json.loads(meta_path.read_text(encoding="utf-8"))["max_height_m"])
    candidates = [
        proc / f"heightmap_{size}_composed.png",
        proc / f"heightmap_{size}_road_bed.png",
        proc / f"heightmap_{size}_water.png",
        proc / f"heightmap_{size}_bridge_conform.png",
        proc / f"heightmap_{size}.png",
    ]
    hm_path = next((p for p in candidates if p.is_file()), None)
    if hm_path is None:
        return None
    hm = np.asarray(Image.open(hm_path))
    n = int(hm.shape[0])

    def z_at(bx: float, by: float) -> float:
        px = max(0.0, min(float(n - 1), bx / extent * (n - 1)))
        py = max(0.0, min(float(n - 1), (1.0 - by / extent) * (n - 1)))
        x0 = int(math.floor(px))
        y0 = int(math.floor(py))
        x1 = min(x0 + 1, n - 1)
        y1 = min(y0 + 1, n - 1)
        tx, ty = px - x0, py - y0
        v = (
            float(hm[y0, x0]) * (1 - tx) * (1 - ty)
            + float(hm[y0, x1]) * tx * (1 - ty)
            + float(hm[y1, x0]) * (1 - tx) * ty
            + float(hm[y1, x1]) * tx * ty
        )
        return (v / 65535.0) * max_h

    return z_at, hm_path.name


def _sample_road(
    name: str,
    nodes: list[list[float]],
    *,
    step_m: float,
    offsets: list[float],
    include_edges: bool,
    z_at,
) -> list[dict]:
    cum = _cum_s(nodes)
    total = cum[-1]
    if total <= 0:
        return []
    s_vals = [0.0]
    s = step_m
    while s < total - 1e-9:
        s_vals.append(s)
        s += step_m
    if s_vals[-1] < total - 1e-6:
        s_vals.append(total)

    rows: list[dict] = []
    for s_m in s_vals:
        x, y, z, w, tx, ty = _interp_at_s(nodes, cum, s_m)
        half = 0.5 * max(0.0, float(w))
        # Left normal (CCW from tangent in XY).
        lx, ly = -ty, tx
        want: list[tuple[str, float]] = [("offset", o) for o in offsets]
        if include_edges and half > 1e-6:
            want.append(("edge_left", half))
            want.append(("edge_right", -half))
        # Deduplicate identical lateral positions (e.g. offset==half and edge).
        seen: set[float] = set()
        for kind, lat in want:
            key = round(lat, 4)
            if key in seen:
                continue
            # No road surface beyond half-width → omit (edges use exact half).
            if abs(lat) > half + 1e-6:
                continue
            seen.add(key)
            px = x + lat * lx
            py = y + lat * ly
            row = {
                "road": name,
                "s_m": round(s_m, 3),
                "offset_m": round(lat, 4),
                "kind": kind if kind.startswith("edge") else "offset",
                "x": round(px, 3),
                "y": round(py, 3),
                "z": round(z, 3),
                "half_w_m": round(half, 3),
                "tx": round(tx, 5),
                "ty": round(ty, 5),
            }
            if z_at is not None:
                zh = float(z_at(px, py))
                row["z_hm"] = round(zh, 3)
                row["dz_node_hm"] = round(z - zh, 3)
            rows.append(row)
    return rows


def _strip_base(name: str) -> str:
    """decal_Foo_12_s3 → decal_Foo_12 (length-split siblings share a base)."""
    return re.sub(r"_s\d+$", "", name)


def _seg_index(name: str) -> int:
    m = re.search(r"_s(\d+)$", name)
    return int(m.group(1)) if m else 0


def _anomalies(
    roads: list[dict],
    *,
    jump_dz_m: float,
    jump_grade: float,
    gap_xy_m: float,
    pinch_w_m: float,
) -> list[str]:
    msgs: list[str] = []
    for e in roads:
        name = str(e.get("name") or "")
        nodes = [[float(v) for v in n[:4]] for n in e["nodes"]]
        for i, n in enumerate(nodes):
            if float(n[3]) <= pinch_w_m:
                msgs.append(f"PINCH_W  {name} node[{i}] w={n[3]:.2f}m")
        for i in range(1, len(nodes)):
            a, b = nodes[i - 1], nodes[i]
            ds = math.hypot(b[0] - a[0], b[1] - a[1])
            dz = b[2] - a[2]
            if ds <= 1e-6:
                continue
            grade = dz / ds
            # Absolute step on a short chord, or extreme grade on longer chords.
            if (ds <= 2.0 and abs(dz) >= jump_dz_m) or abs(grade) >= jump_grade:
                msgs.append(
                    f"JUMP_DZ  {name} node[{i-1}->{i}] "
                    f"ds={ds:.2f}m dz={dz:+.3f}m grade={grade:+.3f}"
                )

    # Continuity between max_decal_length siblings (_s0, _s1, …).
    by_base: dict[str, list[dict]] = {}
    for e in roads:
        by_base.setdefault(_strip_base(str(e.get("name") or "")), []).append(e)
    for base, group in by_base.items():
        if len(group) < 2:
            continue
        group = sorted(group, key=lambda e: _seg_index(str(e.get("name") or "")))
        for a, b in zip(group, group[1:]):
            p = a["nodes"][-1]
            q = b["nodes"][0]
            dxy = math.hypot(float(q[0]) - float(p[0]), float(q[1]) - float(p[1]))
            dz = float(q[2]) - float(p[2])
            if dxy >= gap_xy_m or abs(dz) >= jump_dz_m:
                msgs.append(
                    f"STRIP_GAP {a.get('name')} -> {b.get('name')} "
                    f"(end->start) dxy={dxy:.3f}m dz={dz:+.3f}m base={base}"
                )
    return msgs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--level",
        default=None,
        help="BeamNG level folder name (default: site beamng.level_name)",
    )
    ap.add_argument(
        "--roads-json",
        type=Path,
        default=None,
        help="Override path to roads items.level.json (NDJSON)",
    )
    ap.add_argument(
        "--name",
        default=None,
        help="Substring filter on DecalRoad name (e.g. Brunnwald)",
    )
    ap.add_argument(
        "--step",
        type=float,
        default=1.0,
        help="Sample spacing along centerline in meters (default 1)",
    )
    ap.add_argument(
        "--offsets",
        default="0",
        help="Comma-separated lateral offsets in m (left +, right -). "
        "Samples with |offset| > half_width are omitted. Default: 0",
    )
    ap.add_argument(
        "--include-edges",
        action="store_true",
        help="Also emit left/right ribbon edges (±half_width) at each s",
    )
    ap.add_argument(
        "--no-heightmap",
        action="store_true",
        help="Do not sample processed heightmap for z_hm",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="CSV output path (default: data/processed/<site>/diag_decal_road_profile.csv)",
    )
    ap.add_argument("--jump-dz", type=float, default=0.15, help="Abs ΔZ flag on short chords <=2m (m)")
    ap.add_argument(
        "--jump-grade",
        type=float,
        default=0.25,
        help="|dz/ds| grade flag threshold (default 0.25 = 25%%)",
    )
    ap.add_argument("--gap-xy", type=float, default=0.5, help="Strip endpoint XY gap (m)")
    ap.add_argument("--pinch-w", type=float, default=4.0, help="Width pinch threshold (m)")
    args = ap.parse_args()

    site = load_site()
    bng = site.get("beamng") or {}
    level_name = str(args.level or bng.get("level_name") or "").strip()
    if not level_name and args.roads_json is None:
        raise SystemExit("beamng.level_name missing (or pass --level / --roads-json)")

    proc = processed_dir(site)
    path = Path(args.roads_json) if args.roads_json else _roads_items_path(level_name, proc)
    entries = _load_ndjson(path)
    roads = _asphalt_roads(entries, args.name)
    if not roads:
        raise SystemExit(f"No asphalt DecalRoads in {path}" + (f" matching '{args.name}'" if args.name else ""))

    offsets = _parse_offsets(args.offsets) or [0.0]
    # Unique preserve order.
    seen_o: set[float] = set()
    offsets_u: list[float] = []
    for o in offsets:
        k = round(o, 6)
        if k not in seen_o:
            seen_o.add(k)
            offsets_u.append(float(o))

    size = int(bng.get("mask_size") or 512)
    meta_path = proc / "heightmap_meta.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        extent = float(meta.get("terrain_extent_m") or (size * float(bng.get("meters_per_pixel") or 1.0)))
    else:
        extent = float(size) * float(bng.get("meters_per_pixel") or 1.0)

    z_pack = None if args.no_heightmap else _load_z_at(proc, size, extent)
    z_at = z_pack[0] if z_pack else None
    hm_label = z_pack[1] if z_pack else None

    all_rows: list[dict] = []
    total_len = 0.0
    for e in roads:
        nodes = [[float(v) for v in n[:4]] for n in e["nodes"]]
        cum = _cum_s(nodes)
        total_len += cum[-1]
        all_rows.extend(
            _sample_road(
                str(e.get("name") or ""),
                nodes,
                step_m=max(0.05, float(args.step)),
                offsets=offsets_u,
                include_edges=bool(args.include_edges),
                z_at=z_at,
            )
        )

    out = args.out or (proc / "diag_decal_road_profile.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "road",
        "s_m",
        "offset_m",
        "kind",
        "x",
        "y",
        "z",
        "half_w_m",
        "tx",
        "ty",
    ]
    if z_at is not None:
        fieldnames += ["z_hm", "dz_node_hm"]
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in all_rows:
            w.writerow(row)

    print(f"source: {path}")
    print(f"asphalt strips: {len(roads)}  total_len~{total_len:.1f}m")
    print(f"offsets: {offsets_u}" + (" +edges" if args.include_edges else ""))
    print(f"samples written: {len(all_rows)} -> {out}")
    if hm_label:
        print(f"heightmap: {hm_label}")
    elif not args.no_heightmap:
        print("heightmap: (none found)")

    msgs = _anomalies(
        roads,
        jump_dz_m=float(args.jump_dz),
        jump_grade=float(args.jump_grade),
        gap_xy_m=float(args.gap_xy),
        pinch_w_m=float(args.pinch_w),
    )
    print(
        f"anomalies: {len(msgs)} "
        f"(jump_dz>={args.jump_dz} on ds<=2m OR |grade|>={args.jump_grade}; "
        f"gap_xy>={args.gap_xy}; pinch_w<={args.pinch_w})"
    )
    for m in msgs[:80]:
        print(f"  {m}")
    if len(msgs) > 80:
        print(f"  … {len(msgs) - 80} more")


if __name__ == "__main__":
    main()
