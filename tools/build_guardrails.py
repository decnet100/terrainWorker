"""Place BeamNG roadside TSStatics from annotations GPKG or OSM heuristic.

Default style is Leitpoller posts every ``spacing_m`` (reflector mesh).
Set ``beamng.guardrails.style: sections`` for abutting Italy guardrail meshes.

Preferred source: data/annotations/*.gpkg layer `guardrail` (after seed + QGIS edit).
Fallback: offset from roads_beamng.json when GPKG missing/empty.

Does NOT write or overwrite the annotations GPKG — use seed_annotations.py --force.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
from PIL import Image
from shapely.geometry import LineString, MultiLineString

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import SiteCoords, annotations_gpkg, load_site, processed_dir  # noqa: E402
from road_edge import prepare_roads_for_edge, rail_offset_m, sample_center_at_s  # noqa: E402
from guardrail_rules import (  # noqa: E402
    detect_steep_sides,
    enrich_prepared_roads,
    is_steep_sides,
    normalize_styles,
    resolve_rail_cfg,
    side_signs as rule_side_signs,
)
from gip_road_segments import (  # noqa: E402
    gip_is_subordinate_lane,
    gip_skip_guardrails,
    is_gip_tunnel_segment,
    load_gip_road_segments,
)
from guardrail_junctions import (  # noqa: E402
    build_junction_axes,
    filter_joints_open_junctions,
    find_junctions,
)
from strassennetz_roads import (  # noqa: E402
    load_strassennetz_roads_dict,
    load_strassennetz_sliced_by_gip,
)

SITE = load_site()
PROC = processed_dir(SITE)
BNG = SITE.get("beamng", {})
GR = BNG.get("guardrails", {}) or {}
ANN = SITE.get("annotations") or {}
COORDS = SiteCoords(SITE)
# Match DecalRoad stitch/blend so rails follow the same lane-ramp edge.
_DEC = BNG.get("decal_roads") or {}
_DEC_DEF = _DEC.get("defaults") if isinstance(_DEC.get("defaults"), dict) else _DEC

LEVEL_NAME = BNG.get("level_name", "autoroad_m28_test")
# Default style list; rules may place posts and/or sections in the same build.
STYLE_DEFAULTS = normalize_styles(GR.get("styles") or GR.get("style") or "posts")
# Legacy single STYLE for GPKG path when only one kind is configured.
STYLE = (
    "posts"
    if STYLE_DEFAULTS == ["posts"]
    else ("sections" if STYLE_DEFAULTS == ["sections"] else "both")
)
_DEFAULT_POST_SHAPE = "reflector"
_DEFAULT_SECTION_SHAPE = "/art/shapes/objects/italy_guardrails_common_section.dae"
POST_SHAPE = str(
    GR.get("post_shape")
    or (GR.get("shape") if "posts" in STYLE_DEFAULTS else None)
    or _DEFAULT_POST_SHAPE
)
SECTION_SHAPE = str(
    GR.get("section_shape")
    or (
        GR.get("shape")
        if "sections" in STYLE_DEFAULTS and "posts" not in STYLE_DEFAULTS
        else None
    )
    or _DEFAULT_SECTION_SHAPE
)
SHAPE = POST_SHAPE  # mutated in main() when vendoring reflector
MESH_LENGTH = float(GR.get("mesh_length_m", 3.1))
SECTION_LEN = float(GR.get("section_length_m", 4.2))
SPACING_M = float(GR.get("spacing_m") or GR.get("post_spacing_m") or 25.0)
POST_SCALE = float(
    GR.get("post_scale")
    if GR.get("post_scale") is not None
    else (0.7 if "posts" in STYLE_DEFAULTS else 1.0)
)

STEAM_LEVELS = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive\content\levels"
)
DRIVER_TRAINING_ZIP = STEAM_LEVELS / "driver_training.zip"
REFLECTOR_ZIP_PREFIX = "levels/driver_training/art/shapes/objects/"
REFLECTOR_LOCAL_REL = Path("art") / "shapes" / "objects"
CURVE_R15_M = float(GR.get("curve_r15_m", 15.0))
CURVE_R10_M = float(GR.get("curve_r10_m", 10.0))
CURVE_STEP_R15 = float(GR.get("curve_step_r15_m", 2.8))
CURVE_STEP_R10 = float(GR.get("curve_step_r10_m", 2.2))
ABUT_OVERLAP = float(GR.get("abut_overlap_m", 0.08))
LATERAL_EXTRA = float(GR.get("lateral_extra_m", 1.0))
Z_SAMPLE_INWARD = float(GR.get("z_sample_inward_m", 0.35))
# On MeshRoad decks: sit on the deck (not DGM gorge) and tuck toward the curb.
BRIDGE_DECK_Z = bool(GR.get("bridge_deck_z", True))
BRIDGE_LATERAL_EXTRA = float(
    GR.get("bridge_lateral_extra_m")
    if GR.get("bridge_lateral_extra_m") is not None
    else 0.0
)
BRIDGE_MATCH_PAD_M = float(GR.get("bridge_match_pad_m", 1.5))
SIDES = GR.get("sides", "both")
HIGHWAYS = set(
    GR.get(
        "highways",
        ["motorway", "trunk", "primary", "secondary", "tertiary"],
    )
)
Z_LIFT = float(GR.get("z_lift_m", 0.05))
PIVOT_GROUND_OFFSET = float(GR.get("pivot_ground_offset_m", 0.0))
ALIGN_PITCH = bool(GR.get("align_pitch", True))
SNAP_TO_HEIGHTMAP = bool(GR.get("snap_to_heightmap", True))
FACE_Y_OUTWARD = bool(GR.get("face_y_outward", True))
YAW_FLIP_RIGHT = bool(GR.get("yaw_flip_right", True))
ROAD_WIDTH_SCALE = float(BNG.get("road_width_scale", 1.0))
ENABLED = bool(GR.get("enabled", True))
TERRAIN_EXTENT = COORDS.terrain_extent
CURVATURE_WINDOW_M = float(GR.get("curvature_window_m", 8.0))
# Stop rails before gallery/tunnel portals (no rails through the structure)
CLIP_GALLERIES = bool(GR.get("clip_galleries", True))
STOP_BEFORE_GALLERY_M = float(GR.get("stop_before_gallery_m", 3.0))
GALLERY_CLIP_PAD_M = float(GR.get("gallery_clip_pad_m", 2.0))
# Drop joints that sit inside another carriageway (legacy online clip).
# Prefer open_junctions post-pass (side toward foreign only).
CLIP_CENTERLINES = bool(GR.get("clip_centerlines", False))
CENTERLINE_CLIP_PAD_M = float(GR.get("centerline_clip_pad_m", 0.75))
# Post-pass: open mouths where compatible GIP roads meet.
OPEN_JUNCTIONS = bool(GR.get("open_junctions", True))
JUNCTION_JOIN_M = float(GR.get("junction_join_m", 4.0))
JUNCTION_MOUTH_M = float(GR.get("junction_mouth_m", 12.0))
JUNCTION_Z_SEP_M = float(GR.get("junction_z_sep_m", 3.0))
# Drop joints whose XY lies over a buried tunnel (rails on the mountain).
CLIP_TUNNELS = bool(GR.get("clip_tunnels", True))
TUNNEL_CLIP_PAD_M = float(
    GR.get("tunnel_clip_pad_m")
    if GR.get("tunnel_clip_pad_m") is not None
    else GALLERY_CLIP_PAD_M
)
# auto | gpkg | heuristic
SOURCE_MODE = str(ANN.get("guardrail_source", GR.get("source", "auto"))).lower()
# Prefer guardrails.* overrides, else same defaults as decal_roads.
STITCH_ABUTTING = bool(
    GR.get("stitch_abutting")
    if GR.get("stitch_abutting") is not None
    else _DEC_DEF.get("stitch_abutting", True)
)
STITCH_TOL_M = float(
    GR.get("stitch_tol_m")
    if GR.get("stitch_tol_m") is not None
    else (
        _DEC_DEF.get("stitch_tol_m")
        if _DEC_DEF.get("stitch_tol_m") is not None
        else 1.25
    )
)
WIDTH_FILL_DIP_M = float(
    GR.get("width_fill_dip_m")
    if GR.get("width_fill_dip_m") is not None
    else (
        _DEC_DEF.get("width_fill_dip_m")
        if _DEC_DEF.get("width_fill_dip_m") is not None
        else 40.0
    )
)
WIDTH_BLEND_M = float(
    GR.get("width_blend_m")
    if GR.get("width_blend_m") is not None
    else (
        _DEC_DEF.get("width_blend_m")
        if _DEC_DEF.get("width_blend_m") is not None
        else 25.0
    )
)
DENSIFY_MAX_STEP_M = float(
    GR.get("densify_max_step_m")
    if GR.get("densify_max_step_m") is not None
    else (
        _DEC_DEF.get("densify_max_step_m")
        if _DEC_DEF.get("densify_max_step_m") is not None
        else 12.0
    )
)
CENTERLINE_MODE = str(GR.get("centerline") or "osm").lower().strip()
LANE_WIDTH_M = float(BNG.get("lane_width_m") if BNG.get("lane_width_m") is not None else 3.75)
RAIL_RULES = list(GR.get("rules") or GR.get("items") or [])
RAIL_DEFAULTS = {
    "sides": SIDES,
    "present": True,
    "lateral_extra_m": LATERAL_EXTRA,
    "spacing_m": SPACING_M,
    "section_length_m": SECTION_LEN,
    "style": list(STYLE_DEFAULTS),
}
_STEEP_RAW = GR.get("steep_sides") if isinstance(GR.get("steep_sides"), dict) else {}
# Enabled when sides is steep/auto, or steep_sides.enabled explicitly true.
STEEP_SAMPLE_M = float(
    _STEEP_RAW.get("sample_m") if _STEEP_RAW.get("sample_m") is not None else 250.0
)
STEEP_LOOK_OUT_M = float(
    _STEEP_RAW.get("look_out_m") if _STEEP_RAW.get("look_out_m") is not None else 6.0
)
STEEP_DROP_M = float(
    _STEEP_RAW.get("drop_m") if _STEEP_RAW.get("drop_m") is not None else 2.5
)
STEEP_MIN_HITS = int(
    _STEEP_RAW.get("min_hits") if _STEEP_RAW.get("min_hits") is not None else 1
)
STEEP_FALLBACK = str(_STEEP_RAW.get("fallback") or "both").lower().strip()


def vendor_reflector_pack(user_level: Path, level_name: str) -> str:
    """Copy reflector mesh + materials into this level; return local VFS shape path.

    Cross-level ``/levels/driver_training/.../reflector.dae`` loads the mesh, but
    sibling materials are not registered — posts render untextured / wrong.
    Native mesh height ≈ 1.43 m; use ``post_scale`` (~0.7 → ≈1.0 m).
    """
    if not DRIVER_TRAINING_ZIP.is_file():
        raise SystemExit(f"Missing {DRIVER_TRAINING_ZIP}")

    dest = user_level / REFLECTOR_LOCAL_REL
    dest.mkdir(parents=True, exist_ok=True)
    mesh_files = (
        "reflector.dae",
        "reflector.cdae",
        "reflector.dae.imposter.dds",
        "reflector.dae.imposter_normals.dds",
    )
    n_copied = 0
    with zipfile.ZipFile(DRIVER_TRAINING_ZIP) as z:
        names = set(z.namelist())
        for name in mesh_files:
            src = REFLECTOR_ZIP_PREFIX + name
            if src not in names:
                if name.endswith(".cdae"):
                    continue
                raise SystemExit(f"Missing in driver_training.zip: {src}")
            (dest / name).write_bytes(z.read(src))
            n_copied += 1

        objects_mats = json.loads(
            z.read("levels/driver_training/art/shapes/objects/main.materials.json")
        )
        shapes_mats = json.loads(
            z.read("levels/driver_training/art/shapes/main.materials.json")
        )

    # Copy diamondplate DDS from assets (driver_training only has .link stubs).
    assets_zip = (
        Path(r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive\content\assets")
        / "materials"
        / "tileable.zip"
    )
    tex_prefix = "assets/materials/tileable/metal/speedbump/"
    tex_names = (
        "diamondplate_d.dds",
        "diamondplate_n.dds",
        "diamondplate_r.dds",
        "diamondplate_s.dds",
    )
    if assets_zip.is_file():
        with zipfile.ZipFile(assets_zip) as az:
            for name in tex_names:
                src = tex_prefix + name
                if src in az.namelist():
                    (dest / name).write_bytes(az.read(src))
                    n_copied += 1

    # Local relative maps so sibling main.materials.json resolves in this level.
    speedbump = dict(objects_mats.get("speedbump") or {})
    speedbump["name"] = "speedbump"
    speedbump["mapTo"] = "speedbump"
    speedbump["class"] = "Material"
    stages = list(speedbump.get("Stages") or [{}, {}, {}, {}])
    while len(stages) < 4:
        stages.append({})
    stage0 = dict(stages[0] or {})
    stage0.update(
        {
            "colorMap": "diamondplate_d.dds",
            "normalMap": "diamondplate_n.dds",
            "reflectivityMap": "diamondplate_r.dds",
            "specularMap": "diamondplate_s.dds",
            "vertColor": True,
            "pixelSpecular": True,
            "specularPower": 32,
            "useAnisotropic": True,
        }
    )
    stages[0] = stage0
    speedbump["Stages"] = stages

    soft = dict(shapes_mats.get("SOFT_COLLISION_GENERAL") or {})
    soft.setdefault("name", "SOFT_COLLISION_GENERAL")
    soft.setdefault("mapTo", "SOFT_COLLISION_GENERAL")
    soft.setdefault("class", "Material")

    # Mesh also refs these; invent simple stand-ins if stock defs are missing.
    vertexcolor = {
        "name": "vertexcolor_shadeless",
        "mapTo": "vertexcolor_shadeless",
        "class": "Material",
        "persistentId": "a11e0002-11e0-4ead-b100-0000ffffff02",
        "Stages": [
            {
                "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
                "roughnessFactor": 0.85,
                "vertColor": True,
                "emissiveFactor": [0.05, 0.05, 0.05],
            },
            {},
            {},
            {},
        ],
        "alphaRef": 0,
        "castShadows": True,
        "materialTag0": "beamng",
        "translucentBlendOp": "None",
        "version": 1.5,
    }
    ind_plastic = {
        "name": "ind_plastic",
        "mapTo": "ind_plastic",
        "class": "Material",
        "persistentId": "a11e0002-11e0-4ead-b100-0000ffffff03",
        "Stages": [
            {
                "baseColorFactor": [0.92, 0.92, 0.9, 1.0],
                "roughnessFactor": 0.55,
                "metallicFactor": 0.0,
                "vertColor": True,
            },
            {},
            {},
            {},
        ],
        "alphaRef": 0,
        "materialTag0": "beamng",
        "translucentBlendOp": "None",
        "version": 1.5,
    }

    mats_path = dest / "main.materials.json"
    existing: dict = {}
    if mats_path.is_file() and mats_path.stat().st_size:
        try:
            existing = json.loads(mats_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    existing["speedbump"] = speedbump
    existing["SOFT_COLLISION_GENERAL"] = soft
    existing["vertexcolor_shadeless"] = vertexcolor
    existing["ind_plastic"] = ind_plastic
    mats_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")

    vfs = f"/levels/{level_name}/{REFLECTOR_LOCAL_REL.as_posix()}/reflector.dae"
    print(f"Vendored reflector pack → {dest} ({n_copied} mesh files + materials)")
    return vfs


def _norm3(x: float, y: float, z: float) -> tuple[float, float, float]:
    L = math.sqrt(x * x + y * y + z * z)
    if L < 1e-12:
        return (1.0, 0.0, 0.0)
    return (x / L, y / L, z / L)


def _rot_matrix_pitched(tx: float, ty: float, tz: float, y_sign: float) -> list[float]:
    """X = 3D tangent, Z = upright (world-up projected), Y = Z×X * y_sign."""
    xx, xy, xz = _norm3(tx, ty, tz)
    dot = xz
    zx, zy, zz = _norm3(-xx * dot, -xy * dot, 1.0 - xz * dot)
    yx = zy * xz - zz * xy
    yy = zz * xx - zx * xz
    yz = zx * xy - zy * xx
    yx, yy, yz = _norm3(yx * y_sign, yy * y_sign, yz * y_sign)
    xx = yy * zz - yz * zy
    xy = yz * zx - yx * zz
    xz = yx * zy - yy * zx
    xx, xy, xz = _norm3(xx, xy, xz)
    return [xx, xy, xz, yx, yy, yz, zx, zy, zz]


def _yaw180(rot: list[float]) -> list[float]:
    xx, xy, xz, yx, yy, yz, zx, zy, zz = rot
    return [-xx, -xy, -xz, -yx, -yy, -yz, zx, zy, zz]


def _polyline_length_xy(nodes: list[list[float]]) -> float:
    total = 0.0
    for i in range(len(nodes) - 1):
        total += math.hypot(nodes[i + 1][0] - nodes[i][0], nodes[i + 1][1] - nodes[i][1])
    return total


def _sample_at(nodes: list[list[float]], dist: float) -> tuple[float, float, float] | None:
    if len(nodes) < 2:
        return None
    acc = 0.0
    for i in range(len(nodes) - 1):
        x0, y0, z0 = nodes[i][0], nodes[i][1], nodes[i][2]
        x1, y1, z1 = nodes[i + 1][0], nodes[i + 1][1], nodes[i + 1][2]
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        if dist <= acc + length + 1e-9:
            t = max(0.0, min(1.0, (dist - acc) / length))
            return (x0 + dx * t, y0 + dy * t, z0 + (z1 - z0) * t)
        acc += length
    return (nodes[-1][0], nodes[-1][1], nodes[-1][2])


def _local_radius_xy(nodes: list[list[float]], dist: float, window: float) -> float:
    total = _polyline_length_xy(nodes)
    p0 = _sample_at(nodes, max(0.0, dist - window))
    p1 = _sample_at(nodes, dist)
    p2 = _sample_at(nodes, min(total, dist + window))
    if not p0 or not p1 or not p2:
        return float("inf")
    ax, ay = p0[0], p0[1]
    bx, by = p1[0], p1[1]
    cx, cy = p2[0], p2[1]
    ab = math.hypot(bx - ax, by - ay)
    bc = math.hypot(cx - bx, cy - by)
    ac = math.hypot(cx - ax, cy - ay)
    if ab < 1e-6 or bc < 1e-6 or ac < 1e-6:
        return float("inf")
    area2 = abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax))
    if area2 < 1e-9:
        return float("inf")
    return (ab * bc * ac) / (2.0 * area2)


def _step_for_radius(radius: float, section_len: float) -> float:
    if radius <= CURVE_R10_M:
        return CURVE_STEP_R10
    if radius <= CURVE_R15_M:
        return CURVE_STEP_R15
    if radius < 40.0:
        t = (radius - CURVE_R15_M) / (40.0 - CURVE_R15_M)
        return CURVE_STEP_R15 + t * (section_len - CURVE_STEP_R15)
    return section_len


def _tangent_between(
    nodes: list[list[float]], d0: float, d1: float
) -> tuple[float, float, float]:
    p0 = _sample_at(nodes, d0)
    p1 = _sample_at(nodes, d1)
    if not p0 or not p1:
        return (1.0, 0.0, 0.0)
    return _norm3(p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2])


def _load_heightmap_z() -> tuple[np.ndarray, float] | None:
    import heightmap_layers as hml

    size = int(BNG.get("mask_size", 512))
    try:
        elev, max_h, _label = hml.load_composed_or_dgm(PROC, size)
    except FileNotFoundError:
        return None
    # Guardrails sample via u16 bilinear; keep that encoding.
    u16 = np.clip(np.round(elev / max(max_h, 1e-6) * 65535.0), 0, 65535).astype(np.uint16)
    return u16, max_h


def _heightmap_z(hm: np.ndarray, max_h: float, bx: float, by: float) -> float:
    size = hm.shape[0]
    px = bx / TERRAIN_EXTENT * (size - 1)
    py = (1.0 - by / TERRAIN_EXTENT) * (size - 1)
    x0 = int(math.floor(px))
    y0 = int(math.floor(py))
    x1 = min(x0 + 1, size - 1)
    y1 = min(y0 + 1, size - 1)
    x0 = max(0, min(x0, size - 1))
    y0 = max(0, min(y0, size - 1))
    tx, ty = px - x0, py - y0
    v00 = float(hm[y0, x0])
    v10 = float(hm[y0, x1])
    v01 = float(hm[y1, x0])
    v11 = float(hm[y1, x1])
    v = v00 * (1 - tx) * (1 - ty) + v10 * tx * (1 - ty) + v01 * (1 - tx) * ty + v11 * tx * ty
    return (v / 65535.0) * max_h


def _joint_dists(nodes: list[list[float]], section_len: float) -> list[float]:
    total = _polyline_length_xy(nodes)
    if total < 1.0:
        return []
    dists = [0.0]
    dist = 0.0
    while dist < total - 0.05:
        probe = min(dist + 0.5, total)
        r = _local_radius_xy(nodes, probe, CURVATURE_WINDOW_M)
        step = _step_for_radius(r, section_len)
        if dist + step >= total - 0.05:
            if total - dist >= 0.4:
                dists.append(total)
            elif len(dists) >= 2:
                dists[-1] = total
            break
        dist += step
        dists.append(dist)
    return dists


def _post_dists(nodes: list[list[float]], spacing_m: float) -> list[float]:
    """Uniform station distances for Leitpoller / delineator posts."""
    total = _polyline_length_xy(nodes)
    spacing = max(1.0, float(spacing_m))
    if total < 0.5:
        return []
    dists = [0.0]
    d = spacing
    while d < total - 1e-6:
        dists.append(d)
        d += spacing
    if total - dists[-1] > 0.35 * spacing:
        dists.append(total)
    return dists


def _side_signs() -> list[tuple[str, float]]:
    return rule_side_signs(SIDES)


def _sign_for_side(side_name: str) -> float:
    return 1.0 if side_name == "left" else -1.0


def _truthy_present(val) -> bool:
    if val is None:
        return True
    if isinstance(val, (bool, np.bool_)):
        return bool(val)
    s = str(val).strip().lower()
    return s not in {"0", "false", "no", "n", "off"}


def _iter_lines(geom) -> list[LineString]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if isinstance(geom, MultiLineString):
        return [g for g in geom.geoms if isinstance(g, LineString) and not g.is_empty]
    # Polygon exterior etc. — ignore
    return []


def _nodes_from_line_crs(
    line: LineString,
    hm_pack: tuple[np.ndarray, float] | None,
) -> list[list[float]]:
    nodes: list[list[float]] = []
    for x, y in line.coords:
        bx, by = COORDS.crs_to_beamng(float(x), float(y))
        if hm_pack is not None:
            hm, max_h = hm_pack
            pz = _heightmap_z(hm, max_h, bx, by) + Z_LIFT + PIVOT_GROUND_OFFSET
        else:
            pz = Z_LIFT + PIVOT_GROUND_OFFSET
        nodes.append([bx, by, pz])
    return nodes


def _entries_posts_along_rail(
    nodes: list[list[float]],
    side_name: str,
    spacing_m: float,
    *,
    resample: bool = True,
    shape: str | None = None,
) -> list[dict]:
    """Place upright Leitpoller / reflector posts every spacing_m (no X-stretch)."""
    total = _polyline_length_xy(nodes)
    if total < 0.5:
        return []
    if resample:
        dists = _post_dists(nodes, spacing_m)
        joints: list[tuple[float, float, float, float]] = []
        for d in dists:
            p = _sample_at(nodes, d)
            if p:
                joints.append((p[0], p[1], p[2], d))
    else:
        # Pre-offset joints: recover approx stations along the offset polyline.
        joints = []
        acc = 0.0
        for i, n in enumerate(nodes):
            if i > 0:
                acc += math.hypot(
                    n[0] - nodes[i - 1][0], n[1] - nodes[i - 1][1]
                )
            joints.append((n[0], n[1], n[2], acc))
    if not joints:
        return []

    entries: list[dict] = []
    sc = max(0.05, float(POST_SCALE))
    shape_name = shape or POST_SHAPE or SHAPE
    for px, py, pz, dist in joints:
        d0 = max(0.0, dist - 0.75)
        d1 = min(total, dist + 0.75)
        if abs(d1 - d0) < 1e-4:
            d0, d1 = max(0.0, dist - 0.1), min(total, dist + 0.1)
        tx, ty, tz = _tangent_between(nodes, d0, d1)
        if not ALIGN_PITCH:
            tx, ty, tz = tx, ty, 0.0
        sign = _sign_for_side(side_name)
        y_sign = sign if FACE_Y_OUTWARD else -sign
        rot = _rot_matrix_pitched(tx, ty, tz, y_sign)
        if side_name == "right" and YAW_FLIP_RIGHT:
            rot = _yaw180(rot)
        entries.append({
            "name": _tsstatic_name("post", px, py, pz, side_name),
            "class": "TSStatic",
            "__parent": "guardrails",
            "position": [round(px, 3), round(py, 3), round(pz, 3)],
            "rotationMatrix": [round(v, 6) for v in rot],
            "scale": [round(sc, 4), round(sc, 4), round(sc, 4)],
            "shapeName": shape_name,
            "collisionType": "Collision Mesh",
            "decalType": "Collision Mesh",
            "useInstanceRenderData": True,
            "isRenderEnabled": True,
        })
    return entries


def _tsstatic_name(kind: str, px: float, py: float, pz: float, side: str) -> str:
    """Stable unique name so re-inject replaces instead of stacking anonymous objects."""
    return (
        f"gr_{kind}_{side}_"
        f"{int(round(px * 10))}_{int(round(py * 10))}_{int(round(pz * 10))}"
    )


def _dedupe_entries(entries: list[dict], tol_m: float = 0.35) -> list[dict]:
    """Drop near-identical positions of the *same* kind (post vs sect stay both)."""
    if tol_m <= 0 or len(entries) < 2:
        return entries

    def _kind(e: dict) -> str:
        name = str(e.get("name") or "")
        parts = name.split("_")
        return parts[1] if len(parts) > 1 else name

    kept: list[dict] = []
    for e in entries:
        pos = e.get("position") or [0, 0, 0]
        px, py = float(pos[0]), float(pos[1])
        kind = _kind(e)
        dup = False
        for k in kept:
            if _kind(k) != kind:
                continue
            kp = k.get("position") or [0, 0, 0]
            if math.hypot(px - float(kp[0]), py - float(kp[1])) <= tol_m:
                dup = True
                break
        if not dup:
            kept.append(e)
    dropped = len(entries) - len(kept)
    if dropped:
        print(f"Deduped {dropped} near-duplicate placements (tol={tol_m}m)")
    return kept


def _entries_along_rail(
    nodes: list[list[float]],
    side_name: str,
    section_len: float,
    curve_stats: dict,
    *,
    resample: bool = True,
    style: str = "posts",
    shape: str | None = None,
) -> list[dict]:
    """Place posts and/or abutting section meshes along a pre-offset rail."""
    styles = normalize_styles(style, default="posts")
    out: list[dict] = []
    if "posts" in styles:
        out.extend(
            _entries_posts_along_rail(
                nodes,
                side_name,
                section_len if section_len > 0 else SPACING_M,
                resample=resample,
                shape=shape or POST_SHAPE or SHAPE,
            )
        )
    if "sections" not in styles:
        return out

    if resample:
        dists = _joint_dists(nodes, section_len if section_len > 0 else SECTION_LEN)
        if len(dists) < 2:
            return out
        joints: list[tuple[float, float, float]] = []
        for d in dists:
            p = _sample_at(nodes, d)
            if p:
                joints.append(p)
    else:
        joints = [(n[0], n[1], n[2]) for n in nodes]
    if len(joints) < 2:
        return out

    shape_name = shape or SECTION_SHAPE
    for i in range(len(joints) - 1):
        x0, y0, z0 = joints[i]
        x1, y1, z1 = joints[i + 1]
        dx, dy, dz = x1 - x0, y1 - y0, z1 - z0
        chord = math.sqrt(dx * dx + dy * dy + dz * dz)
        if chord < 0.35:
            continue

        length = chord + ABUT_OVERLAP
        if length <= CURVE_STEP_R10 + ABUT_OVERLAP + 1e-6:
            curve_stats["r10"] += 1
        elif length <= CURVE_STEP_R15 + ABUT_OVERLAP + 1e-6:
            curve_stats["r15"] += 1
        else:
            curve_stats["straight"] += 1

        px = (x0 + x1) * 0.5
        py = (y0 + y1) * 0.5
        pz = (z0 + z1) * 0.5
        if ALIGN_PITCH:
            tx, ty, tz = dx, dy, dz
        else:
            tx, ty, tz = dx, dy, 0.0

        sign = _sign_for_side(side_name)
        if FACE_Y_OUTWARD:
            y_sign = sign
        else:
            y_sign = -sign
        rot = _rot_matrix_pitched(tx, ty, tz, y_sign)
        if side_name == "right" and YAW_FLIP_RIGHT:
            rot = _yaw180(rot)

        scale_x = length / MESH_LENGTH if MESH_LENGTH > 1e-6 else 1.0
        out.append({
            "name": _tsstatic_name("sect", px, py, pz, side_name),
            "class": "TSStatic",
            "__parent": "guardrails",
            "position": [round(px, 3), round(py, 3), round(pz, 3)],
            "rotationMatrix": [round(v, 6) for v in rot],
            "scale": [round(scale_x, 4), 1.0, 1.0],
            "shapeName": shape_name,
            "collisionType": "Collision Mesh",
            "decalType": "Collision Mesh",
            "useInstanceRenderData": True,
            "isRenderEnabled": True,
        })
    return out


def load_guardrail_features(gpkg: Path) -> gpd.GeoDataFrame | None:
    if not gpkg.exists():
        return None
    try:
        gdf = gpd.read_file(gpkg, layer="guardrail")
    except Exception as ex:  # noqa: BLE001
        print(f"Could not read guardrail layer from {gpkg}: {ex}")
        return None
    if len(gdf) == 0:
        return gdf
    if gdf.crs is None:
        gdf = gdf.set_crs(COORDS.crs)
    elif str(gdf.crs).replace("epsg:", "EPSG:") != COORDS.crs:
        gdf = gdf.to_crs(COORDS.crs)
    return gdf


def build_entries_from_gpkg(gdf: gpd.GeoDataFrame) -> list[dict]:
    hm_pack = _load_heightmap_z() if SNAP_TO_HEIGHTMAP else None
    entries: list[dict] = []
    curve_stats = {"straight": 0, "r15": 0, "r10": 0}
    used = 0
    skipped = 0
    corridors = _load_gallery_exclusion_corridors()

    for _, row in gdf.iterrows():
        if not _truthy_present(row.get("present", True)):
            skipped += 1
            continue
        side = str(row.get("side") or "left").strip().lower()
        if side not in {"left", "right"}:
            side = "left"
        if SIDES == "left" and side != "left":
            continue
        if SIDES == "right" and side != "right":
            continue

        section = row.get("section_m")
        try:
            if STYLE == "posts":
                section_len = (
                    float(section)
                    if section is not None and str(section) not in {"", "nan"}
                    else SPACING_M
                )
            else:
                section_len = (
                    float(section)
                    if section is not None and str(section) not in {"", "nan"}
                    else SECTION_LEN
                )
        except (TypeError, ValueError):
            section_len = SPACING_M if STYLE == "posts" else SECTION_LEN

        for line in _iter_lines(row.geometry):
            nodes = _nodes_from_line_crs(line, hm_pack)
            if len(nodes) < 2:
                continue
            joints = [(n[0], n[1], n[2]) for n in nodes]
            min_run = 1 if STYLE == "posts" else 2
            runs = _split_joints_outside_galleries(joints, corridors, min_run=min_run)
            for run in runs:
                rail = [[x, y, z] for x, y, z in run]
                # Honour site YAML style (sections / posts / both). The helper
                # defaults to posts, which used to emit bare "reflector"
                # shapeNames and show as "no mesh" in the World Editor.
                part = _entries_along_rail(
                    rail,
                    side,
                    section_len,
                    curve_stats,
                    style=STYLE_DEFAULTS,
                )
                if part:
                    used += 1
                    entries.extend(part)

    kind = "posts" if STYLE == "posts" else "segments"
    print(
        f"GPKG guardrail: features_used~{used} skipped_present=false={skipped} "
        f"{kind}={len(entries)} style={STYLE_DEFAULTS}"
    )
    if STYLE == "sections":
        print(
            f"Placement mix: straightish={curve_stats['straight']} "
            f"~R15_chords={curve_stats['r15']} ~R10_chords={curve_stats['r10']} "
            f"abut_overlap_m={ABUT_OVERLAP}"
        )
    else:
        print(f"Leitpoller spacing_m={SPACING_M} shape={SHAPE}")
    if corridors:
        print(
            f"Gallery clip: corridors={len(corridors)} "
            f"stop_before_m={STOP_BEFORE_GALLERY_M}"
        )
    return entries


def _load_gallery_exclusion_corridors() -> list[tuple[list[tuple[float, float]], float]]:
    """[(xy_polyline, half_width), ...] spanning portals ± stop_before.

    Used to keep heuristic (and optional GPKG) rails from entering galleries /
    tunnels — rails end cleanly before the portal face.
    """
    if not CLIP_GALLERIES:
        return []
    cl_path = PROC / "galleries_centerlines.json"
    if not cl_path.is_file():
        return []
    try:
        data = json.loads(cl_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    corridors: list[tuple[list[tuple[float, float]], float]] = []
    stop = max(0.0, STOP_BEFORE_GALLERY_M)
    pad = max(0.0, GALLERY_CLIP_PAD_M)
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
        half = 0.5 * max(float(n.get("width") or 7.0) for n in band)
        half += pad + LATERAL_EXTRA
        corridors.append((xy, half))
    return corridors


def _dist_point_to_polyline_xy(
    px: float, py: float, poly: list[tuple[float, float]]
) -> float:
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
        if d < best:
            best = d
    return best


def _project_xy_to_xyz_ribbon(
    bx: float,
    by: float,
    ribbon: list[tuple[float, float, float, float]],
) -> tuple[float, float, float] | None:
    """Nearest point on a MeshRoad ribbon -> (dist_m, z_surf, half_width)."""
    if len(ribbon) < 1:
        return None
    best: tuple[float, float, float] | None = None
    for i, n in enumerate(ribbon):
        x0, y0, z0, w0 = n
        hw0 = 0.5 * float(w0)
        if i == len(ribbon) - 1:
            cand = (math.hypot(bx - x0, by - y0), float(z0), hw0)
        else:
            x1, y1, z1, w1 = ribbon[i + 1]
            dx, dy = x1 - x0, y1 - y0
            seg2 = dx * dx + dy * dy
            t = (
                0.0
                if seg2 < 1e-12
                else max(0.0, min(1.0, ((bx - x0) * dx + (by - y0) * dy) / seg2))
            )
            px = x0 + t * dx
            py = y0 + t * dy
            cand = (
                math.hypot(bx - px, by - py),
                float(z0) + t * (float(z1) - float(z0)),
                hw0 + t * (0.5 * float(w1) - hw0),
            )
        if best is None or cand[0] < best[0]:
            best = cand
    return best


def _load_bridge_decks_for_rails() -> list[dict]:
    """Bridge spans for rail Z/lateral: axis from decks JSON, Z from MeshRoad items.

    Each deck: ``{objectid, axis: [(x,y),...], half_w, ribbons: [[(x,y,z,w),...],...]}``.
    """
    if not BRIDGE_DECK_Z and BRIDGE_LATERAL_EXTRA == LATERAL_EXTRA:
        return []
    decks_path = PROC / "bridges_decks.json"
    items_path = PROC / "bridges_items.level.json"
    if not items_path.is_file():
        return []

    ribbons_by_oid: dict[str, list[list[tuple[float, float, float, float]]]] = {}
    for line in items_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(entry.get("class") or "") != "MeshRoad":
            continue
        name = str(entry.get("name") or "")
        # bridge_<slug>_<objectid>_s<i>
        oid = None
        parts = name.rsplit("_", 2)
        if len(parts) >= 2 and parts[-1].startswith("s") and parts[-2].isdigit():
            oid = parts[-2]
        if oid is None:
            continue
        nodes = entry.get("nodes") or []
        if len(nodes) < 2:
            continue
        ribbon: list[tuple[float, float, float, float]] = []
        for n in nodes:
            if len(n) < 4:
                continue
            z = float(n[2])  # top when node_z_is_top
            ribbon.append((float(n[0]), float(n[1]), z, float(n[3])))
        if len(ribbon) >= 2:
            ribbons_by_oid.setdefault(oid, []).append(ribbon)

    decks: list[dict] = []
    if decks_path.is_file():
        try:
            data = json.loads(decks_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        for deck in data.get("decks") or []:
            oid = deck.get("objectid")
            if oid is None:
                continue
            oid_s = str(oid)
            axis_nodes = deck.get("nodes_xyw") or []
            if len(axis_nodes) < 2:
                continue
            axis = [(float(n[0]), float(n[1])) for n in axis_nodes]
            half = 0.5 * max(float(n[2]) for n in axis_nodes if len(n) >= 3)
            ribbons = ribbons_by_oid.get(oid_s) or []
            if not ribbons:
                continue
            decks.append(
                {
                    "objectid": oid,
                    "axis": axis,
                    "half_w": half,
                    "ribbons": ribbons,
                }
            )
    else:
        # Fallback: treat each ribbon as its own deck axis.
        for oid_s, ribbons in ribbons_by_oid.items():
            for rib in ribbons:
                axis = [(p[0], p[1]) for p in rib]
                half = 0.5 * max(p[3] for p in rib)
                decks.append(
                    {
                        "objectid": int(oid_s) if oid_s.isdigit() else oid_s,
                        "axis": axis,
                        "half_w": half,
                        "ribbons": [rib],
                    }
                )
    return decks


def _bridge_deck_at(
    bx: float,
    by: float,
    decks: list[dict],
) -> tuple[float, float] | None:
    """If (bx,by) sits on a bridge span → (deck_surface_z, deck_full_width)."""
    if not decks:
        return None
    pad = max(0.0, BRIDGE_MATCH_PAD_M)
    best: tuple[float, float, float] | None = None  # axis_dist, z, width
    for deck in decks:
        half = float(deck.get("half_w") or 3.75) + pad
        axis = deck.get("axis") or []
        if len(axis) < 2:
            continue
        d_axis = _dist_point_to_polyline_xy(bx, by, axis)
        if d_axis > half:
            continue
        z_best = None
        d_rib_best = float("inf")
        for rib in deck.get("ribbons") or []:
            proj = _project_xy_to_xyz_ribbon(bx, by, rib)
            if proj is None:
                continue
            d_rib, z, _hw = proj
            if d_rib < d_rib_best:
                d_rib_best = d_rib
                z_best = z
        if z_best is None:
            continue
        width = 2.0 * float(deck.get("half_w") or 3.75)
        if best is None or d_axis < best[0]:
            best = (d_axis, float(z_best), width)
    if best is None:
        return None
    return best[1], best[2]


def _point_in_gallery_exclusion(
    px: float,
    py: float,
    corridors: list[tuple[list[tuple[float, float]], float]],
) -> bool:
    for poly, half in corridors:
        if _dist_point_to_polyline_xy(px, py, poly) <= half:
            return True
    return False


def _split_joints_outside_galleries(
    joints: list[tuple[float, float, float]],
    corridors: list[tuple[list[tuple[float, float]], float]],
    *,
    min_run: int = 2,
) -> list[list[tuple[float, float, float]]]:
    """Split a rail into contiguous runs that stay outside gallery corridors."""
    return _split_joints_outside_corridors(joints, corridors, min_run=min_run)


def _contiguous_rich_runs(
    original: list[tuple],
    kept: list[tuple],
    *,
    min_run: int = 2,
) -> list[list[tuple]]:
    """Re-split ``original`` into contiguous runs whose joints remain in ``kept``.

    ``filter_joints_open_junctions`` drops mouth joints but returns a flat list;
    mid-run gaps must become separate rail pieces.
    """
    if not original or not kept:
        return []
    kept_keys = {(round(float(j[0]), 3), round(float(j[1]), 3)) for j in kept}
    chunks: list[list[tuple]] = []
    cur: list[tuple] = []
    for j in original:
        key = (round(float(j[0]), 3), round(float(j[1]), 3))
        if key in kept_keys:
            cur.append(j)
        else:
            if len(cur) >= min_run:
                chunks.append(cur)
            cur = []
    if len(cur) >= min_run:
        chunks.append(cur)
    return chunks


def _split_joints_outside_corridors(
    joints: list,
    corridors: list[tuple[list[tuple[float, float]], float]],
    *,
    min_run: int = 2,
) -> list[list]:
    """Split a rail into contiguous runs outside exclusion corridors.

    Joints may be ``(x,y,z)`` or longer ``(x,y,z,tx,ty,…)``; extras are kept.
    """
    if not joints:
        return []
    if not corridors:
        return [joints] if len(joints) >= min_run else []
    runs: list[list] = []
    cur: list = []
    for j in joints:
        if _point_in_gallery_exclusion(j[0], j[1], corridors):
            if len(cur) >= min_run:
                runs.append(cur)
            cur = []
            continue
        cur.append(j)
    if len(cur) >= min_run:
        runs.append(cur)
    return runs


def _axis_from_road_nodes(
    nodes: list[list[float]],
) -> tuple[list[tuple[float, float]], float] | None:
    """Centerline XY + half-width from road nodes ``[x,y,z,width]``."""
    if len(nodes) < 2:
        return None
    xy: list[tuple[float, float]] = []
    widths: list[float] = []
    for n in nodes:
        if len(n) < 2:
            continue
        xy.append((float(n[0]), float(n[1])))
        if len(n) >= 4:
            widths.append(float(n[3]))
    if len(xy) < 2:
        return None
    half = 0.5 * (max(widths) if widths else 7.5)
    return xy, half


def _build_centerline_clip_axes(
    roads: list[dict],
) -> list[dict]:
    """Foreign carriageway axes for junction mouth clipping.

    Only roads that themselves can carry rails (named STR_CODE, not minor,
    not tunnel). That keeps Reschen fast (~100 axes instead of ~1900).
    """
    if not CLIP_CENTERLINES:
        return []
    axes: list[dict] = []
    for road in roads:
        # Prefer rail-eligible pieces; OSM fallback has no GIP flags → keep all.
        src = str(road.get("source") or "").lower()
        if src == "gip" or road.get("objekt") is not None or road.get("str_code") is not None:
            if is_gip_tunnel_segment(road):
                continue
            # Ramps skip their own rails but stay in the clip set so the
            # trunk opens at the merge (Einfahrt).
            if gip_skip_guardrails(road) and not gip_is_subordinate_lane(road):
                continue
        nodes = road.get("nodes") or []
        axis = _axis_from_road_nodes(nodes)
        if axis is None:
            continue
        xy, half = axis
        # Precompute segment tangents for collinear (same-corridor) skip.
        tans: list[tuple[float, float]] = []
        for i in range(len(xy) - 1):
            dx = xy[i + 1][0] - xy[i][0]
            dy = xy[i + 1][1] - xy[i][1]
            L = math.hypot(dx, dy) or 1.0
            tans.append((dx / L, dy / L))
        if not tans:
            continue
        oid = road.get("objectid")
        if oid is None:
            oid = road.get("osm_id")
        axes.append(
            {
                "id": oid,
                "id_s": str(oid) if oid is not None else None,
                "xy": xy,
                "half_w": half,
                "tans": tans,
                "str_code": str(road.get("str_code") or ""),
            }
        )
    return axes


def _build_tunnel_mountain_corridors(
    roads: list[dict],
) -> list[tuple[list[tuple[float, float]], float]]:
    """XY bands over buried tunnels — no rails on the mountain above.

    Open galleries stay on ``galleries_centerlines`` portal clip; here only
    true tunnels / Unterflur (S-BT, S-AT, name/kunstbauten), so surface
    joints above the bore die.
    """
    if not CLIP_TUNNELS:
        return []
    pad = max(0.0, TUNNEL_CLIP_PAD_M)
    out: list[tuple[list[tuple[float, float]], float]] = []
    for road in roads:
        kunst = str(road.get("kunstbauten") or "").lower()
        objekt = str(road.get("objekt") or "").upper()
        if objekt == "S-BG" or "galerie" in kunst:
            continue
        if not is_gip_tunnel_segment(road):
            continue
        axis = _axis_from_road_nodes(road.get("nodes") or [])
        if axis is None:
            continue
        xy, half = axis
        out.append((xy, half + pad))
    return out


def _project_xy_to_axis(
    bx: float,
    by: float,
    xy: list[tuple[float, float]],
    tans: list[tuple[float, float]],
) -> tuple[float, float, float] | None:
    """Nearest point on axis → (dist_m, tx, ty) with segment tangent."""
    if len(xy) < 2 or len(tans) < 1:
        return None
    best: tuple[float, float, float] | None = None
    for i in range(len(xy) - 1):
        x0, y0 = xy[i]
        x1, y1 = xy[i + 1]
        dx, dy = x1 - x0, y1 - y0
        seg2 = dx * dx + dy * dy
        if seg2 < 1e-12:
            d = math.hypot(bx - x0, by - y0)
            tx, ty = tans[min(i, len(tans) - 1)]
        else:
            t = max(0.0, min(1.0, ((bx - x0) * dx + (by - y0) * dy) / seg2))
            px = x0 + t * dx
            py = y0 + t * dy
            d = math.hypot(bx - px, by - py)
            tx, ty = tans[i]
        if best is None or d < best[0]:
            best = (d, tx, ty)
    return best


def _joint_blocks_foreign_centerline(
    px: float,
    py: float,
    axes: list[dict],
    *,
    self_id,
    self_tx: float = 0.0,
    self_ty: float = 0.0,
) -> bool:
    """True if joint sits inside another road's carriageway at a crossing.

    Near-collinear foreign axes (same corridor / abutting pieces) are ignored.
    Mouth opening (side toward foreign) is handled by ``open_junctions`` post-pass.
    """
    if not axes:
        return False
    pad = max(0.0, CENTERLINE_CLIP_PAD_M)
    self_s = str(self_id) if self_id is not None else None
    st = math.hypot(self_tx, self_ty)
    if st > 1e-9:
        self_tx, self_ty = self_tx / st, self_ty / st
    else:
        self_tx = self_ty = 0.0
    colin = 0.90
    for ax in axes:
        if self_s is not None and ax.get("id_s") == self_s:
            continue
        if self_id is not None and ax.get("id") == self_id:
            continue
        half = float(ax.get("half_w") or 3.75) + pad
        proj = _project_xy_to_axis(px, py, ax["xy"], ax.get("tans") or [])
        if proj is None:
            continue
        d, ftx, fty = proj
        if d > half:
            continue
        if self_tx or self_ty:
            align = abs(self_tx * ftx + self_ty * fty)
            if align >= colin:
                continue
        return True
    return False


def _split_joints_clear_of_centerlines(
    joints: list[tuple[float, float, float, float, float]],
    axes: list[dict],
    *,
    self_id,
    min_run: int = 2,
) -> list[list[tuple[float, float, float]]]:
    """Drop joints that lie on a foreign carriageway (junction openings).

    Joints are ``(x, y, z, tx, ty)`` with centerline tangent for collinear skip.
    """
    if not joints:
        return []
    if not axes or not CLIP_CENTERLINES:
        out = [(j[0], j[1], j[2]) for j in joints]
        return [out] if len(out) >= min_run else []
    runs: list[list[tuple[float, float, float]]] = []
    cur: list[tuple[float, float, float]] = []
    for j in joints:
        px, py, pz = j[0], j[1], j[2]
        tx = j[3] if len(j) > 3 else 0.0
        ty = j[4] if len(j) > 4 else 0.0
        if _joint_blocks_foreign_centerline(
            px, py, axes, self_id=self_id, self_tx=tx, self_ty=ty
        ):
            if len(cur) >= min_run:
                runs.append(cur)
            cur = []
            continue
        cur.append((px, py, pz))
    if len(cur) >= min_run:
        runs.append(cur)
    return runs


def _offset_joint(
    nodes: list[list[float]],
    dist: float,
    sign: float,
    hm_pack: tuple[np.ndarray, float] | None,
    *,
    lateral_extra_m: float | None = None,
    bridge_decks: list[dict] | None = None,
) -> tuple[float, float, float] | None:
    """Offset using width-at-``dist`` so posts track lane-ramp edges.

    On MeshRoad bridge spans: tuck toward the deck edge and snap Z to the
    deck surface (not the gorge heightmap underneath).
    """
    sample = sample_center_at_s(nodes, dist)
    if sample is None:
        return None
    cx, cy, cz, tx, ty, width = sample
    lat = LATERAL_EXTRA if lateral_extra_m is None else float(lateral_extra_m)
    use_width = float(width)
    on_bridge = None
    if bridge_decks:
        on_bridge = _bridge_deck_at(cx, cy, bridge_decks)
    if on_bridge is not None:
        lat = BRIDGE_LATERAL_EXTRA
        use_width = float(on_bridge[1])
    offset = rail_offset_m(
        use_width, road_width_scale=ROAD_WIDTH_SCALE, lateral_extra_m=lat
    )
    txy = math.hypot(tx, ty)
    if txy < 1e-9:
        lnx, lny = -1.0, 0.0
    else:
        lnx, lny = -ty / txy, tx / txy
    px = cx + lnx * sign * offset
    py = cy + lny * sign * offset
    if on_bridge is not None and BRIDGE_DECK_Z:
        # Z at the rail position (outer strip / crossfall), fallback axis Z.
        at_rail = _bridge_deck_at(px, py, bridge_decks) or on_bridge
        pz = float(at_rail[0]) + Z_LIFT + PIVOT_GROUND_OFFSET
    else:
        z_off = max(0.0, offset - Z_SAMPLE_INWARD)
        zx = cx + lnx * sign * z_off
        zy = cy + lny * sign * z_off
        if hm_pack is not None:
            hm, max_h = hm_pack
            pz = _heightmap_z(hm, max_h, zx, zy) + Z_LIFT + PIVOT_GROUND_OFFSET
        else:
            pz = cz + Z_LIFT + PIVOT_GROUND_OFFSET
    return (px, py, pz)


def build_entries_heuristic(roads: dict) -> list[dict]:
    hm_pack = _load_heightmap_z() if SNAP_TO_HEIGHTMAP else None
    # Always try heightmap for steep-side probes (even if Z-snap off).
    if hm_pack is None and is_steep_sides(SIDES):
        hm_pack = _load_heightmap_z()
    entries: list[dict] = []
    curve_stats = {"straight": 0, "r15": 0, "r10": 0}
    corridors = _load_gallery_exclusion_corridors()
    bridge_decks = _load_bridge_decks_for_rails()
    bridge_hits = 0
    clipped_runs = 0
    rule_hits = 0
    skipped_rules = 0
    skipped_tunnel = 0
    skipped_minor = 0
    steep_counts = {"left": 0, "right": 0, "both": 0, "none": 0, "fallback": 0}
    oid_decisions: list[str] = []

    use_gip = CENTERLINE_MODE in ("gip", "verkehrswege", "objectid", "oid")
    use_sn = CENTERLINE_MODE in (
        "strassennetz",
        "sn",
        "net",
        "strasse",
        "straßennetz",
    )
    # Default for strassennetz: slice by GIP OIDs so rules/steep stay per-segment.
    sn_slice = bool(GR.get("gip_slices", True))
    if use_sn:
        try:
            if sn_slice:
                roads = load_strassennetz_sliced_by_gip(SITE)
            else:
                roads = load_strassennetz_roads_dict(SITE)
        except SystemExit as ex:
            print(f"Straßennetz unavailable ({ex}) — falling back to OSM")
            use_sn = False
    elif use_gip:
        try:
            roads = load_gip_road_segments(SITE)
        except SystemExit as ex:
            print(f"GIP centerline unavailable ({ex}) — falling back to OSM roads_beamng")
            use_gip = False

    if use_sn or use_gip:
        prepared = []
        for road in roads.values():
            nodes = road.get("nodes") or []
            if len(nodes) < 2:
                continue
            prepared.append(dict(road))
        src_label = (
            "strassennetz+gip"
            if use_sn and sn_slice
            else ("strassennetz" if use_sn else "gip")
        )
        print(
            f"Rail edge prep: centerline={src_label} segments={len(prepared)} "
            f"(no OSM stitch) rules={len(RAIL_RULES)} "
            f"steep_sample_m={STEEP_SAMPLE_M}"
        )
    else:
        prepared = prepare_roads_for_edge(
            roads,
            highways=HIGHWAYS,
            stitch_abutting=STITCH_ABUTTING,
            stitch_tol_m=STITCH_TOL_M,
            width_fill_dip_m=WIDTH_FILL_DIP_M,
            width_blend_m=WIDTH_BLEND_M,
            densify_max_step_m=DENSIFY_MAX_STEP_M,
        )
        prepared = enrich_prepared_roads(prepared, roads, lane_width_m=LANE_WIDTH_M)
        print(
            f"Rail edge prep: centerline=osm roads={len(prepared)} stitch={STITCH_ABUTTING} "
            f"tol_m={STITCH_TOL_M} fill_dip_m={WIDTH_FILL_DIP_M} "
            f"blend_m={WIDTH_BLEND_M} densify_m={DENSIFY_MAX_STEP_M} "
            f"rules={len(RAIL_RULES)}"
        )

    cl_axes = _build_centerline_clip_axes(prepared)
    tunnel_corridors = _build_tunnel_mountain_corridors(prepared)
    # Gallery portal bands + tunnel mountain bands share one exclusion pass.
    struct_corridors = list(corridors) + list(tunnel_corridors)
    cl_clipped_runs = 0
    tunnel_clipped_runs = 0
    junc_opened = 0
    junctions: list[dict] = []
    if OPEN_JUNCTIONS:
        j_axes = build_junction_axes(
            prepared,
            skip_tunnel=is_gip_tunnel_segment,
            skip_minor=gip_skip_guardrails,
        )
        junctions = find_junctions(
            j_axes,
            join_m=JUNCTION_JOIN_M,
            z_sep_m=JUNCTION_Z_SEP_M,
        )
        print(
            f"Junction open: partners={len(j_axes)} meetings={len(junctions)} "
            f"join_m={JUNCTION_JOIN_M} mouth_m={JUNCTION_MOUTH_M} "
            f"z_sep_m={JUNCTION_Z_SEP_M}"
        )

    def _z_at(bx: float, by: float) -> float:
        assert hm_pack is not None
        hm, max_h = hm_pack
        return _heightmap_z(hm, max_h, bx, by)

    for road in prepared:
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        oid = road.get("objectid") or road.get("osm_id")
        if (use_gip or use_sn) and is_gip_tunnel_segment(road):
            skipped_tunnel += 1
            oid_decisions.append(f"{oid}:skip_tunnel")
            continue
        if (use_gip or use_sn) and gip_skip_guardrails(road):
            skipped_minor += 1
            oid_decisions.append(f"{oid}:skip_minor")
            continue

        cfg = resolve_rail_cfg(
            road,
            defaults=RAIL_DEFAULTS,
            rules=RAIL_RULES,
            lane_width_m=LANE_WIDTH_M,
        )
        if cfg.get("present") is False or cfg.get("sides") == "none":
            skipped_rules += 1
            oid_decisions.append(f"{oid}:none(rule)")
            continue

        sides_mode = str(cfg.get("sides") or SIDES)
        if is_steep_sides(sides_mode):
            if hm_pack is None:
                sides_mode = STEEP_FALLBACK
                steep_counts["fallback"] += 1
            else:
                sides_mode = detect_steep_sides(
                    nodes,
                    _z_at,
                    sample_m=STEEP_SAMPLE_M,
                    look_out_m=STEEP_LOOK_OUT_M,
                    drop_m=STEEP_DROP_M,
                    road_width_scale=ROAD_WIDTH_SCALE,
                    min_hits=STEEP_MIN_HITS,
                )
                steep_counts[sides_mode] = steep_counts.get(sides_mode, 0) + 1
            cfg = dict(cfg)
            cfg["sides"] = sides_mode

        oid_decisions.append(f"{oid}:{cfg.get('sides')}")

        if cfg.get("sides") != SIDES or (
            cfg.get("lateral_extra_m") is not None
            and float(cfg["lateral_extra_m"]) != LATERAL_EXTRA
        ):
            rule_hits += 1

        if cfg.get("present") is False or cfg.get("sides") == "none":
            skipped_rules += 1
            continue

        sides = rule_side_signs(cfg.get("sides") or SIDES)
        if not sides:
            skipped_rules += 1
            continue
        lat = float(
            cfg["lateral_extra_m"]
            if cfg.get("lateral_extra_m") is not None
            else LATERAL_EXTRA
        )
        spacing = float(
            cfg.get("spacing_m") or cfg.get("post_spacing_m") or SPACING_M
        )
        section = float(cfg.get("section_length_m") or SECTION_LEN)
        styles = normalize_styles(
            cfg.get("styles") or cfg.get("style") or STYLE_DEFAULTS,
            default=STYLE_DEFAULTS,
        )

        for style_kind in styles:
            is_posts = style_kind == "posts"
            step = spacing if is_posts else section
            shape = (POST_SHAPE or SHAPE) if is_posts else SECTION_SHAPE
            for side_name, sign in sides:
                dists = (
                    _post_dists(nodes, step) if is_posts else _joint_dists(nodes, step)
                )
                if len(dists) < (1 if is_posts else 2):
                    continue
                joints: list[tuple[float, float, float, float, float]] = []
                for d in dists:
                    j = _offset_joint(
                        nodes,
                        d,
                        sign,
                        hm_pack,
                        lateral_extra_m=lat,
                        bridge_decks=bridge_decks,
                    )
                    if not j:
                        continue
                    sample = sample_center_at_s(nodes, d)
                    tx = ty = 0.0
                    if sample is not None:
                        tx, ty = float(sample[3]), float(sample[4])
                    if bridge_decks and _bridge_deck_at(j[0], j[1], bridge_decks):
                        bridge_hits += 1
                    joints.append((j[0], j[1], j[2], tx, ty))
                min_run = 1 if is_posts else 2
                if len(joints) < min_run:
                    continue
                runs = _split_joints_outside_corridors(
                    joints, struct_corridors, min_run=min_run
                )
                if tunnel_corridors and len(runs) != 1:
                    tunnel_clipped_runs += max(0, len(runs))
                if corridors and len(runs) != 1:
                    clipped_runs += max(0, len(runs))
                cleared: list[list[tuple[float, float, float]]] = []
                self_xy = [(float(n[0]), float(n[1])) for n in nodes]
                for run in runs:
                    rich_parts: list[list[tuple]] = [run]
                    if junctions and OPEN_JUNCTIONS:
                        before_n = len(run)
                        kept = filter_joints_open_junctions(
                            run,
                            side_name=side_name,
                            self_id=oid,
                            self_xy=self_xy,
                            junctions=junctions,
                            mouth_m=JUNCTION_MOUTH_M,
                        )
                        dropped = before_n - len(kept)
                        if dropped > 0:
                            junc_opened += dropped
                            rich_parts = _contiguous_rich_runs(run, kept, min_run=min_run)
                        else:
                            rich_parts = [run] if len(kept) >= min_run else []
                    for rich in rich_parts:
                        parts = _split_joints_clear_of_centerlines(
                            rich, cl_axes, self_id=oid, min_run=min_run
                        )
                        if cl_axes and CLIP_CENTERLINES and len(parts) != 1:
                            cl_clipped_runs += max(0, len(parts))
                        cleared.extend(parts)
                for run in cleared:
                    rail_nodes = [[x, y, z] for x, y, z in run]
                    entries.extend(
                        _entries_along_rail(
                            rail_nodes,
                            side_name,
                            step,
                            curve_stats,
                            resample=False,
                            style=style_kind,
                            shape=shape,
                        )
                    )

    if is_steep_sides(SIDES) or any(
        is_steep_sides(str(r.get("sides") or "")) for r in RAIL_RULES if isinstance(r, dict)
    ):
        print(
            f"Steep-side probe: sample_m={STEEP_SAMPLE_M} look_out_m={STEEP_LOOK_OUT_M} "
            f"drop_m={STEEP_DROP_M} hits={steep_counts}"
        )
    if use_gip or use_sn:
        # Compact decision dump (readable in PowerShell)
        print(f"OID side decisions ({len(oid_decisions)}):")
        line = []
        for item in oid_decisions:
            line.append(item)
            if len(line) >= 8:
                print("  " + ", ".join(line))
                line = []
        if line:
            print("  " + ", ".join(line))
        if skipped_tunnel:
            print(f"Skipped GIP tunnels/galleries: {skipped_tunnel}")
        if skipped_minor:
            print(
                f"Skipped GIP minor/parking/ramp rails: {skipped_minor}"
            )
    if RAIL_RULES:
        print(
            f"Rail rules: applied_overrides~{rule_hits} "
            f"skipped_none={skipped_rules}"
        )
    if bridge_decks:
        print(
            f"Bridge decks: spans={len(bridge_decks)} "
            f"joints_on_deck~{bridge_hits} "
            f"lateral_extra_m={BRIDGE_LATERAL_EXTRA} deck_z={BRIDGE_DECK_Z}"
        )
    if tunnel_corridors:
        print(
            f"Tunnel mountain clip: bands={len(tunnel_corridors)} "
            f"pad_m={TUNNEL_CLIP_PAD_M} split_runs~{tunnel_clipped_runs}"
        )
    if junctions and OPEN_JUNCTIONS:
        print(
            f"Junction mouth open: meetings={len(junctions)} "
            f"joints_dropped~{junc_opened} mouth_m={JUNCTION_MOUTH_M} "
            f"(side toward foreign only; z_sep_m={JUNCTION_Z_SEP_M})"
        )
    if cl_axes and CLIP_CENTERLINES:
        print(
            f"Centerline junction clip: axes={len(cl_axes)} "
            f"pad_m={CENTERLINE_CLIP_PAD_M} split_runs~{cl_clipped_runs}"
        )

    if STYLE == "sections" or "sections" in STYLE_DEFAULTS:
        print(
            f"Heuristic placement mix: straightish={curve_stats['straight']} "
            f"~R15_chords={curve_stats['r15']} ~R10_chords={curve_stats['r10']} "
            f"abut_overlap_m={ABUT_OVERLAP} lateral_extra_m={LATERAL_EXTRA}"
        )
    n_post = sum(1 for e in entries if str(e.get("name") or "").startswith("gr_post_"))
    n_sect = sum(1 for e in entries if str(e.get("name") or "").startswith("gr_sect_"))
    print(
        f"Heuristic placed: posts={n_post} sections={n_sect} "
        f"spacing_m={SPACING_M} lateral_extra_m={LATERAL_EXTRA}"
    )
    if corridors:
        print(
            f"Gallery clip: corridors={len(corridors)} "
            f"stop_before_m={STOP_BEFORE_GALLERY_M} pad_m={GALLERY_CLIP_PAD_M} "
            f"split_runs~{clipped_runs}"
        )
    return entries


def clear_level_guardrails(user_level: Path | None = None) -> Path | None:
    """Wipe SimGroup guardrails/items.level.json (empty group, keep registration)."""
    if user_level is None:
        user_level = (
            Path.home()
            / "AppData"
            / "Local"
            / "BeamNG"
            / "BeamNG.drive"
            / "current"
            / "levels"
            / LEVEL_NAME
        )
    if not user_level.exists():
        print(f"Level folder missing: {user_level}")
        return None
    group_dir = user_level / "main" / "MissionGroup" / "level_objects" / "guardrails"
    group_dir.mkdir(parents=True, exist_ok=True)
    items_path = group_dir / "items.level.json"
    items_path.write_text("", encoding="utf-8")
    # Ensure SimGroup exists under level_objects
    lo_items = user_level / "main" / "MissionGroup" / "level_objects" / "items.level.json"
    lines = []
    if lo_items.exists():
        lines = [ln for ln in lo_items.read_text(encoding="utf-8").splitlines() if ln.strip()]
    names = set()
    for ln in lines:
        try:
            names.add(json.loads(ln).get("name"))
        except json.JSONDecodeError:
            pass
    if "guardrails" not in names:
        lines.append(
            json.dumps(
                {
                    "name": "guardrails",
                    "class": "SimGroup",
                    "__parent": "level_objects",
                    "enabled": "1",
                },
                separators=(",", ":"),
            )
        )
        lo_items.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Cleared guardrails items: {items_path}")
    return items_path


def write_level_items(entries: list[dict]) -> Path | None:
    user_level = (
        Path.home()
        / "AppData"
        / "Local"
        / "BeamNG"
        / "BeamNG.drive"
        / "current"
        / "levels"
        / LEVEL_NAME
    )
    if not user_level.exists():
        print(f"Level folder missing: {user_level}")
        return None

    # Always wipe first so a partial/old editor save cannot leave orphans in this file.
    clear_level_guardrails(user_level)

    group_dir = user_level / "main" / "MissionGroup" / "level_objects" / "guardrails"
    items_path = group_dir / "items.level.json"
    with items_path.open("w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")
    print(f"Wrote {len(entries)} objects -> {items_path}")
    return items_path


def resolve_entries(roads: dict) -> tuple[list[dict], str]:
    gpkg = annotations_gpkg(SITE)
    mode = SOURCE_MODE
    # GIP / Straßennetz axes: GPKG rails are OSM leftovers — force heuristic
    # unless the user explicitly set guardrail_source: gpkg.
    gip_axes = CENTERLINE_MODE in (
        "gip",
        "verkehrswege",
        "objectid",
        "oid",
        "strassennetz",
        "sn",
        "tiris",
        "gip_sliced",
        "strassennetz_gip",
    )
    if gip_axes and mode == "auto":
        print(
            f"centerline={CENTERLINE_MODE}: forcing heuristic "
            f"(ignore GPKG auto at {gpkg.name})"
        )
        mode = "heuristic"

    if mode == "heuristic":
        return build_entries_heuristic(roads), "heuristic"

    gdf = load_guardrail_features(gpkg)
    has_features = gdf is not None and len(gdf) > 0

    if mode == "gpkg":
        if not has_features:
            raise SystemExit(
                f"annotations.guardrail_source=gpkg but no features in {gpkg}. "
                "Run seed_annotations.py or set guardrail_source: auto."
            )
        return build_entries_from_gpkg(gdf), "gpkg"

    # auto (OSM centerline only)
    if has_features:
        print(f"Using annotations GPKG: {gpkg} ({len(gdf)} guardrail features)")
        return build_entries_from_gpkg(gdf), "gpkg"
    print(f"No guardrail features in {gpkg} — falling back to OSM heuristic")
    return build_entries_heuristic(roads), "heuristic"


def main() -> None:
    global SHAPE, POST_SHAPE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--clear",
        action="store_true",
        help="Only wipe level guardrails/items.level.json (no rebuild)",
    )
    args = ap.parse_args()

    user_level = (
        Path.home()
        / "AppData"
        / "Local"
        / "BeamNG"
        / "BeamNG.drive"
        / "current"
        / "levels"
        / LEVEL_NAME
    )

    if args.clear:
        clear_level_guardrails(user_level if user_level.exists() else None)
        print("Reload the level in BeamNG (do not Save over old duplicates).")
        return

    if not ENABLED:
        print("guardrails.enabled=false — skip")
        return

    roads_path = PROC / "roads_beamng.json"
    if not roads_path.exists():
        raise SystemExit(f"Missing {roads_path} — run build_smoke.py first")

    need_posts = "posts" in STYLE_DEFAULTS
    need_sections = "sections" in STYLE_DEFAULTS
    for r in RAIL_RULES:
        if not isinstance(r, dict):
            continue
        raw = r.get("styles") if r.get("styles") is not None else r.get("style")
        if raw is None:
            continue
        kinds = normalize_styles(raw)
        if "posts" in kinds:
            need_posts = True
        if "sections" in kinds:
            need_sections = True
    if STYLE == "both":
        need_posts = need_sections = True

    if need_posts:
        if not user_level.exists():
            print(f"Level folder missing (will skip inject): {user_level}")
        else:
            shape_l = POST_SHAPE.replace("\\", "/").lower()
            if (
                POST_SHAPE in ("reflector", "reflector.dae")
                or shape_l.endswith("/reflector.dae")
                or ("driver_training" in shape_l and "reflector" in shape_l)
            ):
                POST_SHAPE = vendor_reflector_pack(user_level, LEVEL_NAME)
                SHAPE = POST_SHAPE
        print(
            f"Leitpoller: shape={POST_SHAPE} post_scale={POST_SCALE} "
            f"(~{1.43 * POST_SCALE:.2f} m tall) spacing_m={SPACING_M}"
        )
    if need_sections:
        print(
            f"Guardrail sections: shape={SECTION_SHAPE} "
            f"section_length_m={SECTION_LEN} mesh_length_m={MESH_LENGTH}"
        )
    print(f"Styles enabled: {STYLE_DEFAULTS} (rules may add more)")

    roads = json.loads(roads_path.read_text(encoding="utf-8"))
    entries, source = resolve_entries(roads)
    entries = _dedupe_entries(entries)
    out_json = PROC / "guardrails_items.level.json"
    with out_json.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")

    meta = {
        "count": len(entries),
        "source": source,
        "styles": STYLE_DEFAULTS,
        "style": STYLE,
        "gpkg": str(annotations_gpkg(SITE).relative_to(ROOT)),
        "post_shape": POST_SHAPE if need_posts else None,
        "section_shape": SECTION_SHAPE if need_sections else None,
        "spacing_m": SPACING_M if need_posts else None,
        "post_scale": POST_SCALE if need_posts else None,
        "mesh_length_m": MESH_LENGTH if need_sections else None,
        "section_length_m": SECTION_LEN if need_sections else None,
        "abut_overlap_m": ABUT_OVERLAP,
        "lateral_extra_m": LATERAL_EXTRA,
        "bridge_lateral_extra_m": BRIDGE_LATERAL_EXTRA,
        "bridge_deck_z": BRIDGE_DECK_Z,
        "z_sample_inward_m": Z_SAMPLE_INWARD,
        "face_y_outward": FACE_Y_OUTWARD,
        "yaw_flip_right": YAW_FLIP_RIGHT,
        "open_junctions": OPEN_JUNCTIONS,
        "junction_mouth_m": JUNCTION_MOUTH_M,
        "junction_z_sep_m": JUNCTION_Z_SEP_M,
        "clip_centerlines": CLIP_CENTERLINES,
        "clip_tunnels": CLIP_TUNNELS,
        "centerline": CENTERLINE_MODE,
        "note": "GIP centerline → heuristic; open_junctions opens mouths side-toward-foreign only",
        "sample_scale": entries[0]["scale"] if entries else None,
        "sample_rot_Z": entries[0]["rotationMatrix"][6:9] if entries else None,
    }
    (PROC / "guardrails_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(
        f"Wrote {len(entries)} segments source={source} "
        f"face_y_outward={FACE_Y_OUTWARD} yaw_flip_right={YAW_FLIP_RIGHT}"
    )
    if entries:
        zs = [e["rotationMatrix"][8] for e in entries]
        print(f"rot Zz (should be ~+1 upright): min={min(zs):.3f} max={max(zs):.3f}")

    level_path = write_level_items(entries)
    if level_path:
        print(f"Injected: {level_path}")
        print(
            "Reload the level in BeamNG (File -> Load Level). "
            "If duplicates remain, do NOT Save first - clear with "
            "`python tools/build_guardrails.py --clear`, reload, then rebuild."
        )


if __name__ == "__main__":
    main()
