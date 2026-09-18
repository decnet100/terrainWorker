"""Build site snow_fsc raster from local CLMS HR-WSI FSCOG GeoTIFFs.

Expects scene folders under ``data/raw/fsc_hrwsi/`` (or ``sources.snow.local_dir``),
each containing ``*_FSCOG.tif`` (optional legend/ etc.):

  data/raw/fsc_hrwsi/CLMS_WSI_FSC_020m_T32TPT_…_V200/
      …_FSCOG.tif

Value codes (official legend):
  0–100  snow fraction %
  205    cloud / cloud shadow
  210    inland water
  255    nodata

Composite (multi-scene): clear (0–100) and water (210) overwrite cloud/nodata;
among snow codes keep the **max** fraction. Cloud/nodata never overwrite clear.

Writes:
  data/processed/<site>/snow_fsc.tif          float32 0–1
  data/processed/<site>/snow_fsc_codes.tif    uint8 composite (site grid)
  data/processed/<site>/preview_snow_fsc.png
  data/processed/<site>/snow_fsc.meta.json

Usage:
  cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = \"config/sites/fernpass_mega.yaml\"; python tools\\build_snow_fsc.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import tifffile as tiff
from PIL import Image
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import load_site, processed_dir, site_slug  # noqa: E402

CODE_CLOUD = 205
CODE_WATER = 210
CODE_NODATA = 255


@dataclass
class GeoRaster:
    data: np.ndarray  # uint8 HxW
    crs: str
    origin_x: float  # world x of pixel (0,0) top-left corner
    origin_y: float  # world y of pixel (0,0) top-left corner
    px: float
    py: float  # usually negative if north-up stored with positive scale in tag
    path: Path


def _snow_cfg(site: dict) -> dict:
    src = ((site.get("sources") or {}).get("snow") or {})
    local = src.get("local_dir") or "data/raw/fsc_hrwsi"
    p = Path(str(local))
    if not p.is_absolute():
        p = ROOT / p
    return {
        "local_dir": p,
        "composite": str(src.get("composite") or "max"),  # max|last
    }


def _read_geotiff_uint8(path: Path) -> GeoRaster:
    with tiff.TiffFile(path) as tf:
        page = tf.pages[0]
        data = page.asarray()
        if data.ndim == 3:
            data = data[..., 0]
        data = np.asarray(data, dtype=np.uint8)
        scale = page.tags.get("ModelPixelScaleTag")
        tie = page.tags.get("ModelTiepointTag")
        if scale is None or tie is None:
            raise SystemExit(f"Missing GeoTIFF tags: {path}")
        sx, sy, _sz = scale.value
        # tie: I,J,K, X,Y,Z — pixel (I,J) maps to (X,Y)
        _i, _j, _k, x0, y0, _z = tie.value[:6]
        # North-up GeoTIFF: row increases south → world Y decreases
        origin_x = float(x0 - _i * sx)
        origin_y = float(y0 + _j * sy)
        # GeoKeys ProjectedCSTypeGeoKey 3072 → EPSG
        epsg = 32632
        gk = page.tags.get("GeoKeyDirectoryTag")
        if gk is not None:
            vals = list(gk.value)
            n = int(vals[3]) if len(vals) > 3 else 0
            for i in range(n):
                key, loc, _count, val = vals[4 + i * 4 : 8 + i * 4]
                if int(key) == 3072 and int(loc) == 0:
                    epsg = int(val)
                    break
    return GeoRaster(
        data=data,
        crs=f"EPSG:{epsg}",
        origin_x=origin_x,
        origin_y=origin_y,
        px=float(sx),
        py=-float(sy),  # row step in world Y
        path=path,
    )


def _discover_fscog(local_dir: Path) -> list[Path]:
    if not local_dir.is_dir():
        return []
    files = sorted(local_dir.rglob("*_FSCOG.tif"))
    # exclude QA siblings
    files = [p for p in files if not p.name.upper().endswith("FSCOG-QA.TIF")]
    return files


def _scene_sort_key(path: Path) -> str:
    m = re.search(r"_(\d{8}T\d{6})_", path.name)
    return m.group(1) if m else path.name


def _is_clear_snow(v: np.ndarray) -> np.ndarray:
    return v <= 100


def _is_water(v: np.ndarray) -> np.ndarray:
    return v == CODE_WATER


def _is_empty(v: np.ndarray) -> np.ndarray:
    return (v == CODE_CLOUD) | (v == CODE_NODATA)


def _composite_codes(scenes: list[GeoRaster], *, mode: str) -> tuple[np.ndarray, dict]:
    """Composite onto the grid of the first scene's CRS; union bbox at native res."""
    if not scenes:
        raise SystemExit("No FSCOG scenes")
    crs = scenes[0].crs
    for s in scenes[1:]:
        if s.crs != crs:
            raise SystemExit(f"Mixed CRS not supported yet: {scenes[0].path.name} vs {s.path.name}")

    # Union extent (pixel corners)
    min_x = min(s.origin_x for s in scenes)
    max_y = max(s.origin_y for s in scenes)
    max_x = max(s.origin_x + s.data.shape[1] * s.px for s in scenes)
    min_y = min(s.origin_y + s.data.shape[0] * s.py for s in scenes)
    px = scenes[0].px
    py = scenes[0].py  # negative
    width = int(round((max_x - min_x) / px))
    height = int(round((min_y - max_y) / py))  # py < 0 → positive
    width = max(1, width)
    height = max(1, height)

    out = np.full((height, width), CODE_NODATA, dtype=np.uint8)
    painted = 0
    for s in scenes:
        # Destination window for this scene
        c0 = int(round((s.origin_x - min_x) / px))
        r0 = int(round((s.origin_y - max_y) / py))
        h, w = s.data.shape
        # Clip to out
        src = s.data
        r1, c1 = r0 + h, c0 + w
        rs0, cs0 = max(0, r0), max(0, c0)
        rs1, cs1 = min(height, r1), min(width, c1)
        if rs0 >= rs1 or cs0 >= cs1:
            continue
        src_r0, src_c0 = rs0 - r0, cs0 - c0
        chunk = src[src_r0 : src_r0 + (rs1 - rs0), src_c0 : src_c0 + (cs1 - cs0)]
        dest = out[rs0:rs1, cs0:cs1]

        clear = _is_clear_snow(chunk)
        water = _is_water(chunk)
        empty_dest = _is_empty(dest)

        # Water fills empty only
        m_w = water & empty_dest
        dest[m_w] = CODE_WATER

        # Snow: overwrite empty, or max/last among snow
        snow_dest = _is_clear_snow(dest)
        m_new = clear & empty_dest
        dest[m_new] = chunk[m_new]
        if mode == "last":
            m_upd = clear & snow_dest
            dest[m_upd] = chunk[m_upd]
        else:  # max
            m_upd = clear & snow_dest & (chunk > dest)
            dest[m_upd] = chunk[m_upd]

        out[rs0:rs1, cs0:cs1] = dest
        painted += 1

    meta = {
        "crs": crs,
        "origin_x": min_x,
        "origin_y": max_y,
        "px": px,
        "py": py,
        "width": width,
        "height": height,
        "scenes_painted": painted,
    }
    return out, meta


