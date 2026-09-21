"""Classify OSM buildings from footprint + DOM roof planes (no mesh changes).

For each footprint: inset, nDSM = DOM−DGM, slope/aspect regions, then
footprint class + roof class + cheap instance parameters (yaw, pitch bin,
storeys, chimney).

Writes under data/processed/<site>/:
  building_roof_class.json
  building_roof_class.geojson   (WGS84 — geojson.io / QGIS)
  building_roof_class.kml       (Google Earth / My Maps)
  building_roof_class.csv
  building_roof_class.html      (class counts + Google-Satellite links)
  preview_building_roof_class.png

Usage:
  $env:AUTOROAD_SITE='config/sites/reschen.yaml'
  python tools/classify_building_roofs.py
"""
from __future__ import annotations

import csv
import json
import math
import sys
from collections import Counter
from html import escape
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import numpy as np
import tifffile as tiff
from PIL import Image, ImageDraw
from pyproj import Transformer
from scipy import ndimage
from shapely.geometry.polygon import orient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from build_buildings import (  # noqa: E402
    _cfg,
    _obb_corners,
    _parse_height_m,
    _poly_from_ring,
    _way_ring_crs,
    fetch_osm_buildings,
)
from fetch_dgm import dgm_cache_path  # noqa: E402
from fetch_dom import dom_cache_path  # noqa: E402
from site_coords import SiteCoords, load_site, processed_dir, site_slug  # noqa: E402

FLAT_DEG = 8.0
PITCHED_DEG = 12.0
MIN_REGION_PX = 8
INSET_M = 1.2
CHIMNEY_DZ = 1.6
CHIMNEY_MAX_PX = 12
SPECIAL_TYPES = {
    "church",
    "chapel",
    "cathedral",
    "mosque",
    "synagogue",
    "hotel",
}
SPECIAL_NAME = ("hotel", "kirche", "stift", "pfarr", "kloster")
PITCH_BINS = (20, 30, 40)

# RGB for preview; KML uses the same (converted to AABBGGRR).
CLASS_RGB = {
    "rect_gable_long": (40, 170, 70),
    "rect_gable_cross": (230, 190, 35),
    "rect_flat": (155, 155, 160),
    "rect_hip": (230, 120, 35),
    "rect_shed": (50, 185, 205),
    "near_rect_gable": (95, 145, 95),
    "special": (210, 45, 185),
    "complex": (205, 55, 55),
    "too_few": (80, 80, 80),
}


def _load_elev(path: Path) -> np.ndarray:
    arr = np.asarray(tiff.imread(path), dtype=np.float32)
    if arr.ndim != 2:
        raise SystemExit(f"Expected 2D raster: {path} shape={arr.shape}")
    return arr


def _slope_aspect(dom: np.ndarray, cell: float) -> tuple[np.ndarray, np.ndarray]:
    """Slope degrees and downhill aspect (0=N, 90=E, clockwise). Row0 = north."""
    gy = np.gradient(dom, -cell, axis=0)  # dZ/dy, y increases north
    gx = np.gradient(dom, cell, axis=1)  # dZ/dx, x increases east
    slope = np.degrees(np.arctan(np.hypot(gx, gy))).astype(np.float32)
    aspect = (np.degrees(np.arctan2(-gx, -gy)) + 360.0) % 360.0
    return slope, aspect.astype(np.float32)


def _heading_north_cw(dx: float, dy: float) -> float:
    """Geographic heading 0=N, clockwise, undirected 0–180."""
    return (90.0 - math.degrees(math.atan2(dy, dx))) % 180.0


def _ang_diff_180(a: float, b: float) -> float:
    d = abs((a - b) % 180.0)
    return min(d, 180.0 - d)


