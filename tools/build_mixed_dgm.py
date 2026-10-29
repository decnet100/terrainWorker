"""Mixed Tirol / Swiss DGM for a border site, at 0.5 m and 1 m, in the site CRS.

Tirol: the native WCS mosaic that fetch_dgm.py wrote (data/raw/dgm_<site>_native.tif, fetched in the site CRS;
the WCS's own reprojection sits within about 0.3 m of swissALTI3D). Switzerland: swissALTI3D 0.5 m tiles
(fetch_swissalti3d.py), warped with GDAL. Inside Switzerland (swissBOUNDARIES3D) the Swiss model is used,
outside the Tirol one, each filling the other's gaps. Tirol is lifted by the median height difference on
flat overlap (slope under 10 %); on Pfunds-Spiss that was 0.07 m.

Writes data/raw/dgm_<site>_mixed05.tif (0.5 m, for fix_lateral_rank.py) and data/raw/dgm_<site>_mixed.tif
(1 m, blended over 60 m at the border, with the .aux.xml GeoTransform mask_dgm_by_links.py reads).

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\build_mixed_dgm.py --swiss data\\raw\\swissalti3d_05 --borders data\\raw\\borders\\swissBOUNDARIES3D_1_5_LV95_LN02.gpkg
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import rasterize
from scipy.ndimage import distance_transform_edt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from site_coords import load_site, site_slug  # noqa: E402

BLEND_M = 30.0   # each side of the border, 1 m product only


def warp(src: str, dst: Path, crs: str, bounds, res: float, resampling: str, src_crs: str | None = None) -> None:
    cmd = ["gdalwarp", "-q", "-overwrite", "-t_srs", crs, "-te", *map(str, bounds), "-tr", str(res), str(res),
           "-r", resampling, "-dstnodata", "nan", "-ot", "Float32", "-wm", "2000", "-multi",
           "-co", "TILED=YES", "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=3"]
    if src_crs:
        cmd[3:3] = ["-s_srs", src_crs]
    subprocess.run(cmd + [src, str(dst)], check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--swiss", required=True, help="folder with swissALTI3D *_0.5_2056_5728.tif tiles")
    ap.add_argument("--borders", required=True, help="swissBOUNDARIES3D GeoPackage (layer tlm_landesgebiet)")
    args = ap.parse_args()
    site = load_site()
    slug = site_slug(site)
    crs = str(site.get("crs") or "EPSG:25832")
    raw = ROOT / "data" / "raw"
    work = raw / "mixed"
    work.mkdir(parents=True, exist_ok=True)
    meta = json.loads((raw / f"dgm_{slug}_native.meta.json").read_text(encoding="utf-8"))
    nx0, ny0, nx1, ny1 = (float(v) for v in meta["bbox"])
    res0 = float(meta["resolution_m"])
    tirol_vrt = work / "tirol_native.vrt"
    subprocess.run(["gdal_translate", "-q", "-of", "VRT", "-a_srs", crs, "-a_ullr", str(nx0), str(ny1), str(nx1),
                    str(ny0), "-a_nodata", "nan", str(raw / f"dgm_{slug}_native.tif"), str(tirol_vrt)], check=True)
    tiles = sorted(str(p) for p in Path(args.swiss).glob("*_0.5_2056_5728.tif"))
    swiss_vrt = work / "swissalti.vrt"
    subprocess.run(["gdalbuildvrt", "-q", "-overwrite", str(swiss_vrt), *tiles], check=True)
    ch = gpd.read_file(args.borders, layer="tlm_landesgebiet")
    ch = ch[ch["icc"] == "CH"].to_crs(crs)

    # 0.5 m on the native Tirol grid
    swiss05 = work / "swiss_05.tif"
    warp(str(swiss_vrt), swiss05, crs, (nx0, ny0, nx1, ny1), res0, "bilinear", "EPSG:2056")
    with rasterio.open(tirol_vrt) as t:
        a = t.read(1)
        tf = t.transform
    s = rasterio.open(swiss05).read(1)
    inch = rasterize([(g, 1) for g in ch.geometry], out_shape=a.shape, transform=tf, fill=0).astype(bool)
    gy, gx = np.gradient(s, res0)
    flat = (np.hypot(gx, gy) < 0.1) & np.isfinite(a) & np.isfinite(s)
    off = float(np.median((a - s)[flat])) if flat.any() else 0.0
    print(f"height offset Tirol - Swiss on flat overlap: {off:+.3f} m ({int(flat.sum())} cells)", flush=True)
    a = a - np.float32(off)
    out = np.where(inch, np.where(np.isfinite(s), s, a), np.where(np.isfinite(a), a, s)).astype(np.float32)
    prof = dict(driver="GTiff", height=out.shape[0], width=out.shape[1], count=1, dtype="float32", crs=crs,
                transform=tf, nodata=np.nan, tiled=True, compress="deflate", predictor=3, BIGTIFF="IF_SAFER")
    with rasterio.open(raw / f"dgm_{slug}_mixed05.tif", "w", **prof) as dst:
        dst.write(out, 1)
    del out, s

    # 1 m on the site bbox, blended at the border
    bx = tuple(float(v) for v in site["bbox"])
    t1, s1 = work / "tirol_1m.tif", work / "swiss_1m.tif"
    warp(str(tirol_vrt), t1, crs, bx, 1.0, "average")
    warp(str(swiss_vrt), s1, crs, bx, 1.0, "average", "EPSG:2056")
    with rasterio.open(t1) as t:
        a = t.read(1).astype(np.float64) - off
        tf, prof = t.transform, t.profile
    s = rasterio.open(s1).read(1).astype(np.float64)
    inch = rasterize([(g, 1) for g in ch.geometry], out_shape=a.shape, transform=tf, fill=0).astype(bool)
    signed = np.where(inch, distance_transform_edt(inch), -distance_transform_edt(~inch))
    w = np.clip((signed + BLEND_M) / (2 * BLEND_M), 0, 1)
    out = np.where(np.isfinite(a) & np.isfinite(s), w * s + (1 - w) * a, np.where(np.isfinite(s), s, a)).astype(np.float32)
    prof.update(dtype="float32", nodata=np.nan, compress="deflate", tiled=True)
    dest = raw / f"dgm_{slug}_mixed.tif"
    with rasterio.open(dest, "w", **prof) as dst:
        dst.write(out, 1)
    Path(str(dest) + ".aux.xml").write_text(
        f"<PAMDataset>\n  <SRS>{crs}</SRS>\n  <GeoTransform>{tf.c}, {tf.a}, 0, {tf.f}, 0, {tf.e}</GeoTransform>\n</PAMDataset>\n",
        encoding="utf-8")
    print(f"wrote {raw / f'dgm_{slug}_mixed05.tif'} and {dest}; nodata {np.isnan(out).mean() * 100:.2f} %")


if __name__ == "__main__":
    main()
