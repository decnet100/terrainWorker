"""Copy or junction mods/autoroad_alpine_rt into BeamNG unpacked mods.

Alpine Roadtrip GE mod.

Usage:
  python tools\\deploy_alpine_rt_mod.py
  python tools\\deploy_alpine_rt_mod.py --link
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "mods" / "autoroad_alpine_rt"
DST = (
    Path.home()
    / "AppData"
    / "Local"
    / "BeamNG"
    / "BeamNG.drive"
    / "current"
    / "mods"
    / "unpacked"
    / "autoroad_alpine_rt"
)


def is_reparse_point(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return bool(path.stat().st_file_attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except AttributeError:
        return False


def remove_existing_dst(path: Path) -> None:
    """Remove an existing mod folder, including junctions/symlinks on Windows."""
    if not (os.path.lexists(path) or path.exists() or path.is_symlink() or is_reparse_point(path)):
        return
    r = subprocess.run(["cmd", "/c", "rmdir", "/S", "/Q", str(path)], capture_output=True, text=True)
    if r.returncode == 0:
        return
    try:
        path.unlink()
        return
    except OSError:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        raise


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--link",
        action="store_true",
        help="Create a directory junction instead of copying (live edits)",
    )
    args = ap.parse_args()
    if not SRC.is_dir():
        raise SystemExit(f"Missing mod source: {SRC}")

    DST.parent.mkdir(parents=True, exist_ok=True)
    remove_existing_dst(DST)

    if args.link:
        cmd = ["cmd", "/c", "mklink", "/J", str(DST), str(SRC)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout)
            print(r.stderr, file=sys.stderr)
            raise SystemExit(f"mklink failed ({r.returncode})")
        print(f"Junction: {DST} -> {SRC}")
        return

    shutil.copytree(SRC, DST)
    print(f"Copied {SRC} -> {DST}")


if __name__ == "__main__":
    main()
