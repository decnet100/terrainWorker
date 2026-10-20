"""Torque/BeamNG tree-imposter atlas: views, packing, DXT5nm, DDS, asset.json.

View order matches TSLastDetail::_update (equator within each polar ring,
then one top and one bottom pole). Cell-aware mips: never filter across
atlas cell borders.

Species presets follow the alpine plan (Fichte first).
"""
from __future__ import annotations

import json
import math
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from xml.sax.saxutils import escape

import numpy as np

MAX_TEX = 2048
ALPHA_REF = 84  # Torque imposter alpha-test
DXT5NM_Z_IN_ALPHA = True

SPECIES: dict[str, dict[str, Any]] = {
    "spruce": {
        "equator_steps": 16,
        "polar_steps": 0,
        "include_poles": False,
        "polar_angle": 0.0,
        "dimension": 256,
        # One mesh detail (_a1000). The engine photographs that index if it rebakes.
        "detail_level": 0,
        "size": 25,
        "world_size_m": 32.0,
        "dae_stem": "tree_spruce_large",
        "blend": "fichte2.blend",
        "bark_hints": ("bark", "rinde", "stamm", "trunk"),
        "leaf_hints": ("twig", "leaf", "nadel", "foliage", "needle"),
    },
    "larch": {
        "equator_steps": 12,
        "polar_steps": 1,
        "include_poles": True,
        "polar_angle": 35.0,
        "dimension": 256,
        "detail_level": 0,
        "size": 25,
        "world_size_m": 20.0,
        "dae_stem": "tree_larch_large",
        "blend": "laerche_versuch.blend",
        "bark_hints": ("bark", "rinde", "stamm", "trunk"),
        "leaf_hints": ("twig", "leaf", "nadel", "foliage", "needle"),
    },
    "maple": {
        "equator_steps": 16,
        "polar_steps": 1,
        "include_poles": True,
        "polar_angle": 35.0,
        "dimension": 256,
        "detail_level": 0,
        "size": 25,
        "world_size_m": 18.0,
        "dae_stem": "tree_maple_large",
        "blend": "bergahorn.blend",
        "bark_hints": ("bark", "rinde", "stamm", "trunk"),
        "leaf_hints": ("leaf", "blatt", "foliage"),
    },
    "arve": {
        "equator_steps": 8,
        "polar_steps": 1,
        "include_poles": True,
        "polar_angle": 35.0,
        "dimension": 256,
        "detail_level": 0,
        "size": 25,
        "world_size_m": 18.0,
        "dae_stem": "tree_arve_large",
        "blend": "arve.blend",
        "bark_hints": ("bark", "rinde", "stamm", "trunk"),
        "leaf_hints": ("twig", "leaf", "nadel", "foliage", "needle"),
    },
}


@dataclass(frozen=True)
class ImposterView:
    """One snapshot. rot_x / rot_z are Torque object Euler radians."""

    kind: str  # polar | pole_top | pole_bottom
    polar_index: int
    equator_index: int
    rot_x: float
    rot_z: float


def species_cfg(name: str) -> dict[str, Any]:
    key = str(name).strip().lower()
    if key not in SPECIES:
        raise SystemExit(f"Unknown tree species '{name}'. Known: {sorted(SPECIES)}")
    return dict(SPECIES[key])


def imposter_count(equator: int, polar: int, poles: bool) -> int:
    return ((2 * int(polar) + 1) * int(equator)) + (2 if poles else 0)


def iter_views(
    equator_steps: int,
    polar_steps: int,
    include_poles: bool,
    polar_angle_deg: float,
) -> list[ImposterView]:
    """Same order as Torque TSLastDetail snapshots, poles once after all rings.

    Torque's C++ has includePoles inside the polar loop (would duplicate poles
    when polar_steps>0). The count formula and the docs add two pole images
    total; we follow that.
    """
    eq = max(1, int(equator_steps))
    pol = max(0, int(polar_steps))
    equator_step = 2.0 * math.pi / float(eq)
    polar_ang = math.radians(float(polar_angle_deg))
    polar_step = 0.0
    if pol > 0:
        polar_step = -(0.5 * math.pi - polar_ang) / float(pol)
    rot_x = 0.0
    if pol > 0:
        rot_x = -(polar_ang - 0.5 * math.pi)
    out: list[ImposterView] = []
    n_rings = 2 * pol + 1
    for j in range(n_rings):
        rot_z = -math.pi / 2.0
        for k in range(eq):
            out.append(
                ImposterView("polar", j, k, float(rot_x), float(rot_z))
            )
            rot_z += equator_step
        rot_x += polar_step
    if include_poles:
        out.append(ImposterView("pole_top", -1, 0, -math.pi / 2.0, 0.0))
        out.append(ImposterView("pole_bottom", -1, 1, math.pi / 2.0, 0.0))
    return out


