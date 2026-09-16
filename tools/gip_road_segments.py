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

    xmin, ymin = sc.xmin, sc.ymin
    xmax, ymax = sc.xmax, sc.ymax
    roads: dict = {}
    skipped_short = 0
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
        nodes = []
        for x, y in piece:
            bx, by = sc.crs_to_beamng(x, y)
            z = float(z_at(bx, by)) if z_at else 0.0
            nodes.append([round(bx, 3), round(by, 3), round(z, 3), round(width_m, 2)])
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
            "lanes": float(bng.get("default_lanes") or 2),
        }

    # Cache for debugging / other tools
    proc = processed_dir(site)
    out = proc / "gip_roads_beamng.json"
    out.write_text(json.dumps(roads, indent=2), encoding="utf-8")
    print(
        f"GIP guardrail centerlines: {len(roads)} OBJECTIDs from {path.name} "
        f"(skipped_short={skipped_short}) -> {out.relative_to(Path(__file__).resolve().parents[1])}"
    )
    return roads


def load_gip_polylines_for_decals(site: dict | None = None) -> list[dict]:
    """Format expected by ``build_decal_roads`` / ``load_road_polylines``."""
    site = site or load_site()
    roads = load_gip_road_segments(site)
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


def _xy4(p) -> tuple[float, float]:
    return float(p[0]), float(p[1])


def _unit(dx: float, dy: float) -> tuple[float, float]:
    L = math.hypot(dx, dy) or 1.0
    return dx / L, dy / L


def _chain_gip_spine(roads: list[dict], tol_m: float = 1.0) -> dict:
    """Longest continuous carriageway through GIP OBJECTIDs.

    Decal ``stitch_abutting`` stops at degree>2 ends (ramps/junctions), so the
    longest component is only ~700 m. Bridges need the full corridor (~15 km):
    grow from the longest piece and at branches pick the partner that continues
    the travel direction.
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
    chain = [list(p) for p in work[start]["pts"]]
    used = {start}
    ids = [work[start]["id"]]

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
            else:
                x, y = chain[0][0], chain[0][1]
                dx, dy = _unit(chain[0][0] - chain[1][0], chain[0][1] - chain[1][1])
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
            if not cands:
                continue
            cands.sort(reverse=True)
            _align, _nd2, j, qw = cands[0]
            pts = work[j]["pts"]
            if end_which == "e":
                if qw == "s":
                    chain.extend(list(p) for p in pts[1:])
                else:
                    chain.extend(list(p) for p in reversed(pts[:-1]))
            else:
                if qw == "e":
                    chain[0:0] = [list(p) for p in pts[:-1]]
                else:
                    chain[0:0] = [list(p) for p in reversed(pts[1:])]
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
        "source": "gip",
        "pts": [(p[0], p[1], p[2], p[3]) for p in chain],
        "cum": cum,
        "length": float(cum[-1]),
        "objectids": ids,
        "pieces_used": len(used),
        "pieces_total": len(work),
    }


def load_gip_stitched_span_road(site: dict | None = None) -> dict:
    """One continuous road dict (pts/cum/length) from GIP Verkehrswege.

    Used by bridges/galleries ``centerline: gip`` (same shape as Strassennetz load).
    """
    site = site or load_site()
    roads = load_gip_polylines_for_decals(site)
    if not roads:
        raise SystemExit("No GIP segments for span centerline")
    road = _chain_gip_spine(roads, tol_m=1.0)
    print(
        f"Centerline GIP (spine): {road['name']!r} "
        f"len={road['length']:.1f}m nodes={len(road['pts'])} "
        f"pieces={road['pieces_used']}/{road['pieces_total']}"
    )
    return road


def is_gip_tunnel_segment(road: dict) -> bool:
    """True for tunnel/gallery Kunstbauten (no roadside posts through structure)."""
    kunst = str(road.get("kunstbauten") or "").lower()
    objekt = str(road.get("objekt") or "").upper()
    if objekt in ("S-BT", "S-BG"):
        return True
    if any(k in kunst for k in ("tunnel", "galerie", "unterflur")):
        return True
    return False
