# Backdrop meshes (horizon)

Far scenery around the **playable** heightmap: Collada TSStatics with `collisionType: None`, not a bigger `.ter`. Fernpass Mega is the configured site (`beamng.backdrop` + `sources.backdrop_ortho`). Other sites work from the same YAML keys / code defaults.

Related: [BEAMNG_IMPORT.md](BEAMNG_IMPORT.md) (playable terrain), [HEIGHTMAP_COMPOSE.md](HEIGHTMAP_COMPOSE.md) (Z datum), [ROADTRIP_TYROL.md](ROADTRIP_TYROL.md) (portal sky-arc later sits in front of this).

Status: **POC that already injects**. Not a replacement for Forest / terrain paint on the playable map.

---

## Three rings

All Z is **absolute GHA minus** `heightmap_meta.json` → `z_min_m` (same datum as the playable PNG). Run `build_smoke.py` first.

| Ring | Geometry | DEM | Texture | Mesh step (Fernpass Mega) |
|------|----------|-----|---------|---------------------------|
| **Near** | bbox → `near_m` (~1 km). Hole = the playable bbox. | Tirol WCS DGM (`near_dgm_res_m`, default 2 m). Cells outside Tyrol stay empty — **no Copernicus fill**. | SWISSIMAGE crop of the near extent | `near_step_m` 15 m |
| **Mid** | `near_m` → `mid_m` (~10 km), with `near_overlap_m` / `mid_overlap_m` so rings overlap. **Always kept** (no viewshed). | Copernicus GLO-30 | SWISSIMAGE crop of the mid extent | `mid_step_m` 50 m |
| **Far** | out to `radius_m` (~40 km), minus a hole through mid. **Viewshed-culled**. | Copernicus GLO-30 @ `viewshed_step_m` (50 m) | SWISSIMAGE of the full padded square | `mesh_step_m` 120 m |

Near inner edge: vertices on the playable bbox are snapped to the bbox, Z clamped to the composed heightmap, then dropped by `near_lip_drop_m` (default 1 m) so the ring sits as a **cliff under the lip**, not tucked onto the driveable terrain.

---

## Tools

| Script | Role |
|--------|------|
| `tools/fetch_backdrop.py` | Download + warp rasters into `data/processed/<site>/` |
| `tools/build_backdrop.py` | Viewshed, textures, Collada, optional inject into the BeamNG level |

`build_backdrop` calls fetch unless `--skip-fetch` and a cached DEM already exist.

### Fetch sources

- **DEM (far/mid):** Copernicus GLO-30 public COGs on AWS (`data/raw/copernicus_glo30/`). Warped to the site CRS. EEA-10 is a later swap, not wired.
- **DEM (near):** same Tirol DGM WCS as `fetch_dgm.py`, larger bbox (`near_m` + 200 m). Invalid / nodata (including ~0 m outside AT) → empty cells.
- **Ortho:** SWISSIMAGE Hintergrund via `wms.geo.admin.ch` (`ch.swisstopo.swissimage`). Three GetMap requests (far / near / mid extents), then resampled onto the site-CRS texture grid. Attribution is written to `*.meta.json`.
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
2. Ray viewshed per observer (`n_rays`, earth curvature `curvature_cc` × \(d^2 / 2R\)). Keep far cells with `count >= min_views`, dilate, clip to `radius_m`, punch the mid hole.
3. Bake RGB (ortho × light hillshade, else WorldCover LUT / hypsometric).
4. Mesh quads where the keep-mask is solid. Face winding is **+Z / sky** (right-hand). No collision, no extra reverse winding.
5. Split meshes so each DAE stays under BeamNG’s **16-bit** vertex index limit (65 535): far/near = four quadrants `nw|ne|sw|se`; mid = `mid_tiles`² (default 3×3) with **letter** names (`nw`, `n`, `ne`, …). Digits in the stem (`_00`) are treated as LOD size 0 and render unlit — do not use them.
6. Copy DAE/PNG/`main.materials.json` to `levels/<level>/art/shapes/backdrop/` and inject TSStatics under SimGroup `backdrop`. Raise `LevelInfo.visibleDistance` to cover `radius_m`.

