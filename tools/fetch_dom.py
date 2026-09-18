"""Download DOM (DSM) via WCS for the active site BBOX (cached).

Tirol names this digitales Oberflächenmodell (DOM); same as DSM.
Used later for vegetation height: nDSM ≈ max(0, DOM − DGM).

Default: reuse data/raw/dom_<site>.tif if present.
Re-download only with --force.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import load_site, site_slug  # noqa: E402
from wcs_terrain import fetch_terrain_coverage, terrain_cache_path  # noqa: E402


def dom_cache_path(site: dict) -> Path:
    return terrain_cache_path(site, "dom", default_stem="dom")


# Alias for callers that say DSM
dsm_cache_path = dom_cache_path


def fetch_dom(site: dict, *, force: bool = False) -> Path:
    return fetch_terrain_coverage(
        site, "dom", force=force, label="DOM/DSM", default_stem="dom"
    )


fetch_dsm = fetch_dom


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="Re-download even if cache exists")
    args = ap.parse_args()
    site = load_site()
    print(f"Site: {site_slug(site)}")
    fetch_dom(site, force=args.force)


if __name__ == "__main__":
    main()
