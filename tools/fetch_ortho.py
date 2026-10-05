"""Fetch a lightweight orthophoto preview for the playable bbox.

Supports ``sources.ortho.type: wcs`` (GeoTIFF coverage) or ``wms``
(GetMap JPEG/PNG). WMTS layers on MapProxy are reached via the sibling
WMS endpoint with the same layer id (e.g. Südtirol Basemap-Meteo-Aerial).

This is intentionally low-res (default 2048×2048) so it is usable for
color analytics (e.g. rock color clustering) without downloading huge
imagery tiles.

Outputs:
  data/raw/ortho_<site>_<size>.tif   (WCS) or .jpg (WMS)
  data/processed/<site>/ortho.png
  data/processed/<site>/ortho.meta.json

The step is meant to be called from the build pipeline, but it is opt-in:
set ``beamng.rock_color_clusters.enabled: true`` (or run explicitly with
``--force`` / ``--size``).
"""
from __future__ import annotations

import argparse
import io
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


def _as_hwc(arr: np.ndarray) -> np.ndarray:
    a = np.asarray(arr)
    if a.ndim == 2:
        return np.repeat(a[:, :, None], 3, axis=2)
    if a.ndim != 3:
        raise SystemExit(f"Unexpected ortho array shape: {a.shape}")
    if a.shape[2] in (3, 4):
        return a[:, :, :3]
    if a.shape[0] in (3, 4) and a.shape[2] not in (3, 4):
        a = np.transpose(a[:3, :, :], (1, 2, 0))
        return a
    return a[:, :, :3]


def _to_u8(arr: np.ndarray) -> np.ndarray:
    a = np.asarray(arr)
    if a.dtype == np.uint8:
        return a
    if np.issubdtype(a.dtype, np.integer):
        mx = float(np.max(a)) if a.size else 1.0
        if mx <= 255.0:
            return np.clip(a, 0, 255).astype(np.uint8)
        return np.clip(np.rint(a.astype(np.float64) / 257.0), 0, 255).astype(np.uint8)
    finite = np.isfinite(a)
    if not finite.any():
        return np.zeros(a.shape, dtype=np.uint8)
    mn = float(np.percentile(a[finite], 1))
    mx = float(np.percentile(a[finite], 99))
    span = max(mx - mn, 1e-6)
    u = (a.astype(np.float64) - mn) / span * 255.0
    return np.clip(np.rint(u), 0, 255).astype(np.uint8)


def _fetch_wcs(src: dict, *, crs: str, bbox: list[float], size: int) -> tuple[np.ndarray, dict]:
    url = str(src.get("url") or "")
    coverage = str(src.get("coverage") or src.get("layer") or "")
    version = str(src.get("version") or "1.0.0")
    if not url or not coverage:
        raise SystemExit("sources.ortho.url and coverage required for type: wcs")
    xmin, ymin, xmax, ymax = bbox
    params = {
        "SERVICE": "WCS",
        "VERSION": version,
        "REQUEST": "GetCoverage",
        "COVERAGE": coverage,
        "CRS": crs,
        "BBOX": f"{xmin},{ymin},{xmax},{ymax}",
        "WIDTH": str(size),
        "HEIGHT": str(size),
        "FORMAT": "GeoTIFF",
    }
    headers = {"User-Agent": "beamng_autoroad/0.1 (ortho preview)", "Accept": "*/*"}
    print(f"Ortho WCS {coverage} {size}x{size} ...")
    r = requests.get(url, params=params, timeout=600, headers=headers)
    r.raise_for_status()
    arr = tiff.imread(io.BytesIO(r.content))
    rgb = _to_u8(_as_hwc(arr))
    return rgb, {
        "protocol": "wcs",
        "url": url,
        "coverage": coverage,
        "version": version,
        "raw_bytes": len(r.content),
        "raw_format": "geotiff",
    }


