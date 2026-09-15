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
        "texture_length": float(defaults.get("texture_length") or 6.0),
        "detail_scale": float(defaults.get("detail_scale") or 4.0),
        "render_priority": int(defaults.get("render_priority") or 10),
        "drivability": float(defaults.get("drivability") if defaults.get("drivability") is not None else 1.0),
        "improved_spline": bool(defaults.get("improved_spline", True)),
        "smoothness": float(defaults.get("smoothness") if defaults.get("smoothness") is not None else 0.5),
        "detail": float(defaults.get("detail") if defaults.get("detail") is not None else 0.1),
        "decal_bias": float(defaults.get("decal_bias") if defaults.get("decal_bias") is not None else 0.002),
        "z_bias": float(defaults.get("z_bias") if defaults.get("z_bias") is not None else 0.0),
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
        # Merge OSM way stubs that touch end-to-end (degree-2 only) so short
        # connectors below min_length_m do not leave asphalt gaps.
        "stitch_abutting": bool(defaults.get("stitch_abutting", True)),
        "stitch_tol_m": float(
            defaults.get("stitch_tol_m")
            if defaults.get("stitch_tol_m") is not None
            else 1.25
        ),
        # Cap chord length before DecalRoad (OSM often has 50–80 m straights into
        # curves; improvedSpline then tears mid-ribbon). 0 = no densify.
        "densify_max_step_m": float(
            defaults.get("densify_max_step_m")
            if defaults.get("densify_max_step_m") is not None
            else 12.0
        ),
        # Resample node Z from latest heightmap after densify (OSM/DGM Z drifts).
        # Only raise nodes that sit below the heightmap (never pull bridge decks down).
        "snap_to_heightmap": bool(defaults.get("snap_to_heightmap", True)),
        "z_lift_m": float(
            defaults.get("z_lift_m") if defaults.get("z_lift_m") is not None else 0.03
        ),
        "snap_max_raise_m": float(
            defaults.get("snap_max_raise_m")
            if defaults.get("snap_max_raise_m") is not None
            else 2.0
        ),
        # Smooth heightmap under carriageway so DecalRoad can capture clean tris
        # (kills DGM noise / side-hill z-fight). Needs terrainPreset re-import.
        "road_bed_conform": bool(defaults.get("road_bed_conform", False)),
        "road_bed_pad_m": float(defaults.get("road_bed_pad_m") or 0.5),
        "road_bed_falloff_m": float(defaults.get("road_bed_falloff_m") or 2.0),
        "road_bed_sink_m": float(
            defaults.get("road_bed_sink_m")
            if defaults.get("road_bed_sink_m") is not None
            else 0.05
        ),
        "road_bed_max_delta_m": float(
            defaults.get("road_bed_max_delta_m")
            if defaults.get("road_bed_max_delta_m") is not None
            else 1.25
        ),
        # Split clamp: raise fixes dips; cut carves high bank sawteeth into the bed.
        # If unset, both fall back to road_bed_max_delta_m.
        "road_bed_max_raise_m": (
            float(defaults["road_bed_max_raise_m"])
            if defaults.get("road_bed_max_raise_m") is not None
            else None
        ),
        "road_bed_max_cut_m": (
            float(defaults["road_bed_max_cut_m"])
            if defaults.get("road_bed_max_cut_m") is not None
            else None
        ),
        "road_bed_smooth_m": float(defaults.get("road_bed_smooth_m") or 12.0),
        # Optional per-corridor overrides: match GIP objectid and/or OSM road id.
        "road_bed_items": list(defaults.get("road_bed_items") or []),
        # BeamNG often drops asphalt geometry on very long DecalRoads; split runs.
        "max_decal_length_m": float(
            defaults.get("max_decal_length_m")
            if defaults.get("max_decal_length_m") is not None
            else 150.0
        ),
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


def _densify_nodes(nodes: list[list[float]], max_step_m: float) -> list[list[float]]:
    """Insert vertices so no chord exceeds max_step_m (xy). Keeps xyzw linear."""
    if max_step_m <= 0 or len(nodes) < 2:
        return nodes
    out: list[list[float]] = [list(nodes[0])]
    for a, b in zip(nodes, nodes[1:]):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        n = max(1, int(math.ceil(seg / max_step_m)))
        for k in range(1, n + 1):
            t = k / n
            out.append(
                [
                    a[0] + t * (b[0] - a[0]),
                    a[1] + t * (b[1] - a[1]),
                    a[2] + t * (b[2] - a[2]),
                    a[3] + t * (b[3] - a[3]),
                ]
            )
    return out


