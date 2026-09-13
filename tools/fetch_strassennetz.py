"""Download Tirol Strassennetz WFS (Landesstraßen B/L Achse) for the site.

Same pattern as fetch_gip: filter by STR_CODE, clip to site BBOX, cache under data/raw/.
Also writes processed/strassennetz_beamng.json (BeamNG XY + DGM Z) for span profiling.

Usage:
  $env:AUTOROAD_SITE='config/sites/l13_splining.yaml'
  python tools/fetch_strassennetz.py
  python tools/fetch_strassennetz.py --force
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np
import requests
from PIL import Image
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import SiteCoords, load_site, processed_dir, site_slug  # noqa: E402

SITE = load_site()
RAW = ROOT / "data" / "raw"
PROC = processed_dir(SITE)
RAW.mkdir(parents=True, exist_ok=True)

CRS = str(SITE.get("crs", "EPSG:31254"))
BBOX = list(map(float, SITE["bbox"]))
XMIN, YMIN, XMAX, YMAX = BBOX
NAME = site_slug(SITE)

SRC = (SITE.get("sources") or {}).get("strassennetz") or (SITE.get("sources") or {}).get("roads") or {}
WFS_URL = str(
    SRC.get(
        "url",
        "https://dservices3.arcgis.com/hG7UfxX49PQ8XkXh/arcgis/services/Strassennetz/WFSServer",
    )
)
TYPE_NAME = str(SRC.get("type_name", "Strassennetz:Strassennetz"))
STR_CODE = SRC.get("str_code") or ((SITE.get("sources") or {}).get("gip") or {}).get("str_code")
PAGE = int(SRC.get("page_size", 500))
TIMEOUT = int(SRC.get("timeout_s", 300))


def _cache_key() -> str:
    payload = {"url": WFS_URL, "type": TYPE_NAME, "crs": CRS, "bbox": BBOX, "str_code": STR_CODE}
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def _paths() -> tuple[Path, Path, Path]:
    key = _cache_key()
    stem = f"strassennetz_{NAME}_{key}"
    return RAW / f"{stem}.geojson", RAW / f"{stem}.meta.json", PROC / "strassennetz_beamng.json"


def _fix_mojibake(obj):
    if isinstance(obj, str):
        try:
            fixed = obj.encode("latin-1").decode("utf-8")
            if fixed != obj and "Ã" not in fixed:
                return fixed
            if "Ã" in obj or "Â" in obj:
                return obj.encode("latin-1").decode("utf-8")
            return fixed if "Ã" in obj else obj
        except (UnicodeDecodeError, UnicodeEncodeError):
            return obj
    if isinstance(obj, list):
        return [_fix_mojibake(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _fix_mojibake(v) for k, v in obj.items()}
    return obj


def _filter_str_code(code: str) -> str:
    return f"""<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0">
  <fes:PropertyIsEqualTo>
    <fes:ValueReference>STR_CODE</fes:ValueReference>
    <fes:Literal>{code}</fes:Literal>
  </fes:PropertyIsEqualTo>
