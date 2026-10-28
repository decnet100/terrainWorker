"""GIP links vs swissTLM3D at the Austrian–Swiss border of the Pfunds–Spiss box.

    python -I compare_border.py
Prints the GIP length in Switzerland, how far it lies from swissTLM3D, which pieces are invented (tunnels),
and how the border crossings of both datasets line up. Writes $REVIEW_DATA/compare_border.gpkg (default data/review).
"""
import os
import collections
import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import box

D = os.environ.get("REVIEW_DATA", "data/review")
BOX = box(608147.5, 5195108.5, 616338.5, 5203299.5)
OUT = f"{D}/compare_border.gpkg"

L = gpd.read_file(f"{D}/gip/net_box.gpkg", layer="LINK")
T = gpd.read_file(f"{D}/ch/tlm_wide.gpkg", layer="tlm_strassen_strasse").to_crs(25832)
T["geometry"] = T.geometry.force_2d()
ch = gpd.read_file(f"{D}/ch/sb3d/swissBOUNDARIES3D_1_5_LV95_LN02.gpkg", layer="tlm_landesgebiet")
ch = ch[ch["icc"] == "CH"].to_crs(25832) if "icc" in ch else ch.to_crs(25832)
CH = shapely.union_all(list(ch.geometry)).intersection(BOX.buffer(2000))
border = CH.boundary.intersection(BOX.buffer(600))
print(f"box share in CH: {CH.intersection(BOX).area / BOX.area * 100:.0f} %")

# --- GIP inside Switzerland -------------------------------------------------------------------
L["len_ch"] = L.geometry.intersection(CH).length
L["share_ch"] = L.len_ch / L.geometry.length
gch = L[L.len_ch > 1].copy()
print(f"\nGIP links touching CH: {len(gch)}, {gch.len_ch.sum() / 1000:.1f} km inside CH "
      f"(of {L.geometry.length.sum() / 1000:.1f} km in the box)")
print(gch.groupby("functional_class").len_ch.sum().div(1000).round(2).to_dict())
print("names:", collections.Counter(gch.name_text_hr.fillna("-")).most_common(12))

kunst = set(T.kunstbaute.dropna().unique())
print("TLM kunstbaute values:", kunst)
roads = T[~T.objektart.isin(["1m Weg", "1m Wegfragment"])]
tun = T[T.kunstbaute.isin({"Tunnel", "Galerie", "Unterfuehrung"})]
tree = shapely.STRtree(list(roads.geometry))
rows = []
for _, r in gch.iterrows():
    part = r.geometry.intersection(CH)
    for g in getattr(part, "geoms", [part]):
        if g.length < 1:
            continue
        for d in np.arange(0, g.length, 5.0):
            p = g.interpolate(d)
            i = tree.nearest(p)
            dist = p.distance(roads.geometry.iloc[i])
            in_tun = any(p.distance(x) < 15 for x in tun.geometry)
            rows.append((r.object_id, r.name_text_hr, r.functional_class, p, dist, in_tun))
S = gpd.GeoDataFrame(rows, columns=["link", "name", "frc", "geometry", "dist_tlm", "near_tlm_tunnel"], crs=25832)
print(f"\nGIP stations in CH every 5 m: {len(S)}")
for q in (50, 90, 99):
    print(f"  distance to nearest TLM road p{q}: {np.percentile(S.dist_tlm, q):.1f} m")
for lim in (5, 10, 20, 50):
    print(f"  share > {lim} m: {(S.dist_tlm > lim).mean() * 100:.1f} %")
bad = S[S.dist_tlm > 10]
if len(bad):
    print("  stations > 10 m off, by link name:", collections.Counter(bad.name.fillna("-")).most_common(8))
    print("  of those near a TLM tunnel/gallery:", int(bad.near_tlm_tunnel.sum()))

# per Vinadi-Samnaun style road: the worst runs
runs = []
for link, grp in S.groupby("link"):
    m = grp.dist_tlm.to_numpy()
    if (m > 10).any():
        runs.append((link, grp.name.iloc[0], grp.frc.iloc[0], len(m) * 5, round(float(m.max()), 1),
                     round(float((m > 10).sum() * 5)), bool(grp.near_tlm_tunnel.any())))
if runs:
    print("\nlinks with stations > 10 m from TLM (link, name, frc, len in CH, max dist, m > 10, near TLM tunnel):")
    for r in sorted(runs, key=lambda r: -r[4])[:15]:
        print("  ", r)

# --- border crossings -------------------------------------------------------------------------
def crossings(gdf, idcol):
    out = []
    for _, r in gdf.iterrows():
        x = r.geometry.intersection(border)
        for p in getattr(x, "geoms", [x]):
            if not p.is_empty and p.geom_type == "Point":
                out.append((r[idcol], p))
    return out

cg = crossings(L, "object_id")
ct = crossings(T, "uuid")
print(f"\nborder crossings: GIP {len(cg)}, TLM {len(ct)}")
tp = [p for _, p in ct]
for oid, p in cg:
    d = min((p.distance(q) for q in tp), default=np.nan)
    name = L.loc[L.object_id == oid, "name_text_hr"].iloc[0]
    print(f"  GIP {oid} {name!s:30} nearest TLM crossing {d:7.1f} m")

S.to_file(OUT, layer="gip_in_ch_stations")
gpd.GeoDataFrame(geometry=[border], crs=25832).to_file(OUT, layer="border")
gch.to_file(OUT, layer="gip_links_in_ch")
