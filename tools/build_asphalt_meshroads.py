"""MeshRoad decks for Landesstraßen, Bundesstraßen and Autobahnen.

Every other road stays on the terrain. The deck uses the existing asphalt
Decal nodes, so bridges and galleries stay clipped out. Node Z is the top
surface. Crossfall is zero: one height across the width.

Also restores the unclipped heightmap from backup_pre_clip1800 and disables
the summit cap, so the peaks are the terrain again.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\build_asphalt_meshroads.py
"""
from __future__ import annotations

import json
import math
import re
import shutil
import uuid
from pathlib import Path

import build_bridges as bb
from build_decal_roads import _cfg, stitch_abutting_roads
from diag_road_bed_modes import _gip_polylines
from gip_road_segments import gip_is_tunnel_bore
from site_coords import load_site, processed_dir

USER_LEVELS = bb.USER_LEVELS
DEPTH_M = 0.12
BACKUP_NAME = "backup_pre_clip1800"
_CODE = re.compile(r"^[ABLabl]\d")
_OID = re.compile(r"_(\d+)(?:_r\d+)?(?:_s\d+)?$")


def _is_main_road(road: dict, site: dict) -> bool:
    if gip_is_tunnel_bore(road, site):
        return False
    code = str(road.get("str_code") or "").strip()
    objekt = str(road.get("objekt") or "").upper().strip()
    if _CODE.match(code):
        return True
    return objekt == "S-A"


def _road_ids(site: dict, proc: Path) -> set[str]:
    cfg = _cfg((site.get("beamng") or {}))
    roads = stitch_abutting_roads(_gip_polylines(site, proc), cfg)
    ids: set[str] = set()
    n = 0
    km = 0.0
    for road in roads:
        if not _is_main_road(road, site):
            continue
        n += 1
        pts = road.get("pts") or []
        for a, b in zip(pts, pts[1:]):
            km += math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
        for key in ("id", "objectid"):
            if road.get(key) is not None:
                ids.add(str(road.get(key)))
        for oid in road.get("osm_ids") or []:
            if oid is not None:
                ids.add(str(oid))
    print(f"main roads {n}  {km / 1000:.1f} km  ids={len(ids)}", flush=True)
    return ids


def _decal_oid(name: str) -> str | None:
    m = _OID.search(name)
    return m.group(1) if m else None


def _mesh_nodes(nodes: list) -> list[list[float]]:
    n = len(nodes)
    out = []
    for i, node in enumerate(nodes):
        x, y, z, w = (float(node[0]), float(node[1]), float(node[2]), float(node[3]))
        if i < n - 1:
            x1, y1, z1 = (float(nodes[i + 1][0]), float(nodes[i + 1][1]), float(nodes[i + 1][2]))
            dx, dy, dz = x1 - x, y1 - y, z1 - z
        else:
            x0, y0, z0 = (float(nodes[i - 1][0]), float(nodes[i - 1][1]), float(nodes[i - 1][2]))
            dx, dy, dz = x - x0, y - y0, z - z0
        horiz = math.hypot(dx, dy) or 1.0
        nx, ny, nz = bb.normal_from_pitch_crossfall(dx, dy, dz / horiz, 0.0)
        out.append(
            [
                round(x, 3),
                round(y, 3),
                round(z, 3),
                round(w, 3),
                DEPTH_M,
                round(nx, 4),
                round(ny, 4),
                round(nz, 4),
            ]
        )
    return out


def _restore_heightmap(proc: Path) -> None:
    backup = proc / BACKUP_NAME
    meta_path = proc / "heightmap_meta.json"
    live = json.loads(meta_path.read_text(encoding="utf-8"))
    if "clip_absolute_m" not in live:
        print("heightmap already unclipped", flush=True)
        return
    level_import = (
        USER_LEVELS / str((load_site().get("beamng") or {}).get("level_name")) / "import"
    )
    pairs = [
        (backup / "heightmap_8192.png", proc / "heightmap_8192.png"),
        (backup / "heightmap_8192_composed.png", proc / "heightmap_8192_composed.png"),
        (backup / "heightmap_meta.json", proc / "heightmap_meta.json"),
        (backup / "terrainPreset.json", proc / "terrainPreset.json"),
        (backup / "manifest.json", proc / "heightmap_layers" / "manifest.json"),
        (backup / "road_bed_z.png", proc / "heightmap_layers" / "road_bed_z.png"),
        (backup / "road_bed_w.png", proc / "heightmap_layers" / "road_bed_w.png"),
        (backup / "span_bridge_z.png", proc / "heightmap_layers" / "span_bridge_z.png"),
        (backup / "span_bridge_w.png", proc / "heightmap_layers" / "span_bridge_w.png"),
        (backup / "import_heightmap_8192.png", level_import / "heightmap_8192.png"),
        (backup / "import_terrainPreset.json", level_import / "terrainPreset.json"),
    ]
    for src, dst in pairs:
        if not src.is_file():
            raise SystemExit(f"missing backup {src}")
        shutil.copy2(src, dst)
    print("restored unclipped heightmap from backup_pre_clip1800", flush=True)


