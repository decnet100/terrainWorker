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
    "gip_pad_m",  # meters added to each short GIP end before auto_span/extends
    "auto_span",
    "span_dip_m",
    "span_search_m",
    "abut_tol_m",  # |roadZ−terrain| ≤ tol → flat abutment candidate
    "abut_run_m",  # consecutive meters of flat required
    "abut_search_m",  # outward search from gap (default: span_search_m)
    "abut_snap_z",  # portal Z = max(roadZ, terrainZ) so cut abutments meet Decal
    "approach_conform",  # bake heightmap to MeshRoad Z under deck (flush lips)
    "approach_conform_pad_m",
    "approach_conform_falloff_m",
    "approach_conform_sink_m",
    "approach_conform_max_delta_m",  # skip gorge / cliffs (|ΔZ| larger than this)
    "profile",  # hermite (legacy) | road_spline
    "centerline",  # osm | strassennetz
    "solid_run_m",
    "free_span",  # linear | pchip_ends
    "abutment_s",  # optional [s0, s1] override along centerline
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


def _xy2(pt: tuple) -> tuple[float, float]:
    return (float(pt[0]), float(pt[1]))


def _rebuild_road(seed: dict, pts: list[tuple]) -> dict:
    cum = [0.0]
    for a, b in zip(pts, pts[1:]):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    return {
        "id": seed.get("id"),
        "name": seed.get("name"),
        "highway": seed.get("highway"),
        "pts": pts,
        "cum": cum,
        "length": cum[-1],
        "stitched": True,
    }


def stitch_road_chain(
    roads: list[dict],
    seed: dict,
    *,
    join_tol_m: float = 2.5,
    max_segments: int = 80,
) -> dict:
    """Chain abutting OSM ways (same name, else same major highway) from ``seed``.

    OSM splits named roads into short ways; bridge ``extend_*_m`` is clamped to
    ``road.length``, so without stitching 30 m extends do nothing on a 40 m way.
    """
    name = str(seed.get("name") or "").strip()
    hwy = str(seed.get("highway") or "")
    used: set[int] = {id(seed)}
    pts: list[tuple] = list(seed["pts"])

    def _eligible(r: dict) -> bool:
        if id(r) in used:
            return False
        rn = str(r.get("name") or "").strip()
        rh = str(r.get("highway") or "")
        if name:
            return rn == name
        if hwy in {"path", "footway", "cycleway", "steps", "track", "service"}:
            return False
        return rh == hwy and rh in {
            "motorway",
            "trunk",
            "primary",
            "secondary",
            "tertiary",
            "unclassified",
        }

    for _ in range(max_segments):
        head, tail = _xy2(pts[0]), _xy2(pts[-1])
        best: tuple[float, str, dict, bool] | None = None
        # (dist, "pre"|"post", road, reverse_other)
        for r in roads:
            if not _eligible(r):
                continue
            a, b = _xy2(r["pts"][0]), _xy2(r["pts"][-1])
            for which, end in (("pre", head), ("post", tail)):
                d_a = math.hypot(end[0] - a[0], end[1] - a[1])
                d_b = math.hypot(end[0] - b[0], end[1] - b[1])
                if which == "pre":
                    # connect other END to our head → reverse if a is closer
                    if d_b <= join_tol_m and (best is None or d_b < best[0]):
                        best = (d_b, "pre", r, False)  # ...a..b | head
                    if d_a <= join_tol_m and (best is None or d_a < best[0]):
                        best = (d_a, "pre", r, True)  # reverse → ...b..a | head
                else:
                    if d_a <= join_tol_m and (best is None or d_a < best[0]):
                        best = (d_a, "post", r, False)  # tail | a..b
                    if d_b <= join_tol_m and (best is None or d_b < best[0]):
                        best = (d_b, "post", r, True)  # reverse → tail | b..a
        if best is None:
            break
        _d, which, r, rev = best
        used.add(id(r))
        other = list(reversed(r["pts"])) if rev else list(r["pts"])
        if which == "pre":
            # drop duplicate join vertex
            if math.hypot(other[-1][0] - pts[0][0], other[-1][1] - pts[0][1]) <= join_tol_m:
                pts = other[:-1] + pts
            else:
                pts = other + pts
        else:
            if math.hypot(pts[-1][0] - other[0][0], pts[-1][1] - other[0][1]) <= join_tol_m:
                pts = pts + other[1:]
            else:
                pts = pts + other

    if len(used) <= 1:
        return seed
    out = _rebuild_road(seed, pts)
    out["id"] = f"{seed.get('id')}+{len(used) - 1}"
    return out


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


