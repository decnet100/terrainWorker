"""Load GIP Verkehrswege polylines as per-OBJECTID road segments (BeamNG XY)."""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from PIL import Image
from pyproj import Transformer

from build_bridges import find_gip_geojson
from road_width import default_carriageway_width_m
from site_coords import SiteCoords, load_site, processed_dir

# GIP OBJEKT codes that are not through-carriageway (no auto rails).
_NO_RAIL_OBJEKT = frozenset(
    {
        "S-AR",  # Autobahn-Rampe / Abbiegespur
        "S-AP",  # Autobahn-Anschluss/Parkstreifen
        "S-BP",  # Parkplatz
        "S-BR",  # Bundesstraßen-Rampe
        "S-G",  # örtliches Straßennetz
        "S-F",  # Forstweg
        "S-FRW",  # Rad-/Fuß-/Wanderweg
        "S-GW",  # Wirtschaftsweg
        "S-STRAIL",
    }
)

# One-lane strips subordinate to S-A / S-AT+S-AB or S-B / S-BT+S-BB.
_SUBORDINATE_LANE_OBJEKT = frozenset({"S-AR", "S-AP", "S-BR", "S-BP"})


def _coords_walk(coords, out: list) -> None:
    if coords is None:
        return
    if isinstance(coords[0], (int, float)):
        out.append(coords)
        return
    for c in coords:
        _coords_walk(c, out)


def _load_z_at(site: dict):
    proc = processed_dir(site)
    bng = site.get("beamng") or {}
    size = int(bng.get("mask_size") or 512)
    hm_path = proc / f"heightmap_{size}.png"
    meta_path = proc / "heightmap_meta.json"
    if not hm_path.is_file() or not meta_path.is_file():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    max_h = float(meta.get("max_height_m", bng.get("max_height_m", 254.75)))
    hm = np.asarray(Image.open(hm_path))
    if hm.dtype != np.uint16:
        hm = hm.astype(np.uint16)
    n = int(hm.shape[0])
    extent = float(meta.get("terrain_extent_m") or size)

    def z_at(bx: float, by: float) -> float:
        # Same convention as guardrails / strassennetz: bilinear would be nicer,
        # nearest is enough for segment Z.
        c = int(max(0, min(n - 1, round(bx / extent * (n - 1)))))
        r = int(max(0, min(n - 1, round((1.0 - by / extent) * (n - 1)))))
        return float(hm[r, c]) / 65535.0 * max_h

    return z_at


def gip_skip_guardrails(road: dict) -> bool:
    """True = no posts/sections (minor roads, parking bays, ramps)."""
    code = str(road.get("str_code") or "").strip()
    objekt = str(road.get("objekt") or "").upper().strip()
    if not code:
        return True
    return objekt in _NO_RAIL_OBJEKT


def gip_is_named_road(road: dict) -> bool:
    """True if the piece gets a DecalRoad in ``gip_decals: named``.

    Any non-empty ``STR_CODE`` counts (A12, B180, L…). Autobahn surface
    and ramps (``S-A`` / ``S-AR``) stay named even when STR_CODE is blank.
    ``S-A`` is not an off-switch; closed bores are ``S-AT`` / Kunstbauten.
    """
    if str(road.get("str_code") or "").strip():
        return True
    return gip_objekt(road) in {"S-A", "S-AR"}


def gip_objekt(road: dict) -> str:
    return str(road.get("objekt") or road.get("OBJEKT") or "").upper().strip()


def gip_is_subordinate_lane(road: dict) -> bool:
    """Abbiege-/Anschlussstreifen: eine Spur, unter der Stammstrecke."""
    return gip_objekt(road) in _SUBORDINATE_LANE_OBJEKT


def _yaml_str_list(val) -> list[str]:
    if val is None or val is False:
        return []
    if isinstance(val, (list, tuple)):
        return [str(x).strip() for x in val if str(x).strip()]
    s = str(val).strip()
    return [s] if s else []


def _yaml_int_set(val) -> set[int]:
    out: set[int] = set()
    if val is None or val is False:
        return out
    raw = val if isinstance(val, (list, tuple)) else [val]
    for x in raw:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out


def parse_side_cut_preserve(*parts: dict | None) -> dict:
    """Union of ``side_cut_preserve`` YAML dicts (objekt + objectid).

    Missing key → default ``S-AR`` / ``S-AP`` / ``S-BR`` / ``S-BP``.
    An explicit empty dict preserves nothing (side-cut through all roads).
    """
    found = False
    objekts: set[str] = set()
    oids: set[int] = set()
    pad = 0.5
    for part in parts:
        if not isinstance(part, dict):
            continue
        found = True
        for x in _yaml_str_list(part.get("objekt") or part.get("objekt_types")):
            objekts.add(x.upper())
        oids |= _yaml_int_set(
            part.get("objectid") if part.get("objectid") is not None else part.get("objectids")
        )
        if part.get("pad_m") is not None:
            pad = float(part["pad_m"])
    if not found:
        objekts = set(_SUBORDINATE_LANE_OBJEKT)
    return {"objekt": objekts, "objectid": oids, "pad_m": pad}


def side_cut_preserve_axes(roads: dict, spec: dict) -> list[tuple[list[tuple[float, float]], float]]:
    """GIP polylines that must not receive MeshRoad side-cut."""
    want_obj = {str(x).upper() for x in (spec.get("objekt") or ())}
    want_oid = {int(x) for x in (spec.get("objectid") or ())}
    pad = float(spec.get("pad_m") or 0.0)
    if not want_obj and not want_oid:
        return []
    axes: list[tuple[list[tuple[float, float]], float]] = []
    for road in (roads or {}).values():
        if not isinstance(road, dict):
            continue
        hit = False
        oid = road.get("objectid")
        if oid is not None:
            try:
                if int(oid) in want_oid:
                    hit = True
            except (TypeError, ValueError):
                pass
        if not hit and gip_objekt(road) in want_obj:
            hit = True
        if not hit:
            continue
        nodes = road.get("nodes") or []
        xy: list[tuple[float, float]] = []
        widths: list[float] = []
        for n in nodes:
            if len(n) < 2:
                continue
            xy.append((float(n[0]), float(n[1])))
            if len(n) >= 4:
                widths.append(float(n[3]))
        if len(xy) < 2:
            continue
        half = 0.5 * (max(widths) if widths else 3.75) + pad
        axes.append((xy, half))
    return axes


# Paths: terrain gravel, no road-bed (must not lift a junction onto the main road).
_GRAVEL_NO_BED_OBJEKT = frozenset({"S-F", "S-FRW", "S-STRAIL", "S-GW"})


