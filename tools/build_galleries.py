"""Build GIP galleries/tunnels: open U-shell mesh + portal terrain clearance.

Outputs under data/processed/<site>/:
  - galleries_centerlines.json / galleries_meta.json
  - gallery_meshes/*.dae
  - heightmap_*_gallery.png (portals carved to Hermite road Z)
  - empty theTerrain_holemap.png (holemap cannot cut a height band)

Clearance model: sweep width x clear_height along Hermite CL; lower colliding
heightmap cells to road Z. Re-import terrainPreset.json after build.

Pipeline (bridge-style config):
  - XY on OSM road centerline + extend_before/after
  - Z Hermite from portal road heights; clear_height_m -> roof
  - open_side left|right|both|none -> omit that wall (lookout)
  - Collada loft -> TSStatic under SimGroup galleries

Usage:
  cd C:\\temp\\beamng_autoroad
  $env:AUTOROAD_SITE='config/sites/l13_kuehtai.yaml'
  python tools\\build_galleries.py
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from site_coords import SiteCoords, load_site, processed_dir  # noqa: E402
import build_bridges as bb  # noqa: E402

USER_LEVELS = bb.USER_LEVELS

GALLERY_SCALAR_KEYS = (
    "width_m",
    "width_from_road",
    "clear_height_m",
    "roof_thickness_m",
    "wall_thickness_m",
    "extend_before_m",
    "extend_after_m",
    "step_m",
    "crossfall_max",
    "open_side",
    "z_profile",  # hermite (default) | road | road_spline
    "profile",  # alias: road_spline | hermite (bridges naming)
    "centerline",  # osm | strassennetz
    "free_span",  # linear | pchip_ends
    "solid_run_m",
    "span_dip_m",
    "span_search_m",
    "deck_lift_m",
    "corner_up_m",
    "corner_down_m",
    "corner_band",
    "abutment_s",  # optional [s0, s1] override along centerline
    "portal_grade_step_m",
    "portal_grade_jump",  # relative Δgrade (e.g. 0.04 = +4 pp)
    "portal_grade_baseline_n",
    "portal_out_extra_m",  # further outside after jump
    "portal_grade_span_m",  # smooth grade over this length (kill 1m terraces)
    "portal_jump_min_grade",  # jump must reach at least this grade (into gallery)
    "portal_search_out_m",
    "portal_search_in_m",
    "hole_mode",  # portals | corridor | none
    "hole_pad_m",
    "hole_portal_length_m",
    "hole_out_bite_m",  # meters outside Hermite portal to start the strip
    "hole_above_road_m",  # punch if DGM > hermite_z + this (face lip)
    "hole_max_above_m",  # skip if DGM > hermite_z + this (no sky-shaft through mountain)
    "hole_width_scale",  # 1.0 = full road width; <1 keeps mountain side out of strip
    "deck_enabled",
    "deck_depth_m",
    "deck_width_extra_m",
    "deck_open_extra_m",  # extend asphalt only toward open side
    "deck_extend_m",  # meters past Hermite portals for approach blend
    "parapet_height_m",
    "parapet_thickness_m",
    "parapet_out_m",  # grow parapet outward past open deck edge (keep inner face)
    "column_spacing_m",
    "column_along_m",
    "column_lat_m",
    "column_out_m",  # clear outside deck: inner face this far past open deck edge
    "approach_blend_m",  # marry: unused for Z float; legacy hermite_blend length
    "approach_mode",  # marry (DGM outside) | hermite_blend (float C1 outside)
    "portal_ease_m",  # marry: short C1 just inside portals
    "approach_lift_m",  # MeshRoad slightly above DGM on approaches (anti z-fight)
    "approach_conform",  # bake heightmap to MeshRoad Z on approaches
    "approach_conform_pad_m",
    "approach_conform_falloff_m",
    "approach_conform_sink_m",  # terrain slightly under MeshRoad (anti z-fight)
    "approach_conform_max_delta_m",  # skip mountain cells (|Δz| too large)
    "portal_frame",  # none | block
    "portal_block_m",  # pier/lintel thickness
    "portal_depth_m",  # how far portal extends along road
    "portal_overhang_m",  # lateral oversize past road edge (mask hole cells)
    "portal_clear_extra_m",  # pier inset from road half-width (avoid cutting deck)
    "portal_collar",  # bool — terrain-oriented rock apron around portal
    "portal_collar_out_m",  # meters out of gallery along approach
    "portal_collar_side_m",  # meters into hillside from pier outer face
    "portal_collar_sink_m",  # bury outer verts into DGM (anti z-fight)
    "portal_collar_rings",  # radial samples from portal → terrain
    "debug_centerline",
    "enabled",
    "blend_open_dgm",
    "overhang_m",
)


def default_gallery_cfg(bng: dict) -> tuple[dict, list]:
    raw = bng.get("galleries") or {}
    defaults = {
        "width_m": float(raw.get("width_m") or 7.0),
        "width_from_road": bool(raw.get("width_from_road", True)),
        "clear_height_m": float(raw.get("clear_height_m") or 3.0),
        "roof_thickness_m": float(raw.get("roof_thickness_m") or 0.5),
        "wall_thickness_m": float(raw.get("wall_thickness_m") or 0.4),
        "extend_before_m": float(raw.get("extend_before_m") or 1.0),
        "extend_after_m": float(raw.get("extend_after_m") or 1.0),
        "step_m": float(raw.get("step_m") or 2.0),
        "crossfall_max": float(raw.get("crossfall_max") or 0.12),
        "open_side": str(raw.get("open_side") or "left"),
        # hermite: Z only from secured flat portals (grade-jump); never mid-gallery DGM
        # road_spline: Strassennetz + road_span_profile (4 MeshRoad strips)
        "z_profile": str(raw.get("z_profile") or "hermite"),
        "profile": str(raw.get("profile") or ""),
        "centerline": str(raw.get("centerline") or "osm"),
        "free_span": str(raw.get("free_span") or "linear"),
        "solid_run_m": float(raw.get("solid_run_m") or 3.0),
        "span_dip_m": float(raw.get("span_dip_m") or 0.45),
        "span_search_m": float(raw.get("span_search_m") or 40.0),
        "deck_lift_m": float(raw.get("deck_lift_m") or 0.02),
        "corner_up_m": float(
            raw["corner_up_m"] if raw.get("corner_up_m") is not None else 0.25
        ),
        "corner_down_m": float(
            raw["corner_down_m"] if raw.get("corner_down_m") is not None else 0.06
        ),
        "corner_band": float(
            raw["corner_band"] if raw.get("corner_band") is not None else 0.2
        ),
        "abutment_s": raw.get("abutment_s"),
        "portal_grade_step_m": float(raw.get("portal_grade_step_m") or 1.0),
        "portal_grade_jump": float(raw.get("portal_grade_jump") or 0.04),
        "portal_grade_baseline_n": int(raw.get("portal_grade_baseline_n") or 3),
        "portal_out_extra_m": float(raw.get("portal_out_extra_m") or 2.0),
        "portal_grade_span_m": float(raw.get("portal_grade_span_m") or 3.0),
        "portal_jump_min_grade": float(raw.get("portal_jump_min_grade") or 0.12),
        "portal_search_out_m": float(raw.get("portal_search_out_m") or 25.0),
        "portal_search_in_m": float(raw.get("portal_search_in_m") or 15.0),
        # portals = only Ein-/Ausfahrt (Galerie); corridor = full tunnel cut (later)
        "hole_mode": str(raw.get("hole_mode") or "portals"),
        "hole_pad_m": float(raw.get("hole_pad_m") or 0.0),
        "hole_portal_length_m": float(raw.get("hole_portal_length_m") or 6.0),
        "hole_out_bite_m": float(raw.get("hole_out_bite_m") or 2.0),
        "hole_above_road_m": float(
            raw.get("hole_above_road_m")
            if raw.get("hole_above_road_m") is not None
            else 0.5
        ),
        # None → clear_height + roof_thickness + 1.5 (set below after merge)
        "hole_max_above_m": (
            float(raw["hole_max_above_m"])
            if raw.get("hole_max_above_m") is not None
            else None
        ),
        "hole_width_scale": float(
            raw["hole_width_scale"]
            if raw.get("hole_width_scale") is not None
            else 1.0
        ),
        "deck_enabled": bool(raw.get("deck_enabled", True)),
        "deck_depth_m": float(raw.get("deck_depth_m") or 0.5),
        "deck_width_extra_m": float(raw.get("deck_width_extra_m") or 0.0),
        "deck_open_extra_m": float(raw.get("deck_open_extra_m") or 0.8),
        "deck_extend_m": float(raw.get("deck_extend_m") or 5.0),
        "parapet_height_m": float(raw.get("parapet_height_m") or 1.0),
        "parapet_thickness_m": float(raw.get("parapet_thickness_m") or 0.35),
        "parapet_out_m": float(
            raw["parapet_out_m"] if raw.get("parapet_out_m") is not None else 0.25
        ),
        "column_spacing_m": float(raw.get("column_spacing_m") or 4.0),
        "column_along_m": float(raw.get("column_along_m") or 0.4),
        "column_lat_m": float(raw.get("column_lat_m") or 0.35),
        # >0 = further toward parapet outer edge (from midline); 0 = centered on parapet
        "column_out_m": float(
            raw["column_out_m"] if raw.get("column_out_m") is not None else 0.0
        ),
        # Outside portals: C1 blend DGM approach → Hermite knot (kills grade slam)
        "approach_blend_m": float(raw.get("approach_blend_m") or 12.0),
        # marry = MeshRoad follows DGM outside (skin to terrain); hermite_blend = old float C1
        "approach_mode": str(raw.get("approach_mode") or "marry").lower(),
        "portal_ease_m": float(raw.get("portal_ease_m") or 4.0),
        "approach_lift_m": float(raw.get("approach_lift_m") or 0.015),
        "approach_conform": bool(raw.get("approach_conform", False)),
        "approach_conform_pad_m": float(raw.get("approach_conform_pad_m") or 0.5),
        "approach_conform_falloff_m": float(raw.get("approach_conform_falloff_m") or 1.5),
        "approach_conform_sink_m": float(raw.get("approach_conform_sink_m") or 0.02),
        "approach_conform_max_delta_m": float(
            raw.get("approach_conform_max_delta_m") or 0.35
        ),
        "portal_frame": str(raw.get("portal_frame") or "block"),
        "portal_block_m": float(raw.get("portal_block_m") or 0.7),
        "portal_depth_m": float(raw.get("portal_depth_m") or 1.2),
        "portal_overhang_m": float(raw.get("portal_overhang_m") or 0.8),
        "portal_clear_extra_m": float(raw.get("portal_clear_extra_m") or 0.2),
        "portal_collar": bool(raw.get("portal_collar", True)),
        "portal_collar_out_m": float(raw.get("portal_collar_out_m") or 5.0),
        "portal_collar_side_m": float(raw.get("portal_collar_side_m") or 7.0),
        "portal_collar_sink_m": float(raw.get("portal_collar_sink_m") or 0.1),
        "portal_collar_rings": int(raw.get("portal_collar_rings") or 4),
        "debug_centerline": bool(raw.get("debug_centerline", False)),
        "enabled": True,
        "blend_open_dgm": bool(raw.get("blend_open_dgm", True)),
        "overhang_m": float(raw.get("overhang_m") or 0.6),
        "style": {
            "shell": "gallery",
            "columns": "rect",  # none | rect — open-side posts on parapet
            "edge": "none",
            "portal": "block",
            **(raw.get("style") or {}),
        },
        "materials": {
            "top": "Asphalt",
            "bottom": "Concrete",
            "side": "Concrete",
            "collar": "GalleryRock",
            "texture_length": 4.0,
            "detail_scale": 4.0,
            **(raw.get("materials") or {}),
        },
    }
    if "defaults" in raw and isinstance(raw["defaults"], dict):
        d = raw["defaults"]
        for k in GALLERY_SCALAR_KEYS:
            if k in d:
                defaults[k] = d[k]
        if "style" in d:
            defaults["style"] = {**defaults["style"], **(d.get("style") or {})}
        if "materials" in d:
            defaults["materials"] = {**defaults["materials"], **(d.get("materials") or {})}
    if defaults.get("hole_max_above_m") is None:
        defaults["hole_max_above_m"] = (
            float(defaults["clear_height_m"])
            + float(defaults["roof_thickness_m"])
            + 1.5
        )
    else:
        defaults["hole_max_above_m"] = float(defaults["hole_max_above_m"])
    return defaults, list(raw.get("items") or [])



def _feat_objectids(feat: dict) -> set[int]:
    ids: set[int] = set()
    if feat.get("objectid") is not None:
        ids.add(int(feat["objectid"]))
    for x in feat.get("merged_from") or []:
        if x is not None:
            ids.add(int(x))
    return ids


def _match_objectid(m_oid, feat: dict) -> bool:
    """True if match.objectid (int or list) hits this feature or a merge constituent."""
    feat_oids = _feat_objectids(feat)
    if not feat_oids:
        return False
    if isinstance(m_oid, (list, tuple, set)):
        return any(int(x) in feat_oids for x in m_oid)
    return int(m_oid) in feat_oids


def resolve_gallery_cfg(defaults: dict, items: list, feat: dict) -> dict:
    cfg = dict(defaults)
    cfg["style"] = dict(defaults.get("style") or {})
    mats = dict(defaults.get("materials") or {})
    name = str(feat.get("name") or "")
    matched = None
    for item in items or []:
        m = item.get("match") or {}
        ok = True
        if "objectid" in m and not _match_objectid(m["objectid"], feat):
            ok = False
        if "name" in m and str(m["name"]).lower() not in name.lower():
            ok = False
        if "name_exact" in m and str(m["name_exact"]) != name:
            ok = False
        if ok and m:
            matched = item
            break
    if matched:
        for k in GALLERY_SCALAR_KEYS:
            if k in matched:
                cfg[k] = matched[k]
        if "style" in matched:
            cfg["style"] = {**cfg["style"], **(matched.get("style") or {})}
        if "materials" in matched:
            mats = {**mats, **(matched.get("materials") or {})}
        cfg["match_id"] = matched.get("id") or matched.get("match")
    cfg["materials"] = mats
    return cfg


def _poly_len_xy(xy: list[tuple[float, float]]) -> float:
    return sum(
        math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(xy, xy[1:])
    )


def _chain_abutting_polylines(
    segs: list[list[tuple[float, float]]],
    *,
    tol_m: float,
) -> list[tuple[float, float]]:
    """Concatenate polylines that share endpoints into one path."""
    if not segs:
        return []
    if len(segs) == 1:
        return list(segs[0])

    remaining = [list(s) for s in segs]
    # Start with longest fragment
    remaining.sort(key=_poly_len_xy, reverse=True)
    chain = remaining.pop(0)

    def _d(p, q) -> float:
        return math.hypot(p[0] - q[0], p[1] - q[1])

    while remaining:
        attached = False
        for i, seg in enumerate(remaining):
            variants = (seg, list(reversed(seg)))
            for pts in variants:
                if _d(chain[-1], pts[0]) <= tol_m:
                    # drop duplicate junction vertex
                    chain.extend(pts[1:] if _d(chain[-1], pts[0]) <= tol_m else pts)
                    remaining.pop(i)
                    attached = True
                    break
                if _d(chain[0], pts[-1]) <= tol_m:
                    chain = pts[:-1] + chain
                    remaining.pop(i)
                    attached = True
                    break
            if attached:
                break
        if not attached:
            # orphan fragment — keep longest chain, drop rest
            break
    return chain


def merge_abutting_gallery_features(
    feats: list[dict],
    *,
    tol_m: float = 15.0,
    same_name: bool = True,
    groups: list[list[int]] | None = None,
) -> list[dict]:
    """Merge GIP gallery/tunnel segments that form one physical structure.

    GIP often splits one gallery into abutting OBJECTIDs (e.g. Mugkögele
    7236+10099). Building them separately puts a Hermite portal at the
    joint. Merge first → one centerline, portals only at true outer ends.

    ``groups``: optional explicit OBJECTID lists from site YAML. Otherwise
    auto-merge same-name features whose endpoints lie within ``tol_m``.
    """
    if len(feats) < 2:
        return feats

    n = len(feats)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    oid_to_idx: dict[int, int] = {}
    for i, f in enumerate(feats):
        if f.get("objectid") is not None:
            oid_to_idx[int(f["objectid"])] = i

    # Explicit merge groups from YAML
    for grp in groups or []:
        idxs = [oid_to_idx[int(x)] for x in grp if int(x) in oid_to_idx]
        for a, b in zip(idxs, idxs[1:]):
            union(a, b)

    # Auto: same name + abutting endpoints
    for i in range(n):
        for j in range(i + 1, n):
            if feats[i].get("kind") != feats[j].get("kind"):
                continue
            if same_name and feats[i].get("name") != feats[j].get("name"):
                continue
            ends_i = (feats[i]["xy"][0], feats[i]["xy"][-1])
            ends_j = (feats[j]["xy"][0], feats[j]["xy"][-1])
            if any(
                math.hypot(a[0] - b[0], a[1] - b[1]) <= tol_m
                for a in ends_i
                for b in ends_j
            ):
                union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    out: list[dict] = []
    for idxs in clusters.values():
        if len(idxs) == 1:
            out.append(feats[idxs[0]])
            continue
        members = [feats[i] for i in idxs]
        # Primary id = longest GIP fragment (stable mesh name)
        primary = max(
            members,
            key=lambda f: float(f.get("length_m") or _poly_len_xy(f["xy"])),
        )
        xy = _chain_abutting_polylines([list(f["xy"]) for f in members], tol_m=tol_m)
        if len(xy) < 2:
            out.extend(members)
            continue
        oids = sorted(
            {int(f["objectid"]) for f in members if f.get("objectid") is not None}
        )
        lengths = [float(f["length_m"]) for f in members if f.get("length_m") is not None]
        merged = {
            "kind": primary["kind"],
            "name": primary["name"],
            "objectid": primary.get("objectid"),
            "merged_from": oids,
            "length_m": sum(lengths) if lengths else _poly_len_xy(xy),
            "xy": xy,
        }
        out.append(merged)
        print(
            f"merged gallery '{merged['name']}' objectids={oids} "
            f"→ primary={merged['objectid']} span≈{_poly_len_xy(xy):.1f}m"
        )
    return out


def gallery_features(site: dict, sc: SiteCoords) -> list[dict]:
    path = bb.find_gip_geojson(site)
    data = json.loads(path.read_text(encoding="utf-8"))
    from pyproj import Transformer

    to_site = Transformer.from_crs("EPSG:4326", sc.crs, always_xy=True)
    out = []
    for f in data.get("features") or []:
        props = f.get("properties") or {}
        kind = bb._structure_kind(props)
        if kind not in ("gallery", "tunnel"):
            continue
        raw_pts: list = []
        bb._walk_coords((f.get("geometry") or {}).get("coordinates"), raw_pts)
        xy = []
        for lon, lat, *_ in raw_pts:
            x, y = to_site.transform(float(lon), float(lat))
            bx, by = sc.crs_to_beamng(x, y)
            if -50 <= bx <= sc.terrain_extent + 50 and -50 <= by <= sc.terrain_extent + 50:
                xy.append((bx, by))
        if len(xy) < 2:
            continue
        out.append(
            {
                "kind": kind,
                "name": str(props.get("KUNSTBAUTEN") or kind),
                "objectid": props.get("OBJECTID"),
                "length_m": props.get("Shape__Length"),
                "xy": xy,
            }
        )
    return out


def _is_carriageway(road: dict) -> bool:
    hwy = str(road.get("highway") or "")
    return hwy not in {"path", "footway", "cycleway", "steps", "track"}


def _rebuild_road_cum(pts: list[tuple[float, float, float, float]]) -> dict:
    cum = [0.0]
    for a, b in zip(pts, pts[1:]):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    return {"pts": pts, "cum": cum, "length": cum[-1]}


def extend_road_chain(
    roads: list[dict],
    seed: dict,
    *,
    tol_m: float = 1.0,
    pad_m: float = 40.0,
) -> dict:
    """Merge OSM way fragments that share endpoints into one polyline.

    ``roads_beamng`` often splits one carriageway into many short ways; gallery
    portal search needs the approaches *beyond* the GIP fragment.
    Grows from ``seed`` until both ends have ≥ ``pad_m`` of approach (or no link).
    """
    pool = [r for r in roads if _is_carriageway(r)]
    pts: list[tuple[float, float, float, float]] = [tuple(p) for p in seed["pts"]]
    used = {str(seed["id"])}
    seed_start = (float(seed["pts"][0][0]), float(seed["pts"][0][1]))
    seed_end = (float(seed["pts"][-1][0]), float(seed["pts"][-1][1]))

    def _end_dist(p, q) -> float:
        return math.hypot(p[0] - q[0], p[1] - q[1])

    def _as_road(pts_local: list) -> dict:
        meta = _rebuild_road_cum(pts_local)
        return {
            "id": seed["id"],
            "name": seed.get("name"),
            "highway": seed.get("highway"),
            **meta,
        }

    def _try_grow(at_start: bool) -> bool:
        tip = pts[0] if at_start else pts[-1]
        best = None  # (dist, road, neighbor_start_touches_tip)
        for r in pool:
            if str(r["id"]) in used:
                continue
            d0 = _end_dist(tip, r["pts"][0])
            d1 = _end_dist(tip, r["pts"][-1])
            if d0 <= tol_m and (best is None or d0 < best[0]):
                best = (d0, r, True)
            if d1 <= tol_m and (best is None or d1 < best[0]):
                best = (d1, r, False)
        if best is None:
            return False
        _d, r, at_r_start = best
        used.add(str(r["id"]))
        nbr = [tuple(p) for p in r["pts"]]
        if at_start:
            if at_r_start:
                nbr = list(reversed(nbr))
            pts[:0] = nbr[:-1]
        else:
            if not at_r_start:
                nbr = list(reversed(nbr))
            pts.extend(nbr[1:])
        return True

    for _ in range(32):
        road_tmp = _as_road(pts)
        s_a = bb.project_on_road(road_tmp, seed_start[0], seed_start[1])[0]
        s_b = bb.project_on_road(road_tmp, seed_end[0], seed_end[1])[0]
        s_lo, s_hi = (s_a, s_b) if s_a <= s_b else (s_b, s_a)
        before = s_lo
        after = float(road_tmp["length"]) - s_hi
        if before < pad_m and _try_grow(True):
            continue
        if after < pad_m and _try_grow(False):
            continue
        break

    out = _as_road(pts)
    out["chain_ids"] = sorted(used)
    out["seed_id"] = seed["id"]
    return out


def _sample_dgm_polyline(
    road: dict,
    z_terrain,
    s_lo: float,
    s_hi: float,
    step: float,
) -> list[tuple[float, float, float, float]]:
    """[(s, x, y, z_dgm), ...] along road XY."""
    s_max = float(road["length"])
    s_lo = max(0.0, min(s_max, s_lo))
    s_hi = max(0.0, min(s_max, s_hi))
    if s_hi < s_lo:
        s_lo, s_hi = s_hi, s_lo
    out: list[tuple[float, float, float, float]] = []
    s = s_lo
    while s <= s_hi + 1e-9:
        x, y, _zr, _tx, _ty, _tz, _w = bb.sample_road(road, s)
        z = float(z_terrain(x, y)) if z_terrain is not None else float(_zr)
        out.append((s, x, y, z))
        s += step
    if not out or abs(out[-1][0] - s_hi) > 0.01:
        x, y, _zr, _tx, _ty, _tz, _w = bb.sample_road(road, s_hi)
        z = float(z_terrain(x, y)) if z_terrain is not None else float(_zr)
        out.append((s_hi, x, y, z))
    return out


def find_secured_portal(
    road: dict,
    s_gip: float,
    *,
    end: str,
    z_terrain,
    search_out_m: float = 25.0,
    search_in_m: float = 15.0,
    step: float = 1.0,
    jump_dp: float = 0.04,
    baseline_n: int = 3,
    out_extra_m: float = 1.0,
    grade_span_m: float = 3.0,
    jump_min_grade: float = 0.12,
) -> dict:
    """Secured flat road just outside the gallery via relative DGM grade jump.

    Walk outside → inside. Grades are spanned over ``grade_span_m`` to ignore
    1 m heightmap terracing (0%/15%/0%). A portal jump must be a *rise into*
    the structure (along the walk), at least ``jump_min_grade``, not a flat
    terrace after a mild climb.
    """
    s_max = float(road["length"])
    s_gip = max(0.0, min(s_max, float(s_gip)))

    if z_terrain is None:
        x, y, zr, tx, ty, tz, w = bb.sample_road(road, s_gip)
        return {
            "s": round(s_gip, 3),
            "z": round(zr, 3),
            "z_terr": round(zr, 3),
            "grade": 0.0,
            "jump_s": None,
            "jump_grade": None,
            "method": "no_terrain",
            "x": round(x, 3),
            "y": round(y, 3),
            "width": round(w, 2),
            "tx": round(tx, 5),
            "ty": round(ty, 5),
            "tz": round(tz, 5),
        }

    half = 0.5 * max(step, float(grade_span_m))

    def _z_at(s: float) -> float:
        s = max(0.0, min(s_max, s))
        x, y, zr, _tx, _ty, _tz, _w = bb.sample_road(road, s)
        return float(z_terrain(x, y))

    def _grade_plus(s: float) -> float:
        """dz/ds along +chainage, spanning ``grade_span_m``."""
        return (_z_at(s + half) - _z_at(s - half)) / max(2.0 * half, 1e-3)

    if end == "s0":
        s_a = max(0.0, s_gip - search_out_m)
        s_b = min(s_max, s_gip + search_in_m)
        s_samples = _s_range(s_a, s_b, step)
        # walk +s into gallery; climb onto roof ⇒ grade_+s increases
        walk_sign = +1.0
    else:
        s_a = max(0.0, s_gip - search_in_m)
        s_b = min(s_max, s_gip + search_out_m)
        s_samples = list(reversed(_s_range(s_a, s_b, step)))
        # walk −s into gallery; climb onto roof ⇒ grade_+s decreases ⇒ walk grade rises
        walk_sign = -1.0

    # (s, grade_along_walk)
    grades: list[tuple[float, float]] = []
    for s in s_samples:
        g_plus = _grade_plus(s)
        g_walk = walk_sign * g_plus
        grades.append((s, g_walk))

    jump_idx: int | None = None
    baseline_grades: list[float] = []
    min_base = max(2, min(baseline_n, 2))
    for i, (_s, g) in enumerate(grades):
        if len(baseline_grades) < min_base:
            baseline_grades.append(g)
            continue
        n_b = min(baseline_n, len(baseline_grades))
        base = sum(baseline_grades[-n_b:]) / float(n_b)
        # Only a rise into the gallery (not terrace 15%→0% noise)
        if g >= base + jump_dp and g >= jump_min_grade:
            jump_idx = i
            break
        baseline_grades.append(g)

    g_jump = None
    s_jump = None
    if jump_idx is None:
        method = "fallback_gip"
        if end == "s0":
            s_sec = max(0.0, s_gip - out_extra_m)
        else:
            s_sec = min(s_max, s_gip + out_extra_m)
        base_g = 0.0
    else:
        method = "grade_jump"
        s_jump, g_jump = grades[jump_idx]
        if end == "s0":
            s_sec = max(0.0, float(s_jump) - out_extra_m)
        else:
            s_sec = min(s_max, float(s_jump) + out_extra_m)
        n_b = min(baseline_n, len(baseline_grades)) or 1
        base_g = sum(baseline_grades[-n_b:]) / float(n_b) if baseline_grades else 0.0
        # Store approach grade along +chainage for Hermite
        base_g = walk_sign * base_g

    x, y, _zr, tx, ty, tz, w = bb.sample_road(road, s_sec)
    z = float(z_terrain(x, y))

    return {
        "s": round(s_sec, 3),
        "z": round(z, 3),
        "z_terr": round(z, 3),
        "grade": round(base_g, 5),
        "jump_s": None if s_jump is None else round(float(s_jump), 3),
        "jump_grade": None if g_jump is None else round(float(g_jump), 5),
        "method": method,
        "x": round(x, 3),
        "y": round(y, 3),
        "width": round(w, 2),
        "tx": round(tx, 5),
        "ty": round(ty, 5),
        "tz": round(tz, 5),
    }


def _gallery_uses_road_spline(cfg: dict) -> bool:
    prof = str(cfg.get("profile") or "").lower().strip()
    zprof = str(cfg.get("z_profile") or "").lower().strip()
    return prof == "road_spline" or zprof == "road_spline"


def _shift_xy_open(
    x: float,
    y: float,
    tx: float,
    ty: float,
    open_side: str,
    open_extra_m: float,
) -> tuple[float, float]:
    """Shift so only the open edge grows when width already includes open_extra."""
    extra = max(0.0, float(open_extra_m))
    side = (open_side or "").lower().strip()
    if extra < 1e-9 or side not in ("left", "right"):
        return x, y
    left_u, right_u = bb.left_right_unit(tx, ty)
    u = left_u if side == "left" else right_u
    return x + 0.5 * extra * u[0], y + 0.5 * extra * u[1]


def build_gallery_road_spline(
    feat: dict,
    road: dict,
    z_terrain,
    cfg: dict,
) -> tuple[list[dict], list[dict], dict]:
    """Strassennetz + road_span_profile → shell nodes + 4 MeshRoad strips.

    Abutments = secured portals (grade jump into roof), not bridge dip heuristic —
    mid-gallery DGM is polluted by the roof.
    """
    import road_span_profile as rsp

    clear_h = float(cfg["clear_height_m"])
    roof_t = float(cfg["roof_thickness_m"])
    width_base = float(cfg["width_m"])
    w_extra = max(0.0, float(cfg.get("deck_width_extra_m") or 0.0))
    open_extra = max(0.0, float(cfg.get("deck_open_extra_m") or 0.0))
    open_side = str(cfg.get("open_side") or "left")
    # Match legacy single MeshRoad: widen + shift toward open side
    width_eff = width_base + w_extra + open_extra

    s_ends = [rsp.project_xy(road, x, y)[0] for x, y in (feat["xy"][0], feat["xy"][-1])]
    s_g0, s_g1 = min(s_ends), max(s_ends)

    abut = cfg.get("abutment_s")
    portal_anchors: dict = {}
    if abut is not None and len(abut) >= 2:
        s_p0, s_p1 = float(abut[0]), float(abut[1])
        if s_p1 < s_p0:
            s_p0, s_p1 = s_p1, s_p0
        portal_method = "override"
    else:
        p0 = find_secured_portal(
            road,
            s_g0,
            end="s0",
            z_terrain=z_terrain,
            search_out_m=float(cfg.get("portal_search_out_m") or 25.0),
            search_in_m=float(cfg.get("portal_search_in_m") or 15.0),
            step=float(cfg.get("portal_grade_step_m") or 1.0),
            jump_dp=float(cfg.get("portal_grade_jump") or 0.04),
            baseline_n=int(cfg.get("portal_grade_baseline_n") or 3),
            out_extra_m=float(cfg.get("portal_out_extra_m") or 2.0),
            grade_span_m=float(cfg.get("portal_grade_span_m") or 3.0),
            jump_min_grade=float(cfg.get("portal_jump_min_grade") or 0.12),
        )
        p1 = find_secured_portal(
            road,
            s_g1,
            end="s1",
            z_terrain=z_terrain,
            search_out_m=float(cfg.get("portal_search_out_m") or 25.0),
            search_in_m=float(cfg.get("portal_search_in_m") or 15.0),
            step=float(cfg.get("portal_grade_step_m") or 1.0),
            jump_dp=float(cfg.get("portal_grade_jump") or 0.04),
            baseline_n=int(cfg.get("portal_grade_baseline_n") or 3),
            out_extra_m=float(cfg.get("portal_out_extra_m") or 2.0),
            grade_span_m=float(cfg.get("portal_grade_span_m") or 3.0),
            jump_min_grade=float(cfg.get("portal_jump_min_grade") or 0.12),
        )
        s_p0, s_p1 = float(p0["s"]), float(p1["s"])
        if s_p1 < s_p0:
            p0, p1 = p1, p0
            s_p0, s_p1 = s_p1, s_p0
        portal_anchors = {"s0": p0, "s1": p1}
        portal_method = "grade_jump"

    deck_ext = float(cfg.get("deck_extend_m") or 0.0)
    ext_b = max(float(cfg.get("extend_before_m") or 0.0), deck_ext)
    ext_a = max(float(cfg.get("extend_after_m") or 0.0), deck_ext)

    prof = rsp.build_span_profile(
        road,
        z_terrain,
        feat["xy"],
        width_m=width_eff,
        dip_m=float(cfg.get("span_dip_m") or 0.45),
        search_m=float(cfg.get("span_search_m") or 40.0),
        step_m=float(cfg.get("step_m") or 1.0),
        solid_run_m=float(cfg.get("solid_run_m") or 3.0),
        extend_before_m=ext_b,
        extend_after_m=ext_a,
        deck_lift_m=float(cfg.get("deck_lift_m") or 0.0),
        abutment_s=(s_p0, s_p1),
        free_span=str(cfg.get("free_span") or "linear"),
        corner_up_m=float(cfg.get("corner_up_m") if cfg.get("corner_up_m") is not None else 0.25),
        corner_down_m=float(
            cfg.get("corner_down_m") if cfg.get("corner_down_m") is not None else 0.06
        ),
        corner_band=float(cfg.get("corner_band") if cfg.get("corner_band") is not None else 0.2),
    )

    # Shell / portal nodes: width for loft = base (+ width_extra on closed sense via loft args)
    nodes: list[dict] = []
    for k, s in enumerate(prof.s):
        x, y = prof.xy[k]
        _xr, _yr, _zn, tx, ty, _tz, _w = rsp.sample_road_xy(road, s)
        x, y = _shift_xy_open(x, y, tx, ty, open_side, open_extra)
        z = float(prof.z_center[k])
        nodes.append(
            {
                "x": round(x, 3),
                "y": round(y, 3),
                "z_road": round(z, 3),
                "z_hermite": round(z, 3),
                "z_roof_inner": round(z + clear_h, 3),
                "z_roof_outer": round(z + clear_h + roof_t, 3),
                "width": round(width_base + w_extra, 2),
                "tx": round(tx, 5),
                "ty": round(ty, 5),
                "s": round(float(s), 2),
            }
        )

    slug = bb._slug(str(feat.get("name") or "gallery"))
    oid = feat.get("objectid") or "x"
    depth = float(cfg.get("deck_depth_m") or cfg.get("depth_m") or 0.5)
    mats = cfg.get("materials") or {}
    strip_entries: list[dict] = []
    for i, strip in enumerate(prof.strips):
        shifted = []
        for j, n in enumerate(strip):
            sx, sy, sz, sw, nx, ny, nz = n
            s = float(prof.s[j])
            _xr, _yr, _zn, tx, ty, _tz, _w = rsp.sample_road_xy(road, s)
            sx, sy = _shift_xy_open(sx, sy, tx, ty, open_side, open_extra)
            shifted.append((sx, sy, sz, sw, nx, ny, nz))
        entry = bb.make_meshroad_from_strip(
            f"gallery_deck_{slug}_{oid}_s{i}",
            shifted,
            depth_m=depth,
            materials=mats,
        )
        entry["__parent"] = "galleries"
        strip_entries.append(entry)

    z0 = float(prof.z_center[min(range(len(prof.s)), key=lambda i: abs(prof.s[i] - s_p0))])
    z1 = float(prof.z_center[min(range(len(prof.s)), key=lambda i: abs(prof.s[i] - s_p1))])

    info = dict(prof.info)
    info.update(
        {
            "road_id": road.get("id"),
            "road_name": road.get("name"),
            "z_profile": "road_spline",
            "profile": "road_spline",
            "centerline": "strassennetz",
            "portal_s": [round(s_p0, 3), round(s_p1, 3)],
            "portal_z": [round(z0, 3), round(z1, 3)],
            "portal_anchors": portal_anchors,
            "portal_method": portal_method,
            "gip_len_on_road_m": round(s_g1 - s_g0, 2),
            "clear_height_m": clear_h,
            "roof_thickness_m": roof_t,
            "wall_thickness_m": float(cfg.get("wall_thickness_m") or 0.4),
            "overhang_m": float(cfg.get("overhang_m") or 0.6),
            "open_side": open_side,
            "hole_mode": cfg.get("hole_mode"),
            "hole_pad_m": cfg.get("hole_pad_m"),
            "hole_portal_length_m": cfg.get("hole_portal_length_m"),
            "blend_open_dgm": cfg.get("blend_open_dgm"),
            "style": cfg.get("style") or {},
            "materials": mats,
            "deck_strips": len(strip_entries),
            "width_eff_m": round(width_eff, 2),
            "extend_before_m": ext_b,
            "extend_after_m": ext_a,
            "approach_mode": "road_spline_terrain",
            "approach_conform": bool(cfg.get("approach_conform", False)),
        }
    )
    return nodes, strip_entries, info


def build_gallery_centerline(
    xy_gip: list[tuple[float, float]],
    road: dict,
    cfg: dict,
    *,
    z_terrain=None,
) -> tuple[list[dict], dict]:
    """XY on OSM road; Z = Hermite between secured flat portals only.

    Portals: relative DGM grade jump outside→inside, then ``portal_out_extra_m``
    further out. No mid-gallery DGM knots (that is the roof).
    """
    width_fallback = float(cfg["width_m"])
    width_from_road = bool(cfg.get("width_from_road", True))
    ext_b = float(cfg["extend_before_m"])
    ext_a = float(cfg["extend_after_m"])
    step = float(cfg.get("step_m") or 2.0)
    clear_h = float(cfg["clear_height_m"])
    roof_t = float(cfg["roof_thickness_m"])
    z_profile = str(cfg.get("z_profile") or "hermite").lower()

    s_ends = [bb.project_on_road(road, x, y)[0] for x, y in (xy_gip[0], xy_gip[-1])]
    s_g0, s_g1 = min(s_ends), max(s_ends)
    s_first = bb.project_on_road(road, xy_gip[0][0], xy_gip[0][1])[0]
    s_last = bb.project_on_road(road, xy_gip[-1][0], xy_gip[-1][1])[0]
    forward = s_last >= s_first

    p0 = find_secured_portal(
        road,
        s_g0,
        end="s0",
        z_terrain=z_terrain,
        search_out_m=float(cfg.get("portal_search_out_m") or 25.0),
        search_in_m=float(cfg.get("portal_search_in_m") or 15.0),
        step=float(cfg.get("portal_grade_step_m") or 1.0),
        jump_dp=float(cfg.get("portal_grade_jump") or 0.04),
        baseline_n=int(cfg.get("portal_grade_baseline_n") or 3),
        out_extra_m=float(cfg.get("portal_out_extra_m") or 2.0),
        grade_span_m=float(cfg.get("portal_grade_span_m") or 3.0),
        jump_min_grade=float(cfg.get("portal_jump_min_grade") or 0.12),
    )
    p1 = find_secured_portal(
        road,
        s_g1,
        end="s1",
        z_terrain=z_terrain,
        search_out_m=float(cfg.get("portal_search_out_m") or 25.0),
        search_in_m=float(cfg.get("portal_search_in_m") or 15.0),
        step=float(cfg.get("portal_grade_step_m") or 1.0),
        jump_dp=float(cfg.get("portal_grade_jump") or 0.04),
        baseline_n=int(cfg.get("portal_grade_baseline_n") or 3),
        out_extra_m=float(cfg.get("portal_out_extra_m") or 2.0),
        grade_span_m=float(cfg.get("portal_grade_span_m") or 3.0),
        jump_min_grade=float(cfg.get("portal_jump_min_grade") or 0.12),
    )

    s_p0 = float(p0["s"])
    s_p1 = float(p1["s"])
    if s_p1 < s_p0:
        p0, p1 = p1, p0
        s_p0, s_p1 = s_p1, s_p0
    z0 = float(p0["z"])
    z1 = float(p1["z"])
    m0 = float(p0.get("grade") or 0.0)
    m1 = float(p1.get("grade") or 0.0)
    length_p = max(s_p1 - s_p0, 1e-3)
    m_mean = (z1 - z0) / length_p
    # Approach grade only if it looks like a road (not a roof cliff sample)
    max_approach = 0.20  # 20%
    if str(p0.get("method") or "") != "grade_jump" or abs(m0) > max_approach:
        m0 = m_mean
    if str(p1.get("method") or "") != "grade_jump" or abs(m1) > max_approach:
        m1 = m_mean

    w0 = float(p0["width"]) if width_from_road else width_fallback
    w1 = float(p1["width"]) if width_from_road else width_fallback

    approach_mode = str(cfg.get("approach_mode") or "marry").lower()
    if approach_mode in ("hermite", "float", "c1"):
        approach_mode = "hermite_blend"
    blend_m = float(cfg.get("approach_blend_m") or 12.0)
    blend_m = max(0.0, blend_m)
    ease_m = float(cfg.get("portal_ease_m") or 4.0)
    ease_m = max(0.0, min(ease_m, 0.45 * length_p))
    lift_m = float(cfg.get("approach_lift_m") or 0.0)
    s_max_r = float(road["length"])
    grade_span = float(cfg.get("portal_grade_span_m") or 3.0)

    def _dgm_z(s_q: float) -> float:
        s_q = max(0.0, min(s_max_r, s_q))
        xq, yq, zr, *_rest = bb.sample_road(road, s_q)
        if z_terrain is None:
            return float(zr)
        return float(z_terrain(xq, yq))

    def _dgm_grade(s_q: float) -> float:
        half = 0.5 * max(1.0, grade_span)
        return (_dgm_z(s_q + half) - _dgm_z(s_q - half)) / max(2.0 * half, 1e-3)

    # Marry: portal grades must match local DGM so MeshRoad C1-skins to terrain
    if approach_mode == "marry" and z_terrain is not None:
        m0 = _dgm_grade(s_p0)
        m1 = _dgm_grade(s_p1)
        if abs(m0) > max_approach:
            m0 = m_mean
        if abs(m1) > max_approach:
            m1 = m_mean

    s_out0 = s_p0 - blend_m
    s_out1 = s_p1 + blend_m
    z_out0 = _dgm_z(s_out0) if blend_m > 1e-6 else z0
    z_out1 = _dgm_z(s_out1) if blend_m > 1e-6 else z1
    m_out0 = _dgm_grade(s_out0) if blend_m > 1e-6 else m0
    m_out1 = _dgm_grade(s_out1) if blend_m > 1e-6 else m1
    max_g = 0.25
    if abs(m_out0) > max_g:
        m_out0 = math.copysign(max_g, m_out0) if m_out0 != 0 else 0.0
    if abs(m_out1) > max_g:
        m_out1 = math.copysign(max_g, m_out1) if m_out1 != 0 else 0.0

    # Span: deck follows terrain past portals (marry) or covers float blend
    cover_out = max(ext_b, blend_m if approach_mode == "hermite_blend" else 0.0, 8.0)
    cover_in = max(ext_a, blend_m if approach_mode == "hermite_blend" else 0.0, 8.0)
    if forward:
        s0 = max(0.0, s_p0 - cover_out)
        s1 = min(s_max_r, s_p1 + cover_in)
    else:
        s0 = max(0.0, s_p0 - cover_in)
        s1 = min(s_max_r, s_p1 + cover_out)
    if s1 - s0 < step:
        s1 = min(s_max_r, s0 + step)

    x0, y0, _zr0, tx0, ty0, _tz0, _w0r = bb.sample_road(road, s0)
    x1, y1, _zr1, tx1, ty1, _tz1, _w1r = bb.sample_road(road, s1)

    # Dense samples near portals / ease / (legacy) outside blend
    step_near = min(step, 1.0)
    s_set: set[float] = set()
    s = s0
    while s <= s1 + 1e-9:
        s_set.add(round(s, 6))
        s += step
    s_set.add(round(s1, 6))
    dense_spans: list[tuple[float, float]] = []
    if approach_mode == "hermite_blend" and blend_m > 1e-6:
        dense_spans.extend(((s_p0 - blend_m, s_p0), (s_p1, s_p1 + blend_m)))
    if approach_mode == "marry" and ease_m > 1e-6:
        dense_spans.extend(((s_p0, s_p0 + ease_m), (s_p1 - ease_m, s_p1)))
        dense_spans.extend(((s_p0 - ease_m, s_p0), (s_p1, s_p1 + ease_m)))
        # also densify approaches so MeshRoad tracks DGM waves
        dense_spans.extend(((s0, s_p0), (s_p1, s1)))
    for s_lo, s_hi in dense_spans:
        s = max(s0, s_lo)
        s_end = min(s1, s_hi)
        while s <= s_end + 1e-9:
            s_set.add(round(s, 6))
            s += step_near
        s_set.add(round(s_end, 6))
    s_vals = sorted(s_set)

    # Precompute interior Hermite end slopes for portal ease (marry)
    m_ease0 = m0
    m_ease1 = m1
    z_ease0 = z0
    z_ease1 = z1
    if approach_mode == "marry" and ease_m > 1e-6:
        t_e0 = ease_m / length_p
        t_e1 = 1.0 - ease_m / length_p
        z_ease0 = bb.hermite_z(t_e0, z0, z1, m0, m1, length_p)
        z_ease1 = bb.hermite_z(t_e1, z0, z1, m0, m1, length_p)
        m_ease0 = bb.hermite_dz_ds(t_e0, z0, z1, m0, m1, length_p)
        m_ease1 = bb.hermite_dz_ds(t_e1, z0, z1, m0, m1, length_p)

    nodes: list[dict] = []
    for s in s_vals:
        x, y, z_rib, tx, ty, _tz, w_rib = bb.sample_road(road, s)
        t_mesh = 0.0 if (s1 - s0) < 1e-9 else (s - s0) / (s1 - s0)
        if z_profile == "road":
            z_surf = z_rib
            z_herm = z_rib
        else:
            z_dgm = float(z_terrain(x, y)) if z_terrain is not None else float(z_rib)
            if s < s_p0 - 1e-9:
                z_herm = z0 + m0 * (s - s_p0)
                if approach_mode == "hermite_blend" and blend_m > 1e-6 and s >= s_out0 - 1e-9:
                    t = max(0.0, min(1.0, (s - s_out0) / blend_m))
                    z_surf = bb.hermite_z(t, z_out0, z0, m_out0, m0, blend_m)
                elif approach_mode == "marry" and ease_m > 1e-6 and s >= s_p0 - ease_m - 1e-9:
                    # short C1 just outside: DGM skin → portal (kills grade slam)
                    s_a = s_p0 - ease_m
                    z_a = _dgm_z(s_a) + lift_m
                    m_a = _dgm_grade(s_a)
                    if abs(m_a) > max_g:
                        m_a = math.copysign(max_g, m_a) if m_a != 0 else 0.0
                    te = (s - s_a) / ease_m
                    te = max(0.0, min(1.0, te))
                    z_surf = bb.hermite_z(te, z_a, z0, m_a, m0, ease_m)
                else:
                    dist = s_p0 - s
                    taper = 2.0
                    lift_eff = (
                        lift_m * min(1.0, dist / taper) if taper > 1e-6 else lift_m
                    )
                    z_surf = z_dgm + lift_eff
            elif s > s_p1 + 1e-9:
                z_herm = z1 + m1 * (s - s_p1)
                if approach_mode == "hermite_blend" and blend_m > 1e-6 and s <= s_out1 + 1e-9:
                    t = max(0.0, min(1.0, (s - s_p1) / blend_m))
                    z_surf = bb.hermite_z(t, z1, z_out1, m1, m_out1, blend_m)
                elif approach_mode == "marry" and ease_m > 1e-6 and s <= s_p1 + ease_m + 1e-9:
                    s_b = s_p1 + ease_m
                    z_b = _dgm_z(s_b) + lift_m
                    m_b = _dgm_grade(s_b)
                    if abs(m_b) > max_g:
                        m_b = math.copysign(max_g, m_b) if m_b != 0 else 0.0
                    te = (s - s_p1) / ease_m
                    te = max(0.0, min(1.0, te))
                    z_surf = bb.hermite_z(te, z1, z_b, m1, m_b, ease_m)
                else:
                    dist = s - s_p1
                    taper = 2.0
                    lift_eff = (
                        lift_m * min(1.0, dist / taper) if taper > 1e-6 else lift_m
                    )
                    z_surf = z_dgm + lift_eff
            else:
                t = (s - s_p0) / length_p
                z_herm = bb.hermite_z(t, z0, z1, m0, m1, length_p)
                if approach_mode == "marry" and ease_m > 1e-6 and s <= s_p0 + ease_m + 1e-9:
                    # short C1 from portal (DGM grade) into interior Hermite
                    te = (s - s_p0) / ease_m
                    te = max(0.0, min(1.0, te))
                    z_surf = bb.hermite_z(te, z0, z_ease0, m0, m_ease0, ease_m)
                elif approach_mode == "marry" and ease_m > 1e-6 and s >= s_p1 - ease_m - 1e-9:
                    te = (s - (s_p1 - ease_m)) / ease_m
                    te = max(0.0, min(1.0, te))
                    z_surf = bb.hermite_z(te, z_ease1, z1, m_ease1, m1, ease_m)
                else:
                    z_surf = z_herm
        w = w_rib if width_from_road else (w0 + t_mesh * (w1 - w0))
        if t_mesh <= 1e-9:
            tx, ty = tx0, ty0
        elif t_mesh >= 1.0 - 1e-9:
            tx, ty = tx1, ty1
        nodes.append(
            {
                "x": round(x, 3),
                "y": round(y, 3),
                "z_road": round(z_surf, 3),
                "z_hermite": round(z_herm, 3),
                "z_roof_inner": round(z_surf + clear_h, 3),
                "z_roof_outer": round(z_surf + clear_h + roof_t, 3),
                "width": round(w, 2),
                "tx": round(tx, 5),
                "ty": round(ty, 5),
                "s": round(s, 2),
            }
        )

    info = {
        "road_id": road["id"],
        "road_name": road.get("name"),
        "s0": round(s0, 2),
        "s1": round(s1, 2),
        "gip_s0": round(s_g0, 2),
        "gip_s1": round(s_g1, 2),
        "extend_before_m": ext_b,
        "extend_after_m": ext_a,
        "approach_mode": approach_mode,
        "approach_blend_m": round(blend_m, 2),
        "portal_ease_m": round(ease_m, 2),
        "approach_lift_m": round(lift_m, 4),
        "approach_outer": {
            "s0": [round(s_out0, 3), round(z_out0, 3), round(m_out0, 5)],
            "s1": [round(s_out1, 3), round(z_out1, 3), round(m_out1, 5)],
        },
        "span_len_m": round(s1 - s0, 2),
        "gip_len_on_road_m": round(s_g1 - s_g0, 2),
        "z_profile": z_profile,
        "portal_dz_ds": [round(m0, 5), round(m1, 5)],
        "portal_z": [round(z0, 3), round(z1, 3)],
        "portal_s": [round(s_p0, 3), round(s_p1, 3)],
        "portal_width_m": [round(w0, 2), round(w1, 2)],
        "portal_anchors": {"s0": p0, "s1": p1},
        "clear_height_m": clear_h,
        "roof_thickness_m": roof_t,
        "wall_thickness_m": float(cfg.get("wall_thickness_m") or 0.4),
        "overhang_m": float(cfg.get("overhang_m") or 0.6),
        "open_side": cfg.get("open_side"),
        "hole_mode": cfg.get("hole_mode"),
        "hole_pad_m": cfg.get("hole_pad_m"),
        "hole_portal_length_m": cfg.get("hole_portal_length_m"),
        "blend_open_dgm": cfg.get("blend_open_dgm"),
        "style": cfg.get("style") or {},
        "materials": cfg.get("materials") or {},
        "nodes": len(nodes),
    }
    return nodes, info


def _s_range(s_from: float, s_to: float, step: float) -> list[float]:
    if abs(step) < 1e-9:
        return [s_from]
    out: list[float] = []
    if step > 0:
        s = s_from
        while s <= s_to + 1e-9:
            out.append(s)
            s += step
    else:
        s = s_from
        while s >= s_to - 1e-9:
            out.append(s)
            s += step
    if out and abs(out[-1] - s_to) > 0.01:
        out.append(s_to)
    return out



def _nodes_in_s_window(nodes: list[dict], s_lo: float, s_hi: float) -> list[dict]:
    out = [n for n in nodes if s_lo - 1e-6 <= float(n["s"]) <= s_hi + 1e-6]
    if len(out) >= 2:
        return out
    # Fallback: nearest nodes to window ends
    if not nodes:
        return []
    by_s = sorted(nodes, key=lambda n: float(n["s"]))
    a = min(by_s, key=lambda n: abs(float(n["s"]) - s_lo))
    b = min(by_s, key=lambda n: abs(float(n["s"]) - s_hi))
    if a is b:
        return [a]
    return [a, b] if float(a["s"]) <= float(b["s"]) else [b, a]

# --- Mesh loft -----------------------------------------------------------------

def _add_vert(verts: list, v: tuple[float, float, float]) -> int:
    verts.append(v)
    return len(verts) - 1


def _quad(faces: list, a: int, b: int, c: int, d: int) -> None:
    """Two triangles with outward/consistent winding (a-b-c, a-c-d)."""
    faces.append((a, b, c))
    faces.append((a, c, d))


def loft_gallery_shell(
    nodes: list[dict],
    *,
    open_side: str,
    wall_t: float,
    overhang_m: float,
    embed_m: float = 0.2,
    open_extra_m: float = 0.8,
    deck_width_extra_m: float = 0.5,
    parapet_height_m: float = 1.0,
    parapet_thickness_m: float = 0.35,
    parapet_out_m: float = 0.25,
    column_spacing_m: float = 4.0,
    column_along_m: float = 0.4,
    column_lat_m: float = 0.35,
    column_out_m: float = 0.15,
    build_parapet: bool = True,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]], tuple[float, float, float]]:
    """Build open U-shell + optional open-side parapet/columns.

    open_side: left|right|both|none — which full-height wall is omitted (view out).
    On the open side: low continuous parapet (~1 m) and rectangular posts up to roof.

    Columns sit **on the parapet** (center = parapet midline + ``column_out_m``).
    ``column_out_m`` > 0 shifts toward the outer edge; clamped to stay on the strip.
    ``parapet_out_m`` grows the outer face past the open deck edge (inner face fixed).
    """
    if len(nodes) < 2:
        raise ValueError("need >=2 gallery nodes")

    origin = (float(nodes[0]["x"]), float(nodes[0]["y"]), float(nodes[0]["z_road"]))
    ox, oy, oz = origin
    side = (open_side or "left").lower().strip()
    build_left = side not in ("left", "both")
    build_right = side not in ("right", "both")
    open_left = side in ("left", "both")
    open_right = side in ("right", "both")
    open_extra = max(0.0, float(open_extra_m))
    deck_w_extra = max(0.0, float(deck_width_extra_m))
    # Roof must cover parapet + outward columns
    overhang = max(float(overhang_m), open_extra if build_parapet else float(overhang_m))
    parapet_h = max(0.15, float(parapet_height_m))
    parapet_t = max(0.15, float(parapet_thickness_m))
    parapet_out = max(0.0, float(parapet_out_m))
    col_space = max(1.0, float(column_spacing_m))
    col_along = max(0.15, float(column_along_m))
    col_lat = max(0.15, float(column_lat_m))
    col_out = float(column_out_m)
    # Keep roof over parapet outer edge (and any column that still sticks out)
    overhang = max(
        overhang,
        open_extra + parapet_out,
        open_extra + parapet_out + max(0.0, col_out) + 0.5 * col_lat,
    )

    ring: list[dict] = []
    for n in nodes:
        tx, ty = float(n["tx"]), float(n["ty"])
        left, right = bb.left_right_unit(tx, ty)
        half = 0.5 * float(n["width"])
        cx = float(n["x"]) - ox
        cy = float(n["y"]) - oy
        zb = float(n["z_road"]) - oz - embed_m
        zr = float(n["z_road"]) - oz
        zi = float(n["z_roof_inner"]) - oz
        zo = float(n["z_roof_outer"]) - oz
        # Open-side outer edge (deck/parapet)
        lat_l = half + (open_extra if open_left else 0.0)
        lat_r = half + (open_extra if open_right else 0.0)
        # Roof edge uses overhang (at least open_extra)
        roof_l = half + (overhang if open_left or build_left else overhang)
        roof_r = half + (overhang if open_right or build_right else overhang)
        # Full walls still sit at road half (+ thickness)
        ring.append(
            {
                "left": left,
                "right": right,
                "tx": tx,
                "ty": ty,
                "half": half,
                "s": float(n["s"]),
                "zr": zr,
                "zb": zb,
                "zi": zi,
                "zo": zo,
                "lat_l": lat_l,
                "lat_r": lat_r,
                # closed-wall / roof anchors (road half for closed; open uses lat_*)
                "li_b": (cx + half * left[0], cy + half * left[1], zb),
                "ri_b": (cx + half * right[0], cy + half * right[1], zb),
                "li_i": (cx + half * left[0], cy + half * left[1], zi),
                "ri_i": (cx + half * right[0], cy + half * right[1], zi),
                # open-side deck edge at roof height for underside span
                "li_open_i": (cx + lat_l * left[0], cy + lat_l * left[1], zi),
                "ri_open_i": (cx + lat_r * right[0], cy + lat_r * right[1], zi),
                "lo_o": (
                    cx + roof_l * left[0],
                    cy + roof_l * left[1],
                    zo,
                ),
                "ro_o": (
                    cx + roof_r * right[0],
                    cy + roof_r * right[1],
                    zo,
                ),
                "lo_i": (
                    cx + roof_l * left[0],
                    cy + roof_l * left[1],
                    zi,
                ),
                "ro_i": (
                    cx + roof_r * right[0],
                    cy + roof_r * right[1],
                    zi,
                ),
                "lw_b": (
                    cx + (half + wall_t) * left[0],
                    cy + (half + wall_t) * left[1],
                    zb,
                ),
                "rw_b": (
                    cx + (half + wall_t) * right[0],
                    cy + (half + wall_t) * right[1],
                    zb,
                ),
                "lw_o": (
                    cx + (half + wall_t) * left[0],
                    cy + (half + wall_t) * left[1],
                    zo,
                ),
                "rw_o": (
                    cx + (half + wall_t) * right[0],
                    cy + (half + wall_t) * right[1],
                    zo,
                ),
                # parapet: inner face stays at (open edge − thickness);
                # outer grows by parapet_out_m (covers outward columns)
                "lp_in": (
                    cx + (lat_l - parapet_t) * left[0],
                    cy + (lat_l - parapet_t) * left[1],
                    zb,
                ),
                "lp_out": (
                    cx + (lat_l + parapet_out) * left[0],
                    cy + (lat_l + parapet_out) * left[1],
                    zb,
                ),
                "lp_in_t": (
                    cx + (lat_l - parapet_t) * left[0],
                    cy + (lat_l - parapet_t) * left[1],
                    zr + parapet_h,
                ),
                "lp_out_t": (
                    cx + (lat_l + parapet_out) * left[0],
                    cy + (lat_l + parapet_out) * left[1],
                    zr + parapet_h,
                ),
                "rp_in": (
                    cx + (lat_r - parapet_t) * right[0],
                    cy + (lat_r - parapet_t) * right[1],
                    zb,
                ),
                "rp_out": (
                    cx + (lat_r + parapet_out) * right[0],
                    cy + (lat_r + parapet_out) * right[1],
                    zb,
                ),
                "rp_in_t": (
                    cx + (lat_r - parapet_t) * right[0],
                    cy + (lat_r - parapet_t) * right[1],
                    zr + parapet_h,
                ),
                "rp_out_t": (
                    cx + (lat_r + parapet_out) * right[0],
                    cy + (lat_r + parapet_out) * right[1],
                    zr + parapet_h,
                ),
                "cx": cx,
                "cy": cy,
            }
        )

    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []

    for i in range(len(ring) - 1):
        # Roof underside: closed edge at half, open edge at lat_* so slab covers deck
        a = _add_vert(
            verts, ring[i]["li_open_i"] if open_left else ring[i]["li_i"]
        )
        b = _add_vert(
            verts, ring[i]["ri_open_i"] if open_right else ring[i]["ri_i"]
        )
        c = _add_vert(
            verts,
            ring[i + 1]["ri_open_i"] if open_right else ring[i + 1]["ri_i"],
        )
        d = _add_vert(
            verts,
            ring[i + 1]["li_open_i"] if open_left else ring[i + 1]["li_i"],
        )
        _quad(faces, a, d, c, b)

        # Roof topside
        a = _add_vert(verts, ring[i]["lo_o"])
        b = _add_vert(verts, ring[i]["ro_o"])
        c = _add_vert(verts, ring[i + 1]["ro_o"])
        d = _add_vert(verts, ring[i + 1]["lo_o"])
        _quad(faces, a, b, c, d)

        # Roof slab edges (overhang lips)
        a = _add_vert(verts, ring[i]["lo_i"])
        b = _add_vert(verts, ring[i]["lo_o"])
        c = _add_vert(verts, ring[i + 1]["lo_o"])
        d = _add_vert(verts, ring[i + 1]["lo_i"])
        _quad(faces, a, b, c, d)
        a = _add_vert(verts, ring[i]["ro_i"])
        b = _add_vert(verts, ring[i]["ri_i"] if not open_right else ring[i]["ri_open_i"])
        c = _add_vert(
            verts,
            ring[i + 1]["ri_i"] if not open_right else ring[i + 1]["ri_open_i"],
        )
        d = _add_vert(verts, ring[i + 1]["ro_i"])
        _quad(faces, a, b, c, d)
        a = _add_vert(verts, ring[i]["ro_o"])
        b = _add_vert(verts, ring[i]["ro_i"])
        c = _add_vert(verts, ring[i + 1]["ro_i"])
        d = _add_vert(verts, ring[i + 1]["ro_o"])
        _quad(faces, a, d, c, b)
        # left overhang lip underside→edge when open
        a = _add_vert(
            verts, ring[i]["li_i"] if not open_left else ring[i]["li_open_i"]
        )
        b = _add_vert(verts, ring[i]["lo_i"])
        c = _add_vert(verts, ring[i + 1]["lo_i"])
        d = _add_vert(
            verts,
            ring[i + 1]["li_i"] if not open_left else ring[i + 1]["li_open_i"],
        )
        _quad(faces, a, b, c, d)

        if build_right:
            a = _add_vert(verts, ring[i]["ri_b"])
            b = _add_vert(verts, ring[i]["ri_i"])
            c = _add_vert(verts, ring[i + 1]["ri_i"])
            d = _add_vert(verts, ring[i + 1]["ri_b"])
            _quad(faces, a, b, c, d)
            a = _add_vert(verts, ring[i]["rw_b"])
            b = _add_vert(verts, ring[i]["rw_o"])
            c = _add_vert(verts, ring[i + 1]["rw_o"])
            d = _add_vert(verts, ring[i + 1]["rw_b"])
            _quad(faces, a, d, c, b)
            a = _add_vert(verts, ring[i]["ri_i"])
            b = _add_vert(verts, ring[i]["rw_o"])
            c = _add_vert(verts, ring[i + 1]["rw_o"])
            d = _add_vert(verts, ring[i + 1]["ri_i"])
            _quad(faces, a, d, c, b)

        if build_left:
            a = _add_vert(verts, ring[i]["li_b"])
            b = _add_vert(verts, ring[i]["li_i"])
            c = _add_vert(verts, ring[i + 1]["li_i"])
            d = _add_vert(verts, ring[i + 1]["li_b"])
            _quad(faces, a, d, c, b)
            a = _add_vert(verts, ring[i]["lw_b"])
            b = _add_vert(verts, ring[i]["lw_o"])
            c = _add_vert(verts, ring[i + 1]["lw_o"])
            d = _add_vert(verts, ring[i + 1]["lw_b"])
            _quad(faces, a, b, c, d)
            a = _add_vert(verts, ring[i]["li_i"])
            b = _add_vert(verts, ring[i]["lw_o"])
            c = _add_vert(verts, ring[i + 1]["lw_o"])
            d = _add_vert(verts, ring[i + 1]["li_i"])
            _quad(faces, a, b, c, d)

        # --- Open-side parapet (continuous low wall) ---
        # Winding must match closed walls: left uses flipped quads so the
        # road-side face normals point into the carriageway (not into solid).
        if build_parapet and open_left:
            # inner face (toward road)
            a = _add_vert(verts, ring[i]["lp_in"])
            b = _add_vert(verts, ring[i]["lp_in_t"])
            c = _add_vert(verts, ring[i + 1]["lp_in_t"])
            d = _add_vert(verts, ring[i + 1]["lp_in"])
            _quad(faces, a, d, c, b)
            # outer face
            a = _add_vert(verts, ring[i]["lp_out"])
            b = _add_vert(verts, ring[i]["lp_out_t"])
            c = _add_vert(verts, ring[i + 1]["lp_out_t"])
            d = _add_vert(verts, ring[i + 1]["lp_out"])
            _quad(faces, a, b, c, d)
            # top (normal up)
            a = _add_vert(verts, ring[i]["lp_in_t"])
            b = _add_vert(verts, ring[i]["lp_out_t"])
            c = _add_vert(verts, ring[i + 1]["lp_out_t"])
            d = _add_vert(verts, ring[i + 1]["lp_in_t"])
            _quad(faces, a, d, c, b)
        if build_parapet and open_right:
            a = _add_vert(verts, ring[i]["rp_in"])
            b = _add_vert(verts, ring[i]["rp_in_t"])
            c = _add_vert(verts, ring[i + 1]["rp_in_t"])
            d = _add_vert(verts, ring[i + 1]["rp_in"])
            _quad(faces, a, b, c, d)
            a = _add_vert(verts, ring[i]["rp_out"])
            b = _add_vert(verts, ring[i]["rp_out_t"])
            c = _add_vert(verts, ring[i + 1]["rp_out_t"])
            d = _add_vert(verts, ring[i + 1]["rp_out"])
            _quad(faces, a, d, c, b)
            a = _add_vert(verts, ring[i]["rp_in_t"])
            b = _add_vert(verts, ring[i]["rp_out_t"])
            c = _add_vert(verts, ring[i + 1]["rp_out_t"])
            d = _add_vert(verts, ring[i + 1]["rp_in_t"])
            _quad(faces, a, b, c, d)

    for idx in (0, -1):
        r = ring[idx]
        a = _add_vert(verts, r["li_open_i"] if open_left else r["li_i"])
        b = _add_vert(verts, r["ri_open_i"] if open_right else r["ri_i"])
        c = _add_vert(verts, r["ro_o"])
        d = _add_vert(verts, r["lo_o"])
        if idx == 0:
            _quad(faces, a, b, c, d)
        else:
            _quad(faces, a, d, c, b)
        if build_right:
            a = _add_vert(verts, r["ri_b"])
            b = _add_vert(verts, r["ri_i"])
            c = _add_vert(verts, r["rw_o"])
            d = _add_vert(verts, r["rw_b"])
            if idx == 0:
                _quad(faces, a, b, c, d)
            else:
                _quad(faces, a, d, c, b)
        if build_left:
            a = _add_vert(verts, r["li_b"])
            b = _add_vert(verts, r["lw_b"])
            c = _add_vert(verts, r["lw_o"])
            d = _add_vert(verts, r["li_i"])
            if idx == 0:
                _quad(faces, a, b, c, d)
            else:
                _quad(faces, a, d, c, b)
        if build_parapet and open_left:
            a = _add_vert(verts, r["lp_in"])
            b = _add_vert(verts, r["lp_out"])
            c = _add_vert(verts, r["lp_out_t"])
            d = _add_vert(verts, r["lp_in_t"])
            if idx == 0:
                _quad(faces, a, b, c, d)
            else:
                _quad(faces, a, d, c, b)
        if build_parapet and open_right:
            a = _add_vert(verts, r["rp_in"])
            b = _add_vert(verts, r["rp_out"])
            c = _add_vert(verts, r["rp_out_t"])
            d = _add_vert(verts, r["rp_in_t"])
            if idx == 0:
                _quad(faces, a, d, c, b)
            else:
                _quad(faces, a, b, c, d)

    # --- Rectangular columns on parapet up to roof ---
    if build_parapet and (open_left or open_right) and col_space > 1e-6:
        # cumulative arc length along nodes
        s_acc = [0.0]
        for i in range(1, len(nodes)):
            dx = float(nodes[i]["x"]) - float(nodes[i - 1]["x"])
            dy = float(nodes[i]["y"]) - float(nodes[i - 1]["y"])
            s_acc.append(s_acc[-1] + math.hypot(dx, dy))
        total = s_acc[-1]
        # inset from ends so posts clear portal frames a bit
        inset = max(col_along, 1.0)
        if total > 2.0 * inset + col_space:
            n_cols = max(1, int(round((total - 2.0 * inset) / col_space)) + 1)
            for k in range(n_cols):
                if n_cols == 1:
                    s_t = 0.5 * total
                else:
                    s_t = inset + k * (total - 2.0 * inset) / (n_cols - 1)
                # find segment
                j = 0
                while j + 1 < len(s_acc) and s_acc[j + 1] < s_t:
                    j += 1
                j = min(j, len(nodes) - 2)
                seg = max(s_acc[j + 1] - s_acc[j], 1e-6)
                t = max(0.0, min(1.0, (s_t - s_acc[j]) / seg))
                n0, n1 = nodes[j], nodes[j + 1]
                x = float(n0["x"]) + t * (float(n1["x"]) - float(n0["x"]))
                y = float(n0["y"]) + t * (float(n1["y"]) - float(n0["y"]))
                z_road = float(n0["z_road"]) + t * (
                    float(n1["z_road"]) - float(n0["z_road"])
                )
                z_roof = float(n0["z_roof_inner"]) + t * (
                    float(n1["z_roof_inner"]) - float(n0["z_roof_inner"])
                )
                tx = float(n0["tx"]) + t * (float(n1["tx"]) - float(n0["tx"]))
                ty = float(n0["ty"]) + t * (float(n1["ty"]) - float(n0["ty"]))
                tlen = math.hypot(tx, ty) or 1.0
                tx, ty = tx / tlen, ty / tlen
                left_u, right_u = bb.left_right_unit(tx, ty)
                half = 0.5 * (
                    float(n0["width"]) + t * (float(n1["width"]) - float(n0["width"]))
                )
                z0 = z_road + parapet_h
                z1 = z_roof
                if z1 <= z0 + 0.05:
                    continue
                for is_left, do in ((True, open_left), (False, open_right)):
                    if not do:
                        continue
                    # Sit on parapet: midline + column_out_m, clamped onto the strip
                    p_in = half + open_extra - parapet_t
                    p_out = half + open_extra + parapet_out
                    mid = 0.5 * (p_in + p_out)
                    lat = mid + col_out
                    half_c = 0.5 * col_lat
                    # Keep column footprint on parapet when it fits
                    if p_out - p_in >= col_lat - 1e-6:
                        lat = max(p_in + half_c, min(p_out - half_c, lat))
                    u = left_u if is_left else right_u
                    _add_box_oriented(
                        verts,
                        faces,
                        origin=origin,
                        center=(
                            x + lat * u[0],
                            y + lat * u[1],
                            0.5 * (z0 + z1),
                        ),
                        fwd=(tx, ty),
                        left=left_u,
                        half_along=0.5 * col_along,
                        half_lat=0.5 * col_lat,
                        z0=z0,
                        z1=z1,
                    )

    return verts, faces, origin


def _basis_road(tx: float, ty: float, into_sign: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return (forward_into_gallery_xy, left_xy) unit vectors."""
    tlen = math.hypot(tx, ty) or 1.0
    tx, ty = tx / tlen, ty / tlen
    fwd = (tx * into_sign, ty * into_sign)
    left, _right = bb.left_right_unit(tx, ty)
    return fwd, left


