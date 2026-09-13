"""Inject BeamNG WaterBlock / River from Tirol landcover Gewässer polygons.

Standing water (LN-GWS) → WaterBlock(s) + optional heightmap basin carve.
Terrain Mud stays as shore/bed paint; reflective water needs WaterBlock meshes.

Large lakes: shrink by shore_inset_m, then tile WaterBlocks that overlap the
core. standing_depress_m lowers the DGM under LN-GWS so blocks sit in a hole.
WaterBlock Z comes from densified shoreline samples (not centroid — islands).

Flowing water (LN-GWF) → optional River, or WaterBlocks when wide_flowing_min_width_m
is set and the floodplain is wide enough.

Usage:
  $env:AUTOROAD_SITE='config/sites/l13_kuehtai.yaml'
  python tools/build_water.py
"""
from __future__ import annotations

import json
import math
import sys
import uuid
from pathlib import Path

import numpy as np
from pyproj import Transformer
from shapely.geometry import Polygon
from shapely.validation import make_valid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import SiteCoords, load_site, processed_dir  # noqa: E402
import build_bridges as bb  # noqa: E402
import build_galleries as bg  # noqa: E402
from build_terrain_masks import (  # noqa: E402
    _load_landcover_geojson,
    _tirol_landnutzung_kind,
)

USER_LEVELS = bb.USER_LEVELS


def _cfg(bng: dict) -> dict:
    raw = bng.get("water") or {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        # Landcover "Gewässer fließend" is a floodplain polygon — PCA Rivers become
        # huge flat slabs. Default: terrain Mud only, no River mesh.
        "flowing": str(raw.get("flowing") or "none").lower(),  # none | river
        "standing": str(raw.get("standing") or "waterblock").lower(),  # none | waterblock
        "material": str(raw.get("material") or ""),
        "depth_m": float(raw.get("depth_m") or 3.0),
        "surface_lift_m": float(raw.get("surface_lift_m") or 0.15),
        # Global Z nudge for all WaterBlocks (m). Positive = higher.
        "waterlevel_z_offset_m": float(raw.get("waterlevel_z_offset_m") or 0.0),
        "min_area_m2": float(raw.get("min_area_m2") or 40.0),
        # Single WaterBlock AABB up to this side length; larger lakes are tiled.
        "standing_max_side_m": float(raw.get("standing_max_side_m") or 80.0),
        "standing_tile_m": float(raw.get("standing_tile_m") or 64.0),
        # Positive: erode polygon so Mud rim stays visible around WaterBlocks.
        "shore_inset_m": float(raw.get("shore_inset_m") or 4.0),
        # Grow WaterBlock XY beyond lake AABB (fraction, e.g. 0.08 = +8%).
        "standing_xy_pad": float(raw.get("standing_xy_pad") or 0.0),
        # LN-GWF wider than this (PCA half-width*2) also get WaterBlocks.
        "wide_flowing_min_width_m": float(raw.get("wide_flowing_min_width_m") or 0.0),
        # Lower terrain under LN-GWS lakes so WaterBlocks have a basin (meters).
        "standing_depress_m": float(raw.get("standing_depress_m") or 0.0),
        "standing_depress_blend_m": float(raw.get("standing_depress_blend_m") or 6.0),
        "river_min_length_m": float(raw.get("river_min_length_m") or 12.0),
        "river_min_width_m": float(raw.get("river_min_width_m") or 2.0),
        "river_max_width_m": float(raw.get("river_max_width_m") or 12.0),
        "river_depth_m": float(raw.get("river_depth_m") or 1.5),
        "grid_element_size": float(raw.get("grid_element_size") or 4.0),
        "segment_length": float(raw.get("segment_length") or 8.0),
        "subdivide_length": float(raw.get("subdivide_length") or 2.5),
    }


def _rings_wgs84(geom: dict) -> list[list[tuple[float, float]]]:
    gtype = (geom or {}).get("type")
    coords = (geom or {}).get("coordinates")
    rings: list[list[tuple[float, float]]] = []
    if gtype == "Polygon" and coords:
        rings.append([(float(c[0]), float(c[1])) for c in coords[0] if len(c) >= 2])
    elif gtype == "MultiPolygon" and coords:
        for poly in coords:
            if poly:
                rings.append([(float(c[0]), float(c[1])) for c in poly[0] if len(c) >= 2])
    return [r for r in rings if len(r) >= 3]


