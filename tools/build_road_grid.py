"""0.5 m grid clipped to the Landnutzung carriageway polygons.

Heights and the selection mask are one georeferenced raster each. The mesh
keeps a cell when its centre lies in a street polygon that the Landes- or
Bundesstraße centerline runs through. A quad with a corner outside that
polygon is cut on the boundary. Within 1 m of that outline the raster
already holds the road height, including a strip just outside the polygon,
so a cut vertex samples that raster and does not pick up the slope.
``--raw`` reads the unfiltered 0.5 m DGM instead of the
filtered corridor. ``--heights`` reads one georeferenced 0.5 m GeoTIFF on
the same frame. Collada files are cut only at export, when a part would
exceed the vertex limit, and again by grade-separated layer (overpass /
underpass keep their own 2.5D grid). Partial edge cells stay as triangles.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\build_road_grid.py

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\build_road_grid.py --raw

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\build_road_grid.py --heights data\\processed\\tirol-imst-8192\\corridor50_fpdems\\filtered_7c_10d.tif --clip data\\processed\\tirol-imst-8192\\gip_width_m_new.gpkg --clip-layer buffer

The selection mask, 255 inside:

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\build_road_grid.py --masks-only
"""
from __future__ import annotations

import json
import math
import re
import sys
import uuid
from pathlib import Path

import numpy as np
import tifffile as tiff
import shapely
from shapely import clip_by_rect, constrained_delaunay_triangles, intersects_xy
from shapely.errors import GEOSException
from shapely.geometry import LineString, box
from shapely.ops import unary_union
from shapely.strtree import STRtree

import build_decal_roads as bdr
from build_asphalt_meshroads import _is_main_road
from build_corridor_mesh import CLEARANCE_M, USER_LEVELS, _write_collada, _write_material
from diag_road_bed_modes import _gip_polylines
from filter_road_corridor import raster_frame, write_referenced_tif
from raster_tile_fetch import elevation_nodata_mask
from measure_gip_widths import _ln_to_31254, _pick_poly, find_landnutzung_geojson, load_street_polygons
from site_coords import SiteCoords, load_site, processed_dir, site_slug

from mesh_dateinaht import SAMPLE_M, build_seam_chain

ROOT = Path(__file__).resolve().parents[1]
COLLADA_MAX_VERTS = 65535
RES_M = 0.5
UV_M = 6.0
# Neighbor cells only. A gorge in the 15 m strip can drop ~17 m in one cell.
MAX_EDGE_M = 40.0
MIN_AREA_M2 = 1.0e-6
SNAP_M = 1.0e-3
# Shared cut / outline vertices: round plan XY to millimetres, then store one
# (x, y, z) for that key so neighbouring mesh parts write the same triple.
# Do not snap outline verts onto the 0.5 m lattice; that drops odd edge pixels.
SEAM_DECIMALS = 3
# Cells this close to the carriageway outline take the road height.
EDGE_BAND_M = 1.0
# A donor farther than this belongs to another road.
EDGE_DONOR_M = 4.0
# Outer cell rows, smoothed along the road after the band is filled.
RIM_RINGS = 2
RIM_MEDIAN_M = 3.0
RIM_LOWPASS_SIGMA_M = 4.0
# An open edge farther inside than this is a hole, not the outline.
SEAM_INSIDE_M = EDGE_BAND_M + 0.25
# If the clip still has separate road ribbons (no unify), close hairlines.
AT_GRADE_CLOSE_M = 0.05
# Visible concrete box under a DOM deck. Vertical, so the soffit stays parallel.
DECK_BOX_M = 0.50
# A cut between mesh parts lies inside the deck. It is not the fascia.
DECK_BOX_SEAM_M = 0.75
_MAIN_ROAD_CODE = re.compile(r"^[ABLabl]\d")
IDENTITY_ROT = [1, 0, 0, 0, 1, 0, 0, 0, 1]


def _cell_on(grid: dict, r: int, c: int) -> tuple[bool, float, float, float]:
    height, width = grid["mask"].shape
    x = float(grid["xmin"]) + (c + 0.5) * RES_M
    y = float(grid["ymax"]) - (r + 0.5) * RES_M
    if r < 0 or c < 0 or r >= height or c >= width or not grid["mask"][r, c]:
        return False, float("nan"), x, y
    # Same sampler as clip vertices. At this cell centre the bilinear weight
    # sits on (r, c) alone when xmin/ymax/RES match the raster lattice.
    return True, _sample_z(grid, x, y), x, y


def _sample_z(grid: dict, x: float, y: float) -> float:
    """Bilinear height of the surface raster at an arbitrary plan position."""
    z = grid["z"]
    height, width = z.shape
    fc = (x - float(grid["xmin"])) / RES_M - 0.5
    fr = (float(grid["ymax"]) - y) / RES_M - 0.5
    c0 = math.floor(fc)
    r0 = math.floor(fr)
    tx = fc - c0
    ty = fr - r0
    acc = 0.0
    weight = 0.0
    for dr, dc, wt in (
        (0, 0, (1.0 - tx) * (1.0 - ty)),
        (0, 1, tx * (1.0 - ty)),
        (1, 0, (1.0 - tx) * ty),
        (1, 1, tx * ty),
    ):
        rr = r0 + dr
        cc = c0 + dc
        if wt <= 0.0 or rr < 0 or cc < 0 or rr >= height or cc >= width:
            continue
        sample = float(z[rr, cc])
        if not math.isfinite(sample):
            continue
        acc += wt * sample
        weight += wt
    if weight <= 0.0:
        return float("nan")
    return acc / weight


def _iter_polygons(geom):
    if geom is None or geom.is_empty:
        return
    kind = geom.geom_type
    if kind == "Polygon":
        yield geom
    elif kind == "MultiPolygon":
        yield from geom.geoms
    elif kind == "GeometryCollection":
        for part in geom.geoms:
            yield from _iter_polygons(part)


def _triangulate(poly):
    """Triangles covering ``poly``.

    GEOS refuses a ring it cannot find a convex corner on (collinear or
    repeated vertices left by clip_by_rect). Clean the ring first; if GEOS
    still refuses, fall back to an unconstrained Delaunay and keep the
    triangles whose centre lies inside the polygon.
    """
    # An invalid ring (self-touching, bow-tie) reports a cancelled area, so
    # it must be repaired before the area filter below sees its parts.
    candidates = [poly, shapely.remove_repeated_points(poly, SNAP_M)] if poly.is_valid else []
    candidates += [shapely.make_valid(poly), poly.buffer(0)]
    for cand in candidates:
        if cand is None or cand.is_empty:
            continue
        out = []
        failed = False
        for part in _iter_polygons(cand):
            if part.area < MIN_AREA_M2:
                continue
            try:
                tris = constrained_delaunay_triangles(part)
            except GEOSException:
                failed = True
                break
            out.extend(tris.geoms if hasattr(tris, "geoms") else (tris,))
        if not failed:
            return out
    tris = shapely.delaunay_triangles(poly)
    keep = []
    for tri in tris.geoms if hasattr(tris, "geoms") else (tris,):
        c = tri.centroid
        if poly.contains(c):
            keep.append(tri)
    return keep


COLLISION_CELL_M = 2.0


