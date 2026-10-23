"""Place time-trial missions for the hillclimbs of the active level.

Reads ``data/annotations/hillclimbs.gpkg`` (LineStrings in EPSG:25832).
Keeps rows whose ``level`` equals ``beamng.level_name``. XY goes through the
BEV grid into the site CRS, each vertex snaps onto the built centerline, and
Z is the level's 16-bit terrain: ``uint16 / 65535 * TerrainBlock.maxHeight``,
read from ``theTerrain.ter`` (row 0 is south). The result is a BeamNG
time trial: ``info.json`` and ``race.race.json``.

Writes under ``data/processed/<site>/hillclimbs/`` and, unless ``--no-inject``,
into the user folder ``gameplay/missions/<level>/timeTrial/<id>_v<version>/``.
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.geometry import Point
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from authorities import make_grid_transformer  # noqa: E402
from setup_beamng_level import USER_LEVELS  # noqa: E402
from site_coords import SiteCoords, load_site, processed_dir  # noqa: E402

GPKG = ROOT / "data" / "annotations" / "hillclimbs.gpkg"
LAYER = "hillclimbs"
SOURCE_CRS = "EPSG:25832"
SNAP_MAX_M = 25.0
SNAP_MERGE_M = 2.0
TRIGGER_RADIUS_M = 10.0
RADIUS_MIN_M = 4.0
U16_MAX = 65535.0


def quat_from_dir(dx: float, dy: float, dz: float) -> list[float]:
    """BeamNG ``quatFromDir`` (``lua/common/mathlib.lua``), xyzw."""
    d = np.array([dx, dy, dz], dtype=np.float64)
    n = float(np.linalg.norm(d))
    if n < 1e-8:
        raise ValueError("direction has no length")
    d = d / n
    up = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(d, up))) > 0.9999:
        axis = np.array([1.0, 0.0, 0.0] if abs(d[0]) < 0.9 else [0.0, 1.0, 0.0])
        perp = np.cross(d, axis)
        perp = perp / np.linalg.norm(perp)
        d = d + perp * 1e-5
        d = d / np.linalg.norm(d)
    side = np.cross(d, up)
    side = side / np.linalg.norm(side)
    new_up = np.cross(side, d)
    new_up = new_up / np.linalg.norm(new_up)
    m = np.stack([side, d, new_up], axis=0)
    trace = float(m[0, 0] + m[1, 1] + m[2, 2])
    if trace > 0.0:
        s = math.sqrt(trace + 1.0)
        w = s * 0.5
        s = 0.5 / s
        x = (m[2, 1] - m[1, 2]) * s
        y = (m[0, 2] - m[2, 0]) * s
        z = (m[1, 0] - m[0, 1]) * s
    else:
        i = 0
        if m[1, 1] > m[0, 0]:
            i = 1
        if m[2, 2] > m[i, i]:
            i = 2
        j = (i + 1) % 3
        k = (j + 1) % 3
        q = [0.0, 0.0, 0.0]
        s = math.sqrt((m[i, i] - m[j, j]) - (m[k, k] - 1.0))
        q[i] = s * 0.5
        s = 0.5 / s
        q[j] = (m[j, i] + m[i, j]) * s
        q[k] = (m[k, i] + m[i, k]) * s
        w = (m[k, j] - m[j, k]) * s
        x, y, z = q
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    return [x / norm, y / norm, z / norm, w / norm]


def _check_quat() -> None:
    ident = quat_from_dir(0.0, 1.0, 0.0)
    if abs(ident[3] - 1.0) > 1e-6 or max(abs(v) for v in ident[:3]) > 1e-6:
        raise RuntimeError(f"quatFromDir(+Y) is not identity: {ident}")


class _Centerline:
    def __init__(self, path: Path, crs: str) -> None:
        gdf = gpd.read_file(path, layer="centerline")
        if gdf.crs is None:
            raise SystemExit(f"centerline has no CRS: {path}")
        if str(gdf.crs).upper().replace(" ", "") not in {
            crs.upper().replace(" ", ""),
            f"EPSG:{gdf.crs.to_epsg()}" if gdf.crs.to_epsg() else "",
        }:
            gdf = gdf.to_crs(crs)
        self._geoms = list(gdf.geometry)
        self._width = [
            float(w) if w is not None and math.isfinite(float(w)) else 8.0
            for w in gdf["width_m"]
        ] if "width_m" in gdf.columns else [8.0] * len(self._geoms)
        if not self._geoms:
            raise SystemExit(f"centerline layer is empty: {path}")
        self._tree = STRtree(self._geoms)

    def snap(self, x: float, y: float) -> tuple[float, float, float, float]:
        pt = Point(x, y)
        hit = self._tree.nearest(pt)
        idx = int(hit) if isinstance(hit, (int, np.integer)) else int(self._geoms.index(hit))
        line = self._geoms[idx]
        dist = float(pt.distance(line))
        snapped = line.interpolate(line.project(pt))
        return float(snapped.x), float(snapped.y), dist, self._width[idx]


class _TerrainZ:
    """Local height from the level's 16-bit terrain. Row 0 of the file is south."""

    def __init__(self, level_dir: Path) -> None:
        block_path = (
            level_dir / "main" / "MissionGroup" / "level_objects" / "terrain" / "items.level.json"
        )
        max_h = None
        if block_path.is_file():
            for line in block_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("name") == "theTerrain" or row.get("class") == "TerrainBlock":
                    max_h = float(row["maxHeight"])
                    break
        if max_h is None:
            raise SystemExit(f"TerrainBlock maxHeight missing in {block_path}")
        preset_path = level_dir / "import" / "terrainPreset.json"
        square = 1.0
        if preset_path.is_file():
            square = float(json.loads(preset_path.read_text(encoding="utf-8")).get("squareSize", square))
        ter = level_dir / "theTerrain.ter"
        data = ter.read_bytes()
        version = data[0]
        size = struct.unpack_from("<I", data, 1)[0]
        need = 5 + size * size * 2
        if version != 9 or size < 2 or len(data) < need:
            raise SystemExit(f"unexpected terrain file {ter}: version {version} size {size}")
        self.hm = np.frombuffer(data[5:need], dtype="<u2").reshape(size, size)
        self.max_h = max_h
        self.square = square
        self.size = int(size)

    def at(self, bx: float, by: float) -> float:
        fc = bx / self.square
        fr = by / self.square
        last = self.size - 1
        if fc < 0.0 or fr < 0.0 or fc > last or fr > last:
            raise SystemExit(f"terrain sample outside the level at {bx:.1f}, {by:.1f}")
        c0 = int(math.floor(fc))
        r0 = int(math.floor(fr))
        c1 = min(c0 + 1, last)
        r1 = min(r0 + 1, last)
        tx = fc - c0
        ty = fr - r0

        def metres(r: int, c: int) -> float:
            return float(self.hm[r, c]) / U16_MAX * self.max_h

        return (
            metres(r0, c0) * (1.0 - tx) * (1.0 - ty)
            + metres(r0, c1) * tx * (1.0 - ty)
            + metres(r1, c0) * (1.0 - tx) * ty
            + metres(r1, c1) * tx * ty
        )


