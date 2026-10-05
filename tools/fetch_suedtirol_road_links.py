"""Fetch Südtirol TransportNetwork road axes (LinksSequences-Rendering).

Authority-scoped: only when ``sources.road_links`` has ``authority: suedtirol``.
Tirol / GIP / OSM sites without that block are unchanged.

WFS: ``p_bz-TransportNetwork:LinksSequences-Rendering`` (Provinz Bozen only —
not a Italy-wide endpoint).

Writes:
  data/raw/road_links_suedtirol_<site>.geojson
  data/processed/<site>/suedtirol_roads_beamng.json
  data/processed/<site>/road_links.meta.json
  data/processed/<site>/preview_road_links.png

Usage:
  cd C:\\temp\\beamng_autoroad
  $env:AUTOROAD_SITE = \"config/sites/timmelsjoch.yaml\"
  python tools\\fetch_suedtirol_road_links.py --force
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from road_width import resolve_road_width_m  # noqa: E402
from site_coords import load_site, processed_dir, site_slug  # noqa: E402

DEFAULT_URL = "https://geoservices1.civis.bz.it/geoserver/ows"
DEFAULT_TYPE = "p_bz-TransportNetwork:LinksSequences-Rendering"
DEFAULT_VERSION = "2.0.0"
PAGE = 500
HEADERS = {"User-Agent": "beamng_autoroad/0.1 (road_links)", "Accept": "*/*"}

# LOCATION codes seen on Timmelsjoch probe.
LOC_BRIDGE = {1}
LOC_TUNNEL = {4}

# CLASS → OSM-like highway for width defaults / MeshRoad filters later.
_CLASS_HIGHWAY = {
    1: "secondary",  # transregional (SS …)
    8: "unclassified",
    10: "path",
    11: "track",
    106: "track",
    300: "path",
}


def road_links_cfg(site: dict) -> dict | None:
    """Return road_links config, or None if this site does not use Südtirol links."""
    src = (site.get("sources") or {}).get("road_links")
    if not isinstance(src, dict) or not src:
        return None
    auth = str(src.get("authority") or src.get("catalog") or "").lower()
    if auth not in {"suedtirol", "alto_adige", "bz"}:
        return None
    bng = site.get("beamng") or {}
    return {
        "authority": "suedtirol",
        "url": str(src.get("url") or DEFAULT_URL),
        "type_name": str(src.get("type_name") or src.get("typename") or DEFAULT_TYPE),
        "version": str(src.get("version") or DEFAULT_VERSION),
        "srs": str(src.get("srs") or src.get("crs") or site.get("crs") or "EPSG:25832"),
        "timeout_s": int(src.get("timeout_s") or 120),
        "page": int(src.get("page") or PAGE),
        "main_only": bool(src.get("main_only", False)),
        "size": int(src.get("size") or bng.get("mask_size") or 2048),
    }


def raw_geojson_path(site: dict) -> Path:
    return ROOT / "data" / "raw" / f"road_links_suedtirol_{site_slug(site)}.geojson"


def processed_roads_path(site: dict) -> Path:
    return processed_dir(site) / "suedtirol_roads_beamng.json"


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
        print(f"Using cached road-links GeoJSON: {out}")
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
        print(f"LinksSequences GetFeature startIndex={start} …")
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
            raise SystemExit("LinksSequences pagination exceeded 200k — abort")

    fc = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": srs}},
        "features": features,
    }
    out.write_text(json.dumps(fc), encoding="utf-8")
    print(f"Wrote {out} features={len(features)}")
    return fc


def _line_parts(geom: dict) -> list[list[tuple[float, float]]]:
    gtype = (geom or {}).get("type")
    coords = (geom or {}).get("coordinates")
    parts: list[list[tuple[float, float]]] = []
    if gtype == "LineString" and coords:
        parts.append([(float(c[0]), float(c[1])) for c in coords if len(c) >= 2])
    elif gtype == "MultiLineString" and coords:
        for line in coords:
            parts.append([(float(c[0]), float(c[1])) for c in line if len(c) >= 2])
    return [p for p in parts if len(p) >= 2]


def _highway_for(props: dict) -> str:
    try:
        klass = int(float(props.get("CLASS")))
    except (TypeError, ValueError):
        klass = -1
    if klass in _CLASS_HIGHWAY:
        return _CLASS_HIGHWAY[klass]
    if int(props.get("IS_MAIN_ROAD") or 0) == 1:
        return "secondary"
    return "track"


def _kunstbauten(props: dict) -> str:
    try:
        loc = int(float(props.get("LOCATION")))
    except (TypeError, ValueError):
        return ""
    if loc in LOC_BRIDGE:
        return "Brücke"
    if loc in LOC_TUNNEL:
        return "Tunnel"
    return ""


def span_objectids(site: dict | None = None) -> set[int]:
    """Site YAML ``beamng.roads.span_objectids`` — treat as tunnel/bridge omit."""
    from site_coords import load_site

    site = site or load_site()
    raw = ((site.get("beamng") or {}).get("roads") or {}).get("span_objectids") or []
    out: set[int] = set()
    for x in raw:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out


def axis_is_structure_span(road: dict, site: dict | None = None) -> bool:
    """True for tunnel/bridge axis pieces that must not drape as normal road.

    Uses Südtirol LinksSequences ``kunstbauten`` / ``location`` (and the same
    fields if ever set on OSM/GIP rows). Galleries that are closed bores are
    expected as Tunnel-like tags until a dedicated gallery feed exists.
    Site ``beamng.roads.span_objectids`` forces omit (e.g. ground LOCATION that
    still runs through a span and must not become asphalt mesh).
    """
    oid = road.get("objectid")
    if oid is not None:
        try:
            if int(oid) in span_objectids(site):
                return True
        except (TypeError, ValueError):
            pass
    kb = str(road.get("kunstbauten") or "").lower()
    if any(s in kb for s in ("tunnel", "brücke", "bruecke", "bridge", "galerie", "gallery")):
        return True
    loc = road.get("location")
    if loc is None:
        return False
    try:
        loc_i = int(float(loc))
    except (TypeError, ValueError):
        return False
    # Probe: LOCATION 1 = Brücke, 4 = Tunnel
    return loc_i in {1, 4}


def _str_code(props: dict, highway: str) -> str:
    """Synthetic code so build_asphalt_meshroads `_is_main_road` can match later."""
    cat = str(props.get("NAME_CATEGORY") or props.get("ACRONYM") or "").strip().upper()
    if cat in {"SS", "SR", "SP", "A"}:
        return f"{cat}1"
    if int(props.get("IS_MAIN_ROAD") or 0) == 1 or highway in {
        "motorway",
        "trunk",
        "primary",
        "secondary",
    }:
        return "L1"
    return ""


def features_to_roads(fc: dict, site: dict, cfg: dict) -> list[dict]:
    """Convert WFS features to smoke-style road dicts (BeamNG XY, Z=None)."""
    xmin, ymin, xmax, ymax = (float(v) for v in site["bbox"])
    bng = site.get("beamng") or {}
    main_only = bool(cfg.get("main_only"))
    roads: list[dict] = []
    n_skip = 0
    for feat in fc.get("features") or []:
        props = feat.get("properties") or {}
        if main_only and int(props.get("IS_MAIN_ROAD") or 0) != 1:
            n_skip += 1
            continue
        parts = _line_parts(feat.get("geometry") or {})
        if not parts:
            n_skip += 1
            continue
        highway = _highway_for(props)
        width, lanes, wsrc = resolve_road_width_m(tags={}, highway=highway, bng=bng)
        name = (
            str(props.get("NAME") or "").strip()
            or str(props.get("NAME_D") or "").strip()
            or str(props.get("NAME_I") or "").strip()
        )
        oid = props.get("ID")
        try:
            oid_i = int(oid) if oid is not None else None
        except (TypeError, ValueError):
            oid_i = None
        kb = _kunstbauten(props)
        code = _str_code(props, highway)
        for pi, part in enumerate(parts):
            nodes: list[list] = []
            for x, y in part:
                lx = x - xmin
                ly = y - ymin
                if lx < -50 or ly < -50 or lx > (xmax - xmin) + 50 or ly > (ymax - ymin) + 50:
                    continue
                nodes.append([lx, ly, None, width])
            if len(nodes) < 2:
                n_skip += 1
                continue
            rid = f"{oid_i}_{pi}" if oid_i is not None else f"seg_{len(roads)}"
            roads.append(
                {
                    "id": rid,
                    "objectid": oid_i if oid_i is not None else rid,
                    "highway": highway,
                    "name": name,
                    "lanes": lanes,
                    "width_source": wsrc,
                    "str_code": code,
                    "objekt": "S-A" if code else "",
                    "kunstbauten": kb,
                    "is_main_road": int(props.get("IS_MAIN_ROAD") or 0) == 1,
                    "class": props.get("CLASS"),
                    "form": props.get("FORM"),
                    "surface": props.get("SURFACE"),
                    "location": props.get("LOCATION"),
                    "source": "suedtirol_links",
                    "nodes": nodes,
                }
            )
    print(f"Links → roads={len(roads)} skipped={n_skip} main_only={main_only}")
    return roads


def roads_to_json(roads: list[dict]) -> dict:
    payload: dict[str, dict] = {}
    for i, road in enumerate(roads):
        entry = {
            "nodes": road["nodes"],
            "highway": road["highway"],
            "name": road["name"],
            "objectid": road.get("objectid"),
            "osm_id": road.get("objectid"),
            "str_code": road.get("str_code") or "",
            "objekt": road.get("objekt") or "",
            "kunstbauten": road.get("kunstbauten") or "",
            "source": "suedtirol_links",
            "width_source": road.get("width_source"),
        }
        if road.get("lanes") is not None:
            entry["lanes"] = road["lanes"]
        if road.get("is_main_road") is not None:
            entry["is_main_road"] = road["is_main_road"]
        for key in ("class", "form", "surface", "location"):
            if road.get(key) is not None:
                entry[key] = road[key]
        payload[str(i)] = entry
    return payload


def _preview(roads: list[dict], site: dict, size: int) -> np.ndarray:
    xmin, ymin, xmax, ymax = (float(v) for v in site["bbox"])
    bw = max(xmax - xmin, 1.0)
    bh = max(ymax - ymin, 1.0)
    rgb = np.zeros((size, size, 3), dtype=np.uint8)
    rgb[:] = (25, 25, 30)
    img = Image.fromarray(rgb, mode="RGB")
    draw = ImageDraw.Draw(img)

    def to_px(lx: float, ly: float) -> tuple[float, float]:
        # BeamNG local → pixel (y north, image row 0 = north)
        px = lx / bw * (size - 1)
        py = (1.0 - ly / bh) * (size - 1)
        return px, py

    for road in roads:
        nodes = road.get("nodes") or []
        if len(nodes) < 2:
            continue
        pts = [to_px(float(n[0]), float(n[1])) for n in nodes]
        kb = str(road.get("kunstbauten") or "")
        if kb.startswith("Tunnel"):
            color = (160, 60, 200)
        elif kb.startswith("Brücke"):
            color = (80, 140, 220)
        elif road.get("is_main_road"):
            color = (220, 220, 230)
        else:
            color = (90, 90, 70)
        draw.line(pts, fill=color, width=2 if road.get("is_main_road") else 1)
    return np.asarray(img)


def load_suedtirol_roads_for_smoke(site: dict, *, force: bool = False) -> list[dict]:
    """Ensure cache exists; return smoke-style roads (Z still None)."""
    cfg = road_links_cfg(site)
    if cfg is None:
        raise SystemExit(
            "sources.road_links missing or not authority suedtirol — "
            "Tirol OSM/GIP axis stays the default"
        )
    proc = processed_dir(site)
    out = processed_roads_path(site)
    if out.is_file() and out.stat().st_size > 50 and not force:
        data = json.loads(out.read_text(encoding="utf-8"))
        roads = []
        for key, road in data.items():
            nodes = road.get("nodes") or []
            if len(nodes) < 2:
                continue
            roads.append(
                {
                    "id": road.get("objectid") or key,
                    "objectid": road.get("objectid"),
                    "highway": road.get("highway") or "secondary",
                    "name": road.get("name") or "",
                    "lanes": road.get("lanes"),
                    "width_source": road.get("width_source"),
                    "str_code": road.get("str_code") or "",
                    "objekt": road.get("objekt") or "",
                    "kunstbauten": road.get("kunstbauten") or "",
                    "is_main_road": road.get("is_main_road"),
                    "location": road.get("location"),
                    "source": "suedtirol_links",
                    "nodes": nodes,
                }
            )
        print(f"Using cached Südtirol road links: {out} roads={len(roads)}")
        return roads
    return features_to_roads(fetch_geojson(site, cfg, force=force), site, cfg)


def fetch_suedtirol_road_links(site: dict, *, force: bool = False) -> Path:
    cfg = road_links_cfg(site)
    if cfg is None:
        raise SystemExit(
            "sources.road_links missing or not authority suedtirol — "
            "Tirol OSM/GIP axis stays the default"
        )
    proc = processed_dir(site)
    proc.mkdir(parents=True, exist_ok=True)
    out = processed_roads_path(site)
    meta_p = proc / "road_links.meta.json"
    if out.is_file() and meta_p.is_file() and not force and out.stat().st_size > 50:
        print(f"Using cached road links: {out}")
        return out

    fc = fetch_geojson(site, cfg, force=force)
    roads = features_to_roads(fc, site, cfg)
    payload = roads_to_json(roads)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    Image.fromarray(_preview(roads, site, int(cfg["size"])), mode="RGB").save(
        proc / "preview_road_links.png"
    )
    n_main = sum(1 for r in roads if r.get("is_main_road"))
    n_bridge = sum(1 for r in roads if str(r.get("kunstbauten") or "").startswith("Brücke"))
    n_tunnel = sum(1 for r in roads if str(r.get("kunstbauten") or "").startswith("Tunnel"))
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
        "roads": len(roads),
        "main_roads": n_main,
        "bridge_segments": n_bridge,
        "tunnel_segments": n_tunnel,
        "main_only": cfg["main_only"],
        "note": (
            "Südtirol LinksSequences-Rendering axes only. "
            "Width from highway-class defaults (no official width field). "
            "Tirol sites without sources.road_links keep OSM/GIP."
        ),
    }
    meta_p.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Wrote {out}")
    print(f"Wrote {meta_p}")
    print(f"roads={len(roads)} main={n_main} bridge={n_bridge} tunnel={n_tunnel}")
    return out


def uses_suedtirol_structures(site: dict) -> bool:
    """True when this site builds bridges/tunnels from LinksSequences LOCATION."""
    return road_links_cfg(site) is not None


def _span_kind(road: dict) -> str | None:
    """Return ``bridge`` / ``tunnel`` / None from kunstbauten or LOCATION."""
    kb = str(road.get("kunstbauten") or "").lower()
    if "tunnel" in kb or "unterflur" in kb:
        return "tunnel"
    if "galerie" in kb or "gallery" in kb:
        return "gallery"
    if "brücke" in kb or "bruecke" in kb or "bridge" in kb:
        return "bridge"
    loc = road.get("location")
    if loc is None:
        return None
    try:
        loc_i = int(float(loc))
    except (TypeError, ValueError):
        return None
    if loc_i in LOC_TUNNEL:
        return "tunnel"
    if loc_i in LOC_BRIDGE:
        return "bridge"
    return None


def suedtirol_span_features(
    site: dict,
    *,
    kinds: set[str],
) -> list[dict]:
    """Bridge/tunnel features from ``roads_beamng.json`` (BeamNG XY).

    Same dict shape as GIP ``bridge_features`` / ``gallery_features``:
    ``{name, objectid, xy[, kind, str_code, length_m]}``.
    """
    proc = processed_dir(site)
    path = proc / "roads_beamng.json"
    if not path.is_file():
        path = processed_roads_path(site)
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return []
    want = {str(k).lower() for k in kinds}
    out: list[dict] = []
    for key, road in data.items():
        kind = _span_kind(road)
        if kind is None or kind not in want:
            continue
        nodes = road.get("nodes") or []
        xy: list[tuple[float, float]] = []
        for n in nodes:
            if len(n) < 2:
                continue
            xy.append((float(n[0]), float(n[1])))
        if len(xy) < 2:
            continue
        length = 0.0
        for a, b in zip(xy, xy[1:]):
            length += math.hypot(b[0] - a[0], b[1] - a[1])
        oid = road.get("objectid")
        if oid is None:
            oid = road.get("osm_id") or key
        feat: dict = {
            "name": str(road.get("name") or kind),
            "objectid": oid,
            "str_code": road.get("str_code") or "",
            "length_m": round(length, 2),
            "xy": xy,
            "source": "suedtirol_links",
        }
        if kind in {"tunnel", "gallery"}:
            feat["kind"] = kind
        out.append(feat)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    site = load_site()
    print(f"Site: {site_slug(site)}")
    fetch_suedtirol_road_links(site, force=args.force)


if __name__ == "__main__":
    main()
