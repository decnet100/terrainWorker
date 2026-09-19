"""Ensure Fernpass-style TerrainMaterials exist on a BeamNG user level.

Recreates the editor recipes without World Editor:

  dry_meadow   — clone Grass, baseColor = 35/65 mix of Grass2 + dirt
                 → t_terrain_base_drymeadow_b.png
  ForestFloor  — clone Grass, baseColor = 50/50 mix of grass_b + mud_b
                 → t_terrain_base_mudgrass_b.png
  SnowTirol    — clone snow, same maps, stronger ToD (less roughness, more normals)

Usage:
  cd C:\\temp\\beamng_autoroad
  $env:AUTOROAD_SITE = \"config/sites/fernpass_mega.yaml\"
  python tools\\ensure_terrain_materials.py

Called automatically from compose_biomes / build_level.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import load_site  # noqa: E402

USER_LEVELS = (
    Path.home()
    / "AppData"
    / "Local"
    / "BeamNG"
    / "BeamNG.drive"
    / "current"
    / "levels"
)

# Stable IDs so re-runs do not churn BeamNG material refs
_PID = {
    "dry_meadow": "34b8bfb3-909b-43a1-b3ef-7ebb45c3355e",
    "ForestFloor": "46d57ba4-2a05-43a7-825e-030a18f95409",
    "SnowTirol": "7c1e9a20-4b6d-4f11-9a8e-2d5f0c8b1e44",
}

# Strength tweaks from the hand-tuned Fernpass materials
_DRY_MEADOW_TUNING = {
    "baseColorDetailStrength": [0.35, 0.0],
    "baseColorMacroStrength": [0.1, 0.15],
    "normalDetailStrength": [0.8, 0.15],
    "normalMacroStrength": [0.5, 0.3],
    "roughnessDetailStrength": [1.0, 0.0],
    "roughnessMacroStrength": [0.5, 0.5],
}

# Stock snow is very matte (roughness 0.8) and takes little sun. Meet the backdrop.
_SNOW_TIROL_TUNING = {
    "baseColorDetailStrength": [0.22, 0.0],
    "baseColorMacroStrength": [0.22, 0.35],
    "normalDetailStrength": [0.85, 0.1],
    "normalMacroStrength": [1.0, 0.25],
    "roughnessDetailStrength": [0.35, 0.0],
    "roughnessMacroStrength": [0.30, 0.30],
}

_FOREST_FLOOR_TUNING = {
    "baseColorDetailStrength": [0.35, 0.0],
    "baseColorMacroStrength": [0.1, 0.15],
    "normalDetailStrength": [0.8, 0.15],
    "normalMacroStrength": [0.5, 0.3],
    "roughnessDetailStrength": [1.0, 0.0],
    "roughnessMacroStrength": [0.25, 0.75],
}

MUDGRASS_BLEND_GRASS = 0.5  # ForestFloor: 50% grass + 50% mud base color
DRY_MEADOW_BLEND_GRASS2 = 0.35  # dry_meadow: 35% Grass2 + 65% dirt base color


def _load_mats(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _find_by_internal(data: dict[str, Any], name: str) -> tuple[str | None, dict | None]:
    for k, v in data.items():
        if isinstance(v, dict) and v.get("internalName") == name:
            return k, v
    return None, None


def _clone_named(data: dict[str, Any], internal: str, level_name: str) -> dict[str, Any]:
    _key, block = _find_by_internal(data, internal)
    if block is None:
        raise SystemExit(
            f"TerrainMaterial {internal!r} missing in level {level_name} — "
            "run tools/setup_beamng_level.py first"
        )
    return dict(block)


def _clone_grass(data: dict[str, Any], level_name: str) -> dict[str, Any]:
    return _clone_named(data, "Grass", level_name)


def _rewrite_level_paths(block: dict[str, Any], level_name: str) -> dict[str, Any]:
    """Point any /levels/<other>/… texture refs at this level when local file exists."""
    out = dict(block)
    prefix = f"/levels/{level_name}/"
    for k, v in list(out.items()):
        if not isinstance(v, str) or "/levels/" not in v:
            continue
        # Keep /assets/… global refs
        if v.startswith("/assets/"):
            continue
        # Rewrite foreign level paths to this level
        parts = v.split("/levels/", 1)
        if len(parts) == 2 and "/" in parts[1]:
            _other, rest = parts[1].split("/", 1)
            out[k] = prefix + rest
    return out


def _upsert(data: dict[str, Any], internal: str, block: dict[str, Any]) -> None:
    for k in list(data.keys()):
        v = data[k]
        if isinstance(v, dict) and v.get("internalName") == internal:
            del data[k]
    block = dict(block)
    block["internalName"] = internal
    block["name"] = internal
    block["class"] = "TerrainMaterial"
    block["persistentId"] = _PID.get(internal) or str(uuid.uuid4())
    data[internal] = block


def _blend_rgb(a: Path, b: Path, out: Path, *, w_a: float, label_a: str = "a") -> None:
    """out = w_a * a + (1-w_a) * b (RGB)."""
    ia = np.asarray(Image.open(a).convert("RGB"), dtype=np.float32)
    ib = np.asarray(Image.open(b).convert("RGB"), dtype=np.float32)
    if ia.shape != ib.shape:
        ib_img = Image.fromarray(ib.astype(np.uint8)).resize(
            (ia.shape[1], ia.shape[0]), Image.Resampling.BILINEAR
        )
        ib = np.asarray(ib_img, dtype=np.float32)
    mix = np.clip(w_a * ia + (1.0 - w_a) * ib, 0, 255).astype(np.uint8)
    Image.fromarray(mix, mode="RGB").save(out)
    print(f"Wrote {out.name} (blend {label_a}={w_a:.2f})")


def _base_color_png(
    data: dict[str, Any], terr: Path, internal: str, *, fallback: Path
) -> Path:
    """Local PNG for a TerrainMaterial's baseColorBaseTex."""
    _key, block = _find_by_internal(data, internal)
    if block:
        vfs = str(block.get("baseColorBaseTex") or "")
        name = Path(vfs.replace("\\", "/")).name
        if name:
            cand = terr / name
            if cand.is_file():
                return cand
    if fallback.is_file():
        return fallback
    raise SystemExit(f"Missing baseColor PNG for {internal}: {fallback}")


