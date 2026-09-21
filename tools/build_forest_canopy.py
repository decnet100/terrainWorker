"""Local Kronendach POC: nDSM canopy slab + alpha holes + sparse fir billboards.

Builds one TSStatic over the densest forest window (or beamng.forest_canopy.center_xy).
Z = DGM + nDSM (crown top) + lift. Albedo from dark-green / nDSM shading; opacity
map gets random holes. Sparse non-collidable fir billboards poke through gaps.

Usage:
  cd C:\\temp\\beamng_autoroad
  $env:AUTOROAD_SITE = \"config/sites/fernpass_mega.yaml\"
  python tools\\build_forest_canopy.py
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
import tifffile as tiff
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import load_site, processed_dir, site_slug  # noqa: E402
import build_bridges as bb  # noqa: E402
import build_galleries as bg  # noqa: E402
import build_forest as bf  # noqa: E402

USER_LEVELS = bb.USER_LEVELS
MAT = "AutoroadForestCanopy"
STEM = "forest_canopy_poc"
COLLADA_MAX_VERTS = 65000
IDENTITY_ROT = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


def _cfg(site: dict) -> dict:
    raw = (site.get("beamng") or {}).get("forest_canopy") or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "size_m": float(raw.get("size_m") or 800.0),
        "step_m": float(raw.get("step_m") or 6.0),
        "center_xy": raw.get("center_xy"),
        "lift_m": float(raw.get("lift_m") or 0.15),
        "alpha_hole_frac": float(raw.get("alpha_hole_frac") or 0.18),
        "billboard_spacing_m": float(raw.get("billboard_spacing_m") or 18.0),
        "billboard_max": int(raw.get("billboard_max") or 400),
        "texture_size": int(raw.get("texture_size") or 1024),
        "min_ndsm_m": float(raw.get("min_ndsm_m") or 2.0),
    }


def _read_ndjson(path: Path) -> list[dict]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    rows = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return rows


def _write_ndjson(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def _densest_center(
    forest: np.ndarray, win_px: int, mpp: float
) -> tuple[float, float]:
    """Return BeamNG XY of densest forest window center."""
    from scipy.ndimage import uniform_filter  # noqa: WPS433

    size = forest.shape[0]
    win_px = max(8, min(win_px, size))
    dens = uniform_filter(forest.astype(np.float32), size=win_px, mode="constant")
    r, c = np.unravel_index(int(np.argmax(dens)), dens.shape)
    x = (float(c) + 0.5) * mpp
    y = (float(size - 1 - r) + 0.5) * mpp
    return x, y


def _window_slices(
    size: int, mpp: float, cx: float, cy: float, size_m: float
) -> tuple[int, int, int, int]:
    half = size_m * 0.5
    c0 = int(math.floor((cx - half) / mpp))
    c1 = int(math.ceil((cx + half) / mpp))
    # y north = high: row decreases as y increases
    r_north = int(math.floor((size - 1) - (cy + half) / mpp))
    r_south = int(math.ceil((size - 1) - (cy - half) / mpp))
    r0 = max(0, min(r_north, r_south))
    r1 = min(size, max(r_north, r_south))
    c0 = max(0, c0)
    c1 = min(size, c1)
    if c1 <= c0:
        c1 = min(size, c0 + 8)
    if r1 <= r0:
        r1 = min(size, r0 + 8)
    if r1 - r0 < 8 or c1 - c0 < 8:
        raise SystemExit(f"Canopy window too small: rows {r0}:{r1} cols {c0}:{c1}")
    return r0, r1, c0, c1


def _hash01(i: np.ndarray, j: np.ndarray, seed: int = 17) -> np.ndarray:
    x = (i.astype(np.int64) * 374761393 + j.astype(np.int64) * 668265263 + seed) & 0x7FFFFFFF
    x = (x ^ (x >> 13)) * 1274126177
    return ((x ^ (x >> 16)) & 0x7FFFFFFF).astype(np.float64) / float(0x7FFFFFFF)


def _build_albedo_opacity(
    ndsm: np.ndarray,
    forest: np.ndarray,
    *,
    tex_size: int,
    hole_frac: float,
) -> tuple[np.ndarray, np.ndarray]:
    """RGB albedo + gray opacity (255=opaque)."""
    h, w = ndsm.shape
    # Upsample to texture
    nd = np.asarray(
        Image.fromarray(ndsm.astype(np.float32), mode="F").resize(
            (tex_size, tex_size), Image.Resampling.BILINEAR
        ),
        dtype=np.float32,
    )
    fo = np.asarray(
        Image.fromarray((forest.astype(np.uint8) * 255), mode="L").resize(
            (tex_size, tex_size), Image.Resampling.BILINEAR
        ),
        dtype=np.float32,
    ) / 255.0
    yy, xx = np.mgrid[0:tex_size, 0:tex_size]
    n01 = np.clip(nd / 25.0, 0.0, 1.0)
    # Dark green canopy, brighter on taller nDSM
    g = 28.0 + 70.0 * n01 + 18.0 * _hash01(yy, xx, 3)
    r = 12.0 + 25.0 * n01 + 10.0 * _hash01(yy, xx, 7)
    b = 10.0 + 20.0 * n01 + 8.0 * _hash01(yy, xx, 11)
    rgb = np.stack([r, g, b], axis=-1)
    rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    # Soft edge + random holes inside forest
    edge = np.clip((fo - 0.35) / 0.45, 0.0, 1.0)
    holes = (_hash01(yy // 3, xx // 3, 19) < float(hole_frac)).astype(np.float32)
    fine = (_hash01(yy, xx, 23) < float(hole_frac) * 0.35).astype(np.float32)
    opac = edge * (1.0 - 0.85 * holes) * (1.0 - 0.55 * fine)
    opac = np.clip(opac * 255.0, 0, 255).astype(np.uint8)
    # Kill non-forest hard
    opac[fo < 0.2] = 0
    return rgb, opac


def _write_collada(
    path: Path,
    verts: list[tuple[float, float, float]],
    faces: list[tuple[int, int, int]],
    uvs: list[tuple[float, float]],
    *,
    mat: str,
    stem: str,
    png_name: str,
) -> None:
    n_vert = len(verts)
    n_tri = len(faces)
    if n_vert > COLLADA_MAX_VERTS:
        raise SystemExit(f"{stem}: {n_vert} verts exceeds Collada limit")
    lod = f"{stem}_a999"
    pos_vals = " ".join(f"{x:.3f} {y:.3f} {z:.3f}" for x, y, z in verts)
    uv_vals = " ".join(f"{u:.5f} {v:.5f}" for u, v in uvs)
    p_vals = " ".join(f"{a} {b} {c}" for a, b, c in faces)
    m = escape(mat)
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor><authoring_tool>beamng_autoroad build_forest_canopy</authoring_tool></contributor>
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
    </visual_scene>
  </library_visual_scenes>
  <scene>
    <instance_visual_scene url="#Scene"/>
  </scene>
</COLLADA>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-8", newline="\n")


def _write_material(level_name: str, mesh_dir: Path, png: str, opac: str) -> None:
    base = f"/levels/{level_name}/art/shapes/forest_canopy"
    data = {
        MAT: {
            "name": MAT,
            "mapTo": MAT,
            "class": "Material",
            "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"autoroad:canopy:{MAT}")),
            "Stages": [
                {
                    "baseColorMap": f"{base}/{png}",
                    "opacityMap": f"{base}/{opac}",
                    "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
                    "roughnessFactor": 0.85,
                    "metallicFactor": 0.0,
                },
                {},
                {},
                {},
            ],
            "annotation": "TREE",
            "castShadows": True,
            "alphaTest": True,
            "alphaRef": 90,
            "translucentBlendOp": "None",
            "translucentZWrite": True,
            "doubleSided": True,
            "version": 1.5,
        }
    }
    (mesh_dir / "main.materials.json").write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )


def _build_mesh(
    z_at,
    ndsm: np.ndarray,
    forest: np.ndarray,
    *,
    r0: int,
    r1: int,
    c0: int,
    c1: int,
    size: int,
    mpp: float,
    step_m: float,
    lift_m: float,
    min_ndsm: float,
) -> tuple[list, list, list]:
    step = max(1, int(round(step_m / mpp)))
    rows = list(range(r0, r1, step))
    if rows[-1] != r1 - 1:
        rows.append(r1 - 1)
    cols = list(range(c0, c1, step))
    if cols[-1] != c1 - 1:
        cols.append(c1 - 1)
    nr, nc = len(rows), len(cols)
    verts: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    keep = np.zeros((nr, nc), dtype=bool)
    for i, r in enumerate(rows):
        for j, c in enumerate(cols):
            bx = float(c) * mpp
            by = float(size - 1 - r) * mpp
            nd = float(ndsm[r, c])
            on = bool(forest[r, c]) and nd >= min_ndsm
            keep[i, j] = on
            z = float(z_at(bx, by)) + max(0.0, nd) + lift_m
            verts.append((bx, by, z))
            u = (c - c0) / max(c1 - c0 - 1, 1)
            v = 1.0 - (r - r0) / max(r1 - r0 - 1, 1)
            uvs.append((u, v))

    faces: list[tuple[int, int, int]] = []
    for i in range(nr - 1):
        for j in range(nc - 1):
            if not (keep[i, j] or keep[i, j + 1] or keep[i + 1, j] or keep[i + 1, j + 1]):
                continue
            # Indices: row-major
            a = i * nc + j
            b = i * nc + (j + 1)
            c = (i + 1) * nc + (j + 1)
            d = (i + 1) * nc + j
            # Upward faces (+Z): a-b-c and a-c-d (right-hand, Y north-ish)
            # Verify: (b-a) x (c-a) · +Z > 0
            ax, ay, az = verts[a]
            bx_, by_, bz = verts[b]
            cx_, cy_, cz = verts[c]
            ux, uy, uz = bx_ - ax, by_ - ay, bz - az
            vx, vy, vz = cx_ - ax, cy_ - ay, cz - az
            nz = ux * vy - uy * vx
            if nz >= 0:
                faces.append((a, b, c))
                faces.append((a, c, d))
            else:
                faces.append((a, c, b))
                faces.append((a, d, c))
    return verts, faces, uvs


def _inject_tsstatic(level_name: str, user_level: Path) -> None:
    group = user_level / "main" / "MissionGroup" / "level_objects" / "forest_canopy"
    entry = {
        "name": STEM,
        "class": "TSStatic",
        "__parent": "forest_canopy",
        "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"autoroad:canopy:{STEM}")),
        "position": [0, 0, 0],
        "rotationMatrix": IDENTITY_ROT,
        "scale": [1.0, 1.0, 1.0],
        "shapeName": f"/levels/{level_name}/art/shapes/forest_canopy/{STEM}.dae",
        "collisionType": "None",
        "decalType": "None",
        "castShadows": True,
        "useInstanceRenderData": True,
        "isRenderEnabled": True,
    }
    _write_ndjson(group / "items.level.json", [entry])
    lo = user_level / "main" / "MissionGroup" / "level_objects" / "items.level.json"
    rows = _read_ndjson(lo)
    names = {r.get("name") for r in rows}
    if "forest_canopy" not in names:
        rows.append(
            {
                "name": "forest_canopy",
                "class": "SimGroup",
                "__parent": "level_objects",
                "enabled": "1",
                "persistentId": str(
                    uuid.uuid5(uuid.NAMESPACE_URL, "autoroad:canopy:group")
                ),
            }
        )
        _write_ndjson(lo, rows)
        print("Registered SimGroup forest_canopy")


def _scatter_billboards(
    site: dict,
    cfg: dict,
    forest: np.ndarray,
    *,
    r0: int,
    r1: int,
    c0: int,
    c1: int,
    size: int,
    mpp: float,
    user_level: Path,
    level_name: str,
) -> int:
    z_at, slope_at = bg.load_terrain_z_slope(site)
    rng = np.random.default_rng(42)
    patch = np.zeros_like(forest)
    patch[r0:r1, c0:c1] = forest[r0:r1, c0:c1]
    cells = bf._candidate_cells(
        patch,
        float(cfg["billboard_spacing_m"]),
        mpp,
        rng,
        0.4,
        max_candidates=int(cfg["billboard_max"]) * 2,
    )
    rng.shuffle(cells)
    kind = "autoroad_canopy_spike"
    rows_out: list[dict] = []
    for r, c in cells:
        if len(rows_out) >= int(cfg["billboard_max"]):
            break
        bx = float(c) * mpp
        by = float(size - 1 - r) * mpp
        if float(slope_at(bx, by)) > 42.0:
            continue
        z = float(z_at(bx, by))
        yaw = float(rng.uniform(0, 2 * math.pi))
        scale = float(rng.uniform(0.9, 1.35))
        rows_out.append(
            {
                "type": kind,
                "pos": [bx, by, z],
                "rotationMatrix": bf._yaw_matrix(yaw),
                "scale": scale,
            }
        )

    # Managed item: same bush mesh, no collision
    base = f"/levels/{level_name}/{bf.FIR_LOCAL_REL}"
    item = bf._item_template(
        kind,
        f"{base}/{bf.SHAPE_FILES['autoroad_fir_bush']}",
        radius=0.8,
        collidable=False,
        wind_scale=0.0,
        trunk_bend=0.0,
        branch_amp=0.0,
        detail_amp=0.0,
        detail_freq=1.0,
        mass=1.0,
        rigidity=5.0,
        annotation="TREE",
    )
    bf.vendor_fir_pack(user_level, level_name, cheap=False)  # ensure mesh files on disk
    # Keep POC spike cheap; do not wipe Phase-A bush settings if present.
    path_managed = user_level / "art" / "forest" / "managedItemData.json"
    existing = {}
    if path_managed.is_file():
        try:
            existing = json.loads(path_managed.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    existing[kind] = item
    path_managed.parent.mkdir(parents=True, exist_ok=True)
    path_managed.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    print(f"Updated {path_managed} (+{kind})")

    forest_dir = user_level / "forest"
    forest_dir.mkdir(parents=True, exist_ok=True)
    path = forest_dir / f"{kind}.forest4.json"
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows_out:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    print(f"Wrote {path} ({len(rows_out)} billboards)")
    return len(rows_out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args()
    site = load_site()
    cfg = _cfg(site)
    if not cfg["enabled"]:
        raise SystemExit("beamng.forest_canopy.enabled is false")
    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "").strip()
    if not level_name:
        raise SystemExit("beamng.level_name missing")
    size = int(bng.get("mask_size") or 8192)
    mpp = float(bng.get("meters_per_pixel") or 1.0)
    proc = processed_dir(site)
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        raise SystemExit(f"Level missing: {user_level}")

    forest = bf._load_mask(proc / "mask_forest_high.png", size)
    scrub = bf._load_mask(proc / "mask_forest_scrub.png", size)
    forest = forest | scrub
    ndsm_path = proc / "ndsm.tif"
    if not ndsm_path.is_file():
        raise SystemExit(f"Missing {ndsm_path} - run build_smoke / DOM-DGM first")
    ndsm = np.asarray(tiff.imread(ndsm_path), dtype=np.float32)
    if ndsm.ndim == 3:
        ndsm = ndsm[..., 0]
    if ndsm.shape[0] != size:
        ndsm = np.asarray(
            Image.fromarray(ndsm, mode="F").resize(
                (size, size), Image.Resampling.BILINEAR
            ),
            dtype=np.float32,
        )

    win_px = max(8, int(round(cfg["size_m"] / mpp)))
    if cfg["center_xy"]:
        cx, cy = float(cfg["center_xy"][0]), float(cfg["center_xy"][1])
    else:
        cx, cy = _densest_center(forest, win_px, mpp)
    # Keep the full size_m window inside the playable map.
    half = cfg["size_m"] * 0.5
    extent = size * mpp
    cx = float(np.clip(cx, half + mpp, extent - half - mpp))
    cy = float(np.clip(cy, half + mpp, extent - half - mpp))
    r0, r1, c0, c1 = _window_slices(size, mpp, cx, cy, cfg["size_m"])
    print(
        f"Canopy POC window center=({cx:.0f},{cy:.0f}) m  "
        f"rows={r0}:{r1} cols={c0}:{c1}  "
        f"forest_in_win={100.0 * float(forest[r0:r1, c0:c1].mean()):.1f}%"
    )

    z_at, _slope = bg.load_terrain_z_slope(site)
    verts, faces, uvs = _build_mesh(
        z_at,
        ndsm,
        forest,
        r0=r0,
        r1=r1,
        c0=c0,
        c1=c1,
        size=size,
        mpp=mpp,
        step_m=cfg["step_m"],
        lift_m=cfg["lift_m"],
        min_ndsm=cfg["min_ndsm_m"],
    )
    print(f"Mesh verts={len(verts)} tris={len(faces)}")
    if not faces:
        raise SystemExit("No canopy faces - empty forest window / nDSM too low")

    rgb, opac = _build_albedo_opacity(
        ndsm[r0:r1, c0:c1],
        forest[r0:r1, c0:c1],
        tex_size=cfg["texture_size"],
        hole_frac=cfg["alpha_hole_frac"],
    )
    mesh_dir = user_level / "art" / "shapes" / "forest_canopy"
    mesh_dir.mkdir(parents=True, exist_ok=True)
    png = f"{STEM}_d.png"
    opac_png = f"{STEM}_opacity.png"
    Image.fromarray(rgb, mode="RGB").save(mesh_dir / png)
    Image.fromarray(opac, mode="L").save(mesh_dir / opac_png)
    # Preview in processed/
    Image.fromarray(rgb, mode="RGB").save(proc / "preview_forest_canopy_albedo.png")
    Image.fromarray(opac, mode="L").save(proc / "preview_forest_canopy_opacity.png")

    _write_material(level_name, mesh_dir, png, opac_png)
    _write_collada(
        mesh_dir / f"{STEM}.dae",
        verts,
        faces,
        uvs,
        mat=MAT,
        stem=STEM,
        png_name=png,
    )
    _inject_tsstatic(level_name, user_level)
    n_bb = _scatter_billboards(
        site,
        cfg,
        forest,
        r0=r0,
        r1=r1,
        c0=c0,
        c1=c1,
        size=size,
        mpp=mpp,
        user_level=user_level,
        level_name=level_name,
    )

    summary = {
        "site": site_slug(site),
        "level": level_name,
        "center_xy": [cx, cy],
        "window_px": [r0, r1, c0, c1],
        "verts": len(verts),
        "tris": len(faces),
        "billboards": n_bb,
        "cfg": cfg,
        "note": "Quit BeamNG fully and restart to load TSStatic + forest spike JSON.",
    }
    (proc / "forest_canopy_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(f"Wrote {proc / 'forest_canopy_summary.json'}")
    print("Quit BeamNG fully and restart to see the canopy POC.")


if __name__ == "__main__":
    main()
