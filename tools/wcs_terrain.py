"""Shared WCS GetCoverage download for site terrain sources (DGM, DOM, …).

Large envelopes are fetched as tiles (``raster_tile_fetch``). Incomplete tiles
(mostly invalid elevation) are re-requested; values are never invented.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import tifffile as tiff

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_TILE_PX = 1024
DEFAULT_TILE_RETRIES = 4
DEFAULT_TILE_RETRY_SLEEP_S = 3.0
DEFAULT_MAX_TILE_NODATA_FRAC = 0.05
DEFAULT_MAX_NODATA_FRAC = 0.02


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
    allow_incomplete: bool = False,
    accept_all_nodata: bool = False,
) -> Path:
    """Download one ``sources.<key>`` WCS coverage into ``data/raw/`` (cached)."""
    from raster_tile_fetch import (  # noqa: WPS433
        elevation_nodata_mask,
        fetch_tiled_mosaic,
        http_get_geotiff,
        make_frac_incomplete,
        read_tiff_bytes,
    )

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

    bbox_l = list(map(float, bbox if bbox is not None else site["bbox"]))
    xmin, ymin, xmax, ymax = bbox_l
    res = float(resolution_m if resolution_m is not None else src.get("resolution_m", 0.5))
    width = max(1, int(round((xmax - xmin) / res)))
    height = max(1, int(round((ymax - ymin) / res)))

    tile_px = int(src.get("tile_px", DEFAULT_TILE_PX))
    tile_retries = int(src.get("tile_retries", DEFAULT_TILE_RETRIES))
    tile_retry_sleep_s = float(src.get("tile_retry_sleep_s", DEFAULT_TILE_RETRY_SLEEP_S))
    max_tile_nodata = float(src.get("max_tile_nodata_frac", DEFAULT_MAX_TILE_NODATA_FRAC))
    max_nodata = float(src.get("max_nodata_frac", DEFAULT_MAX_NODATA_FRAC))

    if out.exists() and out.stat().st_size > 1000 and not force:
        arr = np.asarray(tiff.imread(out), dtype=np.float64)
        if arr.ndim == 3:
            arr = arr[..., 0]
        bad = float(elevation_nodata_mask(arr).mean())
        if bad <= max_nodata:
            print(f"Using cached {nice}: {out} ({out.stat().st_size} bytes)")
            return out
        print(f"Cached {nice} incomplete (bad={100*bad:.2f}%) - refetching tiles")

    url = str(src.get("url") or "")
    coverage = str(src.get("coverage") or "")
    version = str(src.get("version") or "1.0.0")
    if not url or not coverage:
        raise SystemExit(f"sources.{source_key}.url and coverage required in site config")

    crs = str(site.get("crs", "EPSG:31254"))
    headers = {"User-Agent": "beamng_autoroad/0.1 (local test)", "Accept": "*/*"}

    def _download_tile(spec) -> np.ndarray:
        params = {
            "SERVICE": "WCS",
            "VERSION": version,
            "REQUEST": "GetCoverage",
            "COVERAGE": coverage,
            "CRS": crs,
            "BBOX": f"{spec.xmin},{spec.ymin},{spec.xmax},{spec.ymax}",
            "WIDTH": str(spec.width_px),
            "HEIGHT": str(spec.height_px),
            "FORMAT": "GeoTIFF",
        }
        data = http_get_geotiff(url, params, timeout_s=600, headers=headers)
        return read_tiff_bytes(data)

    tile_dir = out.with_name(out.stem + "_tiles")
    print(
        f"Downloading {nice} {coverage} BBOX={xmin},{ymin},{xmax},{ymax} "
        f"{width}x{height} @ {res}m (tiles {tile_px}px) …"
    )
    mosaic, tile_stats = fetch_tiled_mosaic(
        bbox=(xmin, ymin, xmax, ymax),
        width_px=width,
        height_px=height,
        tile_px=tile_px,
        download_tile=_download_tile,
        is_incomplete=make_frac_incomplete(
            elevation_nodata_mask,
            max_tile_nodata,
            accept_all_nodata=accept_all_nodata,
        ),
        fill_value=np.nan,
        dtype=np.float32,
        retries=tile_retries,
        retry_sleep_s=tile_retry_sleep_s,
        tile_cache_dir=tile_dir,
        force=force,
        label=nice,
    )

    # Persist finite float32; NaN stays for remaining holes.
    tiff.imwrite(out, mosaic.astype(np.float32), compression="deflate")
    bad_frac = float(elevation_nodata_mask(mosaic).mean())
    meta = {
        "source_key": source_key,
        "url": url,
        "coverage": coverage,
        "version": version,
        "crs": site.get("crs"),
        "bbox": bbox_l,
        "resolution_m": res,
        "width": width,
        "height": height,
        "bytes": out.stat().st_size,
        "tiled": True,
        "tile_stats": tile_stats,
        "nodata_frac": bad_frac,
        "nodata_pct": round(100.0 * bad_frac, 3),
        "path": str(out.relative_to(ROOT) if out.is_relative_to(ROOT) else out),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {out} ({out.stat().st_size} bytes)")
    print(f"Wrote {meta_path}")

    if bad_frac > max_nodata:
        msg = (
            f"WARNING: {nice} incomplete - bad elevation={100*bad_frac:.2f}% "
            f"(limit {100*max_nodata:.2f}%) at {out}"
        )
        print(msg)
        if not allow_incomplete:
            raise SystemExit(
                f"{nice} nodata {100*bad_frac:.2f}% exceeds max_nodata_frac "
                f"{100*max_nodata:.2f}%. Re-run with --force or raise tile_retries."
            )
    return out
