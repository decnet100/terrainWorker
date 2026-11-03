"""List Italy/Utah/ECA building DAE stems more tightly."""
from __future__ import annotations

import zipfile
from collections import defaultdict
from pathlib import Path

LEVELS = Path(r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive\content\levels")
ART = Path(r"C:\Program Files (x86)\Steam\steamapps\common\BeamNG.drive\content\art_shapes.zip")


def list_under(zpath: Path, needle: str) -> list[str]:
    with zipfile.ZipFile(zpath) as z:
        return sorted(
            n.replace("\\", "/")
            for n in z.namelist()
            if n.lower().endswith(".dae") and needle in n.replace("\\", "/").lower()
        )


def stems(paths: list[str]) -> list[str]:
    return sorted({Path(p).stem for p in paths}, key=str.lower)


def dump(title: str, paths: list[str]) -> None:
    ss = stems(paths)
    print(f"\n=== {title}  dae={len(paths)} unique={len(ss)} ===")
    for s in ss:
        print(f"  {s}")


def main() -> None:
    italy = LEVELS / "italy.zip"
    dump("italy buildings/", list_under(italy, "art/shapes/buildings/"))
    dump("italy misc/", list_under(italy, "art/shapes/misc/"))
    dump("italy objects/", list_under(italy, "art/shapes/objects/"))
    dump("italy signs/", list_under(italy, "art/shapes/signs/"))
    dump("utah buildings/", list_under(LEVELS / "Utah.zip", "art/shapes/buildings/"))
    dump("eca buildings/", list_under(LEVELS / "east_coast_usa.zip", "art/shapes/buildings/"))
    dump("art objects/", list_under(ART, "art/shapes/objects/"))
    dump("art common/", list_under(ART, "art/shapes/common/"))

    # ForestItem types they already pull
    print("\n=== italy trees ===")
    for s in stems(list_under(italy, "art/shapes/trees/")):
        print(" ", s)


if __name__ == "__main__":
    main()