def _mission_type_data(length_m: float) -> dict:
    gold = max(20.0, length_m / 12.0)
    silver = gold * 1.25
    bronze = gold * 1.6
    return {
        "allowFlip": True,
        "allowRecover": True,
        "allowRollingStart": False,
        "bronzeTime": int(round(bronze)),
        "bronzeTime2": int(round(bronze)),
        "bronzeTime3": int(round(bronze)),
        "silverTime": int(round(silver)),
        "silverTime2": int(round(silver)),
        "silverTime3": int(round(silver)),
        "goldTime": int(round(gold)),
        "goldTime2": int(round(gold)),
        "goldTime3": int(round(gold)),
        "bronzeTimePenalty": 20,
        "silverTimePenalty": 10,
        "goldTimePenalty": 0,
        "justFinishPenalty": 30,
        "canChangeLapAmount": False,
        "closed": False,
        "defaultLaps": 1,
        "dynamicRewardsExponentForLaps": 1,
        "flipLimit": -1,
        "flipPenalty": 5,
        "recoverLimit": -1,
        "recoverPenalty": 5,
        "reversible": False,
        "mapPreviewMode": "waypoints",
        "startScreenText": "",
        "lapTime1": 60,
        "lapTime2": 55,
        "lapTime3": 50,
        "lapTime4": 45,
        "lapTime5": 40,
    }


