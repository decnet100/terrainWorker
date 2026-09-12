"""Build GIP bridges as BeamNG MeshRoad decks.

Geometry rules (KONZEPT):
  - XY follows the OSM/road centerline (straight road), not raw GIP tangents
  - Z = cubic Hermite along arc length from abutment Z + approach dz/ds
  - MeshRoad node Z = surface_z - depth/2 (slab center)

Per-bridge config in site YAML under beamng.bridges.items[]
(defaults + style placeholders for understructure / edge later).

Usage:
  cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE='config/sites/l13_kuehtai.yaml'; python tools\\build_bridges.py
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import SiteCoords, load_site, processed_dir, site_slug  # noqa: E402

USER_LEVELS = (
    Path.home()
    / "AppData"
    / "Local"
    / "BeamNG"
    / "BeamNG.drive"
    / "current"
    / "levels"
)

# Keys merged from defaults <- item override
BRIDGE_SCALAR_KEYS = (
    "width_m",
    "width_from_road",
    "depth_m",
    "deck_lift_m",
    "node_z_is_top",
    "extend_before_m",
    "extend_after_m",
    "under_inset_m",  # shrink rock-under mask inward from gap (asphalt stays on extends)
    "auto_span",
    "span_dip_m",
    "span_search_m",
    "step_m",
    "crossfall_max",
)


def _structure_kind(props: dict) -> str | None:
    name = str(props.get("KUNSTBAUTEN") or "").strip()
    bez = str(props.get("OBJEKTBEZEICHNUNG") or "").lower()
    if not name or name.lower() == "none":
        return None
    if "galerie" in name.lower() or "tunnel" in bez:
        return "gallery" if "galerie" in name.lower() else "tunnel"
    if "brücke" in name.lower() or "bruecke" in name.lower() or "brücke" in bez or "bruecke" in bez:
        return "bridge"
    if "durchlass" in name.lower():
        return "culvert"
    return "structure"


def _walk_coords(coords, out: list) -> None:
    if not coords:
        return
    if isinstance(coords[0], (int, float)):
        out.append(coords)
        return
    for c in coords:
        _walk_coords(c, out)


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]+", "_", s.strip())
    return s.strip("_")[:48] or "bridge"


def find_gip_geojson(site: dict) -> Path:
    raw = ROOT / "data" / "raw"
    slug = site_slug(site)
    matches = sorted(raw.glob(f"gip_{slug}*.geojson"))
    if not matches:
        raise SystemExit(f"No GIP cache for {slug} - run: python tools\\fetch_gip.py")
    return matches[-1]


def load_road_polylines(proc: Path) -> list[dict]:
    """Each road: {id, name, highway, pts: [(x,y,z,w), ...], cum: [s,...]}."""
    path = proc / "roads_beamng.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    roads = []
    for k, road in data.items():
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        pts = []
        for n in nodes:
            w = float(n[3]) if len(n) > 3 else 7.0
            pts.append((float(n[0]), float(n[1]), float(n[2]), w))
        cum = [0.0]
        for a, b in zip(pts, pts[1:]):
            cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        roads.append(
            {
                "id": k,
                "name": road.get("name"),
                "highway": road.get("highway"),
                "pts": pts,
                "cum": cum,
                "length": cum[-1],
            }
        )
    return roads


def project_on_road(
    road: dict, x: float, y: float
) -> tuple[float, float, float, float, float, float, float, float]:
    """Return (s, px, py, pz, tx, ty, tz, width) on this road polyline."""
    best = None
    pts = road["pts"]
    cum = road["cum"]
    for i, (a, b) in enumerate(zip(pts, pts[1:])):
        ax, ay, az, aw = a
        bx, by, bz, bw = b
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        if seg2 < 1e-9:
            t = 0.0
        else:
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / seg2))
        px = ax + t * dx
        py = ay + t * dy
        pz = az + t * (bz - az)
        pw = aw + t * (bw - aw)
        d2 = (x - px) ** 2 + (y - py) ** 2
        if best is None or d2 < best[0]:
            seglen = math.sqrt(seg2) if seg2 > 0 else 0.0
            if seglen < 1e-9:
                tx, ty, tz = 1.0, 0.0, 0.0
            else:
                L3 = math.sqrt(dx * dx + dy * dy + (bz - az) ** 2) or seglen
                tx, ty, tz = dx / L3, dy / L3, (bz - az) / L3
            s = cum[i] + t * seglen
            best = (d2, s, px, py, pz, tx, ty, tz, pw)
    assert best is not None
    _d2, s, px, py, pz, tx, ty, tz, pw = best
    return s, px, py, pz, tx, ty, tz, pw


def pick_road(roads: list[dict], xy_gip: list[tuple[float, float]]) -> dict:
    """Nearest carriageway centerline to GIP (exclude footpaths)."""
    mid = xy_gip[len(xy_gip) // 2]
    best = None
    for road in roads:
        hwy = str(road.get("highway") or "")
        if hwy in {"path", "footway", "cycleway", "steps", "track"}:
            continue
        _s, px, py, _pz, _tx, _ty, _tz, _w = project_on_road(road, mid[0], mid[1])
        d2 = (mid[0] - px) ** 2 + (mid[1] - py) ** 2
        if best is None or d2 < best[0]:
            best = (d2, road)
    if best is None:
        raise SystemExit("No roads to snap bridge onto")
    return best[1]


def sample_road(
    road: dict, s: float
) -> tuple[float, float, float, float, float, float, float]:
    """Interpolate (x,y,z,tx,ty,tz,width) at chainage s (clamped)."""
    pts = road["pts"]
    cum = road["cum"]
    s = max(0.0, min(float(road["length"]), s))
    for i in range(len(pts) - 1):
        if s <= cum[i + 1] + 1e-9 or i == len(pts) - 2:
            seg = cum[i + 1] - cum[i]
            t = 0.0 if seg < 1e-9 else (s - cum[i]) / seg
            ax, ay, az, aw = pts[i]
            bx, by, bz, bw = pts[i + 1]
            x = ax + t * (bx - ax)
            y = ay + t * (by - ay)
            z = az + t * (bz - az)
            w = aw + t * (bw - aw)
            dx, dy, dz = bx - ax, by - ay, bz - az
            L3 = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
            return x, y, z, dx / L3, dy / L3, dz / L3, w
    x, y, z, w = pts[-1]
    return x, y, z, 1.0, 0.0, 0.0, w


def load_terrain_z(site: dict):
    from PIL import Image

    proc = processed_dir(site)
    size = int((site.get("beamng") or {}).get("mask_size") or 512)
    hm_path = proc / f"heightmap_{size}.png"
    meta_path = proc / "heightmap_meta.json"
    if not hm_path.is_file() or not meta_path.is_file():
        raise SystemExit(f"Missing heightmap - run build_smoke first ({hm_path})")
    hm = np.asarray(Image.open(hm_path))
    max_h = float(json.loads(meta_path.read_text(encoding="utf-8"))["max_height_m"])
    n = int(hm.shape[0])

    def z_at(bx: float, by: float) -> float:
        c = int(max(0, min(n - 1, round(bx))))
        r = int(max(0, min(n - 1, round(n - 1 - by))))
        return float(hm[r, c]) / 65535.0 * max_h

    return z_at


def hermite_z(t: float, z0: float, z1: float, m0: float, m1: float, length: float) -> float:
    t2 = t * t
    t3 = t2 * t
    h00 = 2 * t3 - 3 * t2 + 1
    h10 = t3 - 2 * t2 + t
    h01 = -2 * t3 + 3 * t2
    h11 = t3 - t2
    return h00 * z0 + h10 * (m0 * length) + h01 * z1 + h11 * (m1 * length)


def hermite_dz_ds(t: float, z0: float, z1: float, m0: float, m1: float, length: float) -> float:
    """dz/ds for cubic Hermite parameterized by t in [0,1], s = t * length."""
    # dz/dt then / length
    t2 = t * t
    d00 = 6 * t2 - 6 * t
    d10 = 3 * t2 - 4 * t + 1
    d01 = -6 * t2 + 6 * t
    d11 = 3 * t2 - 2 * t
    dz_dt = d00 * z0 + d10 * (m0 * length) + d01 * z1 + d11 * (m1 * length)
    return dz_dt / length if length > 1e-9 else 0.0


def slope_ds(tx: float, ty: float, tz: float) -> float:
    horiz = math.hypot(tx, ty)
    return 0.0 if horiz < 1e-6 else tz / horiz


def left_right_unit(tx: float, ty: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return (left_xy, right_xy) unit vectors perpendicular to plan tangent."""
    horiz = math.hypot(tx, ty) or 1.0
    fx, fy = tx / horiz, ty / horiz
    left = (-fy, fx)
    right = (fy, -fx)
    return left, right


