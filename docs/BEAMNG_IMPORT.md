# BeamNG import

**File → New Level** in the World Editor is unreliable here.  
**Standard path:** `python tools\setup_beamng_level.py <name>` — unpack the template, rewrite paths, strip ocean/props, copy import assets.

User data (BeamNG **0.39+**):

```text
C:\Users\<user>\AppData\Local\BeamNG\BeamNG.drive\current\levels\
```

| Site | Level folder | Heightmap | Notes |
|------|--------------|-----------|-------|
| Hahntennjoch | `autoroad_m28_test` | `heightmap_512.png` | default site |
| L13 Kühtai | `autoroad_galerie_2048` | `heightmap_2048.png` | galleries |
| L13 splining | `autoroad_l13_splining` | `heightmap_2048.png` | MeshRoad decks |
| Fernpass | `autoroad_fernpass_4096` | `heightmap_4096.png` | B179 crop |
| Fernpass Mega | `autoroad_fernpass_8192` | `heightmap_8192.png` | flagship |
| Reschen | `autoroad_reschen_8192` | `heightmap_8192.png` | B180 |
| Testarena | `autoroad_testarena` | `heightmap_512.png` | specimen crop |

Always take `max_height_m` from `data/processed/<site>/heightmap_meta.json`. Heightmaps are **square**, **power of two**, **16-bit PNG**.

