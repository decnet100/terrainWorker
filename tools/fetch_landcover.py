"""Fetch Tirol landcover polygons (Landnutzung + Waldfläche) via ArcGIS FeatureServer.

WFS bbox on these hosted services often returns 0 features; FeatureServer
geometry queries work (same portal as GIP). Cache under data/raw/, summary
under data/processed/.

Usage:
  $env:AUTOROAD_SITE='config/sites/l13_kuehtai.yaml'
  python tools/fetch_landcover.py
  python tools/fetch_landcover.py --force
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import load_site, processed_dir, site_slug  # noqa: E402

SITE = load_site()
RAW = ROOT / "data" / "raw"
PROC = processed_dir(SITE)
RAW.mkdir(parents=True, exist_ok=True)
PROC.mkdir(parents=True, exist_ok=True)

CRS = str(SITE.get("crs", "EPSG:31254"))
BBOX = list(map(float, SITE["bbox"]))
XMIN, YMIN, XMAX, YMAX = BBOX
NAME = site_slug(SITE)

LC = (SITE.get("sources") or {}).get("landuse") or {}
# Prefer FeatureServer; fall back URLs from site yaml if provided.
DEFAULT_LAYERS = {
    "landnutzung": {
        "url": "https://services3.arcgis.com/hG7UfxX49PQ8XkXh/arcgis/rest/services/Landnutzung/FeatureServer/0",
        "label": "Landnutzung",
    },
    "waldflaeche": {
        "url": "https://services3.arcgis.com/hG7UfxX49PQ8XkXh/arcgis/rest/services/Waldflaeche/FeatureServer/0",
        "label": "Waldfläche",
    },
}
TIMEOUT = int(LC.get("timeout_s", 180))
PAGE = int(LC.get("page_size", 1000))


def _layers() -> dict[str, dict]:
    out = dict(DEFAULT_LAYERS)
    custom = LC.get("layers") or {}
    for key, meta in custom.items():
        if isinstance(meta, dict) and meta.get("url"):
            out[key] = {
                "url": str(meta["url"]),
                "label": str(meta.get("label") or key),
            }
        elif isinstance(meta, str):
            out[key] = {"url": meta, "label": key}
    return out


def _cache_key() -> str:
    payload = {
        "crs": CRS,
        "bbox": BBOX,
        "layers": {k: v["url"] for k, v in sorted(_layers().items())},
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def _paths(layer_key: str) -> tuple[Path, Path]:
    key = _cache_key()
    stem = f"landcover_{layer_key}_{NAME}_{key}"
    return RAW / f"{stem}.geojson", RAW / f"{stem}.meta.json"


def _envelope() -> dict:
    return {
        "xmin": XMIN,
        "ymin": YMIN,
        "xmax": XMAX,
        "ymax": YMAX,
        "spatialReference": {"wkid": int(CRS.split(":")[-1]) if ":" in CRS else 31254},
    }


def _query_layer(url: str) -> list[dict]:
    """Page through FeatureServer /query for site bbox."""
    geom = json.dumps(_envelope())
    all_feats: list[dict] = []
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
        feats = data.get("features") or []
        print(f"  offset={offset} -> {len(feats)}")
        all_feats.extend(feats)
        if len(feats) < PAGE:
            break
        offset += PAGE
    return all_feats


def _summarize(feats: list[dict]) -> dict:
    by_objekt: Counter[str] = Counter()
    by_klasse: Counter[str] = Counter()
    by_bez: Counter[str] = Counter()
    for f in feats:
        p = f.get("properties") or {}
        if p.get("OBJEKT") is not None:
            by_objekt[str(p["OBJEKT"])] += 1
        if p.get("KLASSE") is not None:
            by_klasse[str(p["KLASSE"])] += 1
        if p.get("OBJEKTBEZEICHNUNG") is not None:
            by_bez[str(p["OBJEKTBEZEICHNUNG"])] += 1
    return {
        "feature_count": len(feats),
        "objekt_counts": dict(by_objekt.most_common()),
        "klasse_counts": dict(by_klasse.most_common()),
        "bezeichnung_counts": dict(by_bez.most_common(40)),
    }


def fetch_one(layer_key: str, meta: dict, *, force: bool) -> dict:
    geo_path, meta_path = _paths(layer_key)
    if geo_path.is_file() and meta_path.is_file() and not force:
        cached = json.loads(meta_path.read_text(encoding="utf-8"))
        print(f"Using cache {geo_path.name} ({cached.get('feature_count', '?')} features)")
        return {
            "key": layer_key,
            "label": meta["label"],
            "geojson": geo_path,
            "meta": meta_path,
            "summary": cached,
        }

    print(f"Fetching {meta['label']} <- {meta['url']}")
    feats = _query_layer(meta["url"])
    collection = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "features": feats,
    }
    geo_path.write_text(json.dumps(collection, ensure_ascii=False), encoding="utf-8")
    summary = {
        "layer": layer_key,
        "label": meta["label"],
        "url": meta["url"],
        "crs_query": CRS,
        "bbox": BBOX,
        "cache_key": _cache_key(),
        **_summarize(feats),
    }
    meta_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {geo_path} features={len(feats)}")
    for label, counts in (
        ("OBJEKT", summary["objekt_counts"]),
        ("KLASSE", summary["klasse_counts"]),
    ):
        if counts:
            top = list(counts.items())[:8]
            print(f"  {label}: {top}")
    return {
        "key": layer_key,
        "label": meta["label"],
        "geojson": geo_path,
        "meta": meta_path,
        "summary": summary,
    }


def write_combined_summary(results: list[dict]) -> Path:
    out = {
        "site": NAME,
        "bbox": BBOX,
        "crs": CRS,
        "layers": {
            r["key"]: {
                "label": r["label"],
                "geojson": str(r["geojson"].relative_to(ROOT)).replace("\\", "/"),
                "meta": str(r["meta"].relative_to(ROOT)).replace("\\", "/"),
                **{k: v for k, v in r["summary"].items() if k not in {"layer", "label", "url"}},
            }
            for r in results
        },
    }
    path = PROC / "landcover_summary.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    # Stable pointers for build_terrain_masks
    index = {
        "landnutzung": None,
        "waldflaeche": None,
    }
    for r in results:
        if r["key"] in index:
            index[r["key"]] = str(r["geojson"].relative_to(ROOT)).replace("\\", "/")
    (PROC / "landcover_index.json").write_text(
        json.dumps(index, indent=2), encoding="utf-8"
    )
    print(f"Wrote {path}")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="Re-download even if cache exists")
    args = ap.parse_args()

    src_type = str(LC.get("type") or "featureserver").lower()
    if src_type in ("osm", "none", "off"):
        print(f"sources.landuse.type={src_type} — skipping Tirol landcover fetch")
        return

    results = []
    for key, meta in _layers().items():
        results.append(fetch_one(key, meta, force=args.force))
    write_combined_summary(results)


if __name__ == "__main__":
    main()