def sample_crossfall(
    z_terrain,
    x: float,
    y: float,
    tx: float,
    ty: float,
    half_w: float,
) -> tuple[float, float, float, float]:
    """Heightmap Querprofil: return (crossfall_dz_per_m_right, zl, zc, zr).

    crossfall > 0 means surface rises toward the right (left side lower).
    Sample only on solid road — not over ditches.
    """
    left, right = left_right_unit(tx, ty)
    zc = z_terrain(x, y)
    zl = z_terrain(x + half_w * left[0], y + half_w * left[1])
    zr = z_terrain(x + half_w * right[0], y + half_w * right[1])
    width = max(2.0 * half_w, 0.1)
    # Average grade left→right across full width
    crossfall = (zr - zl) / width
    return crossfall, zl, zc, zr


def normal_from_pitch_crossfall(
    tx: float,
    ty: float,
    dz_ds: float,
    crossfall: float,
) -> tuple[float, float, float]:
    """MeshRoad up-normal: longitudinal pitch + planar Querneigung (one plane)."""
    horiz = math.hypot(tx, ty) or 1.0
    fx, fy = tx / horiz, ty / horiz
    fwd = np.array([fx, fy, dz_ds], dtype=float)
    fn = np.linalg.norm(fwd)
    if fn < 1e-9:
        return (0.0, 0.0, 1.0)
    fwd /= fn
    _left, right_xy = left_right_unit(tx, ty)
    # Move right: horizontal right + vertical crossfall
    right = np.array([right_xy[0], right_xy[1], crossfall], dtype=float)
    rn = np.linalg.norm(right)
    if rn < 1e-9:
        return (0.0, 0.0, 1.0)
    right /= rn
    normal = np.cross(fwd, right)
    nn = np.linalg.norm(normal)
    if nn < 1e-9:
        return (0.0, 0.0, 1.0)
    normal /= nn
    if normal[2] < 0:
        normal = -normal
    return float(normal[0]), float(normal[1]), float(normal[2])


