"""Extrude OSM building footprints → silhouette Collada + TSStatic.

Per-building style (seeded by osm_id):
  - wall / roof Lambert colors
  - slight height jitter
  - gable roof when footprint is box-like enough

Outputs under data/processed/<site>/:
  - osm_buildings.json (cache)
  - building_meshes/*.dae
  - buildings_items.level.json
  - buildings_meta.json

Usage:
  $env:AUTOROAD_SITE='config/sites/fernpass_mega.yaml'
  $env:PYTHONIOENCODING='utf-8'
  python tools/build_buildings.py
"""
from __future__ import annotations

import json
import math
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import requests
from pyproj import Transformer
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient
from shapely.ops import triangulate
from shapely.validation import make_valid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import SiteCoords, load_site, processed_dir, site_slug  # noqa: E402
import build_bridges as bb  # noqa: E402

IDENTITY_ROT = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]

# Alpine / village wall tones (RGB 0..1). BeamNG uses these via materials.json.
WALL_PALETTE: list[tuple[float, float, float]] = [
    (0.94, 0.90, 0.78),  # helles Beige (gewünscht)
    (0.93, 0.91, 0.86),  # cream
    (0.88, 0.84, 0.76),  # beige
    (0.82, 0.78, 0.70),  # sand
    (0.90, 0.88, 0.88),  # light grey
    (0.94, 0.94, 0.92),  # whitewash
    (0.86, 0.80, 0.72),  # plaster
]

# Roof tones — dark grey prominent
ROOF_PALETTE: list[tuple[float, float, float]] = [
    (0.22, 0.22, 0.24),  # Dunkelgrau (gewünscht)
    (0.28, 0.28, 0.30),  # charcoal
    (0.30, 0.32, 0.36),  # slate
    (0.42, 0.22, 0.18),  # terracotta
    (0.35, 0.28, 0.24),  # dark brown
    (0.38, 0.20, 0.16),  # deep red tile
]


@dataclass
class BuildingStyle:
    wall_rgb: tuple[float, float, float]
    roof_rgb: tuple[float, float, float]
    height_scale: float
    roof_kind: str  # "flat" | "gable"
    gable_pitch: float  # ridge height / half-width


@dataclass
class BuildingMesh:
    verts: list[tuple[float, float, float]]
    wall_faces: list[tuple[int, int, int]]
    roof_faces: list[tuple[int, int, int]]
    floor_faces: list[tuple[int, int, int]]
    roof_kind: str


def _cfg(site: dict) -> dict:
    raw = ((site.get("beamng") or {}).get("buildings") or {})
    return {
        "enabled": bool(raw.get("enabled", True)),
        "default_height_m": float(raw.get("default_height_m", 8.0)),
        "level_height_m": float(raw.get("level_height_m", 3.0)),
        "min_area_m2": float(raw.get("min_area_m2", 25.0)),
        "max_area_m2": float(raw.get("max_area_m2", 8000.0)),
        "max_items": int(raw.get("max_items", 5000)),
        "sink_m": float(raw.get("sink_m", 1.5)),
        "force_fetch": bool(raw.get("force_fetch", False)),
        "height_jitter": float(raw.get("height_jitter", 0.12)),
        "gable_chance": float(raw.get("gable_chance", 0.72)),
        "gable_fill_min": float(raw.get("gable_fill_min", 0.88)),
        "style_seed": int(raw.get("style_seed", 17)),
    }


def _slug(s: str) -> str:
    s = re.sub(r"[^\w\-]+", "_", s, flags=re.UNICODE).strip("_")
    return (s or "bldg")[:48]


def _rng_for(osm_id, style_seed: int) -> random.Random:
    try:
        oid = int(osm_id)
    except (TypeError, ValueError):
        oid = abs(hash(str(osm_id))) % (2**31)
    return random.Random((oid * 1000003) ^ int(style_seed))


