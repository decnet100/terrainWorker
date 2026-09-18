# Roadtrip Tyrol

Open Tyrolean geodata → playable mountain-pass roads in **BeamNG.drive**.

This repo is the pipeline (`beamng_autoroad`). **Roadtrip Tyrol** is the multi-map session: you drive one pass, linger in a portal, and hard-switch to the next map with weather and vehicle config kept.

## Status

BeamNG **0.39**. Details: [docs/STATUS.md](docs/STATUS.md). First portal switch: [docs/ROADTRIP_TYROL.md](docs/ROADTRIP_TYROL.md).

| Site | Level | Size | Role |
|------|--------|------|------|
| **Hahntennjoch** | `autoroad_m28_test` | 512² | Default `config/site.yaml`; guardrails + terrain |
| **Fernpass Mega** | `autoroad_fernpass_8192` | 8192² | Flagship B179 (GIP decals, bridges, galleries, rails) |
| **Fernpass** | `autoroad_fernpass_4096` | 4096² | Smaller B179 crop |
| **L13 Kühtai** | `autoroad_galerie_2048` | 2048² | Galleries / bridges |
| **L13 splining** | `autoroad_l13_splining` | 2048² | MeshRoad decks from the Tyrol road network |
| **Testarena** | `autoroad_testarena` | 512² | Specimen crop of a GIP structure (road surface first) |
| **Reschen** | `autoroad_reschen_8192` | 8192² | B180 Reschenstraße (Prutz / Kaunertal), first-look auto |

Site files: [config/sites/](config/sites/README.md). Processed output: `data/processed/<site.name>/`.

## Build

```powershell
cd C:\temp\beamng_autoroad; python -m pip install -r requirements.txt
```

**One-shot** (fresh clone → BeamNG user level with biomes, terrain materials, forest):

```powershell
cd C:\temp\beamng_autoroad; python tools\build_level.py --site config/sites/fernpass_mega.yaml
```

That runs DGM/DOM/BEV → TWI/snow → smoke/masks → level setup → `dry_meadow`/`ForestFloor` materials → compose biomes → forest. Steps: `python tools\build_level.py --list`. Resume with `--from`, single step with `--only`. Existing level folders are kept unless `--force-setup`.

Default site is a local copy of Hahntennjoch (`config/site.yaml`, gitignored). To pin a site for one session:

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/hahntennjoch.yaml"; python tools\fetch_osm.py; python tools\build_smoke.py
```

Fernpass Mega uses **GIP** for the road axis (not OSM). After `build_level`, typical extras:

```powershell
cd C:\temp\beamng_autoroad; $env:AUTOROAD_SITE = "config/sites/fernpass_mega.yaml"; python tools\fetch_gip.py
```

```powershell
cd C:\temp\beamng_autoroad; python tools\init_annotations_gpkg.py
```

```powershell
cd C:\temp\beamng_autoroad; python tools\seed_annotations.py
```

Level import: [docs/BEAMNG_IMPORT.md](docs/BEAMNG_IMPORT.md). Horizon meshes: [docs/BACKDROP.md](docs/BACKDROP.md). Portal mod: [docs/ROADTRIP_TYROL.md](docs/ROADTRIP_TYROL.md). Site profiles: [config/sites/README.md](config/sites/README.md).
## Docs

- [docs/CONCEPT.md](docs/CONCEPT.md) — geodata → playable pass
- [docs/STATUS.md](docs/STATUS.md) — current state, roadmap, commands
- [docs/ROADTRIP_TYROL.md](docs/ROADTRIP_TYROL.md) — multi-map session (Hahntennjoch ↔ Fernpass)
- [docs/SITE_DATA.md](docs/SITE_DATA.md) — WCS/WFS links, bbox, local tiles
- [docs/ANNOTATIONS.md](docs/ANNOTATIONS.md) — GIS: road edge / guardrails / gaps
- [docs/GIP.md](docs/GIP.md) — Tyrol road WFS / structures (cache)
- [docs/ROADS.md](docs/ROADS.md) — width, decals, road-bed
- [docs/HEIGHTMAP_COMPOSE.md](docs/HEIGHTMAP_COMPOSE.md) — DGM + layers → composed PNG
- [docs/BEAMNG_IMPORT.md](docs/BEAMNG_IMPORT.md) — create level, import terrain
- [docs/BACKDROP.md](docs/BACKDROP.md) — far/mid/near horizon meshes (Copernicus + viewshed)
- [config/site.example.yaml](config/site.example.yaml) — template (`site.yaml` is local, gitignored)

Internal Lua/mod paths still use the identifier `tirolrunde` so existing BeamNG installs keep working. The name you see in the game and in this documentation is **Roadtrip Tyrol**.
