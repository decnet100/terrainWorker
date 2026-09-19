"""PNG-only: original near ortho vs snow(red) + green(blue) at 3 open thresholds.

No mesh, no inject. Writes under data/processed/<site>/.

Usage:
  cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = \"config/sites/reschen.yaml\"; python tools\\preview_backdrop_detect.py --snow-cut 0.30 --snow-mix 0.65 --mix 0.4 --dark 0.4
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from build_backdrop import _zoom_hw  # noqa: E402
from build_snow_proxy import _cfg as snow_proxy_cfg  # noqa: E402
from build_snow_proxy import compute_snow_proxy  # noqa: E402
from fetch_backdrop import backdrop_cfg, load_near_dgm, near_extent, near_ortho_path  # noqa: E402
from fetch_bev_landcover import load_bev_forest_mask  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

# Opaque class colors. Never blend with the photo — dark grass must stay knallblau.
_RED = np.array([255, 16, 0], dtype=np.uint8)
_BLUE = np.array([0, 80, 255], dtype=np.uint8)
_SNOW_RGB = np.array([245.0, 248.0, 255.0], dtype=np.float32)


def _green_w(rgb: np.ndarray) -> np.ndarray:
    """Same gate as debug_green_black: G-share above 1/3."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    share = g / (r + g + b + 1e-5)
    return np.clip((share - (1.0 / 3.0)) / 0.03, 0.0, 1.0)


def _paint(ortho: np.ndarray, is_grass: np.ndarray, is_snow: np.ndarray) -> np.ndarray:
    """Three disjoint classes: snow (red) > grass (blue) > unchanged (raw ortho)."""
    vis = np.array(ortho, copy=True)
    vis[is_grass] = _BLUE
    vis[is_snow] = _RED
    return vis


def _caption(im: Image.Image, text: str) -> Image.Image:
    pad = Image.new("RGB", (im.width, im.height + 32), (18, 18, 18))
    pad.paste(im, (0, 32))
    ImageDraw.Draw(pad).text((10, 8), text, fill=(235, 235, 235))
    return pad


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="PNG-only snow/grass detect + olive preview")
    ap.add_argument(
        "--snow",
        default="0.20,0.25,0.30",
        help="Comma snow-proxy floors (higher = less red). Default 0.20,0.25,0.30",
    )
    ap.add_argument(
        "--grass",
        type=float,
        default=0.5,
        help="Green-share weight cutoff for knallblau / olive (0-1). Default 0.5",
    )
    ap.add_argument("--mix", type=float, default=0.55, help="Olive mix on grass only (0-1). Default 0.55")
    ap.add_argument(
        "--dark",
        type=float,
        default=0.28,
        help="How much to darken grass (0=unchanged brightness, 1=black). Default 0.28",
    )
    ap.add_argument(
        "--snow-cut",
        type=float,
        default=0.40,
        help="Snow-proxy floor for the grade PNG (same as detect red). Default 0.40",
    )
    ap.add_argument(
        "--snow-mix",
        type=float,
        default=0.65,
        help="How hard snow pixels go toward white (0=keep photo, 1=flat white). Default 0.65",
    )
    ap.add_argument(
        "--olive-only",
        action="store_true",
        help="Skip detect sheets; rewrite grass olive PNG only (faster).",
    )
    ap.add_argument(
        "--forest",
        default="",
        help="Forest class: bev | green. Default: site forest_detect (Reschen=bev).",
    )
    ap.add_argument("--forest-mix", type=float, default=None, help="Olive mix on BEV forest only (0-1).")
    ap.add_argument("--forest-dark", type=float, default=None, help="Darken BEV forest (0=keep, 1=black).")
    ap.add_argument(
        "--forest-rgb",
        default="",
        help="BEV forest target RGB 0-255, e.g. 72,88,52. Default: site forest_olive.",
    )
    return ap.parse_args()


