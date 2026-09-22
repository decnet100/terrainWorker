"""Inject Alpine Roadtrip portal volumes + arrival SpawnSpheres into BeamNG levels.

Reads config/alpine_rt/portals.yaml, writes the Lua-facing portals.json into
the unpacked mod, copies a translucent red unit cube into each involved level,
and injects:

  - TSStatic box (collisionType None) on from_level
  - SpawnSphere alpine_rt_arrive_<gate_id> on to_level (also listed in info.json)
  - TimeOfDay lat/lon from the site bbox (Tyrol sun path)

Usage:
  python tools\\build_portals.py
  python tools\\build_portals.py --no-inject
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import sys
import uuid
import zlib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import SiteCoords  # noqa: E402

USER_LEVELS = (
    Path.home()
    / "AppData"
    / "Local"
    / "BeamNG"
    / "BeamNG.drive"
    / "current"
    / "levels"
)

MOD_DIR = ROOT / "mods" / "autoroad_alpine_rt"
PORTALS_YAML = ROOT / "config" / "alpine_rt" / "portals.yaml"
CUBE_DAE_NAME = "portal_cube.dae"
MAT_NAME = "portal_red"
SITES_DIR = ROOT / "config" / "sites"


def _norm2(x: float, y: float) -> tuple[float, float]:
    n = math.hypot(x, y) or 1.0
    return x / n, y / n


def _sites_by_level() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not SITES_DIR.is_dir():
        return out
    for p in SITES_DIR.glob("*.yaml"):
        site = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        ln = ((site.get("beamng") or {}).get("level_name") or "").strip()
        if ln:
            site["_yaml"] = str(p)
            out[ln] = site
    return out


def _bbox_center_crs(site: dict) -> tuple[float, float]:
    xmin, ymin, xmax, ymax = map(float, site["bbox"])
    return 0.5 * (xmin + xmax), 0.5 * (ymin + ymax)


def _processed_dir(site: dict) -> Path:
    slug = str(site.get("name", "default")).replace(" ", "_")
    return ROOT / "data" / "processed" / slug


def _sample_heightmap_abs_z(site: dict, bx: float, by: float) -> float | None:
    proc = _processed_dir(site)
    meta_path = proc / "heightmap_meta.json"
    if not meta_path.is_file():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    n = int(meta.get("heightmap_size_px") or (site.get("beamng") or {}).get("mask_size") or 0)
    if n <= 0:
        return None
    hm_path = proc / f"heightmap_{n}.png"
    if not hm_path.is_file():
        return None
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return None
    z0 = float(meta.get("z_min_m") or 0.0)
    mh = float(meta.get("max_height_m") or 1.0)
    ext = float(meta.get("terrain_extent_m") or n)
    arr = np.asarray(Image.open(hm_path))
    col = int(max(0, min(n - 1, round(bx / ext * (n - 1)))))
    row = int(max(0, min(n - 1, round((1.0 - by / ext) * (n - 1)))))
    return z0 + float(arr[row, col]) / 65535.0 * mh


def _site_center_abs_z(site: dict, override: float | None = None) -> float:
    if override is not None:
        return float(override)
    raw = site.get("center_z_m")
    if raw is None:
        raw = (site.get("beamng") or {}).get("center_z_m")
    if raw is not None:
        return float(raw)
    sc = SiteCoords(site)
    cx, cy = _bbox_center_crs(site)
    bx, by = sc.crs_to_beamng(cx, cy)
    sampled = _sample_heightmap_abs_z(site, bx, by)
    if sampled is not None:
        return sampled
    meta_path = _processed_dir(site) / "heightmap_meta.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return 0.5 * (float(meta["z_min_m"]) + float(meta["z_max_m"]))
    return 0.0


def _from_z_min(site: dict) -> float:
    meta_path = _processed_dir(site) / "heightmap_meta.json"
    if meta_path.is_file():
        return float(json.loads(meta_path.read_text(encoding="utf-8")).get("z_min_m") or 0.0)
    return 0.0


def _portal_abs_z(site: dict, beamng_z: float) -> float:
    return _from_z_min(site) + float(beamng_z)


def _ballistic_points(
    start: tuple[float, float, float],
    dir_xy: tuple[float, float],
    *,
    length_m: float,
    peak_m: float,
    z_end_delta: float,
    samples: int,
) -> list[list[float]]:
    dx, dy = _norm2(dir_xy[0], dir_xy[1])
    n = max(8, int(samples))
    pts: list[list[float]] = []
    for i in range(n + 1):
        u = i / n
        z = start[2] + u * z_end_delta + 4.0 * peak_m * u * (1.0 - u)
        pts.append(
            [
                round(start[0] + dx * length_m * u, 3),
                round(start[1] + dy * length_m * u, 3),
                round(z, 3),
            ]
        )
    return pts


def compute_gate_arc(
    gate: dict,
    from_site: dict | None,
    to_site: dict | None,
    arc_cfg: dict,
    *,
    target_z_override: float | None = None,
) -> dict | None:
    if from_site is None or to_site is None:
        return None
    sc = SiteCoords(from_site)
    box = gate["box"]
    px, py, pz = box["pos"]
    portal_crs = sc.beamng_to_crs(px, py)
    dest_crs = _bbox_center_crs(to_site)
    heading_crs = (dest_crs[0] - portal_crs[0], dest_crs[1] - portal_crs[1])
    geo_d = math.hypot(heading_crs[0], heading_crs[1]) or 1.0
    dbx = heading_crs[0] * (sc.terrain_extent / sc.bw)
    dby = heading_crs[1] * (sc.terrain_extent / sc.bh)
    dx, dy = _norm2(dbx, dby)
    length_full = math.hypot(dbx, dby)
    # Optional cap for tests; default = full CRS span to dest bbox center.
    length_m = float(arc_cfg["length_m"]) if arc_cfg.get("length_m") is not None else length_full
    length_m = max(50.0, length_m)
    peak_frac = float(arc_cfg.get("peak_frac") if arc_cfg.get("peak_frac") is not None else 0.06)
    peak_min = float(arc_cfg.get("peak_height_m") if arc_cfg.get("peak_height_m") is not None else 400)
    peak_m = max(peak_min, peak_frac * length_m)
    width_m = float(arc_cfg.get("width_m") or 20)
    samples = int(arc_cfg.get("samples") or max(48, int(length_m / 350.0)))
    dest_abs = _site_center_abs_z(to_site, target_z_override)
    # Dest GHA expressed in *this* map's BeamNG Z (same origin as the portal).
    end_z = dest_abs - _from_z_min(from_site)
    start = (px, py, pz + 1.5)
    z_end_delta = end_z - start[2]
    points = _ballistic_points(
        start,
        (dx, dy),
        length_m=length_m,
        peak_m=peak_m,
        z_end_delta=z_end_delta,
        samples=samples,
    )
    return {
        "width_m": width_m,
        "length_m": round(length_m, 1),
        "peak_height_m": round(peak_m, 1),
        "heading": [round(dx, 4), round(dy, 4)],
        "dest_center_crs": [round(dest_crs[0], 1), round(dest_crs[1], 1)],
        "dest_center_z_m": round(dest_abs, 1),
        "end_z_beamng": round(end_z, 2),
        "geo_distance_m": round(geo_d, 1),
        "points": points,
    }


def _rot_matrix_along(tx: float, ty: float) -> list[float]:
    """X = XY tangent, Z = world up, Y = Z×X."""
    xx, xy = _norm2(tx, ty)
    xz = 0.0
    zx, zy, zz = 0.0, 0.0, 1.0
    yx = zy * xz - zz * xy
    yy = zz * xx - zx * xz
    yz = zx * xy - zy * xx
    yn = math.hypot(yx, yy, yz) or 1.0
    yx, yy, yz = yx / yn, yy / yn, yz / yn
    return [xx, xy, xz, yx, yy, yz, zx, zy, zz]


def _read_ndjson(path: Path) -> list[dict]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    rows: list[dict] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _write_ndjson(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")


def load_portals_yaml(path: Path | None = None) -> dict:
    p = path or PORTALS_YAML
    if not p.is_file():
        raise SystemExit(f"Missing portal graph: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    gates = data.get("gates") or []
    if not gates:
        raise SystemExit(f"No gates in {p}")
    default_dwell = float(data.get("dwell_s_default") or 5)
    default_size = list(data.get("box_size_m") or [12.0, 8.0, 6.0])
    grace_s = float(data.get("grace_s") or 8)
    arc_cfg = dict(data.get("arc") or {})
    sites = _sites_by_level()
    out_gates = []
    for g in gates:
        box = dict(g.get("box") or {})
        arrive = dict(g.get("arrive") or {})
        size = list(box.get("size") or default_size)
        if len(size) < 3:
            size = (size + default_size)[:3]
        tang = list(box.get("tangent") or [1.0, 0.0])
        atang = list(arrive.get("tangent") or tang)
        tx, ty = _norm2(float(tang[0]), float(tang[1]))
        ax, ay = _norm2(float(atang[0]), float(atang[1]))
        pos = [float(x) for x in (box.get("pos") or [0, 0, 0])]
        apos = [float(x) for x in (arrive.get("pos") or [0, 0, 0])]
        gate = {
            "id": str(g["id"]),
            "from_level": str(g["from_level"]),
            "to_level": str(g["to_level"]),
            "label": str(g.get("label") or g["id"]),
            "dwell_s": float(g.get("dwell_s") if g.get("dwell_s") is not None else default_dwell),
            "box": {
                "pos": [round(pos[0], 3), round(pos[1], 3), round(pos[2], 3)],
                "tangent": [round(tx, 4), round(ty, 4)],
                "size": [round(float(size[0]), 3), round(float(size[1]), 3), round(float(size[2]), 3)],
            },
            "arrive": {
                "pos": [round(apos[0], 3), round(apos[1], 3), round(apos[2], 3)],
                "tangent": [round(ax, 4), round(ay, 4)],
            },
        }
        z_over = g.get("target_z_m")
        if z_over is None:
            z_over = (g.get("arc") or {}).get("target_z_m")
        arc = compute_gate_arc(
            gate,
            sites.get(gate["from_level"]),
            sites.get(gate["to_level"]),
            arc_cfg,
            target_z_override=float(z_over) if z_over is not None else None,
        )
        if arc:
            arc["object"] = f"alpine_rt_arc_{gate['id']}"
            gate["arc"] = arc
            gate["dwell_s"] = max(1.0, round(float(arc["geo_distance_m"]) / 1000.0, 1))
            print(
                f"Arc {gate['id']}: heading={arc['heading']} "
                f"geo={arc['geo_distance_m']:.0f}m peak={arc['peak_height_m']:.0f}m "
                f"dest_z={arc['dest_center_z_m']} dwell={gate['dwell_s']}s"
            )
        else:
            print(f"Arc {gate['id']}: skipped (missing site YAML for level)")
        out_gates.append(gate)
    return {"grace_s": grace_s, "gates": out_gates}


def portals_json_payload(cfg: dict) -> dict:
    return {
        "grace_s": cfg["grace_s"],
        "gates": cfg["gates"],
    }


def write_rgba_png(path: Path, rgba: tuple[int, int, int, int], size: int = 4) -> None:
    """Tiny uncompressed-ish RGBA PNG so BeamNG translucent materials get real alpha."""
    r, g, b, a = rgba
    w = h = max(1, int(size))
    raw = b"".join(b"\x00" + bytes([r, g, b, a]) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def _tri_cross(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
) -> tuple[float, float, float]:
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    return (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)


def _orient_faces(
    verts: list[tuple[float, float, float]],
    faces: list[tuple[int, int, int]],
    *,
    toward: str = "centroid",
) -> list[tuple[int, int, int]]:
    """CCW so (b-a)×(c-a) points outward (centroid) or toward +Z."""
    out: list[tuple[int, int, int]] = []
    for a, b, c in faces:
        n = _tri_cross(verts[a], verts[b], verts[c])
        if toward == "plus_z":
            want = n[2]
        else:
            cx = (verts[a][0] + verts[b][0] + verts[c][0]) / 3.0
            cy = (verts[a][1] + verts[b][1] + verts[c][1]) / 3.0
            cz = (verts[a][2] + verts[b][2] + verts[c][2]) / 3.0
            want = n[0] * cx + n[1] * cy + n[2] * cz
        if want < 0:
            a, b, c = a, c, b
        out.append((a, b, c))
    return out


def _expand_unique(
    verts: list[tuple[float, float, float]],
    faces: list[tuple[int, int, int]],
) -> tuple[
    list[tuple[float, float, float]],
    list[tuple[float, float, float]],
    list[tuple[int, int, int]],
]:
    """One vertex+normal per corner so hard edges don't share lighting."""
    out_v: list[tuple[float, float, float]] = []
    out_n: list[tuple[float, float, float]] = []
    out_f: list[tuple[int, int, int]] = []
    for a, b, c in faces:
        n = _tri_cross(verts[a], verts[b], verts[c])
        ln = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2) or 1.0
        n = (n[0] / ln, n[1] / ln, n[2] / ln)
        i = len(out_v)
        out_v.extend((verts[a], verts[b], verts[c]))
        out_n.extend((n, n, n))
        out_f.append((i, i + 1, i + 2))
    return out_v, out_n, out_f


