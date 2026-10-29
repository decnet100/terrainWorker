"""Measured and GIP carriageway width against the Swiss cadastral carriageway (AV LCSF Strasse_Weg).

    python tools\\check_lateral_width_ch.py LATERAL_FIX_GPKG AV_GPKG AV_LAYER SWISSBOUNDARIES_GPKG [TLM_GPKG [DGM]]

TLM_GPKG as in check_lateral_fix_ch.py. DGM (optional, the 0.5 m mixed DGM): also reports how the error splits
between cadastral edges with and without a terrain knee.
"""
import sys
import rasterio
from scipy.ndimage import map_coordinates
import geopandas as gpd, numpy as np, shapely, pandas as pd
fix, lcsf, layer, bnd = sys.argv[1:5]
st = gpd.read_file(fix, layer="station")
av = gpd.read_file(lcsf, layer=layer).to_crs(st.crs)
road = shapely.union_all(list(av[av["art"].isin(["Strasse_Weg"])].geometry)).buffer(0)
ch = gpd.read_file(bnd, layer="tlm_landesgebiet")
ch = shapely.union_all(list(ch[ch.icc == "CH"].to_crs(st.crs).geometry))
st = st[st.within(ch.buffer(-30)) & (st.structure == 0)]
if len(sys.argv) > 5:   # swissTLM3D roads: leave out stations near a tunnel, gallery or bridge
    tlm = gpd.read_file(sys.argv[5], layer="tlm_strassen_strasse").to_crs(st.crs)
    kb = tlm[tlm.kunstbaute.isin(["Tunnel", "Galerie", "Bruecke", "Gedeckte Bruecke"])]
    near = shapely.union_all(list(kb.geometry)).buffer(25)
    before = len(st); st = st[~st.within(near)]
    print(f"left out {before - len(st)} stations within 25 m of a TLM structure")
dgm = rasterio.open(sys.argv[6]) if len(sys.argv) > 6 else None
Z = dgm.read(1) if dgm else None
inv = ~dgm.transform if dgm else None


def knee(xy_in, xy_out):
    """Slope change across an edge: 1 m inside against 1 m outside (only with a DGM)."""
    if Z is None:
        return np.nan
    out = []
    for xy in (xy_in, xy_out):
        col, row = inv * (xy[:, 0], xy[:, 1])
        z = map_coordinates(Z, [row - 0.5, col - 0.5], order=1, cval=np.nan)
        out.append(np.polyfit(np.arange(len(z)) * 0.1, z, 1)[0])
    return abs(out[1] - out[0])


rows = []
for chain, g in st.groupby("chain"):
    g = g.sort_values("s"); p = np.array([[q.x, q.y] for q in g.geometry])
    if len(p) < 3:
        continue
    t = np.gradient(p, axis=0); t /= np.linalg.norm(t, axis=1, keepdims=True); n = np.stack([-t[:, 1], t[:, 0]], 1)
    for (i, r), pi, ni in zip(g.iterrows(), p, n):
        tt = np.arange(-10, 10.01, 0.1); xy = pi + ni * tt[:, None]; ins = shapely.contains_xy(road, xy[:, 0], xy[:, 1])
        if not ins[100]:
            continue
        a = 100
        while a > 0 and ins[a - 1]:
            a -= 1
        b = 100
        while b < len(tt) - 1 and ins[b + 1]:
            b += 1
        if a == 0 or b == len(tt) - 1 or tt[b] - tt[a] > 12:
            continue
        kl = knee(xy[b - 10:b + 1], xy[b:b + 11]) if b + 11 <= len(tt) and b >= 10 else np.nan
        kr = knee(xy[a:a + 11], xy[a - 10:a + 1]) if a >= 10 and a + 11 <= len(tt) else np.nan
        rows.append(dict(rank=r["rank"], av_w=tt[b] - tt[a], av_l=tt[b], av_r=-tt[a], w=r.width_m, gip=r.width_gip_m,
                         hl=r.half_left_m, hr=r.half_right_m, knee=np.nanmin([kl, kr])))
df = pd.DataFrame(rows)
df["err_meas"] = df.w - df.av_w; df["err_gip"] = df.gip - df.av_w
print("stations", len(df))
for r, g in df.groupby("rank"):
    print(f"{r}: AV width median {g.av_w.median():.2f} | measured {g.w.median():.2f}, GIP {g.gip.median():.2f}, corr {np.corrcoef(g.w, g.av_w)[0, 1]:.2f}")
    for k in ("err_meas", "err_gip"):
        e = g[k]
        print(f"   {k:9s} median {e.median():+.2f}  |err| median {e.abs().median():.2f}  p90 {e.abs().quantile(.9):.2f}  within 1 m {np.mean(e.abs() <= 1) * 100:.0f}%")
    print(f"   edges measured-AV: left {(g.hl - g.av_l).median():+.2f}, right {(g.hr - g.av_r).median():+.2f}")
    print("   measured-AV quantiles 10/25/50/75/90:", np.round(np.percentile(g.err_meas, [10, 25, 50, 75, 90]), 2),
          f" share < -2 m {np.mean(g.err_meas < -2) * 100:.0f}%")
    if g.knee.notna().any():
        weak = g.knee < 0.15
        print(f"   AV edge without a terrain knee (< 15 % slope change on the weaker side): {weak.mean() * 100:.0f}% of stations;"
              f" |err| median there {g.err_meas[weak].abs().median():.2f} m, elsewhere {g.err_meas[~weak].abs().median():.2f} m;"
              f" within 1 m elsewhere {np.mean(g.err_meas[~weak].abs() <= 1) * 100:.0f}% (GIP {np.mean(g.err_gip[~weak].abs() <= 1) * 100:.0f}%)")