def _ang_diff_360(a: float, b: float) -> float:
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def _pixels_in_poly(
    poly, xmin: float, ymax: float, cell: float, h: int, w: int
) -> tuple[np.ndarray, np.ndarray]:
    if poly.is_empty:
        return np.zeros(0, np.int32), np.zeros(0, np.int32)
    minx, miny, maxx, maxy = poly.bounds
    c0 = max(0, int(math.floor((minx - xmin) / cell)) - 1)
    c1 = min(w, int(math.ceil((maxx - xmin) / cell)) + 2)
    r0 = max(0, int(math.floor((ymax - maxy) / cell)) - 1)
    r1 = min(h, int(math.ceil((ymax - miny) / cell)) + 2)
    if c1 <= c0 or r1 <= r0:
        return np.zeros(0, np.int32), np.zeros(0, np.int32)
    patch = Image.new("L", (c1 - c0, r1 - r0), 0)
    draw = ImageDraw.Draw(patch)
    geoms = list(poly.geoms) if poly.geom_type == "MultiPolygon" else [poly]
    for g in geoms:
        if g.is_empty:
            continue
        pts = [((x - xmin) / cell - c0, (ymax - y) / cell - r0) for x, y in g.exterior.coords]
        if len(pts) >= 3:
            draw.polygon(pts, fill=1)
        for hole in g.interiors:
            hpts = [((x - xmin) / cell - c0, (ymax - y) / cell - r0) for x, y in hole.coords]
            if len(hpts) >= 3:
                draw.polygon(hpts, fill=0)
    arr = np.asarray(patch)
    rr, cc = np.nonzero(arr)
    return (rr + r0).astype(np.int32), (cc + c0).astype(np.int32)


def _regions(
    rows: np.ndarray,
    cols: np.ndarray,
    slope: np.ndarray,
    aspect: np.ndarray,
) -> list[dict]:
    if rows.size == 0:
        return []
    sl = slope[rows, cols]
    asp = aspect[rows, cols]
    kind = np.where(sl < FLAT_DEG, 0, 1 + (asp / 45.0).astype(np.int32) % 8)
    r0, c0 = int(rows.min()), int(cols.min())
    rr = rows - r0
    cc = cols - c0
    h = int(rr.max()) + 2
    w = int(cc.max()) + 2
    canvas = np.full((h, w), -1, dtype=np.int16)
    canvas[rr, cc] = kind.astype(np.int16)
    out: list[dict] = []
    for k in range(0, 9):
        lab, nlab = ndimage.label(canvas == k)
        for i in range(1, nlab + 1):
            sel = lab[rr, cc] == i
            n = int(sel.sum())
            if n < MIN_REGION_PX:
                continue
            out.append(
                {
                    "kind": "flat" if k == 0 else "pitched",
                    "n_px": n,
                    "slope_med": float(np.median(sl[sel])),
                    "aspect_med": float(np.median(asp[sel])),
                }
            )
    out.sort(key=lambda r: r["n_px"], reverse=True)
    return out


def _classify_roof(regions: list[dict], n_roof: int) -> tuple[str, float, float]:
    """Return (roof_class, pitch_deg, ridge_heading_deg)."""
    if n_roof <= 0:
        return "complex", 0.0, 0.0
    min_keep = max(MIN_REGION_PX, int(0.08 * n_roof))
    keep = [r for r in regions if r["n_px"] >= min_keep]
    pitched = [r for r in keep if r["kind"] == "pitched" and r["slope_med"] >= PITCHED_DEG]
    if not pitched:
        return "flat", 0.0, 0.0
    if len(pitched) == 1:
        r0 = pitched[0]
        ridge = (r0["aspect_med"] + 90.0) % 180.0
        return "shed", r0["slope_med"], ridge
    # pair two largest with opposite aspect
    a, b = pitched[0], pitched[1]
    opp = _ang_diff_360(a["aspect_med"], b["aspect_med"])
    slope_ok = abs(a["slope_med"] - b["slope_med"]) <= 10.0
    if 150.0 <= opp <= 210.0 and slope_ok:
        pitch = 0.5 * (a["slope_med"] + b["slope_med"])
        ridge = (a["aspect_med"] + 90.0) % 180.0
        return "gable", pitch, ridge
    if 3 <= len(pitched) <= 4:
        pitch = float(np.median([r["slope_med"] for r in pitched]))
        return "hip", pitch, (pitched[0]["aspect_med"] + 90.0) % 180.0
    pitch = float(np.median([r["slope_med"] for r in pitched]))
    return "complex", pitch, (pitched[0]["aspect_med"] + 90.0) % 180.0


