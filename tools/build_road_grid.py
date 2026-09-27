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
exceed the vertex limit. Partial edge cells stay as triangles.

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
from shapely import clip_by_rect, constrained_delaunay_triangles, intersects_xy
from shapely.geometry import box
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

ROOT = Path(__file__).resolve().parents[1]
COLLADA_MAX_VERTS = 65535
RES_M = 0.5
UV_M = 6.0
# Neighbor cells only. A gorge in the 15 m strip can drop ~17 m in one cell.
MAX_EDGE_M = 40.0
MIN_AREA_M2 = 1.0e-6
SNAP_M = 1.0e-3
# Cells this close to the carriageway outline take the road height.
EDGE_BAND_M = 1.0
# A donor farther than this belongs to another road.
EDGE_DONOR_M = 4.0
# Outer cell rows, smoothed along the road after the band is filled.
RIM_RINGS = 2
RIM_MEDIAN_M = 3.0
RIM_LOWPASS_SIGMA_M = 4.0
# An open edge farther inside than this is a hole, not the outline.
SEAM_INSIDE_M = 0.75
_MAIN_ROAD_CODE = re.compile(r"^[ABLabl]\d")
IDENTITY_ROT = [1, 0, 0, 0, 1, 0, 0, 0, 1]


def _cell_on(grid: dict, r: int, c: int) -> tuple[bool, float, float, float]:
    height, width = grid["mask"].shape
    x = float(grid["xmin"]) + (c + 0.5) * RES_M
    y = float(grid["ymax"]) - (r + 0.5) * RES_M
    if r < 0 or c < 0 or r >= height or c >= width or not grid["mask"][r, c]:
        return False, float("nan"), x, y
    return True, float(grid["z"][r, c]), x, y


def _sample_z(grid: dict, x: float, y: float) -> float:
    """Height of the filtered surface at an arbitrary plan position."""
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