def write_cube_dae(path: Path) -> None:
    """Centered unit cube [-0.5, 0.5]^3, Z-up, outward faces, unique verts."""
    verts = [
        (-0.5, -0.5, -0.5),
        (0.5, -0.5, -0.5),
        (0.5, 0.5, -0.5),
        (-0.5, 0.5, -0.5),
        (-0.5, -0.5, 0.5),
        (0.5, -0.5, 0.5),
        (0.5, 0.5, 0.5),
        (-0.5, 0.5, 0.5),
    ]
    faces = [
        (0, 1, 2),
        (0, 2, 3),
        (4, 6, 5),
        (4, 7, 6),
        (0, 5, 1),
        (0, 4, 5),
        (1, 6, 2),
        (1, 5, 6),
        (2, 7, 3),
        (2, 6, 7),
        (3, 4, 0),
        (3, 7, 4),
    ]
    _write_mesh_dae(
        path,
        verts,
        faces,
        mat=MAT_NAME,
        lod_name="portal_cube_a999",
        rgba=(1.0, 0.22, 0.16, 0.16),
        toward="centroid",
    )


def _write_mesh_dae(
    path: Path,
    verts: list[tuple[float, float, float]],
    faces: list[tuple[int, int, int]],
    *,
    mat: str,
    lod_name: str,
    rgba: tuple[float, float, float, float] = (1.0, 0.4, 0.28, 0.1),
    toward: str = "centroid",
) -> None:
    faces = _orient_faces(verts, faces, toward=toward)
    verts, norms, faces = _expand_unique(verts, faces)
    pos_vals = " ".join(f"{x:.4f} {y:.4f} {z:.4f}" for x, y, z in verts)
    nrm_vals = " ".join(f"{x:.4f} {y:.4f} {z:.4f}" for x, y, z in norms)
    uv_vals = " ".join("0 0" for _ in verts)
    p_vals = " ".join(f"{a} {b} {c}" for a, b, c in faces)
    n_tri = len(faces)
    n_vert = len(verts)
    r, g, b, a = rgba
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor><authoring_tool>beamng_autoroad build_portals</authoring_tool></contributor>
    <unit name="meter" meter="1"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_effects>
    <effect id="{mat}-effect">
      <profile_COMMON>
        <technique sid="common">
          <lambert>
            <diffuse><color>{r} {g} {b} {a}</color></diffuse>
            <transparent opaque="A_ONE"><color>1 1 1 1</color></transparent>
            <transparency><float>{1.0 - a:.2f}</float></transparency>
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
        <source id="{lod_name}-mesh-normals">
          <float_array id="{lod_name}-mesh-normals-array" count="{n_vert * 3}">{nrm_vals}</float_array>
          <technique_common>
            <accessor source="#{lod_name}-mesh-normals-array" count="{n_vert}" stride="3">
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
          <input semantic="NORMAL" source="#{lod_name}-mesh-normals" offset="0"/>
          <input semantic="TEXCOORD" source="#{lod_name}-mesh-map-0" offset="0" set="0"/>
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


