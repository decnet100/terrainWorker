"""Shared WCS GetCoverage download for site terrain sources (DGM, DOM, …)."""
from __future__ import annotations

import json
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]


def terrain_cache_path(site: dict, source_key: str, *, default_stem: str | None = None) -> Path:
    src = (site.get("sources") or {}).get(source_key) or {}
    if src.get("path"):
        p = Path(str(src["path"]))
        return p if p.is_absolute() else ROOT / p
    from site_coords import site_slug  # local import: tools/ on sys.path

    stem = default_stem or source_key
    return ROOT / "data" / "raw" / f"{stem}_{site_slug(site)}.tif"


def fetch_terrain_coverage(
    site: dict,
    source_key: str,
    *,
    force: bool = False,
    label: str | None = None,
    default_stem: str | None = None,
    bbox: list[float] | tuple[float, float, float, float] | None = None,
    resolution_m: float | None = None,
    out_path: Path | None = None,
) -> Path:
    """Download one ``sources.<key>`` WCS coverage into ``data/raw/`` (cached)."""
    src = (site.get("sources") or {}).get(source_key) or {}
    if not src:
        raise SystemExit(f"sources.{source_key} missing in site config")

    out = Path(out_path) if out_path is not None else terrain_cache_path(
        site, source_key, default_stem=default_stem
    )
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    meta_path = out.with_suffix(".meta.json")
    nice = label or source_key.upper()

    bbox = list(map(float, bbox if bbox is not None else site["bbox"]))
    xmin, ymin, xmax, ymax = bbox
    res = float(resolution_m if resolution_m is not None else src.get("resolution_m", 0.5))
    width = max(1, int(round((xmax - xmin) / res)))
    height = max(1, int(round((ymax - ymin) / res)))

    if out.exists() and out.stat().st_size > 1000 and not force:
        print(f"Using cached {nice}: {out} ({out.stat().st_size} bytes)")
        return out

    url = str(src.get("url") or "")
    coverage = str(src.get("coverage") or "")
    version = str(src.get("version") or "1.0.0")
    if not url or not coverage:
        raise SystemExit(f"sources.{source_key}.url and coverage required in site config")

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
    print(f"Downloading {nice} {coverage} BBOX={params['BBOX']} {width}x{height} @ {res}m …")
    headers = {"User-Agent": "beamng_autoroad/0.1 (local test)", "Accept": "*/*"}
    r = requests.get(url, params=params, headers=headers, timeout=600)
    r.raise_for_status()
    ctype = (r.headers.get("content-type") or "").lower()
    if "tiff" not in ctype and not r.content.startswith(b"II") and not r.content.startswith(b"MM"):
        preview = r.content[:400]
        raise SystemExit(f"WCS did not return GeoTIFF (content-type={ctype}): {preview!r}")

    out.write_bytes(r.content)
    meta = {
        "source_key": source_key,
        "url": url,
        "coverage": coverage,
        "version": version,
        "crs": site.get("crs"),
        "bbox": bbox,
        "resolution_m": res,
        "width": width,
        "height": height,
        "bytes": len(r.content),
        "path": str(out.relative_to(ROOT) if out.is_relative_to(ROOT) else out),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {out} ({len(r.content)} bytes)")
    print(f"Wrote {meta_path}")
    return out