def main() -> None:
    args = _parse_args()
    snow_thresh = tuple(float(x.strip()) for x in str(args.snow).split(",") if x.strip())
    grass_cut = float(args.grass)
    mix = float(args.mix)
    dark = float(np.clip(args.dark, 0.0, 1.0))
    site = load_site()
    proc = processed_dir(site)
    forest_src = str(args.forest or backdrop_cfg(site).get("forest_detect") or "green").strip().lower()
    op = near_ortho_path(site)
    if not op.is_file():
        raise SystemExit(f"missing {op}")
    ortho = np.asarray(Image.open(op).convert("RGB"))
    rgb = ortho.astype(np.float32) / 255.0
    green_w = _green_w(rgb)
    is_meadow = green_w >= grass_cut
    if forest_src == "bev":
        is_forest = load_bev_forest_mask(site, ortho.shape[:2], near_extent(site))
    else:
        is_forest = np.zeros(is_meadow.shape, dtype=bool)
    is_olive = is_meadow | is_forest
    snow_img = None
    if not args.olive_only:
        pack = load_near_dgm(site)
        if pack is None:
            raise SystemExit("near DGM missing")
        elev, meta = pack
        cell_m = abs(float(meta["px"]))
        snow_cfg = snow_proxy_cfg(site)
        finite = np.isfinite(elev)
        fill = float(np.nanmedian(elev)) if finite.any() else 0.0
        z = np.where(finite, elev, fill).astype(np.float64)
        snow, _, _ = compute_snow_proxy(z, cell_m, snow_cfg)
        snow = np.where(finite, snow, 0.0).astype(np.float32)
        snow_img = _zoom_hw(snow, ortho.shape[0])
        if snow_img.shape[:2] != ortho.shape[:2]:
            snow_img = np.array(
                Image.fromarray((np.clip(snow_img, 0, 1) * 255).astype(np.uint8)).resize(
                    (ortho.shape[1], ortho.shape[0]), Image.Resampling.BILINEAR
                ),
                dtype=np.float32,
            ) / 255.0
        print(
            f"ortho {ortho.shape}  meadow={100.0 * float(is_meadow.mean()):.1f}%  "
            f"forest={forest_src} {100.0 * float(is_forest.mean()):.1f}%  "
            f"olive={100.0 * float(is_olive.mean()):.1f}%  "
            f"snow p50={float(np.nanmedian(snow_img)):.2f} p90={float(np.nanpercentile(snow_img, 90)):.2f}"
        )
    else:
        print(
            f"ortho {ortho.shape}  meadow={100.0 * float(is_meadow.mean()):.1f}%  "
            f"forest={forest_src} {100.0 * float(is_forest.mean()):.1f}%  "
            f"olive={100.0 * float(is_olive.mean()):.1f}%  olive-only"
        )

    tiles = [_caption(Image.fromarray(ortho, mode="RGB"), "Ursprung Swissimage near")]
    written: list[Path] = []
    orig_p = proc / "preview_detect_near_orig.png"
    Image.fromarray(ortho, mode="RGB").save(orig_p)
    written.append(orig_p)
    if args.olive_only:
        snow_thresh = ()
    for thr in snow_thresh:
        assert snow_img is not None
        is_snow = snow_img >= thr
        painted = _paint(ortho, is_olive, is_snow)
        name = f"preview_detect_near_s{int(round(thr * 100)):02d}.png"
        path = proc / name
        Image.fromarray(painted, mode="RGB").save(path)
        written.append(path)
        pct = 100.0 * float((snow_img >= thr).mean())
        tiles.append(
            _caption(
                Image.fromarray(painted, mode="RGB"),
                f"Schnee rot >= {thr:.2f} ({pct:.1f}%)  |  Gruen blau",
            )
        )
        print(f"  thresh {thr:.2f}  snow cells {pct:.1f}%  -> {path.name}")

    if snow_thresh:
        tw = 640
        thumbs = []
        for t in tiles:
            im = t.copy()
            im.thumbnail((tw, tw + 40))
            thumbs.append(im)
        sheet = Image.new("RGB", (tw * len(thumbs), thumbs[0].height), (18, 18, 18))
        for i, th in enumerate(thumbs):
            sheet.paste(th, (i * tw, 0))
        sheet_p = proc / "preview_detect_near_sheet.png"
        sheet.save(sheet_p)
        written.append(sheet_p)

    # Meadow olive, then BEV forest (own mix/dark/rgb), snow later.
    cfg = backdrop_cfg(site)
    olive = np.array([120.0, 108.0, 64.0], dtype=np.float32)
    rgb8 = ortho.astype(np.float32)
    meadow_g = (1.0 - mix) * rgb8 + mix * olive
    meadow_g *= 1.0 - dark
    grass = np.clip(np.where(is_meadow[..., None], meadow_g, rgb8), 0, 255).astype(np.uint8)
    fmix = float(args.forest_mix if args.forest_mix is not None else cfg.get("forest_mix", mix))
    fdark = float(np.clip(args.forest_dark if args.forest_dark is not None else cfg.get("forest_dark", dark), 0.0, 1.0))
    if str(args.forest_rgb).strip():
        folive = np.array([float(x) for x in str(args.forest_rgb).split(",")[:3]], dtype=np.float32)
    else:
        folive = np.array(list(cfg.get("forest_olive") or [72, 88, 52])[:3], dtype=np.float32)
    forest_g = (1.0 - fmix) * rgb8 + fmix * folive
    forest_g *= 1.0 - fdark
    grass = np.clip(np.where(is_forest[..., None], forest_g, grass), 0, 255).astype(np.uint8)
    grass_p = proc / "preview_grass_olive_near.png"
    Image.fromarray(grass, mode="RGB").save(grass_p)
    written.append(grass_p)

    snow_cut = float(args.snow_cut)
    snow_mix = float(np.clip(args.snow_mix, 0.0, 1.0))
    grade = grass.copy()
    if snow_img is not None:
        is_snow = snow_img >= snow_cut
        sm = snow_mix * is_snow.astype(np.float32)
        grade = np.clip(
            (1.0 - sm[..., None]) * grade.astype(np.float32) + sm[..., None] * _SNOW_RGB,
            0,
            255,
        ).astype(np.uint8)
        print(
            f"grade snow>={snow_cut:.2f}={100.0 * float(is_snow.mean()):.1f}%  snow-mix={snow_mix:.2f}"
        )
    grade_p = proc / "preview_grade_near.png"
    Image.fromarray(grade, mode="RGB").save(grade_p)
    tw2 = 900
    pair = []
    for im, cap in (
        (Image.fromarray(ortho, mode="RGB"), "Ursprung Swissimage near"),
        (
            Image.fromarray(grade, mode="RGB"),
            f"Wiese {mix:.2f}/{dark:.2f}  Wald {int(folive[0])},{int(folive[1])},{int(folive[2])} {fmix:.2f}/{fdark:.2f}",
        ),
    ):
        im = _caption(im, cap)
        im.thumbnail((tw2, tw2 + 40))
        pair.append(im)
    gsheet = Image.new("RGB", (tw2 * 2, pair[0].height), (18, 18, 18))
    gsheet.paste(pair[0], (0, 0))
    gsheet.paste(pair[1], (tw2, 0))
    gsheet_p = proc / "preview_grade_near_sheet.png"
    gsheet.save(gsheet_p)
    written.extend([grade_p, gsheet_p])
    print(
        f"olive meadow={100.0 * float(is_meadow.mean()):.1f}% mix={mix:.2f}/{dark:.2f}  "
        f"forest={100.0 * float(is_forest.mean()):.1f}% rgb={int(folive[0])},{int(folive[1])},{int(folive[2])} "
        f"{fmix:.2f}/{fdark:.2f}  "
        f"-> {grass_p.name}; grade -> {grade_p.name}"
    )
    print("Wrote " + ", ".join(p.name for p in written))


if __name__ == "__main__":
    main()
