"""Derive TWI (and optional nDSM) terrain indices for biome / vegetation work.

TWI (Beven–Kirkby) from DGM via D8 flow accumulation:
  TWI = ln( a / tan(β) )
  a   = specific catchment area ≈ (flow_acc × cellsize)
  β   = local outflow slope

nDSM (vegetation / object height proxy) when DOM is present:
  nDSM = max(0, DOM − DGM)

Writes under data/processed/<site>/:
  twi.tif, preview_twi.png, twi.meta.json
  ndsm.tif, preview_ndsm.png, ndsm.meta.json   (if DOM available)

Usage:
  $env:AUTOROAD_SITE='config/sites/fernpass_mega.yaml'
  python tools/build_twi.py
  python tools/build_twi.py --skip-ndsm
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import tifffile as tiff
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from fetch_dgm import dgm_cache_path  # noqa: E402
from fetch_dom import dom_cache_path  # noqa: E402
from site_coords import load_site, processed_dir, site_slug  # noqa: E402

# D8: (drow, dcol) — row0 = north/top of GeoTIFF as fetched
_D8 = (
    (-1, -1),
    (-1, 0),
    (-1, 1),
    (0, -1),
    (0, 1),
    (1, -1),
    (1, 0),
    (1, 1),
)


def _load_elev(path: Path) -> np.ndarray:
    arr = np.asarray(tiff.imread(path), dtype=np.float64)
    if arr.ndim != 2:
        raise SystemExit(f"Expected 2D elevation raster: {path} shape={arr.shape}")
    print(
        f"{path.name}: shape={arr.shape} dtype=float64 "
        f"min={float(np.nanmin(arr)):.3f} max={float(np.nanmax(arr)):.3f}"
    )
    return arr


def _cellsize_m(site: dict, shape: tuple[int, int]) -> float:
    bbox = list(map(float, site["bbox"]))
    xmin, ymin, xmax, ymax = bbox
    h, w = shape
    # Prefer declared DGM resolution when grid matches request
    src = (site.get("sources") or {}).get("dgm") or {}
    res = float(src.get("resolution_m") or 0.0)
    if res > 0:
        return res
    return float(((xmax - xmin) / max(w, 1) + (ymax - ymin) / max(h, 1)) * 0.5)


def _fill_nan(z: np.ndarray) -> np.ndarray:
    """Replace non-finite with local valid mean; leftover → global median."""
    out = z.copy()
    bad = ~np.isfinite(out)
    if not bad.any():
        return out
    # 3×3 mean of valid neighbors (one pass is enough for sparse holes)
    padded = np.pad(out, 1, mode="edge")
    acc = np.zeros_like(out)
    cnt = np.zeros_like(out, dtype=np.int32)
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            win = padded[1 + dr : 1 + dr + out.shape[0], 1 + dc : 1 + dc + out.shape[1]]
            ok = np.isfinite(win)
            acc = np.where(ok, acc + np.nan_to_num(win, nan=0.0), acc)
            cnt += ok.astype(np.int32)
    fill = np.where(cnt > 0, acc / np.maximum(cnt, 1), np.nan)
    out = np.where(bad & np.isfinite(fill), fill, out)
    still = ~np.isfinite(out)
    if still.any():
        med = float(np.nanmedian(z))
        out = np.where(still, med, out)
    return out


def d8_flow(z: np.ndarray, cellsize: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (receiver flat index or -1, outflow tan(β), flow accumulation)."""
    h, w = z.shape
    n = h * w
    # Steepest descent among 8 neighbors
    best_drop = np.full((h, w), -np.inf, dtype=np.float64)
    best_tan = np.zeros((h, w), dtype=np.float64)
    recv_r = np.full((h, w), -1, dtype=np.int32)
    recv_c = np.full((h, w), -1, dtype=np.int32)

    for dr, dc in _D8:
        dist = cellsize * float(np.hypot(dr, dc))
        # Neighbor elevation via roll (edge cells get wrap — mask below)
        zn = np.roll(np.roll(z, -dr, axis=0), -dc, axis=1)
        drop = (z - zn) / dist
        # Invalidate wrapped edges
        if dr < 0:
            drop[: -dr, :] = -np.inf
        elif dr > 0:
            drop[-dr:, :] = -np.inf
        if dc < 0:
            drop[:, : -dc] = -np.inf
        elif dc > 0:
            drop[:, -dc:] = -np.inf

        better = drop > best_drop
        best_drop = np.where(better, drop, best_drop)
        best_tan = np.where(better, np.maximum(drop, 0.0), best_tan)
        rr = np.arange(h, dtype=np.int32)[:, None] + dr
        cc = np.arange(w, dtype=np.int32)[None, :] + dc
        recv_r = np.where(better, rr, recv_r)
        recv_c = np.where(better, cc, recv_c)

    # No downhill neighbor → sink / flat
    has = best_drop > 0.0
    recv = np.where(has, recv_r * w + recv_c, -1).astype(np.int32).ravel()
    tan_beta = best_tan.ravel()

    # Flow accumulation: high → low
    order = np.argsort(z.ravel())[::-1]
    acc = np.ones(n, dtype=np.float64)
    for i in order:
        j = int(recv[i])
        if j >= 0:
            acc[j] += acc[i]

    return recv.reshape(h, w), tan_beta.reshape(h, w), acc.reshape(h, w)


