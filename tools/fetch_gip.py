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


def _gip_cfg(site: dict | None = None) -> dict:
    return ((site or SITE).get("sources") or {}).get("gip") or {}


def gip_include_mode(site: dict | None = None) -> str:
    """``bbox`` = all Landesstraßen intersecting site.bbox; else trunk STR_CODE only."""
    inc = str(_gip_cfg(site).get("include") or "").strip().lower()
    if inc in ("bbox", "all", "all_in_bbox"):
        return "bbox"
    return "str_code"


def gip_str_codes(site: dict | None = None) -> list[str]:
    """Optional explicit STR_CODE list (fallback / extra WFS fetches)."""
    raw = _gip_cfg(site).get("str_codes")
    if raw is None:
        return []
    if isinstance(raw, (str, int, float)):
        s = str(raw).strip()
        return [s] if s else []
    out: list[str] = []
    for x in raw:
        s = str(x).strip()
        if s:
            out.append(s)
    return out


def gip_cache_key(site: dict | None = None) -> str:
    site = site or SITE
    gip = _gip_cfg(site)
    payload = {
        "url": str(gip.get("url") or WFS_URL),
        "type": str(gip.get("type_name") or TYPE_NAME),
        "crs": str(site.get("crs") or CRS),
        "bbox": list(map(float, site["bbox"])),
        "str_code": gip.get("str_code"),
        "include": gip_include_mode(site),
        "str_codes": gip_str_codes(site),
        # include:bbox downloads the full layer (local/forest/path), not STR_CODE-only.
        "netz": "all" if gip_include_mode(site) == "bbox" else "str_code",
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def gip_cache_geojson_path(site: dict | None = None) -> Path:
    site = site or SITE
    slug = site_slug(site)
    return ROOT / "data" / "raw" / f"gip_{slug}_{gip_cache_key(site)}.geojson"


def _cache_key() -> str:
    return gip_cache_key(SITE)


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


def _dedupe_by_objectid(features: list[dict]) -> list[dict]:
    by_oid: dict[int, dict] = {}
    no_oid: list[dict] = []
    for f in features:
        oid = (f.get("properties") or {}).get("OBJECTID")
        if oid is None:
            no_oid.append(f)
            continue
        by_oid[int(oid)] = f
    return list(by_oid.values()) + no_oid


def _fetch_by_str_codes(codes: list[str]) -> list[dict]:
    all_feats: list[dict] = []
    seen: set[str] = set()
    for code in codes:
        if not code or code in seen:
            continue
        seen.add(code)
        print(f"Downloading GIP/WFS STR_CODE={code} from {WFS_URL}")
        all_feats.extend(_fetch_by_str_code(code))
    return _dedupe_by_objectid(all_feats)


def _fs_headers() -> dict:
    return {"User-Agent": "beamng_autoroad/0.1", "Accept": "application/json"}


def _fetch_by_bbox() -> list[dict]:
    """All Verkehrswege intersecting site.bbox via FeatureServer (WFS bbox is empty)."""
    wkid = CRS.upper().replace("EPSG:", "").strip()
    try:
        wkid_i = int(wkid)
    except ValueError:
        wkid_i = wkid
    page = max(1, min(int(PAGE) if PAGE else 500, 2000))
    all_feats: list[dict] = []
    offset = 0
    print(f"Downloading GIP FeatureServer BBOX intersect {BBOX} inSR={wkid_i}")
    while True:
        params = {
            "where": "1=1",
            "geometry": json.dumps(
                {
                    "xmin": XMIN,
                    "ymin": YMIN,
                    "xmax": XMAX,
                    "ymax": YMAX,
                    "spatialReference": {"wkid": wkid_i},
                }
            ),
            "geometryType": "esriGeometryEnvelope",
            "inSR": wkid_i,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": 4326,
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": page,
        }
        r = requests.get(FS_QUERY, params=params, headers=_fs_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        data = _fix_mojibake(r.json())
        if data.get("error"):
            raise RuntimeError(f"FeatureServer bbox error: {data.get('error')}")
        feats = data.get("features") or []
        print(f"  FeatureServer offset={offset} -> {len(feats)}")
        all_feats.extend(feats)
        exceeded = bool(
            data.get("exceededTransferLimit")
            or (data.get("properties") or {}).get("exceededTransferLimit")
        )
        if not feats or (len(feats) < page and not exceeded):
            break
        offset += len(feats)
        if offset > 200_000:
            print("WARNING: FeatureServer bbox paging stopped at 200000 features")
            break
    print(f"  FeatureServer bbox total: {len(all_feats)}")
    return all_feats


def _has_str_code(feat: dict) -> bool:
    return bool(str((feat.get("properties") or {}).get("STR_CODE") or "").strip())


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
    # Batch OR query
    where = " OR ".join(f"OBJECTID={int(o)}" for o in oids)
    params = {
        "where": where,
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    r = requests.get(FS_QUERY, params=params, headers=_fs_headers(), timeout=TIMEOUT)
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
        print(f"  gip_extra: OBJECTID={oid} -> {label} ({kind})")

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
    str_codes = Counter(
        str((f.get("properties") or {}).get("STR_CODE") or "") for f in features
    )
    return {
        "feature_count": len(features),
        "trunk_str_code": STR_CODE,
        "include": gip_include_mode(),
        "str_code_counts": dict(str_codes.most_common()),
        "kunstbauten_counts": dict(kb.most_common()),
        "kind_counts": dict(kinds),
        "structures": structures,
        "side_structure_count": sum(
            1
            for s in structures
            if str(s.get("str_code") or "").strip() != str(STR_CODE or "").strip()
        ),
    }


def _print_inventory(summary: dict) -> None:
    counts = summary.get("str_code_counts") or {}
    if counts:
        bits = [f"{k or '(none)'}={v}" for k, v in counts.items()]
        print(f"STR_CODE counts: {' '.join(bits)}")
    trunk = str(summary.get("trunk_str_code") or "").strip()
    n_side = int(summary.get("side_structure_count") or 0)
    print(f"Structures in BBOX (trunk={trunk or '?'} side_kunstbauten={n_side}):")
    for s in summary.get("structures") or []:
        code = s.get("str_code") or ""
        mark = "" if str(code).strip() == trunk else " [side]"
        print(f"  {s['kind']:8s} {s['name']} ({s.get('length_m')} m) {code}{mark}")


def _download_features() -> tuple[list[dict], int]:
    """Return (clipped features before gip_extra, unclipped count)."""
    include = gip_include_mode()
    extra_codes = gip_str_codes()
    all_feats: list[dict] = []

    if include == "bbox":
        try:
            all_feats = _fetch_by_bbox()
        except Exception as ex:
            print(f"WARNING: FeatureServer bbox failed ({ex})")
            all_feats = []
        if not all_feats:
            fallback = list(dict.fromkeys(extra_codes + ([str(STR_CODE)] if STR_CODE else [])))
            if not fallback:
                raise SystemExit(
                    "include: bbox returned 0 features and no str_code/str_codes fallback"
                )
            print(f"Falling back to WFS STR_CODE(s): {fallback}")
            all_feats = _fetch_by_str_codes(fallback)
    elif extra_codes:
        codes = list(dict.fromkeys(([str(STR_CODE)] if STR_CODE else []) + extra_codes))
        print("(Spatial BBOX on this ArcGIS WFS returns empty — filter by road, then clip.)")
        all_feats = _fetch_by_str_codes(codes)
    else:
        print(f"Downloading GIP/WFS STR_CODE={STR_CODE} from {WFS_URL}")
        print("(Spatial BBOX on this ArcGIS WFS returns empty — filter by road, then clip.)")
        all_feats = _fetch_by_str_code(str(STR_CODE))

    print(f"Road features total: {len(all_feats)}")
    clipped = _clip_to_bbox(all_feats)
    print(f"After site BBOX clip: {len(clipped)}")
    return clipped, len(all_feats)


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
        _print_inventory(summary)
        return

    if not STR_CODE:
        raise SystemExit(
            "sources.gip.str_code is required as the through-route (trunk). "
            "Example: str_code: L13  — with include: bbox for all roads in site.bbox"
        )

    clipped, n_raw = _download_features()
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
        "include": gip_include_mode(),
        "str_codes": gip_str_codes(),
        "crs_site": CRS,
        "bbox": BBOX,
        "feature_count": len(clipped),
        "road_feature_count": n_raw,
        "gip_extra": _gip_extra_entries(),
        "note": "Cached clip; re-run with --force to refresh road extract; gip_extra merged each run",
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {cache_path}")
    print(f"Wrote {meta_path}")
    print(f"Wrote {summary_path}")
    _print_inventory(summary)


if __name__ == "__main__":
    main()
