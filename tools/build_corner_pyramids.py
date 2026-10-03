"""Inverted pyramids whose tips sit on the four terrain-map corners.

Each tip is one height sample of the loaded terrain: world XY is the sample
index times squareSize, world Z is sample/65535 * TerrainBlock maxHeight.
The TSStatic stays at the origin, in the same frame as the road mesh.
The tip is not raised.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\build_corner_pyramids.py
"""
from __future__ import annotations

import json
import struct
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np

from site_coords import load_site, processed_dir

ROOT = Path(__file__).resolve().parents[1]
USER_LEVELS = (
    Path.home()
    / "AppData"
    / "Local"
    / "BeamNG"
    / "BeamNG.drive"
    / "current"
    / "levels"
)
IDENTITY_ROT = [1, 0, 0, 0, 1, 0, 0, 0, 1]
HALF_M = 100.0
HEIGHT_M = 50.0
GROUP = "corner_pyramids"
STEM = "pyramids"

# South-west, south-east, north-west, north-east. Row 0 of the terrain file is south.
CORNERS = (
    ("SW", 0, 0, (0.92, 0.08, 0.06)),
    ("SE", 0, 1, (0.08, 0.72, 0.10)),
    ("NW", 1, 0, (0.12, 0.32, 0.95)),
    ("NE", 1, 1, (0.95, 0.78, 0.08)),
)


def _read_terrain(level_dir: Path) -> tuple[np.ndarray, float, float]:
    block_path = (
        level_dir / "main" / "MissionGroup" / "level_objects" / "terrain" / "items.level.json"
    )
    max_h = None
    for line in block_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("name") == "theTerrain" or row.get("class") == "TerrainBlock":
            max_h = float(row["maxHeight"])
            break
    if max_h is None:
        raise SystemExit(f"TerrainBlock maxHeight missing in {block_path}")
    preset_path = level_dir / "import" / "terrainPreset.json"
    square = 1.0
    if preset_path.is_file():
        preset = json.loads(preset_path.read_text(encoding="utf-8"))
        square = float(preset.get("squareSize", square))
    ter = level_dir / "theTerrain.ter"
    data = ter.read_bytes()
    version = data[0]
    size = struct.unpack_from("<I", data, 1)[0]
    need = 5 + size * size * 2
    if version != 9 or size < 2 or len(data) < need:
        raise SystemExit(f"unexpected terrain file {ter}: version {version} size {size}")
    hm = np.frombuffer(data[5:need], dtype="<u2").reshape(size, size).copy()
    return hm, max_h, square


def _outward(a: np.ndarray, b: np.ndarray, c: np.ndarray, interior: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ab = b - a
    ac = c - a
    n = np.cross(ab, ac)
    if float(np.dot(n, n)) <= 0.0:
        raise SystemExit("degenerate pyramid face")
    centroid = (a + b + c) / 3.0
    if float(np.dot(n, interior - centroid)) > 0.0:
        b, c = c, b
        n = -n
    if float(np.dot(n, interior - centroid)) >= 0.0:
        raise SystemExit("pyramid face does not point outward")
    return a, b, c


def _pyramid(tip: np.ndarray, rgb: tuple[float, float, float]) -> tuple[np.ndarray, np.ndarray]:
    s = HALF_M
    h = HEIGHT_M
    base = [
        tip + np.array([-s, -s, h]),
        tip + np.array([s, -s, h]),
        tip + np.array([s, s, h]),
        tip + np.array([-s, s, h]),
    ]
    interior = tip + np.array([0.0, 0.0, h * 0.25])
    tris = [
        (tip, base[0], base[1]),
        (tip, base[1], base[2]),
        (tip, base[2], base[3]),
        (tip, base[3], base[0]),
        (base[0], base[1], base[2]),
        (base[0], base[2], base[3]),
    ]
    pos: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    for a, b, c in tris:
        a, b, c = _outward(np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64), np.asarray(c, dtype=np.float64), interior)
        i0 = len(pos)
        pos.extend((tuple(a.tolist()), tuple(b.tolist()), tuple(c.tolist())))
        faces.append((i0, i0 + 1, i0 + 2))
    return np.asarray(pos, dtype=np.float64), np.asarray(faces, dtype=np.int32)


