"""Terrain squares, their triangles and the hole map, the way the BeamNG (Torque3D) terrain uses them.

A hole-map pixel is a terrain VERTEX, not a cell. Marking vertex (i, j) removes the one square from (i, j) to
(i + 1, j + 1): render and collision both skip a square whose lower-left vertex is empty (Torque3D terrFile.cpp,
TerrainFile::_buildGridMap, level 0; terrCell.cpp, _updatePrimitiveBuffer). So a hole for a square is written at its
south-west vertex. Testing pixel positions against an opening and marking the pixel itself shifts every hole by half
a square to the north-east.

Each square is cut into two triangles along a fixed diagonal: south-west to north-east when (i ^ j) is even
(Split45), south-east to north-west otherwise (TerrainFile::_buildGridMap, level 0; TerrainBlock::getHeight).

Grid convention of the gallery tools (build_galleries._to_px_beamng): vertex (px, py) of the image lies at
x = px * pitch, y = (size - 1 - py) * pitch, row 0 north. Grid row j = size - 1 - py counts from the south.
"""
from __future__ import annotations

import math

import numpy as np


def square_hole_pixel(i: int, j: int, size: int) -> tuple[int, int]:
    """(py, px) of the hole-map pixel that removes square (i, j) (lower-left vertex i, j; j from the south)."""
    return size - 1 - j, i


def square_box(i: int, j: int, pitch: float) -> tuple[float, float, float, float]:
    return i * pitch, j * pitch, (i + 1) * pitch, (j + 1) * pitch


def square_range(bounds: tuple[float, float, float, float], size: int, pitch: float) -> tuple[int, int, int, int]:
    """Squares (i0..i1, j0..j1 inclusive) that can touch the box (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = bounds
    i0 = max(0, int(math.floor(x0 / pitch)) - 1)
    j0 = max(0, int(math.floor(y0 / pitch)) - 1)
    i1 = min(size - 2, int(math.floor(x1 / pitch)) + 1)
    j1 = min(size - 2, int(math.floor(y1 / pitch)) + 1)
    return i0, j0, i1, j1


def stamp_polygon(hole: np.ndarray, poly, *, size: int, pitch: float, min_area_m2: float = 1e-3) -> int:
    """Hole every square that overlaps ``poly`` (shapely, BeamNG metres) by more than ``min_area_m2``.

    Returns the number of new hole pixels.
    """
    from shapely import box, prepared

    if poly is None or poly.is_empty:
        return 0
    pp = prepared.prep(poly)
    i0, j0, i1, j1 = square_range(poly.bounds, size, pitch)
    n = 0
    for j in range(j0, j1 + 1):
        for i in range(i0, i1 + 1):
            sq = box(*square_box(i, j, pitch))
            if not pp.intersects(sq):
                continue
            if poly.intersection(sq).area <= min_area_m2:
                continue
            py, px = square_hole_pixel(i, j, size)
            if hole[py, px] < 255:
                hole[py, px] = 255
                n += 1
    return n


def square_triangles(i: int, j: int) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]:
    """The two triangles of square (i, j) as vertex offsets (di, dj) from its lower-left vertex."""
    if ((i ^ j) & 1) == 0:  # Split45: diagonal south-west -> north-east
        return ((0, 0), (1, 0), (1, 1)), ((0, 0), (1, 1), (0, 1))
    return ((0, 0), (1, 0), (0, 1)), ((1, 0), (1, 1), (0, 1))


# barycentric sample points of a triangle (corners, edges and inside), 10 steps per edge
_N = 10
_BARY = np.array(
    [(a / _N, b / _N, 1.0 - (a + b) / _N) for a in range(_N + 1) for b in range(_N + 1 - a)], dtype=np.float64
)


def _project(points: np.ndarray, ax: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Nearest point on the axis polyline for every point: (s, signed offset, z_road, half width)."""
    p0 = ax[:-1, :2]
    d = ax[1:, :2] - p0
    seg2 = np.maximum((d * d).sum(1), 1e-12)
    best = np.full(len(points), np.inf)
    s = np.zeros(len(points))
    off = np.zeros(len(points))
    z = np.zeros(len(points))
    hw = np.zeros(len(points))
    for k in range(len(p0)):
        rel = points - p0[k]
        t = np.clip((rel @ d[k]) / seg2[k], 0.0, 1.0)
        q = rel - t[:, None] * d[k]
        dist = np.hypot(q[:, 0], q[:, 1])
        better = dist < best
        if not better.any():
            continue
        best[better] = dist[better]
        cross = d[k, 0] * rel[:, 1] - d[k, 1] * rel[:, 0]
        off[better] = np.where(cross >= 0, 1.0, -1.0)[better] * dist[better]
        s[better] = (ax[k, 2] + t * (ax[k + 1, 2] - ax[k, 2]))[better]
        z[better] = (ax[k, 3] + t * (ax[k + 1, 3] - ax[k, 3]))[better]
        hw[better] = (0.5 * (ax[k, 4] + t * (ax[k + 1, 4] - ax[k, 4])))[better]
    return s, off, z, hw


