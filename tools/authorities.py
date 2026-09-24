"""Administrative authorities for a level that crosses a map boundary.

The catalog ``config/authorities.yaml`` names each country/province (CRS,
road-id band, WGS84 ring). A site must list the ones its CRS+bbox covers.

* missing ``authorities`` or ``authorities: leave_empty`` → abort, load no roads
* the working CRS is the authority whose validity polygon covers the largest
  part of the level bbox (or a fixed ``level.crs``, or the only listed key)
* geometry from a smaller authority is reprojected only through that
  authority's official grid; a missing file aborts
* road ``objectid`` stays an int: offset + local id
  (Tirol 0, Südtirol +10_000_000, Schweiz +20_000_000; stride 10_000_000)
* cached road layers are stamped to the level id on ingest; a foreign band aborts
* border continuations are automatic for an unambiguous pair and a review
  list from three endpoints onward
* land-cover classes of a foreign authority map through that authority's
  table and are clipped to its validity polygon
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pyproj import CRS, Transformer
from shapely.geometry import Point, box, shape
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "config" / "authorities.yaml"
_LEAVE_EMPTY = frozenset({"leave_empty", "empty", "none", "null", "false"})
_CATALOG_CACHE: dict | None = None

SEMANTIC_KINDS = frozenset(
    {
        "water_standing",
        "water_flowing",
        "road",
        "settlement",
        "forest_high",
        "forest_scrub",
        "meadow",
    }
)
TIROL_MAPPER = "tirol_landnutzung"
TIROL_WALD_MAPPER = "tirol_wald"
_CLASS_FIELDS = (
    "KLASSE",
    "OBJEKT",
    "OBJEKTBEZEICHNUNG",
    "class",
    "CLASS",
    "code",
    "CODE",
    "art",
    "ART",
)
_DEFAULT_STRIDE = 10_000_000
RESERVED_ROAD_ID_OFFSETS = {
    "tirol": 0,
    "suedtirol": 10_000_000,
    "schweiz": 20_000_000,
}
_DEFAULT_BAND_M = 40.0
_DEFAULT_SNAP_M = 15.0
_LAEA = "EPSG:3035"
AT_GIS_GRID_NAME = "at_bev_AT_GIS_GRID_2021_09_28.tif"
AT_GIS_GRID_URL = f"https://cdn.proj.org/{AT_GIS_GRID_NAME}"
AT_GIS_GRID_PATH = ROOT / "data" / "grids" / AT_GIS_GRID_NAME
_MGI_EPSG = frozenset({4312, 31251, 31252, 31253, 31254, 31255, 31256, 31257})
_ETRS_EPSG = frozenset({4258, 25832, 25833, 25834, 3034, 3035})


@dataclass
class Authority:
    key: str
    crs: str
    road_id_offset: int
    grid: Path | None = None
    validity: Path | None = None
    validity_crs: str | None = None
    landcover: str | dict | None = None
    landcover_field: str | None = None
    landcover_path: Path | None = None
    landcover_crs: str | None = None
    roads: Path | None = None
    roads_crs: str | None = None
    road_id_field: str = "OBJECTID"


@dataclass
class LevelRules:
    crs_mode: str = "auto"
    border_band_m: float = _DEFAULT_BAND_M
    border_snap_m: float = _DEFAULT_SNAP_M
    road_id_stride: int = _DEFAULT_STRIDE
    border_confirmed: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class RoadEnd:
    objectid: int
    source_id: int
    authority: str
    x: float
    y: float
    end: str


class ChainTransform:
    """Apply pyproj-style ``transform(x, y)`` steps in order."""

    def __init__(self, steps: list) -> None:
        self.steps = steps

    def transform(self, x: float, y: float) -> tuple[float, float]:
        for step in self.steps:
            x, y = step.transform(x, y)
        return float(x), float(y)


def same_crs(a: str, b: str) -> bool:
    return CRS.from_user_input(a) == CRS.from_user_input(b)


def _resolve_path(value: str | Path | None) -> Path | None:
    if value is None or value == "":
        return None
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _as_int(value, label: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as ex:
        raise SystemExit(f"{label} ist keine Ganzzahl: {value!r}") from ex


def level_rules(site: dict) -> LevelRules:
    raw = site.get("level") or {}
    if not isinstance(raw, dict):
        raise SystemExit("level muss ein Block sein")
    mode = str(raw.get("crs") or "auto").strip()
    confirmed = raw.get("border_confirmed") or []
    if not isinstance(confirmed, list):
        raise SystemExit("level.border_confirmed muss eine Liste sein")
    stride = _as_int(raw.get("road_id_stride") or _DEFAULT_STRIDE, "road_id_stride")
    if stride <= 0:
        raise SystemExit("road_id_stride muss positiv sein")
    return LevelRules(
        crs_mode=mode,
        border_band_m=float(raw.get("border_band_m") or _DEFAULT_BAND_M),
        border_snap_m=float(raw.get("border_snap_m") or _DEFAULT_SNAP_M),
        road_id_stride=stride,
        border_confirmed=list(confirmed),
    )


def load_authority_catalog() -> dict:
    """Reserved authorities from ``config/authorities.yaml``."""
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None:
        return _CATALOG_CACHE
    if not CATALOG_PATH.is_file():
        raise SystemExit(f"Behördenkatalog fehlt: {CATALOG_PATH}")
    raw = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or not isinstance(raw.get("authorities"), dict):
        raise SystemExit(f"{CATALOG_PATH.name}: Block authorities fehlt")
    out: dict = {"stride": int(raw.get("stride") or _DEFAULT_STRIDE), "authorities": {}}
    for key, spec in raw["authorities"].items():
        if not isinstance(spec, dict):
            raise SystemExit(f"Katalog {key}: Block fehlt")
        ring = spec.get("wgs84_ring") or []
        if not isinstance(ring, list) or len(ring) < 4:
            raise SystemExit(f"Katalog {key}: wgs84_ring braucht mindestens 4 Punkte")
        pts = [(float(p[0]), float(p[1])) for p in ring]
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        nuts_ids = []
        for item in spec.get("nuts_ids") or []:
            token = str(item).strip().upper()
            if token:
                nuts_ids.append(token)
        out["authorities"][str(key)] = {
            "crs": str(spec.get("crs") or "").strip(),
            "road_id_offset": _as_int(spec.get("road_id_offset"), f"Katalog {key}.road_id_offset"),
            "landcover": spec.get("landcover"),
            "nuts_ids": nuts_ids,
            "ring": pts,
        }
    _CATALOG_CACHE = out
    return out


def catalog_authority_keys() -> tuple[str, ...]:
    return tuple(load_authority_catalog()["authorities"].keys())


def _catalog_entry(key: str) -> dict:
    cat = load_authority_catalog()["authorities"]
    if key not in cat:
        raise SystemExit(
            f"Behörde {key} steht nicht im Katalog ({', '.join(cat)})"
        )
    return cat[key]


def authorities_spec(site: dict | None) -> dict[str, dict] | None:
    """Declared authorities, or ``None`` if the key is missing.

    ``leave_empty`` / ``[]`` / ``false`` become an empty dict (explicit refuse).
    A string or list of keys inherits the catalog. A mapping may override fields.
    """
    site = site or {}
    if "authorities" not in site:
        return None
    raw = site.get("authorities")
    if raw is False or raw is None:
        return {}
    if isinstance(raw, str):
        token = raw.strip()
        if token.lower() in _LEAVE_EMPTY:
            return {}
        return {token: {}}
    if isinstance(raw, list):
        if not raw:
            return {}
        out: dict[str, dict] = {}
        for item in raw:
            if isinstance(item, str) and item.strip():
                out[item.strip()] = {}
            elif isinstance(item, dict) and item.get("key"):
                out[str(item["key"]).strip()] = {
                    k: v for k, v in item.items() if k != "key"
                }
            else:
                raise SystemExit(f"authorities-Liste: ungültiger Eintrag {item!r}")
        return out
    if isinstance(raw, dict):
        return raw
    raise SystemExit("authorities muss ein Name, eine Liste oder ein Block je Behörde sein")


def site_bbox_wgs84(site: dict):
    """Site bbox as a lon/lat polygon (the four authored corners, in order)."""
    from shapely.geometry import Polygon

    if "bbox" not in site:
        raise SystemExit("bbox fehlt — ohne Ausschnitt keine Behördenprüfung")
    crs = str(site.get("crs") or "EPSG:31254")
    xmin, ymin, xmax, ymax = map(float, site["bbox"])
    tf = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    ring = [
        tf.transform(xmin, ymin),
        tf.transform(xmax, ymin),
        tf.transform(xmax, ymax),
        tf.transform(xmin, ymax),
        tf.transform(xmin, ymin),
    ]
    poly = Polygon([(float(x), float(y)) for x, y in ring])
    if poly.is_empty or not poly.is_valid:
        poly = box(
            min(p[0] for p in ring),
            min(p[1] for p in ring),
            max(p[0] for p in ring),
            max(p[1] for p in ring),
        )
    return poly


NUTS_L2_NAME = "NUTS_RG_01M_2024_4326_LEVL_2.geojson"
NUTS_L2_URL = (
    "https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/" + NUTS_L2_NAME
)
NUTS_L2_PATH = ROOT / "data" / "boundaries" / NUTS_L2_NAME
_COVER_TOL = 0.002


def ensure_nuts_boundaries() -> Path:
    """Eurostat GISCO NUTS 2024 level-2 polygons (WGS84). Downloads once."""
    path = NUTS_L2_PATH
    if path.is_file() and path.stat().st_size > 100_000:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    import requests

    print(f"Lade Verwaltungsgrenzen (GISCO NUTS 2024): {NUTS_L2_URL}")
    tmp = path.with_suffix(path.suffix + ".part")
    try:
        r = requests.get(NUTS_L2_URL, timeout=120)
        r.raise_for_status()
        tmp.write_bytes(r.content)
        tmp.replace(path)
    except Exception as ex:
        if tmp.is_file():
            tmp.unlink()
        raise SystemExit(f"NUTS-Grenzen fehlen und Download scheiterte: {ex}") from ex
    if not path.is_file() or path.stat().st_size < 100_000:
        raise SystemExit(f"NUTS-Datei unvollständig: {path}")
    print(f"  -> {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")
    return path


def _nuts_id(props: dict) -> str:
    for key in ("NUTS_ID", "nuts_id", "NUTS_CODE"):
        raw = props.get(key)
        if raw:
            return str(raw).strip().upper()
    return ""


def _nuts_name(props: dict) -> str:
    for key in ("NAME_LATN", "NUTS_NAME", "NAME_ENGL"):
        raw = props.get(key)
        if raw:
            return str(raw).strip()
    return _nuts_id(props)


def load_nuts_features(*, download: bool = False) -> list[tuple[str, str, object]]:
    """``(nuts_id, name, shapely geom)`` from the cached GISCO file."""
    path = ensure_nuts_boundaries() if download else NUTS_L2_PATH
    if not path.is_file():
        if download:
            path = ensure_nuts_boundaries()
        else:
            return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[tuple[str, str, object]] = []
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        nid = _nuts_id(props)
        geom = feat.get("geometry")
        if not nid or not geom:
            continue
        try:
            g = shape(geom)
        except Exception:
            continue
        if g.is_empty:
            continue
        out.append((nid, _nuts_name(props), g))
    if not out:
        raise SystemExit("NUTS-Datei enthält keine Flächen")
    return out


def _ring_polygon(spec: dict):
    return shape({"type": "Polygon", "coordinates": [spec["ring"]]})


def authority_polygons_wgs84(*, use_nuts: bool = True) -> dict[str, object]:
    """Catalog key → WGS84 polygon (NUTS union when the file is available)."""
    catalog = load_authority_catalog()["authorities"]
    nuts_map: dict[str, object] = {}
    if use_nuts:
        try:
            for nid, _name, geom in load_nuts_features(download=False):
                nuts_map[nid] = geom
        except SystemExit:
            nuts_map = {}
    out: dict[str, object] = {}
    for key, spec in catalog.items():
        parts = []
        for nid in spec.get("nuts_ids") or []:
            geom = nuts_map.get(str(nid).upper())
            if geom is not None:
                parts.append(geom)
        if parts:
            out[key] = unary_union(parts)
        else:
            out[key] = _ring_polygon(spec)
    return out


def detect_authorities_in_bbox(site: dict, *, use_nuts: bool = True) -> list[str]:
    """Catalog keys whose official (or fallback) polygon meets the site bbox."""
    level = site_bbox_wgs84(site)
    hits: list[str] = []
    for key, poly in authority_polygons_wgs84(use_nuts=use_nuts).items():
        if poly is None or poly.is_empty or level.disjoint(poly):
            continue
        hits.append(key)
    return hits


@dataclass
class AuthorityCoverage:
    known: list[str]
    covered_frac: float
    uncovered_frac: float
    foreign: list[dict]
    complete: bool

    def summary(self) -> str:
        known = ", ".join(self.known) if self.known else "(keine)"
        line = (
            f"Bekannt: {known}. Abdeckung {100.0 * self.covered_frac:.1f} %."
        )
        if self.complete:
            return line + " Der Ausschnitt liegt vollständig in den Katalogbehörden."
        extra = ", ".join(
            f"{row['nuts_id']} ({row['name']})" for row in self.foreign[:8]
        ) or "kein NUTS-Treffer"
        return (
            f"{line} Unbekannt {100.0 * self.uncovered_frac:.1f} %. "
            f"Außerhalb: {extra}."
        )


def cover_site_authorities(site: dict, *, use_nuts: bool = True) -> AuthorityCoverage:
    """How much of the authored bbox sits inside catalog authorities."""
    level = site_bbox_wgs84(site)
    level_a = _to_laea(level, "EPSG:4326")
    total = float(level_a.area)
    if total <= 0:
        raise SystemExit("bbox hat keine Fläche")
    known_geoms = []
    known_keys: list[str] = []
    for key, poly in authority_polygons_wgs84(use_nuts=use_nuts).items():
        if poly.is_empty or level.disjoint(poly):
            continue
        known_keys.append(key)
        known_geoms.append(poly.intersection(level))
    if known_geoms:
        covered = _to_laea(unary_union(known_geoms), "EPSG:4326")
        covered_frac = min(1.0, float(covered.area) / total)
        leftover = level.difference(unary_union(known_geoms))
    else:
        covered_frac = 0.0
        leftover = level
    uncovered_frac = max(0.0, 1.0 - covered_frac)
    foreign: list[dict] = []
    mapped = {
        nid
        for spec in load_authority_catalog()["authorities"].values()
        for nid in spec.get("nuts_ids") or []
    }
    if uncovered_frac > _COVER_TOL and use_nuts:
        try:
            for nid, name, geom in load_nuts_features(download=False):
                if nid in mapped or leftover.disjoint(geom):
                    continue
                piece = leftover.intersection(geom)
                if piece.is_empty:
                    continue
                frac = float(_to_laea(piece, "EPSG:4326").area) / total
                if frac < 0.0005:
                    continue
                foreign.append(
                    {
                        "nuts_id": nid,
                        "name": name,
                        "frac": frac,
                    }
                )
        except SystemExit:
            foreign = []
    foreign.sort(key=lambda row: -float(row["frac"]))
    return AuthorityCoverage(
        known=known_keys,
        covered_frac=covered_frac,
        uncovered_frac=uncovered_frac,
        foreign=foreign,
        complete=uncovered_frac <= _COVER_TOL and bool(known_keys),
    )


def next_reserved_offset() -> int:
    used = set(RESERVED_ROAD_ID_OFFSETS.values())
    for spec in load_authority_catalog()["authorities"].values():
        used.add(int(spec["road_id_offset"]))
    stride = int(load_authority_catalog()["stride"])
    offset = 0
    while offset in used:
        offset += stride
    return offset


def format_authorities_yaml(keys: list[str]) -> str:
    keys = [str(k).strip() for k in keys if str(k).strip()]
    if len(keys) == 1:
        return f"authorities: {keys[0]}\n"
    lines = ["authorities:\n"]
    for key in keys:
        lines.append(f"  - {key}\n")
    return "".join(lines)


def upsert_site_authorities(path: Path, keys: list[str]) -> str:
    """Write catalog keys into a site YAML. Keeps extra fields in a mapping."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    keys = [str(k).strip() for k in keys if str(k).strip()]
    if not keys:
        raise SystemExit("Keine Behörden zum Eintragen")
    existing = authorities_spec(data)
    rich = bool(existing) and any(
        isinstance(existing.get(k), dict) and existing[k] for k in existing
    )
    if rich:
        missing = [k for k in keys if k not in existing]
        if not missing:
            return "unchanged"
        block = "\n".join(f"  {k}: {{}}" for k in missing) + "\n"
        if re.search(r"(?m)^authorities:\s*$", text):
            text = re.sub(r"(?m)^authorities:\s*$", f"authorities:\n{block.rstrip()}", text, count=1)
        else:
            text = text.rstrip() + "\n" + block
        path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        return "merged"
    blob = format_authorities_yaml(keys)
    if re.search(r"(?m)^authorities:\s", text) or re.search(r"(?m)^authorities:\s*$", text):
        text = re.sub(
            r"(?ms)^authorities:.*?(?=\n[^\s]|\Z)",
            blob.rstrip(),
            text,
            count=1,
        )
    elif re.search(r"(?m)^crs:\s", text):
        text = re.sub(r"(?m)^(crs:\s.+\n)", r"\1" + blob, text, count=1)
    else:
        text = blob + text
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return "written"


