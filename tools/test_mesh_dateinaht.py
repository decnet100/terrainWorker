"""Fixture and checks for docs/MESH_DATEINAHT.md (tests 7, 1, 2, 3, 4, 6).

7  small rectangle + template, two DAE
1  every chain point in both files; no extra seam verts
2  role=cell on 0.5 m centres
3  unique cells in order, neighbour step only
4  each chain edge in both DAE, same XYZ to mm
6  road/road gaps on a known seam must not be a plan offset or Z step

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\test_mesh_dateinaht.py
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
from pathlib import Path

import numpy as np
from shapely.geometry import LineString, Point, box

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_corridor_mesh import CLEARANCE_M
from check_road_mesh import _boundary_edges, _kind_of, _parse_dae, _up_faces
from mesh_dateinaht import (
    MAX_CELL_STEP,
    MM,
    RES_M,
    build_seam_chain,
    cell_xy,
    chain_to_json,
    point_to_cell,
    round_mm,
    sample_z_bilinear,
)
from site_coords import SiteCoords, load_site, processed_dir

XY_SEAL_M = 0.01
Z_SEAL_M = 0.01
NEAR_M = 0.25


def _write_dae(path: Path, pos: np.ndarray, faces: np.ndarray, stem: str) -> None:
    n_vert = int(pos.shape[0])
    n_tri = int(faces.shape[0])
    lod = f"{stem}_a999"
    pos_vals = " ".join(f"{v:.3f}" for v in pos.reshape(-1))
    uv = np.zeros((n_vert, 2), dtype=np.float64)
    uv_vals = " ".join(f"{v:.5f}" for v in uv.reshape(-1))
    p_vals = " ".join(str(int(v)) for v in faces.reshape(-1))
    path.write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>
  <library_geometries>
    <geometry id="{lod}-mesh" name="{lod}-mesh">
      <mesh>
        <source id="{lod}-mesh-positions">
          <float_array id="{lod}-mesh-positions-array" count="{n_vert * 3}">{pos_vals}</float_array>
          <technique_common>
            <accessor source="#{lod}-mesh-positions-array" count="{n_vert}" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{lod}-mesh-map-0">
          <float_array id="{lod}-mesh-map-0-array" count="{n_vert * 2}">{uv_vals}</float_array>
          <technique_common>
            <accessor source="#{lod}-mesh-map-0-array" count="{n_vert}" stride="2">
              <param name="S" type="float"/><param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{lod}-mesh-vertices">
          <input semantic="POSITION" source="#{lod}-mesh-positions"/>
        </vertices>
        <triangles count="{n_tri}">
          <input semantic="VERTEX" source="#{lod}-mesh-vertices" offset="0"/>
          <input semantic="TEXCOORD" source="#{lod}-mesh-map-0" offset="0" set="0"/>
          <p>{p_vals}</p>
        </triangles>
      </mesh>
    </geometry>
  </library_geometries>
</COLLADA>
""",
        encoding="utf-8",
    )


def _emit_strip(seam: list[dict], sign: float) -> tuple[np.ndarray, np.ndarray]:
    """Quads on one side of the seam, sharing the chain vertices."""
    pos = []
    faces = []

    def add(p) -> int:
        pos.append((float(p[0]), float(p[1]), float(p[2])))
        return len(pos) - 1

    n = len(seam)
    inner = []
    outer = []
    for p in seam:
        inner.append(add((p["x"], p["y"], p["z"])))
        outer.append(add((p["x"] + sign * RES_M, p["y"], p["z"])))
    for i in range(n - 1):
        a, b = inner[i], inner[i + 1]
        c, d = outer[i + 1], outer[i]
        # (b-a)×(d-a) and (c-a) — wind so +Z
        pa, pb, pd = pos[a], pos[b], pos[d]
        cross = (pb[0] - pa[0]) * (pd[1] - pa[1]) - (pb[1] - pa[1]) * (pd[0] - pa[0])
        if cross >= 0.0:
            faces.append((a, b, c))
            faces.append((a, c, d))
        else:
            faces.append((a, d, c))
            faces.append((a, c, b))
    return np.asarray(pos, dtype=np.float64), np.asarray(faces, dtype=np.int32)


