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
# FeatureServer for OBJECTID lookups (Kunstbauten-Nachträge außerhalb STR_CODE).
FS_QUERY = str(
    GIP.get(
        "feature_server_query",
        "https://services3.arcgis.com/hG7UfxX49PQ8XkXh/arcgis/rest/services/Verkehrswege/FeatureServer/0/query",
    )
)

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
    """Classify Kunstbaute; honors YAML gip_extra override when present."""
    extra = str(props.get("_autoroad_gip_extra_kind") or "").strip().lower()
    if extra in {"bridge", "gallery", "tunnel", "culvert", "structure"}:
        return extra
    name = str(props.get("KUNSTBAUTEN") or "").strip()
    bez = str(props.get("OBJEKTBEZEICHNUNG") or "").lower()
    if not name or name.lower() == "none":
        return None
    nl = name.lower()
    if "galerie" in nl:
        return "gallery"
    if "tunnel" in nl or "unterführung" in nl or "unterfuehrung" in nl or "tunnel" in bez:
        return "tunnel"
    if "brücke" in nl or "bruecke" in nl or "brücke" in bez or "bruecke" in bez:
        return "bridge"
    if "durchlass" in nl:
        return "culvert"
    return "structure"


def _gip_extra_entries(site: dict | None = None) -> list[dict]:
    """Manual Kunstbauten Nachträge from site YAML.

    beamng.bridges.gip_extra:
      - objectid: 3992
        # name: optional (default Brücke {oid} / Tunnel {oid} / …)
        # kind: bridge | tunnel | gallery (default bridge)
    """
    site = site or SITE
    raw = ((site.get("beamng") or {}).get("bridges") or {}).get("gip_extra") or []
    out: list[dict] = []
    for i, entry in enumerate(raw):
        if isinstance(entry, (int, float, str)) and str(entry).strip().isdigit():
            out.append({"objectid": int(entry), "kind": "bridge", "name": None})
            continue
        if not isinstance(entry, dict):
            continue
        oid = entry.get("objectid")
        if oid is None:
            continue
        out.append({
            "objectid": int(oid),
            "kind": str(entry.get("kind") or "bridge").lower(),
            "name": entry.get("name"),
        })
    return out


def _fetch_features_by_objectids(oids: list[int]) -> list[dict]:
    """Pull individual Verkehrswege features via FeatureServer (any STR_CODE)."""
    if not oids:
        return []
    headers = {"User-Agent": "beamng_autoroad/0.1", "Accept": "application/json"}
    # Batch OR query
    where = " OR ".join(f"OBJECTID={int(o)}" for o in oids)
    params = {
        "where": where,
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    r = requests.get(FS_QUERY, params=params, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    data = _fix_mojibake(r.json())
    feats = data.get("features") or []
    print(f"  FeatureServer OBJECTID fetch: asked={len(oids)} got={len(feats)}")
    return feats


def _apply_gip_extra(features: list[dict], site: dict | None = None) -> list[dict]:
    """Merge YAML gip_extra OBJECTIDs; label Brücke/Tunnel/{oid} when unnamed."""
    extras = _gip_extra_entries(site)
    if not extras:
        return features

    by_oid: dict[int, dict] = {}
    for f in features:
        p = f.get("properties") or {}
        oid = p.get("OBJECTID")
        if oid is not None:
            by_oid[int(oid)] = f

    missing = [e["objectid"] for e in extras if e["objectid"] not in by_oid]
    if missing:
        for f in _fetch_features_by_objectids(missing):
            p = f.get("properties") or {}
            oid = p.get("OBJECTID")
            if oid is not None:
                by_oid[int(oid)] = f

    merged = list(features)
    present = {
        int((f.get("properties") or {}).get("OBJECTID"))
        for f in merged
        if (f.get("properties") or {}).get("OBJECTID") is not None
    }

    for e in extras:
        oid = e["objectid"]
        f = by_oid.get(oid)
        if f is None:
            print(f"  WARNING: gip_extra OBJECTID={oid} not found on FeatureServer")
            continue
        f = json.loads(json.dumps(f))  # deep copy
        p = dict(f.get("properties") or {})
        kind = e["kind"]
        if e.get("name"):
            label = str(e["name"])
        elif kind == "bridge":
            label = f"Brücke {oid}"
        elif kind == "gallery":
            label = f"Galerie {oid}"
        elif kind == "tunnel":
            label = f"Tunnel {oid}"
        else:
            label = f"{kind} {oid}"
        p["KUNSTBAUTEN"] = label
        p["_autoroad_gip_extra"] = True
        p["_autoroad_gip_extra_kind"] = kind
        f["properties"] = p
        if oid in present:
            merged = [
                f if int((x.get("properties") or {}).get("OBJECTID") or -1) == oid else x
                for x in merged
            ]
        else:
            merged.append(f)
            present.add(oid)
        print(f"  gip_extra: OBJECTID={oid} → {label} ({kind})")

    return merged


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
        feats = _apply_gip_extra(data.get("features") or [])
        data["features"] = feats
        # Persist merge so build_bridges sees extras without re-fetch logic
        cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        summary = _summarize(feats)
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
    clipped = _apply_gip_extra(clipped)

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
        "gip_extra": _gip_extra_entries(),
        "note": "Cached clip; re-run with --force to refresh road extract; gip_extra merged each run",
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
