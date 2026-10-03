"""Download Tirol Gewässernetz (Fliessgewässer centerlines) for the site BBOX.

Land Tirol OGD, CC BY 3.0 AT. Native CRS is EPSG:31254. Same FeatureServer
bbox paging as fetch_gip (WFS bbox on this host is empty).

Usage:
  $env:AUTOROAD_SITE='config/sites/fernpass.yaml'
  python tools/fetch_waterways.py
  python tools/fetch_waterways.py --force
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

import requests
from shapely.geometry import LineString, MultiLineString, box, mapping, shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import SiteCoords, load_site, processed_dir, site_slug  # noqa: E402

DEFAULT_FS_QUERY = (
    "https://services3.arcgis.com/hG7UfxX49PQ8XkXh/arcgis/rest/services/"
    "Fliessgewaesser/FeatureServer/0/query"
)


def _src(site: dict) -> dict:
    return ((site.get("sources") or {}).get("waterways") or {})


def _fix_mojibake(obj):
    if isinstance(obj, str):
        try:
            if "Ã" in obj or "Â" in obj:
                return obj.encode("latin-1").decode("utf-8")
            return obj
        except (UnicodeDecodeError, UnicodeEncodeError):
            return obj
    if isinstance(obj, list):
        return [_fix_mojibake(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _fix_mojibake(v) for k, v in obj.items()}
    return obj


def _crs_wkid(crs: str) -> int:
    text = str(crs or "EPSG:31254").upper().replace("EPSG:", "").strip()
    try:
        return int(text)
    except ValueError:
        return 31254


def cache_key(site: dict) -> str:
    coords = SiteCoords(site)
    src = _src(site)
    payload = {
        "url": str(src.get("url") or DEFAULT_FS_QUERY),
        "crs": coords.crs,
        "bbox": [coords.xmin, coords.ymin, coords.xmax, coords.ymax],
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def cache_paths(site: dict) -> tuple[Path, Path, Path]:
    slug = site_slug(site)
    key = cache_key(site)
    raw = ROOT / "data" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    stem = f"waterways_{slug}_{key}"
    return raw / f"{stem}.geojson", raw / f"{stem}.meta.json", processed_dir(site) / "waterways.geojson"


def _as_lines(geom: dict) -> list[LineString]:
    if not geom:
        return []
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    lines: list[LineString] = []
    if gtype == "LineString" and coords and len(coords) >= 2:
        xy = [(float(c[0]), float(c[1])) for c in coords if len(c) >= 2]
        if len(xy) >= 2:
            lines.append(LineString(xy))
    elif gtype == "MultiLineString":
        for part in coords or []:
            if part and len(part) >= 2:
                xy = [(float(c[0]), float(c[1])) for c in part if len(c) >= 2]
                if len(xy) >= 2:
                    lines.append(LineString(xy))
    else:
        try:
            g = shape(geom)
        except Exception:
            return []
        if g.is_empty:
            return []
        if g.geom_type == "LineString":
            lines.append(g)
        elif g.geom_type == "MultiLineString":
            lines.extend(p for p in g.geoms if not p.is_empty and p.length > 0)
        elif g.geom_type == "GeometryCollection":
            for p in g.geoms:
                if p.geom_type == "LineString" and not p.is_empty:
                    lines.append(p)
                elif p.geom_type == "MultiLineString":
                    lines.extend(q for q in p.geoms if not q.is_empty)
    return [ln for ln in lines if ln.length > 0.5]


def _clip_features(features: list[dict], xmin: float, ymin: float, xmax: float, ymax: float) -> list[dict]:
    frame = box(xmin, ymin, xmax, ymax)
    kept: list[dict] = []
    for feat in features:
        lines = _as_lines(feat.get("geometry") or {})
        if not lines:
            continue
        try:
            geom = unary_union(lines)
            clipped = geom.intersection(frame)
        except Exception:
            continue
        if clipped.is_empty:
            continue
        parts = _as_lines(mapping(clipped))
        if not parts:
            continue
        out_geom = (
            mapping(parts[0])
            if len(parts) == 1
            else mapping(MultiLineString(parts))
        )
        kept.append(
            {
                "type": "Feature",
                "properties": feat.get("properties") or {},
                "geometry": out_geom,
            }
        )
    return kept


def _fetch_bbox(site: dict) -> list[dict]:
    coords = SiteCoords(site)
    src = _src(site)
    url = str(src.get("url") or DEFAULT_FS_QUERY)
    page = max(1, min(int(src.get("page_size") or 500), 2000))
    timeout = int(src.get("timeout_s") or 300)
    wkid = _crs_wkid(coords.crs)
    all_feats: list[dict] = []
    offset = 0
    print(
        f"Downloading Fliessgewaesser BBOX "
        f"[{coords.xmin:.1f},{coords.ymin:.1f},{coords.xmax:.1f},{coords.ymax:.1f}] "
        f"inSR={wkid}"
    )
    headers = {"User-Agent": "beamng_autoroad/0.1", "Accept": "application/json"}
    while True:
        params = {
            "where": "1=1",
            "geometry": json.dumps(
                {
                    "xmin": coords.xmin,
                    "ymin": coords.ymin,
                    "xmax": coords.xmax,
                    "ymax": coords.ymax,
                    "spatialReference": {"wkid": wkid},
                }
            ),
            "geometryType": "esriGeometryEnvelope",
            "inSR": wkid,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": wkid,
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": page,
        }
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        data = _fix_mojibake(r.json())
        if data.get("error"):
            raise RuntimeError(f"FeatureServer error: {data.get('error')}")
        feats = data.get("features") or []
        print(f"  FeatureServer offset={offset} -> {len(feats)}")
        all_feats.extend(feats)
        exceeded = bool(
            data.get("exceededTransferLimit")
            or (data.get("properties") or {}).get("exceededTransferLimit")
        )
        if not feats or (len(feats) < page and not exceeded):
            break
        offset += len(feats)
        if offset > 100_000:
            print("WARNING: waterways paging stopped at 100000 features")
            break
    print(f"  FeatureServer total: {len(all_feats)}")
    return all_feats


def _summarize(features: list[dict]) -> dict:
    types = Counter()
    grkat = Counter()
    named = 0
    length_m = 0.0
    for f in features:
        props = f.get("properties") or {}
        types[str(props.get("GEW_TYP") or "(none)")] += 1
        grkat[str(props.get("GRKATWRRL") or "(none)")] += 1
        if str(props.get("GEW_NAME") or "").strip():
            named += 1
        for ln in _as_lines(f.get("geometry") or {}):
            length_m += float(ln.length)
    return {
        "feature_count": len(features),
        "named": named,
        "length_km": round(length_m / 1000.0, 2),
        "gew_typ": dict(types.most_common()),
        "grkatwrrl": dict(grkat.most_common(20)),
    }


def load_waterways(site: dict | None = None) -> dict:
    """Cached FeatureCollection in the site working CRS, or empty FC."""
    site = site or load_site()
    _, _, proc_path = cache_paths(site)
    if not proc_path.is_file():
        return {"type": "FeatureCollection", "features": []}
    try:
        data = json.loads(proc_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"type": "FeatureCollection", "features": []}
    if not isinstance(data, dict):
        return {"type": "FeatureCollection", "features": []}
    data.setdefault("type", "FeatureCollection")
    data.setdefault("features", [])
    return data


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="Re-download even if cache exists")
    args = ap.parse_args()

    site = load_site()
    if not _src(site):
        raise SystemExit("sources.waterways missing — add the FeatureServer URL in the site YAML")
    coords = SiteCoords(site)
    cache_path, meta_path, proc_path = cache_paths(site)
    src = _src(site)

    if cache_path.is_file() and meta_path.is_file() and not args.force:
        print(f"Using cached waterways: {cache_path}")
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        feats = _clip_features(
            data.get("features") or [],
            coords.xmin,
            coords.ymin,
            coords.xmax,
            coords.ymax,
        )
    else:
        raw = _fetch_bbox(site)
        feats = _clip_features(raw, coords.xmin, coords.ymin, coords.xmax, coords.ymax)
        fc = {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": coords.crs}},
            "features": feats,
        }
        cache_path.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
        data = fc

    summary = _summarize(feats)
    meta = {
        "cache_key": cache_key(site),
        "url": str(src.get("url") or DEFAULT_FS_QUERY),
        "crs": coords.crs,
        "bbox": [coords.xmin, coords.ymin, coords.xmax, coords.ymax],
        "feature_count": summary["feature_count"],
        "attribution": "Land Tirol — Gewässernetz, CC BY 3.0 AT",
        "gew_typ": summary["gew_typ"],
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    proc_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "crs": {"type": "name", "properties": {"name": coords.crs}},
                "features": feats,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    summary_path = processed_dir(site) / "waterways_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {cache_path}")
    print(f"Wrote {proc_path} ({summary['feature_count']} lines, {summary['length_km']} km)")
    print(f"  GEW_TYP: {summary['gew_typ']}")
    print("Attribution: Land Tirol — Gewässernetz (CC BY 3.0 AT)")


if __name__ == "__main__":
    main()