def _decimate_collision(pos: np.ndarray, faces: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Cluster interior vertices on a 2 m plan grid. Boundary vertices stay put.

    The outline and the cut between parts are shared vertices. Moving them
    would open a gap in the collision mesh. Interior points of one cell
    become their average, so the driving surface keeps its grade.
    """
    n = int(pos.shape[0])
    counts: dict[tuple[int, int], int] = {}
    for tri in faces:
        corners = (int(tri[0]), int(tri[1]), int(tri[2]))
        for u, v in ((corners[0], corners[1]), (corners[1], corners[2]), (corners[2], corners[0])):
            key = (u, v) if u < v else (v, u)
            counts[key] = counts.get(key, 0) + 1
    boundary = np.zeros(n, dtype=bool)
    for (u, v), count in counts.items():
        if count == 1:
            boundary[u] = True
            boundary[v] = True
    cell = np.floor(pos[:, :2] / COLLISION_CELL_M).astype(np.int64)
    buckets: dict[tuple[int, int], list[int]] = {}
    for i in range(n):
        if boundary[i]:
            continue
        buckets.setdefault((int(cell[i, 0]), int(cell[i, 1])), []).append(i)
    remap = np.empty(n, dtype=np.int32)
    new_pos: list[np.ndarray] = []
    for i in range(n):
        if not boundary[i]:
            continue
        remap[i] = len(new_pos)
        new_pos.append(pos[i])
    for members in buckets.values():
        idx = np.asarray(members, dtype=np.int64)
        slot = len(new_pos)
        new_pos.append(pos[idx].mean(axis=0))
        remap[idx] = slot
    if not new_pos:
        return pos, faces
    out = np.asarray(new_pos, dtype=np.float64)
    nf = remap[faces]
    keep = (nf[:, 0] != nf[:, 1]) & (nf[:, 1] != nf[:, 2]) & (nf[:, 2] != nf[:, 0])
    nf = nf[keep]
    if nf.size == 0:
        return pos, faces
    tri = out[nf]
    ab = tri[:, 1] - tri[:, 0]
    ac = tri[:, 2] - tri[:, 0]
    nz = ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0]
    flip = nz < 0.0
    if np.any(flip):
        swapped = nf[flip][:, [0, 2, 1]]
        nf = nf.copy()
        nf[flip] = swapped
    nf = nf[np.abs(nz) >= 1.0e-6]
    if nf.size == 0:
        return pos, faces
    return out, nf


def _check_geometry(pos: np.ndarray, faces: np.ndarray, *, stem: str, z_min: float, max_h: float) -> None:
    if pos.size == 0:
        raise SystemExit(f"{stem}: empty mesh")
    if not np.isfinite(pos).all():
        raise SystemExit(f"{stem}: non-finite vertex")
    if np.max(np.abs(pos[:, :2])) > 1.0e5:
        raise SystemExit(f"{stem}: coordinate exploded")
    z = pos[:, 2]
    if float(z.min()) < -50.0 or float(z.max()) > max_h + 50.0:
        raise SystemExit(
            f"{stem}: z {float(z.min()):.2f}..{float(z.max()):.2f} "
            f"outside 0..{max_h:.1f} (datum {z_min:.1f})"
        )
    tri = pos[faces]
    ab = tri[:, 1] - tri[:, 0]
    ac = tri[:, 2] - tri[:, 0]
    bc = tri[:, 2] - tri[:, 1]
    cross = np.cross(ab, ac)
    area = 0.5 * np.linalg.norm(cross, axis=1)
    edges = np.stack(
        (
            np.linalg.norm(ab, axis=1),
            np.linalg.norm(ac, axis=1),
            np.linalg.norm(bc, axis=1),
        ),
        axis=1,
    )
    if not np.isfinite(area).all() or not np.isfinite(edges).all():
        raise SystemExit(f"{stem}: non-finite face")
    if int(np.count_nonzero(area < MIN_AREA_M2)):
        raise SystemExit(f"{stem}: degenerate face area={float(area.min()):.3e}")
    if float(edges.max()) > MAX_EDGE_M:
        raise SystemExit(f"{stem}: edge {float(edges.max()):.2f} m")
    if int(np.count_nonzero(cross[:, 2] <= 0.0)):
        raise SystemExit(f"{stem}: face normal points down")


def _load_deck_box(sc: SiteCoords) -> dict | None:
    """DOM-deck ribbons in terrain XY. Only these faces get the visible box."""
    path = processed_dir(sc.site) / "dgm_repair_transect" / "carriageway_bridged.gpkg"
    if not path.is_file():
        return None
    import geopandas as gpd

    try:
        gdf = gpd.read_file(path, layer="bridge_deck")
    except (ValueError, OSError):
        return None
    geoms = [g for g in gdf.geometry if g is not None and not g.is_empty]
    if not geoms:
        return None

    def _xy(coords: np.ndarray) -> np.ndarray:
        out = np.array(coords, dtype=np.float64, copy=True)
        out[:, 0] = (out[:, 0] - sc.xmin) / sc.bw * sc.terrain_span
        out[:, 1] = (out[:, 1] - sc.ymin) / sc.bh * sc.terrain_span
        return out

    moved = [shapely.transform(g, _xy) for g in geoms]
    return {"union": shapely.union_all(moved), "n": len(moved)}


def _append_deck_box(
    pos: np.ndarray,
    uv: np.ndarray,
    faces: np.ndarray,
    deck: dict,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Soffit and fascia under deck triangles. Top faces stay as they are.

    Bottom and sides use their own vertices, so the crease at the deck edge
    does not share a normal with the driving surface. The collision mesh is
    built before this and is not passed in.
    """
    union = deck["union"]
    cent = pos[faces].mean(axis=1)[:, :2]
    inside = shapely.contains_xy(union, cent[:, 0], cent[:, 1])
    if not np.any(inside):
        return pos, uv, faces
    deck_faces = faces[inside]
    used = np.unique(deck_faces.reshape(-1))
    remap = np.full(pos.shape[0], -1, dtype=np.int64)
    remap[used] = np.arange(pos.shape[0], pos.shape[0] + used.shape[0])
    bot_pos = pos[used].copy()
    bot_pos[:, 2] -= DECK_BOX_M
    bot_uv = uv[used].copy()
    bot_faces = np.stack(
        (
            remap[deck_faces[:, 0]],
            remap[deck_faces[:, 2]],
            remap[deck_faces[:, 1]],
        ),
        axis=1,
    )
    ab = bot_pos[bot_faces[:, 1] - pos.shape[0]] - bot_pos[bot_faces[:, 0] - pos.shape[0]]
    ac = bot_pos[bot_faces[:, 2] - pos.shape[0]] - bot_pos[bot_faces[:, 0] - pos.shape[0]]
    if int(np.count_nonzero(np.cross(ab, ac)[:, 2] >= -1.0e-9)):
        raise SystemExit("deck box: soffit normal does not point down")

    counts: dict[tuple[int, int], int] = {}
    for tri in deck_faces:
        a, b, c = int(tri[0]), int(tri[1]), int(tri[2])
        for edge in ((a, b), (b, c), (c, a)):
            counts[edge] = counts.get(edge, 0) + 1
    outline = [(u, v) for (u, v), n in counts.items() if n == 1 and counts.get((v, u), 0) == 0]
    rim = union.boundary
    side_at: dict[tuple[int, bool], int] = {}
    side_pos: list[np.ndarray] = []
    side_uv: list[np.ndarray] = []
    side_faces: list[tuple[int, int, int]] = []
    side_base = int(pos.shape[0] + used.shape[0])

    def side_vert(idx: int, lower: bool) -> int:
        key = (idx, lower)
        hit = side_at.get(key)
        if hit is not None:
            return hit
        p = pos[idx].copy()
        if lower:
            p[2] -= DECK_BOX_M
        n = side_base + len(side_pos)
        side_at[key] = n
        side_pos.append(p)
        side_uv.append(uv[idx].copy())
        return n

    for u, v in outline:
        mid = 0.5 * (pos[u, :2] + pos[v, :2])
        if bool(shapely.contains_xy(union, [mid[0]], [mid[1]])[0]):
            dist = float(shapely.distance(rim, shapely.points(float(mid[0]), float(mid[1]))))
            if dist > DECK_BOX_SEAM_M:
                continue
        su0 = side_vert(u, False)
        su1 = side_vert(u, True)
        sv1 = side_vert(v, True)
        sv0 = side_vert(v, False)
        side_faces.append((su0, su1, sv1))
        side_faces.append((su0, sv1, sv0))

    parts_pos = [pos, bot_pos]
    parts_uv = [uv, bot_uv]
    parts_faces = [faces, bot_faces]
    if side_pos:
        parts_pos.append(np.stack(side_pos))
        parts_uv.append(np.stack(side_uv))
        parts_faces.append(np.asarray(side_faces, dtype=np.int64))
    return np.vstack(parts_pos), np.vstack(parts_uv), np.vstack(parts_faces)


def _z_nearest(grid: dict, x: float, y: float, reach: int = 3) -> float:
    """Height of the nearest finite road cell around a plan position.

    Depends on the position and the raster only, not on the quad that asked.
    A cut vertex on a part boundary therefore gets the same height from both
    parts, which ``_z_from_inside`` cannot promise (each part sees another
    quad).
    """
    z = grid["z"]
    mask = grid["mask"]
    height, width = z.shape
    fc = (x - float(grid["xmin"])) / RES_M - 0.5
    fr = (float(grid["ymax"]) - y) / RES_M - 0.5
    c0 = int(round(fc))
    r0 = int(round(fr))
    best = float("nan")
    best_d = float("inf")
    for rr in range(max(0, r0 - reach), min(height, r0 + reach + 1)):
        for cc in range(max(0, c0 - reach), min(width, c0 + reach + 1)):
            if not mask[rr, cc]:
                continue
            sample = float(z[rr, cc])
            if not math.isfinite(sample):
                continue
            d = math.hypot(fc - cc, fr - rr)
            if d < best_d or (d == best_d and (rr, cc) < (r0, c0)):
                best_d = d
                best = sample
    return best


def _z_from_inside(x: float, y: float, packed: list) -> float:
    """Height at a cut point from the inside cell centres of this quad only."""
    acc = 0.0
    weight = 0.0
    for _rr, _cc, cx, cy, cz, on in packed:
        if not on or not math.isfinite(cz):
            continue
        dist = math.hypot(x - cx, y - cy)
        if dist < 1.0e-4:
            return float(cz)
        wt = 1.0 / dist
        acc += wt * float(cz)
        weight += wt
    if weight <= 0.0:
        return float("nan")
    return acc / weight


def _mm_xy(x: float, y: float) -> tuple[float, float]:
    return (round(float(x), SEAM_DECIMALS), round(float(y), SEAM_DECIMALS))


def _canon_xyz(
    bag: dict[tuple, tuple[float, float, float]],
    key: tuple,
    x: float,
    y: float,
    z_abs: float,
) -> tuple[float, float, float]:
    """First writer defines (x, y, z); later writers copy that triple exactly."""
    hit = bag.get(key)
    if hit is not None:
        return hit
    x, y = _mm_xy(x, y)
    stored = (x, y, float(z_abs))
    bag[key] = stored
    return stored


def _row_center_y(grid: dict, r: int) -> float:
    return float(grid["ymax"]) - (r + 0.5) * RES_M


def _split_template(grid: dict, mid: int) -> LineString:
    y = _row_center_y(grid, mid)
    pad = 50.0
    return LineString(
        [
            (float(grid["xmin"]) - pad, y),
            (float(grid["xmax"]) + pad, y),
        ]
    )


def _split_chains(grid: dict, mid: int) -> list[list[dict]]:
    polys = grid.get("polys") or []
    if not polys:
        return []
    try:
        road = unary_union(polys)
    except (GEOSException, ValueError):
        road = shapely.union_all([shapely.make_valid(p) for p in polys])
    if road is None or road.is_empty:
        return []
    return build_seam_chain(
        _split_template(grid, mid),
        shapely.make_valid(road),
        grid["mask"],
        grid["z"],
        float(grid["xmin"]),
        float(grid["ymax"]),
        sample_z=lambda x, y: _sample_z(grid, x, y),
    )


def _nearest_chain_point(
    x: float, y: float, chains: list, *, max_d: float
) -> dict | None:
    best = None
    best_d = max_d
    for ch in chains:
        for p in ch:
            d = math.hypot(x - float(p["x"]), y - float(p["y"]))
            if d <= best_d:
                best_d = d
                best = p
    return best


def _bind_seam_names(records: list[dict], written: list[dict]) -> list[dict]:
    out = []
    for rec in records:
        mid = int(rec["mid"])
        r0, r1 = int(rec["r0"]), int(rec["r1"])
        lefts = [
            w["name"]
            for w in written
            if int(w["rows"][1]) == mid and r0 <= int(w["rows"][0]) < mid
        ]
        rights = [
            w["name"]
            for w in written
            if int(w["rows"][0]) == mid and mid < int(w["rows"][1]) <= r1
        ]
        left = lefts[-1] if lefts else None
        right = rights[0] if rights else None
        for ch in rec["chains"]:
            out.append(
                {
                    "left": left,
                    "right": right,
                    "mid_row": mid,
                    "points": ch,
                }
            )
    return out


def _build_part(
    grid: dict,
    sc: SiteCoords,
    *,
    r0: int,
    r1: int,
    z_min: float,
    seam_rows: list[int] | None = None,
    chains: list | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, set, int] | None:
    mask = grid["mask"]
    height, width = mask.shape
    if r0 >= height or r1 <= 0 or not mask[max(0, r0) : min(height, r1 + 1)].any():
        return None
    present = mask.any(axis=1)
    extent = sc.terrain_span
    index: dict[tuple[int, int], int] = {}
    pos_l: list[tuple[float, float, float]] = []
    uv_l: list[tuple[float, float]] = []
    faces: list[tuple[int, int, int]] = []
    used: set[tuple[int, int]] = set()
    canon_cell: dict[tuple, tuple[float, float, float]] = grid.setdefault(
        "canon_cell", {}
    )
    canon_xy: dict[tuple, tuple[float, float, float]] = grid.setdefault(
        "canon_xy", {}
    )
    chain_items = chains or []
    chain_lists = [ch for ch, _sign in chain_items]
    seam_ys = [_row_center_y(grid, r) for r in (seam_rows or [])]
    for ch in chain_lists:
        for p in ch:
            zx = float(p["z"])
            if not math.isfinite(zx):
                continue
            if p.get("role") == "cell" and p.get("row") is not None:
                _canon_xyz(
                    canon_cell,
                    (int(p["row"]), int(p["col"])),
                    float(p["x"]),
                    float(p["y"]),
                    zx,
                )
            else:
                _canon_xyz(
                    canon_xy, _mm_xy(float(p["x"]), float(p["y"])),
                    float(p["x"]), float(p["y"]), zx,
                )

    def vid(r: int, c: int, x: float, y: float, z_abs: float) -> int:
        key = (r, c)
        hit = index.get(key)
        if hit is not None:
            return hit
        x, y, z_abs = _canon_xyz(canon_cell, key, x, y, z_abs)
        bx = (x - sc.xmin) / sc.bw * extent
        by = (y - sc.ymin) / sc.bh * extent
        bz = (z_abs - z_min) + CLEARANCE_M
        index[key] = len(pos_l)
        pos_l.append((bx, by, bz))
        uv_l.append((bx / UV_M, by / UV_M))
        used.add(key)
        return index[key]

    def emit(a: int, b: int, c: int) -> None:
        pa, pb, pc = pos_l[a], pos_l[b], pos_l[c]
        abx, aby = pb[0] - pa[0], pb[1] - pa[1]
        acx, acy = pc[0] - pa[0], pc[1] - pa[1]
        cross = abx * acy - aby * acx
        # A clip corner can snap onto a grid line. The triangle then has no area.
        if abs(cross) < MIN_AREA_M2 * 2.0:
            return
        if cross < 0.0:
            b, c = c, b
        faces.append((a, b, c))

    polys = grid.get("polys") or []
    tree = grid.get("tree")
    boundary: dict[tuple[float, float], int] = {}
    clipped = 0

    def resolve(x: float, y: float, packed: list) -> int | None:
        for rr, cc, cx, cy, cz, on in packed:
            if on and math.hypot(x - cx, y - cy) <= SNAP_M:
                return vid(rr, cc, cx, cy, cz)
        if seam_ys and chain_lists and any(abs(y - sy) <= RES_M * 0.52 for sy in seam_ys):
            hitp = _nearest_chain_point(x, y, chain_lists, max_d=RES_M * 2.0)
            if hitp is not None:
                if hitp.get("role") == "cell" and hitp.get("row") is not None:
                    return vid(
                        int(hitp["row"]),
                        int(hitp["col"]),
                        float(hitp["x"]),
                        float(hitp["y"]),
                        float(hitp["z"]),
                    )
                x, y, z_abs = float(hitp["x"]), float(hitp["y"]), float(hitp["z"])
                key = _mm_xy(x, y)
                hit = boundary.get(key)
                if hit is not None:
                    return hit
                x, y, z_abs = _canon_xyz(canon_xy, key, x, y, z_abs)
                bx = (x - sc.xmin) / sc.bw * extent
                by = (y - sc.ymin) / sc.bh * extent
                bz = (z_abs - z_min) + CLEARANCE_M
                boundary[key] = len(pos_l)
                pos_l.append((bx, by, bz))
                uv_l.append((bx / UV_M, by / UV_M))
                return boundary[key]
            return None
        key = _mm_xy(x, y)
        hit = boundary.get(key)
        if hit is not None:
            return hit
        z_abs = _sample_z(grid, key[0], key[1])
        if not math.isfinite(z_abs):
            z_abs = _z_nearest(grid, key[0], key[1])
        if not math.isfinite(z_abs):
            z_abs = _z_from_inside(key[0], key[1], packed)
        if not math.isfinite(z_abs):
            return None
        x, y, z_abs = _canon_xyz(canon_xy, key, key[0], key[1], z_abs)
        bx = (x - sc.xmin) / sc.bw * extent
        by = (y - sc.ymin) / sc.bh * extent
        bz = (z_abs - z_min) + CLEARANCE_M
        boundary[key] = len(pos_l)
        pos_l.append((bx, by, bz))
        uv_l.append((bx / UV_M, by / UV_M))
        return boundary[key]

    def emit_clip(packed: list) -> None:
        nonlocal clipped
        xs = [p[2] for p in packed]
        ys = [p[3] for p in packed]
        minx, maxx = min(xs), max(xs)
        miny, maxy = min(ys), max(ys)
        parts = []
        for idx in tree.query(box(minx, miny, maxx, maxy)):
            poly = polys[int(idx)]
            try:
                inter = clip_by_rect(poly, minx, miny, maxx, maxy)
            except Exception:
                try:
                    inter = poly.intersection(box(minx, miny, maxx, maxy))
                except Exception:
                    continue
            if inter is not None and not inter.is_empty:
                parts.append(inter)
        if not parts:
            return
        if len(parts) == 1:
            geom = parts[0]
        else:
            try:
                geom = unary_union(parts)
            except (GEOSException, ValueError):
                geom = shapely.GeometryCollection(
                    [shapely.make_valid(p) for p in parts]
                )
            else:
                if geom is None or geom.is_empty:
                    return
                geom = shapely.make_valid(geom)
        emitted = False
        for poly in _iter_polygons(geom):
            if poly.area < MIN_AREA_M2:
                continue
            for tri in _triangulate(poly):
                coords = list(tri.exterior.coords)
                if len(coords) < 4:
                    continue
                ids = []
                ok = True
                for x, y in coords[:3]:
                    vert = resolve(float(x), float(y), packed)
                    if vert is None:
                        ok = False
                        break
                    ids.append(vert)
                if not ok or len(set(ids)) < 3:
                    continue
                ax, ay = coords[0]
                bx, by = coords[1]
                cx, cy = coords[2]
                area = abs((bx - ax) * (cy - ay) - (cx - ax) * (by - ay)) * 0.5
                if area < MIN_AREA_M2:
                    continue
                emit(ids[0], ids[1], ids[2])
                emitted = True
        if emitted:
            clipped += 1

    r_lo = max(0, r0)
    r_hi = min(height, r1)
    for r in range(r_lo, r_hi):
        rows = [r]
        if r + 1 < height:
            rows.append(r + 1)
        if not any(present[i] for i in rows):
            continue
        active = np.zeros(width, dtype=bool)
        for rr in rows:
            if not present[rr]:
                continue
            cols = np.nonzero(mask[rr])[0]
            active[cols] = True
            active[np.maximum(cols - 1, 0)] = True
        for c in np.nonzero(active)[0].tolist():
            packed = []
            inside = []
            for rr, cc in ((r, c), (r, c + 1), (r + 1, c + 1), (r + 1, c)):
                on, z, x, y = _cell_on(grid, rr, cc)
                on = bool(on and math.isfinite(z))
                packed.append((rr, cc, x, y, z, on))
                if on:
                    inside.append((rr, cc, x, y, z))
            if len(inside) == 4:
                ids = [vid(rr, cc, x, y, z) for rr, cc, x, y, z in inside]
                emit(ids[0], ids[3], ids[2])
                emit(ids[0], ids[2], ids[1])
            elif inside and tree is not None:
                emit_clip(packed)
    if chain_items:
        def chain_vid(p: dict):
            zx = float(p["z"])
            if not math.isfinite(zx):
                return None
            if p.get("role") == "cell" and p.get("row") is not None:
                return vid(
                    int(p["row"]), int(p["col"]),
                    float(p["x"]), float(p["y"]), zx,
                )
            key = _mm_xy(float(p["x"]), float(p["y"]))
            hit = boundary.get(key)
            if hit is not None:
                return hit
            x, y, z_abs = _canon_xyz(
                canon_xy, key, float(p["x"]), float(p["y"]), zx
            )
            bx = (x - sc.xmin) / sc.bw * extent
            by = (y - sc.ymin) / sc.bh * extent
            bz = (z_abs - z_min) + CLEARANCE_M
            boundary[key] = len(pos_l)
            pos_l.append((bx, by, bz))
            uv_l.append((bx / UV_M, by / UV_M))
            return boundary[key]

        edge_seen = set()
        for a, b, c in faces:
            for u, v in ((a, b), (b, c), (c, a)):
                edge_seen.add((u, v) if u < v else (v, u))
        stitched = []
        for ch, ysign in chain_items:
            ids = []
            for p in ch:
                i = chain_vid(p)
                if i is not None:
                    ids.append(i)
            stitched.append((ids, ysign))
        world = []
        for bx, by, _bz in pos_l:
            world.append(
                (
                    sc.xmin + bx / extent * sc.bw,
                    sc.ymin + by / extent * sc.bh,
                )
            )
        for ids, ysign in stitched:
            for ia, ib in zip(ids, ids[1:]):
                if ia == ib:
                    continue
                e = (ia, ib) if ia < ib else (ib, ia)
                if e in edge_seen:
                    continue
                ax, ay = world[ia]
                bx_, by_ = world[ib]
                mx, my = 0.5 * (ax + bx_), 0.5 * (ay + by_)
                best = None
                best_d = RES_M * 3.0
                for k, (wx, wy) in enumerate(world):
                    if k == ia or k == ib:
                        continue
                    if (wy - my) * ysign < 0.05:
                        continue
                    d = math.hypot(wx - mx, wy - my)
                    if d < best_d:
                        best_d = d
                        best = k
                if best is None:
                    continue
                before = len(faces)
                emit(ia, ib, best)
                if len(faces) > before:
                    edge_seen.add(e)
    if chain_lists:
        for ch in chain_lists:
            for p in ch:
                if p.get("role") != "cell" or p.get("row") is None:
                    continue
                rr, cc = int(p["row"]), int(p["col"])
                if r0 <= rr < r1 or rr in (seam_rows or []):
                    on, z, x, y = _cell_on(grid, rr, cc)
                    if on and math.isfinite(z):
                        vid(rr, cc, x, y, z)
    if not faces:
        return None
    pos = np.asarray(pos_l, dtype=np.float64)
    uv = np.asarray(uv_l, dtype=np.float64)
    fac = np.asarray(faces, dtype=np.int32)
    return pos, uv, fac, used, clipped


def _seam_rows_for(grid: dict, r0: int, r1: int) -> list[int]:
    rows = []
    for rec in grid.get("file_seams") or []:
        mid = int(rec["mid"])
        if r1 == mid or r0 == mid:
            rows.append(mid)
    return rows


def _chains_for(grid: dict, r0: int, r1: int) -> list:
    """Chains touching this row band, with interior side in +Y (north) or -Y."""
    acc: list = []
    for rec in grid.get("file_seams") or []:
        mid = int(rec["mid"])
        for ch in rec.get("chains") or []:
            if r1 == mid:
                acc.append((ch, 1.0))
            elif r0 == mid:
                acc.append((ch, -1.0))
    return acc


def _emit_rows(
    grid: dict,
    sc: SiteCoords,
    *,
    r0: int,
    r1: int,
    z_min: float,
    max_h: float,
    out_dir: Path,
    written: list[dict],
    covered: set,
    prefix: str = "part_",
) -> None:
    height = int(grid["mask"].shape[0])
    if (r1 - r0) > 1:
        n_cells = int(grid["mask"][max(0, r0) : min(height, r1)].sum())
        if n_cells > 50000:
            mid = r0 + (r1 - r0) // 2
            chains = _split_chains(grid, mid)
            grid.setdefault("file_seams", []).append(
                {"r0": r0, "mid": mid, "r1": r1, "chains": chains}
            )
            print(
                f"  split rows {r0}-{r1} at {mid}: {len(chains)} seam pieces",
                flush=True,
            )
            _emit_rows(
                grid, sc, r0=r0, r1=mid, z_min=z_min, max_h=max_h,
                out_dir=out_dir, written=written, covered=covered, prefix=prefix,
            )
            _emit_rows(
                grid, sc, r0=mid, r1=r1, z_min=z_min, max_h=max_h,
                out_dir=out_dir, written=written, covered=covered, prefix=prefix,
            )
            return
    built = _build_part(
        grid, sc, r0=r0, r1=r1, z_min=z_min,
        seam_rows=_seam_rows_for(grid, r0, r1),
        chains=_chains_for(grid, r0, r1),
    )
    if built is None:
        return
    pos, uv, faces, used, clipped = built
    if pos.shape[0] > COLLADA_MAX_VERTS and (r1 - r0) > 1:
        mid = r0 + (r1 - r0) // 2
        chains = _split_chains(grid, mid)
        grid.setdefault("file_seams", []).append(
            {"r0": r0, "mid": mid, "r1": r1, "chains": chains}
        )
        print(
            f"  split verts {pos.shape[0]} rows {r0}-{r1} at {mid}: "
            f"{len(chains)} seam pieces",
            flush=True,
        )
        _emit_rows(
            grid, sc, r0=r0, r1=mid, z_min=z_min, max_h=max_h,
            out_dir=out_dir, written=written, covered=covered, prefix=prefix,
        )
        _emit_rows(
            grid, sc, r0=mid, r1=r1, z_min=z_min, max_h=max_h,
            out_dir=out_dir, written=written, covered=covered, prefix=prefix,
        )
        return
    if pos.shape[0] > COLLADA_MAX_VERTS:
        raise SystemExit(f"rows {r0}-{r1}: {pos.shape[0]} verts in one row band")
    stem = f"{prefix}{len(written):03d}"
    _check_geometry(pos, faces, stem=stem, z_min=z_min, max_h=max_h)
    col_pos, col_faces = _decimate_collision(pos, faces)
    _check_geometry(col_pos, col_faces, stem=f"{stem} collision", z_min=z_min, max_h=max_h)
    _note_seams(grid, pos, faces)
    deck = grid.get("deck_box")
    n_top = int(faces.shape[0])
    if deck is not None:
        pos, uv, faces = _append_deck_box(pos, uv, faces, deck)
        if int(faces.shape[0]) != n_top:
            print(
                f"  {stem}: deck box {DECK_BOX_M:.2f} m, "
                f"+{int(faces.shape[0]) - n_top} tris",
                flush=True,
            )
        if pos.shape[0] > COLLADA_MAX_VERTS:
            raise SystemExit(
                f"{stem}: {pos.shape[0]} verts after the deck box"
            )
    _write_collada(
        out_dir / f"{stem}.dae", pos, uv, faces, stem,
        col_pos=col_pos, col_faces=col_faces,
    )
    grid["col_verts"] = int(grid.get("col_verts") or 0) + int(col_pos.shape[0])
    grid["col_tris"] = int(grid.get("col_tris") or 0) + int(col_faces.shape[0])
    covered.update(used)
    grid["clip_quads"] = int(grid.get("clip_quads") or 0) + clipped
    written.append(
        {
            "name": stem,
            "rows": [int(r0), int(r1)],
            "verts": int(pos.shape[0]),
            "tris": int(faces.shape[0]),
        }
    )


def _inject(level_name: str, entries: list[dict], art_src: Path) -> None:
    user_level = USER_LEVELS / level_name
    art = user_level / "art" / "shapes" / "road_grid"
    art.mkdir(parents=True, exist_ok=True)
    keep = {p.name for p in art_src.iterdir() if p.suffix.lower() in {".dae", ".json"}}
    for src in art_src.iterdir():
        if src.suffix.lower() in {".dae", ".json"}:
            (art / src.name).write_bytes(src.read_bytes())
    for dst in list(art.iterdir()):
        if dst.suffix.lower() == ".dae" and dst.name not in keep:
            dst.unlink()
    cache = USER_LEVELS.parent / "temp" / "levels" / level_name / "art" / "shapes" / "road_grid"
    if cache.is_dir():
        for stale in cache.glob("*.cdae"):
            stale.unlink()
    group_dir = user_level / "main/MissionGroup/level_objects/road_grid"
    group_dir.mkdir(parents=True, exist_ok=True)
    items = group_dir / "items.level.json"
    items.write_text(
        "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in entries),
        encoding="utf-8",
    )
    lo = user_level / "main/MissionGroup/level_objects/items.level.json"
    rows = []
    for ln in lo.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            rows.append(json.loads(ln))
    found = False
    for row in rows:
        if row.get("class") != "SimGroup":
            continue
        if row.get("name") == "road_grid":
            row["enabled"] = "1"
            found = True
        elif row.get("name") in ("asphalt_mesh", "corridor_mesh"):
            row["enabled"] = "0"
    if not found:
        rows.append(
            {
                "name": "road_grid",
                "class": "SimGroup",
                "__parent": "level_objects",
                "enabled": "1",
                "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "autoroad:road_grid:group")),
            }
        )
    lo.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows),
        encoding="utf-8",
    )