def ensure_mudgrass_texture(terr: Path) -> Path:
    grass_b = terr / "t_terrain_base_grass_b.png"
    mud_b = terr / "t_terrain_base_mud_b.png"
    out = terr / "t_terrain_base_mudgrass_b.png"
    if not grass_b.is_file() or not mud_b.is_file():
        raise SystemExit(
            f"Need {grass_b.name} and {mud_b.name} under {terr} "
            "(template level terrains)"
        )
    _blend_rgb(grass_b, mud_b, out, w_a=MUDGRASS_BLEND_GRASS, label_a="grass")
    return out


def ensure_drymeadow_texture(data: dict[str, Any], terr: Path) -> Path:
    grass2_b = _base_color_png(
        data, terr, "Grass2", fallback=terr / "t_terrain_base_grass_b.png"
    )
    dirt_b = terr / "t_terrain_base_dirt_b.png"
    if not dirt_b.is_file():
        raise SystemExit(f"Missing {dirt_b} — template should ship dirt base textures")
    out = terr / "t_terrain_base_drymeadow_b.png"
    _blend_rgb(
        grass2_b,
        dirt_b,
        out,
        w_a=DRY_MEADOW_BLEND_GRASS2,
        label_a="Grass2",
    )
    return out


def ensure_dry_meadow(data: dict[str, Any], terr: Path, level_name: str) -> None:
    tex = ensure_drymeadow_texture(data, terr)
    block = _rewrite_level_paths(_clone_grass(data, level_name), level_name)
    base = f"/levels/{level_name}/art/terrains"
    block["baseColorBaseTex"] = f"{base}/{tex.name}"
    block["annotation"] = "GRASS"
    block["groundmodelName"] = block.get("groundmodelName") or "GRASS"
    block.update(_DRY_MEADOW_TUNING)
    _upsert(data, "dry_meadow", block)
    print(
        "Ensured TerrainMaterial dry_meadow "
        f"(Grass2 {DRY_MEADOW_BLEND_GRASS2:.0%} + dirt {1.0 - DRY_MEADOW_BLEND_GRASS2:.0%} baseColor)"
    )