def resolve_bridge_cfg(defaults: dict, items: list, feat: dict) -> dict:
    """Merge defaults with first matching items[] entry."""
    cfg = dict(defaults)
    cfg["style"] = dict(defaults.get("style") or {})
    mats = dict(defaults.get("materials") or {})
    oid = feat.get("objectid")
    name = str(feat.get("name") or "")
    matched = None
    for item in items or []:
        m = item.get("match") or {}
        ok = True
        if "objectid" in m and int(m["objectid"]) != int(oid or -1):
            ok = False
        if "name" in m and str(m["name"]).lower() not in name.lower():
            ok = False
        if "name_exact" in m and str(m["name_exact"]) != name:
            ok = False
        if ok and m:
            matched = item
            break
    if matched:
        for k in BRIDGE_SCALAR_KEYS:
            if k in matched:
                cfg[k] = matched[k]
        if "style" in matched:
            cfg["style"] = {**cfg["style"], **(matched.get("style") or {})}
        if "materials" in matched:
            mats = {**mats, **(matched.get("materials") or {})}
        cfg["match_id"] = matched.get("id") or matched.get("match")
    cfg["materials"] = mats
    return cfg


def build_span_on_road(
    xy_gip: list[tuple[float, float]],
    road: dict,
    z_terrain,
    cfg: dict,
) -> tuple[
    list[tuple[float, float]],
    list[float],
    list[tuple[float, float, float]],
    list[float],
    dict,
]:
    """Deck on road centerline with portal-profile interpolation.

    - XY follows OSM road centerline
    - Portals: road Z/dz/ds/width + Heightmap Querneigung (planar)
    - Between: Hermite Z, lerp width + crossfall; normal = pitch + crossfall
    Returns xy, center_z, normals, widths, info.
    """
    width_fallback = float(cfg["width_m"])
    width_from_road = bool(cfg.get("width_from_road", True))
    depth = float(cfg["depth_m"])
    lift = float(cfg["deck_lift_m"])
    node_z_is_top = bool(cfg.get("node_z_is_top", False))
    ext_b = float(cfg["extend_before_m"])
    ext_a = float(cfg["extend_after_m"])
    auto = bool(cfg.get("auto_span", True))
    dip_m = float(cfg["span_dip_m"])
    search_m = float(cfg["span_search_m"])
    step = float(cfg.get("step_m") or 1.0)
    # Optional clamp so ditch edges don't invent crazy banking
    cf_max = float(cfg.get("crossfall_max") or 0.12)  # ~7 deg

    s_ends = [project_on_road(road, x, y)[0] for x, y in (xy_gip[0], xy_gip[-1])]
    s_g0, s_g1 = min(s_ends), max(s_ends)
    s_first = project_on_road(road, xy_gip[0][0], xy_gip[0][1])[0]
    s_last = project_on_road(road, xy_gip[-1][0], xy_gip[-1][1])[0]
    forward = s_last >= s_first

    def half_at(s: float) -> float:
        *_rest, w = sample_road(road, s)
        return 0.5 * (w if width_from_road else width_fallback)

    if auto:
        s_lo = max(0.0, s_g0 - search_m)
        s_hi = min(float(road["length"]), s_g1 + search_m)
        gap = []
        s = s_lo
        while s <= s_hi + 1e-9:
            x, y, road_z, tx, ty, _tz, _w = sample_road(road, s)
            horiz = math.hypot(tx, ty) or 1.0
            lx, ly = -ty / horiz, tx / horiz
            half = half_at(s)
            terr = min(
                z_terrain(x, y),
                z_terrain(x + half * lx, y + half * ly),
                z_terrain(x - half * lx, y - half * ly),
            )
            gap.append((s, road_z - terr >= dip_m, road_z - terr))
            s += step
        if any(g for _s, g, _d in gap):
            s_gap0 = next(s for s, g, _d in gap if g)
            s_gap1 = next(s for s, g, _d in reversed(gap) if g)
        else:
            s_gap0, s_gap1 = s_g0, s_g1
        max_dip = max((d for _s, _g, d in gap), default=0.0)
    else:
        s_gap0, s_gap1 = s_g0, s_g1
        max_dip = None

    if forward:
        s0 = max(0.0, s_gap0 - ext_b)
        s1 = min(float(road["length"]), s_gap1 + ext_a)
    else:
        s0 = max(0.0, s_gap0 - ext_a)
        s1 = min(float(road["length"]), s_gap1 + ext_b)

    if s1 - s0 < step:
        s1 = min(float(road["length"]), s0 + step)

    # Portal frames: longitudinal from road, Querneigung from heightmap
    x0, y0, z0r, tx0, ty0, tz0, w0r = sample_road(road, s0)
    x1, y1, z1r, tx1, ty1, tz1, w1r = sample_road(road, s1)
    w0 = w0r if width_from_road else width_fallback
    w1 = w1r if width_from_road else width_fallback
    z0 = z0r + lift
    z1 = z1r + lift
    m0 = slope_ds(tx0, ty0, tz0)
    m1 = slope_ds(tx1, ty1, tz1)
    length = s1 - s0 or 1.0

    cf0, zl0, zc0, zr0 = sample_crossfall(z_terrain, x0, y0, tx0, ty0, 0.5 * w0)
    cf1, zl1, zc1, zr1 = sample_crossfall(z_terrain, x1, y1, tx1, ty1, 0.5 * w1)
    cf0 = max(-cf_max, min(cf_max, cf0))
    cf1 = max(-cf_max, min(cf_max, cf1))

    s_vals: list[float] = []
    s = s0
    while s <= s1 + 1e-9:
        s_vals.append(s)
        s += step
    if abs(s_vals[-1] - s1) > 0.01:
        s_vals.append(s1)

    xy: list[tuple[float, float]] = []
    centers: list[float] = []
    normals: list[tuple[float, float, float]] = []
    widths: list[float] = []
    crossfalls: list[float] = []

    for s in s_vals:
        t = (s - s0) / length
        x, y, _zr, tx, ty, _tz, _wr = sample_road(road, s)
        z_surf = hermite_z(t, z0, z1, m0, m1, length)
        dz = hermite_dz_ds(t, z0, z1, m0, m1, length)
        cf = cf0 + t * (cf1 - cf0)
        if t <= 1e-9:
            z_surf, dz, w, cf = z0, m0, w0, cf0
            tx, ty = tx0, ty0
        elif t >= 1.0 - 1e-9:
            z_surf, dz, w, cf = z1, m1, w1, cf1
            tx, ty = tx1, ty1
        else:
            w = w0 + t * (w1 - w0)

        nrm = normal_from_pitch_crossfall(tx, ty, dz, cf)
        # node_z_is_top: Z is driving surface; else MeshRoad node = slab center
        z_node = z_surf if node_z_is_top else (z_surf - 0.5 * depth)
        xy.append((x, y))
        centers.append(z_node)
        normals.append(nrm)
        widths.append(w)
        crossfalls.append(cf)

    # Terrain under-mask: ditch/gap only — extends stay asphalt; optional inset inward.
    inset = float(cfg.get("under_inset_m") or 0.0)
    s_u0 = s_gap0 + inset
    s_u1 = s_gap1 - inset
    under_xyw: list[list[float]] = []
    if s_u1 - s_u0 >= 0.25:
        s_u_vals: list[float] = []
        s_u = s_u0
        while s_u <= s_u1 + 1e-9:
            s_u_vals.append(s_u)
            s_u += step
        if abs(s_u_vals[-1] - s_u1) > 0.01:
            s_u_vals.append(s_u1)
        for s_u in s_u_vals:
            xu, yu, _zu, _txu, _tyu, _tzu, wu = sample_road(road, s_u)
            t_u = 0.0 if length < 1e-9 else max(0.0, min(1.0, (s_u - s0) / length))
            w_u = (w0 + t_u * (w1 - w0)) if width_from_road else width_fallback
            under_xyw.append([round(xu, 3), round(yu, 3), round(w_u, 2)])

    info = {
        "road_id": road["id"],
        "road_name": road.get("name"),
        "auto_span": auto,
        "profile": "portal_hermite_plus_heightmap_crossfall",
        "node_z_is_top": node_z_is_top,
        "deck_lift_m": lift,
        "width_from_road": width_from_road,
        "portal_width_m": [round(w0, 2), round(w1, 2)],
        "portal_z": [round(z0, 3), round(z1, 3)],
        "portal_dz_ds": [round(m0, 4), round(m1, 4)],
        "portal_crossfall": [round(cf0, 4), round(cf1, 4)],
        "portal_crossfall_deg": [
            round(math.degrees(math.atan(cf0)), 2),
            round(math.degrees(math.atan(cf1)), 2),
        ],
        "portal_hm_LCR": [
            [round(zl0, 2), round(zc0, 2), round(zr0, 2)],
            [round(zl1, 2), round(zc1, 2), round(zr1, 2)],
        ],
        "s0": round(s0, 2),
        "s1": round(s1, 2),
        "gap_s0": round(s_gap0, 2),
        "gap_s1": round(s_gap1, 2),
        "under_s0": round(s_u0, 2),
        "under_s1": round(s_u1, 2),
        "under_inset_m": inset,
        "under_nodes_xyw": under_xyw,
        "gip_s0": round(s_g0, 2),
        "gip_s1": round(s_g1, 2),
        "extend_before_m": ext_b,
        "extend_after_m": ext_a,
        "span_len_m": round(s1 - s0, 2),
        "gap_len_m": round(s_gap1 - s_gap0, 2),
        "under_len_m": round(max(0.0, s_u1 - s_u0), 2),
        "gip_len_on_road_m": round(s_g1 - s_g0, 2),
        "max_dip_m": None if max_dip is None else round(max_dip, 2),
        "nodes": len(xy),
        "style": cfg.get("style") or {},
    }
    return xy, centers, normals, widths, info