def gip_is_forest_track(road: dict) -> bool:
    """GIP ``OBJEKT=S-F`` (forstwirtschaftlicher Weg)."""
    return gip_objekt(road) == "S-F"


def gip_is_gravel_path(road: dict) -> bool:
    """Forstweg / Wirtschaftsweg / Fuß- und Radweg: Kies, ohne Heightmap-Conform."""
    return gip_objekt(road) in _GRAVEL_NO_BED_OBJEKT


def not_tunnel_objectids(site: dict | None = None) -> set[int]:
    """GIP OBJECTIDs that stay surface even if the road name says Tunnel."""
    site = site or load_site()
    raw = ((site.get("beamng") or {}).get("roads") or {}).get("not_tunnel_objectids") or []
    out: set[int] = set()
    for x in raw:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out


def load_unify_specs(site: dict | None = None) -> list[dict]:
    """Named virtual axes from ``beamng.roads.unify``."""
    site = site or load_site()
    raw = ((site.get("beamng") or {}).get("roads") or {}).get("unify") or []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        uid = str(entry.get("id") or "").strip()
        oids = []
        for x in entry.get("objectids") or []:
            try:
                oids.append(int(x))
            except (TypeError, ValueError):
                continue
        if not uid or len(oids) < 2:
            continue
        replaces = entry.get("replaces")
        if replaces is None:
            replaces = ["galleries", "decals", "guardrails", "road_bed"]
        out.append(
            {
                "id": uid,
                "objectids": oids,
                "mode": str(entry.get("mode") or "mean_axis").lower().strip(),
                "width_m": entry.get("width_m"),
                "step_m": float(entry.get("step_m") or 2.0),
                "kind": str(entry.get("kind") or "tunnel").lower().strip(),
                "replaces": [str(x).lower() for x in replaces],
            }
        )
    return out


def load_follow_parent_specs(site: dict | None = None) -> list[dict]:
    """YAML ``beamng.roads.follow_parent``: child Z tracks a parent axis."""
    site = site or load_site()
    raw = ((site.get("beamng") or {}).get("roads") or {}).get("follow_parent") or []
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        match = entry.get("match") or {}
        oids: list[int] = []
        raw_oids = match.get("objectid")
        if raw_oids is None:
            raw_oids = entry.get("objectid")
        if raw_oids is not None:
            if not isinstance(raw_oids, (list, tuple)):
                raw_oids = [raw_oids]
            for x in raw_oids:
                try:
                    oids.append(int(x))
                except (TypeError, ValueError):
                    continue
        parent_oids: list[int] = []
        for x in entry.get("parent_objectids") or []:
            try:
                parent_oids.append(int(x))
            except (TypeError, ValueError):
                continue
        parent = entry.get("parent")
        if not oids:
            continue
        if parent is None and not parent_oids:
            continue
        out.append(
            {
                "objectids": oids,
                "parent": str(parent).strip() if parent is not None else "",
                "parent_objectids": parent_oids,
                "hold_m": float(entry.get("hold_m") or 0.0),
                "blend_m": float(
                    entry["blend_m"] if entry.get("blend_m") is not None else 30.0
                ),
                "join_max_m": float(
                    entry["join_max_m"]
                    if entry.get("join_max_m") is not None
                    else 40.0
                ),
                "max_raise_m": float(
                    entry["max_raise_m"]
                    if entry.get("max_raise_m") is not None
                    else 8.0
                ),
                "max_cut_m": float(
                    entry["max_cut_m"] if entry.get("max_cut_m") is not None else 8.0
                ),
                "sink_m": entry.get("sink_m"),
            }
        )
    return out


def _smoothstep01(t: float) -> float:
    t = max(0.0, min(1.0, float(t)))
    return t * t * (3.0 - 2.0 * t)


def _nearest_on_poly_xyz(
    px: float, py: float, xyz: list[tuple[float, float, float]]
) -> tuple[float, float] | None:
    """XY-Abstand und Z des nächsten Punkts auf der 3D-Polylinie."""
    best: tuple[float, float] | None = None
    for a, b in zip(xyz, xyz[1:]):
        ax, ay, az = a
        bx, by, bz = b
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        if seg2 < 1e-12:
            t = 0.0
            qx, qy, qz = ax, ay, az
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
            qx = ax + t * dx
            qy = ay + t * dy
            qz = az + t * (bz - az)
        d = math.hypot(px - qx, py - qy)
        if best is None or d < best[0]:
            best = (d, float(qz))
    return best


def _parent_xyz_from_spec(
    spec: dict, roads: dict, site: dict
) -> list[tuple[float, float, float]]:
    """Unify-Mittelachse oder GIP-OBJECTID(s) als (x, y, z)."""
    uid = str(spec.get("parent") or "").strip()
    for us in load_unify_specs(site):
        if us["id"] != uid:
            continue
        members = []
        for oid in us["objectids"]:
            road = roads.get(str(oid))
            if road and len(road.get("nodes") or []) >= 2:
                members.append(road)
        if not members:
            return []
        axis_members = [
            m for m in members if float(m.get("length_m") or 0.0) >= 20.0
        ]
        if len(axis_members) < 2:
            axis_members = members
        if len(axis_members) >= 2:
            polys = [
                [(float(n[0]), float(n[1])) for n in (m.get("nodes") or [])]
                for m in axis_members
            ]
            xy = mean_axis_xy(polys, step_m=us["step_m"])
            if len(xy) < 2:
                return []
            out: list[tuple[float, float, float]] = []
            for i, (x, y) in enumerate(xy):
                frac = i / (len(xy) - 1)
                z = sum(_sample_nodes_z(m["nodes"], frac) for m in axis_members) / len(
                    axis_members
                )
                out.append((float(x), float(y), float(z)))
            return out
        nodes = members[0].get("nodes") or []
        return [(float(n[0]), float(n[1]), float(n[2])) for n in nodes if len(n) >= 3]
    oids = list(spec.get("parent_objectids") or [])
    if uid:
        try:
            oids.append(int(uid))
        except (TypeError, ValueError):
            pass
    xyz: list[tuple[float, float, float]] = []
    for oid in oids:
        road = roads.get(str(oid))
        if not road:
            continue
        for n in road.get("nodes") or []:
            if len(n) >= 3:
                xyz.append((float(n[0]), float(n[1]), float(n[2])))
    return xyz


