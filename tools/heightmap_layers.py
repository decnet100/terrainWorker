"""Immutable DGM + sparse heightmap proposals, then one compose.

Each build_* that touches terrain writes a layer (replace Z or add Δz) with a
weight mask. ``compose_heightmap`` always starts from the DGM
(``heightmap_<N>.png``) and never overwrites it.

Layer order is ``LAYER_SPECS`` (higher replace priority wins). Change that dict
when the pipeline order is decided; do not bake order into the tools.

Provisional defaults (one span replace for bridge/gallery/tunnel; water additive):
  water    add      10
  road_bed replace  40
  span     replace  55   # MeshRoad structures: parts bridge + gallery
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
USER_LEVELS = (
    Path.home()
    / "AppData"
    / "Local"
    / "BeamNG"
    / "BeamNG.drive"
    / "current"
    / "levels"
)

# name -> {mode, priority}. Higher replace priority is applied later = wins.
LAYER_SPECS: dict[str, dict] = {
    "water": {"mode": "add", "priority": 10},
    "road_bed": {"mode": "replace", "priority": 40},
    "span": {"mode": "replace", "priority": 55},
}
SPAN_PARTS = ("bridge", "gallery")

LAYERS_DIRNAME = "heightmap_layers"
MANIFEST_NAME = "manifest.json"
W_EPS = 1e-4


def _layer_file(proc: Path, rel: str) -> Path:
    return proc.joinpath(*str(rel).replace("\\", "/").split("/"))


def layers_dir(proc: Path) -> Path:
    d = proc / LAYERS_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def manifest_path(proc: Path) -> Path:
    return layers_dir(proc) / MANIFEST_NAME


def load_manifest(proc: Path) -> dict:
    p = manifest_path(proc)
    if not p.is_file():
        return {"version": 1, "layers": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"version": 1, "layers": []}
    data.setdefault("version", 1)
    data.setdefault("layers", [])
    return data


def save_manifest(proc: Path, data: dict) -> None:
    p = manifest_path(proc)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_dgm(proc: Path, size: int) -> tuple[np.ndarray, float]:
    """Pristine DGM meters + max_height_m. Never a bake."""
    meta = json.loads((proc / "heightmap_meta.json").read_text(encoding="utf-8"))
    max_h = float(meta["max_height_m"])
    path = proc / f"heightmap_{size}.png"
    if not path.is_file():
        raise FileNotFoundError(f"Missing DGM heightmap {path}")
    u16 = np.asarray(Image.open(path))
    if u16.dtype != np.uint16:
        u16 = u16.astype(np.uint16)
    elev = u16.astype(np.float64) / 65535.0 * max_h
    return elev, max_h


def composed_path(proc: Path, size: int) -> Path:
    return proc / f"heightmap_{size}_composed.png"


def load_composed_or_dgm(proc: Path, size: int) -> tuple[np.ndarray, float, str]:
    """Meters elev for snapping (guardrails, decals). Prefers compose output."""
    meta = json.loads((proc / "heightmap_meta.json").read_text(encoding="utf-8"))
    max_h = float(meta["max_height_m"])
    for path, label in (
        (composed_path(proc, size), "composed"),
        (proc / f"heightmap_{size}.png", "dgm"),
    ):
        if path.is_file():
            u16 = np.asarray(Image.open(path))
            if u16.dtype != np.uint16:
                u16 = u16.astype(np.uint16)
            return u16.astype(np.float64) / 65535.0 * max_h, max_h, label
    raise FileNotFoundError(f"No heightmap in {proc}")


def _encode_z_u16(z_m: np.ndarray, max_h: float) -> np.ndarray:
    return np.clip(np.round(z_m / max(max_h, 1e-6) * 65535.0), 0, 65535).astype(np.uint16)


def _decode_z_u16(u16: np.ndarray, max_h: float) -> np.ndarray:
    return u16.astype(np.float64) / 65535.0 * max_h


def _encode_w_u8(weight: np.ndarray) -> np.ndarray:
    return np.clip(np.round(np.asarray(weight, dtype=np.float64) * 255.0), 0, 255).astype(
        np.uint8
    )


def _decode_w_u8(u8: np.ndarray) -> np.ndarray:
    return u8.astype(np.float64) / 255.0


def _encode_dz_u16(delta_m: np.ndarray) -> np.ndarray:
    """Signed mm in uint16 with +32768 bias (PNG I;16 is unsigned)."""
    mm = np.round(np.asarray(delta_m, dtype=np.float64) * 1000.0) + 32768.0
    return np.clip(mm, 0, 65535).astype(np.uint16)


def _decode_dz_u16(u16: np.ndarray) -> np.ndarray:
    return (u16.astype(np.float64) - 32768.0) / 1000.0


def _upsert_layer(proc: Path, entry: dict) -> None:
    data = load_manifest(proc)
    name = entry["name"]
    layers = [x for x in data["layers"] if x.get("name") != name]
    layers.append(entry)
    layers.sort(key=lambda x: (int(x.get("priority") or 0), str(x.get("name") or "")))
    data["layers"] = layers
    spec = LAYER_SPECS.get(name) or {}
    data["size"] = entry.get("size", data.get("size"))
    data["max_height_m"] = entry.get("max_height_m", data.get("max_height_m"))
    data["dgm"] = f"heightmap_{data['size']}.png"
    data["composed"] = f"heightmap_{data['size']}_composed.png"
    data.setdefault("note", "Edit LAYER_SPECS in heightmap_layers.py to change order.")
    _ = spec
    save_manifest(proc, data)


def drop_layer(proc: Path, name: str) -> bool:
    """Remove a layer's rasters + manifest row. Returns True if it existed."""
    data = load_manifest(proc)
    found = [x for x in data["layers"] if x.get("name") == name]
    if not found:
        return False
    data["layers"] = [x for x in data["layers"] if x.get("name") != name]
    save_manifest(proc, data)
    d = layers_dir(proc)
    for suffix in ("_z.png", "_w.png", "_dz.png"):
        p = d / f"{name}{suffix}"
        if p.is_file():
            p.unlink()
    if name == "span":
        for part in SPAN_PARTS:
            for suffix in ("_z.png", "_w.png"):
                p = d / f"span_{part}{suffix}"
                if p.is_file():
                    p.unlink()
    print(f"Heightmap layer dropped: {name}")
    return True


