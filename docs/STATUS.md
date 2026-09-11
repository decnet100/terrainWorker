# Stand und Roadmap

Kurzüberblick nach dem Hahntennjoch-Smoke-Test (BeamNG 0.39).  
Details: [BEAMNG_IMPORT.md](BEAMNG_IMPORT.md), [ANNOTATIONS.md](ANNOTATIONS.md), [KONZEPT.md](KONZEPT.md).

## Erledigt

### Terrain / Straße
- DGM → 16-bit Heightmap + `roads_beamng.json` (BeamNG-Meter, BBOX gestreckt auf 512²)
- Terrain-Masken: Grass / dirt_rocky_large / rock / Asphalt (+ Bankett)
- `terrainPreset.json` → Level-`import/`

### Leitplanken
- Italy-Mesh `italy_guardrails_common_section`, Stoß-an-Stoß entlang **Straßenrand**-Sehne
- Orientierung: `face_y_outward: true`, `yaw_flip_right: false` (beide Seiten korrekt)
- `lateral_extra_m: 0.55`, `abut_overlap_m: 0.08`, Z etwas Richtung Asphalt sampeln
- Kein Pitch-Clamp (zerstört den Verbund); Pitch = echte 3D-Joint-Sehne
- `build_guardrails.py` schreibt **nur** Level-Items, **nie** die Annotations-GPKG

### GIS-Annotationen
- Schema: Layer `road_edge`, `guardrail`, `centerline` — [ANNOTATIONS.md](ANNOTATIONS.md)
- `tools/init_annotations_gpkg.py` — leere GPKG
- `tools/seed_annotations.py` — Heuristik → GPKG (CRS = Site); ohne `--force` keine Überschreibung
- Smoke-GPKG geseedet: `data/annotations/tirol-m28-test-500m.gpkg`
- Workflow-Ziel: Seed → QGIS editieren (Lücken = Einmündungen) → Build aus GPKG

## Offen / als Nächstes

| Priorität | Thema | Notiz |
|-----------|--------|--------|
| 1 | **Build liest GPKG** | `guardrail`/`road_edge` statt OSM-Offset; leer/fehlend → Heuristik-Fallback |
| 2 | **Terrain-Look Alm/Moos** | Stock: kein Almrosen-Material; Moos-Texturen unter `assets/.../forest/t_moss/` (u. a. automation_test_track). Eigenes `TerrainMaterial` klonbar; optional Soft-Cover splitten (Höhe/Hang/Exposition) |
| 3 | **Geologie / LISA** | Grundfarbe Dirt/Rock + Vegetations-Bias; Tirol Landnutzung (LISA) besser als OSM; GeoSphere-Geologie prüfen |
| 4 | QGIS-Feinschliff Ränder/Öffnungen | nach Seed editieren |
| 5 | DecalRoad / feinere Surfaces | |
| 6 | Gebogene Leitplanken / AssemblySpline | Stock hat keine echten R10/R15-Meshes |
| 7 | Tunnel/Galerien, Kamm-Impostors, Gebäude | Konzept Phase 1.5+ |

## Bewusst nicht automatisch

- `seed_annotations.py` läuft **nicht** in `build_smoke` / `build_guardrails`
- `config/site.yaml` und `data/raw|processed` bleiben lokal (gitignore)

## Schnellbefehle

```powershell
python tools\build_terrain_masks.py
python tools\build_guardrails.py
python tools\seed_annotations.py          # nur wenn GPKG-Layer leer
python tools\seed_annotations.py --force  # Heuristik bewusst neu seeden
```