def _footprint_class(poly, fill_min: float) -> tuple[str, float, float, float]:
    """Return (fp_class, fill, length_m, heading_deg)."""
    obb = _obb_corners(orient(poly, sign=1.0))
    if obb is None:
        return "complex", 0.0, 0.0, 0.0
    corners, length, width, fill = obb
    if length < width:
        length, width = width, length
    # long-edge heading
    e01 = math.hypot(corners[1][0] - corners[0][0], corners[1][1] - corners[0][1])
    e12 = math.hypot(corners[2][0] - corners[1][0], corners[2][1] - corners[1][1])
    if e01 >= e12:
        heading = _heading_north_cw(corners[1][0] - corners[0][0], corners[1][1] - corners[0][1])
    else:
        heading = _heading_north_cw(corners[2][0] - corners[1][0], corners[2][1] - corners[1][1])
    if fill >= fill_min:
        fp = "rect"
    elif fill >= 0.70:
        fp = "near_rect"
    else:
        fp = "complex"
    return fp, float(fill), float(length), heading


def _is_special(building: str, name: str, area: float) -> bool:
    b = (building or "").lower()
    n = (name or "").lower()
    if b in SPECIAL_TYPES:
        return True
    if area >= 1800.0:
        return True
    return any(tok in n for tok in SPECIAL_NAME)


def _pitch_bin(pitch: float) -> int:
    if pitch < 8.0:
        return 0
    return min(PITCH_BINS, key=lambda b: abs(b - pitch))


def _combine_class(fp: str, roof: str, ridge: float, obb_heading: float) -> str:
    if roof == "gable":
        if _ang_diff_180(ridge, obb_heading) <= 45.0:
            roof = "gable_long"
        else:
            roof = "gable_cross"
    if fp == "rect" and roof == "gable_long":
        return "rect_gable_long"
    if fp == "rect" and roof == "gable_cross":
        return "rect_gable_cross"
    if fp == "rect" and roof == "flat":
        return "rect_flat"
    if fp == "rect" and roof == "hip":
        return "rect_hip"
    if fp == "rect" and roof == "shed":
        return "rect_shed"
    if fp == "near_rect" and roof.startswith("gable"):
        return "near_rect_gable"
    if roof == "flat" and fp != "complex":
        return "rect_flat"
    return "complex"


def classify_one(
    *,
    poly,
    building: str,
    name: str,
    area: float,
    height_m: float,
    fill_min: float,
    dom: np.ndarray,
    dgm: np.ndarray,
    slope: np.ndarray,
    aspect: np.ndarray,
    xmin: float,
    ymax: float,
    cell: float,
) -> dict:
    h, w = dom.shape
    fp, fill, length, obb_h = _footprint_class(poly, fill_min)
    inner = poly.buffer(-INSET_M)
    if inner.is_empty:
        inner = poly
    rows, cols = _pixels_in_poly(inner, xmin, ymax, cell, h, w)
    if rows.size < 6:
        rows, cols = _pixels_in_poly(poly, xmin, ymax, cell, h, w)
    rec = {
        "footprint": fp,
        "obb_fill": round(fill, 3),
        "obb_length_m": round(length, 2),
        "yaw_deg": round(obb_h, 1),
        "n_px": int(rows.size),
        "roof": "too_few",
        "class": "too_few",
        "pitch_deg": 0.0,
        "pitch_bin_deg": 0,
        "ridge_deg": 0.0,
        "storeys": 1,
        "has_chimney": False,
        "ndsm_med_m": 0.0,
        "flat_z_med": None,
        "regions": 0,
        "special": _is_special(building, name, area),
    }
    if rows.size < 8:
        if rec["special"]:
            rec["class"] = "special"
        return rec

    ndsm = np.maximum(dom[rows, cols] - dgm[rows, cols], 0.0)
    on_roof = ndsm >= 1.2
    if int(on_roof.sum()) < 8:
        on_roof = np.ones(rows.size, dtype=bool)
    rr, cc = rows[on_roof], cols[on_roof]
    nd = ndsm[on_roof]
    rec["n_px"] = int(rr.size)
    rec["ndsm_med_m"] = round(float(np.median(nd)), 2)
    storeys = max(1, int(round(max(rec["ndsm_med_m"], height_m) / 3.0)))
    rec["storeys"] = min(storeys, 6)

    roof_med = float(np.median(nd))
    chim = nd > (roof_med + CHIMNEY_DZ)
    rec["has_chimney"] = bool(2 <= int(chim.sum()) <= CHIMNEY_MAX_PX)

    regions = _regions(rr, cc, slope, aspect)
    rec["regions"] = len(regions)
    roof, pitch, ridge = _classify_roof(regions, rr.size)
    rec["roof"] = roof
    rec["pitch_deg"] = round(pitch, 1)
    rec["ridge_deg"] = round(ridge, 1)
    rec["pitch_bin_deg"] = _pitch_bin(pitch)
    flats = [r for r in regions if r["kind"] == "flat"]
    if flats:
        rec["flat_z_med"] = round(float(np.median(dom[rr, cc])), 2)

    if rec["special"]:
        rec["class"] = "special"
    else:
        rec["class"] = _combine_class(fp, roof, ridge, obb_h)
    rec["exemplar"] = (
        rec["class"]
        if rec["class"] in {"special", "complex", "too_few"}
        else f"{rec['class']}_s{min(rec['storeys'], 3)}_p{rec['pitch_bin_deg']}"
    )
    return rec


