# Backdrop meshes (horizon)

Far scenery around the **playable** heightmap: Collada TSStatics with `collisionType: None`, not a bigger `.ter`. The player cannot drive on it; driving off the playable edge still falls.

Fernpass Mega and Reschen are wired (`beamng.backdrop` + `sources.backdrop_ortho` in those site YAMLs). Other sites pick up the same YAML keys (or code defaults).

Related: [BEAMNG_IMPORT.md](BEAMNG_IMPORT.md) (playable terrain), [HEIGHTMAP_COMPOSE.md](HEIGHTMAP_COMPOSE.md) (Z datum), [ROADTRIP_TYROL.md](ROADTRIP_TYROL.md) (portal sky-arc later sits in front of this).

Status: **POC that already injects**. Not a replacement for Forest / terrain paint on the playable map.

---

## Three rings

All Z is **absolute metres minus** `heightmap_meta.json` → `z_min_m` (same datum as the playable PNG). Run `build_smoke.py` first so that file exists.

Distances below are measured **outward from the playable bbox**, not from the map centre.

| Ring | Geometry | DEM | Texture | Mesh step (Fernpass Mega) |
|------|----------|-----|---------|---------------------------|
| **Near** | bbox → `near_m` (default 2 km; **Reschen 5 km**). Hole = the playable bbox. | Tirol WCS DGM (`near_dgm_res_m`, default 2 m). Cells outside Tyrol stay empty — **no Copernicus fill**. Mesh Z is **cubic-spline** sampled from that DGM (not a stride of raster cells). | SWISSIMAGE crop of the near extent | `near_step_m` 15 m |
| **Mid** | `near_m` → `mid_m` (~10 km), with `near_overlap_m` / `mid_overlap_m` so rings overlap. **Always kept** (no viewshed). | Copernicus GLO-30 **+ filtered Tirol nDSM** (`mid_canopy`, `mid_dgm_res_m` default 8 m). Outside AT coverage the bump is 0. | SWISSIMAGE crop of the mid extent | `mid_step_m` 50 m |
| **Far** | out to `radius_m` (~40 km from site centre), minus a hole through mid. **Viewshed-culled**. | Copernicus GLO-30 @ `viewshed_step_m` (50 m) | SWISSIMAGE of the full padded square | `mesh_step_m` 120 m |

Near inner edge: vertices on the playable bbox are snapped to the bbox, Z clamped to the composed heightmap, then dropped by `near_lip_drop_m` (default 1 m). That is a **cliff under the lip**, not a tuck under the driveable surface (tucking z-fights / pokes through).

Mesh Z used to take every *k*-th DGM cell (`elev[::stride]`). That is nearest-neighbour downsample and shows as terraces. Near now samples the 2 m DGM with a **cubic spline** at each 15 m vertex. Mid/far stay bilinear on the 50 m Copernicus grid (the GLO-30→site-CRS warp itself is cubic).

Near canopy: Tirol **DOM − DGM** on the same 2 m ring. Heights **< 1 m** are ignored. Isolated spikes (masts) go away via a local-median outlier clip plus connected-component area (`near_canopy_min_area_m2`). Remaining nDSM is **added to DTM Z** on the same near mesh — no extra object, just a jagged ridgeline. Preview: `preview_backdrop_near_canopy.png`.

Mid canopy: the same filter, from a coarser Tirol WCS (`mid_dgm_res_m`, default 8 m) over `mid_extent`. nDSM is cubic-spline aligned onto the Copernicus 50 m grid and **added** (DTM stays GLO-30). CH/IT/DE cells stay flat. Far has no nDSM. Preview: `preview_backdrop_mid_canopy.png`.

SWISSIMAGE is a Swiss product. Around Fernpass it covers the west well; Austrian / German ground in the crop may be empty or coarse. The mesh still exists from the DEM.

---

## Tools

| Script | Role |
|--------|------|
| `tools/fetch_backdrop.py` | Download + warp rasters into `data/processed/<site>/` |
| `tools/fetch_bev_landcover.py` | BEV INSPIRE LC WMS (playable bbox). Near/mid/far envelopes via `fetch_bev_near` / `load_bev_forest_mask`. |
| `tools/preview_forest_detect.py` | PNG-only: Waldfläche vs BEV vs WorldCover vs canopy vs combo |
| `tools/preview_backdrop_detect.py` | PNG-only meadow/forest/snow grade (same knobs as the bake) |
| `tools/build_backdrop.py` | Viewshed, textures, Collada, optional inject into the BeamNG user level |