def write_arc_ribbon_dae(path: Path, points: list[list[float]], width_m: float) -> None:
    """Ribbon in world metres, origin at first point (TSStatic position = start)."""
    if len(points) < 2:
        return
    hw = max(0.5, float(width_m) * 0.5)
    sx, sy, sz = points[0]
    verts: list[tuple[float, float, float]] = []
    for i, p in enumerate(points):
        if i < len(points) - 1:
            tx, ty = points[i + 1][0] - p[0], points[i + 1][1] - p[1]
        else:
            tx, ty = p[0] - points[i - 1][0], p[1] - points[i - 1][1]
        rx, ry = _norm2(-ty, tx)
        lx, ly, lz = p[0] - sx, p[1] - sy, p[2] - sz
        verts.append((lx - rx * hw, ly - ry * hw, lz))
        verts.append((lx + rx * hw, ly + ry * hw, lz))
    faces: list[tuple[int, int, int]] = []
    nseg = len(points) - 1
    for i in range(nseg):
        a, b, c, d = i * 2, i * 2 + 1, i * 2 + 2, i * 2 + 3
        faces.append((a, c, b))
        faces.append((b, c, d))
    _write_mesh_dae(
        path,
        verts,
        faces,
        mat="portal_arc",
        lod_name="portal_arc_a999",
        rgba=(1.0, 0.40, 0.28, 0.18),
        toward="plus_z",
    )