def append_catalog_authority(
    key: str,
    *,
    nuts_id: str,
    crs: str,
    road_id_offset: int | None = None,
    name: str = "",
) -> None:
    """Append one authority to ``config/authorities.yaml``."""
    key = str(key).strip()
    nuts_id = str(nuts_id).strip().upper()
    crs = str(crs).strip()
    if not key or not nuts_id or not crs:
        raise SystemExit("Neue Behörde braucht key, nuts_id und crs")
    if key in load_authority_catalog()["authorities"]:
        raise SystemExit(f"Behörde {key} steht schon im Katalog")
    offset = (
        int(road_id_offset)
        if road_id_offset is not None
        else next_reserved_offset()
    )
    geom = None
    for nid, _name, g in load_nuts_features(download=True):
        if nid == nuts_id:
            geom = g
            break
    if geom is None:
        raise SystemExit(f"NUTS {nuts_id} fehlt in der Grenzdatei")
    simple = geom.simplify(0.02, preserve_topology=True)
    if simple.is_empty:
        simple = geom
    ext = list(simple.convex_hull.exterior.coords)
    ring = [[round(float(x), 5), round(float(y), 5)] for x, y in ext]
    comment = f"  # {name}\n" if name else ""
    ring_lines = "\n".join(f"      - [{a}, {b}]" for a, b in ring)
    block = (
        f"\n{comment}  {key}:\n"
        f"    crs: {crs}\n"
        f"    road_id_offset: {offset}\n"
        f"    nuts_ids: [{nuts_id}]\n"
        f"    wgs84_ring:\n{ring_lines}\n"
    )
    text = CATALOG_PATH.read_text(encoding="utf-8")
    CATALOG_PATH.write_text(text.rstrip() + "\n" + block, encoding="utf-8")
    global _CATALOG_CACHE
    _CATALOG_CACHE = None


