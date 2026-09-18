# Status and roadmap

Snapshot after the Hahntennjoch smoke test and Fernpass Mega work (BeamNG 0.39).  
Details: [BEAMNG_IMPORT.md](BEAMNG_IMPORT.md), [ROADS.md](ROADS.md), [ANNOTATIONS.md](ANNOTATIONS.md), [CONCEPT.md](CONCEPT.md), [GIP.md](GIP.md), [HEIGHTMAP_COMPOSE.md](HEIGHTMAP_COMPOSE.md).  
Multi-map session (first portal switch): [ROADTRIP_TYROL.md](ROADTRIP_TYROL.md).

## Done

### Sites

- **Hahntennjoch** — default `config/site.yaml` / `config/sites/hahntennjoch.yaml` (`autoroad_m28_test`, 512²)
- **L13 Kühtai** — `config/sites/l13_kuehtai.yaml` (`autoroad_galerie_2048`, galleries via GIP)
- **L13 splining** — `config/sites/l13_splining.yaml` (Tyrol road-network axis + MeshRoad decks)
- **Fernpass 4096** — `config/sites/fernpass.yaml`
- **Fernpass Mega 8192** — `config/sites/fernpass_mega.yaml` (B179, GIP decals, bridges/galleries, Italy rails)
- **Testarena** — `config/sites/testarena.yaml` (crop of a GIP structure; road surface first)
- Processed output is split: `data/processed/<site.name>/`

`config/sites/oetz.yaml` is still a copy of the site template, not a finished Ötztal map.

### Terrain / road

- DGM → heightmap + `roads_beamng.json`
- **Lanes→width** (default 2×3.75 m): [ROADS.md](ROADS.md)
- Terrain masks + cache (land cover / slope); decal stitch + width smooth; road-bed conform
- Heightmap: immutable DGM + layers → `heightmap_<N>_composed.png` — [HEIGHTMAP_COMPOSE.md](HEIGHTMAP_COMPOSE.md)
- `terrainPreset.json` → level `import/` (import writes `theTerrain.ter`; reload alone does not)

### Guardrails / delineators

- Fernpass Mega: **`style: sections`** (Italy rail). Delineator posts only when intended / per rule (`posts` / `both`)
- Axis = GIP OBJECTIDs; on bridges, deck Z + tighter lateral — [GIP.md](GIP.md#centerline-sources-fernpass-mega)
- `--clear` + unique names against duplicates
- GPKG path unchanged — [ANNOTATIONS.md](ANNOTATIONS.md)

### GIS annotations

- Schema + seed + GPKG consume for guardrails — [ANNOTATIONS.md](ANNOTATIONS.md)

### Roadtrip Tyrol

- First hard switch Hahntennjoch ↔ Fernpass Mega — [ROADTRIP_TYROL.md](ROADTRIP_TYROL.md)

### Other tools (present, still evolving)

- Backdrop meshes (Copernicus GLO-30 + viewshed): `fetch_backdrop.py` / `build_backdrop.py`
- Forest, TWI, snow proxy, biome compose: `build_forest.py`, `build_twi.py`, `build_snow_proxy.py`, `compose_biomes.py`

## Open / next

| Priority | Topic | Note |
|----------|--------|------|
| 1 | Stronger road-bed / re-import check | Micro-bumps: `road_bed_smooth_m` + re-import the heightmap |
| 2 | QGIS guardrail polish | Gaps in `guardrail` |
| 3 | Span principle bridge/gallery/tunnel | Heightmap at abutments, not under the slab — [HEIGHTMAP_COMPOSE.md](HEIGHTMAP_COMPOSE.md) |
| 4 | `road_edge` → asphalt / shoulder masks | same two-step workflow |
| 5 | Terrain look meadow/moss | `t_moss` |
| — | Roadtrip Tyrol product | Segment times, more gates, traffic, damage — [ROADTRIP_TYROL.md](ROADTRIP_TYROL.md) |

## Deliberately not automatic

- `seed_annotations.py` does **not** run inside `build_smoke` / `build_guardrails`
- `config/site.yaml` and `data/raw|processed` stay local (gitignore)

## Quick commands

```powershell
cd C:\temp\beamng_autoroad; python tools\seed_annotations.py
```

```powershell
cd C:\temp\beamng_autoroad; python tools\seed_annotations.py --force
```

```powershell
cd C:\temp\beamng_autoroad; python tools\fetch_gip.py
```

```powershell
cd C:\temp\beamng_autoroad; python tools\fetch_gip.py --force
```

```powershell
cd C:\temp\beamng_autoroad; python tools\build_guardrails.py
```

```powershell
cd C:\temp\beamng_autoroad; python tools\build_terrain_masks.py
```
