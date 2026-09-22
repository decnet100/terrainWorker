"""Fetch a georeferenced overview PNG for the Alpine Roadtrip HUD map.

The image is north-up in the portal ``map.bbox`` CRS (EPSG:31254 for Tirol).
HUD pixels use the same affine as the site masks:

    u = (easting  - xmin) / (xmax - xmin)
    v = (northing - ymin) / (ymax - ymin)
    px = u * (width - 1)
    py = (1 - v) * (height - 1)

Default source is basemap.at WMTS (CC-BY 4.0, attribution
``Datenquelle: basemap.at``). Later regions plug in another entry in SOURCES
(swisstopo Landeskarte, OSM, South Tyrol OGD).

Usage:
  python tools\\fetch_overview_basemap.py
  python tools\\fetch_overview_basemap.py --force
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sys
from pathlib import Path

import numpy as np
import requests
import yaml
from PIL import Image, ImageDraw, ImageFont
from pyproj import Transformer
from scipy.ndimage import map_coordinates

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

PORTALS_YAML = ROOT / "config" / "alpine_rt" / "portals.yaml"
MOD_APP = ROOT / "mods" / "autoroad_alpine_rt" / "ui" / "modules" / "apps" / "alpineRtTrafficMap"
TILE_CACHE = ROOT / "data" / "raw" / "basemap_at_tiles"
UA = {"User-Agent": "beamng_autoroad/0.1 (local alpine-rt overview; CC-BY basemap.at)"}

# Tile URL is Google-3857 (z / row / col). Fallback host if mapsneu is down.
SOURCES = {
    "basemap_at": {
        "kind": "wmts_z_row_col",
        "urls": (
            "https://mapsneu.wien.gv.at/basemap/geolandbasemap/normal/google3857/{z}/{y}/{x}.png",
            "https://maps.wien.gv.at/basemap/geolandbasemap/normal/google3857/{z}/{y}/{x}.png",
        ),
        "tile_crs": "EPSG:3857",
        "tile_size": 256,
        "attribution": "Datenquelle: basemap.at",
        "license": "CC-BY 4.0",
    },
    # Stubs: wire when a site bbox leaves Austria.
    "swisstopo_landeskarte": {
        "kind": "wms",
        "url": "https://wms.geo.admin.ch/",
        "layer": "ch.swisstopo.pixelkarte-farbe",
        "attribution": "Hintergrund: swisstopo",
        "license": "see geo.admin.ch terms",
    },
    "osm": {
        "kind": "wmts_z_col_row",
        "urls": ("https://tile.openstreetmap.org/{z}/{x}/{y}.png",),
        "tile_crs": "EPSG:3857",
        "tile_size": 256,
        "attribution": "© OpenStreetMap contributors",
        "license": "ODbL",
    },
}

MERCATOR = 20037508.342789244


def affine_crs_to_pixel(
    east: float, north: float, bbox: list[float], width: int, height: int
) -> tuple[float, float]:
    xmin, ymin, xmax, ymax = (float(v) for v in bbox[:4])
    u = (east - xmin) / max(xmax - xmin, 1e-9)
    v = (north - ymin) / max(ymax - ymin, 1e-9)
    return u * (width - 1), (1.0 - v) * (height - 1)


def affine_pixel_to_crs(
    px: float, py: float, bbox: list[float], width: int, height: int
) -> tuple[float, float]:
    xmin, ymin, xmax, ymax = (float(v) for v in bbox[:4])
    u = px / max(width - 1, 1)
    v = 1.0 - (py / max(height - 1, 1))
    return xmin + u * (xmax - xmin), ymin + v * (ymax - ymin)


def expand_bbox_to_aspect(bbox: list[float], aspect: float) -> list[float]:
    """Grow a CRS box (do not shrink) until width/height equals aspect (x/y)."""
    xmin, ymin, xmax, ymax = (float(v) for v in bbox[:4])
    cx = 0.5 * (xmin + xmax)
    cy = 0.5 * (ymin + ymax)
    w = max(1.0, xmax - xmin)
    h = max(1.0, ymax - ymin)
    aspect = float(aspect)
    if aspect <= 0:
        return [xmin, ymin, xmax, ymax]
    if w / h < aspect:
        w = h * aspect
    else:
        h = w / aspect
    return [
        round(cx - 0.5 * w, 1),
        round(cy - 0.5 * h, 1),
        round(cx + 0.5 * w, 1),
        round(cy + 0.5 * h, 1),
    ]


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2**z
    lat = min(85.05112878, max(-85.05112878, lat))
    x = int(math.floor((lon + 180.0) / 360.0 * n))
    lat_rad = math.radians(lat)
    y = int(
        math.floor(
            (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
        )
    )
    n1 = n - 1
    return max(0, min(n1, x)), max(0, min(n1, y))


def tile_origin_3857(x: int, y: int, z: int) -> tuple[float, float, float]:
    """Top-left of tile (x, y) in EPSG:3857 and pixel size (metres)."""
    n = 2**z
    tile_m = (2.0 * MERCATOR) / n
    origin_x = -MERCATOR + x * tile_m
    origin_y = MERCATOR - y * tile_m
    return origin_x, origin_y, tile_m


def _wgs84_envelope(bbox: list[float], crs: str) -> tuple[float, float, float, float]:
    xmin, ymin, xmax, ymax = (float(v) for v in bbox[:4])
    to_ll = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    xs = [xmin, xmax, xmin, xmax, 0.5 * (xmin + xmax)]
    ys = [ymin, ymin, ymax, ymax, 0.5 * (ymin + ymax)]
    lon, lat = to_ll.transform(xs, ys)
    return float(min(lon)), float(min(lat)), float(max(lon)), float(max(lat))


def _tile_window(
    west: float, south: float, east: float, north: float, z: int
) -> tuple[int, int, int, int, int]:
    z = max(0, min(18, int(z)))
    x0, y0 = lonlat_to_tile(west, north, z)
    x1, y1 = lonlat_to_tile(east, south, z)
    return z, x0, x1, y0, y1


def _choose_zoom(
    west: float,
    south: float,
    east: float,
    north: float,
    *,
    max_tiles: int,
    max_zoom: int,
) -> tuple[int, int, int, int, int]:
    chosen = None
    for z in range(6, max_zoom + 1):
        win = _tile_window(west, south, east, north, z)
        n = (win[2] - win[1] + 1) * (win[4] - win[3] + 1)
        if n <= max_tiles:
            chosen = win
        else:
            break
    if chosen is None:
        return _tile_window(west, south, east, north, min(8, max_zoom))
    return chosen


def _get_tile(urls: tuple[str, ...], z: int, x: int, y: int, dest: Path) -> Image.Image | None:
    if dest.is_file() and dest.stat().st_size > 80:
        try:
            return Image.open(dest).convert("RGB")
        except OSError:
            dest.unlink(missing_ok=True)
    last_err = None
    for tmpl in urls:
        url = tmpl.format(z=z, x=x, y=y)
        try:
            r = requests.get(url, timeout=30, headers=UA)
            r.raise_for_status()
            if r.content[:8] != b"\x89PNG\r\n\x1a\n" and r.content[:2] != b"\xff\xd8":
                last_err = f"not an image ({r.headers.get('content-type')})"
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        except Exception as ex:  # noqa: BLE001
            last_err = ex
    print(f"  tile z{z}/{y}/{x} skip: {last_err}")
    return None


def _mosaic_3857(spec: dict, z: int, x0: int, x1: int, y0: int, y1: int) -> tuple[np.ndarray, float, float, float]:
    ts = int(spec.get("tile_size") or 256)
    cols = x1 - x0 + 1
    rows = y1 - y0 + 1
    canvas = Image.new("RGB", (cols * ts, rows * ts), (230, 230, 226))
    urls = tuple(spec["urls"])
    got = 0
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            cache = TILE_CACHE / str(z) / str(ty) / f"{tx}.png"
            tile = _get_tile(urls, z, tx, ty, cache)
            if tile is None:
                continue
            if tile.size != (ts, ts):
                tile = tile.resize((ts, ts), Image.Resampling.BILINEAR)
            canvas.paste(tile, ((tx - x0) * ts, (ty - y0) * ts))
            got += 1
    print(f"  mosaic {got}/{cols * rows} tiles at z{z}")
    origin_x, origin_y, tile_m = tile_origin_3857(x0, y0, z)
    px = tile_m / ts
    return np.asarray(canvas, dtype=np.float32), origin_x, origin_y, px


def _warp_to_crs(
    mosaic: np.ndarray,
    origin_x: float,
    origin_y: float,
    px_m: float,
    bbox: list[float],
    out_crs: str,
    width: int,
    height: int,
) -> np.ndarray:
    xmin, ymin, xmax, ymax = (float(v) for v in bbox[:4])
    xs = xmin + (np.arange(width, dtype=np.float64) + 0.5) * ((xmax - xmin) / width)
    ys = ymax - (np.arange(height, dtype=np.float64) + 0.5) * ((ymax - ymin) / height)
    xx, yy = np.meshgrid(xs, ys)
    to_merc = Transformer.from_crs(out_crs, "EPSG:3857", always_xy=True)
    xe, yn = to_merc.transform(xx.ravel(), yy.ravel())
    xe = np.asarray(xe, dtype=np.float64).reshape(xx.shape)
    yn = np.asarray(yn, dtype=np.float64).reshape(yy.shape)
    col = (xe - origin_x) / px_m - 0.5
    row = (origin_y - yn) / px_m - 0.5
    coords = np.vstack([row.ravel(), col.ravel()])
    out = np.empty((height, width, 3), dtype=np.uint8)
    for i in range(3):
        samp = map_coordinates(
            mosaic[:, :, i], coords, order=1, mode="nearest", prefilter=False
        )
        out[:, :, i] = np.clip(samp.reshape(height, width), 0, 255)
    return out


def _output_size(bbox: list[float], long_edge: int) -> tuple[int, int]:
    xmin, ymin, xmax, ymax = (float(v) for v in bbox[:4])
    span_x = max(1.0, xmax - xmin)
    span_y = max(1.0, ymax - ymin)
    if span_y >= span_x:
        height = int(long_edge)
        width = max(64, int(round(long_edge * span_x / span_y)))
    else:
        width = int(long_edge)
        height = max(64, int(round(long_edge * span_y / span_x)))
    return width, height


def _draw_attribution(img: Image.Image, text: str) -> None:
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
    except OSError:
        font = None
    pad = 6
    tw, th = draw.textbbox((0, 0), text, font=font)[2:]
    x = img.width - tw - pad - 4
    y = img.height - th - pad - 2
    draw.rectangle((x - 4, y - 2, img.width - 2, img.height - 2), fill=(20, 22, 24))
    draw.text((x, y), text, fill=(230, 232, 228), font=font)


def _write_worldfile(path: Path, bbox: list[float], width: int, height: int) -> None:
    xmin, ymin, xmax, ymax = (float(v) for v in bbox[:4])
    px = (xmax - xmin) / width
    py = (ymax - ymin) / height
    # ESRI world file: centre of the top-left pixel.
    lines = [
        f"{px:.8f}",
        "0.0",
        "0.0",
        f"{-py:.8f}",
        f"{xmin + 0.5 * px:.3f}",
        f"{ymax - 0.5 * py:.3f}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="ascii")


def _meta_matches(meta_path: Path, want: dict) -> bool:
    if not meta_path.is_file():
        return False
    try:
        have = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    for key in ("source", "crs", "bbox", "width", "height", "zoom"):
        if have.get(key) != want.get(key):
            return False
    return True


def fetch_overview_basemap(
    map_cfg: dict,
    *,
    dest: Path | None = None,
    force: bool = False,
) -> Path | None:
    source_id = str(map_cfg.get("source") or "basemap_at").strip().lower()
    if source_id in {"none", "schematic", "off"}:
        return None
    spec = SOURCES.get(source_id)
    if spec is None:
        print(f"Unknown overview source {source_id!r} (known: {', '.join(SOURCES)})")
        return None
    if spec.get("kind") != "wmts_z_row_col":
        print(f"Overview source {source_id} is not wired yet ({spec.get('kind')})")
        return None
    bbox = list(map_cfg.get("bbox") or [])
    if len(bbox) < 4:
        print("map.bbox missing, skip overview fetch")
        return None
    crs = str(map_cfg.get("crs") or "EPSG:31254")
    dest = dest or MOD_APP / "basemap.png"
    long_edge = int(map_cfg.get("long_edge_px") or 1800)
    max_tiles = int(map_cfg.get("max_tiles") or 80)
    width, height = _output_size(bbox, long_edge)
    west, south, east, north = _wgs84_envelope(bbox, crs)
    if map_cfg.get("zoom") is not None:
        z, x0, x1, y0, y1 = _tile_window(west, south, east, north, int(map_cfg["zoom"]))
    else:
        max_zoom = int(map_cfg.get("max_zoom") or 10)
        z, x0, x1, y0, y1 = _choose_zoom(
            west, south, east, north, max_tiles=max_tiles, max_zoom=max_zoom
        )
    want_meta = {
        "source": source_id,
        "crs": crs,
        "bbox": [round(float(v), 1) for v in bbox[:4]],
        "width": width,
        "height": height,
        "zoom": z,
    }
    meta_path = dest.with_suffix(".meta.json")
    if dest.is_file() and dest.stat().st_size > 8000 and not force and _meta_matches(meta_path, want_meta):
        print(f"Using cached overview {dest.relative_to(ROOT)}")
        return dest
    print(
        f"Overview {source_id} {crs} {width}x{height} "
        f"z{z} tiles=({x1 - x0 + 1}x{y1 - y0 + 1}) WGS84=[{west:.4f},{south:.4f},{east:.4f},{north:.4f}]"
    )
    mosaic, origin_x, origin_y, px_m = _mosaic_3857(spec, z, x0, x1, y0, y1)
    rgb = _warp_to_crs(mosaic, origin_x, origin_y, px_m, bbox, crs, width, height)
    img = Image.fromarray(rgb, mode="RGB")
    attr = str(spec.get("attribution") or "")
    if attr:
        _draw_attribution(img, attr)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "PNG", optimize=True)
    _write_worldfile(dest.with_suffix(".pgw"), bbox, width, height)
    meta = {
        **want_meta,
        "attribution": attr,
        "license": spec.get("license"),
        "zoom": z,
        "wgs84_bbox": [west, south, east, north],
        "pixel_size_m": [
            round((float(bbox[2]) - float(bbox[0])) / width, 3),
            round((float(bbox[3]) - float(bbox[1])) / height, 3),
        ],
        "transform": {
            "u": "(easting - xmin) / (xmax - xmin)",
            "v": "(northing - ymin) / (ymax - ymin)",
            "px": "u * (width - 1)",
            "py": "(1 - v) * (height - 1)",
        },
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {dest.relative_to(ROOT)} + {dest.with_suffix('.pgw').name}")
    return dest


def apply_source_metadata(map_cfg: dict) -> dict:
    """Copy attribution / CRS defaults from the chosen source into map_cfg."""
    out = dict(map_cfg)
    source_id = str(out.get("source") or "basemap_at").strip().lower()
    spec = SOURCES.get(source_id) or {}
    out.setdefault("crs", "EPSG:31254")
    if spec.get("attribution") and not out.get("attribution"):
        out["attribution"] = spec["attribution"]
    if spec.get("license") and not out.get("license"):
        out["license"] = spec["license"]
    out["source"] = source_id
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=PORTALS_YAML)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    data = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    map_cfg = apply_source_metadata(dict(data.get("map") or {}))
    if not map_cfg.get("bbox"):
        from build_portals import _sites_by_level, _union_site_bbox

        gates = data.get("gates") or []
        levels = sorted(
            {str(g["from_level"]) for g in gates} | {str(g["to_level"]) for g in gates}
        )
        union = _union_site_bbox(_sites_by_level(), levels)
        aspect = map_cfg.get("image_aspect")
        map_cfg["bbox"] = expand_bbox_to_aspect(union, float(aspect)) if aspect and union else union
    dest = fetch_overview_basemap(map_cfg, force=args.force)
    if dest is None:
        raise SystemExit("overview basemap not written")


if __name__ == "__main__":
    main()
