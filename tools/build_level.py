"""One-shot site build: raw data → processed → BeamNG user level.

Goal: fresh clone + this script ≈ the Fernpass (or other site) layout we tune here.

Usage (PowerShell):
  cd C:\\temp\\beamng_autoroad
  python tools\\build_level.py --site config/sites/fernpass_mega.yaml

Steps (skippable with --from / --only):
  fetch_dgm, fetch_dom, fetch_bev_landcover, build_twi, build_snow_proxy,
  build_smoke, build_terrain_masks, setup_beamng_level,
  ensure_terrain_materials, compose_biomes, build_forest

Heavy extras (bridges/galleries/decals/water) stay separate — run those after.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from pipeline_catalog import CORE_STEP_IDS, STEPS_BY_ID, command_for  # noqa: E402

USER_LEVELS = (
    Path.home()
    / "AppData"
    / "Local"
    / "BeamNG"
    / "BeamNG.drive"
    / "current"
    / "levels"
)

STEPS: list[str] = list(CORE_STEP_IDS)


def _run(cmd: list[str], site: str) -> None:
    env = os.environ.copy()
    env["AUTOROAD_SITE"] = site
    # Child prints use arrows / Greek; Windows cp1252 consoles otherwise crash.
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    print(f"\n=== {' '.join(cmd)} ===")
    subprocess.run(cmd, cwd=str(ROOT), env=env, check=True)


def _cmd_for(step: str, site: str, level_name: str, *, force_setup: bool) -> list[str] | None:
    spec = STEPS_BY_ID.get(step)
    if spec is None or not spec.core:
        raise SystemExit(f"Unknown step {step}")
    if step == "setup_beamng_level":
        dst = USER_LEVELS / level_name
        if dst.is_dir() and not force_setup:
            print(f"\n=== setup_beamng_level SKIP (exists: {dst}) ===")
            return None
    flags = {"force": force_setup} if step == "setup_beamng_level" else {}
    return command_for(spec, site_rel=site, level_name=level_name, flags=flags)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--site",
        default="",
        help="Site YAML, e.g. config/sites/fernpass_mega.yaml",
    )
    ap.add_argument("--from", dest="from_step", default="", help="Start at this step")
    ap.add_argument("--only", default="", help="Run only this step")
    ap.add_argument("--list", action="store_true", help="List steps and exit")
    ap.add_argument(
        "--force-setup",
        action="store_true",
        help="Recreate BeamNG level folder even if it already exists",
    )
    args = ap.parse_args()

    if args.list:
        for name in STEPS:
            print(name)
        return

    if not args.site:
        raise SystemExit("--site is required (unless --list)")

    site_rel = args.site.replace("\\", "/")
    site_path = Path(site_rel)
    if not site_path.is_absolute():
        site_path = ROOT / site_path
    if not site_path.is_file():
        raise SystemExit(f"Site not found: {site_path}")
    site_arg = str(site_path.relative_to(ROOT)).replace("\\", "/")

    os.environ["AUTOROAD_SITE"] = site_arg
    from site_coords import load_site  # noqa: WPS433

    site = load_site()
    level_name = str((site.get("beamng") or {}).get("level_name") or "").strip()
    if not level_name:
        raise SystemExit("beamng.level_name missing in site YAML")

    if args.only:
        if args.only not in STEPS:
            raise SystemExit(f"Unknown step {args.only!r}. Use --list")
        selected = [args.only]
    else:
        start = 0
        if args.from_step:
            if args.from_step not in STEPS:
                raise SystemExit(f"Unknown --from {args.from_step!r}. Use --list")
            start = STEPS.index(args.from_step)
        selected = STEPS[start:]

    for step in selected:
        cmd = _cmd_for(step, site_arg, level_name, force_setup=bool(args.force_setup))
        if cmd is None:
            continue
        _run(cmd, site_arg)

    print("\nbuild_level done. Import terrainPreset.json in World Editor if needed.")
    print("Optional next: bridges / galleries / decals / water / buildings.")


if __name__ == "__main__":
    main()