def write_replace_layer(
    proc: Path,
    name: str,
    z_m: np.ndarray,
    weight: np.ndarray,
    *,
    max_h: float,
    size: int | None = None,
) -> dict:
    spec = LAYER_SPECS.get(name)
    if spec is None or spec["mode"] != "replace":
        raise ValueError(f"Unknown replace layer {name!r} — add it to LAYER_SPECS")
    w = np.asarray(weight, dtype=np.float64)
    z = np.asarray(z_m, dtype=np.float64)
    if z.shape != w.shape:
        raise ValueError(f"z/weight shape mismatch {z.shape} vs {w.shape}")
    size = int(size or z.shape[0])
    n = int(np.count_nonzero(w > W_EPS))
    d = layers_dir(proc)
    rel_z = f"{LAYERS_DIRNAME}/{name}_z.png"
    rel_w = f"{LAYERS_DIRNAME}/{name}_w.png"
    Image.fromarray(_encode_z_u16(z, max_h), mode="I;16").save(d / f"{name}_z.png")
    Image.fromarray(_encode_w_u8(w), mode="L").save(d / f"{name}_w.png")
    entry = {
        "name": name,
        "mode": "replace",
        "priority": int(spec["priority"]),
        "size": size,
        "max_height_m": float(max_h),
        "z": rel_z,
        "weight": rel_w,
        "nz": n,
    }
    _upsert_layer(proc, entry)
    print(f"Heightmap layer {name}: replace px={n} priority={spec['priority']}")
    return entry


def write_span_part(
    proc: Path,
    part: str,
    z_m: np.ndarray,
    weight: np.ndarray,
    *,
    max_h: float,
    size: int | None = None,
) -> dict:
    """Write one MeshRoad-structure contribution into the shared ``span`` layer.

    ``part`` is ``bridge`` or ``gallery`` (tunnels use gallery). Re-running one
    tool updates only its part; compose unions both so they cannot fight via
    priority.
    """
    if part not in SPAN_PARTS:
        raise ValueError(f"Unknown span part {part!r} — expected one of {SPAN_PARTS}")
    spec = LAYER_SPECS["span"]
    w = np.asarray(weight, dtype=np.float64)
    z = np.asarray(z_m, dtype=np.float64)
    if z.shape != w.shape:
        raise ValueError(f"z/weight shape mismatch {z.shape} vs {w.shape}")
    size = int(size or z.shape[0])
    n = int(np.count_nonzero(w > W_EPS))
    d = layers_dir(proc)
    rel_z = f"{LAYERS_DIRNAME}/span_{part}_z.png"
    rel_w = f"{LAYERS_DIRNAME}/span_{part}_w.png"
    Image.fromarray(_encode_z_u16(z, max_h), mode="I;16").save(d / f"span_{part}_z.png")
    Image.fromarray(_encode_w_u8(w), mode="L").save(d / f"span_{part}_w.png")

    data = load_manifest(proc)
    entry = next((x for x in data.get("layers") or [] if x.get("name") == "span"), None)
    if entry is None:
        entry = {
            "name": "span",
            "mode": "replace",
            "priority": int(spec["priority"]),
            "size": size,
            "max_height_m": float(max_h),
            "parts": {},
        }
    parts = dict(entry.get("parts") or {})
    parts[part] = {"z": rel_z, "weight": rel_w, "nz": n}
    entry["parts"] = parts
    entry["size"] = size
    entry["max_height_m"] = float(max_h)
    entry.pop("z", None)
    entry.pop("weight", None)
    _upsert_layer(proc, entry)
    print(
        f"Heightmap layer span/{part}: replace px={n} "
        f"parts={sorted(parts)} priority={spec['priority']}"
    )
    return entry


