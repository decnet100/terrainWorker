"""Fetch BEV INSPIRE Land Cover raster via tiled WMS GetMap (GeoTIFF).

Source: https://data.bev.gv.at/geoserver/INSdataLC/wms
Layer: LC.LandCoverRaster — AT Gesamtmosaik LC 2021–2023, 6 Klassen
  0 Vegetation hoch
  1 Vegetation mittel
  2 Vegetation niedrig
  3 Bodenflächen
  4 Gebäude
  5 Gewässer

WCS is disabled on this GeoServer; WMS ``image/geotiff`` returns classified
uint8. WMS 1.3.0 bbox order follows the CRS axis: northing first for
EPSG:31254, easting first for EPSG:25832. Tiles that miss Austria are not
requested. A tile on the border is requested once; nodata there is the
foreign side and is not retried.

Downloads in tiles (see ``tools/raster_tile_fetch.py``). Incomplete tiles
(class 6 nodata) are re-requested; holes are never invented.

Site YAML (``sources.bev_landcover``):
  tile_px: 1024
  tile_retries: 4
  tile_retry_sleep_s: 3
  max_nodata_frac: 0.02
  max_tile_nodata_frac: 0.05

Writes:
  data/raw/bev_landcover_<site>.tif (+ .meta.json)
  data/raw/bev_landcover_<site>_tiles/   (per-tile cache)
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
import tifffile as tiff
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from raster_tile_fetch import (  # noqa: E402
    TileSpec,
    build_tile_grid,
    classified_nodata_mask,
    fetch_tiled_mosaic,
    http_get_geotiff,
    make_frac_incomplete,
    read_tiff_bytes,
)
from site_coords import load_site, processed_dir, site_slug  # noqa: E402

DEFAULT_WMS = "https://data.bev.gv.at/geoserver/INSdataLC/ows"
DEFAULT_LAYER = "LC.LandCoverRaster"
NODATA_CODE = 6
DEFAULT_MAX_NODATA_FRAC = 0.02
DEFAULT_MAX_TILE_NODATA_FRAC = 0.05
DEFAULT_TILE_PX = 1024
DEFAULT_TILE_RETRIES = 4
DEFAULT_TILE_RETRY_SLEEP_S = 3.0

CLASS_NAMES = {
    0: "vegetation_hoch",
    1: "vegetation_mittel",
    2: "vegetation_niedrig",
    3: "bodenflaechen",
    4: "gebaeude",
    5: "gewaesser",
    6: "nodata",
}

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
        "timeout_s": int(src.get("timeout_s") or 120),
        "max_nodata_frac": float(src.get("max_nodata_frac", DEFAULT_MAX_NODATA_FRAC)),
        "max_tile_nodata_frac": float(
            src.get("max_tile_nodata_frac", DEFAULT_MAX_TILE_NODATA_FRAC)
        ),
        "tile_px": int(src.get("tile_px", DEFAULT_TILE_PX)),
        "tile_retries": int(
            src.get("tile_retries", src.get("retries", DEFAULT_TILE_RETRIES))
        ),
        "tile_retry_sleep_s": float(
            src.get("tile_retry_sleep_s", src.get("retry_sleep_s", DEFAULT_TILE_RETRY_SLEEP_S))
        ),
    }


def nodata_fraction(codes: np.ndarray) -> float:
    return float(classified_nodata_mask(codes, NODATA_CODE).mean())


def _nodata_is_outside_austria(meta: dict) -> bool:
    """Skipped tiles and no failed downloads: the holes are outside Austria."""
    stats = meta.get("tile_stats") or {}
    return int(stats.get("tiles_failed") or 0) == 0 and int(stats.get("tiles_skipped") or 0) > 0


def report_incomplete_landcover(
    codes: np.ndarray,
    *,
    where: str,
    max_nodata_frac: float,
    allow_incomplete: bool = False,
    outside_austria: bool = False,
) -> float:
    frac = nodata_fraction(codes)
    pct = 100.0 * frac
    limit_pct = 100.0 * max_nodata_frac
    if outside_austria:
        print(
            f"BEV landcover nodata={pct:.2f}% outside Austria, kept ({where})",
            flush=True,
        )
        return frac
    if frac <= max_nodata_frac:
        if frac > 0:
            print(
                f"BEV landcover nodata={pct:.2f}% (limit {limit_pct:.2f}%) - OK ({where})"
            )
        return frac
    msg = (
        f"WARNING: BEV landcover incomplete - nodata={pct:.2f}% "
        f"(limit {limit_pct:.2f}%) at {where}.\n"
        "  Rectangular holes usually mean WMS tiles still failed after retries.\n"
        "  Re-run: python tools\\fetch_bev_landcover.py --force\n"
        "  Do not invent fill for those pixels."
    )
    print(msg, file=sys.stderr)
    if not allow_incomplete:
        raise SystemExit(
            f"BEV landcover nodata {pct:.2f}% exceeds max_nodata_frac "
            f"{limit_pct:.2f}%. Pass --allow-incomplete to continue anyway."
        )
    print(
        "  Continuing because --allow-incomplete / allow_incomplete was set.",
        file=sys.stderr,
    )
    return frac


def _raw_paths(site: dict) -> tuple[Path, Path]:
    slug = site_slug(site)
    raw = ROOT / "data" / "raw"
    return raw / f"bev_landcover_{slug}.tif", raw / f"bev_landcover_{slug}.meta.json"


def _pixel_size(
    bbox_xy: tuple[float, float, float, float], size: int
) -> tuple[int, int]:
    xmin, ymin, xmax, ymax = (float(v) for v in bbox_xy)
    width_m = max(xmax - xmin, 1.0)
    height_m = max(ymax - ymin, 1.0)
    if width_m >= height_m:
        w = int(size)
        h = max(1, int(round(size * height_m / width_m)))
    else:
        h = int(size)
        w = max(1, int(round(size * width_m / height_m)))
    return w, h


def _wms13_north_first(crs: str) -> bool:
    """True when the first CRS axis is northing, so a WMS 1.3.0 bbox leads with y."""
    from pyproj import CRS

    code = str(crs).split(":")[-1]
    direction = str(CRS.from_epsg(int(code)).axis_info[0].direction or "")
    return direction.lower().startswith("north")


def _wms13_bbox(spec: TileSpec, *, north_first: bool) -> str:
    if north_first:
        return f"{spec.ymin},{spec.xmin},{spec.ymax},{spec.xmax}"
    return f"{spec.xmin},{spec.ymin},{spec.xmax},{spec.ymax}"


def _austria_wgs84():
    """Union of the Austrian NUTS level-2 polygons."""
    from shapely.ops import unary_union

    from authorities import load_nuts_features

    parts = [
        geom
        for nid, _name, geom in load_nuts_features(download=True)
        if str(nid).startswith("AT")
    ]
    if not parts:
        raise SystemExit("Austrian NUTS polygons are missing")
    return unary_union(parts)


def _tile_vs_austria(spec: TileSpec, to_wgs, austria) -> str:
    """``out`` misses Austria, ``in`` lies inside, ``edge`` crosses the border."""
    from shapely.geometry import Polygon

    lon, lat = to_wgs.transform(
        [spec.xmin, spec.xmax, spec.xmax, spec.xmin],
        [spec.ymin, spec.ymin, spec.ymax, spec.ymax],
    )
    poly = Polygon(list(zip(lon, lat)))
    if poly.is_empty or not austria.intersects(poly):
        return "out"
    if austria.covers(poly):
        return "in"
    return "edge"


def _download_bev_tile(site: dict, cfg: dict, spec, *, north_first: bool) -> np.ndarray:
    import logging

    crs = str(site.get("crs", "EPSG:31254"))
    params = {
        "SERVICE": "WMS",
        "VERSION": "1.3.0",
        "REQUEST": "GetMap",
        "LAYERS": cfg["layer"],
        "STYLES": "",
        "CRS": crs,
        "BBOX": _wms13_bbox(spec, north_first=north_first),
        "WIDTH": str(spec.width_px),
        "HEIGHT": str(spec.height_px),
        "FORMAT": "image/geotiff",
        "TRANSPARENT": "FALSE",
    }
    data = http_get_geotiff(cfg["url"], params, timeout_s=cfg["timeout_s"])
    # GeoServer writes GDAL_NODATA as '6.0'. tifffile warns and still reads the classes.
    log = logging.getLogger("tifffile")
    level = log.level
    log.setLevel(logging.ERROR)
    try:
        return read_tiff_bytes(data)
    finally:
        log.setLevel(level)


def fetch_bev_landcover_bbox(
    site: dict,
    bbox_xy: tuple[float, float, float, float],
    dest: Path,
    *,
    size: int,
    force: bool = False,
    allow_incomplete: bool = False,
    accept_all_nodata: bool = False,
) -> Path:
    """Tiled WMS GetMap for one envelope in the site CRS."""
    cfg = _cfg(site)
    cfg["size"] = int(size)
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta_path = dest.with_suffix(".meta.json")
    xmin, ymin, xmax, ymax = (float(v) for v in bbox_xy)
    w, h = _pixel_size(bbox_xy, int(size))

    if dest.is_file() and dest.stat().st_size > 1000 and meta_path.is_file() and not force:
        codes = _load_classified(dest)
        try:
            cached_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cached_meta = {}
        if nodata_fraction(codes) <= cfg["max_nodata_frac"] or _nodata_is_outside_austria(
            cached_meta
        ):
            print(f"Using cached BEV landcover: {dest} ({dest.stat().st_size} bytes)")
            return dest
        print(
            f"Cached BEV landcover incomplete "
            f"(nodata={100*nodata_fraction(codes):.2f}%) - refetching tiles"
        )

    tile_dir = dest.with_name(dest.stem + "_tiles")
    crs = str(site.get("crs", "EPSG:31254"))
    north_first = _wms13_north_first(crs)
    tiles = build_tile_grid((xmin, ymin, xmax, ymax), w, h, cfg["tile_px"])
    from pyproj import Transformer

    to_wgs = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    austria = _austria_wgs84()
    outside: set[tuple[int, int]] = set()
    border: set[tuple[int, int]] = set()
    for spec in tiles:
        kind = _tile_vs_austria(spec, to_wgs, austria)
        key = (spec.iy, spec.ix)
        if kind == "out":
            outside.add(key)
        elif kind == "edge":
            border.add(key)
    print(
        f"BEV landcover: {len(outside)} tiles outside Austria skipped, "
        f"{len(border)} on the border",
        flush=True,
    )
    mosaic, tile_stats = fetch_tiled_mosaic(
        bbox=(xmin, ymin, xmax, ymax),
        width_px=w,
        height_px=h,
        tile_px=cfg["tile_px"],
        download_tile=lambda spec: _download_bev_tile(
            site, cfg, spec, north_first=north_first
        ),
        is_incomplete=make_frac_incomplete(
            lambda a: classified_nodata_mask(a, NODATA_CODE),
            cfg["max_tile_nodata_frac"],
            accept_all_nodata=accept_all_nodata,
        ),
        skip_tile=lambda spec: (spec.iy, spec.ix) in outside,
        keep_incomplete=lambda spec: (spec.iy, spec.ix) in border,
        fill_value=NODATA_CODE,
        dtype=np.uint8,
        retries=cfg["tile_retries"],
        retry_sleep_s=cfg["tile_retry_sleep_s"],
        tile_cache_dir=tile_dir,
        force=force,
        label="BEV landcover",
    )
    tiff.imwrite(dest, mosaic, compression="deflate")
    frac = nodata_fraction(mosaic)
    meta = {
        "url": cfg["url"],
        "layer": cfg["layer"],
        "crs": site.get("crs"),
        "bbox": [xmin, ymin, xmax, ymax],
        "size": [w, h],
        "format": "image/geotiff",
        "bytes": dest.stat().st_size,
        "classes": CLASS_NAMES,
        "mosaic": "AT_Gesamtmosaik_LC_2021-2023",
        "tiled": True,
        "tile_stats": tile_stats,
        "nodata_frac": frac,
        "nodata_pct": round(100.0 * frac, 3),
        "path": str(dest.relative_to(ROOT)).replace("\\", "/"),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {dest} ({dest.stat().st_size} bytes)")
    print(f"Wrote {meta_path}")
    report_incomplete_landcover(
        mosaic,
        where=str(dest),
        max_nodata_frac=cfg["max_nodata_frac"],
        allow_incomplete=allow_incomplete,
        outside_austria=_nodata_is_outside_austria(meta),
    )
    return dest


def fetch_bev_landcover(
    site: dict,
    *,
    force: bool = False,
    allow_incomplete: bool = False,
) -> Path:
    cfg = _cfg(site)
    out, _meta = _raw_paths(site)
    xmin, ymin, xmax, ymax = map(float, site["bbox"])
    return fetch_bev_landcover_bbox(
        site,
        (xmin, ymin, xmax, ymax),
        out,
        size=cfg["size"],
        force=force,
        allow_incomplete=allow_incomplete,
    )


def fetch_bev_near(site: dict, *, force: bool = False, size: int | None = None) -> Path:
    """BEV mosaic for the backdrop near envelope (AT only; CH/IT stay nodata)."""
    from fetch_backdrop import backdrop_cfg, near_extent  # noqa: WPS433

    slug = site_slug(site)
    dest = ROOT / "data" / "raw" / f"bev_landcover_near_{slug}.tif"
    nsize = int(size or backdrop_cfg(site).get("near_texture_size") or 2048)
    # Near ring crosses borders — allow nodata outside AT.
    return fetch_bev_landcover_bbox(
        site,
        near_extent(site),
        dest,
        size=nsize,
        force=force,
        allow_incomplete=True,
        accept_all_nodata=True,
    )


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
    allow = tag in ("near", "mid", "far", "custom")
    return fetch_bev_landcover_bbox(
        site,
        extent,
        dest,
        size=size,
        force=force,
        allow_incomplete=allow,
        accept_all_nodata=allow,
    )


def _extent_close(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
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
        path = fetch_bev_near(
            site, force=force, size=int(cfg.get("near_texture_size") or dest_shape[0])
        )
    elif _extent_close(ext, mid_extent(site)):
        path = fetch_bev_extent(
            site,
            ext,
            "mid",
            size=int(cfg.get("mid_texture_size") or dest_shape[0]),
            force=force,
        )
    elif _extent_close(ext, padded_extent(site)):
        path = fetch_bev_extent(
            site,
            ext,
            "far",
            size=int(cfg.get("texture_size") or dest_shape[0]),
            force=force,
        )
    else:
        path = fetch_bev_extent(
            site, ext, "custom", size=int(dest_shape[0]), force=force
        )

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
        raise SystemExit(
            f"Expected classified single-band GeoTIFF, got shape={arr.shape}. "
            "Check WMS BBOX axis order / FORMAT."
        )
    return arr.astype(np.uint8, copy=False)


def _preview_rgb(codes: np.ndarray, path: Path) -> None:
    h, w = codes.shape
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    for code, col in CLASS_RGB.items():
        rgb[codes == code] = col
    known = np.zeros(codes.shape, dtype=bool)
    for c in CLASS_RGB:
        known |= codes == c
    rgb[~known] = (0, 0, 0)
    Image.fromarray(rgb, mode="RGB").save(path)
    print(f"Wrote {path}")


def _export_processed(
    site: dict,
    raw_path: Path,
    *,
    allow_incomplete: bool = False,
) -> None:
    proc = processed_dir(site)
    codes = _load_classified(raw_path)
    size = int((site.get("beamng") or {}).get("mask_size") or codes.shape[0])
    if codes.shape != (size, size):
        img = Image.fromarray(codes, mode="L")
        img = img.resize((size, size), resample=Image.Resampling.NEAREST)
        codes = np.asarray(img, dtype=np.uint8)
        print(f"Resampled landcover -> {size}x{size}")

    cfg = _cfg(site)
    meta_path = raw_path.with_suffix(".meta.json")
    try:
        raw_meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    except json.JSONDecodeError:
        raw_meta = {}
    nodata_frac = report_incomplete_landcover(
        codes,
        where=str(raw_path),
        max_nodata_frac=cfg["max_nodata_frac"],
        allow_incomplete=allow_incomplete,
        outside_austria=_nodata_is_outside_austria(raw_meta),
    )

    tiff.imwrite(proc / "landcover_bev.tif", codes, compression="deflate")
    _preview_rgb(codes, proc / "preview_landcover_bev.png")
    hist = {
        CLASS_NAMES.get(int(v), str(int(v))): float((codes == v).mean())
        for v in np.unique(codes)
    }
    meta = {
        "site": site_slug(site),
        "source": "bev_inspire_wms",
        "raw": str(raw_path.relative_to(ROOT)),
        "classes": CLASS_NAMES,
        "histogram": hist,
        "nodata_frac": nodata_frac,
        "nodata_pct": round(100.0 * nodata_frac, 3),
        "max_nodata_frac": cfg["max_nodata_frac"],
        "incomplete": nodata_frac > cfg["max_nodata_frac"],
        "tiled": True,
        "files": ["landcover_bev.tif", "preview_landcover_bev.png"],
    }
    (proc / "landcover_bev.meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    print(f"Wrote {proc / 'landcover_bev.tif'}")
    print(f"Wrote {proc / 'landcover_bev.meta.json'}")
    for k, v in sorted(hist.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v*100:.2f}%")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="Re-download even if cache exists")
    ap.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Continue even if nodata share exceeds max_nodata_frac (still warns)",
    )
    args = ap.parse_args()
    site = load_site()
    print(f"Site: {site_slug(site)}")
    raw = fetch_bev_landcover(
        site, force=args.force, allow_incomplete=args.allow_incomplete
    )
    _export_processed(site, raw, allow_incomplete=args.allow_incomplete)


if __name__ == "__main__":
    main()