def _translucent_mat(
    name: str,
    persistent_id: str,
    png: str,
    *,
    roughness: float,
    opacity: float,
) -> dict:
    # 0.39 PBR: opacityFactor, not baseColor alpha. PreMulAlpha = glass.
    return {
        "name": name,
        "mapTo": name,
        "class": "Material",
        "persistentId": persistent_id,
        "Stages": [
            {
                "baseColorMap": png,
                "baseColorFactor": [1.0, 0.30, 0.20, 1.0],
                "opacityFactor": float(opacity),
                "emissiveFactor": [0.0, 0.0, 0.0],
                "roughnessFactor": roughness,
                "metallicFactor": 0.0,
            },
            {},
            {},
            {},
        ],
        "translucent": True,
        "translucentBlendOp": "PreMulAlpha",
        "translucentRecvShadows": False,
        "alphaTest": False,
        "castShadows": False,
        "doubleSided": True,
        "version": 1.5,
    }


def portal_material_dict(tex_dir: str = "") -> dict:
    prefix = tex_dir.rstrip("/") + "/" if tex_dir else ""
    return {
        MAT_NAME: _translucent_mat(
            MAT_NAME,
            "a11e7101-71e0-4ead-b100-0000ffffff71",
            f"{prefix}portal_ghost.png",
            roughness=0.85,
            opacity=0.14,
        ),
        "portal_arc": _translucent_mat(
            "portal_arc",
            "a11e7101-71e0-4ead-b100-0000ffffff72",
            f"{prefix}portal_arc.png",
            roughness=0.9,
            opacity=0.35,
        ),
    }


