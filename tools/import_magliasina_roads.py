"""Bring the road meshes Magliasina built (magliasinaNG roads_only.py) into the BeamNG level of a site.

Reads the chunk triangles (`road_<tx>_<ty>.npz`, local Magliasina coordinates) and the terrain carve
(`terrain_override.npz`) from the Magliasina work folder, the local origin and scale from the export's
manifest.json (export_magliasina.py), and the site's heightmap_meta.json.

* Coordinates: local (x, y) -> site CRS (E0 + x K, N0 + y K) -> BeamNG terrain metres (SiteCoords.crs_to_beamng);
  z -> z - z_min_m of the heightmap. Every triangle of a chunk whose centre lies in --crop is taken.
* Meshes: one DAE per chunk and material group (asphalt, gravel, concrete for bridges, stone for the walls),
  split at COLLADA_MAX_VERTS, collision = the visible mesh, into art/shapes/magliasina_roads/, as TSStatics of the
  SimGroup magliasina_roads. --replace-road-grid switches the road_grid group off.
* Terrain: a replace layer `magliasina_carve` (priority 50, after corridor_dgm, before span) holds the carve
  under the meshes and the cap under the bridge decks, on the heightmap lattice inside --crop. Compose the
  heightmap afterwards (compose_heightmap.py) and re-import it.
Without the level folder (or with --no-inject) only data/processed/<site>/magliasina_import/ is written.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/pfunds_spiess.yaml"; python tools\\import_magliasina_roads.py --crop 612550 5198250 614050 5199750
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_corridor_mesh as bcm  # noqa: E402
from heightmap_layers import load_dgm, write_replace_layer  # noqa: E402
from site_coords import SiteCoords, load_site, processed_dir  # noqa: E402

GROUP = "magliasina_roads"
ART = "magliasina_roads"
# Magliasina material -> material in the level
MATERIAL_GROUP = {
    "mp_road_asphalt": "Asphalt", "mp_road_asphalt_fresh": "Asphalt", "mp_hard_asphalt": "Asphalt",
    "mp_road_sett": "Asphalt", "mp_road_cobble": "Asphalt", "mp_sidewalk": "Asphalt", "mp_island": "Asphalt",
    "mp_path_paved": "Asphalt", "mp_road_gravel": "magliasina_gravel", "mp_road_dirt": "magliasina_gravel",
    "mp_path_dirt": "magliasina_gravel", "mp_road_grass": "magliasina_gravel",
    "mp_road_wall": "magliasina_stone",
}
DEFAULT_GROUP = "magliasina_concrete"     # bridge decks, slabs, parapets, piers (and anything not listed: report)
NEW_MATERIALS = {
    "magliasina_gravel": [0.42, 0.39, 0.34, 1.0],
    "magliasina_stone": [0.45, 0.44, 0.42, 1.0],
    "magliasina_concrete": [0.55, 0.55, 0.53, 1.0],
}
UV_M = 6.0


def to_beamng(sc: SiteCoords, e0: float, n0: float, k: float, z_min: float, v: np.ndarray) -> np.ndarray:
    X = e0 + v[:, 0] * k
    Y = n0 + v[:, 1] * k
    bx = (X - sc.xmin) / sc.bw * sc.terrain_span
    by = (Y - sc.ymin) / sc.bh * sc.terrain_span
    return np.column_stack([bx, by, v[:, 2] - z_min])


def write_dae(path: Path, pos: np.ndarray, faces: np.ndarray, stem: str, material: str) -> None:
    """build_corridor_mesh's writer (visible + collision mesh), with the material of this group."""
    uv = pos[:, :2] / UV_M
    old = bcm.MAT
    bcm.MAT = material                     # the writer names its one material from this module constant
    try:
        bcm._write_collada(path, pos, uv, faces, stem)
    finally:
        bcm.MAT = old


