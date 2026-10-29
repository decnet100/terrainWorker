"""Check a lateral correction against the Swiss cadastral carriageway (AV LCSF Strasse_Weg).

For every station of the correction inside Switzerland, a ray along the local normal finds both edges of the
AV road polygon; the distance from the point to the middle of that chord is the error. Same for the original
GIP line (station minus its offset). Stations where the chord is missing or wider than 12 m are skipped.

    python tools\\check_lateral_fix_ch.py LATERAL_FIX_GPKG AV_GPKG AV_LAYER SWISSBOUNDARIES_GPKG [TLM_GPKG]

AV_GPKG: Swiss cadastral land cover with the class in field "art" (Strasse_Weg ...), e.g. the av_landcover file
of the site. TLM_GPKG (optional): swissTLM3D with layer tlm_strassen_strasse; stations within 25 m of a
tunnel, gallery or bridge are then left out.
"""
import sys
import geopandas as gpd
import numpy as np
import shapely

fix, lcsf, layer, bnd = sys.argv[1:5]
st = gpd.read_file(fix, layer="station")
links = gpd.read_file(fix, layer="link")
av = gpd.read_file(lcsf, layer=layer).to_crs(st.crs)
road = shapely.union_all(list(av[av["art"].isin(["Strasse_Weg", "Verkehrsinsel"])].geometry)).buffer(0)
shapely.prepare(road)
ch = gpd.read_file(bnd, layer="tlm_landesgebiet")
ch = shapely.union_all(list(ch[ch.icc == "CH"].to_crs(st.crs).geometry))
st = st[st.within(ch.buffer(-30)) & (st.structure == 0)].copy()
if len(sys.argv) > 5:   # swissTLM3D roads: leave out stations near a tunnel, gallery or bridge
    tlm = gpd.read_file(sys.argv[5], layer="tlm_strassen_strasse").to_crs(st.crs)
    kb = tlm[tlm.kunstbaute.isin(["Tunnel", "Galerie", "Bruecke", "Gedeckte Bruecke"])]
    st = st[~st.within(shapely.union_all(list(kb.geometry)).buffer(25))]

# normal from neighbouring stations of the same chain
rows = []
for chain, g in st.groupby("chain"):
    g = g.sort_values("s")
    p = np.array([[q.x, q.y] for q in g.geometry])
    if len(p) < 3:
        continue
    t = np.gradient(p, axis=0)
    t /= np.maximum(np.linalg.norm(t, axis=1, keepdims=True), 1e-9)
    n = np.stack([-t[:, 1], t[:, 0]], axis=1)
    for (i, r), pi, ni in zip(g.iterrows(), p, n):
        for kind, q in (("corrected", pi), ("gip", pi - ni * r.offset_m)):
            tt = np.arange(-10, 10.01, 0.1)
            xy = q[None, :] + ni[None, :] * tt[:, None]
            inside = shapely.contains_xy(road, xy[:, 0], xy[:, 1])
            if not inside.any():
                continue
            # the run of inside samples closest to the point
            idx = np.flatnonzero(inside)
            runs = np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1)
            run = min(runs, key=lambda r_: min(abs(tt[r_[0]]), abs(tt[r_[-1]])) if not (tt[r_[0]] <= 0 <= tt[r_[-1]]) else -1)
            if run[0] == 0 or run[-1] == len(tt) - 1:
                continue
            width = tt[run[-1]] - tt[run[0]]
            if width > 12 or width < 2:
                continue
            mid = 0.5 * (tt[run[0]] + tt[run[-1]])
            rows.append((r["rank"], chain, kind, abs(mid), width))
d = np.array(rows, dtype=object)
import pandas as pd
df = pd.DataFrame(rows, columns=["rank", "chain", "kind", "err", "width"])
print(f"stations in CH with an AV chord: {df[df.kind == 'gip'].shape[0]}")
print(df.groupby(["rank", "kind"]).err.describe(percentiles=[0.5, 0.9])[["count", "50%", "90%"]].round(2).to_string())
print("\nshare within 1.5 m of the AV centre (Magliasina MAX_SHIFT):")
print(df.assign(ok=df.err <= 1.5).groupby(["rank", "kind"]).ok.mean().round(3).to_string())