def build_fixture(dest: Path) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    xmin, ymax = 1000.0, 2010.0
    width, height = 40, 20
    road = box(1002.0, 2003.0, 1018.0, 2007.0)
    mask = np.zeros((height, width), dtype=bool)
    z = np.full((height, width), np.nan, dtype=np.float64)
    for r in range(height):
        for c in range(width):
            x, y = cell_xy(xmin, ymax, r, c)
            if road.covers(Point(x, y)) or road.distance(Point(x, y)) < 1.0e-9:
                mask[r, c] = True
                z[r, c] = 100.0 + 0.02 * (x - xmin) + 0.01 * (y - 2000.0)
    template = LineString([(1010.0, 1990.0), (1010.0, 2020.0)])
    chains = build_seam_chain(template, road, mask, z, xmin, ymax)
    if len(chains) != 1:
        raise SystemExit(f"fixture expected 1 seam piece, got {len(chains)}")
    chain = chains[0]
    meta = chain_to_json(chains, left="part_a", right="part_b")
    meta["xmin"] = xmin
    meta["ymax"] = ymax
    (dest / "mesh_seams.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    pos_l, faces_l = _emit_strip(chain, -1.0)
    pos_r, faces_r = _emit_strip(chain, 1.0)
    _write_dae(dest / "part_a.dae", pos_l, faces_l, "part_a")
    _write_dae(dest / "part_b.dae", pos_r, faces_r, "part_b")
    (dest / "road_grid_index.json").write_text(
        json.dumps(
            {
                "tiles": [
                    {"name": "part_a", "kind": "road"},
                    {"name": "part_b", "kind": "road"},
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return meta


def _load_up(path: Path) -> tuple[np.ndarray, np.ndarray]:
    pos, faces = _parse_dae(path)
    up = _up_faces(pos, faces)
    return pos, up


def _dae_to_world(pos: np.ndarray, sc: SiteCoords, z_min: float) -> np.ndarray:
    """BeamNG lattice + relative Z -> CRS / DGM height (same as the chain)."""
    out = np.empty_like(pos)
    out[:, 0] = sc.xmin + pos[:, 0] / sc.terrain_span * sc.bw
    out[:, 1] = sc.ymin + pos[:, 1] / sc.terrain_span * sc.bh
    out[:, 2] = pos[:, 2] + z_min - CLEARANCE_M
    return out


def _mm_set(pos: np.ndarray) -> set[tuple]:
    out = set()
    for p in pos:
        out.add(round_mm(p[0], p[1], p[2]))
    return out


def _mm_xy_set(pos: np.ndarray) -> dict[tuple, list[float]]:
    bag: dict[tuple, list[float]] = {}
    for p in pos:
        key = round_mm(p[0], p[1])
        bag.setdefault(key, []).append(float(p[2]))
    return bag


def check_1(
    chain: list[dict],
    pos_a: np.ndarray,
    pos_b: np.ndarray,
    xy_a: dict[tuple, list[float]] | None = None,
    xy_b: dict[tuple, list[float]] | None = None,
    allowed_xy: set[tuple] | None = None,
) -> list[str]:
    fail = []
    if xy_a is None:
        xy_a = _mm_xy_set(pos_a)
    if xy_b is None:
        xy_b = _mm_xy_set(pos_b)
    for p in chain:
        key_xy = round_mm(p["x"], p["y"])
        za = xy_a.get(key_xy)
        zb = xy_b.get(key_xy)
        if not za:
            fail.append(f"1 missing in left: {key_xy}")
        elif min(abs(z - p["z"]) for z in za) > Z_SEAL_M:
            fail.append(f"1 left Z mismatch at {key_xy}")
        if not zb:
            fail.append(f"1 missing in right: {key_xy}")
        elif min(abs(z - p["z"]) for z in zb) > Z_SEAL_M:
            fail.append(f"1 right Z mismatch at {key_xy}")
    chain_xy = {round_mm(p["x"], p["y"]) for p in chain}
    if allowed_xy:
        chain_xy |= allowed_xy
    segs = [
        ((chain[i]["x"], chain[i]["y"]), (chain[i + 1]["x"], chain[i + 1]["y"]))
        for i in range(len(chain) - 1)
    ]
    xs = [p["x"] for p in chain]
    ys = [p["y"] for p in chain]
    pad = XY_SEAL_M + 0.02
    xmin_s, xmax_s = min(xs) - pad, max(xs) + pad
    ymin_s, ymax_s = min(ys) - pad, max(ys) + pad
    for pos, side in ((pos_a, "left"), (pos_b, "right")):
        hit = (
            (pos[:, 0] >= xmin_s)
            & (pos[:, 0] <= xmax_s)
            & (pos[:, 1] >= ymin_s)
            & (pos[:, 1] <= ymax_s)
        )
        for p in pos[hit]:
            xy = (float(p[0]), float(p[1]))
            if round_mm(*xy) in chain_xy:
                continue
            on = False
            for (ax, ay), (bx, by) in segs:
                if _dist_point_seg(xy[0], xy[1], ax, ay, bx, by) <= XY_SEAL_M:
                    on = True
                    break
            if on:
                fail.append(f"1 extra seam vert {side} {round_mm(*xy)}")
    return fail


def _dist_point_seg(px, py, ax, ay, bx, by) -> float:
    vx, vy = bx - ax, by - ay
    L2 = vx * vx + vy * vy
    if L2 <= 1.0e-18:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / L2))
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def check_2(chain: list[dict], xmin: float, ymax: float) -> list[str]:
    fail = []
    for p in chain:
        if p["role"] != "cell":
            continue
        r, c = int(p["row"]), int(p["col"])
        cx, cy = cell_xy(xmin, ymax, r, c)
        d = math.hypot(p["x"] - cx, p["y"] - cy)
        if d > 0.001:
            fail.append(f"2 cell not on centre seq={p['seq']} d={d:.4f}")
        rr, cc = point_to_cell(xmin, ymax, p["x"], p["y"])
        if (rr, cc) != (r, c):
            fail.append(f"2 cell index mismatch seq={p['seq']}")
    return fail


def check_3(chain: list[dict]) -> list[str]:
    fail = []
    cells = [(p["row"], p["col"], p["seq"]) for p in chain if p["role"] == "cell"]
    seen = set()
    prev = None
    for r, c, seq in cells:
        key = (int(r), int(c))
        if key in seen:
            fail.append(f"3 duplicate cell {key} seq={seq}")
        seen.add(key)
        if prev is not None:
            step = math.hypot(r - prev[0], c - prev[1])
            if step > MAX_CELL_STEP:
                fail.append(f"3 jump {prev}->{key} step={step:.3f} seq={seq}")
        prev = key
    return fail


def _all_edge_xy_keys(pos: np.ndarray, faces: np.ndarray) -> set[tuple]:
    keys = set()
    if faces.size == 0:
        return keys
    for tri in faces:
        i, j, k = int(tri[0]), int(tri[1]), int(tri[2])
        for u, v in ((i, j), (j, k), (k, i)):
            p = round_mm(pos[u, 0], pos[u, 1])
            q = round_mm(pos[v, 0], pos[v, 1])
            keys.add((p, q) if p <= q else (q, p))
    return keys


def check_4(
    chain: list[dict],
    pos_a: np.ndarray,
    faces_a,
    pos_b: np.ndarray,
    faces_b,
    keys_a: set[tuple] | None = None,
    keys_b: set[tuple] | None = None,
) -> list[str]:
    fail = []
    ka = keys_a if keys_a is not None else _all_edge_xy_keys(pos_a, faces_a)
    kb = keys_b if keys_b is not None else _all_edge_xy_keys(pos_b, faces_b)
    for i in range(len(chain) - 1):
        p = round_mm(chain[i]["x"], chain[i]["y"])
        q = round_mm(chain[i + 1]["x"], chain[i + 1]["y"])
        key = (p, q) if p <= q else (q, p)
        if key not in ka:
            fail.append(f"4 chain edge missing in left seq={i}->{i+1}")
        if key not in kb:
            fail.append(f"4 chain edge missing in right seq={i}->{i+1}")
    return fail


def check_6_on_dir(out_dir: Path, seams: list[dict]) -> list[str]:
    """Open-edge midpoints on a recorded seam must meet the other file at mm XY."""
    fail = []
    from scipy.spatial import cKDTree

    parts = []
    for p in sorted(out_dir.glob("part_*.dae")):
        pos, faces = _parse_dae(p)
        up = _up_faces(pos, faces)
        edges = _boundary_edges(pos, up)
        parts.append((p.stem, edges))
    if len(parts) < 2:
        return ["6 need two part_*.dae"]

    mids = []
    owners = []
    for name, edges in parts:
        if edges.size == 0:
            continue
        mid = np.column_stack(
            (
                0.5 * (edges[:, 0] + edges[:, 3]),
                0.5 * (edges[:, 1] + edges[:, 4]),
                0.5 * (edges[:, 2] + edges[:, 5]),
            )
        )
        mids.append(mid)
        owners.extend([name] * len(mid))
    mid = np.vstack(mids)
    z = mid[:, 2]
    owners = np.asarray(owners)
    tree = cKDTree(mid[:, :2])

    segs = []
    for seam in seams:
        pts = seam["points"]
        for i in range(len(pts) - 1):
            segs.append((pts[i], pts[i + 1]))

    def on_seam(x, y) -> bool:
        for a, b in segs:
            if _dist_point_seg(x, y, a["x"], a["y"], b["x"], b["y"]) <= XY_SEAL_M:
                return True
        return False

    n_on = 0
    n_bad = 0
    for i in range(len(mid)):
        if not on_seam(float(mid[i, 0]), float(mid[i, 1])):
            continue
        n_on += 1
        hits = tree.query_ball_point(mid[i, :2], r=XY_SEAL_M)
        other = [
            j
            for j in hits
            if j != i and owners[j] != owners[i]
        ]
        if not other:
            n_bad += 1
            if len(fail) < 12:
                fail.append(f"6 seam midpoint has no other-file match {owners[i]}")
            continue
        dz = min(abs(float(z[i] - z[j])) for j in other)
        if dz > Z_SEAL_M:
            n_bad += 1
            if len(fail) < 12:
                fail.append(
                    f"6 seam midpoint Z step {dz*100:.1f} cm {owners[i]}"
                )
    if n_on == 0:
        fail.append("6 no open-edge midpoints on the recorded seam")
    elif n_bad:
        fail.append(f"6 {n_bad}/{n_on} seam-edge midpoints not sealed")
    return fail


def run_fixture(dest: Path) -> list[str]:
    meta = build_fixture(dest)
    chain = meta["seams"][0]["points"]
    pos_a, up_a = _load_up(dest / "part_a.dae")
    pos_b, up_b = _load_up(dest / "part_b.dae")
    fail = []
    fail.extend(check_1(chain, pos_a, pos_b))
    fail.extend(check_2(chain, meta["xmin"], meta["ymax"]))
    fail.extend(check_3(chain))
    fail.extend(check_4(chain, pos_a, up_a, pos_b, up_b))
    fail.extend(check_6_on_dir(dest, meta["seams"]))
    return fail


def run_site_if_seams() -> tuple[str, list[str]]:
    try:
        site = load_site()
    except Exception as exc:
        return "skip", [f"no site ({exc})"]
    out_dir = processed_dir(site) / "road_grid"
    seam_path = out_dir / "mesh_seams.json"
    if not seam_path.is_file():
        return "skip", [
            f"no {seam_path.name} - Imst still uses the old row split, test 6 skipped"
        ]
    payload = json.loads(seam_path.read_text(encoding="utf-8"))
    seams = payload.get("seams") or []
    if not seams:
        return "skip", ["mesh_seams.json has no seams"]

    sc = SiteCoords(site)
    proc = processed_dir(site)
    z_min = float(
        json.loads((proc / "heightmap_meta.json").read_text(encoding="utf-8"))["z_min_m"]
    )
    xmin = ymax = None
    for seam in seams:
        for p in seam.get("points") or []:
            if p.get("role") == "cell" and p.get("row") is not None:
                xmin = float(p["x"]) - (int(p["col"]) + 0.5) * RES_M
                ymax = float(p["y"]) + (int(p["row"]) + 0.5) * RES_M
                break
        if xmin is not None:
            break

    cache: dict[str, dict] = {}

    def load_named(name: str):
        if name not in cache:
            path = out_dir / f"{name}.dae"
            if not path.is_file():
                cache[name] = None
                return None
            print(f"  load {name}.dae", flush=True)
            pos, up = _load_up(path)
            pos = _dae_to_world(pos, sc, z_min)
            cache[name] = {
                "pos": pos,
                "up": up,
                "xy": _mm_xy_set(pos),
                "edge_keys": _all_edge_xy_keys(pos, up),
            }
        return cache[name]

    print(f"6 site: {len(seams)} seams in {out_dir}", flush=True)
    fail: list[str] = []
    n_ok = 0
    n_skip = 0
    pair_xy: dict[tuple, set] = {}
    for rec in seams:
        pair = (rec.get("left"), rec.get("right"))
        bag = pair_xy.setdefault(pair, set())
        for p in rec.get("points") or []:
            bag.add(round_mm(p["x"], p["y"]))
    for i, seam in enumerate(seams):
        left, right = seam.get("left"), seam.get("right")
        chain = seam.get("points") or []
        if not left or not right or len(chain) < 2:
            n_skip += 1
            continue
        a = load_named(left)
        b = load_named(right)
        if a is None or b is None:
            fail.append(f"6 missing dae {left} or {right}")
            continue
        chunk = []
        chunk.extend(
            check_1(
                chain, a["pos"], b["pos"], a["xy"], b["xy"],
                pair_xy.get((left, right)),
            )
        )
        if xmin is not None:
            chunk.extend(check_2(chain, xmin, ymax))
        chunk.extend(check_3(chain))
        chunk.extend(
            check_4(
                chain,
                a["pos"],
                a["up"],
                b["pos"],
                b["up"],
                a["edge_keys"],
                b["edge_keys"],
            )
        )
        if chunk:
            fail.extend(f"seam {i} {left}/{right}: {m}" for m in chunk[:8])
        else:
            n_ok += 1
        if (i + 1) % 200 == 0:
            print(f"  site seams {i+1}/{len(seams)} ok={n_ok} fail={len(fail)}", flush=True)
    tags = {"1": 0, "2": 0, "3": 0, "4": 0, "6": 0}
    for msg in fail:
        for key in tags:
            if f": {key} " in msg or msg.startswith(f"{key} "):
                tags[key] += 1
                break
    print(
        f"  site seams done: {n_ok} ok, {n_skip} skipped, {len(fail)} messages "
        f"(1={tags['1']} 2={tags['2']} 3={tags['3']} 4={tags['4']} 6={tags['6']})",
        flush=True,
    )
    return "run", fail


def main() -> None:
    dest = Path(tempfile.mkdtemp(prefix="mesh_dateinaht_"))
    print(f"7 fixture -> {dest}", flush=True)
    fail = run_fixture(dest)
    n_chain = json.loads((dest / "mesh_seams.json").read_text(encoding="utf-8"))
    n = len(n_chain["seams"][0]["points"])
    print(f"  chain points {n}", flush=True)
    if fail:
        print(f"FAIL fixture ({len(fail)})", flush=True)
        for line in fail:
            print(f"  {line}", flush=True)
    else:
        print("OK 7+1+2+3+4+6 on fixture", flush=True)

    status, site_fail = run_site_if_seams()
    if status == "skip":
        print(f"6 site: {site_fail[0]}", flush=True)
    elif site_fail:
        print(f"FAIL 6 site ({len(site_fail)})", flush=True)
        for line in site_fail[:24]:
            print(f"  {line}", flush=True)
        if len(site_fail) > 24:
            print(f"  ... {len(site_fail) - 24} more", flush=True)
        fail.extend(site_fail[:24])
    else:
        print("OK 6 on site road_grid", flush=True)

    if fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
