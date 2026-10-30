"""Export the merged road network in the shape the Magliasina road pipeline reads (magliasinaNG, beamng/pipeline).

Reads border_merge/border_merge.gpkg (merge_border_network.py) and lateral_fix/lateral_fix.gpkg (station widths).
Writes data/processed/<site>/magliasina/:

* tlm/TLM_STRASSE.json - Magliasina's download_tlm format {"fields", "features": [{"props", "parts"}]}, in the site
  CRS (not LV95: the Magliasina side needs its adapter for the CRS anyway). Own pieces get a swissTLM3D OBJEKTART
  from their measured width (median of the stations, else the GIP width): >= 9 m 10m Strasse, >= 7 m 8m Strasse,
  >= 5 m 6m Strasse, >= 3.8 m 4m Strasse, else 3m Strasse. KUNSTBAUTE from export_kunstbaute (Keine, Bruecke,
  Tunnel, Galerie), and a ground piece GIP puts on level +1 (an overpass the terrain check could not see) becomes
  Bruecke; level -1 stays, it can be the road under a bridge; UUID is the piece id; BELAGSART Hart; RICHTUNGSG / KREISEL from the GIP form of way;
  Z is NaN (Magliasina builds decks between the ramps). swissTLM3D lines keep their attributes and 3D.
  Own ways (functional class 10 and above) are left out: they are not laterally corrected yet. Connectors of
  `unite` rows are left out with them.
* av/LCSF.geojson - carriageway polygons with "Genere": strada_sentiero is the union of the own ribbons, the TLM
  branch strips and the AV Strasse_Weg, less the AV marciapiede and spartitraffico, which stay their own class.
  One union means no two road polygons overlap at a junction; Magliasina hands a polygon to every line inside it.
  av/SOSF, SOLI, SOPT.geojson are empty (no survey bridges or walls).
* dati/ponti.json - {"ponti": []}; Magliasina writes its own list on the first build.
* manifest.json - CRS, bbox, a local origin suggestion, the DGM path, counts.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\export_magliasina.py
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
from pyproj import Proj, Transformer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from gip_routing_load import gpkg_path  # noqa: E402
from site_coords import load_site, processed_dir, site_slug  # noqa: E402

FIELDS = ["UUID", "OBJEKTART", "KUNSTBAUTE", "BELAGSART", "STUFE", "RICHTUNGSG", "KREISEL", "BEFAHRBARK",
          "STRNAME", "NAME", "SRC", "LINK_ID"]
WIDTH_CLASS = [(9.0, "10m Strasse"), (7.0, "8m Strasse"), (5.0, "6m Strasse"), (3.8, "4m Strasse"), (0.0, "3m Strasse")]
WAY_FC = 10


def objektart(width: float) -> str:
    for lim, name in WIDTH_CLASS:
        if width >= lim:
            return name
    return "3m Strasse"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.parse_args()
    site = load_site()
    proc = processed_dir(site)
    crs = site.get("crs") or "EPSG:25832"
    merged = proc / "border_merge" / "border_merge.gpkg"
    lines = gpd.read_file(merged, layer="line")
    carr = gpd.read_file(merged, layer="carriageway")
    st = gpd.read_file(proc / "lateral_fix" / "lateral_fix.gpkg", layer="station", ignore_geometry=True)
    pieces = gpd.read_file(proc / "lateral_fix" / "kunstbaute.gpkg", ignore_geometry=True).set_index("piece_id")
    glinks = gpd.read_file(gpkg_path(site), layer="link", ignore_geometry=True).set_index("link_id")
    width_link = st[st["structure"] == 0].groupby("link_id")["width_m"].median()

    feats, skipped, raised = [], {"own_way": 0, "connector": 0}, [0]
    for _, r in lines.iterrows():
        g = r.geometry
        if g is None or g.is_empty:
            continue
        if r["src"] == "connector":
            skipped["connector"] += 1
            continue
        if r["src"] == "own":
            pc = pieces.loc[r["id"]] if r["id"] in pieces.index else None
            fc = int(pc["fc"]) if pc is not None and pc["fc"] == pc["fc"] else 99
            if fc >= WAY_FC and fc != 99:
                skipped["own_way"] += 1
                continue
            lk = glinks.loc[r["link_id"]] if r["link_id"] in glinks.index else None
            w = width_link.get(r["link_id"], np.nan)
            if not np.isfinite(w):
                w = float(r["width_m"]) if r["width_m"] == r["width_m"] and r["width_m"] else 5.0
            fow = int(lk["fow"]) if lk is not None and lk["fow"] == lk["fow"] else 3
            kb = r["kunstbaute"] or "Keine"
            level = int(lk["level_i"]) if lk is not None and lk["level_i"] == lk["level_i"] else 0
            if kb == "Keine" and level > 0:
                kb = "Bruecke"                  # GIP puts it above ground; the terrain check had nothing to go on
                raised[0] += 1
            props = {"UUID": r["id"], "OBJEKTART": objektart(float(w)), "KUNSTBAUTE": kb,
                     "BELAGSART": "Hart", "STUFE": int(lk["level_i"]) if lk is not None and lk["level_i"] == lk["level_i"] else 0,
                     "RICHTUNGSG": "Wahr" if fow == 2 else "Falsch", "KREISEL": "Wahr" if fow == 4 else "Falsch",
                     "BEFAHRBARK": "Wahr", "STRNAME": (lk["name"] if lk is not None and isinstance(lk["name"], str) else ""),
                     "NAME": "", "SRC": "gip", "LINK_ID": r["link_id"]}
            coords = [[float(x), float(y), math.nan] for x, y, *_ in g.coords]
        else:
            props = {"UUID": r["id"], "OBJEKTART": r["objektart"], "KUNSTBAUTE": r["kunstbaute"] or "Keine",
                     # swissTLM3D's own surface; a line without one counts as hard when it is a Strasse
                     "BELAGSART": r.get("belagsart") or ("Hart" if r["objektart"] and "Strasse" in r["objektart"] else "Natur"),
                     "STUFE": 0,
                     "RICHTUNGSG": "Falsch", "KREISEL": "Falsch", "BEFAHRBARK": "Wahr", "STRNAME": "", "NAME": "",
                     "SRC": "tlm", "LINK_ID": ""}
            coords = [[float(c[0]), float(c[1]), float(c[2]) if len(c) > 2 else math.nan] for c in g.coords]
        feats.append({"props": props, "parts": [coords]})

    # carriageway polygons
    road_src = carr[carr["klass"] == "strada_sentiero"]
    other = carr[carr["klass"] != "strada_sentiero"]
    other_u = shapely.union_all(list(other.geometry)) if len(other) else shapely.Polygon()
    road = shapely.union_all(list(road_src.geometry)).difference(other_u)
    road = shapely.make_valid(road)
    polys = []
    for part in getattr(road, "geoms", [road]):
        if part.geom_type == "Polygon" and part.area > 0.5:
            polys.append({"Genere": "strada_sentiero", "geometry": part})
    for _, r in other.iterrows():
        for part in getattr(r.geometry, "geoms", [r.geometry]):
            if part.geom_type == "Polygon" and part.area > 0.5:
                polys.append({"Genere": r["klass"], "geometry": part})

    out = proc / "magliasina"
    for sub in ("tlm", "av", "dati"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    (out / "tlm" / "TLM_STRASSE.json").write_text(json.dumps({"fields": FIELDS, "features": feats}), encoding="utf-8")
    gpd.GeoDataFrame(polys, crs=crs).to_file(out / "av" / "LCSF.geojson", driver="GeoJSON")
    for name in ("SOSF", "SOLI", "SOPT"):
        (out / "av" / f"{name}.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")
    (out / "dati" / "ponti.json").write_text(json.dumps({"ponti": []}), encoding="utf-8")
    bbox = [float(v) for v in site["bbox"]]
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    lon, lat = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform(cx, cy)
    scale = float(Proj(crs).get_factors(lon, lat).meridional_scale)
    manifest = {"site": site_slug(site), "crs": crs, "bbox": bbox, "origin": [round(cx), round(cy)],
                "scale_factor_at_origin": scale, "height_system": "swissALTI3D LN02; Tirol lifted onto it (build_mixed_dgm.py)",
                "dgm_05": str(ROOT / "data" / "raw" / f"dgm_{site_slug(site)}_mixed05.tif"),
                "dgm_1": str(ROOT / "data" / "raw" / f"dgm_{site_slug(site)}_mixed.tif"),
                "lines": len(feats), "lines_skipped": skipped, "gip_level_1_as_bridge": raised[0],
                "lines_by_class": dict(Counter(f["props"]["OBJEKTART"] for f in feats)),
                "polygons": dict(Counter(p["Genere"] for p in polys))}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k2: manifest[k2] for k2 in ("lines", "lines_skipped", "gip_level_1_as_bridge", "lines_by_class", "polygons")}, ensure_ascii=False))
    print(f"(open the following in qgis or similar tool to check: {out})")


if __name__ == "__main__":
    main()
