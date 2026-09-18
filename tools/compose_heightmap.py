"""Compose DGM + heightmap layers into heightmap_<N>_composed.png.

Does not run build_* — only rematerializes whatever layers already exist.

Usage:
  $env:AUTOROAD_SITE='config/sites/fernpass_mega.yaml'
  python tools/compose_heightmap.py
  python tools/compose_heightmap.py --dump-steps
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from site_coords import load_site, processed_dir  # noqa: E402
import heightmap_layers as hml  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dump-steps",
        action="store_true",
        help=(
            "Write per-step PNGs (absolute z, relative dz, opacity) under "
            "heightmap_layers/steps/"
        ),
    )
    ap.add_argument(
        "--no-sync",
        action="store_true",
        help="Do not copy composed PNG into the BeamNG level import/ folder",
    )
    ap.add_argument(
        "--omit-layer",
        action="append",
        default=[],
        metavar="NAME",
        help="Skip a layer (repeatable): span/bridge, road_bed, water. Writes a sidecar PNG.",
    )
    args = ap.parse_args()

    site = load_site()
    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "").strip() or None
    size = int(bng.get("mask_size") or 512)
    proc = processed_dir(site)
    _elev, max_h = hml.load_dgm(proc, size)
    data = hml.load_manifest(proc)
    names = [x.get("name") for x in data.get("layers") or []]
    print(f"Layers: {names or '(none)'}")
    if args.omit_layer:
        print(f"Omit: {args.omit_layer}")
    hml.compose(
        proc,
        size=size,
        max_h=max_h,
        level_name=level_name,
        sync_import=not args.no_sync,
        dump_steps=args.dump_steps,
        skip_layers=args.omit_layer or None,
    )


if __name__ == "__main__":
    main()
