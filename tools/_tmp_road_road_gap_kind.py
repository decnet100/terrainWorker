"""Classify road/road open-edge 'gaps': midpoint offset vs missing shared verts."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_road_mesh import _boundary_edges, _kind_of, _parse_dae, _up_faces
from site_coords import load_site, processed_dir

XY_SEAL_M = 0.01
NEAR_M = 0.25
MM = 3


def main() -> None:
    out_dir = processed_dir(load_site()) / "road_grid"
    index = json.loads((out_dir / "road_grid_index.json").read_text(encoding="utf-8"))
    meshes = []
    for item in index.get("tiles") or []:
        name = str(item["name"])
        if _kind_of(name) != "road":
            continue
        pos, faces = _parse_dae(out_dir / f"{name}.dae")
        up = _up_faces(pos, faces)
        edges = _boundary_edges(pos, up)
        if edges.size == 0:
            continue
        used = np.unique(up.reshape(-1))
        verts = pos[used]
        meshes.append({"name": name, "edges": edges, "verts": verts})

    # All road open-edge midpoints + endpoints
    mids = []
    ends0 = []
    ends1 = []
    labels = []  # mesh index
    for mi, m in enumerate(meshes):
        e = m["edges"]
        mid = np.column_stack(
            (
                0.5 * (e[:, 0] + e[:, 3]),
                0.5 * (e[:, 1] + e[:, 4]),
                0.5 * (e[:, 2] + e[:, 5]),
            )
        )
        mids.append(mid)
        ends0.append(e[:, 0:3])
        ends1.append(e[:, 3:6])
        labels.extend([mi] * len(mid))
    mid = np.vstack(mids)
    end0 = np.vstack(ends0)
    end1 = np.vstack(ends1)
    labels = np.asarray(labels, dtype=np.int32)

    # Vertex trees per mesh (mm XY)
    vert_xy = []
    vert_tree = []
    for m in meshes:
        xy = np.round(m["verts"][:, :2], MM)
        # unique mm keys as points
        uniq = np.unique(xy, axis=0)
        vert_xy.append(uniq)
        vert_tree.append(cKDTree(uniq))

    tree = cKDTree(mid[:, :2])
    dist, idx = tree.query(mid[:, :2], k=min(16, len(mid)), workers=-1)

    n_near = 0
    n_dicht_mid = 0
    n_gap = 0
    # gap subclasses
    both_ends_on_other = 0
    one_end_on_other = 0
    no_end_on_other = 0
    dxy_gap = []
    dxy_both = []
    dxy_none = []
    # nearest vertex on other mesh to this edge midpoint
    mid_to_other_vert = []

    for i in range(len(mid)):
        mi = int(labels[i])
        found = False
        for k in range(1, dist.shape[1]):
            dxy = float(dist[i, k])
            if dxy > NEAR_M:
                break
            j = int(idx[i, k])
            mj = int(labels[j])
            if mj == mi:
                continue
            found = True
            n_near += 1
            if dxy <= XY_SEAL_M:
                n_dicht_mid += 1
            else:
                n_gap += 1
                dxy_gap.append(dxy)
                # do endpoints of edge i exist as mm-XY verts on mesh mj?
                p0 = (round(float(end0[i, 0]), MM), round(float(end0[i, 1]), MM))
                p1 = (round(float(end1[i, 0]), MM), round(float(end1[i, 1]), MM))
                d0, _ = vert_tree[mj].query(p0)
                d1, _ = vert_tree[mj].query(p1)
                on0 = d0 <= XY_SEAL_M
                on1 = d1 <= XY_SEAL_M
                if on0 and on1:
                    both_ends_on_other += 1
                    dxy_both.append(dxy)
                elif on0 or on1:
                    one_end_on_other += 1
                else:
                    no_end_on_other += 1
                    dxy_none.append(dxy)
                dv, _ = vert_tree[mj].query(mid[i, :2])
                mid_to_other_vert.append(float(dv))
            break

    def pct(a, p):
        a = np.asarray(a)
        return float(np.percentile(a, p)) if a.size else float("nan")

    print(f"road files {len(meshes)}")
    print(f"open edges with other-road neighbor within {NEAR_M*100:.0f} cm: {n_near}")
    print(f"  midpoint XY seal <= {XY_SEAL_M*100:.0f} mm: {n_dicht_mid}")
    print(f"  midpoint XY gap {XY_SEAL_M*100:.0f} mm .. {NEAR_M*100:.0f} cm: {n_gap}")
    if dxy_gap:
        a = np.asarray(dxy_gap)
        print(
            f"  gap midpoint dxy cm: p50 {np.median(a)*100:.1f}  "
            f"p90 {pct(a,90)*100:.1f}  max {a.max()*100:.1f}"
        )
        # half-metre peak?
        near_half = ((a > 0.20) & (a < 0.30)).sum()
        print(f"  gap midpoints with 20..30 cm dxy (half-cell band): {near_half}")
    print("among gap edges:")
    print(f"  both endpoints already on other mesh (mm): {both_ends_on_other}")
    print(f"  one endpoint on other mesh: {one_end_on_other}")
    print(f"  no endpoint on other mesh: {no_end_on_other}")
    if dxy_both:
        a = np.asarray(dxy_both)
        print(
            f"    both-ends: midpoint dxy cm p50 {np.median(a)*100:.1f} "
            f"max {a.max()*100:.1f}  -> tessellation/edge pairing, not missing verts"
        )
    if dxy_none:
        a = np.asarray(dxy_none)
        print(
            f"    no-ends: midpoint dxy cm p50 {np.median(a)*100:.1f} "
            f"max {a.max()*100:.1f}  -> parallel offset / different cut geometry"
        )
    if mid_to_other_vert:
        a = np.asarray(mid_to_other_vert)
        print(
            f"  gap midpoint -> nearest other-mesh vertex cm: "
            f"p50 {np.median(a)*100:.1f}  p90 {pct(a,90)*100:.1f}  max {a.max()*100:.1f}"
        )
        print(
            f"  of those, nearest vert <= 1 cm: {(a <= XY_SEAL_M).sum()}  "
            f"<= 5 cm: {(a <= 0.05).sum()}  > 10 cm: {(a > 0.10).sum()}"
        )


if __name__ == "__main__":
    main()