def apply_follow_parent_z(roads: dict, site: dict) -> None:
    """Child-Node-Z: hold_m an der Eltern-Achse, dann Blend zurück aufs DGM.

    Strecke wird vom Treffpunkt (näheres Child-Ende zur Eltern-Polylinie)
    entlang der Rampe gemessen — nicht als Radius um den Knoten, damit der
    Rest der Rampe unangetastet bleibt.
    """
    for spec in load_follow_parent_specs(site):
        parent = _parent_xyz_from_spec(spec, roads, site)
        if len(parent) < 2:
            print(
                f"follow_parent: no parent axis for {spec.get('parent') or spec.get('parent_objectids')}"
            )
            continue
        hold = float(spec["hold_m"])
        blend = max(0.0, float(spec["blend_m"]))
        join_max = float(spec["join_max_m"])
        for oid in spec["objectids"]:
            road = roads.get(str(oid))
            if not road:
                print(f"follow_parent: missing oid {oid}")
                continue
            nodes = road.get("nodes") or []
            if len(nodes) < 2:
                continue
            d0 = _nearest_on_poly_xyz(float(nodes[0][0]), float(nodes[0][1]), parent)
            d1 = _nearest_on_poly_xyz(float(nodes[-1][0]), float(nodes[-1][1]), parent)
            if d0 is None or d1 is None:
                continue
            from_start = d0[0] <= d1[0]
            join_d = min(d0[0], d1[0])
            if join_d > join_max:
                print(
                    f"follow_parent oid={oid}: join {join_d:.1f}m > {join_max}m, skipped"
                )
                continue
            cum = [0.0]
            for a, b in zip(nodes, nodes[1:]):
                cum.append(
                    cum[-1]
                    + math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
                )
            total = cum[-1]
            n_hold = 0
            n_blend = 0
            dz_abs: list[float] = []
            for i, node in enumerate(nodes):
                s = cum[i] if from_start else (total - cum[i])
                hit = _nearest_on_poly_xyz(float(node[0]), float(node[1]), parent)
                z_p = float(hit[1]) if hit else float(node[2])
                z_d = float(node[2])
                if s <= hold:
                    z = z_p
                    n_hold += 1
                elif blend > 0 and s <= hold + blend:
                    t = _smoothstep01((s - hold) / blend)
                    z = z_p * (1.0 - t) + z_d * t
                    n_blend += 1
                else:
                    z = z_d
                dz_abs.append(abs(z - z_d))
                node[2] = round(float(z), 3)
            road["follow_parent"] = {
                "parent": spec.get("parent") or spec.get("parent_objectids"),
                "hold_m": hold,
                "blend_m": blend,
                "max_raise_m": spec["max_raise_m"],
                "max_cut_m": spec["max_cut_m"],
                "sink_m": spec.get("sink_m"),
                "join_from_start": from_start,
                "join_dist_m": round(join_d, 3),
            }
            print(
                f"follow_parent oid={oid}: hold={hold:.0f}m blend={blend:.0f}m "
                f"from_{'start' if from_start else 'end'} join={join_d:.2f}m "
                f"nodes_hold={n_hold} blend={n_blend} "
                f"|dz|max={max(dz_abs) if dz_abs else 0:.2f}m"
            )


def unify_replaced_oids(site: dict | None = None, consumer: str = "") -> set[int]:
    want = str(consumer or "").lower().strip()
    out: set[int] = set()
    for spec in load_unify_specs(site):
        if want and want not in spec["replaces"]:
            continue
        out.update(spec["objectids"])
    return out


def _poly_len_xy(xy: list[tuple[float, float]]) -> float:
    total = 0.0
    for a, b in zip(xy, xy[1:]):
        total += math.hypot(b[0] - a[0], b[1] - a[1])
    return total


def _poly_cum_xy(xy: list[tuple[float, float]]) -> list[float]:
    cum = [0.0]
    for a, b in zip(xy, xy[1:]):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    return cum


def _interp_xy(xy: list[tuple[float, float]], cum: list[float], s: float) -> tuple[float, float]:
    if s <= 0:
        return xy[0]
    if s >= cum[-1]:
        return xy[-1]
    for i in range(1, len(cum)):
        if cum[i] < s:
            continue
        span = max(cum[i] - cum[i - 1], 1e-9)
        t = (s - cum[i - 1]) / span
        ax, ay = xy[i - 1]
        bx, by = xy[i]
        return (ax + t * (bx - ax), ay + t * (by - ay))
    return xy[-1]


def mean_axis_xy(
    polys: list[list[tuple[float, float]]],
    *,
    step_m: float = 2.0,
) -> list[tuple[float, float]]:
    """Average parallel polylines into one axis (same direction, resampled)."""
    if not polys:
        return []
    usable = [list(p) for p in polys if len(p) >= 2]
    if not usable:
        return []
    if len(usable) == 1:
        return usable[0]
    ref = usable[0]
    oriented = [ref]
    for poly in usable[1:]:
        d_start = math.hypot(poly[0][0] - ref[0][0], poly[0][1] - ref[0][1])
        d_end = math.hypot(poly[-1][0] - ref[0][0], poly[-1][1] - ref[0][1])
        oriented.append(list(reversed(poly)) if d_end < d_start else poly)
    target = max(_poly_len_xy(p) for p in oriented)
    n = max(2, int(round(target / max(float(step_m), 0.25))) + 1)
    cums = [_poly_cum_xy(p) for p in oriented]
    out: list[tuple[float, float]] = []
    for i in range(n):
        frac = i / (n - 1)
        xs = 0.0
        ys = 0.0
        for poly, cum in zip(oriented, cums):
            x, y = _interp_xy(poly, cum, frac * cum[-1])
            xs += x
            ys += y
        k = float(len(oriented))
        out.append((xs / k, ys / k))
    return out


def road_from_unify_feature(
    feat: dict,
    *,
    width_m: float,
    z_at=None,
) -> dict:
    """Hermite/spline road dict whose axis *is* the unified polyline."""
    pts: list[tuple[float, float, float, float]] = []
    for x, y in feat.get("xy") or []:
        z = float(z_at(x, y)) if z_at else 0.0
        pts.append((float(x), float(y), z, float(width_m)))
    if len(pts) < 2:
        raise SystemExit(f"unify {feat.get('unify_id')}: not enough points")
    cum = [0.0]
    for a, b in zip(pts, pts[1:]):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    uid = str(feat.get("unify_id") or "axis")
    return {
        "id": f"unify_{uid}",
        "pts": pts,
        "cum": cum,
        "length": cum[-1],
        "unify_id": uid,
        "objectids": list(feat.get("merged_from") or []),
        "corridor_kind": "unify",
    }