def smooth_twi(twi: np.ndarray, cellsize: float, smooth_m: float) -> np.ndarray:
    """Gentle Gaussian blur so wet/dry patches are not 1 m speckles.

    ``smooth_m`` is the Gaussian σ in metres (≈ characteristic length).
    """
    if smooth_m <= 0:
        return twi
    from scipy.ndimage import gaussian_filter  # noqa: WPS433

    sigma_px = float(smooth_m) / max(float(cellsize), 1e-6)
    out = gaussian_filter(np.asarray(twi, dtype=np.float64), sigma=sigma_px, mode="nearest")
    print(
        f"TWI smoothed sigma={smooth_m:g} m ({sigma_px:.2f} px): "
        f"min={float(out.min()):.2f} max={float(out.max()):.2f} "
        f"p50={float(np.percentile(out, 50)):.2f}"
    )
    return out.astype(np.float32)


def compute_twi(
    z: np.ndarray,
    cellsize: float,
    *,
    min_slope: float = 1e-3,
    smooth_m: float = 0.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (twi, tan_beta, flow_acc)."""
    z = _fill_nan(np.asarray(z, dtype=np.float64))
    t0 = time.perf_counter()
    _recv, tan_beta, acc = d8_flow(z, cellsize)
    # Specific catchment area [m]: contrib. area / contour width ≈ acc * cellsize
    specific = acc * cellsize
    slope = np.maximum(tan_beta, min_slope)
    twi = np.log(specific / slope)
    print(
        f"TWI computed in {time.perf_counter() - t0:.1f}s "
        f"(min={float(twi.min()):.2f} max={float(twi.max()):.2f} "
        f"p50={float(np.percentile(twi, 50)):.2f})"
    )
    twi = smooth_twi(twi.astype(np.float32), cellsize, smooth_m)
    return twi.astype(np.float32), tan_beta.astype(np.float32), acc.astype(np.float32)


def compute_ndsm(dgm: np.ndarray, dom: np.ndarray) -> np.ndarray:
    if dom.shape != dgm.shape:
        # Resample DOM to DGM grid (nearest / bilinear via PIL)
        img = Image.fromarray(np.asarray(dom, dtype=np.float32), mode="F")
        img = img.resize((dgm.shape[1], dgm.shape[0]), resample=Image.Resampling.BILINEAR)
        dom = np.array(img, dtype=np.float64)
    dgm = _fill_nan(np.asarray(dgm, dtype=np.float64))
    dom = _fill_nan(np.asarray(dom, dtype=np.float64))
    ndsm = np.maximum(dom - dgm, 0.0)
    print(
        f"nDSM: min={float(ndsm.min()):.2f} max={float(ndsm.max()):.2f} "
        f"p95={float(np.percentile(ndsm, 95)):.2f} m"
    )
    return ndsm.astype(np.float32)


def _preview_png(arr: np.ndarray, path: Path, *, lo_p: float = 2.0, hi_p: float = 98.0) -> None:
    a = np.asarray(arr, dtype=np.float64)
    lo, hi = np.percentile(a, (lo_p, hi_p))
    if hi <= lo:
        hi = lo + 1.0
    u8 = np.clip((a - lo) / (hi - lo) * 255.0, 0, 255).astype(np.uint8)
    Image.fromarray(u8, mode="L").save(path)
    print(f"Wrote {path} (stretch p{lo_p:g}–p{hi_p:g})")


def _write_raster(path: Path, arr: np.ndarray) -> None:
    tiff.imwrite(path, np.asarray(arr), compression="deflate")
    print(f"Wrote {path} ({path.stat().st_size} bytes)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--min-slope",
        type=float,
        default=1e-3,
        help="Floor for tan(β) to avoid ln blow-up on flats (default 1e-3)",
    )
    ap.add_argument(
        "--skip-ndsm",
        action="store_true",
        help="Do not compute nDSM even if DOM cache exists",
    )
    ap.add_argument(
        "--require-dom",
        action="store_true",
        help="Fail if DOM/DSM cache is missing (otherwise skip nDSM)",
    )
    ap.add_argument(
        "--smooth-m",
        type=float,
        default=None,
        help="Gaussian σ in metres after TWI (overrides sources.twi.smooth_m)",
    )
    args = ap.parse_args()

    site = load_site()
    slug = site_slug(site)
    proc = processed_dir(site)
    print(f"Site: {slug}")

    twi_cfg = ((site.get("sources") or {}).get("twi") or {})
    smooth_m = float(
        args.smooth_m
        if args.smooth_m is not None
        else (twi_cfg.get("smooth_m") if twi_cfg.get("smooth_m") is not None else 10.0)
    )

    dgm_path = dgm_cache_path(site)
    if not dgm_path.is_file():
        raise SystemExit(f"Missing DGM {dgm_path} — run: python tools/fetch_dgm.py")

    dgm = _load_elev(dgm_path)
    cell = _cellsize_m(site, dgm.shape)
    print(f"cellsize_m={cell}  smooth_m={smooth_m}")

    twi, tan_beta, acc = compute_twi(
        dgm, cell, min_slope=float(args.min_slope), smooth_m=smooth_m
    )
    _write_raster(proc / "twi.tif", twi)
    _write_raster(proc / "twi_flow_acc.tif", acc)
    _write_raster(proc / "twi_tan_slope.tif", tan_beta)
    _preview_png(twi, proc / "preview_twi.png")
    twi_meta = {
        "site": slug,
        "source": str(dgm_path.relative_to(ROOT)),
        "formula": "ln( (flow_acc * cellsize) / max(tan_beta, min_slope) )",
        "flow": "D8",
        "cellsize_m": cell,
        "min_slope": float(args.min_slope),
        "smooth_m": smooth_m,
        "smooth": f"gaussian_sigma_m={smooth_m}",
        "shape": list(dgm.shape),
        "twi_min": float(twi.min()),
        "twi_max": float(twi.max()),
        "twi_p50": float(np.percentile(twi, 50)),
        "files": ["twi.tif", "twi_flow_acc.tif", "twi_tan_slope.tif", "preview_twi.png"],
    }
    (proc / "twi.meta.json").write_text(json.dumps(twi_meta, indent=2), encoding="utf-8")
    print(f"Wrote {proc / 'twi.meta.json'}")

    if args.skip_ndsm:
        return

    dom_path = dom_cache_path(site)
    if not dom_path.is_file():
        msg = f"DOM/DSM not cached ({dom_path}) — skip nDSM. Fetch: python tools/fetch_dom.py"
        if args.require_dom:
            raise SystemExit(msg)
        print(msg)
        return

    dom = _load_elev(dom_path)
    ndsm = compute_ndsm(dgm, dom)
    _write_raster(proc / "ndsm.tif", ndsm)
    _preview_png(ndsm, proc / "preview_ndsm.png", lo_p=0.0, hi_p=99.0)
    ndsm_meta = {
        "site": slug,
        "dgm": str(dgm_path.relative_to(ROOT)),
        "dom": str(dom_path.relative_to(ROOT)),
        "formula": "max(0, DOM - DGM)",
        "shape": list(ndsm.shape),
        "ndsm_min": float(ndsm.min()),
        "ndsm_max": float(ndsm.max()),
        "ndsm_p95": float(np.percentile(ndsm, 95)),
        "files": ["ndsm.tif", "preview_ndsm.png"],
    }
    (proc / "ndsm.meta.json").write_text(json.dumps(ndsm_meta, indent=2), encoding="utf-8")
    print(f"Wrote {proc / 'ndsm.meta.json'}")


if __name__ == "__main__":
    main()
