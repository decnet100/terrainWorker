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


def fetch_bev_landcover(site: dict, *, force: bool = False) -> Path:
    cfg = _cfg(site)
    out, meta_path = _raw_paths(site)
    out.parent.mkdir(parents=True, exist_ok=True)
    xmin, ymin, xmax, ymax = map(float, site["bbox"])
    size = cfg["size"]

    if out.is_file() and out.stat().st_size > 1000 and not force:
        print(f"Using cached BEV landcover: {out} ({out.stat().st_size} bytes)")
        return out

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
        "WIDTH": str(size),
        "HEIGHT": str(size),
        "FORMAT": "image/geotiff",
        "TRANSPARENT": "FALSE",
    }
    print(
        f"BEV Land Cover WMS GetMap {cfg['layer']} "
        f"{size}x{size} CRS={params['CRS']} BBOX={bbox} …"
    )
    r = requests.get(cfg["url"], params=params, timeout=cfg["timeout_s"])
    r.raise_for_status()
    ctype = (r.headers.get("content-type") or "").lower()
    if "tiff" not in ctype and not r.content.startswith((b"II", b"MM")):
        raise SystemExit(
            f"WMS did not return GeoTIFF (content-type={ctype}): {r.content[:300]!r}"
        )
    out.write_bytes(r.content)
    meta = {
        "url": cfg["url"],
        "layer": cfg["layer"],
        "crs": site.get("crs"),
        "bbox": [xmin, ymin, xmax, ymax],
        "bbox_wms13": bbox,
        "size": size,
        "format": "image/geotiff",
        "bytes": len(r.content),
        "classes": CLASS_NAMES,
        "mosaic": "AT_Gesamtmosaik_LC_2021-2023",
        "path": str(out.relative_to(ROOT)),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {out} ({len(r.content)} bytes)")
    print(f"Wrote {meta_path}")
    return out


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
