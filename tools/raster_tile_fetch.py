"""Tiled raster download with per-tile retry for incomplete responses.

Used by WMS GetMap (BEV landcover) and WCS GetCoverage (DGM/DOM): split the
request into tiles, mosaic locally, and re-fetch only tiles that look empty
or full of nodata. Does not invent pixel values for holes.

Site YAML knobs (under the relevant ``sources.*`` block):
  tile_px: 1024
  tile_retries: 4
  tile_retry_sleep_s: 3
  max_nodata_frac: 0.02          # final mosaic gate
  max_tile_nodata_frac: 0.05     # when to retry one tile
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import tifffile as tiff

TileDownloader = Callable[["TileSpec"], np.ndarray]
IncompleteFn = Callable[[np.ndarray], bool]


@dataclass(frozen=True)
class TileSpec:
    """One request tile and its placement in the mosaic (row 0 = north / ymax)."""

    ix: int
    iy: int
    xmin: float
    ymin: float
    xmax: float
    ymax: float
    width_px: int
    height_px: int
    col0: int
    row0: int


def build_tile_grid(
    bbox: tuple[float, float, float, float],
    width_px: int,
    height_px: int,
    tile_px: int,
) -> list[TileSpec]:
    """Axis-aligned pixel tiles covering the full mosaic without overlap."""
    xmin, ymin, xmax, ymax = (float(v) for v in bbox)
    width_px = max(1, int(width_px))
    height_px = max(1, int(height_px))
    tile_px = max(64, int(tile_px))
    span_x = xmax - xmin
    span_y = ymax - ymin
    tiles: list[TileSpec] = []
    for row0 in range(0, height_px, tile_px):
        row1 = min(height_px, row0 + tile_px)
        # row 0 = north edge (ymax)
        ty1 = ymax - (row0 / height_px) * span_y
        ty0 = ymax - (row1 / height_px) * span_y
        for col0 in range(0, width_px, tile_px):
            col1 = min(width_px, col0 + tile_px)
            tx0 = xmin + (col0 / width_px) * span_x
            tx1 = xmin + (col1 / width_px) * span_x
            tiles.append(
                TileSpec(
                    ix=col0 // tile_px,
                    iy=row0 // tile_px,
                    xmin=tx0,
                    ymin=ty0,
                    xmax=tx1,
                    ymax=ty1,
                    width_px=col1 - col0,
                    height_px=row1 - row0,
                    col0=col0,
                    row0=row0,
                )
            )
    return tiles


def classified_nodata_mask(arr: np.ndarray, nodata_code: int = 6) -> np.ndarray:
    a = np.asarray(arr)
    return (a == nodata_code) | (a > nodata_code)


def elevation_nodata_mask(arr: np.ndarray) -> np.ndarray:
    """Tirol DGM/DOM: non-finite, zero, or outside alpine range = bad."""
    a = np.asarray(arr, dtype=np.float64)
    if a.ndim == 3:
        a = a[..., 0]
    return ~np.isfinite(a) | (a < 50.0) | (a > 4500.0) | (a == 0.0)


def make_frac_incomplete(
    mask_fn: Callable[[np.ndarray], np.ndarray],
    max_frac: float,
    *,
    accept_all_nodata: bool = False,
) -> IncompleteFn:
    """Mark a tile incomplete when nodata share exceeds ``max_frac``.

    If ``accept_all_nodata`` is set (backdrop rings / cross-border), a tile that
    is entirely nodata is treated as outside coverage — not as a failed fetch.
    """
    limit = float(max_frac)

    def _incomplete(arr: np.ndarray) -> bool:
        if arr.size == 0:
            return True
        frac = float(mask_fn(arr).mean())
        if accept_all_nodata and frac >= 0.999:
            return False
        return frac > limit

    return _incomplete


def fetch_tiled_mosaic(
    *,
    bbox: tuple[float, float, float, float],
    width_px: int,
    height_px: int,
    tile_px: int,
    download_tile: TileDownloader,
    is_incomplete: IncompleteFn,
    fill_value: int | float,
    dtype: np.dtype | type,
    retries: int = 4,
    retry_sleep_s: float = 3.0,
    tile_cache_dir: Path | None = None,
    force: bool = False,
    label: str = "raster",
) -> tuple[np.ndarray, dict]:
    """Download tiles, retry incomplete ones, return mosaic + stats dict."""
    tiles = build_tile_grid(bbox, width_px, height_px, tile_px)
    mosaic = np.full((height_px, width_px), fill_value, dtype=dtype)
    if tile_cache_dir is not None:
        tile_cache_dir.mkdir(parents=True, exist_ok=True)

    pending = list(tiles)
    ok_count = 0
    fail_ids: list[str] = []
    attempts_used = 0

    for attempt in range(1, max(1, int(retries)) + 1):
        attempts_used = attempt
        if attempt > 1:
            print(
                f"{label}: retry incomplete tiles "
                f"({len(pending)}/{len(tiles)}) after {retry_sleep_s:.0f}s "
                f"(round {attempt}/{retries})"
            )
            time.sleep(float(retry_sleep_s))

        still_bad: list[TileSpec] = []
        for spec in pending:
            tid = f"r{spec.iy}_c{spec.ix}"
            cache_path = (
                tile_cache_dir / f"{tid}.tif" if tile_cache_dir is not None else None
            )
            arr: np.ndarray | None = None
            if (
                cache_path is not None
                and cache_path.is_file()
                and cache_path.stat().st_size > 64
                and not force
                and attempt == 1
            ):
                try:
                    arr = np.asarray(tiff.imread(cache_path))
                except Exception:  # noqa: BLE001
                    arr = None

            if arr is None:
                try:
                    arr = np.asarray(download_tile(spec))
                except Exception as exc:  # noqa: BLE001
                    print(f"  {label} tile {tid} download failed: {exc}")
                    still_bad.append(spec)
                    continue

            if arr.ndim == 3:
                arr = arr[..., 0]
            if arr.shape != (spec.height_px, spec.width_px):
                # Nearest resize if server rounded differently
                from PIL import Image

                img = Image.fromarray(arr)
                if arr.dtype == np.uint8:
                    img = img.resize(
                        (spec.width_px, spec.height_px),
                        resample=Image.Resampling.NEAREST,
                    )
                    arr = np.asarray(img, dtype=np.uint8)
                else:
                    img = Image.fromarray(arr.astype(np.float32))
                    img = img.resize(
                        (spec.width_px, spec.height_px),
                        resample=Image.Resampling.BILINEAR,
                    )
                    arr = np.asarray(img, dtype=np.float32)

            if is_incomplete(arr):
                print(
                    f"  {label} tile {tid} incomplete "
                    f"({spec.width_px}x{spec.height_px})"
                )
                if cache_path is not None and cache_path.is_file():
                    try:
                        cache_path.unlink()
                    except OSError:
                        pass
                still_bad.append(spec)
                continue

            mosaic[
                spec.row0 : spec.row0 + spec.height_px,
                spec.col0 : spec.col0 + spec.width_px,
            ] = arr.astype(dtype, copy=False)
            ok_count += 1
            if cache_path is not None:
                try:
                    tiff.imwrite(cache_path, arr)
                except Exception:  # noqa: BLE001
                    pass

        pending = still_bad
        if not pending:
            break
        # Next rounds must re-download (ignore stale bad caches)
        force = True

    if pending:
        fail_ids = [f"r{t.iy}_c{t.ix}" for t in pending]
        print(
            f"WARNING: {label}: {len(pending)} tile(s) still incomplete after "
            f"{attempts_used} round(s): {', '.join(fail_ids[:12])}"
            + ("…" if len(fail_ids) > 12 else "")
        )

    stats = {
        "tile_px": int(tile_px),
        "tile_count": len(tiles),
        "tiles_ok": ok_count if not pending else len(tiles) - len(pending),
        "tiles_failed": len(pending),
        "tiles_failed_ids": fail_ids,
        "rounds": attempts_used,
        "width_px": int(width_px),
        "height_px": int(height_px),
        "bbox": [float(v) for v in bbox],
    }
    print(
        f"{label}: mosaic {width_px}x{height_px} from {len(tiles)} tiles "
        f"({stats['tiles_ok']} ok, {stats['tiles_failed']} failed, "
        f"{attempts_used} round(s))"
    )
    return mosaic, stats


def http_get_geotiff(
    url: str,
    params: dict,
    *,
    timeout_s: int = 600,
    headers: dict | None = None,
) -> bytes:
    """GET that must return a GeoTIFF body; raises on HTML/error payloads."""
    import requests

    hdrs = headers or {"User-Agent": "beamng_autoroad/0.1", "Accept": "*/*"}
    r = requests.get(url, params=params, headers=hdrs, timeout=timeout_s)
    r.raise_for_status()
    ctype = (r.headers.get("content-type") or "").lower()
    if "tiff" not in ctype and not r.content.startswith((b"II", b"MM")):
        raise RuntimeError(
            f"not GeoTIFF (content-type={ctype}): {r.content[:240]!r}"
        )
    return r.content


def read_tiff_bytes(data: bytes) -> np.ndarray:
    import io

    return np.asarray(tiff.imread(io.BytesIO(data)))