def write_portal_material(mats_path: Path, tex_dir: str = "") -> None:
    data: dict = {}
    if mats_path.is_file() and mats_path.stat().st_size:
        try:
            data = json.loads(mats_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    data.update(portal_material_dict(tex_dir))
    mats_path.parent.mkdir(parents=True, exist_ok=True)
    mats_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def ensure_mod_art(cfg: dict) -> Path:
    art = MOD_DIR / "art" / "shapes" / "portals"
    write_cube_dae(art / CUBE_DAE_NAME)
    write_rgba_png(art / "portal_ghost.png", (255, 90, 55, 255), size=64)
    write_rgba_png(art / "portal_arc.png", (255, 110, 70, 255), size=64)
    for g in cfg.get("gates") or []:
        arc = g.get("arc") or {}
        pts = arc.get("points") or []
        if len(pts) >= 2:
            write_arc_ribbon_dae(
                art / f"portal_arc_{g['id']}.dae",
                pts,
                float(arc.get("width_m") or 20),
            )
    write_portal_material(art / "main.materials.json", "/art/shapes/portals")
    json_path = MOD_DIR / "lua" / "ge" / "extensions" / "alpine_rt" / "portals.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(portals_json_payload(cfg), indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {json_path.relative_to(ROOT)}")
    return art


def copy_art_to_level(level_name: str, art_src: Path) -> None:
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        print(f"Level folder missing (skip art): {user_level}")
        return
    dest = user_level / "art" / "shapes" / "portals"
    dest.mkdir(parents=True, exist_ok=True)
    for src in art_src.iterdir():
        if src.suffix.lower() in {".dae", ".png"}:
            shutil.copy2(src, dest / src.name)
    write_portal_material(
        dest / "main.materials.json",
        f"/levels/{level_name}/art/shapes/portals",
    )


def _register_simgroup(user_level: Path, name: str) -> None:
    lo_items = user_level / "main" / "MissionGroup" / "level_objects" / "items.level.json"
    rows = _read_ndjson(lo_items)
    names = {r.get("name") for r in rows}
    if name in names:
        return
    rows.append(
        {
            "name": name,
            "class": "SimGroup",
            "__parent": "level_objects",
            "enabled": "1",
            "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"alpine_rt:{name}")),
        }
    )
    _write_ndjson(lo_items, rows)
    print(f"Registered SimGroup {name} under level_objects")


