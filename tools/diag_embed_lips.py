"""Visualize terrain_embed delta near gallery portals."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import load_site, processed_dir  # noqa: E402


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    bng = site.get("beamng") or {}
    size = int(bng.get("mask_size") or 2048)
    extent = float(bng.get("meters_per_pixel") or 1.0) * size
    meta = json.loads((proc / "heightmap_meta.json").read_text(encoding="utf-8"))
    max_h = float(meta["max_height_m"])
    dgm = np.asarray(Image.open(proc / f"heightmap_{size}.png"), dtype=np.float64) / 65535.0 * max_h
    emb_path = proc / f"heightmap_{size}_gallery_embed.png"
    if not emb_path.is_file():
        raise SystemExit(f"missing {emb_path}")
    emb = np.asarray(Image.open(emb_path), dtype=np.float64) / 65535.0 * max_h
    delta = emb - dgm
    cl = json.loads((proc / "galleries_centerlines.json").read_text(encoding="utf-8"))
    out = ROOT / "data" / "tmp_template_peek"
    out.mkdir(parents=True, exist_ok=True)

    def to_px(bx: float, by: float) -> tuple[int, int]:
        px = bx / extent * (size - 1)
        py = (1.0 - by / extent) * (size - 1)
        return int(round(px)), int(round(py))

    for g in cl["galleries"]:
        nodes = g["nodes"]
        portal_s = g.get("portal_s") or [nodes[0]["s"], nodes[-1]["s"]]
        slug = "".join(ch if ch.isalnum() else "_" for ch in str(g.get("name") or "gal"))[:40]
        for label, s_p in (("s0", float(portal_s[0])), ("s1", float(portal_s[1]))):
            n = min(nodes, key=lambda nn: abs(float(nn["s"]) - s_p))
            cx, cy = float(n["x"]), float(n["y"])
            tx, ty = float(n["tx"]), float(n["ty"])
            h = math.hypot(tx, ty) or 1.0
            lx, ly = -ty / h, tx / h
            half = 0.5 * float(n["width"])
            px, py = to_px(cx, cy)
            win = 22
            r0, r1 = max(0, py - win), min(size, py + win + 1)
            c0, c1 = max(0, px - win), min(size, px + win + 1)
            patch = delta[r0:r1, c0:c1]
            changed = np.abs(patch) > 0.02
            print(
                f"{g['name']} {label}: changed={int(changed.sum())} "
                f"max|dZ|={float(np.abs(patch).max()):.3f} z_road={n['z_road']}"
            )
            print("  lat from CL (left+):")
            for lat in np.arange(-(half + 4.0), half + 4.5, 0.5):
                x = cx + lat * lx
                y = cy + lat * ly
                c, r = to_px(x, y)
                if 0 <= r < size and 0 <= c < size:
                    d = float(delta[r, c])
                    mark = "*" if abs(d) > 0.05 else "."
                    print(f"    lat={lat:+5.1f} dZ={d:+7.3f} {mark}")

            rgb = np.zeros((patch.shape[0], patch.shape[1], 3), dtype=np.uint8)
            rgb[..., 0] = np.clip(np.maximum(patch, 0.0) / 3.0 * 255.0, 0, 255).astype(np.uint8)
            rgb[..., 2] = np.clip(np.maximum(-patch, 0.0) / 3.0 * 255.0, 0, 255).astype(np.uint8)
            rgb[..., 1] = np.where(changed, 30, 70).astype(np.uint8)
            Image.fromarray(rgb).save(out / f"embed_delta_{slug}_{label}.png")
            Image.fromarray((changed.astype(np.uint8) * 255)).save(
                out / f"embed_mask_{slug}_{label}.png"
            )
    print(f"wrote viz under {out}")


if __name__ == "__main__":
    main()