def _entry(level_name: str, stem: str) -> dict:
    return {
        "name": stem,
        "class": "TSStatic",
        "__parent": "road_grid",
        "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"autoroad:road_grid:{stem}")),
        "position": [0, 0, 0],
        "rotationMatrix": IDENTITY_ROT,
        "scale": [1.0, 1.0, 1.0],
        "shapeName": f"/levels/{level_name}/art/shapes/road_grid/{stem}.dae",
        "collisionType": "Collision Mesh",
        "decalType": "Collision Mesh",
        "castShadows": True,
        "useInstanceRenderData": True,
        "isRenderEnabled": True,
    }


def _centerline_samples(pts: list, step_m: float = 5.0) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    if len(pts) < 2:
        return out
    for a, b in zip(pts, pts[1:]):
        ax, ay = float(a[0]), float(a[1])
        bx, by = float(b[0]), float(b[1])
        dist = math.hypot(bx - ax, by - ay)
        n = max(1, int(math.ceil(dist / step_m)))
        for i in range(n):
            t = i / n
            out.append((ax + (bx - ax) * t, ay + (by - ay) * t))
    out.append((float(pts[-1][0]), float(pts[-1][1])))
    return out


def _carriageway_polygons(site: dict, proc: Path, sc: SiteCoords) -> tuple[list, dict]:
    """Street polygons the meshed centerlines run through, in EPSG:31254."""
    path = find_landnutzung_geojson(site)
    if path is None:
        raise SystemExit("no Landnutzung geojson for this site")
    geoms, codes = load_street_polygons(path, _ln_to_31254())
    if not geoms:
        raise SystemExit(f"no street polygons in {path.name}")
    tree = STRtree(geoms)
    id_to_idx = {id(g): i for i, g in enumerate(geoms)}
    cfg = bdr._cfg(site.get("beamng") or {})
    roads = [
        r
        for r in bdr.stitch_abutting_roads(_gip_polylines(site, proc), cfg)
        if _is_main_road(r, site)
    ]
    chosen: set[int] = set()
    by_code: dict[str, int] = {}
    miss = 0
    total = 0
    for road in roads:
        obj = str(road.get("objekt") or "")
        for bx, by in _centerline_samples(road.get("pts") or []):
            x, y = sc.beamng_to_crs(bx, by)
            poly, code = _pick_poly(tree, geoms, codes, x, y, obj)
            total += 1
            if poly is None:
                miss += 1
                continue
            chosen.add(id_to_idx[id(poly)])
            key = code or "?"
            by_code[key] = by_code.get(key, 0) + 1
    if total == 0 or not chosen:
        raise SystemExit("centerlines hit no carriageway polygon")
    if miss / total > 0.02:
        raise SystemExit(
            f"{miss} of {total} centerline samples are outside every street polygon"
        )
    polys = [geoms[i] for i in sorted(chosen)]
    stats = {
        "landnutzung": path.name,
        "samples": total,
        "samples_outside": miss,
        "polygons": len(polys),
        "samples_by_code": by_code,
    }
    print(
        f"carriageway polygons: {len(polys)} from {total} samples, "
        f"outside {miss}, codes {by_code}",
        flush=True,
    )
    return polys, stats


