"""Extrude building roofprints → silhouette Collada + TSStatic.

Default source is tiris Dachflächen (roofprints), walls inset 0.9 m from
the roof so eaves do not sit on the carriageway. OSM ways are opt-in
(``beamng.buildings.source: osm``).

Per-building style (seeded by id):
  - wall / roof colors (tinted facade albedo)
  - window / timber facade tiled in metres per wall
  - slight height jitter
  - gable roof when the roofprint is box-like enough

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
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

import requests
from pyproj import Transformer
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient
from shapely.ops import transform as shp_transform
from shapely.ops import triangulate
from shapely.validation import make_valid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import SiteCoords, load_site, processed_dir, site_slug  # noqa: E402
import build_bridges as bb  # noqa: E402
import building_facades as facades  # noqa: E402

IDENTITY_ROT = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]

# Alpine / village wall tones (RGB 0..1). Material albedo is this * WALL_ALBEDO_SCALE.
WALL_PALETTE: list[tuple[float, float, float]] = [
    (0.94, 0.90, 0.78),  # helles Beige (gewünscht)
    (0.93, 0.91, 0.86),  # cream
    (0.88, 0.84, 0.76),  # beige
    (0.82, 0.78, 0.70),  # sand
    (0.90, 0.88, 0.88),  # light grey
    (0.94, 0.94, 0.92),  # whitewash
    (0.86, 0.80, 0.72),  # plaster
]

# Night: near-white plaster reads as self-lit against dark terrain.
WALL_ALBEDO_SCALE = 0.55
ROOF_ALBEDO_SCALE = 0.55

# Lambert / meta only — in-game color comes from the tile PNG.
ROOF_COLOR_RGB: dict[str, tuple[float, float, float]] = {
    "grey": (0.38, 0.38, 0.40),
    "red": (0.66, 0.28, 0.20),
    "brown": (0.43, 0.27, 0.18),
}
ROOF_MAT = {
    "grey": "BldgRoofGry",
    "red": "BldgRoofRed",
    "brown": "BldgRoofBrn",
}
ROOF_TEX = {
    "grey": "roof_tile_grey.png",
    "red": "roof_tile_red.png",
    "brown": "roof_tile_brown.png",
}
WOOD_MAT = "BldgWood"
WOOD_RGB = (0.48, 0.34, 0.22)
ROOF_THICK_M = 0.10


@dataclass
class BuildingStyle:
    wall_rgb: tuple[float, float, float]
    roof_rgb: tuple[float, float, float]
    height_scale: float
    roof_kind: str  # "flat" | "gable"
    gable_pitch: float  # ridge height / half-width
    facade: str  # "win" | "win2" | "blank"
    roof_color: str = "grey"


@dataclass
class BuildingMesh:
    verts: list[tuple[float, float, float]]
    uvs: list[tuple[float, float]]
    wall_faces: list[tuple[int, int, int]]
    roof_faces: list[tuple[int, int, int]]
    floor_faces: list[tuple[int, int, int]]
    roof_kind: str
    gable_faces: list[tuple[int, int, int]] = field(default_factory=list)
    soffit_faces: list[tuple[int, int, int]] = field(default_factory=list)


WALL_MAT_PREFIX = {
    "win": "BldgWall",
    "win2": "BldgWal2",
    "blank": "BldgBlank",
}
BLANK_TYPES = {
    "industrial",
    "warehouse",
    "garage",
    "garages",
    "shed",
    "hangar",
    "barn",
    "roof",
    "container",
    "farm_auxiliary",
}


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
        "source": str(raw.get("source") or "tiris").lower(),
        "eave_inset_m": float(
            raw["eave_inset_m"] if raw.get("eave_inset_m") is not None else 0.9
        ),
        "facade_tile_w_m": float(raw.get("facade_tile_w_m", facades.TILE_W_M)),
        "facade_tile_h_m": float(raw.get("facade_tile_h_m", raw.get("level_height_m", facades.TILE_H_M))),
        "skip_osm_ids": _int_id_set(raw.get("skip_osm_ids")),
    }


def _osm_id_int(raw) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _int_id_set(raw) -> set[int]:
    out: set[int] = set()
    if raw is None:
        return out
    if isinstance(raw, (str, int)):
        raw = [raw]
    for x in raw:
        s = str(x).strip()
        if s.startswith("bldg_") and "_" in s:
            s = s.rsplit("_", 1)[-1]
        try:
            out.add(int(s))
        except (TypeError, ValueError):
            continue
    return out


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
    rc = rng.random()
    if rc < 0.40:
        roof_color = "grey"
    elif rc < 0.78:
        roof_color = "red"
    else:
        roof_color = "brown"
    roof = ROOF_COLOR_RGB[roof_color]
    jitter = float(cfg["height_jitter"])
    hscale = 1.0 + rng.uniform(-jitter, jitter) if jitter > 0 else 1.0
    b = (building_type or "yes").lower()
    flat_bias = b in {"industrial", "warehouse", "garage", "garages", "shed", "hangar", "retail"}
    chance = float(cfg["gable_chance"]) * (0.25 if flat_bias else 1.0)
    use_gable = bool(can_gable and rng.random() < chance)
    pitch = rng.uniform(0.35, 0.55)
    if b in BLANK_TYPES:
        facade = "blank"
    elif rng.random() < 0.38:
        facade = "win2"
    else:
        facade = "win"
    return BuildingStyle(
        wall_rgb=wall,
        roof_rgb=roof,
        height_scale=hscale,
        roof_kind="gable" if use_gable else "flat",
        gable_pitch=pitch,
        facade=facade,
        roof_color=roof_color,
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


def _rgb_from_mat_name(name: str) -> tuple[float, float, float] | None:
    suffix = name.rsplit("_", 1)[-1]
    if len(suffix) != 6:
        return None
    try:
        return (
            int(suffix[0:2], 16) / 255.0,
            int(suffix[2:4], 16) / 255.0,
            int(suffix[4:6], 16) / 255.0,
        )
    except ValueError:
        return None


def _albedo_rgb(name: str, rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    if name.startswith(("BldgRoofRed", "BldgRoofBrn", "BldgRoofGry")):
        s = ROOF_ALBEDO_SCALE
        return (s, s, s)
    if name.startswith("BldgWood"):
        return (1.0, 1.0, 1.0)
    scale = ROOF_ALBEDO_SCALE if name.startswith("BldgRoof") else WALL_ALBEDO_SCALE
    return tuple(max(0.0, min(1.0, c * scale)) for c in rgb)  # type: ignore[return-value]


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
    shapes = f"/levels/{level_name}/art/shapes/buildings"
    data = {k: v for k, v in data.items() if not str(k).startswith("Bldg")}
    for name, rgb in used.items():
        r, g, b = _albedo_rgb(name, rgb)
        rough = 0.90
        nrm = None
        if name.startswith("BldgRoofRed"):
            base_tex = f"{shapes}/{ROOF_TEX['red']}"
            nrm = f"{shapes}/{facades.ROOF_NORMAL_FILE}"
        elif name.startswith("BldgRoofBrn"):
            base_tex = f"{shapes}/{ROOF_TEX['brown']}"
            nrm = f"{shapes}/{facades.ROOF_NORMAL_FILE}"
        elif name.startswith("BldgRoof"):
            base_tex = f"{shapes}/{ROOF_TEX['grey']}"
            nrm = f"{shapes}/{facades.ROOF_NORMAL_FILE}"
        elif name.startswith("BldgWood"):
            base_tex = f"{shapes}/{facades.BOARD_FILE}"
        elif name.startswith("BldgWal2"):
            base_tex = f"{shapes}/facade_window2.png"
        elif name.startswith("BldgBlank"):
            base_tex = f"{shapes}/facade_blank.png"
        elif name.startswith("BldgFloor"):
            base_tex = f"{terr}/t_terrain_base_dirt_b.png"
        else:
            base_tex = f"{shapes}/facade_window.png"
        stage = {
            "baseColorMap": base_tex,
            "roughnessFactor": rough,
            "metallicFactor": 0.0,
            "emissiveFactor": [0.0, 0.0, 0.0],
            "baseColorFactor": [round(r, 4), round(g, 4), round(b, 4), 1.0],
            "useAnisotropic": True,
        }
        if nrm:
            stage["normalMap"] = nrm
        data[name] = {
            "name": name,
            "mapTo": name,
            "class": "Material",
            "persistentId": f"b1dg{abs(hash(name)) % 10**10:010d}",
            "Stages": [stage, {}, {}, {}],
            "annotation": "BUILDING",
            "materialTag0": "building",
            "materialTag1": "beamng",
            "version": 1.5,
        }
    mats_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {mats_path.name} ({len(used)} building materials)")


def refresh_installed_building_materials(user_level: Path, level_name: str) -> int:
    """Re-tint existing mapTo names (no remesh). RGB comes from the hex suffix."""
    mats_path = user_level / "art" / "shapes" / "buildings" / "main.materials.json"
    if not mats_path.is_file() or mats_path.stat().st_size == 0:
        return 0
    try:
        data = json.loads(mats_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    used: dict[str, tuple[float, float, float]] = {}
    for name in data:
        key = str(name)
        if not key.startswith("Bldg"):
            continue
        rgb = _rgb_from_mat_name(key)
        used[key] = rgb if rgb is not None else (1.0, 1.0, 1.0)
    if not used:
        return 0
    ensure_building_materials(user_level, level_name, used)
    return len(used)


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


def _wall_uv_span(length_m: float, tile_w: float) -> tuple[float, float]:
    """U0, span in tiles. Narrow walls show the beam strip, not a crushed window."""
    if length_m < 1.8:
        return 0.0, max(0.12, length_m / tile_w)
    return 0.0, max(1.0, float(round(length_m / tile_w)))


def _storey_v(z0: float, z1: float, tile_h: float) -> tuple[float, float]:
    """V at bottom/top. Full-height walls snap to whole storeys."""
    if abs(z0) < 1e-6:
        return 0.0, max(1.0, float(round((z1 - z0) / tile_h)))
    return z0 / tile_h, z1 / tile_h


def _append_wall_quad(
    verts: list[tuple[float, float, float]],
    uvs: list[tuple[float, float]],
    faces: list[tuple[int, int, int]],
    p0: tuple[float, float],
    p1: tuple[float, float],
    z0: float,
    z1: float,
    *,
    tile_w: float,
    tile_h: float,
    snap_v: bool = True,
) -> None:
    """One wall rectangle, unique verts (hard corner). Outward = CCW right-hand."""
    dx = p1[0] - p0[0]
    dy = p1[1] - p0[1]
    length = math.hypot(dx, dy)
    if length < 0.05:
        return
    u0, u_span = _wall_uv_span(length, tile_w)
    if snap_v:
        v0, v1 = _storey_v(z0, z1, tile_h)
    else:
        v0, v1 = z0 / tile_h, z1 / tile_h
    i = len(verts)
    verts.extend(
        [
            (p0[0], p0[1], z0),
            (p1[0], p1[1], z0),
            (p1[0], p1[1], z1),
            (p0[0], p0[1], z1),
        ]
    )
    uvs.extend([(u0, v0), (u0 + u_span, v0), (u0 + u_span, v1), (u0, v1)])
    # (p1-p0)×(up) = (dy, -dx, 0) = outward for a CCW footprint
    faces.extend([(i, i + 1, i + 2), (i, i + 2, i + 3)])


def _append_wall_tri(
    verts: list[tuple[float, float, float]],
    uvs: list[tuple[float, float]],
    faces: list[tuple[int, int, int]],
    p0: tuple[float, float],
    p1: tuple[float, float],
    pr: tuple[float, float],
    z0: float,
    z1: float,
    zr: float,
    *,
    tile_w: float,
    tile_h: float,
    v_base: float,
) -> None:
    """Gable triangle on a CCW short edge. Ridge UV sits mid-span."""
    length = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
    if length < 0.05:
        return
    u0, u_span = _wall_uv_span(length, tile_w)
    v_r = v_base + max(0.15, (zr - z0) / tile_h)
    i = len(verts)
    verts.extend(
        [
            (p0[0], p0[1], z0),
            (p1[0], p1[1], z1),
            (pr[0], pr[1], zr),
        ]
    )
    uvs.extend([(u0, v_base), (u0 + u_span, v_base), (u0 + 0.5 * u_span, v_r)])
    a, b, c = verts[i], verts[i + 1], verts[i + 2]
    nx, ny, _nz = _tri_normal(a, b, c)
    ox, oy = (p1[1] - p0[1]), -(p1[0] - p0[0])
    if nx * ox + ny * oy < 0:
        faces.append((i, i + 2, i + 1))
    else:
        faces.append((i, i + 1, i + 2))


def _append_planar_tri(
    verts: list[tuple[float, float, float]],
    uvs: list[tuple[float, float]],
    faces: list[tuple[int, int, int]],
    pts: tuple[tuple[float, float], ...],
    z: float,
    tile_m: float,
) -> None:
    i = len(verts)
    for x, y in pts:
        verts.append((x, y, z))
        uvs.append((x / tile_m, y / tile_m))
    faces.append((i, i + 1, i + 2))


def _append_roof_quad(
    verts: list[tuple[float, float, float]],
    uvs: list[tuple[float, float]],
    faces: list[tuple[int, int, int]],
    p00: tuple[float, float, float],
    p10: tuple[float, float, float],
    p11: tuple[float, float, float],
    p01: tuple[float, float, float],
    tile_m: float,
) -> None:
    u_span = math.dist(p00[:2], p10[:2]) / tile_m
    v_span = math.dist(p00[:2], p01[:2]) / tile_m
    i = len(verts)
    verts.extend([p00, p10, p11, p01])
    uvs.extend([(0.0, 0.0), (u_span, 0.0), (u_span, v_span), (0.0, v_span)])
    faces.extend([(i, i + 1, i + 2), (i, i + 2, i + 3)])


def _hat2(dx: float, dy: float) -> tuple[float, float]:
    length = math.hypot(dx, dy) or 1.0
    return (dx / length, dy / length)


def _expand_obb(
    corners: list[tuple[float, float]],
    over_u: float,
    over_v: float,
) -> list[tuple[float, float]]:
    c0, c1, c2, c3 = corners
    ux, uy = _hat2(c1[0] - c0[0], c1[1] - c0[1])
    vx, vy = _hat2(c2[0] - c1[0], c2[1] - c1[1])
    return [
        (c0[0] - ux * over_u - vx * over_v, c0[1] - uy * over_u - vy * over_v),
        (c1[0] + ux * over_u - vx * over_v, c1[1] + uy * over_u - vy * over_v),
        (c2[0] + ux * over_u + vx * over_v, c2[1] + uy * over_u + vy * over_v),
        (c3[0] - ux * over_u + vx * over_v, c3[1] - uy * over_u + vy * over_v),
    ]


def _edge_parallel(
    p0: tuple[float, float],
    p1: tuple[float, float],
    axis: tuple[float, float],
    *,
    min_dot: float = 0.72,
) -> bool:
    hx, hy = _hat2(p1[0] - p0[0], p1[1] - p0[1])
    return abs(hx * axis[0] + hy * axis[1]) >= min_dot


def _drop_z(
    p: tuple[float, float, float],
    dz: float,
) -> tuple[float, float, float]:
    return (p[0], p[1], p[2] - dz)


def _append_roof_slab(
    verts: list[tuple[float, float, float]],
    uvs: list[tuple[float, float]],
    roof_faces: list[tuple[int, int, int]],
    soffit_faces: list[tuple[int, int, int]],
    fascia_faces: list[tuple[int, int, int]],
    p00: tuple[float, float, float],
    p10: tuple[float, float, float],
    p11: tuple[float, float, float],
    p01: tuple[float, float, float],
    tile_m: float,
    thick: float,
) -> None:
    top = (p00, p10, p11, p01)
    bot = tuple(_drop_z(p, thick) for p in top)
    _append_roof_quad(verts, uvs, roof_faces, top[0], top[1], top[2], top[3], tile_m)
    _append_roof_quad(verts, uvs, soffit_faces, bot[0], bot[3], bot[2], bot[1], tile_m)
    _append_roof_quad(verts, uvs, fascia_faces, top[0], top[1], bot[1], bot[0], tile_m)
    _append_roof_quad(verts, uvs, fascia_faces, top[1], top[2], bot[2], bot[1], tile_m)
    _append_roof_quad(verts, uvs, fascia_faces, top[3], top[0], bot[0], bot[3], tile_m)


def _flat_parts(
    exterior: list[tuple[float, float]],
    height_m: float,
    *,
    tile_w: float,
    tile_h: float,
    roof_exterior: list[tuple[float, float]] | None = None,
    end_axis: tuple[float, float] | None = None,
    thick_m: float = ROOF_THICK_M,
    include_roof: bool = True,
) -> BuildingMesh:
    verts: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    wall: list[tuple[int, int, int]] = []
    wood: list[tuple[int, int, int]] = []
    z1 = float(height_m)
    n = len(exterior)
    for i in range(n):
        j = (i + 1) % n
        dest = wood if (end_axis and _edge_parallel(exterior[i], exterior[j], end_axis)) else wall
        _append_wall_quad(
            verts, uvs, dest, exterior[i], exterior[j], 0.0, z1, tile_w=tile_w, tile_h=tile_h
        )

    roof_ext = roof_exterior or exterior
    roof_poly = Polygon(roof_ext + [roof_ext[0]])
    wall_poly = Polygon(exterior + [exterior[0]])
    roof: list[tuple[int, int, int]] = []
    soffit: list[tuple[int, int, int]] = []
    floor: list[tuple[int, int, int]] = []
    roof_tile = facades.ROOF_TILE_M
    z_top = z1 + float(thick_m)
    if include_roof:
        for tri in _triangulate_xy(roof_poly):
            _append_planar_tri(verts, uvs, roof, tri, z_top, roof_tile)
            _append_planar_tri(verts, uvs, soffit, tri, z1, roof_tile)
        rn = len(roof_ext)
        for i in range(rn):
            j = (i + 1) % rn
            _append_wall_quad(
                verts, uvs, wood, roof_ext[i], roof_ext[j], z1, z_top, tile_w=tile_w, tile_h=tile_h
            )
        roof = _orient_faces(roof, verts, mode="roof")
        soffit = _orient_faces(soffit, verts, mode="floor")
    for tri in _triangulate_xy(wall_poly):
        _append_planar_tri(verts, uvs, floor, tri, 0.0, roof_tile)
    floor = _orient_faces(floor, verts, mode="floor")
    return BuildingMesh(verts, uvs, wall, roof, floor, "flat", wood, soffit)


def _quad_tris(a: int, b: int, c: int, d: int) -> list[tuple[int, int, int]]:
    """Triangulate quad a→b→c→d with diagonal a–c."""
    return [(a, b, c), (a, c, d)]


def _gable_parts(
    corners: list[tuple[float, float]],
    height_m: float,
    pitch: float,
    *,
    tile_w: float,
    tile_h: float,
    include_walls: bool = True,
    include_floor: bool = True,
    overhang_l: float = 0.0,
    overhang_w: float = 0.0,
    thick_m: float = ROOF_THICK_M,
) -> BuildingMesh:
    """Extrude OBB rectangle with gable ridge along the long axis.

    Corners are the house box (CCW). The roof plane goes through the wall
    eaves; outer eaves hang down along the pitch. Ridge joins midpoints of
    the two *short* edges.
    """
    c0, c1, c2, c3 = corners[0], corners[1], corners[2], corners[3]
    e01 = math.hypot(c1[0] - c0[0], c1[1] - c0[1])
    e12 = math.hypot(c2[0] - c1[0], c2[1] - c1[1])
    long_is_01 = e01 >= e12
    half_w = (e12 if long_is_01 else e01) * 0.5
    over_u = overhang_l if long_is_01 else overhang_w
    over_v = overhang_w if long_is_01 else overhang_l
    outer = _expand_obb(corners, over_u, over_v)
    o0, o1, o2, o3 = outer

    if long_is_01:
        ra = ((c1[0] + c2[0]) * 0.5, (c1[1] + c2[1]) * 0.5)
        rb = ((c3[0] + c0[0]) * 0.5, (c3[1] + c0[1]) * 0.5)
        ora = ((o1[0] + o2[0]) * 0.5, (o1[1] + o2[1]) * 0.5)
        orb = ((o3[0] + o0[0]) * 0.5, (o3[1] + o0[1]) * 0.5)
        roof_eaves = ((o0, o1, ora, orb), (o2, o3, orb, ora))
        gable_bases = ((c1, c2, ra), (c3, c0, rb))
        wood_sides = {1, 3}
    else:
        ra = ((c0[0] + c1[0]) * 0.5, (c0[1] + c1[1]) * 0.5)
        rb = ((c2[0] + c3[0]) * 0.5, (c2[1] + c3[1]) * 0.5)
        ora = ((o0[0] + o1[0]) * 0.5, (o0[1] + o1[1]) * 0.5)
        orb = ((o2[0] + o3[0]) * 0.5, (o2[1] + o3[1]) * 0.5)
        roof_eaves = ((o1, o2, orb, ora), (o3, o0, ora, orb))
        gable_bases = ((c0, c1, ra), (c2, c3, rb))
        wood_sides = {0, 2}

    ridge_h = max(0.8, float(pitch) * max(half_w, 0.5))
    z_eave = float(height_m)
    z_outer = z_eave - float(pitch) * max(overhang_w, 0.0)
    z_ridge = z_eave + ridge_h
    _v0, v_eave = _storey_v(0.0, z_eave, tile_h)

    verts: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    wall: list[tuple[int, int, int]] = []
    wood: list[tuple[int, int, int]] = []
    if include_walls:
        for i in range(4):
            j = (i + 1) % 4
            dest = wood if i in wood_sides else wall
            _append_wall_quad(
                verts, uvs, dest, corners[i], corners[j], 0.0, z_eave, tile_w=tile_w, tile_h=tile_h
            )
    for a, b, ridge in gable_bases:
        _append_wall_tri(
            verts,
            uvs,
            wood,
            a,
            b,
            ridge,
            z_eave,
            z_eave,
            z_ridge,
            tile_w=tile_w,
            tile_h=tile_h,
            v_base=v_eave,
        )

    roof: list[tuple[int, int, int]] = []
    soffit: list[tuple[int, int, int]] = []
    fascia: list[tuple[int, int, int]] = []
    roof_tile = facades.ROOF_TILE_M
    for e0, e1, r_near_e1, r_near_e0 in roof_eaves:
        _append_roof_slab(
            verts,
            uvs,
            roof,
            soffit,
            fascia,
            (e0[0], e0[1], z_outer),
            (e1[0], e1[1], z_outer),
            (r_near_e1[0], r_near_e1[1], z_ridge),
            (r_near_e0[0], r_near_e0[1], z_ridge),
            roof_tile,
            thick_m,
        )
    roof = _orient_faces(roof, verts, mode="roof")
    soffit = _orient_faces(soffit, verts, mode="floor")
    fascia = _orient_faces(fascia, verts, mode="wall")
    wood.extend(fascia)

    floor: list[tuple[int, int, int]] = []
    if include_floor:
        _append_planar_tri(verts, uvs, floor, (c0, c1, c2), 0.0, roof_tile)
        _append_planar_tri(verts, uvs, floor, (c0, c2, c3), 0.0, roof_tile)
        floor = _orient_faces(floor, verts, mode="floor")
    return BuildingMesh(verts, uvs, wall, roof, floor, "gable", wood, soffit)


def _merge_mesh(walls: BuildingMesh, roof: BuildingMesh) -> BuildingMesh:
    off = len(walls.verts)
    def _shift(faces: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
        return [(a + off, b + off, c + off) for a, b, c in faces]

    return BuildingMesh(
        walls.verts + roof.verts,
        walls.uvs + roof.uvs,
        walls.wall_faces + _shift(roof.wall_faces),
        walls.roof_faces + _shift(roof.roof_faces),
        walls.floor_faces + _shift(roof.floor_faces),
        roof.roof_kind,
        walls.gable_faces + _shift(roof.gable_faces),
        walls.soffit_faces + _shift(roof.soffit_faces),
    )


def build_mesh(
    ring_centered: list[tuple[float, float]],
    height_m: float,
    style: BuildingStyle,
    *,
    fill_min: float,
    tile_w: float = facades.TILE_W_M,
    tile_h: float = facades.TILE_H_M,
    roof_ring_centered: list[tuple[float, float]] | None = None,
) -> BuildingMesh:
    def _as_poly(ring: list[tuple[float, float]]) -> Polygon:
        if ring[0] != ring[-1]:
            closed = list(ring) + [ring[0]]
        else:
            closed = list(ring)
        poly = _poly_from_ring(closed)
        if poly is None:
            raise ValueError("invalid footprint")
        return orient(poly, sign=1.0)

    wall_poly = _as_poly(ring_centered)
    roof_poly = _as_poly(roof_ring_centered) if roof_ring_centered else wall_poly
    # Caller already subtracted the placement centroid; keep that frame.
    wall_ext = [(float(x), float(y)) for x, y in wall_poly.exterior.coords[:-1]]
    roof_ext = [(float(x), float(y)) for x, y in roof_poly.exterior.coords[:-1]]
    if len(wall_ext) < 3 or len(roof_ext) < 3:
        raise ValueError("too few vertices")

    roof_obb = _obb_corners(roof_poly)
    wall_obb = _obb_corners(wall_poly)
    can_gable = False
    if roof_obb is not None:
        _rc, _rl, r_width, r_fill = roof_obb
        can_gable = r_fill >= fill_min and r_width >= 2.5

    split = roof_ring_centered is not None
    box_obb = wall_obb if (split and wall_obb is not None) else roof_obb
    if style.roof_kind == "gable" and can_gable and box_obb is not None:
        corners, box_len, box_wid, _fill = box_obb
        corners_local = [(float(x), float(y)) for x, y in corners]
        over_l = 0.0
        over_w = 0.0
        if split and roof_obb is not None:
            _c, r_len, r_wid, _f = roof_obb
            over_l = max(0.15, 0.5 * (r_len - box_len))
            over_w = max(0.15, 0.5 * (r_wid - box_wid))
        e0, e1, e2, _e3 = corners_local
        if math.hypot(e1[0] - e0[0], e1[1] - e0[1]) >= math.hypot(e2[0] - e1[0], e2[1] - e1[1]):
            end_axis = _hat2(e2[0] - e1[0], e2[1] - e1[1])
        else:
            end_axis = _hat2(e1[0] - e0[0], e1[1] - e0[1])
        gable = _gable_parts(
            corners_local,
            height_m,
            style.gable_pitch,
            tile_w=tile_w,
            tile_h=tile_h,
            include_walls=not split,
            include_floor=not split,
            overhang_l=over_l,
            overhang_w=over_w,
        )
        if not split:
            return gable
        walls = _flat_parts(
            wall_ext,
            height_m,
            tile_w=tile_w,
            tile_h=tile_h,
            roof_exterior=wall_ext,
            end_axis=end_axis,
            include_roof=False,
        )
        return _merge_mesh(walls, gable)
    return _flat_parts(
        wall_ext, height_m, tile_w=tile_w, tile_h=tile_h, roof_exterior=roof_ext
    )


def write_building_collada(
    path: Path,
    mesh: BuildingMesh,
    style: BuildingStyle,
    *,
    mesh_stem: str,
) -> dict[str, tuple[float, float, float]]:
    """Collada with per-color BeamNG mapTo names. Returns {mat_name: rgb} used."""
    verts = mesh.verts
    uvs = mesh.uvs
    if len(uvs) != len(verts):
        raise ValueError(f"uv/vert mismatch {len(uvs)} != {len(verts)}")
    wall_prefix = WALL_MAT_PREFIX.get(style.facade, "BldgWall")
    wall_mat = _mat_name(wall_prefix, style.wall_rgb)
    roof_mat = ROOF_MAT.get(style.roof_color, "BldgRoofGry")
    wood_faces = list(mesh.gable_faces) + list(mesh.soffit_faces)
    floor_rgb = tuple(max(0.0, c * 0.55) for c in style.wall_rgb)
    floor_mat = _mat_name("BldgFloor", floor_rgb)  # type: ignore[arg-type]
    groups = [
        (wall_mat, style.wall_rgb, mesh.wall_faces),
        (WOOD_MAT, WOOD_RGB, wood_faces),
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
    uv_vals = " ".join(f"{u:.5f} {v:.5f}" for u, v in uvs)
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


def _geojson_to_poly(geom: dict) -> Polygon | None:
    if not geom:
        return None
    kind = geom.get("type")
    coords = geom.get("coordinates") or []
    try:
        if kind == "Polygon":
            poly = Polygon(coords[0], coords[1:] if len(coords) > 1 else None)
        elif kind == "MultiPolygon":
            parts = [Polygon(p[0], p[1:] if len(p) > 1 else None) for p in coords]
            parts = [p for p in parts if not p.is_empty]
            if not parts:
                return None
            poly = max(parts, key=lambda p: p.area)
        else:
            return None
    except Exception:  # noqa: BLE001
        return None
    if not poly.is_valid:
        poly = make_valid(poly)
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        if poly.geom_type != "Polygon":
            return None
    return poly if not poly.is_empty else None


def _inset_roofprint(poly: Polygon, inset_m: float) -> Polygon:
    if inset_m <= 0.05:
        return poly
    inner = poly.buffer(-inset_m, join_style=2)
    if inner.is_empty or inner.area < 4.0:
        inner = poly.buffer(-max(0.25, inset_m * 0.45), join_style=2)
    if inner.is_empty or inner.area < 2.0:
        return poly
    if inner.geom_type == "MultiPolygon":
        inner = max(inner.geoms, key=lambda g: g.area)
    if inner.geom_type != "Polygon":
        return poly
    return inner


def _tiris_height_m(props: dict, cfg: dict) -> float:
    for key in ("GEB_HOEHE_MEDIAN", "GEB_HOEHE_MAX"):
        raw = props.get(key)
        if raw in (None, ""):
            continue
        try:
            h = float(raw)
        except (TypeError, ValueError):
            continue
        if h >= 2.0:
            return max(2.5, h)
    return float(cfg["default_height_m"])


def _ring_beamng(poly: Polygon, sc: SiteCoords) -> list[tuple[float, float]]:
    return [sc.crs_to_beamng(float(x), float(y)) for x, y in poly.exterior.coords[:-1]]


def buildings_from_tiris(data: dict, site: dict, cfg: dict) -> list[dict]:
    import build_galleries as bg  # noqa: WPS433

    sc = SiteCoords(site)
    to_crs = Transformer.from_crs("EPSG:4326", sc.crs, always_xy=True)
    z_at, _ = bg.load_terrain_z_slope(site)
    inset_m = float(cfg.get("eave_inset_m") or 0.0)
    out: list[dict] = []
    for feat in data.get("features") or []:
        if not isinstance(feat, dict):
            continue
        poly_wgs = _geojson_to_poly(feat.get("geometry") or {})
        if poly_wgs is None:
            continue
        poly = shp_transform(to_crs.transform, poly_wgs)
        if poly.is_empty or poly.geom_type != "Polygon":
            if poly.geom_type == "MultiPolygon":
                poly = max(poly.geoms, key=lambda g: g.area)
            else:
                continue
        area = float(poly.area)
        if area < cfg["min_area_m2"] or area > cfg["max_area_m2"]:
            continue
        props = feat.get("properties") or {}
        if props.get("OBJECTID") is None:
            continue
        wall_poly = _inset_roofprint(orient(poly, sign=1.0), inset_m)
        cx, cy = float(poly.centroid.x), float(poly.centroid.y)
        bx, by = sc.crs_to_beamng(cx, cy)
        if bx < 0 or by < 0 or bx > sc.terrain_extent or by > sc.terrain_extent:
            continue
        height = _tiris_height_m(props, cfg)
        roof_ring = _ring_beamng(poly, sc)
        wall_ring = _ring_beamng(wall_poly, sc)
        if len(roof_ring) < 3 or len(wall_ring) < 3:
            continue
        wcx, wcy = sc.crs_to_beamng(float(wall_poly.centroid.x), float(wall_poly.centroid.y))
        z_min, z_max = _footprint_z_span(wall_ring, (wcx, wcy), z_at)
        btype = "shed" if height < 3.8 or area < 40.0 else "yes"
        out.append({
            "osm_id": props.get("OBJECTID"),
            "source": "tiris",
            "building": btype,
            "name": "",
            "area_m2": round(area, 1),
            "height_m": round(height, 2),
            "centroid_crs": [cx, cy],
            "centroid_beamng": [bx, by],
            "ring_beamng": wall_ring,
            "roof_ring_beamng": roof_ring,
            "eave_inset_m": inset_m,
            "z_min": z_min,
            "z_max": z_max,
            "z_span": round(z_max - z_min, 3),
        })
        if len(out) >= cfg["max_items"]:
            break
    return out


def _load_building_features(site: dict, proc: Path, cfg: dict) -> list[dict]:
    if cfg.get("source") == "tiris":
        from fetch_tiris_buildings import load_tiris_geojson  # noqa: WPS433

        data = load_tiris_geojson(site, proc, force=cfg["force_fetch"])
        feats = buildings_from_tiris(data, site, cfg)
        src = "tiris"
    else:
        data = fetch_osm_buildings(site, proc, force=cfg["force_fetch"])
        feats = buildings_from_osm(data, site, cfg)
        src = "osm"
    skip = cfg.get("skip_osm_ids") or set()
    if skip:
        before = len(feats)
        feats = [f for f in feats if _osm_id_int(f.get("osm_id")) not in skip]
        print(f"Buildings skip_osm_ids={sorted(skip)} dropped={before - len(feats)}")
    print(f"Buildings after filters ({src}): {len(feats)}")
    return feats


def build_and_inject(site: dict) -> None:
    cfg = _cfg(site)
    if not cfg["enabled"]:
        print("buildings.enabled=false — skip")
        return

    proc = processed_dir(site)
    mesh_dir = proc / "building_meshes"
    mesh_dir.mkdir(parents=True, exist_ok=True)

    feats = _load_building_features(site, proc, cfg)

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
        slug = _slug(feat["name"] or feat.get("source") or feat["building"] or str(oid))
        stem = f"bldg_{slug}_{oid}"
        try:
            cx, cy = feat["centroid_beamng"]
            ring_local = [(x - cx, y - cy) for x, y in feat["ring_beamng"]]
            roof_abs = feat.get("roof_ring_beamng") or feat["ring_beamng"]
            roof_local = [(x - cx, y - cy) for x, y in roof_abs]
            use_split = roof_abs is not feat["ring_beamng"]
            # Gable from the roofprint, not the inset walls
            poly = _poly_from_ring(
                roof_local + ([roof_local[0]] if roof_local[0] != roof_local[-1] else [])
            )
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
                tile_w=cfg["facade_tile_w_m"],
                tile_h=cfg["facade_tile_h_m"],
                roof_ring_centered=roof_local if use_split else None,
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
            "facade": style.facade,
            "roof_color": style.roof_color,
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
        "source": cfg.get("source") or "osm",
        "eave_inset_m": cfg.get("eave_inset_m") or 0.0,
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

    keep_dae = {f"{e['name']}.dae" for e in entries}
    for folder in (mesh_dir, shapes_dir if user_level.exists() else None):
        if folder is None:
            continue
        removed = 0
        for p in folder.glob("bldg_*.dae"):
            if p.name not in keep_dae:
                p.unlink()
                removed += 1
        if removed:
            print(f"Removed {removed} stale DAEs in {folder}")

    if not user_level.exists():
        print(f"Level folder missing: {user_level} — meshes in processed only")
        return

    facades.write_facade_textures(facades.ASSET_DIR)
    written = facades.write_facade_textures(shapes_dir)
    print(f"Installed {len(written)} facade textures → {shapes_dir}")
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