def drop_span_part(proc: Path, part: str) -> bool:
    """Remove one span contribution; keep the other. Drops ``span`` if empty."""
    if part not in SPAN_PARTS:
        raise ValueError(f"Unknown span part {part!r}")
    data = load_manifest(proc)
    entry = next((x for x in data.get("layers") or [] if x.get("name") == "span"), None)
    if entry is None:
        return False
    parts = dict(entry.get("parts") or {})
    if part not in parts:
        return False
    parts.pop(part, None)
    d = layers_dir(proc)
    for suffix in ("_z.png", "_w.png"):
        p = d / f"span_{part}{suffix}"
        if p.is_file():
            p.unlink()
    if not parts:
        return drop_layer(proc, "span")
    entry["parts"] = parts
    _upsert_layer(proc, entry)
    print(f"Heightmap layer span/{part} dropped; remaining={sorted(parts)}")
    return True


def _union_span_parts(
    proc: Path, entry: dict, shape: tuple[int, ...], max_h: float
) -> tuple[np.ndarray, np.ndarray] | None:
    """Merge span parts: higher weight wins; later part wins ties."""
    parts = entry.get("parts") or {}
    if not parts:
        return None
    z = np.zeros(shape, dtype=np.float64)
    w = np.zeros(shape, dtype=np.float64)
    any_ok = False
    for name in SPAN_PARTS:
        spec = parts.get(name)
        if not spec:
            continue
        z_path = _layer_file(proc, str(spec.get("z") or ""))
        w_path = _layer_file(proc, str(spec.get("weight") or ""))
        if not z_path.is_file() or not w_path.is_file():
            continue
        zt = _decode_z_u16(np.asarray(Image.open(z_path)).astype(np.uint16), max_h)
        wt = _decode_w_u8(np.asarray(Image.open(w_path)).astype(np.uint8))
        if zt.shape != shape or wt.shape != shape:
            print(f"  skip span/{name}: shape mismatch")
            continue
        take = wt >= w
        z = np.where(take, zt, z)
        w = np.where(take, wt, w)
        any_ok = True
    if not any_ok:
        return None
    return z, w


def write_add_layer(
    proc: Path,
    name: str,
    delta_m: np.ndarray,
    weight: np.ndarray,
    *,
    max_h: float,
    size: int | None = None,
) -> dict:
    spec = LAYER_SPECS.get(name)
    if spec is None or spec["mode"] != "add":
        raise ValueError(f"Unknown add layer {name!r} — add it to LAYER_SPECS")
    w = np.asarray(weight, dtype=np.float64)
    dz = np.asarray(delta_m, dtype=np.float64)
    if dz.shape != w.shape:
        raise ValueError(f"delta/weight shape mismatch {dz.shape} vs {w.shape}")
    size = int(size or dz.shape[0])
    n = int(np.count_nonzero(w > W_EPS))
    d = layers_dir(proc)
    rel_d = f"{LAYERS_DIRNAME}/{name}_dz.png"
    rel_w = f"{LAYERS_DIRNAME}/{name}_w.png"
    Image.fromarray(_encode_dz_u16(dz), mode="I;16").save(d / f"{name}_dz.png")
    Image.fromarray(_encode_w_u8(w), mode="L").save(d / f"{name}_w.png")
    entry = {
        "name": name,
        "mode": "add",
        "priority": int(spec["priority"]),
        "size": size,
        "max_height_m": float(max_h),
        "delta": rel_d,
        "weight": rel_w,
        "nz": n,
    }
    _upsert_layer(proc, entry)
    print(f"Heightmap layer {name}: add px={n} priority={spec['priority']}")
    return entry