def resample_xy(
    xy: list[tuple[float, float]],
    step_m: float = 2.0,
) -> list[tuple[float, float]]:
    """Even spacing along a polyline (keeps the last point)."""
    if len(xy) < 2:
        return list(xy)
    cum = _poly_cum_xy(xy)
    length = float(cum[-1])
    if length < 1e-6:
        return [xy[0], xy[-1]]
    step = max(float(step_m), 0.25)
    out: list[tuple[float, float]] = []
    s = 0.0
    while s <= length + 1e-9:
        out.append(_interp_xy(xy, cum, s))
        s += step
    last = xy[-1]
    if math.hypot(out[-1][0] - last[0], out[-1][1] - last[1]) > 0.05:
        out.append(last)
    return out


def load_unify_axis_xy(
    site: dict | None,
    unify_id: str,
    *,
    min_len_m: float = 20.0,
) -> tuple[list[tuple[float, float]], dict]:
    """Mean axis of a named ``beamng.roads.unify`` entry (GIP members)."""
    uid = str(unify_id or "").strip()
    spec = next((s for s in load_unify_specs(site) if s["id"] == uid), None)
    if spec is None:
        raise SystemExit(f"unify {uid!r}: unknown id")
    roads = load_gip_road_segments(site or load_site())
    members: list[dict] = []
    for oid in spec["objectids"]:
        road = roads.get(str(oid))
        if road and len(road.get("nodes") or []) >= 2:
            members.append(road)
    if not members:
        raise SystemExit(
            f"unify {uid}: no in-map GIP pieces {spec['objectids']}"
        )
    long_enough = [
        m for m in members if float(m.get("length_m") or 0.0) >= float(min_len_m)
    ]
    use = long_enough if len(long_enough) >= 1 else members
    polys = [
        [(float(n[0]), float(n[1])) for n in (m.get("nodes") or [])]
        for m in use
    ]
    if spec["mode"] != "mean_axis":
        raise SystemExit(f"unify {uid}: unknown mode {spec['mode']!r}")
    if len(polys) == 1:
        xy = list(polys[0])
    else:
        xy = mean_axis_xy(polys, step_m=spec["step_m"])
    if len(xy) < 2:
        raise SystemExit(f"unify {uid}: axis has no line")
    return xy, spec


def apply_gallery_unify(feats: list[dict], site: dict | None = None) -> list[dict]:
    """Replace listed GIP halves with one named mean-axis feature."""
    specs = [
        s for s in load_unify_specs(site) if "galleries" in s["replaces"]
    ]
    if not specs:
        return feats

    def feat_oids(feat: dict) -> set[int]:
        ids: set[int] = set()
        if feat.get("objectid") is not None:
            ids.add(int(feat["objectid"]))
        for x in feat.get("merged_from") or []:
            ids.add(int(x))
        return ids

    by_oid: dict[int, dict] = {}
    for feat in feats:
        for oid in feat_oids(feat):
            by_oid[oid] = feat

    consumed: set[int] = set()
    extra: list[dict] = []
    for spec in specs:
        members = [by_oid[i] for i in spec["objectids"] if i in by_oid]
        if len(members) < 2:
            print(
                f"unify {spec['id']}: need two in-map GIP pieces, "
                f"got {len(members)} {spec['objectids']}"
            )
            continue
        polys = [list(m["xy"]) for m in members]
        if spec["mode"] != "mean_axis":
            raise SystemExit(f"unify {spec['id']}: unknown mode {spec['mode']!r}")
        xy = mean_axis_xy(polys, step_m=spec["step_m"])
        if len(xy) < 2:
            print(f"unify {spec['id']}: mean_axis produced no line")
            continue
        consumed.update(spec["objectids"])
        span = _poly_len_xy(xy)
        extra.append(
            {
                "kind": spec["kind"],
                "name": spec["id"],
                "unify_id": spec["id"],
                "objectid": spec["objectids"][0],
                "merged_from": list(spec["objectids"]),
                "str_code": members[0].get("str_code"),
                "length_m": span,
                "xy": xy,
            }
        )
        print(
            f"unify {spec['id']}: mean_axis {spec['objectids']} "
            f"-> {span:.1f}m points={len(xy)}"
        )

    kept = [f for f in feats if not (feat_oids(f) & consumed)]
    return extra + kept


def is_gip_open_gallery(road: dict) -> bool:
    """Open gallery (S-BG / Kunstbauten), not a closed tunnel bore."""
    if gip_objekt(road) == "S-BG":
        return True
    kunst = str(road.get("kunstbauten") or "").lower()
    return "galerie" in kunst and "tunnel" not in kunst


def gip_is_tunnel_bore(road: dict, site: dict | None = None) -> bool:
    """Closed tunnel tube (OBJEKT / Kunstbauten). Road title is not enough."""
    return is_gip_tunnel_segment(road, site) and not is_gip_open_gallery(road)


def gip_skip_surface_paint(road: dict, site: dict | None = None) -> bool:
    """No terrain asphalt: gravel paths stay raw, closed tunnel bores stay mountain."""
    return gip_is_gravel_path(road) or gip_is_tunnel_bore(road, site)


def gip_skip_road_bed(road: dict) -> bool:
    """Forest, farm, foot/cycle paths and tunnel bores stay on raw DGM."""
    return gip_skip_surface_paint(road)


def gip_is_bundesstrasse(code: str) -> bool:
    c = str(code or "").strip()
    return len(c) >= 2 and c[0] in "Bb" and c[1].isdigit()


def gip_str_code_matches(code: str, pattern: str) -> bool:
    """Exact ``STR_CODE`` or a single trailing glob (``L*`` → Landesstraßen)."""
    code = str(code or "").strip()
    pat = str(pattern or "").strip()
    if not code or not pat:
        return False
    if pat.endswith("*") and "*" not in pat[:-1]:
        prefix = pat[:-1]
        return bool(prefix) and code.startswith(prefix)
    return code == pat


def _width_by_str_code(site: dict) -> dict[str, float]:
    """YAML ``beamng.roads.width_by_str_code`` (node metres, before width_scale)."""
    raw = ((site.get("beamng") or {}).get("roads") or {}).get("width_by_str_code") or {}
    out: dict[str, float] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if v is None:
            continue
        code = str(k).strip()
        if not code:
            continue
        try:
            out[code] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _width_for_str_code(code: str, by_code: dict[str, float] | None) -> float | None:
    """Exact key first, then longest matching ``L*``-style prefix."""
    if not by_code:
        return None
    code = str(code or "").strip()
    if not code:
        return None
    if code in by_code:
        return float(by_code[code])
    hits: list[tuple[int, float]] = []
    for pat, w in by_code.items():
        if gip_str_code_matches(code, pat):
            hits.append((len(str(pat).rstrip("*")), float(w)))
    if not hits:
        return None
    hits.sort(reverse=True)
    return hits[0][1]


