"""Administrative authorities for a level that crosses a map boundary.

Absent ``authorities`` in the site YAML, callers keep today's single-CRS
behaviour. With the block:

* the working CRS is the authority whose validity polygon covers the largest
  part of the level bbox (or a fixed ``level.crs``)
* geometry from a smaller authority is reprojected only through that
  authority's official grid; a missing file aborts
* road ``objectid`` stays an int: offset + local id
  (Tirol 0, Südtirol +10_000_000, Schweiz +20_000_000; stride 10_000_000)
* border continuations are automatic for an unambiguous pair and a review
  list from three endpoints onward
* land-cover classes of a foreign authority map through that authority's
  table and are clipped to its validity polygon
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pyproj import CRS, Transformer
from shapely.geometry import Point, box, shape
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]

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
_DEFAULT_BAND_M = 40.0
_DEFAULT_SNAP_M = 15.0
_LAEA = "EPSG:3035"


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


def load_authorities(site: dict) -> list[Authority]:
    """Authorities in YAML order. Empty when the site has no such block."""
    raw = site.get("authorities")
    if not raw:
        return []
    if not isinstance(raw, dict):
        raise SystemExit("authorities muss ein Block je Behörde sein")
    out: list[Authority] = []
    seen_offset: dict[int, str] = {}
    for key, spec in raw.items():
        if not isinstance(spec, dict):
            raise SystemExit(f"Behörde {key} muss ein Block sein")
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


def gip_authority_key(site: dict) -> str:
    gip = (site.get("sources") or {}).get("gip") or {}
    return str(gip.get("authority") or "tirol")


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
    """Working CRS. Without authorities this is ``site.crs``."""
    if not site.get("authorities"):
        return str(site.get("crs") or "EPSG:31254")
    rules = level_rules(site)
    if rules.crs_mode.lower() != "auto":
        return rules.crs_mode
    return majority_authority(site).crs


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


def grid_pipeline(src_crs: str, dst_crs: str, grid: Path) -> str:
    src = CRS.from_user_input(src_crs)
    dst = CRS.from_user_input(dst_crs)
    grids = grid.as_posix()
    if " " in grids:
        grids = f'"{grids}"'
    return (
        "+proj=pipeline "
        + _steps_to_radians(src)
        + f" +step +proj=hgridshift +grids={grids} "
        + _steps_from_radians(dst)
    )


def make_grid_transformer(src_crs: str, dst_crs: str, grid: Path | None):
    """Reproject with the named NTv2 grid. Missing file aborts."""
    if same_crs(src_crs, dst_crs):
        return Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    if grid is None or not grid.is_file():
        shown = grid if grid is not None else "(nicht gesetzt)"
        raise SystemExit(
            f"Umrechnungsraster fehlt ({src_crs} → {dst_crs}): {shown}"
        )
    return Transformer.from_pipeline(grid_pipeline(src_crs, dst_crs, grid))


def transformer_into_working(site: dict, authority_key: str, geometry_crs: str):
    """Geometry CRS → working CRS.

    Same CRS: identity. Authority CRS already equal to the working CRS:
    ordinary pyproj (the in-country WGS84 hop used for Tirol GIP). A foreign
    authority CRS uses that authority's grid after the file is brought into
    the authority CRS. No Helmert fallback.
    """
    auth = authority_by_key(site, authority_key)
    working = resolved_working_crs(site)
    if same_crs(geometry_crs, working):
        return Transformer.from_crs(geometry_crs, working, always_xy=True)
    if same_crs(auth.crs, working):
        return Transformer.from_crs(geometry_crs, working, always_xy=True)
    grid_tf = make_grid_transformer(auth.crs, working, auth.grid)
    if same_crs(geometry_crs, auth.crs):
        return grid_tf
    into_native = Transformer.from_crs(geometry_crs, auth.crs, always_xy=True)
    return ChainTransform([into_native, grid_tf])


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
    local = _as_int(local_id, "lokale Straßen-ID")
    if not site.get("authorities"):
        return local, local
    auth = authority_by_key(site, authority_key)
    stride = level_rules(site).road_id_stride
    if local < 0 or local >= stride:
        raise SystemExit(
            f"Behörde {auth.key}: lokale ID {local} liegt außerhalb des "
            f"Stufenabstands {stride}"
        )
    oid = auth.road_id_offset + local
    for other in load_authorities(site):
        if other.key == auth.key:
            continue
        if other.road_id_offset <= oid < other.road_id_offset + stride:
            raise SystemExit(
                f"Behörde {auth.key}: ID {oid} fällt in den Nummernraum von {other.key}"
            )
    return oid, local


def gip_ids(
    site: dict | None,
    raw_oid,
    *,
    authority_key: str | None = None,
) -> tuple[int, int]:
    """``(level_objectid, source_id)``. No ``authorities`` → identity."""
    site = site or {}
    local = _as_int(raw_oid, "GIP OBJECTID")
    if not site.get("authorities"):
        return local, local
    return level_objectid(site, local, authority_key or gip_authority_key(site))


def gip_source_oid(props: dict | None) -> int | None:
    """Unshifted id from stamped or raw GIP properties."""
    props = props if isinstance(props, dict) else {}
    for key in ("_autoroad_source_id", "SOURCE_OBJECTID", "OBJECTID"):
        raw = props.get(key)
        if raw is None or raw == "":
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def gip_ids_from_props(
    site: dict | None,
    props: dict | None,
    *,
    authority_key: str | None = None,
) -> tuple[int, int] | None:
    """Idempotent: source field wins, then ``OBJECTID`` is treated as local."""
    src = gip_source_oid(props)
    if src is None:
        return None
    return gip_ids(site, src, authority_key=authority_key)


def split_level_objectid(site: dict | None, level_oid) -> tuple[int, str | None]:
    """``(source_id, authority_key)``. No ``authorities`` → ``(oid, None)``."""
    site = site or {}
    oid = _as_int(level_oid, "level objectid")
    if not site.get("authorities"):
        return oid, None
    stride = level_rules(site).road_id_stride
    for auth in load_authorities(site):
        if auth.road_id_offset <= oid < auth.road_id_offset + stride:
            return oid - auth.road_id_offset, auth.key
    raise SystemExit(f"objectid {oid} liegt in keinem Behörden-Nummernraum")


def stamp_gip_props(
    site: dict | None,
    props: dict | None,
    *,
    authority_key: str | None = None,
) -> dict:
    """Rewrite ``OBJECTID`` to the level id; keep the service id as source."""
    out = dict(props or {})
    ids = gip_ids_from_props(site, out, authority_key=authority_key)
    if ids is None:
        return out
    level, source = ids
    out["OBJECTID"] = int(level)
    out["SOURCE_OBJECTID"] = int(source)
    out["_autoroad_source_id"] = int(source)
    if site and site.get("authorities"):
        out["_autoroad_authority"] = authority_key or gip_authority_key(site)
    return out


def stamp_gip_features(site: dict | None, features: list[dict]) -> list[dict]:
    """Stamp every Feature's properties. Does not mutate the input list items."""
    out: list[dict] = []
    for feat in features or []:
        copy = dict(feat)
        copy["properties"] = stamp_gip_props(site, feat.get("properties") or {})
        out.append(copy)
    return out


