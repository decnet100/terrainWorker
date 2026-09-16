"""Straßennetz centerlines for guardrails / decals (BeamNG XY).

Unlike GIP Verkehrswege (many OBJECTID fragments), Strassennetz is typically
one continuous Landesstraßen axis — often cleaner for lateral offsets.

When ``slice_by_gip`` is True, each GIP OBJECTID is projected onto the
Straßennetz polyline and yields one rail decision segment (geometry = SN,
id = GIP OBJECTID).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from gip_road_segments import is_gip_tunnel_segment, load_gip_road_segments
from road_width import default_carriageway_width_m
from site_coords import load_site, processed_dir


def load_strassennetz_roads_dict(site: dict | None = None) -> dict:
    """Load ``strassennetz_beamng.json`` as guardrail-style road dict.

    Keys are ``str(objectid)`` (or synthetic). Nodes keep BeamNG [x,y,z,w].
    """
    site = site or load_site()
    proc = processed_dir(site)
    path = proc / "strassennetz_beamng.json"
    if not path.is_file():
        raise SystemExit(
            f"Missing {path} — run: python tools/fetch_strassennetz.py"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    bng = site.get("beamng") or {}
    width_default = default_carriageway_width_m(bng)
    bridges_w = (bng.get("bridges") or {}).get("defaults") or {}
    if bridges_w.get("width_m") is not None:
        width_default = float(bridges_w["width_m"])

    out: dict = {}
    for key, road in data.items():
        nodes_in = road.get("nodes") or []
        if len(nodes_in) < 2:
            continue
        nodes = []
        for n in nodes_in:
            w = float(n[3]) if len(n) > 3 else width_default
            # Prefer configured carriageway width (SN cache may have stale 7.0).
            w = float(width_default)
            nodes.append(
                [float(n[0]), float(n[1]), float(n[2]), round(w, 2)]
            )
        oid = road.get("objectid")
        rid = str(int(oid)) if oid is not None else str(key)
        length = 0.0
        for a, b in zip(nodes, nodes[1:]):
            length += math.hypot(b[0] - a[0], b[1] - a[1])
        out[rid] = {
            "nodes": nodes,
            "highway": road.get("highway") or "primary",
            "name": road.get("name") or "strassennetz",
            "osm_id": int(oid) if oid is not None else key,
            "objectid": int(oid) if oid is not None else None,
            "osm_ids": [int(oid)] if oid is not None else [key],
            "str_code": road.get("str_code"),
            "source": "strassennetz",
            "length_m": round(length, 2),
            "lanes": float(bng.get("default_lanes") or 2),
        }
    if not out:
        raise SystemExit(f"No usable polylines in {path}")
    print(f"Straßennetz centerlines: {len(out)} piece(s) from {path.name}")
    return out


def _cum_xy(nodes: list[list[float]]) -> list[float]:
    cum = [0.0]
    for a, b in zip(nodes, nodes[1:]):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    return cum


def _project_s(nodes: list[list[float]], cum: list[float], x: float, y: float) -> float:
    best = None
    for i, (a, b) in enumerate(zip(nodes, nodes[1:])):
        ax, ay = a[0], a[1]
        bx, by = b[0], b[1]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        if seg2 < 1e-12:
            t = 0.0
            d2 = (x - ax) ** 2 + (y - ay) ** 2
            s = cum[i]
        else:
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / seg2))
            px, py = ax + t * dx, ay + t * dy
            d2 = (x - px) ** 2 + (y - py) ** 2
            s = cum[i] + t * math.sqrt(seg2)
        if best is None or d2 < best[0]:
            best = (d2, s)
    assert best is not None
    return float(best[1])


def _slice_nodes(
    nodes: list[list[float]], cum: list[float], s0: float, s1: float
) -> list[list[float]]:
    if s1 < s0:
        s0, s1 = s1, s0
    total = cum[-1]
    s0 = max(0.0, min(total, s0))
    s1 = max(0.0, min(total, s1))
    if s1 - s0 < 1.0:
        return []

    def sample(s: float) -> list[float]:
        s = max(0.0, min(total, s))
        for i in range(len(nodes) - 1):
            if cum[i + 1] + 1e-9 >= s:
                a, b = nodes[i], nodes[i + 1]
                seglen = cum[i + 1] - cum[i]
                t = 0.0 if seglen < 1e-9 else (s - cum[i]) / seglen
                return [
                    a[0] + t * (b[0] - a[0]),
                    a[1] + t * (b[1] - a[1]),
                    a[2] + t * (b[2] - a[2]),
                    a[3] + t * (b[3] - a[3]),
                ]
        return list(nodes[-1])

    out = [sample(s0)]
    for i, s in enumerate(cum):
        if s0 < s < s1:
            out.append(list(nodes[i]))
    end = sample(s1)
    if math.hypot(end[0] - out[-1][0], end[1] - out[-1][1]) > 0.05:
        out.append(end)
    return out if len(out) >= 2 else []


def load_strassennetz_sliced_by_gip(site: dict | None = None) -> dict:
    """SN geometry cut into GIP-OBJECTID spans (decision units keep GIP ids)."""
    site = site or load_site()
    sn_roads = load_strassennetz_roads_dict(site)
    # Prefer longest SN piece as master axis
    master = max(sn_roads.values(), key=lambda r: float(r.get("length_m") or 0.0))
    sn_nodes = master["nodes"]
    cum = _cum_xy(sn_nodes)

    try:
        gip = load_gip_road_segments(site)
    except SystemExit as ex:
        print(f"GIP slice unavailable ({ex}) — using whole Straßennetz")
        return sn_roads

    out: dict = {}
    skipped = 0
    for oid_key, grow in gip.items():
        gnodes = grow.get("nodes") or []
        if len(gnodes) < 2:
            skipped += 1
            continue
        if is_gip_tunnel_segment(grow):
            # Keep entry marked? Caller skips tunnels; still emit for logging.
            pass
        s0 = _project_s(sn_nodes, cum, gnodes[0][0], gnodes[0][1])
        s1 = _project_s(sn_nodes, cum, gnodes[-1][0], gnodes[-1][1])
        # Also sample mid for better span if ends project poorly
        mid = gnodes[len(gnodes) // 2]
        sm = _project_s(sn_nodes, cum, mid[0], mid[1])
        lo, hi = min(s0, s1, sm), max(s0, s1, sm)
        # Expand slightly toward ends of GIP chord
        lo, hi = min(s0, s1), max(s0, s1)
        if hi - lo < 2.0:
            lo, hi = min(s0, sm, s1), max(s0, sm, s1)
        sliced = _slice_nodes(sn_nodes, cum, lo, hi)
        if len(sliced) < 2:
            skipped += 1
            continue
        length = 0.0
        for a, b in zip(sliced, sliced[1:]):
            length += math.hypot(b[0] - a[0], b[1] - a[1])
        oid = grow.get("objectid")
        out[str(oid)] = {
            "nodes": [[round(v, 3) if i < 3 else round(v, 2) for i, v in enumerate(n)] for n in sliced],
            "highway": "primary",
            "name": grow.get("name") or master.get("name"),
            "osm_id": oid,
            "objectid": oid,
            "osm_ids": [oid],
            "str_code": grow.get("str_code") or master.get("str_code"),
            "kunstbauten": grow.get("kunstbauten"),
            "objekt": grow.get("objekt"),
            "source": "strassennetz+gip",
            "length_m": round(length, 2),
            "lanes": grow.get("lanes") or master.get("lanes"),
            "sn_s0": round(lo, 2),
            "sn_s1": round(hi, 2),
        }

    proc = processed_dir(site)
    cache = proc / "strassennetz_gip_slices_beamng.json"
    cache.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(
        f"Straßennetz∥GIP slices: {len(out)} segments "
        f"(skipped={skipped}) -> {cache.name}"
    )
    return out if out else sn_roads


def load_strassennetz_polylines_for_decals(site: dict | None = None) -> list[dict]:
    """Format expected by ``build_decal_roads`` / ``load_road_polylines``."""
    roads = load_strassennetz_roads_dict(site)
    out: list[dict] = []
    for key, road in roads.items():
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        pts = [
            (float(n[0]), float(n[1]), float(n[2]), float(n[3])) for n in nodes
        ]
        cum = [0.0]
        for a, b in zip(pts, pts[1:]):
            cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        out.append(
            {
                "id": key,
                "name": road.get("name"),
                "highway": road.get("highway") or "primary",
                "pts": pts,
                "cum": cum,
                "length": cum[-1],
            }
        )
    return out
