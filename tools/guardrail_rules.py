"""Guardrail / Leitpoller placement rules from site YAML.

Example::

    beamng:
      guardrails:
        sides: steep           # DGM: only downhill-steep sides (sparse samples)
        steep_sides:
          sample_m: 250        # one probe every ~250 m along each road
          look_out_m: 6.0      # sample beyond asphalt edge
          drop_m: 2.5          # center − outward Z ≥ this → steep
          min_hits: 1
        rules:
          - match: { lanes: ">2" }
            sides: both
          - match: { ids: [2301-2312, 2315] }
            sides: left

``sides: steep`` / ``auto`` / ``auto_steep`` triggers heightmap probing.
``match.ids`` accepts ranges (``2301-2312``). ``match.lanes`` accepts ``>2`` etc.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Iterable, Sequence

from road_edge import left_unit, polyline_length_xy, sample_center_at_s

_RANGE_RE = re.compile(
    r"^\s*(-?\d+)\s*(?:-|\.\.)\s*(-?\d+)\s*$"
)
_PRED_RE = re.compile(
    r"^\s*(>=|<=|!=|==|=|>|<)?\s*([+-]?\d+(?:\.\d+)?)\s*$"
)

# Keys copied from a matching rule onto the resolved rail config.
_OVERRIDE_KEYS = (
    "sides",
    "present",
    "enabled",
    "lateral_extra_m",
    "spacing_m",
    "post_spacing_m",
    "section_length_m",
    "style",
    "styles",
)

_POST_ALIASES = frozenset(
    {"posts", "post", "leitpoller", "delineator", "markers", "marker"}
)
_SECTION_ALIASES = frozenset(
    {"sections", "section", "rail", "rails", "guardrail", "planke", "wbeam"}
)
_BOTH_ALIASES = frozenset({"both", "all", "posts+sections", "sections+posts", "mixed"})


def normalize_styles(raw: Any, *, default: str | list[str] = "posts") -> list[str]:
    """Return ordered unique styles: ``posts`` and/or ``sections``.

    Accepts ``posts`` / ``sections`` / ``both``, a list, or ``styles: [...]``.
    """
    if raw is None:
        raw = default
    out: list[str] = []

    def _add(token: Any) -> None:
        if token is None:
            return
        s = str(token).lower().strip()
        if not s:
            return
        if s in _BOTH_ALIASES:
            for t in ("posts", "sections"):
                if t not in out:
                    out.append(t)
            return
        if s in _POST_ALIASES:
            if "posts" not in out:
                out.append("posts")
            return
        if s in _SECTION_ALIASES:
            if "sections" not in out:
                out.append("sections")
            return

    if isinstance(raw, (list, tuple, set)):
        for t in raw:
            _add(t)
    else:
        _add(raw)
    if not out:
        _add(default)
    return out or ["posts"]

_STEEP_ALIASES = frozenset({"steep", "auto", "auto_steep", "dgm", "terrain"})
ZAt = Callable[[float, float], float]


def parse_id_token(token: Any) -> set[str]:
    """Expand one id / range token to a set of string ids."""
    if token is None:
        return set()
    if isinstance(token, bool):
        return set()
    if isinstance(token, (int, float)) and not isinstance(token, bool):
        # Keep ints as decimal strings without .0
        if isinstance(token, float) and token == int(token):
            return {str(int(token))}
        return {str(int(token)) if isinstance(token, float) and token.is_integer() else str(token)}
    s = str(token).strip()
    if not s:
        return set()
    m = _RANGE_RE.match(s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        lo, hi = (a, b) if a <= b else (b, a)
        return {str(i) for i in range(lo, hi + 1)}
    return {s}


def parse_id_set(spec: Any) -> set[str]:
    """Parse ``ids: 2301-2310`` or ``ids: [2301-2305, 2310]`` → set of str ids."""
    if spec is None:
        return set()
    if isinstance(spec, (list, tuple, set)):
        out: set[str] = set()
        for t in spec:
            out |= parse_id_token(t)
        return out
    return parse_id_token(spec)


def _eval_predicate(raw: Any, value: float | None) -> bool:
    """Compare ``value`` against ``>2`` / ``>=3`` / ``2`` / ``{gt: 2}``."""
    if value is None:
        return False
    if isinstance(raw, dict):
        ok = True
        if "gt" in raw:
            ok = ok and value > float(raw["gt"])
        if "gte" in raw or "ge" in raw:
            ok = ok and value >= float(raw.get("gte", raw.get("ge")))
        if "lt" in raw:
            ok = ok and value < float(raw["lt"])
        if "lte" in raw or "le" in raw:
            ok = ok and value <= float(raw.get("lte", raw.get("le")))
        if "eq" in raw or "=" in raw:
            ok = ok and value == float(raw.get("eq", raw.get("=")))
        if "ne" in raw or "!=" in raw:
            ok = ok and value != float(raw.get("ne", raw.get("!=")))
        return ok
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(value) == float(raw)
    m = _PRED_RE.match(str(raw))
    if not m:
        return False
    op = m.group(1) or "="
    thr = float(m.group(2))
    if op in ("=", "=="):
        return value == thr
    if op == "!=":
        return value != thr
    if op == ">":
        return value > thr
    if op == ">=":
        return value >= thr
    if op == "<":
        return value < thr
    if op == "<=":
        return value <= thr
    return False


def _road_id_strings(road: dict) -> set[str]:
    ids: set[str] = set()
    for key in ("osm_id", "id", "objectid"):
        v = road.get(key)
        if v is not None and v != "":
            ids |= parse_id_token(v)
    for v in road.get("osm_ids") or []:
        if v is not None and v != "":
            ids |= parse_id_token(v)
    return ids


def _road_lanes(road: dict, *, lane_width_m: float = 3.75) -> float | None:
    if road.get("lanes") is not None:
        try:
            return float(road["lanes"])
        except (TypeError, ValueError):
            pass
    # Infer from mean node width when OSM lanes were missing.
    nodes = road.get("nodes") or road.get("pts") or []
    widths = [float(n[3]) for n in nodes if len(n) > 3]
    if widths and lane_width_m > 1e-6:
        return (sum(widths) / len(widths)) / float(lane_width_m)
    return None


def match_rule(match: dict | None, road: dict, *, lane_width_m: float = 3.75) -> bool:
    """True if ``road`` satisfies all clauses in ``match`` (empty match → True)."""
    if not match:
        return True
    if not isinstance(match, dict):
        return False

    if "ids" in match or "id" in match or "osm_id" in match or "objectid" in match:
        wanted = set()
        for k in ("ids", "id", "osm_id", "objectid"):
            if k in match:
                wanted |= parse_id_set(match[k])
        if wanted and not (wanted & _road_id_strings(road)):
            return False

    if "highway" in match:
        hw = str(road.get("highway") or "").lower()
        raw = match["highway"]
        if isinstance(raw, (list, tuple, set)):
            allowed = {str(h).lower() for h in raw}
        else:
            allowed = {str(raw).lower()}
        if hw not in allowed:
            return False

    if "name" in match:
        name = str(road.get("name") or "")
        kunst = str(road.get("kunstbauten") or "")
        blob = f"{name} {kunst}".strip()
        raw = match["name"]
        if isinstance(raw, (list, tuple, set)):
            allowed = {str(x) for x in raw}
            if name not in allowed and kunst not in allowed and blob not in allowed:
                return False
        else:
            s = str(raw)
            if (
                name != s
                and kunst != s
                and s.lower() not in name.lower()
                and s.lower() not in kunst.lower()
            ):
                return False

    if "objekt" in match:
        obj = str(road.get("objekt") or "").upper().strip()
        raw = match["objekt"]
        if isinstance(raw, (list, tuple, set)):
            allowed = {str(x).upper().strip() for x in raw}
        else:
            allowed = {str(raw).upper().strip()}
        if obj not in allowed:
            return False

    if "kunstbauten" in match:
        kunst = str(road.get("kunstbauten") or "")
        raw = match["kunstbauten"]
        if isinstance(raw, (list, tuple, set)):
            if not any(str(x).lower() in kunst.lower() for x in raw):
                return False
        elif str(raw).lower() not in kunst.lower():
            return False

    lanes = _road_lanes(road, lane_width_m=lane_width_m)

    if "lanes" in match and not _eval_predicate(match["lanes"], lanes):
        return False
    # Convenience aliases
    for alias, op in (
        ("lanes_gt", "gt"),
        ("lanes_gte", "gte"),
        ("lanes_lt", "lt"),
        ("lanes_lte", "lte"),
        ("lanes_eq", "eq"),
    ):
        if alias in match:
            if not _eval_predicate({op: match[alias]}, lanes):
                return False

    return True


def resolve_rail_cfg(
    road: dict,
    *,
    defaults: dict,
    rules: Iterable[dict] | None,
    lane_width_m: float = 3.75,
) -> dict:
    """Cascade-merge defaults with all matching rules (later overrides earlier)."""
    cfg = dict(defaults or {})
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        m = rule.get("match")
        if m is None and any(k in rule for k in ("ids", "lanes", "highway", "lanes_gt")):
            # Allow flat shorthand: { ids: [...], sides: left }
            m = {k: rule[k] for k in ("ids", "id", "osm_id", "objectid", "lanes",
                                        "lanes_gt", "lanes_gte", "lanes_lt",
                                        "lanes_lte", "lanes_eq", "highway", "name",
                                        "objekt", "kunstbauten")
                 if k in rule}
        if not match_rule(m if isinstance(m, dict) else {}, road, lane_width_m=lane_width_m):
            continue
        for k in _OVERRIDE_KEYS:
            if k in rule and rule[k] is not None:
                cfg[k] = rule[k]
    # styles list wins over singular style when both set on the same rule layer;
    # cascade already copied whichever was last — normalize at the end.
    if cfg.get("styles") is not None:
        cfg["style"] = normalize_styles(cfg.get("styles"), default=cfg.get("style") or "posts")
    elif cfg.get("style") is not None:
        cfg["style"] = normalize_styles(cfg.get("style"), default="posts")
    # Normalize enabled → present
    if cfg.get("enabled") is False:
        cfg["present"] = False
    if cfg.get("present") is False:
        cfg["sides"] = "none"
    sides = str(cfg.get("sides") or "both").lower().strip()
    if sides in ("none", "off", "no", "false", "0"):
        cfg["sides"] = "none"
        cfg["present"] = False
    elif sides in _STEEP_ALIASES:
        cfg["sides"] = "steep"
        cfg.setdefault("present", True)
    else:
        cfg["sides"] = sides
        cfg.setdefault("present", True)
    return cfg


def is_steep_sides(sides: str | None) -> bool:
    return str(sides or "").lower().strip() in _STEEP_ALIASES or str(
        sides or ""
    ).lower().strip() == "steep"


def _probe_stations(total: float, sample_m: float) -> list[float]:
    """Sparse stations along a road: ends + every ``sample_m`` (few hundred m)."""
    sample_m = max(10.0, float(sample_m))
    if total < 1.0:
        return []
    if total <= sample_m * 1.25:
        # Short fragment: mid (+ ends if long enough)
        if total < 8.0:
            return [0.5 * total]
        return [0.0, 0.5 * total, total]
    dists = [0.0]
    d = sample_m
    while d < total - 0.5 * sample_m:
        dists.append(d)
        d += sample_m
    if dists[-1] < total - 1.0:
        dists.append(total)
    return dists


def detect_steep_sides(
    nodes: Sequence[Sequence[float]],
    z_at: ZAt,
    *,
    sample_m: float = 250.0,
    look_out_m: float = 6.0,
    drop_m: float = 2.5,
    road_width_scale: float = 1.0,
    min_hits: int = 1,
) -> str:
    """Return ``left`` / ``right`` / ``both`` / ``none`` from sparse DGM probes.

    At each station, sample heightmap at the centerline and at
    ``half_width + look_out_m`` left/right. A side counts as steep when
    ``z_center - z_out >= drop_m`` on at least ``min_hits`` stations.
    """
    if len(nodes) < 2:
        return "none"
    total = polyline_length_xy(nodes)
    stations = _probe_stations(total, sample_m)
    if not stations:
        return "none"

    look = max(0.5, float(look_out_m))
    drop = max(0.1, float(drop_m))
    scale = float(road_width_scale) if road_width_scale else 1.0
    need = max(1, int(min_hits))
    left_hits = 0
    right_hits = 0

    for s in stations:
        sample = sample_center_at_s(nodes, s)
        if sample is None:
            continue
        cx, cy, _cz, tx, ty, width = sample
        try:
            zc = float(z_at(cx, cy))
        except Exception:  # noqa: BLE001
            continue
        half = max(0.5, 0.5 * float(width) * scale)
        lx, ly = left_unit(tx, ty)
        for sign, bucket in ((1.0, "left"), (-1.0, "right")):
            ox = cx + lx * sign * (half + look)
            oy = cy + ly * sign * (half + look)
            try:
                zo = float(z_at(ox, oy))
            except Exception:  # noqa: BLE001
                continue
            if zc - zo >= drop:
                if bucket == "left":
                    left_hits += 1
                else:
                    right_hits += 1

    left_ok = left_hits >= need
    right_ok = right_hits >= need
    if left_ok and right_ok:
        return "both"
    if left_ok:
        return "left"
    if right_ok:
        return "right"
    return "none"


def side_signs(sides: str) -> list[tuple[str, float]]:
    s = str(sides or "both").lower().strip()
    if s in ("none", "off", "no", "false", "0") or s in _STEEP_ALIASES:
        return []
    if s == "left":
        return [("left", 1.0)]
    if s == "right":
        return [("right", -1.0)]
    return [("left", 1.0), ("right", -1.0)]


def enrich_prepared_roads(
    prepared: list[dict],
    source_roads: dict | list,
    *,
    lane_width_m: float = 3.75,
) -> list[dict]:
    """Attach ``lanes`` / name from original fragments onto stitched polylines."""
    lookup: dict[str, dict] = {}
    if isinstance(source_roads, dict):
        items = source_roads.items()
        for key, road in items:
            for k in (key, road.get("osm_id"), road.get("id")):
                if k is not None and k != "":
                    lookup[str(k)] = road
    else:
        for road in source_roads:
            for k in (road.get("osm_id"), road.get("id"), road.get("objectid")):
                if k is not None and k != "":
                    lookup[str(k)] = road

    out: list[dict] = []
    for road in prepared:
        rr = dict(road)
        lanes_vals: list[float] = []
        names: list[str] = []
        for oid in rr.get("osm_ids") or [rr.get("osm_id"), rr.get("id")]:
            if oid is None or oid == "":
                continue
            src = lookup.get(str(oid))
            if not src:
                continue
            if src.get("lanes") is not None:
                try:
                    lanes_vals.append(float(src["lanes"]))
                except (TypeError, ValueError):
                    pass
            n = src.get("name")
            if n:
                names.append(str(n))
        if lanes_vals:
            rr["lanes"] = max(lanes_vals)
        elif rr.get("lanes") is None:
            inferred = _road_lanes(rr, lane_width_m=lane_width_m)
            if inferred is not None:
                rr["lanes"] = inferred
        if names and not rr.get("name"):
            rr["name"] = names[0]
        out.append(rr)
    return out
