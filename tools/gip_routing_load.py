"""Cut the GIP routing IDF export to the site and join it onto Verkehrswege.

The Tirol WFS lines have no node, level or turn fields. The national routing
export (dataset A, ``routingexport_ogd.txt``) is the official node-link graph:
shared node = at-grade junction, crossing without a node plus different
``LEVEL_INTERMEDIATE`` = grade separation. IDs do not match Tirol ``OBJECTID``;
the join is by geometry.

Writes ``data/processed/<site>/gip_routing/gip_routing.gpkg`` (QGIS) and
``gip_routing.json`` (topology for later tools). Import::

    from gip_routing_load import load_routing, roads_share_node, roads_share_link, roads_grade_cross

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\gip_routing_load.py

Path: ``--idf``, ``sources.gip.routing_idf``, or
``data/raw/gip_routingexport/routingexport_ogd.txt``.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from pyproj import Transformer
from shapely.geometry import LineString, Point
from shapely.ops import substring

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from authorities import (  # noqa: E402
    apply_working_frame,
    gip_authority_key,
    load_authorities,
    transformer_into_working,
)
from build_bridges import find_gip_geojson  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

DEFAULT_IDF = ROOT / "data" / "raw" / "gip_routingexport" / "routingexport_ogd.txt"
MARGIN_M = 300.0
MATCH_M = 3.0
MATCH_SHARE = 0.5
CHUNK = 64 << 20

# GIP Standard 2.3.4 look-up tables, values that actually appear on roads.
FRC = {
    -1: "nicht anwendbar",
    0: "transnational",
    1: "transregional",
    2: "zentralörtlich",
    3: "regional",
    4: "Gemeindeverbindung",
    5: "innerörtlich",
    6: "Sammelstraße",
    7: "interne Erschließung",
    8: "sonstige Straße",
    10: "Rad-/Fußweg",
    11: "Wirtschaftsweg",
    12: "sonstiger Weg",
    45: "Treppe",
    98: "Betriebsumkehr",
    99: "Betriebsweg",
    103: "Seilbahn",
    105: "Almaufschließung",
    106: "Forstaufschließung",
    200: "Singletrail",
    201: "Shared Trail",
    300: "Wanderweg",
}
FOW = {
    -1: "nicht anwendbar",
    1: "Autobahn/Schnellstraße",
    2: "Fahrbahnteilung",
    3: "ungeteilte Fahrbahn",
    4: "Kreisverkehr",
    6: "Parkplatz",
    10: "Rampe",
    12: "Parkplatz Zu-/Ausfahrt",
    15: "Fußweg",
    18: "Stiege",
    200: "Singletrail",
    301: "Betriebsumkehr",
    504: "Geh- und Radweg",
    516: "Schlepplift",
    519: "Sessellift",
    520: "Babylift",
}
BASE_TYPE = {
    1: "Fahrbahn",
    2: "Radweg",
    6: "Stiege",
    7: "Gehweg",
    13: "Aufstiegshilfe",
    36: "Geh- und Radweg",
}
_STRUCT_KEYS = ("brücke", "bruecke", "tunnel", "galerie", "unterfuehrung", "ueberfuehrung")


def out_dir(site: dict | None = None) -> Path:
    d = processed_dir(site) / "gip_routing"
    d.mkdir(parents=True, exist_ok=True)
    return d


def json_path(site: dict | None = None) -> Path:
    return out_dir(site) / "gip_routing.json"


def gpkg_path(site: dict | None = None) -> Path:
    return out_dir(site) / "gip_routing.gpkg"


def idf_path(site: dict | None = None, arg: str | None = None) -> Path:
    if arg:
        p = Path(arg)
        return p if p.is_absolute() else ROOT / p
    site = site or load_site()
    conf = ((site.get("sources") or {}).get("gip") or {}).get("routing_idf")
    if conf:
        p = Path(str(conf))
        return p if p.is_absolute() else ROOT / p
    if DEFAULT_IDF.is_file():
        return DEFAULT_IDF
    raise SystemExit(
        "routing IDF missing: pass --idf, set sources.gip.routing_idf, "
        f"or place the file at {DEFAULT_IDF}"
    )


def load_routing(site: dict | None = None) -> dict:
    path = json_path(site)
    if not path.is_file():
        raise SystemExit(f"missing {path} — run: python tools\\gip_routing_load.py")
    return json.loads(path.read_text(encoding="utf-8"))


def roads_share_node(topo: dict, oid_a: int, oid_b: int) -> list[str]:
    """Routing node ids that both Verkehrswege pieces sit on."""
    roads = topo.get("roads") or {}
    a = set((roads.get(str(oid_a)) or {}).get("nodes") or [])
    b = set((roads.get(str(oid_b)) or {}).get("nodes") or [])
    return sorted(a & b)


def _primary_hit(info: dict) -> dict | None:
    """The one routing link this WFS piece belongs to.

    Join records may list several overlapping centreline links. With the
    routing graph present that list is not a merge key: the piece sits on
    the link with the longest overlap (on a tie, the lower functional class).
    """
    hits = [
        h
        for h in (info.get("links") or [])
        if float(h.get("share") or 0.0) >= MATCH_SHARE
    ]
    if not hits:
        return None
    return max(
        hits,
        key=lambda h: (
            float(h.get("overlap_m") or 0.0),
            -float(h.get("fc") if h.get("fc") is not None else 99.0),
            float(h.get("share") or 0.0),
        ),
    )


def _hit_ids(info: dict) -> set[str]:
    hit = _primary_hit(info)
    return {str(hit["id"])} if hit else set()


def roads_share_link(topo: dict, oid_a: int, oid_b: int) -> list[str]:
    """Routing links that both Verkehrswege pieces run along (same carriageway)."""
    roads = topo.get("roads") or {}

    def ids(oid: int) -> set[str]:
        return _hit_ids(roads.get(str(oid)) or {})

    return sorted(ids(oid_a) & ids(oid_b))


def roads_grade_cross(topo: dict, oid_a: int, oid_b: int) -> dict | None:
    """Crossing without a shared node, if the two pieces meet that way."""
    ia, ib = int(oid_a), int(oid_b)
    for row in topo.get("crossings") or []:
        a = set(row.get("roads_a") or [])
        b = set(row.get("roads_b") or [])
        if (ia in a and ib in b) or (ia in b and ib in a):
            return row
    return None


def try_load_routing(site: dict | None = None) -> dict | None:
    path = json_path(site)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# Gap between an overpass polygon and the road that goes under it, in metres.
# One 0.5 m mesh cell, so the two surfaces do not share an outline.
GRADE_GAP_M = 0.5
# Centreline pieces of one routing link are joined when their ends are this close.
AXIS_JOIN_M = 5.0


def _find(parent: dict[int, int], a: int) -> int:
    parent.setdefault(a, a)
    while parent[a] != a:
        parent[a] = parent[parent[a]]
        a = parent[a]
    return a


def same_link_components(topo: dict) -> dict[int, int]:
    """OBJECTID → representative (smallest id) of the shared-routing-link component."""
    parent: dict[int, int] = {}
    by_link: dict[str, list[int]] = defaultdict(list)
    for info in (topo.get("roads") or {}).values():
        try:
            oid = int(info["objectid"])
        except (KeyError, TypeError, ValueError):
            continue
        parent.setdefault(oid, oid)
        hit = _primary_hit(info)
        if hit is not None:
            by_link[str(hit["id"])].append(oid)
    for oids in by_link.values():
        uniq = sorted(set(oids))
        if len(uniq) < 2:
            continue
        root = _find(parent, uniq[0])
        for oid in uniq[1:]:
            other = _find(parent, oid)
            if other != root:
                if other < root:
                    parent[root] = other
                    root = other
                else:
                    parent[other] = root
    return {oid: _find(parent, oid) for oid in parent}


def grade_clip_pairs(topo: dict) -> list[tuple[set[int], set[int]]]:
    """(upper OBJECTIDs, lower OBJECTIDs) at a crossing with different levels."""
    roads = topo.get("roads") or {}
    pairs = []
    for row in topo.get("crossings") or []:
        la = float(row.get("level_a") or 0.0)
        lb = float(row.get("level_b") or 0.0)
        if la == lb:
            continue
        a_id = str(row.get("a") or "")
        b_id = str(row.get("b") or "")
        a: set[int] = set()
        b: set[int] = set()
        for info in roads.values():
            try:
                oid = int(info["objectid"])
            except (KeyError, TypeError, ValueError):
                continue
            ids = _hit_ids(info)
            on_a = a_id in ids
            on_b = b_id in ids
            if on_a and not on_b:
                a.add(oid)
            elif on_b and not on_a:
                b.add(oid)
        if not a or not b:
            continue
        if la > lb:
            pairs.append((a, b))
        else:
            pairs.append((b, a))
    return pairs


def merge_centerline_parts(geoms: list, join_m: float = AXIS_JOIN_M):
    """One LineString from pieces of the same routing link, or None."""
    parts = []
    for geom in geoms:
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type == "LineString" and geom.length >= 1.0:
            parts.append(geom)
        elif geom.geom_type == "MultiLineString":
            parts.extend(p for p in geom.geoms if p.geom_type == "LineString" and p.length >= 1.0)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    merged = shapely.line_merge(shapely.union_all(parts))
    if merged.geom_type == "LineString":
        return merged
    leftover = [p for p in merged.geoms if p.geom_type == "LineString" and p.length >= 1.0]
    leftover.sort(key=lambda g: -g.length)
    acc = leftover.pop(0)
    changed = True
    while leftover and changed:
        changed = False
        a0 = Point(acc.coords[0])
        a1 = Point(acc.coords[-1])
        best_i = None
        best = None
        for i, piece in enumerate(leftover):
            p0 = Point(piece.coords[0])
            p1 = Point(piece.coords[-1])
            cand = (
                (a1.distance(p0), "append", False),
                (a1.distance(p1), "append", True),
                (a0.distance(p1), "prepend", False),
                (a0.distance(p0), "prepend", True),
            )
            dist, how, rev = min(cand, key=lambda t: t[0])
            if dist <= join_m and (best is None or dist < best[0]):
                best = (dist, how, rev, i)
        if best is None:
            break
        _dist, how, rev, i = best
        piece = leftover.pop(i)
        coords = list(piece.coords)
        if rev:
            coords = list(reversed(coords))
        acc_c = list(acc.coords)
        if how == "append":
            if Point(acc_c[-1]).distance(Point(coords[0])) < 0.05:
                coords = coords[1:]
            acc = LineString(acc_c + coords)
        else:
            if Point(coords[-1]).distance(Point(acc_c[0])) < 0.05:
                acc_c = acc_c[1:]
            acc = LineString(coords + acc_c)
        changed = True
    return acc


# WFS codes for a bridge deck. Centerline shift leaves these out of the
# carriageway layer (all-structure groups); the mesh still needs the polygon.
_DECK_OBJEKT = frozenset({"S-AB", "S-BB", "S-LB", "S-GB"})


def _append_missing_deck_polygons(roads: gpd.GeoDataFrame, site: dict, topo: dict):
    """Put S-BB / S-AB / S-LB shift buffers back when the clip omitted them."""
    gpkg = processed_dir(site) / "centerline_shift_taper" / "centerline_shift.gpkg"
    if not gpkg.is_file() or "objectid" not in roads.columns:
        return roads, 0
    try:
        buf = gpd.read_file(gpkg, layer="chosen_buffer")
    except Exception:
        return roads, 0
    if buf.empty or "objectid" not in buf.columns:
        return roads, 0
    present: set[int] = set()
    for val in roads["objectid"]:
        try:
            present.add(int(val))
        except (TypeError, ValueError):
            continue
    info_of = topo.get("roads") or {}
    present_links: set[str] = set()
    for oid_p in present:
        hit = _primary_hit(info_of.get(str(oid_p)) or {})
        if hit is not None:
            present_links.add(str(hit["id"]))
    from gip_bridge_flags import load_flags

    flags = load_flags(site) or {}
    extra_oids: list[int] = []
    extra_geoms = []
    for oid, sub in buf.groupby("objectid"):
        try:
            oid_i = int(oid)
        except (TypeError, ValueError):
            continue
        if oid_i in present:
            continue
        info = info_of.get(str(oid_i)) or {}
        obj = str(info.get("objekt") or "").upper().strip()
        if obj not in _DECK_OBJEKT:
            continue
        if not (flags.get(str(oid_i)) or {}).get("bridge"):
            continue
        hit = _primary_hit(info)
        # A stub on its own 6 m link sits on the outline of an existing
        # clip and opens the 2.5D mesh. Only join when that link already
        # has a carriageway piece (the Eichenweg deck 4617 on the B189
        # link of 4616 / 4919).
        if hit is None or str(hit["id"]) not in present_links:
            continue
        polys = []
        for geom in sub.geometry:
            polys.extend(_iter_poly_geoms(geom))
        if not polys:
            continue
        extra_oids.append(oid_i)
        extra_geoms.append(shapely.make_valid(shapely.union_all(polys)))
        present.add(oid_i)
    if not extra_oids:
        return roads, 0
    extra = gpd.GeoDataFrame({"objectid": extra_oids}, geometry=extra_geoms, crs=roads.crs)
    out = gpd.GeoDataFrame(
        pd.concat([roads, extra], ignore_index=True),
        geometry="geometry",
        crs=roads.crs,
    )
    return out, len(extra_oids)


def apply_routing_carriageways(roads: gpd.GeoDataFrame, site: dict | None = None, gap_m: float = GRADE_GAP_M):
    """Dissolve WFS pieces on one routing link; tag grade-separated layers.

    Returns ``(roads, stats)``. Without a routing JSON the frame is unchanged.
    ``objectid`` becomes the smallest member of the component; ``members`` lists
    the original ids. Grouping follows the routing link, not overlapping WFS
    geometry: each piece is assigned to one link. A crossing without a shared
    node and with different ``LEVEL_INTERMEDIATE`` is not clipped in XY: the
    upper group is ``mesh_kind=overpass``, the lower group ``underpass``. Each
    of those is a separate 2.5D mesh. ``gap_m`` is unused (kept so callers do
    not break).
    """
    del gap_m
    topo = try_load_routing(site)
    empty = {
        "used": False,
        "groups": 0,
        "merged_oids": 0,
        "grade_clips": 0,
        "clip_area_m2": 0.0,
        "grade_overpass": 0,
        "grade_underpass": 0,
    }
    if topo is None or "objectid" not in roads.columns:
        return roads, empty

    roads, n_deck = _append_missing_deck_polygons(roads, site, topo)
    if n_deck:
        print(f"deck polygons from shift buffer: {n_deck}", flush=True)

    components = same_link_components(topo)
    member_of: dict[int, list[int]] = defaultdict(list)
    rows = []
    for rec in roads.itertuples(index=False):
        try:
            oid = int(rec.objectid)
        except (TypeError, ValueError):
            continue
        if rec.geometry is None or rec.geometry.is_empty:
            continue
        rows.append((oid, rec.geometry))
        member_of[components.get(oid, oid)].append(oid)

    by_oid: dict[int, list] = defaultdict(list)
    for oid, geom in rows:
        by_oid[oid].append(geom)

    group_geom: dict[int, object] = {}
    group_members: dict[int, list[int]] = {}
    for oid, _geom in rows:
        rep = components.get(oid, oid)
        if rep in group_geom:
            continue
        members = sorted(set(member_of.get(rep, [oid])) & set(by_oid))
        if not members:
            members = [oid]
        polys = []
        for m in members:
            for g in by_oid[m]:
                polys.extend(_iter_poly_geoms(g))
        if not polys:
            continue
        union = shapely.make_valid(shapely.union_all(polys))
        group_geom[rep] = union
        group_members[rep] = members

    overpass_reps: set[int] = set()
    underpass_reps: set[int] = set()
    adj: dict[int, set[int]] = defaultdict(set)
    for upper_oids, lower_oids in grade_clip_pairs(topo):
        upper_reps = {components.get(o, o) for o in upper_oids if components.get(o, o) in group_geom}
        lower_reps = {components.get(o, o) for o in lower_oids if components.get(o, o) in group_geom}
        upper_only = upper_reps - lower_reps
        if not upper_only or not lower_reps:
            continue
        overpass_reps |= upper_only
        underpass_reps |= lower_reps
        for a in upper_reps:
            for b in lower_reps:
                if a != b:
                    adj[a].add(b)
                    adj[b].add(a)
    both = overpass_reps & underpass_reps
    overpass_reps -= both
    underpass_reps -= both
    mesh_layer: dict[int, int] = {}
    for n in sorted(adj):
        used = {mesh_layer[m] for m in adj[n] if m in mesh_layer}
        layer = 0
        while layer in used:
            layer += 1
        mesh_layer[n] = layer

    out_rows = []
    for rep, geom in group_geom.items():
        if geom is None or geom.is_empty:
            continue
        members = group_members.get(rep, [rep])
        if rep in both:
            kind = "span"
            key = int(rep)
        elif rep in overpass_reps:
            kind = "overpass"
            key = int(rep)
        elif rep in underpass_reps:
            kind = "underpass"
            key = int(rep)
        else:
            kind = "road"
            key = 0
        out_rows.append(
            {
                "objectid": int(rep),
                "members": ",".join(str(x) for x in members),
                "n_members": len(members),
                "mesh_kind": kind,
                "mesh_key": key,
                "mesh_layer": int(mesh_layer.get(rep, 0)),
                "geometry": geom,
            }
        )
    if not out_rows:
        return roads, empty
    out = gpd.GeoDataFrame(out_rows, geometry="geometry", crs=roads.crs)
    merged = sum(1 for r in out_rows if r["n_members"] > 1)
    stats = {
        "used": True,
        "groups": len(out_rows),
        "merged_oids": int(sum(r["n_members"] for r in out_rows if r["n_members"] > 1)),
        "merged_groups": merged,
        "grade_clips": 0,
        "clip_area_m2": 0.0,
        "grade_overpass": int(sum(1 for r in out_rows if r["mesh_kind"] == "overpass")),
        "grade_underpass": int(sum(1 for r in out_rows if r["mesh_kind"] == "underpass")),
        "grade_span": int(sum(1 for r in out_rows if r["mesh_kind"] == "span")),
        "mesh_layers": int(max((r["mesh_layer"] for r in out_rows), default=0) + 1),
        "deck_polys_added": int(n_deck),
    }
    return out, stats


def _iter_poly_geoms(geom) -> list:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return [g for g in geom.geoms if g.geom_type == "Polygon"]
    if hasattr(geom, "geoms"):
        out = []
        for g in geom.geoms:
            out.extend(_iter_poly_geoms(g))
        return out
    return []


def _num(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default=None):
    n = _num(value, default)
    if n is None:
        return default
    return int(n)


def _text(value) -> str:
    return str(value or "").strip()


def _index_tables(path: Path) -> list[dict]:
    cache = path.with_suffix(path.suffix + ".tables.json")
    size = path.stat().st_size
    mtime = path.stat().st_mtime
    if cache.is_file():
        try:
            blob = json.loads(cache.read_text(encoding="utf-8"))
            if blob.get("idf_size") == size and abs(float(blob.get("idf_mtime") or 0) - mtime) < 1:
                return blob["tables"]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass
    print(f"indexing tables in {path.name} ({size / 1e9:.1f} GB) …", flush=True)
    tables: list[dict] = []
    t0 = time.time()
    with path.open("rb") as f:
        pos = 0
        carry = b""
        while True:
            buf = f.read(CHUNK)
            if not buf:
                break
            data = carry + buf
            base = pos - len(carry)
            start = 0
            while True:
                i = data.find(b"\ntbl;", start)
                if i < 0:
                    break
                j = data.find(b"\nrec;", i + 1)
                if j < 0 and len(buf) == CHUNK:
                    break
                head = data[i + 1 : (j if j > 0 else len(data))].decode("utf-8", "replace")
                entry: dict = {"offset": base + i + 1}
                for ln in head.split("\n"):
                    if ln.startswith("tbl;"):
                        entry["table"] = ln[4:].strip()
                    elif ln.startswith("atr;"):
                        entry["atr"] = ln[4:].strip().split(";")
                    elif ln.startswith("frm;"):
                        entry["frm"] = ln[4:].strip().split(";")
                    elif ln.startswith("num;"):
                        try:
                            entry["num"] = int(ln[4:].strip())
                        except ValueError:
                            entry["num"] = 0
                if "table" in entry and "atr" in entry:
                    tables.append(entry)
                    print(f"  {entry['table']}: {entry.get('num', '?')} @ {entry['offset']}", flush=True)
                start = i + 1
            pos += len(buf)
            leftover = data.find(b"\ntbl;", start)
            carry = data[start - 1 :] if leftover < 0 and start else data[-4096:]
            if len(carry) > 1 << 20:
                carry = carry[-4096:]
    print(f"  indexed {len(tables)} tables in {time.time() - t0:.1f}s", flush=True)
    cache.write_text(
        json.dumps({"idf_size": size, "idf_mtime": mtime, "tables": tables}, indent=1),
        encoding="utf-8",
    )
    return tables


def _iter_rows(path: Path, tables: list[dict], name: str, quick=None):
    tab = {t["table"]: t for t in tables}
    order = [t["table"] for t in tables]
    t = tab[name]
    nxt = order.index(name) + 1
    end = tab[order[nxt]]["offset"] if nxt < len(order) else path.stat().st_size
    atr = t["atr"]
    t0 = time.time()
    n = kept = 0
    with path.open("rb") as f:
        f.seek(t["offset"])
        remaining = end - t["offset"]
        buf = io.BufferedReader(f, 16 << 20)
        while remaining > 0:
            line = buf.readline()
            if not line:
                break
            remaining -= len(line)
            if not line.startswith(b"rec;"):
                continue
            n += 1
            if quick is not None and not quick(line):
                continue
            text = line[4:].decode("utf-8", "replace").rstrip("\r\n")
            parts = next(csv.reader([text], delimiter=";", quotechar='"'))
            if len(parts) != len(atr):
                continue
            kept += 1
            yield dict(zip(atr, parts))
    print(f"  {name}: {n} scanned, {kept} kept, {time.time() - t0:.1f}s", flush=True)


def _field_quick(atr: list[str], field: str, idset: set[str]):
    idx = atr.index(field) + 1

    def check(line: bytes) -> bool:
        parts = line.split(b";", idx + 1)
        if len(parts) <= idx:
            return False
        return parts[idx].strip(b'"\r\n').decode("ascii", "ignore") in idset

    return check


def _either_quick(atr: list[str], a: str, b: str, idset: set[str]):
    ia = atr.index(a) + 1
    ib = atr.index(b) + 1
    last = max(ia, ib) + 1

    def check(line: bytes) -> bool:
        parts = line.split(b";", last)
        if len(parts) <= max(ia, ib):
            return False
        return (
            parts[ia].strip(b'"\r\n').decode("ascii", "ignore") in idset
            or parts[ib].strip(b'"\r\n').decode("ascii", "ignore") in idset
        )

    return check


def _wgs_window(crs: str, bbox: list[float], margin_m: float) -> tuple[float, float, float, float]:
    xmin, ymin, xmax, ymax = bbox
    to_wgs = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    xs = [xmin - margin_m, xmax + margin_m, xmin - margin_m, xmax + margin_m]
    ys = [ymin - margin_m, ymin - margin_m, ymax + margin_m, ymax + margin_m]
    lon, lat = to_wgs.transform(xs, ys)
    return min(lon), max(lon), min(lat), max(lat)


def _into_working(site: dict, crs: str):
    if load_authorities(site):
        try:
            return transformer_into_working(site, gip_authority_key(site), "EPSG:4326")
        except SystemExit:
            pass
    return Transformer.from_crs("EPSG:4326", crs, always_xy=True)


def _xy(tr, lon, lat) -> tuple[float, float]:
    x, y = tr.transform(lon, lat)
    return float(x), float(y)


def _extract(path: Path, tables: list[dict], window, tr) -> dict:
    lon0, lon1, lat0, lat1 = window
    tab = {t["table"]: t for t in tables}

    nodes: dict[str, dict] = {}
    for r in _iter_rows(path, tables, "NODE"):
        x, y = _num(r["X"]), _num(r["Y"])
        if x is None or y is None:
            continue
        if lon0 <= x <= lon1 and lat0 <= y <= lat1:
            nodes[r["OBJECT_ID"]] = r
    print(f"  nodes in window: {len(nodes)}", flush=True)

    node_ids = set(nodes)
    links: dict[str, dict] = {}
    q_link_nodes = _either_quick(tab["LINK"]["atr"], "NODE_FROM_ID", "NODE_TO_ID", node_ids)
    for r in _iter_rows(path, tables, "LINK", quick=q_link_nodes):
        links[r["OBJECT_ID"]] = r
    print(f"  links touching those nodes: {len(links)}", flush=True)

    needed = {r["NODE_FROM_ID"] for r in links.values()} | {r["NODE_TO_ID"] for r in links.values()}
    missing = needed - node_ids
    if missing:
        q_miss = _field_quick(tab["NODE"]["atr"], "OBJECT_ID", missing)
        for r in _iter_rows(path, tables, "NODE", quick=q_miss):
            nodes[r["OBJECT_ID"]] = r
        print(f"  extra endpoint nodes: {len(missing)}", flush=True)

    link_ids = set(links)
    q_on_link = _field_quick(tab["LINK_COORDINATE"]["atr"], "LINK_ID", link_ids)
    coords: dict[str, list] = defaultdict(list)
    for r in _iter_rows(path, tables, "LINK_COORDINATE", quick=q_on_link):
        coords[r["LINK_ID"]].append(
            (int(float(r["SEQUENCE"] or 0)), _num(r["X"]), _num(r["Y"]), _num(r["Z"]))
        )

    q_use = _field_quick(tab["LINEAR_USE_PART"]["atr"], "LINK_ID", link_ids)
    use_parts: dict[str, list] = defaultdict(list)
    for r in _iter_rows(path, tables, "LINEAR_USE_PART", quick=q_use):
        use_parts[r["LINK_ID"]].append(r)
    use_ids = {p["LINEAR_USE_ID"] for parts in use_parts.values() for p in parts}

    q_turn = _field_quick(tab["TURN_LINK"]["atr"], "NODE_VIA_ID", set(nodes))
    turns = list(_iter_rows(path, tables, "TURN_LINK", quick=q_turn))
    turns = [t for t in turns if t["LINK_FROM_ID"] in link_ids and t["LINK_TO_ID"] in link_ids]

    refobj = {r["OBJECT_ID"]: r for r in _iter_rows(path, tables, "REFERENCE_OBJECT")}
    q_l2r = _field_quick(tab["LINK_2_REFERENCE_OBJECT"]["atr"], "LINK_ID", link_ids)
    link2ref = list(_iter_rows(path, tables, "LINK_2_REFERENCE_OBJECT", quick=q_l2r))
    q_u2r = _field_quick(tab["USE_2_REFERENCE_OBJECT"]["atr"], "LINEAR_USE_ID", use_ids)
    use2ref = list(_iter_rows(path, tables, "USE_2_REFERENCE_OBJECT", quick=q_u2r))

    waynames = {r["OBJECT_ID"]: r for r in _iter_rows(path, tables, "WAY_NAMES")}
    q_l2w = _field_quick(tab["LINK_2_WAY_NAMES"]["atr"], "LINK_ID", link_ids)
    link2way = list(_iter_rows(path, tables, "LINK_2_WAY_NAMES", quick=q_l2w))
    q_l2e = _field_quick(tab["LINK_2_EDGE_SEQUENCE"]["atr"], "LINK_ID", link_ids)
    link2edge = list(_iter_rows(path, tables, "LINK_2_EDGE_SEQUENCE", quick=q_l2e))

    node_xy: dict[str, tuple[float, float]] = {}
    for nid, r in nodes.items():
        node_xy[nid] = _xy(tr, _num(r["X"]), _num(r["Y"]))

    return {
        "nodes": nodes,
        "node_xy": node_xy,
        "links": links,
        "coords": coords,
        "use_parts": use_parts,
        "turns": turns,
        "refobj": refobj,
        "link2ref": link2ref,
        "use2ref": use2ref,
        "waynames": waynames,
        "link2way": link2way,
        "link2edge": link2edge,
    }


def _uses_for(link_id: str, raw: dict) -> list[dict]:
    out = []
    for p in raw["use_parts"].get(link_id, []):
        bt = _int(p["BASE_TYPE"])
        out.append(
            {
                "base_type": bt,
                "base_name": BASE_TYPE.get(bt, str(bt)),
                "width_m": _num(p["WIDTH_AVERAGE"]),
                "width_min_m": _num(p["WIDTH_MINIMAL"]),
                "offset_m": _num(p["OFFSET_AVERAGE"]),
                "from_pct": _num(p["LINK_PERCENTAGE_FROM"]),
                "to_pct": _num(p["LINK_PERCENTAGE_TO"]),
                "surface": _int(p["SURFACE"]),
            }
        )
    return out


def _width_m(uses: list[dict]) -> float | None:
    lanes = [u["width_m"] for u in uses if u.get("base_type") == 1 and u.get("width_m") is not None]
    return float(lanes[0]) if lanes else None


def _structs_for(link_id: str, raw: dict, by_link: dict | None = None) -> list[dict]:
    rows = ((by_link or {}).get(link_id) or []) if by_link is not None else [r for r in raw["link2ref"] if r["LINK_ID"] == link_id]
    out = []
    for r in rows:
        ro = raw["refobj"].get(r["REFERENCE_OBJECT_ID"])
        if not ro:
            continue
        kind = _text(ro.get("REFERENCE_TYPE_LONG"))
        if not any(k in kind.lower() for k in _STRUCT_KEYS):
            continue
        out.append(
            {
                "id": r["REFERENCE_OBJECT_ID"],
                "type": kind,
                "name": _text(ro.get("FEATURE_NAME") or ro.get("NAME_TEXT")),
                "external_id": _text(ro.get("EXTERNAL_ID")),
                "from_pct": _num(r["LINK_PERCENTAGE_FROM"]),
                "to_pct": _num(r["LINK_PERCENTAGE_TO"]),
            }
        )
    return out


def _way_for(link_id: str, raw: dict, by_link: dict | None = None) -> tuple[str, str]:
    cat = code = ""
    rows = ((by_link or {}).get(link_id) or []) if by_link is not None else [r for r in raw["link2way"] if r["LINK_ID"] == link_id]
    for r in rows:
        wn = raw["waynames"].get(r["WAY_OBJECT_ID"])
        if not wn:
            continue
        cat = cat or _text(wn.get("NAME_CATEGORY_LONG"))
        code = code or _text(wn.get("STREET_CODE") or wn.get("SHORT_NAME"))
    return cat, code


def _seq_name(link_id: str, raw: dict, by_link: dict | None = None) -> str:
    rows = ((by_link or {}).get(link_id) or []) if by_link is not None else raw["link2edge"]
    for r in rows:
        if r["LINK_ID"] == link_id:
            return _text(r.get("EDGE_SEQUENCE_FEATURE_NAME"))
    return ""


def _link_line(link_id: str, rec: dict, raw: dict, tr) -> LineString | None:
    a = rec["NODE_FROM_ID"]
    b = rec["NODE_TO_ID"]
    if a not in raw["nodes"] or b not in raw["nodes"]:
        return None
    na, nb = raw["nodes"][a], raw["nodes"][b]
    pts = [(_num(na["X"]), _num(na["Y"]), _num(na["Z"]))]
    for _seq, x, y, z in sorted(raw["coords"].get(link_id, [])):
        if x is None or y is None:
            continue
        pts.append((x, y, z))
    pts.append((_num(nb["X"]), _num(nb["Y"]), _num(nb["Z"])))
    if len(pts) < 2:
        return None
    xs, ys = tr.transform([p[0] for p in pts], [p[1] for p in pts])
    return LineString([(float(x), float(y), p[2] if p[2] is not None else 0.0) for x, y, p in zip(xs, ys, pts)])


def _build_records(raw: dict, tr) -> tuple[dict, dict, dict]:
    """Return (nodes_out, links_out, geoms_by_link)."""
    nodes_out: dict[str, dict] = {}
    for nid, r in raw["nodes"].items():
        x, y = raw["node_xy"][nid]
        nodes_out[nid] = {
            "id": nid,
            "short_id": _int(r["SHORT_ID"]),
            "level": _num(r["LEVEL"], 0.0),
            "x": round(x, 3),
            "y": round(y, 3),
            "z": _num(r["Z"]),
            "degree": _int(r["EDGE_DEGREE"], 0),
            "form": _int(r["FORM_OF_NODE"], 0),
            "name": _text(r.get("FEATURE_NAME")),
            "plateau_id": _text(r.get("PLATEAU_ID")) or None,
            "links": [],
        }

    geoms: dict[str, LineString] = {}
    links_out: dict[str, dict] = {}
    ref_by = defaultdict(list)
    for r in raw["link2ref"]:
        ref_by[r["LINK_ID"]].append(r)
    way_by = defaultdict(list)
    for r in raw["link2way"]:
        way_by[r["LINK_ID"]].append(r)
    seq_by = defaultdict(list)
    for r in raw["link2edge"]:
        seq_by[r["LINK_ID"]].append(r)
    for lid, rec in raw["links"].items():
        geom = _link_line(lid, rec, raw, tr)
        if geom is None:
            continue
        geoms[lid] = geom
        uses = _uses_for(lid, raw)
        fc = _int(rec["FUNCTIONAL_CLASS"])
        fow = _int(rec["FORM_OF_WAY"])
        way_cat, street_code = _way_for(lid, raw, way_by)
        nf, nt = rec["NODE_FROM_ID"], rec["NODE_TO_ID"]
        links_out[lid] = {
            "id": lid,
            "short_id": _int(rec["SHORT_ID"]),
            "node_from": nf,
            "node_to": nt,
            "fc": fc,
            "fc_name": FRC.get(fc, str(fc)),
            "fow": fow,
            "fow_name": FOW.get(fow, str(fow)),
            "edge_category": _text(rec.get("EDGE_CATEGORY")),
            "level_i": _num(rec.get("LEVEL_INTERMEDIATE"), 0.0),
            "level_from": _num(raw["nodes"].get(nf, {}).get("LEVEL"), 0.0),
            "level_to": _num(raw["nodes"].get(nt, {}).get("LEVEL"), 0.0),
            "width_m": _width_m(uses),
            "lanes_tow": _int(rec.get("LANES_TOW_MAX")),
            "lanes_bkw": _int(rec.get("LANES_BKW_MAX")),
            "oneway_car": _int(rec.get("ONEWAY_CAR")),
            "maxspeed": _int(rec.get("MAXSPEED_TOW_CAR")),
            "urban": _int(rec.get("URBAN")),
            "name": _text(rec.get("NAME_TEXT_HR")),
            "short_name": _text(rec.get("SHORT_NAME_HR")),
            "way_category": way_cat or None,
            "street_code": street_code or _text(rec.get("SHORT_NAME_HR")) or None,
            "sequence_name": _seq_name(lid, raw, seq_by) or None,
            "length_m": round(_num(rec.get("LENGTH"), geom.length), 3),
            "uses": uses,
            "structures": _structs_for(lid, raw, ref_by),
        }
        if nf in nodes_out:
            nodes_out[nf]["links"].append(lid)
        if nt in nodes_out:
            nodes_out[nt]["links"].append(lid)

    for n in nodes_out.values():
        n["links"] = sorted(set(n["links"]))
    return {k: v for k, v in nodes_out.items() if v["links"]}, links_out, geoms


def _turns_by_node(raw: dict, link_ids: set[str], node_ids: set[str]) -> dict[str, list]:
    out: dict[str, list] = defaultdict(list)
    for t in raw["turns"]:
        via = t["NODE_VIA_ID"]
        frm, to = t["LINK_FROM_ID"], t["LINK_TO_ID"]
        if via not in node_ids or frm not in link_ids or to not in link_ids:
            continue
        out[via].append({"from": frm, "to": to})
    return dict(out)


def _crossings(links_out: dict, geoms: dict) -> list[dict]:
    ids = list(geoms)
    tree = shapely.STRtree([geoms[i] for i in ids])
    pairs = tree.query([geoms[i] for i in ids], predicate="crosses")
    seen = set()
    rows = []
    for a, b in pairs.T:
        if a >= b:
            continue
        ia, ib = ids[int(a)], ids[int(b)]
        key = tuple(sorted((ia, ib)))
        if key in seen:
            continue
        seen.add(key)
        la, lb = links_out[ia], links_out[ib]
        share = {la["node_from"], la["node_to"]} & {lb["node_from"], lb["node_to"]}
        if share:
            continue
        hit = geoms[ia].intersection(geoms[ib])
        if hit.is_empty:
            continue
        pt = hit if hit.geom_type == "Point" else hit.representative_point()
        rows.append(
            {
                "x": round(float(pt.x), 3),
                "y": round(float(pt.y), 3),
                "a": ia,
                "b": ib,
                "level_a": la["level_i"],
                "level_b": lb["level_i"],
                "fc_a": la["fc"],
                "fc_b": lb["fc"],
                "name_a": la["name"] or la["short_name"],
                "name_b": lb["name"] or lb["short_name"],
                "roads_a": [],
                "roads_b": [],
            }
        )
    return rows


def _join_roads(site: dict, links_out: dict, geoms: dict, nodes_out: dict, crossings: list[dict]) -> dict:
    try:
        roads = gpd.read_file(find_gip_geojson(site))
    except SystemExit as exc:
        print(f"  no Verkehrswege cache ({exc}) — topology only", flush=True)
        return {}
    if roads.crs is None:
        roads = roads.set_crs(str(site.get("crs") or "EPSG:31254"))
    working = str(site.get("crs") or "EPSG:31254")
    if str(roads.crs) != working:
        roads = roads.to_crs(working)

    line_ids = list(geoms)
    tree = shapely.STRtree([geoms[i] for i in line_ids])
    bufs = {i: geoms[i].buffer(MATCH_M) for i in line_ids}
    node_ids = list(nodes_out)
    ntree = shapely.STRtree([Point(n["x"], n["y"]) for n in (nodes_out[i] for i in node_ids)])

    joined: dict[str, dict] = {}
    road_by_oid: dict[int, object] = {}
    for rec in roads.itertuples(index=False):
        oid = int(getattr(rec, "OBJECTID"))
        geom = rec.geometry
        if geom is None or geom.is_empty:
            continue
        road_by_oid[oid] = geom
        hits = []
        for i in tree.query(geom.buffer(MATCH_M), predicate="intersects"):
            lid = line_ids[int(i)]
            overlap = float(geom.intersection(bufs[lid]).length)
            if overlap <= 0:
                continue
            share_road = overlap / max(geom.length, 1e-6)
            share_link = overlap / max(geoms[lid].length, 1e-6)
            if share_road < MATCH_SHARE and share_link < MATCH_SHARE:
                continue
            hits.append(
                {
                    "id": lid,
                    "overlap_m": round(overlap, 2),
                    "share": round(min(1.0, share_road), 3),
                    "fc": links_out[lid]["fc"],
                    "level_i": links_out[lid]["level_i"],
                }
            )
        node_hits = []
        for j in ntree.query(geom.buffer(8.0), predicate="intersects"):
            nid = node_ids[int(j)]
            n = nodes_out[nid]
            if geom.distance(Point(n["x"], n["y"])) <= 8.0:
                node_hits.append(nid)
        levels = sorted({h["level_i"] for h in hits})
        joined[str(oid)] = {
            "objectid": oid,
            "objekt": _text(getattr(rec, "OBJEKT", "")),
            "str_code": _text(getattr(rec, "STR_CODE", "")) or None,
            "strname": _text(getattr(rec, "STRNAME", "")) or None,
            "length_m": round(float(geom.length), 2),
            "links": sorted(hits, key=lambda h: -h["overlap_m"]),
            "nodes": node_hits,
            "levels": levels,
        }

    for row in crossings:
        pt = Point(row["x"], row["y"])
        for oid, geom in road_by_oid.items():
            if geom.distance(pt) > 8.0:
                continue
            info = joined.get(str(oid))
            if not info:
                continue
            lids = _hit_ids(info)
            if row["a"] in lids:
                row["roads_a"].append(oid)
            if row["b"] in lids:
                row["roads_b"].append(oid)
        row["roads_a"] = sorted(set(row["roads_a"]))
        row["roads_b"] = sorted(set(row["roads_b"]))
    return joined


def _write_gpkg(
    path: Path,
    site: dict,
    crs: str,
    nodes_out: dict,
    links_out: dict,
    geoms: dict,
    crossings: list[dict],
    roads: dict,
    turns: dict,
) -> None:
    if path.exists():
        path.unlink()

    n_rows = [
        {
            "node_id": n["id"],
            "short_id": n["short_id"],
            "level": n["level"],
            "z": n["z"],
            "degree": n["degree"],
            "form": n["form"],
            "name": n["name"],
            "n_links": len(n["links"]),
            "geometry": Point(n["x"], n["y"]),
        }
        for n in nodes_out.values()
    ]
    gpd.GeoDataFrame(n_rows, crs=crs).to_file(path, layer="node", driver="GPKG")

    l_rows = []
    for lid, rec in links_out.items():
        l_rows.append(
            {
                "link_id": lid,
                "short_id": rec["short_id"],
                "fc": rec["fc"],
                "fc_name": rec["fc_name"],
                "fow": rec["fow"],
                "edge_cat": rec["edge_category"],
                "level_i": rec["level_i"],
                "level_from": rec["level_from"],
                "level_to": rec["level_to"],
                "width_m": rec["width_m"],
                "name": rec["name"],
                "short_name": rec["short_name"],
                "street_code": rec["street_code"],
                "length_m": rec["length_m"],
                "n_use": len(rec["uses"]),
                "n_struct": len(rec["structures"]),
                "geometry": geoms[lid],
            }
        )
    gpd.GeoDataFrame(l_rows, crs=crs).to_file(path, layer="link", driver="GPKG")

    t_rows = []
    for via, items in turns.items():
        n = nodes_out.get(via)
        if not n:
            continue
        t_rows.append(
            {
                "node_id": via,
                "n_turns": len(items),
                "degree": n["degree"],
                "name": n["name"],
                "geometry": Point(n["x"], n["y"]),
            }
        )
    if t_rows:
        gpd.GeoDataFrame(t_rows, crs=crs).to_file(path, layer="turn_node", driver="GPKG")

    c_rows = [
        {
            "level_a": r["level_a"],
            "level_b": r["level_b"],
            "fc_a": r["fc_a"],
            "fc_b": r["fc_b"],
            "name_a": r["name_a"],
            "name_b": r["name_b"],
            "roads_a": ",".join(str(x) for x in r["roads_a"]),
            "roads_b": ",".join(str(x) for x in r["roads_b"]),
            "geometry": Point(r["x"], r["y"]),
        }
        for r in crossings
    ]
    if c_rows:
        gpd.GeoDataFrame(c_rows, crs=crs).to_file(path, layer="crossing", driver="GPKG")

    b_rows = []
    for rec in links_out.values():
        geom = geoms[rec["id"]]
        for s in rec["structures"]:
            f0 = (s["from_pct"] or 0.0) / 100.0
            f1 = (s["to_pct"] if s["to_pct"] is not None else 100.0) / 100.0
            if f1 < f0:
                f0, f1 = f1, f0
            try:
                piece = substring(geom, f0, f1, normalized=True)
            except Exception:
                piece = geom
            b_rows.append(
                {
                    "ref_id": s["id"],
                    "link_id": rec["id"],
                    "typ": s["type"],
                    "name": s["name"],
                    "external_id": s["external_id"],
                    "from_pct": s["from_pct"],
                    "to_pct": s["to_pct"],
                    "geometry": piece,
                }
            )
    if b_rows:
        gpd.GeoDataFrame(b_rows, crs=crs).to_file(path, layer="bridge_ref", driver="GPKG")

    if roads:
        try:
            vw = gpd.read_file(find_gip_geojson(site))
            if vw.crs is None:
                vw = vw.set_crs(crs)
            elif str(vw.crs) != crs:
                vw = vw.to_crs(crs)
            extra = []
            for rec in vw.itertuples(index=False):
                oid = int(getattr(rec, "OBJECTID"))
                info = roads.get(str(oid)) or {}
                extra.append(
                    {
                        "OBJECTID": oid,
                        "OBJEKT": _text(getattr(rec, "OBJEKT", "")),
                        "n_links": len(info.get("links") or []),
                        "n_nodes": len(info.get("nodes") or []),
                        "levels": ",".join(str(x) for x in (info.get("levels") or [])),
                        "link_ids": ";".join(h["id"] for h in (info.get("links") or [])[:8]),
                        "geometry": rec.geometry,
                    }
                )
            gpd.GeoDataFrame(extra, crs=crs).to_file(path, layer="road_join", driver="GPKG")
        except SystemExit:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--idf", help="routingexport_ogd.txt")
    ap.add_argument("--margin", type=float, default=None, help=f"bbox margin in m (default {MARGIN_M})")
    ap.add_argument("--force", action="store_true", help="rebuild even if output is newer than the IDF")
    args = ap.parse_args()

    site = load_site()
    crs = str(site.get("crs") or "EPSG:31254")
    bbox = list(map(float, site["bbox"]))
    if load_authorities(site):
        crs, bbox = apply_working_frame(site)
    gip = (site.get("sources") or {}).get("gip") or {}
    margin = float(args.margin if args.margin is not None else gip.get("routing_margin_m") or MARGIN_M)

    src = idf_path(site, args.idf)
    if not src.is_file():
        raise SystemExit(f"missing {src}")
    jp, gp = json_path(site), gpkg_path(site)
    if not args.force and jp.is_file() and gp.is_file():
        newest = max(jp.stat().st_mtime, gp.stat().st_mtime)
        if newest >= src.stat().st_mtime:
            print(f"up to date: {jp}")
            return

    tables = _index_tables(src)
    needed = {
        "NODE",
        "LINK",
        "LINK_COORDINATE",
        "LINEAR_USE_PART",
        "TURN_LINK",
        "REFERENCE_OBJECT",
        "LINK_2_REFERENCE_OBJECT",
        "USE_2_REFERENCE_OBJECT",
        "WAY_NAMES",
        "LINK_2_WAY_NAMES",
        "LINK_2_EDGE_SEQUENCE",
    }
    have = {t["table"] for t in tables}
    missing = needed - have
    if missing:
        raise SystemExit(f"IDF is missing tables: {sorted(missing)}")

    window = _wgs_window(crs, bbox, margin)
    print(
        f"window lon {window[0]:.5f}..{window[1]:.5f} lat {window[2]:.5f}..{window[3]:.5f} "
        f"(margin {margin:.0f} m)",
        flush=True,
    )
    tr = _into_working(site, crs)
    raw = _extract(src, tables, window, tr)
    nodes_out, links_out, geoms = _build_records(raw, tr)
    turns = _turns_by_node(raw, set(links_out), set(nodes_out))
    crossings = _crossings(links_out, geoms)
    print(f"  graph: {len(nodes_out)} nodes, {len(links_out)} links, {sum(len(v) for v in turns.values())} turns, {len(crossings)} crossings", flush=True)
    roads = _join_roads(site, links_out, geoms, nodes_out, crossings)
    n_joined = sum(1 for r in roads.values() if r["links"])
    print(f"  Verkehrswege: {len(roads)} axes, {n_joined} with a routing link", flush=True)

    blob = {
        "idf": str(src),
        "crs": crs,
        "bbox": bbox,
        "margin_m": margin,
        "match_m": MATCH_M,
        "match_share": MATCH_SHARE,
        "n_nodes": len(nodes_out),
        "n_links": len(links_out),
        "n_turns": sum(len(v) for v in turns.values()),
        "n_crossings": len(crossings),
        "n_roads": len(roads),
        "n_roads_joined": n_joined,
        "nodes": nodes_out,
        "links": links_out,
        "turns_by_node": turns,
        "crossings": crossings,
        "roads": roads,
    }
    jp.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {jp} ({jp.stat().st_size / 1e6:.1f} MB)", flush=True)
    _write_gpkg(gp, site, crs, nodes_out, links_out, geoms, crossings, roads, turns)
    print(f"wrote {gp}", flush=True)
    grade = [c for c in crossings if c["level_a"] != c["level_b"] or c["roads_a"] or c["roads_b"]]
    print(f"grade-separated crossings: {len(grade)} (of {len(crossings)} without a shared node)")


if __name__ == "__main__":
    main()