`build_backdrop` calls fetch unless `--skip-fetch` **and** a cached far DEM already exist. With `--skip-fetch` it can still refresh a missing near DGM or mid ortho.

### Fetch sources

- **DEM (far/mid):** Copernicus GLO-30 public COGs on AWS (`data/raw/copernicus_glo30/`). Warped to the site CRS with a **cubic spline**; 1–2 px gaps at 1° tile edges are filled from the nearest finite neighbour. EEA-10 is a later swap, not wired.
- **DEM (near):** same Tirol DGM WCS as `fetch_dgm.py`, larger bbox (`near_m` + 200 m). Invalid / nodata (including ~0 m outside AT) → empty cells.
- **Ortho:** SWISSIMAGE Hintergrund via `wms.geo.admin.ch` (`ch.swisstopo.swissimage`). Three GetMap requests (far / near / mid extents), then cubic-spline resampled onto the site-CRS texture grid. Attribution is written to `*.meta.json`.
- **Fallback tint:** ESA WorldCover 2021 (Planetary Computer / S3). If ortho or WorldCover fail, bake uses elevation + slope (green valley → rock → snow).

YAML for the WMS (Fernpass Mega):

```yaml
sources:
  backdrop_ortho:
    type: wms
    url: "https://wms.geo.admin.ch/"
    layer: ch.swisstopo.swissimage
    version: "1.3.0"
```

If `sources.backdrop_ortho` is missing, fetch still uses those defaults.

### Build steps

1. Load the warped DEM. Observers = `observer_grid` × `observer_grid` on the playable bbox (default 5×5) plus `extra_peaks` local maxima (default 4).
2. Ray viewshed per observer (`n_rays`, earth curvature `curvature_cc` × \(d^2 / 2R\)). Keep far cells with `count >= min_views`, **close** internal holes (`viewshed_close_m`), small outer pad (`viewshed_pad_m`), clip to `radius_m`, punch the mid hole, then stitch N/S and E/W channels (`viewshed_stitch_m`) where keep exists on both sides.
3. Bake RGB: ortho × weak hillshade (`ortho_hillshade`) × `albedo_gain`, else WorldCover LUT / hypsometric. DEM slope becomes a tangent-space `normalMap` so TimeOfDay lights the ring. Then, if `sources.snow` is set, the same DGM snow proxy as the playable map tints peaks toward `snow_light` / `snow_heavy`.
4. Mesh quads where the keep-mask is solid. Face winding is **+Z / sky** (right-hand). No collision, no extra reverse winding.
5. Split meshes so each DAE stays under BeamNG’s **16-bit** vertex index limit (65 535): far/near = four quadrants `nw|ne|sw|se`; mid = `mid_tiles`² (default 3×3) with **letter** names (`nw`, `n`, `ne`, …).
6. Copy DAE/PNG/`main.materials.json` to `levels/<level>/art/shapes/backdrop/` and inject TSStatics under SimGroup `backdrop`. Raise `LevelInfo.visibleDistance` to cover `radius_m`.

---

## Commands

Quit BeamNG if you will inject (files under the user level folder). Smoke must already have written `heightmap_meta.json`.

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"; python tools\fetch_backdrop.py
```

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"; python tools\build_backdrop.py
```

Reschen (same three rings):

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/reschen.yaml"; python tools\build_backdrop.py
```

Cached rasters, rebuild meshes + textures + inject:

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"; python tools\build_backdrop.py --skip-fetch --skip-worldcover
```

Fetch flags: `--force` (ignore raster cache), `--skip-worldcover`, `--skip-ortho`, `--skip-near-dgm`.

Build flags: `--skip-fetch`, `--no-inject` (write `data/processed/<site>/backdrop_meshes/` only), `--mesh-step` (override far `mesh_step_m`), same `--skip-*` as fetch.

`--skip-ortho` skips **using** the ortho PNGs even if they are already on disk (hypsometric tint instead). Omit it when you want Swissimage.

After inject of **Lua, portals, DAE, or materials**: **quit BeamNG entirely** and start a new session. Freeroam reload / World-Editor reload keeps the old in-memory materials and `tirolrunde` extension. `textures-only` plus a file swap also does **not** bind new mesh textures — full remesh + quit.

