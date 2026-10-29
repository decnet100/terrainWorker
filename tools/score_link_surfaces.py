"""Score the link-surface polygons and list bridges, tunnels and galleries.

The surface score uses the raw 0.5 m DGM under each chain, outside structure
spans. Cross difference is the absolute height gap of the two carriageway
edges. Roughness is the absolute deviation from the local 1.5 m mean.
Kink is the turn away from a straight line, measured on an 8 m base.
Overlap is the intersection of two different chains after the rank cut.

Structures come from the routing reference objects already attached to the
links. The ground dip is the same chord test as ``gip_brunnel_match``:
a bridge whose ground falls 1 m or more under the chord is an opening, a
bridge shorter than 10 m without that fall is a culvert.

Does not write a BeamNG level.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\score_link_surfaces.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
from shapely.geometry import LineString, Point

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_road_grid import _load_geotiff  # noqa: E402
from gip_brunnel_match import CULVERT_MAX_M, DIP_OPEN_M, _dip, _kind  # noqa: E402
from gip_routing_load import gpkg_path  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

STATION_M = 4.0
# Sample this far inside the stored carriageway edge.
EDGE_INSET_M = 0.25
# Stations at or above this cross difference are marked. This is the old
# placement window, reported so a chain can be compared with it.
CROSS_MARK_M = 0.50
KINK_BASE_M = 8.0
KINK_STEP_M = 4.0
KINK_POINT_DEG = 15.0
JUNCTION_M = 20.0
OVERLAP_MIN_M2 = 1.0
STRUCT_SKIP_M = 1.0
ACROSS_STEP_M = 0.5


def _percentiles(values: np.ndarray, qs: tuple[float, ...]) -> list[float | None]:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return [None] * len(qs)
    picked = np.percentile(finite, [q * 100.0 for q in qs])
    return [round(float(v), 3) for v in picked]


def _weighted(values: np.ndarray, weights: np.ndarray, qs: tuple[float, ...]) -> list[float | None]:
    keep = np.isfinite(values) & np.isfinite(weights) & (weights > 0)
    if not np.any(keep):
        return [None] * len(qs)
    order = np.argsort(values[keep])
    v = values[keep][order]
    w = np.cumsum(weights[keep][order])
    w = w / w[-1]
    return [round(float(np.interp(q, w, v)), 3) for q in qs]


def _z_at(xs: np.ndarray, ys: np.ndarray, elev: np.ndarray, spec: dict) -> np.ndarray:
    """Bilinear height. A cell value sits at the cell centre, half a cell in from the corner.

    A neighbour with weight zero is not required. A neighbour that would
    actually contribute and is missing leaves the height empty.
    """
    res = float(spec["resolution_m"])
    fc = (np.asarray(xs, dtype=np.float64) - float(spec["xmin"])) / res - 0.5
    fr = (float(spec["ymax"]) - np.asarray(ys, dtype=np.float64)) / res - 0.5
    shape = fc.shape
    fc = np.ravel(fc)
    fr = np.ravel(fr)
    c0 = np.floor(fc).astype(np.int32)
    r0 = np.floor(fr).astype(np.int32)
    dc = fc - c0
    dr = fr - r0
    height, width = elev.shape
    c1 = c0 + 1
    r1 = r0 + 1
    c1 = np.where((dc == 0.0) & (c1 == width), c0, c1)
    r1 = np.where((dr == 0.0) & (r1 == height), r0, r1)
    out = np.full(fc.shape, np.nan, dtype=np.float64)
    inside = (
        (r0 >= 0) & (c0 >= 0) & (r0 < height) & (c0 < width)
        & (r1 >= 0) & (c1 >= 0) & (r1 < height) & (c1 < width)
    )
    if np.any(inside):
        rr = r0[inside]
        cc = c0[inside]
        tx = dc[inside]
        ty = dr[inside]
        z00 = elev[rr, cc].astype(np.float64, copy=False)
        z10 = elev[rr, c1[inside]].astype(np.float64, copy=False)
        z01 = elev[r1[inside], cc].astype(np.float64, copy=False)
        z11 = elev[r1[inside], c1[inside]].astype(np.float64, copy=False)
        w00 = (1.0 - tx) * (1.0 - ty)
        w10 = tx * (1.0 - ty)
        w01 = (1.0 - tx) * ty
        w11 = tx * ty
        bad = (
            ((w00 > 0.0) & ~np.isfinite(z00))
            | ((w10 > 0.0) & ~np.isfinite(z10))
            | ((w01 > 0.0) & ~np.isfinite(z01))
            | ((w11 > 0.0) & ~np.isfinite(z11))
        )
        z00 = np.where(np.isfinite(z00), z00, 0.0)
        z10 = np.where(np.isfinite(z10), z10, 0.0)
        z01 = np.where(np.isfinite(z01), z01, 0.0)
        z11 = np.where(np.isfinite(z11), z11, 0.0)
        mixed = z00 * w00 + z10 * w10 + z01 * w01 + z11 * w11
        mixed[bad] = np.nan
        out[np.flatnonzero(inside)] = mixed
    return out.reshape(shape)


def _roughness(xs: np.ndarray, ys: np.ndarray, elev: np.ndarray, spec: dict) -> np.ndarray:
    """Absolute deviation from the local 1.5 m mean. A uniform slope scores near zero."""
    res = spec["resolution_m"]
    col = np.floor((xs - spec["xmin"]) / res).astype(np.int32)
    row = np.floor((spec["ymax"] - ys) / res).astype(np.int32)
    height, width = elev.shape
    acc = np.zeros(xs.shape, dtype=np.float64)
    cnt = np.zeros(xs.shape, dtype=np.float64)
    center = np.full(xs.shape, np.nan, dtype=np.float64)
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            rr = row + dr
            cc = col + dc
            ok = (rr >= 0) & (cc >= 0) & (rr < height) & (cc < width)
            vals = np.full(xs.shape, np.nan, dtype=np.float64)
            vals[ok] = elev[rr[ok], cc[ok]]
            finite = np.isfinite(vals)
            acc[finite] += vals[finite]
            cnt[finite] += 1.0
            if dr == 0 and dc == 0:
                center = vals
    enough = cnt >= 6.0
    mean = np.divide(acc, cnt, out=np.full_like(acc, np.nan), where=enough)
    rough = np.abs(center - mean)
    rough[~enough | ~np.isfinite(center)] = np.nan
    return rough


def _kind_of(text) -> str:
    folded = (
        str(text or "")
        .lower()
        .replace("ü", "ue")
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ß", "ss")
    )
    return _kind(folded)


def _suggest(kind: str, length: float, dip: float) -> str:
    if kind == "bridge":
        if np.isfinite(dip) and dip >= DIP_OPEN_M:
            return "bridge"
        if length <= CULVERT_MAX_M:
            return "culvert"
        return "check"
    return kind or "unknown"


def _area_parts(geom) -> list:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        out = []
        for part in geom.geoms:
            out.extend(_area_parts(part))
        return out
    return []


def _lines_of(geom) -> list:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "MultiLineString":
        merged = shapely.line_merge(geom)
        if merged.geom_type == "LineString":
            return [merged]
        return [g for g in merged.geoms if g.geom_type == "LineString"]
    return []


def _kinks(line: LineString) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    length = float(line.length)
    if length < 2.0 * KINK_BASE_M + 1.0:
        return np.array([]), np.array([]), np.array([])
    station = np.arange(KINK_BASE_M, length - KINK_BASE_M + 1e-6, KINK_STEP_M)
    before = shapely.line_interpolate_point(line, station - KINK_BASE_M)
    here = shapely.line_interpolate_point(line, station)
    after = shapely.line_interpolate_point(line, station + KINK_BASE_M)
    ax = shapely.get_x(here) - shapely.get_x(before)
    ay = shapely.get_y(here) - shapely.get_y(before)
    bx = shapely.get_x(after) - shapely.get_x(here)
    by = shapely.get_y(after) - shapely.get_y(here)
    ok = (np.hypot(ax, ay) > 0.5) & (np.hypot(bx, by) > 0.5)
    b1 = np.degrees(np.arctan2(ax, ay)) % 360.0
    b2 = np.degrees(np.arctan2(bx, by)) % 360.0
    # Both bearings point along the chain, so 0 degrees is straight.
    turn = np.abs(b1 - b2) % 360.0
    turn = np.minimum(turn, 360.0 - turn)
    turn[~ok] = np.nan
    return shapely.get_x(here), shapely.get_y(here), turn


def _nearest_junction(xs: np.ndarray, ys: np.ndarray, jx: np.ndarray, jy: np.ndarray) -> np.ndarray:
    if xs.size == 0 or jx.size == 0:
        return np.full(xs.shape, np.inf)
    return np.hypot(xs[:, None] - jx[None, :], ys[:, None] - jy[None, :]).min(axis=1)


def _road_name(short_ids: str, name_of: dict[int, str]) -> str:
    names = []
    for part in str(short_ids).split(","):
        if not part:
            continue
        text = name_of.get(int(part), "")
        if text and text not in names:
            names.append(text)
    return ", ".join(names)


def _half_widths(station: np.ndarray, width_m, width_along) -> np.ndarray:
    """Half the stored carriageway width, inset so the sample stays on the surface."""
    text = "" if width_along is None else str(width_along)
    if text in ("", "None", "nan"):
        width = np.full(station.shape, float(width_m))
    else:
        runs = []
        for part in text.split(","):
            station_m, width = part.split(":")
            runs.append((float(station_m), float(width)))
        width = np.empty(station.shape)
        j = 0
        for i, at in enumerate(station):
            while j + 1 < len(runs) and runs[j + 1][0] <= at + 1e-6:
                j += 1
            width[i] = runs[j][1]
    return width / 2.0 - EDGE_INSET_M


def _score_chain(line, poly, struct, width_m, width_along, elev, spec):
    """Cross difference of the stored edges, and roughness between them.

    Stations on a bridge, tunnel or gallery are left out. A station whose
    edges fall outside this chain's polygon was cut away by a higher rank.
    """
    length = float(line.length)
    empty = {
        "cross": np.array([]),
        "rough": np.array([]),
        "rough_w": np.array([]),
        "worst": None,
        "n_cross": 0,
        "n_skip_struct": 0,
    }
    if length < STATION_M or width_m is None or not np.isfinite(float(width_m)):
        return empty
    station = np.arange(STATION_M, max(STATION_M, length - STATION_M) + 1e-6, STATION_M)
    if station.size == 0:
        return empty
    half = _half_widths(station, width_m, width_along)
    here = shapely.line_interpolate_point(line, station)
    before = shapely.line_interpolate_point(line, np.clip(station - 1.0, 0.0, length))
    after = shapely.line_interpolate_point(line, np.clip(station + 1.0, 0.0, length))
    dx = shapely.get_x(after) - shapely.get_x(before)
    dy = shapely.get_y(after) - shapely.get_y(before)
    norm = np.hypot(dx, dy)
    ok_dir = norm > 1e-6
    nx = np.zeros_like(dx)
    ny = np.zeros_like(dy)
    nx[ok_dir] = -dy[ok_dir] / norm[ok_dir]
    ny[ok_dir] = dx[ok_dir] / norm[ok_dir]
    hx = shapely.get_x(here)
    hy = shapely.get_y(here)
    on_struct = np.zeros(station.shape, dtype=bool)
    if struct is not None and not struct.is_empty:
        on_struct = shapely.distance(struct, here) <= STRUCT_SKIP_M
    wide = half >= 0.4
    lx = hx + nx * half
    ly = hy + ny * half
    rx = hx - nx * half
    ry = hy - ny * half
    on_poly = np.ones(station.shape, dtype=bool)
    if poly is not None:
        on_poly = shapely.contains(poly, shapely.points(lx, ly)) & shapely.contains(poly, shapely.points(rx, ry))
    use = ok_dir & wide & on_poly & ~on_struct
    zl = _z_at(lx, ly, elev, spec)
    zr = _z_at(rx, ry, elev, spec)
    cross = np.full(station.shape, np.nan)
    both = use & np.isfinite(zl) & np.isfinite(zr)
    cross[both] = np.abs(zl[both] - zr[both])
    finite = np.isfinite(cross)
    worst = None
    if np.any(finite):
        i = int(np.nanargmax(cross))
        worst = (float(hx[i]), float(hy[i]), float(cross[i]))
    reach = float(np.nanmax(half)) if np.any(wide) else 0.0
    rough = np.array([])
    if reach >= 0.4 and np.any(use):
        offsets = np.arange(-reach, reach + 1e-6, ACROSS_STEP_M)
        px = hx[:, None] + nx[:, None] * offsets[None, :]
        py = hy[:, None] + ny[:, None] * offsets[None, :]
        keep = (np.abs(offsets)[None, :] <= half[:, None] + 1e-6) & use[:, None]
        if poly is not None:
            keep &= shapely.contains(poly, shapely.points(px, py))
        if np.any(keep):
            rough = _roughness(px[keep], py[keep], elev, spec)
    return {
        "cross": cross[finite],
        "rough": rough,
        "rough_w": np.full(rough.shape, STATION_M * ACROSS_STEP_M),
        "worst": worst,
        "n_cross": int(finite.sum()),
        "n_skip_struct": int((ok_dir & wide & on_poly & on_struct).sum()),
    }


def _load_surface(proc: Path):
    names = (
        "surface.gpkg",
        "surface_next.gpkg",
        "surface_junc.gpkg",
        "surface_curve.gpkg",
        "surface_meet.gpkg",
        "surface_width.gpkg",
    )
    found = [proc / "link_surface" / name for name in names if (proc / "link_surface" / name).is_file()]
    path = None
    fit = None
    for candidate in reversed(found):
        frame = gpd.read_file(candidate, layer="axis_fit")
        if "width_m" in frame.columns:
            path = candidate
            fit = frame
            break
    if path is None or fit is None:
        raise SystemExit("axis_fit has no width_m; run build_link_surfaces.py --stamp-width")
    surface = gpd.read_file(path, layer="surface")
    try:
        junctions = gpd.read_file(path, layer="junction_point")
    except Exception:
        junctions = gpd.GeoDataFrame(geometry=[], crs=fit.crs)
    return path, fit, surface, junctions


def _write(path: Path, layers: dict[str, gpd.GeoDataFrame]) -> Path:
    for name in ("score.gpkg", "score_next.gpkg"):
        dest = path.parent / name
        if dest.exists():
            try:
                dest.unlink()
            except PermissionError:
                print(f"{name} is open, trying the next file", flush=True)
                continue
        first = True
        for layer, frame in layers.items():
            if frame.empty:
                continue
            frame.to_file(dest, layer=layer, driver="GPKG")
            first = False
        if first:
            raise SystemExit("no score rows")
        return dest
    raise SystemExit(f"could not write a score file in {path.parent}")


def main() -> None:
    site = load_site()
    proc = processed_dir(site)
    src, fit, surface, junctions = _load_surface(proc)
    elev, spec = _load_geotiff(proc / "corridor50_raw" / "corridor50_raw.tif")
    print(f"scoring {src.name}, chains {len(fit)}, surfaces {len(surface)}", flush=True)

    polys: dict[str, list] = defaultdict(list)
    for rec in surface.itertuples(index=False):
        polys[str(rec.short_ids)].append(rec.geometry)
    union = {key: shapely.unary_union(parts) for key, parts in polys.items()}

    jx = np.array([g.x for g in junctions.geometry], dtype=float) if len(junctions) else np.array([])
    jy = np.array([g.y for g in junctions.geometry], dtype=float) if len(junctions) else np.array([])

    routing = gpkg_path(site)
    link_rows = gpd.read_file(routing, layer="link")
    short_of = {str(r.link_id): int(r.short_id) for r in link_rows.itertuples(index=False)}
    name_of: dict[int, str] = {}
    for rec_link in link_rows.itertuples(index=False):
        text = rec_link.name
        if text is None or (isinstance(text, float) and not np.isfinite(text)):
            continue
        text = str(text).strip()
        if not text or text.lower() in ("none", "nan"):
            continue
        name_of[int(rec_link.short_id)] = text
    chain_of: dict[int, int] = {}
    for i, rec in enumerate(fit.itertuples(index=False)):
        for part in str(rec.short_ids).split(","):
            if part:
                chain_of[int(part)] = i

    struct_geom: dict[int, list] = defaultdict(list)
    struct_rows = []
    try:
        refs = gpd.read_file(routing, layer="bridge_ref")
    except Exception:
        refs = gpd.GeoDataFrame()
        print("no bridge_ref layer, structure check skipped", flush=True)
    grouped: dict[str, list] = defaultdict(list)
    for rec in refs.itertuples(index=False):
        grouped[str(rec.ref_id)].append(rec)
        sid = short_of.get(str(rec.link_id))
        idx = chain_of.get(sid) if sid is not None else None
        if idx is not None and rec.geometry is not None:
            struct_geom[idx].append(rec.geometry)

    struct_by_chain = {
        idx: shapely.unary_union(parts) for idx, parts in struct_geom.items() if parts
    }

    chain_rows = []
    kink_rows = []
    worst_rows = []
    all_cross = []
    all_cross_w = []
    all_rough = []
    all_rough_w = []
    all_kink = []
    all_kink_free = []
    for i, rec in enumerate(fit.itertuples(index=False)):
        line = rec.geometry
        if line.geom_type == "MultiLineString":
            line = max(line.geoms, key=lambda g: g.length)
        key = str(rec.short_ids)
        poly = union.get(key)
        struct = struct_by_chain.get(i)
        scored = _score_chain(line, poly, struct, rec.width_m, rec.width_along, elev, spec)
        kx, ky, turn = _kinks(line)
        near = _nearest_junction(kx, ky, jx, jy)
        at_j = near <= JUNCTION_M
        free = turn.copy()
        if free.size:
            free[at_j] = np.nan
        p50, p90, pmax = _percentiles(scored["cross"], (0.5, 0.9, 1.0))
        r50, r90 = _percentiles(scored["rough"], (0.5, 0.9))
        k90, kmax = _percentiles(turn, (0.9, 1.0))
        kfree = _percentiles(free, (1.0,))[0]
        over = scored["cross"] >= CROSS_MARK_M
        over_1 = scored["cross"] >= 1.0
        worst = scored["worst"]
        chain_rows.append(
            {
                "code": rec.code,
                "fc": int(rec.fc),
                "fitted": int(rec.fitted),
                "name": _road_name(key, name_of),
                "width_m": None if rec.width_m is None else round(float(rec.width_m), 2),
                "width_along": None if rec.width_along is None else str(rec.width_along),
                "length_m": round(float(line.length), 1),
                "structure_m": round(float(struct.length), 1) if struct is not None else 0.0,
                "n_cross": scored["n_cross"],
                "n_on_structure": scored["n_skip_struct"],
                "cross_p50_m": p50,
                "cross_p90_m": p90,
                "cross_max_m": pmax,
                "cross_share_0_50": None if scored["n_cross"] == 0 else round(float(np.mean(over)), 3),
                "cross_share_1": None if scored["n_cross"] == 0 else round(float(np.mean(over_1)), 3),
                "worst_x": None if worst is None else round(worst[0], 1),
                "worst_y": None if worst is None else round(worst[1], 1),
                "worst_m": None if worst is None else round(worst[2], 2),
                "rough_p50_m": r50,
                "rough_p90_m": r90,
                "kink_p90_deg": k90,
                "kink_max_deg": kmax,
                "kink_max_free_deg": kfree,
                "short_ids": key,
                "geometry": line,
            }
        )
        if scored["cross"].size:
            all_cross.append(scored["cross"])
            all_cross_w.append(np.full(scored["cross"].shape, STATION_M))
        if scored["rough"].size:
            all_rough.append(scored["rough"])
            all_rough_w.append(scored["rough_w"])
        if turn.size:
            all_kink.append(turn[np.isfinite(turn)])
            all_kink_free.append(free[np.isfinite(free)])
        if scored["worst"] is not None and scored["worst"][2] >= 0.25:
            wx, wy, wz = scored["worst"]
            worst_rows.append(
                {
                    "code": rec.code,
                    "cross_m": round(wz, 3),
                    "short_ids": key,
                    "geometry": Point(wx, wy),
                }
            )
        if turn.size:
            keep = np.isfinite(turn) & (turn >= KINK_POINT_DEG)
            for x, y, deg, junction in zip(kx[keep], ky[keep], turn[keep], at_j[keep]):
                kink_rows.append(
                    {
                        "code": rec.code,
                        "deg": round(float(deg), 1),
                        "at_junction": int(bool(junction)),
                        "short_ids": key,
                        "geometry": Point(float(x), float(y)),
                    }
                )
        if (i + 1) % 50 == 0:
            print(f"  chains {i + 1}", flush=True)

    overlap_rows = []
    items = [(key, union[key]) for key in union]
    if len(items) >= 2:
        geoms = [g for _k, g in items]
        tree = shapely.STRtree(geoms)
        pairs = tree.query(geoms, predicate="intersects")
        meta = {str(r.short_ids): (r.code, int(r.fc)) for r in fit.itertuples(index=False)}
        seen = set()
        for a, b in pairs.T:
            ia, ib = int(a), int(b)
            if ia >= ib:
                continue
            if (ia, ib) in seen:
                continue
            seen.add((ia, ib))
            ka, kb = items[ia][0], items[ib][0]
            if ka == kb:
                continue
            inter = geoms[ia].intersection(geoms[ib])
            if inter.is_empty or inter.area < OVERLAP_MIN_M2:
                continue
            ca, fa = meta.get(ka, ("", -1))
            cb, fb = meta.get(kb, ("", -1))
            for part in _area_parts(inter):
                if part.area < OVERLAP_MIN_M2:
                    continue
                overlap_rows.append(
                    {
                        "code_a": ca,
                        "code_b": cb,
                        "same_rank": int(fa == fb),
                        "area_m2": round(float(part.area), 1),
                        "a": ka,
                        "b": kb,
                        "geometry": part,
                    }
                )

    for ref_id, group in grouped.items():
        parts = []
        shorts = []
        names = []
        for rec in group:
            parts.extend(_lines_of(rec.geometry))
            sid = short_of.get(str(rec.link_id))
            if sid is not None:
                shorts.append(sid)
            if getattr(rec, "name", None):
                names.append(str(rec.name))
        if not parts:
            continue
        length = float(sum(p.length for p in parts))
        dips = [_dip(p, elev, spec) for p in parts]
        dip = max((d for d, _n in dips), default=float("nan"))
        kind = _kind_of(group[0].typ)
        suggest = _suggest(kind, length, dip)
        chain_ids = sorted({chain_of[s] for s in shorts if s in chain_of})
        codes = sorted({str(fit.code.iloc[i]) for i in chain_ids})
        fitted = [int(fit.fitted.iloc[i]) for i in chain_ids]
        centroid = shapely.unary_union(parts).centroid
        struct_rows.append(
            {
                "ref_id": ref_id,
                "kind": kind,
                "suggest": suggest,
                "name": names[0] if names else None,
                "length_m": round(length, 1),
                "dip_m": None if not np.isfinite(dip) else round(float(dip), 2),
                "code": ",".join(codes),
                "fitted": int(any(fitted)) if fitted else 0,
                "short_ids": ",".join(str(s) for s in sorted(set(shorts))),
                "x": round(float(centroid.x), 1),
                "y": round(float(centroid.y), 1),
                "geometry": shapely.line_merge(shapely.unary_union(parts)),
            }
        )

    crs = fit.crs
    def _frame(rows: list) -> gpd.GeoDataFrame:
        if not rows:
            return gpd.GeoDataFrame(geometry=[], crs=crs)
        return gpd.GeoDataFrame(rows, crs=crs)

    layers = {
        "chain": _frame(chain_rows),
        "cross_worst": _frame(worst_rows),
        "kink": _frame(kink_rows),
        "overlap": _frame(overlap_rows),
        "structure": _frame(struct_rows),
    }
    dest = _write(proc / "link_surface" / "score.gpkg", layers)

    cross_v = np.concatenate(all_cross) if all_cross else np.array([])
    cross_w = np.concatenate(all_cross_w) if all_cross_w else np.array([])
    rough_v = np.concatenate(all_rough) if all_rough else np.array([])
    rough_w = np.concatenate(all_rough_w) if all_rough_w else np.array([])
    kink_v = np.concatenate(all_kink) if all_kink else np.array([])
    kink_free_v = np.concatenate([a for a in all_kink_free if a.size]) if any(a.size for a in all_kink_free) else np.array([])
    same = [r for r in overlap_rows if r["same_rank"]]
    other = [r for r in overlap_rows if not r["same_rank"]]
    by_suggest: dict[str, int] = defaultdict(int)
    for row in struct_rows:
        by_suggest[row["suggest"]] += 1
    summary = {
        "source": str(src),
        "chains": len(chain_rows),
        "cross_p50_m": _weighted(cross_v, cross_w, (0.5,))[0],
        "cross_p90_m": _weighted(cross_v, cross_w, (0.9,))[0],
        "cross_p99_m": _weighted(cross_v, cross_w, (0.99,))[0],
        "cross_share_0_50": None if cross_v.size == 0 else round(float(np.mean(cross_v >= CROSS_MARK_M)), 3),
        "cross_share_1": None if cross_v.size == 0 else round(float(np.mean(cross_v >= 1.0)), 3),
        "rough_p50_m": _weighted(rough_v, rough_w, (0.5,))[0],
        "rough_p90_m": _weighted(rough_v, rough_w, (0.9,))[0],
        "rough_p99_m": _weighted(rough_v, rough_w, (0.99,))[0],
        "kink_base_m": KINK_BASE_M,
        "kink_p90_deg": _percentiles(kink_v, (0.9,))[0],
        "kink_p99_deg": _percentiles(kink_v, (0.99,))[0],
        "kink_free_p99_deg": _percentiles(kink_free_v, (0.99,))[0],
        "kink_points_15": len(kink_rows),
        "overlap_same_rank_m2": round(sum(r["area_m2"] for r in same), 1),
        "overlap_same_rank_n": len(same),
        "overlap_other_rank_m2": round(sum(r["area_m2"] for r in other), 1),
        "overlap_other_rank_n": len(other),
        "structures": len(struct_rows),
        "by_suggest": dict(by_suggest),
        "dip_open_m": DIP_OPEN_M,
        "culvert_max_m": CULVERT_MAX_M,
    }
    over_rows = [
        {k: v for k, v in row.items() if k != "geometry"}
        for row in chain_rows
        if (row["cross_max_m"] or 0) >= CROSS_MARK_M
    ]
    over_rows.sort(key=lambda r: (r["code"], -r["length_m"]))
    report = {
        "summary": summary,
        "cross_over_0_50": over_rows,
        "structures": [
            {k: v for k, v in row.items() if k != "geometry"} for row in struct_rows
        ],
    }
    report_path = dest.with_suffix(".json")
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(
        f"cross p50 {summary['cross_p50_m']} m, p90 {summary['cross_p90_m']} m, "
        f"p99 {summary['cross_p99_m']} m, share >= {CROSS_MARK_M:.2f} m {summary['cross_share_0_50']}, "
        f"share >= 1.00 m {summary['cross_share_1']}",
        flush=True,
    )
    n_half = len(over_rows)
    n_one = sum(1 for row in over_rows if (row["cross_max_m"] or 0) >= 1.0)
    print(
        f"chains with a station >= {CROSS_MARK_M:.2f} m: {n_half}; "
        f"chains with a station >= 1.00 m: {n_one}",
        flush=True,
    )
    print("code             len_m  p50  p90   max >=0.5  >=1  worst_xy name short_ids")
    for row in over_rows:
        name = row["name"] or "-"
        worst = (
            f"{row['worst_x']:.0f} {row['worst_y']:.0f}"
            if row["worst_x"] is not None
            else "-"
        )
        print(
            f"{row['code']:16} {row['length_m']:7.0f} {row['cross_p50_m']:5.2f} "
            f"{row['cross_p90_m']:5.2f} {row['cross_max_m']:6.2f} "
            f"{row['cross_share_0_50']:5.2f} {row['cross_share_1']:5.2f} "
            f"{worst} {name} {row['short_ids']}",
            flush=True,
        )
    print(
        f"roughness p50 {summary['rough_p50_m']} m, p90 {summary['rough_p90_m']} m, "
        f"p99 {summary['rough_p99_m']} m",
        flush=True,
    )
    n_vary = sum(1 for row in chain_rows if row["width_along"] and "," in str(row["width_along"]))
    print(f"axes {len(chain_rows)}, with a width change along the chain {n_vary}", flush=True)
    print(
        f"kink on {KINK_BASE_M:.0f} m base, p90 {summary['kink_p90_deg']} deg, "
        f"p99 {summary['kink_p99_deg']} deg, p99 away from a junction {summary['kink_free_p99_deg']} deg, "
        f"points >= {KINK_POINT_DEG:.0f} deg {summary['kink_points_15']}",
        flush=True,
    )
    print(
        f"overlap same rank {summary['overlap_same_rank_n']} parts, {summary['overlap_same_rank_m2']} m2; "
        f"other rank {summary['overlap_other_rank_n']} parts, {summary['overlap_other_rank_m2']} m2",
        flush=True,
    )
    print(f"structures {len(struct_rows)}: " + ", ".join(f"{k} {v}" for k, v in sorted(by_suggest.items())), flush=True)
    print(f"{'kind':12} {'suggest':10} {'len':>7} {'dip':>6} code short_ids")
    for row in sorted(struct_rows, key=lambda r: -r["length_m"]):
        dip_txt = "   -  " if row["dip_m"] is None else f"{row['dip_m']:6.2f}"
        name = f" {row['name']}" if row["name"] else ""
        print(
            f"{row['kind']:12} {row['suggest']:10} {row['length_m']:7.1f} {dip_txt} "
            f"{row['code']} {row['short_ids']}{name}"
        )
    print(
        f"(open the following in qgis or similar tool to check: {dest}, {src})",
        flush=True,
    )


if __name__ == "__main__":
    main()
