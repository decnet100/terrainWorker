"""Seed annotations GPKG from OSM/heuristic offsets (draft for QGIS editing).

Writes centerline, road_edge, and guardrail LineStrings in site CRS.
Does NOT run from build_smoke / build_guardrails — only this script.

Safety: refuses to overwrite non-empty layers unless --force is passed.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import SiteCoords, annotations_gpkg, load_site, processed_dir  # noqa: E402
from init_annotations_gpkg import create_gpkg  # noqa: E402
from road_edge import (  # noqa: E402
    edge_xy_at_s,
    prepare_roads_for_edge,
)
from guardrail_rules import (  # noqa: E402
    detect_steep_sides,
    enrich_prepared_roads,
    is_steep_sides,
    resolve_rail_cfg,
    side_signs as rule_side_signs,
)
from PIL import Image
import numpy as np


SITE = load_site()
PROC = processed_dir(SITE)
BNG = SITE.get("beamng", {})
GR = BNG.get("guardrails", {}) or {}
ANN = SITE.get("annotations") or {}
COORDS = SiteCoords(SITE)
_DEC = BNG.get("decal_roads") or {}
_DEC_DEF = _DEC.get("defaults") if isinstance(_DEC.get("defaults"), dict) else _DEC

CRS = COORDS.crs
XMIN, YMIN = COORDS.xmin, COORDS.ymin
BW, BH = COORDS.bw, COORDS.bh
TERRAIN_EXTENT = COORDS.terrain_extent
ROAD_WIDTH_SCALE = float(BNG.get("road_width_scale", 1.0))
LATERAL_EXTRA = float(GR.get("lateral_extra_m", 0.55))
HIGHWAYS = set(
    GR.get(
        "highways",
        ["motorway", "trunk", "primary", "secondary", "tertiary"],
    )
)
SIDES = GR.get("sides", "both")
SECTION_LEN = float(GR.get("section_length_m", 4.2))
SAMPLE_STEP_M = float(ANN.get("seed_sample_step_m", 2.0))
STITCH_ABUTTING = bool(
    GR.get("stitch_abutting")
    if GR.get("stitch_abutting") is not None
    else _DEC_DEF.get("stitch_abutting", True)
)
STITCH_TOL_M = float(
    GR.get("stitch_tol_m")
    if GR.get("stitch_tol_m") is not None
    else (
        _DEC_DEF.get("stitch_tol_m")
        if _DEC_DEF.get("stitch_tol_m") is not None
        else 1.25
    )
)
WIDTH_FILL_DIP_M = float(
    GR.get("width_fill_dip_m")
    if GR.get("width_fill_dip_m") is not None
    else (
        _DEC_DEF.get("width_fill_dip_m")
        if _DEC_DEF.get("width_fill_dip_m") is not None
        else 40.0
    )
)
WIDTH_BLEND_M = float(
    GR.get("width_blend_m")
    if GR.get("width_blend_m") is not None
    else (
        _DEC_DEF.get("width_blend_m")
        if _DEC_DEF.get("width_blend_m") is not None
        else 25.0
    )
)
DENSIFY_MAX_STEP_M = float(
    GR.get("densify_max_step_m")
    if GR.get("densify_max_step_m") is not None
    else (
        _DEC_DEF.get("densify_max_step_m")
        if _DEC_DEF.get("densify_max_step_m") is not None
        else 12.0
    )
)
LANE_WIDTH_M = float(BNG.get("lane_width_m") if BNG.get("lane_width_m") is not None else 3.75)
RAIL_RULES = list(GR.get("rules") or GR.get("items") or [])
RAIL_DEFAULTS = {
    "sides": SIDES,
    "present": True,
    "lateral_extra_m": LATERAL_EXTRA,
    "section_length_m": SECTION_LEN,
}
_STEEP_RAW = GR.get("steep_sides") if isinstance(GR.get("steep_sides"), dict) else {}
STEEP_SAMPLE_M = float(
    _STEEP_RAW.get("sample_m") if _STEEP_RAW.get("sample_m") is not None else 250.0
)
STEEP_LOOK_OUT_M = float(
    _STEEP_RAW.get("look_out_m") if _STEEP_RAW.get("look_out_m") is not None else 6.0
)
STEEP_DROP_M = float(
    _STEEP_RAW.get("drop_m") if _STEEP_RAW.get("drop_m") is not None else 2.5
)
STEEP_MIN_HITS = int(
    _STEEP_RAW.get("min_hits") if _STEEP_RAW.get("min_hits") is not None else 1
)
STEEP_FALLBACK = str(_STEEP_RAW.get("fallback") or "both").lower().strip()

SEED_LAYERS = ("centerline", "road_edge", "guardrail")


def _load_heightmap_z() -> tuple[np.ndarray, float] | None:
    hm_path = PROC / f"heightmap_{int(BNG.get('mask_size', 512))}.png"
    meta_path = PROC / "heightmap_meta.json"
    if not hm_path.exists() or not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    max_h = float(meta.get("max_height_m", BNG.get("max_height_m", 254.75)))
    arr = np.array(Image.open(hm_path))
    if arr.dtype != np.uint16:
        arr = arr.astype(np.uint16)
    return arr, max_h


def _heightmap_z(hm: np.ndarray, max_h: float, bx: float, by: float) -> float:
    size = hm.shape[0]
    px = bx / TERRAIN_EXTENT * (size - 1)
    py = (1.0 - by / TERRAIN_EXTENT) * (size - 1)
    x0 = int(math.floor(px))
    y0 = int(math.floor(py))
    x1 = min(x0 + 1, size - 1)
    y1 = min(y0 + 1, size - 1)
    x0 = max(0, min(x0, size - 1))
    y0 = max(0, min(y0, size - 1))
    tx, ty = px - x0, py - y0
    v00 = float(hm[y0, x0])
    v10 = float(hm[y0, x1])
    v01 = float(hm[y1, x0])
    v11 = float(hm[y1, x1])
    v = (
        v00 * (1 - tx) * (1 - ty)
        + v10 * tx * (1 - ty)
        + v01 * (1 - tx) * ty
        + v11 * tx * ty
    )
    return (v / 65535.0) * max_h


def _gpkg_path() -> Path:
    return annotations_gpkg(SITE)


def _beamng_to_crs(bx: float, by: float) -> tuple[float, float]:
    return COORDS.beamng_to_crs(bx, by)


def _polyline_length_xy(nodes: list[list[float]]) -> float:
    total = 0.0
    for i in range(len(nodes) - 1):
        total += math.hypot(nodes[i + 1][0] - nodes[i][0], nodes[i + 1][1] - nodes[i][1])
    return total


def _sample_dists(total: float, step: float) -> list[float]:
    if total < step:
        return [0.0, total] if total >= 0.5 else []
    dists = [0.0]
    d = step
    while d < total - 0.05:
        dists.append(d)
        d += step
    if dists[-1] < total - 1e-6:
        dists.append(total)
    return dists


def _side_signs() -> list[tuple[str, float]]:
    return rule_side_signs(SIDES)


def _offset_line_crs(
    nodes: list[list[float]],
    sign: float,
    step: float,
    *,
    lateral_extra_m: float,
) -> LineString | None:
    """Offset polyline using width-at-s (lane ramps) + optional lateral_extra."""
    total = _polyline_length_xy(nodes)
    dists = _sample_dists(total, step)
    if len(dists) < 2:
        return None
    coords = []
    for dist in dists:
        edge = edge_xy_at_s(
            nodes,
            dist,
            sign,
            road_width_scale=ROAD_WIDTH_SCALE,
            lateral_extra_m=lateral_extra_m,
        )
        if not edge:
            continue
        bx, by = edge[0], edge[1]
        coords.append(_beamng_to_crs(bx, by))
    if len(coords) < 2:
        return None
    return LineString(coords)


def _centerline_crs(nodes: list[list[float]]) -> LineString | None:
    if len(nodes) < 2:
        return None
    coords = [_beamng_to_crs(n[0], n[1]) for n in nodes]
    return LineString(coords)


def _layer_counts(path: Path) -> dict[str, int]:
    counts = {}
    for name in SEED_LAYERS:
        try:
            gdf = gpd.read_file(path, layer=name)
            counts[name] = len(gdf)
        except Exception:  # noqa: BLE001
            counts[name] = -1
    return counts


def _replace_layer(path: Path, layer: str, gdf: gpd.GeoDataFrame) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(f'DELETE FROM "{layer}"')
        conn.commit()
    if len(gdf) == 0:
        return
    gdf.to_file(path, layer=layer, driver="GPKG", mode="a")


def build_frames(roads: dict) -> dict[str, gpd.GeoDataFrame]:
    center_rows: list[dict] = []
    edge_rows: list[dict] = []
    rail_rows: list[dict] = []

    prepared = prepare_roads_for_edge(
        roads,
        highways=HIGHWAYS,
        stitch_abutting=STITCH_ABUTTING,
        stitch_tol_m=STITCH_TOL_M,
        width_fill_dip_m=WIDTH_FILL_DIP_M,
        width_blend_m=WIDTH_BLEND_M,
        densify_max_step_m=DENSIFY_MAX_STEP_M,
    )
    prepared = enrich_prepared_roads(prepared, roads, lane_width_m=LANE_WIDTH_M)
    hm_pack = _load_heightmap_z() if is_steep_sides(SIDES) else None

    def _z_at(bx: float, by: float) -> float:
        assert hm_pack is not None
        hm, max_h = hm_pack
        return _heightmap_z(hm, max_h, bx, by)

    for road in prepared:
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        cfg = resolve_rail_cfg(
            road,
            defaults=RAIL_DEFAULTS,
            rules=RAIL_RULES,
            lane_width_m=LANE_WIDTH_M,
        )
        sides_mode = str(cfg.get("sides") or SIDES)
        if is_steep_sides(sides_mode):
            if hm_pack is None:
                sides_mode = STEEP_FALLBACK
            else:
                sides_mode = detect_steep_sides(
                    nodes,
                    _z_at,
                    sample_m=STEEP_SAMPLE_M,
                    look_out_m=STEEP_LOOK_OUT_M,
                    drop_m=STEEP_DROP_M,
                    road_width_scale=ROAD_WIDTH_SCALE,
                    min_hits=STEEP_MIN_HITS,
                )
            cfg = dict(cfg)
            cfg["sides"] = sides_mode

        hw = road.get("highway", "")
        osm_id = road.get("osm_id")
        road_ref = str(osm_id) if osm_id is not None else "road"
        # Attribute: mean width after blend (geometry uses per-s width).
        widths = [float(n[3]) for n in nodes if len(n) > 3]
        width_attr = (
            sum(widths) / len(widths) if widths else 6.0
        ) * ROAD_WIDTH_SCALE

        cl = _centerline_crs(nodes)
        if cl is not None:
            center_rows.append(
                {
                    "id": f"cl_{road_ref}",
                    "width_m": width_attr,
                    "highway": hw,
                    "notes": "seed:heuristic edge_at_s",
                    "geometry": cl,
                }
            )

        # road_edge: always both sides (asphalt edge), independent of rail sides
        for side_name, sign in rule_side_signs("both"):
            edge = _offset_line_crs(
                nodes, sign, SAMPLE_STEP_M, lateral_extra_m=0.0
            )
            if edge is not None:
                edge_rows.append(
                    {
                        "id": f"edge_{road_ref}_{side_name}",
                        "side": side_name,
                        "road_ref": road_ref,
                        "source": "derived",
                        "notes": "seed:heuristic width(s)/2",
                        "geometry": edge,
                    }
                )

        if cfg.get("present") is False or cfg.get("sides") == "none":
            continue
        lat = float(
            cfg["lateral_extra_m"]
            if cfg.get("lateral_extra_m") is not None
            else LATERAL_EXTRA
        )
        section = float(cfg.get("section_length_m") or SECTION_LEN)
        for side_name, sign in rule_side_signs(cfg.get("sides") or SIDES):
            rail = _offset_line_crs(
                nodes, sign, SAMPLE_STEP_M, lateral_extra_m=lat
            )
            if rail is not None:
                rail_rows.append(
                    {
                        "id": f"rail_{road_ref}_{side_name}",
                        "side": side_name,
                        "present": True,
                        "kind": "wbeam",
                        "gap_reason": None,
                        "road_ref": road_ref,
                        "section_m": section,
                        "notes": f"seed:heuristic sides={cfg.get('sides')}",
                        "geometry": rail,
                    }
                )

    return {
        "centerline": gpd.GeoDataFrame(center_rows, geometry="geometry", crs=CRS),
        "road_edge": gpd.GeoDataFrame(edge_rows, geometry="geometry", crs=CRS),
        "guardrail": gpd.GeoDataFrame(rail_rows, geometry="geometry", crs=CRS),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing features in the annotations GPKG (required if not empty)",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="GPKG path (default: annotations.gpkg from site.yaml)",
    )
    args = ap.parse_args()

    out = Path(args.out) if args.out else _gpkg_path()
    if not out.is_absolute():
        out = ROOT / out

    roads_path = PROC / "roads_beamng.json"
    if not roads_path.exists():
        raise SystemExit(f"Missing {roads_path} — run build_smoke.py first")

    if not out.exists():
        print(f"GPKG missing — creating empty schema at {out}")
        create_gpkg(out, CRS)

    counts = _layer_counts(out)
    nonempty = {k: v for k, v in counts.items() if v > 0}
    if nonempty and not args.force:
        raise SystemExit(
            "Annotations GPKG already has features "
            f"({nonempty}). Refusing to overwrite.\n"
            "Re-run with --force only if you intentionally want to replace edits:\n"
            "  python tools/seed_annotations.py --force"
        )

    roads = json.loads(roads_path.read_text(encoding="utf-8"))
    frames = build_frames(roads)
    for name, gdf in frames.items():
        _replace_layer(out, name, gdf)
        print(f"  {name}: {len(gdf)} features")

    print(f"Seeded {out}")
    print(
        f"CRS={CRS} sample_step_m={SAMPLE_STEP_M} "
        f"lateral_extra_m={LATERAL_EXTRA} (guardrail only) "
        f"width_blend_m={WIDTH_BLEND_M}"
    )
    print(
        "Edit in QGIS. Normal builds never call this seed "
        "(re-seed only with --force)."
    )

if __name__ == "__main__":
    main()
