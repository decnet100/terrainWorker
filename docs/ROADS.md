# Roads: width, decals, road-bed

State after Fernpass Mega polish (lanes→width, decal transitions, delineators).

Related: [BEAMNG_IMPORT.md](BEAMNG_IMPORT.md), [GIP.md](GIP.md) (**centerline sources**), `tools/road_width.py`. Local cracks / patches / manholes on a future road Collada: [ROAD_SURFACE_DETAIL.md](ROAD_SURFACE_DETAIL.md) (idea only).

**Axis (Fernpass Mega):** decals / guardrails / bridges / galleries → **GIP**, not OSM. Unnamed GIP (Gassen) get terrain asphalt + road-bed only — [GIP.md](GIP.md).

---

## Carriageway width (lanes → metres)

**Default:** `default_lanes × lane_width_m` = **2 × 3.75 m = 7.5 m**  
(`beamng.lane_width_m` / `beamng.default_lanes` in the site YAML).

**GIP defaults (all sites, node metres before `width_scale`):**

| Piece | Width | Surface |
|-------|-------|---------|
| `S-F` Forstweg | 4.0 m | terrain gravel (`dirt_material`), no road-bed |
| `S-GW` Wirtschaftsweg | 4.0 m | terrain gravel, no road-bed |
| `S-FRW` / `S-STRAIL` Fuß- und Radweg | 2.5 m | terrain gravel, no road-bed |
| `S-G` örtliches Netz | 5.0 m | terrain asphalt (no Decal if `gip_decals: named`) |
| `S-AR` / `S-AP` / `S-BR` / `S-BP` | 1 × `lane_width_m` | subordinate lane; no own rails; solid edge lines, no dashed center |
| `B*` Bundesstraße, 2 Fahrbahnen | 8.0 m | full kit |
| `B*` with `lanes_by_str_code` ≥ 3 | `lanes × lane_width_m` | full kit |
| `L*` | site default, catalog `L*: 7.0`, or Landnutzung sample | full kit |

YAML `beamng.roads.width_by_objectid` (rare) and `data/roads/gip_overrides.yaml` override a named piece. Example: catalog `L*: 7.0` when Landnutzung has no usable sample; Decal/terrain still multiply `width_scale` (Reschen 0.9 → 6.3 m).

### Where is what?

| Place | Content |
|-------|---------|
| `data/roads/gip_widths.json` | six Landnutzung samples per GIP OBJECTID + mean width |
| `data/roads/gip_overrides.yaml` | not-tunnel flags, extra side-cut OIDs, rare width overrides, `L*: 7.0` fallback |
| Site YAML | level crop, materials, MeshRoad recipes; optional `width_by_objectid` |
| `data/processed/.../gip_roads_beamng.json` | **final** node widths `[x,y,z,width]` — what decals / masks / guardrails use |

GIP widths are **not** stored per site. Fernpass 4096 and 8192 share the same OBJECTIDs.

### Where is what?

1. Site YAML `beamng.roads.width_by_objectid`
2. Catalog `width_by_objectid`
3. Landnutzung mean (`applied: true` in `gip_widths.json`)
4. Site YAML `width_by_str_code` / catalog `width_by_str_code` (`L*`)
5. Class defaults in the table above

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"; python tools\measure_gip_widths.py --all-known
```

### Lookup order (`tools/road_width.py` → `build_smoke`)

1. OSM tag `width` (m), else  
2. OSM **`lanes`** (total count) × `lane_width_m`, else  
3. Only if `lanes` is missing **and** both `lanes:forward` **and** `lanes:backward` are set: sum × `lane_width_m`, else  
4. `lanes_by_highway[highway]` or `default_lanes` × `lane_width_m`

**Important (OSM):** `lanes=` is the **total**. On two-way roads `lanes=2` = one lane each way.  
A lone `lanes:backward=1` next to `lanes=2` must **not** overwrite the total (common Fernpass tagging) — otherwise you get false 3.75 m stretches.

### Recompute

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"; python tools\build_smoke.py
```

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"; python tools\build_decal_roads.py
```

OSM cache only if Overpass raw data is stale: `data\raw\osm_roads_tirol-fernpass-8192.json`.

---

## Decal transitions (stitch + width smooth)

OSM splits the B179 into many short ways. Without merge you get gaps; with merge, short narrow stubs remain as **waists** (2–1–3…).

`build_decal_roads`:

1. **`stitch_abutting`** — merge degree-2 end-to-end (`stitch_tol_m`)
2. **`width_fill_dip_m`** (default 40) — morphologically close short narrow dips
3. **`width_blend_m`** (default 25) — remaining lane jumps ease out
4. gallery/tunnel/bridge clip always; `terrain_roof: keep_asphalt` is **not** cut away (e.g. tunnel 8082)

```powershell
cd C:\temp\beamng_autoroad; python tools\build_decal_roads.py --skip-road-bed
```

```powershell
cd C:\temp\beamng_autoroad; python tools\build_decal_roads.py
```

---

## Road-bed (terrain under the road)

**What:** a smoothed height band under the carriageway in the heightmap — not the decal texture. Goal: damp DGM micro-spikes so decal and terrain share a grade.

**When active:** `beamng.decal_roads.road_bed_conform: true` and build **without** `--skip-road-bed`.

**Output:** `data/processed/<site>/heightmap_<size>_road_bed.png` (+ sync to level `import/`).

**Important knobs:**

| Key | Role |
|-----|------|
| `road_bed_smooth_m` | along-track window for target Z (larger = smoother grade) |
| `road_bed_pad_m` / `falloff_m` | width of the stamped band |
| `road_bed_sink_m` | bed a little under decal Z |
| `road_bed_max_raise_m` / `max_cut_m` | clamp against DGM |
| `road_bed_items` | per-road override; `match.str_code` (`L17` or `L*`) or `match.objectid` / `road_id` |
| `beamng.roads.follow_parent` | child Z tracks a parent axis for `hold_m` from the join, then `blend_m` back to DGM ([GIP.md](GIP.md), recipe [STRUCTURES.md](STRUCTURES.md)) |
| `beamng.roads.side_cut_preserve` | GIP types/OBJECTIDs that block MeshRoad side-cut beside a deck ([STRUCTURES.md](STRUCTURES.md)) |

Decals: `snap_to_heightmap: true` → node Z from the road-bed (if present).

**“New in the terrain”:** writing the PNG is not enough. Re-import the heightmap / `terrainPreset.json` in the World Editor and save the level — otherwise you still drive on the old terrain geometry. Micro-bumps despite smooth = often a forgotten re-import.

---

## Delineators / guardrails

Fernpass Mega default: **`style: sections`** (Italy rail mesh). Delineator posts (`style: posts`, mesh `reflector`, `post_scale: 0.7` ~1 m, `spacing_m: 25`) only when intended or via `both` / rules.

```powershell
cd C:\temp\beamng_autoroad; python tools\build_guardrails.py --clear
```

Reload the level (do not save if duplicates from an old session are still in the tree).

```powershell
cd C:\temp\beamng_autoroad; python tools\build_guardrails.py
```

Reload the level again.

Italy rails: `style: sections` + `italy_guardrails_common_section`.

---

## Terrain-mask cache

`build_terrain_masks` caches Tyrol land cover + DGM slope under  
`processed/<site>/cache/terrain_masks/`. Roads / bridges / galleries are always recomputed.

```powershell
cd C:\temp\beamng_autoroad; python tools\build_terrain_masks.py
```

```powershell
cd C:\temp\beamng_autoroad; python tools\build_terrain_masks.py --force
```
