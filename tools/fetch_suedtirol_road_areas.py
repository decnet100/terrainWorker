"""Fetch Südtirol TrasRoadArea polygons and rasterize asphalt / structure masks.

Authority-scoped: only runs when ``sources.road_areas`` is set (Südtirol).
Tirol sites without that block keep OSM/GIP stroke asphalt unchanged.

WFS: ``gvcc-BasicCore:TrasRoadArea-Polygon`` (EPSG:25832).

Writes:
  data/raw/road_areas_suedtirol_<site>.geojson
  data/processed/<site>/mask_road_area.png          (vehicle pavement)
  data/processed/<site>/mask_road_tunnel.png        (LOC tunnel)
  data/processed/<site>/mask_road_bridge.png        (LOC bridge)
  data/processed/<site>/mask_road_junction.png      (PART Kreuzung)
  data/processed/<site>/road_areas.meta.json
  data/processed/<site>/preview_road_areas.png

Usage:
  cd C:\\temp\\beamng_autoroad
  $env:AUTOROAD_SITE = \"config/sites/timmelsjoch.yaml\"
  python tools\\fetch_suedtirol_road_areas.py --force
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import load_site, processed_dir, site_slug  # noqa: E402

DEFAULT_URL = "https://geoservices3.civis.bz.it/geoserver/ows"
DEFAULT_TYPE = "gvcc-BasicCore:TrasRoadArea-Polygon"
DEFAULT_VERSION = "2.0.0"
PAGE = 500
HEADERS = {"User-Agent": "beamng_autoroad/0.1 (road_areas)", "Accept": "*/*"}

# Official BasicCore codes seen on Timmelsjoch / docs.
LOC_GROUND = {"010000"}
LOC_BRIDGE = {"020000"}
LOC_TUNNEL = {"030000"}
PART_CARRIAGEWAY = {"010000"}
PART_JUNCTION = {"020100"}
TYPE_VEHICLE = {"010000"}


def road_areas_cfg(site: dict) -> dict | None:
    """Return road_areas source config, or None if this site does not use it."""
    src = (site.get("sources") or {}).get("road_areas")
    if not isinstance(src, dict) or not src:
        return None
    auth = str(src.get("authority") or src.get("catalog") or "suedtirol").lower()
    if auth not in {"suedtirol", "alto_adige", "bz"}:
        # Explicit non-Südtirol block is ignored here (no silent Tirol override).
        return None
    bng = site.get("beamng") or {}
    return {
        "authority": "suedtirol",
        "url": str(src.get("url") or DEFAULT_URL),
        "type_name": str(src.get("type_name") or src.get("typename") or DEFAULT_TYPE),
        "version": str(src.get("version") or DEFAULT_VERSION),
        "srs": str(src.get("srs") or src.get("crs") or site.get("crs") or "EPSG:25832"),
        "size": int(src.get("size") or bng.get("mask_size") or 2048),
        "timeout_s": int(src.get("timeout_s") or 120),
        "page": int(src.get("page") or PAGE),
    }


def raw_geojson_path(site: dict) -> Path:
    return ROOT / "data" / "raw" / f"road_areas_suedtirol_{site_slug(site)}.geojson"


def _http_json(url: str, *, timeout_s: int) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        data = r.read().decode("utf-8", "replace")
    if data.lstrip().startswith("<"):
        raise SystemExit(f"WFS returned XML/error: {data[:400]}")
    return json.loads(data)


def fetch_geojson(site: dict, cfg: dict, *, force: bool = False) -> dict:
    out = raw_geojson_path(site)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.is_file() and out.stat().st_size > 100 and not force:
        print(f"Using cached road-area GeoJSON: {out}")
        return json.loads(out.read_text(encoding="utf-8"))

    xmin, ymin, xmax, ymax = (float(v) for v in site["bbox"])
    srs = cfg["srs"]
    features: list[dict] = []
    start = 0
    total = None
    while True:
        params = {
            "service": "WFS",
            "version": cfg["version"],
            "request": "GetFeature",
            "typeNames": cfg["type_name"],
            "srsName": srs,
            "bbox": f"{xmin},{ymin},{xmax},{ymax},{srs}",
            "count": str(cfg["page"]),
            "startIndex": str(start),
            "outputFormat": "application/json",
        }
        url = cfg["url"] + ("&" if "?" in cfg["url"] else "?") + urllib.parse.urlencode(params)
        print(f"TrasRoadArea GetFeature startIndex={start} …")
        blob = _http_json(url, timeout_s=cfg["timeout_s"])
        batch = blob.get("features") or []
        if total is None:
            total = blob.get("totalFeatures") or blob.get("numberMatched")
            print(f"numberMatched={total}")
        features.extend(batch)
        start += len(batch)
        if not batch:
            break
        if total is not None and start >= int(total):
            break
        if start > 200_000:
            raise SystemExit("TrasRoadArea pagination exceeded 200k — abort")

    fc = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": srs}},
        "features": features,
    }
    out.write_text(json.dumps(fc), encoding="utf-8")
    print(f"Wrote {out} features={len(features)}")
    return fc


def _rings_px(
    geom: dict,
    *,
    xmin: float,
    ymax: float,
    res: float,
) -> list[list[tuple[float, float]]]:
    gtype = (geom or {}).get("type")
    coords = (geom or {}).get("coordinates")
    raw: list = []
    if gtype == "Polygon" and coords:
        raw.append(coords[0])
    elif gtype == "MultiPolygon" and coords:
        for poly in coords:
            if poly:
                raw.append(poly[0])
    out: list[list[tuple[float, float]]] = []
    for ring in raw:
        pts: list[tuple[float, float]] = []
        for c in ring:
            if len(c) < 2:
                continue
            x, y = float(c[0]), float(c[1])
            pts.append(((x - xmin) / res, (ymax - y) / res))
        if len(pts) >= 3:
            out.append(pts)
    return out


def _code(props: dict, key: str) -> str:
    return str(props.get(key) or "").strip()


def rasterize_masks(
    fc: dict,
    site: dict,
    cfg: dict,
) -> dict[str, np.ndarray]:
    size = int(cfg["size"])
    xmin, ymin, xmax, ymax = (float(v) for v in site["bbox"])
    res = (xmax - xmin) / size
    asphalt = Image.new("L", (size, size), 0)
    tunnel = Image.new("L", (size, size), 0)
    bridge = Image.new("L", (size, size), 0)
    junction = Image.new("L", (size, size), 0)
    da, dt, db, dj = (
        ImageDraw.Draw(asphalt),
        ImageDraw.Draw(tunnel),
        ImageDraw.Draw(bridge),
        ImageDraw.Draw(junction),
    )
    n_asphalt = n_tunnel = n_bridge = n_junc = n_skip = 0
    for feat in fc.get("features") or []:
        props = feat.get("properties") or {}
        typ = _code(props, "TYPE")
        if typ and typ not in TYPE_VEHICLE:
            n_skip += 1
            continue
        rings = _rings_px(
            feat.get("geometry") or {}, xmin=xmin, ymax=ymax, res=res
        )
        if not rings:
            n_skip += 1
            continue
        loc = _code(props, "LOC")
        part = _code(props, "PART")
        for pts in rings:
            # Span principle: tunnel/bridge pavement is not terrain asphalt
            # (deck/bore come later). Only ground (and blank LOC) paint asphalt.
            if loc in LOC_TUNNEL:
                dt.polygon(pts, outline=1, fill=1)
                n_tunnel += 1
                continue
            if loc in LOC_BRIDGE:
                db.polygon(pts, outline=1, fill=1)
                n_bridge += 1
                continue
            da.polygon(pts, outline=1, fill=1)
            n_asphalt += 1
            if part in PART_JUNCTION:
                dj.polygon(pts, outline=1, fill=1)
                n_junc += 1
    print(
        f"Road areas painted: asphalt_polys={n_asphalt} tunnel={n_tunnel} "
        f"bridge={n_bridge} junction={n_junc} skipped={n_skip}"
    )
    return {
        "asphalt": np.asarray(asphalt, dtype=np.uint8) > 0,
        "tunnel": np.asarray(tunnel, dtype=np.uint8) > 0,
        "bridge": np.asarray(bridge, dtype=np.uint8) > 0,
        "junction": np.asarray(junction, dtype=np.uint8) > 0,
    }


def _preview(masks: dict[str, np.ndarray]) -> np.ndarray:
    h, w = masks["asphalt"].shape
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    rgb[:] = (25, 25, 30)
    rgb[masks["asphalt"]] = (50, 50, 55)
    rgb[masks["junction"]] = (220, 160, 40)
    rgb[masks["bridge"]] = (80, 140, 220)
    rgb[masks["tunnel"]] = (160, 60, 200)
    return rgb


def load_road_area_masks(proc: Path, size: int) -> dict[str, np.ndarray] | None:
    """Load previously written road-area masks, or None if missing."""
    asphalt_p = proc / "mask_road_area.png"
    if not asphalt_p.is_file():
        return None

    def _read(name: str) -> np.ndarray:
        path = proc / name
        if not path.is_file():
            return np.zeros((size, size), dtype=bool)
        arr = np.asarray(Image.open(path).convert("L"))
        if arr.shape != (size, size):
            arr = np.asarray(
                Image.fromarray(arr, mode="L").resize(
                    (size, size), resample=Image.Resampling.NEAREST
                )
            )
        return arr > 0

    return {
        "asphalt": _read("mask_road_area.png"),
        "tunnel": _read("mask_road_tunnel.png"),
        "bridge": _read("mask_road_bridge.png"),
        "junction": _read("mask_road_junction.png"),
    }


def fetch_suedtirol_road_areas(site: dict, *, force: bool = False) -> Path:
    cfg = road_areas_cfg(site)
    if cfg is None:
        raise SystemExit(
            "sources.road_areas missing or not authority suedtirol — "
            "Tirol stroke asphalt stays the default"
        )
    proc = processed_dir(site)
    proc.mkdir(parents=True, exist_ok=True)
    meta_p = proc / "road_areas.meta.json"
    asphalt_p = proc / "mask_road_area.png"
    if (
        asphalt_p.is_file()
        and meta_p.is_file()
        and not force
        and asphalt_p.stat().st_size > 100
    ):
        print(f"Using cached road-area masks: {asphalt_p}")
        return asphalt_p

    fc = fetch_geojson(site, cfg, force=force)
    masks = rasterize_masks(fc, site, cfg)
    Image.fromarray(masks["asphalt"].astype(np.uint8) * 255, mode="L").save(
        proc / "mask_road_area.png"
    )
    Image.fromarray(masks["tunnel"].astype(np.uint8) * 255, mode="L").save(
        proc / "mask_road_tunnel.png"
    )
    Image.fromarray(masks["bridge"].astype(np.uint8) * 255, mode="L").save(
        proc / "mask_road_bridge.png"
    )
    Image.fromarray(masks["junction"].astype(np.uint8) * 255, mode="L").save(
        proc / "mask_road_junction.png"
    )
    Image.fromarray(_preview(masks), mode="RGB").save(proc / "preview_road_areas.png")
    meta = {
        "authority": cfg["authority"],
        "source": {
            "type": "wfs",
            "url": cfg["url"],
            "type_name": cfg["type_name"],
            "version": cfg["version"],
            "srs": cfg["srs"],
            "geojson": str(raw_geojson_path(site).relative_to(ROOT)).replace("\\", "/"),
        },
        "crs": site.get("crs"),
        "bbox": list(map(float, site["bbox"])),
        "size": cfg["size"],
        "coverage_pct": {
            "asphalt": round(float(masks["asphalt"].mean() * 100), 3),
            "tunnel": round(float(masks["tunnel"].mean() * 100), 3),
            "bridge": round(float(masks["bridge"].mean() * 100), 3),
            "junction": round(float(masks["junction"].mean() * 100), 3),
        },
        "note": (
            "Asphalt from TrasRoadArea polygons (Südtirol only). "
            "LOC tunnel/bridge and PART junction are side masks; "
            "OSM remains the road axis / Kunstbauten primary."
        ),
    }
    meta_p.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {asphalt_p}")
    print(f"Wrote {meta_p}")
    print(f"coverage asphalt={meta['coverage_pct']['asphalt']}%")
    return asphalt_p


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    site = load_site()
    print(f"Site: {site_slug(site)}")
    fetch_suedtirol_road_areas(site, force=args.force)


if __name__ == "__main__":
    main()