def _build_part(
    grid: dict,
    sc: SiteCoords,
    *,
    r0: int,
    r1: int,
    z_min: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, set, int] | None:
    mask = grid["mask"]
    height, width = mask.shape
    if r0 >= height or r1 <= 0 or not mask[max(0, r0) : min(height, r1 + 1)].any():
        return None
    present = mask.any(axis=1)
    extent = sc.terrain_extent
    index: dict[tuple[int, int], int] = {}
    pos_l: list[tuple[float, float, float]] = []
    uv_l: list[tuple[float, float]] = []
    faces: list[tuple[int, int, int]] = []
    used: set[tuple[int, int]] = set()

    def vid(r: int, c: int, x: float, y: float, z_abs: float) -> int:
        key = (r, c)
        hit = index.get(key)
        if hit is not None:
            return hit
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
        if abx * acy - aby * acx < 0.0:
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
        key = (round(x, 4), round(y, 4))
        hit = boundary.get(key)
        if hit is not None:
            return hit
        z_abs = _sample_z(grid, x, y)
        if not math.isfinite(z_abs):
            z_abs = _z_from_inside(x, y, packed)
        if not math.isfinite(z_abs):
            return None
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
            inter = clip_by_rect(polys[int(idx)], minx, miny, maxx, maxy)
            if not inter.is_empty:
                parts.append(inter)
        if not parts:
            return
        geom = parts[0] if len(parts) == 1 else unary_union(parts)
        emitted = False
        for poly in _iter_polygons(geom):
            if poly.area < MIN_AREA_M2:
                continue
            tris = constrained_delaunay_triangles(poly)
            geoms = tris.geoms if hasattr(tris, "geoms") else (tris,)
            for tri in geoms:
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
    if not faces:
        return None
    pos = np.asarray(pos_l, dtype=np.float64)
    uv = np.asarray(uv_l, dtype=np.float64)
    fac = np.asarray(faces, dtype=np.int32)
    return pos, uv, fac, used, clipped


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
) -> None:
    height = int(grid["mask"].shape[0])
    if (r1 - r0) > 1:
        n_cells = int(grid["mask"][max(0, r0) : min(height, r1)].sum())
        if n_cells > 50000:
            mid = r0 + (r1 - r0) // 2
            _emit_rows(
                grid, sc, r0=r0, r1=mid, z_min=z_min, max_h=max_h,
                out_dir=out_dir, written=written, covered=covered,
            )
            _emit_rows(
                grid, sc, r0=mid, r1=r1, z_min=z_min, max_h=max_h,
                out_dir=out_dir, written=written, covered=covered,
            )
            return
    built = _build_part(grid, sc, r0=r0, r1=r1, z_min=z_min)
    if built is None:
        return
    pos, uv, faces, used, clipped = built
    if pos.shape[0] > COLLADA_MAX_VERTS and (r1 - r0) > 1:
        mid = r0 + (r1 - r0) // 2
        _emit_rows(
            grid, sc, r0=r0, r1=mid, z_min=z_min, max_h=max_h,
            out_dir=out_dir, written=written, covered=covered,
        )
        _emit_rows(
            grid, sc, r0=mid, r1=r1, z_min=z_min, max_h=max_h,
            out_dir=out_dir, written=written, covered=covered,
        )
        return
    if pos.shape[0] > COLLADA_MAX_VERTS:
        raise SystemExit(f"rows {r0}-{r1}: {pos.shape[0]} verts in one row band")
    stem = f"part_{len(written):03d}"
    _check_geometry(pos, faces, stem=stem, z_min=z_min, max_h=max_h)
    col_pos, col_faces = _decimate_collision(pos, faces)
    _check_geometry(col_pos, col_faces, stem=f"{stem} collision", z_min=z_min, max_h=max_h)
    _note_seams(grid, pos, faces)
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
    """
    from scipy.ndimage import maximum_filter
    from scipy.spatial import cKDTree
    from shapely import distance, points

    z = grid["z"]
    if not z.flags.writeable:
        z = np.array(z, dtype=np.float32, copy=True)
        grid["z"] = z
    mask = grid["mask"]
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return 0
    x = float(grid["xmin"]) + (xs.astype(np.float64) + 0.5) * RES_M
    y = float(grid["ymax"]) - (ys.astype(np.float64) + 0.5) * RES_M
    best = np.full(ys.shape[0], np.inf, dtype=np.float64)
    for poly in grid.get("polys") or []:
        minx, miny, maxx, maxy = poly.bounds
        sel = (x >= minx) & (x <= maxx) & (y >= miny) & (y <= maxy)
        idx = np.nonzero(sel)[0]
        if idx.size == 0:
            continue
        d = np.asarray(distance(poly.boundary, points(x[idx], y[idx])), dtype=np.float64)
        best[idx] = np.minimum(best[idx], d)
    known = np.isfinite(best)
    dist_img = np.full(mask.shape, -1.0, dtype=np.float32)
    dist_img[ys[known], xs[known]] = best[known].astype(np.float32)
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


def _note_seams(grid: dict, pos: np.ndarray, faces: np.ndarray) -> None:
    """Count boundary edges. A shared part cut that matches in height counts twice."""
    bag: dict[tuple, int] = grid.setdefault("seams", {})
    local: dict[tuple, int] = {}
    for tri in faces:
        corners = (int(tri[0]), int(tri[1]), int(tri[2]))
        for u, v in ((corners[0], corners[1]), (corners[1], corners[2]), (corners[2], corners[0])):
            p = pos[u]
            q = pos[v]
            ku = (round(float(p[0]), 3), round(float(p[1]), 3))
            kv = (round(float(q[0]), 3), round(float(q[1]), 3))
            zu = round(float(p[2]), 3)
            zv = round(float(q[2]), 3)
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
    polys = grid.get("polys") or []
    tree = grid.get("tree")
    for (ku, kv), heights in groups.items():
        if len(heights) >= 2:
            z0 = 0.5 * (heights[0][0] + heights[0][1])
            z1 = 0.5 * (heights[1][0] + heights[1][1])
            if abs(z0 - z1) > 0.01:
                cracks += 1
            continue
        if tree is None or not polys:
            continue
        mx = 0.5 * (ku[0] + kv[0])
        my = 0.5 * (ku[1] + kv[1])
        x, y = sc.beamng_to_crs(mx, my)
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
            f"mesh gaps: {cracks} part cuts with different height, "
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


def _ensure_raw_mosaic(site: dict, proc: Path) -> tuple[Path, dict]:
    """One GeoTIFF of the downloaded 0.5 m squares, no grade filter."""
    out_dir = proc / "corridor50_raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "corridor50_raw_index.json"
    raster = out_dir / "corridor50_raw.tif"
    if raster.is_file() and index_path.is_file():
        return out_dir, json.loads(index_path.read_text(encoding="utf-8"))
    raw_dir = ROOT / "data" / "raw" / f"dgm_{site_slug(site)}_corridor50"
    src = json.loads((raw_dir / "corridor_index.json").read_text(encoding="utf-8"))
    specs = list(src["tiles"])
    frame = raster_frame(specs)
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


def _main_route_row(row) -> bool:
    """Landesstraße, Bundesstraße, Autobahn, including their ramps."""
    code = str(row.get("STR_CODE") or "").strip()
    obj = str(row.get("OBJEKT") or "").upper().strip()
    if _MAIN_ROAD_CODE.match(code):
        return True
    return obj == "S-A"


def _load_clip_polygons(path: Path, layer: str, *, main_only: bool = False) -> tuple[list, dict]:
    import geopandas as gpd

    gdf = gpd.read_file(path, layer=layer)
    if main_only:
        keep = gdf.apply(_main_route_row, axis=1)
        gdf = gdf.loc[keep]
    polys = []
    for geom in gdf.geometry:
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type == "MultiPolygon":
            polys.extend(part for part in geom.geoms if part.geom_type == "Polygon" and part.area > 1.0)
        elif geom.geom_type == "Polygon" and geom.area > 1.0:
            polys.append(geom)
    if not polys:
        raise SystemExit(f"no polygons in {path} layer {layer}")
    stats = {
        "clip": str(path),
        "layer": layer,
        "polygons": len(polys),
        "main_only": bool(main_only),
    }
    print(f"clip {layer}: {len(polys)} polygons from {path}", flush=True)
    return polys, stats


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
        ref = json.loads(
            (proc / "corridor50_road" / "corridor50_road_index.json").read_text(encoding="utf-8")
        )
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
    grid = {
        "z": z,
        "xmin": float(index["xmin"]),
        "ymin": float(index["ymin"]),
        "xmax": float(index["xmax"]),
        "ymax": float(index["ymax"]),
    }
    polys_arg = _arg_value("--clip")
    clip_layer = _arg_value("--clip-layer") or "ribbon"
    if polys_arg:
        polys, poly_stats = _load_clip_polygons(
            Path(polys_arg), clip_layer, main_only="--clip-main" in sys.argv
        )
        clip_label = f"{clip_layer}; partial quads cut onto the boundary with surface z"
    else:
        polys, poly_stats = _carriageway_polygons(site, proc, sc)
        clip_label = "landnutzung polygon; partial quads cut onto the boundary with surface z"
    tree = STRtree(polys)
    print(f"road grid: {height_label}", flush=True)
    grid["polys"] = polys
    grid["tree"] = tree
    grid["mask"] = _clip_mask(z, grid, polys, tree)
    n_road = int(grid["mask"].sum()) - _drop_orphan_cells(grid)
    n_band, n_smooth, n_out = _smooth_edge_heights(grid)
    print(
        f"edge band {EDGE_BAND_M:.0f} m, cells filled {n_band}, "
        f"smoothed {n_smooth}, outside {n_out}",
        flush=True,
    )
    _write_mask(proc, grid, poly_stats)
    if "--masks-only" in sys.argv:
        return
    if n_road == 0:
        raise SystemExit("no road cells")

    out_dir = proc / "road_grid"
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.dae"):
        old.unlink()
    written: list[dict] = []
    covered: set = set()
    _emit_rows(
        grid, sc,
        r0=0, r1=int(grid["mask"].shape[0]),
        z_min=z_min, max_h=max_h,
        out_dir=out_dir, written=written, covered=covered,
    )
    print(
        f"  mesh parts={len(written)} boundary_quads={int(grid.get('clip_quads') or 0)}",
        flush=True,
    )

    missing = []
    rr, cc = np.nonzero(grid["mask"])
    for r, c in zip(rr.tolist(), cc.tolist()):
        if (int(r), int(c)) not in covered:
            missing.append((int(r), int(c)))
    if missing:
        raise SystemExit(
            f"{len(missing)} road cells have no face, first {missing[:5]}"
        )

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
        "covered_cells": len(covered),
        "parts": len(written),
        "verts": n_vert,
        "tris": n_tri,
        "max_edge_m": MAX_EDGE_M,
        "geometry": "finite, edge<=40m, area>0, normal up, seams sealed",
        "tiles": written,
    }
    (out_dir / "road_grid_index.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    _assert_sealed(grid, sc)
    entries = [_entry(level_name, item["name"]) for item in written]
    _inject(level_name, entries, out_dir)
    print(
        f"Road grid: {n_road} cells, all covered, {len(written)} parts, "
        f"{n_vert} verts, {n_tri} tris, "
        f"collision {int(grid.get('col_verts') or 0)} verts / {int(grid.get('col_tris') or 0)} tris, "
        f"geometry ok",
        flush=True,
    )


if __name__ == "__main__":
    main()