def _pathnode(name: str, old_id: int, pos: list[float], normal: list[float], radius: float, rec: int, rev: int) -> dict:
    return {
        "customFields": {"names": {}, "tags": {}, "types": {}, "values": {}},
        "mode": "manual",
        "name": name,
        "navRadiusScale": 1,
        "normal": normal,
        "oldId": old_id,
        "pos": pos,
        "radius": radius,
        "recovery": rec,
        "reverseRecovery": rev,
        "sidePadding": [1, 1],
        "useAsSplit": True,
        "visible": True,
    }


def _start(name: str, old_id: int, group: str, pos: list[float], rot: list[float]) -> dict:
    return {"group": group, "name": name, "oldId": old_id, "pos": pos, "rot": rot}


def _build_race(stage_id: str, display: str, nodes: list[dict]) -> dict:
    """nodes: site-snapped beamng points with pos, normal, radius."""
    n = len(nodes)
    starts: list[dict] = []
    pathnodes: list[dict] = []
    segments: list[dict] = []
    player = nodes[0]
    starts.append(_start("Player Start", 1, "start", player["pos"], player["rot"]))
    next_id = 2
    rec_ids: list[tuple[int, int]] = []
    for i, node in enumerate(nodes):
        fwd = next_id
        rev = next_id + 1
        next_id += 2
        rec_ids.append((fwd, rev))
        starts.append(_start(f"{stage_id}_n{i + 1:02d} recovery", fwd, "recovery", node["pos"], node["rot"]))
        starts.append(_start(f"{stage_id}_n{i + 1:02d} recovery reverse", rev, "recovery", node["pos"], node["rot_reverse"]))
    pn_ids: list[int] = []
    for i, node in enumerate(nodes):
        pn = next_id
        next_id += 1
        pn_ids.append(pn)
        fwd, rev = rec_ids[i]
        pathnodes.append(
            _pathnode(f"{stage_id}_n{i + 1:02d}", pn, node["pos"], node["normal"], node["radius"], fwd, rev)
        )
    for i in range(n - 1):
        seg = next_id
        next_id += 1
        segments.append(
            {
                "capsules": {},
                "from": pn_ids[i],
                "mode": "waypoint",
                "name": f"{stage_id}_s{i + 1:02d}",
                "oldId": seg,
                "to": pn_ids[i + 1],
            }
        )
    return {
        "authors": "autoroad",
        "classification": {
            "allowRollingStart": False,
            "branching": False,
            "closed": False,
            "reversible": False,
        },
        "date": int(time.time()),
        "defaultLaps": 1,
        "defaultStartPosition": 1,
        "description": display,
        "difficulty": 0,
        "endNode": pn_ids[-1],
        "forwardPrefabs": {},
        "hideMission": False,
        "name": display,
        "pacenotes": {},
        "pathnodes": pathnodes,
        "prefabs": {},
        "reversePrefabs": {},
        "reverseStartPosition": -1,
        "rollingReverseStartPosition": -1,
        "rollingStartPosition": -1,
        "segments": segments,
        "simplifyAiPath": False,
        "startNode": pn_ids[0],
        "startPositions": starts,
    }