def proposal_from_diff(dgm: np.ndarray, baked: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Replace proposal: target = baked Z, weight = 1 where bake changed DGM."""
    w = (np.abs(baked.astype(np.float64) - dgm.astype(np.float64)) > 1e-4).astype(np.float64)
    return baked.astype(np.float64), w


def compose(
    proc: Path,
    *,
    size: int,
    max_h: float,
    level_name: str | None = None,
    sync_import: bool = True,
) -> Path:
    """DGM + add layers + replace layers (priority order) → composed PNG."""
    dgm, max_h_file = load_dgm(proc, size)
    max_h = float(max_h or max_h_file)
    z = dgm.copy()
    data = load_manifest(proc)
    layers = list(data.get("layers") or [])
    layers.sort(key=lambda x: (int(x.get("priority") or 0), str(x.get("name") or "")))

    n_add = n_rep = 0
    for layer in layers:
        mode = str(layer.get("mode") or "")
        if mode == "add":
            w_path = _layer_file(proc, str(layer.get("weight") or ""))
            d_path = _layer_file(proc, str(layer.get("delta") or ""))
            if not w_path.is_file() or not d_path.is_file():
                print(f"  skip layer {layer.get('name')}: missing rasters")
                continue
            w = _decode_w_u8(np.asarray(Image.open(w_path)).astype(np.uint8))
            if w.shape != z.shape:
                print(f"  skip layer {layer.get('name')}: shape {w.shape} != {z.shape}")
                continue
            dz = _decode_dz_u16(np.asarray(Image.open(d_path)).astype(np.uint16))
            z += dz * w
            n_add += 1
        elif mode == "replace":
            if layer.get("parts") or str(layer.get("name") or "") == "span":
                merged = _union_span_parts(proc, layer, z.shape, max_h)
                if merged is None:
                    print(f"  skip layer {layer.get('name')}: no span parts")
                    continue
                zt, w = merged
            else:
                w_path = _layer_file(proc, str(layer.get("weight") or ""))
                z_path = _layer_file(proc, str(layer.get("z") or ""))
                if not w_path.is_file() or not z_path.is_file():
                    print(f"  skip layer {layer.get('name')}: missing rasters")
                    continue
                w = _decode_w_u8(np.asarray(Image.open(w_path)).astype(np.uint8))
                zt = _decode_z_u16(np.asarray(Image.open(z_path)).astype(np.uint16), max_h)
                if w.shape != z.shape:
                    print(f"  skip layer {layer.get('name')}: shape {w.shape} != {z.shape}")
                    continue
            z = z * (1.0 - w) + zt * w
            n_rep += 1

    np.clip(z, 0.0, max_h, out=z)
    u16 = _encode_z_u16(z, max_h)
    out = composed_path(proc, size)
    Image.fromarray(u16, mode="I;16").save(out)
    data["composed"] = out.name
    data["size"] = size
    data["max_height_m"] = max_h
    save_manifest(proc, data)
    print(f"Heightmap compose: adds={n_add} replaces={n_rep} -> {out.name}")

    if sync_import and level_name:
        _sync_import(proc, level_name, size, max_h, u16)
    return out


def _sync_import(
    proc: Path, level_name: str, size: int, max_h: float, u16: np.ndarray
) -> None:
    user_import = USER_LEVELS / level_name / "import"
    if not user_import.parent.is_dir():
        return
    user_import.mkdir(parents=True, exist_ok=True)
    Image.fromarray(u16, mode="I;16").save(user_import / f"heightmap_{size}.png")
    preset_path = proc / "terrainPreset.json"
    preset: dict = {}
    if preset_path.is_file():
        try:
            preset = json.loads(preset_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            preset = {}
    preset.setdefault("type", "TerrainData")
    preset.setdefault("name", "theTerrain")
    preset["heightScale"] = float(max_h)
    preset["heightMapPath"] = f"/levels/{level_name}/import/heightmap_{size}.png"
    text = json.dumps(preset, indent=2) + "\n"
    preset_path.write_text(text, encoding="utf-8")
    (user_import / "terrainPreset.json").write_text(text, encoding="utf-8")
    print(f"Synced composed heightmap -> {user_import}")
    print("Re-import terrainPreset.json in World Editor (heightmap changed).")


def restore_import_from_compose_or_dgm(proc: Path, level_name: str, size: int) -> None:
    """Copy composed (else DGM) into the level import folder."""
    user_import = USER_LEVELS / level_name / "import"
    if not user_import.parent.is_dir():
        return
    user_import.mkdir(parents=True, exist_ok=True)
    src = composed_path(proc, size)
    if not src.is_file():
        src = proc / f"heightmap_{size}.png"
    if src.is_file():
        shutil.copy2(src, user_import / f"heightmap_{size}.png")
        print(f"Synced heightmap {src.name} -> {user_import}")
