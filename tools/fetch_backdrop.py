"""Fetch far-field DEM (+ optional WorldCover) for the site backdrop mesh.

DEM: Copernicus GLO-30 public COGs (AWS, ~30 m, no account). Warped to the
site CRS at ``beamng.backdrop.viewshed_step_m``. EEA-10 can replace this later.

Ortho: SWISSIMAGE Hintergrund (ch.swisstopo.swissimage) via geo.admin.ch WMS.
WorldCover remains a fallback tint if the WMS fails.

Writes:
  data/raw/copernicus_glo30/<tile>.tif
  data/raw/worldcover/<tile>.tif
  data/processed/<site>/backdrop_dem.tif + .meta.json
  data/processed/<site>/backdrop_worldcover.tif   (optional, same grid)
  data/processed/<site>/backdrop_ortho.png        (SWISSIMAGE, site grid)

Usage:
  cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = \"config/sites/fernpass_mega.yaml\"; python tools\\fetch_backdrop.py
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests
import tifffile as tiff
from PIL import Image
from pyproj import Transformer
from scipy.ndimage import distance_transform_edt, map_coordinates

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import load_site, processed_dir, site_slug  # noqa: E402
from wcs_terrain import fetch_terrain_coverage  # noqa: E402

GLO30_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"
SWISSIMAGE_WMS = "https://wms.geo.admin.ch/"
SWISSIMAGE_LAYER = "ch.swisstopo.swissimage"  # title: SWISSIMAGE Hintergrund
UA = {"User-Agent": "beamng_autoroad/0.1 (local backdrop fetch)"}
NODATA = -32767.0

# ESA WorldCover 2021 v200 tiles are 3° × 3°.
WORLDCOVER_ITEM = "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
PC_ITEM = "https://planetarycomputer.microsoft.com/api/stac/v1/collections/esa-worldcover/items/ESA_WorldCover_10m_2021_v200_{tile}"
PC_SIGN = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"
WORLDCOVER_URLS = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/" + WORLDCOVER_ITEM,
    "https://ai4edataeuwest.blob.core.windows.net/esa-worldcover/v200/2021/map/" + WORLDCOVER_ITEM,
)


@dataclass
class GeoRaster:
    data: np.ndarray
    origin_x: float
    origin_y: float
    px: float
    py: float  # negative when north-up
    crs: str


def backdrop_cfg(site: dict) -> dict:
    raw = ((site.get("beamng") or {}).get("backdrop") or {})
    radius = float(raw.get("radius_m", 40000.0))
    step = float(raw.get("viewshed_step_m", 50.0))
    return {
        "radius_m": radius,
        "viewshed_step_m": step,
        "observer_grid": int(raw.get("observer_grid", 5)),
        "observer_z_extra_m": float(raw.get("observer_z_extra_m", 10.0)),
        "min_views": int(raw.get("min_views", 1)),
        "mesh_step_m": float(raw.get("mesh_step_m", 120.0)),
        "hole_pad_m": float(raw.get("hole_pad_m", 30.0)),
        "near_m": float(raw.get("near_m", 2000.0)),
        "near_step_m": float(raw.get("near_step_m", 5.0)),
        "near_dgm_res_m": float(raw.get("near_dgm_res_m", 2.0)),
        "near_overlap_m": float(raw.get("near_overlap_m", 80.0)),
        "near_lip_drop_m": float(raw.get("near_lip_drop_m", 1.0)),
        "near_texture_size": int(raw.get("near_texture_size", 2048)),
        "near_normal_size": int(raw.get("near_normal_size") or raw.get("near_texture_size") or 2048),
        "near_tiles": int(raw.get("near_tiles", 9)),
        "near_canopy": bool(raw.get("near_canopy", True)),
        "near_canopy_min_m": float(raw.get("near_canopy_min_m", 1.0)),
        "near_canopy_spike_m": float(raw.get("near_canopy_spike_m", 8.0)),
        "near_canopy_median_px": int(raw.get("near_canopy_median_px", 5)),
        "near_canopy_min_area_m2": float(raw.get("near_canopy_min_area_m2", 40.0)),
        "mid_m": float(raw.get("mid_m", 10000.0)),
        "mid_step_m": float(raw.get("mid_step_m", 15.0)),
        "mid_dgm_res_m": float(raw.get("mid_dgm_res_m", 8.0)),
        "mid_canopy": bool(raw.get("mid_canopy", raw.get("near_canopy", True))),
        "mid_overlap_m": float(raw.get("mid_overlap_m", 150.0)),
        "mid_texture_size": int(raw.get("mid_texture_size", 2048)),
        "mid_tiles": int(raw.get("mid_tiles", 8)),
        "n_rays": int(raw.get("n_rays", 2048)),
        "curvature_cc": float(raw.get("curvature_cc", 0.85714)),
        # Fill internal viewshed holes up to this width (50 m cells: 600 m → 6 close iters).
        "viewshed_close_m": float(raw.get("viewshed_close_m", 600.0)),
        # Extra outer pad after closing (old path was 4×50 m dilation, no close).
        "viewshed_pad_m": float(raw.get("viewshed_pad_m", 100.0)),
        # Fill N/S and E/W channels outside the mid hole where keep exists on both sides.
        "viewshed_stitch_m": float(raw.get("viewshed_stitch_m", 2500.0)),
        "texture_size": int(raw.get("texture_size", 2048)),
        "albedo_gain": float(raw.get("albedo_gain", 0.58)),
        # >1 darkens midtones of the ortho (probe the pale-green seam).
        "ortho_gamma": float(raw.get("ortho_gamma", 1.0)),
        # 0 = flat albedo (engine + mesh/normal do the light). Old bake was 0.38.
        "ortho_hillshade": float(raw.get("ortho_hillshade", 0.10)),
        "normal_strength": float(raw.get("normal_strength", 0.32)),
        # none | playable_olive (D): pull Swissimage greens toward dry_meadow olive
        "ortho_grade": str(raw.get("ortho_grade") or "none").strip().lower(),
        "olive_mix": float(raw.get("olive_mix", 0.68)),
        "lip_fade_m": float(raw.get("lip_fade_m", 800.0)),
        "lip_olive": float(raw.get("lip_olive", 0.40)),
        "lip_darken": float(raw.get("lip_darken", 0.12)),
        "debug_lip_stripe": bool(raw.get("debug_lip_stripe", False)),
        "debug_green_black": bool(raw.get("debug_green_black", False)),
        "snow_tint": bool(raw.get("snow_tint", True)),
        "detect_grade": bool(raw.get("detect_grade", False)),
        "forest_detect": str(raw.get("forest_detect") or "green").strip().lower(),
        "grass_cut": float(raw.get("grass_cut", 0.5)),
        "grass_mix": float(raw.get("grass_mix", 0.55)),
        "grass_dark": float(raw.get("grass_dark", 0.4)),
        "forest_mix": float(raw.get("forest_mix", raw.get("grass_mix", 0.55))),
        "forest_dark": float(raw.get("forest_dark", raw.get("grass_dark", 0.4))),
        "forest_olive": [float(v) for v in (raw.get("forest_olive") or [72, 88, 52])],
        "snow_cut": float(raw.get("snow_cut", 0.40)),
        "snow_mix": float(raw.get("snow_mix", 0.65)),
        "base_color": [float(v) for v in (raw.get("base_color") or [1.0, 1.0, 1.0])],
        "extra_peaks": int(raw.get("extra_peaks", 4)),
    }


def site_center(site: dict) -> tuple[float, float]:
    xmin, ymin, xmax, ymax = map(float, site["bbox"])
    return 0.5 * (xmin + xmax), 0.5 * (ymin + ymax)


def padded_extent(site: dict) -> tuple[float, float, float, float]:
    cfg = backdrop_cfg(site)
    cx, cy = site_center(site)
    r = cfg["radius_m"]
    return cx - r, cy - r, cx + r, cy + r


def _extent_matches(
    meta: dict | None,
    extent: tuple[float, float, float, float],
    *,
    tol_m: float = 30.0,
) -> bool:
    if not meta:
        return False
    stored = meta.get("request_bbox") or meta.get("extent") or meta.get("bbox")
    if not stored or len(stored) != 4:
        return False
    return all(abs(float(a) - float(b)) <= tol_m for a, b in zip(stored, extent))


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def near_extent(site: dict) -> tuple[float, float, float, float]:
    cfg = backdrop_cfg(site)
    bx0, by0, bx1, by1 = map(float, site["bbox"])
    m = float(cfg["near_m"]) + 200.0
    return bx0 - m, by0 - m, bx1 + m, by1 + m


def mid_extent(site: dict) -> tuple[float, float, float, float]:
    cfg = backdrop_cfg(site)
    bx0, by0, bx1, by1 = map(float, site["bbox"])
    m = float(cfg["mid_m"]) + 200.0
    return bx0 - m, by0 - m, bx1 + m, by1 + m


def dem_paths(site: dict) -> tuple[Path, Path]:
    proc = processed_dir(site)
    return proc / "backdrop_dem.tif", proc / "backdrop_dem.meta.json"


def worldcover_path(site: dict) -> Path:
    return processed_dir(site) / "backdrop_worldcover.tif"


def ortho_path(site: dict) -> Path:
    return processed_dir(site) / "backdrop_ortho.png"


def near_ortho_path(site: dict) -> Path:
    return processed_dir(site) / "backdrop_near_ortho.png"


def mid_ortho_path(site: dict) -> Path:
    return processed_dir(site) / "backdrop_mid_ortho.png"


def near_dgm_paths(site: dict) -> tuple[Path, Path]:
    return near_raster_paths(site, "dgm")


def near_dom_paths(site: dict) -> tuple[Path, Path]:
    return near_raster_paths(site, "dom")


def ring_raster_paths(site: dict, ring: str, kind: str) -> tuple[Path, Path]:
    proc = processed_dir(site)
    return proc / f"backdrop_{ring}_{kind}.tif", proc / f"backdrop_{ring}_{kind}.meta.json"


def near_raster_paths(site: dict, kind: str) -> tuple[Path, Path]:
    return ring_raster_paths(site, "near", kind)


def mid_raster_paths(site: dict, kind: str) -> tuple[Path, Path]:
    return ring_raster_paths(site, "mid", kind)


def _read_geotiff(path: Path, *, max_dim: int | None = None) -> GeoRaster:
    with tiff.TiffFile(path) as tf:
        series = tf.series[0]
        levels = list(getattr(series, "levels", None) or [series])
        full = levels[0]
        chosen = full
        if max_dim is not None:
            for lev in levels:
                if max(int(lev.shape[0]), int(lev.shape[1])) <= max_dim:
                    chosen = lev
                    break
            else:
                chosen = levels[-1]
        page = chosen.pages[0] if getattr(chosen, "pages", None) else tf.pages[0]
        scale = page.tags.get("ModelPixelScaleTag")
        tie = page.tags.get("ModelTiepointTag")
        if scale is None or tie is None:
            # Overviews often inherit tags only on page 0.
            p0 = tf.pages[0]
            scale = scale or p0.tags.get("ModelPixelScaleTag")
            tie = tie or p0.tags.get("ModelTiepointTag")
        if scale is None or tie is None:
            raise SystemExit(f"Missing GeoTIFF tags: {path}")
        sx, sy, _sz = scale.value[:3]
        _i, _j, _k, x0, y0, _z = tie.value[:6]
        origin_x = float(x0 - _i * sx)
        origin_y = float(y0 + _j * sy)
        data = np.asarray(chosen.asarray())
        if data.ndim == 3:
            data = data[..., 0]
        fh, fw = int(full.shape[0]), int(full.shape[1])
        h, w = int(data.shape[0]), int(data.shape[1])
        if (h, w) != (fh, fw):
            sx = float(sx) * fw / max(w, 1)
            sy = float(sy) * fh / max(h, 1)
        epsg = _epsg_from_page(tf.pages[0])
    return GeoRaster(
        data=data,
        origin_x=origin_x,
        origin_y=origin_y,
        px=float(sx),
        py=-float(sy),
        crs=f"EPSG:{epsg}" if epsg else "EPSG:4326",
    )


def _epsg_from_page(page) -> int | None:
    gk = page.tags.get("GeoKeyDirectoryTag")
    if gk is None:
        return None
    vals = list(gk.value)
    n = int(vals[3]) if len(vals) > 3 else 0
    geographic = projected = None
    for i in range(n):
        key, loc, _count, val = vals[4 + i * 4 : 8 + i * 4]
        if int(loc) != 0:
            continue
        if int(key) == 3072:
            projected = int(val)
        elif int(key) == 2048:
            geographic = int(val)
    return projected or geographic


def _download(url: str, dest: Path, *, timeout: tuple[int, int] = (15, 120)) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    print(f"GET {url}")
    try:
        r = requests.get(url, stream=True, timeout=timeout, headers=UA)
        r.raise_for_status()
    except Exception as ex:  # noqa: BLE001
        print(f"  skip: {type(ex).__name__}: {ex}")
        return False
    total = int(r.headers.get("Content-Length") or 0)
    n = 0
    with part.open("wb") as f:
        for chunk in r.iter_content(1024 * 1024):
            if not chunk:
                continue
            f.write(chunk)
            n += len(chunk)
            if total and n % (8 * 1024 * 1024) < 1024 * 1024:
                print(f"  {n / 1e6:.1f}/{total / 1e6:.1f} MB")
    if n < 1000:
        part.unlink(missing_ok=True)
        print("  skip: empty body")
        return False
    part.replace(dest)
    print(f"  wrote {dest.name} ({n / 1e6:.1f} MB)")
    return True


def glo30_tile_stem(lat_sw: int, lon_sw: int) -> str:
    ns = "N" if lat_sw >= 0 else "S"
    ew = "E" if lon_sw >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat_sw):02d}_00_{ew}{abs(lon_sw):03d}_00_DEM"


def _degree_tiles(min_v: float, max_v: float) -> range:
    a = math.floor(min_v)
    b = math.floor(max_v - 1e-12)
    return range(a, b + 1)


def _wgs84_bbox(
    site: dict, extent: tuple[float, float, float, float] | None = None
) -> tuple[float, float, float, float]:
    xmin, ymin, xmax, ymax = extent if extent is not None else padded_extent(site)
    to_ll = Transformer.from_crs(site.get("crs", "EPSG:31254"), "EPSG:4326", always_xy=True)
    corners = [
        to_ll.transform(xmin, ymin),
        to_ll.transform(xmin, ymax),
        to_ll.transform(xmax, ymin),
        to_ll.transform(xmax, ymax),
    ]
    lons = [c[0] for c in corners]
    lats = [c[1] for c in corners]
    pad = 0.02
    return min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad


def fetch_glo30_tiles(site: dict, *, force: bool = False) -> list[Path]:
    west, south, east, north = _wgs84_bbox(site)
    raw = ROOT / "data" / "raw" / "copernicus_glo30"
    raw.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for lat in _degree_tiles(south, north):
        for lon in _degree_tiles(west, east):
            stem = glo30_tile_stem(lat, lon)
            dest = raw / f"{stem}.tif"
            if dest.is_file() and dest.stat().st_size > 1000 and not force:
                print(f"Using cached GLO-30 {dest.name}")
                paths.append(dest)
                continue
            url = f"{GLO30_BASE}/{stem}/{stem}.tif"
            if _download(url, dest):
                paths.append(dest)
            elif dest.is_file():
                dest.unlink()
    if not paths:
        raise SystemExit("No Copernicus GLO-30 tiles downloaded")
    return paths


def _worldcover_tile_id(lat: float, lon: float) -> str:
    lat0 = int(math.floor(lat / 3.0) * 3)
    lon0 = int(math.floor(lon / 3.0) * 3)
    ns = "N" if lat0 >= 0 else "S"
    ew = "E" if lon0 >= 0 else "W"
    return f"{ns}{abs(lat0):02d}{ew}{abs(lon0):03d}"


def _worldcover_pc_url(tile: str) -> str | None:
    try:
        item = requests.get(PC_ITEM.format(tile=tile), timeout=20, headers=UA)
        item.raise_for_status()
        href = (((item.json().get("assets") or {}).get("map") or {}).get("href")) or ""
        if not href:
            return None
        signed = requests.get(PC_SIGN, params={"href": href}, timeout=20, headers=UA)
        signed.raise_for_status()
        return str((signed.json() or {}).get("href") or href)
    except Exception as ex:  # noqa: BLE001
        print(f"  Planetary Computer: {type(ex).__name__}: {ex}")
        return None


def fetch_worldcover_tile(site: dict, *, force: bool = False) -> Path | None:
    west, south, east, north = _wgs84_bbox(site)
    tile = _worldcover_tile_id(0.5 * (south + north), 0.5 * (west + east))
    dest = ROOT / "data" / "raw" / "worldcover" / WORLDCOVER_ITEM.format(tile=tile)
    if dest.is_file() and dest.stat().st_size > 1000 and not force:
        print(f"Using cached WorldCover {dest.name}")
        return dest
    urls: list[str] = []
    pc = _worldcover_pc_url(tile)
    if pc:
        urls.append(pc)
    urls.extend(u.format(tile=tile) for u in WORLDCOVER_URLS)
    for url in urls:
        if _download(url, dest, timeout=(12, 90)):
            return dest
    print("WorldCover not fetched - backdrop will use elevation/slope tint")
    return None


def fetch_swissimage_ortho(
    site: dict,
    *,
    force: bool = False,
    dest: Path | None = None,
    size: int | None = None,
    extent: tuple[float, float, float, float] | None = None,
    label: str = "Swissimage",
) -> Path | None:
    """WMS GetMap SWISSIMAGE Hintergrund, warped onto a site-CRS texture grid."""
    cfg = backdrop_cfg(site)
    dest = dest or ortho_path(site)
    meta_p = dest.with_suffix(".meta.json")
    size = int(size or cfg["texture_size"])
    xmin, ymin, xmax, ymax = extent if extent is not None else padded_extent(site)
    want = (xmin, ymin, xmax, ymax)
    if (
        dest.is_file()
        and dest.stat().st_size > 1000
        and not force
        and _extent_matches(_read_json(meta_p), want)
    ):
        print(f"Using cached {label} {dest}")
        return dest
    src = ((site.get("sources") or {}).get("backdrop_ortho") or {})
    url = str(src.get("url") or SWISSIMAGE_WMS)
    layer = str(src.get("layer") or SWISSIMAGE_LAYER)
    west, south, east, north = _wgs84_bbox(site, (xmin, ymin, xmax, ymax))
    params = {
        "SERVICE": "WMS",
        "VERSION": str(src.get("version") or "1.3.0"),
        "REQUEST": "GetMap",
        "LAYERS": layer,
        "STYLES": "",
        "CRS": "EPSG:4326",
        "BBOX": f"{south},{west},{north},{east}",
        "WIDTH": str(size),
        "HEIGHT": str(size),
        "FORMAT": "image/jpeg",
    }
    print(f"{label} WMS {layer} {size}x{size} BBOX={params['BBOX']} ...")
    try:
        r = requests.get(url, params=params, timeout=120, headers=UA)
        r.raise_for_status()
    except Exception as ex:  # noqa: BLE001
        print(f"  {label} skip: {type(ex).__name__}: {ex}")
        return None
    ctype = (r.headers.get("content-type") or "").lower()
    if "image" not in ctype or r.content[:2] != b"\xff\xd8":
        preview = r.content[:240]
        print(f"  {label} skip: not JPEG ({ctype}): {preview!r}")
        return None
    src_img = np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"), dtype=np.float32)
    sh, sw = src_img.shape[:2]
    xs = xmin + (np.arange(size, dtype=np.float64) + 0.5) * ((xmax - xmin) / size)
    ys = ymax - (np.arange(size, dtype=np.float64) + 0.5) * ((ymax - ymin) / size)
    xx, yy = np.meshgrid(xs, ys)
    to_ll = Transformer.from_crs(str(site.get("crs", "EPSG:31254")), "EPSG:4326", always_xy=True)
    lon, lat = to_ll.transform(xx.ravel(), yy.ravel())
    lon = np.asarray(lon, dtype=np.float64).reshape(xx.shape)
    lat = np.asarray(lat, dtype=np.float64).reshape(yy.shape)
    col = (lon - west) / max(east - west, 1e-12) * (sw - 1)
    row = (north - lat) / max(north - south, 1e-12) * (sh - 1)
    coords = np.vstack([row.ravel(), col.ravel()])
    out = np.empty((size, size, 3), dtype=np.uint8)
    for i in range(3):
        samp = map_coordinates(
            src_img[:, :, i], coords, order=3, mode="nearest", prefilter=True
        )
        out[:, :, i] = np.clip(samp.reshape(size, size), 0, 255)
    dest.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(out, mode="RGB").save(dest)
    meta_p.write_text(
        json.dumps(
            {
                "source": "swissimage_hintergrund",
                "layer": layer,
                "url": url,
                "wgs84_bbox": [west, south, east, north],
                "extent": [xmin, ymin, xmax, ymax],
                "request_bbox": [xmin, ymin, xmax, ymax],
                "size": size,
                "attribution": "SWISSIMAGE Hintergrund (c) swisstopo, wms.geo.admin.ch",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {dest} {out.shape}")
    return dest


def _sample_into(
    dest: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    src: GeoRaster,
    to_src: Transformer,
    *,
    order: int = 3,
) -> None:
    """Warp one source tile onto dest. Default cubic spline; WorldCover uses order=0."""
    lon, lat = to_src.transform(xs.ravel(), ys.ravel())
    lon = np.asarray(lon, dtype=np.float64).reshape(xs.shape)
    lat = np.asarray(lat, dtype=np.float64).reshape(ys.shape)
    col = (lon - src.origin_x) / src.px - 0.5
    row = (lat - src.origin_y) / src.py - 0.5
    h, w = src.data.shape[:2]
    inside = (row >= 0) & (row <= h - 1) & (col >= 0) & (col <= w - 1)
    if not np.any(inside):
        return
    coords = np.vstack([row[inside], col[inside]])
    src_f = np.asarray(src.data, dtype=np.float64)
    order = max(0, int(order))
    if order == 0:
        vals = map_coordinates(src_f, coords, order=0, mode="nearest")
    else:
        finite = np.isfinite(src_f) & (src_f > -1000) & (src_f < 9000)
        fill = float(np.median(src_f[finite])) if finite.any() else 0.0
        zsrc = np.where(finite, src_f, fill)
        vals = map_coordinates(
            zsrc, coords, order=order, mode="nearest", prefilter=order >= 3
        )
        valid = map_coordinates(
            finite.astype(np.float32), coords, order=0, mode="nearest"
        )
        vals = np.where(valid > 0.5, vals, np.nan)
    bad = ~np.isfinite(vals) | (vals < -1000) | (vals > 9000)
    vals = np.where(bad, np.nan, vals)
    slot = dest[inside]
    take = ~np.isfinite(slot) & np.isfinite(vals)
    slot[take] = vals[take]
    dest[inside] = slot


def _fill_short_nodata(arr: np.ndarray, max_px: int = 2) -> np.ndarray:
    """Copy nearest finite neighbour into 1–2 px gaps (GLO-30 1° tile seams)."""
    nan = ~np.isfinite(arr)
    if not nan.any() or nan.all():
        return arr
    dist, (ri, ci) = distance_transform_edt(nan, return_indices=True)
    out = arr.copy()
    take = nan & (dist <= max_px)
    out[take] = arr[ri[take], ci[take]]
    return out


def load_backdrop_dem(site: dict) -> tuple[np.ndarray, dict]:
    tif, meta_path = dem_paths(site)
    if not tif.is_file() or not meta_path.is_file():
        raise FileNotFoundError(tif)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    arr = np.asarray(tiff.imread(tif), dtype=np.float64)
    nodata = meta.get("nodata")
    if nodata is not None:
        arr = np.where(arr == float(nodata), np.nan, arr)
    arr = np.where(np.isfinite(arr) & (arr > -1000) & (arr < 9000), arr, np.nan)
    return arr, meta


def _sanitize_tirol_dgm(arr: np.ndarray) -> np.ndarray:
    a = np.asarray(arr, dtype=np.float64)
    if a.ndim == 3:
        a = a[..., 0]
    # Alps: real ground is hundreds of metres. 0 / huge negatives = WCS nodata (DE, …).
    bad = ~np.isfinite(a) | (a < 50.0) | (a > 4500.0)
    return np.where(bad, np.nan, a)


def load_near_dgm(site: dict) -> tuple[np.ndarray, dict] | None:
    return _load_ring_raster(site, "near", "dgm")


def load_near_dom(site: dict) -> tuple[np.ndarray, dict] | None:
    return _load_ring_raster(site, "near", "dom")


def load_mid_dgm(site: dict) -> tuple[np.ndarray, dict] | None:
    return _load_ring_raster(site, "mid", "dgm")


def load_mid_dom(site: dict) -> tuple[np.ndarray, dict] | None:
    return _load_ring_raster(site, "mid", "dom")


def _load_ring_raster(site: dict, ring: str, kind: str) -> tuple[np.ndarray, dict] | None:
    tif, meta_path = ring_raster_paths(site, ring, kind)
    if not tif.is_file() or not meta_path.is_file():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    arr = _sanitize_tirol_dgm(tiff.imread(tif))
    nodata = meta.get("nodata")
    if nodata is not None:
        arr = np.where(arr == float(nodata), np.nan, arr)
    arr = _sanitize_tirol_dgm(arr)
    if not np.isfinite(arr).any():
        return None
    return arr, meta


def fetch_near_dgm(site: dict, *, force: bool = False) -> Path | None:
    """Tirol WCS DGM for the near ring. Cells without coverage stay empty."""
    return _fetch_ring_wcs(site, "dgm", "near", force=force)


def fetch_near_dom(site: dict, *, force: bool = False) -> Path | None:
    """Tirol WCS DOM/DSM for the near ring (canopy silhouette)."""
    if not ((site.get("sources") or {}).get("dom")):
        print("near DOM skip: no sources.dom")
        return None
    return _fetch_ring_wcs(site, "dom", "near", force=force)


def fetch_mid_dgm(site: dict, *, force: bool = False) -> Path | None:
    """Tirol WCS DGM for the mid envelope (nDSM only; mesh DTM stays Copernicus)."""
    return _fetch_ring_wcs(site, "dgm", "mid", force=force)


def fetch_mid_dom(site: dict, *, force: bool = False) -> Path | None:
    if not ((site.get("sources") or {}).get("dom")):
        print("mid DOM skip: no sources.dom")
        return None
    return _fetch_ring_wcs(site, "dom", "mid", force=force)


def _fetch_near_wcs(site: dict, kind: str, *, force: bool = False) -> Path | None:
    return _fetch_ring_wcs(site, kind, "near", force=force)


def _fetch_ring_wcs(site: dict, kind: str, ring: str, *, force: bool = False) -> Path | None:
    cfg = backdrop_cfg(site)
    label = f"{'DGM' if kind == 'dgm' else 'DOM'} {ring}"
    src_key = "dgm" if kind == "dgm" else "dom"
    tif, meta_path = ring_raster_paths(site, ring, kind)
    xmin, ymin, xmax, ymax = near_extent(site) if ring == "near" else mid_extent(site)
    want = (xmin, ymin, xmax, ymax)
    if (
        tif.is_file()
        and meta_path.is_file()
        and tif.stat().st_size > 1000
        and not force
        and _extent_matches(_read_json(meta_path), want)
    ):
        print(f"Using cached {label} {tif}")
        return tif
    if tif.is_file() and not force:
        print(f"{label} cache extent stale — refetching")
    res = float(cfg["near_dgm_res_m"] if ring == "near" else cfg.get("mid_dgm_res_m", 8.0))
    raw = ROOT / "data" / "raw" / f"{src_key}_{site_slug(site)}_{ring}.tif"
    raw_stale = not _extent_matches(_read_json(raw.with_suffix(".meta.json")), want)
    try:
        fetch_terrain_coverage(
            site,
            src_key,
            force=force or raw_stale,
            label=label,
            default_stem=f"{src_key}_{ring}",
            bbox=(xmin, ymin, xmax, ymax),
            resolution_m=res,
            out_path=raw,
            allow_incomplete=True,
            accept_all_nodata=True,
        )
    except SystemExit as ex:
        print(f"{label} skip: {ex}")
        return None
    except Exception as ex:  # noqa: BLE001
        print(f"{label} skip: {type(ex).__name__}: {ex}")
        return None
    try:
        src = _read_geotiff(raw)
        arr = _sanitize_tirol_dgm(src.data)
        origin_x, origin_y = float(src.origin_x), float(src.origin_y)
        px, py = float(src.px), float(src.py)
    except SystemExit:
        arr = _sanitize_tirol_dgm(tiff.imread(raw))
        origin_x, origin_y, px, py = xmin, ymax, res, -res
    h, w = int(arr.shape[0]), int(arr.shape[1])
    xmax = origin_x + w * px
    ymin = origin_y + h * py
    finite = int(np.isfinite(arr).sum())
    print(
        f"  {label} finite={finite}/{arr.size} "
        f"({100.0 * finite / max(arr.size, 1):.1f}%)"
    )
    if finite == 0:
        print(f"  {label}: no valid samples (outside Tirol coverage?)")
        return None
    packed = np.where(np.isfinite(arr), arr, NODATA).astype(np.float32)
    tif.parent.mkdir(parents=True, exist_ok=True)
    tiff.imwrite(tif, packed)
    meta_path.write_text(
        json.dumps(
            {
                "crs": str(site.get("crs", "EPSG:31254")),
                "origin_x": origin_x,
                "origin_y": origin_y,
                "px": px,
                "py": py,
                "width": w,
                "height": h,
                "bbox": [origin_x, ymin, xmax, origin_y],
                "request_bbox": list(want),
                "nodata": NODATA,
                "source": f"tirol_wcs_{kind}",
                "site": site_slug(site),
                "finite": finite,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {tif} {packed.shape} z={float(np.nanmin(arr)):.1f}..{float(np.nanmax(arr)):.1f}")
    return tif


def fetch_backdrop(
    site: dict,
    *,
    force: bool = False,
    skip_worldcover: bool = False,
    skip_ortho: bool = False,
    skip_near_dgm: bool = False,
) -> Path:
    cfg = backdrop_cfg(site)
    tif, meta_path = dem_paths(site)
    if tif.is_file() and meta_path.is_file() and tif.stat().st_size > 1000 and not force:
        print(f"Using cached backdrop DEM {tif}")
    else:
        tiles = fetch_glo30_tiles(site, force=force)
        print(f"Warp GLO-30 -> {site.get('crs')} @ {cfg['viewshed_step_m']} m ...")
        # Attach paths for log (GeoRaster has no path; print tile names above)
        xmin, ymin, xmax, ymax = padded_extent(site)
        cols = max(2, int(round((xmax - xmin) / cfg["viewshed_step_m"])))
        rows = max(2, int(round((ymax - ymin) / cfg["viewshed_step_m"])))
        xs = xmin + (np.arange(cols, dtype=np.float64) + 0.5) * cfg["viewshed_step_m"]
        ys = ymax - (np.arange(rows, dtype=np.float64) + 0.5) * cfg["viewshed_step_m"]
        xx, yy = np.meshgrid(xs, ys)
        out = np.full((rows, cols), np.nan, dtype=np.float64)
        to_src = Transformer.from_crs(str(site.get("crs", "EPSG:31254")), "EPSG:4326", always_xy=True)
        for p in tiles:
            src = _read_geotiff(p)
            print(f"  {p.name} {src.data.shape[1]}x{src.data.shape[0]}")
            _sample_into(out, xx, yy, src, to_src, order=3)
        n_nan = int((~np.isfinite(out)).sum())
        out = _fill_short_nodata(out, max_px=2)
        n_nan_after = int((~np.isfinite(out)).sum())
        finite = int(np.isfinite(out).sum())
        print(
            f"  DEM cubic spline + {n_nan - n_nan_after} seam fills  "
            f"finite={finite}/{out.size} leftover_nan={n_nan_after} "
            f"z={float(np.nanmin(out)):.1f}..{float(np.nanmax(out)):.1f}"
        )
        arr = np.where(np.isfinite(out), out, NODATA).astype(np.float32)
        meta = {
            "crs": str(site.get("crs", "EPSG:31254")),
            "origin_x": xmin,
            "origin_y": ymax,
            "px": cfg["viewshed_step_m"],
            "py": -cfg["viewshed_step_m"],
            "width": cols,
            "height": rows,
            "bbox": [xmin, ymin, xmax, ymax],
            "nodata": NODATA,
            "source": "copernicus_glo30",
            "site": site_slug(site),
        }
        tif.parent.mkdir(parents=True, exist_ok=True)
        tiff.imwrite(tif, arr)
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"Wrote {tif} {arr.shape}")

    if not skip_worldcover:
        wc_out = worldcover_path(site)
        if wc_out.is_file() and wc_out.stat().st_size > 1000 and not force:
            print(f"Using cached WorldCover warp {wc_out}")
        else:
            wc_tile = fetch_worldcover_tile(site, force=force)
            if wc_tile is not None:
                print("Warp WorldCover onto DEM grid (overview) ...")
                xmin, ymin, xmax, ymax = padded_extent(site)
                step = cfg["viewshed_step_m"]
                cols = max(2, int(round((xmax - xmin) / step)))
                rows = max(2, int(round((ymax - ymin) / step)))
                xs = xmin + (np.arange(cols, dtype=np.float64) + 0.5) * step
                ys = ymax - (np.arange(rows, dtype=np.float64) + 0.5) * step
                xx, yy = np.meshgrid(xs, ys)
                out = np.full((rows, cols), np.nan, dtype=np.float64)
                src = _read_geotiff(wc_tile, max_dim=8000)
                print(f"  WorldCover level {src.data.shape[1]}x{src.data.shape[0]}")
                to_src = Transformer.from_crs(
                    str(site.get("crs", "EPSG:31254")), "EPSG:4326", always_xy=True
                )
                _sample_into(out, xx, yy, src, to_src, order=0)
                codes = np.where(np.isfinite(out), np.rint(out), 0).astype(np.uint8)
                tiff.imwrite(wc_out, codes)
                print(f"Wrote {wc_out} finite={(codes > 0).sum()}/{codes.size}")
    if not skip_ortho:
        fetch_swissimage_ortho(site, force=force)
        ncfg = backdrop_cfg(site)
        fetch_swissimage_ortho(
            site,
            force=force,
            dest=near_ortho_path(site),
            size=int(ncfg["near_texture_size"]),
            extent=near_extent(site),
            label="Swissimage near",
        )
        fetch_swissimage_ortho(
            site,
            force=force,
            dest=mid_ortho_path(site),
            size=int(ncfg["mid_texture_size"]),
            extent=mid_extent(site),
            label="Swissimage mid",
        )
    if not skip_near_dgm:
        fetch_near_dgm(site, force=force)
        fetch_near_dom(site, force=force)
        if backdrop_cfg(site).get("mid_canopy", True):
            fetch_mid_dgm(site, force=force)
            fetch_mid_dom(site, force=force)
    return tif


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--skip-worldcover", action="store_true")
    ap.add_argument("--skip-ortho", action="store_true")
    ap.add_argument("--skip-near-dgm", action="store_true")
    args = ap.parse_args()
    site = load_site()
    print(f"Site: {site_slug(site)}")
    fetch_backdrop(
        site,
        force=args.force,
        skip_worldcover=args.skip_worldcover,
        skip_ortho=args.skip_ortho,
        skip_near_dgm=args.skip_near_dgm,
    )


if __name__ == "__main__":
    main()