def _lanes_by_str_code(site: dict) -> dict[str, float]:
    raw = ((site.get("beamng") or {}).get("roads") or {}).get("lanes_by_str_code") or {}
    out: dict[str, float] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if v is None:
            continue
        code = str(k).strip()
        if not code:
            continue
        try:
            out[code] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _declared_lanes(
    code: str, props: dict, by_lanes: dict[str, float] | None
) -> float | None:
    """YAML ``lanes_by_str_code`` or a GIP lane field if the service ever sends one."""
    for key in ("LANES", "FAHRSTREIFEN", "STREIFEN", "ANZAHL_FS"):
        raw = props.get(key)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return _width_for_str_code(code, by_lanes)


def _gip_segment_width_m(
    props: dict,
    default_m: float,
    *,
    by_code: dict[str, float] | None = None,
    by_lanes: dict[str, float] | None = None,
    lane_width_m: float = 3.75,
) -> float:
    """Objekt / STR_CODE defaults; YAML ``width_by_str_code`` wins on named roads."""
    default_m = float(default_m)
    objekt = str(props.get("OBJEKT") or "").upper().strip()
    code = str(props.get("STR_CODE") or "").strip()
    if objekt in ("S-FRW", "S-STRAIL"):
        return 2.5
    if objekt in ("S-F", "S-GW"):
        return 4.0
    if objekt == "S-G":
        return 5.0
    if objekt in _SUBORDINATE_LANE_OBJEKT:
        return float(lane_width_m)
    if not code:
        return 5.5
    w = _width_for_str_code(code, by_code)
    if w is not None:
        return w
    if gip_is_bundesstrasse(code):
        lanes = _declared_lanes(code, props, by_lanes)
        if lanes is not None and lanes >= 3:
            return float(lanes) * float(lane_width_m)
        return 8.0
    return default_m


def load_gip_road_segments(site: dict, *, width_m: float | None = None) -> dict:
    """One entry per GIP OBJECTID clipped to the site bbox.

    Returns dict keyed by ``str(objectid)`` with ``nodes`` ``[x,y,z,width]``,
    ``objectid``, ``name``, ``highway``, ``kunstbauten``, ``objekt``.
    """
    sc = SiteCoords(site)
    path = find_gip_geojson(site)
    data = json.loads(path.read_text(encoding="utf-8"))
    feats = data.get("features") or []
    to_site = Transformer.from_crs("EPSG:4326", sc.crs, always_xy=True)
    z_at = _load_z_at(site)
    bng = site.get("beamng") or {}
    if width_m is None:
        bridges_w = (bng.get("bridges") or {}).get("defaults") or {}
        if bridges_w.get("width_m") is not None:
            width_m = float(bridges_w["width_m"])
        else:
            width_m = default_carriageway_width_m(bng)
    width_m = float(width_m)
    by_code = _width_by_str_code(site)
    by_lanes = _lanes_by_str_code(site)
    lane_width_m = float(bng.get("lane_width_m") or 3.75)

    xmin, ymin = sc.xmin, sc.ymin
    xmax, ymax = sc.xmax, sc.ymax
    roads: dict = {}
    skipped_short = 0
    str_counts: dict[str, int] = {}
    for f in feats:
        props = f.get("properties") or {}
        oid = props.get("OBJECTID")
        if oid is None:
            continue
        pts_ll: list = []
        _coords_walk((f.get("geometry") or {}).get("coordinates"), pts_ll)
        xy_site: list[tuple[float, float]] = []
        for p in pts_ll:
            if len(p) < 2:
                continue
            x, y = to_site.transform(float(p[0]), float(p[1]))
            xy_site.append((x, y))

        pieces: list[list[tuple[float, float]]] = []
        cur: list[tuple[float, float]] = []
        for x, y in xy_site:
            if xmin <= x <= xmax and ymin <= y <= ymax:
                cur.append((x, y))
            elif cur:
                if len(cur) >= 2:
                    pieces.append(cur)
                cur = []
        if len(cur) >= 2:
            pieces.append(cur)
        if not pieces:
            continue

        # Prefer longest in-bbox piece for this OBJECTID
        piece = max(
            pieces,
            key=lambda poly: sum(
                math.hypot(poly[i + 1][0] - poly[i][0], poly[i + 1][1] - poly[i][1])
                for i in range(len(poly) - 1)
            ),
        )
        w_seg = _gip_segment_width_m(
            props,
            width_m,
            by_code=by_code,
            by_lanes=by_lanes,
            lane_width_m=lane_width_m,
        )
        nodes = []
        for x, y in piece:
            bx, by = sc.crs_to_beamng(x, y)
            z = float(z_at(bx, by)) if z_at else 0.0
            nodes.append([round(bx, 3), round(by, 3), round(z, 3), round(w_seg, 2)])
        if len(nodes) < 2:
            skipped_short += 1
            continue
        length = 0.0
        for a, b in zip(nodes, nodes[1:]):
            length += math.hypot(b[0] - a[0], b[1] - a[1])
        if length < 1.0:
            skipped_short += 1
            continue
        name = (
            props.get("STRNAME")
            or props.get("OBJEKTBEZEICHNUNG")
            or props.get("STR_CODE")
            or f"gip_{oid}"
        )
        roads[str(int(oid))] = {
            "nodes": nodes,
            "highway": "primary",
            "name": name,
            "osm_id": int(oid),
            "objectid": int(oid),
            "osm_ids": [int(oid)],
            "str_code": props.get("STR_CODE"),
            "kunstbauten": props.get("KUNSTBAUTEN"),
            "objekt": props.get("OBJEKT"),
            "source": "gip",
            "length_m": round(length, 2),
            "lanes": (
                1.0
                if str(props.get("OBJEKT") or "").upper().strip()
                in _SUBORDINATE_LANE_OBJEKT
                else (
                    _declared_lanes(
                        str(props.get("STR_CODE") or "").strip(), props, by_lanes
                    )
                    or float(bng.get("default_lanes") or 2)
                )
            ),
        }
        code_k = str(props.get("STR_CODE") or "")
        str_counts[code_k] = str_counts.get(code_k, 0) + 1

    apply_follow_parent_z(roads, site)

    # Cache for debugging / other tools
    proc = processed_dir(site)
    out = proc / "gip_roads_beamng.json"
    out.write_text(json.dumps(roads, indent=2), encoding="utf-8")
    print(
        f"GIP guardrail centerlines: {len(roads)} OBJECTIDs from {path.name} "
        f"(skipped_short={skipped_short} str_codes={dict(str_counts)}) "
        f"-> {out.relative_to(Path(__file__).resolve().parents[1])}"
    )
    return roads