def _add_box_oriented(
    verts: list,
    faces: list,
    *,
    origin: tuple[float, float, float],
    center: tuple[float, float, float],
    fwd: tuple[float, float],
    left: tuple[float, float],
    half_along: float,
    half_lat: float,
    z0: float,
    z1: float,
) -> None:
    """Axis-aligned in road frame: along=fwd, lat=left, Z up. z0/z1 absolute world Z."""
    ox, oy, oz = origin
    cx, cy, cz = center
    # 8 corners: along ±, lat ±, z0/z1
    corners_w = []
    for sa in (-1.0, 1.0):
        for sl in (-1.0, 1.0):
            for z in (z0, z1):
                x = cx + sa * half_along * fwd[0] + sl * half_lat * left[0]
                y = cy + sa * half_along * fwd[1] + sl * half_lat * left[1]
                corners_w.append((x - ox, y - oy, z - oz))
    # indices: a- sa=-1 sl=-1 z0, b- sa=-1 sl=-1 z1, c- sa=-1 sl=+1 z0, ...
    # Order: (sa,sl,z): 0=(-,-,0) 1=(-,-,1) 2=(-,+,0) 3=(-,+,1) 4=(+,-,0) 5=(+,-,1) 6=(+,+,0) 7=(+,+,1)
    idx = [_add_vert(verts, c) for c in corners_w]
    # Outward normals (from box center). Order matches sa/sl/z corners above.
    _quad(faces, idx[0], idx[1], idx[3], idx[2])  # -along
    _quad(faces, idx[4], idx[6], idx[7], idx[5])  # +along
    _quad(faces, idx[0], idx[4], idx[5], idx[1])  # -lat
    _quad(faces, idx[2], idx[3], idx[7], idx[6])  # +lat
    _quad(faces, idx[0], idx[2], idx[6], idx[4])  # bottom
    _quad(faces, idx[1], idx[5], idx[7], idx[3])  # top


