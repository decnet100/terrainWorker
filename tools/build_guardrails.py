"""Place BeamNG guardrail TSStatics along roads_beamng.json centerlines.

Spacing always matches the *scaled* mesh length so segments abut (no gaps).
Austrian 4.2 m look = scale the ~3.1 m Italy mesh on X. Tight bends use shorter
chords with matching X-scale (stock BeamNG has no R10/R15 bent meshes).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import yaml
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SITE = yaml.safe_load((ROOT / "config" / "site.yaml").read_text(encoding="utf-8"))
PROC = ROOT / "data" / "processed"
BNG = SITE.get("beamng", {})
GR = BNG.get("guardrails", {}) or {}

LEVEL_NAME = BNG.get("level_name", "autoroad_m28_test")
SHAPE = GR.get(
    "shape",
    "/art/shapes/objects/italy_guardrails_common_section.dae",
)
# Native length of italy_guardrails_common_section (AABB ≈ 3.1 m).
MESH_LENGTH = float(GR.get("mesh_length_m", 3.1))
# Desired straight-section length (AT ~4.0–4.3 m) → applied via scale_x.
SECTION_LEN = float(GR.get("section_length_m", 4.2))
CURVE_R15_M = float(GR.get("curve_r15_m", 15.0))
CURVE_R10_M = float(GR.get("curve_r10_m", 10.0))
CURVE_STEP_R15 = float(GR.get("curve_step_r15_m", 2.8))
CURVE_STEP_R10 = float(GR.get("curve_step_r10_m", 2.2))
LATERAL_EXTRA = float(GR.get("lateral_extra_m", 1.0))
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
# Mesh W-beam faces local -Y: point Y outward so -Y aims at the road.
FACE_Y_OUTWARD = bool(GR.get("face_y_outward", True))
# Right side: 180° yaw (keeps Z up) so both roadsides show the beam to traffic.
YAW_FLIP_RIGHT = bool(GR.get("yaw_flip_right", True))
ROAD_WIDTH_SCALE = float(BNG.get("road_width_scale", 1.0))
ENABLED = bool(GR.get("enabled", True))
MPP = float(BNG.get("meters_per_pixel", 1.0))
HM_SIZE = int(BNG.get("mask_size", 512))
TERRAIN_EXTENT = HM_SIZE * MPP
CURVATURE_WINDOW_M = float(GR.get("curvature_window_m", 8.0))


def _norm3(x: float, y: float, z: float) -> tuple[float, float, float]:
    L = math.sqrt(x * x + y * y + z * z)
    if L < 1e-12:
        return (1.0, 0.0, 0.0)
    return (x / L, y / L, z / L)


def _rot_matrix_pitched(tx: float, ty: float, tz: float, y_sign: float) -> list[float]:
    """X = 3D tangent, Z = upright (world-up projected), Y = Z×X * y_sign.

    Important: do NOT rebuild Z from X×Y after flipping Y — that inverts 'up'
    on one roadside and buries the rail.
    """
    xx, xy, xz = _norm3(tx, ty, tz)
    dot = xz  # world_up · T
    zx, zy, zz = _norm3(-xx * dot, -xy * dot, 1.0 - xz * dot)
    # Y = Z × X
    yx = zy * xz - zz * xy
    yy = zz * xx - zx * xz
    yz = zx * xy - zy * xx
    yx, yy, yz = _norm3(yx * y_sign, yy * y_sign, yz * y_sign)
    # Keep Z upright; re-fit X so basis stays orthonormal (X = Y × Z).
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


def _step_for_radius(radius: float) -> float:
    if radius <= CURVE_R10_M:
        return CURVE_STEP_R10
    if radius <= CURVE_R15_M:
        return CURVE_STEP_R15
    if radius < 40.0:
        t = (radius - CURVE_R15_M) / (40.0 - CURVE_R15_M)
        return CURVE_STEP_R15 + t * (SECTION_LEN - CURVE_STEP_R15)
    return SECTION_LEN


def _tangent_between(
    nodes: list[list[float]], d0: float, d1: float
) -> tuple[float, float, float]:
    p0 = _sample_at(nodes, d0)
    p1 = _sample_at(nodes, d1)
    if not p0 or not p1:
        return (1.0, 0.0, 0.0)
    return _norm3(p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2])


def _load_heightmap_z() -> tuple[np.ndarray, float] | None:
    hm_path = PROC / "heightmap_512.png"
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


def _resample_adaptive(
    nodes: list[list[float]],
) -> list[tuple[float, float, float, float, float, float, float]]:
    """(x,y,z, tx,ty,tz, step) — step is chord length; mesh scaled to match."""
    total = _polyline_length_xy(nodes)
    if total < 1.0:
        return []
    samples = []
    first_r = _local_radius_xy(nodes, min(CURVATURE_WINDOW_M, total * 0.5), CURVATURE_WINDOW_M)
    dist = min(_step_for_radius(first_r) * 0.5, total * 0.5)
    while dist < total - 0.05:
        r = _local_radius_xy(nodes, dist, CURVATURE_WINDOW_M)
        step = _step_for_radius(r)
        half = step * 0.5
        p = _sample_at(nodes, dist)
        if p:
            d0 = max(0.0, dist - half)
            d1 = min(total, dist + half)
            tx, ty, tz = _tangent_between(nodes, d0, d1)
            samples.append((p[0], p[1], p[2], tx, ty, tz, step))
        dist += step
    return samples


def _side_signs() -> list[tuple[str, float]]:
    if SIDES == "left":
        return [("left", 1.0)]
    if SIDES == "right":
        return [("right", -1.0)]
    return [("left", 1.0), ("right", -1.0)]


def build_entries(roads: dict) -> list[dict]:
    hm_pack = _load_heightmap_z() if SNAP_TO_HEIGHTMAP else None
    entries: list[dict] = []
    curve_stats = {"straight": 0, "r15": 0, "r10": 0}

    for road in roads.values():
        hw = road.get("highway", "")
        if hw not in HIGHWAYS:
            continue
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        width = float(nodes[0][3]) if len(nodes[0]) > 3 else 6.0
        offset = width * ROAD_WIDTH_SCALE * 0.5 + LATERAL_EXTRA
        samples = _resample_adaptive(nodes)

        for side_name, sign in _side_signs():
            for x, y, z, tx, ty, tz, step in samples:
                if step <= CURVE_STEP_R10 + 1e-6:
                    curve_stats["r10"] += 1
                elif step <= CURVE_STEP_R15 + 1e-6:
                    curve_stats["r15"] += 1
                else:
                    curve_stats["straight"] += 1

                txy = math.hypot(tx, ty)
                if txy < 1e-9:
                    lnx, lny = -1.0, 0.0
                else:
                    lnx, lny = -ty / txy, tx / txy
                px = x + lnx * sign * offset
                py = y + lny * sign * offset
                if hm_pack is not None:
                    hm, max_h = hm_pack
                    pz = _heightmap_z(hm, max_h, px, py) + Z_LIFT + PIVOT_GROUND_OFFSET
                else:
                    pz = z + Z_LIFT + PIVOT_GROUND_OFFSET

                # Y along left-normal * side: outward if FACE_Y_OUTWARD (beam on -Y).
                if FACE_Y_OUTWARD:
                    y_sign = sign  # +left on left side, -left on right → both outward
                else:
                    y_sign = -sign
                if ALIGN_PITCH:
                    rot = _rot_matrix_pitched(tx, ty, tz, y_sign)
                else:
                    rot = _rot_matrix_pitched(tx, ty, 0.0, y_sign)
                if side_name == "right" and YAW_FLIP_RIGHT:
                    rot = _yaw180(rot)

                # Length match so segments abut (positive scales only).
                scale_x = step / MESH_LENGTH if MESH_LENGTH > 1e-6 else 1.0
                sx, sy, sz = scale_x, 1.0, 1.0

                entries.append({
                    "class": "TSStatic",
                    "__parent": "guardrails",
                    "position": [round(px, 3), round(py, 3), round(pz, 3)],
                    "rotationMatrix": [round(v, 6) for v in rot],
                    "scale": [round(sx, 4), round(sy, 4), round(sz, 4)],
                    "shapeName": SHAPE,
                    "collisionType": "Collision Mesh",
                    "decalType": "Collision Mesh",
                    "useInstanceRenderData": True,
                    "isRenderEnabled": True,
                })
    print(
        f"Placement mix: straightish={curve_stats['straight']} "
        f"~R15_chords={curve_stats['r15']} ~R10_chords={curve_stats['r10']}"
    )
    return entries


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

    group_dir = user_level / "main" / "MissionGroup" / "level_objects" / "guardrails"
    group_dir.mkdir(parents=True, exist_ok=True)
    items_path = group_dir / "items.level.json"
    with items_path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")

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
                {"name": "guardrails", "class": "SimGroup", "__parent": "level_objects", "enabled": "1"},
                separators=(",", ":"),
            )
        )
        lo_items.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("Registered SimGroup guardrails under level_objects")
    return items_path


def main() -> None:
    if not ENABLED:
        print("guardrails.enabled=false — skip")
        return

    roads_path = PROC / "roads_beamng.json"
    if not roads_path.exists():
        raise SystemExit(f"Missing {roads_path} — run build_smoke.py first")

    roads = json.loads(roads_path.read_text(encoding="utf-8"))
    entries = build_entries(roads)
    out_json = PROC / "guardrails_items.level.json"
    with out_json.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")

    meta = {
        "count": len(entries),
        "shape": SHAPE,
        "mesh_length_m": MESH_LENGTH,
        "section_length_m": SECTION_LEN,
        "face_y_outward": FACE_Y_OUTWARD,
        "yaw_flip_right": YAW_FLIP_RIGHT,
        "note": "Z stays upright when facing; no scale axis mirrors",
        "lateral_extra_m": LATERAL_EXTRA,
        "sample_scale": entries[0]["scale"] if entries else None,
        "sample_rot_Z": entries[0]["rotationMatrix"][6:9] if entries else None,
    }
    (PROC / "guardrails_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {len(entries)} segments face_y_outward={FACE_Y_OUTWARD} yaw_flip_right={YAW_FLIP_RIGHT}")
    if entries:
        zs = [e["rotationMatrix"][8] for e in entries]
        print(f"rot Zz (should be ~+1 upright): min={min(zs):.3f} max={max(zs):.3f}")

    level_path = write_level_items(entries)
    if level_path:
        print(f"Injected: {level_path}")
        print("Reload the level in BeamNG.")


if __name__ == "__main__":
    main()
