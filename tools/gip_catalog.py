"""Central GIP road store: measured widths + a few geographic overrides.

Site YAML stays for level presentation (bbox, materials, MeshRoad recipes).
Properties of a GIP OBJECTID — width samples, not-tunnel flags — live here so
overlapping maps (Fernpass 4096/8192, later Imst) share one answer.

    data/roads/gip_widths.json      measured left/right at start/mid/end
    data/roads/gip_overrides.yaml   not_tunnel, side_cut extras, rare widths
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROADS_DIR = ROOT / "data" / "roads"
WIDTHS_PATH = ROADS_DIR / "gip_widths.json"
OVERRIDES_PATH = ROADS_DIR / "gip_overrides.yaml"

# Landnutzung OBJEKT codes that are a road surface (not rail / airfield).
LN_STREET_CODES = frozenset({"LN-VSU", "LN-VSO", "LN-VFW", "LN-VPR"})

_OVERRIDES_CACHE: dict[str, Any] | None = None
_WIDTHS_CACHE: dict[str, Any] | None = None


def roads_dir() -> Path:
    ROADS_DIR.mkdir(parents=True, exist_ok=True)
    return ROADS_DIR


def load_overrides(*, force: bool = False) -> dict[str, Any]:
    global _OVERRIDES_CACHE
    if _OVERRIDES_CACHE is not None and not force:
        return _OVERRIDES_CACHE
    if not OVERRIDES_PATH.is_file():
        _OVERRIDES_CACHE = {}
        return _OVERRIDES_CACHE
    raw = yaml.safe_load(OVERRIDES_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raw = {}
    _OVERRIDES_CACHE = raw
    return raw


def load_widths(*, force: bool = False) -> dict[str, Any]:
    global _WIDTHS_CACHE
    if _WIDTHS_CACHE is not None and not force:
        return _WIDTHS_CACHE
    if not WIDTHS_PATH.is_file():
        _WIDTHS_CACHE = {"version": 1, "crs": "EPSG:31254", "segments": {}}
        return _WIDTHS_CACHE
    raw = json.loads(WIDTHS_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raw = {}
    segs = raw.get("segments")
    if not isinstance(segs, dict):
        raw["segments"] = {}
    _WIDTHS_CACHE = raw
    return raw


def save_widths(data: dict[str, Any]) -> Path:
    global _WIDTHS_CACHE
    roads_dir()
    segs = data.get("segments") or {}
    ordered = {
        k: segs[k]
        for k in sorted(segs.keys(), key=lambda s: (len(s), s))
    }
    out = {
        "version": int(data.get("version") or 1),
        "crs": str(data.get("crs") or "EPSG:31254"),
        "ln_codes": list(data.get("ln_codes") or sorted(LN_STREET_CODES)),
        "segments": ordered,
    }
    WIDTHS_PATH.write_text(
        json.dumps(out, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _WIDTHS_CACHE = out
    return WIDTHS_PATH


def _int_set(raw) -> set[int]:
    out: set[int] = set()
    if raw is None or raw is False:
        return out
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    for x in items:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out


def catalog_not_tunnel_oids() -> set[int]:
    return _int_set(load_overrides().get("not_tunnel_objectids"))


def catalog_side_cut_oids() -> set[int]:
    return _int_set(load_overrides().get("side_cut_preserve_objectids"))


def catalog_width_by_objectid() -> dict[int, float]:
    raw = load_overrides().get("width_by_objectid") or {}
    out: dict[int, float] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if v is None:
            continue
        try:
            out[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def catalog_width_by_str_code() -> dict[str, float]:
    raw = load_overrides().get("width_by_str_code") or {}
    out: dict[str, float] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if v is None:
            continue
        code = str(k).strip()
        if not code:
            continue
        try:
            out[code] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def catalog_segment(oid: int | str) -> dict[str, Any] | None:
    try:
        key = str(int(oid))
    except (TypeError, ValueError):
        return None
    seg = (load_widths().get("segments") or {}).get(key)
    return seg if isinstance(seg, dict) else None


def measured_width_m(oid: int | str) -> float | None:
    """Mean carriageway width if the catalog marked the sample as applicable."""
    seg = catalog_segment(oid)
    if not seg or not seg.get("applied"):
        return None
    w = seg.get("width_mean_m")
    if w is None:
        return None
    try:
        val = float(w)
    except (TypeError, ValueError):
        return None
    return val if val >= 1.5 else None


def site_width_by_objectid(site: dict | None) -> dict[int, float]:
    raw = ((site or {}).get("beamng") or {}).get("roads") or {}
    raw = raw.get("width_by_objectid") or {}
    out: dict[int, float] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if v is None:
            continue
        try:
            out[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out
