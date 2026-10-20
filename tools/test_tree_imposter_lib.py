"""Atlas order, DXT5nm layout, cell-aware mips, cheap DAE winding.

  cd C:\\temp\\beamng_autoroad; python tools\\test_tree_imposter_lib.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tree_imposter_lib import (
    ALPHA_REF,
    atlas_size,
    cell_origin,
    cheap_lod_geometry,
    downsample_cells,
    imposter_count,
    iter_views,
    mip_chain,
    pack_and_write,
    pack_cells,
    _LOWER_GEOM,
    patch_spruce_dae_xml,
    species_cfg,
    spruce_far_quads,
    encode_tangent_normal,
    spruce_view_basis,
    spruce_view_quad,
    attach_camera_billboard,
    crown_occlusion_factor,
    billboard_slot,
    encode_billboard_normal,
    spruce_view_basis,
    world_normal_to_dxt5nm,
    write_dds_dxt,
)


def test_spruce_view_count() -> None:
    spec = species_cfg("spruce")
    views = iter_views(
        spec["equator_steps"],
        spec["polar_steps"],
        spec["include_poles"],
        spec["polar_angle"],
    )
    n = imposter_count(spec["equator_steps"], spec["polar_steps"], spec["include_poles"])
    assert n == 16, n
    assert len(views) == n, len(views)
    assert views[0].kind == "polar" and abs(views[0].rot_z + np.pi / 2) < 1e-6
    assert all(v.kind == "polar" for v in views)
    assert all(abs(v.rot_x) < 1e-5 for v in views), [v.rot_x for v in views]


def test_atlas_pack_order() -> None:
    dim = 32
    n = 26
    w, h = atlas_size(n, dim)
    assert w % dim == 0 and h % dim == 0
    cells = []
    for i in range(n):
        c = np.zeros((dim, dim, 4), dtype=np.float32)
        c[..., 0] = (i + 1) / 255.0
        c[..., 3] = 1.0
        cells.append(c)
    atlas = pack_cells(cells, dim, (w, h))
    for i in range(n):
        x, y = cell_origin(i, dim, w)
        val = atlas[y + dim // 2, x + dim // 2, 0]
        assert abs(val - (i + 1) / 255.0) < 1e-5, (i, val)


def test_dxt5nm_z_in_alpha() -> None:
    img = np.zeros((8, 8, 4), dtype=np.float32)
    # world +Z -> encoded (0.5, 0.5, 1) then DXT5nm X=0.5 Y=0.5 Z=1 in alpha
    img[..., 0] = 0.5
    img[..., 1] = 0.5
    img[..., 2] = 1.0
    img[..., 3] = 1.0
    enc = world_normal_to_dxt5nm(img)
    assert abs(float(enc[0, 0, 0]) - 0.5) < 0.02
    assert abs(float(enc[0, 0, 1]) - 0.5) < 0.02
    assert float(enc[0, 0, 2]) == 0.0
    assert abs(float(enc[0, 0, 3]) - 1.0) < 0.02
    dead = img.copy()
    dead[..., 3] = 0.0
    enc_d = world_normal_to_dxt5nm(dead)
    assert float(enc_d[0, 0].sum()) == 0.0


def test_mip_does_not_bleed_cells() -> None:
    dim = 16
    w, h = 32, 16
    atlas = np.zeros((h, w, 4), dtype=np.float32)
    atlas[:, :dim, 0] = 1.0
    atlas[:, :dim, 3] = 1.0
    atlas[:, dim:, 1] = 1.0
    atlas[:, dim:, 3] = 1.0
    small, nd = downsample_cells(atlas, dim)
    assert nd == 8
    # left cell stays red, right green — no yellow mix in the middle of a cell
    assert float(small[4, 4, 0]) > 0.9 and float(small[4, 4, 1]) < 0.1
    assert float(small[4, 12, 1]) > 0.9 and float(small[4, 12, 0]) < 0.1


def test_mip_chain_stops_at_2px() -> None:
    # 1024 atlas, 256 px cells: 1024 … 4 are cell mips, plus one 2 px level = 10.
    atlas = np.zeros((1024, 1024, 4), dtype=np.float32)
    levels = mip_chain(atlas, 256)
    assert len(levels) == 10, len(levels)
    assert levels[-1].shape[:2] == (2, 2)


def test_pack_writes_sidecars() -> None:
    spec = species_cfg("spruce")
    dim = 32
    spec = dict(spec)
    spec["dimension"] = dim
    n = imposter_count(spec["equator_steps"], spec["polar_steps"], spec["include_poles"])
    cells_a, cells_n = [], []
    for i in range(n):
        a = np.zeros((dim, dim, 4), dtype=np.float32)
        a[4:-4, 4:-4, 1] = 0.4
        a[4:-4, 4:-4, 3] = 1.0
        nrm = np.zeros((dim, dim, 4), dtype=np.float32)
        nrm[4:-4, 4:-4, :] = (0.5, 0.5, 1.0, 1.0)
        cells_a.append(a)
        cells_n.append(nrm)
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        info = pack_and_write(cells_a, cells_n, out, "tree_spruce_large.dae", spec)
        assert (out / "tree_spruce_large.dae.imposter.dds").is_file()
        assert (out / "tree_spruce_large.dae.imposter_normals.dds").is_file()
        assert (out / "tree_spruce_large.dae.asset.json").is_file()
        raw = (out / "tree_spruce_large.dae.imposter.dds").read_bytes()
        assert raw[:4] == b"DDS "
        assert b"DXT3" in raw[:128]
        import struct

        mip_count = struct.unpack_from("<I", raw, 28)[0]
        rgb_bits, r_mask = struct.unpack_from("<II", raw, 88)
        assert mip_count >= 2, mip_count
        assert rgb_bits == 32 and r_mask == 0xFF
        nraw = (out / "tree_spruce_large.dae.imposter_normals.dds").read_bytes()
        assert b"DXT5" in nraw[:128]
        import json

        asset = json.loads((out / "tree_spruce_large.dae.asset.json").read_text(encoding="utf-8"))
        block = asset["imposters"][0]
        assert block["equatorSteps"] == 16
        assert block["polarSteps"] == 0
        assert block["includePoles"] is False
        assert block["dimension"] == dim
        assert info["cells"] == n
        _ = ALPHA_REF
        write_dds_dxt  # imported for side-effect-free smoke


def test_cheap_lod_outward_faces() -> None:
    geo = cheap_lod_geometry(20.0, 4.0)
    # first trunk quad: (b-a) x (c-a) · radial > 0
    v = geo["trunk_verts"]
    f = geo["trunk_faces"][0]
    a = np.array(v[f[0]])
    b = np.array(v[f[1]])
    c = np.array(v[f[2]])
    nrm = np.cross(b - a, c - a)
    mid = (a + b + c) / 3.0
    radial = np.array([mid[0], mid[1], 0.0])
    assert float(np.dot(nrm, radial)) > 0.0, (nrm, radial)


def test_spruce_far_cards_face_camera_and_drop_billboard() -> None:
    quads = spruce_far_quads(uv_rect=(0.2, 0.1, 0.8, 0.9))
    assert len(quads) == 3
    for deg, quad in zip((0.0, 60.0, 120.0), quads):
        a, b, c = (np.array(p) for p in quad[:3])
        nrm = np.cross(b - a, c - a)
        ang = np.deg2rad(deg)
        # View-0 camera sits on -X; each card's normal follows that offset around Z.
        direction = np.array([-np.cos(ang), -np.sin(ang), 0.0])
        assert float(np.dot(nrm, direction)) > 0.0, (deg, nrm, direction)
    tiny = """<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <library_images>
    <image id="SpruceBark-image" name="SpruceBark-image"><init_from>bark_spruce.png</init_from></image>
  </library_images>
  <library_effects>
    <effect id="SpruceBark-effect">
      <profile_COMMON></profile_COMMON>
    </effect>
  </library_effects>
  <library_materials>
    <material id="SpruceBark-material" name="SpruceBark"><instance_effect url="#SpruceBark-effect"/></material>
  </library_materials>
  <library_geometries>
    <geometry id="tree_spruce_large_a1000-mesh" name="tree_spruce_large_a1000-mesh">
      <mesh><source id="tree_spruce_large_a1000-mesh-positions"></source></mesh>
    </geometry>
    <geometry id="tree_spruce_large_a500-mesh" name="tree_spruce_large_a500-mesh">
      <mesh></mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="base00" name="base00" type="NODE">
        <node id="bb_autobillboard25" name="bb_autobillboard25" type="NODE"/>
        <node id="start01" name="start01" type="NODE">
          <node id="tree_spruce_large_a1000" name="tree_spruce_large_a1000" type="NODE">
            <instance_geometry url="#tree_spruce_large_a1000-mesh">
              <bind_material><technique_common>
                  <instance_material symbol="SpruceBark" target="#SpruceBark-material"/>
              </technique_common></bind_material>
            </instance_geometry>
          </node>
          <node id="tree_spruce_large_a500" name="tree_spruce_large_a500" type="NODE">
            <instance_geometry url="#tree_spruce_large_a500-mesh"/>
          </node>
        </node>
      </node>
    </visual_scene>
  </library_visual_scenes>
