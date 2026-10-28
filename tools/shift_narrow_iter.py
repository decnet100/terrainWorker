"""Narrow and shift again, only where the cross difference is already high.

Each round takes 10 cm off the carriageway width and repeats the lateral fit
from the current axis. A round is kept only when the edge-to-edge height
difference falls and the maximum does not jump by more than 1 m. A chain
stops when a round does not help, or when 2 m have been taken off.

The start is the first fitted axis, not the plain second shift.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\shift_narrow_iter.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
from shapely.geometry import LineString

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_link_surfaces import (  # noqa: E402
    BEARING_M,
    JUNCTION_MIN_M2,
    SLIVER_M2,
    _cum,
    _fit_to_ground,
    _parts,
    _point_at,
    _ray_hits,
    _ribbon,
    _smooth_curve,
    _width_profile,
)
from build_road_grid import _load_geotiff  # noqa: E402
from gip_routing_load import gpkg_path  # noqa: E402
from sample_cross_profile import MIN_WIDTH_M  # noqa: E402
from score_link_surfaces import _weighted  # noqa: E402
from shift_fitted_again import END_M, MARK_M, _cross_summary, _shift_of, _widths, _xy  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

NARROW_M = 0.1
MAX_NARROW_M = 2.0
MAX_ROUNDS = 12


def _better(before: dict, after: dict) -> bool:
    if None in (after["p50"], before["p50"], after["max"], before["max"]):
        return False
    if after["share_half"] > before["share_half"] + 0.005:
        return False
    if after["p50"] > before["p50"] + 0.02:
        return False
    if after["max"] > before["max"] + 1.0:
        return False
    return (
        after["share_one"] < before["share_one"] - 0.005
        or after["share_half"] < before["share_half"] - 0.005
        or after["p50"] < before["p50"] - 0.01
        or after["max"] < before["max"] - 0.05
    )


def _num(value, digits: int) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}"


def _measure(chain, poly, struct, elev, spec) -> dict:
    cross, p50, pmax, share_half, share_one = _cross_summary(
        LineString(chain["pts"]), poly, struct, chain["width_m"], chain["width_along"], elev, spec
    )
    return {
        "cross": cross,
        "p50": p50,
        "max": None if pmax is None else float(pmax),
        "share_half": 0.0 if share_half is None else float(share_half),
        "share_one": 0.0 if share_one is None else float(share_one),
    }


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    src = proc / "link_surface" / "surface_width.gpkg"
    score_path = proc / "link_surface" / "score.gpkg"
    fit = gpd.read_file(src, layer="axis_fit")
    scored = gpd.read_file(score_path, layer="chain")
    junctions = gpd.read_file(src, layer="junction_point")
    bad = {
        str(rec.short_ids)
        for rec in scored.itertuples(index=False)
        if rec.cross_max_m is not None and float(rec.cross_max_m) >= MARK_M
    }
    name_of = {}
    for rec in scored.itertuples(index=False):
        name = rec.name
        if name is None or (isinstance(name, float) and not np.isfinite(name)):
            name = ""
        text = str(name).strip()
        name_of[str(rec.short_ids)] = "" if text.lower() in ("", "nan", "none") else text
    print(
        f"narrow {NARROW_M:.2f} m and refit, {len(bad)} chains with a station >= {MARK_M:.2f} m",
        flush=True,
    )
    elev, spec = _load_geotiff(proc / "corridor50_raw" / "corridor50_raw.tif")
    links = gpd.read_file(gpkg_path(site), layer="link")
    short_of = {str(rec.link_id): int(rec.short_id) for rec in links.itertuples(index=False)}
    old_surface = gpd.read_file(src, layer="surface")
    old_poly: dict[str, list] = defaultdict(list)
    for rec in old_surface.itertuples(index=False):
        old_poly[str(rec.short_ids)].append(rec.geometry)

    chains: dict[str, dict] = {}
    order = []
    for rec in fit.itertuples(index=False):
        key = str(rec.short_ids)
        pts = _xy(rec.geometry)
        chains[key] = {
            "pts": pts,
            "ws": _widths(pts, rec.width_m, rec.width_along),
            "code": rec.code,
            "fc": int(rec.fc),
            "fitted": int(rec.fitted),
            "name": name_of.get(key, ""),
            "bad": key in bad,
            "old_pts": list(pts),
            "narrow_m": 0.0,
            "rounds": 0,
            "active": key in bad,
        }
        order.append(key)
    id_to_key = {}
    for key in chains:
        for part in key.split(","):
            if part:
                id_to_key[int(part)] = key
    struct_parts: dict[str, list] = defaultdict(list)
    try:
        refs = gpd.read_file(gpkg_path(site), layer="bridge_ref")
    except Exception:
        refs = gpd.GeoDataFrame()
    for rec in refs.itertuples(index=False):
        sid = short_of.get(str(rec.link_id))
        key = id_to_key.get(sid) if sid is not None else None
        if key is not None and rec.geometry is not None and not rec.geometry.is_empty:
            struct_parts[key].append(rec.geometry)
    struct_of = {key: shapely.unary_union(parts) for key, parts in struct_parts.items() if parts}

    def ribbon_poly(chain):
        return _ribbon(chain["pts"], chain["ws"])

    baseline = {}
    for key in order:
        chain = chains[key]
        if not chain["bad"]:
            continue
        poly = shapely.unary_union(old_poly[key]) if old_poly.get(key) else ribbon_poly(chain)
        width_m, width_along = _width_profile(chain["pts"], chain["ws"])
        chain["width_m"] = width_m
        chain["width_along"] = width_along
        baseline[key] = _measure(chain, poly, struct_of.get(key), elev, spec)

    for round_i in range(1, MAX_ROUNDS + 1):
        kept = 0
        tried = 0
        for key in order:
            chain = chains[key]
            if not chain["active"]:
                continue
            if chain["narrow_m"] + NARROW_M > MAX_NARROW_M + 1e-6:
                chain["active"] = False
                continue
            if min(chain["ws"]) - NARROW_M < MIN_WIDTH_M - 1e-6:
                chain["active"] = False
                continue
            tried += 1
            trial_ws = [round(w - NARROW_M, 2) for w in chain["ws"]]
            pts, ws, fitted = _fit_to_ground(list(chain["pts"]), trial_ws, elev, spec)
            if not fitted:
                chain["active"] = False
                continue
            pts = _smooth_curve(pts, [])
            width_m, width_along = _width_profile(pts, ws)
            trial = {
                "pts": pts,
                "ws": ws,
                "width_m": width_m,
                "width_along": width_along,
            }
            poly = _ribbon(pts, ws)
            after = _measure(
                {"pts": pts, "width_m": width_m, "width_along": width_along},
                poly,
                struct_of.get(key),
                elev,
                spec,
            )
            current = _measure(chain, ribbon_poly(chain), struct_of.get(key), elev, spec)
            if not _better(current, after):
                chain["active"] = False
                continue
            chain["pts"] = pts
            chain["ws"] = ws
            chain["width_m"] = width_m
            chain["width_along"] = width_along
            chain["narrow_m"] = round(chain["narrow_m"] + NARROW_M, 2)
            chain["rounds"] += 1
            if after["share_half"] <= 0.02 and after["max"] < MARK_M:
                chain["active"] = False
            kept += 1
        print(f"round {round_i}: tried {tried}, kept {kept}", flush=True)
        if kept == 0:
            break

    ends_before = {}
    # Seat against the original junction points. Ends may have moved, so use a
    # wider match than the first fit: the end that was on the point, or the
    # current end if it is still close.
    src_fit_ends = {}
    for rec in fit.itertuples(index=False):
        pts = _xy(rec.geometry)
        src_fit_ends[str(rec.short_ids)] = (pts[0], pts[-1])
    for rec in junctions.itertuples(index=False):
        node = (float(rec.geometry.x), float(rec.geometry.y))
        through_ids = {int(part) for part in str(rec.through).split(",") if part}
        through_key = next(
            (key for key in order if through_ids <= {int(p) for p in key.split(",") if p}),
            None,
        )
        if through_key is None:
            continue
        through = chains[through_key]["pts"]
        alongs = []
        seated = []
        for key in order:
            if key == through_key:
                continue
            start, end = src_fit_ends[key]
            for at_start, pt in ((True, start), (False, end)):
                if (pt[0] - node[0]) ** 2 + (pt[1] - node[1]) ** 2 > END_M ** 2:
                    continue
                seq = chains[key]["pts"] if at_start else list(reversed(chains[key]["pts"]))
                if len(seq) < 2:
                    continue
                origin = _point_at(seq, min(BEARING_M, max(4.0, _cum(seq)[-1] * 0.5)))
                found = _ray_hits(origin, seq[0], through, node)
                if found is None:
                    continue
                alongs.append(found[1])
                seated.append((key, at_start))
        if not alongs:
            continue
        junction = _point_at(through, sum(alongs) / len(alongs))
        for key, at_start in seated:
            if at_start:
                chains[key]["pts"][0] = junction
            else:
                chains[key]["pts"][-1] = junction

    raw: dict[int, list] = defaultdict(list)
    for key in order:
        poly = _ribbon(chains[key]["pts"], chains[key]["ws"])
        if poly is not None:
            raw[chains[key]["fc"]].append((key, poly))
    cut = {}
    higher = []
    higher_union = None
    for fc in sorted(raw):
        for key, poly in raw[fc]:
            geom = poly if higher_union is None or higher_union.is_empty else shapely.difference(poly, higher_union)
            cut[key] = geom
        higher.extend(poly for _key, poly in raw[fc])
        higher_union = shapely.unary_union(higher)

    before_v, before_w, after_v, after_w = [], [], [], []
    rows = []
    for key in order:
        chain = chains[key]
        if not chain["bad"] or key not in cut:
            continue
        struct = struct_of.get(key)
        base = baseline[key]
        after = _measure(chain, cut[key], struct, elev, spec)
        if base["cross"].size:
            before_v.append(base["cross"])
            before_w.append(np.full(base["cross"].shape, 4.0))
        if after["cross"].size:
            after_v.append(after["cross"])
            after_w.append(np.full(after["cross"].shape, 4.0))
        rows.append(
            {
                "code": chain["code"],
                "name": chain["name"],
                "length_m": round(float(LineString(chain["pts"]).length), 1),
                "narrow_m": chain["narrow_m"],
                "rounds": chain["rounds"],
                "moved_m": round(_shift_of(LineString(chain["old_pts"]), chain["pts"]), 2),
                "before_p50": None if base["p50"] is None else round(base["p50"], 3),
                "after_p50": None if after["p50"] is None else round(after["p50"], 3),
                "before_share_0_50": round(base["share_half"], 3),
                "after_share_0_50": round(after["share_half"], 3),
                "before_share_1": round(base["share_one"], 3),
                "after_share_1": round(after["share_one"], 3),
                "before_max": None if base["max"] is None else round(base["max"], 2),
                "after_max": None if after["max"] is None else round(after["max"], 2),
                "short_ids": key,
                "geometry": LineString(chain["pts"]),
            }
        )
    bv = np.concatenate(before_v) if before_v else np.array([])
    bw = np.concatenate(before_w) if before_w else np.array([])
    av = np.concatenate(after_v) if after_v else np.array([])
    aw = np.concatenate(after_w) if after_w else np.array([])
    print(
        "cross p50 "
        f"{_weighted(bv, bw, (0.5,))[0]} -> {_weighted(av, aw, (0.5,))[0]} m, "
        f"share >= 0.50 {None if bv.size == 0 else round(float(np.mean(bv >= MARK_M)), 3)} -> "
        f"{None if av.size == 0 else round(float(np.mean(av >= MARK_M)), 3)}, "
        f"share >= 1 {None if bv.size == 0 else round(float(np.mean(bv >= 1.0)), 3)} -> "
        f"{None if av.size == 0 else round(float(np.mean(av >= 1.0)), 3)}",
        flush=True,
    )
    kept_n = sum(1 for row in rows if row["rounds"] > 0)
    print(f"chains that kept at least one round: {kept_n} of {len(rows)}", flush=True)
    print("code             len  narrow rounds move  p50  share>=0.5  share>=1  max  name")
    for row in sorted(rows, key=lambda r: -r["length_m"]):
        if row["length_m"] < 400 and row["code"] == "Weg":
            continue
        name = row["name"] or row["short_ids"].split(",")[0]
        print(
            f"{row['code']:16} {row['length_m']:6.0f} {row['narrow_m']:5.2f} {row['rounds']:2d} {row['moved_m']:5.2f}  "
            f"{row['before_p50']} -> {row['after_p50']}  "
            f"{row['before_share_0_50']} -> {row['after_share_0_50']}  "
            f"{row['before_share_1']} -> {row['after_share_1']}  "
            f"{row['before_max']} -> {row['after_max']}  {name}",
            flush=True,
        )

    crs = fit.crs
    dest = proc / "link_surface" / "surface_narrow.gpkg"
    if dest.exists():
        try:
            dest.unlink()
        except PermissionError as exc:
            raise SystemExit(f"{dest.name} is open") from exc
    gpd.read_file(src, layer="axis").to_file(dest, layer="axis", driver="GPKG")
    fit_rows = []
    for key in order:
        chain = chains[key]
        width_m, width_along = _width_profile(chain["pts"], chain["ws"])
        fit_rows.append(
            {
                "code": chain["code"],
                "fc": chain["fc"],
                "fitted": chain["fitted"],
                "width_m": width_m,
                "width_along": width_along,
                "narrow_m": chain["narrow_m"],
                "short_ids": key,
                "geometry": LineString(chain["pts"]),
            }
        )
    gpd.GeoDataFrame(fit_rows, crs=crs).to_file(dest, layer="axis_fit", driver="GPKG")
    surface_rows = []
    for key, geom in cut.items():
        chain = chains[key]
        width_m, width_along = _width_profile(chain["pts"], chain["ws"])
        for part in _parts(geom):
            if part.area < SLIVER_M2:
                continue
            surface_rows.append(
                {
                    "code": chain["code"],
                    "fc": chain["fc"],
                    "width_m": width_m,
                    "short_ids": key,
                    "narrow_m": chain["narrow_m"],
                    "area_m2": round(float(part.area), 1),
                    "geometry": part,
                }
            )
    if surface_rows:
        gpd.GeoDataFrame(surface_rows, crs=crs).to_file(dest, layer="surface", driver="GPKG")
    junction_rows = []
    for fc, items in raw.items():
        geoms = [poly for _key, poly in items]
        if len(geoms) < 2:
            continue
        pairs = shapely.STRtree(geoms).query(geoms, predicate="intersects")
        seen = set()
        for i, j in pairs.T:
            a, b = int(i), int(j)
            if a >= b or (a, b) in seen:
                continue
            seen.add((a, b))
            inter = geoms[a].intersection(geoms[b])
            if inter.is_empty or inter.area < JUNCTION_MIN_M2:
                continue
            for part in _parts(inter):
                if part.area < JUNCTION_MIN_M2:
                    continue
                junction_rows.append(
                    {
                        "code": chains[items[a][0]]["code"],
                        "fc": fc,
                        "a": items[a][0],
                        "b": items[b][0],
                        "area_m2": round(float(part.area), 1),
                        "geometry": part,
                    }
                )
    if junction_rows:
        gpd.GeoDataFrame(junction_rows, crs=crs).to_file(dest, layer="junction", driver="GPKG")
    gpd.GeoDataFrame(rows, crs=crs).to_file(dest, layer="compare", driver="GPKG")
    print(f"(open the following in qgis or similar tool to check: {dest}, {src})", flush=True)


if __name__ == "__main__":
    main()
