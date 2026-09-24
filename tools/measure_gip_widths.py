"""Measure GIP carriageway width from Tirol Landnutzung traffic polygons.

Along the full GIP polyline (EPSG:31254, not the site-clipped BeamNG piece)
sample start / mid / end. At each point walk left and right until the LN-V*
polygon ends. Six values go into data/roads/gip_widths.json; the mean is what
load_gip_road_segments applies.

Usage:

  python tools\\measure_gip_widths.py
  python tools\\measure_gip_widths.py --site config/sites/reschen.yaml --force
  python tools\\measure_gip_widths.py --all-known
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform as shp_transform
from shapely.strtree import STRtree
from shapely.validation import make_valid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from build_bridges import find_gip_geojson  # noqa: E402
from gip_catalog import (  # noqa: E402
    LN_STREET_CODES,
    catalog_not_tunnel_oids,
    catalog_width_by_str_code,
    load_widths,
    save_widths,
)
from gip_road_segments import (  # noqa: E402
    _SUBORDINATE_LANE_OBJEKT,
    _coords_walk,
    _gip_segment_width_m,
    gip_objekt,
)
from road_edge import left_unit, polyline_length_xy, sample_center_at_s  # noqa: E402
from site_coords import load_site, site_slug  # noqa: E402

LN_URL = (
    "https://services3.arcgis.com/hG7UfxX49PQ8XkXh/arcgis/rest/services/"
    "Landnutzung/FeatureServer/0"
)
INSET_M = 8.0
STEP_MAX_M = 20.0
NEAR_M = 2.0
OUTLIER_FACTOR = 2.5
MEAN_FACTOR = 1.75
KNOWN_SITES = (
    "config/sites/fernpass.yaml",
    "config/sites/fernpass_mega.yaml",
    "config/sites/reschen.yaml",
    "config/sites/imst.yaml",
)

# Prefer the Landnutzung class that matches the GIP piece.
_LN_PREF = {
    "S-F": ("LN-VFW",),
    "S-GW": ("LN-VFW",),
    "S-FRW": ("LN-VFW", "LN-VSO"),
    "S-STRAIL": ("LN-VFW", "LN-VSO"),
    "S-G": ("LN-VSO", "LN-VPR"),
}


def _ln_to_31254() -> Transformer:
    """Landnutzung cache stays EPSG:4326 for now."""
    return Transformer.from_crs("EPSG:4326", "EPSG:31254", always_xy=True)


def _gip_to_31254(site: dict, data: dict) -> Transformer:
    from authorities import gip_fc_crs, make_grid_transformer, same_crs

    src = gip_fc_crs(data, site)
    dest = "EPSG:31254"
    if same_crs(src, dest):
        return Transformer.from_crs(src, dest, always_xy=True)
    return make_grid_transformer(src, dest, None)


def _walk_xy(geom: dict, tf: Transformer) -> list[tuple[float, float]]:
    pts: list = []
    _coords_walk((geom or {}).get("coordinates"), pts)
    out: list[tuple[float, float]] = []
    for p in pts:
        if len(p) < 2:
            continue
        x, y = tf.transform(float(p[0]), float(p[1]))
        if math.isfinite(x) and math.isfinite(y):
            out.append((float(x), float(y)))
    return out


def _sample_s(length: float) -> list[tuple[str, float]]:
    if length < 2.0:
        return [("mid", max(0.0, 0.5 * length))]
    inset = min(INSET_M, 0.15 * length)
    if inset * 2.0 >= length - 0.5:
        inset = max(0.0, 0.1 * length)
    return [
        ("start", inset),
        ("mid", 0.5 * length),
        ("end", max(inset, length - inset)),
    ]


def _ln_code(props: dict) -> str:
    return str(props.get("OBJEKT") or props.get("objekt") or "").upper().strip()


def _is_street_ln(props: dict) -> bool:
    code = _ln_code(props)
    if code in LN_STREET_CODES:
        return True
    # Some extracts keep the long Bezeichnung only.
    if code.startswith("LN-V") and code not in {"LN-VB", "LN-VF"}:
        return True
    return False


def find_landnutzung_geojson(site: dict) -> Path | None:
    raw = ROOT / "data" / "raw"
    slug = site_slug(site)
    matches = sorted(raw.glob(f"landcover_landnutzung_{slug}_*.geojson"))
    return matches[-1] if matches else None


def load_street_polygons(path: Path, tf: Transformer) -> tuple[list, list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    geoms = []
    codes: list[str] = []
    for f in data.get("features") or []:
        props = f.get("properties") or {}
        if not _is_street_ln(props):
            continue
        geom = f.get("geometry") or {}
        if not geom:
            continue
        try:
            shp = shape(geom)
        except Exception:
            continue
        if shp.is_empty:
            continue
        if shp.geom_type in ("MultiPolygon", "GeometryCollection"):
            parts = [p for p in shp.geoms if p.geom_type == "Polygon"]
        elif shp.geom_type == "Polygon":
            parts = [shp]
        else:
            continue
        code = _ln_code(props)
        if code not in LN_STREET_CODES and not code.startswith("LN-V"):
            code = "LN-VSU"
        for part in parts:
            poly = shp_transform(lambda x, y, z=None: tf.transform(x, y), part)
            if poly.is_empty:
                continue
            if not poly.is_valid:
                poly = make_valid(poly)
                if poly.geom_type != "Polygon":
                    if poly.geom_type == "MultiPolygon":
                        poly = max(poly.geoms, key=lambda g: g.area)
                    else:
                        continue
            geoms.append(poly)
            codes.append(code)
    return geoms, codes


def _pick_poly(
    tree: STRtree,
    geoms: list,
    codes: list[str],
    x: float,
    y: float,
    gip_obj: str,
) -> tuple[object | None, str | None]:
    pt = Point(x, y)
    hits = list(tree.query(pt.buffer(NEAR_M)))
    cands: list[tuple[float, float, int]] = []
    pref = _LN_PREF.get(gip_obj, ("LN-VSU", "LN-VSO", "LN-VPR", "LN-VFW"))
    for idx in hits:
        poly = geoms[int(idx)]
        code = codes[int(idx)]
        if poly.covers(pt) or poly.contains(pt):
            dist = 0.0
        else:
            dist = float(poly.distance(pt))
            if dist > NEAR_M:
                continue
        rank = pref.index(code) if code in pref else len(pref)
        cands.append((dist, float(rank), int(idx)))
    if not cands:
        return None, None
    cands.sort()
    idx = cands[0][2]
    return geoms[idx], codes[idx]


def _ray_extent(poly, x: float, y: float, dx: float, dy: float) -> float | None:
    n = math.hypot(dx, dy)
    if n < 1e-12:
        return None
    dx, dy = dx / n, dy / n
    start = Point(x, y)
    end = Point(x + dx * STEP_MAX_M, y + dy * STEP_MAX_M)
    ray = LineString([start, end])
    try:
        hit = poly.intersection(ray)
    except Exception:
        return None
    if hit.is_empty:
        return None
    dists: list[float] = []

    def collect(g) -> None:
        if g is None or g.is_empty:
            return
        t = g.geom_type
        if t == "Point":
            dists.append(math.hypot(float(g.x) - x, float(g.y) - y))
        elif t in ("LineString", "LinearRing"):
            for c in g.coords:
                dists.append(math.hypot(float(c[0]) - x, float(c[1]) - y))
        elif t in ("MultiLineString", "GeometryCollection", "MultiPoint"):
            for p in g.geoms:
                collect(p)

    collect(hit)
    if not dists:
        return None
    return float(min(STEP_MAX_M, max(dists)))


def _apply_reason(props: dict, oid: int) -> str | None:
    objekt = str(props.get("OBJEKT") or "").upper().strip()
    if objekt in _SUBORDINATE_LANE_OBJEKT:
        return "subordinate_lane"
    if objekt in ("S-AT", "S-BT", "S-LT", "S-BG"):
        if oid in catalog_not_tunnel_oids():
            return None
        return "tunnel_bore"
    kunst = str(props.get("KUNSTBAUTEN") or "").lower()
    if any(k in kunst for k in ("tunnel", "galerie", "unterflur")):
        if oid in catalog_not_tunnel_oids():
            return None
        return "kunstbauten"
    return None


def measure_feature(
    props: dict,
    xy: list[tuple[float, float]],
    tree: STRtree,
    geoms: list,
    codes: list[str],
    *,
    class_width_m: float,
) -> dict:
    from authorities import gip_source_oid  # noqa: WPS433

    src = gip_source_oid(props)
    oid = int(src if src is not None else props.get("OBJECTID"))
    nodes = [[p[0], p[1], 0.0] for p in xy]
    length = polyline_length_xy(nodes)
    samples = []
    widths: list[float] = []
    gip_obj = str(props.get("OBJEKT") or "").upper().strip()
    for at, s in _sample_s(length):
        rec = sample_center_at_s(nodes, s)
        row = {
            "at": at,
            "s_m": round(float(s), 2),
            "left_m": None,
            "right_m": None,
            "ln_objekt": None,
        }
        if rec is None:
            samples.append(row)
            continue
        x, y, _z, tx, ty, _w = rec
        lx, ly = left_unit(tx, ty)
        poly, ln_code = _pick_poly(tree, geoms, codes, x, y, gip_obj)
        if poly is None:
            samples.append(row)
            continue
        row["ln_objekt"] = ln_code
        left = _ray_extent(poly, x, y, lx, ly)
        right = _ray_extent(poly, x, y, -lx, -ly)
        row["left_m"] = None if left is None else round(float(left), 2)
        row["right_m"] = None if right is None else round(float(right), 2)
        if left is not None and right is not None:
            w = float(left) + float(right)
            cap = max(6.0, OUTLIER_FACTOR * float(class_width_m))
            if 1.5 <= w <= cap:
                widths.append(w)
            else:
                row["reject"] = f"width {w:.2f} m outside 1.5-{cap:.1f}"
        samples.append(row)
    reason = _apply_reason(props, oid)
    mean = round(sum(widths) / len(widths), 2) if widths else None
    applied = reason is None and mean is not None and len(widths) >= 2
    if applied and mean > MEAN_FACTOR * float(class_width_m):
        applied = False
        reason = "mean_above_class"
    return {
        "objectid": oid,
        "str_code": props.get("STR_CODE"),
        "gip_objekt": props.get("OBJEKT"),
        "length_m": round(float(length), 2),
        "samples": samples,
        "n_valid": len(widths),
        "width_mean_m": mean,
        "class_width_m": round(float(class_width_m), 2),
        "applied": applied,
        "skip": reason,
    }


def measure_site(site: dict, catalog: dict, *, force: bool) -> dict:
    gip_path = find_gip_geojson(site)
    ln_path = find_landnutzung_geojson(site)
    if ln_path is None or not ln_path.is_file():
        raise SystemExit(
            f"No Landnutzung GeoJSON for {site_slug(site)}. "
            "Run: python tools\\fetch_landcover.py"
        )
    print(f"GIP        {gip_path.name}")
    print(f"Landnutzung {ln_path.name}")
    data = json.loads(gip_path.read_text(encoding="utf-8"))
    ln_tf = _ln_to_31254()
    gip_tf = _gip_to_31254(site, data)
    geoms, codes = load_street_polygons(ln_path, ln_tf)
    print(f"Street polygons: {len(geoms)}")
    if not geoms:
        raise SystemExit("No LN-V* street polygons in Landnutzung cache")
    tree = STRtree(geoms)
    feats = data.get("features") or []
    segs = catalog.setdefault("segments", {})
    n_new = n_skip = n_applied = 0
    from authorities import gip_source_oid, stamp_gip_props  # noqa: WPS433

    for f in feats:
        props = stamp_gip_props(site, f.get("properties") or {})
        src = gip_source_oid(props)
        if src is None:
            continue
        oid = int(src)
        key = str(oid)
        if not force and key in segs:
            n_skip += 1
            continue
        xy = _walk_xy(f.get("geometry") or {}, gip_tf)
        if len(xy) < 2:
            continue
        class_w = _gip_segment_width_m(
            props,
            7.5,
            by_code=catalog_width_by_str_code(),
            by_lanes=None,
            lane_width_m=3.75,
        )
        rec = measure_feature(props, xy, tree, geoms, codes, class_width_m=class_w)
        rec["site"] = site_slug(site)
        segs[key] = rec
        n_new += 1
        if rec.get("applied"):
            n_applied += 1
        if n_new and n_new % 200 == 0:
            print(f"  ... {n_new} measured")
    print(
        f"{site_slug(site)}: wrote {n_new} (applied={n_applied}) "
        f"already={n_skip} catalog={len(segs)}"
    )
    return catalog


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--site",
        action="append",
        default=[],
        help="Site YAML (repeatable). Default: AUTOROAD_SITE / config/site.yaml",
    )
    ap.add_argument(
        "--all-known",
        action="store_true",
        help="Fernpass 4096/8192, Reschen, Imst",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Re-measure OBJECTIDs already in the catalog",
    )
    args = ap.parse_args()
    paths = list(args.site)
    if args.all_known:
        paths = list(KNOWN_SITES)
    if not paths:
        paths = [None]
    catalog = load_widths(force=True)
    catalog["ln_codes"] = sorted(LN_STREET_CODES)
    for p in paths:
        site = load_site(p)
        print(f"=== {site_slug(site)} ===")
        catalog = measure_site(site, catalog, force=bool(args.force))
    out = save_widths(catalog)
    applied = sum(
        1 for s in (catalog.get("segments") or {}).values() if s.get("applied")
    )
    print(f"Wrote {out} segments={len(catalog.get('segments') or {})} applied={applied}")


if __name__ == "__main__":
    main()