def _load_decal_heightmap(proc: Path, size: int):
    """Prefer latest bake (water → bridge conform → pristine). Returns (z_at, label) or None."""
    from PIL import Image
    import numpy as np

    meta_path = proc / "heightmap_meta.json"
    if not meta_path.is_file():
        return None
    max_h = float(json.loads(meta_path.read_text(encoding="utf-8"))["max_height_m"])
    candidates = [
        proc / f"heightmap_{size}_water.png",
        proc / f"heightmap_{size}_bridge_conform.png",
        proc / f"heightmap_{size}.png",
    ]
    hm_path = next((p for p in candidates if p.is_file()), None)
    if hm_path is None:
        return None
    hm = np.asarray(Image.open(hm_path))
    if hm.dtype != np.uint16:
        hm = hm.astype(np.uint16)
    n = int(hm.shape[0])

    def z_at(bx: float, by: float) -> float:
        # Bilinear, BeamNG SW origin (same convention as guardrails).
        px = max(0.0, min(float(n - 1), bx))
        py = max(0.0, min(float(n - 1), (n - 1) - by))
        x0 = int(math.floor(px))
        y0 = int(math.floor(py))
        x1 = min(x0 + 1, n - 1)
        y1 = min(y0 + 1, n - 1)
        tx, ty = px - x0, py - y0
        v00 = float(hm[y0, x0])
        v10 = float(hm[y0, x1])
        v01 = float(hm[y1, x0])
        v11 = float(hm[y1, x1])
        v = (
            v00 * (1 - tx) * (1 - ty)
            + v10 * tx * (1 - ty)
            + v01 * (1 - tx) * ty
            + v11 * tx * ty
        )
        return (v / 65535.0) * max_h

    return z_at, hm_path.name


def _snap_nodes_z(
    nodes: list[list[float]],
    z_at,
    lift_m: float,
    max_raise_m: float,
) -> tuple[list[list[float]], float, float, int]:
    """Raise Z up to heightmap+lift when buried; never lower (bridge decks).

    Returns (nodes, min_dz, max_dz, n_raised).
    """
    out: list[list[float]] = []
    min_dz = 0.0
    max_dz = 0.0
    n_raised = 0
    max_raise = max(0.0, float(max_raise_m))
    for i, n in enumerate(nodes):
        z_old = float(n[2])
        z_hm = float(z_at(n[0], n[1])) + lift_m
        if z_old < z_hm - 1e-4:
            target = z_hm
            if max_raise > 0:
                target = min(target, z_old + max_raise)
            z_new = target
            n_raised += 1
        else:
            z_new = z_old
        dz = z_new - z_old
        if i == 0:
            min_dz = max_dz = dz
        else:
            min_dz = min(min_dz, dz)
            max_dz = max(max_dz, dz)
        out.append([n[0], n[1], z_new, n[3]])
    return out, min_dz, max_dz, n_raised


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


def _xy(n) -> tuple[float, float]:
    return float(n[0]), float(n[1])


