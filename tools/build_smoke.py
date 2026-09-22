"""Inspect DGM GeoTIFF and emit BeamNG 16-bit heightmap + road axis JSON.

GIP sites (``decal_roads.centerline_source: gip`` or ``sources.roads.type: gip``)
write the axis from Verkehrswege and do not call Overpass. OSM is only the
fallback when the site still uses Overpass roads.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import requests
import tifffile as tiff
from PIL import Image
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import load_site, processed_dir, site_slug  # noqa: E402
from fetch_dgm import dgm_cache_path  # noqa: E402
from road_width import resolve_road_width_m  # noqa: E402

SITE = load_site()
RAW = ROOT / "data" / "raw"
PROC = processed_dir(SITE)
RAW.mkdir(parents=True, exist_ok=True)

BBOX = SITE["bbox"]  # xmin, ymin, xmax, ymax
XMIN, YMIN, XMAX, YMAX = map(float, BBOX)
CRS = SITE.get("crs", "EPSG:31254")
MPP = float(SITE.get("beamng", {}).get("meters_per_pixel", 1.0))
HM_SIZE = int(SITE.get("beamng", {}).get("mask_size", 512))
BNG = SITE.get("beamng") or {}
BW = XMAX - XMIN
BH = YMAX - YMIN
SLUG = site_slug(SITE)


def geo_local_to_beamng(lx: float, ly: float) -> tuple[float, float]:
    """Map bbox-local meters → BeamNG terrain meters.

    Heightmap is square HM_SIZE×HM_SIZE imported with squareSize≈MPP (usually 1.0),
    so both axes of the (possibly non-square) bbox are stretched onto the terrain square.
    """
    extent = HM_SIZE * MPP
    return lx / BW * extent, ly / BH * extent


def load_dgm(path: Path) -> np.ndarray:
    arr = tiff.imread(path)
    print(f"DGM {path.name}: shape={arr.shape} dtype={arr.dtype} "
          f"min={float(np.nanmin(arr)):.3f} max={float(np.nanmax(arr)):.3f}")
    return np.asarray(arr, dtype=np.float64)


def to_heightmap(elev: np.ndarray, out_size: int = 512) -> tuple[np.ndarray, float, float]:
    """Resample to square power-of-two grayscale uint16 for BeamNG."""
    from PIL import Image as PILImage

    # Replace nodata-ish values
    elev = elev.copy()
    elev[~np.isfinite(elev)] = np.nan
    zmin = float(np.nanmin(elev))
    zmax = float(np.nanmax(elev))
    if zmax <= zmin:
        raise RuntimeError("Invalid elevation range")
    # Pad relief a bit so terrain isn't clipped at extremes
    pad = max(5.0, 0.05 * (zmax - zmin))
    z0, z1 = zmin - pad, zmax + pad

    norm = (elev - z0) / (z1 - z0)
    norm = np.clip(np.nan_to_num(norm, nan=0.0), 0.0, 1.0)
    u16 = (norm * 65535.0).astype(np.uint16)

    img = PILImage.fromarray(u16, mode="I;16")
    # BeamNG: row 0 is typically +Y / north depending on import; keep geo top=north
    img = img.resize((out_size, out_size), resample=PILImage.Resampling.BILINEAR)
    return np.array(img), z0, z1


def _uses_gip_axis(site: dict | None = None) -> bool:
    site = site or SITE
    bng = site.get("beamng") or {}
    if str((bng.get("decal_roads") or {}).get("centerline_source") or "").lower() == "gip":
        return True
    if str((bng.get("guardrails") or {}).get("centerline") or "").lower() == "gip":
        return True
    roads_type = str(((site.get("sources") or {}).get("roads") or {}).get("type") or "").lower()
    return roads_type == "gip"


def fetch_osm_roads() -> list[dict]:
    """Fetch OSM highways in bbox, return list of roads as node lists in local meters."""
    to_wgs = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
    west, south = to_wgs.transform(XMIN, YMIN)
    east, north = to_wgs.transform(XMAX, YMAX)
    # Overpass expects south,west,north,east
    query = f"""
    [out:json][timeout:60];
    (
      way["highway"~"motorway|trunk|primary|secondary|tertiary|unclassified|residential|service|track|path|living_street"]({south},{west},{north},{east});
    );
    out geom;
    """
    cache = RAW / f"osm_roads_{SLUG}.json"
    read_path = cache
    if not read_path.exists():
        legacy = RAW / "osm_roads.json"
        if legacy.exists() and SLUG.startswith("tirol-m28"):
            read_path = legacy
    if read_path.exists():
        print(f"Using cached {read_path}")
        data = json.loads(read_path.read_text(encoding="utf-8"))
    else:
        print("Overpass query…")
        headers = {
            "User-Agent": "beamng_autoroad/0.1 (local test)",
            "Accept": "application/json",
        }
        last_err = None
        data = None
        for url in (
            "https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
        ):
            try:
                r = requests.post(url, data={"data": query}, headers=headers, timeout=90)
                r.raise_for_status()
                data = r.json()
                cache.write_text(json.dumps(data), encoding="utf-8")
                break
            except Exception as ex:  # noqa: BLE001
                last_err = ex
                print(f"Overpass failed at {url}: {ex}")
        if data is None:
            raise RuntimeError(f"Overpass failed: {last_err}")
    to_local = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)

    roads = []
    width_src_counts: dict[str, int] = {}
    for el in data.get("elements", []):
        if el.get("type") != "way" or "geometry" not in el:
            continue
        tags = el.get("tags", {})
        hw = tags.get("highway", "unclassified")
        width, lanes, wsrc = resolve_road_width_m(tags=tags, highway=hw, bng=BNG)
        width_src_counts[wsrc] = width_src_counts.get(wsrc, 0) + 1
        nodes = []
        for g in el["geometry"]:
            x, y = to_local.transform(g["lon"], g["lat"])
            # BeamNG local: origin at bbox southwest, z up; y north
            lx = x - XMIN
            ly = y - YMIN
            if lx < -50 or ly < -50 or lx > (XMAX - XMIN) + 50 or ly > (YMAX - YMIN) + 50:
                continue
            nodes.append([lx, ly, None, width])  # z filled later
        if len(nodes) >= 2:
            roads.append({
                "id": el["id"],
                "highway": hw,
                "name": tags.get("name") or tags.get("ref") or "",
                "lanes": lanes,
                "width_source": wsrc,
                "nodes": nodes,
            })
    print(f"OSM roads: {len(roads)} width_sources={width_src_counts}")
    return roads


def sample_z(elev: np.ndarray, lx: float, ly: float) -> float | None:
    """Sample elevation at local meters from SW corner; elev covers full bbox."""
    bw = XMAX - XMIN
    bh = YMAX - YMIN
    if lx < 0 or ly < 0 or lx > bw or ly > bh:
        return None
    h, w = elev.shape[:2]
    fx = lx / bw * (w - 1)
    fy = (1.0 - ly / bh) * (h - 1)
    x0 = int(math.floor(fx))
    y0 = int(math.floor(fy))
    x1 = min(x0 + 1, w - 1)
    y1 = min(y0 + 1, h - 1)
    x0 = max(0, min(x0, w - 1))
    y0 = max(0, min(y0, h - 1))
    tx, ty = fx - x0, fy - y0
    vals = [float(elev[y0, x0]), float(elev[y0, x1]), float(elev[y1, x0]), float(elev[y1, x1])]
    if not all(math.isfinite(v) for v in vals):
        return None
    v00, v10, v01, v11 = vals
    return (v00 * (1 - tx) * (1 - ty) + v10 * tx * (1 - ty) +
            v01 * (1 - tx) * ty + v11 * tx * ty)


def fill_road_heights(roads: list[dict], elev: np.ndarray, z0: float) -> list[dict]:
    """Set z relative to terrain base z0; XY in BeamNG terrain meters."""
    out = []
    for road in roads:
        nodes = []
        for lx, ly, _, width in road["nodes"]:
            z_abs = sample_z(elev, lx, ly)
            if z_abs is None:
                continue
            z_rel = z_abs - z0
            bx, by = geo_local_to_beamng(lx, ly)
            nodes.append([round(bx, 3), round(by, 3), round(z_rel, 3), round(width, 2)])
        if len(nodes) < 2:
            continue
        if len(nodes) >= 3:
            zs = [n[2] for n in nodes]
            sm = zs[:]
            for i in range(1, len(zs) - 1):
                sm[i] = 0.25 * zs[i - 1] + 0.5 * zs[i] + 0.25 * zs[i + 1]
            for i, n in enumerate(nodes):
                n[2] = round(sm[i], 3)
        road2 = dict(road)
        road2["nodes"] = nodes
        out.append(road2)
    return out


def roads_to_beamng_json(roads: list[dict]) -> dict:
    """Format for BeamNG Terrain And Road Importer style."""
    payload = {}
    for i, road in enumerate(roads):
        entry = {
            "nodes": road["nodes"],
            "highway": road["highway"],
            "name": road["name"],
            "osm_id": road["id"],
        }
        if road.get("lanes") is not None:
            entry["lanes"] = road["lanes"]
        if road.get("width_source"):
            entry["width_source"] = road["width_source"]
        payload[str(i)] = entry
    return payload


def main() -> None:
    dgm_path = dgm_cache_path(SITE)
    if not dgm_path.exists():
        # Legacy Hahntennjoch cache
        legacy = RAW / "dgm_wcs10.tif"
        if legacy.exists() and SLUG.startswith("tirol-m28"):
            dgm_path = legacy
        else:
            raise SystemExit(
                f"Missing {dgm_path} — run: "
                f'$env:AUTOROAD_SITE="config/sites/…"; python tools/fetch_dgm.py'
            )

    elev = load_dgm(dgm_path)
    hm, z0, z1 = to_heightmap(elev, out_size=HM_SIZE)
    hm_path = PROC / f"heightmap_{HM_SIZE}.png"
    Image.fromarray(hm, mode="I;16").save(hm_path)
    meta = {
        "crs": CRS,
        "bbox": [XMIN, YMIN, XMAX, YMAX],
        "size_m": [BW, BH],
        "heightmap": str(hm_path.relative_to(ROOT)),
        "heightmap_size_px": HM_SIZE,
        "meters_per_pixel": MPP,
        "terrain_extent_m": HM_SIZE * MPP,
        "z_min_m": z0,
        "z_max_m": z1,
        "max_height_m": z1 - z0,
        "coord_note": (
            "roads_beamng.json XY are BeamNG terrain meters: "
            "geo_local * (terrain_extent / bbox_size) per axis. "
            f"Import with Meters per Pixel={MPP}, size {HM_SIZE}, origin SW (0,0,0)."
        ),
        "note": "Import PNG as 16-bit grayscale; set Max Height to max_height_m; origin SW.",
    }
    (PROC / "heightmap_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print("Wrote", hm_path)
    print(f"max_height_m={meta['max_height_m']:.2f} mpp={meta['meters_per_pixel']:.3f}")

    roads_path = PROC / "roads_beamng.json"
    if _uses_gip_axis(SITE):
        from gip_road_segments import load_gip_road_segments  # noqa: WPS433

        print("Axis from GIP — skipping Overpass")
        gip_roads = load_gip_road_segments(SITE)
        roads_path.write_text(json.dumps(gip_roads, indent=2), encoding="utf-8")
        print(f"Wrote {roads_path} roads={len(gip_roads)} (gip)")
    else:
        try:
            roads = fetch_osm_roads()
            roads = fill_road_heights(roads, elev, z0)
            roads_path.write_text(
                json.dumps(roads_to_beamng_json(roads), indent=2), encoding="utf-8"
            )
            print("Wrote", roads_path, "roads=", len(roads))
        except RuntimeError as ex:
            print(f"WARNING: {ex}")
            print("Heightmap is written; roads_beamng.json left empty.")
            if not roads_path.is_file():
                roads_path.write_text("{}", encoding="utf-8")

    # Simple preview hillshade PNG for sanity check
    from PIL import Image as PILImage
    e = elev.copy()
    e = np.nan_to_num(e, nan=float(np.nanmean(e)))
    gy, gx = np.gradient(e)
    slope = np.pi / 2 - np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    altitude = np.deg2rad(45)
    azimuth = np.deg2rad(315)
    shaded = np.sin(altitude) * np.sin(slope) + np.cos(altitude) * np.cos(slope) * np.cos(azimuth - aspect)
    shaded = ((shaded - shaded.min()) / (shaded.max() - shaded.min() + 1e-9) * 255).astype(np.uint8)
    PILImage.fromarray(shaded, mode="L").save(PROC / "preview_hillshade.png")
    print("Wrote preview_hillshade.png")

    # Terrain paint masks (OSM landuse + slope + road buffer)
    import runpy
    runpy.run_path(str(ROOT / "tools" / "build_terrain_masks.py"), run_name="__main__")
    runpy.run_path(str(ROOT / "tools" / "build_guardrails.py"), run_name="__main__")


if __name__ == "__main__":
    main()
