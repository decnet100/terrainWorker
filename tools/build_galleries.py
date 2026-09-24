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
import time
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from site_coords import SiteCoords, load_site, processed_dir  # noqa: E402
import build_bridges as bb  # noqa: E402
from road_width import default_carriageway_width_m  # noqa: E402

USER_LEVELS = bb.USER_LEVELS

GALLERY_SCALAR_KEYS = (
    "width_m",
    "width_from_road",
    "clear_width_m",  # inner tube width (walls sit outside); None = road width
    "clear_height_m",
    "roof_thickness_m",
    "wall_thickness_m",
    "wall_radius_m",  # closed tube: side-wall arc (≥ half clear height; 0 = rectangular)
    "wall_arc_segments",
    "extend_before_m",
    "extend_after_m",
    "step_m",
    "crossfall_max",
    "open_side",
    "z_profile",  # linear (default) | road | road_spline | hermite (legacy)
    "profile",  # alias: road_spline | hermite (bridges naming)
    "centerline",  # osm | strassennetz | gip
    "free_span",  # linear | pchip_ends
    "solid_run_m",
    "span_dip_m",
    "span_search_m",
    "deck_lift_m",
    "corner_up_m",
    "corner_down_m",
    "corner_band",
    "abutment_s",  # optional [s0, s1] override along centerline
    "append_unify",  # one_ended: sequential XY of this unify past the portal
    "append_m",  # metres of that axis from the portal (None = full unify)
    "abut_run_m",  # last metres of that span held at abutment Z (bridge pad)
    "abutment_z",  # optional target Z; else A12 / max(road, DGM)
    "abut_snap_z",
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
    "portal_seat",  # bake 2 m apron + roof force + door hole at daylight portals
    "portal_apron_m",  # meters toward the exit: terrain → deck Z
    "portal_roof_force_m",  # meters inside the portal where roof Z is enforced
    "portal_roof_force_len_m",  # how far that raise continues into the bore
    "portal_hole",  # punch holemap at the portal door
    "portal_hole_in_m",  # hole extends this far inside from the portal
    "portal_hole_out_m",  # hole slightly past the portal face
    "portal_apron_pad_m",  # extra half-width on the deck apron
    "bore_clear",  # closed tube: carve DGM that sits inside the driving box
    "roof_conform_in_m",  # metres inside the daylight portal before roof owns Z
    "roof_conform_max_cut_m",  # lower thin overburden onto roof outer
    "roof_conform_max_raise_m",  # raise voids up to roof outer
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
    "approach_conform",  # Default on: seat Auflage to deck Z (bore never)
    "approach_conform_pad_m",
    "approach_conform_falloff_m",
    "approach_conform_sink_m",  # terrain slightly under MeshRoad (anti z-fight)
    "approach_conform_max_delta_m",  # skip mountain cells (|Δz| too large)
    "approach_conform_max_raise_m",
    "approach_conform_max_cut_m",
    "approach_conform_range_m",
    "approach_conform_pull_m",
    "approach_conform_side_cut",
    "approach_conform_side_range_m",
    "approach_conform_side_drop_m",
    "side_cut_preserve",  # {objekt, objectid} skip MeshRoad side-cut
    "approach_conform_side_cut_preserve",
    "force_deck_z",  # Default on: high DGM under the outer plate → Oberkante
    "force_deck_z_sink_m",
    "force_deck_z_fill",  # also raise low DGM under the plate to deck − sink
    # Terrain above span: none (default) | rock | keep_asphalt (explicit only)
    "terrain_roof",
    "one_ended",  # only one daylight portal on this map
    "fake_end_drop_m",  # MeshRoad Z at the in-map far end vs real portal
    "one_ended_daylight",  # s0 | s1 | auto
    "daylight_near_objectid",  # pick the portal closer to this GIP piece
    "portal_shift_out_m",  # metres along the road, outward from the door
    "portal_shift_from_objectid",  # shift origin (GIP); default = chosen portal
    # Portal/opening lip bake so holemap has free 1×1 tiles at edges
    "terrain_embed",
    "terrain_embed_upper",  # bake roof lip (default off until roof edge is solid)
    "terrain_embed_rings",  # legacy fallback → meters via mpp (both lips)
    "terrain_embed_upper_m",  # hangward hard radius from upper station (m)
    "terrain_embed_upper_inset_m",  # pull magenta station inward vs green (≥1 cell)
    "terrain_embed_lower_m",  # outward hard radius from foundation line (m)
    "terrain_embed_in_m",  # inward hard radius toward road (m); None → foundation_m
    "terrain_embed_soft_m",  # soft falloff beyond hard radius (both sides)
    "terrain_embed_curve",  # smoothstep | cosine | linear
    "terrain_embed_foundation_m",  # lower lip Z drop + outward offset of lip line
    "terrain_embed_lower_z_m",  # lower lip only: + raises bake/debug Z toward deck underside
    "terrain_embed_max_delta_m",
    "terrain_embed_open_side",  # bake long lookout opening (open flank only)
    "terrain_embed_portal_face",  # bake portal-front lower lip (default off)
    "terrain_embed_portal_face_upper",  # also bake portal-front upper lip (opt-in)
    "terrain_embed_portal_holes",  # holemap strip between portal green/magenta faces
    "terrain_embed_debug",  # MeshRoad of foundation/roof lip lines in WE
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
    "trim_s0_m",  # meters to drop from GIP polyline start (portal s0)
    "trim_s1_m",  # meters to drop from GIP polyline end (portal s1)
    "fitout",  # auto | true | false — ceiling lamps/fans in closed tubes
    "fitout_min_m",  # auto: only if bore length exceeds this (default 100)
    "lamp_spacing_m",
    "lamp_inset_m",  # skip this many metres inside each portal
    "lamp_drop_m",  # hanging below roof underside
    "lamp_range_m",
    "lamp_brightness",
    "lamp_light",  # PointLight at each lamp; false = mesh only
    "fan_spacing_m",
    "fan_inset_m",
    "fan_diameter_m",
    "fan_length_m",
    "fan_drop_m",  # cylinder centre below roof; None = radius + 2 cm
    "fan_wall_m",
    "fan_rpm",  # UV rotate on static discs; 0 = still blades
    "fan_blades",
)


def default_gallery_cfg(bng: dict) -> tuple[dict, list]:
    raw = bng.get("galleries") or {}
    defaults = {
        "width_m": (
            float(raw["width_m"])
            if raw.get("width_m") is not None
            else default_carriageway_width_m(bng)
        ),
        "width_from_road": bool(raw.get("width_from_road", True)),
        "clear_width_m": (
            float(raw["clear_width_m"])
            if raw.get("clear_width_m") is not None
            else None
        ),
        "clear_height_m": float(raw.get("clear_height_m") or 3.0),
        "roof_thickness_m": float(raw.get("roof_thickness_m") or 0.5),
        "wall_thickness_m": float(raw.get("wall_thickness_m") or 0.4),
        "wall_radius_m": (
            float(raw["wall_radius_m"]) if raw.get("wall_radius_m") is not None else None
        ),
        "wall_arc_segments": int(raw.get("wall_arc_segments") or 10),
        "extend_before_m": float(raw.get("extend_before_m") or 1.0),
        "extend_after_m": float(raw.get("extend_after_m") or 1.0),
        "step_m": float(raw.get("step_m") or 2.0),
        "crossfall_max": float(raw.get("crossfall_max") or 0.12),
        "open_side": str(raw.get("open_side") or "left"),
        # linear: straight Z between portals (approaches stay on DGM).
        # road / road_spline: follow the road axis. hermite = legacy, do not use.
        "z_profile": str(raw.get("z_profile") or "linear"),
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
        "append_unify": str(raw.get("append_unify") or ""),
        "append_m": (
            float(raw["append_m"]) if raw.get("append_m") is not None else None
        ),
        "abut_run_m": float(raw.get("abut_run_m") or 5.0),
        "abutment_z": (
            float(raw["abutment_z"]) if raw.get("abutment_z") is not None else None
        ),
        "abut_snap_z": bool(raw.get("abut_snap_z", True)),
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
        "portal_seat": bool(raw.get("portal_seat", True)),
        "portal_apron_m": float(
            raw["portal_apron_m"] if raw.get("portal_apron_m") is not None else 8.0
        ),
        "portal_roof_force_m": float(
            raw["portal_roof_force_m"]
            if raw.get("portal_roof_force_m") is not None
            else 0.0
        ),
        "portal_roof_force_len_m": float(
            raw["portal_roof_force_len_m"]
            if raw.get("portal_roof_force_len_m") is not None
            else 0.0
        ),
        "portal_hole": bool(raw.get("portal_hole", False)),
        "portal_hole_in_m": float(raw.get("portal_hole_in_m") or 1.0),
        "portal_hole_out_m": float(raw.get("portal_hole_out_m") or 0.25),
        "portal_apron_pad_m": float(raw.get("portal_apron_pad_m") or 0.5),
        "bore_clear": bool(raw.get("bore_clear", True)),
        "roof_conform_in_m": float(
            raw["roof_conform_in_m"]
            if raw.get("roof_conform_in_m") is not None
            else 12.0
        ),
        "roof_conform_max_cut_m": float(
            raw["roof_conform_max_cut_m"]
            if raw.get("roof_conform_max_cut_m") is not None
            else 8.0
        ),
        "roof_conform_max_raise_m": float(
            raw["roof_conform_max_raise_m"]
            if raw.get("roof_conform_max_raise_m") is not None
            else 1.5
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
        "approach_conform": bool(raw.get("approach_conform", True)),
        "approach_conform_pad_m": float(raw.get("approach_conform_pad_m") or 0.5),
        "approach_conform_falloff_m": float(raw.get("approach_conform_falloff_m") or 1.5),
        "approach_conform_sink_m": float(raw.get("approach_conform_sink_m") or 0.02),
        "approach_conform_max_delta_m": float(
            raw.get("approach_conform_max_delta_m") or 0.35
        ),
        "approach_conform_max_raise_m": (
            float(raw["approach_conform_max_raise_m"])
            if raw.get("approach_conform_max_raise_m") is not None
            else None
        ),
        "approach_conform_max_cut_m": (
            float(raw["approach_conform_max_cut_m"])
            if raw.get("approach_conform_max_cut_m") is not None
            else None
        ),
        "approach_conform_range_m": (
            float(raw["approach_conform_range_m"])
            if raw.get("approach_conform_range_m") is not None
            else None
        ),
        "approach_conform_pull_m": (
            float(raw["approach_conform_pull_m"])
            if raw.get("approach_conform_pull_m") is not None
            else None
        ),
        "approach_conform_side_cut": bool(raw.get("approach_conform_side_cut", True)),
        "approach_conform_side_range_m": (
            float(raw["approach_conform_side_range_m"])
            if raw.get("approach_conform_side_range_m") is not None
            else None
        ),
        "approach_conform_side_drop_m": (
            float(raw["approach_conform_side_drop_m"])
            if raw.get("approach_conform_side_drop_m") is not None
            else None
        ),
        "side_cut_preserve": (
            dict(raw["side_cut_preserve"])
            if isinstance(raw.get("side_cut_preserve"), dict)
            else None
        ),
        "approach_conform_side_cut_preserve": (
            dict(raw["approach_conform_side_cut_preserve"])
            if isinstance(raw.get("approach_conform_side_cut_preserve"), dict)
            else None
        ),
        "force_deck_z": bool(raw.get("force_deck_z", True)),
        "force_deck_z_sink_m": (
            float(raw["force_deck_z_sink_m"])
            if raw.get("force_deck_z_sink_m") is not None
            else None
        ),
        "force_deck_z_fill": bool(raw.get("force_deck_z_fill", True)),
        # none = Spanne lässt das Gelände (Default). rock / keep_asphalt nur
        # wenn YAML das Dach ausdrücklich setzt.
        "terrain_roof": str(raw.get("terrain_roof") or "none").lower(),
        "one_ended": bool(raw.get("one_ended", False)),
        "fake_end_drop_m": float(raw.get("fake_end_drop_m") or 10.0),
        "one_ended_daylight": str(raw.get("one_ended_daylight") or "auto").lower(),
        "daylight_near_objectid": raw.get("daylight_near_objectid"),
        "portal_shift_out_m": float(raw.get("portal_shift_out_m") or 0.0),
        "portal_shift_from_objectid": raw.get("portal_shift_from_objectid"),
        "terrain_embed": bool(raw.get("terrain_embed", False)),
        "terrain_embed_upper": bool(raw.get("terrain_embed_upper", False)),
        "terrain_embed_rings": int(raw.get("terrain_embed_rings") or 2),
        "terrain_embed_upper_m": (
            float(raw["terrain_embed_upper_m"])
            if raw.get("terrain_embed_upper_m") is not None
            else None
        ),
        "terrain_embed_upper_inset_m": (
            float(raw["terrain_embed_upper_inset_m"])
            if raw.get("terrain_embed_upper_inset_m") is not None
            else None
        ),
        "terrain_embed_lower_m": (
            float(raw["terrain_embed_lower_m"])
            if raw.get("terrain_embed_lower_m") is not None
            else None
        ),
        "terrain_embed_in_m": (
            float(raw["terrain_embed_in_m"])
            if raw.get("terrain_embed_in_m") is not None
            else None
        ),
        "terrain_embed_soft_m": float(
            raw["terrain_embed_soft_m"]
            if raw.get("terrain_embed_soft_m") is not None
            else 1.0
        ),
        "terrain_embed_curve": str(raw.get("terrain_embed_curve") or "smoothstep"),
        "terrain_embed_foundation_m": float(
            raw.get("terrain_embed_foundation_m")
            if raw.get("terrain_embed_foundation_m") is not None
            else 1.0
        ),
        "terrain_embed_lower_z_m": float(
            raw.get("terrain_embed_lower_z_m")
            if raw.get("terrain_embed_lower_z_m") is not None
            else 0.0
        ),
        "terrain_embed_max_delta_m": float(
            raw.get("terrain_embed_max_delta_m")
            if raw.get("terrain_embed_max_delta_m") is not None
            else 12.0
        ),
        "terrain_embed_open_side": bool(raw.get("terrain_embed_open_side", True)),
        # Portal-front lower lip — off by default (no hang-side lips ever)
        "terrain_embed_portal_face": bool(raw.get("terrain_embed_portal_face", False)),
        "terrain_embed_portal_face_upper": bool(
            raw.get("terrain_embed_portal_face_upper", False)
        ),
        "terrain_embed_portal_holes": bool(
            raw.get("terrain_embed_portal_holes", False)
        ),
        "terrain_embed_debug": bool(raw.get("terrain_embed_debug", False)),
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
        "fitout": raw["fitout"] if "fitout" in raw else "auto",
        "fitout_min_m": float(raw.get("fitout_min_m") or 100.0),
        "lamp_spacing_m": float(raw.get("lamp_spacing_m") or 12.0),
        "lamp_inset_m": float(raw.get("lamp_inset_m") or 10.0),
        "lamp_drop_m": float(raw.get("lamp_drop_m") or 0.12),
        "lamp_range_m": float(raw.get("lamp_range_m") or 20.0),
        "lamp_brightness": float(raw.get("lamp_brightness") or 1.6),
        "lamp_light": bool(raw.get("lamp_light", True)),
        "fan_spacing_m": float(raw.get("fan_spacing_m") or 50.0),
        "fan_inset_m": float(raw.get("fan_inset_m") or 18.0),
        "fan_diameter_m": float(raw.get("fan_diameter_m") or 0.60),
        "fan_length_m": float(raw.get("fan_length_m") or 1.50),
        "fan_drop_m": (
            float(raw["fan_drop_m"]) if raw.get("fan_drop_m") is not None else None
        ),
        "fan_wall_m": float(raw.get("fan_wall_m") or 0.06),
        "fan_rpm": float(raw.get("fan_rpm") if raw.get("fan_rpm") is not None else 60.0),
        "fan_blades": int(raw.get("fan_blades") or 6),
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
        if "unify" in m and str(m["unify"]) != str(feat.get("unify_id") or ""):
            ok = False
        if "objectid" in m and not _match_objectid(m["objectid"], feat):
            ok = False
        if "name" in m and str(m["name"]).lower() not in name.lower():
            ok = False
        if "name_exact" in m and str(m["name_exact"]) != name:
            ok = False
        if ok and m:
            matched = item
            break
    if feat.get("kind") == "tunnel":
        cfg["terrain_roof"] = "none"
        cfg["open_side"] = "none"
        cfg["style"] = {**cfg["style"], "shell": "tunnel"}
        if cfg.get("clear_width_m") is None:
            cfg["clear_width_m"] = 9.5
        if cfg.get("wall_radius_m") is None:
            cfg["wall_radius_m"] = 4.5
        if float(cfg.get("clear_height_m") or 0.0) < 4.0:
            cfg["clear_height_m"] = 4.0
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


def _trim_polyline_m(
    xy: list[tuple[float, float]],
    trim_m: float,
    *,
    from_start: bool,
) -> list[tuple[float, float]]:
    """Drop ``trim_m`` meters from one end of a polyline."""
    if trim_m <= 0 or len(xy) < 2:
        return list(xy)
    pts = list(xy) if from_start else list(reversed(xy))
    acc = 0.0
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        if acc + seg <= trim_m + 1e-9:
            acc += seg
            continue
        t = (trim_m - acc) / seg if seg > 1e-9 else 0.0
        start = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
        out = [start] + pts[i + 1 :]
        return out if from_start else list(reversed(out))
    # trimmed away entire poly — keep a stub near the far end
    stub = pts[-2:] if from_start else list(reversed(pts[:2]))
    return stub if len(stub) >= 2 else list(xy)


def apply_gallery_span_trims(
    feat: dict, cfg: dict, site: dict, sc: SiteCoords
) -> dict:
    """Trim GIP axis ends before centerline build.

    ``trim_s0_m`` / ``trim_s1_m``: meters to cut from polyline start / end
    (same ends as portal s0 / s1). Increase a side to shorten that portal.
    """
    _ = (site, sc)
    xy = list(feat.get("xy") or [])
    if len(xy) < 2:
        return feat
    notes: list[str] = []
    for key, from_start in (("trim_s0_m", True), ("trim_s1_m", False)):
        tm = float(cfg.get(key) or 0.0)
        if tm > 0:
            before = _poly_len_xy(xy)
            xy = _trim_polyline_m(xy, tm, from_start=from_start)
            notes.append(f"{key}={tm:.1f} ({before:.1f}->{_poly_len_xy(xy):.1f}m)")
    if not notes:
        return feat
    out = dict(feat)
    out["xy"] = xy
    out["trim_notes"] = notes
    s0, s1 = xy[0], xy[-1]
    print(
        f"  gallery oid={feat.get('objectid')}: "
        f"after trim L={_poly_len_xy(xy):.1f}m "
        f"s0=({s0[0]:.1f},{s0[1]:.1f}) s1=({s1[0]:.1f},{s1[1]:.1f})"
    )
    for n in notes:
        print(f"  gallery oid={feat.get('objectid')}: {n}")
    return out


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
            close: list[tuple[int, int]] = []
            for ai, a in enumerate(ends_i):
                for bi, b in enumerate(ends_j):
                    if math.hypot(a[0] - b[0], a[1] - b[1]) <= tol_m:
                        close.append((ai, bi))
            if not close:
                continue
            # Parallel tubes share a mouth (both free ends also close) or both
            # endpoints. Serial GIP pieces often meet end-to-end (same index).
            if feats[i].get("kind") == "tunnel":
                if len(close) != 1:
                    continue
                ai, bi = close[0]
                free_i = ends_i[1 - ai]
                free_j = ends_j[1 - bi]
                if math.hypot(free_i[0] - free_j[0], free_i[1] - free_j[1]) <= max(
                    4.0 * tol_m, 40.0
                ):
                    continue
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
            "str_code": primary.get("str_code"),
            "merged_from": oids,
            "length_m": sum(lengths) if lengths else _poly_len_xy(xy),
            "xy": xy,
        }
        out.append(merged)
        print(
            f"merged gallery '{merged['name']}' objectids={oids} "
            f"-> primary={merged['objectid']} span~{_poly_len_xy(xy):.1f}m"
        )
    return out


def gallery_features(site: dict, sc: SiteCoords) -> list[dict]:
    path = bb.find_gip_geojson(site)
    data = json.loads(path.read_text(encoding="utf-8"))
    from gip_road_segments import not_tunnel_objectids
    from authorities import gip_source_oid, stamp_gip_props, transformer_gip_fc_into_working

    skip_oids = not_tunnel_objectids(site)
    to_site = transformer_gip_fc_into_working(site, data)
    out = []
    for f in data.get("features") or []:
        props = stamp_gip_props(site, f.get("properties") or {})
        kind = bb._structure_kind(props)
        objekt = str(props.get("OBJEKT") or "").upper().strip()
        blob = " ".join(
            str(props.get(k) or "")
            for k in ("KUNSTBAUTEN", "OBJEKTBEZEICHNUNG")
        ).lower()
        # STRNAME is the road title (e.g. "A12 Landecker Tunnel") — not a
        # structure flag. Surface A12 pieces stay out of this list.
        if kind not in ("gallery", "tunnel"):
            if objekt == "S-BG" or "galerie" in blob:
                kind = "gallery"
            elif objekt in ("S-AT", "S-BT", "S-LT") or any(
                k in blob for k in ("tunnel", "unterflur")
            ):
                kind = "tunnel"
            else:
                continue
        oid_raw = props.get("OBJECTID")
        src_raw = gip_source_oid(props)
        try:
            if (oid_raw is not None and int(oid_raw) in skip_oids) or (
                src_raw is not None and int(src_raw) in skip_oids
            ):
                continue
        except (TypeError, ValueError):
            pass
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
                "name": str(
                    props.get("KUNSTBAUTEN")
                    or props.get("STRNAME")
                    or props.get("OBJEKTBEZEICHNUNG")
                    or kind
                ),
                "objectid": props.get("OBJECTID"),
                "str_code": props.get("STR_CODE"),
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
        weld_adjacent_m=float(cfg.get("weld_adjacent_m") or 0.0),
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
            "approach_conform": bool(cfg.get("approach_conform", True)),
        }
    )
    return nodes, strip_entries, info


def _portal_daylight_score(p: dict, z_terrain) -> float:
    """Higher = more likely a real portal (grade jump, road on DGM)."""
    score = 0.0
    if str(p.get("method") or "") == "grade_jump":
        score += 100.0
    z_p = float(p.get("z") or 0.0)
    if z_terrain is not None and p.get("x") is not None:
        buried = float(z_terrain(float(p["x"]), float(p["y"]))) - z_p
        score -= buried
    return score


def _sample_fake_portal(road: dict, s: float, z: float, z_terrain) -> dict:
    x, y, zr, tx, ty, tz, w = bb.sample_road(road, s)
    z_terr = float(z_terrain(x, y)) if z_terrain is not None else float(zr)
    return {
        "s": round(float(s), 3),
        "z": round(float(z), 3),
        "z_terr": round(z_terr, 3),
        "grade": 0.0,
        "jump_s": None,
        "jump_grade": None,
        "method": "one_ended_fake",
        "x": round(x, 3),
        "y": round(y, 3),
        "width": round(w, 2),
        "tx": round(tx, 5),
        "ty": round(ty, 5),
        "tz": round(tz, 5),
    }


def _gip_oid_xy(oid: int, site: dict | None = None) -> tuple[float, float] | None:
    """Mean BeamNG XY of a GIP OBJECTID (for daylight_near_objectid)."""
    try:
        path = bb.find_gip_geojson(site or load_site())
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    sc = SiteCoords(site or load_site())
    from authorities import gip_ids_from_props, gip_source_oid, stamp_gip_props, transformer_gip_fc_into_working

    site = site or load_site()
    to_site = transformer_gip_fc_into_working(site, data)
    want = int(oid)
    for f in data.get("features") or []:
        pr = stamp_gip_props(site, f.get("properties") or {})
        ids = gip_ids_from_props(site, pr)
        src = gip_source_oid(pr)
        try:
            hit = (ids is not None and int(ids[0]) == want) or (
                src is not None and int(src) == want
            )
        except (TypeError, ValueError):
            hit = False
        if not hit:
            continue
        raw: list = []
        bb._walk_coords((f.get("geometry") or {}).get("coordinates"), raw)
        xs, ys = [], []
        for lon, lat, *_ in raw:
            x, y = to_site.transform(float(lon), float(lat))
            bx, by = sc.crs_to_beamng(x, y)
            xs.append(bx)
            ys.append(by)
        if xs:
            return (sum(xs) / len(xs), sum(ys) / len(ys))
    return None


def _one_ended_keep_end(cfg: dict, p0: dict, p1: dict) -> str:
    """Which portal is daylight: s0, s1, or auto."""
    raw = str(cfg.get("one_ended_daylight") or "auto").lower().strip()
    if raw in ("s0", "0"):
        return "s0"
    if raw in ("s1", "1"):
        return "s1"
    near = cfg.get("daylight_near_objectid")
    if near is None:
        return "auto"
    try:
        oid = int(near)
    except (TypeError, ValueError):
        return "auto"
    xy = _gip_oid_xy(oid)
    if xy is None:
        return "auto"
    d0 = math.hypot(float(p0.get("x") or 0.0) - xy[0], float(p0.get("y") or 0.0) - xy[1])
    d1 = math.hypot(float(p1.get("x") or 0.0) - xy[0], float(p1.get("y") or 0.0) - xy[1])
    return "s0" if d0 <= d1 else "s1"


