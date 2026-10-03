"""Decide which GIP pieces are bridges, from the heightmap rather than the label alone.

GIP ``S-AB`` / ``S-BB`` / ``S-LB`` / ``S-GB`` and a Kunstbauten name containing "Brücke" are
claims. A claim is a *bridge* only when the heightmap sags under the chord
(there is a void to stamp a deck into). When the DGM already follows the
claimed piece:

- no other road under it → ordinary carriageway (``no_opening``)
- another road under it → the DGM *is* the upper deck; the structure is the
  lower road (``cover_is_dgm``). Do not build an upper plate. The lower road
  is an underpass (mesh + later portals). Clearance is
  ``min(4 m, GIP-axis gap − 1 m)``.

A short named piece that does sag, or that has a road under it while the
DGM is *not* the upper surface, becomes a bridge even when GIP called it a
normal carriageway.

The result is ``gip_bridge_flags.json``. Road loading applies it to ``objekt``
and ``bridge``. Bridge generation reads ``bridge`` and ignores the raw label.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from site_coords import load_site, processed_dir

# Third letter B, and not a gallery (S-BG).
_GIP_BRIDGE_OBJEKT = frozenset({"S-AB", "S-BB", "S-LB", "S-GB"})
_BRIDGE_TO_ROAD = {"S-AB": "S-A", "S-BB": "S-B", "S-LB": "S-L", "S-GB": "S-G"}
_ROAD_TO_BRIDGE = {v: k for k, v in _BRIDGE_TO_ROAD.items()}
_TUNNEL_OR_GALLERY = frozenset({"S-AT", "S-BT", "S-LT", "S-BG"})

# Below this, the deck chord and the heightmap agree: no opening under the piece.
DIP_REVOKE_M = 1.0
# A named carriageway this deep under its own chord is a span, not a grade.
DIP_PROMOTE_M = 2.5
# Other road this far below the piece counts as passing under, not a junction.
UNDERCROSS_M = 2.5
# Unmarked upper road (often S-G): the surface model is a deck when it stays
# this far above the DGM for at least DOM_OPEN_RUN_M. Footpaths are ignored.
DOM_OPEN_M = 1.5
DOM_OPEN_RUN_M = 4.0
# The deck has to be the piece, not a 5 m bump on a junction (91651).
DOM_OPEN_SHARE = 0.5
_FOOT_OBJEKT = frozenset({"S-FRW", "S-STRAIL"})
# Unsigned municipal underpass: 4 m clearance, or less if the two axes
# leave less room (1 m stays for the cover structure).
CLEAR_DEFAULT_M = 4.0
CLEAR_RESERVE_M = 1.0
PROMOTE_MIN_M = 6.0
PROMOTE_MAX_M = 160.0
_CELL_M = 40.0


def flags_path(site: dict | None = None) -> Path:
    site = site or load_site()
    return processed_dir(site) / "gip_bridge_flags.json"


def load_flags(site: dict | None = None) -> dict[str, dict] | None:
    path = flags_path(site)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("flags") if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        return None
    return rows


def gip_objekt(road: dict) -> str:
    """OBJEKT as GIP sent it, even after a previous correction."""
    raw = road.get("objekt_gip")
    if raw:
        return str(raw).upper().strip()
    return str(road.get("objekt") or "").upper().strip()


def _name_says_bridge(road: dict) -> bool:
    blob = " ".join(
        str(road.get(k) or "") for k in ("kunstbauten", "objektbezeichnung")
    ).lower()
    return "brücke" in blob or "bruecke" in blob


def gip_claims_bridge(road: dict) -> bool:
    obj = gip_objekt(road)
    if obj in _TUNNEL_OR_GALLERY:
        return False
    if obj in _GIP_BRIDGE_OBJEKT:
        return True
    return _name_says_bridge(road)


def _chain(nodes: list) -> tuple[list[float], float]:
    cum = [0.0]
    for a, b in zip(nodes, nodes[1:]):
        cum.append(cum[-1] + math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1])))
    return cum, float(cum[-1] if cum else 0.0)


def chord_dip_m(nodes: list) -> float:
    """How far the heightmap falls below the straight line between the two ends."""
    if len(nodes) < 3:
        return 0.0
    cum, total = _chain(nodes)
    if total < 1e-6:
        return 0.0
    z0 = float(nodes[0][2])
    z1 = float(nodes[-1][2])
    dip = 0.0
    for s, n in zip(cum, nodes):
        chord = z0 + (s / total) * (z1 - z0)
        dip = max(dip, chord - float(n[2]))
    return dip


def _seg_hit(a, b, c, d) -> tuple[float, float] | None:
    rx, ry = float(b[0]) - float(a[0]), float(b[1]) - float(a[1])
    sx, sy = float(d[0]) - float(c[0]), float(d[1]) - float(c[1])
    den = rx * sy - ry * sx
    if abs(den) < 1e-9:
        return None
    t = ((float(c[0]) - float(a[0])) * sy - (float(c[1]) - float(a[1])) * sx) / den
    u = ((float(c[0]) - float(a[0])) * ry - (float(c[1]) - float(a[1])) * rx) / den
    if 0.02 < t < 0.98 and 0.02 < u < 0.98:
        return t, u
    return None


def _grid_key(x: float, y: float) -> tuple[int, int]:
    return int(math.floor(x / _CELL_M)), int(math.floor(y / _CELL_M))


def _index_segments(roads: dict) -> dict[tuple[int, int], list[tuple[str, list, list]]]:
    buckets: dict[tuple[int, int], list[tuple[str, list, list]]] = {}
    for key, road in roads.items():
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        cum, _total = _chain(nodes)
        for i in range(len(nodes) - 1):
            a, b = nodes[i], nodes[i + 1]
            x0, x1 = sorted((float(a[0]), float(b[0])))
            y0, y1 = sorted((float(a[1]), float(b[1])))
            ix0, iy0 = _grid_key(x0, y0)
            ix1, iy1 = _grid_key(x1, y1)
            for ix in range(ix0, ix1 + 1):
                for iy in range(iy0, iy1 + 1):
                    buckets.setdefault((ix, iy), []).append((str(key), a, b))
    return buckets


def _as_oid(key) -> int | None:
    try:
        return int(key)
    except (TypeError, ValueError):
        return None


def cover_clear_height_m(dz_m: float) -> float:
    """Lichte Höhe unter einem DGM-Deck: 4 m, oder Achsabstand minus 1 m."""
    return min(CLEAR_DEFAULT_M, float(dz_m) - CLEAR_RESERVE_M)


def undercross_hits(key: str, road: dict, buckets: dict) -> list[tuple[int, float]]:
    """Other roads crossing the interior and sitting clearly lower.

    Each hit is ``(objectid, dz_m)`` with ``dz_m = z_upper − z_lower`` at
    the crossing. The same object keeps its largest gap.
    """
    nodes = road.get("nodes") or []
    if len(nodes) < 2:
        return []
    cum, total = _chain(nodes)
    if total < 1.0:
        return []
    end_keep = min(4.0, 0.25 * total)
    best: dict[int, float] = {}
    seen: set[tuple[str, int]] = set()
    for i in range(len(nodes) - 1):
        a, b = nodes[i], nodes[i + 1]
        x0, x1 = sorted((float(a[0]), float(b[0])))
        y0, y1 = sorted((float(a[1]), float(b[1])))
        ix0, iy0 = _grid_key(x0, y0)
        ix1, iy1 = _grid_key(x1, y1)
        for ix in range(ix0 - 1, ix1 + 2):
            for iy in range(iy0 - 1, iy1 + 2):
                for other_key, c, d in buckets.get((ix, iy), []):
                    if other_key == key:
                        continue
                    hit = _seg_hit(a, b, c, d)
                    if hit is None:
                        continue
                    t, _u = hit
                    s = cum[i] + t * (cum[i + 1] - cum[i])
                    if s < end_keep or s > total - end_keep:
                        continue
                    token = (other_key, int(round(s)))
                    if token in seen:
                        continue
                    seen.add(token)
                    z_here = float(a[2]) + t * (float(b[2]) - float(a[2]))
                    u = hit[1]
                    z_other = float(c[2]) + u * (float(d[2]) - float(c[2]))
                    dz = z_here - z_other
                    if dz < UNDERCROSS_M:
                        continue
                    oid = _as_oid(other_key)
                    if oid is None:
                        continue
                    prev = best.get(oid)
                    if prev is None or dz > prev:
                        best[oid] = dz
    return [(oid, best[oid]) for oid in best]


def undercross_oids(key: str, road: dict, buckets: dict) -> list[int]:
    return [oid for oid, _dz in undercross_hits(key, road, buckets)]


def undercross_count(key: str, road: dict, buckets: dict) -> int:
    return len(undercross_hits(key, road, buckets))


def _corrected_objekt(obj: str, *, bridge: bool) -> str:
    if bridge:
        return _ROAD_TO_BRIDGE.get(obj, obj)
    return _BRIDGE_TO_ROAD.get(obj, obj)


def _grade_pairs(site: dict | None):
    try:
        from gip_routing_load import grade_clip_pairs, try_load_routing
    except ImportError:
        return None, []
    topo = try_load_routing(site)
    if topo is None:
        return None, []
    return topo, grade_clip_pairs(topo)


def promote_dom_decks(site: dict, roads: dict, flags: dict[str, dict]) -> list[str]:
    """Turn a routing overpass into a bridge when the DOM holds the deck.

    GIP Z often follows the DGM down onto the road below, so the chord test
    never sees an opening. The surface model does. Footpaths stay out. A
    piece longer than ``PROMOTE_MAX_M`` is a through road, not this deck.
    """
    topo, pairs = _grade_pairs(site)
    if topo is None:
        return []
    info = topo.get("roads") or {}
    by_oid: dict[int, dict] = {}
    for key, road in roads.items():
        try:
            by_oid[int(road.get("objectid") or key)] = road
        except (TypeError, ValueError):
            continue
    lowers_of: dict[int, set[int]] = {}
    for up, lo in pairs:
        for oid in up:
            lowers_of.setdefault(int(oid), set()).update(int(x) for x in lo)

    def _obj(oid: int) -> str:
        return str((info.get(str(oid)) or {}).get("objekt") or gip_objekt(by_oid.get(oid) or {})).upper()

    candidates = []
    for key, rec in flags.items():
        if rec.get("reason") != "routing_over" or rec.get("bridge"):
            continue
        if float(rec.get("length_m") or 0.0) > PROMOTE_MAX_M:
            continue
        try:
            oid = int(key)
        except (TypeError, ValueError):
            continue
        if _obj(oid) in _FOOT_OBJEKT:
            continue
        lowers = lowers_of.get(oid) or set()
        if not lowers or all(_obj(o) in _FOOT_OBJEKT for o in lowers):
            continue
        road = by_oid.get(oid)
        if road is None or len(road.get("nodes") or []) < 2:
            continue
        candidates.append((oid, road, rec))
    if not candidates:
        return []

    from blend_bridge_deck import _load_dom
    from build_road_grid import _load_geotiff
    from site_coords import SiteCoords

    sc = SiteCoords(site)
    dgm, spec = _load_geotiff(processed_dir(site) / "corridor50_raw" / "corridor50_raw.tif")
    promoted = []
    for oid, road, rec in candidates:
        nodes = road["nodes"]
        crs = [sc.terrain_to_crs(float(n[0]), float(n[1])) for n in nodes]
        xs = [p[0] for p in crs]
        ys = [p[1] for p in crs]
        dom, frame = _load_dom(site, oid, min(xs) - 4.0, min(ys) - 4.0, max(xs) + 4.0, max(ys) + 4.0)
        run = _dom_open_run_m(crs, dom, frame, dgm, spec)
        need = max(DOM_OPEN_RUN_M, DOM_OPEN_SHARE * float(rec.get("length_m") or 0.0))
        if run < need:
            continue
        rec["bridge"] = True
        rec["reason"] = "dom_deck"
        rec["role"] = "bridge"
        rec["objekt"] = _corrected_objekt(str(rec.get("objekt_gip") or ""), bridge=True)
        rec["dom_open_m"] = round(run, 1)
        promoted.append(str(oid))
    return promoted


def _sample_cell(arr, meta, x: float, y: float) -> float:
    c = int(round((x - float(meta["xmin"])) / 0.5 - 0.5))
    r = int(round((float(meta["ymax"]) - y) / 0.5 - 0.5))
    if r < 0 or c < 0 or r >= arr.shape[0] or c >= arr.shape[1]:
        return float("nan")
    return float(arr[r, c])


def _dom_open_run_m(crs, dom, frame, dgm, spec) -> float:
    """Longest stretch where the surface model sits above the DGM."""
    step = 1.0
    best = 0.0
    cur = 0.0
    for (x0, y0), (x1, y1) in zip(crs, crs[1:]):
        dist = math.hypot(x1 - x0, y1 - y0)
        n = max(1, int(round(dist / step)))
        for i in range(n):
            t = i / n
            x = x0 + t * (x1 - x0)
            y = y0 + t * (y1 - y0)
            gap = _sample_cell(dom, frame, x, y) - _sample_cell(dgm, spec, x, y)
            if gap >= DOM_OPEN_M:
                cur += dist / n
                best = max(best, cur)
            else:
                cur = 0.0
    return best


def _routing_overpass_oids(site: dict | None) -> set[int]:
    """Upper OBJECTIDs at a GIP crossing without a shared node."""
    try:
        from gip_routing_load import grade_clip_pairs, try_load_routing
    except ImportError:
        return set()
    topo = try_load_routing(site)
    if topo is None:
        return set()
    upper: set[int] = set()
    for up, _lo in grade_clip_pairs(topo):
        upper |= set(up)
    return upper


def classify_roads(roads: dict, site: dict | None = None) -> dict[str, dict]:
    buckets = _index_segments(roads)
    routing_over = _routing_overpass_oids(site)
    flags: dict[str, dict] = {}
    for key, road in roads.items():
        obj = gip_objekt(road)
        if obj in _TUNNEL_OR_GALLERY:
            continue
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        try:
            oid_i = int(road.get("objectid") or key)
        except (TypeError, ValueError):
            continue
        _cum, length = _chain(nodes)
        dip = chord_dip_m(nodes)
        claim = gip_claims_bridge(road)
        named = bool(str(road.get("str_code") or "").strip())
        stem = obj in _ROAD_TO_BRIDGE or obj in _GIP_BRIDGE_OBJEKT or obj in {"S-A", "S-B", "S-L"}
        short = PROMOTE_MIN_M <= length <= PROMOTE_MAX_M
        need_cross = claim or (named and stem and short and dip >= DIP_PROMOTE_M)
        hits = undercross_hits(str(key), road, buckets) if need_cross else []
        under_oids = [oid for oid, _dz in hits]
        crosses = len(hits)
        dz_under = max((dz for _oid, dz in hits), default=0.0)
        routing_upper = oid_i in routing_over
        if claim and dip < DIP_REVOKE_M and crosses == 0:
            bridge = False
            reason = "no_opening"
        elif claim and dip < DIP_REVOKE_M and crosses > 0:
            # DGM already is the upper road. Build the lower road, not a deck.
            bridge = False
            reason = "cover_is_dgm"
        elif (not claim) and named and stem and short and (dip >= DIP_PROMOTE_M or crosses > 0):
            bridge = True
            reason = "opening" if dip >= DIP_PROMOTE_M else "road_under"
        elif (not claim) and routing_upper:
            # Stay a carriageway. The mesh split comes from the routing graph.
            # A raised plate needs DOM; S-G is not a GIP bridge label.
            bridge = False
            reason = "routing_over"
        elif claim:
            bridge = True
            reason = "kept"
        else:
            continue
        new_obj = _corrected_objekt(obj, bridge=bridge)
        rec = {
            "bridge": bridge,
            "objekt": new_obj,
            "objekt_gip": obj,
            "dip_m": round(dip, 2),
            "undercross": crosses,
            "under_oids": under_oids,
            "length_m": round(length, 1),
            "str_code": road.get("str_code"),
            "reason": reason,
        }
        if reason == "cover_is_dgm":
            rec["role"] = "cover"
            rec["dz_under_m"] = round(dz_under, 2)
            rec["clear_height_m"] = round(cover_clear_height_m(dz_under), 2)
        elif reason == "routing_over":
            rec["role"] = "overpass"
        elif bridge:
            rec["role"] = "bridge"
        else:
            rec["role"] = "road"
        flags[str(int(road.get("objectid") or key))] = rec
    return flags


def apply_flags(roads: dict, flags: dict[str, dict]) -> int:
    """Write ``bridge`` and the corrected ``objekt`` onto road records."""
    n = 0
    for key, road in roads.items():
        try:
            oid = str(int(road.get("objectid") or key))
        except (TypeError, ValueError):
            continue
        rec = flags.get(oid)
        if rec is None:
            road["bridge"] = False
            continue
        road["bridge"] = bool(rec.get("bridge"))
        road["objekt_gip"] = rec.get("objekt_gip") or gip_objekt(road)
        if rec.get("objekt"):
            road["objekt"] = rec["objekt"]
        road["bridge_reason"] = rec.get("reason")
        road["structure_role"] = rec.get("role")
        if rec.get("under_oids"):
            road["under_oids"] = list(rec["under_oids"])
        if rec.get("clear_height_m") is not None:
            road["clear_height_m"] = rec["clear_height_m"]
        if rec.get("dz_under_m") is not None:
            road["dz_under_m"] = rec["dz_under_m"]
        n += 1
    return n


def write_flags(site: dict, flags: dict[str, dict]) -> Path:
    path = flags_path(site)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "dip_revoke_m": DIP_REVOKE_M,
        "dip_promote_m": DIP_PROMOTE_M,
        "undercross_m": UNDERCROSS_M,
        "clear_default_m": CLEAR_DEFAULT_M,
        "clear_reserve_m": CLEAR_RESERVE_M,
        "flags": flags,
    }
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return path


def apply_stored_flags(site: dict, roads: dict) -> int:
    flags = load_flags(site)
    if not flags:
        return 0
    return apply_flags(roads, flags)


def cover_records(site: dict | None = None) -> list[dict]:
    """GIP bridge claims whose DGM is the upper road (``cover_is_dgm``)."""
    flags = load_flags(site) or {}
    out = []
    for key, rec in flags.items():
        if rec.get("reason") != "cover_is_dgm":
            continue
        try:
            oid = int(key)
        except (TypeError, ValueError):
            continue
        unders = [int(x) for x in (rec.get("under_oids") or [])]
        if not unders:
            continue
        out.append(
            {
                "cover_oid": oid,
                "under_oids": unders,
                "clear_height_m": float(rec.get("clear_height_m") or CLEAR_DEFAULT_M),
                "dz_under_m": rec.get("dz_under_m"),
            }
        )
    return out


def cover_bore_geoms(site: dict | None = None, under_oids: set[int] | None = None) -> list[dict]:
    """Cover ∩ lower-road shift buffers. Empty until centerline shift has run."""
    import geopandas as gpd
    import shapely
    from shapely.ops import unary_union

    recs = cover_records(site)
    if under_oids is not None:
        recs = [r for r in recs if under_oids.intersection(r["under_oids"])]
    if not recs:
        return []
    gpkg = processed_dir(site) / "centerline_shift_taper" / "centerline_shift.gpkg"
    if not gpkg.is_file():
        return []
    try:
        buf = gpd.read_file(gpkg, layer="chosen_buffer")
    except Exception:
        return []
    if buf.empty or "objectid" not in buf.columns:
        return []

    def _union(oid: int):
        sub = buf[buf.objectid == oid]
        polys = [g for g in sub.geometry if g is not None and not g.is_empty]
        if not polys:
            return None
        return shapely.make_valid(unary_union(polys))

    out = []
    for rec in recs:
        host = _union(rec["cover_oid"])
        if host is None:
            continue
        host = host.buffer(2.0)
        for uoid in rec["under_oids"]:
            if under_oids is not None and uoid not in under_oids:
                continue
            low = _union(uoid)
            if low is None:
                continue
            hit = shapely.make_valid(low.intersection(host))
            if hit.is_empty:
                continue
            out.append({**rec, "under_oid": uoid, "geometry": hit})
    return out


def cover_bore_union(site: dict | None = None, under_oids: set[int] | None = None):
    from shapely.ops import unary_union

    geoms = [b["geometry"] for b in cover_bore_geoms(site, under_oids)]
    if not geoms:
        return None
    return unary_union(geoms).buffer(0)


def bridge_xy_from_cache(site: dict) -> list[dict] | None:
    """Bridge centerlines from the flag. None when the site has not been classified."""
    flags = load_flags(site)
    if flags is None:
        return None
    proc = processed_dir(site)
    cache = proc / "gip_roads_beamng.json"
    if not cache.is_file():
        return None
    roads = json.loads(cache.read_text(encoding="utf-8"))
    if not isinstance(roads, dict):
        return None
    apply_flags(roads, flags)
    out: list[dict] = []
    for key, road in roads.items():
        if not road.get("bridge"):
            continue
        nodes = road.get("nodes") or []
        xy = [(float(n[0]), float(n[1])) for n in nodes if len(n) >= 2]
        if len(xy) < 2:
            continue
        _cum, length = _chain(nodes)
        out.append(
            {
                "name": str(road.get("kunstbauten") or road.get("name") or "bridge"),
                "objectid": road.get("objectid") or key,
                "str_code": road.get("str_code"),
                "length_m": round(length, 2),
                "xy": xy,
            }
        )
    return out


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    cache = proc / "gip_roads_beamng.json"
    if not cache.is_file():
        raise SystemExit(f"Missing {cache}")
    roads = json.loads(cache.read_text(encoding="utf-8"))
    flags = classify_roads(roads, site)
    dom_decks = promote_dom_decks(site, roads, flags)
    path = write_flags(site, flags)
    apply_flags(roads, flags)
    cache.write_text(json.dumps(roads, indent=2), encoding="utf-8")
    revoked = [k for k, r in flags.items() if r["reason"] == "no_opening"]
    covers = [k for k, r in flags.items() if r["reason"] == "cover_is_dgm"]
    overs = [k for k, r in flags.items() if r["reason"] == "routing_over"]
    promoted = [k for k, r in flags.items() if r["reason"] in {"opening", "road_under"}]
    decks = [k for k, r in flags.items() if r["reason"] == "dom_deck"]
    kept = [k for k, r in flags.items() if r["reason"] == "kept"]
    print(f"bridge flags -> {path}")
    print(
        f"kept={len(kept)} revoked={len(revoked)} "
        f"cover_is_dgm={len(covers)} routing_over={len(overs)} "
        f"dom_deck={len(decks)} promoted={len(promoted)}"
    )
    for label, keys in (
        ("revoked", revoked),
        ("cover", covers),
        ("over", overs),
        ("dom_deck", decks),
        ("promoted", promoted),
    ):
        for k in keys:
            rec = flags[k]
            extra = ""
            if rec.get("under_oids"):
                extra = f" under_oids={rec['under_oids']}"
            if rec.get("clear_height_m") is not None:
                extra += f" clear={rec['clear_height_m']} dz={rec.get('dz_under_m')}"
            if rec.get("dom_open_m") is not None:
                extra += f" dom_open={rec['dom_open_m']}"
            print(
                f"  {label} oid={k} {rec['objekt_gip']}->{rec['objekt']} "
                f"{rec.get('str_code') or ''} len={rec['length_m']} "
                f"dip={rec['dip_m']} under={rec['undercross']}{extra}"
            )


if __name__ == "__main__":
    main()
