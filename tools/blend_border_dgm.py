"""Blend the Tyrol DGM with swissALTI3D across the coverage edge.

Swiss tiles are pasted into one LV95 mosaic first. Reprojection samples
that mosaic, so a kilometre line has neighbours from both tiles. The edge
used for the mix is the nodata contour of delivered Tyrol heights. Tiles
that never arrived are already nodata and stay that way until a Swiss
height exists. Inside a band of half-width ``b`` both heights are mixed.
Outside it, and wherever only one height exists, that height is kept.

Writes a 1 m GeoTIFF on the same grid as ``dgm_<slug>.tif`` plus a ratio
raster (0 = Tyrol, 1 = Swiss).

    cd C:\\temp\\beamng_autoroad
    python tools\\blend_border_dgm.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np
import requests
import tifffile as tiff
from pyproj import Transformer
from scipy.ndimage import distance_transform_edt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from raster_tile_fetch import elevation_nodata_mask  # noqa: E402

HALF_M = 10.0
STAC = (
    "https://data.geo.admin.ch/api/stac/v1/collections/"
    "ch.swisstopo.swissalti3d/items"
)
WGS_BBOX = "10.4199,46.9007,10.5295,46.9730"
RAW = ROOT / "data" / "raw"
NATIVE = RAW / "dgm_pfunds-spiess-8192_native.tif"
NATIVE_META = RAW / "dgm_pfunds-spiess-8192_native.meta.json"
LEVEL = RAW / "dgm_pfunds-spiess-8192.tif"
TILE_DIR = RAW / "swissalti3d"
MIXED = RAW / "dgm_pfunds-spiess-8192_mixed.tif"
RATIO = RAW / "dgm_pfunds-spiess-8192_mix_ratio.tif"
MOSAIC = TILE_DIR / "swissalti3d_mosaic.tif"


def geometric_weight(signed_m: np.ndarray, half_m: float) -> np.ndarray:
    """Swiss share. 0 on the Tyrol side of the band, 1 on the Swiss side."""
    t = np.clip((signed_m + half_m) / (2.0 * half_m), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _geotransform(path: Path) -> tuple[float, float, float, float]:
    aux = Path(str(path) + ".aux.xml")
    text = aux.read_text(encoding="utf-8")
    match = re.search(r"<GeoTransform>([^<]+)</GeoTransform>", text)
    if match is None:
        raise SystemExit(f"GeoTransform missing in {aux}")
    origin_x, scale_x, _rot, origin_y, _rot2, scale_y = (
        float(part) for part in match.group(1).split(",")
    )
    return origin_x, scale_x, origin_y, scale_y


def _utm_srs() -> str:
    match = re.search(
        r"<SRS>.*</SRS>",
        (Path(str(LEVEL) + ".aux.xml")).read_text(encoding="utf-8"),
        flags=re.DOTALL,
    )
    if match is None:
        raise SystemExit("SRS missing on the Tyrol 1 m DGM")
    return match.group(0)


def _write_geotiff(
    path: Path,
    arr: np.ndarray,
    geo: tuple[float, float, float, float],
    *,
    srs_xml: str | None = None,
) -> None:
    origin_x, scale_x, origin_y, scale_y = geo
    tiff.imwrite(path, arr.astype(np.float32), compression="deflate")
    srs = srs_xml if srs_xml is not None else _utm_srs()
    aux = (
        "<PAMDataset>\n  "
        + srs
        + "\n  <GeoTransform>"
        + f"{origin_x}, {scale_x}, 0, {origin_y}, 0, {scale_y}"
        + "</GeoTransform>\n</PAMDataset>\n"
    )
    Path(str(path) + ".aux.xml").write_text(aux, encoding="utf-8")
    # Centre of the north-west pixel.
    tfw = (
        f"{scale_x}\n0\n0\n{scale_y}\n"
        f"{origin_x + 0.5 * scale_x}\n{origin_y + 0.5 * scale_y}\n"
    )
    path.with_suffix(".tfw").write_text(tfw, encoding="utf-8")


def _sample_native() -> tuple[np.ndarray, tuple[float, float, float, float]]:
    meta = json.loads(NATIVE_META.read_text(encoding="utf-8"))
    nx0, ny0, nx1, ny1 = (float(v) for v in meta["bbox"])
    nres = float(meta["resolution_m"])
    native = np.asarray(tiff.imread(NATIVE), dtype=np.float32)
    if native.ndim == 3:
        native = native[..., 0]
    origin_x, scale_x, origin_y, scale_y = _geotransform(LEVEL)
    with tiff.TiffFile(LEVEL) as handle:
        height, width = handle.pages[0].shape[:2]
    cols = np.arange(width, dtype=np.float64)
    rows = np.arange(height, dtype=np.float64)
    x = origin_x + (cols + 0.5) * scale_x
    y = origin_y + (rows + 0.5) * scale_y
    nc = np.rint((x - nx0) / nres - 0.5).astype(np.int32)
    nr = np.rint((ny1 - y) / nres - 0.5).astype(np.int32)
    if nc.min() < 0 or nr.min() < 0 or nc.max() >= native.shape[1] or nr.max() >= native.shape[0]:
        raise SystemExit("1 m grid is outside the native DGM")
    sampled = native[nr[:, None], nc[None, :]]
    bad = elevation_nodata_mask(sampled)
    sampled = np.array(sampled, dtype=np.float32, copy=True)
    sampled[bad] = np.nan
    return sampled, (origin_x, scale_x, origin_y, scale_y)


def _signed_distance_m(finite: np.ndarray, pixel_m: float) -> np.ndarray:
    """Positive on the empty side, negative inside delivered Tyrol heights."""
    inside = distance_transform_edt(finite) * pixel_m
    outside = distance_transform_edt(~finite) * pixel_m
    signed = np.where(finite, -inside, outside).astype(np.float32)
    return signed


def _stac_hrefs() -> dict[str, str]:
    response = requests.get(STAC, params={"bbox": WGS_BBOX, "limit": 100}, timeout=60)
    response.raise_for_status()
    hrefs: dict[str, str] = {}
    for feature in response.json().get("features") or []:
        for name, asset in (feature.get("assets") or {}).items():
            if name.endswith("_0.5_2056_5728.tif"):
                hrefs[name.split("_")[2]] = asset["href"]
    return hrefs


def _ensure_tile(key: str, href: str) -> Path:
    TILE_DIR.mkdir(parents=True, exist_ok=True)
    dest = TILE_DIR / href.rsplit("/", 1)[-1]
    preview = RAW / "border_ch" / dest.name
    if dest.is_file() and dest.stat().st_size > 1000:
        return dest
    if preview.is_file() and preview.stat().st_size > 1000:
        return preview
    print(f"download {key}", flush=True)
    with requests.get(href, stream=True, timeout=180) as response:
        response.raise_for_status()
        with dest.open("wb") as handle:
            for chunk in response.iter_content(1 << 20):
                if chunk:
                    handle.write(chunk)
    return dest


def _tile_grid(path: Path) -> tuple[np.ndarray, float, float, float]:
    with tiff.TiffFile(path) as handle:
        page = handle.pages[0]
        arr = page.asarray()
        scale = page.tags["ModelPixelScaleTag"].value
        tie = page.tags["ModelTiepointTag"].value
    grid = np.asarray(arr, dtype=np.float32)
    if grid.ndim == 3:
        grid = grid[..., 0]
    bad = elevation_nodata_mask(grid) | (grid <= -1000.0)
    grid = np.array(grid, copy=True)
    grid[bad] = np.nan
    return grid, float(tie[3]), float(tie[4]), float(scale[0])


def _bilinear(grid: np.ndarray, row: np.ndarray, col: np.ndarray) -> np.ndarray:
    height, width = grid.shape
    r0 = np.floor(row).astype(np.int32)
    c0 = np.floor(col).astype(np.int32)
    inside = (r0 >= 0) & (c0 >= 0) & (r0 + 1 < height) & (c0 + 1 < width)
    out = np.full(row.shape, np.nan, dtype=np.float32)
    if not np.any(inside):
        return out
    rr = r0[inside]
    cc = c0[inside]
    dr = (row[inside] - rr).astype(np.float32)
    dc = (col[inside] - cc).astype(np.float32)
    v00 = grid[rr, cc]
    v01 = grid[rr, cc + 1]
    v10 = grid[rr + 1, cc]
    v11 = grid[rr + 1, cc + 1]
    valid = np.isfinite(v00) & np.isfinite(v01) & np.isfinite(v10) & np.isfinite(v11)
    mixed = (
        v00 * (1.0 - dr) * (1.0 - dc)
        + v01 * (1.0 - dr) * dc
        + v10 * dr * (1.0 - dc)
        + v11 * dr * dc
    )
    picked = out[inside]
    picked[valid] = mixed[valid]
    out[inside] = picked
    return out


def _assemble_lv95(tiles: list[Path]) -> tuple[np.ndarray, float, float, float]:
    """Paste 1 km COGs onto one LV95 grid. Tile edges meet, they do not overlap."""
    placed: list[tuple[np.ndarray, float, float, float]] = []
    for path in tiles:
        placed.append(_tile_grid(path))
    step = placed[0][3]
    west = min(item[1] for item in placed)
    north = max(item[2] for item in placed)
    east = max(item[1] + item[0].shape[1] * item[3] for item in placed)
    south = min(item[2] - item[0].shape[0] * item[3] for item in placed)
    width = int(round((east - west) / step))
    height = int(round((north - south) / step))
    mosaic = np.full((height, width), np.nan, dtype=np.float32)
    for grid, tie_x, tie_y, tile_step in placed:
        col = int(round((tie_x - west) / tile_step))
        row = int(round((north - tie_y) / tile_step))
        tile_h, tile_w = grid.shape
        slot = mosaic[row : row + tile_h, col : col + tile_w]
        clash = np.isfinite(slot) & np.isfinite(grid)
        if int(np.count_nonzero(clash)):
            raise SystemExit("Swiss tiles overlap on the LV95 mosaic")
        mosaic[row : row + tile_h, col : col + tile_w] = grid
    print(
        f"LV95 mosaic {width}x{height} @ {step} m, "
        f"west {west:.1f} north {north:.1f}",
        flush=True,
    )
    from pyproj import CRS

    wkt = escape(CRS.from_epsg(2056).to_wkt())
    _write_geotiff(
        MOSAIC,
        mosaic,
        (west, step, north, -step),
        srs_xml=f"<SRS>{wkt}</SRS>",
    )
    print(f"wrote {MOSAIC}", flush=True)
    return mosaic, west, north, step


def _sample_swiss(
    origin_x: float,
    scale_x: float,
    origin_y: float,
    scale_y: float,
    need: np.ndarray,
) -> np.ndarray:
    height, width = need.shape
    swiss = np.full((height, width), np.nan, dtype=np.float32)
    rows, cols = np.nonzero(need)
    if len(rows) == 0:
        return swiss
    x = origin_x + (cols.astype(np.float64) + 0.5) * scale_x
    y = origin_y + (rows.astype(np.float64) + 0.5) * scale_y
    to_lv = Transformer.from_crs("EPSG:25832", "EPSG:2056", always_xy=True)
    east, north = to_lv.transform(x, y)
    east = np.asarray(east, dtype=np.float64)
    north = np.asarray(north, dtype=np.float64)
    tile_e = np.floor(east / 1000.0).astype(np.int32)
    tile_n = np.floor((north - 1e-6) / 1000.0).astype(np.int32)
    code = tile_e.astype(np.int64) * 100000 + tile_n.astype(np.int64)
    hrefs = _stac_hrefs()
    unique = np.unique(code)
    print(f"swiss sample pixels {len(rows)}, tiles {len(unique)}", flush=True)
    paths: list[Path] = []
    for key_code in unique:
        tile_key = f"{int(key_code // 100000):04d}-{int(key_code % 100000):04d}"
        href = hrefs.get(tile_key)
        if href is None:
            print(f"no swissALTI3D tile {tile_key}", flush=True)
            continue
        paths.append(_ensure_tile(tile_key, href))
    mosaic, west, north_edge, step = _assemble_lv95(paths)
    col = (east - west) / step - 0.5
    row = (north_edge - north) / step - 0.5
    swiss[rows, cols] = _bilinear(mosaic, row, col)
    return swiss


def blend(half_m: float = HALF_M) -> dict:
    z_at, geo = _sample_native()
    origin_x, scale_x, origin_y, scale_y = geo
    pixel_m = float(abs(scale_x))
    finite = np.isfinite(z_at)
    signed = _signed_distance_m(finite, pixel_m)
    target = geometric_weight(signed, half_m).astype(np.float32)
    # Swiss heights are only required on the empty side and inside the band.
    need = signed > -half_m
    z_ch = _sample_swiss(origin_x, scale_x, origin_y, scale_y, need)
    has_at = finite
    has_ch = np.isfinite(z_ch)
    both = has_at & has_ch
    ratio = np.full(z_at.shape, np.nan, dtype=np.float32)
    mixed = np.full(z_at.shape, np.nan, dtype=np.float32)
    ratio[has_at & ~has_ch] = 0.0
    mixed[has_at & ~has_ch] = z_at[has_at & ~has_ch]
    ratio[has_ch & ~has_at] = 1.0
    mixed[has_ch & ~has_at] = z_ch[has_ch & ~has_at]
    w = target[both]
    ratio[both] = w
    mixed[both] = (1.0 - w) * z_at[both] + w * z_ch[both]
    _write_geotiff(MIXED, mixed, geo)
    _write_geotiff(RATIO, ratio, geo)
    blend_n = int(np.count_nonzero(both & (target > 0.001) & (target < 0.999)))
    report = {
        "half_width_m": half_m,
        "pixel_m": pixel_m,
        "tyrol_only": int(np.count_nonzero(has_at & ~has_ch)),
        "swiss_only": int(np.count_nonzero(has_ch & ~has_at)),
        "both": int(np.count_nonzero(both)),
        "blended": blend_n,
        "empty": int(np.count_nonzero(~has_at & ~has_ch)),
        "mixed": str(MIXED),
        "ratio": str(RATIO),
        "lv95_mosaic": str(MOSAIC),
        "ratio_note": "0 = Tyrol, 1 = Swiss, empty = neither height",
    }
    (RAW / "dgm_pfunds-spiess-8192_mix.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2), flush=True)
    return report


def _self_check() -> None:
    s = np.array([-20.0, -10.0, 0.0, 10.0, 20.0], dtype=np.float64)
    w = geometric_weight(s, 10.0)
    assert abs(w[0]) < 1e-9 and abs(w[1]) < 1e-9
    assert abs(w[2] - 0.5) < 1e-9
    assert abs(w[3] - 1.0) < 1e-9 and abs(w[4] - 1.0) < 1e-9
    # Missing Swiss height keeps the Tyrol value; the used ratio is then 0.
    z_at = np.array([1000.0, 1000.0], dtype=np.float32)
    z_ch = np.array([np.nan, 1000.05], dtype=np.float32)
    target = np.array([0.3, 0.3], dtype=np.float32)
    has_at = np.isfinite(z_at)
    has_ch = np.isfinite(z_ch)
    used = np.full(2, np.nan, dtype=np.float32)
    used[has_at & ~has_ch] = 0.0
    used[has_at & has_ch] = target[has_at & has_ch]
    assert used[0] == 0.0 and abs(float(used[1]) - 0.3) < 1e-6
    # A sample on the kilometre line reads both tiles once they share a grid.
    step = 0.5
    mosaic = np.full((4, 8), np.nan, dtype=np.float32)
    mosaic[:, 0:4] = 10.0
    mosaic[:, 4:8] = 20.0
    on_join = _bilinear(mosaic, np.array([1.0]), np.array([2.0 / step - 0.5]))
    assert np.isfinite(on_join[0]) and abs(float(on_join[0]) - 15.0) < 1e-4
    print("self-check: band weight, nodata fallback, and tile join hold", flush=True)


def main() -> None:
    _self_check()
    blend()


if __name__ == "__main__":
    main()