def _to_beamng(
    ring_ll: list[tuple[float, float]],
    to_crs: Transformer,
    coords: SiteCoords,
) -> np.ndarray:
    pts = []
    for lon, lat in ring_ll:
        x, y = to_crs.transform(lon, lat)
        bx, by = coords.crs_to_beamng(float(x), float(y))
        pts.append((bx, by))
    return np.asarray(pts, dtype=np.float64)


def _poly_area(xy: np.ndarray) -> float:
    x, y = xy[:, 0], xy[:, 1]
    return 0.5 * float(np.abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def _densify_ring(xy: np.ndarray, spacing_m: float) -> np.ndarray:
    """Evenly sample along a closed ring (last point may equal first)."""
    if xy.shape[0] < 2:
        return xy
    pts = [xy[0]]
    for i in range(len(xy) - 1):
        p0 = xy[i]
        p1 = xy[i + 1]
        seg = p1 - p0
        length = float(np.hypot(seg[0], seg[1]))
        if length < 1e-6:
            continue
        n = max(1, int(math.ceil(length / max(spacing_m, 0.5))))
        for k in range(1, n + 1):
            t = k / n
            pts.append(p0 + t * seg)
    return np.asarray(pts, dtype=np.float64)


def _lake_surface_z(z_at, xy: np.ndarray, *, spacing_m: float = 8.0) -> float:
    """Estimate flat water surface height from DGM (island-safe).

    Prefer the modal elevation of *interior* samples in the lower half of the
    height range — alpine DGMs are usually flat at the water plane over the
    lake body, while island summits are high outliers. Shore-ring samples are
    a fallback (cliffs can bias them upward).
    """
    from shapely.geometry import Point

    poly = _as_polygon(xy)
    zs: list[float] = []
    if poly is not None and poly.area > 1.0:
        minx, miny, maxx, maxy = poly.bounds
        # ~1 sample / 12 m², capped.
        n = int(max(80, min(1200, poly.area / 12.0)))
        rng = np.random.default_rng(abs(hash((round(minx, 1), round(miny, 1))) ) % (2**32))
        tries = 0
        while len(zs) < n and tries < n * 8:
            tries += 1
            x = float(rng.uniform(minx, maxx))
            y = float(rng.uniform(miny, maxy))
            if poly.contains(Point(x, y)):
                zs.append(float(z_at(x, y)))
    if len(zs) >= 20:
        arr = np.asarray(zs, dtype=np.float64)
        # Keep lower half so island peaks never enter the mode vote.
        lo = arr[arr <= float(np.median(arr))]
        if lo.size == 0:
            lo = arr
        bins = np.round(lo * 4.0) / 4.0  # 0.25 m bins
        vals, counts = np.unique(bins, return_counts=True)
        return float(vals[int(np.argmax(counts))])

    # Fallback: densified exterior ring, low percentile.
    if xy.shape[0] < 3:
        return float(z_at(float(xy[:, 0].mean()), float(xy[:, 1].mean())))
    ring = xy
    if len(ring) > 3 and np.hypot(*(ring[0] - ring[-1])) < 0.05:
        ring = ring[:-1]
    closed = np.vstack([ring, ring[0]])
    samples = _densify_ring(closed, spacing_m)
    shore = np.asarray(
        [float(z_at(float(p[0]), float(p[1]))) for p in samples], dtype=np.float64
    )
    if shore.size == 0:
        return float(z_at(float(xy[:, 0].mean()), float(xy[:, 1].mean())))
    return float(np.percentile(shore, 20))


def _water_block_box(
    name: str, cx: float, cy: float, sx: float, sy: float, z_surf: float, cfg: dict
) -> dict:
    # Volume depth must reach the carved lake bed when standing_depress_m is set.
    depth = max(float(cfg["depth_m"]), float(cfg["standing_depress_m"]) + 0.5)
    pad = max(0.0, float(cfg.get("standing_xy_pad") or 0.0))
    sx_p = max(2.0, sx * (1.0 + pad))
    sy_p = max(2.0, sy * (1.0 + pad))
    z_pos = float(z_surf) + float(cfg.get("waterlevel_z_offset_m") or 0.0)
    obj: dict = {
        "name": name,
        "class": "WaterBlock",
        "__parent": "Water",
        "persistentId": str(uuid.uuid4()),
        "position": [cx, cy, z_pos],
        "rotationMatrix": [1, 0, 0, 0, 1, 0, 0, 0, 1],
        "scale": [sx_p, sy_p, depth],
        "gridElementSize": float(cfg["grid_element_size"]),
    }
    mat = cfg.get("material") or ""
    if mat:
        obj["material"] = mat
    return obj


def _as_polygon(xy: np.ndarray) -> Polygon | None:
    if xy.shape[0] < 3:
        return None
    try:
        poly = make_valid(Polygon(xy))
    except Exception:
        return None
    if poly.is_empty:
        return None
    if poly.geom_type == "Polygon":
        return poly if not poly.is_empty else None
    if poly.geom_type == "MultiPolygon":
        parts = [g for g in poly.geoms if isinstance(g, Polygon) and not g.is_empty]
        if not parts:
            return None
        return max(parts, key=lambda g: g.area)
    return None


def _core_geoms(xy: np.ndarray, shore_inset_m: float) -> list[Polygon]:
    """Erode standing water so Mud rim remains; fall back to full poly if too thin."""
    poly = _as_polygon(xy)
    if poly is None:
        return []
    if shore_inset_m <= 0:
        return [poly]
    eroded = poly.buffer(-float(shore_inset_m))
    if eroded.is_empty:
        return [poly]
    if eroded.geom_type == "Polygon":
        return [eroded]
    if eroded.geom_type == "MultiPolygon":
        return [g for g in eroded.geoms if isinstance(g, Polygon) and not g.is_empty]
    return [poly]


def _pca_width_m(xy: np.ndarray) -> float:
    """Approx floodplain width via PCA (2 * median |lateral|)."""
    if xy.shape[0] < 3:
        return 0.0
    mean = xy.mean(axis=0)
    centered = xy - mean
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    perp = vt[1] if vt.shape[0] > 1 else np.array([-vt[0, 1], vt[0, 0]])
    lat = centered @ perp
    return 2.0 * float(np.percentile(np.abs(lat), 50))


def _standing_water_blocks(name: str, xy: np.ndarray, z_at, cfg: dict) -> list[dict]:
    """One AABB WaterBlock per lake core; tile only if larger than standing_max_side_m.

    Prefer few large blocks (FPS). Dense tiling is only a fallback for huge polys.
    Z is taken once from the full shoreline (not core centroid) so islands/hills
    inside the lake do not lift the water plane.
    """
    from shapely.geometry import box as shapely_box

    cores = _core_geoms(xy, float(cfg["shore_inset_m"]))
    if not cores:
        return []
    max_side = float(cfg["standing_max_side_m"])
    tile = max(8.0, float(cfg["standing_tile_m"]))
    lift = float(cfg["surface_lift_m"])
    min_area = float(cfg["min_area_m2"])
    z_surf = _lake_surface_z(z_at, xy) + lift
    out: list[dict] = []
    for ci, core in enumerate(cores):
        if core.area < min_area * 0.25:
            continue
        minx, miny, maxx, maxy = core.bounds
        sx = float(maxx - minx)
        sy = float(maxy - miny)
        # Place XY at AABB center (coverage); Z stays shoreline water level.
        cx = 0.5 * (minx + maxx)
        cy = 0.5 * (miny + maxy)
        if max(sx, sy) <= max_side:
            out.append(_water_block_box(f"{name}_c{ci}", cx, cy, sx, sy, z_surf, cfg))
            continue
        # Sparse fallback for oversized lakes (axis-aligned tiles, no dense overlap).
        nx = max(1, int(math.ceil(sx / tile)))
        ny = max(1, int(math.ceil(sy / tile)))
        tw = sx / nx
        th = sy / ny
        min_overlap = 0.25 * tw * th
        for iy in range(ny):
            for ix in range(nx):
                tcx = minx + (ix + 0.5) * tw
                tcy = miny + (iy + 0.5) * th
                tile_g = shapely_box(
                    tcx - tw * 0.5, tcy - th * 0.5, tcx + tw * 0.5, tcy + th * 0.5
                )
                try:
                    inter = core.intersection(tile_g)
                except Exception:
                    continue
                if inter.is_empty or float(inter.area) < min_overlap:
                    continue
                out.append(
                    _water_block_box(
                        f"{name}_c{ci}_t{ix}_{iy}", tcx, tcy, tw, th, z_surf, cfg
                    )
                )
    return out


def _river(name: str, xy: np.ndarray, z_at, cfg: dict) -> dict | None:
    """Two-node River along PCA long axis of the polygon."""
    mean = xy.mean(axis=0)
    centered = xy - mean
    # SVD principal axis
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    axis = vt[0]
    proj = centered @ axis
    t0, t1 = float(proj.min()), float(proj.max())
    length = t1 - t0
    if length < float(cfg["river_min_length_m"]):
        return None
    # width ≈ 2 * RMS perpendicular extent (clamped)
    perp = vt[1] if vt.shape[0] > 1 else np.array([-axis[1], axis[0]])
    lat = centered @ perp
    width = max(float(cfg["river_min_width_m"]), 2.0 * float(np.percentile(np.abs(lat), 50)))
    width = min(width, float(cfg["river_max_width_m"]))
    p0 = mean + axis * t0
    p1 = mean + axis * t1
    z0 = float(z_at(float(p0[0]), float(p0[1]))) + float(cfg["surface_lift_m"])
    z1 = float(z_at(float(p1[0]), float(p1[1]))) + float(cfg["surface_lift_m"])
    depth = float(cfg["river_depth_m"])
    nodes = [
        [float(p0[0]), float(p0[1]), z0, width, depth, 0.0, 0.0, 1.0],
        [float(p1[0]), float(p1[1]), z1, width, depth, 0.0, 0.0, 1.0],
    ]
    # downhill flow: higher Z first
    if z0 < z1:
        nodes.reverse()
    obj: dict = {
        "name": name,
        "class": "River",
        "__parent": "Water",
        "persistentId": str(uuid.uuid4()),
        "position": nodes[0][:3],
        "nodes": nodes,
        "SegmentLength": float(cfg["segment_length"]),
        "SubdivideLength": float(cfg["subdivide_length"]),
        "FlowMagnitudePhysics": 1.5,
        "LowLODDistance": 80.0,
    }
    mat = cfg.get("material") or ""
    if mat:
        obj["material"] = mat
    return obj


def _load_water_features(proc: Path) -> list[tuple[str, dict]]:
    index_path = proc / "landcover_index.json"
    if not index_path.is_file():
        raise SystemExit(f"Missing {index_path} — run tools/fetch_landcover.py")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    ln = _load_landcover_geojson(index.get("landnutzung"))
    if not ln:
        raise SystemExit("Landnutzung GeoJSON missing")
    out: list[tuple[str, dict]] = []
    for f in ln.get("features") or []:
        kind = _tirol_landnutzung_kind(f.get("properties") or {})
        if kind not in ("water_standing", "water_flowing"):
            continue
        out.append((kind, f))
    return out


def build_entries(site: dict, cfg: dict) -> list[dict]:
    proc = processed_dir(site)
    coords = SiteCoords(site)
    to_crs = Transformer.from_crs("EPSG:4326", coords.crs, always_xy=True)
    z_at, _ = bg.load_terrain_z_slope(site)

    entries: list[dict] = []
    n_block = n_river = n_skip = n_wide = 0
    wide_min = float(cfg["wide_flowing_min_width_m"])
    for i, (kind, feat) in enumerate(_load_water_features(proc)):
        props = feat.get("properties") or {}
        oid = props.get("OBJECTID") or props.get("objectid") or i
        for ri, ring in enumerate(_rings_wgs84(feat.get("geometry") or {})):
            xy = _to_beamng(ring, to_crs, coords)
            area = abs(_poly_area(xy))
            if area < float(cfg["min_area_m2"]):
                continue
            name = f"water_{kind}_{oid}_{ri}"
            as_standing = kind == "water_standing"
            if kind == "water_flowing" and wide_min > 0 and _pca_width_m(xy) >= wide_min:
                as_standing = True
                n_wide += 1

            if as_standing:
                if cfg["standing"] != "waterblock":
                    n_skip += 1
                    continue
                blocks = _standing_water_blocks(name, xy, z_at, cfg)
                if not blocks:
                    n_skip += 1
                    continue
                entries.extend(blocks)
                n_block += len(blocks)
                continue

            if cfg["flowing"] != "river":
                n_skip += 1
                continue
            river = _river(name, xy, z_at, cfg)
            if river is not None:
                entries.append(river)
                n_river += 1
            else:
                n_skip += 1
    print(
        f"Water objects: WaterBlock={n_block} River={n_river} "
        f"skipped={n_skip} wide_flowing→block={n_wide} "
        f"(flowing={cfg['flowing']}, standing={cfg['standing']})"
    )
    return entries


def write_level(level_name: str, entries: list[dict]) -> Path | None:
    user_level = USER_LEVELS / level_name
    if not user_level.is_dir():
        print(f"Level folder missing: {user_level}")
        return None
    group_dir = user_level / "main" / "MissionGroup" / "level_objects" / "Water"
    group_dir.mkdir(parents=True, exist_ok=True)
    items_path = group_dir / "items.level.json"
    with items_path.open("w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")

    lo_items = user_level / "main" / "MissionGroup" / "level_objects" / "items.level.json"
    lines = []
    if lo_items.is_file() and lo_items.stat().st_size:
        lines = [ln for ln in lo_items.read_text(encoding="utf-8").splitlines() if ln.strip()]
    names = set()
    for ln in lines:
        try:
            names.add(json.loads(ln).get("name"))
        except json.JSONDecodeError:
            pass
    if "Water" not in names:
        lines.append(
            json.dumps(
                {
                    "name": "Water",
                    "class": "SimGroup",
                    "__parent": "level_objects",
                    "enabled": "1",
                    "persistentId": str(uuid.uuid4()),
                },
                separators=(",", ":"),
            )
        )
        lo_items.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("Registered SimGroup Water under level_objects")
    return items_path


def _standing_rings_px(
    site: dict, size: int, extent: float, min_area_m2: float
) -> list[list[tuple[float, float]]]:
    """LN-GWS polygon rings in heightmap pixel space (only true standing water)."""
    proc = processed_dir(site)
    coords = SiteCoords(site)
    to_crs = Transformer.from_crs("EPSG:4326", coords.crs, always_xy=True)
    rings_px: list[list[tuple[float, float]]] = []
    for kind, feat in _load_water_features(proc):
        if kind != "water_standing":
            continue
        for ring in _rings_wgs84(feat.get("geometry") or {}):
            xy = _to_beamng(ring, to_crs, coords)
            if abs(_poly_area(xy)) < min_area_m2:
                continue
            pts = []
            for bx, by in xy:
                px = float(bx) / extent * (size - 1)
                py = (1.0 - float(by) / extent) * (size - 1)
                pts.append((px, py))
            if len(pts) >= 3:
                rings_px.append(pts)
    return rings_px


def _load_elev_m(site: dict, level_name: str) -> tuple[np.ndarray, float, int, float, Path]:
    """Load meters elev from processed bakes (never import — keeps carve idempotent)."""
    from PIL import Image

    proc = processed_dir(site)
    bng = site.get("beamng") or {}
    size = int(bng.get("mask_size") or 512)
    mpp = float(bng.get("meters_per_pixel") or 1.0)
    extent = float(size) * mpp
    meta = json.loads((proc / "heightmap_meta.json").read_text(encoding="utf-8"))
    max_h = float(meta["max_height_m"])
    # Prefer latest structure bake; skip *_water.png so re-runs don't stack -5m.
    candidates = [
        proc / f"heightmap_{size}_gallery_embed.png",
        proc / f"heightmap_{size}_gallery_approach.png",
        proc / f"heightmap_{size}_gallery.png",
        proc / f"heightmap_{size}_bridge_conform.png",
        proc / f"heightmap_{size}.png",
    ]
    hm_path = next((p for p in candidates if p.is_file()), None)
    if hm_path is None:
        raise SystemExit(f"Missing heightmap — run build_smoke first ({candidates[-1]})")
    hm = np.asarray(Image.open(hm_path), dtype=np.float64)
    elev = hm / 65535.0 * max_h
    return elev, max_h, size, extent, hm_path


def depress_standing_lakes(
    site: dict,
    level_name: str,
    cfg: dict,
) -> Path | None:
    """Lower terrain under LN-GWS lakes so WaterBlocks sit in a basin.

    Soft shore via Gaussian blur of the standing mask. Does not touch flowing
    floodplain mud. Writes heightmap_{N}_water.png and syncs level import/.
    """
    from PIL import Image, ImageDraw, ImageFilter

    depress = float(cfg["standing_depress_m"])
    if depress <= 0:
        return None

    elev, max_h, size, extent, src_path = _load_elev_m(site, level_name)
    rings = _standing_rings_px(site, size, extent, float(cfg["min_area_m2"]))
    if not rings:
        print("standing_depress: no LN-GWS polygons — skip")
        return None

    mask_img = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask_img)
    for pts in rings:
        draw.polygon([(float(x), float(y)) for x, y in pts], outline=255, fill=255)

    blend_m = max(0.0, float(cfg["standing_depress_blend_m"]))
    if blend_m > 0:
        # ~1 px ≈ mpp meters; radius ≈ half blend width.
        radius = max(1.0, 0.5 * blend_m / max(extent / max(size - 1, 1), 1e-6))
        weight_img = mask_img.filter(ImageFilter.GaussianBlur(radius=radius))
    else:
        weight_img = mask_img
    weight = np.asarray(weight_img, dtype=np.float64) / 255.0
    n_core = int(np.count_nonzero(np.asarray(mask_img) > 0))
    if n_core == 0:
        print("standing_depress: empty mask — skip")
        return None

    out = elev.astype(np.float64).copy()
    out -= depress * weight
    out = np.clip(out, 0.0, max_h)

    u16 = np.clip(np.round(out / max(max_h, 1e-6) * 65535.0), 0, 65535).astype(np.uint16)
    proc = processed_dir(site)
    carved_path = proc / f"heightmap_{size}_water.png"
    Image.fromarray(u16, mode="I;16").save(carved_path)

    # Preview: blue = depressed weight
    preview = np.zeros((size, size, 3), dtype=np.uint8)
    preview[..., 0] = np.clip(weight * 40, 0, 255).astype(np.uint8)
    preview[..., 1] = np.clip(weight * 120, 0, 255).astype(np.uint8)
    preview[..., 2] = np.clip(weight * 255, 0, 255).astype(np.uint8)
    Image.fromarray(preview, mode="RGB").save(proc / "preview_water_depress.png")

    preset_path = proc / "terrainPreset.json"
    preset: dict = {}
    if preset_path.is_file():
        try:
            preset = json.loads(preset_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            preset = {}
    preset.setdefault("type", "TerrainData")
    preset.setdefault("name", "theTerrain")
    preset["heightScale"] = float(max_h)
    preset["heightMapPath"] = f"/levels/{level_name}/import/heightmap_{size}.png"
    preset_path.write_text(json.dumps(preset, indent=2), encoding="utf-8")

    user_import = USER_LEVELS / level_name / "import"
    if user_import.parent.is_dir():
        user_import.mkdir(parents=True, exist_ok=True)
        Image.fromarray(u16, mode="I;16").save(user_import / f"heightmap_{size}.png")
        (user_import / "terrainPreset.json").write_text(
            json.dumps(preset, indent=2), encoding="utf-8"
        )
        print(f"Synced water-depress heightmap -> {user_import}")
        print("Re-import terrainPreset.json in World Editor (heightmap changed).")

    print(
        f"Standing lake depress: -{depress:.1f}m on {n_core} px "
        f"(blend={blend_m:.1f}m, src={src_path.name}) -> {carved_path.name}"
    )
    return carved_path


def main() -> None:
    site = load_site()
    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "").strip()
    if not level_name:
        raise SystemExit("beamng.level_name missing")
    cfg = _cfg(bng)
    if not cfg["enabled"]:
        raise SystemExit("water.enabled is false")

    proc = processed_dir(site)
    # Basin first so import HM is ready; WaterBlock Z still samples pristine surface.
    depress_standing_lakes(site, level_name, cfg)

    entries = build_entries(site, cfg)
    out = proc / "water_items.level.json"
    with out.open("w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")
    print(f"Wrote {out} ({len(entries)} objects)")

    injected = write_level(level_name, entries)
    if injected:
        print(f"Injected: {injected}")
        # Verify Fernstein-sized lakes landed at expected Z (editor must not overwrite).
        for e in entries:
            if "401334" in str(e.get("name") or ""):
                p, s = e["position"], e["scale"]
                print(
                    f"Fernsteinsee block {e['name']}: "
                    f"posZ={p[2]:.3f} xy=({p[0]:.1f},{p[1]:.1f}) "
                    f"scaleXY=({s[0]:.1f},{s[1]:.1f}) depth={s[2]:.1f}"
                )
        # Read-back from disk (catches BeamNG editor holding old MissionGroup).
        try:
            disk = [
                json.loads(ln)
                for ln in injected.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]
            for e in disk:
                if "401334" in str(e.get("name") or ""):
                    print(f"Disk verify 401334 posZ={e['position'][2]:.3f}")
        except OSError as ex:
            print(f"Disk verify failed: {ex}")
        print(
            "Reload the level in BeamNG WITHOUT saving the old MissionGroup "
            "(World Editor can overwrite Water items with stale Z)."
        )


if __name__ == "__main__":
    main()
