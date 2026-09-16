"""Convert Overpass OSM JSON road dumps to GeoJSON LineStrings.

Writes next to each source:
  *.geojson                 — EPSG:4326
  *_epsgNNNN.geojson        — site CRS (from AUTOROAD_SITE / site.yaml)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import load_site  # noqa: E402


def overpass_ways_to_fc(data: dict, *, transformer: Transformer | None = None) -> dict:
    feats: list[dict] = []
    for e in data.get("elements") or []:
        if e.get("type") != "way":
            continue
        geom = e.get("geometry") or []
        if len(geom) < 2:
            continue
        coords: list[list[float]] = []
        for p in geom:
            lon, lat = float(p["lon"]), float(p["lat"])
            if transformer is not None:
                x, y = transformer.transform(lon, lat)
                coords.append([float(x), float(y)])
            else:
                coords.append([lon, lat])
        tags = dict(e.get("tags") or {})
        props = {
            "osm_id": e.get("id"),
            "highway": tags.get("highway"),
            "name": tags.get("name"),
            "ref": tags.get("ref"),
            "lanes": tags.get("lanes"),
            "width": tags.get("width"),
            "surface": tags.get("surface"),
        }
        for k, v in tags.items():
            if k not in props:
                props[k] = v
        feats.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": {"type": "LineString", "coordinates": coords},
            }
        )
    return {"type": "FeatureCollection", "features": feats}


def convert_file(src: Path, *, site_crs: str, to_site: Transformer) -> None:
    data = json.loads(src.read_text(encoding="utf-8"))
    fc4326 = overpass_ways_to_fc(data)
    out4326 = src.with_suffix(".geojson")
    out4326.write_text(json.dumps(fc4326, ensure_ascii=False), encoding="utf-8")
    print(f"{out4326}: {len(fc4326['features'])} ways CRS=EPSG:4326")

    fc_site = overpass_ways_to_fc(data, transformer=to_site)
    fc_site["crs"] = {"type": "name", "properties": {"name": site_crs}}
    slug = site_crs.replace(":", "").lower()
    out_site = src.with_name(f"{src.stem}_{slug}.geojson")
    out_site.write_text(json.dumps(fc_site, ensure_ascii=False), encoding="utf-8")
    print(f"{out_site}: {len(fc_site['features'])} ways CRS={site_crs}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="Overpass JSON files (default: fernpass osm_roads caches)",
    )
    args = ap.parse_args()
    site = load_site()
    site_crs = str(site.get("crs") or "EPSG:31254")
    to_site = Transformer.from_crs("EPSG:4326", site_crs, always_xy=True)

    files = list(args.files)
    if not files:
        raw = ROOT / "data" / "raw"
        files = sorted(raw.glob("osm_roads_tirol-fernpass-*.json"))
        files = [p for p in files if not p.name.endswith(".bak")]
    if not files:
        raise SystemExit("No input files")

    for src in files:
        if not src.is_file():
            print(f"skip missing: {src}")
            continue
        convert_file(src, site_crs=site_crs, to_site=to_site)


if __name__ == "__main__":
    main()
