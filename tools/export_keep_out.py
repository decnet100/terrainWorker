"""What marks a road's edge, for the Magliasina edge steps: no ground is raised to the road there.

Magliasina's paved_edges / unpaved steps (run by its roads_only.py) raise the ground to the road edges, except
within 2 m of guard rails, fences, walls, buildings, the railway and water. This writes those things from the
site's data into data/processed/<site>/magliasina/keep_out/<kind>.geojson (site CRS), where prepare_external.py
picks them up:

* buildings: tiris_buildings.geojson (fetch_tiris_buildings.py) and, in Switzerland, the AV land cover class
  Gebaeude (sources.av_landcover, or the geodienste.ch WFS when that layer has no buildings);
* water: waterways.geojson (fetch_waterways.py) and the AV classes Gewaesser_*;
* guardrails: data/annotations/*.gpkg / guardrail.geojson, layer or file `guardrail`, when it holds lines;
* walls, fences, railway: none yet for Tirol (no source in the pipeline).

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\export_keep_out.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from site_coords import load_site, processed_dir  # noqa: E402

GEODIENSTE_LCSF = "https://geodienste.ch/db/av_0/deu"


def _read(path: Path, layer: str | None = None) -> gpd.GeoDataFrame | None:
    try:
        g = gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)
    except Exception:
        return None
    return g if len(g) else None


def _swiss_av(site: dict, box_crs) -> gpd.GeoDataFrame | None:
    src = (site.get("sources") or {}).get("av_landcover") or {}
    if not src.get("path"):
        return None
    g = _read(ROOT / src["path"], src.get("layer"))
    if g is None:
        return None
    attr = src.get("attribute", "art")
    g = g.rename(columns={attr: "art"})
    if g.crs is None:
        g = g.set_crs(src.get("crs") or "EPSG:2056")
    if not (g["art"] == "Gebaeude").any():
        # the cadastral layer of the site may come without buildings: ask the WFS for them
        from urllib.parse import urlencode
        b = gpd.GeoSeries([box_crs], crs=site.get("crs")).to_crs(2056).total_bounds
        q = {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": "ms:LCSF",
             "BBOX": f"{b[0]},{b[1]},{b[2]},{b[3]},urn:ogc:def:crs:EPSG::2056", "OUTPUTFORMAT": "geojson",
             "SRSNAME": "urn:ogc:def:crs:EPSG::2056"}
        try:
            extra = gpd.read_file(f"{GEODIENSTE_LCSF}?{urlencode(q)}").set_crs(2056, allow_override=True)
            extra = extra.rename(columns={"Art": "art"})
            g = pd.concat([g, extra[extra["art"] == "Gebaeude"]], ignore_index=True)
            print(f"Swiss buildings from the WFS: {int((extra['art'] == 'Gebaeude').sum())}", flush=True)
        except Exception as e:                       # offline: the Swiss side has no buildings then
            print(f"Swiss buildings not fetched: {e}", flush=True)
    return g


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    crs = site.get("crs") or "EPSG:25832"
    box = shapely.box(*map(float, site["bbox"])).buffer(200.0)
    out = proc / "magliasina" / "keep_out"
    out.mkdir(parents=True, exist_ok=True)
    kinds: dict[str, list] = {"buildings": [], "water": [], "guardrails": []}
    b = _read(proc / "tiris_buildings.geojson")
    if b is not None:
        kinds["buildings"].append(b.to_crs(crs))
    w = _read(proc / "waterways.geojson")
    if w is not None:
        kinds["water"].append(w.to_crs(crs))
    av = _swiss_av(site, box)
    if av is not None:
        kinds["buildings"].append(av[av["art"] == "Gebaeude"].to_crs(crs))
        kinds["water"].append(av[av["art"].astype(str).str.startswith("Gewaesser")].to_crs(crs))
    for f in sorted((ROOT / "data" / "annotations").glob("*")):
        g = None
        if f.suffix == ".gpkg":
            g = _read(f, "guardrail")
        elif f.name == "guardrail.geojson":
            g = _read(f)
        if g is not None:
            kinds["guardrails"].append(g.to_crs(crs))
    report = {}
    for kind, parts in kinds.items():
        dest = out / f"{kind}.geojson"
        parts = [p[["geometry"]] for p in parts if p is not None and len(p)]
        if not parts:
            dest.unlink(missing_ok=True)
            report[kind] = 0
            continue
        g = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=crs)
        g = g[g.intersects(box)]
        g.to_file(dest, driver="GeoJSON")
        report[kind] = len(g)
    (out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"keep-out features {report}; walls, fences, railway: no source yet")
    print(f"(open the following in qgis or similar tool to check: {out})")


if __name__ == "__main__":
    main()
