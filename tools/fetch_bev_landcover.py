"""Fetch BEV INSPIRE Land Cover raster via WMS GetMap (GeoTIFF).

Source: https://data.bev.gv.at/geoserver/INSdataLC/wms
Layer: LC.LandCoverRaster — AT Gesamtmosaik LC 2021–2023, 6 Klassen
  0 Vegetation hoch
  1 Vegetation mittel
  2 Vegetation niedrig
  3 Bodenflächen
  4 Gebäude
  5 Gewässer

WCS is disabled on this GeoServer; WMS ``image/geotiff`` returns classified
uint8. EPSG:31254 WMS 1.3.0 BBOX axis order: miny,minx,maxy,maxx.

Writes:
  data/raw/bev_landcover_<site>.tif (+ .meta.json)
  data/processed/<site>/landcover_bev.tif
  data/processed/<site>/preview_landcover_bev.png
  data/processed/<site>/landcover_bev.meta.json

Usage:
  cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = \"config/sites/fernpass_mega.yaml\"; python tools\\fetch_bev_landcover.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import requests
import tifffile as tiff
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import load_site, processed_dir, site_slug  # noqa: E402

DEFAULT_WMS = "https://data.bev.gv.at/geoserver/INSdataLC/ows"
DEFAULT_LAYER = "LC.LandCoverRaster"

# Official legend order (top→bottom) → class code in returned GeoTIFF
CLASS_NAMES = {
    0: "vegetation_hoch",
    1: "vegetation_mittel",
    2: "vegetation_niedrig",
    3: "bodenflaechen",
    4: "gebaeude",
    5: "gewaesser",
    6: "nodata",  # GeoServer GDAL_NODATA
}

# Palette for preview (approx. BEV legend colours)
CLASS_RGB = {
    0: (34, 100, 34),
    1: (80, 170, 60),
    2: (180, 220, 140),
    3: (196, 170, 120),
    4: (220, 40, 40),
    5: (40, 100, 220),
}


def _cfg(site: dict) -> dict:
    src = ((site.get("sources") or {}).get("bev_landcover") or {})
    size = int(
        src.get("size")
        or (site.get("beamng") or {}).get("mask_size")
        or 512
    )
    return {
        "url": str(src.get("url") or DEFAULT_WMS),
        "layer": str(src.get("layer") or DEFAULT_LAYER),
        "size": size,
        "timeout_s": int(src.get("timeout_s") or 600),
    }


def _raw_paths(site: dict) -> tuple[Path, Path]:
    slug = site_slug(site)
    raw = ROOT / "data" / "raw"
    return raw / f"bev_landcover_{slug}.tif", raw / f"bev_landcover_{slug}.meta.json"


def fetch_bev_landcover_bbox(
    site: dict,
    bbox_xy: tuple[float, float, float, float],
    dest: Path,
    *,
    size: int,
    force: bool = False,
) -> Path:
    """WMS GetMap for an arbitrary EPSG:31254 envelope (playable or near)."""
    cfg = _cfg(site)
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta_path = dest.with_suffix(".meta.json")
    xmin, ymin, xmax, ymax = (float(v) for v in bbox_xy)
    width_m = max(xmax - xmin, 1.0)
    height_m = max(ymax - ymin, 1.0)
    # Keep square pixels; WMS WIDTH/HEIGHT follow the envelope aspect.
    if width_m >= height_m:
        w = int(size)
        h = max(1, int(round(size * height_m / width_m)))
    else:
        h = int(size)
        w = max(1, int(round(size * width_m / height_m)))

    if dest.is_file() and dest.stat().st_size > 1000 and meta_path.is_file() and not force:
        print(f"Using cached BEV landcover: {dest} ({dest.stat().st_size} bytes)")
        return dest

    # WMS 1.3.0 + EPSG:31254 → axis order northing,easting
    bbox = f"{ymin},{xmin},{ymax},{xmax}"
    params = {
        "SERVICE": "WMS",
        "VERSION": "1.3.0",
        "REQUEST": "GetMap",
        "LAYERS": cfg["layer"],
        "STYLES": "",
        "CRS": str(site.get("crs", "EPSG:31254")),
        "BBOX": bbox,
        "WIDTH": str(w),
        "HEIGHT": str(h),
        "FORMAT": "image/geotiff",
        "TRANSPARENT": "FALSE",
    }
    print(
        f"BEV Land Cover WMS GetMap {cfg['layer']} "
        f"{w}x{h} CRS={params['CRS']} BBOX={bbox} …"
    )
    r = requests.get(cfg["url"], params=params, timeout=cfg["timeout_s"])
    r.raise_for_status()
    ctype = (r.headers.get("content-type") or "").lower()
    if "tiff" not in ctype and not r.content.startswith((b"II", b"MM")):
        raise SystemExit(
            f"WMS did not return GeoTIFF (content-type={ctype}): {r.content[:300]!r}"
        )
    dest.write_bytes(r.content)
    meta = {
        "url": cfg["url"],
        "layer": cfg["layer"],
        "crs": site.get("crs"),
        "bbox": [xmin, ymin, xmax, ymax],
        "bbox_wms13": bbox,
        "size": [w, h],
        "format": "image/geotiff",
        "bytes": len(r.content),
        "classes": CLASS_NAMES,
        "mosaic": "AT_Gesamtmosaik_LC_2021-2023",
        "path": str(dest.relative_to(ROOT)).replace("\\", "/"),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {dest} ({len(r.content)} bytes)")
    print(f"Wrote {meta_path}")
    return dest


def fetch_bev_landcover(site: dict, *, force: bool = False) -> Path:
    cfg = _cfg(site)
    out, _meta_path = _raw_paths(site)
    xmin, ymin, xmax, ymax = map(float, site["bbox"])
    return fetch_bev_landcover_bbox(
        site,
        (xmin, ymin, xmax, ymax),
        out,
        size=cfg["size"],
        force=force,
    )


def fetch_bev_near(site: dict, *, force: bool = False, size: int | None = None) -> Path:
    """BEV mosaic for the backdrop near envelope (AT only; CH/IT stay nodata)."""
    from fetch_backdrop import backdrop_cfg, near_extent  # noqa: WPS433

    slug = site_slug(site)
    dest = ROOT / "data" / "raw" / f"bev_landcover_near_{slug}.tif"
    nsize = int(size or backdrop_cfg(site).get("near_texture_size") or 2048)
    return fetch_bev_landcover_bbox(site, near_extent(site), dest, size=nsize, force=force)


def fetch_bev_extent(
    site: dict,
    extent: tuple[float, float, float, float],
    tag: str,
    *,
    size: int,
    force: bool = False,
) -> Path:
    slug = site_slug(site)
    dest = ROOT / "data" / "raw" / f"bev_landcover_{tag}_{slug}.tif"
    return fetch_bev_landcover_bbox(site, extent, dest, size=size, force=force)


def _extent_close(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return all(abs(float(x) - float(y)) < 1.5 for x, y in zip(a, b))


def load_bev_forest_mask(
    site: dict,
    dest_shape: tuple[int, int],
    extent: tuple[float, float, float, float],
    *,
    force: bool = False,
) -> np.ndarray:
    """BEV vegetation_hoch|mittel as bool mask on dest_shape (nearest)."""
    from fetch_backdrop import backdrop_cfg, mid_extent, near_extent, padded_extent  # noqa: WPS433
    from scipy.ndimage import map_coordinates  # noqa: WPS433

    cfg = backdrop_cfg(site)
    ext = tuple(float(v) for v in extent)
    if _extent_close(ext, near_extent(site)):
        path = fetch_bev_near(site, force=force, size=int(cfg.get("near_texture_size") or dest_shape[0]))
    elif _extent_close(ext, mid_extent(site)):
        path = fetch_bev_extent(site, ext, "mid", size=int(cfg.get("mid_texture_size") or dest_shape[0]), force=force)
    elif _extent_close(ext, padded_extent(site)):
        path = fetch_bev_extent(site, ext, "far", size=int(cfg.get("texture_size") or dest_shape[0]), force=force)
    else:
        path = fetch_bev_extent(site, ext, "custom", size=int(dest_shape[0]), force=force)

    meta_path = path.with_suffix(".meta.json")
    codes = _load_classified(path)
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        bx0, by0, bx1, by1 = map(float, meta["bbox"])
    else:
        bx0, by0, bx1, by1 = ext
    bh, bw = int(codes.shape[0]), int(codes.shape[1])
    h, w = int(dest_shape[0]), int(dest_shape[1])
    ox, oy = float(ext[0]), float(ext[3])
    px = (float(ext[2]) - float(ext[0])) / w
    py = -(float(ext[3]) - float(ext[1])) / h
    xs = ox + (np.arange(w, dtype=np.float64) + 0.5) * px
    ys = oy + (np.arange(h, dtype=np.float64) + 0.5) * py
    xx, yy = np.meshgrid(xs, ys)
    col = (xx - bx0) / ((bx1 - bx0) / bw) - 0.5
    row = (yy - by1) / (-(by1 - by0) / bh) - 0.5
    inside = (row >= 0) & (row <= bh - 1) & (col >= 0) & (col <= bw - 1)
    sampled = np.full((h, w), 6, dtype=np.uint8)
    if np.any(inside):
        vals = map_coordinates(
            codes.astype(np.float64),
            np.vstack([row[inside], col[inside]]),
            order=0,
            mode="nearest",
        )
        sampled[inside] = np.rint(vals).astype(np.uint8)
    return (sampled == 0) | (sampled == 1)


def _load_classified(path: Path) -> np.ndarray:
    arr = np.asarray(tiff.imread(path))
    if arr.ndim == 3:
        # unexpected RGB render — leave as-is fails; take first band if grayscale-ish
        raise SystemExit(
            f"Expected classified single-band GeoTIFF, got shape={arr.shape}. "
            "Check WMS BBOX axis order / FORMAT."
        )
    return arr.astype(np.uint8, copy=False)


def _preview_rgb(codes: np.ndarray, path: Path) -> None:
    h, w = codes.shape
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    for code, col in CLASS_RGB.items():
        m = codes == code
        rgb[m] = col
    # unknown / nodata
    known = np.zeros(codes.shape, dtype=bool)
    for c in CLASS_RGB:
        known |= codes == c
    rgb[~known] = (0, 0, 0)
    Image.fromarray(rgb, mode="RGB").save(path)
    print(f"Wrote {path}")


def _export_processed(site: dict, raw_path: Path) -> None:
    proc = processed_dir(site)
    codes = _load_classified(raw_path)
    size = int((site.get("beamng") or {}).get("mask_size") or codes.shape[0])
    if codes.shape != (size, size):
        img = Image.fromarray(codes, mode="L")
        img = img.resize((size, size), resample=Image.Resampling.NEAREST)
        codes = np.asarray(img, dtype=np.uint8)
        print(f"Resampled landcover → {size}x{size}")

    tiff.imwrite(proc / "landcover_bev.tif", codes, compression="deflate")
    _preview_rgb(codes, proc / "preview_landcover_bev.png")
    hist = {CLASS_NAMES.get(int(v), str(int(v))): float((codes == v).mean()) for v in np.unique(codes)}
    meta = {
        "site": site_slug(site),
        "source": "bev_inspire_wms",
        "raw": str(raw_path.relative_to(ROOT)),
        "classes": CLASS_NAMES,
        "histogram": hist,
        "files": ["landcover_bev.tif", "preview_landcover_bev.png"],
    }
    (proc / "landcover_bev.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {proc / 'landcover_bev.tif'}")
    print(f"Wrote {proc / 'landcover_bev.meta.json'}")
    for k, v in sorted(hist.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v*100:.2f}%")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="Re-download even if cache exists")
    args = ap.parse_args()
    site = load_site()
    print(f"Site: {site_slug(site)}")
    raw = fetch_bev_landcover(site, force=args.force)
    _export_processed(site, raw)


if __name__ == "__main__":
    main()