The World Editor keeps the object list from when you *loaded*; **Save Level** from that old session writes that list back to disk and drops objects the script just added. Do not save over inject.

---

## `beamng.backdrop` knobs

Defaults live in `fetch_backdrop.backdrop_cfg`. Fernpass Mega overrides many of them.

| Key | Default | Meaning |
|-----|---------|---------|
| `radius_m` | 40000 | Far extent from site centre |
| `viewshed_step_m` | 50 | Far DEM / viewshed cell size |
| `observer_grid` | 5 | Observers along each bbox edge |
| `observer_z_extra_m` | 10 | Eye height above DEM |
| `extra_peaks` | 4 | Extra observers on bbox DEM maxima |
| `min_views` | 1 | Far cell must be seen this many times |
| `n_rays` | 2048 | Rays per observer |
| `curvature_cc` | 0.85714 | Refraction-ish multiplier on earth drop |
| `viewshed_close_m` | 600 | Fill **enclosed** keep holes up to this width. Does not fill bays that open into the mid hole. |
| `viewshed_pad_m` | 100 | Outer pad after closing (was 4×50 m dilation, no close). |
| `viewshed_stitch_m` | 2500 | Fill N/S and E/W channels outside the hole where keep exists on both sides. The east-arm break is ~2 km at the hole wall; 6 km+ valleys stay open. |
| `mesh_step_m` | 120 | Far triangle spacing |
| `hole_pad_m` | 30 | Minimum far hole around the playable bbox (also at least mid−overlap) |
| `near_m` | 2000 | Near ring outer distance from bbox (Reschen: 5000) |
| `near_step_m` | 10 (Mega/Reschen: 15) | Near triangle spacing |
| `near_dgm_res_m` | 2 | Near WCS request resolution |
| `near_overlap_m` | 80 | Mid starts this far inside `near_m` |
| `near_lip_drop_m` | 1 | Drop near verts on the bbox vs playable Z |
| `near_texture_size` | 2048 | Near albedo PNG |
| `near_normal_size` | = albedo | Near `normalMap` PNG. Reschen 4096 from the 2 m DGM (~4.5 m/texel). |
| `near_tiles` | 3 | Near Collada grid (letter names). Reschen 5 km @ 15 m uses 5×5 so each tile stays under 65 535 verts. |
| `near_canopy` | true | Add filtered nDSM (DOM−DGM) onto near mesh Z. Needs `sources.dom`. |
| `near_canopy_min_m` | 1 | Ignore objects shorter than this |
| `near_canopy_spike_m` | 8 | Clip pixels this far above a 5×5 local median (masts) |
| `near_canopy_median_px` | 5 | Median window (cells) for the spike clip |
| `near_canopy_min_area_m2` | 40 | Drop connected blobs smaller than this |
| `mid_m` | 10000 | Mid ring outer distance from bbox |
| `mid_step_m` | 50 | Mid triangle spacing |
| `mid_overlap_m` | 150 | Far hole starts this far inside `mid_m` |
| `mid_texture_size` | 2048 | Mid PNG |
| `mid_tiles` | 3 | Mid Collada grid (letter names, not digits) |
| `mid_canopy` | = `near_canopy` | Add the same filtered nDSM onto Copernicus mid Z. Needs `sources.dom`. |
| `mid_dgm_res_m` | 8 | Tirol WCS cell size for mid nDSM only (mesh stays `mid_step_m`). |
| `texture_size` | 2048 | Far PNG |
| `albedo_gain` | 0.58 | Multiply on ortho bake. Aerial photos are already sunlit; BeamNG lights them again. Lower = darker. Fernpass Mega uses 0.58. |
| `base_color` | 1,1,1 | Baked onto a soft **green-share** mask (`G/(R+G+B)` above gray), times `(1 − snow_proxy)`. Rock/snow stay. Engine factor is 1,1,1. |
| `ortho_gamma` | 1.0 | `rgb ** gamma` on the photo before shade. `>1` darkens midtones (Reschen probe 1.20). |
| `ortho_hillshade` | 0.10 | Extra baked shade on ortho (`(1−amp)+amp×hs`). High values lock NW shadows and only match golden hour. |
| `normal_strength` | 0.65 | Scale of the DEM `normalMap`. Higher = more ToD relief on slopes. |
| `ortho_grade` | none | `playable_olive`: lerp photo-green toward dry_meadow olive (Reschen). Snow/rock stay. Unused when `detect_grade` is on. |
| `olive_mix` | 0.68 | How hard `playable_olive` pulls greens (was 0.55). |
| `lip_fade_m` | 800 | Extra olive + darken on the near ring, fading out from the playable bbox. |
| `lip_olive` / `lip_darken` | 0.40 / 0.12 | Strength of that seam grade. |
| `detect_grade` | false | Reschen: bake meadows + BEV forest + snow classes onto the ortho (see below). |
| `forest_detect` | green | `bev`: add BEV `vegetation_hoch` + `vegetation_mittel` as the forest class. `green`: green-share only. |
| `grass_cut` / `grass_mix` / `grass_dark` | 0.5 / 0.55 / 0.28 | Meadow class = photo green-share ≥ cut. Mix toward olive RGB 120,108,64, then darken. |
| `forest_mix` / `forest_dark` / `forest_olive` | same as grass / `[72,88,52]` | BEV forest only. `forest_olive` is 0–255. Forest wins on overlap with meadows. |
| `snow_cut` / `snow_mix` | 0.40 / 0.65 | DGM snow-proxy class on top of both. Mix toward 245,248,255. |
| `snow_tint` | true | Legacy `_tint_snow` path. Reschen keeps this **false** when `detect_grade` is on (otherwise forests go white). |

