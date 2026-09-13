# Stand und Roadmap

Kurzüberblick nach dem Hahntennjoch-Smoke-Test (BeamNG 0.39).  
Details: [BEAMNG_IMPORT.md](BEAMNG_IMPORT.md), [ANNOTATIONS.md](ANNOTATIONS.md), [KONZEPT.md](KONZEPT.md).  
Spätere Multi-Map-Session (nicht jetzt bauen): [TIROLRUNDE.md](TIROLRUNDE.md).

## Erledigt

### Sites
- **Hahntennjoch** — Default `config/site.yaml` / `config/sites/hahntennjoch.yaml` (Leitplanken/Gelände)
- **L13 Kühtai** — `config/sites/l13_kuehtai.yaml` (Galerien via GIP); aktiv mit `AUTOROAD_SITE`
- Processed getrennt: `data/processed/<site.name>/`

### Terrain / Straße (Hahntennjoch)
- DGM → 16-bit Heightmap + `roads_beamng.json` (BeamNG-Meter, BBOX gestreckt auf 512²)
- Terrain-Masken: Grass / dirt_rocky_large / rock / Asphalt (+ Bankett)
- `terrainPreset.json` → Level-`import/`

### Leitplanken
- Italy-Mesh, Stoß-an-Stoß; Orientierung `face_y_outward` / `yaw_flip_right: false`
- `lateral_extra_m: 0.55`, `abut_overlap_m: 0.08`
- **Zweistufig:** Seed → QGIS → `build_guardrails.py` liest Layer `guardrail` (`annotations.guardrail_source: auto`)
- Build schreibt **nie** die GPKG; leer/fehlend → OSM-Heuristik

### GIS-Annotationen
- Schema + Seed + GPKG-Consume für Guardrails — [ANNOTATIONS.md](ANNOTATIONS.md)
- Smoke-GPKG: `data/annotations/tirol-m28-test-500m.gpkg`

## Offen / als Nächstes

| Priorität | Thema | Notiz |
|-----------|--------|--------|
| 1 | QGIS-Feinschliff / Öffnungen | Lücken in `guardrail` testen |
| 2 | **Galerien/Brücken aus GIP** | Brücken-MVP: `build_bridges.py` → MeshRoad (Klammbach). Galerien/Hole-Maps offen. Level: `autoroad_galerie_test` |
| 3 | `road_edge` → Asphalt/Bankett-Masken | analog zweistufig |
| 4 | **Terrain-Look Alm/Moos** | `t_moss`-Texturen; eigenes TerrainMaterial |
| 5 | **Geologie / LISA** | Grundfarbe + Vegetations-Bias |
| 6 | DGM für L13 1024² neu laden | site.yaml bereits auf Kühtai-Quadrat |
| 7 | DecalRoad, Tunnel-Hermite, Gebäude | |
| — | **Tirolrunde** (Multi-Map Session) | Vision only — [TIROLRUNDE.md](TIROLRUNDE.md); nach mehreren spielbaren Pässen |

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