def loft_portal_frame(
    node: dict,
    *,
    into_sign: float,
    clear_h: float,
    roof_t: float,
    block_m: float,
    depth_m: float,
    overhang_m: float,
    open_side: str = "left",
    clear_extra_m: float = 0.2,
    out_lip_m: float = 0.25,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]], tuple[float, float, float]]:
    """Chunky rectangular portal: pier(s) + lintel (no road threshold).

    Skips the pier on ``open_side`` so an open gallery does not put a block
    into the lookout / across a curved carriageway. Pier inner face sits at
    half-width + ``clear_extra_m`` so it does not cut the deck MeshRoad.
    """
    ox = float(node["x"])
    oy = float(node["y"])
    oz = float(node.get("z_hermite") if node.get("z_hermite") is not None else node["z_road"])
    origin = (ox, oy, oz)
    tx = float(node.get("tx") or 1.0)
    ty = float(node.get("ty") or 0.0)
    fwd, left = _basis_road(tx, ty, into_sign)
    half = 0.5 * float(node.get("width") or 7.0)
    block = max(0.4, float(block_m))
    depth = max(0.6, float(depth_m))
    over = max(0.0, float(overhang_m))
    clear_x = max(0.0, float(clear_extra_m))
    out_lip = max(0.0, min(float(out_lip_m), depth * 0.35))
    along_center = 0.5 * depth - out_lip
    half_along = 0.5 * depth
    z_road = oz
    z_inner = z_road + clear_h
    z_lintel_top = z_inner + max(roof_t, block * 0.6)
    side = (open_side or "left").lower().strip()
    build_left_pier = side not in ("left", "both")
    build_right_pier = side not in ("right", "both")
    # Opening edge inset so pier clears the asphalt
    pier_inner = half + clear_x

    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []

    def _pier(lat_sign: float) -> None:
        """lat_sign +1 = left side of road, -1 = right."""
        inner = pier_inner
        outer = pier_inner + max(over, block)
        lat_c = lat_sign * 0.5 * (outer + inner)
        half_lat = 0.5 * (outer - inner)
        _add_box_oriented(
            verts,
            faces,
            origin=origin,
            center=(
                ox + along_center * fwd[0] + lat_c * left[0],
                oy + along_center * fwd[1] + lat_c * left[1],
                z_road,
            ),
            fwd=fwd,
            left=left,
            half_along=half_along,
            half_lat=half_lat,
            z0=z_road - 0.15,
            z1=z_lintel_top,
        )

    if build_left_pier:
        _pier(+1.0)
    if build_right_pier:
        _pier(-1.0)

    # Lintel across full outer width (beam over the opening)
    lintel_half_lat = pier_inner + max(over, block)
    _add_box_oriented(
        verts,
        faces,
        origin=origin,
        center=(
            ox + along_center * fwd[0],
            oy + along_center * fwd[1],
            z_road,
        ),
        fwd=fwd,
        left=left,
        half_along=half_along,
        half_lat=lintel_half_lat,
        z0=z_inner,
        z1=z_lintel_top,
    )
    # No threshold / sill across the carriageway — that cut through the deck.

    return verts, faces, origin


