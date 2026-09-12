"""Download DGM via WCS for the active site BBOX (cached).

Default: reuse data/raw/dgm_<site>.tif if present.
Re-download only with --force.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import load_site, site_slug  # noqa: E402


def dgm_cache_path(site: dict) -> Path:
    src = (site.get("sources") or {}).get("dgm") or {}
    if src.get("path"):
        p = Path(str(src["path"]))
        return p if p.is_absolute() else ROOT / p
    return ROOT / "data" / "raw" / f"dgm_{site_slug(site)}.tif"


def fetch_dgm(site: dict, *, force: bool = False) -> Path:
    src = (site.get("sources") or {}).get("dgm") or {}
    out = dgm_cache_path(site)
    out.parent.mkdir(parents=True, exist_ok=True)
    meta_path = out.with_suffix(".meta.json")

    bbox = list(map(float, site["bbox"]))
    xmin, ymin, xmax, ymax = bbox
    res = float(src.get("resolution_m", 0.5))
    width = max(1, int(round((xmax - xmin) / res)))
    height = max(1, int(round((ymax - ymin) / res)))

    if out.exists() and out.stat().st_size > 1000 and not force:
        print(f"Using cached DGM: {out} ({out.stat().st_size} bytes)")
        return out

    url = str(src.get("url") or "")
    coverage = str(src.get("coverage") or "")
    version = str(src.get("version") or "1.0.0")
    if not url or not coverage:
        raise SystemExit("sources.dgm.url and coverage required in site config")

    # ArcGIS WCS 1.0.0 GetCoverage
    params = {
        "SERVICE": "WCS",
        "VERSION": version,
        "REQUEST": "GetCoverage",
        "COVERAGE": coverage,
        "CRS": str(site.get("crs", "EPSG:31254")),
        "BBOX": f"{xmin},{ymin},{xmax},{ymax}",
        "WIDTH": str(width),
        "HEIGHT": str(height),
        "FORMAT": "GeoTIFF",
    }
    print(f"Downloading DGM {coverage} BBOX={params['BBOX']} {width}x{height} @ {res}m …")
    headers = {"User-Agent": "beamng_autoroad/0.1 (local test)", "Accept": "*/*"}
    r = requests.get(url, params=params, headers=headers, timeout=600)
    r.raise_for_status()
    ctype = (r.headers.get("content-type") or "").lower()
    if "tiff" not in ctype and not r.content.startswith(b"II") and not r.content.startswith(b"MM"):
        # Often XML exception
        preview = r.content[:400]
        raise SystemExit(f"WCS did not return GeoTIFF (content-type={ctype}): {preview!r}")

    out.write_bytes(r.content)
    meta = {
        "url": url,
        "coverage": coverage,
        "version": version,
        "crs": site.get("crs"),
        "bbox": bbox,
        "resolution_m": res,
        "width": width,
        "height": height,
        "bytes": len(r.content),
        "path": str(out.relative_to(ROOT)),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {out} ({len(r.content)} bytes)")
    print(f"Wrote {meta_path}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="Re-download even if cache exists")
    args = ap.parse_args()
    site = load_site()
    print(f"Site: {site_slug(site)}")
    fetch_dgm(site, force=args.force)


if __name__ == "__main__":
    main()
