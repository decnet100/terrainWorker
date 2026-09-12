"""Inject BeamNG WaterBlock / River from Tirol landcover Gewässer polygons.

Standing water (LN-GWS) → WaterBlock (AABB).
Flowing water (LN-GWF) → River along polygon long axis (approx centerline).

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
        "min_area_m2": float(raw.get("min_area_m2") or 40.0),
        "standing_max_side_m": float(raw.get("standing_max_side_m") or 60.0),
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


def _z_samples(z_at, xy: np.ndarray, n: int = 24) -> float:
    """Median terrain Z over polygon samples (surface height)."""
    xs = xy[:, 0]
    ys = xy[:, 1]
    xmin, xmax = float(xs.min()), float(xs.max())
    ymin, ymax = float(ys.min()), float(ys.max())
    if xmax <= xmin or ymax <= ymin:
        return float(z_at(float(xs.mean()), float(ys.mean())))
    zs = []
    rng = np.random.default_rng(0)
    for _ in range(n):
        bx = float(rng.uniform(xmin, xmax))
        by = float(rng.uniform(ymin, ymax))
        zs.append(float(z_at(bx, by)))
    # also corners + centroid
    zs.append(float(z_at(float(xs.mean()), float(ys.mean()))))
    return float(np.median(zs))


def _water_block(name: str, xy: np.ndarray, z_surf: float, cfg: dict) -> dict:
    xmin, xmax = float(xy[:, 0].min()), float(xy[:, 0].max())
    ymin, ymax = float(xy[:, 1].min()), float(xy[:, 1].max())
    cx, cy = 0.5 * (xmin + xmax), 0.5 * (ymin + ymax)
    sx = max(2.0, xmax - xmin)
    sy = max(2.0, ymax - ymin)
    obj: dict = {
        "name": name,
        "class": "WaterBlock",
        "__parent": "Water",
        "persistentId": str(uuid.uuid4()),
        "position": [cx, cy, z_surf],
        "rotationMatrix": [1, 0, 0, 0, 1, 0, 0, 0, 1],
        "scale": [sx, sy, float(cfg["depth_m"])],
        "gridElementSize": float(cfg["grid_element_size"]),
    }
    mat = cfg.get("material") or ""
    if mat:
        obj["material"] = mat
    return obj


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
    n_block = n_river = n_skip = 0
    for i, (kind, feat) in enumerate(_load_water_features(proc)):
        props = feat.get("properties") or {}
        oid = props.get("OBJECTID") or props.get("objectid") or i
        for ri, ring in enumerate(_rings_wgs84(feat.get("geometry") or {})):
            xy = _to_beamng(ring, to_crs, coords)
            area = abs(_poly_area(xy))
            if area < float(cfg["min_area_m2"]):
                continue
            z_surf = _z_samples(z_at, xy) + float(cfg["surface_lift_m"])
            name = f"water_{kind}_{oid}_{ri}"
            if kind == "water_standing":
                if cfg["standing"] != "waterblock":
                    n_skip += 1
                    continue
                sx = float(xy[:, 0].max() - xy[:, 0].min())
                sy = float(xy[:, 1].max() - xy[:, 1].min())
                if max(sx, sy) > float(cfg["standing_max_side_m"]):
                    print(f"Skip large standing pond {name}: {sx:.0f}x{sy:.0f}m")
                    n_skip += 1
                    continue
                entries.append(_water_block(name, xy, z_surf, cfg))
                n_block += 1
            else:
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
        f"skipped={n_skip} (flowing={cfg['flowing']}, standing={cfg['standing']})"
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
    entries = build_entries(site, cfg)
    out = proc / "water_items.level.json"
    with out.open("w", encoding="utf-8", newline="\n") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")
    print(f"Wrote {out} ({len(entries)} objects)")

    injected = write_level(level_name, entries)
    if injected:
        print(f"Injected: {injected}")
        print("Reload the level in BeamNG to see water.")


if __name__ == "__main__":
    main()