def make_box_tsstatic(level_name: str, gate: dict) -> dict:
    box = gate["box"]
    tx, ty = box["tangent"]
    rot = _rot_matrix_along(tx, ty)
    sx, sy, sz = box["size"]
    gid = gate["id"]
    return {
        "name": f"alpine_rt_box_{gid}",
        "class": "TSStatic",
        "__parent": "alpine_rt_portals",
        "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"alpine_rt:box:{gid}")),
        "position": list(box["pos"]),
        "rotationMatrix": [round(v, 6) for v in rot],
        "scale": [sx, sy, sz],
        "shapeName": f"/levels/{level_name}/art/shapes/portals/{CUBE_DAE_NAME}",
        "collisionType": "None",
        "decalType": "None",
        "castShadows": False,
        "originSort": True,
        "useInstanceRenderData": False,
        "isRenderEnabled": True,
        "hidden": False,
    }


def make_arc_tsstatic(level_name: str, gate: dict) -> dict | None:
    arc = gate.get("arc") or {}
    pts = arc.get("points") or []
    if len(pts) < 2:
        return None
    gid = gate["id"]
    start = pts[0]
    return {
        "name": arc.get("object") or f"alpine_rt_arc_{gid}",
        "class": "TSStatic",
        "__parent": "alpine_rt_portals",
        "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"alpine_rt:arc:{gid}")),
        "position": [start[0], start[1], start[2] - 50000.0],
        "rotationMatrix": [1, 0, 0, 0, 1, 0, 0, 0, 1],
        "scale": [1.0, 1.0, 1.0],
        "shapeName": f"/levels/{level_name}/art/shapes/portals/portal_arc_{gid}.dae",
        "collisionType": "None",
        "decalType": "None",
        "castShadows": False,
        "originSort": True,
        "meshCulling": False,
        "useInstanceRenderData": False,
        "isRenderEnabled": True,
        "hidden": False,
    }


def make_arrive_spawn(gate: dict) -> dict:
    arrive = gate["arrive"]
    tx, ty = arrive["tangent"]
    rot = _rot_matrix_along(tx, ty)
    gid = gate["id"]
    return {
        "name": f"alpine_rt_arrive_{gid}",
        "class": "SpawnSphere",
        "__parent": "PlayerDropPoints",
        "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"alpine_rt:arrive:{gid}")),
        "position": list(arrive["pos"]),
        "rotationMatrix": [round(v, 6) for v in rot],
        "dataBlock": "SpawnSphereMarker",
        "enabled": "1",
        "radius": 1,
    }


def ensure_visible_distance(level_name: str, need_m: float) -> None:
    """LevelInfo.visibleDistance is 7500 on the template — too short for 15 km arcs."""
    user_level = USER_LEVELS / level_name
    path = (
        user_level
        / "main"
        / "MissionGroup"
        / "level_objects"
        / "sky_and_sun"
        / "items.level.json"
    )
    if not path.is_file():
        return
    want = max(7500.0, float(need_m) * 1.15)
    rows = _read_ndjson(path)
    changed = False
    for row in rows:
        if row.get("class") != "LevelInfo":
            continue
        cur = float(row.get("visibleDistance") or 0)
        if want > cur + 1:
            row["visibleDistance"] = round(want, 1)
            changed = True
            print(f"{level_name}: visibleDistance {cur:.0f} -> {want:.0f} m")
    if changed:
        _write_ndjson(path, rows)


def inject_boxes(level_name: str, gates: list[dict]) -> None:
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        print(f"Level folder missing (skip boxes): {user_level}")
        return
    entries = [make_box_tsstatic(level_name, g) for g in gates]
    # Arc mesh is spawned by Lua only while the player is in the box.
    group_dir = user_level / "main" / "MissionGroup" / "level_objects" / "alpine_rt_portals"
    _write_ndjson(group_dir / "items.level.json", entries)
    _register_simgroup(user_level, "alpine_rt_portals")
    print(f"Injected {len(entries)} portal object(s) -> {level_name}")


