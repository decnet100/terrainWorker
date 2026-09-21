"""Tileable alpine facade / roof albedo for OSM building silhouettes.

Textures are mostly light plaster so BeamNG baseColorFactor can tint
beige / off-white. Windows and timber stay dark after the multiply.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

SIZE = 1024
# Module size in world metres (must match UV tiling in build_buildings).
TILE_W_M = 3.0
TILE_H_M = 3.0
ROOF_TILE_M = 2.0

# Light plaster — tinted in-game. Keep below 0.95 so night albedo stays sane.
PLASTER = np.array([228, 222, 210], dtype=np.float32)
WOOD = np.array([68, 44, 28], dtype=np.float32)
WOOD_DARK = np.array([46, 30, 20], dtype=np.float32)
FRAME = np.array([54, 36, 24], dtype=np.float32)
GLASS = np.array([32, 38, 44], dtype=np.float32)
GLASS_HI = np.array([48, 56, 64], dtype=np.float32)
ROOF = np.array([48, 48, 52], dtype=np.float32)
ROOF_LINE = np.array([36, 36, 40], dtype=np.float32)

ASSET_DIR = Path(__file__).resolve().parents[1] / "data" / "assets" / "buildings"


def _px(metres: float, axis_m: float) -> int:
    return int(round(metres / axis_m * SIZE))


def _fill_rect(img: np.ndarray, x0: int, y0: int, x1: int, y1: int, rgb: np.ndarray) -> None:
    x0, x1 = max(0, min(x0, x1)), min(SIZE, max(x0, x1))
    y0, y1 = max(0, min(y0, y1)), min(SIZE, max(y0, y1))
    if x1 <= x0 or y1 <= y0:
        return
    img[y0:y1, x0:x1] = rgb


def _plaster_base(rng: np.random.Generator) -> np.ndarray:
    img = np.empty((SIZE, SIZE, 3), dtype=np.float32)
    img[...] = PLASTER
    grain = rng.normal(0.0, 5.5, (SIZE, SIZE, 1)).astype(np.float32)
    streak = rng.normal(0.0, 3.0, (SIZE, 1, 1)).astype(np.float32)
    img += grain + streak
    return img


def _wood_column(img: np.ndarray, x0: int, x1: int, rng: np.random.Generator) -> None:
    x0, x1 = max(0, x0), min(SIZE, x1)
    if x1 <= x0:
        return
    w = x1 - x0
    col = np.broadcast_to(WOOD, (SIZE, w, 3)).copy()
    grain = rng.normal(0.0, 6.0, (SIZE, 1, 1)).astype(np.float32)
    col += grain
    # darker core
    mid = w // 2
    col[:, max(0, mid - 1) : mid + 2] = WOOD_DARK
    img[:, x0:x1] = col


def _wood_band(img: np.ndarray, y0: int, y1: int, rng: np.random.Generator) -> None:
    y0, y1 = max(0, y0), min(SIZE, y1)
    if y1 <= y0:
        return
    h = y1 - y0
    band = np.broadcast_to(WOOD, (h, SIZE, 3)).copy()
    grain = rng.normal(0.0, 5.0, (1, SIZE, 1)).astype(np.float32)
    band += grain
    img[y0:y1] = band


def _window(img: np.ndarray, cx: int, cy: int, w_m: float, h_m: float) -> None:
    """cy is window center in pixel rows (0 = top of image / eaves)."""
    hw = _px(w_m, TILE_W_M) // 2
    hh = _px(h_m, TILE_H_M) // 2
    frame = _px(0.08, TILE_W_M)
    x0, x1 = cx - hw, cx + hw
    y0, y1 = cy - hh, cy + hh
    _fill_rect(img, x0, y0, x1, y1, FRAME)
    gx0, gx1 = x0 + frame, x1 - frame
    gy0, gy1 = y0 + frame, y1 - frame
    _fill_rect(img, gx0, gy0, gx1, gy1, GLASS)
    # faint brighter band near the top of the glass (not emissive)
    split = gy0 + max(2, (gy1 - gy0) // 5)
    _fill_rect(img, gx0, gy0, gx1, split, GLASS_HI)
    # mullions
    mx = (gx0 + gx1) // 2
    my = gy0 + int((gy1 - gy0) * 0.42)
    bar = max(2, frame // 2)
    _fill_rect(img, mx - bar, gy0, mx + bar, gy1, FRAME)
    _fill_rect(img, gx0, my - bar, gx1, my + bar, FRAME)


def render_facade(kind: str, *, seed: int = 1) -> np.ndarray:
    """kind: win | win2 | blank. Image top = eaves, bottom = ground."""
    rng = np.random.default_rng(seed)
    img = _plaster_base(rng)

    beam_m = 0.13
    half = max(2, _px(beam_m * 0.5, TILE_W_M))
    # floor / storey plates wrap vertically, then posts on top so corners stay wood
    _wood_band(img, 0, half, rng)
    _wood_band(img, SIZE - half, SIZE, rng)
    sill_top = SIZE - _px(0.92, TILE_H_M)
    _wood_band(img, sill_top - half, sill_top + half, rng)
    _wood_column(img, 0, half, rng)
    _wood_column(img, SIZE - half, SIZE, rng)

    if kind == "win":
        wh = 1.35
        cy = sill_top - half - _px(wh * 0.5, TILE_H_M)
        _window(img, SIZE // 2, cy, 1.15, wh)
    elif kind == "win2":
        wh = 1.20
        cy = sill_top - half - _px(wh * 0.5, TILE_H_M)
        _window(img, _px(0.92, TILE_W_M), cy, 0.82, wh)
        _window(img, _px(2.08, TILE_W_M), cy, 0.82, wh)
        mid0 = SIZE // 2 - half
        mid1 = SIZE // 2 + half
        _wood_column(img, mid0, mid1, rng)

    return np.clip(img, 0, 255).astype(np.uint8)


def render_roof(*, seed: int = 4) -> np.ndarray:
    """Legacy dark shingle. Prefer render_roof_tiles()."""
    albedo, _h = render_roof_tiles("grey", seed=seed)
    return albedo


# Tile body RGB — own pattern, not a photo copy.
ROOF_TILE_RGB = {
    "red": np.array([168, 72, 52], dtype=np.float32),
    "brown": np.array([110, 68, 46], dtype=np.float32),
    "grey": np.array([96, 96, 100], dtype=np.float32),
}


def _roof_height_field() -> np.ndarray:
    """Tileable barrel-tile height 0..1. Rows overlap; columns offset every other row."""
    row_h = max(8, _px(0.20, ROOF_TILE_M))
    col_w = max(8, _px(0.22, ROOF_TILE_M))
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    row = yy / float(row_h)
    col = (xx + ((yy // row_h) % 2) * (col_w * 0.5)) / float(col_w)
    fx = col - np.floor(col)
    fy = row - np.floor(row)
    # two barrels per tile (S-ish profile), plus a lip at the lower overlap
    barrel = 0.55 + 0.45 * np.cos((fx - 0.25) * 2.0 * np.pi)
    barrel2 = 0.55 + 0.45 * np.cos((fx - 0.75) * 2.0 * np.pi)
    profile = np.maximum(barrel, barrel2)
    lip = np.clip(1.0 - fy * 7.0, 0.0, 1.0) * 0.22
    groove = np.clip(0.04 - np.minimum(fx, 1.0 - fx), 0.0, 0.04) / 0.04
    h = np.clip(profile * 0.82 + lip - groove * 0.35, 0.0, 1.0)
    return h.astype(np.float32)


def render_roof_tiles(color: str, *, seed: int = 4) -> tuple[np.ndarray, np.ndarray]:
    """Return (albedo uint8 RGB, height 0..1). color: red | brown | grey."""
    rng = np.random.default_rng(seed + {"red": 0, "brown": 11, "grey": 23}.get(color, 0))
    base = ROOF_TILE_RGB.get(color, ROOF_TILE_RGB["grey"])
    height = _roof_height_field()
    img = np.empty((SIZE, SIZE, 3), dtype=np.float32)
    img[...] = base
    img *= (0.78 + 0.28 * height)[..., None]
    img += rng.normal(0.0, 3.4, (SIZE, SIZE, 1)).astype(np.float32)
    # faint streak along the barrel
    img += rng.normal(0.0, 2.0, (1, SIZE, 1)).astype(np.float32)
    return np.clip(img, 0, 255).astype(np.uint8), height


def height_to_normal(height: np.ndarray, *, strength: float = 7.0) -> np.ndarray:
    """Tangent-space normal PNG (OpenGL +Y)."""
    dy, dx = np.gradient(height.astype(np.float32))
    nx = -dx * strength
    ny = dy * strength
    nz = np.ones_like(nx)
    n = np.stack((nx, ny, nz), axis=-1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True) + 1e-8
    return np.clip((n * 0.5 + 0.5) * 255.0, 0, 255).astype(np.uint8)


def render_boards(*, seed: int = 7) -> np.ndarray:
    """Vertical timber boards, tileable. U = across boards, V = along grain."""
    rng = np.random.default_rng(seed)
    board_m = 0.14
    bw = max(6, _px(board_m, TILE_W_M))
    tones = np.array(
        [
            [124, 86, 54],
            [108, 74, 46],
            [136, 94, 60],
            [116, 80, 50],
            [98, 68, 42],
        ],
        dtype=np.float32,
    )
    img = np.empty((SIZE, SIZE, 3), dtype=np.float32)
    x = 0
    bi = 0
    while x < SIZE:
        x1 = min(SIZE, x + bw)
        tone = tones[bi % len(tones)]
        col = np.broadcast_to(tone, (SIZE, x1 - x, 3)).copy()
        grain = rng.normal(0.0, 5.5, (SIZE, 1, 1)).astype(np.float32)
        col += grain
        # darker pith line
        mid = (x1 - x) // 2
        col[:, max(0, mid - 1) : mid + 2] *= 0.78
        img[:, x:x1] = col
        # groove
        if x1 < SIZE:
            img[:, x1 - 1 : min(SIZE, x1 + 1)] = (48, 32, 20)
        x = x1
        bi += 1
    return np.clip(img, 0, 255).astype(np.uint8)


FACADE_FILES = {
    "win": "facade_window.png",
    "win2": "facade_window2.png",
    "blank": "facade_blank.png",
}

ROOF_TILE_FILES = {
    "red": "roof_tile_red.png",
    "brown": "roof_tile_brown.png",
    "grey": "roof_tile_grey.png",
}
ROOF_NORMAL_FILE = "roof_tile_n.png"
BOARD_FILE = "facade_boards.png"


def write_facade_textures(dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for kind, name in FACADE_FILES.items():
        path = dest / name
        Image.fromarray(render_facade(kind, seed={"win": 1, "win2": 2, "blank": 3}[kind])).save(path)
        written.append(path)
    boards = dest / BOARD_FILE
    Image.fromarray(render_boards()).save(boards)
    written.append(boards)
    height = None
    for color, name in ROOF_TILE_FILES.items():
        albedo, height = render_roof_tiles(color)
        path = dest / name
        Image.fromarray(albedo).save(path)
        written.append(path)
    assert height is not None
    nrm = dest / ROOF_NORMAL_FILE
    Image.fromarray(height_to_normal(height)).save(nrm)
    written.append(nrm)
    # leftover name from the first facade pass
    shingle = dest / "roof_shingle.png"
    Image.fromarray(render_roof()).save(shingle)
    written.append(shingle)
    return written


def main() -> None:
    paths = write_facade_textures(ASSET_DIR)
    for p in paths:
        print(f"Wrote {p}")


if __name__ == "__main__":
    main()