def _crop_window(z: np.ndarray, frame: dict, polys: list, pad_m: float = 8.0):
    """Raster window around the polygons, with room for the edge band."""
    height, width = z.shape
    minx = min(float(p.bounds[0]) for p in polys) - pad_m
    miny = min(float(p.bounds[1]) for p in polys) - pad_m
    maxx = max(float(p.bounds[2]) for p in polys) + pad_m
    maxy = max(float(p.bounds[3]) for p in polys) + pad_m
    c0 = max(0, int(math.floor((minx - float(frame["xmin"])) / RES_M)))
    c1 = min(width, int(math.ceil((maxx - float(frame["xmin"])) / RES_M)))
    r0 = max(0, int(math.floor((float(frame["ymax"]) - maxy) / RES_M)))
    r1 = min(height, int(math.ceil((float(frame["ymax"]) - miny) / RES_M)))
    if r1 <= r0 or c1 <= c0:
        return None
    sub = {
        "xmin": float(frame["xmin"]) + c0 * RES_M,
        "xmax": float(frame["xmin"]) + c1 * RES_M,
        "ymax": float(frame["ymax"]) - r0 * RES_M,
        "ymin": float(frame["ymax"]) - r1 * RES_M,
    }
    return r0, r1, c0, c1, sub


def _clip_mask(z: np.ndarray, spec: dict, polys: list, tree: STRtree) -> np.ndarray:
    """Finite height cells whose centre lies in a carriageway polygon."""
    finite = np.isfinite(z)
    mask = np.zeros(finite.shape, dtype=bool)
    rr, cc = np.nonzero(finite)
    if rr.size == 0 or not polys:
        return mask
    xs = float(spec["xmin"]) + (cc.astype(np.float64) + 0.5) * RES_M
    ys = float(spec["ymax"]) - (rr.astype(np.float64) + 0.5) * RES_M
    tile = box(float(spec["xmin"]), float(spec["ymin"]), float(spec["xmax"]), float(spec["ymax"]))
    inside = np.zeros(rr.shape[0], dtype=bool)
    for idx in tree.query(tile):
        poly = polys[int(idx)]
        minx, miny, maxx, maxy = poly.bounds
        sel = (xs >= minx) & (xs <= maxx) & (ys >= miny) & (ys <= maxy)
        if not np.any(sel):
            continue
        inside[sel] |= intersects_xy(poly, xs[sel], ys[sel])
    mask[rr, cc] = inside
    return mask