def apply_site_authorities(path: Path, *, write: bool = True) -> AuthorityCoverage:
    """Detect authorities for a site YAML and optionally write them."""
    path = Path(path)
    site = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    ensure_nuts_boundaries()
    cov = cover_site_authorities(site, use_nuts=True)
    if write and cov.known:
        upsert_site_authorities(path, cov.known)
    return cov


def require_road_authorities(site: dict) -> list[str]:
    """Abort when authorities are missing, empty, or omit a bbox hit.

    Returns the declared keys. Call this before any road-layer fetch or load.
    """
    spec = authorities_spec(site)
    detected = detect_authorities_in_bbox(site) if site.get("bbox") is not None else []
    found = ", ".join(detected) if detected else "(kein Katalogtreffer)"
    if spec is None:
        raise SystemExit(
            f"authorities fehlt. Der Ausschnitt (CRS {site.get('crs')}) liegt in: {found}. "
            "Behörden eintragen (z. B. authorities: tirol) oder "
            "authorities: leave_empty — dann wird kein Straßenlayer geladen."
        )
    if not spec:
        raise SystemExit(
            f"authorities ist leer / leave_empty. Straßenlayer werden nicht geladen. "
            f"Der Ausschnitt liegt in: {found}."
        )
    missing = [key for key in detected if key not in spec]
    if missing:
        raise SystemExit(
            f"Ausschnitt liegt in {found}, fehlt unter authorities: {', '.join(missing)}"
        )
    return list(spec.keys())


def load_authorities(site: dict) -> list[Authority]:
    """Authorities in YAML order. Catalog fills crs / offset when omitted."""
    spec_map = authorities_spec(site)
    if not spec_map:
        return []
    out: list[Authority] = []
    seen_offset: dict[int, str] = {}
    for key, raw_spec in spec_map.items():
        spec = dict(raw_spec) if isinstance(raw_spec, dict) else {}
        if not isinstance(raw_spec, dict):
            raise SystemExit(f"Behörde {key} muss ein Block sein")
        try:
            catalog = _catalog_entry(str(key))
        except SystemExit:
            catalog = None
        if catalog:
            spec.setdefault("crs", catalog["crs"])
            spec.setdefault("road_id_offset", catalog["road_id_offset"])
            if spec.get("landcover") is None and catalog.get("landcover"):
                spec["landcover"] = catalog["landcover"]
        if not spec.get("crs"):
            raise SystemExit(f"Behörde {key}: crs fehlt")
        if "road_id_offset" not in spec:
            raise SystemExit(f"Behörde {key}: road_id_offset fehlt")
        offset = _as_int(spec.get("road_id_offset"), f"{key}.road_id_offset")
        if offset < 0:
            raise SystemExit(f"Behörde {key}: road_id_offset muss >= 0 sein")
        if offset in seen_offset:
            raise SystemExit(
                f"Behörde {key}: road_id_offset {offset} ist schon {seen_offset[offset]}"
            )
        seen_offset[offset] = str(key)
        landcover = spec.get("landcover")
        if isinstance(landcover, dict):
            cleaned: dict = {}
            for class_key, kind in landcover.items():
                kind_s = str(kind).strip()
                if kind_s not in SEMANTIC_KINDS:
                    raise SystemExit(
                        f"Behörde {key}: Landbedeckung {class_key!r} → {kind_s!r} "
                        f"ist keine bekannte Bedeutung"
                    )
                cleaned[class_key] = kind_s
            landcover = cleaned
        elif landcover is not None:
            landcover = str(landcover).strip()
            if landcover not in {TIROL_MAPPER, TIROL_WALD_MAPPER}:
                raise SystemExit(
                    f"Behörde {key}: landcover {landcover!r} ist weder eine Tabelle "
                    f"noch {TIROL_MAPPER}"
                )
            if landcover in {TIROL_MAPPER, TIROL_WALD_MAPPER} and str(key) != "tirol":
                raise SystemExit(
                    f"Behörde {key}: {landcover} gilt nur für tirol und würde "
                    f"deren Klassen übernehmen"
                )
        out.append(
            Authority(
                key=str(key),
                crs=str(spec["crs"]).strip(),
                road_id_offset=offset,
                grid=_resolve_path(spec.get("grid")),
                validity=_resolve_path(spec.get("validity")),
                validity_crs=(str(spec["validity_crs"]).strip() if spec.get("validity_crs") else None),
                landcover=landcover,
                landcover_field=(
                    str(spec["landcover_field"]).strip() if spec.get("landcover_field") else None
                ),
                landcover_path=_resolve_path(spec.get("landcover_path")),
                landcover_crs=(
                    str(spec["landcover_crs"]).strip() if spec.get("landcover_crs") else None
                ),
                roads=_resolve_path(spec.get("roads")),
                roads_crs=(str(spec["roads_crs"]).strip() if spec.get("roads_crs") else None),
                road_id_field=str(spec.get("road_id_field") or "OBJECTID"),
            )
        )
    return out


def authority_by_key(site: dict, key: str) -> Authority:
    for auth in load_authorities(site):
        if auth.key == key:
            return auth
    raise SystemExit(f"Behörde {key} fehlt unter authorities")


def gip_authority_key(site: dict | None = None) -> str:
    """Home authority for the road layer (GIP or ``authorities.*.roads``).

    Order: ``sources.gip.authority``, ``level.authority``, the only YAML
    authority, otherwise ``tirol`` (the Verkehrswege service).
    """
    site = site or {}
    gip = (site.get("sources") or {}).get("gip") or {}
    raw = gip.get("authority")
    if raw:
        return str(raw).strip()
    level = site.get("level") if isinstance(site.get("level"), dict) else {}
    raw = (level or {}).get("authority") or (level or {}).get("home_authority")
    if raw:
        return str(raw).strip()
    spec = authorities_spec(site)
    if spec:
        keys = list(spec.keys())
        if len(keys) == 1:
            return keys[0]
        if "tirol" in keys:
            return "tirol"
        return keys[0]
    return "tirol"


def authority_band(site: dict | None, authority_key: str) -> tuple[int, int]:
    """``(offset, stride)`` for a reserved or YAML authority."""
    site = site or {}
    key = str(authority_key or "").strip()
    if not key:
        raise SystemExit("Behörde fehlt für den Straßen-Nummernraum")
    stride = level_rules(site).road_id_stride
    if site.get("authorities"):
        try:
            return authority_by_key(site, key).road_id_offset, stride
        except SystemExit:
            if key not in RESERVED_ROAD_ID_OFFSETS:
                raise
    if key not in RESERVED_ROAD_ID_OFFSETS:
        raise SystemExit(
            f"Behörde {key}: kein reservierter Nummernraum "
            f"(bekannt: {', '.join(RESERVED_ROAD_ID_OFFSETS)})"
        )
    return RESERVED_ROAD_ID_OFFSETS[key], stride