def pick_style(osm_id, building_type: str, cfg: dict, *, can_gable: bool) -> BuildingStyle:
    rng = _rng_for(osm_id, cfg["style_seed"])
    # helles Beige ~35%; sonst restliche Palette
    if rng.random() < 0.35:
        wall = WALL_PALETTE[0]
    else:
        wall = WALL_PALETTE[1 + rng.randrange(max(1, len(WALL_PALETTE) - 1))]
    # Dunkelgrau oft, aber nicht immer
    if rng.random() < 0.40:
        roof = ROOF_PALETTE[0]
    else:
        roof = ROOF_PALETTE[1 + rng.randrange(max(1, len(ROOF_PALETTE) - 1))]
    jitter = float(cfg["height_jitter"])
    hscale = 1.0 + rng.uniform(-jitter, jitter) if jitter > 0 else 1.0
    b = (building_type or "yes").lower()
    flat_bias = b in {"industrial", "warehouse", "garage", "garages", "shed", "hangar", "retail"}
    chance = float(cfg["gable_chance"]) * (0.25 if flat_bias else 1.0)
    use_gable = bool(can_gable and rng.random() < chance)
    pitch = rng.uniform(0.35, 0.55)
    return BuildingStyle(
        wall_rgb=wall,
        roof_rgb=roof,
        height_scale=hscale,
        roof_kind="gable" if use_gable else "flat",
        gable_pitch=pitch,
    )


def fetch_osm_buildings(site: dict, proc: Path, *, force: bool = False) -> dict:
    cache = proc / "osm_buildings.json"
    raw_cache = ROOT / "data" / "raw" / f"osm_buildings_{site_slug(site)}.json"
    if not force:
        for p in (cache, raw_cache):
            if p.is_file() and p.stat().st_size > 100:
                print(f"Using cached buildings: {p}")
                return json.loads(p.read_text(encoding="utf-8"))

    sc = SiteCoords(site)
    to_wgs = Transformer.from_crs(sc.crs, "EPSG:4326", always_xy=True)
    w, s = to_wgs.transform(sc.xmin, sc.ymin)
    e, n = to_wgs.transform(sc.xmax, sc.ymax)
    query = f"""
    [out:json][timeout:180];
    (
      way["building"]({s},{w},{n},{e});
      relation["building"]({s},{w},{n},{e});
    );
    out geom;
    """
    headers = {"User-Agent": "beamng_autoroad/0.1", "Accept": "application/json"}
    data = None
    last_err = None
    for url in (
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass.private.coffee/api/interpreter",
    ):
        try:
            r = requests.post(url, data={"data": query}, headers=headers, timeout=240)
            r.raise_for_status()
            data = r.json()
            print(f"Overpass buildings OK via {url} elements={len(data.get('elements', []))}")
            break
        except Exception as ex:  # noqa: BLE001
            last_err = ex
            print(f"Overpass failed at {url}: {ex}")
    if data is None:
        raise SystemExit(f"OSM buildings fetch failed: {last_err}")

    raw_cache.parent.mkdir(parents=True, exist_ok=True)
    raw_cache.write_text(json.dumps(data), encoding="utf-8")
    cache.write_text(json.dumps(data), encoding="utf-8")
    return data


def _parse_height_m(tags: dict, cfg: dict) -> float:
    h = tags.get("height") or tags.get("building:height")
    if h not in (None, ""):
        s = str(h).strip().lower().replace("m", "").replace(",", ".")
        for sep in (";", "|", "/"):
            if sep in s:
                s = s.split(sep)[0].strip()
                break
        try:
            return max(2.5, float(s))
        except ValueError:
            pass
    levels = tags.get("building:levels") or tags.get("levels")
    if levels not in (None, ""):
        s = str(levels).strip().replace(",", ".")
        for sep in (";", "|", "/"):
            if sep in s:
                s = s.split(sep)[0].strip()
                break
        try:
            return max(2.5, float(s) * float(cfg["level_height_m"]))
        except ValueError:
            pass
    b = str(tags.get("building") or "yes").lower()
    defaults = {
        "house": 7.0,
        "detached": 7.0,
        "semidetached_house": 7.5,
        "terrace": 8.0,
        "apartments": 14.0,
        "residential": 10.0,
        "hotel": 12.0,
        "commercial": 9.0,
        "retail": 8.0,
        "industrial": 9.0,
        "warehouse": 10.0,
        "farm": 6.0,
        "barn": 6.0,
        "church": 16.0,
        "chapel": 10.0,
        "school": 10.0,
        "garage": 3.0,
        "garages": 3.0,
        "shed": 3.0,
        "hut": 3.5,
        "cabin": 5.0,
    }
    return float(defaults.get(b, cfg["default_height_m"]))


def _way_ring_crs(el: dict, to_crs: Transformer) -> list[tuple[float, float]] | None:
    geom = el.get("geometry") or []
    if len(geom) < 3:
        return None
    pts: list[tuple[float, float]] = []
    for g in geom:
        x, y = to_crs.transform(float(g["lon"]), float(g["lat"]))
        pts.append((x, y))
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    return pts if len(pts) >= 4 else None