def _info(level: str, mission_id: str, display: str, length_m: float, pos: list[float], rot: list[float]) -> dict:
    folder = f"/gameplay/missions/{level}/timeTrial/{mission_id}/"
    return {
        "additionalAttributes": {},
        "author": "autoroad",
        "careerSetup": {
            "defaultStarKeys": [
                "bronzeTime",
                "silverTime",
                "goldTime",
            ],
            "showInFreeroam": True,
            "skill": "(none)",
            "starOutroTexts": {},
            "starRewards": {},
            "starsActive": {
                "bronzeTime": True,
                "silverTime": True,
                "goldTime": True,
            },
        },
        "customAdditionalAttributes": {},
        "date": int(time.time()),
        "description": display,
        "devMission": False,
        "grouping": {"id": "", "label": ""},
        "layers": [
            {
                "dir": folder,
                "fixed": True,
                "isMissionFolderDir": True,
                "isMissionTypeDir": False,
            },
            {
                "dir": "/gameplay/missionTypes/timeTrial/",
                "fixed": True,
                "isMissionFolderDir": False,
                "isMissionTypeDir": True,
            },
        ],
        "missionType": "timeTrial",
        "missionTypeData": _mission_type_data(length_m),
        "name": display,
        "retryBehaviour": "infiniteRetries",
        "setupModules": {
            "traffic": {"enabled": False},
            "vehicles": {
                "enabled": True,
                "includePlayerVehicle": True,
                "playerVehicleDiffs": {},
                "prioritizePlayerVehicle": True,
                "vehicles": {},
            },
        },
        "startCondition": {"type": "automatic"},
        "startTrigger": {
            "level": level,
            "pos": pos,
            "radius": TRIGGER_RADIUS_M,
            "rot": rot,
            "type": "coordinates",
        },
        "visibleCondition": {"type": "always"},
    }


def _write_mission(folder: Path, info: dict, race: dict, sidecar: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("info.json", info),
        ("race.race.json", race),
        ("alpine_rt.json", sidecar),
    ):
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        (folder / name).write_text(text, encoding="utf-8")


def _prepare_nodes(
    coords: list[tuple[float, float]],
    *,
    to_site,
    centerline: _Centerline,
    terrain: _TerrainZ,
    sc: SiteCoords,
    stage_id: str,
) -> tuple[list[dict], float, float]:
    raw: list[tuple[float, float, float, float]] = []
    max_dist = 0.0
    for x_utm, y_utm in coords:
        sx, sy = to_site.transform(x_utm, y_utm)
        x, y, dist, width = centerline.snap(float(sx), float(sy))
        max_dist = max(max_dist, dist)
        if dist > SNAP_MAX_M:
            raise SystemExit(
                f"{stage_id}: vertex is {dist:.1f} m off the centerline "
                f"(limit {SNAP_MAX_M:.0f} m) at UTM {x_utm:.1f}, {y_utm:.1f}"
            )
        raw.append((x, y, dist, width))
    snapped = [raw[0]]
    for item in raw[1:-1]:
        if math.hypot(item[0] - snapped[-1][0], item[1] - snapped[-1][1]) >= SNAP_MERGE_M:
            snapped.append(item)
    finish = raw[-1]
    if math.hypot(finish[0] - snapped[-1][0], finish[1] - snapped[-1][1]) < SNAP_MERGE_M:
        snapped[-1] = finish
    else:
        snapped.append(finish)
    if len(snapped) < 2:
        raise SystemExit(f"{stage_id}: fewer than two points after snapping")

    length = 0.0
    xy: list[tuple[float, float, float, float]] = []
    for i, (x, y, dist, width) in enumerate(snapped):
        bx, by = sc.crs_to_beamng(x, y)
        bz = terrain.at(bx, by)
        if bz < 0.0 or bz > terrain.max_h:
            raise SystemExit(
                f"{stage_id}: local Z {bz:.2f} outside 0..{terrain.max_h:.1f} at {bx:.1f}, {by:.1f}"
            )
        radius = max(RADIUS_MIN_M, width * 0.5 + 1.5)
        xy.append((bx, by, bz, radius))
        if i:
            px, py = snapped[i - 1][0], snapped[i - 1][1]
            length += math.hypot(x - px, y - py)

    nodes: list[dict] = []
    for i, (bx, by, bz, radius) in enumerate(xy):
        if i + 1 < len(xy):
            dx = xy[i + 1][0] - bx
            dy = xy[i + 1][1] - by
            dz = xy[i + 1][2] - bz
        else:
            dx = bx - xy[i - 1][0]
            dy = by - xy[i - 1][1]
            dz = bz - xy[i - 1][2]
        normal = [dx, dy, dz]
        norm = math.sqrt(dx * dx + dy * dy + dz * dz)
        normal = [dx / norm, dy / norm, dz / norm]
        rot = quat_from_dir(dx, dy, dz)
        rot_reverse = quat_from_dir(-dx, -dy, -dz)
        nodes.append(
            {
                "pos": [bx, by, bz],
                "normal": normal,
                "rot": rot,
                "rot_reverse": rot_reverse,
                "radius": radius,
            }
        )
    return nodes, length, max_dist


