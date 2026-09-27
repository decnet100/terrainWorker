"""Put the 0.5 m corridor ground onto the playable heightmap.

The road the player drives is the mesh. This layer is the ground beside and
under it, taken from the raw 0.5 m DGM. It replaces the coarser heightmap and
the road-bed layer inside the corridor. Bridge and gallery span layers stay
on top.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\apply_corridor_dgm.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import tifffile as tiff
from PIL import Image

import heightmap_layers as hml
from site_coords import SiteCoords, load_site, processed_dir

RES_M = 0.5


def _bilinear(arr: np.ndarray, fx: np.ndarray, fy: np.ndarray) -> np.ndarray:
    height, width = arr.shape
    x0 = np.floor(fx).astype(np.int32)
    y0 = np.floor(fy).astype(np.int32)
    x1 = np.clip(x0 + 1, 0, width - 1)
    y1 = np.clip(y0 + 1, 0, height - 1)
    x0 = np.clip(x0, 0, width - 1)
    y0 = np.clip(y0, 0, height - 1)
    tx = (fx - np.floor(fx)).astype(np.float32)
    ty = (fy - np.floor(fy)).astype(np.float32)
    out = (
        arr[y0, x0] * (1.0 - tx) * (1.0 - ty)
        + arr[y0, x1] * tx * (1.0 - ty)
        + arr[y1, x0] * (1.0 - tx) * ty
        + arr[y1, x1] * tx * ty
    )
    return np.asarray(out, dtype=np.float32)


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

    raw_path = proc / "corridor50_raw" / "corridor50_raw.tif"
    if not raw_path.is_file():
        raise SystemExit(f"missing {raw_path}")
    with tiff.TiffFile(raw_path) as src:
        page = src.pages[0]
        raw = np.asarray(page.asarray(), dtype=np.float32)
        tie = tuple(page.tags["ModelTiepointTag"].value)
        pix = tuple(page.tags["ModelPixelScaleTag"].value)
    if raw.ndim == 3:
        raw = raw[..., 0]
    res = float(pix[0])
    if abs(res - RES_M) > 1.0e-3:
        raise SystemExit(f"{raw_path.name} pixel size is {res}, expected {RES_M}")
    xmin = float(tie[3])
    ymax = float(tie[4])
    height, width = raw.shape
    xmax = xmin + width * res
    ymin = ymax - height * res

    c0 = max(0, int(np.floor((xmin - sc.xmin) / sc.bw * (size - 1))) - 1)
    c1 = min(size, int(np.ceil((xmax - sc.xmin) / sc.bw * (size - 1))) + 2)
    r_north = (size - 1) * (1.0 - (ymax - sc.ymin) / sc.bh)
    r_south = (size - 1) * (1.0 - (ymin - sc.ymin) / sc.bh)
    r0 = max(0, int(np.floor(min(r_north, r_south))) - 1)
    r1 = min(size, int(np.ceil(max(r_north, r_south))) + 2)

    z_u16 = np.zeros((size, size), dtype=np.uint16)
    w_u8 = np.zeros((size, size), dtype=np.uint8)
    n_write = 0
    for r_a in range(r0, r1, 256):
        r_b = min(r1, r_a + 256)
        cc = np.arange(c0, c1, dtype=np.float64)
        rr = np.arange(r_a, r_b, dtype=np.float64)
        bx = cc * scale
        by = (size - 1 - rr) * scale
        crs_x = sc.xmin + bx / extent * sc.bw
        crs_y = sc.ymin + by / extent * sc.bh
        gx, gy = np.meshgrid(crs_x, crs_y)
        inside = (gx >= xmin) & (gx < xmax) & (gy >= ymin) & (gy < ymax)
        fx = (gx - xmin) / res - 0.5
        fy = (ymax - gy) / res - 0.5
        sampled = _bilinear(raw, fx, fy)
        good = inside & np.isfinite(sampled) & (sampled > 50.0) & (sampled < 4500.0)
        if not np.any(good):
            continue
        rel = sampled[good].astype(np.float64) - z_min
        z_u16[r_a:r_b, c0:c1][good] = hml._encode_z_u16(rel, max_h)
        w_u8[r_a:r_b, c0:c1][good] = 255
        n_write += int(np.count_nonzero(good))
        print(f"  rows {r_a}-{r_b} px={int(np.count_nonzero(good))}", flush=True)

    if n_write == 0:
        raise SystemExit("0.5 m DGM hit no heightmap pixel")
    layers = hml.layers_dir(proc)
    Image.fromarray(z_u16, mode="I;16").save(layers / "corridor_dgm_z.png")
    Image.fromarray(w_u8, mode="L").save(layers / "corridor_dgm_w.png")
    hml._upsert_layer(
        proc,
        {
            "name": "corridor_dgm",
            "mode": "replace",
            "priority": 45,
            "size": size,
            "max_height_m": max_h,
            "z": "heightmap_layers/corridor_dgm_z.png",
            "weight": "heightmap_layers/corridor_dgm_w.png",
            "nz": n_write,
        },
    )
    print(f"corridor ground px={n_write} — composing", flush=True)
    hml.compose(proc, size=size, max_h=max_h, level_name=level_name or None)


if __name__ == "__main__":
    main()
