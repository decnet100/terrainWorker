# beamng_autoroad

Semiautomatisierte Pipeline: Tiroler OGD → spielbare Passstraßen in **BeamNG.drive**.

## Stand

Smoke-Test **Hahntennjochstraße** (M28 / EPSG:31254, ~500×510 m), BeamNG 0.39:

- Heightmap + OSM-Straßen, Terrain-Masken (Gras/Dirt/Fels/Asphalt + Bankett)
- Leitplanken Stoß-an-Stoß (Italy-Mesh), Orientierung und Abstand grob stimmig
- Annotations-GPKG geseedet (Heuristik); Edit in QGIS vorgesehen
- Aktueller Stand + Roadmap: [docs/STATUS.md](docs/STATUS.md)
- Import: [docs/BEAMNG_IMPORT.md](docs/BEAMNG_IMPORT.md)

| Phase 1 (jetzt) | Als Nächstes |
|-----------------|--------------|
| Straße + Heightmap + Masken | Build aus Annotations-GPKG |
| Leitplanken (Heuristik) | Öffnungen / Rand in QGIS |
| GIS-Seed Schema | Moos-/Alm-Materials, Geologie/LISA |
| | Tunnel, Kamm-Impostors, Gebäude |

## Neu bauen

```powershell
cd C:\temp\beamng_autoroad
python -m pip install -r requirements.txt
python tools\fetch_osm.py
python tools\build_smoke.py
# oder nur Masken / Leitplanken:
# python tools\build_terrain_masks.py
# python tools\build_guardrails.py
# Annotations-GPKG (leer) anlegen / Heuristik seeden:
# python tools\init_annotations_gpkg.py
# python tools\seed_annotations.py
# python tools\seed_annotations.py --force   # nur bewusst Überschreiben
```

## Dokumente

- [docs/KONZEPT.md](docs/KONZEPT.md)
- [docs/STATUS.md](docs/STATUS.md) — Stand, Roadmap, Schnellbefehle
- [docs/DATENUEBERGABE.md](docs/DATENUEBERGABE.md)
- [docs/ANNOTATIONS.md](docs/ANNOTATIONS.md) — GIS: Straßenrand / Leitplanken / Öffnungen
- [docs/BEAMNG_IMPORT.md](docs/BEAMNG_IMPORT.md)
- [config/site.example.yaml](config/site.example.yaml) — Vorlage (`site.yaml` lokal, gitignore)
