"""Fetch a lightweight orthophoto preview for the playable bbox (WCS).

This is intentionally low-res (default 2048×2048) so it is usable for
color analytics (e.g. rock color clustering) without downloading huge
imagery tiles.

Outputs:
  data/raw/ortho_<site>_<size>.tif
  data/processed/<site>/ortho.png
  data/processed/<site>/ortho.meta.json

The step is meant to be called from the build pipeline, but it is opt-in:
set ``beamng.rock_color_clusters.enabled: true`` (or run explicitly).
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


def _as_hwc(arr: np.ndarray) -> np.ndarray:
    a = np.asarray(arr)
    if a.ndim == 2:
        return np.repeat(a[:, :, None], 3, axis=2)
    if a.ndim != 3:
        raise SystemExit(f"Unexpected ortho array shape: {a.shape}")
    # Common: (H,W,3) or (3,H,W)
    if a.shape[2] in (3, 4):
        return a[:, :, :3]
    if a.shape[0] in (3, 4) and a.shape[2] not in (3, 4):
        a = np.transpose(a[:3, :, :], (1, 2, 0))
        return a
    # Fallback: pick first 3 channels on last axis
    return a[:, :, :3]


def _to_u8(arr: np.ndarray) -> np.ndarray:
    a = np.asarray(arr)
    if a.dtype == np.uint8:
        return a
    # Many WCS return uint16 [0..65535] or float in [0..255].
    if np.issubdtype(a.dtype, np.integer):
        mx = float(np.max(a)) if a.size else 1.0
        if mx <= 255.0:
            return np.clip(a, 0, 255).astype(np.uint8)
        return np.clip(np.rint(a.astype(np.float64) / 257.0), 0, 255).astype(np.uint8)
    # float: assume 0..255, else normalize robustly
    finite = np.isfinite(a)
    if not finite.any():
        return np.zeros(a.shape, dtype=np.uint8)
    mn = float(np.percentile(a[finite], 1))
    mx = float(np.percentile(a[finite], 99))
    span = max(mx - mn, 1e-6)
    u = (a.astype(np.float64) - mn) / span * 255.0
    return np.clip(np.rint(u), 0, 255).astype(np.uint8)


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
    url = str(src.get("url") or "")
    coverage = str(src.get("coverage") or "")
    version = str(src.get("version") or "1.0.0")
    if not url or not coverage:
        print("Fetch ortho preview: sources.ortho missing url/coverage — skip.")
        return

    size = int(args.size or rc.get("ortho_size_px") or 2048)
    size = max(256, min(size, 8192))
    bbox = list(map(float, site["bbox"]))
    xmin, ymin, xmax, ymax = bbox
    crs = str(site.get("crs", "EPSG:31254"))

    raw = ROOT / "data" / "raw" / f"ortho_{slug}_{size}.tif"
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
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(r.content)
    arr = tiff.imread(raw)
    rgb = _to_u8(_as_hwc(arr))
    proc.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb, mode="RGB").save(out_png)
    meta = {
        "source": "sources.ortho (WCS)",
        "url": url,
        "coverage": coverage,
        "version": version,
        "crs": crs,
        "bbox": bbox,
        "size": size,
        "raw": str(raw.relative_to(ROOT)).replace("\\", "/"),
        "png": str(out_png.relative_to(ROOT)).replace("\\", "/"),
    }
    meta_p.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {out_png}")


if __name__ == "__main__":
    main()

