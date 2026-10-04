"""One road outline for the mesh, minus bridge / underpass / span.

Overpass, underpass and span keep their own polygons (stacked Z). Everything
else is one surface: snap, dissolve, smooth, then carve out the grade-
separated footprints so a plan cell is not owned twice. Fishnet tiles feed
build_road_grid.py. Intermediate polygons land in mesh_clip_steps/.

Decals stay on GIP centerlines; they do not read this file.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\unify_carriageway_mesh_clip.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely
from shapely import BufferJoinStyle, set_precision
from shapely.geometry import Polygon, box

sys.path.insert(0, str(Path(__file__).resolve().parent))
from site_coords import load_site, processed_dir

# Snap every vertex onto this lattice before the union.
SNAP_M = 0.05
# Close hairlines left after the dissolve.
CLOSE_M = 0.05
# Round the outline after the dissolve (kerb scale, not road width).
SMOOTH_M = 0.15
HOLE_KEEP_M2 = 15.0
# Tiles for the mesh clip. Overlap so a cell never falls on a tile seam alone.
TILE_M = 80.0
TILE_OVERLAP_M = 2.0
# Only these stay separate. No road/link/junction split for the mesh.
GRADE_SEPARATED = frozenset({"overpass", "underpass", "span"})


def _parts(geom):
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return [g for g in geom.geoms if not g.is_empty]
    if geom.geom_type == "GeometryCollection":
        out = []
        for g in geom.geoms:
            out.extend(_parts(g))
        return out
    return []


def _restore_holes(closed, source):
    holes = []
    for part in _parts(source):
        for ring in part.interiors:
            hole = Polygon(ring)
            if not hole.is_empty and hole.area >= HOLE_KEEP_M2:
                holes.append(hole)
    if not holes:
        return closed
    return shapely.make_valid(closed.difference(shapely.union_all(holes)))


def _write_step(
    dest: Path,
    geoms,
    *,
    crs,
    step: str,
    note: str,
    extra: dict | None = None,
) -> None:
    """One inspectable GPKG per polygon stage (multipart allowed)."""
    rows = []
    for i, geom in enumerate(geoms):
        if geom is None or geom.is_empty:
            continue
        for j, part in enumerate(_parts(geom)):
            if part.area < 1.0e-6:
                continue
            row = {
                "step": step,
                "note": note,
                "part": i,
                "ring": j,
                "area_m2": float(part.area),
                "geometry": part,
            }
            if extra:
                row.update(extra)
            rows.append(row)
    if dest.exists():
        dest.unlink()
    if not rows:
        gpd.GeoDataFrame(
            [{"step": step, "note": note, "part": -1, "ring": -1, "area_m2": 0.0}],
            geometry=[Polygon()],
            crs=crs,
        ).to_file(dest, layer="carriageway", driver="GPKG")
        print(f"  wrote {dest.name} (empty)", flush=True)
        return
    gpd.GeoDataFrame(rows, geometry="geometry", crs=crs).to_file(
        dest, layer="carriageway", driver="GPKG"
    )
    area = sum(r["area_m2"] for r in rows)
    print(f"  wrote {dest.name}: {len(rows)} parts, {area:.0f} m2", flush=True)


def _unify_with_steps(geoms: list, steps_dir: Path, crs) -> object | None:
    """Snap → dissolve → close → smooth; write each stage as GPKG."""
    steps_dir.mkdir(parents=True, exist_ok=True)
    for stale in steps_dir.glob("*.gpkg"):
        stale.unlink()

    _write_step(
        steps_dir / "01_road_source.gpkg",
        geoms,
        crs=crs,
        step="01_source",
        note="all polygons that are not overpass/underpass/span",
    )

    snapped = []
    for g in geoms:
        if g is None or g.is_empty:
            continue
        snapped.append(set_precision(shapely.make_valid(g), grid_size=SNAP_M))
    if not snapped:
        return None
    _write_step(
        steps_dir / "02_snapped.gpkg",
        snapped,
        crs=crs,
        step="02_snapped",
        note=f"set_precision grid_size={SNAP_M} m (5 cm lattice, not 0.5 m cells)",
    )

    merged = shapely.make_valid(shapely.union_all(snapped))
    _write_step(
        steps_dir / "03_dissolved.gpkg",
        [merged],
        crs=crs,
        step="03_dissolved",
        note="one surface: former road/link/junction overlap removed",
    )

    closed = shapely.make_valid(
        merged.buffer(CLOSE_M, join_style=BufferJoinStyle.round).buffer(
            -CLOSE_M, join_style=BufferJoinStyle.round
        )
    )
    closed = _restore_holes(closed, merged)
    _write_step(
        steps_dir / "04_closed.gpkg",
        [closed],
        crs=crs,
        step="04_closed",
        note=f"buffer +-{CLOSE_M} m to close hairlines; holes >= {HOLE_KEEP_M2} m2 restored",
    )

    smooth = shapely.make_valid(
        closed.buffer(SMOOTH_M, join_style=BufferJoinStyle.round).buffer(
            -SMOOTH_M, join_style=BufferJoinStyle.round
        )
    )
    smooth = _restore_holes(smooth, closed)
    kept = [p for p in _parts(smooth) if p.area >= 1.0]
    if not kept:
        return None
    unified = kept[0] if len(kept) == 1 else shapely.union_all(kept)
    _write_step(
        steps_dir / "05_smooth.gpkg",
        [unified],
        crs=crs,
        step="05_smooth",
        note=f"buffer +-{SMOOTH_M} m kerb rounding; splinters < 1 m2 dropped",
    )
    return unified


def _fishnet(geom, tile_m: float = TILE_M, overlap_m: float = TILE_OVERLAP_M) -> list:
    """Cut the unified surface into overlapping tiles for fast per-cell clips."""
    minx, miny, maxx, maxy = geom.bounds
    step = tile_m - overlap_m
    if step <= 0:
        raise SystemExit("tile overlap must be smaller than the tile size")
    x0 = math.floor(minx / step) * step
    y0 = math.floor(miny / step) * step
    out = []
    y = y0
    while y < maxy:
        x = x0
        while x < maxx:
            cell = box(x, y, x + tile_m, y + tile_m)
            if cell.intersects(geom):
                hit = shapely.make_valid(geom.intersection(cell))
                for part in _parts(hit):
                    if part.area >= 0.25:
                        out.append(part)
            x += step
        y += step
    return out


def _self_check() -> None:
    a = box(0, -2, 40, 2)
    b = box(38, -3, 60, 3)
    under = box(10, -1, 20, 1)
    snapped = [
        set_precision(shapely.make_valid(g), grid_size=SNAP_M) for g in (a, b)
    ]
    merged = shapely.make_valid(shapely.union_all(snapped))
    closed = shapely.make_valid(
        merged.buffer(CLOSE_M, join_style=BufferJoinStyle.round).buffer(
            -CLOSE_M, join_style=BufferJoinStyle.round
        )
    )
    exclusive = shapely.make_valid(closed.difference(under))
    if exclusive.intersection(under).area > 1.0e-6:
        raise SystemExit("unify self-check exclusive still overlaps under")
    if exclusive.area < 150:
        raise SystemExit(f"unify self-check exclusive area {exclusive.area}")
    tiles = _fishnet(exclusive, tile_m=20.0, overlap_m=2.0)
    if len(tiles) < 2:
        raise SystemExit(f"unify self-check tiles {len(tiles)}")


def main() -> None:
    _self_check()
    site = load_site()
    proc = processed_dir(site)
    src = proc / "road_surface_smooth" / "carriageway_smooth.gpkg"
    if not src.is_file():
        raise SystemExit(f"missing {src}")
    gdf = gpd.read_file(src, layer="carriageway")
    kind = gdf["mesh_kind"].astype(str)
    grade = gdf[kind.isin(GRADE_SEPARATED)].copy()
    road = gdf[~kind.isin(GRADE_SEPARATED)].copy()
    print(
        f"source {src.name}: road surface {len(road)}  "
        f"over/under/span {len(grade)}",
        flush=True,
    )
    steps_dir = proc / "road_surface_smooth" / "mesh_clip_steps"
    print(f"polygon steps -> {steps_dir.relative_to(proc)}", flush=True)
    unified = _unify_with_steps(list(road.geometry), steps_dir, gdf.crs)
    if unified is None or unified.is_empty:
        raise SystemExit("road unify produced nothing")
    before = float(road.geometry.area.sum())
    after = float(unified.area)
    print(
        f"snap {SNAP_M*100:.0f} cm, close {CLOSE_M*100:.0f} cm, "
        f"smooth {SMOOTH_M*100:.0f} cm: area {before:.0f} -> {after:.0f} m2 "
        f"(delta {after - before:+.0f})",
        flush=True,
    )
    if len(grade):
        _write_step(
            steps_dir / "06_grade_separated.gpkg",
            list(grade.geometry),
            crs=gdf.crs,
            step="06_grade_separated",
            note="overpass/underpass/span; carved out of the road surface next",
        )
        grade_u = shapely.make_valid(shapely.union_all(list(grade.geometry)))
        cut = float(unified.intersection(grade_u).area)
        exclusive = shapely.make_valid(unified.difference(grade_u))
        kept = [p for p in _parts(exclusive) if p.area >= 1.0]
        if not kept:
            raise SystemExit("road surface empty after carving over/under/span")
        exclusive = kept[0] if len(kept) == 1 else shapely.union_all(kept)
        remain = float(exclusive.intersection(grade_u).area)
        if remain > 0.01:
            raise SystemExit(
                f"exclusive still overlaps grade-separated by {remain:.3f} m2"
            )
        _write_step(
            steps_dir / "07_exclusive.gpkg",
            [exclusive],
            crs=gdf.crs,
            step="07_exclusive",
            note=(
                "road surface minus over/under/span; one owner per plan cell "
                f"(carved {cut:.0f} m2)"
            ),
        )
        print(
            f"exclusive road: carved {cut:.0f} m2 over/under/span -> "
            f"{float(exclusive.area):.0f} m2",
            flush=True,
        )
        unified = exclusive
    tiles = _fishnet(unified)
    print(f"fishnet {TILE_M:.0f} m -> {len(tiles)} tiles", flush=True)
    _write_step(
        steps_dir / "08_fishnet.gpkg",
        tiles,
        crs=gdf.crs,
        step="08_fishnet",
        note=f"overlapping {TILE_M:.0f} m tiles (overlap {TILE_OVERLAP_M:.0f} m) for mesh clip",
    )

    road_rows = [
        {
            "objectid": 870_000_000 + i,
            "members": "unified",
            "narrowed": 0,
            "narrow_m": 0.0,
            "mesh_kind": "road",
            "mesh_key": 0,
            "mesh_layer": 0,
            "geometry": part,
        }
        for i, part in enumerate(tiles)
    ]
    out = pd.concat(
        [
            grade,
            gpd.GeoDataFrame(road_rows, geometry="geometry", crs=gdf.crs),
        ],
        ignore_index=True,
    )
    dest = proc / "road_surface_smooth" / "carriageway_mesh_clip.gpkg"
    if dest.exists():
        dest.unlink()
    out.to_file(dest, layer="carriageway", driver="GPKG")
    outline = dest.with_name("carriageway_mesh_outline.gpkg")
    if outline.exists():
        outline.unlink()
    gpd.GeoDataFrame(
        [
            {
                "objectid": 870_000_000,
                "members": "unified",
                "mesh_kind": "road",
                "mesh_key": 0,
                "mesh_layer": 0,
                "geometry": unified,
            }
        ],
        geometry="geometry",
        crs=gdf.crs,
    ).to_file(outline, layer="carriageway", driver="GPKG")
    print(f"wrote {dest.name} and {outline.name}", flush=True)


if __name__ == "__main__":
    main()