</COLLADA>
"""
    out = patch_spruce_dae_xml(tiny, quads)
    assert "bb_autobillboard" not in out
    assert "tree_spruce_large_a1000" in out
    assert "tree_spruce_large_a500" not in out
    assert "tree_spruce_large_a2500" not in out
    assert "tree_spruce_large_a25" in out
    assert out.count("SpruceCard-material") >= 1
    assert out.count("<library_visual_scenes>") == 1


def test_card_normal_is_tangent_space_directx() -> None:
    facing = encode_tangent_normal(np.array([-1.0, 0.0, 0.0]), 0)
    assert np.allclose(facing, [0.5, 0.5, 1.0], atol=1e-5), facing
    upward = encode_tangent_normal(np.array([0.0, 0.0, 1.0]), 0)
    assert np.allclose(upward, [0.5, 0.0, 0.5], atol=1e-5), upward
    away = encode_tangent_normal(np.array([1.0, 0.0, 0.0]), 0)
    assert float(away[2]) < 0.05, away


def test_mid_views_face_their_cameras_and_keep_near_mesh() -> None:
    sample = (
        '    <geometry id="tree_spruce_large_a1000-mesh" name="tree_spruce_large_a1000-mesh">\n'
        "      <mesh></mesh>\n    </geometry>\n"
        '    <geometry id="tree_spruce_large_a80-mesh" name="tree_spruce_large_a80-mesh">\n'
        "      <mesh></mesh>\n    </geometry>\n"
    )
    kept = _LOWER_GEOM.sub("", sample)
    assert "a1000-mesh" in kept
    assert "a80-mesh" not in kept
    for index in (0, 4, 8, 12):
        quad = spruce_view_quad(index, (0.2, 0.1, 0.8, 0.9))
        a, b, c = (np.array(p) for p in quad[:3])
        nrm = np.cross(b - a, c - a)
        direction, _right = spruce_view_basis(index)
        assert float(np.dot(nrm, direction)) > 0.0, (index, nrm, direction)
    tiny = """<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <library_images>
    <image id="SpruceBark-image" name="SpruceBark-image"><init_from>bark_spruce.png</init_from></image>
  </library_images>
  <library_effects>
    <effect id="SpruceBark-effect">
      <profile_COMMON></profile_COMMON>
    </effect>
  </library_effects>
  <library_materials>
    <material id="SpruceBark-material" name="SpruceBark"><instance_effect url="#SpruceBark-effect"/></material>
  </library_materials>
  <library_geometries>
    <geometry id="tree_spruce_large_a1000-mesh" name="tree_spruce_large_a1000-mesh">
      <mesh><source id="tree_spruce_large_a1000-mesh-positions"></source></mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="base00" name="base00" type="NODE">
        <node id="start01" name="start01" type="NODE">
          <node id="tree_spruce_large_a1000" name="tree_spruce_large_a1000" type="NODE">
            <instance_geometry url="#tree_spruce_large_a1000-mesh"/>
          </node>
        </node>
      </node>
    </visual_scene>
  </library_visual_scenes>