def _level_short_name(level_name: str, sites: dict[str, dict]) -> str:
    aliases = {
        "autoroad_m28_test": "Hahntennjoch",
        "autoroad_fernpass_8192": "Fernpass",
        "autoroad_fernpass_4096": "Fernpass",
    }
    if level_name in aliases:
        return aliases[level_name]
    site = sites.get(level_name) or {}
    raw = str(site.get("name") or level_name)
    return raw.replace("tirol-", "").replace("-8192", "").replace("-500m", "")


def ensure_named_default_spawn(level_name: str) -> None:
    path = (
        USER_LEVELS
        / level_name
        / "main"
        / "MissionGroup"
        / "PlayerDropPoints"
        / "items.level.json"
    )
    rows = _read_ndjson(path)
    if not rows:
        return
    if any(r.get("name") == "spawns_default" for r in rows):
        return
    for r in rows:
        if r.get("class") == "SpawnSphere" and not r.get("name"):
            r["name"] = "spawns_default"
            _write_ndjson(path, rows)
            print(f"{level_name}: named default spawn spawns_default")
            return


def patch_info_spawn_points(level_name: str, arrive_gates: list[dict], sites: dict[str, dict]) -> None:
    info_path = USER_LEVELS / level_name / "info.json"
    if not info_path.is_file():
        return
    data = json.loads(info_path.read_text(encoding="utf-8"))
    preview = (data.get("previews") or ["template_preview.png"])[0]
    data["supportsTimeOfDay"] = True
    data["defaultSpawnPointName"] = data.get("defaultSpawnPointName") or "spawns_default"
    points = [
        {
            "name": "Default",
            "translationId": "Default",
            "objectname": "spawns_default",
            "preview": preview,
        }
    ]
    seen = {"spawns_default"}
    for g in arrive_gates:
        obj = f"alpine_rt_arrive_{g['id']}"
        if obj in seen:
            continue
        seen.add(obj)
        from_short = _level_short_name(g["from_level"], sites)
        points.append(
            {
                "name": f"Portal ({from_short})",
                "translationId": f"Portal ({from_short})",
                "objectname": obj,
                "preview": preview,
            }
        )
    data["spawnPoints"] = points
    info_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"{level_name}: info.json spawnPoints -> {len(points)}")


def inject_arrivals(level_name: str, gates: list[dict]) -> None:
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        print(f"Level folder missing (skip arrivals): {user_level}")
        return
    path = user_level / "main" / "MissionGroup" / "PlayerDropPoints" / "items.level.json"
    rows = _read_ndjson(path)
    names_new = {f"alpine_rt_arrive_{g['id']}" for g in gates}
    kept = [r for r in rows if r.get("name") not in names_new]
    for g in gates:
        kept.append(make_arrive_spawn(g))
    _write_ndjson(path, kept)
    print(f"Injected {len(gates)} arrival SpawnSphere(s) -> {level_name}")


def patch_level_time_of_day(level_name: str, site: dict | None) -> None:
    if not site:
        print(f"{level_name}: no site YAML, skip TimeOfDay")
        return
    from setup_beamng_level import patch_time_of_day

    patch_time_of_day(USER_LEVELS / level_name, site)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=PORTALS_YAML)
    ap.add_argument(
        "--no-inject",
        action="store_true",
        help="Only write mod portals.json + cube art, do not touch levels",
    )
    args = ap.parse_args()
    cfg = load_portals_yaml(args.config)
    art_src = ensure_mod_art(cfg)
    if args.no_inject:
        return

    sites = _sites_by_level()
    from_levels: dict[str, list[dict]] = {}
    to_levels: dict[str, list[dict]] = {}
    for g in cfg["gates"]:
        from_levels.setdefault(g["from_level"], []).append(g)
        to_levels.setdefault(g["to_level"], []).append(g)

    for level in sorted(set(from_levels) | set(to_levels)):
        copy_art_to_level(level, art_src)
        patch_level_time_of_day(level, sites.get(level))
    for level, gates in from_levels.items():
        inject_boxes(level, gates)
        need = 0.0
        for g in gates:
            arc = g.get("arc") or {}
            need = max(need, float(arc.get("length_m") or 0.0))
        if need > 0:
            ensure_visible_distance(level, need)
    for level, gates in to_levels.items():
        inject_arrivals(level, gates)
        ensure_named_default_spawn(level)
        patch_info_spawn_points(level, gates, sites)


if __name__ == "__main__":
    main()