def interpret_road_id(
    site: dict | None,
    raw_oid,
    authority_key: str,
    *,
    label: str = "objectid",
) -> tuple[int, int]:
    """``(level_objectid, source_id)``. Local or already-level; foreign band aborts."""
    raw = _as_int(raw_oid, label)
    offset, stride = authority_band(site, authority_key)
    if offset <= raw < offset + stride:
        return raw, raw - offset
    if 0 <= raw < stride:
        oid = offset + raw
        site = site or {}
        if site.get("authorities"):
            for other in load_authorities(site):
                if other.key == authority_key:
                    continue
                if other.road_id_offset <= oid < other.road_id_offset + stride:
                    raise SystemExit(
                        f"Behörde {authority_key}: ID {oid} fällt in den "
                        f"Nummernraum von {other.key}"
                    )
        return oid, raw
    raise SystemExit(
        f"Behörde {authority_key}: {label} {raw} liegt weder im lokalen "
        f"Bereich 0…{stride - 1} noch im Nummernraum "
        f"{offset}…{offset + stride - 1}"
    )


def _validity_crs(auth: Authority) -> str:
    return auth.validity_crs or auth.crs


def _load_geojson(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"Datei fehlt: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_geometries(node: dict):
    gtype = (node or {}).get("type")
    if gtype == "FeatureCollection":
        for feat in node.get("features") or []:
            yield from _iter_geometries(feat)
        return
    if gtype == "Feature":
        geom = node.get("geometry")
        if geom:
            yield geom
        return
    if gtype == "GeometryCollection":
        for geom in node.get("geometries") or []:
            yield from _iter_geometries(geom)
        return
    if gtype:
        yield node


def load_validity_polygon(auth: Authority):
    if auth.validity is None:
        raise SystemExit(f"Behörde {auth.key}: validity (Gültigkeitspolygon) fehlt")
    data = _load_geojson(auth.validity)
    geoms = []
    for geom in _iter_geometries(data):
        gtype = geom.get("type")
        if gtype not in {"Polygon", "MultiPolygon"}:
            continue
        geoms.append(shape(geom))
    if not geoms:
        raise SystemExit(f"Behörde {auth.key}: {auth.validity} enthält kein Polygon")
    return unary_union(geoms)


def _to_laea(geom, crs: str):
    tf = Transformer.from_crs(crs, _LAEA, always_xy=True)
    return shp_transform(tf.transform, geom)


def majority_authority(site: dict, auths: list[Authority] | None = None) -> Authority:
    """Authority with the largest validity area inside the authored bbox.

    Equal area keeps the earlier authority. The comparison uses an equal-area
    projection so a few metres of datum shift cannot decide the vote; the
    official grid is applied later, when geometry enters the working CRS.
    """
    auths = auths if auths is not None else load_authorities(site)
    if not auths:
        raise SystemExit("authorities ist leer")
    bbox_crs = str(site.get("crs") or "EPSG:31254")
    xmin, ymin, xmax, ymax = map(float, site["bbox"])
    level = _to_laea(box(xmin, ymin, xmax, ymax), bbox_crs)
    winner = auths[0]
    best = -1.0
    for auth in auths:
        poly = _to_laea(load_validity_polygon(auth), _validity_crs(auth))
        area = float(poly.intersection(level).area)
        if area > best:
            best = area
            winner = auth
    return winner


def resolved_working_crs(site: dict) -> str:
    """Working CRS. Without a usable authorities list this is ``site.crs``."""
    auths = load_authorities(site)
    if not auths:
        return str(site.get("crs") or "EPSG:31254")
    rules = level_rules(site)
    if rules.crs_mode.lower() != "auto":
        return rules.crs_mode
    if len(auths) == 1:
        return auths[0].crs
    with_val = [a for a in auths if a.validity is not None]
    if len(with_val) >= 2:
        return majority_authority(site, with_val).crs
    return auths[0].crs


def _proj_ops(crs: CRS) -> str:
    """Projection step without a Helmert datum shift.

    The official grid is the datum step. Leaving ``+towgs84`` in the
    projection would apply a second, silent shift.
    """
    import warnings

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="You will likely lose important projection information",
        )
        proj4 = crs.to_proj4()
    parts: list[str] = []
    for token in proj4.split():
        if token.startswith("+towgs84=") or token.startswith("+type="):
            continue
        if token in {"+no_defs"} or token.startswith("+units="):
            continue
        parts.append(token)
    if not parts:
        raise SystemExit(f"CRS {crs.to_string()} hat keine Projektionsparameter")
    return " ".join(parts)


def _steps_to_radians(crs: CRS) -> str:
    if crs.is_geographic:
        return "+step +proj=unitconvert +xy_in=deg +xy_out=rad"
    return "+step +inv " + _proj_ops(crs)


def _steps_from_radians(crs: CRS) -> str:
    if crs.is_geographic:
        return "+step +proj=unitconvert +xy_in=rad +xy_out=deg"
    return "+step " + _proj_ops(crs)


def _datum_epsg(crs: CRS) -> int | None:
    code = crs.to_epsg()
    if code:
        return int(code)
    geod = crs.geodetic_crs
    if geod is not None and geod.to_epsg():
        return int(geod.to_epsg())
    return None


def _is_mgi_crs(crs: CRS) -> bool:
    code = _datum_epsg(crs)
    if code in _MGI_EPSG:
        return True
    blob = f"{crs.name or ''} {crs.datum.name if crs.datum else ''}".upper()
    return "MGI" in blob


def _is_etrs_crs(crs: CRS) -> bool:
    code = _datum_epsg(crs)
    if code in _ETRS_EPSG:
        return True
    blob = f"{crs.name or ''} {crs.datum.name if crs.datum else ''}".upper()
    return "ETRS" in blob


def _is_wgs84(crs_in) -> bool:
    crs = crs_in if isinstance(crs_in, CRS) else CRS.from_user_input(crs_in)
    code = crs.to_epsg()
    if code in {4326, 4979}:
        return True
    blob = f"{crs.name or ''} {crs.datum.name if crs.datum else ''}".upper()
    return bool(crs.is_geographic and ("WGS 84" in blob or "WGS84" in blob))


def _same_horizontal_datum(src: CRS, dst: CRS) -> bool:
    if _is_mgi_crs(src) and _is_mgi_crs(dst):
        return True
    if _is_etrs_crs(src) and _is_etrs_crs(dst):
        return True
    da = src.datum.name if src.datum else ""
    db = dst.datum.name if dst.datum else ""
    return bool(da and db and da == db)


def _grid_operator(grid: Path) -> str:
    suffix = grid.suffix.lower()
    if suffix in {".tif", ".tiff"}:
        return "gridshift"
    return "hgridshift"


def grid_pipeline(src_crs: str, dst_crs: str, grid: Path) -> str:
    src = CRS.from_user_input(src_crs)
    dst = CRS.from_user_input(dst_crs)
    grids = grid.as_posix()
    if " " in grids:
        grids = f'"{grids}"'
    op = _grid_operator(grid)
    # BEV AT_GIS_GRID is MGI → ETRS89. Going the other way inverts the step.
    invert = ""
    if _is_etrs_crs(src) and _is_mgi_crs(dst):
        invert = "+inv "
    elif op == "gridshift" and _is_mgi_crs(src) and _is_etrs_crs(dst):
        invert = ""
    return (
        "+proj=pipeline "
        + _steps_to_radians(src)
        + f" +step {invert}+proj={op} +grids={grids} "
        + _steps_from_radians(dst)
    )


def ensure_at_gis_grid() -> Path:
    """BEV MGI↔ETRS89 GeoTIFF (PROJ CDN). Downloads once into data/grids/."""
    path = AT_GIS_GRID_PATH
    if path.is_file() and path.stat().st_size > 1000:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    import urllib.request

    print(f"Lade BEV-Korrekturgitter: {AT_GIS_GRID_URL}")
    tmp = path.with_suffix(path.suffix + ".part")
    try:
        urllib.request.urlretrieve(AT_GIS_GRID_URL, tmp)
        tmp.replace(path)
    except Exception as ex:
        if tmp.is_file():
            tmp.unlink()
        raise SystemExit(f"Korrekturgitter fehlt und Download scheiterte: {ex}") from ex
    if not path.is_file() or path.stat().st_size < 1000:
        raise SystemExit(f"Korrekturgitter unvollständig: {path}")
    print(f"  -> {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")
    return path


def resolve_correction_grid(
    src_crs: str,
    dst_crs: str,
    grid: Path | None = None,
) -> Path | None:
    """Explicit YAML grid, else the BEV AT grid for MGI↔ETRS."""
    if grid is not None:
        return grid
    src = CRS.from_user_input(src_crs)
    dst = CRS.from_user_input(dst_crs)
    if (_is_mgi_crs(src) and _is_etrs_crs(dst)) or (_is_etrs_crs(src) and _is_mgi_crs(dst)):
        return ensure_at_gis_grid()
    return None


