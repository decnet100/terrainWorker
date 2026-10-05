"""Count tiris roofs rejected by flat / container / on-road rules (Imst).

Does not change meshes. Loads the site tiris cache and the carriageway
mesh-clip GPKG, then prints how many roofs each rule drops.

    cd C:\\temp\\beamng_autoroad
    $env:AUTOROAD_SITE = "config/sites/imst.yaml"
    python tools\\test_building_filters.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from building_filters import (  # noqa: E402
    CONTAINER_AREA_MIN_M2,
    CONTAINER_COVER_MIN,
    EXCLUDE_PATH,
    FLAT_MEDIAN_MAX_M,
    ROAD_OVERLAP_MIN,
    classify_rejects,
    load_carriageway_union,
    load_tiris_roofs,
    merge_tiris_exclude,
)
from fetch_tiris_buildings import load_tiris_geojson  # noqa: E402
from site_coords import load_site, processed_dir, site_slug  # noqa: E402

# Cases from the Imst review (must land in the named bucket).
EXPECT_FLAT = {444349}
EXPECT_ON_ROAD = {444348, 194055}
# Large wrappers that cover other tiris roofs (filled after the first run if needed).
EXPECT_CONTAINER: set[int] = set()


def _sample(oids: set[int], n: int = 12) -> str:
    return ", ".join(str(x) for x in sorted(oids)[:n]) + ("…" if len(oids) > n else "")


def main() -> int:
    site = load_site()
    proc = processed_dir(site)
    slug = site_slug(site)
    print(f"Site: {slug}", flush=True)

    data = load_tiris_geojson(site, proc, force=False)
    roofs = load_tiris_roofs(data, site)
    print(f"Roofs in area window (on terrain): {len(roofs)}", flush=True)

    carriageway = load_carriageway_union(site, proc)
    if carriageway is None:
        print("FAIL: missing road_surface_smooth/carriageway_mesh_clip.gpkg")
        return 1
    print(
        f"Carriageway mesh clip area={float(carriageway.area):.0f} m²",
        flush=True,
    )
    print(
        f"Thresholds: flat median < {FLAT_MEDIAN_MAX_M} m; "
        f"road overlap >= {ROAD_OVERLAP_MIN:.0%}; "
        f"container cover >= {CONTAINER_COVER_MIN:.0%} "
        f"and area >= {CONTAINER_AREA_MIN_M2:.0f} m²",
        flush=True,
    )

    report = classify_rejects(roofs, carriageway)
    by_oid = {r.oid: r for r in roofs}

    flat = report["flat"]
    on_road = report["on_road"]
    container = report["container"]
    containers = report["containers"]
    union = report["union"]
    n = report["n_roofs"]

    only_flat = flat - on_road - container
    only_road = on_road - flat - container
    only_container = container - flat - on_road
    multi = (flat & on_road) | (flat & container) | (on_road & container)

    print()
    print("--- counts ---")
    print(f"  flat:                 {len(flat):5d}  ({100 * len(flat) / n:.1f}%)")
    print(f"  on road (>=10%):      {len(on_road):5d}  ({100 * len(on_road) / n:.1f}%)")
    print(f"  container:            {len(container):5d}  ({100 * len(container) / n:.1f}%)")
    print(f"  union (any rule):     {len(union):5d}  ({100 * len(union) / n:.1f}%)")
    print(f"  remaining:            {n - len(union):5d}")
    print()
    print("--- exclusive / overlap ---")
    print(f"  only flat:            {len(only_flat):5d}")
    print(f"  only on road:         {len(only_road):5d}")
    print(f"  only container:       {len(only_container):5d}")
    print(f"  in >=2 rules:         {len(multi):5d}")
    print()
    print(f"flat sample:      {_sample(flat)}")
    print(f"on_road sample:   {_sample(on_road)}")
    print(f"container sample: {_sample(container)}")
    if containers:
        print("container → children (first 15):")
        for oid, kids in sorted(containers.items(), key=lambda kv: -by_oid[kv[0]].area_m2)[:15]:
            rec = by_oid[oid]
            print(
                f"  {oid} area={rec.area_m2:.0f} h_med={rec.h_med} "
                f"covers {sorted(kids)}"
            )

    # Known cases
    failures: list[str] = []
    for oid in EXPECT_FLAT:
        if oid not in by_oid:
            failures.append(f"flat expect {oid}: not in roof set")
        elif oid not in flat:
            failures.append(f"flat expect {oid}: not classified flat (h_med={by_oid[oid].h_med})")
    for oid in EXPECT_ON_ROAD:
        if oid not in by_oid:
            failures.append(f"on_road expect {oid}: not in roof set")
        elif oid not in on_road:
            frac = report["road_frac"].get(oid, 0.0)
            failures.append(f"on_road expect {oid}: frac={frac:.2f}")
    for oid in EXPECT_CONTAINER:
        if oid not in by_oid:
            failures.append(f"container expect {oid}: not in roof set")
        elif oid not in container:
            failures.append(f"container expect {oid}: not a container")

    # Sanity: a normal house should not vanish under all rules together
    # (spot-check: Pfarrkirche roof 198518 from earlier church work).
    keep_check = 198518
    if keep_check in by_oid and keep_check in union:
        # Allowed if it truly matches a rule; just report.
        reasons = []
        if keep_check in flat:
            reasons.append("flat")
        if keep_check in on_road:
            reasons.append(f"road:{report['road_frac'][keep_check]:.2f}")
        if keep_check in container:
            reasons.append("container")
        print(f"note: oid {keep_check} rejected as {','.join(reasons)}")

    out = merge_tiris_exclude(
        site_slug=slug,
        flat=flat,
        on_road=on_road,
        container=container,
    )
    print()
    n_ids = len(json.loads(out.read_text(encoding="utf-8")).get("ids") or [])
    print(f"Wrote shared exclude list {out.relative_to(ROOT)} ids={n_ids}")
    print(f"(path {EXCLUDE_PATH})")

    print()
    if failures:
        print("FAIL:")
        for line in failures:
            print(f"  {line}")
        return 1
    print("OK — expected review OIDs match their buckets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