Snow on the **rings** is a recolour of the baked PNG (`detect_grade` or the older `_tint_snow`). Playable terrain snow is a different material — [SnowTirol](BEAMNG_IMPORT.md#playable-snow-snowtirol).

`build_backdrop` still reads `sources.snow` (preset / lapse / aspect) and `beamng.compose.snow_light` / `snow_heavy` for the proxy itself. No `sources.snow` → no overlay (hypsometric fallback still paints a 2450 m snow band). WorldCover class 80 (water) stays unsnowed.

---

## Detect-grade (Reschen meadows + forest + snow)

Swissimage nadir forests look like grey scribble (the playable “forest” is TSForest trees, not this photo). Green-share olive only hits **meadows**. Sharp forest edges come from **BEV INSPIRE Land Cover** (`vegetation_hoch` = 0, `vegetation_mittel` = 1), fetched for the **near envelope** (playable + `near_m`), not just `site.bbox`.

Tirol Waldfläche polygons are cadastral and sharp in AT, but BEV hoch+mittel was the cleaner class for this crop. WorldCover 10 is 10 m and speckly. DOM−DGM canopy is finest but noisy (hedges/masts). Combo used in the bake: **meadow green-share ∪ BEV forest**, then snow on top.

PNG-only (no remesh):

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/reschen.yaml"; python tools\preview_forest_detect.py
```

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/reschen.yaml"; python tools\preview_backdrop_detect.py --mix 0.55 --dark 0.28 --forest-rgb 72,88,52 --forest-mix 0.45 --forest-dark 0.38 --snow-cut 0.40 --snow-mix 0.65
```

Writes under `data/processed/<site>/`: `preview_forest_*.png`, `preview_grade_near.png`. When the grade PNG looks right, copy the CLI values into `beamng.backdrop` and remesh (not `--textures-only`):

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/reschen.yaml"; python tools\build_backdrop.py --skip-fetch --skip-worldcover
```

First BEV fetch for mid/far extents happens during that bake (near is already cached as `data/raw/bev_landcover_near_<site>.tif`). GeoServer writes `GDAL_NODATA` as `"6.0"`; tifffile warns and still reads the uint8 pixels. Harmless — we treat class 6 ourselves.

BEV is an AT mosaic. This Reschen near crop classified through; CH/IT holes would stay photo + WorldCover/canopy fallback if nodata appears.

---

## Snow on the rings

Swissimage is a summer photo. The playable map paints November snow from the DGM proxy; the horizon would stay green without a matching tint.

On each ring DEM (Copernicus far/mid, Tirol DGM near) the bake:

1. Runs `compute_snow_proxy` (height, north aspect, steep faces, `t0` from the site preset).
2. Below `snow_light`: leave the photo.
3. `snow_light` → `snow_heavy`: lerp toward biome `snow_light` RGB, then `snow_heavy` RGB.

With `detect_grade` (Reschen) that recolour is the hard `snow_cut` / `snow_mix` class, not the old 22%+ white wash (`_tint_snow`). The wash painted DGM snow onto grey canopy and made forests look like leftover winter. Keep `snow_tint: false` when detect-grade is on.

That is a **recolour**, not a second snow mesh. Steep rock already gets less cover from the proxy, so faces stay darker than plateaus. Change `sources.snow.preset` (or the compose thresholds) and rebuild the backdrop the same way you rebuild biomes.

`preview_backdrop_snow.png` is the far-DEM proxy on hillshade (sanity check before launching the game).

---

## Outputs (`data/processed/<site>/`)

| File | Role |
|------|------|
| `backdrop_dem.tif` + `.meta.json` | Warped GLO-30 |
| `backdrop_near_dgm.tif` + `.meta.json` | Tirol DGM ring |
| `backdrop_ortho.png` / `_near_` / `_mid_` + `.meta.json` | SWISSIMAGE grids |
| `backdrop_worldcover.tif` | Optional class raster on the DEM grid |
| `preview_backdrop_viewshed.png` | Hillshade + keep colours + bbox + observers |
| `preview_backdrop_{diffuse,near,mid,count,snow,near_canopy,mid_canopy}.png` | Baked textures / viewshed count / snow proxy / nDSM |
| `preview_forest_*.png` / `preview_grade_near.png` | PNG-only forest-class and olive/snow grade (no remesh) |
| `backdrop_meshes/*.dae` + `backdrop_{diffuse,near,mid}.png` + `main.materials.json` | Game assets |
| `backdrop_meta.json` | Vert/tri counts, observer count, cfg dump |

Raw downloads stay gitignored under `data/raw/copernicus_glo30/`, `data/raw/worldcover/`, and `data/raw/dgm_*_near.tif`.

Inject target (BeamNG 0.39+):

```text
%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\levels\<level_name>\art\shapes\backdrop\
```

TSStatics live in `main\MissionGroup\level_objects\backdrop\`.

---

## BeamNG naming and look

`setup_beamng_level.py` **strips** the stock template backdrop. This tool puts a new SimGroup named `backdrop` back.

Rules that have already bitten this POC:

- **Material names must not equal any SimGroup or TSStatic name.** Materials are `mat_backdrop_far` / `_near` / `_mid`. The group stays `backdrop`. Objects are `backdrop_nw`, `backdrop_near_se`, `backdrop_mid_n`, … If a material is called `backdrop` or `backdrop_near`, BeamNG refuses to create the group/object (log: *identical name … classname: material*). Result: missing mesh or “no material”.
- **No digits in the Collada stem.** `backdrop_mid_00_a999` is parsed as LOD size **0** (collision / unlit). Use letter tiles (`_nw`, `_n`, `_aa`, …) plus the usual `_a999` visible LOD.
- One mesh **> 65 535 vertices** is dropped or invisible. Split into quadrants / `mid_tiles`.
- TSStatics: `collisionType: None`, `decalType: None`, `castShadows: false`.
- Template `visibleDistance` is ~7.5 km; inject raises it so the 40 km ring can draw. Portal arcs use the same knob.

Pale / chalky look is **not** a dedicated “push the background back” render flag. Swissimage is a pre-lit photo; the sun in the level lights it a second time (plus distance fog). Tune `albedo_gain` and rebuild. Noon vs evening mismatch is usually leftover baked hillshade — keep `ortho_hillshade` low and let the `normalMap` + engine sun do the work. Materials: roughness 0.86, slight warm `baseColorFactor` / `emissiveFactor`, `dynamicCubemap`.

`--textures-only` rewrites PNGs and retargets DAE `init_from`. **In this session that did not bind** in-game (level reload, file swap, and quit-without-remesh all kept the old albedo). Treat a grade change as a **full remesh** (`build_backdrop.py --skip-fetch --skip-worldcover`) plus a full BeamNG quit.

PNG-only iteration stays on `preview_backdrop_detect.py` / `preview_forest_detect.py` until the grade PNG is right.

---

## Not in this POC

- Seamless skirt that matches every playable texel (lip is a deliberate 1 m drop)
- Filling German / Italian voids in the near DGM with Copernicus
- EEA-10 DEM; optional near-ring tree billboard clumps (recolor BEV forest first)
- Streaming / one mesh for the whole 80 km square (vertex limit → tiles)