def split(pos: np.ndarray, faces: np.ndarray, cap: int):
    """Pieces of at most `cap` vertices (whole triangles, vertices renumbered)."""
    start = 0
    while start < len(faces):
        used, end = {}, start
        while end < len(faces) and len(used) + 3 <= cap:
            for v in faces[end]:
                used.setdefault(int(v), len(used))
            end += 1
        idx = np.fromiter(used.keys(), dtype=np.int64, count=len(used))
        remap = np.full(int(faces[start:end].max()) + 1, -1, np.int64)
        remap[idx] = np.arange(len(idx))
        yield pos[idx], remap[faces[start:end]]
        start = end


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--roads", help="Magliasina WORK/roads_only folder")
    ap.add_argument("--crop", nargs=4, type=float, metavar=("XMIN", "YMIN", "XMAX", "YMAX"), help="site CRS")
    ap.add_argument("--level", help="level folder name (default beamng.level_name)")
    ap.add_argument("--no-inject", action="store_true")
    ap.add_argument("--replace-road-grid", action="store_true")
    args = ap.parse_args()
    site = load_site()
    proc = processed_dir(site)
    sc = SiteCoords(site)
    manifest = json.loads((proc / "magliasina" / "manifest.json").read_text(encoding="utf-8"))
    e0, n0 = (float(v) for v in manifest["origin"])
    k = float(manifest["scale_factor_at_origin"])
    roads = Path(args.roads) if args.roads else proc / "magliasina" / "magliasina_work" / "work" / "roads_only"
    meta = json.loads((proc / "heightmap_meta.json").read_text(encoding="utf-8"))
    z_min, max_h = float(meta["z_min_m"]), float(meta["max_height_m"])
    size = int(meta["heightmap_size_px"])
    crop = args.crop or [sc.xmin, sc.ymin, sc.xmax, sc.ymax]
    out = proc / "magliasina_import"
    art = out / "art"
    art.mkdir(parents=True, exist_ok=True)
    for f in art.glob("*.dae"):
        f.unlink()

    # --- meshes -----------------------------------------------------------------------------------
    entries, counts, unknown = [], {}, set()
    for f in sorted(roads.glob("road_*.npz")):
        d = np.load(f)
        groups: dict[str, tuple[list, list, int]] = {}
        for i, mat in enumerate(d["materials"]):
            v, t = d[f"v{i}"], d[f"t{i}"]
            cx = e0 + v[t].mean(axis=1)[:, 0] * k
            cy = n0 + v[t].mean(axis=1)[:, 1] * k
            keep = (cx >= crop[0]) & (cx <= crop[2]) & (cy >= crop[1]) & (cy <= crop[3])
            if not keep.any():
                continue
            g = MATERIAL_GROUP.get(str(mat), DEFAULT_GROUP)
            if str(mat) not in MATERIAL_GROUP and not str(mat).startswith("mp_bridge"):
                unknown.add(str(mat))
            P, T, n = groups.setdefault(g, ([], [], 0))
            P.append(to_beamng(sc, e0, n0, k, z_min, v))
            T.append(t[keep] + n)
            groups[g] = (P, T, n + len(v))
        for g, (P, T, _) in groups.items():
            pos, faces = np.concatenate(P), np.concatenate(T)
            used = np.unique(faces)                      # drop the vertices of triangles outside the crop
            remap = np.full(len(pos), -1, np.int64)
            remap[used] = np.arange(len(used))
            pos, faces = pos[used], remap[faces]
            for j, (pp, ff) in enumerate(split(pos, faces, bcm.COLLADA_MAX_VERTS)):
                stem = f"mng_{f.stem[5:]}_{g}_{j}".replace("+", "p").replace("-", "m")
                write_dae(art / f"{stem}.dae", pp, ff, stem, g)
                entries.append({
                    "name": stem, "class": "TSStatic", "__parent": GROUP,
                    "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"autoroad:{GROUP}:{stem}")),
                    "position": [0, 0, 0], "rotationMatrix": [1, 0, 0, 0, 1, 0, 0, 0, 1], "scale": [1.0, 1.0, 1.0],
                    "shapeName": f"/levels/{{level}}/art/shapes/{ART}/{stem}.dae",
                    "collisionType": "Collision Mesh", "decalType": "Collision Mesh", "castShadows": True,
                    "useInstanceRenderData": True, "isRenderEnabled": True})
                counts[g] = counts.get(g, 0) + len(ff)
    mats = {}
    for name, rgba in NEW_MATERIALS.items():
        mats[name] = {"name": name, "mapTo": name, "class": "Material",
                      "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"autoroad:{GROUP}:{name}")),
                      "Stages": [{"baseColorFactor": rgba, "roughnessFactor": 0.9}, {}, {}, {}]}
    (art / "main.materials.json").write_text(json.dumps(mats, indent=1), encoding="utf-8")
    print(f"meshes: {len(entries)} DAE, triangles by material {counts}", flush=True)
    if unknown:
        print(f"materials not in MATERIAL_GROUP (drawn as {DEFAULT_GROUP}): {sorted(unknown)}", flush=True)

    # --- terrain carve layer ----------------------------------------------------------------------
    ovz = np.load(roads / "terrain_override.npz")
    ov, capz = ovz["ov"], ovz["cap"]
    ter_x0, ter_y0 = (float(v) for v in manifest.get("ter_origin", [None, None])) if manifest.get("ter_origin") \
        else (None, None)
    if ter_x0 is None:                                       # prepare_external.env's terrain block
        ter_x0 = (sc.xmin - e0) / k
        ter_y0 = (sc.ymin - n0) / k
    sq = 1.0
    dgm, _ = load_dgm(proc, size)
    target = dgm.copy()
    w = np.zeros_like(dgm)
    # heightmap pixel c, row r: x = xmin + c / (size - 1) * bw, y = ymin + (size - 1 - r) / (size - 1) * bh
    c0 = max(int(np.floor((crop[0] - sc.xmin) / sc.bw * (size - 1))), 0)
    c1 = min(int(np.ceil((crop[2] - sc.xmin) / sc.bw * (size - 1))), size - 1)
    r0 = max(int(np.floor((sc.ymax - crop[3]) / sc.bh * (size - 1))), 0)
    r1 = min(int(np.ceil((sc.ymax - crop[1]) / sc.bh * (size - 1))), size - 1)
    cols, rows = np.meshgrid(np.arange(c0, c1 + 1), np.arange(r0, r1 + 1))
    X = sc.xmin + cols / (size - 1) * sc.bw
    Y = sc.ymin + (size - 1 - rows) / (size - 1) * sc.bh   # heightmap row 0 = north
    inside = (X >= crop[0]) & (X <= crop[2]) & (Y >= crop[1]) & (Y <= crop[3])
    lx, ly = (X - e0) / k, (Y - n0) / k
    ci = np.clip(np.rint((lx - ter_x0) / sq).astype(np.int64), 0, ov.shape[1] - 1)
    ri = np.clip(np.rint((ly - ter_y0) / sq).astype(np.int64), 0, ov.shape[0] - 1)   # Magliasina row 0 = south
    carve, cap = ov[ri, ci], capz[ri, ci]
    win = target[r0:r1 + 1, c0:c1 + 1]
    hit_c = inside & np.isfinite(carve)
    win[hit_c] = np.minimum(win[hit_c], carve[hit_c] - z_min)          # the carve may only lower the ground
    hit_k = inside & np.isfinite(cap)
    win[hit_k] = np.minimum(win[hit_k], cap[hit_k] - z_min)
    w[r0:r1 + 1, c0:c1 + 1] = (hit_c | hit_k).astype(np.float64)
    write_replace_layer(proc, "magliasina_carve", target, w, max_h=max_h, size=size)
    lowered = (dgm - target)[w > 0]
    print(f"carve: {int(w.sum())} heightmap px, lowered median {np.median(lowered):.2f} m, "
          f"p95 {np.percentile(lowered, 95):.2f} m", flush=True)

    # --- level ------------------------------------------------------------------------------------
    level = args.level or (site.get("beamng") or {}).get("level_name")
    user_level = bcm.USER_LEVELS / str(level)
    report = {"crop": crop, "dae": len(entries), "triangles": counts, "carve_px": int(w.sum()), "level": str(user_level)}
    if args.no_inject or not user_level.is_dir():
        (out / "items.level.json").write_text(
            "".join(json.dumps(e).replace("{level}", str(level)) + "\n" for e in entries), encoding="utf-8")
        report["injected"] = False
        print(f"level folder {user_level} not used: wrote {out}", flush=True)
    else:
        dst = user_level / "art" / "shapes" / ART
        dst.mkdir(parents=True, exist_ok=True)
        for old in dst.glob("*.dae"):
            old.unlink()
        for src in art.iterdir():
            (dst / src.name).write_bytes(src.read_bytes())
        cache = bcm.USER_LEVELS.parent / "temp" / "levels" / str(level) / "art" / "shapes" / ART
        for stale in cache.glob("*.cdae") if cache.is_dir() else []:
            stale.unlink()
        group_dir = user_level / "main/MissionGroup/level_objects" / GROUP
        group_dir.mkdir(parents=True, exist_ok=True)
        (group_dir / "items.level.json").write_text(
            "".join(json.dumps(e, separators=(",", ":")).replace("{level}", str(level)) + "\n" for e in entries),
            encoding="utf-8")
        lo = user_level / "main/MissionGroup/level_objects/items.level.json"
        rows = [json.loads(ln) for ln in lo.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not any(r_.get("class") == "SimGroup" and r_.get("name") == GROUP for r_ in rows):
            rows.append({"name": GROUP, "class": "SimGroup", "__parent": "level_objects", "enabled": "1",
                         "persistentId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"autoroad:{GROUP}:group"))})
        for r_ in rows:
            if args.replace_road_grid and r_.get("class") == "SimGroup" and r_.get("name") == "road_grid":
                r_["enabled"] = "0"
        lo.write_text("".join(json.dumps(r_, separators=(",", ":")) + "\n" for r_ in rows), encoding="utf-8")
        report["injected"] = True
        print(f"injected {len(entries)} TSStatics into {user_level}", flush=True)
    (out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print("next: python tools\\compose_heightmap.py, then re-import terrainPreset.json in the game")


if __name__ == "__main__":
    main()