def _iter_features(osm: dict, site: dict, cfg: dict):
    sc = SiteCoords(site)
    to_crs = Transformer.from_crs("EPSG:4326", sc.crs, always_xy=True)
    for el in osm.get("elements") or []:
        if el.get("type") != "way":
            continue
        tags = el.get("tags") or {}
        if "building" not in tags:
            continue
        geom = el.get("geometry") or []
        if len(geom) < 3:
            continue
        ring_wgs = [(float(g["lon"]), float(g["lat"])) for g in geom]
        if ring_wgs[0] != ring_wgs[-1]:
            ring_wgs.append(ring_wgs[0])
        ring = _way_ring_crs(el, to_crs)
        if not ring:
            continue
        poly = _poly_from_ring(ring)
        if poly is None:
            continue
        area = float(poly.area)
        if area < cfg["min_area_m2"] or area > cfg["max_area_m2"]:
            continue
        cx, cy = float(poly.centroid.x), float(poly.centroid.y)
        bx, by = sc.crs_to_beamng(cx, cy)
        if bx < 0 or by < 0 or bx > sc.terrain_extent or by > sc.terrain_extent:
            continue
        yield {
            "osm_id": el.get("id"),
            "building": str(tags.get("building") or "yes"),
            "name": tags.get("name") or "",
            "area_m2": round(area, 1),
            "height_m": round(_parse_height_m(tags, cfg), 2),
            "poly": poly,
            "ring_wgs": ring_wgs,
            "lon": ring_wgs[0][0],
            "lat": ring_wgs[0][1],
        }
        # centroid lon/lat
        # (overwritten below after transform — keep first ring point as fallback)


def _centroid_wgs(ring_wgs: list[tuple[float, float]]) -> tuple[float, float]:
    xs = [p[0] for p in ring_wgs[:-1]]
    ys = [p[1] for p in ring_wgs[:-1]]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def _google_url(lat: float, lon: float) -> str:
    return f"https://www.google.com/maps/@{lat:.6f},{lon:.6f},48m/data=!3m1!1e3"


def _kml_color(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f"c0{b:02x}{g:02x}{r:02x}"


def write_geojson(path: Path, rows: list[dict]) -> None:
    feats = []
    for r in rows:
        props = {k: v for k, v in r.items() if k not in {"ring_wgs", "poly"}}
        feats.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": {"type": "Polygon", "coordinates": [r["ring_wgs"]]},
            }
        )
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False),
        encoding="utf-8",
    )


