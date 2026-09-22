"""Simple editable HUD logos for Alpine Roadtrip weather + traffic.

Writes 64×64 RGBA PNGs next to the UI app. Existing files are left alone
unless --force is set, so hand-edits survive the next build.

Usage:
  python tools\\write_map_icons.py
  python tools\\write_map_icons.py --force
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ICON_DIR = ROOT / "mods" / "autoroad_alpine_rt" / "ui" / "modules" / "apps" / "alpineRtTrafficMap" / "icons"
SIZE = 64

WEATHER = ("sun", "clouds", "rain", "snow", "alert")
TRAFFIC = ("clear", "medium", "heavy")


def _new() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _disk(draw: ImageDraw.ImageDraw, xy: tuple[int, int], r: int, fill) -> None:
    x, y = xy
    draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def _cloud(draw: ImageDraw.ImageDraw, cx: int, cy: int, fill) -> None:
    _disk(draw, (cx - 8, cy + 2), 9, fill)
    _disk(draw, (cx + 7, cy + 3), 8, fill)
    _disk(draw, (cx - 1, cy - 5), 10, fill)
    draw.ellipse((cx - 16, cy - 2, cx + 16, cy + 12), fill=fill)


def icon_sun() -> Image.Image:
    img, draw = _new()
    cx = cy = 32
    for i in range(8):
        ang = i * math.pi / 4
        x0 = cx + math.cos(ang) * 16
        y0 = cy + math.sin(ang) * 16
        x1 = cx + math.cos(ang) * 26
        y1 = cy + math.sin(ang) * 26
        draw.line((x0, y0, x1, y1), fill=(240, 176, 32, 255), width=4)
    _disk(draw, (cx, cy), 12, (255, 208, 64, 255))
    _disk(draw, (cx - 3, cy - 3), 4, (255, 232, 140, 220))
    return img


def icon_clouds() -> Image.Image:
    img, draw = _new()
    _cloud(draw, 34, 34, (168, 176, 184, 255))
    _cloud(draw, 26, 30, (210, 214, 218, 255))
    return img


def icon_rain() -> Image.Image:
    img, draw = _new()
    _cloud(draw, 32, 24, (150, 160, 172, 255))
    for i, x in enumerate((20, 32, 44)):
        y0 = 40 + (i % 2) * 2
        draw.line((x, y0, x - 3, y0 + 14), fill=(64, 140, 210, 255), width=3)
    return img


def icon_snow() -> Image.Image:
    img, draw = _new()
    _cloud(draw, 32, 22, (186, 194, 202, 255))

    def flake(cx: int, cy: int) -> None:
        col = (236, 242, 248, 255)
        draw.line((cx - 5, cy, cx + 5, cy), fill=col, width=2)
        draw.line((cx, cy - 5, cx, cy + 5), fill=col, width=2)
        draw.line((cx - 4, cy - 4, cx + 4, cy + 4), fill=col, width=2)
        draw.line((cx - 4, cy + 4, cx + 4, cy - 4), fill=col, width=2)

    flake(20, 46)
    flake(32, 50)
    flake(46, 44)
    return img


def icon_alert() -> Image.Image:
    img, draw = _new()
    draw.polygon([(32, 8), (56, 54), (8, 54)], fill=(240, 186, 48, 255), outline=(40, 32, 16, 255))
    draw.rectangle((30, 22, 34, 38), fill=(40, 32, 16, 255))
    _disk(draw, (32, 46), 3, (40, 32, 16, 255))
    return img


def _road(draw: ImageDraw.ImageDraw) -> None:
    # Slight S-curve as two thick strokes (asphalt + edge).
    pts = [(8, 50), (18, 44), (28, 28), (38, 20), (52, 14)]
    draw.line(pts, fill=(56, 60, 66, 255), width=14)
    draw.line(pts, fill=(92, 96, 102, 255), width=10)
    draw.line(pts, fill=(232, 200, 72, 220), width=2)


def _car(draw: ImageDraw.ImageDraw, x: int, y: int, heading_deg: float) -> None:
    body = Image.new("RGBA", (14, 8), (0, 0, 0, 0))
    bd = ImageDraw.Draw(body)
    bd.rounded_rectangle((0, 1, 13, 7), radius=2, fill=(48, 96, 168, 255))
    bd.rectangle((8, 2, 12, 6), fill=(180, 210, 230, 220))
    rot = body.rotate(-heading_deg, resample=Image.Resampling.BICUBIC, expand=True)
    return rot, (x - rot.size[0] // 2, y - rot.size[1] // 2)


def icon_traffic_clear() -> Image.Image:
    img, draw = _new()
    _road(draw)
    return img


def icon_traffic_medium() -> Image.Image:
    img, draw = _new()
    _road(draw)
    for cx, cy, ang in ((22, 40, 40), (40, 22, 28)):
        car, pos = _car(draw, cx, cy, ang)
        img.alpha_composite(car, pos)
    return img


def icon_traffic_heavy() -> Image.Image:
    img, draw = _new()
    _road(draw)
    for cx, cy, ang in ((16, 46, 38), (26, 34, 42), (36, 24, 30), (46, 16, 22)):
        car, pos = _car(draw, cx, cy, ang)
        img.alpha_composite(car, pos)
    return img


GENERATORS = {
    "weather_sun": icon_sun,
    "weather_clouds": icon_clouds,
    "weather_rain": icon_rain,
    "weather_snow": icon_snow,
    "weather_alert": icon_alert,
    "traffic_clear": icon_traffic_clear,
    "traffic_medium": icon_traffic_medium,
    "traffic_heavy": icon_traffic_heavy,
}


def write_map_icons(dest: Path | None = None, *, force: bool = False) -> list[Path]:
    dest = dest or ICON_DIR
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, fn in GENERATORS.items():
        path = dest / f"{name}.png"
        if path.is_file() and not force:
            continue
        fn().save(path, "PNG")
        written.append(path)
    write_preview(dest)
    return written


def write_preview(dest: Path) -> Path:
    """Contact sheet so the logos can be checked without the game."""
    names = list(GENERATORS)
    cell = 80
    cols = 4
    rows = 2
    sheet = Image.new("RGBA", (cols * cell, rows * cell + 88), (28, 32, 36, 255))
    draw = ImageDraw.Draw(sheet)
    for i, name in enumerate(names):
        icon = Image.open(dest / f"{name}.png").convert("RGBA")
        x = (i % cols) * cell + 8
        y = (i // cols) * cell + 8
        sheet.alpha_composite(icon, (x, y))
        draw.text((x, y + 66), name.replace("_", " "), fill=(210, 214, 218, 255))
    # Example stack as on the map (weather above traffic).
    stack_y = rows * cell + 8
    draw.text((8, stack_y), "stack: weather + traffic", fill=(180, 186, 190, 255))
    sun = Image.open(dest / "weather_sun.png").convert("RGBA").resize((36, 36))
    road = Image.open(dest / "traffic_medium.png").convert("RGBA").resize((36, 36))
    sheet.alpha_composite(sun, (200, stack_y + 4))
    sheet.alpha_composite(road, (200, stack_y + 42))
    out = dest / "_preview.png"
    sheet.convert("RGB").save(out, "PNG")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="Overwrite existing logos")
    args = ap.parse_args()
    written = write_map_icons(force=args.force)
    print(f"Icons in {ICON_DIR.relative_to(ROOT)} ({len(written)} written)")


if __name__ == "__main__":
    main()
