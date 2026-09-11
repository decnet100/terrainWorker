import json
from pathlib import Path

import requests
from pyproj import Transformer

xmin, ymin, xmax, ymax = 22150.0, 237990.0, 22650.0, 238500.0
t = Transformer.from_crs("EPSG:31254", "EPSG:4326", always_xy=True)
w, s = t.transform(xmin, ymin)
e, n = t.transform(xmax, ymax)
print("bbox wgs", s, w, n, e)

q = (
    f'[out:json][timeout:60];'
    f'way["highway"]({s},{w},{n},{e});'
    f'out geom;'
)
headers = {
    "User-Agent": "beamng_autoroad/0.1 (local test)",
    "Accept": "application/json",
}
urls = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
out = Path("data/raw/osm_roads.json")
for url in urls:
    try:
        r = requests.post(url, data={"data": q}, headers=headers, timeout=90)
        print(url, r.status_code, len(r.content))
        if r.ok:
            out.write_bytes(r.content)
            data = r.json()
            print("elements", len(data.get("elements", [])))
            break
        else:
            print(r.text[:300])
    except Exception as ex:
        print(url, "ERR", ex)
