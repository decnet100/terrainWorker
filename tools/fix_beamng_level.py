"""Repair spawn + TerrainBlock for an existing BeamNG user level.

Typical after World-Editor Import/Save:
  - PlayerDropPoints still at template corner [32,32,5]
  - unnamed SpawnSphere (Freeroam needs name + info.json)
  - terrain/items.level.json emptied
  - asphalt gone because heightmap-only re-import dropped Texture Maps

IMPORTANT: Close BeamNG before running — Save Level overwrites these files.

Usage:
  python tools/fix_beamng_level.py
  python tools/fix_beamng_level.py --level autoroad_galerie_test
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import load_site, processed_dir  # noqa: E402

USER_LEVELS = (
    Path.home()
    / "AppData"
    / "Local"
    / "BeamNG"
    / "BeamNG.drive"
    / "current"
    / "levels"
)


def _read_ndjson(path: Path) -> list[dict]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _write_ndjson(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")


def pick_spawn_from_roads(
    roads_path: Path,
    hm_meta: Path | None = None,
    hm_png: Path | None = None,
) -> tuple[float, float, float] | None:
    if not roads_path.is_file():
        return None
    data = json.loads(roads_path.read_text(encoding="utf-8"))

    hm = None
    max_h = None
    size = None
    if hm_png and hm_png.is_file() and hm_meta and hm_meta.is_file():
        from PIL import Image
        import numpy as np

        hm = np.asarray(Image.open(hm_png))
        meta = json.loads(hm_meta.read_text(encoding="utf-8"))
        max_h = float(meta["max_height_m"])
        size = int(hm.shape[0])

    def z_from_hm(x: float, y: float, fallback: float) -> float:
        if hm is None or max_h is None or size is None:
            return fallback
        # Sample 3x3 neighborhood max — avoids ditch/cell undershoot under the car
        vals = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                c = int(max(0, min(size - 1, round(x) + dx)))
                r = int(max(0, min(size - 1, round(size - 1 - y) + dy)))  # row0 = north
                vals.append(float(hm[r, c]) / 65535.0 * max_h)
        return max(vals) if vals else fallback

    best = None
    for _k, road in data.items():
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        length = 0.0
        for a, b in zip(nodes, nodes[1:]):
            length += math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
        hwy = str(road.get("highway") or "")
        rank = {"motorway": 5, "trunk": 4, "primary": 3, "secondary": 2, "tertiary": 1}.get(
            hwy, 0
        )
        mid = nodes[len(nodes) // 2]
        x, y = float(mid[0]), float(mid[1])
        # Stay clearly inside terrain (gallery stubs can sit near/outside edges)
        if not (64.0 <= x <= 960.0 and 64.0 <= y <= 960.0):
            continue
        centrality = -((x - 512.0) ** 2 + (y - 512.0) ** 2)
        score = (rank, centrality, length, len(nodes))
        z = z_from_hm(x, y, float(mid[2]))
        if best is None or score > best[0]:
            best = (score, (x, y, z), road.get("name"), hwy, length)
    if not best:
        return None
    _sc, (x, y, z), name, hwy, length = best
    print(f"Spawn from roads: {name!r} ({hwy}) L~{length:.0f}m -> ({x:.1f}, {y:.1f}, {z:.1f})")
    # Vehicle clearance; imports often sit a bit high vs heightmap sample
    return x, y, z + 8.0


def restore_terrain_block(level_dir: Path, level: str, max_height: float | None) -> None:
    path = level_dir / "main" / "MissionGroup" / "level_objects" / "terrain" / "items.level.json"
    rows = _read_ndjson(path)
    has_tb = any(r.get("class") == "TerrainBlock" or r.get("name") == "theTerrain" for r in rows)
    if not has_tb:
        print(f"Restoring empty/missing TerrainBlock in {path.relative_to(level_dir)}")
        rows = [
            {
                "name": "theTerrain",
                "class": "TerrainBlock",
                "persistentId": "a500d9a8-fb60-4a81-b438-f9f9593e8f7c",
                "__parent": "terrain",
                "position": [0, 0, 0],
                "maxHeight": float(max_height or 425.0),
                "terrainFile": f"/levels/{level}/theTerrain.ter",
            }
        ]
        _write_ndjson(path, rows)
    else:
        for r in rows:
            if r.get("class") == "TerrainBlock" or r.get("name") == "theTerrain":
                r["terrainFile"] = f"/levels/{level}/theTerrain.ter"
                r["position"] = [0, 0, 0]
                if max_height is not None:
                    r["maxHeight"] = float(max_height)
        _write_ndjson(path, rows)
        print(f"Updated TerrainBlock terrainFile=/levels/{level}/theTerrain.ter position=[0,0,0]")


def patch_spawn(level_dir: Path, xyz: tuple[float, float, float]) -> None:
    path = level_dir / "main" / "MissionGroup" / "PlayerDropPoints" / "items.level.json"
    x, y, z = xyz
    # Named spawn required for info.json / freeroam default
    rows = [
        {
            "name": "spawns_default",
            "class": "SpawnSphere",
            "persistentId": "acc7e7f4-77c7-4563-b3b9-850150c67f87",
            "__parent": "PlayerDropPoints",
            "position": [x, y, z],
            "rotationMatrix": [1, 0, 0, 0, 1, 0, 0, 0, 1],
            "dataBlock": "SpawnSphereMarker",
            "enabled": "1",
            "radius": 1,
        }
    ]
    _write_ndjson(path, rows)
    print(f"SpawnSphere name=spawns_default -> [{x:.2f}, {y:.2f}, {z:.2f}]")


def patch_info_spawn(level_dir: Path) -> None:
    info_path = level_dir / "info.json"
    data = json.loads(info_path.read_text(encoding="utf-8"))
    data["defaultSpawnPointName"] = "spawns_default"
    preview = (data.get("previews") or ["template_preview.png"])[0]
    data["spawnPoints"] = [
        {
            "translationId": "Default",
            "description": "L13 road spawn",
            "objectname": "spawns_default",
            "preview": preview,
        }
    ]
    info_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print("info.json -> defaultSpawnPointName=spawns_default + spawnPoints[]")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--level", help="Level folder name (default: site beamng.level_name)")
    ap.add_argument("--x", type=float, help="Override spawn X")
    ap.add_argument("--y", type=float, help="Override spawn Y")
    ap.add_argument("--z", type=float, help="Override spawn Z")
    ap.add_argument(
        "--restore-terrain",
        action="store_true",
        help="Rewrite terrain/items.level.json TerrainBlock (can fight World Editor; off by default)",
    )
    args = ap.parse_args()

    site = load_site()
    level = args.level or str((site.get("beamng") or {}).get("level_name") or "").strip()
    if not level:
        raise SystemExit("No --level and site has no beamng.level_name")

    level_dir = USER_LEVELS / level
    if not level_dir.is_dir():
        raise SystemExit(f"Level folder missing: {level_dir}")

    proc = processed_dir(site)
    max_h = None
    hm_meta = proc / "heightmap_meta.json"
    if hm_meta.is_file():
        max_h = float(json.loads(hm_meta.read_text(encoding="utf-8")).get("max_height_m") or 0) or None

    print(f"Level: {level_dir}")
    spawn_before = level_dir / "main" / "MissionGroup" / "PlayerDropPoints" / "items.level.json"
    if spawn_before.is_file():
        raw = spawn_before.read_text(encoding="utf-8").replace(" ", "")
        if '"position":[32,32,5]' in raw or "position\":[32.0,32.0,5" in raw:
            print("NOTE: spawn was still at template [32,32,5].")
            print("      Close BeamNG completely BEFORE this fix (Save Level overwrites edits).")

    if args.restore_terrain:
        restore_terrain_block(level_dir, level, max_h)
    else:
        print("Skipping terrain restore (default). Use --restore-terrain only if TerrainBlock JSON is empty.")

    if args.x is not None and args.y is not None and args.z is not None:

        xyz = (args.x, args.y, args.z)
    else:
        hm_size = int((site.get("beamng") or {}).get("mask_size") or 1024)
        xyz = pick_spawn_from_roads(
            proc / "roads_beamng.json",
            proc / "heightmap_meta.json",
            proc / f"heightmap_{hm_size}.png",
        )
        if xyz is None:
            xyz = (512.0, 512.0, 50.0)
            print(f"No suitable road spawn - fallback {xyz}")

    patch_spawn(level_dir, xyz)
    patch_info_spawn(level_dir)

    tj = level_dir / "theTerrain.terrain.json"
    if tj.is_file() and tj.stat().st_size:
        mats = json.loads(tj.read_text(encoding="utf-8")).get("materials") or []
        print(f"theTerrain.terrain.json materials: {mats}")
        if "Asphalt" not in mats:
            print()
            print("ASPHALT MISSING - Import terrainPreset.json (not heightmap alone).")
            print(f"  /levels/{level}/import/terrainPreset.json")

    print()
    print("OK. Close BeamNG if open, then Freeroam -> load level (full reload).")
    print(f"Expected spawn ~ ({xyz[0]:.0f}, {xyz[1]:.0f}, {xyz[2]:.0f}) on L13.")


if __name__ == "__main__":
    main()
