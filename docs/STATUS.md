# Stand und Roadmap

Kurzüberblick nach dem Hahntennjoch-Smoke-Test (BeamNG 0.39).  
Details: [BEAMNG_IMPORT.md](BEAMNG_IMPORT.md), [ROADS.md](ROADS.md), [ANNOTATIONS.md](ANNOTATIONS.md), [KONZEPT.md](KONZEPT.md), [GIP.md](GIP.md), [HEIGHTMAP_COMPOSE.md](HEIGHTMAP_COMPOSE.md).  
Spätere Multi-Map-Session (nicht jetzt bauen): [TIROLRUNDE.md](TIROLRUNDE.md).

## Erledigt

### Sites
- **Hahntennjoch** — Default `config/site.yaml` / `config/sites/hahntennjoch.yaml`
- **L13 Kühtai** — `config/sites/l13_kuehtai.yaml` (Galerien via GIP)
- **Mega-Fernpass 8192** — `config/sites/fernpass_mega.yaml` (B179, Decals, Brücken/Galerien, Leitpoller)
- Processed getrennt: `data/processed/<site.name>/`

### Terrain / Straße
- DGM → Heightmap + `roads_beamng.json`
- **Lanes→Breite** (Default 2×3.75 m): [ROADS.md](ROADS.md)
- Terrain-Masken + Cache (Landcover/Slope); Decal-Stitch + Width-Smooth; Road-Bed-Conform
- Heightmap: unveränderliches DGM + Layer → `heightmap_<N>_composed.png` — [HEIGHTMAP_COMPOSE.md](HEIGHTMAP_COMPOSE.md)
- `terrainPreset.json` → Level-`import/` (Import schreibt `theTerrain.ter`; Reload allein nicht)

### Leitplanken / Leitpoller
- Default: Leitpoller (`style: posts`, vendored `reflector`, `post_scale` ~1 m)
- `--clear` + eindeutige Namen gegen Doppelte; Italy-Schienen weiter per `style: sections`
- GPKG-Pfad unverändert — [ANNOTATIONS.md](ANNOTATIONS.md)

### GIS-Annotationen
- Schema + Seed + GPKG-Consume für Guardrails — [ANNOTATIONS.md](ANNOTATIONS.md)

## Offen / als Nächstes

| Priorität | Thema | Notiz |
|-----------|--------|--------|
| 1 | Road-Bed stärker / Re-Import-Check | Micro-Bumps: `road_bed_smooth_m` + Heightmap neu importieren |
| 2 | QGIS-Feinschliff Guardrails | Lücken in `guardrail` |
| 3 | Span-Prinzip Brücke/Galerie/Tunnel | Heightmap an Widerlagern, nicht unter der Platte — [HEIGHTMAP_COMPOSE.md](HEIGHTMAP_COMPOSE.md) |
| 4 | `road_edge` → Asphalt/Bankett-Masken | analog zweistufig |
| 5 | Terrain-Look Alm/Moos | `t_moss` |
| — | **Tirolrunde** | Vision — [TIROLRUNDE.md](TIROLRUNDE.md) |

## Bewusst nicht automatisch

- `seed_annotations.py` läuft **nicht** in `build_smoke` / `build_guardrails`
- `config/site.yaml` und `data/raw|processed` bleiben lokal (gitignore)

## Schnellbefehle

```powershell
python tools\seed_annotations.py          # Entwurf (nur wenn leer)
python tools\seed_annotations.py --force  # Heuristik bewusst neu seeden
python tools\fetch_gip.py                 # GIP/WFS Cache (Kunstbauten)
python tools\fetch_gip.py --force         # GIP neu laden
# … in QGIS editieren …
python tools\build_guardrails.py          # 3D aus GPKG (auto)
python tools\build_terrain_masks.py
```
