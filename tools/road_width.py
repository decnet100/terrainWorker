"""Carriageway width from lane count (shared by OSM smoke + Strassennetz).

Default model (Austrian / Central European feel):
  default_lanes × lane_width_m = 2 × 3.75 m = 7.5 m

Priority when resolving a road:
  1. Explicit OSM ``width`` tag (metres)
  2. OSM ``lanes`` / ``lanes:forward``+``lanes:backward`` × ``lane_width_m``
  3. ``lanes_by_highway[highway]`` × ``lane_width_m`` (else ``default_lanes``)
"""
from __future__ import annotations

import math
from typing import Any


def road_width_cfg(bng: dict | None) -> dict[str, Any]:
    raw = bng or {}
    lane_width_m = float(raw.get("lane_width_m") if raw.get("lane_width_m") is not None else 3.75)
    default_lanes = float(raw.get("default_lanes") if raw.get("default_lanes") is not None else 2.0)
    default_road_width_m = float(
        raw.get("default_road_width_m")
        if raw.get("default_road_width_m") is not None
        else lane_width_m * default_lanes
    )
    # Class → lane count when OSM has neither width nor lanes.
    lanes_by_highway = {
        "motorway": 4.0,
        "trunk": 2.0,
        "primary": 2.0,
        "secondary": 2.0,
        "tertiary": 2.0,
        "unclassified": 2.0,
        "residential": 2.0,
        "living_street": 1.0,
        "service": 1.0,
        "track": 1.0,
        "path": 1.0,
    }
    override = raw.get("lanes_by_highway") or {}
    if isinstance(override, dict):
        for k, v in override.items():
            try:
                lanes_by_highway[str(k).lower()] = float(v)
            except (TypeError, ValueError):
                continue
    return {
        "lane_width_m": lane_width_m,
        "default_lanes": default_lanes,
        "default_road_width_m": default_road_width_m,
        "lanes_by_highway": lanes_by_highway,
    }


