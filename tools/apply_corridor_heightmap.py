"""Resample the filtered 0.5 m road surface onto the 1 m heightmap.

Samples the corridor at each heightmap node and writes those heights into the
road-bed layer, only where the filter actually moved the ground. The pristine
DGM stays untouched. Compose then rebuilds the playable heightmap.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\apply_corridor_heightmap.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import tifffile as tiff
from PIL import Image

import heightmap_layers as hml
from site_coords import SiteCoords, load_site, processed_dir, site_slug

ROOT = Path(__file__).resolve().parents[1]
# Below this, the pixel is still the raw corridor (span, clamp reject, mask edge).
MOVE_M = 0.005


def _load_pair(raw: Path, filt: Path) -> tuple[np.ndarray, np.ndarray]:
    src = np.asarray(tiff.imread(raw), dtype=np.float32)
    dst = np.asarray(tiff.imread(filt), dtype=np.float32)
    if src.ndim == 3:
        src = src[..., 0]
    if dst.ndim == 3:
        dst = dst[..., 0]
    if src.shape != dst.shape:
        raise SystemExit(f"shape {raw.name} {src.shape} != {dst.shape}")
    return src, dst


def _bilinear(arr: np.ndarray, fx: np.ndarray, fy: np.ndarray) -> np.ndarray:
    height, width = arr.shape
    x0 = np.floor(fx).astype(np.int32)
    y0 = np.floor(fy).astype(np.int32)
    x1 = x0 + 1
    y1 = y0 + 1
    valid = (x0 >= 0) & (y0 >= 0) & (x1 < width) & (y1 < height)
    x0c = np.clip(x0, 0, width - 1)
    x1c = np.clip(x1, 0, width - 1)
    y0c = np.clip(y0, 0, height - 1)
    y1c = np.clip(y1, 0, height - 1)
    tx = (fx - x0).astype(np.float32)
    ty = (fy - y0).astype(np.float32)
    v00 = arr[y0c, x0c]
    v10 = arr[y0c, x1c]
    v01 = arr[y1c, x0c]
    v11 = arr[y1c, x1c]
    out = (
        v00 * (1.0 - tx) * (1.0 - ty)
        + v10 * tx * (1.0 - ty)
        + v01 * (1.0 - tx) * ty
        + v11 * tx * ty
    )
    bad = (
        ~valid
        | ~np.isfinite(v00)
        | ~np.isfinite(v10)
        | ~np.isfinite(v01)
        | ~np.isfinite(v11)
    )
    out = np.asarray(out, dtype=np.float32)
    out[bad] = np.nan
    return out


def main() -> None:
    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "").strip()
    meta = json.loads((proc / "heightmap_meta.json").read_text(encoding="utf-8"))
    size = int(meta.get("heightmap_size_px") or bng.get("mask_size") or 8192)
    extent = float(meta.get("terrain_extent_m") or size)
    max_h = float(meta["max_height_m"])
    z_min = float(meta["z_min_m"])
    scale = extent / (size - 1)

    layers = proc / "heightmap_layers"
    z_path = layers / "road_bed_z.png"
    w_path = layers / "road_bed_w.png"
    if not z_path.is_file() or not w_path.is_file():
        raise SystemExit("road_bed layer missing")
    z_u16 = np.array(Image.open(z_path), copy=True)
    w_u8 = np.array(Image.open(w_path), copy=True)
    if z_u16.shape != (size, size) or w_u8.shape != (size, size):
        raise SystemExit(f"road_bed shape {z_u16.shape} != {size}")
    if z_u16.dtype != np.uint16:
        z_u16 = z_u16.astype(np.uint16)
    if w_u8.dtype != np.uint8:
        w_u8 = w_u8.astype(np.uint8)

    raw_dir = ROOT / "data" / "raw" / f"dgm_{site_slug(site)}_corridor50"
    raw_index = json.loads((raw_dir / "corridor_index.json").read_text(encoding="utf-8"))
    filt_dir = proc / "corridor50_road"
    filt = json.loads((filt_dir / "corridor50_road_index.json").read_text(encoding="utf-8"))
    if "raster" not in filt:
        raise SystemExit("filtered corridor is not one raster; run filter_road_corridor.py")
    filtered = np.asarray(tiff.imread(filt_dir / filt["raster"]), dtype=np.float32)
    if filtered.ndim == 3:
        filtered = filtered[..., 0]
    fx0 = float(filt["xmin"])
    fy0 = float(filt["ymax"])
    fres = float(filt["resolution_m"])
    composed = hml._decode_z_u16(
        np.asarray(Image.open(hml.composed_path(proc, size))).astype(np.uint16),
        max_h,
    )

    n_write = 0
    max_delta = 0.0
    for spec in raw_index["tiles"]:
        src = np.asarray(tiff.imread(raw_dir / spec["name"]), dtype=np.float32)
        if src.ndim == 3:
            src = src[..., 0]
        xmin, xmax = float(spec["xmin"]), float(spec["xmax"])
        ymin, ymax = float(spec["ymin"]), float(spec["ymax"])
        c0 = max(0, int(np.floor((xmin - sc.xmin) / sc.bw * (size - 1))) - 1)
        c1 = min(size, int(np.ceil((xmax - sc.xmin) / sc.bw * (size - 1))) + 2)
        r_north = (size - 1) * (1.0 - (ymax - sc.ymin) / sc.bh)
        r_south = (size - 1) * (1.0 - (ymin - sc.ymin) / sc.bh)
        r0 = max(0, int(np.floor(min(r_north, r_south))) - 1)
        r1 = min(size, int(np.ceil(max(r_north, r_south))) + 2)
        cc = np.arange(c0, c1, dtype=np.float64)
        rr = np.arange(r0, r1, dtype=np.float64)
        bx = cc * scale
        by = (size - 1 - rr) * scale
        crs_x = sc.xmin + bx / extent * sc.bw
        crs_y = sc.ymin + by / extent * sc.bh
        gx, gy = np.meshgrid(crs_x, crs_y)
        inside = (gx >= xmin) & (gx < xmax) & (gy >= ymin) & (gy < ymax)
        fx = (gx - xmin) / (xmax - xmin) * int(spec["width"]) - 0.5
        fy = (ymax - gy) / (ymax - ymin) * int(spec["height"]) - 0.5
        raw = _bilinear(src, fx, fy)
        got = _bilinear(filtered, (gx - fx0) / fres - 0.5, (fy0 - gy) / fres - 0.5)
        moved = inside & np.isfinite(got) & np.isfinite(raw) & (np.abs(got - raw) >= MOVE_M)
        if not np.any(moved):
            print(f"  {spec['name']} no move", flush=True)
            continue
        rel = got[moved].astype(np.float64) - z_min
        enc = hml._encode_z_u16(rel, max_h)
        sl = (slice(r0, r1), slice(c0, c1))
        old = composed[sl][moved]
        max_delta = max(max_delta, float(np.max(np.abs(rel - old))))
        ys, xs = np.nonzero(moved)
        z_u16[r0 + ys, c0 + xs] = enc
        w_u8[r0 + ys, c0 + xs] = 255
        n = int(np.count_nonzero(moved))
        n_write += n
        print(f"  {spec['name']} px={n}", flush=True)

    Image.fromarray(z_u16, mode="I;16").save(z_path)
    Image.fromarray(w_u8, mode="L").save(w_path)
    hml._upsert_layer(
        proc,
        {
            "name": "road_bed",
            "mode": "replace",
            "priority": 40,
            "size": size,
            "max_height_m": max_h,
            "z": "heightmap_layers/road_bed_z.png",
            "weight": "heightmap_layers/road_bed_w.png",
            "nz": int(np.count_nonzero(w_u8 > 0)),
        },
    )
    print(
        f"road bed updated px={n_write} max_delta={max_delta:.3f}m — composing",
        flush=True,
    )
    hml.compose(proc, size=size, max_h=max_h, level_name=level_name or None)
    print(
        f"Inserted {n_write} heightmap nodes from the 0.5 m surface "
        f"(threshold {MOVE_M:.3f} m, max move {max_delta:.3f} m)",
        flush=True,
    )


if __name__ == "__main__":
    main()
