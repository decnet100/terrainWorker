"""Shared pipeline order: CLI (`build_level.py`) and the step GUI.

The list order is the intended sequence. `core=True` matches the one-shot
`build_level.py` run. Everything else is listed so the order stays visible,
but it is not started automatically.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SITES_DIR = ROOT / "config" / "sites"
RUNS_NAME = "pipeline_runs.json"
GUI_STATE_PATH = ROOT / "data" / "processed" / "_pipeline_gui.json"

SITE_LABELS: dict[str, str] = {
    "hahntennjoch.yaml": "Hahntennjoch",
    "l13_kuehtai.yaml": "L13 Kühtai",
    "l13_splining.yaml": "L13 Splining",
    "fernpass.yaml": "Fernpass 4096",
    "fernpass_mega.yaml": "Fernpass Mega",
    "testarena.yaml": "Testarena",
    "reschen.yaml": "Reschen",
    "imst.yaml": "Imst / Tarrenz",
    "oetz.yaml": "Ötztal (draft)",
}


@dataclass(frozen=True)
class Flag:
    key: str
    cli: str
    kind: str  # bool | float | str | choice
    label: str
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    summary: str
    script: str
    group: str
    docs: str
    yaml_keys: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    flags: tuple[Flag, ...] = ()
    core: bool = False
    needs: str = "always"
    extra_args: tuple[str, ...] = ()


STEPS: tuple[Step, ...] = (
    Step(
        id="fetch_dgm",
        title="Fetch DGM",
        summary="Terrain height (WCS or local file) into data/raw/.",
        script="tools/fetch_dgm.py",
        group="1  Raw data",
        docs="docs/SITE_DATA.md",
        yaml_keys=(
            "sources.dgm.type",
            "sources.dgm.coverage",
            "sources.dgm.resolution_m",
            "sources.dgm.path",
        ),
        outputs=("data/raw/dgm_{slug}.tif",),
        flags=(Flag("force", "--force", "bool", "Re-download, ignore cache"),),
        core=True,
    ),
    Step(
        id="fetch_ortho",
        title="Fetch ortho preview",
        summary="Low-res orthophoto preview (playable bbox) for color analytics.",
        script="tools/fetch_ortho.py",
        group="1  Raw data",
        docs="docs/SITE_DATA.md",
        yaml_keys=(
            "sources.ortho.type",
            "sources.ortho.coverage",
            "sources.ortho.resolution_m",
            "beamng.rock_color_clusters.enabled",
            "beamng.rock_color_clusters.ortho_size_px",
        ),
        outputs=("data/processed/{slug}/ortho.png",),
        flags=(Flag("force", "--force", "bool", "Re-download, ignore cache"),),
        core=True,
        needs="sources.ortho",
    ),
    Step(
        id="fetch_dom",
        title="Fetch DOM",
        summary="Surface model for vegetation height (nDSM = DOM − DGM).",
        script="tools/fetch_dom.py",
        group="1  Raw data",
        docs="docs/SITE_DATA.md",
        yaml_keys=(
            "sources.dom.type",
            "sources.dom.coverage",
            "sources.dom.resolution_m",
            "sources.dom.optional",
        ),
        outputs=("data/raw/dom_{slug}.tif",),
        flags=(Flag("force", "--force", "bool", "Re-download, ignore cache"),),
        core=True,
        needs="sources.dom",
    ),
    Step(
        id="fetch_bev_landcover",
        title="Fetch BEV land cover",
        summary="Six classes (tall / medium / low vegetation, bare, buildings, water) as a raster.",
        script="tools/fetch_bev_landcover.py",
        group="1  Raw data",
        docs="docs/SITE_DATA.md",
        yaml_keys=(
            "sources.bev_landcover.type",
            "sources.bev_landcover.layer",
            "sources.landuse.type",
        ),
        outputs=(
            "data/raw/bev_landcover_{slug}.tif",
            "data/processed/{slug}/landcover_bev.tif",
        ),
        flags=(Flag("force", "--force", "bool", "Re-download, ignore cache"),),
        core=True,
        needs="bev",
    ),
    Step(
        id="fetch_gip",
        title="Fetch GIP roads",
        summary="Tyrol road WFS. Needed before axis, decals, bridges, and galleries.",
        script="tools/fetch_gip.py",
        group="1  Raw data",
        docs="docs/GIP.md",
        yaml_keys=(
            "sources.gip.str_code",
            "sources.gip.include",
            "sources.gip.str_codes",
        ),
        outputs=("data/processed/{slug}/gip_structures.json",),
        flags=(Flag("force", "--force", "bool", "Re-download, ignore cache"),),
        needs="sources.gip",
    ),
    Step(
        id="fetch_landcover",
        title="Fetch Landnutzung",
        summary="Tirol traffic and land-use polygons (needed before GIP width samples).",
        script="tools/fetch_landcover.py",
        group="1  Raw data",
        docs="docs/ROADS.md",
        yaml_keys=(
            "sources.landuse.type",
            "sources.landuse.layers.landnutzung.url",
        ),
        outputs=("data/processed/{slug}/landcover_index.json",),
        flags=(Flag("force", "--force", "bool", "Re-download even if cache exists"),),
        needs="tirol_landcover",
    ),
    Step(
        id="measure_gip_widths",
        title="Measure GIP widths",
        summary="Three Landnutzung cross-sections per GIP OBJECTID → data/roads/gip_widths.json (shared, not per map).",
        script="tools/measure_gip_widths.py",
        group="1  Raw data",
        docs="docs/ROADS.md",
        yaml_keys=(
            "beamng.roads.width_by_objectid",
            "beamng.roads.width_by_str_code",
        ),
        outputs=("data/roads/gip_widths.json",),
        flags=(
            Flag("force", "--force", "bool", "Re-measure OBJECTIDs already in the catalog"),
            Flag("all_known", "--all-known", "bool", "Fernpass + Reschen + Imst"),
        ),
        needs="sources.gip",
    ),
    Step(
        id="fetch_strassennetz",
        title="Fetch road network",
        summary="Smoothed provincial axes (only where the site uses the network instead of GIP).",
        script="tools/fetch_strassennetz.py",
        group="1  Raw data",
        docs="docs/GIP.md",
        yaml_keys=(
            "sources.strassennetz.str_code",
            "sources.strassennetz.type_name",
        ),
        outputs=("data/processed/{slug}/strassennetz_beamng.json",),
        flags=(Flag("force", "--force", "bool", "Re-download, ignore cache"),),
        needs="sources.strassennetz",
    ),
    Step(
        id="build_twi",
        title="TWI and nDSM",
        summary="Wetness index from the DGM; optional vegetation height from DOM − DGM.",
        script="tools/build_twi.py",
        group="2  Terrain and axis",
        docs="docs/HEIGHTMAP_COMPOSE.md",
        yaml_keys=("sources.twi.smooth_m",),
        outputs=(
            "data/processed/{slug}/twi.tif",
            "data/processed/{slug}/ndsm.tif",
        ),
        flags=(
            Flag("skip_ndsm", "--skip-ndsm", "bool", "Skip nDSM"),
            Flag("require_dom", "--require-dom", "bool", "Fail if DOM is missing"),
            Flag("smooth_m", "--smooth-m", "float", "Smoothing σ in metres"),
        ),
        core=True,
    ),
    Step(
        id="build_snow_proxy",
        title="Snow proxy",
        summary="Snow cover from elevation, aspect, slope, and a temperature preset.",
        script="tools/build_snow_proxy.py",
        group="2  Terrain and axis",
        docs="docs/BEAMNG_IMPORT.md",
        yaml_keys=(
            "sources.snow.type",
            "sources.snow.preset",
            "sources.snow.t0_c",
            "sources.snow.z0_m",
        ),
        outputs=("data/processed/{slug}/snow_proxy.tif",),
        flags=(
            Flag(
                "preset",
                "--preset",
                "choice",
                "Season",
                ("", "november", "june", "september", "winter"),
            ),
        ),
        core=True,
    ),
    Step(
        id="build_smoke",
        title="Heightmap and axis",
        summary="DGM → 16-bit PNG. Axis from GIP when the site uses it; Overpass only for OSM sites. Then masks and guardrails.",
        script="tools/build_smoke.py",
        group="2  Terrain and axis",
        docs="docs/ROADS.md",
        yaml_keys=(
            "beamng.mask_size",
            "beamng.meters_per_pixel",
            "beamng.lane_width_m",
            "beamng.default_lanes",
            "sources.roads.type",
        ),
        outputs=(
            "data/processed/{slug}/heightmap_{mask_size}.png",
            "data/processed/{slug}/roads_beamng.json",
            "data/processed/{slug}/heightmap_meta.json",
        ),
        core=True,
    ),
    Step(
        id="build_terrain_masks",
        title="Terrain masks",
        summary="Asphalt, rock, gravel, meadow — including sync into the level import/ folder.",
        script="tools/build_terrain_masks.py",
        group="2  Terrain and axis",
        docs="docs/BEAMNG_IMPORT.md",
        yaml_keys=(
            "beamng.slope_rock_deg",
            "beamng.road_width_scale",
            "beamng.shoulder_m",
            "beamng.road_terrain",
            "sources.landuse.type",
        ),
        outputs=(
            "data/processed/{slug}/terrainPreset.json",
            "data/processed/{slug}/preview_terrain_materials.png",
        ),
        flags=(Flag("force", "--force", "bool", "Recompute land cover / slope"),),
        core=True,
    ),
    Step(
        id="cluster_rock_colors",
        title="Cluster rock colors",
        summary="Orthophoto rock pixels → representative RGB palette + class masks.",
        script="tools/cluster_rock_colors.py",
        group="2  Terrain and axis",
        docs="docs/ROCK_COLOR_CLUSTERS.md",
        outputs=("data/processed/{slug}/rock_color_clusters/rock_colors_k3.json",),
        yaml_keys=(
            "beamng.rock_color_clusters.enabled",
            "beamng.rock_color_clusters.ortho_size_px",
            "beamng.rock_color_clusters.k_start",
            "beamng.rock_color_clusters.k_max",
            "beamng.rock_color_clusters.threshold",
            "beamng.rock_color_clusters.spread_p",
            "beamng.rock_color_clusters.max_samples",
        ),
        core=True,
        needs="beamng.rock_color_clusters",
    ),
    Step(
        id="setup_beamng_level",
        title="Create level",
        summary="Unpack the template, rewrite paths, fill import/. Replace an existing level only with --force.",
        script="tools/setup_beamng_level.py",
        group="3  BeamNG-Level",
        docs="docs/BEAMNG_IMPORT.md",
        yaml_keys=("beamng.level_name", "beamng.mask_size", "beamng.meters_per_pixel"),
        flags=(
            Flag("force", "--force", "bool", "Replace the existing level folder"),
            Flag("keep_ocean", "--keep-ocean", "bool", "Keep the ocean"),
            Flag("no_sync", "--no-sync", "bool", "Do not copy import/"),
        ),
        core=True,
        extra_args=("level", "site"),
    ),
    Step(
        id="ensure_terrain_materials",
        title="Terrain materials",
        summary="Create meadow, forest floor, and snow (SnowTirol) in the level — no World Editor.",
        script="tools/ensure_terrain_materials.py",
        group="3  BeamNG-Level",
        docs="docs/BEAMNG_IMPORT.md",
        yaml_keys=(
            "beamng.dry_grass_material",
            "beamng.forest_material",
            "beamng.grass_material",
            "beamng.snow_material",
            "beamng.snow_albedo_gain",
        ),
        core=True,
        extra_args=("site",),
    ),
    Step(
        id="compose_biomes",
        title="Compose biomes",
        summary="BEV × nDSM × TWI × snow → exclusive biomes and soft terrain paint.",
        script="tools/compose_biomes.py",
        group="3  BeamNG-Level",
        docs="docs/BEAMNG_IMPORT.md",
        yaml_keys=(
            "beamng.compose.ndsm_canopy_m",
            "beamng.compose.ndsm_scrub_m",
            "beamng.compose.twi_wet",
            "beamng.compose.snow_light",
            "beamng.compose.snow_heavy",
        ),
        outputs=("data/processed/{slug}/biome_compose.json",),
        core=True,
    ),
    Step(
        id="build_forest",
        title="Scatter forest",
        summary="Forest points from the biome masks. The level folder must already exist.",
        script="tools/build_forest.py",
        group="3  BeamNG-Level",
        docs="docs/CONCEPT.md",
        yaml_keys=(
            "beamng.forest.trees",
            "beamng.forest.dry_grass",
            "beamng.forest.density",
        ),
        outputs=("data/processed/{slug}/forest_scatter_summary.json",),
        core=True,
    ),
    Step(
        id="init_annotations_gpkg",
        title="Create annotations GPKG",
        summary="Empty schema (edge, guardrail, axis). Does not overwrite a file that already has features.",
        script="tools/init_annotations_gpkg.py",
        group="4  Annotations",
        docs="docs/ANNOTATIONS.md",
        yaml_keys=("annotations.gpkg", "crs"),
    ),
    Step(
        id="seed_annotations",
        title="Seed annotations",
        summary="Write a heuristic draft into the GPKG. Never part of the core pipeline. Existing features only with --force.",
        script="tools/seed_annotations.py",
        group="4  Annotations",
        docs="docs/ANNOTATIONS.md",
        yaml_keys=(
            "annotations.gpkg",
            "annotations.guardrail_source",
            "beamng.guardrails.lateral_extra_m",
        ),
        flags=(Flag("force", "--force", "bool", "Replace existing features"),),
    ),
    Step(
        id="build_decal_roads",
        title="DecalRoads and road-bed",
        summary="Asphalt decals plus the road_bed heightmap layer (skip under bridge decks).",
        script="tools/build_decal_roads.py",
        group="5  Carriageway and structures",
        docs="docs/ROADS.md",
        yaml_keys=(
            "beamng.decal_roads.enabled",
            "beamng.decal_roads.centerline_source",
            "beamng.decal_roads.gip_decals",
            "beamng.roads.width_by_str_code",
            "beamng.roads.follow_parent",
            "beamng.lane_width_m",
        ),
        outputs=(
            "data/processed/{slug}/decal_roads_items.level.json",
            "data/processed/{slug}/heightmap_layers/road_bed_z.png",
        ),
        flags=(
            Flag(
                "centerline",
                "--centerline",
                "choice",
                "Centerline",
                ("", "solid", "dashed", "none"),
            ),
            Flag("skip_road_bed", "--skip-road-bed", "bool", "Decals only, no road-bed"),
        ),
        needs="beamng.decal_roads",
    ),
    Step(
        id="build_bridges",
        title="Bridges",
        summary="MeshRoad decks and the span/bridge heightmap part at the abutments.",
        script="tools/build_bridges.py",
        group="5  Carriageway and structures",
        docs="docs/GIP.md",
        yaml_keys=(
            "beamng.bridges.defaults.profile",
            "beamng.bridges.defaults.centerline",
            "beamng.bridges.defaults.width_m",
        ),
        outputs=("data/processed/{slug}/heightmap_layers/span_bridge_z.png",),
        flags=(Flag("step", "--step", "float", "Node spacing in metres"),),
        needs="beamng.bridges",
    ),
    Step(
        id="build_galleries",
        title="Galleries and tunnels",
        summary="DAE arches, heightmap holes, and the span/gallery heightmap part.",
        script="tools/build_galleries.py",
        group="5  Carriageway and structures",
        docs="docs/GIP.md",
        yaml_keys=(
            "beamng.galleries.defaults.centerline",
            "beamng.galleries.defaults.profile",
            "beamng.galleries.defaults.fitout",
            "beamng.roads.side_cut_preserve",
        ),
        outputs=("data/processed/{slug}/heightmap_layers/span_gallery_z.png",),
        flags=(
            Flag("step", "--step", "float", "Node spacing in metres"),
            Flag("only", "--only", "str", "Only these OBJECTIDs (comma)"),
            Flag("holes_only", "--holes-only", "bool", "Hole map only"),
            Flag("skip_holemap", "--skip-holemap", "bool", "Leave the hole map alone"),
        ),
        needs="beamng.galleries",
    ),
    Step(
        id="build_water",
        title="Water",
        summary=(
            "WaterBlocks + optional lake basin on the heightmap. "
            "fit_check vs terrain and MeshRoad (run after bridges/galleries)."
        ),
        script="tools/build_water.py",
        group="5  Carriageway and structures",
        docs="docs/HEIGHTMAP_COMPOSE.md",
        yaml_keys=(
            "beamng.water.enabled",
            "beamng.water.fit_check",
            "beamng.water.hang_max_m",
            "beamng.water.meshroad_clearance_m",
        ),
        outputs=(
            "data/processed/{slug}/heightmap_layers/water_w.png",
            "data/processed/{slug}/water_items.level.json",
        ),
        needs="beamng.water",
    ),
    Step(
        id="compose_heightmap",
        title="Compose heightmap",
        summary="Re-mix DGM + existing layers without rebuilding the structures.",
        script="tools/compose_heightmap.py",
        group="5  Carriageway and structures",
        docs="docs/HEIGHTMAP_COMPOSE.md",
        yaml_keys=("beamng.mask_size",),
        outputs=("data/processed/{slug}/heightmap_{mask_size}_composed.png",),
        flags=(
            Flag("dump_steps", "--dump-steps", "bool", "Write intermediate PNGs"),
            Flag("no_sync", "--no-sync", "bool", "Do not copy into import/"),
        ),
    ),
    Step(
        id="build_guardrails",
        title="Guardrails",
        summary="Posts or Italy rail sections. Source: GPKG or heuristic (annotations.guardrail_source).",
        script="tools/build_guardrails.py",
        group="5  Carriageway and structures",
        docs="docs/ANNOTATIONS.md",
        yaml_keys=(
            "beamng.guardrails.enabled",
            "beamng.guardrails.style",
            "beamng.guardrails.spacing_m",
            "beamng.guardrails.sides",
            "beamng.guardrails.centerline",
            "annotations.guardrail_source",
        ),
        outputs=(
            "data/processed/{slug}/guardrails_items.level.json",
            "data/processed/{slug}/guardrails_meta.json",
        ),
        flags=(Flag("clear", "--clear", "bool", "Only clear the level group"),),
        needs="beamng.guardrails",
    ),
    Step(
        id="build_backdrop",
        title="Backdrop rings",
        summary="Near / mid / far Collada rings around the playable map (fetches missing raw data itself).",
        script="tools/build_backdrop.py",
        group="6  Horizon and settlement",
        docs="docs/BACKDROP.md",
        yaml_keys=(
            "beamng.backdrop.radius_m",
            "beamng.backdrop.near_m",
            "beamng.backdrop.mid_m",
            "beamng.backdrop.mesh_step_m",
            "beamng.backdrop.albedo_gain",
            "sources.backdrop_ortho.layer",
        ),
        outputs=("data/processed/{slug}/preview_backdrop_count.png",),
        flags=(
            Flag("skip_fetch", "--skip-fetch", "bool", "Do not re-fetch raw data"),
            Flag("textures_only", "--textures-only", "bool", "Textures only, no mesh"),
            Flag("skip_ortho", "--skip-ortho", "bool", "Do not fetch ortho"),
            Flag("skip_worldcover", "--skip-worldcover", "bool", "Do not fetch Worldcover"),
            Flag("no_inject", "--no-inject", "bool", "Do not write into the level"),
            Flag("mesh_step", "--mesh-step", "float", "Mesh step in metres"),
        ),
        needs="beamng.backdrop",
    ),
    Step(
        id="build_buildings",
        title="Buildings",
        summary="TIRIS roofprints, walls inset 0.9 m from the eave. OSM only if source: osm.",
        script="tools/build_buildings.py",
        group="6  Horizon and settlement",
        docs="docs/CONCEPT.md",
        yaml_keys=(
            "beamng.buildings.enabled",
            "beamng.buildings.source",
            "beamng.buildings.eave_inset_m",
            "sources.tiris_buildings.url",
        ),
        needs="beamng.buildings",
    ),
    Step(
        id="build_testarena",
        title="Crop test arena",
        summary="Small crop around a GIP structure (carriageway first).",
        script="tools/build_testarena.py",
        group="6  Horizon and settlement",
        docs="docs/BEAMNG_IMPORT.md",
        yaml_keys=("beamng.level_name",),
        flags=(
            Flag("oid", "--oid", "str", "GIP OBJECTID"),
            Flag("setup", "--setup", "bool", "Create the level from the template"),
            Flag("build", "--build", "bool", "Build bridges and decals"),
        ),
        needs="testarena",
    ),
    Step(
        id="build_portals",
        title="Place portals",
        summary="Switch volumes and arrival spawns for Roadtrip Tyrol (all maps).",
        script="tools/build_portals.py",
        group="7  Roadtrip (all maps)",
        docs="docs/ROADTRIP_TYROL.md",
        flags=(Flag("no_inject", "--no-inject", "bool", "Do not write into the level"),),
        needs="roadtrip",
    ),
    Step(
        id="deploy_tirolrunde_mod",
        title="Deploy Roadtrip mod",
        summary="Copy the mod into BeamNG unpacked, or create a directory junction.",
        script="tools/deploy_tirolrunde_mod.py",
        group="7  Roadtrip (all maps)",
        docs="docs/ROADTRIP_TYROL.md",
        flags=(Flag("link", "--link", "bool", "Directory junction instead of a copy"),),
        needs="roadtrip",
    ),
)

STEPS_BY_ID: dict[str, Step] = {s.id: s for s in STEPS}
CORE_STEP_IDS: list[str] = [s.id for s in STEPS if s.core]


def nested_get(data: Any, path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def is_applicable(step: Step, site: dict) -> bool:
    need = step.needs
    if need in ("always", "roadtrip", ""):
        return True
    if need == "testarena":
        level = str((site.get("beamng") or {}).get("level_name") or "")
        return level == "autoroad_testarena"
    if need == "bev":
        if nested_get(site, "sources.bev_landcover"):
            return True
        lu = str(nested_get(site, "sources.landuse.type") or "").lower()
        return lu in ("bev", "bev_wms", "bev_landcover")
    if need == "beamng.rock_color_clusters":
        rc = nested_get(site, "beamng.rock_color_clusters")
        return bool(isinstance(rc, dict) and rc.get("enabled"))
    if need == "tirol_landcover":
        lu = str(nested_get(site, "sources.landuse.type") or "featureserver").lower()
        if lu in ("osm", "none", "off"):
            return False
        return bool(
            nested_get(site, "sources.landuse.layers.landnutzung")
            or nested_get(site, "sources.landuse")
        )
    val = nested_get(site, need)
    if val is None:
        return False
    if isinstance(val, dict) and val.get("enabled") is False:
        return False
    return True


def in_site_sequence(step: Step) -> bool:
    return step.needs != "roadtrip"


def format_value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        if not value:
            return "(empty)"
        if all(isinstance(x, dict) for x in value):
            return f"{len(value)} entries"
        if len(value) > 8:
            return f"{len(value)} values"
        return ", ".join(str(x) for x in value)
    if isinstance(value, dict):
        return f"{len(value)} keys"
    text = str(value)
    if len(text) > 72:
        return text[:69] + "…"
    return text


def config_rows(step: Step, site: dict) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path in step.yaml_keys:
        if path in seen:
            continue
        seen.add(path)
        rows.append((path, format_value(nested_get(site, path))))
    if step.id == "build_buildings":
        rows = [
            (
                key,
                (
                    "tiris (default)"
                    if key == "beamng.buildings.source" and value == "—"
                    else "0.9 (default)"
                    if key == "beamng.buildings.eave_inset_m" and value == "—"
                    else value
                ),
            )
            for key, value in rows
        ]
    if step.id == "measure_gip_widths":
        try:
            from gip_catalog import load_widths  # noqa: WPS433

            segs = (load_widths(force=True).get("segments") or {})
            applied = sum(1 for s in segs.values() if isinstance(s, dict) and s.get("applied"))
            rows.append(("data/roads/gip_widths.json", f"{len(segs)} segments ({applied} applied)"))
        except Exception:
            rows.append(("data/roads/gip_widths.json", "—"))
    if step.id == "build_bridges":
        items = nested_get(site, "beamng.bridges") or {}
        extra = [k for k in items if k != "defaults"]
        rows.append(("beamng.bridges (objects)", format_value(extra)))
    if step.id == "build_galleries":
        items = nested_get(site, "beamng.galleries") or {}
        extra = [k for k in items if k != "defaults"]
        rows.append(("beamng.galleries (objects)", format_value(extra)))
    return rows


def load_site_file(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@dataclass
class SiteInfo:
    path: Path
    rel: str
    label: str
    slug: str
    level_name: str
    mask_size: int
    crs: str
    draft: bool
    data: dict = field(repr=False)


def list_sites() -> list[SiteInfo]:
    found: list[SiteInfo] = []
    if SITES_DIR.is_dir():
        for path in sorted(SITES_DIR.glob("*.yaml")):
            data = load_site_file(path)
            bng = data.get("beamng") or {}
            rel = str(path.relative_to(ROOT)).replace("\\", "/")
            label = SITE_LABELS.get(path.name, path.stem)
            found.append(
                SiteInfo(
                    path=path,
                    rel=rel,
                    label=label,
                    slug=str(data.get("name") or path.stem).replace(" ", "_"),
                    level_name=str(bng.get("level_name") or ""),
                    mask_size=int(bng.get("mask_size") or 0),
                    crs=str(data.get("crs") or ""),
                    draft="draft" in label.lower() or path.name == "oetz.yaml",
                    data=data,
                )
            )
    return found


def expand_output(pattern: str, site: dict, slug: str) -> Path:
    bng = site.get("beamng") or {}
    text = pattern.format(
        slug=slug,
        mask_size=int(bng.get("mask_size") or 512),
        level=str(bng.get("level_name") or ""),
    )
    return ROOT / text


def newest_output(step: Step, site: dict, slug: str) -> Path | None:
    existing: list[Path] = []
    for pattern in step.outputs:
        path = expand_output(pattern, site, slug)
        if path.is_file():
            existing.append(path)
    if not existing:
        return None
    return max(existing, key=lambda p: p.stat().st_mtime)


def runs_path(slug: str) -> Path:
    return ROOT / "data" / "processed" / slug / RUNS_NAME


def load_runs(slug: str) -> dict[str, Any]:
    path = runs_path(slug)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def record_run(slug: str, step_id: str, *, ok: bool, seconds: float) -> None:
    path = runs_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load_runs(slug)
    data[step_id] = {
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "ok": bool(ok),
        "seconds": round(float(seconds), 1),
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def format_when(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return dt.strftime("%Y-%m-%d %H:%M")


def format_seconds(seconds: float) -> str:
    sec = int(round(seconds))
    if sec < 60:
        return f"{sec} s"
    minutes, rest = divmod(sec, 60)
    if minutes < 60:
        return f"{minutes} min {rest} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


def last_run_label(step: Step, site: dict, slug: str) -> str:
    rec = load_runs(slug).get(step.id)
    if isinstance(rec, dict) and rec.get("finished_at"):
        when = format_when(str(rec["finished_at"]))
        dur = format_seconds(float(rec.get("seconds") or 0))
        if rec.get("ok"):
            return f"{when}  ({dur})"
        return f"{when}  failed  ({dur})"
    out = newest_output(step, site, slug)
    if out is not None:
        when = datetime.fromtimestamp(out.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        return f"{when}  (output file)"
    return "not run yet"


def command_for(
    step: Step,
    *,
    site_rel: str,
    level_name: str,
    flags: dict[str, Any] | None = None,
    python: str | None = None,
) -> list[str]:
    import sys

    py = python or sys.executable
    flags = flags or {}
    cmd = [py, step.script.replace("/", "\\")]
    if "level" in step.extra_args:
        cmd.append(level_name)
    if "site" in step.extra_args:
        cmd.extend(["--site", site_rel])
    for flag in step.flags:
        raw = flags.get(flag.key)
        if flag.kind == "bool":
            if raw:
                cmd.append(flag.cli)
            continue
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        cmd.extend([flag.cli, text])
    return cmd


def load_gui_state() -> dict[str, Any]:
    if not GUI_STATE_PATH.is_file():
        return {}
    try:
        data = json.loads(GUI_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_gui_state(data: dict[str, Any]) -> None:
    GUI_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    GUI_STATE_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
