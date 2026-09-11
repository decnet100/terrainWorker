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
import yaml
from shapely.geometry import LineString

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from init_annotations_gpkg import create_gpkg  # noqa: E402

SITE = yaml.safe_load((ROOT / "config" / "site.yaml").read_text(encoding="utf-8"))
PROC = ROOT / "data" / "processed"
BNG = SITE.get("beamng", {})
GR = BNG.get("guardrails", {}) or {}
ANN = SITE.get("annotations") or {}

CRS = str(SITE.get("crs", "EPSG:31254"))
BBOX = list(map(float, SITE["bbox"]))
XMIN, YMIN, XMAX, YMAX = BBOX
BW, BH = XMAX - XMIN, YMAX - YMIN
MPP = float(BNG.get("meters_per_pixel", 1.0))
HM_SIZE = int(BNG.get("mask_size", 512))
TERRAIN_EXTENT = HM_SIZE * MPP
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
# Dense vertices for editable GIS lines (not mesh joint spacing).
SAMPLE_STEP_M = float(ANN.get("seed_sample_step_m", 2.0))

SEED_LAYERS = ("centerline", "road_edge", "guardrail")


def _gpkg_path() -> Path:
    rel = ANN.get("gpkg") or f"data/annotations/{SITE.get('name', 'annotations')}.gpkg"
    p = Path(str(rel).replace(" ", "_"))
    return p if p.is_absolute() else ROOT / p


def _beamng_to_crs(bx: float, by: float) -> tuple[float, float]:
    lx = bx / TERRAIN_EXTENT * BW
    ly = by / TERRAIN_EXTENT * BH
    return XMIN + lx, YMIN + ly


def _polyline_length_xy(nodes: list[list[float]]) -> float:
    total = 0.0
    for i in range(len(nodes) - 1):
        total += math.hypot(nodes[i + 1][0] - nodes[i][0], nodes[i + 1][1] - nodes[i][1])
    return total


def _sample_at(nodes: list[list[float]], dist: float) -> tuple[float, float, float] | None:
    if len(nodes) < 2:
        return None
    acc = 0.0
    for i in range(len(nodes) - 1):
        x0, y0, z0 = nodes[i][0], nodes[i][1], nodes[i][2]
        x1, y1, z1 = nodes[i + 1][0], nodes[i + 1][1], nodes[i + 1][2]
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        if dist <= acc + length + 1e-9:
            t = max(0.0, min(1.0, (dist - acc) / length))
            return (x0 + dx * t, y0 + dy * t, z0 + (z1 - z0) * t)
        acc += length
    return (nodes[-1][0], nodes[-1][1], nodes[-1][2])


def _tangent_xy(nodes: list[list[float]], dist: float) -> tuple[float, float]:
    total = _polyline_length_xy(nodes)
    d0 = max(0.0, dist - 0.5)
    d1 = min(total, dist + 0.5)
    p0 = _sample_at(nodes, d0)
    p1 = _sample_at(nodes, d1)
    if not p0 or not p1:
        return (1.0, 0.0)
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    L = math.hypot(dx, dy)
    if L < 1e-12:
        return (1.0, 0.0)
    return (dx / L, dy / L)


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
    if SIDES == "left":
        return [("left", 1.0)]
    if SIDES == "right":
        return [("right", -1.0)]
    return [("left", 1.0), ("right", -1.0)]


def _offset_line_crs(
    nodes: list[list[float]], sign: float, offset_m: float, step: float
) -> LineString | None:
    total = _polyline_length_xy(nodes)
    dists = _sample_dists(total, step)
    if len(dists) < 2:
        return None
    coords = []
    for dist in dists:
        p = _sample_at(nodes, dist)
        if not p:
            continue
        tx, ty = _tangent_xy(nodes, dist)
        lnx, lny = -ty, tx
        bx = p[0] + lnx * sign * offset_m
        by = p[1] + lny * sign * offset_m
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

    for road_key, road in roads.items():
        hw = road.get("highway", "")
        if hw not in HIGHWAYS:
            continue
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        osm_id = road.get("osm_id")
        road_ref = str(osm_id) if osm_id is not None else str(road_key)
        width = float(nodes[0][3]) if len(nodes[0]) > 3 else 6.0
        half = width * ROAD_WIDTH_SCALE * 0.5
        rail_off = half + LATERAL_EXTRA

        cl = _centerline_crs(nodes)
        if cl is not None:
            center_rows.append(
                {
                    "id": f"cl_{road_ref}",
                    "width_m": width * ROAD_WIDTH_SCALE,
                    "highway": hw,
                    "notes": "seed:heuristic",
                    "geometry": cl,
                }
            )

        for side_name, sign in _side_signs():
            edge = _offset_line_crs(nodes, sign, half, SAMPLE_STEP_M)
            if edge is not None:
                edge_rows.append(
                    {
                        "id": f"edge_{road_ref}_{side_name}",
                        "side": side_name,
                        "road_ref": road_ref,
                        "source": "derived",
                        "notes": "seed:heuristic width/2",
                        "geometry": edge,
                    }
                )
            rail = _offset_line_crs(nodes, sign, rail_off, SAMPLE_STEP_M)
            if rail is not None:
                rail_rows.append(
                    {
                        "id": f"rail_{road_ref}_{side_name}",
                        "side": side_name,
                        "present": True,
                        "kind": "wbeam",
                        "gap_reason": None,
                        "road_ref": road_ref,
                        "section_m": SECTION_LEN,
                        "notes": "seed:heuristic width/2+lateral_extra",
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
        f"lateral_extra_m={LATERAL_EXTRA} (guardrail only)"
    )
    print(
        "Edit in QGIS. Normal builds never call this seed "
        "(re-seed only with --force)."
    )

if __name__ == "__main__":
    main()
