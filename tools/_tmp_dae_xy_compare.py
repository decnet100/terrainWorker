"""Compare plan XY / Z of two road_grid DAE files."""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from site_coords import load_site, processed_dir


def load_pos(path: Path) -> np.ndarray:
    text = path.read_text(encoding="utf-8", errors="replace")
    chosen = None
    for m in re.finditer(
        r'<float_array[^>]*id="([^"]+)"[^>]*count="(\d+)"[^>]*>(.*?)</float_array>',
        text,
        re.S | re.I,
    ):
        if "position" not in m.group(1).lower():
            continue
        count = int(m.group(2))
        nums = np.fromstring(m.group(3), sep=" ", dtype=np.float64)
        if nums.size == count and count % 3 == 0:
            chosen = nums.reshape(-1, 3)
            break
    if chosen is None:
        raise SystemExit(f"no positions in {path}")
    print(
        f"{path.name}: {chosen.shape[0]} verts  "
        f"xyz min={chosen.min(0)} max={chosen.max(0)}",
        flush=True,
    )
    return chosen


def main() -> None:
    site = load_site()
    root = processed_dir(site) / "road_grid"
    a_path = root / "part_051.dae"
    b_path = root / "under28800_000.dae"
    if not b_path.is_file():
        hits = sorted(root.glob("*28800*.dae"))
        if not hits:
            raise SystemExit("under28800 dae missing")
        b_path = hits[0]
    print(f"A={a_path.name}\nB={b_path.name}", flush=True)
    a = load_pos(a_path)
    b = load_pos(b_path)

    def key_xy(p, dec=3):
        return (round(float(p[0]), dec), round(float(p[1]), dec))

    bag_a: dict[tuple, list] = defaultdict(list)
    bag_b: dict[tuple, list] = defaultdict(list)
    for i, p in enumerate(a):
        bag_a[key_xy(p)].append((i, float(p[2]), float(p[0]), float(p[1])))
    for i, p in enumerate(b):
        bag_b[key_xy(p)].append((i, float(p[2]), float(p[0]), float(p[1])))

    common = set(bag_a) & set(bag_b)
    print(
        f"mm-XY keys part={len(bag_a)} under={len(bag_b)} common={len(common)}",
        flush=True,
    )
    diffs = []
    for k in common:
        za = [t[1] for t in bag_a[k]]
        zb = [t[1] for t in bag_b[k]]
        dz = abs(max(za + zb) - min(za + zb))
        if dz > 0.001:
            diffs.append((dz, bag_a[k][0][2], bag_a[k][0][3], za, zb))
    diffs.sort(reverse=True)
    print(f"common mm-XY with |dz|>1mm: {len(diffs)}", flush=True)
    for dz, x, y, za, zb in diffs[:30]:
        print(
            f"  dz={dz:.4f} xy=({x:.3f},{y:.3f}) "
            f"z_part={[round(z, 4) for z in za]} "
            f"z_under={[round(z, 4) for z in zb]}",
            flush=True,
        )

    tree = cKDTree(a[:, :2])
    d, idx = tree.query(b[:, :2], k=1)
    for lim_d, lim_dz, label in (
        (0.05, 0.02, "XY<5cm |dz|>2cm"),
        (0.25, 0.05, "XY<25cm |dz|>5cm"),
        (0.01, 0.01, "XY<1cm |dz|>1cm"),
    ):
        hits = []
        for i in range(len(b)):
            if d[i] >= lim_d:
                continue
            j = int(idx[i])
            dz = abs(float(b[i, 2] - a[j, 2]))
            if dz <= lim_dz:
                continue
            hits.append(
                (
                    dz,
                    float(d[i]),
                    float(b[i, 0]),
                    float(b[i, 1]),
                    float(b[i, 2]),
                    float(a[j, 0]),
                    float(a[j, 1]),
                    float(a[j, 2]),
                )
            )
        hits.sort(reverse=True)
        print(f"{label}: {len(hits)}", flush=True)
        for h in hits[:20]:
            print(
                f"  dz={h[0]:.4f} dxy={h[1]:.4f} "
                f"under=({h[2]:.4f},{h[3]:.4f},{h[4]:.4f}) "
                f"part=({h[5]:.4f},{h[6]:.4f},{h[7]:.4f})",
                flush=True,
            )

    # How many under verts sit on half-metre lattice in terrain XY?
    # Cell centres are *.25 / *.75 when xmin is *.0/*.5 — report frac near *.0/*.5 vs *.25/*.75
    for name, pos in (("part_051", a), (b_path.stem, b)):
        frac = pos[:, :2] - np.floor(pos[:, :2] * 2) / 2
        near_half = np.minimum(frac, 0.5 - frac).max(axis=1) < 0.02
        near_quarter = (
            (np.abs((pos[:, 0] % 0.5) - 0.25) < 0.02)
            & (np.abs((pos[:, 1] % 0.5) - 0.25) < 0.02)
        )
        print(
            f"{name}: verts near *.0/*.5 lattice {near_half.mean()*100:.1f}%  "
            f"near *.25/*.25 centres {near_quarter.mean()*100:.1f}%",
            flush=True,
        )


if __name__ == "__main__":
    main()
