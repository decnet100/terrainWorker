"""Pack captured tree-imposter cells into BeamNG sidecar DDS + cheap DAE.

Reads PNG cells written by the Blender capture script (albedo_*.png,
normal_*.png) or a folder produced by --dump-cells.

Usage:
  cd C:\\temp\\beamng_autoroad; python tools\\pack_tree_imposters.py --species spruce --cells C:\\temp\\assets\\trees\\out\\spruce\\cells
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from tree_imposter_lib import (  # noqa: E402
    SPECIES,
    atlas_size,
    pack_and_write,
    species_cfg,
    write_cheap_dae,
    write_tree_materials,
)

DEFAULT_PACK = ROOT / "data" / "assets" / "trees"


def _load_rgba(path: Path) -> np.ndarray:
    im = Image.open(path).convert("RGBA")
    return np.asarray(im, dtype=np.float32) / 255.0


def load_cells(cells_dir: Path, n: int, dim: int) -> tuple[list[np.ndarray], list[np.ndarray]]:
    albedo, nrm = [], []
    for i in range(n):
        a = cells_dir / f"albedo_{i:02d}.png"
        npath = cells_dir / f"normal_{i:02d}.png"
        if not a.is_file() or not npath.is_file():
            raise SystemExit(f"Missing cell pair {a.name} / {npath.name} in {cells_dir}")
        ca, cn = _load_rgba(a), _load_rgba(npath)
        if ca.shape[0] != dim or ca.shape[1] != dim:
            ca = np.asarray(
                Image.fromarray((ca * 255).astype(np.uint8), "RGBA").resize(
                    (dim, dim), Image.Resampling.BOX
                ),
                dtype=np.float32,
            ) / 255.0
            cn = np.asarray(
                Image.fromarray((cn * 255).astype(np.uint8), "RGBA").resize(
                    (dim, dim), Image.Resampling.BOX
                ),
                dtype=np.float32,
            ) / 255.0
        albedo.append(ca)
        nrm.append(cn)
    return albedo, nrm


def _copy_bark(species: str, dest: Path) -> str | None:
    """Best-effort bark albedo from the artist folder."""
    roots = [
        Path(r"C:\temp\assets\trees"),
        ROOT / "data" / "assets" / "trees",
    ]
    hints = {
        "spruce": ["white-spruce-tree-bark-bl", "SpruceBark.color"],
        "larch": ["BarkPoplar", "bark_willow"],
        "maple": ["japanese_sycamore"],
        "arve": ["pine_bark"],
    }
    want = hints.get(species, [])
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            low = p.name.lower()
            if p.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
                continue
            if "diff" not in low and "color" not in low and "col" not in low and "albedo" not in low:
                continue
            if any(h.lower() in str(p).lower() for h in want):
                target = dest / f"bark_{species}{p.suffix.lower()}"
                shutil.copy2(p, target)
                return target.name
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--species", default="spruce", choices=sorted(SPECIES))
    ap.add_argument(
        "--cells",
        type=Path,
        help="Folder with albedo_00.png .. and normal_00.png ..",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Pack folder (default data/assets/trees/<species>)",
    )
    ap.add_argument("--no-dae", action="store_true", help="Skip cheap crossplane DAE")
    args = ap.parse_args()
    spec = species_cfg(args.species)
    cells_dir = args.cells
    if cells_dir is None:
        cells_dir = Path(r"C:\temp\assets\trees") / "out" / args.species / "cells"
    if not cells_dir.is_dir():
        raise SystemExit(f"Cell folder missing: {cells_dir} — run the Blender capture first.")
    out = args.out or (DEFAULT_PACK / args.species)
    out.mkdir(parents=True, exist_ok=True)
    from tree_imposter_lib import imposter_count

    n = imposter_count(spec["equator_steps"], spec["polar_steps"], spec["include_poles"])
    dim = int(spec["dimension"])
    albedo, nrm = load_cells(cells_dir, n, dim)
    dae_name = spec["dae_stem"] + ".dae"
    info = pack_and_write(albedo, nrm, out, dae_name, spec)
    # Side card for the cheap mesh LOD: first equator view (ring around Z=0).
    # With polar_steps=1 that is the middle ring = equator_steps cells offset.
    eq = int(spec["equator_steps"])
    pol = int(spec["polar_steps"])
    equator_start = eq * pol  # middle ring when polar_steps==1 (rings: up, eq, down)
    side = albedo[min(equator_start, n - 1)]
    from tree_imposter_lib import _u8, alpha_bleed

    side_u8 = _u8(alpha_bleed(side, 6))
    card_name = f"{spec['dae_stem']}_lod1_side.png"
    Image.fromarray(side_u8, mode="RGBA").save(out / card_name)
    bark = _copy_bark(args.species, out)
    card_mat = f"Autoroad_{args.species}_Card"
    bark_mat = f"Autoroad_{args.species}_Bark"
    if not args.no_dae:
        write_cheap_dae(
            out / dae_name,
            stem=spec["dae_stem"],
            height_m=float(spec["world_size_m"]),
            radius_m=float(spec["world_size_m"]) * 0.28,
            card_png=card_name,
            bark_png=bark,
            card_mat=card_mat,
            bark_mat=bark_mat,
        )
        write_tree_materials(
            out / "main.materials.json",
            card_mat=card_mat,
            bark_mat=bark_mat,
            card_png=card_name,
            bark_png=bark,
            level_rel=f"art/shapes/trees/trees_{args.species}",
        )
    meta = {
        "species": args.species,
        "spec": spec,
        "pack": info,
        "cells_dir": str(cells_dir),
        "atlas_wh": list(atlas_size(n, dim)),
    }
    (out / "pack_summary.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