def _write_collada(path: Path, parts: list[tuple[str, np.ndarray, np.ndarray]]) -> None:
    """One visual mesh on the LOD node, same node layout as the road-grid parts."""
    lod = f"{STEM}_a999"
    blocks = []
    pos_all: list[np.ndarray] = []
    face_all: list[np.ndarray] = []
    offset = 0
    effects = []
    materials = []
    binds = []
    for name, pos, faces in parts:
        m = escape(name)
        n_tri = int(faces.shape[0])
        shifted = faces + offset
        p_vals = " ".join(str(int(v)) for v in shifted.reshape(-1))
        blocks.append(
            f"""        <triangles material="{m}" count="{n_tri}">
          <input semantic="VERTEX" source="#{lod}-mesh-vertices" offset="0"/>
          <input semantic="TEXCOORD" source="#{lod}-mesh-map-0" offset="0" set="0"/>
          <p>{p_vals}</p>
        </triangles>"""
        )
        effects.append(
            f"""    <effect id="{m}-effect">
      <profile_COMMON>
        <technique sid="common">
          <lambert><diffuse><color>1 1 1 1</color></diffuse></lambert>
        </technique>
      </profile_COMMON>
    </effect>"""
        )
        materials.append(
            f"""    <material id="{m}-material" name="{m}">
      <instance_effect url="#{m}-effect"/>
    </material>"""
        )
        binds.append(
            f"""                  <instance_material symbol="{m}" target="#{m}-material">
                    <bind_vertex_input semantic="UVSET0" input_semantic="TEXCOORD" input_set="0"/>
                  </instance_material>"""
        )
        pos_all.append(pos)
        face_all.append(shifted)
        offset += int(pos.shape[0])
    pos = np.vstack(pos_all)
    faces = np.vstack(face_all)
    n_vert = int(pos.shape[0])
    n_tri = int(faces.shape[0])
    pos_vals = " ".join(f"{v:.6f}" for v in pos.reshape(-1))
    uv = np.zeros((n_vert, 2), dtype=np.float64)
    uv_vals = " ".join(f"{v:.5f}" for v in uv.reshape(-1))
    col_p = " ".join(str(int(v)) for v in faces.reshape(-1))
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor><authoring_tool>beamng_autoroad build_corner_pyramids</authoring_tool></contributor>
    <unit name="meter" meter="1"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_effects>
{chr(10).join(effects)}
  </library_effects>
  <library_materials>
{chr(10).join(materials)}
  </library_materials>
  <library_geometries>
    <geometry id="{lod}-mesh" name="{lod}-mesh">
      <mesh>
        <source id="{lod}-mesh-positions">
          <float_array id="{lod}-mesh-positions-array" count="{n_vert * 3}">{pos_vals}</float_array>
          <technique_common>
            <accessor source="#{lod}-mesh-positions-array" count="{n_vert}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{lod}-mesh-map-0">
          <float_array id="{lod}-mesh-map-0-array" count="{n_vert * 2}">{uv_vals}</float_array>
          <technique_common>
            <accessor source="#{lod}-mesh-map-0-array" count="{n_vert}" stride="2">
              <param name="S" type="float"/>
              <param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{lod}-mesh-vertices">
          <input semantic="POSITION" source="#{lod}-mesh-positions"/>
        </vertices>
{chr(10).join(blocks)}
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
          <p>{col_p}</p>
        </triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="base00" name="base00" type="NODE">
        <node id="start01" name="start01" type="NODE">
          <node id="{lod}" name="{lod}" type="NODE">
            <instance_geometry url="#{lod}-mesh">
              <bind_material>
                <technique_common>
{chr(10).join(binds)}
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


def _materials(level_name: str, colors: dict[str, tuple[float, float, float]]) -> dict:
    out = {}
    for name, rgb in colors.items():
        out[name] = {
            "name": name,
            "mapTo": name,
            "class": "Material",
            "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"autoroad:{GROUP}:mat:{name}")),
            "Stages": [
                {
                    "baseColorFactor": [rgb[0], rgb[1], rgb[2], 1.0],
                    "emissiveFactor": [rgb[0] * 0.35, rgb[1] * 0.35, rgb[2] * 0.35],
                    "roughnessFactor": 0.6,
                    "metallicFactor": 0.0,
                },
                {},
                {},
                {},
            ],
            "doubleSided": True,
            "invertBackFaceNormals": True,
            "castShadows": True,
            "annotation": "BUILDING",
            "materialTag0": "beamng",
            "version": 1.5,
        }
    return out


