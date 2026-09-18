"""Cut GIP specimens from a source site into a 512 m test-arena level.

v1: one OBJECTID plus 1-hop GIP neighbors, 512x512 real DGM window (1 m/px).
Later specimens pack as AABB tiles; the arena size stays the canvas.

Usage:
  $env:AUTOROAD_SITE='config/sites/fernpass_mega.yaml'
  python tools/build_testarena.py --oid 2304
  python tools/build_testarena.py --oid 2304 --setup --build
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from pyproj import Transformer
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from site_coords import (  # noqa: E402
    SiteCoords,
    load_site,
    processed_dir,
    resolve_site_path,
    site_slug,
)
import heightmap_layers as hml  # noqa: E402

ARENA_NAME = "tirol-testarena"
ARENA_LEVEL = "autoroad_testarena"
ARENA_YAML = ROOT / "config" / "sites" / "testarena.yaml"


def _ends(nodes: list) -> tuple[tuple[float, float], tuple[float, float]]:
    a, b = nodes[0], nodes[-1]
    return (float(a[0]), float(a[1])), (float(b[0]), float(b[1]))


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def neighbor_oids(roads: dict, oid: int, *, tol_m: float) -> list[int]:
    core = roads.get(str(oid))
    if not core or len(core.get("nodes") or []) < 2:
        raise SystemExit(f"OBJECTID {oid} not in source gip_roads_beamng.json")
    e0, e1 = _ends(core["nodes"])
    out: list[int] = []
    for key, road in roads.items():
        other = int(road.get("objectid") or key)
        if other == oid:
            continue
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        a, b = _ends(nodes)
        if min(_dist(a, e0), _dist(a, e1), _dist(b, e0), _dist(b, e1)) <= tol_m:
            out.append(other)
    return sorted(out)


def aabb_xy(nodes: list) -> tuple[float, float, float, float]:
    xs = [float(n[0]) for n in nodes]
    ys = [float(n[1]) for n in nodes]
    return min(xs), min(ys), max(xs), max(ys)


def crop_elev(
    src: np.ndarray,
    *,
    src_extent: float,
    bx0: float,
    by0: float,
    dst_size: int,
    dst_extent: float,
) -> np.ndarray:
    """Bilinear sample source elev (meters) onto dest grid in dest BeamNG XY."""
    src_n = int(src.shape[0])
    c = np.arange(dst_size, dtype=np.float64)
    r = np.arange(dst_size, dtype=np.float64)
    px, py = np.meshgrid(c, r)
    bx = px / max(dst_size - 1, 1) * dst_extent
    by = (1.0 - py / max(dst_size - 1, 1)) * dst_extent
    sx = bx0 + bx
    sy = by0 + by
    spx = sx / src_extent * (src_n - 1)
    spy = (1.0 - sy / src_extent) * (src_n - 1)
    spx = np.clip(spx, 0.0, src_n - 1.001)
    spy = np.clip(spy, 0.0, src_n - 1.001)
    c0 = np.floor(spx).astype(np.int32)
    r0 = np.floor(spy).astype(np.int32)
    c1 = np.clip(c0 + 1, 0, src_n - 1)
    r1 = np.clip(r0 + 1, 0, src_n - 1)
    tx = spx - c0
    ty = spy - r0
    v00 = src[r0, c0]
    v01 = src[r0, c1]
    v10 = src[r1, c0]
    v11 = src[r1, c1]
    top = v00 * (1.0 - tx) + v01 * tx
    bot = v10 * (1.0 - tx) + v11 * tx
    return top * (1.0 - ty) + bot * ty


def _z_at_u16(u16: np.ndarray, max_h: float, size: int, extent: float, bx: float, by: float) -> float:
    n = int(u16.shape[0])
    c = int(max(0, min(n - 1, round(bx / extent * (n - 1)))))
    r = int(max(0, min(n - 1, round((1.0 - by / extent) * (n - 1)))))
    return float(u16[r, c]) / 65535.0 * max_h


def write_roads_beamng(
    *,
    roads: dict,
    oids: list[int],
    bx0: float,
    by0: float,
    u16: np.ndarray,
    max_h: float,
    size: int,
    extent: float,
    path: Path,
) -> dict:
    out = {}
    for oid in oids:
        src = roads[str(oid)]
        nodes = []
        for n in src["nodes"]:
            dx = float(n[0]) - bx0
            dy = float(n[1]) - by0
            if dx < -1.0 or dy < -1.0 or dx > extent + 1.0 or dy > extent + 1.0:
                continue
            z = _z_at_u16(u16, max_h, size, extent, dx, dy)
            w = float(n[3]) if len(n) > 3 else 7.5
            nodes.append([round(dx, 3), round(dy, 3), round(z, 3), round(w, 2)])
        if len(nodes) < 2:
            continue
        rec = dict(src)
        rec["nodes"] = nodes
        out[str(oid)] = rec
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out



def encode_heightmap(elev_m: np.ndarray) -> tuple[np.ndarray, float, float, float]:
    zmin = float(np.nanmin(elev_m))
    zmax = float(np.nanmax(elev_m))
    pad = max(5.0, 0.05 * (zmax - zmin))
    z0, z1 = zmin - pad, zmax + pad
    u16 = np.clip(np.round((elev_m - z0) / (z1 - z0) * 65535.0), 0, 65535).astype(
        np.uint16
    )
    return u16, z0, z1, z1 - z0


def _item_oids(item: dict) -> set[int]:
    match = item.get("match") or {}
    raw = match.get("objectid")
    if raw is None:
        return set()
    if isinstance(raw, list):
        return {int(x) for x in raw}
    return {int(raw)}


def write_site_yaml(
    *,
    source: dict,
    source_path: Path,
    bbox: list[float],
    oids: list[int],
    size: int,
) -> Path:
    src_bng = copy.deepcopy(source.get("beamng") or {})
    bridges = copy.deepcopy(src_bng.get("bridges") or {})
    extras = []
    for e in bridges.get("gip_extra") or []:
        if int(e.get("objectid")) in oids:
            extras.append(e)
    # Seed missing core oids as named bridges (gip_extra is how 2304 becomes a bridge).
    have = {int(e.get("objectid")) for e in extras}
    core = oids[0]
    if core not in have:
        extras.append({"objectid": core, "name": f"Brucke {core}"})
    bridges["gip_extra"] = extras
    items = []
    for it in bridges.get("items") or []:
        if _item_oids(it) & set(oids) and it.get("enabled") is not False:
            items.append(it)
    if not any(core in _item_oids(it) for it in items):
        items.append({"match": {"objectid": core}})
    bridges["items"] = items

    decals = copy.deepcopy(src_bng.get("decal_roads") or {})
    decals["road_bed_items"] = [
        x
        for x in (decals.get("road_bed_items") or [])
        if _item_oids(x) & set(oids)
    ]
    # Driveline lab: longer longitudinal smooth, denser Decal nodes.
    decals["road_bed_smooth_m"] = 28.0
    decals["densify_max_step_m"] = 4.0
    decals["snap_follow_heightmap"] = True
    decals["smooth_decal_z_m"] = 28.0
    decals["abutment_blend_m"] = 12.0
    # Shared MeshRoad corners (adjacent strips) snap to one Z.
    bridges.setdefault("defaults", {})
    if isinstance(bridges.get("defaults"), dict):
        bridges["defaults"]["weld_adjacent_m"] = 0.02
        # Raise only outside the portals (2 m lip + 8 m pull), never under the slab.
        bridges["defaults"]["approach_conform"] = True
        bridges["defaults"]["approach_conform_range_m"] = 2.0
        bridges["defaults"]["approach_conform_pull_m"] = 8.0
        bridges["defaults"]["approach_conform_side_cut"] = True
        bridges["defaults"]["approach_conform_side_range_m"] = 2.0
        bridges["defaults"]["approach_conform_side_drop_m"] = 0.5
        bridges["defaults"]["approach_conform_side_corner_m"] = 2.0
        bridges["defaults"]["approach_conform_max_raise_m"] = 2.0
        bridges["defaults"]["approach_conform_deck_raise_m"] = 0.0
    # Deck asphalt is overObjects on MeshRoad — do not raise terrain under the slab.
    decals["road_bed_conform_bridge_decks"] = False
    decals["road_bed_bridge_deck_raise_m"] = 0.0
    rails = copy.deepcopy(src_bng.get("guardrails") or {})
    rails["enabled"] = True

    site = {
        "name": ARENA_NAME,
        "crs": source.get("crs") or "EPSG:31254",
        "bbox": bbox,
        "sources": {
            "dgm": {
                "type": "local",
                "note": "Heightmap cropped by tools/build_testarena.py; do not fetch WCS.",
            },
            "roads": {"type": "gip"},
            "gip": copy.deepcopy((source.get("sources") or {}).get("gip") or {}),
        },
        "beamng": {
            "meters_per_pixel": 1.0,
            "level_name": ARENA_LEVEL,
            "mask_size": int(size),
            "lane_width_m": src_bng.get("lane_width_m", 3.75),
            "default_lanes": src_bng.get("default_lanes", 2),
            "road_width_scale": src_bng.get("road_width_scale", 0.9),
            "shoulder_m": src_bng.get("shoulder_m", 3.0),
            "road_terrain": src_bng.get("road_terrain", "asphalt"),
            "bridges": bridges,
            "galleries": {"defaults": {"approach_conform": False}, "items": []},
            "guardrails": rails,
            "water": {"enabled": False},
            "forest": {"enabled": False},
            "buildings": {"enabled": False},
            "decal_roads": decals,
        },
        "annotations": {
            "gpkg": f"data/annotations/{ARENA_NAME}.gpkg",
        },
        "testarena": {
            "source_site": str(source_path).replace("\\", "/"),
            "oids": oids,
        },
    }
    ARENA_YAML.parent.mkdir(parents=True, exist_ok=True)
    text = (
        f"# Generated by tools/build_testarena.py from {source_path.as_posix()}\n"
        f"# Level: {ARENA_LEVEL}  size: {size}x{size} m @ 1 m/px\n"
        + yaml.safe_dump(site, sort_keys=False, allow_unicode=True)
    )
    ARENA_YAML.write_text(text, encoding="utf-8")
    return ARENA_YAML


def write_gip_geojson(
    *,
    source: dict,
    roads: dict,
    oids: list[int],
    out: Path,
) -> None:
    sc = SiteCoords(source)
    to_ll = Transformer.from_crs(sc.crs, "EPSG:4326", always_xy=True)
    feats = []
    core = oids[0]
    for oid in oids:
        road = roads[str(oid)]
        coords = []
        for n in road["nodes"]:
            x, y = sc.beamng_to_crs(float(n[0]), float(n[1]))
            lon, lat = to_ll.transform(x, y)
            coords.append([round(lon, 8), round(lat, 8)])
        props = {
            "OBJECTID": int(oid),
            "STRNAME": road.get("name"),
            "STR_CODE": road.get("str_code") or "B179",
            "KUNSTBAUTEN": road.get("kunstbauten"),
            "OBJEKT": road.get("objekt"),
            "OBJEKTBEZEICHNUNG": road.get("name"),
        }
        if int(oid) == int(core):
            props["KUNSTBAUTEN"] = props.get("KUNSTBAUTEN") or f"Brucke {oid}"
            props["_autoroad_gip_extra_kind"] = "bridge"
            props["_autoroad_gip_extra"] = True
        feats.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": {"type": "LineString", "coordinates": coords},
            }
        )
    out.write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--oid", type=int, default=2304, help="Primary GIP OBJECTID")
    ap.add_argument(
        "--source",
        default="config/sites/fernpass_mega.yaml",
        help="Donor site YAML",
    )
    ap.add_argument("--size", type=int, default=512)
    ap.add_argument("--pad-m", type=float, default=10.0)
    ap.add_argument("--neighbor-tol-m", type=float, default=2.5)
    ap.add_argument("--setup", action="store_true", help="Create BeamNG level from template")
    ap.add_argument("--build", action="store_true", help="Run bridges + decals for the arena")
    ap.add_argument("--force-level", action="store_true", help="Replace existing testarena level")
    args = ap.parse_args()

    source_path = resolve_site_path(args.source)
    source = load_site(source_path)
    src_proc = processed_dir(source)
    src_sc = SiteCoords(source)
    size = int(args.size)
    extent = float(size)

    gip_path = src_proc / "gip_roads_beamng.json"
    if not gip_path.is_file():
        raise SystemExit(f"Missing {gip_path} - run build_bridges on the source site first")
    roads = json.loads(gip_path.read_text(encoding="utf-8"))
    oid = int(args.oid)
    nbs = neighbor_oids(roads, oid, tol_m=float(args.neighbor_tol_m))
    oids = [oid] + nbs
    print(f"Specimen oid={oid} neighbors={nbs}")

    xmin, ymin, xmax, ymax = aabb_xy(roads[str(oid)]["nodes"])
    pad = float(args.pad_m)
    aabb_w = (xmax - xmin) + 2.0 * pad
    aabb_h = (ymax - ymin) + 2.0 * pad
    if aabb_w > extent or aabb_h > extent:
        raise SystemExit(
            f"AABB+pad {aabb_w:.1f}x{aabb_h:.1f} m does not fit {size} m arena"
        )
    cx = 0.5 * (xmin + xmax)
    cy = 0.5 * (ymin + ymax)
    bx0 = cx - 0.5 * extent
    by0 = cy - 0.5 * extent
    src_ext = float(src_sc.terrain_extent)
    if bx0 < 0 or by0 < 0 or bx0 + extent > src_ext or by0 + extent > src_ext:
        raise SystemExit(
            f"Window ({bx0:.1f},{by0:.1f})+{extent} leaves source terrain "
            f"(0..{src_ext:.0f})"
        )

    src_size = int((source.get("beamng") or {}).get("mask_size") or 8192)
    src_elev, src_max_h = hml.load_dgm(src_proc, src_size)
    # load_dgm returns meters 0..max_h (relative to source z0).
    _ = src_max_h
    crop = crop_elev(
        src_elev,
        src_extent=src_ext,
        bx0=bx0,
        by0=by0,
        dst_size=size,
        dst_extent=extent,
    )
    u16, z0, z1, max_h = encode_heightmap(crop)

    sw = src_sc.beamng_to_crs(bx0, by0)
    ne = src_sc.beamng_to_crs(bx0 + extent, by0 + extent)
    bbox = [
        round(min(sw[0], ne[0]), 3),
        round(min(sw[1], ne[1]), 3),
        round(max(sw[0], ne[0]), 3),
        round(max(sw[1], ne[1]), 3),
    ]
    yaml_path = write_site_yaml(
        source=source,
        source_path=source_path,
        bbox=bbox,
        oids=oids,
        size=size,
    )
    arena = load_site(yaml_path)
    proc = processed_dir(arena)
    Image.fromarray(u16, mode="I;16").save(proc / f"heightmap_{size}.png")
    meta = {
        "crs": arena["crs"],
        "bbox": bbox,
        "size_m": [extent, extent],
        "heightmap": str((proc / f"heightmap_{size}.png").relative_to(ROOT)).replace(
            "\\", "/"
        ),
        "heightmap_size_px": size,
        "meters_per_pixel": 1.0,
        "terrain_extent_m": extent,
        "z_min_m": z0,
        "z_max_m": z1,
        "max_height_m": max_h,
        "source_site": source_path.as_posix(),
        "source_window_beamng": [round(bx0, 3), round(by0, 3), round(extent, 3)],
        "note": "Crop from source DGM; z remapped to crop min/max. Import Max Height=max_height_m.",
    }
    (proc / "heightmap_meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    write_roads_beamng(
        roads=roads,
        oids=oids,
        bx0=bx0,
        by0=by0,
        u16=u16,
        max_h=max_h,
        size=size,
        extent=extent,
        path=proc / "roads_beamng.json",
    )

    raw = ROOT / "data" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    gip_out = raw / f"gip_{ARENA_NAME}.geojson"
    write_gip_geojson(source=source, roads=roads, oids=oids, out=gip_out)

    spec = {
        "arena": ARENA_NAME,
        "level": ARENA_LEVEL,
        "size": size,
        "oids": oids,
        "neighbors": nbs,
        "pad_m": pad,
        "aabb_src_beamng": [xmin, ymin, xmax, ymax],
        "window_src_beamng": [bx0, by0, bx0 + extent, by0 + extent],
        "bbox_crs": bbox,
        "max_height_m": max_h,
    }
    (proc / "specimens.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")

    hml.compose(
        proc,
        size=size,
        max_h=max_h,
        level_name=ARENA_LEVEL,
        sync_import=False,
    )
    preset = {
        "type": "TerrainData",
        "name": "theTerrain",
        "heightScale": float(max_h),
        "heightMapPath": f"/levels/{ARENA_LEVEL}/import/heightmap_{size}.png",
    }
    (proc / "terrainPreset.json").write_text(
        json.dumps(preset, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {yaml_path}")
    print(f"Wrote {proc / f'heightmap_{size}.png'} max_h={max_h:.2f}m")
    print(f"Wrote {gip_out} features={len(oids)}")
    print(
        f"Window source BeamNG XY=({bx0:.1f},{by0:.1f}) "
        f"core at dest ~({cx - bx0:.1f},{cy - by0:.1f})"
    )

    if args.setup:
        from setup_beamng_level import main as setup_main

        argv = [
            "setup_beamng_level.py",
            ARENA_LEVEL,
            "--site",
            str(yaml_path),
            "--title",
            "Autoroad Testarena",
        ]
        if args.force_level:
            argv.append("--force")
        sys.argv = argv
        setup_main()
        hml.compose(
            proc,
            size=size,
            max_h=max_h,
            level_name=ARENA_LEVEL,
            sync_import=True,
        )
        from setup_beamng_level import USER_LEVELS, _read_ndjson, _write_ndjson

        dx, dy = cx - bx0, cy - by0
        z_sp = _z_at_u16(u16, max_h, size, extent, dx, dy) + 4.0
        sp_path = (
            USER_LEVELS
            / ARENA_LEVEL
            / "main"
            / "MissionGroup"
            / "PlayerDropPoints"
            / "items.level.json"
        )
        rows = _read_ndjson(sp_path)
        for row in rows:
            if row.get("class") == "SpawnSphere":
                row["position"] = [round(dx, 3), round(dy, 3), round(z_sp, 3)]
                row["enabled"] = "1"
        _write_ndjson(sp_path, rows)
        print(f"  spawn at oid={oid} -> [{dx:.1f}, {dy:.1f}, {z_sp:.1f}]")

    if args.build:
        import os

        os.environ["AUTOROAD_SITE"] = str(yaml_path)
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
        import build_bridges
        import build_decal_roads

        sys.argv = ["build_bridges.py"]
        build_bridges.main()
        sys.argv = ["build_decal_roads.py"]
        build_decal_roads.main()
        print("Testarena bridges + decals done. Import terrainPreset.json in World Editor.")


if __name__ == "__main__":
    main()