def make_grid_transformer(src_crs: str, dst_crs: str, grid: Path | None):
    """Reproject with the named grid (NTv2 or GeoTIFF). Missing file aborts."""
    if same_crs(src_crs, dst_crs):
        return Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    resolved = resolve_correction_grid(src_crs, dst_crs, grid)
    if resolved is not None:
        if not resolved.is_file():
            raise SystemExit(
                f"Umrechnungsraster fehlt ({src_crs} → {dst_crs}): {resolved}"
            )
        return Transformer.from_pipeline(grid_pipeline(src_crs, dst_crs, resolved))
    src = CRS.from_user_input(src_crs)
    dst = CRS.from_user_input(dst_crs)
    if _same_horizontal_datum(src, dst):
        return Transformer.from_pipeline(
            "+proj=pipeline " + _steps_to_radians(src) + " " + _steps_from_radians(dst)
        )
    shown = grid if grid is not None else "(nicht gesetzt)"
    raise SystemExit(f"Umrechnungsraster fehlt ({src_crs} → {dst_crs}): {shown}")


def crs_wkid(crs: str) -> int:
    code = CRS.from_user_input(crs).to_epsg()
    if code is None:
        raise SystemExit(f"CRS {crs} hat keine EPSG-Nummer (ArcGIS inSR/outSR)")
    return int(code)


def gip_native_crs(site: dict | None = None) -> str:
    """CRS in which GIP is requested and cached (not 4326)."""
    site = site or {}
    gip = (site.get("sources") or {}).get("gip") or {}
    raw = gip.get("crs")
    if raw:
        return str(raw).strip()
    if site.get("authorities"):
        return authority_by_key(site, gip_authority_key(site)).crs
    return str(site.get("crs") or "EPSG:31254")


def feature_collection_crs(data: dict | None) -> str | None:
    """``crs.properties.name`` from a GeoJSON FeatureCollection, if present."""
    if not isinstance(data, dict):
        return None
    crs = data.get("crs")
    if not isinstance(crs, dict):
        return None
    props = crs.get("properties") if isinstance(crs.get("properties"), dict) else {}
    name = props.get("name")
    if not name:
        return None
    raw = str(name).strip()
    if not raw:
        return None
    upper = raw.upper()
    if upper.startswith("URN:OGC:DEF:CRS:EPSG"):
        return f"EPSG:{raw.rsplit(':', 1)[-1]}"
    return raw


def gip_fc_crs(data: dict | None, site: dict | None = None) -> str:
    """CRS written on a cached GIP FeatureCollection, else the native request CRS."""
    found = feature_collection_crs(data)
    if found:
        return found
    return gip_native_crs(site)


def transformer_into_working(site: dict, authority_key: str, geometry_crs: str):
    """Geometry CRS → working CRS.

    Same CRS: identity. Crossing a datum uses the authority grid (or the BEV
    AT GeoTIFF for MGI↔ETRS). No silent Helmert / WGS84 hop for GIP.
    """
    auth = authority_by_key(site, authority_key)
    working = resolved_working_crs(site)
    if same_crs(geometry_crs, working):
        return Transformer.from_crs(geometry_crs, working, always_xy=True)
    if same_crs(geometry_crs, auth.crs):
        return make_grid_transformer(auth.crs, working, auth.grid)
    if same_crs(auth.crs, working):
        return make_grid_transformer(geometry_crs, working, auth.grid)
    into_native = make_grid_transformer(geometry_crs, auth.crs, auth.grid)
    onto_working = make_grid_transformer(auth.crs, working, auth.grid)
    return ChainTransform([into_native, onto_working])


def apply_working_frame(site: dict) -> tuple[str, list[float]]:
    """``(working_crs, bbox)``. Bbox stays put when it is already in that CRS."""
    authored = str(site.get("crs") or "EPSG:31254")
    bbox = list(map(float, site["bbox"]))
    if not site.get("authorities"):
        return authored, bbox
    working = resolved_working_crs(site)
    if same_crs(authored, working):
        return working, bbox
    owner = None
    for auth in load_authorities(site):
        if same_crs(auth.crs, authored):
            owner = auth
            break
    if owner is None:
        raise SystemExit(
            f"bbox-CRS {authored} gehört zu keiner Behörde, "
            f"daher fehlt das Umrechnungsraster nach {working}"
        )
    tf = make_grid_transformer(authored, working, owner.grid)
    xmin, ymin, xmax, ymax = bbox
    xs: list[float] = []
    ys: list[float] = []
    for x, y in ((xmin, ymin), (xmin, ymax), (xmax, ymin), (xmax, ymax)):
        xx, yy = tf.transform(x, y)
        xs.append(float(xx))
        ys.append(float(yy))
    return working, [min(xs), min(ys), max(xs), max(ys)]


def level_objectid(site: dict, local_id: int, authority_key: str) -> tuple[int, int]:
    """``(objectid, source_id)``. ``source_id`` is the unshifted local id."""
    return interpret_road_id(site, local_id, authority_key, label="lokale Straßen-ID")


def gip_ids(
    site: dict | None,
    raw_oid,
    *,
    authority_key: str | None = None,
) -> tuple[int, int]:
    """``(level_objectid, source_id)``. Reserved bands apply without YAML."""
    site = site or {}
    key = authority_key or gip_authority_key(site)
    return interpret_road_id(site, raw_oid, key, label="Straßen-OBJECTID")


def props_authority_key(
    site: dict | None,
    props: dict | None,
    authority_key: str | None = None,
) -> str:
    if authority_key:
        return str(authority_key).strip()
    raw = (props or {}).get("_autoroad_authority")
    if raw:
        return str(raw).strip()
    return gip_authority_key(site)


def gip_source_oid(props: dict | None) -> int | None:
    """Unshifted service id. Prefers ``SOURCE_OBJECTID`` over a stamped OBJECTID."""
    props = props if isinstance(props, dict) else {}
    for key in ("_autoroad_source_id", "SOURCE_OBJECTID"):
        raw = props.get(key)
        if raw is None or raw == "":
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    raw = props.get("OBJECTID")
    if raw is None or raw == "":
        return None
    try:
        oid = int(raw)
    except (TypeError, ValueError):
        return None
    stride = _DEFAULT_STRIDE
    for key, offset in RESERVED_ROAD_ID_OFFSETS.items():
        if offset and offset <= oid < offset + stride:
            return oid - offset
    return oid


def gip_ids_from_props(
    site: dict | None,
    props: dict | None,
    *,
    authority_key: str | None = None,
) -> tuple[int, int] | None:
    """Idempotent: source field or OBJECTID, using the feature's authority."""
    props = props if isinstance(props, dict) else {}
    raw = None
    for key in ("_autoroad_source_id", "SOURCE_OBJECTID", "OBJECTID"):
        if props.get(key) is None or props.get(key) == "":
            continue
        try:
            raw = int(props.get(key))
            break
        except (TypeError, ValueError):
            continue
    if raw is None:
        return None
    return gip_ids(site, raw, authority_key=props_authority_key(site, props, authority_key))


def split_level_objectid(site: dict | None, level_oid) -> tuple[int, str | None]:
    """``(source_id, authority_key)``. Reserved bands apply without YAML."""
    site = site or {}
    oid = _as_int(level_oid, "level objectid")
    stride = level_rules(site).road_id_stride
    if site.get("authorities"):
        for auth in load_authorities(site):
            if auth.road_id_offset <= oid < auth.road_id_offset + stride:
                return oid - auth.road_id_offset, auth.key
    for key, offset in RESERVED_ROAD_ID_OFFSETS.items():
        if offset <= oid < offset + stride:
            return oid - offset, key
    raise SystemExit(f"objectid {oid} liegt in keinem Behörden-Nummernraum")


def stamp_gip_props(
    site: dict | None,
    props: dict | None,
    *,
    authority_key: str | None = None,
) -> dict:
    """Rewrite ``OBJECTID`` to the level id; keep the service id as source."""
    out = dict(props or {})
    key = props_authority_key(site, out, authority_key)
    ids = gip_ids_from_props(site, out, authority_key=key)
    if ids is None:
        return out
    level, source = ids
    out["OBJECTID"] = int(level)
    out["SOURCE_OBJECTID"] = int(source)
    out["_autoroad_source_id"] = int(source)
    out["_autoroad_authority"] = key
    return out


def stamp_gip_features(
    site: dict | None,
    features: list[dict],
    *,
    authority_key: str | None = None,
) -> list[dict]:
    """Stamp every Feature's properties. Does not mutate the input list items."""
    key = authority_key or gip_authority_key(site)
    out: list[dict] = []
    for feat in features or []:
        copy = dict(feat)
        copy["properties"] = stamp_gip_props(
            site, feat.get("properties") or {}, authority_key=key
        )
        out.append(copy)
    return out


