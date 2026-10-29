"""Cut GIP link geometry outside Austria or outside the terrain box.

The Swiss country polygon and the Austrian country polygon are projected to
EPSG:25832. Overlap and gap are measured inside the site bbox. A link keeps
the part that lies in the terrain box and is not definitely in Switzerland.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\clip_gip_swiss.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyogrio
import shapely
from pyproj import Transformer
from shapely.geometry import Point, box
from shapely.ops import substring

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from gip_routing_load import gpkg_path  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402

WORK = "EPSG:25832"
SWISS_GPKG = ROOT / "data" / "raw" / "borders" / "swissBOUNDARIES3D_1_5_LV95_LN02.gpkg"
AUSTRIA_GPKG = ROOT / "data" / "raw" / "borders" / "AT_INSPIRE_BEV_AU_AB_epsg3416_20260402.gpkg"
PAD_M = 2000.0
MIN_PART_M = 1.0


def _listed(site: dict) -> bool:
    raw = site.get("authorities")
    if isinstance(raw, dict):
        return "schweiz" in raw
    if isinstance(raw, list):
        return "schweiz" in raw
    return raw == "schweiz"


def _piece(path: Path, layer: str, where: str, window) -> tuple[object, str]:
    frame = gpd.read_file(path, layer=layer, where=where)
    if frame.crs is None:
        raise SystemExit(f"{path.name} layer {layer} has no CRS")
    native = gpd.GeoDataFrame(geometry=[window], crs=WORK).to_crs(frame.crs).geometry.iloc[0]
    geom = shapely.make_valid(shapely.force_2d(frame.geometry.iloc[0]))
    cut = shapely.make_valid(geom.intersection(native))
    out = gpd.GeoDataFrame(geometry=[cut], crs=frame.crs).to_crs(WORK)
    return shapely.make_valid(shapely.force_2d(out.geometry.iloc[0])), str(frame.crs)


def _border_stats(ch, at, frame) -> dict:
    ch_f = shapely.make_valid(ch.intersection(frame))
    at_f = shapely.make_valid(at.intersection(frame))
    overlap = shapely.make_valid(ch_f.intersection(at_f))
    gap = shapely.make_valid(frame.difference(shapely.union_all([ch_f, at_f])))
    edge = ch_f.boundary
    edge = shapely.difference(edge, frame.boundary.buffer(5.0))
    inside = np.array([])
    depth = np.array([])
    if not edge.is_empty and edge.length > 1.0:
        count = max(2, int(edge.length / 10.0))
        pts = shapely.line_interpolate_point(edge, np.linspace(0.0, edge.length, count))
        depth = np.asarray(shapely.distance(pts, at_f.boundary), dtype=np.float64)
        inside = np.asarray(shapely.contains(at_f, pts), dtype=bool)
    return {
        "ch": ch_f,
        "at": at_f,
        "overlap": overlap,
        "gap": gap,
        "overlap_m2": float(overlap.area),
        "gap_m2": float(gap.area),
        "n": int(depth.size),
        "inside": inside,
        "depth": depth,
    }


def _print_stats(stats: dict, ch_crs: str, at_crs: str) -> None:
    print(f"swiss source {SWISS_GPKG.name} {ch_crs}", flush=True)
    print(Transformer.from_crs(ch_crs, WORK, always_xy=True).definition, flush=True)
    print(f"austria source {AUSTRIA_GPKG.name} {at_crs}", flush=True)
    print(Transformer.from_crs(at_crs, WORK, always_xy=True).definition, flush=True)
    print(
        f"inside site bbox: overlap {stats['overlap_m2']:.1f} m2, gap {stats['gap_m2']:.1f} m2",
        flush=True,
    )
    depth = stats["depth"]
    inside = stats["inside"]
    if depth.size == 0:
        print("no swiss border samples inside the site bbox", flush=True)
        return
    print(
        f"border samples {depth.size}, median separation {float(np.median(depth)):.2f} m, "
        f"max {float(depth.max()):.2f} m",
        flush=True,
    )
    if np.any(inside):
        print(
            f"samples inside Austria {int(inside.sum())}, max overlap depth {float(depth[inside].max()):.2f} m",
            flush=True,
        )
    outside = ~inside
    if np.any(outside):
        print(
            f"samples outside Austria {int(outside.sum())}, max gap width {float(depth[outside].max()):.2f} m",
            flush=True,
        )


def _lines(geom) -> list:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "GeometryCollection":
        geom = shapely.union_all([part for part in geom.geoms if "Line" in part.geom_type])
        if geom.is_empty:
            return []
    if geom.geom_type == "LineString":
        return [geom] if geom.length >= MIN_PART_M else []
    if geom.geom_type == "MultiLineString":
        return [part for part in geom.geoms if part.length >= MIN_PART_M]
    return []


def _coords(geom) -> list:
    if geom.geom_type == "LineString":
        return list(geom.coords)
    if geom.geom_type == "MultiLineString":
        coords = []
        for part in geom.geoms:
            coords.extend(part.coords)
        return coords
    if geom.geom_type == "GeometryCollection":
        coords = []
        for part in geom.geoms:
            coords.extend(_coords(part))
        return coords
    return []


def _pct(value, default: float) -> float:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return default
    return float(value)


def _cut_refs(original, part, refs: list[dict]) -> list[dict]:
    kept = []
    length = float(original.length)
    part_length = float(part.length)
    for row in refs:
        start = _pct(row.get("from_pct"), 0.0)
        end = _pct(row.get("to_pct"), 100.0)
        lo, hi = sorted((start, end))
        label = substring(original, lo / 100.0 * length, hi / 100.0 * length)
        hit = label.intersection(part)
        if hit.is_empty or getattr(hit, "length", 0.0) < 0.5:
            continue
        stations = [part.project(Point(xy[:2])) for xy in _coords(hit)]
        if not stations:
            continue
        a, b = min(stations), max(stations)
        if b - a < 0.5:
            continue
        copied = dict(row)
        copied["from_pct"] = a / part_length * 100.0
        copied["to_pct"] = b / part_length * 100.0
        copied["geometry"] = substring(part, a, b)
        kept.append(copied)
    return kept


def _apply(links: gpd.GeoDataFrame, refs: gpd.GeoDataFrame, keep) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, dict]:
    shapely.prepare(keep)
    by_link: dict[str, list[dict]] = {}
    for row in refs.to_dict("records"):
        by_link.setdefault(str(row["link_id"]), []).append(row)
    link_rows = []
    ref_rows = []
    cut = 0
    dropped = 0
    parts_n = 0
    for rec in links.to_dict("records"):
        geom = rec["geometry"]
        lid = str(rec["link_id"])
        if geom is None or geom.is_empty or not shapely.intersects(geom, keep):
            dropped += 1
            continue
        parts = _lines(shapely.intersection(geom, keep))
        if not parts:
            dropped += 1
            continue
        if len(parts) == 1 and abs(float(parts[0].length) - float(geom.length)) <= 0.05:
            link_rows.append(rec)
            ref_rows.extend(by_link.get(lid, []))
            continue
        cut += 1
        parts_n += len(parts)
        ordered = sorted(parts, key=lambda part: geom.project(Point(part.coords[0])))
        for index, part in enumerate(ordered, start=1):
            row = dict(rec)
            row["geometry"] = part
            row["length_m"] = float(part.length)
            row["link_id"] = lid if index == 1 else f"{lid}~{index}"
            owned = _cut_refs(geom, part, by_link.get(lid, []))
            row["n_struct"] = len(owned)
            link_rows.append(row)
            for ref in owned:
                ref["link_id"] = row["link_id"]
                ref_rows.append(ref)
    out_links = gpd.GeoDataFrame(link_rows, geometry="geometry", crs=links.crs)
    out_refs = gpd.GeoDataFrame(ref_rows, geometry="geometry", crs=refs.crs)
    return out_links, out_refs, {"cut": cut, "dropped": dropped, "parts": parts_n, "kept": len(out_links)}


def _write(path: Path, layers: dict[str, gpd.GeoDataFrame]) -> Path:
    tmp = path.with_name(path.stem + "_clip_tmp.gpkg")
    if tmp.exists():
        tmp.unlink()
    first = True
    for name, frame in layers.items():
        frame.to_file(tmp, layer=name, driver="GPKG", mode="w" if first else "a")
        first = False
    node = layers["node"].geometry.iloc[0]
    back = gpd.read_file(tmp, layer="node").geometry.iloc[0]
    if abs(node.x - back.x) > 1e-4 or abs(node.y - back.y) > 1e-4:
        tmp.unlink()
        raise SystemExit("rewritten node moved; GIP file left unchanged")
    try:
        tmp.replace(path)
    except PermissionError:
        dest = path.with_name(path.stem + "_clipped.gpkg")
        try:
            if dest.exists():
                dest.unlink()
        except PermissionError:
            dest = path.with_name(path.stem + "_clipped_box.gpkg")
            if dest.exists():
                dest.unlink()
        tmp.replace(dest)
        print(f"{path.name} is open, wrote {dest}", flush=True)
        return dest
    stale = path.with_name(path.stem + "_clipped.gpkg")
    if stale.exists():
        stale.unlink()
    return path


def exclude_swiss(site: dict) -> Path:
    """Remove Swiss pieces from the site GIP. Returns the GIP file to read."""
    if not _listed(site):
        return gpkg_path(site)
    if not SWISS_GPKG.is_file() or not AUSTRIA_GPKG.is_file():
        raise SystemExit(f"missing country polygons {SWISS_GPKG} or {AUSTRIA_GPKG}")
    xmin, ymin, xmax, ymax = (float(v) for v in site["bbox"])
    frame = box(xmin, ymin, xmax, ymax)
    window = box(xmin - PAD_M, ymin - PAD_M, xmax + PAD_M, ymax + PAD_M)
    ch, ch_crs = _piece(SWISS_GPKG, "tlm_landesgebiet", "icc = 'CH'", window)
    at, at_crs = _piece(AUSTRIA_GPKG, "AU_flat_view", "nationalLevel = '1stOrder'", window)
    stats = _border_stats(ch, at, frame)
    _print_stats(stats, str(ch_crs), str(at_crs))
    folder = processed_dir(site) / "link_surface" / "check_grade"
    folder.mkdir(parents=True, exist_ok=True)
    border = folder / "country_border.gpkg"
    if border.exists():
        try:
            border.unlink()
        except PermissionError:
            border = folder / "country_border_next.gpkg"
    first = True
    for name in ("ch", "at", "overlap", "gap"):
        geom = stats[name]
        if geom.is_empty:
            continue
        gpd.GeoDataFrame({"kind": [name]}, geometry=[geom], crs=WORK).to_file(
            border, layer=name, driver="GPKG", mode="w" if first else "a"
        )
        first = False
    print(
        f"(open the following in qgis or similar tool to check: {border})",
        flush=True,
    )

    # Heights exist for xmin <= x < xmax and ymin < y <= ymax.
    terrain = box(xmin, ymin + 0.01, xmax - 0.01, ymax)
    keep = shapely.make_valid(terrain.difference(ch.difference(at)))
    path = gpkg_path(site)
    names = [row[0] for row in pyogrio.list_layers(path)]
    layers = {name: gpd.read_file(path, layer=name) for name in names}
    source_links = layers.get("link_before_ch", layers["link"])
    source_refs = layers.get("bridge_ref_before_ch", layers["bridge_ref"])
    layers["link_before_ch"] = source_links
    layers["bridge_ref_before_ch"] = source_refs
    layers["link"], layers["bridge_ref"], counts = _apply(source_links, source_refs, keep)
    print(
        f"GIP links {len(source_links)} -> {counts['kept']}, "
        f"cut {counts['cut']}, dropped {counts['dropped']}, parts {counts['parts']}",
        flush=True,
    )
    return _write(path, layers)


def main() -> None:
    exclude_swiss(load_site())


if __name__ == "__main__":
    main()
