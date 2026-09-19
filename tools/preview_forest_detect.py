"""PNG-only: sharp forest detect on the near Swissimage (no remesh, no inject).

Compares Tirol Waldfläche (cadastral), BEV vegetation_hoch, ESA WorldCover 10,
and DOM−DGM canopy, then a recommended combo:

  Waldfläche Hochwald | BEV hoch | (WorldCover 10 ∩ canopy ≥ 1 m)

Waldfläche is fetched for the near envelope (playable + 5 km), not only site.bbox.
CH/IT stays WC ∩ canopy.

Usage:
  cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = \"config/sites/reschen.yaml\"; python tools\\preview_forest_detect.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import requests
import tifffile as tiff
from PIL import Image, ImageDraw
from pyproj import Transformer
from scipy.ndimage import map_coordinates, zoom

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from build_backdrop import filter_near_canopy  # noqa: E402
from build_terrain_masks import _tirol_wald_kind  # noqa: E402
from fetch_backdrop import (  # noqa: E402
    _read_geotiff,
    backdrop_cfg,
    fetch_worldcover_tile,
    load_near_dgm,
    load_near_dom,
    near_extent,
    near_ortho_path,
    worldcover_path,
)
from fetch_bev_landcover import fetch_bev_near  # noqa: E402
from fetch_landcover import DEFAULT_LAYERS, PAGE, TIMEOUT  # noqa: E402
from site_coords import load_site, processed_dir, site_slug  # noqa: E402

_FOREST = np.array([0, 170, 40], dtype=np.uint8)
_SCRUB = np.array([180, 200, 20], dtype=np.uint8)


def _caption(im: Image.Image, text: str) -> Image.Image:
    pad = Image.new("RGB", (im.width, im.height + 32), (18, 18, 18))
    pad.paste(im, (0, 32))
    ImageDraw.Draw(pad).text((10, 8), text, fill=(235, 235, 235))
    return pad


def _paint(ortho: np.ndarray, mask: np.ndarray, rgb: np.ndarray) -> np.ndarray:
    vis = np.array(ortho, copy=True)
    vis[mask] = rgb
    return vis


def _near_tex_meta(site: dict, shape: tuple[int, int]) -> dict:
    xmin, ymin, xmax, ymax = near_extent(site)
    h, w = int(shape[0]), int(shape[1])
    return {
        "origin_x": float(xmin),
        "origin_y": float(ymax),
        "px": (xmax - xmin) / w,
        "py": -(ymax - ymin) / h,
        "width": w,
        "height": h,
    }


def _xy_grid(meta: dict, h: int, w: int) -> tuple[np.ndarray, np.ndarray]:
    ox, oy = float(meta["origin_x"]), float(meta["origin_y"])
    px, py = float(meta["px"]), float(meta["py"])
    xs = ox + (np.arange(w, dtype=np.float64) + 0.5) * px
    ys = oy + (np.arange(h, dtype=np.float64) + 0.5) * py
    return np.meshgrid(xs, ys)


def _zoom_bool(mask: np.ndarray, h: int, w: int) -> np.ndarray:
    if mask.shape == (h, w):
        return mask
    z = zoom(mask.astype(np.float32), (h / mask.shape[0], w / mask.shape[1]), order=0)
    return z >= 0.5


def _wald_cache_paths(site: dict) -> tuple[Path, Path]:
    slug = site_slug(site)
    nx0, ny0, nx1, ny1 = near_extent(site)
    stem = f"landcover_waldflaeche_near_{slug}_{int(nx0)}_{int(ny0)}_{int(nx1)}_{int(ny1)}"
    raw = ROOT / "data" / "raw"
    return raw / f"{stem}.geojson", raw / f"{stem}.meta.json"


def fetch_wald_near(site: dict) -> dict:
    """Waldfläche polygons for the near envelope (AT only; FeatureServer clip)."""
    geo_path, meta_path = _wald_cache_paths(site)
    if geo_path.is_file() and meta_path.is_file():
        cached = json.loads(meta_path.read_text(encoding="utf-8"))
        print(f"Using cache {geo_path.name} ({cached.get('feature_count', '?')} features)")
        return json.loads(geo_path.read_text(encoding="utf-8"))

    url = str(
        ((site.get("sources") or {}).get("landuse") or {}).get("layers", {}).get("waldflaeche", {}).get("url")
        or DEFAULT_LAYERS["waldflaeche"]["url"]
    )
    xmin, ymin, xmax, ymax = near_extent(site)
    geom = json.dumps(
        {
            "xmin": xmin,
            "ymin": ymin,
            "xmax": xmax,
            "ymax": ymax,
            "spatialReference": {"wkid": 31254},
        }
    )
    print(f"Fetching Waldfläche near {xmin:.0f},{ymin:.0f}–{xmax:.0f},{ymax:.0f}")
    feats: list[dict] = []
    offset = 0
    while True:
        params = {
            "f": "geojson",
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "true",
            "geometry": geom,
            "geometryType": "esriGeometryEnvelope",
            "inSR": 31254,
            "spatialRel": "esriSpatialRelIntersects",
            "outSR": 4326,
            "resultOffset": offset,
            "resultRecordCount": PAGE,
        }
        r = requests.get(f"{url.rstrip('/')}/query", params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(data["error"])
        page = data.get("features") or []
        print(f"  offset={offset} -> {len(page)}")
        feats.extend(page)
        if len(page) < PAGE:
            break
        offset += PAGE
    collection = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "features": feats,
    }
    geo_path.parent.mkdir(parents=True, exist_ok=True)
    geo_path.write_text(json.dumps(collection, ensure_ascii=False), encoding="utf-8")
    meta_path.write_text(
        json.dumps({"feature_count": len(feats), "extent": [xmin, ymin, xmax, ymax]}, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {geo_path.name} features={len(feats)}")
    return collection


def _rasterize_wald(geo: dict, dest_meta: dict, h: int, w: int) -> tuple[np.ndarray, np.ndarray]:
    to_xy = Transformer.from_crs("EPSG:4326", dest_meta.get("crs") or "EPSG:31254", always_xy=True)
    ox, oy = float(dest_meta["origin_x"]), float(dest_meta["origin_y"])
    px, py = float(dest_meta["px"]), float(dest_meta["py"])
    high_img = Image.new("L", (w, h), 0)
    scrub_img = Image.new("L", (w, h), 0)
    dh, ds = ImageDraw.Draw(high_img), ImageDraw.Draw(scrub_img)
    n_high = n_scrub = 0

    def rings(geom: dict) -> list[list[tuple[float, float]]]:
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
                x, y = to_xy.transform(float(c[0]), float(c[1]))
                pts.append(((x - ox) / px, (y - oy) / py))
            if len(pts) >= 3:
                out.append(pts)
        return out

    for feat in geo.get("features") or []:
        kind = _tirol_wald_kind(feat.get("properties") or {})
        if not kind:
            continue
        polys = rings(feat.get("geometry") or {})
        if not polys:
            continue
        draw = dh if kind == "forest_high" else ds
        for pts in polys:
            draw.polygon(pts, outline=1, fill=1)
        if kind == "forest_high":
            n_high += 1
        else:
            n_scrub += 1
    print(f"Waldfläche raster: hoch={n_high} strauch={n_scrub}")
    return np.array(high_img) > 0, np.array(scrub_img) > 0


def _sample_worldcover(site: dict, dest_meta: dict, h: int, w: int) -> np.ndarray:
    tile = fetch_worldcover_tile(site, force=False)
    xx, yy = _xy_grid(dest_meta, h, w)
    to_wgs = Transformer.from_crs(str(site.get("crs", "EPSG:31254")), "EPSG:4326", always_xy=True)
    if tile is not None and tile.is_file():
        src = _read_geotiff(tile)
        lon, lat = to_wgs.transform(xx.ravel(), yy.ravel())
        lon = np.asarray(lon, dtype=np.float64).reshape(xx.shape)
        lat = np.asarray(lat, dtype=np.float64).reshape(yy.shape)
        col = (lon - src.origin_x) / src.px - 0.5
        row = (lat - src.origin_y) / src.py - 0.5
        codes = map_coordinates(
            np.asarray(src.data, dtype=np.float64),
            np.vstack([row.ravel(), col.ravel()]),
            order=0,
            mode="nearest",
        ).reshape(h, w)
        return np.rint(codes).astype(np.uint8)

    wc_p = worldcover_path(site)
    if not wc_p.is_file():
        raise SystemExit("WorldCover missing — run fetch_backdrop")
    wc = np.asarray(tiff.imread(wc_p))
    print(f"WorldCover fallback resize {wc.shape} -> {(h, w)}")
    return np.array(Image.fromarray(wc).resize((w, h), Image.Resampling.NEAREST))


def _align_bev(site: dict, dest_meta: dict, h: int, w: int) -> np.ndarray | None:
    path = fetch_bev_near(site)
    meta_path = path.with_suffix(".meta.json")
    if not path.is_file():
        print("BEV near landcover missing")
        return None
    bev = np.asarray(tiff.imread(path))
    if bev.ndim == 3:
        bev = bev[..., 0]
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        bx0, by0, bx1, by1 = map(float, meta["bbox"])
    else:
        bx0, by0, bx1, by1 = near_extent(site)
    bh, bw = int(bev.shape[0]), int(bev.shape[1])
    src_meta = {
        "origin_x": bx0,
        "origin_y": by1,
        "px": (bx1 - bx0) / bw,
        "py": -(by1 - by0) / bh,
    }
    xx, yy = _xy_grid(dest_meta, h, w)
    col = (xx - src_meta["origin_x"]) / src_meta["px"] - 0.5
    row = (yy - src_meta["origin_y"]) / src_meta["py"] - 0.5
    inside = (row >= 0) & (row <= bh - 1) & (col >= 0) & (col <= bw - 1)
    out = np.full((h, w), 6, dtype=np.uint8)  # nodata
    if not np.any(inside):
        return out
    sampled = map_coordinates(
        bev.astype(np.float64),
        np.vstack([row[inside], col[inside]]),
        order=0,
        mode="nearest",
    )
    out[inside] = np.rint(sampled).astype(np.uint8)
    return out


def _canopy_mask(site: dict, cfg: dict, h: int, w: int) -> np.ndarray:
    dgm_pack = load_near_dgm(site)
    dom_pack = load_near_dom(site)
    if dgm_pack is None or dom_pack is None:
        print("near DGM/DOM missing — canopy empty")
        return np.zeros((h, w), dtype=bool)
    dgm, dgm_meta = dgm_pack
    dom, dom_meta = dom_pack
    if dom.shape != dgm.shape:
        from build_backdrop import _align_to_grid

        dom = _align_to_grid(dom, dom_meta, dgm.shape, dgm_meta)
    both = np.isfinite(dgm) & np.isfinite(dom)
    ndsm = np.zeros(dgm.shape, dtype=np.float32)
    ndsm[both] = np.maximum(dom[both] - dgm[both], 0.0).astype(np.float32)
    cell = abs(float(dgm_meta["px"]))
    canopy = filter_near_canopy(ndsm, cell, cfg)
    return _zoom_bool(canopy >= 1.0, h, w)


def _thumb_row(items: list[tuple[Image.Image, str]], tw: int) -> Image.Image:
    thumbs = []
    for im, cap in items:
        t = _caption(im, cap)
        t.thumbnail((tw, tw + 40))
        thumbs.append(t)
    sheet = Image.new("RGB", (tw * len(thumbs), thumbs[0].height), (18, 18, 18))
    for i, th in enumerate(thumbs):
        sheet.paste(th, (i * tw, 0))
    return sheet


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    cfg = backdrop_cfg(site)
    op = near_ortho_path(site)
    if not op.is_file():
        raise SystemExit(f"missing {op}")
    ortho = np.asarray(Image.open(op).convert("RGB"))
    h, w = ortho.shape[:2]
    dest_meta = _near_tex_meta(site, (h, w))
    dest_meta["crs"] = str(site.get("crs", "EPSG:31254"))
    print(f"near ortho {ortho.shape}  px={dest_meta['px']:.2f} m")

    wald_geo = fetch_wald_near(site)
    wald_high, wald_scrub = _rasterize_wald(wald_geo, dest_meta, h, w)
    wald = wald_high | wald_scrub

    wc = _sample_worldcover(site, dest_meta, h, w)
    wc10 = wc == 10
    wc20 = wc == 20

    bev = _align_bev(site, dest_meta, h, w)
    bev_hoch = (bev == 0) if bev is not None else np.zeros((h, w), dtype=bool)
    bev_mittel = (bev == 1) if bev is not None else np.zeros((h, w), dtype=bool)
    bev_valid = (bev <= 5) if bev is not None else np.zeros((h, w), dtype=bool)
    bev_forest = bev_hoch | bev_mittel

    canopy = _canopy_mask(site, cfg, h, w)
    # BEV wins in AT; CH/IT (nodata) fall back to Wald / WC∩canopy.
    combo = bev_forest | ((~bev_valid) & (wald_high | (wc10 & canopy)))

    layers = [
        ("orig", ortho, "Ursprung Swissimage near"),
        ("wald", _paint(ortho, wald, _FOREST), f"Waldflaeche near  {100.0 * float(wald.mean()):.1f}%"),
        ("bev", _paint(ortho, bev_forest, _FOREST), f"BEV near hoch+mittel  {100.0 * float(bev_forest.mean()):.1f}%"),
        ("wc10", _paint(ortho, wc10, _FOREST), f"WorldCover 10  {100.0 * float(wc10.mean()):.1f}%"),
        ("canopy", _paint(ortho, canopy, _FOREST), f"Canopy >=1 m  {100.0 * float(canopy.mean()):.1f}%"),
        ("combo", _paint(ortho, combo, _FOREST), f"Combo BEV | (!AT & wald/WC)  {100.0 * float(combo.mean()):.1f}%"),
    ]
    written: list[Path] = []
    tiles: list[tuple[Image.Image, str]] = []
    for key, arr, cap in layers:
        path = proc / f"preview_forest_{key}.png"
        Image.fromarray(arr, mode="RGB").save(path)
        written.append(path)
        tiles.append((Image.fromarray(arr, mode="RGB"), cap))
        print(f"  {cap} -> {path.name}")

    sheet = _thumb_row(tiles, 640)
    sheet_p = proc / "preview_forest_sheet.png"
    sheet.save(sheet_p)
    written.append(sheet_p)

    # Two-class combo: hoch vs strauch (opaque, no photo blend).
    two = np.array(ortho, copy=True)
    two[wald_scrub | wc20 | bev_mittel] = _SCRUB
    two[combo] = _FOREST
    two_p = proc / "preview_forest_combo_classes.png"
    Image.fromarray(two, mode="RGB").save(two_p)
    written.append(two_p)
    print(
        f"classes hoch={100.0 * float(combo.mean()):.1f}%  "
        f"strauch={100.0 * float(((wald_scrub | wc20 | bev_mittel) & ~combo).mean()):.1f}%  "
        f"-> {two_p.name}"
    )
    print("Wrote " + ", ".join(p.name for p in written))


if __name__ == "__main__":
    main()
