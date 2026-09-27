"""Fetch the Tirol orthophoto along the main roads from the public WMTS.

The service is JPEG tiles in EPSG:31254. The finest level is about 13.2 cm,
resampled from the 20 cm mosaic. Tiles are kept where they meet a 20 m
buffer around the Landes- and Bundesstraßen. A GDAL VRT mosaics them for
QGIS. The playable terrain is unchanged.

    cd C:\\temp\\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/imst.yaml"; python tools\\fetch_road_corridor_ortho.py
"""
from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

from build_asphalt_meshroads import _is_main_road
from build_decal_roads import _cfg, stitch_abutting_roads
from diag_road_bed_modes import _gip_polylines
from filter_road_corridor import write_referenced_tif
from site_coords import SiteCoords, load_site, processed_dir, site_slug

ROOT = Path(__file__).resolve().parents[1]
WMTS = (
    "https://gis.tirol.gv.at/arcgis/rest/services/Basis/basis_ortho/MapServer/WMTS"
    "/tile/1.0.0/Basis_basis_ortho/default/default028mm"
)
# OGC pixel size at the stated scale, and the WMTS top-left as northing, easting.
SCALE_0 = 1935241.9657217835
LEVEL = 12
RES_M = SCALE_0 / (2**LEVEL) * 0.00028
TILE_PX = 256
SPAN_M = TILE_PX * RES_M
ORIGIN_E = -250000.0
ORIGIN_N = 470000.0
BUFFER_M = 20.0
WORKERS = 8
RETRIES = 4


def _tiles_for_roads(roads: list[dict], sc: SiteCoords) -> list[tuple[int, int]]:
    found: set[tuple[int, int]] = set()
    for road in roads:
        pts = road.get("pts") or []
        for a, b in zip(pts, pts[1:]):
            x0, y0 = sc.beamng_to_crs(float(a[0]), float(a[1]))
            x1, y1 = sc.beamng_to_crs(float(b[0]), float(b[1]))
            length = math.hypot(x1 - x0, y1 - y0)
            steps = max(1, int(math.ceil(length / 8.0)))
            for i in range(steps + 1):
                t = i / steps
                east = x0 + t * (x1 - x0)
                north = y0 + t * (y1 - y0)
                c0 = int(math.floor((east - BUFFER_M - ORIGIN_E) / SPAN_M))
                c1 = int(math.floor((east + BUFFER_M - ORIGIN_E) / SPAN_M))
                r0 = int(math.floor((ORIGIN_N - (north + BUFFER_M)) / SPAN_M))
                r1 = int(math.floor((ORIGIN_N - (north - BUFFER_M)) / SPAN_M))
                for row in range(r0, r1 + 1):
                    for col in range(c0, c1 + 1):
                        found.add((row, col))
    return sorted(found)


def _tile_frame(row: int, col: int) -> tuple[float, float]:
    """West edge and north edge of one WMTS tile, EPSG:31254."""
    west = ORIGIN_E + col * SPAN_M
    north = ORIGIN_N - row * SPAN_M
    return west, north


def _fetch_one(row: int, col: int, dest: Path) -> str | None:
    import requests

    if dest.is_file() and dest.stat().st_size > 1000:
        return None
    url = f"{WMTS}/{LEVEL}/{row}/{col}.jpg"
    headers = {"User-Agent": "beamng_autoroad/0.1"}
    last = "no attempt"
    for _attempt in range(1, RETRIES + 1):
        try:
            response = requests.get(url, headers=headers, timeout=60)
            response.raise_for_status()
            if not response.content.startswith(b"\xff\xd8"):
                raise RuntimeError(f"not jpeg ({response.headers.get('content-type')})")
            image = Image.open(BytesIO(response.content)).convert("RGB")
            if image.size != (TILE_PX, TILE_PX):
                raise RuntimeError(f"size {image.size}")
            west, north = _tile_frame(row, col)
            write_referenced_tif(
                dest,
                np.asarray(image, dtype=np.uint8),
                xmin=west,
                ymax=north,
                res=RES_M,
            )
            return None
        except Exception as ex:  # noqa: BLE001
            last = str(ex)
    return last