def _poly_from_ring(ring: list[tuple[float, float]]) -> Polygon | None:
    try:
        poly = Polygon(ring)
    except Exception:  # noqa: BLE001
        return None
    if not poly.is_valid:
        poly = make_valid(poly)
    if poly.is_empty:
        return None
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda g: g.area)
    if poly.geom_type != "Polygon" or poly.area <= 0:
        return None
    return poly


def _triangulate_xy(poly: Polygon) -> list[tuple[tuple[float, float], ...]]:
    tris: list[tuple[tuple[float, float], ...]] = []
    for t in triangulate(poly):
        if t.area <= 1e-6:
            continue
        if not poly.contains(t.centroid) and not poly.covers(t.centroid):
            continue
        coords = list(t.exterior.coords)[:3]
        if len(coords) == 3:
            tris.append(tuple((float(x), float(y)) for x, y in coords))
    return tris


def _tri_normal(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    c: tuple[float, float, float],
) -> tuple[float, float, float]:
    ax, ay, az = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    bx, by, bz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    return (ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx)


def _orient_faces(
    faces: list[tuple[int, int, int]],
    verts: list[tuple[float, float, float]],
    *,
    mode: str,
    edge_outward: dict[tuple[int, int], tuple[float, float]] | None = None,
) -> list[tuple[int, int, int]]:
    """mode: 'wall' | 'roof' | 'floor'.

    Walls: prefer edge-based outward (CCW ring → right-hand outward), not centroid
    (centroid fails on concave footprints).
    Roofs: primarily +Z; pitched faces also push outward in XY.
    """
    out: list[tuple[int, int, int]] = []
    for i0, i1, i2 in faces:
        a, b, c = verts[i0], verts[i1], verts[i2]
        nx, ny, nz = _tri_normal(a, b, c)
        ln = math.sqrt(nx * nx + ny * ny + nz * nz) + 1e-12
        nx, ny, nz = nx / ln, ny / ln, nz / ln
        mx = (a[0] + b[0] + c[0]) / 3.0
        my = (a[1] + b[1] + c[1]) / 3.0

        if mode == "floor":
            if nz > 0:
                i0, i1, i2 = i0, i2, i1
        elif mode == "roof":
            # Flat / near-flat: only +Z. Do NOT use centroid XY (≈0 → random flips).
            if nz < 0:
                i0, i1, i2 = i0, i2, i1
                nx, ny, nz = -nx, -ny, -nz
            elif abs(nz) < 0.85:
                # Pitched: also face away from footprint center
                if mx * nx + my * ny < 0:
                    i0, i1, i2 = i0, i2, i1
        else:  # wall
            flipped = False
            if edge_outward:
                for ea, eb in ((i0, i1), (i1, i2), (i2, i0)):
                    key = (ea, eb)
                    if key in edge_outward:
                        ox, oy = edge_outward[key]
                        if nx * ox + ny * oy < 0:
                            i0, i1, i2 = i0, i2, i1
                            flipped = True
                        break
                    # try reverse edge key
                    key_r = (eb, ea)
                    if key_r in edge_outward:
                        ox, oy = edge_outward[key_r]
                        # traveling opposite → outward flips sign
                        if nx * (-ox) + ny * (-oy) < 0:
                            i0, i1, i2 = i0, i2, i1
                            flipped = True
                        break
            if not flipped and not edge_outward:
                if mx * nx + my * ny < 0:
                    i0, i1, i2 = i0, i2, i1
            elif not flipped and edge_outward and abs(nz) < 0.2:
                # gable ends etc. without matching bottom edge: centroid fallback
                if mx * nx + my * ny < 0:
                    i0, i1, i2 = i0, i2, i1
        out.append((i0, i1, i2))
    return out


def _edge_outward_from_ring(verts: list[tuple[float, float, float]], n: int) -> dict[tuple[int, int], tuple[float, float]]:
    """For bottom verts 0..n-1 (CCW), map edge→outward XY."""
    out: dict[tuple[int, int], tuple[float, float]] = {}
    for i in range(n):
        j = (i + 1) % n
        dx = verts[j][0] - verts[i][0]
        dy = verts[j][1] - verts[i][1]
        # CCW travel → outward is to the right: (dy, -dx)
        ox, oy = dy, -dx
        ol = math.hypot(ox, oy) or 1.0
        out[(i, j)] = (ox / ol, oy / ol)
    return out