def load_gip_polylines_for_decals(site: dict | None = None) -> list[dict]:
    """Format expected by ``build_decal_roads`` / ``load_road_polylines``."""
    site = site or load_site()
    roads = load_gip_road_segments(site)
    skip = unify_replaced_oids(site, "decals")
    out: list[dict] = []
    for key, road in roads.items():
        oid = road.get("objectid")
        if oid is not None and int(oid) in skip:
            continue
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
                "str_code": road.get("str_code"),
                "objectid": road.get("objectid"),
                "objekt": road.get("objekt"),
                "kunstbauten": road.get("kunstbauten"),
                "pts": pts,
                "cum": cum,
                "length": cum[-1],
                "follow_parent": road.get("follow_parent"),
                "lanes": road.get("lanes"),
            }
        )
    out.extend(_unify_decal_roads(site, roads))
    return out


def _sample_nodes_z(nodes: list, frac: float) -> float:
    if not nodes:
        return 0.0
    if len(nodes) == 1:
        return float(nodes[0][2])
    cum = [0.0]
    for a, b in zip(nodes, nodes[1:]):
        cum.append(cum[-1] + math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1])))
    target = frac * cum[-1]
    for i in range(len(cum) - 1):
        if cum[i + 1] >= target - 1e-9:
            span = max(cum[i + 1] - cum[i], 1e-9)
            t = (target - cum[i]) / span
            return float(nodes[i][2]) + t * (float(nodes[i + 1][2]) - float(nodes[i][2]))
    return float(nodes[-1][2])


def _unify_decal_roads(site: dict, roads: dict) -> list[dict]:
    """Mean-axis stand-ins for non-tunnel unify specs (surface A12 pair, etc.)."""
    extra: list[dict] = []
    for spec in load_unify_specs(site):
        if "decals" not in spec["replaces"]:
            continue
        if spec["kind"] == "tunnel":
            continue
        members = []
        for oid in spec["objectids"]:
            road = roads.get(str(oid))
            if road and len(road.get("nodes") or []) >= 2:
                members.append(road)
        if len(members) < 2:
            print(
                f"unify {spec['id']}: decal mean_axis needs two pieces, "
                f"got {len(members)} {spec['objectids']}"
            )
            continue
        axis_members = [
            m for m in members if float(m.get("length_m") or 0.0) >= 20.0
        ]
        if len(axis_members) < 2:
            axis_members = members
        polys = [
            [(float(n[0]), float(n[1])) for n in (m.get("nodes") or [])]
            for m in axis_members
        ]
        xy = mean_axis_xy(polys, step_m=spec["step_m"])
        if len(xy) < 2:
            continue
        width = float(spec.get("width_m") or members[0]["nodes"][0][3])
        pts = []
        for i, (x, y) in enumerate(xy):
            frac = i / (len(xy) - 1)
            z = sum(_sample_nodes_z(m["nodes"], frac) for m in axis_members) / len(
                axis_members
            )
            pts.append((round(x, 3), round(y, 3), round(z, 3), round(width, 2)))
        cum = [0.0]
        for a, b in zip(pts, pts[1:]):
            cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        extra.append(
            {
                "id": f"unify_{spec['id']}",
                "name": spec["id"],
                "highway": "motorway",
                "str_code": members[0].get("str_code") or "A12",
                "objectid": spec["objectids"][0],
                "objekt": "S-A",
                "kunstbauten": None,
                "pts": pts,
                "cum": cum,
                "length": cum[-1],
            }
        )
        print(
            f"unify {spec['id']}: decal mean_axis {spec['objectids']} "
            f"-> {cum[-1]:.1f}m"
        )
    return extra


def _xy4(p) -> tuple[float, float]:
    return float(p[0]), float(p[1])


def _unit(dx: float, dy: float) -> tuple[float, float]:
    L = math.hypot(dx, dy) or 1.0
    return dx / L, dy / L


def _oid(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _norm_str_code(v) -> str:
    s = str(v or "").strip()
    return s or "_none"


def _road_spine_cfg(site: dict) -> dict:
    """YAML ``beamng.roads.spine_start_oid`` / ``spine_next``."""
    raw = (site.get("beamng") or {}).get("roads") or {}
    start = raw.get("spine_start_oid")
    next_map: dict[int, int] = {}
    for e in raw.get("spine_next") or []:
        if not isinstance(e, dict):
            continue
        fo, to = _oid(e.get("from_oid")), _oid(e.get("to_oid"))
        if fo is None or to is None:
            continue
        next_map[fo] = to
    return {
        "start_oid": _oid(start),
        "next": next_map,
    }


def _dist2_to_road(road: dict, x: float, y: float) -> float:
    best = 1e18
    pts = road.get("pts") or []
    for a, b in zip(pts, pts[1:]):
        ax, ay = float(a[0]), float(a[1])
        bx, by = float(b[0]), float(b[1])
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        if seg2 < 1e-12:
            d2 = (x - ax) ** 2 + (y - ay) ** 2
        else:
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / seg2))
            px, py = ax + t * dx, ay + t * dy
            d2 = (x - px) ** 2 + (y - py) ** 2
        if d2 < best:
            best = d2
    return best


def _feat_oids(feat: dict) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()

    def _add(v) -> None:
        n = _oid(v)
        if n is None or n in seen:
            return
        seen.add(n)
        out.append(n)

    _add(feat.get("objectid"))
    for key in ("merged_from", "objectids"):
        for x in feat.get(key) or []:
            _add(x)
    return out


