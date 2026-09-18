"""Parametric snow cover from DGM (height, aspect, slope, base temperature).

No satellite needed. Produces a 0–1 snow-cover fraction for biomes:

  T(z)     = t0_c - lapse_c_per_m * (z - z0_m)
  s_temp   = smoothstep(full_snow_temp_c → snow_temp_c, T)   # 1 = cold
  aspect   = 1 + aspect_strength * cos(aspect)                # N > S
  steep    = lerp(1 → steep_factor, slope 0→steep_deg)
  snow     = clip(s_temp * aspect * steep, 0, 1)

Writes under data/processed/<site>/:
  snow_proxy.tif, snow_fsc.tif (alias), preview_snow_proxy.png,
  snow_proxy.meta.json

Usage:
  cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = \"config/sites/fernpass_mega.yaml\"; python tools\\build_snow_proxy.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import tifffile as tiff
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from fetch_dgm import dgm_cache_path  # noqa: E402
from site_coords import load_site, processed_dir, site_slug  # noqa: E402

# Named presets (overridden by explicit keys under sources.snow)
_PRESETS: dict[str, dict] = {
    # Late-autumn / early winter: snow from mid elevations up
    "november": {
        "t0_c": 1.5,
        "z0_m": 1200.0,
        "lapse_c_per_m": 0.0065,
        "snow_temp_c": 1.0,
        "full_snow_temp_c": -4.0,
        "aspect_strength": 0.40,
        "steep_deg": 48.0,
        "steep_factor": 0.25,
    },
    # Early summer leftovers on high peaks / north faces
    "june": {
        "t0_c": 14.0,
        "z0_m": 1200.0,
        "lapse_c_per_m": 0.0065,
        "snow_temp_c": -2.0,
        "full_snow_temp_c": -8.0,
        "aspect_strength": 0.45,
        "steep_deg": 50.0,
        "steep_factor": 0.2,
    },
    # Early autumn: first dusting on high ridges, pass roads stay clear.
    # Onset T=0 ≈ 1740 m; full at −1 °C ≈ 1890 m — this crop's crest is ~1870 m.
    "september": {
        "t0_c": 3.5,
        "z0_m": 1200.0,
        "lapse_c_per_m": 0.0065,
        "snow_temp_c": 0.0,
        "full_snow_temp_c": -1.0,
        "aspect_strength": 0.50,
        "steep_deg": 50.0,
        "steep_factor": 0.25,
    },
    "winter": {
        "t0_c": -2.0,
        "z0_m": 1200.0,
        "lapse_c_per_m": 0.0065,
        "snow_temp_c": 1.0,
        "full_snow_temp_c": -4.0,
        "aspect_strength": 0.25,
        "steep_deg": 50.0,
        "steep_factor": 0.3,
    },
}


def _cfg(site: dict) -> dict:
    raw = ((site.get("sources") or {}).get("snow") or {})
    preset_name = str(raw.get("preset") or "november").lower()
    base = dict(_PRESETS.get(preset_name) or _PRESETS["november"])
    base["preset"] = preset_name if preset_name in _PRESETS else "november"
    for k in (
        "t0_c",
        "z0_m",
        "lapse_c_per_m",
        "snow_temp_c",
        "full_snow_temp_c",
        "aspect_strength",
        "steep_deg",
        "steep_factor",
    ):
        if raw.get(k) is not None:
            base[k] = float(raw[k])
    return base


def _cellsize_m(site: dict, shape: tuple[int, int]) -> float:
    src = (site.get("sources") or {}).get("dgm") or {}
    res = float(src.get("resolution_m") or 0.0)
    if res > 0:
        return res
    bbox = list(map(float, site["bbox"]))
    xmin, ymin, xmax, ymax = bbox
    h, w = shape
    return float(((xmax - xmin) / max(w, 1) + (ymax - ymin) / max(h, 1)) * 0.5)


def _load_elev(site: dict) -> np.ndarray:
    path = dgm_cache_path(site)
    if not path.is_file():
        raise SystemExit(f"Missing DGM {path} — run: python tools/fetch_dgm.py")
    arr = np.asarray(tiff.imread(path), dtype=np.float64)
    if arr.ndim != 2:
        raise SystemExit(f"Expected 2D DGM: {path} shape={arr.shape}")
    size = int((site.get("beamng") or {}).get("mask_size") or 0)
    if size > 0 and arr.shape != (size, size):
        img = Image.fromarray(arr.astype(np.float32), mode="F")
        img = img.resize((size, size), resample=Image.Resampling.BILINEAR)
        arr = np.asarray(img, dtype=np.float64)
        print(f"DGM resampled → {size}x{size}")
    print(
        f"DGM {path.name}: shape={arr.shape} "
        f"min={float(np.nanmin(arr)):.1f} max={float(np.nanmax(arr)):.1f}"
    )
    return arr


def _slope_aspect_deg(z: np.ndarray, cellsize: float) -> tuple[np.ndarray, np.ndarray]:
    """Return (slope_deg, aspect_deg).

    Aspect: 0° = North, 90° = East, 180° = South, 270° = West.
    Row 0 = north (GeoTIFF top).
    """
    # np.gradient: axis0 = row (south+), axis1 = col (east+)
    dz_drow, dz_dcol = np.gradient(z, cellsize, cellsize)
    dz_dx = dz_dcol  # east
    dz_dy = -dz_drow  # north (row increases south)
    slope_rad = np.arctan(np.hypot(dz_dx, dz_dy))
    slope_deg = np.degrees(slope_rad)
    # atan2(east_component, north_component) → 0 when facing north downhill? 
    # Aspect of the *surface* (direction the slope faces): downhill direction.
    # Downhill vector ≈ (-dz_dx, -dz_dy) in (east, north); aspect from north clockwise.
    aspect_rad = np.arctan2(-dz_dx, -dz_dy)
    aspect_deg = np.degrees(aspect_rad)
    aspect_deg = np.where(aspect_deg < 0, aspect_deg + 360.0, aspect_deg)
    # Flat: aspect undefined → set to 180 (neutral-ish) handled by strength*cos
    flat = slope_deg < 0.5
    aspect_deg = np.where(flat, 0.0, aspect_deg)  # treat flat as N-neutral via low slope
    return slope_deg.astype(np.float32), aspect_deg.astype(np.float32)


def _smoothstep(edge0: float, edge1: float, x: np.ndarray) -> np.ndarray:
    if abs(edge1 - edge0) < 1e-9:
        return (x <= edge0).astype(np.float64)
    t = np.clip((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def compute_snow_proxy(z: np.ndarray, cellsize: float, cfg: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (snow 0–1, slope_deg, aspect_deg)."""
    slope, aspect = _slope_aspect_deg(z, cellsize)
    t0 = float(cfg["t0_c"])
    z0 = float(cfg["z0_m"])
    lapse = float(cfg["lapse_c_per_m"])
    t_snow = float(cfg["snow_temp_c"])
    t_full = float(cfg["full_snow_temp_c"])
    asp_s = float(cfg["aspect_strength"])
    steep_deg = max(1e-3, float(cfg["steep_deg"]))
    steep_f = float(cfg["steep_factor"])

    temp = t0 - lapse * (z - z0)
    # colder → more snow: 1 at t_full and below, 0 at t_snow and above
    # smoothstep from cold→warm mapped then inverted
    s_temp = 1.0 - _smoothstep(t_full, t_snow, temp)

    # North-facing (aspect≈0°): cos≈+1 → boost; South (180°): cos≈-1 → reduce
    aspect_factor = 1.0 + asp_s * np.cos(np.radians(aspect.astype(np.float64)))
    aspect_factor = np.clip(aspect_factor, 0.05, 2.0)
    # Flats: no aspect bias
    aspect_factor = np.where(slope < 0.5, 1.0, aspect_factor)

    steep_t = np.clip(slope.astype(np.float64) / steep_deg, 0.0, 1.0)
    steep_factor = 1.0 + (steep_f - 1.0) * steep_t

    snow = np.clip(s_temp * aspect_factor * steep_factor, 0.0, 1.0).astype(np.float32)
    return snow, slope, aspect