def _resample_portal_at(road: dict, s: float, z_terrain, proto: dict) -> dict:
    """Move a portal to chainage s; Z from DGM so the door sits on the road."""
    s = max(0.0, min(float(road["length"]), float(s)))
    x, y, zr, tx, ty, tz, w = bb.sample_road(road, s)
    z = float(z_terrain(x, y)) if z_terrain is not None else float(zr)
    out = dict(proto)
    out.update(
        {
            "s": round(s, 3),
            "x": round(x, 3),
            "y": round(y, 3),
            "z": round(z, 3),
            "z_terr": round(z, 3),
            "width": round(w, 2),
            "tx": round(tx, 5),
            "ty": round(ty, 5),
            "tz": round(tz, 5),
            "method": str(proto.get("method") or "shift") + "+out",
        }
    )
    return out


def apply_one_ended_portals(
    cfg: dict,
    p0: dict,
    p1: dict,
    s_g0: float,
    s_g1: float,
    road: dict,
    z_terrain,
) -> tuple[dict, dict, dict]:
    """Keep one daylight portal; drop the in-map far end by ``fake_end_drop_m``.

    Real portal = grade jump / road on DGM. Fake portal sits at the other GIP
    end, ``fake_end_drop_m`` below the real portal Z (MeshRoad dives into the
    mountain). Shell mesh stays a short collar at the real end only.
    """
    meta = {
        "one_ended": False,
        "fake_end": None,
        "fake_end_drop_m": None,
        "shell_s": None,
    }
    if not cfg.get("one_ended"):
        return p0, p1, meta
    drop = float(cfg.get("fake_end_drop_m") or 10.0)
    keep = _one_ended_keep_end(cfg, p0, p1)
    sc0 = _portal_daylight_score(p0, z_terrain)
    sc1 = _portal_daylight_score(p1, z_terrain)
    if keep == "s0" or (keep != "s1" and sc0 >= sc1):
        fake_end = "s1"
        z_real = float(p0["z"])
        p1 = _sample_fake_portal(road, s_g1, z_real - drop, z_terrain)
        real_s = float(p0["s"])
        shell_s = [real_s, real_s + max(20.0, float(cfg.get("portal_depth_m") or 1.2) + 15.0)]
    else:
        fake_end = "s0"
        z_real = float(p1["z"])
        p0 = _sample_fake_portal(road, s_g0, z_real - drop, z_terrain)
        real_s = float(p1["s"])
        shell_s = [real_s - max(20.0, float(cfg.get("portal_depth_m") or 1.2) + 15.0), real_s]
    if float(p1["s"]) < float(p0["s"]):
        p0, p1 = p1, p0
        fake_end = "s0" if fake_end == "s1" else "s1"
        shell_s = [shell_s[1], shell_s[0]] if shell_s[1] < shell_s[0] else shell_s
    shift = float(cfg.get("portal_shift_out_m") or 0.0)
    if shift > 0.2:
        s_max = float(road["length"])
        s_from = float(p0["s"]) if fake_end == "s1" else float(p1["s"])
        from_oid = cfg.get("portal_shift_from_objectid")
        if from_oid is not None:
            try:
                ref = _gip_oid_xy(int(from_oid))
            except (TypeError, ValueError):
                ref = None
            if ref is not None:
                s_from = float(bb.project_on_road(road, ref[0], ref[1])[0])
        if fake_end == "s1":
            s_new = max(0.0, s_from - shift)
            p0 = _resample_portal_at(road, s_new, z_terrain, p0)
            p1 = _sample_fake_portal(
                road, float(p1["s"]), float(p0["z"]) - drop, z_terrain
            )
        else:
            s_new = min(s_max, s_from + shift)
            p1 = _resample_portal_at(road, s_new, z_terrain, p1)
            p0 = _sample_fake_portal(
                road, float(p0["s"]), float(p1["z"]) - drop, z_terrain
            )
        real_s = float(p0["s"]) if fake_end == "s1" else float(p1["s"])
        if fake_end == "s1":
            shell_s = [
                real_s,
                real_s + max(20.0, float(cfg.get("portal_depth_m") or 1.2) + 15.0),
            ]
        else:
            shell_s = [
                real_s - max(20.0, float(cfg.get("portal_depth_m") or 1.2) + 15.0),
                real_s,
            ]
    s_lo = min(float(p0["s"]), float(p1["s"]))
    s_hi = max(float(p0["s"]), float(p1["s"]))
    shell_s = [max(s_lo, min(s_hi, shell_s[0])), max(s_lo, min(s_hi, shell_s[1]))]
    if shell_s[1] < shell_s[0]:
        shell_s = [shell_s[1], shell_s[0]]
    if shell_s[1] - shell_s[0] < 4.0:
        if fake_end == "s1":
            shell_s = [s_lo, min(s_hi, s_lo + 20.0)]
        else:
            shell_s = [max(s_lo, s_hi - 20.0), s_hi]
    meta.update(
        {
            "one_ended": True,
            "fake_end": fake_end,
            "fake_end_drop_m": drop,
            "shell_s": [round(shell_s[0], 3), round(shell_s[1], 3)],
        }
    )
    return p0, p1, meta


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
    z_profile = str(cfg.get("z_profile") or "linear").lower()
    if z_profile in ("line", "grade", "straight"):
        z_profile = "linear"

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
    p0, p1, one_meta = apply_one_ended_portals(
        cfg, p0, p1, s_g0, s_g1, road, z_terrain
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
    if z_profile == "linear":
        # Chord slopes only — no cubic ease at the door.
        m0 = m_mean
        m1 = m_mean
        ease_m = 0.0
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
        if str(p0.get("method") or "") != "one_ended_fake":
            m0 = _dgm_grade(s_p0)
        if str(p1.get("method") or "") != "one_ended_fake":
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
    if one_meta.get("one_ended"):
        if one_meta.get("fake_end") == "s1":
            cover_in = 0.0
        elif one_meta.get("fake_end") == "s0":
            cover_out = 0.0
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
                if z_profile == "linear":
                    z_herm = z0 + t * (z1 - z0)
                    z_surf = z_herm
                else:
                    z_herm = bb.hermite_z(t, z0, z1, m0, m1, length_p)
                    if approach_mode == "marry" and ease_m > 1e-6 and s <= s_p0 + ease_m + 1e-9:
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
        "one_ended": one_meta.get("one_ended"),
        "fake_end": one_meta.get("fake_end"),
        "fake_end_drop_m": one_meta.get("fake_end_drop_m"),
        "shell_s": one_meta.get("shell_s"),
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


def _vsub(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _vcross(
    u: tuple[float, float, float], v: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (
        u[1] * v[2] - u[2] * v[1],
        u[2] * v[0] - u[0] * v[2],
        u[0] * v[1] - u[1] * v[0],
    )


def _vdot(
    u: tuple[float, float, float], v: tuple[float, float, float]
) -> float:
    return u[0] * v[0] + u[1] * v[1] + u[2] * v[2]


def _quad_toward(
    faces: list,
    verts: list[tuple[float, float, float]],
    a: int,
    b: int,
    c: int,
    d: int,
    toward: tuple[float, float, float],
) -> None:
    """Emit a quad; flip if (b−a)×(c−a) does not point toward ``toward``."""
    va, vb, vc, vd = verts[a], verts[b], verts[c], verts[d]
    nrm = _vcross(_vsub(vb, va), _vsub(vc, va))
    mid = (
        0.25 * (va[0] + vb[0] + vc[0] + vd[0]),
        0.25 * (va[1] + vb[1] + vc[1] + vd[1]),
        0.25 * (va[2] + vb[2] + vc[2] + vd[2]),
    )
    if _vdot(nrm, _vsub(toward, mid)) < 0:
        _quad(faces, a, d, c, b)
    else:
        _quad(faces, a, b, c, d)


def _quad_along(
    faces: list,
    verts: list[tuple[float, float, float]],
    a: int,
    b: int,
    c: int,
    d: int,
    axis: tuple[float, float, float],
) -> None:
    """Emit a quad so (b−a)×(c−a) has a positive dot with ``axis``."""
    nrm = _vcross(_vsub(verts[b], verts[a]), _vsub(verts[c], verts[a]))
    if _vdot(nrm, axis) < 0:
        _quad(faces, a, d, c, b)
    else:
        _quad(faces, a, b, c, d)


def _side_arc_world(
    cx: float,
    cy: float,
    side_xy: tuple[float, float],
    *,
    inner_half: float,
    wall_t: float,
    zr: float,
    zb: float,
    zi: float,
    zo: float,
    radius_m: float,
    nseg: int,
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    """Inner/outer side-wall polylines: circular arc floor→roof, bulge outward.

    The chord stays at ``inner_half`` (same clear box). Radius below half the
    height is raised to a semicircle. Outer copy is shifted horizontally by
    ``wall_t``.
    """
    h = max(0.2, float(zi) - float(zr))
    zi = float(zr) + h
    half_h = 0.5 * h
    r = max(half_h + 1e-4, float(radius_m))
    d = math.sqrt(max(0.0, r * r - half_h * half_h))
    alpha = math.atan2(half_h, d) if d > 1e-9 else 0.5 * math.pi
    nseg = max(4, int(nseg))
    sx, sy = float(side_xy[0]), float(side_xy[1])
    inner: list[tuple[float, float, float]] = []
    outer: list[tuple[float, float, float]] = []
    for k in range(nseg + 1):
        t = k / nseg
        phi = -alpha + t * (2.0 * alpha)
        lat_i = inner_half - d + r * math.cos(phi)
        z_i = zr + half_h + r * math.sin(phi)
        lat_o = lat_i + wall_t
        z_frac = 0.0 if h < 1e-9 else (z_i - zr) / h
        z_o = zb + z_frac * (zo - zb)
        inner.append((cx + lat_i * sx, cy + lat_i * sy, z_i))
        outer.append((cx + lat_o * sx, cy + lat_o * sy, z_o))
    return inner, outer


def _arc_sagitta_m(radius_m: float, height_m: float) -> float:
    h = max(0.2, float(height_m))
    r = max(0.5 * h, float(radius_m))
    half_h = 0.5 * h
    return r - math.sqrt(max(0.0, r * r - half_h * half_h))


def loft_gallery_shell(
    nodes: list[dict],
    *,
    open_side: str,
    wall_t: float,
    overhang_m: float,
    embed_m: float = 0.2,
    open_extra_m: float = 0.8,
    deck_width_extra_m: float = 0.5,
    clear_width_m: float | None = None,
    parapet_height_m: float = 1.0,
    parapet_thickness_m: float = 0.35,
    parapet_out_m: float = 0.25,
    column_spacing_m: float = 4.0,
    column_along_m: float = 0.4,
    column_lat_m: float = 0.35,
    column_out_m: float = 0.15,
    build_parapet: bool = True,
    wall_radius_m: float | None = None,
    wall_arc_segments: int = 10,
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
    closed_tube = side in ("none", "off", "false", "0")
    if closed_tube:
        build_parapet = False
    open_extra = 0.0 if closed_tube else max(0.0, float(open_extra_m))
    deck_w_extra = max(0.0, float(deck_width_extra_m))
    clear_w = float(clear_width_m) if clear_width_m is not None else 0.0
    # Closed tube: roof outer flush with wall outer. Gallery: cover parapet.
    if closed_tube:
        overhang = max(float(wall_t), float(overhang_m) if overhang_m else 0.0)
    else:
        overhang = max(float(overhang_m), open_extra if build_parapet else float(overhang_m))
    parapet_h = max(0.15, float(parapet_height_m))
    parapet_t = max(0.15, float(parapet_thickness_m))
    parapet_out = max(0.0, float(parapet_out_m))
    col_space = max(1.0, float(column_spacing_m))
    col_along = max(0.15, float(column_along_m))
    col_lat = max(0.15, float(column_lat_m))
    col_out = float(column_out_m)
    # Keep roof over parapet outer edge (and any column that still sticks out)
    if not closed_tube:
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
        inner_half = max(half, 0.5 * clear_w) if clear_w > 1e-6 else half
        cx = float(n["x"]) - ox
        cy = float(n["y"]) - oy
        zb = float(n["z_road"]) - oz - embed_m
        zr = float(n["z_road"]) - oz
        zi = float(n["z_roof_inner"]) - oz
        zo = float(n["z_roof_outer"]) - oz
        # Open-side outer edge (deck/parapet)
        lat_l = half + (open_extra if open_left else 0.0)
        lat_r = half + (open_extra if open_right else 0.0)
        # Roof: closed tube flush with wall outer; gallery uses overhang from road
        if closed_tube:
            roof_l = inner_half + wall_t
            roof_r = inner_half + wall_t
        else:
            roof_l = half + (overhang if open_left or build_left else overhang)
            roof_r = half + (overhang if open_right or build_right else overhang)
        ring.append(
            {
                "left": left,
                "right": right,
                "tx": tx,
                "ty": ty,
                "half": half,
                "inner_half": inner_half,
                "s": float(n["s"]),
                "zr": zr,
                "zb": zb,
                "zi": zi,
                "zo": zo,
                "lat_l": lat_l,
                "lat_r": lat_r,
                # closed-wall inner face at lichte Breite; open galleries stay at road
                "li_b": (cx + inner_half * left[0], cy + inner_half * left[1], zb),
                "ri_b": (cx + inner_half * right[0], cy + inner_half * right[1], zb),
                "li_i": (cx + inner_half * left[0], cy + inner_half * left[1], zi),
                "ri_i": (cx + inner_half * right[0], cy + inner_half * right[1], zi),
                "lw_road": (cx + half * left[0], cy + half * left[1], zr),
                "rw_road": (cx + half * right[0], cy + half * right[1], zr),
                "lw_walk": (cx + inner_half * left[0], cy + inner_half * left[1], zr),
                "rw_walk": (cx + inner_half * right[0], cy + inner_half * right[1], zr),
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
                    cx + (inner_half + wall_t) * left[0],
                    cy + (inner_half + wall_t) * left[1],
                    zb,
                ),
                "rw_b": (
                    cx + (inner_half + wall_t) * right[0],
                    cy + (inner_half + wall_t) * right[1],
                    zb,
                ),
                "lw_o": (
                    cx + (inner_half + wall_t) * left[0],
                    cy + (inner_half + wall_t) * left[1],
                    zo,
                ),
                "rw_o": (
                    cx + (inner_half + wall_t) * right[0],
                    cy + (inner_half + wall_t) * right[1],
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

    n_arc = max(4, int(wall_arc_segments or 10))
    radius = float(wall_radius_m or 0.0)
    rounded = bool(closed_tube and radius > 1e-3)
    if rounded:
        for r in ring:
            args = dict(
                inner_half=float(r["inner_half"]),
                wall_t=float(wall_t),
                zr=float(r["zr"]),
                zb=float(r["zb"]),
                zi=float(r["zi"]),
                zo=float(r["zo"]),
                radius_m=radius,
                nseg=n_arc,
            )
            r["r_in"], r["r_out"] = _side_arc_world(
                float(r["cx"]), float(r["cy"]), r["right"], **args
            )
            r["l_in"], r["l_out"] = _side_arc_world(
                float(r["cx"]), float(r["cy"]), r["left"], **args
            )

    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []

    for i in range(len(ring) - 1):
        rc = (
            0.5 * (ring[i]["cx"] + ring[i + 1]["cx"]),
            0.5 * (ring[i]["cy"] + ring[i + 1]["cy"]),
            0.5 * (ring[i]["zr"] + ring[i + 1]["zr"]),
        )
        up = (rc[0], rc[1], rc[2] + 10.0)
        down = (rc[0], rc[1], rc[2] - 10.0)
        out_l = (
            rc[0] + 20.0 * ring[i]["left"][0],
            rc[1] + 20.0 * ring[i]["left"][1],
            rc[2],
        )
        out_r = (
            rc[0] + 20.0 * ring[i]["right"][0],
            rc[1] + 20.0 * ring[i]["right"][1],
            rc[2],
        )
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
        _quad_toward(faces, verts, a, d, c, b, down)

        # Roof topside
        a = _add_vert(verts, ring[i]["lo_o"])
        b = _add_vert(verts, ring[i]["ro_o"])
        c = _add_vert(verts, ring[i + 1]["ro_o"])
        d = _add_vert(verts, ring[i + 1]["lo_o"])
        _quad_toward(faces, verts, a, b, c, d, up)

        # Roof slab edges (overhang lips)
        a = _add_vert(verts, ring[i]["lo_i"])
        b = _add_vert(verts, ring[i]["lo_o"])
        c = _add_vert(verts, ring[i + 1]["lo_o"])
        d = _add_vert(verts, ring[i + 1]["lo_i"])
        _quad_toward(faces, verts, a, b, c, d, out_l)
        a = _add_vert(verts, ring[i]["ro_i"])
        b = _add_vert(verts, ring[i]["ri_i"] if not open_right else ring[i]["ri_open_i"])
        c = _add_vert(
            verts,
            ring[i + 1]["ri_i"] if not open_right else ring[i + 1]["ri_open_i"],
        )
        d = _add_vert(verts, ring[i + 1]["ro_i"])
        _quad_toward(faces, verts, a, b, c, d, rc)
        a = _add_vert(verts, ring[i]["ro_o"])
        b = _add_vert(verts, ring[i]["ro_i"])
        c = _add_vert(verts, ring[i + 1]["ro_i"])
        d = _add_vert(verts, ring[i + 1]["ro_o"])
        _quad_toward(faces, verts, a, d, c, b, out_r)
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
        _quad_toward(faces, verts, a, b, c, d, rc)

        # Closed tube: inner face sits on the bankett (z_road), not embed_m below.
        inner_r0 = ring[i]["rw_walk"] if closed_tube else ring[i]["ri_b"]
        inner_r1 = ring[i + 1]["rw_walk"] if closed_tube else ring[i + 1]["ri_b"]
        inner_l0 = ring[i]["lw_walk"] if closed_tube else ring[i]["li_b"]
        inner_l1 = ring[i + 1]["lw_walk"] if closed_tube else ring[i + 1]["li_b"]

        # Inward = toward the carriageway, horizontal (not a point on the axis).
        in_r = (-ring[i]["right"][0], -ring[i]["right"][1], 0.0)
        in_l = (-ring[i]["left"][0], -ring[i]["left"][1], 0.0)

        if rounded:
            for inn0, out0, inn1, out1, outward in (
                (
                    ring[i]["r_in"],
                    ring[i]["r_out"],
                    ring[i + 1]["r_in"],
                    ring[i + 1]["r_out"],
                    out_r,
                ),
                (
                    ring[i]["l_in"],
                    ring[i]["l_out"],
                    ring[i + 1]["l_in"],
                    ring[i + 1]["l_out"],
                    out_l,
                ),
            ):
                for k in range(n_arc):
                    a = _add_vert(verts, inn0[k])
                    b = _add_vert(verts, inn0[k + 1])
                    c = _add_vert(verts, inn1[k + 1])
                    d = _add_vert(verts, inn1[k])
                    _quad_toward(faces, verts, a, b, c, d, rc)
                    a = _add_vert(verts, out0[k])
                    b = _add_vert(verts, out0[k + 1])
                    c = _add_vert(verts, out1[k + 1])
                    d = _add_vert(verts, out1[k])
                    _quad_toward(faces, verts, a, d, c, b, outward)
        else:
            if build_right:
                a = _add_vert(verts, inner_r0)
                b = _add_vert(verts, ring[i]["ri_i"])
                c = _add_vert(verts, ring[i + 1]["ri_i"])
                d = _add_vert(verts, inner_r1)
                _quad_along(faces, verts, a, b, c, d, in_r)
                a = _add_vert(verts, ring[i]["rw_b"])
                b = _add_vert(verts, ring[i]["rw_o"])
                c = _add_vert(verts, ring[i + 1]["rw_o"])
                d = _add_vert(verts, ring[i + 1]["rw_b"])
                _quad_toward(faces, verts, a, d, c, b, out_r)
                a = _add_vert(verts, ring[i]["ri_i"])
                b = _add_vert(verts, ring[i]["rw_o"])
                c = _add_vert(verts, ring[i + 1]["rw_o"])
                d = _add_vert(verts, ring[i + 1]["ri_i"])
                _quad_toward(faces, verts, a, d, c, b, up)

            if build_left:
                a = _add_vert(verts, inner_l0)
                b = _add_vert(verts, ring[i]["li_i"])
                c = _add_vert(verts, ring[i + 1]["li_i"])
                d = _add_vert(verts, inner_l1)
                _quad_along(faces, verts, a, b, c, d, in_l)
                a = _add_vert(verts, ring[i]["lw_b"])
                b = _add_vert(verts, ring[i]["lw_o"])
                c = _add_vert(verts, ring[i + 1]["lw_o"])
                d = _add_vert(verts, ring[i + 1]["lw_b"])
                _quad_toward(faces, verts, a, b, c, d, out_l)
                a = _add_vert(verts, ring[i]["li_i"])
                b = _add_vert(verts, ring[i]["lw_o"])
                c = _add_vert(verts, ring[i + 1]["lw_o"])
                d = _add_vert(verts, ring[i + 1]["li_i"])
                _quad_toward(faces, verts, a, b, c, d, up)

        # Bankett top = deck top (z_road), both sides. No embed kick:
        # zb is only for burying the outer wall, not a visible inner step.
        if (
            closed_tube
            and float(ring[i]["inner_half"]) > float(ring[i]["half"]) + 1e-3
        ):
            # BeamNG shows the walkway from above when the file normal is −Z.
            a = _add_vert(verts, ring[i]["lw_road"])
            b = _add_vert(verts, ring[i]["lw_walk"])
            c = _add_vert(verts, ring[i + 1]["lw_walk"])
            d = _add_vert(verts, ring[i + 1]["lw_road"])
            _quad_along(faces, verts, a, b, c, d, (0.0, 0.0, -1.0))
            a = _add_vert(verts, ring[i]["rw_road"])
            b = _add_vert(verts, ring[i]["rw_walk"])
            c = _add_vert(verts, ring[i + 1]["rw_walk"])
            d = _add_vert(verts, ring[i + 1]["rw_road"])
            _quad_along(faces, verts, a, b, c, d, (0.0, 0.0, -1.0))

        # --- Open-side parapet (continuous low wall) ---
        if build_parapet and open_left:
            a = _add_vert(verts, ring[i]["lp_in"])
            b = _add_vert(verts, ring[i]["lp_in_t"])
            c = _add_vert(verts, ring[i + 1]["lp_in_t"])
            d = _add_vert(verts, ring[i + 1]["lp_in"])
            _quad_toward(faces, verts, a, d, c, b, rc)
            a = _add_vert(verts, ring[i]["lp_out"])
            b = _add_vert(verts, ring[i]["lp_out_t"])
            c = _add_vert(verts, ring[i + 1]["lp_out_t"])
            d = _add_vert(verts, ring[i + 1]["lp_out"])
            _quad_toward(faces, verts, a, b, c, d, out_l)
            a = _add_vert(verts, ring[i]["lp_in_t"])
            b = _add_vert(verts, ring[i]["lp_out_t"])
            c = _add_vert(verts, ring[i + 1]["lp_out_t"])
            d = _add_vert(verts, ring[i + 1]["lp_in_t"])
            _quad_toward(faces, verts, a, d, c, b, up)
        if build_parapet and open_right:
            a = _add_vert(verts, ring[i]["rp_in"])
            b = _add_vert(verts, ring[i]["rp_in_t"])
            c = _add_vert(verts, ring[i + 1]["rp_in_t"])
            d = _add_vert(verts, ring[i + 1]["rp_in"])
            _quad_toward(faces, verts, a, b, c, d, rc)
            a = _add_vert(verts, ring[i]["rp_out"])
            b = _add_vert(verts, ring[i]["rp_out_t"])
            c = _add_vert(verts, ring[i + 1]["rp_out_t"])
            d = _add_vert(verts, ring[i + 1]["rp_out"])
            _quad_toward(faces, verts, a, d, c, b, out_r)
            a = _add_vert(verts, ring[i]["rp_in_t"])
            b = _add_vert(verts, ring[i]["rp_out_t"])
            c = _add_vert(verts, ring[i + 1]["rp_out_t"])
            d = _add_vert(verts, ring[i + 1]["rp_in_t"])
            _quad_toward(faces, verts, a, b, c, d, up)

    for idx in (0, -1):
        r = ring[idx]
        tx, ty = float(r["tx"]), float(r["ty"])
        # Portal cap faces out of the tube: −tangent at s0, +tangent at s1.
        sign = -1.0 if idx == 0 else 1.0
        toward = (r["cx"] + sign * 10.0 * tx, r["cy"] + sign * 10.0 * ty, r["zr"])
        a = _add_vert(verts, r["li_open_i"] if open_left else r["li_i"])
        b = _add_vert(verts, r["ri_open_i"] if open_right else r["ri_i"])
        c = _add_vert(verts, r["ro_o"])
        d = _add_vert(verts, r["lo_o"])
        _quad_toward(faces, verts, a, b, c, d, toward)
        if rounded:
            for inn, out in ((r["r_in"], r["r_out"]), (r["l_in"], r["l_out"])):
                for k in range(n_arc):
                    a = _add_vert(verts, inn[k])
                    b = _add_vert(verts, inn[k + 1])
                    c = _add_vert(verts, out[k + 1])
                    d = _add_vert(verts, out[k])
                    _quad_toward(faces, verts, a, b, c, d, toward)
        else:
            if build_right:
                inner_bot = r["rw_walk"] if closed_tube else r["ri_b"]
                outer_bot = (
                    (r["rw_o"][0], r["rw_o"][1], r["zr"]) if closed_tube else r["rw_b"]
                )
                a = _add_vert(verts, inner_bot)
                b = _add_vert(verts, r["ri_i"])
                c = _add_vert(verts, r["rw_o"])
                d = _add_vert(verts, outer_bot)
                _quad_toward(faces, verts, a, b, c, d, toward)
            if build_left:
                inner_bot = r["lw_walk"] if closed_tube else r["li_b"]
                outer_bot = (
                    (r["lw_o"][0], r["lw_o"][1], r["zr"]) if closed_tube else r["lw_b"]
                )
                a = _add_vert(verts, inner_bot)
                b = _add_vert(verts, outer_bot)
                c = _add_vert(verts, r["lw_o"])
                d = _add_vert(verts, r["li_i"])
                _quad_toward(faces, verts, a, b, c, d, toward)
        if build_parapet and open_left:
            a = _add_vert(verts, r["lp_in"])
            b = _add_vert(verts, r["lp_out"])
            c = _add_vert(verts, r["lp_out_t"])
            d = _add_vert(verts, r["lp_in_t"])
            _quad_toward(faces, verts, a, b, c, d, toward)
        if build_parapet and open_right:
            a = _add_vert(verts, r["rp_in"])
            b = _add_vert(verts, r["rp_out"])
            c = _add_vert(verts, r["rp_out_t"])
            d = _add_vert(verts, r["rp_in_t"])
            _quad_toward(faces, verts, a, b, c, d, toward)

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
    """Axis-aligned in road frame: along=fwd, lat=left, Z up. z0/z1 absolute world Z.

    Face winding assumes a right-handed (fwd, left, Z). Portal s1 flips ``fwd``
    via into_sign while keeping road-left, so det(fwd,left)<0 — reverse quads.
    """
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
    box_c = (
        sum(p[0] for p in corners_w) / 8.0,
        sum(p[1] for p in corners_w) / 8.0,
        sum(p[2] for p in corners_w) / 8.0,
    )
    # Outward = from box center through the face midpoint.
    quads = (
        (idx[0], idx[1], idx[3], idx[2]),  # -along
        (idx[4], idx[6], idx[7], idx[5]),  # +along
        (idx[0], idx[4], idx[5], idx[1]),  # -lat
        (idx[2], idx[3], idx[7], idx[6]),  # +lat
        (idx[0], idx[2], idx[6], idx[4]),  # bottom
        (idx[1], idx[5], idx[7], idx[3]),  # top
    )
    for a, b, c, d in quads:
        va, vb, vc, vd = verts[a], verts[b], verts[c], verts[d]
        mid = (
            0.25 * (va[0] + vb[0] + vc[0] + vd[0]),
            0.25 * (va[1] + vb[1] + vc[1] + vd[1]),
            0.25 * (va[2] + vb[2] + vc[2] + vd[2]),
        )
        toward = (
            mid[0] + (mid[0] - box_c[0]),
            mid[1] + (mid[1] - box_c[1]),
            mid[2] + (mid[2] - box_c[2]),
        )
        _quad_toward(faces, verts, a, b, c, d, toward)

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
    clear_width_m: float | None = None,
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
    road_half = 0.5 * float(node.get("width") or 7.5)
    inner_half = road_half
    if clear_width_m is not None and float(clear_width_m) > 1e-6:
        inner_half = max(road_half, 0.5 * float(clear_width_m))
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
    # Flush with the inner wall when a bankett exists; otherwise inset so the
    # pier does not cut the asphalt MeshRoad.
    if inner_half > road_half + 1e-3:
        pier_inner = inner_half
    else:
        pier_inner = road_half + clear_x

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
    half = 0.5 * float(node.get("width") or 7.5)
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
    closed_tube = side in ("none", "off", "false", "0")
    s_p0 = s_p1 = None
    cf0 = cf1 = 0.0
    if portal_s is not None and len(portal_s) >= 2:
        s_p0, s_p1 = float(portal_s[0]), float(portal_s[1])
        if s_p1 < s_p0:
            s_p0, s_p1 = s_p1, s_p0
    if z_terrain is not None and s_p0 is not None and not closed_tube:
        n0 = _node_nearest_s(nodes, s_p0)
        n1 = _node_nearest_s(nodes, s_p1)
        w0 = float(n0.get("width") or 7.5)
        w1 = float(n1.get("width") or 7.5)
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
        w = float(n.get("width") or 7.5) + float(width_extra_m) + open_extra
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

        # Closed tube: no DGM crossfall. Portal hillside would tilt the
        # slab ~0.12 and leave a step against the flat bankett on one side.
        if closed_tube:
            cf = 0.0
        elif s_p0 is not None and s_p0 - 1e-6 <= s <= s_p1 + 1e-6:
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
        # BeamNG MeshRoad node Z = driving surface (same as bridges node_z_is_top).
        z_node = z
        mesh_nodes.append(
            [
                round(x, 3),
                round(y, 3),
                round(z_node, 3),
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


def _mat4_axis_angle(axis: tuple[float, float, float], deg: float) -> list[float]:
    """Row-major 4x4 rotate around axis (BeamNG Collada subset uses matrices)."""
    ax, ay, az = axis
    nrm = math.sqrt(ax * ax + ay * ay + az * az) or 1.0
    ax, ay, az = ax / nrm, ay / nrm, az / nrm
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    t = 1.0 - c
    return [
        t * ax * ax + c,
        t * ax * ay - s * az,
        t * ax * az + s * ay,
        0.0,
        t * ay * ax + s * az,
        t * ay * ay + c,
        t * ay * az - s * ax,
        0.0,
        t * az * ax - s * ay,
        t * az * ay + s * ax,
        t * az * az + c,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    ]


def write_collada(
    path: Path,
    verts: list[tuple[float, float, float]],
    faces: list[tuple[int, int, int]],
    *,
    material_name: str = "Concrete",
    mesh_stem: str | None = None,
    uv_scale_m: float = 4.0,
    spin_axis: tuple[float, float, float] | None = None,
    spin_period_s: float | None = None,
    spin_degrees: float = 360.0,
    extra_verts: list[tuple[float, float, float]] | None = None,
    extra_faces: list[tuple[int, int, int]] | None = None,
    extra_material: str | None = None,
    extra_uvs: list[tuple[float, float]] | None = None,
    uvs: list[tuple[float, float]] | None = None,
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
    if uvs and len(uvs) == len(verts):
        uv_vals = " ".join(f"{u:.5f} {v:.5f}" for u, v in uvs)
    else:
        uv_vals = " ".join(f"{x / scale:.5f} {y / scale:.5f}" for x, y, _z in verts)
    p_vals = " ".join(f"{a} {b} {c}" for a, b, c in faces)
    n_tri = len(faces)
    n_vert = len(verts)
    mat = escape(material_name)
    stem = mesh_stem or path.stem
    # LOD name must have a letter before the pixel threshold
    lod_name = f"{stem}_a999"
    has_extra = bool(extra_verts and extra_faces and extra_material)
    spin_libs = ""
    spin_rotate = ""
    if spin_axis is not None and spin_period_s is not None and float(spin_period_s) > 1e-4:
        # BeamNG Collada subset: Matrix4x4 keyframes. 0° and 360° are the same
        # matrix, so intermediate poses are required.
        period = float(spin_period_s)
        n_keys = 12
        times = [period * i / n_keys for i in range(n_keys + 1)]
        mats: list[float] = []
        for i in range(n_keys + 1):
            mats.extend(_mat4_axis_angle(spin_axis, float(spin_degrees) * i / n_keys))
        time_s = " ".join(f"{t:.6f}" for t in times)
        mat_s = " ".join(f"{v:.6f}" for v in mats)
        n_out = len(times)
        interp = " ".join(["LINEAR"] * n_out)
        spin_rotate = (
            "\n            <matrix sid=\"matrix\">"
            "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>"
        )
        spin_libs = f"""
  <library_animations>
    <animation id="spin-matrix" name="ambient">
      <source id="spin-matrix-input">
        <float_array id="spin-matrix-input-array" count="{n_out}">{time_s}</float_array>
        <technique_common>
          <accessor source="#spin-matrix-input-array" count="{n_out}" stride="1">
            <param name="TIME" type="float"/>
          </accessor>
        </technique_common>
      </source>
      <source id="spin-matrix-output">
        <float_array id="spin-matrix-output-array" count="{n_out * 16}">{mat_s}</float_array>
        <technique_common>
          <accessor source="#spin-matrix-output-array" count="{n_out}" stride="16">
            <param name="TRANSFORM" type="float4x4"/>
          </accessor>
        </technique_common>
      </source>
      <source id="spin-matrix-interpolation">
        <Name_array id="spin-matrix-interpolation-array" count="{n_out}">{interp}</Name_array>
        <technique_common>
          <accessor source="#spin-matrix-interpolation-array" count="{n_out}" stride="1">
            <param name="INTERPOLATION" type="Name"/>
          </accessor>
        </technique_common>
      </source>
      <sampler id="spin-matrix-sampler">
        <input semantic="INPUT" source="#spin-matrix-input"/>
        <input semantic="OUTPUT" source="#spin-matrix-output"/>
        <input semantic="INTERPOLATION" source="#spin-matrix-interpolation"/>
      </sampler>
      <channel source="#spin-matrix-sampler" target="{lod_name}/matrix"/>
    </animation>
  </library_animations>
  <library_animation_clips>
    <animation_clip id="ambient" name="ambient" start="0" end="{period:.6f}">
      <instance_animation url="#spin-matrix"/>
    </animation_clip>
  </library_animation_clips>"""
    extra_effect = extra_mat_lib = extra_geom = extra_node = ""
    if has_extra:
        emat = escape(extra_material)
        elod = f"{stem}_disc_a999"
        epos = " ".join(f"{x:.4f} {y:.4f} {z:.4f}" for x, y, z in extra_verts)
        if extra_uvs and len(extra_uvs) == len(extra_verts):
            euv = " ".join(f"{u:.5f} {v:.5f}" for u, v in extra_uvs)
        else:
            euv = " ".join(f"{x / scale:.5f} {y / scale:.5f}" for x, y, _z in extra_verts)
        ep = " ".join(f"{a} {b} {c}" for a, b, c in extra_faces)
        extra_effect = f"""
    <effect id="{emat}-effect">
      <profile_COMMON>
        <technique sid="common">
          <lambert>
            <diffuse><color>0.45 0.46 0.48 1</color></diffuse>
          </lambert>
        </technique>
      </profile_COMMON>
    </effect>"""
        extra_mat_lib = f"""
    <material id="{emat}-material" name="{emat}">
      <instance_effect url="#{emat}-effect"/>
    </material>"""
        extra_geom = f"""
    <geometry id="{elod}-mesh" name="{elod}-mesh">
      <mesh>
        <source id="{elod}-mesh-positions">
          <float_array id="{elod}-mesh-positions-array" count="{len(extra_verts) * 3}">{epos}</float_array>
          <technique_common>
            <accessor source="#{elod}-mesh-positions-array" count="{len(extra_verts)}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{elod}-mesh-map-0">
          <float_array id="{elod}-mesh-map-0-array" count="{len(extra_verts) * 2}">{euv}</float_array>
          <technique_common>
            <accessor source="#{elod}-mesh-map-0-array" count="{len(extra_verts)}" stride="2">
              <param name="S" type="float"/>
              <param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{elod}-mesh-vertices">
          <input semantic="POSITION" source="#{elod}-mesh-positions"/>
        </vertices>
        <triangles material="{emat}" count="{len(extra_faces)}">
          <input semantic="VERTEX" source="#{elod}-mesh-vertices" offset="0"/>
          <input semantic="TEXCOORD" source="#{elod}-mesh-map-0" offset="0" set="0"/>
          <p>{ep}</p>
        </triangles>
      </mesh>
    </geometry>"""
        extra_node = f"""
            <instance_geometry url="#{elod}-mesh">
              <bind_material>
                <technique_common>
                  <instance_material symbol="{emat}" target="#{emat}-material">
                    <bind_vertex_input semantic="UVSET0" input_semantic="TEXCOORD" input_set="0"/>
                  </instance_material>
                </technique_common>
              </bind_material>
            </instance_geometry>"""
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
    </effect>{extra_effect}
  </library_effects>
  <library_materials>
    <material id="{mat}-material" name="{mat}">
      <instance_effect url="#{mat}-effect"/>
    </material>{extra_mat_lib}
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
    </geometry>{extra_geom}
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
{spin_libs}
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="base00" name="base00" type="NODE">
        <node id="start01" name="start01" type="NODE">
          <node id="{lod_name}" name="{lod_name}" type="NODE">{spin_rotate}
            <instance_geometry url="#{lod_name}-mesh">
              <bind_material>
                <technique_common>
                  <instance_material symbol="{mat}" target="#{mat}-material">
                    <bind_vertex_input semantic="UVSET0" input_semantic="TEXCOORD" input_set="0"/>
                  </instance_material>
                </technique_common>
              </bind_material>
            </instance_geometry>{extra_node}
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


def ensure_gallery_material(
    user_level: Path,
    level_name: str,
    mat_name: str = "Concrete",
    *,
    fan_rpm: float = 60.0,
) -> None:
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
    elif mat_name in ("TunnelLamp", "tunnel_lamp"):
        data[mat_name] = {
            "name": mat_name,
            "mapTo": mat_name,
            "class": "Material",
            "persistentId": "ga11e7c0-c0ac-4e1e-9c11-0000ga11e703",
            "Stages": [
                {
                    "baseColorFactor": [1.0, 0.94, 0.78, 1.0],
                    "emissiveFactor": [6.0, 5.4, 4.0],
                    "roughnessFactor": 0.35,
                    "metallicFactor": 0.05,
                },
                {},
                {},
                {},
            ],
            "annotation": "BUILDING",
            "materialTag0": "beamng",
            "doubleSided": True,
            "version": 1.5,
        }
    elif mat_name in ("TunnelFanRotorCW", "TunnelFanRotorCCW"):
        # Rotate Animation = bit 0x2. PBR 1.5 clips via opacityMap, not baseColor alpha.
        deg_s = max(0.0, float(fan_rpm)) * 6.0
        if "CCW" in mat_name:
            deg_s = -deg_s
        tex = f"/levels/{level_name}/art/shapes/galleries/tunnel_fan_rotor.png"
        opac = f"/levels/{level_name}/art/shapes/galleries/{TUNNEL_FAN_ROTOR_OPACITY_PNG}"
        pid = (
            "ga11e7c0-c0ac-4e1e-9c11-0000ga11e705"
            if "CCW" not in mat_name
            else "ga11e7c0-c0ac-4e1e-9c11-0000ga11e706"
        )
        data[mat_name] = {
            "name": mat_name,
            "mapTo": mat_name,
            "class": "Material",
            "persistentId": pid,
            "Stages": [
                {
                    "baseColorMap": tex,
                    "opacityMap": opac,
                    "baseColorFactor": [0.92, 0.93, 0.95, 1.0],
                    "roughnessFactor": 0.38,
                    "metallicFactor": 0.55,
                    # Layer fields like official water: integer flags, Point2F in the stage.
                    # rotate=2, wave=4 — Wave on cancels the visible spin.
                    "animFlags": 2,
                    "rotSpeed": deg_s,
                    "rotPivotOffset": [-0.5, -0.5],
                    "waveAmp": 0.0,
                    "waveFreq": 0.0,
                },
                {},
                {},
                {},
            ],
            "alphaTest": True,
            "alphaRef": 115,
            "translucentBlendOp": "None",
            "translucentZWrite": True,
            "doubleSided": True,
            "invertBackFaceNormals": True,
            "castShadows": False,
            "annotation": "BUILDING",
            "materialTag0": "beamng",
            "version": 1.5,
        }
    elif mat_name in ("TunnelFan", "tunnel_fan", "TunnelJetFan"):
        data[mat_name] = {
            "name": mat_name,
            "mapTo": mat_name,
            "class": "Material",
            "persistentId": "ga11e7c0-c0ac-4e1e-9c11-0000ga11e704",
            "Stages": [
                {
                    "baseColorFactor": [0.16, 0.17, 0.19, 1.0],
                    "roughnessFactor": 0.42,
                    "metallicFactor": 0.75,
                },
                {},
                {},
                {},
            ],
            "annotation": "BUILDING",
            "materialTag0": "beamng",
            "doubleSided": True,
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
            "doubleSided": True,
            "version": 1.5,
        }
    mats_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


TUNNEL_LAMP_DAE = "tunnel_lamp.dae"
TUNNEL_FAN_DAE = "tunnel_jetfan.dae"
TUNNEL_FAN_ROTOR_CW_DAE = "tunnel_jetfan_rotor_cw.dae"
TUNNEL_FAN_ROTOR_CCW_DAE = "tunnel_jetfan_rotor_ccw.dae"
TUNNEL_FAN_ROTOR_PNG = "tunnel_fan_rotor.png"
TUNNEL_FAN_ROTOR_OPACITY_PNG = "tunnel_fan_rotor_o.data.png"
_LANE_WIDTH_M = 3.75


def _rot_matrix_along_xy(tx: float, ty: float) -> list[float]:
    """X = plan tangent, Z = world up, Y = Z×X (left of driving direction)."""
    horiz = math.hypot(tx, ty) or 1.0
    xx, xy, xz = tx / horiz, ty / horiz, 0.0
    yx, yy, yz = -xy, xx, 0.0
    yn = math.hypot(yx, yy, yz) or 1.0
    yx, yy, yz = yx / yn, yy / yn, yz / yn
    return [xx, xy, xz, yx, yy, yz, 0.0, 0.0, 1.0]


def _gallery_is_closed_tube(feat: dict, cfg: dict) -> bool:
    if str(feat.get("kind") or "").lower() == "tunnel":
        return True
    side = str(cfg.get("open_side") or "").lower().strip()
    shell = str((cfg.get("style") or {}).get("shell") or "").lower()
    return side in ("none", "off", "false", "0") or shell == "tunnel"


def _fitout_wanted(feat: dict, cfg: dict, bore_m: float) -> bool:
    """Ceiling lamps/fans: closed tubes only; galleries stay empty."""
    if not _gallery_is_closed_tube(feat, cfg):
        return False
    raw = cfg.get("fitout", "auto")
    if raw is False or str(raw).lower() in ("false", "off", "0", "none", "no"):
        return False
    if raw is True or str(raw).lower() in ("true", "on", "1", "yes"):
        return bore_m >= 20.0
    return bore_m >= float(cfg.get("fitout_min_m") or 100.0)


def _station_s_list(s0: float, s1: float, spacing: float, inset: float) -> list[float]:
    """Evenly spaced stations, inset from both portals, at least one if room."""
    lo = float(s0) + max(0.0, float(inset))
    hi = float(s1) - max(0.0, float(inset))
    if hi <= lo + 0.5:
        return []
    sp = max(2.0, float(spacing))
    n = max(1, int(math.floor((hi - lo) / sp)) + 1)
    span = (n - 1) * sp
    start = lo + 0.5 * max(0.0, (hi - lo) - span)
    return [start + i * sp for i in range(n)]


def _sample_gallery_node(nodes: list[dict], s: float) -> dict | None:
    if not nodes:
        return None
    by_s = sorted(nodes, key=lambda n: float(n["s"]))
    s = float(s)
    if s <= float(by_s[0]["s"]):
        n = by_s[0]
        return dict(n)
    if s >= float(by_s[-1]["s"]):
        return dict(by_s[-1])
    for a, b in zip(by_s, by_s[1:]):
        sa, sb = float(a["s"]), float(b["s"])
        if sa <= s <= sb:
            t = 0.0 if abs(sb - sa) < 1e-9 else (s - sa) / (sb - sa)

            def mix(key: str) -> float:
                return float(a[key]) + t * (float(b[key]) - float(a[key]))

            tx, ty = mix("tx"), mix("ty")
            nrm = math.hypot(tx, ty) or 1.0
            return {
                "x": mix("x"),
                "y": mix("y"),
                "z_road": mix("z_road"),
                "z_roof_inner": mix("z_roof_inner"),
                "width": mix("width"),
                "tx": tx / nrm,
                "ty": ty / nrm,
                "s": s,
            }
    return dict(by_s[-1])


def make_tunnel_lamp_mesh() -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Box luminaire: local X along tube, origin at the roof contact, hangs −Z."""
    hx, hy, hz = 0.40, 0.11, 0.10
    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    specs = (
        ((-hx, -hy, 0.0), (hx, -hy, 0.0), (hx, hy, 0.0), (-hx, hy, 0.0), (0.0, 0.0, 1.0)),
        ((-hx, -hy, -hz), (-hx, hy, -hz), (hx, hy, -hz), (hx, -hy, -hz), (0.0, 0.0, -1.0)),
        ((-hx, -hy, 0.0), (-hx, -hy, -hz), (hx, -hy, -hz), (hx, -hy, 0.0), (0.0, -1.0, 0.0)),
        ((-hx, hy, 0.0), (hx, hy, 0.0), (hx, hy, -hz), (-hx, hy, -hz), (0.0, 1.0, 0.0)),
        ((hx, -hy, 0.0), (hx, -hy, -hz), (hx, hy, -hz), (hx, hy, 0.0), (1.0, 0.0, 0.0)),
        ((-hx, -hy, 0.0), (-hx, hy, 0.0), (-hx, hy, -hz), (-hx, -hy, -hz), (-1.0, 0.0, 0.0)),
    )
    for a, b, c, d, toward in specs:
        ia = _add_vert(verts, a)
        ib = _add_vert(verts, b)
        ic = _add_vert(verts, c)
        id_ = _add_vert(verts, d)
        # Unique verts per face so the box keeps hard edges.
        _quad_toward(
            faces,
            verts,
            ia,
            ib,
            ic,
            id_,
            (0.25 * (a[0] + b[0] + c[0] + d[0]) + toward[0],
             0.25 * (a[1] + b[1] + c[1] + d[1]) + toward[1],
             0.25 * (a[2] + b[2] + c[2] + d[2]) + toward[2]),
        )
    return verts, faces


def make_tunnel_jetfan_mesh(
    *,
    length_m: float = 1.5,
    diameter_m: float = 0.6,
    wall_m: float = 0.06,
    nseg: int = 20,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Double-walled cylinder along local X (open ends, separate rim verts)."""
    r_out = 0.5 * max(0.2, float(diameter_m))
    r_in = max(0.08, r_out - max(0.02, float(wall_m)))
    x0, x1 = -0.5 * float(length_m), 0.5 * float(length_m)
    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    nseg = max(8, int(nseg))

    def ring(radius: float, x: float) -> list[int]:
        ids: list[int] = []
        for i in range(nseg):
            ang = 2.0 * math.pi * i / nseg
            ids.append(
                _add_vert(
                    verts,
                    (x, radius * math.cos(ang), radius * math.sin(ang)),
                )
            )
        return ids

    o0, o1 = ring(r_out, x0), ring(r_out, x1)
    for i in range(nseg):
        j = (i + 1) % nseg
        mid_y = 0.5 * (verts[o0[i]][1] + verts[o0[j]][1])
        mid_z = 0.5 * (verts[o0[i]][2] + verts[o0[j]][2])
        _quad_toward(faces, verts, o0[i], o1[i], o1[j], o0[j], (0.0, mid_y * 4.0, mid_z * 4.0))

    i0, i1 = ring(r_in, x0), ring(r_in, x1)
    for i in range(nseg):
        j = (i + 1) % nseg
        _quad_toward(faces, verts, i0[i], i0[j], i1[j], i1[i], (0.0, 0.0, 0.0))

    for x, toward_x in ((x0, x0 - 1.0), (x1, x1 + 1.0)):
        outer, inner = ring(r_out, x), ring(r_in, x)
        toward = (toward_x, 0.0, 0.0)
        for i in range(nseg):
            j = (i + 1) % nseg
            _quad_toward(faces, verts, outer[i], outer[j], inner[j], inner[i], toward)
    return verts, faces


def write_tunnel_fan_rotor_png(path: Path, n_blades: int = 6, size: int = 256) -> None:
    """Impeller disc: 6 blades + hub, transparent between blades."""
    n_blades = max(3, min(9, int(n_blades)))
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    cx = cy = size * 0.5
    r_out = size * 0.48
    r_hub = size * 0.13
    half = 11.0
    for i in range(n_blades):
        mid = i * 360.0 / n_blades
        dr.pieslice(
            [cx - r_out, cy - r_out, cx + r_out, cy + r_out],
            mid - half,
            mid + half,
            fill=(72, 76, 82, 255),
        )
    dr.ellipse(
        [cx - r_hub, cy - r_hub, cx + r_hub, cy + r_hub],
        fill=(38, 40, 44, 255),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)
    # PBR 1.5 alpha-test reads opacityMap, not the color PNG alpha.
    alpha = im.getchannel("A")
    Image.merge("RGB", (alpha, alpha, alpha)).save(
        path.parent / TUNNEL_FAN_ROTOR_OPACITY_PNG
    )


def make_jetfan_rotor_discs(
    *,
    diameter_m: float,
    wall_m: float,
    nseg: int = 20,
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]], list[tuple[float, float]]]:
    """One impeller plane in the housing middle (material is double-sided)."""
    r_out = 0.5 * max(0.2, float(diameter_m))
    r_in = max(0.08, r_out - max(0.02, float(wall_m)))
    r = max(0.08, r_in - 0.02)
    nseg = max(8, int(nseg))
    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    uvs: list[tuple[float, float]] = []

    def add_face(x: float, toward_x: float) -> None:
        toward = (toward_x, 0.0, 0.0)
        ring_yz: list[tuple[float, float]] = []
        for i in range(nseg):
            ang = 2.0 * math.pi * i / nseg
            ring_yz.append((r * math.cos(ang), r * math.sin(ang)))
        for i in range(nseg):
            j = (i + 1) % nseg
            y0, z0 = ring_yz[i]
            y1, z1 = ring_yz[j]
            pts = ((x, 0.0, 0.0), (x, y0, z0), (x, y1, z1))
            uv_tri = (
                (0.5, 0.5),
                (0.5 + 0.5 * y0 / r, 0.5 + 0.5 * z0 / r),
                (0.5 + 0.5 * y1 / r, 0.5 + 0.5 * z1 / r),
            )
            ia = _add_vert(verts, pts[0])
            ib = _add_vert(verts, pts[1])
            ic = _add_vert(verts, pts[2])
            uvs.extend(uv_tri)
            va, vb, vc = verts[ia], verts[ib], verts[ic]
            nrm = _vcross(_vsub(vb, va), _vsub(vc, va))
            mid = (
                (va[0] + vb[0] + vc[0]) / 3.0,
                (va[1] + vb[1] + vc[1]) / 3.0,
                (va[2] + vb[2] + vc[2]) / 3.0,
            )
            if _vdot(nrm, _vsub(toward, mid)) < 0:
                faces.append((ia, ic, ib))
                uvs[-1], uvs[-2] = uvs[-2], uvs[-1]
            else:
                faces.append((ia, ib, ic))

    add_face(0.0, 1.0)
    return verts, faces, uvs


def write_tunnel_fitout_shapes(
    *,
    proc_dir: Path,
    user_level: Path,
    level_name: str,
    diameter_m: float,
    length_m: float,
    wall_m: float,
    fan_rpm: float = 60.0,
    fan_blades: int = 6,
) -> tuple[str, str, str | None, str | None]:
    """Housing is static. Rotor DAEs carry the ambient matrix clip. Return VFS paths."""
    lamp_v, lamp_f = make_tunnel_lamp_mesh()
    fan_v, fan_f = make_tunnel_jetfan_mesh(
        length_m=length_m, diameter_m=diameter_m, wall_m=wall_m
    )
    disc_v, disc_f, disc_uv = make_jetfan_rotor_discs(
        diameter_m=diameter_m, wall_m=wall_m
    )
    dests = [proc_dir]
    if user_level.is_dir():
        dests.append(user_level / "art" / "shapes" / "galleries")
        ensure_gallery_material(user_level, level_name, "TunnelLamp")
        ensure_gallery_material(user_level, level_name, "TunnelFan")
        ensure_gallery_material(
            user_level, level_name, "TunnelFanRotorCW", fan_rpm=fan_rpm
        )
        ensure_gallery_material(
            user_level, level_name, "TunnelFanRotorCCW", fan_rpm=fan_rpm
        )
    for dest in dests:
        dest.mkdir(parents=True, exist_ok=True)
        write_tunnel_fan_rotor_png(dest / TUNNEL_FAN_ROTOR_PNG, n_blades=fan_blades)
        write_collada(
            dest / TUNNEL_LAMP_DAE,
            lamp_v,
            lamp_f,
            material_name="TunnelLamp",
            mesh_stem=Path(TUNNEL_LAMP_DAE).stem,
            uv_scale_m=1.0,
        )
        write_collada(
            dest / TUNNEL_FAN_DAE,
            fan_v,
            fan_f,
            material_name="TunnelFan",
            mesh_stem=Path(TUNNEL_FAN_DAE).stem,
            uv_scale_m=1.0,
        )
        for dae, disc_mat in (
            (TUNNEL_FAN_ROTOR_CW_DAE, "TunnelFanRotorCW"),
            (TUNNEL_FAN_ROTOR_CCW_DAE, "TunnelFanRotorCCW"),
        ):
            write_collada(
                dest / dae,
                disc_v,
                disc_f,
                material_name=disc_mat,
                mesh_stem=Path(dae).stem,
                uv_scale_m=1.0,
                uvs=disc_uv,
            )
        leftover = dest / "tunnel_jetfan_ccw.dae"
        if leftover.is_file():
            leftover.unlink()
    vfs = f"/levels/{level_name}/art/shapes/galleries"
    rotor_cw = f"{vfs}/{TUNNEL_FAN_ROTOR_CW_DAE}"
    rotor_ccw = f"{vfs}/{TUNNEL_FAN_ROTOR_CCW_DAE}"
    return f"{vfs}/{TUNNEL_LAMP_DAE}", f"{vfs}/{TUNNEL_FAN_DAE}", rotor_cw, rotor_ccw


def _fitout_tsstatic(
    name: str,
    pos: tuple[float, float, float],
    rot: list[float],
    shape_vfs: str,
    *,
    play_ambient: bool = False,
    dynamic: bool = False,
) -> dict:
    row = {
        "name": name,
        "class": "TSStatic",
        "__parent": "galleries",
        "position": [round(pos[0], 3), round(pos[1], 3), round(pos[2], 3)],
        "rotationMatrix": [round(v, 6) for v in rot],
        "scale": [1.0, 1.0, 1.0],
        "shapeName": shape_vfs,
        "collisionType": "None",
        "decalType": "None",
        "castShadows": False,
        "useInstanceRenderData": not (play_ambient or dynamic),
        "isRenderEnabled": True,
    }
    if play_ambient:
        row["playAmbient"] = True
    if dynamic:
        row["dynamic"] = True
    return row


def _fitout_pointlight(
    name: str,
    pos: tuple[float, float, float],
    *,
    brightness: float,
    range_m: float,
) -> dict:
    return {
        "name": name,
        "class": "PointLight",
        "__parent": "galleries",
        "position": [round(pos[0], 3), round(pos[1], 3), round(pos[2], 3)],
        "rotationMatrix": [1, 0, 0, 0, 1, 0, 0, 0, 1],
        "color": [1.0, 0.92, 0.75, 1.0],
        "brightness": float(brightness),
        "range": float(range_m),
        "castShadows": False,
        "isEnabled": True,
    }


def build_tunnel_fitout_entries(
    *,
    nodes: list[dict],
    s_p0: float,
    s_p1: float,
    cfg: dict,
    slug: str,
    oid,
    lamp_vfs: str,
    fan_vfs: str,
    rotor_cw_vfs: str | None = None,
    rotor_ccw_vfs: str | None = None,
) -> tuple[list[dict], dict]:
    """Place midline lamps and per-lane jet fans inside the bore (not the approach)."""
    entries: list[dict] = []
    stats = {"lamps": 0, "lights": 0, "fans": 0, "rotors": 0}
    lamp_ss = _station_s_list(
        s_p0,
        s_p1,
        float(cfg.get("lamp_spacing_m") or 12.0),
        float(cfg.get("lamp_inset_m") or 10.0),
    )
    fan_ss = _station_s_list(
        s_p0,
        s_p1,
        float(cfg.get("fan_spacing_m") or 50.0),
        float(cfg.get("fan_inset_m") or 18.0),
    )
    width = float(cfg.get("width_m") or (nodes[0].get("width") if nodes else 7.5) or 7.5)
    n_lanes = max(1, min(3, int(round(width / _LANE_WIDTH_M))))
    lane_w = width / n_lanes
    lane_offsets = [((i - 0.5 * (n_lanes - 1)) * lane_w) for i in range(n_lanes)]
    diam = float(cfg.get("fan_diameter_m") or 0.60)
    fan_drop = (
        float(cfg["fan_drop_m"])
        if cfg.get("fan_drop_m") is not None
        else 0.5 * diam + 0.02
    )
    lamp_drop = float(cfg.get("lamp_drop_m") or 0.12)

    for i, s in enumerate(lamp_ss):
        if n_lanes == 1 and any(abs(s - fs) < 2.0 for fs in fan_ss):
            continue
        n = _sample_gallery_node(nodes, s)
        if n is None:
            continue
        rot = _rot_matrix_along_xy(float(n["tx"]), float(n["ty"]))
        z = float(n["z_roof_inner"]) - lamp_drop
        pos = (float(n["x"]), float(n["y"]), z)
        entries.append(
            _fitout_tsstatic(f"gallery_{slug}_{oid}__lamp_{i:03d}", pos, rot, lamp_vfs)
        )
        stats["lamps"] += 1
        if cfg.get("lamp_light", True) and float(cfg.get("lamp_brightness") or 0) > 1e-6:
            light_pos = (pos[0], pos[1], pos[2] - 0.08)
            entries.append(
                _fitout_pointlight(
                    f"gallery_{slug}_{oid}__light_{i:03d}",
                    light_pos,
                    brightness=float(cfg.get("lamp_brightness") or 1.6),
                    range_m=float(cfg.get("lamp_range_m") or 20.0),
                )
            )
            stats["lights"] += 1

    for i, s in enumerate(fan_ss):
        n = _sample_gallery_node(nodes, s)
        if n is None:
            continue
        rot = _rot_matrix_along_xy(float(n["tx"]), float(n["ty"]))
        _left, right = bb.left_right_unit(float(n["tx"]), float(n["ty"]))
        z = float(n["z_roof_inner"]) - fan_drop
        for k, off in enumerate(lane_offsets):
            # off > 0 = right of driving direction
            lx = float(n["x"]) + off * right[0]
            ly = float(n["y"]) + off * right[1]
            pos = (lx, ly, z)
            side = "c" if abs(off) < 0.05 else ("r" if off > 0 else "l")
            entries.append(
                _fitout_tsstatic(
                    f"gallery_{slug}_{oid}__fan_{i:03d}_{side}{k}",
                    pos,
                    rot,
                    fan_vfs,
                )
            )
            stats["fans"] += 1
            if rotor_cw_vfs and rotor_ccw_vfs:
                rotor_vfs = rotor_cw_vfs if (k % 2 == 0) else rotor_ccw_vfs
                entries.append(
                    _fitout_tsstatic(
                        f"gallery_{slug}_{oid}__fanrotor_{i:03d}_{side}{k}",
                        pos,
                        rot,
                        rotor_vfs,
                        dynamic=True,
                    )
                )
                stats["rotors"] += 1
    return entries, stats


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
) -> tuple[float, float, float, float] | None:
    """Nearest point on polyline → (dist_m, z, half_width, s).

    z_key:
      hermite — ``z_hermite`` when present (portal carve / hole compare)
      road — ``z_road`` (MeshRoad surface; approach conform)
      roof — ``z_roof_outer`` (top of tube, portal lintel)
    """
    if len(nodes) < 1:
        return None

    def _z_of(n: dict) -> float:
        if z_key == "road":
            return float(n["z_road"])
        if z_key == "roof":
            if n.get("z_roof_outer") is not None:
                return float(n["z_roof_outer"])
            return float(n["z_road"])
        if z_key == "roof_inner":
            if n.get("z_roof_inner") is not None:
                return float(n["z_roof_inner"])
            return float(n["z_road"])
        if z_key == "roof_inner":
            if n.get("z_roof_inner") is not None:
                return float(n["z_roof_inner"])
            return float(n["z_road"])
        if z_key == "roof_inner":
            if n.get("z_roof_inner") is not None:
                return float(n["z_roof_inner"])
            return float(n["z_road"])
        if n.get("z_hermite") is not None:
            return float(n["z_hermite"])
        return float(n["z_road"])

    best: tuple[float, float, float, float] | None = None
    for i, n in enumerate(nodes):
        x0, y0 = float(n["x"]), float(n["y"])
        z0 = _z_of(n)
        w0 = 0.5 * float(n.get("width") or 7.5)
        s0 = float(n["s"]) if n.get("s") is not None else float(i)
        if i == len(nodes) - 1:
            d = math.hypot(bx - x0, by - y0)
            cand = (d, z0, w0, s0)
        else:
            n1 = nodes[i + 1]
            x1, y1 = float(n1["x"]), float(n1["y"])
            z1 = _z_of(n1)
            w1 = 0.5 * float(n1.get("width") or 7.5)
            s1 = float(n1["s"]) if n1.get("s") is not None else float(i + 1)
            dx, dy = x1 - x0, y1 - y0
            seg2 = dx * dx + dy * dy
            if seg2 < 1e-12:
                t = 0.0
            else:
                t = max(0.0, min(1.0, ((bx - x0) * dx + (by - y0) * dy) / seg2))
            px = x0 + t * dx
            py = y0 + t * dy
            d = math.hypot(bx - px, by - py)
            cand = (d, z0 + t * (z1 - z0), w0 + t * (w1 - w0), s0 + t * (s1 - s0))
        if best is None or cand[0] < best[0]:
            best = cand
    return best


def _daylight_deck_nodes(g: dict, info: dict) -> list[dict]:
    """Outer MeshRoad plate (portal → abutment), never the buried bore."""
    nodes = g.get("nodes") or info.get("nodes") or []
    if len(nodes) < 2:
        return []
    portal_s = info.get("portal_s") or g.get("portal_s") or []
    if len(portal_s) < 2:
        return []
    s0, s1 = float(portal_s[0]), float(portal_s[1])
    if s1 < s0:
        s0, s1 = s1, s0
    fake = info.get("fake_end") or g.get("fake_end")
    one = bool(
        info.get("one_ended")
        if info.get("one_ended") is not None
        else g.get("one_ended")
    )
    abut_end = info.get("abutment_end_s")
    if not one or fake not in ("s0", "s1"):
        return []
    if fake == "s0":
        s_lo = s1
        s_hi = (
            float(abut_end)
            if abut_end is not None
            else max(float(n["s"]) for n in nodes)
        )
    else:
        s_hi = s0
        s_lo = (
            float(abut_end)
            if abut_end is not None
            else min(float(n["s"]) for n in nodes)
        )
    if s_hi < s_lo:
        s_lo, s_hi = s_hi, s_lo
    out = [n for n in nodes if s_lo - 1e-6 <= float(n["s"]) <= s_hi + 1e-6]
    return out if len(out) >= 2 else []


def _gallery_bridge_conform_cfg(g: dict, defaults: dict, info: dict) -> dict:
    """Same knobs ``conform_bridge_heightmap`` reads on a bridge item."""

    def _num(key: str, fallback):
        if g.get(key) is not None:
            return g[key]
        if defaults.get(key) is not None:
            return defaults[key]
        return fallback

    sink = float(_num("approach_conform_sink_m", 0.02))
    force_sink = g.get("force_deck_z_sink_m")
    if force_sink is None:
        force_sink = defaults.get("force_deck_z_sink_m")
    if force_sink is None:
        force_sink = sink
    return {
        "approach_conform": True,
        "force_deck_z": bool(
            g.get("force_deck_z")
            if g.get("force_deck_z") is not None
            else defaults.get("force_deck_z", True)
        ),
        "force_deck_z_sink_m": float(force_sink),
        "force_deck_z_fill": bool(
            g.get("force_deck_z_fill")
            if g.get("force_deck_z_fill") is not None
            else defaults.get("force_deck_z_fill", True)
        ),
        "approach_conform_pad_m": float(_num("approach_conform_pad_m", 0.5)),
        "approach_conform_falloff_m": float(_num("approach_conform_falloff_m", 1.5)),
        "approach_conform_sink_m": sink,
        "approach_conform_max_delta_m": float(_num("approach_conform_max_delta_m", 1.0)),
        "approach_conform_max_raise_m": _num("approach_conform_max_raise_m", 1.0),
        "approach_conform_max_cut_m": _num("approach_conform_max_cut_m", 12.0),
        "approach_conform_range_m": _num("approach_conform_range_m", 2.0),
        "approach_conform_pull_m": _num("approach_conform_pull_m", 8.0),
        "approach_conform_side_cut": bool(
            g.get("approach_conform_side_cut")
            if g.get("approach_conform_side_cut") is not None
            else defaults.get("approach_conform_side_cut", True)
        ),
        "approach_conform_side_range_m": _num("approach_conform_side_range_m", None),
        "approach_conform_side_drop_m": _num("approach_conform_side_drop_m", None),
        "side_cut_preserve": (
            g.get("side_cut_preserve")
            if isinstance(g.get("side_cut_preserve"), dict)
            else defaults.get("side_cut_preserve")
        ),
        "approach_conform_side_cut_preserve": (
            g.get("approach_conform_side_cut_preserve")
            if isinstance(g.get("approach_conform_side_cut_preserve"), dict)
            else defaults.get("approach_conform_side_cut_preserve")
        ),
        "extend_before_m": 0.0,
        "extend_after_m": 0.0,
        "depth_m": float(
            g.get("deck_depth_m")
            if g.get("deck_depth_m") is not None
            else defaults.get("deck_depth_m") or 0.5
        ),
    }


def _apply_bridge_conform_weights(
    elev: np.ndarray, z_tgt: np.ndarray, weight: np.ndarray
) -> int:
    sel = weight > 1e-6
    if not np.any(sel):
        return 0
    w = np.clip(weight[sel], 0.0, 1.0)
    z0 = elev[sel]
    z_new = (1.0 - w) * z0 + w * z_tgt[sel]
    elev[sel] = z_new
    return int(np.count_nonzero(np.abs(z_new - z0) >= 1e-4))


def conform_approach_heightmap(
    galleries: list[dict],
    span_infos: list[dict],
    *,
    size: int,
    extent: float,
    defaults: dict,
    elev: np.ndarray,
) -> tuple[np.ndarray, dict]:
    """Bake heightmap to MeshRoad on portal approaches / outer tunnel decks.

    One-ended plates (portal → abutment) use the bridge deck-cut: high DGM
    under the slab is lowered to the driving surface. Other galleries keep
    the short approach ribbon. The buried bore is never touched.
    """
    out = elev.astype(np.float64).copy()
    info_by_oid = {info.get("objectid"): info for info in span_infos}
    tested = 0
    changed = 0
    max_abs = 0.0
    bridge_stats = None

    plate_oids: set = set()
    br_entries: list[dict] = []
    br_infos: list[dict] = []
    br_cfgs: list[dict] = []
    for g in galleries:
        cfg_conform = g.get("approach_conform")
        if cfg_conform is None:
            cfg_conform = defaults.get("approach_conform", True)
        if not cfg_conform:
            continue
        info = info_by_oid.get(g.get("objectid")) or {}
        plate = _daylight_deck_nodes(g, info)
        if len(plate) < 2:
            continue
        depth = float(
            g.get("deck_depth_m")
            if g.get("deck_depth_m") is not None
            else defaults.get("deck_depth_m") or 0.5
        )
        nodes_mr = []
        for n in plate:
            nodes_mr.append(
                [
                    float(n["x"]),
                    float(n["y"]),
                    float(n["z_road"]),
                    float(n.get("width") or 7.5),
                    depth,
                ]
            )
        br_entries.append({"nodes": nodes_mr})
        br_infos.append(
            {
                **info,
                "node_z_is_top": True,
                "depth_m": depth,
                "extend_before_m": 0.0,
                "extend_after_m": 0.0,
            }
        )
        br_cfgs.append(_gallery_bridge_conform_cfg(g, defaults, info))
        plate_oids.add(g.get("objectid"))

    if br_entries:
        z_tgt, w_acc, bridge_stats = bb.conform_bridge_heightmap(
            br_entries,
            br_infos,
            br_cfgs,
            size=size,
            extent=extent,
            elev=out,
        )
        n_app = _apply_bridge_conform_weights(out, z_tgt, w_acc)
        changed += n_app
        max_abs = max(max_abs, float(bridge_stats.get("cut_max_delta_m") or 0.0))
        max_abs = max(max_abs, float(bridge_stats.get("max_delta_m") or 0.0))
        print(
            f"Tunnel deck conform (bridge cut): plates={len(br_entries)} "
            f"applied={n_app} cut_max={bridge_stats.get('cut_max_delta_m')}m"
        )

    for g in galleries:
        cfg_conform = g.get("approach_conform")
        if cfg_conform is None:
            cfg_conform = defaults.get("approach_conform", True)
        if not cfg_conform:
            continue
        if g.get("objectid") in plate_oids:
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
        abut = info.get("abutment_s")
        if abut is not None and len(abut) >= 2:
            a0, a1 = float(abut[0]), float(abut[1])
            if a1 < a0:
                a0, a1 = a1, a0
            pad_band = [
                n
                for n in nodes
                if a0 - 1e-6 <= float(n["s"]) <= a1 + 1e-6
            ]
            if len(pad_band) >= 2:
                bands.append(pad_band)

        for band in bands:
            half_ref = 0.5 * max(float(n.get("width") or 7.5) for n in band) + 0.5 * width_extra
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
                    dist, z_road, hw = proj[0], proj[1], proj[2]
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
        "bridge_deck": bridge_stats,
    }
    print(
        f"Approach conform: tested={tested} changed={changed} "
            f"max_delta={max_abs:.3f}m (heightmap -> MeshRoad Z, soft shoulders)"
    )
    return out, stats


def _daylight_portal_specs(g: dict, info: dict) -> list[dict]:
    """Daylight portals only (skip the one_ended fake end)."""
    portal_s = info.get("portal_s") or g.get("portal_s") or []
    if len(portal_s) < 2:
        return []
    s0, s1 = float(portal_s[0]), float(portal_s[1])
    if s1 < s0:
        s0, s1 = s1, s0
    fake = info.get("fake_end") or g.get("fake_end")
    one = bool(
        info.get("one_ended")
        if info.get("one_ended") is not None
        else g.get("one_ended")
    )
    specs: list[dict] = []
    if not (one and fake == "s0"):
        specs.append({"end": "s0", "s": s0, "out": -1.0})
    if not (one and fake == "s1"):
        specs.append({"end": "s1", "s": s1, "out": 1.0})
    return specs


def _z_on_pts(
    x: float, y: float, pts: list, max_dist: float = 12.0
) -> float | None:
    """Z of the nearest segment on ``pts`` (x,y,z,…), or None if too far."""
    best: tuple[float, float] | None = None
    for i in range(len(pts) - 1):
        x0, y0, z0 = float(pts[i][0]), float(pts[i][1]), float(pts[i][2])
        x1, y1, z1 = float(pts[i + 1][0]), float(pts[i + 1][1]), float(pts[i + 1][2])
        dx, dy = x1 - x0, y1 - y0
        seg2 = dx * dx + dy * dy
        if seg2 < 1e-12:
            continue
        t = max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / seg2))
        dist = math.hypot(x - (x0 + t * dx), y - (y0 + t * dy))
        z = z0 + t * (z1 - z0)
        if best is None or dist < best[0]:
            best = (dist, z)
    if best is None or best[0] > max_dist:
        return None
    return best[1]


def _surface_road_z_fn(site: dict):
    """A12 / unify-approach Z (not DGM) for deck approach ramps."""
    from gip_road_segments import load_gip_polylines_for_decals

    roads = load_gip_polylines_for_decals(site)
    preferred: list = []
    fallback: list = []
    for r in roads:
        rid = str(r.get("id") or "")
        name = str(r.get("name") or "")
        code = str(r.get("str_code") or "")
        pts = []
        for n in r.get("pts") or r.get("nodes") or []:
            if len(n) >= 3:
                pts.append((float(n[0]), float(n[1]), float(n[2])))
        if len(pts) < 2:
            continue
        if "approach" in rid.lower() or "approach" in name.lower():
            preferred.extend(pts)
        elif code == "A12":
            fallback.extend(pts)
    use = preferred or fallback
    if len(use) < 2:
        return None

    def z_at(x: float, y: float) -> float | None:
        return _z_on_pts(x, y, use, max_dist=12.0)

    return z_at


def append_daylight_deck_approach(
    nodes: list[dict],
    info: dict,
    cfg: dict,
    z_terrain=None,
    z_road_at=None,
) -> list[dict]:
    """Extend the MeshRoad past each daylight portal onto the road ahead.

    The GIP / unify axis usually stops at the door, so ``sample_road`` cannot
    grow the slab. New samples follow the exit tangent. Z goes from portal
    deck to the A12 (``z_road_at``), not a single DGM pixel.
    The tunnel shell stays on ``portal_s``; only the deck uses these nodes.
    """
    extend_m = float(cfg.get("deck_extend_m") or 0.0)
    if extend_m <= 0.2 or len(nodes) < 2:
        return nodes
    specs = _daylight_portal_specs({"portal_s": info.get("portal_s")}, info)
    if not specs:
        return nodes
    step = max(0.5, min(1.0, float(cfg.get("step_m") or 1.0)))
    by_s = sorted(nodes, key=lambda n: float(n["s"]))
    out = list(by_s)
    for spec in specs:
        s_p = float(spec["s"])
        out_s = float(spec["out"])
        portal = min(out, key=lambda n: abs(float(n["s"]) - s_p))
        z_p = float(portal["z_road"])
        width = float(portal.get("width") or 7.5)
        if out_s >= 0.0:
            a, b = out[-2], out[-1]
        else:
            a, b = out[0], out[1]
        dx = float(b["x"]) - float(a["x"])
        dy = float(b["y"]) - float(a["y"])
        if float(b["s"]) < float(a["s"]):
            dx, dy = -dx, -dy
        seg = math.hypot(dx, dy) or 1.0
        tx, ty = dx / seg, dy / seg
        ox, oy = tx * out_s, ty * out_s
        def _z_along(d: float, x: float, y: float) -> float:
            z_tgt = z_p
            if z_road_at is not None:
                z_r = z_road_at(x, y)
                if z_r is not None:
                    z_tgt = float(z_r)
            elif z_terrain is not None:
                z_tgt = float(z_terrain(x, y))
            hold_m = min(2.0, 0.25 * extend_m)
            if d <= hold_m:
                return z_p
            t = (d - hold_m) / max(extend_m - hold_m, 1e-6)
            return z_p + t * (z_tgt - z_p)

        # Rewrite Z on existing samples past the door (A12, not DGM lip).
        for n in out:
            s = float(n["s"])
            along = (s - s_p) * out_s
            if along < -1e-6 or along > extend_m + 1e-6:
                continue
            n["z_road"] = round(_z_along(along, float(n["x"]), float(n["y"])), 3)
            n["z_hermite"] = n["z_road"]
        have = max(
            ((float(n["s"]) - s_p) * out_s for n in out),
            default=0.0,
        )
        if have >= extend_m - 0.25:
            end_x = float(portal["x"]) + ox * extend_m
            end_y = float(portal["y"]) + oy * extend_m
            print(
                f"deck approach {info.get('name') or '?'}: "
                f"{spec['end']} +{extend_m:.1f}m z {z_p:.2f} -> "
                f"{_z_along(extend_m, end_x, end_y):.2f}"
            )
            continue
        n_steps = max(2, int(math.ceil((extend_m - max(have, 0.0)) / step)))
        start_d = max(have, 0.0)
        extra: list[dict] = []
        z_end_print = z_p
        for i in range(1, n_steps + 1):
            d = start_d + (extend_m - start_d) * (i / n_steps)
            px = float(portal["x"]) + ox * d
            py = float(portal["y"]) + oy * d
            z = _z_along(d, px, py)
            z_end_print = z
            extra.append(
                {
                    "x": round(px, 3),
                    "y": round(py, 3),
                    "z_road": round(z, 3),
                    "z_hermite": round(z, 3),
                    "z_roof_inner": round(z + 4.0, 3),
                    "z_roof_outer": round(z + 4.5, 3),
                    "width": round(width, 2),
                    "tx": round(tx, 5),
                    "ty": round(ty, 5),
                    "s": round(s_p + out_s * d, 2),
                    "deck_approach": True,
                }
            )
        if out_s >= 0.0:
            out.extend(extra)
        else:
            out = list(reversed(extra)) + out
        print(
            f"deck approach {info.get('name') or '?'}: "
            f"{spec['end']} +{extend_m:.1f}m z {z_p:.2f} -> {z_end_print:.2f}"
        )
    out.sort(key=lambda n: float(n["s"]))
    if out:
        info["s0"] = round(float(out[0]["s"]), 2)
        info["s1"] = round(float(out[-1]["s"]), 2)
        info["span_len_m"] = round(float(out[-1]["s"]) - float(out[0]["s"]), 2)
        info["deck_extend_m"] = round(extend_m, 2)
    return out


def _orient_poly_from(
    xy: list[tuple[float, float]],
    origin: tuple[float, float],
) -> list[tuple[float, float]]:
    """Walk away from ``origin`` (reverse the line if the far end is closer)."""
    if len(xy) < 2:
        return list(xy)
    ox, oy = origin
    d0 = math.hypot(xy[0][0] - ox, xy[0][1] - oy)
    d1 = math.hypot(xy[-1][0] - ox, xy[-1][1] - oy)
    return list(reversed(xy)) if d1 < d0 else list(xy)


def _drop_prefix_near(
    xy: list[tuple[float, float]],
    origin: tuple[float, float],
    tol_m: float,
) -> list[tuple[float, float]]:
    """Skip leading samples that still sit on the portal."""
    if len(xy) < 2:
        return list(xy)
    ox, oy = origin
    i = 0
    while i < len(xy) - 1 and math.hypot(xy[i][0] - ox, xy[i][1] - oy) <= tol_m:
        i += 1
    return xy[i:]


def _keep_prefix_m(
    xy: list[tuple[float, float]],
    cap_m: float,
) -> list[tuple[float, float]]:
    """Keep the first ``cap_m`` metres of a polyline."""
    if cap_m <= 0.0 or len(xy) < 2:
        return list(xy)
    out = [xy[0]]
    acc = 0.0
    for a, b in zip(xy, xy[1:]):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        if acc + seg <= cap_m + 1e-9:
            out.append(b)
            acc += seg
            continue
        t = (cap_m - acc) / seg if seg > 1e-9 else 0.0
        out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
        return out
    return out


def append_one_ended_abutment(
    nodes: list[dict],
    info: dict,
    cfg: dict,
    site: dict,
    z_terrain=None,
    z_road_at=None,
) -> list[dict]:
    """One-ended deck: linear Z from the portal to a target, last 5 m = pad.

    XY comes from ``append_unify`` (sequential, not mean-axis with the bore).
    Vertically the span is a straight line from portal Z to the desired Z.
    The last ``abut_run_m`` metres stay at that Z — same seating idea as a
    bridge abutment. The closed tube stays on ``portal_s``.
    """
    uid = str(cfg.get("append_unify") or "").strip()
    if not uid or not cfg.get("one_ended") or not info.get("one_ended"):
        return nodes
    if len(nodes) < 2:
        return nodes
    specs = _daylight_portal_specs({"portal_s": info.get("portal_s")}, info)
    if len(specs) != 1:
        return nodes
    spec = specs[0]
    s_p = float(spec["s"])
    out_s = float(spec["out"])
    portal = min(nodes, key=lambda n: abs(float(n["s"]) - s_p))
    origin = (float(portal["x"]), float(portal["y"]))
    z_p = float(portal["z_road"])
    width = float(portal.get("width") or cfg.get("width_m") or 7.5)
    clear_h = float(info.get("clear_height_m") or cfg.get("clear_height_m") or 4.0)
    roof_t = float(info.get("roof_thickness_m") or cfg.get("roof_thickness_m") or 0.5)

    from gip_road_segments import load_unify_axis_xy, resample_xy

    xy, _uspec = load_unify_axis_xy(site, uid)
    xy = _orient_poly_from(xy, origin)
    xy = _drop_prefix_near(xy, origin, tol_m=4.0)
    if math.hypot(xy[0][0] - origin[0], xy[0][1] - origin[1]) > 1.0:
        xy = [origin] + list(xy)
    else:
        xy = [origin] + list(xy[1:])
    step = max(0.5, min(2.0, float(cfg.get("step_m") or 1.0)))
    xy = resample_xy(xy, step_m=step)
    if len(xy) < 2:
        print(f"one-ended abutment {info.get('name') or '?'}: {uid} too short")
        return nodes
    cap = cfg.get("append_m")
    if cap is not None and float(cap) > 0.0:
        xy = _keep_prefix_m(xy, float(cap))
        if len(xy) < 2:
            print(f"one-ended abutment {info.get('name') or '?'}: {uid} append_m too short")
            return nodes

    length = 0.0
    segs: list[float] = [0.0]
    for a, b in zip(xy, xy[1:]):
        length += math.hypot(b[0] - a[0], b[1] - a[1])
        segs.append(length)
    if length < 2.0:
        print(f"one-ended abutment {info.get('name') or '?'}: {uid} {length:.1f}m")
        return nodes

    end_xy = xy[-1]
    z_tgt = cfg.get("abutment_z")
    if z_tgt is not None:
        z_tgt = float(z_tgt)
    else:
        z_r = z_road_at(end_xy[0], end_xy[1]) if z_road_at is not None else None
        z_t = (
            float(z_terrain(end_xy[0], end_xy[1])) if z_terrain is not None else None
        )
        if bool(cfg.get("abut_snap_z", True)) and z_r is not None and z_t is not None:
            z_tgt = max(float(z_r), float(z_t))
        elif z_r is not None:
            z_tgt = float(z_r)
        elif z_t is not None:
            z_tgt = float(z_t)
        else:
            z_tgt = z_p
    abut = min(max(float(cfg.get("abut_run_m") or 5.0), 0.5), 0.5 * length)
    ramp = max(length - abut, 1e-3)

    extra: list[dict] = []
    for i, (x, y) in enumerate(xy[1:], start=1):
        d = segs[i]
        if d >= ramp:
            z = z_tgt
        else:
            z = z_p + (d / ramp) * (z_tgt - z_p)
        px0, py0 = xy[i - 1]
        dx, dy = x - px0, y - py0
        seg = math.hypot(dx, dy) or 1.0
        tx, ty = dx / seg, dy / seg
        extra.append(
            {
                "x": round(x, 3),
                "y": round(y, 3),
                "z_road": round(z, 3),
                "z_hermite": round(z, 3),
                "z_roof_inner": round(z + clear_h, 3),
                "z_roof_outer": round(z + clear_h + roof_t, 3),
                "width": round(width, 2),
                "tx": round(tx, 5),
                "ty": round(ty, 5),
                "s": round(s_p + out_s * d, 2),
                "deck_approach": True,
                "abutment": d >= ramp - 1e-6,
            }
        )
    # Drop the old marry/cover samples past the door — they share s with the
    # new span and weave a 90-degree zigzag into the MeshRoad.
    if out_s >= 0.0:
        kept = [n for n in nodes if float(n["s"]) <= s_p + 1e-6]
        out = kept + extra
    else:
        kept = [n for n in nodes if float(n["s"]) >= s_p - 1e-6]
        out = list(reversed(extra)) + kept
    out.sort(key=lambda n: float(n["s"]))
    s_end = s_p + out_s * length
    s_pad0 = s_p + out_s * ramp
    pad = sorted((s_pad0, s_end))
    info["s0"] = round(float(out[0]["s"]), 2)
    info["s1"] = round(float(out[-1]["s"]), 2)
    info["span_len_m"] = round(float(out[-1]["s"]) - float(out[0]["s"]), 2)
    info["append_unify"] = uid
    info["append_m"] = None if cap is None else round(float(cap), 2)
    info["abutment_s"] = [round(pad[0], 3), round(pad[1], 3)]
    info["abutment_end_s"] = round(s_end, 3)
    info["abutment_z"] = round(z_tgt, 3)
    info["abut_run_m"] = round(abut, 2)
    info["abutment_len_m"] = round(length, 2)
    print(
        f"one-ended abutment {info.get('name') or '?'}: {uid} "
        f"{length:.1f}m z {z_p:.2f} -> {z_tgt:.2f} pad={abut:.1f}m"
    )
    return out


def _s_window(s_a: float, s_b: float) -> tuple[float, float]:
    return (s_a, s_b) if s_a <= s_b else (s_b, s_a)


def _extend_portal_apron_nodes(
    nodes: list[dict],
    s_p: float,
    out_s: float,
    length_m: float,
    *,
    step_m: float = 0.5,
) -> list[dict]:
    """Virtual centerline from the portal toward the exit.

    The MeshRoad stops at the daylight portal, so a window past ``s_p`` has
    no real nodes. Samples continue along the exit tangent at deck Z.
    """
    if length_m <= 1e-6 or len(nodes) < 2:
        return []
    by_s = sorted(nodes, key=lambda n: float(n["s"]))
    portal = min(by_s, key=lambda n: abs(float(n["s"]) - s_p))
    if out_s >= 0.0:
        a, b = by_s[-2], by_s[-1]
    else:
        a, b = by_s[0], by_s[1]
    dx = float(b["x"]) - float(a["x"])
    dy = float(b["y"]) - float(a["y"])
    if float(b["s"]) < float(a["s"]):
        dx, dy = -dx, -dy
    seg = math.hypot(dx, dy) or 1.0
    tx, ty = dx / seg, dy / seg
    ox, oy = tx * out_s, ty * out_s
    z = float(portal["z_road"])
    z_roof = (
        float(portal["z_roof_outer"])
        if portal.get("z_roof_outer") is not None
        else z
    )
    width = float(portal.get("width") or 7.5)
    n_steps = max(2, int(math.ceil(length_m / max(step_m, 0.1))))
    out: list[dict] = []
    for i in range(n_steps + 1):
        d = i * (length_m / n_steps)
        out.append(
            {
                "x": float(portal["x"]) + ox * d,
                "y": float(portal["y"]) + oy * d,
                "z_road": z,
                "z_hermite": z,
                "z_roof_outer": z_roof,
                "width": width,
                "s": s_p + out_s * d,
            }
        )
    return out


def _raster_portal_band(
    elev: np.ndarray,
    hole: np.ndarray | None,
    nodes: list[dict],
    *,
    size: int,
    extent: float,
    half_m: float,
    pad_m: float,
    falloff_m: float,
    z_key: str,
    mode: str,
    sink_m: float = 0.0,
    s_lo: float | None = None,
    s_hi: float | None = None,
) -> tuple[int, int, float]:
    """Apply one centerline band. mode: set | raise_min | hole.

    When ``s_lo``/``s_hi`` are set, only pixels whose nearest point lies in
    that along-track window are written — otherwise a short band snaps every
    nearby pixel to the portal endpoint (door / roof bleed).

    Returns (tested, changed, max_abs_delta).
    """
    if len(nodes) < 1:
        return 0, 0, 0.0
    tested = 0
    changed = 0
    max_abs = 0.0
    xs = [float(n["x"]) for n in nodes]
    ys = [float(n["y"]) for n in nodes]
    margin = half_m + pad_m + max(falloff_m, 0.0) + 1.0
    px0, py0 = _to_px_beamng(min(xs) - margin, max(ys) + margin, size, extent)
    px1, py1 = _to_px_beamng(max(xs) + margin, min(ys) - margin, size, extent)
    c0 = max(0, int(math.floor(min(px0, px1))))
    c1 = min(size - 1, int(math.ceil(max(px0, px1))))
    r0 = max(0, int(math.floor(min(py0, py1))))
    r1 = min(size - 1, int(math.ceil(max(py0, py1))))
    hard = half_m + pad_m
    outer = hard + max(falloff_m, 1e-6)
    s_eps = 0.15
    for py in range(r0, r1 + 1):
        for px in range(c0, c1 + 1):
            bx, by = _from_px_beamng(float(px), float(py), size, extent)
            proj = _project_point_to_nodes(bx, by, nodes, z_key=z_key)
            if proj is None:
                continue
            dist, z_ref, _hw, s_hit = proj
            if dist > outer:
                continue
            if s_lo is not None and s_hi is not None:
                if s_hit < s_lo - s_eps or s_hit > s_hi + s_eps:
                    continue
            tested += 1
            if mode == "hole":
                if dist <= hard and hole is not None:
                    if hole[py, px] < 255:
                        hole[py, px] = 255
                        changed += 1
                continue
            if dist <= hard:
                w = 1.0
            else:
                w = 1.0 - (dist - hard) / max(falloff_m, 1e-6)
                w = max(0.0, min(1.0, w))
            z0 = float(elev[py, px])
            target = float(z_ref) - sink_m
            if mode == "raise_min":
                if z0 >= target - 1e-4:
                    continue
                z_new = (1.0 - w) * z0 + w * target
                if z_new < z0:
                    continue
            else:
                z_new = (1.0 - w) * z0 + w * target
            if abs(z_new - z0) < 1e-4:
                continue
            elev[py, px] = z_new
            changed += 1
            max_abs = max(max_abs, abs(z_new - z0))
    return tested, changed, max_abs


def bake_portal_seat(
    galleries: list[dict],
    span_infos: list[dict],
    *,
    size: int,
    extent: float,
    defaults: dict,
    elev: np.ndarray,
    hole: np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray | None, dict]:
    """Seat each daylight portal: deck apron, roof lintel, door hole.

    Along-track (outside → inside), default 2 m / door / 1 m:

    - toward the exit: terrain = deck Z (carriageway + pad)
    - at the portal: holemap punch (clear opening)
    - 1 m inside: terrain at least roof-outer Z (lintel so the hole has a seat)
    """
    out = elev.astype(np.float64).copy()
    hole_out = None if hole is None else np.array(hole, dtype=np.uint8, copy=True)
    info_by_oid = {info.get("objectid"): info for info in span_infos}
    n_apr = n_roof = n_hole = 0
    max_abs = 0.0
    n_portals = 0

    for g in galleries:
        if not _gallery_flag(g, defaults, "portal_seat", True):
            continue
        nodes = g.get("nodes") or []
        if len(nodes) < 2:
            continue
        info = info_by_oid.get(g.get("objectid")) or {}
        specs = _daylight_portal_specs(g, info)
        if not specs:
            continue
        apron_m = _gallery_num(g, defaults, "portal_apron_m", 8.0)
        roof_in = _gallery_num(g, defaults, "portal_roof_force_m", 0.0)
        roof_len = _gallery_num(g, defaults, "portal_roof_force_len_m", 0.0)
        hole_in = _gallery_num(g, defaults, "portal_hole_in_m", 1.0)
        hole_out_m = _gallery_num(g, defaults, "portal_hole_out_m", 0.25)
        pad = _gallery_num(g, defaults, "portal_apron_pad_m", 0.5)
        do_hole = _gallery_flag(g, defaults, "portal_hole", False)
        clear_w = g.get("clear_width_m")
        if clear_w is None:
            clear_w = defaults.get("clear_width_m")
        road_half = 0.5 * max(float(n.get("width") or 7.5) for n in nodes)
        inner_half = (
            max(road_half, 0.5 * float(clear_w))
            if clear_w is not None and float(clear_w) > 1e-6
            else road_half
        )
        wall_t = _gallery_num(g, defaults, "wall_thickness_m", 0.4)

        for spec in specs:
            n_portals += 1
            s_p = float(spec["s"])
            out_s = float(spec["out"])
            # Start 0.5 m outside the door. Pixels inside the bore project
            # onto the portal endpoint and would flatten the mountain inward.
            a0, a1 = _s_window(s_p + 0.5 * out_s, s_p + apron_m * out_s)
            h0, h1 = _s_window(s_p - hole_in * out_s, s_p + hole_out_m * out_s)
            r0, r1 = _s_window(
                s_p - roof_in * out_s,
                s_p - (roof_in + roof_len) * out_s,
            )
            apron_nodes = _nodes_in_s_window(nodes, a0, a1)
            if len(apron_nodes) < 2:
                apron_nodes = _extend_portal_apron_nodes(nodes, s_p, out_s, apron_m)
            hole_nodes = _nodes_in_s_window(nodes, h0, h1)
            roof_nodes = _nodes_in_s_window(nodes, r0, r1)

            t, c, d = _raster_portal_band(
                out,
                hole_out,
                apron_nodes,
                size=size,
                extent=extent,
                half_m=road_half,
                pad_m=pad,
                falloff_m=1.5,
                z_key="road",
                mode="set",
                sink_m=0.02,
                s_lo=a0,
                s_hi=a1,
            )
            n_apr += c
            max_abs = max(max_abs, d)

            if roof_in > 1e-6 and roof_len > 1e-6 and len(roof_nodes) >= 1:
                t, c, d = _raster_portal_band(
                    out,
                    hole_out,
                    roof_nodes,
                    size=size,
                    extent=extent,
                    half_m=inner_half + wall_t,
                    pad_m=0.25,
                    falloff_m=1.0,
                    z_key="roof",
                    mode="raise_min",
                    s_lo=r0,
                    s_hi=r1,
                )
                n_roof += c
                max_abs = max(max_abs, d)

            if do_hole and hole_out is not None:
                t, c, d = _raster_portal_band(
                    out,
                    hole_out,
                    hole_nodes,
                    size=size,
                    extent=extent,
                    half_m=inner_half,
                    pad_m=0.15,
                    falloff_m=0.0,
                    z_key="road",
                    mode="hole",
                    s_lo=h0,
                    s_hi=h1,
                )
                n_hole += c

    stats = {
        "portals": n_portals,
        "apron_px": n_apr,
        "roof_px": n_roof,
        "hole_px": n_hole,
        "max_delta_m": round(max_abs, 3),
        "method": "portal_seat_apron_roof_hole",
    }
    print(
        f"Portal seat: portals={n_portals} apron_px={n_apr} "
        f"roof_px={n_roof} hole_px={n_hole} max_delta={max_abs:.3f}m"
    )
    return out, hole_out, stats


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


def _gallery_flag(g: dict, defaults: dict, key: str, default: bool = False) -> bool:
    if g.get(key) is not None:
        return bool(g.get(key))
    if defaults.get(key) is not None:
        return bool(defaults.get(key))
    return bool(default)


def _gallery_num(g: dict, defaults: dict, key: str, fallback: float) -> float:
    if g.get(key) is not None:
        return float(g[key])
    if defaults.get(key) is not None:
        return float(defaults[key])
    return float(fallback)


def _smooth_weight(dist_m: float, core_m: float, soft_m: float, curve: str) -> float:
    """1 inside core, 0 outside core+soft; smooth falloff in between."""
    if dist_m <= core_m:
        return 1.0
    span = max(0.0, soft_m)
    if span < 1e-9:
        return 0.0
    t = (dist_m - core_m) / span
    if t >= 1.0:
        return 0.0
    u = 1.0 - t  # 1 at core edge → 0 at outer
    c = (curve or "smoothstep").lower().strip()
    if c in ("linear", "lin"):
        return u
    if c in ("cosine", "cos", "cosine_half"):
        return 0.5 * (1.0 + math.cos(math.pi * (1.0 - u)))
    # smoothstep (default): 3u² − 2u³
    return u * u * (3.0 - 2.0 * u)


def _dist_point_to_poly(
    bx: float,
    by: float,
    poly: list[tuple[float, float, float]],
) -> tuple[float, float]:
    """Nearest distance to polyline + Z at closest point. poly = (x,y,z)."""
    if not poly:
        return 1e9, 0.0
    best_d = 1e9
    best_z = float(poly[0][2])
    for i, (x0, y0, z0) in enumerate(poly):
        if i + 1 >= len(poly):
            d = math.hypot(bx - x0, by - y0)
            if d < best_d:
                best_d = d
                best_z = z0
            continue
        x1, y1, z1 = poly[i + 1]
        dx, dy = x1 - x0, y1 - y0
        seg2 = dx * dx + dy * dy
        if seg2 < 1e-12:
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((bx - x0) * dx + (by - y0) * dy) / seg2))
        px = x0 + t * dx
        py = y0 + t * dy
        d = math.hypot(bx - px, by - py)
        if d < best_d:
            best_d = d
            best_z = z0 + t * (z1 - z0)
    return best_d, best_z


def _embed_lip_radii(
    g: dict,
    defaults: dict,
    *,
    mpp: float,
) -> tuple[float, float, float, float, float, float]:
    """Return (upper_core_m, out_core_m, in_core_m, soft_m, foundation_m, lower_z_m).

    Lower bake is centered on the foundation *outer* line:
    - ``out_core`` = reach away from the road (terrain side)
    - ``in_core``  = reach toward the road (defaults to foundation_m so the
      Vorfeld band clears without crossing the carriageway)
    - lower bake/debug Z = ``z_road - foundation_m + lower_z_m``
    """
    rings = int(
        g["terrain_embed_rings"]
        if g.get("terrain_embed_rings") is not None
        else defaults.get("terrain_embed_rings") or 2
    )
    rings = max(1, min(8, rings))
    ring_m = rings * mpp
    upper = g.get("terrain_embed_upper_m")
    if upper is None:
        upper = defaults.get("terrain_embed_upper_m")
    lower = g.get("terrain_embed_lower_m")
    if lower is None:
        lower = defaults.get("terrain_embed_lower_m")
    upper_r = float(upper) if upper is not None else ring_m
    out_r = float(lower) if lower is not None else ring_m
    foundation = _gallery_num(g, defaults, "terrain_embed_foundation_m", 1.0)
    soft = _gallery_num(g, defaults, "terrain_embed_soft_m", 1.0)
    in_raw = g.get("terrain_embed_in_m")
    if in_raw is None:
        in_raw = defaults.get("terrain_embed_in_m")
    in_r = float(in_raw) if in_raw is not None else foundation
    lower_z = _gallery_num(g, defaults, "terrain_embed_lower_z_m", 0.0)
    return (
        max(0.0, upper_r),
        max(0.0, out_r),
        max(0.0, in_r),
        max(0.0, soft),
        max(0.0, foundation),
        float(lower_z),
    )


def _foundation_edge_polylines_n(
    nodes: list[dict],
    *,
    open_side: str,
    open_extra_m: float,
    width_extra_m: float,
    foundation_m: float,
    only_open: bool,
    step_m: float = 0.5,
    z_offset_m: float = 0.0,
) -> list[list[tuple[float, float, float, float, float]]]:
    """Foundation outer lip as (x,y,z,nx,ny) with outward normals (away from road).

    Z = ``z_road - foundation_m + z_offset_m`` (``terrain_embed_lower_z_m``).
    """
    if len(nodes) < 1:
        return []
    by_s = sorted(nodes, key=lambda n: float(n["s"]))
    samples: list[dict] = [by_s[0]]
    for n in by_s[1:]:
        prev = samples[-1]
        ds = abs(float(n["s"]) - float(prev["s"]))
        if ds < 1e-6:
            samples[-1] = n
            continue
        n_seg = max(1, int(math.ceil(ds / max(step_m, 0.25))))
        for k in range(1, n_seg + 1):
            t = k / n_seg
            samples.append(
                {
                    "x": float(prev["x"]) + t * (float(n["x"]) - float(prev["x"])),
                    "y": float(prev["y"]) + t * (float(n["y"]) - float(prev["y"])),
                    "tx": float(n.get("tx") or prev.get("tx") or 1.0),
                    "ty": float(n.get("ty") or prev.get("ty") or 0.0),
                    "width": float(prev.get("width") or 7.5)
                    + t * (float(n.get("width") or 7.5) - float(prev.get("width") or 7.5)),
                    "z_road": float(prev["z_road"])
                    + t * (float(n["z_road"]) - float(prev["z_road"])),
                    "s": float(prev["s"]) + t * (float(n["s"]) - float(prev["s"])),
                }
            )
    side = (open_side or "left").lower().strip()
    want_left = (side in ("left", "both")) if only_open else True
    want_right = (side in ("right", "both")) if only_open else True
    out_lines: list[list[tuple[float, float, float, float, float]]] = []
    for want, u_name in ((want_left, "left"), (want_right, "right")):
        if not want:
            continue
        line: list[tuple[float, float, float, float, float]] = []
        for n in samples:
            tx = float(n.get("tx") or 1.0)
            ty = float(n.get("ty") or 0.0)
            left_u, right_u = bb.left_right_unit(tx, ty)
            u = left_u if u_name == "left" else right_u
            half = 0.5 * float(n.get("width") or 7.5) + 0.5 * width_extra_m
            if side in (u_name, "both"):
                bot_lat = half + open_extra_m
            else:
                bot_lat = half
            lat = bot_lat + max(0.0, foundation_m)
            z_bot = float(n["z_road"]) - foundation_m + z_offset_m
            line.append(
                (
                    float(n["x"]) + lat * u[0],
                    float(n["y"]) + lat * u[1],
                    z_bot,
                    float(u[0]),
                    float(u[1]),
                )
            )
        if len(line) >= 2:
            out_lines.append(line)
    return out_lines


def _portal_face_polyline_n(
    node: dict,
    *,
    into_sign: float,
    out_m: float,
    open_extra_m: float,
    width_extra_m: float,
    foundation_m: float,
    overhang_m: float,
    n_lat: int = 17,
    z_offset_m: float = 0.0,
    lat_span_m: float | None = None,
) -> list[tuple[float, float, float, float, float]]:
    """Portal face lower lip with outward normal (= out of gallery along road).

    ``lat_span_m`` overrides lateral half-extent (default: road half + extras +
    foundation). Holemap stamps should pass carriageway-only span so hillside
    cells above the lintel are not punched.
    """
    x = float(node["x"])
    y = float(node["y"])
    tx = float(node.get("tx") or 1.0)
    ty = float(node.get("ty") or 0.0)
    horiz = math.hypot(tx, ty) or 1.0
    fx, fy = tx / horiz, ty / horiz
    nx, ny = into_sign * fx, into_sign * fy
    left_u, _ = bb.left_right_unit(tx, ty)
    half = 0.5 * float(node.get("width") or 7.5) + 0.5 * width_extra_m
    z_bot = float(node["z_road"]) - foundation_m + z_offset_m
    ox = x + into_sign * out_m * fx
    oy = y + into_sign * out_m * fy
    if lat_span_m is not None:
        span = max(0.5, float(lat_span_m))
    else:
        span = half + max(open_extra_m, overhang_m) + foundation_m
    lower: list[tuple[float, float, float, float, float]] = []
    for i in range(max(3, n_lat)):
        t = -1.0 + 2.0 * i / max(n_lat - 1, 1)
        lat = t * span
        lower.append(
            (
                ox + lat * left_u[0],
                oy + lat * left_u[1],
                z_bot,
                nx,
                ny,
            )
        )
    return lower


def _dist_signed_to_poly_n(
    bx: float,
    by: float,
    poly: list[tuple[float, float, float, float, float]],
) -> tuple[float, float, float]:
    """Nearest euclidean distance, Z, and signed lateral (dot with outward normal)."""
    if not poly:
        return 1e9, 0.0, 0.0
    best_d = 1e9
    best_z = float(poly[0][2])
    best_s = 0.0
    for i, (x0, y0, z0, nx0, ny0) in enumerate(poly):
        if i + 1 >= len(poly):
            d = math.hypot(bx - x0, by - y0)
            if d < best_d:
                best_d = d
                best_z = z0
                best_s = (bx - x0) * nx0 + (by - y0) * ny0
            continue
        x1, y1, z1, nx1, ny1 = poly[i + 1]
        dx, dy = x1 - x0, y1 - y0
        seg2 = dx * dx + dy * dy
        if seg2 < 1e-12:
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((bx - x0) * dx + (by - y0) * dy) / seg2))
        px = x0 + t * dx
        py = y0 + t * dy
        d = math.hypot(bx - px, by - py)
        if d < best_d:
            best_d = d
            best_z = z0 + t * (z1 - z0)
            nx = nx0 + t * (nx1 - nx0)
            ny = ny0 + t * (ny1 - ny0)
            nh = math.hypot(nx, ny) or 1.0
            best_s = ((bx - px) * nx + (by - py) * ny) / nh
    return best_d, best_z, best_s