def _drop_orphan_cells(grid: dict) -> int:
    """A stair pixel with no three-corner quad cannot become a face."""
    mask = grid["mask"]
    height, width = mask.shape
    rr, cc = np.nonzero(mask)
    kill: list[tuple[int, int]] = []
    for r, c in zip(rr.tolist(), cc.tolist()):
        kept = False
        for r0, c0 in ((r, c), (r, c - 1), (r - 1, c), (r - 1, c - 1)):
            n = 0
            for rr2, cc2 in ((r0, c0), (r0, c0 + 1), (r0 + 1, c0 + 1), (r0 + 1, c0)):
                if 0 <= rr2 < height and 0 <= cc2 < width and mask[rr2, cc2]:
                    n += 1
            if n >= 3:
                kept = True
                break
        if not kept:
            kill.append((int(r), int(c)))
    for r, c in kill:
        mask[r, c] = False
    if kill:
        print(f"dropped {len(kill)} stair pixels that cannot form a face", flush=True)
    return len(kill)


def _erode4(mask: np.ndarray) -> np.ndarray:
    eroded = mask.copy()
    if mask.shape[0] > 1:
        eroded[1:, :] &= mask[:-1, :]
        eroded[:-1, :] &= mask[1:, :]
    if mask.shape[1] > 1:
        eroded[:, 1:] &= mask[:, :-1]
        eroded[:, :-1] &= mask[:, 1:]
    return eroded


def _rim_chains(nbrs: list[list[int]]) -> list[list[int]]:
    """Open chains stop at a junction. A loop stays on its own ring."""
    n = len(nbrs)
    deg = [len(row) for row in nbrs]

    def edge(a: int, b: int) -> tuple[int, int]:
        return (a, b) if a < b else (b, a)

    used: set[tuple[int, int]] = set()
    chains: list[list[int]] = []

    def walk(start: int, nxt: int) -> list[int]:
        chain = [start]
        prev, cur = start, nxt
        used.add(edge(prev, cur))
        while True:
            chain.append(cur)
            if deg[cur] != 2:
                break
            onward = [k for k in nbrs[cur] if k != prev and edge(cur, k) not in used]
            if len(onward) != 1:
                break
            prev, cur = cur, onward[0]
            used.add(edge(prev, cur))
            if cur == start:
                break
        return chain

    for i in range(n):
        if deg[i] == 2:
            continue
        for j in nbrs[i]:
            if edge(i, j) not in used:
                chains.append(walk(i, j))
    for i in range(n):
        if deg[i] != 2:
            continue
        for j in nbrs[i]:
            if edge(i, j) not in used:
                chains.append(walk(i, j))
    return chains


def _lowpass_ring(z: np.ndarray, edge: np.ndarray) -> int:
    """Median, then a Gaussian, along one rim ring. Left and right stay apart."""
    from scipy.ndimage import gaussian_filter1d, median_filter

    ys, xs = np.nonzero(edge)
    n = int(ys.shape[0])
    if n == 0:
        return 0
    index = {(int(ys[i]), int(xs[i])): i for i in range(n)}
    nbrs: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        r = int(ys[i])
        c = int(xs[i])
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                j = index.get((r + dr, c + dc))
                if j is not None:
                    nbrs[i].append(j)
    old = np.asarray(z[ys, xs], dtype=np.float64)
    acc = np.zeros(n, dtype=np.float64)
    weight = np.zeros(n, dtype=np.float64)
    median_px = int(round(2.0 * RIM_MEDIAN_M / RES_M)) | 1
    sigma_px = RIM_LOWPASS_SIGMA_M / RES_M
    for chain in _rim_chains(nbrs):
        if len(chain) < 2:
            continue
        closed = (
            len(chain) >= 3
            and len(nbrs[chain[0]]) == 2
            and chain[0] in nbrs[chain[-1]]
        )
        nodes = chain
        vals = old[np.asarray(nodes, dtype=np.int64)]
        mode = "wrap" if closed else "nearest"
        if vals.shape[0] < 2:
            continue
        size = median_px if median_px < vals.shape[0] else (int(vals.shape[0]) - (1 - int(vals.shape[0]) % 2))
        if size < 1:
            size = 1
        med = median_filter(vals, size=size, mode=mode)
        smooth = gaussian_filter1d(med.astype(np.float64), sigma=sigma_px, mode=mode)
        acc[np.asarray(nodes, dtype=np.int64)] += smooth
        weight[np.asarray(nodes, dtype=np.int64)] += 1.0
    new = old.copy()
    hit = weight > 0.0
    new[hit] = acc[hit] / weight[hit]
    z[ys, xs] = new.astype(np.float32)
    return int(np.count_nonzero(np.abs(new - old) >= 0.02))


def _assign_edge_band(grid: dict) -> int:
    """Give every cell within 1 m of the outline the nearby road height.

    The donor is the nearest carriageway cell that sits more than 1 m inside,
    or the centre of a road that is itself narrower than 2 m. A donor more
    than a few metres away is left alone, so a parallel road is not copied.

    Distance to the outline comes from the mask, not from each polygon ring.
    A closed at-grade union may be one large outline; walking every ring would
    not finish.
    """
    from scipy.ndimage import distance_transform_edt, maximum_filter
    from scipy.spatial import cKDTree

    z = grid["z"]
    if not z.flags.writeable:
        z = np.array(z, dtype=np.float32, copy=True)
        grid["z"] = z
    mask = grid["mask"]
    if not mask.any():
        return 0
    dist_img = np.full(mask.shape, -1.0, dtype=np.float32)
    dist_img[mask] = (distance_transform_edt(mask) * RES_M).astype(np.float32)[mask]
    local_max = maximum_filter(dist_img, size=3, mode="constant", cval=-1.0)
    on_mask = dist_img >= 0.0
    center = on_mask & (dist_img <= EDGE_BAND_M) & (dist_img + 1.0e-4 >= local_max)
    donors = (on_mask & (dist_img > EDGE_BAND_M) & np.isfinite(z)) | (center & np.isfinite(z))
    band = on_mask & (dist_img <= EDGE_BAND_M) & ~donors & np.isfinite(z)
    dry, drx = np.nonzero(donors)
    by, bx = np.nonzero(band)
    if dry.size == 0 or by.size == 0:
        return 0
    tree = cKDTree(np.column_stack([dry, drx]).astype(np.float64))
    _dd, ii = tree.query(
        np.column_stack([by, bx]).astype(np.float64),
        k=1,
        distance_upper_bound=EDGE_DONOR_M / RES_M,
    )
    ok = ii < dry.shape[0]
    z[by[ok], bx[ok]] = z[dry[ii[ok]], drx[ii[ok]]]
    return int(np.count_nonzero(ok))


