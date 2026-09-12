"""Create empty annotations GeoPackage (road_edge, guardrail, centerline).

Schema: docs/ANNOTATIONS.md
Default output from active site (config/site.yaml or AUTOROAD_SITE).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from site_coords import annotations_gpkg, load_site  # noqa: E402


def _seed_line(attrs: dict, crs: str) -> gpd.GeoDataFrame:
    """One temporary LineString so GPKG registers LINESTRING + columns."""
    row = dict(attrs)
    row["geometry"] = LineString([(0.0, 0.0), (1.0, 0.0)])
    return gpd.GeoDataFrame([row], geometry="geometry", crs=crs)


def create_gpkg(path: Path, crs: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    layers = {
        "road_edge": _seed_line(
            {
                "id": "_schema",
                "side": "left",
                "road_ref": None,
                "source": None,
                "notes": None,
            },
            crs,
        ),
        "guardrail": _seed_line(
            {
                "id": "_schema",
                "side": "left",
                "present": True,
                "kind": "wbeam",
                "gap_reason": None,
                "road_ref": None,
                "section_m": None,
                "notes": None,
            },
            crs,
        ),
        "centerline": _seed_line(
            {
                "id": "_schema",
                "width_m": None,
                "highway": None,
                "notes": None,
            },
            crs,
        ),
    }

    for i, (name, gdf) in enumerate(layers.items()):
        gdf.to_file(path, layer=name, driver="GPKG", mode="w" if i == 0 else "a")

    # Drop seed rows; force LINESTRING in GeoPackage metadata for QGIS.
    with sqlite3.connect(path) as conn:
        for name in layers:
            conn.execute(f'DELETE FROM "{name}"')
            conn.execute(
                "UPDATE gpkg_geometry_columns SET geometry_type_name = 'LINESTRING' "
                "WHERE table_name = ?",
                (name,),
            )
        conn.commit()

    return path


def main() -> None:
    site = load_site()
    default_crs = str(site.get("crs", "EPSG:31254"))
    default_out = annotations_gpkg(site)

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(default_out), help="Output .gpkg path")
    ap.add_argument("--crs", default=default_crs, help="CRS, e.g. EPSG:31254")
    args = ap.parse_args()

    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out

    create_gpkg(out, args.crs)
    print(f"Created {out}")
    print(f"CRS={args.crs} layers=road_edge,guardrail,centerline (empty)")
    print("Edit in QGIS; see docs/ANNOTATIONS.md")


if __name__ == "__main__":
    main()