def atlas_size(n_cells: int, dim: int, max_tex: int = MAX_TEX) -> tuple[int, int]:
    """Smallest power-of-two rectangle that holds n_cells of size dim."""
    dim = max(32, int(dim))
    n_cells = max(1, int(n_cells))
    side = int(max_tex)
    tex = [side, side]
    while True:
        half = [tex[0] // 2, tex[1] // 2]
        if half[0] < dim or (half[0] // dim) * (half[1] // dim) < n_cells:
            if (tex[0] // dim) * (half[1] // dim) >= n_cells and half[1] >= dim:
                tex[1] = half[1]
            break
        tex = half
    cols = max(1, tex[0] // dim)
    rows = int(math.ceil(n_cells / cols))
    h = 1
    while h < rows * dim:
        h *= 2
    tex[1] = max(dim, h)
    if (tex[0] // dim) * (tex[1] // dim) < n_cells:
        raise ValueError(f"Cannot pack {n_cells} cells of {dim} into {max_tex}")
    return int(tex[0]), int(tex[1])


def cell_origin(index: int, dim: int, atlas_w: int) -> tuple[int, int]:
    cols = atlas_w // dim
    return (index % cols) * dim, (index // cols) * dim


def pack_cells(
    cells: list[np.ndarray], dim: int, atlas_wh: tuple[int, int]
) -> np.ndarray:
    """Pack RGBA float or uint8 cells row-major into an atlas."""
    w, h = atlas_wh
    atlas = np.zeros((h, w, 4), dtype=np.float32)
    for i, cell in enumerate(cells):
        arr = np.asarray(cell, dtype=np.float32)
        if arr.max() > 1.5:
            arr = arr / 255.0
        if arr.shape[0] != dim or arr.shape[1] != dim:
            raise ValueError(f"cell {i} is {arr.shape[:2]}, expected {dim}x{dim}")
        x, y = cell_origin(i, dim, w)
        atlas[y : y + dim, x : x + dim] = arr[..., :4]
    return atlas


def alpha_bleed(arr: np.ndarray, iterations: int = 8) -> np.ndarray:
    """Fill transparent RGB from opaque neighbours (Torque _colorAverageFilter)."""
    out = np.asarray(arr, dtype=np.float32).copy()
    if out.max() > 1.5:
        out = out / 255.0
    rgb = out[..., :3].copy()
    filled = out[..., 3] > (ALPHA_REF / 255.0)
    for _ in range(int(iterations)):
        acc = np.zeros_like(rgb)
        cnt = np.zeros(filled.shape, dtype=np.float32)
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            f = np.roll(filled, (dy, dx), axis=(0, 1))
            c = np.roll(rgb, (dy, dx), axis=(0, 1))
            acc += c * f[..., None]
            cnt += f
        new = (~filled) & (cnt > 0)
        if not np.any(new):
            break
        rgb[new] = acc[new] / cnt[new][:, None]
        filled = filled | new
    out[..., :3] = rgb
    return out


def world_normal_to_dxt5nm(arr: np.ndarray) -> np.ndarray:
    """World-space unit normals (RGB 0-1 = -1..1) -> DXT5nm (X,Y,0,Z)."""
    a = np.asarray(arr, dtype=np.float32)
    if a.max() > 1.5:
        a = a / 255.0
    xyz = a[..., :3] * 2.0 - 1.0
    n = np.linalg.norm(xyz, axis=-1, keepdims=True)
    n = np.maximum(n, 1e-6)
    xyz = xyz / n
    enc = xyz * 0.5 + 0.5
    out = np.zeros(a.shape[:2] + (4,), dtype=np.float32)
    out[..., 0] = enc[..., 0]
    out[..., 1] = enc[..., 1]
    out[..., 2] = 0.0
    out[..., 3] = enc[..., 2]
    if a.shape[-1] >= 4:
        out[..., 2] = 0.0
        # keep silhouette in a side channel? Torque writes Z to alpha only.
        # Transparent pixels: leave as 0.
        dead = a[..., 3] <= (ALPHA_REF / 255.0)
        out[dead] = 0.0
    return out


def _u8(arr: np.ndarray) -> np.ndarray:
    a = np.asarray(arr, dtype=np.float32)
    if a.max() <= 1.5:
        a = a * 255.0
    return np.clip(np.rint(a), 0, 255).astype(np.uint8)


def downsample_cells(atlas: np.ndarray, dim: int) -> tuple[np.ndarray, int]:
    """Box-filter each cell independently to dim/2."""
    h, w = atlas.shape[:2]
    new_dim = max(1, dim // 2)
    cols = w // dim
    rows = h // dim
    out = np.zeros((rows * new_dim, cols * new_dim, atlas.shape[2]), dtype=atlas.dtype)
    src = atlas.astype(np.float32)
    for r in range(rows):
        for c in range(cols):
            tile = src[r * dim : (r + 1) * dim, c * dim : (c + 1) * dim]
            if tile.size == 0:
                continue
            # 2x2 box
            a = tile[0::2, 0::2]
            b = tile[1::2, 0::2] if tile.shape[0] > 1 else a
            c2 = tile[0::2, 1::2] if tile.shape[1] > 1 else a
            d = tile[1::2, 1::2] if tile.shape[0] > 1 and tile.shape[1] > 1 else a
            m = 0.25 * (a + b + c2 + d)
            out[r * new_dim : (r + 1) * new_dim, c * new_dim : (c + 1) * new_dim] = m
    return out, new_dim


def mip_chain(atlas: np.ndarray, dim: int) -> list[np.ndarray]:
    """Cell-aware mips down to 1 px per cell, then one whole-image step to 2 px.

    A 1024 atlas then has 10 levels (1024 … 2), same count as the stock
    douglasfir imposter DDS. The chain stops at 2 px; a 1 px tail is not written.
    """
    levels = [atlas]
    cur, d = atlas, dim
    while d > 1 and min(cur.shape[0], cur.shape[1]) > 1:
        cur, d = downsample_cells(cur, d)
        levels.append(cur)
        if d <= 1:
            break
    h, w = cur.shape[:2]
    if min(h, w) > 2 and h % 2 == 0 and w % 2 == 0:
        half = 0.25 * (
            cur[0::2, 0::2] + cur[1::2, 0::2] + cur[0::2, 1::2] + cur[1::2, 1::2]
        )
        levels.append(half.astype(cur.dtype, copy=False))
    return levels


# --------------------------------------------------------------------------- DDS / DXT
def _rgb565(r: int, g: int, b: int) -> int:
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def _unpack565(c: int) -> tuple[int, int, int]:
    r = ((c >> 11) & 31) * 255 // 31
    g = ((c >> 5) & 63) * 255 // 63
    b = (c & 31) * 255 // 31
    return r, g, b


def _dxt_color_block(pixels: np.ndarray) -> bytes:
    """pixels: 16x3 uint8. Always 4-color mode (c0 > c1)."""
    lum = pixels[:, 0].astype(np.int32) * 2 + pixels[:, 1].astype(np.int32) * 3 + pixels[:, 2]
    i0 = int(np.argmax(lum))
    i1 = int(np.argmin(lum))
    c0 = _rgb565(int(pixels[i0, 0]), int(pixels[i0, 1]), int(pixels[i0, 2]))
    c1 = _rgb565(int(pixels[i1, 0]), int(pixels[i1, 1]), int(pixels[i1, 2]))
    if c0 == c1:
        c0 = min(0xFFFF, c0 + 1)
    if c0 < c1:
        c0, c1 = c1, c0
        i0, i1 = i1, i0
    p0 = np.array(_unpack565(c0), dtype=np.int16)
    p1 = np.array(_unpack565(c1), dtype=np.int16)
    palette = [
        p0,
        p1,
        (2 * p0 + p1) // 3,
        (p0 + 2 * p1) // 3,
    ]
    bits = 0
    for i in range(16):
        px = pixels[i].astype(np.int16)
        d = [int(np.sum((px - pal) * (px - pal))) for pal in palette]
        bits |= int(np.argmin(d)) << (2 * i)
    return struct.pack("<HHI", c0, c1, bits)


def _dxt3_alpha_block(alphas: np.ndarray) -> bytes:
    """alphas: 16 uint8. Explicit 4-bit."""
    out = bytearray(8)
    for i in range(8):
        a0 = int(alphas[2 * i]) >> 4
        a1 = int(alphas[2 * i + 1]) >> 4
        out[i] = a0 | (a1 << 4)
    return bytes(out)


def _dxt5_alpha_block(alphas: np.ndarray) -> bytes:
    a_max = int(alphas.max())
    a_min = int(alphas.min())
    if a_max == a_min:
        a_max = min(255, a_max + 1)
    # 8-value mode needs a0 > a1
    a0, a1 = a_max, a_min
    if a0 <= a1:
        a0 = min(255, a1 + 1)
    table = [a0, a1]
    for i in range(1, 7):
        table.append(((7 - i) * a0 + i * a1) // 7)
    bits = 0
    for i in range(16):
        a = int(alphas[i])
        best = min(range(8), key=lambda k: abs(table[k] - a))
        bits |= best << (3 * i)
    return bytes([a0, a1]) + int(bits).to_bytes(6, "little")


def _encode_dxt_image(rgba: np.ndarray, fmt: str) -> bytes:
    h, w = rgba.shape[:2]
    bw, bh = (w + 3) // 4, (h + 3) // 4
    pad = np.zeros((bh * 4, bw * 4, 4), dtype=np.uint8)
    pad[:h, :w] = rgba
    chunks = []
    for by in range(bh):
        for bx in range(bw):
            block = pad[by * 4 : by * 4 + 4, bx * 4 : bx * 4 + 4]
            pix = block.reshape(16, 4)
            if fmt == "DXT3":
                chunks.append(_dxt3_alpha_block(pix[:, 3]))
            else:
                chunks.append(_dxt5_alpha_block(pix[:, 3]))
            chunks.append(_dxt_color_block(pix[:, :3]))
    return b"".join(chunks)


def _dds_header(width: int, height: int, fourcc: bytes, mip_count: int, pitch: int) -> bytes:
    flags = 0x1 | 0x2 | 0x4 | 0x1000  # caps, height, width, pixelformat
    flags |= 0x20000  # mipmap count
    flags |= 0x80000  # linear size
    caps1 = 0x1000 | 0x400000 | 0x8  # texture | mipmap | complex
    pf_flags = 0x4  # FOURCC
    header = struct.pack(
        "<I"  # size
        "I"  # flags
        "I"  # height
        "I"  # width
        "I"  # pitch
        "I"  # depth
        "I"  # mipmap count
        "11I"  # reserved
        "I"  # pf size
        "I"  # pf flags
        "4s"  # fourcc
        "5I"  # rgb bitmasks + pf reserved
        "I"  # caps
        "4I",  # caps2-4 + reserved2
        124,
        flags,
        height,
        width,
        pitch,
        0,
        mip_count,
        *([0] * 11),
        32,
        pf_flags,
        fourcc,
        32,  # rgbBitCount, set beside FOURCC like the stock tree DDS
        0x000000FF,
        0x00FF0000,
        0x0000FF00,
        0xFF000000,
        caps1,
        0,
        0,
        0,
        0,
    )
    return b"DDS " + header


def write_dds_dxt(path: Path, atlas_01: np.ndarray, dim: int, fourcc: bytes) -> None:
    levels = mip_chain(atlas_01, dim)
    mip_bytes = []
    for lvl in levels:
        rgba = _u8(lvl)
        fmt = "DXT3" if fourcc == b"DXT3" else "DXT5"
        mip_bytes.append(_encode_dxt_image(rgba, fmt))
    h, w = _u8(levels[0]).shape[:2]
    payload = b"".join(mip_bytes)
    header = _dds_header(w, h, fourcc, len(levels), len(mip_bytes[0]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + payload)


def write_asset_json(
    path: Path,
    *,
    equator_steps: int,
    polar_steps: int,
    include_poles: bool,
    polar_angle: float,
    dimension: int,
    detail_level: int,
    size: int,
) -> None:
    data = {
        "imposters": [
            {
                "detailLevel": int(detail_level),
                "dimension": int(dimension),
                "equatorSteps": int(equator_steps),
                "includePoles": bool(include_poles),
                "polarAngle": float(polar_angle),
                "polarSteps": int(polar_steps),
                "size": int(size),
            }
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def pack_and_write(
    cells_rgba: list[np.ndarray],
    cells_nrm: list[np.ndarray],
    out_dir: Path,
    dae_name: str,
    spec: dict[str, Any],
    *,
    bleed_iter: int = 8,
) -> dict[str, Any]:
    """Pack cells, write PNG preview + DDS sidecars + asset.json next to a DAE name."""
    dim = int(spec["dimension"])
    n = len(cells_rgba)
    expect = imposter_count(spec["equator_steps"], spec["polar_steps"], spec["include_poles"])
    if n != expect:
        raise ValueError(f"{n} albedo cells, expected {expect}")
    if len(cells_nrm) != n:
        raise ValueError("normal cell count mismatch")
    wh = atlas_size(n, dim)
    bled = [alpha_bleed(c, bleed_iter) for c in cells_rgba]
    albedo = pack_cells(bled, dim, wh)
    nrm_enc = [world_normal_to_dxt5nm(c) for c in cells_nrm]
    # copy albedo alpha onto encoded normals so empty texels stay 0
    nrm_atlas = pack_cells(nrm_enc, dim, wh)
    dead = albedo[..., 3] <= (ALPHA_REF / 255.0)
    nrm_atlas[dead] = 0.0

    out_dir.mkdir(parents=True, exist_ok=True)
    from PIL import Image

    alb_u8 = _u8(albedo)
    Image.fromarray(alb_u8, mode="RGBA").save(out_dir / f"{dae_name}.imposter.png")
    nrm_vis = _u8(nrm_atlas)
    Image.fromarray(nrm_vis, mode="RGBA").save(out_dir / f"{dae_name}.imposter_normals.png")

    dds_color = out_dir / f"{dae_name}.imposter.dds"
    dds_nrm = out_dir / f"{dae_name}.imposter_normals.dds"
    write_dds_dxt(dds_color, albedo, dim, b"DXT3")
    write_dds_dxt(dds_nrm, nrm_atlas, dim, b"DXT5")
    write_asset_json(
        out_dir / f"{dae_name}.asset.json",
        equator_steps=spec["equator_steps"],
        polar_steps=spec["polar_steps"],
        include_poles=spec["include_poles"],
        polar_angle=spec["polar_angle"],
        dimension=dim,
        detail_level=spec["detail_level"],
        size=spec["size"],
    )
    return {
        "cells": n,
        "atlas": [wh[0], wh[1]],
        "dimension": dim,
        "dds_color": str(dds_color),
        "dds_normals": str(dds_nrm),
    }


# --------------------------------------------------------------------------- cheap DAE (crossplanes + trunk)
def _quad(
    corners: list[tuple[float, float, float]],
) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]], list[tuple[float, float]]]:
    """Two tris, right-hand; corners CCW as seen from the visible side."""
    a, b, c, d = corners
    verts = [a, b, c, d]
    # (b-a) x (c-a) should point toward the viewer of this card.
    faces = [(0, 1, 2), (0, 2, 3)]
    uvs = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    return verts, faces, uvs


def cheap_lod_geometry(height_m: float, radius_m: float) -> dict[str, Any]:
    """Trunk cylinder (open) + two vertical crossplanes. Crown cards sit on Z."""
    h = float(height_m)
    r = float(radius_m)
    trunk_r = max(0.12, r * 0.06)
    trunk_h = h * 0.55
    # Crossplanes: full height, width 2r
    cards = []
    for yaw in (0.0, math.pi / 2.0):
        c, s = math.cos(yaw), math.sin(yaw)
        # Plane in the vertical plane rotated by yaw; visible sides +XY
        # Corners bottom-left, bottom-right, top-right, top-left in plane local XZ
        hx, hy = c * r, s * r
        corners = [
            (-hx, -hy, 0.0),
            (hx, hy, 0.0),
            (hx, hy, h),
            (-hx, -hy, h),
        ]
        # Winding: from the +normal ( -s, c, 0 ) side, CCW
        # (b-a)=(2hx, 2hy, 0), (d-a)=(0,0,h); wait we use a,b,c
        # (b-a) x (c-a) = (2hx, 2hy, 0) x (2hx, 2hy, h)
        # For yaw=0: a=(-r,0,0) b=(r,0,0) c=(r,0,h) -> (2r,0,0)x(2r,0,h)=(0, -4r h, 0)
        # That's -Y. Flip if we want +Y as front: swap b/d order...
        # User walks mostly around; doubleSided on the material. Keep this winding
        # and set doubleSided.
        v, f, uv = _quad(corners)
        cards.append((v, f, uv))

    # Trunk: 8-sided prism, outward faces
    n_seg = 8
    tverts: list[tuple[float, float, float]] = []
    tfaces: list[tuple[int, int, int]] = []
    tuv: list[tuple[float, float]] = []
    for i in range(n_seg):
        a0 = 2 * math.pi * i / n_seg
        a1 = 2 * math.pi * (i + 1) / n_seg
        x0, y0 = trunk_r * math.cos(a0), trunk_r * math.sin(a0)
        x1, y1 = trunk_r * math.cos(a1), trunk_r * math.sin(a1)
        base = len(tverts)
        tverts.extend(
            [(x0, y0, 0.0), (x1, y1, 0.0), (x1, y1, trunk_h), (x0, y0, trunk_h)]
        )
        # outward: (b-a) x (c-a) · radial > 0
        tfaces.append((base, base + 1, base + 2))
        tfaces.append((base, base + 2, base + 3))
        u0, u1 = i / n_seg, (i + 1) / n_seg
        tuv.extend([(u0, 0.0), (u1, 0.0), (u1, 1.0), (u0, 1.0)])
    return {
        "cards": cards,
        "trunk_verts": tverts,
        "trunk_faces": tfaces,
        "trunk_uvs": tuv,
        "height": h,
        "radius": r,
    }


def write_cheap_dae(
    path: Path,
    *,
    stem: str,
    height_m: float,
    radius_m: float,
    card_png: str,
    bark_png: str | None,
    card_mat: str,
    bark_mat: str,
) -> None:
    geo = cheap_lod_geometry(height_m, radius_m)
    lod = f"{stem}_a20"
    # Merge card verts
    verts: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    card_faces: list[tuple[int, int, int]] = []
    for v, f, uv in geo["cards"]:
        off = len(verts)
        verts.extend(v)
        uvs.extend(uv)
        card_faces.extend((a + off, b + off, c + off) for a, b, c in f)
    bark_off = len(verts)
    verts.extend(geo["trunk_verts"])
    uvs.extend(geo["trunk_uvs"])
    bark_faces = [(a + bark_off, b + bark_off, c + bark_off) for a, b, c in geo["trunk_faces"]]

    def face_block(name: str, faces: Iterable[tuple[int, int, int]]) -> str:
        fl = list(faces)
        p = " ".join(f"{a} {a} {b} {b} {c} {c}" for a, b, c in fl)
        return (
            f'        <triangles material="{name}" count="{len(fl)}">\n'
            f'          <input semantic="VERTEX" source="#{lod}-mesh-vertices" offset="0"/>\n'
            f'          <input semantic="TEXCOORD" source="#{lod}-mesh-map-0" offset="1" set="0"/>\n'
            f"          <p>{p}</p>\n"
            f"        </triangles>"
        )

    pos_vals = " ".join(f"{x:.4f} {y:.4f} {z:.4f}" for x, y, z in verts)
    uv_vals = " ".join(f"{u:.5f} {v:.5f}" for u, v in uvs)
    n_vert = len(verts)
    cm = escape(card_mat)
    bm = escape(bark_mat)
    bark_img = ""
    bark_diff = "<color>0.22 0.14 0.08 1</color>"
    if bark_png:
        bark_img = f"""    <image id="{bm}-image" name="{bm}-image">
      <init_from>{escape(bark_png)}</init_from>
    </image>"""
        bark_diff = f'<texture texture="{bm}-sampler" texcoord="UVSET0"/>'
        bark_tex_params = f"""    <newparam sid="{bm}-surface"><surface type="2D"><init_from>{bm}-image</init_from></surface></newparam>
    <newparam sid="{bm}-sampler"><sampler2D><source>{bm}-surface</source></sampler2D></newparam>"""
    else:
        bark_tex_params = ""
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor><authoring_tool>beamng_autoroad tree_imposter_lib</authoring_tool></contributor>
    <unit name="meter" meter="1"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_images>
    <image id="{cm}-image" name="{cm}-image">
      <init_from>{escape(card_png)}</init_from>
    </image>
{bark_img}
  </library_images>
  <library_effects>
    <effect id="{cm}-effect">
      <profile_COMMON>
        <newparam sid="{cm}-surface"><surface type="2D"><init_from>{cm}-image</init_from></surface></newparam>
        <newparam sid="{cm}-sampler"><sampler2D><source>{cm}-surface</source></sampler2D></newparam>
        <technique sid="common">
          <lambert>
            <diffuse><texture texture="{cm}-sampler" texcoord="UVSET0"/></diffuse>
          </lambert>
        </technique>
        <extra><technique profile="GOOGLEEARTH"><double_sided>1</double_sided></technique></extra>
      </profile_COMMON>
    </effect>
    <effect id="{bm}-effect">
      <profile_COMMON>
{bark_tex_params}
        <technique sid="common">
          <lambert>
            <diffuse>{bark_diff}</diffuse>
          </lambert>
        </technique>
      </profile_COMMON>
    </effect>
  </library_effects>
  <library_materials>
    <material id="{cm}-material" name="{cm}">
      <instance_effect url="#{cm}-effect"/>
    </material>
    <material id="{bm}-material" name="{bm}">
      <instance_effect url="#{bm}-effect"/>
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
{face_block(cm, card_faces)}
{face_block(bm, bark_faces)}
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
                  <instance_material symbol="{cm}" target="#{cm}-material">
                    <bind_vertex_input semantic="UVSET0" input_semantic="TEXCOORD" input_set="0"/>
                  </instance_material>
                  <instance_material symbol="{bm}" target="#{bm}-material">
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


# Fichte2 capture 2026-10-20: ortho frame is radius * 2.05 about the AABB center
# (tree_imposter_capture.ensure_camera). albedo_00 looks along +X; camera right is -Y.
SPRUCE_CARD_CENTER = (-0.29528141021728516, 0.0920560359954834, 15.926722526550293)
SPRUCE_CARD_RADIUS = 17.97062468991007
SPRUCE_ORTHO_PAD = 2.05
# a1000 is the near mesh and must not match. Alternatives need "-mesh" right after the number.
_LOWER_GEOM = re.compile(
    r"    <geometry id=\"tree_spruce_large_a(?:500|250|80|25)-mesh\"[\s\S]*?</geometry>\n"
)
_SPRUCE_CARD_IMAGE = re.compile(r"    <image id=\"SpruceCard-image\"[\s\S]*?</image>\n")
_SPRUCE_CARD_EFFECT = re.compile(r"    <effect id=\"SpruceCard-effect\">[\s\S]*?</effect>\n")
_SPRUCE_CARD_MAT = re.compile(r"    <material id=\"SpruceCard-material\"[\s\S]*?</material>\n")
_SPRUCE_MID_IMAGE = re.compile(r"    <image id=\"SpruceMid-image\"[\s\S]*?</image>\n")
_SPRUCE_MID_EFFECT = re.compile(r"    <effect id=\"SpruceMid-effect\">[\s\S]*?</effect>\n")
_SPRUCE_MID_MAT = re.compile(r"    <material id=\"SpruceMid-material\"[\s\S]*?</material>\n")
# Every fourth equator photo: 4 directions, 90 degrees apart.
MID_VIEW_INDEXES = (0, 4, 8, 12)
MID_CELL = (256, 512)
MID_MARGIN = 8
_VISUAL_SCENE = re.compile(r"  <library_visual_scenes>[\s\S]*?</library_visual_scenes>")
_NEAR_NODE = re.compile(
    r"<node id=\"tree_spruce_large_a1000\" name=\"tree_spruce_large_a1000\" type=\"NODE\">[\s\S]*?</node>"
)


def spruce_view_basis(index: int, equator_steps: int = 16) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Camera offset and camera-right for one equator capture. View 0 sits on -X."""
    rot_z = -math.pi / 2 + (index % equator_steps) * (2 * math.pi / equator_steps)
    cz, sz = math.cos(rot_z), math.sin(rot_z)
    direction = (sz, -cz, 0.0)
    right = (cz, sz, 0.0)
    return direction, right


def billboard_slot(look: tuple[float, float, float], equator_steps: int = 16) -> int:
    """Atlas cell the imposter shader reads when the camera sits along look.

    Same half-step as imposter.hlsl: the cell switches before the capture angle,
    so the photo is centered on its own camera. Object +X is yaw zero.
    """
    step = (2.0 * math.pi) / float(max(1, int(equator_steps)))
    half = (step / 2.0) - 0.0001
    look_az = math.atan2(float(look[1]), float(look[0]))
    rot_z = look_az + half
    if rot_z < 0.0:
        rot_z += 2.0 * math.pi
    if rot_z > 2.0 * math.pi:
        rot_z -= 2.0 * math.pi
    return int(rot_z / step)


def billboard_basis(look: tuple[float, float, float]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """T, B, N the pixel shader actually uses. N points from the tree at the camera.

    imposter.hlsl writes bitangent = up, then the vertex shader throws that row
    away and rebuilds B = cross(N, T) * -1.
    """
    n = np.asarray(look, dtype=np.float64)
    n = n / max(float(np.linalg.norm(n)), 1e-8)
    up = np.array([0.0, 0.0, 1.0])
    cam_right = np.cross(n, up)
    cam_right = cam_right / max(float(np.linalg.norm(cam_right)), 1e-8)
    tangent = -cam_right
    bitangent = np.cross(n, cam_right)
    return tangent, bitangent, n


def encode_billboard_normal(world_rgba: np.ndarray, view_index: int) -> np.ndarray:
    """World-space capture (RGB 0..1) to DXT5nm for this view.

    The shader reads alpha as X and green as Y, then flips Y and rebuilds Z.
    A needle facing the camera lands on (green 0.5, alpha 0.5).
    """
    src = np.asarray(world_rgba, dtype=np.float32)
    if src.max() > 1.5:
        src = src / 255.0
    xyz = src[..., :3] * 2.0 - 1.0
    direction, _right = spruce_view_basis(view_index)
    tangent, bitangent, normal = billboard_basis(direction)
    tx = xyz[..., 0] * tangent[0] + xyz[..., 1] * tangent[1] + xyz[..., 2] * tangent[2]
    ty = xyz[..., 0] * bitangent[0] + xyz[..., 1] * bitangent[1] + xyz[..., 2] * bitangent[2]
    tz = xyz[..., 0] * normal[0] + xyz[..., 1] * normal[1] + xyz[..., 2] * normal[2]
    tz = np.maximum(tz, 0.0)
    length = np.maximum(np.sqrt(tx * tx + ty * ty + tz * tz), 1e-6)
    tx, ty = tx / length, ty / length
    out = np.zeros(xyz.shape[:2] + (4,), dtype=np.float32)
    out[..., 1] = (-ty) * 0.5 + 0.5
    out[..., 3] = tx * 0.5 + 0.5
    return out


def _bleed_dxt5nm(encoded: np.ndarray, coverage: np.ndarray, iterations: int = 4) -> np.ndarray:
    """Bleed tangent X (alpha) and Y (green) into empty texels."""
    packed = np.zeros_like(encoded)
    packed[..., 0] = encoded[..., 3]
    packed[..., 1] = encoded[..., 1]
    packed[..., 3] = coverage
    bled = alpha_bleed(packed, iterations)
    out = encoded.copy()
    out[..., 3] = bled[..., 0]
    out[..., 1] = bled[..., 1]
    return out


def pack_camera_billboard(
    cells_rgba: list[np.ndarray],
    cells_nrm: list[np.ndarray],
    out_dir: Path,
    dae_name: str,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Pack equator photos in the order imposter.hlsl indexes, plus tangent normals."""
    equator = int(spec["equator_steps"])
    n = len(cells_rgba)
    if n != equator or int(spec["polar_steps"]) != 0 or spec["include_poles"]:
        raise ValueError("camera billboard pack expects one equator ring and no poles")
    slots = [billboard_slot(spruce_view_basis(i, equator)[0], equator) for i in range(n)]
    if sorted(slots) != list(range(n)):
        raise ValueError(f"billboard slots are not a permutation: {slots}")
    dim = int(spec["dimension"])
    ordered_alb: list[np.ndarray | None] = [None] * n
    ordered_nrm: list[np.ndarray | None] = [None] * n
    for i, slot in enumerate(slots):
        albedo = alpha_bleed(cells_rgba[i], 8)
        ordered_alb[slot] = albedo
        encoded = encode_billboard_normal(cells_nrm[i], i)
        encoded = _bleed_dxt5nm(encoded, albedo[..., 3], 4)
        ordered_nrm[slot] = encoded
    wh = atlas_size(n, dim)
    albedo_atlas = pack_cells(ordered_alb, dim, wh)  # type: ignore[arg-type]
    normal_atlas = pack_cells(ordered_nrm, dim, wh)  # type: ignore[arg-type]
    out_dir.mkdir(parents=True, exist_ok=True)
    from PIL import Image

    Image.fromarray(_u8(albedo_atlas), mode="RGBA").save(out_dir / f"{dae_name}.imposter.png")
    Image.fromarray(_u8(normal_atlas), mode="RGBA").save(out_dir / f"{dae_name}.imposter_normals.png")
    write_dds_dxt(out_dir / f"{dae_name}.imposter.dds", albedo_atlas, dim, b"DXT3")
    write_dds_dxt(out_dir / f"{dae_name}.imposter_normals.dds", normal_atlas, dim, b"DXT5")
    write_asset_json(
        out_dir / f"{dae_name}.asset.json",
        equator_steps=equator,
        polar_steps=0,
        include_poles=False,
        polar_angle=float(spec["polar_angle"]),
        dimension=dim,
        detail_level=int(spec["detail_level"]),
        size=int(spec["size"]),
    )
    return {"cells": n, "slots": slots, "atlas": [wh[0], wh[1]], "dimension": dim}


_CARD_NODE = re.compile(
    r"\n          <node id=\"tree_spruce_large_a(?:80|25)\" name=\"tree_spruce_large_a(?:80|25)\" type=\"NODE\">[\s\S]*?</node>"
)


def attach_camera_billboard(text: str) -> str:
    """Drop the fixed cards and add the empty engine billboard node."""
    text = _LOWER_GEOM.sub("", text)
    text = _CARD_NODE.sub("", text)
    if "bb_autobillboard25" not in text:
        needle = '      <node id="base00" name="base00" type="NODE">\n'
        insert = needle + '        <node id="bb_autobillboard25" name="bb_autobillboard25" type="NODE"/>\n'
        if needle not in text:
            raise ValueError("base00 missing")
        text = text.replace(needle, insert, 1)
    if "tree_spruce_large_a1000" not in text:
        raise ValueError("near mesh missing")
    if 'id="tree_spruce_large_a80"' in text or 'id="tree_spruce_large_a25"' in text:
        raise ValueError("crossed cards still in DAE")
    return text


def encode_tangent_normal(world_n: np.ndarray, view_index: int) -> np.ndarray:
    """World normals (-1..1) to a DirectX tangent-space map (0..1) for this card.

    The card is one plane. Needles keep their angle, including those that point
    away from the camera, so a side sun does not light the whole crown.
    """
    n = np.asarray(world_n, dtype=np.float32)
    direction, right = spruce_view_basis(view_index)
    rx, ry, rz = right
    dx, dy, dz = direction
    tx = n[..., 0] * rx + n[..., 1] * ry + n[..., 2] * rz
    ty = n[..., 2]
    tz = n[..., 0] * dx + n[..., 1] * dy + n[..., 2] * dz
    length = np.sqrt(tx * tx + ty * ty + tz * tz)
    length = np.maximum(length, 1e-6)
    tx, ty, tz = tx / length, ty / length, tz / length
    return np.stack((tx * 0.5 + 0.5, -ty * 0.5 + 0.5, tz * 0.5 + 0.5), axis=-1)


def tangent_normal_image(normal_path: Path, uv_rect: tuple[float, float, float, float], view_index: int):
    """Crop a world-space capture and return an RGB DirectX normal image."""
    from PIL import Image

    crop = _crop_rgba(Image.open(normal_path).convert("RGBA"), uv_rect)
    arr = np.asarray(crop).astype(np.float32) / 255.0
    rgb = np.clip(encode_tangent_normal(arr[..., :3] * 2.0 - 1.0, view_index), 0.0, 1.0)
    empty = arr[..., 3] < (8.0 / 255.0)
    rgb[empty] = (0.5, 0.5, 1.0)
    return Image.fromarray((rgb * 255.0).astype(np.uint8), mode="RGB")


def spruce_view_quad(
    index: int,
    uv_rect: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
    center: tuple[float, float, float] = SPRUCE_CARD_CENTER,
    radius: float = SPRUCE_CARD_RADIUS,
    ortho_pad: float = SPRUCE_ORTHO_PAD,
) -> list[tuple[float, float, float]]:
    """One card facing this capture. Corners are bottom-left, bottom-right, top-right, top-left."""
    u0, v0, u1, v1 = uv_rect
    span = float(radius) * float(ortho_pad)
    cx, cy, cz = center
    _direction, (rx, ry, _rz) = spruce_view_basis(index)

    def at(u: float, v: float) -> tuple[float, float, float]:
        return (
            cx + (u - 0.5) * span * rx,
            cy + (u - 0.5) * span * ry,
            cz + (v - 0.5) * span,
        )

    return [at(u0, v0), at(u1, v0), at(u1, v1), at(u0, v1)]


def spruce_far_quads(
    center: tuple[float, float, float] = SPRUCE_CARD_CENTER,
    radius: float = SPRUCE_CARD_RADIUS,
    uv_rect: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0),
    angles_deg: tuple[float, ...] = (0.0, 60.0, 120.0),
    ortho_pad: float = SPRUCE_ORTHO_PAD,
) -> list[list[tuple[float, float, float]]]:
    """Three crossed cards. uv_rect is (u0, v0, u1, v1) in the ortho frame, v up.

    Corner order is bottom-left, bottom-right, top-right, top-left. The first
    card faces the albedo_00 camera (normal toward -X).
    """
    u0, v0, u1, v1 = uv_rect
    span = float(radius) * float(ortho_pad)
    cx, cy, cz = center
    right0 = (0.0, -1.0, 0.0)
    quads: list[list[tuple[float, float, float]]] = []
    for deg in angles_deg:
        ang = math.radians(deg)
        c, s = math.cos(ang), math.sin(ang)
        rx = right0[0] * c - right0[1] * s
        ry = right0[0] * s + right0[1] * c

        def at(u: float, v: float, rx: float = rx, ry: float = ry) -> tuple[float, float, float]:
            return (
                cx + (u - 0.5) * span * rx,
                cy + (u - 0.5) * span * ry,
                cz + (v - 0.5) * span,
            )

        quads.append([at(u0, v0), at(u1, v0), at(u1, v1), at(u0, v1)])
    return quads


def _quads_geometry_xml(
    lod: str,
    material: str,
    quads: list[list[tuple[float, float, float]]],
    quad_uvs: list[list[tuple[float, float]]] | None = None,
) -> str:
    verts: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    uv_all: list[tuple[float, float]] = []
    full = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    for i, quad in enumerate(quads):
        base = len(verts)
        verts.extend(quad)
        uv_all.extend(quad_uvs[i] if quad_uvs else full)
        faces.append((base, base + 1, base + 2))
        faces.append((base, base + 2, base + 3))
    pos_vals = " ".join(f"{x:.4f} {y:.4f} {z:.4f}" for x, y, z in verts)
    uv_vals = " ".join(f"{u:.5f} {v:.5f}" for u, v in uv_all)
    p = " ".join(f"{a} {a} {b} {b} {c} {c}" for a, b, c in faces)
    n_vert = len(verts)
    return (
        f'    <geometry id="{lod}-mesh" name="{lod}-mesh">\n'
        f"      <mesh>\n"
        f'        <source id="{lod}-mesh-positions">\n'
        f'          <float_array id="{lod}-mesh-positions-array" count="{n_vert * 3}">{pos_vals}</float_array>\n'
        f'          <technique_common><accessor source="#{lod}-mesh-positions-array" count="{n_vert}" stride="3">\n'
        f'            <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>\n'
        f"          </accessor></technique_common>\n        </source>\n"
        f'        <source id="{lod}-mesh-map-0">\n'
        f'          <float_array id="{lod}-mesh-map-0-array" count="{n_vert * 2}">{uv_vals}</float_array>\n'
        f'          <technique_common><accessor source="#{lod}-mesh-map-0-array" count="{n_vert}" stride="2">\n'
        f'            <param name="S" type="float"/><param name="T" type="float"/>\n'
        f"          </accessor></technique_common>\n        </source>\n"
        f'        <vertices id="{lod}-mesh-vertices">'
        f'<input semantic="POSITION" source="#{lod}-mesh-positions"/></vertices>\n'
        f'        <triangles material="{material}" count="{len(faces)}">\n'
        f'          <input semantic="VERTEX" source="#{lod}-mesh-vertices" offset="0"/>\n'
        f'          <input semantic="TEXCOORD" source="#{lod}-mesh-map-0" offset="1" set="0"/>\n'
        f"          <p>{p}</p>\n"
        f"        </triangles>\n"
        f"      </mesh>\n    </geometry>\n"
    )


def _card_geometry_xml(quads: list[list[tuple[float, float, float]]]) -> str:
    return _quads_geometry_xml("tree_spruce_large_a25", "SpruceCard", quads)


def _lod_node_xml(lod: str, material: str) -> str:
    return (
        f'          <node id="{lod}" name="{lod}" type="NODE">\n'
        f'            <instance_geometry url="#{lod}-mesh">\n'
        f"              <bind_material><technique_common>\n"
        f'                  <instance_material symbol="{material}" target="#{material}-material">\n'
        f'                    <bind_vertex_input semantic="UVSET0" input_semantic="TEXCOORD" input_set="0"/>\n'
        f"                  </instance_material>\n"
        f"              </technique_common></bind_material>\n"
        f"            </instance_geometry>\n          </node>"
    )


def _card_node_xml() -> str:
    return _lod_node_xml("tree_spruce_large_a25", "SpruceCard")


def ensure_spruce_mid_material(text: str) -> str:
    """Collada stubs so the eight view cards can bind SpruceMid. No double-sided extra."""
    if "SpruceMid-material" in text:
        return text
    image = '    <image id="SpruceMid-image" name="SpruceMid-image"><init_from>spruce_mid.color.png</init_from></image>\n'
    effect = (
        '    <effect id="SpruceMid-effect">\n'
        "      <profile_COMMON>\n"
        '        <newparam sid="SpruceMid-surface"><surface type="2D"><init_from>SpruceMid-image</init_from></surface></newparam>\n'
        '        <newparam sid="SpruceMid-sampler"><sampler2D><source>SpruceMid-surface</source></sampler2D></newparam>\n'
        '        <technique sid="common"><lambert><diffuse><texture texture="SpruceMid-sampler" texcoord="UVSET0"/></diffuse></lambert></technique>\n'
        "      </profile_COMMON>\n"
        "    </effect>\n"
    )
    material = '    <material id="SpruceMid-material" name="SpruceMid"><instance_effect url="#SpruceMid-effect"/></material>\n'
    if "  </library_images>\n" not in text:
        raise ValueError("DAE has no library_images")
    text = text.replace("  </library_images>\n", image + "  </library_images>\n", 1)
    text = text.replace("  </library_effects>\n", effect + "  </library_effects>\n", 1)
    text = text.replace("  </library_materials>\n", material + "  </library_materials>\n", 1)
    return text


def attach_mid_cards(
    text: str,
    quads: list[list[tuple[float, float, float]]],
    uvs: list[list[tuple[float, float]]],
) -> str:
    """Put the eight view cards back under the near mesh. The billboard node stays."""
    if 'id="tree_spruce_large_a80"' in text:
        raise ValueError("mid cards already in DAE")
    if "bb_autobillboard25" not in text:
        raise ValueError("billboard node missing")
    text = ensure_spruce_mid_material(text)
    geo = _quads_geometry_xml("tree_spruce_large_a80", "SpruceMid", quads, uvs)
    if "  </library_geometries>\n" not in text:
        raise ValueError("DAE has no library_geometries")
    text = text.replace("  </library_geometries>\n", geo + "  </library_geometries>\n", 1)
    near = _NEAR_NODE.search(text)
    if near is None:
        raise ValueError("near LOD node tree_spruce_large_a1000 missing")
    node = "\n" + _lod_node_xml("tree_spruce_large_a80", "SpruceMid")
    text = text[: near.end()] + node + text[near.end() :]
    if 'id="tree_spruce_large_a25"' in text:
        raise ValueError("far cards would share pixel size 25 with the billboard")
    return text


def patch_spruce_dae_xml(
    text: str,
    quads: list[list[tuple[float, float, float]]],
    mid_quads: list[list[tuple[float, float, float]]] | None = None,
    mid_uvs: list[list[tuple[float, float]]] | None = None,
) -> str:
    """Near mesh stays _a1000. Optional _a80 view cards, then the _a25 stamp. No atlas billboard."""
    text = _LOWER_GEOM.sub("", text)
    text = _SPRUCE_CARD_IMAGE.sub("", text)
    text = _SPRUCE_CARD_EFFECT.sub("", text)
    text = _SPRUCE_CARD_MAT.sub("", text)
    text = _SPRUCE_MID_IMAGE.sub("", text)
    text = _SPRUCE_MID_EFFECT.sub("", text)
    text = _SPRUCE_MID_MAT.sub("", text)
    image = '    <image id="SpruceCard-image" name="SpruceCard-image"><init_from>spruce_card.color.png</init_from></image>\n'
    effect = (
        '    <effect id="SpruceCard-effect">\n'
        "      <profile_COMMON>\n"
        '        <newparam sid="SpruceCard-surface"><surface type="2D"><init_from>SpruceCard-image</init_from></surface></newparam>\n'
        '        <newparam sid="SpruceCard-sampler"><sampler2D><source>SpruceCard-surface</source></sampler2D></newparam>\n'
        '        <technique sid="common"><lambert><diffuse><texture texture="SpruceCard-sampler" texcoord="UVSET0"/></diffuse></lambert></technique>\n'
        '        <extra><technique profile="GOOGLEEARTH"><double_sided>1</double_sided></technique></extra>\n'
        "      </profile_COMMON>\n"
        "    </effect>\n"
    )
    material = '    <material id="SpruceCard-material" name="SpruceCard"><instance_effect url="#SpruceCard-effect"/></material>\n'
    if mid_quads:
        if not mid_uvs or len(mid_uvs) != len(mid_quads):
            raise ValueError("mid UVs do not match mid quads")
        image += '    <image id="SpruceMid-image" name="SpruceMid-image"><init_from>spruce_mid.color.png</init_from></image>\n'
        effect += (
            '    <effect id="SpruceMid-effect">\n'
            "      <profile_COMMON>\n"
            '        <newparam sid="SpruceMid-surface"><surface type="2D"><init_from>SpruceMid-image</init_from></surface></newparam>\n'
            '        <newparam sid="SpruceMid-sampler"><sampler2D><source>SpruceMid-surface</source></sampler2D></newparam>\n'
            '        <technique sid="common"><lambert><diffuse><texture texture="SpruceMid-sampler" texcoord="UVSET0"/></diffuse></lambert></technique>\n'
            "      </profile_COMMON>\n"
            "    </effect>\n"
        )
        material += '    <material id="SpruceMid-material" name="SpruceMid"><instance_effect url="#SpruceMid-effect"/></material>\n'
    if "  </library_images>\n" not in text:
        raise ValueError("DAE has no library_images")
    text = text.replace("  </library_images>\n", image + "  </library_images>\n", 1)
    text = text.replace("  </library_effects>\n", effect + "  </library_effects>\n", 1)
    text = text.replace("  </library_materials>\n", material + "  </library_materials>\n", 1)
    geo = ""
    if mid_quads:
        geo += _quads_geometry_xml("tree_spruce_large_a80", "SpruceMid", mid_quads, mid_uvs)
    geo += _card_geometry_xml(quads)
    if "  </library_geometries>\n" not in text:
        raise ValueError("DAE has no library_geometries")
    text = text.replace("  </library_geometries>\n", geo + "  </library_geometries>\n", 1)
    near = _NEAR_NODE.search(text)
    if near is None:
        raise ValueError("near LOD node tree_spruce_large_a1000 missing")
    mid_node = _lod_node_xml("tree_spruce_large_a80", "SpruceMid") + "\n" if mid_quads else ""
    scene = (
        "  <library_visual_scenes>\n"
        '    <visual_scene id="Scene" name="Scene">\n'
        '      <node id="base00" name="base00" type="NODE">\n'
        '        <node id="start01" name="start01" type="NODE">\n'
        f"          {near.group(0)}\n"
        f"{mid_node}"
        f"{_card_node_xml()}\n"
        "        </node>\n      </node>\n    </visual_scene>\n"
        "  </library_visual_scenes>"
    )
    text, n = _VISUAL_SCENE.subn(scene, text, count=1)
    if n != 1:
        raise ValueError("visual scene not replaced")
    if (
        "bb_autobillboard" in text
        or "tree_spruce_large_a500" in text
        or "tree_spruce_large_a250-mesh" in text
        or 'id="tree_spruce_large_a250"' in text
    ):
        raise ValueError("billboard or old twig LOD still in DAE")
    if mid_quads:
        mid_effect = _SPRUCE_MID_EFFECT.search(text)
        if mid_effect is None or "double_sided" in mid_effect.group(0):
            raise ValueError("SpruceMid card must stay single-sided")
        if 'id="tree_spruce_large_a80"' not in text or "tree_spruce_large_a1000-mesh" not in text:
            raise ValueError("mid or near mesh missing after patch")
    return text


def crop_card_uv(image_path: Path, alpha_min: int = 8, pad_px: int = 8) -> tuple[float, float, float, float]:
    """Opaque bounds of a capture cell as (u0, v0, u1, v1), v up. Full frame if empty."""
    from PIL import Image

    im = Image.open(image_path).convert("RGBA")
    w, h = im.size
    alpha = im.getchannel("A")
    # Histogram is cheap; bbox needs a threshold, so walk the mask.
    mask = alpha.point(lambda p: 255 if p > alpha_min else 0)
    box = mask.getbbox()
    if box is None:
        return (0.0, 0.0, 1.0, 1.0)
    left, top, right, bottom = box
    left = max(0, left - pad_px)
    top = max(0, top - pad_px)
    right = min(w, right + pad_px)
    bottom = min(h, bottom + pad_px)
    u0 = left / w
    u1 = right / w
    v1 = 1.0 - (top / h)
    v0 = 1.0 - (bottom / h)
    return (u0, v0, u1, v1)


def _crop_rgba(im, uv_rect: tuple[float, float, float, float]):
    w, h = im.size
    u0, v0, u1, v1 = uv_rect
    left = int(round(u0 * w))
    right = int(round(u1 * w))
    top = int(round((1.0 - v1) * h))
    bottom = int(round((1.0 - v0) * h))
    left = max(0, min(left, w - 1))
    top = max(0, min(top, h - 1))
    right = max(left + 1, min(right, w))
    bottom = max(top + 1, min(bottom, h))
    return im.crop((left, top, right, bottom))


def write_cropped_card(src: Path, dst: Path, uv_rect: tuple[float, float, float, float]) -> None:
    """Crop src to uv_rect (v up) and write dst. uv_rect must match crop_card_uv output."""
    from PIL import Image

    cropped = _crop_rgba(Image.open(src).convert("RGBA"), uv_rect)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(dst)
    # PBR alpha-test reads the opacity map's color, not the PNG alpha.
    # The crown is darker than the trunk, so the color photo itself fails the test.
    write_card_opacity(cropped, dst.with_name("spruce_card_o.data.png"))


def write_card_opacity(card_rgba, dst: Path) -> None:
    """RGB mask: each channel is the card's alpha. Matches twig_spruce_o.data.png (RGB, no alpha)."""
    from PIL import Image

    alpha = card_rgba.getchannel("A")
    Image.merge("RGB", (alpha, alpha, alpha)).save(dst)


def crown_occlusion_factor(ao: np.ndarray, covered: np.ndarray, out_lo: float = 0.45) -> np.ndarray:
    """Map an occlusion render onto out_lo..1 so tips stay at the albedo and the interior drops.

    Alpha-card crowns come out of the tracer much darker than a solid surface, so a raw
    multiply would turn the whole tree black. The bright end of this view is kept at 1.
    """
    factor = np.asarray(ao, dtype=np.float32)
    if factor.max() > 1.5:
        factor = factor / 255.0
    if factor.ndim == 3:
        factor = factor[..., 0]
    out = np.ones(factor.shape, dtype=np.float32)
    if not np.any(covered):
        return out
    samples = factor[covered]
    lo = float(np.percentile(samples, 8))
    hi = float(np.percentile(samples, 92))
    span = max(hi - lo, 1e-3)
    shaped = np.clip((factor - lo) / span, 0.0, 1.0)
    out[covered] = float(out_lo) + (1.0 - float(out_lo)) * shaped[covered]
    return out


def load_albedo_occluded(path: Path):
    """Albedo RGBA. If ao_XX.png sits beside albedo_XX.png, the crown interior is darkened."""
    from PIL import Image

    im = Image.open(path).convert("RGBA")
    ao_path = path.with_name(path.name.replace("albedo_", "ao_"))
    if not ao_path.is_file() or "albedo_" not in path.name:
        return im
    arr = np.asarray(im).astype(np.float32)
    ao = Image.open(ao_path).convert("RGB")
    if ao.size != im.size:
        ao = ao.resize(im.size, Image.Resampling.BILINEAR)
    factor = crown_occlusion_factor(np.asarray(ao), arr[..., 3] > 16)
    arr[..., :3] *= factor[..., None]
    return Image.fromarray(np.clip(np.rint(arr), 0, 255).astype(np.uint8), "RGBA")


def build_spruce_mid_atlas(
    cells_dir: Path,
    color_dst: Path,
    opacity_dst: Path,
    normal_dst: Path | None = None,
) -> tuple[list[list[tuple[float, float, float]]], list[list[tuple[float, float]]]]:
    """Eight direction-matched cards: color atlas, opacity atlas, tangent normal atlas."""
    from PIL import Image

    cell_w, cell_h = MID_CELL
    cols = 4
    rows = (len(MID_VIEW_INDEXES) + cols - 1) // cols
    atlas_w, atlas_h = cols * cell_w, rows * cell_h
    atlas = Image.new("RGBA", (atlas_w, atlas_h), (0, 0, 0, 0))
    normals = Image.new("RGB", (atlas_w, atlas_h), (128, 128, 255))
    quads: list[list[tuple[float, float, float]]] = []
    uvs: list[list[tuple[float, float]]] = []
    inner_w = cell_w - 2 * MID_MARGIN
    inner_h = cell_h - 2 * MID_MARGIN
    for n, index in enumerate(MID_VIEW_INDEXES):
        src = cells_dir / f"albedo_{index:02d}.png"
        if not src.is_file():
            raise FileNotFoundError(src)
        im = Image.open(src).convert("RGBA")
        uv_rect = crop_card_uv(src)
        crop = _crop_rgba(im, uv_rect)
        nrm_src = cells_dir / f"normal_{index:02d}.png"
        nrm = tangent_normal_image(nrm_src, uv_rect, index) if nrm_src.is_file() else None
        scale = min(inner_w / crop.size[0], inner_h / crop.size[1], 1.0)
        if scale < 1.0:
            size = (max(1, int(crop.size[0] * scale)), max(1, int(crop.size[1] * scale)))
            crop = crop.resize(size, Image.Resampling.LANCZOS)
            if nrm is not None:
                nrm = nrm.resize(size, Image.Resampling.BILINEAR)
        col, row = n % cols, n // cols
        ox = col * cell_w + MID_MARGIN
        oy = row * cell_h + MID_MARGIN
        atlas.paste(crop, (ox, oy))
        if nrm is not None:
            normals.paste(nrm, (ox, oy))
        cw, ch = crop.size
        u0 = ox / atlas_w
        u1 = (ox + cw) / atlas_w
        v_top = 1.0 - oy / atlas_h
        v_bot = 1.0 - (oy + ch) / atlas_h
        uvs.append([(u0, v_bot), (u1, v_bot), (u1, v_top), (u0, v_top)])
        quads.append(spruce_view_quad(index, uv_rect))
    color_dst.parent.mkdir(parents=True, exist_ok=True)
    atlas.save(color_dst)
    write_card_opacity(atlas, opacity_dst)
    if normal_dst is not None:
        normals.save(normal_dst)
    return quads, uvs


def simplify_spruce_materials(path: Path, vfs_prefix: str) -> None:
    """Twig color factor, no normal/specular/subsurface. Far card uses spruce_card.color.png."""
    prefix = vfs_prefix.rstrip("/")
    data = json.loads(path.read_text(encoding="utf-8"))
    for name, block in list(data.items()):
        low = name.lower()
        is_twig = any(h in low for h in ("twig", "leaf", "nadel")) and "card" not in low
        is_bark = "bark" in low or "rinde" in low
        if is_twig:
            block["Stages"] = [
                {
                    "metallicFactor": 0.0,
                    "baseColorMap": f"{prefix}/twig_spruce.color.png",
                    "opacityMap": f"{prefix}/twig_spruce_o.data.png",
                    "roughnessFactor": 0.9,
                },
                {},
                {},
                {},
            ]
            for key in ("specularStrength0", "subSurface", "subSurfaceIntensity"):
                block.pop(key, None)
            block["alphaTest"] = True
            block["alphaRef"] = 75
            block["doubleSided"] = True
            block["invertBackFaceNormals"] = True
            block["castShadows"] = False
        elif is_bark:
            block["Stages"] = [
                {
                    "metallicFactor": 0.0,
                    "baseColorMap": f"{prefix}/bark_spruce.color.png",
                    "roughnessFactor": 0.85,
                },
                {},
                {},
                {},
            ]
            block["castShadows"] = False
    data["SpruceCard"] = {
        "name": "SpruceCard",
        "mapTo": "SpruceCard",
        "class": "Material",
        "Stages": [
            {
                "metallicFactor": 0.0,
                "baseColorMap": f"{prefix}/spruce_card.color.png",
                "opacityMap": f"{prefix}/spruce_card_o.data.png",
                "roughnessFactor": 1.0,
                "specular": [0.0, 0.0, 0.0, 1.0],
            },
            {},
            {},
            {},
        ],
        "castShadows": False,
        "annotation": "TREE",
        "version": 1.5,
        "alphaTest": True,
        "alphaRef": 65,
        "doubleSided": True,
        "invertBackFaceNormals": True,
        "pixelSpecular": False,
        "specularStrength0": "0",
        "subSurface": False,
    }
    # Double sided: defaultMat.hlsl flips normal.z on the back face under MFT_DoubleSided.
    data["SpruceMid"] = {
        "name": "SpruceMid",
        "mapTo": "SpruceMid",
        "class": "Material",
        "Stages": [
            {
                "metallicFactor": 0.0,
                "baseColorMap": f"{prefix}/spruce_mid.color.png",
                "opacityMap": f"{prefix}/spruce_mid_o.data.png",
                "normalMap": f"{prefix}/spruce_mid_n.data.png",
                "normalMapStrength": 1.0,
                "roughnessFactor": 1.0,
                "specular": [0.0, 0.0, 0.0, 1.0],
            },
            {},
            {},
            {},
        ],
        "castShadows": False,
        "annotation": "TREE",
        "version": 1.5,
        "alphaTest": True,
        "alphaRef": 65,
        "doubleSided": True,
        "invertBackFaceNormals": True,
        "pixelSpecular": False,
        "specularStrength0": "0",
        "subSurface": False,
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def install_spruce_far_lod(dae_path: Path | str, card_src: Path | str | None = None) -> dict[str, Any]:
    """Near mesh, eight view cards, then the size-25 camera billboard.

    Card color stays the raw albedo. The billboard color is multiplied by the
    occlusion masks when ao_XX.png is present. DDS is stamped newer than the DAE
    so the engine keeps the sidecars.
    """
    import os
    import time

    dae_path = Path(dae_path)
    cells = Path(card_src).parent if card_src else Path(r"C:\temp\assets\trees\out\spruce\cells")
    spec = species_cfg("spruce")
    n = int(spec["equator_steps"])
    dim = int(spec["dimension"])
    from PIL import Image

    albedo: list[np.ndarray] = []
    normals: list[np.ndarray] = []
    for i in range(n):
        color_path = cells / f"albedo_{i:02d}.png"
        normal_path = cells / f"normal_{i:02d}.png"
        if not color_path.is_file() or not normal_path.is_file():
            raise FileNotFoundError(f"missing capture {color_path.name}")
        color = load_albedo_occluded(color_path)
        normal = Image.open(normal_path).convert("RGBA")
        if color.size != (dim, dim):
            color = color.resize((dim, dim), Image.Resampling.BOX)
            normal = normal.resize((dim, dim), Image.Resampling.BOX)
        albedo.append(np.asarray(color, dtype=np.float32) / 255.0)
        normals.append(np.asarray(normal, dtype=np.float32) / 255.0)
    text = attach_camera_billboard(dae_path.read_text(encoding="utf-8"))
    quads, uvs = build_spruce_mid_atlas(
        cells,
        dae_path.with_name("spruce_mid.color.png"),
        dae_path.with_name("spruce_mid_o.data.png"),
        dae_path.with_name("spruce_mid_n.data.png"),
    )
    text = attach_mid_cards(text, quads, uvs)
    dae_path.write_text(text, encoding="utf-8", newline="\n")
    info = pack_camera_billboard(albedo, normals, dae_path.parent, dae_path.name, spec)
    now = time.time()
    os.utime(dae_path, (now, now))
    for suffix in (".imposter.dds", ".imposter_normals.dds", ".asset.json"):
        side = Path(str(dae_path) + suffix)
        os.utime(side, (now + 5.0, now + 5.0))
    mat = dae_path.with_name("main.materials.json")
    if mat.is_file():
        simplify_spruce_materials(mat, "art/shapes/trees/trees_spruce")
    info["dae"] = str(dae_path)
    return info


def write_tree_materials(path: Path, *, card_mat: str, bark_mat: str, card_png: str, bark_png: str | None, level_rel: str) -> None:
    """BeamNG materials.json for the cheap LOD mesh (not the engine imposter)."""
    base = level_rel.rstrip("/")
    stages_card = {
        "baseColorMap": f"{base}/{card_png}",
        "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
        "roughnessFactor": 0.85,
        "metallicFactor": 0.0,
    }
    data = {
        card_mat: {
            "name": card_mat,
            "mapTo": card_mat,
            "class": "Material",
            "Stages": [stages_card, {}, {}, {}],
            "alphaTest": True,
            "alphaRef": 84,
            "translucentBlendOp": "None",
            "translucentZWrite": True,
            "doubleSided": True,
            "castShadows": False,
            "annotation": "TREE",
            "version": 1.5,
        },
        bark_mat: {
            "name": bark_mat,
            "mapTo": bark_mat,
            "class": "Material",
            "Stages": [
                {
                    **(
                        {"baseColorMap": f"{base}/{bark_png}"}
                        if bark_png
                        else {"baseColorFactor": [0.22, 0.14, 0.08, 1.0]}
                    ),
                    "roughnessFactor": 0.9,
                    "metallicFactor": 0.0,
                },
                {},
                {},
                {},
            ],
            "castShadows": False,
            "annotation": "TREE",
            "version": 1.5,
        },
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