def _chain_gip_spine(
    roads: list[dict],
    tol_m: float = 1.0,
    *,
    start_oid: int | None = None,
    spine_next: dict[int, int] | None = None,
) -> dict:
    """Longest continuous carriageway through GIP OBJECTIDs.

    Decal ``stitch_abutting`` stops at degree>2 ends (ramps/junctions), so the
    longest component is only ~700 m. Bridges need the full corridor (~15 km):
    grow from the longest piece (or ``start_oid``) and at branches pick the
    partner that continues the travel direction, unless ``spine_next`` forces
    a successor OBJECTID.
    """
    work: list[dict] = []
    for r in roads:
        pts = r.get("pts") or []
        if len(pts) < 2:
            continue
        work.append(
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "highway": r.get("highway") or "primary",
                "str_code": r.get("str_code"),
                "pts": [
                    [float(p[0]), float(p[1]), float(p[2]), float(p[3])] for p in pts
                ],
                "length": float(r.get("length") or 0.0),
            }
        )
    if not work:
        raise SystemExit("No GIP segments for span centerline")

    tol2 = float(tol_m) * float(tol_m)
    start = max(range(len(work)), key=lambda i: work[i]["length"])
    if start_oid is not None:
        matches = [i for i, w in enumerate(work) if _oid(w["id"]) == int(start_oid)]
        if matches:
            start = matches[0]
        else:
            print(
                f"WARNING: spine_start_oid={start_oid} not in this STR_CODE group; "
                "using longest piece"
            )
    chain = [list(p) for p in work[start]["pts"]]
    used = {start}
    ids = [work[start]["id"]]
    id_s = work[start]["id"]
    id_e = work[start]["id"]
    junctions: list[dict] = []
    overrides_applied: list[int] = []
    next_map = spine_next or {}

    def end_xy(poly: dict, which: str) -> tuple[float, float]:
        p = poly["pts"][0] if which == "s" else poly["pts"][-1]
        return _xy4(p)

    def end_outward(poly: dict, which: str) -> tuple[float, float]:
        pts = poly["pts"]
        if which == "e":
            return _unit(pts[-1][0] - pts[-2][0], pts[-1][1] - pts[-2][1])
        return _unit(pts[0][0] - pts[1][0], pts[0][1] - pts[1][1])

    grew = True
    while grew:
        grew = False
        for end_which in ("e", "s"):
            if len(chain) < 2:
                break
            if end_which == "e":
                x, y = chain[-1][0], chain[-1][1]
                dx, dy = _unit(chain[-1][0] - chain[-2][0], chain[-1][1] - chain[-2][1])
                end_oid = _oid(id_e)
            else:
                x, y = chain[0][0], chain[0][1]
                dx, dy = _unit(chain[0][0] - chain[1][0], chain[0][1] - chain[1][1])
                end_oid = _oid(id_s)
            cands: list[tuple[float, float, int, str]] = []
            for j, other in enumerate(work):
                if j in used:
                    continue
                for qw in ("s", "e"):
                    qx, qy = end_xy(other, qw)
                    d2 = (x - qx) * (x - qx) + (y - qy) * (y - qy)
                    if d2 > tol2:
                        continue
                    odx, ody = end_outward(other, qw)
                    # Prefer partner whose outward opposes chain outward (= continuation).
                    align = -(dx * odx + dy * ody)
                    cands.append((align, -d2, j, qw))
            forced = next_map.get(end_oid) if end_oid is not None else None
            n_raw = len(cands)
            if forced is not None and end_oid in overrides_applied:
                forced = None
            if forced is not None:
                forced_cands = [c for c in cands if _oid(work[c[2]]["id"]) == forced]
                if not forced_cands:
                    already = any(_oid(work[j]["id"]) == forced for j in used)
                    if not already:
                        print(
                            f"WARNING: spine_next from_oid={end_oid} to_oid={forced} "
                            "not adjacent at this end; stopping this end"
                        )
                    continue
                cands = forced_cands
            if not cands:
                continue
            cands.sort(reverse=True)
            align, _nd2, j, qw = cands[0]
            rejected = [
                _oid(work[c[2]]["id"])
                for c in cands[1:]
                if _oid(work[c[2]]["id"]) is not None
            ]
            if n_raw > 1 or forced is not None:
                junctions.append(
                    {
                        "end": end_which,
                        "from_oid": end_oid,
                        "chosen_oid": _oid(work[j]["id"]),
                        "align": round(float(align), 4),
                        "n_candidates": n_raw,
                        "rejected_oids": rejected,
                        "override": forced is not None,
                    }
                )
            if forced is not None and end_oid is not None:
                overrides_applied.append(end_oid)
            pts = work[j]["pts"]
            if end_which == "e":
                if qw == "s":
                    chain.extend(list(p) for p in pts[1:])
                else:
                    chain.extend(list(p) for p in reversed(pts[:-1]))
                id_e = work[j]["id"]
            else:
                if qw == "e":
                    chain[0:0] = [list(p) for p in pts[:-1]]
                else:
                    chain[0:0] = [list(p) for p in reversed(pts[1:])]
                id_s = work[j]["id"]
            used.add(j)
            ids.append(work[j]["id"])
            grew = True

    if len(chain) < 2:
        raise SystemExit("GIP spine chain produced empty centerline")

    cum = [0.0]
    for a, b in zip(chain, chain[1:]):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    name = work[start].get("name") or "GIP Verkehrswege"
    return {
        "id": "gip_spine",
        "name": name,
        "highway": work[start].get("highway") or "primary",
        "str_code": work[start].get("str_code"),
        "source": "gip",
        "pts": [(p[0], p[1], p[2], p[3]) for p in chain],
        "cum": cum,
        "length": float(cum[-1]),
        "objectids": ids,
        "pieces_used": len(used),
        "pieces_total": len(work),
        "junctions": junctions,
        "overrides_applied": overrides_applied,
    }


def _corridor_id(code: str, kind: str, leftover_i: int) -> str:
    code_s = "unknown" if code == "_none" else code
    if kind == "trunk":
        return f"gip_trunk_{code_s}"
    if leftover_i <= 0:
        return f"gip_{code_s}"
    return f"gip_{code_s}_leftover_{leftover_i}"


