"""Build BeamNG terrain layer masks from OSM landuse + DGM slope + road buffer.

Heavy steps (Tirol landcover raster @ mask_size, DGM slope) are cached under
``processed/<site>/cache/terrain_masks/`` and reused when inputs are unchanged.
Road / bridge / gallery masks always recompute (cheap). Use ``--force`` to
ignore caches.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

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
# asphalt = paint terrain Asphalt under roads (legacy).
# gravel = option 2: terrain bed/bankett only; DecalRoad supplies asphalt look.
ROAD_TERRAIN = str(SITE.get("beamng", {}).get("road_terrain") or "asphalt").lower()
DIRT_MATERIAL = str(SITE.get("beamng", {}).get("dirt_material") or "dirt_rocky_large")
BW = XMAX - XMIN
BH = YMAX - YMIN
TERRAIN_EXTENT = OUT_SIZE * MPP

# Layer order must match import material list (template-compatible names).
MATERIALS = [
    "Grass",              # 0 meadow / Alm
    DIRT_MATERIAL,        # 1 alpine default + road bed/bankett (often gravel)
    "rock",               # 2 bare rock / scree / steep slopes
    "Asphalt",            # 3 carriageway (empty when road_terrain=gravel)
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
    "Dirt": "DIRT",
    "gravel": "DIRT",
    "rock": "ROCK",
    "Asphalt": "ASPHALT",
    "Grass2": "GRASS",
    "Mud": "MUD",
    "Concrete": "CONCRETE",
}
GROUNDMODELS.setdefault(DIRT_MATERIAL, "DIRT")


def _file_sig(path: Path | None) -> dict[str, Any]:
    if path is None or not Path(path).is_file():
        return {"path": str(path) if path else None, "missing": True}
    p = Path(path)
    st = p.stat()
    return {
        "path": str(p.resolve()).replace("\\", "/"),
        "mtime_ns": int(st.st_mtime_ns),
        "size": int(st.st_size),
    }


def _cache_dir() -> Path:
    d = PROC / "cache" / "terrain_masks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_cached_npz(
    name: str, fingerprint: dict, *, force: bool
) -> dict[str, np.ndarray] | None:
    if force:
        return None
    base = _cache_dir() / name
    npz_path = base.with_suffix(".npz")
    meta_path = base.with_suffix(".json")
    if not npz_path.is_file() or not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if meta.get("fingerprint") != fingerprint:
        return None
    data = np.load(npz_path, allow_pickle=False)
    out = {k: data[k] for k in data.files}
    print(f"Cache hit: {npz_path.name}")
    return out


def _save_cached_npz(name: str, fingerprint: dict, arrays: dict[str, np.ndarray]) -> None:
    base = _cache_dir() / name
    npz_path = base.with_suffix(".npz")
    meta_path = base.with_suffix(".json")
    np.savez_compressed(npz_path, **arrays)
    meta_path.write_text(
        json.dumps({"fingerprint": fingerprint, "arrays": sorted(arrays.keys())}, indent=2),
        encoding="utf-8",
    )
    print(f"Cache write: {npz_path.name}")


def _landcover_source_sigs() -> dict[str, Any]:
    index_path = PROC / "landcover_index.json"
    sig: dict[str, Any] = {
        "index": _file_sig(index_path),
        "size": OUT_SIZE,
        "crs": CRS,
        "bbox": [XMIN, YMIN, XMAX, YMAX],
    }
    if index_path.is_file():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            index = {}
        for key in ("landnutzung", "waldflaeche"):
            rel = index.get(key)
            path = None
            if rel:
                path = Path(rel) if Path(rel).is_absolute() else ROOT / rel
            sig[key] = _file_sig(path)
    return sig


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


def rasterize_bev_landcover(
    size: int, *, force: bool = False
) -> tuple[np.ndarray, dict[str, np.ndarray], str]:
    """Map BEV INSPIRE Land Cover (6 classes) → terrain CLASS_* + biome extras.

    BEV codes (LC.LandCoverRaster GeoTIFF from WMS):
      0 vegetation_hoch → FOREST + forest_high
      1 vegetation_mittel → FOREST + forest_scrub
      2 vegetation_niedrig → GRASS
      3 bodenflaechen → DIRTY/bare (CLASS_DIRT); steep still → rock later
      4 gebaeude → CONCRETE
      5 gewaesser → WATER
      6 nodata → unclassified (-1)
    """
    from fetch_bev_landcover import _raw_paths, fetch_bev_landcover  # noqa: WPS433

    fingerprint = {
        "schema": "bev_landcover_v1",
        "size": size,
        "bbox": [XMIN, YMIN, XMAX, YMAX],
        "crs": CRS,
        "bev": _file_sig(_raw_paths(SITE)[0]),
    }
    cached = _load_cached_npz("bev_landcover", fingerprint, force=force)
    if cached is not None:
        landuse = cached["landuse"].astype(np.int8, copy=False)
        extras = {
            "forest_high": cached["forest_high"].astype(bool, copy=False),
            "forest_scrub": cached["forest_scrub"].astype(bool, copy=False),
            "water": cached["water"].astype(bool, copy=False),
            "veg_hoch": cached["veg_hoch"].astype(bool, copy=False),
            "veg_mittel": cached["veg_mittel"].astype(bool, copy=False),
            "veg_niedrig": cached["veg_niedrig"].astype(bool, copy=False),
            "bare": cached["bare"].astype(bool, copy=False),
            "building": cached["building"].astype(bool, copy=False),
            "bev_codes": cached["bev_codes"].astype(np.uint8, copy=False),
        }
        note = "bev_landcover_cached"
        print(
            f"BEV landcover (cached): "
            f"forest={(landuse == CLASS_FOREST).mean()*100:.2f}% "
            f"grass={(landuse == CLASS_GRASS).mean()*100:.2f}% "
            f"dirt={(landuse == CLASS_DIRT).mean()*100:.2f}% "
            f"water={(landuse == CLASS_WATER).mean()*100:.2f}% "
            f"concrete={(landuse == CLASS_CONCRETE).mean()*100:.2f}%"
        )
        return landuse, extras, note

    raw_path = fetch_bev_landcover(SITE, force=force)
    codes = np.asarray(tiff.imread(raw_path))
    if codes.ndim == 3:
        raise SystemExit(f"BEV landcover expected single-band, got {codes.shape}")
    codes = codes.astype(np.uint8, copy=False)
    if codes.shape != (size, size):
        img = Image.fromarray(codes, mode="L")
        img = img.resize((size, size), resample=Image.Resampling.NEAREST)
        codes = np.asarray(img, dtype=np.uint8)

    landuse = np.full((size, size), -1, dtype=np.int8)
    veg_hoch = codes == 0
    veg_mittel = codes == 1
    veg_niedrig = codes == 2
    bare = codes == 3
    building = codes == 4
    water = codes == 5

    landuse[veg_niedrig] = CLASS_GRASS
    landuse[veg_hoch | veg_mittel] = CLASS_FOREST
    landuse[bare] = CLASS_DIRT
    landuse[building] = CLASS_CONCRETE
    landuse[water] = CLASS_WATER
    # code 6 / other → leave -1 (dirt default in classify)

    extras = {
        "forest_high": veg_hoch,
        "forest_scrub": veg_mittel,
        "water": water,
        "veg_hoch": veg_hoch,
        "veg_mittel": veg_mittel,
        "veg_niedrig": veg_niedrig,
        "bare": bare,
        "building": building,
        "bev_codes": codes,
    }
    note = "bev_landcover"
    print(
        f"BEV landcover: "
        f"hoch={veg_hoch.mean()*100:.2f}% mittel={veg_mittel.mean()*100:.2f}% "
        f"niedrig={veg_niedrig.mean()*100:.2f}% bare={bare.mean()*100:.2f}% "
        f"building={building.mean()*100:.2f}% water={water.mean()*100:.2f}%"
    )
    _save_cached_npz(
        "bev_landcover",
        fingerprint,
        {
            "landuse": landuse.astype(np.int8, copy=False),
            "forest_high": veg_hoch.astype(np.uint8),
            "forest_scrub": veg_mittel.astype(np.uint8),
            "water": water.astype(np.uint8),
            "veg_hoch": veg_hoch.astype(np.uint8),
            "veg_mittel": veg_mittel.astype(np.uint8),
            "veg_niedrig": veg_niedrig.astype(np.uint8),
            "bare": bare.astype(np.uint8),
            "building": building.astype(np.uint8),
            "bev_codes": codes,
        },
    )
    return landuse, extras, note


def rasterize_tirol_landcover(
    size: int, *, force: bool = False
) -> tuple[np.ndarray, dict[str, np.ndarray], str]:
    """Rasterize Landnutzung (+ optional Waldfläche).

    Returns (class_grid [-1 unset], extra_masks, source_note).
    Extra masks: forest_high, forest_scrub, water (bool arrays as uint8 0/255 later).
    Cached when landcover GeoJSON / bbox / size are unchanged.
    """
    index_path = PROC / "landcover_index.json"
    if not index_path.is_file():
        return np.full((size, size), -1, dtype=np.int8), {}, "none"

    fingerprint = {
        "schema": "tirol_landcover_v1",
        **_landcover_source_sigs(),
        "size": size,
        "classes": {
            "grass": CLASS_GRASS,
            "dirt": CLASS_DIRT,
            "forest": CLASS_FOREST,
            "water": CLASS_WATER,
            "concrete": CLASS_CONCRETE,
        },
    }
    cached = _load_cached_npz("tirol_landcover", fingerprint, force=force)
    if cached is not None:
        landuse = cached["landuse"].astype(np.int8, copy=False)
        extras = {
            "forest_high": cached["forest_high"].astype(bool, copy=False),
            "forest_scrub": cached["forest_scrub"].astype(bool, copy=False),
            "water": cached["water"].astype(bool, copy=False),
        }
        if "note" in cached:
            raw_note = cached["note"]
            note = str(raw_note.item() if getattr(raw_note, "shape", None) == () else raw_note)
        else:
            note = "tirol_landcover_cached"
        print(
            f"Tirol landcover (cached): "
            f"grass={(landuse == CLASS_GRASS).mean()*100:.2f}% "
            f"forest={(landuse == CLASS_FOREST).mean()*100:.2f}% "
            f"water={(landuse == CLASS_WATER).mean()*100:.2f}% "
            f"concrete={(landuse == CLASS_CONCRETE).mean()*100:.2f}%"
        )
        return landuse, extras, note

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
    _save_cached_npz(
        "tirol_landcover",
        fingerprint,
        {
            "landuse": landuse.astype(np.int8, copy=False),
            "forest_high": forest_high.astype(np.uint8),
            "forest_scrub": forest_scrub.astype(np.uint8),
            "water": water.astype(np.uint8),
            "note": np.array(note),
        },
    )
    return landuse, extras, note


def compute_slope_cached(*, force: bool = False) -> np.ndarray:
    """DGM → elev grid → slope degrees; cached on DGM + grid geometry."""
    dgm_path = dgm_cache_path(SITE)
    if not dgm_path.exists():
        legacy = RAW / "dgm_wcs10.tif"
        if legacy.exists() and SLUG.startswith("tirol-m28"):
            dgm_path = legacy
        else:
            raise SystemExit(f"Missing {dgm_path} — run tools/fetch_dgm.py first")

    fingerprint = {
        "schema": "slope_v1",
        "dgm": _file_sig(dgm_path),
        "size": OUT_SIZE,
        "crs": CRS,
        "bbox": [XMIN, YMIN, XMAX, YMAX],
        "mpp": MPP,
    }
    cached = _load_cached_npz("slope", fingerprint, force=force)
    if cached is not None:
        slope = cached["slope"].astype(np.float32, copy=False)
        print(
            f"Slope deg (cached): min={slope.min():.1f} max={slope.max():.1f} "
            f"rock_threshold={SLOPE_ROCK_DEG} open_px={SLOPE_ROCK_OPEN_PX}"
        )
        return slope

    elev = np.asarray(tiff.imread(dgm_path), dtype=np.float64)
    elev_g = elev_to_grid(elev, OUT_SIZE)
    slope = slope_degrees(elev_g).astype(np.float32)
    print(
        f"Slope deg: min={slope.min():.1f} max={slope.max():.1f} "
        f"rock_threshold={SLOPE_ROCK_DEG} open_px={SLOPE_ROCK_OPEN_PX}"
    )
    _save_cached_npz("slope", fingerprint, {"slope": slope})
    return slope


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


def _stroke_road_nodes(
    roads: dict,
    size: int,
    draw_a,
    draw_s,
    *,
    skip_objekt: frozenset[str] | None = None,
    skip_pred=None,
    only_objekt: frozenset[str] | None = None,
    width_scale: float | None = None,
    paint_shoulder: bool = True,
    min_width_m: float = 2.0,
) -> int:
    """Paint asphalt + shoulder strokes from a roads_beamng-style dict. Returns count."""
    m_per_px = TERRAIN_EXTENT / max(size - 1, 1)
    scale = ROAD_WIDTH_SCALE if width_scale is None else float(width_scale)
    skip = {str(x).upper().strip() for x in (skip_objekt or ())}
    only = {str(x).upper().strip() for x in (only_objekt or ())}
    n = 0
    for road in roads.values():
        objekt = str(road.get("objekt") or road.get("OBJEKT") or "").upper().strip()
        if skip and objekt in skip:
            continue
        if skip_pred is not None and skip_pred(road):
            continue
        if only and objekt not in only:
            continue
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        width = float(nodes[0][3]) if len(nodes[0]) > 3 else 6.0
        asphalt_m = max(min_width_m, width * scale)
        shoulder_m = asphalt_m + 2.0 * SHOULDER_M
        stroke_a = max(2, int(round(asphalt_m / m_per_px)))
        stroke_s = max(stroke_a + 1, int(round(shoulder_m / m_per_px)))
        pts = [_to_px_beamng(n[0], n[1], size) for n in nodes]
        if paint_shoulder:
            draw_s.line(pts, fill=255, width=stroke_s, joint="curve")
        draw_a.line(pts, fill=255, width=stroke_a, joint="curve")
        n += 1
    return n


def road_masks(size: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (asphalt, shoulder, forest_gravel) along roads (BeamNG metres).

    OSM ``roads_beamng.json`` plus, when Decals use GIP, all GIP segments
    (named + minor). ``S-F`` / ``S-GW`` / ``S-FRW`` / ``S-STRAIL`` paint gravel
    at node width, not asphalt.
    """
    from gip_road_segments import _GRAVEL_NO_BED_OBJEKT, gip_skip_surface_paint

    gravel_obj = frozenset(_GRAVEL_NO_BED_OBJEKT)
    asphalt_img = Image.new("L", (size, size), 0)
    shoulder_img = Image.new("L", (size, size), 0)
    gravel_img = Image.new("L", (size, size), 0)
    draw_a = ImageDraw.Draw(asphalt_img)
    draw_s = ImageDraw.Draw(shoulder_img)
    draw_g = ImageDraw.Draw(gravel_img)
    n_osm = n_gip = n_path = 0

    path = PROC / "roads_beamng.json"
    if path.exists():
        n_osm = _stroke_road_nodes(
            json.loads(path.read_text(encoding="utf-8")), size, draw_a, draw_s
        )

    decal_src = str(
        ((SITE.get("beamng") or {}).get("decal_roads") or {}).get("centerline_source")
        or ""
    ).lower()
    gip_path = PROC / "gip_roads_beamng.json"
    if decal_src in ("gip", "verkehrswege", "objectid", "oid") and gip_path.is_file():
        gip_roads = json.loads(gip_path.read_text(encoding="utf-8"))
        n_gip = _stroke_road_nodes(
            gip_roads,
            size,
            draw_a,
            draw_s,
            skip_objekt=gravel_obj,
            skip_pred=gip_skip_surface_paint,
        )
        n_path = _stroke_road_nodes(
            gip_roads,
            size,
            draw_g,
            draw_g,
            only_objekt=gravel_obj,
            width_scale=1.0,
            paint_shoulder=False,
            min_width_m=1.0,
        )
        print(
            f"Road masks: OSM segments={n_osm} GIP asphalt={n_gip} "
            f"GIP gravel paths (S-F/S-GW/S-FRW/S-STRAIL)={n_path}"
        )

    gravel = np.array(gravel_img, dtype=np.uint8) > 0
    asphalt = np.array(asphalt_img, dtype=np.uint8) > 0
    shoulder = np.array(shoulder_img, dtype=np.uint8) > 0
    if gravel.any():
        # Eat leftover OSM asphalt rims around the 4 m forest track.
        m_per_px = TERRAIN_EXTENT / max(size - 1, 1)
        pad_px = max(1, int(round(2.0 / m_per_px)))
        g_img = Image.fromarray(gravel.astype(np.uint8) * 255, mode="L")
        for _ in range(pad_px):
            g_img = g_img.filter(ImageFilter.MaxFilter(3))
        erase = np.array(g_img, dtype=np.uint8) > 0
        asphalt = asphalt & ~erase
        shoulder = (shoulder | (erase & ~gravel)) & ~gravel
    return asphalt, shoulder, gravel


