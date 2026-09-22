"""Extract stock tree imposters to PNG previews under data/tmp_tree_previews/."""
from __future__ import annotations

import struct
import zipfile
from pathlib import Path

from PIL import Image

ROOT = Path(r"C:\temp\beamng_autoroad")
OUT = ROOT / "data" / "tmp_tree_previews"
LEVELS = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive\content\levels"
)

# Representative stock trees for alpine / fake-larch thinking
PICKS = [
    ("driver_training.zip", "levels/driver_training/art/shapes/trees/trees_douglasfir/tree_douglasfir_large_a.dae.imposter.dds", "douglasfir_large"),
    ("driver_training.zip", "levels/driver_training/art/shapes/trees/trees_douglasfir/tree_douglasfir_small_a.dae.imposter.dds", "douglasfir_small"),
    ("driver_training.zip", "levels/driver_training/art/shapes/trees/trees_douglasfir/tree_douglasfir_bush_a.dae.imposter.dds", "douglasfir_bush"),
    ("driver_training.zip", "levels/driver_training/art/shapes/trees/trees_beech/tree_beech_large_b.dae.imposter.dds", "beech_large"),
    ("driver_training.zip", "levels/driver_training/art/shapes/trees/trees_aspen/tree_aspen_large_a.dae.imposter.dds", "aspen_large"),
    ("italy.zip", "levels/italy/art/shapes/trees/trees_italy/scots_pine.dae.imposter.dds", "scots_pine"),
    ("italy.zip", "levels/italy/art/shapes/trees/trees_italy/maritime_pine.dae.imposter.dds", "maritime_pine"),
    ("italy.zip", "levels/italy/art/shapes/trees/trees_italy/maritime_pine_2.dae.imposter.dds", "maritime_pine_2"),
    ("west_coast_usa.zip", "levels/west_coast_usa/art/shapes/trees/trees_conifer/pine_radiata_large.dae.imposter.dds", "pine_radiata_large"),
]


