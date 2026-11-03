"""Write a small test pattern into a level's hole map, to see in the game which terrain square a hole pixel removes.

terrain_holes.py takes a hole-map pixel as a terrain vertex that removes the square north-east of it (as in
Torque3D). This script checks that in BeamNG: it adds a few holes on top of the level's hole map and prints, for each,
the square that should vanish under that rule and the one a "pixel = cell" reading would expect.

The pattern (in terrain vertices, from the point given with --at; east = +x, north = +y):
  A  one pixel at (0, 0)
  L  an L: (4, 0) (5, 0) (6, 0) east and (4, 1) (4, 2) north, so mirroring or turning shows at once
  C  two pixels touching at a corner: (9, 0) and (10, 1)

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/testarena.yaml"; python tools\\hole_rule_test.py --at 250 250
    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/testarena.yaml"; python tools\\hole_rule_test.py --restore

The first call keeps a copy of the hole map (theTerrain_holemap.before_hole_test.png); --restore puts it back.
After either call: quit BeamNG, start it, load the level, World Editor -> Terrain -> Import terrainPreset.json.
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

from site_coords import SiteCoords, load_site  # noqa: E402
from build_bridges import USER_LEVELS  # noqa: E402

PATTERN = {
    "A": [(0, 0)],
    "L": [(4, 0), (5, 0), (6, 0), (4, 1), (4, 2)],
    "C": [(9, 0), (10, 1)],
}
BACKUP = "theTerrain_holemap.before_hole_test.png"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--at", nargs=2, type=float, metavar=("X", "Y"), help="BeamNG metres of vertex (0, 0)")
    ap.add_argument("--restore", action="store_true", help="put the hole map from before the test back")
    args = ap.parse_args()

    site = load_site()
    sc = SiteCoords(site)
    bng = site.get("beamng") or {}
    level = str(bng.get("level_name") or "").strip()
    size = int(bng.get("mask_size") or 512)
    pitch = float(sc.square_m)
    imp = USER_LEVELS / level / "import"
    if not imp.is_dir():
        raise SystemExit(f"no level import folder {imp}")
    hm = imp / "theTerrain_holemap.png"
    backup = imp / BACKUP

    if args.restore:
        if not backup.is_file():
            raise SystemExit(f"no backup {backup}")
        shutil.copyfile(backup, hm)
        shutil.copyfile(backup, imp / "holeMap.png")
        backup.unlink()
        print(f"restored {hm} - re-import terrainPreset.json")
        return
    if args.at is None:
        raise SystemExit("--at X Y (BeamNG metres) or --restore")

    if hm.is_file():
        hole = np.asarray(Image.open(hm), dtype=np.uint8)
        hole = (hole[..., 0] if hole.ndim == 3 else hole).copy()
        if not backup.is_file():
            shutil.copyfile(hm, backup)
    else:
        hole = np.zeros((size, size), dtype=np.uint8)
        Image.fromarray(hole).save(backup)
    if hole.shape != (size, size):
        raise SystemExit(f"hole map is {hole.shape}, site mask_size {size}")

    i0 = int(round(args.at[0] / pitch))
    j0 = int(round(args.at[1] / pitch))
    rows = []
    for name, cells in PATTERN.items():
        for di, dj in cells:
            i, j = i0 + di, j0 + dj
            if not (0 <= i < size - 1 and 0 <= j < size - 1):
                raise SystemExit(f"pattern leaves the terrain at vertex ({i}, {j})")
            py, px = size - 1 - j, i
            hole[py, px] = 255
            x, y = i * pitch, j * pitch
            rows.append(
                f"  {name} pixel col {px} row {py} = vertex x {x:.0f} y {y:.0f}: "
                f"expected hole x {x:.0f}..{x + pitch:.0f}, y {y:.0f}..{y + pitch:.0f}"
                f"   (pixel-as-cell would be x {x - pitch / 2:.1f}..{x + pitch / 2:.1f}, "
                f"y {y - pitch / 2:.1f}..{y + pitch / 2:.1f})"
            )
    Image.fromarray(hole).save(hm)
    Image.fromarray(hole).save(imp / "holeMap.png")
    preset_path = imp / "terrainPreset.json"
    if preset_path.is_file():
        preset = json.loads(preset_path.read_text(encoding="utf-8"))
        want = f"/levels/{level}/import/theTerrain_holemap.png"
        if preset.get("holeMapPath") != want:
            preset["holeMapPath"] = want
            preset_path.write_text(json.dumps(preset, indent=2), encoding="utf-8")
    print(f"wrote {hm} (backup {backup.name})")
    print("BeamNG coordinates, terrain imported at position 0,0,0 (east = +x, north = +y):")
    print("\n".join(rows))


if __name__ == "__main__":
    main()
