"""GIP centerlines with the measured carriageway width as vertex M.

Joins each level's GIP cache to data/roads/gip_widths.json on the source
OBJECTID. Only lines with a measured mean are written. Vertex M is that
full width. QGIS "Variable width buffer (by M value)" treats M as the
diameter. applied is 1 on every line in the file, including
mean_above_class. catalog_applied is the pipeline flag.

A second layer, buffer, holds the same corridors as polygons (radius =
width / 2), so the check does not depend on the QGIS tool.

Output is one GeoPackage per level, EPSG:31254:

    data/processed/<slug>/gip_width_m.gpkg

pyogrio cannot create measured geometries, so the line layer is written
as XY and the M ordinate is then stored in the GeoPackage blob.

Usage:

  python tools\\export_gip_width_m.py --site config/sites/imst.yaml
  python tools\\export_gip_width_m.py --all-known
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import struct
import sys
from pathlib import Path

import geopandas as gpd
from shapely.geometry import shape
from shapely.ops import transform as shp_transform

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from authorities import (  # noqa: E402
    CRS,
    gip_fc_crs,
    gip_source_oid,
    make_grid_transformer,
    same_crs,
)
from build_bridges import find_gip_geojson  # noqa: E402
from gip_catalog import catalog_segment  # noqa: E402
from measure_gip_widths import KNOWN_SITES  # noqa: E402
from site_coords import load_site, processed_dir, site_slug  # noqa: E402

LAYER = "gip"
BUFFER_LAYER = "buffer"
OUT_NAME = "gip_width_m.gpkg"
_ENV_BYTES = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
_STRIDE = (2, 3, 3, 4)  # XY, XYZ, XYM, XYZM


def _dims(typ: int) -> tuple[int, int]:
    """Return (base type, dim) with dim 0=XY, 1=XYZ, 2=XYM, 3=XYZM."""
    if typ > 3007:
        has_z = bool(typ & 0x80000000)
        has_m = bool(typ & 0x40000000)
        return typ & 0xFF, int(has_z) + 2 * int(has_m)
    return typ % 1000, typ // 1000


def _iso_type(base: int, dim: int) -> int:
    return dim * 1000 + base


def _geom_end(wkb: bytes, start: int = 0) -> int:
    order = wkb[start]
    bo = "<" if order == 1 else ">"
    typ = struct.unpack_from(bo + "I", wkb, start + 1)[0]
    base, dim = _dims(typ)
    if base == 2:
        n = struct.unpack_from(bo + "I", wkb, start + 5)[0]
        return start + 9 + n * _STRIDE[dim] * 8
    if base == 5:
        n = struct.unpack_from(bo + "I", wkb, start + 5)[0]
        off = start + 9
        for _ in range(n):
            off = _geom_end(wkb, off)
        return off
    raise ValueError(f"unsupported WKB type {typ}")


def _wkb_with_m(wkb: bytes, m: float) -> bytes:
    """ISO WKB with a constant M on every vertex. XY/Z stay as they are."""
    order = wkb[0]
    bo = "<" if order == 1 else ">"
    typ = struct.unpack_from(bo + "I", wkb, 1)[0]
    base, dim = _dims(typ)
    new_dim = 3 if dim in (1, 3) else 2
    if base == 2:
        n = struct.unpack_from(bo + "I", wkb, 5)[0]
        stride = _STRIDE[dim]
        out = struct.pack(bo + "BII", order, _iso_type(base, new_dim), n)
        off = 9
        for _ in range(n):
            vals = struct.unpack_from(bo + ("d" * stride), wkb, off)
            if dim in (0, 2):
                x, y = vals[0], vals[1]
                out += struct.pack(bo + "ddd", x, y, m)
            else:
                x, y, z = vals[0], vals[1], vals[2]
                out += struct.pack(bo + "dddd", x, y, z, m)
            off += stride * 8
        return out
    if base == 5:
        n = struct.unpack_from(bo + "I", wkb, 5)[0]
        off = 9
        parts: list[bytes] = []
        for _ in range(n):
            end = _geom_end(wkb, off)
            parts.append(_wkb_with_m(wkb[off:end], m))
            off = end
        return struct.pack(bo + "BII", order, _iso_type(base, new_dim), n) + b"".join(parts)
    raise ValueError(f"unsupported WKB type {typ}")


def _iter_m(wkb: bytes):
    order = wkb[0]
    bo = "<" if order == 1 else ">"
    typ = struct.unpack_from(bo + "I", wkb, 1)[0]
    base, dim = _dims(typ)
    if dim not in (2, 3):
        return
    if base == 2:
        n = struct.unpack_from(bo + "I", wkb, 5)[0]
        stride = _STRIDE[dim]
        off = 9
        for _ in range(n):
            vals = struct.unpack_from(bo + ("d" * stride), wkb, off)
            yield float(vals[-1])
            off += stride * 8
        return
    if base == 5:
        n = struct.unpack_from(bo + "I", wkb, 5)[0]
        off = 9
        for _ in range(n):
            end = _geom_end(wkb, off)
            yield from _iter_m(wkb[off:end])
            off = end


def _gpkg_wkb_off(blob: bytes) -> int:
    if not blob or blob[:2] != b"GP":
        raise ValueError("not a GeoPackage geometry")
    env = (blob[3] >> 1) & 7
    try:
        return 8 + _ENV_BYTES[env]
    except KeyError as exc:
        raise ValueError(f"bad envelope code {env}") from exc


def _self_check_wkb() -> None:
    line = struct.pack("<BII", 1, 2, 2)
    line += struct.pack("<dd", 0.0, 1.0)
    line += struct.pack("<dd", 10.0, 1.0)
    out = _wkb_with_m(line, 3.75)
    got = list(_iter_m(out))
    if got != [3.75, 3.75]:
        raise RuntimeError(f"LineString M self-check failed: {got}")
    multi = struct.pack("<BII", 1, 5, 1) + line
    mout = _wkb_with_m(multi, 2.5)
    if list(_iter_m(mout)) != [2.5, 2.5]:
        raise RuntimeError("MultiLineString M self-check failed")


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _attr(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    return json.dumps(value, ensure_ascii=False)


def _to_31254(site: dict, data: dict):
    """GIP cache → EPSG:31254.

    A geographic WGS84/ETRS cache (older extracts) is shifted with the BEV
    MGI grid. 4326 and 4258 share the same horizontal coordinates in PROJ;
    the grid is what lands the line on the orthophoto.
    """
    src = gip_fc_crs(data, site)
    dest = "EPSG:31254"
    if same_crs(src, dest):
        from pyproj import Transformer

        return Transformer.from_crs(src, dest, always_xy=True)
    crs = CRS.from_user_input(src)
    epsg = crs.to_epsg()
    if crs.is_geographic and epsg in (4326, 4258):
        return make_grid_transformer("EPSG:4258", dest, None)
    return make_grid_transformer(src, dest, None)


def _project(geom: dict, tf):
    try:
        shp = shape(geom)
    except Exception:
        return None
    if shp.is_empty:
        return None
    out = shp_transform(lambda x, y, z=None: tf.transform(float(x), float(y)), shp)
    if out.is_empty or out.geom_type not in ("LineString", "MultiLineString"):
        return None
    return out


def _rows_for_site(site: dict) -> tuple[list[dict], str]:
    path = find_gip_geojson(site)
    data = json.loads(path.read_text(encoding="utf-8"))
    tf = _to_31254(site, data)
    rows: list[dict] = []
    checked = False
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        geom = _project(feat.get("geometry") or {}, tf)
        if geom is None:
            continue
        if not checked:
            x0, y0 = geom.coords[0][:2] if geom.geom_type == "LineString" else geom.geoms[0].coords[0][:2]
            if abs(x0) < 1000.0 or abs(y0) < 1000.0:
                raise SystemExit(
                    f"{path.name} still looks geographic after projection ({x0:.3f}, {y0:.3f})"
                )
            checked = True
        oid = gip_source_oid(props)
        seg = catalog_segment(oid) if oid is not None else None
        mean = _num((seg or {}).get("width_mean_m"))
        if mean is None or mean <= 0:
            continue
        row = {k: _attr(v) for k, v in props.items()}
        row["width_mean_m"] = round(mean, 2)
        row["class_width_m"] = _num((seg or {}).get("class_width_m"))
        row["applied"] = 1
        row["catalog_applied"] = 1 if seg and seg.get("applied") else 0
        row["n_valid"] = int(seg.get("n_valid") or 0) if seg else 0
        row["width_skip"] = (seg or {}).get("skip")
        row["geometry"] = geom
        rows.append(row)
    return rows, path.name


def _suspend_triggers(con: sqlite3.Connection, table: str) -> list[str]:
    rows = list(
        con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
            (table,),
        )
    )
    for name, _sql in rows:
        con.execute(f'DROP TRIGGER "{name}"')
    return [sql for _name, sql in rows if sql]


def _assign_m(path: Path) -> tuple[int, int]:
    """Write width_mean_m onto every vertex. Returns (with_m, failed)."""
    con = sqlite3.connect(path)
    try:
        col = con.execute(
            "SELECT column_name, geometry_type_name FROM gpkg_geometry_columns WHERE table_name = ?",
            (LAYER,),
        ).fetchone()
        if col is None:
            raise SystemExit(f"{path} has no layer {LAYER}")
        geom_col, type_name = col
        sqls = _suspend_triggers(con, LAYER)
        n_m = n_fail = 0
        cur = con.execute(f'SELECT fid, "{geom_col}", width_mean_m FROM "{LAYER}"')
        for fid, blob, width in cur.fetchall():
            if width is None or blob is None:
                continue
            try:
                off = _gpkg_wkb_off(blob)
                new = blob[:off] + _wkb_with_m(blob[off:], float(width))
            except (ValueError, struct.error):
                n_fail += 1
                continue
            con.execute(
                f'UPDATE "{LAYER}" SET "{geom_col}" = ? WHERE fid = ?',
                (new, fid),
            )
            n_m += 1
        declared = str(type_name)
        if n_m and "M" not in declared.upper().split():
            declared = f"{declared} M"
        con.execute(
            "UPDATE gpkg_geometry_columns SET geometry_type_name = ?, m = 1 WHERE table_name = ?",
            (declared, LAYER),
        )
        for sql in sqls:
            con.execute(sql)
        con.commit()
    finally:
        con.close()
    return n_m, n_fail


def _verify_m(path: Path) -> None:
    con = sqlite3.connect(path)
    try:
        geom_col = con.execute(
            "SELECT column_name FROM gpkg_geometry_columns WHERE table_name = ?",
            (LAYER,),
        ).fetchone()[0]
        bad = 0
        checked = 0
        for _fid, blob, width in con.execute(
            f'SELECT fid, "{geom_col}", width_mean_m FROM "{LAYER}" WHERE width_mean_m IS NOT NULL'
        ):
            off = _gpkg_wkb_off(blob)
            ms = list(_iter_m(blob[off:]))
            checked += 1
            if not ms or any(not math.isclose(v, float(width), abs_tol=1e-6) for v in ms):
                bad += 1
        empty = con.execute(
            f'SELECT COUNT(*) FROM "{LAYER}" WHERE width_mean_m IS NULL'
        ).fetchone()[0]
    finally:
        con.close()
    if bad or checked == 0 or empty:
        raise SystemExit(
            f"M check failed for {path}: bad={bad} checked={checked} without_m={empty}"
        )
    print(f"  M check ok: {checked} lines, every line has M")


def export_site(site: dict) -> Path:
    slug = site_slug(site)
    rows, gip_name = _rows_for_site(site)
    if not rows:
        raise SystemExit(f"No GIP lines for {slug}")
    out = processed_dir(site) / OUT_NAME
    if out.exists():
        try:
            out.unlink()
        except PermissionError:
            alt = out.with_name(out.stem + "_new.gpkg")
            print(f"{out.name} is open in another program. Writing {alt.name}.")
            if alt.exists():
                alt.unlink()
            out = alt
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:31254")
    gdf.to_file(
        out,
        layer=LAYER,
        driver="GPKG",
        layer_metadata={
            "title": "GIP with measured width as M",
            "description": (
                "Every line has vertex M = width_mean_m in metres, including "
                "mean_above_class. applied is 1 on all of them. "
                "Layer buffer is the corridor (radius = width / 2)."
            ),
        },
    )
    n_m, n_fail = _assign_m(out)
    if n_fail:
        raise SystemExit(f"{out}: {n_fail} lines could not take an M value")
    _verify_m(out)
    buffers = gdf.copy()
    buffers["geometry"] = [
        geom.buffer(float(width) / 2.0, resolution=16)
        for geom, width in zip(gdf.geometry, gdf["width_mean_m"])
    ]
    buffers.to_file(out, layer=BUFFER_LAYER, driver="GPKG", mode="a")
    n_above = int((gdf["width_skip"] == "mean_above_class").sum())
    print(
        f"{slug}: {gip_name} -> {out} "
        f"lines={len(gdf)} with_m={n_m} buffers={len(buffers)} "
        f"mean_above_class={n_above}"
    )
    return out


def main() -> None:
    _self_check_wkb()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--site",
        action="append",
        default=[],
        help="Site YAML (repeatable). Default: AUTOROAD_SITE / config/site.yaml",
    )
    ap.add_argument(
        "--all-known",
        action="store_true",
        help="Fernpass, Fernpass Mega, Reschen, Imst",
    )
    args = ap.parse_args()
    paths = list(KNOWN_SITES) if args.all_known else list(args.site)
    if not paths:
        paths = [None]
    for p in paths:
        site = load_site(p)
        print(f"=== {site_slug(site)} ===")
        export_site(site)


if __name__ == "__main__":
    main()