def road_terrain_dip(road: dict, z_terrain, s: float) -> tuple[float, float, float]:
    """(dip_min, road_z, terr_center). dip = road_z − min(terrain at center + edges)."""
    x, y, road_z, tx, ty, _tz, w = sample_road(road, s)
    horiz = math.hypot(tx, ty) or 1.0
    lx, ly = -ty / horiz, tx / horiz
    half = 0.5 * w
    tc = float(z_terrain(x, y))
    terr = min(
        tc,
        float(z_terrain(x + half * lx, y + half * ly)),
        float(z_terrain(x - half * lx, y - half * ly)),
    )
    return road_z - terr, road_z, tc


def find_flat_abutment(
    road: dict,
    z_terrain,
    s_edge: float,
    direction: float,
    *,
    tol_m: float,
    run_m: float,
    search_m: float,
    step: float,
) -> tuple[float, str, float]:
    """Walk outward from a gap edge to a real deck abutment.

    Prefers ``|dip| ≤ tol_m`` for ``run_m`` consecutive meters (road ≈ terrain).
    Returns ``(s, reason, dip_at_s)`` at the gap-facing end of that run so the
    MeshRoad lands on solid approach instead of a cut/gorge sample.
    """
    road_len = float(road["length"])
    step = max(0.25, float(step))
    search_m = max(step, float(search_m))
    run_m = max(step, float(run_m))
    samples: list[tuple[float, float]] = []
    nsteps = int(search_m / step) + 1
    for i in range(nsteps):
        s_i = s_edge + direction * i * step
        if s_i < -1e-9 or s_i > road_len + 1e-9:
            break
        s_i = max(0.0, min(road_len, s_i))
        dip, _rz, _tc = road_terrain_dip(road, z_terrain, s_i)
        samples.append((s_i, dip))
        if i > 0 and abs(samples[-1][0] - samples[-2][0]) < 1e-9:
            break

    need = max(1, int(math.ceil(run_m / step)))
    if len(samples) >= need:
        for i in range(len(samples) - need + 1):
            window = samples[i : i + need]
            if all(abs(d) <= tol_m for _s, d in window):
                s_a, d_a = window[0]
                return s_a, "flat", d_a
        best_i = 0
        best_score = float("inf")
        for i in range(len(samples) - need + 1):
            score = sum(abs(d) for _s, d in samples[i : i + need]) / need
            if score < best_score:
                best_score = score
                best_i = i
        s_a, d_a = samples[best_i]
        return s_a, "best_dip", d_a

    if samples:
        s_a, d_a = samples[0]
        return s_a, "edge", d_a
    return s_edge, "edge", 0.0


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
    gip_pad = max(0.0, float(cfg.get("gip_pad_m") or 0.0))
    auto = bool(cfg.get("auto_span", True))
    dip_m = float(cfg["span_dip_m"])
    search_m = float(cfg["span_search_m"])
    abut_tol = float(cfg.get("abut_tol_m") if cfg.get("abut_tol_m") is not None else 0.25)
    abut_run = float(cfg.get("abut_run_m") if cfg.get("abut_run_m") is not None else 3.0)
    abut_search = float(cfg.get("abut_search_m") or search_m)
    abut_snap_z = bool(cfg.get("abut_snap_z", True))
    step = float(cfg.get("step_m") or 1.0)
    # Optional clamp so ditch edges don't invent crazy banking
    cf_max = float(cfg.get("crossfall_max") or 0.12)  # ~7 deg

    s_ends = [project_on_road(road, x, y)[0] for x, y in (xy_gip[0], xy_gip[-1])]
    s_g0, s_g1 = min(s_ends), max(s_ends)
    # Short GIP segments: pad ends so auto_span / extends have room to reach abutments.
    if gip_pad > 0.0:
        s_g0 = max(0.0, s_g0 - gip_pad)
        s_g1 = min(float(road["length"]), s_g1 + gip_pad)
    s_first = project_on_road(road, xy_gip[0][0], xy_gip[0][1])[0]
    s_last = project_on_road(road, xy_gip[-1][0], xy_gip[-1][1])[0]
    forward = s_last >= s_first

    s_dip0 = s_dip1 = None
    if auto:
        s_lo = max(0.0, s_g0 - search_m)
        s_hi = min(float(road["length"]), s_g1 + search_m)
        gap = []
        s = s_lo
        while s <= s_hi + 1e-9:
            dip, _rz, _tc = road_terrain_dip(road, z_terrain, s)
            gap.append((s, dip >= dip_m, dip))
            s += step
        if any(g for _s, g, _d in gap):
            s_dip0 = next(s for s, g, _d in gap if g)
            s_dip1 = next(s for s, g, _d in reversed(gap) if g)
            # Always cover the (padded) GIP. Dip search may grow further out, but must
            # not shrink past GIP — alpine approaches often have road Z ≥ terrain (cut),
            # so "before" has no dip while "after" does → one-sided spans.
            s_gap0 = min(s_g0, s_dip0)
            s_gap1 = max(s_g1, s_dip1)
        else:
            s_gap0, s_gap1 = s_g0, s_g1
        max_dip = max((d for _s, _g, d in gap), default=0.0)
    else:
        s_gap0, s_gap1 = s_g0, s_g1
        max_dip = None

    # Portals on real abutments (road ≈ terrain), not on cut/gorge samples at gap edges.
    abut0, abut0_why, abut0_dip = find_flat_abutment(
        road,
        z_terrain,
        s_gap0,
        -1.0,
        tol_m=abut_tol,
        run_m=abut_run,
        search_m=abut_search,
        step=step,
    )
    abut1, abut1_why, abut1_dip = find_flat_abutment(
        road,
        z_terrain,
        s_gap1,
        +1.0,
        tol_m=abut_tol,
        run_m=abut_run,
        search_m=abut_search,
        step=step,
    )
    abut0 = min(abut0, s_gap0)
    abut1 = max(abut1, s_gap1)

    if forward:
        s0 = max(0.0, abut0 - ext_b)
        s1 = min(float(road["length"]), abut1 + ext_a)
    else:
        s0 = max(0.0, abut0 - ext_a)
        s1 = min(float(road["length"]), abut1 + ext_b)

    if s1 - s0 < step:
        s1 = min(float(road["length"]), s0 + step)

    # Portal frames: longitudinal from road, Querneigung from heightmap
    x0, y0, z0r, tx0, ty0, tz0, w0r = sample_road(road, s0)
    x1, y1, z1r, tx1, ty1, tz1, w1r = sample_road(road, s1)
    w0 = w0r if width_from_road else width_fallback
    w1 = w1r if width_from_road else width_fallback
    tc0 = float(z_terrain(x0, y0))
    tc1 = float(z_terrain(x1, y1))
    if abut_snap_z:
        z0 = max(z0r, tc0) + lift
        z1 = max(z1r, tc1) + lift
    else:
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
        "depth_m": depth,
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
        "abut_s0": round(abut0, 2),
        "abut_s1": round(abut1, 2),
        "abut_why": [abut0_why, abut1_why],
        "abut_dip_m": [round(abut0_dip, 3), round(abut1_dip, 3)],
        "abut_tol_m": abut_tol,
        "abut_run_m": abut_run,
        "abut_snap_z": abut_snap_z,
        "dip_s0": None if s_dip0 is None else round(s_dip0, 2),
        "dip_s1": None if s_dip1 is None else round(s_dip1, 2),
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
        "gip_pad_m": gip_pad,
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