</COLLADA>
"""
    mid_quads = [spruce_view_quad(0, (0.2, 0.1, 0.8, 0.9))]
    mid_uvs = [[(0.1, 0.2), (0.3, 0.2), (0.3, 0.8), (0.1, 0.8)]]
    out = patch_spruce_dae_xml(tiny, spruce_far_quads(), mid_quads=mid_quads, mid_uvs=mid_uvs)
    assert "tree_spruce_large_a1000-mesh" in out
    assert 'id="tree_spruce_large_a80"' in out
    assert "double_sided" not in out.split('id="SpruceMid-effect"')[1].split("</effect>")[0]


def test_hull_skirt_keeps_crown_z() -> None:
    from build_forest_hull import _skirt_base

    dgm, top = 100.0, 120.0
    assert _skirt_base(dgm, top, mode="full", plate_m=6.0) == 100.0
    assert abs(_skirt_base(dgm, top, mode="three_quarter", plate_m=6.0) - 105.0) < 1e-6
    assert abs(_skirt_base(dgm, top, mode="plate", plate_m=6.0) - 114.0) < 1e-6
    # plate never sinks below DGM
    assert _skirt_base(dgm, dgm + 2.0, mode="plate", plate_m=6.0) == dgm


def test_crown_occlusion_keeps_tips_and_drops_interior() -> None:
    ao = np.zeros((4, 4), dtype=np.float32)
    ao[:, :2] = 0.1
    ao[:, 2:] = 0.9
    covered = np.ones((4, 4), dtype=bool)
    factor = crown_occlusion_factor(ao, covered, out_lo=0.45)
    assert float(factor[:, 2].mean()) > 0.95
    assert abs(float(factor[:, 0].mean()) - 0.45) < 0.05


def test_billboard_slots_are_permutation() -> None:
    slots = [billboard_slot(spruce_view_basis(i)[0]) for i in range(16)]
    assert sorted(slots) == list(range(16)), slots
    assert slots[0] == 8, slots[0]


def test_billboard_normal_faces_camera() -> None:
    direction, _right = spruce_view_basis(0)
    facing = np.zeros((1, 1, 4), dtype=np.float32)
    facing[..., :3] = np.array(direction) * 0.5 + 0.5
    facing[..., 3] = 1.0
    enc = encode_billboard_normal(facing, 0)
    assert abs(float(enc[0, 0, 3]) - 0.5) < 0.02, float(enc[0, 0, 3])
    assert abs(float(enc[0, 0, 1]) - 0.5) < 0.02, float(enc[0, 0, 1])
    up = np.zeros((1, 1, 4), dtype=np.float32)
    up[..., 0] = 0.5
    up[..., 1] = 0.5
    up[..., 2] = 1.0
    up[..., 3] = 1.0
    enc_up = encode_billboard_normal(up, 0)
    assert abs(float(enc_up[0, 0, 1]) - 1.0) < 0.02, float(enc_up[0, 0, 1])
    assert abs(float(enc_up[0, 0, 3]) - 0.5) < 0.02, float(enc_up[0, 0, 3])


def test_attach_camera_billboard_drops_cards() -> None:
    sample = """
      <node id="base00" name="base00" type="NODE">
        <node id="start01" name="start01" type="NODE">
          <node id="tree_spruce_large_a1000" name="tree_spruce_large_a1000" type="NODE">
          </node>
          <node id="tree_spruce_large_a80" name="tree_spruce_large_a80" type="NODE">
          </node>
          <node id="tree_spruce_large_a25" name="tree_spruce_large_a25" type="NODE">
          </node>
        </node>
      </node>
"""
    out = attach_camera_billboard(sample)
    assert "bb_autobillboard25" in out
    assert "tree_spruce_large_a1000" in out
    assert 'id="tree_spruce_large_a80"' not in out
    assert 'id="tree_spruce_large_a25"' not in out


def main() -> None:
    test_spruce_view_count()
    test_atlas_pack_order()
    test_dxt5nm_z_in_alpha()
    test_mip_does_not_bleed_cells()
    test_mip_chain_stops_at_2px()
    test_pack_writes_sidecars()
    test_cheap_lod_outward_faces()
    test_spruce_far_cards_face_camera_and_drop_billboard()
    test_card_normal_is_tangent_space_directx()
    test_mid_views_face_their_cameras_and_keep_near_mesh()
    test_hull_skirt_keeps_crown_z()
    test_billboard_slots_are_permutation()
    test_billboard_normal_faces_camera()
    test_attach_camera_billboard_drops_cards()
    test_crown_occlusion_keeps_tips_and_drops_interior()
    print("test_tree_imposter_lib: ok")


if __name__ == "__main__":
    main()