def bridge_features(site: dict, sc: SiteCoords) -> list[dict]:
    path = find_gip_geojson(site)
    data = json.loads(path.read_text(encoding="utf-8"))
    to_site = Transformer.from_crs("EPSG:4326", sc.crs, always_xy=True)
    out = []
    for f in data.get("features") or []:
        props = f.get("properties") or {}
        if _structure_kind(props) != "bridge":
            continue
        raw_pts: list = []
        _walk_coords((f.get("geometry") or {}).get("coordinates"), raw_pts)
        xy = []
        for lon, lat, *_ in raw_pts:
            x, y = to_site.transform(float(lon), float(lat))
            bx, by = sc.crs_to_beamng(x, y)
            if -50 <= bx <= sc.terrain_extent + 50 and -50 <= by <= sc.terrain_extent + 50:
                xy.append((bx, by))
        if len(xy) < 2:
            continue
        out.append(
            {
                "name": str(props.get("KUNSTBAUTEN") or "bridge"),
                "objectid": props.get("OBJECTID"),
                "length_m": props.get("Shape__Length"),
                "xy": xy,
            }
        )
    return out


def make_meshroad(
    name: str,
    xy: list[tuple[float, float]],
    zs: list[float],
    normals: list[tuple[float, float, float]],
    widths: list[float],
    *,
    depth_m: float,
    materials: dict,
) -> dict:
    nodes = []
    for (x, y), z, nrm, w in zip(xy, zs, normals, widths):
        nodes.append(
            [
                round(x, 3),
                round(y, 3),
                round(z, 3),
                round(w, 2),
                round(depth_m, 2),
                round(nrm[0], 4),
                round(nrm[1], 4),
                round(nrm[2], 4),
            ]
        )
    # meters of road per texture repeat; smaller = finer (terrain asphalt detail ~2–4 m)
    tex_len = float(
        materials.get("texture_length")
        or materials.get("textureLength")
        or 2.5
    )
    return {
        "name": name,
        "class": "MeshRoad",
        "__parent": "bridges",
        "topMaterial": materials.get("top") or "Asphalt",
        "bottomMaterial": materials.get("bottom") or "Concrete",
        "sideMaterial": materials.get("side") or "Concrete",
        "textureLength": tex_len,
        "breakAngle": 2,
        "widthSubdivisions": 0,
        "nodes": nodes,
    }