def intruding_squares(
    elev: np.ndarray,
    hole: np.ndarray | None,
    nodes: list[dict],
    *,
    size: int,
    pitch: float,
    s_lo: float,
    s_hi: float,
    clear_h: float,
    floor_eps: float = 0.05,
    side_margin: float = 0.15,
) -> list[dict]:
    """Terrain squares (not holed) with a triangle inside the clearance box of a tube.

    The box runs along the axis ``nodes`` (x, y, s, z_road, width) from ``s_lo`` to ``s_hi``: |offset| up to half
    the width + ``side_margin``, height from z_road + ``floor_eps`` to z_road + ``clear_h``. The triangles are the
    terrain's own (``square_triangles``), sampled on a barycentric grid. Returns one dict per square: i, j, s of the
    deepest point, how far into the box it reaches (m) and the hole pixel.
    """
    ax = np.array(
        [(float(n["x"]), float(n["y"]), float(n["s"]), float(n["z_road"]), float(n.get("width") or 7.5)) for n in nodes],
        dtype=np.float64,
    )
    if len(ax) < 2:
        return []
    order = np.argsort(ax[:, 2])
    ax = ax[order]
    inside = (ax[:, 2] >= s_lo - 2.0) & (ax[:, 2] <= s_hi + 2.0)
    if inside.sum() < 2:
        return []
    ax = ax[inside]
    reach = float(ax[:, 4].max()) * 0.5 + side_margin + 1.5 * pitch
    bounds = (ax[:, 0].min() - reach, ax[:, 1].min() - reach, ax[:, 0].max() + reach, ax[:, 1].max() + reach)
    i0, j0, i1, j1 = square_range(bounds, size, pitch)
    ii, jj = np.meshgrid(np.arange(i0, i1 + 1), np.arange(j0, j1 + 1))
    ii, jj = ii.ravel(), jj.ravel()
    if hole is not None:
        py, px = size - 1 - jj, ii
        keep = hole[py, px] == 0
        ii, jj = ii[keep], jj[keep]
    # coarse filter: square centre near the axis
    cs, coff, _cz, chw = _project(np.stack([(ii + 0.5) * pitch, (jj + 0.5) * pitch], 1), ax)
    near = (np.abs(coff) <= chw + side_margin + 0.75 * pitch) & (cs >= s_lo - pitch) & (cs <= s_hi + pitch)
    ii, jj, cz = ii[near], jj[near], _cz[near]
    # a triangle's heights lie between its corners: skip squares wholly above the box or wholly under the floor
    # (road height taken at the centre, +/- the steepest grade of the axis over a square diagonal)
    corners = np.stack(
        [elev[size - 1 - (jj + dj), ii + di] for di, dj in ((0, 0), (1, 0), (0, 1), (1, 1))], 1
    )
    ds = np.diff(ax[:, 2])
    grade = float(np.max(np.abs(np.diff(ax[:, 3])) / np.maximum(ds, 1e-6))) if len(ds) else 0.0
    tol = grade * 1.5 * pitch
    maybe = (corners.max(1) > cz + floor_eps - tol) & (corners.min(1) < cz + clear_h + tol)
    ii, jj = ii[maybe], jj[maybe]
    out: list[dict] = []
    if len(ii) == 0:
        return out

    def h(i, j):
        return elev[size - 1 - j, i]

    pts, zs, owner = [], [], []
    for k, (i, j) in enumerate(zip(ii.tolist(), jj.tolist())):
        for tri in square_triangles(i, j):
            c = np.array([((i + di) * pitch, (j + dj) * pitch, h(i + di, j + dj)) for di, dj in tri])
            p = _BARY @ c
            pts.append(p[:, :2])
            zs.append(p[:, 2])
            owner.append(np.full(len(p), k))
    pts = np.concatenate(pts)
    zs = np.concatenate(zs)
    owner = np.concatenate(owner)
    s, off, z_road, hw = _project(pts, ax)
    lo = z_road + floor_eps
    hi = z_road + clear_h
    hit = (s >= s_lo) & (s <= s_hi) & (np.abs(off) <= hw + side_margin) & (zs > lo) & (zs < hi)
    if not hit.any():
        return out
    depth = np.minimum(zs - lo, hi - zs)
    for k in np.unique(owner[hit]):
        m = hit & (owner == k)
        a = int(np.argmax(np.where(m, depth, -1.0)))
        i, j = int(ii[k]), int(jj[k])
        py, px = square_hole_pixel(i, j, size)
        out.append(
            {"i": i, "j": j, "py": py, "px": px, "s": round(float(s[a]), 2), "into_m": round(float(depth[a]), 3)}
        )
    return out


