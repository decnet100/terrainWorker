"""Create a BeamNG user level from content/levels/template.zip.

Interactive (prompt for name) or:
  python tools/setup_beamng_level.py autoroad_l13_test
  python tools/setup_beamng_level.py --site config/sites/l13_kuehtai.yaml

Does:
  - extract template into AppData .../current/levels/<name>/
  - rewrite /levels/template → /levels/<name> in text assets
  - set info.json title (Freeroam list)
  - remove ocean WaterPlanes
  - strip template backdrop / groundcover / floating pac_rock forest
  - set theTerrain writable path + position 0,0,0
  - move spawn near origin
  - optionally copy processed import/ assets for matching site
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

USER_LEVELS = (
    Path.home()
    / "AppData"
    / "Local"
    / "BeamNG"
    / "BeamNG.drive"
    / "current"
    / "levels"
)

TEMPLATE_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive\content\levels\template.zip"),
    Path(r"C:\Program Files\Steam\steamapps\common\BeamNG.drive\content\levels\template.zip"),
    Path(r"D:\SteamLibrary\steamapps\common\BeamNG.drive\content\levels\template.zip"),
]

LEVEL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,63}$")
TEXT_SUFFIXES = {".json", ".cs", ".txt", ".html", ".md", ".csv", ".xml", ".torquelua"}


def find_template_zip(explicit: Path | None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise SystemExit(f"template.zip not found: {p}")
        return p
    for c in TEMPLATE_CANDIDATES:
        if c.is_file():
            return c
    raise SystemExit(
        "template.zip not found. Pass --template PATH "
        "(usually ...\\BeamNG.drive\\content\\levels\\template.zip)"
    )


def validate_level_name(name: str) -> str:
    name = name.strip()
    if not LEVEL_NAME_RE.match(name):
        raise SystemExit(
            f"Invalid level name {name!r}. Use letters/digits/underscore, "
            "start with a letter (e.g. autoroad_l13_test)."
        )
    if name.lower() in {"template", "levels", "common"}:
        raise SystemExit(f"Reserved name: {name}")
    return name


def resolve_site_for_level(level: str, site_arg: str | None) -> dict | None:
    try:
        from site_coords import load_site, resolve_site_path  # noqa: WPS433
    except ImportError:
        return None

    candidates: list[Path] = []
    if site_arg:
        candidates.append(resolve_site_path(site_arg))
    else:
        env = __import__("os").environ.get("AUTOROAD_SITE")
        if env:
            candidates.append(resolve_site_path(env))
        candidates.append(ROOT / "config" / "site.yaml")
        sites_dir = ROOT / "config" / "sites"
        if sites_dir.is_dir():
            candidates.extend(sorted(sites_dir.glob("*.yaml")))

    for p in candidates:
        if not p.is_file():
            continue
        try:
            site = load_site(p)
        except Exception:  # noqa: BLE001
            continue
        bn = ((site.get("beamng") or {}).get("level_name") or "").strip()
        if bn == level:
            print(f"Matched site config: {p}")
            return site
    if site_arg:
        # Explicit site even if level_name differs — use it for import sync
        p = resolve_site_path(site_arg)
        if p.is_file():
            print(f"Using site config (level_name may differ): {p}")
            return load_site(p)
    return None


def extract_template(zip_path: Path, dst: Path) -> None:
    """Extract levels/template/* directly into dst (no nested levels/template)."""
    prefix = "levels/template/"
    print(f"Extracting {zip_path.name} → {dst}")
    dst.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        members = [n for n in zf.namelist() if n.startswith(prefix) and not n.endswith("/")]
        if not members:
            raise SystemExit(f"Zip has no entries under {prefix!r}")
        for name in members:
            rel = name[len(prefix) :]
            if not rel or ".." in Path(rel).parts:
                continue
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, out.open("wb") as dst_f:
                shutil.copyfileobj(src, dst_f)
    print(f"  extracted {len(members)} files")


def rewrite_template_paths(dst: Path, level: str) -> int:
    old = "/levels/template"
    new = f"/levels/{level}"
    old_bare = "levels/template"
    new_bare = f"levels/{level}"
    n_files = 0
    for path in dst.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and ".level.json" not in path.name:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if old not in text and old_bare not in text:
            continue
        # Prefer slash form first so bare replace does not double-hit
        text2 = text.replace(old, new).replace(old_bare, new_bare)
        if text2 != text:
            path.write_text(text2, encoding="utf-8")
            n_files += 1
    print(f"Rewrote path prefix in {n_files} text files ({old} → {new})")
    return n_files


def _read_ndjson(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _write_ndjson(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")


def patch_info_json(dst: Path, level: str, title: str | None, size_m: int | None) -> None:
    info_path = dst / "info.json"
    data = json.loads(info_path.read_text(encoding="utf-8"))
    data["title"] = title or level
    data["description"] = f"autoroad level ({level})"
    data["authors"] = "autoroad"
    data["isAuxiliary"] = False
    data["supportsTraffic"] = False
    data["supportsTimeOfDay"] = True
    if size_m:
        data["size"] = [size_m, size_m]
    preview = (data.get("previews") or ["template_preview.png"])[0]
    data["defaultSpawnPointName"] = data.get("defaultSpawnPointName") or "spawns_default"
    if not data.get("spawnPoints"):
        data["spawnPoints"] = [
            {
                "name": "Default",
                "translationId": "Default",
                "objectname": "spawns_default",
                "preview": preview,
            }
        ]
    info_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Updated info.json title={data['title']!r} isAuxiliary=false")


def clear_oceans(dst: Path) -> None:
    cleared = 0
    for path in dst.rglob("items.level.json"):
        rows = _read_ndjson(path)
        keep = [r for r in rows if not (r.get("class") == "WaterPlane" or r.get("name") == "ocean")]
        if len(keep) != len(rows):
            _write_ndjson(path, keep)
            cleared += len(rows) - len(keep)
            print(f"  removed ocean/WaterPlane from {path.relative_to(dst)}")
    print(f"Removed {cleared} water object(s)")


def disable_template_forest(dst: Path) -> None:
    """Rename template rock/bush Forest dumps so they do not spawn.

    The template scatters ``pac_rock_*`` at the old −512/−512 origin and
    template Z (~100 m). After our heightmap import they float. Fernpass
    already keeps these as ``*.for_bak``; do the same on every setup/forest
    build so World Editor Save is not required to hide them.
    """
    forest_dir = dst / "forest"
    n = 0
    if forest_dir.is_dir():
        for path in list(forest_dir.glob("*.forest4.json")):
            stem = path.name.lower()
            if stem.startswith("autoroad_"):
                continue
            if not (stem.startswith("pac_rock_") or stem.startswith("tro_")):
                continue
            bak = path.with_name(path.name + ".for_bak")
            if bak.exists():
                path.unlink()
            else:
                path.replace(bak)
            n += 1
            print(f"  disabled template forest {path.name}")
    leftover = dst / "template.forest.json"
    if leftover.is_file():
        bak = leftover.with_name("template.forest.json.for_bak")
        if bak.exists():
            leftover.unlink()
        else:
            leftover.replace(bak)
        n += 1
        print("  disabled template.forest.json")
    if n:
        print(f"Disabled {n} template forest file(s)")


def strip_template_props(dst: Path) -> None:
    """Drop backdrop mesh + template groundcover; keep theTerrain + sky."""
    terrain_items = dst / "main" / "MissionGroup" / "level_objects" / "terrain" / "items.level.json"
    if terrain_items.is_file():
        rows = _read_ndjson(terrain_items)
        keep = []
        for r in rows:
            shape = str(r.get("shapeName") or "")
            if "s_template_backdrop" in shape or "s_template_ocean" in shape:
                print(f"  drop TSStatic {shape}")
                continue
            keep.append(r)
        _write_ndjson(terrain_items, keep)

    for rel in (
        Path("main/MissionGroup/level_objects/vegetation/items.level.json"),
        Path("main/MissionGroup/vegetation/items.level.json"),
    ):
        path = dst / rel
        if path.is_file():
            # Keep Forest + ForestWindEmitter; drop template GroundCover
            rows = _read_ndjson(path)
            keep = [r for r in rows if r.get("class") in {"Forest", "ForestWindEmitter"}]
            if not any(r.get("class") == "Forest" for r in keep):
                keep.append(
                    {
                        "name": "theForest",
                        "class": "Forest",
                        "__parent": "vegetation",
                        "persistentId": "564dc79c-697c-4544-838c-ca62b097e065",
                    }
                )
            _write_ndjson(path, keep)
            print(f"  cleared groundcover in {rel} (kept {len(keep)} forest/wind)")

    disable_template_forest(dst)


def patch_terrain_block(dst: Path, level: str, max_height: float | None) -> None:
    path = dst / "main" / "MissionGroup" / "level_objects" / "terrain" / "items.level.json"
    rows = _read_ndjson(path)
    changed = False
    for r in rows:
        if r.get("class") != "TerrainBlock" and r.get("name") != "theTerrain":
            continue
        r["name"] = "theTerrain"
        r["class"] = "TerrainBlock"
        r["terrainFile"] = f"/levels/{level}/theTerrain.ter"
        r["position"] = [0, 0, 0]
        if max_height is not None:
            r["maxHeight"] = float(max_height)
        changed = True
        print(
            f"  theTerrain → terrainFile={r['terrainFile']} "
            f"position=[0,0,0] maxHeight={r.get('maxHeight')}"
        )
    if changed:
        _write_ndjson(path, rows)
    else:
        print("  WARNING: theTerrain TerrainBlock not found in terrain/items.level.json")

    tj = dst / "theTerrain.terrain.json"
    if tj.is_file():
        data = json.loads(tj.read_text(encoding="utf-8"))
        data["datafile"] = f"/levels/{level}/theTerrain.ter"
        if "heightmapImage" in data:
            data["heightmapImage"] = f"/levels/{level}/theTerrain.terrainheightmap.png"
        tj.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def pick_spawn_xyz(site: dict | None) -> tuple[float, float, float]:
    """Prefer mid-point of longest higher-class OSM road; else map center."""
    if not site:
        return 32.0, 32.0, 5.0
    from site_coords import processed_dir  # noqa: WPS433

    roads_path = processed_dir(site) / "roads_beamng.json"
    if not roads_path.is_file():
        return 32.0, 32.0, 5.0
    data = json.loads(roads_path.read_text(encoding="utf-8"))
    best = None
    for _k, road in data.items():
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        length = 0.0
        for a, b in zip(nodes, nodes[1:]):
            length += math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
        hwy = str(road.get("highway") or "")
        rank = {"motorway": 5, "trunk": 4, "primary": 3, "secondary": 2, "tertiary": 1}.get(hwy, 0)
        mid = nodes[len(nodes) // 2]
        score = (rank, length, len(nodes))
        if best is None or score > best[0]:
            best = (score, mid, road.get("name"), hwy)
    if not best:
        return 32.0, 32.0, 5.0
    _sc, mid, name, hwy = best
    x, y, z = float(mid[0]), float(mid[1]), float(mid[2]) + 4.0
    print(f"  spawn from road {name!r} ({hwy}) -> [{x:.1f}, {y:.1f}, {z:.1f}]")
    return x, y, z


def patch_spawn(dst: Path, site: dict | None = None) -> None:
    path = dst / "main" / "MissionGroup" / "PlayerDropPoints" / "items.level.json"
    x, y, z = pick_spawn_xyz(site)
    rows = _read_ndjson(path)
    if not rows:
        rows = [
            {
                "name": "spawns_default",
                "class": "SpawnSphere",
                "__parent": "PlayerDropPoints",
                "position": [x, y, z],
                "dataBlock": "SpawnSphereMarker",
                "enabled": "1",
                "radius": 1,
            }
        ]
    else:
        named = False
        for r in rows:
            if r.get("class") != "SpawnSphere":
                continue
            r["position"] = [x, y, z]
            r["enabled"] = "1"
            if not r.get("name"):
                r["name"] = "spawns_default"
            named = True
            break
        if not named:
            rows[0]["name"] = rows[0].get("name") or "spawns_default"
    _write_ndjson(path, rows)
    print(f"  spawn → [{x:.2f}, {y:.2f}, {z:.2f}]")


def _tod_items_path(dst: Path) -> Path | None:
    for rel in (
        Path("main/MissionGroup/level_objects/sky_and_sun/items.level.json"),
        Path("main/MissionGroup/sky_and_sun/items.level.json"),
    ):
        p = dst / rel
        if p.is_file():
            return p
    return None


def patch_time_of_day(dst: Path, site: dict | None) -> None:
    """Point TimeOfDay at the site WGS84 center (northern-hemisphere sun path)."""
    if not site:
        return
    from site_coords import wgs84_center  # noqa: WPS433

    path = _tod_items_path(dst)
    if path is None:
        print("  TimeOfDay: no sky_and_sun items (skip)")
        return
    lat, lon = wgs84_center(site)
    info_path = dst / "info.json"
    year, month, day = 2026, 6, 20
    if info_path.is_file():
        info = json.loads(info_path.read_text(encoding="utf-8"))
        date = info.get("defaultDate") or {}
        year = int(date.get("year") or year)
        month = int(date.get("month") or month)
        day = int(date.get("day") or day)
    rows = _read_ndjson(path)
    changed = False
    for row in rows:
        if row.get("class") != "TimeOfDay":
            continue
        row["latitude"] = round(lat, 5)
        row["longitude"] = round(lon, 5)
        row["axisTilt"] = 23.44
        row["utcOffset"] = "2"
        row["dstRule"] = "eu"
        row["azimuthOverride"] = 0
        row["celestialProfile"] = "earth"
        row["year"] = year
        row["month"] = month
        row["day"] = day
        changed = True
    if changed:
        _write_ndjson(path, rows)
        print(f"  TimeOfDay lat={lat:.4f} lon={lon:.4f} utc+2 {year}-{month:02d}-{day:02d}")
    else:
        print("  TimeOfDay: object missing (skip)")


def sync_import_assets(dst: Path, site: dict | None) -> None:
    import_dir = dst / "import"
    import_dir.mkdir(parents=True, exist_ok=True)
    if not site:
        print("No matching site — left empty import/ (copy assets later or run build_smoke)")
        return

    from site_coords import processed_dir  # noqa: WPS433

    proc = processed_dir(site)
    copied = 0
    for pattern in (
        "heightmap_*.png",
        "layerMap_*.png",
        "theTerrain_layerMap_*.png",
        "terrainPreset.json",
        "preview_*.png",
    ):
        for src in proc.glob(pattern):
            shutil.copy2(src, import_dir / src.name)
            copied += 1
    # Prefer short layerMap names for VFS preset if only theTerrain_* exist
    for src in list(import_dir.glob("theTerrain_layerMap_*.png")):
        short = src.name.replace("theTerrain_", "", 1)
        if not (import_dir / short).exists():
            shutil.copy2(src, import_dir / short)
            copied += 1

    # Fix terrainPreset.json VFS paths to this level
    preset_path = import_dir / "terrainPreset.json"
    level = dst.name
    if preset_path.is_file():
        try:
            preset = json.loads(preset_path.read_text(encoding="utf-8"))
            hm = preset.get("heightMapPath") or ""
            if "/levels/" in hm:
                # replace any level segment
                parts = hm.split("/")
                if len(parts) >= 3 and parts[1] == "levels":
                    parts[2] = level
                    preset["heightMapPath"] = "/".join(parts)
            for key in ("textureMaps", "layerMaps", "maps", "opacityMaps"):
                maps = preset.get(key)
                if isinstance(maps, list):
                    for i, m in enumerate(maps):
                        if isinstance(m, str) and "/levels/" in m:
                            parts = m.split("/")
                            if len(parts) >= 3 and parts[1] == "levels":
                                parts[2] = level
                                maps[i] = "/".join(parts)
                        elif isinstance(m, dict) and "path" in m:
                            p = m["path"]
                            if isinstance(p, str) and "/levels/" in p:
                                parts = p.split("/")
                                if len(parts) >= 3 and parts[1] == "levels":
                                    parts[2] = level
                                    m["path"] = "/".join(parts)
            preset_path.write_text(json.dumps(preset, indent=2) + "\n", encoding="utf-8")
        except Exception as ex:  # noqa: BLE001
            print(f"  WARNING: could not patch terrainPreset.json: {ex}")

    print(f"Synced {copied} file(s) from {proc} → {import_dir}")


def meta_max_height(site: dict | None) -> float | None:
    if not site:
        return None
    from site_coords import processed_dir  # noqa: WPS433

    meta = processed_dir(site) / "heightmap_meta.json"
    if not meta.is_file():
        return None
    data = json.loads(meta.read_text(encoding="utf-8"))
    mh = data.get("max_height_m")
    return float(mh) if mh is not None else None


def meta_size(site: dict | None) -> int | None:
    if not site:
        return None
    bng = site.get("beamng") or {}
    if "mask_size" in bng:
        return int(bng["mask_size"])
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("name", nargs="?", help="Level folder name under .../levels/")
    ap.add_argument("--title", help="Freeroam display title (default: level name)")
    ap.add_argument("--site", help="Site YAML for import sync / max height")
    ap.add_argument("--template", type=Path, help="Path to template.zip")
    ap.add_argument("--force", action="store_true", help="Delete existing level folder first")
    ap.add_argument("--keep-ocean", action="store_true", help="Do not remove WaterPlane ocean")
    ap.add_argument("--keep-props", action="store_true", help="Keep template backdrop/vegetation")
    ap.add_argument("--no-sync", action="store_true", help="Do not copy processed import assets")
    args = ap.parse_args()

    name = args.name
    if not name:
        try:
            name = input("New BeamNG level name (e.g. autoroad_l13_test): ").strip()
        except EOFError:
            raise SystemExit("No level name given") from None
    level = validate_level_name(name)

    zip_path = find_template_zip(args.template)
    print(f"template.zip: {zip_path} ({zip_path.stat().st_size // (1024 * 1024)} MB)")

    dst = USER_LEVELS / level
    print(f"Target: {dst}")
    if dst.exists():
        if not args.force:
            raise SystemExit(
                f"Level folder already exists: {dst}\n"
                "Re-run with --force to replace, or pick another name."
            )
        print("--force: removing existing folder…")
        shutil.rmtree(dst)

    USER_LEVELS.mkdir(parents=True, exist_ok=True)

    # Extract via temp then move (atomic-ish, clearer on failure)
    with tempfile.TemporaryDirectory(prefix="autoroad_level_") as tmp:
        tmp_dst = Path(tmp) / level
        extract_template(zip_path, tmp_dst)
        shutil.move(str(tmp_dst), str(dst))

    site = resolve_site_for_level(level, args.site)
    max_h = meta_max_height(site)
    size_m = meta_size(site)

    rewrite_template_paths(dst, level)
    patch_info_json(dst, level, args.title, size_m)

    print("Cleanup:")
    if not args.keep_ocean:
        clear_oceans(dst)
    else:
        print("  kept ocean (--keep-ocean)")
    if not args.keep_props:
        strip_template_props(dst)
    else:
        print("  kept props (--keep-props)")

    print("Terrain / spawn / sky:")
    patch_terrain_block(dst, level, max_h)
    patch_spawn(dst, site)
    patch_time_of_day(dst, site)

    # Drop bulky unused template_source.zip if present
    junk = dst / "template_source.zip"
    if junk.is_file():
        junk.unlink()
        print("Removed template_source.zip")

    if not args.no_sync:
        sync_import_assets(dst, site)
    else:
        (dst / "import").mkdir(parents=True, exist_ok=True)

    # Sanity checks
    ter = dst / "theTerrain.ter"
    info = dst / "info.json"
    ok = ter.is_file() and info.is_file()
    print()
    print("=" * 60)
    if ok:
        print(f"READY: {dst}")
        print()
        print("Next in BeamNG:")
        print(f"  1. Freeroam → load level '{level}'")
        print("  2. F11 → Terrain Tools → Import Terrain")
        print(f"  3. Load .../levels/{level}/import/terrainPreset.json  (or heightmap PNG)")
        print("  4. Confirm theTerrain.terrainFile = "
              f"/levels/{level}/theTerrain.ter")
        print("  5. Position 0,0,0 → Import → Save Level")
        if max_h is not None:
            print(f"     (max_height_m from site ≈ {max_h:.2f})")
        print()
        print("Then re-run: python tools/build_smoke.py   # syncs masks + guardrails")
    else:
        print(f"FAILED sanity check (missing theTerrain.ter or info.json): {dst}")
        sys.exit(1)
    print("=" * 60)


if __name__ == "__main__":
    main()