</fes:Filter>"""


def _get_page(start: int, count: int, filter_xml: str | None) -> list[dict]:
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": TYPE_NAME,
        "outputFormat": "GEOJSON",
        "count": count,
        "startIndex": start,
    }
    if filter_xml:
        params["FILTER"] = filter_xml
    r = requests.get(WFS_URL, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    data = _fix_mojibake(json.loads(r.content.decode("utf-8")))
    return data.get("features") or []


def _fetch_by_str_code(code: str) -> list[dict]:
    filt = _filter_str_code(code)
    all_feats: list[dict] = []
    start = 0
    while True:
        feats = _get_page(start, PAGE, filt)
        print(f"  page startIndex={start} -> {len(feats)}")
        if not feats:
            break
        all_feats.extend(feats)
        if len(feats) < PAGE:
            break
        start += PAGE
    return all_feats


def _coords_walk(obj, out: list) -> None:
    if obj is None:
        return
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], (int, float)):
            out.append(obj)
            return
        for x in obj:
            _coords_walk(x, out)


def _clip_features_to_bbox(features: list[dict]) -> list[dict]:
    """Keep features that intersect site BBOX (GeoJSON lon/lat)."""
    to_site = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)
    kept = []
    for f in features:
        pts: list = []
        _coords_walk((f.get("geometry") or {}).get("coordinates"), pts)
        if any(
            XMIN <= (xy := to_site.transform(float(p[0]), float(p[1])))[0] <= XMAX
            and YMIN <= xy[1] <= YMAX
            for p in pts
            if len(p) >= 2
        ):
            kept.append(f)
    return kept


def _load_z_at(site: dict):
    size = int((site.get("beamng") or {}).get("mask_size") or 512)
    hm_path = PROC / f"heightmap_{size}.png"
    meta_path = PROC / "heightmap_meta.json"
    if not hm_path.is_file() or not meta_path.is_file():
        return None, None
    hm = np.asarray(Image.open(hm_path))
    max_h = float(json.loads(meta_path.read_text(encoding="utf-8"))["max_height_m"])
    n = int(hm.shape[0])

    def z_at(bx: float, by: float) -> float:
        c = int(max(0, min(n - 1, round(bx))))
        r = int(max(0, min(n - 1, round(n - 1 - by))))
        return float(hm[r, c]) / 65535.0 * max_h

    return z_at, max_h


def write_beamng_roads(features: list[dict], site: dict, out_path: Path) -> dict:
    """Clip centerline to BBOX, convert to BeamNG XY, sample DGM Z."""
    sc = SiteCoords(site)
    to_site = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)
    z_at, _max_h = _load_z_at(site)
    width_default = float((site.get("beamng") or {}).get("bridges", {}).get("defaults", {}).get("width_m") or 7.0)

    roads = {}
    rid = 0
    for f in features:
        props = f.get("properties") or {}
        pts_ll: list = []
        _coords_walk((f.get("geometry") or {}).get("coordinates"), pts_ll)
        xy_site: list[tuple[float, float]] = []
        for p in pts_ll:
            if len(p) < 2:
                continue
            x, y = to_site.transform(float(p[0]), float(p[1]))
            xy_site.append((x, y))

        # Clip to bbox (keep contiguous runs)
        pieces: list[list[tuple[float, float]]] = []
        cur: list[tuple[float, float]] = []
        for x, y in xy_site:
            if XMIN <= x <= XMAX and YMIN <= y <= YMAX:
                cur.append((x, y))
            elif cur:
                if len(cur) >= 2:
                    pieces.append(cur)
                cur = []
        if len(cur) >= 2:
            pieces.append(cur)

        for piece in pieces:
            nodes = []
            for x, y in piece:
                bx, by = sc.crs_to_beamng(x, y)
                z = float(z_at(bx, by)) if z_at else 0.0
                nodes.append([round(bx, 3), round(by, 3), round(z, 3), round(width_default, 2)])
            if len(nodes) < 2:
                continue
            length = 0.0
            for a, b in zip(nodes, nodes[1:]):
                length += math.hypot(b[0] - a[0], b[1] - a[1])
            roads[str(rid)] = {
                "name": props.get("NAME") or props.get("STR_CODE") or "strassennetz",
                "highway": "primary",
                "str_code": props.get("STR_CODE"),
                "objectid": props.get("OBJECTID"),
                "source": "strassennetz",
                "length_m": round(length, 2),
                "nodes": nodes,
            }
            rid += 1

    out_path.write_text(json.dumps(roads, indent=2), encoding="utf-8")
    return {"roads": len(roads), "path": str(out_path)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if not STR_CODE:
        raise SystemExit("sources.strassennetz.str_code or sources.gip.str_code required")

    cache_path, meta_path, beamng_path = _paths()
    if cache_path.exists() and meta_path.exists() and not args.force:
        print(f"Using cached Strassennetz: {cache_path}")
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        info = write_beamng_roads(data.get("features") or [], SITE, beamng_path)
        print(f"Wrote {beamng_path} ({info['roads']} pieces)")
        return

    print(f"Downloading Strassennetz STR_CODE={STR_CODE} from {WFS_URL}")
    all_feats = _fetch_by_str_code(str(STR_CODE))
    print(f"Road features total: {len(all_feats)}")
    clipped = _clip_features_to_bbox(all_feats)
    print(f"After site BBOX intersect: {len(clipped)}")

    fc = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "features": clipped,
    }
    cache_path.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    meta = {
        "cache_key": _cache_key(),
        "url": WFS_URL,
        "type_name": TYPE_NAME,
        "str_code": STR_CODE,
        "crs_site": CRS,
        "bbox": BBOX,
        "feature_count": len(clipped),
        "road_feature_count": len(all_feats),
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    info = write_beamng_roads(clipped, SITE, beamng_path)
    print(f"Wrote {cache_path}")
    print(f"Wrote {meta_path}")
    print(f"Wrote {beamng_path} ({info['roads']} pieces)")


if __name__ == "__main__":
    main()
