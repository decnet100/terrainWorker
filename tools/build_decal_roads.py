"""Build BeamNG DecalRoad strips from roads_beamng.json (OSM centerlines).

Layers (configurable in site YAML ``beamng.decal_roads``):
  - carriageway: asphalt DecalRoad (drivability > 0)
  - centerline: painted line on top (drivability -1), style solid | dashed | none

Dashed markings are geometric (dash/gap segments), not a special texture —
works with a solid white line material.

Usage:
  $env:AUTOROAD_SITE='config/sites/l13_kuehtai.yaml'
  python tools/build_decal_roads.py
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import load_site, processed_dir  # noqa: E402
import build_bridges as bb  # noqa: E402

USER_LEVELS = bb.USER_LEVELS


def _cfg(bng: dict) -> dict:
    raw = bng.get("decal_roads") or {}
    defaults = raw.get("defaults") if isinstance(raw.get("defaults"), dict) else raw
    return {
        "enabled": bool(defaults.get("enabled", True)),
        "width_scale": float(defaults.get("width_scale") or 1.0),
        "material": str(defaults.get("material") or "Asphalt"),
        "texture_length": float(defaults.get("texture_length") or 8.0),
        "render_priority": int(defaults.get("render_priority") or 10),
        "drivability": float(defaults.get("drivability") if defaults.get("drivability") is not None else 1.0),
        "improved_spline": bool(defaults.get("improved_spline", True)),
        "smoothness": float(defaults.get("smoothness") if defaults.get("smoothness") is not None else 0.5),
        "detail": float(defaults.get("detail") if defaults.get("detail") is not None else 0.1),
        "decal_bias": float(defaults.get("decal_bias") if defaults.get("decal_bias") is not None else 0.002),
        "over_objects": bool(defaults.get("over_objects", False)),
        "start_end_fade": list(defaults.get("start_end_fade") or [2.0, 2.0]),
        "highways": [
            str(h).lower()
            for h in (
                defaults.get("highways")
                or ["motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential"]
            )
        ],
        "min_length_m": float(defaults.get("min_length_m") or 8.0),
        "clip_galleries": bool(defaults.get("clip_galleries", True)),
        "stop_before_gallery_m": float(defaults.get("stop_before_gallery_m") or 2.0),
        "gallery_clip_pad_m": float(defaults.get("gallery_clip_pad_m") or 1.0),
        "centerline": {
            "enabled": bool((defaults.get("centerline") or {}).get("enabled", True)),
            "style": str((defaults.get("centerline") or {}).get("style") or "dashed").lower(),
            "width_m": float((defaults.get("centerline") or {}).get("width_m") or 0.15),
            "material": str((defaults.get("centerline") or {}).get("material") or "Road_Line_White"),
            "texture_length": float((defaults.get("centerline") or {}).get("texture_length") or 4.0),
            "render_priority": int((defaults.get("centerline") or {}).get("render_priority") or 25),
            "dash_m": float((defaults.get("centerline") or {}).get("dash_m") or 3.0),
            "gap_m": float((defaults.get("centerline") or {}).get("gap_m") or 6.0),
            "decal_bias": float(
                (defaults.get("centerline") or {}).get("decal_bias")
                if (defaults.get("centerline") or {}).get("decal_bias") is not None
                else 0.004
            ),
        },
    }


def _polyline_length(nodes: list[list[float]]) -> float:
    total = 0.0
    for a, b in zip(nodes, nodes[1:]):
        total += math.hypot(b[0] - a[0], b[1] - a[1])
    return total


def _resample_by_s(
    nodes: list[list[float]],
) -> tuple[list[float], list[list[float]]]:
    """Cumulative s + nodes (xyzw)."""
    cum = [0.0]
    for a, b in zip(nodes, nodes[1:]):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    return cum, nodes


def _sample_at_s(cum: list[float], nodes: list[list[float]], s: float) -> list[float]:
    if s <= cum[0]:
        return list(nodes[0])
    if s >= cum[-1]:
        return list(nodes[-1])
    j = 0
    while j + 1 < len(cum) and cum[j + 1] < s:
        j += 1
    j = min(j, len(nodes) - 2)
    seg = max(cum[j + 1] - cum[j], 1e-9)
    t = (s - cum[j]) / seg
    a, b = nodes[j], nodes[j + 1]
    return [
        a[0] + t * (b[0] - a[0]),
        a[1] + t * (b[1] - a[1]),
        a[2] + t * (b[2] - a[2]),
        a[3] + t * (b[3] - a[3]),
    ]


def _slice_nodes(
    nodes: list[list[float]], s0: float, s1: float
) -> list[list[float]]:
    if s1 - s0 < 0.5 or len(nodes) < 2:
        return []
    cum, _ = _resample_by_s(nodes)
    out = [_sample_at_s(cum, nodes, s0)]
    for i, s in enumerate(cum):
        if s0 < s < s1:
            out.append(list(nodes[i]))
    out.append(_sample_at_s(cum, nodes, s1))
    # drop near-duplicates
    cleaned = [out[0]]
    for p in out[1:]:
        if math.hypot(p[0] - cleaned[-1][0], p[1] - cleaned[-1][1]) > 0.05:
            cleaned.append(p)
    return cleaned if len(cleaned) >= 2 else []


def _dashed_runs(
    nodes: list[list[float]], dash_m: float, gap_m: float
) -> list[list[list[float]]]:
    total = _polyline_length(nodes)
    if total < dash_m:
        return [nodes] if len(nodes) >= 2 else []
    runs: list[list[list[float]]] = []
    s = 0.0
    paint = True
    while s < total - 0.25:
        if paint:
            s1 = min(total, s + dash_m)
            chunk = _slice_nodes(nodes, s, s1)
            if len(chunk) >= 2:
                runs.append(chunk)
            s = s1
        else:
            s = min(total, s + gap_m)
        paint = not paint
    return runs


def _load_gallery_corridors(proc: Path, cfg: dict) -> list[tuple[list[tuple[float, float]], float]]:
    if not cfg["clip_galleries"]:
        return []
    cl_path = proc / "galleries_centerlines.json"
    if not cl_path.is_file():
        return []
    try:
        data = json.loads(cl_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    stop = max(0.0, cfg["stop_before_gallery_m"])
    pad = max(0.0, cfg["gallery_clip_pad_m"])
    corridors: list[tuple[list[tuple[float, float]], float]] = []
    for g in data.get("galleries") or []:
        nodes = g.get("nodes") or []
        if len(nodes) < 2:
            continue
        portal = g.get("portal_s")
        if portal and len(portal) >= 2:
            s_lo, s_hi = float(portal[0]), float(portal[1])
            if s_hi < s_lo:
                s_lo, s_hi = s_hi, s_lo
            s_lo -= stop
            s_hi += stop
            band = [n for n in nodes if s_lo - 1e-6 <= float(n["s"]) <= s_hi + 1e-6]
        else:
            band = nodes
        if len(band) < 2:
            continue
        xy = [(float(n["x"]), float(n["y"])) for n in band]
        half = 0.5 * max(float(n.get("width") or 7.0) for n in band) + pad
        corridors.append((xy, half))
    return corridors


def _dist_poly(px: float, py: float, poly: list[tuple[float, float]]) -> float:
    best = float("inf")
    for i in range(len(poly) - 1):
        x0, y0 = poly[i]
        x1, y1 = poly[i + 1]
        dx, dy = x1 - x0, y1 - y0
        seg2 = dx * dx + dy * dy
        if seg2 < 1e-12:
            d = math.hypot(px - x0, py - y0)
        else:
            t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / seg2))
            d = math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))
        best = min(best, d)
    return best


def _split_outside(
    nodes: list[list[float]],
    corridors: list[tuple[list[tuple[float, float]], float]],
) -> list[list[list[float]]]:
    if not corridors:
        return [nodes] if len(nodes) >= 2 else []
    runs: list[list[list[float]]] = []
    cur: list[list[float]] = []
    for n in nodes:
        inside = any(_dist_poly(n[0], n[1], poly) <= half for poly, half in corridors)
        if inside:
            if len(cur) >= 2:
                runs.append(cur)
            cur = []
            continue
        cur.append(n)
    if len(cur) >= 2:
        runs.append(cur)
    return runs


def _ensure_line_material(user_level: Path, level_name: str, mat_name: str) -> None:
    """Minimal opaque-ish white strip material for centerlines."""
    mats_path = user_level / "art" / "road" / "main.materials.json"
    mats_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if mats_path.is_file() and mats_path.stat().st_size:
        try:
            data = json.loads(mats_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    if mat_name in data:
        return
    # Reuse asphalt albedo path if present; tint near-white for a readable line.
    asphalt_tex = f"/levels/{level_name}/art/terrains/t_asphalt_02_b.png"
    data[mat_name] = {
        "name": mat_name,
        "mapTo": mat_name,
        "class": "Material",
        "persistentId": "a11e0001-11e0-4ead-b100-0000ffffff01",
        "Stages": [
            {
                "baseColorMap": asphalt_tex,
                "baseColorFactor": [0.95, 0.95, 0.92, 1.0],
                "roughnessFactor": 0.85,
                "emissiveFactor": [0.05, 0.05, 0.04],
            },
            {},
            {},
            {},
        ],
        "alphaRef": 0,
        "castShadows": False,
        "materialTag0": "RoadAndPath",
        "materialTag1": "beamng",
        "version": 1.5,
    }
    mats_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Added DecalRoad material {mat_name} -> {mats_path}")


def _decal_entry(
    *,
    name: str,
    nodes: list[list[float]],
    material: str,
    texture_length: float,
    render_priority: int,
    drivability: float,
    cfg: dict,
    decal_bias: float | None = None,
    auto_lanes: bool = True,
) -> dict:
    fade = cfg["start_end_fade"]
    entry = {
        "name": name,
        "class": "DecalRoad",
        "__parent": "roads",
        "material": material,
        "textureLength": round(texture_length, 3),
        "renderPriority": int(render_priority),
        "drivability": float(drivability),
        "autoLanes": bool(auto_lanes) and drivability > 0,
        "oneWay": False,
        "improvedSpline": bool(cfg["improved_spline"]),
        "smoothness": float(cfg["smoothness"]),
        "detail": float(cfg["detail"]),
        "decalBias": float(decal_bias if decal_bias is not None else cfg["decal_bias"]),
        "overObjects": bool(cfg["over_objects"]),
        "startEndFade": [float(fade[0]), float(fade[1])],
        "nodes": [
            [round(n[0], 3), round(n[1], 3), round(n[2], 3), round(n[3], 3)] for n in nodes
        ],
    }
    if drivability <= 0:
        entry["hiddenInNavi"] = True
        entry["autoJunction"] = False
    else:
        entry["autoJunction"] = True
    return entry


def build_entries(roads: list[dict], cfg: dict, corridors) -> list[dict]:
    entries: list[dict] = []
    hwy_ok = set(cfg["highways"])
    w_scale = max(0.1, cfg["width_scale"])
    cl = cfg["centerline"]
    n_asphalt = n_line = 0

    for ri, road in enumerate(roads):
        hwy = str(road.get("highway") or "").lower()
        if hwy_ok and hwy not in hwy_ok:
            continue
        raw_nodes = []
        for p in road.get("pts") or road.get("nodes") or []:
            if len(p) < 4:
                continue
            raw_nodes.append(
                [float(p[0]), float(p[1]), float(p[2]), float(p[3]) * w_scale]
            )
        if len(raw_nodes) < 2:
            continue
        if _polyline_length(raw_nodes) < cfg["min_length_m"]:
            continue

        runs = _split_outside(raw_nodes, corridors)
        slug = bb._slug(str(road.get("name") or hwy or "road"))
        oid = road.get("id") or road.get("osm_id") or ri

        for run_i, run in enumerate(runs):
            if len(run) < 2:
                continue
            name = f"decal_{slug}_{oid}"
            if len(runs) > 1:
                name += f"_r{run_i}"
            entries.append(
                _decal_entry(
                    name=name,
                    nodes=run,
                    material=cfg["material"],
                    texture_length=cfg["texture_length"],
                    render_priority=cfg["render_priority"],
                    drivability=cfg["drivability"],
                    cfg=cfg,
                    auto_lanes=True,
                )
            )
            n_asphalt += 1

            if not cl["enabled"] or cl["style"] in ("none", "off", "false"):
                continue
            line_w = max(0.05, float(cl["width_m"]))
            line_nodes = [[n[0], n[1], n[2], line_w] for n in run]
            style = cl["style"]
            if style == "solid":
                chunks = [line_nodes]
            elif style == "dashed":
                chunks = _dashed_runs(line_nodes, float(cl["dash_m"]), float(cl["gap_m"]))
            else:
                chunks = [line_nodes]

            for ci, chunk in enumerate(chunks):
                lname = f"line_{slug}_{oid}"
                if len(runs) > 1:
                    lname += f"_r{run_i}"
                if len(chunks) > 1:
                    lname += f"_d{ci}"
                entries.append(
                    _decal_entry(
                        name=lname,
                        nodes=chunk,
                        material=cl["material"],
                        texture_length=cl["texture_length"],
                        render_priority=cl["render_priority"],
                        drivability=-1,
                        cfg=cfg,
                        decal_bias=cl["decal_bias"],
                        auto_lanes=False,
                    )
                )
                n_line += 1

    print(
        f"DecalRoads: asphalt={n_asphalt} centerline={n_line} "
        f"style={cl.get('style')} clip_corridors={len(corridors)}"
    )
    return entries


def write_level(level_name: str, entries: list[dict], cfg: dict) -> Path | None:
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        print(f"Level folder missing: {user_level}")
        return None
    _ensure_line_material(user_level, level_name, cfg["centerline"]["material"])
    # Ensure asphalt exists (bridges may already have written it)
    bb.ensure_meshroad_materials(
        user_level, level_name, {"top": cfg["material"], "texture_length": cfg["texture_length"]}
    )

    group_dir = user_level / "main" / "MissionGroup" / "level_objects" / "roads"
    group_dir.mkdir(parents=True, exist_ok=True)
    items_path = group_dir / "items.level.json"
    with items_path.open("w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")

    lo_items = user_level / "main" / "MissionGroup" / "level_objects" / "items.level.json"
    lines = []
    if lo_items.is_file() and lo_items.stat().st_size:
        lines = [ln for ln in lo_items.read_text(encoding="utf-8").splitlines() if ln.strip()]
    names = set()
    for ln in lines:
        try:
            names.add(json.loads(ln).get("name"))
        except json.JSONDecodeError:
            pass
    if "roads" not in names:
        lines.append(
            json.dumps(
                {"name": "roads", "class": "SimGroup", "__parent": "level_objects", "enabled": "1"},
                separators=(",", ":"),
            )
        )
        lo_items.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("Registered SimGroup roads under level_objects")
    return items_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--centerline",
        choices=("solid", "dashed", "none"),
        default=None,
        help="Override centerline style",
    )
    args = ap.parse_args()

    site = load_site()
    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "").strip()
    if not level_name:
        raise SystemExit("beamng.level_name missing")
    cfg = _cfg(bng)
    if args.centerline is not None:
        cfg["centerline"]["style"] = args.centerline
        cfg["centerline"]["enabled"] = args.centerline != "none"
    if not cfg["enabled"]:
        raise SystemExit("decal_roads.enabled is false")

    proc = processed_dir(site)
    roads = bb.load_road_polylines(proc)
    if not roads:
        raise SystemExit(f"Missing {proc / 'roads_beamng.json'}")

    corridors = _load_gallery_corridors(proc, cfg)
    entries = build_entries(roads, cfg, corridors)
    out = proc / "decal_roads_items.level.json"
    with out.open("w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")
    print(f"Wrote {out} ({len(entries)} objects)")

    injected = write_level(level_name, entries, cfg)
    if injected:
        print(f"Injected: {injected}")
        print("Reload the level in BeamNG (DecalRoads regenerate on load).")


if __name__ == "__main__":
    main()
