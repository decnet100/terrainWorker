"""Keep the mixed DGM only inside a buffer of every GIP link.

The mixed file is the full tile, Tyrol and Swiss together, on a 1 m grid.
Cells whose centre lies more than 15 m from every link are cleared. The
result is the terrain for the grade checks.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\mask_dgm_by_links.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
import tifffile as tiff

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from filter_road_corridor import write_referenced_tif  # noqa: E402
from gip_routing_load import gpkg_path  # noqa: E402
from raster_tile_fetch import elevation_nodata_mask  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

BUFFER_M = 15.0


def _grid(path: Path) -> tuple[np.ndarray, float, float, float]:
    """Array plus outer north-west corner and cell size, from the aux file."""
    aux = Path(str(path) + ".aux.xml")
    text = aux.read_text(encoding="utf-8")
    match = re.search(r"<GeoTransform>([^<]+)</GeoTransform>", text)
    if match is None:
        raise SystemExit(f"GeoTransform missing in {aux}")
    origin_x, scale_x, _rot, origin_y, _rot2, scale_y = (float(part) for part in match.group(1).split(","))
    if abs(abs(scale_x) - abs(scale_y)) > 1e-6:
        raise SystemExit(f"{path} has unequal pixel scales {scale_x} and {scale_y}")
    with tiff.TiffFile(path) as src:
        z = np.asarray(src.pages[0].asarray(), dtype=np.float32)
    if z.ndim == 3:
        z = z[..., 0]
    bad = elevation_nodata_mask(z)
    if np.any(bad):
        z = z.copy()
        z[bad] = np.nan
    return z, float(origin_x), float(origin_y), float(abs(scale_x))


def _paint(mask: np.ndarray, geom, xmin: float, ymax: float, res: float) -> None:
    buffered = geom.buffer(BUFFER_M)
    if buffered.is_empty:
        return
    minx, miny, maxx, maxy = buffered.bounds
    height, width = mask.shape
    c0 = max(0, int(np.floor((minx - xmin) / res)))
    c1 = min(width, int(np.ceil((maxx - xmin) / res)))
    r0 = max(0, int(np.floor((ymax - maxy) / res)))
    r1 = min(height, int(np.ceil((ymax - miny) / res)))
    if c1 <= c0 or r1 <= r0:
        return
    cols = np.arange(c0, c1, dtype=np.float64)
    rows = np.arange(r0, r1, dtype=np.float64)
    xx = xmin + (cols + 0.5) * res
    yy = ymax - (rows + 0.5) * res
    grid_x, grid_y = np.meshgrid(xx, yy)
    inside = shapely.contains_xy(buffered, grid_x, grid_y)
    mask[r0:r1, c0:c1] |= inside


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    source = ROOT / str(((site.get("sources") or {}).get("dgm") or {}).get("path") or "")
    if not source.is_file():
        raise SystemExit(f"missing mixed DGM {source}")
    z, xmin, ymax, res = _grid(source)
    print(f"source {source} {z.shape[1]}x{z.shape[0]} cell {res:.2f} m", flush=True)
    links = gpd.read_file(gpkg_path(site), layer="link")
    mask = np.zeros(z.shape, dtype=bool)
    used = 0
    for geom in links.geometry:
        if geom is None or geom.is_empty:
            continue
        _paint(mask, geom, xmin, ymax, res)
        used += 1
        if used % 100 == 0:
            print(f"  links {used}", flush=True)
    kept = int(mask.sum())
    z = z.copy()
    z[~mask] = np.nan
    finite = int(np.isfinite(z).sum())
    out_dir = proc / "dgm_link15"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "dgm_link15.tif"
    write_referenced_tif(dest, z, xmin=xmin, ymax=ymax, res=res)
    print(
        f"links {used} buffer {BUFFER_M:.0f} m cells {kept} with height {finite}",
        flush=True,
    )
    print(f"(open the following in qgis or similar tool to check: {dest})", flush=True)


if __name__ == "__main__":
    main()