def _preview(arr: np.ndarray, path: Path) -> None:
    u8 = np.clip(np.asarray(arr, dtype=np.float64) * 255.0, 0, 255).astype(np.uint8)
    Image.fromarray(u8, mode="L").save(path)
    print(f"Wrote {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--preset",
        type=str,
        default="",
        help="Override sources.snow.preset (november|june|winter)",
    )
    args = ap.parse_args()

    site = load_site()
    slug = site_slug(site)
    proc = processed_dir(site)
    if args.preset:
        site.setdefault("sources", {}).setdefault("snow", {})["preset"] = args.preset
    cfg = _cfg(site)
    print(f"Site: {slug}  snow preset={cfg['preset']}")

    z = _load_elev(site)
    cell = _cellsize_m(site, z.shape)
    snow, slope, aspect = compute_snow_proxy(z, cell, cfg)

    tiff.imwrite(proc / "snow_proxy.tif", snow, compression="deflate")
    tiff.imwrite(proc / "snow_fsc.tif", snow, compression="deflate")  # biome alias
    tiff.imwrite(proc / "snow_proxy_slope.tif", slope, compression="deflate")
    tiff.imwrite(proc / "snow_proxy_aspect.tif", aspect, compression="deflate")
    _preview(snow, proc / "preview_snow_proxy.png")
    # also refresh FSC preview name used earlier
    _preview(snow, proc / "preview_snow_fsc.png")

    meta = {
        "site": slug,
        "source": "dgm_proxy",
        "preset": cfg["preset"],
        "params": {k: cfg[k] for k in cfg if k != "preset"},
        "cellsize_m": cell,
        "shape": list(snow.shape),
        "snow_mean": float(snow.mean()),
        "snow_p50": float(np.percentile(snow, 50)),
        "snow_p95": float(np.percentile(snow, 95)),
        "snow_frac_gt_0_1": float((snow > 0.1).mean()),
        "snow_frac_gt_0_5": float((snow > 0.5).mean()),
        "files": [
            "snow_proxy.tif",
            "snow_fsc.tif",
            "preview_snow_proxy.png",
            "preview_snow_fsc.png",
            "snow_proxy_slope.tif",
            "snow_proxy_aspect.tif",
        ],
    }
    (proc / "snow_proxy.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (proc / "snow_fsc.meta.json").write_text(
        json.dumps({**meta, "note": "Alias of snow_proxy (DGM parametric)"}, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {proc / 'snow_proxy.meta.json'}")
    print(
        f"snow mean={meta['snow_mean']:.3f}  "
        f">10%={meta['snow_frac_gt_0_1']*100:.1f}%  "
        f">50%={meta['snow_frac_gt_0_5']*100:.1f}%  "
        f"p95={meta['snow_p95']:.3f}"
    )


if __name__ == "__main__":
    main()