def load_gip_corridors(site: dict | None = None) -> list[dict]:
    """One continuous polyline per corridor (trunk STR_CODE isolated).

    Writes ``gip_corridors.json`` (ids, lengths, junction choices — no pts).
    """
    site = site or load_site()
    roads = load_gip_polylines_for_decals(site)
    if not roads:
        raise SystemExit("No GIP segments for corridors")
    cfg = _road_spine_cfg(site)
    trunk_code = str(
        ((site.get("sources") or {}).get("gip") or {}).get("str_code") or ""
    ).strip()
    trunk_key = _norm_str_code(trunk_code) if trunk_code else ""

    groups: dict[str, list[dict]] = {}
    for r in roads:
        groups.setdefault(_norm_str_code(r.get("str_code")), []).append(r)

    order: list[str] = []
    if trunk_key and trunk_key in groups:
        order.append(trunk_key)
    for k in sorted(groups.keys()):
        if k not in order:
            order.append(k)

    corridors: list[dict] = []
    applied: list[int] = []
    for code in order:
        unused = list(groups[code])
        if code == "_none":
            n_minor = 0
            for piece in unused:
                road = _chain_gip_spine([piece], tol_m=1.0)
                n_minor += 1
                cid = f"gip_minor_{piece.get('id')}"
                road["id"] = cid
                road["str_code"] = None
                road["corridor_kind"] = "minor"
                road["source"] = "gip"
                corridors.append(road)
            print(f"GIP minor corridors: {n_minor} pieces (no spine chain)")
            continue
        first = True
        leftover_i = 0
        while unused:
            is_trunk = bool(first and code == trunk_key)
            start_oid = cfg["start_oid"] if is_trunk else None
            # spine_next is global (OBJECTIDs unique); apply on every chain so a
            # leftover B179 fork can still be steered.
            road = _chain_gip_spine(
                unused,
                tol_m=1.0,
                start_oid=start_oid,
                spine_next=cfg["next"] or None,
            )
            applied.extend(int(x) for x in (road.get("overrides_applied") or []))
            used_ids = {str(i) for i in (road.get("objectids") or [])}
            unused = [r for r in unused if str(r.get("id")) not in used_ids]
            if is_trunk:
                kind = "trunk"
            elif first:
                kind = "side"
            else:
                kind = "leftover"
                leftover_i += 1
            cid = _corridor_id(code, kind, leftover_i if kind == "leftover" else 0)
            road["id"] = cid
            road["str_code"] = None if code == "_none" else code
            road["corridor_kind"] = kind
            road["source"] = "gip"
            corridors.append(road)
            print(
                f"GIP corridor {cid}: kind={kind} len={road['length']:.1f}m "
                f"pieces={road['pieces_used']}/{road['pieces_total']} "
                f"junctions={len(road.get('junctions') or [])}"
            )
            first = False

    for fo, to in cfg["next"].items():
        if fo not in set(applied):
            print(
                f"WARNING: spine_next from_oid={fo} to_oid={to} did not apply "
                "(from_oid never at a chain end that grew)"
            )

    proc = processed_dir(site)
    summary = {
        "trunk_str_code": trunk_code or None,
        "spine_start_oid": cfg["start_oid"],
        "spine_next": [
            {"from_oid": fo, "to_oid": to} for fo, to in cfg["next"].items()
        ],
        "corridors": [
            {
                "id": c["id"],
                "name": c.get("name"),
                "str_code": c.get("str_code"),
                "kind": c.get("corridor_kind"),
                "length_m": round(float(c["length"]), 2),
                "nodes": len(c.get("pts") or []),
                "objectids": c.get("objectids"),
                "pieces_used": c.get("pieces_used"),
                "pieces_total": c.get("pieces_total"),
                "junctions": c.get("junctions") or [],
            }
            for c in corridors
        ],
    }
    out = proc / "gip_corridors.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(
        f"GIP corridors: {len(corridors)} "
        f"-> {out.relative_to(Path(__file__).resolve().parents[1])}"
    )
    return corridors


def load_gip_stitched_span_road(site: dict | None = None) -> dict:
    """Trunk corridor (through-route) as one continuous road dict.

    Used by callers that still want a single spine. Prefer
    ``load_gip_corridor_for_feature`` for Kunstbauten.
    """
    site = site or load_site()
    corridors = load_gip_corridors(site)
    trunk = next((c for c in corridors if c.get("corridor_kind") == "trunk"), None)
    if trunk is None:
        trunk = max(corridors, key=lambda c: float(c.get("length") or 0.0))
    print(
        f"Centerline GIP (spine/trunk): {trunk['name']!r} "
        f"id={trunk.get('id')} "
        f"len={trunk['length']:.1f}m nodes={len(trunk['pts'])} "
        f"pieces={trunk['pieces_used']}/{trunk['pieces_total']}"
    )
    return trunk


def load_gip_corridor_for_feature(
    feat: dict,
    site: dict | None = None,
    corridors: list[dict] | None = None,
) -> dict:
    """Corridor that owns this Kunstbaute's OBJECTID(s).

    Fallback: nearest corridor of the same STR_CODE, then nearest overall.
    """
    site = site or load_site()
    if corridors is None:
        corridors = load_gip_corridors(site)
    if not corridors:
        raise SystemExit("No GIP corridors")

    oids = _feat_oids(feat)
    if oids:
        scored: list[tuple[int, float, dict]] = []
        for c in corridors:
            ids = {_oid(x) for x in (c.get("objectids") or [])}
            n = sum(1 for o in oids if o in ids)
            if n:
                scored.append((n, float(c.get("length") or 0.0), c))
        if scored:
            scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
            return scored[0][2]

    xy = feat.get("xy") or []
    mid = xy[len(xy) // 2] if xy else None
    feat_code = str(feat.get("str_code") or "").strip()

    def _nearest(cands: list[dict]) -> dict:
        if mid is None or len(xy) < 1:
            return max(cands, key=lambda c: float(c.get("length") or 0.0))
        return min(cands, key=lambda c: _dist2_to_road(c, float(mid[0]), float(mid[1])))

    if feat_code:
        same = [
            c
            for c in corridors
            if str(c.get("str_code") or "").strip() == feat_code
        ]
        if same:
            picked = _nearest(same)
            print(
                f"WARNING: oid={feat.get('objectid')} not in corridor objectids; "
                f"using nearest {feat_code} corridor {picked.get('id')}"
            )
            return picked

    picked = _nearest(corridors)
    print(
        f"WARNING: oid={feat.get('objectid')} corridor fallback to nearest "
        f"{picked.get('id')} ({picked.get('str_code')})"
    )
    return picked


def is_gip_tunnel_segment(road: dict, site: dict | None = None) -> bool:
    """True for tunnel/gallery Kunstbauten or S-AT/S-BT/S-BG/S-LT.

    ``STRNAME`` is ignored (A12 approach pieces are titled “Landecker Tunnel”
    without being underground). YAML ``not_tunnel_objectids`` wins.
    """
    skip = not_tunnel_objectids(site)
    oids: list[int] = []
    for raw in (
        road.get("objectid"),
        road.get("osm_id"),
        road.get("id"),
        *(road.get("osm_ids") or []),
    ):
        try:
            oids.append(int(raw))
        except (TypeError, ValueError):
            continue
    if skip and any(oid in skip for oid in oids):
        return False
    objekt = str(road.get("objekt") or "").upper()
    # S-BT Landesstraßen-Tunnel, S-BG Galerie, S-AT Autobahn-Tunnel,
    # S-LT Landesstraße-L Tunnel/Galerie
    if objekt in ("S-BT", "S-BG", "S-AT", "S-LT"):
        return True
    kunst = str(road.get("kunstbauten") or "").lower()
    if any(k in kunst for k in ("tunnel", "galerie", "unterflur")):
        return True
    return False
