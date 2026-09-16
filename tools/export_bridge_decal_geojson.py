"""Export MeshRoad 2304 + nearby asphalt Decals as GeoJSON (site CRS + Z)."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from site_coords import SiteCoords, load_site, processed_dir

OID = "2304"
RADIUS_M = 50.0


def _load_ndjson(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            out.append(json.loads(ln))
    return out


def main() -> None:
    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    level = (
        Path.home()
        / "AppData/Local/BeamNG/BeamNG.drive/current/levels"
        / str(site["beamng"]["level_name"])
    )
    mesh = [
        e
        for e in _load_ndjson(
            level / "main/MissionGroup/level_objects/bridges/items.level.json"
        )
        if e.get("class") == "MeshRoad" and OID in str(e.get("name"))
    ]
    asphalt = [
        e
        for e in _load_ndjson(
            level / "main/MissionGroup/level_objects/roads/items.level.json"
        )
        if e.get("class") == "DecalRoad" and float(e.get("drivability") or 0) > 0
    ]
    if not mesh:
        raise SystemExit(f"no MeshRoad with {OID}")

    starts = [s["nodes"][0] for s in mesh]
    sx = sum(n[0] for n in starts) / len(starts)
    sy = sum(n[1] for n in starts) / len(starts)

    def near(nodes: list) -> bool:
        return any(math.hypot(n[0] - sx, n[1] - sy) <= RADIUS_M for n in nodes)

    feats: list[dict] = []

    def line_feat(kind: str, e: dict, nodes: list) -> dict:
        coords = []
        bng = []
        for n in nodes:
            cx, cy = sc.beamng_to_crs(float(n[0]), float(n[1]))
            coords.append([round(cx, 4), round(cy, 4), round(float(n[2]), 4)])
            bng.append(
                [round(float(n[0]), 4), round(float(n[1]), 4), round(float(n[2]), 4)]
            )
        return {
            "type": "Feature",
            "properties": {
                "kind": kind,
                "name": e.get("name"),
                "class": e.get("class"),
                "n": len(nodes),
                "width0": float(nodes[0][3]) if len(nodes[0]) > 3 else None,
                "depth0": float(nodes[0][4]) if len(nodes[0]) > 4 else None,
                "beamng_xyz": bng,
            },
            "geometry": {"type": "LineString", "coordinates": coords},
        }

    def point_feats(kind: str, e: dict, nodes: list) -> list[dict]:
        out = []
        for i, n in enumerate(nodes):
            cx, cy = sc.beamng_to_crs(float(n[0]), float(n[1]))
            out.append(
                {
                    "type": "Feature",
                    "properties": {
                        "kind": kind + "_node",
                        "name": e.get("name"),
                        "i": i,
                        "z": round(float(n[2]), 4),
                        "w": round(float(n[3]), 4) if len(n) > 3 else None,
                        "beamng_x": round(float(n[0]), 4),
                        "beamng_y": round(float(n[1]), 4),
                    },
                    "geometry": {
                        "type": "Point",
                        "coordinates": [round(cx, 4), round(cy, 4), round(float(n[2]), 4)],
                    },
                }
            )
        return out

    for e in mesh:
        feats.append(line_feat("meshroad", e, e["nodes"]))
        feats.extend(point_feats("meshroad", e, e["nodes"]))
    for e in asphalt:
        if not near(e["nodes"]):
            continue
        feats.append(line_feat("decal", e, e["nodes"]))
        feats.extend(point_feats("decal", e, e["nodes"]))

    gj = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": sc.crs}},
        "features": feats,
    }
    out = proc / "diag_bridge_2304.geojson"
    out.write_text(json.dumps(gj, indent=2), encoding="utf-8")
    n_mesh = sum(1 for f in feats if f["properties"]["kind"] == "meshroad")
    n_decal = sum(1 for f in feats if f["properties"]["kind"] == "decal")
    print(f"wrote {out}")
    print(f"  crs={sc.crs}  mesh_lines={n_mesh}  decal_lines={n_decal}  features={len(feats)}")
    print("  LineString + Point; Z in coordinates; BeamNG xyz in properties")


if __name__ == "__main__":
    main()
