"""Fetch tiris Gebäude (roof polygons) for the site bbox via FeatureServer.

Same ArcGIS host as Landnutzung / GIP. Writes WGS84 GeoJSON + KML for
overlay against OSM / Google. Optional second KML folder from
building_roof_class.geojson if that file exists.

Usage:
  $env:AUTOROAD_SITE='config/sites/reschen.yaml'
  python tools/fetch_tiris_buildings.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import load_site, processed_dir, site_slug  # noqa: E402

DEFAULT_URL = (
    "https://services3.arcgis.com/hG7UfxX49PQ8XkXh/arcgis/rest/services/"
    "Gebaeude/FeatureServer/0"
)
PAGE = 2000
TIMEOUT = 180


def _src(site: dict) -> dict:
    return (site.get("sources") or {}).get("tiris_buildings") or {}


def _url(site: dict) -> str:
    return str(_src(site).get("url") or DEFAULT_URL)


def _cache_stem(site: dict) -> str:
    bbox = list(map(float, site["bbox"]))
    payload = {"url": _url(site), "bbox": bbox, "crs": site.get("crs")}
    key = hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return f"tiris_buildings_{site_slug(site)}_{key}"


def _envelope(site: dict) -> dict:
    xmin, ymin, xmax, ymax = map(float, site["bbox"])
    wkid = 31254
    crs = str(site.get("crs") or "EPSG:31254")
    if ":" in crs:
        try:
            wkid = int(crs.split(":")[-1])
        except ValueError:
            pass
    return {
        "xmin": xmin,
        "ymin": ymin,
        "xmax": xmax,
        "ymax": ymax,
        "spatialReference": {"wkid": wkid},
    }


def query_bbox(site: dict) -> list[dict]:
    url = _url(site).rstrip("/") + "/query"
    geom = json.dumps(_envelope(site))
    wkid = _envelope(site)["spatialReference"]["wkid"]
    all_feats: list[dict] = []
    offset = 0
    print(f"Fetching tiris Gebäude BBOX={site['bbox']} inSR={wkid}")
    while True:
        params = {
            "f": "geojson",
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "true",
            "geometry": geom,
            "geometryType": "esriGeometryEnvelope",
            "inSR": wkid,
            "spatialRel": "esriSpatialRelIntersects",
            "outSR": 4326,
            "resultOffset": offset,
            "resultRecordCount": PAGE,
        }
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(data["error"])
        feats = data.get("features") or []
        print(f"  offset={offset} -> {len(feats)}", flush=True)
        all_feats.extend(feats)
        exceeded = bool(
            data.get("exceededTransferLimit")
            or (data.get("properties") or {}).get("exceededTransferLimit")
        )
        if not feats or (len(feats) < PAGE and not exceeded):
            break
        offset += len(feats)
        if offset > 200_000:
            print("WARNING: stopped paging at 200000 features")
            break
    return all_feats


def _ring_coords(geom: dict) -> list[list[tuple[float, float]]]:
    if not geom:
        return []
    t = geom.get("type")
    coords = geom.get("coordinates") or []
    if t == "Polygon":
        return [[(float(x), float(y)) for x, y, *_ in ring] for ring in coords]
    if t == "MultiPolygon":
        out = []
        for poly in coords:
            for ring in poly:
                out.append([(float(x), float(y)) for x, y, *_ in ring])
        return out
    return []


def write_kml(path: Path, folders: list[tuple[str, str, list[dict]]]) -> None:
    """folders: (name, kml_poly_color AABBGGRR, geojson features)."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        "<Document><name>tiris vs OSM buildings</name>",
    ]
    for i, (name, color, feats) in enumerate(folders):
        sid = f"s{i}"
        parts.append(
            f'<Style id="{sid}"><PolyStyle><color>{color}</color><outline>1</outline></PolyStyle>'
            f"<LineStyle><color>ff000000</color><width>1</width></LineStyle></Style>"
        )
        parts.append(f"<Folder><name>{xml_escape(name)} ({len(feats)})</name>")
        for feat in feats:
            rings = _ring_coords(feat.get("geometry") or {})
            if not rings:
                continue
            props = feat.get("properties") or {}
            label = (
                props.get("name")
                or props.get("OBJECTID")
                or props.get("osm_id")
                or ""
            )
            h = props.get("GEB_HOEHE_MEDIAN") or props.get("ndsm_med_m") or ""
            desc = xml_escape(str(h))
            outer = rings[0]
            coord = " ".join(f"{x:.7f},{y:.7f},0" for x, y in outer)
            parts.append(
                f"<Placemark><name>{xml_escape(str(label))}</name>"
                f"<styleUrl>#{sid}</styleUrl><description>{desc}</description>"
                f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{coord}"
                f"</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
            )
        parts.append("</Folder>")
    parts.append("</Document></kml>")
    path.write_text("\n".join(parts), encoding="utf-8")