def stitch_abutting_roads(roads: list[dict], cfg: dict) -> list[dict]:
    """Merge degree-2 end-to-end OSM fragments of the same highway class.

    OSM often inserts 3–6 m connector ways between longer primary segments.
    Those fall under ``min_length_m`` and leave visible asphalt gaps (e.g. GIP
    2169 / Fernpass Rast). Only merge when each endpoint has a unique partner
    within ``stitch_tol_m`` (skips T-junctions / crossings).
    """
    if not cfg.get("stitch_abutting", True):
        return roads
    tol = max(0.0, float(cfg.get("stitch_tol_m") or 0.0))
    if tol <= 0:
        return roads
    hwy_ok = {str(h).lower() for h in (cfg.get("highways") or [])}
    tol2 = tol * tol

    # Work list: mutable polylines eligible for asphalt.
    work: list[dict] = []
    passthrough: list[dict] = []
    for r in roads:
        hwy = str(r.get("highway") or "").lower()
        pts = list(r.get("pts") or r.get("nodes") or [])
        if hwy_ok and hwy not in hwy_ok:
            passthrough.append(r)
            continue
        if len(pts) < 2:
            passthrough.append(r)
            continue
        work.append(
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "highway": r.get("highway"),
                "pts": [[float(p[0]), float(p[1]), float(p[2]), float(p[3])] for p in pts],
                "osm_ids": [r.get("id")],
            }
        )

    def ends(poly: dict) -> list[tuple[str, tuple[float, float]]]:
        pts = poly["pts"]
        return [("s", _xy(pts[0])), ("e", _xy(pts[-1]))]

    merges = 0
    changed = True
    while changed:
        changed = False
        # endpoint -> list of (poly_idx, which)
        buckets: dict[tuple[int, int], list[tuple[int, str]]] = {}
        coords: list[tuple[int, str, float, float]] = []
        for pi, poly in enumerate(work):
            for which, (x, y) in ends(poly):
                coords.append((pi, which, x, y))
                key = (int(round(x / tol)), int(round(y / tol)))
                buckets.setdefault(key, []).append((pi, which))

        # For each endpoint, collect unique partners within tol
        partners: dict[tuple[int, str], tuple[int, str]] = {}
        degree: dict[tuple[int, str], int] = {}
        for pi, which, x, y in coords:
            near: list[tuple[int, str]] = []
            gx, gy = int(round(x / tol)), int(round(y / tol))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for qj, qw in buckets.get((gx + dx, gy + dy), []):
                        if qj == pi:
                            continue
                        qx, qy = _xy(
                            work[qj]["pts"][0] if qw == "s" else work[qj]["pts"][-1]
                        )
                        if (x - qx) * (x - qx) + (y - qy) * (y - qy) <= tol2:
                            near.append((qj, qw))
            # unique by poly index
            uniq = list({n[0]: n for n in near}.values())
            degree[(pi, which)] = len(uniq)
            if len(uniq) == 1:
                partners[(pi, which)] = uniq[0]

        used: set[int] = set()
        new_work: list[dict] = []
        for pi, poly in enumerate(work):
            if pi in used:
                continue
            # try merge at start or end with mutual unique partner
            merged_any = False
            for which in ("e", "s"):
                key = (pi, which)
                if key not in partners:
                    continue
                qj, qw = partners[key]
                if qj in used or pi in used:
                    continue
                # reciprocal + degree-2 both ends
                if partners.get((qj, qw)) != (pi, which):
                    continue
                if degree.get(key, 0) != 1 or degree.get((qj, qw), 0) != 1:
                    continue
                other = work[qj]
                if str(poly.get("highway") or "").lower() != str(
                    other.get("highway") or ""
                ).lower():
                    continue
                a = poly["pts"]
                b = other["pts"]
                # Orient so a end touches b start
                if which == "e" and qw == "s":
                    chain = a + b[1:]
                elif which == "e" and qw == "e":
                    chain = a + list(reversed(b))[1:]
                elif which == "s" and qw == "e":
                    chain = b + a[1:]
                elif which == "s" and qw == "s":
                    chain = list(reversed(b)) + a[1:]
                else:
                    continue
                if len(chain) < 2:
                    continue
                used.add(pi)
                used.add(qj)
                new_work.append(
                    {
                        "id": poly.get("id") or other.get("id"),
                        "name": poly.get("name") or other.get("name"),
                        "highway": poly.get("highway"),
                        "pts": chain,
                        "osm_ids": list(poly.get("osm_ids") or [])
                        + list(other.get("osm_ids") or []),
                    }
                )
                merges += 1
                merged_any = True
                changed = True
                break
            if not merged_any and pi not in used:
                new_work.append(poly)
                used.add(pi)
        work = new_work

    out = passthrough + work
    if merges:
        print(
            f"Decal stitch: {merges} abutting merge(s) "
            f"(tol={tol}m, asphalt roads now {len(work)})"
        )
    return out


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


def _gallery_keeps_surface_road(gal: dict) -> bool:
    """Underpass / surface-first roofs: DecalRoad above must not be clipped away."""
    mode = str(gal.get("terrain_roof") or "rock").lower().strip()
    return mode in ("keep_asphalt", "under_asphalt", "surface_first", "asphalt")


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
    skipped_keep = 0
    for g in data.get("galleries") or []:
        if _gallery_keeps_surface_road(g):
            skipped_keep += 1
            continue
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
    if skipped_keep:
        print(
            f"Decal gallery-clip: skipped {skipped_keep} keep_asphalt "
            f"(surface DecalRoad preserved); {len(corridors)} corridors active"
        )
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
        "zBias": float(cfg.get("z_bias") or 0.0),
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