def _decode_rgb_block(block8: bytes) -> list[tuple[int, int, int]]:
    c0 = int.from_bytes(block8[0:2], "little")
    c1 = int.from_bytes(block8[2:4], "little")
    cbits = int.from_bytes(block8[4:8], "little")

    def rgb565(c: int) -> tuple[int, int, int]:
        r = ((c >> 11) & 31) * 255 // 31
        g = ((c >> 5) & 63) * 255 // 63
        b = (c & 31) * 255 // 31
        return r, g, b

    colors = [rgb565(c0), rgb565(c1)]
    if c0 > c1:
        colors.append(tuple((2 * colors[0][i] + colors[1][i]) // 3 for i in range(3)))
        colors.append(tuple((colors[0][i] + 2 * colors[1][i]) // 3 for i in range(3)))
    else:
        colors.append(tuple((colors[0][i] + colors[1][i]) // 2 for i in range(3)))
        colors.append((0, 0, 0))
    out = []
    for pi in range(16):
        ci = (cbits >> (2 * pi)) & 0x3
        out.append(colors[ci])
    return out


def _decode_dxt5_block(block: bytes) -> list[tuple[int, int, int, int]]:
    """Decode one 4x4 DXT5 block -> 16 RGBA pixels row-major."""
    a0, a1 = block[0], block[1]
    abits = int.from_bytes(block[2:8], "little")
    alphas = [a0, a1]
    if a0 > a1:
        for i in range(1, 7):
            alphas.append(((7 - i) * a0 + i * a1) // 7)
    else:
        for i in range(1, 5):
            alphas.append(((5 - i) * a0 + i * a1) // 5)
        alphas.extend([0, 255])
    colors = _decode_rgb_block(block[8:16])
    out = []
    for pi in range(16):
        ai = (abits >> (3 * pi)) & 0x7
        r, g, b = colors[pi]
        out.append((r, g, b, alphas[ai]))
    return out


def _decode_dxt3_block(block: bytes) -> list[tuple[int, int, int, int]]:
    """Decode one 4x4 DXT3 block -> 16 RGBA pixels row-major."""
    alphas = []
    for i in range(8):
        b = block[i]
        alphas.append((b & 0xF) * 17)
        alphas.append((b >> 4) * 17)
    colors = _decode_rgb_block(block[8:16])
    return [(colors[i][0], colors[i][1], colors[i][2], alphas[i]) for i in range(16)]


def dds_to_rgba(data: bytes) -> Image.Image:
    if data[:4] != b"DDS ":
        raise ValueError("not DDS")
    height, width = struct.unpack_from("<II", data, 12)
    fourcc = data[84:88]
    if fourcc == b"DX10":
        dxgi = struct.unpack_from("<I", data, 128)[0]
        # BC2=74 DXT3, BC3=77 DXT5, BC1=71 DXT1
        if dxgi == 77:
            fmt = "DXT5"
        elif dxgi == 74:
            fmt = "DXT3"
        else:
            raise ValueError(f"unsupported DXGI {dxgi}")
        payload = data[148:]
    else:
        fmt = fourcc.decode("ascii")
        payload = data[128:]

    if fmt == "DXT5":
        decode = _decode_dxt5_block
    elif fmt == "DXT3":
        decode = _decode_dxt3_block
    else:
        raise ValueError(f"unsupported fourCC {fmt}")

    bw, bh = (width + 3) // 4, (height + 3) // 4
    img = Image.new("RGBA", (width, height))
    px = img.load()
    off = 0
    for by in range(bh):
        for bx in range(bw):
            block = payload[off : off + 16]
            off += 16
            if len(block) < 16:
                break
            cells = decode(block)
            for i, (r, g, b, a) in enumerate(cells):
                x = bx * 4 + (i % 4)
                y = by * 4 + (i // 4)
                if x < width and y < height:
                    px[x, y] = (r, g, b, a)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    index = []
    for zip_name, member, label in PICKS:
        zp = LEVELS / zip_name
        if not zp.is_file():
            print("missing zip", zip_name)
            continue
        with zipfile.ZipFile(zp) as z:
            if member not in z.namelist():
                # fuzzy find
                base = Path(member).name
                cand = [n for n in z.namelist() if n.endswith(base)]
                if not cand:
                    print("missing member", member)
                    continue
                member = cand[0]
            raw = z.read(member)
        try:
            im = dds_to_rgba(raw)
        except Exception as exc:  # noqa: BLE001
            print(label, "decode fail", exc)
            continue
        # Imposter sheets are multi-angle atlases — show a readable crop (first row-ish)
        w, h = im.size
        # take a vertical strip / left portion that usually has a side view
        crop = im.crop((0, 0, min(w, max(256, w // 4)), min(h, max(256, h // 2))))
        # composite on dark green for alpha
        bg = Image.new("RGBA", crop.size, (30, 45, 35, 255))
        out = Image.alpha_composite(bg, crop)
        path = OUT / f"{label}.png"
        out.convert("RGB").save(path)
        # also full sheet small
        sheet = im.copy()
        sheet.thumbnail((1024, 1024))
        bg2 = Image.new("RGBA", sheet.size, (30, 45, 35, 255))
        full = Image.alpha_composite(bg2, sheet.convert("RGBA"))
        full_path = OUT / f"{label}_sheet.png"
        full.convert("RGB").save(full_path)
        index.append((label, path.name, full_path.name, w, h))
        print("wrote", path, "from", w, "x", h)

    (OUT / "README.txt").write_text(
        "Stock BeamNG tree imposters (cropped + sheet). Source: official level zips.\n"
        + "\n".join(f"{a}: {b} / {c} (src {d}x{e})" for a, b, c, d, e in index),
        encoding="utf-8",
    )
    print("done", OUT)


if __name__ == "__main__":
    main()
