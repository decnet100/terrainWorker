"""Build BeamNG DecalRoad strips from roads_beamng.json (OSM centerlines).

Layers (configurable in site YAML ``beamng.decal_roads``):
  - carriageway: asphalt DecalRoad (drivability > 0)
  - centerline: painted line on top (drivability -1), style solid | dashed | none
  - bridge decks: after clip_bridges, new Decals on MeshRoad XYZ (overObjects)

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
from road_width import smooth_roads_widths  # noqa: E402

USER_LEVELS = bb.USER_LEVELS


def _float_band(raw, default: list[float]) -> list[float]:
    """[lo, hi] meters, or [] to disable. ``off`` / empty list = off."""
    if raw is None:
        return [float(x) for x in default]
    if raw is False or raw == [] or raw == "off" or raw == "none":
        return []
    return [float(x) for x in raw]


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
        # osm (default) | strassennetz — carriageway axis source
        "centerline_source": str(
            defaults.get("centerline_source")
            or defaults.get("source")
            or "osm"
        ).lower().strip(),
        # named = DecalRoads only for GIP pieces with STR_CODE; all still get road-bed.
        "gip_decals": str(defaults.get("gip_decals") or "named").lower().strip(),
        # Merge OSM way stubs that touch end-to-end (degree-2 only) so short
        # connectors below min_length_m do not leave asphalt gaps.
        "stitch_abutting": bool(defaults.get("stitch_abutting", True)),
        "stitch_tol_m": float(
            defaults.get("stitch_tol_m")
            if defaults.get("stitch_tol_m") is not None
            else 1.25
        ),
        # After stitch: fill short narrow OSM stubs, then soft-ramp lane steps.
        "width_fill_dip_m": float(
            defaults.get("width_fill_dip_m")
            if defaults.get("width_fill_dip_m") is not None
            else 40.0
        ),
        "width_blend_m": float(
            defaults.get("width_blend_m")
            if defaults.get("width_blend_m") is not None
            else 25.0
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
        # True: set node Z to composed (raise and lower). False: raise-only.
        "snap_follow_heightmap": bool(defaults.get("snap_follow_heightmap", False)),
        # Moving-average Z along the Decal polyline after snap (0 = off).
        "smooth_decal_z_m": float(
            defaults.get("smooth_decal_z_m")
            if defaults.get("smooth_decal_z_m") is not None
            else 0.0
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
        # Cut terrain Decals at MeshRoad abutments, then emit deck Decals.
        "clip_bridges": bool(defaults.get("clip_bridges", True)),
        "deck_decals": bool(defaults.get("deck_decals", True)),
        "deck_decal_overlap_m": float(
            defaults.get("deck_decal_overlap_m")
            if defaults.get("deck_decal_overlap_m") is not None
            else 1.0
        ),
        "bridge_clip_pad_m": float(defaults.get("bridge_clip_pad_m") or 2.0),
        # dz = z_decal - z_deck_top. Remove if adjacent and in these bands;
        # nodes inside the slab thickness are kept.
        "clip_dz_down_m": _float_band(defaults.get("clip_dz_down_m"), [-5.0, -0.2]),
        "clip_dz_up_m": _float_band(defaults.get("clip_dz_up_m"), []),
        # Keep OSM road-bed from using ditch Z under MeshRoad decks.
        "road_bed_skip_bridge_unders": bool(
            defaults.get("road_bed_skip_bridge_unders", True)
        ),
        "road_bed_bridge_pad_m": float(
            defaults.get("road_bed_bridge_pad_m")
            if defaults.get("road_bed_bridge_pad_m") is not None
            else 2.0
        ),
        # After skip: raise heightmap to MeshRoad Z so deck Decals drape.
        "road_bed_conform_bridge_decks": bool(
            defaults.get("road_bed_conform_bridge_decks", True)
        ),
        "road_bed_bridge_deck_raise_m": float(
            defaults.get("road_bed_bridge_deck_raise_m")
            if defaults.get("road_bed_bridge_deck_raise_m") is not None
            else 16.0
        ),
        "road_bed_bridge_deck_sink_m": float(
            defaults.get("road_bed_bridge_deck_sink_m")
            if defaults.get("road_bed_bridge_deck_sink_m") is not None
            else 0.03
        ),
        # After clip: lerp approach Z to deck Z over this length (0 = off).
        "abutment_blend_m": float(
            defaults.get("abutment_blend_m")
            if defaults.get("abutment_blend_m") is not None
            else 0.0
        ),
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


def _load_decal_heightmap(proc: Path, size: int, extent: float):
    """Snap Z from composed heightmap (else DGM). Returns (z_at, label) or None."""
    import heightmap_layers as hml

    try:
        elev, _max_h, label = hml.load_composed_or_dgm(proc, size)
    except FileNotFoundError:
        return None
    n = int(elev.shape[0])
    ext = float(extent) if extent else float(n)

    def z_at(bx: float, by: float) -> float:
        px = max(0.0, min(float(n - 1), bx / ext * (n - 1)))
        py = max(0.0, min(float(n - 1), (1.0 - by / ext) * (n - 1)))
        x0 = int(math.floor(px))
        y0 = int(math.floor(py))
        x1 = min(x0 + 1, n - 1)
        y1 = min(y0 + 1, n - 1)
        tx, ty = px - x0, py - y0
        return (
            float(elev[y0, x0]) * (1 - tx) * (1 - ty)
            + float(elev[y0, x1]) * tx * (1 - ty)
            + float(elev[y1, x0]) * (1 - tx) * ty
            + float(elev[y1, x1]) * tx * ty
        )

    return z_at, label


def _snap_nodes_z(
    nodes: list[list[float]],
    z_at,
    lift_m: float,
    max_raise_m: float,
    *,
    follow: bool = False,
) -> tuple[list[list[float]], float, float, int]:
    """Snap node Z to heightmap+lift.

    follow=False: raise when buried, never lower (legacy, protects decks).
    follow=True: set Z to the heightmap (raise and lower), clamped by max_raise.

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
        if follow:
            target = z_hm
            if max_raise > 0:
                target = min(z_old + max_raise, max(z_old - max_raise, target))
            z_new = target
            if abs(z_new - z_old) > 1e-4:
                n_raised += 1
        elif z_old < z_hm - 1e-4:
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
                "str_code": r.get("str_code"),
                "objekt": r.get("objekt"),
                "kunstbauten": r.get("kunstbauten"),
                "objectid": r.get("objectid") or r.get("osm_id"),
                "pts": [[float(p[0]), float(p[1]), float(p[2]), float(p[3])] for p in pts],
                "osm_ids": list(r.get("osm_ids") or [r.get("id") or r.get("objectid")]),
                "follow_parent": r.get("follow_parent"),
                "lanes": r.get("lanes"),
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
                            sc_a = str(work[pi].get("str_code") or "").strip()
                            sc_b = str(work[qj].get("str_code") or "").strip()
                            if sc_a and sc_b and sc_a != sc_b:
                                continue
                            obj_a = str(work[pi].get("objekt") or "").upper().strip()
                            obj_b = str(work[qj].get("objekt") or "").upper().strip()
                            if obj_a and obj_b and obj_a != obj_b:
                                continue
                            if bool(work[pi].get("follow_parent")) != bool(
                                work[qj].get("follow_parent")
                            ):
                                continue
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
                        "str_code": poly.get("str_code") or other.get("str_code"),
                        "objekt": poly.get("objekt") or other.get("objekt"),
                        "kunstbauten": poly.get("kunstbauten") or other.get("kunstbauten"),
                        "objectid": poly.get("objectid") or other.get("objectid"),
                        "pts": chain,
                        "osm_ids": list(poly.get("osm_ids") or [])
                        + list(other.get("osm_ids") or []),
                        "follow_parent": poly.get("follow_parent")
                        or other.get("follow_parent"),
                        "lanes": poly.get("lanes") or other.get("lanes"),
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