def _split_by_length(
    nodes: list[list[float]], max_len_m: float
) -> list[list[list[float]]]:
    """Split polyline into chunks of roughly max_len_m; shared endpoint, no gap."""
    if max_len_m <= 0 or len(nodes) < 2:
        return [nodes]
    total = _polyline_length(nodes)
    if total <= max_len_m + 0.5:
        return [nodes]
    cum, _ = _resample_by_s(nodes)
    chunks: list[list[list[float]]] = []
    s0 = 0.0
    while s0 < total - 0.5:
        s1 = min(total, s0 + max_len_m)
        # Avoid a tiny leftover stub: merge into previous window.
        if total - s1 < max(8.0, 0.25 * max_len_m) and s1 < total:
            s1 = total
        piece = _slice_nodes(nodes, s0, s1)
        if len(piece) >= 2:
            chunks.append(piece)
        if s1 >= total - 1e-6:
            break
        s0 = s1
    return chunks if chunks else [nodes]


def build_entries(roads: list[dict], cfg: dict, corridors, z_at=None) -> list[dict]:
    entries: list[dict] = []
    hwy_ok = set(cfg["highways"])
    w_scale = max(0.1, cfg["width_scale"])
    cl = cfg["centerline"]
    n_asphalt = n_line = 0
    snap = bool(cfg.get("snap_to_heightmap")) and z_at is not None
    lift = float(cfg.get("z_lift_m") or 0.0)
    max_raise = float(cfg.get("snap_max_raise_m") or 0.0)
    snap_min = snap_max = 0.0
    snap_n = 0
    raised_n = 0

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

        densified = _densify_nodes(raw_nodes, float(cfg.get("densify_max_step_m") or 0.0))
        if snap:
            densified, dmin, dmax, nr = _snap_nodes_z(
                densified, z_at, lift, max_raise
            )
            raised_n += nr
            if snap_n == 0:
                snap_min, snap_max = dmin, dmax
            else:
                snap_min = min(snap_min, dmin)
                snap_max = max(snap_max, dmax)
            snap_n += len(densified)
        runs = _split_outside(densified, corridors)
        slug = bb._slug(str(road.get("name") or hwy or "road"))
        oid = road.get("id") or road.get("osm_id") or ri
        max_len = float(cfg.get("max_decal_length_m") or 0.0)

        pieces: list[tuple[int, int, list]] = []
        for run_i, run in enumerate(runs):
            if len(run) < 2:
                continue
            segs = _split_by_length(run, max_len)
            for seg_i, seg in enumerate(segs):
                pieces.append((run_i, seg_i, seg))

        multi_run = len(runs) > 1
        for run_i, seg_i, run in pieces:
            if len(run) < 2:
                continue
            name = f"decal_{slug}_{oid}"
            if multi_run:
                name += f"_r{run_i}"
            if max_len > 0 and (seg_i > 0 or len([p for p in pieces if p[0] == run_i]) > 1):
                name += f"_s{seg_i}"
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
                if multi_run:
                    lname += f"_r{run_i}"
                if max_len > 0 and (seg_i > 0 or len([p for p in pieces if p[0] == run_i]) > 1):
                    lname += f"_s{seg_i}"
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
        + (
            f" max_decal_length_m={cfg.get('max_decal_length_m')}"
            if cfg.get("max_decal_length_m")
            else ""
        )
    )
    if snap and snap_n:
        print(
            f"  snap_to_heightmap: nodes={snap_n} raised={raised_n} "
            f"dz=[{snap_min:+.3f},{snap_max:+.3f}]m lift={lift:.3f}m "
            f"max_raise={max_raise:.2f}m"
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
        user_level,
        level_name,
        {
            "top": cfg["material"],
            "texture_length": cfg["texture_length"],
            "detail_scale": cfg.get("detail_scale") or 4.0,
        },
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


def _elev_z_at(elev, size: int, extent: float, max_h: float):
    import numpy as np

    n = int(elev.shape[0])
    is_u16 = elev.dtype == np.uint16 or float(np.nanmax(elev)) > max_h * 1.5

    def z_at(bx: float, by: float) -> float:
        px = max(0.0, min(float(n - 1), bx / extent * (n - 1)))
        py = max(0.0, min(float(n - 1), (1.0 - by / extent) * (n - 1)))
        x0 = int(math.floor(px))
        y0 = int(math.floor(py))
        x1 = min(x0 + 1, n - 1)
        y1 = min(y0 + 1, n - 1)
        tx, ty = px - x0, py - y0
        v = (
            float(elev[y0, x0]) * (1 - tx) * (1 - ty)
            + float(elev[y0, x1]) * tx * (1 - ty)
            + float(elev[y1, x0]) * (1 - tx) * ty
            + float(elev[y1, x1]) * tx * ty
        )
        if is_u16:
            return (v / 65535.0) * max_h
        return v

    return z_at


def _smooth_polyline_z(nodes: list[dict], window_m: float) -> list[dict]:
    if len(nodes) < 2 or window_m <= 0:
        return nodes
    cum = [0.0]
    for a, b in zip(nodes, nodes[1:]):
        cum.append(cum[-1] + math.hypot(b["x"] - a["x"], b["y"] - a["y"]))
    half = 0.5 * window_m
    out = []
    for i, n in enumerate(nodes):
        s0, s1 = cum[i] - half, cum[i] + half
        num = den = 0.0
        for j, m in enumerate(nodes):
            if s0 <= cum[j] <= s1:
                num += float(m["z"])
                den += 1.0
        z = num / den if den else float(n["z"])
        out.append({**n, "z": z})
    return out


def _stamp_disk_accum(
    value_acc,
    weight_acc,
    *,
    cx: float,
    cy: float,
    radius_px: float,
    soft_px: float,
    z: float,
    raise_lim=None,
    cut_lim=None,
    max_raise_m: float | None = None,
    max_cut_m: float | None = None,
) -> None:
    """Add a soft disk into shared value/weight accumulators (ROI only)."""
    import numpy as np

    outer = radius_px + max(soft_px, 0.0)
    if outer < 0.5:
        return
    size = value_acc.shape[0]
    c0 = max(0, int(math.floor(cx - outer)))
    c1 = min(size - 1, int(math.ceil(cx + outer)))
    r0 = max(0, int(math.floor(cy - outer)))
    r1 = min(size - 1, int(math.ceil(cy + outer)))
    if c1 < c0 or r1 < r0:
        return
    yy, xx = np.ogrid[r0 : r1 + 1, c0 : c1 + 1]
    dist = np.hypot(xx - cx, yy - cy)
    w = np.zeros_like(dist, dtype=np.float64)
    core = dist <= radius_px
    w[core] = 1.0
    if soft_px > 1e-6:
        ring = (~core) & (dist <= outer)
        w[ring] = 1.0 - (dist[ring] - radius_px) / soft_px
        np.clip(w, 0.0, 1.0, out=w)
    value_acc[r0 : r1 + 1, c0 : c1 + 1] += z * w
    weight_acc[r0 : r1 + 1, c0 : c1 + 1] += w
    hit = w > 1e-6
    if not np.any(hit):
        return
    if raise_lim is not None and max_raise_m is not None:
        roi = raise_lim[r0 : r1 + 1, c0 : c1 + 1]
        roi[hit] = np.maximum(roi[hit], float(max_raise_m))
    if cut_lim is not None and max_cut_m is not None:
        roi = cut_lim[r0 : r1 + 1, c0 : c1 + 1]
        roi[hit] = np.maximum(roi[hit], float(max_cut_m))


def _gip_beamng_polyline(site: dict, objectid: int) -> list[tuple[float, float]]:
    """BeamNG XY polyline for a GIP OBJECTID (empty if missing)."""
    from pyproj import Transformer

    sc = __import__("site_coords", fromlist=["SiteCoords"]).SiteCoords(site)
    path = bb.find_gip_geojson(site)
    data = json.loads(path.read_text(encoding="utf-8"))
    to_site = Transformer.from_crs("EPSG:4326", sc.crs, always_xy=True)
    for f in data.get("features") or []:
        props = f.get("properties") or {}
        if int(props.get("OBJECTID") or -1) != int(objectid):
            continue
        raw: list = []
        bb._walk_coords((f.get("geometry") or {}).get("coordinates"), raw)
        xy: list[tuple[float, float]] = []
        for lon, lat, *_ in raw:
            x, y = to_site.transform(float(lon), float(lat))
            bx, by = sc.crs_to_beamng(x, y)
            xy.append((bx, by))
        return xy
    return []


def _dist_to_poly_xy(
    px: float, py: float, poly: list[tuple[float, float]]
) -> float:
    best = float("inf")
    for a, b in zip(poly, poly[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        seg2 = dx * dx + dy * dy
        if seg2 < 1e-12:
            d = math.hypot(px - a[0], py - a[1])
        else:
            t = max(0.0, min(1.0, ((px - a[0]) * dx + (py - a[1]) * dy) / seg2))
            d = math.hypot(px - (a[0] + t * dx), py - (a[1] + t * dy))
        best = min(best, d)
    return best


def _road_bed_params_for_road(
    road: dict,
    cfg: dict,
    *,
    gip_polys: dict[int, list[tuple[float, float]]],
) -> dict:
    """Resolve pad/falloff/sink/max_raise/max_cut/smooth for one OSM road."""
    max_delta = float(
        cfg.get("road_bed_max_delta_m")
        if cfg.get("road_bed_max_delta_m") is not None
        else 1.25
    )
    max_raise = cfg.get("road_bed_max_raise_m")
    max_cut = cfg.get("road_bed_max_cut_m")
    base = {
        "pad_m": float(cfg.get("road_bed_pad_m") or 0.5),
        "falloff_m": float(cfg.get("road_bed_falloff_m") or 2.0),
        "sink_m": float(
            cfg.get("road_bed_sink_m") if cfg.get("road_bed_sink_m") is not None else 0.05
        ),
        "max_delta_m": max_delta,
        "max_raise_m": float(max_raise) if max_raise is not None else max_delta,
        "max_cut_m": float(max_cut) if max_cut is not None else max_delta,
        "smooth_m": float(cfg.get("road_bed_smooth_m") or 12.0),
        "override": None,
    }
    items = cfg.get("road_bed_items") or []
    if not items:
        return base
    rid = str(road.get("id") or road.get("osm_id") or "")
    pts = road.get("pts") or road.get("nodes") or []
    for item in items:
        if not isinstance(item, dict):
            continue
        match = item.get("match") or {}
        hit = False
        ids = match.get("road_id")
        if ids is not None:
            if not isinstance(ids, (list, tuple)):
                ids = [ids]
            if any(str(i) == rid for i in ids):
                hit = True
        oids = match.get("objectid")
        if not hit and oids is not None:
            if not isinstance(oids, (list, tuple)):
                oids = [oids]
            for oid in oids:
                poly = gip_polys.get(int(oid))
                if not poly or len(poly) < 2 or len(pts) < 2:
                    continue
                near = sum(
                    1
                    for p in pts
                    if _dist_to_poly_xy(float(p[0]), float(p[1]), poly) < 25.0
                )
                if near >= max(2, len(pts) // 3):
                    hit = True
                    break
        if not hit:
            continue
        out = dict(base)
        out["override"] = match
        for key in ("pad_m", "falloff_m", "sink_m", "max_delta_m", "smooth_m"):
            if item.get(key) is not None:
                out[key] = float(item[key])
        # After optional max_delta override, resolve raise/cut (item wins, else delta).
        if item.get("max_raise_m") is not None:
            out["max_raise_m"] = float(item["max_raise_m"])
        elif item.get("max_delta_m") is not None:
            out["max_raise_m"] = float(item["max_delta_m"])
        if item.get("max_cut_m") is not None:
            out["max_cut_m"] = float(item["max_cut_m"])
        elif item.get("max_delta_m") is not None:
            out["max_cut_m"] = float(item["max_delta_m"])
        return out
    return base


def conform_road_bed_heightmap(
    roads: list[dict],
    cfg: dict,
    *,
    elev,
    size: int,
    extent: float,
    max_h: float,
    site: dict | None = None,
):
    """Flatten heightmap under all roads via one shared value/opacity raster.

    1) Project every road top-down into accumulators (target Z = value, blend = weight).
    2) opacity from PIL stroke+blur; value = value_acc/weight_acc.
    3) Single composite onto the heightmap (per-pixel max_delta clamp).
    """
    import time

    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    if not cfg.get("road_bed_conform"):
        return elev, {"enabled": False}

    t0 = time.perf_counter()
    out = elev.astype(np.float64).copy()
    if float(np.nanmax(out)) > max_h * 1.5:
        out = out / 65535.0 * max_h
    base = out.copy()
    z_at = _elev_z_at(base, size, extent, max_h)

    hwy_ok = set(cfg["highways"])
    w_scale = max(0.1, cfg["width_scale"])
    densify = float(cfg.get("densify_max_step_m") or 0.0)
    stamp_step = min(densify if densify > 0 else 8.0, 4.0)
    pad0 = float(cfg.get("road_bed_pad_m") or 0.5)
    falloff0 = float(cfg.get("road_bed_falloff_m") or 2.0)
    max_delta0 = float(
        cfg.get("road_bed_max_delta_m")
        if cfg.get("road_bed_max_delta_m") is not None
        else 1.25
    )
    max_raise0 = cfg.get("road_bed_max_raise_m")
    max_cut0 = cfg.get("road_bed_max_cut_m")
    max_raise0 = float(max_raise0) if max_raise0 is not None else max_delta0
    max_cut0 = float(max_cut0) if max_cut0 is not None else max_delta0
    mpp = extent / max(size - 1, 1)

    gip_polys: dict[int, list[tuple[float, float]]] = {}
    if site is not None:
        for item in cfg.get("road_bed_items") or []:
            match = (item or {}).get("match") or {}
            oids = match.get("objectid")
            if oids is None:
                continue
            if not isinstance(oids, (list, tuple)):
                oids = [oids]
            for oid in oids:
                oid_i = int(oid)
                if oid_i not in gip_polys:
                    gip_polys[oid_i] = _gip_beamng_polyline(site, oid_i)

    value_acc = np.zeros((size, size), dtype=np.float64)
    weight_acc = np.zeros((size, size), dtype=np.float64)
    raise_lim = np.zeros((size, size), dtype=np.float64)
    cut_lim = np.zeros((size, size), dtype=np.float64)
    hard_img = Image.new("L", (size, size), 0)
    soft_img = Image.new("L", (size, size), 0)
    draw_h = ImageDraw.Draw(hard_img)
    draw_s = ImageDraw.Draw(soft_img)

    n_roads = 0
    n_stamps = 0
    n_over = 0
    max_falloff_used = falloff0
    for road in roads:
        hwy = str(road.get("highway") or "").lower()
        if hwy_ok and hwy not in hwy_ok:
            continue
        raw = []
        for p in road.get("pts") or road.get("nodes") or []:
            if len(p) < 4:
                continue
            raw.append([float(p[0]), float(p[1]), float(p[2]), float(p[3]) * w_scale])
        if len(raw) < 2 or _polyline_length(raw) < cfg["min_length_m"]:
            continue
        bp = _road_bed_params_for_road(road, cfg, gip_polys=gip_polys)
        pad = float(bp["pad_m"])
        falloff = float(bp["falloff_m"])
        sink = float(bp["sink_m"])
        max_raise = float(bp["max_raise_m"])
        max_cut = float(bp["max_cut_m"])
        smooth_m = float(bp["smooth_m"])
        max_falloff_used = max(max_falloff_used, falloff)
        if bp.get("override"):
            n_over += 1

        densified = _densify_nodes(raw, stamp_step)
        nodes = [
            {
                "x": n[0],
                "y": n[1],
                "z": float(z_at(n[0], n[1])),
                "width": n[3],
            }
            for n in densified
        ]
        nodes = _smooth_polyline_z(nodes, smooth_m)
        n_roads += 1

        half_ref = 0.5 * max(float(n["width"]) for n in nodes) + pad
        core_px = max(2, int(round((2.0 * half_ref) / mpp)))
        soft_px = max(core_px + 1, int(round((2.0 * (half_ref + falloff)) / mpp)))
        pts = []
        for n in nodes:
            px, py = bb._to_px_beamng(float(n["x"]), float(n["y"]), size, extent)
            pts.append((px, py))
        if len(pts) >= 2:
            draw_h.line(pts, fill=255, width=core_px, joint="curve")
            draw_s.line(pts, fill=255, width=soft_px, joint="curve")

        soft_r_px = falloff / mpp
        for n in nodes:
            px, py = bb._to_px_beamng(float(n["x"]), float(n["y"]), size, extent)
            rad_px = (0.5 * float(n["width"]) + pad) / mpp
            _stamp_disk_accum(
                value_acc,
                weight_acc,
                cx=px,
                cy=py,
                radius_px=rad_px,
                soft_px=soft_r_px,
                z=float(n["z"]) - sink,
                raise_lim=raise_lim,
                cut_lim=cut_lim,
                max_raise_m=max_raise,
                max_cut_m=max_cut,
            )
            n_stamps += 1

    blur_r = max(1, int(round(max_falloff_used / mpp)))
    soft_blur = soft_img.filter(ImageFilter.GaussianBlur(radius=blur_r))
    hard = np.asarray(hard_img, dtype=np.float64) / 255.0
    soft = np.asarray(soft_blur, dtype=np.float64) / 255.0
    opacity = np.maximum(hard, soft)
    np.clip(opacity, 0.0, 1.0, out=opacity)

    has_w = weight_acc > 1e-9
    value = np.zeros_like(base)
    value[has_w] = value_acc[has_w] / weight_acc[has_w]
    value[~has_w] = base[~has_w]

    # Raise = target above terrain (fill dips); cut = terrain above target (bank).
    d_raise = value - base
    d_cut = base - value
    lim_raise = np.where(raise_lim > 1e-9, raise_lim, max_raise0)
    lim_cut = np.where(cut_lim > 1e-9, cut_lim, max_cut0)
    opacity = opacity.copy()
    bad_raise = (d_raise > 1e-6) & (d_raise > lim_raise)
    bad_cut = (d_cut > 1e-6) & (d_cut > lim_cut)
    opacity[bad_raise | bad_cut] = 0.0

    out = base * (1.0 - opacity) + value * opacity
    changed = int(np.count_nonzero(np.abs(out - base) > 1e-4))
    max_abs = float(np.max(np.abs(out - base))) if changed else 0.0
    elapsed = time.perf_counter() - t0

    stats = {
        "enabled": True,
        "roads": n_roads,
        "overrides": n_over,
        "stamps": n_stamps,
        "changed": changed,
        "max_delta_m": round(max_abs, 3),
        "elapsed_s": round(elapsed, 2),
        "method": "road_bed_value_opacity_raster",
    }
    print(
        f"Road-bed conform (raster): roads={n_roads} overrides={n_over} "
        f"stamps={n_stamps} changed={changed} max_delta={max_abs:.3f}m "
        f"elapsed={elapsed:.2f}s"
    )
    return out, stats


def write_road_bed_heightmap(
    proc: Path,
    level_name: str,
    *,
    elev,
    max_height_m: float,
    size: int,
) -> Path:
    import numpy as np
    from PIL import Image
    import shutil

    u16 = np.clip(
        np.round(elev / max(max_height_m, 1e-6) * 65535.0),
        0,
        65535,
    ).astype(np.uint16)
    carved = proc / f"heightmap_{size}_road_bed.png"
    Image.fromarray(u16).save(carved)
    user_import = USER_LEVELS / level_name / "import"
    user_import.mkdir(parents=True, exist_ok=True)
    dest = user_import / f"heightmap_{size}.png"
    shutil.copy2(carved, dest)
    # Keep terrainPreset pointing at import heightmap
    preset = user_import / "terrainPreset.json"
    if preset.is_file():
        try:
            data = json.loads(preset.read_text(encoding="utf-8"))
            data["heightMapPath"] = f"/levels/{level_name}/import/heightmap_{size}.png"
            preset.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except json.JSONDecodeError:
            pass
    print(f"Synced road-bed heightmap -> {user_import}")
    print("Re-import terrainPreset.json in World Editor (heightmap changed).")
    return carved


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--centerline",
        choices=("solid", "dashed", "none"),
        default=None,
        help="Override centerline style",
    )
    ap.add_argument(
        "--skip-road-bed",
        action="store_true",
        help="Skip heightmap road-bed conform (Decal JSON only)",
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
    roads = stitch_abutting_roads(roads, cfg)

    size = int(bng.get("mask_size") or 512)
    meta_path = proc / "heightmap_meta.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        max_h = float(meta["max_height_m"])
        extent = float(meta.get("terrain_extent_m") or size)
    else:
        max_h = 256.0
        extent = float(size)

    if cfg.get("road_bed_conform") and not args.skip_road_bed:
        import numpy as np
        from PIL import Image

        candidates = [
            proc / f"heightmap_{size}_water.png",
            proc / f"heightmap_{size}_bridge_conform.png",
            proc / f"heightmap_{size}.png",
        ]
        hm_path = next((p for p in candidates if p.is_file()), None)
        if hm_path is None:
            print("road_bed_conform: no heightmap — skipped")
        else:
            elev_u16 = np.asarray(Image.open(hm_path))
            elev_m = elev_u16.astype(np.float64) / 65535.0 * max_h
            elev_m, _stats = conform_road_bed_heightmap(
                roads,
                cfg,
                elev=elev_m,
                size=size,
                extent=extent,
                max_h=max_h,
                site=site,
            )
            write_road_bed_heightmap(
                proc, level_name, elev=elev_m, max_height_m=max_h, size=size
            )
            print(f"road_bed_conform base={hm_path.name}")

    z_at = None
    if cfg.get("snap_to_heightmap"):
        road_bed = proc / f"heightmap_{size}_road_bed.png"
        if cfg.get("road_bed_conform") and road_bed.is_file():
            from PIL import Image
            import numpy as np

            hm = np.asarray(Image.open(road_bed))
            n = int(hm.shape[0])

            def z_at_rb(bx: float, by: float) -> float:
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

            z_at = z_at_rb
            print("snap_to_heightmap: using heightmap_*_road_bed.png")
        else:
            pack = _load_decal_heightmap(proc, size)
            if pack is None:
                print("snap_to_heightmap: no heightmap found — keeping roads_beamng Z")
            else:
                z_at, label = pack
                print(f"snap_to_heightmap: using {label}")

    corridors = _load_gallery_corridors(proc, cfg)
    entries = build_entries(roads, cfg, corridors, z_at=z_at)
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