def _bake_lip_field_sided(
    out: np.ndarray,
    *,
    size: int,
    extent: float,
    poly: list[tuple[float, float, float, float, float]],
    out_core_m: float,
    in_core_m: float,
    soft_m: float,
    max_delta_m: float,
    curve: str,
) -> tuple[int, int, float]:
    """Bake lip with separate inward/outward hard radii (half-plane of normal).

    Positive signed distance = outward (along stored normal). A side with
    ``core_m <= 0`` is skipped entirely (no soft falloff either).
    """
    if len(poly) < 1:
        return 0, 0, 0.0
    radius = max(out_core_m, in_core_m) + soft_m
    if radius < 1e-9:
        return 0, 0, 0.0
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    margin = radius + 1.5
    px0, py0 = _to_px_beamng(min(xs) - margin, max(ys) + margin, size, extent)
    px1, py1 = _to_px_beamng(max(xs) + margin, min(ys) - margin, size, extent)
    c0 = max(0, int(math.floor(min(px0, px1))))
    c1 = min(size - 1, int(math.ceil(max(px0, px1))))
    r0 = max(0, int(math.floor(min(py0, py1))))
    r1 = min(size - 1, int(math.ceil(max(py0, py1))))
    if c1 < c0 or r1 < r0:
        return 0, 0, 0.0

    tested = 0
    changed = 0
    max_abs = 0.0
    for py in range(r0, r1 + 1):
        for px in range(c0, c1 + 1):
            bx, by = _from_px_beamng(float(px), float(py), size, extent)
            dist, z_tgt, signed = _dist_signed_to_poly_n(bx, by, poly)
            core = out_core_m if signed >= 0.0 else in_core_m
            # core<=0 ⇒ no influence on that half-plane (do NOT leak soft_m
            # across the line — that was wiping the green lower lip).
            if core <= 1e-9:
                continue
            if dist > core + soft_m + 1e-9:
                continue
            wt = _smooth_weight(dist, core, soft_m, curve)
            if wt <= 1e-6:
                continue
            tested += 1
            z0 = float(out[py, px])
            if abs(z0 - z_tgt) > max_delta_m:
                continue
            z_new = (1.0 - wt) * z0 + wt * z_tgt
            if abs(z_new - z0) < 1e-4:
                continue
            out[py, px] = z_new
            changed += 1
            max_abs = max(max_abs, abs(z_new - z0))
    return tested, changed, max_abs


