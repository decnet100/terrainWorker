"""Mesh the filtered 0.5 m road surface and place it above the terrain.

One quad per corridor cell whose filter moved the ground. Vertices sit 4 cm
above that surface so the 3.2 cm heightmap step cannot poke through. The
measured road-edge clip and the later node reduction are not in this step.
The coarse asphalt MeshRoad group is switched off so the two decks do not stack.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\build_corridor_mesh.py
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
import tifffile as tiff
from PIL import Image

from site_coords import SiteCoords, load_site, processed_dir, site_slug

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
COLLADA_MAX_VERTS = 65535
MOVE_M = 0.005
CLEARANCE_M = 0.04
UV_M = 6.0
IDENTITY_ROT = [1, 0, 0, 0, 1, 0, 0, 0, 1]
MAT = "Asphalt"


def _load_moved(raw: Path, filt: Path) -> tuple[np.ndarray, np.ndarray]:
    src = np.asarray(tiff.imread(raw), dtype=np.float32)
    dst = np.asarray(tiff.imread(filt), dtype=np.float32)
    if src.ndim == 3:
        src = src[..., 0]
    if dst.ndim == 3:
        dst = dst[..., 0]
    moved = np.isfinite(src) & np.isfinite(dst) & (np.abs(dst - src) >= MOVE_M)
    return dst, moved


def _mesh_window(
    z_abs: np.ndarray,
    moved: np.ndarray,
    *,
    r0: int,
    r1: int,
    c0: int,
    c1: int,
    xmin: float,
    ymax: float,
    res: float,
    sc: SiteCoords,
    z_min: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    keep = moved[r0:r1, c0:c1]
    if keep.shape[0] < 2 or keep.shape[1] < 2:
        return None
    quad = (
        keep[:-1, :-1]
        & keep[:-1, 1:]
        & keep[1:, 1:]
        & keep[1:, :-1]
    )
    if not np.any(quad):
        return None
    used = np.zeros(keep.shape, dtype=bool)
    used[:-1, :-1] |= quad
    used[:-1, 1:] |= quad
    used[1:, 1:] |= quad
    used[1:, :-1] |= quad
    n_vert = int(used.sum())
    idx = np.full(keep.shape, -1, dtype=np.int32)
    idx[used] = np.arange(n_vert, dtype=np.int32)
    rr, cc = np.nonzero(used)
    col = c0 + cc
    row = r0 + rr
    crs_x = xmin + (col.astype(np.float64) + 0.5) * res
    crs_y = ymax - (row.astype(np.float64) + 0.5) * res
    bx = (crs_x - sc.xmin) / sc.bw * sc.terrain_span
    by = (crs_y - sc.ymin) / sc.bh * sc.terrain_span
    zz = z_abs[r0:r1, c0:c1][rr, cc].astype(np.float64) - z_min + CLEARANCE_M
    pos = np.column_stack((bx, by, zz))
    uv = np.column_stack((bx / UV_M, by / UV_M))
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
    return pos, uv, faces


def _write_collada(
    path: Path,
    pos: np.ndarray,
    uv: np.ndarray,
    faces: np.ndarray,
    stem: str,
    col_pos: np.ndarray | None = None,
    col_faces: np.ndarray | None = None,
) -> None:
    n_vert = int(pos.shape[0])
    n_tri = int(faces.shape[0])
    if col_pos is None or col_faces is None:
        col_pos = pos
        col_faces = faces
    n_col = int(col_pos.shape[0])
    n_col_tri = int(col_faces.shape[0])
    if n_vert > COLLADA_MAX_VERTS or n_col > COLLADA_MAX_VERTS:
        raise SystemExit(f"{stem}: {n_vert} visual / {n_col} collision verts > {COLLADA_MAX_VERTS}")
    lod = f"{stem}_a999"
    mat = escape(MAT)
    pos_vals = " ".join(f"{v:.3f}" for v in pos.reshape(-1))
    uv_vals = " ".join(f"{v:.5f}" for v in uv.reshape(-1))
    p_vals = " ".join(str(int(v)) for v in faces.reshape(-1))
    col_pos_vals = " ".join(f"{v:.3f}" for v in col_pos.reshape(-1))
    col_p_vals = " ".join(str(int(v)) for v in col_faces.reshape(-1))
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor><authoring_tool>beamng_autoroad build_corridor_mesh</authoring_tool></contributor>
    <unit name="meter" meter="1"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_effects>
    <effect id="{mat}-effect">
      <profile_COMMON>
        <technique sid="common">
          <lambert>
            <diffuse><color>0.16 0.16 0.17 1</color></diffuse>
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
        <triangles material="{mat}" count="{n_tri}">
          <input semantic="VERTEX" source="#{lod}-mesh-vertices" offset="0"/>
          <input semantic="TEXCOORD" source="#{lod}-mesh-map-0" offset="0" set="0"/>
          <p>{p_vals}</p>
        </triangles>
      </mesh>
    </geometry>
    <geometry id="Colmesh-1-mesh" name="Colmesh-1-mesh">
      <mesh>
        <source id="Colmesh-1-mesh-positions">
          <float_array id="Colmesh-1-mesh-positions-array" count="{n_col * 3}">{col_pos_vals}</float_array>
          <technique_common>
            <accessor source="#Colmesh-1-mesh-positions-array" count="{n_col}" stride="3">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="Colmesh-1-mesh-vertices">
          <input semantic="POSITION" source="#Colmesh-1-mesh-positions"/>
        </vertices>
        <triangles count="{n_col_tri}">
          <input semantic="VERTEX" source="#Colmesh-1-mesh-vertices" offset="0"/>
          <p>{col_p_vals}</p>
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


def _emit(
    z_abs: np.ndarray,
    moved: np.ndarray,
    *,
    r0: int,
    r1: int,
    c0: int,
    c1: int,
    xmin: float,
    ymax: float,
    res: float,
    sc: SiteCoords,
    z_min: float,
    out_dir: Path,
    stem: str,
    written: list[dict],
) -> None:
    built = _mesh_window(
        z_abs, moved,
        r0=r0, r1=r1, c0=c0, c1=c1,
        xmin=xmin, ymax=ymax, res=res, sc=sc, z_min=z_min,
    )
    if built is None:
        return
    pos, uv, faces = built
    if pos.shape[0] > COLLADA_MAX_VERTS:
        if (r1 - r0) >= (c1 - c0) and (r1 - r0) > 2:
            mid = r0 + (r1 - r0) // 2
            _emit(z_abs, moved, r0=r0, r1=mid, c0=c0, c1=c1, xmin=xmin, ymax=ymax, res=res, sc=sc, z_min=z_min, out_dir=out_dir, stem=f"{stem}a", written=written)
            _emit(z_abs, moved, r0=mid - 1, r1=r1, c0=c0, c1=c1, xmin=xmin, ymax=ymax, res=res, sc=sc, z_min=z_min, out_dir=out_dir, stem=f"{stem}b", written=written)
            return
        if (c1 - c0) > 2:
            mid = c0 + (c1 - c0) // 2
            _emit(z_abs, moved, r0=r0, r1=r1, c0=c0, c1=mid, xmin=xmin, ymax=ymax, res=res, sc=sc, z_min=z_min, out_dir=out_dir, stem=f"{stem}a", written=written)
            _emit(z_abs, moved, r0=r0, r1=r1, c0=mid - 1, c1=c1, xmin=xmin, ymax=ymax, res=res, sc=sc, z_min=z_min, out_dir=out_dir, stem=f"{stem}b", written=written)
            return
        raise SystemExit(f"{stem}: {pos.shape[0]} verts in a 2-cell window")
    _write_collada(out_dir / f"{stem}.dae", pos, uv, faces, stem)
    written.append(
        {
            "name": stem,
            "verts": int(pos.shape[0]),
            "tris": int(faces.shape[0]),
        }
    )


def _ts_entry(level_name: str, stem: str) -> dict:
    return {
        "name": stem,
        "class": "TSStatic",
        "__parent": "corridor_mesh",
        "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"autoroad:corridor_mesh:{stem}")),
        "position": [0, 0, 0],
        "rotationMatrix": IDENTITY_ROT,
        "scale": [1.0, 1.0, 1.0],
        "shapeName": f"/levels/{level_name}/art/shapes/corridor_mesh/{stem}.dae",
        "collisionType": "Collision Mesh",
        "decalType": "Collision Mesh",
        "castShadows": True,
        "useInstanceRenderData": True,
        "isRenderEnabled": True,
    }


def _write_material(path: Path) -> None:
    data = {
        MAT: {
            "name": MAT,
            "mapTo": MAT,
            "class": "Material",
            "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "autoroad:corridor_mesh:Asphalt")),
            "Stages": [
                {
                    "baseColorFactor": [0.16, 0.16, 0.17, 1.0],
                    "roughnessFactor": 0.85,
                },
                {},
                {},
                {},
            ],
        }
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _inject(level_name: str, entries: list[dict], art_src: Path) -> None:
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        raise SystemExit(f"Level folder missing: {user_level}")
    art = user_level / "art" / "shapes" / "corridor_mesh"
    art.mkdir(parents=True, exist_ok=True)
    keep = {p.name for p in art_src.iterdir() if p.suffix.lower() in {".dae", ".json"}}
    for src in art_src.iterdir():
        if src.suffix.lower() in {".dae", ".json"}:
            (art / src.name).write_bytes(src.read_bytes())
    for dst in list(art.iterdir()):
        if dst.suffix.lower() == ".dae" and dst.name not in keep:
            dst.unlink()
    group_dir = user_level / "main" / "MissionGroup" / "level_objects" / "corridor_mesh"
    group_dir.mkdir(parents=True, exist_ok=True)
    items_path = group_dir / "items.level.json"
    with items_path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in entries:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    lo_items = user_level / "main" / "MissionGroup" / "level_objects" / "items.level.json"
    rows = []
    if lo_items.is_file():
        for ln in lo_items.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    found = False
    for row in rows:
        if row.get("name") == "corridor_mesh" and row.get("class") == "SimGroup":
            row["enabled"] = "1"
            found = True
        if row.get("name") == "asphalt_mesh" and row.get("class") == "SimGroup":
            row["enabled"] = "0"
    if not found:
        rows.append(
            {
                "name": "corridor_mesh",
                "class": "SimGroup",
                "__parent": "level_objects",
                "enabled": "1",
                "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "autoroad:corridor_mesh:group")),
            }
        )
    with lo_items.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    print(f"Injected {len(entries)} TSStatics, asphalt_mesh disabled", flush=True)


def main() -> None:
    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "").strip()
    if not level_name:
        raise SystemExit("beamng.level_name missing")
    meta = json.loads((proc / "heightmap_meta.json").read_text(encoding="utf-8"))
    z_min = float(meta["z_min_m"])
    raw_dir = ROOT / "data" / "raw" / f"dgm_{site_slug(site)}_corridor50"
    raw_index = json.loads((raw_dir / "corridor_index.json").read_text(encoding="utf-8"))
    filt_dir = proc / "corridor50_road"
    filt = json.loads((filt_dir / "corridor50_road_index.json").read_text(encoding="utf-8"))
    if "raster" not in filt:
        raise SystemExit("filtered corridor is not one raster; run filter_road_corridor.py")
    filtered = np.asarray(tiff.imread(filt_dir / filt["raster"]), dtype=np.float32)
    if filtered.ndim == 3:
        filtered = filtered[..., 0]
    res = float(filt["resolution_m"])
    moved = np.zeros(filtered.shape, dtype=bool)
    for spec in raw_index["tiles"]:
        src = np.asarray(tiff.imread(raw_dir / spec["name"]), dtype=np.float32)
        if src.ndim == 3:
            src = src[..., 0]
        r0 = int(round((float(filt["ymax"]) - float(spec["ymax"])) / res))
        c0 = int(round((float(spec["xmin"]) - float(filt["xmin"])) / res))
        h, w = src.shape
        dst = filtered[r0 : r0 + h, c0 : c0 + w]
        if dst.shape != src.shape:
            raise SystemExit(f"{spec['name']} does not sit on the filtered raster")
        moved[r0 : r0 + h, c0 : c0 + w] = (
            np.isfinite(src) & np.isfinite(dst) & (np.abs(dst - src) >= MOVE_M)
        )
    out_dir = proc / "corridor_mesh"
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.dae"):
        old.unlink()

    written: list[dict] = []
    _emit(
        filtered,
        moved,
        r0=0,
        r1=int(filtered.shape[0]),
        c0=0,
        c1=int(filtered.shape[1]),
        xmin=float(filt["xmin"]),
        ymax=float(filt["ymax"]),
        res=res,
        sc=sc,
        z_min=z_min,
        out_dir=out_dir,
        stem="part",
        written=written,
    )
    print(f"  parts={len(written)}", flush=True)

    _write_material(out_dir / "main.materials.json")
    n_vert = sum(item["verts"] for item in written)
    n_tri = sum(item["tris"] for item in written)
    meta_out = {
        "clearance_m": CLEARANCE_M,
        "move_m": MOVE_M,
        "resolution_m": res,
        "parts": len(written),
        "verts": n_vert,
        "tris": n_tri,
        "tiles": written,
    }
    (out_dir / "corridor_mesh_index.json").write_text(
        json.dumps(meta_out, indent=2) + "\n", encoding="utf-8"
    )
    entries = [_ts_entry(level_name, item["name"]) for item in written]
    _inject(level_name, entries, out_dir)
    print(
        f"Corridor mesh: {len(written)} parts, {n_vert} verts, {n_tri} tris, "
        f"+{CLEARANCE_M * 100:.0f} cm",
        flush=True,
    )


if __name__ == "__main__":
    main()