def _mesh_material(
    name: str,
    *,
    base_color: str,
    normal: str,
    annotation: str,
    persistent_id: str,
    roughness: str | None = None,
    ao: str | None = None,
    base_color_factor: list[float] | None = None,
    detail_scale: float = 1.0,
    detail_map: str | None = None,
    detail_normal: str | None = None,
) -> dict:
    """Regular Material for MeshRoad (TerrainMaterial names are not usable here)."""
    stage: dict = {
        "baseColorMap": base_color,
        "normalMap": normal,
        "roughnessFactor": 0.9,
    }
    if roughness:
        stage["roughnessMap"] = roughness
    if ao:
        stage["ambientOcclusionMap"] = ao
    if base_color_factor is not None:
        stage["baseColorFactor"] = base_color_factor
    # MeshRoad U spans full width once; detailScale makes grain finer across + along.
    if detail_map and abs(detail_scale - 1.0) > 1e-6:
        stage["detailMap"] = detail_map
        stage["detailScale"] = [float(detail_scale), float(detail_scale)]
        if detail_normal:
            stage["detailNormalMap"] = detail_normal
            stage["detailNormalMapStrength"] = 0.65
    return {
        "name": name,
        "mapTo": name,
        "class": "Material",
        "persistentId": persistent_id,
        "Stages": [stage, {}, {}, {}],
        "alphaRef": 0,
        "annotation": annotation,
        "castShadows": True,
        "materialTag0": "RoadAndPath",
        "materialTag1": "beamng",
        "version": 1.5,
    }


