"""Abstand zwischen benachbarten Meshes aller Typen.

Jede DAE in road_grid (part / over / under / span), nur die sichtbare
Fahrbahn (nach oben). Offene Kanten und Vertices werden typsübergreifend
verglichen — auch road gegen under, nicht nur part gegen part.

  versiegelt:  Draufsicht <= 1 cm und |dZ| <= 1 cm
  luecke_xy:   Nachbarkante 1..25 cm weg in der Draufsicht
  stufe_z:     Draufsicht <= 1 cm, |dZ| > 1 cm (kein Over/Under-Stapel)
  stapel:      over gegen under/span, dieselbe Draufsicht, |dZ| > 40 cm
  ohne_nachbar: keine andere Datei innerhalb 25 cm (Umriss oder echte Lücke)

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\check_mesh_type_gaps.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_road_mesh import (  # noqa: E402
    _boundary_edges,
    _kind_of,
    _parse_dae,
    _pct,
    _up_faces,
)
from site_coords import SiteCoords, load_site, processed_dir

XY_SEAL_M = 0.01
Z_SEAL_M = 0.01
NEAR_M = 0.25
STACK_Z_M = 0.40
MM = 3
KINDS = ("road", "overpass", "underpass", "span")


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _is_stack_pair(ka: str, kb: str) -> bool:
    kinds = {ka, kb}
    if "underpass" in kinds and ("overpass" in kinds or "span" in kinds):
        return True
    if kinds == {"overpass", "span"}:
        return True
    return False


def _load_meshes(out_dir: Path) -> list[dict]:
    index_path = out_dir / "road_grid_index.json"
    names: list[str] = []
    if index_path.is_file():
        tiles = json.loads(index_path.read_text(encoding="utf-8")).get("tiles") or []
        names = [str(t["name"]) for t in tiles]
    if not names:
        names = sorted(p.stem for p in out_dir.glob("*.dae"))
    meshes = []
    for name in names:
        path = out_dir / f"{name}.dae"
        if not path.is_file():
            print(f"missing {path.name}", flush=True)
            continue
        pos, faces = _parse_dae(path)
        up = _up_faces(pos, faces)
        if up.size == 0:
            print(f"  skip {name}: no upward faces", flush=True)
            continue
        used = np.unique(up.reshape(-1))
        verts = pos[used]
        edges = _boundary_edges(pos, up)
        kind = _kind_of(name)
        meshes.append(
            {
                "name": name,
                "kind": kind,
                "verts": verts,
                "edges": edges,
            }
        )
        print(
            f"  {name} ({kind}) up_tris={up.shape[0]} "
            f"up_verts={verts.shape[0]} open_edges={edges.shape[0]}",
            flush=True,
        )
    return meshes


def _edge_mids(edges: np.ndarray) -> np.ndarray:
    if edges.size == 0:
        return np.zeros((0, 3), dtype=np.float64)
    mx = 0.5 * (edges[:, 0] + edges[:, 3])
    my = 0.5 * (edges[:, 1] + edges[:, 4])
    mz = 0.5 * (edges[:, 2] + edges[:, 5])
    return np.column_stack((mx, my, mz))


def _empty_bucket() -> dict:
    return {
        "n_query_edges": 0,
        "versiegelt": 0,
        "luecke_xy": 0,
        "stufe_z": 0,
        "stapel": 0,
        "ohne_nachbar": 0,
        "dxy_luecke": [],
        "dz_stufe": [],
        "dz_stapel": [],
        "worst_luecke": [],
        "worst_stufe": [],
        "vert_mm_shared": 0,
        "vert_mm_dz": [],
        "vert_near_5cm": 0,
        "vert_near_5cm_dz": [],
    }


def _keep_worst(bag: list, item: tuple, limit: int = 8) -> None:
    bag.append(item)
    bag.sort(key=lambda r: -r[0])
    del bag[limit:]


def main() -> None:
    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    out_dir = proc / "road_grid"
    print(f"meshes in {out_dir}", flush=True)
    meshes = _load_meshes(out_dir)
    if len(meshes) < 2:
        raise SystemExit("need at least two meshes")

    by_kind: dict[str, int] = defaultdict(int)
    for m in meshes:
        by_kind[m["kind"]] += 1
    print(
        "files: "
        + ", ".join(f"{k}={by_kind[k]}" for k in KINDS if by_kind[k]),
        flush=True,
    )

    mids = []
    labels = []
    for mi, m in enumerate(meshes):
        mid = _edge_mids(m["edges"])
        if mid.size == 0:
            continue
        mids.append(mid)
        labels.extend([(mi, m["name"], m["kind"])] * int(mid.shape[0]))
    if not mids:
        raise SystemExit("no open edges")
    mid = np.vstack(mids)
    xy = mid[:, :2]
    z = mid[:, 2]
    tree = cKDTree(xy)

    buckets: dict[tuple[str, str], dict] = defaultdict(_empty_bucket)

    dist, idx = tree.query(xy, k=min(24, len(xy)), workers=-1)
    if dist.ndim == 1:
        dist = dist[:, None]
        idx = idx[:, None]

    for i in range(len(xy)):
        mi, name_i, kind_i = labels[i]
        found = False
        for k in range(1, dist.shape[1]):
            j = int(idx[i, k])
            dxy = float(dist[i, k])
            if dxy > NEAR_M:
                break
            mj, name_j, kind_j = labels[j]
            if mj == mi:
                continue
            found = True
            pair = _pair_key(kind_i, kind_j)
            b = buckets[pair]
            b["n_query_edges"] += 1
            dz = abs(float(z[i] - z[j]))
            stacked = _is_stack_pair(kind_i, kind_j) and dxy <= XY_SEAL_M and dz >= STACK_Z_M
            sample = (
                max(dxy, dz),
                dxy,
                dz,
                name_i,
                name_j,
                float(xy[i, 0]),
                float(xy[i, 1]),
            )
            if stacked:
                b["stapel"] += 1
                b["dz_stapel"].append(dz)
            elif dxy <= XY_SEAL_M and dz <= Z_SEAL_M:
                b["versiegelt"] += 1
            elif dxy <= XY_SEAL_M:
                b["stufe_z"] += 1
                b["dz_stufe"].append(dz)
                _keep_worst(b["worst_stufe"], sample)
            else:
                b["luecke_xy"] += 1
                b["dxy_luecke"].append(dxy)
                _keep_worst(b["worst_luecke"], sample)
            break
        if not found:
            # still count against every other kind that exists? only as unmatched
            # Attribute unmatched to pairs later is noisy; keep per-kind dump.
            pass

    unmatched_by_kind: dict[str, int] = defaultdict(int)
    unmatched_ex: dict[str, list] = defaultdict(list)
    for i in range(len(xy)):
        mi, name_i, kind_i = labels[i]
        hit = False
        for k in range(1, dist.shape[1]):
            dxy = float(dist[i, k])
            if dxy > NEAR_M:
                break
            mj, _nj, _kj = labels[int(idx[i, k])]
            if mj != mi:
                hit = True
                break
        if not hit:
            unmatched_by_kind[kind_i] += 1
            if len(unmatched_ex[kind_i]) < 5:
                x, y = sc.terrain_to_crs(float(xy[i, 0]), float(xy[i, 1]))
                unmatched_ex[kind_i].append((name_i, x, y))

    print("\n--- offene Kanten, Nachbar = andere Datei innerhalb 25 cm ---\n", flush=True)
    print(
        f"{'paar':<28} {'n':>8} {'dicht':>8} {'luecke':>8} {'stufe_z':>8} {'stapel':>8}",
        flush=True,
    )
    for pair in sorted(buckets):
        b = buckets[pair]
        label = f"{pair[0]} / {pair[1]}"
        print(
            f"{label:<28} {b['n_query_edges']:8d} {b['versiegelt']:8d} "
            f"{b['luecke_xy']:8d} {b['stufe_z']:8d} {b['stapel']:8d}",
            flush=True,
        )
        if b["luecke_xy"]:
            arr = np.asarray(b["dxy_luecke"])
            print(
                f"    luecke cm  p50 {np.median(arr)*100:.1f}  "
                f"p90 {_pct(arr, 90)*100:.1f}  max {arr.max()*100:.1f}",
                flush=True,
            )
        if b["stufe_z"]:
            arr = np.asarray(b["dz_stufe"])
            print(
                f"    stufe cm   p50 {np.median(arr)*100:.1f}  "
                f"p90 {_pct(arr, 90)*100:.1f}  max {arr.max()*100:.1f}",
                flush=True,
            )
        if b["stapel"]:
            arr = np.asarray(b["dz_stapel"])
            print(
                f"    stapel cm  p50 {np.median(arr)*100:.1f}  "
                f"max {arr.max()*100:.1f}",
                flush=True,
            )
        for row in b["worst_stufe"][:5]:
            x, y = sc.terrain_to_crs(row[5], row[6])
            print(
                f"    stufe {row[2]*100:.1f} cm  {row[3]} / {row[4]}  "
                f"CRS {x:.1f},{y:.1f}",
                flush=True,
            )
        for row in b["worst_luecke"][:5]:
            x, y = sc.terrain_to_crs(row[5], row[6])
            print(
                f"    luecke {row[1]*100:.1f} cm dz {row[2]*100:.1f} cm  "
                f"{row[3]} / {row[4]}  CRS {x:.1f},{y:.1f}",
                flush=True,
            )

    print("\n--- Kanten ohne Nachbar-Datei in 25 cm (meist Umriss) ---\n", flush=True)
    for kind in KINDS:
        n = unmatched_by_kind.get(kind, 0)
        if not n:
            continue
        print(f"  {kind}: {n}", flush=True)
        for name, x, y in unmatched_ex[kind]:
            print(f"    {name}  CRS {x:.1f},{y:.1f}", flush=True)

    print("\n--- Vertices: gleiches mm-XY in zwei Dateien ---\n", flush=True)
    vert_bags: dict[tuple[int, int], list] = defaultdict(list)
    for mi, m in enumerate(meshes):
        for p in m["verts"]:
            key = (round(float(p[0]), MM), round(float(p[1]), MM))
            vert_bags[key].append((mi, float(p[2])))

    vert_pair: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"n": 0, "dz": [], "worst": []}
    )
    for key, hits in vert_bags.items():
        files = {h[0] for h in hits}
        if len(files) < 2:
            continue
        by_file: dict[int, list[float]] = defaultdict(list)
        for mi, zz in hits:
            by_file[mi].append(zz)
        ids = sorted(by_file)
        for a, b in ((ids[i], ids[j]) for i in range(len(ids)) for j in range(i + 1, len(ids))):
            za = by_file[a]
            zb = by_file[b]
            dz = abs(max(za + zb) - min(za + zb))
            pair = _pair_key(meshes[a]["kind"], meshes[b]["kind"])
            rec = vert_pair[pair]
            rec["n"] += 1
            rec["dz"].append(dz)
            if dz > Z_SEAL_M:
                tx, ty = key
                x, y = sc.terrain_to_crs(tx, ty)
                _keep_worst(
                    rec["worst"],
                    (
                        dz,
                        meshes[a]["name"],
                        meshes[b]["name"],
                        x,
                        y,
                    ),
                )

    print(
        f"{'paar':<28} {'mm-XY':>8} {'dz>1cm':>8} {'p50cm':>8} {'maxcm':>8}",
        flush=True,
    )
    for pair in sorted(vert_pair):
        rec = vert_pair[pair]
        arr = np.asarray(rec["dz"])
        n_bad = int((arr > Z_SEAL_M).sum())
        print(
            f"{pair[0] + ' / ' + pair[1]:<28} {rec['n']:8d} {n_bad:8d} "
            f"{np.median(arr)*100:8.1f} {arr.max()*100:8.1f}",
            flush=True,
        )
        for row in rec["worst"][:5]:
            print(
                f"    dz {row[0]*100:.1f} cm  {row[1]} / {row[2]}  "
                f"CRS {row[3]:.1f},{row[4]:.1f}",
                flush=True,
            )

    report = {
        "xy_seal_m": XY_SEAL_M,
        "z_seal_m": Z_SEAL_M,
        "near_m": NEAR_M,
        "stack_z_m": STACK_Z_M,
        "files_by_kind": dict(by_kind),
        "edge_pairs": {
            f"{a}/{b}": {
                "n": buckets[(a, b)]["n_query_edges"],
                "versiegelt": buckets[(a, b)]["versiegelt"],
                "luecke_xy": buckets[(a, b)]["luecke_xy"],
                "stufe_z": buckets[(a, b)]["stufe_z"],
                "stapel": buckets[(a, b)]["stapel"],
            }
            for a, b in sorted(buckets)
        },
        "unmatched_edges_by_kind": dict(unmatched_by_kind),
        "vert_mm_pairs": {
            f"{a}/{b}": {
                "n": vert_pair[(a, b)]["n"],
                "dz_gt_1cm": int(
                    (np.asarray(vert_pair[(a, b)]["dz"]) > Z_SEAL_M).sum()
                ),
                "dz_max_m": float(np.max(vert_pair[(a, b)]["dz"])),
            }
            for a, b in sorted(vert_pair)
        },
    }
    dest = out_dir / "mesh_type_gaps.json"
    dest.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {dest}", flush=True)

    bad_edge = sum(
        buckets[p]["luecke_xy"] + buckets[p]["stufe_z"] for p in buckets
    )
    bad_vert = sum(
        int((np.asarray(vert_pair[p]["dz"]) > Z_SEAL_M).sum())
        for p in vert_pair
        if not _is_stack_pair(*p)
    )
    print(
        f"\nsumme luecke+stufe (Kanten): {bad_edge}  "
        f"mm-XY |dZ|>1cm ohne Stapel-Paare (Vertices): {bad_vert}",
        flush=True,
    )


if __name__ == "__main__":
    main()
