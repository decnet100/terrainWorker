"""First check of whether the raw terrain contains a road.

Every GIP link is sampled every 2.5 m. Each sample also has a point 1.5 m to
the left and 1.5 m to the right. The grade of each line, over that step, is
the unsigned slope. Further side distances can be added in SIDE_M; this run
uses 1.5 m only.

A sample that lands on water is invalid before those tests. Water is the
Austrian land-cover class for water, plus Swiss cadastral polygons whose
type starts with Gewaesser. A line that touches water does not enter the
class figures. Its reason is water.

A bridge, tunnel or gallery label covers a station range on its link. The
first pass sets that range aside. The rest of the link stays in the tests and
can form a group. A link named in the manual file is set aside for its whole
length. The spline looks for a group of at least six valid intervals on each
side of the labeled range, starting outside that range on the same link.

The chain crosses link ends onto the next link with the same
street code. One other link at the junction is taken as it is. Otherwise the
next link is the same functional class, and a remaining choice is the link
whose angle at the node is closest to 180 degrees. The spline runs through
those two groups. An interval inside the label is recovered when both
ends are within 1 m of the spline and the miss divided by the distance to the
nearest group stays under 10 percent. Recovered intervals go back into the
class figures, the tests run again, and the spline is tried once more so a
link that had no group beside it can use a neighbour that has just been
recovered.

A chord between two valid groups that rises faster than the road allows is
rejected. The limit is 15 percent on S-B and S-L, 20 percent on S-GW and
Sammelstrasse, and 30 percent on Weg. The shorter group is then invalid,
with reason steep_chord, but only when that group is 100 m or shorter.
A longer group stays. Equal lengths of 100 m or less drop both. The chords
are built again until every remaining chord stays under the limit, or the
shorter group is too long to drop.

A line fails when its grade is over 30 percent, or over 40 percent on Weg,
or over the median plus three standard deviations of its functional class.
The class figures use only grades at or below that cap. An interval fails
only when every line has failed.
One passing line keeps it. A line that passed is then checked against its
neighbours on that same line. With a valid interval on both sides, it fails
when its grade misses the average of those two by more than the class allows.
With a valid interval on only one side, the allowed miss is twice that. The
class allowance is the median of the two-sided misses plus three standard
deviations.

The terrain is the mixed DGM inside a 15 m buffer of every GIP link
(``dgm_link15/dgm_link15.tif``). That grid is 1 m. Build it with
``tools/mask_dgm_by_links.py`` when it is missing.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\score_check_grade.py
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
import tifffile as tiff
from scipy.interpolate import CubicSpline
from shapely import STRtree
from shapely.geometry import LineString, Point
from shapely.ops import linemerge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_link_surfaces import _code  # noqa: E402
from fetch_bev_landcover import _raw_paths  # noqa: E402
from gip_routing_load import gpkg_path  # noqa: E402
from raster_tile_fetch import elevation_nodata_mask  # noqa: E402
from score_link_surfaces import _z_at  # noqa: E402
from site_coords import load_site, processed_dir, resolve_site_path  # noqa: E402

STEP_M = 2.5
SIDE_M = (1.5,)
GRADE_CAP_PCT = 30.0
WEG_GRADE_CAP_PCT = 40.0
MIN_STEP_M = 1.0
# Raw BEV mosaic: 5 is water, 6 is nodata. See fetch_bev_landcover.CLASS_NAMES.
BEV_WATER = 5
GROUP_MIN = 6
# A valid group longer than this is a road, not a false flat. It is not dropped.
GROUP_KEEP_M = 100.0
# End-to-end chord slope must stay under this percent. Exactly the limit fails.
SLOPE_LIMIT_PCT = {
    "S-B": 15.0,
    "S-L": 15.0,
    "S-GW": 20.0,
    "Sammelstrasse": 20.0,
    "Weg": 30.0,
}
SPLINE_MISS_M = 1.0
SPLINE_RATIO = 0.10
NODE_MATCH_M = 2.0
RAY_M = 12.0


def _load_terrain(path: Path) -> tuple[np.ndarray, dict]:
    """GeoTIFF written with an outer north-west corner. Cell size is the file's own."""
    with tiff.TiffFile(path) as src:
        page = src.pages[0]
        z = np.asarray(page.asarray(), dtype=np.float32)
        scale = tuple(page.tags["ModelPixelScaleTag"].value)
        tie = tuple(page.tags["ModelTiepointTag"].value)
    if z.ndim == 3:
        z = z[..., 0]
    bad = elevation_nodata_mask(z) | (z < 50.0) | (z > 4500.0)
    if np.any(bad):
        z = z.copy()
        z[bad] = np.nan
    res = float(scale[0])
    height, width = z.shape
    xmin = float(tie[3])
    ymax = float(tie[4])
    return z, {
        "raster": path.name,
        "source": str(path),
        "xmin": xmin,
        "ymin": ymax - height * res,
        "xmax": xmin + width * res,
        "ymax": ymax,
        "width": int(width),
        "height": int(height),
        "resolution_m": res,
    }


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    return text


def _line(geom) -> LineString | None:
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "MultiLineString":
        geom = linemerge(geom)
        if geom.geom_type == "MultiLineString":
            geom = max(geom.geoms, key=lambda part: part.length)
    if geom.geom_type != "LineString" or geom.length < MIN_STEP_M:
        return None
    return geom


def _stations(length: float) -> list[float]:
    stops = [float(s) for s in np.arange(0.0, length, STEP_M)]
    if not stops:
        stops = [0.0]
    if length - stops[-1] >= MIN_STEP_M:
        stops.append(float(length))
    return stops


def _normal(line: LineString, station: float) -> tuple[float, float]:
    length = float(line.length)
    before = line.interpolate(max(0.0, station - 1.0))
    after = line.interpolate(min(length, station + 1.0))
    dx = float(after.x - before.x)
    dy = float(after.y - before.y)
    span = math.hypot(dx, dy)
    if span < 1e-6:
        return 0.0, 0.0
    return -dy / span, dx / span


