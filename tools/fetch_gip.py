"""Download Tirol Verkehrswege / GIP-like WFS for the site BBOX (cached).

Default: reuse cache if present and bbox/str_code match.
Re-download only with --force (service is large/slow).

Layers come from Landesstraßen WFS (Kunstbauten = Brücke, Galerie, …).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

import requests
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import load_site, processed_dir, site_slug  # noqa: E402

SITE = load_site()
RAW = ROOT / "data" / "raw"
PROC = processed_dir(SITE)
RAW.mkdir(parents=True, exist_ok=True)

CRS = str(SITE.get("crs", "EPSG:31254"))
BBOX = list(map(float, SITE["bbox"]))
XMIN, YMIN, XMAX, YMAX = BBOX
NAME = site_slug(SITE)

GIP = (SITE.get("sources") or {}).get("gip") or {}
WFS_URL = str(
    GIP.get(
        "url",
        "https://dservices3.arcgis.com/hG7UfxX49PQ8XkXh/arcgis/services/Verkehrswege/WFSServer",
    )
)
TYPE_NAME = str(GIP.get("type_name", "Verkehrswege:Verkehrswege"))
STR_CODE = GIP.get("str_code") or None
PAGE = int(GIP.get("page_size", 500))
TIMEOUT = int(GIP.get("timeout_s", 300))


def _cache_key() -> str:
    payload = {
        "url": WFS_URL,
        "type": TYPE_NAME,
        "crs": CRS,
        "bbox": BBOX,
        "str_code": STR_CODE,
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def _paths() -> tuple[Path, Path, Path]:
    key = _cache_key()
    stem = f"gip_{NAME}_{key}"
    return RAW / f"{stem}.geojson", RAW / f"{stem}.meta.json", PROC / "gip_structures.json"


def _filter_str_code(code: str) -> str:
    return f"""<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0">
  <fes:PropertyIsEqualTo>
    <fes:ValueReference>STR_CODE</fes:ValueReference>
    <fes:Literal>{code}</fes:Literal>
  </fes:PropertyIsEqualTo>
</fes:Filter>"""


def _fix_mojibake(obj):
    """Repair UTF-8 strings that were decoded as Latin-1 (common with some ArcGIS feeds)."""
    if isinstance(obj, str):
        try:
            fixed = obj.encode("latin-1").decode("utf-8")
            if fixed != obj and "Ã" not in fixed:
                return fixed
            # already looks broken with replacement markers from console — try anyway
            if any(ord(c) > 127 for c in obj) and "Ã" in obj:
                return obj.encode("latin-1").decode("utf-8")
            # Mugkögele pattern: ö as two chars already wrong; detect via known replacements
            return fixed if "Ã" in obj or "Â" in obj else obj
        except (UnicodeDecodeError, UnicodeEncodeError):
            return obj
    if isinstance(obj, list):
        return [_fix_mojibake(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _fix_mojibake(v) for k, v in obj.items()}
    return obj


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
    # Prefer raw UTF-8; then repair mojibake property strings if needed.
    try:
        data = json.loads(r.content.decode("utf-8"))
    except UnicodeDecodeError:
        data = r.json()
    data = _fix_mojibake(data)
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


def _clip_to_bbox(features: list[dict]) -> list[dict]:
    """GeoJSON from this WFS is EPSG:4326; clip using site CRS bbox."""
    to_site = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)
    kept = []
    for f in features:
        geom = f.get("geometry") or {}
        pts: list = []
        _coords_walk(geom.get("coordinates"), pts)
        hit = False
        for lon, lat, *rest in pts:
            x, y = to_site.transform(float(lon), float(lat))
            if XMIN <= x <= XMAX and YMIN <= y <= YMAX:
                hit = True
                break
        if hit:
            kept.append(f)
    return kept


def _structure_kind(props: dict) -> str | None:
    name = str(props.get("KUNSTBAUTEN") or "").strip()
    bez = str(props.get("OBJEKTBEZEICHNUNG") or "").lower()
    if not name or name.lower() == "none":
        return None
    if "galerie" in name.lower() or "tunnel" in bez:
        if "galerie" in name.lower():
            return "gallery"
        return "tunnel"
    if "brücke" in name.lower() or "bruecke" in name.lower() or "brücke" in bez or "bruecke" in bez:
        return "bridge"
    if "durchlass" in name.lower():
        return "culvert"
    return "structure"


def _summarize(features: list[dict]) -> dict:
    kb = Counter()
    kinds = Counter()
    structures = []
    for f in features:
        p = f.get("properties") or {}
        kb[str(p.get("KUNSTBAUTEN"))] += 1
        kind = _structure_kind(p)
        if kind:
            kinds[kind] += 1
            structures.append({
                "kind": kind,
                "name": p.get("KUNSTBAUTEN"),
                "objekt": p.get("OBJEKT"),
                "objektbezeichnung": p.get("OBJEKTBEZEICHNUNG"),
                "str_code": p.get("STR_CODE"),
                "strname": p.get("STRNAME"),
                "length_m": p.get("Shape__Length"),
                "objectid": p.get("OBJECTID"),
                "gml_id": p.get("GmlID"),
            })
    return {
        "feature_count": len(features),
        "kunstbauten_counts": dict(kb.most_common()),
        "kind_counts": dict(kinds),
        "structures": structures,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if a matching cache exists",
    )
    args = ap.parse_args()

    RAW.mkdir(parents=True, exist_ok=True)
    PROC.mkdir(parents=True, exist_ok=True)
    cache_path, meta_path, summary_path = _paths()

    if cache_path.exists() and meta_path.exists() and not args.force:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        print(f"Using cached GIP extract: {cache_path}")
        print(f"  features={meta.get('feature_count')} key={meta.get('cache_key')}")
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        summary = _summarize(data.get("features") or [])
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Wrote {summary_path}")
        for s in summary["structures"]:
            print(f"  {s['kind']:8s} {s['name']} ({s.get('length_m')} m)")
        return

    if not STR_CODE:
        raise SystemExit(
            "sources.gip.str_code is required for efficient download "
            "(full Tirol WFS is huge). Example: str_code: L13"
        )

    print(f"Downloading GIP/WFS STR_CODE={STR_CODE} from {WFS_URL}")
    print("(Spatial BBOX on this ArcGIS WFS returns empty — filter by road, then clip.)")
    all_feats = _fetch_by_str_code(str(STR_CODE))
    print(f"Road features total: {len(all_feats)}")
    clipped = _clip_to_bbox(all_feats)
    print(f"After site BBOX clip: {len(clipped)}")

    fc = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "features": clipped,
    }
    cache_path.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    summary = _summarize(clipped)
    meta = {
        "cache_key": _cache_key(),
        "url": WFS_URL,
        "type_name": TYPE_NAME,
        "str_code": STR_CODE,
        "crs_site": CRS,
        "bbox": BBOX,
        "feature_count": len(clipped),
        "road_feature_count": len(all_feats),
        "note": "Cached clip; re-run with --force to refresh",
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {cache_path}")
    print(f"Wrote {meta_path}")
    print(f"Wrote {summary_path}")
    print("Structures in BBOX:")
    for s in summary["structures"]:
        print(f"  {s['kind']:8s} {s['name']} ({s.get('length_m')} m)")


if __name__ == "__main__":
    main()