def _is_one_lane_marking(road: dict) -> bool:
    """One-lane strip: solid edge lines, no dashed centerline."""
    from gip_road_segments import gip_is_subordinate_lane

    if gip_is_subordinate_lane(road):
        return True
    try:
        lanes = float(road.get("lanes"))
    except (TypeError, ValueError):
        return False
    return 0.0 < lanes <= 1.0 + 1e-6


def _polyline_tangents_xy(nodes: list[list[float]]) -> list[tuple[float, float]]:
    n = len(nodes)
    out: list[tuple[float, float]] = []
    for i in range(n):
        if i == 0:
            dx = float(nodes[1][0]) - float(nodes[0][0])
            dy = float(nodes[1][1]) - float(nodes[0][1])
        elif i == n - 1:
            dx = float(nodes[i][0]) - float(nodes[i - 1][0])
            dy = float(nodes[i][1]) - float(nodes[i - 1][1])
        else:
            dx = float(nodes[i + 1][0]) - float(nodes[i - 1][0])
            dy = float(nodes[i + 1][1]) - float(nodes[i - 1][1])
        length = math.hypot(dx, dy) or 1.0
        out.append((dx / length, dy / length))
    return out


def _edge_line_nodes(
    run: list[list[float]], line_w: float, sign: float
) -> list[list[float]]:
    """Offset a DecalRoad run to the asphalt edge. ``sign`` +1 left, −1 right."""
    if len(run) < 2:
        return []
    tans = _polyline_tangents_xy(run)
    inset = 0.5 * float(line_w)
    out: list[list[float]] = []
    for n, (tx, ty) in zip(run, tans):
        lx, ly = -ty, tx
        half = 0.5 * float(n[3])
        dist = max(0.05, half - inset)
        out.append(
            [
                float(n[0]) + sign * lx * dist,
                float(n[1]) + sign * ly * dist,
                float(n[2]),
                float(line_w),
            ]
        )
    return out


def _gallery_keeps_surface_road(gal: dict) -> bool:
    """Underpass / surface-first roofs: DecalRoad above must not be clipped away."""
    mode = str(gal.get("terrain_roof") or "none").lower().strip()
    return mode in ("keep_asphalt", "under_asphalt", "surface_first", "asphalt")


def _extend_xy_poly(
    xy: list[tuple[float, float]], dist_m: float
) -> list[tuple[float, float]]:
    """Push both endpoints outward along end tangents by dist_m."""
    if dist_m <= 1e-9 or len(xy) < 2:
        return xy
    out = list(xy)

    def _unit(ax: float, ay: float, bx: float, by: float) -> tuple[float, float]:
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy)
        if L < 1e-9:
            return 1.0, 0.0
        return dx / L, dy / L

    ux, uy = _unit(out[1][0], out[1][1], out[0][0], out[0][1])
    out[0] = (out[0][0] + ux * dist_m, out[0][1] + uy * dist_m)
    ux, uy = _unit(out[-2][0], out[-2][1], out[-1][0], out[-1][1])
    out[-1] = (out[-1][0] + ux * dist_m, out[-1][1] + uy * dist_m)
    return out


def _bridge_band_nodes(deck: dict, *, prefer_under: bool) -> list:
    """XYW nodes for a bridge band. Decal clip wants the full deck; road-bed
    skip can use the under inset. Fall back either way if one list is short."""
    under = deck.get("under_nodes_xyw") or []
    deck_xy = deck.get("nodes_xyw") or []
    if prefer_under and len(under) >= 2:
        return under
    if len(deck_xy) >= 2:
        return deck_xy
    return under if len(under) >= 2 else []


def _load_bridge_bands(
    proc: Path,
    *,
    pad_m: float,
    extend_m: float = 0.0,
    min_gap_for_extend_m: float = 0.0,
    prefer_under: bool = True,
) -> list[tuple[list[tuple[float, float]], float]]:
    """Bridge XY bands from bridges_decks.json (Decal clip / road-bed skip)."""
    path = proc / "bridges_decks.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    pad = max(0.0, float(pad_m))
    extend = max(0.0, float(extend_m))
    min_gap = max(0.0, float(min_gap_for_extend_m))
    corridors: list[tuple[list[tuple[float, float]], float]] = []
    for deck in data.get("decks") or []:
        nodes = _bridge_band_nodes(deck, prefer_under=prefer_under)
        if len(nodes) < 2:
            continue
        gap = float(deck.get("gap_len_m") or deck.get("span_len_m") or 0.0)
        use_ext = extend if gap >= min_gap else 0.0
        xy = [(float(n[0]), float(n[1])) for n in nodes]
        xy = _extend_xy_poly(xy, use_ext)
        half = 0.5 * max(float(n[2]) if len(n) > 2 else 7.5 for n in nodes) + pad
        corridors.append((xy, half))
    return corridors


def _load_bridge_under_bands(
    proc: Path,
    *,
    pad_m: float,
    extend_m: float = 0.0,
    min_gap_for_extend_m: float = 0.0,
) -> list[tuple[list[tuple[float, float]], float]]:
    """Under-deck bands (road-bed must not fill the ditch)."""
    return _load_bridge_bands(
        proc,
        pad_m=pad_m,
        extend_m=extend_m,
        min_gap_for_extend_m=min_gap_for_extend_m,
        prefer_under=True,
    )


def _load_gallery_corridors(proc: Path, cfg: dict) -> list[tuple[list[tuple[float, float]], float]]:
    # Always clip the bore. clip_galleries: false is ignored — only
    # terrain_roof: keep_asphalt leaves a surface road above.
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
            kind = str(g.get("kind") or "").lower()
            open_side = str(g.get("open_side") or "").lower()
            closed = kind == "tunnel" or open_side in ("none", "off", "false", "0")
            fake = g.get("fake_end")
            one = bool(g.get("one_ended"))
            if closed:
                # Bore only — surface DecalRoads on the daylight apron stay
                # and are seated onto the MeshRoad (see _seat_on_gallery_deck).
                if one and fake == "s0":
                    s_hi -= 0.35
                elif one and fake == "s1":
                    s_lo += 0.35
                else:
                    s_lo += 0.35
                    s_hi -= 0.35
            else:
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


def _load_clip_corridors(proc: Path, cfg: dict) -> list[tuple[list[tuple[float, float]], float]]:
    """Gallery exclusion corridors (bridges use MeshRoad Z, not this)."""
    return _load_gallery_corridors(proc, cfg)


def _load_gallery_centerlines(proc: Path) -> list[dict]:
    path = proc / "galleries_centerlines.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return [g for g in (data.get("galleries") or []) if len(g.get("nodes") or []) >= 2]


def _gallery_approach_nodes(g: dict) -> list[dict]:
    """Nodes on the daylight apron only — never the underground bore."""
    nodes = g.get("nodes") or []
    portal = g.get("portal_s") or []
    if len(nodes) < 2:
        return []
    if len(portal) < 2:
        return nodes
    s0, s1 = float(portal[0]), float(portal[1])
    if s1 < s0:
        s0, s1 = s1, s0
    fake = g.get("fake_end")
    one = bool(g.get("one_ended"))
    overlap = 2.0
    out: list[dict] = []
    for n in nodes:
        s = float(n["s"])
        if one and fake == "s0":
            if s >= s1 - overlap:
                out.append(n)
        elif one and fake == "s1":
            if s <= s0 + overlap:
                out.append(n)
        else:
            if s <= s0 + overlap or s >= s1 - overlap:
                out.append(n)
    return out


