"""Generic carriageway span profile for bridges, galleries, and similar.

Pipeline:
  1. Centerline from Strassennetz (or any road dict with pts/cum)
  2. Sample 5 longitudinal terrain profiles at width fractions
     0.1 / 0.3 / 0.5 / 0.7 / 0.9
  3. Find abutments where terrain drops (conservative = earliest drop
     on any lateral line, projected to centerline s)
  4. Approaches: cross-profile = inner-lane plane + soft corner blend
     to DGM at edges (0 / 1)
  5. Free span: centerline Z = linear (no sag); lateral offsets lerped
     from abutment cross-profiles (PCHIP available for multi-knot Z)
  6. Emit 4 parallel strip polylines for MeshRoad ribbons

Optional per-structure override: ``abutment_s: [s0, s1]`` (meters along
the centerline) when the heuristic fails.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

try:
    from scipy.interpolate import PchipInterpolator
except ImportError:  # pragma: no cover
    PchipInterpolator = None  # type: ignore

# Lateral sample stations for ditch/abutment heuristics
LATERAL_FRACS: tuple[float, ...] = (0.1, 0.3, 0.5, 0.7, 0.9)
# Full cross-profile including true road edges (for MeshRoad strip Z)
PROFILE_FRACS: tuple[float, ...] = (0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0)
# Four MeshRoad ribbons covering the FULL width 0..1
STRIP_PAIRS: tuple[tuple[float, float], ...] = (
    (0.0, 0.25),
    (0.25, 0.5),
    (0.5, 0.75),
    (0.75, 1.0),
)


ZAt = Callable[[float, float], float]


@dataclass
class SpanProfile:
    """Result of build_span_profile."""

    s0: float
    s1: float
    abut_s0: float
    abut_s1: float
    gip_s0: float
    gip_s1: float
    s: list[float]
    # centerline BeamNG XY / Z / width
    xy: list[tuple[float, float]]
    z_center: list[float]
    width: list[float]
    # shape (N, len(PROFILE_FRACS)) aligned cross-profile Z
    z_lateral: list[list[float]]
    # 4 strips: each is list of (x,y,z,width,nx,ny,nz)
    strips: list[list[tuple[float, float, float, float, float, float, float]]]
    info: dict = field(default_factory=dict)


def _left_unit(tx: float, ty: float) -> tuple[float, float]:
    horiz = math.hypot(tx, ty) or 1.0
    return (-ty / horiz, tx / horiz)


def _right_unit(tx: float, ty: float) -> tuple[float, float]:
    lx, ly = _left_unit(tx, ty)
    return (-lx, -ly)


def normal_from_pitch_crossfall(
    tx: float,
    ty: float,
    dz_ds: float,
    crossfall: float,
) -> tuple[float, float, float]:
    """MeshRoad up-normal from longitudinal grade + planar Querneigung."""
    horiz = math.hypot(tx, ty) or 1.0
    fx, fy = tx / horiz, ty / horiz
    fwd = np.array([fx, fy, dz_ds], dtype=float)
    fn = np.linalg.norm(fwd)
    if fn < 1e-9:
        return (0.0, 0.0, 1.0)
    fwd /= fn
    rx, ry = _right_unit(tx, ty)
    right = np.array([rx, ry, crossfall], dtype=float)
    rn = np.linalg.norm(right)
    if rn < 1e-9:
        return (0.0, 0.0, 1.0)
    right /= rn
    normal = np.cross(fwd, right)
    nn = np.linalg.norm(normal)
    if nn < 1e-9:
        return (0.0, 0.0, 1.0)
    normal /= nn
    if normal[2] < 0:
        normal = -normal
    return float(normal[0]), float(normal[1]), float(normal[2])


def fit_carriageway_cross_profile(
    z_raw: np.ndarray,
    width: float,
    *,
    fracs: tuple[float, ...] = PROFILE_FRACS,
    max_crossfall: float = 0.12,
    corner_up_m: float = 0.25,
    corner_down_m: float = 0.06,
    corner_band: float = 0.2,
) -> tuple[np.ndarray, float, float]:
    """Inner-lane plane + soft corner match to DGM.

    Crossfall from 0.3/0.5/0.7 (clamped). Near the edges (frac within
    ``corner_band`` of 0 or 1), pull Z toward the raw DGM sample — more
    upward than downward — so deck corners meet asphalt without diving
    into embankment/ditch.
    """
    z_raw = np.asarray(z_raw, dtype=float)
    if len(z_raw) != len(fracs):
        raise ValueError(f"z_raw length {len(z_raw)} != fracs {len(fracs)}")
    i_c = fracs.index(0.5)
    i_l = fracs.index(0.3)
    i_r = fracs.index(0.7)
    zc = float(z_raw[i_c])
    span = (0.7 - 0.3) * width
    cf = (float(z_raw[i_r]) - float(z_raw[i_l])) / max(span, 1e-6)
    cf = max(-max_crossfall, min(max_crossfall, cf))

    z_out = np.empty(len(fracs), dtype=float)
    for i, f in enumerate(fracs):
        zp = zc + cf * (f - 0.5) * width
        # How close to a road edge? 0 at center, 1 at f=0 or f=1
        edge_w = max(0.0, min(1.0, (corner_band - min(f, 1.0 - f)) / max(corner_band, 1e-6)))
        if edge_w <= 0.0:
            z_out[i] = zp
            continue
        delta = float(z_raw[i]) - zp
        delta = max(-corner_down_m, min(corner_up_m, delta))
        z_out[i] = zp + edge_w * delta
    return z_out, zc, cf


def z_on_profile(z_prof: list[float] | np.ndarray, f: float, fracs: tuple[float, ...] = PROFILE_FRACS) -> float:
    """Linear interpolate aligned cross-profile at lateral fraction f."""
    z_prof = np.asarray(z_prof, dtype=float)
    if f <= fracs[0]:
        return float(z_prof[0])
    if f >= fracs[-1]:
        return float(z_prof[-1])
    for i in range(len(fracs) - 1):
        if fracs[i] <= f <= fracs[i + 1]:
            t = (f - fracs[i]) / max(fracs[i + 1] - fracs[i], 1e-9)
            return float(z_prof[i] + t * (z_prof[i + 1] - z_prof[i]))
    return float(z_prof[len(z_prof) // 2])


def weld_adjacent_strip_nodes(
    strips: list[list[tuple[float, float, float, float, float, float, float]]],
    *,
    tol_m: float,
) -> int:
    """Snap MeshRoad corners that lie within ``tol_m`` in XY to their midpoint.

    Adjacent strips share an edge; those reconstructed corners are typically
    millimetres apart in plan but differ in Z (corner_up / crossfall). Cluster
    by XY, then move every contributing strip node to the cluster mean Z.
    Side-by-side nodes at the same station are unioned through the shared
    edge, so a whole portal becomes one height.

    Returns how many strip-nodes changed. ``tol_m <= 0`` skips.
    """
    if tol_m <= 1e-9 or len(strips) < 2 or not strips[0]:
        return 0
    n_st = len(strips)
    n_k = len(strips[0])
    if any(len(s) != n_k for s in strips):
        return 0

    def tangent_at(s: int, k: int) -> tuple[float, float]:
        nodes = strips[s]
        if k + 1 < n_k:
            dx = nodes[k + 1][0] - nodes[k][0]
            dy = nodes[k + 1][1] - nodes[k][1]
        elif k > 0:
            dx = nodes[k][0] - nodes[k - 1][0]
            dy = nodes[k][1] - nodes[k - 1][1]
        else:
            return 1.0, 0.0
        L = math.hypot(dx, dy) or 1.0
        return dx / L, dy / L

    def corners(s: int, k: int) -> list[tuple[float, float, float]]:
        x, y, z, w, *_rest = strips[s][k]
        tx, ty = tangent_at(s, k)
        lx, ly = _left_unit(tx, ty)
        hw = 0.5 * float(w)
        return [
            (x + lx * hw, y + ly * hw, z),
            (x - lx * hw, y - ly * hw, z),
        ]

    n_nodes = n_st * n_k
    parent = list(range(n_nodes))

    def idx(s: int, k: int) -> int:
        return s * n_k + k

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Corners: (node_index, x, y, z)
    cxy: list[tuple[int, float, float, float]] = []
    for s in range(n_st):
        for k in range(n_k):
            nid = idx(s, k)
            for cx, cy, cz in corners(s, k):
                cxy.append((nid, cx, cy, cz))

    # Union nodes whose reconstructed corners fall within tol in plan.
    for i in range(len(cxy)):
        ni, xi, yi, _zi = cxy[i]
        for j in range(i + 1, len(cxy)):
            nj, xj, yj, _zj = cxy[j]
            if ni == nj:
                continue
            if math.hypot(xi - xj, yi - yj) <= tol_m:
                union(ni, nj)

    clusters: dict[int, list[int]] = {}
    for i in range(n_nodes):
        clusters.setdefault(find(i), []).append(i)

    n_changed = 0
    new_z = [strips[i // n_k][i % n_k][2] for i in range(n_nodes)]
    for members in clusters.values():
        if len(members) < 2:
            continue
        mz = sum(strips[i // n_k][i % n_k][2] for i in members) / len(members)
        for i in members:
            new_z[i] = mz

    mutable: list[list[list]] = [list(map(list, s)) for s in strips]
    for i in range(n_nodes):
        s, k = divmod(i, n_k)
        if abs(mutable[s][k][2] - new_z[i]) > 1e-6:
            n_changed += 1
        mutable[s][k][2] = new_z[i]

    # Rebuild pitch-only normals so a strip's own left/right corners share Z.
    for s in range(n_st):
        for k in range(n_k):
            n = mutable[s][k]
            if k + 1 < n_k:
                dx = mutable[s][k + 1][0] - n[0]
                dy = mutable[s][k + 1][1] - n[1]
                dz = mutable[s][k + 1][2] - n[2]
            elif k > 0:
                dx = n[0] - mutable[s][k - 1][0]
                dy = n[1] - mutable[s][k - 1][1]
                dz = n[2] - mutable[s][k - 1][2]
            else:
                continue
            Lxy = math.hypot(dx, dy) or 1.0
            tx, ty = dx / Lxy, dy / Lxy
            dz_ds = dz / Lxy
            nx, ny, nz = normal_from_pitch_crossfall(tx, ty, dz_ds, 0.0)
            n[4], n[5], n[6] = nx, ny, nz

    for s in range(n_st):
        strips[s] = [tuple(p) for p in mutable[s]]
    return n_changed


def sample_road_xy(
    road: dict, s: float
) -> tuple[float, float, float, float, float, float, float]:
    """(x, y, z_node, tx, ty, tz, width) at arc-length s."""
    pts = road["pts"]
    cum = road["cum"]
    s = max(0.0, min(float(road["length"]), float(s)))
    for i in range(len(pts) - 1):
        if cum[i + 1] + 1e-9 >= s:
            a, b = pts[i], pts[i + 1]
            seglen = cum[i + 1] - cum[i]
            t = 0.0 if seglen < 1e-9 else (s - cum[i]) / seglen
            ax, ay, az, aw = a
            bx, by, bz, bw = b
            x = ax + t * (bx - ax)
            y = ay + t * (by - ay)
            z = az + t * (bz - az)
            w = aw + t * (bw - aw)
            dx, dy, dz = bx - ax, by - ay, bz - az
            # Plan tangent for lateral offsets (horizontal); keep tz for grade separately
            L_xy = math.hypot(dx, dy) or 1.0
            L3 = math.sqrt(dx * dx + dy * dy + dz * dz) or L_xy
            return x, y, z, dx / L_xy, dy / L_xy, dz / L3, w
    x, y, z, w = pts[-1]
    return x, y, z, 1.0, 0.0, 0.0, w


def project_xy(road: dict, x: float, y: float) -> tuple[float, float, float]:
    """Nearest (s, px, py) on road."""
    best = None
    pts = road["pts"]
    cum = road["cum"]
    for i, (a, b) in enumerate(zip(pts, pts[1:])):
        ax, ay = a[0], a[1]
        bx, by = b[0], b[1]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 < 1e-9 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / seg2))
        px, py = ax + t * dx, ay + t * dy
        d2 = (x - px) ** 2 + (y - py) ** 2
        seglen = math.sqrt(seg2) if seg2 > 0 else 0.0
        s = cum[i] + t * seglen
        if best is None or d2 < best[0]:
            best = (d2, s, px, py)
    assert best is not None
    return best[1], best[2], best[3]


def sample_lateral_terrain(
    road: dict,
    z_terrain: ZAt,
    s: float,
    width: float | None = None,
    fracs: tuple[float, ...] = LATERAL_FRACS,
) -> tuple[np.ndarray, float, float, float, float]:
    """Terrain Z at lateral fractions. Returns (z5, x, y, tx, ty)."""
    x, y, _z, tx, ty, _tz, w = sample_road_xy(road, s)
    if width is not None:
        w = width
    lx, ly = _left_unit(tx, ty)
    # frac 0.5 = center; 0.0 = left edge (-half), 1.0 = right edge (+half)
    half = 0.5 * w
    zs = []
    for f in fracs:
        # left-edge offset = (0.5 - f) in left-units * full width? 
        # f=0.1 → near left: offset along left = +(0.5-0.1)*w = +0.4w from center toward left
        off = (0.5 - f) * w
        zs.append(float(z_terrain(x + off * lx, y + off * ly)))
    return np.asarray(zs, dtype=float), x, y, tx, ty


def _rolling_baseline(z: np.ndarray, win: int) -> np.ndarray:
    """Causal max baseline (solid approach reference) looking backward."""
    out = np.empty_like(z)
    for i in range(len(z)):
        j0 = max(0, i - win + 1)
        out[i] = float(np.max(z[j0 : i + 1]))
    return out


def find_abutments(
    road: dict,
    z_terrain: ZAt,
    s_g0: float,
    s_g1: float,
    *,
    dip_m: float = 0.45,
    search_m: float = 40.0,
    step_m: float = 1.0,
    solid_run_m: float = 3.0,
    width: float | None = None,
    abutment_s: tuple[float, float] | list[float] | None = None,
) -> tuple[float, float, dict]:
    """Conservative abutments from 5-line terrain drop heuristic.

    If ``abutment_s`` is given, use it directly (manual von–bis).
    """
    if abutment_s is not None and len(abutment_s) >= 2:
        a0, a1 = float(abutment_s[0]), float(abutment_s[1])
        if a1 < a0:
            a0, a1 = a1, a0
        return a0, a1, {"method": "override", "abutment_s": [a0, a1]}

    s_lo = max(0.0, min(s_g0, s_g1) - search_m)
    s_hi = min(float(road["length"]), max(s_g0, s_g1) + search_m)
    step = max(0.25, float(step_m))
    s_vals = np.arange(s_lo, s_hi + 0.5 * step, step)
    if len(s_vals) < 5:
        return min(s_g0, s_g1), max(s_g0, s_g1), {"method": "gip_fallback", "reason": "short_window"}

    # z[line, s]
    z_mat = np.zeros((len(LATERAL_FRACS), len(s_vals)))
    for j, s in enumerate(s_vals):
        zs, *_ = sample_lateral_terrain(road, z_terrain, float(s), width=width)
        z_mat[:, j] = zs

    win = max(1, int(round(solid_run_m / step)))
    # Per-line: unstable where baseline_back - z > dip (drop into ditch)
    unstable = np.zeros_like(z_mat, dtype=bool)
    for i in range(len(LATERAL_FRACS)):
        base = _rolling_baseline(z_mat[i], win)
        unstable[i] = (base - z_mat[i]) >= dip_m

    # Also mark forward-looking drop when walking from the right
    unstable_r = np.zeros_like(z_mat, dtype=bool)
    for i in range(len(LATERAL_FRACS)):
        z_rev = z_mat[i, ::-1]
        base_r = _rolling_baseline(z_rev, win)[::-1]
        unstable_r[i] = (base_r - z_mat[i]) >= dip_m

    any_u = unstable | unstable_r
    if not np.any(any_u):
        # No clear ditch — keep padded GIP
        return min(s_g0, s_g1), max(s_g0, s_g1), {
            "method": "gip_fallback",
            "reason": "no_dip",
            "max_drop_m": float(np.max(_rolling_baseline(z_mat[2], win) - z_mat[2])),
        }

    # Conservative: first unstable from left across ANY line; last from right
    col_any = np.any(any_u, axis=0)
    idxs = np.where(col_any)[0]
    i0, i1 = int(idxs[0]), int(idxs[-1])
    # Step back onto solid by solid_run_m
    back = win
    i0 = max(0, i0 - back)
    i1 = min(len(s_vals) - 1, i1 + back)
    a0 = float(s_vals[i0])
    a1 = float(s_vals[i1])
    # Which line triggered first/last
    first_lines = [LATERAL_FRACS[i] for i in range(len(LATERAL_FRACS)) if any_u[i, idxs[0]]]
    last_lines = [LATERAL_FRACS[i] for i in range(len(LATERAL_FRACS)) if any_u[i, idxs[-1]]]
    return a0, a1, {
        "method": "lateral5_conservative",
        "dip_m": dip_m,
        "solid_run_m": solid_run_m,
        "first_unstable_fracs": first_lines,
        "last_unstable_fracs": last_lines,
        "gap_len_m": round(a1 - a0, 2),
    }


def pchip_or_linear(x: np.ndarray, y: np.ndarray, xq: np.ndarray) -> np.ndarray:
    """Monotone cubic (PCHIP) if scipy present and enough knots; else linear."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    xq = np.asarray(xq, dtype=float)
    order = np.argsort(x)
    x, y = x[order], y[order]
    # drop duplicate x
    ok = np.concatenate([[True], np.diff(x) > 1e-9])
    x, y = x[ok], y[ok]
    if len(x) < 2:
        return np.full_like(xq, float(y[0]) if len(y) else 0.0, dtype=float)
    if len(x) == 2 or PchipInterpolator is None:
        return np.interp(xq, x, y)
    return np.asarray(PchipInterpolator(x, y)(xq), dtype=float)