def write_kml(path: Path, rows: list[dict]) -> None:
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["class"], []).append(r)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        "<Document>",
        "<name>building_roof_class</name>",
    ]
    for klass, rgb in CLASS_RGB.items():
        parts.append(
            f'<Style id="s_{klass}"><PolyStyle><color>{_kml_color(rgb)}</color>'
            f"<outline>1</outline></PolyStyle>"
            f"<LineStyle><color>ff000000</color><width>1</width></LineStyle></Style>"
        )
    for klass, items in sorted(by.items(), key=lambda kv: -len(kv[1])):
        parts.append(f"<Folder><name>{xml_escape(klass)} ({len(items)})</name>")
        for r in items:
            coords = " ".join(f"{lon:.7f},{lat:.7f},0" for lon, lat in r["ring_wgs"])
            desc = (
                f"{r['class']} / {r.get('exemplar','')}  pitch={r['pitch_deg']}°  "
                f"yaw={r['yaw_deg']}°  storeys={r['storeys']}  chimney={r['has_chimney']}  "
                f"{r.get('google','')}"
            )
            parts.append(
                f"<Placemark><name>{xml_escape(str(r['osm_id']))}</name>"
                f"<styleUrl>#s_{r['class']}</styleUrl>"
                f"<description>{xml_escape(desc)}</description>"
                f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{coords}"
                f"</coordinates></LinearRing></outerBoundaryIs></Polygon>"
                f"</Placemark>"
            )
        parts.append("</Folder>")
    parts.append("</Document></kml>")
    path.write_text("\n".join(parts), encoding="utf-8")


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = [
        "osm_id",
        "name",
        "building",
        "class",
        "exemplar",
        "footprint",
        "roof",
        "pitch_deg",
        "pitch_bin_deg",
        "yaw_deg",
        "storeys",
        "has_chimney",
        "area_m2",
        "ndsm_med_m",
        "obb_fill",
        "lat",
        "lon",
        "google",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_html(path: Path, rows: list[dict], counts: dict[str, int], site_name: str) -> None:
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["class"], []).append(r)
    blocks = []
    for klass, n in counts.most_common() if hasattr(counts, "most_common") else sorted(
        counts.items(), key=lambda kv: -kv[1]
    ):
        items = by.get(klass, [])
        sample = items[:: max(1, len(items) // 8)][:8]
        links = []
        for r in sample:
            label = escape(str(r.get("name") or r["osm_id"]))
            links.append(f'<li><a href="{escape(r["google"])}">{label}</a> — {r["pitch_deg"]}° / {r["storeys"]} G.</li>')
        rgb = CLASS_RGB.get(klass, (120, 120, 120))
        swatch = f"rgb({rgb[0]},{rgb[1]},{rgb[2]})"
        blocks.append(
            f"<h2><span style='display:inline-block;width:1em;height:1em;background:{swatch}'></span> "
            f"{escape(klass)} — {n}</h2><ul>{''.join(links)}</ul>"
        )
    html = f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><title>Dachklassen {escape(site_name)}</title>
<style>body{{font-family:sans-serif;max-width:52rem;margin:2rem auto;line-height:1.4}}
code{{background:#eee;padding:0.1em 0.3em}}</style></head><body>
<h1>Dachklassen — {escape(site_name)}</h1>
<p>{len(rows)} Gebäude. KML in Google Earth öffnen, GeoJSON nach
<a href="https://geojson.io">geojson.io</a> ziehen. Links unten: Satellit bei 48&nbsp;m.</p>
<p>Dateien neben dieser HTML: <code>building_roof_class.kml</code>,
<code>.geojson</code>, <code>.csv</code>, <code>preview_building_roof_class.png</code>.</p>
{''.join(blocks)}
</body></html>
"""
    path.write_text(html, encoding="utf-8")


def write_preview(path: Path, rows: list[dict], site: dict, size: int = 4096) -> None:
    xmin, ymin, xmax, ymax = map(float, site["bbox"])
    bw, bh = xmax - xmin, ymax - ymin
    img = Image.new("RGB", (size, size), (18, 18, 20))
    draw = ImageDraw.Draw(img)
    to_crs = Transformer.from_crs("EPSG:4326", str(site.get("crs", "EPSG:31254")), always_xy=True)
    for r in rows:
        rgb = CLASS_RGB.get(r["class"], (120, 120, 120))
        pts = []
        for lon, lat in r["ring_wgs"]:
            x, y = to_crs.transform(lon, lat)
            u = (x - xmin) / bw * (size - 1)
            v = (ymax - y) / bh * (size - 1)
            pts.append((u, v))
        if len(pts) >= 3:
            draw.polygon(pts, fill=rgb, outline=(0, 0, 0))
    # legend
    y = 12
    for i, (klass, rgb) in enumerate(CLASS_RGB.items()):
        draw.rectangle((12, y, 36, y + 16), fill=rgb)
        draw.text((42, y), klass, fill=(230, 230, 230))
        y += 20
    img.save(path)


def main() -> None:
    site = load_site()
    cfg = _cfg(site)
    slug = site_slug(site)
    proc = processed_dir(site)
    print(f"Site: {slug}", flush=True)

    dgm_path = dgm_cache_path(site)
    dom_path = dom_cache_path(site)
    if not dgm_path.is_file() or not dom_path.is_file():
        raise SystemExit(f"Need DGM and DOM caches:\n  {dgm_path}\n  {dom_path}")

    print(f"Loading {dgm_path.name} / {dom_path.name}", flush=True)
    dgm = _load_elev(dgm_path)
    dom = _load_elev(dom_path)
    if dgm.shape != dom.shape:
        raise SystemExit(f"DGM/DOM shape mismatch {dgm.shape} vs {dom.shape}")
    xmin, ymin, xmax, ymax = map(float, site["bbox"])
    cell = float(((site.get("sources") or {}).get("dom") or {}).get("resolution_m") or 1.0)
    print("Slope / aspect …", flush=True)
    slope, aspect = _slope_aspect(dom, cell)

    osm = fetch_osm_buildings(site, proc, force=False)
    fill_min = float(cfg["gable_fill_min"])
    rows: list[dict] = []
    skipped = 0
    for i, feat in enumerate(_iter_features(osm, site, cfg)):
        try:
            rec = classify_one(
                poly=feat["poly"],
                building=feat["building"],
                name=feat["name"],
                area=feat["area_m2"],
                height_m=feat["height_m"],
                fill_min=fill_min,
                dom=dom,
                dgm=dgm,
                slope=slope,
                aspect=aspect,
                xmin=xmin,
                ymax=ymax,
                cell=cell,
            )
        except Exception as ex:  # noqa: BLE001
            skipped += 1
            if skipped <= 8:
                print(f"  skip {feat['osm_id']}: {ex}")
            continue
        lon, lat = _centroid_wgs(feat["ring_wgs"])
        rec.update(
            {
                "osm_id": feat["osm_id"],
                "building": feat["building"],
                "name": feat["name"],
                "area_m2": feat["area_m2"],
                "height_m": feat["height_m"],
                "lon": round(lon, 6),
                "lat": round(lat, 6),
                "google": _google_url(lat, lon),
                "ring_wgs": feat["ring_wgs"],
            }
        )
        rows.append(rec)
        if (i + 1) % 400 == 0:
            print(f"  classified {i + 1}", flush=True)

    counts = Counter(r["class"] for r in rows)
    exemplars = Counter(r.get("exemplar") or r["class"] for r in rows)
    summary = {
        "site": slug,
        "count": len(rows),
        "skipped": skipped,
        "class_counts": dict(counts.most_common()),
        "exemplar_counts": dict(exemplars.most_common()),
        "chimney_true": sum(1 for r in rows if r["has_chimney"]),
        "pitch_bins": dict(Counter(r["pitch_bin_deg"] for r in rows).most_common()),
        "notes": {
            "yaw_deg": "OBB long axis, 0=north, clockwise, 0–180 undirected",
            "pitch_bin_deg": "nearest of 20/30/40, or 0 if flat",
            "class": "DOM roof + OBB fill; meshes not changed",
        },
    }
    (proc / "building_roof_class.json").write_text(
        json.dumps({"summary": summary, "items": [{k: v for k, v in r.items() if k != "ring_wgs"} for r in rows]}, indent=2),
        encoding="utf-8",
    )
    write_geojson(proc / "building_roof_class.geojson", rows)
    write_kml(proc / "building_roof_class.kml", rows)
    write_csv(proc / "building_roof_class.csv", rows)
    write_html(proc / "building_roof_class.html", rows, counts, slug)
    write_preview(proc / "preview_building_roof_class.png", rows, site)
    print(f"Wrote {proc / 'building_roof_class.html'} ({len(rows)} buildings)")
    print("Classes:")
    for k, n in counts.most_common():
        print(f"  {n:5d}  {k}")
    print("Top exemplars:")
    for k, n in exemplars.most_common(15):
        print(f"  {n:5d}  {k}")


if __name__ == "__main__":
    main()