def make_meshroad_from_strip(
    name: str,
    strip_nodes: list[tuple[float, float, float, float, float, float, float]],
    *,
    depth_m: float,
    materials: dict,
) -> dict:
    """Strip node = (x, y, z, width, nx, ny, nz)."""
    xy = [(n[0], n[1]) for n in strip_nodes]
    zs = [n[2] for n in strip_nodes]
    widths = [n[3] for n in strip_nodes]
    normals = [(n[4], n[5], n[6]) for n in strip_nodes]
    return make_meshroad(name, xy, zs, normals, widths, depth_m=depth_m, materials=materials)


def load_strassennetz_road(proc: Path) -> dict:
    import road_span_profile as rsp

    path = proc / "strassennetz_beamng.json"
    if not path.is_file():
        raise SystemExit(
            f"Missing {path} — run: python tools\\fetch_strassennetz.py"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    return rsp.road_dict_from_strassennetz_json(data)


def build_bridge_entries_road_spline(
    feat: dict,
    road: dict,
    z_terrain,
    cfg: dict,
) -> tuple[list[dict], dict]:
    """One GIP bridge → 4 MeshRoad strips via road_span_profile."""
    import road_span_profile as rsp

    abut = cfg.get("abutment_s")
    if abut is not None:
        abut = [float(abut[0]), float(abut[1])]
    prof = rsp.build_span_profile(
        road,
        z_terrain,
        feat["xy"],
        width_m=float(cfg["width_m"]),
        dip_m=float(cfg.get("span_dip_m") or 0.45),
        search_m=float(cfg.get("span_search_m") or 40.0),
        step_m=float(cfg.get("step_m") or 1.0),
        solid_run_m=float(cfg.get("solid_run_m") or 3.0),
        extend_before_m=float(cfg.get("extend_before_m") or 0.0),
        extend_after_m=float(cfg.get("extend_after_m") or 0.0),
        deck_lift_m=float(cfg.get("deck_lift_m") or 0.0),
        abutment_s=abut,
        free_span=str(cfg.get("free_span") or "linear"),
        corner_up_m=float(cfg.get("corner_up_m") if cfg.get("corner_up_m") is not None else 0.25),
        corner_down_m=float(cfg.get("corner_down_m") if cfg.get("corner_down_m") is not None else 0.06),
        corner_band=float(cfg.get("corner_band") if cfg.get("corner_band") is not None else 0.2),
    )
    info = dict(prof.info)
    info["name"] = feat["name"]
    info["objectid"] = feat.get("objectid")
    info["match"] = cfg.get("match_id")
    info["road_name"] = road.get("name")
    info["road_len_m"] = round(float(road["length"]), 1)
    info["road_stitched"] = False
    info["centerline"] = "strassennetz"
    info["node_z_is_top"] = bool(cfg.get("node_z_is_top", True))
    info["depth_m"] = float(cfg["depth_m"])

    # Under-mask along centerline between abutments (inset)
    inset = float(cfg.get("under_inset_m") or 0.0)
    s_u0 = prof.abut_s0 + inset
    s_u1 = prof.abut_s1 - inset
    under_xyw: list[list[float]] = []
    step = float(cfg.get("step_m") or 1.0)
    if s_u1 - s_u0 >= 0.25:
        s_u = s_u0
        while s_u <= s_u1 + 1e-9:
            x, y, _z, _tx, _ty, _tz, _w = rsp.sample_road_xy(road, s_u)
            under_xyw.append([round(x, 3), round(y, 3), round(float(cfg["width_m"]), 2)])
            s_u += step
        if not under_xyw or abs(under_xyw[-1][0] - x) > 0.01 or abs(under_xyw[-1][1] - y) > 0.01:
            x, y, *_ = rsp.sample_road_xy(road, s_u1)
            under_xyw.append([round(x, 3), round(y, 3), round(float(cfg["width_m"]), 2)])
    info["under_nodes_xyw"] = under_xyw
    info["under_s0"] = round(s_u0, 2)
    info["under_s1"] = round(s_u1, 2)
    info["under_len_m"] = round(max(0.0, s_u1 - s_u0), 2)
    info["under_inset_m"] = inset
    info["gap_s0"] = info["abut_s0"]
    info["gap_s1"] = info["abut_s1"]
    info["extend_before_m"] = float(cfg.get("extend_before_m") or 0.0)
    info["extend_after_m"] = float(cfg.get("extend_after_m") or 0.0)

    slug = _slug(feat["name"])
    oid = feat.get("objectid") or "x"
    entries = []
    for i, strip in enumerate(prof.strips):
        entries.append(
            make_meshroad_from_strip(
                f"bridge_{slug}_{oid}_s{i}",
                strip,
                depth_m=float(cfg["depth_m"]),
                materials=cfg["materials"],
            )
        )
    # Combined nodes_xyw for decks (centerline)
    info["nodes_xyw"] = [
        [round(xy[0], 3), round(xy[1], 3), round(w, 2)]
        for xy, w in zip(prof.xy, prof.width)
    ]
    return entries, info


def _to_px_beamng(bx: float, by: float, size: int, extent: float) -> tuple[float, float]:
    px = bx / extent * (size - 1)
    py = (1.0 - by / extent) * (size - 1)
    return px, py


def _from_px_beamng(px: float, py: float, size: int, extent: float) -> tuple[float, float]:
    bx = px / max(size - 1, 1) * extent
    by = (1.0 - py / max(size - 1, 1)) * extent
    return bx, by


def _project_xy_to_nodes(
    bx: float,
    by: float,
    nodes: list[dict],
) -> tuple[float, float, float] | None:
    """Nearest point on deck polyline → (dist_m, z_surf, half_width)."""
    if len(nodes) < 1:
        return None
    best: tuple[float, float, float] | None = None
    for i, n in enumerate(nodes):
        x0, y0 = float(n["x"]), float(n["y"])
        z0 = float(n["z"])
        w0 = 0.5 * float(n.get("width") or 7.0)
        if i == len(nodes) - 1:
            cand = (math.hypot(bx - x0, by - y0), z0, w0)
        else:
            n1 = nodes[i + 1]
            x1, y1 = float(n1["x"]), float(n1["y"])
            z1 = float(n1["z"])
            w1 = 0.5 * float(n1.get("width") or 7.0)
            dx, dy = x1 - x0, y1 - y0
            seg2 = dx * dx + dy * dy
            t = 0.0 if seg2 < 1e-12 else max(0.0, min(1.0, ((bx - x0) * dx + (by - y0) * dy) / seg2))
            px = x0 + t * dx
            py = y0 + t * dy
            cand = (math.hypot(bx - px, by - py), z0 + t * (z1 - z0), w0 + t * (w1 - w0))
        if best is None or cand[0] < best[0]:
            best = cand
    return best


def meshroad_surface_nodes(entry: dict, info: dict) -> list[dict]:
    """MeshRoad nodes as {x,y,z,width} with Z = driving surface."""
    depth = float(info.get("depth_m") or (entry.get("nodes") or [[0, 0, 0, 0, 0.4]])[0][4] or 0.4)
    top = bool(info.get("node_z_is_top", False))
    out = []
    for n in entry.get("nodes") or []:
        if len(n) < 4:
            continue
        z = float(n[2]) if top else float(n[2]) + 0.5 * depth
        out.append({"x": float(n[0]), "y": float(n[1]), "z": z, "width": float(n[3])})
    return out


def conform_bridge_heightmap(
    entries: list[dict],
    span_infos: list[dict],
    cfgs: list[dict],
    *,
    size: int,
    extent: float,
    elev: np.ndarray,
) -> tuple[np.ndarray, dict]:
    """Bake heightmap to MeshRoad surface under each deck ribbon.

    Only cells with |terrain − deck| ≤ max_delta are touched, so gorge air
    gaps stay intact while abutment lips / cut mismatches flush to the deck.
    """
    out = elev.astype(np.float64).copy()
    tested = 0
    changed = 0
    max_abs = 0.0
    any_on = False

    for entry, info, cfg in zip(entries, span_infos, cfgs):
        if not bool(cfg.get("approach_conform", False)):
            continue
        any_on = True
        nodes = meshroad_surface_nodes(entry, info)
        if len(nodes) < 2:
            continue
        pad = float(cfg.get("approach_conform_pad_m") if cfg.get("approach_conform_pad_m") is not None else 0.5)
        falloff = float(
            cfg.get("approach_conform_falloff_m")
            if cfg.get("approach_conform_falloff_m") is not None
            else 1.5
        )
        sink = float(
            cfg.get("approach_conform_sink_m")
            if cfg.get("approach_conform_sink_m") is not None
            else 0.02
        )
        max_delta = float(
            cfg.get("approach_conform_max_delta_m")
            if cfg.get("approach_conform_max_delta_m") is not None
            else 1.0
        )
        half_ref = 0.5 * max(float(n["width"]) for n in nodes)
        xs = [float(n["x"]) for n in nodes]
        ys = [float(n["y"]) for n in nodes]
        margin = half_ref + pad + falloff + 1.0
        px0, py0 = _to_px_beamng(min(xs) - margin, max(ys) + margin, size, extent)
        px1, py1 = _to_px_beamng(max(xs) + margin, min(ys) - margin, size, extent)
        c0 = max(0, int(math.floor(min(px0, px1))))
        c1 = min(size - 1, int(math.ceil(max(px0, px1))))
        r0 = max(0, int(math.floor(min(py0, py1))))
        r1 = min(size - 1, int(math.ceil(max(py0, py1))))

        for py in range(r0, r1 + 1):
            for px in range(c0, c1 + 1):
                bx, by = _from_px_beamng(float(px), float(py), size, extent)
                proj = _project_xy_to_nodes(bx, by, nodes)
                if proj is None:
                    continue
                dist, z_deck, hw = proj
                half = hw + pad
                outer = half + max(falloff, 1e-6)
                if dist > outer:
                    continue
                tested += 1
                target = float(z_deck) - sink
                z0 = float(out[py, px])
                if abs(z0 - target) > max_delta:
                    continue
                if dist <= half:
                    w = 1.0
                else:
                    w = 1.0 - (dist - half) / max(falloff, 1e-6)
                    w = max(0.0, min(1.0, w))
                z_new = (1.0 - w) * z0 + w * target
                if abs(z_new - z0) < 1e-4:
                    continue
                out[py, px] = z_new
                changed += 1
                max_abs = max(max_abs, abs(z_new - z0))

    stats = {
        "enabled": any_on,
        "tested": tested,
        "changed": changed,
        "max_delta_m": round(max_abs, 3),
        "method": "bridge_approach_conform",
    }
    if any_on:
        print(
            f"Bridge approach conform: tested={tested} changed={changed} "
            f"max_delta={max_abs:.3f}m (heightmap → MeshRoad Z)"
        )
    return out, stats


def write_bridge_conform_heightmap(
    proc: Path,
    level_name: str,
    *,
    elev: np.ndarray,
    max_height_m: float,
) -> Path:
    """Write conformed heightmap into processed/ and sync level import/."""
    from PIL import Image

    size = int(elev.shape[0])
    u16 = np.clip(
        np.round(elev / max(max_height_m, 1e-6) * 65535.0),
        0,
        65535,
    ).astype(np.uint16)
    carved_name = f"heightmap_{size}_bridge_conform.png"
    carved_path = proc / carved_name
    Image.fromarray(u16, mode="I;16").save(carved_path)

    preset_path = proc / "terrainPreset.json"
    preset: dict = {}
    if preset_path.is_file():
        try:
            preset = json.loads(preset_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            preset = {}
    preset.setdefault("type", "TerrainData")
    preset.setdefault("name", "theTerrain")
    preset["heightScale"] = float(max_height_m)
    preset["heightMapPath"] = f"/levels/{level_name}/import/heightmap_{size}.png"
    preset_path.write_text(json.dumps(preset, indent=2), encoding="utf-8")

    user_import = USER_LEVELS / level_name / "import"
    if user_import.parent.is_dir():
        user_import.mkdir(parents=True, exist_ok=True)
        Image.fromarray(u16, mode="I;16").save(user_import / f"heightmap_{size}.png")
        (user_import / "terrainPreset.json").write_text(
            json.dumps(preset, indent=2), encoding="utf-8"
        )
        print(f"Synced bridge-conform heightmap -> {user_import}")
        print("Re-import terrainPreset.json in World Editor (heightmap changed).")
    return carved_path


def restore_pristine_heightmap(proc: Path, level_name: str, size: int) -> None:
    """Copy pristine DGM heightmap back into import/ when conform is off."""
    import shutil

    src = proc / f"heightmap_{size}.png"
    user_import = USER_LEVELS / level_name / "import"
    if src.is_file() and user_import.parent.is_dir():
        user_import.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, user_import / f"heightmap_{size}.png")
        print(f"Restored DGM heightmap -> {user_import / f'heightmap_{size}.png'}")


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
    """Register MeshRoad/DecalRoad materials; Asphalt matched to terrain paint.

    Terrain ``Asphalt`` blends ``t_terrain_base_asphalt`` + detail ``t_asphalt_02``
    (+ macro). Mesh/Decal cannot use TerrainMaterial, so we approximate the same
    stack on a regular Material and keep one shared ``Asphalt`` name.
    """
    mats_path = user_level / "art" / "road" / "main.materials.json"
    mats_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if mats_path.is_file() and mats_path.stat().st_size:
        try:
            data = json.loads(mats_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}

    terr = f"/levels/{level_name}/art/terrains"
    # Detail grain vs base: terrain detailStrength≈0.3; keep a mild overlay.
    detail_scale = float(materials.get("detail_scale") or 4.0)
    asphalt_name = materials.get("top") or "Asphalt"
    wanted = {
        asphalt_name: _mesh_material(
            asphalt_name,
            # Same maps the TerrainMaterial uses for base + close-up detail.
            base_color=f"{terr}/t_terrain_base_asphalt_b.png",
            normal=f"{terr}/t_terrain_base_asphalt_nm.png",
            roughness=f"{terr}/t_terrain_base_asphalt_r.png",
            ao=f"{terr}/t_terrain_base_asphalt_ao.png",
            annotation="ASPHALT",
            persistent_id="a070a5a1-b71d-4e1e-9c11-0000a5fa1701",
            # No heavy darkening — terrain base is already the right value.
            base_color_factor=[0.92, 0.92, 0.90, 1.0],
            detail_scale=detail_scale,
            detail_map=f"{terr}/t_asphalt_02_b.png",
            detail_normal=f"{terr}/t_asphalt_02_nm.png",
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
    # Mild detail normals (terrain normalDetailStrength≈0.6/0.2).
    asp_stage = wanted[asphalt_name]["Stages"][0]
    if "detailNormalMap" in asp_stage:
        asp_stage["detailNormalMapStrength"] = 0.45
    for name, mat in wanted.items():
        data[name] = mat
    mats_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(
        f"MeshRoad materials in {mats_path.name}: {', '.join(sorted(set(wanted)))} "
        f"(detail_scale={detail_scale}, asphalt≈terrain base+02)"
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
        "gip_pad_m": float(raw.get("gip_pad_m") or 0.0),
        "auto_span": bool(raw.get("auto_span", True)),
        "span_dip_m": float(raw.get("span_dip_m") or 0.55),
        "span_search_m": float(raw.get("span_search_m") or 25.0),
        "abut_tol_m": float(raw.get("abut_tol_m") if raw.get("abut_tol_m") is not None else 0.25),
        "abut_run_m": float(raw.get("abut_run_m") if raw.get("abut_run_m") is not None else 3.0),
        "abut_search_m": (
            float(raw["abut_search_m"]) if raw.get("abut_search_m") is not None else None
        ),
        "abut_snap_z": bool(raw.get("abut_snap_z", True)),
        "approach_conform": bool(raw.get("approach_conform", False)),
        "approach_conform_pad_m": float(
            raw.get("approach_conform_pad_m")
            if raw.get("approach_conform_pad_m") is not None
            else 0.5
        ),
        "approach_conform_falloff_m": float(
            raw.get("approach_conform_falloff_m")
            if raw.get("approach_conform_falloff_m") is not None
            else 1.5
        ),
        "approach_conform_sink_m": float(
            raw.get("approach_conform_sink_m")
            if raw.get("approach_conform_sink_m") is not None
            else 0.02
        ),
        "approach_conform_max_delta_m": float(
            raw.get("approach_conform_max_delta_m")
            if raw.get("approach_conform_max_delta_m") is not None
            else 1.0
        ),
        "profile": str(raw.get("profile") or "hermite"),
        "centerline": str(raw.get("centerline") or "osm"),
        "solid_run_m": float(raw.get("solid_run_m") if raw.get("solid_run_m") is not None else 3.0),
        "free_span": str(raw.get("free_span") or "linear"),
        "abutment_s": raw.get("abutment_s"),
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
            "texture_length": 6.0,
            "detail_scale": 4.0,
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
    defaults, items = default_bridge_cfg(bng)
    if args.step is not None:
        defaults["step_m"] = args.step

    use_spline = str(defaults.get("profile") or "hermite") == "road_spline"
    use_net = str(defaults.get("centerline") or "osm") == "strassennetz" or use_spline

    roads = load_road_polylines(proc)
    net_road = None
    if use_net:
        net_road = load_strassennetz_road(proc)
        print(
            f"Centerline Strassennetz: {net_road.get('name')!r} "
            f"len={net_road['length']:.1f}m nodes={len(net_road['pts'])}"
        )
    elif not roads:
        raise SystemExit(f"Missing {proc / 'roads_beamng.json'}")

    z_terrain = load_terrain_z(site)
    feats = bridge_features(site, sc)
    if not feats:
        raise SystemExit("No bridge features in GIP cache for this site")

    entries = []
    span_infos = []
    bridge_cfgs = []
    for feat in feats:
        cfg = resolve_bridge_cfg(defaults, items, feat)
        bridge_cfgs.append(cfg)
        if str(cfg.get("profile") or "hermite") == "road_spline":
            road = net_road if net_road is not None else load_strassennetz_road(proc)
            strip_entries, info = build_bridge_entries_road_spline(
                feat, road, z_terrain, cfg
            )
            span_infos.append(info)
            entries.extend(strip_entries)
            zc = info.get("abut") or {}
            print(
                f"{feat['name']} oid={feat.get('objectid')}: "
                f"profile=road_spline strips={len(strip_entries)} "
                f"span={info['span_len_m']}m gap={info.get('gap_len_m')}m "
                f"under={info.get('under_len_m')}m "
                f"abut={info.get('abut_s0')}/{info.get('abut_s1')} "
                f"method={zc.get('method')} "
                f"on {info.get('road_name')!r} "
                f"ext=({cfg['extend_before_m']}/{cfg['extend_after_m']})"
            )
            continue

        if not roads:
            raise SystemExit(f"Missing {proc / 'roads_beamng.json'}")
        road = pick_road(roads, feat["xy"])
        road = stitch_road_chain(roads, road)
        xy, zs, normals, widths, info = build_span_on_road(feat["xy"], road, z_terrain, cfg)
        info["name"] = feat["name"]
        info["objectid"] = feat.get("objectid")
        info["match"] = cfg.get("match_id")
        info["road_len_m"] = round(float(road["length"]), 1)
        info["road_stitched"] = bool(road.get("stitched"))
        info["nodes_xyw"] = [
            [round(x, 3), round(y, 3), round(w, 2)] for (x, y), w in zip(xy, widths)
        ]
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
            f"under={info.get('under_len_m')}m "
            f"nodes={info['nodes']} "
            f"on road {info['road_name']!r} "
            f"(len={info.get('road_len_m')}m stitched={info.get('road_stitched')}) "
            f"abut={info.get('abut_s0')}/{info.get('abut_s1')} "
            f"({info.get('abut_why')}) "
            f"portal_w={info['portal_width_m']} "
            f"ext=({cfg['extend_before_m']}/{cfg['extend_after_m']}) "
            f"surface_z={min(surf):.2f}..{max(surf):.2f}"
        )

    out_json = proc / "bridges_items.level.json"
    with out_json.open("w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")

    decks = []
    for info in span_infos:
        decks.append(
            {
                "name": info.get("name"),
                "objectid": info.get("objectid"),
                "nodes_xyw": info.get("nodes_xyw") or [],
                "under_nodes_xyw": info.get("under_nodes_xyw") or [],
                "span_len_m": info.get("span_len_m"),
                "gap_len_m": info.get("gap_len_m"),
                "under_len_m": info.get("under_len_m"),
                "under_inset_m": info.get("under_inset_m"),
                "extend_before_m": info.get("extend_before_m"),
                "extend_after_m": info.get("extend_after_m"),
                "profile": info.get("profile"),
            }
        )
    decks_path = proc / "bridges_decks.json"
    decks_path.write_text(
        json.dumps({"level": level_name, "decks": decks}, indent=2),
        encoding="utf-8",
    )

    meta = {
        "count": len(entries),
        "spans": len(span_infos),
        "level": level_name,
        "defaults": {k: defaults[k] for k in BRIDGE_SCALAR_KEYS},
        "style_defaults": defaults["style"],
        "materials": defaults["materials"],
        "spans_detail": span_infos,
        "decks_file": str(decks_path.relative_to(ROOT)).replace("\\", "/"),
        "note": "profile=road_spline: Strassennetz + 4 MeshRoad strips; hermite: legacy.",
    }
    (proc / "bridges_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {out_json} ({len(entries)} MeshRoads, {len(span_infos)} spans)")
    print(f"Wrote {decks_path}")

    size = int(bng.get("mask_size") or 512)
    extent = float(bng.get("meters_per_pixel") or 1.0) * float(size)
    any_conform = any(bool(c.get("approach_conform")) for c in bridge_cfgs)
    if any_conform and not use_spline:
        from PIL import Image

        hm_path = proc / f"heightmap_{size}.png"
        meta_hm_path = proc / "heightmap_meta.json"
        if not hm_path.is_file() or not meta_hm_path.is_file():
            raise SystemExit(f"Missing heightmap for approach_conform ({hm_path})")
        elev0 = np.asarray(Image.open(hm_path), dtype=np.float64)
        max_h = float(json.loads(meta_hm_path.read_text(encoding="utf-8"))["max_height_m"])
        elev_m = elev0 / 65535.0 * max_h
        elev1, conf_stats = conform_bridge_heightmap(
            entries, span_infos, bridge_cfgs, size=size, extent=extent, elev=elev_m
        )
        carved = write_bridge_conform_heightmap(
            proc, level_name, elev=elev1, max_height_m=max_h
        )
        meta["approach_conform"] = conf_stats
        meta["approach_conform_heightmap"] = str(carved.relative_to(ROOT)).replace("\\", "/")
        (proc / "bridges_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    elif not any_conform:
        restore_pristine_heightmap(proc, level_name, size)

    level_path = write_level(level_name, entries, defaults["materials"])
    if level_path:
        print(f"Injected: {level_path}")
        print("Reload the level in BeamNG (materials may need World Editor refresh).")
    else:
        print("Skipped inject (create level folder first).")


if __name__ == "__main__":
    main()