def _resample_codes_to_site(
    codes: np.ndarray,
    geo: dict,
    site: dict,
) -> np.ndarray:
    """Nearest-neighbour sample composite → site mask_size grid.

    Samples at ~source resolution first (fast), then nearest-upscale to mask_size.
    """
    size = int((site.get("beamng") or {}).get("mask_size") or 512)
    xmin, ymin, xmax, ymax = map(float, site["bbox"])
    site_crs = str(site.get("crs", "EPSG:31254"))
    to_src = Transformer.from_crs(site_crs, geo["crs"], always_xy=True)

    # Coarse grid ≈ FSC pixel size in site metres
    step = float(abs(geo["px"]))
    nw = max(1, int(np.ceil((xmax - xmin) / step)))
    nh = max(1, int(np.ceil((ymax - ymin) / step)))
    xs = xmin + (np.arange(nw) + 0.5) * (xmax - xmin) / nw
    ys = ymax - (np.arange(nh) + 0.5) * (ymax - ymin) / nh
    xx, yy = np.meshgrid(xs, ys)
    sx, sy = to_src.transform(xx.ravel(), yy.ravel())
    sx = np.asarray(sx, dtype=np.float64)
    sy = np.asarray(sy, dtype=np.float64)

    cols = np.floor((sx - geo["origin_x"]) / geo["px"]).astype(np.int64)
    rows = np.floor((sy - geo["origin_y"]) / geo["py"]).astype(np.int64)
    h, w = codes.shape
    valid = (rows >= 0) & (rows < h) & (cols >= 0) & (cols < w)
    coarse = np.full(nh * nw, CODE_NODATA, dtype=np.uint8)
    coarse[valid] = codes[rows[valid], cols[valid]]
    coarse = coarse.reshape(nh, nw)

    if nh == size and nw == size:
        return coarse
    img = Image.fromarray(coarse, mode="L")
    img = img.resize((size, size), resample=Image.Resampling.NEAREST)
    return np.asarray(img, dtype=np.uint8)

