"""Replace the clipped heightmap cap with a 1:1 terrain mesh.

Reads the unclipped backup heightmap, keeps every sample at or above the clip
plus a skirt that sits under the playable terrain, and writes Collada tiles.
BeamNG indexes a mesh with 16-bit vertices, so each tile stays under that limit.
The playable heightmap is not modified.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\build_summit_cap.py
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
from PIL import Image
from scipy.ndimage import distance_transform_edt

from site_coords import load_site, processed_dir

USER_LEVELS = (
    Path.home()
    / "AppData"
    / "Local"
    / "BeamNG"
    / "BeamNG.drive"
    / "current"
    / "levels"
)
COLLADA_MAX_VERTS = 65535
TILE_CELLS = 200
SKIRT_M = 40.0
LIP_M = 1.0
BACKUP_NAME = "backup_pre_clip1800"
IDENTITY_ROT = [1, 0, 0, 0, 1, 0, 0, 0, 1]

# Same class colours as compose_biomes._preview_materials, layer order 0..8.
LAYER_FILES = (
    "layerMap_0_dry_meadow.png",
    "layerMap_1_gravel.png",
    "layerMap_2_rock.png",
    "layerMap_3_Asphalt.png",
    "layerMap_4_ForestFloor.png",
    "layerMap_5_Mud.png",
    "layerMap_6_Concrete.png",
    "layerMap_7_Grass.png",
    "layerMap_8_SnowTirol.png",
)
LAYER_RGB = np.array(
    [
        [190, 160, 70],
        [140, 110, 70],
        [150, 150, 155],
        [40, 40, 45],
        [30, 90, 40],
        [40, 90, 180],
        [180, 180, 175],
        [60, 150, 55],
        [235, 240, 245],
    ],
    dtype=np.uint8,
)


def _decode_u16(path: Path, max_h: float) -> np.ndarray:
    u16 = np.asarray(Image.open(path))
    if u16.dtype != np.uint16:
        u16 = u16.astype(np.uint16)
    return u16.astype(np.float32) * np.float32(max_h / 65535.0)


def _class_ids(proc: Path, shape: tuple[int, int]) -> np.ndarray:
    best_w = np.zeros(shape, dtype=np.uint8)
    best_i = np.full(shape, 2, dtype=np.uint8)  # rock where nothing is painted
    for i, name in enumerate(LAYER_FILES):
        path = proc / name
        if not path.is_file():
            print(f"  no layer {name}")
            continue
        w = np.asarray(Image.open(path))
        if w.shape != shape:
            raise SystemExit(f"{name} shape {w.shape} != {shape}")
        if w.dtype != np.uint8:
            w = w.astype(np.uint8)
        take = w > best_w
        best_w[take] = w[take]
        best_i[take] = np.uint8(i)
        print(f"  layer {name} px={int(take.sum())}", flush=True)
    return best_i


def _write_collada(
    path: Path,
    pos: np.ndarray,
    uv: np.ndarray,
    faces: np.ndarray,
    *,
    mat: str,
    stem: str,
    png_name: str,
) -> None:
    n_vert = int(pos.shape[0])
    n_tri = int(faces.shape[0])
    if n_vert > COLLADA_MAX_VERTS:
        raise SystemExit(f"{stem}: {n_vert} verts > {COLLADA_MAX_VERTS}")
    lod = f"{stem}_a999"
    m = escape(mat)
    pos_vals = " ".join(f"{v:.3f}" for v in pos.reshape(-1))
    uv_vals = " ".join(f"{v:.5f}" for v in uv.reshape(-1))
    p_vals = " ".join(str(int(v)) for v in faces.reshape(-1))
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor><authoring_tool>beamng_autoroad build_summit_cap</authoring_tool></contributor>
    <unit name="meter" meter="1"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_images>
    <image id="{m}-image" name="{m}-image">
      <init_from>{escape(png_name)}</init_from>
    </image>
  </library_images>
  <library_effects>
    <effect id="{m}-effect">
      <profile_COMMON>
        <newparam sid="{m}-surface"><surface type="2D"><init_from>{m}-image</init_from></surface></newparam>
        <newparam sid="{m}-sampler"><sampler2D><source>{m}-surface</source></sampler2D></newparam>
        <technique sid="common">
          <lambert>
            <diffuse><texture texture="{m}-sampler" texcoord="UVSET0"/></diffuse>
          </lambert>
        </technique>
      </profile_COMMON>
    </effect>
  </library_effects>
  <library_materials>
    <material id="{m}-material" name="{m}">
      <instance_effect url="#{m}-effect"/>
    </material>
  </library_materials>
  <library_geometries>
    <geometry id="{lod}-mesh" name="{lod}-mesh">
      <mesh>
        <source id="{lod}-mesh-positions">
          <float_array id="{lod}-mesh-positions-array" count="{n_vert * 3}">{pos_vals}</float_array>
          <technique_common>
            <accessor source="#{lod}-mesh-positions-array" count="{n_vert}" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{lod}-mesh-map-0">
          <float_array id="{lod}-mesh-map-0-array" count="{n_vert * 2}">{uv_vals}</float_array>
          <technique_common>
            <accessor source="#{lod}-mesh-map-0-array" count="{n_vert}" stride="2">
              <param name="S" type="float"/><param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{lod}-mesh-vertices">
          <input semantic="POSITION" source="#{lod}-mesh-positions"/>
        </vertices>
        <triangles material="{m}" count="{n_tri}">
          <input semantic="VERTEX" source="#{lod}-mesh-vertices" offset="0"/>
          <input semantic="TEXCOORD" source="#{lod}-mesh-map-0" offset="0" set="0"/>
          <p>{p_vals}</p>
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
                  <instance_material symbol="{m}" target="#{m}-material">
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
    path.write_text(xml, encoding="utf-8", newline="\n")


def _tile_mesh(
    z: np.ndarray,
    keep: np.ndarray,
    *,
    r0: int,
    c0: int,
    r1: int,
    c1: int,
    scale: float,
    size: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    keep_win = keep[r0:r1, c0:c1]
    if keep_win.shape[0] < 2 or keep_win.shape[1] < 2:
        return None
    quad = (
        keep_win[:-1, :-1]
        & keep_win[:-1, 1:]
        & keep_win[1:, 1:]
        & keep_win[1:, :-1]
    )
    if not np.any(quad):
        return None
    used = np.zeros(keep_win.shape, dtype=bool)
    used[:-1, :-1] |= quad
    used[:-1, 1:] |= quad
    used[1:, 1:] |= quad
    used[1:, :-1] |= quad
    idx = np.full(keep_win.shape, -1, dtype=np.int32)
    n_vert = int(used.sum())
    if n_vert > COLLADA_MAX_VERTS:
        raise SystemExit(f"tile {r0},{c0}: {n_vert} verts > {COLLADA_MAX_VERTS}")
    idx[used] = np.arange(n_vert, dtype=np.int32)
    rr, cc = np.nonzero(used)
    z_win = z[r0:r1, c0:c1]
    pos = np.column_stack(
        (
            (c0 + cc) * scale,
            (size - 1 - (r0 + rr)) * scale,
            z_win[rr, cc],
        )
    ).astype(np.float64)
    nrows = r1 - r0
    ncols = c1 - c0
    uv = np.column_stack(
        (
            cc / max(ncols - 1, 1),
            1.0 - rr / max(nrows - 1, 1),
        )
    ).astype(np.float64)
    qy, qx = np.nonzero(quad)
    nw = idx[:-1, :-1][qy, qx]
    ne = idx[:-1, 1:][qy, qx]
    se = idx[1:, 1:][qy, qx]
    sw = idx[1:, :-1][qy, qx]
    faces = np.empty((qy.size * 2, 3), dtype=np.int32)
    faces[0::2, 0] = nw
    faces[0::2, 1] = sw
    faces[0::2, 2] = se
    faces[1::2, 0] = nw
    faces[1::2, 1] = se
    faces[1::2, 2] = ne
    a = pos[int(faces[0, 0])]
    b = pos[int(faces[0, 1])]
    c = pos[int(faces[0, 2])]
    ab = b - a
    ac = c - a
    nz = float(ab[0] * ac[1] - ab[1] * ac[0])
    if nz < 0.0:
        faces[:, [1, 2]] = faces[:, [2, 1]]
        nz = -nz
    if nz <= 0.0:
        raise SystemExit(f"tile {r0},{c0}: face normal is not upward")
    return pos, uv, faces


def _ts_entry(level_name: str, stem: str) -> dict:
    return {
        "name": stem,
        "class": "TSStatic",
        "__parent": "summit",
        "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"autoroad:summit:{stem}")),
        "position": [0, 0, 0],
        "rotationMatrix": IDENTITY_ROT,
        "scale": [1.0, 1.0, 1.0],
        "shapeName": f"/levels/{level_name}/art/shapes/summit/{stem}.dae",
        "collisionType": "None",
        "decalType": "None",
        "castShadows": True,
        "useInstanceRenderData": True,
        "isRenderEnabled": True,
    }


def _inject(level_name: str, entries: list[dict], art: Path, mesh_dir: Path) -> None:
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        raise SystemExit(f"Level folder missing: {user_level}")
    art.mkdir(parents=True, exist_ok=True)
    keep = {p.name for p in mesh_dir.iterdir() if p.suffix.lower() in {".dae", ".png", ".json"}}
    for src in mesh_dir.iterdir():
        if src.suffix.lower() in {".dae", ".png", ".json"}:
            (art / src.name).write_bytes(src.read_bytes())
    for dst in art.iterdir():
        if dst.name.startswith("summit_") and dst.suffix.lower() in {".dae", ".png"} and dst.name not in keep:
            dst.unlink()
    group_dir = user_level / "main" / "MissionGroup" / "level_objects" / "summit"
    group_dir.mkdir(parents=True, exist_ok=True)
    items_path = group_dir / "items.level.json"
    with items_path.open("w", encoding="utf-8", newline="\n") as f:
        for row in entries:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    lo_items = user_level / "main" / "MissionGroup" / "level_objects" / "items.level.json"
    rows = []
    if lo_items.is_file() and lo_items.stat().st_size:
        for ln in lo_items.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    names = {r.get("name") for r in rows}
    if "summit" not in names:
        rows.append(
            {
                "name": "summit",
                "class": "SimGroup",
                "__parent": "level_objects",
                "enabled": "1",
                "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "autoroad:summit:group")),
            }
        )
        with lo_items.open("w", encoding="utf-8", newline="\n") as f:
            for row in rows:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
        print("Registered SimGroup summit under level_objects")
    print(f"Injected {len(entries)} TSStatics -> {items_path}")


def main() -> None:
    site = load_site()
    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "").strip()
    if not level_name:
        raise SystemExit("beamng.level_name missing")
    proc = processed_dir(site)
    live_meta = json.loads((proc / "heightmap_meta.json").read_text(encoding="utf-8"))
    if "clip_absolute_m" not in live_meta:
        raise SystemExit("heightmap_meta has no clip_absolute_m — clip the terrain first")
    clip_abs = float(live_meta["clip_absolute_m"])
    backup = proc / BACKUP_NAME
    src_meta = json.loads((backup / "heightmap_meta.json").read_text(encoding="utf-8"))
    old_max = float(src_meta["max_height_m"])
    z0 = float(src_meta["z_min_m"])
    size = int(src_meta.get("heightmap_size_px") or bng.get("mask_size") or 8192)
    extent = float(src_meta.get("terrain_extent_m") or size)
    scale = extent / (size - 1)
    print(f"decoding backup heightmap, clip {clip_abs:.1f} m absolute", flush=True)
    elev = _decode_u16(backup / "heightmap_8192.png", old_max)
    if elev.shape != (size, size):
        raise SystemExit(f"backup heightmap {elev.shape} != {size}")
    clip_rel = np.float32(clip_abs - z0)
    interior = elev >= clip_rel
    print(f"interior px={int(interior.sum())}  distance field", flush=True)
    dist = distance_transform_edt(~interior)
    keep = dist <= SKIRT_M
    skirt = keep & ~interior
    live_max = float(live_meta["max_height_m"])
    print("decoding clipped terrain for the skirt", flush=True)
    composed = _decode_u16(proc / "heightmap_8192_composed.png", live_max)
    z = elev.copy()
    z[skirt] = composed[skirt] - np.float32(LIP_M)
    print(
        f"keep px={int(keep.sum())} skirt={int(skirt.sum())} lip={LIP_M:.1f} m",
        flush=True,
    )
    del dist, elev, interior, skirt, composed
    print("painting class ids from terrain layers", flush=True)
    class_id = _class_ids(proc, (size, size))

    mesh_dir = proc / "summit_meshes"
    mesh_dir.mkdir(parents=True, exist_ok=True)
    for stale in mesh_dir.glob("summit_*"):
        stale.unlink()

    entries: list[dict] = []
    materials: dict = {}
    n_tri = 0
    n_vert = 0
    tiles = 0
    for r0 in range(0, size - 1, TILE_CELLS):
        r1 = min(size, r0 + TILE_CELLS + 1)
        for c0 in range(0, size - 1, TILE_CELLS):
            c1 = min(size, c0 + TILE_CELLS + 1)
            built = _tile_mesh(z, keep, r0=r0, c0=c0, r1=r1, c1=c1, scale=scale, size=size)
            if built is None:
                continue
            pos, uv, faces = built
            stem = f"summit_y{r0:04d}_x{c0:04d}"
            mat = f"mat_{stem}"
            png_name = f"{stem}.png"
            rgb = LAYER_RGB[class_id[r0:r1, c0:c1]]
            Image.fromarray(rgb, mode="RGB").save(mesh_dir / png_name)
            _write_collada(
                mesh_dir / f"{stem}.dae",
                pos,
                uv,
                faces,
                mat=mat,
                stem=stem,
                png_name=png_name,
            )
            materials[mat] = {
                "name": mat,
                "mapTo": mat,
                "class": "Material",
                "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"autoroad:summit:mat:{mat}")),
                "Stages": [
                    {
                        "baseColorMap": f"/levels/{level_name}/art/shapes/summit/{png_name}",
                        "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
                        "roughnessFactor": 0.92,
                        "metallicFactor": 0.0,
                        "emissiveFactor": [0.0, 0.0, 0.0],
                    },
                    {},
                    {},
                    {},
                ],
                "annotation": "ROCK",
                "castShadows": True,
                "version": 1.5,
            }
            entries.append(_ts_entry(level_name, stem))
            n_vert += int(pos.shape[0])
            n_tri += int(faces.shape[0])
            tiles += 1
            if tiles % 25 == 0:
                print(f"  tiles={tiles} verts={n_vert} tris={n_tri}", flush=True)

    (mesh_dir / "main.materials.json").write_text(
        json.dumps(materials, indent=2) + "\n", encoding="utf-8"
    )
    meta_out = {
        "clip_absolute_m": clip_abs,
        "z_min_m": z0,
        "skirt_m": SKIRT_M,
        "lip_m": LIP_M,
        "tile_cells": TILE_CELLS,
        "tiles": tiles,
        "verts": n_vert,
        "tris": n_tri,
        "collision": "None",
        "source": f"{BACKUP_NAME}/heightmap_8192.png",
    }
    (proc / "summit_meta.json").write_text(
        json.dumps(meta_out, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"summit tiles={tiles} verts={n_vert} tris={n_tri} -> {mesh_dir}",
        flush=True,
    )
    art = USER_LEVELS / level_name / "art" / "shapes" / "summit"
    _inject(level_name, entries, art, mesh_dir)
    print("Quit BeamNG fully and restart so the new TSStatics load.", flush=True)


if __name__ == "__main__":
    main()
