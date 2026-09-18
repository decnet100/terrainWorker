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
JV_ZIP = STEAM_LEVELS / "johnson_valley.zip"
FIR_ZIP_PREFIX = "levels/driver_training/art/shapes/trees/trees_douglasfir/"
FIR_LOCAL_REL = "art/shapes/trees/trees_douglasfir"
DRY_GRASS_ZIP_PREFIX = "levels/johnson_valley/art/shapes/groundcover/"
DRY_GRASS_LOCAL_REL = "art/shapes/groundcover/dry_grass"

SHAPE_FILES = {
    "autoroad_fir_small": "tree_douglasfir_small_a.dae",
    "autoroad_fir_large": "tree_douglasfir_large_a.dae",
    "autoroad_fir_bush": "tree_douglasfir_bush_a.dae",
}

DRY_GRASS_SHAPES = {
    "autoroad_dry_grass_01": "s_gc_dry_grass_01.dae",
    "autoroad_dry_grass_02": "s_gc_dry_grass_02.dae",
    "autoroad_dry_grass_03": "s_gc_dry_grass_03.dae",
    "autoroad_dry_grass_04": "s_gc_dry_grass_04.dae",
}


def _item_template(
    internal: str,
    shape_vfs: str,
    *,
    radius: float,
    collidable: bool,
    annotation: str | None = None,
) -> dict:
    return {
        "class": "ForestItemData",
        "internalName": internal,
        "name": internal,
        "shapeFile": shape_vfs,
        "radius": radius,
        "collidable": collidable,
        "windScale": 1.0 if collidable else 0.8,
        "trunkBendScale": 0.45 if collidable else 0.05,
        "branchAmp": 1.0 if collidable else 0.35,
        "detailAmp": 0.25,
        "detailFreq": 1.0,
        "annotation": annotation or ("TREE" if collidable else "BUSH"),
    }


def vendor_fir_pack(user_level: Path, level_name: str) -> dict[str, dict]:
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
    print(f"Vendored douglasfir pack → {dest} ({n_copied} files)")

    base = f"/levels/{level_name}/{FIR_LOCAL_REL}"
    return {
        "autoroad_fir_small": _item_template(
            "autoroad_fir_small",
            f"{base}/{SHAPE_FILES['autoroad_fir_small']}",
            radius=1.5,
            collidable=True,
        ),
        "autoroad_fir_large": _item_template(
            "autoroad_fir_large",
            f"{base}/{SHAPE_FILES['autoroad_fir_large']}",
            radius=2.0,
            collidable=True,
        ),
        "autoroad_fir_bush": _item_template(
            "autoroad_fir_bush",
            f"{base}/{SHAPE_FILES['autoroad_fir_bush']}",
            radius=0.8,
            collidable=False,
        ),
    }


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
    print(f"Vendored dry_grass pack → {dest} ({n_copied} files)")

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
    }


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
):
    step = max(1, int(round(spacing_m / mpp)))
    rows, cols = np.where(mask)
    if rows.size == 0:
        return []
    cells = {}
    for r, c in zip(rows.tolist(), cols.tolist()):
        key = (r // step, c // step)
        if key not in cells:
            cells[key] = (r, c)
    out = []
    for r, c in cells.values():
        jr = (rng.random() - 0.5) * 2.0 * jitter * step
        jc = (rng.random() - 0.5) * 2.0 * jitter * step
        out.append((r + jr, c + jc))
    return out


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

    grass_kinds = list(DRY_GRASS_SHAPES.keys())
    placements: dict[str, list[dict]] = {
        "autoroad_fir_large": [],
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
    scrub_frac = float(np.clip(cfg["scrub_fraction"], 0.0, 0.9))
    max_scrub = int(round(max_items * scrub_frac)) if cfg["trees"] else 0
    max_high = max(0, max_items - max_scrub)
    jitter = float(cfg["jitter"])

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
        high_cells = _candidate_cells(
            high, float(cfg["spacing_high_m"]), mpp, rng, jitter
        )
        scrub_cells = _candidate_cells(
            scrub, float(cfg["spacing_scrub_m"]), mpp, rng, jitter
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
        print(f"Created Forest theForest → {items_path}")

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
        old.unlink()
    for kind, rows in placements.items():
        if not rows:
            continue
        fname = f"{kind}.forest4.json"
        path = forest_dir / fname
        with path.open("w", encoding="utf-8", newline="\n") as f:
            for row in rows:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
        (proc / fname).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Wrote {path} ({len(rows)} items)")


def main() -> None:
    site = load_site()
    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "").strip()
    if not level_name:
        raise SystemExit("beamng.level_name missing")
    cfg = _cfg(bng)
    if not cfg["enabled"]:
        raise SystemExit("forest.enabled is false")

    proc = processed_dir(site)
    if cfg["trees"] and not (proc / "mask_forest_high.png").is_file():
        raise SystemExit("Missing mask_forest_*.png — run tools/build_terrain_masks.py first")
    if cfg["dry_grass"] and not (
        (proc / "mask_biome_meadow_dry.png").is_file()
        or (proc / "mask_biome_veg_niedrig.png").is_file()
    ):
        raise SystemExit(
            "Missing mask_biome_meadow_dry.png — run tools/compose_biomes.py first"
        )

    placements = scatter(site, cfg)
    meta = {
        "level": level_name,
        "counts": {k: len(v) for k, v in placements.items()},
        "cfg": cfg,
    }
    (proc / "forest_scatter_summary.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )

    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        raise SystemExit(f"Level folder missing: {user_level}")
    ensure_forest_object(user_level)
    items: dict[str, dict] = {}
    if cfg["trees"]:
        items.update(vendor_fir_pack(user_level, level_name))
    if cfg["dry_grass"]:
        items.update(vendor_dry_grass_pack(user_level, level_name))
    write_managed(user_level, items)
    write_forest4(user_level, proc, placements)
    print("Reload the level in BeamNG (Forest object loads forest/*.forest4.json).")


if __name__ == "__main__":
    main()
