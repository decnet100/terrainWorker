"""Match GIP ``BRUNNEL`` structures to the Tirol road lines and propose bridge flags.

The GIP reference export (``gip_reference_ogd.gpkg``, layer ``BRUNNEL``) lists
every bridge, tunnel, gallery, under- and overpass in Austria as a line piece
with name, level and type. Our road data are the Tirol ``Verkehrswege`` lines
with their own ``OBJECTID``; the two have no shared key. This tool joins them
by geometry: a structure belongs to every road line that runs along it (inside
``MATCH_M``) for most of the structure's length.

Per match it reports where on the road the structure sits (station from/to on
the road line), how much of the road it covers, and, from the raw 0.5 m DGM
along the structure, how far the ground drops below the chord between the two
ends (``dip_m``). A drop is an opening (bridge); no drop with a short piece is a
culvert or a slab the road simply runs over. Existing entries of
``gip_bridge_flags.json`` are compared, so the output says which structures we
already treat as bridges, which we dropped, and which are new.

Nothing is written into ``gip_bridge_flags.json``. The result is a proposal:
``data/processed/<site>/gip_brunnel_proposal.json`` and a printed table.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\gip_brunnel_match.py --reference "C:\\Users\\chdem\\Downloads\\C_gip_reference_ogd\\gip_reference_ogd.gpkg"

The path can also live in the site YAML under ``sources.gip.reference_gpkg``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyogrio
import shapely

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_bridges import find_gip_geojson  # noqa: E402
from build_road_grid import RES_M, _load_geotiff  # noqa: E402
from gip_bridge_flags import load_flags  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

LAYER = "BRUNNEL"
# A road line counts as carrying the structure when it stays this close.
MATCH_M = 3.0
# ... for at least this share of the structure's length.
MATCH_SHARE = 0.5
# Sampling step along the structure for the ground dip.
SAMPLE_M = 0.5
# Below this dip the ground does not open under the piece.
DIP_OPEN_M = 1.0
# Shorter than this and without a dip: a culvert, the road runs over it.
CULVERT_MAX_M = 10.0

# Road classes that get a mesh; the rest are terrain paths.
_MESH_OBJEKT = frozenset({"S-A", "S-AB", "S-B", "S-BB", "S-L", "S-LB", "S-G", "S-GB", "S-AR", "S-AP", "S-BR", "S-BP"})

_TYPE_DE = {
    "Brücke": "bridge",
    "Bruecke": "bridge",
    "Unterfuehrung": "underpass",
    "Ueberfuehrung": "overpass",
    "Tunnel": "tunnel",
    "Galerie": "gallery",
}


def _reference_path(site: dict, arg: str | None) -> Path:
    if arg:
        return Path(arg)
    conf = ((site.get("sources") or {}).get("gip") or {}).get("reference_gpkg")
    if conf:
        return Path(conf) if Path(conf).is_absolute() else ROOT / conf
    raise SystemExit("reference gpkg: pass --reference or set sources.gip.reference_gpkg in the site YAML")


def _kind(value) -> str:
    text = str(value or "")
    for key, kind in _TYPE_DE.items():
        if key.lower() in text.lower():
            return kind
    return text or "unknown"


def _lines(geom) -> list:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "MultiLineString":
        merged = shapely.line_merge(geom)
        return [merged] if merged.geom_type == "LineString" else list(merged.geoms)
    return []


def _dip(line, z: np.ndarray, spec: dict) -> tuple[float, int]:
    """Largest drop of the raw ground below the chord between the ends."""
    n = max(3, int(np.ceil(line.length / SAMPLE_M)) + 1)
    s = np.linspace(0.0, line.length, n)
    pts = shapely.line_interpolate_point(line, s)
    x = shapely.get_x(pts)
    y = shapely.get_y(pts)
    col = np.floor((x - spec["xmin"]) / RES_M).astype(int)
    row = np.floor((spec["ymax"] - y) / RES_M).astype(int)
    ok = (row >= 0) & (col >= 0) & (row < z.shape[0]) & (col < z.shape[1])
    zz = np.full(n, np.nan)
    zz[ok] = z[row[ok], col[ok]]
    fin = np.isfinite(zz)
    if int(fin.sum()) < 3:
        return float("nan"), int(fin.sum())
    i0, i1 = np.flatnonzero(fin)[[0, -1]]
    chord = zz[i0] + (s - s[i0]) / max(s[i1] - s[i0], 1e-6) * (zz[i1] - zz[i0])
    return float(np.nanmax(chord - zz)), int(fin.sum())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--reference", help="gip_reference_ogd.gpkg")
    ap.add_argument("--no-dgm", action="store_true", help="skip the ground dip (faster)")
    args = ap.parse_args()

    site = load_site()
    proc = processed_dir(site)
    ref = _reference_path(site, args.reference)
    if not ref.is_file():
        raise SystemExit(f"missing {ref}")

    roads = gpd.read_file(find_gip_geojson(site))
    if roads.crs is None:
        roads = roads.set_crs("EPSG:31254")
    crs = roads.crs
    bbox = tuple(roads.to_crs(4326).total_bounds)
    struct = pyogrio.read_dataframe(ref, layer=LAYER, bbox=bbox).to_crs(crs)
    print(f"{LAYER}: {len(struct)} structures in the site box, {len(roads)} road lines", flush=True)

    flags = load_flags(site) or {}
    z = spec = None
    if not args.no_dgm:
        raw = proc / "corridor50_raw" / "corridor50_raw.tif"
        if raw.is_file():
            z, spec = _load_geotiff(raw)
        else:
            print(f"no {raw.name}, dip not measured", flush=True)

    tree = shapely.STRtree(list(roads.geometry))
    rows = []
    for rec in struct.itertuples(index=False):
        pieces = _lines(rec.geometry)
        if not pieces:
            continue
        length = float(sum(p.length for p in pieces))
        buf = shapely.union_all([p.buffer(MATCH_M, cap_style="flat") for p in pieces])
        matches = []
        for j in tree.query(buf):
            road_line = roads.geometry.iloc[j]
            inside = road_line.intersection(buf)
            if inside.is_empty or inside.length < MATCH_SHARE * min(length, road_line.length):
                continue
            # Station range of the structure on this road line. A road that
            # merely crosses the structure projects both ends onto almost the
            # same station and is not carrying it.
            ends = [shapely.Point(c) for p in pieces for c in (p.coords[0], p.coords[-1])]
            st = sorted(float(road_line.project(e)) for e in ends)
            if st[-1] - st[0] < MATCH_SHARE * min(length, road_line.length):
                continue
            oid = int(roads.OBJECTID.iloc[j])
            flag = flags.get(str(oid))
            matches.append(
                {
                    "objectid": oid,
                    "objekt": str(roads.OBJEKT.iloc[j] or ""),
                    "str_code": roads.STR_CODE.iloc[j] if isinstance(roads.STR_CODE.iloc[j], str) else None,
                    "road_length_m": round(float(road_line.length), 1),
                    "station_from_m": round(st[0], 1),
                    "station_to_m": round(st[-1], 1),
                    "covered_share": round(inside.length / max(road_line.length, 1e-6), 2),
                    "flag": None if flag is None else ("bridge" if flag.get("bridge") else f"not_bridge:{flag.get('reason')}"),
                }
            )
        dip, n_ok = (float("nan"), 0)
        if z is not None:
            dips = [_dip(p, z, spec) for p in pieces]
            dip = max(d for d, _ in dips)
            n_ok = sum(k for _, k in dips)
        kind = _kind(getattr(rec, "reference_type_long", None))
        if kind == "bridge":
            if np.isfinite(dip) and dip >= DIP_OPEN_M:
                suggest = "bridge"
            elif length <= CULVERT_MAX_M:
                suggest = "culvert"
            else:
                suggest = "check"
        else:
            suggest = kind
        rows.append(
            {
                "brunnel_id": int(rec.short_id),
                "name": rec.name if isinstance(rec.name, str) else None,
                "kind": kind,
                "level": int(rec.level) if rec.level is not None else None,
                "length_m": round(length, 1),
                "dip_m": None if not np.isfinite(dip) else round(dip, 2),
                "dgm_samples": n_ok,
                "suggest": suggest,
                "roads": sorted(matches, key=lambda m: -m["covered_share"]),
                "x": round(float(buf.centroid.x), 1),
                "y": round(float(buf.centroid.y), 1),
            }
        )

    rows.sort(key=lambda r: -r["length_m"])
    matched_oids = {m["objectid"] for r in rows for m in r["roads"]}
    flagged = {int(k) for k, v in flags.items() if v.get("bridge")}
    out = {
        "reference": str(ref),
        "layer": LAYER,
        "match_m": MATCH_M,
        "match_share": MATCH_SHARE,
        "dip_open_m": DIP_OPEN_M,
        "culvert_max_m": CULVERT_MAX_M,
        "structures": len(rows),
        "unmatched": sum(1 for r in rows if not r["roads"]),
        "flagged_now": sorted(flagged),
        "flagged_not_in_brunnel": sorted(flagged - matched_oids),
        # Only classes that get a road mesh; paths (S-FRW, S-F, S-GW) are terrain.
        "new_candidates": sorted(
            {
                m["objectid"]
                for r in rows
                for m in r["roads"]
                if r["suggest"] == "bridge" and m["flag"] != "bridge" and m["objekt"] in _MESH_OBJEKT
            }
        ),
        "new_candidates_paths": sorted(
            {
                m["objectid"]
                for r in rows
                for m in r["roads"]
                if r["suggest"] == "bridge" and m["flag"] != "bridge" and m["objekt"] not in _MESH_OBJEKT
            }
        ),
        "flagged_but_culvert": sorted(
            {m["objectid"] for r in rows for m in r["roads"] if r["suggest"] == "culvert" and m["flag"] == "bridge"}
        ),
        "rows": rows,
    }
    path = proc / "gip_brunnel_proposal.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"{'id':>6} {'kind':8} {'len':>6} {'dip':>5} {'suggest':8} roads (oid objekt station flag)")
    for r in rows:
        roads_txt = "; ".join(
            f"{m['objectid']} {m['objekt']} {m['station_from_m']:.0f}-{m['station_to_m']:.0f}/{m['road_length_m']:.0f}"
            + (f" [{m['flag']}]" if m["flag"] else "")
            for m in r["roads"]
        ) or "-"
        dip_txt = "  -  " if r["dip_m"] is None else f"{r['dip_m']:5.2f}"
        name = f" {r['name']}" if r["name"] else ""
        print(f"{r['brunnel_id']:>6} {r['kind']:8} {r['length_m']:6.1f} {dip_txt} {r['suggest']:8} {roads_txt}{name}")
    print(
        f"\nstructures {out['structures']}, unmatched {out['unmatched']}, flagged now {len(flagged)}, "
        f"flagged but not in {LAYER}: {out['flagged_not_in_brunnel']}, "
        f"flagged but no opening: {out['flagged_but_culvert']}"
    )
    print(f"new bridge candidates on mesh roads {len(out['new_candidates'])}: {out['new_candidates']}")
    print(f"new bridge candidates on paths {len(out['new_candidates_paths'])}: {out['new_candidates_paths']}")
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
