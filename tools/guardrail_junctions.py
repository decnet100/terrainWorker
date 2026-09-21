"""Junction mouth opening for guardrails (post-pass).

After rails are placed along GIP axes, open mouths where compatible roads meet.
Grade-separated crossings (bridge over road) and tunnel partners are ignored.
"""
from __future__ import annotations

import math
from typing import Any


def _hypot(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(bx - ax, by - ay)


def _unit(dx: float, dy: float) -> tuple[float, float]:
    L = math.hypot(dx, dy) or 1.0
    return dx / L, dy / L


def _project(
    bx: float, by: float, xy: list[tuple[float, float]]
) -> tuple[float, float, float, float, float] | None:
    """Nearest on polyline → (dist, px, py, tx, ty)."""
    if len(xy) < 2:
        return None
    best = None
    for i in range(len(xy) - 1):
        x0, y0 = xy[i]
        x1, y1 = xy[i + 1]
        dx, dy = x1 - x0, y1 - y0
        seg2 = dx * dx + dy * dy
        if seg2 < 1e-12:
            d = _hypot(bx, by, x0, y0)
            px, py = x0, y0
            tx, ty = 1.0, 0.0
        else:
            t = max(0.0, min(1.0, ((bx - x0) * dx + (by - y0) * dy) / seg2))
            px, py = x0 + t * dx, y0 + t * dy
            d = _hypot(bx, by, px, py)
            tx, ty = _unit(dx, dy)
        if best is None or d < best[0]:
            best = (d, px, py, tx, ty)
    return best


def _z_along(nodes: list, px: float, py: float) -> float:
    """Nearest node Z (good enough for grade separation)."""
    best = None
    for n in nodes:
        if len(n) < 3:
            continue
        d = _hypot(px, py, float(n[0]), float(n[1]))
        if best is None or d < best[0]:
            best = (d, float(n[2]))
    return best[1] if best else 0.0


def _ends(xy: list[tuple[float, float]]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """[(end_xy, outward_tangent), ...] at start and end."""
    if len(xy) < 2:
        return []
    t0 = _unit(xy[0][0] - xy[1][0], xy[0][1] - xy[1][1])  # outward at start
    t1 = _unit(xy[-1][0] - xy[-2][0], xy[-1][1] - xy[-2][1])  # outward at end
    return [(xy[0], t0), (xy[-1], t1)]


def build_junction_axes(
    roads: list[dict],
    *,
    skip_tunnel,
    skip_minor,
) -> list[dict[str, Any]]:
    """Rail-eligible axes for junction detection (no tunnels / minors)."""
    axes: list[dict[str, Any]] = []
    for road in roads:
        if skip_tunnel(road) or skip_minor(road):
            continue
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        xy = [(float(n[0]), float(n[1])) for n in nodes]
        widths = [float(n[3]) for n in nodes if len(n) >= 4]
        half = 0.5 * (max(widths) if widths else 7.5)
        oid = road.get("objectid")
        if oid is None:
            oid = road.get("osm_id")
        axes.append(
            {
                "id": oid,
                "id_s": str(oid) if oid is not None else None,
                "xy": xy,
                "nodes": nodes,
                "half_w": half,
                "ends": _ends(xy),
                "objekt": str(road.get("objekt") or "").upper(),
                "str_code": str(road.get("str_code") or ""),
            }
        )
    return axes


def find_junctions(
    axes: list[dict[str, Any]],
    *,
    join_m: float = 4.0,
    z_sep_m: float = 3.0,
    colin: float = 0.90,
) -> list[dict[str, Any]]:
    """Meetings of compatible axes (endpoint↔endpoint or endpoint↔polyline).

    Skips near-collinear abutting (same through corridor) and grade-separated
    pairs (|Δz| > z_sep_m) so bridge decks do not open rails on the road below.
    """
    join_m = max(0.5, float(join_m))
    z_sep_m = max(0.0, float(z_sep_m))
    out: list[dict[str, Any]] = []
    seen: set[tuple] = set()

    def _add(a: dict, b: dict, jx: float, jy: float, za: float, zb: float) -> None:
        if abs(za - zb) > z_sep_m:
            return
        key = tuple(sorted((a["id_s"], b["id_s"]))) + (round(jx, 1), round(jy, 1))
        if key in seen:
            return
        seen.add(key)
        out.append(
            {
                "a_id": a["id"],
                "b_id": b["id"],
                "xy": (jx, jy),
                "a": a,
                "b": b,
            }
        )

    # Endpoint ↔ endpoint
    for i, a in enumerate(axes):
        for ea, ta in a["ends"]:
            za = _z_along(a["nodes"], ea[0], ea[1])
            for b in axes[i + 1 :]:
                for eb, tb in b["ends"]:
                    if _hypot(ea[0], ea[1], eb[0], eb[1]) > join_m:
                        continue
                    align = abs(ta[0] * tb[0] + ta[1] * tb[1])
                    # Outward tangents of abutting through-pieces point opposite
                    # (~180°) or same if both start — collinear if |align| high.
                    if align >= colin:
                        continue
                    zb = _z_along(b["nodes"], eb[0], eb[1])
                    jx = 0.5 * (ea[0] + eb[0])
                    jy = 0.5 * (ea[1] + eb[1])
                    _add(a, b, jx, jy, za, zb)

    # Endpoint ↔ foreign polyline (T: side road meets mid of through)
    for a in axes:
        for ea, ta in a["ends"]:
            za = _z_along(a["nodes"], ea[0], ea[1])
            for b in axes:
                if a["id_s"] == b["id_s"]:
                    continue
                # Skip if already near a B endpoint (handled above)
                if any(_hypot(ea[0], ea[1], eb[0], eb[1]) <= join_m for eb, _ in b["ends"]):
                    continue
                proj = _project(ea[0], ea[1], b["xy"])
                if proj is None:
                    continue
                dist, px, py, btx, bty = proj
                if dist > max(b["half_w"], join_m):
                    continue
                align = abs(ta[0] * btx + ta[1] * bty)
                if align >= colin:
                    continue
                zb = _z_along(b["nodes"], px, py)
                _add(a, b, px, py, za, zb)

    return out


def side_facing_foreign(
    self_xy: list[tuple[float, float]],
    jx: float,
    jy: float,
    foreign_xy: list[tuple[float, float]],
) -> str | None:
    """``left`` / ``right`` of self toward foreign at junction, or None."""
    proj_s = _project(jx, jy, self_xy)
    proj_f = _project(jx, jy, foreign_xy)
    if proj_s is None or proj_f is None:
        return None
    _ds, sx, sy, stx, sty = proj_s
    _df, fx, fy, _ftx, _fty = proj_f
    # Prefer vector from self point toward a foreign point away from J if possible
    vx, vy = fx - sx, fy - sy
    if math.hypot(vx, vy) < 0.25:
        # Foreign coincides at J — use foreign end farther from J
        best = None
        for p in (foreign_xy[0], foreign_xy[-1]):
            d = _hypot(jx, jy, p[0], p[1])
            if best is None or d > best[0]:
                best = (d, p[0] - sx, p[1] - sy)
        if best is None:
            return None
        vx, vy = best[1], best[2]
    left_x, left_y = -sty, stx
    if vx * left_x + vy * left_y >= 0:
        return "left"
    return "right"


def filter_joints_open_junctions(
    joints: list[tuple],
    *,
    side_name: str,
    self_id,
    self_xy: list[tuple[float, float]],
    junctions: list[dict[str, Any]],
    mouth_m: float = 12.0,
) -> list[tuple]:
    """Drop joints on the side facing a foreign axis near a junction mouth."""
    if not joints or not junctions:
        return joints
    mouth_m = max(1.0, float(mouth_m))
    side = str(side_name or "").lower().strip()
    self_s = str(self_id) if self_id is not None else None
    out: list[tuple] = []
    for j in joints:
        px, py = float(j[0]), float(j[1])
        drop = False
        for jn in junctions:
            a_s = str(jn["a_id"]) if jn.get("a_id") is not None else None
            b_s = str(jn["b_id"]) if jn.get("b_id") is not None else None
            if self_s not in (a_s, b_s):
                continue
            jx, jy = jn["xy"]
            if _hypot(px, py, jx, jy) > mouth_m:
                continue
            foreign = jn["b"] if self_s == a_s else jn["a"]
            face = side_facing_foreign(self_xy, jx, jy, foreign["xy"])
            if face is None:
                continue
            if face == side:
                drop = True
                break
            # Tip of the joining road: also drop joints that sit in the foreign
            # carriageway (both sides would block the asphalt opening).
            proj = _project(px, py, foreign["xy"])
            if proj is not None and proj[0] <= float(foreign["half_w"]) + 1.0:
                drop = True
                break
        if not drop:
            out.append(j)
    return out