def _fetch_wms(src: dict, *, crs: str, bbox: list[float], size: int) -> tuple[np.ndarray, dict]:
    url = str(src.get("url") or "")
    layer = str(src.get("layer") or src.get("coverage") or "")
    version = str(src.get("version") or "1.3.0")
    fmt = str(src.get("format") or "image/jpeg")
    if not url or not layer:
        raise SystemExit("sources.ortho.url and layer required for type: wms")
    xmin, ymin, xmax, ymax = bbox
    # WMS 1.3.0 + EPSG:25832 / projected CRS: axis order is minx,miny,maxx,maxy
    # (same as geographic only for EPSG:4326 etc.). Keep XY for UTM / GK.
    params = {
        "SERVICE": "WMS",
        "VERSION": version,
        "REQUEST": "GetMap",
        "LAYERS": layer,
        "STYLES": str(src.get("styles") or ""),
        "CRS" if version.startswith("1.3") else "SRS": crs,
        "BBOX": f"{xmin},{ymin},{xmax},{ymax}",
        "WIDTH": str(size),
        "HEIGHT": str(size),
        "FORMAT": fmt,
        "TRANSPARENT": "FALSE",
    }
    headers = {"User-Agent": "beamng_autoroad/0.1 (ortho preview)", "Accept": "*/*"}
    print(f"Ortho WMS {layer} {size}x{size} ...")
    r = requests.get(url, params=params, timeout=600, headers=headers)
    r.raise_for_status()
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "xml" in ctype or r.content[:5] == b"<?xml":
        raise SystemExit(
            f"Ortho WMS returned XML/error instead of image "
            f"({ctype}, {len(r.content)} bytes): {r.content[:300]!r}"
        )
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    if img.size != (size, size):
        img = img.resize((size, size), resample=Image.Resampling.BILINEAR)
    rgb = np.asarray(img, dtype=np.uint8)
    return rgb, {
        "protocol": "wms",
        "url": url,
        "layer": layer,
        "version": version,
        "format": fmt,
        "raw_bytes": len(r.content),
        "raw_format": fmt,
        "note": (
            "WMTS layer ids work on the MapProxy WMS endpoint with the same "
            "name; tileMatrixSet is not needed for a single GetMap crop."
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--size", type=int, default=0, help="Preview size in px (square). 0 = YAML/default.")
    ap.add_argument("--force", action="store_true", help="Re-download even if cached.")
    args = ap.parse_args()

    site = load_site()
    slug = site_slug(site)
    proc = processed_dir(site)
    bng = site.get("beamng") or {}
    rc = bng.get("rock_color_clusters") or {}
    enabled = bool(rc.get("enabled", False))
    if not enabled and not args.force and not args.size:
        print("Fetch ortho preview: disabled (set beamng.rock_color_clusters.enabled: true).")
        return

    src = (site.get("sources") or {}).get("ortho") or {}
    kind = str(src.get("type") or "wcs").strip().lower()
    if kind == "wmts":
        # Same MapProxy layers are exposed as WMS; avoid a full tile client.
        kind = "wms"
        print("sources.ortho.type: wmts → using WMS GetMap on the same layer id")

    size = int(args.size or rc.get("ortho_size_px") or 2048)
    size = max(256, min(size, 8192))
    bbox = list(map(float, site["bbox"]))
    crs = str(site.get("crs", "EPSG:31254"))

    raw_ext = ".tif" if kind == "wcs" else ".jpg"
    raw = ROOT / "data" / "raw" / f"ortho_{slug}_{size}{raw_ext}"
    out_png = proc / "ortho.png"
    meta_p = proc / "ortho.meta.json"

    if (
        out_png.is_file()
        and meta_p.is_file()
        and out_png.stat().st_size > 1000
        and not args.force
    ):
        print(f"Using cached ortho preview: {out_png}")
        return

    if kind == "wcs":
        rgb, src_meta = _fetch_wcs(src, crs=crs, bbox=bbox, size=size)
    elif kind == "wms":
        rgb, src_meta = _fetch_wms(src, crs=crs, bbox=bbox, size=size)
    else:
        raise SystemExit(f"sources.ortho.type unsupported: {kind!r} (use wcs or wms)")

    raw.parent.mkdir(parents=True, exist_ok=True)
    proc.mkdir(parents=True, exist_ok=True)
    if kind == "wcs":
        tiff.imwrite(raw, rgb)
    else:
        Image.fromarray(rgb, mode="RGB").save(raw, quality=92)
    Image.fromarray(rgb, mode="RGB").save(out_png)
    meta = {
        "source": f"sources.ortho ({src_meta.get('protocol')})",
        "crs": crs,
        "bbox": bbox,
        "size": size,
        "raw": str(raw.relative_to(ROOT)).replace("\\", "/"),
        "png": str(out_png.relative_to(ROOT)).replace("\\", "/"),
        **src_meta,
    }
    meta_p.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {out_png}")


if __name__ == "__main__":
    main()