def _paint_outside_band(grid: dict) -> int:
    """Copy the smoothed road height into the 1 m strip just outside the polygon.

    A cut vertex on the outline samples the raster on both sides. The outside
    sample has to be the road, otherwise the slope beside the carriageway
    comes back into the edge.
    """
    from scipy.ndimage import binary_dilation
    from scipy.spatial import cKDTree

    z = grid["z"]
    mask = grid["mask"]
    reach = int(math.ceil(EDGE_BAND_M / RES_M))
    square = np.ones((3, 3), dtype=bool)
    outside = binary_dilation(mask, structure=square, iterations=reach) & ~mask
    oy, ox = np.nonzero(outside)
    my, mx = np.nonzero(mask & np.isfinite(z))
    if oy.size == 0 or my.size == 0:
        return 0
    tree = cKDTree(np.column_stack([my, mx]).astype(np.float64))
    dist, ii = tree.query(
        np.column_stack([oy, ox]).astype(np.float64),
        k=1,
        distance_upper_bound=EDGE_BAND_M / RES_M,
    )
    ok = (ii < my.shape[0]) & np.isfinite(dist)
    z[oy[ok], ox[ok]] = z[my[ii[ok]], mx[ii[ok]]]
    return int(np.count_nonzero(ok))


def _smooth_edge_heights(grid: dict) -> tuple[int, int, int]:
    """Fill the outer metre from the road, then low-pass that band along the road."""
    assigned = _assign_edge_band(grid)
    z = grid["z"]
    band = grid["mask"].copy()
    smoothed = 0
    for _ring in range(RIM_RINGS):
        edge = band & ~_erode4(band) & np.isfinite(z)
        smoothed += _lowpass_ring(z, edge)
        band = _erode4(band)
    outside = _paint_outside_band(grid)
    return assigned, smoothed, outside


def _surface_array(grid: dict) -> np.ndarray:
    """Heights the mesh is cut from, after edge fill, NaN off the road plus the 1 m rim."""
    from scipy.ndimage import binary_dilation

    z = np.asarray(grid["z"], dtype=np.float32)
    mask = grid["mask"]
    reach = int(math.ceil(EDGE_BAND_M / RES_M))
    keep = binary_dilation(mask, structure=np.ones((3, 3), dtype=bool), iterations=reach)
    return np.where(keep & np.isfinite(z), z, np.float32(np.nan))


def _nanmin_stack(acc: np.ndarray | None, layer: np.ndarray) -> np.ndarray:
    """Lowest finite height. The terrain clamp must sit under the underpass, not the deck."""
    if acc is None:
        return layer.copy()
    both = np.isfinite(acc) & np.isfinite(layer)
    out = acc.copy()
    only_layer = ~np.isfinite(acc) & np.isfinite(layer)
    out[both] = np.minimum(acc[both], layer[both])
    out[only_layer] = layer[only_layer]
    return out


def _write_surface(out_dir: Path, grid: dict, z: np.ndarray | None = None) -> Path:
    """The heights the mesh is actually cut from, after edge fill and smoothing.

    Road cells plus the 1 m strip outside the outline that ``_paint_outside_band``
    filled; everything else NaN. ``apply_corridor_dgm.py`` clamps the terrain
    against this raster, so the ceiling follows the mesh top and not the
    repaired DGM, which differs from the mesh in the outer metre. Where an
    overpass and an underpass occupy the same XY, the stored value is the
    lower surface so the gorge is not allowed up to the deck.
    """
    out = _surface_array(grid) if z is None else z
    dest = out_dir / "road_grid_z.tif"
    write_referenced_tif(dest, out, xmin=float(grid["xmin"]), ymax=float(grid["ymax"]), res=RES_M)
    print(f"Wrote {dest} cells={int(np.count_nonzero(np.isfinite(out)))}", flush=True)
    return dest


def _note_seams(grid: dict, pos: np.ndarray, faces: np.ndarray) -> None:
    """Count boundary edges. A shared part cut that matches in height counts twice."""
    bag: dict[tuple, int] = grid.setdefault("seams", {})
    local: dict[tuple, int] = {}
    for tri in faces:
        corners = (int(tri[0]), int(tri[1]), int(tri[2]))
        for u, v in ((corners[0], corners[1]), (corners[1], corners[2]), (corners[2], corners[0])):
            p = pos[u]
            q = pos[v]
            ku = (
                round(float(p[0]), SEAM_DECIMALS),
                round(float(p[1]), SEAM_DECIMALS),
            )
            kv = (
                round(float(q[0]), SEAM_DECIMALS),
                round(float(q[1]), SEAM_DECIMALS),
            )
            zu = round(float(p[2]), SEAM_DECIMALS)
            zv = round(float(q[2]), SEAM_DECIMALS)
            if ku <= kv:
                key = (ku, kv, zu, zv)
            else:
                key = (kv, ku, zv, zu)
            local[key] = local.get(key, 0) + 1
    for key, count in local.items():
        if count == 1:
            bag[key] = bag.get(key, 0) + 1


def _assert_sealed(grid: dict, sc: SiteCoords) -> None:
    """Reject a part cut that does not meet, and a hole that is not the outline."""
    from shapely.geometry import Point

    bag: dict[tuple, int] = grid.get("seams") or {}
    groups: dict[tuple, list[tuple[float, float]]] = {}
    for key, count in bag.items():
        if count != 1:
            continue
        ku, kv, zu, zv = key
        groups.setdefault((ku, kv), []).append((float(zu), float(zv)))
    cracks = 0
    holes = 0
    hole_at = None
    crack_at = None
    polys = grid.get("polys") or []
    tree = grid.get("tree")
    for (ku, kv), heights in groups.items():
        if len(heights) >= 2:
            z0 = 0.5 * (heights[0][0] + heights[0][1])
            z1 = 0.5 * (heights[1][0] + heights[1][1])
            if abs(z0 - z1) > 0.01:
                cracks += 1
                if crack_at is None:
                    mx = 0.5 * (ku[0] + kv[0])
                    my = 0.5 * (ku[1] + kv[1])
                    cx, cy = sc.terrain_to_crs(mx, my)
                    crack_at = (round(cx, 1), round(cy, 1), round(abs(z0 - z1), 3))
            continue
        if tree is None or not polys:
            continue
        mx = 0.5 * (ku[0] + kv[0])
        my = 0.5 * (ku[1] + kv[1])
        x, y = sc.terrain_to_crs(mx, my)
        if min(
            x - float(grid["xmin"]),
            float(grid["xmax"]) - x,
            y - float(grid["ymin"]),
            float(grid["ymax"]) - y,
        ) <= RES_M:
            continue
        pt = Point(x, y)
        near = False
        for idx in tree.query(box(x - 1.0, y - 1.0, x + 1.0, y + 1.0)):
            if polys[int(idx)].boundary.distance(pt) <= SEAM_INSIDE_M:
                near = True
                break
        if not near:
            holes += 1
            if hole_at is None:
                hole_at = (round(x, 1), round(y, 1))
    if cracks or holes:
        raise SystemExit(
            f"mesh gaps: {cracks} part cuts with different height "
            f"(first at CRS {crack_at}, (x, y, dz_m)), "
            f"{holes} open edges inside the road, first {hole_at}"
        )
    rim = sum(1 for heights in groups.values() if len(heights) == 1)
    print(f"seams sealed, outline edges {rim}", flush=True)


def _write_mask(proc: Path, grid: dict, stats: dict) -> Path:
    """One uint8 raster, same frame as the height GeoTIFF. 255 = carriageway."""
    dest = proc / "road_grid_mask.tif"
    old_dir = proc / "road_grid_mask"
    if old_dir.is_dir():
        for stale in old_dir.glob("*"):
            try:
                stale.unlink()
            except OSError:
                print(f"  could not remove {stale.name}", flush=True)
        try:
            old_dir.rmdir()
        except OSError:
            pass
    write_referenced_tif(
        dest,
        (grid["mask"].astype(np.uint8) * 255),
        xmin=float(grid["xmin"]),
        ymax=float(grid["ymax"]),
        res=RES_M,
    )
    n_road = int(grid["mask"].sum())
    report = {
        "what": (
            f"Cells inside the clip polygons ({stats.get('layer') or stats.get('landnutzung') or 'carriageway'}), "
            "inside the height window. This is the mesh selection."
        ),
        "raster": dest.name,
        "value": "255 inside the carriageway polygon, 0 outside",
        "row0": "north, same pixel frame as corridor50_road.tif",
        "resolution_m": RES_M,
        "road_px": n_road,
        "xmin": grid["xmin"],
        "ymin": grid["ymin"],
        "xmax": grid["xmax"],
        "ymax": grid["ymax"],
        "width": int(grid["mask"].shape[1]),
        "height": int(grid["mask"].shape[0]),
        "polygons": stats,
    }
    dest.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {dest} road_px={n_road}", flush=True)
    return dest


def _mosaic_matches(index: dict, frame: dict, n_tiles: int) -> bool:
    if int(index.get("tiles") or -1) != n_tiles:
        return False
    return all(index.get(key) == frame[key] for key in ("xmin", "ymin", "xmax", "ymax", "width", "height"))


