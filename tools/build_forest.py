"""Scatter BeamNG Forest items from biome / forest masks.

Writes:
  - art/shapes/trees/trees_douglasfir/ (vendored from driver_training + materials)
  - art/shapes/groundcover/dry_grass/ (vendored from johnson_valley)
  - art/forest/managedItemData.json entries
  - forest/autoroad_*.forest4.json placements

Usage:
  $env:AUTOROAD_SITE='config/sites/fernpass_mega.yaml'
  python tools/build_forest.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import load_site, processed_dir  # noqa: E402
import build_bridges as bb  # noqa: E402
import build_galleries as bg  # noqa: E402

USER_LEVELS = bb.USER_LEVELS

STEAM_LEVELS = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive\content\levels"
)
DRIVER_TRAINING_ZIP = STEAM_LEVELS / "driver_training.zip"
ITALY_ZIP = STEAM_LEVELS / "italy.zip"
JV_ZIP = STEAM_LEVELS / "johnson_valley.zip"
PINE_ZIP_PREFIX = "levels/italy/art/shapes/trees/trees_italy/"
PINE_LOCAL_REL = "art/shapes/trees/trees_pine"
PINE_SHAPE = "scots_pine.dae"
PINE_MATERIALS = ("scots_pine", "scots_pine_branches", "scots_pine_trunk")
BEECH_ZIP = USER_LEVELS.parent / "mods" / "repo" / "Aldcourt.zip"
BEECH_ZIP_PREFIX = "levels/Aldcourt/art/shapes/trees/trees_beech/"
BEECH_LOCAL_REL = "art/shapes/trees/trees_beech"
BEECH_SHAPE = "tree_beech_forest_a.dae"
BEECH_FILES = (
    BEECH_SHAPE,
    "tree_beech_forest_a.cdae",
    "tree_beech_forest_a.dae.imposter.dds",
    "tree_beech_forest_a.dae.imposter_normals.dds",
    "materials.cs",
    "vegetation_beech_bark_d.dds",
    "vegetation_beech_bark_n.dds",
    "vegetation_beech_bark_s.dds",
    "vegetation_beech_d.dds",
    "vegetation_beech_n.dds",
    "vegetation_beech_s.dds",
)
SPECIES_ITEMS = {
    "pine": "autoroad_pine_large",
    "douglas": "autoroad_douglas_large",
    "beech": "autoroad_beech_forest",
    "larch": "autoroad_larch_large",
    "bush": "autoroad_krummholz_bush",
}
FIR_ZIP_PREFIX = "levels/driver_training/art/shapes/trees/trees_douglasfir/"
FIR_LOCAL_REL = "art/shapes/trees/trees_douglasfir"
DRY_GRASS_ZIP_PREFIX = "levels/johnson_valley/art/shapes/groundcover/"
DRY_GRASS_LOCAL_REL = "art/shapes/groundcover/dry_grass"

SHAPE_FILES = {
    "autoroad_fir_small": "tree_douglasfir_small_a.dae",
    "autoroad_fir_large": "tree_douglasfir_large_a.dae",
    "autoroad_fir_bush": "tree_douglasfir_bush_a.dae",
}

SPRUCE_PACK = ROOT / "data" / "assets" / "trees" / "spruce"
SPRUCE_LOCAL_REL = "art/shapes/trees/trees_spruce"
SPRUCE_SHAPE = "tree_spruce_large.dae"
LARCH_PACK = ROOT / "data" / "assets" / "trees" / "larch"
LARCH_LOCAL_REL = "art/shapes/trees/trees_larch"
LARCH_SHAPE = "tree_larch_large.dae"

DRY_GRASS_SHAPES = {
    "autoroad_dry_grass_01": "s_gc_dry_grass_01.dae",
    "autoroad_dry_grass_02": "s_gc_dry_grass_02.dae",
    "autoroad_dry_grass_03": "s_gc_dry_grass_03.dae",
    "autoroad_dry_grass_04": "s_gc_dry_grass_04.dae",
}


def _stamp_imposter_sidecars(dae_path: Path) -> None:
    """Keep imposter sidecars newer than the DAE so the engine does not rebake them."""
    if not dae_path.is_file():
        return
    newer = dae_path.stat().st_mtime + 5.0
    for suffix in (".imposter.dds", ".imposter_normals.dds", ".asset.json"):
        side = dae_path.parent / (dae_path.name + suffix)
        if side.is_file():
            os.utime(side, (newer, newer))


def _item_template(
    internal: str,
    shape_vfs: str,
    *,
    radius: float,
    collidable: bool,
    annotation: str | None = None,
    wind_scale: float = 0.4,
    trunk_bend: float = 0.03,
    branch_amp: float = 0.02,
    detail_amp: float = 0.2,
    detail_freq: float = 5.0,
    mass: float | None = None,
    rigidity: float | None = None,
) -> dict:
    """Forest item tuned like driver_training douglasfir (gentle wind, stiff trunk)."""
    obj: dict = {
        "class": "TSForestItemData",
        "internalName": internal,
        "name": internal,
        "shapeFile": shape_vfs,
        "radius": radius,
        "collidable": collidable,
        "windScale": wind_scale,
        "trunkBendScale": trunk_bend,
        "branchAmp": branch_amp,
        "detailAmp": detail_amp,
        "detailFreq": detail_freq,
        "annotation": annotation or ("TREE" if collidable else "BUSH"),
    }
    if mass is not None:
        obj["mass"] = mass
    if rigidity is not None:
        obj["rigidity"] = rigidity
    return obj


def vendor_fir_pack(
    user_level: Path,
    level_name: str,
    *,
    cheap: bool = False,
    large_internal: str = "autoroad_fir_large",
) -> dict[str, dict]:
    """Copy douglasfir meshes + materials into this level."""
    if not DRIVER_TRAINING_ZIP.is_file():
        raise SystemExit(f"Missing {DRIVER_TRAINING_ZIP}")

    dest = user_level / FIR_LOCAL_REL
    dest.mkdir(parents=True, exist_ok=True)
    needed = {
        "main.materials.json",
        "tree_douglasfir_small_a.dae",
        "tree_douglasfir_small_a.cdae",
        "tree_douglasfir_small_a.dae.imposter.dds",
        "tree_douglasfir_small_a.dae.imposter_normals.dds",
        "tree_douglasfir_small_a.dae.asset.json",
        "tree_douglasfir_large_a.dae",
        "tree_douglasfir_large_a.cdae",
        "tree_douglasfir_large_a.dae.imposter.dds",
        "tree_douglasfir_large_a.dae.imposter_normals.dds",
        "tree_douglasfir_large_a.dae.asset.json",
        "tree_douglasfir_bush_a.dae",
        "tree_douglasfir_bush_a.cdae",
        "tree_douglasfir_bush_a.dae.imposter.dds",
        "tree_douglasfir_bush_a.dae.imposter_normals.dds",
        "tree_douglasfir_bush_a.dae.asset.json",
    }
    n_copied = 0
    with zipfile.ZipFile(DRIVER_TRAINING_ZIP) as z:
        for name in needed:
            src = FIR_ZIP_PREFIX + name
            if src not in z.namelist():
                if name.endswith(".cdae") or name.endswith(".asset.json"):
                    continue
                raise SystemExit(f"Missing in driver_training.zip: {src}")
            (dest / name).write_bytes(z.read(src))
            n_copied += 1
    for shape in SHAPE_FILES.values():
        _stamp_imposter_sidecars(dest / shape)
    print(f"Vendored douglasfir pack -> {dest} ({n_copied} files)")

    base = f"/levels/{level_name}/{FIR_LOCAL_REL}"
    if large_internal != "autoroad_fir_large":
        print(f"Stock douglas as {large_internal}")
        return {
            large_internal: _item_template(
                large_internal,
                f"{base}/{SHAPE_FILES['autoroad_fir_large']}",
                radius=2.0,
                collidable=not cheap,
                wind_scale=0.0 if cheap else 0.4,
                trunk_bend=0.0 if cheap else 0.03,
                branch_amp=0.0 if cheap else 0.02,
                detail_amp=0.0 if cheap else 0.2,
                detail_freq=1.0 if cheap else 5.0,
                mass=70.0,
                rigidity=150.0,
                annotation="TREE",
            ),
        }
    if cheap:
        print("Forest cheap mode: single LARGE fir, no collision, wind off")
        return {
            "autoroad_fir_large": _item_template(
                "autoroad_fir_large",
                f"{base}/{SHAPE_FILES['autoroad_fir_large']}",
                radius=2.0,
                collidable=False,
                wind_scale=0.0,
                trunk_bend=0.0,
                branch_amp=0.0,
                detail_amp=0.0,
                detail_freq=1.0,
                mass=70.0,
                rigidity=150.0,
                annotation="TREE",
            ),
        }
    return {
        # Wind/bend matched to driver_training stock (was trunkBend 0.45 / branchAmp 1.0 → tip glitch)
        "autoroad_fir_small": _item_template(
            "autoroad_fir_small",
            f"{base}/{SHAPE_FILES['autoroad_fir_small']}",
            radius=1.5,
            collidable=True,
            wind_scale=0.4,
            trunk_bend=0.05,
            branch_amp=0.03,
            detail_amp=0.2,
            detail_freq=5.0,
            mass=10.0,
            rigidity=20.0,
        ),
        "autoroad_fir_large": _item_template(
            "autoroad_fir_large",
            f"{base}/{SHAPE_FILES['autoroad_fir_large']}",
            radius=2.0,
            collidable=True,
            wind_scale=0.4,
            trunk_bend=0.03,
            branch_amp=0.02,
            detail_amp=0.2,
            detail_freq=5.0,
            mass=70.0,
            rigidity=150.0,
        ),
        "autoroad_fir_bush": _item_template(
            "autoroad_fir_bush",
            f"{base}/{SHAPE_FILES['autoroad_fir_bush']}",
            radius=0.8,
            collidable=False,
            wind_scale=0.5,
            trunk_bend=0.1,
            branch_amp=0.01,
            detail_amp=0.2,
            detail_freq=1.5,
            mass=1.0,
            rigidity=5.0,
            annotation="BUSH",
        ),
    }


def _rewrite_spruce_material_maps(
    mat_path: Path, level_name: str, local_rel: str = SPRUCE_LOCAL_REL
) -> None:
    """Point every texture map at /levels/<level>/<local_rel>/<file>.

    Idempotent: a doubled /levels/.../levels/... prefix from an older vendor
    pass is collapsed by taking only the filename.
    """
    if not mat_path.is_file():
        return
    data = json.loads(mat_path.read_text(encoding="utf-8"))
    vfs = f"/levels/{level_name}/{local_rel}"
    tex_keys = {
        "baseColorMap",
        "normalMap",
        "opacityMap",
        "roughnessMap",
        "ambientOcclusionMap",
        "metallicMap",
        "emissiveMap",
    }
    for block in data.values():
        if not isinstance(block, dict):
            continue
        for stage in block.get("Stages") or []:
            if not isinstance(stage, dict):
                continue
            for key, val in list(stage.items()):
                if key not in tex_keys or not isinstance(val, str) or not val:
                    continue
                fname = Path(val.replace("\\", "/")).name
                stage[key] = f"{vfs}/{fname}"
    mat_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def spruce_pack_ready(pack: Path | None = None) -> bool:
    p = pack or SPRUCE_PACK
    dae = p / SPRUCE_SHAPE
    asset = p / f"{SPRUCE_SHAPE}.asset.json"
    dds = p / f"{SPRUCE_SHAPE}.imposter.dds"
    return dae.is_file() and asset.is_file() and dds.is_file()


def vendor_spruce_pack(
    user_level: Path, level_name: str, *, cheap: bool = False
) -> dict[str, dict]:
    """Copy baked spruce DAE + imposter sidecars into the level."""
    if not spruce_pack_ready():
        raise SystemExit(
            f"Spruce pack incomplete in {SPRUCE_PACK} — "
            "run Blender tree_imposter_capture.py then tools\\pack_tree_imposters.py"
        )
    dest = user_level / SPRUCE_LOCAL_REL
    dest.mkdir(parents=True, exist_ok=True)
    n_copied = 0
    for src in SPRUCE_PACK.iterdir():
        if not src.is_file():
            continue
        if src.suffix.lower() in {".json", ".dae", ".dds", ".png", ".jpg", ".jpeg"}:
            (dest / src.name).write_bytes(src.read_bytes())
            n_copied += 1
    print(f"Vendored spruce pack -> {dest} ({n_copied} files)")
    _rewrite_spruce_material_maps(dest / "main.materials.json", level_name)
    _stamp_imposter_sidecars(dest / SPRUCE_SHAPE)
    vfs = f"/levels/{level_name}/{SPRUCE_LOCAL_REL}/{SPRUCE_SHAPE}"
    large = _item_template(
        "autoroad_fir_large",
        vfs,
        radius=2.0,
        collidable=not cheap,
        wind_scale=0.0 if cheap else 0.35,
        trunk_bend=0.0 if cheap else 0.03,
        branch_amp=0.0 if cheap else 0.02,
        detail_amp=0.0 if cheap else 0.2,
        detail_freq=1.0 if cheap else 5.0,
        mass=70.0,
        rigidity=150.0,
        annotation="TREE",
    )
    if cheap:
        print("Forest cheap mode: single SPRUCE, no collision, wind off")
        return {"autoroad_fir_large": large}
    return {
        "autoroad_fir_small": _item_template(
            "autoroad_fir_small",
            vfs,
            radius=1.5,
            collidable=True,
            wind_scale=0.35,
            trunk_bend=0.04,
            branch_amp=0.02,
            detail_amp=0.2,
            detail_freq=5.0,
            mass=10.0,
            rigidity=20.0,
        ),
        "autoroad_fir_large": large,
        "autoroad_fir_bush": _item_template(
            "autoroad_fir_bush",
            vfs,
            radius=0.8,
            collidable=False,
            wind_scale=0.4,
            trunk_bend=0.08,
            branch_amp=0.01,
            detail_amp=0.15,
            detail_freq=1.5,
            mass=1.0,
            rigidity=5.0,
            annotation="BUSH",
        ),
    }


def larch_pack_ready(pack: Path | None = None) -> bool:
    p = pack or LARCH_PACK
    dae = p / LARCH_SHAPE
    asset = p / f"{LARCH_SHAPE}.asset.json"
    dds = p / f"{LARCH_SHAPE}.imposter.dds"
    return dae.is_file() and asset.is_file() and dds.is_file()


def vendor_larch_pack(user_level: Path, level_name: str, *, cheap: bool = False) -> dict[str, dict]:
    """Copy the larch DAE and its cooker maps into the level."""
    if not larch_pack_ready():
        raise SystemExit(f"Larch pack incomplete in {LARCH_PACK}")
    dest = user_level / LARCH_LOCAL_REL
    dest.mkdir(parents=True, exist_ok=True)
    n_copied = 0
    for src in LARCH_PACK.iterdir():
        if not src.is_file():
            continue
        if src.suffix.lower() in {".json", ".dae", ".dds", ".png", ".jpg", ".jpeg"}:
            (dest / src.name).write_bytes(src.read_bytes())
            n_copied += 1
    print(f"Vendored larch pack -> {dest} ({n_copied} files)")
    _rewrite_spruce_material_maps(
        dest / "main.materials.json", level_name, LARCH_LOCAL_REL
    )
    _stamp_imposter_sidecars(dest / LARCH_SHAPE)
    vfs = f"/levels/{level_name}/{LARCH_LOCAL_REL}/{LARCH_SHAPE}"
    large = _item_template(
        "autoroad_larch_large",
        vfs,
        radius=2.0,
        collidable=not cheap,
        wind_scale=0.0 if cheap else 0.35,
        trunk_bend=0.0 if cheap else 0.03,
        branch_amp=0.0 if cheap else 0.02,
        detail_amp=0.0 if cheap else 0.2,
        detail_freq=1.0 if cheap else 5.0,
        mass=70.0,
        rigidity=150.0,
        annotation="TREE",
    )
    return {"autoroad_larch_large": large}


def vendor_variant_pack(
    user_level: Path,
    level_name: str,
    *,
    pack: Path,
    local_rel: str,
    shape: str,
    internal: str,
    cheap: bool,
) -> dict[str, dict]:
    """Copy one extra tree pack into the level and register its forest item."""
    dae = pack / shape
    if not (
        dae.is_file()
        and (pack / f"{shape}.asset.json").is_file()
        and (pack / f"{shape}.imposter.dds").is_file()
    ):
        raise SystemExit(f"Tree pack incomplete in {pack}")
    dest = user_level / local_rel
    dest.mkdir(parents=True, exist_ok=True)
    n_copied = 0
    for src in pack.iterdir():
        if src.is_file() and src.suffix.lower() in {".json", ".dae", ".dds", ".png", ".jpg", ".jpeg"}:
            (dest / src.name).write_bytes(src.read_bytes())
            n_copied += 1
    print(f"Vendored {internal} -> {dest} ({n_copied} files)")
    _rewrite_spruce_material_maps(dest / "main.materials.json", level_name, local_rel)
    _stamp_imposter_sidecars(dest / shape)
    item = _item_template(
        internal,
        f"/levels/{level_name}/{local_rel}/{shape}",
        radius=2.0,
        collidable=not cheap,
        wind_scale=0.0 if cheap else 0.35,
        trunk_bend=0.0 if cheap else 0.03,
        branch_amp=0.0 if cheap else 0.02,
        detail_amp=0.0 if cheap else 0.2,
        detail_freq=1.0 if cheap else 5.0,
        mass=70.0,
        rigidity=150.0,
        annotation="TREE",
    )
    return {internal: item}


def vendor_pine_pack(
    user_level: Path, level_name: str, *, cheap: bool = False
) -> dict[str, dict]:
    """Copy the Italy Scots pine and the three materials that point at /assets."""
    if not ITALY_ZIP.is_file():
        raise SystemExit(f"Missing {ITALY_ZIP}")
    dest = user_level / PINE_LOCAL_REL
    dest.mkdir(parents=True, exist_ok=True)
    needed = [
        PINE_SHAPE,
        "scots_pine.cdae",
        "scots_pine.dae.asset.json",
        "scots_pine.dae.imposter.dds",
        "scots_pine.dae.imposter_normals.dds",
    ]
    with zipfile.ZipFile(ITALY_ZIP) as z:
        names = set(z.namelist())
        for name in needed:
            src = PINE_ZIP_PREFIX + name
            if src not in names:
                if name.endswith(".cdae"):
                    continue
                raise SystemExit(f"Missing in italy.zip: {src}")
            (dest / name).write_bytes(z.read(src))
        raw = json.loads(z.read(PINE_ZIP_PREFIX + "main.materials.json"))
    keep = {key: raw[key] for key in PINE_MATERIALS if key in raw}
    if len(keep) != len(PINE_MATERIALS):
        missing = [key for key in PINE_MATERIALS if key not in keep]
        raise SystemExit(f"Scots pine materials missing: {missing}")
    (dest / "main.materials.json").write_text(
        json.dumps(keep, indent=2) + "\n", encoding="utf-8"
    )
    _stamp_imposter_sidecars(dest / PINE_SHAPE)
    print(f"Vendored scots pine -> {dest}")
    item = _item_template(
        "autoroad_pine_large",
        f"/levels/{level_name}/{PINE_LOCAL_REL}/{PINE_SHAPE}",
        radius=2.0,
        collidable=not cheap,
        wind_scale=0.0 if cheap else 0.35,
        trunk_bend=0.0 if cheap else 0.03,
        branch_amp=0.0 if cheap else 0.02,
        detail_amp=0.0 if cheap else 0.2,
        detail_freq=1.0 if cheap else 5.0,
        mass=70.0,
        rigidity=150.0,
        annotation="TREE",
    )
    return {"autoroad_pine_large": item}


def vendor_beech_pack(
    user_level: Path, level_name: str, *, cheap: bool = False
) -> dict[str, dict]:
    """Copy the Aldcourt forest beech. Materials stay the stock materials.cs."""
    if not BEECH_ZIP.is_file():
        raise SystemExit(f"Missing {BEECH_ZIP}")
    dest = user_level / BEECH_LOCAL_REL
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(BEECH_ZIP) as z:
        names = set(z.namelist())
        for name in BEECH_FILES:
            src = BEECH_ZIP_PREFIX + name
            if src not in names:
                if name.endswith(".cdae"):
                    continue
                raise SystemExit(f"Missing in Aldcourt.zip: {src}")
            (dest / name).write_bytes(z.read(src))
    _stamp_imposter_sidecars(dest / BEECH_SHAPE)
    print(f"Vendored beech -> {dest}")
    item = _item_template(
        "autoroad_beech_forest",
        f"/levels/{level_name}/{BEECH_LOCAL_REL}/{BEECH_SHAPE}",
        radius=2.0,
        collidable=not cheap,
        wind_scale=0.0 if cheap else 0.35,
        trunk_bend=0.0 if cheap else 0.03,
        branch_amp=0.0 if cheap else 0.02,
        detail_amp=0.0 if cheap else 0.2,
        detail_freq=1.0 if cheap else 5.0,
        mass=70.0,
        rigidity=150.0,
        annotation="TREE",
    )
    return {"autoroad_beech_forest": item}


def vendor_dry_grass_pack(user_level: Path, level_name: str) -> dict[str, dict]:
    """Copy Johnson Valley dry-grass groundcover into this level."""
    if not JV_ZIP.is_file():
        raise SystemExit(f"Missing {JV_ZIP}")

    dest = user_level / DRY_GRASS_LOCAL_REL
    dest.mkdir(parents=True, exist_ok=True)
    needed = {"main.materials.json"}
    for dae in DRY_GRASS_SHAPES.values():
        needed.add(dae)
        needed.add(dae.replace(".dae", ".cdae"))

    n_copied = 0
    with zipfile.ZipFile(JV_ZIP) as z:
        names = set(z.namelist())
        for name in sorted(needed):
            src = DRY_GRASS_ZIP_PREFIX + name
            if src not in names:
                if name.endswith(".cdae"):
                    continue
                raise SystemExit(f"Missing in johnson_valley.zip: {src}")
            if name == "main.materials.json":
                full = json.loads(z.read(src).decode("utf-8"))
                keep = {
                    k: v
                    for k, v in full.items()
                    if "dry_grass" in k.lower()
                    or "dry_grass" in json.dumps(v).lower()
                }
                (dest / name).write_text(json.dumps(keep, indent=2), encoding="utf-8")
            else:
                (dest / name).write_bytes(z.read(src))
            n_copied += 1
    print(f"Vendored dry_grass pack -> {dest} ({n_copied} files)")

    base = f"/levels/{level_name}/{DRY_GRASS_LOCAL_REL}"
    items = {}
    for internal, dae in DRY_GRASS_SHAPES.items():
        items[internal] = _item_template(
            internal,
            f"{base}/{dae}",
            radius=0.35,
            collidable=False,
            annotation="GRASS",
        )
    return items


def _cfg(bng: dict) -> dict:
    raw = bng.get("forest") or {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "trees": bool(raw.get("trees", True)),
        "dry_grass": bool(raw.get("dry_grass", True)),
        "cheap": bool(raw.get("cheap", False)),
        "spacing_high_m": float(raw.get("spacing_high_m") or 10.0),
        "spacing_scrub_m": float(raw.get("spacing_scrub_m") or 5.0),
        "spacing_meadow_m": float(raw.get("spacing_meadow_m") or 4.0),
        "jitter": float(raw.get("jitter") if raw.get("jitter") is not None else 0.45),
        "max_items": int(raw.get("max_items") or 12000),
        "max_items_meadow": int(
            raw.get("max_items_meadow")
            if raw.get("max_items_meadow") is not None
            else (raw.get("max_items") or 12000)
        ),
        "slope_max_deg": float(raw.get("slope_max_deg") or 42.0),
        "slope_max_meadow_deg": float(
            raw.get("slope_max_meadow_deg")
            if raw.get("slope_max_meadow_deg") is not None
            else (raw.get("slope_max_deg") or 42.0)
        ),
        "clear_road_m": float(raw.get("clear_road_m") or 4.0),
        "scale_high": list(raw.get("scale_high") or [0.85, 1.25]),
        "scale_scrub": list(raw.get("scale_scrub") or [0.7, 1.15]),
        "scale_meadow": list(raw.get("scale_meadow") or [0.9, 1.4]),
        "scrub_fraction": float(
            raw.get("scrub_fraction") if raw.get("scrub_fraction") is not None else 0.35
        ),
        "seed": int(raw.get("seed") or 13),
        "shape_pack": str(raw.get("shape_pack") or "douglasfir").strip().lower(),
        "hull_skip_interior": bool(raw.get("hull_skip_interior", True)),
        "larch_share": float(raw.get("larch_share") or 0.0),
        "spruceb_share": float(raw.get("spruceb_share") or 0.0),
        "sprucec_share": float(raw.get("sprucec_share") or 0.0),
        "douglas_share": float(raw.get("douglas_share") or 0.0),
        "pine_share": float(raw.get("pine_share") or 0.0),
        "waldveg_enabled": bool((raw.get("waldveg") or {}).get("enabled", False)),
        "waldveg_url": str((raw.get("waldveg") or {}).get("url") or "").strip(),
        "waldveg_types": {
            str(code): {str(k): float(v) for k, v in (mix or {}).items()}
            for code, mix in ((raw.get("waldveg") or {}).get("types") or {}).items()
        },
        "waldveg_default": {
            str(k): float(v)
            for k, v in (((raw.get("waldveg") or {}).get("default")) or {}).items()
        },
    }


def _spruce_items() -> list[str]:
    items = ["autoroad_fir_large"]
    for key, internal, shape in (
        ("spruceb", "autoroad_spruceb_large", "tree_spruceb_large.dae"),
        ("sprucec", "autoroad_sprucec_large", "tree_sprucec_large.dae"),
    ):
        if (ROOT / "data" / "assets" / "trees" / key / shape).is_file():
            items.append(internal)
    return items


def _expand_mix(mix: dict, spruce_items: list[str]) -> list[tuple[str, float]]:
    """Species shares to a cumulative roll table. Spruce is split across its meshes."""
    weighted: list[tuple[str, float]] = []
    total = 0.0
    for species, share in mix.items():
        share = float(share)
        if share <= 0.0:
            continue
        if species == "spruce":
            if not spruce_items:
                raise SystemExit("spruce share set but no spruce mesh is available")
            each = share / len(spruce_items)
            for item in spruce_items:
                total += each
                weighted.append((item, total))
        else:
            item = SPECIES_ITEMS.get(species)
            if item is None:
                raise SystemExit(f"Unknown tree species in waldveg mix: {species}")
            total += share
            weighted.append((item, total))
    if weighted and not 0.98 <= total <= 1.02:
        raise SystemExit(f"Waldveg shares sum to {total:.3f}, expected 1")
    if total <= 0.0:
        return []
    scale = 1.0 / total
    acc = 0.0
    table: list[tuple[str, float]] = []
    for item, raw_acc in weighted:
        acc = raw_acc * scale
        table.append((item, acc))
    if table:
        table[-1] = (table[-1][0], 1.0)
    return table


def _waldveg_species(cfg: dict) -> set[str]:
    if not cfg.get("waldveg_enabled"):
        return set()
    used: set[str] = set()
    mixes = list((cfg.get("waldveg_types") or {}).values())
    default = cfg.get("waldveg_default") or {}
    if default:
        mixes.append(default)
    for mix in mixes:
        for name, share in mix.items():
            if float(share) > 0.0:
                used.add(str(name))
    return used


def _yaw_matrix(yaw_rad: float) -> list[float]:
    c, s = math.cos(yaw_rad), math.sin(yaw_rad)
    return [c, -s, 0.0, s, c, 0.0, 0.0, 0.0, 1.0]


def _load_mask(path: Path, size: int) -> np.ndarray:
    if not path.is_file():
        return np.zeros((size, size), dtype=bool)
    arr = np.asarray(Image.open(path).convert("L"), dtype=np.uint8)
    if arr.shape[0] != size or arr.shape[1] != size:
        arr = np.asarray(
            Image.fromarray(arr, mode="L").resize((size, size), Image.Resampling.NEAREST),
            dtype=np.uint8,
        )
    return arr > 0


def _dilate(mask: np.ndarray, px: int) -> np.ndarray:
    if px <= 0 or not mask.any():
        return mask
    img = Image.fromarray(mask.astype(np.uint8) * 255, mode="L")
    from PIL import ImageFilter

    for _ in range(px):
        img = img.filter(ImageFilter.MaxFilter(3))
    return np.asarray(img, dtype=np.uint8) > 0


def _candidate_cells(
    mask: np.ndarray,
    spacing_m: float,
    mpp: float,
    rng: np.random.Generator,
    jitter: float,
    *,
    max_candidates: int | None = None,
):
    step = max(1, int(round(spacing_m / mpp)))
    rows, cols = np.where(mask)
    if rows.size == 0:
        return []
    # One seed per spacing cell (vectorized).
    keys = (rows // step).astype(np.int64) * (mask.shape[1] // step + 1) + (
        cols // step
    ).astype(np.int64)
    _, uniq = np.unique(keys, return_index=True)
    rows = rows[uniq].astype(np.float64)
    cols = cols[uniq].astype(np.float64)
    if max_candidates is not None and rows.size > max_candidates:
        pick = rng.choice(rows.size, size=int(max_candidates), replace=False)
        rows = rows[pick]
        cols = cols[pick]
    jr = (rng.random(rows.size) - 0.5) * 2.0 * jitter * step
    jc = (rng.random(cols.size) - 0.5) * 2.0 * jitter * step
    return list(zip((rows + jr).tolist(), (cols + jc).tolist()))


def scatter(site: dict, cfg: dict) -> dict[str, list[dict]]:
    proc = processed_dir(site)
    bng = site.get("beamng") or {}
    size = int(bng.get("mask_size") or 2048)
    mpp = float(bng.get("meters_per_pixel") or 1.0)
    extent = size * mpp
    z_at, slope_at = bg.load_terrain_z_slope(site)
    rng = np.random.default_rng(int(cfg["seed"]))

    high = _load_mask(proc / "mask_forest_high.png", size)
    scrub = _load_mask(proc / "mask_forest_scrub.png", size)
    meadow = _load_mask(proc / "mask_biome_meadow_dry.png", size)
    if not meadow.any():
        meadow = _load_mask(proc / "mask_biome_veg_niedrig.png", size)
    water = _load_mask(proc / "mask_water.png", size)
    asphalt = _load_mask(proc / "mask_asphalt.png", size)
    shoulder = _load_mask(proc / "mask_shoulder.png", size)
    settlement = _load_mask(proc / "mask_settlement.png", size)
    kill = _load_mask(proc / "mask_biome_kill.png", size)
    clear = _dilate(
        asphalt | shoulder | water | settlement | kill,
        max(1, int(round(float(cfg["clear_road_m"]) / mpp))),
    )

    high = high & ~clear
    scrub = scrub & ~high & ~clear
    meadow = meadow & ~clear & ~high & ~scrub
    if cfg.get("hull_skip_interior"):
        hull_in = _load_mask(proc / "mask_forest_hull_interior.png", size)
        if hull_in.any():
            high = high & ~hull_in
            scrub = scrub & ~hull_in
            print(
                f"Forest: skip hull interior {100.0 * float(hull_in.mean()):.2f}% "
                "(rim imposters remain)"
            )

    grass_kinds = list(DRY_GRASS_SHAPES.keys())
    cheap = bool(cfg.get("cheap"))
    placements: dict[str, list[dict]] = {
        "autoroad_fir_large": [],
        "autoroad_larch_large": [],
        "autoroad_spruceb_large": [],
        "autoroad_sprucec_large": [],
        "autoroad_douglas_large": [],
        "autoroad_pine_large": [],
        "autoroad_beech_forest": [],
        "autoroad_krummholz_bush": [],
        "autoroad_fir_small": [],
        "autoroad_fir_bush": [],
        **{k: [] for k in grass_kinds},
    }
    slope_max = float(cfg["slope_max_deg"])
    slope_meadow = float(cfg["slope_max_meadow_deg"])
    s_hi = cfg["scale_high"]
    s_sc = cfg["scale_scrub"]
    s_md = cfg["scale_meadow"]
    total = 0
    high_total = 0
    scrub_total = 0
    meadow_total = 0
    max_items = int(cfg["max_items"])
    max_meadow = int(cfg["max_items_meadow"])
    if cheap:
        # All budget on one non-collidable bush type.
        scrub_frac = 0.0
        max_scrub = 0
        max_high = max_items
    else:
        scrub_frac = float(np.clip(cfg["scrub_fraction"], 0.0, 0.9))
        max_scrub = int(round(max_items * scrub_frac)) if cfg["trees"] else 0
        max_high = max(0, max_items - max_scrub)
    jitter = float(cfg["jitter"])
    # Large caps lose too many cells to slope and road clearance at a 10% surplus.
    headroom = max(1000, max_items // 10)
    if max_items >= 100_000:
        headroom = max(headroom, max_items // 2)
    cand_cap = max_items + headroom

    def try_place(
        kind: str,
        r: float,
        c: float,
        scale_lo: float,
        scale_hi: float,
        *,
        slope_limit: float,
        budget: str = "high",
    ) -> str:
        nonlocal total, high_total, scrub_total, meadow_total
        if budget == "high" and high_total >= max_high:
            return "full"
        if budget == "scrub" and scrub_total >= max_scrub:
            return "full"
        if budget == "meadow" and meadow_total >= max_meadow:
            return "full"
        if budget in ("high", "scrub") and total >= max_items:
            return "full"
        bx = float(c) * mpp
        by = float((size - 1) - r) * mpp
        if bx < 0 or by < 0 or bx > extent or by > extent:
            return "skip"
        if float(slope_at(bx, by)) > slope_limit:
            return "skip"
        z = float(z_at(bx, by))
        yaw = float(rng.uniform(0, 2 * math.pi))
        scale = float(rng.uniform(scale_lo, scale_hi))
        placements[kind].append(
            {
                "type": kind,
                "pos": [bx, by, z],
                "rotationMatrix": _yaw_matrix(yaw),
                "scale": scale,
            }
        )
        if budget == "meadow":
            meadow_total += 1
        elif budget == "scrub":
            scrub_total += 1
            total += 1
        else:
            high_total += 1
            total += 1
        return "ok"

    if cfg["trees"]:
        # Cheap density test: merge high+scrub, one large fir.
        if cheap:
            dense = high | scrub
            high_cells = _candidate_cells(
                dense,
                float(cfg["spacing_high_m"]),
                mpp,
                rng,
                jitter,
                max_candidates=cand_cap,
            )
            rng.shuffle(high_cells)
            use_waldveg = bool(cfg.get("waldveg_enabled"))
            veg = None
            id_to_code: dict[int, str] = {}
            tables: dict[str, list[tuple[str, float]]] = {}
            default_table: list[tuple[str, float]] = []
            if use_waldveg:
                from fetch_tirol_waldveg import CODES_NAME, MASK_NAME  # noqa: WPS433

                veg_path = proc / MASK_NAME
                if not veg_path.is_file():
                    raise SystemExit(f"Missing {veg_path}")
                veg = np.asarray(Image.open(veg_path), dtype=np.uint8)
                if veg.ndim == 3:
                    veg = veg[..., 0]
                meta = json.loads((proc / CODES_NAME).read_text(encoding="utf-8"))
                id_to_code = {int(v): str(k) for k, v in (meta.get("ids") or {}).items()}
                spruce_items = _spruce_items()
                tables = {
                    code: _expand_mix(mix, spruce_items)
                    for code, mix in (cfg.get("waldveg_types") or {}).items()
                }
                default_table = _expand_mix(cfg.get("waldveg_default") or {}, spruce_items)
            else:
                mix = (
                    ("autoroad_larch_large", float(cfg.get("larch_share") or 0.0)),
                    ("autoroad_spruceb_large", float(cfg.get("spruceb_share") or 0.0)),
                    ("autoroad_sprucec_large", float(cfg.get("sprucec_share") or 0.0)),
                    ("autoroad_douglas_large", float(cfg.get("douglas_share") or 0.0)),
                    ("autoroad_pine_large", float(cfg.get("pine_share") or 0.0)),
                )
            skipped = 0
            by_code: dict[str, int] = {}
            for r, c in high_cells:
                if use_waldveg:
                    ri = int(round(r))
                    ci = int(round(c))
                    code = ""
                    if veg is not None and 0 <= ri < veg.shape[0] and 0 <= ci < veg.shape[1]:
                        code = id_to_code.get(int(veg[ri, ci]), "")
                    table = default_table if code == "" else tables.get(code)
                    if not table:
                        skipped += 1
                        continue
                    roll = float(rng.random())
                    kind = table[-1][0]
                    for name, acc in table:
                        if roll < acc:
                            kind = name
                            break
                else:
                    roll = float(rng.random())
                    kind = "autoroad_fir_large"
                    acc = 0.0
                    for name, share in mix:
                        acc += share
                        if roll < acc:
                            kind = name
                            break
                    code = ""
                if kind == "autoroad_krummholz_bush":
                    scale_lo, scale_hi_m = float(s_sc[0]), float(s_sc[1])
                else:
                    scale_lo, scale_hi_m = float(s_hi[0]), float(s_hi[1])
                status = try_place(
                    kind,
                    r,
                    c,
                    scale_lo,
                    scale_hi_m,
                    slope_limit=slope_max,
                    budget="high",
                )
                if status == "ok" and use_waldveg:
                    by_code[code or "(ohne Typ)"] = by_code.get(code or "(ohne Typ)", 0) + 1
                if status == "full":
                    break
            if use_waldveg:
                print(f"Waldveg: {skipped} candidates on types without a tree mix")
                for code, n in sorted(by_code.items(), key=lambda kv: -kv[1]):
                    print(f"  {code:12} {n}")
        else:
            high_cells = _candidate_cells(
                high,
                float(cfg["spacing_high_m"]),
                mpp,
                rng,
                jitter,
                max_candidates=cand_cap,
            )
            scrub_cells = _candidate_cells(
                scrub,
                float(cfg["spacing_scrub_m"]),
                mpp,
                rng,
                jitter,
                max_candidates=cand_cap,
            )
            rng.shuffle(high_cells)
            rng.shuffle(scrub_cells)

            for r, c in high_cells:
                kind = "autoroad_fir_large" if rng.random() < 0.35 else "autoroad_fir_small"
                status = try_place(
                    kind,
                    r,
                    c,
                    float(s_hi[0]),
                    float(s_hi[1]),
                    slope_limit=slope_max,
                    budget="high",
                )
                if status == "full":
                    break

            for r, c in scrub_cells:
                status = try_place(
                    "autoroad_fir_bush",
                    r,
                    c,
                    float(s_sc[0]),
                    float(s_sc[1]),
                    slope_limit=slope_max,
                    budget="scrub",
                )
                if status == "full":
                    break

    if cfg["dry_grass"]:
        meadow_cells = _candidate_cells(
            meadow, float(cfg["spacing_meadow_m"]), mpp, rng, jitter
        )
        rng.shuffle(meadow_cells)
        for r, c in meadow_cells:
            kind = grass_kinds[int(rng.integers(0, len(grass_kinds)))]
            status = try_place(
                kind,
                r,
                c,
                float(s_md[0]),
                float(s_md[1]),
                slope_limit=slope_meadow,
                budget="meadow",
            )
            if status == "full":
                break

    n_grass = sum(len(placements[k]) for k in grass_kinds)
    print(
        f"Forest scatter: high={high_total} scrub={scrub_total} "
        f"dry_grass={n_grass} total_trees={total} "
        f"(caps high={max_high} scrub={max_scrub} meadow={max_meadow})"
    )
    return placements


def write_managed(user_level: Path, items: dict) -> None:
    path = user_level / "art" / "forest" / "managedItemData.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if path.is_file() and path.stat().st_size:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    for name, block in items.items():
        data[name] = block
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Updated {path}")


def ensure_forest_object(user_level: Path) -> None:
    """BeamNG only loads forest/*.forest4.json if a Forest scene object exists."""
    veg = user_level / "main" / "MissionGroup" / "level_objects" / "vegetation"
    veg.mkdir(parents=True, exist_ok=True)
    items_path = veg / "items.level.json"
    rows: list[dict] = []
    if items_path.is_file() and items_path.stat().st_size:
        for ln in items_path.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    if any(r.get("class") == "Forest" for r in rows):
        print(f"Forest object already present in {items_path.relative_to(user_level)}")
    else:
        rows.append(
            {
                "name": "theForest",
                "class": "Forest",
                "__parent": "vegetation",
                "persistentId": "564dc79c-697c-4544-838c-ca62b097e065",
            }
        )
        with items_path.open("w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
        print(f"Created Forest theForest -> {items_path}")

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
    if "vegetation" not in names:
        lines.append(
            json.dumps(
                {
                    "name": "vegetation",
                    "class": "SimGroup",
                    "__parent": "level_objects",
                    "enabled": "1",
                    "persistentId": "c520cc7b-0b9e-4ac5-8afb-6999aada3e25",
                },
                separators=(",", ":"),
            )
        )
        lo_items.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("Registered SimGroup vegetation under level_objects")


def write_forest4(user_level: Path, proc: Path, placements: dict[str, list[dict]]) -> None:
    forest_dir = user_level / "forest"
    forest_dir.mkdir(parents=True, exist_ok=True)
    for old in forest_dir.glob("autoroad_*.forest4.json"):
        if old.name.startswith("autoroad_canopy"):
            continue
        old.unlink()
    for kind, rows in placements.items():
        if not rows:
            continue
        fname = f"{kind}.forest4.json"
        path = forest_dir / fname
        with path.open("w", encoding="utf-8", newline="\n") as f:
            for row in rows:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
        # Skip duplicating huge cheap dumps into processed/
        if len(rows) <= 80000:
            (proc / fname).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Wrote {path} ({len(rows)} items)")


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--cheap",
        action="store_true",
        help="Density stress: one LARGE fir shape, no collision (overrides YAML cheap)",
    )
    args = ap.parse_args()

    site = load_site()
    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "").strip()
    if not level_name:
        raise SystemExit("beamng.level_name missing")
    cfg = _cfg(bng)
    if args.cheap:
        cfg["cheap"] = True
    if not cfg["enabled"]:
        raise SystemExit("forest.enabled is false")
    if cfg["cheap"]:
        cfg["dry_grass"] = False
        cfg["max_items_meadow"] = 0
        print(
            f"Forest CHEAP density test: max_items={cfg['max_items']} "
            f"spacing_high={cfg['spacing_high_m']}m"
        )

    proc = processed_dir(site)
    if cfg["trees"] and not (proc / "mask_forest_high.png").is_file():
        raise SystemExit("Missing mask_forest_*.png - run tools/build_terrain_masks.py first")
    if cfg.get("waldveg_enabled"):
        if not cfg.get("waldveg_url"):
            raise SystemExit("forest.waldveg.url missing")
        from fetch_tirol_waldveg import ensure_waldveg_mask  # noqa: WPS433

        ensure_waldveg_mask(site, cfg["waldveg_url"])
    if cfg["dry_grass"] and not (
        (proc / "mask_biome_meadow_dry.png").is_file()
        or (proc / "mask_biome_veg_niedrig.png").is_file()
    ):
        raise SystemExit(
            "Missing mask_biome_meadow_dry.png - run tools/compose_biomes.py first"
        )

    placements = scatter(site, cfg)
    n_trees = sum(
        len(v)
        for k, v in placements.items()
        if k.startswith(
            (
                "autoroad_fir",
                "autoroad_larch",
                "autoroad_spruce",
                "autoroad_douglas",
                "autoroad_pine",
                "autoroad_beech",
                "autoroad_krummholz",
            )
        )
    )
    meta = {
        "level": level_name,
        "counts": {k: len(v) for k, v in placements.items()},
        "total_trees": n_trees,
        "cfg": cfg,
        "eval_notes": {
            "phase": "cheap_imposters" if cfg.get("cheap") else "normal",
            "howto": [
                "Quit BeamNG fully, then load the level (forest JSON + managedItemData).",
                "Note FPS standing in dense forest and while driving ~80 km/h.",
                "Note load time / hitch when forest chunks stream in.",
            ],
            "fps_standing": None,
            "fps_driving": None,
            "load_notes": None,
            "looks_dense_enough": None,
        },
    }
    (proc / "forest_scatter_summary.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    print(f"Wrote {proc / 'forest_scatter_summary.json'} (total_trees={n_trees})")

    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        raise SystemExit(f"Level folder missing: {user_level}")
    from setup_beamng_level import disable_template_forest  # noqa: WPS433

    disable_template_forest(user_level)
    ensure_forest_object(user_level)
    items: dict[str, dict] = {}
    if cfg["trees"]:
        pack = str(cfg.get("shape_pack") or "douglasfir")
        if pack == "spruce":
            if spruce_pack_ready():
                items.update(
                    vendor_spruce_pack(user_level, level_name, cheap=bool(cfg["cheap"]))
                )
                used = _waldveg_species(cfg)
                if float(cfg.get("larch_share") or 0.0) > 0.0 or "larch" in used:
                    if larch_pack_ready():
                        items.update(
                            vendor_larch_pack(
                                user_level, level_name, cheap=bool(cfg["cheap"])
                            )
                        )
                    else:
                        print(f"larch_share set but {LARCH_PACK} is incomplete")
                for key, internal, shape in (
                    ("spruceb", "autoroad_spruceb_large", "tree_spruceb_large.dae"),
                    ("sprucec", "autoroad_sprucec_large", "tree_sprucec_large.dae"),
                ):
                    if float(cfg.get(f"{key}_share") or 0.0) <= 0.0 and "spruce" not in used:
                        continue
                    variant = ROOT / "data" / "assets" / "trees" / key
                    if (variant / shape).is_file():
                        items.update(
                            vendor_variant_pack(
                                user_level,
                                level_name,
                                pack=variant,
                                local_rel=f"art/shapes/trees/trees_{key}",
                                shape=shape,
                                internal=internal,
                                cheap=bool(cfg["cheap"]),
                            )
                        )
                    else:
                        print(f"{key}_share set but {variant} is incomplete")
                if (
                    float(cfg.get("douglas_share") or 0.0) > 0.0
                    or "douglas" in used
                    or "bush" in used
                ):
                    items.update(
                        vendor_fir_pack(
                            user_level,
                            level_name,
                            cheap=bool(cfg["cheap"]),
                            large_internal="autoroad_douglas_large",
                        )
                    )
                if "bush" in used:
                    items["autoroad_krummholz_bush"] = _item_template(
                        "autoroad_krummholz_bush",
                        f"/levels/{level_name}/{FIR_LOCAL_REL}/{SHAPE_FILES['autoroad_fir_bush']}",
                        radius=0.8,
                        collidable=not bool(cfg["cheap"]),
                        wind_scale=0.0 if cfg["cheap"] else 0.5,
                        trunk_bend=0.0 if cfg["cheap"] else 0.1,
                        branch_amp=0.0 if cfg["cheap"] else 0.01,
                        detail_amp=0.0 if cfg["cheap"] else 0.2,
                        detail_freq=1.0 if cfg["cheap"] else 1.5,
                        mass=1.0,
                        rigidity=5.0,
                        annotation="BUSH",
                    )
                if float(cfg.get("pine_share") or 0.0) > 0.0 or "pine" in used:
                    items.update(
                        vendor_pine_pack(
                            user_level, level_name, cheap=bool(cfg["cheap"])
                        )
                    )
                if "beech" in used:
                    items.update(
                        vendor_beech_pack(
                            user_level, level_name, cheap=bool(cfg["cheap"])
                        )
                    )
            else:
                print(
                    f"shape_pack=spruce but {SPRUCE_PACK} is incomplete — "
                    "falling back to douglasfir"
                )
                items.update(
                    vendor_fir_pack(user_level, level_name, cheap=bool(cfg["cheap"]))
                )
        else:
            items.update(vendor_fir_pack(user_level, level_name, cheap=bool(cfg["cheap"])))
    if cfg["dry_grass"]:
        items.update(vendor_dry_grass_pack(user_level, level_name))
    write_managed(user_level, items)
    write_forest4(user_level, proc, placements)
    print("Quit BeamNG fully and restart - Forest object loads forest/*.forest4.json.")


if __name__ == "__main__":
    main()