def main() -> None:
    _check_quat()
    site = load_site()
    sc = SiteCoords(site)
    level = str((site.get("beamng") or {}).get("level_name") or "").strip()
    if not level:
        raise SystemExit("beamng.level_name missing")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gpkg", default=str(GPKG), help="Hillclimb GeoPackage")
    ap.add_argument("--no-inject", action="store_true", help="Do not write into the BeamNG user folder")
    args = ap.parse_args()

    gpkg = Path(args.gpkg)
    if not gpkg.is_absolute():
        gpkg = ROOT / gpkg
    if not gpkg.is_file():
        raise SystemExit(f"hillclimb gpkg missing: {gpkg}")

    proc = processed_dir(site)
    center_path = proc / "centerline_shift_taper" / "centerline_shift.gpkg"
    level_dir = USER_LEVELS / level
    if not center_path.is_file():
        raise SystemExit(f"missing {center_path}")
    if not level_dir.is_dir():
        raise SystemExit(f"level folder missing: {level_dir}")
    terrain = _TerrainZ(level_dir)

    gdf = gpd.read_file(gpkg, layer=LAYER)
    if gdf.crs is None or gdf.crs.to_epsg() != 25832:
        raise SystemExit(f"{gpkg} layer {LAYER} must be {SOURCE_CRS}, got {gdf.crs}")
    rows = gdf[gdf["level"].astype(str) == level]
    if rows.empty:
        raise SystemExit(f"no hillclimbs for level {level} in {gpkg}")

    to_site = make_grid_transformer(SOURCE_CRS, sc.crs, None)
    centerline = _Centerline(center_path, sc.crs)
    out_root = proc / "hillclimbs"
    user_root = USER_LEVELS.parent / "gameplay" / "missions" / level / "timeTrial"
    for _, row in rows.iterrows():
            stage_id = str(row["id"]).strip()
            display = str(row["name"]).strip() or stage_id
            version = int(float(row["version"]))
            mission_id = f"{stage_id}_v{version}"
            geom = row.geometry
            if geom is None or geom.geom_type != "LineString" or len(geom.coords) < 2:
                raise SystemExit(f"{stage_id}: need a LineString with at least two vertices")
            coords = [(float(c[0]), float(c[1])) for c in geom.coords]
            nodes, length, max_dist = _prepare_nodes(
                coords,
                to_site=to_site,
                centerline=centerline,
                terrain=terrain,
                sc=sc,
                stage_id=stage_id,
            )
            info = _info(level, mission_id, display, length, nodes[0]["pos"], nodes[0]["rot"])
            race = _build_race(stage_id, display, nodes)
            sidecar = {
                "version": version,
                "reference_length_m": round(length, 2),
                "countdown_s": 10,
            }
            _write_mission(out_root / mission_id, info, race, sidecar)
            dest = out_root / mission_id
            if not args.no_inject:
                dest = user_root / mission_id
                _write_mission(dest, info, race, sidecar)
            print(
                f"{mission_id}: nodes={len(nodes)} length={length:.1f} m "
                f"max_snap={max_dist:.1f} m z={nodes[0]['pos'][2]:.2f}..{nodes[-1]['pos'][2]:.2f} "
                f"-> {dest}"
            )


if __name__ == "__main__":
    main()