def _entry(level_name: str) -> dict:
    return {
        "name": STEM,
        "class": "TSStatic",
        "__parent": GROUP,
        "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"autoroad:{GROUP}:{STEM}")),
        "position": [0, 0, 0],
        "rotationMatrix": IDENTITY_ROT,
        "scale": [1.0, 1.0, 1.0],
        "shapeName": f"/levels/{level_name}/art/shapes/{GROUP}/{STEM}.dae",
        "collisionType": "Collision Mesh",
        "decalType": "Collision Mesh",
        "castShadows": True,
        "useInstanceRenderData": True,
        "isRenderEnabled": True,
    }


def _inject(level_name: str, mesh_dir: Path) -> None:
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        raise SystemExit(f"Level folder missing: {user_level}")
    art = user_level / "art" / "shapes" / GROUP
    art.mkdir(parents=True, exist_ok=True)
    for src in mesh_dir.iterdir():
        if src.suffix.lower() in {".dae", ".json"}:
            (art / src.name).write_bytes(src.read_bytes())
    stale_dae = art / "corner_pyramids.dae"
    if stale_dae.is_file():
        stale_dae.unlink()
    cache = USER_LEVELS.parent / "temp" / "levels" / level_name / "art" / "shapes" / GROUP
    if cache.is_dir():
        for stale in cache.glob("*.cdae"):
            stale.unlink()
    group_dir = user_level / "main" / "MissionGroup" / "level_objects" / GROUP
    group_dir.mkdir(parents=True, exist_ok=True)
    items = group_dir / "items.level.json"
    items.write_text(json.dumps(_entry(level_name), separators=(",", ":")) + "\n", encoding="utf-8")
    lo = user_level / "main" / "MissionGroup" / "level_objects" / "items.level.json"
    rows = []
    for line in lo.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    found = False
    for row in rows:
        if row.get("class") == "SimGroup" and row.get("name") == GROUP:
            row["enabled"] = "1"
            found = True
    if not found:
        rows.append(
            {
                "name": GROUP,
                "class": "SimGroup",
                "__parent": "level_objects",
                "enabled": "1",
                "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"autoroad:{GROUP}:group")),
            }
        )
    lo.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows), encoding="utf-8")


def main() -> None:
    site = load_site()
    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "").strip()
    if not level_name:
        raise SystemExit("beamng.level_name missing")
    level_dir = USER_LEVELS / level_name
    hm, max_h, square = _read_terrain(level_dir)
    size = int(hm.shape[0])
    last = size - 1
    parts = []
    colors = {}
    tips = []
    for name, iy, ix, rgb in CORNERS:
        row = iy * last
        col = ix * last
        sample = int(hm[row, col])
        z = (sample / 65535.0) * max_h
        tip = np.array([col * square, row * square, z], dtype=np.float64)
        mat = f"Corner{name}"
        pos, faces = _pyramid(tip, rgb)
        parts.append((mat, pos, faces))
        colors[mat] = rgb
        tips.append(
            {
                "name": name,
                "material": mat,
                "color_rgb": list(rgb),
                "sample_xy": [col, row],
                "sample_u16": sample,
                "tip_xyz_m": [round(float(tip[0]), 6), round(float(tip[1]), 6), round(float(tip[2]), 6)],
            }
        )
        print(
            f"{name} sample ({col},{row}) u16={sample} tip=({tip[0]:.3f}, {tip[1]:.3f}, {tip[2]:.3f})",
            flush=True,
        )
    out_dir = processed_dir(site) / GROUP
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_collada(out_dir / f"{STEM}.dae", parts)
    mats = _materials(level_name, colors)
    (out_dir / "main.materials.json").write_text(json.dumps(mats, indent=2) + "\n", encoding="utf-8")
    report = {
        "level": level_name,
        "terrain_file": "theTerrain.ter",
        "heightmap_samples": size,
        "square_size_m": square,
        "max_height_m": max_h,
        "half_base_m": HALF_M,
        "height_m": HEIGHT_M,
        "z": "uint16 / 65535 * TerrainBlock.maxHeight",
        "xy": "sample index * squareSize; row 0 of theTerrain.ter is south",
        "clearance_m": 0.0,
        "tsstatic_position": [0, 0, 0],
        "tips": tips,
    }
    (out_dir / "corner_pyramids.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    _inject(level_name, out_dir)
    print(f"Injected {STEM}.dae -> {level_name}", flush=True)
    print("Quit BeamNG fully and restart so the new TSStatic loads.", flush=True)


if __name__ == "__main__":
    main()
