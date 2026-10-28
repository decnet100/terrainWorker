"""Fetch swissTLM3D roads (geo.admin.ch API) for the Swiss part of a bbox given in EPSG:25832.

    python -I fetch_ch.py OUT_DIR xmin ymin xmax ymax
"""
import json, sys, time, urllib.parse, urllib.request
from pyproj import Transformer

out, *bb = sys.argv[1:]
xmin, ymin, xmax, ymax = map(float, bb)
t = Transformer.from_crs("EPSG:25832", "EPSG:2056", always_xy=True)
xs, ys = t.transform([xmin, xmax, xmin, xmax], [ymin, ymin, ymax, ymax])
E0, N0, E1, N1 = min(xs), min(ys), max(xs), max(ys)
print("LV95 bbox", round(E0), round(N0), round(E1), round(N1))

API = "https://api3.geo.admin.ch/rest/services/api/MapServer/identify"
LAYER = "ch.swisstopo.swisstlm3d-strassen"
TILE = 1000.0
feats = {}
e = E0
while e < E1:
    n = N0
    while n < N1:
        env = f"{e},{n},{min(e + TILE, E1)},{min(n + TILE, N1)}"
        offset = 0
        while True:
            q = dict(geometry=env, geometryType="esriGeometryEnvelope", layers=f"all:{LAYER}",
                     mapExtent=env, imageDisplay="1000,1000,96", tolerance=0, returnGeometry="true",
                     geometryFormat="geojson", sr=2056, limit=50, offset=offset)
            url = API + "?" + urllib.parse.urlencode(q)
            for attempt in range(4):
                try:
                    res = json.load(urllib.request.urlopen(url, timeout=60))["results"]
                    break
                except Exception as ex:
                    print("retry", ex); time.sleep(3)
            else:
                raise SystemExit("failed " + url)
            for r in res:
                feats[r["id"]] = {"type": "Feature", "id": r["id"], "geometry": r["geometry"],
                                  "properties": r.get("properties") or r.get("attributes")}
            if len(res) < 50:
                break
            offset += 50
        n += TILE
    e += TILE
    print("column done", round(e), len(feats), flush=True)
json.dump({"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "EPSG:2056"}},
           "features": list(feats.values())}, open(f"{out}/tlm_strassen.json", "w"))
print("features", len(feats))
