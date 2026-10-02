"""Label nave and tower for OSM churches from tiris roofs and the DOM raster.

No meshes. One site, churches only: OSM marks the building, tiris roof
polygons supply the split, and DOM−DGM locates the high pixels inside
the footprint when the tower is not its own polygon.

Writes under data/processed/<site>/:
  church_parts.geojson
  church_parts.kml
  church_parts.json
  church_parts.html

Usage:
  $env:AUTOROAD_SITE='config/sites/imst.yaml'
  python tools/join_church_parts.py
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import numpy as np
import requests
import tifffile as tiff
from pyproj import Transformer
from scipy import ndimage
from shapely.geometry import LineString, MultiPoint, Point, Polygon, mapping
from shapely.geometry.polygon import orient
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union
from shapely.validation import make_valid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from classify_building_roofs import _pixels_in_poly  # noqa: E402
from fetch_dgm import dgm_cache_path  # noqa: E402
from fetch_dom import dom_cache_path  # noqa: E402
from fetch_tiris_buildings import _url  # noqa: E402
from site_coords import SiteCoords, load_site, processed_dir, site_slug  # noqa: E402

WORSHIP_BUILDING = {"church", "chapel", "cathedral"}
TOWER_PART = {"tower", "bell_tower", "spire"}
MIN_AREA_M2 = 15.0
WORSHIP_GAP_M = 2.0
PART_ATTACH_M = 15.0
TIRIS_OVERLAP = 0.35
TIRIS_TOUCH_M = 4.0
HEIGHT_DELTA_M = 3.0
PEAK_ABOVE_M = 8.0
SPIRE_ABOVE_M = 15.0
MIN_PEAK_PX = 6
MIN_SPIRE_PX = 3
MAX_PEAK_FRAC = 0.35

# KML AABBGGRR
KML_COLOR = {
    "nave": "c000b050",
    "tower": "c0d040c0",
    "annex": "c00080e0",
    "merged": "c02020d0",
    "osm_outline": "60f0a000",
    "axis": "ff0000ff",
    "peak": "ff00ffff",
}
ROLE_DE = {
    "nave": "Schiff",
    "tower": "Turm",
    "annex": "Anbau",
    "merged": "nicht getrennt",
    "osm_outline": "OSM-Umriss",
    "axis": "Firstachse",
    "peak": "Spitze im Raster",
}


def _as_float(raw) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _poly(ring: list[tuple[float, float]]) -> Polygon | None:
    if len(ring) < 4:
        return None
    try:
        poly = Polygon(ring)
    except Exception:  # noqa: BLE001
        return None
    if not poly.is_valid:
        poly = make_valid(poly)
    if poly.is_empty:
        return None
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda g: g.area)
    if poly.geom_type != "Polygon" or poly.area <= 0:
        return None
    return orient(poly, sign=1.0)


def _ring_from_geom(geom: list[dict]) -> list[tuple[float, float]]:
    pts = [(float(g["lon"]), float(g["lat"])) for g in geom]
    if len(pts) >= 2 and pts[0] != pts[-1]:
        pts.append(pts[0])
    return pts


def _is_worship(tags: dict) -> bool:
    b = str(tags.get("building") or "").lower()
    if b in WORSHIP_BUILDING:
        return True
    return str(tags.get("amenity") or "") == "place_of_worship"


def _is_tower_tags(tags: dict) -> bool:
    part = str(tags.get("building:part") or "").lower()
    if part in TOWER_PART:
        return True
    if str(tags.get("building") or "").lower() == "tower":
        return True
    tower = str(tags.get("tower:type") or "").lower()
    return any(tok in tower for tok in ("bell", "church", "clock"))


def _compass(deg: float) -> str:
    dirs = ["N", "NO", "O", "SO", "S", "SW", "W", "NW"]
    return dirs[int((deg + 22.5) // 45.0) % 8]


def _heading_north_cw(dx: float, dy: float) -> float:
    return (90.0 - math.degrees(math.atan2(dy, dx))) % 360.0


def _obb(poly: Polygon) -> dict | None:
    mrr = poly.minimum_rotated_rectangle
    if mrr.is_empty or mrr.area <= 1e-9:
        return None
    coords = list(orient(mrr, sign=1.0).exterior.coords)[:-1]
    if len(coords) != 4:
        return None
    corners = [(float(x), float(y)) for x, y in coords]
    e01 = math.hypot(corners[1][0] - corners[0][0], corners[1][1] - corners[0][1])
    e12 = math.hypot(corners[2][0] - corners[1][0], corners[2][1] - corners[1][1])
    if e01 >= e12:
        hx, hy = corners[1][0] - corners[0][0], corners[1][1] - corners[0][1]
        length, width = e01, e12
    else:
        hx, hy = corners[2][0] - corners[1][0], corners[2][1] - corners[1][1]
        length, width = e12, e01
    n = math.hypot(hx, hy) or 1.0
    cx = sum(c[0] for c in corners) / 4.0
    cy = sum(c[1] for c in corners) / 4.0
    return {
        "cx": cx,
        "cy": cy,
        "hx": hx / n,
        "hy": hy / n,
        "length_m": float(length),
        "width_m": float(width),
        "fill": float(poly.area) / float(mrr.area),
        "yaw_deg": round(_heading_north_cw(hx, hy) % 180.0, 1),
    }


def fetch_osm_churches(site: dict, *, force: bool) -> dict:
    slug = site_slug(site)
    cache = ROOT / "data" / "raw" / f"osm_churches_{slug}.json"
    if cache.is_file() and cache.stat().st_size > 50 and not force:
        print(f"Using cached OSM churches: {cache}")
        return json.loads(cache.read_text(encoding="utf-8"))

    sc = SiteCoords(site)
    to_wgs = Transformer.from_crs(sc.crs, "EPSG:4326", always_xy=True)
    w, s = to_wgs.transform(sc.xmin, sc.ymin)
    e, n = to_wgs.transform(sc.xmax, sc.ymax)
    # Equality filters only. Regex and relations time out on this bbox.
    bbox = f"({s},{w},{n},{e})"
    query = f"""
    [out:json][timeout:60];
    (
      way["amenity"="place_of_worship"]{bbox};
      way["building"="church"]{bbox};
      way["building"="chapel"]{bbox};
      way["building"="cathedral"]{bbox};
      way["building:part"="tower"]{bbox};
    );
    out geom;
    """
    headers = {"User-Agent": "beamng_autoroad/0.1", "Accept": "application/json"}
    data = None
    last_err: Exception | None = None
    for url in (
        "https://overpass.openstreetmap.fr/api/interpreter",
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    ):
        try:
            print(f"Overpass churches via {url}", flush=True)
            r = requests.post(url, data={"data": query}, headers=headers, timeout=70)
            r.raise_for_status()
            payload = r.json()
            n = len(payload.get("elements") or [])
            if n == 0:
                raise RuntimeError("0 elements (mirror empty or query missed)")
            data = payload
            print(f"Overpass churches OK via {url} elements={n}", flush=True)
            break
        except Exception as ex:  # noqa: BLE001
            last_err = ex
            print(f"Overpass failed at {url}: {ex}", flush=True)
    if data is None:
        raise SystemExit(f"OSM church fetch failed: {last_err}")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data), encoding="utf-8")
    return data


def _element_polys_wgs(el: dict) -> list[list[tuple[float, float]]]:
    if el.get("type") == "way":
        geom = el.get("geometry") or []
        if len(geom) < 4:
            return []
        return [_ring_from_geom(geom)]
    if el.get("type") != "relation":
        return []
    rings = []
    for mem in el.get("members") or []:
        if mem.get("type") != "way":
            continue
        role = str(mem.get("role") or "")
        if role not in ("", "outer", "part"):
            continue
        geom = mem.get("geometry") or []
        if len(geom) < 4:
            continue
        rings.append(_ring_from_geom(geom))
    return rings


def osm_records(data: dict, to_crs: Transformer) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, int, int]] = set()
    for el in data.get("elements") or []:
        tags = el.get("tags") or {}
        worship = _is_worship(tags)
        tower = _is_tower_tags(tags)
        if not worship and not tower:
            continue
        oid = int(el.get("id") or 0)
        for i, ring in enumerate(_element_polys_wgs(el)):
            key = (str(el.get("type")), oid, i)
            if key in seen:
                continue
            poly_wgs = _poly(ring)
            if poly_wgs is None:
                continue
            poly = shp_transform(to_crs.transform, poly_wgs)
            if poly.geom_type != "Polygon":
                continue
            area = float(poly.area)
            if area < MIN_AREA_M2:
                continue
            seen.add(key)
            out.append({
                "osm_type": el.get("type"),
                "osm_id": oid,
                "kind": "worship" if worship else "part",
                "tower_tag": tower,
                "name": str(tags.get("name") or ""),
                "building": str(tags.get("building") or ""),
                "amenity": str(tags.get("amenity") or ""),
                "religion": str(tags.get("religion") or ""),
                "denomination": str(tags.get("denomination") or ""),
                "area_m2": area,
                "poly": poly,
                "poly_wgs": poly_wgs,
            })
    return out


def _clusters(records: list[dict]) -> list[dict]:
    worship = [r for r in records if r["kind"] == "worship"]
    parts = [r for r in records if r["kind"] == "part"]
    n = len(worship)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if worship[i]["poly"].distance(worship[j]["poly"]) <= WORSHIP_GAP_M:
                parent[find(j)] = find(i)
    groups: dict[int, list[dict]] = {}
    for i, rec in enumerate(worship):
        groups.setdefault(find(i), []).append(rec)
    clusters = [{"worship": g, "osm_parts": []} for g in groups.values()]
    for part in parts:
        best_i = None
        best_d = PART_ATTACH_M
        for i, cl in enumerate(clusters):
            outline = unary_union([w["poly"] for w in cl["worship"]])
            dist = part["poly"].distance(outline)
            if dist <= best_d:
                best_d = dist
                best_i = i
        if best_i is not None:
            clusters[best_i]["osm_parts"].append(part)
    return clusters


def query_tiris_wgs(
    site: dict,
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
) -> list[dict]:
    """Envelope in WGS84. Avoids a PROJ/Esri datum shift on the site CRS."""
    url = _url(site).rstrip("/") + "/query"
    geom = json.dumps({
        "xmin": min_lon,
        "ymin": min_lat,
        "xmax": max_lon,
        "ymax": max_lat,
        "spatialReference": {"wkid": 4326},
    })
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
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outSR": 4326,
            "resultOffset": offset,
            "resultRecordCount": 500,
        }
        r = requests.get(url, params=params, timeout=90)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(data["error"])
        page = data.get("features") or []
        feats.extend(page)
        exceeded = bool(
            data.get("exceededTransferLimit")
            or (data.get("properties") or {}).get("exceededTransferLimit")
        )
        if not page or (len(page) < 500 and not exceeded):
            break
        offset += len(page)
        if offset > 2000:
            break
    return feats


def _tiris_part(feat: dict, to_crs: Transformer) -> dict | None:
    geom = feat.get("geometry") or {}
    if geom.get("type") != "Polygon":
        coords = geom.get("coordinates") or []
        if geom.get("type") == "MultiPolygon" and coords:
            geom = {"type": "Polygon", "coordinates": max(coords, key=lambda p: Polygon(p[0]).area)}
        else:
            return None
    ring = [(float(x), float(y)) for x, y, *_ in (geom.get("coordinates") or [[]])[0]]
    poly_wgs = _poly(ring)
    if poly_wgs is None:
        return None
    poly = shp_transform(to_crs.transform, poly_wgs)
    if poly.geom_type != "Polygon":
        return None
    props = feat.get("properties") or {}
    return {
        "objectid": props.get("OBJECTID"),
        "poly": poly,
        "area_m2": float(poly.area),
        "h_max": _as_float(props.get("GEB_HOEHE_MAX")),
        "h_med": _as_float(props.get("GEB_HOEHE_MEDIAN")),
        "source": "tiris",
        "osm_tower": False,
    }


def _height(part: dict) -> float:
    for key in ("h_max", "h_med"):
        val = part.get(key)
        if val is not None and val >= 2.0:
            return float(val)
    return 0.0


def _median_height(part: dict) -> float:
    """Height of the roof body. Max alone is a spire or a single high pixel."""
    med = part.get("h_med")
    if med is not None and med >= 2.0:
        return float(med)
    return _height(part)


def _height_span(part: dict) -> float | None:
    hi, med = part.get("h_max"), part.get("h_med")
    if hi is None or med is None:
        return None
    return float(hi) - float(med)


def _select_tiris(outline: Polygon, feats: list[dict], to_crs: Transformer) -> list[dict]:
    parts: list[dict] = []
    seen: set[int] = set()
    for feat in feats:
        part = _tiris_part(feat, to_crs)
        if part is None:
            continue
        oid = part["objectid"]
        if isinstance(oid, int) and oid in seen:
            continue
        inter = float(part["poly"].intersection(outline).area)
        of_part = inter / part["area_m2"] if part["area_m2"] > 0 else 0.0
        of_osm = inter / float(outline.area) if outline.area > 0 else 0.0
        # A neighbour that merely touches the wall is not part of the church.
        if of_part < TIRIS_OVERLAP and of_osm < 0.20 and not (inter >= 8.0 and of_part >= 0.25):
            continue
        part["match"] = "overlap"
        parts.append(part)
        if isinstance(oid, int):
            seen.add(oid)
    if not parts:
        return []
    nave_h = max(_height(p) for p in parts)
    largest = max(p["area_m2"] for p in parts)
    for feat in feats:
        part = _tiris_part(feat, to_crs)
        if part is None:
            continue
        oid = part["objectid"]
        if isinstance(oid, int) and oid in seen:
            continue
        if part["poly"].distance(outline) > TIRIS_TOUCH_M:
            continue
        if part["area_m2"] < 8.0 or part["area_m2"] > max(160.0, 0.55 * largest):
            continue
        h = _height(part)
        if h < 12.0 and h < nave_h + HEIGHT_DELTA_M:
            continue
        part["match"] = "adjacent_tall"
        parts.append(part)
        if isinstance(oid, int):
            seen.add(oid)
    return parts


def _label(parts: list[dict]) -> tuple[str, str]:
    """Return (confidence, how the tower was chosen)."""
    if len(parts) == 1:
        parts[0]["role"] = "merged"
        return "niedrig", "ein Polygon"
    parts.sort(key=lambda p: p["area_m2"], reverse=True)
    nave = parts[0]
    nave["role"] = "nave"
    nave_h = _median_height(nave)
    candidates = []
    for part in parts[1:]:
        small = part["area_m2"] <= max(120.0, 0.5 * nave["area_m2"])
        taller = nave_h > 0 and _median_height(part) >= nave_h + HEIGHT_DELTA_M + 1.0
        tagged = bool(part.get("osm_tower"))
        if tagged or (small and taller):
            part["role"] = "tower_candidate"
            candidates.append(part)
        else:
            part["role"] = "annex"
    if not candidates:
        return "niedrig", "kein Turm erkannt"
    top = max(_median_height(p) for p in candidates)
    kept = 0
    for part in candidates:
        if _median_height(part) >= top - HEIGHT_DELTA_M:
            part["role"] = "tower"
            kept += 1
        else:
            part["role"] = "annex"
    if not kept:
        return "niedrig", "kein Turm erkannt"
    tagged_tower = any(p.get("osm_tower") for p in parts if p["role"] == "tower")
    by_height = any(not p.get("osm_tower") for p in parts if p["role"] == "tower")
    if tagged_tower and by_height:
        how = "hoehe+osm"
    elif tagged_tower:
        how = "osm-tag"
    else:
        how = "hoehe"
    return "hoch", how


def _offset_place(obb: dict | None, x: float, y: float, prefix: str) -> dict:
    if not obb:
        return {}
    dx, dy = x - obb["cx"], y - obb["cy"]
    along = dx * obb["hx"] + dy * obb["hy"]
    side = dx * (-obb["hy"]) + dy * obb["hx"]
    length = obb["length_m"]
    if abs(along) >= abs(side) and abs(along) >= 0.2 * length:
        place = "stirnseite"
    elif abs(side) > abs(along):
        place = "seite"
    else:
        place = "unbekannt"
    bearing = _heading_north_cw(dx, dy)
    return {
        f"{prefix}_place": place,
        f"{prefix}_bearing_deg": round(bearing, 1),
        f"{prefix}_compass": _compass(bearing),
        f"{prefix}_along_m": round(along, 1),
        f"{prefix}_side_m": round(side, 1),
    }


def _tower_place(nave: dict, tower: dict) -> dict:
    return _offset_place(
        nave.get("obb"),
        float(tower["poly"].centroid.x),
        float(tower["poly"].centroid.y),
        "tower",
    )


class ElevGrid:
    """DOM and DGM, row 0 = north, same bbox as the site raster cache."""

    def __init__(self, dom: np.ndarray, dgm: np.ndarray, bbox: list[float]):
        self.dom = dom
        self.dgm = dgm
        xmin, ymin, xmax, ymax = bbox
        self.xmin = float(xmin)
        self.ymax = float(ymax)
        height, width = dom.shape
        self.cell = (float(xmax) - float(xmin)) / float(width)
        self._cell_y = (float(ymax) - float(ymin)) / float(height)


def load_elev_grid(site: dict) -> ElevGrid:
    dom_path = dom_cache_path(site)
    dgm_path = dgm_cache_path(site)
    if not dom_path.is_file() or not dgm_path.is_file():
        raise SystemExit(f"Need DOM and DGM caches:\n  {dom_path}\n  {dgm_path}")
    meta_path = dom_path.with_suffix(".meta.json")
    if meta_path.is_file():
        bbox = list(map(float, json.loads(meta_path.read_text(encoding="utf-8"))["bbox"]))
    else:
        bbox = list(map(float, site["bbox"]))
    print(f"Loading {dom_path.name} / {dgm_path.name}", flush=True)
    dom = np.asarray(tiff.imread(dom_path), dtype=np.float32)
    dgm = np.asarray(tiff.imread(dgm_path), dtype=np.float32)
    if dom.ndim == 3:
        dom = dom[..., 0]
    if dgm.ndim == 3:
        dgm = dgm[..., 0]
    if dom.shape != dgm.shape:
        raise SystemExit(f"DOM/DGM shape mismatch {dom.shape} vs {dgm.shape}")
    grid = ElevGrid(dom, dgm, bbox)
    if abs(grid.cell - grid._cell_y) > 0.02:
        raise SystemExit(f"DOM pixels are not square: {grid.cell:.4f} x {grid._cell_y:.4f}")
    print(f"DOM {dom.shape[1]}x{dom.shape[0]} cell={grid.cell:.3f} m", flush=True)
    return grid


def _is_ridge(xs: np.ndarray, ys: np.ndarray, obb: dict | None) -> bool:
    if not obb or xs.size < 6:
        return False
    along = (xs - obb["cx"]) * obb["hx"] + (ys - obb["cy"]) * obb["hy"]
    side = (xs - obb["cx"]) * (-obb["hy"]) + (ys - obb["cy"]) * obb["hx"]
    ext_a = float(along.max() - along.min())
    ext_s = float(side.max() - side.min())
    return ext_a >= 16.0 and ext_a >= 2.5 * max(ext_s, 0.5)


def _peak_hull(xs: np.ndarray, ys: np.ndarray, cx: float, cy: float) -> Polygon:
    hull = MultiPoint([(float(x), float(y)) for x, y in zip(xs, ys)]).convex_hull
    if hull.geom_type != "Polygon" or hull.area < 0.5:
        hull = Point(cx, cy).buffer(1.2, quad_segs=8)
    return hull


def locate_peak(poly: Polygon, grid: ElevGrid, obb: dict | None) -> dict:
    """High DOM−DGM blob inside the footprint. Empty when the roof has no local spike."""
    height, width = grid.dom.shape
    rows, cols = _pixels_in_poly(poly, grid.xmin, grid.ymax, grid.cell, height, width)
    if rows.size < 8:
        return {"found": False}
    dom = grid.dom[rows, cols]
    dgm = grid.dgm[rows, cols]
    good = (
        np.isfinite(dom) & np.isfinite(dgm)
        & (dom > 50.0) & (dom < 4500.0)
        & (dgm > 50.0) & (dgm < 4500.0)
    )
    ndsm = dom - dgm
    valid = good & (ndsm >= 1.2)
    if int(valid.sum()) < 8:
        return {"found": False}
    roof_vals = ndsm[valid]
    roof_med = float(np.median(roof_vals))
    roof_max = float(np.max(roof_vals))
    base = {
        "found": False,
        "roof_ndsm_med_m": round(roof_med, 1),
        "roof_ndsm_max_m": round(roof_max, 1),
        "n_roof_px": int(valid.sum()),
    }
    best = _best_peak(rows, cols, ndsm, valid, roof_med, grid, obb, PEAK_ABOVE_M, MIN_PEAK_PX)
    if best is None:
        # Slender tower: only a few pixels, but compact and clearly above the roof.
        best = _best_peak(
            rows, cols, ndsm, valid, roof_med, grid, obb,
            PEAK_ABOVE_M - 2.0, MIN_SPIRE_PX, compact_m=6.0,
        )
    if best is None:
        best = _best_peak(rows, cols, ndsm, valid, roof_med, grid, obb, SPIRE_ABOVE_M, MIN_SPIRE_PX)
    if best is None:
        return base
    best.update(base)
    best["found"] = True
    return best


def _best_peak(
    rows: np.ndarray,
    cols: np.ndarray,
    ndsm: np.ndarray,
    valid: np.ndarray,
    roof_med: float,
    grid: ElevGrid,
    obb: dict | None,
    above_m: float,
    min_px: int,
    compact_m: float | None = None,
) -> dict | None:
    high = valid & (ndsm >= roof_med + above_m)
    if int(high.sum()) < min_px:
        return None
    hr = rows[high]
    hc = cols[high]
    r0, c0 = int(hr.min()), int(hc.min())
    canvas = np.zeros((int(hr.max() - r0) + 1, int(hc.max() - c0) + 1), dtype=np.uint8)
    canvas[hr - r0, hc - c0] = 1
    labels, nlab = ndimage.label(canvas, structure=np.ones((3, 3), dtype=np.uint8))
    n_valid = int(valid.sum())
    chosen: dict | None = None
    for i in range(1, int(nlab) + 1):
        sel = labels[hr - r0, hc - c0] == i
        n = int(sel.sum())
        if n < min_px or n > MAX_PEAK_FRAC * n_valid:
            continue
        vals = ndsm[high][sel]
        xs = grid.xmin + (hc[sel].astype(np.float64) + 0.5) * grid.cell
        ys = grid.ymax - (hr[sel].astype(np.float64) + 0.5) * grid.cell
        if compact_m is not None:
            if float(xs.max() - xs.min()) > compact_m or float(ys.max() - ys.min()) > compact_m:
                continue
            if float(np.max(vals)) < roof_med + 9.5:
                continue
        if _is_ridge(xs, ys, obb) and float(np.max(vals)) < roof_med + SPIRE_ABOVE_M:
            continue
        med = float(np.median(vals))
        mx = float(np.max(vals))
        rank = (med, mx, n)
        if chosen is not None and rank <= chosen["rank"]:
            continue
        wts = np.maximum(vals - roof_med, 0.5)
        cx = float(np.average(xs, weights=wts))
        cy = float(np.average(ys, weights=wts))
        chosen = {
            "rank": rank,
            "cx": cx,
            "cy": cy,
            "poly": _peak_hull(xs, ys, cx, cy),
            "n_px": n,
            "h_med": round(med, 1),
            "h_max": round(mx, 1),
            "above_m": above_m,
        }
    if chosen is None:
        return None
    chosen.pop("rank", None)
    return chosen


def _church_name(worship: list[dict], idx: int) -> str:
    for rec in worship:
        if rec.get("name"):
            return str(rec["name"])
    oid = min(int(w["osm_id"]) for w in worship)
    return f"Kirche {oid}" if worship else f"Kirche {idx}"


def build_churches(
    site: dict,
    records: list[dict],
    to_crs: Transformer,
    grid: ElevGrid,
) -> list[dict]:
    churches: list[dict] = []
    printed_keys = False
    for idx, cl in enumerate(_clusters(records), start=1):
        worship = cl["worship"]
        outline = unary_union([w["poly"] for w in worship])
        outline_wgs = unary_union([w["poly_wgs"] for w in worship])
        if outline.geom_type == "MultiPolygon":
            outline = max(outline.geoms, key=lambda g: g.area)
        if outline_wgs.geom_type == "MultiPolygon":
            outline_wgs = max(outline_wgs.geoms, key=lambda g: g.area)
        min_lon, min_lat, max_lon, max_lat = outline_wgs.bounds
        # ~40 m at this latitude, so a tower just outside the OSM outline is inside the query.
        pad_lat = 40.0 / 111_320.0
        pad_lon = 40.0 / (111_320.0 * max(0.2, math.cos(math.radians(min_lat))))
        try:
            feats = query_tiris_wgs(
                site,
                min_lon - pad_lon,
                min_lat - pad_lat,
                max_lon + pad_lon,
                max_lat + pad_lat,
            )
        except Exception as ex:  # noqa: BLE001
            print(f"  tiris failed for cluster {idx}: {ex}")
            feats = []
        if feats and not printed_keys:
            print("tiris property keys:", sorted((feats[0].get("properties") or {}).keys()))
            printed_keys = True
        parts = _select_tiris(outline, feats, to_crs)
        source = "tiris"
        if not parts:
            source = "osm"
            use = worship + cl["osm_parts"]
            for rec in use:
                parts.append({
                    "objectid": None,
                    "osm_id": rec["osm_id"],
                    "poly": rec["poly"],
                    "area_m2": rec["area_m2"],
                    "h_max": None,
                    "h_med": None,
                    "source": "osm",
                    "osm_tower": rec["tower_tag"] and rec["kind"] == "part",
                    "match": "osm",
                })
        else:
            for rec in cl["osm_parts"]:
                if not rec["tower_tag"]:
                    continue
                for part in parts:
                    if part["poly"].intersects(rec["poly"]):
                        part["osm_tower"] = True
        if not parts:
            continue
        confidence, how = _label(parts)
        for part in parts:
            if part["role"] == "nave" or (part["role"] == "merged" and len(parts) == 1):
                part["obb"] = _obb(part["poly"])
        nave = next((p for p in parts if p["role"] in ("nave", "merged")), parts[0])
        place: dict = {}
        towers = [p for p in parts if p["role"] == "tower"]
        if towers and nave.get("obb"):
            place = _tower_place(nave, max(towers, key=_height))
        cid = f"osm-{min(int(w['osm_id']) for w in worship)}"
        name = _church_name(worship, idx)
        split = any(p["role"] == "tower" for p in parts)
        spans = [s for s in (_height_span(p) for p in parts) if s is not None]
        height_span = max(spans) if spans else None
        footprint = unary_union([p["poly"] for p in parts])
        peak = locate_peak(footprint, grid, nave.get("obb"))
        peak_place: dict = {}
        if peak.get("found"):
            peak_place = _offset_place(nave.get("obb"), peak["cx"], peak["cy"], "peak")
            if towers:
                tw = max(towers, key=_median_height)
                dist = math.hypot(
                    peak["cx"] - float(tw["poly"].centroid.x),
                    peak["cy"] - float(tw["poly"].centroid.y),
                )
                peak["agrees_m"] = round(dist, 1)
        note = ""
        if peak.get("found") and not split:
            note = (
                f"Spitze im Raster {peak_place.get('peak_compass', '')}, "
                f"{peak['h_max']} m, {peak['n_px']} px"
            )
            if confidence == "niedrig":
                confidence = "mittel"
                how = "raster"
        elif peak.get("found") and split:
            dist = peak.get("agrees_m")
            if dist is not None and dist <= 8.0:
                note = "Raster liegt auf dem Turmdach"
            elif dist is not None:
                note = f"Raster und Turmpolygon {dist:.0f} m auseinander"
        elif not split and height_span is not None and height_span >= 15.0:
            note = "Spitze in der Polygonhöhe, im Raster keine lokale Fläche"
        churches.append({
            "church_id": cid,
            "name": name,
            "confidence": confidence,
            "tower_by": how,
            "split": split,
            "part_source": source,
            "building": worship[0].get("building") or "",
            "religion": worship[0].get("religion") or "",
            "denomination": worship[0].get("denomination") or "",
            "osm_ids": sorted({int(w["osm_id"]) for w in worship}),
            "outline": outline,
            "nave": nave,
            "parts": parts,
            "place": place,
            "peak": peak,
            "peak_place": peak_place,
            "height_span_m": None if height_span is None else round(height_span, 1),
            "note": note,
            "lat": None,
            "lon": None,
        })
        print(
            f"  {name}: parts={len(parts)} split={split} "
            f"confidence={confidence} via={how} source={source}"
            + (
                f" peak={peak['h_max']}m/{peak['n_px']}px {peak_place.get('peak_compass', '')}"
                if peak.get("found")
                else " peak=none"
            )
            + (f" note={note}" if note else "")
        )
    return churches


def _to_wgs_geom(geom, to_wgs: Transformer) -> dict:
    return mapping(shp_transform(to_wgs.transform, geom))


def _part_props(ch: dict, part: dict) -> dict:
    obb = part.get("obb") or {}
    props = {
        "church_id": ch["church_id"],
        "name": ch["name"],
        "role": part["role"],
        "role_de": ROLE_DE.get(part["role"], part["role"]),
        "split": ch["split"],
        "confidence": ch["confidence"],
        "tower_by": ch["tower_by"],
        "part_source": part.get("source"),
        "match": part.get("match") or "",
        "area_m2": round(float(part["area_m2"]), 1),
        "height_max_m": None if part.get("h_max") is None else round(float(part["h_max"]), 1),
        "height_med_m": None if part.get("h_med") is None else round(float(part["h_med"]), 1),
        "height_span_m": None if _height_span(part) is None else round(_height_span(part), 1),
        "note": ch.get("note") or "",
        "tiris_objectid": part.get("objectid"),
        "osm_id": part.get("osm_id"),
        "building": ch["building"],
        "religion": ch["religion"],
        "denomination": ch["denomination"],
    }
    if obb:
        props["nave_length_m"] = round(obb["length_m"], 1)
        props["nave_width_m"] = round(obb["width_m"], 1)
        props["nave_yaw_deg"] = obb["yaw_deg"]
    props.update(ch["place"])
    return props


def write_outputs(site: dict, churches: list[dict], to_wgs: Transformer) -> None:
    proc = processed_dir(site)
    features: list[dict] = []
    summary_rows: list[dict] = []
    for ch in churches:
        cxy = ch["outline"].centroid
        lon, lat = to_wgs.transform(float(cxy.x), float(cxy.y))
        ch["lon"], ch["lat"] = round(float(lon), 6), round(float(lat), 6)
        features.append({
            "type": "Feature",
            "properties": {
                "church_id": ch["church_id"],
                "name": ch["name"],
                "role": "osm_outline",
                "role_de": ROLE_DE["osm_outline"],
                "split": ch["split"],
                "confidence": ch["confidence"],
                "tower_by": ch["tower_by"],
                "part_source": ch["part_source"],
                "building": ch["building"],
            },
            "geometry": _to_wgs_geom(ch["outline"], to_wgs),
        })
        nave = ch["nave"]
        obb = nave.get("obb")
        if obb:
            half = obb["length_m"] * 0.5
            axis = LineString([
                (obb["cx"] - obb["hx"] * half, obb["cy"] - obb["hy"] * half),
                (obb["cx"] + obb["hx"] * half, obb["cy"] + obb["hy"] * half),
            ])
            features.append({
                "type": "Feature",
                "properties": {
                    "church_id": ch["church_id"],
                    "name": ch["name"],
                    "role": "axis",
                    "role_de": ROLE_DE["axis"],
                    "nave_yaw_deg": obb["yaw_deg"],
                    "nave_length_m": round(obb["length_m"], 1),
                    "nave_width_m": round(obb["width_m"], 1),
                },
                "geometry": _to_wgs_geom(axis, to_wgs),
            })
        peak = ch.get("peak") or {}
        if peak.get("found") and peak.get("poly") is not None:
            features.append({
                "type": "Feature",
                "properties": {
                    "church_id": ch["church_id"],
                    "name": ch["name"],
                    "role": "peak",
                    "role_de": ROLE_DE["peak"],
                    "split": ch["split"],
                    "confidence": ch["confidence"],
                    "tower_by": ch["tower_by"],
                    "peak_h_med_m": peak.get("h_med"),
                    "peak_h_max_m": peak.get("h_max"),
                    "peak_n_px": peak.get("n_px"),
                    "roof_ndsm_med_m": peak.get("roof_ndsm_med_m"),
                    "roof_ndsm_max_m": peak.get("roof_ndsm_max_m"),
                    "peak_agrees_m": peak.get("agrees_m"),
                    "note": ch.get("note") or "",
                    **(ch.get("peak_place") or {}),
                },
                "geometry": _to_wgs_geom(peak["poly"], to_wgs),
            })
        for part in ch["parts"]:
            features.append({
                "type": "Feature",
                "properties": _part_props(ch, part),
                "geometry": _to_wgs_geom(part["poly"], to_wgs),
            })
        towers = [p for p in ch["parts"] if p["role"] == "tower"]
        summary_rows.append({
            "church_id": ch["church_id"],
            "name": ch["name"],
            "lat": ch["lat"],
            "lon": ch["lon"],
            "split": ch["split"],
            "confidence": ch["confidence"],
            "tower_by": ch["tower_by"],
            "part_source": ch["part_source"],
            "n_parts": len(ch["parts"]),
            "nave_height_med_m": round(_median_height(nave), 1) or None,
            "nave_height_max_m": None if nave.get("h_max") is None else round(float(nave["h_max"]), 1),
            "tower_height_med_m": None if not towers else round(max(_median_height(p) for p in towers), 1),
            "tower_height_max_m": None if not towers else round(max(_height(p) for p in towers), 1),
            "height_span_m": ch.get("height_span_m"),
            "note": ch.get("note") or "",
            "peak_found": bool(peak.get("found")),
            "peak_h_med_m": peak.get("h_med"),
            "peak_h_max_m": peak.get("h_max"),
            "peak_n_px": peak.get("n_px"),
            "roof_ndsm_med_m": peak.get("roof_ndsm_med_m"),
            "roof_ndsm_max_m": peak.get("roof_ndsm_max_m"),
            "peak_agrees_m": peak.get("agrees_m"),
            **(ch.get("peak_place") or {}),
            "nave_length_m": None if not obb else round(obb["length_m"], 1),
            "nave_width_m": None if not obb else round(obb["width_m"], 1),
            "nave_yaw_deg": None if not obb else obb["yaw_deg"],
            **ch["place"],
            "building": ch["building"],
            "osm_ids": ch["osm_ids"],
        })

    collection = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "features": features,
    }
    geo_path = proc / "church_parts.geojson"
    geo_path.write_text(json.dumps(collection, ensure_ascii=False), encoding="utf-8")
    counts = {
        "churches": len(churches),
        "split": sum(1 for c in churches if c["split"]),
        "merged": sum(1 for c in churches if not c["split"]),
        "confidence_hoch": sum(1 for c in churches if c["confidence"] == "hoch"),
        "confidence_mittel": sum(1 for c in churches if c["confidence"] == "mittel"),
        "confidence_niedrig": sum(1 for c in churches if c["confidence"] == "niedrig"),
        "source_tiris": sum(1 for c in churches if c["part_source"] == "tiris"),
        "source_osm": sum(1 for c in churches if c["part_source"] == "osm"),
        "peak": sum(1 for c in churches if (c.get("peak") or {}).get("found")),
    }
    summary = {"site": site_slug(site), "counts": counts, "churches": summary_rows}
    (proc / "church_parts.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _write_kml(proc / "church_parts.kml", features)
    _write_html(proc / "church_parts.html", summary)
    print(f"Wrote {geo_path} features={len(features)}")
    print("counts:", json.dumps(counts, ensure_ascii=False))


def _kml_coords(geom: dict) -> str:
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "LineString":
        ring = coords
    elif gtype == "Polygon":
        ring = coords[0] if coords else []
    else:
        return ""
    return " ".join(f"{x:.7f},{y:.7f},0" for x, y, *_ in ring)


def _write_kml(path: Path, features: list[dict]) -> None:
    order = ["nave", "tower", "annex", "merged", "peak", "osm_outline", "axis"]
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>Kirchen Schiff/Turm</name>',
    ]
    for role in order:
        color = KML_COLOR[role]
        sid = f"s_{role}"
        parts.append(
            f'<Style id="{sid}"><LineStyle><color>{color}</color><width>2</width></LineStyle>'
            f"<PolyStyle><color>{color}</color></PolyStyle></Style>"
        )
        group = [f for f in features if (f.get("properties") or {}).get("role") == role]
        parts.append(f"<Folder><name>{xml_escape(ROLE_DE[role])} ({len(group)})</name>")
        for feat in group:
            props = feat.get("properties") or {}
            geom = feat.get("geometry") or {}
            coord = _kml_coords(geom)
            if not coord:
                continue
            label = props.get("name") or props.get("church_id") or ""
            body = xml_escape(
                f"{props.get('role_de', '')} {props.get('height_max_m', '')} m "
                f"{props.get('confidence', '')} {props.get('tower_compass', '')}"
            )
            tag = "LineString" if geom.get("type") == "LineString" else "Polygon"
            inner = (
                f"<coordinates>{coord}</coordinates>"
                if tag == "LineString"
                else f"<outerBoundaryIs><LinearRing><coordinates>{coord}</coordinates></LinearRing></outerBoundaryIs>"
            )
            parts.append(
                f"<Placemark><name>{xml_escape(str(label))}</name>"
                f"<styleUrl>#{sid}</styleUrl><description>{body}</description>"
                f"<{tag}>{inner}</{tag}></Placemark>"
            )
        parts.append("</Folder>")
    parts.append("</Document></kml>")
    path.write_text("\n".join(parts), encoding="utf-8")


def _write_html(path: Path, summary: dict) -> None:
    counts = summary["counts"]
    rows = []
    for ch in summary["churches"]:
        lat, lon = ch["lat"], ch["lon"]
        link = f"https://www.google.com/maps/@{lat},{lon},19z/data=!3m1!1e3"
        place = ch.get("peak_place") or ch.get("tower_place") or "—"
        compass = ch.get("peak_compass") or ch.get("tower_compass") or ""
        nave_med = ch.get("nave_height_med_m")
        nave_max = ch.get("nave_height_max_m")
        tower_med = ch.get("tower_height_med_m")
        span = ch.get("height_span_m")
        peak_h = ch.get("peak_h_max_m")
        peak_n = ch.get("peak_n_px")
        roof_med = ch.get("roof_ndsm_med_m")
        rows.append(
            "<tr>"
            f"<td>{xml_escape(str(ch['name']))}</td>"
            f"<td>{'ja' if ch['split'] else 'nein'}</td>"
            f"<td>{xml_escape(str(ch['confidence']))}</td>"
            f"<td>{roof_med if roof_med is not None else '—'}</td>"
            f"<td>{peak_h if peak_h is not None else '—'}</td>"
            f"<td>{peak_n if peak_n is not None else '—'}</td>"
            f"<td>{xml_escape(str(place))} {xml_escape(str(compass))}</td>"
            f"<td>{nave_med if nave_med is not None else '—'}</td>"
            f"<td>{nave_max if nave_max is not None else '—'}</td>"
            f"<td>{tower_med if tower_med is not None else '—'}</td>"
            f"<td>{span if span is not None else '—'}</td>"
            f"<td>{xml_escape(str(ch.get('note') or ''))}</td>"
            f"<td><a href=\"{link}\">Satellit</a></td>"
            "</tr>"
        )
    path.write_text(
        f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><title>Kirchen {xml_escape(summary['site'])}</title>
<style>body{{font-family:sans-serif;max-width:76rem;margin:2rem auto;line-height:1.4}}
table{{border-collapse:collapse;width:100%}} td,th{{border-bottom:1px solid #ccc;padding:0.3rem;text-align:left}}
code{{background:#eee;padding:0.1em 0.3em}}</style></head><body>
<h1>Kirchen — Schiff und Turm</h1>
<p>{counts['churches']} Kirchen, davon {counts['split']} mit getrenntem Turmdach,
{counts['peak']} mit einer Spitze im DOM−DGM.
Sicherheit hoch {counts['confidence_hoch']}, mittel {counts['confidence_mittel']},
niedrig {counts['confidence_niedrig']}.
Teile aus tiris: {counts['source_tiris']}, nur OSM: {counts['source_osm']}.</p>
<p>Die gelbe Fläche <code>peak</code> ist die Gruppe hoher Pixel im Oberflächenmodell,
mindestens 8 m über dem Dachmedian. GeoJSON nach
<a href="https://geojson.io">geojson.io</a>, KML in Google Earth.</p>
<table>
<tr><th>Name</th><th>Turm getrennt</th><th>Sicherheit</th>
<th>Dach Median</th><th>Spitze Max</th><th>Pixel</th><th>Lage Raster</th>
<th>Polygon Median</th><th>Polygon Max</th><th>Turm Median</th><th>Spanne</th>
<th>Hinweis</th><th></th></tr>
{''.join(rows)}
</table>
</body></html>
""",
        encoding="utf-8",
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="OSM-Cache neu laden")
    args = ap.parse_args()
    site = load_site()
    sc = SiteCoords(site)
    to_crs = Transformer.from_crs("EPSG:4326", sc.crs, always_xy=True)
    to_wgs = Transformer.from_crs(sc.crs, "EPSG:4326", always_xy=True)
    data = fetch_osm_churches(site, force=args.force)
    records = osm_records(data, to_crs)
    print(f"OSM polygons area>={MIN_AREA_M2:.0f} m²: {len(records)}")
    grid = load_elev_grid(site)
    churches = build_churches(site, records, to_crs, grid)
    write_outputs(site, churches, to_wgs)


if __name__ == "__main__":
    main()