def _mat_name(prefix: str, rgb: tuple[float, float, float]) -> str:
    r, g, b = (int(round(max(0.0, min(1.0, c)) * 255)) for c in rgb)
    return f"{prefix}_{r:02x}{g:02x}{b:02x}"


def ensure_building_materials(
    user_level: Path,
    level_name: str,
    used: dict[str, tuple[float, float, float]],
) -> None:
    """Write BeamNG Materials so DAE mapTo names get real colors (Lambert alone is ignored)."""
    mats_path = user_level / "art" / "shapes" / "buildings" / "main.materials.json"
    mats_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if mats_path.is_file() and mats_path.stat().st_size:
        try:
            data = json.loads(mats_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    terr = f"/levels/{level_name}/art/terrains"
    for name, rgb in used.items():
        r, g, b = rgb
        rough = 0.88 if name.startswith("BldgRoof") else 0.92
        base_tex = (
            f"{terr}/t_terrain_base_concrete_b.png"
            if name.startswith("BldgRoof")
            else f"{terr}/t_terrain_base_dirt_b.png"
        )
        # Prefer a soft albedo if present; fallback concrete/dirt
        data[name] = {
            "name": name,
            "mapTo": name,
            "class": "Material",
            "persistentId": f"b1dg{abs(hash(name)) % 10**10:010d}",
            "Stages": [
                {
                    "baseColorMap": base_tex,
                    "roughnessFactor": rough,
                    "baseColorFactor": [round(r, 4), round(g, 4), round(b, 4), 1.0],
                },
                {},
                {},
                {},
            ],
            "annotation": "BUILDING",
            "materialTag0": "building",
            "materialTag1": "beamng",
            "version": 1.5,
        }
    mats_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {mats_path.name} ({len(used)} building materials)")


def _obb_corners(poly: Polygon) -> tuple[list[tuple[float, float]], float, float, float] | None:
    """Return (4 CCW corners centered later), length, width, fill_ratio."""
    mrr = poly.minimum_rotated_rectangle
    if mrr.is_empty or mrr.area <= 1e-9:
        return None
    fill = float(poly.area) / float(mrr.area)
    coords = list(orient(mrr, sign=1.0).exterior.coords)[:-1]
    if len(coords) != 4:
        return None
    corners = [(float(x), float(y)) for x, y in coords]
    e01 = math.hypot(corners[1][0] - corners[0][0], corners[1][1] - corners[0][1])
    e12 = math.hypot(corners[2][0] - corners[1][0], corners[2][1] - corners[1][1])
    length, width = max(e01, e12), min(e01, e12)
    return corners, length, width, fill


def _flat_parts(
    exterior: list[tuple[float, float]],
    height_m: float,
) -> BuildingMesh:
    n = len(exterior)
    z0, z1 = 0.0, float(height_m)
    verts: list[tuple[float, float, float]] = []
    for x, y in exterior:
        verts.append((x, y, z0))
    for x, y in exterior:
        verts.append((x, y, z1))

    edge_out = _edge_outward_from_ring(verts, n)
    wall: list[tuple[int, int, int]] = []
    for i in range(n):
        j = (i + 1) % n
        wall.append((i, j, j + n))
        wall.append((i, j + n, i + n))
    wall = _orient_faces(wall, verts, mode="wall", edge_outward=edge_out)

    local_poly = Polygon(exterior + [exterior[0]])
    roof: list[tuple[int, int, int]] = []
    floor: list[tuple[int, int, int]] = []
    for tri in _triangulate_xy(local_poly):
        idxs: list[int] = []
        for x, y in tri:
            best = min(range(n), key=lambda k: (exterior[k][0] - x) ** 2 + (exterior[k][1] - y) ** 2)
            idxs.append(best)
        if len(set(idxs)) != 3:
            continue
        a, b, c = idxs
        roof.append((a + n, b + n, c + n))
        floor.append((a, b, c))
    roof = _orient_faces(roof, verts, mode="roof")
    floor = _orient_faces(floor, verts, mode="floor")
    return BuildingMesh(verts, wall, roof, floor, "flat")


def _quad_tris(a: int, b: int, c: int, d: int) -> list[tuple[int, int, int]]:
    """Triangulate quad a→b→c→d with diagonal a–c."""
    return [(a, b, c), (a, c, d)]


def _gable_parts(
    corners: list[tuple[float, float]],
    height_m: float,
    pitch: float,
) -> BuildingMesh:
    """Extrude OBB rectangle with gable ridge along the long axis.

    Corners must be CCW. Ridge joins midpoints of the two *short* edges.
    Each roof plane is quad eave0→eave1→ridge_near_eave1→ridge_near_eave0.
    """
    c0, c1, c2, c3 = corners[0], corners[1], corners[2], corners[3]
    e01 = math.hypot(c1[0] - c0[0], c1[1] - c0[1])
    e12 = math.hypot(c2[0] - c1[0], c2[1] - c1[1])

    if e01 >= e12:
        half_w = e12 * 0.5
        ra = ((c1[0] + c2[0]) * 0.5, (c1[1] + c2[1]) * 0.5)  # mid short 1–2 → v8
        rb = ((c3[0] + c0[0]) * 0.5, (c3[1] + c0[1]) * 0.5)  # mid short 3–0 → v9
        # Long eaves 0–1 and 2–3
        roof_raw = _quad_tris(4, 5, 8, 9) + _quad_tris(6, 7, 9, 8)
        gable_ends = [(5, 6, 8), (7, 4, 9)]
    else:
        half_w = e01 * 0.5
        ra = ((c0[0] + c1[0]) * 0.5, (c0[1] + c1[1]) * 0.5)  # mid short 0–1 → v8
        rb = ((c2[0] + c3[0]) * 0.5, (c2[1] + c3[1]) * 0.5)  # mid short 2–3 → v9
        # Long eaves 1–2 and 3–0 — must use diagonal of the roof quad, not an eave–ridge edge
        roof_raw = _quad_tris(5, 6, 9, 8) + _quad_tris(7, 4, 8, 9)
        gable_ends = [(4, 5, 8), (6, 7, 9)]

    ridge_h = max(0.8, float(pitch) * max(half_w, 0.5))
    z_eave = float(height_m)
    z_ridge = z_eave + ridge_h

    verts: list[tuple[float, float, float]] = []
    for x, y in (c0, c1, c2, c3):
        verts.append((x, y, 0.0))
    for x, y in (c0, c1, c2, c3):
        verts.append((x, y, z_eave))
    verts.append((ra[0], ra[1], z_ridge))
    verts.append((rb[0], rb[1], z_ridge))

    edge_out = _edge_outward_from_ring(verts, 4)
    wall: list[tuple[int, int, int]] = []
    for i in range(4):
        j = (i + 1) % 4
        wall.append((i, j, j + 4))
        wall.append((i, j + 4, i + 4))
    wall.extend(gable_ends)
    wall = _orient_faces(wall, verts, mode="wall", edge_outward=edge_out)
    roof = _orient_faces(list(roof_raw), verts, mode="roof")
    floor = _orient_faces([(0, 1, 2), (0, 2, 3)], verts, mode="floor")
    return BuildingMesh(verts, wall, roof, floor, "gable")


def build_mesh(
    ring_centered: list[tuple[float, float]],
    height_m: float,
    style: BuildingStyle,
    *,
    fill_min: float,
) -> BuildingMesh:
    if ring_centered[0] != ring_centered[-1]:
        ring = list(ring_centered) + [ring_centered[0]]
    else:
        ring = list(ring_centered)
    poly = _poly_from_ring(ring)
    if poly is None:
        raise ValueError("invalid footprint")
    poly = orient(poly, sign=1.0)
    cx, cy = float(poly.centroid.x), float(poly.centroid.y)
    exterior = [(float(x) - cx, float(y) - cy) for x, y in poly.exterior.coords[:-1]]
    if len(exterior) < 3:
        raise ValueError("too few vertices")

    obb = _obb_corners(Polygon(exterior + [exterior[0]]))
    can_gable = False
    corners_local: list[tuple[float, float]] | None = None
    if obb is not None:
        corners, _length, width, fill = obb
        can_gable = fill >= fill_min and width >= 2.5
        # re-center OBB corners on centroid (already near 0)
        ocx = sum(c[0] for c in corners) / 4.0
        ocy = sum(c[1] for c in corners) / 4.0
        corners_local = [(c[0] - ocx, c[1] - ocy) for c in corners]

    if style.roof_kind == "gable" and can_gable and corners_local is not None:
        return _gable_parts(corners_local, height_m, style.gable_pitch)
    return _flat_parts(exterior, height_m)


def write_building_collada(
    path: Path,
    mesh: BuildingMesh,
    style: BuildingStyle,
    *,
    mesh_stem: str,
) -> dict[str, tuple[float, float, float]]:
    """Collada with per-color BeamNG mapTo names. Returns {mat_name: rgb} used."""
    verts = mesh.verts
    wall_mat = _mat_name("BldgWall", style.wall_rgb)
    roof_mat = _mat_name("BldgRoof", style.roof_rgb)
    floor_rgb = tuple(max(0.0, c * 0.55) for c in style.wall_rgb)
    floor_mat = _mat_name("BldgFloor", floor_rgb)  # type: ignore[arg-type]
    groups = [
        (wall_mat, style.wall_rgb, mesh.wall_faces),
        (roof_mat, style.roof_rgb, mesh.roof_faces),
        (floor_mat, floor_rgb, mesh.floor_faces),
    ]
    groups = [(n, rgb, faces) for n, rgb, faces in groups if faces]
    if not groups:
        raise ValueError("empty mesh")

    used = {n: rgb for n, rgb, _f in groups}
    all_faces: list[tuple[int, int, int]] = []
    for _n, _rgb, faces in groups:
        all_faces.extend(faces)

    n_vert = len(verts)
    pos_vals = " ".join(f"{x:.4f} {y:.4f} {z:.4f}" for x, y, z in verts)
    uv_vals = " ".join(f"{x / 4.0:.5f} {y / 4.0:.5f}" for x, y, _z in verts)
    col_p = " ".join(f"{a} {b} {c}" for a, b, c in all_faces)
    lod = f"{mesh_stem}_a999"

    effects = []
    materials = []
    triangles = []
    binds = []
    for name, rgb, faces in groups:
        r, g, b = rgb
        mat = escape(name)
        effects.append(
            f"""    <effect id="{mat}-effect">
      <profile_COMMON>
        <technique sid="common">
          <lambert>
            <diffuse><color>{r:.3f} {g:.3f} {b:.3f} 1</color></diffuse>
          </lambert>
        </technique>
      </profile_COMMON>
    </effect>"""
        )
        materials.append(
            f"""    <material id="{mat}-material" name="{mat}">
      <instance_effect url="#{mat}-effect"/>
    </material>"""
        )
        p_vals = " ".join(f"{a} {b} {c}" for a, b, c in faces)
        triangles.append(
            f"""        <triangles material="{mat}" count="{len(faces)}">
          <input semantic="VERTEX" source="#{lod}-mesh-vertices" offset="0"/>
          <input semantic="TEXCOORD" source="#{lod}-mesh-map-0" offset="0" set="0"/>
          <p>{p_vals}</p>
        </triangles>"""
        )
        binds.append(
            f"""                  <instance_material symbol="{mat}" target="#{mat}-material">
                    <bind_vertex_input semantic="UVSET0" input_semantic="TEXCOORD" input_set="0"/>
                  </instance_material>"""
        )

    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor><authoring_tool>beamng_autoroad build_buildings</authoring_tool></contributor>
    <unit name="meter" meter="1"/>
    <up_axis>Z_UP</up_axis>
  </asset>
  <library_effects>
{chr(10).join(effects)}
  </library_effects>
  <library_materials>
{chr(10).join(materials)}
  </library_materials>
  <library_geometries>
    <geometry id="{lod}-mesh" name="{lod}-mesh">
      <mesh>
        <source id="{lod}-mesh-positions">
          <float_array id="{lod}-mesh-positions-array" count="{n_vert * 3}">{pos_vals}</float_array>
          <technique_common>
            <accessor source="#{lod}-mesh-positions-array" count="{n_vert}" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{lod}-mesh-map-0">
          <float_array id="{lod}-mesh-map-0-array" count="{n_vert * 2}">{uv_vals}</float_array>
          <technique_common>
            <accessor source="#{lod}-mesh-map-0-array" count="{n_vert}" stride="2">
              <param name="S" type="float"/><param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{lod}-mesh-vertices">
          <input semantic="POSITION" source="#{lod}-mesh-positions"/>
        </vertices>
{chr(10).join(triangles)}
      </mesh>
    </geometry>
    <geometry id="Colmesh-1-mesh" name="Colmesh-1-mesh">
      <mesh>
        <source id="Colmesh-1-mesh-positions">
          <float_array id="Colmesh-1-mesh-positions-array" count="{n_vert * 3}">{pos_vals}</float_array>
          <technique_common>
            <accessor source="#Colmesh-1-mesh-positions-array" count="{n_vert}" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="Colmesh-1-mesh-vertices">
          <input semantic="POSITION" source="#Colmesh-1-mesh-positions"/>
        </vertices>
        <triangles count="{len(all_faces)}">
          <input semantic="VERTEX" source="#Colmesh-1-mesh-vertices" offset="0"/>
          <p>{col_p}</p>
        </triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="base00" name="base00" type="NODE">
        <node id="start01" name="start01" type="NODE">
          <node id="{lod}" name="{lod}" type="NODE">
            <instance_geometry url="#{lod}-mesh">
              <bind_material>
                <technique_common>
{chr(10).join(binds)}
                </technique_common>
              </bind_material>
            </instance_geometry>
          </node>
        </node>
        <node id="collision-1" name="collision-1" type="NODE">
          <node id="Colmesh-1" name="Colmesh-1" type="NODE">
            <instance_geometry url="#Colmesh-1-mesh"/>
          </node>
        </node>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene>
    <instance_visual_scene url="#Scene"/>
  </scene>
</COLLADA>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-8", newline="\n")
    return used


def _footprint_z_span(
    ring_beamng: list[tuple[float, float]],
    centroid: tuple[float, float],
    z_at,
) -> tuple[float, float]:
    samples: list[tuple[float, float]] = list(ring_beamng) + [centroid]
    n = len(ring_beamng)
    for i in range(n):
        x0, y0 = ring_beamng[i]
        x1, y1 = ring_beamng[(i + 1) % n]
        samples.append(((x0 + x1) * 0.5, (y0 + y1) * 0.5))
    zs = [float(z_at(x, y)) for x, y in samples]
    return min(zs), max(zs)


def buildings_from_osm(data: dict, site: dict, cfg: dict) -> list[dict]:
    import build_galleries as bg  # noqa: WPS433

    sc = SiteCoords(site)
    to_crs = Transformer.from_crs("EPSG:4326", sc.crs, always_xy=True)
    z_at, _ = bg.load_terrain_z_slope(site)

    out: list[dict] = []
    for el in data.get("elements") or []:
        if el.get("type") != "way":
            continue
        tags = el.get("tags") or {}
        if "building" not in tags:
            continue
        ring = _way_ring_crs(el, to_crs)
        if not ring:
            continue
        poly = _poly_from_ring(ring)
        if poly is None:
            continue
        area = float(poly.area)
        if area < cfg["min_area_m2"] or area > cfg["max_area_m2"]:
            continue
        cx, cy = float(poly.centroid.x), float(poly.centroid.y)
        bx, by = sc.crs_to_beamng(cx, cy)
        if bx < 0 or by < 0 or bx > sc.terrain_extent or by > sc.terrain_extent:
            continue
        height = _parse_height_m(tags, cfg)
        ring_b = [sc.crs_to_beamng(x, y) for x, y in ring[:-1]]
        z_min, z_max = _footprint_z_span(ring_b, (bx, by), z_at)
        out.append({
            "osm_id": el.get("id"),
            "building": str(tags.get("building") or "yes"),
            "name": tags.get("name") or "",
            "area_m2": round(area, 1),
            "height_m": round(height, 2),
            "centroid_crs": [cx, cy],
            "centroid_beamng": [bx, by],
            "ring_beamng": ring_b,
            "z_min": z_min,
            "z_max": z_max,
            "z_span": round(z_max - z_min, 3),
        })
        if len(out) >= cfg["max_items"]:
            break
    return out


def build_and_inject(site: dict) -> None:
    cfg = _cfg(site)
    if not cfg["enabled"]:
        print("buildings.enabled=false — skip")
        return

    proc = processed_dir(site)
    mesh_dir = proc / "building_meshes"
    mesh_dir.mkdir(parents=True, exist_ok=True)

    data = fetch_osm_buildings(site, proc, force=cfg["force_fetch"])
    feats = buildings_from_osm(data, site, cfg)
    print(f"Buildings after filters: {len(feats)}")

    level_name = str((site.get("beamng") or {}).get("level_name") or "autoroad_test")
    user_level = bb.USER_LEVELS / level_name
    shapes_dir = user_level / "art" / "shapes" / "buildings"
    if user_level.exists():
        shapes_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []
    meta_rows: list[dict] = []
    n_gable = 0
    mats_used: dict[str, tuple[float, float, float]] = {}
    for i, feat in enumerate(feats):
        oid = feat["osm_id"]
        slug = _slug(feat["name"] or feat["building"] or str(oid))
        stem = f"bldg_{slug}_{oid}"
        try:
            cx, cy = feat["centroid_beamng"]
            ring_local = [(x - cx, y - cy) for x, y in feat["ring_beamng"]]
            # Probe rect-fit for style decision
            poly = _poly_from_ring(ring_local + ([ring_local[0]] if ring_local[0] != ring_local[-1] else []))
            can_gable = False
            if poly is not None:
                obb = _obb_corners(orient(poly, sign=1.0))
                if obb is not None:
                    _c, _l, width, fill = obb
                    can_gable = fill >= cfg["gable_fill_min"] and width >= 2.5
            style = pick_style(oid, feat["building"], cfg, can_gable=can_gable)
            height = max(2.5, float(feat["height_m"]) * style.height_scale)
            mesh = build_mesh(
                ring_local,
                height,
                style,
                fill_min=cfg["gable_fill_min"],
            )
            if mesh.roof_kind == "gable":
                n_gable += 1
        except Exception as ex:  # noqa: BLE001
            print(f"  skip {oid}: {ex}")
            continue

        dae_proc = mesh_dir / f"{stem}.dae"
        used = write_building_collada(dae_proc, mesh, style, mesh_stem=stem)
        mats_used.update(used)
        shape_vfs = f"/levels/{level_name}/art/shapes/buildings/{stem}.dae"
        if user_level.exists():
            (shapes_dir / f"{stem}.dae").write_bytes(dae_proc.read_bytes())

        z = float(feat["z_min"]) - float(cfg["sink_m"])
        entries.append({
            "name": stem,
            "class": "TSStatic",
            "__parent": "buildings",
            "position": [round(cx, 3), round(cy, 3), round(z, 3)],
            "rotationMatrix": IDENTITY_ROT,
            "scale": [1.0, 1.0, 1.0],
            "shapeName": shape_vfs,
            "collisionType": "Collision Mesh",
            "decalType": "Collision Mesh",
            "useInstanceRenderData": True,
            "isRenderEnabled": True,
        })
        meta_rows.append({
            "osm_id": oid,
            "stem": stem,
            "building": feat["building"],
            "roof": mesh.roof_kind,
            "wall_rgb": [round(c, 3) for c in style.wall_rgb],
            "roof_rgb": [round(c, 3) for c in style.roof_rgb],
            "height_m": round(height, 2),
            "position": entries[-1]["position"],
        })
        if (i + 1) % 200 == 0:
            print(f"  meshed {i + 1}/{len(feats)}")

    items_out = proc / "buildings_items.level.json"
    with items_out.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")
    meta = {
        "count": len(entries),
        "gable_count": n_gable,
        "flat_count": len(entries) - n_gable,
        "sink_m": cfg["sink_m"],
        "items_sample": meta_rows[:50],
        "items_file": str(items_out.relative_to(ROOT)),
    }
    (proc / "buildings_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(
        f"Wrote {items_out} ({len(entries)} buildings, "
        f"gable={n_gable} flat={len(entries) - n_gable})"
    )

    if not user_level.exists():
        print(f"Level folder missing: {user_level} — meshes in processed only")
        return

    ensure_building_materials(user_level, level_name, mats_used)

    group_dir = user_level / "main" / "MissionGroup" / "level_objects" / "buildings"
    group_dir.mkdir(parents=True, exist_ok=True)
    items_path = group_dir / "items.level.json"
    with items_path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")

    lo_items = user_level / "main" / "MissionGroup" / "level_objects" / "items.level.json"
    lines: list[str] = []
    if lo_items.is_file() and lo_items.stat().st_size:
        lines = [ln for ln in lo_items.read_text(encoding="utf-8").splitlines() if ln.strip()]
    names: set[str | None] = set()
    kept: list[str] = []
    for ln in lines:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        n = obj.get("name")
        if n in names:
            continue
        names.add(n)
        kept.append(json.dumps(obj, separators=(",", ":")))
    if "buildings" not in names:
        kept.append(
            json.dumps(
                {"name": "buildings", "class": "SimGroup", "__parent": "level_objects", "enabled": "1"},
                separators=(",", ":"),
            )
        )
        print("Registered SimGroup buildings under level_objects")
    lo_items.write_text("\n".join(kept) + "\n", encoding="utf-8")
    print(f"Injected {len(entries)} TSStatics → {items_path}")


def main() -> None:
    site = load_site()
    print(f"Site: {site_slug(site)}")
    build_and_inject(site)


if __name__ == "__main__":
    main()