def _codes_to_fraction(codes: np.ndarray) -> np.ndarray:
    """0–100 → 0–1; water/cloud/nodata → 0."""
    out = np.zeros(codes.shape, dtype=np.float32)
    m = codes <= 100
    out[m] = codes[m].astype(np.float32) / 100.0
    return out


def _preview(frac: np.ndarray, path: Path) -> None:
    u8 = np.clip(np.asarray(frac) * 255.0, 0, 255).astype(np.uint8)
    Image.fromarray(u8, mode="L").save(path)
    print(f"Wrote {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--local-dir",
        type=str,
        default="",
        help="Override sources.snow.local_dir (default data/raw/fsc_hrwsi)",
    )
    args = ap.parse_args()

    site = load_site()
    slug = site_slug(site)
    proc = processed_dir(site)
    cfg = _snow_cfg(site)
    if args.local_dir:
        p = Path(args.local_dir)
        cfg["local_dir"] = p if p.is_absolute() else ROOT / p

    print(f"Site: {slug}")
    print(f"Local FSC dir: {cfg['local_dir']}")

    files = _discover_fscog(cfg["local_dir"])
    if not files:
        raise SystemExit(
            f"No *_FSCOG.tif under {cfg['local_dir']}.\n"
            "Drop WEkEO scene folders there (…/CLMS_WSI_FSC_…/…_FSCOG.tif)."
        )
    files = sorted(files, key=_scene_sort_key)
    print(f"Found {len(files)} FSCOG scene(s):")
    for f in files:
        print(f"  {f.relative_to(ROOT)}")

    scenes = [_read_geotiff_uint8(f) for f in files]
    for s in scenes:
        a = s.data
        print(
            f"  {s.path.name}: {a.shape} crs={s.crs} "
            f"clear={(a <= 100).mean()*100:.1f}% cloud={(a == CODE_CLOUD).mean()*100:.1f}% "
            f"water={(a == CODE_WATER).mean()*100:.1f}% nodata={(a == CODE_NODATA).mean()*100:.1f}%"
        )

    codes_u, geo = _composite_codes(scenes, mode=cfg["composite"])
    print(
        f"Composite {geo['width']}x{geo['height']} {geo['crs']} "
        f"clear={(codes_u <= 100).mean()*100:.1f}% cloud={(codes_u == CODE_CLOUD).mean()*100:.1f}%"
    )

    codes_site = _resample_codes_to_site(codes_u, geo, site)
    frac = _codes_to_fraction(codes_site)

    tiff.imwrite(proc / "snow_fsc_codes.tif", codes_site, compression="deflate")
    tiff.imwrite(proc / "snow_fsc.tif", frac, compression="deflate")
    print(f"Wrote {proc / 'snow_fsc_codes.tif'}")
    print(f"Wrote {proc / 'snow_fsc.tif'}")
    _preview(frac, proc / "preview_snow_fsc.png")

    meta = {
        "site": slug,
        "product": "CLMS_HRWSI_FSC",
        "band": "FSCOG",
        "source": "local",
        "local_dir": str(cfg["local_dir"].relative_to(ROOT)),
        "scenes": [str(p.relative_to(ROOT)) for p in files],
        "composite": cfg["composite"],
        "codes": {
            "0-100": "snow fraction %",
            "205": "cloud/shadow",
            "210": "inland water",
            "255": "nodata",
        },
        "note": "Fraction 0–1 from codes 0–100; water/cloud/nodata → 0 in snow_fsc.tif",
        "site_clear_pct": float((codes_site <= 100).mean() * 100),
        "site_cloud_pct": float((codes_site == CODE_CLOUD).mean() * 100),
        "site_water_pct": float((codes_site == CODE_WATER).mean() * 100),
        "site_snow_gt_10pct": float(((codes_site <= 100) & (codes_site >= 10)).mean() * 100),
        "site_snow_gt_50pct": float(((codes_site <= 100) & (codes_site >= 50)).mean() * 100),
        "files": ["snow_fsc.tif", "snow_fsc_codes.tif", "preview_snow_fsc.png"],
    }
    (proc / "snow_fsc.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {proc / 'snow_fsc.meta.json'}")
    print(
        f"Site: clear={meta['site_clear_pct']:.1f}%  "
        f"snow>10%={meta['site_snow_gt_10pct']:.1f}%  "
        f"snow>50%={meta['site_snow_gt_50pct']:.1f}%  "
        f"cloud={meta['site_cloud_pct']:.1f}%  water={meta['site_water_pct']:.1f}%"
    )


if __name__ == "__main__":
    main()