def _gallery_deck_z(
    px: float,
    py: float,
    galleries: list[dict],
    *,
    pad_m: float = 2.5,
) -> float | None:
    """Deck-top Z if (px,py) lies on a daylight apron (+ pad), not the bore."""
    best: tuple[float, float] | None = None
    for g in galleries:
        approach = _gallery_approach_nodes(g)
        if len(approach) < 2:
            continue
        on_apr = {id(n) for n in approach}
        nodes = g.get("nodes") or []
        half_ref = 0.5 * max(float(n.get("width") or 7.5) for n in approach) + pad_m
        for a, b in zip(nodes, nodes[1:]):
            if id(a) not in on_apr and id(b) not in on_apr:
                continue
            ax, ay = float(a["x"]), float(a["y"])
            bx, by = float(b["x"]), float(b["y"])
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            if seg2 < 1e-12:
                continue
            t = ((px - ax) * dx + (py - ay) * dy) / seg2
            if t < 0.0 or t > 1.0:
                continue
            qx, qy = ax + t * dx, ay + t * dy
            dist = math.hypot(px - qx, py - qy)
            if dist > half_ref:
                continue
            z = float(a["z_road"]) + t * (float(b["z_road"]) - float(a["z_road"]))
            if best is None or dist < best[0]:
                best = (dist, z)
    if best is None:
        return None
    return best[1]


def _seat_on_gallery_deck(
    nodes: list[list[float]],
    galleries: list[dict],
    *,
    sink_m: float = 0.015,
    pad_m: float = 2.5,
) -> tuple[list[list[float]], int]:
    """Pull Decal Z to deck top minus sink (1–2 cm under the MeshRoad)."""
    if not galleries or not nodes:
        return nodes, 0
    out: list[list[float]] = []
    n_seat = 0
    for n in nodes:
        z_deck = _gallery_deck_z(n[0], n[1], galleries, pad_m=pad_m)
        if z_deck is None:
            out.append(n)
            continue
        nn = list(n)
        nn[2] = float(z_deck) - sink_m
        out.append(nn)
        n_seat += 1
    return out, n_seat


def _split_by_pred(
    nodes: list[list[float]], pred
) -> list[tuple[list[list[float]], bool]]:
    """Split a polyline into runs where ``pred`` is constant. Boundary node shared."""
    if len(nodes) < 2:
        return []
    out: list[tuple[list[list[float]], bool]] = []
    cur = [nodes[0]]
    flag = bool(pred(nodes[0]))
    for n in nodes[1:]:
        f = bool(pred(n))
        if f != flag:
            if len(cur) >= 2:
                out.append((cur, flag))
            cur = [cur[-1], n]
            flag = f
        else:
            cur.append(n)
    if len(cur) >= 2:
        out.append((cur, flag))
    return out


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


def _load_ndjson_class(path: Path, class_name: str) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if e.get("class") == class_name and len(e.get("nodes") or []) >= 2:
            out.append(e)
    return out


def _load_meshroads(proc: Path, level_name: str) -> list[dict]:
    """MeshRoads as the level has them (bridges, then galleries)."""
    found: list[dict] = []
    seen: set[str] = set()
    paths = [
        USER_LEVELS / level_name / "main" / "MissionGroup" / "level_objects" / "bridges" / "items.level.json",
        proc / "bridges_items.level.json",
        USER_LEVELS / level_name / "main" / "MissionGroup" / "level_objects" / "galleries" / "items.level.json",
        proc / "galleries_items.level.json",
    ]
    for path in paths:
        for e in _load_ndjson_class(path, "MeshRoad"):
            name = str(e.get("name") or "")
            if name in seen:
                continue
            seen.add(name)
            found.append(e)
    return found


def _project_meshroad(
    px: float, py: float, nodes: list
) -> tuple[float, float, float, float] | None:
    """Perp. hit on a span segment (t in [0,1], not past the ends).

    Returns (dist, z_top, half_width, depth_m) or None.
    """
    best: tuple[float, float, float, float] | None = None
    for a, b in zip(nodes, nodes[1:]):
        ax, ay, az = float(a[0]), float(a[1]), float(a[2])
        bx, by, bz = float(b[0]), float(b[1]), float(b[2])
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        if seg2 < 1e-12:
            continue
        t = ((px - ax) * dx + (py - ay) * dy) / seg2
        if t < 0.0 or t > 1.0:
            continue
        qx, qy = ax + t * dx, ay + t * dy
        dist = math.hypot(px - qx, py - qy)
        z_top = az + t * (bz - az)
        wa = float(a[3]) if len(a) > 3 else 7.5
        wb = float(b[3]) if len(b) > 3 else wa
        half = 0.5 * (wa + t * (wb - wa))
        depth_a = float(a[4]) if len(a) > 4 else 0.4
        depth_b = float(b[4]) if len(b) > 4 else depth_a
        depth = depth_a + t * (depth_b - depth_a)
        if best is None or dist < best[0]:
            best = (dist, z_top, half, depth)
    return best


def _nearest_meshroad(
    px: float, py: float, meshroads: list[dict], pad_m: float = 0.0
) -> tuple[float, float, float] | None:
    """If (px,py) lies on a MeshRoad strip footprint, (z_top, depth_m, dist)."""
    best: tuple[float, float, float] | None = None
    for e in meshroads:
        hit = _project_meshroad(px, py, e.get("nodes") or [])
        if hit is None:
            continue
        dist, z_top, half, depth = hit
        if dist > half + max(0.0, pad_m):
            continue
        if best is None or dist < best[2]:
            best = (z_top, depth, dist)
    return best


def _meshroad_z_at(px: float, py: float, meshroads: list[dict]) -> float | None:
    """Deck-top Z at nearest MeshRoad (t clamped to the span, for abutment cuts)."""
    best: tuple[float, float] | None = None
    for e in meshroads:
        nodes = e.get("nodes") or []
        for a, b in zip(nodes, nodes[1:]):
            ax, ay, az = float(a[0]), float(a[1]), float(a[2])
            bx, by, bz = float(b[0]), float(b[1]), float(b[2])
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            if seg2 < 1e-12:
                continue
            t = ((px - ax) * dx + (py - ay) * dy) / seg2
            t = max(0.0, min(1.0, t))
            qx, qy = ax + t * dx, ay + t * dy
            dist = math.hypot(px - qx, py - qy)
            z_top = az + t * (bz - az)
            if best is None or dist < best[0]:
                best = (dist, z_top)
    if best is None or best[0] > 25.0:
        return None
    return best[1]


def _group_meshroad_spans(
    meshroads: list[dict],
) -> list[tuple[tuple[float, float, float], tuple[float, float, float]]]:
    """One (start_xyz, end_xyz) per bridge, averaged over MeshRoad strips."""
    groups: dict[str, list[dict]] = {}
    for e in meshroads:
        name = str(e.get("name") or "")
        base = name.rsplit("_s", 1)[0] if "_s" in name else name
        groups.setdefault(base, []).append(e)
    spans: list[tuple[tuple[float, float, float], tuple[float, float, float]]] = []
    for strips in groups.values():
        starts = [s["nodes"][0] for s in strips if s.get("nodes")]
        ends = [s["nodes"][-1] for s in strips if s.get("nodes")]
        if not starts or not ends:
            continue

        def avg(pts, i: int) -> float:
            return sum(float(p[i]) for p in pts) / len(pts)

        spans.append(
            (
                (avg(starts, 0), avg(starts, 1), avg(starts, 2)),
                (avg(ends, 0), avg(ends, 1), avg(ends, 2)),
            )
        )
    return spans


