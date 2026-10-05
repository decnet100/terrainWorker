"""Landcover catalog codes + treatment groups.

Raw authority codes stay in ``landcover_codes.tif`` (uint16, 0 = nodata).
``config/landcover_treatment.yaml`` maps codes → soft groups used by terrain
masks and biome compose. Group membership can change without re-fetching.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TREATMENT_PATH = ROOT / "config" / "landcover_treatment.yaml"

# Soft groups (stable names for masks / biome compose).
TREATMENT_GROUPS = (
    "forest_tall",
    "forest_scrub",
    "meadow",
    "bare",
    "building",
    "water",
)

# Compact uint8 ids written to landcover_treatment.tif (0 = untreated).
TREATMENT_ID = {name: i + 1 for i, name in enumerate(TREATMENT_GROUPS)}
TREATMENT_NAME = {i + 1: name for i, name in enumerate(TREATMENT_GROUPS)}

# Terrain class ints — keep in sync with build_terrain_masks / compose_biomes.
CLASS_DIRT = 1
CLASS_FOREST = 4
CLASS_WATER = 5
CLASS_CONCRETE = 6
CLASS_GRASS = 7

GROUP_TO_LANDUSE = {
    "forest_tall": CLASS_FOREST,
    "forest_scrub": CLASS_FOREST,
    "meadow": CLASS_GRASS,
    "bare": CLASS_DIRT,
    "building": CLASS_CONCRETE,
    "water": CLASS_WATER,
}


def treatment_path(site: dict | None = None) -> Path:
    raw = None
    if site:
        lc = (site.get("sources") or {}).get("landcover") or {}
        raw = lc.get("treatment") or (site.get("landcover") or {}).get("treatment")
    if raw:
        p = Path(str(raw))
        return p if p.is_absolute() else ROOT / p
    return DEFAULT_TREATMENT_PATH


def load_treatment(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_TREATMENT_PATH
    if not p.is_file():
        raise SystemExit(f"Landcover treatment missing: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"{p.name}: root must be a mapping")
    if str(data.get("schema") or "") != "landcover_treatment_v1":
        raise SystemExit(f"{p.name}: expected schema landcover_treatment_v1")
    return data


def catalog_spec(treatment: dict[str, Any], catalog: str) -> dict[str, Any]:
    cats = treatment.get("catalogs") or {}
    spec = cats.get(catalog)
    if not isinstance(spec, dict):
        raise SystemExit(f"Unknown landcover catalog {catalog!r}")
    return spec


def code_to_group(treatment: dict[str, Any], catalog: str) -> dict[int, str]:
    """Map catalog code → treatment group name."""
    spec = catalog_spec(treatment, catalog)
    table = spec.get("treatment") or {}
    out: dict[int, str] = {}
    for group, codes in table.items():
        g = str(group)
        if g not in TREATMENT_GROUPS:
            raise SystemExit(f"Unknown treatment group {g!r} in catalog {catalog}")
        if not isinstance(codes, (list, tuple)):
            raise SystemExit(f"treatment.{g} must be a list of codes")
        for c in codes:
            out[int(c)] = g
    return out


def code_labels(treatment: dict[str, Any], catalog: str) -> dict[int, str]:
    spec = catalog_spec(treatment, catalog)
    raw = spec.get("codes") or {}
    return {int(k): str(v) for k, v in raw.items()}


def nodata_value(treatment: dict[str, Any] | None = None) -> int:
    if treatment is None:
        return 0
    return int(treatment.get("nodata") or 0)


def codes_path(proc: Path) -> Path:
    return proc / "landcover_codes.tif"


def codes_meta_path(proc: Path) -> Path:
    return proc / "landcover_codes.meta.json"


def treatment_raster_path(proc: Path) -> Path:
    return proc / "landcover_treatment.tif"


def apply_treatment_ids(
    codes: np.ndarray,
    *,
    catalog: str,
    treatment: dict[str, Any] | None = None,
) -> np.ndarray:
    """uint8 treatment id raster (0 = untreated / nodata)."""
    treat = treatment or load_treatment()
    mapping = code_to_group(treat, catalog)
    nodata = nodata_value(treat)
    src = np.asarray(codes)
    out = np.zeros(src.shape, dtype=np.uint8)
    for code, group in mapping.items():
        out[src == int(code)] = TREATMENT_ID[group]
    out[src == nodata] = 0
    return out


def treatment_masks(
    codes: np.ndarray,
    *,
    catalog: str,
    treatment: dict[str, Any] | None = None,
) -> dict[str, np.ndarray]:
    ids = apply_treatment_ids(codes, catalog=catalog, treatment=treatment)
    return {name: ids == tid for name, tid in TREATMENT_ID.items()}


def landuse_from_treatment(
    codes: np.ndarray,
    *,
    catalog: str,
    treatment: dict[str, Any] | None = None,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Terrain CLASS_* grid (-1 unset) + biome extras (compatible names)."""
    masks = treatment_masks(codes, catalog=catalog, treatment=treatment)
    landuse = np.full(codes.shape, -1, dtype=np.int8)
    for group, cls in GROUP_TO_LANDUSE.items():
        landuse[masks[group]] = cls
    extras = {
        "forest_high": masks["forest_tall"],
        "forest_scrub": masks["forest_scrub"],
        "water": masks["water"],
        "veg_hoch": masks["forest_tall"],
        "veg_mittel": masks["forest_scrub"],
        "veg_niedrig": masks["meadow"],
        "bare": masks["bare"],
        "building": masks["building"],
        "landcover_codes": np.asarray(codes, dtype=np.uint16),
        "treatment_id": apply_treatment_ids(
            codes, catalog=catalog, treatment=treatment
        ),
    }
    return landuse, extras


def write_codes_meta(
    path: Path,
    *,
    catalog: str,
    codes: np.ndarray,
    site: dict,
    source: dict[str, Any],
    treatment: dict[str, Any] | None = None,
) -> None:
    treat = treatment or load_treatment()
    nodata = nodata_value(treat)
    present = sorted({int(v) for v in np.unique(codes) if int(v) != nodata})
    labels = code_labels(treat, catalog)
    mapping = code_to_group(treat, catalog)
    legend = []
    for code in present:
        legend.append(
            {
                "code": code,
                "label": labels.get(code, ""),
                "treatment": mapping.get(code),
            }
        )
    meta = {
        "schema": "landcover_codes_v1",
        "catalog": catalog,
        "nodata": nodata,
        "crs": site.get("crs"),
        "bbox": list(map(float, site.get("bbox") or [])),
        "size": [int(codes.shape[1]), int(codes.shape[0])],
        "dtype": "uint16",
        "source": source,
        "treatment_file": str(
            treatment_path(site).relative_to(ROOT)
        ).replace("\\", "/"),
        "code_counts": {
            str(int(c)): int((codes == c).sum())
            for c in present
        },
        "legend": legend,
    }
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def load_codes_bundle(proc: Path) -> tuple[np.ndarray, dict[str, Any]] | None:
    """Return (codes, meta) or None if the catalog raster is missing."""
    import tifffile as tiff

    path = codes_path(proc)
    meta_p = codes_meta_path(proc)
    if not path.is_file() or not meta_p.is_file():
        return None
    arr = np.asarray(tiff.imread(path))
    if arr.ndim == 3:
        arr = arr[..., 0]
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    return arr.astype(np.uint16, copy=False), meta