---

## Commands

Quit BeamNG if you will inject. Smoke must already have written `heightmap_meta.json`.

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"; python tools\fetch_backdrop.py
```

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"; python tools\build_backdrop.py
```

Fetch flags: `--force` (ignore raster cache), `--skip-worldcover`, `--skip-ortho`, `--skip-near-dgm`.

Build flags: `--skip-fetch` (use processed rasters; still may refresh near DGM / mid ortho), `--no-inject` (write `data/processed/<site>/backdrop_meshes/` only), `--mesh-step` (override far `mesh_step_m`), same `--skip-*` as fetch.

Reload the level after inject. **Do not Save Level** over the injected `items.level.json` from an old editor session.

---

## `beamng.backdrop` knobs

Defaults live in `fetch_backdrop.backdrop_cfg`. Fernpass Mega overrides:

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
| `mesh_step_m` | 120 | Far triangle spacing |
| `hole_pad_m` | 30 | Minimum far hole around the playable bbox (also at least mid−overlap) |
| `near_m` | 1000 | Near ring outer distance from bbox |
| `near_step_m` | 10 (Mega: 15) | Near triangle spacing |
| `near_dgm_res_m` | 2 | Near WCS request resolution |
| `near_overlap_m` | 80 | Mid starts this far inside `near_m` |
| `near_lip_drop_m` | 1 | Drop near verts on the bbox vs playable Z |
| `near_texture_size` | 2048 | Near PNG |
| `mid_m` | 10000 | Mid ring outer distance |
| `mid_step_m` | 50 | Mid triangle spacing |
| `mid_overlap_m` | 150 | Far hole starts this far inside `mid_m` |
| `mid_texture_size` | 2048 | Mid PNG |
| `mid_tiles` | 3 | Mid Collada grid (letter names) |
| `texture_size` | 2048 | Far PNG |

---

## Outputs (`data/processed/<site>/`)

| File | Role |
|------|------|
| `backdrop_dem.tif` + `.meta.json` | Warped GLO-30 |
| `backdrop_near_dgm.tif` + `.meta.json` | Tirol DGM ring |
| `backdrop_ortho.png` / `_near_` / `_mid_` + `.meta.json` | SWISSIMAGE grids |
| `backdrop_worldcover.tif` | Optional class raster on the DEM grid |
| `preview_backdrop_viewshed.png` | Hillshade + keep colours + bbox + observers |
| `preview_backdrop_*.png` | Diffuse / near / mid / viewshed-count |
| `backdrop_meshes/*.dae` + `backdrop_{diffuse,near,mid}.png` + `main.materials.json` | Game assets |
| `backdrop_meta.json` | Vert/tri counts, observer count, cfg dump |

Raw downloads stay gitignored under `data/raw/copernicus_glo30/` and `data/raw/worldcover/`.

Materials: `mat_backdrop_far` / `_near` / `_mid`, `castShadows: false`.

---

## BeamNG notes

- `setup_beamng_level.py` **strips** the stock template backdrop. This tool puts a new SimGroup `backdrop` back.
- TSStatics: `collisionType: None`, `decalType: None`, `castShadows: false`.
- Template `visibleDistance` is ~7.5 km; inject raises it so the 40 km ring can draw. Portal arcs use the same knob.
- Reload Freeroam after inject. World Editor Save can freeze a stale `items.level.json`.

---

## Not in this POC

- Seamless skirt that matches every playable texel (lip is a deliberate 1 m drop)
- Filling German / Italian voids in the near DGM with Copernicus
- EEA-10 DEM, winter / snow textures, forest cards on the backdrop
- Streaming / one mesh for the whole 80 km square (vertex limit → tiles)