def _mesh_gaps_on_nodes(
    nodes: list,
    meshroads: list[dict],
    *,
    hit_lim: float = 8.0,
) -> list[tuple[float, float, float, float]]:
    """(s0, s1, z0, z1) MeshRoad spans projected onto a polyline."""
    if len(nodes) < 2 or not meshroads:
        return []
    xyzw: list[list[float]] = []
    for n in nodes:
        if isinstance(n, dict):
            xyzw.append(
                [float(n["x"]), float(n["y"]), float(n["z"]), float(n.get("width") or 0.0)]
            )
        else:
            xyzw.append([float(n[0]), float(n[1]), float(n[2]), float(n[3] if len(n) > 3 else 0.0)])
    gaps: list[tuple[float, float, float, float]] = []
    for start, end in _group_meshroad_spans(meshroads):
        ha = _project_xy_on_nodes(start[0], start[1], xyzw)
        hb = _project_xy_on_nodes(end[0], end[1], xyzw)
        if ha is None or hb is None:
            continue
        da, sa = ha
        db, sb = hb
        if da > hit_lim or db > hit_lim:
            continue
        s0, s1 = (sa, sb) if sa <= sb else (sb, sa)
        z0, z1 = (start[2], end[2]) if sa <= sb else (end[2], start[2])
        gaps.append((s0, s1, z0, z1))
    if not gaps:
        return []
    gaps.sort()
    merged: list[tuple[float, float, float, float]] = []
    for s0, s1, z0, z1 in gaps:
        if merged and s0 <= merged[-1][1] + 1.0:
            prev = merged[-1]
            if s1 > prev[1]:
                merged[-1] = (prev[0], s1, prev[2], z1)
        else:
            merged.append((s0, s1, z0, z1))
    return merged


def _hold_span_deck_z(nodes: list, meshroads: list[dict]) -> list:
    """Set Z on MeshRoad span samples to deck Z so smoothing cannot pull the gorge."""
    gaps = _mesh_gaps_on_nodes(nodes, meshroads)
    if not gaps:
        return nodes
    xyzw: list[list[float]] = []
    for n in nodes:
        if isinstance(n, dict):
            xyzw.append([float(n["x"]), float(n["y"]), float(n["z"]), 0.0])
        else:
            xyzw.append([float(n[0]), float(n[1]), float(n[2]), 0.0])
    cum, _ = _resample_by_s(xyzw)
    out = []
    for i, n in enumerate(nodes):
        s = cum[i]
        z_hold = None
        for s0, s1, z0, z1 in gaps:
            if s0 - 0.5 <= s <= s1 + 0.5:
                span = max(s1 - s0, 1e-6)
                t = (s - s0) / span
                z_hold = z0 + t * (z1 - z0)
                break
        if z_hold is None:
            out.append(n)
        elif isinstance(n, dict):
            out.append({**n, "z": float(z_hold)})
        else:
            held = list(n)
            held[2] = float(z_hold)
            out.append(held)
    return out


def _blend_run_abutment_z(nodes: list[list[float]], blend_m: float) -> list[list[float]]:
    """Lerp Z toward the already-set end nodes over blend_m of chainage."""
    if blend_m <= 0 or len(nodes) < 2:
        return nodes
    cum, _ = _resample_by_s(nodes)
    total = cum[-1]
    if total < 1e-6:
        return nodes
    z_a = float(nodes[0][2])
    z_b = float(nodes[-1][2])
    out = [list(n) for n in nodes]
    for i, s in enumerate(cum):
        z = float(nodes[i][2])
        if s <= blend_m:
            w = 1.0 - (s / blend_m)
            z = z * (1.0 - w) + z_a * w
        dist_end = total - s
        if dist_end <= blend_m:
            w = 1.0 - (dist_end / blend_m)
            z = z * (1.0 - w) + z_b * w
        out[i][2] = z
    return out


def _meshroad_deck_centerline(strips: list[dict]) -> list[list[float]]:
    """Average parallel MeshRoad strips into one [x,y,z,width] carriageway."""
    seqs = [list(s.get("nodes") or []) for s in strips]
    seqs = [s for s in seqs if len(s) >= 2]
    if not seqs:
        return []
    n = min(len(s) for s in seqs)
    out: list[list[float]] = []
    for i in range(n):
        xs = [float(s[i][0]) for s in seqs]
        ys = [float(s[i][1]) for s in seqs]
        zs = [float(s[i][2]) for s in seqs]
        ws = [float(s[i][3]) if len(s[i]) > 3 else 0.0 for s in seqs]
        w = sum(ws) if sum(ws) > 0.5 else 7.5
        out.append(
            [sum(xs) / len(xs), sum(ys) / len(ys), sum(zs) / len(zs), w]
        )
    return out


def _closed_tunnel_deck_names(proc: Path) -> set[str]:
    """MeshRoad names of closed tubes — do not copy those onto DecalRoads.

    A one-ended tunnel deck dives ``fake_end_drop_m``; a leftover Decal of that
    ramp sits on the daylight asphalt as ~1 m steps.
    """
    path = proc / "galleries_centerlines.json"
    skip: set[str] = set()
    if not path.is_file():
        return skip
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return skip
    for g in data.get("galleries") or []:
        kind = str(g.get("kind") or "").lower()
        open_side = str(g.get("open_side") or "").lower()
        if kind != "tunnel" and open_side not in ("none", "off", "false", "0"):
            continue
        slug = bb._slug(str(g.get("name") or "gallery"))
        oid = g.get("objectid") or "x"
        skip.add(f"gallery_deck_{slug}_{oid}")
    return skip


def _iter_bridge_decks(
    meshroads: list[dict],
    *,
    skip_names: set[str] | None = None,
) -> list[tuple[str, list[list[float]]]]:
    groups: dict[str, list[dict]] = {}
    skip = skip_names or set()
    for e in meshroads or []:
        name = str(e.get("name") or "")
        if not (
            name.startswith("bridge_") or name.startswith("gallery_deck_")
        ):
            continue
        if name in skip:
            continue
        base = name.rsplit("_s", 1)[0] if "_s" in name else (name or "bridge")
        if base in skip:
            continue
        groups.setdefault(base, []).append(e)
    out: list[tuple[str, list[list[float]]]] = []
    for base, strips in groups.items():
        cl = _meshroad_deck_centerline(strips)
        if len(cl) >= 2:
            out.append((base, cl))
    return out


def _extend_nodes_ends(nodes: list[list[float]], dist_m: float) -> list[list[float]]:
    """Push endpoints outward along end tangents; keep Z and width."""
    if dist_m <= 1e-9 or len(nodes) < 2:
        return [list(n) for n in nodes]
    out = [list(n) for n in nodes]

    def _unit(a: list[float], b: list[float]) -> tuple[float, float]:
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy) or 1.0
        return dx / length, dy / length

    ux, uy = _unit(out[1], out[0])
    out[0][0] += ux * dist_m
    out[0][1] += uy * dist_m
    ux, uy = _unit(out[-2], out[-1])
    out[-1][0] += ux * dist_m
    out[-1][1] += uy * dist_m
    return out


def _project_xy_on_nodes(
    px: float, py: float, nodes: list[list[float]]
) -> tuple[float, float] | None:
    """Nearest point on Decal polyline → (dist_m, s_m)."""
    if len(nodes) < 2:
        return None
    cum, _ = _resample_by_s(nodes)
    best: tuple[float, float] | None = None
    for i, (a, b) in enumerate(zip(nodes, nodes[1:])):
        ax, ay = float(a[0]), float(a[1])
        bx, by = float(b[0]), float(b[1])
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        if seg2 < 1e-12:
            continue
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
        qx, qy = ax + t * dx, ay + t * dy
        dist = math.hypot(px - qx, py - qy)
        s = cum[i] + t * (cum[i + 1] - cum[i])
        if best is None or dist < best[0]:
            best = (dist, s)
    return best


