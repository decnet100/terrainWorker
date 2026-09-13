"""Build BeamNG terrain layer masks from OSM landuse + DGM slope + road buffer."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import requests
import tifffile as tiff
from PIL import Image, ImageDraw, ImageFilter
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import load_site, processed_dir, site_slug  # noqa: E402
from fetch_dgm import dgm_cache_path  # noqa: E402

SITE = load_site()
RAW = ROOT / "data" / "raw"
PROC = processed_dir(SITE)
SLUG = site_slug(SITE)

BBOX = SITE["bbox"]
XMIN, YMIN, XMAX, YMAX = map(float, BBOX)
CRS = SITE.get("crs", "EPSG:31254")
OUT_SIZE = int(SITE.get("beamng", {}).get("mask_size", 512))
MPP = float(SITE.get("beamng", {}).get("meters_per_pixel", 1.0))
SLOPE_ROCK_DEG = float(SITE.get("beamng", {}).get("slope_rock_deg", 38.0))
# Morphological opening (px) on slope-rock before paint — kills thin contour zebra.
SLOPE_ROCK_OPEN_PX = int(SITE.get("beamng", {}).get("slope_rock_open_px", 2))
ROAD_WIDTH_SCALE = float(SITE.get("beamng", {}).get("road_width_scale", 1.0))
SHOULDER_M = float(SITE.get("beamng", {}).get("shoulder_m", 1.5))
BW = XMAX - XMIN
BH = YMAX - YMIN
TERRAIN_EXTENT = OUT_SIZE * MPP

# Layer order must match import material list (template-compatible names).
MATERIALS = [
    "Grass",              # 0 meadow / Alm
    "dirt_rocky_large",   # 1 alpine default + road shoulder
    "rock",               # 2 bare rock / scree / steep slopes
    "Asphalt",            # 3 carriageway
    "Grass2",             # 4 forest floor (Hochwald / Strauch)
    "Mud",                # 5 water footprint (under WaterBlock/River)
    "Concrete",           # 6 Siedlung / Anwesen / Gewerbe (Zementflächen)
]

CLASS_GRASS = 0
CLASS_DIRT = 1
CLASS_ROCK = 2
CLASS_ASPHALT = 3
CLASS_FOREST = 4
CLASS_WATER = 5
CLASS_CONCRETE = 6

GROUNDMODELS = {
    "Grass": "GRASS",
    "dirt_rocky_large": "DIRT_ROCKY_LARGE",
    "rock": "ROCK",
    "Asphalt": "ASPHALT",
    "Grass2": "GRASS",
    "Mud": "MUD",
    "Concrete": "CONCRETE",
}


def _local_xy(lon: float, lat: float, to_local: Transformer) -> tuple[float, float]:
    x, y = to_local.transform(lon, lat)
    return x - XMIN, y - YMIN


def _to_px_geo(lx: float, ly: float, size: int) -> tuple[float, float]:
    """BBox-local meters → PNG pixel (row0 = north)."""
    px = lx / BW * (size - 1)
    py = (1.0 - ly / BH) * (size - 1)
    return px, py


def _to_px_beamng(bx: float, by: float, size: int) -> tuple[float, float]:
    """BeamNG terrain meters (square extent) → PNG pixel (row0 = north)."""
    px = bx / TERRAIN_EXTENT * (size - 1)
    py = (1.0 - by / TERRAIN_EXTENT) * (size - 1)
    return px, py


def fetch_osm_landuse() -> dict:
    cache = RAW / f"osm_landuse_{SLUG}.json"
    read_path = cache
    if not read_path.exists():
        legacy = RAW / "osm_landuse.json"
        if legacy.exists() and SLUG.startswith("tirol-m28"):
            read_path = legacy
    if read_path.exists():
        print(f"Using cached {read_path}")
        return json.loads(read_path.read_text(encoding="utf-8"))

    to_wgs = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
    west, south = to_wgs.transform(XMIN, YMIN)
    east, north = to_wgs.transform(XMAX, YMAX)
    query = f"""
    [out:json][timeout:90];
    (
      way["natural"~"wood|scrub|scree|bare_rock|cliff|grassland|heath|fell"]({south},{west},{north},{east});
      way["landuse"~"forest|meadow|grass|farmland|quarry"]({south},{west},{north},{east});
      relation["natural"~"wood|scrub|scree|bare_rock|cliff"]({south},{west},{north},{east});
      relation["landuse"~"forest|meadow|grass"]({south},{west},{north},{east});
    );
    out geom;
    """
    headers = {"User-Agent": "beamng_autoroad/0.1 (local test)", "Accept": "application/json"}
    last_err = None
    for url in (
        "https://overpass.private.coffee/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass-api.de/api/interpreter",
    ):
        try:
            r = requests.post(url, data={"data": query}, headers=headers, timeout=120)
            r.raise_for_status()
            data = r.json()
            cache.write_text(json.dumps(data), encoding="utf-8")
            print(f"Fetched OSM landuse -> {cache} ({len(data.get('elements', []))} elements)")
            return data
        except Exception as ex:  # noqa: BLE001
            last_err = ex
            print(f"Overpass landuse failed at {url}: {ex}")
    raise RuntimeError(f"OSM landuse fetch failed: {last_err}")


def _load_landcover_geojson(rel_or_abs: str | None) -> dict | None:
    if not rel_or_abs:
        return None
    path = Path(rel_or_abs)
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _geojson_rings_to_px(geom: dict, to_local: Transformer, size: int) -> list[list[tuple[float, float]]]:
    """Extract outer rings from GeoJSON polygon/multipolygon → pixel coords."""
    gtype = (geom or {}).get("type")
    coords = (geom or {}).get("coordinates")
    rings_ll: list = []
    if gtype == "Polygon" and coords:
        rings_ll.append(coords[0])
    elif gtype == "MultiPolygon" and coords:
        for poly in coords:
            if poly:
                rings_ll.append(poly[0])
    out: list[list[tuple[float, float]]] = []
    for ring in rings_ll:
        pts = []
        for c in ring:
            if len(c) < 2:
                continue
            lon, lat = float(c[0]), float(c[1])
            lx, ly = _local_xy(lon, lat, to_local)
            pts.append(_to_px_geo(lx, ly, size))
        if len(pts) >= 3:
            out.append(pts)
    return out


def _paint_rings(grid: np.ndarray, rings: list[list[tuple[float, float]]], value: int) -> None:
    size = grid.shape[0]
    for pts in rings:
        img = Image.new("L", (size, size), 0)
        draw = ImageDraw.Draw(img)
        draw.polygon(pts, outline=1, fill=1)
        mask = np.array(img, dtype=np.uint8) > 0
        grid[mask] = value


def _tirol_landnutzung_kind(props: dict) -> str | None:
    """Map Tirol Landnutzung props → semantic kind."""
    klasse = str(props.get("KLASSE") or "").lower()
    objekt = str(props.get("OBJEKT") or "").upper()
    bez = str(props.get("OBJEKTBEZEICHNUNG") or "").lower()

    if objekt.startswith("LN-G") or "gewässer" in bez or "gewasser" in bez:
        if "stehend" in klasse or objekt == "LN-GWS":
            return "water_standing"
        return "water_flowing"
    if "straße" in klasse or "strasse" in klasse or "weg" in klasse or objekt.startswith("LN-V"):
        return "road"
    # LN-S*: Siedlung Wohnen/Misch/Industrie/Anwesen/Sonstige; also keyword fallback
    if (
        objekt.startswith("LN-S")
        or "siedlung" in bez
        or "anwesen" in klasse
        or "industrie" in klasse
        or "gewerbe" in klasse
        or "wohn" in klasse
    ):
        return "settlement"
    if "hochwald" in klasse or objekt == "LN-WHW":
        return "forest_high"
    if "strauch" in klasse or "krumm" in klasse or objekt == "LN-WST":
        return "forest_scrub"
    if "alm" in klasse or "extensiv" in bez or "grünland" in klasse or "gruenland" in klasse:
        return "meadow"
    if "wald" in bez or objekt.startswith("LN-W"):
        return "forest_scrub"
    return None


def _tirol_wald_kind(props: dict) -> str | None:
    objekt = str(props.get("OBJEKT") or "").upper()
    bez = str(props.get("OBJEKTBEZEICHNUNG") or "").lower()
    if objekt == "WHOCH" or bez == "hochwald":
        return "forest_high"
    if objekt == "WSTRAU" or "strauch" in bez or "krumm" in bez:
        return "forest_scrub"
    if "wald" in bez:
        return "forest_high"
    return None


def rasterize_tirol_landcover(size: int) -> tuple[np.ndarray, dict[str, np.ndarray], str]:
    """Rasterize Landnutzung (+ optional Waldfläche).

    Returns (class_grid [-1 unset], extra_masks, source_note).
    Extra masks: forest_high, forest_scrub, water (bool arrays as uint8 0/255 later).
    """
    index_path = PROC / "landcover_index.json"
    if not index_path.is_file():
        return np.full((size, size), -1, dtype=np.int8), {}, "none"

    index = json.loads(index_path.read_text(encoding="utf-8"))
    ln = _load_landcover_geojson(index.get("landnutzung"))
    wf = _load_landcover_geojson(index.get("waldflaeche"))
    if not ln and not wf:
        return np.full((size, size), -1, dtype=np.int8), {}, "none"

    to_local = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)
    landuse = np.full((size, size), -1, dtype=np.int8)
    forest_high = np.zeros((size, size), dtype=bool)
    forest_scrub = np.zeros((size, size), dtype=bool)
    water = np.zeros((size, size), dtype=bool)

    def apply_kind(kind: str | None, rings: list[list[tuple[float, float]]]) -> None:
        nonlocal forest_high, forest_scrub, water
        if not kind or not rings:
            return
        if kind == "meadow":
            _paint_rings(landuse, rings, CLASS_GRASS)
        elif kind == "forest_high":
            _paint_rings(landuse, rings, CLASS_FOREST)
            tmp = np.zeros((size, size), dtype=np.uint8)
            _paint_rings(tmp, rings, 1)
            forest_high = forest_high | (tmp > 0)
        elif kind == "forest_scrub":
            _paint_rings(landuse, rings, CLASS_FOREST)
            tmp = np.zeros((size, size), dtype=np.uint8)
            _paint_rings(tmp, rings, 1)
            forest_scrub = forest_scrub | (tmp > 0)
        elif kind in ("water_flowing", "water_standing"):
            _paint_rings(landuse, rings, CLASS_WATER)
            tmp = np.zeros((size, size), dtype=np.uint8)
            _paint_rings(tmp, rings, 1)
            water = water | (tmp > 0)
        elif kind == "settlement":
            _paint_rings(landuse, rings, CLASS_CONCRETE)
        # road: ignore — asphalt from OSM roads

    # Landnutzung first (broader), Waldfläche refines forest type
    n_ln = n_wf = 0
    if ln:
        for f in ln.get("features") or []:
            kind = _tirol_landnutzung_kind(f.get("properties") or {})
            rings = _geojson_rings_to_px(f.get("geometry") or {}, to_local, size)
            if kind and rings:
                apply_kind(kind, rings)
                n_ln += 1
    if wf:
        for f in wf.get("features") or []:
            kind = _tirol_wald_kind(f.get("properties") or {})
            rings = _geojson_rings_to_px(f.get("geometry") or {}, to_local, size)
            if kind and rings:
                apply_kind(kind, rings)
                n_wf += 1

    extras = {
        "forest_high": forest_high,
        "forest_scrub": forest_scrub,
        "water": water,
    }
    note = f"tirol_landnutzung({n_ln})+waldflaeche({n_wf})"
    print(
        f"Tirol landcover: painted_ln={n_ln} painted_wald={n_wf} "
        f"grass={(landuse == CLASS_GRASS).mean()*100:.2f}% "
        f"forest={(landuse == CLASS_FOREST).mean()*100:.2f}% "
        f"forest_high={forest_high.mean()*100:.2f}% "
        f"forest_scrub={forest_scrub.mean()*100:.2f}% "
        f"water={(landuse == CLASS_WATER).mean()*100:.2f}% "
        f"concrete={(landuse == CLASS_CONCRETE).mean()*100:.2f}%"
    )
    return landuse, extras, note


def _tag_class(tags: dict) -> int | None:
    natural = (tags.get("natural") or "").lower()
    landuse = (tags.get("landuse") or "").lower()
    if natural in {"bare_rock", "scree", "cliff"} or landuse == "quarry":
        return CLASS_ROCK
    if natural in {"wood"} or landuse == "forest":
        return CLASS_GRASS
    if natural in {"scrub", "grassland", "heath", "fell"} or landuse in {
        "meadow", "grass", "farmland",
    }:
        return CLASS_GRASS
    if landuse in {
        "residential", "commercial", "industrial", "retail",
        "construction", "garages", "railway",
    }:
        return CLASS_CONCRETE
    return None


def _ring_pixels(geometry: list[dict], to_local: Transformer, size: int) -> list[tuple[float, float]]:
    pts = []
    for g in geometry:
        lx, ly = _local_xy(g["lon"], g["lat"], to_local)
        pts.append(_to_px_geo(lx, ly, size))
    return pts


def rasterize_landuse(data: dict, size: int) -> np.ndarray:
    """Return int8 class grid; -1 = unset."""
    grid = np.full((size, size), -1, dtype=np.int8)
    to_local = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)

    def paint(pts: list[tuple[float, float]], cls: int) -> None:
        if len(pts) < 3:
            return
        img = Image.new("L", (size, size), 0)
        draw = ImageDraw.Draw(img)
        draw.polygon(pts, outline=1, fill=1)
        mask = np.array(img, dtype=np.uint8) > 0
        # Later polygons overwrite earlier; apply rock last by painting order below.
        grid[mask] = cls

    # Soft cover first, rock last so rock wins overlaps.
    soft: list[tuple[list[tuple[float, float]], int]] = []
    hard: list[tuple[list[tuple[float, float]], int]] = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        cls = _tag_class(tags)
        if cls is None:
            continue
        if el.get("type") == "way" and "geometry" in el:
            pts = _ring_pixels(el["geometry"], to_local, size)
            (hard if cls == CLASS_ROCK else soft).append((pts, cls))
        elif el.get("type") == "relation" and "members" in el:
            for mem in el["members"]:
                if mem.get("type") != "way":
                    continue
                if mem.get("role") not in {"outer", "", None}:
                    continue
                geom = mem.get("geometry")
                if not geom:
                    continue
                pts = _ring_pixels(geom, to_local, size)
                (hard if cls == CLASS_ROCK else soft).append((pts, cls))

    for pts, cls in soft:
        paint(pts, cls)
    for pts, cls in hard:
        paint(pts, cls)
    return grid


def elev_to_grid(elev: np.ndarray, size: int) -> np.ndarray:
    """Resample DGM (row0=north) to square float grid.

    Must stay float32 — an 8-bit detour (~3 m steps over alpine range) turns
    slope into flat/riser zebra, so thresholds like 38° vs 43° do nothing.
    """
    e = np.nan_to_num(elev.astype(np.float32), nan=float(np.nanmean(elev)))
    if e.shape[0] == size and e.shape[1] == size:
        return e
    # PIL mode F = 32-bit float; bilinear keeps continuous gradients.
    return np.array(
        Image.fromarray(e, mode="F").resize((size, size), resample=Image.Resampling.BILINEAR),
        dtype=np.float32,
    )


def slope_degrees(elev_grid: np.ndarray) -> np.ndarray:
    bw = XMAX - XMIN
    bh = YMAX - YMIN
    size = elev_grid.shape[0]
    dx = bw / max(size - 1, 1)
    dy = bh / max(size - 1, 1)
    # rows increase south (north at row0), so dy along axis0 is negative north — magnitude only.
    d_row, d_col = np.gradient(elev_grid, dy, dx)
    return np.degrees(np.arctan(np.hypot(d_col, d_row)))


def road_masks(size: int) -> tuple[np.ndarray, np.ndarray]:
    """Return (asphalt_mask, shoulder_mask) along OSM roads (BeamNG meters).

    Asphalt uses full OSM width * road_width_scale.
    Shoulder is asphalt width + 2 * shoulder_m (dirt bankett).
    """
    path = PROC / "roads_beamng.json"
    asphalt_img = Image.new("L", (size, size), 0)
    shoulder_img = Image.new("L", (size, size), 0)
    if not path.exists():
        z = np.zeros((size, size), dtype=bool)
        return z, z

    m_per_px = TERRAIN_EXTENT / max(size - 1, 1)
    draw_a = ImageDraw.Draw(asphalt_img)
    draw_s = ImageDraw.Draw(shoulder_img)
    roads = json.loads(path.read_text(encoding="utf-8"))
    for road in roads.values():
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        width = float(nodes[0][3]) if len(nodes[0]) > 3 else 6.0
        asphalt_m = max(2.0, width * ROAD_WIDTH_SCALE)
        shoulder_m = asphalt_m + 2.0 * SHOULDER_M
        stroke_a = max(2, int(round(asphalt_m / m_per_px)))
        stroke_s = max(stroke_a + 1, int(round(shoulder_m / m_per_px)))
        pts = [_to_px_beamng(n[0], n[1], size) for n in nodes]
        draw_s.line(pts, fill=255, width=stroke_s, joint="curve")
        draw_a.line(pts, fill=255, width=stroke_a, joint="curve")
    asphalt = np.array(asphalt_img, dtype=np.uint8) > 0
    shoulder = np.array(shoulder_img, dtype=np.uint8) > 0
    return asphalt, shoulder


def bridge_under_mask(size: int, margin_m: float = 0.5) -> np.ndarray:
    """Gap footprint under bridges → rock (extends stay asphalt).

    Prefers under_nodes_xyw from bridges_decks.json (gap − under_inset_m).
    """
    path = PROC / "bridges_decks.json"
    img = Image.new("L", (size, size), 0)
    if not path.exists():
        return np.zeros((size, size), dtype=bool)

    m_per_px = TERRAIN_EXTENT / max(size - 1, 1)
    draw = ImageDraw.Draw(img)
    data = json.loads(path.read_text(encoding="utf-8"))
    n_decks = 0
    for deck in data.get("decks") or []:
        nodes = deck.get("under_nodes_xyw") or deck.get("nodes_xyw") or []
        if len(nodes) < 2:
            continue
        widths = [float(n[2]) for n in nodes if len(n) >= 3]
        width_m = max(widths) if widths else 7.0
        stroke = max(2, int(round((width_m + 2.0 * margin_m) / m_per_px)))
        pts = [_to_px_beamng(float(n[0]), float(n[1]), size) for n in nodes]
        draw.line(pts, fill=255, width=stroke, joint="curve")
        r = max(1, stroke // 2)
        for px, py in (pts[0], pts[-1]):
            draw.ellipse((px - r, py - r, px + r, py + r), fill=255)
        n_decks += 1
    mask = np.array(img, dtype=np.uint8) > 0
    print(
        f"Bridge under-mask: decks={n_decks} "
        f"coverage={(mask.mean() * 100):.3f}% (margin={margin_m}m, gap−inset)"
    )
    return mask


def gallery_roof_mask(size: int, margin_m: float = 1.5) -> np.ndarray:
    """Gallery roof footprint → rock (OSM asphalt must not paint the roof).

    Road centerlines still run through galleries, so without this carve the
    terrain *above* the structure stays Asphalt. Carriageway is MeshRoad.
    Uses portal-span nodes from galleries_centerlines.json when present.
    """
    path = PROC / "galleries_centerlines.json"
    img = Image.new("L", (size, size), 0)
    if not path.exists():
        return np.zeros((size, size), dtype=bool)

    m_per_px = TERRAIN_EXTENT / max(size - 1, 1)
    draw = ImageDraw.Draw(img)
    data = json.loads(path.read_text(encoding="utf-8"))
    n_gals = 0
    for gal in data.get("galleries") or []:
        nodes = gal.get("nodes") or []
        if len(nodes) < 2:
            continue
        portal = gal.get("portal_s")
        if portal is not None and len(portal) >= 2:
            s0, s1 = float(portal[0]), float(portal[1])
            if s1 < s0:
                s0, s1 = s1, s0
            span = [n for n in nodes if s0 - 1e-6 <= float(n.get("s") or 0.0) <= s1 + 1e-6]
            if len(span) >= 2:
                nodes = span
        widths = [float(n.get("width") or 7.0) for n in nodes]
        width_m = max(widths) if widths else 7.0
        # Cover roof + closed-side overhang a bit past the road ribbon.
        stroke = max(2, int(round((width_m + 2.0 * margin_m) / m_per_px)))
        pts = [_to_px_beamng(float(n["x"]), float(n["y"]), size) for n in nodes]
        draw.line(pts, fill=255, width=stroke, joint="curve")
        r = max(1, stroke // 2)
        for px, py in (pts[0], pts[-1]):
            draw.ellipse((px - r, py - r, px + r, py + r), fill=255)
        n_gals += 1
    mask = np.array(img, dtype=np.uint8) > 0
    print(
        f"Gallery roof-mask: galleries={n_gals} "
        f"coverage={(mask.mean() * 100):.3f}% (margin={margin_m}m → rock)"
    )
    return mask


def slope_rock_mask(slope: np.ndarray, threshold_deg: float, open_px: int) -> np.ndarray:
    """Steep cells, then binary opening to drop thin contour stripes.

    Alpine DGMs produce bench/riser patterns near a slope threshold; raw
    ``slope >= T`` carves horizontal zebra into Grass. Opening keeps fat
    cliff faces and removes 1-pixel ribbons.
    """
    raw = slope >= float(threshold_deg)
    if open_px <= 0:
        return raw
    img = Image.fromarray(raw.astype(np.uint8) * 255, mode="L")
    # Min=erode, Max=dilate; 3×3 repeated ≈ open_px metres at 1 m/px
    for _ in range(int(open_px)):
        img = img.filter(ImageFilter.MinFilter(3))
    for _ in range(int(open_px)):
        img = img.filter(ImageFilter.MaxFilter(3))
    return np.array(img, dtype=np.uint8) > 0


def classify(
    landuse: np.ndarray,
    slope: np.ndarray,
    asphalt: np.ndarray,
    shoulder: np.ndarray,
    bridge_under: np.ndarray | None = None,
    gallery_roof: np.ndarray | None = None,
) -> np.ndarray:
    """Exclusive material class per pixel.

    Priority: Asphalt > Concrete(settlement) > Water > Rock > Forest > Grass > Dirt.
    Terrain under bridge decks / on gallery roofs is forced to rock
    (carriageway is MeshRoad).
    """
    out = np.full(landuse.shape, CLASS_DIRT, dtype=np.uint8)
    out[landuse == CLASS_GRASS] = CLASS_GRASS
    out[landuse == CLASS_FOREST] = CLASS_FOREST
    rock = slope_rock_mask(slope, SLOPE_ROCK_DEG, SLOPE_ROCK_OPEN_PX)
    out[rock] = CLASS_ROCK
    out[landuse == CLASS_ROCK] = CLASS_ROCK
    out[landuse == CLASS_WATER] = CLASS_WATER
    out[landuse == CLASS_CONCRETE] = CLASS_CONCRETE
    out[shoulder] = CLASS_DIRT
    out[asphalt] = CLASS_ASPHALT
    if bridge_under is not None and bridge_under.any():
        out[bridge_under] = CLASS_ROCK
    if gallery_roof is not None and gallery_roof.any():
        out[gallery_roof] = CLASS_ROCK
    return out


def write_layer_maps(classes: np.ndarray, out_dir: Path) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for i, name in enumerate(MATERIALS):
        mask = (classes == i).astype(np.uint8) * 255
        fname = f"theTerrain_layerMap_{i}_{name}.png"
        path = out_dir / fname
        Image.fromarray(mask, mode="L").save(path)
        entries.append({
            "index": i,
            "material": name,
            "file": str(path.relative_to(ROOT)),
            "coverage_pct": round(float(mask.mean()) / 255.0 * 100.0, 2),
            "groundmodel_hint": GROUNDMODELS.get(name, "DIRT"),
        })
        print(f"Wrote {path.name} coverage={entries[-1]['coverage_pct']}%")
    return entries


def write_preview(classes: np.ndarray, path: Path) -> None:
    """RGB preview: grass / forest / dirt / rock / asphalt / water / concrete."""
    rgb = np.zeros((*classes.shape, 3), dtype=np.uint8)
    rgb[classes == CLASS_GRASS] = (60, 140, 50)
    rgb[classes == CLASS_DIRT] = (140, 110, 70)
    rgb[classes == CLASS_ROCK] = (150, 150, 155)
    rgb[classes == CLASS_ASPHALT] = (40, 40, 45)
    rgb[classes == CLASS_FOREST] = (30, 90, 40)
    rgb[classes == CLASS_WATER] = (40, 90, 180)
    rgb[classes == CLASS_CONCRETE] = (180, 180, 175)
    Image.fromarray(rgb, mode="RGB").save(path)
    print(f"Wrote {path}")


def main() -> None:
    dgm_path = dgm_cache_path(SITE)
    if not dgm_path.exists():
        legacy = RAW / "dgm_wcs10.tif"
        if legacy.exists() and SLUG.startswith("tirol-m28"):
            dgm_path = legacy
        else:
            raise SystemExit(f"Missing {dgm_path} — run tools/fetch_dgm.py first")

    elev = np.asarray(tiff.imread(dgm_path), dtype=np.float64)
    elev_g = elev_to_grid(elev, OUT_SIZE)
    slope = slope_degrees(elev_g)
    print(f"Slope deg: min={slope.min():.1f} max={slope.max():.1f} "
          f"rock_threshold={SLOPE_ROCK_DEG} open_px={SLOPE_ROCK_OPEN_PX}")

    lu_src = str(((SITE.get("sources") or {}).get("landuse") or {}).get("type") or "osm").lower()
    extras: dict[str, np.ndarray] = {}
    source_note = "osm_landuse"
    if lu_src in ("featureserver", "wfs", "tirol", "landcover"):
        landuse, extras, source_note = rasterize_tirol_landcover(OUT_SIZE)
        if source_note == "none" or int((landuse >= 0).sum()) == 0:
            print("Tirol landcover missing/empty — falling back to OSM")
            try:
                lu_data = fetch_osm_landuse()
                landuse = rasterize_landuse(lu_data, OUT_SIZE)
                source_note = "osm_landuse_fallback"
            except Exception as ex:  # noqa: BLE001
                print(f"OSM fallback failed: {ex}")
                landuse = np.full((OUT_SIZE, OUT_SIZE), -1, dtype=np.int8)
                source_note = "slope_only"
    else:
        try:
            lu_data = fetch_osm_landuse()
            landuse = rasterize_landuse(lu_data, OUT_SIZE)
        except Exception as ex:  # noqa: BLE001
            print(f"OSM landuse failed: {ex} — slope/roads only")
            landuse = np.full((OUT_SIZE, OUT_SIZE), -1, dtype=np.int8)
            source_note = "slope_only"

    asphalt, shoulder = road_masks(OUT_SIZE)
    bridge_under = bridge_under_mask(OUT_SIZE)
    gallery_roof = gallery_roof_mask(OUT_SIZE)
    structure_rock = bridge_under | gallery_roof
    asphalt_vis = asphalt & ~structure_rock
    shoulder_vis = shoulder & ~structure_rock
    print(
        f"Road masks: asphalt={(asphalt_vis.mean()*100):.2f}% "
        f"shoulder={(shoulder_vis.mean()*100):.2f}% "
        f"(width_scale={ROAD_WIDTH_SCALE}, shoulder_m={SHOULDER_M}; "
        f"bridge/gallery roof carved to rock)"
    )
    classes = classify(
        landuse, slope, asphalt_vis, shoulder_vis, bridge_under, gallery_roof
    )

    mask_dir = PROC / "terrain_masks"
    entries = write_layer_maps(classes, mask_dir)
    write_preview(classes, PROC / "preview_terrain_materials.png")

    # Slope helper for ridge impostors later
    slope_u8 = np.clip(slope / 60.0 * 255.0, 0, 255).astype(np.uint8)
    Image.fromarray(slope_u8, mode="L").save(PROC / "mask_slope.png")

    forest_high = extras.get("forest_high")
    forest_scrub = extras.get("forest_scrub")
    water = extras.get("water")
    if forest_high is None:
        forest_high = np.zeros((OUT_SIZE, OUT_SIZE), dtype=bool)
    if forest_scrub is None:
        forest_scrub = np.zeros((OUT_SIZE, OUT_SIZE), dtype=bool)
    if water is None:
        water = np.zeros((OUT_SIZE, OUT_SIZE), dtype=bool)
    # Forest masks = landcover polygons only (never carve by slope — that
    # puts contour noise / "Unruhe" inside mask_forest.png).
    forest = forest_high | forest_scrub
    if not forest.any():
        forest = landuse == CLASS_GRASS
    Image.fromarray(forest.astype(np.uint8) * 255, mode="L").save(PROC / "mask_forest.png")
    Image.fromarray(forest_high.astype(np.uint8) * 255, mode="L").save(PROC / "mask_forest_high.png")
    Image.fromarray(forest_scrub.astype(np.uint8) * 255, mode="L").save(PROC / "mask_forest_scrub.png")
    Image.fromarray(water.astype(np.uint8) * 255, mode="L").save(PROC / "mask_water.png")
    rock = (classes == CLASS_ROCK).astype(np.uint8) * 255
    Image.fromarray(rock, mode="L").save(PROC / "mask_rock.png")
    Image.fromarray(asphalt_vis.astype(np.uint8) * 255, mode="L").save(PROC / "mask_asphalt.png")
    Image.fromarray(shoulder_vis.astype(np.uint8) * 255, mode="L").save(PROC / "mask_shoulder.png")
    Image.fromarray(bridge_under.astype(np.uint8) * 255, mode="L").save(PROC / "mask_bridge_under.png")
    Image.fromarray(gallery_roof.astype(np.uint8) * 255, mode="L").save(PROC / "mask_gallery_roof.png")
    Image.fromarray((classes == CLASS_CONCRETE).astype(np.uint8) * 255, mode="L").save(
        PROC / "mask_settlement.png"
    )

    meta = {
        "size_px": OUT_SIZE,
        "crs": CRS,
        "bbox": [XMIN, YMIN, XMAX, YMAX],
        "slope_rock_deg": SLOPE_ROCK_DEG,
        "road_width_scale": ROAD_WIDTH_SCALE,
        "shoulder_m": SHOULDER_M,
        "materials": entries,
        "landcover_source": source_note,
        "coverage": {
            "grass_pct": round(float((classes == CLASS_GRASS).mean() * 100), 2),
            "dirt_pct": round(float((classes == CLASS_DIRT).mean() * 100), 2),
            "rock_pct": round(float((classes == CLASS_ROCK).mean() * 100), 2),
            "asphalt_pct": round(float((classes == CLASS_ASPHALT).mean() * 100), 2),
            "forest_pct": round(float((classes == CLASS_FOREST).mean() * 100), 2),
            "water_pct": round(float((classes == CLASS_WATER).mean() * 100), 2),
            "concrete_pct": round(float((classes == CLASS_CONCRETE).mean() * 100), 2),
            "forest_high_pct": round(float(forest_high.mean() * 100), 2),
            "forest_scrub_pct": round(float(forest_scrub.mean() * 100), 2),
            "gallery_roof_pct": round(float(gallery_roof.mean() * 100), 2),
        },
        "sources": [
            source_note,
            "dgm_slope",
            "osm_roads_asphalt_shoulder",
            "bridges_decks_under_rock",
            "galleries_roof_rock",
            "tirol_settlement_concrete",
        ],
        "import_notes": [
            "Terrain Tools → Import Terrain → Load terrainPreset.json (recommended)",
            "Heightmap: heightmap_%d.png, Max Height from heightmap_meta.json" % OUT_SIZE,
            "Texture maps in order: Grass, dirt_rocky_large, rock, Asphalt, Grass2, Mud, Concrete",
            "Groundmodels: GRASS / DIRT_ROCKY_LARGE / ROCK / ASPHALT / GRASS / MUD / CONCRETE",
            "Tirol: Almen→Grass; Wald→Grass2+forest scatter; Gewässer→Mud+WaterBlock/River",
            "Siedlung (LN-S*) → Concrete (Zementflächen)",
            "Asphalt = OSM width; dirt = shoulder bankett",
            "Under bridge decks: rock (MeshRoad carries the asphalt)",
            "Gallery roofs: rock (OSM asphalt carved; MeshRoad is the carriageway)",
            "Keep Flip Y Axis consistent with heightmap import",
        ],
    }
    meta_path = PROC / "terrain_materials.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print("Wrote", meta_path)

    # BeamNG-friendly short names + import preset (VFS paths)
    level_name = SITE.get("beamng", {}).get("level_name", "autoroad_test")
    hm_name = f"heightmap_{OUT_SIZE}.png"
    short_maps = []
    for e in entries:
        src = ROOT / e["file"]
        short = f"layerMap_{e['index']}_{e['material']}.png"
        for dest in (PROC / short, mask_dir / short):
            Image.open(src).save(dest)
        short_maps.append({
            "path": f"/levels/{level_name}/import/{short}",
            "material": e["material"],
            "channel": "R",
        })
        Image.open(src).save(PROC / src.name)

    meta_hm = 254.75
    hm_meta = PROC / "heightmap_meta.json"
    if hm_meta.exists():
        meta_hm = float(json.loads(hm_meta.read_text(encoding="utf-8")).get("max_height_m", meta_hm))
    preset = {
        "type": "TerrainData",
        "name": "theTerrain",
        "squareSize": MPP,
        "heightScale": meta_hm,
        "heightMapPath": f"/levels/{level_name}/import/{hm_name}",
        "holeMapPath": "",
        "opacityMaps": short_maps,
        "pos": {"x": 0, "y": 0, "z": 0},
    }
    hole_src = PROC / "theTerrain_holemap.png"
    if hole_src.exists():
        preset["holeMapPath"] = f"/levels/{level_name}/import/theTerrain_holemap.png"
    (PROC / "terrainPreset.json").write_text(json.dumps(preset, indent=2), encoding="utf-8")
    print("Wrote", PROC / "terrainPreset.json")

    # Sync into BeamNG user level import/ (VFS-visible)
    user_import = (
        Path.home()
        / "AppData"
        / "Local"
        / "BeamNG"
        / "BeamNG.drive"
        / "current"
        / "levels"
        / level_name
        / "import"
    )
    if user_import.parent.exists():
        user_import.mkdir(parents=True, exist_ok=True)
        for short in (m["path"].rsplit("/", 1)[-1] for m in short_maps):
            Image.open(PROC / short).save(user_import / short)
        hm = PROC / hm_name
        if hm.exists():
            Image.open(hm).save(user_import / hm_name)
        if hole_src.exists():
            Image.open(hole_src).save(user_import / "theTerrain_holemap.png")
            Image.open(hole_src).save(user_import / "holeMap.png")
        for helper in (
            "mask_forest.png",
            "mask_forest_high.png",
            "mask_forest_scrub.png",
            "mask_water.png",
            "preview_terrain_materials.png",
        ):
            hp = PROC / helper
            if hp.is_file():
                Image.open(hp).save(user_import / helper)
        (user_import / "terrainPreset.json").write_text(
            json.dumps(preset, indent=2), encoding="utf-8"
        )
        print(f"Synced import assets -> {user_import}")
    else:
        print(f"Skip BeamNG sync (no level folder yet): {user_import.parent}")


if __name__ == "__main__":
    main()
