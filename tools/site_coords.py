"""Site CRS ↔ BeamNG terrain meters, paths, and active site selection.

Active site (first match):
  1. AUTOROAD_SITE env (path relative to repo or absolute)
  2. config/site.yaml
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def resolve_site_path(path: Path | str | None = None) -> Path:
    if path is not None:
        p = Path(path)
        return p if p.is_absolute() else ROOT / p
    env = os.environ.get("AUTOROAD_SITE") or os.environ.get("BEAMNG_AUTOROAD_SITE")
    if env:
        p = Path(env.strip().strip('"'))
        return p if p.is_absolute() else ROOT / p
    return ROOT / "config" / "site.yaml"


def load_site(path: Path | str | None = None) -> dict:
    p = resolve_site_path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Site config missing: {p}. "
            "Copy config/sites/hahntennjoch.yaml → config/site.yaml "
            "or set AUTOROAD_SITE=config/sites/l13_kuehtai.yaml"
        )
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def site_slug(site: dict | None = None) -> str:
    site = site or load_site()
    return str(site.get("name", "default")).replace(" ", "_")


def processed_dir(site: dict | None = None) -> Path:
    d = ROOT / "data" / "processed" / site_slug(site)
    d.mkdir(parents=True, exist_ok=True)
    return d


def raw_dir(site: dict | None = None) -> Path:
    """Shared raw/ by default; optional per-site subfolder if sources.*.raw_subdir set."""
    site = site or load_site()
    sub = (site.get("sources") or {}).get("raw_subdir")
    d = ROOT / "data" / "raw" / str(sub) if sub else ROOT / "data" / "raw"
    d.mkdir(parents=True, exist_ok=True)
    return d


def annotations_gpkg(site: dict | None = None) -> Path:
    site = site or load_site()
    ann = site.get("annotations") or {}
    rel = ann.get("gpkg") or f"data/annotations/{site_slug(site)}.gpkg"
    p = Path(str(rel).replace(" ", "_"))
    return p if p.is_absolute() else ROOT / p


def wgs84_center(site: dict | None = None) -> tuple[float, float]:
    """BBox midpoint as (latitude, longitude) degrees."""
    from pyproj import Transformer

    site = site or load_site()
    crs = str(site.get("crs", "EPSG:31254"))
    xmin, ymin, xmax, ymax = map(float, site["bbox"])
    if site.get("authorities"):
        from authorities import apply_working_frame

        crs, bbox = apply_working_frame(site)
        xmin, ymin, xmax, ymax = bbox
    to_wgs = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lon, lat = to_wgs.transform(0.5 * (xmin + xmax), 0.5 * (ymin + ymax))
    return float(lat), float(lon)


class SiteCoords:
    """Map between site CRS (absolute meters) and BeamNG terrain XY."""

    def __init__(self, site: dict | None = None):
        site = site or load_site()
        self.site = site
        self.crs = str(site.get("crs", "EPSG:31254"))
        bbox = list(map(float, site["bbox"]))
        if site.get("authorities"):
            from authorities import apply_working_frame

            self.crs, bbox = apply_working_frame(site)
        self.xmin, self.ymin, self.xmax, self.ymax = bbox
        self.bw = self.xmax - self.xmin
        self.bh = self.ymax - self.ymin
        bng = site.get("beamng", {}) or {}
        mpp = float(bng.get("meters_per_pixel", 1.0))
        size = int(bng.get("mask_size", 512))
        self.terrain_extent = size * mpp

    def beamng_to_crs(self, bx: float, by: float) -> tuple[float, float]:
        lx = bx / self.terrain_extent * self.bw
        ly = by / self.terrain_extent * self.bh
        return self.xmin + lx, self.ymin + ly

    def crs_to_beamng(self, x: float, y: float) -> tuple[float, float]:
        lx = x - self.xmin
        ly = y - self.ymin
        return lx / self.bw * self.terrain_extent, ly / self.bh * self.terrain_extent