def _split_off_bridge_decks(
    nodes: list[list[float]], meshroads: list[dict]
) -> list[list[list[float]]]:
    """Cut terrain Decals at MeshRoad abutments and drop the span.

    Does not wait for a Decal node to sit on the deck (10 m densify often
    skips the whole 13 m slab). Projects each bridge's start/end onto the
    Decal polyline and slices there. Cut Z = deck Z.
    """
    if len(nodes) < 2 or not meshroads:
        return [nodes] if len(nodes) >= 2 else []
    cum, _ = _resample_by_s(nodes)
    total = cum[-1]
    hit_lim = 8.0
    gaps: list[tuple[float, float, float, float]] = []
    for start, end in _group_meshroad_spans(meshroads):
        ha = _project_xy_on_nodes(start[0], start[1], nodes)
        hb = _project_xy_on_nodes(end[0], end[1], nodes)
        if ha is None or hb is None:
            continue
        da, sa = ha
        db, sb = hb
        if da > hit_lim or db > hit_lim:
            continue
        s0, s1 = (sa, sb) if sa <= sb else (sb, sa)
        z0, z1 = (start[2], end[2]) if sa <= sb else (end[2], start[2])
        gaps.append((s0, s1, z0, z1))
    if not gaps:
        return [nodes]
    gaps.sort()
    merged: list[list[float]] = []
    for s0, s1, z0, z1 in gaps:
        if merged and s0 <= merged[-1][1] + 1.0:
            if s1 > merged[-1][1]:
                merged[-1][1] = s1
                merged[-1][3] = z1
        else:
            merged.append([s0, s1, z0, z1])

    runs: list[list[list[float]]] = []
    cursor = 0.0
    z_enter = None
    for s0, s1, z0, z1 in merged:
        if s0 - cursor >= 0.5:
            piece = _slice_nodes(nodes, cursor, s0)
            if len(piece) >= 2:
                if z_enter is not None:
                    piece[0][2] = z_enter
                piece[-1][2] = z0
                runs.append(piece)
        cursor = s1
        z_enter = z1
    if total - cursor >= 0.5:
        piece = _slice_nodes(nodes, cursor, total)
        if len(piece) >= 2:
            if z_enter is not None:
                piece[0][2] = z_enter
            runs.append(piece)
    return runs


def _split_by_inside(
    nodes: list[list[float]],
    inside_fn,
) -> list[list[list[float]]]:
    """Keep runs where inside_fn is false; cut at the boundary."""
    if len(nodes) < 2:
        return []

    def cut_point(a: list[float], b: list[float], a_in: bool) -> list[float]:
        lo, hi = 0.0, 1.0
        for _ in range(24):
            mid = 0.5 * (lo + hi)
            p = [
                a[0] + mid * (b[0] - a[0]),
                a[1] + mid * (b[1] - a[1]),
                a[2] + mid * (b[2] - a[2]),
                a[3] + mid * (b[3] - a[3]),
            ]
            if inside_fn(p) == a_in:
                lo = mid
            else:
                hi = mid
        t = hi if a_in else lo
        if a_in:
            t = min(1.0, t + 1e-4)
        else:
            t = max(0.0, t - 1e-4)
        outside = a if not a_in else b
        return [
            a[0] + t * (b[0] - a[0]),
            a[1] + t * (b[1] - a[1]),
            float(outside[2]),
            float(outside[3]),
        ]

    runs: list[list[list[float]]] = []
    cur: list[list[float]] = []
    prev: list[float] | None = None
    prev_in = False
    for n in nodes:
        n_in = bool(inside_fn(n))
        if prev is not None and prev_in != n_in:
            cut = cut_point(prev, n, prev_in)
            if not prev_in:
                cur.append(cut)
                if len(cur) >= 2:
                    runs.append(cur)
                cur = []
            else:
                cur = [cut]
        if not n_in:
            cur.append(n)
        prev = n
        prev_in = n_in
    if len(cur) >= 2:
        runs.append(cur)
    return runs


def _split_outside(
    nodes: list[list[float]],
    corridors: list[tuple[list[tuple[float, float]], float]],
) -> list[list[list[float]]]:
    if not corridors:
        return [nodes] if len(nodes) >= 2 else []

    def inside(n: list[float]) -> bool:
        return any(_dist_poly(n[0], n[1], poly) <= half for poly, half in corridors)

    return _split_by_inside(nodes, inside)


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
    over_objects: bool | None = None,
) -> dict:
    fade = cfg["start_end_fade"]
    over = cfg["over_objects"] if over_objects is None else over_objects
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
        "overObjects": bool(over),
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


