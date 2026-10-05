"""Apply constant CRS offset to roads_beamng.json and refill Z."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from site_coords import SiteCoords, load_site, processed_dir  # noqa: E402
from snap_centerline_to_road_area import _fill_z_from_heightmap  # noqa: E402
from sweep_centerline_offset_grade import _shift_roads  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--d-east", type=float, required=True)
    ap.add_argument("--d-north", type=float, required=True)
    args = ap.parse_args()

    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    src = proc / "roads_beamng.json"
    if not src.is_file():
        raise SystemExit(f"Missing {src}")
    bak = proc / "roads_beamng_before_crs_offset.json"
    if not bak.is_file():
        shutil.copy2(src, bak)
        print(f"Backup {bak.name}")
    else:
        print(f"Keeping existing backup {bak.name}")

    roads = json.loads(src.read_text(encoding="utf-8"))
    shifted = _shift_roads(roads, sc, float(args.d_east), float(args.d_north))
    _fill_z_from_heightmap(site, shifted)
    src.write_text(json.dumps(shifted, indent=2), encoding="utf-8")
    meta = {
        "d_east_m": float(args.d_east),
        "d_north_m": float(args.d_north),
        "source_backup": str(bak.relative_to(ROOT)).replace("\\", "/"),
        "note": "Constant CRS translation after TrasRoadArea snap; Z re-sampled.",
    }
    (proc / "roads_beamng_crs_offset.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    print(
        f"Applied dE={args.d_east} dN={args.d_north} -> {src} "
        f"({len(shifted)} roads)"
    )


if __name__ == "__main__":
    main()
