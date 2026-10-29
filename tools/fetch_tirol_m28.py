"""Fetch the Tirol WCS DGM/DOM natively in M28 (EPSG:31254) for a bbox given in EPSG:25832, as 0.5 m tiles + VRT.

Native M28 tiles, for comparing the WCS's own reprojection with a local one (check_wcs_reprojection.py).

    python tools\\fetch_tirol_m28.py OUT_DIR COVERAGE xmin ymin xmax ymax [pad_m]

Only needed for check_wcs_reprojection.py: the pipeline fetches in the site CRS, which is fine.
"""
import os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pyproj import Transformer

WCS = "https://gis.tirol.gv.at/arcgis/services/Service_Public/terrain/MapServer/WCSServer"
out, cov, *rest = sys.argv[1:]
xmin, ymin, xmax, ymax = map(float, rest[:4])
pad = float(rest[4]) if len(rest) > 4 else 50.0
os.makedirs(out, exist_ok=True)
t = Transformer.from_crs(25832, 31254, always_xy=True)
xs, ys = t.transform([xmin, xmax, xmin, xmax], [ymin, ymin, ymax, ymax])
X0, Y0 = int(min(xs) - pad) // 2 * 2, int(min(ys) - pad) // 2 * 2
X1, Y1 = int(max(xs) + pad + 2) // 2 * 2, int(max(ys) + pad + 2) // 2 * 2
TILE = 512.0                      # m -> 1024 px at 0.5 m
jobs = []
y = Y0
while y < Y1:
    x = X0
    while x < X1:
        jobs.append((x, y))
        x += TILE
    y += TILE
print(f"M28 {X0},{Y0} .. {X1},{Y1}: {len(jobs)} tiles")


def get(job):
    x, y = job
    f = os.path.join(out, f"{cov}_{int(x)}_{int(y)}.tif")
    if os.path.exists(f) and os.path.getsize(f) > 1000:
        return f
    url = (f"{WCS}?SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCoverage&COVERAGE={cov}&CRS=EPSG:31254"
           f"&BBOX={x},{y},{x + TILE},{y + TILE}&WIDTH=1024&HEIGHT=1024&FORMAT=GeoTIFF")
    for attempt in range(5):
        r = subprocess.run(["curl", "-s", "--max-time", "180", "-o", f, "-w", "%{http_code} %{content_type}", url],
                           capture_output=True, text=True)
        if r.stdout.startswith("200 image/tiff"):
            return f
        time.sleep(3 * (attempt + 1))
    print("failed", job, r.stdout)
    return None


with ThreadPoolExecutor(6) as ex:
    files = [f for f in ex.map(get, jobs) if f]
print("ok", len(files), "of", len(jobs))
vrt = os.path.join(out, f"{cov}.vrt")
subprocess.run(["gdalbuildvrt", "-q", "-overwrite", vrt, *files], check=True)
print("wrote", vrt)