def build_entries(
    roads: list[dict],
    cfg: dict,
    corridors,
    z_at=None,
    meshroads: list[dict] | None = None,
    galleries: list[dict] | None = None,
) -> list[dict]:
    entries: list[dict] = []
    hwy_ok = set(cfg["highways"])
    w_scale = max(0.1, cfg["width_scale"])
    cl = cfg["centerline"]
    n_asphalt = n_line = n_edge = 0
    snap = bool(cfg.get("snap_to_heightmap")) and z_at is not None
    lift = float(cfg.get("z_lift_m") or 0.0)
    max_raise = float(cfg.get("snap_max_raise_m") or 0.0)
    snap_min = snap_max = 0.0
    snap_n = 0
    raised_n = 0
    seated_n = 0
    galleries = galleries or []

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
        # follow_parent already wrote parent Z onto the GIP nodes; do not
        # snap that window back onto the previous composed DGM.
        if snap and not road.get("follow_parent"):
            densified, dmin, dmax, nr = _snap_nodes_z(
                densified,
                z_at,
                lift,
                max_raise,
                follow=bool(cfg.get("snap_follow_heightmap")),
            )
            raised_n += nr
            if snap_n == 0:
                snap_min, snap_max = dmin, dmax
            else:
                snap_min = min(snap_min, dmin)
                snap_max = max(snap_max, dmax)
            snap_n += len(densified)
        if meshroads:
            densified = _hold_span_deck_z(densified, meshroads)
        smooth_z = float(cfg.get("smooth_decal_z_m") or 0.0)
        if smooth_z > 0 and len(densified) >= 2:
            tmp = [
                {"x": n[0], "y": n[1], "z": n[2], "width": n[3]} for n in densified
            ]
            tmp = _smooth_polyline_z(tmp, smooth_z)
            densified = [[t["x"], t["y"], t["z"], t["width"]] for t in tmp]
        runs = [densified]
        if cfg.get("clip_bridges") and meshroads:
            split: list[list[list[float]]] = []
            for run in runs:
                split.extend(_split_off_bridge_decks(run, meshroads))
            runs = split if split else runs
        out_g: list[list[list[float]]] = []
        for run in runs:
            out_g.extend(_split_outside(run, corridors))
        runs = out_g if corridors else runs
        blend_m = float(cfg.get("abutment_blend_m") or 0.0)
        if blend_m > 0:
            runs = [
                _blend_run_abutment_z(run, blend_m) for run in runs if len(run) >= 2
            ]
        tagged: list[tuple[list[list[float]], bool]] = []
        for run in runs:
            if len(run) < 2:
                continue
            on_deck = False
            if galleries:
                run, ns = _seat_on_gallery_deck(run, galleries)
                seated_n += ns
                if ns:
                    parts = _split_by_pred(
                        run,
                        lambda n: _gallery_deck_z(n[0], n[1], galleries) is not None,
                    )
                    if parts:
                        tagged.extend(parts)
                        continue
            tagged.append((run, on_deck))
        slug = bb._slug(str(road.get("name") or hwy or "road"))
        oid = road.get("id") or road.get("osm_id") or ri
        max_len = float(cfg.get("max_decal_length_m") or 0.0)

        pieces: list[tuple[int, int, list, bool]] = []
        for run_i, (run, on_deck) in enumerate(tagged):
            if len(run) < 2:
                continue
            segs = _split_by_length(run, max_len)
            for seg_i, seg in enumerate(segs):
                pieces.append((run_i, seg_i, seg, on_deck))

        multi_run = len(tagged) > 1
        for run_i, seg_i, run, on_deck in pieces:
            if len(run) < 2:
                continue
            name = f"decal_{slug}_{oid}"
            if multi_run:
                name += f"_r{run_i}"
            if max_len > 0 and (seg_i > 0 or len([p for p in pieces if p[0] == run_i]) > 1):
                name += f"_s{seg_i}"
            if on_deck:
                name += "_ondeck"
            entries.append(
                _decal_entry(
                    name=name,
                    nodes=run,
                    material=cfg["material"],
                    texture_length=cfg["texture_length"],
                    render_priority=int(cfg["render_priority"]) + (1 if on_deck else 0),
                    drivability=cfg["drivability"],
                    cfg=cfg,
                    auto_lanes=not _is_one_lane_marking(road),
                    over_objects=True if on_deck else None,
                )
            )
            n_asphalt += 1

            if not cl["enabled"] or cl["style"] in ("none", "off", "false"):
                continue
            line_w = max(0.05, float(cl["width_m"]))
            one_lane = _is_one_lane_marking(road)
            if one_lane:
                mark_runs: list[tuple[str, list[list[float]]]] = []
                left = _edge_line_nodes(run, line_w, 1.0)
                right = _edge_line_nodes(run, line_w, -1.0)
                if len(left) >= 2:
                    mark_runs.append(("e0", left))
                if len(right) >= 2:
                    mark_runs.append(("e1", right))
            else:
                line_nodes = [[n[0], n[1], n[2], line_w] for n in run]
                style = cl["style"]
                if style == "dashed":
                    chunks = _dashed_runs(
                        line_nodes, float(cl["dash_m"]), float(cl["gap_m"])
                    )
                else:
                    chunks = [line_nodes]
                mark_runs = []
                for ci, chunk in enumerate(chunks):
                    tag = f"d{ci}" if len(chunks) > 1 else ""
                    mark_runs.append((tag, chunk))

            for tag, chunk in mark_runs:
                if len(chunk) < 2:
                    continue
                lname = f"line_{slug}_{oid}"
                if multi_run:
                    lname += f"_r{run_i}"
                if max_len > 0 and (seg_i > 0 or len([p for p in pieces if p[0] == run_i]) > 1):
                    lname += f"_s{seg_i}"
                if tag:
                    lname += f"_{tag}"
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
                        over_objects=True if on_deck else None,
                    )
                )
                n_line += 1
                if one_lane:
                    n_edge += 1

    n_deck = n_deck_line = 0
    if cfg.get("deck_decals") and meshroads:
        n_deck, n_deck_line = _append_bridge_deck_decals(entries, cfg, meshroads)
        n_asphalt += n_deck
        n_line += n_deck_line

    print(
        f"DecalRoads: asphalt={n_asphalt} centerline={n_line} "
        f"one_lane_edges={n_edge} "
        f"style={cl.get('style')} gallery_clip={len(corridors)}"
        f" meshroads={len(meshroads or [])} deck={n_deck}"
        + (
            f" max_decal_length_m={cfg.get('max_decal_length_m')}"
            if cfg.get("max_decal_length_m")
            else ""
        )
    )
    if seated_n:
        print(
            f"  gallery deck seat: nodes={seated_n} "
            f"(Z = MeshRoad top - 1.5 cm, overObjects)"
        )
    if snap and snap_n:
        print(
            f"  snap_to_heightmap: nodes={snap_n} raised={raised_n} "
            f"dz=[{snap_min:+.3f},{snap_max:+.3f}]m lift={lift:.3f}m "
            f"max_raise={max_raise:.2f}m"
        )
    return entries


def _append_bridge_deck_decals(
    entries: list[dict], cfg: dict, meshroads: list[dict]
) -> tuple[int, int]:
    """Asphalt + markings on MeshRoad decks (Z/width from mesh, not DGM)."""
    cl = cfg["centerline"]
    overlap = float(cfg.get("deck_decal_overlap_m") or 0.0)
    n_asphalt = n_line = 0
    skip_tunnels = _closed_tunnel_deck_names(processed_dir(load_site()))
    for base, nodes in _iter_bridge_decks(meshroads, skip_names=skip_tunnels):
        run = _extend_nodes_ends(nodes, overlap)
        if len(run) < 2 or _polyline_length(run) < 1.0:
            continue
        slug = bb._slug(base)
        entries.append(
            _decal_entry(
                name=f"decal_{slug}_deck",
                nodes=run,
                material=cfg["material"],
                texture_length=cfg["texture_length"],
                render_priority=int(cfg["render_priority"]) + 1,
                drivability=cfg["drivability"],
                cfg=cfg,
                auto_lanes=True,
                over_objects=True,
            )
        )
        n_asphalt += 1
        if not cl["enabled"] or cl["style"] in ("none", "off", "false"):
            continue
        line_w = max(0.05, float(cl["width_m"]))
        line_nodes = [[n[0], n[1], n[2], line_w] for n in run]
        style = cl["style"]
        if style == "dashed":
            chunks = _dashed_runs(line_nodes, float(cl["dash_m"]), float(cl["gap_m"]))
        else:
            chunks = [line_nodes]
        for ci, chunk in enumerate(chunks):
            lname = f"line_{slug}_deck"
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
                    over_objects=True,
                )
            )
            n_line += 1
    n_apr, n_apr_line = _append_tunnel_approach_deck_decals(entries, cfg)
    n_asphalt += n_apr
    n_line += n_apr_line
    if n_asphalt:
        print(
            f"  deck Decals: asphalt={n_asphalt} centerline={n_line} "
            f"overlap={overlap:.1f}m overObjects=1"
        )
    return n_asphalt, n_line


def _gallery_daylight_s(g: dict) -> list[tuple[str, float, float]]:
    """(end, s_portal, out_sign) for real portals only."""
    portal = g.get("portal_s") or []
    if len(portal) < 2:
        return []
    s0, s1 = float(portal[0]), float(portal[1])
    if s1 < s0:
        s0, s1 = s1, s0
    fake = g.get("fake_end")
    one = bool(g.get("one_ended"))
    out: list[tuple[str, float, float]] = []
    if not (one and fake == "s0"):
        out.append(("s0", s0, -1.0))
    if not (one and fake == "s1"):
        out.append(("s1", s1, 1.0))
    return out


