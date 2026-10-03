"""Compare the weighted road bed with the exclusive-carriageway trial.

The trial is off unless ``beamng.decal_roads.road_bed_exclusive`` is true.
This script always runs both and writes profiles where they disagree.
It does not import terrain into a level.

Synthetic check (no site data):

    python tools\\diag_road_bed_modes.py --synthetic

Site check (processed DGM required). Hotspots are the disagreement itself:

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\diag_road_bed_modes.py
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_bridges as bb  # noqa: E402
import build_decal_roads as bdr  # noqa: E402
from site_coords import load_site, processed_dir  # noqa: E402


def _road(y: float, z: float, width: float, code: str, highway: str, length: float = 40.0):
    x0, x1 = 20.0, 20.0 + length
    return {
        "highway": highway,
        "str_code": code,
        "pts": [
            [x0, y, z, width],
            [x1, y, z, width],
        ],
    }


def _run_pair(roads, elev, *, size, extent, max_h, smooth_m=12.0, falloff_m=6.0, pad_m=0.0):
    cfg = {
        "road_bed_conform": True,
        "highways": set(),
        "width_scale": 1.0,
        "densify_max_step_m": 4.0,
        "min_length_m": 1.0,
        "road_bed_pad_m": pad_m,
        "road_bed_falloff_m": falloff_m,
        "road_bed_sink_m": 0.0,
        "road_bed_max_delta_m": 50.0,
        "road_bed_max_raise_m": 50.0,
        "road_bed_max_cut_m": 50.0,
        "road_bed_smooth_m": smooth_m,
        "road_bed_items": [],
        "road_bed_exclusive": False,
    }
    kw = dict(elev=elev, size=size, extent=extent, max_h=max_h, meshroads=[])
    out_w, _, _, _ = bdr.conform_road_bed_heightmap(roads, cfg, **kw)
    cfg_x = dict(cfg)
    cfg_x["road_bed_exclusive"] = True
    out_x, _, _, _ = bdr.conform_road_bed_heightmap(roads, cfg_x, **kw)
    return out_w, out_x


def _synthetic() -> int:
    size = 80
    extent = 80.0
    elev = np.zeros((size, size), dtype=np.float64)
    for y in range(size):
        elev[y, :] = 0.0 if y < 40 else 4.0

    # Shelves 8 m apart. Cores are 4 m and 2 m half-width, so the feathers overlap.
    # 8 m between centerlines: cores are 4 m and 2 m, feathers reach into the other core.
    roads = [
        _road(46.0, 0.0, 8.0, "B171", "primary"),
        _road(38.0, 4.0, 4.0, "S-G", "residential"),
    ]
    out_w, out_x = _run_pair(roads, elev, size=size, extent=extent, max_h=20.0, falloff_m=6.0)
    # Pixel row: py = (1 - by/extent) * (size-1)
    def row_for(by: float) -> int:
        return int(round((size - 1) - by / extent * size))

    r_hi = row_for(28.0)
    r_lo = row_for(36.0)
    x = size // 2

    def tilt(grid, row: int) -> float:
        return float(grid[row - 1, x] - grid[row + 1, x])

    print("parallel shelves (core tilt over 2 m, metres)")
    print(f"  weighted  upper={tilt(out_w, r_hi):+.3f}  lower={tilt(out_w, r_lo):+.3f}")
    print(f"  exclusive upper={tilt(out_x, r_hi):+.3f}  lower={tilt(out_x, r_lo):+.3f}")

    nodes = [
        {"x": float(i), "y": 10.0, "z": 0.0, "width": 6.0}
        for i in range(0, 40, 2)
    ]
    nodes[10]["z"] = 8.0
    nodes[10]["hold_z"] = True
    soft = bdr._smooth_polyline_z(nodes, 12.0, freeze_holds=False)
    held = bdr._smooth_polyline_z(nodes, 12.0, freeze_holds=True)
    print("deck sample inside a 12 m window")
    print(f"  weighted  z={soft[10]['z']:.3f}  neighbour={soft[8]['z']:.3f}")
    print(f"  exclusive z={held[10]['z']:.3f}  neighbour={held[8]['z']:.3f}")
    return 0


def _gip_polylines(site: dict, proc: Path) -> list[dict]:
    """Decal polylines from the cached BeamNG GIP file when the source CRS will not reload."""
    import json

    from gip_road_segments import unify_replaced_oids

    path = proc / "gip_roads_beamng.json"
    if not path.is_file():
        from gip_road_segments import load_gip_polylines_for_decals

        return load_gip_polylines_for_decals(site)
    roads = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(roads, dict):
        return []
    skip = unify_replaced_oids(site, "decals")
    out: list[dict] = []
    for key, road in roads.items():
        oid = road.get("objectid")
        if oid is not None and int(oid) in skip:
            continue
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        pts = [(float(n[0]), float(n[1]), float(n[2]), float(n[3])) for n in nodes]
        out.append(
            {
                "id": key,
                "name": road.get("name"),
                "highway": road.get("highway") or "primary",
                "str_code": road.get("str_code"),
                "objectid": road.get("objectid"),
                "objekt": road.get("objekt"),
                "kunstbauten": road.get("kunstbauten"),
                "pts": pts,
                "follow_parent": road.get("follow_parent"),
                "lanes": road.get("lanes"),
            }
        )
    from gip_road_segments import _unify_decal_roads

    out.extend(_unify_decal_roads(site, roads))
    print(f"GIP cache {path.name}: {len(out)} polylines")
    return out


def _load_site_job(site: dict) -> dict | None:
    import json

    import heightmap_layers as hml

    bng = site.get("beamng") or {}
    level_name = str(bng.get("level_name") or "").strip()
    cfg = bdr._cfg(bng)
    proc = processed_dir(site)
    src = str(cfg.get("centerline_source") or "osm").lower().strip()
    print(f"loading roads from {src or 'osm'}", flush=True)
    if src in ("gip", "verkehrswege", "objectid", "oid"):
        roads = _gip_polylines(site, proc)
        print(f"stitching {len(roads)} polylines", flush=True)
        roads = bdr.stitch_abutting_roads(roads, cfg)
    else:
        roads = bb.load_road_polylines(proc)
        roads = bdr.stitch_abutting_roads(roads, cfg)
    if not roads:
        print("no roads")
        return None
    fill = float(cfg.get("width_fill_dip_m") or 0.0)
    blend = float(cfg.get("width_blend_m") or 0.0)
    if fill > 0 or blend > 0:
        from road_width import smooth_roads_widths

        print(f"smoothing widths, {len(roads)} polylines", flush=True)
        roads = smooth_roads_widths(roads, fill_dip_m=fill, blend_m=blend)
    size = int(bng.get("mask_size") or 512)
    meta_path = proc / "heightmap_meta.json"
    if not meta_path.is_file():
        print(f"missing {meta_path}")
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    extent = float(meta.get("terrain_extent_m") or size)
    try:
        print(f"loading DGM {size}", flush=True)
        elev, max_h = hml.load_dgm(proc, size)
    except FileNotFoundError:
        print("no DGM heightmap")
        return None
    meshroads = []
    if cfg.get("clip_bridges") or cfg.get("road_bed_conform"):
        meshroads = bdr._load_meshroads(proc, level_name)
    return {
        "roads": roads,
        "cfg": cfg,
        "elev": elev,
        "size": size,
        "extent": extent,
        "max_h": max_h,
        "meshroads": meshroads,
        "proc": proc,
        "site": site,
    }


def _hotspots(job: dict, out_w, out_x, *, min_dz: float, limit: int, mag=None) -> Path:
    """Pick the strongest disagreement peaks with a spatial keep-out.

    Do not run connected-component label over the full 8192 grid: a loop of
    ``nonzero(labeled == i)`` per blob scans the whole map each time and can
    pin the process for many minutes (or OOM) on Imst-scale masks.
    """
    print(f"hotspots: scanning deltas >= {min_dz:.2f} m", flush=True)
    if mag is None:
        mag = np.abs(out_x - out_w)
    ys, xs = np.nonzero(mag >= min_dz)
    size = int(job["size"])
    if ys.size == 0:
        print(f"no pixel differs by >= {min_dz:.2f} m", flush=True)
        path = job["proc"] / "diag_road_bed_exclusive.csv"
        path.write_text(
            "spot,kind,along_m,offset_m,x_m,y_m,z_dgm,z_weighted,z_exclusive,dz_m\n",
            encoding="utf-8",
        )
        print(f"wrote {path}", flush=True)
        return path

    print(f"hotspots: {ys.size} candidate pixels", flush=True)
    vals = mag[ys, xs]
    order = np.argsort(-vals)
    sep2 = 40 * 40
    blobs: list[tuple[float, int, int, int]] = []
    for oi in order:
        py = int(ys[oi])
        px = int(xs[oi])
        if any((py - cy) * (py - cy) + (px - cx) * (px - cx) < sep2 for _, cy, cx, _ in blobs):
            continue
        # Local patch size for a rough footprint (not a full component count).
        r0 = max(0, py - 20)
        r1 = min(size, py + 21)
        c0 = max(0, px - 20)
        c1 = min(size, px + 21)
        count = int(np.count_nonzero(mag[r0:r1, c0:c1] >= min_dz))
        blobs.append((float(vals[oi]), py, px, count))
        if len(blobs) >= limit:
            break

    proc: Path = job["proc"]
    path = proc / "diag_road_bed_exclusive.csv"
    extent = float(job["extent"])
    base = job["elev"]
    print(f"hotspots: writing {len(blobs)} spots to {path.name}", flush=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["spot", "kind", "along_m", "offset_m", "x_m", "y_m", "z_dgm", "z_weighted", "z_exclusive", "dz_m"]
        )
        for rank, (peak, py, px, count) in enumerate(blobs, start=1):
            bx, by = bb._from_px_beamng(px, py, size, extent)
            peak_dz = float(out_x[py, px] - out_w[py, px])
            print(
                f"spot {rank}: peak {peak_dz:+.3f} m  "
                f"xy=({bx:.1f},{by:.1f})  patch={count}",
                flush=True,
            )
            for kind in ("cross", "long"):
                for step in range(-20, 21):
                    if kind == "cross":
                        yy, xx = py + step, px
                    else:
                        yy, xx = py, px + step
                    if not (0 <= yy < size and 0 <= xx < size):
                        continue
                    x_m, y_m = bb._from_px_beamng(xx, yy, size, extent)
                    w.writerow(
                        [
                            rank,
                            kind,
                            step if kind == "long" else 0,
                            step if kind == "cross" else 0,
                            f"{x_m:.2f}",
                            f"{y_m:.2f}",
                            f"{float(base[yy, xx]):.3f}",
                            f"{float(out_w[yy, xx]):.3f}",
                            f"{float(out_x[yy, xx]):.3f}",
                            f"{float(out_x[yy, xx] - out_w[yy, xx]):.3f}",
                        ]
                    )
    print(f"wrote {path}", flush=True)
    return path


def _site(min_dz: float, limit: int) -> int:
    site = load_site()
    job = _load_site_job(site)
    if job is None:
        return 1
    cfg = dict(job["cfg"])
    cfg["road_bed_exclusive"] = False
    kw = dict(
        elev=job["elev"],
        size=job["size"],
        extent=job["extent"],
        max_h=job["max_h"],
        site=job["site"],
        proc=job["proc"],
        meshroads=job["meshroads"],
    )
    print("pass 1/2 weighted", flush=True)
    out_w, stats_w, _, _ = bdr.conform_road_bed_heightmap(job["roads"], cfg, **kw)
    cfg_x = dict(cfg)
    cfg_x["road_bed_exclusive"] = True
    print("pass 2/2 exclusive", flush=True)
    out_x, stats_x, _, _ = bdr.conform_road_bed_heightmap(job["roads"], cfg_x, **kw)
    mag = np.abs(out_x - out_w)
    print(
        f"pixels > 5 cm: {int(np.count_nonzero(mag > 0.05))}  "
        f"> 20 cm: {int(np.count_nonzero(mag > 0.20))}  "
        f"max {float(mag.max()):.3f} m",
        flush=True,
    )
    print(
        f"weighted {stats_w['elapsed_s']} s   exclusive {stats_x['elapsed_s']} s",
        flush=True,
    )
    _hotspots(job, out_w, out_x, min_dz=min_dz, limit=limit, mag=mag)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--min-dz", type=float, default=0.15)
    ap.add_argument("--spots", type=int, default=8)
    args = ap.parse_args()
    if args.synthetic:
        raise SystemExit(_synthetic())
    raise SystemExit(_site(args.min_dz, args.spots))


if __name__ == "__main__":
    main()