def _gallery_edge_polylines(
    nodes: list[dict],
    *,
    open_side: str,
    overhang_m: float,
    open_extra_m: float,
    width_extra_m: float,
    foundation_m: float,
    only_open: bool,
    step_m: float = 0.5,
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    """Dense (x,y,z) polylines for lower (road) and upper (roof) lips."""
    if len(nodes) < 1:
        return [], []
    # Resample along s at ~step_m
    by_s = sorted(nodes, key=lambda n: float(n["s"]))
    samples: list[dict] = [by_s[0]]
    for n in by_s[1:]:
        prev = samples[-1]
        ds = abs(float(n["s"]) - float(prev["s"]))
        if ds < 1e-6:
            samples[-1] = n
            continue
        n_seg = max(1, int(math.ceil(ds / max(step_m, 0.25))))
        for k in range(1, n_seg + 1):
            t = k / n_seg
            samples.append(
                {
                    "x": float(prev["x"]) + t * (float(n["x"]) - float(prev["x"])),
                    "y": float(prev["y"]) + t * (float(n["y"]) - float(prev["y"])),
                    "tx": float(n.get("tx") or prev.get("tx") or 1.0),
                    "ty": float(n.get("ty") or prev.get("ty") or 0.0),
                    "width": float(prev.get("width") or 7.5)
                    + t * (float(n.get("width") or 7.5) - float(prev.get("width") or 7.5)),
                    "z_road": float(prev["z_road"])
                    + t * (float(n["z_road"]) - float(prev["z_road"])),
                    "z_roof_outer": float(
                        prev["z_roof_outer"]
                        if prev.get("z_roof_outer") is not None
                        else prev["z_road"]
                    )
                    + t
                    * (
                        float(
                            n["z_roof_outer"]
                            if n.get("z_roof_outer") is not None
                            else n["z_road"]
                        )
                        - float(
                            prev["z_roof_outer"]
                            if prev.get("z_roof_outer") is not None
                            else prev["z_road"]
                        )
                    ),
                    "s": float(prev["s"]) + t * (float(n["s"]) - float(prev["s"])),
                }
            )

    lower: list[tuple[float, float, float]] = []
    upper: list[tuple[float, float, float]] = []
    side = (open_side or "left").lower().strip()
    want_left = (side in ("left", "both")) if only_open else True
    want_right = (side in ("right", "both")) if only_open else True

    for n in samples:
        tx = float(n.get("tx") or 1.0)
        ty = float(n.get("ty") or 0.0)
        left_u, right_u = bb.left_right_unit(tx, ty)
        half = 0.5 * float(n.get("width") or 7.5) + 0.5 * width_extra_m
        z_bot = float(n["z_road"]) - foundation_m
        z_top = float(n.get("z_roof_outer") if n.get("z_roof_outer") is not None else n["z_road"])
        x = float(n["x"])
        y = float(n["y"])

        def _add(u: tuple[float, float], bot_lat: float, top_lat: float) -> None:
            # Road edge
            lower.append((x + bot_lat * u[0], y + bot_lat * u[1], z_bot))
            # Forefield midline (foundation band center) for Vorfeld planieren
            if foundation_m > 1e-6:
                mid = bot_lat + 0.5 * foundation_m
                lower.append((x + mid * u[0], y + mid * u[1], z_bot))
                outer = bot_lat + foundation_m
                lower.append((x + outer * u[0], y + outer * u[1], z_bot))
            upper.append((x + top_lat * u[0], y + top_lat * u[1], z_top))

        if want_left:
            if side in ("left", "both"):
                _add(left_u, half + open_extra_m, half + max(overhang_m, open_extra_m))
            else:
                _add(left_u, half, half + overhang_m)
        if want_right:
            if side in ("right", "both"):
                _add(right_u, half + open_extra_m, half + max(overhang_m, open_extra_m))
            else:
                _add(right_u, half, half + overhang_m)
    return lower, upper


def _portal_face_polylines(
    node: dict,
    *,
    into_sign: float,
    out_m: float,
    open_extra_m: float,
    width_extra_m: float,
    foundation_m: float,
    overhang_m: float,
    n_lat: int = 17,
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    """Cross-road polylines just outside a portal (lower floor + upper lintel)."""
    x = float(node["x"])
    y = float(node["y"])
    tx = float(node.get("tx") or 1.0)
    ty = float(node.get("ty") or 0.0)
    horiz = math.hypot(tx, ty) or 1.0
    fx, fy = tx / horiz, ty / horiz
    left_u, _ = bb.left_right_unit(tx, ty)
    half = 0.5 * float(node.get("width") or 7.5) + 0.5 * width_extra_m
    z_bot = float(node["z_road"]) - foundation_m
    z_top = float(
        node["z_roof_outer"]
        if node.get("z_roof_outer") is not None
        else node["z_road"]
    )
    ox = x + into_sign * out_m * fx
    oy = y + into_sign * out_m * fy
    ux = x - into_sign * 0.35 * out_m * fx
    uy = y - into_sign * 0.35 * out_m * fy
    span = half + max(open_extra_m, overhang_m) + foundation_m
    lower: list[tuple[float, float, float]] = []
    upper: list[tuple[float, float, float]] = []
    for i in range(max(3, n_lat)):
        t = -1.0 + 2.0 * i / max(n_lat - 1, 1)
        lat = t * span
        lower.append((ox + lat * left_u[0], oy + lat * left_u[1], z_bot))
        upper.append((ux + lat * left_u[0], uy + lat * left_u[1], z_top))
    return lower, upper


def _roof_edge_polylines(
    nodes: list[dict],
    *,
    open_side: str,
    overhang_m: float,
    open_extra_m: float,
    width_extra_m: float,
    only_open: bool,
    step_m: float = 0.5,
    foundation_m: float = 1.0,
    inset_m: float = 1.0,
) -> list[list[tuple[float, float, float]]]:
    """Roof lip XY/Z — inset toward centerline vs green; Z = z_roof_outer."""
    return [
        [(p[0], p[1], p[2]) for p in line]
        for line in _roof_edge_polylines_n(
            nodes,
            open_side=open_side,
            overhang_m=overhang_m,
            open_extra_m=open_extra_m,
            width_extra_m=width_extra_m,
            only_open=only_open,
            step_m=step_m,
            foundation_m=foundation_m,
            inset_m=inset_m,
        )
    ]


def _roof_edge_polylines_n(
    nodes: list[dict],
    *,
    open_side: str,
    overhang_m: float,
    open_extra_m: float,
    width_extra_m: float,
    only_open: bool,
    step_m: float = 0.5,
    foundation_m: float = 1.0,
    inset_m: float = 1.0,
) -> list[list[tuple[float, float, float, float, float]]]:
    """Roof lip (x,y,z,nx,ny); hangward normals; inset toward centerline vs green.

    Green station = bot_lat + foundation_m. Magenta = that minus inset_m (>=1 cell)
    so hangward upper cannot cover the lower valley half-plane. Z = z_roof_outer.
    """
    _ = overhang_m
    if len(nodes) < 1:
        return []
    by_s = sorted(nodes, key=lambda n: float(n["s"]))
    samples: list[dict] = [by_s[0]]
    for n in by_s[1:]:
        prev = samples[-1]
        ds = abs(float(n["s"]) - float(prev["s"]))
        if ds < 1e-6:
            samples[-1] = n
            continue
        n_seg = max(1, int(math.ceil(ds / max(step_m, 0.25))))
        for k in range(1, n_seg + 1):
            t = k / n_seg
            z0 = float(
                prev["z_roof_outer"]
                if prev.get("z_roof_outer") is not None
                else prev["z_road"]
            )
            z1 = float(
                n["z_roof_outer"] if n.get("z_roof_outer") is not None else n["z_road"]
            )
            samples.append(
                {
                    "x": float(prev["x"]) + t * (float(n["x"]) - float(prev["x"])),
                    "y": float(prev["y"]) + t * (float(n["y"]) - float(prev["y"])),
                    "tx": float(n.get("tx") or prev.get("tx") or 1.0),
                    "ty": float(n.get("ty") or prev.get("ty") or 0.0),
                    "width": float(prev.get("width") or 7.5)
                    + t * (float(n.get("width") or 7.5) - float(prev.get("width") or 7.5)),
                    "z_roof_outer": z0 + t * (z1 - z0),
                    "s": float(prev["s"]) + t * (float(n["s"]) - float(prev["s"])),
                }
            )
    side = (open_side or "left").lower().strip()
    want_left = (side in ("left", "both")) if only_open else True
    want_right = (side in ("right", "both")) if only_open else True
    inset = max(0.0, float(inset_m))
    out_lines: list[list[tuple[float, float, float, float, float]]] = []
    for want, u_name in ((want_left, "left"), (want_right, "right")):
        if not want:
            continue
        line: list[tuple[float, float, float, float, float]] = []
        for n in samples:
            tx = float(n.get("tx") or 1.0)
            ty = float(n.get("ty") or 0.0)
            left_u, right_u = bb.left_right_unit(tx, ty)
            u_out = left_u if u_name == "left" else right_u
            nx, ny = -float(u_out[0]), -float(u_out[1])
            half = 0.5 * float(n.get("width") or 7.5) + 0.5 * width_extra_m
            if side in (u_name, "both"):
                bot_lat = half + open_extra_m
            else:
                bot_lat = half
            green_lat = bot_lat + max(0.0, foundation_m)
            lat = max(0.0, green_lat - inset)
            z_top = float(
                n["z_roof_outer"]
                if n.get("z_roof_outer") is not None
                else n.get("z_road") or 0.0
            )
            line.append(
                (
                    float(n["x"]) + lat * u_out[0],
                    float(n["y"]) + lat * u_out[1],
                    z_top,
                    nx,
                    ny,
                )
            )
        if len(line) >= 2:
            out_lines.append(line)
    return out_lines


def _portal_face_upper_polyline_n(
    node: dict,
    *,
    into_sign: float,
    out_m: float,
    open_extra_m: float,
    width_extra_m: float,
    foundation_m: float,
    overhang_m: float,
    n_lat: int = 17,
    inset_frac: float = 0.35,
    lat_span_m: float | None = None,
) -> list[tuple[float, float, float, float, float]]:
    """Portal-front upper lip with outward normal (out of gallery along road).

    Station is inset toward gallery interior vs lower (``inset_frac * out_m``).
    Bake with ``out_core=0``, ``in_core=upper`` so influence is gallery-inward only.
    ``lat_span_m`` — see ``_portal_face_polyline_n``.
    """
    x = float(node["x"])
    y = float(node["y"])
    tx = float(node.get("tx") or 1.0)
    ty = float(node.get("ty") or 0.0)
    horiz = math.hypot(tx, ty) or 1.0
    fx, fy = tx / horiz, ty / horiz
    # Same outward normal as lower face; sided bake uses the inward half-plane.
    nx, ny = into_sign * fx, into_sign * fy
    left_u, _ = bb.left_right_unit(tx, ty)
    half = 0.5 * float(node.get("width") or 7.5) + 0.5 * width_extra_m
    z_top = float(
        node["z_roof_outer"]
        if node.get("z_roof_outer") is not None
        else node["z_road"]
    )
    ux = x - into_sign * float(inset_frac) * out_m * fx
    uy = y - into_sign * float(inset_frac) * out_m * fy
    if lat_span_m is not None:
        span = max(0.5, float(lat_span_m))
    else:
        span = half + max(open_extra_m, overhang_m) + foundation_m
    upper: list[tuple[float, float, float, float, float]] = []
    for i in range(max(3, n_lat)):
        t = -1.0 + 2.0 * i / max(n_lat - 1, 1)
        lat = t * span
        upper.append(
            (
                ux + lat * left_u[0],
                uy + lat * left_u[1],
                z_top,
                nx,
                ny,
            )
        )
    return upper


def _portal_face_upper_polyline(
    node: dict,
    *,
    into_sign: float,
    out_m: float,
    open_extra_m: float,
    width_extra_m: float,
    foundation_m: float,
    overhang_m: float,
    n_lat: int = 17,
) -> list[tuple[float, float, float]]:
    """Portal lintel lip XY/Z (debug) — see ``_portal_face_upper_polyline_n``."""
    return [
        (p[0], p[1], p[2])
        for p in _portal_face_upper_polyline_n(
            node,
            into_sign=into_sign,
            out_m=out_m,
            open_extra_m=open_extra_m,
            width_extra_m=width_extra_m,
            foundation_m=foundation_m,
            overhang_m=overhang_m,
            n_lat=n_lat,
        )
    ]


def _foundation_edge_polylines(
    nodes: list[dict],
    *,
    open_side: str,
    open_extra_m: float,
    width_extra_m: float,
    foundation_m: float,
    only_open: bool,
    step_m: float = 0.5,
) -> list[list[tuple[float, float, float]]]:
    """One polyline per side: road edge + foundation outward (z = z_road − foundation).

    This is the line terrain_embed pulls to foundation height (Vorfeld outer).
    """
    if len(nodes) < 1:
        return []
    by_s = sorted(nodes, key=lambda n: float(n["s"]))
    samples: list[dict] = [by_s[0]]
    for n in by_s[1:]:
        prev = samples[-1]
        ds = abs(float(n["s"]) - float(prev["s"]))
        if ds < 1e-6:
            samples[-1] = n
            continue
        n_seg = max(1, int(math.ceil(ds / max(step_m, 0.25))))
        for k in range(1, n_seg + 1):
            t = k / n_seg
            samples.append(
                {
                    "x": float(prev["x"]) + t * (float(n["x"]) - float(prev["x"])),
                    "y": float(prev["y"]) + t * (float(n["y"]) - float(prev["y"])),
                    "tx": float(n.get("tx") or prev.get("tx") or 1.0),
                    "ty": float(n.get("ty") or prev.get("ty") or 0.0),
                    "width": float(prev.get("width") or 7.5)
                    + t * (float(n.get("width") or 7.5) - float(prev.get("width") or 7.5)),
                    "z_road": float(prev["z_road"])
                    + t * (float(n["z_road"]) - float(prev["z_road"])),
                    "s": float(prev["s"]) + t * (float(n["s"]) - float(prev["s"])),
                }
            )
    side = (open_side or "left").lower().strip()
    want_left = (side in ("left", "both")) if only_open else True
    want_right = (side in ("right", "both")) if only_open else True
    lines: list[list[tuple[float, float, float]]] = []
    for want, u_name in ((want_left, "left"), (want_right, "right")):
        if not want:
            continue
        line: list[tuple[float, float, float]] = []
        for n in samples:
            tx = float(n.get("tx") or 1.0)
            ty = float(n.get("ty") or 0.0)
            left_u, right_u = bb.left_right_unit(tx, ty)
            u = left_u if u_name == "left" else right_u
            half = 0.5 * float(n.get("width") or 7.5) + 0.5 * width_extra_m
            if side in (u_name, "both"):
                bot_lat = half + open_extra_m
            else:
                bot_lat = half
            lat = bot_lat + max(0.0, foundation_m)
            z_bot = float(n["z_road"]) - foundation_m
            line.append(
                (
                    float(n["x"]) + lat * u[0],
                    float(n["y"]) + lat * u[1],
                    z_bot,
                )
            )
        if len(line) >= 2:
            lines.append(line)
    return lines


def _bake_lip_field(
    out: np.ndarray,
    *,
    size: int,
    extent: float,
    poly: list[tuple[float, float, float]],
    core_m: float,
    soft_m: float,
    max_delta_m: float,
    curve: str,
) -> tuple[int, int, float]:
    """Bake a continuous lip band around a dense edge polyline.

    Uses a temporary weight buffer over the local bbox and dense samples
    along the polyline (no sparse disk gaps). Falloff via ``_smooth_weight``.
    """
    if len(poly) < 1 or (core_m + soft_m) < 1e-9:
        return 0, 0, 0.0
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    margin = core_m + soft_m + 1.5
    px0, py0 = _to_px_beamng(min(xs) - margin, max(ys) + margin, size, extent)
    px1, py1 = _to_px_beamng(max(xs) + margin, min(ys) - margin, size, extent)
    c0 = max(0, int(math.floor(min(px0, px1))))
    c1 = min(size - 1, int(math.ceil(max(px0, px1))))
    r0 = max(0, int(math.floor(min(py0, py1))))
    r1 = min(size - 1, int(math.ceil(max(py0, py1))))
    if c1 < c0 or r1 < r0:
        return 0, 0, 0.0

    h = r1 - r0 + 1
    w = c1 - c0 + 1
    best_w = np.zeros((h, w), dtype=np.float32)
    best_z = np.zeros((h, w), dtype=np.float64)
    radius = core_m + soft_m
    rad_px = int(math.ceil(radius / max(extent / max(size - 1, 1), 1e-6))) + 2

    # Dense samples: keep poly as-is (already ~0.5 m) — paint each sample disk
    for x, y, z_tgt in poly:
        pc, pr = _to_px_beamng(x, y, size, extent)
        pc_i, pr_i = int(round(pc)), int(round(pr))
        for py in range(max(r0, pr_i - rad_px), min(r1, pr_i + rad_px) + 1):
            for px in range(max(c0, pc_i - rad_px), min(c1, pc_i + rad_px) + 1):
                bx, by = _from_px_beamng(float(px), float(py), size, extent)
                dist = math.hypot(bx - x, by - y)
                wt = _smooth_weight(dist, core_m, soft_m, curve)
                if wt <= 1e-6:
                    continue
                li = py - r0
                lj = px - c0
                if wt > best_w[li, lj]:
                    best_w[li, lj] = wt
                    best_z[li, lj] = z_tgt

    tested = 0
    changed = 0
    max_abs = 0.0
    for li in range(h):
        for lj in range(w):
            wt = float(best_w[li, lj])
            if wt <= 1e-6:
                continue
            tested += 1
            py = r0 + li
            px = c0 + lj
            z0 = float(out[py, px])
            z_tgt = float(best_z[li, lj])
            if abs(z0 - z_tgt) > max_delta_m:
                continue
            z_new = (1.0 - wt) * z0 + wt * z_tgt
            if abs(z_new - z0) < 1e-4:
                continue
            out[py, px] = z_new
            changed += 1
            max_abs = max(max_abs, abs(z_new - z0))
    return tested, changed, max_abs



def _stamp_embed_strip_holes(
    hole: np.ndarray,
    *,
    size: int,
    extent: float,
    lo_n: list[tuple[float, float, float, float, float]],
    up_n: list[tuple[float, float, float, float, float]],
    inset_m: float = 0.0,
) -> int:
    """Mark holemap cells whose *centers* lie strictly between lower/upper lips.

    No expansion past the lips — ``inset_m`` shrinks the strip from both edges
    so lip-touching neighbour cells stay solid (Stirn and open flank).
    """
    n = min(len(lo_n), len(up_n))
    if n < 2 or hole is None:
        return 0
    inset = max(0.0, float(inset_m))
    xs = [float(lo_n[i][0]) for i in range(n)] + [float(up_n[i][0]) for i in range(n)]
    ys = [float(lo_n[i][1]) for i in range(n)] + [float(up_n[i][1]) for i in range(n)]
    # Tight bbox — no pad (neighbour spill was the bug)
    px0, py0 = _to_px_beamng(min(xs), max(ys), size, extent)
    px1, py1 = _to_px_beamng(max(xs), min(ys), size, extent)
    c0 = max(0, int(math.floor(min(px0, px1))))
    c1 = min(size - 1, int(math.ceil(max(px0, px1))))
    r0 = max(0, int(math.floor(min(py0, py1))))
    r1 = min(size - 1, int(math.ceil(max(py0, py1))))
    if c1 < c0 or r1 < r0:
        return 0

    before = int((hole > 0).sum())
    for py in range(r0, r1 + 1):
        for px in range(c0, c1 + 1):
            if hole[py, px] > 0:
                continue
            bx, by = _from_px_beamng(float(px), float(py), size, extent)
            inside = False
            for i in range(n - 1):
                lx0, ly0 = float(lo_n[i][0]), float(lo_n[i][1])
                lx1, ly1 = float(lo_n[i + 1][0]), float(lo_n[i + 1][1])
                ux0, uy0 = float(up_n[i][0]), float(up_n[i][1])
                ux1, uy1 = float(up_n[i + 1][0]), float(up_n[i + 1][1])
                mx0 = 0.5 * (lx0 + ux0)
                my0 = 0.5 * (ly0 + uy0)
                mx1 = 0.5 * (lx1 + ux1)
                my1 = 0.5 * (ly1 + uy1)
                mdx, mdy = mx1 - mx0, my1 - my0
                mseg2 = mdx * mdx + mdy * mdy
                if mseg2 < 1e-12:
                    t = 0.0
                    tb = 0.0
                else:
                    tb = ((bx - mx0) * mdx + (by - my0) * mdy) / mseg2
                    if tb < -1e-3 or tb > 1.0 + 1e-3:
                        continue
                    t = max(0.0, min(1.0, tb))
                lx = lx0 + t * (lx1 - lx0)
                ly = ly0 + t * (ly1 - ly0)
                ux = ux0 + t * (ux1 - ux0)
                uy = uy0 + t * (uy1 - uy0)
                sx, sy = lx - ux, ly - uy
                sep = math.hypot(sx, sy)
                if sep < 1e-6:
                    continue
                # Distance from upper lip toward lower along the connector
                along = ((bx - ux) * sx + (by - uy) * sy) / sep
                if along <= inset or along >= sep - inset:
                    continue
                inside = True
                break
            if inside:
                hole[py, px] = 255
    return int((hole > 0).sum()) - before


# Back-compat alias
_stamp_portal_face_holes = _stamp_embed_strip_holes


def embed_terrain_lips_heightmap(
    galleries: list[dict],
    span_infos: list[dict],
    *,
    size: int,
    extent: float,
    defaults: dict,
    elev: np.ndarray,
    hole: np.ndarray | None = None,
) -> tuple[np.ndarray, dict]:
    """Planieren portal/opening lips via distance field + smooth falloff.

    Lower lip → ``z_road - foundation`` (Vorfeld / free hole tiles).
    Upper lip → ``z_roof_outer``.
    Optional: holemap strip between portal green/magenta faces.
    """
    out = elev.astype(np.float64).copy()
    info_by_oid = {info.get("objectid"): info for info in span_infos}
    interior_ends = _interior_portal_ends(span_infos)
    mpp = extent / max(size - 1, 1)
    tested = 0
    changed = 0
    max_abs = 0.0
    n_gal = 0
    hole_px = 0
    debug_lines: list[dict] = []

    def _acc(t: int, c: int, m: float) -> None:
        nonlocal tested, changed, max_abs
        tested += t
        changed += c
        max_abs = max(max_abs, m)

    for g in galleries:
        if not _gallery_flag(g, defaults, "terrain_embed", False):
            continue
        nodes = g.get("nodes") or []
        if len(nodes) < 2:
            continue
        n_gal += 1
        info = info_by_oid.get(g.get("objectid")) or {}
        upper_core, out_core, in_core, soft_m, foundation, lower_z = _embed_lip_radii(
            g, defaults, mpp=mpp
        )
        inset_raw = g.get("terrain_embed_upper_inset_m")
        if inset_raw is None:
            inset_raw = defaults.get("terrain_embed_upper_inset_m")
        upper_inset = float(inset_raw) if inset_raw is not None else float(mpp)
        upper_inset = max(float(mpp), upper_inset)  # at least one terrain cell
        max_delta = _gallery_num(g, defaults, "terrain_embed_max_delta_m", 12.0)
        do_open = _gallery_flag(g, defaults, "terrain_embed_open_side", True)
        do_portal_face = _gallery_flag(g, defaults, "terrain_embed_portal_face", False)
        do_portal_face_upper = _gallery_flag(
            g, defaults, "terrain_embed_portal_face_upper", False
        )
        do_portal_holes = _gallery_flag(g, defaults, "terrain_embed_portal_holes", False)
        do_upper = _gallery_flag(g, defaults, "terrain_embed_upper", False)
        if upper_core <= 1e-9:
            do_upper = False
        curve = str(
            g.get("terrain_embed_curve")
            or defaults.get("terrain_embed_curve")
            or "smoothstep"
        )
        overhang = _gallery_num(g, defaults, "overhang_m", 0.6)
        if g.get("overhang_m") is None and info.get("overhang_m") is not None:
            overhang = float(info["overhang_m"])
        open_extra = _gallery_num(g, defaults, "deck_open_extra_m", 0.8)
        width_extra = _gallery_num(g, defaults, "deck_width_extra_m", 0.0)
        open_side = str(
            g.get("open_side") or info.get("open_side") or defaults.get("open_side") or "left"
        ).lower().strip()
        portal_len = _gallery_num(g, defaults, "hole_portal_length_m", 4.0)
        out_bite = _gallery_num(g, defaults, "hole_out_bite_m", 2.0)

        portal_s = info.get("portal_s") or g.get("portal_s") or []
        if len(portal_s) >= 2:
            s_p0, s_p1 = float(portal_s[0]), float(portal_s[1])
        else:
            s_p0, s_p1 = float(nodes[0]["s"]), float(nodes[-1]["s"])
        if s_p1 < s_p0:
            s_p0, s_p1 = s_p1, s_p0

        # Longitudinal lips: open flank only — never hang/mountain side.
        # kind: portal_door = open side at portals (hole strip); open_mid = lookout.
        bands: list[tuple[list[dict], bool, str]] = []
        if open_side in ("left", "right", "both"):
            door = _portal_doorway_bands(
                nodes,
                {**info, "portal_s": [s_p0, s_p1]},
                mode="portals",
                portal_len=portal_len,
                out_bite=out_bite,
            )
            if not any(len(b) >= 1 for b in door):
                door = [
                    _nodes_in_s_window(nodes, s_p0 - out_bite, s_p0 + portal_len),
                    _nodes_in_s_window(nodes, s_p1 - portal_len, s_p1 + out_bite),
                ]
            for b in door:
                if len(b) >= 1:
                    bands.append((b, True, "portal_door"))
            if do_open:
                mid = _nodes_in_s_window(nodes, s_p0, s_p1)
                if len(mid) >= 2:
                    bands.append((mid, True, "open_mid"))

        oid = g.get("objectid")
        slug = bb._slug(str(g.get("name") or "gallery"))
        for bi, (band, only_open, band_kind) in enumerate(bands):
            found_lines = list(
                _foundation_edge_polylines_n(
                    band,
                    open_side=open_side,
                    open_extra_m=open_extra,
                    width_extra_m=width_extra,
                    foundation_m=foundation,
                    only_open=only_open,
                    step_m=1.0 if only_open else min(0.5, mpp),
                    z_offset_m=lower_z,
                )
            )
            roof_lines = list(
                _roof_edge_polylines_n(
                    band,
                    open_side=open_side,
                    overhang_m=overhang,
                    open_extra_m=open_extra,
                    width_extra_m=width_extra,
                    only_open=only_open,
                    step_m=1.0 if only_open else min(0.5, mpp),
                    foundation_m=foundation,
                    inset_m=upper_inset,
                )
            )
            for li, line_n in enumerate(found_lines):
                _acc(
                    *_bake_lip_field_sided(
                        out,
                        size=size,
                        extent=extent,
                        poly=line_n,
                        out_core_m=out_core,
                        in_core_m=in_core,
                        soft_m=soft_m,
                        max_delta_m=max_delta,
                        curve=curve,
                    )
                )
                debug_lines.append(
                    {
                        "name": f"embed_found_{slug}_{oid}_b{bi}_s{li}",
                        "objectid": oid,
                        "kind": "foundation_outer",
                        "points": [(p[0], p[1], p[2]) for p in line_n],
                    }
                )
            for li, roof_n in enumerate(roof_lines):
                debug_lines.append(
                    {
                        "name": f"embed_roof_{slug}_{oid}_b{bi}_s{li}",
                        "objectid": oid,
                        "kind": "roof_outer",
                        "points": [(p[0], p[1], p[2]) for p in roof_n],
                    }
                )
                if do_upper:
                    _acc(
                        *_bake_lip_field_sided(
                            out,
                            size=size,
                            extent=extent,
                            poly=roof_n,
                            out_core_m=upper_core,
                            in_core_m=0.0,
                            soft_m=soft_m,
                            max_delta_m=max_delta,
                            curve=curve,
                        )
                    )
            # Open flank: hole strip between green/magenta along full lookout
            # (portal_door + open_mid). Hang side never.
            if (
                do_portal_holes
                and hole is not None
                and band_kind in ("portal_door", "open_mid")
            ):
                for lo_n, up_n in zip(found_lines, roof_lines):
                    hole_px += _stamp_embed_strip_holes(
                        hole,
                        size=size,
                        extent=extent,
                        lo_n=lo_n,
                        up_n=up_n,
                        inset_m=0.05 * mpp,
                    )

        # Portal faces (cross-road lintel) — opt-in; default off
        if do_portal_face:
            for s_p, into, end_tag in (
                (s_p0, -1.0, "s0"),
                (s_p1, +1.0, "s1"),
            ):
                if (oid, end_tag) in interior_ends:
                    continue
                n = _node_nearest_s(nodes, s_p)
                face_out = max(out_bite * 0.5, out_core * 0.5, mpp)
                lo_n = _portal_face_polyline_n(
                    n,
                    into_sign=into,
                    out_m=face_out,
                    open_extra_m=open_extra,
                    width_extra_m=width_extra,
                    foundation_m=foundation,
                    overhang_m=overhang,
                    z_offset_m=lower_z,
                )
                _acc(
                    *_bake_lip_field_sided(
                        out,
                        size=size,
                        extent=extent,
                        poly=lo_n,
                        out_core_m=out_core,
                        in_core_m=in_core,
                        soft_m=soft_m,
                        max_delta_m=max_delta,
                        curve=curve,
                    )
                )
                debug_lines.append(
                    {
                        "name": f"embed_found_{slug}_{oid}_face{'0' if into < 0 else '1'}",
                        "objectid": oid,
                        "kind": "foundation_portal_face",
                        "points": [(p[0], p[1], p[2]) for p in lo_n],
                    }
                )
                need_up = (do_portal_face_upper and do_upper and upper_core > 1e-9) or (
                    do_portal_holes and hole is not None
                )
                up_n = None
                if need_up:
                    up_n = _portal_face_upper_polyline_n(
                        n,
                        into_sign=into,
                        out_m=face_out,
                        open_extra_m=open_extra,
                        width_extra_m=width_extra,
                        foundation_m=foundation,
                        overhang_m=overhang,
                    )
                if do_portal_face_upper and do_upper and upper_core > 1e-9 and up_n:
                    debug_lines.append(
                        {
                            "name": f"embed_roof_{slug}_{oid}_face{'0' if into < 0 else '1'}",
                            "objectid": oid,
                            "kind": "roof_portal_face",
                            "points": [(p[0], p[1], p[2]) for p in up_n],
                        }
                    )
                    _acc(
                        *_bake_lip_field_sided(
                            out,
                            size=size,
                            extent=extent,
                            poly=up_n,
                            out_core_m=0.0,
                            in_core_m=upper_core,
                            soft_m=soft_m,
                            max_delta_m=max_delta,
                            curve=curve,
                        )
                    )
                if do_portal_holes and hole is not None:
                    # Carriageway-only span: bake/debug lips reach into the hillside
                    # (foundation+overhang); punching that full strip deleted slope
                    # cells that appear "above" the visible upper lip.
                    half_road = (
                        0.5 * float(n.get("width") or 7.5) + 0.5 * width_extra
                    )
                    hole_lat = half_road + 0.5 * mpp
                    lo_h = _portal_face_polyline_n(
                        n,
                        into_sign=into,
                        out_m=face_out,
                        open_extra_m=0.0,
                        width_extra_m=width_extra,
                        foundation_m=foundation,
                        overhang_m=0.0,
                        z_offset_m=lower_z,
                        lat_span_m=hole_lat,
                    )
                    up_h = _portal_face_upper_polyline_n(
                        n,
                        into_sign=into,
                        out_m=face_out,
                        open_extra_m=0.0,
                        width_extra_m=width_extra,
                        foundation_m=foundation,
                        overhang_m=0.0,
                        lat_span_m=hole_lat,
                    )
                    hole_px += _stamp_embed_strip_holes(
                        hole,
                        size=size,
                        extent=extent,
                        lo_n=lo_h,
                        up_n=up_h,
                        inset_m=0.35 * mpp,
                    )

    stats = {
        "galleries": n_gal,
        "tested": tested,
        "changed": changed,
        "max_delta_m": round(max_abs, 3),
        "method": "terrain_embed_distance_field",
        "curve": str(defaults.get("terrain_embed_curve") or "smoothstep"),
        "upper_enabled": bool(defaults.get("terrain_embed_upper", False)),
        "portal_face": bool(defaults.get("terrain_embed_portal_face", False)),
        "portal_face_upper": bool(
            defaults.get("terrain_embed_portal_face_upper", False)
        ),
        "portal_holes": bool(defaults.get("terrain_embed_portal_holes", False)),
        "portal_hole_pixels": int(hole_px),
        "upper_m": defaults.get("terrain_embed_upper_m"),
        "lower_m": defaults.get("terrain_embed_lower_m"),
        "in_m": defaults.get("terrain_embed_in_m"),
        "soft_m": float(defaults.get("terrain_embed_soft_m") or 1.0),
        "rings": int(defaults.get("terrain_embed_rings") or 2),
        "foundation_m": float(defaults.get("terrain_embed_foundation_m") or 1.0),
        "lower_z_m": float(defaults.get("terrain_embed_lower_z_m") or 0.0),
        "debug_foundation_lines": debug_lines,
    }
    if n_gal:
        in_show = defaults.get("terrain_embed_in_m")
        in_lbl = in_show if in_show is not None else "~foundation"
        hole_note = f", portal_holes={hole_px}px" if hole_px else ""
        print(
            f"Terrain embed: galleries={n_gal} tested={tested} changed={changed} "
            f"max_delta={max_abs:.3f}m (lower out={defaults.get('terrain_embed_lower_m')} "
            f"in={in_lbl}"
            f"{'+upper' if defaults.get('terrain_embed_upper') else ''}"
            f"{hole_note}, "
            f"curve={stats['curve']})"
        )
    return out, stats


def write_terrain_embed_assets(
    proc: Path,
    level_name: str,
    *,
    elev: np.ndarray,
    dgm: np.ndarray,
    max_height_m: float,
    hole: np.ndarray | None = None,
    tag: str = "gallery_embed",
) -> Path:
    """Write gallery replace layer from bake-vs-DGM, compose, sync holemap."""
    import heightmap_layers as hml

    _ = tag
    size = int(elev.shape[0])
    z_tgt, weight = hml.proposal_from_diff(dgm, elev)
    hml.write_span_part(proc, "gallery", z_tgt, weight, max_h=max_height_m, size=size)
    carved = hml.compose(proc, size=size, max_h=max_height_m, level_name=level_name)

    if hole is not None:
        write_hole_assets(proc, level_name, hole)
    return carved


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
    bands = [
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
    fake = info.get("fake_end")
    if info.get("one_ended") and fake in ("s0", "s1"):
        return [bands[0]] if fake == "s1" else [bands[1]]
    return bands


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
                0.5 * max(float(n.get("width") or 7.5) for n in band) * width_scale
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
                    dist, z_herm, _hw = proj[0], proj[1], proj[2]
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


def make_embed_debug_meshroad(
    name: str,
    points: list[tuple[float, float, float]],
    *,
    material: str,
    lift_m: float = 0.0,
    width_m: float = 0.25,
) -> dict:
    """Thin MeshRoad debug lip. Parent: gallery_embed_debug. lift_m must stay 0."""
    mesh_nodes = []
    for i, (x, y, z) in enumerate(points):
        if i + 1 < len(points):
            tx = points[i + 1][0] - x
            ty = points[i + 1][1] - y
        elif i > 0:
            tx = x - points[i - 1][0]
            ty = y - points[i - 1][1]
        else:
            tx, ty = 1.0, 0.0
        _ = (tx, ty)
        mesh_nodes.append(
            [
                round(float(x), 3),
                round(float(y), 3),
                round(float(z) + lift_m, 3),
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
        "__parent": "gallery_embed_debug",
        "topMaterial": material,
        "bottomMaterial": material,
        "sideMaterial": material,
        "textureLength": 4,
        "breakAngle": 3,
        "widthSubdivisions": 0,
        "nodes": mesh_nodes,
    }


def make_embed_foundation_meshroad(
    name: str,
    points: list[tuple[float, float, float]],
    *,
    lift_m: float = 0.0,
    width_m: float = 0.25,
) -> dict:
    """Thin green MeshRoad on the foundation outer lip (debug)."""
    return make_embed_debug_meshroad(
        name, points, material="GalleryEmbedFound", lift_m=lift_m, width_m=width_m
    )


def ensure_embed_debug_materials(user_level: Path, level_name: str) -> None:
    mats_path = user_level / "art" / "road" / "main.materials.json"
    mats_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if mats_path.is_file() and mats_path.stat().st_size:
        try:
            data = json.loads(mats_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    data["GalleryEmbedFound"] = {
        "name": "GalleryEmbedFound",
        "mapTo": "GalleryEmbedFound",
        "class": "Material",
        "persistentId": "eeb06c11-0000-4e1e-9c11-0000eeb06c11",
        "Stages": [
            {
                "baseColorFactor": [0.1, 1.0, 0.25, 1.0],
                "roughnessFactor": 0.6,
                "emissiveFactor": [0.05, 0.45, 0.1],
            },
            {},
            {},
            {},
        ],
        "materialTag0": "RoadAndPath",
        "materialTag1": "beamng",
        "version": 1.5,
    }
    # Magenta — roof outer lip (upper bake target); strong emissive for WE visibility
    data["GalleryEmbedRoof"] = {
        "name": "GalleryEmbedRoof",
        "mapTo": "GalleryEmbedRoof",
        "class": "Material",
        "persistentId": "eeb06c22-0000-4e1e-9c22-0000eeb06c22",
        "Stages": [
            {
                "baseColorFactor": [1.0, 0.0, 1.0, 1.0],
                "roughnessFactor": 0.4,
                "emissiveFactor": [1.0, 0.0, 0.9],
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


def ensure_embed_foundation_material(user_level: Path, level_name: str) -> None:
    ensure_embed_debug_materials(user_level, level_name)

# Core + optional child folders under level_objects; used to heal wiped NDJSON lists.
_LEVEL_OBJECTS_CORE = (
    ("terrain", {"name": "terrain", "class": "SimGroup", "__parent": "level_objects"}),
    (
        "vegetation",
        {
            "name": "vegetation",
            "class": "SimGroup",
            "persistentId": "c520cc7b-0b9e-4ac5-8afb-6999aada3e25",
            "__parent": "level_objects",
        },
    ),
    (
        "Water",
        {
            "name": "Water",
            "class": "SimGroup",
            "persistentId": "c1f4830d-6f16-4679-955e-6def276090ed",
            "__parent": "level_objects",
        },
    ),
    (
        "sky_and_sun",
        {
            "name": "sky_and_sun",
            "class": "SimGroup",
            "persistentId": "ac783c6a-45b2-46eb-9db2-2320356db9a9",
            "__parent": "level_objects",
        },
    ),
)


def _register_level_objects_simgroup(user_level: Path, name: str) -> None:
    """Ensure a SimGroup entry exists in level_objects/items.level.json (NDJSON)."""
    lo_dir = user_level / "main" / "MissionGroup" / "level_objects"
    lo_items = lo_dir / "items.level.json"
    lines: list[str] = []
    if lo_items.is_file() and lo_items.stat().st_size:
        lines = [ln for ln in lo_items.read_text(encoding="utf-8").splitlines() if ln.strip()]
    names: set[str | None] = set()
    kept: list[str] = []
    for ln in lines:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            # Drop leftover pretty-print / array fragments from older broken writes.
            continue
        if not isinstance(obj, dict):
            continue
        n = obj.get("name")
        if n in names:
            continue
        names.add(n)
        kept.append(json.dumps(obj, separators=(",", ":")))

    # Heal missing core groups if their folders exist (previous buggy writes wiped them).
    for folder, obj in _LEVEL_OBJECTS_CORE:
        if obj["name"] in names:
            continue
        if (lo_dir / folder).is_dir():
            kept.insert(
                0 if folder == "terrain" else len(kept),
                json.dumps(obj, separators=(",", ":")),
            )
            names.add(obj["name"])
            print(f"Restored SimGroup {obj['name']} under level_objects")

    for folder in ("bridges", "roads", "guardrails", "galleries"):
        if folder in names:
            continue
        if (lo_dir / folder).is_dir():
            kept.append(
                json.dumps(
                    {
                        "name": folder,
                        "class": "SimGroup",
                        "__parent": "level_objects",
                        "enabled": "1",
                    },
                    separators=(",", ":"),
                )
            )
            names.add(folder)
            print(f"Restored SimGroup {folder} under level_objects")

    if name not in names:
        kept.append(
            json.dumps(
                {
                    "name": name,
                    "class": "SimGroup",
                    "__parent": "level_objects",
                    "enabled": "1",
                },
                separators=(",", ":"),
            )
        )
        print(f"Registered SimGroup {name} under level_objects")
    lo_items.parent.mkdir(parents=True, exist_ok=True)
    with lo_items.open("w", encoding="utf-8", newline="\n") as f:
        for ln in kept:
            f.write(ln + "\n")


def clear_gallery_debug_artifacts(level_name: str) -> None:
    """Drop leftover deck-profile / centerline debug (TSStatics, DAE, materials)."""
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        return
    group_dir = (
        user_level / "main" / "MissionGroup" / "level_objects" / "gallery_debug"
    )
    items = group_dir / "items.level.json"
    if items.is_file():
        items.write_text("", encoding="utf-8")
    shapes = user_level / "art" / "shapes" / "galleries"
    n_dae = 0
    if shapes.is_dir():
        for f in shapes.glob("debug_*.dae"):
            f.unlink()
            n_dae += 1
    mats_path = shapes / "main.materials.json"
    n_mat = 0
    if mats_path.is_file() and mats_path.stat().st_size:
        try:
            data = json.loads(mats_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            drop = [k for k in data if str(k).startswith("GalleryDebug")]
            for k in drop:
                data.pop(k, None)
                n_mat += 1
            if drop:
                mats_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    lo_items = user_level / "main" / "MissionGroup" / "level_objects" / "items.level.json"
    if lo_items.is_file() and lo_items.stat().st_size:
        kept: list[str] = []
        for ln in lo_items.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                obj = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("name") == "gallery_debug":
                continue
            kept.append(json.dumps(obj, separators=(",", ":")))
        lo_items.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    print(
        f"Cleared gallery_debug (dae={n_dae} GalleryDebug-mats={n_mat}) -> {items}"
    )


def clear_embed_foundation_debug(level_name: str) -> None:
    """Remove previous foundation-lip debug MeshRoads (SimGroup gallery_embed_debug)."""
    user_level = USER_LEVELS / level_name
    group_dir = (
        user_level / "main" / "MissionGroup" / "level_objects" / "gallery_embed_debug"
    )
    items = group_dir / "items.level.json"
    if items.is_file():
        items.write_text("", encoding="utf-8")
        print(f"Cleared gallery_embed_debug -> {items}")


def inject_embed_foundation_debug(
    level_name: str,
    lines: list[dict],
) -> Path | None:
    """Write/replace embed lip debug MeshRoads under gallery_embed_debug.

    kind foundation_outer → green; roof_outer / roof_portal_face → magenta.
    Z is exact bake target (lift_m=0).
    """
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        return None
    group_dir = (
        user_level / "main" / "MissionGroup" / "level_objects" / "gallery_embed_debug"
    )
    group_dir.mkdir(parents=True, exist_ok=True)
    items_path = group_dir / "items.level.json"
    if not lines:
        items_path.write_text("", encoding="utf-8")
        print("gallery_embed_debug: no lines (cleared)")
        return items_path

    ensure_embed_debug_materials(user_level, level_name)
    entries = []
    n_found = 0
    n_roof = 0
    n_face = 0
    for line in lines:
        pts = line.get("points") or []
        if len(pts) < 2:
            continue
        kind = str(line.get("kind") or "foundation_outer")
        if "portal_face" in kind or "_face" in str(line.get("name") or ""):
            n_face += 1
        if kind.startswith("roof"):
            mat = "GalleryEmbedRoof"
            width = 0.8  # thicker — often coplanar above green / near shell
            n_roof += 1
        else:
            mat = "GalleryEmbedFound"
            width = 0.3
            n_found += 1
        entries.append(
            make_embed_debug_meshroad(
                str(line["name"]), pts, material=mat, lift_m=0.0, width_m=width
            )
        )
    with items_path.open("w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")

    _register_level_objects_simgroup(user_level, "gallery_embed_debug")
    print(
        f"Injected {len(entries)} embed debug MeshRoads "
        f"(green={n_found}, magenta={n_roof}, portal_face~{n_face}) -> {items_path}"
    )
    return items_path


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


def _gallery_family_key(name: str) -> str | None:
    """gallery_deck_landecker_227551 / gallery_portal_…_s1 → landecker_227551.

    Ceiling lamps/fans use ``gallery_{slug}_{oid}__lamp_000``; the ``__`` suffix
    is stripped so ``--only`` replace still drops the previous fitout.
    """
    n = str(name or "")
    for pfx in (
        "gallery_portal_collar_",
        "gallery_portal_",
        "gallery_deck_",
        "gallery_",
    ):
        if n.startswith(pfx):
            rest = n[len(pfx) :]
            if rest.endswith("_s0") or rest.endswith("_s1"):
                rest = rest[:-3]
            if "__" in rest:
                rest = rest.split("__", 1)[0]
            return rest
    return None


def _gallery_families_with_siblings(
    entries: list[dict], sibling_oids: set[int] | None = None
) -> set[str]:
    """Replace keys for this inject, plus leftover OIDs of a merged gallery.

    ``--only 3699`` writes ``Lermooser_Tunnel_3699``. A previous unmerged
    ``Lermooser_Tunnel_3842`` must go away with the same merge group.
    """
    families = {
        k for e in entries if (k := _gallery_family_key(str(e.get("name") or "")))
    }
    if not sibling_oids:
        return families
    extra: set[str] = set()
    for fam in families:
        parts = fam.rsplit("_", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        slug = parts[0]
        for oid in sibling_oids:
            extra.add(f"{slug}_{int(oid)}")
    return families | extra


def _cleanup_stale_gallery_dae(
    user_level: Path,
    proc: Path,
    stale_families: set[str],
) -> int:
    """Delete DAEs whose family key is a replaced merge sibling."""
    if not stale_families:
        return 0
    removed = 0
    for folder in (
        user_level / "art" / "shapes" / "galleries",
        proc / "gallery_meshes",
    ):
        if not folder.is_dir():
            continue
        for f in folder.glob("*.dae"):
            key = _gallery_family_key(f.stem)
            if key not in stale_families:
                continue
            f.unlink()
            removed += 1
            print(f"Removed leftover gallery mesh {f.name}")
    return removed


def _cleanup_orphan_gallery_dae(
    user_level: Path,
    proc: Path,
    entries: list[dict],
) -> int:
    """Delete gallery DAEs that the current inject no longer references."""
    keep: set[str] = set()
    for e in entries:
        shape = str(e.get("shapeName") or "")
        if shape:
            keep.add(Path(shape).stem)
        n = str(e.get("name") or "")
        if n:
            keep.add(n)
    keep.update(
        (
            "tunnel_lamp",
            "tunnel_jetfan",
            "tunnel_jetfan_rotor_cw",
            "tunnel_jetfan_rotor_ccw",
        )
    )
    removed = 0
    for folder in (
        user_level / "art" / "shapes" / "galleries",
        proc / "gallery_meshes",
    ):
        if not folder.is_dir():
            continue
        for f in folder.glob("*.dae"):
            if f.stem in keep:
                continue
            f.unlink()
            removed += 1
            print(f"Removed leftover gallery mesh {f.name}")
    return removed


def inject_gallery_tsstatics(
    level_name: str,
    entries: list[dict],
    *,
    merge: bool = False,
    sibling_oids: set[int] | None = None,
    proc: Path | None = None,
) -> Path | None:
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        print(f"Level folder missing: {user_level}")
        return None
    group_dir = user_level / "main" / "MissionGroup" / "level_objects" / "galleries"
    group_dir.mkdir(parents=True, exist_ok=True)
    items_path = group_dir / "items.level.json"
    written = list(entries)
    dropped_families: set[str] = set()
    if merge and items_path.is_file() and items_path.stat().st_size:
        old: list[dict] = []
        for ln in items_path.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                old.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
        families = _gallery_families_with_siblings(entries, sibling_oids)
        current = {
            k for e in entries if (k := _gallery_family_key(str(e.get("name") or "")))
        }
        dropped_families = families - current
        kept = [
            e
            for e in old
            if _gallery_family_key(str(e.get("name") or "")) not in families
        ]
        written = kept + list(entries)
        print(
            f"Gallery inject merge: kept {len(kept)} replaced {len(entries)}"
            + (f" dropped_siblings={sorted(dropped_families)}" if dropped_families else "")
        )
    with items_path.open("w", encoding="utf-8", newline="\n") as f:
        for e in written:
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
    if dropped_families:
        n_rm = _cleanup_stale_gallery_dae(
            user_level, proc or (user_level / "_none"), dropped_families
        )
        if n_rm:
            print(f"Removed {n_rm} leftover sibling gallery DAE(s)")
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
    ap.add_argument(
        "--skip-holemap",
        action="store_true",
        help="Do not read/write/sync holemap (keep existing BeamNG import hole maps)",
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
    from gip_road_segments import apply_gallery_unify, road_from_unify_feature

    feats = apply_gallery_unify(feats, site)
    if not feats:
        raise SystemExit("No gallery/tunnel features after unify")

    if only_ids:
        feats = [
            f
            for f in feats
            if _feat_objectids(f) & only_ids
        ]
        if not feats:
            raise SystemExit(f"No features matched --only {sorted(only_ids)}")

    use_spline_default = _gallery_uses_road_spline(defaults)
    cl = str(defaults.get("centerline") or "").lower().strip()
    use_gip_cl = cl in ("gip", "verkehrswege", "objectid", "oid")
    need_spline_road = use_spline_default
    if not need_spline_road or not use_gip_cl:
        for it in items:
            it_cl = str(it.get("centerline") or "").lower()
            if it_cl in ("gip", "verkehrswege", "objectid", "oid"):
                use_gip_cl = True
            if str(it.get("profile") or it.get("z_profile") or "").lower() == "road_spline":
                need_spline_road = True
            if it_cl and _gallery_uses_road_spline(it):
                need_spline_road = True

    if use_gip_cl:
        from gip_road_segments import (
            load_gip_corridors,
            load_gip_corridor_for_feature,
            load_gip_polylines_for_decals,
        )

        roads = load_gip_polylines_for_decals(site)
        print(f"Gallery roads centerline=gip segments={len(roads)}")

    corridors = None
    net_road = None
    if need_spline_road:
        if use_gip_cl:
            corridors = load_gip_corridors(site)
        else:
            # road_spline historically defaults to Strassennetz when centerline unset/osm
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
    z_road_at = _surface_road_z_fn(site)

    for feat in feats:
        cfg = resolve_gallery_cfg(defaults, items, feat)
        if cfg.get("enabled") is False:
            print(f"skip oid={feat.get('objectid')} (enabled: false)")
            continue
        feat = apply_gallery_span_trims(feat, cfg, site, sc)

        spline = _gallery_uses_road_spline(cfg)
        strip_entries: list[dict] = []
        unify_road = None
        if feat.get("unify_id"):
            w = float(cfg.get("width_m") or 7.5)
            unify_road = road_from_unify_feature(feat, width_m=w, z_at=z_terrain)
        if spline:
            if unify_road is not None:
                road = unify_road
            elif use_gip_cl:
                road = load_gip_corridor_for_feature(feat, site, corridors=corridors)
            elif net_road is not None:
                road = net_road
            else:
                cl_f = str(cfg.get("centerline") or defaults.get("centerline") or "osm").lower()
                if cl_f in ("gip", "verkehrswege", "objectid", "oid"):
                    from gip_road_segments import load_gip_corridor_for_feature

                    road = load_gip_corridor_for_feature(feat, site, corridors=corridors)
                else:
                    road = bb.load_strassennetz_road(proc)
            nodes, strip_entries, info = build_gallery_road_spline(
                feat, road, z_terrain, cfg
            )
            info["seed_road_id"] = road.get("id")
            info["corridor_id"] = road.get("id")
            info["corridor_kind"] = road.get("corridor_kind")
            info["chain_ids"] = None
            info["chain_len_m"] = round(float(road["length"]), 2)
        else:
            if unify_road is not None:
                road = unify_road
                seed = {"id": road.get("id")}
            elif use_gip_cl or str(cfg.get("centerline") or "").lower() in (
                "gip",
                "verkehrswege",
                "objectid",
                "oid",
            ):
                road = load_gip_corridor_for_feature(
                    feat, site, corridors=corridors
                )
                seed = {"id": road.get("id")}
            else:
                if not roads:
                    raise SystemExit(
                        f"Missing {proc / 'roads_beamng.json'} (needed for hermite galleries)"
                    )
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
        info["unify_id"] = feat.get("unify_id")
        info["objectid"] = feat.get("objectid")
        info["merged_from"] = feat.get("merged_from")
        info["kind"] = feat["kind"]
        info["gip_length_m"] = feat.get("length_m")
        info["match"] = cfg.get("match_id")
        if str(cfg.get("append_unify") or "").strip() and cfg.get("one_ended"):
            nodes = append_one_ended_abutment(
                nodes, info, cfg, site, z_terrain=z_terrain, z_road_at=z_road_at
            )
        else:
            nodes = append_daylight_deck_approach(
                nodes, info, cfg, z_terrain=z_terrain, z_road_at=z_road_at
            )
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
            # Closed tunnel: loft walls/roof along the full bore (MeshRoad span).
            # Open galleries keep the short portal-collar shell when one_ended.
            if feat.get("kind") == "tunnel" or str(cfg.get("open_side") or "") == "none":
                s_m0, s_m1 = s_p0, s_p1
            else:
                shell = info.get("shell_s") or (s_p0, s_p1)
                s_m0, s_m1 = float(shell[0]), float(shell[1])
            if s_m1 < s_m0:
                s_m0, s_m1 = s_m1, s_m0
            mesh_nodes = [n for n in nodes if s_m0 - 1e-6 <= float(n["s"]) <= s_m1 + 1e-6]
            if len(mesh_nodes) < 2:
                mesh_nodes = nodes

            # --- shell (walls + roof + open-side parapet/columns) ---
            col_style = str(
                (cfg.get("style") or {}).get("columns") or "rect"
            ).lower()
            open_side = str(cfg.get("open_side") or "left")
            build_parapet = (
                col_style not in ("none", "off", "false", "0")
                and open_side not in ("none", "off", "false", "0")
            )
            verts, faces, origin = loft_gallery_shell(
                mesh_nodes,
                open_side=open_side,
                wall_t=float(cfg.get("wall_thickness_m") or 0.4),
                overhang_m=float(cfg.get("overhang_m") or 0.6),
                clear_width_m=(
                    float(cfg["clear_width_m"])
                    if cfg.get("clear_width_m") is not None
                    else None
                ),
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
                wall_radius_m=(
                    float(cfg["wall_radius_m"])
                    if cfg.get("wall_radius_m") is not None
                    else None
                ),
                wall_arc_segments=int(cfg.get("wall_arc_segments") or 10),
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
                    d0, d1 = s_p0 - deck_ext, s_p1 + deck_ext
                    if info.get("one_ended"):
                        if info.get("fake_end") == "s1":
                            d1 = s_p1
                        elif info.get("fake_end") == "s0":
                            d0 = s_p0
                    if info.get("abutment_end_s") is not None:
                        s_ab = float(info["abutment_end_s"])
                        d0 = min(d0, s_ab)
                        d1 = max(d1, s_ab)
                    deck_nodes = [
                        n
                        for n in nodes
                        if d0 - 1e-6 <= float(n["s"]) <= d1 + 1e-6
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
                src_nodes = nodes if info.get("one_ended") else mesh_nodes
                n0 = _node_nearest_s(src_nodes, s_p0)
                n1 = _node_nearest_s(src_nodes, s_p1)
                portal_ends = [
                    ("s0", n0, +1.0),
                    ("s1", n1, -1.0),
                ]
                fake = info.get("fake_end")
                if info.get("one_ended") and fake in ("s0", "s1"):
                    portal_ends = [e for e in portal_ends if e[0] != fake]
                for end_name, node, into_sign in portal_ends:
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
                        clear_width_m=cfg.get("clear_width_m"),
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

            # Ceiling lamps + jet fans: closed tubes longer than fitout_min_m.
            fitout_stats = {"lamps": 0, "lights": 0, "fans": 0, "rotors": 0}
            bore_m = abs(s_p1 - s_p0)
            if _fitout_wanted(feat, cfg, bore_m):
                lamp_vfs, fan_vfs, rotor_cw_vfs, rotor_ccw_vfs = write_tunnel_fitout_shapes(
                    proc_dir=shapes_dir_proc,
                    user_level=user_level,
                    level_name=level_name,
                    diameter_m=float(cfg.get("fan_diameter_m") or 0.60),
                    length_m=float(cfg.get("fan_length_m") or 1.50),
                    wall_m=float(cfg.get("fan_wall_m") or 0.06),
                    fan_rpm=float(cfg.get("fan_rpm") if cfg.get("fan_rpm") is not None else 60.0),
                    fan_blades=int(cfg.get("fan_blades") or 6),
                )
                extra, fitout_stats = build_tunnel_fitout_entries(
                    nodes=mesh_nodes,
                    s_p0=s_p0,
                    s_p1=s_p1,
                    cfg=cfg,
                    slug=slug,
                    oid=oid,
                    lamp_vfs=lamp_vfs,
                    fan_vfs=fan_vfs,
                    rotor_cw_vfs=rotor_cw_vfs,
                    rotor_ccw_vfs=rotor_ccw_vfs,
                )
                ts_entries.extend(extra)
                print(
                    f"  fitout oid={oid} bore={bore_m:.1f}m "
                    f"lamps={fitout_stats['lamps']} lights={fitout_stats['lights']} "
                    f"fans={fitout_stats['fans']}"
                )

        if args.holes_only:
            fitout_stats = {"lamps": 0, "lights": 0, "fans": 0, "rotors": 0}

        centerlines.append(
            {
                "name": feat["name"],
                "objectid": feat.get("objectid"),
                "merged_from": feat.get("merged_from"),
                "kind": feat["kind"],
                "fitout": fitout_stats,
                "open_side": cfg.get("open_side"),
                "clear_height_m": cfg["clear_height_m"],
                "hole_mode": cfg.get("hole_mode"),
                "hole_pad_m": cfg.get("hole_pad_m"),
                "hole_portal_length_m": cfg.get("hole_portal_length_m"),
                "debug_centerline": cfg.get("debug_centerline", True),
                "portal_s": info.get("portal_s"),
                "shell_s": info.get("shell_s"),
                "one_ended": info.get("one_ended"),
                "fake_end": info.get("fake_end"),
                "fake_end_drop_m": info.get("fake_end_drop_m"),
                "append_unify": info.get("append_unify"),
                "append_m": info.get("append_m"),
                "abutment_s": info.get("abutment_s"),
                "abutment_end_s": info.get("abutment_end_s"),
                "abutment_z": info.get("abutment_z"),
                "abut_run_m": info.get("abut_run_m"),
                "abutment_len_m": info.get("abutment_len_m"),
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
                "approach_conform_max_cut_m": cfg.get("approach_conform_max_cut_m"),
                "approach_conform_max_raise_m": cfg.get("approach_conform_max_raise_m"),
                "approach_conform_range_m": cfg.get("approach_conform_range_m"),
                "approach_conform_pull_m": cfg.get("approach_conform_pull_m"),
                "approach_conform_side_cut": cfg.get("approach_conform_side_cut"),
                "force_deck_z": cfg.get("force_deck_z"),
                "force_deck_z_sink_m": cfg.get("force_deck_z_sink_m"),
                "force_deck_z_fill": cfg.get("force_deck_z_fill"),
                "deck_depth_m": cfg.get("deck_depth_m"),
                "terrain_roof": cfg.get("terrain_roof") or "none",
                "terrain_embed": cfg.get("terrain_embed"),
                "terrain_embed_rings": cfg.get("terrain_embed_rings"),
                "terrain_embed_upper_m": cfg.get("terrain_embed_upper_m"),
                "terrain_embed_upper_inset_m": cfg.get("terrain_embed_upper_inset_m"),
                "terrain_embed_lower_m": cfg.get("terrain_embed_lower_m"),
                "terrain_embed_in_m": cfg.get("terrain_embed_in_m"),
                "terrain_embed_soft_m": cfg.get("terrain_embed_soft_m"),
                "terrain_embed_curve": cfg.get("terrain_embed_curve"),
                "terrain_embed_foundation_m": cfg.get("terrain_embed_foundation_m"),
                "terrain_embed_lower_z_m": cfg.get("terrain_embed_lower_z_m"),
                "terrain_embed_max_delta_m": cfg.get("terrain_embed_max_delta_m"),
                "terrain_embed_open_side": cfg.get("terrain_embed_open_side"),
                "terrain_embed_portal_face": cfg.get("terrain_embed_portal_face"),
                "terrain_embed_portal_face_upper": cfg.get(
                    "terrain_embed_portal_face_upper"
                ),
                "terrain_embed_portal_holes": cfg.get("terrain_embed_portal_holes"),
                "terrain_embed_debug": cfg.get("terrain_embed_debug"),
                "terrain_embed_upper": cfg.get("terrain_embed_upper"),
                "clear_width_m": cfg.get("clear_width_m"),
                "portal_seat": cfg.get("portal_seat"),
                "portal_apron_m": cfg.get("portal_apron_m"),
                "portal_roof_force_m": cfg.get("portal_roof_force_m"),
                "portal_roof_force_len_m": cfg.get("portal_roof_force_len_m"),
                "portal_hole": cfg.get("portal_hole"),
                "portal_hole_in_m": cfg.get("portal_hole_in_m"),
                "portal_hole_out_m": cfg.get("portal_hole_out_m"),
                "portal_apron_pad_m": cfg.get("portal_apron_pad_m"),
                "wall_thickness_m": cfg.get("wall_thickness_m"),
                "wall_radius_m": cfg.get("wall_radius_m"),
                "overhang_m": cfg.get("overhang_m"),
                "deck_open_extra_m": cfg.get("deck_open_extra_m"),
                "roof_thickness_m": cfg.get("roof_thickness_m"),
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
            + (
                f"one_ended={info.get('fake_end')} drop={info.get('fake_end_drop_m')} "
                if info.get("one_ended")
                else ""
            )
            + (
                f"abut={info.get('abutment_len_m')}m z={info.get('abutment_z')} "
                if info.get("abutment_end_s") is not None
                else ""
            )
            + (
                f"wall_r={float(cfg['wall_radius_m']):.2f}m "
                f"bulge={_arc_sagitta_m(float(cfg['wall_radius_m']), float(cfg.get('clear_height_m') or 4.0)):.2f}m "
                if cfg.get("wall_radius_m") is not None and float(cfg.get("wall_radius_m") or 0) > 1e-3
                else ""
            )
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
    hole_path = proc / "theTerrain_holemap.png"
    manual = proc / "theTerrain_holemap_manual.png"
    hole: np.ndarray | None = None
    if args.skip_holemap:
        print("Skipping holemap read/write (--skip-holemap)")
    elif manual.is_file():
        hole = np.asarray(Image.open(manual), dtype=np.uint8).copy()
        if hole.ndim == 3:
            hole = hole[..., 0].copy()
        print(f"Portal holes start from manual map ({int((hole > 0).sum())} px)")
    else:
        hole = np.zeros((size, size), dtype=np.uint8)
    if hole is not None and (hole.shape[0] != size or hole.shape[1] != size):
        hole = np.zeros((size, size), dtype=np.uint8)
    elif hole is not None and (not hole.flags.writeable):
        hole = np.array(hole, dtype=np.uint8, copy=True)
    any_seat = any(
        _gallery_flag(g, defaults, "portal_seat", True) for g in centerlines
    ) or bool(defaults.get("portal_seat", True))
    any_conform = any(
        _gallery_flag(g, defaults, "approach_conform", True) for g in centerlines
    ) or bool(defaults.get("approach_conform"))
    any_embed = any(
        _gallery_flag(g, defaults, "terrain_embed", False) for g in centerlines
    ) or bool(defaults.get("terrain_embed"))
    conf_stats = None
    embed_stats = None
    seat_stats = None
    if (any_seat or any_conform or any_embed) and centerlines:
        hm_path = proc / f"heightmap_{size}.png"
        meta_hm = json.loads((proc / "heightmap_meta.json").read_text(encoding="utf-8"))
        max_h = float(meta_hm["max_height_m"])
        dgm = np.asarray(Image.open(hm_path), dtype=np.float64) / 65535.0 * max_h
        elev = dgm.copy()
        if any_seat:
            t0 = time.perf_counter()
            elev, hole, seat_stats = bake_portal_seat(
                centerlines,
                span_infos,
                size=size,
                extent=extent,
                defaults=defaults,
                elev=elev,
                hole=hole,
            )
            print(f"portal_seat bake {time.perf_counter() - t0:.2f}s")
        if any_conform:
            t0 = time.perf_counter()
            elev, conf_stats = conform_approach_heightmap(
                centerlines,
                span_infos,
                size=size,
                extent=extent,
                defaults=defaults,
                elev=elev,
            )
            print(f"approach_conform bake {time.perf_counter() - t0:.2f}s")
        if any_embed:
            t0 = time.perf_counter()
            elev, embed_stats = embed_terrain_lips_heightmap(
                centerlines,
                span_infos,
                size=size,
                extent=extent,
                defaults=defaults,
                elev=elev,
                hole=hole,
            )
            print(f"terrain_embed bake {time.perf_counter() - t0:.2f}s")
            dbg_lines = list(embed_stats.pop("debug_foundation_lines", []) or [])
            if _gallery_flag({}, defaults, "terrain_embed_debug", False) or any(
                _gallery_flag(g, defaults, "terrain_embed_debug", False) for g in centerlines
            ):
                inject_embed_foundation_debug(level_name, dbg_lines)
            else:
                clear_embed_foundation_debug(level_name)
            embed_stats["debug_foundation_line_count"] = len(dbg_lines)
            if hole is not None and want_portal_holes:
                hole_path = write_hole_assets(proc, level_name, hole)
                n_h = int((hole > 0).sum())
                print(
                    f"Portal-face holemap: +{embed_stats.get('portal_hole_pixels', 0)} px "
                    f"(total {n_h}) -> {hole_path}"
                )
        tag = (
            "gallery_embed"
            if any_embed
            else ("gallery_seat" if any_seat else "gallery_approach")
        )
        baked_path = write_terrain_embed_assets(
            proc,
            level_name,
            elev=elev,
            dgm=dgm,
            max_height_m=max_h,
            hole=hole,
            tag=tag,
        )
        print(f"Wrote {baked_path}")
    else:
        import heightmap_layers as hml

        hml.drop_span_part(proc, "gallery")
        hm_src = proc / f"heightmap_{size}.png"
        if hm_src.is_file():
            max_h = float(
                json.loads((proc / "heightmap_meta.json").read_text(encoding="utf-8"))[
                    "max_height_m"
                ]
            )
            hml.compose(proc, size=size, max_h=max_h, level_name=level_name)
        clear_embed_foundation_debug(level_name)
    if hole is not None:
        coverage = float(hole.mean()) / 255.0 * 100.0
        print(f"Wrote {hole_path} coverage={coverage:.3f}% (manual/preserve workflow)")
    else:
        coverage = None
        print("Holemap not updated (--skip-holemap)")

    meta = {
        "count": len(centerlines),
        "level": level_name,
        "defaults": {k: defaults[k] for k in GALLERY_SCALAR_KEYS if k in defaults},
        "style_defaults": defaults["style"],
        "materials_defaults": defaults["materials"],
        "spans": span_infos,
        "centerlines_file": str(cl_path.relative_to(ROOT)).replace("\\", "/"),
        "holemap": (
            str(hole_path.relative_to(ROOT)).replace("\\", "/")
            if hole is not None
            else None
        ),
        "hole_coverage_pct": round(coverage, 3) if coverage is not None else None,
        "skip_holemap": bool(args.skip_holemap),
        "approach_conform": conf_stats,
        "terrain_embed": embed_stats,
        "portal_seat": seat_stats,
        "note": (
            "portal_seat: deck apron toward the exit (default 8 m). "
            "Roof force and door hole stay off unless YAML sets them. "
            "Re-import terrainPreset if heightmap was baked."
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
        # Leftover MeshRoad/TSStatic debug shreds the deck visually (and used to collide).
        clear_gallery_debug_artifacts(level_name)

    if args.holes_only:
        print("Skipped gallery mesh inject (--holes-only). Re-import terrainPreset.json.")
        return

    sibling_oids: set[int] = set()
    for feat in feats:
        sibling_oids |= _feat_objectids(feat)
    items_path = inject_gallery_tsstatics(
        level_name,
        ts_entries,
        merge=bool(only_ids),
        sibling_oids=sibling_oids,
        proc=proc,
    )
    proc_items = proc / "galleries_items.level.json"
    with proc_items.open("w", encoding="utf-8", newline="\n") as f:
        for e in ts_entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")
    print(f"Wrote {proc_items}")
    if items_path:
        user_level = USER_LEVELS / level_name
        if not only_ids:
            n_rm = _cleanup_orphan_gallery_dae(user_level, proc, ts_entries)
            if n_rm:
                print(f"Removed {n_rm} leftover gallery DAE(s)")
        print(f"Injected: {items_path}")
        print(
            "BeamNG ganz beenden und neu starten. "
            "Heightmap/Holemap: terrainPreset im World Editor neu importieren."
        )
    else:
        print("Skipped inject (create level folder first).")


if __name__ == "__main__":
    main()