def _manual_bridges() -> set[str]:
    """Link ids listed beside the site yaml. Each whole link is a bridge."""
    site_path = resolve_site_path()
    path = site_path.with_name(site_path.stem + "_manual_invalid.txt")
    if not path.is_file():
        return set()
    ids = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.split("#", 1)[0].strip()
        if text:
            ids.add(text)
    print(f"manual bridges {len(ids)} from {path}", flush=True)
    return ids


def _pct(value, default: float) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number):
        return default
    return number


def _structure_pcts(site: dict) -> dict[str, list[tuple[float, float]]]:
    """Station share of each bridge, tunnel or gallery, in percent of its link."""
    refs = gpd.read_file(gpkg_path(site), layer="bridge_ref")
    ranges: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in refs.itertuples(index=False):
        start = _pct(row.from_pct, 0.0)
        end = _pct(row.to_pct, 100.0)
        if abs(end - start) < 0.05:
            continue
        ranges[str(row.link_id)].append((start, end))
    print(f"structure labels {sum(len(v) for v in ranges.values())} on {len(ranges)} links", flush=True)
    return ranges


def _label_ranges(lid: str, length: float, pcts: dict[str, list[tuple[float, float]]], manual: set[str]) -> list[tuple[float, float]]:
    """Meters along the line. A manual id covers the whole line."""
    if lid in manual:
        return [(0.0, length)]
    out = []
    for start, end in pcts.get(lid, []):
        span = abs(end - start) / 100.0 * length
        mid = 0.5 * (start + end) / 100.0 * length
        lo = max(0.0, mid - 0.5 * span)
        hi = min(length, lo + span)
        lo = max(0.0, hi - span)
        if hi - lo >= 0.5:
            out.append((lo, hi))
    return out


def _in_label(s0: float, s1: float, ranges: list[tuple[float, float]]) -> bool:
    return any(s1 > lo and s0 < hi for lo, hi in ranges)


def _ends(items: list[dict], nodes: gpd.GeoDataFrame) -> dict[str, tuple[str, str]]:
    """Node at each end of a sampled link. A far endpoint keeps its own key."""
    geoms = list(nodes.geometry)
    node_ids = [str(value) for value in nodes["node_id"].tolist()]
    tree = STRtree(geoms) if geoms else None

    def nearest(x: float, y: float, tag: str) -> str:
        if tree is None:
            return tag
        point = Point(x, y)
        idx = int(tree.nearest(point))
        if point.distance(geoms[idx]) > NODE_MATCH_M:
            return tag
        return node_ids[idx]

    ends = {}
    for item in items:
        line = item["line"]
        lid = item["id"]
        ends[lid] = (
            nearest(float(line.coords[0][0]), float(line.coords[0][1]), lid + ":a"),
            nearest(float(line.coords[-1][0]), float(line.coords[-1][1]), lid + ":b"),
        )
    return ends


def _turn_angle(origin: tuple[float, float], other: tuple[float, float]) -> float:
    """Angle in degrees between two rays that leave the same node. Straight is 180."""
    left = math.hypot(origin[0], origin[1])
    right = math.hypot(other[0], other[1])
    if left < 1e-9 or right < 1e-9:
        return 0.0
    cos = (origin[0] * other[0] + origin[1] * other[1]) / (left * right)
    cos = max(-1.0, min(1.0, cos))
    return math.degrees(math.acos(cos))


def _ray(line: LineString, at_start: bool) -> tuple[float, float]:
    length = float(line.length)
    if at_start:
        origin = line.interpolate(0.0)
        other = line.interpolate(min(length, RAY_M))
    else:
        origin = line.interpolate(length)
        other = line.interpolate(max(0.0, length - RAY_M))
    return float(other.x - origin.x), float(other.y - origin.y)


def _chains(items: list[dict], ends: dict[str, tuple[str, str]]) -> list[list[tuple[str, bool]]]:
    """Ordered links. The bool is true when the chain follows the line direction."""
    by_id = {item["id"]: item for item in items}
    at: dict[str, list[str]] = defaultdict(list)
    for lid, (start, end) in ends.items():
        at[start].append(lid)
        if start != end:
            at[end].append(lid)
    used: set[str] = set()

    def opening(lid: str, node: str) -> tuple[float, float]:
        start, _end = ends[lid]
        return _ray(by_id[lid]["line"], at_start=(node == start))

    def pick(arrival: str, node: str, cands: list[str]) -> str:
        if len(cands) == 1:
            return cands[0]
        code = by_id[arrival]["street"]
        same = [cand for cand in cands if code and by_id[cand]["street"] == code]
        if same:
            pool = same
        else:
            level = by_id[arrival]["fc"]
            same_fc = [cand for cand in cands if by_id[cand]["fc"] == level]
            pool = same_fc or cands
        return max(pool, key=lambda cand: _turn_angle(opening(arrival, node), opening(cand, node)))

    def beyond(lid: str, leave_node: str) -> list[tuple[str, bool]]:
        out = []
        prev = lid
        node = leave_node
        while True:
            cands = [cand for cand in at[node] if cand not in used and cand != prev]
            if not cands:
                break
            nxt = pick(prev, node, cands)
            used.add(nxt)
            start, end = ends[nxt]
            out.append((nxt, node == start))
            prev = nxt
            node = end if node == start else start
        return out

    chains = []
    for lid in ends:
        if lid in used:
            continue
        used.add(lid)
        start, end = ends[lid]
        left = beyond(lid, start)
        right = beyond(lid, end)
        chains.append([(i, not forward) for i, forward in reversed(left)] + [(lid, True)] + right)
    print(f"chains {len(chains)}", flush=True)
    return chains


