"""Cluster orthophoto colors inside the rock mask.

Goal: derive a small set of representative rock colors (RGB incl. brightness)
from an orthophoto, restricted to rock pixels (e.g. build_terrain_masks.py
output mask_rock.png).

The script starts at k=3 clusters and increases k until intra-cluster color
spread is below a threshold (percentile) or k_max is reached.

Outputs:
  - JSON report with cluster centers + spread metrics
  - quantized preview PNG
  - per-cluster masks (PNG)
  - palette strip PNG
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from PIL import ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))


def _load_rgb(path: Path) -> np.ndarray:
    img = Image.open(path).convert("RGB")
    return np.asarray(img, dtype=np.uint8)


def _load_mask(path: Path, shape_hw: tuple[int, int]) -> np.ndarray:
    """Load a mask PNG; resize to target shape if needed. Returns bool[H,W]."""
    img = Image.open(path).convert("L")
    h, w = shape_hw
    if img.size != (w, h):
        img = img.resize((w, h), resample=Image.Resampling.NEAREST)
    m = np.asarray(img, dtype=np.uint8)
    return m > 127


def _srgb_to_linear(u: np.ndarray) -> np.ndarray:
    """u: float in [0,1]. Returns linear RGB."""
    a = 0.055
    return np.where(u <= 0.04045, u / 12.92, ((u + a) / (1 + a)) ** 2.4)


def _linear_to_xyz(rgb_lin: np.ndarray) -> np.ndarray:
    """RGB linear (sRGB primaries) -> XYZ (D65)."""
    m = np.array(
        [
            [0.4124564, 0.3575761, 0.1804375],
            [0.2126729, 0.7151522, 0.0721750],
            [0.0193339, 0.1191920, 0.9503041],
        ],
        dtype=np.float64,
    )
    return rgb_lin @ m.T


def _xyz_to_lab(xyz: np.ndarray) -> np.ndarray:
    """XYZ -> CIE Lab (D65/2°)."""
    # D65 reference white
    white = np.array([0.95047, 1.00000, 1.08883], dtype=np.float64)
    x = xyz / white
    eps = 216.0 / 24389.0  # (6/29)^3
    k = 24389.0 / 27.0
    f = np.where(x > eps, np.cbrt(x), (k * x + 16.0) / 116.0)
    fx, fy, fz = f[:, 0], f[:, 1], f[:, 2]
    L = 116.0 * fy - 16.0
    a = 500.0 * (fx - fy)
    b = 200.0 * (fy - fz)
    return np.stack([L, a, b], axis=1).astype(np.float64, copy=False)


def _rgb_u8_to_lab(rgb_u8: np.ndarray) -> np.ndarray:
    """rgb_u8: Nx3 uint8 -> Lab Nx3 float64."""
    rgb = rgb_u8.astype(np.float64) / 255.0
    lin = _srgb_to_linear(rgb)
    xyz = _linear_to_xyz(lin)
    return _xyz_to_lab(xyz)


def _kmeans_pp_init(x: np.ndarray, k: int, rng: np.random.Generator) -> np.ndarray:
    """kmeans++ init; returns centers array [k,d]."""
    n = x.shape[0]
    d = x.shape[1]
    centers = np.empty((k, d), dtype=np.float64)
    i0 = int(rng.integers(0, n))
    centers[0] = x[i0]
    dist2 = np.sum((x - centers[0]) ** 2, axis=1)
    for ci in range(1, k):
        denom = float(dist2.sum())
        if not np.isfinite(denom) or denom <= 0:
            centers[ci] = x[int(rng.integers(0, n))]
            dist2 = np.minimum(dist2, np.sum((x - centers[ci]) ** 2, axis=1))
            continue
        probs = dist2 / denom
        pick = int(rng.choice(n, p=probs))
        centers[ci] = x[pick]
        dist2 = np.minimum(dist2, np.sum((x - centers[ci]) ** 2, axis=1))
    return centers


def _kmeans(
    x: np.ndarray,
    k: int,
    *,
    seed: int,
    max_iter: int = 40,
    tol: float = 1e-4,
) -> tuple[np.ndarray, np.ndarray]:
    """Simple k-means on x (NxD float). Returns (labels Nx1 int, centers KxD)."""
    if x.shape[0] < k:
        raise ValueError(f"Need at least k samples (n={x.shape[0]} k={k})")
    rng = np.random.default_rng(int(seed))
    centers = _kmeans_pp_init(x, k, rng)

    labels = np.zeros((x.shape[0],), dtype=np.int32)
    for _it in range(int(max_iter)):
        # Assign
        # (N,K) squared distances: ||x||^2 + ||c||^2 - 2 x·c
        x2 = np.sum(x * x, axis=1, keepdims=True)
        c2 = np.sum(centers * centers, axis=1, keepdims=True).T
        dist2 = x2 + c2 - 2.0 * (x @ centers.T)
        new_labels = np.argmin(dist2, axis=1).astype(np.int32)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels

        # Update
        new_centers = np.empty_like(centers)
        for ci in range(k):
            mask = labels == ci
            if not np.any(mask):
                # Empty cluster: re-seed to a random sample
                new_centers[ci] = x[int(rng.integers(0, x.shape[0]))]
            else:
                new_centers[ci] = x[mask].mean(axis=0)
        shift = float(np.sqrt(np.sum((new_centers - centers) ** 2)))
        centers = new_centers
        if shift <= float(tol):
            break
    return labels, centers


def _cluster_metrics(x: np.ndarray, labels: np.ndarray, centers: np.ndarray) -> dict:
    """Compute per-cluster spread in Lab space (ΔE76)."""
    k = int(centers.shape[0])
    out = {"clusters": [], "global": {}}
    d = x - centers[labels]
    de = np.sqrt(np.sum(d * d, axis=1))
    out["global"] = {
        "n": int(x.shape[0]),
        "de_mean": float(np.mean(de)) if de.size else 0.0,
        "de_p95": float(np.percentile(de, 95)) if de.size else 0.0,
        "de_max": float(np.max(de)) if de.size else 0.0,
    }
    for ci in range(k):
        idx = labels == ci
        if not np.any(idx):
            out["clusters"].append(
                {
                    "id": int(ci),
                    "n": 0,
                    "center_lab": [float(v) for v in centers[ci].tolist()],
                    "de_mean": 0.0,
                    "de_p95": 0.0,
                    "de_max": 0.0,
                }
            )
            continue
        dec = de[idx]
        out["clusters"].append(
            {
                "id": int(ci),
                "n": int(idx.sum()),
                "center_lab": [float(v) for v in centers[ci].tolist()],
                "de_mean": float(np.mean(dec)),
                "de_p95": float(np.percentile(dec, 95)),
                "de_max": float(np.max(dec)),
            }
        )
    # stable sort: most pixels first
    out["clusters"] = sorted(out["clusters"], key=lambda c: (-int(c["n"]), int(c["id"])))
    return out


def _centers_lab_to_rgb_u8(centers_lab: np.ndarray, ref_rgb_u8: np.ndarray, labels: np.ndarray) -> np.ndarray:
    """Return representative center RGB values as means in RGB (not Lab-inverted).

    We intentionally report RGB centers as the mean of original sRGB pixels in each cluster,
    to avoid a Lab->RGB conversion (out-of-gamut issues). Clustering still happens in Lab.
    """
    k = int(centers_lab.shape[0])
    out = np.zeros((k, 3), dtype=np.float64)
    counts = np.zeros((k,), dtype=np.int64)
    for ci in range(k):
        idx = labels == ci
        if not np.any(idx):
            continue
        out[ci] = ref_rgb_u8[idx].mean(axis=0)
        counts[ci] = int(idx.sum())
    # For empty clusters, pick nearest populated cluster RGB.
    for ci in range(k):
        if counts[ci] > 0:
            continue
        d2 = np.sum((centers_lab - centers_lab[ci]) ** 2, axis=1)
        j = int(np.argmin(d2))
        out[ci] = out[j]
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)

def _rgb_stats(rgb_u8: np.ndarray) -> dict:
    if rgb_u8.size == 0:
        return {
            "n": 0,
            "mean": [0.0, 0.0, 0.0],
            "median": [0.0, 0.0, 0.0],
            "p10": [0.0, 0.0, 0.0],
            "p90": [0.0, 0.0, 0.0],
        }
    x = rgb_u8.astype(np.float64)
    return {
        "n": int(rgb_u8.shape[0]),
        "mean": [float(v) for v in np.mean(x, axis=0).tolist()],
        "median": [float(v) for v in np.median(x, axis=0).tolist()],
        "p10": [float(v) for v in np.percentile(x, 10, axis=0).tolist()],
        "p90": [float(v) for v in np.percentile(x, 90, axis=0).tolist()],
    }


def _lab_stats(lab: np.ndarray) -> dict:
    if lab.size == 0:
        return {
            "n": 0,
            "mean": [0.0, 0.0, 0.0],
            "median": [0.0, 0.0, 0.0],
            "p10": [0.0, 0.0, 0.0],
            "p90": [0.0, 0.0, 0.0],
        }
    x = lab.astype(np.float64)
    return {
        "n": int(lab.shape[0]),
        "mean": [float(v) for v in np.mean(x, axis=0).tolist()],
        "median": [float(v) for v in np.median(x, axis=0).tolist()],
        "p10": [float(v) for v in np.percentile(x, 10, axis=0).tolist()],
        "p90": [float(v) for v in np.percentile(x, 90, axis=0).tolist()],
    }


def _write_palette(path: Path, colors: np.ndarray, *, swatch_px: int = 64) -> None:
    k = int(colors.shape[0])
    img = np.zeros((swatch_px, swatch_px * k, 3), dtype=np.uint8)
    for i in range(k):
        img[:, i * swatch_px : (i + 1) * swatch_px, :] = colors[i][None, None, :]
    Image.fromarray(img, mode="RGB").save(path)

def _write_palette_labeled(
    path: Path,
    colors: np.ndarray,
    *,
    labels: list[str],
    swatch_px: int = 64,
    label_h: int = 26,
) -> None:
    k = int(colors.shape[0])
    if len(labels) != k:
        raise ValueError("labels length must match colors")
    w = swatch_px * k
    h = swatch_px + label_h
    img = Image.new("RGB", (w, h), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    # swatches
    for i in range(k):
        x0 = i * swatch_px
        draw.rectangle((x0, 0, x0 + swatch_px - 1, swatch_px - 1), fill=tuple(int(v) for v in colors[i]))
    # labels
    for i, text in enumerate(labels):
        x0 = i * swatch_px + 2
        y0 = swatch_px + 2
        # tiny shadow for readability
        draw.text((x0 + 1, y0 + 1), text, font=font, fill=(0, 0, 0))
        draw.text((x0, y0), text, font=font, fill=(255, 255, 255))
    img.save(path)


def _auto_inputs_from_site(site: dict) -> tuple[Path | None, Path | None, Path]:
    """Try to locate default ortho/mask under processed/<site>/."""
    from site_coords import processed_dir  # local import

    proc = processed_dir(site)
    mask = proc / "mask_rock.png"
    ortho_candidates = [
        proc / "ortho.png",
        proc / "ortho_rgb.png",
        proc / "orthophoto.png",
        proc / "backdrop_near_ortho.png",
        proc / "backdrop_ortho.png",
    ]
    ortho = next((p for p in ortho_candidates if p.is_file() and p.stat().st_size > 1000), None)
    m = mask if mask.is_file() and mask.stat().st_size > 100 else None
    out_dir = proc / "rock_color_clusters"
    return ortho, m, out_dir


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ortho", default="", help="Orthophoto RGB image (PNG/JPG).")
    ap.add_argument("--mask", default="", help="Rock mask (PNG, white=rock).")
    ap.add_argument("--out", default="", help="Output directory (default: processed/<site>/rock_color_clusters).")
    ap.add_argument("--k-start", type=int, default=3, help="Initial cluster count.")
    ap.add_argument("--k-max", type=int, default=12, help="Maximum cluster count.")
    ap.add_argument(
        "--threshold",
        type=float,
        default=10.0,
        help="Stop when each cluster ΔE76 p95 is <= threshold (Lab space).",
    )
    ap.add_argument(
        "--spread-p",
        type=float,
        default=95.0,
        help="Percentile used for the spread check (e.g. 95).",
    )
    ap.add_argument("--max-samples", type=int, default=250_000, help="Max pixels sampled from the mask.")
    ap.add_argument("--seed", type=int, default=1337, help="Random seed for sampling and kmeans++.")
    ap.add_argument("--dry-run", action="store_true", help="Compute metrics, but do not write images.")
    args = ap.parse_args()

    site = None
    ortho_p: Path | None = Path(args.ortho) if args.ortho else None
    mask_p: Path | None = Path(args.mask) if args.mask else None
    out_dir: Path | None = Path(args.out) if args.out else None

    # Site defaults (AUTOROAD_SITE) are optional; explicit CLI paths win.
    if ortho_p is None or mask_p is None or out_dir is None:
        try:
            from site_coords import load_site  # local import

            site = load_site()
        except Exception:  # noqa: BLE001
            site = None
        if site is not None:
            auto_ortho, auto_mask, auto_out = _auto_inputs_from_site(site)
            ortho_p = ortho_p or auto_ortho
            mask_p = mask_p or auto_mask
            out_dir = out_dir or auto_out

    if ortho_p is None:
        raise SystemExit("Missing --ortho (and no processed/<site> default ortho found).")
    if mask_p is None:
        raise SystemExit("Missing --mask (and no processed/<site>/mask_rock.png found).")
    if out_dir is None:
        raise SystemExit("Missing --out (internal error).")
    if not ortho_p.is_file():
        raise SystemExit(f"Ortho not found: {ortho_p}")
    if not mask_p.is_file():
        raise SystemExit(f"Mask not found: {mask_p}")

    ortho = _load_rgb(ortho_p)
    h, w = int(ortho.shape[0]), int(ortho.shape[1])
    rock = _load_mask(mask_p, (h, w))
    n_rock = int(rock.sum())
    if n_rock == 0:
        raise SystemExit(f"Rock mask is empty after resize: {mask_p}")

    flat_rgb = ortho.reshape(-1, 3)
    flat_rock = rock.reshape(-1)
    rock_rgb = flat_rgb[flat_rock]

    rng = np.random.default_rng(int(args.seed))
    max_samples = int(args.max_samples)
    if rock_rgb.shape[0] > max_samples:
        sel = rng.choice(rock_rgb.shape[0], size=max_samples, replace=False)
        rock_rgb_s = rock_rgb[sel]
    else:
        rock_rgb_s = rock_rgb
    x_lab = _rgb_u8_to_lab(rock_rgb_s)

    k_start = max(1, int(args.k_start))
    k_max = max(k_start, int(args.k_max))
    spread_p = float(args.spread_p)
    spread_p = float(np.clip(spread_p, 50.0, 99.9))
    threshold = float(args.threshold)

    chosen = None
    for k in range(k_start, k_max + 1):
        labels, centers = _kmeans(x_lab, k, seed=int(args.seed), max_iter=40, tol=1e-4)
        # evaluate per-cluster percentile spread
        d = x_lab - centers[labels]
        de = np.sqrt(np.sum(d * d, axis=1))
        ok = True
        worst = 0.0
        for ci in range(k):
            idx = labels == ci
            if not np.any(idx):
                continue
            p = float(np.percentile(de[idx], spread_p))
            worst = max(worst, p)
            if p > threshold:
                ok = False
        chosen = (k, labels, centers)
        print(
            f"k={k}  samples={x_lab.shape[0]}  rock_px={n_rock}  "
            f"ΔE p{spread_p:.0f} worst={worst:.2f}  threshold={threshold:.2f}  ok={ok}"
        )
        if ok:
            break

    if chosen is None:
        raise SystemExit("kmeans failed (internal error).")
    k, labels, centers = chosen
    metrics = _cluster_metrics(x_lab, labels, centers)

    # Quantize full-res rock pixels (can be expensive but OK for 2k–4k).
    # We assign in Lab space for consistency: convert only rock pixels.
    rock_lab_full = _rgb_u8_to_lab(rock_rgb)
    x2 = np.sum(rock_lab_full * rock_lab_full, axis=1, keepdims=True)
    c2 = np.sum(centers * centers, axis=1, keepdims=True).T
    dist2 = x2 + c2 - 2.0 * (rock_lab_full @ centers.T)
    lab_labels_full = np.argmin(dist2, axis=1).astype(np.int32)

    # RGB centers and "normal" color from full assignments.
    rgb_centers_full = np.zeros((k, 3), dtype=np.float64)
    counts_full = np.zeros((k,), dtype=np.int64)
    for ci in range(k):
        idx = lab_labels_full == ci
        if not np.any(idx):
            continue
        rgb_centers_full[ci] = rock_rgb[idx].mean(axis=0)
        counts_full[ci] = int(idx.sum())
    # Fill empty clusters from nearest by Lab center.
    for ci in range(k):
        if counts_full[ci] > 0:
            continue
        d2c = np.sum((centers - centers[ci]) ** 2, axis=1)
        j = int(np.argmin(d2c))
        rgb_centers_full[ci] = rgb_centers_full[j]
    rgb_centers = np.clip(np.rint(rgb_centers_full), 0, 255).astype(np.uint8)

    rgb_by_id = {int(i): rgb_centers[int(i)].tolist() for i in range(k)}
    n_by_id = {int(i): int(counts_full[int(i)]) for i in range(k)}
    for c in metrics["clusters"]:
        cid = int(c["id"])
        c["center_rgb"] = [int(v) for v in rgb_by_id[cid]]
        c["n_full"] = int(n_by_id[cid])
        rock_px_full = int(rock_rgb.shape[0])
        c["coverage_pct_full"] = (
            float(100.0 * c["n_full"] / rock_px_full) if rock_px_full > 0 else 0.0
        )

    rock_rgb_stats = _rgb_stats(rock_rgb)
    # "Normal" color should not be dominated by deep shadow pixels.
    # We report robust global median, plus a trimmed-mean on mid-lightness pixels.
    L_full = rock_lab_full[:, 0].astype(np.float64, copy=False)
    L_med = float(np.median(L_full)) if L_full.size else 0.0
    L20 = float(np.percentile(L_full, 20)) if L_full.size else 0.0
    L80 = float(np.percentile(L_full, 80)) if L_full.size else 0.0
    mid = (L_full >= L20) & (L_full <= L80)
    trimmed_mean_rgb = (
        [float(v) for v in np.mean(rock_rgb[mid].astype(np.float64), axis=0).tolist()]
        if np.any(mid)
        else rock_rgb_stats["mean"]
    )
    # Cluster whose center L is closest to the median lightness (tie-break by full count).
    center_L = centers[:, 0].astype(np.float64, copy=False)
    rep_id = int(
        np.lexsort(
            (
                -counts_full.astype(np.int64, copy=False),
                np.abs(center_L - L_med),
            )
        )[0]
    ) if counts_full.size else 0
    rock_rgb_median = [int(round(v)) for v in rock_rgb_stats["median"]]
    report = {
        "schema": "rock_color_clusters_v1",
        "inputs": {
            "ortho": str(ortho_p),
            "mask": str(mask_p),
            "autoroa_site": os.environ.get("AUTOROAD_SITE", ""),
            "ortho_size": [h, w],
        },
        "params": {
            "k_start": k_start,
            "k_max": k_max,
            "k_chosen": int(k),
            "threshold_de_p": float(threshold),
            "spread_percentile": float(spread_p),
            "max_samples": int(max_samples),
            "seed": int(args.seed),
        },
        "rock_stats": {
            "rock_px_full": int(rock_rgb.shape[0]),
            "rgb_full": rock_rgb_stats,
            "lab_sample": _lab_stats(x_lab),
            "normal_color": {
                "global_median_rgb": rock_rgb_median,
                "trimmed_mean_rgb": [int(round(v)) for v in trimmed_mean_rgb],
                "rep_cluster_id": int(rep_id),
                "rep_cluster_rgb": [int(v) for v in rgb_centers[rep_id].tolist()],
                "lightness_L_median": L_med,
                "lightness_L_p20": L20,
                "lightness_L_p80": L80,
            },
        },
        "metrics_lab_de76": metrics,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"rock_colors_k{k}.json"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {json_path}")

    if args.dry_run:
        return

    palette_path = out_dir / f"rock_palette_k{k}.png"
    _write_palette(palette_path, rgb_centers, swatch_px=64)
    print(f"Wrote {palette_path}")

    # Labeled palette (id / % / n_full)
    labeled_path = out_dir / f"rock_palette_k{k}_labeled.png"
    rock_px_full = int(rock_rgb.shape[0])
    labels = []
    for i in range(k):
        n = int(counts_full[i])
        pct = (100.0 * n / rock_px_full) if rock_px_full > 0 else 0.0
        labels.append(f"{i} {pct:.1f}%\\n{n}")
    _write_palette_labeled(labeled_path, rgb_centers, labels=labels, swatch_px=64, label_h=28)
    print(f"Wrote {labeled_path}")

    # Build quantized preview image: rock pixels replaced by cluster RGB, others unchanged.
    out = ortho.reshape(-1, 3).copy()
    out_idx = np.flatnonzero(flat_rock)
    out[out_idx] = rgb_centers[lab_labels_full]
    q_img = out.reshape(h, w, 3)
    quant_path = out_dir / f"rock_quantized_k{k}.png"
    Image.fromarray(q_img, mode="RGB").save(quant_path)
    print(f"Wrote {quant_path}")

    # Per-class masks
    for ci in range(k):
        m = np.zeros((h * w,), dtype=np.uint8)
        m[out_idx[lab_labels_full == ci]] = 255
        mp = out_dir / f"rock_class_{ci:02d}_k{k}.png"
        Image.fromarray(m.reshape(h, w), mode="L").save(mp)


if __name__ == "__main__":
    main()
