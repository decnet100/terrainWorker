"""Hard facts: does gip_pad/extend change Kriegerbach MeshRoad endpoints?"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from site_coords import SiteCoords, load_site, processed_dir  # noqa: E402
import build_bridges as bb  # noqa: E402


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    sc = SiteCoords(site)
    roads = bb.load_road_polylines(proc)
    z = bb.load_terrain_z(site)
    feat = next(f for f in bb.bridge_features(site, sc) if f.get("objectid") == 4103)
    defaults, items = bb.default_bridge_cfg(site.get("beamng") or {})
    cfg0 = bb.resolve_bridge_cfg(defaults, items, feat)
    road = bb.stitch_road_chain(roads, bb.pick_road(roads, feat["xy"]))

    def run(pad: float, ext_b: float, ext_a: float, search: float) -> dict:
        cfg = copy.deepcopy(cfg0)
        cfg["gip_pad_m"] = pad
        cfg["extend_before_m"] = ext_b
        cfg["extend_after_m"] = ext_a
        cfg["span_search_m"] = search
        cfg["auto_span"] = True
        xy, zs, _n, _w, info = bb.build_span_on_road(feat["xy"], road, z, cfg)
        length = sum(
            math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(xy, xy[1:])
        )
        return {
            "pad": pad,
            "ext_b": ext_b,
            "ext_a": ext_a,
            "search": search,
            "span": info["span_len_m"],
            "gap": info["gap_len_m"],
            "s0": info["s0"],
            "s1": info["s1"],
            "gap_s0": info["gap_s0"],
            "gap_s1": info["gap_s1"],
            "n": len(xy),
            "L": round(length, 2),
            "start": (round(xy[0][0], 2), round(xy[0][1], 2)),
            "end": (round(xy[-1][0], 2), round(xy[-1][1], 2)),
        }

    cases = [
        run(0, 0, 0, 25),
        run(0, 30, 30, 25),
        run(20, 8, 8, 80),
        run(50, 0, 0, 25),
        run(50, 30, 30, 25),
    ]
    print("Kriegerbach oid=4103 — in-memory A/B (build_span_on_road)")
    print(
        f"{'pad':>4} {'extB':>5} {'extA':>5} {'srch':>5} "
        f"{'span':>7} {'gap':>7} {'s0':>8} {'s1':>8} start_xy end_xy"
    )
    for c in cases:
        print(
            f"{c['pad']:4.0f} {c['ext_b']:5.0f} {c['ext_a']:5.0f} {c['search']:5.0f} "
            f"{c['span']:7.1f} {c['gap']:7.1f} {c['s0']:8.1f} {c['s1']:8.1f} "
            f"{c['start']} {c['end']}"
        )

    # Prove endpoints move when pad changes
    a = cases[0]
    b = cases[3]
    print("\nDelta pad0 vs pad50 (no extends):")
    print(f"  span {a['span']} -> {b['span']}  (delta {b['span'] - a['span']:.1f})")
    print(f"  start {a['start']} -> {b['start']}")
    print(f"  end   {a['end']} -> {b['end']}")
    print(f"  s0    {a['s0']} -> {b['s0']}")
    print(f"  s1    {a['s1']} -> {b['s1']}")

    level = (
        Path.home()
        / "AppData/Local/BeamNG/BeamNG.drive/current/levels/autoroad_fernpass_4096"
    )
    ip = level / "main/MissionGroup/level_objects/bridges/items.level.json"
    print(f"\nON DISK: {ip}")
    print(f"  exists={ip.exists()} mtime={ip.stat().st_mtime if ip.exists() else None}")
    if ip.exists():
        print(f"  sha1={hashlib.sha1(ip.read_bytes()).hexdigest()[:12]}")
        for line in ip.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            if "4103" not in e.get("name", ""):
                continue
            nodes = e["nodes"]
            length = sum(
                math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(nodes, nodes[1:])
            )
            print(f"  disk nodes={len(nodes)} len={length:.1f}")
            print(f"  disk start={nodes[0][:3]}")
            print(f"  disk end  ={nodes[-1][:3]}")

    print("\nOther level files mentioning 4103/Kriegerbach:")
    for p in level.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in {".json", ".cs", ".txt"}:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "4103" in text or "Kriegerbach" in text:
            print(f"  {p.relative_to(level)}")


if __name__ == "__main__":
    main()
