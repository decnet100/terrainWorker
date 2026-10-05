"""Download DGM via WCS for the active site BBOX (cached).

Fetches the **native** cell size from ``sources.dgm.resolution_m`` (padded),
then cubic-spline resamples onto ``beamng.meters_per_pixel``. Do not set
``resolution_m`` to the level spacing — that would resample on the server.

Default: reuse data/raw/dgm_<site>.tif if present and meta matches.
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


def dgm_cache_path(site: dict) -> Path:
    return terrain_cache_path(site, "dgm", default_stem="dgm")


def fetch_dgm(site: dict, *, force: bool = False) -> Path:
    return fetch_terrain_coverage(
        site, "dgm", force=force, label="DGM", default_stem="dgm"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="Re-download even if cache exists")
    args = ap.parse_args()
    site = load_site()
    print(f"Site: {site_slug(site)}")
    fetch_dgm(site, force=args.force)


if __name__ == "__main__":
    main()