def _bilinear(elev: np.ndarray, x: np.ndarray, y: np.ndarray, size: int, pitch: float) -> np.ndarray:
    px = np.clip(x / pitch, 0, size - 1.001)
    gy = np.clip(y / pitch, 0, size - 1.001)
    i = np.floor(px).astype(int)
    j = np.floor(gy).astype(int)
    tx, ty = px - i, gy - j
    r0, r1 = size - 1 - j, size - 1 - (j + 1)
    return (
        elev[r0, i] * (1 - tx) * (1 - ty)
        + elev[r0, i + 1] * tx * (1 - ty)
        + elev[r1, i] * (1 - tx) * ty
        + elev[r1, i + 1] * tx * ty
    )


def buried_from(
    elev: np.ndarray,
    nodes: list[dict],
    *,
    s_door: float,
    inward: float,
    size: int,
    pitch: float,
    top_h: float,
    max_m: float = 80.0,
    run_m: float = 3.0,
    step: float = 1.0,
) -> float | None:
    """First station inward from a portal where the tube is under the mountain.

    Walks from ``s_door`` in direction ``inward`` (+1 / -1) and returns the s where the terrain across the whole
    tube (half width + 1 m either side) has stood above z_road + ``top_h`` (roof top) for ``run_m``. Up to there
    the terrain is the portal face or the cut in front of it. None if that never happens within ``max_m``.
    """
    ns = sorted(nodes, key=lambda n: float(n["s"]))
    s = np.array([float(n["s"]) for n in ns])
    xs = np.array([float(n["x"]) for n in ns])
    ys = np.array([float(n["y"]) for n in ns])
    zs = np.array([float(n["z_road"]) for n in ns])
    ws = np.array([float(n.get("width") or 7.5) for n in ns])
    run = 0.0
    k = 0
    while k * step <= max_m + 1e-9:
        sq = s_door + inward * k * step
        if sq < s[0] or sq > s[-1]:
            break
        x, y, z, w = (np.interp(sq, s, a) for a in (xs, ys, zs, ws))
        x1, y1 = np.interp(sq + 0.5, s, xs), np.interp(sq + 0.5, s, ys)
        x0, y0 = np.interp(sq - 0.5, s, xs), np.interp(sq - 0.5, s, ys)
        tx, ty = x1 - x0, y1 - y0
        L = math.hypot(tx, ty) or 1.0
        nx, ny = -ty / L, tx / L
        d = np.linspace(-(0.5 * w + 1.0), 0.5 * w + 1.0, 9)
        terr = _bilinear(elev, x + nx * d, y + ny * d, size, pitch)
        if float(terr.min()) >= z + top_h:
            run += step
            if run >= run_m - 1e-9:
                return sq - inward * (run - step)
        else:
            run = 0.0
        k += 1
    return None