def ensure_forest_floor(data: dict[str, Any], terr: Path, level_name: str) -> None:
    ensure_mudgrass_texture(terr)
    block = _rewrite_level_paths(_clone_grass(data, level_name), level_name)
    base = f"/levels/{level_name}/art/terrains"
    block["baseColorBaseTex"] = f"{base}/t_terrain_base_mudgrass_b.png"
    block["annotation"] = "GRASS"
    block["groundmodelName"] = block.get("groundmodelName") or "GRASS"
    block.update(_FOREST_FLOOR_TUNING)
    _upsert(data, "ForestFloor", block)
    print("Ensured TerrainMaterial ForestFloor (Grass + mudgrass 50/50 baseColor)")


def _dim_png(src: Path, dest: Path, gain: float) -> None:
    rgb = np.asarray(Image.open(src).convert("RGB"), dtype=np.float32)
    out = np.clip(rgb * float(gain), 0, 255).astype(np.uint8)
    Image.fromarray(out, mode="RGB").save(dest)
    print(
        f"Wrote {dest.name}  albedo_gain={gain:.2f}  "
        f"mean {rgb.mean():.0f} -> {out.mean():.0f}"
    )


def ensure_snow_tirol(data: dict[str, Any], terr: Path, level_name: str) -> None:
    site = load_site()
    gain = float((site.get("beamng") or {}).get("snow_albedo_gain", 0.75))
    src = terr / "t_terrain_base_snow_b.png"
    dest = terr / "t_terrain_base_snowtirol_b.png"
    if not src.is_file():
        raise SystemExit(f"Missing {src} — template snow base color")
    _dim_png(src, dest, gain)
    block = _rewrite_level_paths(_clone_named(data, "snow", level_name), level_name)
    base = f"/levels/{level_name}/art/terrains"
    block["baseColorBaseTex"] = f"{base}/{dest.name}"
    block["annotation"] = "SNOW"
    block["groundmodelName"] = "SNOW"
    block.update(_SNOW_TIROL_TUNING)
    _upsert(data, "SnowTirol", block)
    print(
        f"Ensured TerrainMaterial SnowTirol  albedo_gain={gain:.2f}  "
        "(darker base = more sun/sky color)"
    )


def ensure_terrain_materials(user_level: Path, level_name: str) -> None:
    terr = user_level / "art" / "terrains"
    mats_path = terr / "main.materials.json"
    if not terr.is_dir():
        raise SystemExit(f"Missing terrains folder: {terr}")
    data = _load_mats(mats_path)
    ensure_dry_meadow(data, terr, level_name)
    ensure_forest_floor(data, terr, level_name)
    ensure_snow_tirol(data, terr, level_name)
    mats_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Wrote {mats_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--site",
        default="",
        help="Site YAML (sets AUTOROAD_SITE); default = active site",
    )
    ap.add_argument(
        "--level",
        default="",
        help="Override beamng.level_name",
    )
    args = ap.parse_args()
    if args.site:
        import os

        os.environ["AUTOROAD_SITE"] = args.site

    site = load_site()
    level_name = (args.level or str((site.get("beamng") or {}).get("level_name") or "")).strip()
    if not level_name:
        raise SystemExit("beamng.level_name missing")
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        raise SystemExit(f"Level folder missing: {user_level} — run setup_beamng_level.py")
    ensure_terrain_materials(user_level, level_name)


if __name__ == "__main__":
    main()