def loft_portal_collar(
    node: dict,
    *,
    into_sign: float,
    z_at,
    clear_h: float,
    roof_t: float,
    block_m: float,
    depth_m: float,
    overhang_m: float,
    open_side: str = "left",
    clear_extra_m: float = 0.2,
    out_lip_m: float = 0.25,
    collar_out_m: float = 5.0,
    collar_side_m: float = 7.0,
    collar_sink_m: float = 0.1,
    n_rings: int = 4,
    n_lat: int = 8,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]], tuple[float, float, float]]:
    """Rock fill for sky-gaps ABOVE the portal roof (never into the clear opening).

    Typical alpine case: holemap punched terrain that sat above the gallery, so
    the DGM at the roof footprint is still higher than ``z_roof``. We build an
    upstand from the lintel/roof top up to that DGM, plus a short apron into the
    mountain side. Vertices are clamped to ``>= z_roof`` so nothing drops into
    the carriageway.
    """
    ox = float(node["x"])
    oy = float(node["y"])
    oz = float(node.get("z_hermite") if node.get("z_hermite") is not None else node["z_road"])
    origin = (ox, oy, oz)
    tx = float(node.get("tx") or 1.0)
    ty = float(node.get("ty") or 0.0)
    fwd, left = _basis_road(tx, ty, into_sign)
    out_xy = (-fwd[0], -fwd[1])
    half = 0.5 * float(node.get("width") or 7.0)
    block = max(0.4, float(block_m))
    depth = max(0.6, float(depth_m))
    over = max(0.0, float(overhang_m))
    clear_x = max(0.0, float(clear_extra_m))
    out_lip = max(0.0, min(float(out_lip_m), depth * 0.35))
    along_center = 0.5 * depth - out_lip
    half_along = 0.5 * depth
    pier_outer = half + clear_x + max(over, block)
    z_road = oz
    z_roof = z_road + float(clear_h) + max(float(roof_t), block * 0.6)
    side = (open_side or "left").lower().strip()
    if side in ("left", "both"):
        mtn = -1.0
    elif side == "right":
        mtn = +1.0
    else:
        mtn = 0.0
    rings = max(2, int(n_rings))
    n_l = max(4, int(n_lat))
    side_m = max(1.0, float(collar_side_m))
    out_m = max(1.0, float(collar_out_m))
    sink = max(0.0, float(collar_sink_m))
    z_floor = z_roof + 0.02

    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []

    def _w(x: float, y: float, z: float) -> tuple[float, float, float]:
        return (x - ox, y - oy, z - oz)

    def _dgm(x: float, y: float) -> float:
        try:
            return float(z_at(x, y)) - sink
        except Exception:  # noqa: BLE001
            return z_roof

    def _facing_quad(
        a: tuple[float, float, float],
        b: tuple[float, float, float],
        c: tuple[float, float, float],
        d: tuple[float, float, float],
        prefer: tuple[float, float, float],
    ) -> None:
        ax, ay, az = a
        bx, by, bz = b
        cx, cy, cz = c
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        if nx * prefer[0] + ny * prefer[1] + nz * prefer[2] < 0:
            a, b, c, d = a, d, c, b
        _quad(
            faces,
            _add_vert(verts, a),
            _add_vert(verts, b),
            _add_vert(verts, c),
            _add_vert(verts, d),
        )

    def _grid(
        xy_at,
        *,
        prefer: tuple[float, float, float],
        min_rise: float = 0.2,
    ) -> int:
        """Build rings: ring0 = roof, outer rings = max(DGM, roof). Skip flat pads."""
        rows: list[list[tuple[float, float, float]]] = []
        max_rise = 0.0
        for ri in range(rings):
            t = ri / (rings - 1)
            col: list[tuple[float, float, float]] = []
            for li in range(n_l):
                s = li / (n_l - 1)
                x, y = xy_at(t, s)
                if ri == 0:
                    z = z_floor
                else:
                    z = max(_dgm(x, y), z_floor)
                    max_rise = max(max_rise, z - z_floor)
                col.append(_w(x, y, z))
            rows.append(col)
        if max_rise < min_rise:
            return 0
        n0 = len(faces)
        for ri in range(rings - 1):
            for li in range(n_l - 1):
                _facing_quad(
                    rows[ri][li],
                    rows[ri][li + 1],
                    rows[ri + 1][li + 1],
                    rows[ri + 1][li],
                    prefer,
                )
        return len(faces) - n0

    along_face = along_center - half_along

    # --- A) Upstand over the opening (tympanum): roof → DGM above lintel ---
    # Sample slightly out + toward mountain so we hit the punched hillside mass,
    # not the approach road grade.
    def _xy_tympanum(t: float, s: float) -> tuple[float, float]:
        lat = (-pier_outer) * (1.0 - s) + pier_outer * s
        # stay over/near structure: mostly up via DGM at near-roof XY, nudge out+mtn
        along = along_face - (out_m * 0.4) * t
        lat += (mtn * side_m * 0.5 if mtn else 0.0) * t
        x = ox + along * fwd[0] + lat * left[0]
        y = oy + along * fwd[1] + lat * left[1]
        return x, y

    _grid(
        _xy_tympanum,
        prefer=(out_xy[0] * 0.3, out_xy[1] * 0.3, 0.9),
        min_rise=0.15,
    )

    # --- B) Mountain-side roof → hillside (modest lateral reach) ---
    def _xy_mountain(t: float, s: float, msign: float) -> tuple[float, float]:
        if msign < 0:
            lat0, lat1 = -pier_outer, -half * 0.1
        else:
            lat0, lat1 = half * 0.1, pier_outer
        lat = lat0 * (1.0 - s) + lat1 * s + msign * side_m * t
        along = along_center + (out_m * 0.35) * t  # into gallery along roof
        x = ox + along * fwd[0] + lat * left[0]
        y = oy + along * fwd[1] + lat * left[1]
        return x, y

    if mtn != 0.0:
        _grid(
            lambda t, s: _xy_mountain(t, s, mtn),
            prefer=(-mtn * left[0] * 0.4, -mtn * left[1] * 0.4, 0.85),
            min_rise=0.15,
        )
    else:
        for msign in (+1.0, -1.0):
            _grid(
                lambda t, s, m=msign: _xy_mountain(t, s, m),
                prefer=(-msign * left[0] * 0.4, -msign * left[1] * 0.4, 0.85),
                min_rise=0.15,
            )

    # --- C) Upstand: roof edge → highest nearby DGM (closes punched sky-hole) ---
    prefer_up = (out_xy[0] * 0.15, out_xy[1] * 0.15, 1.0)
    n_u = max(6, n_l)
    search_dirs: list[tuple[float, float]] = []
    for k in range(12):
        ang = (2.0 * math.pi * k) / 12.0
        search_dirs.append((math.cos(ang), math.sin(ang)))
    # bias mountain + uphill-out
    if mtn != 0.0:
        search_dirs.insert(0, (mtn * left[0], mtn * left[1]))
        search_dirs.insert(
            1,
            (mtn * left[0] * 0.7 + out_xy[0] * 0.3, mtn * left[1] * 0.7 + out_xy[1] * 0.3),
        )
    search_dirs.insert(0, out_xy)

    def _highest_near(x0: float, y0: float) -> tuple[float, float, float]:
        best_x, best_y, best_z = x0, y0, _dgm(x0, y0)
        for radius in (0.5, 1.0, 1.5, 2.5, 3.5, 5.0, 6.5, 8.0):
            for dx, dy in search_dirs:
                x = x0 + dx * radius
                y = y0 + dy * radius
                z = _dgm(x, y)
                if z > best_z:
                    best_x, best_y, best_z = x, y, z
        return best_x, best_y, best_z

    bottom: list[tuple[float, float, float]] = []
    top: list[tuple[float, float, float]] = []
    for i in range(n_u):
        s = i / (n_u - 1)
        lat = (-pier_outer) * (1.0 - s) + pier_outer * s
        along = along_face + 0.4 * depth  # on roof slab, not over approach
        x0 = ox + along * fwd[0] + lat * left[0]
        y0 = oy + along * fwd[1] + lat * left[1]
        hx, hy, hz = _highest_near(x0, y0)
        bottom.append(_w(x0, y0, z_floor))
        top.append(_w(hx, hy, max(hz, z_floor)))
    rise = max(top[i][2] - bottom[i][2] for i in range(n_u))
    if rise >= 0.15:
        for i in range(n_u - 1):
            _facing_quad(bottom[i], bottom[i + 1], top[i + 1], top[i], prefer_up)

    # Extra mountain wing if we still have little geometry
    if len(faces) < 4 and mtn != 0.0:
        for i in range(n_u):
            s = i / (n_u - 1)
            lat = (-half if mtn < 0 else half) * 0.2 + mtn * pier_outer * s
            along = along_center
            x0 = ox + along * fwd[0] + lat * left[0]
            y0 = oy + along * fwd[1] + lat * left[1]
            hx, hy, hz = _highest_near(x0, y0)
            if hz >= z_floor + 0.15:
                b = _w(x0, y0, z_floor)
                tpt = _w(hx, hy, hz)
                bottom[i] = b
                top[i] = tpt
        rise = max(top[i][2] - bottom[i][2] for i in range(n_u))
        if rise >= 0.15:
            for i in range(n_u - 1):
                _facing_quad(bottom[i], bottom[i + 1], top[i + 1], top[i], prefer_up)

    return verts, faces, origin



