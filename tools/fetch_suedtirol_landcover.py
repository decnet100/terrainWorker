"""Fetch Südtirol INSPIRE landcover (CORINE) and rasterize catalog codes.

WFS: ``p_bz-Inspire:LC.LandCoverSurfaces`` on geoservices8 (EPSG:25832).
Writes the **catalog** raster (raw CORINE CODE), not a collapsed BEV class:

  data/raw/landcover_corine_<site>.geojson
  data/processed/<site>/landcover_codes.tif          (uint16, 0=nodata)
  data/processed/<site>/landcover_codes.meta.json
  data/processed/<site>/landcover_treatment.tif      (uint8 treatment ids)
  data/processed/<site>/preview_landcover_codes.png

Treatment groups come from ``config/landcover_treatment.yaml`` (catalog corine).

Usage:
  cd C:\\temp\\beamng_autoroad
  $env:AUTOROAD_SITE = \"config/sites/timmelsjoch.yaml\"
  python tools\\fetch_suedtirol_landcover.py
  python tools\\fetch_suedtirol_landcover.py --force
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import tifffile as tiff
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from landcover_catalog import (  # noqa: E402
    apply_treatment_ids,
    codes_meta_path,
    codes_path,
    load_treatment,
    nodata_value,
    treatment_raster_path,
    write_codes_meta,
)
from site_coords import load_site, processed_dir, site_slug  # noqa: E402

DEFAULT_URL = "https://geoservices8.civis.bz.it/geoserver/ows"
DEFAULT_TYPE = "p_bz-Inspire:LC.LandCoverSurfaces"
DEFAULT_VERSION = "2.0.0"
PAGE = 500
HEADERS = {"User-Agent": "beamng_autoroad/0.1 (landcover)", "Accept": "*/*"}

# Distinct preview colours per CORINE code (cyclic fallback for unknowns).
_PREVIEW_RGB = {
    231: (120, 200, 80),
    311: (20, 90, 30),
    312: (15, 70, 25),
    313: (30, 100, 40),
    321: (140, 210, 90),
    322: (100, 140, 60),
    324: (70, 130, 50),
    331: (210, 200, 140),
    332: (160, 160, 165),
    333: (180, 160, 110),
    335: (230, 240, 250),
    111: (200, 50, 50),
    112: (220, 80, 80),
    511: (40, 100, 200),
    512: (50, 120, 210),
}


def _cfg(site: dict) -> dict:
    src = (site.get("sources") or {}).get("landcover") or {}
    bng = site.get("beamng") or {}
    return {
        "url": str(src.get("url") or DEFAULT_URL),
        "type_name": str(src.get("type_name") or src.get("typename") or DEFAULT_TYPE),
        "version": str(src.get("version") or DEFAULT_VERSION),
        "srs": str(src.get("srs") or src.get("crs") or site.get("crs") or "EPSG:25832"),
        "code_field": str(src.get("code_field") or "CODE"),
        "label_field": str(src.get("label_field") or "LAND_COVER_OBSERVATION"),
        "catalog": str(src.get("catalog") or "corine"),
        "size": int(src.get("size") or bng.get("mask_size") or 2048),
        "timeout_s": int(src.get("timeout_s") or 120),
        "page": int(src.get("page") or PAGE),
    }


def raw_geojson_path(site: dict) -> Path:
    return ROOT / "data" / "raw" / f"landcover_corine_{site_slug(site)}.geojson"


def _http_json(url: str, *, timeout_s: int) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        data = r.read().decode("utf-8", "replace")
    if data.lstrip().startswith("<"):
        raise SystemExit(f"WFS returned XML/error: {data[:400]}")
    return json.loads(data)


def fetch_geojson(site: dict, cfg: dict, *, force: bool = False) -> dict:
    out = raw_geojson_path(site)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.is_file() and out.stat().st_size > 100 and not force:
        print(f"Using cached landcover GeoJSON: {out}")
        return json.loads(out.read_text(encoding="utf-8"))

    xmin, ymin, xmax, ymax = (float(v) for v in site["bbox"])
    srs = cfg["srs"]
    features: list[dict] = []
    start = 0
    total = None
    while True:
        params = {
            "service": "WFS",
            "version": cfg["version"],
            "request": "GetFeature",
            "typeNames": cfg["type_name"],
            "srsName": srs,
            "bbox": f"{xmin},{ymin},{xmax},{ymax},{srs}",
            "count": str(cfg["page"]),
            "startIndex": str(start),
            "outputFormat": "application/json",
        }
        url = cfg["url"] + ("&" if "?" in cfg["url"] else "?") + urllib.parse.urlencode(params)
        print(f"WFS GetFeature startIndex={start} …")
        blob = _http_json(url, timeout_s=cfg["timeout_s"])
        batch = blob.get("features") or []
        if total is None:
            total = blob.get("totalFeatures") or blob.get("numberMatched")
            print(f"numberMatched={total}")
        features.extend(batch)
        start += len(batch)
        if not batch:
            break
        if total is not None and start >= int(total):
            break
        if start > 100_000:
            raise SystemExit("Landcover WFS pagination exceeded 100k features — abort")

    fc = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": srs}},
        "features": features,
    }
    out.write_text(json.dumps(fc), encoding="utf-8")
    print(f"Wrote {out} features={len(features)}")
    return fc


def _rings_px(
    geom: dict,
    *,
    xmin: float,
    ymax: float,
    res: float,
    size: int,
) -> list[list[tuple[float, float]]]:
    gtype = (geom or {}).get("type")
    coords = (geom or {}).get("coordinates")
    raw: list = []
    if gtype == "Polygon" and coords:
        raw.append(coords[0])
    elif gtype == "MultiPolygon" and coords:
        for poly in coords:
            if poly:
                raw.append(poly[0])
    out: list[list[tuple[float, float]]] = []
    for ring in raw:
        pts: list[tuple[float, float]] = []
        for c in ring:
            if len(c) < 2:
                continue
            x, y = float(c[0]), float(c[1])
            # row 0 = north (ymax)
            col = (x - xmin) / res
            row = (ymax - y) / res
            pts.append((col, row))
        if len(pts) >= 3:
            out.append(pts)
    return out


def rasterize_codes(
    fc: dict,
    site: dict,
    cfg: dict,
    *,
    nodata: int,
) -> np.ndarray:
    size = int(cfg["size"])
    xmin, ymin, xmax, ymax = (float(v) for v in site["bbox"])
    res_x = (xmax - xmin) / size
    res_y = (ymax - ymin) / size
    if abs(res_x - res_y) > 1e-6:
        print(f"WARNING: non-square cells res_x={res_x} res_y={res_y} — using res_x")
    res = res_x
    code_field = cfg["code_field"]
    grid = np.full((size, size), nodata, dtype=np.uint16)
    painted = 0
    skipped = 0
    for feat in fc.get("features") or []:
        props = feat.get("properties") or {}
        raw = props.get(code_field)
        try:
            code = int(float(raw))
        except (TypeError, ValueError):
            skipped += 1
            continue
        if code == nodata:
            skipped += 1
            continue
        rings = _rings_px(
            feat.get("geometry") or {},
            xmin=xmin,
            ymax=ymax,
            res=res,
            size=size,
        )
        if not rings:
            skipped += 1
            continue
        # Paint into a temp mask then assign code (later features overwrite).
        img = Image.new("L", (size, size), 0)
        draw = ImageDraw.Draw(img)
        for pts in rings:
            draw.polygon(pts, outline=1, fill=1)
        mask = np.asarray(img, dtype=np.uint8) > 0
        if not mask.any():
            continue
        grid[mask] = np.uint16(code)
        painted += 1
    print(f"Rasterized polygons painted={painted} skipped={skipped}")
    return grid


def _preview_rgb(codes: np.ndarray, nodata: int) -> np.ndarray:
    h, w = codes.shape
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    rgb[:] = (30, 30, 35)
    for code in np.unique(codes):
        c = int(code)
        if c == nodata:
            continue
        colour = _PREVIEW_RGB.get(c)
        if colour is None:
            # Stable pseudo-colour from code digits.
            colour = (
                40 + (c * 37) % 180,
                40 + (c * 91) % 180,
                40 + (c * 17) % 180,
            )
        rgb[codes == c] = colour
    return rgb


def fetch_suedtirol_landcover(site: dict, *, force: bool = False) -> Path:
    cfg = _cfg(site)
    proc = processed_dir(site)
    proc.mkdir(parents=True, exist_ok=True)
    out = codes_path(proc)
    meta_p = codes_meta_path(proc)
    treat_p = treatment_raster_path(proc)

    if (
        out.is_file()
        and meta_p.is_file()
        and treat_p.is_file()
        and not force
        and out.stat().st_size > 100
    ):
        print(f"Using cached landcover codes: {out}")
        return out

    treat = load_treatment()
    nodata = nodata_value(treat)
    fc = fetch_geojson(site, cfg, force=force)
    codes = rasterize_codes(fc, site, cfg, nodata=nodata)
    tiff.imwrite(out, codes, compression="deflate")
    write_codes_meta(
        meta_p,
        catalog=cfg["catalog"],
        codes=codes,
        site=site,
        source={
            "type": "wfs",
            "url": cfg["url"],
            "type_name": cfg["type_name"],
            "version": cfg["version"],
            "srs": cfg["srs"],
            "code_field": cfg["code_field"],
            "geojson": str(raw_geojson_path(site).relative_to(ROOT)).replace("\\", "/"),
        },
        treatment=treat,
    )
    treatment = apply_treatment_ids(codes, catalog=cfg["catalog"], treatment=treat)
    tiff.imwrite(treat_p, treatment, compression="deflate")
    preview = proc / "preview_landcover_codes.png"
    Image.fromarray(_preview_rgb(codes, nodata), mode="RGB").save(preview)
    print(f"Wrote {out}")
    print(f"Wrote {meta_p}")
    print(f"Wrote {treat_p}")
    print(f"Wrote {preview}")
    covered = float((codes != nodata).mean() * 100.0)
    print(f"Catalog coverage={covered:.2f}% catalog={cfg['catalog']}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="Re-download and re-rasterize")
    args = ap.parse_args()
    site = load_site()
    print(f"Site: {site_slug(site)}")
    fetch_suedtirol_landcover(site, force=args.force)


if __name__ == "__main__":
    main()
