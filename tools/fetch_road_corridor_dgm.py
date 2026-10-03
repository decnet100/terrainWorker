"""Fetch the 0.5 m DGM in a corridor around the road mesh.

Landes- and Bundesstraßen, Autobahnen, and Gemeindestraße S-G. S-GW is not
included. The playable heightmap is left unchanged.

The site DGM is stored at 1 m. This requests the 50 cm coverage only for tiles
the road buffer touches, and leaves the playable heightmap unchanged.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\fetch_road_corridor_dgm.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import tifffile as tiff

from build_decal_roads import _cfg, stitch_abutting_roads
from gip_road_segments import road_mesh_piece
from diag_road_bed_modes import _gip_polylines
from raster_tile_fetch import elevation_nodata_mask, http_get_geotiff, read_tiff_bytes
from site_coords import SiteCoords, load_site, processed_dir, site_slug

ROOT = Path(__file__).resolve().parents[1]
RES_M = 0.5
BUFFER_M = 15.0
TILE_M = 512.0
TILE_PX = int(round(TILE_M / RES_M))
MAX_NODATA = 0.05
RETRIES = 4


def _tiles_for_roads(roads: list[dict], sc: SiteCoords) -> list[tuple[int, int]]:
    half = TILE_M
    found: set[tuple[int, int]] = set()
    xmin, ymin = sc.xmin, sc.ymin
    nx = int(math.ceil((sc.xmax - xmin) / TILE_M))
    ny = int(math.ceil((sc.ymax - ymin) / TILE_M))

    def mark(x: float, y: float) -> None:
        ix0 = int(math.floor((x - BUFFER_M - xmin) / TILE_M))
        ix1 = int(math.floor((x + BUFFER_M - xmin) / TILE_M))
        iy0 = int(math.floor((y - BUFFER_M - ymin) / TILE_M))
        iy1 = int(math.floor((y + BUFFER_M - ymin) / TILE_M))
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                if 0 <= ix < nx and 0 <= iy < ny:
                    found.add((ix, iy))

    for road in roads:
        pts = road.get("pts") or []
        for a, b in zip(pts, pts[1:]):
            x0, y0 = sc.beamng_to_crs(float(a[0]), float(a[1]))
            x1, y1 = sc.beamng_to_crs(float(b[0]), float(b[1]))
            length = math.hypot(x1 - x0, y1 - y0)
            steps = max(1, int(math.ceil(length / 10.0)))
            for i in range(steps + 1):
                t = i / steps
                mark(x0 + t * (x1 - x0), y0 + t * (y1 - y0))
    return sorted(found)


def _tile_bbox(sc: SiteCoords, ix: int, iy: int) -> tuple[float, float, float, float, int, int]:
    xmin = sc.xmin + ix * TILE_M
    ymin = sc.ymin + iy * TILE_M
    xmax = min(sc.xmax, xmin + TILE_M)
    ymax = min(sc.ymax, ymin + TILE_M)
    width = max(1, int(round((xmax - xmin) / RES_M)))
    height = max(1, int(round((ymax - ymin) / RES_M)))
    return xmin, ymin, xmax, ymax, width, height


def _download_one(url: str, crs: str, version: str, coverage: str, spec: dict) -> np.ndarray:
    params = {
        "SERVICE": "WCS",
        "VERSION": version,
        "REQUEST": "GetCoverage",
        "COVERAGE": coverage,
        "CRS": crs,
        "BBOX": f"{spec['xmin']},{spec['ymin']},{spec['xmax']},{spec['ymax']}",
        "WIDTH": str(spec["width"]),
        "HEIGHT": str(spec["height"]),
        "FORMAT": "GeoTIFF",
    }
    headers = {"User-Agent": "beamng_autoroad/0.1 (local test)", "Accept": "*/*"}
    data = http_get_geotiff(url, params, timeout_s=600, headers=headers)
    arr = read_tiff_bytes(data)
    if arr.ndim == 3:
        arr = arr[..., 0]
    return arr.astype(np.float32)


def main() -> None:
    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    cfg = _cfg((site.get("beamng") or {}))
    roads = [r for r in stitch_abutting_roads(_gip_polylines(site, proc), cfg) if road_mesh_piece(r)]
    km = 0.0
    for road in roads:
        pts = road.get("pts") or []
        for a, b in zip(pts, pts[1:]):
            km += math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
    tiles = _tiles_for_roads(roads, sc)
    print(
        f"corridor roads={len(roads)} {km/1000:.1f} km  "
        f"buffer={BUFFER_M:.0f} m  tiles={len(tiles)} @ {RES_M} m",
        flush=True,
    )

    src = (site.get("sources") or {}).get("dgm") or {}
    url = str(src.get("url") or "")
    coverage = str(src.get("coverage") or "")
    version = str(src.get("version") or "1.0.0")
    if not url or not coverage:
        raise SystemExit("sources.dgm url/coverage missing")

    out_dir = ROOT / "data" / "raw" / f"dgm_{site_slug(site)}_corridor50"
    out_dir.mkdir(parents=True, exist_ok=True)
    index = []
    failed = []
    for n, (ix, iy) in enumerate(tiles, start=1):
        xmin, ymin, xmax, ymax, width, height = _tile_bbox(sc, ix, iy)
        name = f"r{iy:03d}_c{ix:03d}.tif"
        path = out_dir / name
        spec = {
            "name": name,
            "ix": ix,
            "iy": iy,
            "xmin": xmin,
            "ymin": ymin,
            "xmax": xmax,
            "ymax": ymax,
            "width": width,
            "height": height,
            "resolution_m": RES_M,
        }
        if path.is_file() and path.stat().st_size > 1000:
            arr = np.asarray(tiff.imread(path))
            bad = float(elevation_nodata_mask(arr).mean())
            if bad <= MAX_NODATA:
                spec["nodata_frac"] = round(bad, 5)
                spec["cached"] = True
                index.append(spec)
                print(f"  {n}/{len(tiles)} cache {name} bad={100*bad:.2f}%", flush=True)
                continue
        last_err = ""
        saved = False
        for attempt in range(1, RETRIES + 1):
            try:
                arr = _download_one(url, sc.crs, version, coverage, spec)
                bad = float(elevation_nodata_mask(arr).mean())
                tiff.imwrite(path, arr, compression="deflate")
                spec["nodata_frac"] = round(bad, 5)
                spec["cached"] = False
                index.append(spec)
                print(
                    f"  {n}/{len(tiles)} {name} {width}x{height} bad={100*bad:.2f}%",
                    flush=True,
                )
                saved = True
                if bad > MAX_NODATA:
                    failed.append(f"{name} nodata {100*bad:.1f}%")
                break
            except Exception as ex:  # noqa: BLE001
                last_err = str(ex)
                print(f"  {n}/{len(tiles)} {name} attempt {attempt} failed: {ex}", flush=True)
        if not saved:
            failed.append(f"{name}: {last_err}")

    meta = {
        "crs": sc.crs,
        "resolution_m": RES_M,
        "buffer_m": BUFFER_M,
        "tile_m": TILE_M,
        "coverage": coverage,
        "roads_km": round(km / 1000.0, 3),
        "road_count": len(roads),
        "tiles": index,
        "failed": failed,
    }
    meta_path = out_dir / "corridor_index.json"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {meta_path} tiles={len(index)} failed={len(failed)}", flush=True)
    if failed:
        raise SystemExit(f"{len(failed)} tile(s) failed")


if __name__ == "__main__":
    main()
