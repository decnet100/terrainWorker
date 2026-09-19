"""Compose BEV landcover with nDSM, TWI, snow_proxy, and slope into:

  1. Disjoint biome masks (Forest / Biome tool)
  2. Refined terrain material layerMaps (soft surfaces only)

Hard surfaces (asphalt, concrete, bridge/gallery rock, BEV water) are never
overwritten by nDSM/TWI/snow.

Usage:
  cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = \"config/sites/fernpass_mega.yaml\"; python tools\\compose_biomes.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import tifffile as tiff
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import load_site, processed_dir, site_slug  # noqa: E402

STEAM_LEVELS = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive\content\levels"
)
JV_ZIP = STEAM_LEVELS / "johnson_valley.zip"
USER_LEVELS = (
    Path.home()
    / "AppData"
    / "Local"
    / "BeamNG"
    / "BeamNG.drive"
    / "current"
    / "levels"
)

# BEV LC.LandCoverRaster codes
BEV_HOCH = 0
BEV_MITTEL = 1
BEV_NIEDRIG = 2
BEV_BARE = 3
BEV_BUILDING = 4
BEV_WATER = 5

# Disjoint biome IDs (priority: low number wins when assigned first via mask)
BIOME_NAMES = [
    "kill",
    "water",
    "snow_heavy",
    "snow_light",
    "rock",
    "forest_tall",
    "forest_scrub",
    "forest_gap",
    "meadow_wet",
    "meadow_dry",
    "bare_wet",
    "bare_dry",
]
BIOME_ID = {n: i for i, n in enumerate(BIOME_NAMES)}

BIOME_RGB = {
    "kill": (40, 40, 45),
    "water": (40, 90, 180),
    "snow_heavy": (245, 245, 250),
    "snow_light": (200, 210, 230),
    "rock": (150, 150, 155),
    "forest_tall": (20, 70, 30),
    "forest_scrub": (50, 100, 40),
    "forest_gap": (90, 140, 60),
    "meadow_wet": (50, 120, 100),
    "meadow_dry": (80, 160, 50),
    "bare_wet": (100, 90, 60),
    "bare_dry": (150, 120, 70),
}

# Terrain classes (match MATERIALS order in main())
CLASS_DRY_GRASS = 0  # dry_meadow
CLASS_DIRT = 1       # gravel / bare
CLASS_ROCK = 2
CLASS_ASPHALT = 3
CLASS_FOREST = 4     # ForestFloor
CLASS_WATER = 5      # Mud
CLASS_CONCRETE = 6
CLASS_GRASS = 7      # green Grass
CLASS_SNOW = 8       # snow


def vendor_dirt_grass_material(user_level: Path, level_name: str) -> str:
    """Install Johnson Valley ``dirt_grass`` terrain material into this level."""
    import zipfile

    if not JV_ZIP.is_file():
        raise SystemExit(f"Missing {JV_ZIP} — needed for dirt_grass terrain textures")

    terr = user_level / "art" / "terrains"
    terr.mkdir(parents=True, exist_ok=True)
    prefix = "levels/johnson_valley/art/terrains/"
    files = [
        "t_dirt_dry_grass_ao.png",
        "t_dirt_dry_grass_b.png",
        "t_dirt_dry_grass_h.png",
        "t_dirt_dry_grass_nm.png",
        "t_dirt_dry_grass_r.png",
        "t_macro_clumpy_ao.png",
        "t_macro_clumpy_b.png",
        "t_macro_clumpy_h.png",
        "t_macro_clumpy_nm.png",
        "t_macro_clumpy_r.png",
        "t_terrain_base_ao.png",
        "t_terrain_base_b.png",
        "t_terrain_base_h.png",
        "t_terrain_base_nm.png",
        "t_terrain_base_r.png",
    ]
    with zipfile.ZipFile(JV_ZIP) as z:
        names = set(z.namelist())
        for name in files:
            src = prefix + name
            if src not in names:
                raise SystemExit(f"Missing in johnson_valley.zip: {src}")
            (terr / name).write_bytes(z.read(src))
        mat_src = json.loads(z.read(prefix + "main.materials.json").decode("utf-8"))

    block = None
    for _k, v in mat_src.items():
        if v.get("internalName") == "dirt_grass":
            block = dict(v)
            break
    if block is None:
        raise SystemExit("dirt_grass TerrainMaterial not found in johnson_valley")

    base = f"/levels/{level_name}/art/terrains"
    rewrites = {
        "/levels/johnson_valley/art/terrains/t_dirt_dry_grass_ao.png": f"{base}/t_dirt_dry_grass_ao.png",
        "/levels/johnson_valley/art/terrains/t_dirt_dry_grass_b.png": f"{base}/t_dirt_dry_grass_b.png",
        "/levels/johnson_valley/art/terrains/t_dirt_dry_grass_h.png": f"{base}/t_dirt_dry_grass_h.png",
        "/levels/johnson_valley/art/terrains/t_dirt_dry_grass_nm.png": f"{base}/t_dirt_dry_grass_nm.png",
        "/levels/johnson_valley/art/terrains/t_dirt_dry_grass_r.png": f"{base}/t_dirt_dry_grass_r.png",
        "/levels/johnson_valley/art/terrains/t_macro_clumpy_ao.png": f"{base}/t_macro_clumpy_ao.png",
        "/levels/johnson_valley/art/terrains/t_macro_clumpy_b.png": f"{base}/t_macro_clumpy_b.png",
        "/levels/johnson_valley/art/terrains/t_macro_clumpy_h.png": f"{base}/t_macro_clumpy_h.png",
        "/levels/johnson_valley/art/terrains/t_macro_clumpy_nm.png": f"{base}/t_macro_clumpy_nm.png",
        "/levels/johnson_valley/art/terrains/t_macro_clumpy_r.png": f"{base}/t_macro_clumpy_r.png",
        "/levels/johnson_valley/art/terrains/t_terrain_base_ao.png": f"{base}/t_terrain_base_ao.png",
        "/levels/johnson_valley/art/terrains/t_terrain_base_b.png": f"{base}/t_terrain_base_b.png",
        "/levels/johnson_valley/art/terrains/t_terrain_base_h.png": f"{base}/t_terrain_base_h.png",
        "/levels/johnson_valley/art/terrains/t_terrain_base_nm.png": f"{base}/t_terrain_base_nm.png",
        "/levels/johnson_valley/art/terrains/t_terrain_base_r.png": f"{base}/t_terrain_base_r.png",
    }
    for key, val in list(block.items()):
        if isinstance(val, str) and val in rewrites:
            block[key] = rewrites[val]
    block["name"] = "dirt_grass"
    block["internalName"] = "dirt_grass"

    mats_path = terr / "main.materials.json"
    data: dict[str, Any] = {}
    if mats_path.is_file() and mats_path.stat().st_size:
        try:
            data = json.loads(mats_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    # Drop previous dirt_grass keys, then insert
    data = {
        k: v
        for k, v in data.items()
        if not (
            isinstance(v, dict)
            and str(v.get("internalName") or "").lower() == "dirt_grass"
        )
    }
    data["dirt_grass"] = block
    mats_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Vendored terrain material dirt_grass → {mats_path}")
    return "dirt_grass"


def clear_dry_grass_forest(user_level: Path) -> None:
    """Remove JV dry-grass Forest items from a previous scatter experiment."""
    forest_dir = user_level / "forest"
    n = 0
    if forest_dir.is_dir():
        for old in forest_dir.glob("autoroad_dry_grass_*.forest4.json"):
            old.unlink()
            n += 1
    managed = user_level / "art" / "forest" / "managedItemData.json"
    if managed.is_file():
        try:
            data = json.loads(managed.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        keep = {k: v for k, v in data.items() if not str(k).startswith("autoroad_dry_grass")}
        if len(keep) != len(data):
            managed.write_text(json.dumps(keep, indent=2), encoding="utf-8")
            n += 1
    if n:
        print(f"Cleared dry_grass forest placements/items ({n} updates)")


def _compose_cfg(site: dict) -> dict[str, float]:
    raw = ((site.get("beamng") or {}).get("compose") or {})
    return {
        "ndsm_canopy_m": float(raw.get("ndsm_canopy_m", 5.0)),
        "ndsm_scrub_m": float(raw.get("ndsm_scrub_m", 1.5)),
        "ndsm_clearing_m": float(raw.get("ndsm_clearing_m", 1.0)),
        "twi_wet": float(raw.get("twi_wet", 5.0)),
        "snow_light": float(raw.get("snow_light", 0.25)),
        "snow_heavy": float(raw.get("snow_heavy", 0.55)),
    }


def _load_u8_mask(path: Path, size: int) -> np.ndarray:
    if not path.is_file():
        return np.zeros((size, size), dtype=bool)
    arr = np.asarray(Image.open(path).convert("L"))
    if arr.shape != (size, size):
        arr = np.asarray(
            Image.fromarray(arr, mode="L").resize(
                (size, size), resample=Image.Resampling.NEAREST
            )
        )
    return arr > 127


def _load_raster(path: Path, size: int, *, nearest: bool = False) -> np.ndarray:
    if not path.is_file():
        raise SystemExit(f"Missing {path}")
    arr = np.asarray(tiff.imread(path))
    if arr.ndim == 3:
        arr = arr[..., 0]
    if arr.shape != (size, size):
        img = Image.fromarray(arr)
        resample = Image.Resampling.NEAREST if nearest else Image.Resampling.BILINEAR
        # float arrays: scale via numpy after PIL if needed
        if arr.dtype == np.uint8:
            arr = np.asarray(
                Image.fromarray(arr, mode="L").resize((size, size), resample=resample)
            )
        else:
            # normalize to float32 image via nearest/bilinear on float is awkward;
            # use simple block / zoom via PIL after casting for continuous fields
            f = arr.astype(np.float32)
            mn, mx = float(np.nanmin(f)), float(np.nanmax(f))
            span = max(mx - mn, 1e-6)
            u8 = np.clip((f - mn) / span * 255.0, 0, 255).astype(np.uint8)
            u8r = np.asarray(
                Image.fromarray(u8, mode="L").resize((size, size), resample=resample)
            )
            arr = u8r.astype(np.float32) / 255.0 * span + mn
    return arr


def _assign_soft(
    bev: np.ndarray,
    ndsm: np.ndarray,
    twi: np.ndarray,
    rock: np.ndarray,
    cfg: dict[str, float],
) -> np.ndarray:
    """Biome IDs without snow / kill (water still assigned)."""
    size = bev.shape[0]
    out = np.full((size, size), BIOME_ID["bare_dry"], dtype=np.uint8)

    canopy = ndsm >= cfg["ndsm_canopy_m"]
    clearing = ndsm < cfg["ndsm_clearing_m"]
    wet = twi >= cfg["twi_wet"]

    hoch = bev == BEV_HOCH
    mittel = bev == BEV_MITTEL
    niedrig = bev == BEV_NIEDRIG
    bare = bev == BEV_BARE
    water = bev == BEV_WATER
    forest_poly = hoch | mittel

    out[bare] = BIOME_ID["bare_dry"]
    out[bare & wet] = BIOME_ID["bare_wet"]
    out[niedrig] = BIOME_ID["meadow_dry"]
    out[niedrig & wet] = BIOME_ID["meadow_wet"]
    # Forest: gap → scrub → tall (mittel never becomes tall canopy)
    out[forest_poly & clearing] = BIOME_ID["forest_gap"]
    out[forest_poly & ~clearing & ~canopy] = BIOME_ID["forest_scrub"]
    out[hoch & canopy] = BIOME_ID["forest_tall"]
    out[mittel & canopy] = BIOME_ID["forest_scrub"]
    out[rock] = BIOME_ID["rock"]
    out[water] = BIOME_ID["water"]
    return out


def _apply_snow_kill(
    soft: np.ndarray,
    ndsm: np.ndarray,
    snow: np.ndarray,
    kill: np.ndarray,
    cfg: dict[str, float],
) -> np.ndarray:
    out = soft.copy()
    canopy = ndsm >= cfg["ndsm_canopy_m"]
    water = soft == BIOME_ID["water"]
    # snow only where not kill/water; heavy only off-canopy
    light = (snow >= cfg["snow_light"]) & ~kill & ~water
    heavy = (snow >= cfg["snow_heavy"]) & ~kill & ~water & ~canopy
    out[light] = BIOME_ID["snow_light"]
    out[heavy] = BIOME_ID["snow_heavy"]
    out[kill] = BIOME_ID["kill"]
    out[water] = BIOME_ID["water"]
    return out


def _biome_to_materials(
    biome: np.ndarray,
    soft: np.ndarray,
    kill_asphalt: np.ndarray,
    kill_concrete: np.ndarray,
    kill_struct_rock: np.ndarray,
) -> np.ndarray:
    """Terrain classes from biomes; hard kills win.

    Soft landcover first, then snow biomes overwrite paint (0.39 layer index).
    meadow_dry → dry_meadow; meadow_wet / forest_gap → green Grass;
    forest_* → ForestFloor; bare_dry → gravel; wet water → Mud; snow_* → snow.
    """
    mats = np.full(soft.shape, CLASS_DIRT, dtype=np.uint8)
    mats[soft == BIOME_ID["meadow_dry"]] = CLASS_DRY_GRASS
    mats[soft == BIOME_ID["forest_gap"]] = CLASS_GRASS
    mats[soft == BIOME_ID["meadow_wet"]] = CLASS_GRASS
    mats[soft == BIOME_ID["forest_tall"]] = CLASS_FOREST
    mats[soft == BIOME_ID["forest_scrub"]] = CLASS_FOREST
    mats[soft == BIOME_ID["bare_dry"]] = CLASS_DIRT
    mats[soft == BIOME_ID["bare_wet"]] = CLASS_WATER
    mats[soft == BIOME_ID["rock"]] = CLASS_ROCK
    mats[soft == BIOME_ID["water"]] = CLASS_WATER
    # Snow paint from final (snow-aware) biome — visible November cover
    mats[biome == BIOME_ID["snow_light"]] = CLASS_SNOW
    mats[biome == BIOME_ID["snow_heavy"]] = CLASS_SNOW
    mats[kill_struct_rock] = CLASS_ROCK
    mats[kill_concrete] = CLASS_CONCRETE
    mats[kill_asphalt] = CLASS_ASPHALT
    # Keep water/kill visible over snow
    mats[soft == BIOME_ID["water"]] = CLASS_WATER
    mats[kill_asphalt] = CLASS_ASPHALT
    mats[kill_concrete] = CLASS_CONCRETE
    return mats


def _write_layer_maps(
    classes: np.ndarray,
    out_dir: Path,
    materials: list[str],
    dirt_material: str,
) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ground = {
        "Grass": "GRASS",
        "dirt_grass": "DIRT_GRASS",
        "dry_meadow": "GRASS",
        "ForestFloor": "GRASS",
        "Grass2": "GRASS",
        "dirt_rocky_large": "DIRT_ROCKY_LARGE",
        "Dirt": "DIRT",
        "gravel": "DIRT",
        "rock": "ROCK",
        "Asphalt": "ASPHALT",
        "Grass2": "GRASS",
        "Mud": "MUD",
        "Concrete": "CONCRETE",
        "snow": "SNOW",
        "SnowTirol": "SNOW",
    }
    ground.setdefault(dirt_material, "DIRT")
    entries = []
    for i, name in enumerate(materials):
        mask = (classes == i).astype(np.uint8) * 255
        fname = f"theTerrain_layerMap_{i}_{name}.png"
        path = out_dir / fname
        Image.fromarray(mask, mode="L").save(path)
        short = out_dir.parent / f"layerMap_{i}_{name}.png"
        Image.fromarray(mask, mode="L").save(short)
        Image.fromarray(mask, mode="L").save(out_dir.parent / fname)
        cov = round(float(mask.mean()) / 255.0 * 100.0, 2)
        entries.append(
            {
                "index": i,
                "material": name,
                "file": str(path.relative_to(ROOT)),
                "coverage_pct": cov,
                "groundmodel_hint": ground.get(name, "DIRT"),
            }
        )
        print(f"Wrote {fname} coverage={cov}%")
    return entries


def _preview_biomes(biome: np.ndarray, path: Path) -> None:
    rgb = np.zeros((*biome.shape, 3), dtype=np.uint8)
    for name, bid in BIOME_ID.items():
        rgb[biome == bid] = BIOME_RGB[name]
    Image.fromarray(rgb, mode="RGB").save(path)
    print(f"Wrote {path}")


def _preview_materials(classes: np.ndarray, path: Path) -> None:
    rgb = np.zeros((*classes.shape, 3), dtype=np.uint8)
    rgb[classes == CLASS_DRY_GRASS] = (190, 160, 70)  # dry / yellow
    rgb[classes == CLASS_DIRT] = (140, 110, 70)
    rgb[classes == CLASS_ROCK] = (150, 150, 155)
    rgb[classes == CLASS_ASPHALT] = (40, 40, 45)
    rgb[classes == CLASS_FOREST] = (30, 90, 40)
    rgb[classes == CLASS_WATER] = (40, 90, 180)
    rgb[classes == CLASS_CONCRETE] = (180, 180, 175)
    rgb[classes == CLASS_GRASS] = (60, 150, 55)
    rgb[classes == CLASS_SNOW] = (235, 240, 245)
    Image.fromarray(rgb, mode="RGB").save(path)
    print(f"Wrote {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    site = load_site()
    proc = processed_dir(site)
    slug = site_slug(site)
    bng = site.get("beamng") or {}
    size = int(bng.get("mask_size") or 512)
    mpp = float(bng.get("meters_per_pixel") or 1.0)
    level_name = str(bng.get("level_name") or "autoroad_test")
    dirt_material = str(bng.get("dirt_material") or "gravel")
    dry_grass_material = str(bng.get("dry_grass_material") or "dirt_grass")
    forest_material = str(bng.get("forest_material") or "Grass2")
    grass_material = str(bng.get("grass_material") or "Grass")
    snow_material = str(bng.get("snow_material") or "snow")
    materials = [
        dry_grass_material,
        dirt_material,
        "rock",
        "Asphalt",
        forest_material,
        "Mud",
        "Concrete",
        grass_material,
        snow_material,
    ]
    cfg = _compose_cfg(site)
    print(
        f"Site: {slug}  size={size}  compose={cfg}  "
        f"dry={dry_grass_material} forest={forest_material} "
        f"grass={grass_material} snow={snow_material}"
    )
    user_level = USER_LEVELS / level_name
    if user_level.is_dir():
        from ensure_terrain_materials import ensure_terrain_materials  # noqa: WPS433

        ensure_terrain_materials(user_level, level_name)
        if dry_grass_material == "dirt_grass":
            vendor_dirt_grass_material(user_level, level_name)
        clear_dry_grass_forest(user_level)
        # Ensure configured soft materials exist (dry_meadow, ForestFloor, …).
        mats_path = user_level / "art" / "terrains" / "main.materials.json"
        if mats_path.is_file():
            try:
                mats = json.loads(mats_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                mats = {}
            for need in (dry_grass_material, forest_material, grass_material, snow_material):
                have = any(
                    isinstance(v, dict) and v.get("internalName") == need
                    for v in mats.values()
                )
                if not have:
                    print(
                        f"WARNING: terrain material {need!r} missing in "
                        f"{mats_path} — import may show pink/missing"
                    )
                else:
                    print(f"Using terrain material {need!r}")
    else:
        print(f"Level folder missing, skip material vendor: {user_level}")

    bev = _load_raster(proc / "landcover_bev.tif", size, nearest=True).astype(np.uint8)
    ndsm = _load_raster(proc / "ndsm.tif", size).astype(np.float32)
    twi = _load_raster(proc / "twi.tif", size).astype(np.float32)
    snow = _load_raster(proc / "snow_proxy.tif", size).astype(np.float32)

    asphalt = _load_u8_mask(proc / "mask_asphalt.png", size)
    settlement = _load_u8_mask(proc / "mask_settlement.png", size)
    building_bev = bev == BEV_BUILDING
    rock = _load_u8_mask(proc / "mask_rock.png", size)
    bridge = _load_u8_mask(proc / "mask_bridge_under.png", size)
    gallery = _load_u8_mask(proc / "mask_gallery_roof.png", size)
    struct_rock = bridge | gallery | rock
    kill = asphalt | settlement | building_bev | bridge | gallery

    soft = _assign_soft(bev, ndsm, twi, rock, cfg)
    biome = _apply_snow_kill(soft, ndsm, snow, kill, cfg)
    mats = _biome_to_materials(
        biome, soft, asphalt, settlement | building_bev, struct_rock
    )

    # Biome PNGs (disjoint)
    for name, bid in BIOME_ID.items():
        mask = (biome == bid).astype(np.uint8) * 255
        path = proc / f"mask_biome_{name}.png"
        Image.fromarray(mask, mode="L").save(path)
    # Keep legacy aliases used earlier
    Image.fromarray((biome == BIOME_ID["forest_tall"]).astype(np.uint8) * 255, mode="L").save(
        proc / "mask_biome_veg_hoch.png"
    )
    Image.fromarray(
        ((biome == BIOME_ID["forest_scrub"]) | (biome == BIOME_ID["forest_gap"])).astype(
            np.uint8
        )
        * 255,
        mode="L",
    ).save(proc / "mask_biome_veg_mittel.png")
    Image.fromarray(
        ((biome == BIOME_ID["meadow_dry"]) | (biome == BIOME_ID["meadow_wet"])).astype(
            np.uint8
        )
        * 255,
        mode="L",
    ).save(proc / "mask_biome_veg_niedrig.png")
    Image.fromarray(
        ((biome == BIOME_ID["bare_dry"]) | (biome == BIOME_ID["bare_wet"])).astype(np.uint8)
        * 255,
        mode="L",
    ).save(proc / "mask_biome_bare.png")
    Image.fromarray((building_bev | settlement).astype(np.uint8) * 255, mode="L").save(
        proc / "mask_biome_building.png"
    )
    Image.fromarray((biome == BIOME_ID["water"]).astype(np.uint8) * 255, mode="L").save(
        proc / "mask_biome_water.png"
    )
    Image.fromarray(bev, mode="L").save(proc / "mask_biome_bev_codes.png")
    Image.fromarray(biome.astype(np.uint8), mode="L").save(proc / "mask_biome_ids.png")

    # Forest helpers for scatter
    forest = (biome == BIOME_ID["forest_tall"]) | (biome == BIOME_ID["forest_scrub"])
    Image.fromarray(forest.astype(np.uint8) * 255, mode="L").save(proc / "mask_forest.png")
    Image.fromarray((biome == BIOME_ID["forest_tall"]).astype(np.uint8) * 255, mode="L").save(
        proc / "mask_forest_high.png"
    )
    Image.fromarray(
        (biome == BIOME_ID["forest_scrub"]).astype(np.uint8) * 255, mode="L"
    ).save(proc / "mask_forest_scrub.png")
    Image.fromarray((biome == BIOME_ID["water"]).astype(np.uint8) * 255, mode="L").save(
        proc / "mask_water.png"
    )

    mask_dir = proc / "terrain_masks"
    entries = _write_layer_maps(mats, mask_dir, materials, dirt_material)
    _preview_biomes(biome, proc / "preview_biome_compose.png")
    _preview_materials(mats, proc / "preview_terrain_materials.png")

    coverage = {
        name: round(float((biome == bid).mean() * 100), 2)
        for name, bid in BIOME_ID.items()
    }
    mat_cov = {
        e["material"]: e["coverage_pct"] for e in entries
    }
    meta: dict[str, Any] = {
        "site": slug,
        "size_px": size,
        "thresholds": cfg,
        "biome_names": BIOME_NAMES,
        "biome_coverage_pct": coverage,
        "material_coverage_pct": mat_cov,
        "materials": entries,
        "sources": [
            "landcover_bev.tif",
            "ndsm.tif",
            "twi.tif",
            "snow_proxy.tif",
            "mask_asphalt/settlement/rock/bridge/gallery",
        ],
        "notes": [
            "Biomes are exclusive (one ID per pixel).",
            "meadow_dry → dry_meadow; meadow_wet / forest_gap → Grass.",
            "forest_tall / forest_scrub → ForestFloor.",
            "snow_light / snow_heavy → snow (over soft paint).",
            "bare_dry → gravel; steep → rock; water / bare_wet → Mud.",
            "0.39 terrain import uses layer index from opacityMaps — try >7 materials.",
        ],
    }
    (proc / "biome_compose.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    print(f"Wrote {proc / 'biome_compose.json'}")
    print("Biome coverage %:", json.dumps(coverage, indent=2))
    print("Material coverage %:", json.dumps(mat_cov, indent=2))

    # Refresh terrainPreset opacity paths
    hm_meta = proc / "heightmap_meta.json"
    meta_hm = 254.75
    if hm_meta.exists():
        meta_hm = float(
            json.loads(hm_meta.read_text(encoding="utf-8")).get("max_height_m", meta_hm)
        )
    short_maps = [
        {
            "path": f"/levels/{level_name}/import/layerMap_{e['index']}_{e['material']}.png",
            "material": e["material"],
            "channel": "R",
        }
        for e in entries
    ]
    preset = {
        "type": "TerrainData",
        "name": "theTerrain",
        "squareSize": mpp,
        "heightScale": meta_hm,
        "heightMapPath": f"/levels/{level_name}/import/heightmap_{size}.png",
        "holeMapPath": "",
        "opacityMaps": short_maps,
        "pos": {"x": 0, "y": 0, "z": 0},
    }
    hole_src = proc / "theTerrain_holemap.png"
    if hole_src.exists():
        preset["holeMapPath"] = f"/levels/{level_name}/import/theTerrain_holemap.png"
    (proc / "terrainPreset.json").write_text(json.dumps(preset, indent=2), encoding="utf-8")

    # Sync BeamNG import/
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
        for e in entries:
            short = f"layerMap_{e['index']}_{e['material']}.png"
            Image.open(proc / short).save(user_import / short)
        helpers = [
            "preview_biome_compose.png",
            "preview_terrain_materials.png",
            "mask_biome_ids.png",
            *[f"mask_biome_{n}.png" for n in BIOME_NAMES],
            "mask_biome_veg_hoch.png",
            "mask_biome_veg_mittel.png",
            "mask_biome_veg_niedrig.png",
            "mask_biome_bare.png",
            "mask_biome_building.png",
            "mask_biome_water.png",
            "mask_forest.png",
            "mask_forest_high.png",
            "mask_forest_scrub.png",
            "mask_water.png",
        ]
        for h in helpers:
            hp = proc / h
            if hp.is_file():
                Image.open(hp).save(user_import / h)
        (user_import / "terrainPreset.json").write_text(
            json.dumps(preset, indent=2), encoding="utf-8"
        )
        (user_import / "biome_compose.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )
        print(f"Synced import assets -> {user_import}")
    else:
        print(f"Level folder missing, skip sync: {user_import.parent}")


if __name__ == "__main__":
    main()
