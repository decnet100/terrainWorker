"""Shared WCS GetCoverage download for site terrain sources (DGM, DOM, …).

Large envelopes are fetched as tiles (``raster_tile_fetch``). Incomplete tiles
(mostly invalid elevation) are re-requested; values are never invented.

Site-level downloads always request the **native** cell size
(``sources.<key>.resolution_m``), with a padded envelope so the cubic-spline
kernel has support outside the playable bbox. The mosaic is then resampled
onto ``beamng.meters_per_pixel`` (default target) with a cubic B-spline
(``cubicspline``). WCS must not be asked for the level resolution — that
would leave resampling to the server (typically bilinear).

Crop callers (bridge DOM, …) that pass ``resolution_m=`` keep a direct fetch
at that spacing with no site-target resample.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import tifffile as tiff

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_TILE_PX = 1024
DEFAULT_TILE_RETRIES = 4
DEFAULT_TILE_RETRY_SLEEP_S = 3.0
DEFAULT_MAX_TILE_NODATA_FRAC = 0.05
DEFAULT_MAX_NODATA_FRAC = 0.02
DEFAULT_FETCH_PAD_CELLS = 4
RESAMPLE_METHOD = "cubicspline"


def terrain_cache_path(site: dict, source_key: str, *, default_stem: str | None = None) -> Path:
    src = (site.get("sources") or {}).get(source_key) or {}
    if src.get("path"):
        p = Path(str(src["path"]))
        return p if p.is_absolute() else ROOT / p
    from site_coords import site_slug  # local import: tools/ on sys.path

    stem = default_stem or source_key
    return ROOT / "data" / "raw" / f"{stem}_{site_slug(site)}.tif"


def native_cache_path(final_path: Path) -> Path:
    """Sidecar for the native-resolution mosaic next to the level raster."""
    return final_path.with_name(f"{final_path.stem}_native{final_path.suffix}")


def _as_bbox(bbox: list[float] | tuple[float, float, float, float]) -> list[float]:
    vals = [float(v) for v in bbox]
    if len(vals) != 4:
        raise SystemExit(f"bbox must be xmin,ymin,xmax,ymax — got {bbox!r}")
    xmin, ymin, xmax, ymax = vals
    if not (xmax > xmin and ymax > ymin):
        raise SystemExit(f"bbox is empty or inverted: {vals}")
    return [xmin, ymin, xmax, ymax]


def _grid_origin(src: dict) -> tuple[float, float] | None:
    """Optional cell-centre of native index (0,0): ``grid_origin: [easting, northing]``."""
    raw = src.get("grid_origin")
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise SystemExit("sources.*.grid_origin must be [easting, northing]")
    return float(raw[0]), float(raw[1])


def expand_and_snap_bbox(
    bbox: list[float] | tuple[float, float, float, float],
    *,
    native_res: float,
    pad_cells: int,
    grid_origin: tuple[float, float] | None = None,
) -> tuple[list[float], int, int]:
    """Pad the playable bbox and snap outward onto the native cell grid.

    Returns ``(fetch_bbox, width_px, height_px)`` at ``native_res``. Row 0 of
    the mosaic is north (``ymax``). With ``grid_origin`` (cell centre of
    index 0,0), edges sit half a cell off that origin; without it, edges snap
    to multiples of ``native_res`` in the CRS.
    """
    xmin, ymin, xmax, ymax = _as_bbox(bbox)
    res = float(native_res)
    if res <= 0:
        raise SystemExit(f"native resolution must be positive, got {native_res}")
    pad = max(0, int(pad_cells)) * res
    xmin -= pad
    ymin -= pad
    xmax += pad
    ymax += pad

    if grid_origin is not None:
        oe, on = grid_origin
        half = 0.5 * res

        def snap_lo_e(v: float) -> float:
            return (oe - half) + math.floor((v - (oe - half)) / res) * res

        def snap_hi_e(v: float) -> float:
            return (oe - half) + math.ceil((v - (oe - half)) / res) * res

        # Rows increase southward from the origin cell.
        j_north = int(math.floor(((on + half) - ymax) / res))
        j_south = int(math.ceil(((on + half) - ymin) / res))
        ymax_s = (on + half) - j_north * res
        ymin_s = (on + half) - j_south * res
        xmin_s = snap_lo_e(xmin)
        xmax_s = snap_hi_e(xmax)
    else:
        xmin_s = math.floor(xmin / res) * res
        ymin_s = math.floor(ymin / res) * res
        xmax_s = math.ceil(xmax / res) * res
        ymax_s = math.ceil(ymax / res) * res

    width = max(1, int(round((xmax_s - xmin_s) / res)))
    height = max(1, int(round((ymax_s - ymin_s) / res)))
    # Re-derive edges from integer cell counts so WIDTH matches the envelope.
    xmax_s = xmin_s + width * res
    ymax_s = ymin_s + height * res
    return [xmin_s, ymin_s, xmax_s, ymax_s], width, height


def target_grid_shape(
    bbox: list[float] | tuple[float, float, float, float],
    target_res: float,
) -> tuple[int, int]:
    xmin, ymin, xmax, ymax = _as_bbox(bbox)
    res = float(target_res)
    if res <= 0:
        raise SystemExit(f"target resolution must be positive, got {target_res}")
    width = max(1, int(round((xmax - xmin) / res)))
    height = max(1, int(round((ymax - ymin) / res)))
    return width, height


def resample_elevation_cubicspline(
    native: np.ndarray,
    *,
    native_bbox: list[float],
    native_res: float,
    target_bbox: list[float],
    target_res: float,
) -> np.ndarray:
    """Cubic B-spline resample (row 0 = north) onto the target envelope.

    Pixel centres of the native mosaic are assumed at
    ``(xmin + (col + 0.5) * res, ymax - (row + 0.5) * res)``. Nodata stays
    nodata (nearest mask); only finite source cells drive the spline.
    """
    from scipy.ndimage import map_coordinates

    src = np.asarray(native, dtype=np.float64)
    if src.ndim == 3:
        src = src[..., 0]
    nx0, ny0, nx1, ny1 = _as_bbox(native_bbox)
    tx0, ty0, tx1, ty1 = _as_bbox(target_bbox)
    nres = float(native_res)
    tres = float(target_res)
    sh, sw = src.shape
    meta_w = max(1, int(round((nx1 - nx0) / nres)))
    meta_h = max(1, int(round((ny1 - ny0) / nres)))
    if (sw, sh) != (meta_w, meta_h):
        # Trust the array; rebuild south/east from NW + shape.
        nx1 = nx0 + sw * nres
        ny0 = ny1 - sh * nres

    tw, th = target_grid_shape(target_bbox, tres)
    fx = (np.arange(tw, dtype=np.float64) + 0.5) * tres + tx0
    fy = ty1 - (np.arange(th, dtype=np.float64) + 0.5) * tres
    col_idx = (fx - nx0) / nres - 0.5
    row_idx = (ny1 - fy) / nres - 0.5
    # meshgrid: axis0 = row (north→south), axis1 = col (west→east)
    cols, rows = np.meshgrid(col_idx, row_idx)

    finite = np.isfinite(src)
    if not finite.any():
        return np.full((th, tw), np.nan, dtype=np.float32)

    fill = float(np.nanmean(src[finite]))
    filled = np.where(finite, src, fill).astype(np.float64)
    coords = np.array([rows, cols], dtype=np.float64)
    out = map_coordinates(
        filled, coords, order=3, mode="nearest", prefilter=True
    ).astype(np.float32)
    valid = map_coordinates(
        finite.astype(np.float32), coords, order=0, mode="nearest"
    )
    out[valid < 0.5] = np.nan
    return out


def _meta_matches_target(
    meta: dict,
    *,
    bbox: list[float],
    target_res: float,
    native_res: float,
    pad_cells: int,
) -> bool:
    box = meta.get("bbox")
    if not isinstance(box, list) or len(box) != 4:
        return False
    if any(abs(float(a) - float(b)) > 1e-3 for a, b in zip(box, bbox)):
        return False
    if abs(float(meta.get("resolution_m") or 0) - target_res) > 1e-6:
        return False
    if abs(float(meta.get("native_resolution_m") or native_res) - native_res) > 1e-6:
        return False
    if int(meta.get("fetch_pad_cells") or 0) != int(pad_cells):
        return False
    method = str(meta.get("resample") or "")
    if pad_cells == 0 and abs(native_res - target_res) <= 1e-9:
        return method in {"", "none", RESAMPLE_METHOD}
    return method == RESAMPLE_METHOD


def _download_native_mosaic(
    *,
    site: dict,
    src: dict,
    source_key: str,
    nice: str,
    fetch_bbox: list[float],
    width: int,
    height: int,
    native_res: float,
    out_native: Path,
    force: bool,
    allow_incomplete: bool,
    accept_all_nodata: bool,
) -> tuple[np.ndarray, dict]:
    from raster_tile_fetch import (  # noqa: WPS433
        elevation_nodata_mask,
        fetch_tiled_mosaic,
        http_get_geotiff,
        make_frac_incomplete,
        read_tiff_bytes,
    )

    meta_path = out_native.with_suffix(".meta.json")
    max_tile_nodata = float(src.get("max_tile_nodata_frac", DEFAULT_MAX_TILE_NODATA_FRAC))
    max_nodata = float(src.get("max_nodata_frac", DEFAULT_MAX_NODATA_FRAC))
    tile_px = int(src.get("tile_px", DEFAULT_TILE_PX))
    tile_retries = int(src.get("tile_retries", DEFAULT_TILE_RETRIES))
    tile_retry_sleep_s = float(src.get("tile_retry_sleep_s", DEFAULT_TILE_RETRY_SLEEP_S))

    if out_native.exists() and out_native.stat().st_size > 1000 and not force:
        cached: dict = {}
        if meta_path.is_file():
            try:
                cached = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                cached = {}
        box = cached.get("bbox") or []
        same = (
            isinstance(box, list)
            and len(box) == 4
            and all(abs(float(a) - float(b)) <= 1e-3 for a, b in zip(box, fetch_bbox))
            and abs(float(cached.get("resolution_m") or 0) - native_res) <= 1e-6
            and int(cached.get("width") or 0) == width
            and int(cached.get("height") or 0) == height
        )
        arr = np.asarray(tiff.imread(out_native), dtype=np.float64)
        if arr.ndim == 3:
            arr = arr[..., 0]
        bad = float(elevation_nodata_mask(arr).mean())
        if same and bad <= max_nodata:
            print(
                f"Using cached native {nice}: {out_native} "
                f"({out_native.stat().st_size} bytes)"
            )
            return arr.astype(np.float32), cached
        if bad <= max_nodata and arr.shape == (height, width) and not cached:
            print(
                f"Using cached native {nice}: {out_native} "
                f"({out_native.stat().st_size} bytes)"
            )
            return arr.astype(np.float32), {
                "bbox": fetch_bbox,
                "resolution_m": native_res,
                "width": width,
                "height": height,
            }
        print(f"Cached native {nice} incomplete or size mismatch — refetching")

    url = str(src.get("url") or "")
    coverage = str(src.get("coverage") or "")
    version = str(src.get("version") or "1.0.0")
    if not url or not coverage:
        raise SystemExit(f"sources.{source_key}.url and coverage required in site config")

    crs = str(site.get("crs", "EPSG:31254"))
    headers = {"User-Agent": "beamng_autoroad/0.1 (local test)", "Accept": "*/*"}
    xmin, ymin, xmax, ymax = fetch_bbox

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

    tile_dir = out_native.with_name(out_native.stem + "_tiles")
    print(
        f"Downloading native {nice} {coverage} BBOX={xmin},{ymin},{xmax},{ymax} "
        f"{width}x{height} @ {native_res}m (tiles {tile_px}px) …"
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
        label=f"native {nice}",
    )

    out_native.parent.mkdir(parents=True, exist_ok=True)
    tiff.imwrite(out_native, mosaic.astype(np.float32), compression="deflate")
    bad_frac = float(elevation_nodata_mask(mosaic).mean())
    meta = {
        "source_key": source_key,
        "url": url,
        "coverage": coverage,
        "version": version,
        "crs": site.get("crs"),
        "bbox": fetch_bbox,
        "resolution_m": native_res,
        "width": width,
        "height": height,
        "bytes": out_native.stat().st_size,
        "tiled": True,
        "tile_stats": tile_stats,
        "nodata_frac": bad_frac,
        "nodata_pct": round(100.0 * bad_frac, 3),
        "path": str(
            out_native.relative_to(ROOT) if out_native.is_relative_to(ROOT) else out_native
        ),
        "role": "native",
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote native {out_native} ({out_native.stat().st_size} bytes)")

    if bad_frac > max_nodata:
        msg = (
            f"WARNING: native {nice} incomplete - bad elevation={100 * bad_frac:.2f}% "
            f"(limit {100 * max_nodata:.2f}%) at {out_native}"
        )
        print(msg)
        if not allow_incomplete:
            raise SystemExit(
                f"native {nice} nodata {100 * bad_frac:.2f}% exceeds max_nodata_frac "
                f"{100 * max_nodata:.2f}%. Re-run with --force or raise tile_retries."
            )
    return mosaic.astype(np.float32), meta


def fetch_terrain_coverage(
    site: dict,
    source_key: str,
    *,
    force: bool = False,
    label: str | None = None,
    default_stem: str | None = None,
    bbox: list[float] | tuple[float, float, float, float] | None = None,
    resolution_m: float | None = None,
    target_resolution_m: float | None = None,
    pad_cells: int | None = None,
    out_path: Path | None = None,
    allow_incomplete: bool = False,
    accept_all_nodata: bool = False,
) -> Path:
    """Download one ``sources.<key>`` WCS coverage into ``data/raw/`` (cached).

    Site default: fetch at native ``sources.<key>.resolution_m`` with padding,
    then cubic-spline onto ``beamng.meters_per_pixel``.

    When ``resolution_m`` is passed (crop tools), fetch that spacing over
    ``bbox`` with no site-target resample (``pad_cells`` default 0).
    """
    from raster_tile_fetch import elevation_nodata_mask  # noqa: WPS433

    src = (site.get("sources") or {}).get(source_key) or {}
    if not src:
        raise SystemExit(f"sources.{source_key} missing in site config")

    # Local path override: caller already prepared the level raster.
    if src.get("path") and out_path is None and resolution_m is None:
        out = terrain_cache_path(site, source_key, default_stem=default_stem)
        if not out.exists():
            raise SystemExit(f"sources.{source_key}.path missing on disk: {out}")
        print(f"Using local {label or source_key.upper()}: {out}")
        return out

    out = Path(out_path) if out_path is not None else terrain_cache_path(
        site, source_key, default_stem=default_stem
    )
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    meta_path = out.with_suffix(".meta.json")
    nice = label or source_key.upper()

    playable = _as_bbox(bbox if bbox is not None else site["bbox"])
    yaml_native = float(src.get("resolution_m") or 0.5)

    if resolution_m is not None:
        # Crop / legacy: direct fetch at the requested spacing.
        native_res = float(resolution_m)
        target_res = float(
            target_resolution_m if target_resolution_m is not None else native_res
        )
        pad = int(pad_cells if pad_cells is not None else 0)
        site_level = False
    else:
        native_res = yaml_native
        bng = site.get("beamng") or {}
        target_res = float(
            target_resolution_m
            if target_resolution_m is not None
            else bng.get("meters_per_pixel") or native_res
        )
        pad = int(
            pad_cells
            if pad_cells is not None
            else src.get("fetch_pad_cells", DEFAULT_FETCH_PAD_CELLS)
        )
        site_level = True

    max_nodata = float(src.get("max_nodata_frac", DEFAULT_MAX_NODATA_FRAC))
    origin = _grid_origin(src) if site_level else None

    if (
        out.exists()
        and out.stat().st_size > 1000
        and not force
        and meta_path.is_file()
    ):
        try:
            cached = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cached = {}
        if _meta_matches_target(
            cached,
            bbox=playable,
            target_res=target_res,
            native_res=native_res,
            pad_cells=pad,
        ):
            arr = np.asarray(tiff.imread(out), dtype=np.float64)
            if arr.ndim == 3:
                arr = arr[..., 0]
            bad = float(elevation_nodata_mask(arr).mean())
            if bad <= max_nodata:
                print(f"Using cached {nice}: {out} ({out.stat().st_size} bytes)")
                return out
            print(f"Cached {nice} incomplete (bad={100 * bad:.2f}%) — rebuilding")

    # Direct path when native == target and no pad: one mosaic on playable bbox.
    if abs(native_res - target_res) <= 1e-9 and pad == 0:
        tw, th = target_grid_shape(playable, target_res)
        mosaic, _meta = _download_native_mosaic(
            site=site,
            src=src,
            source_key=source_key,
            nice=nice,
            fetch_bbox=playable,
            width=tw,
            height=th,
            native_res=native_res,
            out_native=out,
            force=force,
            allow_incomplete=allow_incomplete,
            accept_all_nodata=accept_all_nodata,
        )
        bad_frac = float(elevation_nodata_mask(mosaic).mean())
        meta = {
            "source_key": source_key,
            "url": src.get("url"),
            "coverage": src.get("coverage"),
            "version": src.get("version") or "1.0.0",
            "crs": site.get("crs"),
            "bbox": playable,
            "resolution_m": target_res,
            "native_resolution_m": native_res,
            "native_bbox": playable,
            "fetch_pad_cells": 0,
            "resample": "none",
            "width": tw,
            "height": th,
            "bytes": out.stat().st_size,
            "nodata_frac": bad_frac,
            "nodata_pct": round(100.0 * bad_frac, 3),
            "path": str(out.relative_to(ROOT) if out.is_relative_to(ROOT) else out),
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return out

    fetch_bbox, nw, nh = expand_and_snap_bbox(
        playable,
        native_res=native_res,
        pad_cells=pad,
        grid_origin=origin,
    )
    out_native = native_cache_path(out)
    native_arr, native_meta = _download_native_mosaic(
        site=site,
        src=src,
        source_key=source_key,
        nice=nice,
        fetch_bbox=fetch_bbox,
        width=nw,
        height=nh,
        native_res=native_res,
        out_native=out_native,
        force=force,
        allow_incomplete=allow_incomplete,
        accept_all_nodata=accept_all_nodata,
    )

    tw, th = target_grid_shape(playable, target_res)
    print(
        f"Resample {nice} cubicspline "
        f"{nw}x{nh} @ {native_res}m → {tw}x{th} @ {target_res}m "
        f"(pad_cells={pad})"
    )
    target = resample_elevation_cubicspline(
        native_arr,
        native_bbox=list(native_meta.get("bbox") or fetch_bbox),
        native_res=native_res,
        target_bbox=playable,
        target_res=target_res,
    )
    tiff.imwrite(out, target.astype(np.float32), compression="deflate")
    bad_frac = float(elevation_nodata_mask(target).mean())
    meta = {
        "source_key": source_key,
        "url": src.get("url"),
        "coverage": src.get("coverage"),
        "version": src.get("version") or "1.0.0",
        "crs": site.get("crs"),
        "bbox": playable,
        "resolution_m": target_res,
        "native_resolution_m": native_res,
        "native_bbox": fetch_bbox,
        "native_path": str(
            out_native.relative_to(ROOT) if out_native.is_relative_to(ROOT) else out_native
        ),
        "fetch_pad_cells": pad,
        "resample": RESAMPLE_METHOD,
        "grid_origin": list(origin) if origin else None,
        "width": tw,
        "height": th,
        "bytes": out.stat().st_size,
        "nodata_frac": bad_frac,
        "nodata_pct": round(100.0 * bad_frac, 3),
        "path": str(out.relative_to(ROOT) if out.is_relative_to(ROOT) else out),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {out} ({out.stat().st_size} bytes)")
    print(f"Wrote {meta_path}")

    if bad_frac > max_nodata:
        msg = (
            f"WARNING: {nice} incomplete - bad elevation={100 * bad_frac:.2f}% "
            f"(limit {100 * max_nodata:.2f}%) at {out}"
        )
        print(msg)
        if not allow_incomplete:
            raise SystemExit(
                f"{nice} nodata {100 * bad_frac:.2f}% exceeds max_nodata_frac "
                f"{100 * max_nodata:.2f}%. Re-run with --force or raise tile_retries."
            )
    return out