def _load_water(site: dict):
    """Austrian water pixels and Swiss water polygons, in the site CRS."""
    path, meta_path = _raw_paths(site)
    if not path.is_file() or not meta_path.is_file():
        raise SystemExit(f"missing land cover {path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    codes = np.asarray(tiff.imread(path))
    if codes.ndim == 3:
        codes = codes[..., 0]
    xmin, ymin, xmax, ymax = (float(v) for v in meta["bbox"])
    height, width = codes.shape
    grid = {
        "water": codes == BEV_WATER,
        "xmin": xmin,
        "ymin": ymin,
        "xmax": xmax,
        "ymax": ymax,
        "width": int(width),
        "height": int(height),
    }
    n_raster = int(grid["water"].sum())
    print(f"land cover {path.name}: water pixels {n_raster}", flush=True)

    swiss = None
    src = ((site.get("sources") or {}).get("av_landcover") or {})
    rel = str(src.get("path") or "").strip()
    av_path = ROOT / rel if rel and not Path(rel).is_file() else Path(rel) if rel else None
    if av_path is not None and av_path.is_file():
        layer = str(src.get("layer") or "Bodenbedeckung_ohne_Gebaeude_farbig")
        attr = str(src.get("attribute") or "art")
        polygons = gpd.read_file(av_path, layer=layer)
        if polygons.crs is None:
            polygons = polygons.set_crs(str(src.get("crs") or "EPSG:2056"))
        kind = polygons[attr].astype(str)
        polygons = polygons[kind.str.startswith("Gewaesser")]
        if not polygons.empty:
            polygons = polygons.to_crs(site.get("crs") or "EPSG:25832")
            swiss = shapely.union_all(polygons.geometry.values)
            shapely.prepare(swiss)
            print(f"swiss water polygons {len(polygons)} from {av_path.name}", flush=True)
    return grid, swiss


def _on_water(xs: np.ndarray, ys: np.ndarray, grid: dict, swiss) -> np.ndarray:
    """True where the sample sits on a water pixel or inside a water polygon."""
    span_x = (grid["xmax"] - grid["xmin"]) / grid["width"]
    span_y = (grid["ymax"] - grid["ymin"]) / grid["height"]
    col = np.floor((xs - grid["xmin"]) / span_x).astype(np.int32)
    row = np.floor((grid["ymax"] - ys) / span_y).astype(np.int32)
    mask = grid["water"]
    out = np.zeros(xs.shape, dtype=bool)
    inside = (row >= 0) & (col >= 0) & (row < mask.shape[0]) & (col < mask.shape[1])
    out[inside] = mask[row[inside], col[inside]]
    if swiss is not None:
        pending = ~out
        if np.any(pending):
            hit = np.zeros(xs.shape, dtype=bool)
            hit[pending] = shapely.contains_xy(swiss, xs[pending], ys[pending])
            out |= hit
    return out


def _grade_cap(code: str) -> float:
    if code == "Weg":
        return WEG_GRADE_CAP_PCT
    return GRADE_CAP_PCT


def _reason(slope: float, limit: float | None, *, water: bool = False, cap: float = GRADE_CAP_PCT) -> str:
    if water:
        return "water"
    if not np.isfinite(slope):
        return "no_ground"
    if slope > cap:
        return "slope_30"
    if limit is not None and slope > limit:
        return "slope_class"
    return ""


def _change_values(slopes: list[float], passed: list[bool]) -> tuple[list[str], list[float]]:
    """Miss of this grade against one or two neighbours that passed the grade test."""
    n = len(slopes)
    kind = [""] * n
    value = [float("nan")] * n
    for i, slope in enumerate(slopes):
        if not passed[i] or not np.isfinite(slope):
            continue
        prev = i - 1 if i > 0 and passed[i - 1] and np.isfinite(slopes[i - 1]) else None
        nxt = i + 1 if i + 1 < n and passed[i + 1] and np.isfinite(slopes[i + 1]) else None
        if prev is not None and nxt is not None:
            kind[i] = "triple"
            value[i] = abs(slope - 0.5 * (slopes[prev] + slopes[nxt]))
        elif prev is not None:
            kind[i] = "pair"
            value[i] = abs(slope - slopes[prev])
        elif nxt is not None:
            kind[i] = "pair"
            value[i] = abs(slope - slopes[nxt])
    return kind, value


def _dist(values: list[float]) -> dict:
    kept = np.asarray([v for v in values if np.isfinite(v)], dtype=np.float64)
    if kept.size == 0:
        return {"n": 0, "median": None, "std": None, "p90": None, "p95": None, "p99": None, "max": None, "limit": None}
    std = float(kept.std(ddof=1)) if kept.size > 1 else 0.0
    median = float(np.median(kept))
    return {
        "n": int(kept.size),
        "median": round(median, 3),
        "std": round(std, 3),
        "p90": round(float(np.quantile(kept, 0.90)), 3),
        "p95": round(float(np.quantile(kept, 0.95)), 3),
        "p99": round(float(np.quantile(kept, 0.99)), 3),
        "max": round(float(kept.max()), 3),
        "limit": round(median + 3.0 * std, 3),
    }


def _change_fail(kind: str, value: float, limit: float | None) -> bool:
    if kind == "" or limit is None or not np.isfinite(value):
        return False
    cap = limit if kind == "triple" else 2.0 * limit
    return value > cap


def _interval_reason(*reasons: str) -> str:
    """Empty when any line passed. Otherwise the sharpest failure among them."""
    failed = [reason for reason in reasons if reason]
    if len(failed) < len(reasons):
        return ""
    if "slope_30" in failed:
        return "slope_30"
    if "slope_class" in failed:
        return "slope_class"
    if "water" in failed:
        return "water"
    return "no_ground"


def _line_grade(z: np.ndarray, water: np.ndarray, step: np.ndarray) -> tuple[list[float], list[bool]]:
    """Grade of one line. A step that touches water is kept out of the statistics."""
    slopes = []
    touched = []
    for i, distance in enumerate(step):
        on_water = bool(water[i] or water[i + 1])
        touched.append(on_water)
        slopes.append(float("nan") if on_water else _grade(float(z[i]), float(z[i + 1]), float(distance)))
    return slopes, touched


def _samples(line: LineString, elev, spec, grid: dict, swiss) -> dict[str, np.ndarray] | None:
    """Elevations on the center line and on each side distance in SIDE_M."""
    stops = _stations(float(line.length))
    if len(stops) < 2:
        return None
    center = []
    normals = []
    for station in stops:
        point = line.interpolate(station)
        center.append((float(point.x), float(point.y)))
        normals.append(_normal(line, station))
    xy = np.asarray(center, dtype=np.float64)
    normal = np.asarray(normals, dtype=np.float64)
    out = {
        "s": np.asarray(stops, dtype=np.float64),
        "x": xy[:, 0],
        "y": xy[:, 1],
        "z": _z_at(xy[:, 0], xy[:, 1], elev, spec),
        "water": _on_water(xy[:, 0], xy[:, 1], grid, swiss),
    }
    for offset in SIDE_M:
        out[f"xl_{offset}"] = xy[:, 0] + normal[:, 0] * offset
        out[f"yl_{offset}"] = xy[:, 1] + normal[:, 1] * offset
        out[f"zl_{offset}"] = _z_at(out[f"xl_{offset}"], out[f"yl_{offset}"], elev, spec)
        out[f"wl_{offset}"] = _on_water(out[f"xl_{offset}"], out[f"yl_{offset}"], grid, swiss)
        out[f"xr_{offset}"] = xy[:, 0] - normal[:, 0] * offset
        out[f"yr_{offset}"] = xy[:, 1] - normal[:, 1] * offset
        out[f"zr_{offset}"] = _z_at(out[f"xr_{offset}"], out[f"yr_{offset}"], elev, spec)
        out[f"wr_{offset}"] = _on_water(out[f"xr_{offset}"], out[f"yr_{offset}"], grid, swiss)
    return out


def _grade(z0: float, z1: float, distance: float) -> float:
    if distance < 1e-6 or not np.isfinite(z0) or not np.isfinite(z1):
        return float("nan")
    return 100.0 * abs(z1 - z0) / distance


def _class_limits(grades: dict[str, list[float]]) -> dict[str, dict]:
    limits = {}
    for code, values in grades.items():
        cap = _grade_cap(code)
        kept = np.asarray([v for v in values if np.isfinite(v) and v <= cap], dtype=np.float64)
        if kept.size == 0:
            limits[code] = {"n": 0, "median": None, "std": None, "limit": None}
            continue
        std = float(kept.std(ddof=1)) if kept.size > 1 else 0.0
        median = float(np.median(kept))
        limits[code] = {
            "n": int(kept.size),
            "median": round(median, 3),
            "std": round(std, 3),
            "limit": round(median + 3.0 * std, 3),
        }
    return limits


def _write(folder: Path, layers: dict[str, gpd.GeoDataFrame]) -> Path:
    dest = None
    for name in ("check_grade.gpkg", "check_grade_next.gpkg", "check_grade_change.gpkg", "check_grade_recover.gpkg"):
        candidate = folder / name
        if not candidate.exists():
            dest = candidate
            break
        try:
            candidate.unlink()
            dest = candidate
            break
        except PermissionError:
            print(f"{name} is open, trying the next file", flush=True)
    if dest is None:
        raise SystemExit("close the open check-grade geopackages and run again")
    first = True
    for name, frame in layers.items():
        if frame.empty:
            continue
        frame.to_file(dest, layer=name, driver="GPKG", mode="w" if first else "a")
        first = False
    if first:
        raise SystemExit("no check intervals")
    return dest


def _print_stats(label: str, limits: dict, triple_stats: dict, pair_stats: dict) -> None:
    for code, row in sorted(limits.items()):
        print(
            f"{label} grade {code}: n {row['n']} median {row['median']} std {row['std']} limit {row['limit']}",
            flush=True,
        )
    for code in sorted(set(triple_stats) | set(pair_stats)):
        triple = triple_stats.get(code) or _dist([])
        pair = pair_stats.get(code) or _dist([])
        pair_limit = None if triple["limit"] is None else round(2.0 * triple["limit"], 3)
        print(
            f"{label} change {code}: triples n {triple['n']} median {triple['median']} std {triple['std']} "
            f"p90 {triple['p90']} p99 {triple['p99']} max {triple['max']} limit {triple['limit']} "
            f"pair limit {pair_limit}; pairs n {pair['n']} median {pair['median']} "
            f"p90 {pair['p90']} p99 {pair['p99']} max {pair['max']}",
            flush=True,
        )


def _run_tests(items: dict[str, dict]) -> tuple[dict, dict, dict]:
    """Grade and slope change for intervals currently marked included."""
    pool: dict[str, list[float]] = defaultdict(list)
    for item in items.values():
        included = item["included"]
        if not any(included):
            continue
        for values in item["grades"].values():
            pool[item["code"]].extend(value for value, keep in zip(values, included) if keep)
    limits = _class_limits(pool)
    triple_pool: dict[str, list[float]] = defaultdict(list)
    pair_pool: dict[str, list[float]] = defaultdict(list)
    for item in items.values():
        included = item["included"]
        limit = (limits.get(item["code"]) or {"limit": None})["limit"]
        cap = _grade_cap(item["code"])
        state = {}
        for key, slopes in item["grades"].items():
            water = item["water"][key]
            passed = [
                bool(included[i]) and _reason(slope, limit, water=water[i], cap=cap) == ""
                for i, slope in enumerate(slopes)
            ]
            kind, value = _change_values(slopes, passed)
            state[key] = (slopes, passed, kind, value)
            for one_kind, one_value, keep in zip(kind, value, included):
                if not keep:
                    continue
                if one_kind == "triple":
                    triple_pool[item["code"]].append(one_value)
                elif one_kind == "pair":
                    pair_pool[item["code"]].append(one_value)
        item["state"] = state
    triple_stats = {code: _dist(values) for code, values in triple_pool.items()}
    pair_stats = {code: _dist(values) for code, values in pair_pool.items()}
    return limits, triple_stats, pair_stats


def _interval_alive(item: dict, index: int, limits: dict, triple_stats: dict) -> bool:
    if not item["included"][index]:
        return False
    limit = (limits.get(item["code"]) or {"limit": None})["limit"]
    cap = _grade_cap(item["code"])
    change_limit = triple_stats.get(item["code"], {}).get("limit")
    alive = False
    for key, slopes in item["grades"].items():
        _slopes, _passed, kind, value = item["state"][key]
        grade = _reason(slopes[index], limit, water=item["water"][key][index], cap=cap)
        change = "slope_change" if _change_fail(kind[index], value[index], change_limit) else ""
        if grade == "" and change == "":
            alive = True
    return alive


def _chain_rows(chain: list[tuple[str, bool]], items: dict[str, dict], limits: dict, triple_stats: dict) -> list[dict]:
    rows = []
    base = 0.0
    for lid, forward in chain:
        item = items[lid]
        stations = item["sampled"]["s"]
        height = item["sampled"]["z"]
        length = float(item["line"].length)
        n = len(item["included"])
        order = range(n) if forward else range(n - 1, -1, -1)
        for i in order:
            xy = item["sampled"]
            if forward:
                s0 = base + float(stations[i])
                s1 = base + float(stations[i + 1])
                z0 = float(height[i])
                z1 = float(height[i + 1])
                p0 = (float(xy["x"][i]), float(xy["y"][i]))
                p1 = (float(xy["x"][i + 1]), float(xy["y"][i + 1]))
            else:
                s0 = base + (length - float(stations[i + 1]))
                s1 = base + (length - float(stations[i]))
                z0 = float(height[i + 1])
                z1 = float(height[i])
                p0 = (float(xy["x"][i + 1]), float(xy["y"][i + 1]))
                p1 = (float(xy["x"][i]), float(xy["y"][i]))
            rows.append(
                {
                    "id": lid,
                    "i": i,
                    "s0": s0,
                    "s1": s1,
                    "z0": z0,
                    "z1": z1,
                    "p0": p0,
                    "p1": p1,
                    "ok": _interval_alive(item, i, limits, triple_stats),
                    "structure": item["structure"][i],
                }
            )
        base += length
    return rows


def _group_spans(rows: list[dict]) -> list[tuple[int, int, float, float, list[tuple[float, float]]]]:
    spans = []
    start = None
    for i, row in enumerate(rows + [None]):
        good = row is not None and row["ok"]
        if good and start is None:
            start = i
        if not good and start is not None:
            if i - start >= GROUP_MIN:
                knots = [(rows[start]["s0"], rows[start]["z0"])]
                for part in rows[start:i]:
                    knots.append((part["s1"], part["z1"]))
                spans.append((start, i, rows[start]["s0"], rows[i - 1]["s1"], knots))
            start = None
    return spans


def _facing_knot(knots: list[tuple[float, float]], *, last: bool) -> tuple[float, float] | None:
    order = range(len(knots) - 1, -1, -1) if last else range(len(knots))
    for index in order:
        station, height = knots[index]
        if np.isfinite(height):
            return station, float(height)
    return None


def _drop_steep_groups(chains: list[list[tuple[str, bool]]], items: dict[str, dict], limits: dict, triple_stats: dict) -> tuple[int, int]:
    """Invalidate the shorter group wherever the chord between two groups is too steep."""
    chosen: dict[tuple[str, int], None] = {}
    group_ids: set[tuple[int, float]] = set()
    for chain_i, chain in enumerate(chains):
        rows = _chain_rows(chain, items, limits, triple_stats)
        spans = _group_spans(rows)
        for left, right in zip(spans, spans[1:]):
            edge_lo = _facing_knot(left[4], last=True)
            edge_hi = _facing_knot(right[4], last=False)
            if edge_lo is None or edge_hi is None:
                continue
            span = edge_hi[0] - edge_lo[0]
            if span <= 1e-3:
                continue
            slope = 100.0 * abs(edge_hi[1] - edge_lo[1]) / span
            codes = set()
            for index in range(left[0], left[1]):
                codes.add(items[rows[index]["id"]]["code"])
            for index in range(right[0], right[1]):
                codes.add(items[rows[index]["id"]]["code"])
            known = [SLOPE_LIMIT_PCT[code] for code in codes if code in SLOPE_LIMIT_PCT]
            if not known or slope < min(known):
                continue
            len_lo = left[3] - left[2]
            len_hi = right[3] - right[2]
            victims = []
            if len_lo <= len_hi + 1e-3 and len_lo <= GROUP_KEEP_M:
                victims.append(left)
            if len_hi <= len_lo + 1e-3 and len_hi <= GROUP_KEEP_M:
                victims.append(right)
            for victim in victims:
                group_ids.add((chain_i, victim[2]))
                for row in rows[victim[0] : victim[1]]:
                    chosen[(row["id"], row["i"])] = None
    for lid, index in chosen:
        item = items[lid]
        item["included"][index] = False
        item["recovered"][index] = False
        item["drop_reason"][index] = "steep_chord"
    return len(chosen), len(group_ids)


def _settle(chains, items, limits, triple_stats, pair_stats, label: str):
    """Drop false groups on steep chords and rebuild the class figures."""
    for _ in range(24):
        n_interval, n_group = _drop_steep_groups(chains, items, limits, triple_stats)
        if n_group == 0:
            return limits, triple_stats, pair_stats
        print(f"{label}: steep chord dropped {n_interval} intervals in {n_group} groups", flush=True)
        limits, triple_stats, pair_stats = _run_tests(items)
    print(f"{label}: steep chords remain after 24 passes", flush=True)
    return limits, triple_stats, pair_stats


def _group_line(part: list[dict]) -> LineString | None:
    coords = []
    for row in part:
        if not coords or coords[-1] != row["p0"]:
            coords.append(row["p0"])
        coords.append(row["p1"])
    if len(coords) < 2:
        return None
    return LineString(coords)


def _end_against_spline(station: float, height: float, predict, edge_before: float, edge_after: float) -> tuple[str, float, float]:
    dist = min(abs(station - edge_before), abs(edge_after - station))
    predicted = float(predict(station))
    if not np.isfinite(height) or not np.isfinite(predicted):
        return "no_ground", float("nan"), float("nan")
    miss = abs(height - predicted)
    ratio = miss / dist if dist > 1e-6 else 0.0
    if miss > SPLINE_MISS_M:
        return "off_spline_1m", miss, ratio
    if ratio >= SPLINE_RATIO:
        return "off_spline_10pct", miss, ratio
    return "", miss, ratio


def _spline_reason(reasons: list[str]) -> str:
    if "off_spline_1m" in reasons:
        return "off_spline_1m"
    if "off_spline_10pct" in reasons:
        return "off_spline_10pct"
    return "no_ground"


def _recover(chains: list[list[tuple[str, bool]]], items: dict[str, dict], limits: dict, triple_stats: dict) -> tuple[int, list[dict]]:
    """Accept structure intervals that sit on the spline between two groups."""
    gained = 0
    groups = []
    for chain_i, chain in enumerate(chains):
        rows = _chain_rows(chain, items, limits, triple_stats)
        spans = _group_spans(rows)
        for group_i, (start, stop, lo, hi, _knots) in enumerate(spans):
            part = rows[start:stop]
            geom = _group_line(part)
            if geom is None:
                continue
            streets = []
            link_ids = []
            for row in part:
                if not link_ids or link_ids[-1] != row["id"]:
                    link_ids.append(row["id"])
                street = items[row["id"]]["street"]
                if street and (not streets or streets[-1] != street):
                    streets.append(street)
            groups.append(
                {
                    "chain": chain_i,
                    "group_i": group_i,
                    "n_interval": stop - start,
                    "length_m": round(hi - lo, 1),
                    "n_link": len(link_ids),
                    "street_code": ",".join(streets),
                    "class_code": items[part[0]["id"]]["code"],
                    "geometry": geom,
                }
            )
        for row in rows:
            if not row["structure"] or row["ok"]:
                continue
            before = None
            after = None
            for _start, _stop, lo, hi, knots in spans:
                if hi <= row["s0"] + 1e-6:
                    before = (hi, knots)
                elif lo >= row["s1"] - 1e-6 and after is None:
                    after = (lo, knots)
                    break
            item = items[row["id"]]
            if before is None or after is None:
                item["included"][row["i"]] = False
                item["recovered"][row["i"]] = False
                item["spline_reason"][row["i"]] = "no_group"
                item["spline_miss"][row["i"]] = None
                item["spline_ratio"][row["i"]] = None
                continue
            knots = []
            for station, height in before[1] + after[1]:
                if not np.isfinite(height):
                    continue
                if knots and station <= knots[-1][0] + 1e-3:
                    continue
                knots.append((station, height))
            if len(knots) < 2:
                item["included"][row["i"]] = False
                item["recovered"][row["i"]] = False
                item["spline_reason"][row["i"]] = "no_group"
                continue
            predict = CubicSpline([knot[0] for knot in knots], [knot[1] for knot in knots], extrapolate=False)
            ends = (
                _end_against_spline(row["s0"], row["z0"], predict, before[0], after[0]),
                _end_against_spline(row["s1"], row["z1"], predict, before[0], after[0]),
            )
            misses = [one[1] for one in ends if np.isfinite(one[1])]
            ratios = [one[2] for one in ends if np.isfinite(one[2])]
            item["spline_miss"][row["i"]] = None if not misses else round(max(misses), 3)
            item["spline_ratio"][row["i"]] = None if not ratios else round(max(ratios), 3)
            failed = [one[0] for one in ends if one[0]]
            if failed:
                item["included"][row["i"]] = False
                item["recovered"][row["i"]] = False
                item["spline_reason"][row["i"]] = _spline_reason(failed)
                continue
            if not item["included"][row["i"]]:
                gained += 1
            item["included"][row["i"]] = True
            item["recovered"][row["i"]] = True
            item["spline_reason"][row["i"]] = ""
    return gained, groups


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    terrain_path = proc / "dgm_link15" / "dgm_link15.tif"
    if not terrain_path.is_file():
        raise SystemExit(f"missing link buffer terrain {terrain_path}; run tools/mask_dgm_by_links.py")
    print(f"terrain {terrain_path}", flush=True)
    elev, spec = _load_terrain(terrain_path)
    grid, swiss = _load_water(site)
    manual = _manual_bridges()
    links = gpd.read_file(gpkg_path(site), layer="link")
    nodes = gpd.read_file(gpkg_path(site), layer="node")
    structure_pcts = _structure_pcts(site)
    print(f"links {len(links)}", flush=True)

    items: dict[str, dict] = {}
    for rec in links.itertuples(index=False):
        line = _line(rec.geometry)
        if line is None:
            continue
        sampled = _samples(line, elev, spec, grid, swiss)
        if sampled is None:
            continue
        code = _code(int(rec.fc))
        step = np.hypot(np.diff(sampled["x"]), np.diff(sampled["y"]))
        center_slopes, center_water = _line_grade(sampled["z"], sampled["water"], step)
        grades = {"center": center_slopes}
        water = {"center": center_water}
        for side in SIDE_M:
            step_l = np.hypot(np.diff(sampled[f"xl_{side}"]), np.diff(sampled[f"yl_{side}"]))
            step_r = np.hypot(np.diff(sampled[f"xr_{side}"]), np.diff(sampled[f"yr_{side}"]))
            grades[f"left_{side}"], water[f"left_{side}"] = _line_grade(
                sampled[f"zl_{side}"], sampled[f"wl_{side}"], step_l
            )
            grades[f"right_{side}"], water[f"right_{side}"] = _line_grade(
                sampled[f"zr_{side}"], sampled[f"wr_{side}"], step_r
            )
        n_step = len(center_slopes)
        lid = str(rec.link_id)
        labels = _label_ranges(lid, float(line.length), structure_pcts, manual)
        structure = [
            _in_label(float(sampled["s"][i]), float(sampled["s"][i + 1]), labels)
            for i in range(n_step)
        ]
        items[lid] = {
            "id": lid,
            "rec": rec,
            "line": line,
            "code": code,
            "fc": int(rec.fc),
            "street": _text(rec.street_code),
            "sampled": sampled,
            "grades": grades,
            "water": water,
            "structure": structure,
            "included": [not flag for flag in structure],
            "recovered": [False] * n_step,
            "spline_reason": [""] * n_step,
            "drop_reason": [""] * n_step,
            "spline_miss": [None] * n_step,
            "spline_ratio": [None] * n_step,
        }

    ends = _ends(list(items.values()), nodes)
    chains = _chains(list(items.values()), ends)
    limits, triple_stats, pair_stats = _run_tests(items)
    limits, triple_stats, pair_stats = _settle(chains, items, limits, triple_stats, pair_stats, "pass 1")
    _print_stats("pass 1", limits, triple_stats, pair_stats)
    valid_groups: list[dict] = []
    for number in (1, 2):
        gained, valid_groups = _recover(chains, items, limits, triple_stats)
        print(f"spline {number}: recovered {gained} intervals, groups {len(valid_groups)}", flush=True)
        limits, triple_stats, pair_stats = _run_tests(items)
        limits, triple_stats, pair_stats = _settle(
            chains, items, limits, triple_stats, pair_stats, f"pass {number + 1}"
        )
        _print_stats(f"pass {number + 1}", limits, triple_stats, pair_stats)

    interval_rows = []
    link_rows = []
    offset = SIDE_M[0]
    left_key = f"left_{offset}"
    right_key = f"right_{offset}"
    for item in items.values():
        rec = item["rec"]
        line = item["line"]
        code = item["code"]
        sampled = item["sampled"]
        state = item["state"]
        water_flags = item["water"]
        grade_limit = (limits.get(code) or {"limit": None})["limit"]
        cap = _grade_cap(code)
        change_limit = triple_stats.get(code, {}).get("limit")
        center_slopes, _, center_kind, center_value = state["center"]
        left_slopes, _, left_kind, left_value = state[left_key]
        right_slopes, _, right_kind, right_value = state[right_key]
        n_bad = 0
        n_change = 0
        n_center = 0
        n_left = 0
        n_right = 0
        n_recovered = 0
        for i, slope in enumerate(center_slopes):
            left = left_slopes[i]
            right = right_slopes[i]
            grade_reason = ""
            if item["drop_reason"][i]:
                held = item["drop_reason"][i]
                center_reason = left_reason = right_reason = held
                interval_reason = held
                center_change = left_change = right_change = ""
            elif not item["included"][i]:
                held = item["spline_reason"][i] or "no_group"
                center_reason = left_reason = right_reason = held
                interval_reason = held
                center_change = left_change = right_change = ""
            else:
                center_reason = _reason(slope, grade_limit, water=water_flags["center"][i], cap=cap)
                left_reason = _reason(left, grade_limit, water=water_flags[left_key][i], cap=cap)
                right_reason = _reason(right, grade_limit, water=water_flags[right_key][i], cap=cap)
                grade_reason = _interval_reason(center_reason, left_reason, right_reason)
                center_change = "slope_change" if _change_fail(center_kind[i], center_value[i], change_limit) else ""
                left_change = "slope_change" if _change_fail(left_kind[i], left_value[i], change_limit) else ""
                right_change = "slope_change" if _change_fail(right_kind[i], right_value[i], change_limit) else ""
                alive = any(
                    grade == "" and change == ""
                    for grade, change in (
                        (center_reason, center_change),
                        (left_reason, left_change),
                        (right_reason, right_change),
                    )
                )
                if alive:
                    interval_reason = ""
                elif any(change == "slope_change" for change in (center_change, left_change, right_change)):
                    interval_reason = "slope_change"
                else:
                    interval_reason = grade_reason
            if interval_reason:
                n_bad += 1
            if item["included"][i] and grade_reason == "" and interval_reason:
                n_change += 1
            if item["recovered"][i]:
                n_recovered += 1
            if center_reason:
                n_center += 1
            if left_reason:
                n_left += 1
            if right_reason:
                n_right += 1
            interval_rows.append(
                {
                    "link_id": rec.link_id,
                    "short_id": int(rec.short_id),
                    "street_code": _text(rec.street_code),
                    "name": _text(rec.name),
                    "class_code": code,
                    "fc_name": _text(rec.fc_name),
                    "s0_m": round(float(sampled["s"][i]), 2),
                    "s1_m": round(float(sampled["s"][i + 1]), 2),
                    "side_m": offset,
                    "z0_m": None if not np.isfinite(sampled["z"][i]) else round(float(sampled["z"][i]), 3),
                    "z1_m": None if not np.isfinite(sampled["z"][i + 1]) else round(float(sampled["z"][i + 1]), 3),
                    "slope_pct": None if not np.isfinite(slope) else round(float(slope), 2),
                    "ok": 0 if interval_reason else 1,
                    "reason": interval_reason,
                    "structure": 1 if item["structure"][i] else 0,
                    "recovered": 1 if item["recovered"][i] else 0,
                    "spline_miss_m": item["spline_miss"][i],
                    "spline_ratio": item["spline_ratio"][i],
                    "center_ok": 0 if center_reason else 1,
                    "change_center": None if not np.isfinite(center_value[i]) else round(float(center_value[i]), 3),
                    "change_center_kind": center_kind[i],
                    "center_change_ok": -1 if center_kind[i] == "" else (0 if center_change else 1),
                    "z0_left_m": None if not np.isfinite(sampled[f"zl_{offset}"][i]) else round(float(sampled[f"zl_{offset}"][i]), 3),
                    "z1_left_m": None if not np.isfinite(sampled[f"zl_{offset}"][i + 1]) else round(float(sampled[f"zl_{offset}"][i + 1]), 3),
                    "slope_left_pct": None if not np.isfinite(left) else round(float(left), 2),
                    "left_ok": 0 if left_reason else 1,
                    "left_reason": left_reason,
                    "change_left": None if not np.isfinite(left_value[i]) else round(float(left_value[i]), 3),
                    "change_left_kind": left_kind[i],
                    "left_change_ok": -1 if left_kind[i] == "" else (0 if left_change else 1),
                    "z0_right_m": None if not np.isfinite(sampled[f"zr_{offset}"][i]) else round(float(sampled[f"zr_{offset}"][i]), 3),
                    "z1_right_m": None if not np.isfinite(sampled[f"zr_{offset}"][i + 1]) else round(float(sampled[f"zr_{offset}"][i + 1]), 3),
                    "slope_right_pct": None if not np.isfinite(right) else round(float(right), 2),
                    "right_ok": 0 if right_reason else 1,
                    "right_reason": right_reason,
                    "change_right": None if not np.isfinite(right_value[i]) else round(float(right_value[i]), 3),
                    "change_right_kind": right_kind[i],
                    "right_change_ok": -1 if right_kind[i] == "" else (0 if right_change else 1),
                    "geometry": LineString(
                        [(float(sampled["x"][i]), float(sampled["y"][i])), (float(sampled["x"][i + 1]), float(sampled["y"][i + 1]))]
                    ),
                }
            )
        n = len(center_slopes)
        link_rows.append(
            {
                "link_id": rec.link_id,
                "short_id": int(rec.short_id),
                "street_code": _text(rec.street_code),
                "name": _text(rec.name),
                "class_code": code,
                "fc_name": _text(rec.fc_name),
                "n_struct": int(rec.n_struct),
                "structure": 1 if any(item["structure"]) else 0,
                "n_interval": n,
                "n_recovered": n_recovered,
                "n_invalid": n_bad,
                "n_change_invalid": n_change,
                "n_center_invalid": n_center,
                "n_left_invalid": n_left,
                "n_right_invalid": n_right,
                "valid_pct": round(100.0 * (n - n_bad) / n, 1) if n else None,
                "geometry": line,
            }
        )

    crs = links.crs
    intervals = gpd.GeoDataFrame(interval_rows, crs=crs)
    todo = intervals[intervals["ok"] == 0].copy()
    folder = proc / "link_surface" / "check_grade"
    folder.mkdir(parents=True, exist_ok=True)
    dest = _write(
        folder,
        {
            "interval": intervals,
            "todo": todo,
            "link": gpd.GeoDataFrame(link_rows, crs=crs),
            "valid_group": gpd.GeoDataFrame(valid_groups, crs=crs),
        },
    )
    pd.DataFrame(
        [{"class_code": code, **row} for code, row in sorted(limits.items())]
    ).to_csv(folder / "class_grade.csv", index=False)
    change_rows = []
    for code in sorted(set(triple_stats) | set(pair_stats)):
        triple = triple_stats.get(code) or _dist([])
        pair = pair_stats.get(code) or _dist([])
        change_rows.append(
            {
                "class_code": code,
                "triple_n": triple["n"],
                "triple_median": triple["median"],
                "triple_std": triple["std"],
                "triple_p90": triple["p90"],
                "triple_p95": triple["p95"],
                "triple_p99": triple["p99"],
                "triple_max": triple["max"],
                "triple_limit": triple["limit"],
                "pair_limit": None if triple["limit"] is None else round(2.0 * triple["limit"], 3),
                "pair_n": pair["n"],
                "pair_median": pair["median"],
                "pair_p90": pair["p90"],
                "pair_p95": pair["p95"],
                "pair_p99": pair["p99"],
                "pair_max": pair["max"],
            }
        )
    pd.DataFrame(change_rows).to_csv(folder / "class_slope_change.csv", index=False)

    n = len(intervals)
    n_bad = int((intervals["ok"] == 0).sum())
    n_change = int((intervals["reason"] == "slope_change").sum())
    n_recovered = int((intervals["recovered"] == 1).sum())
    line_change = int(
        ((intervals["center_change_ok"] == 0) | (intervals["left_change_ok"] == 0) | (intervals["right_change_ok"] == 0)).sum()
    )
    print(
        f"intervals {n} invalid {n_bad} ({100.0 * n_bad / n:.1f}%), "
        f"recovered {n_recovered}, "
        f"slope change removes {n_change} intervals and flags a line on {line_change}",
        flush=True,
    )
    print(intervals.loc[intervals["ok"] == 0, "reason"].value_counts().to_string(), flush=True)
    print(f"valid groups {len(valid_groups)} in layer valid_group", flush=True)
    print(
        "(open the following in qgis or similar tool to check: "
        f"{dest}, {folder / 'class_grade.csv'}, {folder / 'class_slope_change.csv'})",
        flush=True,
    )


if __name__ == "__main__":
    main()
