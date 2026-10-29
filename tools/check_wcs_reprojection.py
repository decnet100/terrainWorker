"""How far does the Tirol WCS's own reprojection to EPSG:25832 sit from the BEV grid transformation?

Fetches a native M28 (EPSG:31254) patch, warps it locally to EPSG:25832 through PROJ (BEV AT_GIS_GRID when found),
and finds the shift that best aligns it with the same area of the mosaic the WCS delivered in EPSG:25832.

    python tools\\check_wcs_reprojection.py MOSAIC_25832 x y [x y ...]

GDAL must find at_bev_AT_GIS_GRID_2021_09_28.tif in its PROJ data path, or it silently falls back to the
1.5 m Helmert "MGI to ETRS89 (1)" (gdaltransform shows which).
"""
import sys, subprocess, tempfile, os
import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling
from scipy.ndimage import map_coordinates
from pyproj import Transformer

WCS = "https://gis.tirol.gv.at/arcgis/services/Service_Public/terrain/MapServer/WCSServer"
mosaic = sys.argv[1]
pts = list(zip(*[iter(map(float, sys.argv[2:]))] * 2))
to_m28 = Transformer.from_crs(25832, 31254, always_xy=True)
HALF = 200.0
tmp = tempfile.mkdtemp()

for x, y in pts:
    mx, my = to_m28.transform(x, y)
    bb = [round(mx - HALF - 20), round(my - HALF - 20), round(mx + HALF + 20), round(my + HALF + 20)]
    n = int((bb[2] - bb[0]) / 0.5)
    f = os.path.join(tmp, f"m28_{int(x)}_{int(y)}.tif")
    url = (f"{WCS}?SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCoverage&COVERAGE=Gelaendemodell_50cm_M28&CRS=EPSG:31254"
           f"&BBOX={bb[0]},{bb[1]},{bb[2]},{bb[3]}&WIDTH={n}&HEIGHT={n}&FORMAT=GeoTIFF")
    subprocess.run(["curl", "-s", "--max-time", "120", "-o", f, url], check=True)
    # local warp onto a 0.5 m grid in 25832 around (x, y)
    res = 0.5
    w = int(2 * HALF / res)
    dst_t = rasterio.transform.from_origin(x - HALF, y + HALF, res, res)
    local = np.full((w, w), np.nan, np.float32)
    with rasterio.open(f) as src:
        reproject(rasterio.band(src, 1), local, dst_transform=dst_t, dst_crs="EPSG:25832",
                  src_nodata=src.nodata, dst_nodata=np.nan, resampling=Resampling.bilinear)
    with rasterio.open(mosaic) as m:
        r0, c0 = m.index(x - HALF - 5, y + HALF + 5)
        win = rasterio.windows.Window(c0, r0, w + 40, w + 40)
        ref = m.read(1, window=win).astype(np.float64)
        rt = m.window_transform(win)
    # ref pixel coords for each local cell centre, shifted by (dx, dy)
    jj, ii = np.meshgrid(np.arange(w), np.arange(w))
    cx = x - HALF + (jj + 0.5) * res
    cy = y + HALF - (ii + 0.5) * res
    inv = ~rt
    ok = np.isfinite(local)

    def rms(dx, dy):
        col, row = inv * (cx + dx, cy + dy)
        v = map_coordinates(ref, [row - 0.5, col - 0.5], order=1, cval=np.nan)
        d = (v - local)[ok & np.isfinite(v)]
        return np.sqrt(np.mean((d - np.median(d)) ** 2)), np.median(d)

    best = (1e9, 0, 0, 0)
    for step, span in ((0.25, 4.0), (0.02, 0.3)):
        cx0, cy0 = best[1], best[2]
        for dx in np.arange(cx0 - span, cx0 + span + 1e-9, step):
            for dy in np.arange(cy0 - span, cy0 + span + 1e-9, step):
                e, dz = rms(dx, dy)
                if e < best[0]:
                    best = (e, dx, dy, dz)
    e0, dz0 = rms(0, 0)
    print(f"({x:.0f},{y:.0f}): rms at zero shift {e0:.3f} m; best shift dE={best[1]:+.2f} dN={best[2]:+.2f} m "
          f"(rms {best[0]:.3f} m, dz {best[3]:+.3f} m)")
