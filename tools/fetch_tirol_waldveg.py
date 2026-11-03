"""Rasterize Tirol Waldvegetation (Schiechtl/Stern) onto the site mask.

Writes mask_waldveg.png (pixel value = type id, 0 = no polygon) and
waldveg_codes.json next to the other forest masks. Existing files are kept.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry import box, shape
from shapely.validation import make_valid

from site_coords import processed_dir

MASK_NAME = "mask_waldveg.png"
CODES_NAME = "waldveg_codes.json"


def _post(layer_query: str, params: dict, tries: int = 4) -> dict:
    body = urllib.parse.urlencode(params).encode()
    last = None
    for i in range(tries):
        req = urllib.request.Request(
            layer_query, data=body, headers={"User-Agent": "beamng-autoroad"}
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as exc:
            last = exc
            print(f"waldveg retry {i}: {type(exc).__name__}")
            continue
    else:
        raise SystemExit(last)
    if "error" in data:
        raise SystemExit(json.dumps(data["error"], ensure_ascii=False)[:2000])
    return data


def _polygons(geom):
    if geom.is_empty:
        return
    kind = geom.geom_type
    if kind == "Polygon":
        yield geom
    elif kind in ("MultiPolygon", "GeometryCollection"):
        for part in geom.geoms:
            yield from _polygons(part)


def _burn(draw, geom, fill: int, to_px) -> None:
    for poly in _polygons(geom):
        draw.polygon([to_px(x, y) for x, y in poly.exterior.coords], fill=fill)
        for hole in poly.interiors:
            draw.polygon([to_px(x, y) for x, y in hole.coords], fill=0)


def ensure_waldveg_mask(site: dict, url: str) -> Path:
    """Download and rasterize when the mask is missing. Return the PNG path."""
    proc = processed_dir(site)
    mask_path = proc / MASK_NAME
    codes_path = proc / CODES_NAME
    if mask_path.is_file() and codes_path.is_file():
        return mask_path

    bng = site.get("beamng") or {}
    size = int(bng.get("mask_size") or 2048)
    xmin, ymin, xmax, ymax = (float(v) for v in site["bbox"])
    clip = box(xmin, ymin, xmax, ymax)
    query = url.rstrip("/") + "/query"
    ids = _post(
        query,
        {
            "where": "1=1",
            "geometry": f"{xmin},{ymin},{xmax},{ymax}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "31254",
            "spatialRel": "esriSpatialRelIntersects",
            "returnIdsOnly": "true",
            "f": "json",
        },
    )
    object_ids = ids.get("objectIds") or []
    print(f"waldveg polygons touching bbox: {len(object_ids)}")
    feats = []
    for start in range(0, len(object_ids), 200):
        chunk = object_ids[start : start + 200]
        data = _post(
            query,
            {
                "objectIds": ",".join(str(i) for i in chunk),
                "outSR": "31254",
                "outFields": "OBJEKT,OBJEKTBEZEICHNUNG",
                "returnGeometry": "true",
                "f": "geojson",
                "maxAllowableOffset": "15",
            },
        )
        feats.extend(data.get("features") or [])
        print(f"waldveg fetched {len(feats)}")

    areas: dict[str, float] = defaultdict(float)
    names: dict[str, str] = {}
    pieces: dict[str, list] = defaultdict(list)
    for feat in feats:
        props = feat.get("properties") or {}
        code = str(props.get("OBJEKT") or "").strip()
        if not code:
            continue
        names[code] = str(props.get("OBJEKTBEZEICHNUNG") or "")
        geom = make_valid(shape(feat["geometry"]))
        piece = geom.intersection(clip)
        if piece.is_empty:
            continue
        areas[code] += float(piece.area)
        pieces[code].append(piece)
    if not areas:
        raise SystemExit("Waldvegetation returned no polygons inside the site bbox")

    # Small stands paint over large ones where two polygons overlap.
    codes = [code for code, _area in sorted(areas.items(), key=lambda kv: -kv[1])]
    ids_by_code = {code: i + 1 for i, code in enumerate(sorted(areas))}

    def to_px(x: float, y: float) -> tuple[float, float]:
        px = (x - xmin) / (xmax - xmin) * (size - 1)
        py = (1.0 - (y - ymin) / (ymax - ymin)) * (size - 1)
        return px, py

    canvas = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(canvas)
    for code in codes:
        fill = ids_by_code[code]
        for piece in pieces[code]:
            _burn(draw, piece, fill, to_px)
    canvas.save(mask_path)
    codes_path.write_text(
        json.dumps(
            {"ids": ids_by_code, "names": names, "area_m2": areas},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    raster = np.asarray(canvas, dtype=np.uint8)
    print(f"Wrote {mask_path} ({int((raster > 0).sum()) / 1e6:.2f} km2)")
    for code in sorted(areas, key=lambda c: -areas[c]):
        print(f"  {code:6} {areas[code] / 1e6:8.3f} km2  {names.get(code, '')}")
    return mask_path