def _ensure_raw_mosaic(site: dict, proc: Path) -> tuple[Path, dict]:
    """One GeoTIFF of the downloaded 0.5 m squares, no grade filter.

    Rebuilds when the tile set or its frame changed, so a newly fetched
    Gemeindestraße square is not left out of an older mosaic.
    """
    out_dir = proc / "corridor50_raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "corridor50_raw_index.json"
    raster = out_dir / "corridor50_raw.tif"
    raw_dir = ROOT / "data" / "raw" / f"dgm_{site_slug(site)}_corridor50"
    src = json.loads((raw_dir / "corridor_index.json").read_text(encoding="utf-8"))
    specs = list(src["tiles"])
    frame = raster_frame(specs)
    if raster.is_file() and index_path.is_file():
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if _mosaic_matches(index, frame, len(specs)):
            return out_dir, index
        print("corridor mosaic is behind the downloaded tiles, rebuilding", flush=True)
    dat = out_dir / "_mosaic.dat"
    if dat.exists():
        dat.unlink()
    surface = np.memmap(dat, dtype=np.float32, mode="w+", shape=(frame["height"], frame["width"]))
    surface[:] = np.nan
    for spec in specs:
        arr = np.asarray(tiff.imread(raw_dir / spec["name"]), dtype=np.float32)
        if arr.ndim == 3:
            arr = arr[..., 0]
        bad = elevation_nodata_mask(arr)
        if np.any(bad):
            arr = arr.copy()
            arr[bad] = np.nan
        r0 = int(round((frame["ymax"] - float(spec["ymax"])) / RES_M))
        c0 = int(round((float(spec["xmin"]) - frame["xmin"]) / RES_M))
        h, w = arr.shape
        surface[r0 : r0 + h, c0 : c0 + w] = arr
        print(f"  raw {spec['name']}", flush=True)
    write_referenced_tif(
        raster,
        np.asarray(surface),
        xmin=frame["xmin"],
        ymax=frame["ymax"],
        res=RES_M,
    )
    surface.flush()
    del surface
    dat.unlink(missing_ok=True)
    index = {
        "raster": raster.name,
        "source": "unfiltered 0.5 m DGM",
        "resolution_m": RES_M,
        "crs": str(src.get("crs") or "EPSG:31254"),
        "xmin": frame["xmin"],
        "ymin": frame["ymin"],
        "xmax": frame["xmax"],
        "ymax": frame["ymax"],
        "width": frame["width"],
        "height": frame["height"],
        "row0": "north",
        "tiles": len(specs),
    }
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {raster} {frame['width']}x{frame['height']}", flush=True)
    return out_dir, index


def _arg_value(flag: str) -> str | None:
    if flag not in sys.argv:
        return None
    i = sys.argv.index(flag)
    if i + 1 >= len(sys.argv) or sys.argv[i + 1].startswith("--"):
        raise SystemExit(f"{flag} needs a path")
    return sys.argv[i + 1]


def _load_geotiff(path: Path) -> tuple[np.ndarray, dict]:
    with tiff.TiffFile(path) as src:
        page = src.pages[0]
        z = np.asarray(page.asarray(), dtype=np.float32)
        scale = tuple(page.tags["ModelPixelScaleTag"].value)
        tie = tuple(page.tags["ModelTiepointTag"].value)
    if z.ndim == 3:
        z = z[..., 0]
    bad = (~np.isfinite(z)) | (z <= -9999.0) | (z < 50.0) | (z > 4500.0)
    if bad.any():
        z = z.copy()
        z[bad] = np.nan
    res_x = float(scale[0])
    res_y = float(scale[1])
    if abs(res_x - RES_M) > 1e-6 or abs(res_y - RES_M) > 1e-6:
        raise SystemExit(f"{path} pixel size is {res_x} x {res_y}, expected {RES_M}")
    height, width = z.shape
    xmin = float(tie[3])
    ymax = float(tie[4])
    return z, {
        "raster": path.name,
        "source": str(path),
        "xmin": xmin,
        "ymin": ymax - height * RES_M,
        "xmax": xmin + width * RES_M,
        "ymax": ymax,
        "width": int(width),
        "height": int(height),
        "resolution_m": RES_M,
    }


def _row_code_objekt(row) -> tuple[str, str]:
    code = str(row.get("STR_CODE") or row.get("str_code") or "").strip()
    obj = str(row.get("OBJEKT") or row.get("objekt") or "").upper().strip()
    return code, obj


def _main_route_row(row) -> bool:
    """Landesstraße, Bundesstraße, Autobahn, including their ramps."""
    code, obj = _row_code_objekt(row)
    if _MAIN_ROAD_CODE.match(code):
        return True
    return obj == "S-A"


def _road_mesh_row(row) -> bool:
    """Main routes plus Gemeindestraße S-G. S-GW is a track and stays out."""
    from gip_road_segments import road_mesh_piece

    return road_mesh_piece(row)


def _mesh_prefix(kind: str, key: int) -> str:
    if kind == "overpass":
        return f"over{int(key)}_"
    if kind == "underpass":
        return f"under{int(key)}_"
    if kind == "span":
        return f"span{int(key)}_"
    return "part_"


def _polys_of(geom) -> list:
    out = []
    if geom is None or geom.is_empty:
        return out
    if geom.geom_type == "MultiPolygon":
        out.extend(part for part in geom.geoms if part.geom_type == "Polygon" and part.area > 1.0)
    elif geom.geom_type == "Polygon" and geom.area > 1.0:
        out.append(geom)
    return out


def _load_clip_polygons(path: Path, layer: str, *, main_only: bool = False) -> tuple[list[dict], dict]:
    import geopandas as gpd

    gdf = gpd.read_file(path, layer=layer)
    if main_only:
        keep = gdf.apply(_main_route_row, axis=1)
        gdf = gdf.loc[keep]
    if "objectid" in gdf.columns and "members" not in gdf.columns:
        from gip_routing_load import apply_routing_carriageways
        from site_coords import load_site

        gdf, routing = apply_routing_carriageways(gdf, load_site())
        if routing.get("used"):
            print(
                f"routing layers: {routing['groups']} groups, "
                f"{routing['merged_groups']} merged, "
                f"{routing.get('grade_overpass', 0)} overpass / "
                f"{routing.get('grade_underpass', 0)} underpass meshes",
                flush=True,
            )
    buckets: dict[tuple[str, int], dict] = {}
    has_kind = "mesh_kind" in gdf.columns
    has_key = "mesh_key" in gdf.columns
    has_layer = "mesh_layer" in gdf.columns
    for rec in gdf.itertuples(index=False):
        kind = "road"
        key = 0
        mesh_layer = 0
        if has_kind:
            kind = str(getattr(rec, "mesh_kind", "road") or "road").strip() or "road"
        if has_key:
            try:
                key = int(getattr(rec, "mesh_key", 0) or 0)
            except (TypeError, ValueError):
                key = 0
        if has_layer:
            try:
                mesh_layer = int(getattr(rec, "mesh_layer", 0) or 0)
            except (TypeError, ValueError):
                mesh_layer = 0
        if kind in ("road", "junction", "link"):
            # One road raster (no link/junction split). Files split at 65535 verts.
            kind = "road"
            key = 0
        elif key == 0:
            try:
                key = int(getattr(rec, "objectid", 0) or 0)
            except (TypeError, ValueError):
                key = 0
        polys = _polys_of(rec.geometry)
        if not polys:
            continue
        slot = buckets.setdefault((kind, key), {"polys": [], "layer": mesh_layer})
        slot["polys"].extend(polys)
        slot["layer"] = mesh_layer
    # carriageway_mesh_clip.gpkg already dissolved the at-grade surface into
    # overlapping tiles. Do not grow those again.
    unified = False
    if "members" in gdf.columns:
        unified = any(
            str(m) == "unified" for m in gdf["members"].tolist() if m is not None
        )
    for (kind, _key), slot in buckets.items():
        if len(slot["polys"]) < 2:
            continue
        if kind in ("overpass", "underpass", "span"):
            # Half a metre glues stacked deck pieces of one bridge.
            merged = shapely.make_valid(shapely.union_all(slot["polys"]))
            merged = shapely.make_valid(merged.buffer(0.5).buffer(-0.5))
            closed = _polys_of(merged)
            if closed:
                slot["polys"] = closed
        elif kind == "road" and not unified:
            grown = []
            for poly in slot["polys"]:
                g = shapely.make_valid(poly.buffer(AT_GRADE_CLOSE_M))
                grown.extend(_polys_of(g))
            if grown:
                slot["polys"] = grown
    groups = []
    for (kind, key), slot in sorted(
        buckets.items(), key=lambda item: (0 if item[0][0] == "road" else 1, item[0][0], item[0][1])
    ):
        groups.append(
            {
                "kind": kind,
                "key": int(key),
                "layer": int(slot["layer"]),
                "prefix": _mesh_prefix(kind, key),
                "polys": slot["polys"],
            }
        )
    if not groups:
        raise SystemExit(f"no polygons in {path} layer {layer}")
    n_poly = sum(len(g["polys"]) for g in groups)
    stats = {
        "clip": str(path),
        "layer": layer,
        "polygons": n_poly,
        "mesh_layers": [
            {"kind": g["kind"], "key": g["key"], "polygons": len(g["polys"])} for g in groups
        ],
        "main_only": bool(main_only),
        "at_grade_unified": bool(unified),
    }
    counts: dict[str, int] = {}
    for group in groups:
        counts[group["kind"]] = counts.get(group["kind"], 0) + 1
    summary = ", ".join(f"{kind}={count}" for kind, count in sorted(counts.items()))
    road_n = next((len(g["polys"]) for g in groups if g["kind"] == "road"), 0)
    note = "unified tiles" if unified else f"overlap {AT_GRADE_CLOSE_M*100:.0f} cm"
    print(
        f"clip {layer}: {n_poly} polygons in {len(groups)} meshes ({summary})"
        f"; at-grade {note}, {road_n} pieces",
        flush=True,
    )
    return groups, stats