def write_html(path: Path, n_tiris: int, n_osm: int | None, slug: str) -> None:
    osm_line = (
        f"<p>OSM-Klassen (zum Vergleich): {n_osm} Polygone in "
        f"<code>building_roof_class.geojson</code>.</p>"
        if n_osm is not None
        else ""
    )
    path.write_text(
        f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><title>tiris Gebäude {slug}</title>
<style>body{{font-family:sans-serif;max-width:48rem;margin:2rem auto;line-height:1.4}}
code{{background:#eee;padding:0.1em 0.3em}}</style></head><body>
<h1>tiris Gebäude — {slug}</h1>
<p>{n_tiris} Dachflächen (tiris, CC-BY). GeoJSON nach
<a href="https://geojson.io">geojson.io</a> ziehen, KML in Google Earth.
Farbe in der Vergleichs-KML: tiris orange, OSM blau.</p>
{osm_line}
<p>Dateien: <code>tiris_buildings.geojson</code>, <code>tiris_buildings.kml</code>,
<code>buildings_osm_vs_tiris.kml</code>.</p>
</body></html>
""",
        encoding="utf-8",
    )


def load_tiris_geojson(site: dict, proc: Path, *, force: bool = False) -> dict:
    """Return FeatureCollection, fetching into processed/ if missing."""
    path = proc / "tiris_buildings.geojson"
    if path.is_file() and path.stat().st_size > 100 and not force:
        print(f"Using cached tiris buildings: {path}")
        return json.loads(path.read_text(encoding="utf-8"))
    feats = query_bbox(site)
    collection = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "features": feats,
    }
    path.write_text(json.dumps(collection, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {path} features={len(feats)}")
    return collection


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    site = load_site()
    slug = site_slug(site)
    raw = ROOT / "data" / "raw"
    proc = processed_dir(site)
    raw.mkdir(parents=True, exist_ok=True)
    stem = _cache_stem(site)
    raw_geo = raw / f"{stem}.geojson"
    raw_meta = raw / f"{stem}.meta.json"

    if raw_geo.is_file() and raw_meta.is_file() and not args.force:
        print(f"Using cache {raw_geo.name}")
        collection = json.loads(raw_geo.read_text(encoding="utf-8"))
        feats = collection.get("features") or []
    else:
        feats = query_bbox(site)
        collection = {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
            "features": feats,
        }
        raw_geo.write_text(json.dumps(collection, ensure_ascii=False), encoding="utf-8")
        heights = [
            float(p["GEB_HOEHE_MEDIAN"])
            for f in feats
            for p in [f.get("properties") or {}]
            if p.get("GEB_HOEHE_MEDIAN") is not None
        ]
        meta = {
            "site": slug,
            "url": _url(site),
            "bbox": list(map(float, site["bbox"])),
            "feature_count": len(feats),
            "height_median_p50": sorted(heights)[len(heights) // 2] if heights else None,
            "cache": str(raw_geo.relative_to(ROOT)).replace("\\", "/"),
        }
        raw_meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"Wrote {raw_geo} features={len(feats)}")

    proc_geo = proc / "tiris_buildings.geojson"
    proc_geo.write_text(json.dumps(collection, ensure_ascii=False), encoding="utf-8")
    write_kml(proc / "tiris_buildings.kml", [("tiris Gebäude", "c01478dc", feats)])

    osm_path = proc / "building_roof_class.geojson"
    osm_feats = None
    if osm_path.is_file():
        osm_feats = (json.loads(osm_path.read_text(encoding="utf-8")).get("features") or [])
        write_kml(
            proc / "buildings_osm_vs_tiris.kml",
            [
                ("OSM (Klassen)", "c0f0a000", osm_feats),
                ("tiris Dachflächen", "c01478dc", feats),
            ],
        )
        print(f"Wrote comparison KML OSM={len(osm_feats)} tiris={len(feats)}")
    write_html(
        proc / "tiris_buildings.html",
        len(feats),
        len(osm_feats) if osm_feats is not None else None,
        slug,
    )
    print(f"Wrote {proc_geo} ({len(feats)} buildings)")


if __name__ == "__main__":
    main()
