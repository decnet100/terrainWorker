"""Cut the GIP network and swissTLM3D layers to the Pfunds–Spiss box (EPSG:25832), into small GeoPackages.

    python -I extract_box.py
"""
import os
import sqlite3
import geopandas as gpd
from shapely.geometry import box

BOX = box(608147.5, 5195108.5, 616338.5, 5203299.5)
PAD = 500
D = os.environ.get("REVIEW_DATA", "data/review")


def cut(src, layer, dst, crs, pad=PAD):
    bb = tuple(gpd.GeoSeries([BOX.buffer(pad)], crs=25832).to_crs(crs).total_bounds)
    g = gpd.read_file(src, layer=layer, bbox=bb)
    g = g.to_crs(25832)
    g = g[g.intersects(BOX.buffer(pad))]
    g.to_file(dst, layer=layer.split(".")[-1])
    print(layer, len(g))


for lyr in ("LINK", "EDGE", "NODE", "LINEAR_USE"):
    cut(f"{D}/gip/gip_network_ogd.gpkg", lyr, f"{D}/gip/net_box.gpkg", 4326)

tlm = f"{D}/ch/SWISSTLM3D_2026_LV95_LN02.gpkg"
names = [r[0] for r in sqlite3.connect(tlm).execute("select table_name from gpkg_contents")]
print("TLM layers:", [n for n in names if "strass" in n.lower() or "grenz" in n.lower() or "hoheits" in n.lower()])
for lyr in names:
    if lyr.lower() in ("tlm_strassen_strasse", "tlm_oev_eisenbahn"):
        cut(tlm, lyr, f"{D}/ch/tlm_box.gpkg", 2056)
        # GIP links in CH reach well beyond the box (Engadin); compare_border needs TLM there too
        cut(tlm, lyr, f"{D}/ch/tlm_wide.gpkg", 2056, pad=4000)