def _first_float(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    s = str(raw).strip().lower().replace("m", "").replace(",", ".")
    for sep in (";", "|", "/", "-"):
        if sep in s:
            s = s.split(sep)[0].strip()
            break
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def parse_osm_lanes(tags: dict) -> float | None:
    """Total lane count from OSM tags, or None if absent/unparseable.

    Prefer the total ``lanes`` tag. Only sum ``lanes:forward``+``lanes:backward``
    when both are present and ``lanes`` is missing — partial directional tags
    alone (e.g. only ``lanes:backward=1`` next to ``lanes=2``) are common and
    must not override the total.
    """
    total = _first_float(tags.get("lanes"))
    fwd = _first_float(tags.get("lanes:forward"))
    back = _first_float(tags.get("lanes:backward"))
    if total is not None:
        return total
    if fwd is not None and back is not None:
        s = float(fwd) + float(back)
        return s if s > 0 else None
    # Incomplete directional-only tagging — too ambiguous to invent a total.
    return None


def resolve_road_width_m(
    *,
    tags: dict | None = None,
    highway: str | None = None,
    bng: dict | None = None,
) -> tuple[float, float, str]:
    """Return (width_m, lanes, source).

    ``source`` is one of: osm_width | osm_lanes | class_lanes | default_lanes.
    """
    cfg = road_width_cfg(bng)
    tags = tags or {}
    hw = str(highway or tags.get("highway") or "unclassified").lower()

    raw_w = _first_float(tags.get("width"))
    if raw_w is not None:
        lanes = parse_osm_lanes(tags)
        if lanes is None:
            lanes = float(cfg["lanes_by_highway"].get(hw, cfg["default_lanes"]))
        return max(1.5, float(raw_w)), float(lanes), "osm_width"

    lanes = parse_osm_lanes(tags)
    if lanes is not None:
        return max(1.5, float(lanes) * float(cfg["lane_width_m"])), float(lanes), "osm_lanes"

    if hw in cfg["lanes_by_highway"]:
        lanes = float(cfg["lanes_by_highway"][hw])
        src = "class_lanes"
    else:
        lanes = float(cfg["default_lanes"])
        src = "default_lanes"
    return max(1.5, float(lanes) * float(cfg["lane_width_m"])), float(lanes), src


def default_carriageway_width_m(bng: dict | None = None) -> float:
    """Default full carriageway width (2 × 3.75 m unless site overrides)."""
    return float(road_width_cfg(bng)["default_road_width_m"])


def _cum_s_xy(pts: list[list[float]]) -> list[float]:
    s = [0.0]
    for i in range(1, len(pts)):
        s.append(
            s[-1]
            + math.hypot(float(pts[i][0]) - float(pts[i - 1][0]), float(pts[i][1]) - float(pts[i - 1][1]))
        )
    return s


def _window_op(s: list[float], w: list[float], radius_m: float, op) -> list[float]:
    n = len(w)
    out = [0.0] * n
    j0 = 0
    j1 = 0
    for i in range(n):
        lo = s[i] - radius_m
        hi = s[i] + radius_m
        while j0 < n and s[j0] < lo:
            j0 += 1
        if j1 < j0:
            j1 = j0
        while j1 < n and s[j1] <= hi:
            j1 += 1
        window = w[j0:j1]
        out[i] = float(op(window)) if window else float(w[i])
    return out


def smooth_polyline_widths(
    pts: list[list[float]],
    *,
    fill_dip_m: float = 40.0,
    blend_m: float = 25.0,
) -> list[list[float]]:
    """Remove short narrow width dips and soft-blend remaining lane steps.

    OSM often inserts short connector ways with fewer lanes between wider
    sections (2–1–3…). Linear DecalRoad width then pinches to an hourglass.
    Morphological closing fills dips shorter than ``fill_dip_m``; a distance
    box-filter then ramps remaining steps over ``blend_m``.
    """
    if len(pts) < 3:
        return pts
    fill_dip_m = max(0.0, float(fill_dip_m))
    blend_m = max(0.0, float(blend_m))
    if fill_dip_m <= 0 and blend_m <= 0:
        return pts

    s = _cum_s_xy(pts)
    w = [float(p[3]) for p in pts]
    if fill_dip_m > 0:
        r = 0.5 * fill_dip_m
        w = _window_op(s, _window_op(s, w, r, max), r, min)
    if blend_m > 0:
        r = 0.5 * blend_m
        # distance-weighted average in window (triangle-ish via uniform box)
        n = len(w)
        w2 = [0.0] * n
        j0 = j1 = 0
        for i in range(n):
            lo = s[i] - r
            hi = s[i] + r
            while j0 < n and s[j0] < lo:
                j0 += 1
            if j1 < j0:
                j1 = j0
            while j1 < n and s[j1] <= hi:
                j1 += 1
            num = 0.0
            den = 0.0
            for j in range(j0, j1):
                wt = 1.0 - abs(s[j] - s[i]) / max(r, 1e-6)
                if wt <= 0:
                    continue
                num += w[j] * wt
                den += wt
            w2[i] = (num / den) if den > 1e-9 else w[i]
        w = w2

    out: list[list[float]] = []
    for p, ww in zip(pts, w):
        out.append([float(p[0]), float(p[1]), float(p[2]), max(1.5, float(ww))])
    return out


def smooth_roads_widths(
    roads: list[dict],
    *,
    fill_dip_m: float = 40.0,
    blend_m: float = 25.0,
) -> list[dict]:
    """Apply ``smooth_polyline_widths`` to each road's pts/nodes."""
    out: list[dict] = []
    for r in roads:
        rr = dict(r)
        key = "pts" if rr.get("pts") is not None else "nodes"
        pts = list(rr.get(key) or [])
        if len(pts) >= 3:
            rr[key] = smooth_polyline_widths(pts, fill_dip_m=fill_dip_m, blend_m=blend_m)
        out.append(rr)
    return out