def make_gallery_deck_meshroad(
    name: str,
    nodes: list[dict],
    *,
    depth_m: float,
    width_extra_m: float,
    materials: dict,
    z_terrain=None,
    portal_s: tuple[float, float] | list[float] | None = None,
    crossfall_max: float = 0.12,
    open_side: str | None = None,
    open_extra_m: float = 0.0,
) -> dict:
    """Asphalt MeshRoad with bridge-style pitch + Querneigung.

    Crossfall is sampled from the heightmap at the Hermite portals (solid road),
    then lerped along the gallery — mid-gallery DGM is roof/polluted and must
    not drive banking. Approaches outside portals sample local heightmap CF.

    ``open_extra_m`` extends the deck only toward ``open_side`` (shift centerline
    + widen) so the closed edge stays put.
    """
    if not nodes:
        raise ValueError("deck needs nodes")
    cf_max = float(crossfall_max)
    open_extra = max(0.0, float(open_extra_m))
    side = (open_side or "").lower().strip()
    s_p0 = s_p1 = None
    cf0 = cf1 = 0.0
    if portal_s is not None and len(portal_s) >= 2:
        s_p0, s_p1 = float(portal_s[0]), float(portal_s[1])
        if s_p1 < s_p0:
            s_p0, s_p1 = s_p1, s_p0
    if z_terrain is not None and s_p0 is not None:
        n0 = _node_nearest_s(nodes, s_p0)
        n1 = _node_nearest_s(nodes, s_p1)
        w0 = float(n0.get("width") or 7.0)
        w1 = float(n1.get("width") or 7.0)
        cf0, _zl0, _zc0, _zr0 = bb.sample_crossfall(
            z_terrain,
            float(n0["x"]),
            float(n0["y"]),
            float(n0.get("tx") or 1.0),
            float(n0.get("ty") or 0.0),
            0.5 * w0,
        )
        cf1, _zl1, _zc1, _zr1 = bb.sample_crossfall(
            z_terrain,
            float(n1["x"]),
            float(n1["y"]),
            float(n1.get("tx") or 1.0),
            float(n1.get("ty") or 0.0),
            0.5 * w1,
        )
        cf0 = max(-cf_max, min(cf_max, cf0))
        cf1 = max(-cf_max, min(cf_max, cf1))

    length_p = max((s_p1 - s_p0) if s_p0 is not None else 1.0, 1e-3)
    mesh_nodes = []
    for i, n in enumerate(nodes):
        z = float(n["z_road"])
        w = float(n.get("width") or 7.0) + float(width_extra_m) + open_extra
        tx = float(n.get("tx") or 1.0)
        ty = float(n.get("ty") or 0.0)
        x = float(n["x"])
        y = float(n["y"])
        if open_extra > 1e-9 and side in ("left", "right"):
            left_u, right_u = bb.left_right_unit(tx, ty)
            u = left_u if side == "left" else right_u
            # shift so only the open edge grows
            x += 0.5 * open_extra * u[0]
            y += 0.5 * open_extra * u[1]
        s = float(n["s"])
        # Longitudinal pitch from neighbors (m per m along XY)
        if i + 1 < len(nodes):
            dx = float(nodes[i + 1]["x"]) - float(n["x"])
            dy = float(nodes[i + 1]["y"]) - float(n["y"])
            dz = float(nodes[i + 1]["z_road"]) - z
        elif i > 0:
            dx = float(n["x"]) - float(nodes[i - 1]["x"])
            dy = float(n["y"]) - float(nodes[i - 1]["y"])
            dz = z - float(nodes[i - 1]["z_road"])
        else:
            dx, dy, dz = tx, ty, 0.0
        horiz = math.hypot(dx, dy) or 1.0
        dz_ds = dz / horiz

        # Crossfall: lerp portals inside; sample DGM on approaches
        if s_p0 is not None and s_p0 - 1e-6 <= s <= s_p1 + 1e-6:
            t = (s - s_p0) / length_p
            cf = cf0 + t * (cf1 - cf0)
        elif z_terrain is not None:
            cf, _zl, _zc, _zr = bb.sample_crossfall(
                z_terrain, float(n["x"]), float(n["y"]), tx, ty, 0.5 * w
            )
            cf = max(-cf_max, min(cf_max, cf))
        else:
            cf = 0.0

        nrm = bb.normal_from_pitch_crossfall(tx, ty, dz_ds, cf)
        mesh_nodes.append(
            [
                round(x, 3),
                round(y, 3),
                round(z, 3),
                round(w, 2),
                round(depth_m, 2),
                round(nrm[0], 4),
                round(nrm[1], 4),
                round(nrm[2], 4),
            ]
        )
    tex_len = float(materials.get("texture_length") or materials.get("textureLength") or 4.0)
    return {
        "name": name,
        "class": "MeshRoad",
        "__parent": "galleries",
        "topMaterial": materials.get("top") or "Asphalt",
        "bottomMaterial": materials.get("bottom") or "Concrete",
        "sideMaterial": materials.get("side") or "Concrete",
        "textureLength": tex_len,
        "breakAngle": 3,
        "widthSubdivisions": 0,
        "nodes": mesh_nodes,
    }


