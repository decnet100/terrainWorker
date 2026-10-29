"""Download swissALTI3D 0.5 m tiles (latest per tile) for a bbox given in EPSG:25832.

    python tools\\fetch_swissalti3d.py data\\raw\\swissalti3d_05 xmin ymin xmax ymax
"""
import json, os, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pyproj import Transformer

out, *bb = sys.argv[1:]
xmin, ymin, xmax, ymax = map(float, bb)
os.makedirs(out, exist_ok=True)
t = Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)
lons, lats = t.transform([xmin, xmax, xmin, xmax], [ymin, ymin, ymax, ymax])
bbox = f"{min(lons)},{min(lats)},{max(lons)},{max(lats)}"
url = f"https://data.geo.admin.ch/api/stac/v0.9/collections/ch.swisstopo.swissalti3d/items?bbox={bbox}&limit=100"
best = {}
while url:
    page = json.load(urllib.request.urlopen(url, timeout=60))
    for it in page["features"]:
        tile = it["id"].split("_")[-1]          # e.g. 2827-1196
        for a in it["assets"].values():
            h = a["href"]
            if h.endswith("_0.5_2056_5728.tif"):
                year = it["id"].split("_")[1]
                if tile not in best or year > best[tile][0]:
                    best[tile] = (year, h)
    url = next((l["href"] for l in page.get("links", []) if l["rel"] == "next"), None)
print("tiles", len(best))


def get(item):
    _, h = item
    dst = os.path.join(out, os.path.basename(h))
    if not os.path.exists(dst):
        urllib.request.urlretrieve(h, dst)
    return dst


with ThreadPoolExecutor(8) as ex:
    files = list(ex.map(get, best.values()))
print("downloaded", len(files))