def validate_authority_oids(
    site: dict | None,
    features: list[dict],
    authority_key: str | None = None,
) -> str:
    """Abort if a stamped OBJECTID is outside the home authority band."""
    key = authority_key or gip_authority_key(site)
    offset, stride = authority_band(site, key)
    lo, hi = offset, offset + stride
    foreign: list[int] = []
    missing = 0
    for feat in features or []:
        props = feat.get("properties") or {}
        raw = props.get("OBJECTID")
        if raw is None or raw == "":
            missing += 1
            continue
        try:
            oid = int(raw)
        except (TypeError, ValueError):
            foreign.append(raw)  # type: ignore[arg-type]
            continue
        if not (lo <= oid < hi):
            foreign.append(oid)
    if missing:
        raise SystemExit(
            f"Behörde {key}: {missing} Feature(s) ohne OBJECTID nach dem Stempeln"
        )
    if foreign:
        shown = ", ".join(str(x) for x in foreign[:8])
        extra = "" if len(foreign) <= 8 else f" … (+{len(foreign) - 8})"
        raise SystemExit(
            f"Behörde {key}: OBJECTID außerhalb {lo}…{hi - 1}: {shown}{extra}"
        )
    return key


def stamp_road_layer_file(
    path: Path,
    site: dict | None,
    authority_key: str,
    *,
    persist: bool = True,
) -> dict:
    """Stamp ``OBJECTID`` to the level id, validate the band, optionally rewrite."""
    data = _load_geojson(path)
    key = str(authority_key).strip()
    feats = stamp_gip_features(site, data.get("features") or [], authority_key=key)
    validate_authority_oids(site, feats, key)
    data["features"] = feats
    if persist:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def ingest_authority_road_files(site: dict) -> list[Path]:
    """Stamp every ``authorities.*.roads`` file to the reserved / YAML band."""
    written: list[Path] = []
    for auth in load_authorities(site):
        if auth.roads is None:
            continue
        stamp_road_layer_file(auth.roads, site, auth.key, persist=True)
        written.append(auth.roads)
    return written


def transformer_gip_into_working(site: dict, geometry_crs: str | None = None):
    """Cached GIP geometry (native authority CRS) → working CRS."""
    geom = geometry_crs or gip_native_crs(site)
    if _is_wgs84(geom):
        raise SystemExit(
            "GIP-Geometrie liegt in EPSG:4326 / WGS84. "
            "Dienst im lokalen CRS neu laden: python tools\\fetch_gip.py --force"
        )
    working = resolved_working_crs(site)
    if same_crs(geom, working):
        return Transformer.from_crs(geom, working, always_xy=True)
    if site.get("authorities"):
        return transformer_into_working(site, gip_authority_key(site), geom)
    return make_grid_transformer(geom, working, None)


def transformer_gip_fc_into_working(site: dict, data: dict | None = None):
    """Like ``transformer_gip_into_working``, using the FeatureCollection CRS."""
    return transformer_gip_into_working(site, gip_fc_crs(data, site))


def landcover_kind(auth: Authority, props: dict) -> str | None:
    """Map one feature onto a semantic kind.

    Tirol uses the existing field mapper. Every other authority uses only its
    own class table. An unknown class paints nothing. A missing table aborts
    instead of borrowing the Tirol mapper.
    """
    spec = auth.landcover
    if spec is None and auth.key == "tirol":
        spec = TIROL_MAPPER
    if spec == TIROL_MAPPER:
        if auth.key != "tirol":
            raise SystemExit(f"Behörde {auth.key}: Tiroler Landnutzung ist hier nicht zulässig")
        from build_terrain_masks import _tirol_landnutzung_kind

        return _tirol_landnutzung_kind(props)
    if spec == TIROL_WALD_MAPPER:
        if auth.key != "tirol":
            raise SystemExit(f"Behörde {auth.key}: Tiroler Waldfläche ist hier nicht zulässig")
        from build_terrain_masks import _tirol_wald_kind

        return _tirol_wald_kind(props)
    if not isinstance(spec, dict) or not spec:
        raise SystemExit(
            f"Behörde {auth.key}: keine Landbedeckungstabelle "
            f"(Tiroler Klassen werden nicht übernommen)"
        )
    fields = (auth.landcover_field,) if auth.landcover_field else _CLASS_FIELDS
    lookup = {str(key).strip().lower(): kind for key, kind in spec.items()}
    for name in fields:
        if name not in props or props.get(name) is None:
            continue
        hit = lookup.get(str(props.get(name)).strip().lower())
        if hit is not None:
            return hit
    return None


def _polygons_of(geom) -> list:
    if geom is None or geom.is_empty:
        return []
    gtype = geom.geom_type
    if gtype == "Polygon":
        return [geom]
    if gtype == "MultiPolygon":
        return list(geom.geoms)
    if gtype == "GeometryCollection":
        out = []
        for part in geom.geoms:
            out.extend(_polygons_of(part))
        return out
    return []


def _working_validity(site: dict, auth: Authority):
    poly = load_validity_polygon(auth)
    src = _validity_crs(auth)
    tf = transformer_into_working(site, auth.key, src)
    return shp_transform(tf.transform, poly)


def foreign_landcover_rings(site: dict) -> list[tuple[str, list[tuple[float, float]]]]:
    """Foreign land-cover outer rings in working-CRS metres.

    Clipped to that authority's validity, with the Tirol validity removed so
    a foreign class cannot overwrite a Tirol pixel. Tirol itself is not
    painted here.
    """
    if not site.get("authorities"):
        return []
    auths = load_authorities(site)
    foreign = [a for a in auths if a.key != "tirol" and a.landcover_path is not None]
    if not foreign:
        return []
    tirol = next((a for a in auths if a.key == "tirol"), None)
    if tirol is None or tirol.validity is None:
        raise SystemExit(
            "Fremde Landbedeckung braucht das Tiroler Gültigkeitspolygon, "
            "sonst ist die Tiroler Fläche nicht ausgenommen"
        )
    tirol_poly = _working_validity(site, tirol)
    xmin, ymin, xmax, ymax = map(float, apply_working_frame(site)[1])
    level_box = box(xmin, ymin, xmax, ymax)
    rings: list[tuple[str, list[tuple[float, float]]]] = []
    for auth in foreign:
        if not isinstance(auth.landcover, dict) or not auth.landcover:
            raise SystemExit(
                f"Behörde {auth.key}: Landbedeckungsdatei ohne eigene Klassentabelle"
            )
        path = auth.landcover_path
        if path is None:
            continue
        data = _load_geojson(path)
        features = data.get("features") or []
        if not features:
            raise SystemExit(f"Behörde {auth.key}: {path} enthält keine Features")
        own = _working_validity(site, auth)
        allow = own.intersection(level_box).difference(tirol_poly)
        if allow.is_empty:
            continue
        geom_crs = auth.landcover_crs or auth.crs
        tf = transformer_into_working(site, auth.key, geom_crs)
        for feat in features:
            props = feat.get("properties") or {}
            kind = landcover_kind(auth, props)
            if not kind or kind == "road":
                continue
            geom = feat.get("geometry") or {}
            if geom.get("type") not in {"Polygon", "MultiPolygon"}:
                continue
            projected = shp_transform(tf.transform, shape(geom))
            clipped = projected.intersection(allow)
            for poly in _polygons_of(clipped):
                ring = [(float(x), float(y)) for x, y in poly.exterior.coords]
                if len(ring) >= 4:
                    rings.append((kind, ring))
    return rings


def _line_parts(geom: dict) -> list[list[tuple[float, float]]]:
    gtype = (geom or {}).get("type")
    coords = (geom or {}).get("coordinates") or []
    parts: list[list[tuple[float, float]]] = []

    def take(seq) -> None:
        pts = []
        for c in seq:
            if isinstance(c, (list, tuple)) and len(c) >= 2 and isinstance(c[0], (int, float)):
                pts.append((float(c[0]), float(c[1])))
        if len(pts) >= 2:
            parts.append(pts)

    if gtype == "LineString":
        take(coords)
    elif gtype == "MultiLineString":
        for seq in coords:
            take(seq)
    return parts


def road_ends_from_authority(site: dict, auth: Authority) -> list[RoadEnd]:
    """Endpoints of one authority's road file, in working-CRS metres."""
    if auth.roads is None:
        return []
    data = stamp_road_layer_file(auth.roads, site, auth.key, persist=True)
    geom_crs = auth.roads_crs or feature_collection_crs(data) or auth.crs
    tf = transformer_into_working(site, auth.key, geom_crs)
    ends: list[RoadEnd] = []
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        if auth.road_id_field not in props and "OBJECTID" not in props:
            continue
        ids = gip_ids_from_props(site, props, authority_key=auth.key)
        if ids is None:
            field = auth.road_id_field
            if field not in props or props.get(field) is None:
                continue
            ids = interpret_road_id(
                site, props.get(field), auth.key, label=f"{auth.key}.{field}"
            )
        oid, source_id = ids
        for part in _line_parts(feat.get("geometry") or {}):
            x0, y0 = tf.transform(part[0][0], part[0][1])
            x1, y1 = tf.transform(part[-1][0], part[-1][1])
            ends.append(
                RoadEnd(oid, source_id, auth.key, float(x0), float(y0), "start")
            )
            ends.append(
                RoadEnd(oid, source_id, auth.key, float(x1), float(y1), "end")
            )
    return ends


