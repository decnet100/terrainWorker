"""Split the corrected links into pieces at each structure.

Reads data/processed/<site>/lateral_fix/lateral_fix.gpkg (link + station from
fix_lateral_rank.py). A bridge, tunnel or gallery becomes its own line. The cut
is the midpoint between the last station of one type and the first of the next,
and both pieces use that exact point. Road on the ground is Keine. A link with
no stations is one Keine piece.

kunstbaute is the swissTLM3D name Magliasina reads. Tunnel and Galerie are left
out of its road mesh (network.SKIP); we build those. Tunnel/Galerie, the span
the terrain could not name, is written as Tunnel so that mesh skips it too.
The original name stays in typ. Height is not written: Magliasina builds a
bridge deck between the ramps and does not use an axis height.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\export_kunstbaute.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point
from shapely.ops import substring

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from site_coords import load_site, processed_dir  # noqa: E402

# Magliasina network.SKIP drops Tunnel and Galerie. Bruecke keeps a deck.
KUNSTBAUTE = {
    "": "Keine",
    "Brücke": "Bruecke",
    "Tunnel": "Tunnel",
    "Galerie": "Galerie",
    "Tunnel/Galerie": "Tunnel",
}


def _piece(line: LineString, d0: float, d1: float) -> LineString | None:
    if d1 - d0 < 0.05:
        return None
    seg = substring(line, d0, d1, normalized=False)
    p0 = line.interpolate(d0)
    p1 = line.interpolate(d1)
    coords = list(getattr(seg, "coords", []))
    if len(coords) < 2:
        coords = [(p0.x, p0.y), (p1.x, p1.y)]
    else:
        coords[0] = (p0.x, p0.y)
        coords[-1] = (p1.x, p1.y)
    out = LineString(coords)
    return out if out.length >= 0.05 else None


def _split_link(line: LineString, stations: pd.DataFrame) -> list[dict]:
    """Pieces along one link. stations carry structure_typ, structure_src and a point geometry."""
    if stations.empty or line is None or line.is_empty or line.length < 0.05:
        return []
    dist = np.array([line.project(g) for g in stations.geometry], dtype=float)
    order = np.argsort(dist, kind="mergesort")
    dist = dist[order]
    typ = stations.structure_typ.fillna("").astype(str).to_numpy()[order]
    src = stations.structure_src.fillna("").astype(str).to_numpy()[order]
    runs: list[tuple[int, int]] = []
    a = 0
    for i in range(1, len(typ) + 1):
        if i == len(typ) or typ[i] != typ[a]:
            runs.append((a, i - 1))
            a = i
    bounds = [0.0]
    for (a0, b0), (a1, _b1) in zip(runs, runs[1:]):
        bounds.append(0.5 * (dist[b0] + dist[a1]))
    bounds.append(float(line.length))
    pieces = []
    for (a0, b0), d0, d1 in zip(runs, bounds, bounds[1:]):
        geom = _piece(line, d0, d1)
        if geom is None:
            continue
        name = typ[a0]
        if name not in KUNSTBAUTE:
            raise SystemExit(f"unknown structure_typ {name!r}")
        sources = sorted({s for s in src[a0:b0 + 1] if s})
        pieces.append({
            "kunstbaute": KUNSTBAUTE[name],
            "typ": name,
            "src": ",".join(sources),
            "n_station": int(b0 - a0 + 1),
            "geometry": geom,
        })
    return pieces


def main() -> None:
    site = load_site()
    src_path = processed_dir(site) / "lateral_fix" / "lateral_fix.gpkg"
    if not src_path.is_file():
        raise SystemExit(f"missing {src_path}")
    links = gpd.read_file(src_path, layer="link")
    stations = gpd.read_file(src_path, layer="station")
    by_link = {lid: g for lid, g in stations.groupby("link_id")}
    rows = []
    for rec in links.itertuples():
        line = rec.geometry
        if line is None or line.is_empty:
            continue
        if line.geom_type != "LineString":
            raise SystemExit(f"{rec.link_id} geometry is {line.geom_type}")
        parts = _split_link(line, by_link.get(rec.link_id, stations.iloc[0:0]))
        if not parts:
            if line.length < 0.05:
                continue
            parts = [{"kunstbaute": "Keine", "typ": "", "src": "", "n_station": 0, "geometry": line}]
        for i, part in enumerate(parts):
            part.update({
                "piece_id": f"{rec.link_id}#{i}",
                "link_id": rec.link_id,
                "i": i,
                "rank": None if pd.isna(rec.rank) else rec.rank,
                "fc": rec.fc,
                "fc_name": rec.fc_name,
                "width_m": rec.width_m,
                "name": rec.name,
                "short_name": rec.short_name,
                "corrected": int(rec.corrected),
            })
            rows.append(part)
    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=links.crs)
    # neighbouring pieces of one link share the cut point
    bad = 0
    for _lid, g in out.groupby("link_id"):
        g = g.sort_values("i")
        coords = [(ln.coords[0], ln.coords[-1]) for ln in g.geometry]
        for (_a0, a1), (b0, _b1) in zip(coords, coords[1:]):
            if Point(a1).distance(Point(b0)) > 1e-6:
                bad += 1
    dest = src_path.with_name("kunstbaute.gpkg")
    out.to_file(dest, layer="piece")
    counts = out.groupby("kunstbaute").size().to_dict()
    struct = out[out.kunstbaute != "Keine"]
    print(f"pieces {len(out)} on {out.link_id.nunique()} links: {counts}")
    print(f"structures {len(struct)}, shared cuts that differ: {bad}")
    print(f"(open the following in qgis or similar tool to check: {dest})")


if __name__ == "__main__":
    main()