def build_span_profile(
    road: dict,
    z_terrain: ZAt,
    xy_gip: list[tuple[float, float]],
    *,
    width_m: float | None = None,
    dip_m: float = 0.45,
    search_m: float = 40.0,
    step_m: float = 1.0,
    solid_run_m: float = 3.0,
    extend_before_m: float = 0.0,
    extend_after_m: float = 0.0,
    deck_lift_m: float = 0.0,
    portal_z_offset_m: tuple[float, float] | list[float] | None = None,
    abutment_s: tuple[float, float] | list[float] | None = None,
    free_span: str = "linear",  # linear | pchip_ends (no sag default = linear)
    corner_up_m: float = 0.25,
    corner_down_m: float = 0.06,
    corner_band: float = 0.2,
    weld_adjacent_m: float = 0.0,
) -> SpanProfile:
    """Build full span profile + 4 strip polylines."""
    s_ends = [project_xy(road, x, y)[0] for x, y in (xy_gip[0], xy_gip[-1])]
    s_g0, s_g1 = min(s_ends), max(s_ends)
    *_rest, w0 = sample_road_xy(road, 0.5 * (s_g0 + s_g1))
    width = float(width_m if width_m is not None else w0)

    abut0, abut1, abut_info = find_abutments(
        road,
        z_terrain,
        s_g0,
        s_g1,
        dip_m=dip_m,
        search_m=search_m,
        step_m=step_m,
        solid_run_m=solid_run_m,
        width=width,
        abutment_s=abutment_s,
    )
    s0 = max(0.0, abut0 - float(extend_before_m))
    s1 = min(float(road["length"]), abut1 + float(extend_after_m))
    if s1 - s0 < step_m:
        s1 = min(float(road["length"]), s0 + step_m)

    step = max(0.25, float(step_m))
    s_vals = list(np.arange(s0, s1 + 0.5 * step, step))
    if abs(s_vals[-1] - s1) > 0.01:
        s_vals.append(s1)

    lift = float(deck_lift_m)
    if portal_z_offset_m is None:
        dz0, dz1 = 0.0, 0.0
    elif isinstance(portal_z_offset_m, (int, float)):
        dz0 = dz1 = float(portal_z_offset_m)
    else:
        seq = list(portal_z_offset_m)
        if not seq:
            dz0 = dz1 = 0.0
        elif len(seq) == 1:
            dz0 = dz1 = float(seq[0])
        else:
            dz0, dz1 = float(seq[0]), float(seq[1])
    max_cf = 0.12
    corner_up = float(corner_up_m)
    corner_down = float(corner_down_m)
    cband = float(corner_band)
    n_prof = len(PROFILE_FRACS)

    def profile_at(s_ref: float) -> tuple[np.ndarray, float, float]:
        z_raw, *_ = sample_lateral_terrain(
            road, z_terrain, s_ref, width=width, fracs=PROFILE_FRACS
        )
        return fit_carriageway_cross_profile(
            z_raw,
            width,
            fracs=PROFILE_FRACS,
            max_crossfall=max_cf,
            corner_up_m=corner_up,
            corner_down_m=corner_down,
            corner_band=cband,
        )

    # Abutment cross-profiles: planar inner lanes + soft corner match
    z5_a0, zc_a0, cf_a0 = profile_at(abut0)
    z5_a1, zc_a1, cf_a1 = profile_at(abut1)
    zc_a0 = float(zc_a0) + dz0
    zc_a1 = float(zc_a1) + dz1
    z5_a0 = np.asarray(z5_a0, dtype=float) + dz0
    z5_a1 = np.asarray(z5_a1, dtype=float) + dz1
    off_a0 = z5_a0 - zc_a0
    off_a1 = z5_a1 - zc_a1

    def slope_at(s_ref: float, sign: float) -> float:
        ds = max(step, solid_run_m)
        z_here = float(profile_at(s_ref)[1])
        z_out = float(profile_at(s_ref + sign * ds)[1])
        return (z_here - z_out) / ds if sign < 0 else (z_out - z_here) / ds

    m0 = slope_at(abut0, -1.0)
    m1 = slope_at(abut1, +1.0)

    xy: list[tuple[float, float]] = []
    z_center: list[float] = []
    z_lateral: list[list[float]] = []
    widths: list[float] = []
    tangents: list[tuple[float, float]] = []
    crossfalls: list[float] = []

    span_len = max(abut1 - abut0, 1e-6)

    for s in s_vals:
        x, y, _zn, tx, ty, _tz, _w = sample_road_xy(road, s)
        tangents.append((tx, ty))
        widths.append(width)
        xy.append((x, y))
        if s <= abut0 + 1e-9 or s >= abut1 - 1e-9:
            z5, zc, cf = profile_at(s)
            side_dz = dz0 if s <= abut0 + 1e-9 else dz1
            zc = float(zc) + lift + side_dz
            z_center.append(zc)
            z_lateral.append([float(v) + lift + side_dz for v in z5])
            crossfalls.append(cf)
        else:
            t = (s - abut0) / span_len
            if free_span == "pchip_ends" and PchipInterpolator is not None:
                t2 = t * t
                t3 = t2 * t
                h00 = 2 * t3 - 3 * t2 + 1
                h10 = t3 - 2 * t2 + t
                h01 = -2 * t3 + 3 * t2
                h11 = t3 - t2
                z_h = h00 * zc_a0 + h10 * (m0 * span_len) + h01 * zc_a1 + h11 * (m1 * span_len)
                z_lin = zc_a0 + t * (zc_a1 - zc_a0)
                zc = max(float(z_h), float(z_lin)) + lift
            else:
                zc = float(zc_a0 + t * (zc_a1 - zc_a0) + lift)
            off = off_a0 + t * (off_a1 - off_a0)
            cf = float(cf_a0 + t * (cf_a1 - cf_a0))
            z5 = [float(zc + off[i]) for i in range(n_prof)]
            z_center.append(zc)
            z_lateral.append(z5)
            crossfalls.append(cf)

    def local_cf(z_prof: list[float], f: float) -> float:
        """Finite-diff crossfall around strip mid (follows corner blend)."""
        df = 0.05
        f0 = max(0.0, f - df)
        f1 = min(1.0, f + df)
        z0 = z_on_profile(z_prof, f0)
        z1 = z_on_profile(z_prof, f1)
        return (z1 - z0) / max((f1 - f0) * width, 1e-6)

    # Build 4 strips — full width 0..1; Z from corner-aligned profile
    strips: list[list[tuple[float, float, float, float, float, float, float]]] = []
    for f_lo, f_hi in STRIP_PAIRS:
        f_mid = 0.5 * (f_lo + f_hi)
        strip_w = (f_hi - f_lo) * width
        nodes: list[tuple[float, float, float, float, float, float, float]] = []
        for k, s in enumerate(s_vals):
            x, y = xy[k]
            tx, ty = tangents[k]
            lx, ly = _left_unit(tx, ty)
            off = (0.5 - f_mid) * width
            sx = x + off * lx
            sy = y + off * ly
            z = z_on_profile(z_lateral[k], f_mid)
            if k + 1 < len(s_vals):
                dz_ds = (z_center[k + 1] - z_center[k]) / max(s_vals[k + 1] - s, 1e-6)
            elif k > 0:
                dz_ds = (z_center[k] - z_center[k - 1]) / max(s - s_vals[k - 1], 1e-6)
            else:
                dz_ds = 0.0
            cf_loc = local_cf(z_lateral[k], f_mid)
            nx, ny, nz = normal_from_pitch_crossfall(tx, ty, dz_ds, cf_loc)
            nodes.append((sx, sy, z, strip_w, nx, ny, nz))
        strips.append(nodes)

    n_weld = weld_adjacent_strip_nodes(strips, tol_m=float(weld_adjacent_m))

    info = {
        "profile": "road_spline",
        "free_span": free_span,
        "lateral_fracs": list(LATERAL_FRACS),
        "profile_fracs": list(PROFILE_FRACS),
        "strip_pairs": [list(p) for p in STRIP_PAIRS],
        "width_m": width,
        "crossfall_max": max_cf,
        "corner_up_m": corner_up,
        "corner_down_m": corner_down,
        "corner_band": cband,
        "cross_profile": "planar_inner_plus_corner_blend",
        "s0": round(s0, 2),
        "s1": round(s1, 2),
        "abut_s0": round(abut0, 2),
        "abut_s1": round(abut1, 2),
        "gip_s0": round(s_g0, 2),
        "gip_s1": round(s_g1, 2),
        "span_len_m": round(s1 - s0, 2),
        "gap_len_m": round(abut1 - abut0, 2),
        "deck_lift_m": lift,
        "portal_z_offset_m": [round(dz0, 3), round(dz1, 3)],
        "abut": abut_info,
        "nodes": len(s_vals),
        "strips": len(strips),
        "weld_adjacent_m": round(float(weld_adjacent_m), 3),
        "weld_adjacent_n": n_weld,
    }
    return SpanProfile(
        s0=s0,
        s1=s1,
        abut_s0=abut0,
        abut_s1=abut1,
        gip_s0=s_g0,
        gip_s1=s_g1,
        s=s_vals,
        xy=xy,
        z_center=z_center,
        width=widths,
        z_lateral=z_lateral,
        strips=strips,
        info=info,
    )


def road_dict_from_strassennetz_json(data: dict) -> dict:
    """Pick the longest piece as the working centerline."""
    best = None
    for k, road in data.items():
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        pts = [(float(n[0]), float(n[1]), float(n[2]), float(n[3] if len(n) > 3 else 7.0)) for n in nodes]
        cum = [0.0]
        for a, b in zip(pts, pts[1:]):
            cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        cand = {
            "id": k,
            "name": road.get("name"),
            "highway": road.get("highway") or "primary",
            "str_code": road.get("str_code"),
            "source": "strassennetz",
            "pts": pts,
            "cum": cum,
            "length": cum[-1],
        }
        if best is None or cand["length"] > best["length"]:
            best = cand
    if best is None:
        raise SystemExit("strassennetz_beamng.json has no usable polylines")
    return best