def collect_road_ends(site: dict) -> list[RoadEnd]:
    """Road ends for every authority that has a ``roads`` file or the GIP cache."""
    ends: list[RoadEnd] = []
    gip_key = gip_authority_key(site)
    for auth in load_authorities(site):
        if auth.roads is not None:
            ends.extend(road_ends_from_authority(site, auth))
            continue
        if auth.key != gip_key:
            continue
        from build_bridges import find_gip_geojson

        path = find_gip_geojson(site)
        cached = _load_geojson(path) if path.is_file() else {}
        borrowed = Authority(
            key=auth.key,
            crs=auth.crs,
            road_id_offset=auth.road_id_offset,
            grid=auth.grid,
            roads=path,
            roads_crs=feature_collection_crs(cached) or gip_native_crs(site),
            road_id_field=auth.road_id_field,
        )
        ends.extend(road_ends_from_authority(site, borrowed))
    return ends


def shared_border(site: dict):
    """Union of pairwise validity boundaries, in the working CRS."""
    auths = [a for a in load_authorities(site) if a.validity is not None]
    if len(auths) < 2:
        raise SystemExit("Grenzkante braucht Gültigkeitspolygone von mindestens zwei Behörden")
    polys = [_working_validity(site, a) for a in auths]
    pieces = []
    for i, pa in enumerate(polys):
        for pb in polys[i + 1 :]:
            shared = pa.boundary.intersection(pb.buffer(1.0))
            if not shared.is_empty:
                pieces.append(shared)
    if not pieces:
        raise SystemExit("Keine gemeinsame Gültigkeitskante zwischen den Behörden")
    return unary_union(pieces)


