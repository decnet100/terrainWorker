"""Opaque forest-group hulls (nDSM crown + side skirt). Not tree imposters.

Replaces inner Forest billboards on large dense patches so the engine does
not alpha-overdraw thousands of cards. Imposters stay in a rim around each
patch (written as mask_forest_hull_interior.png for build_forest).

Skirt grows downward from the nDSM crown (plate / three_quarter / full).
The crown Z always stays on nDSM.

  cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = \"config/sites/fernpass_mega.yaml\"; python tools\\build_forest_hull.py

Off by default: beamng.forest_hull.enabled.
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
MAT_CROWN = "AutoroadForestHullCrown"
MAT_SIDE = "AutoroadForestHullSide"
STEM = "forest_hull"
COLLADA_MAX_VERTS = 65000
IDENTITY_ROT = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


def _cfg(site: dict) -> dict:
    raw = (site.get("beamng") or {}).get("forest_hull") or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "rim_m": float(raw.get("rim_m") or 60.0),
        "min_area_m2": float(raw.get("min_area_m2") or 5000.0),
        "min_ndsm_m": float(raw.get("min_ndsm_m") or 8.0),
        "skirt": str(raw.get("skirt") or "plate").strip().lower(),
        "plate_thickness_m": float(raw.get("plate_thickness_m") or 6.0),
        "step_m": float(raw.get("step_m") or 8.0),
        "texture_size": int(raw.get("texture_size") or 1024),
        "max_patches": int(raw.get("max_patches") or 24),
        "lift_m": float(raw.get("lift_m") or 0.05),
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


def _erode(mask: np.ndarray, px: int) -> np.ndarray:
    if px <= 0:
        return mask
    img = Image.fromarray(mask.astype(np.uint8) * 255, mode="L")
    from PIL import ImageFilter

    for _ in range(px):
        img = img.filter(ImageFilter.MinFilter(3))
    return np.asarray(img, dtype=np.uint8) > 0


def _patches(mask: np.ndarray, min_px: int, max_patches: int) -> list[np.ndarray]:
    from scipy.ndimage import label  # noqa: WPS433

    lab, n = label(mask)
    if n <= 0:
        return []
    sizes = np.bincount(lab.ravel())
    order = np.argsort(sizes[1:])[::-1]
    out = []
    for idx in order:
        pid = int(idx) + 1
        if int(sizes[pid]) < min_px:
            break
        out.append(lab == pid)
        if len(out) >= max_patches:
            break
    return out


def _skirt_base(dgm: float, top: float, *, mode: str, plate_m: float) -> float:
    if mode == "full":
        return dgm
    if mode == "three_quarter":
        return dgm + 0.25 * max(0.0, top - dgm)
    return max(dgm, top - float(plate_m))


def _hash01(i: np.ndarray, j: np.ndarray, seed: int) -> np.ndarray:
    x = (i.astype(np.int64) * 374761393 + j.astype(np.int64) * 668265263 + seed) & 0x7FFFFFFF
    x = (x ^ (x >> 13)) * 1274126177
    return ((x ^ (x >> 16)) & 0x7FFFFFFF).astype(np.float64) / float(0x7FFFFFFF)


def _crown_albedo(
    ortho: np.ndarray | None,
    ndsm: np.ndarray,
    keep: np.ndarray,
    tex: int,
) -> np.ndarray:
    if ortho is not None:
        rgb = np.asarray(
            Image.fromarray(ortho, mode="RGB").resize((tex, tex), Image.Resampling.BILINEAR)
        )
    else:
        nd = np.asarray(
            Image.fromarray(ndsm.astype(np.float32), mode="F").resize(
                (tex, tex), Image.Resampling.BILINEAR
            ),
            dtype=np.float32,
        )
        n01 = np.clip(nd / 25.0, 0.0, 1.0)
        yy, xx = np.mgrid[0:tex, 0:tex]
        g = 32.0 + 55.0 * n01 + 12.0 * _hash01(yy, xx, 3)
        r = 18.0 + 22.0 * n01 + 8.0 * _hash01(yy, xx, 7)
        b = 12.0 + 18.0 * n01 + 6.0 * _hash01(yy, xx, 11)
        rgb = np.clip(np.stack([r, g, b], axis=-1), 0, 255).astype(np.uint8)
    km = np.asarray(
        Image.fromarray(keep.astype(np.uint8) * 255, mode="L").resize(
            (tex, tex), Image.Resampling.BILINEAR
        ),
        dtype=np.float32,
    ) / 255.0
    # Darken non-forest so UV bleed is not bright
    rgb = (rgb.astype(np.float32) * km[..., None] + 12.0 * (1.0 - km[..., None])).astype(np.uint8)
    return rgb


def _crown_normal(ndsm: np.ndarray, tex: int) -> np.ndarray:
    nd = np.asarray(
        Image.fromarray(ndsm.astype(np.float32), mode="F").resize(
            (tex, tex), Image.Resampling.BILINEAR
        ),
        dtype=np.float32,
    )
    gy, gx = np.gradient(nd)
    yy, xx = np.mgrid[0:tex, 0:tex]
    gx = gx + 0.35 * (_hash01(yy, xx, 31) - 0.5)
    gy = gy + 0.35 * (_hash01(yy, xx, 37) - 0.5)
    nx, ny, nz = -gx, gy, np.ones_like(gx)
    n = np.sqrt(nx * nx + ny * ny + nz * nz)
    n = np.maximum(n, 1e-6)
    enc = np.stack(
        [
            nx / n * 0.5 + 0.5,
            ny / n * 0.5 + 0.5,
            nz / n * 0.5 + 0.5,
        ],
        axis=-1,
    )
    return np.clip(enc * 255.0, 0, 255).astype(np.uint8)


def _side_albedo(tex: int = 512) -> np.ndarray:
    yy, xx = np.mgrid[0:tex, 0:tex]
    v = yy / max(tex - 1, 1)
    # Darker toward the ground, vertical streaks
    streak = 0.55 + 0.45 * _hash01(yy // 4, xx // 7, 41)
    r = (18 + 22 * v) * streak
    g = (22 + 28 * v) * streak
    b = (12 + 16 * v) * streak
    speckle = 8.0 * (_hash01(yy, xx, 43) - 0.5)
    rgb = np.stack([r, g, b], axis=-1) + speckle[..., None]
    return np.clip(rgb, 0, 255).astype(np.uint8)


def _load_ortho(proc: Path, size: int) -> np.ndarray | None:
    for name in ("ortho.png", "backdrop_near_ortho.png", "backdrop_ortho.png"):
        p = proc / name
        if not p.is_file():
            continue
        im = Image.open(p).convert("RGB")
        if im.size != (size, size):
            im = im.resize((size, size), Image.Resampling.BILINEAR)
        return np.asarray(im, dtype=np.uint8)
    return None


def _build_patch_mesh(
    keep: np.ndarray,
    z_at,
    ndsm: np.ndarray,
    *,
    size: int,
    mpp: float,
    step: int,
    lift_m: float,
    skirt_mode: str,
    plate_m: float,
    r0: int,
    r1: int,
    c0: int,
    c1: int,
) -> tuple[list, list, list, list]:
    rows = list(range(r0, r1, step))
    if rows[-1] != r1 - 1:
        rows.append(r1 - 1)
    cols = list(range(c0, c1, step))
    if cols[-1] != c1 - 1:
        cols.append(c1 - 1)
    nr, nc = len(rows), len(cols)
    on = np.zeros((nr, nc), dtype=bool)
    top: list[tuple[float, float, float]] = []
    bot: list[tuple[float, float, float]] = []
    uv_top: list[tuple[float, float]] = []
    for i, r in enumerate(rows):
        for j, c in enumerate(cols):
            bx = float(c) * mpp
            by = float(size - 1 - r) * mpp
            flag = bool(keep[r, c])
            on[i, j] = flag
            dgm = float(z_at(bx, by))
            nd = float(ndsm[r, c])
            zt = dgm + max(0.0, nd) + lift_m
            zb = _skirt_base(dgm, zt, mode=skirt_mode, plate_m=plate_m)
            top.append((bx, by, zt))
            bot.append((bx, by, zb))
            u = c / max(size - 1, 1)
            v = 1.0 - r / max(size - 1, 1)
            uv_top.append((u, v))

    crown_faces: list[tuple[int, int, int]] = []
    for i in range(nr - 1):
        for j in range(nc - 1):
            if not (on[i, j] or on[i, j + 1] or on[i + 1, j] or on[i + 1, j + 1]):
                continue
            a = i * nc + j
            b = i * nc + (j + 1)
            c = (i + 1) * nc + (j + 1)
            d = (i + 1) * nc + j
            ax, ay, az = top[a]
            bx_, by_, bz = top[b]
            cx_, cy_, cz = top[c]
            nz = (bx_ - ax) * (cy_ - ay) - (by_ - ay) * (cx_ - ax)
            if nz >= 0:
                crown_faces.append((a, b, c))
                crown_faces.append((a, c, d))
            else:
                crown_faces.append((a, c, b))
                crown_faces.append((a, d, c))

    # Skirt on edges where a keep-cell meets empty. Bottom verts appended after top.
    side_verts: list[tuple[float, float, float]] = []
    side_uvs: list[tuple[float, float]] = []
    side_faces: list[tuple[int, int, int]] = []
    dist_acc = 0.0

    def add_skirt(i0, j0, i1, j1, i_cell, j_cell):
        nonlocal dist_acc
        a = i0 * nc + j0
        b = i1 * nc + j1
        t0, t1 = top[a], top[b]
        b0, b1 = bot[a], bot[b]
        cell_xy = (
            (top[i_cell * nc + j_cell][0] + top[i_cell * nc + min(j_cell + 1, nc - 1)][0]) * 0.5,
            (top[i_cell * nc + j_cell][1] + top[min(i_cell + 1, nr - 1) * nc + j_cell][1]) * 0.5,
        )
        edge_mid = ((t0[0] + t1[0]) * 0.5, (t0[1] + t1[1]) * 0.5)
        outward = (edge_mid[0] - cell_xy[0], edge_mid[1] - cell_xy[1])
        base = len(side_verts)
        # order b0, b1, t1, t0
        side_verts.extend([b0, b1, t1, t0])
        span = math.hypot(t1[0] - t0[0], t1[1] - t0[1])
        u0 = dist_acc / 8.0
        u1 = (dist_acc + span) / 8.0
        dist_acc += span
        side_uvs.extend([(u0, 0.0), (u1, 0.0), (u1, 1.0), (u0, 1.0)])
        # (b1-b0) x (t1-b0)
        e1 = (b1[0] - b0[0], b1[1] - b0[1], b1[2] - b0[2])
        e2 = (t1[0] - b0[0], t1[1] - b0[1], t1[2] - b0[2])
        nxy = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2])
        if nxy[0] * outward[0] + nxy[1] * outward[1] >= 0:
            side_faces.append((base, base + 1, base + 2))
            side_faces.append((base, base + 2, base + 3))
        else:
            side_faces.append((base, base + 2, base + 1))
            side_faces.append((base, base + 3, base + 2))

    for i in range(nr - 1):
        for j in range(nc - 1):
            if not on[i, j]:
                continue
            # north (smaller row index) empty
            if i == 0 or not on[i - 1, j]:
                add_skirt(i, j, i, j + 1, i, j)
            # south
            if i == nr - 2 or not on[i + 1, j]:
                add_skirt(i + 1, j + 1, i + 1, j, i, j)
            # west (smaller col)
            if j == 0 or not on[i, j - 1]:
                add_skirt(i + 1, j, i, j, i, j)
            # east
            if j == nc - 2 or not on[i, j + 1]:
                add_skirt(i, j + 1, i + 1, j + 1, i, j)

    # Merge: crown uses top verts 0..N-1; side uses its own list appended
    n_top = len(top)
    verts = list(top) + side_verts
    uvs = list(uv_top) + side_uvs
    side_faces_off = [(a + n_top, b + n_top, c + n_top) for a, b, c in side_faces]
    return verts, crown_faces, side_faces_off, uvs


def _write_collada(
    path: Path,
    verts: list[tuple[float, float, float]],
    uvs: list[tuple[float, float]],
    crown_faces: list[tuple[int, int, int]],
    side_faces: list[tuple[int, int, int]],
    *,
    stem: str,
    crown_png: str,
    side_png: str,
) -> None:
    n_vert = len(verts)
    if n_vert > COLLADA_MAX_VERTS:
        raise SystemExit(f"{stem}: {n_vert} verts exceeds Collada limit")
    lod = f"{stem}_a999"
    pos_vals = " ".join(f"{x:.3f} {y:.3f} {z:.3f}" for x, y, z in verts)
    uv_vals = " ".join(f"{u:.5f} {v:.5f}" for u, v in uvs)

    def tris(mat: str, faces: list[tuple[int, int, int]]) -> str:
        p = " ".join(f"{a} {a} {b} {b} {c} {c}" for a, b, c in faces)
        return (
            f'        <triangles material="{mat}" count="{len(faces)}">\n'
            f'          <input semantic="VERTEX" source="#{lod}-mesh-vertices" offset="0"/>\n'
            f'          <input semantic="TEXCOORD" source="#{lod}-mesh-map-0" offset="1" set="0"/>\n'
            f"          <p>{p}</p>\n"
            f"        </triangles>"
        )

    mc, ms = escape(MAT_CROWN), escape(MAT_SIDE)
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor><authoring_tool>beamng_autoroad build_forest_hull</authoring_tool></contributor>
    <unit name="meter" meter="1"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_images>
    <image id="{mc}-image" name="{mc}-image"><init_from>{escape(crown_png)}</init_from></image>
    <image id="{ms}-image" name="{ms}-image"><init_from>{escape(side_png)}</init_from></image>
  </library_images>
  <library_effects>
    <effect id="{mc}-effect">
      <profile_COMMON>
        <newparam sid="{mc}-surface"><surface type="2D"><init_from>{mc}-image</init_from></surface></newparam>
        <newparam sid="{mc}-sampler"><sampler2D><source>{mc}-surface</source></sampler2D></newparam>
        <technique sid="common">
          <lambert><diffuse><texture texture="{mc}-sampler" texcoord="UVSET0"/></diffuse></lambert>
        </technique>
      </profile_COMMON>
    </effect>
    <effect id="{ms}-effect">
      <profile_COMMON>
        <newparam sid="{ms}-surface"><surface type="2D"><init_from>{ms}-image</init_from></surface></newparam>
        <newparam sid="{ms}-sampler"><sampler2D><source>{ms}-surface</source></sampler2D></newparam>
        <technique sid="common">
          <lambert><diffuse><texture texture="{ms}-sampler" texcoord="UVSET0"/></diffuse></lambert>
        </technique>
      </profile_COMMON>
    </effect>
  </library_effects>
  <library_materials>
    <material id="{mc}-material" name="{mc}"><instance_effect url="#{mc}-effect"/></material>
    <material id="{ms}-material" name="{ms}"><instance_effect url="#{ms}-effect"/></material>
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
{tris(mc, crown_faces)}
{tris(ms, side_faces)}
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="{lod}" name="{lod}" type="NODE">
        <instance_geometry url="#{lod}-mesh">
          <bind_material>
            <technique_common>
              <instance_material symbol="{mc}" target="#{mc}-material">
                <bind_vertex_input semantic="UVSET0" input_semantic="TEXCOORD" input_set="0"/>
              </instance_material>
              <instance_material symbol="{ms}" target="#{ms}-material">
                <bind_vertex_input semantic="UVSET0" input_semantic="TEXCOORD" input_set="0"/>
              </instance_material>
            </technique_common>
          </bind_material>
        </instance_geometry>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-8", newline="\n")


def _write_materials(level_name: str, mesh_dir: Path, crown_png: str, nrm_png: str, side_png: str) -> None:
    base = f"/levels/{level_name}/art/shapes/forest_hull"
    data = {
        MAT_CROWN: {
            "name": MAT_CROWN,
            "mapTo": MAT_CROWN,
            "class": "Material",
            "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"autoroad:hull:{MAT_CROWN}")),
            "Stages": [
                {
                    "baseColorMap": f"{base}/{crown_png}",
                    "normalMap": f"{base}/{nrm_png}",
                    "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
                    "roughnessFactor": 0.9,
                    "metallicFactor": 0.0,
                },
                {},
                {},
                {},
            ],
            "annotation": "TREE",
            "castShadows": True,
            "alphaTest": False,
            "doubleSided": False,
            "version": 1.5,
        },
        MAT_SIDE: {
            "name": MAT_SIDE,
            "mapTo": MAT_SIDE,
            "class": "Material",
            "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"autoroad:hull:{MAT_SIDE}")),
            "Stages": [
                {
                    "baseColorMap": f"{base}/{side_png}",
                    "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
                    "roughnessFactor": 0.95,
                    "metallicFactor": 0.0,
                },
                {},
                {},
                {},
            ],
            "annotation": "TREE",
            "castShadows": True,
            "alphaTest": False,
            "doubleSided": False,
            "version": 1.5,
        },
    }
    (mesh_dir / "main.materials.json").write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )


def _inject(level_name: str, user_level: Path) -> None:
    group = user_level / "main" / "MissionGroup" / "level_objects" / "forest_hull"
    entry = {
        "name": STEM,
        "class": "TSStatic",
        "__parent": "forest_hull",
        "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"autoroad:hull:{STEM}")),
        "position": [0, 0, 0],
        "rotationMatrix": IDENTITY_ROT,
        "scale": [1.0, 1.0, 1.0],
        "shapeName": f"/levels/{level_name}/art/shapes/forest_hull/{STEM}.dae",
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
    if "forest_hull" not in names:
        rows.append(
            {
                "name": "forest_hull",
                "class": "SimGroup",
                "__parent": "level_objects",
                "enabled": "1",
                "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "autoroad:hull:group")),
            }
        )
        _write_ndjson(lo, rows)
        print("Registered SimGroup forest_hull")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args()
    site = load_site()
    cfg = _cfg(site)
    if not cfg["enabled"]:
        raise SystemExit("beamng.forest_hull.enabled is false")
    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "").strip()
    if not level_name:
        raise SystemExit("beamng.level_name missing")
    size = int(bng.get("mask_size") or 2048)
    mpp = float(bng.get("meters_per_pixel") or 1.0)
    proc = processed_dir(site)
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        raise SystemExit(f"Level missing: {user_level}")

    forest = bf._load_mask(proc / "mask_forest_high.png", size)
    ndsm_path = proc / "ndsm.tif"
    if not ndsm_path.is_file():
        raise SystemExit(f"Missing {ndsm_path}")
    ndsm = np.asarray(tiff.imread(ndsm_path), dtype=np.float32)
    if ndsm.ndim == 3:
        ndsm = ndsm[..., 0]
    if ndsm.shape[0] != size:
        ndsm = np.asarray(
            Image.fromarray(ndsm, mode="F").resize((size, size), Image.Resampling.BILINEAR),
            dtype=np.float32,
        )
    dense = forest & (ndsm >= float(cfg["min_ndsm_m"]))
    rim_px = max(1, int(round(float(cfg["rim_m"]) / mpp)))
    interior = _erode(dense, rim_px)
    min_px = max(8, int(round(float(cfg["min_area_m2"]) / (mpp * mpp))))
    patches = _patches(interior, min_px, int(cfg["max_patches"]))
    union = np.zeros_like(interior)
    for p in patches:
        union |= p
    Image.fromarray(union.astype(np.uint8) * 255, mode="L").save(
        proc / "mask_forest_hull_interior.png"
    )
    print(
        f"Hull patches={len(patches)} interior={100.0 * float(union.mean()):.2f}% "
        f"rim={cfg['rim_m']} m skirt={cfg['skirt']}"
    )
    if not patches:
        raise SystemExit("No hull patches — lower min_area_m2 / min_ndsm_m or rim_m")

    z_at, _slope = bg.load_terrain_z_slope(site)
    step = max(1, int(round(float(cfg["step_m"]) / mpp)))
    all_verts: list = []
    all_uvs: list = []
    crown_faces: list = []
    side_faces: list = []
    for pi, patch in enumerate(patches):
        rs, cs = np.where(patch)
        r0, r1 = int(rs.min()), int(rs.max()) + 1
        c0, c1 = int(cs.min()), int(cs.max()) + 1
        verts, cf, sf, uvs = _build_patch_mesh(
            patch,
            z_at,
            ndsm,
            size=size,
            mpp=mpp,
            step=step,
            lift_m=float(cfg["lift_m"]),
            skirt_mode=str(cfg["skirt"]),
            plate_m=float(cfg["plate_thickness_m"]),
            r0=r0,
            r1=r1,
            c0=c0,
            c1=c1,
        )
        off = len(all_verts)
        all_verts.extend(verts)
        all_uvs.extend(uvs)
        crown_faces.extend((a + off, b + off, c + off) for a, b, c in cf)
        side_faces.extend((a + off, b + off, c + off) for a, b, c in sf)
        print(f"  patch {pi}: verts={len(verts)} crown_tris={len(cf)} side_tris={len(sf)}")
        if len(all_verts) > COLLADA_MAX_VERTS:
            raise SystemExit("Hull vert cap — raise step_m or lower max_patches")

    ortho = _load_ortho(proc, size)
    tex = int(cfg["texture_size"])
    crown_rgb = _crown_albedo(ortho, ndsm, union, tex)
    nrm = _crown_normal(ndsm, tex)
    side = _side_albedo(512)
    mesh_dir = user_level / "art" / "shapes" / "forest_hull"
    mesh_dir.mkdir(parents=True, exist_ok=True)
    crown_png, nrm_png, side_png = f"{STEM}_crown.png", f"{STEM}_n.png", f"{STEM}_side.png"
    Image.fromarray(crown_rgb, mode="RGB").save(mesh_dir / crown_png)
    Image.fromarray(nrm, mode="RGB").save(mesh_dir / nrm_png)
    Image.fromarray(side, mode="RGB").save(mesh_dir / side_png)
    Image.fromarray(crown_rgb, mode="RGB").save(proc / "preview_forest_hull_crown.png")
    _write_materials(level_name, mesh_dir, crown_png, nrm_png, side_png)
    _write_collada(
        mesh_dir / f"{STEM}.dae",
        all_verts,
        all_uvs,
        crown_faces,
        side_faces,
        stem=STEM,
        crown_png=crown_png,
        side_png=side_png,
    )
    _inject(level_name, user_level)
    summary = {
        "site": site_slug(site),
        "level": level_name,
        "patches": len(patches),
        "verts": len(all_verts),
        "crown_tris": len(crown_faces),
        "side_tris": len(side_faces),
        "cfg": cfg,
        "note": "Quit BeamNG fully. Imposters inside union are skipped when forest.hull_skip_interior is on.",
    }
    (proc / "forest_hull_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Wrote {proc / 'forest_hull_summary.json'}")
    print("Quit BeamNG fully and restart to load the hull TSStatic.")


if __name__ == "__main__":
    main()