def write_collada(
    path: Path,
    verts: list[tuple[float, float, float]],
    faces: list[tuple[int, int, int]],
    *,
    material_name: str = "Concrete",
    mesh_stem: str | None = None,
    uv_scale_m: float = 4.0,
) -> None:
    """Z-up Collada with BeamNG static hierarchy + Colmesh-1 collision.

    Hierarchy (required for ``collisionType: Collision Mesh``):
      base00
        start01
          <stem>_a999          — visible LOD
        collision-1
          Colmesh-1           — physics mesh (same tris for now; simplify later)
    """
    pos_vals = " ".join(f"{x:.4f} {y:.4f} {z:.4f}" for x, y, z in verts)
    # Planar UV from local XY so rock/concrete maps tile like terrain (~uv_scale_m).
    scale = max(0.5, float(uv_scale_m))
    uv_vals = " ".join(f"{x / scale:.5f} {y / scale:.5f}" for x, y, _z in verts)
    p_vals = " ".join(f"{a} {b} {c}" for a, b, c in faces)
    n_tri = len(faces)
    n_vert = len(verts)
    mat = escape(material_name)
    stem = mesh_stem or path.stem
    # LOD name must have a letter before the pixel threshold
    lod_name = f"{stem}_a999"
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor><authoring_tool>beamng_autoroad build_galleries</authoring_tool></contributor>
    <unit name="meter" meter="1"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_effects>
    <effect id="{mat}-effect">
      <profile_COMMON>
        <technique sid="common">
          <lambert>
            <diffuse><color>0.55 0.55 0.52 1</color></diffuse>
          </lambert>
        </technique>
      </profile_COMMON>
    </effect>
  </library_effects>
  <library_materials>
    <material id="{mat}-material" name="{mat}">
      <instance_effect url="#{mat}-effect"/>
    </material>
  </library_materials>
  <library_geometries>
    <geometry id="{lod_name}-mesh" name="{lod_name}-mesh">
      <mesh>
        <source id="{lod_name}-mesh-positions">
          <float_array id="{lod_name}-mesh-positions-array" count="{n_vert * 3}">{pos_vals}</float_array>
          <technique_common>
            <accessor source="#{lod_name}-mesh-positions-array" count="{n_vert}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{lod_name}-mesh-map-0">
          <float_array id="{lod_name}-mesh-map-0-array" count="{n_vert * 2}">{uv_vals}</float_array>
          <technique_common>
            <accessor source="#{lod_name}-mesh-map-0-array" count="{n_vert}" stride="2">
              <param name="S" type="float"/>
              <param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{lod_name}-mesh-vertices">
          <input semantic="POSITION" source="#{lod_name}-mesh-positions"/>
        </vertices>
        <triangles material="{mat}" count="{n_tri}">
          <input semantic="VERTEX" source="#{lod_name}-mesh-vertices" offset="0"/>
          <input semantic="TEXCOORD" source="#{lod_name}-mesh-map-0" offset="0" set="0"/>
          <p>{p_vals}</p>
        </triangles>
      </mesh>
    </geometry>
    <geometry id="Colmesh-1-mesh" name="Colmesh-1-mesh">
      <mesh>
        <source id="Colmesh-1-mesh-positions">
          <float_array id="Colmesh-1-mesh-positions-array" count="{n_vert * 3}">{pos_vals}</float_array>
          <technique_common>
            <accessor source="#Colmesh-1-mesh-positions-array" count="{n_vert}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="Colmesh-1-mesh-vertices">
          <input semantic="POSITION" source="#Colmesh-1-mesh-positions"/>
        </vertices>
        <triangles count="{n_tri}">
          <input semantic="VERTEX" source="#Colmesh-1-mesh-vertices" offset="0"/>
          <p>{p_vals}</p>
        </triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="base00" name="base00" type="NODE">
        <node id="start01" name="start01" type="NODE">
          <node id="{lod_name}" name="{lod_name}" type="NODE">
            <instance_geometry url="#{lod_name}-mesh">
              <bind_material>
                <technique_common>
                  <instance_material symbol="{mat}" target="#{mat}-material">
                    <bind_vertex_input semantic="UVSET0" input_semantic="TEXCOORD" input_set="0"/>
                  </instance_material>
                </technique_common>
              </bind_material>
            </instance_geometry>
          </node>
        </node>
        <node id="collision-1" name="collision-1" type="NODE">
          <node id="Colmesh-1" name="Colmesh-1" type="NODE">
            <instance_geometry url="#Colmesh-1-mesh"/>
          </node>
        </node>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene>
    <instance_visual_scene url="#Scene"/>
  </scene>
</COLLADA>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-8", newline="\n")