def _cluster(ends: list[RoadEnd], snap_m: float) -> list[list[RoadEnd]]:
    parent = list(range(len(ends)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(len(ends)):
        for j in range(i + 1, len(ends)):
            dx = ends[i].x - ends[j].x
            dy = ends[i].y - ends[j].y
            if (dx * dx + dy * dy) ** 0.5 <= snap_m:
                union(i, j)
    groups: dict[int, list[RoadEnd]] = {}
    for i, end in enumerate(ends):
        groups.setdefault(find(i), []).append(end)
    return list(groups.values())


def _end_record(end: RoadEnd) -> dict:
    return {
        "objectid": end.objectid,
        "source_id": end.source_id,
        "authority": end.authority,
        "x": round(end.x, 3),
        "y": round(end.y, 3),
        "end": end.end,
    }


def match_border_ends(
    ends: list[RoadEnd],
    border,
    *,
    band_m: float,
    snap_m: float,
    confirmed: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Pair endpoints near the validity edge.

    Two endpoints from two authorities become a link. Three or more endpoints
    in one group are not linked; they go to the review list. A confirmed pair
    from the level YAML is added as a link even when its group was ambiguous.
    """
    if snap_m <= 0 or band_m <= 0:
        raise SystemExit("border_snap_m und border_band_m müssen positiv sein")
    near = [e for e in ends if border.distance(Point(e.x, e.y)) <= band_m]
    links: list[dict] = []
    review: list[dict] = []
    confirmed = confirmed or []
    confirmed_pairs = set()
    for row in confirmed:
        a = _as_int(row.get("from_objectid"), "border_confirmed.from_objectid")
        b = _as_int(row.get("to_objectid"), "border_confirmed.to_objectid")
        confirmed_pairs.add(tuple(sorted((a, b))))

    for group in _cluster(near, snap_m):
        if len(group) >= 3:
            review.append(
                {
                    "reason": "mehrdeutig",
                    "endpoints": [_end_record(e) for e in group],
                }
            )
            continue
        if len(group) != 2:
            continue
        a, b = group
        if a.authority == b.authority:
            continue
        pair = tuple(sorted((a.objectid, b.objectid)))
        left, right = (a, b) if a.objectid <= b.objectid else (b, a)
        dist = ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5
        links.append(
            {
                "from_objectid": left.objectid,
                "to_objectid": right.objectid,
                "from_authority": left.authority,
                "to_authority": right.authority,
                "distance_m": round(dist, 3),
                "confirmed": pair in confirmed_pairs,
            }
        )

    for row in confirmed:
        a = _as_int(row.get("from_objectid"), "border_confirmed.from_objectid")
        b = _as_int(row.get("to_objectid"), "border_confirmed.to_objectid")
        pair = tuple(sorted((a, b)))
        if any(
            tuple(sorted((lnk["from_objectid"], lnk["to_objectid"]))) == pair for lnk in links
        ):
            continue
        links.append(
            {
                "from_objectid": pair[0],
                "to_objectid": pair[1],
                "from_authority": row.get("from_authority"),
                "to_authority": row.get("to_authority"),
                "distance_m": None,
                "confirmed": True,
            }
        )
    links.sort(key=lambda row: (row["from_objectid"], row["to_objectid"]))
    return links, review


def write_border_links(site: dict, proc: Path) -> tuple[Path, Path]:
    """Write ``border_links.json`` and ``border_links_review.json``."""
    rules = level_rules(site)
    ends = collect_road_ends(site)
    authorities_present = {e.authority for e in ends}
    if len(authorities_present) < 2:
        raise SystemExit(
            "Grenzverbindung braucht Straßenenden von mindestens zwei Behörden"
        )
    border = shared_border(site)
    links, review = match_border_ends(
        ends,
        border,
        band_m=rules.border_band_m,
        snap_m=rules.border_snap_m,
        confirmed=rules.border_confirmed,
    )
    proc.mkdir(parents=True, exist_ok=True)
    links_path = proc / "border_links.json"
    review_path = proc / "border_links_review.json"
    payload = {
        "crs": resolved_working_crs(site),
        "band_m": rules.border_band_m,
        "snap_m": rules.border_snap_m,
        "links": links,
    }
    links_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    review_path.write_text(
        json.dumps({"crs": payload["crs"], "review": review}, indent=2),
        encoding="utf-8",
    )
    print(
        f"Grenzverbindungen: {len(links)} Paar(e), {len(review)} Prüffall/Prüffälle "
        f"-> {links_path.name}, {review_path.name}"
    )
    return links_path, review_path


def _self_check() -> None:
    import math
    import tempfile

    from shapely.geometry import LineString, Point

    assert level_objectid({"crs": "EPSG:31254"}, 4365, "tirol") == (4365, 4365)

    site = {
        "crs": "EPSG:31254",
        "bbox": [0, 0, 1000, 1000],
        "authorities": {
            "tirol": {"crs": "EPSG:31254", "road_id_offset": 0, "landcover": TIROL_MAPPER},
            "suedtirol": {
                "crs": "EPSG:25832",
                "road_id_offset": 10_000_000,
                "landcover": {"wald": "forest_high", "1": "meadow"},
            },
        },
        "level": {"road_id_stride": 10_000_000},
    }
    assert level_objectid(site, 4365, "tirol") == (4365, 4365)
    assert level_objectid(site, 4365, "suedtirol") == (10_004_365, 4365)
    assert gip_ids({}, 3992) == (3992, 3992)
    assert gip_ids(site, 4365, authority_key="suedtirol") == (10_004_365, 4365)
    assert gip_ids(
        {"sources": {"gip": {"authority": "suedtirol"}}}, 4365
    ) == (10_004_365, 4365)
    assert gip_ids(
        {"sources": {"gip": {"authority": "suedtirol"}}}, 10_004_365
    ) == (10_004_365, 4365)
    try:
        gip_ids({"sources": {"gip": {"authority": "suedtirol"}}}, 20_000_365)
        raise AssertionError("fremde Behörden-ID wurde nicht abgelehnt")
    except SystemExit:
        pass
    stamped = stamp_gip_props(site, {"OBJECTID": 4365}, authority_key="suedtirol")
    assert stamped["OBJECTID"] == 10_004_365
    assert stamped["SOURCE_OBJECTID"] == 4365
    again = stamp_gip_props(site, stamped, authority_key="suedtirol")
    assert again["OBJECTID"] == 10_004_365
    assert split_level_objectid(site, 10_004_365) == (4365, "suedtirol")
    assert split_level_objectid({}, 3992) == (3992, "tirol")
    assert split_level_objectid({}, 10_004_365) == (4365, "suedtirol")
    assert gip_authority_key({"authorities": {"suedtirol": {"crs": "EPSG:25832"}}}) == (
        "suedtirol"
    )
    assert gip_authority_key({"authorities": "suedtirol"}) == "suedtirol"
    assert gip_authority_key({"level": {"authority": "suedtirol"}}) == "suedtirol"

    imst = {
        "crs": "EPSG:31254",
        "bbox": [26316.0, 232503.0, 34316.0, 240503.0],
    }
    assert detect_authorities_in_bbox(imst) == ["tirol"]
    bolzano = {"crs": "EPSG:4326", "bbox": [11.30, 46.45, 11.40, 46.55]}
    assert detect_authorities_in_bbox(bolzano) == ["suedtirol"]
    try:
        require_road_authorities(imst)
        raise AssertionError("fehlende authorities wurden nicht abgelehnt")
    except SystemExit:
        pass
    try:
        require_road_authorities({**imst, "authorities": "leave_empty"})
        raise AssertionError("leave_empty wurde nicht abgelehnt")
    except SystemExit:
        pass
    assert require_road_authorities({**imst, "authorities": "tirol"}) == ["tirol"]
    with tempfile.TemporaryDirectory() as tmp:
        yml = Path(tmp) / "site.yaml"
        yml.write_text("name: t\ncrs: EPSG:31254\nbbox: [0, 0, 1, 1]\n", encoding="utf-8")
        assert upsert_site_authorities(yml, ["tirol"]) == "written"
        assert "authorities: tirol" in yml.read_text(encoding="utf-8")
        assert upsert_site_authorities(yml, ["tirol"]) == "written"
    try:
        require_road_authorities({**bolzano, "authorities": "tirol"})
        raise AssertionError("fehlendes Südtirol wurde nicht abgelehnt")
    except SystemExit:
        pass
    try:
        level_objectid(site, 20_000_365, "suedtirol")
        raise AssertionError("Überlauf wurde nicht abgelehnt")
    except SystemExit:
        pass
    try:
        load_authorities(
            {
                "authorities": {
                    "suedtirol": {
                        "crs": "EPSG:25832",
                        "road_id_offset": 10_000_000,
                        "landcover": TIROL_MAPPER,
                    }
                }
            }
        )
        raise AssertionError("Tirol-Mapper auf fremder Behörde wurde akzeptiert")
    except SystemExit:
        pass

    st = authority_by_key(site, "suedtirol")
    assert landcover_kind(st, {"KLASSE": "hochwald", "wald": "nope"}) is None
    assert landcover_kind(st, {"KLASSE": "wald"}) == "forest_high"
    assert landcover_kind(st, {"CODE": "1"}) == "meadow"
    tirol = authority_by_key(site, "tirol")
    assert (
        landcover_kind(tirol, {"OBJEKT": "LN-WHW", "KLASSE": "Hochwald"})
        == "forest_high"
    )

    try:
        make_grid_transformer("EPSG:25832", "EPSG:31254", Path("data/grids/fehlt.gsb"))
        raise AssertionError("fehlendes Raster wurde nicht abgelehnt")
    except SystemExit as ex:
        assert "fehlt" in str(ex)

    pipeline = grid_pipeline("EPSG:25832", "EPSG:31254", Path("data/grids/example.gsb"))
    assert "+proj=hgridshift" in pipeline
    assert "+inv" in pipeline
    assert "towgs84" not in pipeline
    assert "example.gsb" in pipeline
    tif_pipe = grid_pipeline(
        "EPSG:31254", "EPSG:25832", Path("data/grids") / AT_GIS_GRID_NAME
    )
    assert "+proj=gridshift" in tif_pipe
    assert "+inv +proj=gridshift" not in tif_pipe

    assert gip_native_crs({}) == "EPSG:31254"
    assert gip_native_crs({"sources": {"gip": {"crs": "EPSG:31255"}}}) == "EPSG:31255"
    assert (
        feature_collection_crs(
            {"crs": {"type": "name", "properties": {"name": "EPSG:31254"}}}
        )
        == "EPSG:31254"
    )
    try:
        transformer_gip_into_working({"crs": "EPSG:31254"}, "EPSG:4326")
        raise AssertionError("WGS84-GIP wurde nicht abgelehnt")
    except SystemExit:
        pass
    west_east = make_grid_transformer("EPSG:31254", "EPSG:31255", None)
    xx, yy = west_east.transform(50000.0, 220000.0)
    assert math.isfinite(xx) and math.isfinite(yy)

    # Two squares, Tirol covers more of the bbox. Coordinates are metres in
    # each authority CRS; the vote only needs a stable winner.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tirol_poly = {
            "type": "Polygon",
            "coordinates": [[[0, 0], [800, 0], [800, 1000], [0, 1000], [0, 0]]],
        }
        sued_poly = {
            "type": "Polygon",
            "coordinates": [[[800, 0], [1000, 0], [1000, 1000], [800, 1000], [800, 0]]],
        }
        (tmp_path / "tirol.json").write_text(json.dumps(tirol_poly), encoding="utf-8")
        (tmp_path / "sued.json").write_text(json.dumps(sued_poly), encoding="utf-8")
        vote_site = {
            "crs": "EPSG:31254",
            "bbox": [0, 0, 1000, 1000],
            "authorities": {
                "tirol": {
                    "crs": "EPSG:31254",
                    "road_id_offset": 0,
                    "validity": str(tmp_path / "tirol.json"),
                    "validity_crs": "EPSG:31254",
                },
                "suedtirol": {
                    "crs": "EPSG:31254",
                    "road_id_offset": 10_000_000,
                    "validity": str(tmp_path / "sued.json"),
                    "validity_crs": "EPSG:31254",
                },
            },
            "level": {"crs": "auto"},
        }
        assert majority_authority(vote_site).key == "tirol"
        assert same_crs(resolved_working_crs(vote_site), "EPSG:31254")
        fixed = dict(vote_site)
        fixed["level"] = {"crs": "EPSG:25832"}
        assert resolved_working_crs(fixed) == "EPSG:25832"

        border = LineString([(800, 0), (800, 1000)])
        ends = [
            RoadEnd(4365, 4365, "tirol", 800, 100, "end"),
            RoadEnd(10_004_365, 4365, "suedtirol", 805, 100, "start"),
            RoadEnd(1, 1, "tirol", 800, 500, "end"),
            RoadEnd(10_000_002, 2, "suedtirol", 804, 500, "start"),
            RoadEnd(10_000_003, 3, "suedtirol", 806, 502, "start"),
            RoadEnd(9, 9, "tirol", 100, 100, "end"),
        ]
        links, review = match_border_ends(ends, border, band_m=40, snap_m=15)
        assert len(links) == 1
        assert links[0]["from_objectid"] == 4365
        assert links[0]["to_objectid"] == 10_004_365
        assert len(review) == 1
        assert len(review[0]["endpoints"]) == 3
        assert all(e["objectid"] != 9 for e in review[0]["endpoints"])
        far = Point(100, 100).distance(border)
        assert far > 40

        fc = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"KLASSE": "wald"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[0, 0], [1000, 0], [1000, 1000], [0, 1000], [0, 0]]],
                    },
                },
                {
                    "type": "Feature",
                    "properties": {"KLASSE": "hochwald"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[10, 10], [80, 10], [80, 80], [10, 80], [10, 10]]],
                    },
                },
            ],
        }
        (tmp_path / "tirol_half.json").write_text(
            json.dumps(
                {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [500, 0], [500, 1000], [0, 1000], [0, 0]]],
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "sued_half.json").write_text(
            json.dumps(
                {
                    "type": "Polygon",
                    "coordinates": [[[500, 0], [1000, 0], [1000, 1000], [500, 1000], [500, 0]]],
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "lc.json").write_text(json.dumps(fc), encoding="utf-8")
        clip_site = {
            "crs": "EPSG:31254",
            "bbox": [0, 0, 1000, 1000],
            "authorities": {
                "tirol": {
                    "crs": "EPSG:31254",
                    "road_id_offset": 0,
                    "validity": str(tmp_path / "tirol_half.json"),
                    "landcover": TIROL_MAPPER,
                },
                "suedtirol": {
                    "crs": "EPSG:31254",
                    "road_id_offset": 10_000_000,
                    "validity": str(tmp_path / "sued_half.json"),
                    "landcover_path": str(tmp_path / "lc.json"),
                    "landcover_crs": "EPSG:31254",
                    "landcover": {"wald": "forest_high"},
                },
            },
            "level": {"crs": "EPSG:31254"},
        }
        painted = foreign_landcover_rings(clip_site)
        assert len(painted) == 1
        assert painted[0][0] == "forest_high"
        assert min(p[0] for p in painted[0][1]) >= 500 - 1e-4

    assert math.hypot(3, 4) == 5
    print("authorities self-check ok")


if __name__ == "__main__":
    _self_check()
