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

SITE = load_site()
PROC = processed_dir(SITE)
BNG = SITE.get("beamng", {})
GR = BNG.get("guardrails", {}) or {}
ANN = SITE.get("annotations") or {}
COORDS = SiteCoords(SITE)

LEVEL_NAME = BNG.get("level_name", "autoroad_m28_test")
# posts = Leitpoller / delineators every spacing_m (default).
# sections = abutting Italy-style guardrail meshes (legacy).
_STYLE_RAW = str(GR.get("style") or "posts").lower().strip()
STYLE = (
    "posts"
    if _STYLE_RAW in ("posts", "post", "leitpoller", "delineator", "markers", "marker")
    else "sections"
)
# Native reflector mesh is ~1.43 m tall; 0.7 ≈ 1.0 m (typical AT Leitpoller).
_DEFAULT_SHAPE = (
    "reflector"  # resolved to vendored local path in main()
    if STYLE == "posts"
    else "/art/shapes/objects/italy_guardrails_common_section.dae"
)
SHAPE = str(GR.get("shape") or _DEFAULT_SHAPE)
MESH_LENGTH = float(GR.get("mesh_length_m", 3.1))
SECTION_LEN = float(GR.get("section_length_m", 4.2))
# Post / Leitpoller spacing along the rail (AT typically 25 or 50 m).
SPACING_M = float(GR.get("spacing_m") or GR.get("post_spacing_m") or 25.0)
POST_SCALE = float(
    GR.get("post_scale") if GR.get("post_scale") is not None else (0.7 if STYLE == "posts" else 1.0)
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
# auto | gpkg | heuristic
SOURCE_MODE = str(ANN.get("guardrail_source", GR.get("source", "auto"))).lower()


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
    hm_path = PROC / f"heightmap_{int(BNG.get('mask_size', 512))}.png"
    meta_path = PROC / "heightmap_meta.json"
    if not hm_path.exists() or not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    max_h = float(meta.get("max_height_m", BNG.get("max_height_m", 254.75)))
    arr = np.array(Image.open(hm_path))
    if arr.dtype != np.uint16:
        arr = arr.astype(np.uint16)
    return arr, max_h


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
    if SIDES == "left":
        return [("left", 1.0)]
    if SIDES == "right":
        return [("right", -1.0)]
    return [("left", 1.0), ("right", -1.0)]


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
            "shapeName": SHAPE,
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
    """Drop near-identical positions (same side stacked from overlapping runs)."""
    if tol_m <= 0 or len(entries) < 2:
        return entries
    kept: list[dict] = []
    for e in entries:
        pos = e.get("position") or [0, 0, 0]
        px, py = float(pos[0]), float(pos[1])
        dup = False
        for k in kept:
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
) -> list[dict]:
    """Place abutting guardrail sections, or Leitpoller posts when style=posts."""
    if STYLE == "posts":
        return _entries_posts_along_rail(
            nodes, side_name, section_len if section_len > 0 else SPACING_M, resample=resample
        )

    if resample:
        dists = _joint_dists(nodes, section_len)
        if len(dists) < 2:
            return []
        joints: list[tuple[float, float, float]] = []
        for d in dists:
            p = _sample_at(nodes, d)
            if p:
                joints.append(p)
    else:
        joints = [(n[0], n[1], n[2]) for n in nodes]
    if len(joints) < 2:
        return []

    entries: list[dict] = []
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
        entries.append({
            "name": _tsstatic_name("sect", px, py, pz, side_name),
            "class": "TSStatic",
            "__parent": "guardrails",
            "position": [round(px, 3), round(py, 3), round(pz, 3)],
            "rotationMatrix": [round(v, 6) for v in rot],
            "scale": [round(scale_x, 4), 1.0, 1.0],
            "shapeName": SHAPE,
            "collisionType": "Collision Mesh",
            "decalType": "Collision Mesh",
            "useInstanceRenderData": True,
            "isRenderEnabled": True,
        })
    return entries


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
                part = _entries_along_rail(rail, side, section_len, curve_stats)
                if part:
                    used += 1
                    entries.extend(part)

    kind = "posts" if STYLE == "posts" else "segments"
    print(
        f"GPKG guardrail: features_used~{used} skipped_present=false={skipped} "
        f"{kind}={len(entries)} style={STYLE}"
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
    if not joints:
        return []
    if not corridors:
        return [joints] if len(joints) >= min_run else []
    runs: list[list[tuple[float, float, float]]] = []
    cur: list[tuple[float, float, float]] = []
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


def _offset_joint(
    nodes: list[list[float]],
    dist: float,
    sign: float,
    offset: float,
    hm_pack: tuple[np.ndarray, float] | None,
) -> tuple[float, float, float] | None:
    total = _polyline_length_xy(nodes)
    p = _sample_at(nodes, dist)
    if not p:
        return None
    d0 = max(0.0, dist - 0.5)
    d1 = min(total, dist + 0.5)
    tx, ty, _tz = _tangent_between(nodes, d0, d1)
    txy = math.hypot(tx, ty)
    if txy < 1e-9:
        lnx, lny = -1.0, 0.0
    else:
        lnx, lny = -ty / txy, tx / txy
    px = p[0] + lnx * sign * offset
    py = p[1] + lny * sign * offset
    z_off = max(0.0, offset - Z_SAMPLE_INWARD)
    zx = p[0] + lnx * sign * z_off
    zy = p[1] + lny * sign * z_off
    if hm_pack is not None:
        hm, max_h = hm_pack
        pz = _heightmap_z(hm, max_h, zx, zy) + Z_LIFT + PIVOT_GROUND_OFFSET
    else:
        pz = p[2] + Z_LIFT + PIVOT_GROUND_OFFSET
    return (px, py, pz)


def build_entries_heuristic(roads: dict) -> list[dict]:
    hm_pack = _load_heightmap_z() if SNAP_TO_HEIGHTMAP else None
    entries: list[dict] = []
    curve_stats = {"straight": 0, "r15": 0, "r10": 0}
    corridors = _load_gallery_exclusion_corridors()
    clipped_runs = 0

    for road in roads.values():
        hw = road.get("highway", "")
        if hw not in HIGHWAYS:
            continue
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        width = float(nodes[0][3]) if len(nodes[0]) > 3 else 6.0
        offset = width * ROAD_WIDTH_SCALE * 0.5 + LATERAL_EXTRA

        for side_name, sign in _side_signs():
            step = SPACING_M if STYLE == "posts" else SECTION_LEN
            dists = (
                _post_dists(nodes, step)
                if STYLE == "posts"
                else _joint_dists(nodes, step)
            )
            if len(dists) < (1 if STYLE == "posts" else 2):
                continue
            joints: list[tuple[float, float, float]] = []
            for d in dists:
                j = _offset_joint(nodes, d, sign, offset, hm_pack)
                if j:
                    joints.append(j)
            min_run = 1 if STYLE == "posts" else 2
            if len(joints) < min_run:
                continue
            runs = _split_joints_outside_galleries(joints, corridors, min_run=min_run)
            if corridors and len(runs) != 1:
                clipped_runs += max(0, len(runs))
            for run in runs:
                rail_nodes = [[x, y, z] for x, y, z in run]
                entries.extend(
                    _entries_along_rail(
                        rail_nodes, side_name, step, curve_stats, resample=False
                    )
                )

    if STYLE == "sections":
        print(
            f"Heuristic placement mix: straightish={curve_stats['straight']} "
            f"~R15_chords={curve_stats['r15']} ~R10_chords={curve_stats['r10']} "
            f"abut_overlap_m={ABUT_OVERLAP} lateral_extra_m={LATERAL_EXTRA}"
        )
    else:
        print(
            f"Heuristic Leitpoller: posts={len(entries)} spacing_m={SPACING_M} "
            f"lateral_extra_m={LATERAL_EXTRA} shape={SHAPE}"
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

    # auto
    if has_features:
        print(f"Using annotations GPKG: {gpkg} ({len(gdf)} guardrail features)")
        return build_entries_from_gpkg(gdf), "gpkg"
    print(f"No guardrail features in {gpkg} — falling back to OSM heuristic")
    return build_entries_heuristic(roads), "heuristic"


def main() -> None:
    global SHAPE
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

    if STYLE == "posts":
        # Always vendor into the level so materials register (cross-level refs don't).
        if not user_level.exists():
            print(f"Level folder missing (will skip inject): {user_level}")
        else:
            shape_l = SHAPE.replace("\\", "/").lower()
            if (
                SHAPE in ("reflector", "reflector.dae")
                or shape_l.endswith("/reflector.dae")
                or "driver_training" in shape_l and "reflector" in shape_l
            ):
                SHAPE = vendor_reflector_pack(user_level, LEVEL_NAME)
        print(
            f"Leitpoller: shape={SHAPE} post_scale={POST_SCALE} "
            f"(~{1.43 * POST_SCALE:.2f} m tall) spacing_m={SPACING_M}"
        )

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
        "style": STYLE,
        "gpkg": str(annotations_gpkg(SITE).relative_to(ROOT)),
        "shape": SHAPE,
        "spacing_m": SPACING_M if STYLE == "posts" else None,
        "post_scale": POST_SCALE if STYLE == "posts" else None,
        "mesh_length_m": MESH_LENGTH,
        "section_length_m": SECTION_LEN,
        "abut_overlap_m": ABUT_OVERLAP,
        "lateral_extra_m": LATERAL_EXTRA,
        "z_sample_inward_m": Z_SAMPLE_INWARD,
        "face_y_outward": FACE_Y_OUTWARD,
        "yaw_flip_right": YAW_FLIP_RIGHT,
        "note": "GPKG preferred when guardrail layer non-empty; build never writes GPKG",
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
            "Reload the level in BeamNG (File→Load Level). "
            "If duplicates remain, do NOT Save first — clear with "
            "`python tools/build_guardrails.py --clear`, reload, then rebuild."
        )


if __name__ == "__main__":
    main()
