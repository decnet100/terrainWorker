"""Carriageway edge positions for Leitpoller / guardrails.

Posts and rails sit at a fixed lateral distance from the asphalt edge.
Along lane-count transitions the half-width changes with arc-length ``s``;
callers must use ``edge_xy_at_s`` (or ``sample_center_at_s`` + offset)
instead of a constant ``nodes[0][3]``.

``prepare_roads_for_edge`` stitches abutting OSM fragments and soft-blends
width steps — same pipeline as DecalRoad asphalt — so the edge ramp matches
the visible carriageway.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence


def polyline_length_xy(nodes: Sequence[Sequence[float]]) -> float:
    total = 0.0
    for i in range(len(nodes) - 1):
        total += math.hypot(nodes[i + 1][0] - nodes[i][0], nodes[i + 1][1] - nodes[i][1])
    return total


def _node_width(n: Sequence[float], default: float = 6.0) -> float:
    return float(n[3]) if len(n) > 3 else default


def sample_center_at_s(
    nodes: Sequence[Sequence[float]], s: float
) -> tuple[float, float, float, float, float, float] | None:
    """Interpolate centerline at arc-length ``s``.

    Returns ``(x, y, z, tx, ty, width)`` with horizontal unit tangent
    ``(tx, ty)`` and linearly interpolated node width.
    """
    if len(nodes) < 2:
        return None
    total = polyline_length_xy(nodes)
    s = max(0.0, min(total, float(s)))
    acc = 0.0
    for i in range(len(nodes) - 1):
        a, b = nodes[i], nodes[i + 1]
        dx, dy = float(b[0]) - float(a[0]), float(b[1]) - float(a[1])
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        if s <= acc + length + 1e-9:
            t = max(0.0, min(1.0, (s - acc) / length))
            x = float(a[0]) + dx * t
            y = float(a[1]) + dy * t
            z = float(a[2]) + (float(b[2]) - float(a[2])) * t
            w0, w1 = _node_width(a), _node_width(b)
            w = w0 + (w1 - w0) * t
            tx, ty = dx / length, dy / length
            return (x, y, z, tx, ty, w)
        acc += length
    last = nodes[-1]
    # Fallback tangent from last non-degenerate segment
    tx, ty = 1.0, 0.0
    for i in range(len(nodes) - 2, -1, -1):
        dx = float(nodes[i + 1][0]) - float(nodes[i][0])
        dy = float(nodes[i + 1][1]) - float(nodes[i][1])
        L = math.hypot(dx, dy)
        if L >= 1e-9:
            tx, ty = dx / L, dy / L
            break
    return (
        float(last[0]),
        float(last[1]),
        float(last[2]),
        tx,
        ty,
        _node_width(last),
    )


def left_unit(tx: float, ty: float) -> tuple[float, float]:
    horiz = math.hypot(tx, ty) or 1.0
    return (-ty / horiz, tx / horiz)


def side_sign(side: str | float) -> float:
    """``left`` → +1, ``right`` → -1 (left-normal convention)."""
    if isinstance(side, (int, float)):
        return 1.0 if float(side) >= 0.0 else -1.0
    s = str(side).lower().strip()
    if s in ("right", "r", "-1"):
        return -1.0
    return 1.0


def rail_offset_m(
    width_m: float,
    *,
    road_width_scale: float = 1.0,
    lateral_extra_m: float = 0.0,
) -> float:
    """Distance from centerline to post/rail axis (asphalt half-width + extra)."""
    return max(0.0, float(width_m) * float(road_width_scale) * 0.5 + float(lateral_extra_m))


def edge_xy_at_s(
    nodes: Sequence[Sequence[float]],
    s: float,
    side: str | float,
    *,
    road_width_scale: float = 1.0,
    lateral_extra_m: float = 0.0,
) -> tuple[float, float, float, float, float, float] | None:
    """World XY(+Z) on the roadside axis at arc-length ``s``.

    Uses the **interpolated width at ``s``** so lane-count ramps keep posts at a
    fixed distance from the changing asphalt edge.

    Returns ``(x, y, z, tx, ty, offset_m)`` where ``(tx, ty)`` is the centerline
    tangent and ``offset_m`` is the lateral distance applied
    (``|half_width| + lateral_extra``).
    """
    sample = sample_center_at_s(nodes, s)
    if sample is None:
        return None
    x, y, z, tx, ty, width = sample
    offset = rail_offset_m(
        width, road_width_scale=road_width_scale, lateral_extra_m=lateral_extra_m
    )
    sign = side_sign(side)
    lx, ly = left_unit(tx, ty)
    return (
        x + lx * sign * offset,
        y + ly * sign * offset,
        z,
        tx,
        ty,
        offset,
    )


def _roads_dict_to_list(roads: dict) -> list[dict]:
    out: list[dict] = []
    for key, road in roads.items():
        nodes = road.get("nodes") or road.get("pts") or []
        if len(nodes) < 2:
            continue
        pts = [
            [float(n[0]), float(n[1]), float(n[2]), _node_width(n)]
            for n in nodes
        ]
        out.append(
            {
                "id": road.get("osm_id", key),
                "name": road.get("name"),
                "highway": road.get("highway"),
                "lanes": road.get("lanes"),
                "pts": pts,
                "osm_ids": [road.get("osm_id", key)],
            }
        )
    return out


def densify_nodes_xy(
    nodes: list[list[float]], max_step_m: float
) -> list[list[float]]:
    """Insert vertices so no chord exceeds ``max_step_m`` (linear xyzw)."""
    if max_step_m <= 0 or len(nodes) < 2:
        return nodes
    out: list[list[float]] = [list(nodes[0])]
    for a, b in zip(nodes, nodes[1:]):
        seg = math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
        n = max(1, int(math.ceil(seg / max_step_m)))
        for k in range(1, n + 1):
            t = k / n
            out.append(
                [
                    float(a[0]) + t * (float(b[0]) - float(a[0])),
                    float(a[1]) + t * (float(b[1]) - float(a[1])),
                    float(a[2]) + t * (float(b[2]) - float(a[2])),
                    float(a[3]) + t * (float(b[3]) - float(a[3])),
                ]
            )
    return out


def prepare_roads_for_edge(
    roads: dict | list,
    *,
    highways: Iterable[str] | None = None,
    stitch_abutting: bool = True,
    stitch_tol_m: float = 1.25,
    width_fill_dip_m: float = 40.0,
    width_blend_m: float = 25.0,
    densify_max_step_m: float = 12.0,
) -> list[dict]:
    """Stitch abutting ways + blend width steps; return ``[{highway, nodes}, ...]``.

    Matches DecalRoad carriageway geometry so rails track the same edge ramp
    through 2↔4 (etc.) lane transitions and avoid doubled posts at OSM joints.
    Densify before width-smooth so sparse OSM joints get a real blend ramp.
    """
    from build_decal_roads import stitch_abutting_roads  # noqa: WPS433
    from road_width import smooth_roads_widths  # noqa: WPS433

    if isinstance(roads, dict):
        work = _roads_dict_to_list(roads)
    else:
        work = []
        for r in roads:
            pts = r.get("pts") or r.get("nodes") or []
            if len(pts) < 2:
                continue
            work.append(
                {
                    "id": r.get("id") or r.get("osm_id"),
                    "name": r.get("name"),
                    "highway": r.get("highway"),
                    "lanes": r.get("lanes"),
                    "pts": [
                        [float(n[0]), float(n[1]), float(n[2]), _node_width(n)]
                        for n in pts
                    ],
                    "osm_ids": list(r.get("osm_ids") or [r.get("id") or r.get("osm_id")]),
                }
            )

    hwy_list = [str(h).lower() for h in (highways or [])]
    cfg = {
        "stitch_abutting": bool(stitch_abutting),
        "stitch_tol_m": float(stitch_tol_m),
        "highways": hwy_list,
    }
    work = stitch_abutting_roads(work, cfg)

    step = max(0.0, float(densify_max_step_m))
    if step > 0:
        for r in work:
            pts = r.get("pts") or []
            if len(pts) >= 2:
                r["pts"] = densify_nodes_xy(pts, step)

    fill = max(0.0, float(width_fill_dip_m))
    blend = max(0.0, float(width_blend_m))
    if fill > 0 or blend > 0:
        work = smooth_roads_widths(work, fill_dip_m=fill, blend_m=blend)

    hwy_ok = {h.lower() for h in hwy_list} if hwy_list else None
    out: list[dict] = []
    for r in work:
        hw = str(r.get("highway") or "").lower()
        if hwy_ok is not None and hw not in hwy_ok:
            continue
        pts = r.get("pts") or r.get("nodes") or []
        if len(pts) < 2:
            continue
        out.append(
            {
                "highway": r.get("highway"),
                "name": r.get("name"),
                "lanes": r.get("lanes"),
                "osm_id": r.get("id"),
                "osm_ids": r.get("osm_ids"),
                "nodes": [
                    [float(p[0]), float(p[1]), float(p[2]), float(p[3])] for p in pts
                ],
            }
        )
    return out