def transformer_gip_into_working(site: dict):
    """GIP WFS geometry is EPSG:4326 → working CRS."""
    if site.get("authorities"):
        return transformer_into_working(site, gip_authority_key(site), "EPSG:4326")
    return Transformer.from_crs(
        "EPSG:4326", str(site.get("crs") or "EPSG:31254"), always_xy=True
    )


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
    data = _load_geojson(auth.roads)
    geom_crs = auth.roads_crs or auth.crs
    tf = transformer_into_working(site, auth.key, geom_crs)
    ends: list[RoadEnd] = []
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        if auth.road_id_field not in props or props.get(auth.road_id_field) is None:
            continue
        local = _as_int(props.get(auth.road_id_field), f"{auth.key}.{auth.road_id_field}")
        oid, source_id = level_objectid(site, local, auth.key)
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
        borrowed = Authority(
            key=auth.key,
            crs=auth.crs,
            road_id_offset=auth.road_id_offset,
            grid=auth.grid,
            roads=path,
            roads_crs="EPSG:4326",
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
    stamped = stamp_gip_props(site, {"OBJECTID": 4365}, authority_key="suedtirol")
    assert stamped["OBJECTID"] == 10_004_365
    assert stamped["SOURCE_OBJECTID"] == 4365
    again = stamp_gip_props(site, stamped, authority_key="suedtirol")
    assert again["OBJECTID"] == 10_004_365
    assert split_level_objectid(site, 10_004_365) == (4365, "suedtirol")
    assert split_level_objectid({}, 3992) == (3992, None)
    try:
        level_objectid(site, 10_000_000, "suedtirol")
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
    assert "towgs84" not in pipeline
    assert "example.gsb" in pipeline

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