def _disable_summit(level_name: str) -> None:
    lo = (
        USER_LEVELS
        / level_name
        / "main"
        / "MissionGroup"
        / "level_objects"
        / "items.level.json"
    )
    if not lo.is_file():
        return
    rows = []
    changed = False
    for ln in lo.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if row.get("name") == "summit" and row.get("class") == "SimGroup":
            if str(row.get("enabled")) != "0":
                row["enabled"] = "0"
                changed = True
        rows.append(row)
    if changed:
        with lo.open("w", encoding="utf-8", newline="\n") as f:
            for row in rows:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
        print("summit group disabled", flush=True)


def main() -> None:
    site = load_site()
    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "").strip()
    if not level_name:
        raise SystemExit("beamng.level_name missing")
    proc = processed_dir(site)
    _restore_heightmap(proc)
    _disable_summit(level_name)

    ids = _road_ids(site, proc)
    roads_path = (
        USER_LEVELS
        / level_name
        / "main"
        / "MissionGroup"
        / "level_objects"
        / "roads"
        / "items.level.json"
    )
    if not roads_path.is_file():
        raise SystemExit(f"missing {roads_path}")
    backup_items = proc / BACKUP_NAME / "roads_items.level.json"
    if not backup_items.is_file():
        shutil.copy2(roads_path, backup_items)
        print(f"backup {backup_items.name}", flush=True)

    rows = []
    for ln in roads_path.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            rows.append(json.loads(ln))

    selected: list[str] = []
    for row in rows:
        if row.get("class") != "DecalRoad":
            continue
        name = str(row.get("name") or "")
        if not name.startswith("decal_"):
            continue
        if "_ondeck" in name or name.endswith("_deck") or "_deck_" in name:
            continue
        if float(row.get("drivability") or 0) <= 0:
            continue
        oid = _decal_oid(name)
        if oid is None or oid not in ids:
            continue
        selected.append(name)

    stems = {name[len("decal_") :] for name in selected}
    n_over = 0
    for row in rows:
        name = str(row.get("name") or "")
        stem = None
        if name.startswith("decal_"):
            stem = name[len("decal_") :]
        elif name.startswith("line_"):
            stem = name[len("line_") :]
        if stem is None:
            continue
        # line_STEM_d3 shares the decal stem STEM
        base = stem
        if name.startswith("line_"):
            base = re.sub(r"_d\d+$", "", stem)
        if base not in stems and stem not in stems:
            continue
        if row.get("overObjects") is not True:
            row["overObjects"] = True
            n_over += 1
    roads_path.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows),
        encoding="utf-8",
    )

    entries = []
    for row in rows:
        name = str(row.get("name") or "")
        if name not in selected:
            continue
        nodes = row.get("nodes") or []
        if len(nodes) < 2:
            continue
        stem = "mesh_" + name[len("decal_") :]
        entries.append(
            {
                "name": stem,
                "class": "MeshRoad",
                "__parent": "asphalt_mesh",
                "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"autoroad:asphalt_mesh:{stem}")),
                "topMaterial": "Asphalt",
                "bottomMaterial": "Asphalt",
                "sideMaterial": "Asphalt",
                "textureLength": 6,
                "breakAngle": 3,
                "widthSubdivisions": 0,
                "nodes": _mesh_nodes(nodes),
            }
        )

    user_level = USER_LEVELS / level_name
    group_dir = user_level / "main" / "MissionGroup" / "level_objects" / "asphalt_mesh"
    group_dir.mkdir(parents=True, exist_ok=True)
    items_path = group_dir / "items.level.json"
    items_path.write_text(
        "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in entries),
        encoding="utf-8",
    )
    lo = user_level / "main" / "MissionGroup" / "level_objects" / "items.level.json"
    lo_rows = []
    if lo.is_file():
        for ln in lo.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                try:
                    lo_rows.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
    if not any(r.get("name") == "asphalt_mesh" for r in lo_rows):
        lo_rows.append(
            {
                "name": "asphalt_mesh",
                "class": "SimGroup",
                "__parent": "level_objects",
                "enabled": "1",
                "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, "autoroad:asphalt_mesh:group")),
            }
        )
        lo.write_text(
            "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in lo_rows),
            encoding="utf-8",
        )
        print("Registered SimGroup asphalt_mesh", flush=True)
    print(
        f"MeshRoads {len(entries)}  decals overObjects flipped {n_over}  depth {DEPTH_M} m",
        flush=True,
    )


if __name__ == "__main__":
    main()