def ensure_gallery_material(user_level: Path, level_name: str, mat_name: str = "Concrete") -> None:
    """Ensure a regular Material exists for gallery DAE mapTo."""
    mats_path = user_level / "art" / "shapes" / "galleries" / "main.materials.json"
    mats_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if mats_path.is_file() and mats_path.stat().st_size:
        try:
            data = json.loads(mats_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    terr = f"/levels/{level_name}/art/terrains"
    if mat_name in ("GalleryRock", "rock", "Rock"):
        # Match terrain layer "rock": pac detail maps (what you see on steep faces)
        # plus a soft base tint; UVs on collar DAEs tile at ~4 m.
        data[mat_name] = {
            "name": mat_name,
            "mapTo": mat_name,
            "class": "Material",
            "persistentId": "ga11e7c0-c0ac-4e1e-9c11-0000ga11e702",
            "Stages": [
                {
                    "baseColorMap": f"{terr}/t_rocks_pac_b.png",
                    "normalMap": f"{terr}/t_rocks_pac_nm.png",
                    "roughnessMap": f"{terr}/t_rocks_pac_r.png",
                    "ambientOcclusionMap": f"{terr}/t_rocks_pac_ao.png",
                    "detailMap": f"{terr}/t_terrain_base_rock_b.png",
                    "detailScale": [0.35, 0.35],
                    "detailNormalMap": f"{terr}/t_terrain_base_rock_nm.png",
                    "roughnessFactor": 0.96,
                    "baseColorFactor": [0.72, 0.70, 0.66, 1.0],
                },
                {},
                {},
                {},
            ],
            "annotation": "ROCK",
            "materialTag0": "Natural",
            "materialTag1": "beamng",
            "version": 1.5,
        }
    else:
        data[mat_name] = {
            "name": mat_name,
            "mapTo": mat_name,
            "class": "Material",
            "persistentId": "ga11e7c0-c0ac-4e1e-9c11-0000ga11e701",
            "Stages": [
                {
                    "baseColorMap": f"{terr}/t_terrain_base_concrete_b.png",
                    "normalMap": f"{terr}/t_terrain_base_concrete_nm.png",
                    "roughnessFactor": 0.85,
                    "baseColorFactor": [0.7, 0.7, 0.68, 1.0],
                },
                {},
                {},
                {},
            ],
            "annotation": "CONCRETE",
            "materialTag0": "RoadAndPath",
            "materialTag1": "beamng",
            "version": 1.5,
        }
    mats_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# --- Hole map -----------------------------------------------------------------

def _to_px_beamng(bx: float, by: float, size: int, extent: float) -> tuple[float, float]:
    px = bx / extent * (size - 1)
    py = (1.0 - by / extent) * (size - 1)
    return px, py


def _from_px_beamng(px: float, py: float, size: int, extent: float) -> tuple[float, float]:
    bx = px / max(size - 1, 1) * extent
    by = (1.0 - py / max(size - 1, 1)) * extent
    return bx, by


def _project_point_to_nodes(
    bx: float,
    by: float,
    nodes: list[dict],
    *,
    z_key: str = "hermite",
) -> tuple[float, float, float] | None:
    """Nearest point on polyline → (dist_m, z, half_width).

    z_key:
      hermite — ``z_hermite`` when present (portal carve / hole compare)
      road — ``z_road`` (MeshRoad surface; approach conform)
    """
    if len(nodes) < 1:
        return None

    def _z_of(n: dict) -> float:
        if z_key == "road":
            return float(n["z_road"])
        if n.get("z_hermite") is not None:
            return float(n["z_hermite"])
        return float(n["z_road"])

    best: tuple[float, float, float] | None = None
    for i, n in enumerate(nodes):
        x0, y0 = float(n["x"]), float(n["y"])
        z0 = _z_of(n)
        w0 = 0.5 * float(n.get("width") or 7.0)
        if i == len(nodes) - 1:
            d = math.hypot(bx - x0, by - y0)
            cand = (d, z0, w0)
        else:
            n1 = nodes[i + 1]
            x1, y1 = float(n1["x"]), float(n1["y"])
            z1 = _z_of(n1)
            w1 = 0.5 * float(n1.get("width") or 7.0)
            dx, dy = x1 - x0, y1 - y0
            seg2 = dx * dx + dy * dy
            if seg2 < 1e-12:
                t = 0.0
            else:
                t = max(0.0, min(1.0, ((bx - x0) * dx + (by - y0) * dy) / seg2))
            px = x0 + t * dx
            py = y0 + t * dy
            d = math.hypot(bx - px, by - py)
            cand = (d, z0 + t * (z1 - z0), w0 + t * (w1 - w0))
        if best is None or cand[0] < best[0]:
            best = cand
    return best


def conform_approach_heightmap(
    galleries: list[dict],
    span_infos: list[dict],
    *,
    size: int,
    extent: float,
    defaults: dict,
    elev: np.ndarray,
) -> tuple[np.ndarray, dict]:
    """Bake heightmap to MeshRoad ``z_road`` on portal approaches only.

    BeamNG terrain is a regular heightfield — there are no free mesh corners to
    snap. Instead we set samples under the approach ribbon to the deck surface
    (minus a tiny sink) and soft-blend laterally into the untouched DGM.
    Mid-gallery mountain is never touched.
    """
    out = elev.astype(np.float64).copy()
    info_by_oid = {info.get("objectid"): info for info in span_infos}
    tested = 0
    changed = 0
    max_abs = 0.0

    for g in galleries:
        cfg_conform = g.get("approach_conform")
        if cfg_conform is None:
            cfg_conform = defaults.get("approach_conform", False)
        if not cfg_conform:
            continue
        nodes = g.get("nodes") or []
        if len(nodes) < 2:
            continue
        info = info_by_oid.get(g.get("objectid")) or {}
        portal_s = info.get("portal_s") or []
        if len(portal_s) < 2:
            continue
        s_p0, s_p1 = float(portal_s[0]), float(portal_s[1])
        if s_p1 < s_p0:
            s_p0, s_p1 = s_p1, s_p0
        blend = float(
            g["approach_blend_m"]
            if g.get("approach_blend_m") is not None
            else defaults.get("approach_blend_m") or 12.0
        )
        pad = float(
            g["approach_conform_pad_m"]
            if g.get("approach_conform_pad_m") is not None
            else defaults.get("approach_conform_pad_m") or 0.5
        )
        falloff = float(
            g["approach_conform_falloff_m"]
            if g.get("approach_conform_falloff_m") is not None
            else defaults.get("approach_conform_falloff_m") or 1.5
        )
        sink = float(
            g["approach_conform_sink_m"]
            if g.get("approach_conform_sink_m") is not None
            else defaults.get("approach_conform_sink_m") or 0.02
        )
        max_delta = float(
            g["approach_conform_max_delta_m"]
            if g.get("approach_conform_max_delta_m") is not None
            else defaults.get("approach_conform_max_delta_m") or 0.35
        )
        width_extra = float(
            g["deck_width_extra_m"]
            if g.get("deck_width_extra_m") is not None
            else defaults.get("deck_width_extra_m") or 0.0
        )
        # Approach ribbons only (outside Hermite portals)
        bands: list[list[dict]] = []
        lo = [
            n
            for n in nodes
            if float(n["s"]) <= s_p0 + 1e-6 and float(n["s"]) >= s_p0 - blend - 1e-6
        ]
        hi = [
            n
            for n in nodes
            if float(n["s"]) >= s_p1 - 1e-6 and float(n["s"]) <= s_p1 + blend + 1e-6
        ]
        if len(lo) >= 2:
            bands.append(lo)
        if len(hi) >= 2:
            bands.append(hi)

        for band in bands:
            half_ref = 0.5 * max(float(n.get("width") or 7.0) for n in band) + 0.5 * width_extra
            xs = [float(n["x"]) for n in band]
            ys = [float(n["y"]) for n in band]
            margin = half_ref + pad + falloff + 1.0
            px0, py0 = _to_px_beamng(min(xs) - margin, max(ys) + margin, size, extent)
            px1, py1 = _to_px_beamng(max(xs) + margin, min(ys) - margin, size, extent)
            c0 = max(0, int(math.floor(min(px0, px1))))
            c1 = min(size - 1, int(math.ceil(max(px0, px1))))
            r0 = max(0, int(math.floor(min(py0, py1))))
            r1 = min(size - 1, int(math.ceil(max(py0, py1))))

            for py in range(r0, r1 + 1):
                for px in range(c0, c1 + 1):
                    bx, by = _from_px_beamng(float(px), float(py), size, extent)
                    proj = _project_point_to_nodes(bx, by, band, z_key="road")
                    if proj is None:
                        continue
                    dist, z_road, hw = proj
                    half = hw + 0.5 * width_extra + pad
                    outer = half + max(falloff, 1e-6)
                    if dist > outer:
                        continue
                    tested += 1
                    target = float(z_road) - sink
                    z0 = float(out[py, px])
                    # Only fix small lips (2–20cm). Never drag mountain walls down.
                    if abs(z0 - target) > max_delta:
                        continue
                    if dist <= half:
                        w = 1.0
                    else:
                        w = 1.0 - (dist - half) / max(falloff, 1e-6)
                        w = max(0.0, min(1.0, w))
                    z_new = (1.0 - w) * z0 + w * target
                    if abs(z_new - z0) < 1e-4:
                        continue
                    out[py, px] = z_new
                    changed += 1
                    max_abs = max(max_abs, abs(z_new - z0))

    stats = {
        "tested": tested,
        "changed": changed,
        "max_delta_m": round(max_abs, 3),
        "method": "approach_conform_z_road",
    }
    print(
        f"Approach conform: tested={tested} changed={changed} "
        f"max_delta={max_abs:.3f}m (heightmap → MeshRoad Z, soft shoulders)"
    )
    return out, stats


def write_approach_conform_assets(
    proc: Path,
    level_name: str,
    *,
    elev: np.ndarray,
    max_height_m: float,
    hole: np.ndarray | None = None,
) -> Path:
    """Write approach-baked heightmap; sync import/ (keep holemap if provided)."""
    size = int(elev.shape[0])
    u16 = np.clip(
        np.round(elev / max(max_height_m, 1e-6) * 65535.0),
        0,
        65535,
    ).astype(np.uint16)
    carved_name = f"heightmap_{size}_gallery_approach.png"
    Image.fromarray(u16, mode="I;16").save(proc / carved_name)

    preset_path = proc / "terrainPreset.json"
    preset: dict = {}
    if preset_path.is_file():
        try:
            preset = json.loads(preset_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            preset = {}
    preset.setdefault("type", "TerrainData")
    preset.setdefault("name", "theTerrain")
    preset["heightScale"] = float(max_height_m)
    preset["heightMapPath"] = f"/levels/{level_name}/import/heightmap_{size}.png"
    preset["holeMapPath"] = f"/levels/{level_name}/import/theTerrain_holemap.png"
    preset_path.write_text(json.dumps(preset, indent=2), encoding="utf-8")

    user_import = USER_LEVELS / level_name / "import"
    if user_import.parent.is_dir():
        user_import.mkdir(parents=True, exist_ok=True)
        Image.fromarray(u16, mode="I;16").save(user_import / f"heightmap_{size}.png")
        (user_import / "terrainPreset.json").write_text(
            json.dumps(preset, indent=2), encoding="utf-8"
        )
        if hole is not None:
            Image.fromarray(hole, mode="L").save(user_import / "theTerrain_holemap.png")
            Image.fromarray(hole, mode="L").save(user_import / "holeMap.png")
        print(f"Synced approach-conform heightmap -> {user_import}")
        print("Re-import terrainPreset.json in World Editor (heightmap changed).")
    return proc / carved_name


def load_terrain_z_slope(site: dict):
    """Return (z_at(bx,by), slope_deg_at(bx,by)) from processed heightmap."""
    from PIL import Image

    proc = processed_dir(site)
    size = int((site.get("beamng") or {}).get("mask_size") or 512)
    mpp = float((site.get("beamng") or {}).get("meters_per_pixel") or 1.0)
    hm_path = proc / f"heightmap_{size}.png"
    meta_path = proc / "heightmap_meta.json"
    if not hm_path.is_file() or not meta_path.is_file():
        raise SystemExit(f"Missing heightmap - run build_smoke first ({hm_path})")
    hm = np.asarray(Image.open(hm_path), dtype=np.float64)
    max_h = float(json.loads(meta_path.read_text(encoding="utf-8"))["max_height_m"])
    n = int(hm.shape[0])
    elev = hm / 65535.0 * max_h
    # rows increase south (row0 = north / high by); cols = bx
    d_row, d_col = np.gradient(elev, mpp, mpp)
    slope = np.degrees(np.arctan(np.hypot(d_col, d_row)))

    def _rc(bx: float, by: float) -> tuple[int, int]:
        c = int(max(0, min(n - 1, round(bx / mpp))))
        r = int(max(0, min(n - 1, round((n - 1) - by / mpp))))
        return r, c

    def z_at(bx: float, by: float) -> float:
        """Bilinear heightmap sample (BeamNG terrain is continuous between posts)."""
        # cols = bx/mpp, rows = (n-1) - by/mpp
        fc = bx / mpp
        fr = (n - 1) - by / mpp
        c0 = int(math.floor(fc))
        r0 = int(math.floor(fr))
        c1 = c0 + 1
        r1 = r0 + 1
        c0 = max(0, min(n - 1, c0))
        c1 = max(0, min(n - 1, c1))
        r0 = max(0, min(n - 1, r0))
        r1 = max(0, min(n - 1, r1))
        tx = fc - math.floor(fc)
        ty = fr - math.floor(fr)
        tx = max(0.0, min(1.0, tx))
        ty = max(0.0, min(1.0, ty))
        z00 = float(elev[r0, c0])
        z10 = float(elev[r0, c1])
        z01 = float(elev[r1, c0])
        z11 = float(elev[r1, c1])
        z0 = z00 * (1.0 - tx) + z10 * tx
        z1 = z01 * (1.0 - tx) + z11 * tx
        return z0 * (1.0 - ty) + z1 * ty

    def slope_at(bx: float, by: float) -> float:
        r, c = _rc(bx, by)
        return float(slope[r, c])

    return z_at, slope_at


def _interior_portal_ends(
    span_infos: list[dict],
    *,
    tol_m: float = 2.0,
) -> set[tuple[object, str]]:
    """(objectid, 's0'|'s1') portals that abut another gallery on the same road."""
    interior: set[tuple[object, str]] = set()
    for a in span_infos:
        for b in span_infos:
            if a.get("objectid") == b.get("objectid"):
                continue
            if a.get("road_id") != b.get("road_id"):
                continue
            for end_a, sa in (("s0", float(a["gip_s0"])), ("s1", float(a["gip_s1"]))):
                for sb in (float(b["gip_s0"]), float(b["gip_s1"])):
                    if abs(sa - sb) <= tol_m:
                        interior.add((a.get("objectid"), end_a))
    return interior


def _node_nearest_s(nodes: list[dict], s_target: float) -> dict:
    return min(nodes, key=lambda n: abs(float(n["s"]) - s_target))


def _portal_doorway_bands(
    nodes: list[dict],
    info: dict,
    *,
    mode: str,
    portal_len: float,
    out_bite: float,
) -> list[list[dict]]:
    """Centerline node windows for portal doorways (or full corridor)."""
    s_p0, s_p1 = info.get("portal_s") or (nodes[0]["s"], nodes[-1]["s"])
    s_p0, s_p1 = float(s_p0), float(s_p1)
    if s_p1 < s_p0:
        s_p0, s_p1 = s_p1, s_p0
    s_mesh0 = float(info.get("s0", nodes[0]["s"]))
    s_mesh1 = float(info.get("s1", nodes[-1]["s"]))
    if mode == "corridor":
        return [_nodes_in_s_window(nodes, s_p0, s_p1)]

    anchors = info.get("portal_anchors") or {}
    a0 = anchors.get("s0") or {}
    a1 = anchors.get("s1") or {}
    jump0 = float(a0["jump_s"]) if a0.get("jump_s") is not None else s_p0
    jump1 = float(a1["jump_s"]) if a1.get("jump_s") is not None else s_p1
    return [
        _nodes_in_s_window(
            nodes,
            max(s_mesh0, jump0 - out_bite),
            min(s_p1, jump0 + portal_len),
        ),
        _nodes_in_s_window(
            nodes,
            max(s_p0, jump1 - portal_len),
            min(s_mesh1, jump1 + out_bite),
        ),
    ]


def carve_gallery_clearance(
    galleries: list[dict],
    span_infos: list[dict],
    *,
    size: int,
    extent: float,
    defaults: dict,
    elev: np.ndarray,
    max_height_m: float,
) -> tuple[np.ndarray, np.ndarray, dict]:
    """Sweep a width×height rectangle along Hermite CL; lower colliding terrain.

    Mental model: push a box (road width × clear_height) along the centerline
    and remove rock that intersects it. BeamNG holemaps cannot cut a height
    band (white = delete whole XY column → sky shafts). So we *carve* the
    heightmap: set surface Z to Hermite road Z inside the footprint. That
    clears the portal face without punching holes through the mountain peak.

    Returns (carved_elev_m, empty_hole_u8, stats).
    """
    carved = elev.astype(np.float64).copy()
    hole = np.zeros((size, size), dtype=np.uint8)
    info_by_oid = {info.get("objectid"): info for info in span_infos}

    pad_default = float(defaults.get("hole_pad_m") or 0.0)
    above = float(
        defaults["hole_above_road_m"]
        if defaults.get("hole_above_road_m") is not None
        else 0.15
    )
    out_bite_default = float(defaults.get("hole_out_bite_m") or 2.0)
    width_scale_default = float(defaults.get("hole_width_scale") or 1.0)
    clear_h_default = float(defaults.get("clear_height_m") or 3.0)
    # height of swept box (informational / future mesh clip); carve always
    # lowers to road floor when surface intersects [z_h, z_h+height].
    _ = clear_h_default

    tested = 0
    carved_n = 0
    skip_ok = 0
    max_drop = 0.0

    for g in galleries:
        mode = str(g.get("hole_mode") or defaults.get("hole_mode") or "portals").lower()
        if mode in ("none", "off", "false", "0"):
            continue
        nodes = g.get("nodes") or []
        if len(nodes) < 2:
            continue
        pad = float(g["hole_pad_m"] if g.get("hole_pad_m") is not None else pad_default)
        width_scale = float(
            g["hole_width_scale"]
            if g.get("hole_width_scale") is not None
            else width_scale_default
        )
        width_scale = max(0.2, min(1.5, width_scale))
        portal_len = float(
            g["hole_portal_length_m"]
            if g.get("hole_portal_length_m") is not None
            else defaults.get("hole_portal_length_m") or 4.0
        )
        out_bite = float(
            g["hole_out_bite_m"]
            if g.get("hole_out_bite_m") is not None
            else out_bite_default
        )
        clear_h = float(
            g["clear_height_m"]
            if g.get("clear_height_m") is not None
            else clear_h_default
        )
        info = info_by_oid.get(g.get("objectid")) or {}
        bands = _portal_doorway_bands(
            nodes, info, mode=mode, portal_len=portal_len, out_bite=out_bite
        )

        for band in bands:
            if len(band) < 1:
                continue
            half_ref = (
                0.5 * max(float(n.get("width") or 7.0) for n in band) * width_scale
            )
            xs = [float(n["x"]) for n in band]
            ys = [float(n["y"]) for n in band]
            margin = half_ref + pad + 1.0
            px0, py0 = _to_px_beamng(min(xs) - margin, max(ys) + margin, size, extent)
            px1, py1 = _to_px_beamng(max(xs) + margin, min(ys) - margin, size, extent)
            c0 = max(0, int(math.floor(min(px0, px1))))
            c1 = min(size - 1, int(math.ceil(max(px0, px1))))
            r0 = max(0, int(math.floor(min(py0, py1))))
            r1 = min(size - 1, int(math.ceil(max(py0, py1))))

            for py in range(r0, r1 + 1):
                for px in range(c0, c1 + 1):
                    bx, by = _from_px_beamng(float(px), float(py), size, extent)
                    proj = _project_point_to_nodes(bx, by, band)
                    if proj is None:
                        continue
                    dist, z_herm, _hw = proj
                    if dist > half_ref + pad:
                        continue
                    tested += 1
                    z_herm = float(z_herm)
                    z_terr = float(carved[py, px])
                    # Solid-below-heightmap intersects box [z_herm, z_herm+clear_h]
                    # iff surface is above the floor.
                    if z_terr <= z_herm + above:
                        skip_ok += 1
                        continue
                    # Carve surface down to road (clears the box). Do NOT hole —
                    # hole would delete the whole column and open sky above.
                    drop = z_terr - z_herm
                    carved[py, px] = z_herm
                    carved_n += 1
                    if drop > max_drop:
                        max_drop = drop

    stats = {
        "tested": tested,
        "carved": carved_n,
        "skip_ok": skip_ok,
        "max_drop_m": round(max_drop, 2),
        "clear_height_m": clear_h_default,
        "width_scale": width_scale_default,
        "method": "heightmap_carve",
    }
    print(
        f"Portal carve: tested={tested} carved={carved_n} "
        f"already_clear={skip_ok} max_drop={max_drop:.2f}m "
        f"width_scale={width_scale_default} (holemap left empty)"
    )
    return carved, hole, stats


def paint_gallery_holes(
    galleries: list[dict],
    span_infos: list[dict],
    *,
    size: int,
    extent: float,
    defaults: dict,
    z_terrain,
    slope_at=None,
) -> np.ndarray:
    """Deprecated path: holemap punch. Prefer ``carve_gallery_clearance``."""
    _ = (galleries, span_infos, extent, defaults, z_terrain, slope_at)
    return np.zeros((size, size), dtype=np.uint8)


def write_terrain_clear_assets(
    proc: Path,
    level_name: str,
    *,
    carved_elev: np.ndarray,
    hole: np.ndarray,
    max_height_m: float,
) -> tuple[Path, Path]:
    """Write empty holemap + carved heightmap; sync to user level import/."""
    size = int(carved_elev.shape[0])
    # 16-bit heightmap encoding (same as build_smoke)
    u16 = np.clip(
        np.round(carved_elev / max(max_height_m, 1e-6) * 65535.0),
        0,
        65535,
    ).astype(np.uint16)

    for name in (
        "theTerrain_holemap.png",
        "holeMap.png",
        f"holemap_{size}.png",
    ):
        Image.fromarray(hole, mode="L").save(proc / name)

    carved_name = f"heightmap_{size}_gallery.png"
    Image.fromarray(u16, mode="I;16").save(proc / carved_name)

    preset_path = proc / "terrainPreset.json"
    preset: dict = {}
    if preset_path.is_file():
        try:
            preset = json.loads(preset_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            preset = {}
    preset.setdefault("type", "TerrainData")
    preset.setdefault("name", "theTerrain")
    preset["heightScale"] = float(max_height_m)
    preset["heightMapPath"] = f"/levels/{level_name}/import/heightmap_{size}.png"
    preset["holeMapPath"] = f"/levels/{level_name}/import/theTerrain_holemap.png"
    preset_path.write_text(json.dumps(preset, indent=2), encoding="utf-8")

    user_import = USER_LEVELS / level_name / "import"
    if user_import.parent.is_dir():
        user_import.mkdir(parents=True, exist_ok=True)
        Image.fromarray(hole, mode="L").save(user_import / "theTerrain_holemap.png")
        Image.fromarray(hole, mode="L").save(user_import / "holeMap.png")
        # Overwrite import heightmap with carved portal clearance
        Image.fromarray(u16, mode="I;16").save(user_import / f"heightmap_{size}.png")
        (user_import / "terrainPreset.json").write_text(
            json.dumps(preset, indent=2), encoding="utf-8"
        )
        print(f"Synced carved heightmap + empty holemap -> {user_import}")
    return proc / carved_name, proc / "theTerrain_holemap.png"


def write_hole_assets(
    proc: Path,
    level_name: str,
    hole: np.ndarray,
) -> Path:
    """Write hole map + patch terrainPreset.json; sync to user level import/."""
    size = hole.shape[0]
    for name in (
        "theTerrain_holemap.png",
        "holeMap.png",
        f"holemap_{size}.png",
    ):
        Image.fromarray(hole, mode="L").save(proc / name)

    preset_path = proc / "terrainPreset.json"
    preset: dict = {}
    if preset_path.is_file():
        try:
            preset = json.loads(preset_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            preset = {}
    preset.setdefault("type", "TerrainData")
    preset.setdefault("name", "theTerrain")
    preset["holeMapPath"] = f"/levels/{level_name}/import/theTerrain_holemap.png"
    preset_path.write_text(json.dumps(preset, indent=2), encoding="utf-8")

    user_import = USER_LEVELS / level_name / "import"
    if user_import.parent.is_dir():
        user_import.mkdir(parents=True, exist_ok=True)
        Image.fromarray(hole, mode="L").save(user_import / "theTerrain_holemap.png")
        Image.fromarray(hole, mode="L").save(user_import / "holeMap.png")
        (user_import / "terrainPreset.json").write_text(
            json.dumps(preset, indent=2), encoding="utf-8"
        )
        print(f"Synced hole map -> {user_import}")
    return proc / "theTerrain_holemap.png"


# --- Level inject --------------------------------------------------------------

def make_debug_centerline_meshroad(
    name: str,
    nodes: list[dict],
    *,
    lift_m: float = 2.5,
    width_m: float = 0.35,
) -> dict:
    """Thin MeshRoad along gallery axis for WE debug.

    Lifted well above the deck so it cannot act as an invisible tire curb
    (MeshRoad always has collision in BeamNG).
    """
    mesh_nodes = []
    for i, n in enumerate(nodes):
        tx = float(n.get("tx") or 0.0)
        ty = float(n.get("ty") or 0.0)
        if abs(tx) + abs(ty) < 1e-9 and i + 1 < len(nodes):
            tx = float(nodes[i + 1]["x"]) - float(n["x"])
            ty = float(nodes[i + 1]["y"]) - float(n["y"])
        elif abs(tx) + abs(ty) < 1e-9 and i > 0:
            tx = float(n["x"]) - float(nodes[i - 1]["x"])
            ty = float(n["y"]) - float(nodes[i - 1]["y"])
        # flat up-normal for visibility
        mesh_nodes.append(
            [
                round(float(n["x"]), 3),
                round(float(n["y"]), 3),
                round(float(n["z_road"]) + lift_m, 3),
                round(width_m, 2),
                0.05,
                0.0,
                0.0,
                1.0,
            ]
        )
    return {
        "name": name,
        "class": "MeshRoad",
        "__parent": "gallery_debug",
        "topMaterial": "GalleryDebugCL",
        "bottomMaterial": "GalleryDebugCL",
        "sideMaterial": "GalleryDebugCL",
        "textureLength": 4,
        "breakAngle": 3,
        "widthSubdivisions": 0,
        "nodes": mesh_nodes,
    }


def ensure_debug_centerline_material(user_level: Path, level_name: str) -> None:
    mats_path = user_level / "art" / "road" / "main.materials.json"
    mats_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if mats_path.is_file() and mats_path.stat().st_size:
        try:
            data = json.loads(mats_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    data["GalleryDebugCL"] = {
        "name": "GalleryDebugCL",
        "mapTo": "GalleryDebugCL",
        "class": "Material",
        "persistentId": "deb06c11-0000-4e1e-9c11-0000deb06c11",
        "Stages": [
            {
                "baseColorFactor": [1.0, 0.15, 0.9, 1.0],
                "roughnessFactor": 0.7,
                "emissiveFactor": [0.4, 0.05, 0.35],
            },
            {},
            {},
            {},
        ],
        "materialTag0": "RoadAndPath",
        "materialTag1": "beamng",
        "version": 1.5,
    }
    mats_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def inject_debug_centerlines(level_name: str, centerlines: list[dict]) -> Path | None:
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        return None
    ensure_debug_centerline_material(user_level, level_name)
    group_dir = user_level / "main" / "MissionGroup" / "level_objects" / "gallery_debug"
    group_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for g in centerlines:
        nodes = g.get("nodes") or []
        if len(nodes) < 2:
            continue
        oid = g.get("objectid") or "x"
        slug = bb._slug(str(g.get("name") or "gallery"))
        entries.append(
            make_debug_centerline_meshroad(f"debug_cl_{slug}_{oid}", nodes)
        )
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
    if "gallery_debug" not in names:
        lines.append(
            json.dumps(
                {
                    "name": "gallery_debug",
                    "class": "SimGroup",
                    "__parent": "level_objects",
                    "enabled": "1",
                },
                separators=(",", ":"),
            )
        )
        lo_items.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("Registered SimGroup gallery_debug under level_objects")
    print(f"Injected {len(entries)} debug centerline MeshRoad(s) -> {items_path}")
    return items_path


def inject_gallery_tsstatics(
    level_name: str,
    entries: list[dict],
) -> Path | None:
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        print(f"Level folder missing: {user_level}")
        return None
    group_dir = user_level / "main" / "MissionGroup" / "level_objects" / "galleries"
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
    if "galleries" not in names:
        lines.append(
            json.dumps(
                {"name": "galleries", "class": "SimGroup", "__parent": "level_objects", "enabled": "1"},
                separators=(",", ":"),
            )
        )
        lo_items.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("Registered SimGroup galleries under level_objects")
    return items_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--step", type=float, default=None)
    ap.add_argument(
        "--only",
        type=str,
        default=None,
        help="Comma-separated OBJECTIDs to build (e.g. 7236,8276)",
    )
    ap.add_argument(
        "--holes-only",
        action="store_true",
        help="Only rebuild hole map / meta (skip DAE + TSStatic inject)",
    )
    args = ap.parse_args()

    site = load_site()
    sc = SiteCoords(site)
    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "").strip()
    if not level_name:
        raise SystemExit("beamng.level_name missing in site config")

    proc = processed_dir(site)
    roads = bb.load_road_polylines(proc)

    defaults, items = default_gallery_cfg(bng)
    if args.step is not None:
        defaults["step_m"] = args.step
    only_ids = None
    if args.only:
        only_ids = {int(x.strip()) for x in args.only.split(",") if x.strip()}

    feats = gallery_features(site, sc)
    if not feats:
        raise SystemExit("No gallery/tunnel features in GIP cache for this site")

    gal_raw = bng.get("galleries") or {}
    merge_on = bool(gal_raw.get("merge_abutting", True))
    merge_tol = float(gal_raw.get("merge_tol_m") or 15.0)
    merge_groups = gal_raw.get("merge_groups") or None
    if merge_on:
        feats = merge_abutting_gallery_features(
            feats,
            tol_m=merge_tol,
            same_name=True,
            groups=merge_groups,
        )

    if only_ids:
        feats = [
            f
            for f in feats
            if _feat_objectids(f) & only_ids
        ]
        if not feats:
            raise SystemExit(f"No features matched --only {sorted(only_ids)}")

    use_spline_default = _gallery_uses_road_spline(defaults)
    use_net = use_spline_default or str(defaults.get("centerline") or "").lower() == "strassennetz"
    if not use_net:
        for it in items:
            if str(it.get("profile") or it.get("z_profile") or "").lower() == "road_spline":
                use_net = True
                break
            if str(it.get("centerline") or "").lower() == "strassennetz":
                use_net = True
                break
    net_road = None
    if use_net:
        net_road = bb.load_strassennetz_road(proc)
        print(
            f"Centerline Strassennetz: {net_road.get('name')!r} "
            f"len={net_road['length']:.1f}m nodes={len(net_road['pts'])}"
        )
    elif not roads:
        raise SystemExit(f"Missing {proc / 'roads_beamng.json'}")

    centerlines = []
    span_infos = []
    ts_entries = []
    shapes_dir_proc = proc / "gallery_meshes"
    shapes_dir_proc.mkdir(parents=True, exist_ok=True)

    user_level = USER_LEVELS / level_name
    if user_level.is_dir() and not args.holes_only:
        ensure_gallery_material(
            user_level,
            level_name,
            str((defaults.get("materials") or {}).get("side") or "Concrete"),
        )
        ensure_gallery_material(
            user_level,
            level_name,
            str((defaults.get("materials") or {}).get("collar") or "GalleryRock"),
        )
        # MeshRoad asphalt/concrete (same as bridges)
        bb.ensure_meshroad_materials(user_level, level_name, defaults.get("materials") or {})
        (user_level / "art" / "shapes" / "galleries").mkdir(parents=True, exist_ok=True)

    z_terrain, slope_at = load_terrain_z_slope(site)

    for feat in feats:
        cfg = resolve_gallery_cfg(defaults, items, feat)
        if cfg.get("enabled") is False:
            print(f"skip oid={feat.get('objectid')} (enabled: false)")
            continue
        if feat["kind"] == "tunnel" and cfg.get("open_side") == defaults.get("open_side"):
            if not any(
                _match_objectid((it.get("match") or {}).get("objectid"), feat)
                for it in items
                if (it.get("match") or {}).get("objectid") is not None
            ):
                cfg["open_side"] = "none"
                cfg["style"] = {**cfg["style"], "shell": "tunnel"}

        spline = _gallery_uses_road_spline(cfg)
        strip_entries: list[dict] = []
        if spline:
            road = net_road if net_road is not None else bb.load_strassennetz_road(proc)
            nodes, strip_entries, info = build_gallery_road_spline(
                feat, road, z_terrain, cfg
            )
            info["seed_road_id"] = road.get("id")
            info["chain_ids"] = None
            info["chain_len_m"] = round(float(road["length"]), 2)
        else:
            if not roads:
                raise SystemExit(f"Missing {proc / 'roads_beamng.json'} (needed for hermite galleries)")
            seed = bb.pick_road(roads, feat["xy"])
            pad = float(cfg.get("portal_search_out_m") or 25.0) + float(
                cfg.get("portal_out_extra_m") or 2.0
            ) + 5.0
            road = extend_road_chain(roads, seed, tol_m=1.0, pad_m=pad)
            nodes, info = build_gallery_centerline(
                feat["xy"], road, cfg, z_terrain=z_terrain
            )
            info["seed_road_id"] = seed.get("id")
            info["chain_ids"] = road.get("chain_ids")
            info["chain_len_m"] = round(float(road["length"]), 2)
        info["name"] = feat["name"]
        info["objectid"] = feat.get("objectid")
        info["merged_from"] = feat.get("merged_from")
        info["kind"] = feat["kind"]
        info["gip_length_m"] = feat.get("length_m")
        info["match"] = cfg.get("match_id")
        span_infos.append(info)

        oid = feat.get("objectid") or len(centerlines)
        slug = bb._slug(str(feat["name"]))
        shape_vfs = None
        origin = (float(nodes[0]["x"]), float(nodes[0]["y"]), float(nodes[0]["z_road"]))
        n_tris = 0

        if not args.holes_only:
            mat = str((cfg.get("materials") or {}).get("side") or "Concrete")
            s_p0, s_p1 = info.get("portal_s") or (nodes[0]["s"], nodes[-1]["s"])
            s_p0, s_p1 = float(s_p0), float(s_p1)
            if s_p1 < s_p0:
                s_p0, s_p1 = s_p1, s_p0
            mesh_nodes = [n for n in nodes if s_p0 - 1e-6 <= float(n["s"]) <= s_p1 + 1e-6]
            if len(mesh_nodes) < 2:
                mesh_nodes = nodes

            # --- shell (walls + roof + open-side parapet/columns) ---
            col_style = str(
                (cfg.get("style") or {}).get("columns") or "rect"
            ).lower()
            build_parapet = col_style not in ("none", "off", "false", "0")
            verts, faces, origin = loft_gallery_shell(
                mesh_nodes,
                open_side=str(cfg.get("open_side") or "left"),
                wall_t=float(cfg.get("wall_thickness_m") or 0.4),
                overhang_m=float(cfg.get("overhang_m") or 0.6),
                open_extra_m=float(cfg.get("deck_open_extra_m") or 0.8),
                deck_width_extra_m=float(cfg.get("deck_width_extra_m") or 0.5),
                parapet_height_m=float(cfg.get("parapet_height_m") or 1.0),
                parapet_thickness_m=float(cfg.get("parapet_thickness_m") or 0.35),
                parapet_out_m=float(
                    cfg["parapet_out_m"] if cfg.get("parapet_out_m") is not None else 0.25
                ),
                column_spacing_m=float(cfg.get("column_spacing_m") or 4.0),
                column_along_m=float(cfg.get("column_along_m") or 0.4),
                column_lat_m=float(cfg.get("column_lat_m") or 0.35),
                column_out_m=float(
                    cfg["column_out_m"] if cfg.get("column_out_m") is not None else 0.0
                ),
                build_parapet=build_parapet,
            )
            n_tris = len(faces)
            dae_name = f"gallery_{slug}_{oid}.dae"
            write_collada(
                shapes_dir_proc / dae_name,
                verts,
                faces,
                material_name=mat,
                mesh_stem=Path(dae_name).stem,
            )
            if user_level.is_dir():
                write_collada(
                    user_level / "art" / "shapes" / "galleries" / dae_name,
                    verts,
                    faces,
                    material_name=mat,
                    mesh_stem=Path(dae_name).stem,
                )
            shape_vfs = f"/levels/{level_name}/art/shapes/galleries/{dae_name}"
            ts_entries.append(
                {
                    "name": f"gallery_{slug}_{oid}",
                    "class": "TSStatic",
                    "__parent": "galleries",
                    "position": [round(origin[0], 3), round(origin[1], 3), round(origin[2], 3)],
                    "scale": [1.0, 1.0, 1.0],
                    "shapeName": shape_vfs,
                    "collisionType": "Collision Mesh",
                    "decalType": "Collision Mesh",
                    "useInstanceRenderData": True,
                    "isRenderEnabled": True,
                }
            )

            # --- Fahrbahn (MeshRoad): road_spline = 4 strips; else single Hermite deck ---
            if cfg.get("deck_enabled", True):
                if spline and strip_entries:
                    ts_entries.extend(strip_entries)
                else:
                    deck_ext = max(
                        float(cfg.get("deck_extend_m") or 5.0),
                        float(cfg.get("approach_blend_m") or 0.0),
                    )
                    deck_nodes = [
                        n
                        for n in nodes
                        if (s_p0 - deck_ext) - 1e-6 <= float(n["s"]) <= (s_p1 + deck_ext) + 1e-6
                    ]
                    if len(deck_nodes) < 2:
                        deck_nodes = mesh_nodes
                    deck = make_gallery_deck_meshroad(
                        f"gallery_deck_{slug}_{oid}",
                        deck_nodes,
                        depth_m=float(cfg.get("deck_depth_m") or 0.5),
                        width_extra_m=float(cfg.get("deck_width_extra_m") or 0.0),
                        materials=cfg.get("materials") or {},
                        z_terrain=z_terrain,
                        portal_s=(s_p0, s_p1),
                        crossfall_max=float(cfg.get("crossfall_max") or 0.12),
                        open_side=str(cfg.get("open_side") or "left"),
                        open_extra_m=float(cfg.get("deck_open_extra_m") or 0.0),
                    )
                    ts_entries.append(deck)

            # --- Block portals at both ends ---
            portal_style = str(
                cfg.get("portal_frame")
                or (cfg.get("style") or {}).get("portal")
                or "block"
            ).lower()
            if portal_style not in ("none", "off", "false", "0"):
                clear_h = float(cfg.get("clear_height_m") or 3.0)
                roof_t = float(cfg.get("roof_thickness_m") or 0.5)
                block_m = float(cfg.get("portal_block_m") or 0.7)
                depth_m = float(cfg.get("portal_depth_m") or 1.2)
                over_m = float(cfg.get("portal_overhang_m") or 0.8)
                clear_x = float(cfg.get("portal_clear_extra_m") or 0.2)
                open_side = str(cfg.get("open_side") or "left")
                n0 = _node_nearest_s(mesh_nodes, s_p0)
                n1 = _node_nearest_s(mesh_nodes, s_p1)
                for end_name, node, into_sign in (
                    ("s0", n0, +1.0),
                    ("s1", n1, -1.0),
                ):
                    pv, pf, porig = loft_portal_frame(
                        node,
                        into_sign=into_sign,
                        clear_h=clear_h,
                        roof_t=roof_t,
                        block_m=block_m,
                        depth_m=depth_m,
                        overhang_m=over_m,
                        open_side=open_side,
                        clear_extra_m=clear_x,
                    )
                    n_tris += len(pf)
                    pdae = f"gallery_portal_{slug}_{oid}_{end_name}.dae"
                    write_collada(
                        shapes_dir_proc / pdae,
                        pv,
                        pf,
                        material_name=mat,
                        mesh_stem=Path(pdae).stem,
                    )
                    if user_level.is_dir():
                        write_collada(
                            user_level / "art" / "shapes" / "galleries" / pdae,
                            pv,
                            pf,
                            material_name=mat,
                            mesh_stem=Path(pdae).stem,
                        )
                    ts_entries.append(
                        {
                            "name": f"gallery_portal_{slug}_{oid}_{end_name}",
                            "class": "TSStatic",
                            "__parent": "galleries",
                            "position": [
                                round(porig[0], 3),
                                round(porig[1], 3),
                                round(porig[2], 3),
                            ],
                            "scale": [1.0, 1.0, 1.0],
                            "shapeName": f"/levels/{level_name}/art/shapes/galleries/{pdae}",
                            "collisionType": "Collision Mesh",
                            "decalType": "Collision Mesh",
                            "useInstanceRenderData": True,
                            "isRenderEnabled": True,
                        }
                    )
                    if cfg.get("portal_collar", True):
                        cv, cf, corig = loft_portal_collar(
                            node,
                            into_sign=into_sign,
                            z_at=z_terrain,
                            clear_h=clear_h,
                            roof_t=roof_t,
                            block_m=block_m,
                            depth_m=depth_m,
                            overhang_m=over_m,
                            open_side=open_side,
                            clear_extra_m=clear_x,
                            collar_out_m=float(cfg.get("portal_collar_out_m") or 5.0),
                            collar_side_m=float(cfg.get("portal_collar_side_m") or 7.0),
                            collar_sink_m=float(cfg.get("portal_collar_sink_m") or 0.1),
                            n_rings=int(cfg.get("portal_collar_rings") or 4),
                        )
                        if cf:
                            n_tris += len(cf)
                            collar_mat = str(
                                (cfg.get("materials") or {}).get("collar") or "GalleryRock"
                            )
                            tex_len = float(
                                (cfg.get("materials") or {}).get("texture_length") or 4.0
                            )
                            if user_level.is_dir():
                                ensure_gallery_material(user_level, level_name, collar_mat)
                            cdae = f"gallery_portal_collar_{slug}_{oid}_{end_name}.dae"
                            write_collada(
                                shapes_dir_proc / cdae,
                                cv,
                                cf,
                                material_name=collar_mat,
                                mesh_stem=Path(cdae).stem,
                                uv_scale_m=tex_len,
                            )
                            if user_level.is_dir():
                                write_collada(
                                    user_level / "art" / "shapes" / "galleries" / cdae,
                                    cv,
                                    cf,
                                    material_name=collar_mat,
                                    mesh_stem=Path(cdae).stem,
                                    uv_scale_m=tex_len,
                                )
                            ts_entries.append(
                                {
                                    "name": f"gallery_portal_collar_{slug}_{oid}_{end_name}",
                                    "class": "TSStatic",
                                    "__parent": "galleries",
                                    "position": [
                                        round(corig[0], 3),
                                        round(corig[1], 3),
                                        round(corig[2], 3),
                                    ],
                                    "scale": [1.0, 1.0, 1.0],
                                    "shapeName": (
                                        f"/levels/{level_name}/art/shapes/galleries/{cdae}"
                                    ),
                                    # Decorative rock fill only — never block the carriageway.
                                    "collisionType": "None",
                                    "decalType": "None",
                                    "useInstanceRenderData": True,
                                    "isRenderEnabled": True,
                                }
                            )

        centerlines.append(
            {
                "name": feat["name"],
                "objectid": feat.get("objectid"),
                "merged_from": feat.get("merged_from"),
                "kind": feat["kind"],
                "open_side": cfg.get("open_side"),
                "clear_height_m": cfg["clear_height_m"],
                "hole_mode": cfg.get("hole_mode"),
                "hole_pad_m": cfg.get("hole_pad_m"),
                "hole_portal_length_m": cfg.get("hole_portal_length_m"),
                "debug_centerline": cfg.get("debug_centerline", True),
                "portal_s": info.get("portal_s"),
                "deck_enabled": cfg.get("deck_enabled", True),
                "deck_width_extra_m": cfg.get("deck_width_extra_m"),
                "portal_frame": cfg.get("portal_frame"),
                "approach_blend_m": cfg.get("approach_blend_m"),
                "approach_mode": cfg.get("approach_mode"),
                "portal_ease_m": cfg.get("portal_ease_m"),
                "approach_lift_m": cfg.get("approach_lift_m"),
                "approach_conform": cfg.get("approach_conform"),
                "approach_conform_pad_m": cfg.get("approach_conform_pad_m"),
                "approach_conform_falloff_m": cfg.get("approach_conform_falloff_m"),
                "approach_conform_sink_m": cfg.get("approach_conform_sink_m"),
                "profile": info.get("profile") or info.get("z_profile"),
                "shape": shape_vfs,
                "origin": [round(origin[0], 3), round(origin[1], 3), round(origin[2], 3)],
                "mesh_tris": n_tris,
                "nodes": nodes,
            }
        )
        deck_note = (
            f"strips={len(strip_entries)}"
            if spline and strip_entries
            else f"deck={cfg.get('deck_enabled', True)}"
        )
        print(
            f"{feat['kind']} {feat['name']!r} oid={oid}: "
            f"span={info['span_len_m']}m gip={info['gip_len_on_road_m']}m "
            f"z_profile={info.get('z_profile')} "
            f"portal_z={info.get('portal_z')} portal_s={info.get('portal_s')} "
            f"{deck_note} "
            f"portal_frame={cfg.get('portal_frame')} "
            + (f"tris={n_tris}" if not args.holes_only else "(holes-only)")
        )

    cl_path = proc / "galleries_centerlines.json"
    cl_path.write_text(
        json.dumps({"level": level_name, "galleries": centerlines}, indent=2),
        encoding="utf-8",
    )

    size = int(bng.get("mask_size") or 512)
    extent = float(sc.terrain_extent)
    _ = (extent, z_terrain, slope_at)
    # Preserve manual holemap if present; otherwise keep empty.
    hole_path = proc / "theTerrain_holemap.png"
    manual = proc / "theTerrain_holemap_manual.png"
    if manual.is_file():
        hole = np.asarray(Image.open(manual), dtype=np.uint8)
        if hole.ndim == 3:
            hole = hole[..., 0]
        print(f"Keeping manual holemap ({int((hole > 0).sum())} holes) from {manual.name}")
    elif hole_path.is_file():
        hole = np.asarray(Image.open(hole_path), dtype=np.uint8)
        if hole.ndim == 3:
            hole = hole[..., 0]
        if int((hole > 0).sum()) == 0:
            hole = np.zeros((size, size), dtype=np.uint8)
        else:
            print(f"Keeping existing holemap ({int((hole > 0).sum())} holes)")
    else:
        hole = np.zeros((size, size), dtype=np.uint8)
    if hole.shape[0] != size or hole.shape[1] != size:
        hole = np.zeros((size, size), dtype=np.uint8)
    hole_path = write_hole_assets(proc, level_name, hole)
    # Optional: bake approach heightmap to MeshRoad Z (flush seam, no 2–5cm lips)
    any_conform = any(
        bool(g.get("approach_conform", defaults.get("approach_conform")))
        for g in centerlines
    ) or bool(defaults.get("approach_conform"))
    if any_conform and centerlines:
        hm_path = proc / f"heightmap_{size}.png"
        meta_hm = json.loads((proc / "heightmap_meta.json").read_text(encoding="utf-8"))
        max_h = float(meta_hm["max_height_m"])
        elev0 = np.asarray(Image.open(hm_path), dtype=np.float64) / 65535.0 * max_h
        elev1, conf_stats = conform_approach_heightmap(
            centerlines,
            span_infos,
            size=size,
            extent=extent,
            defaults=defaults,
            elev=elev0,
        )
        conf_path = write_approach_conform_assets(
            proc,
            level_name,
            elev=elev1,
            max_height_m=max_h,
            hole=hole,
        )
        print(f"Wrote {conf_path}")
    else:
        conf_stats = None
        # Restore pristine DGM heightmap (undo prior approach_conform bake).
        hm_src = proc / f"heightmap_{size}.png"
        user_import = USER_LEVELS / level_name / "import"
        if hm_src.is_file() and user_import.parent.is_dir():
            user_import.mkdir(parents=True, exist_ok=True)
            shutil.copy2(hm_src, user_import / f"heightmap_{size}.png")
            print(
                f"Restored DGM heightmap -> {user_import / f'heightmap_{size}.png'} "
                "(re-import terrainPreset.json if previous conform was loaded)"
            )
    coverage = float(hole.mean()) / 255.0 * 100.0
    print(f"Wrote {hole_path} coverage={coverage:.3f}% (manual/preserve workflow)")

    meta = {
        "count": len(centerlines),
        "level": level_name,
        "defaults": {k: defaults[k] for k in GALLERY_SCALAR_KEYS},
        "style_defaults": defaults["style"],
        "materials_defaults": defaults["materials"],
        "spans": span_infos,
        "centerlines_file": str(cl_path.relative_to(ROOT)).replace("\\", "/"),
        "holemap": str(hole_path.relative_to(ROOT)).replace("\\", "/"),
        "hole_coverage_pct": round(coverage, 3),
        "approach_conform": conf_stats,
        "note": (
            "approach_mode=marry: MeshRoad follows DGM outside portals "
            "(skinned to terrain); Hermite only inside + short portal_ease. "
            "Holemap preserved. Re-import terrainPreset if heightmap was restored."
        ),
    }
    meta_path = proc / "galleries_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {cl_path}")
    print(f"Wrote {meta_path}")

    if defaults.get("debug_centerline", False):
        debug_cls = [
            g for g in centerlines if g.get("debug_centerline", False) is not False
        ]
        inject_debug_centerlines(level_name, debug_cls)
    else:
        # Remove leftover debug MeshRoad (it has collision and shreds tires on the deck)
        dbg = (
            USER_LEVELS
            / level_name
            / "main"
            / "MissionGroup"
            / "level_objects"
            / "gallery_debug"
            / "items.level.json"
        )
        if dbg.is_file():
            dbg.write_text("", encoding="utf-8")
            print(f"Cleared debug centerline MeshRoad -> {dbg}")

    if args.holes_only:
        print("Skipped gallery mesh inject (--holes-only). Re-import terrainPreset.json.")
        return

    items_path = inject_gallery_tsstatics(level_name, ts_entries)
    if items_path:
        print(f"Injected: {items_path}")
        print("Reload level (deck MeshRoad + portal TSStatics). Holemap preserved if manual.")
    else:
        print("Skipped inject (create level folder first).")


if __name__ == "__main__":
    main()
