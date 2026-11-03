"""Shared rectangle seams: same XYZ on both meshes, manifold, faces up."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_backdrop import _rect_seam_xy, build_meshes  # noqa: E402
from site_coords import SiteCoords  # noqa: E402


def _site() -> dict:
    return {
        "bbox": [0.0, 0.0, 80.0, 80.0],
        "crs": "EPSG:31254",
        "beamng": {"meters_per_pixel": 1.0, "mask_size": 81},
    }


def _pack(origin: float) -> tuple[np.ndarray, dict, np.ndarray]:
    n = 100
    # North-up, same as the Tirol rasters: row 0 is north.
    px, py = 1.0, -1.0
    ox, oy = origin, 100.0 + origin
    cols = np.arange(n, dtype=np.float64)
    rows = np.arange(n, dtype=np.float64)
    xx = ox + (cols + 0.5) * px
    yy = oy + (rows + 0.5) * py
    xx, yy = np.meshgrid(xx, yy)
    elev = 10.0 + 0.05 * xx + 0.02 * yy
    meta = {
        "px": px,
        "py": py,
        "origin_x": ox,
        "origin_y": oy,
        "bbox": [0.0, 0.0, 100.0, 100.0],
    }
    inner = (xx >= 20.0) & (xx <= 60.0) & (yy >= 20.0) & (yy <= 60.0)
    outer = (xx >= 8.0) & (xx <= 72.0) & (yy >= 8.0) & (yy <= 72.0)
    keep = outer & ~inner
    return elev, meta, keep


def _verts(meshes: dict) -> np.ndarray:
    parts = [np.array(v, dtype=np.float64) for v, _f, _u in meshes.values() if v]
    return np.vstack(parts)


def _faces(meshes: dict) -> list[tuple[np.ndarray, list]]:
    return [(np.array(v, dtype=np.float64), f) for v, f, _u in meshes.values() if f]


def _has_point(verts: np.ndarray, p: np.ndarray, tol: float = 1e-6) -> bool:
    d = np.max(np.abs(verts - p), axis=1)
    return bool(np.any(d <= tol))


def _on_rect(x: float, y: float, rect: tuple[float, float, float, float], tol: float = 0.05) -> bool:
    x0, y0, x1, y1 = rect
    on_v = (abs(x - x0) <= tol or abs(x - x1) <= tol) and (y0 - tol <= y <= y1 + tol)
    on_h = (abs(y - y0) <= tol or abs(y - y1) <= tol) and (x0 - tol <= x <= x1 + tol)
    return on_v or on_h


def check(origin: float, inner_xyz: np.ndarray, outer_xyz: np.ndarray) -> dict:
    elev, meta, keep = _pack(origin)
    sc = SiteCoords(_site())
    inner = (20.0, 20.0, 60.0, 60.0)
    outer = (8.0, 8.0, 72.0, 72.0)
    meshes = build_meshes(
        elev,
        keep,
        meta,
        sc,
        0.0,
        5.0,
        split_quadrants=False,
        split_tiles=1,
        uv_bbox=(0.0, 0.0, 100.0, 100.0),
        seams=[
            {"xyz": inner_xyz, "rect": inner, "cover": "outside"},
            {"xyz": outer_xyz, "rect": outer, "cover": "inside"},
        ],
    )
    verts = _verts(meshes)
    missing_i = [p for p in inner_xyz if not _has_point(verts, p)]
    missing_o = [p for p in outer_xyz if not _has_point(verts, p)]
    inside = (
        (verts[:, 0] > 20.02)
        & (verts[:, 0] < 59.98)
        & (verts[:, 1] > 20.02)
        & (verts[:, 1] < 59.98)
    )
    outside = (
        (verts[:, 0] < 7.98)
        | (verts[:, 0] > 72.02)
        | (verts[:, 1] < 7.98)
        | (verts[:, 1] > 72.02)
    )
    down = []
    boundary = []
    bad_bound = []
    for v, faces in _faces(meshes):
        for a, b, c in faces:
            pa, pb, pc = v[a], v[b], v[c]
            cz = (pb[0] - pa[0]) * (pc[1] - pa[1]) - (pb[1] - pa[1]) * (pc[0] - pa[0])
            if cz <= 1e-8:
                down.append(cz)
        counts: dict[tuple[int, int], int] = {}
        for a, b, c in faces:
            for e in ((a, b), (b, c), (c, a)):
                key = (e[0], e[1]) if e[0] < e[1] else (e[1], e[0])
                counts[key] = counts.get(key, 0) + 1
        for (a, b), n in counts.items():
            if n == 1:
                boundary.append((a, b))
                pa, pb = v[a], v[b]
                if not (
                    _on_rect(pa[0], pa[1], inner) and _on_rect(pb[0], pb[1], inner)
                    or _on_rect(pa[0], pa[1], outer) and _on_rect(pb[0], pb[1], outer)
                ):
                    bad_bound.append((pa[:2], pb[:2]))
            elif n > 2:
                bad_bound.append(("multi", n, a, b))
    return {
        "missing_inner": len(missing_i),
        "missing_outer": len(missing_o),
        "inside": int(inside.sum()),
        "outside": int(outside.sum()),
        "down": len(down),
        "bad_bound": len(bad_bound),
        "bad_sample": bad_bound[:4],
        "verts": verts,
    }


def check_gap(origin: float, inner_xyz: np.ndarray, outer_xyz: np.ndarray) -> dict:
    """Viewshed hole on the attachment line must not drop the shared polyline."""
    elev, meta, keep = _pack(origin)
    n = elev.shape[0]
    px, py = 1.0, -1.0
    ox, oy = origin, 100.0 + origin
    cols = ox + (np.arange(n) + 0.5) * px
    rows = oy + (np.arange(n) + 0.5) * py
    xx, yy = np.meshgrid(cols, rows)
    # West of the inner hole, on the attachment line. Inside the hole is already dropped.
    keep[(xx > 10.0) & (xx < 20.0) & (yy > 32.0) & (yy < 48.0)] = False
    sc = SiteCoords(_site())
    inner = (20.0, 20.0, 60.0, 60.0)
    outer = (8.0, 8.0, 72.0, 72.0)
    meshes = build_meshes(
        elev,
        keep,
        meta,
        sc,
        0.0,
        5.0,
        split_quadrants=False,
        split_tiles=1,
        uv_bbox=(0.0, 0.0, 100.0, 100.0),
        seams=[
            {"xyz": inner_xyz, "rect": inner, "cover": "outside"},
            {"xyz": outer_xyz, "rect": outer, "cover": "inside"},
        ],
    )
    verts = _verts(meshes)
    missing_i = sum(0 if _has_point(verts, p) else 1 for p in inner_xyz)
    missing_o = sum(0 if _has_point(verts, p) else 1 for p in outer_xyz)
    multi = 0
    down = 0
    for v, faces in _faces(meshes):
        counts: dict[tuple[int, int], int] = {}
        for a, b, c in faces:
            pa, pb, pc = v[a], v[b], v[c]
            cz = (pb[0] - pa[0]) * (pc[1] - pa[1]) - (pb[1] - pa[1]) * (pc[0] - pa[0])
            if cz <= 1e-8:
                down += 1
            for e in ((a, b), (b, c), (c, a)):
                key = (e[0], e[1]) if e[0] < e[1] else (e[1], e[0])
                counts[key] = counts.get(key, 0) + 1
        multi += sum(1 for n in counts.values() if n > 2)
    return {"missing_inner": missing_i, "missing_outer": missing_o, "down": down, "multi": multi}


def main() -> None:
    inner_xy = _rect_seam_xy(20, 20, 60, 60, 4.0)
    outer_xy = _rect_seam_xy(8, 8, 72, 72, 4.0)
    inner_xyz = np.column_stack([inner_xy, 100.0 + 0.01 * inner_xy[:, 1]])
    outer_xyz = np.column_stack([outer_xy, 50.0 + 0.01 * outer_xy[:, 0]])
    a = check(0.0, inner_xyz, outer_xyz)
    b = check(0.37, inner_xyz, outer_xyz)
    g = check_gap(0.37, inner_xyz, outer_xyz)
    print("origin 0   ", {k: a[k] for k in a if k != "verts" and k != "bad_sample"})
    print("origin 0.37", {k: b[k] for k in b if k != "verts" and k != "bad_sample"})
    print("gap 0.37   ", g)
    if a["bad_sample"]:
        print("bad a", a["bad_sample"])
    if b["bad_sample"]:
        print("bad b", b["bad_sample"])
    # Same seam XYZ in both phases.
    def seam_z(verts: np.ndarray, xyz: np.ndarray) -> int:
        n = 0
        for p in xyz:
            if not _has_point(verts, p):
                n += 1
        return n

    mismatch = seam_z(a["verts"], inner_xyz) + seam_z(b["verts"], inner_xyz)
    mismatch += seam_z(a["verts"], outer_xyz) + seam_z(b["verts"], outer_xyz)
    ok = (
        a["missing_inner"] == 0
        and a["missing_outer"] == 0
        and b["missing_inner"] == 0
        and b["missing_outer"] == 0
        and a["inside"] == 0
        and b["inside"] == 0
        and a["outside"] == 0
        and b["outside"] == 0
        and a["down"] == 0
        and b["down"] == 0
        and a["bad_bound"] == 0
        and b["bad_bound"] == 0
        and mismatch == 0
        and g["missing_inner"] == 0
        and g["missing_outer"] == 0
        and g["down"] == 0
        and g["multi"] == 0
    )
    if not ok:
        raise SystemExit("seam check failed")
    print("seam check ok")


if __name__ == "__main__":
    main()