def bridge_under_mask(size: int, margin_m: float = 0.5) -> np.ndarray:
    """Gap footprint under bridges → rock (extends stay asphalt).

    Prefers under_nodes_xyw from bridges_decks.json (gap − under_inset_m).
    Falls back to nodes_xyw only when the inset ate the whole gap — never
    a reason to paint rock; this mask only punches the road stroke.
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
        nodes = deck.get("under_nodes_xyw") or []
        if len(nodes) < 2:
            # Short span: inset left no under-polyline; still omit the gap.
            nodes = deck.get("nodes_xyw") or []
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
        f"coverage={(mask.mean() * 100):.3f}% (margin={margin_m}m, gap-inset)"
    )
    return mask


def gallery_roof_mask(size: int, margin_m: float = 1.5) -> tuple[np.ndarray, np.ndarray]:
    """Gallery roof footprints for terrain paint.

    Returns ``(rock_force, rock_under_asphalt)`` only if YAML set a roof:
    - ``rock``: rock wins over road asphalt.
    - ``keep_asphalt``: rock only where no road asphalt (surface road above).
    Default ``none`` is skipped (span omit only, landcover stays).
    """
    path = PROC / "galleries_centerlines.json"
    empty = np.zeros((size, size), dtype=bool)
    if not path.exists():
        return empty, empty

    m_per_px = TERRAIN_EXTENT / max(size - 1, 1)
    img_force = Image.new("L", (size, size), 0)
    img_keep = Image.new("L", (size, size), 0)
    draw_f = ImageDraw.Draw(img_force)
    draw_k = ImageDraw.Draw(img_keep)
    data = json.loads(path.read_text(encoding="utf-8"))
    n_force = 0
    n_keep = 0
    n_skip = 0
    for gal in data.get("galleries") or []:
        mode = str(gal.get("terrain_roof") or "none").lower().strip()
        if mode in ("none", "off", "false", "0"):
            n_skip += 1
            continue
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
        stroke = max(2, int(round((width_m + 2.0 * margin_m) / m_per_px)))
        pts = [_to_px_beamng(float(n["x"]), float(n["y"]), size) for n in nodes]
        keep = mode in ("keep_asphalt", "under_asphalt", "surface_first", "asphalt")
        draw = draw_k if keep else draw_f
        draw.line(pts, fill=255, width=stroke, joint="curve")
        r = max(1, stroke // 2)
        for px, py in (pts[0], pts[-1]):
            draw.ellipse((px - r, py - r, px + r, py + r), fill=255)
        if keep:
            n_keep += 1
        else:
            n_force += 1
    force = np.array(img_force, dtype=np.uint8) > 0
    keep_m = np.array(img_keep, dtype=np.uint8) > 0
    print(
        f"Gallery roof-mask: force_rock={n_force} keep_asphalt={n_keep} "
        f"skip={n_skip} coverage_force={(force.mean() * 100):.3f}% "
        f"coverage_keep={(keep_m.mean() * 100):.3f}% (margin={margin_m}m)"
    )
    return force, keep_m


def _gallery_bore_nodes(gal: dict, *, inset_m: float = 1.0) -> list[dict] | None:
    """Centerline between portals. Auflage / append pad stays outside.

    Closed tubes inset a little at daylight so the door lip keeps asphalt.
    ``keep_asphalt`` is not a bore omit (surface road above an underpass).
    """
    mode = str(gal.get("terrain_roof") or "none").lower().strip()
    if mode in ("keep_asphalt", "under_asphalt", "surface_first", "asphalt"):
        return None
    nodes = gal.get("nodes") or []
    if len(nodes) < 2:
        return None
    portal = gal.get("portal_s")
    if portal is None or len(portal) < 2:
        return list(nodes)
    s0, s1 = float(portal[0]), float(portal[1])
    if s1 < s0:
        s0, s1 = s1, s0
    kind = str(gal.get("kind") or "").lower()
    open_side = str(gal.get("open_side") or "").lower()
    closed = kind == "tunnel" or open_side in ("none", "off", "false", "0")
    inset = max(0.0, float(inset_m)) if closed else 0.0
    if inset:
        fake = gal.get("fake_end")
        one = bool(gal.get("one_ended"))
        if one and fake == "s0":
            s1 -= inset
        elif one and fake == "s1":
            s0 += inset
        else:
            s0 += inset
            s1 -= inset
    span = [n for n in nodes if s0 - 1e-6 <= float(n.get("s") or 0.0) <= s1 + 1e-6]
    return span if len(span) >= 2 else None


def gallery_span_omit_mask(size: int, margin_m: float = 1.5) -> np.ndarray:
    """No terrain road stroke between gallery/tunnel portals.

    Default for every gallery/tunnel. Skips ``keep_asphalt`` (surface road
    above an underpass). Does not paint rock — landcover stays.
    """
    path = PROC / "galleries_centerlines.json"
    empty = np.zeros((size, size), dtype=bool)
    if not path.exists():
        return empty
    m_per_px = TERRAIN_EXTENT / max(size - 1, 1)
    img = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(img)
    data = json.loads(path.read_text(encoding="utf-8"))
    n_span = 0
    for gal in data.get("galleries") or []:
        nodes = _gallery_bore_nodes(gal)
        if not nodes:
            continue
        widths = [float(n.get("width") or 7.0) for n in nodes]
        width_m = max(widths) if widths else 7.0
        stroke = max(2, int(round((width_m + 2.0 * margin_m) / m_per_px)))
        pts = [_to_px_beamng(float(n["x"]), float(n["y"]), size) for n in nodes]
        draw.line(pts, fill=255, width=stroke, joint="curve")
        r = max(1, stroke // 2)
        for px, py in (pts[0], pts[-1]):
            draw.ellipse((px - r, py - r, px + r, py + r), fill=255)
        n_span += 1
    mask = np.array(img, dtype=np.uint8) > 0
    print(
        f"Gallery/tunnel span-omit: n={n_span} "
        f"coverage={(mask.mean() * 100):.3f}% (margin={margin_m}m)"
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
    gallery_roof_keep_asphalt: np.ndarray | None = None,
    gravel: np.ndarray | None = None,
) -> np.ndarray:
    """Exclusive material class per pixel.

    Priority: road bed > Concrete > Water > Rock > Forest > Grass > Dirt.
    road_terrain=asphalt → carriageway CLASS_ASPHALT; gravel → CLASS_DIRT
    (DecalRoad supplies the asphalt look). GIP ``S-F`` forest tracks stay
    CLASS_DIRT (site ``dirt_material``, usually gravel) even when roads are
    asphalt. Forced gallery roofs stay rock. Bridge spans do not:
    they only omit the road stroke (caller passes no bridge_under here).
    ``gallery_roof_keep_asphalt`` is rock only where no road asphalt
    (surface road above an underpass wins).
    """
    out = np.full(landuse.shape, CLASS_DIRT, dtype=np.uint8)
    out[landuse == CLASS_GRASS] = CLASS_GRASS
    out[landuse == CLASS_FOREST] = CLASS_FOREST
    rock = slope_rock_mask(slope, SLOPE_ROCK_DEG, SLOPE_ROCK_OPEN_PX)
    out[rock] = CLASS_ROCK
    out[landuse == CLASS_ROCK] = CLASS_ROCK
    out[landuse == CLASS_WATER] = CLASS_WATER
    out[landuse == CLASS_CONCRETE] = CLASS_CONCRETE
    # Road corridor overrides slope-rock (Böschung = Kies/Dirt, not Fels).
    out[shoulder] = CLASS_DIRT
    if ROAD_TERRAIN in ("gravel", "dirt", "none", "decal"):
        out[asphalt] = CLASS_DIRT
    else:
        out[asphalt] = CLASS_ASPHALT
    if gravel is not None and gravel.any():
        out[gravel] = CLASS_DIRT
    if gallery_roof is not None and gallery_roof.any():
        out[gallery_roof] = CLASS_ROCK
    # Underpass roof: rock beside/under surface road, but asphalt (Fernpass) stays.
    if gallery_roof_keep_asphalt is not None and gallery_roof_keep_asphalt.any():
        keep_clear = asphalt
        if gravel is not None:
            keep_clear = keep_clear | gravel
        out[gallery_roof_keep_asphalt & ~keep_clear] = CLASS_ROCK
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
    parser = argparse.ArgumentParser(description="Build BeamNG terrain layer masks")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore landcover/slope caches and recompute from source data",
    )
    args = parser.parse_args()
    force = bool(args.force)

    slope = compute_slope_cached(force=force)

    lu_src = str(((SITE.get("sources") or {}).get("landuse") or {}).get("type") or "osm").lower()
    extras: dict[str, np.ndarray] = {}
    source_note = "osm_landuse"
    if lu_src in ("bev", "bev_wms", "bev_landcover"):
        landuse, extras, source_note = rasterize_bev_landcover(OUT_SIZE, force=force)
        if source_note == "none" or int((landuse >= 0).sum()) == 0:
            print("BEV landcover missing/empty — falling back to Tirol/OSM")
            lu_src = "featureserver"
    if lu_src in ("featureserver", "wfs", "tirol", "landcover"):
        landuse, extras, source_note = rasterize_tirol_landcover(OUT_SIZE, force=force)
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
    elif lu_src not in ("bev", "bev_wms", "bev_landcover"):
        try:
            lu_data = fetch_osm_landuse()
            landuse = rasterize_landuse(lu_data, OUT_SIZE)
        except Exception as ex:  # noqa: BLE001
            print(f"OSM landuse failed: {ex} — slope/roads only")
            landuse = np.full((OUT_SIZE, OUT_SIZE), -1, dtype=np.int8)
            source_note = "slope_only"

    asphalt, shoulder, gravel = road_masks(OUT_SIZE)
    bridge_under = bridge_under_mask(OUT_SIZE)
    gallery_roof, gallery_roof_keep = gallery_roof_mask(OUT_SIZE)
    gallery_span = gallery_span_omit_mask(OUT_SIZE)
    # Between abutments the road never stays on the stroke: bridge gap,
    # gallery/tunnel bore (incl. terrain_roof none). keep_asphalt is omitted
    # from gallery_span so a surface road above an underpass stays.
    # Bridge/tunnel spans do not overwrite landcover (no forced rock).
    structure_omit = bridge_under | gallery_span
    asphalt_vis = asphalt & ~structure_omit
    shoulder_vis = shoulder & ~structure_omit
    gravel_vis = gravel & ~structure_omit
    print(
        f"Road masks: asphalt={(asphalt_vis.mean()*100):.2f}% "
        f"shoulder={(shoulder_vis.mean()*100):.2f}% "
        f"forest_gravel={(gravel_vis.mean()*100):.2f}% "
        f"(width_scale={ROAD_WIDTH_SCALE}, shoulder_m={SHOULDER_M}, "
        f"road_terrain={ROAD_TERRAIN}, dirt={DIRT_MATERIAL}; "
        f"span omit; gallery roof rock; no bridge rock)"
    )
    classes = classify(
        landuse,
        slope,
        asphalt_vis,
        shoulder_vis,
        None,
        gallery_roof,
        gallery_roof_keep,
        gravel_vis,
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
    Image.fromarray(gravel_vis.astype(np.uint8) * 255, mode="L").save(
        PROC / "mask_forest_gravel.png"
    )
    Image.fromarray(bridge_under.astype(np.uint8) * 255, mode="L").save(PROC / "mask_bridge_under.png")
    Image.fromarray(gallery_roof.astype(np.uint8) * 255, mode="L").save(PROC / "mask_gallery_roof.png")
    Image.fromarray(gallery_roof_keep.astype(np.uint8) * 255, mode="L").save(
        PROC / "mask_gallery_roof_keep_asphalt.png"
    )
    Image.fromarray(gallery_span.astype(np.uint8) * 255, mode="L").save(
        PROC / "mask_gallery_span_omit.png"
    )
    Image.fromarray((classes == CLASS_CONCRETE).astype(np.uint8) * 255, mode="L").save(
        PROC / "mask_settlement.png"
    )

    # Biome-ready BEV partitions (disjoint landcover; slope rock still separate).
    veg_hoch = np.asarray(extras.get("veg_hoch", forest_high), dtype=bool)
    veg_mittel = np.asarray(extras.get("veg_mittel", forest_scrub), dtype=bool)
    veg_niedrig = np.asarray(
        extras.get("veg_niedrig", landuse == CLASS_GRASS), dtype=bool
    )
    bare = np.asarray(extras.get("bare", landuse == CLASS_DIRT), dtype=bool)
    building = np.asarray(
        extras.get("building", classes == CLASS_CONCRETE), dtype=bool
    )
    Image.fromarray(veg_hoch.astype(np.uint8) * 255, mode="L").save(
        PROC / "mask_biome_veg_hoch.png"
    )
    Image.fromarray(veg_mittel.astype(np.uint8) * 255, mode="L").save(
        PROC / "mask_biome_veg_mittel.png"
    )
    Image.fromarray(veg_niedrig.astype(np.uint8) * 255, mode="L").save(
        PROC / "mask_biome_veg_niedrig.png"
    )
    Image.fromarray(bare.astype(np.uint8) * 255, mode="L").save(PROC / "mask_biome_bare.png")
    Image.fromarray(building.astype(np.uint8) * 255, mode="L").save(
        PROC / "mask_biome_building.png"
    )
    Image.fromarray(water.astype(np.uint8) * 255, mode="L").save(
        PROC / "mask_biome_water.png"
    )
    bev_codes = extras.get("bev_codes")
    if bev_codes is not None:
        Image.fromarray(np.asarray(bev_codes, dtype=np.uint8), mode="L").save(
            PROC / "mask_biome_bev_codes.png"
        )

    meta = {
        "size_px": OUT_SIZE,
        "crs": CRS,
        "bbox": [XMIN, YMIN, XMAX, YMAX],
        "slope_rock_deg": SLOPE_ROCK_DEG,
        "road_width_scale": ROAD_WIDTH_SCALE,
        "shoulder_m": SHOULDER_M,
        "road_terrain": ROAD_TERRAIN,
        "dirt_material": DIRT_MATERIAL,
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
            "biome_veg_hoch_pct": round(float(veg_hoch.mean() * 100), 2),
            "biome_veg_mittel_pct": round(float(veg_mittel.mean() * 100), 2),
            "biome_veg_niedrig_pct": round(float(veg_niedrig.mean() * 100), 2),
            "biome_bare_pct": round(float(bare.mean() * 100), 2),
            "biome_building_pct": round(float(building.mean() * 100), 2),
            "gallery_roof_pct": round(float(gallery_roof.mean() * 100), 2),
            "gallery_roof_keep_asphalt_pct": round(float(gallery_roof_keep.mean() * 100), 2),
            "gallery_span_omit_pct": round(float(gallery_span.mean() * 100), 2),
        },
        "sources": [
            source_note,
            "dgm_slope",
            "osm_roads_asphalt_shoulder",
            "bridges_decks_under_rock",
            "galleries_roof_rock",
            "bev_landcover" if source_note.startswith("bev") else "tirol_settlement_concrete",
        ],
        "import_notes": [
            "Terrain Tools → Import Terrain → Load terrainPreset.json (recommended)",
            "Heightmap: heightmap_%d.png, Max Height from heightmap_meta.json" % OUT_SIZE,
            "Texture maps in order: Grass, %s, rock, Asphalt, Grass2, Mud, Concrete"
            % DIRT_MATERIAL,
            "Groundmodels: GRASS / DIRT / ROCK / ASPHALT / GRASS / MUD / CONCRETE",
            (
                "BEV: veg_hoch/mittel→Grass2+forest; veg_niedrig→Grass; bare→dirt; "
                "building→Concrete; water→Mud; biomes: mask_biome_*.png"
                if source_note.startswith("bev")
                else "Tirol: Almen→Grass; Wald→Grass2+forest scatter; Gewässer→Mud+WaterBlock/River"
            ),
            (
                "Siedlung/Gebäude → Concrete"
                if source_note.startswith("bev")
                else "Siedlung (LN-S*) → Concrete (Zementflächen)"
            ),
            (
                "road_terrain=gravel: OSM corridor → %s (DecalRoad = asphalt); "
                "shoulder overrides slope-rock"
                % DIRT_MATERIAL
                if ROAD_TERRAIN in ("gravel", "dirt", "none", "decal")
                else "Asphalt = OSM width; dirt/gravel = shoulder bankett"
            ),
            "Under bridge decks: no road stroke, then rock (MeshRoad carries the asphalt)",
            "Gallery/tunnel bore (portal to portal): no road stroke; open gallery roof still rock",
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
        # Prefer latest heightmap bake (water/bridge/gallery) over pristine DGM.
        hm = None
        for cand in (
            PROC / f"heightmap_{OUT_SIZE}_composed.png",
            PROC / f"heightmap_{OUT_SIZE}_water.png",
            PROC / f"heightmap_{OUT_SIZE}_gallery_embed.png",
            PROC / f"heightmap_{OUT_SIZE}_gallery_approach.png",
            PROC / f"heightmap_{OUT_SIZE}_gallery.png",
            PROC / f"heightmap_{OUT_SIZE}_bridge_conform.png",
            PROC / hm_name,
        ):
            if cand.is_file():
                hm = cand
                break
        if hm is not None:
            Image.open(hm).save(user_import / hm_name)
        if hole_src.exists():
            Image.open(hole_src).save(user_import / "theTerrain_holemap.png")
            Image.open(hole_src).save(user_import / "holeMap.png")
        for helper in (
            "mask_forest.png",
            "mask_forest_high.png",
            "mask_forest_scrub.png",
            "mask_water.png",
            "mask_biome_veg_hoch.png",
            "mask_biome_veg_mittel.png",
            "mask_biome_veg_niedrig.png",
            "mask_biome_bare.png",
            "mask_biome_building.png",
            "mask_biome_water.png",
            "mask_biome_bev_codes.png",
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
