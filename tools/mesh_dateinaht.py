"""Build a file-seam point chain (docs/MESH_DATEINAHT.md).

Template line × carriageway polygon → 25 cm samples → nearest on-mask cell
centre (once, in order) → real outline hits at the ends → one (x, y, z).
"""
from __future__ import annotations

import math
from typing import Callable

import numpy as np
from shapely.geometry import LineString, Point

RES_M = 0.5
SAMPLE_M = 0.25
MM = 3
# Consecutive cell centres may be orthogonal or diagonal neighbours.
MAX_CELL_STEP = math.sqrt(2.0) + 1.0e-6


def round_mm(x: float, y: float, z: float | None = None):
    if z is None:
        return (round(float(x), MM), round(float(y), MM))
    return (round(float(x), MM), round(float(y), MM), round(float(z), MM))


def cell_xy(xmin: float, ymax: float, r: int, c: int) -> tuple[float, float]:
    x = float(xmin) + (c + 0.5) * RES_M
    y = float(ymax) - (r + 0.5) * RES_M
    return x, y


def point_to_cell(xmin: float, ymax: float, x: float, y: float) -> tuple[int, int]:
    c = int(math.floor((float(x) - float(xmin)) / RES_M))
    r = int(math.floor((float(ymax) - float(y)) / RES_M))
    return r, c


def nearest_mask_cell(
    mask: np.ndarray,
    xmin: float,
    ymax: float,
    x: float,
    y: float,
    reach: int = 2,
) -> tuple[int, int] | None:
    height, width = mask.shape
    r0, c0 = point_to_cell(xmin, ymax, x, y)
    best = None
    best_d = float("inf")
    for r in range(max(0, r0 - reach), min(height, r0 + reach + 1)):
        for c in range(max(0, c0 - reach), min(width, c0 + reach + 1)):
            if not mask[r, c]:
                continue
            cx, cy = cell_xy(xmin, ymax, r, c)
            d = math.hypot(x - cx, y - cy)
            if d < best_d - 1.0e-12 or (
                abs(d - best_d) <= 1.0e-12 and (r, c) < (best or (r, c))
            ):
                best_d = d
                best = (r, c)
    return best


def sample_z_bilinear(z: np.ndarray, xmin: float, ymax: float, x: float, y: float) -> float:
    height, width = z.shape
    fc = (x - float(xmin)) / RES_M - 0.5
    fr = (float(ymax) - y) / RES_M - 0.5
    c0 = math.floor(fc)
    r0 = math.floor(fr)
    tx = fc - c0
    ty = fr - r0
    acc = 0.0
    weight = 0.0
    for dr, dc, wt in (
        (0, 0, (1.0 - tx) * (1.0 - ty)),
        (0, 1, tx * (1.0 - ty)),
        (1, 0, (1.0 - tx) * ty),
        (1, 1, tx * ty),
    ):
        rr = r0 + dr
        cc = c0 + dc
        if wt <= 0.0 or rr < 0 or cc < 0 or rr >= height or cc >= width:
            continue
        sample = float(z[rr, cc])
        if not math.isfinite(sample):
            continue
        acc += wt * sample
        weight += wt
    if weight <= 0.0:
        return float("nan")
    return acc / weight


def _as_lines(geom) -> list:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "MultiLineString":
        return [g for g in geom.geoms if not g.is_empty]
    if geom.geom_type == "GeometryCollection":
        out = []
        for g in geom.geoms:
            out.extend(_as_lines(g))
        return out
    return []


def densify_line(line: LineString, step_m: float = SAMPLE_M) -> list[tuple[float, float]]:
    length = float(line.length)
    if length <= 0.0:
        c = line.coords[0]
        return [(float(c[0]), float(c[1]))]
    n = max(1, int(math.ceil(length / step_m)))
    pts = []
    for i in range(n + 1):
        t = min(1.0, i / n)
        p = line.interpolate(t * length)
        pts.append((float(p.x), float(p.y)))
    return pts


def build_seam_chain(
    template: LineString,
    road,
    mask: np.ndarray,
    z: np.ndarray,
    xmin: float,
    ymax: float,
    sample_z: Callable[..., float] | None = None,
) -> list[list[dict]]:
    """One chain per template × road intersection.

    Each point: seq, x, y, z, role ('outline'|'cell'), and row/col if cell.
    """
    zfun = sample_z or (
        lambda x, y: sample_z_bilinear(z, xmin, ymax, x, y)
    )
    hit = template.intersection(road)
    pieces = _as_lines(hit)
    if not pieces and not hit.is_empty and hit.geom_type == "Point":
        return []
    chains = []
    for piece in pieces:
        if piece.length < 1.0e-6:
            continue
        outline0 = (float(piece.coords[0][0]), float(piece.coords[0][1]))
        outline1 = (float(piece.coords[-1][0]), float(piece.coords[-1][1]))
        cells: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for x, y in densify_line(piece):
            if road.distance(Point(x, y)) > 1.0e-6:
                continue
            rc = nearest_mask_cell(mask, xmin, ymax, x, y)
            if rc is None:
                continue
            if rc in seen:
                continue
            seen.add(rc)
            cells.append(rc)
        points: list[dict] = []

        def add_outline(xy: tuple[float, float]) -> None:
            zx = zfun(xy[0], xy[1])
            points.append(
                {
                    "x": xy[0],
                    "y": xy[1],
                    "z": float(zx),
                    "role": "outline",
                    "row": None,
                    "col": None,
                }
            )

        def add_cell(r: int, c: int) -> None:
            x, y = cell_xy(xmin, ymax, r, c)
            zx = zfun(x, y)
            points.append(
                {
                    "x": x,
                    "y": y,
                    "z": float(zx),
                    "role": "cell",
                    "row": int(r),
                    "col": int(c),
                }
            )

        add_outline(outline0)
        for r, c in cells:
            add_cell(r, c)
        add_outline(outline1)

        cleaned: list[dict] = []
        for p in points:
            key = round_mm(p["x"], p["y"])
            if cleaned and round_mm(cleaned[-1]["x"], cleaned[-1]["y"]) == key:
                continue
            cleaned.append(p)
        for i, p in enumerate(cleaned):
            p["seq"] = i
        if len(cleaned) >= 2:
            chains.append(cleaned)
    return chains


def chain_to_json(chains: list[list[dict]], *, left: str, right: str) -> dict:
    seams = []
    for ch in chains:
        seams.append(
            {
                "left": left,
                "right": right,
                "points": ch,
            }
        )
    return {"res_m": RES_M, "sample_m": SAMPLE_M, "seams": seams}
