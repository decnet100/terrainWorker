"""A second lateral fit, only on chains that already have a rough cross-section.

The input is the fitted axis, not the original GIP line. A chain is included
when its largest edge-to-edge height difference is at least 0.5 m. Other
chains keep their axis. After the fit, a side arm still ends on the through
road. The through road is not pulled back onto a straight line.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\shift_fitted_again.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
from shapely.geometry import LineString, Point

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
from score_link_surfaces import _percentiles, _score_chain, _weighted  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

MARK_M = 0.5
END_M = 1.5


def _parse_runs(width_m, width_along) -> list[tuple[float, float]]:
    text = "" if width_along is None else str(width_along)
    if text in ("", "None", "nan"):
        return [(0.0, float(width_m))]
    runs = []
    for part in text.split(","):
        station, width = part.split(":")
        runs.append((float(station), float(width)))
    return runs or [(0.0, float(width_m))]


def _widths(pts, width_m, width_along) -> list[float]:
    runs = _parse_runs(width_m, width_along)
    out = []
    j = 0
    for station in _cum(pts):
        while j + 1 < len(runs) and runs[j + 1][0] <= station + 1e-6:
            j += 1
        out.append(runs[j][1])
    return out


def _xy(geom) -> list[tuple[float, float]]:
    if geom.geom_type == "MultiLineString":
        geom = max(geom.geoms, key=lambda g: g.length)
    return [(float(x), float(y)) for x, y, *_rest in geom.coords]


def _shift_of(old: LineString, pts) -> float:
    if len(pts) < 2:
        return 0.0
    line = LineString(pts)
    step = max(8.0, line.length / 40.0)
    count = max(2, int(line.length / step))
    stations = np.linspace(0.0, line.length, count)
    here = shapely.line_interpolate_point(line, stations)
    return float(np.median(shapely.distance(old, here)))


def _cross_summary(line, poly, struct, width_m, width_along, elev, spec):
    scored = _score_chain(line, poly, struct, width_m, width_along, elev, spec)
    p50, _p90, pmax = _percentiles(scored["cross"], (0.5, 0.9, 1.0))
    n = scored["n_cross"]
    share_half = None if n == 0 else round(float(np.mean(scored["cross"] >= MARK_M)), 3)
    share_one = None if n == 0 else round(float(np.mean(scored["cross"] >= 1.0)), 3)
    return scored["cross"], p50, pmax, share_half, share_one


def _seat(chains: dict[str, dict], junctions) -> None:
    """Put side-arm ends back on the through axis. The through axis stays."""
    by_ids: dict[frozenset[int], str] = {}
    for key, chain in chains.items():
        by_ids[frozenset(int(part) for part in key.split(",") if part)] = key
    for rec in junctions.itertuples(index=False):
        through_ids = frozenset(int(part) for part in str(rec.through).split(",") if part)
        through_key = next((key for ids, key in by_ids.items() if through_ids <= ids), None)
        if through_key is None:
            continue
        through = chains[through_key]
        node = (float(rec.geometry.x), float(rec.geometry.y))
        attached = []
        for key, chain in chains.items():
            if key == through_key:
                continue
            for at_start, pt in ((True, chain["pts"][0]), (False, chain["pts"][-1])):
                if (pt[0] - node[0]) ** 2 + (pt[1] - node[1]) ** 2 <= END_M ** 2:
                    attached.append((key, at_start))
        alongs = []
        seated = []
        for key, at_start in attached:
            chain = chains[key]
            seq = chain["pts"] if at_start else list(reversed(chain["pts"]))
            if len(seq) < 2:
                continue
            length = _cum(seq)[-1]
            origin = _point_at(seq, min(BEARING_M, max(4.0, length * 0.5)))
            found = _ray_hits(origin, seq[0], through["pts"], node)
            if found is None:
                continue
            alongs.append(found[1])
            seated.append((key, at_start))
        if not alongs:
            continue
        junction = _point_at(through["pts"], sum(alongs) / len(alongs))
        rec_xy = junction
        for key, at_start in seated:
            chain = chains[key]
            if at_start:
                chain["pts"][0] = rec_xy
            else:
                chain["pts"][-1] = rec_xy
        chain_j = chains[through_key]
        chain_j.setdefault("junctions", []).append(rec_xy)


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    src = proc / "link_surface" / "surface_width.gpkg"
    score_path = proc / "link_surface" / "score.gpkg"
    if not src.is_file() or not score_path.is_file():
        raise SystemExit("surface_width.gpkg and score.gpkg are required")
    fit = gpd.read_file(src, layer="axis_fit")
    scored = gpd.read_file(score_path, layer="chain")
    junctions = gpd.read_file(src, layer="junction_point")
    bad = {
        str(rec.short_ids)
        for rec in scored.itertuples(index=False)
        if rec.cross_max_m is not None and float(rec.cross_max_m) >= MARK_M
    }
    print(f"second fit on {len(bad)} of {len(fit)} chains with a station >= {MARK_M:.2f} m", flush=True)

    elev, spec = _load_geotiff(proc / "corridor50_raw" / "corridor50_raw.tif")
    routing = gpkg_path(site)
    links = gpd.read_file(routing, layer="link")
    short_of = {str(rec.link_id): int(rec.short_id) for rec in links.itertuples(index=False)}
    chains: dict[str, dict] = {}
    order = []
    for rec in fit.itertuples(index=False):
        key = str(rec.short_ids)
        pts = _xy(rec.geometry)
        ws = _widths(pts, rec.width_m, rec.width_along)
        chains[key] = {
            "pts": pts,
            "ws": ws,
            "code": rec.code,
            "fc": int(rec.fc),
            "fitted": int(rec.fitted),
            "name": str(getattr(rec, "name", "") or ""),
            "bad": key in bad,
            "old": LineString(pts),
            "old_width_m": float(rec.width_m),
            "old_width_along": rec.width_along,
            "moved": 0.0,
            "refit": 0,
        }
        order.append(key)
    struct_parts: dict[str, list] = defaultdict(list)
    try:
        refs = gpd.read_file(routing, layer="bridge_ref")
    except Exception:
        refs = gpd.GeoDataFrame()
    id_to_key = {}
    for key in chains:
        for part in key.split(","):
            if part:
                id_to_key[int(part)] = key
    for rec in refs.itertuples(index=False):
        sid = short_of.get(str(rec.link_id))
        key = id_to_key.get(sid) if sid is not None else None
        if key is not None and rec.geometry is not None and not rec.geometry.is_empty:
            struct_parts[key].append(rec.geometry)
    struct_of = {key: shapely.unary_union(parts) for key, parts in struct_parts.items() if parts}

    # Ends are recorded before the fit, so a side arm can be found after it moves.
    ends_before = {key: (chain["pts"][0], chain["pts"][-1]) for key, chain in chains.items()}
    n_fit = 0
    for i, key in enumerate(order):
        chain = chains[key]
        if not chain["bad"]:
            continue
        pts, ws, fitted = _fit_to_ground(chain["pts"], chain["ws"], elev, spec)
        if fitted:
            pts = _smooth_curve(pts, [])
            chain["pts"] = pts
            chain["ws"] = ws
            chain["refit"] = 1
            chain["moved"] = _shift_of(chain["old"], pts)
            n_fit += 1
        if (i + 1) % 25 == 0:
            print(f"  looked at {i + 1}, refitted {n_fit}", flush=True)
    print(f"refitted {n_fit}", flush=True)

    # Restore the pre-fit ends into the search, then seat on the new through axis.
    # The recorded end is only a search key: the ray uses the shifted chain.
    for rec in junctions.itertuples(index=False):
        node = (float(rec.geometry.x), float(rec.geometry.y))
        through_ids = {int(part) for part in str(rec.through).split(",") if part}
        through_key = next(
            (key for key, chain in ((k, chains[k]) for k in order) if through_ids <= {int(p) for p in key.split(",") if p}),
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
            start, end = ends_before[key]
            for at_start, pt in ((True, start), (False, end)):
                if (pt[0] - node[0]) ** 2 + (pt[1] - node[1]) ** 2 > END_M ** 2:
                    continue
                seq = chains[key]["pts"] if at_start else list(reversed(chains[key]["pts"]))
                if len(seq) < 2:
                    continue
                length = _cum(seq)[-1]
                origin = _point_at(seq, min(BEARING_M, max(4.0, length * 0.5)))
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
        chain = chains[key]
        poly = _ribbon(chain["pts"], chain["ws"])
        if poly is None:
            continue
        raw[chain["fc"]].append((key, poly))
    cut: dict[str, object] = {}
    higher = []
    higher_union = None
    for fc in sorted(raw):
        for key, poly in raw[fc]:
            geom = poly
            if higher_union is not None and not higher_union.is_empty:
                geom = shapely.difference(poly, higher_union)
            cut[key] = geom
        higher.extend(poly for _key, poly in raw[fc])
        higher_union = shapely.unary_union(higher)

    before_v = []
    before_w = []
    after_v = []
    after_w = []
    rows = []
    for key in order:
        chain = chains[key]
        if not chain["bad"] or key not in cut:
            continue
        struct = struct_of.get(key)
        width_m, width_along = _width_profile(chain["pts"], chain["ws"])
        chain["width_m"] = width_m
        chain["width_along"] = width_along
        new_line = LineString(chain["pts"])
        a_cross, a50, amax, a_half, a_one = _cross_summary(
            new_line, cut[key], struct, width_m, width_along, elev, spec
        )
        if a_cross.size:
            after_v.append(a_cross)
            after_w.append(np.full(a_cross.shape, 4.0))
        rows.append(
            {
                "code": chain["code"],
                "name": chain["name"],
                "length_m": round(float(new_line.length), 1),
                "moved_m": round(chain["moved"], 2),
                "refit": chain["refit"],
                "before_p50": None,
                "after_p50": a50,
                "before_max": None,
                "after_max": amax,
                "before_share_0_50": None,
                "after_share_0_50": a_half,
                "before_share_1": None,
                "after_share_1": a_one,
                "short_ids": key,
            }
        )

    # The before sample above ignored the old polygon. Recompute it from the
    # stored surface so the comparison uses the same edges as the last score.
    old_surface = gpd.read_file(src, layer="surface")
    old_poly: dict[str, list] = defaultdict(list)
    for rec in old_surface.itertuples(index=False):
        old_poly[str(rec.short_ids)].append(rec.geometry)
    before_v = []
    before_w = []
    for row in rows:
        key = row["short_ids"]
        chain = chains[key]
        struct = struct_of.get(key)
        poly = shapely.unary_union(old_poly[key]) if old_poly.get(key) else None
        cross, p50, pmax, share_half, share_one = _cross_summary(
            chain["old"], poly, struct, chain["old_width_m"], chain["old_width_along"], elev, spec
        )
        row["before_p50"] = p50
        row["before_max"] = pmax
        row["before_share_0_50"] = share_half
        row["before_share_1"] = share_one
        if cross.size:
            before_v.append(cross)
            before_w.append(np.full(cross.shape, 4.0))

    bv = np.concatenate(before_v) if before_v else np.array([])
    bw = np.concatenate(before_w) if before_w else np.array([])
    av = np.concatenate(after_v) if after_v else np.array([])
    aw = np.concatenate(after_w) if after_w else np.array([])
    print(
        "on the refitted chains, cross p50 "
        f"{_weighted(bv, bw, (0.5,))[0]} -> {_weighted(av, aw, (0.5,))[0]} m, "
        f"share >= 0.50 m {None if bv.size == 0 else round(float(np.mean(bv >= MARK_M)), 3)} -> "
        f"{None if av.size == 0 else round(float(np.mean(av >= MARK_M)), 3)}, "
        f"share >= 1 m {None if bv.size == 0 else round(float(np.mean(bv >= 1.0)), 3)} -> "
        f"{None if av.size == 0 else round(float(np.mean(av >= 1.0)), 3)}",
        flush=True,
    )
    print("code             len  move  p50 before/after  share>=0.5  share>=1  max before/after  name")
    for row in sorted(rows, key=lambda r: -r["length_m"]):
        if row["length_m"] < 400 and row["code"] == "Weg":
            continue
        name = row["name"] or row["short_ids"].split(",")[0]
        print(
            f"{row['code']:16} {row['length_m']:6.0f} {row['moved_m']:5.2f}  "
            f"{row['before_p50']} -> {row['after_p50']}  "
            f"{row['before_share_0_50']} -> {row['after_share_0_50']}  "
            f"{row['before_share_1']} -> {row['after_share_1']}  "
            f"{row['before_max']} -> {row['after_max']}  {name}",
            flush=True,
        )

    crs = fit.crs
    fit_rows = []
    for key in order:
        chain = chains[key]
        width_m, width_along = _width_profile(chain["pts"], chain["ws"])
        fit_rows.append(
            {
                "code": chain["code"],
                "fc": chain["fc"],
                "fitted": chain["fitted"] or chain["refit"],
                "width_m": width_m,
                "width_along": width_along,
                "short_ids": key,
                "refit": chain["refit"],
                "geometry": LineString(chain["pts"]),
            }
        )
    surface_rows = []
    for fc in sorted(raw):
        for key, _poly in raw[fc]:
            geom = cut.get(key)
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
                        "width_along": width_along,
                        "fitted": chain["fitted"] or chain["refit"],
                        "short_ids": key,
                        "area_m2": round(float(part.area), 1),
                        "geometry": part,
                    }
                )
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
            ka, kb = items[a][0], items[b][0]
            for part in _parts(inter):
                if part.area < JUNCTION_MIN_M2:
                    continue
                junction_rows.append(
                    {
                        "code": chains[ka]["code"],
                        "fc": fc,
                        "a": ka,
                        "b": kb,
                        "area_m2": round(float(part.area), 1),
                        "geometry": part,
                    }
                )
    dest = proc / "link_surface" / "surface_shift2.gpkg"
    if dest.exists():
        try:
            dest.unlink()
        except PermissionError as exc:
            raise SystemExit(f"{dest.name} is open") from exc
    axis = gpd.read_file(src, layer="axis")
    axis.to_file(dest, layer="axis", driver="GPKG")
    gpd.GeoDataFrame(fit_rows, crs=crs).to_file(dest, layer="axis_fit", driver="GPKG")
    if surface_rows:
        gpd.GeoDataFrame(surface_rows, crs=crs).to_file(dest, layer="surface", driver="GPKG")
    if junction_rows:
        gpd.GeoDataFrame(junction_rows, crs=crs).to_file(dest, layer="junction", driver="GPKG")
    if len(junctions):
        junctions.to_file(dest, layer="junction_point", driver="GPKG")
    gpd.GeoDataFrame(rows, geometry=[LineString(chains[r["short_ids"]]["pts"]) for r in rows], crs=crs).to_file(
        dest, layer="compare", driver="GPKG"
    )
    print(f"(open the following in qgis or similar tool to check: {dest}, {src})", flush=True)


if __name__ == "__main__":
    main()
