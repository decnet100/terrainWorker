"""Decide which GIP pieces are bridges, from the heightmap rather than the label alone.

GIP ``S-AB`` / ``S-BB`` / ``S-LB`` and a Kunstbauten name containing "Brücke" are
claims. A claim is dropped when the piece does not sag below the chord between
its ends and no other road passes under it. A short named piece that does sag,
or that has a road under it, becomes a bridge even when GIP called it a normal
carriageway.

The result is ``gip_bridge_flags.json``. Road loading applies it to ``objekt``
and ``bridge``. Bridge generation reads ``bridge`` and ignores the raw label.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from site_coords import load_site, processed_dir

# Third letter B, and not a gallery (S-BG).
_GIP_BRIDGE_OBJEKT = frozenset({"S-AB", "S-BB", "S-LB"})
_BRIDGE_TO_ROAD = {"S-AB": "S-A", "S-BB": "S-B", "S-LB": "S-L"}
_ROAD_TO_BRIDGE = {v: k for k, v in _BRIDGE_TO_ROAD.items()}
_TUNNEL_OR_GALLERY = frozenset({"S-AT", "S-BT", "S-LT", "S-BG"})

# Below this, the deck chord and the heightmap agree: no opening under the piece.
DIP_REVOKE_M = 1.0
# A named carriageway this deep under its own chord is a span, not a grade.
DIP_PROMOTE_M = 2.5
# Other road this far below the piece counts as passing under, not a junction.
UNDERCROSS_M = 2.5
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


def undercross_count(key: str, road: dict, buckets: dict) -> int:
    """Other roads crossing the interior and sitting clearly lower."""
    nodes = road.get("nodes") or []
    if len(nodes) < 2:
        return 0
    cum, total = _chain(nodes)
    if total < 1.0:
        return 0
    end_keep = min(4.0, 0.25 * total)
    hits = 0
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
                    if z_here - z_other >= UNDERCROSS_M:
                        hits += 1
    return hits


def _corrected_objekt(obj: str, *, bridge: bool) -> str:
    if bridge:
        return _ROAD_TO_BRIDGE.get(obj, obj)
    return _BRIDGE_TO_ROAD.get(obj, obj)


def classify_roads(roads: dict) -> dict[str, dict]:
    buckets = _index_segments(roads)
    flags: dict[str, dict] = {}
    for key, road in roads.items():
        obj = gip_objekt(road)
        if obj in _TUNNEL_OR_GALLERY:
            continue
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        _cum, length = _chain(nodes)
        dip = chord_dip_m(nodes)
        claim = gip_claims_bridge(road)
        named = bool(str(road.get("str_code") or "").strip())
        stem = obj in _ROAD_TO_BRIDGE or obj in _GIP_BRIDGE_OBJEKT or obj in {"S-A", "S-B", "S-L"}
        short = PROMOTE_MIN_M <= length <= PROMOTE_MAX_M
        need_cross = claim or (named and stem and short and dip >= DIP_PROMOTE_M)
        crosses = undercross_count(str(key), road, buckets) if need_cross else 0
        if claim and dip < DIP_REVOKE_M and crosses == 0:
            bridge = False
            reason = "no_opening"
        elif (not claim) and named and stem and short and (dip >= DIP_PROMOTE_M or crosses > 0):
            bridge = True
            reason = "opening" if dip >= DIP_PROMOTE_M else "road_under"
        elif claim:
            bridge = True
            reason = "kept"
        else:
            continue
        new_obj = _corrected_objekt(obj, bridge=bridge)
        if bridge == claim and new_obj == obj and reason == "kept":
            # Still record kept bridges so the bridge pass can trust the file alone.
            pass
        flags[str(int(road.get("objectid") or key))] = {
            "bridge": bridge,
            "objekt": new_obj,
            "objekt_gip": obj,
            "dip_m": round(dip, 2),
            "undercross": crosses,
            "length_m": round(length, 1),
            "str_code": road.get("str_code"),
            "reason": reason,
        }
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
        n += 1
    return n


def write_flags(site: dict, flags: dict[str, dict]) -> Path:
    path = flags_path(site)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "dip_revoke_m": DIP_REVOKE_M,
        "dip_promote_m": DIP_PROMOTE_M,
        "undercross_m": UNDERCROSS_M,
        "flags": flags,
    }
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return path


def apply_stored_flags(site: dict, roads: dict) -> int:
    flags = load_flags(site)
    if not flags:
        return 0
    return apply_flags(roads, flags)


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
    flags = classify_roads(roads)
    path = write_flags(site, flags)
    apply_flags(roads, flags)
    cache.write_text(json.dumps(roads, indent=2), encoding="utf-8")
    revoked = [k for k, r in flags.items() if r["reason"] == "no_opening"]
    promoted = [k for k, r in flags.items() if r["reason"] in {"opening", "road_under"}]
    kept = [k for k, r in flags.items() if r["reason"] == "kept"]
    print(f"bridge flags -> {path}")
    print(f"kept={len(kept)} revoked={len(revoked)} promoted={len(promoted)}")
    for label, keys in (("revoked", revoked), ("promoted", promoted)):
        for k in keys:
            rec = flags[k]
            print(
                f"  {label} oid={k} {rec['objekt_gip']}->{rec['objekt']} "
                f"{rec.get('str_code') or ''} len={rec['length_m']} "
                f"dip={rec['dip_m']} under={rec['undercross']}"
            )


if __name__ == "__main__":
    main()
