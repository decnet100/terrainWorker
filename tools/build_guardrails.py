"""Place BeamNG guardrail TSStatics from annotations GPKG or OSM heuristic.

Preferred source: data/annotations/*.gpkg layer `guardrail` (after seed + QGIS edit).
Fallback: offset from roads_beamng.json when GPKG missing/empty.

Segments span consecutive joints along each rail polyline (Stoß-an-Stoß).
Does NOT write or overwrite the annotations GPKG — use seed_annotations.py --force.
"""
from __future__ import annotations

import json
import math
import sys
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
SHAPE = GR.get(
    "shape",
    "/art/shapes/objects/italy_guardrails_common_section.dae",
)
MESH_LENGTH = float(GR.get("mesh_length_m", 3.1))
SECTION_LEN = float(GR.get("section_length_m", 4.2))
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
# auto | gpkg | heuristic
SOURCE_MODE = str(ANN.get("guardrail_source", GR.get("source", "auto"))).lower()


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


def _entries_along_rail(
    nodes: list[list[float]],
    side_name: str,
    section_len: float,
    curve_stats: dict,
    *,
    resample: bool = True,
) -> list[dict]:
    """Place abutting segments along a rail polyline (BeamNG XY)."""
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
            section_len = float(section) if section is not None and str(section) not in {"", "nan"} else SECTION_LEN
        except (TypeError, ValueError):
            section_len = SECTION_LEN

        for line in _iter_lines(row.geometry):
            nodes = _nodes_from_line_crs(line, hm_pack)
            if len(nodes) < 2:
                continue
            part = _entries_along_rail(nodes, side, section_len, curve_stats)
            if part:
                used += 1
                entries.extend(part)

    print(
        f"GPKG guardrail: features_used~{used} skipped_present=false={skipped} "
        f"segments={len(entries)}"
    )
    print(
        f"Placement mix: straightish={curve_stats['straight']} "
        f"~R15_chords={curve_stats['r15']} ~R10_chords={curve_stats['r10']} "
        f"abut_overlap_m={ABUT_OVERLAP}"
    )
    return entries


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
            dists = _joint_dists(nodes, SECTION_LEN)
            if len(dists) < 2:
                continue
            joints: list[tuple[float, float, float]] = []
            for d in dists:
                j = _offset_joint(nodes, d, sign, offset, hm_pack)
                if j:
                    joints.append(j)
            if len(joints) < 2:
                continue
            # Reuse abutting placer by treating joints as a rail polyline.
            rail_nodes = [[x, y, z] for x, y, z in joints]
            entries.extend(
                _entries_along_rail(
                    rail_nodes, side_name, SECTION_LEN, curve_stats, resample=False
                )
            )

    print(
        f"Heuristic placement mix: straightish={curve_stats['straight']} "
        f"~R15_chords={curve_stats['r15']} ~R10_chords={curve_stats['r10']} "
        f"abut_overlap_m={ABUT_OVERLAP} lateral_extra_m={LATERAL_EXTRA}"
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
    if not ENABLED:
        print("guardrails.enabled=false — skip")
        return

    roads_path = PROC / "roads_beamng.json"
    if not roads_path.exists():
        raise SystemExit(f"Missing {roads_path} — run build_smoke.py first")

    roads = json.loads(roads_path.read_text(encoding="utf-8"))
    entries, source = resolve_entries(roads)
    out_json = PROC / "guardrails_items.level.json"
    with out_json.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")

    meta = {
        "count": len(entries),
        "source": source,
        "gpkg": str(annotations_gpkg(SITE).relative_to(ROOT)),
        "shape": SHAPE,
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
        print("Reload the level in BeamNG.")


if __name__ == "__main__":
    main()
