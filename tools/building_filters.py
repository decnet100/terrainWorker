"""Reject rules for tiris roofprints before meshing.

Rules (independent; a roof can match more than one):

1. Flat — ``GEB_HOEHE_MEDIAN`` below ``flat_median_max_m`` (courtyard / pad).
2. Container — larger roof that covers another (area share or contains its centroid).
3. On road — share of area inside the carriageway mesh clip above a fraction.

Shared OBJECTID list (all sites): ``data/assets/buildings/tiris_exclude.json``.
Tiris IDs are global, so one site's rejects apply when another bbox overlaps.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union
from shapely.strtree import STRtree
from shapely.validation import make_valid

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_PATH = ROOT / "data" / "assets" / "buildings" / "tiris_exclude.json"

# Defaults match the Imst discussion: drop flush pads, wrappers, road blockers.
FLAT_MEDIAN_MAX_M = 2.0
ROAD_OVERLAP_MIN = 0.10
CONTAINER_COVER_MIN = 0.80
CONTAINER_AREA_MIN_M2 = 150.0


@dataclass
class RoofRec:
    oid: int
    poly: Polygon
    area_m2: float
    h_med: float | None
    h_max: float | None


def _as_float(raw) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _poly_crs(geom: dict, to_crs: Transformer) -> Polygon | None:
    if not geom:
        return None
    kind = geom.get("type")
    coords = geom.get("coordinates") or []
    try:
        if kind == "Polygon":
            poly = Polygon(coords[0], coords[1:] if len(coords) > 1 else None)
        elif kind == "MultiPolygon":
            parts = [Polygon(p[0], p[1:] if len(p) > 1 else None) for p in coords]
            parts = [p for p in parts if not p.is_empty]
            if not parts:
                return None
            poly = max(parts, key=lambda p: p.area)
        else:
            return None
    except Exception:  # noqa: BLE001
        return None
    if not poly.is_valid:
        poly = make_valid(poly)
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        if poly.geom_type != "Polygon":
            return None
    if poly.is_empty or poly.area <= 0:
        return None
    poly = shp_transform(to_crs.transform, poly)
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda g: g.area)
    if poly.geom_type != "Polygon" or poly.is_empty:
        return None
    return orient(poly, sign=1.0)


def load_tiris_roofs(
    data: dict,
    site: dict,
    *,
    min_area_m2: float = 25.0,
    max_area_m2: float = 8000.0,
) -> list[RoofRec]:
    """Tiris roof polygons in the site CRS, same area window as build_buildings."""
    from site_coords import SiteCoords  # noqa: WPS433

    sc = SiteCoords(site)
    to_crs = Transformer.from_crs("EPSG:4326", sc.crs, always_xy=True)
    out: list[RoofRec] = []
    for feat in data.get("features") or []:
        if not isinstance(feat, dict):
            continue
        props = feat.get("properties") or {}
        oid = props.get("OBJECTID")
        if oid is None:
            continue
        try:
            oid_i = int(oid)
        except (TypeError, ValueError):
            continue
        poly = _poly_crs(feat.get("geometry") or {}, to_crs)
        if poly is None:
            continue
        area = float(poly.area)
        if area < min_area_m2 or area > max_area_m2:
            continue
        cx, cy = float(poly.centroid.x), float(poly.centroid.y)
        bx, by = sc.crs_to_terrain(cx, cy)
        if bx < 0 or by < 0 or bx > sc.terrain_span or by > sc.terrain_span:
            continue
        out.append(
            RoofRec(
                oid=oid_i,
                poly=poly,
                area_m2=area,
                h_med=_as_float(props.get("GEB_HOEHE_MEDIAN")),
                h_max=_as_float(props.get("GEB_HOEHE_MAX")),
            )
        )
    return out


def is_flat(rec: RoofRec, *, median_max_m: float = FLAT_MEDIAN_MAX_M) -> bool:
    """True when the roof body is flush with the ground (median height)."""
    if rec.h_med is None:
        return False
    return float(rec.h_med) < float(median_max_m)


def load_carriageway_union(site: dict, proc: Path):
    """Union of carriageway mesh-clip polygons (same surface as the road mesh)."""
    import geopandas as gpd  # noqa: WPS433

    path = proc / "road_surface_smooth" / "carriageway_mesh_clip.gpkg"
    if not path.is_file():
        return None
    gdf = gpd.read_file(path)
    if gdf.empty:
        return None
    return unary_union(list(gdf.geometry))


def road_overlap_frac(rec: RoofRec, carriageway) -> float:
    if carriageway is None or rec.poly.is_empty or rec.area_m2 <= 0:
        return 0.0
    inter = rec.poly.intersection(carriageway)
    if inter.is_empty:
        return 0.0
    return float(inter.area) / float(rec.area_m2)


def is_on_road(
    rec: RoofRec,
    carriageway,
    *,
    overlap_min: float = ROAD_OVERLAP_MIN,
) -> bool:
    return road_overlap_frac(rec, carriageway) >= float(overlap_min)


def _convex_fill(poly: Polygon) -> float:
    hull = poly.convex_hull
    if hull.is_empty or hull.area <= 0:
        return 0.0
    return float(poly.area) / float(hull.area)


def container_oids(
    roofs: list[RoofRec],
    *,
    cover_min: float = CONTAINER_COVER_MIN,
    min_area_m2: float = CONTAINER_AREA_MIN_M2,
    max_convex_fill: float = 0.55,
) -> dict[int, list[int]]:
    """Map container OBJECTID → list of covered child OBJECTIDs.

    A is rejected as a container only when it nests another roof *and* looks
    like a wrapper (flat median, or low convex fill), not a solid house that
    merely touches a strip whose centroid falls inside (e.g. 194056/194055).
    """
    if not roofs:
        return {}
    polys = [r.poly for r in roofs]
    tree = STRtree(polys)
    by_idx = {i: r for i, r in enumerate(roofs)}
    out: dict[int, list[int]] = {}
    for i, a in enumerate(roofs):
        if a.area_m2 < float(min_area_m2):
            continue
        wrapper = is_flat(a) or _convex_fill(a.poly) < float(max_convex_fill)
        if not wrapper:
            continue
        kids: set[int] = set()
        for j in tree.query(a.poly):
            j = int(j)
            if j == i:
                continue
            b = by_idx[j]
            if b.area_m2 >= a.area_m2:
                continue
            inter = float(a.poly.intersection(b.poly).area)
            if inter / b.area_m2 >= float(cover_min) or a.poly.contains(b.poly.centroid):
                kids.add(b.oid)
        if kids:
            out[a.oid] = sorted(kids)
    return out


def load_tiris_exclude(path: Path | None = None) -> set[int]:
    """Global tiris OBJECTIDs to skip (shared across sites)."""
    p = path or EXCLUDE_PATH
    if not p.is_file():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    ids = data.get("ids") if isinstance(data, dict) else data
    out: set[int] = set()
    if not isinstance(ids, list):
        return out
    for x in ids:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _oid_list(raw) -> list[int]:
    out: list[int] = []
    if not isinstance(raw, list):
        return out
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def merge_tiris_exclude(
    *,
    site_slug: str,
    flat: set[int],
    on_road: set[int],
    container: set[int],
    path: Path | None = None,
) -> Path:
    """Merge site rejects into the shared exclude file.

    Each site's contribution is replaced on re-run; ``ids`` is the union of
    every site so overlapping bboxes keep rejects from all of them.
    """
    p = path or EXCLUDE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    prev: dict = {}
    if p.is_file():
        try:
            prev = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prev = {}
    if not isinstance(prev, dict):
        prev = {}

    sources = prev.get("sources") if isinstance(prev.get("sources"), dict) else {}
    # Drop legacy count-only entries without oid lists.
    cleaned: dict = {}
    for key, val in sources.items():
        if not isinstance(val, dict):
            continue
        if "flat_ids" in val or "ids" in val or key == site_slug:
            cleaned[key] = val
    sources = cleaned
    sources[site_slug] = {
        "flat_ids": sorted(flat),
        "on_road_ids": sorted(on_road),
        "container_ids": sorted(container),
        "flat": len(flat),
        "on_road": len(on_road),
        "container": len(container),
        "union": len(flat | on_road | container),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    by_reason: dict[str, set[int]] = {"flat": set(), "on_road": set(), "container": set()}
    for val in sources.values():
        if not isinstance(val, dict):
            continue
        by_reason["flat"].update(_oid_list(val.get("flat_ids")))
        by_reason["on_road"].update(_oid_list(val.get("on_road_ids")))
        by_reason["container"].update(_oid_list(val.get("container_ids")))

    all_ids = sorted(by_reason["flat"] | by_reason["on_road"] | by_reason["container"])
    payload = {
        "description": (
            "Tiris Gebäude OBJECTIDs to skip when building BeamNG TSStatics. "
            "IDs are global to the tiris layer; sites share this list so an "
            "overlap in another bbox stays excluded."
        ),
        "ids": all_ids,
        "by_reason": {k: sorted(v) for k, v in by_reason.items()},
        "sources": sources,
    }
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def classify_rejects(
    roofs: list[RoofRec],
    carriageway,
    *,
    flat_median_max_m: float = FLAT_MEDIAN_MAX_M,
    road_overlap_min: float = ROAD_OVERLAP_MIN,
    cover_min: float = CONTAINER_COVER_MIN,
    container_area_min_m2: float = CONTAINER_AREA_MIN_M2,
) -> dict:
    """Return per-rule oid sets and overlap fractions for reporting."""
    flat = {r.oid for r in roofs if is_flat(r, median_max_m=flat_median_max_m)}
    road_frac = {r.oid: road_overlap_frac(r, carriageway) for r in roofs}
    on_road = {oid for oid, frac in road_frac.items() if frac >= float(road_overlap_min)}
    containers = container_oids(
        roofs, cover_min=cover_min, min_area_m2=container_area_min_m2
    )
    container_set = set(containers)
    union = flat | on_road | container_set
    return {
        "flat": flat,
        "on_road": on_road,
        "container": container_set,
        "containers": containers,
        "road_frac": road_frac,
        "union": union,
        "n_roofs": len(roofs),
    }
