"""One height per at-grade GIP routing node, plus a ride check along the links.

The routing graph already stores one point where roads meet at grade. Each
Verkehrsweg piece still takes its own DGM sample, so the two ends that meet
there keep two heights. This script fits one profile over the links in the
site: ends that share a node share one unknown, and where two links leave a
node nearly straight the smoothness runs through the node.

Bridge, tunnel and gallery spans do not pull toward the DGM. Each bridge is
measured against the straight line through its two outer points. The ground
is suspect when it hangs more than ``SUSPECT_DIP_M`` below that line, or when
the fall to the low point is steeper than ``SUSPECT_GRADE``. A suspect span
stays on the line between the open stations outside it. Anything shallower
is treated as a culvert and keeps the ground. That is a stand-in where no
deck survey exists. Every other structure span is held to the same straight
line, across links that meet inside the bore. A piece with no DGM cover is
held to the routing height. The per-bridge numbers land in ``bridge_review``.
Where a gallery or a tunnel covers the same stations as a bridge, the
decision for that link is read from ``structure_choices.json``. A link with
no row there is asked once, and the answer is written before the next
question. A later run uses the file and does not ask again. ``structure``
drops the shared stations from the bridge; the bridge check then runs on
the part that remains. ``bridge`` leaves the shared stations on the bridge.

Nothing is written into a level, a mesh or a heightmap. The fit and the ride
numbers land in ``processed/<site>/gip_routing/``. ``node_heights.gpkg`` is one
height per node. ``link_profile.gpkg`` is the fitted profile along each link.
Heights are sampled from ``corridor50_raw.tif`` when that mosaic exists,
otherwise from the site DGM (native cell size, frame taken from its
``.meta.json``).

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\junction_node_heights.py

    cd C:\\temp\\beamng_autoroad; python tools\\junction_node_heights.py --self-check
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
import tifffile as tiff
import scipy.sparse as sp
from scipy.sparse.csgraph import connected_components
from scipy.sparse.linalg import spsolve
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_road_grid import _load_geotiff  # noqa: E402
from gip_routing_load import gpkg_path, load_routing  # noqa: E402
from authorities import resolved_working_crs  # noqa: E402
from site_coords import SiteCoords, load_site, processed_dir  # noqa: E402

STEP_M = 2.0
STRAIGHT_DEG = 145.0
# Ground below the line through the two outer points. Either check is enough
# to call the bridge suspect and keep it off the DGM.
SUSPECT_DIP_M = 1.5
SUSPECT_GRADE = 0.10
SPAN_MIN_M = 8.0
TUKEY = (2.0, 1.0, 0.6, 0.4, 0.3)
W_HOLD = 30.0
# Stations outside the DGM keep the routing height. A ridge alone pulls
# those chains toward zero and the profile runs away.
W_MISS = 0.25
# Structure stations stay on the line between the two portal unknowns.
W_CHORD = 1.0e5
END_SNAP_M = 2.5
GRADE_M = 6.0


def _lc(fc: int) -> float:
    """Smoothing wavelength in metres for a GIP functional class."""
    if fc <= 1:
        return 60.0
    if fc == 2:
        return 50.0
    if fc == 3:
        return 45.0
    if fc == 4:
        return 38.0
    if fc in (5, 6):
        return 28.0
    if fc in (7, 8):
        return 22.0
    if fc >= 10:
        return 14.0
    return 20.0


def _alpha(lc: float) -> float:
    return (lc / (2.0 * math.pi)) ** 4


def _span_kind(name: str) -> str:
    s = str(name or "").casefold()
    if "brücke" in s or "bruecke" in s:
        return "bridge"
    if "tunnel" in s:
        return "tunnel"
    if "galerie" in s:
        return "gallery"
    if "unterführ" in s or "unterfuehr" in s:
        return "under"
    return ""


def _sample(z: np.ndarray, spec: dict, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    res = float(spec["resolution_m"])
    col = (x - spec["xmin"]) / res - 0.5
    row = (spec["ymax"] - y) / res - 0.5
    c0 = np.floor(col).astype(np.int64)
    r0 = np.floor(row).astype(np.int64)
    h, w = z.shape
    ok = (c0 >= 0) & (r0 >= 0) & (c0 + 1 < w) & (r0 + 1 < h)
    out = np.full(len(x), np.nan, dtype=np.float64)
    if not np.any(ok):
        return out
    tc = col[ok] - c0[ok]
    tr = row[ok] - r0[ok]
    a = z[r0[ok], c0[ok]]
    b = z[r0[ok], c0[ok] + 1]
    c = z[r0[ok] + 1, c0[ok]]
    d = z[r0[ok] + 1, c0[ok] + 1]
    val = a * (1 - tc) * (1 - tr) + b * tc * (1 - tr) + c * (1 - tc) * tr + d * tc * tr
    finite = np.isfinite(val)
    idx = np.flatnonzero(ok)
    out[idx[finite]] = val[finite]
    return out


def _chain(geom, step: float) -> tuple[np.ndarray, np.ndarray] | None:
    seg = shapely.segmentize(shapely.force_2d(geom), step)
    xy = np.asarray(shapely.get_coordinates(seg), dtype=np.float64)
    if len(xy) < 2:
        return None
    ds = np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1]))
    keep = np.r_[True, ds > 0.05]
    xy = xy[keep]
    if len(xy) < 2:
        return None
    s = np.r_[0.0, np.cumsum(np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1])))]
    return xy, s


def _span_records(rec: dict, s: np.ndarray) -> list[dict]:
    """Structure pieces on this link that are long enough to count."""
    length = float(s[-1]) if len(s) else 0.0
    out = []
    if length <= 0:
        return out
    for st in rec.get("structures") or []:
        tag = _span_kind(st.get("type") or "")
        if not tag:
            continue
        a = float(st.get("from_pct") or 0.0)
        b = float(st.get("to_pct") or 0.0)
        lo, hi = (a, b) if a <= b else (b, a)
        lo = min(max(lo, 0.0), 100.0)
        hi = min(max(hi, 0.0), 100.0)
        if (hi - lo) / 100.0 * length < SPAN_MIN_M:
            continue
        on = (s >= lo / 100.0 * length) & (s <= hi / 100.0 * length)
        out.append(
            {
                "id": str(st.get("id") or ""),
                "tag": tag,
                "name": st.get("name") or "",
                "from_pct": lo,
                "to_pct": hi,
                "on": on,
            }
        )
    return out


def _mask_spans(rec: dict, s: np.ndarray) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """Per station: kind, bridge id, and every counted structure piece."""
    kind = np.full(len(s), "", dtype=object)
    bridge_id = np.full(len(s), "", dtype=object)
    spans = _span_records(rec, s)
    for spn in spans:
        kind[spn["on"]] = spn["tag"]
        if spn["tag"] == "bridge":
            bridge_id[spn["on"]] = spn["id"]
    return kind, bridge_id, spans


def _load_site_dgm(site: dict) -> tuple[np.ndarray, dict]:
    """Corridor mosaic, or the site DGM when that mosaic was never built."""
    raw = processed_dir(site) / "corridor50_raw" / "corridor50_raw.tif"
    if raw.is_file():
        print(f"DGM {raw}", flush=True)
        return _load_geotiff(raw)
    from wcs_terrain import native_cache_path, terrain_cache_path

    level = terrain_cache_path(site, "dgm", default_stem="dgm")
    native = native_cache_path(level)
    path = native if native.is_file() else level
    meta_path = path.with_suffix(".meta.json")
    if not path.is_file() or not meta_path.is_file():
        raise SystemExit(f"missing {raw} and no site DGM at {level}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    z = np.asarray(tiff.imread(path), dtype=np.float32)
    if z.ndim == 3:
        z = z[..., 0]
    bad = (~np.isfinite(z)) | (z <= -9999.0) | (z < 50.0) | (z > 4500.0)
    if np.any(bad):
        z = z.copy()
        z[bad] = np.nan
    xmin, ymin, xmax, ymax = (float(v) for v in meta["bbox"])
    res = float(meta["resolution_m"])
    height, width = z.shape
    if abs((xmax - xmin) - width * res) > res or abs((ymax - ymin) - height * res) > res:
        raise SystemExit(
            f"{path.name} is {width}x{height} at {res} m, "
            f"bbox spans {xmax - xmin:.1f} x {ymax - ymin:.1f} m"
        )
    print(
        f"DGM {path} ({width}x{height} @ {res} m, no corridor mosaic)",
        flush=True,
    )
    return z, {
        "raster": path.name,
        "source": str(path),
        "xmin": xmin,
        "ymin": ymin,
        "xmax": xmax,
        "ymax": ymax,
        "width": int(width),
        "height": int(height),
        "resolution_m": res,
    }


def _build_arms(site: dict, step: float, *, ask_overlaps: bool = False) -> tuple[list[dict], list[dict]]:
    topo = load_routing(site)
    links = gpd.read_file(gpkg_path(site), layer="link")
    geom_of = {str(r.link_id): r.geometry for r in links.itertuples(index=False)}
    z, spec = _load_site_dgm(site)
    nodes = topo["nodes"]
    arms = []
    missed = 0
    for lid, rec in topo["links"].items():
        geom = geom_of.get(lid)
        if geom is None or geom.is_empty:
            missed += 1
            continue
        chained = _chain(geom, step)
        if chained is None:
            missed += 1
            continue
        xy, s = chained
        d = _sample(z, spec, xy[:, 0], xy[:, 1])
        kind, bridge_id, spans = _mask_spans(rec, s)
        w = np.where(np.isfinite(d), 1.0, 0.0)
        w[kind != ""] = 0.0
        nf, nt = rec["node_from"], rec["node_to"]
        na, nb = nodes.get(nf) or {}, nodes.get(nt) or {}
        arms.append(
            {
                "id": lid,
                "s": s,
                "xy": xy,
                "d": d,
                "w": w,
                "kind": kind,
                "bridge_id": bridge_id,
                "spans": spans,
                "node_from": nf,
                "node_to": nt,
                "level_from": float(rec.get("level_from") or 0.0),
                "level_to": float(rec.get("level_to") or 0.0),
                "z_gip_from": float(na.get("z") or np.nan),
                "z_gip_to": float(nb.get("z") or np.nan),
                "lc": _lc(int(rec.get("fc") or 8)),
                "name": rec.get("name") or rec.get("street_code") or "",
                "street_code": rec.get("street_code") or "",
                "width_m": float(rec["width_m"]) if rec.get("width_m") not in (None, "") else None,
                "fc": int(rec.get("fc") or -1),
            }
        )
    print(
        f"links {len(arms)} (no geometry {missed}), "
        f"stations {sum(len(a['s']) for a in arms)}, "
        f"with DGM {sum(int(np.any(a['w'] > 0)) for a in arms)}",
        flush=True,
    )
    del z
    reviews = _assess_bridges(arms)
    n_suspect = sum(1 for r in reviews if r["suspect"])
    print(f"bridges {len(reviews)}, suspect {n_suspect}", flush=True)
    reviews = _resolve_overlaps(
        arms,
        reviews,
        processed_dir(site) / "gip_routing" / "structure_choices.json",
        ask_overlaps,
        _inspect_lines(site),
    )
    return arms, reviews


def _overlap_pairs(arms: list[dict]) -> dict[str, dict]:
    """Gallery or tunnel stations that are also marked as a bridge."""
    pairs: dict[str, dict] = {}
    for arm in arms:
        spans = arm.get("spans") or []
        for i, left in enumerate(spans):
            for right in spans[i + 1 :]:
                tags = {left["tag"], right["tag"]}
                if "bridge" not in tags or not ({"gallery", "tunnel"} & tags):
                    if tags == {"tunnel"} and left["id"] != right["id"]:
                        both = left["on"] & right["on"]
                        if int(both.sum()) >= 2:
                            print(
                                f"note: tunnels {left['id'][:8]} and {right['id'][:8]} "
                                "overlap. Both stay off the ground.",
                                flush=True,
                            )
                    continue
                both = left["on"] & right["on"]
                idx = np.flatnonzero(both)
                if len(idx) < 2:
                    continue
                bridge = left if left["tag"] == "bridge" else right
                cover = right if left["tag"] == "bridge" else left
                link_id = str(arm.get("id") or "")
                key = "|".join((link_id, *sorted((bridge["id"], cover["id"]))))
                rec = pairs.setdefault(
                    key,
                    {
                        "link_id": link_id,
                        "bridge_id": bridge["id"],
                        "cover_id": cover["id"],
                        "cover_tag": cover["tag"],
                        "bridge_name": bridge["name"],
                        "cover_name": cover["name"],
                        "street": arm.get("street_code") or "",
                        "metres": 0.0,
                        "places": [],
                    },
                )
                rec["metres"] += float(arm["s"][idx[-1]] - arm["s"][idx[0]])
                rec["street"] = rec["street"] or (arm.get("street_code") or "")
                rec["places"].append((arm, bridge, cover, both))
    return pairs


def _stored_choice(raw) -> str | None:
    if isinstance(raw, dict):
        raw = raw.get("choice")
    if raw in ("structure", "bridge"):
        return str(raw)
    return None


def _save_choices(path: Path, choices: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(choices, indent=2, ensure_ascii=False), encoding="utf-8")


def _apply_overlap(info: dict, choice: str) -> None:
    """``structure`` cuts the bridge back to the stations outside the overlap."""
    for arm, bridge, cover, both in info["places"]:
        if choice == "bridge":
            arm["kind"][both] = "bridge"
            arm["bridge_id"][both] = bridge["id"]
            continue
        bridge["on"] = np.asarray(bridge["on"]) & ~np.asarray(both)
        arm["kind"][both] = cover["tag"]
        arm["bridge_id"][both] = ""
        arm["w"][both] = 0.0


def _outside_pct(info: dict) -> str:
    """Percent ranges of the bridge that are not also the gallery or tunnel."""
    bits = []
    for _arm, bridge, cover, _both in info["places"]:
        if "from_pct" not in bridge or "from_pct" not in cover:
            continue
        lo, hi = sorted((float(bridge["from_pct"]), float(bridge["to_pct"])))
        c0, c1 = sorted((float(cover["from_pct"]), float(cover["to_pct"])))
        parts = []
        if c0 > lo + 0.05:
            parts.append(f"{lo:.1f}–{min(hi, c0):.1f}%")
        if c1 < hi - 0.05:
            parts.append(f"{max(lo, c1):.1f}–{hi:.1f}%")
        if parts:
            bits.append(", ".join(parts))
    return "; ".join(bits)


def _ask_overlap(info: dict, review: dict | None, check_paths: list[str]) -> str:
    tag = "gallery" if info["cover_tag"] == "gallery" else "tunnel"
    outside = _outside_pct(info)
    print(flush=True)
    print(
        f"overlap {info['metres']:.0f} m, link {info['link_id'][:8]}, "
        f"{info['street'] or 'no street code'}",
        flush=True,
    )
    print(f"  {tag} {info['cover_id'][:8]}  {info['cover_name']}", flush=True)
    print(f"  bridge {info['bridge_id'][:8]}  {info['bridge_name']}", flush=True)
    if review is None:
        print("  bridge check: no measurement", flush=True)
    elif review.get("decision") == "ground":
        print(
            f"  bridge check: on the ground "
            f"(depth {review.get('depth_m')} m, {review.get('grade_pct')} %)",
            flush=True,
        )
    else:
        print(
            f"  bridge check: suspect, straight line "
            f"({', '.join(review.get('reasons') or [])}, "
            f"depth {review.get('depth_m')} m, {review.get('grade_pct')} %)",
            flush=True,
        )
    if outside:
        print(f"  s = shared stretch is the {tag}; the bridge keeps {outside}", flush=True)
    else:
        print(f"  s = shared stretch is the {tag}; the rest of the bridge stays a bridge", flush=True)
    print("  b = shared stretch stays part of the bridge", flush=True)
    print(
        "(open the following in qgis or similar tool to check: ["
        + ", ".join(check_paths)
        + "])",
        flush=True,
    )
    while True:
        ans = input("choice [s/b]: ").strip().lower()
        if ans in ("s", "b"):
            return "structure" if ans == "s" else "bridge"
        print("Enter s or b.", flush=True)


def _inspect_lines(site: dict) -> list[str]:
    gpkg = processed_dir(site) / "gip_routing" / "gip_routing.gpkg"
    from wcs_terrain import native_cache_path, terrain_cache_path

    level = terrain_cache_path(site, "dgm", default_stem="dgm")
    dgm = level if level.is_file() else native_cache_path(level)
    return [f"{gpkg} (layer bridge_ref)", str(dgm)]


def _resolve_overlaps(
    arms: list[dict],
    reviews: list[dict],
    path: Path,
    ask: bool,
    check_paths: list[str] | None = None,
) -> list[dict]:
    """Use a stored row for the link. Ask only when that row is missing."""
    pairs = _overlap_pairs(arms)
    if not pairs:
        return reviews
    check_paths = check_paths or []
    choices = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    by_id = {row["id"]: row for row in reviews}
    cuts: dict[str, str] = {}
    for key, info in pairs.items():
        review = by_id.get(info["bridge_id"])
        choice = _stored_choice(choices.get(key))
        if choice is None:
            if not ask:
                raise SystemExit(
                    f"link {info['link_id'][:8]} "
                    f"({info['cover_id'][:8]} / {info['bridge_id'][:8]}) "
                    f"has no row in {path}. "
                    "Run the script in a console and it will ask once."
                )
            choice = _ask_overlap(info, review, check_paths)
            choices[key] = {
                "link_id": info["link_id"],
                "street": info["street"],
                "bridge_id": info["bridge_id"],
                "cover_id": info["cover_id"],
                "cover_tag": info["cover_tag"],
                "choice": choice,
            }
            _save_choices(path, choices)
            print(f"saved choice in {path}", flush=True)
        else:
            print(
                f"link {info['link_id'][:8]} "
                f"({info['cover_id'][:8]} / {info['bridge_id'][:8]}): "
                f"choice from file ({choice})",
                flush=True,
            )
        _apply_overlap(info, choice)
        if choice == "structure":
            cuts[info["bridge_id"]] = info["cover_tag"]
    fresh = _assess_bridges(arms)
    for row in fresh:
        if row["id"] in cuts:
            row["overlap_choice"] = cuts[row["id"]]
    return fresh


def _print_interior_ground(arms: list[dict], fit: dict) -> None:
    """Tunnels and bridges whose inner stations still sit on the DGM."""
    print(
        "Inside on the ground (|profile − DGM| median under 1 m, outer 10 m dropped):",
        flush=True,
    )
    n = 0
    seen: set[tuple[str, str]] = set()
    for ai, arm in enumerate(arms):
        z = fit["z"][ai]
        for spn in arm.get("spans") or []:
            if spn["tag"] not in ("tunnel", "bridge"):
                continue
            key = (spn["tag"], spn["id"])
            if key in seen:
                continue
            idx = np.flatnonzero(spn["on"])
            if len(idx) < 3:
                continue
            s0 = float(arm["s"][idx[0]])
            s1 = float(arm["s"][idx[-1]])
            if s1 - s0 > 24.0:
                idx = idx[(arm["s"][idx] >= s0 + 10.0) & (arm["s"][idx] <= s1 - 10.0)]
            if len(idx) < 3:
                continue
            d = arm["d"][idx]
            zz = z[idx]
            ok = np.isfinite(d) & np.isfinite(zz)
            if int(ok.sum()) < 3:
                continue
            diff = np.abs(zz[ok] - d[ok])
            med = float(np.median(diff))
            if med >= 1.0:
                continue
            seen.add(key)
            n += 1
            print(
                f"  {spn['tag']} {spn['id'][:8]} {arm.get('street_code') or '-'} "
                f"{s1 - s0:.0f} m  Median {med:.2f} m",
                flush=True,
            )
    if n == 0:
        print("  none", flush=True)


def _end_key(arm: dict, end: str) -> tuple[str, float]:
    if end == "from":
        return arm["node_from"], round(arm["level_from"], 1)
    return arm["node_to"], round(arm["level_to"], 1)


def _away(arm: dict, end: str) -> np.ndarray | None:
    xy = arm["xy"]
    s = arm["s"]
    if end == "from":
        hit = np.flatnonzero(s >= min(GRADE_M, float(s[-1])))
        j = int(hit[0]) if len(hit) else len(s) - 1
        if j == 0:
            return None
        v = xy[j] - xy[0]
    else:
        hit = np.flatnonzero((s[-1] - s) >= min(GRADE_M, float(s[-1])))
        j = int(hit[-1]) if len(hit) else 0
        if j == len(s) - 1:
            return None
        v = xy[j] - xy[-1]
    n = float(np.hypot(v[0], v[1]))
    if n < 0.5:
        return None
    return v / n


def _find(parent: dict, node: tuple) -> tuple:
    parent.setdefault(node, node)
    while parent[node] != node:
        parent[node] = parent[parent[node]]
        node = parent[node]
    return node


def _union(parent: dict, a: tuple, b: tuple) -> None:
    ra, rb = _find(parent, a), _find(parent, b)
    if ra != rb:
        parent[rb] = ra


def _dists(start: tuple, adj: dict) -> dict[tuple, float]:
    dist = {start: 0.0}
    heap = [(0.0, start)]
    while heap:
        so_far, node = heapq.heappop(heap)
        if so_far != dist.get(node):
            continue
        for nxt, step in adj.get(node, ()):
            cand = so_far + step
            if cand < dist.get(nxt, 1e300):
                dist[nxt] = cand
                heapq.heappush(heap, (cand, nxt))
    return dist


def _assess_bridges(arms: list[dict]) -> list[dict]:
    """Depth of each bridge below the line of its two outer points.

    A span is suspect when that depth exceeds ``SUSPECT_DIP_M`` or the grade
    down to the low point exceeds ``SUSPECT_GRADE``. Suspect spans keep DGM
    weight 0. The others are culverts and take the ground back.
    """
    groups: dict[str, list[tuple]] = defaultdict(list)
    for ai, arm in enumerate(arms):
        ids = arm.get("bridge_id")
        if ids is None:
            continue
        for i, sid in enumerate(ids):
            if sid:
                groups[str(sid)].append((ai, int(i)))
    ends: dict[tuple, list[tuple]] = defaultdict(list)
    for ai, arm in enumerate(arms):
        if len(arm["s"]) == 0:
            continue
        ends[_end_key(arm, "from")].append((ai, 0))
        ends[_end_key(arm, "to")].append((ai, len(arm["s"]) - 1))

    reviews = []
    for sid, members in groups.items():
        parent: dict[tuple, tuple] = {}
        member_set = set(members)
        for ai, i in members:
            if (ai, i + 1) in member_set:
                _union(parent, (ai, i), (ai, i + 1))
            if i not in (0, len(arms[ai]["s"]) - 1):
                continue
            end_name = "from" if i == 0 else "to"
            for other in ends[_end_key(arms[ai], end_name)]:
                if other in member_set and other != (ai, i):
                    _union(parent, (ai, i), other)
        comps: dict[tuple, list[tuple]] = defaultdict(list)
        for pt in members:
            comps[_find(parent, pt)].append(pt)
        for comp in comps.values():
            reviews.append(_review_bridge(arms, sid, comp, ends))
    reviews.sort(key=lambda r: (-int(r["suspect"]), -(r["depth_m"] or 0.0)))
    return reviews


def _review_bridge(arms: list[dict], sid: str, members: list[tuple], ends: dict) -> dict:
    member_set = set(members)
    adj: dict[tuple, list[tuple]] = defaultdict(list)
    street = ""
    for ai, i in members:
        arm = arms[ai]
        street = street or (arm.get("street_code") or "")
        if (ai, i + 1) in member_set:
            step = abs(float(arm["s"][i + 1] - arm["s"][i]))
            adj[(ai, i)].append(((ai, i + 1), step))
            adj[(ai, i + 1)].append(((ai, i), step))
        if i not in (0, len(arm["s"]) - 1):
            continue
        end_name = "from" if i == 0 else "to"
        for other in ends[_end_key(arm, end_name)]:
            if other in member_set and other != (ai, i):
                adj[(ai, i)].append((other, 0.0))
    da0 = _dists(members[0], adj)
    a = max(members, key=lambda p: da0.get(p, -1.0))
    da = _dists(a, adj)
    b = max(members, key=lambda p: da.get(p, -1.0))
    db = _dists(b, adj)
    length = float(da.get(b, 0.0))

    def zd(pt: tuple) -> float:
        return float(arms[pt[0]]["d"][pt[1]])

    za, zb = zd(a), zd(b)
    on_path = []
    for pt in members:
        ua, ub = da.get(pt), db.get(pt)
        if ua is None or ub is None:
            continue
        if length > 0.0 and abs(ua + ub - length) > 0.5:
            continue
        on_path.append(pt)
    depth = 0.0
    run = 0.0
    deep = a
    measured = math.isfinite(za) and math.isfinite(zb) and length >= 1.0
    if measured:
        for pt in on_path:
            dd = zd(pt)
            if not math.isfinite(dd):
                continue
            chord = za + (zb - za) * (da[pt] / length)
            dip = chord - dd
            if dip > depth:
                depth = float(dip)
                deep = pt
                run = float(min(da[pt], db[pt]))
    grade = depth / max(run, 4.0) if measured else 0.0
    reasons: list[str] = []
    if not measured:
        reasons.append("no_outer")
    else:
        if depth > SUSPECT_DIP_M:
            reasons.append("depth")
        if grade > SUSPECT_GRADE:
            reasons.append("grade")
    suspect = bool(reasons)
    if not suspect:
        for ai, i in members:
            if np.isfinite(arms[ai]["d"][i]):
                arms[ai]["w"][i] = 1.0
    dx, dy = arms[deep[0]]["xy"][deep[1]]
    coords = []
    for pt in sorted(on_path or members, key=lambda p: da.get(p, 0.0)):
        zz = zd(pt)
        x, y = arms[pt[0]]["xy"][pt[1]]
        if math.isfinite(zz):
            coords.append((float(x), float(y), float(zz)))
    return {
        "id": sid,
        "street": street,
        "length_m": round(length, 1),
        "depth_m": round(depth, 3) if measured else None,
        "grade_pct": round(100.0 * grade, 2) if measured else None,
        "run_m": round(run, 1) if measured else None,
        "z_outer_a_m": round(za, 2) if math.isfinite(za) else None,
        "z_outer_b_m": round(zb, 2) if math.isfinite(zb) else None,
        "suspect": suspect,
        "reasons": reasons,
        "decision": "chord" if suspect else "ground",
        "x": round(float(dx), 2),
        "y": round(float(dy), 2),
        "coords": coords,
    }


def _append_structure_chords(arms, u_of, rows, cols, vals, weights, r: int):
    """Pin structure stations to the line between the open stations at each end.

    Returns the new row count and one record per pinned station:
    ``(arm, index, unknown_a, unknown_b, weight_a, weight_b)``.
    """
    span = [(np.asarray(arm["kind"], dtype=object) != "") & (np.asarray(arm["w"]) <= 0) for arm in arms]
    parent: dict[tuple, tuple] = {}
    ends: dict[tuple, list[tuple]] = defaultdict(list)
    for ai, arm in enumerate(arms):
        mask = span[ai]
        on = np.flatnonzero(mask)
        for i, j in zip(on[:-1], on[1:]):
            if int(j) == int(i) + 1:
                _union(parent, (ai, int(i)), (ai, int(j)))
        if len(arm["s"]):
            ends[_end_key(arm, "from")].append((ai, 0))
            ends[_end_key(arm, "to")].append((ai, len(arm["s"]) - 1))
    for lst in ends.values():
        inside = [(ai, i) for ai, i in lst if span[ai][i]]
        for other in inside[1:]:
            _union(parent, inside[0], other)

    comps: dict[tuple, list[tuple]] = defaultdict(list)
    for ai, arm in enumerate(arms):
        for i in np.flatnonzero(span[ai]):
            comps[_find(parent, (ai, int(i)))].append((ai, int(i)))

    pinned = []
    for members in comps.values():
        member_set = set(members)
        adj: dict[tuple, list[tuple]] = defaultdict(list)
        portals: list[tuple] = []
        seen_portal: set[tuple] = set()

        def _touch(a: tuple, b: tuple, step: float, portal: bool) -> None:
            if step < 0:
                return
            adj[a].append((b, step))
            adj[b].append((a, step))
            if portal and b not in seen_portal:
                seen_portal.add(b)
                portals.append(b)

        for ai, i in members:
            arm = arms[ai]
            for j, end_name in ((i - 1, None), (i + 1, None)):
                if j < 0 or j >= len(arm["s"]):
                    continue
                step = abs(float(arm["s"][j] - arm["s"][i]))
                if (ai, j) in member_set:
                    if j > i:
                        adj[(ai, i)].append(((ai, j), step))
                        adj[(ai, j)].append(((ai, i), step))
                elif float(arm["w"][j]) > 0:
                    _touch((ai, i), (ai, j), step, True)
            if i not in (0, len(arm["s"]) - 1):
                continue
            end_name = "from" if i == 0 else "to"
            for aj, j in ends[_end_key(arm, end_name)]:
                if (aj, j) == (ai, i):
                    continue
                if (aj, j) in member_set:
                    adj[(ai, i)].append(((aj, j), 0.0))
                elif float(arms[aj]["w"][j]) > 0:
                    _touch((ai, i), (aj, j), 0.0, True)

        if not portals:
            continue
        reach = {p: _dists(p, adj) for p in portals}
        if len(portals) == 1:
            pairs = []
            pa = portals[0]
            for ai, i in members:
                if (ai, i) in reach[pa]:
                    pairs.append((ai, i, pa, pa, 1.0, 0.0))
        else:
            far_a, far_b, far_d = portals[0], portals[1], -1.0
            for a in portals:
                for b in portals:
                    dab = reach[a].get(b, -1.0)
                    if dab > far_d:
                        far_a, far_b, far_d = a, b, dab
            pairs = []
            for ai, i in members:
                da = reach[far_a].get((ai, i))
                db = reach[far_b].get((ai, i))
                if da is None or db is None or da + db <= 1e-6:
                    continue
                pairs.append((ai, i, far_a, far_b, db / (da + db), da / (da + db)))

        for ai, i, pa, pb, fa, fb in pairs:
            ua = int(u_of[pa[0]][pa[1]])
            ub = int(u_of[pb[0]][pb[1]])
            ui = int(u_of[ai][i])
            rows.append(np.array([r, r, r]))
            cols.append(np.array([ui, ua, ub]))
            vals.append(np.array([1.0, -fa, -fb]))
            weights.append(np.array([W_CHORD]))
            pinned.append((ai, i, ua, ub, fa, fb))
            r += 1
    return r, pinned


def fit_arms(arms: list[dict]) -> dict:
    """Shared node unknowns, smoothness through straight junctions."""
    node_u: dict[tuple[str, float], int] = {}
    u_of: list[np.ndarray] = []
    next_u = 0
    for arm in arms:
        n = len(arm["s"])
        uu = np.empty(n, dtype=np.int64)
        kf, kt = _end_key(arm, "from"), _end_key(arm, "to")
        for key in (kf, kt):
            if key not in node_u:
                node_u[key] = next_u
                next_u += 1
        uu[0] = node_u[kf]
        uu[-1] = node_u[kt]
        for i in range(1, n - 1):
            uu[i] = next_u
            next_u += 1
        u_of.append(uu)
    n_unk = next_u

    rows: list[np.ndarray] = []
    cols: list[np.ndarray] = []
    vals: list[np.ndarray] = []
    weights: list[np.ndarray] = []
    r = 0
    for arm, uu in zip(arms, u_of):
        n = len(uu)
        if n < 3:
            continue
        ss = arm["s"]
        h1, h2 = np.diff(ss)[:-1], np.diff(ss)[1:]
        ok = (h1 > 1e-3) & (h2 > 1e-3)
        k = np.flatnonzero(ok) + 1
        if len(k) == 0:
            continue
        h1, h2 = h1[ok], h2[ok]
        c0 = 2.0 / (h1 * (h1 + h2))
        c2 = 2.0 / (h2 * (h1 + h2))
        c1 = -(c0 + c2)
        rr = r + np.arange(len(k))
        for cc, off in ((c0, -1), (c1, 0), (c2, 1)):
            rows.append(rr)
            cols.append(uu[k + off])
            vals.append(cc)
        weights.append(np.full(len(k), _alpha(arm["lc"]) * float(np.mean(np.diff(ss)))))
        r += len(k)

    # ends leaving each node, for the straight-through rows
    ends: dict[tuple[str, float], list[tuple]] = defaultdict(list)
    for ai, arm in enumerate(arms):
        for end in ("from", "to"):
            direction = _away(arm, end)
            if direction is None:
                continue
            ends[_end_key(arm, end)].append((direction, ai, end, arm["lc"]))
    pairs = []
    for key, lst in ends.items():
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                di, ai, ea, lca = lst[i]
                dj, aj, eb, lcb = lst[j]
                ang = math.degrees(math.acos(float(np.clip(di @ dj, -1.0, 1.0))))
                if ang < STRAIGHT_DEG:
                    continue
                ua = u_of[ai]
                ub = u_of[aj]
                if ea == "from":
                    ha = float(arms[ai]["s"][min(1, len(ua) - 1)] - arms[ai]["s"][0])
                    a_node, a_in = int(ua[0]), int(ua[min(1, len(ua) - 1)])
                else:
                    ha = float(arms[ai]["s"][-1] - arms[ai]["s"][max(0, len(ua) - 2)])
                    a_node, a_in = int(ua[-1]), int(ua[max(0, len(ua) - 2)])
                if eb == "from":
                    hb = float(arms[aj]["s"][min(1, len(ub) - 1)] - arms[aj]["s"][0])
                    b_in = int(ub[min(1, len(ub) - 1)])
                else:
                    hb = float(arms[aj]["s"][-1] - arms[aj]["s"][max(0, len(ub) - 2)])
                    b_in = int(ub[max(0, len(ub) - 2)])
                if ha < 1e-3 or hb < 1e-3 or a_in == a_node or b_in == a_node:
                    continue
                c0 = 2.0 / (ha * (ha + hb))
                c2 = 2.0 / (hb * (ha + hb))
                rows.append(np.array([r, r, r]))
                cols.append(np.array([a_in, a_node, b_in]))
                vals.append(np.array([c0, -(c0 + c2), c2]))
                weights.append(np.array([_alpha(min(lca, lcb)) * 0.5 * (ha + hb)]))
                pairs.append((key, ai, ea, aj, eb))
                r += 1

    r, pinned = _append_structure_chords(arms, u_of, rows, cols, vals, weights, r)

    if r == 0:
        raise SystemExit("no smoothness rows")
    sm_r = np.concatenate(rows)
    sm_c = np.concatenate(cols)
    sm_v = np.concatenate(vals)
    sm_w = np.concatenate(weights)
    s_mat = sp.csr_matrix((sm_v, (sm_r, sm_c)), shape=(r, n_unk))
    q = (s_mat.T @ sp.diags(sm_w) @ s_mat).tocsr()

    # data term, one row per station
    n_st = sum(len(a["s"]) for a in arms)
    pu = np.empty(n_st, dtype=np.int64)
    zdata = np.empty(n_st, dtype=np.float64)
    w0 = np.empty(n_st, dtype=np.float64)
    p = 0
    for arm, uu in zip(arms, u_of):
        n = len(uu)
        pu[p : p + n] = uu
        zdata[p : p + n] = np.where(np.isfinite(arm["d"]), arm["d"], 0.0)
        w0[p : p + n] = arm["w"]
        p += n
    p_mat = sp.csr_matrix((np.ones(n_st), (np.arange(n_st), pu)), shape=(n_st, n_unk))

    w_prior = np.zeros(n_unk)
    z_prior = np.zeros(n_unk)
    for arm, uu in zip(arms, u_of):
        length = float(arm["s"][-1] - arm["s"][0])
        frac = (arm["s"] - arm["s"][0]) / length if length > 0 else np.zeros(len(uu))
        zf, zt = arm["z_gip_from"], arm["z_gip_to"]
        if not (np.isfinite(zf) and np.isfinite(zt)):
            continue
        target = zf + (zt - zf) * frac
        missing = ~np.isfinite(arm["d"])
        for i in np.flatnonzero(missing & np.isfinite(target)):
            ui = int(uu[i])
            w_prior[ui] += W_MISS
            z_prior[ui] += W_MISS * float(target[i])

    has = np.zeros(n_unk, dtype=bool)
    has[pu[w0 > 0]] = True
    # dataless components of the smoothness graph are pinned to the routing height
    adj = (s_mat.T @ s_mat) + sp.eye(n_unk, format="csr")
    ncomp, lab = connected_components(adj, directed=False)
    comp_has = np.bincount(lab, weights=has.astype(np.float64), minlength=ncomp) > 0
    z_hold = np.zeros(n_unk)
    w_hold = np.zeros(n_unk)
    held_nodes = set()
    for arm, uu in zip(arms, u_of):
        length = float(arm["s"][-1] - arm["s"][0])
        frac = (arm["s"] - arm["s"][0]) / length if length > 0 else np.zeros(len(uu))
        zf, zt = arm["z_gip_from"], arm["z_gip_to"]
        if not (np.isfinite(zf) and np.isfinite(zt)):
            continue
        target = zf + (zt - zf) * frac
        for i, ui in enumerate(uu):
            if comp_has[lab[ui]]:
                continue
            z_hold[ui] = target[i]
            w_hold[ui] = W_HOLD
            if i == 0 or i == len(uu) - 1:
                held_nodes.add(_end_key(arm, "from" if i == 0 else "to"))

    for it, c in enumerate(TUKEY + (None,)):
        ww = w0.copy()
        a = q + (p_mat.T @ sp.diags(ww) @ p_mat)
        b = p_mat.T @ (ww * zdata)
        if np.any(w_hold > 0):
            a = a + sp.diags(w_hold)
            b = b + w_hold * z_hold
        if np.any(w_prior > 0):
            a = a + sp.diags(w_prior)
            b = b + z_prior
        a = a + sp.eye(n_unk) * 1e-8
        z_u = spsolve(a.tocsc(), b)
        if c is None:
            break
        z_st = z_u[pu]
        resid = z_st - zdata
        t = np.clip(np.abs(resid) / c, 0.0, 1.0)
        w0 = np.where(w0 > 0, (1.0 - t**2) ** 2, 0.0)
        # a component that lost every weight keeps the weights it had
        kept = np.bincount(lab[pu], weights=w0, minlength=ncomp) > 0
        # stations that still have a DGM sample and sit in a component whose
        # weights all went to zero take the weight back, so the piece stays up
        data = np.concatenate([arm["w"] for arm in arms]) > 0
        w0[data & ~kept[lab[pu]] & comp_has[lab[pu]]] = 1.0
        use = data
        print(
            f"  pass {it} ({c:.1f} m): kept {100.0 * (w0[use] > 0).mean():.1f} %, "
            f"|fit - DGM| p50 {np.median(np.abs(resid[use])):.3f} "
            f"p99 {np.percentile(np.abs(resid[use]), 99):.2f}",
            flush=True,
        )

    fitted = []
    for uu in u_of:
        fitted.append(z_u[uu])
    node_z = {key: float(z_u[ui]) for key, ui in node_u.items()}
    off_chord = []
    for ai, i, ua, ub, fa, fb in pinned:
        target = fa * float(z_u[ua]) + fb * float(z_u[ub])
        off_chord.append(abs(float(z_u[u_of[ai][i]]) - target))
    print(
        f"unknowns {n_unk}, node heights {len(node_u)}, "
        f"straight pairs {len(pairs)}, held to routing {len(held_nodes)}, "
        f"structure stations on a portal line {len(pinned)}",
        flush=True,
    )
    return {
        "z": fitted,
        "node_z": node_z,
        "held": held_nodes,
        "pairs": pairs,
        "u_of": u_of,
        "off_chord": off_chord,
    }


def _pct(v: np.ndarray, cuts: tuple[float, ...] = (0.05, 0.20, 0.50)) -> dict:
    v = np.asarray(v, dtype=np.float64)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return {"n": 0}
    out = {
        "n": int(v.size),
        "p50": round(float(np.percentile(v, 50)), 4),
        "p90": round(float(np.percentile(v, 90)), 4),
        "p99": round(float(np.percentile(v, 99)), 4),
        "max": round(float(v.max()), 4),
    }
    for cut in cuts:
        out[f"over_{cut:g}"] = int(np.sum(v >= cut))
    return out


def _second(z: np.ndarray, s: np.ndarray) -> np.ndarray:
    if len(z) < 3:
        return np.zeros(0)
    h1, h2 = np.diff(s)[:-1], np.diff(s)[1:]
    ok = (h1 > 1e-3) & (h2 > 1e-3) & np.isfinite(z[:-2]) & np.isfinite(z[1:-1]) & np.isfinite(z[2:])
    if not np.any(ok):
        return np.zeros(0)
    # metres of height across one station step, same reading as the wheel metric
    return np.abs(z[:-2][ok] - 2.0 * z[1:-1][ok] + z[2:][ok])


def _grade(arm: dict, z: np.ndarray, end: str) -> float:
    s = arm["s"]
    if end == "from":
        hit = np.flatnonzero(s >= min(GRADE_M, float(s[-1]) * 0.5))
        if len(hit) == 0 or hit[0] == 0:
            return float("nan")
        i = int(hit[0])
        if not (np.isfinite(z[0]) and np.isfinite(z[i])):
            return float("nan")
        return float((z[i] - z[0]) / s[i])
    hit = np.flatnonzero((s[-1] - s) >= min(GRADE_M, float(s[-1]) * 0.5))
    if len(hit) == 0 or hit[-1] == len(s) - 1:
        return float("nan")
    i = int(hit[-1])
    if not (np.isfinite(z[-1]) and np.isfinite(z[i])):
        return float("nan")
    return float((z[i] - z[-1]) / (s[-1] - s[i]))


def _piece_steps(site: dict) -> dict:
    """Height spread of Verkehrswege ends that already sit on the same point."""
    path = processed_dir(site) / "gip_roads_beamng.json"
    if not path.is_file():
        return {"missing": str(path)}
    roads = json.loads(path.read_text(encoding="utf-8"))
    sc = SiteCoords(site)
    xy = []
    zz = []
    struct = []
    for road in roads.values():
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        kunst = str(road.get("kunstbauten") or "").casefold()
        built = any(k in kunst for k in ("brücke", "bruecke", "tunnel", "galerie", "unterführ"))
        for idx in (0, -1):
            x, y = sc.terrain_to_crs(float(nodes[idx][0]), float(nodes[idx][1]))
            xy.append((x, y))
            zz.append(float(nodes[idx][2]))
            struct.append(built)
    if len(xy) < 2:
        return {"n_ends": len(xy)}
    pts = np.asarray(xy, dtype=np.float64)
    zz = np.asarray(zz, dtype=np.float64)
    parent = np.arange(len(pts))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    tree = cKDTree(pts)
    for i, j in tree.query_pairs(END_SNAP_M):
        a, b = find(int(i)), find(int(j))
        if a != b:
            parent[b] = a
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(pts)):
        groups[find(i)].append(i)
    all_spread = []
    open_spread = []
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        spread = float(zz[idxs].max() - zz[idxs].min())
        all_spread.append(spread)
        if not any(struct[i] for i in idxs):
            open_spread.append(spread)
    return {
        "snap_m": END_SNAP_M,
        "clusters": _pct(np.asarray(all_spread)),
        "clusters_without_structure": _pct(np.asarray(open_spread)),
    }


def _report(arms: list[dict], fit: dict, site: dict) -> dict:
    spreads = []
    move = []
    for key, z_fit in fit["node_z"].items():
        samples = []
        for arm in arms:
            for end, d_i in (("from", 0), ("to", -1)):
                if _end_key(arm, end) != key:
                    continue
                if np.isfinite(arm["d"][d_i]):
                    samples.append(float(arm["d"][d_i]))
        if len(samples) >= 2:
            spreads.append(max(samples) - min(samples))
        if samples:
            move.append(abs(z_fit - float(np.median(samples))))
    raw_d2 = []
    fit_d2 = []
    for arm, z in zip(arms, fit["z"]):
        raw_d2.append(_second(arm["d"], arm["s"]))
        fit_d2.append(_second(z, arm["s"]))
    raw_d2 = np.concatenate(raw_d2) if raw_d2 else np.zeros(0)
    fit_d2 = np.concatenate(fit_d2) if fit_d2 else np.zeros(0)
    degree: dict[tuple, int] = defaultdict(int)
    for arm in arms:
        degree[_end_key(arm, "from")] += 1
        degree[_end_key(arm, "to")] += 1
    kink_raw = []
    kink_fit = []
    kink_raw_j = []
    kink_fit_j = []
    for key, ai, ea, aj, eb in fit["pairs"]:
        gr = _grade(arms[ai], arms[ai]["d"], ea) + _grade(arms[aj], arms[aj]["d"], eb)
        gf = _grade(arms[ai], fit["z"][ai], ea) + _grade(arms[aj], fit["z"][aj], eb)
        junction = degree[key] >= 3
        if np.isfinite(gr):
            kink_raw.append(abs(gr))
            if junction:
                kink_raw_j.append(abs(gr))
        if np.isfinite(gf):
            kink_fit.append(abs(gf))
            if junction:
                kink_fit_j.append(abs(gf))
    return {
        "dgm_step_at_node_m": _pct(np.asarray(spreads)),
        "fit_minus_dgm_at_node_m": _pct(np.asarray(move)),
        "second_difference_dgm_m": _pct(raw_d2),
        "second_difference_fit_m": _pct(fit_d2),
        "straight_grade_kink_dgm": _pct(np.asarray(kink_raw), (0.02, 0.05, 0.10)),
        "straight_grade_kink_fit": _pct(np.asarray(kink_fit), (0.02, 0.05, 0.10)),
        "junction_grade_kink_dgm": _pct(np.asarray(kink_raw_j), (0.02, 0.05, 0.10)),
        "junction_grade_kink_fit": _pct(np.asarray(kink_fit_j), (0.02, 0.05, 0.10)),
        "piece_ends_in_gip_roads": _piece_steps(site),
        "straight_pairs": len(fit["pairs"]),
        "nodes_held_to_routing": len(fit["held"]),
        "structure_off_portal_line_m": _pct(np.asarray(fit.get("off_chord") or [], dtype=np.float64)),
        "note": (
            "Heights are metres. Grade kink is |grade away from the node on A "
            "+ grade away on B| over 6 m, so 0.05 is a 5 % break in slope and "
            "0 means the slope continues. junction_ counts only nodes with "
            "three or more links. The fit is not written into the mesh or the level."
        ),
    }


def _write_bridge_review(out: Path, reviews: list[dict], crs: str) -> None:
    slim = [{k: v for k, v in row.items() if k != "coords"} for row in reviews]
    (out / "bridge_review.json").write_text(json.dumps(slim, indent=2), encoding="utf-8")
    rows = []
    for row, src in zip(slim, reviews):
        coords = src.get("coords") or []
        if len(coords) < 2:
            continue
        rows.append({**row, "reasons": ",".join(src.get("reasons") or []), "geometry": shapely.LineString(coords)})
    if not rows:
        print(f"wrote {out / 'bridge_review.json'} ({len(slim)} bridges, no lines)", flush=True)
        return
    gpd.GeoDataFrame(rows, crs=crs).to_file(out / "bridge_review.gpkg", layer="bridge_review", driver="GPKG")
    print(f"wrote {out / 'bridge_review.gpkg'} ({len(rows)} bridges)", flush=True)


def _write(site: dict, arms: list[dict], fit: dict, report: dict, reviews: list[dict] | None = None) -> Path:
    out = processed_dir(site) / "gip_routing"
    out.mkdir(parents=True, exist_ok=True)
    topo = load_routing(site)
    rows = []
    for key, z_fit in fit["node_z"].items():
        nid, level = key
        node = topo["nodes"].get(nid) or {}
        samples = []
        for arm in arms:
            for end, d_i in (("from", 0), ("to", -1)):
                if _end_key(arm, end) == key and np.isfinite(arm["d"][d_i]):
                    samples.append(float(arm["d"][d_i]))
        rows.append(
            {
                "node_id": nid,
                "level": level,
                "x": node.get("x"),
                "y": node.get("y"),
                "z_gip": node.get("z"),
                "z_dgm": round(float(np.median(samples)), 3) if samples else None,
                "z_fit": round(z_fit, 3),
                "dgm_spread_m": round(max(samples) - min(samples), 3) if len(samples) >= 2 else 0.0,
                "n_samples": len(samples),
                "held_to_routing": key in fit["held"],
                "geometry": shapely.Point(node["x"], node["y"]) if "x" in node else None,
            }
        )
    (out / "node_heights_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    keep = [r for r in rows if r["geometry"] is not None]
    gpd.GeoDataFrame(keep, crs=str(topo.get("crs") or resolved_working_crs(site))).to_file(
        out / "node_heights.gpkg", layer="node_height", driver="GPKG"
    )
    slim = [{k: v for k, v in r.items() if k != "geometry"} for r in keep]
    (out / "node_heights.json").write_text(json.dumps(slim), encoding="utf-8")
    crs = str(topo.get("crs") or resolved_working_crs(site))
    _write_profiles(out, arms, fit, crs)
    if reviews is not None:
        _write_bridge_review(out, reviews, crs)
    return out


def _write_profiles(out: Path, arms: list[dict], fit: dict, crs: str) -> None:
    """One linestring per link, vertices at the fitted profile."""
    rows = []
    for arm, z in zip(arms, fit["z"]):
        coords = []
        for (x, y), zz in zip(arm["xy"], z):
            if np.isfinite(zz):
                coords.append((float(x), float(y), float(zz)))
        if len(coords) < 2:
            continue
        rows.append(
            {
                "link_id": arm["id"],
                "name": arm.get("name") or "",
                "street_code": arm.get("street_code") or "",
                "fc": int(arm.get("fc") or -1),
                "width_m": arm.get("width_m"),
                "geometry": shapely.LineString(coords),
            }
        )
    if not rows:
        return
    gpd.GeoDataFrame(rows, crs=crs).to_file(out / "link_profile.gpkg", layer="link_profile", driver="GPKG")
    print(f"wrote {out / 'link_profile.gpkg'} ({len(rows)} links)", flush=True)


def _self_check() -> None:
    def arm(x0, x1, z, kind, nodes):
        n = int(round((x1 - x0) / STEP_M)) + 1
        x = np.linspace(x0, x1, n)
        s = x - x0
        return {
            "id": f"{nodes[0]}-{nodes[1]}",
            "s": s,
            "xy": np.column_stack([x, np.zeros(n)]),
            "d": np.full(n, z, dtype=np.float64),
            "w": np.ones(n),
            "kind": np.full(n, kind, dtype=object),
            "node_from": nodes[0],
            "node_to": nodes[1],
            "level_from": 0.0,
            "level_to": 0.0,
            "z_gip_from": z,
            "z_gip_to": z,
            "lc": 20.0,
            "name": "",
            "fc": 4,
        }

    low = arm(0.0, 20.0, 100.0, "", ("A", "B"))
    high = arm(20.0, 40.0, 100.4, "", ("B", "C"))
    fit = fit_arms([low, high])
    zb = fit["node_z"][("B", 0.0)]
    if not (100.05 < zb < 100.35):
        raise SystemExit(f"shared node landed at {zb}, expected between the two DGM heights")
    if abs(fit["z"][0][-1] - fit["z"][1][0]) > 1e-6:
        raise SystemExit("the two arms do not share the node height")
    raw = max(_second(low["d"], low["s"]).max(initial=0), _second(high["d"], high["s"]).max(initial=0))
    # the step sits between the arms; the fitted profile through the node must be smoother
    both = np.concatenate([fit["z"][0], fit["z"][1][1:]])
    ss = np.concatenate([low["s"], high["s"][1:] + 20.0])
    fitted_step = float(_second(both, ss).max()) if len(both) > 2 else 0.0
    _ = raw
    if fitted_step > 0.25:
        raise SystemExit(f"fitted second difference {fitted_step} still has the 0.4 m step")

    deck = arm(0.0, 40.0, 100.0, "", ("P", "Q"))
    mid = (deck["s"] > 12.0) & (deck["s"] < 28.0)
    deck["d"][mid] = 94.0
    deck["w"][mid] = 0.0
    deck["kind"][mid] = "bridge"
    deck["z_gip_from"] = deck["z_gip_to"] = 100.0
    fit_b = fit_arms([deck])
    if float(np.min(fit_b["z"][0][mid])) < 98.0:
        raise SystemExit("bridge span followed the DGM dip")

    def marked(length: float, dip: float, at: float, nodes: tuple[str, str], bid: str) -> tuple[dict, int]:
        span = arm(0.0, length, 100.0, "bridge", nodes)
        span["w"][:] = 0.0
        span["kind"][:] = "bridge"
        span["bridge_id"] = np.full(len(span["s"]), bid, dtype=object)
        i = int(np.argmin(np.abs(span["s"] - at)))
        span["d"][i] = 100.0 - dip
        return span, i

    valley, i_valley = marked(40.0, 6.0, 20.0, ("V0", "V1"), "valley")
    rev = _assess_bridges([valley])[0]
    if not rev["suspect"] or "depth" not in rev["reasons"] or valley["w"][i_valley] != 0.0:
        raise SystemExit(f"6 m valley was not suspect: {rev}")
    mild, i_mild = marked(40.0, 0.4, 20.0, ("M0", "M1"), "mild")
    rev_m = _assess_bridges([mild])[0]
    if rev_m["suspect"] or mild["w"][i_mild] != 1.0:
        raise SystemExit(f"0.4 m culvert was labelled suspect: {rev_m}")
    hang, i_hang = marked(80.0, 2.0, 40.0, ("H0", "H1"), "hang")
    rev_h = _assess_bridges([hang])[0]
    if not rev_h["suspect"] or rev_h["reasons"] != ["depth"] or hang["w"][i_hang] != 0.0:
        raise SystemExit(f"2 m hang over 80 m should be depth-only suspect: {rev_h}")
    creek, i_creek = marked(16.0, 1.2, 8.0, ("C0", "C1"), "creek")
    rev_c = _assess_bridges([creek])[0]
    if not rev_c["suspect"] or "grade" not in rev_c["reasons"] or "depth" in rev_c["reasons"]:
        raise SystemExit(f"1.2 m over 16 m should be grade-only suspect: {rev_c}")
    if creek["w"][i_creek] != 0.0:
        raise SystemExit("small-stream bridge was given back to the DGM")

    both = arm(0.0, 40.0, 100.0, "bridge", ("O0", "O1"))
    both["w"][:] = 1.0
    both["street_code"] = "B1"
    both["bridge_id"] = np.full(len(both["s"]), "br", dtype=object)
    cover = np.zeros(len(both["s"]), dtype=bool)
    cover[5:15] = True
    both["spans"] = [
        {"id": "br", "tag": "bridge", "name": "", "on": np.ones(len(both["s"]), dtype=bool)},
        {"id": "gal", "tag": "gallery", "name": "", "on": cover},
    ]
    found = _overlap_pairs([both])
    if len(found) != 1:
        raise SystemExit(f"expected one gallery/bridge overlap, got {list(found)}")
    _apply_overlap(next(iter(found.values())), "structure")
    if not np.all(both["kind"][cover] == "gallery") or np.any(both["w"][cover] != 0.0):
        raise SystemExit("gallery choice did not take the overlap off the ground")
    if np.any(both["w"][~cover] != 1.0) or np.any(both["kind"][~cover] != "bridge"):
        raise SystemExit("bridge outside the overlap was changed")
    if np.any(both["spans"][0]["on"][cover]):
        raise SystemExit("bridge span still covers the gallery")
    both["spans"][0]["on"] = np.ones(len(both["s"]), dtype=bool)
    both["w"][:] = 1.0
    both["kind"][:] = "bridge"
    both["bridge_id"][:] = "br"
    key = next(iter(found))
    if not key.startswith(both["id"] + "|"):
        raise SystemExit(f"overlap row is not keyed by the link: {key}")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "structure_choices.json"
        path.write_text(json.dumps({key: {"choice": "structure"}}), encoding="utf-8")
        _resolve_overlaps([both], [], path, ask=False)
        if np.any(both["w"][cover] != 0.0):
            raise SystemExit("stored choice was not applied")
        if np.any(both["spans"][0]["on"][cover]):
            raise SystemExit("stored choice left the bridge on the gallery")
        try:
            both["spans"][0]["on"] = np.ones(len(both["s"]), dtype=bool)
            both["spans"][1]["id"] = "other"
            _resolve_overlaps([both], [], path, ask=False)
        except SystemExit:
            pass
        else:
            raise SystemExit("missing row was not left for a console question")
    print("self-check: shared node, bridge span, suspect labels, and overlap choice hold", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--self-check", action="store_true", help="run the small synthetic fit and stop")
    ap.add_argument("--step", type=float, default=STEP_M, help="station spacing in metres")
    args = ap.parse_args()
    if args.self_check:
        _self_check()
        return
    site = load_site()
    arms, reviews = _build_arms(site, float(args.step), ask_overlaps=sys.stdin.isatty())
    fit = fit_arms(arms)
    report = _report(arms, fit, site)
    report["bridge_review"] = {
        "n": len(reviews),
        "suspect": sum(1 for r in reviews if r["suspect"]),
        "by_depth": sum(1 for r in reviews if "depth" in r["reasons"]),
        "by_grade": sum(1 for r in reviews if "grade" in r["reasons"]),
        "suspect_dip_m": SUSPECT_DIP_M,
        "suspect_grade": SUSPECT_GRADE,
    }
    out = _write(site, arms, fit, report, reviews)
    print(json.dumps(report, indent=2))
    print(f"wrote {out / 'node_heights.gpkg'}")
    for row in reviews:
        if not row["suspect"]:
            continue
        print(
            f"suspect {row['id'][:8]} {row['street'] or '-'} "
            f"depth {row['depth_m']} m  {row['grade_pct']}%  {','.join(row['reasons'])}",
            flush=True,
        )
    _print_interior_ground(arms, fit)


if __name__ == "__main__":
    main()