def _write_vrt(path: Path, tiles: list[tuple[int, int]]) -> None:
    rows = [row for row, _col in tiles]
    cols = [col for _row, col in tiles]
    row0, row1 = min(rows), max(rows)
    col0, col1 = min(cols), max(cols)
    width = (col1 - col0 + 1) * TILE_PX
    height = (row1 - row0 + 1) * TILE_PX
    xmin = ORIGIN_E + col0 * SPAN_M
    ymax = ORIGIN_N - row0 * SPAN_M
    sources = {1: [], 2: [], 3: []}
    for row, col in tiles:
        name = f"r{row}_c{col}.tif"
        xoff = (col - col0) * TILE_PX
        yoff = (row - row0) * TILE_PX
        block = (
            "    <SimpleSource>\n"
            f"      <SourceFilename relativeToVRT=\"1\">{name}</SourceFilename>\n"
            "      <SourceBand>{band}</SourceBand>\n"
            "      <SrcRect xOff=\"0\" yOff=\"0\" xSize=\"256\" ySize=\"256\" />\n"
            f"      <DstRect xOff=\"{xoff}\" yOff=\"{yoff}\" xSize=\"256\" ySize=\"256\" />\n"
            "    </SimpleSource>\n"
        )
        for band in (1, 2, 3):
            sources[band].append(block.format(band=band))
    names = ("Red", "Green", "Blue")
    bands = []
    for band, name in zip((1, 2, 3), names):
        bands.append(
            "  <VRTRasterBand dataType=\"Byte\" band=\"{band}\">\n"
            "    <ColorInterp>{name}</ColorInterp>\n"
            "{sources}"
            "  </VRTRasterBand>\n".format(
                band=band, name=name, sources="".join(sources[band])
            )
        )
    path.write_text(
        "<VRTDataset rasterXSize=\"{width}\" rasterYSize=\"{height}\">\n"
        "  <SRS>EPSG:31254</SRS>\n"
        "  <GeoTransform>{xmin}, {res}, 0, {ymax}, 0, {neg}</GeoTransform>\n"
        "{bands}"
        "</VRTDataset>\n".format(
            width=width,
            height=height,
            xmin=xmin,
            res=RES_M,
            ymax=ymax,
            neg=-RES_M,
            bands="".join(bands),
        ),
        encoding="utf-8",
    )


def main() -> None:
    site = load_site()
    sc = SiteCoords(site)
    proc = processed_dir(site)
    cfg = _cfg((site.get("beamng") or {}))
    roads = [
        road
        for road in stitch_abutting_roads(_gip_polylines(site, proc), cfg)
        if _is_main_road(road, site)
    ]
    tiles = _tiles_for_roads(roads, sc)
    print(
        f"ortho WMTS level {LEVEL}  {RES_M:.4f} m  buffer={BUFFER_M:.0f} m  "
        f"tiles={len(tiles)}",
        flush=True,
    )
    if not tiles:
        raise SystemExit("no orthophoto tiles")

    out_dir = ROOT / "data" / "raw" / f"ortho_{site_slug(site)}_corridor_wmts"
    out_dir.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(_fetch_one, row, col, out_dir / f"r{row}_c{col}.tif"): (row, col)
            for row, col in tiles
        }
        for fut in as_completed(futures):
            row, col = futures[fut]
            err = fut.result()
            done += 1
            if err:
                failed.append(f"r{row}_c{col}: {err}")
                print(f"  {done}/{len(tiles)} r{row}_c{col} failed: {err}", flush=True)
            elif done % 200 == 0 or done == len(tiles):
                print(f"  {done}/{len(tiles)}", flush=True)

    vrt = out_dir / "corridor_ortho.vrt"
    ok_tiles = [
        (row, col)
        for row, col in tiles
        if (out_dir / f"r{row}_c{col}.tif").is_file()
    ]
    _write_vrt(vrt, ok_tiles)
    index = {
        "crs": "EPSG:31254",
        "service": WMTS,
        "level": LEVEL,
        "resolution_m": RES_M,
        "tile_px": TILE_PX,
        "tile_m": SPAN_M,
        "origin_easting": ORIGIN_E,
        "origin_northing": ORIGIN_N,
        "top_left_axis_order": "northing, easting",
        "buffer_m": BUFFER_M,
        "format": "jpeg resampled from the 20 cm mosaic",
        "tiles": len(ok_tiles),
        "failed": failed,
        "vrt": vrt.name,
    }
    (out_dir / "corridor_ortho_index.json").write_text(
        json.dumps(index, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {vrt} tiles={len(ok_tiles)} failed={len(failed)}", flush=True)
    if failed:
        raise SystemExit(f"{len(failed)} tile(s) failed")


if __name__ == "__main__":
    main()