Portal session between the finished levels: [alpine-roadtrip](https://github.com/decnet100/alpine-roadtrip). Horizon meshes around the playable map: [BACKDROP.md](BACKDROP.md) (`build_backdrop.py` after the heightmap exists).

---

## Carriageway width (lanes)

Default model: **`default_lanes` × `lane_width_m` = 2 × 3.75 m = 7.5 m** (`beamng` in the site YAML).

Details, OSM traps (`lanes` vs `lanes:backward`), decal transitions and **road-bed**: [ROADS.md](ROADS.md).

Short: `tools/build_smoke.py` → `roads_beamng.json` (node `width`); then rebuild decals/masks.

---

## 1. Create a level from the template (script)

**Do not** use File → New Level and **do not** use an old Expand-Archive one-liner — zip layout and paths `/levels/template/...` break easily.

**Quit the game**, then:

```powershell
cd C:\temp\beamng_autoroad; python tools\setup_beamng_level.py autoroad_m28_test --site config/sites/hahntennjoch.yaml
```

```powershell
cd C:\temp\beamng_autoroad; python tools\setup_beamng_level.py autoroad_galerie_2048 --site config/sites/l13_kuehtai.yaml
```

```powershell
cd C:\temp\beamng_autoroad; python tools\setup_beamng_level.py autoroad_fernpass_8192 --site config/sites/fernpass_mega.yaml
```

```powershell
cd C:\temp\beamng_autoroad; python tools\setup_beamng_level.py
```

Replace an existing folder: `--force`.

The script:

- unpacks `content\levels\template.zip` to  
  `%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\levels\<name>\`
- rewrites every `/levels/template` → `/levels/<name>` (important!)
- sets `info.json` (title, `isAuxiliary=false`)
- removes Ocean/WaterPlane + template backdrop/groundcover
- makes `theTerrain.terrainFile` writable, position `0,0,0`, `maxHeight` from site meta
- places a spawn near the origin
- copies `import/` from `data/processed/...` when the site matches

It ends with `READY: ...` and the next BeamNG steps.

Then:

1. BeamNG → Freeroam → load the level (not “New Level”).
2. **F11** → section **2. Import heightmap**.
3. After import: `python tools\build_smoke.py` (with the matching `AUTOROAD_SITE`) for mask sync + guardrails.

---

## 2. Import heightmap

**Important:** `theTerrain` → Inspector → `terrainFile` must be  
`/levels/<your_level>/theTerrain.ter` (**not** `/levels/template/...`).  
Otherwise the import does not persist (the template path is read-only).

1. Toolbar → Landscape/Default → **Terrain Tools** → **Import Terrain**
2. Dialog:
   - Terrain name: `theTerrain`
   - Heightmap: `/levels/<level>/import/heightmap_*.png`  
     (or copy files from `data/processed/...` into `import\` first)
   - **Meters per Pixel:** `1.0` (unless the site YAML says otherwise)
   - **Max Height:** value from `heightmap_meta.json` → `max_height_m`
   - Position: **`0, 0, 0`** (not template −512/−512/100)
3. **Import** → **File → Save Level**
4. Delete/disable the template **ocean** (otherwise water at Z≈116)

`import/heightmap_<N>.png` is the **composed** map (DGM + layers), not the raw DGM.  
Reload **without** this import leaves `theTerrain.ter` unchanged. Mixer and pitfalls: [HEIGHTMAP_COMPOSE.md](HEIGHTMAP_COMPOSE.md).

Often black afterwards → layer masks (next section).

### Terrain materials from masks

In 0.39, opacity / layer maps are called **“Texture Maps”** — the list starts **empty**.

**Preset (simplest path):**

1. `python tools\build_terrain_masks.py` (with matching `AUTOROAD_SITE`) — syncs to `levels/<level>/import/`
2. Import dialog → **Load...** → `terrainPreset.json`
3. Materials: Grass / dirt_rocky_large / rock / **Asphalt**, channel **R**
4. Groundmodels: `GRASS` / `DIRT_ROCKY_LARGE` / `ROCK` / `ASPHALT`
5. Position `0,0,0` → **Import** → save

**Manual:** Texture Maps → **Add Texture Map**:

- `layerMap_0_Grass.png`
- `layerMap_1_dirt_rocky_large.png`
- `layerMap_2_rock.png`
- `layerMap_3_Asphalt.png`

Carriageway width: OSM or GIP (~7 m × `road_width_scale`) + dirt shoulder (`shoulder_m`).

The file dialog only sees the **game VFS** (`/levels/...`), not `C:\temp\...`. Files must sit under `levels\<level>\import\`.

Preview after build: `preview_terrain_materials.png`.

Official docs: [Terrain / heightmaps](https://docs.beamng.com/modding/levels/level_creation/section2/)

### Playable snow (`SnowTirol`)

Stock terrain `snow` is almost white (base albedo mean ~212/255) and very matte. It takes **little Time-of-Day / sky colour** compared with the backdrop (photo × `albedo_gain` on a normal PBR material). Do not edit stock `snow` — clone it.

`tools/ensure_terrain_materials.py` writes `SnowTirol` (same maps, groundmodel `SNOW`):

- darker base PNG `t_terrain_base_snowtirol_b.png` via `beamng.snow_albedo_gain` (Reschen `0.75` → mean ~159). **This is the colour-reception knob.** Lower = more sunset/sky tint. `1.0` = stock chalk.
- less `roughness*Strength`, more `normal*Strength` (sheen / slope light — not the same as tint).

Reschen: `snow_material: SnowTirol`. After the first name change, re-import `import/terrainPreset.json` so layer 8 is `SnowTirol`. Later albedo-gain tweaks only need:

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/reschen.yaml"; python tools\ensure_terrain_materials.py
```

Then **quit BeamNG** (materials stay in memory). No heightmap re-import.

`compose_biomes.py` maps `SnowTirol` → groundmodel `SNOW`.

---

## 3. Guardrails

Details: [ANNOTATIONS.md](ANNOTATIONS.md)

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/hahntennjoch.yaml"; python tools\seed_annotations.py
```

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/hahntennjoch.yaml"; python tools\build_guardrails.py
```

Writes TSStatics to  
`...\levels\<level>\main\MissionGroup\level_objects\guardrails\items.level.json`

Default for many sites: **delineator posts** (`style: posts`, mesh `reflector.dae` ≈1.43 m native, `post_scale: 0.7` ≈1.0 m, spacing `spacing_m` 25 or 50).  
The build vendors mesh + `main.materials.json` into `art/shapes/objects/` (otherwise materials go missing on cross-level refs).  
Fernpass Mega uses continuous Italy rails: `style: sections` + `italy_guardrails_common_section`.

**Remove duplicates / reset:**

```powershell
cd C:\temp\beamng_autoroad; python tools\build_guardrails.py --clear
```

Reload the level (do not save).

```powershell
cd C:\temp\beamng_autoroad; python tools\build_guardrails.py
```

Reload the level again.

Config: `beamng.guardrails` + `annotations.guardrail_source` (`auto`|`gpkg`|`heuristic`).

- `auto`: non-empty `guardrail` layer → GPKG, else OSM heuristic
- `style` / `spacing_m` / `post_scale` — delineators
- `--clear` — empty the SimGroup only
- `align_pitch` / `snap_to_heightmap` / `section_length_m` / `abut_overlap_m` — see site YAML (`sections`)
- `yaw_flip_right` / `face_y_outward` — W-profile / reflector toward the road
- Height: `pivot_ground_offset_m` / `z_lift_m`; heuristic offset: `lateral_extra_m`

---

## If the road is gone / spawn is wrong

**Spawn:** often still template `[32,32,5]` (corner). Freeroam also needs a **named** spawn + `info.json`.

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/l13_kuehtai.yaml"; python tools\fix_beamng_level.py
```

Quit BeamNG first. Script pick so far: longest / highest OSM class, point near map centre, Z = heightmap + lift.

**Set the spawn in the World Editor (recommended):**

1. Load the level → F11
2. Scene Tree: `MissionGroup` → `PlayerDropPoints` → object **`spawns_default`**
3. Move tool (W) onto the road; Z a little above the ground
4. File → Save Level
5. Restart Freeroam (or respawn the vehicle) — `info.json` already points at `spawns_default`

**No asphalt surface after re-import:** usually the heightmap was imported **without** texture maps.  
`theTerrain.terrain.json` then still has e.g. `BeachSand` instead of `Asphalt`.

1. Terrain Tools → Import Terrain → **Load…** →  
   `/levels/<level>/import/terrainPreset.json`
2. Check: four texture maps, last = `layerMap_3_Asphalt.png`, material **Asphalt**, channel **R**
3. Position `0,0,0` → Import → Save Level → reload the level

Re-importing only the heightmap drops the layer assignment. Preview: `import/preview_terrain_materials.png` (dark = asphalt).

If `main/.../terrain/items.level.json` is empty (that sometimes happens after Save): `fix_beamng_level.py` restores the TerrainBlock.

### If the level does not start

1. Hard-quit the game (Task Manager).
2. **Do not** File → New Level again with an empty folder.
3. Load an official map (`smallgrid` / `gridmap_v2`) — if that works, check the template path and repeat section 1.
4. Console `` ` `` (below Esc); log:  
   `%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\beamng.log`  
   (older installs maybe `%LOCALAPPDATA%\BeamNG.drive\<version>\`)
5. Disable mods briefly, try again.

**Do not** put levels in `Documents\BeamNG.drive` or `BeamNG.drive\0.36\` — the pipeline syncs to `BeamNG\BeamNG.drive\current\levels\`.

---

## Rebuild

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/hahntennjoch.yaml"; python tools\fetch_dgm.py
```

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/hahntennjoch.yaml"; python tools\build_smoke.py
```

Fernpass Mega:

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"; python tools\fetch_gip.py
```

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"; python tools\build_smoke.py
```

Horizon (Fernpass Mega): [BACKDROP.md](BACKDROP.md)

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"; python tools\build_backdrop.py
```