def ensure_meshroad_materials(user_level: Path, level_name: str, materials: dict) -> None:
    """Register MeshRoad materials in art/road, reusing this level's terrain asphalt/concrete maps."""
    mats_path = user_level / "art" / "road" / "main.materials.json"
    mats_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if mats_path.is_file() and mats_path.stat().st_size:
        try:
            data = json.loads(mats_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}

    terr = f"/levels/{level_name}/art/terrains"
    detail_scale = float(materials.get("detail_scale") or 6.0)
    # Use terrain asphalt *detail* maps (same set the TerrainMaterial blends in close-up).
    wanted = {
        materials.get("top") or "Asphalt": _mesh_material(
            materials.get("top") or "Asphalt",
            base_color=f"{terr}/t_asphalt_02_b.png",
            normal=f"{terr}/t_asphalt_02_nm.png",
            roughness=f"{terr}/t_asphalt_02_r.png",
            ao=f"{terr}/t_asphalt_02_ao.png",
            annotation="ASPHALT",
            persistent_id="a070a5a1-b71d-4e1e-9c11-0000a5fa1701",
            base_color_factor=[0.62, 0.62, 0.62, 1.0],
            detail_scale=detail_scale,
            detail_map=f"{terr}/t_asphalt_03_b.png",
            detail_normal=f"{terr}/t_asphalt_03_nm.png",
        ),
        materials.get("bottom") or "Concrete": _mesh_material(
            materials.get("bottom") or "Concrete",
            base_color=f"{terr}/t_terrain_base_concrete_b.png",
            normal=f"{terr}/t_terrain_base_concrete_nm.png",
            annotation="CONCRETE",
            persistent_id="c0ac7e7e-b71d-4e1e-9c11-0000c0ac7e7e",
        ),
        materials.get("side") or "Concrete": _mesh_material(
            materials.get("side") or "Concrete",
            base_color=f"{terr}/t_terrain_base_concrete_b.png",
            normal=f"{terr}/t_terrain_base_concrete_nm.png",
            annotation="CONCRETE",
            persistent_id="c0ac7e7e-b71d-4e1e-9c11-0000c0ac7e7e",
        ),
    }
    for name, mat in wanted.items():
        data[name] = mat
    mats_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(
        f"MeshRoad materials in {mats_path.name}: {', '.join(sorted(set(wanted)))} "
        f"(detail_scale={detail_scale})"
    )