def main() -> None:
    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "").strip()
    if not level_name:
        raise SystemExit("beamng.level_name missing")
    meta = json.loads((proc / "heightmap_meta.json").read_text(encoding="utf-8"))
    z_min = float(meta["z_min_m"])
    max_h = float(meta["max_height_m"])
    use_raw = "--raw" in sys.argv
    heights_arg = _arg_value("--heights")
    if use_raw and heights_arg:
        raise SystemExit("--raw and --heights exclude each other")
    if heights_arg:
        heights_path = Path(heights_arg)
        if not heights_path.is_file():
            raise SystemExit(f"heights raster missing: {heights_path}")
        z, index = _load_geotiff(heights_path)
        raw_index = proc / "corridor50_raw" / "corridor50_raw_index.json"
        road_index = proc / "corridor50_road" / "corridor50_road_index.json"
        ref_path = raw_index if raw_index.is_file() else road_index
        ref = json.loads(ref_path.read_text(encoding="utf-8"))
        for key in ("xmin", "ymin", "xmax", "ymax", "width", "height"):
            if index[key] != ref[key]:
                raise SystemExit(
                    f"heights frame {key}={index[key]} does not match corridor {ref[key]}"
                )
        height_label = f"feature-preserving smoothing ({heights_path.name})"
    elif use_raw:
        filt_dir, index = _ensure_raw_mosaic(site, proc)
        z = np.asarray(tiff.imread(filt_dir / index["raster"]), dtype=np.float32)
        height_label = "raw 0.5 m DGM"
    else:
        filt_dir = proc / "corridor50_road"
        index = json.loads((filt_dir / "corridor50_road_index.json").read_text(encoding="utf-8"))
        if "raster" not in index:
            raise SystemExit("corridor50_road index has no single raster; run the filter again")
        z = np.asarray(tiff.imread(filt_dir / index["raster"]), dtype=np.float32)
        height_label = "filtered corridor"
    if z.ndim == 3:
        z = z[..., 0]
    frame = {
        "xmin": float(index["xmin"]),
        "ymin": float(index["ymin"]),
        "xmax": float(index["xmax"]),
        "ymax": float(index["ymax"]),
    }
    z_top = z
    z_by_layer: dict[int, np.ndarray] = {0: z_top}
    if heights_arg:
        parent = Path(heights_arg).parent
        layer0_path = parent / "04_layer0.tif"
        if layer0_path.is_file():
            z0 = np.asarray(tiff.imread(layer0_path), dtype=np.float32)
            if z0.ndim == 3:
                z0 = z0[..., 0]
            if z0.shape == z_top.shape:
                z_by_layer[0] = z0
                print(f"layer 0 heights {layer0_path.name}", flush=True)
        under_path = parent / "04_under.tif"
        if under_path.is_file():
            z1 = np.asarray(tiff.imread(under_path), dtype=np.float32)
            if z1.ndim == 3:
                z1 = z1[..., 0]
            if z1.shape == z_top.shape:
                z_by_layer[1] = z1
                print(f"layer 1 heights {under_path.name}", flush=True)
            else:
                print(
                    f"04_under.tif shape {z1.shape} != heights {z_top.shape}, ignored",
                    flush=True,
                )
    polys_arg = _arg_value("--clip")
    clip_layer = _arg_value("--clip-layer") or "ribbon"
    if polys_arg:
        groups, poly_stats = _load_clip_polygons(
            Path(polys_arg), clip_layer, main_only="--clip-main" in sys.argv
        )
        clip_label = f"{clip_layer}; one 2.5D mesh per grade-separated layer"
    else:
        polys, poly_stats = _carriageway_polygons(site, proc, sc)
        groups = [{"kind": "road", "key": 0, "layer": 0, "prefix": "part_", "polys": polys}]
        clip_label = "landnutzung polygon; partial quads cut onto the boundary with surface z"
    print(f"road grid: {height_label}", flush=True)

    all_polys = [p for g in groups for p in g["polys"]]
    overview = dict(frame)
    overview["z"] = z_top
    tree_all = STRtree(all_polys) if all_polys else STRtree([])
    overview["mask"] = _clip_mask(z_top, overview, all_polys, tree_all)
    _write_mask(proc, overview, poly_stats)
    if "--masks-only" in sys.argv:
        return

    out_dir = proc / "road_grid"
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.dae"):
        old.unlink()

    written: list[dict] = []
    seam_dump: list[dict] = []
    clamp_z = np.full(z_top.shape, np.nan, dtype=np.float32)
    n_road = 0
    col_verts = 0
    col_tris = 0
    clip_quads = 0
    deck_box = _load_deck_box(sc)
    if deck_box is not None:
        print(
            f"bridge box {DECK_BOX_M:.2f} m under {deck_box['n']} deck ribbons",
            flush=True,
        )
    # Over/under/span own their plan cells. The road mesh must not reuse them.
    grade_polys = [
        p for g in groups if g["kind"] != "road" for p in g["polys"]
    ]
    grade_tree = STRtree(grade_polys) if grade_polys else None

    for group in groups:
        polys = group["polys"]
        if not polys:
            continue
        layer = int(group.get("layer") or 0)
        z_src = z_by_layer.get(layer, z_top)
        if z_src.shape != z_top.shape:
            z_src = z_top
        window = _crop_window(z_src, frame, polys)
        if window is None:
            print(f"  skip {group['prefix']}: outside the raster", flush=True)
            continue
        r0, r1, c0, c1, sub = window
        grid = dict(sub)
        grid["z"] = np.array(z_src[r0:r1, c0:c1], dtype=np.float32, copy=True)
        grid["deck_box"] = (
            deck_box if group["kind"] in ("overpass", "road") else None
        )
        tree = STRtree(polys)
        grid["polys"] = polys
        grid["tree"] = tree
        grid["mask"] = _clip_mask(grid["z"], grid, polys, tree)
        if group["kind"] == "road" and grade_tree is not None:
            taken = _clip_mask(grid["z"], grid, grade_polys, grade_tree)
            n_taken = int((grid["mask"] & taken).sum())
            if n_taken:
                grid["mask"] &= ~taken
                print(
                    f"  {group['prefix']} exclusive: dropped {n_taken} cells "
                    f"owned by over/under/span",
                    flush=True,
                )
        n_layer = int(grid["mask"].sum()) - _drop_orphan_cells(grid)
        if n_layer == 0:
            print(f"  skip {group['prefix']}: no cells", flush=True)
            continue
        n_band, n_smooth, n_out = _smooth_edge_heights(grid)
        print(
            f"  {group['prefix']} cells={n_layer} edge filled {n_band}, "
            f"smoothed {n_smooth}, outside {n_out}",
            flush=True,
        )
        layer_written: list[dict] = []
        covered: set = set()
        _emit_rows(
            grid, sc,
            r0=0, r1=int(grid["mask"].shape[0]),
            z_min=z_min, max_h=max_h,
            out_dir=out_dir, written=layer_written, covered=covered,
            prefix=group["prefix"],
        )
        seam_dump.extend(
            _bind_seam_names(grid.get("file_seams") or [], layer_written)
        )
        missing = []
        rr, cc = np.nonzero(grid["mask"])
        for r, c in zip(rr.tolist(), cc.tolist()):
            if (int(r), int(c)) not in covered:
                missing.append((int(r), int(c)))
        if missing:
            raise SystemExit(
                f"{group['prefix']}: {len(missing)} road cells have no face, first {missing[:5]}"
            )
        _assert_sealed(grid, sc)
        for item in layer_written:
            item["kind"] = group["kind"]
            item["key"] = group["key"]
        written.extend(layer_written)
        n_road += n_layer
        col_verts += int(grid.get("col_verts") or 0)
        col_tris += int(grid.get("col_tris") or 0)
        clip_quads += int(grid.get("clip_quads") or 0)
        surf = _surface_array(grid)
        view = clamp_z[r0:r1, c0:c1]
        clamp_z[r0:r1, c0:c1] = _nanmin_stack(view, surf)
        print(
            f"  {group['prefix']} parts={len(layer_written)} "
            f"boundary_quads={int(grid.get('clip_quads') or 0)}",
            flush=True,
        )

    if not written or clamp_z is None:
        raise SystemExit("no road cells")
    _write_surface(out_dir, {**frame, "z": clamp_z, "mask": np.isfinite(clamp_z)}, z=clamp_z)

    _write_material(out_dir / "main.materials.json")
    n_vert = sum(item["verts"] for item in written)
    n_tri = sum(item["tris"] for item in written)
    report = {
        "resolution_m": RES_M,
        "clearance_m": CLEARANCE_M,
        "heights": height_label,
        "clip": clip_label,
        "polygons": poly_stats,
        "road_cells": n_road,
        "parts": len(written),
        "verts": n_vert,
        "tris": n_tri,
        "max_edge_m": MAX_EDGE_M,
        "geometry": (
            "finite, edge<=40m, area>0, normal up, "
            "seams sealed per layer (XY to mm, shared XYZ across part cuts)"
        ),
        "seam_decimals": SEAM_DECIMALS,
        "tiles": written,
    }
    (out_dir / "road_grid_index.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "mesh_seams.json").write_text(
        json.dumps(
            {
                "res_m": RES_M,
                "sample_m": SAMPLE_M,
                "seams": seam_dump,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote mesh_seams.json ({len(seam_dump)} pieces)", flush=True)
    entries = [_entry(level_name, item["name"]) for item in written]
    _inject(level_name, entries, out_dir)
    n_over = sum(1 for item in written if item.get("kind") == "overpass")
    n_under = sum(1 for item in written if item.get("kind") == "underpass")
    print(
        f"Road grid: {n_road} cells, {len(written)} parts "
        f"({n_over} overpass, {n_under} underpass), "
        f"{n_vert} verts, {n_tri} tris, "
        f"collision {col_verts} verts / {col_tris} tris, "
        f"geometry ok",
        flush=True,
    )


if __name__ == "__main__":
    main()
