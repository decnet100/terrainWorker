"""Build BeamNG terrain layer masks from OSM landuse + DGM slope + road buffer."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import requests
import tifffile as tiff
from PIL import Image, ImageDraw
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
ROAD_WIDTH_SCALE = float(SITE.get("beamng", {}).get("road_width_scale", 1.0))
SHOULDER_M = float(SITE.get("beamng", {}).get("shoulder_m", 1.5))
BW = XMAX - XMIN
BH = YMAX - YMIN
TERRAIN_EXTENT = OUT_SIZE * MPP

# Layer order must match import material list (template-compatible names).
MATERIALS = [
    "Grass",              # 0 forest / meadow / soft cover
    "dirt_rocky_large",   # 1 alpine default + road shoulder
    "rock",               # 2 bare rock / scree / steep slopes
    "Asphalt",            # 3 carriageway
]

CLASS_GRASS = 0
CLASS_DIRT = 1
CLASS_ROCK = 2
CLASS_ASPHALT = 3

GROUNDMODELS = {
    "Grass": "GRASS",
    "dirt_rocky_large": "DIRT_ROCKY_LARGE",
    "rock": "ROCK",
    "Asphalt": "ASPHALT",
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
    """Resample DGM (row0=north) to square float grid."""
    e = np.nan_to_num(elev.astype(np.float32), nan=float(np.nanmean(elev)))
    # Normalize to 0..255 for PIL resize, then rescale — keeps relative slopes.
    z0, z1 = float(e.min()), float(e.max())
    if z1 <= z0:
        return np.full((size, size), z0, dtype=np.float32)
    u8 = ((e - z0) / (z1 - z0) * 255.0).astype(np.uint8)
    img = Image.fromarray(u8, mode="L").resize((size, size), resample=Image.Resampling.BILINEAR)
    return (np.array(img, dtype=np.float32) / 255.0) * (z1 - z0) + z0


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


def classify(
    landuse: np.ndarray,
    slope: np.ndarray,
    asphalt: np.ndarray,
    shoulder: np.ndarray,
) -> np.ndarray:
    """Exclusive material class per pixel.

    Priority: Asphalt > Rock > Grass (landuse) > Dirt (default / shoulder).
    """
    out = np.full(landuse.shape, CLASS_DIRT, dtype=np.uint8)
    out[landuse == CLASS_GRASS] = CLASS_GRASS
    out[slope >= SLOPE_ROCK_DEG] = CLASS_ROCK
    out[landuse == CLASS_ROCK] = CLASS_ROCK
    # Shoulder bankett (dirt), then asphalt carriageway on top.
    out[shoulder] = CLASS_DIRT
    out[asphalt] = CLASS_ASPHALT
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
    """RGB preview: G=grass, brown=dirt, grey=rock, dark=asphalt."""
    rgb = np.zeros((*classes.shape, 3), dtype=np.uint8)
    rgb[classes == CLASS_GRASS] = (60, 140, 50)
    rgb[classes == CLASS_DIRT] = (140, 110, 70)
    rgb[classes == CLASS_ROCK] = (150, 150, 155)
    rgb[classes == CLASS_ASPHALT] = (40, 40, 45)
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
          f"rock_threshold={SLOPE_ROCK_DEG}")

    lu_data = fetch_osm_landuse()
    landuse = rasterize_landuse(lu_data, OUT_SIZE)
    asphalt, shoulder = road_masks(OUT_SIZE)
    print(
        f"Road masks: asphalt={(asphalt.mean()*100):.2f}% "
        f"shoulder={(shoulder.mean()*100):.2f}% "
        f"(width_scale={ROAD_WIDTH_SCALE}, shoulder_m={SHOULDER_M})"
    )
    classes = classify(landuse, slope, asphalt, shoulder)

    mask_dir = PROC / "terrain_masks"
    entries = write_layer_maps(classes, mask_dir)
    write_preview(classes, PROC / "preview_terrain_materials.png")

    # Slope helper for ridge impostors later
    slope_u8 = np.clip(slope / 60.0 * 255.0, 0, 255).astype(np.uint8)
    Image.fromarray(slope_u8, mode="L").save(PROC / "mask_slope.png")

    forest = ((landuse == CLASS_GRASS) & (slope < SLOPE_ROCK_DEG)).astype(np.uint8) * 255
    Image.fromarray(forest, mode="L").save(PROC / "mask_forest.png")
    rock = (classes == CLASS_ROCK).astype(np.uint8) * 255
    Image.fromarray(rock, mode="L").save(PROC / "mask_rock.png")
    Image.fromarray(asphalt.astype(np.uint8) * 255, mode="L").save(PROC / "mask_asphalt.png")
    Image.fromarray(shoulder.astype(np.uint8) * 255, mode="L").save(PROC / "mask_shoulder.png")

    meta = {
        "size_px": OUT_SIZE,
        "crs": CRS,
        "bbox": [XMIN, YMIN, XMAX, YMAX],
        "slope_rock_deg": SLOPE_ROCK_DEG,
        "road_width_scale": ROAD_WIDTH_SCALE,
        "shoulder_m": SHOULDER_M,
        "materials": entries,
        "sources": ["osm_landuse", "dgm_slope", "osm_roads_asphalt_shoulder"],
        "import_notes": [
            "Terrain Tools → Import Terrain → Load terrainPreset.json (recommended)",
            "Heightmap: heightmap_%d.png, Max Height from heightmap_meta.json" % OUT_SIZE,
            "Texture maps in order: Grass, dirt_rocky_large, rock, Asphalt",
            "Groundmodels: GRASS / DIRT_ROCKY_LARGE / ROCK / ASPHALT",
            "Asphalt = full OSM width; dirt = shoulder bankett around it",
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
        (user_import / "terrainPreset.json").write_text(
            json.dumps(preset, indent=2), encoding="utf-8"
        )
        print(f"Synced import assets -> {user_import}")
    else:
        print(f"Skip BeamNG sync (no level folder yet): {user_import.parent}")


if __name__ == "__main__":
    main()