def write_level(level_name: str, entries: list[dict], materials: dict) -> Path | None:
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        print(f"Level folder missing: {user_level}")
        return None
    ensure_meshroad_materials(user_level, level_name, materials)
    group_dir = user_level / "main" / "MissionGroup" / "level_objects" / "bridges"
    group_dir.mkdir(parents=True, exist_ok=True)
    items_path = group_dir / "items.level.json"
    with items_path.open("w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")

    lo_items = user_level / "main" / "MissionGroup" / "level_objects" / "items.level.json"
    lines = []
    if lo_items.is_file() and lo_items.stat().st_size:
        lines = [ln for ln in lo_items.read_text(encoding="utf-8").splitlines() if ln.strip()]
    names = set()
    for ln in lines:
        try:
            names.add(json.loads(ln).get("name"))
        except json.JSONDecodeError:
            pass
    if "bridges" not in names:
        lines.append(
            json.dumps(
                {"name": "bridges", "class": "SimGroup", "__parent": "level_objects", "enabled": "1"},
                separators=(",", ":"),
            )
        )
        lo_items.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("Registered SimGroup bridges under level_objects")
    return items_path


def default_bridge_cfg(bng: dict) -> tuple[dict, list]:
    raw = bng.get("bridges") or {}
    # Back-compat: flat keys become defaults
    defaults = {
        "width_m": float(raw.get("width_m") or 7.0),
        "width_from_road": bool(raw.get("width_from_road", True)),
        "depth_m": float(raw.get("depth_m") or 0.4),
        "deck_lift_m": float(raw.get("deck_lift_m") or 0.0),
        "node_z_is_top": bool(raw.get("node_z_is_top", False)),
        "extend_before_m": float(raw.get("extend_before_m") or raw.get("approach_overlap_m") or 0.5),
        "extend_after_m": float(raw.get("extend_after_m") or raw.get("approach_overlap_m") or 0.5),
        "under_inset_m": float(raw.get("under_inset_m") or 0.0),
        "auto_span": bool(raw.get("auto_span", True)),
        "span_dip_m": float(raw.get("span_dip_m") or 0.55),
        "span_search_m": float(raw.get("span_search_m") or 25.0),
        "step_m": float(raw.get("step_m") or 1.0),
        "crossfall_max": float(raw.get("crossfall_max") or 0.12),
        "style": {
            "understructure": "none",  # none | slab | piers | walls
            "edge": "none",  # none | guardrail | curb | wall
            **(raw.get("style") or {}),
        },
        "materials": {
            "top": "Asphalt",
            "bottom": "Concrete",
            "side": "Concrete",
            "texture_length": 2.5,
            "detail_scale": 6.0,
            **(raw.get("materials") or {}),
        },
    }
    # Flat material keys
    if raw.get("top_material"):
        defaults["materials"]["top"] = raw["top_material"]
    if raw.get("bottom_material"):
        defaults["materials"]["bottom"] = raw["bottom_material"]
    if raw.get("side_material"):
        defaults["materials"]["side"] = raw["side_material"]
    if "defaults" in raw and isinstance(raw["defaults"], dict):
        d = raw["defaults"]
        for k in BRIDGE_SCALAR_KEYS:
            if k in d:
                defaults[k] = d[k]
        if "style" in d:
            defaults["style"] = {**defaults["style"], **d["style"]}
        if "materials" in d:
            defaults["materials"] = {**defaults["materials"], **d["materials"]}
    items = list(raw.get("items") or [])
    return defaults, items


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--step", type=float, default=None)
    args = ap.parse_args()

    site = load_site()
    sc = SiteCoords(site)
    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "").strip()
    if not level_name:
        raise SystemExit("beamng.level_name missing in site config")

    proc = processed_dir(site)
    roads = load_road_polylines(proc)
    if not roads:
        raise SystemExit(f"Missing {proc / 'roads_beamng.json'}")

    defaults, items = default_bridge_cfg(bng)
    if args.step is not None:
        defaults["step_m"] = args.step

    z_terrain = load_terrain_z(site)
    feats = bridge_features(site, sc)
    if not feats:
        raise SystemExit("No bridge features in GIP cache for this site")

    entries = []
    span_infos = []
    for feat in feats:
        cfg = resolve_bridge_cfg(defaults, items, feat)
        road = pick_road(roads, feat["xy"])
        xy, zs, normals, widths, info = build_span_on_road(feat["xy"], road, z_terrain, cfg)
        info["name"] = feat["name"]
        info["objectid"] = feat.get("objectid")
        info["match"] = cfg.get("match_id")
        span_infos.append(info)
        mesh_name = f"bridge_{_slug(feat['name'])}_{feat.get('objectid') or len(entries)}"
        entries.append(
            make_meshroad(
                mesh_name,
                xy,
                zs,
                normals,
                widths,
                depth_m=float(cfg["depth_m"]),
                materials=cfg["materials"],
            )
        )
        surf = [z + 0.5 * float(cfg["depth_m"]) for z in zs]
        print(
            f"{feat['name']} oid={feat.get('objectid')}: "
            f"span={info['span_len_m']}m gap={info.get('gap_len_m')}m "
            f"under={info.get('under_len_m')}m(+inset {cfg.get('under_inset_m', 0)}) "
            f"nodes={info['nodes']} "
            f"on road {info['road_name']!r} "
            f"portal_w={info['portal_width_m']} "
            f"ext=({cfg['extend_before_m']}/{cfg['extend_after_m']}) "
            f"texLen={cfg['materials'].get('texture_length')} "
            f"detail={cfg['materials'].get('detail_scale')} "
            f"surface_z={min(surf):.2f}..{max(surf):.2f}"
        )

    out_json = proc / "bridges_items.level.json"
    with out_json.open("w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")

    # Deck footprints for terrain masks: asphalt under gap → rock (extends stay asphalt).
    decks = []
    for e, info in zip(entries, span_infos):
        nodes = e.get("nodes") or []
        under = info.get("under_nodes_xyw") or []
        decks.append(
            {
                "name": e.get("name"),
                "objectid": info.get("objectid"),
                "nodes_xyw": [[n[0], n[1], n[3]] for n in nodes if len(n) >= 4],
                "under_nodes_xyw": under,
                "span_len_m": info.get("span_len_m"),
                "gap_len_m": info.get("gap_len_m"),
                "under_len_m": info.get("under_len_m"),
                "under_inset_m": info.get("under_inset_m"),
                "extend_before_m": info.get("extend_before_m"),
                "extend_after_m": info.get("extend_after_m"),
            }
        )
    decks_path = proc / "bridges_decks.json"
    decks_path.write_text(
        json.dumps({"level": level_name, "decks": decks}, indent=2),
        encoding="utf-8",
    )

    meta = {
        "count": len(entries),
        "level": level_name,
        "defaults": {k: defaults[k] for k in BRIDGE_SCALAR_KEYS},
        "style_defaults": defaults["style"],
        "materials": defaults["materials"],
        "spans": span_infos,
        "decks_file": str(decks_path.relative_to(ROOT)).replace("\\", "/"),
        "note": "XY on OSM road centerline; Z Hermite; MeshRoad materials are regular Materials (not TerrainMaterial).",
    }
    (proc / "bridges_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {out_json}")
    print(f"Wrote {decks_path}")

    level_path = write_level(level_name, entries, defaults["materials"])
    if level_path:
        print(f"Injected: {level_path}")
        print("Reload the level in BeamNG (materials may need World Editor refresh).")
    else:
        print("Skipped inject (create level folder first).")


if __name__ == "__main__":
    main()