def _append_tunnel_approach_deck_decals(
    entries: list[dict], cfg: dict
) -> tuple[int, int]:
    """Closed-tube approach only: MeshRoad past the door, overObjects.

    The full bore is not copied (fake_end drop would stair-step the asphalt).
    This matches bridge decks: the slab owns the mouth, surface DecalRoads yield.
    """
    proc = processed_dir(load_site())
    cl_path = proc / "galleries_centerlines.json"
    if not cl_path.is_file():
        return 0, 0
    try:
        data = json.loads(cl_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0, 0
    overlap = max(1.0, float(cfg.get("deck_decal_overlap_m") or 1.0))
    cl = cfg["centerline"]
    n_asphalt = n_line = 0
    for g in data.get("galleries") or []:
        kind = str(g.get("kind") or "").lower()
        open_side = str(g.get("open_side") or "").lower()
        if kind != "tunnel" and open_side not in ("none", "off", "false", "0"):
            continue
        nodes = g.get("nodes") or []
        if len(nodes) < 2:
            continue
        slug = bb._slug(str(g.get("name") or "gallery"))
        oid = g.get("objectid") or "x"
        for end_name, s_p, out_s in _gallery_daylight_s(g):
            if out_s >= 0.0:
                band = [n for n in nodes if float(n["s"]) >= s_p - overlap]
            else:
                band = [n for n in nodes if float(n["s"]) <= s_p + overlap]
            band = sorted(band, key=lambda n: float(n["s"]))
            if out_s < 0.0:
                band = list(reversed(band))
            run = [
                [
                    float(n["x"]),
                    float(n["y"]),
                    float(n["z_road"]),
                    float(n.get("width") or 7.5),
                ]
                for n in band
            ]
            if len(run) < 2 or _polyline_length(run) < 2.0:
                continue
            run = _extend_nodes_ends(run, overlap)
            name = f"decal_gallery_deck_{slug}_{oid}_{end_name}_approach"
            entries.append(
                _decal_entry(
                    name=name,
                    nodes=run,
                    material=cfg["material"],
                    texture_length=cfg["texture_length"],
                    render_priority=int(cfg["render_priority"]) + 2,
                    drivability=cfg["drivability"],
                    cfg=cfg,
                    auto_lanes=True,
                    over_objects=True,
                )
            )
            n_asphalt += 1
            if not cl["enabled"] or cl["style"] in ("none", "off", "false"):
                continue
            line_w = max(0.05, float(cl["width_m"]))
            line_nodes = [[n[0], n[1], n[2], line_w] for n in run]
            if cl["style"] == "dashed":
                chunks = _dashed_runs(line_nodes, float(cl["dash_m"]), float(cl["gap_m"]))
            else:
                chunks = [line_nodes]
            for ci, chunk in enumerate(chunks):
                lname = f"line_gallery_deck_{slug}_{oid}_{end_name}_approach"
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
                        over_objects=True,
                    )
                )
                n_line += 1
    if n_asphalt:
        print(
            f"  tunnel approach Decals: asphalt={n_asphalt} "
            f"centerline={n_line} overObjects=1 priority+2"
        )
    return n_asphalt, n_line


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
    opacity_max=None,
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
    if opacity_max is not None:
        roi_op = opacity_max[r0 : r1 + 1, c0 : c1 + 1]
        np.maximum(roi_op, w, out=roi_op)
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
    from authorities import gip_ids_from_props, stamp_gip_props, transformer_gip_fc_into_working

    sc = __import__("site_coords", fromlist=["SiteCoords"]).SiteCoords(site)
    path = bb.find_gip_geojson(site)
    data = json.loads(path.read_text(encoding="utf-8"))
    to_site = transformer_gip_fc_into_working(site, data)
    want = int(objectid)
    for f in data.get("features") or []:
        props = stamp_gip_props(site, f.get("properties") or {})
        ids = gip_ids_from_props(site, props)
        if ids is None or int(ids[0]) != want:
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
    fp = road.get("follow_parent") or {}
    if fp:
        if fp.get("max_raise_m") is not None:
            base["max_raise_m"] = max(
                float(base["max_raise_m"]), float(fp["max_raise_m"])
            )
        if fp.get("max_cut_m") is not None:
            base["max_cut_m"] = max(float(base["max_cut_m"]), float(fp["max_cut_m"]))
        if fp.get("sink_m") is not None:
            base["sink_m"] = float(fp["sink_m"])
    items = cfg.get("road_bed_items") or []
    if not items:
        return base
    rid = str(road.get("id") or road.get("osm_id") or "")
    road_oid = road.get("objectid")
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
        codes = match.get("str_code")
        if not hit and codes is not None:
            if not isinstance(codes, (list, tuple)):
                codes = [codes]
            road_code = str(road.get("str_code") or "").strip()
            if road_code:
                from gip_road_segments import gip_str_code_matches

                if any(gip_str_code_matches(road_code, str(c).strip()) for c in codes):
                    hit = True
        oids = match.get("objectid")
        if not hit and oids is not None:
            if not isinstance(oids, (list, tuple)):
                oids = [oids]
            oid_set: set[int] = set()
            for oid in oids:
                try:
                    oid_set.add(int(oid))
                except (TypeError, ValueError):
                    continue
            if road_oid is not None:
                try:
                    if int(road_oid) in oid_set:
                        hit = True
                except (TypeError, ValueError):
                    pass
            if not hit and rid:
                try:
                    if int(rid) in oid_set:
                        hit = True
                except (TypeError, ValueError):
                    pass
            if not hit:
                for oid in oid_set:
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
    proc: Path | None = None,
    meshroads: list[dict] | None = None,
):
    """Flatten heightmap under all roads via one shared value/opacity raster.

    1) Project every road top-down into accumulators (target Z = value, blend = weight).
    2) opacity from PIL stroke+blur; value = value_acc/weight_acc.
    3) Zero opacity under bridge decks (keep ditch / avoid OSM road-bed in the span).
    4) Optionally stamp MeshRoad deck Z so Decals drape on the slab.
    5) Single composite onto the heightmap (per-pixel max_delta clamp).
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
    opacity_acc = np.zeros((size, size), dtype=np.float64)
    raise_lim = np.zeros((size, size), dtype=np.float64)
    cut_lim = np.zeros((size, size), dtype=np.float64)
    hard_img = Image.new("L", (size, size), 0)
    soft_img = Image.new("L", (size, size), 0)
    draw_h = ImageDraw.Draw(hard_img)
    draw_s = ImageDraw.Draw(soft_img)

    from gip_road_segments import gip_skip_road_bed

    n_roads = 0
    n_stamps = 0
    n_over = 0
    max_falloff_used = falloff0
    for road in roads:
        hwy = str(road.get("highway") or "").lower()
        if hwy_ok and hwy not in hwy_ok:
            continue
        if gip_skip_road_bed(road):
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
        use_node_z = bool(road.get("follow_parent"))
        nodes = [
            {
                "x": n[0],
                "y": n[1],
                "z": float(n[2]) if use_node_z else float(z_at(n[0], n[1])),
                "width": n[3],
            }
            for n in densified
        ]
        if meshroads:
            nodes = _hold_span_deck_z(nodes, meshroads)
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
                opacity_max=opacity_acc,
            )
            n_stamps += 1

    blur_r = max(1, int(round(max_falloff_used / mpp)))
    soft_blur = soft_img.filter(ImageFilter.GaussianBlur(radius=blur_r))
    hard = np.asarray(hard_img, dtype=np.float64) / 255.0
    soft = np.asarray(soft_blur, dtype=np.float64) / 255.0
    # PIL strokes thin on diagonals and leave edge pixels at ~0.8 while the
    # Euclidean stamp core is already 1.0. Take the stronger of both so a
    # pixel that has a target Z is not left as a partial mix.
    opacity = np.maximum(np.maximum(hard, soft), opacity_acc)
    np.clip(opacity, 0.0, 1.0, out=opacity)

    # OSM road-bed must not fight MeshRoad decks (bridge_deck layer owns those pixels).
    from site_coords import processed_dir as _processed_dir

    bed_proc = proc if proc is not None else (_processed_dir(site) if site else None)
    n_bridge_skip = 0
    if bed_proc is not None:
        bpad = float(
            cfg.get("road_bed_bridge_pad_m")
            if cfg.get("road_bed_bridge_pad_m") is not None
            else cfg.get("bridge_clip_pad_m") or 2.0
        )
        under = _load_bridge_bands(
            bed_proc, pad_m=bpad, extend_m=0.0, prefer_under=False
        )
        if under:
            ex = Image.new("L", (size, size), 0)
            dex = ImageDraw.Draw(ex)
            for poly, half in under:
                if len(poly) < 2:
                    continue
                pts = [
                    bb._to_px_beamng(float(x), float(y), size, extent) for x, y in poly
                ]
                wpx = max(2, int(round((2.0 * half) / mpp)))
                dex.line(pts, fill=255, width=wpx, joint="curve")
            emask = np.asarray(ex, dtype=np.uint8) > 0
            n_bridge_skip = int(emask.sum())
            opacity = opacity.copy()
            opacity[emask] = 0.0
            print(
                f"Road-bed: skipped {len(under)} bridge decks "
                f"({n_bridge_skip} px, pad={bpad:.1f}m)"
            )

    n_gallery_skip = 0
    if bed_proc is not None:
        gal_corr = _load_gallery_corridors(bed_proc, cfg)
        if gal_corr:
            gx = Image.new("L", (size, size), 0)
            dgx = ImageDraw.Draw(gx)
            for poly, half in gal_corr:
                if len(poly) < 2:
                    continue
                pts = [
                    bb._to_px_beamng(float(x), float(y), size, extent) for x, y in poly
                ]
                wpx = max(2, int(round((2.0 * half) / mpp)))
                dgx.line(pts, fill=255, width=wpx, joint="curve")
            gmask = np.asarray(gx, dtype=np.uint8) > 0
            n_gallery_skip = int(gmask.sum())
            opacity = opacity.copy()
            opacity[gmask] = 0.0
            print(
                f"Road-bed: skipped {len(gal_corr)} gallery/tunnel spans "
                f"({n_gallery_skip} px)"
            )

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
        "bridge_under_px_skipped": n_bridge_skip,
        "gallery_span_px_skipped": n_gallery_skip,
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
    return out, stats, value, opacity


def write_road_bed_heightmap(
    proc: Path,
    level_name: str,
    *,
    z_tgt,
    weight,
    max_height_m: float,
    size: int,
) -> Path:
    import heightmap_layers as hml

    hml.write_replace_layer(
        proc, "road_bed", z_tgt, weight, max_h=max_height_m, size=size
    )
    return hml.compose(proc, size=size, max_h=max_height_m, level_name=level_name)


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
    src = str(cfg.get("centerline_source") or "osm").lower().strip()
    if src in ("strassennetz", "sn", "net", "strasse", "straßennetz"):
        from strassennetz_roads import load_strassennetz_polylines_for_decals

        roads = load_strassennetz_polylines_for_decals(site)
        if not roads:
            raise SystemExit(
                f"Missing {proc / 'strassennetz_beamng.json'} — "
                "run: python tools/fetch_strassennetz.py"
            )
        print(f"Decal centerline_source=strassennetz roads={len(roads)}")
        cfg = dict(cfg)
        cfg["stitch_abutting"] = False
    elif src in ("gip", "verkehrswege", "objectid", "oid"):
        from gip_road_segments import load_gip_polylines_for_decals

        roads = load_gip_polylines_for_decals(site)
        if not roads:
            raise SystemExit("No GIP road segments — run: python tools/fetch_gip.py")
        print(
            f"Decal centerline_source=gip roads={len(roads)} "
            f"str_codes={sorted({str(r.get('str_code') or '') for r in roads})}"
        )
        # Stitch abutting GIP fragments into continuous asphalt ribbons
        roads = stitch_abutting_roads(roads, cfg)
        named_n = sum(1 for r in roads if str(r.get("str_code") or "").strip())
        print(
            f"GIP after stitch: {len(roads)} "
            f"(named={named_n} minor={len(roads) - named_n})"
        )
    else:
        roads = bb.load_road_polylines(proc)
        if not roads:
            raise SystemExit(f"Missing {proc / 'roads_beamng.json'}")
        roads = stitch_abutting_roads(roads, cfg)
    fill = float(cfg.get("width_fill_dip_m") or 0.0)
    blend = float(cfg.get("width_blend_m") or 0.0)
    if fill > 0 or blend > 0:
        roads = smooth_roads_widths(roads, fill_dip_m=fill, blend_m=blend)
        print(f"Decal width smooth: fill_dip_m={fill} blend_m={blend}")

    decal_roads = roads
    if src in ("gip", "verkehrswege", "objectid", "oid"):
        mode = str(cfg.get("gip_decals") or "named").lower().strip()
        if mode in ("named", "str_code", "landes"):
            from gip_road_segments import gip_is_named_road

            decal_roads = [r for r in roads if gip_is_named_road(r)]
            print(
                f"GIP DecalRoads: {len(decal_roads)} named "
                f"(road-bed/asphalt still {len(roads)}; gip_decals={mode})"
            )
        elif mode not in ("all", "full"):
            print(f"WARNING: unknown gip_decals={mode!r} — using all roads")
        from gip_road_segments import gip_is_tunnel_bore

        before_tun = len(decal_roads)
        decal_roads = [r for r in decal_roads if not gip_is_tunnel_bore(r, site)]
        n_tun = before_tun - len(decal_roads)
        n_autobahn = sum(
            1
            for r in decal_roads
            if str(r.get("str_code") or "").upper().startswith("A")
            or str(r.get("objekt") or "").upper() in ("S-A", "S-AR")
        )
        if n_tun:
            print(f"GIP DecalRoads: skipped {n_tun} tunnel segment(s) (terrain drape)")
        print(f"GIP DecalRoads: Autobahn/S-A kept={n_autobahn}")

    size = int(bng.get("mask_size") or 512)
    meta_path = proc / "heightmap_meta.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        max_h = float(meta["max_height_m"])
        extent = float(meta.get("terrain_extent_m") or size)
    else:
        max_h = 256.0
        extent = float(size)

    meshroads: list[dict] = []
    if cfg.get("clip_bridges") or cfg.get("deck_decals") or cfg.get("road_bed_conform"):
        meshroads = _load_meshroads(proc, level_name)

    if cfg.get("road_bed_conform") and not args.skip_road_bed:
        import heightmap_layers as hml

        try:
            elev_m, max_h_dgm = hml.load_dgm(proc, size)
        except FileNotFoundError:
            print("road_bed_conform: no DGM heightmap — skipped")
            elev_m = None
        if elev_m is not None:
            _out, _stats, value, opacity = conform_road_bed_heightmap(
                roads,
                cfg,
                elev=elev_m,
                size=size,
                extent=extent,
                max_h=max_h_dgm,
                site=site,
                proc=proc,
                meshroads=meshroads,
            )
            write_road_bed_heightmap(
                proc,
                level_name,
                z_tgt=value,
                weight=opacity,
                max_height_m=max_h_dgm,
                size=size,
            )
            print("road_bed_conform: wrote road_bed layer vs DGM")

    z_at = None
    if cfg.get("snap_to_heightmap"):
        pack = _load_decal_heightmap(proc, size, extent)
        if pack is None:
            print("snap_to_heightmap: no heightmap found — keeping roads_beamng Z")
        else:
            z_at, label = pack
            print(f"snap_to_heightmap: using {label}")

    corridors = _load_clip_corridors(proc, cfg)
    if meshroads:
        print(
            f"Decal MeshRoads: {len(meshroads)} strips "
            f"(clip={bool(cfg.get('clip_bridges'))} deck={bool(cfg.get('deck_decals'))})"
        )
    galleries_cl = _load_gallery_centerlines(proc)
    entries = build_entries(
        decal_roads,
        cfg,
        corridors,
        z_at=z_at,
        meshroads=meshroads,
        galleries=galleries_cl,
    )
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
